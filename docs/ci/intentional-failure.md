# Intentional-failure CI evidence

T003 acceptance requires that CI is proven to actually fail when the code
is broken — not just to silently stay green. This document records the
manual procedure used to produce that evidence and the historical results.

## Procedure

1. Open the repository on GitHub.
2. Trigger the **"Intentional CI failure"** workflow from the Actions tab:
   <https://github.com/melodydeck04/robinhood-lp/actions/workflows/intentional-failure.yml>
   (use `Run workflow` → branch `main`).
3. The workflow introduces a deliberate failure (test, lint, type, secret,
   or vulnerable dependency), waits for CI to report red, then reverts the
   change in a follow-up commit on the same run.
4. The result is recorded below.

## Results

| Date | Branch | Failure kind | Expected fail | Observed fail | Notes |
| --- | --- | --- | --- | --- | --- |
| (to be filled in after the first manual run) | | | | | |

If `Observed fail` ever differs from `Expected fail`, the CI gate is
broken and must be fixed before further T003 acceptance.

## Why a workflow instead of a test fixture

A reusable workflow is preferable to a one-off broken test because:

- the same evidence procedure can be re-run on demand after CI changes;
- the artifact (the failed run) is preserved in GitHub Actions history;
- no broken commit ever lands on `main` (the workflow reverts itself).
