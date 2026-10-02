from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

BASE = """
[settings]
calendar_name = "Test"
timezone      = "Europe/Paris"
project_id    = 7
data_dir      = "data"
refresh_minutes = 120

[period]
start = "2026-09-01"
end   = "2027-02-28"

[[sources]]
name = "A"
file = "{a}"

[[sources]]
name = "B"
file = "{b}"

[[courses]]
label    = "Stoch"
match    = "calcul stochastique"
priority = 90

[[courses]]
label    = "Geo"
match    = "geometrie differentielle"
priority = 80

[[courses]]
label    = "Control"
match    = "controle optimal"
priority = 30

[[courses]]
label    = "C++"
match    = "^c\\\\+\\\\+"
priority = 10
attend   = false

[[drop]]
match = "reunion d'information"
"""


def make_config(tmp: Path, extra: str = "", base: str = BASE) -> Path:
    text = base.format(a=(FIXTURES / "a.ics").as_posix(), b=(FIXTURES / "b.ics").as_posix())
    p = tmp / "config.toml"
    p.write_text(textwrap.dedent(text) + "\n" + textwrap.dedent(extra), encoding="utf-8")
    return p


class TempDir:
    def __enter__(self) -> Path:
        self._td = tempfile.TemporaryDirectory()
        return Path(self._td.name)

    def __exit__(self, *exc):
        self._td.cleanup()
