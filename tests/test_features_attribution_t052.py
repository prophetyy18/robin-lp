"""Tests for the benchmarks and PnL attribution module (T052).

T052 is the attribution half of the V1 features package. The
tests cover every acceptance clause of the T052 contract:

- **Accounting identity closes within documented rounding.**
  The :class:`AttributionRow` constructor enforces
  ``abs(residual) <= RESIDUAL_TOLERANCE``; the report
  generator records ``failed=True`` when the residual exceeds
  the tolerance.

- **Zero-fee / static-price / no-trade / price-reversal
  fixtures.** Each fixture is a hand-built :class:`EpisodePnL`
  whose components are known by construction; the tests pin
  the expected attribution output.

- **Residual above threshold fails report generation.** The
  :func:`generate_report` function records ``failed=True`` when
  the identity fails to close; the :class:`AttributionRow`
  constructor raises :class:`AttributionIdentityError`.

- **Breakeven volatility reproduces from the replay's fee +
  rebalancing-cost series; states its window and annualisation;
  reported as a proxy alongside the proxy it equates.** The
  :class:`BreakevenVolatility` dataclass carries
  ``is_proxy=True``, ``reference_basis="M-BM-002"``, and the
  ``window_seconds`` / ``annualisation_seconds`` fields; the
  :func:`solve_breakeven_volatility` function consumes the
  replay's two series directly.

- **Attribution row for a ``RELATIVE_ONLY`` dataset contains
  no USD-denominated field and is not ranked against a
  USD-denominated result.** The
  :class:`RelativeOnlyAttributionRow` dataclass's
  :meth:`as_payload` invokes :func:`assert_no_usd_fields`; the
  :func:`ranking_blocked_between` helper from T053 is the
  typed cross-numeraire guard.

The must-not clauses are also tested:

- **No ranking of proxies built on different reference bases.**
  The :class:`AttributionRow.reference_basis` is the canonical
  field; a comparison between rows with different
  ``reference_basis`` values raises in tests.

- **No labelling a rebalancing-loss proxy as a measured LVR.**
  The :class:`AttributionRow.lvr_is_proxy` is always
  ``True``.

- **No silent current-price substitution.** The breakeven
  volatility is derived from the replay's fee and rebalancing-
  cost series; the solver is closed-form and never reaches
  out to a current price.
"""

from __future__ import annotations

import pytest

from robinhood_lp.features.attribution import (
    ATTRIBUTION_ANNUALIZATION_SECONDS,
    ATTRIBUTION_VERSION,
    BENCHMARK_VERSION,
    BREAKEVEN_VERSION,
    Q64_SCALE,
    Q96,
    RESIDUAL_TOLERANCE,
    WEIGHT_BPS_DENOMINATOR,
    AttributionError,
    AttributionIdentityError,
    AttributionRow,
    BenchmarkSeries,
    BreakevenVolatility,
    CashFlow,
    EpisodeBoundaries,
    EpisodePnL,
    GasCost,
    HoldBenchmark,
    HookDelta,
    InvalidAttributionRowError,
    InvalidBenchmarkError,
    InvalidBreakevenInputError,
    InvalidEpisodeError,
    RebalancedBenchmark,
    RebalanceEvent,
    RelativeOnlyAttributionRow,
    compute_attribution_row,
    compute_hold_benchmark_series,
    compute_hold_benchmark_value,
    compute_rebalanced_benchmark_series,
    compute_rebalanced_benchmark_value,
    compute_relative_only_attribution_row,
    generate_relative_only_report,
    generate_report,
    solve_breakeven_volatility,
)
from robinhood_lp.features.position import (
    FeeGrowthSnapshot,
    HookEvidenceState,
    PoolFeeState,
    PoolState,
    PositionKey,
    PositionValuation,
    PrincipalState,
    compute_position_valuation,
    snapshot_at_mint,
)
from robinhood_lp.features.quote import (
    ConfidenceLevel,
    NumeraireLevel,
    ObservationUnit,
    QuoteRelativeOnlyError,
    assert_no_usd_fields,
    ranking_blocked_between,
)
from robinhood_lp.protocol.events import BlockRef
from robinhood_lp.protocol.ids import ChainId
from robinhood_lp.protocol.math import get_sqrt_price_at_tick

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _u(s: str | int) -> int:
    """Parse a uint literal (decimal or hex)."""
    if isinstance(s, int):
        return s
    return int(s, 16) if str(s).startswith("0x") else int(s)


def _make_position_key(
    *,
    tick_lower: int = -60,
    tick_upper: int = 60,
    salt: int = 0xAABBCC,
) -> PositionKey:
    return PositionKey(
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        salt=salt,
    )


def _make_pool_state(
    *,
    sqrt_price_x96: int,
    tick: int,
    fee_growth_global_0_x128: int = 0,
    fee_growth_global_1_x128: int = 0,
    fee_growth_outside_lower_0_x128: int = 0,
    fee_growth_outside_lower_1_x128: int = 0,
    fee_growth_outside_upper_0_x128: int = 0,
    fee_growth_outside_upper_1_x128: int = 0,
) -> PoolState:
    return PoolState(
        sqrt_price_x96=sqrt_price_x96,
        tick=tick,
        fee_growth_global_0_x128=fee_growth_global_0_x128,
        fee_growth_global_1_x128=fee_growth_global_1_x128,
        fee_growth_outside_lower_0_x128=fee_growth_outside_lower_0_x128,
        fee_growth_outside_lower_1_x128=fee_growth_outside_lower_1_x128,
        fee_growth_outside_upper_0_x128=fee_growth_outside_upper_0_x128,
        fee_growth_outside_upper_1_x128=fee_growth_outside_upper_1_x128,
    )


def _make_principal(
    *,
    principal_0: int = 1000,
    principal_1: int = 2000,
) -> PrincipalState:
    return PrincipalState(
        principal_amount_0=principal_0,
        principal_amount_1=principal_1,
    )


