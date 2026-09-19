# T027 independent review

- Base commit: `17dfe99d7342dd2d8305abcb1657cb000883d62c`
- Candidate commit: `a0686f709b33b9a4f4677245c6fad922b4bbe197`
- Verdict: **PASS**

## Checks

### diff_localized — PASS

The change remains a clean additive module under the discovery layer plus its tests and the controller-driven config transition. The attempt 2 diff extends the same file set as attempt 1 with the additional implementation of the boundary case the prior review flagged.

Evidence:

- git diff --stat 17dfe99..a0686f7: src/robinhood_lp/discovery/__init__.py +28, src/robinhood_lp/discovery/research_classification.py +997 (new), tests/test_research_classification.py +1220 (new), todo/config.yaml +-12, todo/evidence/P02/T027/attempt-001-developer.json +45, todo/evidence/P02/T027/attempt-002-developer.json +44, todo/reviews/P02/T027/review-001.json +275, todo/reviews/P02/T027/review-001.md +282
- All added source files are under the discovery/storage layer (ADR-006); no Intent, Spec, contract, workflow, controller, dependency or signer surface changed
- The new module docstring declares 'This module sits in the storage layer per ADR-006 and depends on robinhood_lp.protocol and the T023 classifier. It must not import robinhood_lp.config or higher layers.'

### prior_required_change_implemented — PASS

The required change from the prior review is fully implemented. (1) _check_flag_bits_against_code_hash is no longer a stub - it actually detects the structural disagreement case the prior review called out (non-zero hook + zero flag bits + recorded sha256 code_hash). (2) The optional flag_hash_mismatch parameter is wired into classify_research_member with the documented None/True/False semantics. (3) test_hook_flag_bits_disagree_with_code_hash_demotes now asserts HOOK_FLAG_HASH_MISMATCH is emitted and verifies the audit-trail source pointer.

Evidence:

- Prior review required: '_check_flag_bits_against_code_hash was a stub returning True, so the HOOK_FLAG_HASH_MISMATCH reason code was defined but never emitted. The test test_hook_flag_bits_disagree_with_code_hash_demotes did not assert HOOK_FLAG_HASH_MISMATCH was in the reasons list.'
- New implementation in src/robinhood_lp/discovery/research_classification.py:463-535: _check_flag_bits_against_code_hash is no longer a stub. It returns hook_evidence.has_any_flag for a non-zero hook address with a recorded sha256 code_hash, so a non-zero hook + has_any_flag=False + recorded sha256 code_hash now returns False (mismatch).
- src/robinhood_lp/discovery/research_classification.py:595,652-660,689-693,784-839: classify_research_member now exposes the optional flag_hash_mismatch: bool | None = None parameter. None falls back to the internal structural check; True forces HOOK_FLAG_HASH_MISMATCH; False suppresses the external signal so the classifier relies solely on the internal structural check.
- Empirical run (manual): PoolKey(hooks=Address(1<<20)) + HookEvidence(is_zero=False, has_any_flag=False, code_hash='d'*64) + hook_settlement_verified=True + COMPLETE coverage -> level=ingestion, has(HOOK_FLAG_HASH_MISMATCH)=True, evidence_pointer 'hook_flag_hash_mismatch=true;source=internal'.
- Empirical run (manual): PoolKey(hooks=Address(1<<7)) + HookEvidence(has_any_flag=True, code_hash='e'*64) + flag_hash_mismatch=True -> level=ingestion, has(HOOK_FLAG_HASH_MISMATCH)=True, evidence_pointer 'hook_flag_hash_mismatch=true;source=external'.
- Empirical run (manual): flag_hash_mismatch=False on consistent evidence -> has(HOOK_FLAG_HASH_MISMATCH)=False.
- tests/test_research_classification.py: test_hook_flag_bits_disagree_with_code_hash_demotes now constructs pk_zero_bits=PoolKey(hooks=Address(1<<20)) with HookEvidence(has_any_flag=False, code_hash='d'*64) and asserts d.has(ResearchClassificationReasonCode.HOOK_FLAG_HASH_MISMATCH) (test_research_classification.py:540-579) and asserts the audit pointer 'hook_flag_hash_mismatch=true;source=internal' is in evidence_pointers.
- tests/test_research_classification.py: additional 8 new tests cover: test_hook_flag_hash_mismatch_emitted_when_settlement_unverifiable_too, test_flag_hash_mismatch_caller_signal_emits_reason_without_structural_check, test_flag_hash_mismatch_false_suppresses_external_signal_but_keeps_internal, test_no_flag_hash_mismatch_without_code_hash, test_no_flag_hash_mismatch_for_zero_hook, test_no_flag_hash_mismatch_when_consistent_flag_bits_and_hash, test_no_flag_hash_mismatch_for_ill_formed_code_hash, and test_classify_research_member_requires_flag_hash_mismatch_bool.

