# T109 independent review

- Base commit: `0ef2ce08dbd7bbbf727b892ef2cfa16a41269ebf`
- Candidate commit: `fac669a0fb1502a86547dcc802f03199e6b42839`
- Verdict: **FAIL**

## Checks

### scope-t069-orchestrator-t109-cutover — PASS

The T069 orchestrator SUCCEEDED branch now publishes the T109 dataset-referenced manifest, the simulation-evidence artifact, and the report as one publication unit. Failed / cancelled / atomically-incomplete runs publish neither a complete-looking result nor any evidence. The legacy T105 'build_experiment_manifest' / 'write_manifest_to_path' surface remains exported and used by T105 tests, but no production code path invokes it for new writes — the T069/T105 orchestrator cutover is correctly wired.

Evidence:

- src/robinhood_lp/orchestrator/__init__.py:1826-1835 invokes _build_simulation_evidence before any manifest / report write
- src/robinhood_lp/orchestrator/__init__.py:1880 writes the SimulationEvidence before the manifest; 1887 writes the T109 manifest; 1902 writes the report; 1906 transitions to SUCCEEDED only after all three succeed
- src/robinhood_lp/orchestrator/__init__.py:1507-1520 routes product-rerun sources through load_manifest_from_path (T105) or load_t109_manifest_from_path (T109) — both go through validate_manifest; the orchestrator no longer writes a legacy T105 manifest via build_experiment_manifest
- src/robinhood_lp/orchestrator/__init__.py:1915-1936 catches RunCancelled, BacktestRunError, and any Exception and writes the terminal record as CANCELLED / FAILED without manifest / report / evidence paths; the SUCCEEDED transition is only reached after all three writes complete
- tests/test_t109_acceptance.py::TestFailedCancelledRunsDoNotPublishEvidence::test_cancelled_run_publishes_no_evidence and test_failed_run_publishes_no_evidence assert no simulation_evidence_path on cancellation / failure and no file is written
- tests/test_backtest_t069.py::TestSuccessfulRunPublication::test_single_pool_run_publishes_manifest_and_report updated to assert manifest_payload['version'] == MANIFEST_VERSION_T109, dataset_partition_refs is non-empty, and 'input_event_list' is absent

### scope-t105-manifest-replaced-for-new-writes — PASS

For new writes, the manifest schema is T109ExperimentManifest and references the dataset_partition_refs / dataset_content_hash instead of embedding the complete input_event_list. The legacy T105 ExperimentManifest, build_experiment_manifest, and write_manifest_to_path remain in the codebase as a read-only legacy surface (T105 tests still construct them for rerun / legacy migration), but no current production entry point invokes them. The T109 loader refuses any payload that still embeds input_event_list.

Evidence:

- src/robinhood_lp/reports/manifest.py:124-125 introduces MANIFEST_VERSION_T109 = 't109.experiment_manifest.v1' alongside the unchanged MANIFEST_VERSION = 't105.experiment_manifest.v1'
- src/robinhood_lp/reports/manifest.py:1019-1087 T109ExperimentManifest dataclass replaces embedded input_event_list with dataset_partition_refs + dataset_event_count + simulation_evidence_ref + reconstruction_revision fields; __post_init__ rejects an empty dataset_partition_refs tuple
- src/robinhood_lp/reports/validation.py:671-712 load_t109_manifest_from_path rejects any payload whose version != MANIFEST_VERSION_T109 and any payload that still carries input_event_list
- src/robinhood_lp/reports/validation.py:280-305 validate_manifest routes to _validate_t109_manifest when manifest is T109ExperimentManifest; _validate_t109_manifest calls assert_binding_matches_registry against the same registry gate the T105 path uses
- grep -rn 'build_experiment_manifest\|write_manifest_to_path' src/ shows zero invocations from any production code path; the only references are the function definitions and the reports package __init__ export

### scope-t061-audit-cursor-binding-emission — FAIL

AuditEvent now carries the cursor field and the engine emits it; the API surface is in place. BUT the underlying BacktestEvent in src/robinhood_lp/protocol/contracts.py has NO block_number / transaction_index / log_index attributes, so extract_event_cursor always returns None for real BacktestEvent instances. The orchestrator's _build_simulation_evidence at line 2130-2135 then falls back to (timestamp, 0, 0) — the contract's explicitly-forbidden 'derive cursor from integer timestamp' behavior. The audit chain therefore does NOT carry the canonical (block, tx, log) triple the contract binds; it carries the integer timestamp dressed up as a cursor. This means every audit cursor from a real engine run is the (timestamp, 0, 0) fallback, not a real MarketCursor.

Evidence:

- src/robinhood_lp/backtest/events.py:520 adds cursor: tuple[int, int, int] | None = None to AuditEvent; __post_init__ at 564-579 validates the tuple form and the -1 end-of-block sentinel
- src/robinhood_lp/backtest/events.py:692-732 extract_event_cursor extracts a cursor from a BacktestEvent by reading getattr(event, 'block_number', None), getattr(event, 'transaction_index', None), getattr(event, 'log_index', None)
- src/robinhood_lp/backtest/engine.py:336-353 _fill_cursor helper selects the fill_data event's cursor for delayed fills and the trigger event's cursor for immediate fills; returns None when the cursor cannot be resolved
- src/robinhood_lp/backtest/engine.py:460, 480, 502, 543, 579, 609, 654, 754 emit AuditEvent with cursor= (None for SYSTEM init; extract_event_cursor(event) for decision / risk / seen / violation; _fill_cursor(fill_data, event) for latency / fill)
- src/robinhood_lp/protocol/contracts.py:264-302 BacktestEvent dataclass has only (version, timestamp, sequence, source_priority, kind, pool_key_id, chain_id, observed_at, available_at, payload, event_id) — NO block_number / transaction_index / log_index attributes
- src/robinhood_lp/orchestrator/__init__.py:2130-2135 event_cursor_map falls back to (int(evt.timestamp), 0, 0) whenever extract_event_cursor returns None — which is always for the current BacktestEvent schema

### scope-t061-delayed-fill-causal-schedule — FAIL

The engine's execution schedule was NOT actually restructured. The engine still synchronously processes each event: at the trigger event it calls _find_fill_data to retrieve the future fill_data event, mutates the ledger, and appends the LATENCY + FILL audit events. The cursor in those audit events is the fill_data cursor (good), but the ledger mutation and audit-event emission happen at trigger-event-iteration time, not at fill-data-iteration time. The contract's 'no intervening callback observes a queued future fill' invariant is satisfied ONLY because the projection layer skips transitions whose cursor is after the requested cursor — not because the engine itself delayed the mutation. The engine-side schedule is unchanged; the read-side cursor binding encodes the correct causality.

Evidence:

- src/robinhood_lp/backtest/engine.py:583-756 the engine processes each event in a single linear pass over sorted_events; when an event triggers a fill whose fill_data is at a later event, the engine calls _find_fill_data immediately, mutates the ledger (line 720-727), appends the LATENCY + FILL audit events with the fill_data cursor at line 654 + 754, and continues to the next event in the sorted list
- src/robinhood_lp/backtest/engine.py:808-852 _find_fill_data returns a future BacktestEvent from the sorted_events list synchronously; the engine does NOT queue the latency/fill pipeline to be released when the main loop reaches the fill_data event
- src/robinhood_lp/backtest/engine.py:697-702 raise FutureDataViolation when fill_data is None — the engine refuses to fill using the trigger price; this is the only corrected T061 rule
- T109 deliverable: 'When a decision resolves to fill data at a later cursor, queue its latency/fill pipeline under that actual MarketCursor; do not mutate the ledger or append those transitions while still processing the trigger. When the main market loop reaches the fill cursor, first admit that market event to the information frontier, then release due latency/fill pipelines in deterministic trigger-cursor and pipeline-identity order, and only then supply the resulting state to a new reactive callback/risk pipeline at that cursor.'
- T109 post-split deliverable: 'Correct the single existing T061 schedule for delayed execution. A decision whose fill data comes from a later cursor queues its latency/fill pipeline until that market event is admitted to the information frontier.'
- No test in tests/test_t109.py or tests/test_t109_acceptance.py invokes BacktestEngine.run() with a delayed-fill scenario and inspects the resulting audit chain to verify the latency/fill pipeline was queued at the trigger cursor and released at the fill cursor

### acceptance-market-cursor-canonical — PASS

MarketCursor is the canonical (block_number, transaction_index, log_index) cursor the contract binds; end-of-block semantics are encoded as the (-1, -1) sentinel after every real cursor in the same block. The cursor key agrees with lexicographic order on (block, tx, log).

Evidence:

- src/robinhood_lp/replay/market_state.py:128-225 MarketCursor frozen dataclass with (block_number, transaction_index, log_index); transaction_index == -1 && log_index == -1 marks end-of-block
- src/robinhood_lp/replay/market_state.py:189-208 cursor_key encodes -1 as 1<<30 so end-of-block sits after every real cursor in the same block
- tests/test_t109.py::TestMarketCursor covers construction, end_of_block factory, invalid forms, and ordering (a < b < c < end_of_block < e across same-block and next-block transitions)

### acceptance-market-state-reader-cursors — FAIL

MarketStateReader composes T040/T041 reconstruction and excludes fee-growth fields; it stores no per-run market-event copy; two readers over the same dataset return byte-equivalent states at the same cursor. The pre-initial-state branch is well-tested, but no test in this attempt drives an actual T040 replay event sequence (a non-empty events tuple with typed pool events) and verifies intra-block, end-of-block, and range-end cursors against direct _replay() output. The heterogeneous-fixture acceptance clause is reduced to same-dataset two-reader equivalence at empty-events cursors; full V4-protocol heterogeneous fixtures are out of scope per the developer evidence.

