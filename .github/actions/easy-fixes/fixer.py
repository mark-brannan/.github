#!/usr/bin/env python3
"""The fixer job's mechanics, one subcommand per step of claude-review.yml's
`fixer` job. The agent and the GitHub calls live in the workflow; everything
that decides which thread is taken and which path may be committed lives here,
where the tests reach it.

  plan    the review threads (GraphQL nodes) -> this head's open [easy] threads,
          suggestion fences applied to the checkout and
          the agent's line numbers moved past them; a body-old/body-new
          fence pair replaced in the pull request's body text; nothing on
          a head the fixer itself committed
  gate    after the agent: every changed tracked path named by a
          taken thread is kept (unless the base branch changed it since the
          review), every other one reverted; a file the agent
          edited while leaving one of its threads unfinished goes back to how
          the agent found it, suggestion fixes and all; the kept
          files become
          createCommitOnBranch additions
  report  the commit's sha (or why there is none) -> one reply per thread and
          the roll-up for the job summary

When the branch conflicts with its base, the job merges the base in first and
these decide whether a small model may resolve what git left:

  conflicts  after `git merge`: the conflicted files, or the rule that sends
             the conflict to the author instead (see conflict_problem)
  resolved   after the model: whether every conflict is gone and nothing else
             changed, and the model's note per file
  merge-tree the merged commit as the Git Data API's tree entries
  merge-comment  the one signed PR comment on how the conflict went

No path is off limits by its name: a hook, a workflow or a config file is
fixed like any other. A path is refused only when it is unsafe to edit (see
path_problem). A push touching .github/workflows/ needs the agent App to hold
the `workflows` permission; without it GitHub rejects the commit and the
roll-up says so.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

EASY = re.compile(r"^\s*(?:🤖\s*)?\[(?:nit|should)\]\s*\[easy\]")
FENCE = re.compile(r"^```suggestion[ \t]*\r?\n(.*?)^```[ \t]*\r?$", re.S | re.M)
# A fix to the pull request's body: the reviewer gives the exact text there
# now and the text to put in its place. No model writes the body.
BODY_FENCE = re.compile(r"^```body-(old|new)[ \t]*\r?\n(.*?)^```[ \t]*\r?$", re.S | re.M)
BOT_LOGINS = {"claude", "claude[bot]"}
# Every fixer commit carries this trailer, and a head that does is not fixed
# again: the fixer never answers its own commit.
TRAILER = "Claude-Fixer-Run:"


# A thread's path reaches the agent's --allowedTools, so only plain path
# characters pass: a `,`, `(`, `)` or space could forge a tool grant.
SAFE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")


def path_problem(root, path):
    """Why the fixer will not touch this path, or None. Checked before any
    edit and again at commit: a symlink could carry an edit to wherever it
    points."""
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


def body_edit(body):
    """(old, new) from a thread's body-old/body-new fences, None when it has
    neither, or a string saying why the pair cannot be used."""
    found = [(m.group(1), m.group(2).replace("\r\n", "\n").rstrip("\n"))
             for m in BODY_FENCE.finditer(body)]
    if not found:
        return None
    if sorted(k for k, _ in found) != ["new", "old"]:
        return "a body edit needs one body-old fence and one body-new fence"
    pair = dict(found)
    if not pair["old"].strip():
        return "the body-old fence is empty, so there is nothing to find in the body"
    if suggestions(body):
        return "a body edit and a suggestion in one thread"
    return pair["old"], pair["new"]


def apply_body(threads, text):
    """Replace each `body` thread's old text in the pull request's body, in
    thread order. The old text must be there exactly once, as whole lines
    (part of a longer line does not count); otherwise the thread is left. `text` None means the body could not be read. Returns the
    new body, or None when nothing changed."""
    new = None if text is None else text.replace("\r\n", "\n")
    changed = False
    for t in threads:
        if t.get("status") != "body":
            continue
        old, to = t.pop("old"), t.pop("new")
        pat = re.compile(r"(?<![^\n])" + re.escape(old) + r"(?![^\n])")
        n = 0 if new is None else len(pat.findall(new))
        if new is None:
            t.update(status="left", why="the pull request's body could not be read")
        elif n != 1:
            t.update(status="left", why="the body-old text is not in the pull request's body as whole lines" if n == 0
                     else f"the body-old text is in the pull request's body {n} times, not once")
        elif old == to:
            t.update(status="left", why="the body already reads as the body-new text")
        else:
            new = pat.sub(lambda _: to, new, count=1)
            changed = True
    return new if changed else None


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


def plan(nodes, head, root, head_message="", moved=()):
    """`moved` is the paths a merge with the base changed in the checkout
    before the plan: the review's line numbers on them may no longer hold."""
    if by_fixer(head_message):
        return []
    threads = easy_threads(nodes, head)
    by_path = {}
    for t in threads:
        fences = suggestions(t["body"])
        path = t["path"]
        edit = body_edit(t["body"])
        problem = path_problem(root, path) if path else None
        if isinstance(edit, str):
            t.update(status="not-taken", why=edit)
        elif edit:
            t.update(status="body", old=edit[0], new=edit[1])
        elif problem:
            t.update(status="not-taken", why=problem)
        elif path in moved:
            t.update(status="not-taken", why="the merge with the base changed this file, so the review's lines may not hold")
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