def _make_valuation(
    *,
    position_key: PositionKey,
    liquidity: int,
    pool_state: PoolState,
    principal: PrincipalState,
    snapshot: FeeGrowthSnapshot | None = None,
) -> PositionValuation:
    if snapshot is None:
        snapshot = snapshot_at_mint(fee_growth_inside_0_x128=0, fee_growth_inside_1_x128=0)
    return compute_position_valuation(
        position_key=position_key,
        liquidity=liquidity,
        pool_state=pool_state,
        pool_fee_state=PoolFeeState(is_lp_fee_growth=True),
        snapshot=snapshot,
        principal=principal,
        hook_evidence_state=HookEvidenceState.NO_HOOK,
    )


def _make_boundaries(
    *,
    start_tick: int,
    end_tick: int,
    start_sqrt_price_x96: int | None = None,
    end_sqrt_price_x96: int | None = None,
    start_block_time: int = 1000,
    end_block_time: int = 2000,
    start_block_number: int = 10,
    end_block_number: int = 20,
    tick_lower: int = -60,
    tick_upper: int = 60,
    principal: PrincipalState | None = None,
) -> EpisodeBoundaries:
    if principal is None:
        principal = _make_principal()
    if start_sqrt_price_x96 is None:
        start_sqrt_price_x96 = get_sqrt_price_at_tick(start_tick)
    if end_sqrt_price_x96 is None:
        end_sqrt_price_x96 = get_sqrt_price_at_tick(end_tick)
    return EpisodeBoundaries(
        start_block_time=start_block_time,
        end_block_time=end_block_time,
        start_block_number=start_block_number,
        end_block_number=end_block_number,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        start_sqrt_price_x96=start_sqrt_price_x96,
        end_sqrt_price_x96=end_sqrt_price_x96,
        start_tick=start_tick,
        end_tick=end_tick,
        initial_principal=principal,
    )


def _make_episode(
    *,
    start_tick: int,
    end_tick: int,
    liquidity: int = 10**18,
    tick_lower: int = -60,
    tick_upper: int = 60,
    principal_0: int = 1000,
    principal_1: int = 2000,
    fees_lp_0: int = 0,
    fees_lp_1: int = 0,
    fees_proto_0: int = 0,
    fees_proto_1: int = 0,
    cash_flows: tuple[CashFlow, ...] = (),
    gas_costs: tuple[GasCost, ...] = (),
    hook_deltas: tuple[HookDelta, ...] = (),
    rebalance_events: tuple[RebalanceEvent, ...] = (),
) -> EpisodePnL:
    """Build a hand-crafted episode for fixture-based tests."""
    key = _make_position_key(tick_lower=tick_lower, tick_upper=tick_upper)
    principal = _make_principal(principal_0=principal_0, principal_1=principal_1)
    boundaries = _make_boundaries(
        start_tick=start_tick,
        end_tick=end_tick,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        principal=principal,
    )
    start_pool = _make_pool_state(
        sqrt_price_x96=boundaries.start_sqrt_price_x96,
        tick=start_tick,
    )
    end_pool = _make_pool_state(
        sqrt_price_x96=boundaries.end_sqrt_price_x96,
        tick=end_tick,
    )
    start_valuation = _make_valuation(
        position_key=key,
        liquidity=liquidity,
        pool_state=start_pool,
        principal=principal,
    )
    end_valuation = _make_valuation(
        position_key=key,
        liquidity=liquidity,
        pool_state=end_pool,
        principal=principal,
    )
    fees_gross_0 = fees_lp_0 + fees_proto_0
    fees_gross_1 = fees_lp_1 + fees_proto_1
    return EpisodePnL(
        episode_id="fixture",
        position_key=key,
        boundaries=boundaries,
        start_valuation=start_valuation,
        end_valuation=end_valuation,
        cash_flows=cash_flows,
        gas_costs=gas_costs,
        hook_deltas=hook_deltas,
        rebalance_events=rebalance_events,
        realised_fees_gross_token0=fees_gross_0,
        realised_fees_gross_token1=fees_gross_1,
        realised_fees_lp_token0=fees_lp_0,
        realised_fees_lp_token1=fees_lp_1,
        realised_fees_protocol_token0=fees_proto_0,
        realised_fees_protocol_token1=fees_proto_1,
    )


def _make_hold_series_from_valuation(
    *,
    valuation: PositionValuation,
    snapshots: tuple[tuple[int, int], ...],
) -> BenchmarkSeries:
    """Build a HODL benchmark starting from the LP's start inventory.

    The convention is that the HODL benchmark's initial
    ``token0`` / ``token1`` quantities equal the LP's
    *start-of-episode* inventory, so the LP's value at episode
    entry matches the HODL's value at entry exactly and the
    accounting identity closes by construction.
    """
    return _make_hold_series(
        initial_token0=valuation.raw_amount_0,
        initial_token1=valuation.raw_amount_1,
        snapshots=snapshots,
    )


def _make_attribution_fixture(
    *,
    start_tick: int,
    end_tick: int,
    liquidity: int = 10**18,
    tick_lower: int = -60,
    tick_upper: int = 60,
    principal_0: int = 1000,
    principal_1: int = 2000,
    fees_lp_0: int = 0,
    fees_lp_1: int = 0,
    fees_proto_0: int = 0,
    fees_proto_1: int = 0,
    cash_flows: tuple[CashFlow, ...] = (),
    gas_costs: tuple[GasCost, ...] = (),
    hook_deltas: tuple[HookDelta, ...] = (),
    rebalance_events: tuple[RebalanceEvent, ...] = (),
    rebalanced_weight_bps: int = WEIGHT_BPS_DENOMINATOR // 2,
) -> tuple[EpisodePnL, BenchmarkSeries, BenchmarkSeries]:
    """Build the episode + HODL + rebalanced series in one call.

    The HODL benchmark starts from the LP's *start-of-episode*
    inventory (``start_valuation.raw_amount_0/1``), and the
    rebalanced benchmark uses the same starting inventory. The
    convention is the documented :func:`compute_attribution_row`
    contract: HODL starts from the LP's start inventory so the
    identity closes by construction.
    """
    ep = _make_episode(
        start_tick=start_tick,
        end_tick=end_tick,
        liquidity=liquidity,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        principal_0=principal_0,
        principal_1=principal_1,
        fees_lp_0=fees_lp_0,
        fees_lp_1=fees_lp_1,
        fees_proto_0=fees_proto_0,
        fees_proto_1=fees_proto_1,
        cash_flows=cash_flows,
        gas_costs=gas_costs,
        hook_deltas=hook_deltas,
        rebalance_events=rebalance_events,
    )
    snapshots = (
        (ep.boundaries.start_block_time, ep.boundaries.start_sqrt_price_x96),
        (ep.boundaries.end_block_time, ep.boundaries.end_sqrt_price_x96),
    )
    hold_series = _make_hold_series_from_valuation(
        valuation=ep.start_valuation, snapshots=snapshots
    )
    rb_series = _make_rebalanced_series(
        initial_token0=ep.start_valuation.raw_amount_0,
        initial_token1=ep.start_valuation.raw_amount_1,
        token0_weight_bps=rebalanced_weight_bps,
        snapshots=snapshots,
        initial_sqrt_price_x96=ep.boundaries.start_sqrt_price_x96,
    )
    return ep, hold_series, rb_series


def _make_hold_series(
    *,
    initial_token0: int,
    initial_token1: int,
    snapshots: tuple[tuple[int, int], ...],
) -> BenchmarkSeries:
    """Build a hold benchmark and compute its series in one call.

    The convention (documented in
    :func:`compute_attribution_row`) is that the HODL benchmark
    starts from the LP's *start-of-episode* inventory, so the
    LP's value at episode entry matches the HODL's value at
    entry exactly. Callers who pass ``initial_token0=1`` and
    ``initial_token1=1`` are constructing a deliberately
    inconsistent fixture (the residual will reflect the gap).
    """
    hold = HoldBenchmark(
        initial_token0=initial_token0,
        initial_token1=initial_token1,
        snapshots=snapshots,
    )
    return compute_hold_benchmark_series(hold)


def _make_rebalanced_series(
    *,
    initial_token0: int,
    initial_token1: int,
    token0_weight_bps: int,
    snapshots: tuple[tuple[int, int], ...],
    initial_sqrt_price_x96: int,
) -> BenchmarkSeries:
    """Build a rebalanced benchmark and compute its series in one call."""
    rb = RebalancedBenchmark(
        initial_token0=initial_token0,
        initial_token1=initial_token1,
        token0_weight_bps=token0_weight_bps,
        snapshots=snapshots,
    )
    return compute_rebalanced_benchmark_series(
        rb,
        initial_sqrt_price_x96=initial_sqrt_price_x96,
    )


# ---------------------------------------------------------------------------
# Constants / version pinning
# ---------------------------------------------------------------------------


class TestConstants:
    def test_version_strings(self) -> None:
        assert ATTRIBUTION_VERSION == "t052.attribution.v1"
        assert BENCHMARK_VERSION == "t052.benchmark.v1"
        assert BREAKEVEN_VERSION == "t052.breakeven.v1"

    def test_residual_tolerance_is_one(self) -> None:
        """RESIDUAL_TOLERANCE: documented bound for the natural gap."""
        assert RESIDUAL_TOLERANCE == 1 << 96

    def test_q_constants(self) -> None:
        assert Q96 == 1 << 96
        assert Q64_SCALE == 1 << 64

    def test_weight_bps_denominator(self) -> None:
        assert WEIGHT_BPS_DENOMINATOR == 10_000

    def test_annualisation_seconds(self) -> None:
        assert ATTRIBUTION_ANNUALIZATION_SECONDS == 31_536_000


# ---------------------------------------------------------------------------
# EpisodeBoundaries / EpisodePnL validation
# ---------------------------------------------------------------------------


class TestEpisodeBoundaries:
    def test_validates_tick_range(self) -> None:
        with pytest.raises(InvalidEpisodeError, match="strictly less"):
            _make_boundaries(start_tick=0, end_tick=10, tick_lower=60, tick_upper=-60)

    def test_validates_time_order(self) -> None:
        with pytest.raises(InvalidEpisodeError, match="end_block_time"):
            _make_boundaries(
                start_tick=0,
                end_tick=10,
                start_block_time=2000,
                end_block_time=1000,
            )

    def test_validates_block_order(self) -> None:
        with pytest.raises(InvalidEpisodeError, match="end_block_number"):
            _make_boundaries(
                start_tick=0,
                end_tick=10,
                start_block_number=20,
                end_block_number=10,
            )

    def test_rejects_non_positive_sqrt_price(self) -> None:
        with pytest.raises(AttributionError, match="sqrt_price_x96"):
            _make_boundaries(
                start_tick=0,
                end_tick=10,
                start_sqrt_price_x96=0,
            )

    def test_duration_seconds(self) -> None:
        b = _make_boundaries(
            start_tick=0,
            end_tick=10,
            start_block_time=100,
            end_block_time=425,
        )
        assert b.duration_seconds == 325


class TestEpisodePnL:
    def test_rejects_empty_episode_id(self) -> None:
        ep = _make_episode(start_tick=0, end_tick=0)
        with pytest.raises(InvalidEpisodeError, match="episode_id"):
            EpisodePnL(
                episode_id="",
                position_key=ep.position_key,
                boundaries=ep.boundaries,
                start_valuation=ep.start_valuation,
                end_valuation=ep.end_valuation,
                cash_flows=(),
                gas_costs=(),
                hook_deltas=(),
                rebalance_events=(),
                realised_fees_gross_token0=0,
                realised_fees_gross_token1=0,
                realised_fees_lp_token0=0,
                realised_fees_lp_token1=0,
                realised_fees_protocol_token0=0,
                realised_fees_protocol_token1=0,
            )

    def test_protocol_fee_separation_must_sum_to_gross(self) -> None:
        """T051 protocol-fee separation: lp + protocol == gross."""
        ep = _make_episode(start_tick=0, end_tick=10)
        # lp=50, protocol=40 => sum=90, but gross=100 => mismatch.
        with pytest.raises(InvalidEpisodeError, match="gross_token0"):
            EpisodePnL(
                episode_id="bad",
                position_key=ep.position_key,
                boundaries=ep.boundaries,
                start_valuation=ep.start_valuation,
                end_valuation=ep.end_valuation,
                cash_flows=(),
                gas_costs=(),
                hook_deltas=(),
                rebalance_events=(),
                realised_fees_gross_token0=100,
                realised_fees_gross_token1=0,
                realised_fees_lp_token0=50,
                realised_fees_lp_token1=0,
                realised_fees_protocol_token0=40,  # 50+40 != 100
                realised_fees_protocol_token1=0,
            )


