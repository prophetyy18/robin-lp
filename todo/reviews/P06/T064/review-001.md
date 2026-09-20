# T064 independent review

- Base commit: `41bda04ac0a8e4f59863f6e56fc651f88a6fd994`
- Candidate commit: `137d496cbd8573ca8c281c561d6cf00bcfc37b80`
- Verdict: **PASS**

## Checks

### anchored_rolling_walk_forward_present — PASS

Both ANCHORED (train start held at 0, train end grows by fold_step) and ROLLING (train start slides by fold_step) windows are implemented in WalkForwardSplit with deterministic boundary records.

Evidence:

- src/robinhood_lp/robustness/splits.py:436-575 (WalkForwardSplit dataclass + build_walk_forward_splits)
- tests/test_robustness_t064.py:610-673 (TestWalkForwardSplit covers ANCHORED train-end growth and ROLLING train-start slide)

### untouched_test_holdout_present — PASS

TimeHoldoutSplit keeps the test fold strictly after validation and records four boundaries (train end, validation start/end, test start). The walk-forward path does not consume the untouched test holdout.

Evidence:

- src/robinhood_lp/robustness/splits.py:576-660 (TimeHoldoutSplit enforces test.anchor_start > validation.anchor_end and exposes test_is_untouched)
- tests/test_robustness_t064.py:486-531 (TestTimeHoldoutSplit verifies test strictly after validation and boundaries recorded per fold edge)

### parameter_surfaces_and_pool_regime_segmentation — PASS

ParameterSurface enumerates the deterministic cross-product of axes (int|str|bool). RegimeSegmentation enumerates segments with closed regime labels and rejects empty segmentations; ALL_REGIMES is reserved as a meta-label and cannot be assigned to a specific segment.

Evidence:

- src/robinhood_lp/robustness/surfaces.py:104-300 (ParameterAxis / ParameterSurface grid enumeration)
- src/robinhood_lp/robustness/surfaces.py:344-460 (RegimeSegment / RegimeSegmentation with VALID_REGIME_LABELS closed vocabulary)
- tests/test_robustness_t064.py:984-1104 (TestParameterSurface, TestRegimeSegmentation)

### scenario_catalogue_six_categories_with_declared_outcomes — PASS

Six categories (gas/latency/slippage/fee stress + missing-data + reorg) are covered. Each scenario declares a HALT reason code or DEGRADE fallback name at construction; the Scenario constructor enforces it via MissingScenarioOutcomeError.

Evidence:

- src/robinhood_lp/robustness/scenarios.py:62-76 (VALID_CATEGORIES = STRESSED_GAS, STRESSED_LATENCY, STRESSED_SLIPPAGE, STRESSED_FEE, MISSING_DATA, REORG)
- src/robinhood_lp/robustness/scenarios.py:362-460 (default_stress_scenario_catalogue declares one scenario per category with HALT reason codes or DEGRADE fallback name)
- tests/test_robustness_t064.py:768-803 (TestScenarioCatalogue verifies default catalogue covers all six categories, HALT scenarios have reason codes, DEGRADE scenarios have fallback names)

### multiple_comparison_disclosure_present — PASS

The MultipleComparisonDisclosure carries the number of comparisons, the family-wise correction method, the unadjusted vs adjusted significant counts, and the sensitivity spread beside the best metric value. Rejects adjusted > unadjusted.

Evidence:

- src/robinhood_lp/robustness/disclosure.py:84-200 (MultipleComparisonDisclosure with n_comparisons, n_families, adjustment_method, family_wise_alpha_q64_64, n_significant_unadjusted/adjusted, best_metric_value, sensitivity_spread_q64_64)
- tests/test_robustness_t064.py:1112-1165 (TestMultipleComparisonDisclosure verifies best+spread, rejects adjusted>unadjusted)

### pool_holdout_primary_generalisation — PASS

Pool holdout is the primary generalisation test: PRIMARY_GENERALISATION_AXIS is hard-coded to POOL_HOLDOUT, the report refuses an empty pool_holdout_results tuple, and the per-held-out-pool metric_name is cross-checked against sensitivity_summary. Time holdout and walk-forward are retained beside it.

Evidence:

- src/robinhood_lp/robustness/splits.py:698-780 (PoolHoldoutSplit with disjoint train/holdout pool sets; non-empty holdout_pool_folds)
- src/robinhood_lp/robustness/reports.py:74 (PRIMARY_GENERALISATION_AXIS = 'POOL_HOLDOUT'), 318-325 (rejects empty pool_holdout_results with MissingPrimaryGeneralisationError)
- tests/test_robustness_t064.py:681-757 (TestPoolHoldoutSplit rejects overlap, mismatched role; verifies per-pool boundaries and holdout_pool_ids in declaration order)

