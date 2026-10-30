#!/usr/bin/env python3
"""The fixer job's mechanics, one subcommand per step of claude-review.yml's
`fixer` job. The agent and the GitHub calls live in the workflow; everything
that decides which thread is taken and which path may be committed lives here,
where the tests reach it.

  plan    the review threads (GraphQL nodes) -> this head's open [easy] threads,
          risky paths refused, suggestion fences applied to the checkout and
          the agent's line numbers moved past them; nothing on a head the
          fixer itself committed
  gate    after the agent: every changed tracked path not risky and named by a
          taken thread is kept, every other one reverted; a file the agent
          edited while leaving one of its threads unfinished goes back to how
          the agent found it, suggestion fixes and all; the kept
          files become
          createCommitOnBranch additions
  report  the commit's sha (or why there is none) -> one reply per thread and
          the roll-up for the job summary

The risky class is `RISKY` in the environment: shell globs, whitespace
separated, matched against the path from the repo root like a `case` pattern
(so `*` crosses `/`), the same list the triage step's risky_path() reads.
"""
import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

EASY = re.compile(r"^\s*(?:🤖\s*)?\[(?:nit|should)\]\s*\[easy\]")
FENCE = re.compile(r"^```suggestion[ \t]*\r?\n(.*?)^```[ \t]*\r?$", re.S | re.M)
BOT_LOGINS = {"claude", "claude[bot]"}
# Every fixer commit carries this trailer, and a head that does is not fixed
# again: the fixer never answers its own commit.
TRAILER = "Claude-Fixer-Run:"


def risky_globs():
    return os.environ.get("RISKY", "").split()


def is_risky(path, globs):
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


# A thread's path reaches the agent's --allowedTools, so only plain path
# characters pass: a `,`, `(`, `)` or space could forge a tool grant.
SAFE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")


def path_problem(root, path):
    """Why the fixer will not touch this path, or None. Checked before any
    edit and again at commit: a symlink could carry an edit past the risky
    class to wherever it points."""
    if not SAFE_PATH.match(path) or path.startswith("/") or ".." in path.split("/"):
        return "its path has characters the fixer does not pass on"
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        if os.path.islink(os.path.join(root, *parts[:i])):
            return "its path goes through a symlink"
    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(root, path))
    if os.path.commonpath([real_root, real]) != real_root:
        return "its path resolves outside the checkout"
    try:
        staged = git(root, "ls-files", "-s", "--", path).decode()
    except subprocess.CalledProcessError:
        staged = ""
    if staged.startswith("120000 "):
        return "its path is a symlink in git"
    return None


def suggestions(body):
    return [m.group(1) for m in FENCE.finditer(body)]


def easy_threads(nodes, head):
    """This head's unresolved claude[bot] threads whose finding is tagged
    [easy], with no reply yet (a reply means someone, or an earlier fixer
    run, has already answered it)."""
    out = []
    for n in nodes:
        comments = (n.get("comments") or {}).get("nodes") or []
        if n.get("isResolved") or not comments:
            continue
        first = comments[0]
        author = first.get("author") or {}
        # A login alone is not enough: `claude` is also a User account, and
        # anyone can comment on a public PR.
        if (author.get("__typename") != "Bot" or author.get("login") not in BOT_LOGINS
                or not EASY.match(first.get("body") or "")
                or ((first.get("originalCommit") or {}).get("oid") != head)
                or (n.get("comments") or {}).get("totalCount", len(comments)) != 1):
            continue
        out.append({
            "thread_id": n["id"],
            "comment_id": first["databaseId"],
            "url": first.get("url", ""),
            "path": n.get("path"),
            "start": n.get("originalStartLine") or n.get("originalLine"),
            "end": n.get("originalLine"),
            "side": n.get("diffSide") or "RIGHT",
            "body": first["body"],
        })
    return out


def eol_of(line):
    if line.endswith("\r\n"):
        return "\r\n"
    return "\n" if line.endswith("\n") else ""