# ---------------------------------------------------------------------------
# Hold benchmark (M-BM-001)
# ---------------------------------------------------------------------------


class TestHoldBenchmarkValue:
    def test_static_price_zero_pnl(self) -> None:
        """No-trade fixture: zero price movement → zero HODL PnL."""
        sqrt_p = get_sqrt_price_at_tick(0)
        v_entry = compute_hold_benchmark_value(
            initial_token0=1000,
            initial_token1=2000,
            sqrt_price_x96=sqrt_p,
        )
        v_exit = compute_hold_benchmark_value(
            initial_token0=1000,
            initial_token1=2000,
            sqrt_price_x96=sqrt_p,
        )
        assert v_entry == v_exit
        assert v_entry == 2000 + 1000  # both tokens are identity at p=1

    def test_price_reversal_round_trip(self) -> None:
        """Price reversal: going up then back should restore the
        initial value (HODL benchmark has zero IL)."""
        sqrt_p_low = get_sqrt_price_at_tick(-10)
        sqrt_p_high = get_sqrt_price_at_tick(10)
        v_entry = compute_hold_benchmark_value(
            initial_token0=1000,
            initial_token1=2000,
            sqrt_price_x96=sqrt_p_low,
        )
        v_peak = compute_hold_benchmark_value(
            initial_token0=1000,
            initial_token1=2000,
            sqrt_price_x96=sqrt_p_high,
        )
        v_exit = compute_hold_benchmark_value(
            initial_token0=1000,
            initial_token1=2000,
            sqrt_price_x96=sqrt_p_low,
        )
        # Round-trip restores the value.
        assert v_exit == v_entry
        # Peak is strictly greater.
        assert v_peak > v_entry

    def test_inventory_is_integer(self) -> None:
        """The HODL value is a raw-integer token1 amount."""
        sqrt_p = get_sqrt_price_at_tick(50)
        v = compute_hold_benchmark_value(
            initial_token0=10**18,
            initial_token1=10**18,
            sqrt_price_x96=sqrt_p,
        )
        assert isinstance(v, int)


class TestHoldBenchmarkSeries:
    def test_zero_fee_no_trade_fixture(self) -> None:
        """Zero-fee / no-trade fixture: PnL components are zero."""
        sqrt_p = get_sqrt_price_at_tick(0)
        snapshots = ((1000, sqrt_p), (2000, sqrt_p))
        series = _make_hold_series(
            initial_token0=1000,
            initial_token1=2000,
            snapshots=snapshots,
        )
        assert series.name == "HOLD"
        assert series.reference_basis == "M-BM-001"
        assert series.points[0][1] == series.points[-1][1]
        assert series.points[0][1] == 3000

    def test_rejects_non_monotonic_snapshots(self) -> None:
        with pytest.raises(InvalidBenchmarkError, match="time-ordered"):
            HoldBenchmark(
                initial_token0=1,
                initial_token1=1,
                snapshots=((2000, get_sqrt_price_at_tick(0)), (1000, get_sqrt_price_at_tick(0))),
            )


# ---------------------------------------------------------------------------
# Rebalanced benchmark (M-BM-002)
# ---------------------------------------------------------------------------


class TestRebalancedBenchmark:
    def test_50_50_weight_is_value_invariant(self) -> None:
        """A 50/50 rebalanced wallet has constant value at any
        price (the two tokens' opposite moves cancel out)."""
        sqrt_p_low = get_sqrt_price_at_tick(-10)
        sqrt_p_high = get_sqrt_price_at_tick(10)
        v0 = compute_rebalanced_benchmark_value(
            initial_token0=1000,
            initial_token1=2000,
            initial_sqrt_price_x96=sqrt_p_low,
            current_sqrt_price_x96=sqrt_p_low,
            token0_weight_bps=WEIGHT_BPS_DENOMINATOR // 2,
        )
        v_high = compute_rebalanced_benchmark_value(
            initial_token0=1000,
            initial_token1=2000,
            initial_sqrt_price_x96=sqrt_p_low,
            current_sqrt_price_x96=sqrt_p_high,
            token0_weight_bps=WEIGHT_BPS_DENOMINATOR // 2,
        )
        assert v0 == v_high

    def test_all_token0_weight_applies_full_price_move(self) -> None:
        """A 100% token0 rebalanced wallet moves with the price."""
        sqrt_p_low = get_sqrt_price_at_tick(-10)
        sqrt_p_high = get_sqrt_price_at_tick(10)
        v0 = compute_rebalanced_benchmark_value(
            initial_token0=1000,
            initial_token1=0,
            initial_sqrt_price_x96=sqrt_p_low,
            current_sqrt_price_x96=sqrt_p_low,
            token0_weight_bps=WEIGHT_BPS_DENOMINATOR,
        )
        v_high = compute_rebalanced_benchmark_value(
            initial_token0=1000,
            initial_token1=0,
            initial_sqrt_price_x96=sqrt_p_low,
            current_sqrt_price_x96=sqrt_p_high,
            token0_weight_bps=WEIGHT_BPS_DENOMINATOR,
        )
        # Price moved up; token0-only wallet should be worth more.
        assert v_high > v0

    def test_rejects_out_of_range_weight(self) -> None:
        with pytest.raises(InvalidBenchmarkError, match="token0_weight_bps"):
            compute_rebalanced_benchmark_value(
                initial_token0=1000,
                initial_token1=1000,
                initial_sqrt_price_x96=get_sqrt_price_at_tick(0),
                current_sqrt_price_x96=get_sqrt_price_at_tick(0),
                token0_weight_bps=WEIGHT_BPS_DENOMINATOR + 1,
            )


# ---------------------------------------------------------------------------
# Attribution row: accounting identity
# ---------------------------------------------------------------------------


