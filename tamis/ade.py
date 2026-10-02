"""Talking to ADE: building export URLs and downloading them (with a cache)."""

from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import ipaddress
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import APP, EdtError, __version__
from .config import Config
from .fsutil import atomic_write, private_dir, warn_if_shared
from .ics import Event, parse_ics, unfold
from .safety import redact_urls, safe_text

log = logging.getLogger("tamis")

DEFAULT_ADE = "https://adecons.unistra.fr/jsp/custom/modules/plannings/anonymous_cal.jsp"
# query parameters that describe a time window; stripped from pasted URLs
PERIOD_PARAMS = {"nbweeks", "firstdate", "lastdate", "nbdays", "date"}


class FetchError(EdtError):
    pass


def source_url(cfg: Config, src: dict, start: dt.date, end: dt.date) -> str:
    """The export URL for one source; any time window in a pasted URL is replaced."""
    if src.get("url"):
        parts = urllib.parse.urlsplit(src["url"])
        q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in PERIOD_PARAMS]
        base = urllib.parse.urlunsplit(parts._replace(query=""))
    else:
        res = src.get("resources")
        if res is None:
            raise EdtError(f"source {src.get('name', '?')!r}: needs `resources`, `url` or `file`")
        if not isinstance(res, list):
            res = [res]
        project = src.get("project_id", cfg.settings.get("project_id"))
        if project is None:
            raise EdtError("set `project_id` in [settings] (or per source)")
        base = src.get("ade_url", cfg.settings.get("ade_url", DEFAULT_ADE))
        q = [("resources", ",".join(str(r) for r in res)),
             ("projectId", str(project)), ("calType", "ical")]
    q += [("firstDate", start.isoformat()), ("lastDate", end.isoformat())]
    return base + "?" + urllib.parse.urlencode(q, safe=",")


def _explain(body: str) -> str:
    low = body[:2000].lower()
    if "<html" in low or "<!doctype html" in low:
        if any(w in low for w in ("cas", "login", "connexion", "shibboleth", "authentification")):
            return "got a login page instead of a calendar - this export needs a signed-in session"
        return "got a web page instead of a calendar"
    return "response is not an iCalendar document"


# Limits for what a (possibly misbehaving or hostile) server may make us do. A real ADE
# export is a few hundred KB and a few thousand events.
MAX_BYTES = 10 * 1024 * 1024    # response size
MAX_EVENTS = 5000               # events per source
MAX_REDIRECTS = 5
DEADLINE_S = 120                # whole download; the 45 s socket timeout is per operation


def _local_host(host: str | None) -> bool:
    # urllib percent-decodes the host before connecting, so we must too (%31%32%37.0.0.1)
    h = urllib.parse.unquote(host or "").strip("[]").rstrip(".").lower()
    if not h or h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        try:                   # legacy forms the OS resolver accepts: 127.1, 2130706433, 0x7f000001
            ip = ipaddress.ip_address(socket.inet_aton(h))
        except (OSError, ValueError):
            return False       # a name: cannot tell without resolving it
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_unspecified)


class _Guard:
    """Hard time limit for one download, including connect, status line and headers (which
    the socket timeout alone does not bound: a server may keep sending `100 Continue`).
    A timer shuts every socket opened for this download down when the time is up."""

    def __init__(self) -> None:
        self.socks: list[socket.socket] = []
        self.expired = False
        self.lock = threading.Lock()
        self.timer = threading.Timer(DEADLINE_S, self.expire)
        self.timer.daemon = True

    def expire(self) -> None:
        with self.lock:
            self.expired = True
            socks = list(self.socks)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def add(self, sock: socket.socket) -> None:
        with self.lock:
            self.socks.append(sock)
            late = self.expired
        if late:
            self.expire()

    def __enter__(self) -> "_Guard":
        _active.guard = self
        self.timer.start()
        return self

    def __exit__(self, *exc) -> None:
        self.timer.cancel()
        _active.guard = None


_active = threading.local()


def _register(conn) -> None:
    g = getattr(_active, "guard", None)
    if g is not None and conn.sock is not None:
        g.add(conn.sock)


