#!/usr/bin/env python3
# complexity: how the cyclomatic complexity of each function a change touches
# moved, base to HEAD. A measurement, never a gate: it exits 0 whatever the
# numbers say, so a reader (a person, a review, triage) can weigh them.
#
# Each changed file goes to the tool for its language, chosen by extension or,
# with none, by its shebang: lizard for Python, JS/TS, Go, Rust and the rest
# it knows; shellmetrics for sh, bash, ksh and zsh. A mixed repo needs no
# setup. The tools are found on PATH, or named by $LIZARD / $SHELLMETRICS / $RCA.
#
# Where rust-code-analysis (named by $RCA or found as rust-code-analysis-cli)
# also reads the file, its cognitive complexity, Halstead measures and
# maintainability index join the function's row. Those, and every lizard
# column (lines, tokens, parameters, nesting depth), are logged in --json for a
# trial; none is weighted, and the summary shows cyclomatic and cognitive.
#
# Usage: complexity.py [--base REF] [--json PATH] [--require-tools]
#
# --base is compared at its merge base with HEAD (default origin/main).
# Markdown goes to stdout, for $GITHUB_STEP_SUMMARY; --json also writes the
# rows for a machine reader. A tool that is missing is reported, and with
# --require-tools is exit 2; usage and git errors are exit 2 too.
# Python 3 stdlib only.
import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

NAME = "complexity"
sys.dont_write_bytecode = True

LIZARD_EXT = {".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".go", ".java", ".js", ".jsx", ".mjs", ".cjs",
              ".ts", ".tsx", ".vue", ".kt", ".kts", ".lua", ".m", ".php", ".pl", ".py", ".rb", ".rs",
              ".scala", ".sol", ".swift", ".zig", ".erl", ".f90", ".gd", ".r"}
# What rust-code-analysis parses, a subset of lizard's.
RCA_EXT = {".c", ".h", ".cc", ".cpp", ".hpp", ".java", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
           ".kt", ".kts", ".py", ".rs"}
SHELL_EXT = {".sh", ".bash", ".ksh", ".zsh"}
# A shebang names the interpreter; an extensionless script is copied out under
# this suffix so lizard, which reads only extensions, can parse it.
SHEBANG = {"python": ".py", "python3": ".py", "node": ".js", "ruby": ".rb", "perl": ".pl",
           "sh": ".sh", "bash": ".sh", "dash": ".sh", "ksh": ".sh", "mksh": ".sh", "zsh": ".sh"}
TOP = "(top level)"
ROWS = 30  # the summary's table stops here; --json keeps every row


def die(msg):
    print("%s: %s" % (NAME, msg), file=sys.stderr)
    sys.exit(2)


def git(*args, root):
    p = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if p.returncode:
        die("git %s: %s" % (" ".join(args), p.stderr.decode(errors="replace").strip()))
    return p.stdout


def suffix(path, head):
    """The extension a tool reads this file as, or None when no tool here
    measures it."""
    ext = os.path.splitext(path)[1].lower()
    if ext in LIZARD_EXT or ext in SHELL_EXT:
        return ext
    if ext or not head.startswith(b"#!"):
        return None
    words = head[2:].split(b"\n", 1)[0].decode(errors="replace").split()
    if words and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
    return SHEBANG.get(os.path.basename(words[0])) if words else None


def tool(env, name):
    return os.environ.get(env) or shutil.which(name)


