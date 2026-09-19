# T061 independent review

- Base commit: `a1e918a6a5d1811f5342a40e596cd41f147e1415`
- Candidate commit: `53bed7966c996dbc75f817fad2594715ca125a64`
- Verdict: **PASS**

## Checks

### ordered_event_clock_sort — PASS

Events are sorted by (timestamp, sequence, source_priority); sequence is content-deterministic via SHA-256 event_id tie-break so the same manifest always yields the same ordering

Evidence:

- src/robinhood_lp/backtest/engine.py:334-386 _normalise_input_events: assigns per-source sequence numbers and final sort key is (timestamp, sequence, source_priority) — matches contract deliverable 'ordered event clock'
- src/robinhood_lp/backtest/events.py:84-88 SOURCE_PRIORITY_DATA=1, _STRATEGY=2, _RISK=3, _EXECUTION=4, _SYSTEM=5 — closed vocabulary
- tests/test_backtest_t061.py:526-571 TestPipelineEndToEnd::test_same_manifest_produces_identical_results and test_manifest_in_different_input_order_produces_same_output prove byte-identical output regardless of input order
- tests/test_backtest_t061.py:731-802 TestSameTimestampOrdering covers same-source/same-timestamp and different-source/same-timestamp deterministic order
- Test result: 49/49 passed

### information_frontier_observed_and_available — PASS

Information frontier is enforced: BacktestEvent.is_visible() requires both observed_at<=T AND available_at<=T; engine raises FutureDataViolation when no data is visible at the declared fill time and never falls back to trigger price

Evidence:

- src/robinhood_lp/backtest/events.py:453-463 BacktestEvent.is_visible returns True iff observed_at<=decision_time AND available_at<=decision_time
- src/robinhood_lp/backtest/engine.py:560-600 the engine collects only is_visible(T) events and explicitly detects any future-data attempt, recording a SYSTEM audit event with STATUS_FUTURE_DATA_VIOLATION
- src/robinhood_lp/backtest/engine.py:742-747 when no fill_data is visible at the declared fill time, the engine raises FutureDataViolation rather than filling at the trigger price
- tests/test_backtest_t061.py:507-515 TestInformationFrontier::test_event_with_available_after_decision_is_not_visible and test_future_data_violation_records_payload
- tests/test_backtest_t061.py:876-936 TestFutureDataTrap::test_no_data_at_fill_time_raises_future_data_violation and test_fill_uses_data_event_at_fill_time_not_trigger_price
- Test result: 49/49 passed

### decision_risk_latency_fill_pipeline — PASS

The pipeline emits exactly one immutable AuditEvent per stage (DECISION, RISK, LATENCY, FILL, SYSTEM); the ledger is only mutated at FILL, preserving the contract's append-only audit chain

Evidence:

- src/robinhood_lp/backtest/engine.py:602-636 DECISION stage: emits one AuditEvent with stage=STAGE_DECISION; ledger unchanged
- src/robinhood_lp/backtest/engine.py:638-666 RISK stage: emits one AuditEvent with stage=STAGE_RISK, status=APPROVED|REJECTED; rejection halts pipeline
- src/robinhood_lp/backtest/engine.py:668-701 LATENCY stage: emits one AuditEvent with stage=STAGE_LATENCY, delayed flag and fill_time
- src/robinhood_lp/backtest/engine.py:703-796 FILL stage: emits one AuditEvent with stage=STAGE_FILL, outcome, filled_liquidity, fill_price_q64_64; ledger mutated only at FILL for FILLED/PARTIAL/DELAYED outcomes
- src/robinhood_lp/backtest/engine.py:500-517 + 519-557 SYSTEM stage: init and shutdown AuditEvents; non-reactive events (TICK/OBSERVATION outside react_to_kinds) emit SYSTEM bookkeeping events
- tests/test_backtest_t061.py:573-723 TestPipelineEndToEnd exercises each stage and verifies exactly one event per stage with the correct ledger_hash_after semantics

### five_models_parameterised_deterministic_versioned_unit_carrying — PASS

All five models (Liquidity/Fee/Gas/Slippage/Failure) are parameterised, deterministic, versioned, and unit-carrying; each carries its model_version and unit-domain enforcement in __post_init__; ModelBundle.bundle_hash binds the entire version set