class _Conn(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _register(self)


class _SConn(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        _register(self)


class _HTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_Conn, req)


class _HTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_SConn, req, context=self._context)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within http(s), never https -> http, never from a public
    host to a literal private/loopback/link-local address, and at most MAX_REDIRECTS."""

    max_redirections = MAX_REDIRECTS
    max_repeats = 1             # a URL seen twice in one chain is a loop

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        deadline = getattr(req, "tamis_deadline", None)
        refusal = None
        if deadline is not None and time.monotonic() > deadline:
            refusal = f"download took longer than {DEADLINE_S} s"
        elif new.scheme not in ("http", "https"):
            refusal = f"redirect to {new.scheme or '?'}: refused"
        elif old.scheme == "https" and new.scheme == "http":
            refusal = "redirect from https to http refused"
        elif _local_host(new.hostname) and not _local_host(old.hostname):
            refusal = "redirect to a private or local address refused"
        if refusal:
            if fp is not None:
                fp.close()
            raise urllib.error.URLError(refusal)
        nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nxt is not None:
            nxt.tamis_deadline = deadline
        return nxt


_opener = urllib.request.build_opener(_SafeRedirect, _HTTP, _HTTPS)


def _read_limited(resp, deadline: float | None = None) -> str:
    """Read the body with a size cap and an overall deadline (read1: returns what has
    arrived instead of waiting for a full buffer, so a slow trickle cannot stall us).
    A body shorter than its Content-Length is a failure, not a smaller calendar."""
    if deadline is None:
        deadline = time.monotonic() + DEADLINE_S
    chunks, total = [], 0
    while True:
        chunk = resp.read1(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BYTES:
            raise ValueError(f"response is larger than {MAX_BYTES // (1024 * 1024)} MB")
        if time.monotonic() > deadline:
            raise TimeoutError(f"download took longer than {DEADLINE_S} s")
        chunks.append(chunk)
    hdrs = getattr(resp, "headers", None)
    # with chunked encoding the length header must be ignored (RFC 9112); chunked bodies
    # raise IncompleteRead themselves when cut off
    declared = (hdrs.get("Content-Length") if hdrs is not None
                and not hdrs.get("Transfer-Encoding") else None)
    if declared and declared.strip().isdigit() and total < int(declared):
        raise http.client.IncompleteRead(b"", int(declared) - total)
    return b"".join(chunks).decode("utf-8", "replace")


def _cache_key(url: str) -> str:
    """One cache file per source, independent of the time window: with a rolling period
    the URL changes every day, and the last good copy must still be found."""
    parts = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() not in PERIOD_PARAMS]
    stable = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))
    return hashlib.sha256(stable.encode()).hexdigest()[:24]


EMPTY_REFUSED_DAYS = 14   # an empty answer is distrusted only while the last copy is this fresh


def _read_cache(path: Path) -> str | None:
    """The last good copy, or None if there is none or it is unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _check_body(body: str, previous: str | None, previous_age_days: float = 0.0) -> None:
    """Raise ValueError unless `body` is a plausible, readable calendar."""
    if "BEGIN:VCALENDAR" not in body:
        raise ValueError(_explain(body))
    # count on unfolded lines: a folded "BEGIN:VEVEN" + " T" is an event too
    lines = [ln.strip().upper() for ln in unfold(body)]
    if "END:VCALENDAR" not in [ln for ln in lines if ln][-1:]:
        raise ValueError("answer is cut off (no END:VCALENDAR at the end); not using it")
    n = lines.count("BEGIN:VEVENT")
    if n > MAX_EVENTS:
        raise ValueError(f"implausibly many events ({n}); not using this answer")
    if n == 0:
        if previous and "BEGIN:VEVENT" in previous and previous_age_days < EMPTY_REFUSED_DAYS:
            # an empty answer where there used to be events is almost always an
            # ADE hiccup (expired session, wrong project), not a cancelled semester;
            # after two weeks of empty answers it is believed (a quiet period)
            raise ValueError("ADE returned an empty calendar; previously it had events")
        return
    if not parse_ics(body, "check", dt.timezone.utc):
        raise ValueError(f"none of the {n} events in the answer could be read")


