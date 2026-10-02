"""Hostile or broken answers from the server must not hang, exhaust memory, poison the
cache or stop the feed from updating silently."""

from __future__ import annotations

import http.server
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from zoneinfo import ZoneInfo

from tamis import ade, rules
from tamis.ade import FetchError, fetch
from tamis.config import Config
from tamis.ics import parse_ics, unfold
from tamis.pipeline import produce

from helpers import TempDir, make_config

CAL = "BEGIN:VCALENDAR\nVERSION:2.0\n{}END:VCALENDAR\n"


def event(uid: str, start: str = "20261002T080000Z", end: str = "20261002T090000Z",
          summary: str = "Calcul stochastique") -> str:
    return (f"BEGIN:VEVENT\nUID:{uid}\nDTSTART:{start}\nDTEND:{end}\n"
            f"SUMMARY:{summary}\nEND:VEVENT\n")


GOOD = CAL.format(event("1"))


class Server:
    def __init__(self):
        self.mode, self.body, self.hits = "ok", GOOD, []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                mode = outer.mode
                if mode == "down":
                    self.send_error(503)
                elif mode in ("redirect-ftp", "redirect-loop"):
                    self.send_response(302)
                    self.send_header("Location", "ftp://example.org/x.ics"
                                     if mode == "redirect-ftp" else "/loop")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif mode == "truncated":   # promises more than it sends, then hangs up
                    data = outer.body.encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data) + 500))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
                    self.close_connection = True
                elif mode == "pingpong":
                    self.send_response(302)
                    self.send_header("Location", "/b" if self.path.startswith("/ade") else
                                     "/ade.jsp?resources=1")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif mode == "drip":      # one byte every 0.2 s, for much longer than the test
                    self.send_response(200)
                    self.send_header("Content-Length", "1000000")
                    self.end_headers()
                    try:
                        for _ in range(100):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(0.2)
                    except OSError:
                        pass
                else:
                    data = outer.body.encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads, self.srv.block_on_close = True, False
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/ade.jsp?resources=1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.addCleanup(self.server.close)
        self._td = TempDir()
        self.store = self._td.__enter__() / "data"
        self.addCleanup(self._td.__exit__, None, None, None)


