# Agent decisions

Append-only. Each entry is a call an agent made in pencil: a default it took
under the one-way-door test, with its undo. Written by `agent-decision`; the
heading is the UTC stamp, so `agent_decisions.md#<stamp>` links one entry.

### 20261007t103436z
- pr-title exempts PRs opened by a Bot account (Dependabot, release-please) Undo: delete the AUTHOR_TYPE=Bot early exit in pr-title.yml ([#50](https://github.com/mark-brannan/.github/pull/50))
- pr-title reads the live title from the API, falling back to the event payload, so callers keep default pull_request types instead of adding edited Undo: use EVENT_TITLE only and document the edited trigger ([#50](https://github.com/mark-brannan/.github/pull/50))
- pr-title PATTERN accepts an optional (scope) and ! after the area Undo: drop (\([^)]+\))?!? from PATTERN ([#50](https://github.com/mark-brannan/.github/pull/50))