### single_embargo_derived_from_label_horizon — PASS

There is exactly one embargo length per split, read from LabelHorizon.purge_embargo_length, in the horizon's own unit (BARS or BLOCKS). WalkForwardSplit, TimeHoldoutSplit, and the RobustnessReport all reject a hand-chosen embargo that disagrees with the derived length; the embargo is recorded with each split and the boundary records carry the unit.

Evidence:

- src/robinhood_lp/robustness/splits.py:121-153 (LabelHorizon derives purge_embargo_length from horizon.value; the unit is the embargo unit)
- src/robinhood_lp/robustness/splits.py:455-475 (WalkForwardSplit.__post_init__ raises HandChosenEmbargoError if the validation gap is not equal to label_horizon.purge_embargo_length)
- src/robinhood_lp/robustness/splits.py:617-640 (TimeHoldoutSplit.__post_init__ checks both train->validation and validation->test gaps equal to the derived embargo length)
- src/robinhood_lp/robustness/reports.py:306-313 (report's purge_embargo_length property equals label_horizon.value)
- tests/test_robustness_t064.py:532-565 (TimeHoldoutSplit rejects gap=5 against horizon=12 via HandChosenEmbargoError)

### report_separates_train_validation_test_includes_sensitivity — PASS

Train / validation / test are three disjoint FoldResult tuples in the report; segment_label overlap is rejected. SensitivitySummary is required; the report also checks that pool_holdout_results.metric_name matches sensitivity_summary.metric_name so the reviewer can read sensitivity beside the per-pool result. The conclusion_statement is mandatory.

Evidence:

- src/robinhood_lp/robustness/reports.py:455-490 (assert_train_validation_test_separation rejects any segment_label that appears in two roles)
- src/robinhood_lp/robustness/reports.py:285-310 (RobustnessReport carries train_results, validation_results, test_results as disjoint tuples; sensitivity_summary is mandatory)
- tests/test_robustness_t064.py:1200-1338 (TestRobustnessReport and TestTrainValidationTestSeparation cover all three overlap rejections and the metric-name consistency check)

### scenarios_halt_or_degrade_per_DS035 — PASS

Every catalogue entry declares its HALT or DEGRADE outcome before the run starts (DS-035). The runner reads the catalogue first (assert_catalogue_complete) before touching any data.

Evidence:

- src/robinhood_lp/robustness/scenarios.py:158-230 (Scenario.__post_init__ enforces outcome_kind in {HALT, DEGRADE}; HALT requires non-empty reason_code; DEGRADE requires non-empty fallback_name registered in VALID_FALLBACK_NAMES)
- src/robinhood_lp/robustness/runner.py:226-230 (assert_catalogue_complete called before any data is touched)
- tests/test_robustness_t064.py:773-892 (default catalogue's HALT entries have reason codes; DEGRADE entries have fallback names; rejects missing outcome declarations)

### no_degrade_falls_through_DS041 — PASS

Every DEGRADE fallback must resolve to a registered, deterministic, rule-based baseline (DS-041). The forbidden set NO_ACTION, FORCE_TRADE, UNDEFINED, BEST_EFFORT is rejected both at FallbackRule construction and at policy.resolve. No DEGRADE can fall through to undefined behaviour or a forced trade.

Evidence:

- src/robinhood_lp/robustness/degradation.py:43-50 (FORBIDDEN_FALLBACK_NAMES = NO_ACTION, FORCE_TRADE, UNDEFINED, BEST_EFFORT)
- src/robinhood_lp/robustness/degradation.py:101-117 (FallbackRule constructor rejects any name outside VALID_FALLBACK_NAMES; DegradationPolicy rejects forbidden names at construction)
- src/robinhood_lp/robustness/degradation.py:166-187 (DegradationPolicy.resolve raises UnregisteredFallbackError for unregistered names and ForbiddenFallbackError for forbidden names)
- src/robinhood_lp/robustness/scenarios.py:243-263 (ScenarioCatalogue rejects DEGRADE scenarios whose fallback_name cannot be resolved via policy.resolve)
- tests/test_robustness_t064.py:919-971 (forbidden names rejected via policy.resolve with ForbiddenFallbackError; assert_no_degrade_falls_through exercises forbidden name rejection)

### pool_holdout_results_per_pool_primary_not_time_only — PASS

Pool holdout is the primary generalisation statement: the report cannot be built without non-empty pool_holdout_results and the to_dict serialisation names 'POOL_HOLDOUT' as the primary axis. A time-only holdout is never presented as primary.

Evidence:

- src/robinhood_lp/robustness/reports.py:74 (PRIMARY_GENERALISATION_AXIS = 'POOL_HOLDOUT'); to_dict() at line 357-405 always names 'POOL_HOLDOUT' as the primary axis and exposes per-held-out-pool results
- src/robinhood_lp/robustness/reports.py:284-326 (pool_holdout_results is non-empty by construction; MissingPrimaryGeneralisationError if absent)
- tests/test_robustness_t064.py:1183-1198 (test_report_rejects_empty_pool_holdout_results)

### hand_chosen_embargo_rejected — PASS

A hand-chosen embargo length that disagrees with the derived label-horizon embargo is rejected at split construction with HandChosenEmbargoError. Only the derived-from-horizon embargo is accepted.

Evidence:

- src/robinhood_lp/robustness/splits.py:455-475 (WalkForwardSplit raises HandChosenEmbargoError when validation gap != label_horizon.purge_embargo_length)
- src/robinhood_lp/robustness/splits.py:617-635 (TimeHoldoutSplit raises HandChosenEmbargoError on both gaps)
- tests/test_robustness_t064.py:532-565 (gap=5 with horizon=12 raises HandChosenEmbargoError on TimeHoldoutSplit)

### no_retune_on_test_no_post_hoc_discard — PASS

The pipeline binds the contract before the run starts; the report refuses a missing pool-holdout result, and the regime segmentation refuses empty segment sets. There is no post-hoc drop path.

Evidence:

- src/robinhood_lp/robustness/reports.py:284-326 (RobustnessReport refuses empty pool_holdout_results; the segmentation contract is the segmentation declared at construction time)
- src/robinhood_lp/robustness/surfaces.py:404-460 (RegimeSegmentation rejects empty segments; ALL_REGIMES is reserved so post-hoc drops are visible)
- tests/test_robustness_t064.py:1183-1198, 1037-1042 (post-hoc drop rejected via empty pool_holdout_results and empty segmentation)

### pytest_robustness_tests_pass — PASS

All 82 tests in tests/test_robustness_t064.py pass.

Evidence:

- pytest tests/test_robustness_t064.py: 82 passed in 0.15s

### ruff_format_check_pass — PASS

Ruff format passes on all nine new/changed files.

Evidence:

- ruff format --check src/robinhood_lp/robustness/ tests/test_robustness_t064.py: 9 files already formatted

### ruff_check_pass — PASS

Ruff lint passes on all nine new/changed files.

Evidence:

- ruff check src/robinhood_lp/robustness/ tests/test_robustness_t064.py: All checks passed!

### strict_mypy_pass — PASS

Strict mypy passes on all nine new/changed files.

Evidence:

- mypy src/robinhood_lp/robustness/ tests/test_robustness_t064.py --strict: Success: no issues found in 9 source files

### layer_purity — PASS

The robustness package imports only the standard library and other in-package robustness modules. The layer_map registers it as a backtest-layer package; the import-graph and layer-direction tests confirm no prohibited imports.

Evidence:

- tools/check_imports/layer_map.py (added 'robinhood_lp.robustness': 'backtest')
- pytest tests/test_import_graph.py tests/test_layer_direction_t007.py: 30 passed
- grep of imports across robustness package shows only intra-package and standard-library imports

### upstream_integration_tests_pass — PASS

The T061 / T062 / T063 / import-graph / layer-direction tests still pass; the new robustness package does not regress any upstream task.

Evidence:

- pytest tests/test_reports_t063.py tests/test_strategy_t062.py tests/test_backtest_t061.py tests/test_import_graph.py tests/test_layer_direction_t007.py: 112 passed

### candidate_head_matches_expected_sha — PASS

HEAD of the review worktree matches the expected candidate SHA 137d496cbd8573ca8c281c561d6cf00bcfc37b80.

Evidence:

- git rev-parse HEAD in the review worktree: 137d496cbd8573ca8c281c561d6cf00bcfc37b80

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- Integration with the backtest engine and the per-fold / per-pool evaluation harness is out of scope for T064; the RobustnessRunner expects injected FoldEvaluator and PoolEvaluator callables. A follow-up task will wire the runner to the research harness (T101).
- The default scenario catalogue uses fixed severities (gas x10, latency +600 units, slippage +200 bps, fee x0.5). A future task may want to drive these from a configurable profile tied to the run's actual cost/latency evidence.
- The sensitivity summary uses the range (best - worst) as the spread. The acceptance clause permits range or stddev; stddev is left for a future refinement.
- Pool-holdout boundary records pin the held-out pool's first block (block_range_start) as the anchor; training-pool edges are implicit via the segmentation. A future revision may want to record train-pool boundaries explicitly as well.
- Two pre-existing failures remain in the full pytest run (test_abi_artifacts.py and test_documentation_citations.py). Both predate the T064 candidate and are owned by other tasks; they do not block T064.
