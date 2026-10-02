"""Round-3 review: deadline over headers, encoded hosts, overflowing dates, half-written
config, header caps, IPv6, git environment and the tests the reviewer found toothless."""

from __future__ import annotations

import http.client
import io
import os
import shutil
import socket
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from tamis import EdtError, ade, rules, server
from tamis.ade import FetchError, fetch
from tamis.config import Config, load_complete
from tamis.health import SourceStatus, _public, warning_events
from tamis.ics import parse_ics
from tamis.pipeline import produce
from tamis.publish import publish_once

from helpers import TempDir, make_config
from test_fetch_limits import CAL, GOOD, ServerCase as FetchCase, event
from test_server_hardening import ServerCase

UTC = ZoneInfo("UTC")


class DeadlineCoversHeadersTests(unittest.TestCase):
    def test_endless_100_continue_cannot_stall_the_download(self):
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        self.addCleanup(lsock.close)

        def serve():
            conn, _ = lsock.accept()
            try:
                conn.recv(4096)
                for _ in range(100):
                    conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                    time.sleep(0.2)
            except OSError:
                pass
            finally:
                conn.close()

        threading.Thread(target=serve, daemon=True).start()
        url = f"http://127.0.0.1:{lsock.getsockname()[1]}/ade.jsp?resources=1"
        with TempDir() as tmp, mock.patch.object(ade, "DEADLINE_S", 1):
            t0 = time.monotonic()
            with self.assertRaises(FetchError) as cm:
                fetch(url, tmp / "d", 0)
            self.assertLess(time.monotonic() - t0, 8)
        self.assertIn("took longer", str(cm.exception))


class EncodedHostTests(unittest.TestCase):
    def test_percent_encoded_local_hosts_are_local(self):
        for host in ("%31%32%37.0.0.1", "%6c%6fcalhost", "%31%32%37.1", "%5b::1%5d"):
            with self.subTest(host=host):
                self.assertTrue(ade._local_host(host))

    def test_redirect_to_an_encoded_loopback_is_refused(self):
        h = ade._SafeRedirect()
        req = urllib.request.Request("https://adecons.example.org/a")
        for new in ("http://%31%32%37.0.0.1:8080/x", "https://%6c%6fcalhost/x"):
            with self.subTest(new=new), self.assertRaises(urllib.error.URLError):
                h.redirect_request(req, None, 302, "Found", {}, new)


class OverflowingDateTests(unittest.TestCase):
    BAD = CAL.format(event("far", end="99991231T235959Z") + event("ok"))

    def test_unusable_dates_are_skipped_like_malformed_ones(self):
        problems: list[str] = []
        evs = parse_ics(self.BAD, "s", ZoneInfo("Europe/Paris"), problems)
        self.assertEqual([e.uid for e in evs], ["ok"])
        self.assertEqual(len(problems), 1)
        for e in evs:                         # converting must not raise later
            self.assertIsNotNone((e.local_start, e.local_end))

    def test_an_answer_with_only_such_events_is_refused(self):
        only = CAL.format(event("far", end="99991231T235959Z"))
        with self.assertRaises(ValueError):
            ade._check_body(only, None)


class HalfWrittenConfigTests(ServerCase):
    auth = True

    def test_empty_or_truncated_config_does_not_switch_the_password_off(self):
        srv = self.start()
        for content in ("", "[settings]\n"):
            with self.subTest(content=content):
                self.cfg_path.write_text(content)
                with self.assertLogs("tamis", "ERROR"):
                    srv.reload_security()
                self.assertEqual(self.get()[0], 401)
                self.assertEqual(self.get(user="me:pw1")[0], 200)

    def test_load_complete_accepts_a_real_config(self):
        self.assertTrue(load_complete(self.cfg_path).sources)

    def test_failed_reload_leaves_everything_unchanged_even_if_the_password_changed(self):
        srv = self.start()
        self.write_auth("me:pw2")
        cfg = Config(self.cfg_path)
        cfg.settings["allowed_hosts"] = 7
        with mock.patch.object(server, "load_complete", return_value=cfg), \
                self.assertLogs("tamis", "ERROR"):
            srv.reload_security()
        self.assertEqual(self.get(user="me:pw1")[0], 200)       # old password still valid
        self.assertEqual(self.get(user="me:pw2")[0], 401)


