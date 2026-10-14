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
# Each thread row also says why it got its class, so a label can be audited by
# clicking through: rule (which test fired), matched (the word or phrase that
# made it fire), reply (the deciding human reply, else the last one, first 200
# characters) and url (the first comment's).
#
# Usage: review_metrics.py [--owner LOGIN] [--repos a,b]
#                          [--days N | --since DATE [--until DATE]]
#                          [--workflow FILE] [--csv PATH] [--threads-csv PATH]
#                          [--out DIR [--sample N]] [--raw PATH | --from PATH]
#                          [--force | --pace N [--resume DIR]]
#
# --repos defaults to every non-archived repo of --owner whose workflow FILE
# (default claude-code-review.yml) exists. Markdown goes to stdout; the CSVs
# hold one row per run and one per thread. --raw also keeps everything read,
# as JSON lines: every run, and every bot thread with each comment in full,
# its author and reactions. --from rebuilds the report from such a file with
# no network, classing the threads by the rules as they are now, so a change
# to the classes re-reads old data instead of fetching it again. --out DIR writes
# <DIR>/<YYYY-MM-DD>-runs.csv, -threads.csv and -report.md in one go (the date is
# UTC today); DIR is the caller's to choose, and the threads carry reply text
# that may quote private pull requests, so choose a private one. --sample N adds
# <DIR>/<date>-label-sheet.csv: N threads spread across the rules, without the
# script's class, and an empty human_class column for hand-labelling; join it
# back to the threads CSV on url. Needs `gh`, authenticated; about two calls
# per run (its jobs, a log per job), so a busy month runs to thousands.
#
# The hourly API budget is shared with every session, so a fetch that needs
# more than half of what is left refuses. --pace N takes the long way instead:
# one call at a time, at most N an hour spread evenly, never taking the budget
# below FLOOR, and sleeping out a rate limit rather than failing. --resume DIR
# keeps each run's rows and each pull request's threads as they are read and
# pins the window and repos in DIR/meta.json, so a stopped gather reruns from
# where it stopped. --since/--until fix the window (UTC days; --until is
# exclusive and defaults to now); --days N means --since N days ago.
# Python 3 stdlib only.
import argparse
import csv
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict, namedtuple
from concurrent.futures import ThreadPoolExecutor

NAME = "review_metrics"
sys.dont_write_bytecode = True
BOT = "claude"  # the GraphQL login of claude[bot]
WORKERS = 2  # log downloads in flight; GitHub's secondary rate limit trips on more
CAP = 1000  # GitHub lists at most this many runs for a filtered query (#73)
FLOOR = 2000  # calls --pace leaves in the hourly budget for every other session
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
    when soft (a fact the report can go without), else fatal. Under --pace
    a rate limit is slept out, never retried into."""
    attempt = 0
    while attempt < 3:
        if PACE:
            PACE.wait()
        p = subprocess.run(["gh", *args], capture_output=True, encoding="utf-8", errors="replace")
        if not p.returncode:
            return p.stdout
        if "404" in p.stderr or "410" in p.stderr:
            return None
        if "rate limit" in p.stderr.lower():
            # Never soft: a fact dropped here would read as a run that never happened.
            if not PACE:
                die("rate limited; the hourly API budget is shared with every session, so rerun after it resets")
            PACE.rest("secondary" in p.stderr.lower())
            continue
        attempt += 1
        time.sleep(2 * attempt)
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


Verdict = namedtuple("Verdict", "cls rule matched reply")


def snippet(text):
    return " ".join(text.split())[:200]


def first_match(rx, comments):
    """The first comment rx finds something in: (what it found, that comment's body), else None."""
    for c in comments:
        m = rx.search(c["body"])
        if m:
            return m.group(0), c["body"]
    return None


def classify(thread, pr_state):
    """The thread's class and why: the rule that fired, the word that matched
    (empty when the rule is a fact, not a word) and the reply that decided it,
    else the last human reply, if any."""
    first = thread["comments"][0]["body"] if thread["comments"] else ""
    replies = [c for c in thread["comments"][1:] if c["author"] != BOT and not c.get("bot")]
    last = snippet(replies[-1]["body"]) if replies else ""
    m = JUDGMENT.match(first)
    if m:
        return Verdict("judgment", "judgment", re.sub(r"^\W+", "", m.group(0)), last)
    if thread["resolved"]:
        if thread["outdated"]:
            return Verdict("fixed", "outdated", "", last)
        hit = first_match(FIXED, replies)
        if hit:
            return Verdict("fixed", "reply-says-fixed", hit[0], snippet(hit[1]))
        hit = first_match(DECLINED, replies)
        if hit:
            return Verdict("declined", "reply-disagrees", hit[0], snippet(hit[1]))
        return Verdict("other", "silent", "", last)
    if thread["outdated"]:
        return Verdict("other", "outdated", "", last)
    if pr_state == "OPEN":
        return Verdict("open", "open", "", last)
    if pr_state != "MERGED":
        return Verdict("other", "closed-unmerged", "", last)
    if not replies:
        return Verdict("ignored", "unanswered-merged", "", "")
    return Verdict("other", "answered-open", "", last)


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
            nodes { url author { __typename login } createdAt body
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
                              "at": cs[0]["at"], "url": t["comments"]["nodes"][0]["url"], "comments": cs})
        page = p["reviewThreads"]["pageInfo"]
        if not page["hasNextPage"]:
            return p["state"], found
        after = page["endCursor"]


def stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def runs_of(repo, workflow, since, until):
    """Every pull_request run created in [since, until). GitHub silently
    stops a filtered listing at CAP, so a window that counts CAP or more is
    halved until each half lists whole (#73)."""
    q = "repos/%s/actions/workflows/%s/runs?event=pull_request&per_page=%d&created=%s..%s"
    found, todo = {}, [(since, until)]
    while todo:
        lo, hi = todo.pop()
        span = (stamp(lo), stamp(hi - dt.timedelta(seconds=1)))
        # A repo named in --repos without the workflow is a 404: no runs.
        n = (gh_json("api", q % ((repo, workflow, 1) + span)) or {}).get("total_count", 0)
        if n >= CAP:
            if hi - lo <= dt.timedelta(minutes=1):
                die("%s: %d runs in the minute from %s; cannot list them whole" % (repo, n, span[0]))
            mid = lo + dt.timedelta(seconds=int((hi - lo).total_seconds()) // 2)
            todo += [(mid, hi), (lo, mid)]
            continue
        if n:
            got = gh_json("api", "--paginate", "--slurp", q % ((repo, workflow, 100) + span))
            for page in got or []:
                for r in page.get("workflow_runs", []):
                    found[r["id"]] = r
    return sorted(found.values(), key=lambda r: r["created_at"])


def pr_index(repo, since):
    """The repo's pull requests updated since `since`, by head label
    (owner:branch), oldest first: one listing a repo instead of a lookup a
    run, since a run's own pull_requests field is often empty."""
    index, page = defaultdict(list), 1
    while True:
        got = gh_json("api", "repos/%s/pulls?state=all&sort=updated&direction=desc&per_page=100&page=%d" % (
            repo, page), soft=True) or []
        for p in got:
            index[p["head"]["label"]].append((p["created_at"], p["number"]))
        if len(got) < 100 or got[-1]["updated_at"] < stamp(since):
            break
        page += 1
    for v in index.values():
        v.sort()
    return index


def pr_of(repo, run, index=None):
    if run.get("pull_requests"):
        return run["pull_requests"][0]["number"]
    owner = ((run.get("head_repository") or {}).get("owner") or {}).get("login", "")
    # A branch name can be reused: the latest PR on it opened before the run.
    hits = [n for at, n in (index or {}).get("%s:%s" % (owner, run["head_branch"]), []) if at <= run["created_at"]]
    if hits:
        return hits[-1]
    prs = gh_json("api", "repos/%s/commits/%s/pulls" % (repo, run["head_sha"]), soft=True) or []
    if not prs:
        # A squash-merged PR's head commit is often no longer associated.
        prs = gh_json("api", "repos/%s/pulls?state=all&head=%s:%s" % (
            repo, owner, run["head_branch"]), soft=True) or []
    return prs[0]["number"] if prs else None


def rows_of(repo, run, index=None):
    jobs = (gh_json("api", "repos/%s/actions/runs/%d/jobs" % (repo, run["id"]), soft=True) or {}).get("jobs", [])
    pr = pr_of(repo, run, index)
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


def collect(repo, runs, index=None):
    """(run id, its rows) for each run, in order; one at a time under --pace."""
    with ThreadPoolExecutor(1 if PACE else WORKERS) as pool:
        for i, (run, rows) in enumerate(zip(runs, pool.map(lambda r: rows_of(repo, r, index), runs)), 1):
            if i % 100 == 0:
                print("%s: %s %d/%d runs" % (NAME, repo, i, len(runs)), file=sys.stderr)
            yield run["id"], rows


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
    v = classify(t, t["pr_state"])
    return {**{k: t[k] for k in ("repo", "pr", "path", "at", "run", "model", "tier", "paths")},
            "class": v.cls, "rule": v.rule, "matched": v.matched, "reply": v.reply, "url": t.get("url", ""),
            "up": first.get("reactions", {}).get("THUMBS_UP", 0),
            "down": first.get("reactions", {}).get("THUMBS_DOWN", 0),
            "first_line": (first["body"].splitlines() or [""])[0][:120]}


SHEET = ["repo", "pr", "url", "path", "at", "pr_state", "resolved", "outdated", "first_line", "replies", "human_class"]


def sample_rows(raw, n):
    """n threads for hand-labelling, dealt round-robin over the (class, rule)
    groups so every kind of call is checked, in a fixed shuffle so a rerun on
    the same data gives the same sheet. The script's own class is left off."""
    rng = random.Random(0)
    groups = defaultdict(list)
    for t in raw:
        v = classify(t, t["pr_state"])
        groups[(v.cls, v.rule)].append(t)
    for g in groups.values():
        rng.shuffle(g)
    keys, picked = sorted(groups), []
    while len(picked) < n and any(groups.values()):
        for k in keys:
            if groups[k] and len(picked) < n:
                picked.append(groups[k].pop())
    return [{"repo": t["repo"], "pr": t["pr"], "url": t.get("url", ""), "path": t["path"], "at": t["at"],
             "pr_state": t["pr_state"], "resolved": t["resolved"], "outdated": t["outdated"],
             "first_line": (t["comments"][0]["body"].splitlines() or [""])[0][:120],
             "replies": " | ".join(snippet(c["body"]) for c in t["comments"][1:]
                                   if c["author"] != BOT and not c.get("bot"))[:500],
             "human_class": ""} for t in picked]


def remaining(path):
    """Calls left this hour, read from the headers of the call about to be
    repeated: /rate_limit and some other endpoints have reported thousands
    left while the Actions endpoints failed at 0."""
    p = subprocess.run(["gh", "api", "-i", path], capture_output=True, encoding="utf-8", errors="replace")
    m = re.search(r"^x-ratelimit-remaining:\s*(\d+)", p.stdout, re.I | re.M)
    r = re.search(r"^x-ratelimit-reset:\s*(\d+)", p.stdout, re.I | re.M)
    return (int(m.group(1)) if m else 0), (int(r.group(1)) if r else int(time.time()) + 3600)


class Pace:
    """--pace: one call at a time, per_hour of them an hour spread evenly,
    reading the true budget every CHECK calls from probe's headers and
    sleeping to the reset when it is below FLOOR. A primary rate limit sleeps
    to the reset; a secondary one, whose reset GitHub does not give, an hour."""
    CHECK = 100

    def __init__(self, per_hour, probe=None):
        self.gap, self.probe = 3600.0 / per_hour, probe
        self.next, self.calls = 0.0, 0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            self.calls += 1
            if self.probe and self.calls % self.CHECK == 0:
                left, reset = remaining(self.probe)
                if left < FLOOR:
                    self.sleep_to(reset, "%d calls left, under the floor of %d" % (left, FLOOR))
            now = time.monotonic()
            if now < self.next:
                time.sleep(self.next - now)
            self.next = max(now, self.next) + self.gap

    def rest(self, secondary):
        if secondary or not self.probe:
            self.sleep_to(time.time() + 3600, "secondary rate limit" if secondary else "rate limited")
        else:
            self.sleep_to(remaining(self.probe)[1], "rate limited")

    @staticmethod
    def sleep_to(epoch, why):
        wake = max(epoch + 5, time.time())
        print("%s: %s; sleeping to %s" % (NAME, why, stamp(dt.datetime.fromtimestamp(wake, dt.timezone.utc))),
              file=sys.stderr)
        time.sleep(wake - time.time())


PACE = None


class Progress:
    """--resume DIR: a run's rows and a pull request's threads, each appended
    as it is read, so a stopped gather skips what it already has."""

    def __init__(self, d):
        self.d = d
        os.makedirs(d, exist_ok=True)

    def load(self, name):
        p = os.path.join(self.d, name)
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def add(self, name, rec):
        with open(os.path.join(self.d, name), "a") as f:
            f.write(json.dumps(rec) + "\n")


def fetch(repos, workflow, since, until, force, keep=None):
    listed = {repo: runs_of(repo, workflow, since, until) for repo in repos}
    have = {k["run"]: k["rows"] for k in keep.load("runs.jsonl")} if keep else {}
    read = {(k["repo"], k["pr"]): k for k in keep.load("threads.jsonl")} if keep else {}
    todo = {repo: [r for r in rs if r["id"] not in have] for repo, rs in listed.items()}
    # About two REST calls a run (jobs, a log per job); pull requests come from
    # one listing a repo, threads from GraphQL's own budget.
    need = 2 * sum(len(v) for v in todo.values())
    first = next(((repo, rs[0]["id"]) for repo, rs in listed.items() if rs), None)
    probe = "repos/%s/actions/runs/%d/jobs" % first if first else None
    left = remaining(probe)[0] if probe else 0
    print("%s: %d runs, %d already read; about %d API calls of the %d left this hour" % (
        NAME, sum(len(v) for v in listed.values()), len(have), need, left), file=sys.stderr)
    if PACE:
        PACE.probe = probe
    elif need > left // 2 and not force:
        die("that is more than half the hourly budget every session shares; "
            "narrow --days or --repos, take the long way with --pace, or pass --force")
    runs, raw = [], []
    for repo in repos:
        print("%s: %s" % (NAME, repo), file=sys.stderr)
        index = pr_index(repo, since) if todo[repo] else {}
        for run_id, rows in collect(repo, todo[repo], index):
            have[run_id] = rows
            if keep:
                keep.add("runs.jsonl", {"run": run_id, "rows": rows})
        rs = [row for r in listed[repo] for row in have.get(r["id"], [])]
        runs += rs
        by_pr = defaultdict(list)
        for r in rs:
            if r["pr"] is not None:
                by_pr[r["pr"]].append(r)
        for pr, prs in sorted(by_pr.items()):
            if not any(r["ran"] for r in prs):
                continue
            if (repo, pr) in read:
                state, found = read[(repo, pr)]["state"], read[(repo, pr)]["found"]
            else:
                state, found = threads(repo, pr)
                if keep:
                    keep.add("threads.jsonl", {"repo": repo, "pr": pr, "state": state, "found": found})
            for t in found:
                r = charge(prs, t) or {}
                raw.append({**t, "repo": repo, "pr": pr, "pr_state": state, "run": r.get("run", ""),
                            "model": r.get("model", ""), "tier": r.get("tier", "") or "pre-triage",
                            "paths": r.get("paths", "") or "pre-triage"})
    return runs, raw


RUNS_FIELDS = ["repo", "pr", "run", "job", "conclusion", "started", "ended", "ran", "model", "effort", "tier",
               "why", "paths", "cost", "turns", "ms"]
THREAD_FIELDS = ["repo", "pr", "run", "at", "class", "rule", "matched", "reply", "url", "model", "tier", "paths",
                 "path", "up", "down", "first_line"]


def main():
    ap = argparse.ArgumentParser(prog=NAME)
    ap.add_argument("--owner", default="mark-brannan")
    ap.add_argument("--repos", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--since", help="window start, YYYY-MM-DD (UTC); overrides --days")
    ap.add_argument("--until", help="window end, YYYY-MM-DD (UTC), exclusive; default now")
    ap.add_argument("--workflow", help="default claude-code-review.yml")
    ap.add_argument("--csv")
    ap.add_argument("--threads-csv")
    ap.add_argument("--out", help="directory for <date>-runs.csv, -threads.csv and -report.md")
    ap.add_argument("--sample", type=int, metavar="N",
                    help="with --out: also write N threads, spread across the rules, as <date>-label-sheet.csv")
    ap.add_argument("--force", action="store_true", help="spend past half the hourly API budget")
    ap.add_argument("--pace", type=int, metavar="N", help="at most N calls an hour, one at a time; sleep out limits")
    ap.add_argument("--resume", metavar="DIR", help="keep what is read in DIR, and skip what is already there")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--raw", help="also write everything read, as JSON lines")
    src.add_argument("--from", dest="source", help="report from a --raw file, no network")
    a = ap.parse_args()
    if a.sample and not a.out:
        die("--sample needs --out DIR")
    if a.out and (a.csv or a.threads_csv):
        die("--out writes its own CSVs; drop --csv/--threads-csv")
    if a.pace is not None and a.pace < 1:
        die("--pace needs a positive number of calls an hour")
    if a.force and a.pace:
        die("--force spends at once; --pace spends slowly; pick one")
    if a.source:
        with open(a.source) as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        since = next((ln["since"] for ln in lines if ln["kind"] == "meta"), "?")
        runs = [ln for ln in lines if ln["kind"] == "run"]
        raw = [ln for ln in lines if ln["kind"] == "thread"]
    else:
        global PACE
        if a.pace:
            PACE = Pace(a.pace)
        keep = Progress(a.resume) if a.resume else None
        pinned = os.path.join(a.resume, "meta.json") if a.resume else None
        asked = {"since": a.since, "until": a.until, "workflow": a.workflow,
                 "repos": [r if "/" in r else "%s/%s" % (a.owner, r) for r in a.repos.split(",") if r]}
        if pinned and os.path.exists(pinned):
            with open(pinned) as f:
                meta = json.load(f)
            for k, v in asked.items():
                if v and v != meta[k]:
                    die("%s is pinned to %s=%s in %s; drop --%s or start a new --resume DIR" % (
                        NAME, k, meta[k], pinned, k))
        else:
            now = dt.datetime.now(dt.timezone.utc)
            workflow = a.workflow or "claude-code-review.yml"
            meta = {"since": a.since or (now - dt.timedelta(days=a.days)).strftime("%Y-%m-%d"),
                    "until": a.until or stamp(now), "workflow": workflow,
                    "repos": asked["repos"] or default_repos(a.owner, workflow)}
        since, repos = meta["since"], meta["repos"]
        try:
            lo = dt.datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
            hi = dt.datetime.strptime(meta["until"], "%Y-%m-%dT%H:%M:%SZ" if "T" in meta["until"] else "%Y-%m-%d") \
                .replace(tzinfo=dt.timezone.utc)
        except ValueError:
            die("--since and --until take YYYY-MM-DD; got %s and %s" % (since, meta["until"]))
        if hi <= lo:
            die("--until %s is not after --since %s" % (meta["until"], since))
        # Pinned only once the window is good, so a bad one never sticks in DIR.
        if pinned and not os.path.exists(pinned):
            with open(pinned, "w") as f:
                json.dump(meta, f)
        runs, raw = fetch(repos, meta["workflow"], lo, hi, a.force, keep)
        if a.raw:
            taken = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(a.raw, "w") as f:
                f.write(json.dumps({"kind": "meta", "since": since, "until": meta["until"], "taken": taken,
                                    "repos": repos}) + "\n")
                for r in runs:
                    f.write(json.dumps({"kind": "run", **r}) + "\n")
                for t in raw:
                    f.write(json.dumps({"kind": "thread", **t}) + "\n")
    ths = [thread_row(t) for t in raw]
    report = markdown(runs, ths, since)
    sys.stdout.write(report)
    csv_path, threads_path = a.csv, a.threads_csv
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        stem = os.path.join(a.out, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"))
        csv_path, threads_path = stem + "-runs.csv", stem + "-threads.csv"
        with open(stem + "-report.md", "w") as f:
            f.write(report)
        if a.sample:
            write_csv(stem + "-label-sheet.csv", sample_rows(raw, a.sample), SHEET)
    if csv_path:
        write_csv(csv_path, runs, RUNS_FIELDS)
    if threads_path:
        write_csv(threads_path, ths, THREAD_FIELDS)


if __name__ == "__main__":
    main()
