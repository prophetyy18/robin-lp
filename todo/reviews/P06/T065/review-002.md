# T065 independent review

- Base commit: `97d83bf0eca4415044551673b7d61fb73f254b12`
- Candidate commit: `2cb05735414abdaffc2714d95f21214226a99a36`
- Verdict: **PASS**

## Checks

### required-change-1-return-emission — PASS

Required change 1 is satisfied. AdaptiveStrategy._range_path() now emits AdaptiveCandidateKind.RETURN via self._return(...) when a held position transitions from out-of-range back to in-range. The transition is detected from the portfolio snapshot's in_range flag, which mirrors the engine ledger (adapter.py line 443).

Evidence:

- src/robinhood_lp/strategy/adaptive.py line 2342-2353 — _range_path() now branches on portfolio.in_range when current tick is inside the Range; if portfolio.in_range is False (the engine-recorded out-of-range flag) it returns self._return(...) which builds a candidate with kind=AdaptiveCandidateKind.RETURN and notes=('RETURN_TO_RANGE',).
- grep -n 'self._return' src/robinhood_lp/strategy/adaptive.py → exactly one call site at line 2343.
- _return() helper at lines 2672-2701 constructs AdaptiveCandidateAction with kind=AdaptiveCandidateKind.RETURN, cost_ratio_q64_64=0, position_reassessment=False, notes=self.notes + ('RETURN_TO_RANGE',).
- Direct probe: build PositionState with in_range=False, current tick inside [-600, 600], run strategy.evaluate(...); candidate.kind == AdaptiveCandidateKind.RETURN, 'RETURN_TO_RANGE' in candidate.notes, regime_state == 'RANGE', tick_lower==tick_upper==0, liquidity==0, capital_q64_64==0.

### required-change-2-return-test — PASS

Required change 2 is satisfied. TestGoldenOutOfRangeLifecycle now contains a return-transition test that builds events for a held position leaving then re-entering the Range and asserts the strategy emits AdaptiveCandidateKind.RETURN with the expected notes.

Evidence:

- tests/test_strategy_t065.py lines 996-1032 — test_return_to_range_after_out_of_range_emits_return exists inside class TestGoldenOutOfRangeLifecycle.
- Test calls _default_strategy(max_rebuild_wait_seconds=1_800), _stable_events(n=20, tick=0, active_liquidity=10_000), _held_position(tick_lower=-600, tick_upper=600, last_accrual_time=5*60, in_range=False) and asserts candidate.kind == AdaptiveCandidateKind.RETURN, 'RETURN_TO_RANGE' in candidate.notes, regime_state == 'RANGE', tick_lower==tick_upper==0, liquidity==0, capital_q64_64==0.
- pytest tests/test_strategy_t065.py::TestGoldenOutOfRangeLifecycle -v → 3 passed: test_out_of_range_within_wait_bound_waits_out, test_out_of_range_beyond_wait_bound_rebuilds, test_return_to_range_after_out_of_range_emits_return.

### required-change-3-ruff-format — PASS

Required change 3 is satisfied. ruff format --check passes on all three files. The previously unformatted multi-line 'if' at adaptive.py:765 has been collapsed.

Evidence:

- ruff format --check src/robinhood_lp/strategy/adaptive.py src/robinhood_lp/strategy/adapter.py tests/test_strategy_t065.py → '3 files already formatted'.
- Diff vs base confirms adaptive.py line 765 multi-line 'if' was collapsed to the formatter's preferred layout (one-line 'if self.five_minute_return_complete and (self.is_relative_only or self.quote_q64_64 is None):').
- Developer evidence (attempt-002-developer.json) records the actual '1 file reformatted, 2 files left unchanged' result and is consistent with the current file state.

### complete-out-of-range-lifecycle-actions — PASS

Complete out-of-Range lifecycle is now reachable from evaluate(). Every contract-required kind (WAIT_OUT, RETURN, REBUILD, REBUILD_DEFER, EXIT) is emitted.

Evidence:

- AdaptiveCandidateKind enum (lines 290-329) declares NO_TRADE, WAIT, PROPOSE, WAIT_OUT, RETURN, REBUILD, REBUILD_DEFER, EXIT.
- evaluate() and _range_path() emit sites: WAIT_OUT (lines 2087, 2167, 2206, 2371); RETURN (line 2343); REBUILD (lines 2237, 2463); REBUILD_DEFER (lines 2225, 2390); EXIT (line 2104). All five contract-required lifecycle kinds are reachable.
- Adapter mapping ADAPTIVE_TO_ENGINE_KIND covers every AdaptiveCandidateKind, including RETURN → 'WAIT' (adapter.py).

### five-minute-usdg-rule — PASS

5-minute USDG rule still fires only on complete bars, suppresses risk-increasing candidates (PROPOSE/REBUILD), forces position reassessment via the structured position_reassessment field, and does not mutate global state.

Evidence:

- compute_five_minute_return_q64_64 (adapter.py lines 180-255) computes return only on completed 5-minute bars.
- evaluate() lines 2102-2116: rule fires against a held position → EXIT with EXTREME_UP_MOVE and position_reassessment=True.
- evaluate() lines 2118-2133: rule fires against an empty ledger → NO_TRADE with EXTREME_UP_MOVE and position_reassessment=True.
- _is_five_minute_rule_triggered (lines 1897-1911) returns True only when complete=True AND return > threshold.
- TestFiveMinuteUSDRule::test_complete_bar_above_100_pct_empty_ledger_no_risk_increase — empty ledger with complete bar returns NO_TRADE not PROPOSE.
- TestFiveMinuteUSDRule::test_complete_bar_above_100_pct_held_position_exits — held position returns EXIT with reassessment.
- TestFiveMinuteUSDRule::test_incomplete_bar_does_not_trigger_rule — incomplete bar with extreme return does NOT fire.
- TestFiveMinuteUSDRule::test_rule_does_not_mutate_global_state — two consecutive evaluations return equal candidates.

### replaceable-regime-and-fee-models — PASS

Replaceable regime and fee-opportunity models remain correctly implemented. Stub injection only alters the assessment; candidate-action shape and audit chain are unchanged.

Evidence:

- src/robinhood_lp/strategy/adaptive.py lines 1133-1178 define runtime-checkable Protocols AdaptiveRegimeModel and AdaptiveFeeOpportunityModel with assess() method.
- RuleBasedAdaptiveRegimeModel and RuleBasedAdaptiveFeeOpportunityModel are the reference implementations.
- TestModelReplacement (tests/test_strategy_t065.py lines 1378-1466) verifies stub injection changes only the assessment, not the candidate-action shape or audit chain.
- pytest tests/test_strategy_t065.py::TestModelReplacement → 2 passed.

### downside-asymmetric-trend-jump-filter — PASS

Downside asymmetry is enforced both at parameter construction and at runtime evaluation.

Evidence:

- AdaptiveRangeParameters.__post_init__ (lines 535-550) enforces up_jump_threshold > down_jump_threshold and up_trend_threshold > down_trend_threshold.
- TestParameterSchema::test_downside_asymmetry_is_required confirms construction rejects equal and inverted thresholds.
- TestGoldenJumps::test_downward_jump_triggers_jump_down_risk confirms -15% bar fires JUMP_RISK.
- TestGoldenJumps::test_downside_asymmetry_smaller_threshold_for_downward confirms +15% bar does NOT fire JUMP_RISK.

### range-occupancy-break-return-assessment — PASS

Range occupancy / break / return assessment remains correct; RETURN is detected and emitted when portfolio.in_range is False but the current tick is inside the Range.

Evidence:

- AdaptiveRegimeAssessment.range_occupancy_q64_64 carries in-range fraction.
- RuleBasedAdaptiveRegimeModel.assess downgrades to UNCERTAIN when occupancy < range_occupancy_threshold_q64_64.
- TestGoldenStableRange (2 tests) and TestGoldenOutOfRangeLifecycle (3 tests) cover occupancy-driven RANGE classification and out-of-range lifecycle.
- RETURN detection now uses portfolio.in_range False with current tick inside Range.
- TestSnapshotInvariants verifies portfolio snapshot tick_lower < tick_upper invariant.

### expected-fee-depth-cost-distributions — PASS

Expected fee/depth/cost distributions are recorded on every decision through the assessment fields and the candidate action's cost_ratio_q64_64.

Evidence:

- AdaptiveFeeOpportunityAssessment carries expected_fee_edge_q64_64, expected_in_range_fraction_q64_64, projected_own_liquidity_share_q64_64, expected_cost_ratio_q64_64, sample_count.
- RuleBasedAdaptiveFeeOpportunityModel.assess projects these quantities from visible volume/liquidity evidence.
- AdaptiveCandidateAction.cost_ratio_q64_64 carries the cost ratio for REBUILD_DEFER candidates.
- _rebuild_defer helper populates cost_ratio_q64_64 from the fee assessment.

### immutable-parameter-schema-and-uncertainty-reason-codes — PASS

