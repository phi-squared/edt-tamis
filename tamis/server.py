"""`serve`: refresh in the background, answer HTTP requests from memory."""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import ipaddress
import http.server
import logging
import os
import socket
import stat
import sys
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path

from . import EdtError
from .config import Config, load_complete
from .paths import WINDOWS
from .pipeline import every, produce, write_if_changed
from .safety import safe_text
from .report import exam_clashes

log = logging.getLogger("tamis")
LOCAL_ONLY = ("127.0.0.1", "::1", "localhost")

# Limits against clients that open connections and then say little or nothing (slowloris).
CONN_TIMEOUT = 15        # seconds of silence before a connection is dropped
CONN_DEADLINE = 60       # hard limit for one whole connection (one request each)
MAX_CONNECTIONS = 64     # simultaneous connections; further ones are closed at once
MAX_REQUEST_BYTES = 16 * 1024   # request line + headers; one request per connection
MAX_PER_ADDRESS = 8      # ... of which one client (IPv4 address or IPv6 /64) may hold this many
# Failed logins from one address: after this many in a minute, answers are delayed.
FAIL_FREE, FAIL_WINDOW, FAIL_DELAY = 5, 60.0, 2.0


def read_auth(cfg: Config) -> str | None:
    """`user:password` from $TAMIS_AUTH or from settings.auth_file (which must be private)."""
    if os.environ.get("TAMIS_AUTH"):
        return os.environ["TAMIS_AUTH"]
    af = cfg.settings.get("auth_file")
    if not af:
        return None
    p = cfg.resolve(af)
    if not p.is_file():
        raise EdtError(f"auth_file {p} does not exist")
    if not WINDOWS and p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise EdtError(f"{p} is readable by other users; run: chmod 600 '{p}'")
    val = p.read_text(encoding="utf-8").strip()
    if ":" not in val:
        raise EdtError(f"{p} must contain a single line user:password")
    return val


class Feed:
    """The current calendar bytes, shared between the refresher and HTTP threads."""

    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()
        self.body, self.etag, self.mtime = b"", "", 0.0
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        body = self.path.read_bytes()
        with self.lock:
            self.body = body
            self.etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
            self.mtime = self.path.stat().st_mtime


def refresh_job(cfg_path: Path, feed: Feed, minutes: float):
    def job():
        cfg = load_complete(cfg_path)  # re-read each time: config edits need no restart
        b = produce(cfg, ttl_min=minutes / 2)
        changed = write_if_changed(feed.path, b.text)
        if changed:
            feed.load()
        n = exam_clashes(b.conflicts)
        failed = [s.name for s in b.health if s.error]
        log.info("%d events%s%s%s", len(b.kept), "  (changed)" if changed else "",
                 f"  !! {n} exam clash(es)" if n else "",
                 f"  !! using old copy for: {', '.join(failed)}" if failed else "")
    return job


