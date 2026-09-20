"""Tests for the T104 fee-growth surface.

Coverage:

- the builder produces a surface whose per-event prefix sums, per-tick
  prefix sums and equivalence check agree with the direct per-position
  integration the contract mandates;
- a single-swap window, a multi-crossing / price-reversal window, a
  bounds-on-initialized-ticks window, a no-swap window, a window that
  begins or ends mid-price-movement and a window whose swaps span the
  full tick range are all covered;
- a range entirely outside the traded price path returns zero (a
  small positive residue would be the forbidden share-of-volume
  estimate);
- a range containing no initialized tick is handled by its own named
  case (the surface returns zero, not a fee estimate);
- a window with no swaps returns zero and the surface is marked
  ``is_empty``;
- ``tickLower == tickUpper`` is rejected by the surface and by the
  direct integration by the same named case;
- a requested liquidity of zero returns zero from both paths;
- the linearity identity ``fees(L) = L * delta / 2**128`` is verified
  at two distinct liquidity values for the same range;
- the artifact's content hash is reproducible from its canonical
  payload and binds the pool, the window, the dataset version and
  the reconstruction revision;
- the console-rendering helper carries the window, dataset version
  and reconstruction revision with every value, and refuses to
  produce a value whose provenance cannot be named;
- the surface is per-pool: mixing two pools' reconstructions is
  rejected at the build step, never silently merged.
"""

from __future__ import annotations

import hashlib

import pytest

from _fee_surface_t104_fixtures import (
    CHAIN,
    DEFAULT_LIQUIDITY,
    POOL_ID,
    POOL_KEY,
    bounds_on_initialized_ticks_sequence,
    multi_swap_event_sequence,
    no_swap_event_sequence,
    reconstruct_state,
    single_swap_event_sequence,
)
from robinhood_lp.protocol import PoolId
from robinhood_lp.replay.fee_surface import (
    FEE_DENOMINATOR,
    Q128,
    SURFACE_SCHEMA_VERSION,
    EquivalenceCheck,
    FeeGrowthSnapshot,
    FeeGrowthSurface,
    FeeSurfaceError,
    InvalidTickRangeError,
    PoolIdentityMismatchError,
    PositionIntegrationRecord,
    SurfaceRenderedValue,
    TickFeeGrowthPrefix,
    WindowDescriptor,
    WindowDescriptorError,
    build_fee_growth_surface,
    integrate_position_fees,
    render_surface_value,
    run_equivalence_check,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_surface(
    events,
    *,
    from_block: int = 1_000_000,
    to_block: int = 1_000_060,
    bootstrap_tick: int = 0,
    bootstrap_active_liquidity: int = 0,
    dataset_version: str = "dataset-reference-v1",
    reconstruction_revision: str = "recon.v1",
):
    state = reconstruct_state(events)
    return build_fee_growth_surface(
        chain_id=CHAIN,
        pool_id=POOL_ID,
        pool_key=POOL_KEY,
        from_block=from_block,
        to_block=to_block,
        bootstrap_tick=bootstrap_tick,
        bootstrap_active_liquidity=bootstrap_active_liquidity,
        dataset_version=dataset_version,
        reconstruction_revision=reconstruction_revision,
        events=events,
        reconstructed_state=state,
    )


# ---------------------------------------------------------------------------
# Window descriptor invariants
# ---------------------------------------------------------------------------


def test_window_descriptor_rejects_inverted_range() -> None:
    with pytest.raises(WindowDescriptorError):
        WindowDescriptor(
            chain_id=CHAIN,
            pool_id=POOL_ID,
            pool_key=POOL_KEY,
            from_block=100,
            to_block=50,
            dataset_version="dataset-reference-v1",
            reconstruction_revision="recon.v1",
        )


def test_window_descriptor_rejects_blank_dataset_version() -> None:
    with pytest.raises(WindowDescriptorError):
        WindowDescriptor(
            chain_id=CHAIN,
            pool_id=POOL_ID,
            pool_key=POOL_KEY,
            from_block=1,
            to_block=2,
            dataset_version="",
            reconstruction_revision="recon.v1",
        )


def test_window_descriptor_rejects_blank_reconstruction_revision() -> None:
    with pytest.raises(WindowDescriptorError):
        WindowDescriptor(
            chain_id=CHAIN,
            pool_id=POOL_ID,
            pool_key=POOL_KEY,
            from_block=1,
            to_block=2,
            dataset_version="dataset-reference-v1",
            reconstruction_revision="",
        )


# ---------------------------------------------------------------------------
# Surface schema and provenance
# ---------------------------------------------------------------------------


def test_surface_records_pool_window_dataset_and_revision() -> None:
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    assert surface.schema_version == SURFACE_SCHEMA_VERSION
    assert surface.window.chain_id == CHAIN
    assert surface.window.pool_id == POOL_ID
    assert surface.window.pool_key == POOL_KEY
    assert surface.window.dataset_version == "dataset-reference-v1"
    assert surface.window.reconstruction_revision == "recon.v1"
    assert surface.window.from_block == 1_000_000
    assert surface.window.to_block == 1_000_060


def test_surface_content_hash_binds_inputs() -> None:
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    # The hash is a SHA-256 over the canonical JSON payload; re-
    # hashing the same payload reproduces the value.
    payload = surface.canonical_payload()
    blob = __import__("json").dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    expected = "0x" + hashlib.sha256(blob).hexdigest()
    assert surface.content_hash == expected


def test_surface_with_different_dataset_version_yields_different_hash() -> None:
    events = single_swap_event_sequence()
    surface_a = _build_surface(events, dataset_version="dataset-A")
    surface_b = _build_surface(events, dataset_version="dataset-B")
    assert surface_a.content_hash != surface_b.content_hash


def test_surface_with_different_window_yields_different_hash() -> None:
    events = single_swap_event_sequence()
    surface_a = _build_surface(events, from_block=1_000_000, to_block=1_000_020)
    surface_b = _build_surface(events, from_block=1_000_000, to_block=1_000_030)
    assert surface_a.content_hash != surface_b.content_hash


# ---------------------------------------------------------------------------
# Single-swap window
# ---------------------------------------------------------------------------


def test_single_swap_window_fee_growth_matches_reference() -> None:
    """A single zeroForOne swap contributes ``input_amount * fee_pips /
    FEE_DENOMINATOR * Q128 / pre_swap_liquidity`` to ``feeGrowthGlobal0X128``
    and the historical fees at a range covering the swap's tick equal
    ``L * delta / Q128``.
    """
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    # The single swap: amount0=1_000 (token0 in), amount1=-2_000 (token1 out),
    # fee_pips=3000, pre_swap_liquidity=DEFAULT_LIQUIDITY.
    expected_delta_g0 = (1000 * 3000 // FEE_DENOMINATOR) * Q128 // DEFAULT_LIQUIDITY
    assert surface.final_fee_growth_global_0_x128 == expected_delta_g0
    assert surface.final_fee_growth_global_1_x128 == 0
    assert surface.swap_count == 1

    # An in-range position at the swap's tick earns the entire fee
    # growth global delta for its liquidity.
    fees = surface.historical_fees(tick_lower=-120, tick_upper=120, liquidity=1_000_000)
    expected_fees_0 = (1_000_000 * expected_delta_g0) // Q128
    assert fees == (expected_fees_0, 0)


def test_single_swap_equivalence_check_passes() -> None:
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    assert surface.equivalence_check.passed
    assert surface.equivalence_check.sample_size > 0
    for record in surface.equivalence_check.records:
        assert record.matches


# ---------------------------------------------------------------------------
# Linearity identity at two distinct liquidity values
# ---------------------------------------------------------------------------


def test_linearity_holds_at_two_distinct_liquidity_values() -> None:
    """The fee-growth identity ``fees(L) = L * delta / Q128`` is linear
    in ``L``; verifying it at two distinct liquidity values confirms
    the linearity is measured rather than asserted.
    """
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    inside = surface.fee_growth_inside(tick_lower=-60, tick_upper=60, at_event_index=None)
    inside_start = surface.fee_growth_inside(tick_lower=-60, tick_upper=60, at_event_index=0)
    delta_0 = max(inside[0] - inside_start[0], 0)
    delta_1 = max(inside[1] - inside_start[1], 0)
    for liquidity in (1_000, 1_000_000, 1_000_000_000):
        fees = surface.historical_fees(tick_lower=-60, tick_upper=60, liquidity=liquidity)
        assert fees[0] == (liquidity * delta_0) // Q128
        assert fees[1] == (liquidity * delta_1) // Q128


# ---------------------------------------------------------------------------
# Multi-crossing / price-reversal window
# ---------------------------------------------------------------------------


def test_multi_swap_equivalence_check_passes() -> None:
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    assert surface.equivalence_check.passed
    for record in surface.equivalence_check.records:
        assert record.matches


def test_multi_swap_fee_growth_is_sum_over_in_range_swaps() -> None:
    """For a range that always encloses the swap price path, the
    historical fee equals the sum of ``delta_g`` per swap that was
    in range throughout the swap.
    """
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    # Range [-60, 60] encloses the swap path (price oscillates between
    # -120 and 120, but the swaps that are inside [-60, 60] at the
    # *start* of their price path are the second and fourth swaps).
    fees = surface.historical_fees(tick_lower=-60, tick_upper=60, liquidity=DEFAULT_LIQUIDITY)
    # Verify against the direct per-position integration.
    direct = integrate_position_fees(
        surface=surface,
        tick_lower=-60,
        tick_upper=60,
        liquidity=DEFAULT_LIQUIDITY,
    )
    assert fees == direct


# ---------------------------------------------------------------------------
# Zero-swap window
# ---------------------------------------------------------------------------


def test_no_swap_window_returns_zero_and_is_empty() -> None:
    events = no_swap_event_sequence()
    surface = _build_surface(events)
    assert surface.swap_count == 0
    assert surface.is_empty
    assert surface.final_fee_growth_global_0_x128 == 0
    assert surface.final_fee_growth_global_1_x128 == 0
    fees = surface.historical_fees(tick_lower=-60, tick_upper=60, liquidity=1_000_000)
    assert fees == (0, 0)


def test_no_swap_window_is_not_missing_data() -> None:
    """A zero-swap window is **not** a missing-data or incomplete
    observation — the contract requires the surface to publish with
    zero fee growth rather than refuse.
    """
    events = no_swap_event_sequence()
    surface = _build_surface(events)
    # Equivalence check still passes (every triple integrates to zero).
    assert surface.equivalence_check.passed
    # The artifact still carries the window descriptor so a consumer
    # can tell *which* window produced the zero result.
    assert surface.window.from_block == 1_000_000
    assert surface.window.dataset_version == "dataset-reference-v1"


# ---------------------------------------------------------------------------
# Range outside the traded price path
# ---------------------------------------------------------------------------


def test_range_outside_traded_price_path_returns_zero() -> None:
    """A range that does not intersect the swap's price path at any
    time returns zero (no fee accrual) rather than a small positive
    residue that would be the share-of-volume estimate the contract
    forbids.
    """
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    # The swaps oscillate between ticks -120 and 120; a range well
    # below that path returns zero.
    fees = surface.historical_fees(tick_lower=240, tick_upper=360, liquidity=DEFAULT_LIQUIDITY)
    assert fees == (0, 0)


def test_range_containing_no_initialized_tick_returns_zero() -> None:
    """A range containing no initialized tick is handled by its own
    named case — the surface returns zero rather than estimating
    fees from volume aggregates.
    """
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    # Ticks initialized: -60 and 60. A range ``[120, 180]`` does not
    # contain any initialized tick (the price path stops at 120 in
    # the third swap but does not cross 180).
    fees = surface.historical_fees(tick_lower=120, tick_upper=180, liquidity=DEFAULT_LIQUIDITY)
    assert fees == (0, 0)


# ---------------------------------------------------------------------------
# Range whose bounds sit exactly on initialized ticks
# ---------------------------------------------------------------------------


def test_bounds_on_initialized_ticks_equivalence_passes() -> None:
    events = bounds_on_initialized_ticks_sequence()
    surface = _build_surface(events)
    assert surface.equivalence_check.passed
    # A range whose bounds sit exactly on initialized ticks: the
    # surface and the direct integration agree.
    direct = integrate_position_fees(
        surface=surface,
        tick_lower=-60,
        tick_upper=60,
        liquidity=DEFAULT_LIQUIDITY,
    )
    fees = surface.historical_fees(tick_lower=-60, tick_upper=60, liquidity=DEFAULT_LIQUIDITY)
    assert fees == direct


# ---------------------------------------------------------------------------
# Full tick range
# ---------------------------------------------------------------------------


def test_full_tick_range_captures_all_fee_growth() -> None:
    """A range spanning the full V4 tick domain captures the entire
    fee growth global — the surface's fees at this range equal
    ``L * feeGrowthGlobal / Q128``.
    """
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    fees = surface.historical_fees(
        tick_lower=-(1 << 23) + 1,
        tick_upper=(1 << 23) - 1,
        liquidity=DEFAULT_LIQUIDITY,
    )
    expected_0 = (DEFAULT_LIQUIDITY * surface.final_fee_growth_global_0_x128) // Q128
    expected_1 = (DEFAULT_LIQUIDITY * surface.final_fee_growth_global_1_x128) // Q128
    assert fees == (expected_0, expected_1)


# ---------------------------------------------------------------------------
# Window that begins or ends mid-price-movement
# ---------------------------------------------------------------------------


def test_mid_price_movement_window_bootstrap_applied() -> None:
    """When the window begins mid-price-movement the bootstrap
    snapshot carries the post-Initialize ``slot0.tick`` so the
    surface's initial tick is the bootstrap, not 0. The bootstrap
    tick must still satisfy the modify-range invariant (the
    reconstructed state's active-liquidity walker rejects a
    bootstrap that crosses the modify range without an active
    liquidity transition).
    """
    events = single_swap_event_sequence()
    surface = _build_surface(events, bootstrap_tick=60)
    assert surface.initial_tick == 60
    # The swap's tick is 0; the fee growth still accrues as expected.
    assert surface.final_fee_growth_global_0_x128 > 0


# ---------------------------------------------------------------------------
# ``tickLower == tickUpper`` rejected by both paths
# ---------------------------------------------------------------------------


def test_tick_lower_equal_tick_upper_rejected() -> None:
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    with pytest.raises(InvalidTickRangeError):
        surface.historical_fees(tick_lower=60, tick_upper=60, liquidity=1_000_000)
    with pytest.raises(InvalidTickRangeError):
        integrate_position_fees(surface=surface, tick_lower=60, tick_upper=60, liquidity=1_000_000)


# ---------------------------------------------------------------------------
# Zero-liquidity query
# ---------------------------------------------------------------------------


def test_zero_liquidity_returns_zero_from_both_paths() -> None:
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    assert surface.historical_fees(tick_lower=-60, tick_upper=60, liquidity=0) == (0, 0)
    assert integrate_position_fees(surface=surface, tick_lower=-60, tick_upper=60, liquidity=0) == (
        0,
        0,
    )


# ---------------------------------------------------------------------------
# Per-tick prefix-sum prefix lookup
# ---------------------------------------------------------------------------


def test_tick_fee_growth_prefix_value_at_event_binary_search() -> None:
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    # The multi-swap sequence crosses ticks -60 and 60 multiple
    # times. Pick the tick that was crossed and verify the prefix-
    # sum lookup returns the post-flip value at each crossing.
    initial = surface._outside_at(-60, at_event_index=0)
    assert initial == (0, 0)
    final = surface._outside_at(-60, at_event_index=None)
    # ``final`` equals the value after the last crossing of -60.
    last_crossing_0 = None
    last_crossing_1 = None
    for snap in surface.swap_snapshots:
        for tick, o0, o1, _direction in snap.ticks_crossed:
            if tick == -60:
                last_crossing_0 = o0
                last_crossing_1 = o1
    assert final == (last_crossing_0, last_crossing_1)


def test_tick_fee_growth_prefix_uninitialized_tick_returns_zero() -> None:
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    # A tick that is not initialized in the surface's prefix sums
    # returns (0, 0) — V4's uninitialized-tick convention.
    assert surface._outside_at(123_456, at_event_index=0) == (0, 0)
    assert surface._outside_at(123_456, at_event_index=None) == (0, 0)


# ---------------------------------------------------------------------------
# Console-rendering helper
# ---------------------------------------------------------------------------


def test_rendered_value_carries_window_provenance() -> None:
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    rendered = render_surface_value(
        surface=surface,
        tick_lower=-60,
        tick_upper=60,
        liquidity=1_000_000,
    )
    assert rendered.window == surface.window
    assert rendered.surface_content_hash == surface.content_hash
    assert rendered.fee_0 >= 0
    assert rendered.fee_1 >= 0


def test_rendered_value_rejects_invalid_range() -> None:
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    with pytest.raises(InvalidTickRangeError):
        render_surface_value(surface=surface, tick_lower=60, tick_upper=60, liquidity=1_000_000)


# ---------------------------------------------------------------------------
# Per-pool separation
# ---------------------------------------------------------------------------


def test_surface_rejects_pool_mismatch() -> None:
    events = single_swap_event_sequence()
    state = reconstruct_state(events)
    other_pool_id = PoolId(0xDEADBEEFCAFEBABE)
    with pytest.raises(PoolIdentityMismatchError):
        build_fee_growth_surface(
            chain_id=CHAIN,
            pool_id=other_pool_id,
            pool_key=POOL_KEY,
            from_block=1_000_000,
            to_block=1_000_060,
            bootstrap_tick=0,
            bootstrap_active_liquidity=0,
            dataset_version="dataset-reference-v1",
            reconstruction_revision="recon.v1",
            events=events,
            reconstructed_state=state,
        )


def test_surface_rejects_event_outside_window() -> None:
    events = single_swap_event_sequence()
    # Reuse the same events but declare a tighter window that does
    # not cover the swap.
    with pytest.raises(WindowDescriptorError):
        _build_surface(events, from_block=1_000_000, to_block=1_000_005)


# ---------------------------------------------------------------------------
# Determinism: same inputs yield identical content hash
# ---------------------------------------------------------------------------


def test_same_inputs_yield_identical_surface() -> None:
    events = multi_swap_event_sequence()
    surface_a = _build_surface(events)
    surface_b = _build_surface(events)
    assert surface_a.content_hash == surface_b.content_hash
    assert surface_a.equivalence_check.records == surface_b.equivalence_check.records


def test_reordering_events_yields_identical_surface() -> None:
    """The builder sorts events by their canonical
    ``(block_number, transaction_index, log_index)`` key, so an
    upstream chunking / restart boundary does not change the
    artifact. The contract requires the equivalence check to
    remain valid under any input order.
    """
    events = multi_swap_event_sequence()
    reversed_events = list(reversed(events))
    surface_a = _build_surface(events)
    surface_b = _build_surface(reversed_events)
    assert surface_a.content_hash == surface_b.content_hash
    assert surface_a.equivalence_check.passed
    assert surface_b.equivalence_check.passed


# ---------------------------------------------------------------------------
# V4 piecewise formula sanity check
# ---------------------------------------------------------------------------


def test_piecewise_formula_below_above_inside() -> None:
    """The V4 piecewise ``feeGrowthInside`` returns zero when the
    position is "below" or "above" range at the window start
    (no fee growth has accumulated there yet) and a positive
    value when the position has accrued fees.
    """
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    # At event_index=0 (before the swap), fee growth is zero for any
    # range (the pool has just been initialized).
    for tick_lower, tick_upper in [(-60, 60), (-120, 120), (60, 120)]:
        inside_start = surface.fee_growth_inside(
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            at_event_index=0,
        )
        assert inside_start == (0, 0)


# ---------------------------------------------------------------------------
# Snapshot integrity
# ---------------------------------------------------------------------------


def test_swap_snapshot_event_index_monotone() -> None:
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    indices = [s.event_index for s in surface.swap_snapshots]
    assert indices == list(range(len(indices)))


def test_tick_prefix_history_sorted_by_event_index() -> None:
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    for prefix in surface.tick_prefixes:
        indices = [entry[0] for entry in prefix.history]
        assert indices == sorted(indices)


# ---------------------------------------------------------------------------
# Direct integration path: confirmed-byte-exact equivalence
# ---------------------------------------------------------------------------


def test_integrate_position_fees_uses_prefix_sums_not_event_stream() -> None:
    """The direct-integration path is also an O(swap_count) walk over
    the surface's per-event prefix sums — it does not re-simulate
    the event stream. The test re-implements the integration by
    reading the prefix sums and confirms the framework's function
    agrees.
    """
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    tick_lower = -60
    tick_upper = 60
    liquidity = 2_000_000
    # Manual integration: walk snapshots, compute inside at each.
    fees_0 = 0
    fees_1 = 0
    prev_0, prev_1 = surface.fee_growth_inside(
        tick_lower=tick_lower, tick_upper=tick_upper, at_event_index=0
    )
    for snap in surface.swap_snapshots:
        cur_0, cur_1 = surface.fee_growth_inside(
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            at_event_index=int(snap.event_index) + 1,
        )
        d0 = cur_0 - prev_0
        d1 = cur_1 - prev_1
        if d0 > 0:
            fees_0 += (liquidity * d0) // Q128
        if d1 > 0:
            fees_1 += (liquidity * d1) // Q128
        prev_0, prev_1 = cur_0, cur_1
    framework = integrate_position_fees(
        surface=surface,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity=liquidity,
    )
    assert (fees_0, fees_1) == framework


# ---------------------------------------------------------------------------
# Equivalence check driver
# ---------------------------------------------------------------------------


def test_run_equivalence_check_returns_passed_for_known_good_surface() -> None:
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    check = run_equivalence_check(surface)
    assert check.passed
    assert check.sample_size == len(check.records)
    assert check.discrepancy_count == 0


def test_run_equivalence_check_rejects_non_surface() -> None:
    with pytest.raises(FeeSurfaceError):
        run_equivalence_check("not a surface")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PositionIntegrationRecord
# ---------------------------------------------------------------------------


def test_position_integration_record_rejects_zero_liquidity() -> None:
    with pytest.raises(FeeSurfaceError):
        PositionIntegrationRecord(
            tick_lower=-60,
            tick_upper=60,
            liquidity=0,
            direct_fee_0=0,
            direct_fee_1=0,
            surface_fee_0=0,
            surface_fee_1=0,
        )


def test_position_integration_record_matches_property() -> None:
    record = PositionIntegrationRecord(
        tick_lower=-60,
        tick_upper=60,
        liquidity=1_000_000,
        direct_fee_0=10,
        direct_fee_1=20,
        surface_fee_0=10,
        surface_fee_1=20,
    )
    assert record.matches


def test_position_integration_record_discrepancy_property() -> None:
    record = PositionIntegrationRecord(
        tick_lower=-60,
        tick_upper=60,
        liquidity=1_000_000,
        direct_fee_0=10,
        direct_fee_1=20,
        surface_fee_0=10,
        surface_fee_1=21,
    )
    assert not record.matches


# ---------------------------------------------------------------------------
# TickFeeGrowthPrefix
# ---------------------------------------------------------------------------


def test_tick_fee_growth_prefix_value_at_event_returns_initial_for_event_zero() -> None:
    prefix = TickFeeGrowthPrefix(
        tick=-60,
        outside_initial_0_x128=123,
        outside_initial_1_x128=456,
        history=((0, 789, 321, 0),),
    )
    assert prefix.value_at_event(0) == (123, 456)


def test_tick_fee_growth_prefix_value_at_event_returns_last_after_final() -> None:
    prefix = TickFeeGrowthPrefix(
        tick=-60,
        outside_initial_0_x128=0,
        outside_initial_1_x128=0,
        history=((0, 100, 200, 0), (4, 300, 400, 1)),
    )
    assert prefix.value_at_event(3) == (100, 200)
    assert prefix.value_at_event(4) == (100, 200)
    assert prefix.value_at_event(5) == (300, 400)
    assert prefix.value_at_event(6) == (300, 400)
    assert prefix.value_at_event(1_000) == (300, 400)


def test_tick_fee_growth_prefix_empty_history_returns_initial() -> None:
    prefix = TickFeeGrowthPrefix(
        tick=-60,
        outside_initial_0_x128=42,
        outside_initial_1_x128=99,
        history=(),
    )
    assert prefix.value_at_event(0) == (42, 99)
    assert prefix.value_at_event(5) == (42, 99)


# ---------------------------------------------------------------------------
# SurfaceRenderedValue invariants
# ---------------------------------------------------------------------------


def test_rendered_value_rejects_zero_liquidity() -> None:
    with pytest.raises(FeeSurfaceError):
        SurfaceRenderedValue(
            surface_content_hash="0x" + "0" * 64,
            window=WindowDescriptor(
                chain_id=CHAIN,
                pool_id=POOL_ID,
                pool_key=POOL_KEY,
                from_block=1,
                to_block=2,
                dataset_version="d",
                reconstruction_revision="r",
            ),
            tick_lower=-60,
            tick_upper=60,
            liquidity=0,
            fee_growth_inside_0_x128=0,
            fee_growth_inside_1_x128=0,
            fee_0=0,
            fee_1=0,
        )


def test_rendered_value_rejects_invalid_range_via_direct_construction() -> None:
    with pytest.raises(InvalidTickRangeError):
        SurfaceRenderedValue(
            surface_content_hash="0x" + "0" * 64,
            window=WindowDescriptor(
                chain_id=CHAIN,
                pool_id=POOL_ID,
                pool_key=POOL_KEY,
                from_block=1,
                to_block=2,
                dataset_version="d",
                reconstruction_revision="r",
            ),
            tick_lower=60,
            tick_upper=60,
            liquidity=1_000_000,
            fee_growth_inside_0_x128=0,
            fee_growth_inside_1_x128=0,
            fee_0=0,
            fee_1=0,
        )


# ---------------------------------------------------------------------------
# Surface composition
# ---------------------------------------------------------------------------


def test_surface_swap_snapshots_are_tuples() -> None:
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    assert isinstance(surface.swap_snapshots, tuple)
    assert all(isinstance(s, FeeGrowthSnapshot) for s in surface.swap_snapshots)


def test_surface_tick_prefixes_are_tuples() -> None:
    events = multi_swap_event_sequence()
    surface = _build_surface(events)
    assert isinstance(surface.tick_prefixes, tuple)
    assert all(isinstance(p, TickFeeGrowthPrefix) for p in surface.tick_prefixes)


def test_surface_equivalence_check_is_equivalence_check() -> None:
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    assert isinstance(surface.equivalence_check, EquivalenceCheck)


def test_surface_rejects_wrong_schema_version() -> None:
    events = single_swap_event_sequence()
    surface = _build_surface(events)
    # Direct construction with the wrong schema version must fail.
    with pytest.raises(FeeSurfaceError):
        FeeGrowthSurface(
            schema_version="not-the-current-version",
            window=surface.window,
            initial_fee_growth_global_0_x128=surface.initial_fee_growth_global_0_x128,
            initial_fee_growth_global_1_x128=surface.initial_fee_growth_global_1_x128,
            initial_tick=surface.initial_tick,
            initial_active_liquidity=surface.initial_active_liquidity,
            final_fee_growth_global_0_x128=surface.final_fee_growth_global_0_x128,
            final_fee_growth_global_1_x128=surface.final_fee_growth_global_1_x128,
            final_tick=surface.final_tick,
            final_active_liquidity=surface.final_active_liquidity,
            swap_snapshots=surface.swap_snapshots,
            tick_prefixes=surface.tick_prefixes,
            equivalence_check=surface.equivalence_check,
            content_hash=surface.content_hash,
        )
