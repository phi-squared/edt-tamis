"""Command-line interface. No GUI by design."""

from __future__ import annotations

import argparse
import logging
import os
import logging.handlers
import secrets
import sys
import threading
from importlib import resources
from pathlib import Path

from . import APP, EdtError, __version__
from .ade import load_sources, source_location
from .config import Config, find_config
from .fsutil import atomic_write, private_dir
from .paths import config_home

log = logging.getLogger("tamis")

EPILOG = """\
typical order:  init -> urls -> status -> titles -> report -> serve (private) or publish (public)

config lookup:  -c PATH, $TAMIS_CONFIG, ./config.toml, then the per-user config folder
                (~/.config/edt-tamis, or %APPDATA%\\edt-tamis on Windows)
"""


def setup_logging(log_file: str | None, timestamps: bool) -> None:
    fmt = "%(asctime)s  %(message)s" if timestamps or log_file else "%(message)s"
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tamis", epilog=EPILOG,
                                 description="One calendar with only your courses, from ADE exports.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", help="config file")
    ap.add_argument("--log-file", help="log to this file (rotated) instead of the terminal")
    ap.add_argument("-V", "--version", action="version", version=f"{APP} {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    p = sub.add_parser("init", help="create your config from the annotated example")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    sub.add_parser("urls", help="print the ADE URLs that will be fetched")
    p = sub.add_parser("titles", help="list distinct event titles (to write `match` rules)")
    p.add_argument("--unmatched", action="store_true", help="only titles no course matches")
    sub.add_parser("report", help="kept sessions, exams and clashes (default command)")
    sub.add_parser("status", help="download every source now; exit code 1 if any fails")
    p = sub.add_parser("build", help="write the filtered calendar to a file once")
    p.add_argument("-o", "--out", help="output path (default: settings.output_file)")

    p = sub.add_parser("serve", help="PRIVATE: refresh periodically and serve over local HTTP")
    p.add_argument("--port", type=int)
    p.add_argument("--bind")
    p.add_argument("--path", help="URL path (default: settings.url_path)")
    p.add_argument("-v", "--verbose", action="store_true", help="log every request")

    p = sub.add_parser("publish", help="PUBLIC: push the calendar to a git repo/gist or a folder")
    p.add_argument("--watch", action="store_true", help="keep running, republish every refresh_minutes")

    sub.add_parser("secret", help="print a random, unguessable file name / URL path")

    p = sub.add_parser("service", help="background job for this machine: launchd / systemd / Windows")
    p.add_argument("--mode", choices=("serve", "publish"), default="serve")
    p.add_argument("--kind", choices=("launchd", "systemd", "windows"),
                   help="default: the one for this operating system")
    p.add_argument("--install", action="store_true", help="write the file (it is only printed otherwise)")
    p.add_argument("--python", help="interpreter to use in the job")
    return ap


def cmd_init(args) -> int:
    target = Path(args.config).expanduser() if args.config else config_home() / "config.toml"
    if target.exists() and not args.force:
        raise EdtError(f"{target} already exists (use --force to overwrite)")
    example = resources.files("tamis").joinpath("config.example.toml").read_text(encoding="utf-8")
    private_dir(target.parent)
    atomic_write(target, example.encode("utf-8"), mode=0o600)   # never briefly world-readable
    print(f"created {target}\nedit it, then run:  python -m tamis urls")
    return 0


def cmd_urls(cfg: Config) -> int:
    start, end = cfg.period()
    print(f"# period {start} -> {end}   config {cfg.path}")
    for src in cfg.sources:
        print(f"{src.get('name', '?')}\n  {source_location(cfg, src)}")
    return 0


def cmd_service(cfg: Config, args) -> int:
    from .service import check_location, install, render
    check_location(cfg)
    unit = render(cfg, args.mode, args.kind, args.python)
    if args.install:
        install(unit)
        log.info("wrote %s\nnow run:\n%s", unit.target, unit.howto)
    else:
        print(unit.text)
        log.info("# save as %s  (or re-run with --install), then:\n%s", unit.target, unit.howto)
    if args.mode == "publish" and not cfg.publish:
        log.warning("note: your config has no [publish] section yet")
    return 0


def run(args) -> int:
    cmd = args.cmd or "report"
    if cmd == "init":
        return cmd_init(args)
    if cmd == "secret":
        print(secrets.token_urlsafe(24) + ".ics")
        return 0

    found = find_config(args.config)
    cfg = Config(found)
    if (not args.config and not os.environ.get("TAMIS_CONFIG") and found.parent == Path.cwd().resolve()
            and found != (config_home() / "config.toml").resolve()
            and (cfg.settings.get("alert_command") or cfg.publish)):
        # a config can run commands and publish; one picked up from the current folder
        # (for example a cloned repo) deserves a second look
        log.warning("WARNING: using %s from the current folder; it can run `alert_command` and "
                    "publish. Check it, or pass -c to be explicit.", found)
    if cmd == "urls":
        return cmd_urls(cfg)
    if cmd == "titles":
        from .report import titles
        print(titles(cfg, load_sources(cfg)[0], args.unmatched))
        return 0
    if cmd == "report":
        from .pipeline import produce
        from .report import health_table, report
        b = produce(cfg)
        print(report(cfg, b.kept, b.conflicts))
        print("\n" + health_table(cfg, b.health))
        return 0
    if cmd == "status":
        from .pipeline import produce
        from .report import health_table
        b = produce(cfg, ttl_min=0)          # always a fresh download attempt
        print(health_table(cfg, b.health))
        if b.warnings and any(w.uid.startswith("stale") for w in b.warnings):
            print("\n" + b.warnings[0].summary)
        return 1 if any(s.error for s in b.health) else 0
    if cmd == "build":
        from .pipeline import produce, write_if_changed
        b = produce(cfg)
        out = cfg.resolve(args.out) if args.out else cfg.output_file
        changed = write_if_changed(out, b.text)
        log.info("%d events -> %s%s", len(b.kept), out, "" if changed else " (unchanged)")
        return 0
    if cmd == "serve":
        from .server import serve
        return serve(cfg, port=args.port, bind=args.bind, url_path=args.path, verbose=args.verbose)
    if cmd == "publish":
        from .publish import publish_job, publish_once
        if not args.watch:
            publish_once(cfg)
            return 0
        from .pipeline import every
        minutes = cfg.number("refresh_minutes", 120, minimum=10, maximum=7 * 24 * 60)
        log.info("publishing every %g min (method: %s)", minutes, cfg.publish.get("method"))
        try:
            every(minutes, publish_job(cfg.path, minutes), threading.Event())
        except KeyboardInterrupt:
            pass
        return 0
    if cmd == "service":
        return cmd_service(cfg, args)
    raise EdtError(f"unknown command {cmd}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # never crash on a console that can't print "é"
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = parser().parse_args(argv)
    setup_logging(args.log_file, timestamps=args.cmd in ("serve", "publish"))
    try:
        return run(args)
    except EdtError as exc:
        log.error("error: %s", exc)
        return 2

