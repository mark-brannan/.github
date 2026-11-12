#!/usr/bin/env python3
# conflict_latency: how long a conflict waits before the fixer gets it, read
# back from the `conflict-latency` notices the shared conflict sweep
# (.github/workflows/conflict-sweep.yml) leaves on each hand-over. A
# measurement, never a gate.
#
# Per hand-over: label_delay, seconds from the push that caused the conflict
# to Mergify's `Has Conflict` label; hand_delay, seconds from the label to the
# hand-over. Only runs whose fix job ran are read, so a quiet sweep costs one
# API call and a hand-over two.
#
# Usage: conflict_latency.py [--owner LOGIN] [--repos a,b] [--days N]
#                            [--workflow FILE] [--csv PATH]
# Markdown summary to stdout; one row per hand-over to --csv.

import argparse
import csv
import datetime as dt
import json
import statistics
import subprocess
import sys

NAME = "conflict_latency"
TITLE = "conflict-latency"
FIELDS = ["repo", "pr", "head", "event", "cause", "caused_at", "labelled_at", "handed_at",
          "label_delay", "hand_delay", "run"]


def gh_json(*args):
    p = subprocess.run(["gh", *args], capture_output=True, encoding="utf-8", errors="replace")
    if p.returncode:
        return None
    out = p.stdout.strip()
    if not out:
        return None
    # --paginate --slurp gives one array of pages.
    return json.loads(out)


def parse_notice(message):
    """`k=v k=v ...` as the sweep writes it; the delays become ints."""
    rec = dict(kv.split("=", 1) for kv in message.split() if "=" in kv)
    for k in ("label_delay", "hand_delay"):
        try:
            rec[k] = int(rec[k])
        except (KeyError, ValueError):
            return None
    return rec


def handed_over(jobs):
    """True when a fix leg ran: only then did the sweep leave a notice."""
    return any("fix" in j.get("name", "") and j.get("conclusion") != "skipped" for j in jobs)


def sweep_job(jobs):
    for j in jobs:
        if j.get("name", "").split(" / ")[-1] == "sweep":
            return j
    return None


def runs_of(repo, workflow, since):
    pages = gh_json("api", "--paginate", "--slurp",
                    "repos/%s/actions/workflows/%s/runs?per_page=100&created=>=%s" % (repo, workflow, since))
    return [r for page in pages or [] for r in page.get("workflow_runs", [])]


def rows_of(repo, run):
    jobs = (gh_json("api", "repos/%s/actions/runs/%d/jobs?per_page=100" % (repo, run["id"])) or {}).get("jobs", [])
    job = sweep_job(jobs)
    if not job or not handed_over(jobs):
        return []
    notes = gh_json("api", "repos/%s/check-runs/%d/annotations?per_page=100" % (repo, job["id"])) or []
    rows = []
    for n in notes:
        if n.get("title") != TITLE:
            continue
        rec = parse_notice(n.get("message", ""))
        if rec:
            rows.append({**{k: rec.get(k, "") for k in FIELDS}, "repo": repo, "run": run["html_url"]})
    return rows


def default_repos(owner, workflow):
    names = gh_json("repo", "list", owner, "--no-archived", "--limit", "200", "--json", "nameWithOwner") or []
    return [n["nameWithOwner"] for n in names
            if gh_json("api", "repos/%s/contents/.github/workflows/%s" % (n["nameWithOwner"], workflow))]


def p90(xs):
    return sorted(xs)[max(0, -(-len(xs) * 9 // 10) - 1)]


def markdown(rows, since):
    out = ["### Conflict latency since %s" % since, ""]
    if not rows:
        return "\n".join(out + ["No hand-overs recorded.", ""])
    out += ["| repo | hand-overs | label delay median | p90 | max | hand delay median | p90 |",
            "|---|---|---|---|---|---|---|"]
    by = {}
    for r in rows:
        by.setdefault(r["repo"], []).append(r)
    for repo, rs in sorted(by.items()) + [("**all**", rows)]:
        ld = [r["label_delay"] for r in rs]
        hd = [r["hand_delay"] for r in rs]
        out.append("| %s | %d | %ds | %ds | %ds | %ds | %ds |" % (
            repo, len(rs), statistics.median(ld), p90(ld), max(ld), statistics.median(hd), p90(hd)))
    return "\n".join(out + [""])


def main(argv=None):
    ap = argparse.ArgumentParser(prog=NAME)
    ap.add_argument("--owner", default="mark-brannan")
    ap.add_argument("--repos", help="comma-separated owner/name; default: every repo with the workflow")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--workflow", default="conflict-sweep.yml", help="the caller's file name")
    ap.add_argument("--csv")
    a = ap.parse_args(argv)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)).strftime("%Y-%m-%d")
    repos = a.repos.split(",") if a.repos else default_repos(a.owner, a.workflow)
    rows = [row for repo in repos for run in runs_of(repo, a.workflow, since) for row in rows_of(repo, run)]
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
    sys.stdout.write(markdown(rows, since))


if __name__ == "__main__":
    main()