class FetchLimitTests(ServerCase):
    def test_oversized_answer_is_refused_and_keeps_last_good_copy(self):
        text, _, err = fetch(self.server.url, self.store, 0)
        self.assertEqual((text, err), (GOOD, ""))
        self.server.body = CAL.format("".join(event(str(i)) for i in range(50)))
        with mock.patch.object(ade, "MAX_BYTES", 2000):
            text, _, err = fetch(self.server.url, self.store, 0)
        self.assertEqual(text, GOOD)
        self.assertIn("larger than", err)

    def test_oversized_answer_without_copy_raises(self):
        self.server.body = CAL.format("".join(event(str(i)) for i in range(50)))
        with mock.patch.object(ade, "MAX_BYTES", 2000), self.assertRaises(FetchError):
            fetch(self.server.url, self.store, 0)
        self.assertFalse(list(self.store.glob("*.ics")))

    def test_too_many_events_is_a_failure(self):
        self.server.body = CAL.format("".join(event(str(i)) for i in range(4)))
        with mock.patch.object(ade, "MAX_EVENTS", 3), self.assertRaises(FetchError):
            fetch(self.server.url, self.store, 0)

    def test_slow_trickle_hits_the_overall_deadline(self):
        self.server.mode = "drip"
        t0 = time.monotonic()
        with mock.patch.object(ade, "DEADLINE_S", 0.6), self.assertRaises(FetchError) as cm:
            fetch(self.server.url, self.store, 0)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIn("took longer", str(cm.exception))

    def test_redirect_out_of_http_is_refused(self):
        self.server.mode = "redirect-ftp"
        with self.assertRaises(FetchError) as cm:
            fetch(self.server.url, self.store, 0)
        self.assertIn("refused", str(cm.exception))

    def test_redirect_loop_is_cut_off(self):
        self.server.mode = "redirect-loop"
        with self.assertRaises(FetchError):
            fetch(self.server.url, self.store, 0)
        self.assertLessEqual(len(self.server.hits), ade.MAX_REDIRECTS + 2)

    def test_truncated_download_keeps_last_good_copy(self):
        fetch(self.server.url, self.store, 0)               # good copy
        self.server.mode = "truncated"
        self.server.body = CAL.format(event("1") + event("2"))
        self.server.body = self.server.body[:self.server.body.index("END:VCALENDAR")]
        text, _, err = fetch(self.server.url, self.store, 0)
        self.assertTrue(err)
        self.assertEqual(text, GOOD)

    def test_cut_off_calendar_without_end_marker_is_refused(self):
        self.server.body = GOOD.replace("END:VCALENDAR\n", "")
        with self.assertRaises(FetchError) as cm:
            fetch(self.server.url, self.store, 0)
        self.assertIn("cut off", str(cm.exception))

    def test_folded_event_marker_counts_towards_the_cap(self):
        folded = "BEGIN:VEVEN\n T\nUID:1\nEND:VEVENT\n"
        self.server.body = CAL.format(folded * 4)
        with mock.patch.object(ade, "MAX_EVENTS", 3), self.assertRaises(FetchError):
            fetch(self.server.url, self.store, 0)

    def test_two_url_redirect_loop_is_cut_off_quickly(self):
        self.server.mode = "pingpong"
        with self.assertRaises(FetchError):
            fetch(self.server.url, self.store, 0)
        self.assertLessEqual(len(self.server.hits), 4)

    def test_redirect_after_the_deadline_is_refused(self):
        h = ade._SafeRedirect()
        req = urllib.request.Request("https://adecons.example.org/a")
        req.tamis_deadline = time.monotonic() - 1
        with self.assertRaises(urllib.error.URLError):
            h.redirect_request(req, None, 302, "Found", {}, "https://other.example.org/b")

    def test_legacy_ip_spellings_count_as_local(self):
        for host in ("127.1", "2130706433", "0177.0.0.1", "0x7f000001", "0", "localhost.",
                     "app.localhost", "[::ffff:127.0.0.1]", "::1"):
            with self.subTest(host=host):
                self.assertTrue(ade._local_host(host))
        for host in ("adecons.unistra.fr", "93.184.216.34", "1.1.1.1"):
            with self.subTest(host=host):
                self.assertFalse(ade._local_host(host))

    def test_redirect_rules(self):
        h = ade._SafeRedirect()
        req = urllib.request.Request("https://adecons.example.org/a")
        ok = h.redirect_request(req, None, 302, "Found", {}, "https://other.example.org/b")
        self.assertEqual(ok.full_url, "https://other.example.org/b")
        for new in ("http://adecons.example.org/b",          # downgrade
                    "ftp://adecons.example.org/b",           # other scheme
                    "file:///etc/passwd",
                    "http://127.0.0.1:8080/x",               # public -> loopback
                    "https://169.254.169.254/latest",        # public -> link-local
                    "https://10.0.0.5/x"):                   # public -> private
            with self.subTest(new=new), self.assertRaises(urllib.error.URLError):
                h.redirect_request(req, None, 302, "Found", {}, new)

    def test_http_error_still_reported_with_code(self):
        self.server.mode = "down"
        with self.assertRaises(FetchError) as cm:
            fetch(self.server.url, self.store, 0)
        self.assertIn("503", str(cm.exception))


class PoisonedCacheTests(ServerCase):
    def test_unreadable_events_never_replace_the_last_good_copy(self):
        fetch(self.server.url, self.store, 0)
        self.server.body = CAL.format(event("9", start="2026XX01T080000Z"))
        text, _, err = fetch(self.server.url, self.store, 0)
        self.assertEqual(text, GOOD)
        self.assertIn("could be read", err)
        (cached,) = self.store.glob("*.ics")
        self.assertEqual(cached.read_text(encoding="utf-8"), GOOD)

    def test_unreadable_events_without_copy_are_not_cached(self):
        self.server.body = CAL.format(event("9", start="2026XX01T080000Z"))
        with self.assertRaises(FetchError):
            fetch(self.server.url, self.store, 0)
        self.assertFalse(list(self.store.glob("*.ics")))

    def test_one_bad_event_among_good_ones_is_accepted(self):
        self.server.body = CAL.format(event("1") + event("2", start="2026XX01T080000Z"))
        text, _, err = fetch(self.server.url, self.store, 0)
        self.assertEqual(err, "")
        problems: list[str] = []
        evs = parse_ics(text, "s", ZoneInfo("UTC"), problems)
        self.assertEqual([e.uid for e in evs], ["1"])
        self.assertEqual(len(problems), 1)
        self.assertIn("'2'", problems[0])