def apply_file(root, path, threads):
    """Apply each thread's single suggestion to one file, bottom-up so earlier
    line numbers stay true; an overlapping or out-of-range one is not taken.
    Returns what was replaced, (start, end, new line count) each, so the
    agent's line numbers on the same file can be moved to match."""
    p = Path(root) / path
    try:
        text = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        for t in threads:
            t.update(status="not-taken", why=f"cannot read {path}: {e.__class__.__name__}")
        return []
    lines = text.splitlines(keepends=True)
    floor = None  # lowest start line already replaced
    done = []
    for t in sorted(threads, key=lambda t: (t["start"], t["end"]), reverse=True):
        s, e = t["start"], t["end"]
        if not (1 <= s <= e <= len(lines)):
            t.update(status="not-taken", why=f"lines {s}-{e} are not in {path} at this head")
            continue
        if floor is not None and e >= floor:
            t.update(status="not-taken", why="its lines overlap another suggestion's")
            continue
        eol = eol_of(lines[e - 1])
        raw = t.pop("suggestion")
        # An empty fence deletes the lines; "\n" alone is one blank line.
        new = raw[:-1].split("\n") if raw else []
        repl = [ln.rstrip("\r") + (eol or "\n") for ln in new]
        if repl and not eol:
            repl[-1] = repl[-1][:-1]
        if repl == lines[s - 1:e]:
            t.update(status="left", why="the file already reads as the suggestion")
            floor = s
            continue
        lines[s - 1:e] = repl
        floor = s
        done.append((s, e, len(repl)))
        t.update(status="fixed", how="suggestion")
    if done:
        p.write_bytes("".join(lines).encode("utf-8"))
    return done


def shift(t, replaced):
    """Move an agent thread's lines past the suggestions applied above it, or
    say why it cannot be: its own lines were replaced."""
    s, e = t.get("start"), t.get("end")
    if e is None:
        return None
    s = s or e
    delta = 0
    for rs, rend, n in replaced:
        if rs <= e and s <= rend:
            return "a suggestion on the same lines was applied first"
        if rend < s:
            delta += n - (rend - rs + 1)
    t.update(start=s + delta, end=e + delta)
    return None


def digest(root, path):
    try:
        return hashlib.sha256((Path(root) / path).read_bytes()).hexdigest()
    except OSError:
        return None


def by_fixer(message):
    return any(ln.strip().startswith(TRAILER) for ln in (message or "").splitlines())


def plan(nodes, head, root, globs, head_message=""):
    if by_fixer(head_message):
        return []
    threads = easy_threads(nodes, head)
    by_path = {}
    for t in threads:
        fences = suggestions(t["body"])
        path = t["path"]
        problem = path_problem(root, path) if path else None
        if problem:
            t.update(status="not-taken", why=problem)
        elif path and is_risky(path, globs):
            t.update(status="not-taken", why=f"{path} is a risky path; the fixer never edits one")
        elif not fences:
            t.update(status="agent")
        elif len(fences) > 1:
            t.update(status="not-taken", why="more than one suggestion fence")
        elif not path or t["end"] is None:
            t.update(status="not-taken", why="a suggestion with no line to apply it to")
        elif t["side"] != "RIGHT":
            t.update(status="not-taken", why="the suggestion is on the old side of the diff")
        else:
            t["suggestion"] = fences[0]
            by_path.setdefault(path, []).append(t)
    replaced = {path: apply_file(root, path, ts) for path, ts in by_path.items()}
    for t in threads:
        t.pop("suggestion", None)
        if t.get("status") == "agent":
            why = shift(t, replaced.get(t["path"], []))
            if why:
                t.update(status="left", why=why)
            else:
                # The file as the agent finds it, so the gate can tell
                # whether the agent touched it, and, where a suggestion
                # changed it, undo the agent without undoing the suggestion.
                t["base"] = digest(root, t["path"])
                if replaced.get(t["path"]):
                    t["base_contents"] = base64.b64encode(
                        (Path(root) / t["path"]).read_bytes()).decode()
    return threads


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], check=True,
                          capture_output=True).stdout


def changed_tracked(root):
    """Tracked paths modified or deleted in the worktree: (path, deleted)."""
    out = git(root, "-c", "core.quotepath=off", "diff", "--no-renames",
              "--name-status", "-z", "HEAD")
    parts = out.decode("utf-8").split("\0")
    pairs = []
    for i in range(0, len(parts) - 1, 2):
        pairs.append((parts[i + 1], parts[i] == "D"))
    return pairs


