# T027 independent review

- Base commit: `17dfe99d7342dd2d8305abcb1657cb000883d62c`
- Candidate commit: `671ff8c9e4e64f39a014fa6a0a78ed9aac5a85cf`
- Verdict: **FAIL**

## Checks

### diff_localized — PASS

The change is a clean additive module under the discovery layer (storage layer per ADR-006) plus its tests and the controller-driven config transitions.

Evidence:

- git diff --stat shows exactly: src/robinhood_lp/discovery/__init__.py +28, src/robinhood_lp/discovery/research_classification.py +897 (new), tests/test_research_classification.py +1024 (new), todo/config.yaml +-8, todo/evidence/P02/T027/attempt-001-developer.json +45
- No source files outside the discovery layer and no contract / spec / workflow / dependency changes

### tests_research_classification — PASS

The new test module fully exercises the public surface; all tests pass in isolation and in combination with neighbouring suites.

Evidence:

- PYTHONPATH=src python -m pytest tests/test_research_classification.py -q -> 54 passed in 0.29s
- PYTHONPATH=src python -m pytest tests/test_research_classification.py tests/test_eligibility.py tests/test_research_universe.py tests/test_research_universe_config.py tests/test_pool_onboarding.py tests/test_initialize_scanner.py -q -> 198 passed in 0.75s
- All 54 new tests cover construction validation, T023 reason-code preservation, (chain_id, PoolKey) keying, unknown-hook / return-delta default, BYTECODE_HASH_CHANGE demotion, HOOK_SETTLEMENT_UNVERIFIABLE cap, EIP-1167 proxy rejected, DYNAMIC_FEE_NOT_OBSERVED cap, DATA_COVERAGE_PARTIAL cap, the backtest gate across use_case in {replay, backtest, model_research}, the is_research_member_backtest_eligible helper, the ResearchNonApprovalStatement, decision to_dict audit-trail serialisation, determinism across repeated calls and decision validation

### full_suite_clean_of_t027_regression — PASS

The only failing test is a pre-existing, unrelated T012 forge-std blocker; no T027-related regression in the full suite.

Evidence:

- PYTHONPATH=src python -m pytest -q -> 1 failed, 1292 passed, 6 skipped in 15.32s
- The single failure is tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output with forge compile errors for TickMath.sol / SqrtPriceMath.sol / PoolKey.sol etc. (No such file or directory, os error 2)
- The failure mode is the T012-era forge-std SelectorOracle compile blocker in tools/oracle (lib/ submodules not present in the worktree); it is unrelated to T027 and matches the same pre-existing state on the base commit 17dfe99

### ruff_lint_and_format — PASS

Lint and formatting are clean across both src and tests.

Evidence:

- python -m ruff check src tests -> 'All checks passed!'
- python -m ruff format --check src tests -> '143 files already formatted'

### mypy_strict — PASS

mypy strict passes on src and on the new test module together.

Evidence:

- PYTHONPATH=src python -m mypy src -> 'Success: no issues found in 82 source files'
- PYTHONPATH=src python -m mypy src tests/test_research_classification.py -> 'Success: no issues found in 83 source files'

### t023_evidence_model_preserved — PASS

The T023 evidence model (reason codes, evidence_pointers, EligibilityDecision) is preserved end-to-end; the contract's first acceptance clause is satisfied.

Evidence:

- ResearchClassificationDecision carries level + reasons + evidence_pointers (research_classification.py:284-288)
- ResearchClassificationDecision.eligibility_decision exposes the underlying T023 EligibilityDecision (research_classification.py:289)
- classify_research_member reuses robinhood_lp.discovery.eligibility.classify_pool via a stub PoolRecord (research_classification.py:678-685)
- Test test_classify_research_member_preserves_t023_reason_codes asserts POOL_NO_HOOK, STATIC_FEE_PLAIN_POOL, METADATA_COMPLETE are preserved; test_classify_research_member_carries_eligibility_decision asserts the underlying decision is exposed

