"""`publish`: put the calendar at a *public* address (git repo / gist, or a folder).

Use this only if you accept that anyone who learns the URL can read your
timetable. The private alternative is `serve` behind Tailscale (see README).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

from . import EdtError
from .config import Config
from .pipeline import produce, write_if_changed
from .report import exam_clashes
from .safety import child_env, redact_urls, safe_text

log = logging.getLogger("tamis")
COMMIT_MSG = "calendar update [edt-tamis]"
# a plain file name: no path separators, no leading dot or dash, no git pathspec magic
# (`:`, `*`, `?`, `[`); `tamis secret` names (letters, digits, `_`, `-`) fit
_FILE_NAME = re.compile(r"\w[\w. -]{0,99}")
# Windows: device names are not files, and a trailing dot or space is silently dropped by
# the OS, so the file on disk and the git pathspec would disagree
_DEVICE = re.compile(r"(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", re.IGNORECASE)
GIT_TIMEOUT_S = 120


class PublishError(EdtError):
    pass


def _device_stem(name: str) -> str:
    """The part Windows looks at: compatibility forms folded (COM¹), spaces before the
    extension dropped (`con .ics`)."""
    return unicodedata.normalize("NFKC", name).split(".", 1)[0].strip()


def check_file_name(name) -> str:
    if (not isinstance(name, str) or not _FILE_NAME.fullmatch(name)
            or name[-1] in ". " or _DEVICE.fullmatch(_device_stem(name))):
        raise PublishError(
            f"publish.file_name {name!r} must be a plain file name (letters, digits, "
            "`_ - .` and spaces; no folders, no leading dot, no trailing dot or space, "
            "no Windows device names like CON or NUL)")
    return name


def _destination(cfg: Config) -> tuple[Path, Path]:
    """(target folder, file to write); refuses anything that could escape the folder."""
    p = cfg.publish
    if not p.get("target"):
        raise PublishError("set `target` in [publish] (the folder or clone to write to)")
    folder = cfg.resolve(p["target"])
    dest = folder / check_file_name(p.get("file_name", "calendar.ics"))
    if dest.is_symlink():
        raise PublishError(f"{dest} is a symbolic link; refusing to write through it")
    return folder, dest


def _git(repo: Path, *args: str, ident: tuple[str, str] | None = None) -> str:
    cmd = ["git", "--literal-pathspecs"]   # file names are never patterns
    if ident:  # a neutral identity so feed commits do not carry your name/e-mail
        cmd += ["-c", f"user.name={ident[0]}", "-c", f"user.email={ident[1]}"]
    cmd += ["-C", str(repo), *args]
    # List argv, never a shell. The only config values that reach the arguments are the
    # validated file name (after `--`, literal), the commit identity (one `-c` argument each)
    # and the target folder (`-C`).
    try:
        # git writes UTF-8 on every platform; the Windows locale (cp1252) must not decide
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",  # noqa: S603
                           env=child_env(), timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise PublishError(f"`git {args[0]}` did not finish within {GIT_TIMEOUT_S} s "
                           f"in {repo}; is the remote reachable?") from None
    if r.returncode != 0:
        # git echoes remote URLs, which may contain credentials
        raise PublishError(f"`git {redact_urls(' '.join(args))}` failed in {repo}:\n"
                           f"{safe_text(redact_urls(r.stderr.strip()))}")
    return r.stdout.strip()


def publish_git(cfg: Config, text: str) -> str:
    p = cfg.publish
    if not shutil.which("git"):
        raise PublishError("git is not installed")
    repo, dest = _destination(cfg)
    if not (repo / ".git").exists():
        raise PublishError(f"{repo} is not a git clone. Clone your feed repo or gist there first.")
    name = dest.name
    write_if_changed(dest, text)
    if not _git(repo, "status", "--porcelain", "--", name):
        return "unchanged"
    ident = (p.get("commit_name", "edt-tamis"), p.get("commit_email", "edt-tamis@localhost"))
    _git(repo, "add", "--", name)
    # -z: names are printed raw (no quoting of non-ASCII names), NUL-separated. The names
    # are never compared with the configured one as text (encodings differ between
    # platforms): "mine" is whatever git itself reports for that one path.
    staged = [f for f in _git(repo, "diff", "--cached", "--name-only", "-z").split("\0") if f]
    mine = {f for f in _git(repo, "diff", "--cached", "--name-only", "-z", "--", name)
            .split("\0") if f}
    others = [f for f in staged if f not in mine]
    if others:
        # a plain `git commit` would publish these too (and force-push them)
        raise PublishError(f"other changes are staged in {repo} ({', '.join(others[:3])}"
                           f"{', ...' if len(others) > 3 else ''}); commit or unstage them first")
    # constant argv, no shell
    has_head = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"],  # noqa: S607
        capture_output=True, timeout=GIT_TIMEOUT_S, env=child_env()).returncode == 0
    squash = (not p.get("keep_history", False) and has_head
              and _git(repo, "log", "-1", "--format=%s") == COMMIT_MSG)
    if squash:
        # overwrite our previous commit: the public repo holds only the current
        # timetable, not an archive of where you were every week
        _git(repo, "commit", "-q", "--amend", "-m", COMMIT_MSG, "--reset-author", ident=ident)
    else:
        _git(repo, "commit", "-q", "-m", COMMIT_MSG, ident=ident)
    if not p.get("push", True):
        return "committed (push disabled)"
    try:
        upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    except PublishError:
        raise PublishError(
            f"no upstream branch in {repo}; run once: git -C '{repo}' push -u origin HEAD"
        ) from None
    remote = upstream.split("/", 1)[0]
    if squash:
        _git(repo, "push", "-q", "--force-with-lease", remote, "HEAD")
    else:
        _git(repo, "push", "-q", remote, "HEAD")
    return "pushed"


def publish_dir(cfg: Config, text: str) -> str:
    folder, dest = _destination(cfg)
    if not folder.is_dir():
        raise PublishError(f"publish target folder {folder} does not exist")
    changed = write_if_changed(dest, text)
    return "written" if changed else "unchanged"


METHODS = {"git": publish_git, "dir": publish_dir}


def publish_once(cfg: Config, ttl_min: float | None = None) -> str:
    method = cfg.publish.get("method")
    if method not in METHODS:
        raise PublishError("add a [publish] section with method = \"git\" or \"dir\" "
                           "(see config.example.toml)")
    b = produce(cfg, ttl_min)
    result = METHODS[method](cfg, b.text)
    n = exam_clashes(b.conflicts)
    url = cfg.publish.get("public_url")
    failed = [s.name for s in b.health if s.error]
    log.info("%d events: %s%s%s%s", len(b.kept), result, f"  -> {url}" if url else "",
             f"  !! {n} exam clash(es)" if n else "",
             f"  !! using old copy for: {', '.join(failed)}" if failed else "")
    return result


def publish_job(cfg_path: Path, minutes: float):
    def job():
        publish_once(Config(cfg_path), ttl_min=minutes / 2)
    return job
