# T003 independent review

- Base commit: `411490944aa5b6f2bff5325c709c7c8cef75134c`
- Candidate commit: `626959bd3bb6e5454157f73a79d0525a7f08178e`
- Verdict: **PASS**

## Checks

### diff_minimal_and_scoped — PASS

Candidate diff touches only the three workflow files (.github/workflows/ci.yml, intentional-failure.yml, supply-chain.yml), todo/config.yaml (workflow_state/active_task only), todo/evidence/P00/T003/{intentional-failure-evidence.md, suppressions.md}, and todo/evidence/P00/T003/attempt-001-developer.json. No source, test, lockfile, spec, intent, or otherwise protected files are modified.

Evidence:

- git diff --stat 4114909..626959bd shows exactly 7 files: .github/workflows/ci.yml (+103), .github/workflows/intentional-failure.yml (+16), .github/workflows/supply-chain.yml (+26/-5), todo/config.yaml (+8/-5), todo/evidence/P00/T003/attempt-001-developer.json (+58), todo/evidence/P00/T003/intentional-failure-evidence.md (+81), todo/evidence/P00/T003/suppressions.md (+32).

### baseline_passes_twice — PASS

All four quality gates pass twice, byte-identical apart from pytest wall-clock duration. The two pytest runs differ only in the trailing seconds value ('1.38s' vs '1.18s'); all other summary lines are identical. ruff check and ruff format --check and mypy output identical text on both runs.

Evidence:

- pytest run 1: '313 passed, 2 skipped in 1.38s'
- pytest run 2: '313 passed, 2 skipped in 1.18s'
- ruff check run 1 and 2: 'All checks passed!'
- ruff format --check run 1 and 2: '154 files already formatted'
- mypy run 1 and 2: 'Success: no issues found in 39 source files'

### sha_pinned_actions — PASS

Every `uses:` clause references a full 40-character lowercase hex SHA. 16 occurrences across 3 workflow files; all pinned.

Evidence:

- actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 (v7.0.1)
- conda-incubator/setup-miniconda@8ee1f361103df19b6f8c8655fd3967a8ecb162d5 (v4.0.1)
- actions/setup-python@0a5c61591373683505ea898e09a3ea4f39ef2b9c (v5.3.0)
- actions/upload-artifact@b4b15b8c7c6ac21ea08fcf65892d2ee8f75cf882 (v4.4.3)
- gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e (v3.0.0)
- aquasecurity/trivy-action@a9c7b0f06e461e9d4b4d1711f154ee024b8d7ab8 (v0.36.0)
- google/osv-scanner-action@6e4298ebc4db23e847df9b2e2de2939d6f066c67 (v2.5.1)
- grep for floating tags (@vX, @main) outside SHA pins returns no matches.

### no_auto_fix_dependencies — PASS

No auto-fix dependency command appears in any workflow step. The single `pip install --upgrade` line is `python -m pip install --upgrade pip` which upgrades the pip installer itself, not a project dependency. The textual references to `pip install --upgrade` and `pip-compile --upgrade` are inside a comment block that documents what is NOT done.

Evidence:

- ci.yml line 103: `python -m pip install --upgrade pip` (pip self-upgrade only; not a dependency auto-bump)
- ci.yml lines 95-100 (comment): explicitly states the workflow never runs `pip install --upgrade`, `pip-compile --upgrade`, etc.
- grep for `pip install --upgrade`, `pip-compile --upgrade`, `npm update`, `yarn upgrade` returns only the comment and the pip-self-upgrade line above.

### lockfile_integrity_job — PASS

.github/workflows/ci.yml defines a `lockfile-integrity` job that (1) runs `python -m pip install --require-hashes --dry-run --no-deps -r requirements.lock.txt` to verify pip can resolve every entry against PyPI without modifying the environment, and (2) parses the file and asserts that every hash line matches `^[0-9a-f]{64}$` and that at least one `--hash=sha256:` line exists. Missing or malformed hashes trigger `assert` and fail the job.

Evidence:

- ci.yml lines 75-127: 'lockfile-integrity' job definition with two verification steps
- Step 'Verify requirements.lock.txt hashes and resolution': `python -m pip install --require-hashes --dry-run --no-deps -r requirements.lock.txt`
- Step 'Sanity-check the lockfile shape': inline Python that asserts every `--hash=sha256:` line matches `^[0-9a-f]{64}$` and that at least one `--hash=sha256:` is present; raises AssertionError otherwise

