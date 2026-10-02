"""Round-2 leftovers: writes that must not follow links, config typos, per-client limits,
log hygiene, git timeouts and odd file names."""

from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import subprocess
import time
import unittest
from unittest import mock

from tamis import EdtError, server
from tamis.ade import SourceStatus
from tamis.config import Config
from tamis.fsutil import atomic_write
from tamis.health import Alerter
from tamis.publish import PublishError, _git, check_file_name
from tamis.rules import compile_courses, norm_pattern, rx

from helpers import TempDir, make_config
from test_server_hardening import ServerCase

POSIX = os.name != "nt"


class FileNameTests(unittest.TestCase):
    def test_windows_device_names_and_trailing_dot_or_space_are_refused(self):
        for name in ("CON", "nul.ics", "Aux", "com1.ics", "LPT9", "calendar.ics ",
                     "calendar.ics.", "a.."):
            with self.subTest(name=name), self.assertRaises(PublishError):
                check_file_name(name)

    def test_similar_ordinary_names_are_fine(self):
        for name in ("console.ics", "nullable.ics", "com.ics", "com10.ics"):
            with self.subTest(name=name):
                self.assertEqual(check_file_name(name), name)


class GitTimeoutTests(unittest.TestCase):
    def test_hung_git_becomes_a_clear_error(self):
        boom = subprocess.TimeoutExpired(["git"], 1)
        with TempDir() as tmp, mock.patch("tamis.publish.subprocess.run", side_effect=boom):
            with self.assertRaises(PublishError) as cm:
                _git(tmp, "push")
        self.assertIn("did not finish", str(cm.exception))


@unittest.skipUnless(POSIX, "symlinks and modes: POSIX only")
class StateFileTests(unittest.TestCase):
    def test_alert_state_does_not_follow_a_planted_link(self):
        with TempDir() as tmp:
            cfg = Config(make_config(tmp))
            victim = tmp / "victim.txt"
            victim.write_text("precious")
            cfg.data_dir.mkdir(parents=True)
            (cfg.data_dir / "alert-state.json").symlink_to(victim)
            Alerter(cfg).update([SourceStatus("A", last_success=None, error="down")])
            self.assertEqual(victim.read_text(), "precious")
            state = cfg.data_dir / "alert-state.json"
            self.assertFalse(state.is_symlink())
            self.assertTrue(json.loads(state.read_text())["stale"])
            self.assertEqual(state.stat().st_mode & 0o077, 0)       # private

    def test_new_data_dir_is_private(self):
        with TempDir() as tmp:
            cfg = Config(make_config(tmp))
            Alerter(cfg).update([SourceStatus("A", last_success=None, error="down")])
            self.assertEqual(cfg.data_dir.stat().st_mode & 0o077, 0)

    def test_atomic_write_honours_a_private_mode(self):
        with TempDir() as tmp:
            atomic_write(tmp / "secret.txt", b"x", mode=0o600)
            self.assertEqual((tmp / "secret.txt").stat().st_mode & 0o077, 0)


class ConfigValueTests(unittest.TestCase):
    def cfg(self, **settings):
        with TempDir() as tmp:
            c = Config(make_config(tmp))
        c.settings.update(settings)
        return c

    def test_typos_give_clear_errors_not_tracebacks(self):
        c = self.cfg()
        for key, bad in (("refresh_minutes", "fast"), ("refresh_minutes", 1),
                         ("stale_after_hours", "twelve"), ("watchdog_days", 1e12),
                         ("watchdog_days", True)):
            with self.subTest(key=key, bad=bad):
                c.settings[key] = bad
                with self.assertRaises(EdtError):
                    c.number(key, 1, minimum=10 if key == "refresh_minutes" else 0,
                             maximum=3650 if key == "watchdog_days" else None)

    def test_valid_numbers_pass_through(self):
        c = self.cfg(refresh_minutes=60)
        self.assertEqual(c.number("refresh_minutes", 120, minimum=10), 60)
        self.assertEqual(c.number("missing", 7), 7)

    def test_bad_course_priority_is_an_edt_error(self):
        c = self.cfg()
        c.data["courses"] = [{"label": "X", "match": "x", "priority": "high"}]
        with self.assertRaises(EdtError):
            compile_courses(c)

    def test_serve_refuses_a_refresh_interval_that_would_hammer_ade(self):
        c = self.cfg(refresh_minutes=1)
        with self.assertRaises(EdtError):
            server.make_server(c, port=0, refresh=False)