### tests_research_classification — PASS

The new test module fully exercises the public surface; all tests pass in isolation and in combination with the neighbouring T023/T026/T100/P02 suites. The 8 new tests for the flag_hash_mismatch boundary case pass.

Evidence:

- PYTHONPATH=src python -m pytest tests/test_research_classification.py -q -> 62 passed in 0.27s
- PYTHONPATH=src python -m pytest tests/test_research_classification.py tests/test_eligibility.py tests/test_research_universe.py tests/test_research_universe_config.py tests/test_pool_onboarding.py tests/test_initialize_scanner.py -q -> 206 passed in 0.73s
- All 62 research-classification tests cover construction validation, T023 reason-code preservation, (chain_id, PoolKey) keying, unknown-hook / return-delta default, BYTECODE_HASH_CHANGE demotion, HOOK_SETTLEMENT_UNVERIFIABLE cap, EIP-1167 proxy rejected, DYNAMIC_FEE_NOT_OBSERVED cap, DATA_COVERAGE_PARTIAL cap, HOOK_FLAG_HASH_MISMATCH (internal structural + external signal + suppression + negative cases), the backtest gate across use_case in {replay, backtest, model_research}, the is_research_member_backtest_eligible helper, the ResearchNonApprovalStatement, decision to_dict audit-trail serialisation, determinism across repeated calls and decision validation

### full_suite_no_t027_regression — PASS

No T027-related regression in the full suite. The single failing test is the pre-existing T012 forge-std blocker already present at base commit 17dfe99 and unrelated to T027.

Evidence:

- PYTHONPATH=src python -m pytest -q --ignore=tests/test_abi_artifacts.py -> 1266 passed, 6 skipped in 14.24s (skips are pre-existing foundry/gpg/vector reorder skips, unchanged from base)
- PYTHONPATH=src python -m pytest -q -> 1 failed, 1300 passed, 6 skipped (the only failure is tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output: forge compile errors for v4-core/src/types/PoolId.sol / Currency.sol / IHooks.sol / etc. — the T012-era forge-std SelectorOracle blocker in tools/oracle/lib (submodules not present in the worktree))
- git diff 17dfe99..a0686f7 -- tools/oracle tests/test_abi_artifacts.py is empty: the abi_artifacts failure is identical to the base commit and is not caused by T027

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

The T023 evidence model is preserved end-to-end; the contract's first acceptance clause is satisfied.

Evidence:

- ResearchClassificationDecision carries level + reasons + evidence_pointers and exposes the underlying T023 EligibilityDecision (research_classification.py:284-291)
- classify_research_member reuses robinhood_lp.discovery.eligibility.classify_pool via a stub PoolRecord (research_classification.py:706-755)
- Test test_classify_research_member_preserves_t023_reason_codes asserts POOL_NO_HOOK, STATIC_FEE_PLAIN_POOL, METADATA_COMPLETE are preserved
- Test test_classify_research_member_carries_eligibility_decision asserts the underlying decision is exposed

### keyed_on_chain_id_pool_key_alone — PASS

Classification remains keyed on (chain_id, PoolKey) alone; the boundary case 'a pool classified identically under two different target tokens' is enforced by the absence of any target_token argument.

Evidence:

- classify_research_member signature takes chain_id and pool_key; no target_token / token-symbol parameter exists (research_classification.py:582-595)
- Decision.to_dict serialises (chain_id, PoolKey) plus derived pool_id, level, reasons, evidence_pointers and non_approval_statement
- Tests test_classify_research_member_keys_on_chain_id_and_pool_key_alone, test_classify_research_member_classified_identically_under_different_target_tokens, test_classify_research_member_never_consults_token_symbol all pass

### unknown_hook_and_return_delta_default_ingestion — PASS

Unknown hooks and return-delta flags default to ingestion through the reused T023 logic.

Evidence:

- Tests test_unknown_hook_defaults_to_ingestion and test_return_delta_flag_defaults_to_ingestion still pass; non-zero hooks stay at INGESTION via HOOK_ADDRESS_PRESENT + HOOK_BEHAVIOUR_UNKNOWN
- This is inherited from the T023 classifier via classify_pool

### code_hash_change_demotes — PASS

Behaviour / code-hash change demotion is preserved from T023 for research members.

Evidence:

- T023 logic in eligibility.py:418-439 still emits BYTECODE_HASH_CHANGE and caps level at INGESTION when previous_code_hash != code_hash on a non-zero hook
- Test test_behaviour_code_hash_change_demotes_to_ingestion passes

### hook_settlement_unverifiable_cap — PASS

A member with unverifiable hook semantics cannot be promoted beyond ingestion.

Evidence:

- _verify_hook_settlement: zero hook is trivially verified; non-zero hook requires hook_settlement_verified is True
- Tests test_hook_settlement_unverifiable_caps_at_ingestion, test_hook_settlement_verified_true_does_not_emit_unverifiable, test_hook_settlement_verified_false_emits_unverifiable, test_zero_hook_is_trivially_verified all pass
- Test test_eip1167_proxy_caps_at_rejected confirms PROXY_DETECTED from T023 wins over HOOK_SETTLEMENT_UNVERIFIABLE

### dynamic_fee_not_observed_cap — PASS

A dynamic-fee pool with no observed fee caps at ingestion with the named reason.

Evidence:

- _derive_observed_dynamic_fee_flag returns True when fee == DYNAMIC_FEE_FLAG and observed_dynamic_fee is None
- Tests test_dynamic_fee_with_no_observed_fee_caps_at_ingestion and test_dynamic_fee_with_observed_fee_does_not_emit_not_observed pass

### data_coverage_partial_cap — PASS

Partial and unknown coverage both cap at ingestion with the named reason; only complete coverage unlocks the backtest gate.

Evidence:

- Coverage handling: PARTIAL emits DATA_COVERAGE_PARTIAL and caps at INGESTION; UNKNOWN emits the same reason and caps at INGESTION until the caller pins COMPLETE
- Tests test_data_coverage_partial_caps_at_ingestion, test_data_coverage_unknown_is_treated_as_partial_until_pinned, test_data_coverage_complete_enables_backtest_for_static_zero_hook all pass

### metadata_call_failure_boundary — PASS

The boundary case 'a pool whose metadata call fails' is covered through T023's METADATA_INCOMPLETE path; T027 preserves the audit trail.

Evidence:

- T023 logic in eligibility.py:_metadata_complete checks both tokens via TokenMetadataRecord.is_complete(); metadata_complete=False on the T027 stub builds None symbol/name/decimals so is_complete() returns False
- Test test_decision_records_metadata_incomplete_for_static_zero_hook asserts METADATA_INCOMPLETE is emitted and level=INGESTION when metadata_complete=False

### backtest_gate_refuses_below_with_named_reason — PASS

The backtest gate refuses a research member below backtest for replay / backtest / model_research with its named reason; reason codes are surfaced in the error message.

Evidence:

- assert_research_member_backtest_eligible raises ResearchSupportGateError when level.value is not in (BACKTEST, PAPER, LIVE)
- is_research_member_backtest_eligible returns the boolean form
- Use cases are Literal['replay', 'backtest', 'model_research']; the function rejects unknown use_case values
- Tests test_gate_passes_for_backtest_decision, test_gate_refuses_below_backtest_with_named_reason, test_gate_refuses_rejected_level, test_gate_rejects_unknown_use_case, test_gate_rejects_non_decision_input, test_is_research_member_backtest_eligible_helper all pass