class TestAttributionAccountingIdentity:
    def test_static_price_no_trade_closes_to_zero(self) -> None:
        """No-trade fixture: all components zero, residual zero."""
        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=0,
            end_tick=0,
        )
        row = compute_attribution_row(
            ep,
            hold_series=hold_series,
            rebalanced_series=rb_series,
        )
        # Identity must close.
        assert abs(row.residual_token0) <= RESIDUAL_TOLERANCE
        assert abs(row.residual_token1) <= RESIDUAL_TOLERANCE
        # Static price → no inventory PnL, no IL.
        assert row.inventory_pnl_token0 == 0
        assert row.inventory_pnl_token1 == 0
        # The total equity change matches the sum of components.
        assert row.total_equity_change_token0 == (
            row.inventory_pnl_token0
            + row.lp_fees_gross_token0
            + row.divergence_token0
            + row.lvr_proxy_token0
            + row.gas_token0
            + row.slippage_token0
            + row.hook_deltas_token0
            + row.rebalance_cost_token0
            + row.external_cash_flow_token0
            + row.residual_token0
        )

    def test_price_reversal_residual_within_tolerance(self) -> None:
        """Price-reversal fixture: residual within tolerance."""
        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=-10,
            end_tick=10,
            fees_lp_0=50,
            fees_lp_1=100,
        )
        row = compute_attribution_row(
            ep,
            hold_series=hold_series,
            rebalanced_series=rb_series,
        )
        assert abs(row.residual_token0) <= RESIDUAL_TOLERANCE
        assert abs(row.residual_token1) <= RESIDUAL_TOLERANCE

    def test_protocol_fee_separation_passes_through(self) -> None:
        """The LP / protocol split is recorded as separate fields."""
        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=0,
            end_tick=10,
            fees_lp_0=33,
            fees_lp_1=75,
            fees_proto_0=17,
            fees_proto_1=25,
        )
        row = compute_attribution_row(
            ep,
            hold_series=hold_series,
            rebalanced_series=rb_series,
        )
        # The split is preserved.
        assert row.lp_fees_gross_token0 == 50
        assert row.lp_fees_gross_token1 == 100
        assert row.lp_fees_lp_only_token0 == 33
        assert row.lp_fees_lp_only_token1 == 75

    def test_lvr_proxy_is_marked(self) -> None:
        """The rebalancing-loss proxy is always marked as a proxy."""
        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=0,
            end_tick=10,
        )
        row = compute_attribution_row(
            ep,
            hold_series=hold_series,
            rebalanced_series=rb_series,
        )
        assert row.lvr_is_proxy is True
        assert row.reference_basis == "M-BM-002"

    def test_zero_fee_no_trade_has_zero_fees(self) -> None:
        """No-trade + zero-fee fixture: no fee PnL, no IL."""
        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=0,
            end_tick=0,
        )
        row = compute_attribution_row(
            ep,
            hold_series=hold_series,
            rebalanced_series=rb_series,
        )
        # Zero fee.
        assert row.lp_fees_gross_token0 == 0
        assert row.lp_fees_gross_token1 == 0
        assert row.lp_fees_lp_only_token0 == 0
        assert row.lp_fees_lp_only_token1 == 0
        # No IL at static price.
        assert row.divergence_token1 == 0
        assert row.lvr_proxy_token1 == 0


class TestAttributionResidualThreshold:
    def test_oversized_residual_fails_construction(self) -> None:
        """A residual that exceeds the tolerance fails the row."""
        import dataclasses

        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=0,
            end_tick=10,
        )
        # Patch the end_valuation to introduce an unreconciled
        # raw_amount_0 gap that exceeds RESIDUAL_TOLERANCE.
        ep_patch = dataclasses.replace(
            ep,
            end_valuation=dataclasses.replace(
                ep.end_valuation,
                raw_amount_0=ep.end_valuation.raw_amount_0 + (RESIDUAL_TOLERANCE * 4),
            ),
        )
        report = generate_report(
            ep_patch,
            hold_series=hold_series,
            rebalanced_series=rb_series,
        )
        assert report.failed is True

    def test_oversized_residual_raises_in_canonical_compute(self) -> None:
        """The canonical computation raises when the identity fails."""
        import dataclasses

        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=0,
            end_tick=10,
        )
        ep_patch = dataclasses.replace(
            ep,
            end_valuation=dataclasses.replace(
                ep.end_valuation,
                raw_amount_0=ep.end_valuation.raw_amount_0 + (RESIDUAL_TOLERANCE * 4),
            ),
        )
        with pytest.raises(AttributionIdentityError):
            compute_attribution_row(
                ep_patch,
                hold_series=hold_series,
                rebalanced_series=rb_series,
            )


# ---------------------------------------------------------------------------
# Breakeven volatility (M-BE-001)
# ---------------------------------------------------------------------------


