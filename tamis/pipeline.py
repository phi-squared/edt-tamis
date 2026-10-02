"""fetch -> filter -> serialise, plus the periodic loop shared by `serve` and `publish --watch`."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import APP, EdtError
from .ade import SourceStatus, load_sources
from .config import Config
from .fsutil import atomic_write
from .ics import Event, to_ics
from .health import Alerter, warning_events
from .rules import build

log = logging.getLogger("tamis")


@dataclass
class Build:
    text: str                         # the finished .ics
    kept: list[Event]                 # course events (without warning events)
    conflicts: list[tuple]
    health: list[SourceStatus]
    warnings: list[Event] = field(default_factory=list)


def produce(cfg: Config, ttl_min: float | None = None, alert: bool = True) -> Build:
    events, health = load_sources(cfg, ttl_min)
    kept, conflicts = build(cfg, events)
    warnings = warning_events(cfg, health)
    if alert:
        Alerter(cfg).update(health)
    s = cfg.settings
    text = to_ics(warnings + kept, name=s.get("calendar_name", "Timetable"), tzname=cfg.tz.key,
                  prodid=APP, refresh_hint_hours=int(cfg.number("refresh_hint_hours", 0, minimum=0,
                                                      maximum=24 * 30)) or None)
    return Build(text, kept, conflicts, health, warnings)


def write_if_changed(path: Path, text: str) -> bool:
    """Atomic write, skipped when the content is identical. True if the file changed."""
    data = text.encode("utf-8")  # bytes: CRLF must survive on every platform
    if path.exists() and path.read_bytes() == data:
        return False
    atomic_write(path, data)
    return True


def every(minutes: float, job: Callable[[], None], stop: threading.Event) -> None:
    """Run `job` now and then every `minutes`; errors are logged, never fatal."""
    while not stop.is_set():
        t0 = time.time()
        try:
            job()
        except EdtError as exc:
            log.error("kept previous calendar: %s", exc)
        except Exception as exc:  # the loop must survive anything
            log.exception("unexpected error, kept previous calendar: %r", exc)
        stop.wait(max(60.0, minutes * 60 - (time.time() - t0)))
