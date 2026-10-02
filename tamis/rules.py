"""Course selection, de-duplication and clash resolution."""

from __future__ import annotations

import re
import unicodedata

from . import EdtError
from .config import Config
from .ics import Event

DEFAULT_EXAMS = r"examen|soutenance|partiel|rattrapage"
MAX_STORED_CONFLICTS = 5000   # per kind (exam / other); see build()


def norm(s: str) -> str:
    """Lower-case, accents removed, whitespace collapsed."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).casefold().strip()


def norm_pattern(pattern: str) -> str:
    """Normalise a config regex like `norm` does for titles (accents, case, whitespace),
    but leave escapes alone: casefolding `\\S`, `\\D`, `\\B`, `\\A` or `\\Z` would
    silently turn them into `\\s`, `\\d`, `\\b`, `\\a` (the bell character) or `\\z`."""
    p = unicodedata.normalize("NFKD", pattern)
    p = re.sub(r"\s+", " ", "".join(c for c in p if not unicodedata.combining(c))).strip()
    out: list[str] = []
    i = 0
    while i < len(p):
        c = p[i]
        if p.startswith("(?P", i):                    # (?P<name>...) / (?P=name): P is upper case
            out.append("(?P")
            i += 3
        elif c == "\\" and i + 1 < len(p):
            end = i + 2
            if p[i + 1] == "N" and p[end:end + 1] == "{" and "}" in p[end:]:
                end = p.index("}", end) + 1          # \N{UNICODE NAME}: names are case-sensitive
            out.append(p[i:end])
            i = end
        else:
            out.append(c.casefold())
            i += 1
    return "".join(out)


def rx(pattern: str) -> re.Pattern:
    try:
        return re.compile(norm_pattern(pattern))
    except re.error as exc:
        raise EdtError(f"bad regex {pattern!r}: {exc}") from None


def _priority(c: dict) -> int:
    p = c.get("priority", 50)
    if isinstance(p, bool) or not isinstance(p, int):
        raise EdtError(f"course {c.get('label', '?')!r}: priority must be a whole number, not {p!r}")
    return p


def _flag(c: dict, key: str, default: bool) -> bool:
    v = c.get(key, default)
    if not isinstance(v, bool):
        raise EdtError(f"course {c.get('label', '?')!r}: {key} must be true or false "
                       f"(without quotes), not {v!r}")
    return v


def _conflict_mode(c: dict, default: str) -> str:
    v = c.get("on_conflict", default)
    if v not in ("mark", "drop"):
        raise EdtError(f"course {c.get('label', '?')!r}: on_conflict must be \"mark\" or "
                       f"\"drop\", not {v!r}")
    return v


def _text_list(c: dict, key: str) -> list[str]:
    v = c.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise EdtError(f"course {c.get('label', '?')!r}: {key} must be a list of text "
                       f'entries, e.g. {key} = ["..."] (not {v!r})')
    return v


def compile_courses(cfg: Config) -> list[dict]:
    default_conflict = cfg.settings.get("on_conflict", "mark")
    if default_conflict not in ("mark", "drop"):
        raise EdtError(f'settings.on_conflict must be "mark" or "drop", not {default_conflict!r}')
    out = []
    for c in cfg.data.get("courses", []):
        if "label" not in c or "match" not in c:
            raise EdtError(f"every [[courses]] entry needs `label` and `match`: {c}")
        out.append({
            "label": c["label"],
            "re": rx(c["match"]),
            "priority": _priority(c),
            "attend": _flag(c, "attend", True),
            "on_conflict": _conflict_mode(c, default_conflict),
            "skip": [rx(p) for p in _text_list(c, "skip_match")],
            "skip_slots": [re.sub(r"\s+", " ", s.strip()) for s in _text_list(c, "skip_slots")],
        })
    return out


def compile_drops(cfg: Config) -> list[re.Pattern]:
    return [rx(d["match"]) for d in cfg.data.get("drop", [])]


def dedup(events: list[Event]) -> list[Event]:
    """The same lecture appears in several programme feeds with one UID; keep the newest."""
    by_uid: dict[str, Event] = {}
    for e in events:
        prev = by_uid.get(e.uid)
        if prev is None or (e.last_modified or "") > (prev.last_modified or ""):
            by_uid[e.uid] = e
    return list(by_uid.values())


def build(cfg: Config, events: list[Event]) -> tuple[list[Event], list[tuple[Event, Event]]]:
    """Returns (kept events sorted by start, list of overlapping pairs)."""
    s = cfg.settings
    exam_re = rx(s.get("exam_pattern", DEFAULT_EXAMS))
    mute_prefix = s.get("mute_prefix", "· ")
    courses = compile_courses(cfg)
    drops = compile_drops(cfg)

    kept: list[Event] = []
    for e in dedup(events):
        n = norm(e.summary)
        is_exam = bool(exam_re.search(n))
        # exams are never hidden by `drop` or `skip_match`: an exam of a course you keep
        # stays, and one that matches no course is left out anyway
        if not is_exam and any(d.search(n) for d in drops):
            continue
        for c in courses:
            if not c["re"].search(n):
                continue
            e.label, e.priority = c["label"], c["priority"]
            e.is_exam = is_exam
            if not is_exam and any(sk.search(n) for sk in c["skip"]):
                break
            if not e.is_exam:
                if e.slot() in c["skip_slots"]:
                    e.muted, e.note = True, "not attending this slot"
                if not c["attend"]:
                    e.muted, e.note = True, "exam only, no attendance"
            kept.append(e)
            break

    kept.sort(key=lambda x: (x.start, x.summary))
    mode_of = {c["label"]: c["on_conflict"] for c in courses}

    conflicts: list[tuple[Event, Event]] = []
    stored = {True: 0, False: 0}   # pairs kept, by 'involves an exam'
    for i, a in enumerate(kept):
        for b in kept[i + 1:]:
            if b.start >= a.end:
                break
            # Bounded, so a flood of identically-timed events cannot exhaust memory. Exam
            # pairs have their own budget: an exam clash must never be crowded out.
            involves_exam = a.is_exam or b.is_exam
            if stored[involves_exam] < MAX_STORED_CONFLICTS:
                stored[involves_exam] += 1
                conflicts.append((a, b))
            if a.is_exam and b.is_exam:
                continue  # never auto-resolve an exam clash; the report shouts instead
            if a.muted or b.muted:
                continue  # already out of the way
            loser, winner = (a, b) if a.priority < b.priority else (b, a)
            if loser.is_exam:
                loser, winner = winner, loser
            if mode_of.get(loser.label) == "drop":
                loser.note = "DROP"
            else:
                loser.muted, loser.note = True, f"clashes with {winner.label}"

    kept = [e for e in kept if e.note != "DROP"]
    for e in kept:
        if e.muted and mute_prefix and not e.summary.startswith(mute_prefix):
            e.summary = mute_prefix + e.summary
    return kept, conflicts