def gate(threads, agent, root, moved=()):
    """Keep only changes a taken thread names; revert the
    rest. `moved` is the paths the base branch changed since the head the
    threads were read from: the commit lands on the updated head and carries
    whole files, so a kept file the base also changed would undo the base's
    change; it is reverted instead. Returns the threads with final statuses
    and the files to commit."""
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
        if path in moved:
            why = "the base branch changed it since the review, so the fix would overwrite that"
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


def report(threads, sha, failure, sign, note="", body_failure=""):
    """One reply per thread and the roll-up. A fix without a commit is `left`;
    so is a body edit that was not saved."""
    replies = []
    for t in threads:
        if t["status"] == "body":
            if body_failure:
                t.update(status="left", why=f"the pull request's body was not saved: {body_failure}")
            else:
                t.update(status="fixed", how="body")
        elif t["status"] == "fixed" and not sha:
            t.update(status="left", why=f"fixed in the runner but not committed: {failure or 'no commit'}")
        if t["status"] == "fixed" and t.get("how") == "body":
            text = "🤖 Fixed in the pull request's body (the body-old text replaced by the body-new text)."
        elif t["status"] == "fixed":
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


# The conflict path. A conflict goes to the model only when it is small and
# plain text; every rule below sends it to the author instead, with the branch
# left as it was.
MAX_CONFLICTS = 5
LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
             "poetry.lock", "Pipfile.lock", "uv.lock", "Cargo.lock", "Gemfile.lock",
             "composer.lock", "flake.lock"}
GENERATED = re.compile(r"(?:^|/)(?:vendor|vendored|third_party|node_modules|dist|generated)/"
                       r"|\.min\.(?:js|css)$|\.pb\.go$|_pb2\.py$|\.snap$")
# `=======` is left out: it is also a Markdown heading underline.
MARKER = re.compile(rb"^(?:<{7}|>{7}|\|{7})(?: |\r?$)", re.M)
NOTE_MAX = 200
# claude-code-action checks these out from the base and resets them in the
# index before the model starts (its restore-config), so a conflict in one
# would reach the model already overwritten.
ACTION_RESETS = (".claude", ".mcp.json", ".claude.json", ".gitmodules", ".ripgreprc",
                 "CLAUDE.md", "CLAUDE.local.md", ".husky")


def unmerged(root):
    """git's unmerged index entries: {path: {stage: (mode, sha)}}."""
    out = git(root, "-c", "core.quotepath=off", "ls-files", "-u", "-z").decode("utf-8")
    entries = {}
    for rec in filter(None, out.split("\0")):
        meta, path = rec.split("\t", 1)
        mode, sha, stage = meta.split()
        entries.setdefault(path, {})[int(stage)] = (mode, sha)
    return entries


