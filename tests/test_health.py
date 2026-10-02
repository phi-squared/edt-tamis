"""Failures must be visible: stale data, login pages, empty answers, dead feeds."""

import datetime as dt
import http.server
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

from tamis.ade import SourceStatus
from tamis.cli import main
from tamis.config import Config
from tamis.health import warning_events
from tamis.pipeline import produce

from helpers import FIXTURES, TempDir, make_config


class Flaky:
    """A fake ADE whose answer can be switched: ok / login page / empty / down."""

    def __init__(self):
        self.mode = "ok"
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if outer.mode == "down":
                    self.send_error(503)
                    return
                body = {
                    "ok": (FIXTURES / "a.ics").read_bytes(),
                    "login": b"<!DOCTYPE html><html><title>CAS - Connexion</title>login</html>",
                    "empty": b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n",
                }[outer.mode]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/ade.jsp"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def cfg_for(tmp: Path, url: str, extra: str = "") -> Config:
    base = f"""
    [settings]
    ade_url = "{url}"
    project_id = 7
    data_dir = "data"
    cache_minutes = 0
    stale_after_hours = 12
    {extra}
    [period]
    start = "2026-09-01"
    end = "2027-02-28"
    [[sources]]
    name = "prog"
    resources = [1]
    [[courses]]
    label = "Stoch"
    match = "calcul stochastique"
    """
    return Config(make_config(tmp, base=base))


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.ade = Flaky()

    def tearDown(self):
        self.ade.close()

    def test_bad_answers_keep_last_good_copy_and_say_why(self):
        with TempDir() as tmp:
            cfg = cfg_for(tmp, self.ade.url)
            good = produce(cfg)
            self.assertTrue(good.kept)
            for mode, words in (("login", "login page"), ("empty", "empty calendar"), ("down", "503")):
                self.ade.mode = mode
                b = produce(cfg)
                self.assertEqual(len(b.kept), len(good.kept), mode)     # nothing vanished
                self.assertIn(words, b.health[0].error, mode)
                self.assertFalse(b.warnings, mode)                     # not stale yet

    def test_stale_warning_event_and_alert(self):
        with TempDir() as tmp:
            marker = tmp / "alerts.txt"
            cmd = [sys.executable, "-c",
                   f"import os; open({str(marker)!r}, 'a').write(os.environ['TAMIS_ALERT'] + '\\n')"]
            cfg = cfg_for(tmp, self.ade.url, f"alert_command = {json.dumps(cmd)}")
            produce(cfg)
            # pretend the last good download was two days ago, then ADE breaks
            for f in cfg.data_dir.glob("*.ics"):
                old = time.time() - 48 * 3600
                os.utime(f, (old, old))
            self.ade.mode = "login"
            b = produce(cfg)
            self.assertEqual(len(b.warnings), 1)
            self.assertIn("NOT updated since", b.warnings[0].summary)
            self.assertIn("DTSTART;VALUE=DATE:", b.text)
            self.assertIn("login page", b.warnings[0].description)
            produce(cfg)                          # still broken: no second alert
            self.ade.mode = "ok"
            b = produce(cfg)
            self.assertFalse(b.warnings)          # warning disappears by itself
            lines = marker.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("NOT updated", lines[0])
            self.assertIn("working again", lines[1])

    def test_status_exit_code(self):
        with TempDir() as tmp:
            cfg = cfg_for(tmp, self.ade.url)
            self.assertEqual(main(["-c", str(cfg.path), "status"]), 0)
            self.ade.mode = "down"
            self.assertEqual(main(["-c", str(cfg.path), "status"]), 1)


class WatchdogTests(unittest.TestCase):
    def test_watchdog_is_n_days_ahead(self):
        with TempDir() as tmp:
            cfg = Config(make_config(tmp))
            cfg.settings["watchdog_days"] = 3
            now = time.time()
            ev = warning_events(cfg, [SourceStatus("x", last_success=now)], now)
            self.assertEqual(len(ev), 1)
            today = dt.datetime.fromtimestamp(now, cfg.tz).date()
            self.assertEqual(ev[0].start.date(), today + dt.timedelta(days=3))
            self.assertTrue(ev[0].all_day)


if __name__ == "__main__":
    unittest.main()
