# T109 independent review

- Base commit: `0ef2ce08dbd7bbbf727b892ef2cfa16a41269ebf`
- Candidate commit: `d0d282f42ea10eab70cbfebc1f606e697ace0e9c`
- Verdict: **FAIL**

## Checks

### scope-t069-publication-extended — FAIL

The T069 BacktestOrchestrator publish path is the single entry point the contract names for the atomic T109 evidence publication. The candidate does not modify that path: a successful T069 run today still publishes a T105 ExperimentManifest built by build_experiment_manifest(input_events=events, ...) and a legacy report, with no SimulationEvidence written and no atomic-publication gate. The contract's cutover requirement is unmet; 'no predecessor-format run may publish without the required evidence' is not enforced.

Evidence:

- src/robinhood_lp/orchestrator/__init__.py unchanged in the candidate diff
- src/robinhood_lp/orchestrator/__init__.py:1812 still calls build_experiment_manifest(input_events=events, ...) and writes the legacy T105 manifest via write_manifest_to_path at line 1860 without ever constructing a SimulationEvidence or binding it atomically with the manifest
- git diff --stat 0ef2ce0..d0d282f shows zero changes to src/robinhood_lp/orchestrator/ and src/robinhood_lp/reports/manifest.py
- T109 deliverable clause: 'Extend the successful T069 publication transaction with an immutable, versioned and checksummed simulation-evidence artifact... New successful runs publish manifest, report and simulation evidence atomically, or publish none of them as successful.'
- T109 'Replacement and migration' clause: 'After cutover, every new product run reaches the T061 engine and the registry-bound publication through T109; no CLI, Web, background or test helper may publish a current successful predecessor-format run without the required evidence.'

### scope-t105-manifest-extended — FAIL

The T105 ExperimentManifest builder is the manifest authority the contract requires T109 to replace. The candidate does not modify build_experiment_manifest or write_manifest_to_path. New T069 runs continue to write a manifest that embeds the entire input event timeline, which directly contradicts the contract's 'no embedded complete market event timeline' rule for new writes.

Evidence:

- src/robinhood_lp/reports/manifest.py unchanged in the candidate diff
- src/robinhood_lp/reports/manifest.py:894-935 still serializes the full input_event_list onto every ExperimentManifest payload
- T109 contract: 'For new writes, replace the embedded complete input_event_list with the immutable T100 dataset content hash and partition references needed to resolve the same canonical input.'
- T109 'Replacement and migration': 'If T105's current serialization duplicates those events, migrate new writes to immutable dataset references while retaining a versioned legacy reader and source checksums for old artifacts.'

### scope-t061-cursor-binding — FAIL

The T061 audit chain is the only source of transitions the T109 SimulationEvidence can carry, but AuditEvent has no MarketCursor field. The candidate provides data classes (RunTransition with cursor) that the engine never produces. The contract's causal cutover — bind every transition to a MarketCursor at emission time and queue delayed fills under the actual cursor — is not implemented in T061 and is not asserted by any test against engine.run(). The developer evidence explicitly admits this: 'the T061 engine's per-event audit chain and the T069 orchestrator's evidence-publication gate are unchanged' and 'The artifact will reject a non-causal chain on the day the orchestrator binds it; the engine-side fix is a separate, smaller follow-up amendment'.

Evidence:

- src/robinhood_lp/backtest/events.py unchanged in the candidate diff
- src/robinhood_lp/backtest/events.py:510-520 AuditEvent dataclass carries only (timestamp, sequence, payload, ledger_hash_after); no MarketCursor field
- src/robinhood_lp/backtest/engine.py unchanged in the candidate diff
- T109 contract deliverable: 'Extend and correct the single existing T061 execution schedule used for new T109 runs. When a decision resolves to fill data at a later cursor, queue its latency/fill pipeline under that actual MarketCursor; do not mutate the ledger or append those transitions while still processing the trigger.'
- T109 contract: 'Bind every transition and checkpoint during the original run to the exact canonical MarketCursor; never derive that binding later from T061's integer timestamp.'

### acceptance-heterogeneous-fixtures-prefix-equivalence — FAIL

The acceptance requires two heterogeneous pool fixtures and a golden T040/T041 prefix equivalence check at four cursor kinds. The candidate's MarketStateReader tests cover only the empty-event pre-initial-state path; intra-block, end-of-block, range-end and same-dataset-identical-fixture scenarios are untested. The cursor-binding logic in _checkpoint_at has no observable evidence of correctness beyond the pre-initial branch.

