"""Per-platform locations. Nothing here is specific to one user or machine."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"


def _env_path(var: str, fallback: Path) -> Path:
    v = os.environ.get(var)
    return Path(v) if v else fallback


def config_home() -> Path:
    if WINDOWS:
        return _env_path("APPDATA", Path.home() / "AppData" / "Roaming") / APP
    return _env_path("XDG_CONFIG_HOME", Path.home() / ".config") / APP


def data_home() -> Path:
    """Last good downloads + the built calendar. Deliberately NOT a cache folder:
    macOS/Windows may purge caches, and these copies are the fallback when ADE is down."""
    if WINDOWS:
        return _env_path("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP / "Data"
    if MACOS:
        return Path.home() / "Library" / "Application Support" / APP
    return _env_path("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP / "data"


def log_home() -> Path:
    if WINDOWS:
        return _env_path("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP / "Logs"
    if MACOS:
        return Path.home() / "Library" / "Logs" / APP
    return _env_path("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP
