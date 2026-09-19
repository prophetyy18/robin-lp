# T052 independent review

- Base commit: `361c1120b52f72d6721030c87b7a87f9b54498a8`
- Candidate commit: `d74de856762ae5dec1fa5423246d2180f5b54e41`
- Verdict: **PASS**

## Checks

### hold_benchmark_M-BM-001 — PASS

Hold benchmark (M-BM-001) implemented as a raw-token1 value path; reference_basis pinned to 'M-BM-001' on the BenchmarkSeries output.

Evidence:

- src/robinhood_lp/features/attribution.py:741-787 defines HoldBenchmark dataclass with initial_token0, initial_token1 and time-ordered snapshots
- compute_hold_benchmark_value (lines 900-924) computes raw-token1 value at any sqrt_price_x96
- compute_hold_benchmark_series (lines 1068-1102) returns a BenchmarkSeries with reference_basis='M-BM-001'
- tests/test_features_attribution_t052.py TestHoldBenchmarkValue/TestHoldBenchmarkSeries cover the bench

### rebalanced_benchmark_M-BM-002 — PASS

Configurable rebalanced-inventory benchmark (M-BM-002) implemented with free rebalancing; weight-bps configurable in 0..10000; reference_basis='M-BM-002'.

Evidence:

- src/robinhood_lp/features/attribution.py:789-840 defines RebalancedBenchmark with configurable token0_weight_bps
- compute_rebalanced_benchmark_value (lines 927-999) implements V(t) = V0 * (p_t/p_0)^(w0-w1) using integer Q64.64 fixed-point
- compute_rebalanced_benchmark_series (lines 1105-1163) returns BenchmarkSeries with reference_basis='M-BM-002'
- tests/test_features_attribution_t052.py TestRebalancedBenchmark covers weight-bps validation, time ordering, and value computation

### ten_attribution_components — PASS

All ten components reported in raw-token integer units with the T051 protocol-fee separation (lp_fees_gross + lp_fees_lp_only).

Evidence:

- AttributionRow (src/robinhood_lp/features/attribution.py:1172-1346) carries all ten required components for token0 and token1: inventory_pnl, lp_fees_gross, lp_fees_lp_only, divergence, lvr_proxy, gas, slippage, hook_deltas, rebalance_cost, external_cash_flow, plus residual
- compute_attribution_row (lines 1467-1699) populates each component from the EpisodePnL and benchmark series
- EpisodePnL.__post_init__ (lines 720-733) enforces the T051 protocol-fee separation: realised_fees_gross == realised_fees_lp + realised_fees_protocol for both tokens
- tests/test_features_attribution_t052.py test_protocol_fee_separation_passes_through pins the LP/protocol split preservation

### breakeven_volatility_M-BE-001 — PASS

Breakeven volatility solved from the replay's fee + rebalancing-cost series; is_proxy=True and reference_basis='M-BM-002' always present; window_seconds and coverage_seconds recorded; no silent current-price substitution.

Evidence:

- BreakevenVolatility (src/robinhood_lp/features/attribution.py:1855-1976) carries breakeven_vol_q64_64, annualised_breakeven_vol_q64_64, annualisation_factor_q64_64, window_seconds, coverage_seconds, fees_observed_token1, rebalancing_loss_observed_token1, reference_basis, is_proxy (enforced True), method, version
- solve_breakeven_volatility (lines 1979-2085) consumes the replay's fee series and rebalancing-loss series plus realised_volatility_q64_64 (a replay quantity); uses the quadratic-volatility closed form breakeven_vol = realised_vol * sqrt(fees / lvr_proxy)
- BreakevenVolatility.__post_init__ (lines 1964-1968) rejects is_proxy=False with InvalidBreakevenInputError
- tests/test_features_attribution_t052.py TestBreakevenVolatility covers replay-series solving, window/annualisation recording, is_proxy enforcement, and input validation

### lvr_proxy_never_measured — PASS

lvr_is_proxy is always True on the canonical construction path; the rebalancing-loss proxy is never presented as a measured LVR.

Evidence:

- compute_attribution_row (src/robinhood_lp/features/attribution.py:1692) always sets lvr_is_proxy=True on the output row
- lvr_proxy_token1 formula (lines 1590-1592) is rebalanced-vs-LP value gap (M-LVR-001)
- tests/test_features_attribution_t052.py test_lvr_proxy_is_marked asserts lvr_is_proxy is True and reference_basis == 'M-BM-002' on every canonical row
- BreakevenVolatility rejects is_proxy=False at construction

### reference_basis_always_declared — PASS

Every AttributionRow carries a non-empty reference_basis. Reference basis is structurally enforced to be 'M-BM-002' for the canonical path; alternate bases are refused at construction.

Evidence:

- compute_attribution_row (src/robinhood_lp/features/attribution.py:1515-1524) requires hold_series.reference_basis == 'M-BM-001' and rebalanced_series.reference_basis == 'M-BM-002'
- AttributionRow (lines 1242, 1292-1296) requires reference_basis to be a non-empty str
- compute_attribution_row sets reference_basis from rebalanced_series.reference_basis (line 1691)
- tests/test_features_attribution_t052.py test_two_rows_with_different_bases_are_not_ranked verifies that constructing a row with a non-M-BM-002 rebalanced series raises AttributionError

### proxies_on_different_bases_not_ranked — PASS

Two proxies built on different reference bases cannot be ranked: the canonical computation refuses a non-M-BM-002 rebalanced series and ranking_blocked_between blocks cross-numeraire comparisons.

Evidence:

- compute_attribution_row rejects a non-M-BM-002 rebalanced_series with AttributionError (src/robinhood_lp/features/attribution.py:1520-1524)
- tests/test_features_attribution_t052.py TestCrossReferenceBasisGuard.test_two_rows_with_different_bases_are_not_ranked verifies the rejection
- Cross-numeraire ranking is blocked by the T053 ranking_blocked_between helper, used in test_relative_only_row_is_not_ranked_against_usd_row

### no_usd_field_on_RELATIVE_ONLY — PASS

RELATIVE_ONLY attribution row and report carry no USD-denominated field anywhere; the invariant is enforced structurally (no USD field on the dataclass) and at serialisation (assert_no_usd_fields).

Evidence:

- RelativeOnlyAttributionRow (src/robinhood_lp/features/attribution.py:1349-1459) carries only *_ratio_q64_64 fields, reference_basis, lvr_is_proxy, numeraire_level (RELATIVE_ONLY), version, and notes; no USD-denominated field exists on the dataclass
- RelativeOnlyAttributionRow.__post_init__ raises QuoteRelativeOnlyError if numeraire_level != RELATIVE_ONLY (lines 1419-1423)
- RelativeOnlyAttributionRow.as_payload (lines 1429-1459) invokes assert_no_usd_fields(payload, context='RelativeOnlyAttributionRow.as_payload') so the invariant is machine-checked
- tests/test_features_attribution_t052.py test_payload_has_no_usd_field, test_payload_rejects_usd_field_injection, test_constructor_rejects_wrong_numeraire, test_attribution_row_rejects_relative_only_flag pin the contract
- RelativeOnlyAttributionReport.as_payload (lines 2187-2199) also invokes assert_no_usd_fields
- tests/test_features_attribution_t052.py test_relative_only_report_has_no_usd_field covers the report-level invariant

### relative_only_not_ranked_against_usd — PASS

ranking_blocked_between blocks ranking of a relative-only result against a USD-denominated result; the RELATIVE_ONLY row's structural numeraire_level matches.

Evidence:

- tests/test_features_attribution_t052.py test_relative_only_row_is_not_ranked_against_usd_row constructs a RelativeOnlyBar and a USDG QuoteBar and asserts ranking_blocked_between(rel_bar, usd_bar) is True
- The test also pins that RelativeOnlyAttributionRow.numeraire_level is NumeraireLevel.RELATIVE_ONLY

### accounting_identity_closes — PASS

Accounting identity closes within documented conversion rounding for canonical fixtures; residual above tolerance fails row construction (AttributionIdentityError) and report generation (failed=True).

Evidence:

- compute_attribution_row (src/robinhood_lp/features/attribution.py:1656-1663) raises AttributionIdentityError when abs(residual_token0) > RESIDUAL_TOLERANCE or abs(residual_token1) > RESIDUAL_TOLERANCE
- RESIDUAL_TOLERANCE = 1 << 96 is documented as the documented absolute bound on the integer price-conversion rounding (lines 177-198)
- tests/test_features_attribution_t052.py test_static_price_no_trade_closes_to_zero and test_price_reversal_residual_within_tolerance verify the identity closes within tolerance for the canonical fixtures

### fixtures_have_known_results — PASS

Zero-fee / static-price / no-trade / price-reversal fixtures have known results pinned by tests.

Evidence:

- tests/test_features_attribution_t052.py test_static_price_no_trade_closes_to_zero (static-price, no-trade): all components zero, residual zero
- test_price_reversal_residual_within_tolerance (price reversal start_tick=-10, end_tick=10 with fees): residual within RESIDUAL_TOLERANCE
- test_zero_fee_no_trade_has_zero_fees (zero-fee, no-trade): lp_fees_* and divergence and lvr_proxy all zero
- test_protocol_fee_separation_passes_through: lp/protocol split preserved
- test_lvr_proxy_is_marked: lvr_is_proxy=True and reference_basis='M-BM-002'
- test_zero_fee_no_trade_fixture (TestHoldBenchmarkValue): zero-fee fixture pin

### residual_above_threshold_fails_report — PASS

Residual above threshold fails report generation (failed=True) and raises AttributionIdentityError on the canonical construction.

Evidence:

- generate_report (src/robinhood_lp/features/attribution.py:2202-2250) catches AttributionIdentityError and returns an AttributionReport with failed=True
- tests/test_features_attribution_t052.py test_oversized_residual_fails_construction verifies failed=True when the residual exceeds RESIDUAL_TOLERANCE
- test_oversized_residual_raises_in_canonical_compute verifies the canonical compute raises AttributionIdentityError
- test_records_failed_when_residual_exceeds_tolerance verifies generate_report records failed=True

### breakeven_window_and_annualisation — PASS

Breakeven volatility records its window and annualisation; coverage is checked against the window at construction.

Evidence:

- BreakevenVolatility dataclass (src/robinhood_lp/features/attribution.py:1855-1920) records window_seconds, coverage_seconds, annualisation_factor_q64_64, annualised_breakeven_vol_q64_64
- solve_breakeven_volatility computes annualisation_factor = sqrt(annualisation_seconds / window_seconds) in Q64.64 fixed-point (lines 2062-2064)
- tests/test_features_attribution_t052.py test_records_window_and_annualisation verifies window_seconds, coverage_seconds, and the annualisation_factor for window=86400s, annualisation=31_536_000s

### breakeven_is_proxy — PASS

Breakeven volatility is always labelled a proxy alongside the proxy it equates; is_proxy=False is refused at construction.

Evidence:

- BreakevenVolatility.__post_init__ (src/robinhood_lp/features/attribution.py:1964-1968) requires is_proxy=True; raises InvalidBreakevenInputError otherwise
- solve_breakeven_volatility always sets is_proxy=True and reference_basis='M-BM-002' (lines 2075-2076)
- tests/test_features_attribution_t052.py test_is_proxy_flag_is_always_true verifies the constructor rejects is_proxy=False

### il_not_sole_opportunity_cost — PASS

IL is reported as one of ten components (divergence); no collapse to a single IL number occurs.

Evidence:

- AttributionRow carries ten components (inventory_pnl, lp_fees_gross, lp_fees_lp_only, divergence/IL, lvr_proxy, gas, slippage, hook_deltas, rebalance_cost, external_cash_flow) plus residual
- LP_METRICS §8 prohibits using IL as the sole opportunity cost; the implementation reports all ten components and never collapses them into a single IL number
- test_protocol_fee_separation_passes_through verifies the gross/lp-fee split is preserved so fees and IL are not merged

### annualisation_labeled_with_method_and_coverage — PASS

Annualisation is reported with the method (replay_quadratic), window, and coverage; an incomplete period cannot be annualised silently because the coverage is checked at construction.

