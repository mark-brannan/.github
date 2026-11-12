#!/usr/bin/env python3
# Tests for conflict_latency.py. Run: python3 .github/scripts/conflict_latency.test.py
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("cl", Path(__file__).resolve().parent / "conflict_latency.py")
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)

NOTICE = ("pr=12 head=abc123 event=push cause=base caused_at=2026-11-11T10:00:00Z "
          "labelled_at=2026-11-11T10:00:27Z handed_at=2026-11-11T10:00:36Z label_delay=27 hand_delay=9")
RUN = {"id": 7, "html_url": "https://github.com/o/r/actions/runs/7"}


def fake_gh(jobs, notes):
    def gh_json(*args):
        path = args[-1]
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": jobs}
        if "/annotations" in path:
            assert notes is not None, "annotations read for a run with no hand-over"
            assert "/check-runs/1/" in path, "annotations must be read from the sweep job"
            return notes
        raise AssertionError(path)
    return gh_json


class Notice(unittest.TestCase):
    def test_the_sweeps_notice_parses_with_int_delays(self):
        rec = cl.parse_notice(NOTICE)
        self.assertEqual((rec["pr"], rec["cause"], rec["label_delay"], rec["hand_delay"]), ("12", "base", 27, 9))

    def test_a_notice_without_delays_is_dropped(self):
        self.assertIsNone(cl.parse_notice("pr=12 cause=base"))
        self.assertIsNone(cl.parse_notice("pr=12 label_delay=soon hand_delay=9"))


class Rows(unittest.TestCase):
    sweep = {"id": 1, "name": "sweep / sweep", "conclusion": "success"}

    def test_a_handed_over_run_yields_its_notices_only(self):
        jobs = [self.sweep, {"id": 2, "name": "sweep / fix (12) / fixer", "conclusion": "success"}]
        notes = [{"title": "conflict-latency", "message": NOTICE}, {"title": "other", "message": NOTICE}]
        with mock.patch.object(cl, "gh_json", fake_gh(jobs, notes)):
            rows = cl.rows_of("o/r", RUN)
        self.assertEqual([(r["repo"], r["pr"], r["label_delay"], r["run"]) for r in rows],
                         [("o/r", "12", 27, RUN["html_url"])])

    def test_a_run_whose_fix_was_skipped_reads_no_annotations(self):
        jobs = [self.sweep, {"id": 2, "name": "sweep / fix", "conclusion": "skipped"}]
        with mock.patch.object(cl, "gh_json", fake_gh(jobs, None)):
            self.assertEqual(cl.rows_of("o/r", RUN), [])


class Summary(unittest.TestCase):
    def test_p90_is_the_nearest_rank(self):
        self.assertEqual(cl.p90(list(range(1, 11))), 9)
        self.assertEqual(cl.p90([5]), 5)

    def test_no_rows_says_so(self):
        self.assertIn("No hand-overs", cl.markdown([], "2026-01-01"))


if __name__ == "__main__":
    unittest.main()
