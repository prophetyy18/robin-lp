# T065 independent review

- Base commit: `97d83bf0eca4415044551673b7d61fb73f254b12`
- Candidate commit: `a703ad31faf4d0e80f376a1495b83a48995acc02`
- Verdict: **FAIL**

## Checks

### replaceable-regime-and-fee-models — PASS

Replaceable regime and fee-opportunity models are correctly implemented. Stub injection only alters the assessment; the candidate-action shape, ledger/risk/execution contracts remain unchanged.

Evidence:

- src/robinhood_lp/strategy/adaptive.py lines 1133-1178 define runtime-checkable Protocols AdaptiveRegimeModel and AdaptiveFeeOpportunityModel with assess() method.
- src/robinhood_lp/strategy/adaptive.py lines 1376-1670 provide RuleBasedAdaptiveRegimeModel and RuleBasedAdaptiveFeeOpportunityModel as the reference implementations.
- tests/test_strategy_t065.py TestModelReplacement (lines 1378-1466) verifies that injecting _AlwaysJumpDownRegime and _AlwaysUncertainFee stubs changes only the assessment, not the candidate-action shape or audit chain.
- Test confirms isinstance(stub, AdaptiveRegimeModel) and isinstance(stub, AdaptiveFeeOpportunityModel) at runtime.

### downside-asymmetric-trend-jump-filter — PASS

Downside asymmetry is enforced both at parameter construction and at runtime evaluation. A downward jump fires JUMP_RISK at a smaller magnitude than an upward jump would require.

Evidence:

- AdaptiveRangeParameters.__post_init__ at lines 535-550 enforces up_jump_threshold > down_jump_threshold and up_trend_threshold > down_trend_threshold (raises InvalidAdaptiveParameterError otherwise).
- TestParameterSchema::test_downside_asymmetry_is_required (tests/test_strategy_t065.py lines 428-441) confirms construction rejects equal and inverted thresholds.
- TestGoldenJumps::test_downward_jump_triggers_jump_down_risk (lines 711-751) confirms a -15% bar fires JUMP_RISK with jumps_down=True.
- TestGoldenJumps::test_downside_asymmetry_smaller_threshold_for_downward (lines 753-793) confirms a +15% bar does NOT fire JUMP_RISK (threshold is 25% for up).
- Default thresholds: down_jump = Q64_SCALE/10 (10%), up_jump = Q64_SCALE/4 (25%); down_trend = Q64_SCALE/20 (5%), up_trend = Q64_SCALE/10 (10%) — down band is strictly tighter.

### range-occupancy-break-return-assessment — PASS

Range occupancy/break/return assessment is present. The strategy computes in-range fraction, downgrades RANGE to UNCERTAIN when occupancy is below the threshold, and handles the out-of-range break path through WAIT_OUT/REBUILD/REBUILD_DEFER.

Evidence:

- AdaptiveRegimeAssessment.range_occupancy_q64_64 (line 908) carries the in-range fraction as Q64.64.
- _range_occupancy_q64_64 helper (lines 1287-1323) computes in-range fraction from the price series.
- RuleBasedAdaptiveRegimeModel.assess at lines 1500-1514 downgrades to UNCERTAIN when occupancy < params.range_occupancy_threshold_q64_64.
- TestSnapshotInvariants (lines 478-555) verifies portfolio snapshot tick_lower < tick_upper invariant for non-empty positions.
- TestGoldenStableRange (lines 608-649) and TestGoldenOutOfRangeLifecycle (lines 915-989) cover occupancy-driven RANGE classification and out-of-range lifecycle.

### expected-fee-depth-cost-distributions — PASS

Expected fee/depth/cost distributions are recorded on every decision through AdaptiveFeeOpportunityAssessment fields and the candidate action's cost_ratio_q64_64.

Evidence:

- AdaptiveFeeOpportunityAssessment (lines 1021-1130) carries expected_fee_edge_q64_64, expected_in_range_fraction_q64_64, projected_own_liquidity_share_q64_64, expected_cost_ratio_q64_64, sample_count.
- RuleBasedAdaptiveFeeOpportunityModel.assess (lines 1578-1670) projects these quantities from the visible volume/liquidity evidence.
- AdaptiveCandidateAction.cost_ratio_q64_64 (line 1706) carries the cost ratio for REBUILD_DEFER candidates.
- _rebuild_defer helper (lines 2723-2753) populates cost_ratio_q64_64 from the fee assessment.

### complete-out-of-range-lifecycle-actions — FAIL

The RETURN lifecycle kind is defined in the enum and a _return() helper exists, but evaluate() never emits it. When a held position transitions from out-of-range to in-range, the strategy emits WAIT with IN_RANGE notes instead of RETURN. The contract requires 'complete out-of-Range lifecycle actions (WAIT_OUT, RETURN, REBUILD, REBUILD_DEFER, EXIT)'; 4 of 5 are emitted. TestGoldenOutOfRangeLifecycle claims RETURN coverage but provides none.

