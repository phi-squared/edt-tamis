"""`service`: generate a background job for this machine (launchd, systemd or Windows Task Scheduler).

Everything is derived at run time (interpreter, package location, config path,
log folder), so the output is correct for whoever runs it, on their machine.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from . import APP, EdtError
from .config import Config
from .fsutil import atomic_write
from .paths import MACOS, WINDOWS, config_home, log_home

log = logging.getLogger("tamis")
PACKAGE_PARENT = Path(__file__).resolve().parent.parent  # where `python -m tamis` works


@dataclass
class Unit:
    kind: str
    text: str
    target: Path
    howto: str


def stable_python() -> str:
    """Prefer an interpreter path that survives `brew upgrade`; pythonw on Windows (no console)."""
    exe = Path(sys.executable)
    if WINDOWS:
        w = exe.with_name("pythonw.exe")
        return str(w if w.exists() else exe)
    if "/Cellar/" in str(exe) or "/opt/python@" in str(exe):
        for cand in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
            if Path(cand).exists():
                return cand
    return str(exe)


def default_kind() -> str:
    return "launchd" if MACOS else "windows" if WINDOWS else "systemd"


def program_args(mode: str, config: Path, logfile: Path | None) -> list[str]:
    args = ["-m", "tamis", "--config", str(config)]
    if logfile:
        args += ["--log-file", str(logfile)]
    args.append(mode)
    if mode == "publish":
        args.append("--watch")
    return args


def _plist(label: str, argv: list[str], workdir: Path, logs: Path) -> str:
    items = "\n".join(f"    <string>{xml_escape(a)}</string>" for a in argv)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>WorkingDirectory</key><string>{xml_escape(str(workdir))}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardErrorPath</key><string>{xml_escape(str(logs / 'launchd.err'))}</string>
</dict>
</plist>
"""


def _no_control_chars(s: str, what: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        raise EdtError(f"{what} contains a control character ({s!r}); cannot write it into a "
                       "service file")
    return s


def _systemd_arg(a: str) -> str:
    """One ExecStart word. Inside double quotes systemd still expands `%` specifiers and
    `$VARIABLES` and processes backslash escapes, so all of those are escaped."""
    a = _no_control_chars(a, "a path or argument")
    a = a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{a}"'


def _systemd(mode: str, argv: list[str], workdir: Path) -> str:
    cmd = " ".join(_systemd_arg(a) for a in argv)
    workdir_line = _no_control_chars(str(workdir), "the program folder").replace("%", "%%")
    return f"""[Unit]
Description={APP} ({mode})
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory={workdir_line}
ExecStart={cmd}
Restart=on-failure
RestartSec=30
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
"""


# PowerShell treats all of these as single-quote characters, not just ASCII '
_PS_QUOTES = "'\u2018\u2019\u201a\u201b"


def _ps_quote(s: str) -> str:
    s = _no_control_chars(s, "a path or argument")
    return "'" + "".join(c + c if c in _PS_QUOTES else c for c in s) + "'"


def _windows(name: str, python: str, args: list[str], workdir: Path) -> str:
    arg_line = subprocess.list2cmdline(args)      # the Windows command-line quoting rules
    return f"""# {APP}: registers a Task Scheduler job that starts at logon and restarts on failure.
# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File "<this file>"
$action   = New-ScheduledTaskAction -Execute {_ps_quote(python)} `
              -Argument {_ps_quote(arg_line)} `
              -WorkingDirectory {_ps_quote(str(workdir))}
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
              -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName {_ps_quote(name)} -Action $action -Trigger $trigger `
  -Settings $settings -Description 'Filtered university timetable feed' -Force | Out-Null
Start-ScheduledTask -TaskName {_ps_quote(name)}
Write-Host {_ps_quote("registered and started: " + name)}
"""


def render(cfg: Config, mode: str = "serve", kind: str | None = None,
           python: str | None = None) -> Unit:
    kind = kind or default_kind()
    python = python or stable_python()
    logs = log_home()
    label = f"local.{APP}.{mode}"
    if kind == "launchd":
        argv = [python, *program_args(mode, cfg.path, logs / f"{mode}.log")]
        target = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        howto = (f"launchctl bootout gui/$(id -u)/{label} 2>/dev/null\n"
                 f"launchctl bootstrap gui/$(id -u) '{target}'\n"
                 f"tail -f '{logs / (mode + '.log')}'")
        return Unit(kind, _plist(label, argv, PACKAGE_PARENT, logs), target, howto)
    if kind == "systemd":
        argv = [python, *program_args(mode, cfg.path, None)]
        unit = f"{APP}-{mode}"
        target = config_home().parent / "systemd" / "user" / f"{unit}.service"
        howto = (f"systemctl --user daemon-reload && systemctl --user enable --now {unit}\n"
                 f"loginctl enable-linger $USER    # keep running while logged out\n"
                 f"journalctl --user -u {unit} -f")
        return Unit(kind, _systemd(mode, argv, PACKAGE_PARENT), target, howto)
    if kind == "windows":
        name = f"{APP} {mode}"
        args = program_args(mode, cfg.path, logs / f"{mode}.log")
        target = config_home() / f"install-{mode}-task.ps1"
        howto = (f'powershell -ExecutionPolicy Bypass -File "{target}"\n'
                 f'# remove again:  Unregister-ScheduledTask -TaskName "{name}"\n'
                 f'# log file:      {logs / (mode + ".log")}')
        return Unit(kind, _windows(name, python, args, PACKAGE_PARENT), target, howto)
    raise ValueError(f"unknown service kind {kind!r}")


PROTECTED_MAC = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents")


def check_location(cfg: Config) -> None:
    if not MACOS:
        return
    for p in (PACKAGE_PARENT, cfg.path):
        if any(str(p).startswith(str(Path.home() / d)) for d in PROTECTED_MAC):
            log.warning("WARNING: %s is in a privacy-protected folder; a background job may be "
                        "denied access. Move it, e.g. to ~/edt-tamis and ~/.config.", p)


def install(unit: Unit) -> None:
    log_home().mkdir(parents=True, exist_ok=True)  # launchd will not create it
    unit.target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(unit.target,
                 unit.text.encode("utf-8-sig" if unit.kind == "windows" else "utf-8"))