class RegexTests(unittest.TestCase):
    def test_named_groups_survive_normalisation(self):
        self.assertEqual(norm_pattern("(?P<N>a)(?P=N)"), "(?P<n>a)(?P=n)")
        self.assertTrue(rx("(?P<x>é)(?P=x)").search("ee"))


class PerClientLimitTests(unittest.TestCase):
    def srv(self, per_address):
        s = server._Server(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler,
                           max_connections=10, max_per_address=per_address)
        self.addCleanup(s.server_close)
        return s

    def test_one_client_cannot_take_every_slot(self):
        s = self.srv(2)
        s.shutdown_request = mock.Mock()
        with mock.patch.object(socketserver.ThreadingMixIn, "process_request") as go:
            for _ in range(3):
                s.process_request(mock.Mock(), ("203.0.113.5", 1))
            self.assertEqual(go.call_count, 2)
            s.shutdown_request.assert_called_once()
            s.process_request(mock.Mock(), ("203.0.113.9", 1))     # someone else is fine
            self.assertEqual(go.call_count, 3)

    def test_slots_come_back_and_loopback_is_exempt(self):
        s = self.srv(1)
        s.shutdown_request = mock.Mock()
        with mock.patch.object(socketserver.ThreadingMixIn, "process_request") as go, \
                mock.patch.object(socketserver.ThreadingMixIn, "process_request_thread"):
            for _ in range(4):                                     # a local proxy: no per-client cap
                s.process_request(mock.Mock(), ("127.0.0.1", 1))
            self.assertEqual(go.call_count, 4)
            s.process_request(mock.Mock(), ("203.0.113.5", 1))
            s.process_request_thread(mock.Mock(), ("203.0.113.5", 1))
            s.process_request(mock.Mock(), ("203.0.113.5", 2))
            self.assertEqual(go.call_count, 6)

    def test_ipv6_clients_are_grouped_by_their_64(self):
        self.assertEqual(server.address_key("2001:db8:1:2:aaaa::1"),
                         server.address_key("2001:db8:1:2:bbbb::9"))
        self.assertNotEqual(server.address_key("2001:db8:1:2::1"),
                            server.address_key("2001:db8:1:3::1"))
        self.assertEqual(server.address_key("::1"), "loopback")


class ServerOddityTests(ServerCase):
    def raw(self, data: bytes) -> bytes:
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(s.close)
        s.sendall(data)
        out = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                return out
            out += chunk

    def test_duplicate_host_headers_are_refused(self):
        self.start()
        both = self.raw(b"GET /calendar.ics HTTP/1.1\r\nHost: localhost\r\nHost: evil.example\r\n"
                        b"Connection: close\r\n\r\n")
        self.assertIn(b" 421 ", both.split(b"\r\n", 1)[0])

    def test_trailing_dot_in_host_is_accepted(self):
        self.start()
        self.assertEqual(self.get(headers={"Host": "LOCALHOST."})[0], 200)

    def test_request_line_is_cleaned_before_it_reaches_the_log(self):
        with self.assertLogs("tamis", "INFO") as cm:
            self.start(verbose=True)
            self.raw(b"GET /x\x1b]0;PWNED\x07\x1b[2J HTTP/1.0\r\n\r\n")
            deadline = time.monotonic() + 3
            while not any("GET" in m for m in cm.output) and time.monotonic() < deadline:
                time.sleep(0.05)
        joined = "\n".join(cm.output)
        self.assertIn("GET", joined)
        self.assertNotIn("\x1b", joined)
        self.assertNotIn("\x07", joined)

    def test_bad_reload_values_keep_the_old_settings_and_do_not_raise(self):
        srv = self.start()
        for key, bad in (("url_path", 1234), ("allowed_hosts", 7)):
            with self.subTest(key=key):
                cfg = Config(self.cfg_path)
                cfg.settings[key] = bad
                with mock.patch.object(server, "load_complete", return_value=cfg), \
                        self.assertLogs("tamis", "ERROR"):
                    srv.reload_security()
                self.assertEqual(self.get()[0], 200)               # still the old path
        self.cfg_path.unlink()                                      # editor mid-save
        with self.assertLogs("tamis", "ERROR"):
            srv.reload_security()
        self.assertEqual(self.get()[0], 200)


if __name__ == "__main__":
    unittest.main()