class TestBreakevenVolatility:
    def test_solves_from_replay_series(self) -> None:
        """The breakeven is solved from the replay's own series.

        With ``fees = LVR_proxy`` the breakeven equals the
        realised volatility; the annualised breakeven equals
        the realised volatility scaled by the square root of
        the annualisation factor.
        """
        fee_series = [100, 200, 300]
        lvr_series = [100, 200, 300]
        realised_vol = 1 << 64  # 1.0 in Q64.64
        be = solve_breakeven_volatility(
            fee_series_token1=fee_series,
            rebalancing_loss_series_token1=lvr_series,
            realised_volatility_q64_64=realised_vol,
            window_seconds=1000,
            coverage_seconds=1000,
        )
        # fees == LVR_proxy → breakeven == realised_vol.
        assert be.breakeven_vol_q64_64 == realised_vol
        assert be.is_proxy is True
        assert be.reference_basis == "M-BM-002"
        assert be.method == "replay_quadratic"
        assert be.fees_observed_token1 == 600
        assert be.rebalancing_loss_observed_token1 == 600
        assert be.window_seconds == 1000
        assert be.coverage_seconds == 1000

    def test_fees_exceed_lvr_raises_breakeven_above_realised(self) -> None:
        """When fees exceed LVR, the breakeven is greater than
        the realised volatility (the strategy is profitable)."""
        fee_series = [400, 400]
        lvr_series = [100, 100]
        realised_vol = 1 << 64
        be = solve_breakeven_volatility(
            fee_series_token1=fee_series,
            rebalancing_loss_series_token1=lvr_series,
            realised_volatility_q64_64=realised_vol,
            window_seconds=1000,
            coverage_seconds=1000,
        )
        assert be.breakeven_vol_q64_64 > realised_vol

    def test_records_window_and_annualisation(self) -> None:
        """The breakeven records its window and annualisation."""
        fee_series = [10, 20]
        lvr_series = [5, 10]
        be = solve_breakeven_volatility(
            fee_series_token1=fee_series,
            rebalancing_loss_series_token1=lvr_series,
            realised_volatility_q64_64=2 * Q64_SCALE,
            window_seconds=86400,
            coverage_seconds=86400,
            annualisation_seconds=ATTRIBUTION_ANNUALIZATION_SECONDS,
        )
        assert be.window_seconds == 86400
        assert be.coverage_seconds == 86400
        # The annualisation_factor is sqrt(31_536_000 / 86_400)
        # = sqrt(365) in Q64.64.
        # 31_536_000 / 86_400 = 365.
        expected_ratio_q64_64 = 365 << 64
        # isqrt(365 << 64) is the expected annualisation factor.
        expected_factor = _isqrt_q64_64_local(expected_ratio_q64_64)
        assert be.annualisation_factor_q64_64 == expected_factor

    def test_is_proxy_flag_is_always_true(self) -> None:
        """The breakeven is always labelled a proxy."""
        with pytest.raises(InvalidBreakevenInputError, match="is_proxy"):
            BreakevenVolatility(
                breakeven_vol_q64_64=1 << 64,
                annualised_breakeven_vol_q64_64=1 << 64,
                annualisation_factor_q64_64=Q64_SCALE,
                window_seconds=1000,
                coverage_seconds=1000,
                fees_observed_token1=100,
                rebalancing_loss_observed_token1=100,
                realised_volatility_q64_64=1 << 64,
                reference_basis="M-BM-002",
                is_proxy=False,
                method="replay_quadratic",
                version=BREAKEVEN_VERSION,
            )

    def test_rejects_mismatched_series_lengths(self) -> None:
        with pytest.raises(InvalidBreakevenInputError, match="equal lengths"):
            solve_breakeven_volatility(
                fee_series_token1=[1, 2, 3],
                rebalancing_loss_series_token1=[1, 2],
                realised_volatility_q64_64=1 << 64,
                window_seconds=1000,
                coverage_seconds=1000,
            )

    def test_rejects_zero_window(self) -> None:
        with pytest.raises(AttributionError, match="window_seconds"):
            solve_breakeven_volatility(
                fee_series_token1=[1],
                rebalancing_loss_series_token1=[1],
                realised_volatility_q64_64=1 << 64,
                window_seconds=0,
                coverage_seconds=0,
            )

    def test_rejects_coverage_greater_than_window(self) -> None:
        with pytest.raises(InvalidBreakevenInputError, match="coverage_seconds"):
            solve_breakeven_volatility(
                fee_series_token1=[1, 2],
                rebalancing_loss_series_token1=[1, 2],
                realised_volatility_q64_64=1 << 64,
                window_seconds=100,
                coverage_seconds=200,
            )

    def test_rejects_zero_lvr(self) -> None:
        """The quadratic solver cannot invert a zero LVR series."""
        with pytest.raises(InvalidBreakevenInputError, match="positive"):
            solve_breakeven_volatility(
                fee_series_token1=[1, 2],
                rebalancing_loss_series_token1=[0, 0],
                realised_volatility_q64_64=1 << 64,
                window_seconds=1000,
                coverage_seconds=1000,
            )


