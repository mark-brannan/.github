#!/usr/bin/env python3
# Tests for review_metrics.py. Run: python3 .github/scripts/review_metrics.test.py
#
# What matters: a job log yields the model from the init message, the cost,
# turns and wall time from the result message, and the tier from the triage
# notice and the path classes from the gate step's env, with the timestamps
# GitHub prefixes stripped; the triage script's own source and the PR body,
# both echoed into the log, are not mistaken for a decision; a log with
# no result message is a run that spent nothing; each bot thread lands in the
# class #70 defines; a thread is charged to the last paid run before it.
import contextlib
import csv
import datetime as dt
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("rm", Path(__file__).resolve().parent / "review_metrics.py")
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)


def stamped(body):
    return "\n".join("2026-10-09T06:33:29.%07dZ %s" % (i, ln) for i, ln in enumerate(body.splitlines()))


LOG = stamped('''##[group]Run set -euo pipefail
echo "::notice::review triage: $decision ($reason)"
  effort_arg="--effort $DEEP_EFFORT"
##[notice]review triage: deep (2 risky path(s): anything under .github/)
env:
  CLASSES: lock=0 design=0 docs=1 risky=2 code=0
  claude_args: --model "claude-opus-5-5" --effort high
Why: this PR may move work to `--effort max`.
review triage: skip (quoted in the body)
{
  "type": "system",
  "subtype": "init",
  "model": "claude-opus-5-5"
}
{
  "type": "result",
  "subtype": "success",
  "duration_ms": 19040,
  "num_turns": 7,
  "total_cost_usd": 0.2222,
  "modelUsage": {
    "claude-opus-5-5": {
      "contextWindow": 1000000
    }
  }
}''')


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def thread(first, replies=(), resolved=False, outdated=False):
    cs = [{"author": "claude", "body": first}] + [
        {"author": a, "body": b, "bot": a.endswith("ai")} for a, b in replies]
    return {"resolved": resolved, "outdated": outdated, "comments": cs}


class ParseLog(unittest.TestCase):
    def test_paid_run(self):
        f = rm.parse_log(LOG)
        self.assertEqual((f["model"], f["cost"], f["turns"], f["ms"]), ("claude-opus-5-5", 0.2222, 7, 19040))
        self.assertEqual((f["tier"], f["effort"], f["paths"]), ("deep", "high", "docs+risky"))
        self.assertTrue(f["why"].startswith("2 risky path(s)"))

    def test_checks_after_the_review_are_not_the_review(self):
        f = rm.parse_log(LOG + "\n" + stamped('''  claude_args: --model claude-sonnet-5-5 --effort medium
{
  "type": "system",
  "subtype": "init",
  "model": "claude-sonnet-5-5"
}
{
  "type": "result",
  "subtype": "success",
  "num_turns": 30,
  "total_cost_usd": 0.35
}'''))
        self.assertEqual((f["model"], f["cost"], f["turns"], f["effort"]), ("claude-opus-5-5", 0.2222, 7, "high"))

    def test_default_tier_review_takes_no_effort_from_a_later_check(self):
        f = rm.parse_log(stamped('''  claude_args: --model "claude-sonnet-5-5"
{
  "type": "result",
  "subtype": "success",
  "total_cost_usd": 0.23
}
  claude_args: --model claude-sonnet-5-5 --effort medium
{
  "type": "result",
  "subtype": "success",
  "total_cost_usd": 0.35
}'''))
        self.assertEqual((f["effort"], f["cost"]), ("", 0.23))

    def test_skipped_run_spent_nothing(self):
        f = rm.parse_log(stamped("##[notice]review triage: skip (Dependabot pull request)\nall done"))
        self.assertIsNone(f["cost"])
        self.assertEqual((f["tier"], f["why"]), ("skip", "Dependabot pull request"))

    def test_pre_triage_log_has_no_tier(self):
        f = rm.parse_log(stamped('{\n  "type": "result",\n  "total_cost_usd": 0.1\n}'))
        self.assertEqual((f["tier"], f["cost"], f["paths"]), ("", 0.1, ""))


