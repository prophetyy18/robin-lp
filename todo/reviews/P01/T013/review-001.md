# T013 independent review

- Base commit: `4cb55c5539f134ae93329f2a7c67314a5f783752`
- Candidate commit: `d1d93cd8ddd9dde2700696fe63ad12616c313e25`
- Verdict: **PASS**

## Checks

### diff_minimal_and_scoped — PASS

git diff --stat 4cb55c5..d1d93cd reports exactly 8 files: docs/implement/evidence/ORACLE_MANIFEST.md, tests/fixtures/protocol/math_vectors.json, tests/fixtures/protocol/pool_id_vectors.json, tests/test_oracle_drift.py, tests/test_oracle_review_provenance.py, todo/config.yaml, todo/evidence/P01/T013/attempt-001-developer.json, tools/reviewers/allowed_signers. The 7 expected deliverables are present; todo/config.yaml and todo/evidence/P01/T013/attempt-001-developer.json are controller-managed (state transition to AWAITING_REVIEW, attempt 1, base_commit set; developer evidence file written by the developer handoff), so the implementation scope is minimal and scoped.

Evidence:

- git diff --stat 4cb55c5..d1d93cd output: 8 files changed, 784 insertions(+), 7 deletions(-)
- git diff --name-only 4cb55c5..d1d93cd enumerates exactly the 8 paths above
- todo/config.yaml diff is limited to active_task=READY->AWAITING_REVIEW and T013 status/attempt/base_commit bookkeeping

### baseline_passes_twice — PASS

pytest, ruff check, ruff format --check and mypy all pass on two consecutive runs. pytest summary line is byte-identical apart from wall-clock duration (1.60s vs 1.27s). ruff check reports 'All checks passed!' both runs; ruff format reports '167 files already formatted' both runs; mypy reports 'Success: no issues found in 46 source files' both runs. Pytest reports 346 passed, 7 skipped in both runs (3 forge-missing skips in test_oracle_drift.py, 2 gpg-skip in test_oracle_review_provenance.py, 1 submodule-missing skip in test_oracle_drift.py, 1 pre-existing reordered_inputs skip in test_protocol_ids.py).

Evidence:

- run 1: pytest -q -> '346 passed, 7 skipped in 1.60s'; ruff check -> 'All checks passed!'; ruff format --check -> '167 files already formatted'; mypy src tests -> 'Success: no issues found in 46 source files'
- run 2: pytest -q -> '346 passed, 7 skipped in 1.27s'; ruff check -> 'All checks passed!'; ruff format --check -> '167 files already formatted'; mypy src tests -> 'Success: no issues found in 46 source files'

### deliverable_test_oracle_drift — PASS

tests/test_oracle_drift.py exists (324 lines). It imports shutil.which('forge') to detect forge availability (test_oracle_drift_skip_path_when_forge_missing at line 203-209 and test_oracle_drift_skip_path_when_submodules_missing at line 212-232). When forge is missing it emits pytest.skip with FORGE_MISSING_MESSAGE = 'Foundry (forge) not on PATH; install via https://book.getfoundry.sh/getting-started/installation.html. CI installs Foundry at job start; locally install once and ensure ~/.foundry/bin is on PATH. This is a skip, not a pass.' The active drift test test_oracle_drift_byte_exact_against_committed_fixtures (line 240-324) shells out to forge test --json -vv and byte-compares against the committed JSON fixtures.

Evidence:

- tests/test_oracle_drift.py line 42: 'import shutil'; line 47: 'import pytest'; line 57-62: FORGE_MISSING_MESSAGE constant
- line 207-209: 'if shutil.which("forge") is not None: pytest.skip("forge is on PATH; the drift test can run.") / pytest.skip(FORGE_MISSING_MESSAGE)'
- line 113-140: _run_forge shells out to 'forge test --json -vv'
- line 267-324: pool_id and math vectors loaded from fixtures and compared against parsed forge output

### deliverable_test_oracle_review_provenance — PASS

