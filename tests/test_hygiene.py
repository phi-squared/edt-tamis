"""Quoting in generated service files, harmless terminal output, no secrets in logs or
child environments."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock
from zoneinfo import ZoneInfo

from tamis import EdtError
from tamis.ade import FetchError, SourceStatus, fetch
from tamis.config import Config
from tamis.health import Alerter
from tamis.ics import parse_ics
from tamis.publish import PublishError, _git
from tamis.report import health_table, report, titles
from tamis.rules import build
from tamis.safety import child_env, redact_urls, safe_text
from tamis.service import _ps_quote, _systemd, _systemd_arg, _windows

from helpers import TempDir, make_config

ESC = "\x1b"


class SystemdQuotingTests(unittest.TestCase):
    def test_specials_are_escaped(self):
        self.assertEqual(_systemd_arg("plain"), '"plain"')
        self.assertEqual(_systemd_arg("a b"), '"a b"')
        self.assertEqual(_systemd_arg("50%"), '"50%%"')
        self.assertEqual(_systemd_arg("$HOME"), '"$$HOME"')
        self.assertEqual(_systemd_arg('say "hi"'), '"say \\"hi\\""')
        self.assertEqual(_systemd_arg("back\\slash"), '"back\\\\slash"')

    def test_newline_cannot_inject_directives(self):
        with self.assertRaises(EdtError):
            _systemd_arg("x\nExecStartPre=/bin/evil")
        with self.assertRaises(EdtError):
            _systemd("serve", ["/usr/bin/python3"], PurePosixPath("/opt/x\nUser=root"))

    def test_unit_has_one_exec_line_and_escaped_workdir(self):
        text = _systemd("serve", ["/usr/bin/python3", "-m", "tamis", "--config", "/h/50%/c.toml"],
                        PurePosixPath("/srv/100%"))
        self.assertEqual(sum(line.startswith("ExecStart=") for line in text.splitlines()), 1)
        self.assertIn('"/h/50%%/c.toml"', text)
        self.assertIn("WorkingDirectory=/srv/100%%\n", text)


class PowerShellQuotingTests(unittest.TestCase):
    def test_every_single_quote_character_is_doubled(self):
        for q in "'\u2018\u2019\u201a\u201b":
            with self.subTest(q=hex(ord(q))):
                self.assertEqual(_ps_quote(f"a{q}b"), f"'a{q}{q}b'")

    def test_control_characters_are_refused(self):
        with self.assertRaises(EdtError):
            _ps_quote("a\nb")

    def test_script_quotes_arguments_like_windows_does(self):
        text = _windows("edt-tamis serve", r"C:\Python\pythonw.exe",
                        ["-m", "tamis", "--config", r"C:\Users\O’Brien\my dir\c.toml", 'a"b'],
                        Path(r"C:\x"))
        self.assertIn('"C:\\Users\\O’’Brien\\my dir\\c.toml"', text)   # quoted for spaces, ’ doubled
        self.assertIn('a\\"b', text)
        self.assertNotIn('Write-Host "', text)


class TerminalTests(unittest.TestCase):
    def test_safe_text(self):
        self.assertEqual(safe_text("Contrôle optimal – 日本"), "Contrôle optimal – 日本")
        self.assertEqual(safe_text(f"a{ESC}[2Jb"), "a?[2Jb")
        self.assertEqual(safe_text(f"x{ESC}]0;pwned\x07y"), "x?]0;pwned?y")
        self.assertEqual(safe_text("a\nb\tc\r"), "a b c ")
        self.assertEqual(safe_text("\u202eevil\u200b"), "?evil?")       # bidi override, zero width
        self.assertEqual(safe_text("\x9b31m"), "?31m")                  # C1 control

    def test_reports_never_contain_escape_sequences(self):
        with TempDir() as tmp:
            cfg = Config(make_config(tmp))
            text = (
                "BEGIN:VCALENDAR\n"
                "BEGIN:VEVENT\nUID:1\nDTSTART:20261002T080000Z\nDTEND:20261002T090000Z\n"
                f"SUMMARY:Examen Calcul stochastique{ESC}]0;pwned\x07\n"
                f"LOCATION:Room{ESC}[2J\nEND:VEVENT\nEND:VCALENDAR\n")
            events = parse_ics(text, "src\x1b[1m", ZoneInfo("UTC"))
            kept, conflicts = build(cfg, events)
            self.assertTrue(kept)
            health = [SourceStatus("s", last_success=1.0, error=f"HTTP 500 {ESC}[31mred", events=1)]
            for out in (report(cfg, kept, conflicts), titles(cfg, events), health_table(cfg, health)):
                self.assertNotIn(ESC, out)
                self.assertNotIn("\x07", out)

    def test_server_text_in_fetch_errors_is_cleaned(self):
        with TempDir() as tmp:
            with mock.patch("tamis.ade._opener.open", side_effect=OSError(f"boom{ESC}[2J")):
                with self.assertRaises(FetchError) as cm:
                    fetch("http://127.0.0.1:1/x", tmp / "d", 0)
        self.assertNotIn(ESC, str(cm.exception))


class SecretsTests(unittest.TestCase):
    def test_redact_urls(self):
        self.assertEqual(redact_urls("fatal: 'https://me:tok3n@github.com/a/b.git/' failed"),
                         "fatal: 'https://***@github.com/a/b.git/' failed")
        self.assertEqual(redact_urls("ssh://git@host/x"), "ssh://***@host/x")
        self.assertEqual(redact_urls("no url here, a@b.example"), "no url here, a@b.example")

    def test_fetch_error_hides_url_credentials(self):
        with TempDir() as tmp, self.assertRaises(FetchError) as cm:
            fetch("http://user:hunter2@127.0.0.1:1/x", tmp / "d", 0)
        self.assertNotIn("hunter2", str(cm.exception))

    def test_child_env_drops_the_server_password(self):
        with mock.patch.dict(os.environ, {"TAMIS_AUTH": "me:pw", "KEEP": "1"}):
            env = child_env(TAMIS_ALERT="hello")
        self.assertNotIn("TAMIS_AUTH", env)
        self.assertEqual((env["KEEP"], env["TAMIS_ALERT"]), ("1", "hello"))

    def alerter(self, tmp, argv):
        cfg = Config(make_config(tmp))
        cfg.settings["alert_command"] = argv
        return Alerter(cfg)

    def test_alert_command_does_not_receive_tamis_auth(self):
        with TempDir() as tmp:
            out = tmp / "env.txt"
            code = f"import os; open({str(out)!r}, 'w').write(os.environ.get('TAMIS_AUTH', 'absent'))"
            with mock.patch.dict(os.environ, {"TAMIS_AUTH": "me:pw"}):
                self.alerter(tmp, [sys.executable, "-c", code]).run("msg")
            self.assertEqual(out.read_text(), "absent")

    def test_failing_alert_command_does_not_log_its_arguments(self):
        with TempDir() as tmp:
            url = "https://ntfy.sh/SECRETTOPIC123"
            for argv in ([sys.executable, "-c", "import sys; sys.exit(3)", url],
                         [str(tmp / "no-such-program"), url]):
                with self.subTest(argv=argv[0] == sys.executable):
                    with self.assertLogs("tamis", "ERROR") as cm:
                        self.alerter(tmp, argv).run("msg")
                    self.assertNotIn("SECRETTOPIC123", "\n".join(cm.output))

    def test_alert_timeout_does_not_log_its_arguments(self):
        with TempDir() as tmp:
            exc = subprocess.TimeoutExpired(["curl", "https://ntfy.sh/SECRETTOPIC123"], 60)
            with mock.patch("tamis.health.subprocess.run", side_effect=exc):
                with self.assertLogs("tamis", "ERROR") as cm:
                    self.alerter(tmp, ["curl", "https://ntfy.sh/SECRETTOPIC123"]).run("msg")
            self.assertNotIn("SECRETTOPIC123", "\n".join(cm.output))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_gets_no_password_and_errors_hide_credentials(self):
        with TempDir() as tmp:
            subprocess.run(["git", "init", "-q", str(tmp)], check=True)
            with mock.patch.dict(os.environ, {"TAMIS_AUTH": "me:pw"}):
                out = _git(tmp, "-c", "alias.penv=!echo ${TAMIS_AUTH:-absent}", "penv")
            self.assertEqual(out, "absent")
            with self.assertRaises(PublishError) as cm:
                _git(tmp, "fetch", "http://user:hunter2@127.0.0.1:1/x.git")
            self.assertNotIn("hunter2", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