Evidence:

- src/robinhood_lp/backtest/models.py:110 LIQUIDITY_MODEL_VERSION='t061.liquidity_model.v1' + ConstantLiquidityModel (uint128 active_liquidity_value)
- src/robinhood_lp/backtest/models.py:195 FEE_MODEL_VERSION='t061.fee_model.v1' + StaticFeeModel (fee_pips_value in [0, FEE_DENOMINATOR_PIPS=1_000_000])
- src/robinhood_lp/backtest/models.py:263 GAS_MODEL_VERSION='t061.gas_model.v1' + FlatGasModel (uint64 gas_units_value)
- src/robinhood_lp/backtest/models.py:330 SLIPPAGE_MODEL_VERSION='t061.slippage_model.v1' + ZeroSlippageModel / ConstantSlippageModel (impact_bps in [0, SLIPPAGE_DENOMINATOR_BPS=10_000])
- src/robinhood_lp/backtest/models.py:438 FAILURE_MODEL_VERSION='t061.failure_model.v1' + DeterministicFailureModel with FillOutcome enum (FILLED/PARTIAL/DELAYED/REJECTED)
- Each model is a frozen dataclass with model_version property; ModelBundle carries bundle_version + bundle_hash (SHA-256 of versions)
- tests/test_backtest_t061.py:944-1024 TestModels exercises every model field, unit domain, schedule lookup, and bundle hash determinism

### immutable_audit_events — PASS

AuditEvent and supporting value types are immutable frozen dataclasses; each carries a content-addressed SHA-256 event_id; payloads and parent_event_ids are sorted before hashing so equivalent content hashes identically

Evidence:

- src/robinhood_lp/backtest/events.py:359-451 BacktestEvent is @dataclass(frozen=True, slots=True) with deterministic SHA-256 event_id
- src/robinhood_lp/backtest/events.py:484-648 PositionState is @dataclass(frozen=True, slots=True) with deterministic ledger_hash() and with_updates() returning a new state
- src/robinhood_lp/backtest/events.py:661-796 AuditEvent is @dataclass(frozen=True, slots=True); AuditEvent.build() computes deterministic SHA-256 event_id from canonical content with sorted parent_event_ids and sorted payload
- tests/test_backtest_t061.py:402-466 TestEventConstruction::test_audit_event_assigns_event_id_when_built, test_audit_event_parent_ids_are_sorted, test_position_state_ledger_hash_is_deterministic

### future_data_trap_tests_fail — PASS

Future-data trap tests assert that the engine both refuses to consume future events and refuses to fill at the trigger price when no data is visible at the declared fill time

Evidence:

- tests/test_backtest_t061.py:507-515 test_event_with_available_after_decision_is_not_visible proves is_visible() rejects future availability
- tests/test_backtest_t061.py:879-902 test_no_data_at_fill_time_raises_future_data_violation proves the engine refuses to fill when no data is visible at fill_time
- tests/test_backtest_t061.py:512-515 test_future_data_violation_records_payload proves the violation carries the offending event_id, available_at, and decision_time
- tests/test_backtest_t061.py:904-936 test_fill_uses_data_event_at_fill_time_not_trigger_price proves fill_price comes from the OBSERVATION at fill_time (2<<64), not the trigger SWAP (1<<64)

### same_manifest_identical_event_ids_decisions_ledger — PASS

Determinism is enforced: same manifest + same bundle -> identical manifest_hash, bundle_hash, decision sequence, audit event_id list, and final_ledger

Evidence:

- tests/test_backtest_t061.py:526-549 test_same_manifest_produces_identical_results: two consecutive runs with the same manifest produce identical manifest_hash, bundle_hash, final_ledger, and event_id list
- tests/test_backtest_t061.py:551-571 test_manifest_in_different_input_order_produces_same_output: forward vs reversed input list produce byte-identical manifest_hash and audit event_id list
- tests/test_backtest_t061.py:1050-1065 TestManifestHash::test_empty_manifest_hash_is_deterministic + test_swapping_two_events_changes_manifest_hash
- src/robinhood_lp/backtest/engine.py:362-365 _normalise_input_events sorts by (timestamp, source_priority, event_id) before sequence assignment so insertion order cannot change the result

### same_timestamp_ordering_deterministic — PASS