class Classify(unittest.TestCase):
    def test_every_rule(self):
        cases = [
            (thread("Judgment (risk): who may apply the label"), "MERGED", ("judgment", "judgment", "Judgment (")),
            (thread("**Judgment (values):** x", resolved=True, outdated=True), "MERGED",
             ("judgment", "judgment", "Judgment (")),
            (thread("off by one", resolved=True, outdated=True), "MERGED", ("fixed", "outdated", "")),
            (thread("off by one", [("solace", "Fixed in abc1234")], resolved=True), "MERGED",
             ("fixed", "reply-says-fixed", "Fixed")),
            (thread("off by one", [("solace", "ok, in abc1234")], resolved=True), "MERGED",
             ("fixed", "reply-says-fixed", "in abc1234")),
            (thread("off by one", [("solace", "No: the bound is inclusive")], resolved=True), "MERGED",
             ("declined", "reply-disagrees", "No")),
            (thread("off by one", [("solace", "Reversal: kept, thread open")], resolved=True), "MERGED",
             ("declined", "reply-disagrees", "Reversal")),
            (thread("off by one", [("solace", "Thanks")], resolved=True), "MERGED", ("other", "silent", "")),
            (thread("off by one", [(rm.BOT, "still applies")], resolved=True), "MERGED", ("other", "silent", "")),
            (thread("off by one", resolved=True), "MERGED", ("other", "silent", "")),
            (thread("off by one", [("coderabbitai", "Agreed")], resolved=True), "MERGED", ("other", "silent", "")),
            (thread("off by one", outdated=True), "MERGED", ("other", "outdated", "")),
            (thread("off by one"), "MERGED", ("ignored", "unanswered-merged", "")),
            (thread("off by one", [("coderabbitai", "Agreed")]), "MERGED", ("ignored", "unanswered-merged", "")),
            (thread("off by one", [("solace", "Later")]), "MERGED", ("other", "answered-open", "")),
            (thread("off by one"), "CLOSED", ("other", "closed-unmerged", "")),
            (thread("off by one"), "OPEN", ("open", "open", "")),
        ]
        for t, state, want in cases:
            with self.subTest(first=t["comments"][0]["body"], state=state, want=want):
                self.assertEqual(tuple(rm.classify(t, state))[:3], want)

    def test_reply_is_the_deciding_one_else_the_last_human_one(self):
        t = thread("off by one", [("solace", "Looking"), ("solace", "No:  the bound\nis inclusive " + "x" * 300),
                                  ("solace", "Fixed")], resolved=True)
        v = rm.classify(t, "MERGED")
        self.assertEqual((v.rule, v.matched), ("reply-says-fixed", "Fixed"))
        self.assertEqual(v.reply, "Fixed")
        t = thread("off by one", [("solace", "No:  the bound\nis inclusive " + "x" * 300)], resolved=True)
        v = rm.classify(t, "MERGED")
        self.assertEqual(v.reply, ("No: the bound is inclusive " + "x" * 300)[:200])
        self.assertEqual(rm.classify(thread("x", [("solace", "Thanks")], resolved=True), "MERGED").reply, "Thanks")
        self.assertEqual(rm.classify(thread("Judgment (risk): x", [("solace", "Keep it")]), "MERGED").reply, "Keep it")


class Outputs(unittest.TestCase):
    def raw(self, n):
        out = []
        for i in range(n):
            t = thread("off by one %d" % i, [("solace", "Fixed in abc1234")] if i % 2 else (), resolved=bool(i % 2))
            t.update({"repo": "o/r", "pr": i, "pr_state": "MERGED", "path": "a.py", "url": "https://x/%d" % i,
                      "at": "2026-10-01T00:%02d:00Z" % i})
            out.append(t)
        return out

    def test_sample_spreads_across_rules_and_hides_the_class(self):
        rows = rm.sample_rows(self.raw(10), 4)
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(r["pr"] % 2 for r in rows), 2)  # both rules represented equally
        self.assertTrue(all(r["human_class"] == "" and r["url"] and r["at"] for r in rows))
        self.assertNotIn("class", rows[0])
        self.assertEqual(rows, rm.sample_rows(self.raw(10), 4))
        self.assertEqual(len(rm.sample_rows(self.raw(3), 50)), 3)

    def test_out_writes_the_dated_files_from_a_raw_file(self):
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / "raw.jsonl"
            t = {**self.raw(2)[1], "kind": "thread", "at": "2026-10-01T00:01:00Z", "run": 1, "model": "m",
                 "tier": "deep", "paths": "risky"}
            raw.write_text(json.dumps({"kind": "meta", "since": "2026-09-14"}) + "\n" + json.dumps(t) + "\n")
            out = Path(d) / "new" / "dir"
            subprocess.run([sys.executable, str(spec.origin), "--from", str(raw), "--out", str(out),
                            "--sample", "5"], capture_output=True, text=True, check=True)
            today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["%s-%s" % (today, k) for k in ("label-sheet.csv", "report.md", "runs.csv",
                                                             "threads.csv")])
            rows = read_csv(out / (today + "-threads.csv"))
            self.assertEqual((rows[0]["rule"], rows[0]["matched"], rows[0]["reply"], rows[0]["url"]),
                             ("reply-says-fixed", "Fixed", "Fixed in abc1234", "https://x/1"))
            sheet = read_csv(out / (today + "-label-sheet.csv"))
            self.assertEqual((len(sheet), sheet[0]["human_class"]), (1, ""))

    def test_sample_without_out_is_refused(self):
        p = subprocess.run([sys.executable, str(spec.origin), "--from", "/dev/null", "--sample", "3"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)

    def test_out_with_csv_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, str(spec.origin), "--from", "/dev/null", "--out", d,
                                "--csv", str(Path(d) / "runs.csv")], capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)
            self.assertEqual(list(Path(d).iterdir()), [])


