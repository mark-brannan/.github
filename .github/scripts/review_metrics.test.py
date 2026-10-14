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
import importlib.util
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

    def test_skipped_run_spent_nothing(self):
        f = rm.parse_log(stamped("##[notice]review triage: skip (Dependabot pull request)\nall done"))
        self.assertIsNone(f["cost"])
        self.assertEqual((f["tier"], f["why"]), ("skip", "Dependabot pull request"))

    def test_pre_triage_log_has_no_tier(self):
        f = rm.parse_log(stamped('{\n  "type": "result",\n  "total_cost_usd": 0.1\n}'))
        self.assertEqual((f["tier"], f["cost"], f["paths"]), ("", 0.1, ""))


class Classify(unittest.TestCase):
    def test_classes(self):
        cases = [
            (thread("Judgment (risk): who may apply the label"), "MERGED", "judgment"),
            (thread("**Judgment (values):** x", resolved=True, outdated=True), "MERGED", "judgment"),
            (thread("off by one", resolved=True, outdated=True), "MERGED", "fixed"),
            (thread("off by one", [("solace", "Fixed in abc1234")], resolved=True), "MERGED", "fixed"),
            (thread("off by one", [("solace", "No: the bound is inclusive")], resolved=True), "MERGED", "declined"),
            (thread("off by one", [("solace", "Reversal: kept, thread open")], resolved=True), "MERGED", "declined"),
            (thread("off by one", [("solace", "Thanks")], resolved=True), "MERGED", "other"),
            (thread("off by one", [(rm.BOT, "still applies")], resolved=True), "MERGED", "other"),
            (thread("off by one", resolved=True), "MERGED", "other"),
            (thread("off by one", outdated=True), "MERGED", "other"),
            (thread("off by one", [("coderabbitai", "Agreed")], resolved=True), "MERGED", "other"),
            (thread("off by one"), "MERGED", "ignored"),
            (thread("off by one", [("coderabbitai", "Agreed")]), "MERGED", "ignored"),
            (thread("off by one", [("solace", "Later")]), "MERGED", "other"),
            (thread("off by one"), "CLOSED", "other"),
            (thread("off by one"), "OPEN", "open"),
        ]
        for t, state, want in cases:
            with self.subTest(first=t["comments"][0]["body"], want=want):
                self.assertEqual(rm.classify(t, state), want)


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


if __name__ == "__main__":
    unittest.main()