def fetch(url: str, store: Path, ttl_min: float) -> tuple[str, float, str]:
    """Download `url`, keeping the last good copy in `store`.

    Returns (ics_text, time_of_last_successful_download, error). `error` is empty
    when the text is current; otherwise the text is the last good copy and `error`
    says why the download failed. Raises FetchError only if there is no copy at all.
    An answer that is too big, too slow, unreadable or implausible counts as a failure
    and never replaces the last good copy.
    """
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise FetchError(f"refusing to fetch {url!r}: only http:// and https:// URLs are allowed")
    private_dir(store)
    warn_if_shared(str(store))
    cached = store / (_cache_key(url) + ".ics")
    # ttl 0 means "always download"; without the `> 0` a file timestamp slightly ahead of the
    # clock (seen on Windows) would make a just-written copy look fresh
    if ttl_min > 0 and cached.exists() and time.time() - cached.stat().st_mtime < ttl_min * 60:
        text = _read_cache(cached)
        if text is not None:
            return text, cached.stat().st_mtime, ""
    # http(s) only: checked above for this URL, and by _SafeRedirect for every redirect
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/{__version__}"})  # noqa: S310
    deadline = time.monotonic() + DEADLINE_S      # one budget for redirects and body together
    req.tamis_deadline = deadline
    guard = _Guard()
    try:
        with guard:
            with _opener.open(req, timeout=45) as r:
                body = _read_limited(r, deadline)
        previous = _read_cache(cached)
        age = (time.time() - cached.stat().st_mtime) / 86400 if previous is not None else 0.0
        _check_body(body, previous, age)
        atomic_write(cached, body.encode("utf-8"))
        return body, time.time(), ""
    except (urllib.error.URLError, http.client.HTTPException, ValueError, TimeoutError,
            OSError) as exc:
        if guard.expired:
            reason = f"download took longer than {DEADLINE_S} s"
        elif isinstance(exc, urllib.error.HTTPError):
            reason = f"HTTP {exc.code} {exc.reason}"
        else:
            reason = getattr(exc, "reason", None) or exc
        # what the server (or a proxy) says is untrusted text; URLs may carry credentials
        reason = safe_text(redact_urls(reason))
        old = _read_cache(cached)
        if old is not None:
            return old, cached.stat().st_mtime, reason
        raise FetchError(f"cannot fetch {redact_urls(url)}: {reason}") from exc


def source_location(cfg: Config, src: dict) -> str:
    """Human-readable 'where does this come from' (file path or URL)."""
    if src.get("file"):
        return str(cfg.resolve(src["file"]))
    return source_url(cfg, src, *cfg.period())


@dataclass
class SourceStatus:
    name: str
    last_success: float | None = None   # epoch seconds of the last good download
    error: str = ""                     # non-empty: this run used the last good copy
    events: int = 0
    skipped: int = 0                    # events in the answer that could not be read


def load_sources(cfg: Config, ttl_min: float | None = None
                 ) -> tuple[list[Event], list[SourceStatus]]:
    """All events from all sources, clipped to the observation period, plus per-source health."""
    start, end = cfg.period()
    ttl = (cfg.number("cache_minutes", 20, minimum=0, maximum=7 * 24 * 60)
           if ttl_min is None else float(ttl_min))
    events: list[Event] = []
    health: list[SourceStatus] = []
    for src in cfg.sources:
        st = SourceStatus(src.get("name", "?"))
        if src.get("file"):
            path = cfg.resolve(src["file"])
            try:
                # a local file is as current as it can be; staleness is about downloads
                text, st.last_success = path.read_text(encoding="utf-8"), time.time()
            except (OSError, UnicodeDecodeError) as exc:
                # the message ends up in the published feed: file name only, no folders
                st.error = f"cannot read {path.name}: {safe_text(type(exc).__name__)}"
                log.error("  ! %s: cannot read %s: %s", st.name, path, safe_text(exc))
                health.append(st)
                continue
        else:
            try:
                text, st.last_success, st.error = fetch(
                    source_url(cfg, src, start, end), cfg.data_dir, ttl)
            except FetchError as exc:
                # no copy at all: carry on with the other sources; the source shows up as
                # stale (warning event + alert) because it has no successful download
                st.error = str(exc)
                log.error("  ! %s: %s", st.name, exc)
                health.append(st)
                continue
        problems: list[str] = []
        got = parse_ics(text, st.name, cfg.tz, problems)
        st.events, st.skipped = len(got), len(problems)
        if problems:
            log.warning("  ! %s: %d event(s) skipped, e.g. %s", st.name, len(problems), problems[0])
        if st.error:
            when = dt.datetime.fromtimestamp(st.last_success)
            log.warning("  ! %s: %s - using the copy from %s", st.name, st.error, f"{when:%a %d %b %H:%M}")
        elif got:
            log.info("  · %s: %d events", st.name, len(got))
        else:
            log.warning("  ! %s: 0 events - wrong resource/project id, or empty period?", st.name)
        events.extend(got)
        health.append(st)
    lo = dt.datetime.combine(start, dt.time.min, cfg.tz)
    hi = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, cfg.tz)
    return [e for e in events if e.end > lo and e.start < hi], health
