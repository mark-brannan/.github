#!/usr/bin/env python3
# Tests for complexity.py. Run: python3 .github/scripts/complexity.test.py
#
# What matters: each changed file reaches the tool for its language, by
# extension or, with none, by shebang, and a file no tool reads reaches
# neither; a function's change is measured from the merge base, so a commit
# on the base branch after the fork is not counted; added and deleted files
# count from and to nothing; an unchanged function is not a row; a missing
# tool is reported, not silent, and is exit 2 only with --require-tools; the
# numbers never fail the run. lizard and shellmetrics are stubs that log the
# files they get and score one function per file: one plus its "if" count.
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent / "complexity.py"

LIZARD = r'''#!/usr/bin/env python3
import os, sys
files = [a for a in sys.argv[1:] if not a.startswith("-")]
open(os.environ["STUB_LOG"], "a").write("lizard " + " ".join(files) + "\n")
for f in files:
    n = 1 + open(f).read().count("if ")
    print('1,%d,7,2,1,"f@1-1@%s","%s","f","f()",1,1,%d' % (n, f, f, n - 1))
'''

RCA = r'''#!/usr/bin/env python3
import json, os, sys
f = sys.argv[-1]
open(os.environ["STUB_LOG"], "a").write("rca " + f + "\n")
n = 1 + open(f).read().count("if ")
m = {"cognitive": {"sum": 10.0 * n}, "mi": {"mi_original": 90.0},
     "halstead": {k: 1.0 for k in ("n1", "N1", "n2", "N2", "length", "vocabulary", "volume", "difficulty", "effort", "bugs")}}
print(json.dumps({"kind": "unit", "start_line": 1, "spaces": [{"kind": "function", "name": "f", "start_line": 1, "metrics": m, "spaces": []}]}))
'''