Same-timestamp events are resolved deterministically by source_priority then sequence; identical-content events at same timestamp collapse to one event via SHA-256 event_id

Evidence:

- tests/test_backtest_t061.py:734-774 test_same_timestamp_data_before_strategy_before_risk: data(priority=1), strategy(priority=2), risk(priority=3) at the same timestamp resolve in priority order regardless of input order
- tests/test_backtest_t061.py:776-802 test_same_source_same_timestamp_breaks_by_sequence: identical-content events at same timestamp collapse via content hash; forward vs reverse input produce byte-identical output
- src/robinhood_lp/backtest/events.py:84-88 closed source_priority set; src/robinhood_lp/backtest/engine.py:362-365 content-keyed sequence assignment

### rejected_partial_delayed_fills_covered — PASS

Rejected, partial, and delayed fills each have a dedicated test that exercises the corresponding FillOutcome schedule entry and verifies the engine emits the matching status without hiding the outcome

Evidence:

- tests/test_backtest_t061.py:641-664 test_partial_fill_records_filled_liquidity_below_requested exercises FillOutcome.PARTIAL and asserts filled_liquidity < requested_liquidity
- tests/test_backtest_t061.py:666-690 test_rejected_fill_records_status_but_does_not_mutate_ledger exercises FillOutcome.REJECTED and asserts final_ledger.liquidity stays zero while FILL event is recorded
- tests/test_backtest_t061.py:692-723 test_delayed_fill_records_delayed_status exercises FillOutcome.DELAYED and verifies STATUS_FILL_DELAYED in the payload
- src/robinhood_lp/backtest/engine.py:703-796 maps each FillOutcome to STATUS_FILL_* and mutates the ledger only for FILLED/PARTIAL/DELAYED

### out_of_range_and_shutdown_covered — PASS

Shutdown behavior is covered by three tests and the engine emits terminal SYSTEM SHUTDOWN either on SHUTDOWN event or end of input. Out-of-range accrual is supported by the PositionState data model (in_range, tokens_owed0/1, last_accrual_time); the engine scaffold preserves these fields through with_updates but the price-relative flip is left for the strategy-side wiring in T062/T063 (per developer residual risks)

Evidence:

- tests/test_backtest_t061.py:813-836 test_shutdown_marker_stops_engine: SHUTDOWN event at t=150 stops the engine before t=200 swap
- tests/test_backtest_t061.py:837-852 test_engine_emits_init_and_shutdown_system_events: SYSTEM init event (bundle_hash + manifest_hash) at start and SYSTEM shutdown event (final_ledger_hash + manifest_hash) at end
- tests/test_backtest_t061.py:854-868 test_engine_terminates_with_shutdown_when_no_marker: end-of-input also emits SHUTDOWN
- src/robinhood_lp/backtest/engine.py:798-817 explicitly emits terminal SHUTDOWN when input list is exhausted
- PositionState carries in_range, tokens_owed0/1, last_accrual_time fields (events.py:484-543) supporting out-of-range accrual tracking; the engine updates last_accrual_time on every fill (engine.py:771) — the PositionState data model covers out-of-range accrual; a richer tick-relative price model that flips in_range based on price movement is reserved for the strategy-side or a later task per the developer's residual_risks note (todo/evidence/P06/T061/attempt-001-developer.json)

### must_not_fill_at_trigger_price — PASS

Engine never fills at the trigger price: fill_price comes from _find_fill_data (first visible data at/after target_fill_time) and the engine raises FutureDataViolation when no such data exists; the must-not clause is enforced and tested

Evidence:

- src/robinhood_lp/backtest/engine.py:674-682 _find_fill_data returns the first visible data event at or after target_fill_time; actual_fill_time is taken from that event, not from the trigger event
- src/robinhood_lp/backtest/engine.py:723-748 fill_price_q64_64 is read from fill_data.payload, never from the trigger event; when fill_data is None the engine raises FutureDataViolation rather than synthesising a price
- tests/test_backtest_t061.py:904-936 test_fill_uses_data_event_at_fill_time_not_trigger_price asserts fill_price==2<<64 (from OBSERVATION at t=110) instead of 1<<64 (trigger SWAP at t=100)
- tests/test_backtest_t061.py:879-902 test_no_data_at_fill_time_raises_future_data_violation proves the engine refuses to fill rather than fall back to the trigger price