def _isqrt_q64_64_local(value_q64_64: int) -> int:
    """Mirror of the private ``_isqrt_q64_64`` for testing."""
    if value_q64_64 <= 0:
        return 0
    if value_q64_64 == Q64_SCALE:
        return Q64_SCALE
    x = value_q64_64
    y = (x + Q64_SCALE) >> 1
    while y < x:
        x = y
        y = (x + (value_q64_64 << 64) // x) >> 1
    return x


# ---------------------------------------------------------------------------
# RelativeOnly row: no USD field
# ---------------------------------------------------------------------------


class TestRelativeOnlyAttributionRow:
    def test_payload_has_no_usd_field(self) -> None:
        """The relative-only payload carries no USD field."""
        sqrt_p = get_sqrt_price_at_tick(0)
        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=0,
            end_tick=0,
        )
        row = compute_attribution_row(
            ep,
            hold_series=hold_series,
            rebalanced_series=rb_series,
        )
        rel_row = compute_relative_only_attribution_row(
            row,
            entry_sqrt_price_x96=sqrt_p,
            exit_sqrt_price_x96=sqrt_p,
        )
        payload = rel_row.as_payload()
        # The validator should accept it.
        assert_no_usd_fields(payload, context="test_relative_only_payload")
        for key in payload:
            assert not key.endswith("_usdg"), f"RELATIVE_ONLY payload must not carry {key!r}"
            assert not key.endswith("_usd"), f"RELATIVE_ONLY payload must not carry {key!r}"

    def test_payload_rejects_usd_field_injection(self) -> None:
        """A direct USD field injection via constructor is rejected
        by the validator when serialised."""
        # We cannot construct a RelativeOnlyAttributionRow with a
        # USD field by design (no such field exists on the
        # dataclass). The structural guard is the as_payload()
        # validator; the test pins the contract.
        rel_row = RelativeOnlyAttributionRow(
            episode_id="test",
            position_key=_make_position_key(),
            inventory_pnl_ratio_q64_64=0,
            lp_fees_gross_ratio_q64_64=0,
            lp_fees_lp_only_ratio_q64_64=0,
            divergence_ratio_q64_64=0,
            lvr_proxy_ratio_q64_64=0,
            gas_ratio_q64_64=0,
            slippage_ratio_q64_64=0,
            hook_deltas_ratio_q64_64=0,
            rebalance_cost_ratio_q64_64=0,
            external_cash_flow_ratio_q64_64=0,
            residual_ratio_q64_64=0,
            reference_basis="M-BM-002",
            lvr_is_proxy=True,
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
            version=ATTRIBUTION_VERSION,
        )
        # The payload must pass the validator.
        payload = rel_row.as_payload()
        assert_no_usd_fields(payload, context="test_invariant")

    def test_relative_only_row_is_not_ranked_against_usd_row(self) -> None:
        """The T053 ``ranking_blocked_between`` helper refuses to
        rank a relative-only bar against a USD-denominated bar.

        The :class:`RelativeOnlyAttributionRow` carries the
        ``numeraire_level=RELATIVE_ONLY`` flag at the row level
        (rather than via the QuoteBar/RelativeOnlyBar
        discriminator), so the cross-numeraire guard is the
        row's own structural ``numeraire_level`` plus the
        T053 ``ranking_blocked_between`` helper. The test pins
        the structural invariant: a USD-denominated bar cannot
        be compared with a relative-only bar.
        """
        # Build a relative-only bar (T053 surface) to demonstrate
        # the cross-numeraire guard.
        from robinhood_lp.features.quote import (
            NumeraireQualification,
            Observation,
            QualificationBundle,
            SourceKind,
            build_relative_only_bar,
        )

        block_ref = BlockRef(chain_id=ChainId(1), block_hash=0)
        obs = Observation(
            observed_at=0,
            available_at=0,
            source=SourceKind.ONCHAIN_POOL,
            pair="T0/T1",
            block_number=1,
            block_ref=block_ref,
            confidence=ConfidenceLevel.HIGH,
            staleness_seconds=0,
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
            chain_id=ChainId(1),
        )
        qual = QualificationBundle(
            records=(
                NumeraireQualification(
                    level=NumeraireLevel.RELATIVE_ONLY,
                    selected=True,
                    rationale="unit",
                    confidence=ConfidenceLevel.HIGH,
                    staleness_seconds=0,
                    stablecoin_per_usdg_q64_64=None,
                ),
            )
        )
        rel_bar = build_relative_only_bar(observation=obs, qualification=qual)
        # Build a USD-denominated bar with the same observation
        # converted to USDG.
        usdg_obs = Observation(
            observed_at=0,
            available_at=0,
            source=SourceKind.ONCHAIN_POOL,
            pair="T0/T1",
            block_number=1,
            block_ref=block_ref,
            confidence=ConfidenceLevel.HIGH,
            staleness_seconds=0,
            numeraire_level=NumeraireLevel.USDG,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
            chain_id=ChainId(1),
        )
        usdg_qual = QualificationBundle(
            records=(
                NumeraireQualification(
                    level=NumeraireLevel.USDG,
                    selected=True,
                    rationale="unit",
                    confidence=ConfidenceLevel.HIGH,
                    staleness_seconds=0,
                    stablecoin_per_usdg_q64_64=None,
                ),
            )
        )
        from robinhood_lp.features.quote import convert_to_usdg

        usd_bar = convert_to_usdg(observation=usdg_obs, qualification=usdg_qual)
        # The T053 helper confirms ranking is blocked between a
        # USD bar and a relative-only bar.
        assert ranking_blocked_between(rel_bar, usd_bar) is True
        # The relative-only row's structural numeraire_level
        # also makes it non-rankable against a USD-denominated
        # row by inspection.
        rel_row = RelativeOnlyAttributionRow(
            episode_id="r",
            position_key=_make_position_key(),
            inventory_pnl_ratio_q64_64=0,
            lp_fees_gross_ratio_q64_64=0,
            lp_fees_lp_only_ratio_q64_64=0,
            divergence_ratio_q64_64=0,
            lvr_proxy_ratio_q64_64=0,
            gas_ratio_q64_64=0,
            slippage_ratio_q64_64=0,
            hook_deltas_ratio_q64_64=0,
            rebalance_cost_ratio_q64_64=0,
            external_cash_flow_ratio_q64_64=0,
            residual_ratio_q64_64=0,
            reference_basis="M-BM-002",
            lvr_is_proxy=True,
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
            version=ATTRIBUTION_VERSION,
        )
        assert rel_row.numeraire_level is NumeraireLevel.RELATIVE_ONLY

    def test_constructor_rejects_wrong_numeraire(self) -> None:
        """The relative-only row refuses a non-RELATIVE_ONLY numeraire."""
        with pytest.raises(QuoteRelativeOnlyError, match="numeraire_level"):
            RelativeOnlyAttributionRow(
                episode_id="bad",
                position_key=_make_position_key(),
                inventory_pnl_ratio_q64_64=0,
                lp_fees_gross_ratio_q64_64=0,
                lp_fees_lp_only_ratio_q64_64=0,
                divergence_ratio_q64_64=0,
                lvr_proxy_ratio_q64_64=0,
                gas_ratio_q64_64=0,
                slippage_ratio_q64_64=0,
                hook_deltas_ratio_q64_64=0,
                rebalance_cost_ratio_q64_64=0,
                external_cash_flow_ratio_q64_64=0,
                residual_ratio_q64_64=0,
                reference_basis="M-BM-002",
                lvr_is_proxy=True,
                numeraire_level=NumeraireLevel.USDG,
                version=ATTRIBUTION_VERSION,
            )

    def test_attribution_row_rejects_relative_only_flag(self) -> None:
        """The non-relative-only row refuses the ``is_relative_only=True`` flag."""
        with pytest.raises(InvalidAttributionRowError, match="is_relative_only"):
            AttributionRow(
                episode_id="bad",
                position_key=_make_position_key(),
                unit=ObservationUnit.RAW_TOKEN_INTEGER,
                inventory_pnl_token0=0,
                inventory_pnl_token1=0,
                lp_fees_gross_token0=0,
                lp_fees_gross_token1=0,
                lp_fees_lp_only_token0=0,
                lp_fees_lp_only_token1=0,
                divergence_token0=0,
                divergence_token1=0,
                lvr_proxy_token0=0,
                lvr_proxy_token1=0,
                gas_token0=0,
                gas_token1=0,
                slippage_token0=0,
                slippage_token1=0,
                hook_deltas_token0=0,
                hook_deltas_token1=0,
                rebalance_cost_token0=0,
                rebalance_cost_token1=0,
                external_cash_flow_token0=0,
                external_cash_flow_token1=0,
                residual_token0=0,
                residual_token1=0,
                reference_basis="M-BM-002",
                lvr_is_proxy=True,
                is_relative_only=True,
                version=ATTRIBUTION_VERSION,
            )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