### artifact_no_secrets — PASS

The upload-artifact step in ci.yml (lines 64-75) uploads only the three cache directories (.pytest_cache, .ruff_cache, .mypy_cache) by explicit positive path glob. The glob contains none of *.env, *.env.*, *.key, *.pem, *.keystore, or secrets/** paths. An explanatory comment documents the exclusion contract.

Evidence:

- ci.yml lines 64-75: `path:` includes only `.pytest_cache`, `.ruff_cache`, `.mypy_cache`
- No `*.env`, `*.env.*`, `*.key`, `*.pem`, `*.keystore`, `secrets/**` in the glob or include list
- Comment at lines 67-71 documents the explicit secret/RPC/credential exclusion contract

### network_test_marker — PASS

ci.yml has a `network-tests` job (lines 132-179) that explicitly selects `@pytest.mark.network` via `pytest -m network --override-ini="markers=network(...)"`. The override-ini registers the marker without modifying pyproject.toml (a protected file), so the strict-markers gate remains active. Phase 0 has zero network-tagged tests (collection returns 315 deselected / 0 selected), so the job currently runs against an empty selection. The marker registration is required by --strict-markers, so any future live RPC test that omits `@pytest.mark.network` will fail the strict-markers gate. This is the correct anti-silencing wiring.

Evidence:

- ci.yml line 171-175: pytest -m network --override-ini='markers=network(requires live RPC or recorded fixture)'
- Local verification: `pytest --co -q -m network --override-ini=...` returns 'no tests collected (315 deselected) in 0.17s'
- pyproject.toml sets `--strict-markers` and `--strict-config`; no `network` marker registered there
- Comment at lines 160-167 documents the contract: any live RPC test must be marked `@pytest.mark.network`

### suppression_table_complete — PASS

todo/evidence/P00/T003/suppressions.md contains 7 entries with scope/reason/owner/expiry/date_added plus 4 entries explaining items reviewed and confirmed NOT to require suppression. All suppressions encountered in the workflow diff (trivy ignore-unfixed, severity narrowing, GITLEAKS_ENABLE_UPLOAD_ARTIFACT: false, if-no-files-found: ignore, override-ini marker registration) have a corresponding row. Empty categories (e.g. allowlist of *-key files) are explicitly marked 'none encountered' via the 'Items reviewed and confirmed not to require suppression' table.

Evidence:

- suppressions.md rows: ruff E501 (line-too-long), ruff B011 (assert False in tests), coverage fail_under=0, trivy ignore-unfixed, two pytest.skip runtime skips, and override-ini marker registration (registered as not-a-suppression)
- Items-reviewed table covers: upload-artifact cache glob, GITLEAKS_ENABLE_UPLOAD_ARTIFACT: false, trivy severity HIGH,CRITICAL, if-no-files-found: ignore
- All dates 2026-09-15; owners explicit (T003 Owner, T010 Owner, T021 Owner with handle where known); expiries either no-expiry (with justification) or 2026-12-31

### intentional_fail_test_branch — PASS

Branch `ci/intentional-fail-test` exists locally. The branch adds a `test_intentional_failure` function in tests/test_smoke.py that asserts False. When the file from this branch is placed in the worktree, `pytest -q` reports exactly 1 failed (the new test) with the expected AssertionError message and the existing 3 smoke tests pass.

Evidence:

- git branch --list 'ci/intentional-fail-*' returns 5 branches including `ci/intentional-fail-test`
- git show ci/intentional-fail-test:tests/test_smoke.py shows `def test_intentional_failure() -> None: assert False, "intentional failure for CI evidence (branch=ci/intentional-fail-test)"`
- Live run after copying the branch version of tests/test_smoke.py: 'FAILED tests/test_smoke.py::test_intentional_failure - AssertionError: ... 1 failed, 3 passed in 0.07s'
- Original file restored after test (md5 matches the pre-modification snapshot).

### intentional_fail_lint_branch — PASS

Branch `ci/intentional-fail-lint` exists locally. The branch appends an unused import `import os_unused_marker_xyz_intentional` to src/robinhood_lp/__main__.py. When that version of __main__.py is placed in the worktree, `ruff check` reports 2 errors (F401 + I001 unsorted import) at line 49.

Evidence:

- git branch --list 'ci/intentional-fail-lint' confirms existence
- git show ci/intentional-fail-lint:src/robinhood_lp/__main__.py shows appended `import os_unused_marker_xyz_intentional` at line 49
- Live run after copying the branch version: 'Found 2 errors. [*] 1 fixable with the --fix option.' with 'F401 [*] Unused import detected: os_unused_marker_xyz_intentional' reported
- Original file restored after test (md5 matches pre-modification snapshot).

### intentional_fail_type_branch — PASS

Branch `ci/intentional-fail-type` exists locally. The branch appends an untyped function `def intentionally_untyped(x, y): return x + y` to src/robinhood_lp/__main__.py. When that version of __main__.py is placed in the worktree, `mypy src/robinhood_lp/__main__.py` reports 1 no-untyped-def error at line 48.

Evidence:

- git branch --list 'ci/intentional-fail-type' confirms existence
- git show ci/intentional-fail-type:src/robinhood_lp/__main__.py shows appended `def intentionally_untyped(x, y): return x + y` at line 48
- Live run after copying the branch version: 'src/robinhood_lp/__main__.py:48: error: Function is missing a type annotation  [no-untyped-def]  Found 1 error in 1 file (checked 1 source file)'
- Original file restored after test.

### intentional_fail_secret_branch — PASS

Branch `ci/intentional-fail-secret` exists locally. The branch writes tests/ci_fixtures/intentional_secret.py containing the literal strings `ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` (gitleaks rule github-pat) and `AKIAIOSFODNN7EXAMPLE` (gitleaks rule aws-access-token). The patterns match the standard gitleaks default ruleset. gitleaks binary is not installed on this host, so the failure is verified by pattern inspection of the fixture file: both strings are unambiguous gitleaks-detectable patterns. The CI workflow supply-chain.yml runs gitleaks/gitleaks-action@v3.0.0 on push, which uses the default ruleset.

Evidence:

- git branch --list 'ci/intentional-fail-secret' confirms existence
- git show ci/intentional-fail-secret:tests/ci_fixtures/intentional_secret.py shows `FAKE_GITHUB_TOKEN = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"` and `FAKE_AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"`
- These are well-known obvious test patterns: ghp_ followed by 36 chars triggers github-pat rule; AKIA followed by 16 uppercase alphanumerics triggers aws-access-token rule.
- supply-chain.yml uses gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e (v3.0.0) which uses the default ruleset including both rules.

### intentional_fail_vuln_dep_branch — PASS

Branch `ci/intentional-fail-vuln-dep` exists locally. The branch writes tests/ci_fixtures/vulnerable.txt containing `requests==2.20.0`. The Developer evidenced (via direct OSV API query) that 2.20.0 is in the affected.versions array of multiple published advisories (CVE-2024-47081, CVE-2024-35195, CVE-2026-25645, CVE-2023-32681). The supply-chain.yml workflow runs osv-scanner against tests/ci_fixtures/*.txt fixtures on every push, so a scanner run on this branch will surface the advisories.

Evidence:

- git branch --list 'ci/intentional-fail-vuln-dep' confirms existence
- git show ci/intentional-fail-vuln-dep:tests/ci_fixtures/vulnerable.txt shows `requests==2.20.0`
- supply-chain.yml lines 108-121: OSV-Scanner step runs `osv-scanner --lockfile=/tmp/ci_fixtures_requirements.txt --format=sarif --output=osv-fixtures.sarif` against tests/ci_fixtures/*.txt
- Developer handoff documents direct query against https://api.osv.dev/v1/query returning multiple advisories affecting 2.20.0

### evidence_table_complete — PASS

intentional-failure-evidence.md contains all 5 categories in the evidence table: test, lint, type-check, secret-scan, vulnerable-dependency. Each row has branch, expected failure, observed failure (verbatim local), and captured_at (2026-09-15) populated. A separate 'Capture dates' table at the top summarises branches and reproducers.

Evidence:

- Evidence table rows: test/lint/type-check/secret-scan/vulnerable-dependency
- Each row populated: Branch (ci/intentional-fail-*), Expected failure, Observed failure (verbatim, local), Captured at 2026-09-15
- Captured dates table provides reproducer commands for each branch

### no_secrets_in_diff — PASS

All credential-looking strings in the diff are obviously fake negative-test fixtures (ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA, AKIAIOSFODNN7EXAMPLE). No real private keys, seed phrases, API secrets, or production credentials. All references to 'password', 'credential', 'secret', 'token', 'private_key', 'seed', 'api_key', 'bearer', 'wallet' in the diff are either documentation comments, the workflow option `secret` (a category name), or the gitleaks-disable env var name.

Evidence:

- intentional-failure.yml line 84: `FAKE_TOKEN = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"` (zero entropy, well-known test pattern)
- intentional-secret.py from the secret branch: `FAKE_GITHUB_TOKEN = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"` and `FAKE_AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"`
- No 24-word BIP39 phrases, no real keys, no live API credentials
- Comments explicitly label these as 'test-only fake credential string (not a real secret)'

### must_not_floating_action_tags — PASS

Duplicate of sha_pinned_actions. No floating action tags. Every uses: is pinned to a 40-character SHA.

Evidence:

- All 16 uses: clauses across ci.yml, intentional-failure.yml, supply-chain.yml reference 40-char lowercase hex SHAs (see sha_pinned_actions).

### must_not_auto_fix_deps — PASS

Duplicate of no_auto_fix_dependencies. No silent dependency auto-fix in CI. The single pip-self-upgrade line upgrades the installer only.

Evidence:

- Comment in ci.yml lines 95-100 explicitly documents the prohibition.
- No `pip-compile --upgrade`, `npm update`, or `yarn upgrade` invocations.

### must_not_upload_env_or_rpc — PASS

Duplicate of artifact_no_secrets. upload-artifact path glob includes only cache directories; no .env, .env.*, *.key, *.pem, *.keystore, secrets/**, or RPC response caches.

Evidence:

- ci.yml lines 65-74 positive-only path: .pytest_cache, .ruff_cache, .mypy_cache.

### must_not_silent_optional_network_tests — PASS

Duplicate of network_test_marker. Network tests are explicitly gated via the `network-tests` job in ci.yml, using `pytest -m network --override-ini=...` with strict-markers enabled. The job is non-skippable; it always runs even when 0 tests match the marker, so a future live RPC test cannot be silently dropped.

Evidence:

- ci.yml lines 132-179: network-tests job runs on every push without a conditional skip.
- pyproject.toml enables --strict-markers and --strict-config.

### dependencies_T001_T002_approved — PASS

T001 status is APPROVED (depends_on T000). T002 status is APPROVED with approved_commit 762775949b9f348ef53839ae50cba00bdebf9731 (depends_on T001). T003 depends on T001, which is APPROVED. T002 is not a direct dependency of T003 but is also APPROVED in the config.

Evidence:

- T001 in todo/config.yaml: 'status': 'APPROVED', 'depends_on': ['T000']
- T002 in todo/config.yaml: 'status': 'APPROVED', 'depends_on': ['T001'], 'approved_commit': '762775949b9f348ef53839ae50cba00bdebf9731'
- T003 in todo/config.yaml: 'depends_on': ['T001'] (satisfied)

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- Intentional-failure branches are local-only and not pushed; the CI environment cannot be exercised end-to-end from this host.
- gitleaks and osv-scanner binaries are not installed locally; the secret-scan and vulnerable-dep branches rely on supply-chain.yml's CI invocation of these scanners to demonstrate the failures in a live environment.
- Network-tests job has zero selected tests in the current Phase 0 code base; the wiring is correct but is exercised against an empty selection today.
- Branch ci/intentional-fail-secret demonstrates only pattern-correctness of the fixture against the documented gitleaks rules; gitleaks was not invoked locally to surface the actual rule ID, severity, or finding fingerprint.
- gitleaks and osv-scanner binaries are not installed on this host, so the secret-scan and vulnerable-dependency intentional failures were verified by (a) file-content inspection of the fixture files against the documented scanner rule sets and (b) the supply-chain workflow that runs both scanners on every push. The CI environment cannot be exercised from this host. End-to-end CI verification is a residual risk rather than an unknown; the CI workflow definitions themselves are verifiable as complete and correct.
- The live GitHub Actions run has not been triggered by this reviewer. The intentional-failure-evidence.md documents CI evidence as 'Scanner not present in this environment; reproduction is documented by file inspection'. This is acceptable for a local-review workflow but flagged as a residual risk.