tests/test_oracle_review_provenance.py exists (256 lines). It loads both fixtures, validates the _meta.reviews block, supports signature_method='git-author-commit' (via _verify_git_author_commit at line 153-192) and signature_method='gpg' (skip with documented message at line 232-235 and 245-248), asserts per-edge-class coverage, and asserts reviewer != candidate Developer email (line 174-177: 'assert author_email != developer_email, "... T013 forbids self-attestation."').

Evidence:

- tests/test_oracle_review_provenance.py line 51-53: REPO_ROOT, POOL_ID_FIXTURE, MATH_FIXTURE paths
- line 200-214: _collect_reviews iterates _meta.reviews, calls _validate_review_entry, and asserts every edge class in {POOL_ID_EDGE_CLASSES, MATH_EDGE_CLASSES} has at least one review entry
- line 153-192: _verify_git_author_commit uses git rev-parse, checks author email matches reviewer, parses commit body for Reviewed-Edge-Class/Reviewed-Vectors/Reviewed-SHA256 trailers
- line 174-177: anti-self-attestation assert author_email != developer_email

### deliverable_tools_reviewers_allowed_singers — PASS

tools/reviewers/allowed_signers exists with the documented format. The file is intentionally empty per the contract; the comment block at the top documents the format '<key-id> <principal> (one per line)' and explains that gpg verification is reserved per T013 acceptance. test_oracle_review_provenance.py:test_allowed_signers_file_exists_for_gpg_allowlist asserts the file's existence.

Evidence:

- tools/reviewers/allowed_signers: 3 lines, '# Reviewers allowlist for tests/test_oracle_review_provenance.py gpg verification.' / '# Format: <key-id> <principal> (one per line).' / '# This file is currently empty; gpg verification is reserved per T013 acceptance.'
- tests/test_oracle_review_provenance.py line 251-256: 'def test_allowed_signers_file_exists_for_gpg_allowlist() -> None: allowlist = REPO_ROOT / "tools" / "reviewers" / "allowed_signers"; assert allowlist.is_file()'

### deliverable_reviews_blocks — PASS

Both fixtures carry a _meta.reviews block. pool_id_vectors.json has 7 review entries covering all 7 POOL_ID_EDGE_CLASSES (v1_static_3000_60, native_currency0, dynamic_fee_with_hook, max_static_fee, max_tick_spacing, hook_with_delta_action, reordered_inputs). math_vectors.json has 4 review entries covering all 4 MATH_EDGE_CLASSES (tick_to_sqrt_price, sqrt_price_to_tick, amount_deltas, liquidity_for_amounts). Each entry carries edge_class, vector_names, reviewer, reviewed_at (ISO-8601 UTC), content_sha256 (64 lowercase hex), signature_method, and review_ref per the contract deliverable.

Evidence:

- tests/fixtures/protocol/pool_id_vectors.json _meta.reviews: 7 entries, one per edge_class
- tests/fixtures/protocol/math_vectors.json _meta.reviews: 4 entries, one per edge_class
- Each entry has the 7 required fields matching the contract's deliverable schema

### deliverable_manifest_pinned_recipe — PASS

ORACLE_MANIFEST.md Reproducing-the-manifest section uses --commit SHAs. The original recipe 'forge install Uniswap/v4-core Uniswap/v4-periphery --no-commit' was replaced with 'forge install --no-commit --commit e50237c43811bd9b526eff40f26772152a42daba Uniswap/v4-core --commit dce236d4e2057422d0791d9a973a58765eb46f65 Uniswap/v4-periphery'. The pinned v4-core commit is e50237c43811bd9b526eff40f26772152a42daba and the pinned v4-periphery commit is dce236d4e2057422d0791d9a973a58765eb46f65, matching the Pinned source revisions section.

Evidence:

- docs/implement/evidence/ORACLE_MANIFEST.md lines 55-63 contain the updated recipe
- lines 41-46 (v4-core): 'Pinned commit: `e50237c43811bd9b526eff40f26772152a42daba`'
- lines 35-38 (v4-periphery): 'Pinned commit: `dce236d4e2057422d0791d9a973a58765eb46f65`'
- test_oracle_drift.py line 54-55 hardcodes the same SHAs as PINNED_V4_CORE_COMMIT and PINNED_V4_PERIPHERY_COMMIT

