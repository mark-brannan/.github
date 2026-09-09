# .github

The CI, review and Mergify definitions every repository under this account
shares. A repository carries one line of Mergify config and two thin
workflows; the substance lives here, once.

| file | consumed as |
|---|---|
| `.mergify.yml` | `extends: .github` in the repository's `.mergify.yml` |
| `.github/workflows/ci-gate.yml` | `uses: mark-brannan/.github/.github/workflows/ci-gate.yml@main` from a job named `ci-gate` |
| `.github/workflows/claude-review.yml` | `uses: mark-brannan/.github/.github/workflows/claude-review.yml@main` |
| `.github/workflows/prose-budget.yml` | `uses: mark-brannan/.github/.github/workflows/prose-budget.yml@main`; the engine is `prose-budget` in `mark-brannan/dotfiles`, the config is the repository's `docs/budgets.json` or `.prose-budgets.json` |
| `.github/actions/npm-test-prose-fallback` | `uses: mark-brannan/.github/.github/actions/npm-test-prose-fallback@main` from an `npm test` step, on a runner with no `prose-budget` engine; pair with the `prose-budget` job above, which is the real gate |

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
mark-brannan/colregs-engine#54, mark-brannan/symphony#66); `dotfiles-ref`
in `prose-budget.yml` now defaults to `main` like everything else.
mark-brannan/.github#18 closed on the ruling. A moving pin per consumer,
remembered and rebumped, cost more than the risk it guarded against ever
materialized. A fix or a break in any workflow here — or in the dotfiles
engine it fetches — reaches every consumer on its next run; that propagation
is the point of this repository existing, not reviewed promotion.

Nothing under `profile/` and no community health files, deliberately: this
repository's name gives it account-wide defaults, and the only things meant
to apply account-wide are the ones above.