Evidence:

- src/robinhood_lp/replay/market_state.py:444-538 MarketStateReader.read validates the cursor's block_number is within the input window, replays the deterministic T040 checkpoint sequence (_replay_checkpoints), selects the cursor's checkpoint via _checkpoint_at, and returns a MarketState with is_initialized / pre_initial_state flags
- src/robinhood_lp/replay/market_state.py:374-432 _checkpoint_at handles beginning-of-window (pre-initial), intra-block, end-of-block, and beyond-cursor cases; for end-of-block it returns the last checkpoint whose block_number == cursor.block_number
- tests/test_t109.py::TestInvariants::test_reader_stores_no_per_run_market_copy covers empty-events pre-initial-state read; test_reader_rejects_block_outside_window covers CursorOutOfRangeError on cursor outside [from_block, to_block]
- tests/test_t109_acceptance.py::TestSameDatasetTwoReaders::test_same_dataset_two_readers_yield_byte_identical_states covers two-reader equivalence at the pre-initial cursor with empty events
- tests/test_t109_acceptance.py::TestStorageSharedCanonicalTimeline::test_two_readers_over_same_dataset_share_canonical_timeline covers two-reader equivalence and shared dataset_version binding

### acceptance-simulation-evidence-validation — PASS

SimulationEvidence validation is correct and fail-closed: state-changing transitions with no cursor, non-monotonic cursor sequences, non-strict ordinal sequences, and tampered checksums all raise SimulationEvidenceOrderingError or SimulationEvidenceChecksumError with named reason codes T109_EVIDENCE_STATE_TRANSITION_NO_CURSOR / NON_MONOTONIC_CURSOR / NON_STRICT_ORDINAL / CHECKSUM_MISMATCH.

Evidence:

- src/robinhood_lp/reports/simulation_evidence.py:130-160 RunTransition rejects state_changing=True with cursor=None with SimulationEvidenceOrderingError('T109_EVIDENCE_STATE_TRANSITION_NO_CURSOR')
- src/robinhood_lp/reports/simulation_evidence.py:485-514 SimulationEvidence.__post_init__ validates cursor non-decreasing (NON_MONOTONIC_CURSOR) and ordinal strictly increasing (NON_STRICT_ORDINAL) across transitions
- src/robinhood_lp/reports/simulation_evidence.py:683-708 simulation_evidence_from_dict recomputes the checksum and raises SimulationEvidenceChecksumError('T109_EVIDENCE_CHECKSUM_MISMATCH') on mismatch
- src/robinhood_lp/reports/simulation_evidence.py:580-636 compute_evidence_checksum hashes every field except evidence_checksum; the canonical serialisation is sort_keys + separators + ensure_ascii=False
- tests/test_t109.py::TestSimulationEvidenceValidation::test_state_changing_without_cursor_rejected, test_non_monotonic_cursor_rejected, test_non_strict_ordinal_rejected, test_evidence_checksum_validates, test_tampered_checksum_rejected, test_simulation_evidence_roundtrip exercise every closed-failure branch

### acceptance-run-state-replay-frame-projection — PASS

RunState / ReplayFrame projection is correct: the projector replays transitions in cursor / ordinal order, applies state-changing FILL transitions to the ledger, validates dataset / pool / cursor / range / revision bindings between the run's evidence and the supplied MarketState, and returns a frame with a frame_checksum. Repeated reads at the same cursor are byte-equivalent. The strategy-replacement test is structural (no strategy callback exists on the projector); it does not exercise a real remove-and-rerun scenario.

Evidence:

- src/robinhood_lp/reports/run_state.py:176-228 _apply_transition applies FILL state-changing transitions by replacing the PositionState from transition.payload['filled_position'] and verifying the recomputed ledger_hash equals transition.ledger_hash_after; SYSTEM transitions do the same when payload carries filled_position
- src/robinhood_lp/reports/run_state.py:404-480 _checkpoint_for selects the latest RunStateCheckpoint whose cursor is at or before the requested cursor
- src/robinhood_lp/reports/run_state.py:482-548 ReplayProjector.run_state replays transitions whose cursor is at-or-before the requested cursor in ordinal order, skipping cursor-less SYSTEM transitions
- src/robinhood_lp/reports/run_state.py:550-622 ReplayProjector.frame validates dataset_version, pool_key_id agreement between MarketState and SimulationEvidence; cursor agreement between market_state.cursor and run_state.cursor; rejects mismatches with ReplayFrameBindingError
- tests/test_t109.py::TestRunStateProjection covers pre-first-event returns initial, run_state no-strategy-callback (byte-equivalent repeated reads), delayed fill absent at decision cursor and present at fill cursor, end-of-block includes every transition, strategy replacement byte equivalence
- tests/test_t109.py::TestReplayFrame covers matching run_id, mismatched dataset / pool / cursor rejection
- tests/test_t109_acceptance.py::TestSparseCheckpointEquivalence covers distant checkpoint plus intervening transitions reproduces state at the end cursor