class FeedContentTests(unittest.TestCase):
    def test_warning_event_has_no_tokens_or_local_folders(self):
        with TempDir() as tmp:
            cfg = Config(make_config(tmp))
            bad = [SourceStatus("A", None, "cannot fetch https://ade.example/x.jsp?data=SECRET123&a=1: HTTP 503"),
                   SourceStatus("B", None, "cannot read /Users/bob/uni/prog.ics: FileNotFoundError")]
            text = "\n".join(e.description for e in warning_events(cfg, bad))
        self.assertNotIn("SECRET123", text)
        self.assertNotIn("bob", text)
        self.assertIn("ade.example", text)
        self.assertIn("prog.ics", text)

    def test_public_helper(self):
        self.assertNotIn("TOKEN", _public("see http://h.example/p?data=TOKEN"))


class HeaderCapTests(ServerCase):
    def test_capped_reader_stops_at_its_limit(self):
        c = server._Capped(io.BytesIO((b"x" * 9999 + b"\n") * 5), 16384)
        total = 0
        while True:
            chunk = c.readline(65537)
            if not chunk:
                break
            total += len(chunk)
        self.assertEqual(total, 16384)

    def test_server_survives_a_flood_of_headers(self):
        self.start()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(s.close)
        try:
            s.sendall(b"GET /calendar.ics HTTP/1.0\r\n" + (b"X: " + b"a" * 60000 + b"\r\n") * 5
                      + b"\r\n")
            s.recv(100)
        except OSError:
            pass
        self.assertEqual(self.get()[0], 200)


class IPv6BindTests(ServerCase):
    def test_bind_to_ipv6_loopback_works(self):
        if not socket.has_ipv6:
            self.skipTest("no IPv6")
        probe = socket.socket(socket.AF_INET6)
        try:
            probe.bind(("::1", 0))
        except OSError:
            self.skipTest("no IPv6 loopback")
        finally:
            probe.close()
        srv, _, stop = server.make_server(self.cfg, port=0, bind="::1", refresh=False)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), stop.set()))
        conn = http.client.HTTPConnection("::1", srv.server_address[1], timeout=5)
        conn.request("GET", "/calendar.ics")
        self.assertEqual(conn.getresponse().status, 200)


class ConfigTypoTests(unittest.TestCase):
    def cfg(self, **settings):
        with TempDir() as tmp:
            c = Config(make_config(tmp))
        c.settings.update(settings)
        return c

    def test_remaining_settings_give_clear_errors(self):
        for key, value in (("cache_minutes", "x"), ("refresh_hint_hours", "2h"), ("port", "abc")):
            with self.subTest(key=key):
                c = self.cfg(**{key: value})
                with self.assertRaises(EdtError):
                    if key == "port":
                        server.make_server(c, refresh=False)
                    elif key == "cache_minutes":
                        ade.load_sources(c)
                    else:
                        produce(c, alert=False)

    def test_wrong_types_in_the_config_file_give_clear_errors(self):
        p = Path("x.toml")
        for data in ({"settings": {"timezone": 5}}, {"settings": [1]}, {"publish": "git"}):
            with self.subTest(data=data), self.assertRaises(EdtError):
                Config(p, data)
        with self.assertRaises(EdtError):
            Config(p, {"period": {"end": "+9999999999w"}}).period()

    def test_course_lists_and_flags_are_checked(self):
        c = self.cfg()
        for course in ({"skip_match": "td"}, {"skip_slots": 5}, {"attend": "false"},
                       {"on_conflict": "maybe"}):
            with self.subTest(course=course):
                c.data["courses"] = [{"label": "X", "match": "x", **course}]
                with self.assertRaises(EdtError):
                    rules.compile_courses(c)


class FetchEdgeTests(FetchCase):
    def test_truncated_body_that_still_has_the_end_marker_is_caught_by_content_length(self):
        # the first check (END:VCALENDAR) passes here, so only Content-Length can notice
        class Resp:
            headers = {"Content-Length": "100000"}
            parts = [GOOD.encode(), b""]

            def read1(self, n):
                return self.parts.pop(0)

        with self.assertRaises(http.client.IncompleteRead):
            ade._read_limited(Resp())

    def test_chunked_response_ignores_content_length(self):
        class Resp:
            headers = {"Content-Length": "100000", "Transfer-Encoding": "chunked"}
            parts = [GOOD.encode(), b""]

            def read1(self, n):
                return self.parts.pop(0)

        self.assertEqual(ade._read_limited(Resp()), GOOD)

    def test_second_cut_off_calendar_is_refused(self):
        with self.assertRaises(ValueError):
            ade._check_body(GOOD + "BEGIN:VCALENDAR\nBEGIN:VEVENT\n", None)

    def test_six_different_urls_in_a_chain_are_too_many(self):
        h = ade._SafeRedirect()
        self.assertEqual(h.max_redirections, ade.MAX_REDIRECTS)
        self.assertLessEqual(ade.MAX_REDIRECTS, 5)

    def test_deadline_is_passed_on_to_the_next_hop(self):
        h = ade._SafeRedirect()
        req = urllib.request.Request("https://a.example/a")
        req.tamis_deadline = time.monotonic() + 100
        nxt = h.redirect_request(req, None, 302, "Found", {}, "https://b.example/b")
        self.assertEqual(nxt.tamis_deadline, req.tamis_deadline)

    def test_empty_answer_is_believed_once_the_old_copy_is_stale(self):
        fetch(self.server.url, self.store, 0)
        for f in self.store.glob("*.ics"):
            old = time.time() - 20 * 86400
            os.utime(f, (old, old))
        self.server.body = CAL.format("")
        text, _, err = fetch(self.server.url, self.store, 0)
        self.assertEqual(err, "")
        self.assertNotIn("BEGIN:VEVENT", text)

    def test_empty_answer_is_still_refused_while_the_copy_is_fresh(self):
        fetch(self.server.url, self.store, 0)
        self.server.body = CAL.format("")
        _, _, err = fetch(self.server.url, self.store, 0)
        self.assertIn("empty calendar", err)

    def test_undecodable_cached_copy_does_not_abort(self):
        fetch(self.server.url, self.store, 0)
        for f in self.store.glob("*.ics"):
            f.write_bytes(b"\xff\xfe\x80")
        self.server.mode = "down"
        with self.assertRaises(FetchError):                       # no usable copy: clean failure
            fetch(self.server.url, self.store, 0)


class ExamBudgetTests(unittest.TestCase):
    def test_exam_pairs_survive_when_ordinary_pairs_use_up_their_budget(self):
        with TempDir() as tmp:
            cfg = Config(make_config(tmp))
            text = CAL.format(
                "".join(event(f"c{i}", summary="Calcul stochastique") for i in range(40))
                + "".join(event(f"x{i}", "20261003T080000Z", "20261003T090000Z",
                                "Examen Calcul stochastique") for i in range(3)))
            evs = parse_ics(text, "s", UTC)
            with mock.patch.object(rules, "MAX_STORED_CONFLICTS", 5):
                _, conflicts = rules.build(cfg, evs)
        self.assertEqual(sum(1 for a, b in conflicts if a.is_exam and b.is_exam), 3)
        self.assertEqual(sum(1 for a, b in conflicts if not (a.is_exam or b.is_exam)), 5)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitEnvironmentTests(unittest.TestCase):
    def git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout.strip()

    def setUp(self):
        self._td = TempDir()
        self.tmp = self._td.__enter__()
        self.addCleanup(self._td.__exit__, None, None, None)
        self.remote, self.clone = self.tmp / "remote.git", self.tmp / "clone"
        self.git("init", "-q", "--bare", str(self.remote), cwd=self.tmp)
        self.git("clone", "-q", str(self.remote), str(self.clone), cwd=self.tmp)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                 "-m", "init", cwd=self.clone)
        self.git("push", "-q", "-u", "origin", "HEAD", cwd=self.clone)

    def cfg(self, name):
        extra = f'[publish]\nmethod = "git"\ntarget = "{self.clone.as_posix()}"\nfile_name = "{name}"\n'
        return Config(make_config(self.tmp, extra))

    def test_no_git_call_inherits_the_server_password(self):
        real = subprocess.run
        seen = []

        def spy(cmd, *a, **kw):
            seen.append((cmd[:4], kw.get("env")))
            return real(cmd, *a, **kw)

        with mock.patch.dict(os.environ, {"TAMIS_AUTH": "me:secret"}), \
                mock.patch("tamis.publish.subprocess.run", spy):
            publish_once(self.cfg("feed.ics"))
        self.assertGreaterEqual(len(seen), 5)
        for cmd, env in seen:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(env)
                self.assertNotIn("TAMIS_AUTH", env)

    def test_non_ascii_file_name_publishes(self):
        self.assertEqual(publish_once(self.cfg("été.ics")), "pushed")
        self.assertEqual(self.git("status", "--porcelain", cwd=self.clone), "")


if __name__ == "__main__":
    unittest.main()
