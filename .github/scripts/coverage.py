#!/usr/bin/env python3
# coverage: of the executable lines a pull request added or changed,
# the share the head test run hit; the total base to head when a base report
# is given; and, when lizard is on PATH, each touched function's cyclomatic
# complexity beside its covered share and CRAP score (Savoia 2007:
# cc^2 * (1 - cov)^3 + cc). The engine of coverage.yml, a sibling of
# complexity.py. Stdlib only.
#
# Input is Cobertura XML, which coverage.py (--cov-report=xml), c8, cargo-llvm-cov
# and gocover-cobertura all write, so the engine is language-neutral; only the
# producer is per language. The changed lines come from `git diff` at the merge
# base, so no base test run is needed for the headline number.
#
# Usage: coverage.py --coverage HEAD.xml [--base-coverage BASE.xml]
#                    [--base REF] [--json PATH] [--fail-under PCT]
# Markdown goes to stdout, for $GITHUB_STEP_SUMMARY and the PR comment.
# Exit 0 whatever the numbers say, unless --fail-under is given and the
# changed-line share is below it: a gate is the caller's choice, not the engine's.

import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

NAME = "coverage"
ROWS = 30


def die(msg):
    sys.stderr.write("%s: %s\n" % (NAME, msg))
    sys.exit(2)


def git(*args, root):
    p = subprocess.run(["git", "-C", root, *args], capture_output=True)
    if p.returncode:
        die("git %s: %s" % (args[0], p.stderr.decode(errors="replace").strip()))
    return p.stdout


def cobertura(path, root):
    """{repo-relative path: {line: hits}} and the report's totals. Cobertura
    keys files by filename under one of <sources>; both are joined and made
    relative to the repository root so they match `git diff` paths."""
    doc = ET.parse(path).getroot()
    sources = [s.text for s in doc.iter("source") if s.text] or ["."]
    files = {}
    for cls in doc.iter("class"):
        fn = cls.get("filename")
        rel = None
        for s in sources:
            cand = os.path.realpath(os.path.join(s, fn))
            if cand.startswith(os.path.realpath(root) + os.sep):
                rel = os.path.relpath(cand, os.path.realpath(root))
                break
        rel = rel or fn
        lines = files.setdefault(rel, {})
        for ln in cls.iter("line"):
            n, hits = int(ln.get("number")), int(ln.get("hits", "0"))
            lines[n] = max(lines.get(n, 0), hits)
    g = lambda k: doc.get(k)
    totals = {"lines_valid": int(g("lines-valid") or 0), "lines_covered": int(g("lines-covered") or 0),
              "branches_valid": int(g("branches-valid") or 0), "branches_covered": int(g("branches-covered") or 0)}
    if totals["lines_valid"] == 0:  # a producer that leaves the totals out
        totals["lines_valid"] = sum(len(v) for v in files.values())
        totals["lines_covered"] = sum(1 for v in files.values() for h in v.values() if h)
    return files, totals


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(root, base):
    """{path: set of new-side line numbers added or changed}, merge base to HEAD."""
    out, path = {}, None
    for raw in git("diff", "-U0", "--no-renames", "--no-color", base, "HEAD", root=root).decode(
            "utf-8", errors="surrogateescape").splitlines():
        if raw.startswith("+++ "):
            path = None if raw == "+++ /dev/null" else raw[4:].removeprefix("b/")
            if path is not None:
                out.setdefault(path, set())
        elif raw.startswith("@@") and path is not None:
            m = HUNK.match(raw)
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            out[path].update(range(start, start + count))
    return out


def ranges(nums):
    """'3–5, 9' for {3,4,5,9}."""
    out, nums = [], sorted(nums)
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else "%d–%d" % (nums[i], nums[j]))
        i = j + 1
    return ", ".join(out)


def pct(n, d):
    return None if d == 0 else round(100.0 * n / d, 1)


def per_file(files, changed):
    rows, outside = [], []
    for path, lines in sorted(changed.items()):
        if path not in files:
            if lines:
                outside.append(path)
            continue
        execu = [n for n in lines if n in files[path]]
        if not execu:
            continue
        missed = [n for n in execu if files[path][n] == 0]
        rows.append({"path": path, "changed": len(execu), "covered": len(execu) - len(missed),
                     "pct": pct(len(execu) - len(missed), len(execu)), "missed": sorted(missed)})
    return sorted(rows, key=lambda r: (r["pct"], -r["changed"], r["path"])), outside


