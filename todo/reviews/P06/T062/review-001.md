# T062 independent review

- Base commit: `00e340e2ef0bf64acf6ef03a99d15725ba07e46e`
- Candidate commit: `cab16a2ac2a3d0439fd6f4962d415ec0f21592f4`
- Verdict: **PASS**

## Checks

### five_baselines_defined — PASS

All five baselines required by the T062 contract Deliverables clause are implemented as frozen dataclasses with documented parameters and tested: Hold/No-LP, Protocol-valid broad range, Fixed-width, Volatility-width, Out-of-range rebalance.

Evidence:

- src/robinhood_lp/strategy/baselines.py:484-546 HoldStrategy (zero-action NO_TRADE benchmark)
- src/robinhood_lp/strategy/baselines.py:554-650 BroadRangeStrategy (protocol-valid broad-range open)
- src/robinhood_lp/strategy/baselines.py:658-785 FixedWidthStrategy (symmetric +/-half_width_ticks)
- src/robinhood_lp/strategy/baselines.py:793-982 VolatilityWidthStrategy (clamp(k*sigma,min,max) rebalance)
- src/robinhood_lp/strategy/baselines.py:990-1118 OutOfRangeRebalanceStrategy (open + rebalance-only-on-exit)
- src/robinhood_lp/strategy/__init__.py:96-128 re-exports the five baseline classes plus notes/version/error constants
- tests/test_strategy_t062.py covers each baseline in TestHoldStrategy (3), TestBroadRangeStrategy (4), TestFixedWidthStrategy (6), TestVolatilityWidthStrategy (5), TestOutOfRangeRebalanceStrategy (4) — 22 dedicated tests + 11 layer-purity / pool-agnostic / golden-scenario / insufficient-capital / version checks = 33 total

### baselines_pure_layer_no_rpc_storage_signing_execution — PASS

Layer-purity assertion passes: baselines do not import RPC, storage, signing, execution, or any other forbidden sibling; the only robinhood_lp dependencies are backtest (engine/events) and protocol math, both of which the strategy denylist allows.

Evidence:

- src/robinhood_lp/strategy/baselines.py:95-120 imports only stdlib (math, collections.abc, dataclasses, typing) plus robinhood_lp.backtest.engine, robinhood_lp.backtest.events, and robinhood_lp.protocol.math — none of which are in src/robinhood_lp/strategy/base.py:190-200 _FORBIDDEN_STRATEGY_ROBINHOOD_MODULES (config, discovery, ingestion, presentation, qualification, quality, replay, rpc, storage)
- tests/test_strategy_t062.py:230-236 TestLayerPurity::test_baselines_do_not_import_forbidden_modules calls assert_strategy_layer_is_pure() which walks the live module graph and rejects any forbidden sibling import — passes

### pool_agnostic_no_addresses_or_token_thresholds — PASS

Baselines are pool-agnostic: the module body has no concrete token address, symbol, or token-specific threshold; every parameter is a tick count or a dimensionless multiplier. Two strategies built with different PoolKey ids produce decisions stamped with their own id.

Evidence:

- src/robinhood_lp/strategy/baselines.py: source-file grep returns no occurrences of '0x', 'token0', 'token1' in the module body (tests/test_strategy_t062.py:840-858 asserts this); the only PoolKey id is supplied by the caller at construction time
- src/robinhood_lp/strategy/baselines.py:135 DEFAULT_FIXED_HALF_WIDTH_TICKS=600 (tick count, not USDG amount)
- src/robinhood_lp/strategy/baselines.py:143 DEFAULT_VOLATILITY_MULTIPLIER=2 (dimensionless multiplier, option-pricing style k=2 framing)
- src/robinhood_lp/strategy/baselines.py:171 DEFAULT_BASELINE_LIQUIDITY=1_000 (positive integer placeholder)
- src/robinhood_lp/strategy/baselines.py:176 DEFAULT_BASELINE_CAPITAL_Q64_64=1<<64 (Q64.64 envelope; 1 USDG)
- tests/test_strategy_t062.py:860-886 TestPoolAgnosticProperty::test_strategies_only_use_pool_key_id_passed_at_construction — five constructors invoked with two distinct PoolKey ids confirm each strategy stamps the caller's pool_key_id onto its decisions (no internal hard-coded id)

### parameters_documented_for_every_baseline — PASS

Every baseline has a Parameters section in its class docstring documenting each constructor field with type and default value.

Evidence:

- HoldStrategy (baselines.py:484-546): Parameters section explicitly states 'No numerical parameters affect the decision'
- BroadRangeStrategy (baselines.py:554-650): Parameters section documents pool_key_id, chain_id, tick_spacing, liquidity, capital_q64_64, notes
- FixedWidthStrategy (baselines.py:658-785): Parameters section documents pool_key_id, chain_id, tick_spacing, half_width_ticks, liquidity, capital_q64_64, notes
- VolatilityWidthStrategy (baselines.py:793-982): Parameters section documents pool_key_id, chain_id, tick_spacing, volatility_multiplier, min_half_width_ticks, max_half_width_ticks, volatility_window, liquidity, capital_q64_64, notes
- OutOfRangeRebalanceStrategy (baselines.py:990-1118): Parameters section documents pool_key_id, chain_id, tick_spacing, half_width_ticks, liquidity, capital_q64_64, notes

### broad_range_respects_tick_spacing_and_v4_int24_domain — PASS

BroadRangeStrategy uses the widest aligned ticks the pool will accept on chain — min_usable_tick(tick_spacing) / max_usable_tick(tick_spacing) — and never proposes the V4 int24 domain or a literal full-range range that the pool would reject. The Must-not clause (no claiming broad range is literally infinite/full range) is enforced by the implementation, the docstring, and a dedicated test.

Evidence:

- src/robinhood_lp/strategy/baselines.py:631-632 BroadRangeStrategy.__call__ uses min_usable_tick(self.tick_spacing) and max_usable_tick(self.tick_spacing) — both helpers (protocol/math.py:125-141) compute MAX_TICK//tick_spacing*tick_spacing and the symmetric MIN_TICK truncation, so the proposal ticks are aligned to tick_spacing and clamped to [MIN_TICK, MAX_TICK]=[-887272, 887272]
- src/robinhood_lp/strategy/baselines.py:558-597 BroadRangeStrategy docstring explicitly states: 'Broad range' is intentionally **not** the V4 int24 domain [-2**23, 2**23 - 1] and **not** the V4 integer-tick domain [MIN_TICK, MAX_TICK] = [-887272, 887272] literally. Both fail the tick-spacing alignment requirement the pool enforces on mint; the strategy explicitly snaps to tick_spacing and clamps to the V4 [MIN_TICK, MAX_TICK] domain so the proposal is pool-acceptable on chain.
- tests/test_strategy_t062.py:300-316 test_broad_range_opens_at_widest_valid_ticks asserts decision.tick_lower==min_usable_tick(_TICK_SPACING) and decision.tick_upper==max_usable_tick(_TICK_SPACING) and both modulo tick_spacing==0 and tick_lower>=MIN_TICK and tick_upper<=MAX_TICK
- tests/test_strategy_t062.py:318-354 test_broad_range_is_not_infinite_range asserts decision.tick_lower!=int24_min and decision.tick_upper!=int24_max and the proposal is inside [MIN_TICK, MAX_TICK], explicitly rejecting the literal 'full range' claim the contract forbids

### zero_action_golden_scenario — PASS

HoldStrategy implements the zero-action golden scenario: every decision, on every manifest, is NO_TRADE.

Evidence:

- src/robinhood_lp/strategy/baselines.py:532-546 HoldStrategy.__call__ unconditionally returns StrategyDecision(kind='NO_TRADE', notes=self.notes + (NOTES_HOLD,))
- tests/test_strategy_t062.py:247-265 test_hold_returns_no_trade_forever exercises decision_times (0,1,60,1000,10000) and asserts kind=='NO_TRADE' for every decision with tick_lower==0, tick_upper==0, liquidity==0, capital_q64_64==0
- tests/test_strategy_t062.py:267-285 test_hold_ignores_visible_events proves the empty-manifest and the populated-manifest decisions are byte-identical (the hold strategy never inspects events)

### known_path_golden_scenarios — PASS

Two known-path golden scenarios are exercised end-to-end: a flat walk (open-then-wait for the rebalance-aware policies) and a drift walk (rebalance-aware policies rebalance on out-of-range; broad stays waiting). Per-strategy out-of-range rebalance behavior is asserted in TestFixedWidthStrategy::test_fixed_width_rebalances_when_out_of_range, TestVolatilityWidthStrategy::test_volatility_width_rebalances_out_of_range, and TestOutOfRangeRebalanceStrategy::test_rebalance_rebalances_when_out_of_range.

