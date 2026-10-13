#!/usr/bin/env python3
# review_metrics: what the shared Claude review costs and what it buys
# (mark-brannan/.github#70). A measurement, never a gate.
#
# Spend: every run of each repo's review workflow in the window, read from its
# job log, where claude-code-action prints the session's init message (the
# model) and its result message (total_cost_usd, turns, wall time), and where
# the triage step (#69) leaves its "review triage: <tier>" notice. A run with
# no result message spent nothing: the review step was skipped, on the
# fingerprint, the triage or the budget. Under an OAuth token total_cost_usd
# is the API-equivalent price, not a bill; it still compares runs fairly.
#
# Outcome: every review thread claude[bot] opened on those pull requests,
# classed as #70 defines it:
#   judgment  the first comment opens "Judgment (": it went to a human
#   fixed     resolved, and a later commit changed the anchored lines (the
#             thread is outdated) or a reply says fixed
#   declined  resolved, lines unchanged, with a human reply
#   ignored   unresolved on a merged or closed pull request
#   open      unresolved on an open pull request
#   other     resolved silently with the lines unchanged; or outdated and
#             unresolved: a fix nobody confirmed
# Each thread is charged to the run whose window holds its creation, so it
# carries that run's model and tier.
#
# Usage: review_metrics.py [--owner LOGIN] [--repos a,b] [--days N]
#                          [--workflow FILE] [--csv PATH] [--threads-csv PATH]
#
# --repos defaults to every non-archived repo of --owner whose workflow FILE
# (default claude-code-review.yml) exists. Markdown goes to stdout; the CSVs
# hold one row per run and one per thread. Needs `gh`, authenticated; one log
# download per run, so a 30-day window over ten repos is a few hundred calls.
# Python 3 stdlib only.
import argparse
import csv
import datetime as dt
import json
import re
import subprocess
import sys
from collections import defaultdict

NAME = "review_metrics"
sys.dont_write_bytecode = True
BOT = "claude"  # the GraphQL login of claude[bot]
STAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
TRIAGE = re.compile(r"review triage: (skip|default|deep) \((.*)\)\s*$")
EFFORT = re.compile(r"--effort\s+([a-z]+)")
FIXED = re.compile(r"\b(fixed|done|addressed|applied|resolved in|in [0-9a-f]{7,40})\b", re.I)
JUDGMENT = re.compile(r"^\W*judgment\s*\(", re.I)


def die(msg):
    print("%s: %s" % (NAME, msg), file=sys.stderr)
    sys.exit(2)


def gh(*args, ok404=False):
    p = subprocess.run(["gh", *args], capture_output=True, encoding="utf-8", errors="replace")
    if p.returncode:
        if ok404 and "404" in p.stderr:
            return None
        die("gh %s: %s" % (" ".join(args[:3]), p.stderr.strip()))
    return p.stdout


def gh_json(*args, ok404=False):
    out = gh(*args, ok404=ok404)
    return None if out is None else json.loads(out)


def pages(path):
    # --paginate --slurp: one JSON array of every page's body.
    return gh_json("api", "--paginate", "--slurp", path) or []


def parse_log(text):
    """The run's model, cost, turns, wall time, tier and effort, from one job log."""
    lines = [STAMP.sub("", ln) for ln in text.splitlines()]
    out = {"model": "", "cost": None, "turns": None, "ms": None, "tier": "", "why": "", "effort": ""}
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
        i += 1
    return out


def classify(thread, pr_state):
    first = thread["comments"][0]["body"] if thread["comments"] else ""
    replies = [c for c in thread["comments"][1:] if c["author"] != BOT]
    if JUDGMENT.match(first):
        return "judgment"
    said_fixed = any(FIXED.search(c["body"]) for c in replies)
    if thread["resolved"]:
        if thread["outdated"] or said_fixed:
            return "fixed"
        return "declined" if replies else "other"
    if thread["outdated"]:
        return "other"
    return "open" if pr_state == "OPEN" else "ignored"


THREADS_Q = """
query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      state
      reviewThreads(first: 100) {
        nodes { isResolved isOutdated path
          comments(first: 20) { nodes { author { login } createdAt body } } } } } } }"""


def threads(repo, pr):
    owner, name = repo.split("/")
    d = gh_json("api", "graphql", "-F", "owner=" + owner, "-F", "name=" + name, "-F", "pr=%d" % pr,
                "-f", "query=" + THREADS_Q)
    p = d["data"]["repository"]["pullRequest"]
    found = []
    for t in p["reviewThreads"]["nodes"]:
        cs = [{"author": (c["author"] or {}).get("login", ""), "at": c["createdAt"], "body": c["body"]}
              for c in t["comments"]["nodes"]]
        if cs and cs[0]["author"] == BOT:
            found.append({"resolved": t["isResolved"], "outdated": t["isOutdated"], "path": t["path"],
                          "at": cs[0]["at"], "comments": cs})
    return p["state"], found