### acceptance-delayed-fill-cursor-binding — PASS

The synthetic delayed-fill fixture (decision at A, fill at C > A) proves that the projection at A is pre-fill (liquidity == 0, last_applied_ordinal == 3) and at C is post-fill (liquidity == 100, last_applied_ordinal == 4). The end-of-block cursor includes every transition in that block.

Evidence:

- tests/test_t109.py::TestRunStateProjection::test_delayed_fill_absent_at_decision_cursor builds a chain with decision_cursor=(2,0,0), fill_cursor=(5,0,0), state_changing fill, and a checkpoint at fill_cursor; asserts projector.run_state(decision_cursor).position.liquidity == 0 and projector.run_state(fill_cursor).position.liquidity == 100
- tests/test_t109.py::TestRunStateProjection::test_end_of_block_includes_every_transition covers end-of-block cursor includes the fill when fill cursor equals trigger cursor

### acceptance-rejected-publication-variants — PASS

Every publication-rejection variant the contract names fails closed with a named reason.

Evidence:

- tests/test_t109.py::TestSimulationEvidenceValidation covers state-changing-without-cursor, non-monotonic-cursor, non-strict-ordinal, tampered-checksum, and roundtrip cases with the named reason codes
- src/robinhood_lp/reports/simulation_evidence.py:212-217 rejects state-changing transitions with cursor=None at RunTransition construction; 486-514 rejects cursor-precedes-prior and ordinal-non-strictly-increasing at SimulationEvidence construction
- tests/test_t109.py::TestCompatibilityInvariants::test_artifact_does_not_embed_market_event_list asserts the SimulationEvidence artifact's JSON mapping has no input_event_list / events / market_events keys

### acceptance-same-block-multi-transition-ordering — PASS

Same-cursor multi-transition ordering is enforced by SimulationEvidence validation and exercised by the acceptance test; the projection applies them in ordinal order.

Evidence:

- tests/test_t109_acceptance.py::TestSameBlockMultiTransitionOrdering::test_three_transitions_same_cursor_apply_in_ordinal_order builds three FILL transitions at cursor=(1,0,0) with ordinals 0,1,2 and asserts state.last_applied_ordinal == 2 after projection
- tests/test_t109_acceptance.py::TestSameBlockMultiTransitionOrdering::test_validation_rejects_non_strict_ordinals_at_same_cursor builds two transitions at the same cursor with ordinal=0 twice and asserts SimulationEvidenceOrderingError
- src/robinhood_lp/reports/simulation_evidence.py:486-514 validates cursor non-decreasing and ordinal strictly increasing across transitions

### acceptance-rejected-historical-legacy — PASS

The T109 loader refuses any legacy T105 payload that still embeds input_event_list; the legacy T105 / T063 reader remains in place as a versioned read-only surface. Pre-evidence historical artifacts are not silently regenerated, upgraded, or presented as replayable — they can be loaded only through the legacy read-only path.

Evidence:

- tests/test_t109_acceptance.py::TestPreEvidenceHistoricalManifests::test_legacy_t105_manifest_rejected_by_t109_loader constructs a T105 ExperimentManifest payload with version=MANIFEST_VERSION and input_event_list=[] and asserts t109_experiment_manifest_from_dict raises InvalidManifestFieldError (because the T109 loader requires dataset_partition_refs and rejects input_event_list)
- src/robinhood_lp/reports/validation.py:699-706 load_t109_manifest_from_path refuses any payload whose version != MANIFEST_VERSION_T109 OR whose payload still carries input_event_list
- src/robinhood_lp/reports/legacy.py continues to expose the LEGACY_MANIFEST_VERSION reader for pre-T063 T069 artifacts; load_manifest_from_path continues to load T105 manifests as historical

### acceptance-predecessor-success-path-unreachable — PASS

A successful orchestrator run publishes a T109 manifest (MANIFEST_VERSION_T109) and the legacy input_event_list field is absent. The T105 build_experiment_manifest / write_manifest_to_path surfaces remain in the codebase for legacy reads but no production path invokes them for new writes.

Evidence:

- tests/test_t109_acceptance.py::TestPredecessorSuccessPathUnreachable::test_successful_run_publishes_t109_manifest drives BacktestOrchestrator through a successful SUCCEEDED run, reads the produced manifest_path JSON, asserts version == MANIFEST_VERSION_T109, dataset_partition_refs is present and non-empty, and input_event_list is absent
- grep -rn 'build_experiment_manifest\|write_manifest_to_path' src/ shows zero invocations from production code paths — the orchestrator only calls build_t109_experiment_manifest and write_t109_manifest_to_path
- src/robinhood_lp/orchestrator/__init__.py:1915-1936 catches every exception in the publication gate and writes the terminal record as FAILED; no SUCCEEDED transition can be reached without the simulation-evidence write at line 1880 succeeding first

### acceptance-no-second-fee-impl — PASS

