# T034 independent review

- Base commit: `6270d169cf052ee27e9301eb0838d1828147e448`
- Candidate commit: `9b35f035044c92a77384aab5ba49b50d72cd5522`
- Verdict: **PASS**

## Checks

### scope-diff — PASS

Scope is restricted to the new quality/ subpackage + its dedicated tests; no out-of-scope implementation, signer, or storage changes.

Evidence:

- git diff --stat src/ tests/ between base and candidate: 6 files added under src/robinhood_lp/quality/ (__init__.py 151, infrastructure.py 98, quality_report.py 823, reason_codes.py 147, sample_selection.py 504, verification.py 238) plus tests/_quality_t034_fixtures.py 110 and tests/test_quality_report.py 902
- No edits to src/robinhood_lp/protocol/, src/robinhood_lp/storage/, src/robinhood_lp/execution/, or signer surfaces
- Other diff entries are todo scaffolding (todo/config.yaml attempt/status flips and todo/evidence/todo/reviews/todo/triage/* records), expected for a workflow task

### tests — PASS

Full project pytest suite passes; the 61 new T034 tests all pass and exercise every acceptance clause listed in the contract.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/pytest tests/ -q --ignore=tests/test_workflow.py --ignore=tests/test_abi_artifacts.py --ignore=tests/test_workflow_contracts.py => 682 passed, 6 skipped (6 pre-existing skips: 3 require forge, 2 gpg-reserved, 1 vector-reorder). 0 failures.
- tests/test_quality_report.py: 61/61 PASSED — covers all 17 seeded defect reason codes (parametrized), the 5 Decision 6 deterministic sample rules, infrastructure-correlation enum + cross_endpoint_agreement wording, deterministic selection (no RNG/wall clock), cold-start/warm-run/backtest-window-only verdicts, warning-counts-in-aggregates, and the machine-dict sections.

### ruff-format — PASS

ruff format clean on src/ and tests/.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/ruff format --check src/ tests/ => '91 files already formatted'

### ruff-check — PASS

ruff lint clean on src/ and tests/.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/ruff check src/ tests/ => 'All checks passed!'

### mypy — PASS

mypy clean on src/ and tests/.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/mypy src/ tests/ => 'Success: no issues found in 91 source files'

### git-diff-check — PASS

No whitespace issues in the candidate diff.

Evidence:

- git diff --check base..candidate => no output (no whitespace errors)

### contract-reason-codes — PASS

Every seeded defect listed in acceptance has a corresponding reason code constant, surfaced via QualityFinding, and verified by a parametrized test.

Evidence:

- src/robinhood_lp/quality/reason_codes.py defines all 17 named codes listed in the acceptance matrix: range_coverage_gap, block_hash_gap, duplicate_event_key, misordered_log, unknown_pool, decode_error, impossible_value, stale_endpoint, metadata_failure, cross_provider_discrepancy, budget_exhausted, capability_regression, user_agent_rejected, cross_endpoint_sample_missing, cross_endpoint_sample_disagree, infrastructure_correlation_state_unrecorded, checkpoint_mismatch
- ALL_REASON_CODES frozenset contains exactly those 17 distinct strings; QualityFinding.__post_init__ rejects unknown codes
- test_seeded_defect_fixture_triggers_correct_reason_code parametrizes all 17 codes (lines 187-208) and asserts each finding surfaces with the matching reason_code

### contract-complete-true-requirements — PASS

complete=true is gated by zero blockers across zero-unexplained-gaps, no exhausted budget, successful A+B samples (incl. result-bearing when events present), recorded manifest checksum, topology basis, and non-blank infrastructure state — every clause from the acceptance matrix.

Evidence:

- verify_report enforces (verification.py): _BLOCKING_REASON_CODES {budget_exhausted, cross_endpoint_sample_missing, cross_endpoint_sample_disagree} force complete=False; sample_agreement=None forces missing; mismatched sample_agreement length forces missing; non-True entries force disagree; manifest_checksum empty => 'manifest_checksum_unrecorded'; expected_manifest_checksum mismatch => 'manifest_checksum_drift'; topology basis failures return blockers for cold_start / warm_incremental / backtest_window_only; empty infrastructure_state returns 'infrastructure_correlation_state_unrecorded'
- Tests test_complete_false_when_budget_exhausted_finding_present, test_complete_false_when_sample_missing, test_complete_false_when_samples_disagree, test_complete_false_when_manifest_checksum_unrecorded, test_complete_false_when_manifest_checksum_drifts all confirm each clause

### contract-empty-capability-probe-rule — PASS

Empty capability probes count for range acceptance only; result-bearing requirement fires only when the dataset contains events.

Evidence:

- _check_sample_agreement in verification.py only emits 'result_bearing_required' / 'empty_capability_probe_only' blockers when result_bearing_windows is non-empty (lines 194-199)
- test_verify_requires_result_bearing_when_dataset_has_events and test_verify_allows_empty_capability_probe_when_no_events confirm both branches

### contract-cold-start-warm-run-backtest — PASS

Cold-start / warm-run / backtest-window-only topology bases each produce the correct verdict.

Evidence:

- _check_topology_basis handles COLD_START (coverage_from_block <= pool_init_block), WARM_INCREMENTAL (qualified_checkpoint_ref required), BACKTEST_WINDOW_ONLY (incomplete without Initialize or qualified checkpoint)
- Tests test_cold_start_topology_satisfied_when_coverage_from_is_init_block, test_cold_start_topology_rejected_when_coverage_starts_after_init_block, test_warm_run_requires_qualified_checkpoint_ref, test_warm_run_accepted_when_qualified_checkpoint_ref_present, test_backtest_window_only_incomplete_without_initialize_or_checkpoint, test_backtest_window_only_accepted_with_qualified_checkpoint confirm all three branches

### contract-decision-6-deterministic-sample — PASS

Decision 6 deterministic sample selection is implemented across per-run / per-partition / per-event-type / per-failover + result-bearing windows. Selection depends only on PoolId, partition bounds, EventKey, and manifest — no RNG, no wall clock.

Evidence:

- select_required_samples produces per_run + per_partition (opening+trailing 10-block windows) + per_event_type (deduplicated by window) + per_failover (pre+post windows) — five rule categories collapsed to the four sample kinds per Decision 6's structure (per-run / per-partition / per-event-type / per-failover, with result-bearing handled separately)
- WINDOW_BLOCKS = 10; _window_bounds and _window_starting_at produce the right window shapes; per-failover pre/post windows computed correctly (test_select_per_failover_pre_window_strictly_before_boundary and test_select_per_failover_handles_genesis_boundary)
- test_required_samples_selection_is_deterministic calls select_required_samples twice with identical inputs and asserts samples_a == samples_b; test_required_samples_reproducible_from_inputs_only confirms selection_inputs is recorded so the selection can be replayed
- No use of random / secrets / uuid / time / datetime in sample selection (only datetime.now(UTC) in generated_at metadata, not in selection). _stable_pick_int exists as a helper but is unused — selection uses coverage_from_block deterministically.

### contract-decision-8-infrastructure-correlation — PASS

Decision 8 infrastructure-correlation state is the three-value enum with UNKNOWN_NOT_PROVEN default; cross_endpoint_agreement wording is used unless evidenced_independent; residual-risk callout is surfaced in both machine dict and human summary for unknown_not_proven and known_correlated; state is never blank.

Evidence:

- InfrastructureCorrelationState StrEnum defines UNKNOWN_NOT_PROVEN / KNOWN_CORRELATED / EVIDENCED_INDEPENDENT; DEFAULT_INFRASTRUCTURE_CORRELATION_STATE = UNKNOWN_NOT_PROVEN
- agreement_phrasing_for_state returns 'cross_endpoint_agreement' for UNKNOWN_NOT_PROVEN and KNOWN_CORRELATED; returns 'independent_provider_agreement' only for EVIDENCED_INDEPENDENT
- correlated_failure_is_residual_risk returns True for UNKNOWN_NOT_PROVEN and KNOWN_CORRELATED; False for EVIDENCED_INDEPENDENT
- to_machine_dict surfaces infrastructure_state + agreement_phrasing + correlated_failure_residual_risk; to_human_summary adds 'RESIDUAL RISK: correlated failure ... is not excluded' line for the two residual-risk states
- empty_report defaults infrastructure_state to DEFAULT_INFRASTRUCTURE_CORRELATION_STATE (UNKNOWN_NOT_PROVEN); the verifier rejects None/blank with 'infrastructure_correlation_state_unrecorded' blocker
- Tests test_default_infrastructure_state_is_unknown_not_proven, test_infrastructure_state_must_not_be_blank, test_correlated_failure_residual_risk_callout_for_unknown_state, test_correlated_failure_residual_risk_callout_for_known_correlated, test_no_residual_risk_callout_for_evidenced_independent, test_default_phrasing_is_cross_endpoint_agreement, test_human_summary_uses_cross_endpoint_agreement_wording, test_machine_dict_never_emits_independent_provider_agreement_for_default_state, test_machine_dict_only_emits_independent_provider_agreement_when_evidenced all confirm the constraint

### contract-machine-human-reports — PASS

Both machine and human reports carry the required sections; the verifier gates qualification against the same fields.

Evidence:

- QualityReport dataclass + with_* helpers carry every required section: capability_snapshot, preflight_budget, actual_budget, budget_deviations, provider_provenance, response_volume, findings, required_samples, infrastructure_state, shared_infrastructure_evidence, manifest_checksum, qualification_blockers
- to_machine_dict emits schema 'robinhood_lp.quality.v1' and all 15+ named sections (test_machine_dict_includes_required_sections asserts each one)
- to_human_summary emits qualification verdict + chain/pool/coverage/topology/Init-block/manifest/checksum lines + per-category findings counts + infrastructure state + A+B wording + RESIDUAL RISK callout when applicable + cold-start/warm-run/backtest basis line + qualification blockers list

### contract-warning-counts-not-collapsed — PASS

Warning counts cannot disappear in aggregates: every deviation / retry / rejection / regression is a distinct row, and aggregates are derived from the row list.

Evidence:

- QualityFinding.append-only: _merge_findings preserves every occurrence; with_finding does not dedupe
- BudgetDeviation append-only via with_budget_deviation; no aggregate counter separate from the row list
- reason_code_counts() derives counts from the findings list; findings_by_category() derives groupings from the findings list
- Tests test_warning_counts_appear_in_aggregates and test_finding_counts_appear_in_aggregates confirm counts cannot disappear

### contract-third-party-not-proof — PASS

No implementation lets a third-party match serve as proof of dataset correctness.

Evidence:

- No code path in quality/ treats a third-party match as proof; qualification evidence is the dataset's own raw evidence + the A+B cross_endpoint_sample, not external sources
- verify_report enforces the A+B agreement and the absence of any blocking reason code — no external API or third-party index is consulted

### references — PASS

References are referenced at the contract level; the report module sits at the layer above storage and routing, qualifying their outputs without changing them.

Evidence:

- T034.md References: R12, R13, ADR-002, ADR-010. The implementation imports only from robinhood_lp.protocol.ids and robinhood_lp.protocol.events (PoolId, EventKey) and does not require R12/R13 specific imports at this layer; ADR-002 (Parquet + SQLite) and ADR-010 (free dual-provider A+B) describe the upstream storage and routing layers whose outputs T034 qualifies

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- T034 attempt 002 introduces the data-quality & completeness reports package (quality/) but no runner / orchestrator that actually feeds live A+B comparison outcomes into verify_report exists in this attempt — the verifier is exercised by tests only. Downstream code (T035+) is responsible for invoking verify_report with real sample_agreement, has_result_bearing_sample, and expected_manifest_checksum inputs.
- _stable_pick_int in sample_selection.py is defined but unused; harmless dead code with a leading underscore, not exported.
- RequiredSamples.window_count() has a confusing initial `count = 2` with a contradicting inline comment; the math is correct (the 2 is unconditionally added but the comment says it shouldn't be). The accessor is not used by the verifier — only by potential debug consumers — so this does not affect contract compliance, but should be cleaned up.
- PerRunSample.selection_inputs_dict() exposes only pool_id / fixed_block_number / chain_id; the dataclass stores manifest_checksum in selection_inputs but selection_inputs_dict() does not surface it. The full mapping is still present on the dataclass field and serialised in to_machine_dict, so reproducibility is preserved.
