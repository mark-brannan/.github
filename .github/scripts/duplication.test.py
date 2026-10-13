#!/usr/bin/env python3
# Tests for duplication.py. Run: python3 .github/scripts/duplication.test.py
#
# What matters: both sides are scanned whole, from the merge base, so a commit
# on the base branch after the fork is not counted; a clone is new only when
# its pair of copies was not a clone at the base, so moving an old clone is
# quiet; a literal is new only in a file that did not hold it, and only once
# two files hold it; an apostrophe in prose is not a string, and Markdown is
# not read; an extensionless script reaches jscpd by its shebang; a missing
# jscpd is reported, not silent, and is exit 2 only with --require-tools; the
# numbers never fail the run. jscpd is a stub that logs the files it sees and
# reports a five-line clone for every two files whose first "DUP" line match.
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent / "duplication.py"

JSCPD = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
out = a[a.index("--output") + 1]
seen, groups = [], {}
for d, _, fs in os.walk("."):
    for f in sorted(fs):
        p = os.path.join(d, f)[2:]
        seen.append(p)
        lines = open(p).read().split("\n")
        for i, l in enumerate(lines):
            if l.startswith("DUP"):
                groups.setdefault(l, []).append((p, i + 1))
                break
open(os.environ["STUB_LOG"], "a").write("jscpd " + " ".join(sorted(seen)) + "\n")
dups = []
for g in groups.values():
    for p, s in g[1:]:
        side = lambda f, n: {"name": f, "start": n, "end": n + 4}
        dups.append({"format": "python", "lines": 5, "firstFile": side(*g[0]), "secondFile": side(p, s)})
os.makedirs(out, exist_ok=True)
json.dump({"duplicates": dups, "statistics": {"total": {"clones": len(dups), "duplicatedLines": 5 * len(dups),
           "percentage": 1.0, "sources": len(seen)}}}, open(os.path.join(out, "jscpd-report.json"), "w"))
'''

BLOCK = "DUP one\nx = 1\ny = 2\nz = 3\nw = 4\n"
REGEX = 'P = re.compile(r"(?:^|/)(?:git|yadm)\\Z")\n'
OLD = 'M = "an old message repeated in two files"\n'


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Duplication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.repo, self.bin, self.log = t / "repo", t / "bin", t / "log"
        self.bin.mkdir()
        (self.bin / "jscpd").write_text(JSCPD)
        (self.bin / "jscpd").chmod(0o755)
        r = self.repo
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        git(r, "config", "user.email", "t@t")
        git(r, "config", "user.name", "t")
        git(r, "config", "commit.gpgsign", "false")
        self.write({"a.py": BLOCK + REGEX + OLD, "b.py": "\n" + BLOCK, "c.py": OLD,
                    "notes.md": REGEX, "prose.py": "# the user's note and the agent's note, side by side\n"})
        git(r, "add", ".")
        git(r, "commit", "-qm", "base")
        git(r, "checkout", "-qb", "topic")
        # b.py's clone moves down a line (old); new.py copies the block and the
        # regex (new); tool, extensionless, is Python by its shebang.
        self.write({"b.py": "\n\n" + BLOCK, "new.py": BLOCK + REGEX, "tool": "#!/usr/bin/env python3\n",
                    "prose2.py": "# the user's note and the agent's note, side by side\n"})
        git(r, "add", ".")
        git(r, "commit", "-qm", "topic")
        # Lands on main after the fork: the merge base keeps it out.
        git(r, "checkout", "-q", "main")
        self.write({"late.py": BLOCK})
        git(r, "add", ".")
        git(r, "commit", "-qm", "late")
        git(r, "checkout", "-q", "topic")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, files):
        for name, body in files.items():
            (self.repo / name).write_text(body)

    def run_tool(self, *args, jscpd=True):
        env = dict(os.environ, STUB_LOG=str(self.log), PATH="/usr/bin:/bin")
        env.pop("JSCPD", None)
        if jscpd:
            env["JSCPD"] = str(self.bin / "jscpd")
        out = Path(self.tmp.name) / "report.json"
        p = subprocess.run([sys.executable, str(TOOL), "--base", "main", "--json", str(out), *args],
                           cwd=self.repo, env=env, capture_output=True, text=True)
        return p, (json.loads(out.read_text()) if out.exists() else None)

    def test_new_clone_and_moved_clone(self):
        p, rep = self.run_tool()
        self.assertEqual(p.returncode, 0, p.stderr)
        pairs = [sorted(c["path"] for c in n["copies"]) for n in rep["new_clones"]]
        self.assertEqual(pairs, [["a.py", "new.py"]])
        self.assertIn("1 new clone", p.stdout)

    def test_new_literal_only_where_new(self):
        _, rep = self.run_tool()
        lits = {r["literal"]: r for r in rep["new_literals"]}
        self.assertEqual(list(lits), ["(?:^|/)(?:git|yadm)\\Z"])
        self.assertEqual(lits["(?:^|/)(?:git|yadm)\\Z"]["added"], ["new.py"])
        self.assertEqual(rep["base_totals"]["literals"], 1)  # OLD; prose and notes.md are not strings

    def test_pipe_is_escaped_in_the_table(self):
        p, _ = self.run_tool()
        self.assertIn("`(?:^\\|/)(?:git\\|yadm)\\Z`", p.stdout)

    def test_whole_tree_from_merge_base(self):
        self.run_tool()
        base, head = self.log.read_text().splitlines()
        self.assertNotIn("late.py", base + head)
        self.assertNotIn("notes.md", base + head)
        self.assertIn("c.py", head)  # unchanged, still scanned
        self.assertIn("tool.py", head)

    def test_script_beside_its_suffixed_name(self):
        # git archive lists `run` before `run.py`; the real run.py keeps its name.
        self.write({"run": "#!/usr/bin/env python3\nDUP run\n", "run.py": "DUP real\n", "real2.py": "DUP real\n"})
        git(self.repo, "add", "run", "run.py", "real2.py")
        git(self.repo, "commit", "-qm", "run")
        _, rep = self.run_tool()
        pairs = [sorted(c["path"] for c in n["copies"]) for n in rep["new_clones"]]
        self.assertIn(["real2.py", "run.py"], pairs)
        self.assertIn(" run ", self.log.read_text().splitlines()[-1] + " ")

    def test_minified_is_not_read(self):
        lit = '"a long string that a bundle repeats"\n'
        self.write({"x.min.js": lit, "y.min.js": lit})
        git(self.repo, "add", "x.min.js", "y.min.js")
        git(self.repo, "commit", "-qm", "min")
        _, rep = self.run_tool()
        self.assertNotIn("a long string that a bundle repeats", {r["literal"] for r in rep["new_literals"]})

    def test_missing_jscpd(self):
        p, rep = self.run_tool(jscpd=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("jscpd not found", p.stdout)
        self.assertEqual(len(rep["new_literals"]), 1)
        p, _ = self.run_tool("--require-tools", jscpd=False)
        self.assertEqual(p.returncode, 2)

    def test_nothing_new(self):
        git(self.repo, "checkout", "-qb", "quiet", "main~1")
        self.write({"b.py": "\n\n" + BLOCK})
        git(self.repo, "add", "b.py")
        git(self.repo, "commit", "-qm", "move")
        p, rep = self.run_tool()
        self.assertEqual((rep["new_clones"], rep["new_literals"]), ([], []))
        self.assertIn("nothing new", p.stdout)


if __name__ == "__main__":
    unittest.main()
