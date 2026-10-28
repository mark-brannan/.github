#!/usr/bin/env python3
# Tests for coverage.py. Run: python3 .github/scripts/coverage.test.py
#
# What matters: only lines the change adds or alters, and of those only the
# ones the report counts as executable, make the headline; a line changed on
# the base branch after the fork is not counted; a changed file the report
# does not know is named, not scored; the base report moves the total; a
# Cobertura path under <source> matches its git path; the numbers fail the
# run only under --fail-under. lizard is a stub scoring one function per file.
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent / "coverage.py"

LIZARD = r'''#!/usr/bin/env python3
import sys
for f in sys.argv[2:]:
    n = sum(1 for _ in open(f))
    print('%d,3,7,0,%d,"f@1-%d@%s","%s","f","f()",1,%d' % (n, n, n, f, f, n))
'''


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def cobertura(source, hits, valid=None, covered=None):
    """A report on a.py with {line: hits}, under <source>."""
    lines = "".join('<line number="%d" hits="%d"/>' % kv for kv in sorted(hits.items()))
    totals = "" if valid is None else ' lines-valid="%d" lines-covered="%d"' % (valid, covered)
    return ('<?xml version="1.0"?><coverage%s><sources><source>%s</source></sources><packages>'
            '<package><classes><class filename="a.py"><lines>%s</lines></class></classes>'
            '</package></packages></coverage>' % (totals, source, lines))


class Coverage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.repo, self.bin = t / "repo", t / "bin"
        (self.repo / "src").mkdir(parents=True)
        self.bin.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        self.write("src/a.py", ["x = 1", "y = 2", "z = 3", "# note"])
        self.commit("base")
        git(self.repo, "switch", "-q", "-c", "pr")
        # Line 2 changed, 4 (a comment, not executable) changed, 5 added; a new file outside the report.
        self.write("src/a.py", ["x = 1", "y = 20", "z = 3", "# note!", "w = 5"])
        self.write("README.md", ["hello"])
        self.commit("pr")
        git(self.repo, "switch", "-q", "main")
        self.write("src/b.py", ["late = 1"])  # on the base after the fork
        self.commit("late")
        git(self.repo, "switch", "-q", "pr")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, lines):
        (self.repo / rel).write_text("\n".join(lines) + "\n")

    def commit(self, msg):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)

    def report(self, name, hits, **kw):
        p = Path(self.tmp.name) / name
        p.write_text(cobertura(str(self.repo / "src"), hits, **kw))
        return str(p)

    def run_tool(self, *args, lizard=False):
        if lizard:
            stub = self.bin / "lizard"
            stub.write_text(LIZARD)
            stub.chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (self.bin, os.path.dirname(subprocess.run(
            ["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip())))
        out = Path(self.tmp.name) / "out.json"
        p = subprocess.run([sys.executable, str(TOOL), "--base", "main", "--json", str(out), *args],
                           cwd=self.repo, capture_output=True, text=True, env=env)
        return p, json.loads(out.read_text()) if out.exists() else None

    def test_changed_executable_lines_only(self):
        p, rep = self.run_tool("--coverage", self.report("h.xml", {1: 1, 2: 0, 3: 1, 5: 1}))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(rep["changed"], {"executable": 2, "covered": 1, "pct": 50.0})
        self.assertEqual(rep["files"][0]["path"], "src/a.py")
        self.assertEqual(rep["files"][0]["missed"], [2])
        self.assertEqual(rep["changed_files"], 2)  # src/b.py, on the base after the fork, is not one
        self.assertEqual(rep["outside"], ["README.md"])
        self.assertIn("50% of changed lines (1 of 2)", p.stdout)
        self.assertIn("A measurement, not a gate.", p.stdout)
        self.assertEqual(rep["functions"], [])  # no lizard on PATH

    def test_base_report_moves_the_total(self):
        head = self.report("h.xml", {1: 1, 2: 1, 3: 1, 5: 0}, valid=4, covered=3)
        base = self.report("b.xml", {1: 1, 2: 0, 3: 0}, valid=3, covered=1)
        p, rep = self.run_tool("--coverage", head, "--base-coverage", base)
        self.assertEqual(rep["total"]["pct"], 75.0)
        self.assertIn("total 75.0% (+41.7 from 33.3%)", p.stdout)

    def test_fail_under_is_the_only_gate(self):
        head = self.report("h.xml", {2: 0, 5: 0})
        self.assertEqual(self.run_tool("--coverage", head)[0].returncode, 0)
        p, rep = self.run_tool("--coverage", head, "--fail-under", "80")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(rep["failed"])

    def test_touched_function_gets_crap(self):
        p, rep = self.run_tool("--coverage", self.report("h.xml", {1: 1, 2: 0, 3: 1, 5: 1}), lizard=True)
        f, = rep["functions"]
        self.assertEqual((f["path"], f["ccn"], f["pct"]), ("src/a.py", 3, 75.0))
        self.assertEqual(f["crap"], round(9 * 0.25 ** 3 + 3, 1))
        self.assertIn("Touched functions by CRAP", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