### keyed_on_chain_id_pool_key_alone — PASS

Classification is keyed on (chain_id, PoolKey) alone and the boundary case 'a pool classified identically under two different target tokens' is enforced by the absence of any target_token argument.

Evidence:

- classify_research_member signature: chain_id, pool_key, *, hook_evidence, metadata_complete, data_coverage_status, observed_dynamic_fee, hook_settlement_verified (research_classification.py:553-562)
- There is no target_token parameter; the classifier cannot consult a token approval state
- Decision.to_dict serialises the (chain_id, PoolKey) identity plus its derived pool_id, level, reasons, evidence_pointers and non_approval_statement (research_classification.py:355-380)
- Test test_classify_research_member_keys_on_chain_id_and_pool_key_alone asserts identical decisions for identical inputs
- Test test_classify_research_member_classified_identically_under_different_target_tokens asserts byte-identical to_dict for the same (chain_id, PoolKey) regardless of caller context
- Test test_classify_research_member_never_consults_token_symbol asserts no 'symbol' appears in the joined evidence

### unknown_hook_and_return_delta_default_ingestion — PASS

Unknown hooks and return-delta flags default to ingestion through the reused T023 logic.

Evidence:

- Test test_unknown_hook_defaults_to_ingestion: PK_NONZERO_HOOK with hook_settlement_verified=False, COMPLETE coverage -> level=INGESTION with HOOK_ADDRESS_PRESENT + HOOK_BEHAVIOUR_UNKNOWN
- Test test_return_delta_flag_defaults_to_ingestion: PK_NONZERO_HOOK_DELTA -> level=INGESTION with HOOK_HAS_DELTA_FLAG
- Both inherit from the T023 classifier via classify_pool, which already caps non-zero hooks at INGESTION until the framework has a pinned code hash and verified implementation

### code_hash_change_demotes — PASS

