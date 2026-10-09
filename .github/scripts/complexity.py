#!/usr/bin/env python3
# complexity: how the cyclomatic complexity of each function a change touches
# moved, base to HEAD. A measurement, never a gate: it exits 0 whatever the
# numbers say, so a reader (a person, a review, triage) can weigh them.
#
# Each changed file goes to the tool for its language, chosen by extension or,
# with none, by its shebang: lizard for Python, JS/TS, Go, Rust and the rest
# it knows; shellmetrics for sh, bash, ksh and zsh. A mixed repo needs no
# setup. Both tools are found on PATH, or named by $LIZARD / $SHELLMETRICS.
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
        words = [w for w in words[1:] if not w.startswith("-")]
    return SHEBANG.get(os.path.basename(words[0])) if words else None


def tool(env, name):
    return os.environ.get(env) or shutil.which(name)


def run_tool(argv, cwd):
    p = subprocess.run(argv, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace")
    if p.returncode and not p.stdout:
        die("%s failed: %s" % (os.path.basename(argv[0]), p.stderr.strip()))
    return p.stdout


def measure(side, files, tools):
    """{(path, function): (ccn, line)} for files {tmpname: path} under side.
    A name that repeats in one file gets #2, #3 so neither hides the other."""
    out = {}

    def put(tmp, func, ccn, line):
        path = files[tmp]
        key, n = (path, func), 1
        while key in out:
            n += 1
            key = (path, "%s#%d" % (func, n))
        out[key] = (ccn, line)

    lz = sorted(t for t in files if os.path.splitext(t)[1] not in SHELL_EXT)
    sh = sorted(t for t in files if os.path.splitext(t)[1] in SHELL_EXT)
    if lz and tools["lizard"]:
        # NLOC,CCN,token,PARAM,length,location,file,function,long_name,start,end
        for r in csv.reader(io.StringIO(run_tool([tools["lizard"], "--csv", *lz], side))):
            if len(r) >= 11 and r[6] in files:
                put(r[6], r[7], int(r[1]), int(r[9]))
    if sh and tools["shellmetrics"]:
        # file,func,lineno,lloc,ccn,lines,comment,blank; <main> is the code
        # outside any function, <begin> and <end> are per-file bookkeeping.
        for r in csv.DictReader(io.StringIO(run_tool([tools["shellmetrics"], "--csv", *sh], side))):
            if r["file"] in files and r["func"] not in ("<begin>", "<end>"):
                put(r["file"], TOP if r["func"] == "<main>" else r["func"], int(r["ccn"]), int(r["lineno"]))
    return out


def changes(root, base):
    """[(status, path)] from the merge base to HEAD, renames as delete + add."""
    raw = git("diff", "--name-status", "-z", "--no-renames", base, "HEAD", root=root)
    parts = raw.decode("utf-8", errors="surrogateescape").split("\0")
    return [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]


def materialize(root, rev, paths, dest):
    """Write rev's copy of each path under dest; {tmpname: path} for those a
    tool measures."""
    files = {}
    for path in paths:
        blob = git("show", "%s:%s" % (rev, path), root=root)
        ext = suffix(path, blob[:200])
        if ext is None:
            continue
        tmp = path if os.path.splitext(path)[1].lower() == ext else path + ext
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
        if b and a and b[0] == a[0]:
            continue
        out.append({"path": key[0], "function": key[1], "line": (a or b)[1],
                    "before": b and b[0], "after": a and a[0],
                    "delta": (a[0] if a else 0) - (b[0] if b else 0)})
    return sorted(out, key=lambda r: (-abs(r["delta"]), r["path"], r["function"]))


def markdown(report):
    rs, n = report["functions"], len(report["functions"])
    lines = ["## Complexity: %+d across %d function%s" % (report["delta"], n, "" if n == 1 else "s"), ""]
    if rs:
        lines += ["| Function | Before | After | Δ |", "|---|---:|---:|---:|"]
        dash = lambda v: "—" if v is None else str(v)
        # Paths and names come from the PR, so nothing in them may close the cell.
        cell = lambda v: "".join(c for c in v if c not in "`|<>\r\n")[:120]
        for r in rs[:ROWS]:
            lines.append("| `%s` · `%s` (line %d) | %s | %s | %+d |" % (
                cell(r["path"]), cell(r["function"]), r["line"], dash(r["before"]), dash(r["after"]), r["delta"]))
        if n > ROWS:
            lines.append("| …and %d more, smaller changes | | | |" % (n - ROWS))
    else:
        lines.append("No function's complexity changed.")
    for name, count in sorted(report["unmeasured"].items()):
        lines += ["", "**%s not found:** %d changed file%s not measured." % (name, count, "" if count == 1 else "s")]
    lines += ["", "Measured %d changed file%s against %s; cyclomatic complexity per function, a measurement, not a gate."
              % (report["files"], "" if report["files"] == 1 else "s", report["base"][:12])]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(prog=NAME, description="Per-function cyclomatic complexity change, base to HEAD.")
    ap.add_argument("--base", default="origin/main", help="compared at its merge base with HEAD")
    ap.add_argument("--json", metavar="PATH", help="also write the rows as JSON")
    ap.add_argument("--require-tools", action="store_true", help="exit 2 when a needed tool is missing")
    a = ap.parse_args()
    root = git("rev-parse", "--show-toplevel", root=None).decode().strip()
    base = git("merge-base", a.base, "HEAD", root=root).decode().strip()
    tools = {"lizard": tool("LIZARD", "lizard"), "shellmetrics": tool("SHELLMETRICS", "shellmetrics")}
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
        for name, shell in (("lizard", False), ("shellmetrics", True)):
            if tools[name]:
                continue
            count = len({p for _, files in sides.values() for t, p in files.items()
                         if (os.path.splitext(t)[1] in SHELL_EXT) == shell})
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
