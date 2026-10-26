#!/usr/bin/env python3
# Tests for the headless-run-cost action's jq filter and its step script. Run:
# python3 .github/actions/headless-run-cost/action.test.py
#
# What matters: the row keeps its stable shape (ts, what, repo, pr, model,
# total_cost_usd, usage, outcome, then the caller's fields); the outcome rule
# calls an is_error result "error" and a run with no result message, no file
# or a truncated file "no result"; the model falls back to the init message;
# and a bad input never fails the step. The step's script is extracted from
# action.yml and run with the GitHub env set, so the test runs what CI runs.
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

INIT = {"type": "system", "subtype": "init", "model": "claude-init-model"}
OK = {"type": "result", "subtype": "success", "is_error": False,
      "total_cost_usd": 0.35, "usage": {"input_tokens": 10}}


def script():
    text = (HERE / "action.yml").read_text()
    m = re.search(r"^      run: \|\n((?:        .*\n|\n)+)", text, re.M)
    return "\n".join(l[8:] for l in m.group(1).splitlines())


class Run:
    def __init__(self, test, exec_content=None, **env):
        self.dir = tempfile.mkdtemp()
        d = Path(self.dir)
        execp = d / "exec.json"
        if exec_content is not None:
            execp.write_text(exec_content if isinstance(exec_content, str) else json.dumps(exec_content))
        e = dict(os.environ, GITHUB_REPOSITORY="o/r", GITHUB_ACTION_PATH=str(HERE),
                 GITHUB_OUTPUT=str(d / "out"), GITHUB_STEP_SUMMARY=str(d / "sum"),
                 EXEC=str(execp), WHAT="w", MODEL="", PR="", FIELDS="{}",
                 FILE=str(d / "rows.jsonl"), SUMMARY="true")
        e.update(env)
        self.proc = subprocess.run(["bash", "-c", script()], env=e, capture_output=True, text=True)
        test.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        lines = (d / "rows.jsonl").read_text().splitlines()
        test.assertEqual(len(lines), 1)
        self.row = json.loads(lines[0])
        self.out = (d / "out").read_text()
        self.summary = (d / "sum").read_text() if (d / "sum").exists() else ""


class Test(unittest.TestCase):
    def test_success(self):
        r = Run(self, [INIT, OK], PR="42", FIELDS='{"head_sha":"abc","tier":"deep"}')
        self.assertEqual(list(r.row)[:8], ["ts", "what", "repo", "pr", "model", "total_cost_usd", "usage", "outcome"])
        self.assertEqual(r.row["pr"], 42)
        self.assertEqual(r.row["model"], "claude-init-model")
        self.assertEqual(r.row["total_cost_usd"], 0.35)
        self.assertEqual(r.row["outcome"], "success")
        self.assertEqual((r.row["head_sha"], r.row["tier"]), ("abc", "deep"))
        self.assertIn("outcome=success", r.out)
        self.assertIn("### Headless run cost: w", r.summary)

    def test_model_input_wins(self):
        self.assertEqual(Run(self, [INIT, OK], MODEL="m").row["model"], "m")

    def test_error_result(self):
        err = dict(OK, is_error=True)
        self.assertEqual(Run(self, [err]).row["outcome"], "error")
        self.assertEqual(Run(self, [dict(OK, subtype="error_max_turns", is_error=True)]).row["outcome"], "error_max_turns")

    def test_no_result_message(self):
        r = Run(self, [INIT])
        self.assertEqual(r.row["outcome"], "no result")
        self.assertIsNone(r.row["total_cost_usd"])
        self.assertIsNone(r.row["pr"])

    def test_missing_and_empty_file(self):
        self.assertEqual(Run(self, None).row["outcome"], "no result")
        self.assertEqual(Run(self, None, EXEC="").row["outcome"], "no result")

    def test_truncated_file(self):
        r = Run(self, json.dumps([INIT, OK])[:-9], MODEL="m", FIELDS='{"issue":7}')
        self.assertEqual(r.row["outcome"], "no result")
        self.assertEqual((r.row["model"], r.row["issue"]), ("m", 7))

    def test_filter_failure_keeps_fields_and_model(self):
        r = Run(self, [1, OK], MODEL="m", FIELDS='{"tier":"deep"}')
        self.assertEqual(r.row["outcome"], "no result")
        self.assertEqual((r.row["model"], r.row["tier"]), ("m", "deep"))

    def test_bad_fields_and_pr_degrade(self):
        r = Run(self, [OK], FIELDS="not json", PR="abc")
        self.assertEqual((r.row["outcome"], r.row["pr"]), ("success", None))
        self.assertIn("::warning::headless-run-cost: fields is not a JSON object", r.proc.stdout)
        self.assertIn("::warning::headless-run-cost: pr is not a number", r.proc.stdout)

    def test_empty_pr_is_silent(self):
        self.assertNotIn("::warning", Run(self, [OK], PR="").proc.stdout)

    def test_summary_off(self):
        self.assertEqual(Run(self, [OK], SUMMARY="false").summary, "")

    def test_appends(self):
        r = Run(self, [OK])
        rows = Path(r.dir, "rows.jsonl")
        before = rows.read_text()
        subprocess.run(["bash", "-c", script()], env=dict(
            os.environ, GITHUB_REPOSITORY="o/r", GITHUB_ACTION_PATH=str(HERE),
            GITHUB_OUTPUT=str(Path(r.dir, "out")), GITHUB_STEP_SUMMARY=str(Path(r.dir, "sum")),
            EXEC="", WHAT="w", MODEL="", PR="", FIELDS="{}", FILE=str(rows), SUMMARY="false"))
        self.assertTrue(rows.read_text().startswith(before))
        self.assertEqual(len(rows.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