def functions(root, files, changed):
    """Touched functions with cyclomatic complexity (lizard), covered share and
    CRAP. Empty when lizard is not on PATH or no touched file is one it reads."""
    lizard = shutil.which("lizard")
    paths = [p for p in changed if p in files and changed[p]]
    if not lizard or not paths:
        return []
    p = subprocess.run([lizard, "--csv", *paths], cwd=root, capture_output=True, text=True)
    out = []
    # NLOC,CCN,token,PARAM,length,location,file,function,long_name,start,end
    for r in csv.reader(io.StringIO(p.stdout)):
        if len(r) < 11:
            continue
        path, func, cc, start, end = r[6], r[7], int(r[1]), int(r[9]), int(r[10])
        path = os.path.normpath(path)
        if path not in files or not any(start <= n <= end for n in changed[path]):
            continue
        execu = [n for n in range(start, end + 1) if n in files[path]]
        hit = sum(1 for n in execu if files[path][n])
        cov = hit / len(execu) if execu else 0.0
        crap = round(cc * cc * (1 - cov) ** 3 + cc, 1)
        out.append({"path": path, "function": func, "line": start, "ccn": cc, "executable": len(execu),
                    "covered": hit, "pct": pct(hit, len(execu)), "crap": crap})
    return sorted(out, key=lambda f: (-f["crap"], f["path"], f["line"]))


def markdown(rep):
    cell = lambda v: "".join(c for c in v if c not in "`|<>\r\n")[:120]
    c = rep["changed"]
    if c["executable"]:
        head = "### Coverage: %.0f%% of changed lines (%d of %d)" % (c["pct"], c["covered"], c["executable"])
    else:
        head = "### Coverage: no executable line changed"
    t, b = rep["total"], rep.get("base_total")
    tot = "total %.1f%%" % t["pct"]
    if b and b["pct"] is not None:
        tot += " (%+.1f from %.1f%%)" % (t["pct"] - b["pct"], b["pct"])
    lines = [head + " · " + tot, ""]
    if rep["files"]:
        lines += ["| File | Changed lines | Covered | Missed |", "|---|---:|---:|---|"]
        for r in rep["files"][:ROWS]:
            lines.append("| `%s` | %d | %.0f%% | %s |" % (cell(r["path"]), r["changed"], r["pct"],
                                                        cell(ranges(r["missed"])) if r["missed"] else ""))
        if len(rep["files"]) > ROWS:
            lines.append("| …and %d more files | | | |" % (len(rep["files"]) - ROWS))
    if rep["functions"]:
        lines += ["", "<details><summary>Touched functions by CRAP (cc² · (1 − covered)³ + cc)</summary>", "",
                  "| File | Function | Cyclomatic | Covered | CRAP |", "|---|---|:---:|---:|---:|"]
        for f in rep["functions"][:ROWS]:
            lines.append("| `%s:%d` | `%s` | %d | %s | %.1f |" % (
                cell(f["path"]), f["line"], cell(f["function"]), f["ccn"],
                "—" if f["pct"] is None else "%.0f%%" % f["pct"], f["crap"]))
        lines += ["", "</details>"]
    tail = "Measured %s against %s: %d changed file%s" % (
        rep["head"][:7], rep["base"][:7], rep["changed_files"], "" if rep["changed_files"] == 1 else "s")
    if rep["outside"]:
        tail += ", %d outside the coverage report" % len(rep["outside"])
    tail += ". " + ("Gate: changed-line coverage under %s%%." % rep["fail_under"]
                    if rep.get("failed") else "A measurement, not a gate.")
    lines += ["", tail]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(prog=NAME)
    ap.add_argument("--coverage", required=True, help="head Cobertura XML")
    ap.add_argument("--base-coverage", help="base Cobertura XML, for the total's delta")
    ap.add_argument("--base", default="origin/main", help="compared at its merge base with HEAD")
    ap.add_argument("--json", metavar="PATH")
    ap.add_argument("--fail-under", type=float, metavar="PCT", help="exit 1 below this changed-line share")
    a = ap.parse_args()
    root = git("rev-parse", "--show-toplevel", root=".").decode().strip()
    base = git("merge-base", a.base, "HEAD", root=root).decode().strip()
    head = git("rev-parse", "HEAD", root=root).decode().strip()
    files, totals = cobertura(a.coverage, root)
    changed = changed_lines(root, base)
    rows, outside = per_file(files, changed)
    execu, covered = sum(r["changed"] for r in rows), sum(r["covered"] for r in rows)
    rep = {"head": head, "base": base, "changed_files": len(changed), "outside": outside,
           "changed": {"executable": execu, "covered": covered, "pct": pct(covered, execu)},
           "total": dict(totals, pct=pct(totals["lines_covered"], totals["lines_valid"])),
           "files": rows, "functions": functions(root, files, changed), "fail_under": a.fail_under}
    if a.base_coverage:
        _, bt = cobertura(a.base_coverage, root)
        rep["base_total"] = dict(bt, pct=pct(bt["lines_covered"], bt["lines_valid"]))
    rep["failed"] = a.fail_under is not None and execu > 0 and rep["changed"]["pct"] < a.fail_under
    sys.stdout.write(markdown(rep))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rep, f, indent=1)
    sys.exit(1 if rep["failed"] else 0)


if __name__ == "__main__":
    main()
