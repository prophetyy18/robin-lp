# T023 independent review

- Base commit: `e7319cfebaef006c3d7fd1293398d9cf243fa272`
- Candidate commit: `6514a0b3d05e7977122e8d8b61bea72320fed7bd`
- Verdict: **PASS**

## Checks

### CRITICAL-baseline-check-abi-artifacts-regression — PASS

Pre-existing T012-era forge blocker per T021/T022 review evidence; FAIL is reproducible on the approved base e7319cf with identical stderr, so it is OUT OF SCOPE for T023. Per the controller decision tree (base fails AND candidate fails with the same error -> pre-existing/out-of-scope PASS), this does not block the review. The T023 candidate does not introduce the regression.

Evidence:

- Run on base e7319cf: PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output -v -> FAILED
- Run on candidate 6514a0b: same test -> FAILED
- Both runs emit identical stderr pattern: tools/oracle/lib/v4-core/src/libraries/TickMath.sol: No such file or directory (os error 2), plus v4-core/src/libraries/SqrtPriceMath.sol, v4-periphery/src/libraries/LiquidityAmounts.sol, v4-core/src/types/PoolKey.sol, PoolId.sol, Currency.sol, IHooks.sol all missing; forge build rc=1
- Cause is forge environment not having tools/oracle/lib/v4-core / v4-periphery installed in the worktree (forge-std/Test.sol and import paths unresolvable); not caused by any T023 code path
- Worktree state restored to 6514a0b after base test run via git checkout (working tree clean, HEAD detached at 6514a0b)

### diff-scope — PASS

Diff is scoped to the expected paths: eligibility.py (deliverable), test_eligibility.py (deliverable), todo/config.yaml (controller-managed workflow_state and attempt counter), and the controller-required todo/evidence developer handoff. No forbidden paths modified.

Evidence:

- git diff e7319cf..6514a0b --stat reports exactly: src/robinhood_lp/discovery/eligibility.py (+210 lines), tests/test_eligibility.py (+431 lines), todo/config.yaml (+8 lines controller bookkeeping), todo/evidence/P02/T023/attempt-001-developer.json (+52 lines developer handoff artifact)
- 4 files changed, 681 insertions(+), 20 deletions(-)

### forbidden-paths-empty-diff — PASS

Forbidden paths diff is empty. The candidate does not touch intent, spec, oracle tools, workflow tools, phase contracts, the three forbidden test modules, or the protocol/rpc/discovery (excluding eligibility) layers.

Evidence:

- git diff e7319cf..6514a0b -- docs/intent/ docs/spec/ tools/oracle/ tools/workflow/ todo/phases/ tests/test_initialize_scanner.py tests/test_abi_artifacts.py tests/test_chain_capability.py src/robinhood_lp/protocol/ src/robinhood_lp/rpc/ src/robinhood_lp/discovery/initialize_log.py src/robinhood_lp/discovery/registry.py src/robinhood_lp/discovery/chain_capability.py returned zero output

### eligibility-test-six-categories-present — PASS

All six required test categories are present and passing.

Evidence:

- static_fee category: test_classify_static_fee_no_hook_complete_metadata_is_backtest, test_classify_static_fee_no_hook_incomplete_metadata_is_ingestion, test_classify_static_fee_no_hook_partial_metadata_is_ingestion PASSED
- dynamic_fee category (V4 high bit set = 0x800000): test_classify_dynamic_fee_no_hook_complete_metadata_is_ingestion PASSED; classifier uses fee != 0x800000 as the dynamic-fee sentinel (matches docs/spec/protocol/PROTOCOL_FACTS.md isDynamicFee(fee) == fee == DYNAMIC_FEE_FLAG == 0x800000)
- zero_hook / nonzero_hook category: test_analyze_hook_zero_address, test_analyze_hook_non_zero_address_with_no_flag_bits, test_analyze_hook_non_zero_address_with_one_flag_bit, test_classify_nonzero_hook_with_only_action_flag_is_ingestion, test_classify_nonzero_hook_with_delta_flag_is_ingestion_with_delta_reason, test_classify_nonzero_hook_with_action_and_delta_is_ingestion PASSED
- EIP-1167 proxy -> REJECTED category: test_eip1167_bytecode_51_byte_variant_demotes_to_rejected, test_eip1167_bytecode_55_byte_variant_demotes_to_rejected PASSED; constants are 363d3d37363d3d3d3d363d3d3d363d73 (16-byte prefix) + 20-byte address + 5af43d82803e903d91602b57fd5bf3 (15-byte suffix), with 51- and 55-byte variants, identical to T024 chain_capability.py detection surface
- code-hash change demotion category: test_bytecode_hash_change_demotes_to_ingestion, test_bytecode_hash_match_does_not_emit_change_reason, test_bytecode_hash_change_with_no_previous_is_ignored, test_bytecode_hash_change_on_zero_hook_is_ignored, test_bytecode_hash_change_does_not_override_rejected PASSED
- unknown hook stays at INGESTION category: test_nonzero_hook_without_bytecode_is_ingestion_not_promoted, test_nonzero_hook_with_code_hash_but_no_bytecode_emits_unavailable, test_nonzero_hook_with_bytecode_does_not_emit_unavailable PASSED

