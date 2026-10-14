#!/usr/bin/env python3
# review_metrics: what the shared Claude review costs and what it buys
# (mark-brannan/.github#70). A measurement, never a gate.
#
# Spend: every run of each repo's review workflow in the window, read from its
# job log, where claude-code-action prints the session's init message (the
# model) and its result message (total_cost_usd, turns, wall time), and where
# the triage step (#69) leaves its "review triage: <tier>" notice and its
# path classes. Only GitHub's own lines are read for those: the PR body is
# echoed into the log too, and may quote them. A run with no result message
# spent nothing: the review step was skipped, on the fingerprint, the triage
# or the budget. Under an OAuth token total_cost_usd
# is the API-equivalent price, not a bill; it still compares runs fairly.
#
# Outcome: every review thread claude[bot] opened on those pull requests,
# classed as #70 defines it:
#   judgment  the first comment opens "Judgment (": it went to a human
#   fixed     resolved, and a later commit changed the anchored lines (the
#             thread is outdated) or a reply says fixed
#   declined  resolved, lines unchanged, with a reply that disagrees or marks
#             a reversal
#   ignored   unresolved and never answered, on a merged pull request
#   open      unresolved on an open pull request
#   other     resolved silently with the lines unchanged; outdated and
#             unresolved, a fix nobody confirmed; answered but left open on
#             a merged one; or left on a pull request closed unmerged
# A reply from another bot is not an answer. The first comment's 👍 and 👎
# are counted, the cheaper human signal #70 names.
# Each thread is charged to the last paid run of its PR started before it, so it
# carries that run's model and tier.
#
# Usage: review_metrics.py [--owner LOGIN] [--repos a,b] [--days N]
#                          [--workflow FILE] [--csv PATH] [--threads-csv PATH]
#                          [--raw PATH | --from PATH] [--force]
#
# --repos defaults to every non-archived repo of --owner whose workflow FILE
# (default claude-code-review.yml) exists. Markdown goes to stdout; the CSVs
# hold one row per run and one per thread. --raw also keeps everything read,
# as JSON lines: every run, and every bot thread with each comment in full,
# its author and reactions. --from rebuilds the report from such a file with
# no network, classing the threads by the rules as they are now, so a change
# to the classes re-reads old data instead of fetching it again. Needs `gh`, authenticated; one log
# download per run, so a 30-day window over ten repos is a few hundred calls.
# Python 3 stdlib only.
import argparse
import csv
import datetime as dt
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

NAME = "review_metrics"
sys.dont_write_bytecode = True
BOT = "claude"  # the GraphQL login of claude[bot]
WORKERS = 2  # log downloads in flight; GitHub's secondary rate limit trips on more
STAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
TRIAGE = re.compile(r"^##\[notice\]review triage: (skip|default|deep) \((.*)\)\s*$")
EFFORT = re.compile(r"^\s+claude_args: .*--effort\s+([a-z]+)")
CLASSES = re.compile(r"^\s+CLASSES: ((?:[a-z]+=\d+ ?)+)$")
FIXED = re.compile(r"\b(fixed|done|addressed|applied|resolved in|in [0-9a-f]{7,40})\b", re.I)
DECLINED = re.compile(r"\b(no|not|won't|wontfix|disagree|by design|intended|intentional|keep(ing)?|kept|"
                      r"revers(al|ed)|out of scope|stays?)\b", re.I)
JUDGMENT = re.compile(r"^\W*judgment\s*\(", re.I)


def die(msg):
    print("%s: %s" % (NAME, msg), file=sys.stderr)
    sys.exit(2)


def gh(*args, soft=False):
    """gh's stdout. A 404, or a 410 for a log past the retention window, is
    None: gone, not an error. Any other failure is retried twice, then None
    when soft (a fact the report can go without), else fatal."""
    for attempt in range(3):
        p = subprocess.run(["gh", *args], capture_output=True, encoding="utf-8", errors="replace")
        if not p.returncode:
            return p.stdout
        if "404" in p.stderr or "410" in p.stderr:
            return None
        if "rate limit" in p.stderr.lower():
            # Never soft: a fact dropped here would read as a run that never happened.
            die("rate limited; the hourly API budget is shared with every session, so rerun after it resets")
        time.sleep(2 * (attempt + 1))
    if soft:
        print("%s: gave up on gh %s: %s" % (NAME, args[-1], p.stderr.strip()), file=sys.stderr)
        return None
    die("gh %s: %s" % (" ".join(args[:3]), p.stderr.strip()))


