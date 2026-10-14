# Agent decisions

Append-only. Each entry is a call an agent made in pencil: a default it took
under the one-way-door test, with its undo. Written by `agent-decision`; the
heading is the UTC stamp, so `agent_decisions.md#<stamp>` links one entry.

### 20261007t103436z
- pr-title exempts PRs opened by a Bot account (Dependabot, release-please) Undo: delete the AUTHOR_TYPE=Bot early exit in pr-title.yml ([#50](https://github.com/mark-brannan/.github/pull/50))
- pr-title reads the live title from the API, falling back to the event payload, so callers keep default pull_request types instead of adding edited Undo: use EVENT_TITLE only and document the edited trigger ([#50](https://github.com/mark-brannan/.github/pull/50))
- pr-title PATTERN accepts an optional (scope) and ! after the area Undo: drop (\([^)]+\))?!? from PATTERN ([#50](https://github.com/mark-brannan/.github/pull/50))

### 20261009t063120z
- The complexity job takes pull-requests: write so it can post its summary as a PR comment; callers grant it or get a notice Undo: drop the permission and the comment step; the summary stays in the run ([#60](https://github.com/mark-brannan/.github/pull/60))

### 20261009t063954z
- The complexity job declares no permissions and inherits the caller's grant; a caller without pull-requests: write gets a notice, not a failure Undo: declare contents: read and pull-requests: write on the job again; every caller must then grant both ([#60](https://github.com/mark-brannan/.github/pull/60))

### 20261013t202807z
- duplication leaves Markdown, plain text, JSON, lockfiles, SVG, vendor/ and node_modules/ out of both checks Undo: empty IGNORE and SKIP_* in duplication.py ([#66](https://github.com/mark-brannan/.github/pull/66))

### 20261013t202825z
- a duplication clone is keyed by its two copies' paths and space-squeezed text, no line numbers Undo: switch to jscpd --baseline-from-ref ([#66](https://github.com/mark-brannan/.github/pull/66))

### 20261013t202840z
- a repeated literal is a quoted string (double, single or backtick, optional r/b/u/f prefix) whose opening quote is not glued to a word Undo: edit LITERAL in duplication.py ([#66](https://github.com/mark-brannan/.github/pull/66))

### 20261013t202850z
- jscpd is installed with --ignore-scripts Undo: drop --ignore-scripts in duplication.yml ([#66](https://github.com/mark-brannan/.github/pull/66))

### 20261013t220627z
- claude-review labels are review:skip and review:deep only, no review:run Undo: add a review:run arm to the triage case ([#69](https://github.com/mark-brannan/.github/pull/69))

### 20261013t220650z
- claude-review triage thresholds 10/50/800 lines, budget pause at 90 percent, deep model claude-opus-5-5 at --effort high Undo: change the workflow input defaults ([#69](https://github.com/mark-brannan/.github/pull/69))

### 20261013t220706z
- claude-review budget probe fails open: an unanswered probe lets the review run Undo: exit 1 on an empty probe ([#69](https://github.com/mark-brannan/.github/pull/69))

### 20261013t220746z
- claude-review triage puts a diff touching only the mixed-loops design set (CLAUDE.md, .claude/rules, ADRs, decisions) in the skip tier Undo: remove the design arm from the triage decision ([#69](https://github.com/mark-brannan/.github/pull/69))

### 20261013t232107z
- a failed comment count leaves the cap unenforced for that run but blocks auto-escalation Undo: treat a failed count as 0 (or as at-cap) ([#69](https://github.com/mark-brannan/.github/pull/69))

### 20261013t232127z
- the 4-per-PR review cap counts every claude[bot] comment on the PR, review runs and @claude replies alike Undo: count only the 'Claude finished' review comments ([#69](https://github.com/mark-brannan/.github/pull/69))

### 20261013t234215z
- review-metrics reports total_cost_usd under the OAuth token as the API-equivalent price, not a bill Undo: report it as a bill, or drop the dollar figures ([#71](https://github.com/mark-brannan/.github/pull/71))

### 20261013t234233z
- review-metrics counts a resolved, outdated thread as fixed; a rebase that moves the lines also marks it Undo: drop isOutdated from the fixed test ([#71](https://github.com/mark-brannan/.github/pull/71))

### 20261013t234251z
- review-metrics reads a reply as saying fixed by a word list: fixed, done, addressed, applied, resolved in, a sha Undo: edit the FIXED word list ([#71](https://github.com/mark-brannan/.github/pull/71))

### 20261013t234335z
- review-metrics reads a reply as disagreeing, or marking a reversal, by a word list: no, not, won't, disagree, by design, intended, keep/kept, reversal/reversed, out of scope, stays Undo: edit the DECLINED word list ([#71](https://github.com/mark-brannan/.github/pull/71))

### 20261014t011629z
- review_metrics --pace leaves a floor of 2,000 calls in the hourly API budget for other sessions, fixed in code rather than a flag Undo: make FLOOR a flag or change the constant ([#74](https://github.com/mark-brannan/.github/pull/74))

### 20261014t011642z
- review_metrics --pace reads the remaining budget every 100 calls, about 1% extra calls Undo: change Pace.CHECK ([#74](https://github.com/mark-brannan/.github/pull/74))

### 20261014t011657z
- review_metrics --pace sleeps a full hour on a secondary rate limit, since GitHub gives no reset time for it Undo: shorten the sleep in Pace.rest ([#74](https://github.com/mark-brannan/.github/pull/74))

### 20261014t011711z
- review_metrics --pace paces GraphQL thread reads too, though they draw on a separate budget; there are few of them Undo: skip PACE.wait for graphql calls in gh() ([#74](https://github.com/mark-brannan/.github/pull/74))
