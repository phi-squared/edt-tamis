"""The HTTP server must stay up and quiet under slow, abusive or odd clients."""

from __future__ import annotations

import http.client
import os
import socket
import threading
import time
import unittest
from unittest import mock

from tamis import server
from tamis.config import Config
from tamis.pipeline import produce, write_if_changed
from tamis.server import make_server

from helpers import TempDir, make_config

SECRET = "s3cr3t-path-9x7q.ics"


class ServerCase(unittest.TestCase):
    extra = ""
    auth = False

    def setUp(self):
        td = TempDir()
        self.tmp = td.__enter__()
        self.addCleanup(td.__exit__, None, None, None)
        if self.auth:
            self.write_auth("me:pw1")
        self.cfg_path = make_config(self.tmp, self.extra)
        if self.auth:
            text = self.cfg_path.read_text()
            self.cfg_path.write_text(
                text.replace("project_id    = 7", 'project_id    = 7\nauth_file = "auth.txt"', 1))
        self.cfg = Config(self.cfg_path)
        write_if_changed(self.cfg.output_file, produce(self.cfg).text)

    def write_auth(self, val: str) -> None:
        f = self.tmp / "auth.txt"
        f.write_text(val + "\n")
        if os.name != "nt":
            os.chmod(f, 0o600)

    def start(self, **kw):
        kw.setdefault("refresh", False)
        srv, feed, stop = make_server(self.cfg, port=0, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), stop.set()))
        self.port = srv.server_address[1]
        return srv

    def get(self, path="/calendar.ics", headers=None, user=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if user:
            import base64
            hdrs["Authorization"] = "Basic " + base64.b64encode(user.encode()).decode()
        conn.request("GET", path, headers=hdrs)
        r = conn.getresponse()
        r.read()
        out = (r.status, dict(r.getheaders()))
        conn.close()
        return out


class HostGuardTests(ServerCase):
    def test_unknown_host_is_refused_without_password(self):
        self.start()
        self.assertEqual(self.get()[0], 200)
        self.assertEqual(self.get(headers={"Host": "localhost:8765"})[0], 200)
        self.assertEqual(self.get(headers={"Host": "[::1]:8765"})[0], 200)
        self.assertEqual(self.get(headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(self.get(headers={"Host": "evil.example:8765"})[0], 421)

    def test_allowed_hosts_setting(self):
        self.cfg.settings["allowed_hosts"] = ["Mymac.tailnet.ts.net"]
        self.start()
        self.assertEqual(self.get(headers={"Host": "mymac.tailnet.ts.net"})[0], 200)
        self.assertEqual(self.get(headers={"Host": "other.example"})[0], 421)

    def test_star_switches_the_guard_off(self):
        self.cfg.settings["allowed_hosts"] = ["*"]
        self.start()
        self.assertEqual(self.get(headers={"Host": "anything.example"})[0], 200)


class AuthTests(ServerCase):
    auth = True

    def test_host_is_not_checked_when_a_password_is_set(self):
        self.start()
        self.assertEqual(self.get(headers={"Host": "proxy.example"}, user="me:pw1")[0], 200)

    def test_password_change_is_picked_up_without_restart(self):
        srv = self.start()
        self.assertEqual(self.get(user="me:pw1")[0], 200)
        self.write_auth("me:pw2")
        srv.reload_security()
        self.assertEqual(self.get(user="me:pw1")[0], 401)
        self.assertEqual(self.get(user="me:pw2")[0], 200)

    def test_broken_auth_file_keeps_the_previous_password(self):
        srv = self.start()
        (self.tmp / "auth.txt").unlink()
        with self.assertLogs("tamis", "ERROR"):
            srv.reload_security()
        self.assertEqual(self.get(user="me:pw1")[0], 200)
        self.assertEqual(self.get(user="me:nope")[0], 401)

    def test_repeated_wrong_passwords_are_slowed_down_not_blocked(self):
        self.start()
        with mock.patch.object(server.time, "sleep") as sleep:
            for _ in range(server.FAIL_FREE):
                self.assertEqual(self.get(user="me:bad")[0], 401)
            sleep.assert_not_called()
            self.assertEqual(self.get(user="me:bad")[0], 401)
            sleep.assert_called_once_with(server.FAIL_DELAY)
            self.assertEqual(self.get(user="me:pw1")[0], 200)        # the owner still gets in
            sleep.assert_called_once()

    def test_non_local_bind_with_password_warns_about_cleartext(self):
        with self.assertLogs("tamis", "WARNING") as cm:
            srv, _, stop = make_server(self.cfg, port=0, bind="0.0.0.0", refresh=False)  # noqa: S104 - tests the warning
        srv.server_close()
        stop.set()
        self.assertTrue(any("unencrypted" in m for m in cm.output))


class SecretPathTests(ServerCase):
    def test_secret_path_is_not_logged(self):
        with self.assertLogs("tamis", "INFO") as cm:
            self.start(url_path=SECRET, verbose=True)
            self.assertEqual(self.get("/" + SECRET)[0], 200)
            self.assertEqual(self.get("/wrong")[0], 404)
        text = "\n".join(cm.output)
        self.assertNotIn(SECRET, text)
        self.assertIn("/<path>", text)
        self.assertIn("/<secret path>", text)

    def test_new_secret_path_is_picked_up_on_reload(self):
        self.cfg_path.write_text(self.cfg_path.read_text().replace(
            "project_id    = 7", f'project_id    = 7\nurl_path = "{SECRET}"', 1))
        srv = self.start()
        self.assertEqual(self.get("/" + SECRET)[0], 404)
        srv.reload_security()
        self.assertEqual(self.get("/" + SECRET)[0], 200)
        self.assertEqual(self.get("/calendar.ics")[0], 404)


class ResponseTests(ServerCase):
    def test_no_server_header(self):
        self.start()
        for path in ("/calendar.ics", "/missing"):
            status, headers = self.get(path)
            self.assertNotIn("Server", headers, path)
            self.assertIn("Date", headers)

    def test_warning_when_requests_arrive_through_a_proxy_without_password(self):
        self.start()
        with self.assertLogs("tamis", "WARNING") as cm:
            self.get(headers={"X-Forwarded-For": "203.0.113.5"})
            self.get(headers={"X-Forwarded-For": "203.0.113.6"})
        self.assertEqual(sum("through a proxy" in m for m in cm.output), 1)


class SlowClientTests(ServerCase):
    def raw(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(s.close)
        return s

    def closed_by_server(self, s) -> bool:
        try:
            return s.recv(100) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            return True
        except socket.timeout:
            return False

    def test_silent_connection_is_dropped(self):
        with mock.patch.object(server, "CONN_TIMEOUT", 0.5):
            self.start()
            s = self.raw()
            self.assertTrue(self.closed_by_server(s))

    def test_trickling_connection_hits_the_hard_deadline(self):
        with mock.patch.object(server, "CONN_DEADLINE", 0.8):
            self.start()
            s = self.raw()
            t0 = time.monotonic()
            try:
                while time.monotonic() - t0 < 4:
                    s.sendall(b"X")
                    time.sleep(0.2)
            except OSError:
                pass
            self.assertLess(time.monotonic() - t0, 4)

    def test_connections_above_the_cap_are_dropped_and_slots_are_freed(self):
        with mock.patch.object(server, "MAX_CONNECTIONS", 2):
            self.start()
            a, b = self.raw(), self.raw()
            time.sleep(0.3)                   # let the server take both slots
            c = self.raw()
            self.assertTrue(self.closed_by_server(c))
            a.close()
            b.close()
            time.sleep(0.3)
            deadline = time.monotonic() + 5   # slots come back once the clients are gone
            while True:
                try:
                    self.assertEqual(self.get()[0], 200)
                    break
                except (AssertionError, OSError):
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
