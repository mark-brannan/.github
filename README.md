# .github

The CI, review and Mergify definitions every repository under this account
shares. A repository carries one line of Mergify config and two thin
workflows; the substance lives here, once.

| file | consumed as |
|---|---|
| `.mergify.yml` | `extends: .github` in the repository's `.mergify.yml` |
| `.github/workflows/ci-gate.yml` | `uses: mark-brannan/.github/.github/workflows/ci-gate.yml@main` from a job named `ci-gate` |
| `.github/workflows/claude-review.yml` | `uses: mark-brannan/.github/.github/workflows/claude-review.yml@main` |

Each file's header comment shows the calling snippet. The required status
check everywhere is `ci-gate / gate`, and the branch rulesets are applied
through the API, not stored here.

Nothing under `profile/` and no community health files, deliberately: this
repository's name gives it account-wide defaults, and the only things meant
to apply account-wide are the three above.
