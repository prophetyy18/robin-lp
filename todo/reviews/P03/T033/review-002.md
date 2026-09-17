# T033 independent review

- Base commit: `1932ac7ad0330763f7a753579fe729c90dd23fcb`
- Candidate commit: `b9b0f58a82e564733c056cbaeb80f99e381fbd29`
- Verdict: **PASS**

## Checks

### scope-diff — PASS

Diff is bounded to the new reorg subpackage, four dedicated test modules, one shared fixture, and the merged T033 contract/docs. No collateral edits to ingestion, RPC, oracle, signer, or workflow controller code.

Evidence:

- git diff --stat shows 4234 insertions across src/robinhood_lp/storage/reorg/{__init__,policy,ancestor,journal,handler}.py and 4 new tests modules under tests/ plus tests/_reorg_t033_fixtures.py.
- No edits outside src/, tests/, and todo/{config.yaml,phases/P03-ingestion-and-storage/T033.md,evidence/P03/T033/*,reviews/P03/T033/*,triage/P03/T033/*}.

### tests — PASS

All 80 new T033 tests pass; the full non-workflow suite (621 tests) remains green. Existing storage_conflict / storage_manifest / storage_writer suites also pass (49/49).

Evidence:

- PYTHONPATH=src pytest tests/test_reorg_ancestor.py tests/test_reorg_journal.py tests/test_reorg_policy.py tests/test_reorg_handler.py -> 80 passed in 0.78s.
- PYTHONPATH=src pytest tests/ -q --ignore=tests/test_workflow.py --ignore=tests/test_abi_artifacts.py --ignore=tests/test_workflow_contracts.py -> 621 passed, 6 skipped in 3.37s. Skips are pre-existing (Foundry/gpg/vector reorder), unrelated to T033.

### ruff-format — PASS

Formatting matches project style across all touched and pre-existing files.

Evidence:

- ruff format --check src/ tests/ -> '83 files already formatted'.

### ruff-check — PASS

No lint findings. F841 / unused-symbol noqa on DECISION_HALT_SHALLOW_REORG, DECISION_HALT_PROVIDER_DISAGREEMENT, DECISION_RECONCILE_ORPHAN_REAPPEARANCE, DECISION_RECONCILE_REMOVED_LOG is intentional (exported via __all__).

Evidence:

- ruff check src/ tests/ -> 'All checks passed!'.

### mypy — PASS

Strict mypy is clean across src/ and tests/, including the new reorg package and fixtures.

Evidence:

- mypy src/ tests/ -> 'Success: no issues found in 83 source files'.

### git-diff-check — PASS

No whitespace / line-ending problems in the candidate diff.

Evidence:

- git diff --check <base> <candidate> returned no output.

### contract-clauses — PASS

All merged-contract acceptance clauses and deliverables are implemented and pinned by tests. Reason-code vocabulary, fail-closed semantics, deep-reorg halt, critical-incident candidate, append-only journal, and partition-evidence preservation all match the contract wording.

Evidence:

- FinalityPolicy.finality_tag default 'finalized' is enforced in __post_init__ (policy.py:227-232); any other value raises FinalityPolicyError. test_finality_policy_finality_tag_must_be_finalized locks the boundary.
- UnfinalizedWindow.lower_bound = finalized + 1 and upper_bound = latest (policy.py UnfinalizedWindow) with __post_init__ validating bounds. test_unfinalized_window_lower_bound_is_finalized_plus_one and test_unfinalized_window_clamps_to_policy_lag confirm.
- Reason code vocabulary covers finalized_unavailable / finalized_ancestry_disagreement / finality_tag_unavailable / FINALIZED_ANCESTRY_VIOLATION plus the orphan taxonomy (REASON_SHALLOW_REORG, REASON_DEEP_REORG, REASON_PROVIDER_DISAGREEMENT, REASON_REMOVED_LOG, REASON_ORPHAN_REAPPEARANCE). test_reason_code_vocabulary_is_complete asserts the set equals VALID_REORG_REASON_CODES.
- find_common_ancestor is depth-bounded and returns COMMON_ANCESTOR_UNREACHABLE on no shared history / depth overflow; handler.observe_new_tip halts on that sentinel via _halt_deep_reorg. test_find_common_ancestor_unreachable_when_no_shared_history, test_find_common_ancestor_returns_unreachable_when_depth_exceeded, test_deep_reorg_halts_qualification_and_blocks_backtest, test_deep_reorg_halts_when_orphan_depth_meets_threshold cover this.
- Reorg journal and critical_incidents tables are append-only: record_orphan uses INSERT (journal.py ReorgJournal.record_orphan) and record_critical_incident_candidate uses INSERT (journal.py ReorgJournal.record_critical_incident_candidate). No UPDATE / DELETE in reorg/*.py. test_critical_incident_recorder_does_not_overwrite and test_orphan_reappearance_does_not_overwrite_prior_entry lock append-only semantics.
- CanonicalChainView.add_header raises ValueError on in-place overwrite (ancestor.py). test_canonical_view_rejects_in_place_overwrite locks the rule. _absorb_observed skips conflicting rows, preserving the prior canonical header. test_partition_row_is_preserved_across_reorgs and test_finalized_ancestry_violation_does_not_rewrite_partition_evidence verify finalized raw evidence survives deep reorgs and critical incidents.
- Provider disagreement / finalized unavailable / tag unavailable all halt qualification: evaluate_finality_boundary returns disagreement / unavailable / tag_unavailable with the right reason_code; apply_finality_decision returns the matching halt decision. test_provider_disagreement_halts_qualification, test_finalized_unavailable_halts_qualification, test_finality_tag_unavailable_halts_qualification confirm.
- FINALIZED_ANCESTRY_VIOLATION critical incident is detected, halted, journaled, and escalated: detect_finalized_ancestry_violation writes a critical_incidents row, returns kind='halt_critical_incident' with DECISION_BLOCK_BACKTEST / DECISION_BLOCK_PAPER / DECISION_BLOCK_LIVE regardless of deep_reorg_halt_qualifies_backtest. test_finalized_ancestry_violation_records_critical_incident and test_finalized_ancestry_violation_blocks_backtest_paper_and_live confirm. Phase 8 operator / incident handling is explicitly out of scope (handler.py / journal.py docstrings).

### must-not — PASS

Every must-not clause is satisfied and locked by tests.

Evidence:

- No production code path consumes a confirmations count: grep for 'confirmations' in reorg/*.py returns only docstring references and the EXPERIMENTAL_CONFIRMATIONS_PLACEHOLDER constant. test_experimental_confirmations_placeholder_is_constant and test_experimental_confirmations_placeholder_is_explicit verify policy has no confirmations field.
- Reorg journal only INSERTs; no DELETE / UPDATE statements in src/robinhood_lp/storage/reorg/*.py. Orphan rows persist; demotion reason is recorded.
- Unresolved ancestry halts: observe_new_tip returns _halt_deep_reorg when find_common_ancestor returns COMMON_ANCESTOR_UNREACHABLE. test_deep_reorg_halts_qualification_and_blocks_backtest covers this.
- Critical incident blocks new qualification / paper / live: _block_gates returns all three gates for halt_critical_incident irrespective of deep_reorg_halt_qualifies_backtest. test_finalized_ancestry_violation_blocks_backtest_paper_and_live covers this.
- No Phase 8 handling: handler detects, halts, journals, escalates; there is no operator declaration / recovery / superseding-canonical-creation code path. All references to operator / Phase 8 are in docstrings.

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- REORG_MANIFEST_SCHEMA_VERSION = 3 is exported from journal.py but the constant is not actually written to schema_meta (MANIFEST_SCHEMA_VERSION stays at 2). The critical_incidents table is still created lazily via CREATE TABLE IF NOT EXISTS, so behaviour is backward compatible; the constant is effectively documentation for a future migration path. The contract does not require the schema_meta row to be bumped, so this is informational only.
