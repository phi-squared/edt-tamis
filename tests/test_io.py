"""HTTP fetching, the server, publishing and service files."""

import http.server
import os
import plistlib
import shutil
import subprocess
import threading
import unittest
import urllib.error
import urllib.request

from tamis import EdtError
from tamis.cli import main
from tamis.config import Config
from tamis.pipeline import produce, write_if_changed
from tamis.publish import COMMIT_MSG, publish_once
from tamis.server import make_server
from tamis.service import render

from helpers import FIXTURES, TempDir, make_config


class FakeAde:
    """Serves a fixture for any path and records the requested URLs."""

    def __init__(self):
        self.hits = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append(self.path)
                body = (FIXTURES / "a.ics").read_bytes()
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


class FetchTests(unittest.TestCase):
    def test_fetch_period_and_cache(self):
        ade = FakeAde()
        try:
            with TempDir() as tmp:
                base = f"""
                [settings]
                ade_url = "{ade.url}"
                project_id = 7
                data_dir = "data"
                [period]
                start = "2026-09-01"
                end = "2027-02-28"
                [[sources]]
                name = "x"
                resources = [11, 12]
                [[courses]]
                label = "Stoch"
                match = "calcul stochastique"
                """
                cfg = Config(make_config(tmp, base=base))
                kept = produce(cfg).kept
                kept2 = produce(cfg).kept          # served from cache
            self.assertEqual(len(ade.hits), 1)
            self.assertIn("resources=11,12", ade.hits[0])
            self.assertIn("firstDate=2026-09-01&lastDate=2027-02-28", ade.hits[0])
            self.assertEqual(len(kept), len(kept2))
            self.assertTrue(kept)
        finally:
            ade.close()


class ServerTests(unittest.TestCase):
    def test_auth_etag_paths(self):
        with TempDir() as tmp:
            (tmp / "auth.txt").write_text("me:secret\n")
            if os.name != "nt":
                os.chmod(tmp / "auth.txt", 0o600)
            cfg = Config(make_config(tmp))
            cfg.settings["auth_file"] = "auth.txt"
            text = produce(cfg).text
            write_if_changed(cfg.output_file, text)
            srv, _, stop = make_server(cfg, port=0, url_path="cal.ics", refresh=False)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"

            def get(path, user=None, headers=None):
                req = urllib.request.Request(base + path, headers=headers or {})
                if user:
                    import base64
                    req.add_header("Authorization", "Basic " + base64.b64encode(user.encode()).decode())
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, r.read(), r.headers
                except urllib.error.HTTPError as e:
                    return e.code, b"", e.headers

            try:
                self.assertEqual(get("/cal.ics")[0], 401)
                self.assertEqual(get("/cal.ics", "me:wrong")[0], 401)
                self.assertEqual(get("/other", "me:secret")[0], 404)
                code, body, h = get("/cal.ics", "me:secret")
                self.assertEqual(code, 200)
                self.assertEqual(body, text.encode())
                code, body, _ = get("/cal.ics", "me:secret", {"If-None-Match": h["ETag"]})
                self.assertEqual((code, body), (304, b""))
            finally:
                stop.set()
                srv.shutdown()
                srv.server_close()

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_world_readable_auth_file_refused(self):
        with TempDir() as tmp:
            (tmp / "auth.txt").write_text("me:secret\n")
            os.chmod(tmp / "auth.txt", 0o644)
            cfg = Config(make_config(tmp))
            cfg.settings["auth_file"] = "auth.txt"
            with self.assertRaises(EdtError):
                make_server(cfg, port=0, refresh=False)


