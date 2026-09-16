# T025 independent review

- Base commit: `e20954fe57db23e3729719b63fe94cc3f1dd154a`
- Candidate commit: `78fb19f17991191de3c70a6f16db2c457405e809`
- Verdict: **PASS**

## Checks

### forbidden-paths-empty — PASS

All forbidden paths are byte-identical to the approved base. Only the new candidate files plus the controller-managed todo/config.yaml bookkeeping changed.

Evidence:

- git diff e20954f..78fb19f -- 'docs/intent/' 'docs/spec/' 'tools/oracle/' 'tools/workflow/' 'todo/phases/' 'tests/test_initialize_scanner.py' 'tests/test_abi_artifacts.py' 'tests/test_chain_capability.py' 'tests/test_eligibility.py' 'src/robinhood_lp/protocol/' 'src/robinhood_lp/rpc/' 'src/robinhood_lp/discovery/initialize_log.py' 'src/robinhood_lp/discovery/registry.py' 'src/robinhood_lp/discovery/chain_capability.py' 'src/robinhood_lp/discovery/eligibility.py' 'docs/implement/protocol-artifacts/' 'docs/implement/chain-capability-report-robinhood-testnet.json' returns zero lines

### candidate-files-scope — PASS

Diff is scoped to the three allowed implementation/test/export-wiring files. todo/config.yaml changes are controller bookkeeping (status READY->AWAITING_REVIEW, attempt 0->1, base_commit set).

Evidence:

- git diff --stat e20954f..78fb19f (excluding todo/config.yaml and todo/evidence/P02/T025/attempt-001-developer.json) shows only: src/robinhood_lp/discovery/__init__.py (+36), src/robinhood_lp/discovery/asset_admission.py (+627), tests/test_asset_admission.py (+863)

### critical-baseline-forge-regression — PASS

The single failing test (test_artifact_byte_matches_regenerated_oracle_output) is a pre-existing T012-era forge-environmental blocker. The same test fails on base e20954f with identical stderr because tools/oracle/lib/{v4-core,v4-periphery} are not installed in this review worktree. The candidate did NOT introduce this regression. Confirmed out-of-scope per the Manager's CRITICAL baseline-check protocol.

Evidence:

- On candidate (worktree HEAD 78fb19f): PYTHONPATH=src pytest tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output FAILS with stderr 'tools/oracle/lib/v4-core/src/libraries/TickMath.sol: No such file or directory' and 'forge-std/Test.sol' import failure (forge submodules not installed).
- Independent fresh checkout of base e20954f to /tmp/review-t025-baseline/lp shows the IDENTICAL failure with the IDENTICAL stderr (tools/oracle/lib/v4-core missing, forge-std/Test.sol missing).
- Full suite on candidate: 1 failed, 421 passed, 6 skipped (+20 passes vs T023 baseline 401 passed). The 1 failure matches the T023 baseline count of 1 failed test (forge environmental).
- Cleanup: /tmp/review-t025-baseline removed after verification.

### track-a-determinism — PASS

Track A is deterministic: same T022 PoolRegistry + T023 EligibilityDecision map + T024 ChainCapabilityReport produce byte-identical PoolAdmissionRecord outputs across runs. Precedence (capability_report_missing -> bytecode_drift -> proxy_detected -> hook_bytecode_unavailable -> eligibility_level=rejected -> admitted) is implemented in _compute_track_a and verified by individual unit tests.

Evidence:

- tests/test_asset_admission.py::test_track_a_is_deterministic_across_runs PASSED: two calls with byte-identical inputs produce equal PoolAdmissionRecord and byte-identical dataclasses.asdict JSON.
- Manual sha256 check via build_p02_closeout_report showed sha256_of_report stable across two runs with pinned generated_at.
- Two consecutive pytest runs of test_asset_admission.py show '20 passed' with stable test counts.

### track-b-pending-owner-by-default — PASS

Track B is pending_owner for every PoolRecord until an explicit OperatorDecision is supplied. The asset_admission module does NOT mint synthetic Owner decisions (zero OperatorDecision() instantiations in src).

Evidence:

- tests/test_asset_admission.py::test_track_b_is_pending_owner_by_default PASSED: without an OperatorDecision, track_b_status=pending_owner, track_b_owner_decision_ref=None, operator_decision=None, live_block=None.
- _compute_track_b returns None for operator_decision when input is None and surfaces operator_decision only when caller supplies one.

### track-b-owner-decision-transitions — PASS

OperatorDecision transitions are exactly per spec: APPROVED->admitted, REJECTED->rejected_by_owner, REVOKED->pending_owner (audit trail preserved). Each requires decision_id, owner_decision_ref, decided_at populated.

Evidence:

- tests/test_asset_admission.py::test_track_b_admitted_only_when_owner_decision_approved PASSED: APPROVED -> TRACK_B_ADMITTED, owner_decision_ref recorded, operator_decision==op, live_block remains None.
- tests/test_asset_admission.py::test_track_b_rejected_when_owner_decision_rejected PASSED: REJECTED -> TRACK_B_REJECTED_BY_OWNER.
- tests/test_asset_admission.py::test_track_b_pending_owner_when_owner_decision_revoked PASSED: REVOKED -> TRACK_B_PENDING_OWNER but operator_decision retained for audit trail.

### live-block-always-none — PASS

live_block is always None in T025. Track B does NOT auto-promote live; live promotion is G-LIVE-GATE-01 territory and is intentionally out of scope.

Evidence:

- _compute_track_b in asset_admission.py returns None for live_block on every code path (verified by inspection of all 5 return branches).
- tests/test_asset_admission.py::test_no_pool_record_lives_in_track_b_when_no_owner_decision PASSED across 3 pools with no operator decision.
- tests/test_asset_admission.py::test_track_b_admitted_only_when_owner_decision_approved PASSED: live_block is None even when OperatorDecision outcome is APPROVED.
- G-LIVE-GATE-01 / G-LIVE-01 territory: live promotion is explicitly out of scope; module docstring + line 393 docstring + line 452 docstring all confirm.

### track-a-bytecode-drift-blocks — PASS

T024 bytecode drift blocks Track A admission (admission_status=rejected_by_capability). Substring match is robust against T024 revisions that add other drift sources.

Evidence:

- tests/test_asset_admission.py::test_track_a_rejected_by_capability_on_bytecode_drift PASSED: T024 errors contain 'bytecode drift' substring -> track_a_status=TRACK_A_REJECTED_BY_CAPABILITY.
- _has_bytecode_drift helper searches report.errors for 'bytecode drift' substring; T024 chain_capability.py line 444/471 produces matching error string.

### track-a-proxy-detected-blocks — PASS

T023 EligibilityReasonCode.PROXY_DETECTED blocks Track A admission (rejected_by_proxy). Precedence: bytecode drift check runs before proxy check, so a pool with both drift and proxy reports the most restrictive reject reason.

Evidence:

- tests/test_asset_admission.py::test_track_a_rejected_by_proxy_when_eip1167_detected PASSED: T023 proxy_detected reason code -> track_a_status=TRACK_A_REJECTED_BY_PROXY, reasons include 'proxy_detected'.

### track-a-hook-unavailable-no-autopromote — PASS

T023 hook_bytecode_unavailable does NOT auto-promote; Track A admission is a dedicated non-admitted status (ingestion_only_pending_review) that signals 'on discovery surface but no permission to simulate'.

Evidence:

- tests/test_asset_admission.py::test_track_a_ingestion_only_when_hook_bytecode_unavailable PASSED: T023 HOOK_BYTECODE_UNAVAILABLE -> track_a_status=TRACK_A_INGESTION_ONLY_PENDING_REVIEW, reason includes 'hook_bytecode_unavailable'.

### p02-closeout-three-exit-gates — PASS

P02 closeout report surfaces the three README exit-gate conditions: two_provider_agreement (from ChainCapabilityReport.cross_endpoint), deployment_report_passed (from ChainCapabilityReport.passed AND empty errors), every_pool_has_support_reason (every PoolRecord has a non-empty T023 EligibilityDecision.reasons list). All three are explicitly verified with dedicated failure tests.

Evidence:

- tests/test_asset_admission.py::test_closeout_report_p02_exit_gate_met_when_all_three_passes PASSED: when T022+T023+T024 inputs are consistent the closeout report confirms two_provider_agreement=True, deployment_report_passed=True, every_pool_has_support_reason=True, p02_exit_gate_met=True.
- tests/test_asset_admission.py::test_closeout_report_two_provider_agreement_fails_on_chain_mismatch PASSED: chain_id disagree -> two_provider_agreement=False, p02_exit_gate_met=False.
- tests/test_asset_admission.py::test_closeout_report_deployment_report_fails_on_drift PASSED: bytecode_drift=True -> deployment_report_passed=False, p02_exit_gate_met=False.
- tests/test_asset_admission.py::test_closeout_report_fails_when_pool_lacks_support_reason PASSED: registry row without T023 decision -> every_pool_has_support_reason=False, p02_exit_gate_met=False.
- tests/test_asset_admission.py::test_closeout_report_from_pinned_testnet_artifact PASSED: loads docs/implement/protocol-artifacts/chain-capability-report-robinhood-testnet.json, builds the report, and confirms all three exit-gate conditions are True.

### audit-trail-evidence-pointers — PASS

Each PoolAdmissionRecord's evidence_pointers references the originating artifacts (artifact SHA, deployment tx hash, decode rule version, pool registry fields). No anonymous records.

Evidence:

- tests/test_asset_admission.py::test_evidence_pointers_include_artifact_sha_and_deployment_tx PASSED: every row's evidence_pointers include 'artifact_sha256=...', 'deployment_tx_hash=...', 'decode_rule_version=...' strings.
- _pool_record_evidence_pointers emits pool_id, tx_hash_first_seen, log_index_first_seen, block_number_first_seen, sqrt_price_x96, initial_tick for every row.

### json-round-trip-byte-stable — PASS

report_to_json / sha256_of_report are byte-stable across runs (given a pinned generated_at). The to_dict output uses sort_keys=True and indent=2 for canonical JSON.

Evidence:

- tests/test_asset_admission.py::test_report_to_json_round_trips PASSED: report_to_json produces JSON that json.loads re-parses with chain_id, two_provider_agreement, deployment_report_passed, every_pool_has_support_reason, p02_exit_gate_met all correctly preserved.
- tests/test_asset_admission.py::test_closeout_report_to_dict_is_deterministic_excluding_timestamp PASSED: two build_p02_closeout_report calls produce byte-identical to_dict when generated_at is pinned; sha256_of_report stable.

### adr-009-integer-path — PASS

ADR-009 integer path is preserved. The new asset_admission module has zero float references; identity is derived from int PoolKey fields and int addresses only (no symbol/name/decimals; ADM-TECH-002 honored).

Evidence:

- grep -n 'float' src/robinhood_lp/discovery/asset_admission.py returns no matches.
- asset_admission uses int addresses, PoolKey.fee (int), tick_spacing (int), block numbers, sqrt_price_x96 (int). No float math anywhere in the module.

### security-no-secrets — PASS

No secret material, no URL values, no API keys. The candidate diff contains only env-var-name references (none) and hex-string artifact identifiers.

Evidence:

- grep -iE 'password|secret|private_key|seed_phrase|api_key|bearer|credential|wallet' on candidate diff for src/robinhood_lp/discovery/asset_admission.py, tests/test_asset_admission.py, src/robinhood_lp/discovery/__init__.py returns no matches.
- Asset-admission module only references hex-string artifact SHA, deployment tx hash, and decode rule version (all already public).

### no-auto-promotion-to-live — PASS

No code path in asset_admission.py sets live_block to non-None or sets any admission_status to 'live'. Track B does NOT auto-promote live.

Evidence:

- grep -nE 'live_block\s*=|TRACK_A_ADMITTED|admission_status\s*=\s*"live"|RunMode\.LIVE' src/robinhood_lp/discovery/asset_admission.py shows only: field declaration with default None, dict serialisation, _compute_track_b unpacking, dataclass assignment, and TRACK_A_ADMITTED constant (Track A's 'admitted' status, not 'live').
- _compute_track_b returns None for live_block on all 5 branches (None input, APPROVED, REJECTED, REVOKED, PENDING outcome).

### no-synthetic-operator-decision — PASS

OperatorDecision must come from explicit caller input, never generated internally. T025 does NOT mint synthetic Owner decisions.

Evidence:

- grep -n 'OperatorDecision(' src/robinhood_lp/discovery/asset_admission.py returns no matches. The module never instantiates OperatorDecision.
- build_pool_admission accepts operator_decision only as a caller-supplied parameter; tests supply OperatorDecision instances explicitly.

### regression-test-eligibility — PASS

Eligibility tests are 39/39 PASS, matching the expected regression baseline (no new failures introduced).

Evidence:

- PYTHONPATH=src pytest tests/test_eligibility.py -v: 39 passed in 0.10s.

### regression-test-chain-capability — PASS

Chain capability tests are 17/17 PASS, matching the expected regression baseline.

Evidence:

- PYTHONPATH=src pytest tests/test_chain_capability.py -v: 17 passed in 0.07s.

### regression-test-initialize-scanner — PASS

Initialize scanner tests are 22/22 PASS, matching the expected regression baseline.

Evidence:

- PYTHONPATH=src pytest tests/test_initialize_scanner.py -v: 22 passed in 0.07s.

### regression-test-abi-artifacts — PASS

ABI artifacts tests show 33 passed / 1 failed, where the 1 failure is the pre-existing T012-era forge blocker confirmed identical on base e20954f (see critical-baseline-forge-regression check). No new failures introduced by T025.

Evidence:

- PYTHONPATH=src pytest tests/test_abi_artifacts.py -v: 33 passed, 1 failed (test_artifact_byte_matches_regenerated_oracle_output, identical to base e20954f failure on the same worktree; forge submodules missing).

### byte-identity-two-runs — PASS

Two-run byte-identity holds across pytest, ruff check, ruff format --check, and mypy. Volatile tokens (elapsed time, forge stderr timestamps) are the only differences per the T023 reviewer precedent.

Evidence:

- Two consecutive pytest runs of tests/test_asset_admission.py produce identical '20 passed' count.
- Two consecutive ruff check src tests runs: 'All checks passed!' (byte-identical).
- Two consecutive ruff format --check src tests runs: '48 files already formatted' (byte-identical).
- Two consecutive mypy src tests runs: 'Success: no issues found in 48 source files' (byte-identical).
- Manual sha256_of_report check on two closeout reports (with pinned generated_at) produced identical hashes.

### developer-handoff-schema-valid — PASS

Developer handoff is schema-valid and consistent with no triage_request. Outcome is CANDIDATE_READY.

Evidence:

- jsonschema validation of todo/evidence/P02/T025/attempt-001-developer.json against todo/schemas/developer-result.schema.json: OK.
- outcome=CANDIDATE_READY; triage_request=None; all required keys present; only schema-allowed properties used.

### ruff-mypy-format-pass — PASS

Static analysis and formatting checks all pass. Two consecutive runs are byte-identical (post normalisation of volatile tokens).

Evidence:

- ruff check src tests: All checks passed!
- ruff format --check src tests: 48 files already formatted.
- mypy src tests: Success: no issues found in 48 source files.

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- Pre-existing T012-era forge blocker (test_artifact_byte_matches_regenerated_oracle_output) fails in this review worktree because tools/oracle/lib/{v4-core,v4-periphery,forge-std} submodules are not installed. Confirmed identical on base e20954f in a separate fresh clone. This is an environmental artifact, not a T025 regression, but CI must install forge submodules for the test to pass.
- DEFAULT_DECODE_RULE_VERSION is a single string constant capturing the T022/T023/T024 binding. Future revisions to those modules must update this constant in the same attempt to keep the audit trail accurate; otherwise the constant will silently drift. The constant is recorded on every PoolAdmissionRecord so the audit trail is self-describing.
- P02 closeout report's generated_at uses datetime.now(UTC); to_dict output is byte-identical only when generated_at is pinned. Callers that compare reports across runs must pin or normalise the timestamp.
- The asset_admission module consumes T023 EligibilityReasonCode.PROXY_DETECTED rather than re-detecting the EIP-1167 minimal-proxy pattern. If T023 adds new rejection reason codes in a future revision, Track A precedence list in asset_admission._compute_track_a must be revised in the same attempt to preserve audit-trail accuracy.
- Track B OperatorDecision dataclass is supplied by callers; T025 does NOT define the OwnerDecision workflow route itself (T041 / T070 territory). Track B remains pending_owner by default until the OwnerDecision workflow is implemented in a later phase.