def gate(threads, agent, root, globs):
    """Keep only changes a taken thread names, off the risky class; revert the
    rest. Returns the threads with final statuses and the files to commit."""
    answers = {a.get("thread_id"): a for a in agent if isinstance(a, dict)}
    agent_threads = [t for t in threads if t.get("status") == "agent"]
    unfinished = {t["path"] for t in agent_threads
                  if answers.get(t["thread_id"], {}).get("done") is not True}
    # A file the agent edited while leaving one of its threads unfinished
    # loses all the agent's edits: a half edit must not ride in on another
    # thread's fix.
    touched_unfinished = {p for p in unfinished
                          if any(t["path"] == p and t.get("base") != digest(root, p)
                                 for t in agent_threads)}
    # Where a suggestion changed the file first, put back the file as the
    # agent found it, so the suggestion fixes stay; elsewhere, back to HEAD.
    undone = {}
    for p in list(touched_unfinished):
        snap = next((t["base_contents"] for t in agent_threads
                     if t["path"] == p and t.get("base_contents")), None)
        if snap is not None and not path_problem(root, p):
            (Path(root) / p).write_bytes(base64.b64decode(snap))
            undone[p] = "the agent edited it but left a thread on it unfinished, so its edit was undone"
            touched_unfinished.discard(p)
    allowed = {t["path"] for t in threads if t.get("status") == "fixed"}
    allowed |= {t["path"] for t in agent_threads if t["path"] not in unfinished}
    kept, reverted = [], {}
    for path, deleted in changed_tracked(root):
        if is_risky(path, globs):
            why = "risky path"
        elif path in touched_unfinished:
            why = "the agent edited it but left a thread on it unfinished"
        elif path not in allowed:
            why = "no taken thread names this path"
        elif deleted:
            why = "a deletion"
        else:
            why = path_problem(root, path)
        if why:
            git(root, "checkout", "HEAD", "--", path)
            reverted[path] = why
        else:
            kept.append(path)
    for t in threads:
        st = t.get("status")
        if st == "fixed" and t["path"] not in kept:
            t.update(status="not-taken", why=f"its change was reverted: {reverted.get(t['path'], 'no change')}")
        elif st == "agent":
            a = answers.get(t["thread_id"])
            if a is None:
                t.update(status="left", why="the agent gave no answer for it")
            elif a.get("done") is not True:
                t.update(status="left", why=str(a.get("why") or "the agent did not take it"))
            elif t["path"] in undone:
                t.update(status="left", why=f"the agent said done but {undone[t['path']]}")
            elif t["path"] in kept:
                t.update(status="fixed", how="agent")
            else:
                t.update(status="left", why=f"the agent said done but {reverted.get(t['path'], 'changed nothing')}")
    additions = [{"path": p,
                  "contents": base64.b64encode((Path(root) / p).read_bytes()).decode()}
                 for p in kept]
    return {"threads": threads, "additions": additions,
            "reverted": [{"path": p, "why": w} for p, w in reverted.items()]}


def report(threads, sha, failure, sign, note=""):
    """One reply per thread and the roll-up. A fix without a commit is `left`."""
    replies = []
    for t in threads:
        if t["status"] == "fixed" and not sha:
            t.update(status="left", why=f"fixed in the runner but not committed: {failure or 'no commit'}")
        if t["status"] == "fixed":
            text = f"🤖 Fixed in {sha} ({'suggestion applied' if t.get('how') == 'suggestion' else 'agent edit'})."
        elif t["status"] == "not-taken":
            text = f"🤖 Not taken by the fixer: {t['why']}."
        else:
            text = f"🤖 Left for the author: {t.get('why') or 'not attempted'}."
        if note:
            text += f"\n\n{note}"
        replies.append({"comment_id": t["comment_id"], "body": f"{text}\n\n{sign}"})
    counts = {k: sum(t["status"] == k for t in threads) for k in ("fixed", "not-taken", "left")}
    md = ["### Claude fixer", "",
          f"fixed {counts['fixed']}, not taken {counts['not-taken']}, left {counts['left']}"
          + (f"; commit {sha}" if sha else "") + (f"; {note}" if note else ""), ""]
    if threads:
        md += ["| thread | path | outcome | why |", "|---|---|---|---|"]
        for t in threads:
            why = (t.get("why") or t.get("how") or "").replace("|", "\\|")
            md.append(f"| [{t['comment_id']}]({t.get('url', '')}) | {t.get('path') or ''} | {t['status']} | {why} |")
    return {"replies": replies, "summary": "\n".join(md) + "\n", "counts": counts}


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--threads", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--root", default=".")
    p.add_argument("--head-message", default="", help="file holding the head commit's message")
    p.add_argument("--out", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--plan", required=True)
    g.add_argument("--agent", required=True)
    g.add_argument("--root", default=".")
    g.add_argument("--out", required=True)
    r = sub.add_parser("report")
    r.add_argument("--gate", required=True)
    r.add_argument("--sha", default="")
    r.add_argument("--failure", default="")
    r.add_argument("--note", default="")
    r.add_argument("--sign", required=True)
    r.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    globs = risky_globs()
    if a.cmd == "plan":
        nodes = json.loads(Path(a.threads).read_text())
        msg = Path(a.head_message).read_text() if a.head_message else ""
        res = plan(nodes, a.head, a.root, globs, msg)
    elif a.cmd == "gate":
        try:
            agent = json.loads(Path(a.agent).read_text())
        except (OSError, ValueError):
            agent = []
        res = gate(json.loads(Path(a.plan).read_text()), agent if isinstance(agent, list) else [], a.root, globs)
    else:
        doc = json.loads(Path(a.gate).read_text())
        res = report(doc["threads"], a.sha, a.failure, a.sign, a.note)
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
