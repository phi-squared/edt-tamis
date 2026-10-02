"""Safe file writing: unique temp names, no following of planted symlinks."""

from __future__ import annotations

import functools
import logging
import os
import secrets
import stat
from pathlib import Path

from .paths import WINDOWS

log = logging.getLogger("tamis")


def private_dir(path: Path) -> None:
    """Create `path` (and parents) readable only by us; an existing folder is left alone."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def atomic_write(path: Path, data: bytes, mode: int = 0o666) -> None:
    """Write `data` to `path` so readers see the old or the new file, never a mix.

    The temporary file has an unpredictable name and is created exclusively and without
    following symlinks, so another local user who can write to the same folder can
    neither plant a link to make us overwrite some other file nor collide with a second
    writer. `os.replace` then swaps it in, replacing (not following) a link at `path`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_BINARY", 0)       # Windows: no newline translation
             | getattr(os, "O_NOFOLLOW", 0))
    for _ in range(10):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
        try:
            fd = os.open(tmp, flags, mode)      # the umask may remove more
        except FileExistsError:
            continue
        break
    else:
        raise OSError(f"cannot create a temporary file next to {path}")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())            # a power cut must not leave an empty file
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@functools.lru_cache(maxsize=None)
def warn_if_shared(directory: str) -> None:
    """Log once if other users can write to `directory` (POSIX only)."""
    if WINDOWS:
        return
    try:
        st = os.stat(directory)
    except OSError:
        return
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        log.warning("WARNING: %s is writable by other users; they could replace the files "
                    "tamis keeps there. Run: chmod 700 '%s'", directory, directory)
    if st.st_uid != os.geteuid():
        log.warning("WARNING: %s belongs to another user.", directory)