SHELLMETRICS = r'''#!/usr/bin/env python3
import os, sys
files = [a for a in sys.argv[1:] if a != "--csv"]
open(os.environ["STUB_LOG"], "a").write("shellmetrics " + " ".join(files) + "\n")
print("file,func,lineno,lloc,ccn,lines,comment,blank")
for f in files:
    n = 1 + open(f).read().count("if ")
    print('"%s","<begin>",0,0,0,1,0,0' % f)
    print('"%s","<main>",0,1,%d,0,0,0' % (f, n))
'''


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Complexity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.repo, self.bin, self.log = t / "repo", t / "bin", t / "log"
        self.bin.mkdir()
        for name, body in (("lizard", LIZARD), ("shellmetrics", SHELLMETRICS), ("rca", RCA)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        r = self.repo
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        git(r, "config", "user.email", "t@t")
        git(r, "config", "user.name", "t")
        self.write({"a.py": "if x:\n", "same.py": "if x:\n", "gone.sh": "if a; then b; fi\n",
                    "tool": "#!/usr/bin/env python3\n", "run": "#!/bin/bash\n", "notes.md": "if words\n"})
        git(r, "add", ".")
        git(r, "commit", "-qm", "base")
        git(r, "checkout", "-qb", "topic")
        self.write({"a.py": "if x:\nif y:\nif z:\n", "new.ts": "if (a) {}\n", "same.py": "if x:\n# note\n",
                    "tool": "#!/usr/bin/env python3\nif a:\n", "run": "#!/bin/bash\nif a; then b; fi\n",
                    "notes.md": "if more words\n"})
        (r / "gone.sh").unlink()
        git(r, "add", "-A")
        git(r, "commit", "-qm", "topic")
        # On main after the fork: not this branch's change, so not counted.
        git(r, "checkout", "-q", "main")
        self.write({"a.py": "if x:\nif main:\nif only:\nif here:\nif four:\n"})
        git(r, "commit", "-qam", "main moves on")
        git(r, "checkout", "-q", "topic")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, files):
        for name, body in files.items():
            (self.repo / name).write_text(body)

    def run_tool(self, *args, tools=("lizard", "shellmetrics", "rca")):
        env = {k: v for k, v in os.environ.items() if k not in ("LIZARD", "SHELLMETRICS", "RCA")}
        env["STUB_LOG"] = str(self.log)
        env["PATH"] = "/usr/bin:/bin"
        for name in tools:
            env[name.upper()] = str(self.bin / name)
        j = Path(self.tmp.name) / "out.json"
        p = subprocess.run([sys.executable, str(TOOL), "--base", "main", "--json", str(j), *args],
                           cwd=self.repo, capture_output=True, text=True, env=env)
        report = json.loads(j.read_text()) if j.exists() else None
        return p, report

    def test_rows_from_the_merge_base(self):
        p, report = self.run_tool()
        self.assertEqual(p.returncode, 0, p.stderr)
        got = {(r["path"], r["function"]): (r["before"], r["after"]) for r in report["functions"]}
        self.assertEqual(got, {
            ("a.py", "f"): (2, 4),            # from the fork's 2, not main's 6
            ("new.ts", "f"): (None, 2),
            ("gone.sh", "(top level)"): (2, None),
            ("tool", "f"): (1, 2),
            ("run", "(top level)"): (1, 2),
        })
        self.assertEqual(report["delta"], 2 + 2 - 2 + 1 + 1)

    def test_every_metric_is_logged_but_the_table_shows_two(self):
        p, report = self.run_tool()
        row = next(r for r in report["functions"] if r["path"] == "a.py")
        self.assertEqual(row["cognitive"], {"before": 20, "after": 40})
        after = row["metrics"]["after"]
        self.assertEqual((after["nloc"], after["tokens"], after["params"], after["length"], after["nesting"]),
                         (1, 7, 2, 1, 3))
        self.assertEqual(after["mi"]["mi_original"], 90.0)
        self.assertIn("volume", after["halstead"])
        self.assertIn("| File | Function | Cyclomatic | Δ | Cognitive |", p.stdout)
        self.assertIn("| `a.py:%d` | `f` | 2 → 4 | +2 | 20 → 40 |" % row["line"], p.stdout)
        self.assertIn("\nMeasured 6 changed files, %s against %s. A measurement, not a gate.\n" % (
            report["head"][:7], report["base"][:7]), p.stdout)
        head, _, rest = p.stdout.partition("<details>")
        self.assertNotIn("halstead", head.lower())
        self.assertIn("<summary>All metrics</summary>", rest)
        self.assertIn("| `a.py` · `f` | halstead.volume | 1.0 | 1.0 |", rest)
        self.assertIn("| `new.ts` · `f` | nesting | — | 1 |", rest)

    def test_each_file_reaches_its_tool(self):
        self.run_tool()
        sent = {}
        for line in self.log.read_text().splitlines():
            name, *files = line.split()
            sent.setdefault(name, set()).update(f.removeprefix("./") for f in files)
        self.assertEqual(sent["lizard"], {"a.py", "same.py", "new.ts", "tool.py"})
        self.assertEqual(sent["shellmetrics"], {"gone.sh", "run.sh"})

    def test_a_dash_named_file_is_a_path_not_an_option(self):
        self.write({"-o.py": "if a:\n"})
        git(self.repo, "add", "--", "-o.py")
        git(self.repo, "commit", "-qm", "dash")
        p, report = self.run_tool()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(("-o.py", 2), {(r["path"], r["after"]) for r in report["functions"]})

    def test_a_missing_tool_is_reported(self):
        p, report = self.run_tool(tools=("lizard",))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(report["unmeasured"], {"shellmetrics": 2, "rust-code-analysis": 4})
        p, _ = self.run_tool("--require-tools", tools=("lizard",))
        self.assertEqual(p.returncode, 2)


    def test_odd_names_route_and_a_submodule_is_skipped(self):
        self.write({"UP.SH": "if a; then b; fi\n", "envs": "#!/usr/bin/env -S LC_ALL=C python3 -u\nif a:\n"})
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        git(self.repo, "update-index", "--add", "--cacheinfo", "160000,%s,sub" % sha)
        git(self.repo, "add", "--", "UP.SH", "envs")
        git(self.repo, "commit", "-qm", "odd")
        self.log.unlink(missing_ok=True)
        p, report = self.run_tool()
        self.assertEqual(p.returncode, 0, p.stderr)
        got = {(r["path"], r["after"]) for r in report["functions"]}
        self.assertIn(("UP.SH", 2), got)
        self.assertIn(("envs", 2), got)
        sent = {}
        for line in self.log.read_text().splitlines():
            name, *files = line.split()
            sent.setdefault(name, set()).update(files)
        self.assertIn("./UP.SH.sh", sent["shellmetrics"])

if __name__ == "__main__":
    unittest.main()