def runs_of(repo, workflow, since):
    q = "repos/%s/actions/workflows/%s/runs?per_page=100&created=%%3E%%3D%s" % (repo, workflow, since)
    got = gh_json("api", "--paginate", "--slurp", q, ok404=True)
    return [r for page in (got or []) for r in page.get("workflow_runs", [])]


def pr_of(repo, run):
    if run.get("pull_requests"):
        return run["pull_requests"][0]["number"]
    prs = gh_json("api", "repos/%s/commits/%s/pulls" % (repo, run["head_sha"]), ok404=True) or []
    if not prs:
        # A squash-merged PR's head commit is often no longer associated.
        prs = gh_json("api", "repos/%s/pulls?state=all&head=%s:%s" % (
            repo, run["head_repository"]["owner"]["login"], run["head_branch"]), ok404=True) or []
    return prs[0]["number"] if prs else None


def collect(repo, workflow, since):
    rows = []
    for run in runs_of(repo, workflow, since):
        if run.get("event") != "pull_request":
            continue
        jobs = gh_json("api", "repos/%s/actions/runs/%d/jobs" % (repo, run["id"]))["jobs"]
        for job in jobs:
            log = gh("api", "--allow-escape-sequences", "repos/%s/actions/jobs/%d/logs" % (repo, job["id"]),
                     ok404=True) or ""
            facts = parse_log(log)
            rows.append({"repo": repo, "pr": pr_of(repo, run), "run": run["id"], "job": job["name"],
                         "conclusion": job.get("conclusion") or "", "started": job.get("started_at") or "",
                         "ended": job.get("completed_at") or "", "ran": facts["cost"] is not None, **facts})
    return rows


def default_repos(owner, workflow):
    names = gh_json("repo", "list", owner, "--no-archived", "--limit", "200", "--json", "nameWithOwner")
    found = []
    for n in names:
        if gh("api", "repos/%s/contents/.github/workflows/%s" % (n["nameWithOwner"], workflow), ok404=True):
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
    groups = sorted({key(r) for r in runs if r["ran"]} | {t["by_" + title] for t in ths})
    out = ["", "### By %s" % title, "",
           "| %s | runs | spend | per run | threads | fixed | declined | ignored | judgment | per fixed |" % title,
           "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for g in groups:
        rs = [r for r in runs if r["ran"] and key(r) == g]
        ts = [t for t in ths if t["by_" + title] == g]
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
           "Per fixed thread: %s." % (money(spend / n["fixed"]) if n["fixed"] else "no fixed threads")]
    out += table("model", lambda r: r["model"], runs, ths)
    out += table("tier", lambda r: r["tier"] or "pre-triage", runs, ths)
    out += table("repo", lambda r: r["repo"], runs, ths)
    skipped = defaultdict(int)
    for r in runs:
        if not r["ran"]:
            if r["tier"]:
                why = "%s: %s" % (r["tier"], r["why"])
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


def main():
    ap = argparse.ArgumentParser(prog=NAME)
    ap.add_argument("--owner", default="mark-brannan")
    ap.add_argument("--repos", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--workflow", default="claude-code-review.yml")
    ap.add_argument("--csv")
    ap.add_argument("--threads-csv")
    a = ap.parse_args()
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)).strftime("%Y-%m-%d")
    repos = [r if "/" in r else "%s/%s" % (a.owner, r) for r in a.repos.split(",") if r] \
        or default_repos(a.owner, a.workflow)
    runs, ths = [], []
    for repo in repos:
        print("%s: %s" % (NAME, repo), file=sys.stderr)
        rs = collect(repo, a.workflow, since)
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
                ths.append({"repo": repo, "pr": pr, "path": t["path"], "at": t["at"],
                            "class": classify(t, state), "run": r.get("run", ""),
                            "by_model": r.get("model", ""), "by_tier": r.get("tier", "") or "pre-triage",
                            "by_repo": repo, "first_line": t["comments"][0]["body"].splitlines()[0][:120]})
    sys.stdout.write(markdown(runs, ths, since))
    if a.csv:
        write_csv(a.csv, runs, ["repo", "pr", "run", "job", "conclusion", "started", "ended", "ran", "model",
                                "effort", "tier", "why", "cost", "turns", "ms"])
    if a.threads_csv:
        write_csv(a.threads_csv, ths, ["repo", "pr", "run", "at", "class", "by_model", "by_tier", "path",
                                       "first_line"])


if __name__ == "__main__":
    main()
