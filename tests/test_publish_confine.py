"""publish must stay inside its folder; file writes must not follow planted links."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import unittest

from tamis.config import Config
from tamis.fsutil import atomic_write
from tamis.publish import PublishError, check_file_name, publish_once

from helpers import TempDir, make_config


class FileNameTests(unittest.TestCase):
    def test_plain_names_are_accepted(self):
        for name in ("calendar.ics", "feed.ics", "k3_Xy-9.ics", "mon calendrier.ics", "été.ics"):
            with self.subTest(name=name):
                self.assertEqual(check_file_name(name), name)

    def test_everything_else_is_refused(self):
        for name in ("", "../x.ics", "/etc/passwd", "a/b.ics", "a\\b.ics", ".git", ".hidden.ics",
                     "-rf", ":/", ":(glob)*.ics", "*.ics", "a?.ics", "a[b].ics", "x" * 101,
                     "a\nb.ics", None, 7):
            with self.subTest(name=name), self.assertRaises(PublishError):
                check_file_name(name)


class DirMethodTests(unittest.TestCase):
    def cfg(self, tmp, target, name=None):
        extra = f'[publish]\nmethod = "dir"\ntarget = "{target}"\n'
        if name is not None:
            extra += f'file_name = "{name}"\n'
        return Config(make_config(tmp, extra))

    def test_missing_target_does_not_fall_back_to_the_config_folder(self):
        with TempDir() as tmp:
            cfg = Config(make_config(tmp, '[publish]\nmethod = "dir"\n'))
            with self.assertRaises(PublishError):
                publish_once(cfg)
            self.assertFalse((tmp / "calendar.ics").exists())

    def test_file_name_cannot_leave_the_folder(self):
        with TempDir() as tmp:
            out = tmp / "www"
            out.mkdir()
            for name in ("../evil.ics", "sub/evil.ics"):
                with self.subTest(name=name), self.assertRaises(PublishError):
                    publish_once(self.cfg(tmp, out.as_posix(), name))
            self.assertFalse((tmp / "evil.ics").exists())
            self.assertEqual(list(out.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "needs symlinks")
    def test_symlink_at_the_destination_is_refused(self):
        with TempDir() as tmp:
            out = tmp / "www"
            out.mkdir()
            victim = tmp / "victim.txt"
            victim.write_text("precious")
            (out / "calendar.ics").symlink_to(victim)
            with self.assertRaises(PublishError):
                publish_once(self.cfg(tmp, out.as_posix()))
            self.assertEqual(victim.read_text(), "precious")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitMethodTests(unittest.TestCase):
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

    def cfg(self, name="feed.ics"):
        extra = f'[publish]\nmethod = "git"\ntarget = "{self.clone.as_posix()}"\nfile_name = "{name}"\n'
        return Config(make_config(self.tmp, extra))

    def test_pathspec_magic_in_the_file_name_is_refused_before_anything_is_written(self):
        (self.clone / "other.ics").write_text("x")
        for name in (":/", ":(glob)*.ics", "*.ics", "../out.ics"):
            with self.subTest(name=name), self.assertRaises(PublishError):
                publish_once(self.cfg(name))
        self.assertEqual(self.git("status", "--porcelain", cwd=self.clone), "?? other.ics")

    def test_other_staged_changes_are_not_published(self):
        (self.clone / "private.txt").write_text("not for the feed")
        self.git("add", "private.txt", cwd=self.clone)
        with self.assertRaises(PublishError) as cm:
            publish_once(self.cfg())
        self.assertIn("private.txt", str(cm.exception))
        self.assertEqual(self.git("log", "--format=%s", cwd=self.clone), "init")
        self.assertEqual(self.git("--git-dir", str(self.remote), "log", "--format=%s",
                                  cwd=self.tmp), "init")

    def test_normal_publish_still_works(self):
        self.assertEqual(publish_once(self.cfg()), "pushed")
        self.assertIn("feed.ics", self.git("ls-tree", "--name-only", "HEAD", cwd=self.clone))


class AtomicWriteTests(unittest.TestCase):
    def test_writes_bytes_exactly_and_leaves_no_temp_files(self):
        with TempDir() as tmp:
            data = b"A\r\nB\r\n\xc3\xa9"
            atomic_write(tmp / "sub" / "f.ics", data)
            self.assertEqual((tmp / "sub" / "f.ics").read_bytes(), data)
            self.assertEqual([p.name for p in (tmp / "sub").iterdir()], ["f.ics"])

    @unittest.skipIf(os.name == "nt", "needs symlinks")
    def test_a_planted_symlink_is_replaced_not_followed(self):
        with TempDir() as tmp:
            victim = tmp / "victim.txt"
            victim.write_text("precious")
            (tmp / "f.ics").symlink_to(victim)
            atomic_write(tmp / "f.ics", b"new")
            self.assertEqual(victim.read_text(), "precious")
            self.assertFalse((tmp / "f.ics").is_symlink())
            self.assertEqual((tmp / "f.ics").read_bytes(), b"new")

    def test_concurrent_writers_never_leave_a_mixed_file(self):
        with TempDir() as tmp:
            target = tmp / "f.ics"
            payloads = [bytes([65 + i]) * 50_000 for i in range(12)]
            errors = []

            def go(data):
                try:
                    for _ in range(5):
                        atomic_write(target, data)
                except OSError as exc:       # Windows may briefly refuse a replace
                    errors.append(exc)

            threads = [threading.Thread(target=go, args=(d,)) for d in payloads]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertIn(target.read_bytes(), payloads)
            self.assertEqual([p.name for p in tmp.iterdir()], ["f.ics"])
            if os.name != "nt":
                self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