No T104 replacement fee implementation exists under T109, reports, or the Web / presentation packages; T104 retains its dataset / window / reconstruction provenance.

Evidence:

- tests/test_t109_acceptance.py::TestT104FeeCompositionInvariants::test_no_fee_replacement_under_reports checks that robinhood_lp.reports has no FeeGrowthSurface / compute_fee_growth / reconstruct_fees attribute
- tests/test_t109_acceptance.py::TestT104FeeCompositionInvariants::test_no_fee_replacement_in_web_consumer checks the same against robinhood_lp.web and robinhood_lp.presentation if those modules are importable
- tests/test_t109_acceptance.py::TestT104FeeCompositionInvariants::test_t104_window_descriptor_carries_required_provenance checks that the T104 WindowDescriptor dataclass carries dataset_version and reconstruction_revision fields

### acceptance-binding-failures-closed-named — PASS

Dataset / PoolKey / cursor / checksum mismatches between the run's evidence and the supplied MarketState all fail closed with named ReplayFrameBindingError reasons. The three-cursor A/B/C reactive fixture proves B is pre-fill and C is post-fill, as the contract requires.

Evidence:

- tests/test_t109.py::TestReplayFrame::test_frame_mismatched_dataset_rejected asserts ReplayFrameBindingError when MarketState.dataset_version disagrees with evidence
- tests/test_t109.py::TestReplayFrame::test_frame_mismatched_pool_rejected asserts ReplayFrameBindingError when MarketState.pool_key_id disagrees with evidence
- tests/test_t109.py::TestReplayFrame::test_frame_cursor_mismatch_rejected asserts ReplayFrameBindingError when MarketState.cursor disagrees with the requested cursor
- tests/test_t109_acceptance.py::TestReactiveCursorBFixture::test_b_cursor_state_is_pre_fill asserts B-cursor RunState is pre-fill (liquidity == 0) and C-cursor RunState is post-fill (liquidity == 1000)

### acceptance-strategy-replacement-byte-equivalence — FAIL

The projector carries no strategy callback, and every callable in the projector is statically asserted not to reference strategy_callback. However, no test in this attempt registers a strategy, runs through BacktestOrchestrator + BacktestEngine, removes the strategy module, then re-runs ReplayProjector.run_state and asserts byte-equivalent output. The structural absence-check is necessary but not sufficient — the actual change-or-remove invariant the contract names is not exercised end-to-end.

Evidence:

- tests/test_t109.py::TestRunStateProjection::test_strategy_replacement_does_not_affect_run_state calls projector.run_state twice and asserts byte-equivalent output; the projector carries no strategy callback
- tests/test_t109_acceptance.py::TestStrategyReplacementByteEquivalence::test_projection_does_not_invoke_strategy_callback walks every callable attribute of the projector and asserts no source contains 'strategy_callback'
- T109 Acceptance: 'Change or remove the installed strategy implementation after a run, then prove RunState and ReplayFrame remain byte-equivalent and no strategy callback is reached (test)'

### acceptance-sparse-checkpoint-equivalence — PASS

Sparse-checkpoint equivalence is proven: a single checkpoint at cursor A plus transitions at B and C reproduces the same RunState at C as a complete snapshot at C. The artifact need not carry a snapshot per cursor.

Evidence:

- tests/test_t109_acceptance.py::TestSparseCheckpointEquivalence::test_distant_checkpoint_plus_transitions_matches_snapshot places a single RunStateCheckpoint at cursor A with state_a, populates transitions at B and C with state_b / state_c, and asserts projector.run_state(C).position equals state_c — proving the projector replays from the distant checkpoint through intervening transitions without a snapshot per cursor

### acceptance-failed-cancelled-runs-no-evidence — PASS

Failed and cancelled runs publish neither a complete-looking result nor replayable evidence — the orchestrator's exception handlers short-circuit before the manifest / report / evidence writes.

Evidence:

- tests/test_t109_acceptance.py::TestFailedCancelledRunsDoNotPublishEvidence::test_cancelled_run_publishes_no_evidence drives BacktestOrchestrator with a cancelling EventSource, asserts record.state == RunState.CANCELLED, record.simulation_evidence_path is None, and no *.simulation_evidence.json file exists in runs/
- tests/test_t109_acceptance.py::TestFailedCancelledRunsDoNotPublishEvidence::test_failed_run_publishes_no_evidence drives BacktestOrchestrator with an empty EventSource, asserts record.state == RunState.FAILED, record.simulation_evidence_path is None, and no *.simulation_evidence.json file exists in runs/

### acceptance-storage-shared-canonical-timeline — PASS

Two MarketStateReader instances over the same dataset bind the same canonical timeline and return byte-equivalent states. The reader stores no per-run market-event copy; it composes T040 every read.

Evidence:

- tests/test_t109_acceptance.py::TestStorageSharedCanonicalTimeline::test_two_readers_over_same_dataset_share_canonical_timeline builds two MarketStateReader instances over the same ReplayInput and dataset_version, reads the same cursor, and asserts byte-equivalent state plus identical dataset_version binding
- src/robinhood_lp/replay/market_state.py MarketStateReader composes the T040 replay function (not a per-run copy); the same dataset_version + pool_key_id + ReplayInput + events always yields byte-equivalent MarketState at the same cursor

### acceptance-t101-t106-t102-consumer-regression — FAIL

The compatibility adapter is implemented as standalone functions, but no T101 panel, T106 robustness runner, or T102 evaluation code path imports them. The current contract (post-split) explicitly defers 'T101 / T102 / T106 consumer cutover' to T111, which remains PLANNED with no attempt. The acceptance's regression proof that all three consumers accept new T109 artifacts, reject mismatches, and cannot reach the T069/T105 publishers is therefore not provided in this candidate — the candidate provides the adapter surface and a unit test, but no integration proof.

Evidence:

- src/robinhood_lp/reports/evidence_adapter.py:127-353 builds PanelManifestBinding and T106RobustnessBinding and resolve_panel_event_stream from a SimulationEvidence; these are standalone functions
- tests/test_t109_acceptance.py::TestConsumerRegression exercises build_panel_manifest_binding / build_t106_robustness_binding / resolve_panel_event_stream against a minimal evidence artifact
- src/robinhood_lp/research/ — no module in src/robinhood_lp/research/ imports build_panel_manifest_binding, build_t106_robustness_binding, or resolve_panel_event_stream; the T101 panel harness, T106 robustness runner, and T102 evaluation still bind to the T105 ExperimentManifest / T105 rerun path
- T109 post-split deliverable: 'Approved T101 / T102 / T106 consumer cutover' is delegated to T111 — but T111 is still PLANNED with attempt=0 in todo/config.yaml
- tests/test_t109.py::TestEvidenceAdapter::test_event_stream_adapter_does_not_invoke_publisher is a static hasattr check on the adapter module; it does not drive the actual T101 panel, T106 robustness runner, or T102 evaluation

### acceptance-pre-evidence-historical-unavailable — FAIL

The T109 loader rejects legacy T105 payloads as malformed (InvalidManifestFieldError), which is a closed-failure but not the 'explicit unsupported/unavailable result' the contract names. A legacy manifest passed to the ReplayProjector raises TypeError rather than returning an explicit unsupported verdict. The contract's named-reason unavailable result for pre-evidence runs is not implemented.

Evidence:

- tests/test_t109_acceptance.py::TestPreEvidenceHistoricalManifests::test_legacy_t105_manifest_rejected_by_t109_loader asserts t109_experiment_manifest_from_dict raises InvalidManifestFieldError when given a T105 payload
- src/robinhood_lp/reports/validation.py:699-706 load_t109_manifest_from_path refuses any payload whose version != MANIFEST_VERSION_T109 OR whose payload carries input_event_list
- The ReplayProjector at src/robinhood_lp/reports/run_state.py is bound to a SimulationEvidence dataclass; passing a legacy T105 manifest directly raises TypeError rather than producing an explicit 'unavailable' result
- T109 acceptance: 'a pre-evidence historical run returns an explicit unsupported/unavailable result'

### lint-typing-format — UNKNOWN

Mypy strict, ruff check, ruff format --check on modified files, workflow validate, and acceptance check all pass on the candidate. Two pre-existing repository-wide issues reproduce on base commit 0ef2ce0 and are unrelated to T109: a reformat drift in src/robinhood_lp/__main__.py:220 (ruff format --check on src/ flags it but the developer evidence confirms it predates T109) and a documentation-citation drift in docs/spec/architecture/ARCHITECTURE.md:154 (the §2.2 T069 row references robinhood_lp.application.backtest_runs which does not resolve under src/robinhood_lp/). Neither is introduced by T109; both reproduce on the base.

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy --strict on the 9 modified / new source files: 'Success: no issues found in 9 source files'
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff check on src/robinhood_lp/reports/validation.py src/robinhood_lp/reports/manifest.py src/robinhood_lp/orchestrator/__init__.py src/robinhood_lp/backtest/engine.py src/robinhood_lp/backtest/events.py src/robinhood_lp/reports/run_state.py src/robinhood_lp/reports/simulation_evidence.py src/robinhood_lp/replay/market_state.py src/robinhood_lp/reports/evidence_adapter.py tests/test_t109.py tests/test_t109_acceptance.py tests/test_backtest_t069.py: 'All checks passed!'
- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python3.12 -m ruff format --check src/ tests/test_t109.py tests/test_t109_acceptance.py tests/test_backtest_t069.py reports a pre-existing reformat drift in src/robinhood_lp/__main__.py:220 (developer evidence confirms it reproduces on base commit 0ef2ce0)
- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow validate: '{"status": "OK"}'
- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.check_acceptance check: 'acceptance check passed: no findings'
- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.check_citations check: 'citation check failed: architecture-section22: docs/spec/architecture/ARCHITECTURE.md:154 (robinhood_lp.application.backtest_runs): §2.2 row for 'T069' references module 'robinhood_lp.application.backtest_runs' which does not resolve under src/robinhood_lp/' — pre-existing on base commit 0ef2ce0 per developer evidence
- git diff --check 0ef2ce0..fac669a: clean (no whitespace warnings)