def attrs(root, paths):
    """{path: {attr: value}} for the attributes that bear on a merge."""
    if not paths:
        return {}
    out = git(root, "check-attr", "-z", "binary", "merge", "linguist-generated",
              "linguist-vendored", "--", *paths).decode("utf-8").split("\0")
    res = {}
    for i in range(0, len(out) - 2, 3):
        res.setdefault(out[i], {})[out[i + 1]] = out[i + 2]
    return res


def conflict_problem(root, path, stages, attr):
    """Why this conflicted file goes to the author, not the model, or None."""
    problem = path_problem(root, path)
    if problem:
        return problem
    if any(path == p or path.startswith(p + "/") for p in ACTION_RESETS):
        return "a path claude-code-action resets from the base before the model runs"
    if 2 not in stages or 3 not in stages:
        return "one side deleted or renamed it"
    if any(mode in ("120000", "160000") for mode, _ in stages.values()):
        return "a symlink or submodule conflict"
    name = path.rsplit("/", 1)[-1]
    if name in LOCKFILES or name.endswith(".sum"):
        return "a lockfile, regenerated rather than merged"
    if GENERATED.search(path) or attr.get("linguist-generated") in ("set", "true") \
            or attr.get("linguist-vendored") in ("set", "true"):
        return "a vendored or generated file"
    if attr.get("binary") == "set" or attr.get("merge") in ("unset", "binary"):
        return "a binary file"
    for _, sha in stages.values():
        blob = git(root, "cat-file", "blob", sha)
        if b"\0" in blob[:8000]:
            return "a binary file"
        if MARKER.search(blob):
            return "it has conflict-marker lines of its own, so a leftover could not be told"
    try:
        text = (Path(root) / path).read_bytes()
    except OSError:
        return "git left no file to resolve"
    if not MARKER.search(text):
        return "git left no conflict markers in it"
    return None


def conflicts(root, max_files=MAX_CONFLICTS):
    """After `git merge --no-commit`: the conflicted files, and whether the
    model may resolve them (`resolve`), git merged cleanly (`clean`), or a rule
    sends them to the author (`escalate`, with the rule and each file's why)."""
    entries = unmerged(root)
    files = sorted(entries)
    if not files:
        return {"status": "clean", "files": []}
    if len(files) > max_files:
        return {"status": "escalate", "files": files,
                "rule": f"more than {max_files} conflicted files ({len(files)})", "problems": {}}
    at = attrs(root, [f for f in files if SAFE_PATH.match(f)])
    problems = {}
    for f in files:
        why = conflict_problem(root, f, entries[f], at.get(f, {}))
        if why:
            problems[f] = why
    if problems:
        return {"status": "escalate", "files": files, "problems": problems,
                "rule": "a conflicted file the fixer leaves to the author"}
    hunks = {f: len(re.findall(rb"^<{7}(?: |\r?$)", (Path(root) / f).read_bytes(), re.M))
             for f in files}
    return {"status": "resolve", "files": files, "hunks": hunks}


def notes(answers, files):
    """The model's one line per file, as plain text a comment can carry."""
    out = {}
    for ln in (answers or "").splitlines():
        path, sep, text = ln.partition(":")
        path = path.strip().strip("`")
        if sep and path in files and path not in out:
            text = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip().replace("@", "@\u200b")
            text = text.replace("|", "\\|")
            out[path] = text[:NOTE_MAX] or "no note"
    return {f: out.get(f, "no note from the model") for f in files}