Evidence:

- T109 Acceptance: 'For two heterogeneous pool fixtures, MarketState at beginning, intra-block, end-of-block and range-end cursors equals direct T040/T041 prefix reconstruction and is identical when two runs reference the same dataset (test, golden)'
- tests/test_t109.py:751-777 only exercises build_market_state_reader with events=() and reads a pre-initial-state cursor; no test drives the reader against an actual T040 replay event sequence
- tests/test_t109.py contains no second heterogeneous fixture and no golden comparison against direct _replay() output
- _checkpoint_at in src/robinhood_lp/replay/market_state.py:370-403 has no test that proves it selects the same checkpoint the T040 prefix reconstruction would return for an intra-block / end-of-block / range-end cursor

### acceptance-t104-equivalence-and-no-second-fee-impl — FAIL

The acceptance requires a T104 fee-growth/equivalence test and a no-second-fee-implementation inspection. The candidate has neither. The new modules' docstrings acknowledge T104 is untouched but provide no observable guarantee; a future amendment could re-implement T104 inside the reports package without violating any test.

Evidence:

- T109 Acceptance: 'A T104 fee-growth/range-fee query at those cursors retains the surface's dataset/window/reconstruction provenance and exact-equivalence behavior, while inspection proves neither T109 nor either Web consumer contains a replacement fee implementation (test).'
- tests/test_t109.py has no import of robinhood_lp.replay.fee_surface, no fee-growth composition call, and no T104 surface test
- The candidate adds no inspection or grep test proving the Web consumers do not embed a T104 replacement
- src/robinhood_lp/reports/run_state.py and src/robinhood_lp/replay/market_state.py both document T104 composition as out-of-scope but never exercise it

### acceptance-t061-run-state-equivalence — FAIL

The acceptance requires a captured-from-T061 golden state to compare against. The candidate only constructs synthetic transitions; no real BacktestEngine.run() output is captured, no T052 attribution snapshot, and no 'byte-equivalent repeated reads' golden exists. The acceptance's 'for each fixture ... equals the original T061 ... captured during that run' is unverified.

Evidence:

- T109 Acceptance: 'For each fixture, a successful run's projected state at entry, in-range fee accrual, out-of-range wait, rebalance and exit cursors exactly equals the original T061 position, integer inventory, ledger, equity and T052 attribution state captured during that run, and repeated reads are byte-equivalent (test, golden).'
- tests/test_t109.py:172-283 _sample_transitions() constructs a fully synthetic chain by hand; no test invokes BacktestEngine.run() and compares a captured audit-state snapshot to ReplayProjector.run_state()
- The developer evidence says: 'the T061 engine's per-event audit chain and the T069 orchestrator's evidence-publication gate are unchanged'
- The acceptance list of cursor kinds (entry, in-range fee accrual, out-of-range wait, rebalance, exit) has no test in the candidate

### acceptance-same-block-multi-transition-ordering — FAIL

The contract's same-block ordering rule — cursor first, ordinal second — is partially enforced in the SimulationEvidence validator but not exercised by any test, and the projection's claim to 'return the state after every transition at that cursor' is not asserted. The acceptance's cursor-by-cursor state-after-every-transition property is unverified.

Evidence:

- T109 Acceptance: 'Same-block market events with multiple action transitions prove ordering first by (block_number, transaction_index, log_index) and then by run_transition_ordinal, and a query at each cursor returns the state after every transition at that cursor (test).'
- tests/test_t109.py:379-389 tests MarketCursor ordering only; no test places multiple transitions at the same MarketCursor with strictly increasing ordinals and verifies the validation + projection order
- SimulationEvidence.__post_init__ in src/robinhood_lp/reports/simulation_evidence.py:486-514 allows same-cursor ordinal progression but no test exercises the boundary (e.g., 3 transitions at cursor (1,0,0) with ordinals 0/1/2, then a transition at (1,0,1) with ordinal 3)
- ReplayProjector.run_state in src/robinhood_lp/reports/run_state.py:481-548 has no test asserting that every transition at the requested cursor is included in the projection before returning

### acceptance-rejected-publication-variants — PASS

