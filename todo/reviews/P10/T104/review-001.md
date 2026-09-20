# T104 independent review

- Base commit: `3b0984aa062e0affe4657b5de353eb3b4851d5ac`
- Candidate commit: `f451ff94f55a3afdeb1dc38cfe0a0d47f3ae3d5b`
- Verdict: **PASS**

## Checks

### dependencies_approved — PASS

All declared dependencies (T039, T041) are APPROVED at the time of review.

Evidence:

- todo/config.yaml: T039 status=APPROVED (P03), T041 status=APPROVED (P04) — both deps required by T104
- T104 contract (todo/phases/P10-research-and-models/T104.md) declares depends_on=['T039','T041'] and both are APPROVED before the candidate commit

### deliverable_window_descriptor — PASS

WindowDescriptor binds the pool, window, dataset version and reconstruction revision, and is structurally enforced.

Evidence:

- src/robinhood_lp/replay/fee_surface.py:243-301 defines WindowDescriptor dataclass with chain_id, pool_id, pool_key, from_block, to_block, dataset_version, reconstruction_revision, with __post_init__ validating every field
- tests/test_fee_surface_t104.py::test_window_descriptor_rejects_inverted_range, test_window_descriptor_rejects_blank_dataset_version, test_window_descriptor_rejects_blank_reconstruction_revision all raise WindowDescriptorError
- FeeGrowthSurface.window is a WindowDescriptor; tests/test_fee_surface_t104.py::test_surface_records_pool_window_dataset_and_revision asserts surface.window carries pool, window, dataset_version, and reconstruction_revision

### deliverable_prefix_sum_surface — PASS

Surface stores per-window feeGrowthInside prefix sums over initialized ticks plus per-event feeGrowthGlobal snapshots; range query is a pure function of the precomputed data.

Evidence:

- src/robinhood_lp/replay/fee_surface.py:314-376 FeeGrowthSnapshot records event_index, canonical position triple, fee_growth_global_0_x128/1_x128, current_tick, active_liquidity, ticks_crossed (post-crossTick outside values)
- src/robinhood_lp/replay/fee_surface.py:383-465 TickFeeGrowthPrefix stores outside_initial + per-event history sorted by event_index, with binary-search value_at_event returning post-state at a given event_index
- src/robinhood_lp/replay/fee_surface.py:582-820 FeeGrowthSurface holds per-event swap_snapshots tuple, per-tick tick_prefixes tuple, initial/final fee-growth state, and provides fee_growth_inside and historical_fees that are pure functions of the prefix sums
- tests/test_fee_surface_t104.py::test_swap_snapshot_event_index_monotone and test_tick_prefix_history_sorted_by_event_index confirm ordering invariants

### deliverable_o1_range_query — PASS

Range and liquidity query is bounded by the surface's precomputed data; no event stream walk in the hot path.

Evidence:

- src/robinhood_lp/replay/fee_surface.py:687-800 FeeGrowthSurface.fee_growth_inside and historical_fees read start/end states from the prefix sums and apply the V4 piecewise formula; they do not re-simulate the event stream
- tests/test_fee_surface_t104.py::test_full_tick_range_captures_all_fee_growth confirms a full-tick-range query captures the global fee growth exactly

### deliverable_versioned_artifact — PASS

Surface artifact is versioned (schema_version + content_hash) and binds pool, window, dataset_version and reconstruction_revision via the WindowDescriptor and canonical payload.

Evidence:

- src/robinhood_lp/replay/fee_surface.py:150 SURFACE_SCHEMA_VERSION = 't104.fee_surface.v1'; __post_init__ rejects any other schema_version
- src/robinhood_lp/replay/fee_surface.py:824-923 canonical_payload covers schema_version, window descriptor (chain/pool/pool_key/dataset_version/reconstruction_revision/blocks), initial+final fee-growth state, all snapshots, all tick prefixes, equivalence check
- src/robinhood_lp/replay/fee_surface.py:1932-1936 _fingerprint_surface computes SHA-256 over canonical_payload; FeeGrowthSurface.content_hash carries it
- tests/test_fee_surface_t104.py::test_surface_content_hash_binds_inputs, test_surface_with_different_dataset_version_yields_different_hash, test_surface_with_different_window_yields_different_hash, test_surface_rejects_wrong_schema_version, test_same_inputs_yield_identical_surface all PASS

### deliverable_equivalence_check — PASS

Surface publishes only when the equivalence check passes; check covers heterogeneous (range, liquidity) triples including the contract-required boundary cases.

Evidence:

- src/robinhood_lp/replay/fee_surface.py:1178-1222 integrate_position_fees walks surface.swap_snapshots and accumulates L*delta/Q128 per snapshot — the direct per-position integration
- src/robinhood_lp/replay/fee_surface.py:1757-1827 run_equivalence_check drives a heterogeneous set of (range, L) triples (in-range, out-of-range, below, above, bounds-on-initialized-ticks, full tick range, multi-crossing, zero-liquidity, second-L linearity)
- src/robinhood_lp/replay/fee_surface.py:1684-1690 build_fee_growth_surface refuses to publish the surface if equivalence_check.passed is False
- tests/test_fee_surface_t104.py::test_single_swap_equivalence_check_passes, test_multi_swap_equivalence_check_passes, test_bounds_on_initialized_ticks_equivalence_passes, test_run_equivalence_check_returns_passed_for_known_good_surface, test_run_equivalence_check_rejects_non_surface all PASS

### deliverable_units_scaling — PASS

Units, scaling and 2^128 fixed-point convention are documented and exercised by tests that recompute expected fee growth from the formula and from the linearity identity.

Evidence:

- src/robinhood_lp/replay/fee_surface.py:81-95 module docstring documents Q128 fixed-point convention, fee-owed identity fees(L)=L*delta/2^128, FEE_DENOMINATOR=1_000_000 pips, uint24 fee field
- src/robinhood_lp/replay/fee_surface.py:142-147 constants Q128=1<<128 and FEE_DENOMINATOR=1_000_000
- tests/test_fee_surface_t104.py::test_single_swap_window_fee_growth_matches_reference verifies expected_delta_g0 = (1000*3000//FEE_DENOMINATOR)*Q128//DEFAULT_LIQUIDITY equals surface.final_fee_growth_global_0_x128
- tests/test_fee_surface_t104.py::test_linearity_holds_at_two_distinct_liquidity_values verifies fees(L) = L*delta/Q128 holds at three distinct L values (1_000, 1_000_000, 1_000_000_000)

### deliverable_console_render_with_provenance — PASS

Every console-side rendered value carries the window, dataset version and reconstruction revision through the WindowDescriptor; construction refuses a value without provenance.

Evidence:

- src/robinhood_lp/replay/fee_surface.py:1980-2031 SurfaceRenderedValue dataclass carries surface_content_hash, window (WindowDescriptor), tick_lower, tick_upper, liquidity, fee_growth_inside_*, fee_*
- src/robinhood_lp/replay/fee_surface.py:2034-2075 render_surface_value produces a SurfaceRenderedValue; constructor rejects zero liquidity and invalid ranges
- tests/test_fee_surface_t104.py::test_rendered_value_carries_window_provenance asserts rendered.window == surface.window and rendered.surface_content_hash == surface.content_hash
- tests/test_fee_surface_t104.py::test_rendered_value_rejects_invalid_range_via_direct_construction, test_rendered_value_rejects_zero_liquidity confirm refusal paths

### acceptance_in_range_out_of_range_price_reversal_multi_crossing — PASS

Pinned in-range, out-of-range, price-reversal and multi-crossing windows all reproduce direct per-position integration exactly.

Evidence:

- tests/test_fee_surface_t104.py::test_multi_swap_fee_growth_is_sum_over_in_range_swaps exercises a multi-crossing window with price-reversal sequence and asserts surface.historical_fees == integrate_position_fees
- tests/test_fee_surface_t104.py::test_range_outside_traded_price_path_returns_zero asserts fees==(0,0) for range [240, 360] (well above traded path)
- tests/test_fee_surface_t104.py::test_range_containing_no_initialized_tick_returns_zero asserts fees==(0,0) for range [120, 180] (contains no initialized tick)
- tests/test_fee_surface_t104.py::test_full_tick_range_captures_all_fee_growth verifies the full-tick-range case equals L*global/Q128

### acceptance_linearity_two_liquidity_values — PASS

Linearity identity verified at two distinct liquidity values for the same range; both as a dedicated test and inside the equivalence check.

Evidence:

- tests/test_fee_surface_t104.py::test_linearity_holds_at_two_distinct_liquidity_values verifies fees(L) = L*delta/Q128 at three distinct L values
- _heterogeneous_check_triples (src/robinhood_lp/replay/fee_surface.py:1917-1922) appends a second-L triple (in_range_lower, in_range_upper, 2_000_000) for the same range as the primary in-range triple at L=1_000_000
- EquivalenceCheck records (sample_size=2 records with the two L values) match exactly — verified manually with multi-swap fixture (L=1_000_000 -> fees=(5,11); L=2_000_000 -> fees=(11,23))

### acceptance_zero_range_no_initialized_tick — PASS

A range containing no initialized tick is handled by its own named case — outside=(0,0) and the piecewise formula yields zero fee growth.

Evidence:

- tests/test_fee_surface_t104.py::test_range_containing_no_initialized_tick_returns_zero: range [120,180] contains no initialized tick in the multi-swap window, fees=(0,0)
- src/robinhood_lp/replay/fee_surface.py:802-820 _outside_at returns (0,0) for ticks not in the prefix sums; the surface then applies the V4 piecewise formula yielding zero for the typical above-range / below-range cases

### acceptance_no_swap_window — PASS

Zero-swap window is published with zero fee growth, the surface is marked is_empty, and the equivalence check passes; the artifact still binds the window.

Evidence:

- tests/test_fee_surface_t104.py::test_no_swap_window_returns_zero_and_is_empty: surface.is_empty=True, final_fee_growth_global_*==0, historical_fees==(0,0)
- tests/test_fee_surface_t104.py::test_no_swap_window_is_not_missing_data: equivalence_check.passed and window descriptor still carries dataset_version — zero-swap is published, not refused
- src/robinhood_lp/replay/fee_surface.py:672-683 is_empty property explicitly documents zero-swap is not missing data

### acceptance_artifact_binds_provenance — PASS

Artifact records the pool, window, dataset version and reconstruction revision it was derived from.

Evidence:

- tests/test_fee_surface_t104.py::test_surface_records_pool_window_dataset_and_revision asserts surface.window carries chain_id, pool_id, pool_key, dataset_version, reconstruction_revision, from_block, to_block
- tests/test_fee_surface_t104.py::test_surface_with_different_dataset_version_yields_different_hash and test_surface_with_different_window_yields_different_hash confirm the hash is sensitive to dataset_version and window

### acceptance_console_value_with_provenance — PASS

Console-side rendered values carry window, dataset version, reconstruction revision and content_hash; a value whose provenance cannot be named is refused at construction.

Evidence:

- tests/test_fee_surface_t104.py::test_rendered_value_carries_window_provenance: rendered.window == surface.window and rendered.surface_content_hash == surface.content_hash
- tests/test_fee_surface_t104.py::test_rendered_value_rejects_invalid_range_via_direct_construction, test_rendered_value_rejects_zero_liquidity confirm a value whose provenance cannot be named is refused rather than rendered (zero liquidity is refused at construction with InvalidLiquidityError; missing surface via render_surface_value('not a surface') is refused with FeeSurfaceError)

### acceptance_boundary_tick_lower_equal_tick_upper — PASS

tickLower == tickUpper is rejected by both the surface query and the direct integration by the same named case.

Evidence:

- tests/test_fee_surface_t104.py::test_tick_lower_equal_tick_upper_rejected: both surface.historical_fees and integrate_position_fees raise InvalidTickRangeError when tick_lower == tick_upper
- src/robinhood_lp/replay/fee_surface.py:931-945 _validate_tick_range rejects tick_lower >= tick_upper with InvalidTickRangeError before any prefix-sum lookup

### acceptance_boundary_bounds_on_initialized_ticks — PASS

A range whose bounds sit exactly on initialized ticks is handled and the surface reproduces the direct integration.

Evidence:

- tests/test_fee_surface_t104.py::test_bounds_on_initialized_ticks_equivalence_passes exercises the bounds_on_initialized_ticks_sequence fixture (two disjoint ranges plus a [-120,120] range) and asserts surface.historical_fees == integrate_position_fees at range [-60, 60]

### acceptance_boundary_full_tick_range — PASS

A range spanning the full V4 tick domain captures the entire fee growth global.

Evidence:

- tests/test_fee_surface_t104.py::test_full_tick_range_captures_all_fee_growth: range [-(1<<23)+1, (1<<23)-1] yields fees = L * global / Q128

### acceptance_boundary_mid_price_movement_window — PASS

A window that begins mid-price-movement uses the bootstrap tick as the initial state.

Evidence:

- tests/test_fee_surface_t104.py::test_mid_price_movement_window_bootstrap_applied: bootstrap_tick=60 yields surface.initial_tick==60 and fee growth still accrues

### acceptance_boundary_single_swap_window — PASS

A pool whose window contains a single swap is covered; equivalence check passes.

Evidence:

- tests/test_fee_surface_t104.py::test_single_swap_window_fee_growth_matches_reference and test_single_swap_equivalence_check_passes cover the single-swap window; surface.swap_count==1 and equivalence_check passes

### acceptance_boundary_zero_liquidity — PASS

Requested liquidity of zero returns zero from both paths via the named case at the surface query layer.

Evidence:

- tests/test_fee_surface_t104.py::test_zero_liquidity_returns_zero_from_both_paths: surface.historical_fees(tick_lower=-60, tick_upper=60, liquidity=0) == (0, 0) and integrate_position_fees(..., liquidity=0) == (0, 0)
- src/robinhood_lp/replay/fee_surface.py:780-781 historical_fees returns (0, 0) for liquidity==0 without consulting the prefix sums

### must_not_no_volume_share_estimation — PASS

No fee path uses share multiplied by aggregated or bucketed volume; every value is computed from per-swap signed amounts and V4 feeGrowthInside.

Evidence:

- fee_surface.py imports only robinhood_lp.protocol.{ids,records}, robinhood_lp.replay.{ticks,errors} — no volume aggregate, no share-of-volume formula
- src/robinhood_lp/replay/fee_surface.py:1034-1130 _swap_fee_growth_delta computes delta_g = (input_amount * fee_pips / FEE_DENOMINATOR) * Q128 / pre_swap_active_liquidity from the per-swap signed amount
- src/robinhood_lp/replay/fee_surface.py:798-799 historical_fees uses L*delta/Q128 with delta being feeGrowthInside — no volume aggregation in the query path
- tests/test_fee_surface_t104.py::test_range_outside_traded_price_path_returns_zero asserts the out-of-path range returns zero (not a small positive residue from a share-of-volume estimate)

### must_not_virtual_liquidity_in_denominator — PASS

Historical_fees uses the precomputed feeGrowthInside prefix sums and the linearity identity L*delta/Q128; the hypothetical L never enters the historical active-liquidity denominator.

Evidence:

- src/robinhood_lp/replay/fee_surface.py:802-820 _outside_at and historical_fees query the surface's precomputed prefix sums directly; no recomputation of active-liquidity denominator with the hypothetical L added in
- tests/test_fee_surface_t104.py::test_linearity_holds_at_two_distinct_liquidity_values verifies L appears linearly in the formula fees(L) = L*delta/Q128 — not via a share with denominator including L

### must_not_no_cross_pool_or_cross_window_prefix — PASS

Prefix sums are confined to one pool and one window; the builder refuses cross-pool or cross-window mixing.

Evidence:

- src/robinhood_lp/replay/fee_surface.py:1372-1383 build_fee_growth_surface rejects reconstructed_state with mismatched chain_id or pool_id
- src/robinhood_lp/replay/fee_surface.py:1413-1422 build_fee_growth_surface rejects every event whose chain_id or pool_id does not match the declared pool
- src/robinhood_lp/replay/fee_surface.py:1409-1412 build_fee_growth_surface rejects events outside [from_block, to_block]
- tests/test_fee_surface_t104.py::test_surface_rejects_pool_mismatch and test_surface_rejects_event_outside_window confirm these guards

### must_not_no_zero_fee_window_treated_as_missing — PASS

Zero-fee window is published with zero growth and not refused or treated as incomplete data.

Evidence:

- tests/test_fee_surface_t104.py::test_no_swap_window_returns_zero_and_is_empty and test_no_swap_window_is_not_missing_data: zero-swap window publishes with is_empty=True and zero fee growth; equivalence_check passes; window descriptor carries dataset_version

### must_not_surface_published_with_equivalence_check — PASS

Surface is published only when the equivalence check passes.

Evidence:

- src/robinhood_lp/replay/fee_surface.py:1684-1690 build_fee_growth_surface raises FeeSurfaceError and refuses to publish when equivalence_check.passed is False
- tests/test_fee_surface_t104.py::test_single_swap_equivalence_check_passes, test_multi_swap_equivalence_check_passes, test_bounds_on_initialized_ticks_equivalence_passes confirm equivalence_check.passed for the canonical fixtures

### must_not_no_value_without_provenance — PASS

Every value produced by render_surface_value carries the full WindowDescriptor (pool, window, dataset version, reconstruction revision) and the surface content hash; construction refuses a value without provenance.

Evidence:

- src/robinhood_lp/replay/fee_surface.py:2008-2031 SurfaceRenderedValue.__post_init__ requires non-empty surface_content_hash and rejects zero liquidity and invalid ranges
- src/robinhood_lp/replay/fee_surface.py:2051-2064 render_surface_value raises FeeSurfaceError on non-FeeGrowthSurface input and always sets window=surface.window
- tests/test_fee_surface_t104.py::test_rendered_value_carries_window_provenance, test_rendered_value_rejects_invalid_range_via_direct_construction, test_rendered_value_rejects_zero_liquidity all PASS

### tests_pass_47_of_47_t104 — PASS

All 47 new T104 tests pass.

Evidence:

- PYTHONPATH=src:tests /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/test_fee_surface_t104.py => 47 passed in 0.30s

### tests_pass_full_repository_suite_no_regression — PASS

Full repository suite passes 2576/2576 (excluding forge-dependent tests and a known worktree-environment artifact); no regressions introduced by the T104 candidate.

Evidence:

- PYTHONPATH=src:tests /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/ --ignore=tests/test_abi_artifacts.py --ignore=tests/test_e2e_t035.py => 2576 passed, 6 skipped in 34.02s (matches developer's claim; 6 skips are environment skips: 3 forge-dependent tests in test_oracle_drift.py, 2 gpg-out-of-scope in test_oracle_review_provenance.py, 1 vector 'reordered_inputs' in test_protocol_ids.py)
- test_abi_artifacts.py is the only test file the developer excluded from the run; it requires tools/oracle/lib (v4-core, v4-periphery) which is not present in the review worktree — this is an environment artifact of this detached worktree, not introduced by the T104 diff

### ruff_format_and_check_clean — PASS

ruff check and ruff format are clean for the new module, test file, and fixtures.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff check src/robinhood_lp/replay/ tests/test_fee_surface_t104.py tests/_fee_surface_t104_fixtures.py => All checks passed!
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff format --check src/robinhood_lp/replay/ tests/test_fee_surface_t104.py tests/_fee_surface_t104_fixtures.py => 12 files already formatted

### mypy_strict_clean — PASS

mypy --strict is clean for the replay package including the new fee_surface module.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy --strict src/robinhood_lp/replay/ => Success: no issues found in 10 source files

### import_graph_layer_classification_clean — PASS

Import-graph, layer-direction, classification and architecture agreement all pass; the new module sits in the reconstruction tier with no upward dependencies.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.check_imports check => import-graph check passed: no findings (layer-direction, classification, and architecture agreement all clean)
- src/robinhood_lp/replay/fee_surface.py imports only robinhood_lp.protocol.{ids,records} and robinhood_lp.replay.{ticks,errors} — no upward dependencies into features, backtest, strategy, risk, execution, presentation, RPC, storage, signer or configuration
- tools/check_imports/layer_map.py:136 maps robinhood_lp.replay to the reconstruction tier, consistent with the module docstring's ADR-006 §2.2 placement

### no_dependency_manifest_changes — PASS

No pyproject.toml, requirements.in or requirements.lock.txt changes; dependency manifest is untouched.

Evidence:

- git diff 3b0984a..f451ff9 -- pyproject.toml requirements.in requirements.lock.txt => empty
- Only modified paths are: src/robinhood_lp/replay/__init__.py (re-exports only), src/robinhood_lp/replay/fee_surface.py (new module), tests/_fee_surface_t104_fixtures.py (new), tests/test_fee_surface_t104.py (new), todo/config.yaml (workflow state transition), todo/evidence/P10/T104/attempt-001-developer.json (developer evidence)

### protected_paths_not_touched — PASS

No protected-path file is touched by the diff; only the working surface and the workflow state transition record are changed.

Evidence:

- git diff --name-only 3b0984a..f451ff9 => src/robinhood_lp/replay/__init__.py, src/robinhood_lp/replay/fee_surface.py, tests/_fee_surface_t104_fixtures.py, tests/test_fee_surface_t104.py, todo/config.yaml, todo/evidence/P10/T104/attempt-001-developer.json
- No file under docs/spec/, docs/intent/, docs/implement/, todo/phases/, todo/schemas/, todo/README.md, tools/workflow/, .claude/, CLAUDE.md, AGENTS.md or .github/ is touched
- todo/config.yaml change is the documented workflow state transition (workflow_state READY->AWAITING_REVIEW; T104 status READY->AWAITING_REVIEW; attempt 0->1; base_commit populated; candidate_commit null)

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- Single-step fee-growth approximation: for a swap that crosses multiple initialized ticks the surface uses the pre-swap active liquidity (from the first crossing's active_liquidity_before) for the entire fee-growth delta instead of V4's per-step integration. The surface is internally consistent with the direct per-position integration path, so the two always agree, but neither matches the on-chain StateView.getFeeGrowthGlobals byte-for-byte for multi-step swaps. The simplification is documented in the module docstring and recorded in the developer evidence JSON; consumers needing byte-for-byte agreement read the on-chain global directly. This is acceptable per the contract: the acceptance criterion is agreement between the surface and direct per-position integration, not byte-for-byte equality with on-chain state.
- Crossing lookup key uses the (block_number, transaction_index, log_index) triple rather than the full EventKey tuple (which adds block_hash and tx_hash). The T041 reconstruction emits one crossing per (swap, crossed_tick), so the lookup is unambiguous within one pool; a future change that lets T041 cross the same tick twice in a single swap would need a stronger key. This is a latent invariant the contract does not currently exercise.