Behaviour / code-hash change demotion is preserved from T023 for research members (the contract's explicit extension).

Evidence:

- T023 logic in eligibility.py:418-439 emits BYTECODE_HASH_CHANGE and caps level at INGESTION when previous_code_hash != code_hash on a non-zero hook
- Test test_behaviour_code_hash_change_demotes_to_ingestion sets previous='a'*64 / current='b'*64 on a non-zero hook and asserts level=INGESTION + BYTECODE_HASH_CHANGE in the research decision

### hook_settlement_unverifiable_cap — PASS

A member with unverifiable hook semantics cannot be promoted beyond ingestion (contract clause satisfied).

Evidence:

- _verify_hook_settlement (research_classification.py:534-550): zero hook is trivially verified; non-zero hook requires hook_settlement_verified is True
- Tests test_hook_settlement_unverifiable_caps_at_ingestion, test_hook_settlement_verified_true_does_not_emit_unverifiable, test_hook_settlement_verified_false_emits_unverifiable, test_zero_hook_is_trivially_verified all pass
- Test test_eip1167_proxy_caps_at_rejected confirms PROXY_DETECTED from T023 still wins over HOOK_SETTLEMENT_UNVERIFIABLE

### dynamic_fee_not_observed_cap — PASS

A dynamic-fee pool with no observed fee caps at ingestion with the named reason (contract boundary case covered).

Evidence:

- _derive_observed_dynamic_fee_flag (research_classification.py:513-531): returns True when fee == DYNAMIC_FEE_FLAG and observed_dynamic_fee is None
- Test test_dynamic_fee_with_no_observed_fee_caps_at_ingestion asserts level=INGESTION + DYNAMIC_FEE_NOT_OBSERVED for PK_DYNAMIC_NO_OBSERVED_FEE without observed_dynamic_fee
- Test test_dynamic_fee_with_observed_fee_does_not_emit_not_observed asserts the reason is NOT emitted when observed_dynamic_fee is provided

### data_coverage_partial_cap — PASS

Partial and unknown coverage both cap at ingestion with the named reason; only complete coverage unlocks the backtest gate.

Evidence:

- Coverage handling at research_classification.py:741-770: PARTIAL emits DATA_COVERAGE_PARTIAL and caps at INGESTION; UNKNOWN emits the same reason and caps at INGESTION until the caller pins COMPLETE
- Test test_data_coverage_partial_caps_at_ingestion, test_data_coverage_unknown_is_treated_as_partial_until_pinned, test_data_coverage_complete_enables_backtest_for_static_zero_hook all pass

### metadata_call_failure_boundary — PASS

The boundary case 'a pool whose metadata call fails' is covered through T023's METADATA_INCOMPLETE path; T027 preserves the audit trail.

Evidence:

- T023 logic in eligibility.py:_metadata_complete checks both tokens via TokenMetadataRecord.is_complete(); metadata_complete=False on the T027 stub builds None symbol/name/decimals so is_complete() returns False
- Test test_decision_records_metadata_incomplete_for_static_zero_hook asserts METADATA_INCOMPLETE is emitted and level=INGESTION when metadata_complete=False

### backtest_gate_refuses_below_with_named_reason — PASS

The backtest gate refuses a research member below backtest for replay / backtest / model_research with its named reason; the reason codes (including the T027-specific DATA_COVERAGE_PARTIAL) are surfaced in the error message.

Evidence:

- assert_research_member_backtest_eligible (research_classification.py:805-854) raises ResearchSupportGateError when level.value is not in (BACKTEST, PAPER, LIVE)
- is_research_member_backtest_eligible (research_classification.py:857-875) returns the boolean form
- Use cases are Literal['replay', 'backtest', 'model_research']; the function rejects unknown use_case values
- Test test_gate_passes_for_backtest_decision, test_gate_refuses_below_backtest_with_named_reason, test_gate_refuses_rejected_level, test_gate_rejects_unknown_use_case, test_gate_rejects_non_decision_input, test_is_research_member_backtest_eligible_helper all pass
- Empirical run with COMPLETE metadata + PARTIAL coverage produced: 'research member (chain_id=46630, pool_id=0xb7a0...) refused for use_case="backtest": level="ingestion" is below backtest; reason_codes=[..., 'data_coverage_partial', ...]'

### non_approval_statement_machine_readable — PASS

A research classification carries a machine-readable non-approval statement; the decision cannot be read as a token approval, a pool approval, or as granting execution authority.

Evidence:

- ResearchNonApprovalStatement (research_classification.py:190-249) is a frozen dataclass with Literal[True]/Literal[False] fields for is_research_only, is_token_approval, is_pool_approval, grants_execution_authority, depends_on_target_token, depends_on_token_symbol, identity_key_kind
- to_dict serialises all fields; the to_dict output is embedded in ResearchClassificationDecision.to_dict
- Test test_non_approval_statement_is_research_only and test_non_approval_statement_to_dict_is_machine_readable verify the statement fields and serialisation
- Test test_decision_to_dict_carries_full_audit_trail verifies the schema is 'robinhood_lp.discovery.research_classification.v1' and all required keys are present

### deterministic_and_audited — PASS

Classification decisions are deterministic and audited; byte-for-byte reproducible given identical inputs.

Evidence:

- Test test_decision_is_deterministic_for_identical_inputs asserts byte-identical to_dict across two calls
- Test test_decision_is_byte_identical_across_repeated_calls runs the same across four representative PoolKey / kwargs pairs and asserts to_dict equality
- The decision carries reasons (EligibilityReason | ResearchClassificationReason, code + detail) and evidence_pointers (list[str]); together they form the audit trail
- Test test_classify_research_member_has_at_least_one_reason asserts every decision carries at least one reason

### demoted_member_visible_to_research_consumer — PASS

A member demoted while a research dataset references it is observable on the decision and the gate surfaces the named reason (contract boundary case covered).

Evidence:

- Test test_demoted_decision_visible_to_research_consumer: a static zero-hook pool with PARTIAL coverage produces a decision at level=INGESTION; the gate surfaces DATA_COVERAGE_PARTIAL in the error message
- ResearchSupportGateError message includes decision.chain_id, decision.pool_id, use_case, decision.level, decision.reason_codes and the non-approval statement text

### hook_flag_hash_mismatch_boundary — FAIL

The boundary case 'a hook address whose flag bits disagree with its code hash' is NOT actually covered. The reason code HOOK_FLAG_HASH_MISMATCH is defined in ResearchClassificationReasonCode and the demotion-to-ingestion path is wired in classify_research_member, but the detection function _check_flag_bits_against_code_hash is a stub that always returns True, so the reason is never emitted by the classifier. There is no API for an external verifier to pass the mismatch signal either. The contract's boundary case is therefore only partially covered (enumeration + demotion wiring) but not actually implemented.

Evidence:

- _check_flag_bits_against_code_hash (research_classification.py:463-503) is a stub: the four code paths return True, the trailing comment 'Without bytecode, a flag-bits-vs-code-hash mismatch can only be reported by the caller (see ``previous_code_hash`` below)' is followed by an unconditional 'return True'
- There is no caller-facing parameter that conveys 'flag bits disagree with code hash' (HookEvidence has no such field; classify_research_member exposes no such parameter)
- Empirical test: pk_zero_bits = PoolKey(hooks=Address(1<<20)) + HookEvidence(code_hash='d'*64, has_any_flag=False) + hook_settlement_verified=True + COMPLETE coverage -> level=INGESTION with reasons [HOOK_ADDRESS_PRESENT, HOOK_BEHAVIOUR_UNKNOWN, HOOK_CODE_HASH_PINNED, HOOK_BYTECODE_UNAVAILABLE, RESEARCH_SCOPE]; HOOK_FLAG_HASH_MISMATCH is NOT emitted
- Test test_hook_flag_bits_disagree_with_code_hash_demotes asserts only HOOK_SETTLEMENT_UNVERIFIABLE; the in-source comment confirms 'the level is capped at ingestion because the hook's settlement effect is unverifiable' rather than because of HOOK_FLAG_HASH_MISMATCH
- Contract acceptance explicitly lists 'a hook address whose flag bits disagree with its code hash' as a boundary case to be covered

### must_not_infer_semantics_from_flag_bits_alone — PASS

The classifier does not infer semantics from hook address flags alone (must-not rule satisfied).

Evidence:

- _check_flag_bits_against_code_hash explicitly comments 'flag bits alone are not a verdict'; non-zero hook stays at ingestion via HOOK_BEHAVIOUR_UNKNOWN until evidence (code_hash, bytecode, settlement verified) supports promotion
- T023 reasoning is preserved; classify_research_member does not invent semantics from the address alone

### must_not_whitelist_by_name — PASS

No whitelist by name; classification is purely identity-based.

Evidence:

- No symbol-based or name-based dispatch anywhere in research_classification.py
- Test test_classify_research_member_never_consults_token_symbol verifies 'symbol' does not appear in joined evidence

### must_not_fall_back_to_plain_pool — PASS

Unknown hook / return-delta behaviour does not fall back to plain-pool promotion.

Evidence:

- Tests test_unknown_hook_defaults_to_ingestion and test_return_delta_flag_defaults_to_ingestion confirm non-zero hooks stay at ingestion regardless of fee or static-fee patterns
- HOOK_BEHAVIOUR_UNKNOWN and HOOK_HAS_DELTA_FLAG reasons are preserved

### must_not_research_classification_stands_in_for_approval — PASS

A research classification cannot be read as a token / pool approval or as granting execution authority.

Evidence:

- ResearchNonApprovalStatement is attached to every ResearchClassificationDecision; the schema string is 'robinhood_lp.discovery.research_classification.v1'
- is_token_approval=Literal[False], is_pool_approval=Literal[False], grants_execution_authority=Literal[False]
- The ResearchSupportGateError message embeds the non-approval statement text so a rejection cannot be misread as an approval

### must_not_classify_by_symbol_or_target_token_context — PASS

Classification is independent of token symbol and target-token context.

Evidence:

- No token-symbol parameter on classify_research_member; no target_token parameter
- ResearchNonApprovalStatement.depends_on_token_symbol=Literal[False], depends_on_target_token=Literal[False]
- Test test_classify_research_member_classified_identically_under_different_target_tokens verifies byte-identical to_dict for the same (chain_id, PoolKey) regardless of caller context

### must_not_promote_above_ingestion_without_execution_evidence — PASS

A research member cannot be promoted above ingestion without the evidence the execution path would require.

Evidence:

- classify_research_member only promotes to BACKTEST when (a) the T023 classifier promotes (zero hook, static fee, metadata complete) and (b) the caller has pinned data_coverage_status=COMPLETE and the hook is verified / zero
- Every non-zero hook without explicit hook_settlement_verified=True stays at ingestion

### scope_discipline_storage_layer — PASS

Scope discipline matches ADR-006; the local DYNAMIC_FEE_FLAG is pinned to the config-layer constant so the two cannot drift silently.

Evidence:

- Module docstring declares 'This module sits in the storage layer per ADR-006' and 'It must not import robinhood_lp.config or higher layers.'
- Imports are limited to robinhood_lp.discovery.eligibility, robinhood_lp.protocol, robinhood_lp.discovery.registry, robinhood_lp.discovery.token_metadata (lazy imports inside the function to keep module-load cheap)
- DYNAMIC_FEE_FLAG is locally re-declared with a constant-value test test_dynamic_fee_flag_matches_config_layer_constant that pins parity with robinhood_lp.config.models.DYNAMIC_FEE_FLAG

### controller_state_transition_only — PASS

The config.yaml change is the expected controller-applied READY -> AWAITING_REVIEW transition for this attempt.

Evidence:

- todo/config.yaml diff shows only the controller-driven transition: workflow_state READY -> AWAITING_REVIEW, status READY -> AWAITING_REVIEW, attempt 0 -> 1, base_commit null -> 17dfe99..., candidate_commit null
- No semantic change to other tasks' state

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- Implement the actual detection of the 'hook flag bits disagree with code hash' boundary case in _check_flag_bits_against_code_hash. The current function (research_classification.py:463-503) is a stub that always returns True, so the HOOK_FLAG_HASH_MISMATCH reason code is defined and the demotion-to-ingestion path is wired but the reason is never emitted. The T027 contract explicitly lists 'a hook address whose flag bits disagree with its code hash' as a boundary case that must be covered, and the test test_hook_flag_bits_disagree_with_code_hash_demotes does not currently assert HOOK_FLAG_HASH_MISMATCH is in the reasons list. Either (a) make _check_flag_bits_against_code_hash return False when the supplied code_hash disagrees with the hook address's flag bits (e.g. a non-zero hook with has_any_flag=False plus a recorded code_hash, or a flag-bits-set hook whose recorded code_hash classifies as no-hook), or (b) add a caller-supplied flag_hash_mismatch signal on classify_research_member so an external verifier can emit the reason. Either way, the test must assert that HOOK_FLAG_HASH_MISMATCH is emitted for at least one concrete flag-bits-vs-code-hash disagreement.

## Residual risks

- DESIGN: classify_research_member takes a metadata_complete boolean (default False) so the T023 classifier can promote zero-hook static-fee research members to backtest when the framework already has complete token metadata. The classifier does not fetch metadata itself; the caller's evidence is the authoritative source. A future task that needs the actual metadata values can extend the API without re-running the T023 classifier. This is consistent with T023's 'absence of evidence is not evidence of absence' invariant and is documented in the developer evidence.
- OUT-OF-SCOPE: tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output fails because the worktree lacks tools/oracle/lib (forge-std submodule clone). This is a pre-existing T012-era blocker identical to the base commit 17dfe99 and unrelated to T027.