class FetchSchemeTests(unittest.TestCase):
    def test_only_http_urls_are_fetched(self):
        from tamis.ade import FetchError, fetch
        for url in ("file:///etc/passwd", "ftp://example.org/x.ics", "gopher://x", "/etc/passwd"):
            with self.subTest(url=url), TempDir() as td:
                with self.assertRaises(FetchError):
                    fetch(url, td, 60)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class PublishTests(unittest.TestCase):
    def git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout.strip()

    def setUp(self):
        self._td = TempDir()
        self.tmp = self._td.__enter__()
        self.remote = self.tmp / "remote.git"
        self.clone = self.tmp / "clone"
        self.git("init", "-q", "--bare", str(self.remote), cwd=self.tmp)
        self.git("clone", "-q", str(self.remote), str(self.clone), cwd=self.tmp)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                 "-m", "init", cwd=self.clone)
        self.git("push", "-q", "-u", "origin", "HEAD", cwd=self.clone)

    def tearDown(self):
        self._td.__exit__(None, None, None)

    def cfg(self, keep_history=False):
        extra = f"""
        [publish]
        method = "git"
        target = "{self.clone.as_posix()}"
        file_name = "feed.ics"
        keep_history = {str(keep_history).lower()}
        """
        return Config(make_config(self.tmp, extra))

    def test_single_commit_without_history(self):
        cfg = self.cfg()
        self.assertEqual(publish_once(cfg), "pushed")
        self.assertEqual(publish_once(cfg), "unchanged")
        # simulate a timetable change
        cfg.data["courses"] = cfg.data["courses"][:1]
        self.assertEqual(publish_once(cfg), "pushed")
        log = self.git("log", "--format=%s|%an", cwd=self.clone)
        self.assertEqual(log.splitlines(), [f"{COMMIT_MSG}|edt-tamis", "init|t"])
        remote_log = self.git("--git-dir", str(self.remote), "log", "--format=%s", cwd=self.tmp)
        self.assertEqual(remote_log.splitlines(), [COMMIT_MSG, "init"])

    def test_keep_history(self):
        cfg = self.cfg(keep_history=True)
        publish_once(cfg)
        cfg.data["courses"] = cfg.data["courses"][:1]
        publish_once(cfg)
        self.assertEqual(len(self.git("log", "--format=%s", cwd=self.clone).splitlines()), 3)

    def test_dir_method(self):
        out = self.tmp / "www"
        out.mkdir()
        cfg = Config(make_config(self.tmp, f'[publish]\nmethod = "dir"\ntarget = "{out.as_posix()}"\n'))
        self.assertEqual(publish_once(cfg), "written")
        self.assertTrue((out / "calendar.ics").read_bytes().startswith(b"BEGIN:VCALENDAR"))
        self.assertEqual(publish_once(cfg), "unchanged")


class ServiceTests(unittest.TestCase):
    def test_all_kinds_render(self):
        with TempDir() as tmp:
            cfg = Config(make_config(tmp))
            for mode in ("serve", "publish"):
                u = render(cfg, mode, "launchd", python="/usr/bin/python3")
                pl = plistlib.loads(u.text.encode())
                self.assertEqual(pl["ProgramArguments"][:3], ["/usr/bin/python3", "-m", "tamis"])
                self.assertEqual(pl["ProgramArguments"][-1], "--watch" if mode == "publish" else "serve")
                self.assertIn(str(cfg.path), pl["ProgramArguments"])

                u = render(cfg, mode, "systemd", python="/usr/bin/python3")
                self.assertIn("ExecStart=\"/usr/bin/python3\" \"-m\" \"tamis\"", u.text)

                u = render(cfg, mode, "windows", python=r"C:\Python312\pythonw.exe")
                self.assertIn("Register-ScheduledTask", u.text)
                self.assertIn(r"'C:\Python312\pythonw.exe'", u.text)


class CliTests(unittest.TestCase):
    def test_init_and_urls(self):
        with TempDir() as tmp:
            target = tmp / "sub" / "config.toml"
            self.assertEqual(main(["-c", str(target), "init"]), 0)
            self.assertIn("[[courses]]", target.read_text(encoding="utf-8"))
            self.assertEqual(main(["-c", str(target), "init"]), 2)   # refuses to overwrite
            self.assertEqual(main(["-c", str(target), "urls"]), 0)

    def test_missing_config_is_a_clean_error(self):
        self.assertEqual(main(["-c", "/definitely/not/here.toml", "report"]), 2)


if __name__ == "__main__":
    unittest.main()