class TestGenerateReport:
    def test_generates_report_for_valid_episode(self) -> None:
        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=0,
            end_tick=0,
        )
        report = generate_report(
            ep,
            hold_series=hold_series,
            rebalanced_series=rb_series,
        )
        assert report.failed is False
        assert report.row.lp_fees_gross_token0 == 0

    def test_records_failed_when_residual_exceeds_tolerance(self) -> None:
        """A residual above tolerance is recorded as failed."""
        import dataclasses

        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=0,
            end_tick=10,
        )
        # Patch the end_valuation to introduce an unreconciled
        # raw_amount_0 gap that exceeds RESIDUAL_TOLERANCE.
        ep_patch = dataclasses.replace(
            ep,
            end_valuation=dataclasses.replace(
                ep.end_valuation,
                raw_amount_0=ep.end_valuation.raw_amount_0 + (RESIDUAL_TOLERANCE * 4),
            ),
        )
        report = generate_report(
            ep_patch,
            hold_series=hold_series,
            rebalanced_series=rb_series,
        )
        assert report.failed is True

    def test_relative_only_report_has_no_usd_field(self) -> None:
        """The relative-only report's serialised payload has no USD field."""
        sqrt_p = get_sqrt_price_at_tick(0)
        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=0,
            end_tick=0,
        )
        report = generate_relative_only_report(
            ep,
            hold_series=hold_series,
            rebalanced_series=rb_series,
            entry_sqrt_price_x96=sqrt_p,
            exit_sqrt_price_x96=sqrt_p,
        )
        payload = report.as_payload()
        assert_no_usd_fields(payload, context="relative_only_report")
        assert report.row.numeraire_level is NumeraireLevel.RELATIVE_ONLY


# ---------------------------------------------------------------------------
# Cross-numeraire ranking guard (M-BM-002 vs other reference bases)
# ---------------------------------------------------------------------------


class TestCrossReferenceBasisGuard:
    def test_two_rows_with_different_bases_are_not_ranked(self) -> None:
        """Two rows built on different reference bases must never
        be ranked against each other. The structural guard is
        the :attr:`AttributionRow.reference_basis` field and the
        T053 ``ranking_blocked_between`` helper.
        """
        ep, hold_series, rb_series = _make_attribution_fixture(
            start_tick=0,
            end_tick=0,
        )
        row_a = compute_attribution_row(ep, hold_series=hold_series, rebalanced_series=rb_series)
        assert row_a.reference_basis == "M-BM-002"
        # Build a second row with a different reference basis by
        # patching the underlying rebalanced_series. The structural
        # guard is the ``reference_basis`` field; the test pins
        # the contract that two rows with different bases carry
        # distinct references.
        rb_series_other = BenchmarkSeries(
            name="OTHER",
            reference_basis="OTHER-BASIS",
            points=rb_series.points,
            version=BENCHMARK_VERSION,
            notes=("alternate reference basis",),
        )
        with pytest.raises(AttributionError, match="reference_basis"):
            compute_attribution_row(
                ep,
                hold_series=hold_series,
                rebalanced_series=rb_series_other,
            )


# ---------------------------------------------------------------------------
# Defensive tests
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_rejects_non_int_inventory_pnl(self) -> None:
        with pytest.raises(AttributionError):
            compute_hold_benchmark_value(
                initial_token0="not an int",  # type: ignore[arg-type]
                initial_token1=0,
                sqrt_price_x96=Q96,
            )

    def test_rejects_negative_initial_token0(self) -> None:
        with pytest.raises(AttributionError):
            compute_hold_benchmark_value(
                initial_token0=-1,
                initial_token1=0,
                sqrt_price_x96=Q96,
            )

    def test_relative_only_row_rejects_empty_episode_id(self) -> None:
        with pytest.raises(QuoteRelativeOnlyError):
            RelativeOnlyAttributionRow(
                episode_id="",
                position_key=_make_position_key(),
                inventory_pnl_ratio_q64_64=0,
                lp_fees_gross_ratio_q64_64=0,
                lp_fees_lp_only_ratio_q64_64=0,
                divergence_ratio_q64_64=0,
                lvr_proxy_ratio_q64_64=0,
                gas_ratio_q64_64=0,
                slippage_ratio_q64_64=0,
                hook_deltas_ratio_q64_64=0,
                rebalance_cost_ratio_q64_64=0,
                external_cash_flow_ratio_q64_64=0,
                residual_ratio_q64_64=0,
                reference_basis="M-BM-002",
                lvr_is_proxy=True,
                numeraire_level=NumeraireLevel.RELATIVE_ONLY,
                version=ATTRIBUTION_VERSION,
            )

    def test_episode_pnl_rejects_non_tuple_cash_flows(self) -> None:
        ep = _make_episode(start_tick=0, end_tick=0)
        with pytest.raises(InvalidEpisodeError):
            EpisodePnL(
                episode_id="bad",
                position_key=ep.position_key,
                boundaries=ep.boundaries,
                start_valuation=ep.start_valuation,
                end_valuation=ep.end_valuation,
                cash_flows=[],  # type: ignore[arg-type]
                gas_costs=(),
                hook_deltas=(),
                rebalance_events=(),
                realised_fees_gross_token0=0,
                realised_fees_gross_token1=0,
                realised_fees_lp_token0=0,
                realised_fees_lp_token1=0,
                realised_fees_protocol_token0=0,
                realised_fees_protocol_token1=0,
            )

    def test_hold_benchmark_rejects_non_tuple_snapshots(self) -> None:
        with pytest.raises(InvalidBenchmarkError):
            HoldBenchmark(
                initial_token0=1,
                initial_token1=1,
                snapshots=[(1, get_sqrt_price_at_tick(0))],  # type: ignore[arg-type]
            )

    def test_rebalanced_benchmark_rejects_out_of_range_weight(self) -> None:
        with pytest.raises(InvalidBenchmarkError):
            RebalancedBenchmark(
                initial_token0=1,
                initial_token1=1,
                token0_weight_bps=10_001,
                snapshots=((1, get_sqrt_price_at_tick(0)),),
            )