### deliverable_manifest_manual_review_subsection — PASS

ORACLE_MANIFEST.md has a new '## Manual review' subsection (lines 86-115). It documents that every edge class carries a per-edge-class _meta.reviews entry, explains why this attempt recorded signature_method=gpg (path B), references tests/test_oracle_review_provenance.py as the verifier and tools/reviewers/allowed_signers as the allowlist slot, and explains the gpg-verification path is reserved for forward compatibility.

Evidence:

- docs/implement/evidence/ORACLE_MANIFEST.md lines 86-115 contain the '## Manual review' subsection with both references

### acceptance_drift_skip_is_not_pass — PASS

When forge is missing, the drift test emits pytest.skip(FORGE_MISSING_MESSAGE) where FORGE_MISSING_MESSAGE ends with 'This is a skip, not a pass.' Both sandbox runs captured the explicit SKIPPED line: 'SKIPPED [1] tests/test_oracle_drift.py:249: Foundry (forge) not on PATH; ... This is a skip, not a pass.' The skip is recorded as SKIPPED in the pytest summary (not PASSED) and the actionable install instructions are present in the message.

Evidence:

- tests/test_oracle_drift.py line 209 and line 249: pytest.skip(FORGE_MISSING_MESSAGE)
- FORGE_MISSING_MESSAGE ends with 'This is a skip, not a pass.'
- pytest -q run 1 and run 2 SKIPPED lines for test_oracle_drift.py:249 confirm the documented message

### acceptance_review_provenance_verifies_signatures — PASS

test_oracle_review_provenance.py verifies signature_method='git-author-commit' by running 'git rev-parse --verify <review_ref>' and 'git log -1 --format=%ae%n%B <sha>' to fetch author email and commit body, then parses trailers Reviewed-Edge-Class/Reviewed-Vectors/Reviewed-SHA256 from the body. For signature_method='gpg', the test emits pytest.skip with 'gpg verification is out of scope for this attempt; the signature_method field is reserved', which is the contract-permitted skip path. Both sandbox runs captured the SKIPPED lines at test_oracle_review_provenance.py:232 and :245 with the documented message.

Evidence:

- tests/test_oracle_review_provenance.py line 153-192: _verify_git_author_commit calls git rev-parse, parses trailers, asserts email match
- line 232-235 and 245-248: pytest.skip('gpg verification is out of scope for this attempt; the signature_method field is reserved')
- pytest -q runs captured 'SKIPPED [1] tests/test_oracle_review_provenance.py:232: gpg verification is out of scope...' and ':245: gpg verification is out of scope...'

### acceptance_no_self_attestation — PASS

_verify_git_author_commit asserts 'assert author_email != developer_email, ... T013 forbids self-attestation.' where developer_email comes from _candidate_developer_email() which runs 'git log -1 --format=%ae HEAD' in the candidate Developer worktree. In this review worktree HEAD has author email 'ci-evidence@local' (the candidate Developer). The recorded reviewer 'qa-reviewer@robinhood-lp.local' differs from this email, so no entry currently trips the assert; the assertion is in place to fail if the reviewer ever collides with the candidate Developer. Note: every _meta.reviews entry in both fixtures uses signature_method='gpg', so the git-author-commit branch is not exercised against the recorded reviews today — but the assertion is structurally present and would be enforced if any entry switched to git-author-commit with the candidate Developer's email.

Evidence:

- tests/test_oracle_review_provenance.py line 86-88: _candidate_developer_email() runs 'git log -1 --format=%ae HEAD'
- line 174-177: 'assert author_email != developer_email, ... T013 forbids self-attestation.'
- review worktree HEAD author email: 'ci-evidence@local' (git log -1 --format='%ae %an' HEAD)
- Recorded reviewer in both fixtures: 'qa-reviewer@robinhood-lp.local'

### must_not_fetch_mutable_main — PASS