Evidence:

- AdaptiveCandidateKind enum (lines 290-329) declares NO_TRADE, WAIT, PROPOSE, WAIT_OUT, RETURN, REBUILD, REBUILD_DEFER, EXIT.
- Task contract deliverables (todo/phases/P06-backtesting-and-strategy/T065.md lines 18-19) explicitly require 'complete out-of-Range lifecycle actions' enumerating WAIT_OUT, RETURN, REBUILD, REBUILD_DEFER, EXIT.
- evaluate() at lines 2006-2298 emits WAIT_OUT (lines 2088, 2168, 2207, 2354), REBUILD (lines 2238, 2446), REBUILD_DEFER (lines 2226, 2373), EXIT (line 2105).
- AdaptiveStrategy._return() helper (lines 2655-2684) is defined and builds a RETURN candidate with notes='RETURN_TO_RANGE', but is NEVER called from evaluate() or _range_path().
- Grep evidence: `grep -n 'self._return' src/robinhood_lp/strategy/adaptive.py` returns no matches in evaluate()/_range_path(); the only occurrence is the helper definition.
- _range_path() line 2335-2348: when a held position is in-range, it emits WAIT with notes=('IN_RANGE',) — not RETURN.
- TestGoldenOutOfRangeLifecycle (lines 915-989) class docstring claims 'The full out-of-Range lifecycle: WAIT_OUT, RETURN, REBUILD' but only test_out_of_range_within_wait_bound_waits_out and test_out_of_range_beyond_wait_bound_rebuilds are implemented; no RETURN test exists.
- Grep for `RETURN` in tests/test_strategy_t065.py returns only docstring references, no test method exercising the RETURN kind.

### immutable-parameter-schema-and-uncertainty-reason-codes — PASS

Immutable parameter schema with uncertainty/reason codes is correctly implemented. AdaptiveRangeParameters is frozen, validates invariants, and produces a deterministic version hash.

Evidence:

- AdaptiveRangeParameters is a frozen dataclass with __post_init__ validation (lines 419-605).
- AdaptiveReasonCode StrEnum (lines 231-289) declares 18 stable reason codes including strategy-hold, regime, fee-opportunity, range-lifecycle, 5-minute rule, and invalidation categories.
- AdaptiveRegimeAssessment.uncertainty_codes and AdaptiveFeeOpportunityAssessment.uncertainty_codes (lines 912, 1047) carry structured uncertainty codes.
- _versioned_parameter_id (lines 1863-1894) produces a SHA-256 hex of canonical parameter serialisation for audit chain versioning.
- TestParameterSchema (lines 419-470) verifies default parameters, asymmetry, capital caps, liquidity bounds, and parameter version determinism.

### five-minute-usdg-rule — PASS

5-minute USDG rule fires only on complete bars, suppresses risk-increasing candidates (PROPOSE/REBUILD), forces position reassessment via the structured position_reassessment field, and does not mutate global state. Incomplete bars never trigger the rule.

Evidence:

- compute_five_minute_return_q64_64 (adapter.py lines 180-255) computes return from the price series only when decision_time has crossed the bar boundary.
- AdaptiveMarketSnapshot.five_minute_return_q64_64 and five_minute_return_complete (lines 710-711) carry the return and the explicit completeness flag.
- _is_five_minute_rule_triggered (lines 1897-1911) returns True only when complete=True AND return > threshold.
- evaluate() lines 2102-2134: rule fires against held position → EXIT with EXTREME_UP_MOVE and position_reassessment=True; rule fires against empty ledger → NO_TRADE with EXTREME_UP_MOVE and position_reassessment=True.
- TestFiveMinuteUSDRule::test_complete_bar_above_100_pct_empty_ledger_no_risk_increase (lines 1047-1076) — empty ledger with complete bar returns NO_TRADE not PROPOSE.
- TestFiveMinuteUSDRule::test_complete_bar_above_100_pct_held_position_exits (lines 1077-1099) — held position returns EXIT with reassessment.
- TestFiveMinuteUSDRule::test_incomplete_bar_does_not_trigger_rule (lines 1101-1136) — incomplete bar with extreme return does NOT fire.
- TestFiveMinuteUSDRule::test_rule_does_not_mutate_global_state (lines 1138-1188) — two consecutive evaluations return equal candidates; fresh strategy instance returns equal candidate.
- The rule lives inside evaluate(); no module-level run-state flag is introduced. position_reassessment is a structured field on the candidate action.

### engine-callback-adapter-section-7 — PASS

Engine-callback adapter correctly projects engine events and ledger into T065 snapshots, runs the strategy, and converts the candidate action back to the engine's StrategyDecision. Both boundaries preserve their meaning.