The publication-rejection variants the contract names — missing cursor on a state-changing transition, non-monotonic cursor, non-strict ordinal, tampered checksum — all fail closed with the named reason codes (T109_EVIDENCE_STATE_TRANSITION_NO_CURSOR / NON_MONOTONIC_CURSOR / NON_STRICT_ORDINAL / CHECKSUM_MISMATCH).

Evidence:

- tests/test_t109.py:404-418 test_state_changing_without_cursor_rejected
- tests/test_t109.py:420-444 test_non_monotonic_cursor_rejected
- tests/test_t109.py:446-468 test_non_strict_ordinal_rejected
- tests/test_t109.py:476-482 test_tampered_checksum_rejected
- src/robinhood_lp/reports/simulation_evidence.py:212-217 rejects state-changing transitions with cursor=None; :486-514 rejects cursor-precedes-prior and ordinal-non-strictly-increasing

### acceptance-delayed-fill-cursor-A-and-C — PASS

The decision-cursor projection excludes a delayed FILL bound to a later cursor, and the fill-cursor projection includes it; the end-of-block cursor includes every transition in the block. The synthetic fixture does not exercise an intervening reactive cursor B (see check below) but the two-cursor A/C behaviour is verified.

Evidence:

- tests/test_t109.py:520-550 test_delayed_fill_absent_at_decision_cursor
- tests/test_t109.py:552-577 test_end_of_block_includes_every_transition
- src/robinhood_lp/reports/run_state.py:481-548 walk transitions up to cursor and skip cursor-less SYSTEM transitions, applying each in order

### acceptance-delayed-execution-reactive-cursor-B — FAIL

The contract's three-cursor fixture (A decision, B reactive market event, C fill-data) and the assertion that RunState(run_id, B) equals the original B-captured state are not implemented or tested. The synthetic _sample_transitions chain has no reactive call site; without it, the T109 acceptance cannot prove 'no intervening callback observes a queued future fill'.

Evidence:

- T109 Acceptance: 'A delayed-execution fixture has trigger cursor A, an intervening reactive market cursor B and fill-data cursor C. The original strategy request, risk input, metric input and accounting state actually supplied at B are all pre-fill and equal RunState(run_id, B); neither latency nor fill is applied before C.'
- tests/test_t109.py contains no fixture with three distinct cursors (A, B, C) and no test asserting RunState(run_id, B) is pre-fill or that the original audit-event sequence captures the B-cursor reactive call without a fill
- tests/test_t109.py:172-283 _sample_transitions only models A and C (decision at A, fill at A or later); a reactive call between them is not modelled

### acceptance-strategy-replacement-byte-equivalence — FAIL

The acceptance requires a fixture that registers a strategy, runs an artifact through the projection, then removes or replaces the strategy and re-reads. The candidate's only test on this property is a structural absence-check; the actual change-or-remove invariant is not exercised.

Evidence:

- T109 Acceptance: 'Change or remove the installed strategy implementation after a run, then prove RunState and ReplayFrame remain byte-equivalent and no strategy callback is reached (test).'
- tests/test_t109.py:579-591 test_strategy_replacement_does_not_affect_run_state only calls projector.run_state(cursor) twice and compares the result; no strategy is installed, removed, or replaced
- No fixture in tests/test_t109.py registers a strategy, runs through an engine, removes the strategy, and re-reads

### acceptance-sparse-checkpoint-plus-replay — FAIL

The contract requires a sparse-checkpoint-equivalence test. The candidate places checkpoints at the test cursor and does not prove the projection reconstructs an identical state from a distant checkpoint plus intervening transitions.

Evidence:

- T109 Acceptance: 'Prove sparse checkpoint plus transition replay yields the same state as the original run without requiring a snapshot per cursor (test).'
- tests/test_t109.py:534-541 places a single checkpoint at the fill cursor only; no test asserts that a checkpoint at cursor A and transitions A->B->C reproduce the same RunState at C as a complete snapshot at C
- src/robinhood_lp/reports/run_state.py:451-465 _checkpoint_for() returns the latest checkpoint whose cursor is at or before the requested cursor, but no test demonstrates a non-co-located checkpoint plus intervening transitions reproduces the original state

### acceptance-binding-failures-closed-named — PASS

Dataset-version, PoolKey, cursor and checksum mismatches between the run evidence and the supplied MarketState fail closed with named ReplayFrameBindingError reasons.

Evidence:

- tests/test_t109.py:620-635 test_frame_mismatched_dataset_rejected
- tests/test_t109.py:637-652 test_frame_mismatched_pool_rejected
- tests/test_t109.py:654-670 test_frame_cursor_mismatch_rejected
- tests/test_t109.py:476-482 test_tampered_checksum_rejected
- src/robinhood_lp/reports/run_state.py:349-398 ReplayFrame.__post_init__ validates market_state vs run_state; :566-579 rejects dataset and pool disagreement

### acceptance-failed-cancelled-runs-no-replay-frame — FAIL

The acceptance requires the orchestrator to refuse publication when evidence is missing and to fail closed with a named reason. The candidate's unchanged orchestrator still publishes a T105 manifest on SUCCEEDED without any evidence gate, so a failed/cancelled/incomplete run does not currently bind a T109 artifact — and there is no test that the new T109 frame readers reject a partial evidence for those states.

Evidence:

- T109 Acceptance: 'failed, cancelled or atomically incomplete runs cannot produce a replay frame (tests).'
- tests/test_t109.py has no RUN_STATE_FAIL / RUN_STATE_CANCELLED / atomically-incomplete fixture; no test asserts that no SimulationEvidence is published in those cases or that a frame cannot be constructed
- The T069 orchestrator does not enforce 'no evidence, no SUCCEEDED' because it is unchanged

### acceptance-storage-shared-canonical-timeline — FAIL

The contract requires storage-inspection evidence that multiple runs share one canonical T100 timeline. The candidate's only structural check (hasattr on the reports package) does not inspect storage and does not demonstrate multi-run sharing.

Evidence:

- T109 Acceptance: 'storage inspection proves one canonical market-event timeline is shared by multiple runs (architecture and integration tests).'
- tests/test_t109.py:865-885 test_no_replay_implementation_under_reports is a structural hasattr check on the package; it does not inspect the data root or prove that two MarketStateReader instances share the same timeline bytes
- No architecture test in tests/ imports build_market_state_reader twice and asserts they resolve identical canonical event sequences from the same T100 data root

### acceptance-t101-t106-t102-consumer-regression — FAIL

The compatibility adapter for T101/T106/T102 is implemented as standalone functions that nothing in the codebase calls. The T101 panel, T106 robustness runner, and T102 evaluation still bind to the unchanged T105 ExperimentManifest, which still carries input_event_list and pre-T109 identity. The acceptance's regression proof (T101/T106/T102 accept new T109 artifacts, reject mismatches, and cannot reach the T069/T105 publishers) is unverified.

Evidence:

- T109 Acceptance: 'T101 and T106 consume new T109 manifests through the compatibility view and T102 completes its T101/T106 integration with the same logical identities and canonical event bytes; dataset-reference/hash/schema mismatches fail, and no regression path reaches the T069/T105 publishers (tests).'
- tests/test_t109.py:678-736 only calls the adapter builders directly; no test drives robinhood_lp.research.panel, robinhood_lp.research.evaluation or the T106 robustness runner through the new adapter
- src/robinhood_lp/research/__init__.py and src/robinhood_lp/orchestrator/__init__.py unchanged; no import of build_panel_manifest_binding, build_t106_robustness_binding or resolve_panel_event_stream exists outside the new module and its __init__
- The acceptance's 'no regression path reaches the T069/T105 publishers' cannot be proven because T101/T106/T102 still reach T069/T105 directly

### acceptance-pre-evidence-historical-unsupported — FAIL

The contract requires that pre-evidence historical runs are not silently regenerated or presented as replayable. The candidate defines no such refusal branch and writes no such test. A legacy T105 manifest is not currently recognised as 'pre-evidence' by any T109 reader.

Evidence:

- T109 contract: 'historical runs created before this evidence schema remain byte-identical and readable as final reports but report exact historical RunState as unavailable; they are not silently regenerated, upgraded or presented as replayable.'
- No test in tests/test_t109.py loads a pre-T109 T105 manifest, passes it to ReplayProjector, and asserts an explicit unavailable result is returned
- src/robinhood_lp/reports/legacy.py is unchanged; no legacy-to-T109 path or refusal branch exists

### acceptance-predecessor-success-path-unreachable — FAIL

The candidate does not demonstrate that the predecessor success path is unreachable. The T069 orchestrator still publishes a T105 manifest on SUCCEEDED with no evidence gate; no test asserts that submission without evidence produces a failure. The candidate's residual risk #1 admits this gap.

Evidence:

- T109 contract: 'no CLI, Web, background or test helper may publish a current successful predecessor-format run without the required evidence'
- src/robinhood_lp/orchestrator/__init__.py:1812-1860 still publishes a T105 manifest via write_manifest_to_path unconditionally on SUCCEEDED; no gate refuses publication when no SimulationEvidence is bound
- tests/test_t109.py:810-819 test_artifact_does_not_embed_market_event_list only checks the artifact dataclass; it does not exercise the orchestrator's success path
- No test in tests/test_t109.py imports BacktestOrchestrator and asserts that a SUCCEEDED run without SimulationEvidence raises or returns FAILED

### lint-typing-format — PASS

Ruff check, ruff format --check, strict mypy on every new module and the test module all pass. The 38 new tests pass and the previously passing T040/T041/T052/T061/T069/T100/T101/T102/T104/T105/T106 suites remain passing on the candidate. The two remaining repository-wide failures (test_abi_artifacts forge / test_documentation_citations T069) reproduce on the base commit (0ef2ce0) and are unrelated to T109 — the forge failure is the worktree missing the forge-std git submodule, the doc-citation failure is a pre-existing §2.2 T069 reference drift.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff check on the five new files: 'All checks passed!'
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff format --check on the five new files: '5 files already formatted'
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy on the five new files with --ignore-missing-imports: 'Success: no issues found in 5 source files'
- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/test_t109.py: 38 passed in 0.14s
- Full targeted-suite run (T040, T041, T052, T061, T069, T100, T101, T102, T104, T105, T106 + T109): 716 passed in 3.36s

### must-not-no-second-replay-impl — PASS

The candidate does not create a second replay-of-raw-events engine. The MarketState reader composes T040; the RunState projector applies recorded transitions only. Strategy callbacks are not invoked; the no-per-run-market-copy invariant is asserted. The new surface is a sparse read/projection layer over the existing T040/T041/T061 authorities.

Evidence:

- src/robinhood_lp/replay/market_state.py delegates reconstruction to the existing T040 replay module (_replay_checkpoints calls _replay from robinhood_lp.replay.replayer)
- src/robinhood_lp/reports/run_state.py applies run-specific RunTransition payloads to the recorded initial PositionState; it does not invoke the T061 engine, the strategy callback, or any storage / RPC / signer surface
- tests/test_t109.py:745-749 test_projector_has_no_strategy_callback asserts the projector carries no strategy attribute
- tests/test_t109.py:751-777 test_reader_stores_no_per_run_market_copy asserts the reader does not persist events separately

### must-not-duplicate-market-timeline — PASS

The simulation evidence artifact never embeds the canonical market event timeline; the dataset content hash is the binding. This holds in the new modules but is undermined by the unchanged T105 builder which still serializes input_event_list (see check 'scope-t105-manifest-extended'); that is a scope-cutover gap, not a duplicate-timeline artifact from T109 itself.

Evidence:

- SimulationEvidence dataclass (src/robinhood_lp/reports/simulation_evidence.py:319-393) carries dataset_version, dataset_content_hash, dataset_schema_version, dataset_decode_version, reconstruction_revision and run_transitions; no input_event_list or events field
- tests/test_t109.py:810-819 test_artifact_does_not_embed_market_event_list explicitly asserts that 'input_event_list', 'events', 'market_events' are absent from evidence.to_dict()
- MarketState dataclass (src/robinhood_lp/replay/market_state.py:215-294) carries a single PoolCheckpoint selected from the T040 sequence; no per-cursor event log

## Must-not violations

- None.

## Unknowns

- T069 acceptance of 'audit cursor order is non-decreasing without post-sorting' in the original engine is asserted only on the synthetic _sample_transitions fixture; an end-to-end run through BacktestEngine.run() that records per-stage cursors into AuditEvent and reproduces the property is not executed.
- T088 ('ReplayFrame consumer') and T096 ('T109 evidence into the final matrix') are downstream consumers the contract lists; no T088 / T096 test in this candidate exercises the new ReplayFrame / SimulationEvidence surfaces.
- T107 / T108 contract amendments are downstream; the candidate's compatibility adapter does not bind those consumers either, and there is no test that the adapter preserves a T107 / T108 logical field.

## Required changes