def resolved(root, files, answers=""):
    """After the model: every conflicted file still there, free of markers and
    safe to commit. Any other tracked file changed in the worktree goes back
    to the merge's version in the index, as the gate reverts what no thread
    names: the model can edit only the conflicted files, so such a change is
    the action's own (its restore-config) or nobody's to make."""
    problems = {}
    for f in files:
        why = path_problem(root, f)
        if not why:
            try:
                if MARKER.search((Path(root) / f).read_bytes()):
                    why = "the model left conflict markers in it"
            except OSError:
                why = "the model deleted it"
        if why:
            problems[f] = why
    out = git(root, "-c", "core.quotepath=off", "diff", "--name-only", "-z").decode("utf-8")
    reverted = sorted(set(filter(None, out.split("\0"))) - set(files))
    for p in reverted:
        git(root, "-c", "core.hooksPath=/dev/null", "checkout", "--", p)
    if problems:
        rule = ("the model left a conflict unresolved"
                if any("markers" in w for w in problems.values())
                else "the resolved tree failed the fixer's gate")
        return {"ok": False, "files": files, "rule": rule, "problems": problems, "reverted": reverted}
    return {"ok": True, "files": files, "notes": notes(answers, files), "reverted": reverted}


def merge_tree(root, base, head):
    """The local merge commit (HEAD) as Git Data API input: tree entries on
    top of the base's tree for every path the merge's tree differs from it,
    and the blobs neither parent holds, which must be uploaded first."""
    out = git(root, "-c", "core.quotepath=off", "diff", "--no-renames", "--raw", "--full-index", "--no-abbrev", "-z",
              base, "HEAD").decode("utf-8").split("\0")
    entries, upload = [], []
    paths = []
    for i in range(0, len(out) - 1, 2):
        if not out[i].startswith(":"):
            break
        old_mode, new_mode, _, sha, status = out[i][1:].split()
        path = out[i + 1]
        paths.append(path)
        if status == "D":
            entries.append({"path": path, "mode": old_mode,
                            "type": "commit" if old_mode == "160000" else "blob", "sha": None})
        else:
            entries.append({"path": path, "mode": new_mode,
                            "type": "commit" if new_mode == "160000" else "blob", "sha": sha})
    in_head = {}
    if paths:
        ls = git(root, "-c", "core.quotepath=off", "ls-tree", "-z", head, "--", *paths).decode("utf-8")
        for rec in filter(None, ls.split("\0")):
            meta, path = rec.split("\t", 1)
            in_head[path] = meta.split()[2]
    upload = [e["path"] for e in entries
              if e["sha"] and e["type"] == "blob" and in_head.get(e["path"]) != e["sha"]]
    return {"base_tree": git(root, "rev-parse", f"{base}^{{tree}}").decode().strip(),
            "tree": git(root, "rev-parse", "HEAD^{tree}").decode().strip(),
            "entries": entries, "upload": upload}


