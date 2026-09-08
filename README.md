# .github

The CI, review and Mergify definitions every repository under this account
shares. A repository carries one line of Mergify config and two thin
workflows; the substance lives here, once.

| file | consumed as |
|---|---|
| `.mergify.yml` | `extends: .github` in the repository's `.mergify.yml` |
| `.github/workflows/ci-gate.yml` | `uses: mark-brannan/.github/.github/workflows/ci-gate.yml@main` from a job named `ci-gate` |
| `.github/workflows/claude-review.yml` | `uses: mark-brannan/.github/.github/workflows/claude-review.yml@main` |
| `.github/workflows/prose-budget.yml` | `uses: mark-brannan/.github/.github/workflows/prose-budget.yml@v1`; the engine is `prose-budget` in `mark-brannan/dotfiles`, the config is the repository's `docs/budgets.json` or `.prose-budgets.json` |
| `.github/actions/npm-test-prose-fallback` | `uses: mark-brannan/.github/.github/actions/npm-test-prose-fallback@main` from an `npm test` step, on a runner with no `prose-budget` engine; pair with the `prose-budget` job above, which is the real gate |

Each file's header comment shows the calling snippet. A documentation gate
fans into `ci-gate` like any other job:

```yaml
jobs:
  prose-budget:
    uses: mark-brannan/.github/.github/workflows/prose-budget.yml@v1
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

`prose-budget` is the one entry above that is versioned, because it reaches
outside this repository: it fetches its engine from `mark-brannan/dotfiles`,
so tracking `main` on both would let a commit to a personal dotfiles
repository turn every consumer red at once. The tag `v1` here is moved
deliberately, and the workflow it points at names an immutable
`prose-budget/vX.Y.Z` tag in dotfiles. Consumers pin `v1` and name no engine
version; promotion and rollback are each one moved tag. The procedure is
`Cut and promote a prose-budget engine version` in that repository's
`RUNBOOK.md`.

Nothing under `profile/` and no community health files, deliberately: this
repository's name gives it account-wide defaults, and the only things meant
to apply account-wide are the ones above.
