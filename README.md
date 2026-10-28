# .github

The CI, review and Mergify definitions every repository under this account
shares. A repository carries one line of Mergify config and two thin
workflows; the substance lives here, once.

| file | consumed as |
|---|---|
| `.mergify.yml` | `extends: .github` in the repository's `.mergify.yml` |
| `.github/workflows/ci-gate.yml` | `uses: mark-brannan/.github/.github/workflows/ci-gate.yml@main` from a job named `ci-gate` |
| `.github/workflows/claude-review.yml` | `uses: mark-brannan/.github/.github/workflows/claude-review.yml@main`; labels `review:skip` and `review:deep` override its triage |
| `.github/workflows/nonempty-diff.yml` | `uses: mark-brannan/.github/.github/workflows/nonempty-diff.yml@main`; fails an empty PR and any PR that adds a line starting with a tab; files exempt from the tab check are listed in `.github/config/no-tabs-allowlist.txt` |
| `.github/workflows/pin-bump-alone.yml` | `uses: mark-brannan/.github/.github/workflows/pin-bump-alone.yml@main`; label `pin-bump-mixed-ok` lets it through |
| `.github/workflows/complexity.yml` | `uses: mark-brannan/.github/.github/workflows/complexity.yml@main`, with `pull-requests: write`, outside `ci-gate`'s needs; posts each touched function's complexity change as a PR comment, never a gate |
| `.github/workflows/coverage.yml` | `uses: mark-brannan/.github/.github/workflows/coverage.yml@main` from a job that `needs` the tests job, with `actions: read` and `pull-requests: write`, outside `ci-gate`'s needs; the tests job uploads a Cobertura report as the `coverage` artifact on PRs and main pushes; posts the share of changed lines the tests hit, and the total's move from main at the merge base, as a PR comment, never a gate |
| `.github/workflows/duplication.yml` | `uses: mark-brannan/.github/.github/workflows/duplication.yml@main`, with `pull-requests: write`, outside `ci-gate`'s needs; posts the new clones and repeated literals a PR adds as a PR comment, never a gate |
| `.github/workflows/prose-budget.yml` | `uses: mark-brannan/.github/.github/workflows/prose-budget.yml@main`; the engine is `prose-budget` in `mark-brannan/claude` (`bin/`), the config is the repository's `docs/budgets.json` or `.prose-budgets.json` |
| `.github/scripts/review_metrics.py` | run by hand, `python3 .github/scripts/review_metrics.py --days 30` with `gh` signed in; what the `claude-review.yml` reviews cost, per run, model and tier, and what became of each comment they left (fixed, declined, ignored, judgment); `--raw` keeps every comment in full, and `--from` re-reads that file with no network, so a reclassification never refetches |
| `.github/actions/npm-test-prose-fallback` | `uses: mark-brannan/.github/.github/actions/npm-test-prose-fallback@main` from an `npm test` step, on a runner with no `prose-budget` engine; pair with the `prose-budget` job above, which is the real gate |
| `.github/actions/headless-run-cost` | `uses: mark-brannan/.github/.github/actions/headless-run-cost@main` after a headless Claude step; appends the run's cost row to `headless-run.jsonl`, which the caller uploads as the `headless-run` artifact |

Each file's header comment shows the calling snippet. A documentation gate
fans into `ci-gate` like any other job:

```yaml
jobs:
  prose-budget:
    uses: mark-brannan/.github/.github/workflows/prose-budget.yml@main
  ci-gate:
    needs: [test, prose-budget]
    if: always()
    permissions: {}
    uses: mark-brannan/.github/.github/workflows/ci-gate.yml@main
    with:
      results: ${{ toJSON(needs) }}
```

The required status
check everywhere is `ci-gate / gate`, and the branch rulesets are applied
through the API, not stored here.

**Nothing here is ever pinned — every `uses:` line stays on `@main`,
deliberately, permanently, at every hop.** This was tried once for
`prose-budget`, two hops deep: consumers pinned this repository's `@v1`, and
`prose-budget.yml` in turn pinned an immutable `mark-brannan/dotfiles` tag
for its engine, because that one workflow reaches outside this repository for
its logic. Both hops were reverted (mark-brannan/colregs#78,
mark-brannan/colregs-engine#54, mark-brannan/symphony#66); `claude-ref`
in `prose-budget.yml` now defaults to `main` like everything else.
mark-brannan/.github#18 closed on the ruling. A moving pin per consumer,
remembered and rebumped, cost more than the risk it guarded against ever
materialized. A fix or a break in any workflow here — or in the
engine it fetches — reaches every consumer on its next run; that propagation
is the point of this repository existing, not reviewed promotion.

Nothing under `profile/`, deliberately: this repository's name gives it
account-wide defaults, and a profile page isn't one of the things meant to
apply account-wide.

`SECURITY.md`, `CODE_OF_CONDUCT.md`, `SUPPORT.md` and `CONTRIBUTING.md` at
the root here *are* account-wide defaults, using
[GitHub's community-health-file fallback](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file-for-your-organization):
any repository under this account that doesn't define its own copy of one
of these files shows this one instead. They're deliberately generic — a
repository with real repo-specific policy (a different reporting channel,
contributor instructions tied to its own build or data format) keeps its
own file as an override instead of relying on the fallback.