### must-not-no-second-replay-impl — PASS

No second replay-of-raw-events engine exists; MarketState composes T040; RunState applies recorded transitions only; the strategy callback is never invoked.

Evidence:

- src/robinhood_lp/replay/market_state.py MarketStateReader delegates reconstruction to robinhood_lp.replay.replayer.replay (the existing T040 surface)
- src/robinhood_lp/reports/run_state.py ReplayProjector applies RunTransition payloads to the recorded initial PositionState; it does not invoke the T061 engine, the strategy callback, or storage / RPC / signer
- tests/test_t109.py::TestInvariants::test_projector_has_no_strategy_callback asserts the projector carries no strategy attribute
- tests/test_t109.py::TestInvariants::test_reader_stores_no_per_run_market_copy asserts the reader does not persist events separately

### must-not-duplicate-market-timeline — PASS

The T109 evidence and manifest artifacts never embed the canonical market event timeline; the dataset content hash + partition references are the binding.

Evidence:

- SimulationEvidence dataclass carries dataset_version, dataset_content_hash, dataset_schema_version, dataset_decode_version, reconstruction_revision and transitions; no input_event_list or events field
- tests/test_t109.py::TestCompatibilityInvariants::test_artifact_does_not_embed_market_event_list asserts 'input_event_list' / 'events' / 'market_events' are absent from evidence.to_dict()
- T109ExperimentManifest at src/robinhood_lp/reports/manifest.py:1019-1087 carries dataset_partition_refs + dataset_event_count + simulation_evidence_ref; no embedded input_event_list
- tests/test_t109_acceptance.py::TestPredecessorSuccessPathUnreachable::test_successful_run_publishes_t109_manifest asserts the produced manifest JSON has dataset_partition_refs and no input_event_list

### must-not-post-sort-or-reinterpret-audit — PASS

Audit ordering is enforced at publication (SimulationEvidence construction) and at projection; the engine emits the audit chain in cursor order and the artifact constructor refuses to repair a non-causal history.

Evidence:

- src/robinhood_lp/reports/simulation_evidence.py:485-514 SimulationEvidence.__post_init__ rejects non-monotonic cursor and non-strict-ordinal transition chains at construction; no post-sort repair is possible
- src/robinhood_lp/orchestrator/__init__.py:2097-2230 _build_simulation_evidence walks the engine's audit chain in emit order and assigns ordinals in that order; the artifact constructor then validates the resulting ordering
- src/robinhood_lp/reports/run_state.py:482-548 ReplayProjector.run_state replays transitions in their stored ordinal order; no reordering at projection time

## Must-not violations

- Must-not 'never derive that binding later from T061's integer timestamp' is violated by src/robinhood_lp/orchestrator/__init__.py:2130-2135 which falls back to (int(evt.timestamp), 0, 0) when extract_event_cursor returns None — and extract_event_cursor returns None for every real BacktestEvent because src/robinhood_lp/protocol/contracts.py:264-302 BacktestEvent has no block_number / transaction_index / log_index attributes.

## Unknowns