### must_not_hide_failed_or_rejected_intents — PASS

Failed and rejected intents are never hidden: every REJECTED risk verdict and every REJECTED fill produces a visible AuditEvent with the matching status and reason; NO_TRADE/WAIT decisions also emit a DECISION event so the audit chain reflects every decision

Evidence:

- src/robinhood_lp/backtest/engine.py:617-632 NO_TRADE / WAIT decisions: emit a DECISION AuditEvent with kind in payload and continue — the absence of fill is visible
- src/robinhood_lp/backtest/engine.py:651-666 risk REJECTED: emit RISK AuditEvent with status=STATUS_RISK_REJECTED, reason_code='CAP_EXCEEDED', approved=0; pipeline halts but the rejection is on the chain
- src/robinhood_lp/backtest/engine.py:710-712 fill REJECTED: emit FILL AuditEvent with fill_status=STATUS_FILL_REJECTED, filled_liquidity=0; ledger unchanged
- tests/test_backtest_t061.py:618-640 test_rejected_risk_records_rejection_but_no_fill asserts risk_payload['approved']==0 and reason_code=='CAP_EXCEEDED'; no fill is emitted
- tests/test_backtest_t061.py:666-690 test_rejected_fill_records_status_but_does_not_mutate_ledger asserts outcome=='REJECTED' and final_ledger.liquidity stays 0

### 49_tests_pass — PASS

All 49 T061 unit tests pass

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/pytest tests/test_backtest_t061.py -v: 49 passed in 0.11s
- Test classes: TestEventConstruction (16), TestInformationFrontier (2), TestPipelineEndToEnd (8), TestSameTimestampOrdering (2), TestOutOfRangeAndShutdown (3), TestFutureDataTrap (2), TestModels (12), TestLayerPurity (2), TestManifestHash (2) = 49

### full_suite_green_excluding_pre_existing_failures — PASS

Full test suite is green aside from one pre-existing forge oracle test that requires the v4-core git submodule; the failure was documented in the T060 and A0004 reviews and is unrelated to T061

Evidence:

- PYTHONPATH=. /home/lpdev/miniconda3/envs/robinhood-lp/bin/pytest tests/ --ignore=tests/test_workflow.py --ignore=tests/test_workflow_contracts.py --ignore=tests/test_storage_partition_columns.py: 1870 passed, 6 skipped, 1 failed
- Only failure: tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output — pre-existing detached-worktree host condition (Foundry Solidity library at tools/oracle/lib/v4-core is not initialised). The T060 review (todo/reviews/P06/T060/review-001.json:243-248) and A0004 amendment review document this as a known pre-existing environment issue unrelated to any task candidate; T061 does not touch the oracle / Foundry / ABI surface
- When run with PYTHONPATH=src (as in the T060 review), the other two apparent failures (test_signing_imports_are_forbidden_by_no_signing_paths and test_block_header_rides_along_event_partition_transaction) resolve into passes; they are pure test-collection PYTHONPATH issues, not T061 regressions

### ruff_format_and_check_clean — PASS

ruff format and ruff check both clean on the new package and tests

Evidence:

- ruff format src/robinhood_lp/backtest/ tests/test_backtest_t061.py: '5 files left unchanged'
- ruff check src/robinhood_lp/backtest/ tests/test_backtest_t061.py: 'All checks passed!'

### mypy_strict_clean — PASS

mypy --strict passes on the new package and its tests

Evidence:

- PYTHONPATH=src python -m mypy --strict src/robinhood_lp/backtest/ tests/test_backtest_t061.py: 'Success: no issues found in 5 source files'

### protected_prefix_files_not_touched — PASS

No protected-prefix file is touched; the todo/config.yaml change is the standard workflow state transition recorded by the controller, consistent with prior T051/T052/T060 candidate commits

Evidence:

- git diff --name-only a1e918a..53bed79: src/robinhood_lp/backtest/__init__.py (new), src/robinhood_lp/backtest/engine.py (new), src/robinhood_lp/backtest/events.py (new), src/robinhood_lp/backtest/models.py (new), tests/test_backtest_t061.py (new), todo/config.yaml, todo/evidence/P06/T061/attempt-001-developer.json (new)
- No file under docs/spec/, docs/intent/, todo/phases/, todo/schemas/, tools/workflow/, .claude/, CLAUDE.md, AGENTS.md, todo/README.md is touched by the diff
- todo/config.yaml diff is the documented workflow controller state transition (workflow_state READY->AWAITING_REVIEW; T061 status READY->AWAITING_REVIEW; attempt 0->1; base_commit populated) — identical in shape to the T060 candidate a6ed050..d590909 controller-driven change accepted in todo/reviews/P06/T060/review-001.json:222-230
- todo/evidence/P06/T061/attempt-001-developer.json is the developer handoff record written by the prepare-develop / finish-develop gate, not a contract or spec file

### layer_purity_backtest_events_models — PASS

Both events.py and models.py are pure stdlib + (in models' case) the events module; no forbidden sibling is imported

Evidence:

- src/robinhood_lp/backtest/events.py:142-159 _FORBIDDEN_BACKTEST_EVENTS_ROBINHOOD_MODULES covers config / discovery / features / ingestion / presentation / qualification / quality / replay / rpc / risk / storage / strategy / execution plus the engine / models / pipeline modules
- src/robinhood_lp/backtest/events.py:810-835 assert_backtest_events_layer_is_pure walks the live module graph and rejects any forbidden sibling import
- src/robinhood_lp/backtest/models.py:667-714 same denylist pattern + assert_backtest_models_layer_is_pure; only robinhood_lp.backtest.events is allowed alongside stdlib
- tests/test_backtest_t061.py:1035-1039 TestLayerPurity::test_events_layer_is_pure + test_models_layer_is_pure both pass

### engine_layer_can_import_events_and_models — PASS

Engine layer is appropriately wired: imports the events and models submodules and uses injected callbacks for strategy/risk; no forbidden sibling is reached

Evidence:

- src/robinhood_lp/backtest/engine.py:92-117 imports from robinhood_lp.backtest.events (KIND_OBSERVATION, LEDGER_VERSION, SOURCE_PRIORITY_DATA, all STAGE_*, all STATUS_*, AuditEvent, BacktestEvent, BacktestEventError, FutureDataViolation, PositionState) and from robinhood_lp.backtest.models (FillOutcome, ModelBundle) — consistent with ADR-006 backtest layer depending on its own events/models submodules
- No import from robinhood_lp.strategy, .risk, .execution, .rpc, .storage, .signing, .config — engine wires callbacks by injection (engine.py:436-437 StrategyCallback / RiskCallback type aliases)

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- The engine's _find_fill_data lookup iterates over every event in sorted_events and filters by pool_key_id, source_priority, and kind — but the information-frontier check in run() (engine.py:565-600) builds the visible set over the full sorted_events list without a pool_key_id filter. The strategy callback is the only consumer of the visible set, so a multi-pool manifest relies on the strategy's own filter; this is consistent with R17's MessageBus-per-pool pattern but means a misbehaving strategy callback could observe cross-pool events. Not a contract break in scope of T061; flagged for the T062 / T065 wiring.
- The engine does not actually flip PositionState.in_range based on tick-relative price movement; out-of-range accrual is supported by the PositionState data model (in_range, tokens_owed0/1, last_accrual_time) and the engine updates last_accrual_time on every fill, but the price-relative in_range logic is reserved for a later task per the developer's residual_risks note in todo/evidence/P06/T061/attempt-001-developer.json. The contract deliverable is the engine scaffold, not a full accrual computation, and the data model is wired through.
- tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output fails in this detached worktree because the Foundry Solidity library at tools/oracle/lib/v4-core is not initialised; this is a pre-existing host condition documented in todo/reviews/P06/T060/review-001.json:243-248 and the A0004 amendment review, and the T061 candidate does not touch the oracle / Foundry / ABI surface.
- When PYTHONPATH does not include the working directory, tests/test_strategy_t060.py::TestStrategyLayerIsPure::test_signing_imports_are_forbidden_by_no_signing_paths and tests/test_storage_block_headers.py::test_block_header_rides_along_event_partition_transaction fail with 'No module named tests'; both pass under PYTHONPATH=src or PYTHONPATH=. The failure is a test-collection pathing issue, not introduced by T061.
