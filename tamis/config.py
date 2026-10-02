"""Loading the TOML config and computing the observation period."""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import EdtError
from .paths import WINDOWS, config_home, data_home


def default_config_candidates() -> list[Path]:
    return [Path.cwd() / "config.toml", config_home() / "config.toml"]


def find_config(explicit: str | None) -> Path:
    if explicit:
        candidates = [Path(explicit)]
    elif os.environ.get("TAMIS_CONFIG"):
        candidates = [Path(os.environ["TAMIS_CONFIG"])]
    else:
        candidates = default_config_candidates()
    for c in candidates:
        c = c.expanduser()
        if c.is_file():
            return c.resolve()
    tried = ", ".join(str(c) for c in candidates)
    raise EdtError(f"no config found (tried {tried}). Run `python -m tamis init` first.")


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        hint = " On Windows, run `python -m pip install tzdata`." if WINDOWS else ""
        raise EdtError(f"unknown time zone {name!r}.{hint}") from None


class Config:
    """Parsed TOML plus the directory it lives in (relative paths resolve against it)."""

    def __init__(self, path: Path, data: dict | None = None):
        self.path = Path(path)
        if data is None:
            try:
                data = tomllib.loads(self.path.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise EdtError(f"{self.path}: {exc}") from None
        self.data = data
        for name in ("settings", "publish", "period"):
            if not isinstance(data.get(name, {}), dict):
                raise EdtError(f"[{name}] must be a table, e.g. `[{name}]` on its own line")
        self.settings: dict = data.get("settings", {})
        self.publish: dict = data.get("publish", {})
        tzname = self.settings.get("timezone", "Europe/Paris")
        if not isinstance(tzname, str):
            raise EdtError(f"settings.timezone must be text such as \"Europe/Paris\", not {tzname!r}")
        self.tz = load_timezone(tzname)

    def number(self, key: str, default: float, *, minimum: float | None = None,
               maximum: float | None = None) -> float:
        """A numeric setting; a typo gives a clear error instead of a traceback."""
        v = self.settings.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
            raise EdtError(f"settings.{key} must be a number, not {v!r}")
        if (minimum is not None and v < minimum) or (maximum is not None and v > maximum):
            lo = "" if minimum is None else f" at least {minimum:g}"
            hi = "" if maximum is None else f" at most {maximum:g}"
            raise EdtError(f"settings.{key} must be{lo}{' and' if lo and hi else ''}{hi}, not {v!r}")
        return v

    def resolve(self, p: str) -> Path:
        q = Path(os.path.expandvars(os.path.expanduser(str(p))))
        return q if q.is_absolute() else (self.path.parent / q)

    @property
    def sources(self) -> list[dict]:
        return self.data.get("sources", [])

    @property
    def data_dir(self) -> Path:
        d = self.settings.get("data_dir") or self.settings.get("cache_dir")  # old name accepted
        return self.resolve(d) if d else data_home()

    @property
    def output_file(self) -> Path:
        o = self.settings.get("output_file")
        return self.resolve(o) if o else self.data_dir / "calendar.ics"

    def period(self, today: dt.date | None = None) -> tuple[dt.date, dt.date]:
        p = self.data.get("period", {})
        today = today or dt.datetime.now(self.tz).date()
        start = parse_day(p.get("start", "today-1w"), today)
        end = parse_day(p.get("end", "today+20w"), today)
        if end < start:
            raise EdtError(f"period: end {end} is before start {start}")
        return start, end


def load_complete(path: Path) -> Config:
    """Read the config for a reload: a file caught half-written (empty, truncated) is
    refused instead of silently switching the password off or emptying the calendar."""
    cfg = Config(path)
    if not cfg.sources or "settings" not in cfg.data:
        raise EdtError(f"{path} looks empty or incomplete (no [settings] or no [[sources]]); "
                       "keeping the previous settings")
    return cfg


_REL = re.compile(r"^(today)?([+-]\d+[dw])?$", re.I)


def parse_day(value, today: dt.date) -> dt.date:
    """Accepts '2026-09-01', a TOML date, 'today', '-2w', '+30d', 'today+20w'."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        pass
    m = _REL.match(s.replace(" ", ""))
    if not m or not (m.group(1) or m.group(2)):
        raise EdtError(f"period: cannot understand {value!r} "
                       "(use YYYY-MM-DD, 'today', '-2w', '+30d' or 'today+20w')")
    if not m.group(2):
        return today
    n = int(m.group(2)[:-1])
    unit = m.group(2)[-1].lower()
    try:
        return today + (dt.timedelta(weeks=n) if unit == "w" else dt.timedelta(days=n))
    except OverflowError:
        raise EdtError(f"period: {value!r} is out of range") from None