class CacheKeyTests(ServerCase):
    def test_rolling_period_still_finds_the_last_good_copy(self):
        base = self.server.url
        fetch(base + "&firstDate=2026-09-01&lastDate=2027-02-28", self.store, 0)
        self.server.mode = "down"
        text, _, err = fetch(base + "&firstDate=2026-09-02&lastDate=2027-03-01", self.store, 0)
        self.assertEqual(text, GOOD)
        self.assertIn("503", err)
        self.assertEqual(len(list(self.store.glob("*.ics"))), 1)

    def test_different_sources_have_different_cache_files(self):
        a = ade._cache_key("http://x.example/ade?resources=1&firstDate=2026-09-01")
        b = ade._cache_key("http://x.example/ade?resources=2&firstDate=2026-09-01")
        self.assertNotEqual(a, b)


class ParserLimitTests(unittest.TestCase):
    def test_unfold_is_linear(self):
        text = "A\n" + " x\n" * 200_000
        t0 = time.monotonic()
        lines = unfold(text)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(lines[0], "A" + "x" * 200_000)

    def test_conflict_pairs_are_bounded_but_exam_pairs_survive(self):
        with TempDir() as tmp:
            cfg = Config(make_config(tmp))
            text = CAL.format("".join(
                event(str(i), summary="Examen Calcul stochastique") for i in range(60)))
            evs = parse_ics(text, "s", ZoneInfo("UTC"))
            with mock.patch.object(rules, "MAX_STORED_CONFLICTS", 10):
                kept, conflicts = rules.build(cfg, evs)
        self.assertEqual(len(kept), 60)
        self.assertEqual(len(conflicts), 10)
        self.assertTrue(all(a.is_exam and b.is_exam for a, b in conflicts))


class FileSourceTests(unittest.TestCase):
    def test_missing_or_undecodable_file_source_does_not_stop_the_others(self):
        with TempDir() as tmp:
            (tmp / "bad.ics").write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
            (tmp / "good.ics").write_text(GOOD, encoding="utf-8")
            base = """
            [settings]
            data_dir = "data"
            [period]
            start = "2026-09-01"
            end = "2027-02-28"
            [[sources]]
            name = "missing"
            file = "nope.ics"
            [[sources]]
            name = "bad"
            file = "bad.ics"
            [[sources]]
            name = "good"
            file = "good.ics"
            [[courses]]
            label = "Stoch"
            match = "calcul stochastique"
            """
            cfg = Config(make_config(tmp, base=base))
            events, health = ade.load_sources(cfg)
        self.assertEqual([h.name for h in health], ["missing", "bad", "good"])
        self.assertIn("cannot read", health[0].error)
        self.assertIn("cannot read", health[1].error)
        self.assertEqual(health[2].events, 1)
        self.assertEqual(len(events), 1)


class UnreachableSourceTests(unittest.TestCase):
    def test_source_without_any_copy_becomes_a_visible_failure(self):
        with TempDir() as tmp:
            base = """
            [settings]
            ade_url = "http://127.0.0.1:1/ade.jsp"
            project_id = 7
            data_dir = "data"
            stale_after_hours = 12
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
            cfg = Config(make_config(tmp, base=base))
            b = produce(cfg, alert=False)          # must not raise
        self.assertIn("cannot fetch", b.health[0].error)
        self.assertEqual(len(b.warnings), 1)
        self.assertIn("NOT updated since", b.warnings[0].summary)
        self.assertIn("NOT updated since", b.text)


if __name__ == "__main__":
    unittest.main()
