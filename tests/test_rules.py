"""Config regexes keep their meaning, and exams are never hidden by drop / skip_match."""

from __future__ import annotations

import re
import unittest
from zoneinfo import ZoneInfo

from tamis import EdtError
from tamis.config import Config
from tamis.ics import parse_ics
from tamis.rules import build, norm, norm_pattern, rx

from helpers import TempDir, make_config

BASE = """
[settings]
timezone = "Europe/Paris"
data_dir = "data"
[period]
start = "2026-09-01"
end = "2027-02-28"
[[sources]]
name = "A"
file = "{a}"
[[courses]]
label = "Stoch"
match = "calcul stochastique"
skip_match = ["\\\\btd\\\\b"]
[[courses]]
label = "Geo"
match = "geometrie"
[[drop]]
match = "reunion"
[[drop]]
match = "seminaire"
"""


def ics(*titles: str) -> str:
    body = "".join(
        f"BEGIN:VEVENT\nUID:u{i}\nDTSTART:202610{i + 1:02d}T080000Z\n"
        f"DTEND:202610{i + 1:02d}T090000Z\nSUMMARY:{t}\nEND:VEVENT\n"
        for i, t in enumerate(titles))
    return "BEGIN:VCALENDAR\n" + body + "END:VCALENDAR\n"


class PatternTests(unittest.TestCase):
    def test_escapes_keep_their_meaning(self):
        for pat in (r"\S", r"\D", r"\W", r"\B", r"\A", r"\Z", r"\d+", r"\bTD\b"):
            with self.subTest(pat=pat):
                for esc in re.findall(r"\\.", pat):
                    self.assertIn(esc, norm_pattern(pat))

    def test_uppercase_escapes_match_the_opposite_of_lowercase(self):
        self.assertIsNone(rx(r"^\S+$").search("two words"))
        self.assertTrue(rx(r"^\S+$").search("oneword"))
        self.assertTrue(rx(r"\D\d").search("td1"))
        self.assertTrue(rx(r"\Atd\Z").search("td"))
        self.assertIsNone(rx(r"\Atd\Z").search("td 2"))

    def test_text_part_is_still_accent_and_case_insensitive(self):
        self.assertTrue(rx("Contrôle   Optimal").search(norm("contrôle optimal S3")))
        self.assertTrue(rx(r"^C\+\+").search(norm("C++ TP")))
        self.assertTrue(rx("straße").search(norm("Straße")))

    def test_named_unicode_escape_is_preserved(self):
        self.assertTrue(rx(r"\N{LATIN SMALL LETTER E}").search("e"))

    def test_bad_regex_is_reported_not_crashed(self):
        with self.assertRaises(EdtError):
            rx("(")


class ExamProtectionTests(unittest.TestCase):
    def run_build(self, *titles):
        with TempDir() as tmp:
            cfg = Config(make_config(tmp, base=BASE))
            kept, _ = build(cfg, parse_ics(ics(*titles), "A", ZoneInfo("UTC")))
        return [e.summary for e in kept]

    def test_skip_match_removes_sessions_but_not_exams(self):
        got = self.run_build("Calcul stochastique TD", "Examen Calcul stochastique TD",
                             "Calcul stochastique cours")
        self.assertEqual(sorted(got), ["Calcul stochastique cours",
                                       "Examen Calcul stochastique TD"])

    def test_drop_removes_events_but_not_exams_of_kept_courses(self):
        got = self.run_build("Geometrie reunion", "Examen Geometrie reunion", "Geometrie seminaire")
        self.assertEqual(got, ["Examen Geometrie reunion"])

    def test_dropped_exam_of_an_unknown_course_is_still_left_out(self):
        self.assertEqual(self.run_build("Examen reunion de rentree"), [])


if __name__ == "__main__":
    unittest.main()
