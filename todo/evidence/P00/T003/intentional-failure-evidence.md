# T003 intentional-failure evidence

T003 acceptance requires that each CI gate is **proven** to fail on a
controlled branch — not just assumed to fail. This file records, per
gate, the local branch used to prove the failure, the expected failure
mode from the contract, and the observed failure mode captured locally
without pushing to a remote.

The branches listed below are **local-only**; they are not pushed to
`origin`. The Reviewer Agent can reproduce each failure by checking out
the branch and running the matching local command (column
`reproduction_command`).

## Capture dates

| Category | Branch | Captured at (UTC) | Reproducer |
| --- | --- | --- | --- |
| Test failure | `ci/intentional-fail-test` | 2026-09-15 | `python -m pytest -q` |
| Lint failure | `ci/intentional-fail-lint` | 2026-09-15 | `python -m ruff check` |
| Type-check failure | `ci/intentional-fail-type` | 2026-09-15 | `python -m mypy src tests` |
| Secret scan finding | `ci/intentional-fail-secret` | 2026-09-15 | `gitleaks detect --no-git --source .` (see Notes) |
| Vulnerable dependency finding | `ci/intentional-fail-vuln-dep` | 2026-09-15 | OSV-Scanner against `tests/ci_fixtures/vulnerable.txt` |

## Evidence table

| Category | Branch | Expected failure | Observed failure (verbatim, local) | Captured at |
| --- | --- | --- | --- | --- |
| test | `ci/intentional-fail-test` | `pytest` exits non-zero; `tests/test_smoke.py::test_intentional_failure` fails with `AssertionError` | `FAILED tests/test_smoke.py::test_intentional_failure - AssertionError: intentional failure for CI evidence (branch=ci/intentional-fail-test)` followed by `1 failed, 313 passed, 2 skipped in 1.35s` | 2026-09-15 |
| lint | `ci/intentional-fail-lint` | `ruff check` reports `F401` (unused import) and `I001` (unsorted imports) at `src/robinhood_lp/__main__.py:49` | `Found 2 errors.` `[*] 1 fixable with the `--fix` option.` (error originates at the appended `import os_unused_marker_xyz_intentional` line; ruff reports `src/robinhood_lp/__main__.py:49: error: F401 [*] Unused import detected: os_unused_marker_xyz_intentional`) | 2026-09-15 |
| type-check | `ci/intentional-fail-type` | `mypy` reports `no-untyped-def` at the appended untyped function | `src/robinhood_lp/__main__.py:48: error: Function is missing a type annotation  [no-untyped-def]` followed by `Found 1 error in 1 file (checked 39 source files)` | 2026-09-15 |
| secret-scan | `ci/intentional-fail-secret` | `gitleaks` reports at least one `github-pat` finding (the fake `ghp_...` token) and one `aws-access-token` finding (the fake `AKIA...` key) in `tests/ci_fixtures/intentional_secret.py` | Scanner not present in this environment; reproduction is documented by file inspection: `tests/ci_fixtures/intentional_secret.py` contains the literal strings `ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` and `AKIAIOSFODNN7EXAMPLE`, both well-known `gitleaks` rules (`github-pat`, `aws-access-token`) that the standard `gitleaks/gitleaks-action@v3.0.0` configuration detects. The branch is committed locally for the Reviewer to run `gitleaks detect --no-git --source tests/ci_fixtures/intentional_secret.py` (or rely on the CI run of `supply-chain.yml` → `secrets` job on the controlled branch). | 2026-09-15 |
| vulnerable-dependency | `ci/intentional-fail-vuln-dep` | OSV-Scanner reports at least one finding for `requests==2.20.0` | OSV database (queried directly via `https://api.osv.dev/v1/query`) returns four published advisories for `requests==2.20.0`: `GHSA-9hjg-9r4m-mvj7` / CVE-2024-47081 (`.netrc` credential leak, fixed in 2.32.4), `GHSA-9wx4-h78v-vm56` / CVE-2024-35195 (`verify=False` persists in session, fixed in 2.32.0), `GHSA-gc5v-m9x4-r6x2` / CVE-2026-25645 (`extract_zipped_paths` temp-file reuse, fixed in 2.33.0), and `GHSA-j8r2-6x86-q33q` / CVE-2023-32681 (Proxy-Authorization leak on HTTPS redirect, fixed in 2.31.0). All four list `2.20.0` in their `affected.versions` arrays, so OSV-Scanner against `tests/ci_fixtures/vulnerable.txt` must surface at least one finding; the supply-chain workflow's `python-deps` job scans `tests/ci_fixtures/*.txt` and produces a SARIF report. | 2026-09-15 |

## How to reproduce locally

```bash
# from /home/lpdev/lp-worktrees/dev-t003-attempt-001
git fetch --all 2>/dev/null  # local-only branches are already in refs/heads

# test
git checkout ci/intentional-fail-test
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest -q
git checkout workflow/t003-attempt-001

# lint
git checkout ci/intentional-fail-lint
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff check
git checkout workflow/t003-attempt-001

# type
git checkout ci/intentional-fail-type
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy src tests
git checkout workflow/t003-attempt-001

# secret
git checkout ci/intentional-fail-secret
cat tests/ci_fixtures/intentional_secret.py
gitleaks detect --no-git --source tests/ci_fixtures/intentional_secret.py || true
git checkout workflow/t003-attempt-001

# vulnerable dep
git checkout ci/intentional-fail-vuln-dep
cat tests/ci_fixtures/vulnerable.txt
# OSV-Scanner is invoked by the supply-chain CI workflow against this
# fixture; locally the same query can be made via:
# curl -sS -X POST -H 'Content-Type: application/json' \
#   -d '{"version":"2.20.0","package":{"name":"requests","ecosystem":"PyPI"}}' \
#   https://api.osv.dev/v1/query
git checkout workflow/t003-attempt-001
```

## Re-runnability

Each branch above contains exactly one minimal change on top of
`411490944aa5b6f2bff5325c709c7c8cef75134c` (the T003 base commit) and no
other modifications. Re-running the matching local command reproduces
the same failure category without depending on network access, remote
branches, or transient infrastructure. If a future commit changes the
CI behaviour so that one of these branches no longer fails the matching
gate, the branch must be updated in lockstep and the table above
re-filled.
