"""Small helpers for text and processes that cross a trust boundary."""

from __future__ import annotations

import os
import re

_URL_CREDENTIALS = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s'\"]+@")


def safe_text(s: object) -> str:
    """`s` made harmless to print on a terminal.

    Event titles, locations and server answers are untrusted: raw escape sequences in
    them could rewrite the screen, set the window title or write to the clipboard.
    Line breaks and tabs become spaces; every other control, format (bidi, zero-width)
    or unassigned character becomes `?`.
    """
    out = []
    for ch in str(s):
        if ch in "\n\r\t\v\f  ":
            out.append(" ")
        elif ch.isprintable():
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def redact_urls(s: object) -> str:
    """Hide `user:password@` in any URL inside `s` (git and HTTP errors echo them)."""
    return _URL_CREDENTIALS.sub(r"\1***@", str(s))


def child_env(**extra: str) -> dict[str, str]:
    """Environment for a child process: ours, without the server password."""
    env = {k: v for k, v in os.environ.items() if k != "TAMIS_AUTH"}
    env.update(extra)
    return env
