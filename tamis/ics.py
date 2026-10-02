"""Minimal iCalendar reading and writing - just what ADE exports need."""

from __future__ import annotations

import datetime as dt
import re
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc
log = logging.getLogger("tamis")


def unfold(text: str) -> list[str]:
    """Join folded lines. Linear time: pieces are collected and joined once per line."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[list[str]] = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1].append(line[1:])
        else:
            out.append([line])
    return ["".join(parts) for parts in out]


def unescape(v: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def escape(v: str) -> str:
    return (v.replace("\\", "\\\\").replace("\n", "\\n")
             .replace(",", "\\,").replace(";", "\\;"))


def fold(line: str) -> str:
    """Fold to 75 octets; continuation lines start with one space."""
    if len(line.encode("utf-8")) <= 75:
        return line
    chunks, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not chunks else 74):
            chunks.append(cur)
            cur = b""
        cur += b
    chunks.append(cur)
    return "\r\n ".join(c.decode("utf-8") for c in chunks)


def parse_dt(value: str, params: str, tz: dt.tzinfo) -> dt.datetime:
    v = value.strip()
    if v.endswith("Z"):
        return dt.datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    m = re.search(r"TZID=([^;:]+)", params)
    if m:
        try:
            tz = ZoneInfo(m.group(1).strip('"'))
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass  # noqa: S110 - unknown TZID: keep the default time zone
    if len(v) == 8:
        return dt.datetime.strptime(v, "%Y%m%d").replace(tzinfo=tz)
    return dt.datetime.strptime(v[:15], "%Y%m%dT%H%M%S").replace(tzinfo=tz)


class Event:
    __slots__ = ("uid", "start", "end", "summary", "location", "description",
                 "last_modified", "source", "label", "priority", "muted",
                 "is_exam", "note", "tz", "all_day")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self) -> str:
        return f"<Event {self.local_start:%Y-%m-%d %H:%M} {self.summary!r}>"

    @property
    def local_start(self) -> dt.datetime:
        return self.start.astimezone(self.tz)

    @property
    def local_end(self) -> dt.datetime:
        return self.end.astimezone(self.tz)

    def slot(self) -> str:
        """Weekly slot like 'Fri 13:30-15:30' (English weekday names, C locale)."""
        s, e = self.local_start, self.local_end
        day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[s.weekday()]
        return f"{day} {s:%H:%M}-{e:%H:%M}"


def _plausible(ev: "Event") -> None:
    """Reject dates that parse but cannot be used later (year 9999 overflows when converted
    to another time zone); raises ValueError/OverflowError like a malformed date."""
    for d in (ev.start, ev.end):
        if not 1990 <= d.year <= 2100:
            raise ValueError(f"date {d.year} is outside 1990-2100")
        d.astimezone(UTC)
        d.astimezone(ev.tz)


def parse_ics(text: str, source: str, tz: dt.tzinfo,
              problems: list[str] | None = None) -> list[Event]:
    """Events of an iCalendar text. An event that cannot be read (for example a malformed
    date) is skipped, not fatal; a description of each skipped event is appended to
    `problems` when a list is given."""
    events: list[Event] = []
    cur: dict[str, tuple[str, str]] | None = None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur and "DTSTART" in cur and "UID" in cur:
                p_s, v_s = cur["DTSTART"]
                p_e, v_e = cur.get("DTEND", cur["DTSTART"])
                try:
                    ev = Event(
                        uid=cur["UID"][1].strip(),
                        start=parse_dt(v_s, p_s, tz), end=parse_dt(v_e, p_e, tz),
                        summary=unescape(cur.get("SUMMARY", ("", ""))[1]).strip(),
                        location=unescape(cur.get("LOCATION", ("", ""))[1]).strip(),
                        description=unescape(cur.get("DESCRIPTION", ("", ""))[1]).strip(),
                        last_modified=cur.get("LAST-MODIFIED", ("", ""))[1].strip(),
                        source=source, muted=False, is_exam=False, note="", tz=tz,
                    )
                    _plausible(ev)
                    events.append(ev)
                except (ValueError, OverflowError) as exc:
                    if problems is not None:
                        # repr() keeps control characters from the (untrusted) text harmless
                        problems.append(f"skipped event {cur['UID'][1].strip()[:60]!r}: {exc}")
            cur = None
        elif cur is not None and ":" in line:
            head, _, value = line.partition(":")
            name, _, params = head.partition(";")
            cur[name.upper()] = (params, value)
    return events


def to_ics(events: list[Event], *, name: str, tzname: str, prodid: str,
           refresh_hint_hours: int | None = None) -> str:
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//{prodid}//EN",
           "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{escape(name)}",
           f"X-WR-TIMEZONE:{tzname}"]
    if refresh_hint_hours:
        h = int(refresh_hint_hours)
        out += [f"REFRESH-INTERVAL;VALUE=DURATION:PT{h}H", f"X-PUBLISHED-TTL:PT{h}H"]
    for e in events:
        # DTSTAMP comes from the source's LAST-MODIFIED, never the clock, so
        # unchanged input gives a byte-identical file: same ETag, no git commit.
        stamp = e.last_modified if re.fullmatch(r"\d{8}T\d{6}Z", e.last_modified or "") \
            else f"{e.start.astimezone(UTC):%Y%m%dT%H%M%SZ}"
        bits = [b for b in (e.label, f"[{e.note}]" if e.note else "") if b]
        body = re.sub(r"\(Modifi[ée] le:[^)]*\)", "", e.description or "")
        body = "\n".join(line.strip() for line in body.split("\n") if line.strip())
        if body:
            bits.append(body)
        if e.all_day:
            when = [f"DTSTART;VALUE=DATE:{e.start:%Y%m%d}", f"DTEND;VALUE=DATE:{e.end:%Y%m%d}"]
        else:
            when = [f"DTSTART:{e.start.astimezone(UTC):%Y%m%dT%H%M%SZ}",
                    f"DTEND:{e.end.astimezone(UTC):%Y%m%dT%H%M%SZ}"]
        out += ["BEGIN:VEVENT", f"UID:{e.uid}", f"DTSTAMP:{stamp}", *when,
                f"SUMMARY:{escape(e.summary)}"]
        if e.location:
            out.append(f"LOCATION:{escape(e.location)}")
        if bits:
            out.append(f"DESCRIPTION:{escape(chr(10).join(bits))}")
        out += ["TRANSP:TRANSPARENT", "STATUS:TENTATIVE"] if e.muted else ["TRANSP:OPAQUE"]
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in out) + "\r\n"