def run_tool(argv, cwd):
    p = subprocess.run(argv, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace")
    if p.returncode and not p.stdout:
        die("%s failed: %s" % (os.path.basename(argv[0]), p.stderr.strip()))
    return p.stdout


def rca_metrics(argv, cwd):
    """{start line: metrics} for every function in one file; {} when the tool
    cannot read it."""
    p = subprocess.run(argv, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace")
    try:
        space = json.loads(p.stdout)
    except ValueError:
        return {}
    found, todo = {}, [space]
    while todo:
        sp = todo.pop()
        todo += sp.get("spaces", [])
        if sp.get("kind") == "function":
            m = sp["metrics"]
            found[sp["start_line"]] = {
                "cognitive": int(m["cognitive"]["sum"]),
                "halstead": {k: m["halstead"][k] for k in
                             ("n1", "N1", "n2", "N2", "length", "vocabulary", "volume", "difficulty", "effort", "bugs")},
                "mi": dict(m["mi"])}
    return found


def measure(side, files, tools):
    """{(path, function): metrics} for files {tmpname: path} under side, where
    metrics is a dict with at least "ccn" and "line". A name that repeats in
    one file gets #2, #3 so neither hides the other."""
    out, at = {}, {}

    def put(tmp, func, line, **metrics):
        path = files[tmp]
        key, n = (path, func), 1
        while key in out:
            n += 1
            key = (path, "%s#%d" % (func, n))
        out[key] = dict(metrics, line=line)
        at[(tmp, line)] = key

    lz = sorted(t for t in files if os.path.splitext(t)[1] not in SHELL_EXT)
    sh = sorted(t for t in files if os.path.splitext(t)[1] in SHELL_EXT)
    if lz and tools["lizard"]:
        # NLOC,CCN,token,PARAM,length,location,file,function,long_name,start,end,nesting
        rs = [r for r in csv.reader(io.StringIO(run_tool([tools["lizard"], "--csv", "-ENS", *lz], side)))
              if len(r) >= 11 and r[6] in files]
        # A name that repeats in a file is keyed by its long name (class,
        # params) for every copy, so inserting one does not renumber the rest.
        seen = {}
        for r in rs:
            seen[(r[6], r[7])] = seen.get((r[6], r[7]), 0) + 1
        for r in rs:
            put(r[6], r[8] if seen[(r[6], r[7])] > 1 else r[7], int(r[9]), ccn=int(r[1]), nloc=int(r[0]),
                tokens=int(r[2]), params=int(r[3]), length=int(r[4]), nesting=int(r[11]) if len(r) > 11 else None)
        if tools["rca"]:
            for t in lz:
                if os.path.splitext(t)[1].lower() in RCA_EXT:
                    for line, m in rca_metrics([tools["rca"], "-m", "-O", "json", "-p", t], side).items():
                        if (t, line) in at:
                            out[at[(t, line)]].update(m)
    if sh and tools["shellmetrics"]:
        # file,func,lineno,lloc,ccn,lines,comment,blank; <main> is the code
        # outside any function, <begin> and <end> are per-file bookkeeping.
        for r in csv.DictReader(io.StringIO(run_tool([tools["shellmetrics"], "--csv", *sh], side))):
            if r["file"] in files and r["func"] not in ("<begin>", "<end>"):
                put(r["file"], TOP if r["func"] == "<main>" else r["func"], int(r["lineno"]), ccn=int(r["ccn"]),
                    nloc=int(r["lloc"]), length=int(r["lines"]))
    return out


def changes(root, base):
    """[(status, path)] from the merge base to HEAD, renames as delete + add.
    A submodule has no blob to measure, so it is left out."""
    raw = git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, "HEAD", root=root)
    parts = raw.decode("utf-8", errors="surrogateescape").split("\0")
    out = []
    for i in range(0, len(parts) - 1, 2):
        modes, status = parts[i].lstrip(":").split()[:2], parts[i].split()[-1]
        if "160000" not in modes:
            out.append((status, parts[i + 1]))
    return out


def materialize(root, rev, paths, dest):
    """Write rev's copy of each path under dest; {tmpname: path} for those a
    tool measures."""
    files = {}
    for path in paths:
        blob = git("show", "%s:%s" % (rev, path), root=root)
        ext = suffix(path, blob[:200])
        if ext is None:
            continue
        tmp = path if os.path.splitext(path)[1] == ext else path + ext
        os.makedirs(os.path.join(dest, os.path.dirname(tmp)), exist_ok=True)
        with open(os.path.join(dest, tmp), "wb") as f:
            f.write(blob)
        # "./" keeps a path that starts with "-" from reading as a tool option.
        files["./" + tmp] = path
    return files


def rows(before, after):
    out = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        # A row is a function whose cyclomatic or cognitive count moved.
        if b and a and b["ccn"] == a["ccn"] and b.get("cognitive") == a.get("cognitive"):
            continue
        out.append({"path": key[0], "function": key[1], "line": (a or b)["line"],
                    "before": b and b["ccn"], "after": a and a["ccn"],
                    "delta": (a["ccn"] if a else 0) - (b["ccn"] if b else 0),
                    "cognitive": {"before": b and b.get("cognitive"), "after": a and a.get("cognitive")},
                    "metrics": {"before": b, "after": a}})
    return sorted(out, key=lambda r: (-abs(r["delta"]), r["path"], r["function"]))


def flat(m, prefix=""):
    """{"halstead.volume": 66.6, ...} for a metrics dict, without its line."""
    out = {}
    for k, v in (m or {}).items():
        if isinstance(v, dict):
            out.update(flat(v, prefix + k + "."))
        elif k != "line":
            out[prefix + k] = v
    return out


def details(rs, dash, cell):
    """A collapsed section: every metric of each function in the table, before → after."""
    lines = ["", "<details>", "<summary>All metrics</summary>", "", "| Function | Metric | Before | After |", "|---|---|---:|---:|"]
    num = lambda v: dash(v if not isinstance(v, float) else round(v, 2))
    for r in rs[:ROWS]:
        b, a = flat(r["metrics"]["before"]), flat(r["metrics"]["after"])
        name = "`%s` · `%s`" % (cell(r["path"]), cell(r["function"]))
        for k in sorted(set(b) | set(a)):
            lines.append("| %s | %s | %s | %s |" % (name, k, num(b.get(k)), num(a.get(k))))
    return lines + ["", "</details>"]


def markdown(report):
    rs, n = report["functions"], len(report["functions"])
    dash = lambda v: "—" if v is None else str(v)
    # Paths and names come from the PR, so nothing in them may close the cell.
    cell = lambda v: "".join(c for c in v if c not in "`|<>\r\n")[:120]
    span = lambda b, a: "%s → %s" % (dash(b), dash(a))
    if rs:
        lines = ["### Complexity: %+d across %d function%s" % (report["delta"], n, "" if n == 1 else "s"), "",
                 "| File | Function | Cyclomatic | Δ | Cognitive |", "|---|---|:---:|---:|:---:|"]
        for r in rs[:ROWS]:
            c = r["cognitive"]
            lines.append("| `%s:%d` | `%s` | %s | %+d | %s |" % (
                cell(r["path"]), r["line"], cell(r["function"]), span(r["before"], r["after"]), r["delta"],
                span(c["before"], c["after"])))
        if n > ROWS:
            lines.append("| …and %d more, smaller changes | | | | |" % (n - ROWS))
        lines += details(rs, dash, cell)
    else:
        lines = ["### Complexity: no change"]
    for name, count in sorted(report["unmeasured"].items()):
        lines += ["", "**%s not found:** %d changed file%s not measured." % (name, count, "" if count == 1 else "s")]
    measured = "Measured %d changed file%s, %s against %s." % (
        report["files"], "" if report["files"] == 1 else "s", report["head"][:7], report["base"][:7])
    lines += ["", measured + (" A measurement, not a gate." if rs else " No function's complexity moved.")]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(prog=NAME, description="Per-function cyclomatic complexity change, base to HEAD.")
    ap.add_argument("--base", default="origin/main", help="compared at its merge base with HEAD")
    ap.add_argument("--json", metavar="PATH", help="also write the rows as JSON")
    ap.add_argument("--require-tools", action="store_true", help="exit 2 when a needed tool is missing")
    a = ap.parse_args()
    root = git("rev-parse", "--show-toplevel", root=None).decode().strip()
    base = git("merge-base", a.base, "HEAD", root=root).decode().strip()
    tools = {"lizard": tool("LIZARD", "lizard"), "shellmetrics": tool("SHELLMETRICS", "shellmetrics"),
             "rca": tool("RCA", "rust-code-analysis-cli")}
    changed = changes(root, base)
    with tempfile.TemporaryDirectory(prefix="complexity-") as tmp:
        sides = {}
        # A deleted file has only a base side, an added one only a head.
        for side, rev, keep in (("base", base, "MD"), ("head", "HEAD", "AM")):
            want = [p for s, p in changed if s[0] in keep]
            dest = os.path.join(tmp, side)
            os.makedirs(dest)
            sides[side] = (dest, materialize(root, rev, want, dest))
        unmeasured = {}
        for name, wants in (("lizard", lambda e: e not in SHELL_EXT), ("shellmetrics", lambda e: e in SHELL_EXT),
                            ("rust-code-analysis", lambda e: e in RCA_EXT)):
            if tools["rca" if name == "rust-code-analysis" else name]:
                continue
            count = len({p for _, files in sides.values() for t, p in files.items()
                         if wants(os.path.splitext(t)[1].lower())})
            if count:
                unmeasured[name] = count
        if unmeasured and a.require_tools:
            die("not found: %s" % ", ".join(sorted(unmeasured)))
        before = measure(*sides["base"], tools)
        after = measure(*sides["head"], tools)
    rs = rows(before, after)
    report = {"base": base, "head": git("rev-parse", "HEAD", root=root).decode().strip(),
              "files": len({p for _, files in sides.values() for p in files.values()}),
              "delta": sum(r["delta"] for r in rs), "functions": rs, "unmeasured": unmeasured}
    if a.json:
        with open(a.json, "w") as f:
            json.dump(report, f, indent=1)
    sys.stdout.write(markdown(report))


if __name__ == "__main__":
    main()