def gh_json(*args, soft=False):
    out = gh(*args, soft=soft)
    try:
        return None if out is None else json.loads(out)
    except ValueError:
        if soft:
            return None
        die("gh %s: not JSON" % " ".join(args[:3]))


def parse_log(text):
    """The run's model, cost, turns, wall time, tier and effort, from one job log."""
    lines = [STAMP.sub("", ln) for ln in text.splitlines()]
    out = {"model": "", "cost": None, "turns": None, "ms": None, "tier": "", "why": "", "effort": "",
           "paths": ""}
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln == "{":
            j = i + 1
            while j < len(lines) and lines[j] != "}":
                j += 1
            try:
                msg = json.loads("\n".join(lines[i:j + 1]))
            except ValueError:
                msg = None
            if isinstance(msg, dict) and msg.get("type") == "system" and msg.get("subtype") == "init":
                out["model"] = msg.get("model") or out["model"]
            elif isinstance(msg, dict) and msg.get("type") == "result":
                out["cost"] = msg.get("total_cost_usd")
                out["turns"] = msg.get("num_turns")
                out["ms"] = msg.get("duration_ms")
            i = j + 1
            continue
        m = TRIAGE.search(ln)
        if m:
            out["tier"], out["why"] = m.group(1), m.group(2)
        m = EFFORT.search(ln)
        if m:
            out["effort"] = m.group(1)
        m = CLASSES.search(ln)
        if m:
            out["paths"] = "+".join(k for k, v in (kv.split("=") for kv in m.group(1).split()) if v != "0") or "none"
        i += 1
    return out


def classify(thread, pr_state):
    first = thread["comments"][0]["body"] if thread["comments"] else ""
    replies = [c for c in thread["comments"][1:] if c["author"] != BOT and not c.get("bot")]
    if JUDGMENT.match(first):
        return "judgment"
    said_fixed = any(FIXED.search(c["body"]) for c in replies)
    if thread["resolved"]:
        if thread["outdated"] or said_fixed:
            return "fixed"
        return "declined" if any(DECLINED.search(c["body"]) for c in replies) else "other"
    if thread["outdated"]:
        return "other"
    if pr_state == "OPEN":
        return "open"
    return "ignored" if pr_state == "MERGED" and not replies else "other"


THREADS_Q = """
query($owner: String!, $name: String!, $pr: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      state
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved isOutdated path
          comments(first: 50) {
            pageInfo { hasNextPage }
            nodes { author { __typename login } createdAt body
              reactionGroups { content reactors { totalCount } } } } } } } } }"""


def threads(repo, pr):
    owner, name = repo.split("/")
    found, after = [], None
    while True:
        args = ["api", "graphql", "-F", "owner=" + owner, "-F", "name=" + name, "-F", "pr=%d" % pr,
                "-f", "query=" + THREADS_Q]
        if after:
            args += ["-f", "after=" + after]
        p = gh_json(*args)["data"]["repository"]["pullRequest"]
        for t in p["reviewThreads"]["nodes"]:
            if t["comments"]["pageInfo"]["hasNextPage"]:
                print("%s: %s#%d: a thread past 50 comments; later replies not read" % (NAME, repo, pr),
                      file=sys.stderr)
            cs = [{"author": (c["author"] or {}).get("login", ""), "bot": (c["author"] or {}).get("__typename") == "Bot",
                   "at": c["createdAt"], "body": c["body"],
                   "reactions": {g["content"]: g["reactors"]["totalCount"] for g in c.get("reactionGroups") or []}}
                  for c in t["comments"]["nodes"]]
            if cs and cs[0]["author"] == BOT:
                found.append({"resolved": t["isResolved"], "outdated": t["isOutdated"], "path": t["path"],
                              "at": cs[0]["at"], "comments": cs})
        page = p["reviewThreads"]["pageInfo"]
        if not page["hasNextPage"]:
            return p["state"], found
        after = page["endCursor"]


def runs_of(repo, workflow, since):
    q = "repos/%s/actions/workflows/%s/runs?per_page=100&created=%%3E%%3D%s" % (repo, workflow, since)
    got = gh_json("api", "--paginate", "--slurp", q)
    return [r for page in (got or []) for r in page.get("workflow_runs", [])]