- Extend src/robinhood_lp/backtest/events.py AuditEvent (and the T061 engine code path that emits it) to carry a MarketCursor(block_number, transaction_index, log_index) per event, never derived from timestamp at read time; queue delayed-fill latency / fill under the actual MarketCursor instead of mutating the ledger while still processing the trigger. Until then the SimulationEvidence cannot be populated from a real engine and the T069 publication gate has nothing to bind.
- Modify the T069 orchestrator's SUCCEEDED path in src/robinhood_lp/orchestrator/__init__.py to construct a SimulationEvidence from the recorded audit chain and publish it atomically with the manifest / report; raise BacktestRunError with a named reason when evidence cannot be produced or validated, so no current successful run can publish a predecessor-format result without the evidence.
- Modify build_experiment_manifest in src/robinhood_lp/reports/manifest.py to drop the embedded input_event_list for new writes and bind the T100 dataset_version + dataset_content_hash + partition references instead; retain the legacy reader for pre-T109 artifacts and fail closed on a hash / partition mismatch. The current builder still embeds the full event list which contradicts the contract's 'no duplicate market event timeline' rule.
- Replace the synthetic _sample_transitions fixture with an integration test that runs a real BacktestEngine.run() over a T100-qualified dataset, captures the recorded ledger / equity / T052 attribution at entry / in-range-fee-accrual / out-of-range-wait / rebalance / exit cursors, and asserts ReplayProjector.run_state at the same cursors returns byte-equivalent state. The contract's 'golden' acceptance requires this end-to-end proof.
- Add the contract's three-cursor delayed-execution fixture (decision A, intervening reactive B, fill C) and assert (a) RunState(run_id, B) is pre-fill, (b) the audit-event sequence captures the B-cursor reactive call without a fill, (c) audit cursor order is non-decreasing without post-sorting, and (d) all existing T061 information-frontier, decision → risk → latency → fill, partial-fill, and ledger-hash guarantees still pass on the real engine output.
- Wire the compatibility adapter (build_panel_manifest_binding, build_t106_robustness_binding, resolve_panel_event_stream) into the actual T101 panel, T106 robustness runner, and T102 evaluation code paths, then add regression tests proving (a) all three accept new T109 artifacts, (b) dataset-reference / hash / schema mismatches fail closed, (c) no path reaches the existing T069 / T105 publishers. The adapter as standalone functions does not satisfy 'T109 owns regression/integration proof that all three accept new T109 artifacts'.
- Add a sparse-checkpoint equivalence test: place a single RunStateCheckpoint at cursor A, populate transitions at A→B→C, and assert ReplayProjector.run_state(C) equals the same projection with a per-cursor snapshot table — proving 'no full snapshot per cursor is required'.
- Add a strategy-removal / strategy-replacement test that registers a strategy, captures the evidence, removes the strategy module, then re-runs ReplayProjector.run_state and ReplayProjector.frame and asserts byte-equivalent output. The current structural absence-check does not exercise the contract's invariant.
- Add a pre-T109 historical manifest test that loads a legacy T105 ExperimentManifest, passes it to a T109 reader, and asserts an explicit unsupported / unavailable result is returned — the contract's 'historical runs remain byte-identical but report RunState as unavailable; not silently regenerated, not presented as replayable' is not currently enforced.
- Add a storage-inspection test that builds two MarketStateReader instances over the same T100 data root and asserts the canonical event sequences are byte-equivalent — the contract's 'one canonical market-event timeline is shared by multiple runs' requires storage-level evidence, not a structural hasattr check.
- Add the T104 fee-growth / range-fee composition test (T104 provenance retained, exact-equivalence behavior preserved) and the 'no replacement fee implementation in T109 or either Web consumer' inspection test the contract lists under Acceptance; these are absent from the candidate.
- Add the predecessor-success-path-unreachable test: drive BacktestOrchestrator (or a focused wrapper around it) into SUCCEEDED and assert the path raises when SimulationEvidence cannot be bound; without this, the cutover the contract names is not enforced.

## Residual risks

- Pre-existing repository-wide failures test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output and test_documentation_citations.py::test_check_passes_on_real_repository reproduce on the base commit 0ef2ce0 and are unrelated to T109; the forge failure is a worktree-level missing git submodule, the doc-citation failure is a pre-existing §2.2 T069 reference drift that the candidate does not touch.
- The new MarketStateReader, RunState / ReplayFrame / ReplayProjector, and SimulationEvidence modules are well-formed and pass ruff / mypy / pytest at the unit level; the gap is the integration into T061, T069, T105 and the T101/T106/T102 consumers, which the candidate's residual-risk list and the contract both flag.