Immutable parameter schema with uncertainty/reason codes is correctly implemented.

Evidence:

- AdaptiveRangeParameters is a frozen dataclass with __post_init__ validation.
- AdaptiveReasonCode StrEnum declares 18 stable reason codes.
- uncertainty_codes fields on assessments carry structured uncertainty codes.
- _versioned_parameter_id produces a SHA-256 hex of canonical parameter serialisation for audit chain versioning.
- TestParameterSchema verifies defaults, asymmetry, capital caps, liquidity bounds, and parameter version determinism (6 tests).

### engine-callback-adapter-section-7 — PASS

Engine-callback adapter correctly projects engine events and ledger into T065 snapshots, runs the strategy, and converts the candidate action back to the engine's StrategyDecision. RETURN is correctly mapped to engine WAIT.

Evidence:

- AdaptiveStrategyCallback (adapter.py lines 561-633) is a frozen dataclass with strategy, mapping, window_seconds fields and __call__(request) method.
- project_admission_snapshot, project_market_snapshot, project_portfolio_snapshot project the engine request into T065 snapshots without mutation.
- _to_engine_decision converts AdaptiveCandidateAction to engine StrategyDecision without changing either boundary.
- ADAPTIVE_TO_ENGINE_KIND maps every lifecycle kind including RETURN → 'WAIT'.
- project_portfolio_snapshot passes through ledger.in_range so the RETURN transition is visible to the strategy when the engine records the flip.
- AST analysis of adapter.py: imports are __future__, collections.abc, dataclasses, math, robinhood_lp.protocol.contracts, robinhood_lp.strategy.adaptive, typing. No forbidden sibling imports.
- TestEngineCallbackAdapter::test_kind_mapping_table_is_complete confirms every AdaptiveCandidateKind maps to an engine kind.
- TestEngineIntegrationReplay::test_engine_replay_same_manifest_same_result confirms engine replay equivalence.

### golden-scenarios-coverage — PASS

All 8 golden scenarios are covered. The 'out-of-Range wait/return' gap from review-001 is closed.

Evidence:

- TestGoldenStableRange (2 tests): empty-ledger PROPOSE, held in-range WAIT.
- TestGoldenFallingToken (2 tests): empty NO_TRADE, held WAIT_OUT.
- TestGoldenJumps (2 tests): downward jump NO_TRADE, upside-asymmetry check.
- TestGoldenLowVolume (2 tests): wash-like volume NO_TRADE, insufficient samples NO_TRADE.
- TestGoldenLiquidityWithdrawal (1 test): sustained drop NO_TRADE.
- TestGoldenOutOfRangeLifecycle (3 tests): WAIT_OUT, REBUILD, RETURN — all contract-required lifecycle kinds now have direct coverage.
- TestGoldenCostlyRebuild (1 test): REBUILD_DEFER.
- Acceptance contract requires 'out-of-Range wait/return and costly rebuild' — RETURN is now explicitly tested.

### every-decision-replays-from-manifest — PASS

Every decision replays from its manifest. The strategy is a pure function of its inputs; the adapter sorts events by timestamp; the engine produces identical results on identical inputs.

Evidence:

- _data_price_series, _data_volume_series, _data_liquidity_series are pure deterministic functions with sorted output.
- _compute_returns_q64_64 is deterministic given the series.
- TestReplayDeterminism::test_two_evaluations_with_same_inputs_are_equal — two calls produce hash-equal candidates.
- TestReplayDeterminism::test_callback_returns_equal_decisions — adapter produces equal StrategyDecisions.
- TestReplayDeterminism::test_events_in_different_input_orders_produce_same_decision — input order does not affect output (series is sorted inside the adapter).
- TestEngineIntegrationReplay::test_engine_replay_same_manifest_same_result — engine.run(events) called twice produces identical manifest_hash, bundle_hash, final_ledger, audit_events.

### must-not-violations — PASS

All five Must-not clauses remain satisfied. No new forbidden behaviour introduced.

Evidence:

- No token appreciation: UP_TREND regime emits NO_TRADE for empty ledger (TestMustNot::test_up_trend_does_not_open_for_empty_ledger).
- No fundamentals/sentiment: AdaptiveMarketSnapshot has no field for fundamentals/sentiment; regime model only reads price/volume/liquidity evidence.
- No equating low liquidity with opportunity: RuleBasedAdaptiveFeeOpportunityModel downgrades to UNCERTAIN when own_share > max_own_liquidity_share_q64_64 or wash-like volume.
- No forcing a trade: TestMustNot::test_no_data_emits_no_trade — empty events return NO_TRADE.
- No bypassing risk: AdaptiveCandidateAction carries structured reason_code and versions; the central risk layer is upstream of the engine pipeline. TestMustNot::test_not_admitted_emits_no_trade — non-admitted pool returns NO_TRADE.