def pr_of(repo, run):
    if run.get("pull_requests"):
        return run["pull_requests"][0]["number"]
    prs = gh_json("api", "repos/%s/commits/%s/pulls" % (repo, run["head_sha"]), soft=True) or []
    if not prs:
        # A squash-merged PR's head commit is often no longer associated.
        prs = gh_json("api", "repos/%s/pulls?state=all&head=%s:%s" % (
            repo, run["head_repository"]["owner"]["login"], run["head_branch"]), soft=True) or []
    return prs[0]["number"] if prs else None


def rows_of(repo, run):
    jobs = (gh_json("api", "repos/%s/actions/runs/%d/jobs" % (repo, run["id"]), soft=True) or {}).get("jobs", [])
    pr = pr_of(repo, run)
    rows = []
    for job in jobs:
        log = gh("api", "--allow-escape-sequences", "repos/%s/actions/jobs/%d/logs" % (repo, job["id"]),
                 soft=True)
        facts = parse_log(log or "")
        rows.append({"repo": repo, "pr": pr, "run": run["id"], "job": job["name"],
                     "conclusion": (job.get("conclusion") or "") if log is not None else "log unavailable",
                     "started": job.get("started_at") or "", "ended": job.get("completed_at") or "",
                     "ran": facts["cost"] is not None, **facts})
    return rows


def collect(repo, runs):
    with ThreadPoolExecutor(WORKERS) as pool:
        return [row for rows in pool.map(lambda r: rows_of(repo, r), runs) for row in rows]


def default_repos(owner, workflow):
    names = gh_json("repo", "list", owner, "--no-archived", "--limit", "200", "--json", "nameWithOwner")
    found = []
    for n in names:
        if gh("api", "repos/%s/contents/.github/workflows/%s" % (n["nameWithOwner"], workflow), soft=True):
            found.append(n["nameWithOwner"])
    return found


def charge(runs, thread):
    """The run that posted this thread: the last ran one of its PR started before it."""
    best = None
    for r in runs:
        if r["ran"] and r["started"] <= thread["at"] and (best is None or r["started"] > best["started"]):
            best = r
    return best


def money(x):
    return "$%.2f" % x


def table(title, key, runs, ths):
    # key gives a run's or a thread's groups (a thread carries its run's
    # model, tier and paths); one in several path classes counts in each.
    groups = sorted({g for r in runs if r["ran"] for g in key(r)} | {g for t in ths for g in key(t)})
    out = ["", "### By %s" % title, "",
           "| %s | runs | spend | per run | threads | fixed | declined | ignored | judgment | per fixed |" % title,
           "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for g in groups:
        rs = [r for r in runs if r["ran"] and g in key(r)]
        ts = [t for t in ths if g in key(t)]
        spend = sum(r["cost"] or 0 for r in rs)
        n = defaultdict(int)
        for t in ts:
            n[t["class"]] += 1
        out.append("| %s | %d | %s | %s | %d | %d | %d | %d | %d | %s |" % (
            g or "-", len(rs), money(spend), money(spend / len(rs)) if rs else "-", len(ts), n["fixed"],
            n["declined"], n["ignored"], n["judgment"], money(spend / n["fixed"]) if n["fixed"] else "-"))
    return out


def markdown(runs, ths, since):
    ran = [r for r in runs if r["ran"]]
    spend = sum(r["cost"] or 0 for r in ran)
    prs = {(r["repo"], r["pr"]) for r in ran}
    n = defaultdict(int)
    for t in ths:
        n[t["class"]] += 1
    out = ["## Claude review: spend and outcome since %s" % since, "",
           "%d review runs, %d of them spent tokens, on %d pull requests: %s API-equivalent, %s per review." % (
               len(runs), len(ran), len(prs), money(spend), money(spend / len(ran)) if ran else "-"),
           "%d bot threads: %s." % (len(ths), ", ".join("%d %s" % (n[k], k) for k in
                                    ("fixed", "declined", "ignored", "judgment", "open", "other") if n[k])),
           "Per fixed thread: %s." % (money(spend / n["fixed"]) if n["fixed"] else "no fixed threads"),
           "Reactions on the bot's first comments: %d 👍, %d 👎." % (
               sum(t["up"] for t in ths), sum(t["down"] for t in ths))]
    out += table("model", lambda r: [r["model"]], runs, ths)
    out += table("tier", lambda r: [r["tier"] or "pre-triage"], runs, ths)
    out += table("path class", lambda r: (r["paths"] or "pre-triage").split("+"), runs, ths)
    out += ["", "A pull request touching several path classes counts in each, so that table sums past the total."]
    out += table("repo", lambda r: [r["repo"]], runs, ths)
    skipped = defaultdict(int)
    for r in runs:
        if not r["ran"]:
            if r["tier"] == "skip":
                why = "skip: %s" % r["why"]
            elif r["conclusion"] == "success":
                why = "review step skipped (fingerprint or no token)"
            else:
                why = r["conclusion"] or "unknown"
            skipped[why] += 1
    if skipped:
        out += ["", "### Runs that spent nothing", "", "| why | runs |", "|---|--:|"]
        out += ["| %s | %d |" % (k, v) for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])]
    return "\n".join(out) + "\n"