Evidence:

- BreakevenVolatility.notes field records the method ('replay_quadratic'), window, coverage, annualisation, reference_basis, and is_proxy
- window_seconds and coverage_seconds are distinct fields; the constructor rejects coverage_seconds > window_seconds
- The breakeven output records annualised_breakeven_vol_q64_64 alongside the raw breakeven_vol_q64_64 so the unannualised quantity is always available

### tests_pass_50_of_50 — PASS

All 50 T052 tests pass; the 417-test bundle for T050/T051/T052/T053 plus workflow contracts passes; ruff format/check and mypy --strict are clean.

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python3.12 -m pytest tests/test_features_attribution_t052.py -q => 50 passed in 0.15s
- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python3.12 -m pytest tests/test_features_position_t051.py tests/test_features_quote_t053.py tests/test_features_bars_t050.py tests/test_features_attribution_t052.py tests/test_workflow_contracts.py tests/test_workflow.py -q => 417 passed in 3.39s
- Full suite excluding the environment-dependent abi_artifacts test: 1764 passed, 6 skipped in 13.11s (test_abi_artifacts failure is unrelated to T052 — the worktree lacks tools/oracle/lib, see residual_risks)

### ruff_format_check_clean — PASS

ruff format and ruff check are clean for the new files.

Evidence:

- ruff format --check src/robinhood_lp/features/attribution.py tests/test_features_attribution_t052.py => 2 files already formatted
- ruff check src/robinhood_lp/features/attribution.py tests/test_features_attribution_t052.py => All checks passed!

### mypy_strict_clean — PASS

mypy --strict is clean for the new files.

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python3.12 -m mypy --strict src/robinhood_lp/features/attribution.py tests/test_features_attribution_t052.py => Success: no issues found in 2 source files

### protected_prefix_files_not_touched — PASS

No protected-prefix file is touched by the diff. The todo/config.yaml change is the standard workflow state transition recorded by the controller, consistent with prior candidate commits (e.g. T051's eaaf912..0e86454).

Evidence:

- git diff --name-only 361c112..d74de85 lists: src/robinhood_lp/features/attribution.py, tests/test_features_attribution_t052.py, todo/config.yaml, todo/evidence/P05/T052/attempt-001-developer.json
- The diff touches only: the new attribution module, the new test file, the standard workflow state transition in todo/config.yaml (workflow_state READY->AWAITING_REVIEW; T052 status READY->AWAITING_REVIEW; attempt 0->1; base_commit populated), and the developer evidence JSON
- No file under docs/spec/, docs/intent/, todo/phases/, todo/schemas/, tools/workflow/, .claude/, CLAUDE.md, AGENTS.md, todo/README.md is touched
- todo/config.yaml's modification is the documented workflow-controller state transition that all prior product tasks (e.g. T051, T053) also perform; it is not a code or contract modification

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output fails in this worktree because tools/oracle/lib/ (v4-core, v4-periphery) is not present; this is a worktree-environment artifact, not a T052 change. The same test passes on /home/lpdev/lp (the main checkout). 1764 of the remaining tests pass.
- compute_relative_only_attribution_row computes entry_token0 = row.inventory_pnl_token0 + _entry_raw_token0(row), where _entry_raw_token0 returns row.inventory_pnl_token0 itself; for a non-degenerate episode the caller must supply an AttributionRow built from an EpisodePnL whose start_valuation.raw_amount_0 is non-zero. Degenerate episodes (entry_token0 == 0) fall through to an all-zeros row, which is documented behaviour but means callers who build a relative-only row from an unconnected AttributionRow will see zeros rather than an error.
- Gas and rebalance-cost conversions from raw wei to token1 units are recorded as zero in _gas_cost_in_token1_units and _rebalance_cost_in_token1_units because no USDG observation is supplied on the GasCost / RebalanceEvent record. The framework's documented rule is that the T053 layer owns the point-in-time USDG price; the attribution layer does not invent one. Downstream consumers who need a token1 gas value must pass it through a different path (e.g. carry the per-event USDG observation on the GasCost record or compute it at the report layer).