def merge_comment(state, base, sign, marker, sha="", failure=""):
    """The one PR comment on the conflict: resolved and pushed, resolved but
    not pushed, or left to the author with the rule that stopped it."""
    files = state.get("files") or []
    if state.get("ok") and sha:
        lines = [f"🤖 The fixer merged `{base}` into this branch in {sha} and resolved "
                 f"the conflicts in {len(files)} file(s):", "", "| file | how |", "|---|---|"]
        lines += [f"| `{f}` | {n} |" for f, n in (state.get("notes") or {}).items()]
        lines += ["", "The merge runs CI and the review like any other push."]
    else:
        if state.get("ok"):
            head = (f"🤖 The fixer resolved the conflicts with `{base}` but could not push "
                    f"the merge: {failure or 'no commit'}.")
            rows = [(f, n) for f, n in (state.get("notes") or {}).items()]
        else:
            head = (f"🤖 The fixer did not resolve this branch's conflicts with `{base}`: "
                    f"{state.get('rule') or 'the fixer stopped before checking a resolution'}.")
            probs = state.get("problems") or {}
            rows = [(f, probs.get(f, "conflicted")) for f in files]
            rows += [(f, w) for f, w in probs.items() if f not in files]
        lines = [head, "", "| file | why |", "|---|---|"] + [f"| `{f}` | {w} |" for f, w in rows]
        lines += ["", "The branch is as it was; the conflicts are the author's to resolve."]
    return "\n".join(lines + ["", marker, sign]) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--threads", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--root", default=".")
    p.add_argument("--head-message", default="", help="file holding the head commit's message")
    p.add_argument("--moved", help="file of paths (one per line) a merge with the base changed")
    p.add_argument("--pr-body", help="file holding the pull request's body")
    p.add_argument("--body-out", help="where to write the body after the body edits; not written when none applied")
    p.add_argument("--out", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--plan", required=True)
    g.add_argument("--agent", required=True)
    g.add_argument("--root", default=".")
    g.add_argument("--moved", help="file of paths (one per line) the base changed since the review's head")
    g.add_argument("--out", required=True)
    r = sub.add_parser("report")
    r.add_argument("--gate", required=True)
    r.add_argument("--sha", default="")
    r.add_argument("--failure", default="")
    r.add_argument("--note", default="")
    r.add_argument("--body-failure", default="", help="why the edited body was not saved")
    r.add_argument("--sign", required=True)
    r.add_argument("--out", required=True)
    c = sub.add_parser("conflicts")
    c.add_argument("--root", default=".")
    c.add_argument("--max-files", type=int, default=MAX_CONFLICTS)
    c.add_argument("--out", required=True)
    v = sub.add_parser("resolved")
    v.add_argument("--conflicts", required=True)
    v.add_argument("--answers", default="")
    v.add_argument("--root", default=".")
    v.add_argument("--out", required=True)
    t = sub.add_parser("merge-tree")
    t.add_argument("--base", required=True)
    t.add_argument("--head", required=True)
    t.add_argument("--root", default=".")
    t.add_argument("--out", required=True)
    m = sub.add_parser("merge-comment")
    m.add_argument("--state", required=True, help="conflicts.json or resolved.json")
    m.add_argument("--base", required=True)
    m.add_argument("--sign", required=True)
    m.add_argument("--marker", required=True)
    m.add_argument("--sha", default="")
    m.add_argument("--failure", default="")
    m.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "conflicts":
        res = conflicts(a.root, a.max_files)
    elif a.cmd == "resolved":
        try:
            answers = Path(a.answers).read_text(errors="replace") if a.answers else ""
        except OSError:
            answers = ""
        res = resolved(a.root, json.loads(Path(a.conflicts).read_text())["files"], answers)
    elif a.cmd == "merge-tree":
        res = merge_tree(a.root, a.base, a.head)
    elif a.cmd == "merge-comment":
        Path(a.out).write_text(merge_comment(json.loads(Path(a.state).read_text()), a.base,
                                             a.sign, a.marker, a.sha, a.failure))
        return 0
    elif a.cmd == "plan":
        nodes = json.loads(Path(a.threads).read_text())
        msg = Path(a.head_message).read_text() if a.head_message else ""
        try:
            moved = set(Path(a.moved).read_text().splitlines()) if a.moved else set()
        except OSError:
            moved = set()
        res = plan(nodes, a.head, a.root, msg, moved)
        try:
            pr_body = Path(a.pr_body).read_text() if a.pr_body else None
        except OSError:
            pr_body = None
        new_body = apply_body(res, pr_body)
        if new_body is not None and a.body_out:
            Path(a.body_out).write_text(new_body)
    elif a.cmd == "gate":
        try:
            agent = json.loads(Path(a.agent).read_text())
        except (OSError, ValueError):
            agent = []
        try:
            moved = set(Path(a.moved).read_text().splitlines()) if a.moved else set()
        except OSError:
            moved = set()
        res = gate(json.loads(Path(a.plan).read_text()), agent if isinstance(agent, list) else [], a.root, moved)
    else:
        doc = json.loads(Path(a.gate).read_text())
        res = report(doc["threads"], a.sha, a.failure, a.sign, a.note, a.body_failure)
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