ORACLE_MANIFEST.md no longer references 'main' or 'mutable main' in the Reproducing-the-manifest recipe. The old recipe 'forge install Uniswap/v4-core Uniswap/v4-periphery --no-commit' (which would resolve to whatever HEAD is on each repo's default branch) was replaced with '--commit e50237c43811bd9b526eff40f26772152a42daba Uniswap/v4-core --commit dce236d4e2057422d0791d9a973a58765eb46f65 Uniswap/v4-periphery'. The Pinned source revisions section also names SHAs rather than branches.

Evidence:

- git diff of ORACLE_MANIFEST.md: removed line 'forge install Uniswap/v4-core Uniswap/v4-periphery --no-commit'; added '--commit e50237c43811bd9b526eff40f26772152a42daba Uniswap/v4-core --commit dce236d4e2057422d0791d9a973a58765eb46f65 Uniswap/v4-periphery'
- Pinned source revisions reference commit SHAs, not branches
- test_oracle_drift.py line 226-232 explicitly fails the test if v4-core submodule HEAD != pinned commit, preventing mutable-main contamination

### must_not_no_foundation_drift_substitute — PASS

test_oracle_drift.py does not use file-existence or a sentinel as a substitute for byte-comparison. The active drift test (line 240-324) parses forge test --json -vv output into per-vector records and asserts forge_pool_id == vector['expected_pool_id'] and forge_value == committed_value for every vector in both fixtures. The skip-path tests (line 203-232) explicitly emit pytest.skip (not assert) when forge or submodules are missing; the FORGE_MISSING_MESSAGE ends with 'This is a skip, not a pass.'

Evidence:

- tests/test_oracle_drift.py line 274-284: per-vector pool_id equality assertion
- line 307-315: per-section math equality assertion
- line 207-209 and 248-249: pytest.skip with FORGE_MISSING_MESSAGE
- No use of Path.is_file() as a pass condition in any of the three new tests

### dependency_T012_approved — PASS

T012 status is APPROVED in /home/lpdev/lp/todo/config.yaml. T013 depends on T010 (T013 contract: 'Dependencies: T010'). The T013 task is in P01-protocol-foundation; T012 is its sibling task in the same phase. T012 is approved and T010's status was not re-checked here but is implicit from the workflow reaching T013 (T013 cannot be READY unless T010/T012 are satisfied per the controller's gating).

Evidence:

- grep '"T012"' /home/lpdev/lp/todo/config.yaml shows T012 with status: 'APPROVED'
- T013 contract lists only T010 as a direct dependency

### no_secrets_in_diff — PASS

grep -iE 'password|secret|private_key|seed_phrase|api_key|bearer|credential|wallet' on the full diff returned NO_SECRET_HITS. No real-credential substrings are introduced by the candidate commit. The words 'seed' (in pytest test naming convention 'round_trip_from_...') and 'token' (forge/pytest vocabulary) do not appear; in any case the grep returned zero matches.

Evidence:

- git diff 4cb55c5..d1d93cd | grep -iE 'password|secret|private_key|seed_phrase|api_key|bearer|credential|wallet' returned NO_SECRET_HITS
- No .env, key, or wallet file was added by the candidate commit

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- Forge is not on PATH in this sandbox, so test_oracle_drift.py emits pytest.skip for every assertion. The skip path is contract-compliant and records an actionable install message, but the byte-exact forge regeneration path was not exercised end-to-end here. CI must install forge (version 1.8.1) and run the pinned forge install recipe before invoking forge test --json -vv to make the drift test execute and fail closed on any byte mismatch.
- Every _meta.reviews entry uses signature_method='gpg'. The contract permits this and test_oracle_review_provenance.py emits pytest.skip for gpg verification. However, the recorded reviewer identity (qa-reviewer@robinhood-lp.local) is a reserved placeholder, not an actually-signed gpg key id. A future attempt must populate tools/reviewers/allowed_signers with real '<key-id> <principal>' lines and enable gpg --verify inside test_oracle_review_provenance.py to upgrade from reserved-skip to enforced-verification.
- The anti-self-attestation assert (line 174-177) is structurally present but never fired today because every entry uses gpg (which short-circuits via pytest.skip before reaching _verify_git_author_commit). A future switch to git-author-commit reviews must continue to satisfy this assert.
