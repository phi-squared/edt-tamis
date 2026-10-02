import datetime as dt
import unittest
from pathlib import Path

from tamis import EdtError
from tamis.ade import source_url
from tamis.config import Config, parse_day
from tamis.ics import fold, parse_ics, unfold
from tamis.pipeline import produce, write_if_changed
from tamis.rules import norm

from helpers import TempDir, make_config

TODAY = dt.date(2026, 9, 26)


class PeriodTests(unittest.TestCase):
    def test_absolute_and_relative(self):
        self.assertEqual(parse_day("2027-02-28", TODAY), dt.date(2027, 2, 28))
        self.assertEqual(parse_day(dt.date(2027, 1, 1), TODAY), dt.date(2027, 1, 1))
        self.assertEqual(parse_day("today", TODAY), TODAY)
        self.assertEqual(parse_day("-2w", TODAY), dt.date(2026, 9, 12))
        self.assertEqual(parse_day("+30d", TODAY), dt.date(2026, 10, 26))
        self.assertEqual(parse_day("today + 20w", TODAY), dt.date(2027, 2, 13))

    def test_garbage(self):
        with self.assertRaises(EdtError):
            parse_day("next tuesday", TODAY)


class UrlTests(unittest.TestCase):
    def cfg(self, tmp):
        return Config(make_config(tmp))

    def test_from_resources(self):
        with TempDir() as tmp:
            u = source_url(self.cfg(tmp), {"resources": [1, 2]}, dt.date(2026, 9, 1), dt.date(2027, 2, 28))
        self.assertIn("resources=1,2&projectId=7&calType=ical", u)
        self.assertTrue(u.endswith("firstDate=2026-09-01&lastDate=2027-02-28"))

    def test_pasted_url_window_replaced(self):
        pasted = "https://ade.example/x.jsp?resources=5&projectId=9&calType=ical&nbWeeks=4"
        with TempDir() as tmp:
            u = source_url(self.cfg(tmp), {"url": pasted}, dt.date(2026, 9, 1), dt.date(2026, 12, 1))
        self.assertNotIn("nbWeeks", u)
        self.assertIn("projectId=9", u)
        self.assertIn("firstDate=2026-09-01&lastDate=2026-12-01", u)


class IcsTests(unittest.TestCase):
    def test_fold_roundtrip(self):
        line = "DESCRIPTION:" + "é" * 80
        folded = fold(line)
        self.assertTrue(all(len(x.encode()) <= 75 for x in folded.split("\r\n")))
        self.assertEqual(unfold(folded), [line])

    def test_norm(self):
        self.assertEqual(norm("  Contrôle   Optimal "), "controle optimal")

    def test_parse_tzid(self):
        from zoneinfo import ZoneInfo
        text = "BEGIN:VEVENT\nUID:x\nDTSTART;TZID=Europe/Paris:20261002T090000\nSUMMARY:a\\, b\nEND:VEVENT\n"
        (e,) = parse_ics(text, "s", ZoneInfo("UTC"))
        self.assertEqual(e.start.utcoffset(), dt.timedelta(hours=2))
        self.assertEqual(e.summary, "a, b")


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, extra=""):
        with TempDir() as tmp:
            cfg = Config(make_config(tmp, extra))
            b = produce(cfg)
            return b.text, b.kept, b.conflicts

    def test_selection_and_rules(self):
        text, kept, conflicts = self.run_pipeline()
        by_uid = {e.uid: e for e in kept}
        # dropped, unmatched, and out-of-period events are gone
        self.assertNotIn("a6", by_uid)
        self.assertNotIn("a7", by_uid)
        self.assertNotIn("b2", by_uid)
        # de-duplication keeps the newer copy (room change)
        self.assertEqual(by_uid["a1"].location, "Room 9")
        # clash: lower priority greyed out, higher untouched
        self.assertTrue(by_uid["a2"].muted)
        self.assertIn("clashes with Stoch", by_uid["a2"].note)
        self.assertFalse(by_uid["a1"].muted)
        # attend = false greys out classes
        self.assertTrue(by_uid["a3"].muted)
        # exams are never greyed out, even when they clash
        self.assertFalse(by_uid["a4"].muted)
        self.assertFalse(by_uid["a5"].muted)
        self.assertTrue(any(a.is_exam and b.is_exam for a, b in conflicts))
        # output
        self.assertIn("SUMMARY:· Contrôle optimal S3", text)
        self.assertIn("TRANSP:TRANSPARENT", text)
        self.assertNotIn("Modifié le", text)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))

    def test_skip_slots_and_drop_mode(self):
        extra = ""
        with TempDir() as tmp:
            p = make_config(tmp)
            t = p.read_text(encoding="utf-8")
            t = t.replace('label    = "Stoch"', 'label    = "Stoch"\nskip_slots = ["Fri 13:30-15:30"]')
            t = t.replace('label    = "Control"', 'label    = "Control"\non_conflict = "drop"')
            p.write_text(t + extra, encoding="utf-8")
            kept = produce(Config(p)).kept
        by_uid = {e.uid: e for e in kept}
        self.assertTrue(by_uid["a1"].muted)          # skipped slot
        self.assertTrue(by_uid["a8"].muted)
        self.assertFalse(by_uid["a4"].muted)         # exam in any slot stays

    def test_output_is_deterministic(self):
        a, _, _ = self.run_pipeline()
        b, _, _ = self.run_pipeline()
        self.assertEqual(a, b)

    def test_write_if_changed_keeps_crlf(self):
        with TempDir() as tmp:
            f = Path(tmp) / "x.ics"
            self.assertTrue(write_if_changed(f, "A\r\nB\r\n"))
            self.assertFalse(write_if_changed(f, "A\r\nB\r\n"))
            self.assertEqual(f.read_bytes(), b"A\r\nB\r\n")


if __name__ == "__main__":
    unittest.main()
