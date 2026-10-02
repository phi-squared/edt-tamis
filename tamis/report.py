"""Human-readable summaries: `report` and `titles`."""

from __future__ import annotations

import datetime as dt
import time
from collections import Counter, defaultdict

from .ade import SourceStatus
from .config import Config
from .health import stale
from .ics import Event
from .rules import compile_courses, compile_drops, dedup, norm
from .safety import safe_text as clean   # titles, places and errors come from outside


def report(cfg: Config, events: list[Event], conflicts: list[tuple[Event, Event]]) -> str:
    start, end = cfg.period()
    L = [f"period {start} -> {end}   {len(events)} sessions kept\n",
         f"{'course':38s} {'tot':>4s} {'att':>4s} {'mute':>5s}  usual slots", "-" * 100]
    per: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        per[e.label].append(e)
    for c in cfg.data.get("courses", []):
        evs = per.get(c["label"], [])
        att = sum(1 for e in evs if not e.muted)
        slots = Counter(e.slot() for e in evs if not e.is_exam)
        top = "; ".join(f"{s} x{n}" for s, n in slots.most_common(3))
        warn = "   <-- matches nothing: title changed? see `titles`" if not evs else ""
        L.append(f"{c['label'][:38]:38s} {len(evs):4d} {att:4d} {len(evs) - att:5d}  {top}{warn}")

    exams = sorted((e for e in events if e.is_exam), key=lambda x: x.start)
    if exams:
        L += ["\nexams / soutenances", "-" * 100]
        L += [f"  {e.local_start:%a %d %b %Y %H:%M}-{e.local_end:%H:%M}  {clean(e.summary)}  @{clean(e.location)}"
              for e in exams]

    if conflicts:
        L += ["\noverlaps (lower priority greyed out)", "-" * 100]
        agg = Counter(tuple(sorted((a.label, b.label))) for a, b in conflicts)
        L += [f"  {n:3d}x  {x}  <->  {y}" for (x, y), n in agg.most_common()]
        hard = [(a, b) for a, b in conflicts if a.is_exam and b.is_exam]
        soft = [(a, b) for a, b in conflicts if (a.is_exam or b.is_exam) and not (a.is_exam and b.is_exam)]
        if hard:
            L.append("\n  *** EXAM CLASHES (two exams at once) ***")
            L += [f"  {a.local_start:%a %d %b %H:%M}  {clean(a.summary)}  <->  {clean(b.summary)}" for a, b in hard]
        if soft:
            L.append("\n  exams overlapping a class (the class was greyed out):")
            L += [f"  {a.local_start:%a %d %b %H:%M}  {clean(a.summary)}  <->  {clean(b.summary)}" for a, b in soft]
    return "\n".join(L)


def exam_clashes(conflicts: list[tuple[Event, Event]]) -> int:
    return sum(1 for a, b in conflicts if a.is_exam and b.is_exam)


def titles(cfg: Config, events: list[Event], only_unmatched: bool = False) -> str:
    courses = compile_courses(cfg)
    drops = compile_drops(cfg)
    groups: dict[str, list[Event]] = defaultdict(list)
    for e in dedup(events):
        groups[e.summary].append(e)
    rows = []
    for title, evs in sorted(groups.items(), key=lambda kv: norm(kv[0])):
        n = norm(title)
        if any(d.search(n) for d in drops):
            tag = "(dropped)"
        else:
            tag = next((c["label"] for c in courses if c["re"].search(n)), "")
        if only_unmatched and tag:
            continue
        srcs = clean(",".join(sorted({e.source for e in evs})))
        title = clean(title)
        slot = Counter(e.slot() for e in evs).most_common(1)[0][0]
        rows.append(f"{len(evs):4d}  {title[:52]:52s} {slot:18s} {tag[:28]:28s} {srcs}")
    head = f"{'n':>4s}  {'title':52s} {'usual slot':18s} {'-> course':28s} sources"
    return "\n".join([head, "-" * len(head)] + rows)


def health_table(cfg: Config, health: list[SourceStatus]) -> str:
    now = time.time()
    bad = {id(s) for s in stale(cfg, health, now)}
    L = [f"{'source':34s} {'events':>6s}  {'last good download':20s} status", "-" * 100]
    for s in health:
        when = (f"{dt.datetime.fromtimestamp(s.last_success, cfg.tz):%a %d %b %H:%M}"
                f" ({(now - s.last_success) / 3600:.0f} h)") if s.last_success else "never"
        state = ("STALE - " if id(s) in bad else "") + (f"FAILED: {clean(s.error)}" if s.error else "ok")
        L.append(f"{clean(s.name)[:34]:34s} {s.events:6d}  {when:20s} {state}")
    return "\n".join(L)