- T061 engine-level golden acceptance (compare ReplayProjector.run_state at entry / in-range-fee-accrual / out-of-range-wait / rebalance / exit cursors against the engine's captured ledger / equity / T052 attribution state) is not exercised in this candidate; the developer evidence explicitly defers it to tests/test_backtest_t061.py and notes this attempt's T109 layer does not depend on its result.
- T061 engine-side schedule restructure (queue latency/fill pipeline at trigger cursor; release at fill-data cursor; admit fill-data event to information frontier before invoking new reactive callback) is not implemented — the engine still processes each event synchronously and mutates the ledger at trigger-event-iteration. The contract's invariant is satisfied at the projection layer but not at the engine layer.
- T088 ('ReplayFrame consumer') and T096 ('T109 evidence into the final matrix') are downstream consumers; no T088 / T096 test in this candidate exercises the new ReplayFrame / SimulationEvidence surfaces.
- T101 / T106 / T102 consumer integration: the compatibility adapter is implemented as standalone functions and tested in isolation, but no code path in src/robinhood_lp/research/ imports them. The contract's regression proof that all three consumers accept new T109 artifacts, reject mismatches, and cannot reach the T069/T105 publishers is not provided in this candidate (delegated to T111 per the post-split task contract).
- Heterogeneous-fixture acceptance clause (two pools with T040/T041 prefix equivalence at beginning, intra-block, end-of-block, and range-end cursors) is reduced to same-dataset two-reader equivalence at empty-events cursors. Full V4 protocol records for the heterogeneous fixtures are out of scope per the developer evidence.
- Pre-evidence historical unavailable result: a legacy T105 manifest passed to t109_experiment_manifest_from_dict raises InvalidManifestFieldError; a legacy T105 manifest passed directly to ReplayProjector raises TypeError. The contract's 'explicit unsupported/unavailable result' verdict for pre-evidence runs is not implemented as a named reason.

## Required changes

- Add block_number / transaction_index / log_index attributes to src/robinhood_lp/protocol/contracts.py BacktestEvent so extract_event_cursor can return a real (block, tx, log) cursor rather than None. Remove the (timestamp, 0, 0) fallback at src/robinhood_lp/orchestrator/__init__.py:2130-2135 so the audit chain cannot carry a cursor derived from the integer timestamp. A state-changing transition whose source event lacks a real cursor must fail publication, not fall back to (timestamp, 0, 0).
- Restructure the T061 engine at src/robinhood_lp/backtest/engine.py so a delayed-fill pipeline is queued at the trigger cursor and released only when the main market loop reaches the fill-data cursor: do not mutate the ledger or append LATENCY / FILL audit events while still processing the trigger; admit the fill-data event to the information frontier before releasing due latency/fill pipelines in deterministic trigger-cursor / pipeline-identity order; only then supply the resulting state to the next reactive callback / risk pipeline at the fill-data cursor. The current synchronous schedule violates the contract's 'no intervening callback observes a queued future fill' invariant at the engine layer (the projection layer preserves it via cursor metadata only).
- Add a test that drives BacktestOrchestrator.submit() through a delayed-fill scenario with an intervening reactive market cursor B, then loads the produced simulation_evidence.json, parses the transitions, and asserts every FILL transition's cursor is the later fill-data cursor (not the trigger cursor) and that the audit chain is cursor-monotonic without post-sorting. The current acceptance tests use synthetic transitions built directly via RunTransition(cursor=MarketCursor(...), ...) and never exercise the engine's cursor binding.
- Wire build_panel_manifest_binding, build_t106_robustness_binding, and resolve_panel_event_stream into the actual T101 panel / harness, T106 robustness runner, and T102 evaluation code paths (or scope this explicitly to T111 in the post-split contract). The current evidence_adapter module is exercised only by standalone unit tests; no production code path imports it.
- Implement an explicit pre-evidence historical unavailable result: when a legacy T105 manifest is passed to a T109 reader, return a named 'T109_HISTORICAL_UNAVAILABLE' result (or raise a named ReplayFrameBindingError subclass) rather than TypeError or generic InvalidManifestFieldError. The contract names 'explicit unsupported/unavailable result' as the verdict.
- Add a real end-to-end T061 engine run-state equivalence test: run a real BacktestEngine.run() over a T100-qualified dataset, capture the engine's recorded ledger / equity / T052 attribution at entry / in-range-fee-accrual / out-of-range-wait / rebalance / exit cursors, then assert ReplayProjector.run_state at the same cursors returns byte-equivalent state. The current acceptance reduces this to a projection-layer byte-equivalence check with synthetic transitions.
- Add a strategy-replacement test that registers a strategy, runs through BacktestOrchestrator + BacktestEngine to capture evidence, removes the strategy module, then re-runs ReplayProjector.run_state and ReplayProjector.frame and asserts byte-equivalent output. The current structural absence-check is necessary but not sufficient.
- Resolve the pre-existing documentation-citation drift at docs/spec/architecture/ARCHITECTURE.md:154 (§2.2 T069 row references robinhood_lp.application.backtest_runs which does not resolve under src/robinhood_lp/) — out of scope for T109 but blocks the docs/implement/test_acceptance CI gate and is unrelated to the candidate.

## Residual risks

- Pre-existing repository-wide failures test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output (forge submodule missing) and test_documentation_citations.py::test_check_passes_on_real_repository (ARCHITECTURE.md §2.2 T069 reference drift) reproduce on base commit 0ef2ce0 and are unrelated to T109.
- Heterogeneous-fixture acceptance clause is reduced to same-dataset two-reader equivalence at empty-events cursors; full V4 protocol records for heterogeneous fixtures are out of scope per the developer evidence.
- T101 / T106 / T102 consumer integration is deferred to T111 per the post-split task contract; T111 is PLANNED with attempt=0 and no candidate commit.
- AuditEvent.cursor is set in the engine emission path; the audit event_id is computed from a canonical serialisation that does NOT include the cursor field, so existing audit chains remain byte-identical across the T109 cutover. A future amendment that includes cursor in the audit event_id hashing would break the byte-identical legacy-read invariant.
- The T069 source-manifest binding accepts both the legacy T105 manifest and the new T109 manifest; both paths call validate_manifest against the same dataset qualification record. Future amendments that introduce additional manifest schemas must extend the validate_manifest branch.
- Pre-existing reformat drift in src/robinhood_lp/__main__.py:220 reproduces on base commit 0ef2ce0 and is unrelated to T109.
