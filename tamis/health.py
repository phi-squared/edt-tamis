"""Making failures visible: warning events inside the calendar, and an optional alert hook.

Two things can go wrong, and they need different answers:

1. ADE cannot be reached (down, blocked, needs a login). The server keeps serving
   the last good copy - silently correct most of the time - but once a source has
   not been refreshed for `stale_after_hours`, an all-day warning event appears on
   *today* in every subscribed calendar, and `alert_command` runs once.

2. Your devices stop receiving the feed at all (server off, Tailscale down). The
   server can't tell you that, so the feed carries a "watchdog" event that is always
   dated `watchdog_days` in the future. As long as updates arrive it keeps moving
   ahead; if they stop, it stays put and eventually shows up on today.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import subprocess
import time

from .ade import SourceStatus
from .config import Config
from .fsutil import atomic_write, private_dir
from .ics import Event
from .safety import child_env

log = logging.getLogger("tamis")


def _day(cfg: Config, d: dt.date) -> dt.datetime:
    return dt.datetime.combine(d, dt.time.min, cfg.tz)


def _all_day(cfg: Config, uid: str, day: dt.date, summary: str, description: str) -> Event:
    return Event(uid=uid, start=_day(cfg, day), end=_day(cfg, day + dt.timedelta(days=1)),
                 summary=summary, location="", description=description, last_modified="",
                 source="edt-tamis", label="", note="", muted=True, is_exam=False,
                 tz=cfg.tz, all_day=True)


def stale(cfg: Config, health: list[SourceStatus], now: float | None = None) -> list[SourceStatus]:
    hours = cfg.number("stale_after_hours", 12, minimum=0.1, maximum=24 * 365)
    now = time.time() if now is None else now
    return [s for s in health
            if s.last_success is None or now - s.last_success > hours * 3600]


def summary_line(cfg: Config, bad: list[SourceStatus]) -> str:
    oldest = min((s.last_success or 0) for s in bad)
    when = dt.datetime.fromtimestamp(oldest, cfg.tz) if oldest else None
    return ("timetable NOT updated since " + (f"{when:%a %d %b %H:%M}" if when else "ever")
            + " - rooms/times may be outdated")


_URL = re.compile(r"(https?://[^\s/?#]+)[^\s]*")
_ABS_PATH = re.compile(r"(?<![\w:/.])(?:[A-Za-z]:\\|/)(?:[^\s/\\]+[/\\])+")


def _public(text: str) -> str:
    """An error text as it may appear inside a published feed: URLs reduced to their host
    (query strings can hold access tokens), folders removed from paths."""
    return _ABS_PATH.sub("", _URL.sub(r"\1/...", text))


def warning_events(cfg: Config, health: list[SourceStatus], now: float | None = None) -> list[Event]:
    now = time.time() if now is None else now
    today = dt.datetime.fromtimestamp(now, cfg.tz).date()
    out = []
    bad = stale(cfg, health, now)
    if bad:
        lines = [f"{s.name}: {_public(s.error) or 'no successful download yet'}" for s in bad]
        lines.append("Check ADE in a browser, or run `tamis status` on the server.")
        out.append(_all_day(cfg, "stale@edt-tamis", today, "⚠ " + summary_line(cfg, bad),
                            "\n".join(lines)))
    days = int(cfg.number("watchdog_days", 0, minimum=0, maximum=3650))
    if days > 0:
        out.append(_all_day(
            cfg, "watchdog@edt-tamis", today + dt.timedelta(days=days),
            "⚠ feed check - if you read this on its own day, the calendar stopped updating",
            f"This event is always placed {days} days ahead by the server. If it has reached "
            "today, this device has not received an update for that long: check that the "
            "server is running and that Tailscale is connected on this device."))
    return out


class Alerter:
    """Runs settings.alert_command when sources go stale and when they recover.

    The state lives in a small file, so one-shot runs (cron, `publish`) do not
    repeat the alert every time either.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state_file = cfg.data_dir / "alert-state.json"

    def _was_stale(self) -> bool:
        try:
            return bool(json.loads(self.state_file.read_text())["stale"])
        except (OSError, ValueError, KeyError):
            return False

    def update(self, health: list[SourceStatus]) -> None:
        bad = stale(self.cfg, health)
        was = self._was_stale()
        if bool(bad) == was:
            return
        private_dir(self.state_file.parent)
        atomic_write(self.state_file, json.dumps({"stale": bool(bad), "since": time.time()}).encode(),
                     mode=0o600)
        msg = ("edt-tamis: " + summary_line(self.cfg, bad)) if bad \
            else "edt-tamis: timetable updates are working again"
        (log.error if bad else log.info)(msg)
        self.run(msg)

    def run(self, message: str) -> None:
        cmd = self.cfg.settings.get("alert_command")
        if not cmd:
            return
        if isinstance(cmd, str):
            cmd = [cmd]
        argv = [str(a).replace("{message}", message) for a in cmd]
        try:
            # argv comes from the owner's own config; list form, never a shell
            subprocess.run(  # noqa: S603
                argv, env=child_env(TAMIS_ALERT=message), timeout=60,
                capture_output=True, check=True)
        # The exceptions' own text contains the whole argv, which may hold a secret (the
        # ntfy topic in the example config), so log only what is safe.
        except subprocess.CalledProcessError as exc:
            log.error("alert_command failed: exit status %s", exc.returncode)
        except subprocess.TimeoutExpired:
            log.error("alert_command timed out after 60 s")
        except OSError as exc:
            log.error("alert_command could not be run: %s", exc.strerror or type(exc).__name__)
        except subprocess.SubprocessError as exc:
            log.error("alert_command failed: %s", type(exc).__name__)