### ruff-check — PASS

ruff lint passes on all three files.

Evidence:

- ruff check src/robinhood_lp/strategy/adaptive.py src/robinhood_lp/strategy/adapter.py tests/test_strategy_t065.py → 'All checks passed!'.

### mypy-strict — PASS

mypy --strict passes on all three files.

Evidence:

- mypy --strict src/robinhood_lp/strategy/adaptive.py src/robinhood_lp/strategy/adapter.py tests/test_strategy_t065.py → 'Success: no issues found in 3 source files'.

### pytest-t065 — PASS

All 54 T065 tests pass.

Evidence:

- PYTHONPATH=src python -m pytest tests/test_strategy_t065.py -q --no-header → '54 passed in 0.16s' (was 53 in attempt-1; +1 from test_return_to_range_after_out_of_range_emits_return).

### pytest-broad-surface — PASS

All 365 tests in the broader T060/T061/T062/T063/T064/T065 surface pass.

Evidence:

- PYTHONPATH=src python -m pytest tests/test_strategy_t060.py tests/test_strategy_t062.py tests/test_backtest_t061.py tests/test_strategy_t065.py tests/test_robustness_t064.py tests/test_reports_t063.py tests/test_import_graph.py tests/test_layer_direction_t007.py --no-header -q → '365 passed in 1.25s'.

### layer-purity — PASS

Layer purity holds by AST analysis and the assert_adaptive_strategy_layer_is_pure() walker.

Evidence:

- assert_adaptive_strategy_layer_is_pure() runs cleanly (no forbidden sibling imports).
- AST analysis of adaptive.py: imports are __future__, math, collections.abc, dataclasses, enum, types, typing, robinhood_lp.protocol.contracts, robinhood_lp.protocol.math, hashlib, importlib. No forbidden modules (backtest, config, discovery, features, ingestion, presentation, qualification, quality, replay, risk, rpc, signer, storage, strategy.baselines, strategy.engine_adapter, execution).
- AST analysis of adapter.py: imports are __future__, math, collections.abc, dataclasses, typing, robinhood_lp.protocol.contracts, robinhood_lp.strategy.adaptive. No forbidden modules.

### no-unintended-tracked-changes — PASS

The candidate commit is well-formed: only T065's working surface changed, the expected files were touched, and HEAD matches.

Evidence:

- git diff --stat 97d83bf..2cb0573 shows changes in src/robinhood_lp/strategy/adapter.py, src/robinhood_lp/strategy/adaptive.py, tests/test_strategy_t065.py, todo/config.yaml, todo/evidence/P06/T065/attempt-001-developer.json, todo/evidence/P06/T065/attempt-002-developer.json, todo/reviews/P06/T065/review-001.json, todo/reviews/P06/T065/review-001.md.
- HEAD of review worktree is 2cb05735414abdaffc2714d95f21214226a99a36, matching candidate_commit.
- All changes are within T065's task-contract surface (src/strategy/, tests/, todo/).

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- The T061 backtest engine (src/robinhood_lp/backtest/engine.py line 688) does not yet write the in_range flag back into PositionState during the fill stage — only tick_lower/tick_upper/liquidity/last_accrual_time are written. The adapter (adapter.py line 443) passes ledger.in_range through to the portfolio snapshot as-is, so the strategy's RETURN detection depends on the engine materialising the flip in a future change. The new T065 test exercises the strategy path today by constructing in_range=False on the PositionState directly. Wiring the engine flip is owned by T061/T067 and is outside T065's surface; it must land before live execution for the RETURN lifecycle action to be reachable through a real engine run.
- Two pre-existing test failures in the repository (unrelated to T065) — tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output (missing v4-core Solidity source files in tools/oracle/lib) and tests/test_documentation_citations.py::test_check_passes_on_real_repository (doc cites storage.schema and backtest.manifest/backtest.robustness modules that do not resolve) — remain present on the base commit and are outside T065's scope.
- Default thresholds (5% down-trend, 10%/25% asymmetric jumps, 60% range occupancy, 5 sample minimum, 25% own-share maximum, 1.0 rebuild-cost ratio) are placeholders bound for the post-testnet paper/shadow evidence gate; they are explicit and reversible, no production default is committed by this attempt.
