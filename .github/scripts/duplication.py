#!/usr/bin/env python3
# duplication: the copies a change adds, base to HEAD. A measurement, never a
# gate: it exits 0 whatever the numbers say, so a reader (a person, a review,
# triage) can weigh them.
#
# Both sides are scanned whole, so a new copy of code the change never touched
# is still found. Two checks run on each side:
#
# - jscpd, which routes each file by extension to one of the formats it knows.
#   An extensionless script is read by its shebang and copied out under the
#   matching suffix, so a mixed repo needs no setup. A clone is new when the
#   pair of copies, each keyed by its path and its text, is not a clone at the
#   base; line numbers play no part, so moving code does not make a clone new.
# - repeated literals: the same quoted string of 20 or more characters in two
#   or more files. jscpd's minimum size hides a one-line copy, a regex or a
#   message; this check sees it. A literal is new when it sits in a file at
#   HEAD that did not hold it at the base, and in two or more files at HEAD.
#
# Prose and data (Markdown, plain text, JSON, lockfiles) are left out of both:
# a repeated paragraph or key is not a helper someone failed to find.
#
# Usage: duplication.py [--base REF] [--json PATH] [--require-tools]
#
# --base is compared at its merge base with HEAD (default origin/main).
# Markdown goes to stdout, for $GITHUB_STEP_SUMMARY; --json also writes the
# report for a machine reader. jscpd is found on PATH, or named by $JSCPD; when
# it is missing the literal check still runs and the summary says so, and with
# --require-tools it is exit 2; usage and git errors are exit 2 too.
# Python 3 stdlib only.
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

NAME = "duplication"
sys.dont_write_bytecode = True

MIN_LINES, MIN_TOKENS = 5, 50
# Globs left out of both checks; jscpd reads them as comma-separated.
IGNORE = ["**/*.md", "**/*.markdown", "**/*.txt", "**/*.json", "**/*.lock", "**/package-lock.json",
          "**/*.svg", "**/*.min.js", "**/vendor/**", "**/node_modules/**"]
SKIP_EXT = {".md", ".markdown", ".txt", ".json", ".lock", ".svg"}
SKIP_NAME = {"package-lock.json"}
SKIP_DIR = {"vendor", "node_modules"}
# A shebang names the interpreter; an extensionless script is copied out under
# this suffix so jscpd, which reads only extensions, can parse it.
SHEBANG = {"python": ".py", "python3": ".py", "node": ".js", "ruby": ".rb", "perl": ".pl",
           "sh": ".sh", "bash": ".sh", "dash": ".sh", "ksh": ".sh", "mksh": ".sh", "zsh": ".sh"}
MIN_LITERAL = 20
# A quoted string on one line: the opening quote not glued to a word (so the
# apostrophes in "the user's and the agent's" are not a string), an optional
# r/b/u/f prefix, escapes kept as written.
LITERAL = re.compile(r"""(?<![\w'"`])(?:[rRbBuUfF]{1,2})?(["'`])((?:(?!\1)[^\\\n]|\\.){%d,})\1(?![\w'"`])"""
                     % MIN_LITERAL)
ROWS = 30  # each table stops here; --json keeps every row


def die(msg):
    print("%s: %s" % (NAME, msg), file=sys.stderr)
    sys.exit(2)


def git(*args, root, stdin=None):
    p = subprocess.run(["git", *args], cwd=root, input=stdin, capture_output=True)
    if p.returncode:
        die("git %s: %s" % (" ".join(args), p.stderr.decode(errors="replace").strip()))
    return p.stdout


def shebang_suffix(head):
    if not head.startswith(b"#!"):
        return None
    words = head[2:].split(b"\n", 1)[0].decode(errors="replace").split()
    if words and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
    return SHEBANG.get(os.path.basename(words[0])) if words else None


def skipped(path):
    parts = path.split("/")
    return (os.path.splitext(path)[1].lower() in SKIP_EXT or parts[-1] in SKIP_NAME
            or parts[-1].endswith(".min.js") or any(p in SKIP_DIR for p in parts[:-1]))