def write_csv(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def thread_row(t):
    """A raw thread as the report and the CSV read it, classed now."""
    first = t["comments"][0]
    return {**{k: t[k] for k in ("repo", "pr", "path", "at", "run", "model", "tier", "paths")},
            "class": classify(t, t["pr_state"]),
            "up": first.get("reactions", {}).get("THUMBS_UP", 0),
            "down": first.get("reactions", {}).get("THUMBS_DOWN", 0),
            "first_line": (first["body"].splitlines() or [""])[0][:120]}


def fetch(repos, workflow, since, force):
    listed = {repo: [r for r in runs_of(repo, workflow, since) if r.get("event") == "pull_request"]
              for repo in repos}
    # About three REST calls a run (jobs, log, a PR lookup); threads go to GraphQL's own budget.
    need = 3 * sum(len(v) for v in listed.values())
    left = (gh_json("api", "rate_limit") or {}).get("resources", {}).get("core", {}).get("remaining", 0)
    print("%s: %d runs, about %d API calls of the %d left this hour" % (
        NAME, need // 3, need, left), file=sys.stderr)
    if need > left // 2 and not force:
        die("that is more than half the hourly budget every session shares; "
            "narrow --days or --repos, or pass --force")
    runs, raw = [], []
    for repo in repos:
        print("%s: %s" % (NAME, repo), file=sys.stderr)
        rs = collect(repo, listed[repo])
        runs += rs
        by_pr = defaultdict(list)
        for r in rs:
            if r["pr"] is not None:
                by_pr[r["pr"]].append(r)
        for pr, prs in sorted(by_pr.items()):
            if not any(r["ran"] for r in prs):
                continue
            state, found = threads(repo, pr)
            for t in found:
                r = charge(prs, t) or {}
                raw.append({**t, "repo": repo, "pr": pr, "pr_state": state, "run": r.get("run", ""),
                            "model": r.get("model", ""), "tier": r.get("tier", "") or "pre-triage",
                            "paths": r.get("paths", "") or "pre-triage"})
    return runs, raw


def main():
    ap = argparse.ArgumentParser(prog=NAME)
    ap.add_argument("--owner", default="mark-brannan")
    ap.add_argument("--repos", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--workflow", default="claude-code-review.yml")
    ap.add_argument("--csv")
    ap.add_argument("--threads-csv")
    ap.add_argument("--force", action="store_true", help="spend past half the hourly API budget")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--raw", help="also write everything read, as JSON lines")
    src.add_argument("--from", dest="source", help="report from a --raw file, no network")
    a = ap.parse_args()
    if a.source:
        with open(a.source) as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        since = next((ln["since"] for ln in lines if ln["kind"] == "meta"), "?")
        runs = [ln for ln in lines if ln["kind"] == "run"]
        raw = [ln for ln in lines if ln["kind"] == "thread"]
    else:
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)).strftime("%Y-%m-%d")
        repos = [r if "/" in r else "%s/%s" % (a.owner, r) for r in a.repos.split(",") if r] \
            or default_repos(a.owner, a.workflow)
        runs, raw = fetch(repos, a.workflow, since, a.force)
        if a.raw:
            taken = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(a.raw, "w") as f:
                f.write(json.dumps({"kind": "meta", "since": since, "taken": taken, "repos": repos}) + "\n")
                for r in runs:
                    f.write(json.dumps({"kind": "run", **r}) + "\n")
                for t in raw:
                    f.write(json.dumps({"kind": "thread", **t}) + "\n")
    ths = [thread_row(t) for t in raw]
    sys.stdout.write(markdown(runs, ths, since))
    if a.csv:
        write_csv(a.csv, runs, ["repo", "pr", "run", "job", "conclusion", "started", "ended", "ran", "model",
                                "effort", "tier", "why", "paths", "cost", "turns", "ms"])
    if a.threads_csv:
        write_csv(a.threads_csv, ths, ["repo", "pr", "run", "at", "class", "model", "tier", "paths", "path",
                                       "up", "down", "first_line"])


if __name__ == "__main__":
    main()