### no-auto-promote-unknown-hooks — PASS

Unknown hooks are not auto-promoted to backtest/paper/live. Classification is code-hash + bytecode + behaviour based, not address-based.

Evidence:

- test_nonzero_hook_without_bytecode_is_ingestion_not_promoted asserts decision.level == RunMode.INGESTION and not decision.has(PROXY_DETECTED) and not decision.has(BYTECODE_HASH_CHANGE)
- Classify_pool branch #6b emits HOOK_BYTECODE_UNAVAILABLE without changing level; classify_pool branch #7 only triggers PROXY_DETECTED when is_eip1167_proxy is True (i.e., bytecode fetched and matched); unknown hooks can never reach BACKTEST/PAPER/LIVE
- test_proxy_detection_overrides_zero_hook_path PASSED (proxy demotes even a fully-eligible zero-hook+static-fee+complete-metadata pool to REJECTED, demonstrating precedence)

### no-address-flag-inference-of-semantics — PASS

Semantics are derived from bytecode (EIP-1167 detection) + code-hash (change detection) + dynamic-fee sentinel, not from hook address flag bits.

Evidence:

- HookEvidence captures has_any_flag / has_delta_flag as computed booleans but the classifier never promotes based on flag presence alone; HOOK_BEHAVIOUR_UNKNOWN is emitted whenever hook is non-zero regardless of flag pattern
- DYNAMIC_FEE_POOL reason is decided from record.pool_key.fee (numeric, not a flag bit); static_fee test path uses is_static_fee = fee <= 1_000_000 and fee != 0x800000, derived from PROTOCOL_FACTS.md MAX_LP_FEE = 1_000_000 and DYNAMIC_FEE_FLAG = 0x800000
- DYNAMIC_FEE_POOL reason is encoded only for zero-hook pools with fee == 0x800000; non-zero-hook pools stay at INGESTION with HOOK_BEHAVIOUR_UNKNOWN

### no-event-topics-or-address-whitelist — PASS

Eligibility classification does not consult event topics or address whitelists; reason codes are local StrEnums.

Evidence:

- grep -n 'EVENT_TOPICS' src/robinhood_lp/discovery/eligibility.py returns 0 hits
- grep -nE 'whitelist|by_name' eligibility.py returns only the must-not docstring text at line 24 ('- whitelist by name;'), not an implementation reference
- Reason codes are local StrEnum values (static_fee_plain_pool, dynamic_fee_pool, hook_address_present, hook_has_delta_flag, hook_behaviour_unknown, hook_code_hash_pinned, metadata_complete, metadata_incomplete, pool_no_hook, upgrade_proxy_observed, proxy_detected, bytecode_hash_change, hook_bytecode_unavailable); none address-based

### no-poolkey-widening-or-sqrt-price-tick-alteration — PASS

T022 PoolRecord.sqrt_price_x96 / initial_tick are not widened; PoolKey is untouched.

Evidence:

- git diff e7319cf..6514a0b -- src/robinhood_lp/discovery/registry.py is empty
- grep -n 'sqrt_price_x96\|initial_tick' src/robinhood_lp/discovery/eligibility.py returns 0 hits; the classifier only touches record.pool_id, record.pool_key.{currency0,currency1,fee,tick_spacing,hooks}, record.token0_metadata, record.token1_metadata
- _make_record in test_eligibility.py constructs PoolKey unchanged

### adr-009-integer-path-preserved — PASS

ADR-009 integer path preserved in eligibility.py.

Evidence:

- grep -n 'float' src/robinhood_lp/discovery/eligibility.py returns 0 hits (no float literals, no math.floats, no float() conversions)
- mypy --strict on the candidate returns Success: no issues found in 46 source files (byte-identical across two runs, SHA-256 a97cee1ee1bdb5e2d1ca7098f7b33a6c6a21263225643192fdeb78654687ed89)

### no-secret-material — PASS

No secret or credential strings in the candidate diff.

Evidence:

- git diff e7319cf..6514a0b -- src/robinhood_lp/discovery/eligibility.py tests/test_eligibility.py | grep -iE 'password|secret|private_key|seed_phrase|api_key|bearer|credential|wallet' returns 0 hits
- EIP-1167 prefix/suffix hex constants are not secret material (they are the public standard EIP-1167 minimal-proxy opcode stub)

### two-run-byte-identity — PASS

All gate outputs are byte-identical across two consecutive runs after normalising timestamps.

Evidence:

- pytest tests/test_eligibility.py -v: Run 1 39 passed in 0.13s; Run 2 39 passed in 0.08s; Run 3 39 passed in 0.08s. Counts byte-identical across all runs.
- ruff check: All checks passed! SHA-256 = 82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18 byte-identical across two runs
- ruff format --check: 178 files already formatted. SHA-256 = c375f615cfe3fc02a2752c83204f39308f22c13d841d979afb4dfc9eeed86c55 byte-identical across two runs
- mypy src tests: Success: no issues found in 46 source files. SHA-256 = a97cee1ee1bdb5e2d1ca7098f7b33a6c6a21263225643192fdeb78654687ed89 byte-identical across two runs

### test_chain_capability_regression — PASS

test_chain_capability.py regression check 17/17 PASS.

Evidence:

- PYTHONPATH=src pytest tests/test_chain_capability.py -v: 17 passed in 0.09s (all 17 tests in test_chain_capability.py PASS)

### test_initialize_scanner_regression — PASS

test_initialize_scanner.py regression check 22/22 PASS.

Evidence:

- PYTHONPATH=src pytest tests/test_initialize_scanner.py -v: 22 passed in 0.09s (all 22 tests in test_initialize_scanner.py PASS)

### developer-handoff-schema — PASS

Developer handoff validates against developer-result.schema.json. outcome=CANDIDATE_READY is consistent with no triage_request.

Evidence:

- jsonschema validate(todo/evidence/P02/T023/attempt-001-developer.json, todo/schemas/developer-result.schema.json) -> PASS
- outcome=CANDIDATE_READY; no triage_request present; 9 commands recorded; 6 residual_risks recorded; blocking_question=null

### full-pytest-counts-byte-identity — PASS

Pytest summary counts are byte-identical across runs. Net new from baseline: T023 attempt-1 grows test_eligibility.py from 17 to 39 tests (+22 new passes); the candidate's broader pytest totals are 1 failed / 401 passed / 6 skipped vs the baseline 0 failed / 373 passed / 6 skipped (per the T015 baseline record) = +28 net passes, +1 net fail. The +1 fail is the pre-existing T012-era blocker per CRITICAL-baseline-check.

Evidence:

- Run 1: 1 failed, 401 passed, 6 skipped in 1.92s
- Run 2: 1 failed, 401 passed, 6 skipped in 1.71s
- Run 3: 1 failed, 401 passed, 6 skipped in 1.71s
- Counts byte-identical across all three runs; only the pre-existing test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output fails (out of scope per CRITICAL-baseline-check)

### deterministic-decisions — PASS

Decisions are deterministic and audited (T023 acceptance).

Evidence:

- test_classification_is_deterministic (pre-existing) PASSED
- test_evidence_pointers_are_deterministic_across_calls (new) PASSED: two calls with identical inputs produce identical evidence_pointers lists

### eip1167-detection-matches-t024 — PASS

EIP-1167 detection in eligibility.py mirrors T024 chain_capability.py byte-for-byte on prefix/suffix/length constants and detection algorithm; the 51- and 55-byte variants are both accepted identically.

Evidence:

- T024 src/robinhood_lp/discovery/chain_capability.py line 73: EIP1167_PREFIX = 363d3d37363d3d3d3d363d3d3d363d73; line 77: EIP1167_SUFFIX = 5af43d82803e903d91602b57fd5bf3; lengths 16+20+15 and 16+4+20+15
- T023 src/robinhood_lp/discovery/eligibility.py _EIP1167_PREFIX / _EIP1167_SUFFIX / _EIP1167_LENGTH_STANDARD (51) / _EIP1167_LENGTH_VARIANT (55) carry identical values; _looks_like_eip1167_minimal_proxy algorithm matches T024's (length check, suffix check, prefix check, with leading-4-byte-zero-slot branch for the 55-byte variant)
- T023 keeps the detection local (no import of chain_capability) so the classifier is free of any rpc dependency

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- Pre-existing T012-era forge blocker (tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output) is reproducible on the approved base e7319cf with identical stderr (forge install of tools/oracle/lib/v4-core and v4-periphery has not been run in this worktree). It is OUT OF SCOPE for T023 per the CRITICAL-baseline-check decision tree and the prior T021/T022 reviewer evidence; not attributed to the candidate. The T012 owner owns the MathOracle.t.sol fix path.
- T023 duplicates the EIP-1167 prefix/suffix constants locally rather than importing chain_capability._looks_like_eip1167_minimal_proxy. This is a deliberate maintainability trade-off (the classifier must remain independent of the rpc dependency). If T024 revises its constants, the local copy in eligibility.py must be revised in the same attempt. Documented in the developer handoff's residual_risks list.
- evidence_pointers is currently a list[str] rather than a structured EvidencePointer dataclass; sufficient for T023 attempt-1's audit-trail acceptance but downstream consumers needing to branch on pointer kind will require a structured type in a future task.
- HOOK_BYTECODE_UNAVAILABLE is emitted whenever bytecode is None on a non-zero hook regardless of whether code_hash is set. This is the stricter (more audited) interpretation; a future attempt could tighten this to require both bytecode and code_hash to be None.