def materialize(root, rev, dest):
    """Write rev's whole tree under dest; {scanned name: path} for every
    regular file either check reads. A submodule has no blob, a symlink no
    text of its own, so both are left out. Blobs come from the object store,
    not `git archive`, which drops what .gitattributes marks export-ignore."""
    files = {}
    real, blobs = set(), []
    for row in git("ls-tree", "-r", "-z", "--full-tree", rev, root=root).split(b"\0"):
        if not row:
            continue
        meta, path = row.split(b"\t", 1)
        mode, kind, sha = meta.split()
        path = path.decode(errors="surrogateescape")
        real.add(path)
        if kind == b"blob" and mode != b"120000" and not skipped(path):
            blobs.append((path, sha))
    out = git("cat-file", "--batch", root=root, stdin=b"".join(sha + b"\n" for _, sha in blobs))
    at = 0
    for path, _ in blobs:
        nl = out.index(b"\n", at)
        size = int(out[at:nl].split()[2])
        blob, at = out[nl + 1:nl + 1 + size], nl + 2 + size
        if b"\0" in blob[:8000]:
            continue
        name = path
        if not os.path.splitext(name)[1]:
            ext = shebang_suffix(blob[:200])
            # `run` beside a real `run.py` keeps its own name: the literal
            # check still reads it, jscpd does not.
            if ext and name + ext not in real:
                name += ext
        target = os.path.join(dest, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(blob)
        files[name] = path
    return files


def read_lines(dest, name):
    with open(os.path.join(dest, name), encoding="utf-8", errors="replace") as f:
        return f.read().split("\n")


def clones(jscpd, dest, files, tmp):
    """{key: clone} for every clone jscpd reports under dest, and its totals.
    A copy is its path and its text with the spaces squeezed out, so the key
    stays put when the copies move or jscpd lists the pair the other way."""
    out = os.path.join(tmp, "jscpd-" + os.path.basename(dest))
    p = subprocess.run([jscpd, "--min-lines", str(MIN_LINES), "--min-tokens", str(MIN_TOKENS),
                        "--ignore", ",".join(IGNORE), "--reporters", "json", "--output", out,
                        "--silent", "--no-colors", "."],
                       cwd=dest, capture_output=True, encoding="utf-8", errors="replace")
    try:
        with open(os.path.join(out, "jscpd-report.json")) as f:
            report = json.load(f)
    except (OSError, ValueError):
        die("jscpd failed: %s" % (p.stderr.strip() or p.stdout.strip() or "no report"))
    text = {}
    found = {}
    for d in report.get("duplicates", []):
        copies = []
        for side in (d["firstFile"], d["secondFile"]):
            name = side["name"][2:] if side["name"].startswith("./") else side["name"]
            if name not in files:
                break
            if name not in text:
                text[name] = read_lines(dest, name)
            body = "".join("".join(text[name][side["start"] - 1:side["end"]]).split())
            copies.append({"path": files[name], "start": side["start"], "end": side["end"],
                           "hash": hashlib.sha1(body.encode()).hexdigest()})
        else:
            copies.sort(key=lambda c: (c["path"], c["hash"], c["start"]))
            key = tuple((c["path"], c["hash"]) for c in copies)
            found.setdefault(key, {"format": d.get("format"), "lines": d.get("lines"), "copies": copies})
    total = report.get("statistics", {}).get("total", {})
    return found, {"clones": total.get("clones", 0), "lines": total.get("duplicatedLines", 0),
                   "percentage": round(total.get("percentage", 0.0), 2), "files": total.get("sources", 0)}


def literals(dest, files):
    """{literal: set of paths} for every quoted string of MIN_LITERAL or more
    characters, however many files hold it: the base's single copies are what
    tell a new copy from an old one."""
    at = {}
    for name, path in files.items():
        with open(os.path.join(dest, name), encoding="utf-8", errors="replace") as f:
            for m in LITERAL.finditer(f.read()):
                at.setdefault(m.group(2), set()).add(path)
    return at


def repeated(lits):
    return sum(1 for paths in lits.values() if len(paths) > 1)


def new_clones(before, after):
    return sorted((after[k] for k in after if k not in before), key=lambda c: (-c["lines"], c["copies"][0]["path"]))


def new_literals(before, after):
    out = []
    for lit, paths in after.items():
        added = paths - before.get(lit, set())
        if added and len(paths) > 1:
            out.append({"literal": lit, "files": sorted(paths), "added": sorted(added)})
    return sorted(out, key=lambda r: (-len(r["files"]), r["literal"]))


def markdown(report):
    # Paths and literals come from the PR, so nothing in them may close the
    # cell or the code span; a pipe is escaped, which GitHub reads inside one.
    cell = lambda v: "".join(c for c in v if c not in "`<>\r\n")[:120].replace("|", "\\|")
    plural = lambda n, word: "%d %s%s" % (n, word, "" if n == 1 else "s")
    nc, nl, b, h = report["new_clones"], report["new_literals"], report["base_totals"], report["head_totals"]
    if nc or nl:
        lines = ["### Duplication: %s, %s" % (plural(len(nc), "new clone"), plural(len(nl), "new repeated literal"))]
    else:
        lines = ["### Duplication: nothing new"]
    lines += ["", "| Whole tree | Base | Head |", "|---|---:|---:|"]
    if report["jscpd"]:
        lines += ["| Clones (jscpd, %d+ lines, %d+ tokens) | %d | %d |" % (MIN_LINES, MIN_TOKENS, b["clones"], h["clones"]),
                  "| Duplicated lines | %d (%s%%) | %d (%s%%) |" % (b["lines"], b["percentage"], h["lines"], h["percentage"])]
    lines.append("| Repeated literals (%d+ characters, 2+ files) | %d | %d |" % (MIN_LITERAL, b["literals"], h["literals"]))
    if nc:
        lines += ["", "**New clones**", "", "| Lines | Copy | Copy |", "|---:|---|---|"]
        for c in nc[:ROWS]:
            lines.append("| %d | %s |" % (c["lines"], " | ".join(
                "`%s:%d-%d`" % (cell(x["path"]), x["start"], x["end"]) for x in c["copies"])))
        if len(nc) > ROWS:
            lines.append("| …and %d more | | |" % (len(nc) - ROWS))
    if nl:
        lines += ["", "**New repeated literals**", "", "| Literal | Files | Newly in |", "|---|---:|---|"]
        for r in nl[:ROWS]:
            lines.append("| `%s` | %d | %s |" % (cell(r["literal"]), len(r["files"]),
                                                  ", ".join("`%s`" % cell(p) for p in r["added"][:5])
                                                  + (" …" if len(r["added"]) > 5 else "")))
        if len(nl) > ROWS:
            lines.append("| …and %d more | | |" % (len(nl) - ROWS))
    if not report["jscpd"]:
        lines += ["", "**jscpd not found:** clones not measured; repeated literals only."]
    lines += ["", "Scanned the whole tree, %s against %s. A measurement, not a gate." % (
        report["head"][:7], report["base"][:7])]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(prog=NAME, description="New clones and repeated literals, base to HEAD.")
    ap.add_argument("--base", default="origin/main", help="compared at its merge base with HEAD")
    ap.add_argument("--json", metavar="PATH", help="also write the report as JSON")
    ap.add_argument("--require-tools", action="store_true", help="exit 2 when jscpd is missing")
    a = ap.parse_args()
    root = git("rev-parse", "--show-toplevel", root=None).decode().strip()
    base = git("merge-base", a.base, "HEAD", root=root).decode().strip()
    head = git("rev-parse", "HEAD", root=root).decode().strip()
    jscpd = os.environ.get("JSCPD") or shutil.which("jscpd")
    if not jscpd and a.require_tools:
        die("not found: jscpd")
    sides = {}
    with tempfile.TemporaryDirectory(prefix="duplication-") as tmp:
        for side, rev in (("base", base), ("head", head)):
            dest = os.path.join(tmp, side)
            os.makedirs(dest)
            files = materialize(root, rev, dest)
            found, totals = clones(jscpd, dest, files, tmp) if jscpd else ({}, {})
            lits = literals(dest, files)
            sides[side] = (found, dict(totals, literals=repeated(lits)), lits)
    (cb, tb, lb), (ch, th, lh) = sides["base"], sides["head"]
    report = {"base": base, "head": head, "jscpd": bool(jscpd), "base_totals": tb, "head_totals": th,
              "new_clones": new_clones(cb, ch), "new_literals": new_literals(lb, lh)}
    if a.json:
        with open(a.json, "w") as f:
            json.dump(report, f, indent=1)
    sys.stdout.write(markdown(report))


if __name__ == "__main__":
    main()