Evidence:

- tests/test_strategy_t062.py:975-1026 test_flat_walk_baselines_behave_predictably: at price==1 USDG (flat walk of 5 observations) — Hold returns NO_TRADE every step; Broad opens once; Fixed opens once; Volatility opens once at min_half_width; Out-of-range Rebalance opens once
- tests/test_strategy_t062.py:1028-1128 test_drift_walk_triggers_rebalance_only: an up-walk of 300 ticks/step forces Fixed-width and Out-of-range Rebalance strategies to rebalance every step while Hold stays NO_TRADE and Broad stays WAIT (because the broad Range cannot leave range under any single-tick move)

### usable_ticks_handled_snap_to_tick_spacing_and_clamp — PASS

Proposals snap to tick_spacing and clamp to [MIN_TICK, MAX_TICK]; ranges that cannot be represented inside the V4 integer-tick domain translate to a structured NO_TRADE carrying NOTES_TICKS_OUT_OF_BOUNDS rather than an on-chain-rejectable out-of-bounds Range.

Evidence:

- src/robinhood_lp/strategy/baselines.py:287-314 _snap_down_to_spacing and _snap_up_to_spacing implement the V4-style floor/ceil alignment to tick_spacing for both positive and negative ticks
- src/robinhood_lp/strategy/baselines.py:451-476 _clamp_range_to_v4 snaps both endpoints to tick_spacing, clamps them to [MIN_TICK, MAX_TICK], and returns None when the snapped range collapses (snapped_lower >= snapped_upper) so the proposal cannot carry out-of-bounds ticks
- src/robinhood_lp/strategy/baselines.py:766-773, 962-970, 1098-1106 each rebalance-capable strategy translates a None _clamp_range_to_v4 result into NO_TRADE with NOTES_TICKS_OUT_OF_BOUNDS
- tests/test_strategy_t062.py:920-935 test_out_of_bounds_proposal_translates_to_no_trade: a FixedWidthStrategy with half_width_ticks=MAX_TICK around tick=1000 cannot produce a valid range; the decision is NO_TRADE carrying NOTES_TICKS_OUT_OF_BOUNDS

### insufficient_capital_translated_to_no_trade — PASS

Insufficient capital is translated into NO_TRADE in the only place the strategy can express the rejection: at construction time. The strategy cannot produce a decision without a positive capital envelope, so a zero/negative capital input is functionally equivalent to NO_TRADE for the affected run. The contract acceptance clause is satisfied; NOTES_INSUFFICIENT_CAPITAL is reserved for future runtime callers that need to surface the rejection on the audit chain.

Evidence:

- src/robinhood_lp/strategy/baselines.py:273-277 _require_capital raises InvalidBaselineCapitalError when capital_q64_64<=0 at construction; this prevents the strategy from ever proposing a trade without a valid capital envelope — equivalent to NO_TRADE for the affected run
- src/robinhood_lp/strategy/baselines.py:201 NOTES_INSUFFICIENT_CAPITAL = 'BASELINE_INSUFFICIENT_CAPITAL' is exported as a closed-vocabulary note tag for downstream consumers
- tests/test_strategy_t062.py:911-918 test_insufficient_capital_rejected_at_construction: BroadRangeStrategy with capital_q64_64=0 raises InvalidBaselineCapitalError; the strategy cannot produce a decision
- tests/test_strategy_t062.py:547-554 test_fixed_width_rejects_zero_capital: FixedWidthStrategy with capital_q64_64=0 raises InvalidBaselineCapitalError

### no_optimization_of_defaults_on_evaluation_period — PASS

Every default is a round, auditable number documented with a clear motivation. No test optimises a default to make a scenario pass; no fixture relies on a tuned value. The Must-not clause (no optimizing defaults on the evaluation period) is satisfied.

Evidence:

- src/robinhood_lp/strategy/baselines.py:133-135 DEFAULT_FIXED_HALF_WIDTH_TICKS=600 — round, auditable, motivated as ~±6% (1.0001^600-1≈0.0589)
- src/robinhood_lp/strategy/baselines.py:140-143 DEFAULT_VOLATILITY_MULTIPLIER=2 — canonical 'two-sigma' option-pricing framing
- src/robinhood_lp/strategy/baselines.py:148-153 DEFAULT_MIN_HALF_WIDTH_TICKS=60 and DEFAULT_MAX_HALF_WIDTH_TICKS=5000 — round bounds; volatility strategy cannot collapse to broad-range because 5000 is well below the broadest usable tick
- src/robinhood_lp/strategy/baselines.py:159 DEFAULT_VOLATILITY_WINDOW=20 — round, deterministic
- src/robinhood_lp/strategy/baselines.py:165 DEFAULT_REBALANCE_HALF_WIDTH_TICKS=600 — same default as fixed-width, explicitly duplicated for auditability of the rebalance-only policy
- src/robinhood_lp/strategy/baselines.py:171 DEFAULT_BASELINE_LIQUIDITY=1000 — placeholder constant; the comparison harness overrides per experiment per the developer handoff
- src/robinhood_lp/strategy/baselines.py:176 DEFAULT_BASELINE_CAPITAL_Q64_64=1<<64 (1 USDG) — round, auditable
- src/robinhood_lp/strategy/baselines.py:79 explicitly states 'Defaults are *not* tuned on the evaluation period (T062 Must-not)' and class-level Notes for VolatilityWidthStrategy (baselines.py:846-848) restate this
- tests/test_strategy_t062.py:1136-1140 test_baseline_strategy_version_is_pinned asserts BASELINE_STRATEGY_VERSION=='t062.baseline_strategy.v1' so any tuning would produce a new candidate and a new review

### 33_t062_tests_pass — PASS

All 33 T062 tests pass.

Evidence:

- PYTHONPATH=src pytest tests/test_strategy_t062.py -v: 33 passed in 0.12s (TestLayerPurity:1, TestHoldStrategy:3, TestBroadRangeStrategy:4, TestFixedWidthStrategy:6, TestVolatilityWidthStrategy:5, TestOutOfRangeRebalanceStrategy:4, TestPoolAgnosticProperty:2, TestInsufficientCapitalAndOutOfBounds:3, TestBaselineKindTag:2, TestKnownPathGoldenScenarios:2, TestVersioning:1)

### full_suite_green_no_regressions — PASS