Evidence:

- AdaptiveStrategyCallback (adapter.py lines 561-633) is a frozen dataclass with strategy, mapping, window_seconds fields and __call__(request) method.
- project_admission_snapshot, project_market_snapshot, project_portfolio_snapshot (adapter.py lines 297-467) project the engine request into T065 snapshots without mutation.
- _to_engine_decision (adapter.py lines 494-558) converts AdaptiveCandidateAction to engine StrategyDecision without changing either boundary.
- ADAPTIVE_TO_ENGINE_KIND mapping (adapter.py lines 488-496) maps lifecycle kinds: NO_TRADE→NO_TRADE, WAIT/WAIT_OUT/RETURN/REBUILD_DEFER→WAIT, PROPOSE/REBUILD/EXIT→PROPOSE.
- TestEngineCallbackAdapter::test_callback_returns_engine_strategy_decision (lines 1252-1269) confirms the adapter returns a StrategyDecision with correct engine-level kind.
- TestEngineCallbackAdapter::test_kind_mapping_table_is_complete (lines 1282-1285) confirms every AdaptiveCandidateKind maps to an engine kind.
- AST analysis of src/robinhood_lp/strategy/adapter.py imports: only __future__, collections.abc, dataclasses, math, robinhood_lp.protocol.contracts, robinhood_lp.strategy.adaptive, typing. No forbidden sibling imports.

### golden-scenarios-coverage — FAIL

7 of 8 golden scenarios are fully covered. The 'out-of-Range wait/return' scenario is partially covered: WAIT_OUT is tested, REBUILD is tested, but the return transition (held position moves from out-of-range back to in-range) is not explicitly tested. This gap is coupled to the lifecycle gap above.

Evidence:

- TestGoldenStableRange (lines 608-649): empty-ledger PROPOSE, held in-range WAIT — covered.
- TestGoldenFallingToken (lines 657-700): empty NO_TRADE, held WAIT_OUT — covered.
- TestGoldenJumps (lines 708-793): downward jump NO_TRADE, upside-asymmetry check — covered.
- TestGoldenLowVolume (lines 801-868): wash-like volume NO_TRADE, insufficient samples NO_TRADE — covered.
- TestGoldenLiquidityWithdrawal (lines 876-907): sustained drop NO_TRADE — covered.
- TestGoldenOutOfRangeLifecycle (lines 915-989): WAIT_OUT and REBUILD — partial. The class docstring claims 'WAIT_OUT, RETURN, REBUILD' coverage but no RETURN test exists.
- TestGoldenCostlyRebuild (lines 997-1036): REBUILD_DEFER — covered.
- Acceptance contract (T065.md line 32) requires 'out-of-Range wait/return and costly rebuild' — the 'return' scenario lacks a dedicated test.

### every-decision-replays-from-manifest — PASS

Every decision replays from its manifest. The strategy is a pure function of its inputs; the adapter sorts events by timestamp before processing; the engine produces identical results on identical inputs.

Evidence:

- _data_price_series, _data_volume_series, _data_liquidity_series (lines 1206-1284) are pure deterministic functions with sorted output.
- _compute_returns_q64_64 (lines 1343-1368) is deterministic given the series.
- TestReplayDeterminism::test_two_evaluations_with_same_inputs_are_equal (lines 1477-1498) — two calls produce hash-equal candidates.
- TestReplayDeterminism::test_callback_returns_equal_decisions (lines 1500-1513) — adapter produces equal StrategyDecisions.
- TestReplayDeterminism::test_events_in_different_input_orders_produce_same_decision (lines 1515-1536) — input order does not affect output (series is sorted inside the adapter).
- TestEngineIntegrationReplay::test_engine_replay_same_manifest_same_result (lines 1348-1370) — engine.run(events) called twice produces identical manifest_hash, bundle_hash, final_ledger, and audit_events.

### must-not-violations — PASS

All five Must-not clauses are satisfied. The strategy never depends on token appreciation, never accepts fundamentals/sentiment, never equates low liquidity with opportunity, never forces a trade, and never bypasses risk.

Evidence:

- No token appreciation: UP_TREND regime emits NO_TRADE for empty ledger (TestMustNot::test_up_trend_does_not_open_for_empty_ledger lines 1548-1584).
- No fundamentals/sentiment: AdaptiveMarketSnapshot has no field for fundamentals/sentiment; regime model only reads price/volume/liquidity evidence.
- No equating low liquidity with opportunity: RuleBasedAdaptiveFeeOpportunityModel downgrades to UNCERTAIN when own_share > max_own_liquidity_share_q64_64 (line 1624) or wash-like volume (line 1600).
- No forcing a trade: TestMustNot::test_no_data_emits_no_trade (lines 1586-1599) — empty events return NO_TRADE; the strategy's default is to hold/wait.
- No bypassing risk: AdaptiveCandidateAction carries structured reason_code and versions; the central risk layer is upstream of the engine pipeline. TestMustNot::test_not_admitted_emits_no_trade (lines 1601-1624) — non-admitted pool returns NO_TRADE.