### non_approval_statement_machine_readable — PASS

A research classification carries a machine-readable non-approval statement; the decision cannot be read as a token approval, a pool approval, or as granting execution authority.

Evidence:

- ResearchNonApprovalStatement is a frozen dataclass with Literal[True]/Literal[False] fields for is_research_only, is_token_approval, is_pool_approval, grants_execution_authority, depends_on_target_token, depends_on_token_symbol, identity_key_kind
- to_dict serialises all fields; the to_dict output is embedded in ResearchClassificationDecision.to_dict
- Tests test_non_approval_statement_is_research_only, test_non_approval_statement_to_dict_is_machine_readable, test_decision_to_dict_carries_full_audit_trail all pass

### deterministic_and_audited — PASS

Classification decisions are deterministic and audited.

Evidence:

- Tests test_decision_is_deterministic_for_identical_inputs and test_decision_is_byte_identical_across_repeated_calls pass
- The decision carries reasons (code + detail) and evidence_pointers (list[str]) forming the audit trail
- Test test_classify_research_member_has_at_least_one_reason asserts every decision carries at least one reason

### demoted_member_visible_to_research_consumer — PASS

A member demoted while a research dataset references it is observable on the decision and the gate surfaces the named reason.

Evidence:

- Test test_demoted_decision_visible_to_research_consumer passes: a static zero-hook pool with PARTIAL coverage produces a decision at level=INGESTION; the gate surfaces DATA_COVERAGE_PARTIAL in the error message
- ResearchSupportGateError message includes decision.chain_id, decision.pool_id, use_case, decision.level, decision.reason_codes and the non-approval statement text

### hook_flag_hash_mismatch_boundary — PASS

The boundary case 'a hook address whose flag bits disagree with its code hash' is now actually covered. The detection function is no longer a stub, the optional caller signal is wired, the demotion path is exercised end-to-end, and the test asserts HOOK_FLAG_HASH_MISMATCH is emitted.

Evidence:

- _check_flag_bits_against_code_hash is no longer a stub. It returns hook_evidence.has_any_flag for a non-zero hook with a recorded sha256 code_hash, so non-zero hook + zero low-14 flag bits + recorded sha256 code_hash returns False (mismatch).
- classify_research_member exposes the optional flag_hash_mismatch: bool | None = None parameter with documented semantics: None falls back on internal structural check; True forces HOOK_FLAG_HASH_MISMATCH; False suppresses external signal and relies on internal structural check only.
- classify_research_member emits HOOK_FLAG_HASH_MISMATCH for either internal_mismatch or external_mismatch (or both) and caps the level at INGESTION (REJECTED wins per the precedence rule); the audit-trail evidence_pointer records the source ('internal', 'external', or 'internal+external').
- Test test_hook_flag_bits_disagree_with_code_hash_demotes (updated) asserts d.has(HOOK_FLAG_HASH_MISMATCH) and the audit pointer 'hook_flag_hash_mismatch=true;source=internal' for a non-zero hook with zero flag bits and a recorded sha256 code_hash.
- Tests test_hook_flag_hash_mismatch_emitted_when_settlement_unverifiable_too, test_flag_hash_mismatch_caller_signal_emits_reason_without_structural_check, test_flag_hash_mismatch_false_suppresses_external_signal_but_keeps_internal cover the combined-source, external-only, and suppression paths.
- Tests test_no_flag_hash_mismatch_without_code_hash, test_no_flag_hash_mismatch_for_zero_hook, test_no_flag_hash_mismatch_when_consistent_flag_bits_and_hash, test_no_flag_hash_mismatch_for_ill_formed_code_hash cover the negative paths (no recorded hash / zero hook / consistent evidence / ill-formed hash).
- All 8 new tests for the boundary case pass.

### must_not_infer_semantics_from_flag_bits_alone — PASS

The classifier does not infer semantics from hook address flags alone (must-not rule satisfied).

Evidence:

- _check_flag_bits_against_code_hash only returns False when the framework has recorded a sha256 code_hash AND has_any_flag=False on a non-zero hook (or the caller passes flag_hash_mismatch=True). It does not infer semantics from flag bits alone.
- A non-zero hook with no recorded hash stays at ingestion via T023's HOOK_BEHAVIOUR_UNKNOWN path; flag bits alone are never used as a promotion signal
- Test test_no_flag_hash_mismatch_without_code_hash verifies the mismatch reason is NOT emitted when no code_hash is recorded

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
- The HOOK_FLAG_HASH_MISMATCH path now actively emits a reason instead of silently treating the row as plain-pool

### must_not_research_classification_stands_in_for_approval — PASS

A research classification cannot be read as a token / pool approval or as granting execution authority.

Evidence:

- ResearchNonApprovalStatement is attached to every ResearchClassificationDecision
- is_token_approval=Literal[False], is_pool_approval=Literal[False], grants_execution_authority=Literal[False]
- The ResearchSupportGateError message embeds the non-approval statement text

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
- test_hook_settlement_unverifiable_caps_at_ingestion and test_dynamic_fee_with_no_observed_fee_caps_at_ingestion verify the caps

### scope_discipline_storage_layer — PASS

Scope discipline matches ADR-006; the local DYNAMIC_FEE_FLAG is pinned to the config-layer constant so the two cannot drift silently.

Evidence:

- Module docstring declares the storage-layer boundary and the no-config-or-higher-layers rule
- Imports are limited to robinhood_lp.discovery.eligibility, robinhood_lp.protocol, robinhood_lp.discovery.registry, robinhood_lp.discovery.token_metadata (lazy imports inside the function)
- DYNAMIC_FEE_FLAG is locally re-declared with a constant-value test test_dynamic_fee_flag_matches_config_layer_constant that pins parity with robinhood_lp.config.models.DYNAMIC_FEE_FLAG

### controller_state_transition_only — PASS

The config.yaml change is the expected controller-applied READY -> AWAITING_REVIEW transition. A stale candidate_commit value is noted as a residual risk but does not affect the review verdict.

Evidence:

- todo/config.yaml diff shows only the controller-driven transition: workflow_state READY -> AWAITING_REVIEW, status READY -> AWAITING_REVIEW, attempt 0 -> 2, base_commit null -> 17dfe99..., candidate_commit null -> 671ff8c..., latest_review -> todo/reviews/P02/T027/review-001.json
- No semantic change to other tasks' state
- Stale value: candidate_commit is recorded as 671ff8c9e4e64f39a014fa6a0a78ed9aac5a85cf (attempt 1's commit) even though attempt=2 and the actual candidate_commit on the worktree is a0686f709b33b9a4f4677245c6fad922b4bbe197. The controller's bookkeeping did not advance candidate_commit between attempts; this is a controller-state inconsistency that does not affect the code being reviewed (HEAD is at the attempt-2 candidate a0686f7 and that is the commit under review)

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- DESIGN: _check_flag_bits_against_code_hash only detects the structurally inconsistent shape (non-zero hook + has_any_flag=False + recorded sha256 code_hash). A deeper disagreement where has_any_flag=True but the bytecode's actual callback set does not match the flag bits requires the external flag_hash_mismatch=True signal from an upstream verifier (T043 hook pack / replay model). The classifier cannot inspect bytecode itself per T023; this is documented in research_classification.py:519-535 and the developer evidence.
- OUT-OF-SCOPE: tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output fails because the worktree lacks tools/oracle/lib (forge-std submodule clone). This is the pre-existing T012-era blocker identical to the base commit 17dfe99 and unrelated to T027.
- CONTROLLER: todo/config.yaml records candidate_commit=671ff8c9e4e64f39a014fa6a0a78ed9aac5a85cf (attempt 1's commit) even though attempt=2. HEAD is correctly at the attempt-2 candidate a0686f709b33b9a4f4677245c6fad922b4bbe197 and that is the commit actually under review, so the review verdict is unaffected; the stale value should be advanced by the next controller transition.