class Charge(unittest.TestCase):
    def test_last_paid_run_before_the_thread(self):
        runs = [{"ran": True, "started": "2026-10-01T00:00:00Z", "run": 1},
                {"ran": False, "started": "2026-10-02T00:00:00Z", "run": 2},
                {"ran": True, "started": "2026-10-03T00:00:00Z", "run": 3},
                {"ran": True, "started": "2026-10-05T00:00:00Z", "run": 4}]
        self.assertEqual(rm.charge(runs, {"at": "2026-10-04T00:00:00Z"})["run"], 3)
        self.assertIsNone(rm.charge(runs, {"at": "2026-09-30T00:00:00Z"}))


class Markdown(unittest.TestCase):
    def test_a_run_counts_in_each_path_class(self):
        run = {"repo": "o/a", "pr": 1, "ran": True, "cost": 0.2, "model": "m", "tier": "deep", "paths": "docs+risky"}
        md = rm.markdown([run], [], "2026-09-13")
        self.assertIn("| docs | 1 | $0.20 |", md)
        self.assertIn("| risky | 1 | $0.20 |", md)


class Replay(unittest.TestCase):
    def test_from_a_raw_file_reclasses_with_no_network(self):
        run = {"kind": "run", "repo": "o/r", "pr": 7, "run": 1, "job": "review", "conclusion": "success",
               "started": "2026-10-01T00:00:00Z", "ended": "", "ran": True, "model": "claude-opus-5-5",
               "effort": "high", "tier": "deep", "why": "x", "paths": "risky", "cost": 2.0, "turns": 9, "ms": 1}
        t = thread("off by one", [("solace", "Fixed in abc1234")], resolved=True)
        t.update({"kind": "thread", "repo": "o/r", "pr": 7, "pr_state": "MERGED", "path": "a.py",
                  "at": "2026-10-01T00:01:00Z", "run": 1, "model": "claude-opus-5-5", "tier": "deep",
                  "paths": "risky"})
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for ln in ({"kind": "meta", "since": "2026-09-14"}, run, t):
                f.write(json.dumps(ln) + "\n")
        out = subprocess.run([sys.executable, str(spec.origin), "--from", f.name, "--repos", "never/fetched"],
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("since 2026-09-14", out)
        self.assertIn("1 bot threads: 1 fixed.", out)
        self.assertIn("| deep | 1 | $2.00 | $2.00 | 1 | 1 | 0 | 0 | 0 | $2.00 |", out)


class Fetch(unittest.TestCase):
    """The network is faked: gh_json answers from a table of runs by creation time."""
    T0 = dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.saved = rm.gh_json, rm.rows_of, rm.remaining, rm.threads, rm.PACE
        self.calls = []
        # 2,500 runs, one a minute: a single listing would stop at CAP.
        self.runs = [{"id": i, "created_at": rm.stamp(self.T0 + dt.timedelta(minutes=i)), "pull_requests": [],
                      "head_branch": "b%d" % (i % 3), "head_repository": {"owner": {"login": "o"}}}
                     for i in range(2500)]

        def gh_json(*args, soft=False):
            q = args[-1]
            self.calls.append(q)
            if "/pulls?" in q:
                return [{"number": 7, "head": {"label": "o:b1"}, "created_at": "2026-09-01T00:00:00Z",
                         "updated_at": "2026-10-01T00:00:00Z"}]
            lo, hi = q.split("created=")[1].split("..")
            hits = [r for r in self.runs if lo <= r["created_at"] <= hi]
            if "per_page=1&" in q:
                return {"total_count": len(hits)}
            return [{"workflow_runs": hits[:rm.CAP]}]
        rm.gh_json = gh_json
        rm.remaining = lambda path: (5000, 0)
        rm.threads = lambda repo, pr: ("MERGED", [])
        rm.PACE = None

    def tearDown(self):
        rm.gh_json, rm.rows_of, rm.remaining, rm.threads, rm.PACE = self.saved

    def test_a_window_past_the_cap_is_halved_until_it_lists_whole(self):
        got = rm.runs_of("o/r", "w.yml", self.T0, self.T0 + dt.timedelta(days=2))
        self.assertEqual([r["id"] for r in got], list(range(2500)))

    def test_pr_comes_from_one_listing_by_branch(self):
        index = rm.pr_index("o/r", self.T0)
        self.assertEqual(rm.pr_of("o/r", self.runs[1], index), 7)
        self.assertEqual(sum("/pulls?" in q for q in self.calls), 1)

    def test_resume_reads_each_run_once(self):
        seen = []
        rm.rows_of = lambda repo, run, index=None: seen.append(run["id"]) or [
            {"repo": repo, "pr": 7, "run": run["id"], "ran": False, "started": run["created_at"]}]
        self.runs = self.runs[:5]
        with tempfile.TemporaryDirectory() as d:
            keep = rm.Progress(d)
            keep.add("runs.jsonl", {"run": 0, "rows": []})
            lo, hi = self.T0, self.T0 + dt.timedelta(days=1)
            runs, _ = rm.fetch(["o/r"], "w.yml", lo, hi, False, keep)
            self.assertEqual(seen, [1, 2, 3, 4])
            self.assertEqual(len(runs), 4)
            rm.fetch(["o/r"], "w.yml", lo, hi, False, keep)
            self.assertEqual(seen, [1, 2, 3, 4])

    def test_over_half_the_budget_refuses_unless_paced(self):
        rm.rows_of = lambda repo, run, index=None: []
        lo, hi = self.T0, self.T0 + dt.timedelta(days=2)
        rm.remaining = lambda path: (1000, 0)
        with self.assertRaises(SystemExit):
            rm.fetch(["o/r"], "w.yml", lo, hi, False)
        rm.PACE = rm.Pace(10 ** 9)
        rm.fetch(["o/r"], "w.yml", lo, hi, False)

    def test_a_repo_without_the_workflow_lists_no_runs(self):
        rm.gh_json = lambda *args, soft=False: None
        self.assertEqual(rm.runs_of("o/r", "w.yml", self.T0, self.T0 + dt.timedelta(days=1)), [])


class Pin(unittest.TestCase):
    """--resume DIR pins the window only once it is good, and a rerun
    without --workflow keeps the pinned one."""

    def run_main(self, *args):
        got, saved = [], (rm.fetch, sys.argv)
        rm.fetch = lambda repos, workflow, lo, hi, force, keep=None: got.append(workflow) or ([], [])
        sys.argv = ["review_metrics.py", "--repos", "r", *args]
        try:
            rm.main()
        finally:
            rm.fetch, sys.argv = saved
        return got

    def test_a_bad_window_is_not_pinned(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in (["--since", "2026-10-14", "--until", "2026-09-14"], ["--since", "2026-09-14T00:00"]):
                with self.assertRaises(SystemExit):
                    self.run_main("--resume", d, *bad)
                self.assertFalse((Path(d) / "meta.json").exists())

    def test_a_rerun_keeps_the_pinned_workflow(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            self.run_main("--resume", d, "--since", "2026-09-14", "--workflow", "other.yml")
            self.assertEqual(self.run_main("--resume", d), ["other.yml"])


class PaceTest(unittest.TestCase):
    def test_spreads_calls_and_sleeps_below_the_floor(self):
        clock, slept = [0.0], []
        saved = rm.time.monotonic, rm.time.sleep, rm.remaining, rm.Pace.sleep_to
        rm.time.monotonic = lambda: clock[0]
        rm.time.sleep = lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s))
        rm.remaining = lambda path: (rm.FLOOR - 1, 99)
        floors = []
        rm.Pace.sleep_to = staticmethod(lambda epoch, why: floors.append(epoch))
        try:
            p = rm.Pace(3600, probe="x")
            for _ in range(rm.Pace.CHECK):
                p.wait()
            self.assertEqual(slept, [1.0] * (rm.Pace.CHECK - 1))
            self.assertEqual(floors, [99])
        finally:
            rm.time.monotonic, rm.time.sleep, rm.remaining, rm.Pace.sleep_to = saved


if __name__ == "__main__":
    unittest.main()