### ruff-check — PASS

ruff lint passes on all three files.

Evidence:

- ruff check src/robinhood_lp/strategy/adaptive.py src/robinhood_lp/strategy/adapter.py tests/test_strategy_t065.py → 'All checks passed!'

### ruff-format — FAIL

ruff format --check fails on src/robinhood_lp/strategy/adaptive.py. The developer's evidence claim of '3 files reformatted' is inconsistent with the current file state.

Evidence:

- ruff format --check src/robinhood_lp/strategy/adaptive.py src/robinhood_lp/strategy/adapter.py tests/test_strategy_t065.py → '1 file would be reformatted, 2 files already formatted' (adaptive.py line 765).
- Diff: the multi-line 'if self.five_minute_return_complete and (self.is_relative_only or self.quote_q64_64 is None)' at line 765 should be re-formatted onto fewer lines per current ruff configuration.
- Developer evidence (todo/evidence/P06/T065/attempt-001-developer.json) claims '3 files reformatted (idempotent on re-run)' — this contradicts the current state of the file.

### mypy-strict — PASS

mypy --strict passes on all three files.

Evidence:

- mypy --strict src/robinhood_lp/strategy/adaptive.py src/robinhood_lp/strategy/adapter.py tests/test_strategy_t065.py → 'Success: no issues found in 3 source files'.

### pytest-t065 — PASS

All 53 T065 tests pass.

Evidence:

- pytest tests/test_strategy_t065.py -q --no-header → '53 passed in 0.22s'.

### pytest-broad-surface — PASS

All 364 tests in the broader T060/T061/T062/T063/T064/T065 surface pass.

Evidence:

- pytest tests/test_strategy_t060.py tests/test_strategy_t062.py tests/test_backtest_t061.py tests/test_strategy_t065.py tests/test_robustness_t064.py tests/test_reports_t063.py tests/test_import_graph.py tests/test_layer_direction_t007.py --no-header -q → '364 passed in 1.38s'.

### layer-purity — PASS

Layer purity holds by AST analysis. The assert_adaptive_strategy_layer_is_pure() walker only inspects vars() of the module, which catches `import X` but not `from X import Y`; however, AST analysis confirms no forbidden imports exist.

Evidence:

- assert_adaptive_strategy_layer_is_pure() passes (TestLayerPurity::test_adaptive_strategy_layer_is_pure line 410).
- AST analysis of src/robinhood_lp/strategy/adaptive.py: imports are __future__, collections.abc, dataclasses, enum, hashlib, importlib, math, robinhood_lp.protocol.contracts, robinhood_lp.protocol.math, types, typing. No forbidden modules (robinhood_lp.backtest, .config, .discovery, .features, .ingestion, .presentation, .qualification, .quality, .replay, .risk, .rpc, .signer, .storage, .strategy.baselines, .strategy.engine_adapter, .execution).
- AST analysis of src/robinhood_lp/strategy/adapter.py: imports are __future__, collections.abc, dataclasses, math, robinhood_lp.protocol.contracts, robinhood_lp.strategy.adaptive, typing. No forbidden modules.

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- Make AdaptiveStrategy.evaluate() (or _range_path) emit AdaptiveCandidateKind.RETURN (via self._return(...)) when a held position transitions from out-of-range to in-range. The current code emits WAIT with notes=('IN_RANGE',) for an in-range held position (lines 2335-2348). The contract requires the lifecycle action RETURN to be reachable from evaluate().
- Add a test in TestGoldenOutOfRangeLifecycle that exercises the return transition: build events where the price moves out of range, then re-enters; verify the strategy emits AdaptiveCandidateKind.RETURN. The class docstring already claims this coverage but no such test exists.
- Re-run ruff format on src/robinhood_lp/strategy/adaptive.py so the file matches the project's formatter configuration (line 765 multi-line 'if' should be reformatted). Update the developer evidence to reflect the actual file state.

## Residual risks

- The 5-minute USDG return depends on visible events carrying a USDG-converted price_q64_64; production requires the T053 qualified USDG quote source to feed the ingest pipeline (out of T065 scope).
- Default thresholds (5% down-trend, 10%/25% asymmetric jumps, 60% range occupancy, 5 sample minimum, 25% own-share maximum, 1.0 rebuild-cost ratio) are placeholders; they are explicit and reversible, no production default is committed.
- Two pre-existing test failures in the repository (unrelated to T065) — tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output and tests/test_documentation_citations.py::test_check_passes_on_real_repository — were present on the base commit and are outside T065's scope.