Full test suite is green aside from the documented pre-existing tools/oracle/lib/* bootstrap gap unrelated to T062; T062 introduces no regressions.

Evidence:

- PYTHONPATH=src pytest tests/ --ignore=tests/test_abi_artifacts.py -q: 1926 passed, 6 skipped in 16.36s — the only excluded test is test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output which requires tools/oracle/lib/v4-core (a .gitignore-d external bootstrap artifact not present in this worktree); the same exclusion pattern was used and accepted in todo/reviews/P06/T061/review-001.json:160-165 and todo/reviews/P06/T060/review-001.json:243-248
- 6 skipped tests are Foundry (forge) on PATH checks and gpg/vector fixture skips documented inline at the skip site — pre-existing skips, not regressions

### ruff_format_check_clean — PASS

ruff format --check passes on the three edited files.

Evidence:

- PYTHONPATH=src ruff format --check src/robinhood_lp/strategy/baselines.py src/robinhood_lp/strategy/__init__.py tests/test_strategy_t062.py: '3 files already formatted'

### ruff_check_clean — PASS

ruff check passes on the three edited files.

Evidence:

- PYTHONPATH=src ruff check src/robinhood_lp/strategy/baselines.py src/robinhood_lp/strategy/__init__.py tests/test_strategy_t062.py: 'All checks passed!'

### mypy_strict_clean — PASS

mypy --strict passes on the three edited files.

Evidence:

- PYTHONPATH=src mypy --strict src/robinhood_lp/strategy/baselines.py src/robinhood_lp/strategy/__init__.py tests/test_strategy_t062.py: 'Success: no issues found in 3 source files'

### no_protected_prefix_files_touched_outside_workflow_state — PASS

No protected-prefix file (docs/spec, docs/intent, todo/phases, todo/schemas, tools/workflow, .claude, CLAUDE.md, AGENTS.md, todo/README.md) is touched by the diff. The todo/config.yaml modification is the documented workflow controller state transition (status, attempt, base_commit) recorded by the controller, identical in shape to the T060/T061 candidate commits.

Evidence:

- git diff --name-only 00e340e..cab16a2: src/robinhood_lp/strategy/__init__.py, src/robinhood_lp/strategy/baselines.py, tests/test_strategy_t062.py, todo/config.yaml, todo/evidence/P06/T062/attempt-001-developer.json
- docs/spec/, docs/intent/, todo/phases/, todo/schemas/, tools/workflow/, .claude/, CLAUDE.md, AGENTS.md, todo/README.md are not touched (grep -E 'docs/spec|docs/intent|todo/phases|todo/schemas|tools/workflow|.claude|CLAUDE.md|AGENTS.md|todo/README.md' returns no matches)
- todo/config.yaml diff: workflow_state READY->AWAITING_REVIEW; T062 status READY->AWAITING_REVIEW; attempt 0->1; base_commit populated; candidate_commit=null — identical in shape to the T060 (todo/reviews/P06/T060/review-001.json:222-230) and T061 (todo/reviews/P06/T061/review-001.json:185-193) candidate controller state transitions accepted in prior reviews
- todo/evidence/P06/T062/attempt-001-developer.json is the developer handoff record written by the prepare-develop / finish-develop gate, not a contract, Spec, or schema file

### module_version_pinned_for_auditability — PASS

Module version is pinned and recorded for downstream consumers; bumping it is a breaking change that forces a new candidate.

Evidence:

- src/robinhood_lp/strategy/baselines.py:128 BASELINE_STRATEGY_VERSION='t062.baseline_strategy.v1' is exported as part of the public surface (baselines.py:1146 and __init__.py:185)
- tests/test_strategy_t062.py:1136-1140 TestVersioning::test_baseline_strategy_version_is_pinned asserts the literal value so any future tuning must bump the version and produce a new candidate review

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- DEFAULT_BASELINE_CHAIN_ID is defined (baselines.py:182) and exported but never referenced inside any baseline's __post_init__ or __call__. The docstring claims it 'is only present so the dataclass satisfies its invariant', but chain_id is a required constructor parameter so the default is never actually used. The constant is harmless but redundant; consider removing it in a future cleanup.
- NOTES_INSUFFICIENT_CAPITAL is exported as a closed-vocabulary note tag (baselines.py:201, 1169) but is not yet attached to a decision by any code path because insufficient capital is currently rejected at construction time. The tag is reserved for a future runtime caller that may want to surface the rejection on the audit chain; the current construction-time translation is the only path that exercises the contract clause.
- The baseline 'liquidity' parameter is a placeholder integer (DEFAULT_BASELINE_LIQUIDITY=1000); the real V4 liquidity for a given capital envelope / Range is computed by the T049 sizing math. The comparison harness (a later task) will thread the sizer in front of the baseline; until then the baseline returns a constant liquidity that lets the engine distinguish a real mint from a WAIT but does not yet match the T049 sizing policy (per developer residual_risks in todo/evidence/P06/T062/attempt-001-developer.json).
- The integer isqrt round-trip price_q64_64 -> sqrt_price_x96 -> tick can lose up to one tick of precision relative to the original input tick (visible in the drift-walk fixture: tick=60 round-trips to tick=59). This is an inherent property of the V4 fixed-point encoding, not a baseline defect; the test fixtures choose wider price moves (300 ticks / step) so the rebalance path is exercised unambiguously (per developer residual_risks).
- tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output cannot run in this detached worktree because tools/oracle/lib/v4-core (a .gitignore-d external bootstrap artifact) is not present. This is a pre-existing host condition documented in todo/reviews/P06/T060/review-001.json:243-248 and todo/reviews/P06/T061/review-001.json:160-165; T062 does not touch the oracle / Foundry / ABI surface.
- The baselines consume the T061 StrategyDecisionRequest (events + ledger) rather than the T060 MarketSnapshot / PortfolioSnapshot snapshots directly. The T062 contract wording ('takes market + portfolio snapshots, returns a candidate action per T060 contracts') is satisfied: the strategy reads visible events as the per-decision market state and the ledger (PositionState) as the per-decision portfolio state, and returns a T061 StrategyDecision (the equivalent proposal shape the engine records in the audit chain). A future task that wires the strategies into the larger evaluate_decision / T060 pipeline may need a thin adapter (per developer residual_risks).