def address_key(addr: str) -> str:
    """What counts as 'one client': an IPv4 address, or an IPv6 /64 (one customer's range)."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return addr
    if ip.is_loopback:
        return "loopback"
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def server_address_host(args) -> str:
    return args[0][0] if args and isinstance(args[0], tuple) else ""


class _Capped:
    """Reads from `raw` until `limit` bytes have been delivered, then reports end of input."""

    def __init__(self, raw, limit: int):
        self._raw, self._left = raw, limit

    def readline(self, size: int = -1) -> bytes:
        n = self._left if size is None or size < 0 else min(size, self._left)
        data = self._raw.readline(n)
        self._left -= len(data)
        return data

    def read(self, size: int = -1) -> bytes:
        n = self._left if size is None or size < 0 else min(size, self._left)
        data = self._raw.read(n)
        self._left -= len(data)
        return data

    def close(self) -> None:
        self._raw.close()


class _Server(http.server.ThreadingHTTPServer):
    """Threading HTTP server with a cap on simultaneous connections."""

    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *a, max_connections: int | None = None,
                 max_per_address: int | None = None, **kw):
        self._slots = threading.BoundedSemaphore(max_connections or MAX_CONNECTIONS)
        if ":" in str(server_address_host(a)):
            self.address_family = socket.AF_INET6          # bind = "::1" / "::"
        self._per_address = max_per_address or MAX_PER_ADDRESS
        self._open: dict[str, int] = {}
        self._open_lock = threading.Lock()
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        key = address_key(client_address[0])
        with self._open_lock:
            # one client may not fill every slot (loopback = a local proxy such as
            # `tailscale serve`, which carries many users: exempt)
            over = key != "loopback" and self._open.get(key, 0) >= self._per_address
            if not over:
                self._open[key] = self._open.get(key, 0) + 1
        if over or not self._slots.acquire(blocking=False):
            if not over:
                self._release_address(key)
            self.shutdown_request(request)          # too many: drop, do not queue
            return
        super().process_request(request, client_address)

    def _release_address(self, key: str) -> None:
        with self._open_lock:
            n = self._open.get(key, 0) - 1
            if n > 0:
                self._open[key] = n
            else:
                self._open.pop(key, None)

    def handle_error(self, request, client_address):
        # a client that vanished mid-request is routine; the stdlib would print a traceback
        log.debug("connection from %s failed: %r", client_address[0], sys.exc_info()[1])

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
            self._release_address(address_key(client_address[0]))


class _Security:
    """Settings that can change without a restart: password, secret path, allowed hosts."""

    def __init__(self, cfg: Config, url_path_arg: str | None):
        self.url_path_arg = url_path_arg
        self.apply(cfg)

    def apply(self, cfg: Config) -> None:
        s = cfg.settings
        auth = read_auth(cfg)
        expected = (("Basic " + base64.b64encode(auth.encode()).decode()).encode()
                    if auth else None)
        path = self.url_path_arg or s.get("url_path", "calendar.ics")
        if not isinstance(path, str):
            raise EdtError(f"settings.url_path must be text, not {path!r}")
        hosts = s.get("allowed_hosts", [])
        if isinstance(hosts, str):
            hosts = [hosts]
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise EdtError(f"settings.allowed_hosts must be a list of names, not {hosts!r}")
        # validated first, assigned together: a bad value never leaves a half-applied state
        self.expected = expected
        self.url_path = "/" + path.lstrip("/")
        self.allowed_hosts = {h.lower() for h in hosts}

    def host_ok(self, host_header: str | None, bind: str) -> bool:
        """DNS-rebinding guard: a local server without a password only answers requests
        addressed to a local name (or one listed in `allowed_hosts`)."""
        if self.expected or bind not in LOCAL_ONLY or "*" in self.allowed_hosts:
            return True
        if not host_header:
            return True
        try:
            host = (urllib.parse.urlsplit("//" + host_header).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        return host in LOCAL_ONLY or host in self.allowed_hosts


def _not_modified_since(header: str, mtime: float) -> bool:
    try:
        return int(mtime) <= email.utils.parsedate_to_datetime(header).timestamp()
    except (TypeError, ValueError):
        return False


def make_server(cfg: Config, *, port: int | None = None, bind: str | None = None,
                url_path: str | None = None, verbose: bool = False, refresh: bool = True):
    """Build (but do not start) the HTTP server. Returns (server, feed, stop_event)."""
    s = cfg.settings
    port = int(cfg.number("port", 8765, minimum=0, maximum=65535)) if port is None else port
    bind = bind or s.get("bind", "127.0.0.1")
    minutes = cfg.number("refresh_minutes", 120, minimum=10, maximum=7 * 24 * 60)
    sec = _Security(cfg, url_path)
    feed = Feed(cfg.output_file)
    stop = threading.Event()
    failures: dict[str, deque] = {}          # failed logins per client address
    failures_lock = threading.Lock()
    seen_proxy = []                          # one-shot flag for the proxy warning

    def reload_security() -> None:
        """Pick up a changed password / secret path / allowed_hosts (called on every refresh)."""
        try:
            sec.apply(load_complete(cfg.path))
        except Exception as exc:  # whatever is wrong: keep serving and keep refreshing
            log.error("kept the previous password and path settings: %s", safe_text(exc))

    if refresh:
        job = refresh_job(cfg.path, feed, minutes)

        def job_and_reload():
            reload_security()
            job()

        threading.Thread(target=every, args=(minutes, job_and_reload, stop),
                         daemon=True, name="refresh").start()

    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = CONN_TIMEOUT

        def setup(self):
            super().setup()
            # a client may not make us buffer megabytes of headers (stdlib allows ~6 MB each)
            self.rfile = _Capped(self.rfile, MAX_REQUEST_BYTES)
            # hard stop for the whole connection, however slowly the client trickles
            self._watchdog = threading.Timer(CONN_DEADLINE, self._abort)
            self._watchdog.daemon = True
            self._watchdog.start()

        def finish(self):
            self._watchdog.cancel()
            super().finish()

        def _abort(self) -> None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def send_response(self, code, message=None):
            # like the stdlib, but without a `Server:` header (no fingerprinting)
            self.log_request(code)
            self.send_response_only(code, message)
            self.send_header("Date", self.date_time_string())

        def _reply(self, code: int, length: int = 0, headers: dict | None = None) -> None:
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(length))
            self.end_headers()

        def _failed_login(self) -> None:
            addr = address_key(self.client_address[0])
            now = time.monotonic()
            with failures_lock:
                q = failures.setdefault(addr, deque())
                q.append(now)
                while q and now - q[0] > FAIL_WINDOW:
                    q.popleft()
                if len(failures) > 1024:               # forget old clients
                    for k in [k for k, v in failures.items()
                              if not v or now - v[-1] > FAIL_WINDOW]:
                        del failures[k]
                slow = len(q) > FAIL_FREE
            if slow:
                time.sleep(FAIL_DELAY)                 # slow guessing down; never lock anyone out

        def _handle(self, send_body: bool) -> None:
            expected = sec.expected
            if not expected and not seen_proxy and any(
                    h in self.headers for h in ("X-Forwarded-For", "Forwarded", "X-Real-IP",
                                                "Tailscale-User-Login")):
                seen_proxy.append(True)
                log.warning("WARNING: requests arrive through a proxy (tailscale serve/funnel, "
                            "nginx, ...) but no password is set; anyone who can reach that "
                            "address can read the calendar.")
            hosts = self.headers.get_all("Host") or []
            if len(hosts) > 1 or not sec.host_ok(hosts[0] if hosts else None, bind):
                self._reply(421)                       # Misdirected Request (DNS rebinding guard)
                return
            if expected and not hmac.compare_digest(
                    self.headers.get("Authorization", "").encode(), expected):
                self._failed_login()
                self._reply(401, headers={"WWW-Authenticate": 'Basic realm="calendar"'})
                return
            if urllib.parse.urlsplit(self.path).path != sec.url_path:
                self._reply(404)
                return
            with feed.lock:
                body, etag, mtime = feed.body, feed.etag, feed.mtime
            if not body:
                self._reply(503, headers={"Retry-After": "120"})
                return
            headers = {"ETag": etag, "Cache-Control": "no-cache",
                       "Last-Modified": email.utils.formatdate(mtime, usegmt=True)}
            inm = self.headers.get("If-None-Match")
            ims = self.headers.get("If-Modified-Since")
            if (inm is not None and etag in [t.strip() for t in inm.split(",")]) or (
                    inm is None and ims is not None and _not_modified_since(ims, mtime)):
                self._reply(304, headers=headers)
                return
            headers["Content-Type"] = "text/calendar; charset=utf-8"
            self._reply(200, len(body), headers)
            if send_body:
                self.wfile.write(body)

        def do_GET(self):
            self._handle(True)

        def do_HEAD(self):
            self._handle(False)

        def log_message(self, fmt, *a):
            if verbose:
                # the secret path is the only protection of some setups: keep it out of logs
                # the request line is whatever the client sent: strip control characters
                log.info("%s %s", self.address_string(),
                         safe_text((fmt % a).replace(sec.url_path, "/<path>")))

    srv = _Server((bind, port), Handler)
    srv.reload_security = reload_security
    if bind not in LOCAL_ONLY and not sec.expected:
        log.warning("WARNING: listening on %s without a password.", bind)
    if bind not in LOCAL_ONLY and sec.expected:
        log.warning("WARNING: listening on %s: the password travels unencrypted (HTTP Basic) "
                    "unless a TLS-terminating proxy is in front.", bind)
    host, real_port = srv.server_address[:2]
    shown = sec.url_path if sec.url_path == "/calendar.ics" else "/<secret path>"
    log.info("serving http://%s:%s%s  (refresh every %g min%s)", host, real_port, shown,
             minutes, ", basic auth on" if sec.expected else "")
    return srv, feed, stop


def serve(cfg: Config, **kw) -> int:
    srv, _, stop = make_server(cfg, **kw)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
    return 0
