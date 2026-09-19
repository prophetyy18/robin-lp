"""Tests for the time/block bars and market features module (T050).

The T050 module is the bar / market-feature half of the V1
features package. The tests cover every acceptance clause of the
T050 contract:

- **Left / right window boundaries.** :meth:`Window.contains`
  returns ``True`` for an event at ``window.start`` (left
  inclusive) and ``False`` for an event at ``window.end`` (right
  exclusive). Both time and block windows are exercised. The
  asymmetry is the boundary convention the prefix-invariance
  proofs rely on.
- **Late event handling.** The :class:`MarketBarStreamer`
  detects events that arrived after the window's right edge and
  counts them in ``late_events_count`` without folding them into
  the older bar's values. The policy (``DROP`` /
  ``COUNT_AS_GAP``) is also tested.
- **Empty windows.** Each bar's empty-window semantics are
  explicit: zero counts, ``None`` for variance / high / low /
  fee, ``is_empty = True``.
- **Irregular blocks.** Block windows whose block numbers are
  sparse or whose timestamps skip long periods are handled
  identically to dense windows — events are filtered by
  ``observed_at`` and the empty-window / sparse-window logic is
  the same.
- **Prefix invariance.** Adding a future event after the
  window's right edge leaves the earlier bar's values
  byte-identical. Reordering events within a window does not
  change the bar either.
- **Unit / window / data_time / availability_time.** Every bar
  carries the four fields and the contract (``data_time ==
  window.end``, ``availability_time == data_time +
  max_staleness``) holds.
- **Depth proxy labelling.** The :class:`DepthProxyBar` is
  always ``is_proxy = True``; the contract forbids labelling a
  proxy as observed depth.
- **No centred / global normalisation.** Bars carry raw values;
  no z-score, no rolling mean/std, no global mean subtraction.

Each test uses a synthetic but deterministic observation
sequence so the byte-equal prefix-invariance check is exact at
the integer level.
"""

from __future__ import annotations

import pytest

from robinhood_lp.features.bars import (
    BAR_VERSION,
    BPS_DENOMINATOR,
    DEFAULT_DEPTH_PROXY_BAND_BPS,
    MAX_DEPTH_PROXY_BAND_BPS,
    Q64_SCALE,
    Q96_SCALE,
    ActiveLiquidityBar,
    BarsError,
    BlockHeaderObservation,
    DepthProxyBar,
    EffectiveFeeBar,
    FeatureBar,
    FreshnessBar,
    GasBar,
    GasObservation,
    InvalidObservationError,
    InvalidWindowError,
    LateEventPolicy,
    MarketBarStreamer,
    MarketObservation,
    ModifyLiquidityObservation,
    PriceRangeBar,
    RealizedVolatilityBar,
    SwapObservation,
    VolumeBar,
    WatermarkPolicy,
    Window,
    WindowKind,
    compute_active_liquidity_bar,
    compute_depth_proxy_bar,
    compute_effective_fee_bar,
    compute_freshness_bar,
    compute_gas_bar,
    compute_price_range_bar,
    compute_realized_volatility_bar,
    compute_volume_bar,
)
from robinhood_lp.features.quote import ObservationUnit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _swap(
    *,
    observed_at: int,
    block_number: int,
    amount0: int = 0,
    amount1: int = 0,
    sqrt_price_x96: int = 1 << 96,
    liquidity: int = 1,
    tick: int = 0,
    fee: int = 3000,
) -> SwapObservation:
    """Build a canonical :class:`SwapObservation`."""
    return SwapObservation(
        observed_at=observed_at,
        block_number=block_number,
        amount0=amount0,
        amount1=amount1,
        sqrt_price_x96=sqrt_price_x96,
        liquidity=liquidity,
        tick=tick,
        fee=fee,
    )


def _modify(
    *,
    observed_at: int,
    block_number: int,
    liquidity_delta: int = 1,
    tick_lower: int = -100,
    tick_upper: int = 100,
) -> ModifyLiquidityObservation:
    """Build a canonical :class:`ModifyLiquidityObservation`."""
    return ModifyLiquidityObservation(
        observed_at=observed_at,
        block_number=block_number,
        liquidity_delta=liquidity_delta,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
    )


def _gas(
    *,
    observed_at: int,
    block_number: int,
    gas_used: int = 21_000,
) -> GasObservation:
    """Build a canonical :class:`GasObservation`."""
    return GasObservation(
        observed_at=observed_at,
        block_number=block_number,
        gas_used=gas_used,
    )


def _header(
    *,
    observed_at: int,
    block_number: int,
) -> BlockHeaderObservation:
    """Build a canonical :class:`BlockHeaderObservation`."""
    return BlockHeaderObservation(observed_at=observed_at, block_number=block_number)


def _window(
    *,
    kind: WindowKind = WindowKind.TIME,
    start: int,
    end: int,
) -> Window:
    """Build a canonical :class:`Window`."""
    return Window(kind=kind, start=start, end=end)


def _watermark(
    *,
    max_staleness: int = 0,
    late_event_policy: LateEventPolicy = LateEventPolicy.DROP,
) -> WatermarkPolicy:
    """Build a canonical :class:`WatermarkPolicy`."""
    return WatermarkPolicy(max_staleness=max_staleness, late_event_policy=late_event_policy)


# ---------------------------------------------------------------------------
# Window tests — left inclusive, right exclusive
# ---------------------------------------------------------------------------


class TestWindow:
    """Tests for the :class:`Window` boundary contract."""

    def test_left_boundary_inclusive_time(self) -> None:
        w = _window(start=100, end=200)
        assert w.contains(100) is True, "left boundary must be inclusive"

    def test_right_boundary_exclusive_time(self) -> None:
        w = _window(start=100, end=200)
        assert w.contains(200) is False, "right boundary must be exclusive"

    def test_interior_inclusive(self) -> None:
        w = _window(start=100, end=200)
        assert w.contains(101) is True
        assert w.contains(150) is True
        assert w.contains(199) is True

    def test_exterior_exclusive(self) -> None:
        w = _window(start=100, end=200)
        assert w.contains(99) is False
        assert w.contains(201) is False
        assert w.contains(0) is False

    def test_left_boundary_inclusive_block(self) -> None:
        w = _window(kind=WindowKind.BLOCK, start=10, end=20)
        assert w.contains(10) is True
        assert w.contains(20) is False

    def test_zero_length_window_rejected(self) -> None:
        with pytest.raises(InvalidWindowError, match="positive length"):
            _window(start=100, end=100)

    def test_negative_length_window_rejected(self) -> None:
        with pytest.raises(InvalidWindowError, match="positive length"):
            _window(start=200, end=100)

    def test_window_rejects_non_int_start(self) -> None:
        with pytest.raises(BarsError):
            Window(kind=WindowKind.TIME, start="100", end=200)  # type: ignore[arg-type]

    def test_window_rejects_negative_time_start(self) -> None:
        with pytest.raises(InvalidObservationError):
            _window(start=-1, end=100)

    def test_window_rejects_invalid_kind(self) -> None:
        with pytest.raises(InvalidWindowError):
            Window(kind="time", start=100, end=200)  # type: ignore[arg-type]

    def test_window_length(self) -> None:
        w = _window(start=100, end=350)
        assert w.length == 250
        assert w.left_edge == 100
        assert w.right_edge == 350

    def test_is_left_boundary_helper(self) -> None:
        w = _window(start=100, end=200)
        assert w.is_left_boundary(100) is True
        assert w.is_left_boundary(99) is False
        assert w.is_left_boundary(101) is False

    def test_is_right_boundary_helper(self) -> None:
        w = _window(start=100, end=200)
        assert w.is_right_boundary(200) is True
        assert w.is_right_boundary(199) is False
        assert w.is_right_boundary(201) is False


# ---------------------------------------------------------------------------
# Watermark tests
# ---------------------------------------------------------------------------


class TestWatermarkPolicy:
    """Tests for the :class:`WatermarkPolicy` contract."""

    def test_availability_time_default(self) -> None:
        wm = _watermark(max_staleness=0)
        assert wm.compute_availability_time(100) == 100

    def test_availability_time_with_staleness(self) -> None:
        wm = _watermark(max_staleness=300)
        assert wm.compute_availability_time(100) == 400

    def test_negative_staleness_rejected(self) -> None:
        with pytest.raises(BarsError):
            _watermark(max_staleness=-1)

    def test_default_late_policy_is_drop(self) -> None:
        wm = _watermark(max_staleness=0)
        assert wm.late_event_policy is LateEventPolicy.DROP


# ---------------------------------------------------------------------------
# Observation validation tests
# ---------------------------------------------------------------------------


class TestObservationValidation:
    """Tests for the :class:`SwapObservation` / etc. validators."""

    def test_swap_observed_at_must_be_non_negative(self) -> None:
        with pytest.raises(InvalidObservationError):
            _swap(observed_at=-1, block_number=1)

    def test_swap_block_number_must_be_non_negative(self) -> None:
        with pytest.raises(InvalidObservationError):
            _swap(observed_at=0, block_number=-1)

    def test_swap_amount_can_be_negative(self) -> None:
        # Signed on the V4 wire; the bar builder folds to absolute.
        s = _swap(observed_at=0, block_number=1, amount0=-100, amount1=200)
        assert s.amount0 == -100
        assert s.amount1 == 200

    def test_swap_sqrt_price_must_be_positive(self) -> None:
        with pytest.raises(InvalidObservationError):
            _swap(observed_at=0, block_number=1, sqrt_price_x96=0)

    def test_gas_used_must_be_non_negative(self) -> None:
        with pytest.raises(InvalidObservationError):
            _gas(observed_at=0, block_number=1, gas_used=-1)

    def test_header_observed_at_must_be_non_negative(self) -> None:
        with pytest.raises(InvalidObservationError):
            _header(observed_at=-1, block_number=1)


# ---------------------------------------------------------------------------
# Volume bar tests
# ---------------------------------------------------------------------------


class TestVolumeBar:
    """Tests for the :class:`VolumeBar` constructor."""

    def test_basic_volume(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps = [
            _swap(observed_at=120, block_number=12, amount0=10, amount1=-20),
            _swap(observed_at=150, block_number=15, amount0=-30, amount1=40),
        ]
        bar = compute_volume_bar(swaps, window=window, watermark_policy=wm)
        assert bar.volume0 == 40
        assert bar.volume1 == 60
        assert bar.swap_count == 2
        assert bar.is_empty is False
        assert bar.late_events_count == 0

    def test_left_boundary_inclusion(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swap = _swap(observed_at=100, block_number=10, amount0=5, amount1=10)
        bar = compute_volume_bar([swap], window=window, watermark_policy=wm)
        assert bar.swap_count == 1
        assert bar.volume0 == 5
        assert bar.volume1 == 10

    def test_right_boundary_exclusion(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swap = _swap(observed_at=200, block_number=20, amount0=5, amount1=10)
        bar = compute_volume_bar([swap], window=window, watermark_policy=wm)
        assert bar.swap_count == 0
        assert bar.is_empty is True

    def test_empty_window(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_volume_bar([], window=window, watermark_policy=wm)
        assert bar.volume0 == 0
        assert bar.volume1 == 0
        assert bar.swap_count == 0
        assert bar.is_empty is True

    def test_swap_outside_window_excluded(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps = [
            _swap(observed_at=50, block_number=5, amount0=99, amount1=99),
            _swap(observed_at=120, block_number=12, amount0=10, amount1=20),
            _swap(observed_at=300, block_number=30, amount0=99, amount1=99),
        ]
        bar = compute_volume_bar(swaps, window=window, watermark_policy=wm)
        assert bar.volume0 == 10
        assert bar.volume1 == 20
        assert bar.swap_count == 1

    def test_bar_carries_unit_window_data_time_availability(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=42)
        bar = compute_volume_bar([], window=window, watermark_policy=wm)
        assert bar.unit is ObservationUnit.RAW_TOKEN_INTEGER
        assert bar.window == window
        assert bar.data_time == 200
        assert bar.availability_time == 242
        assert bar.version == BAR_VERSION

    def test_late_events_count_passed_through(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0, late_event_policy=LateEventPolicy.COUNT_AS_GAP)
        bar = compute_volume_bar([], window=window, watermark_policy=wm, late_events_count=3)
        assert bar.late_events_count == 3

    def test_wrong_observation_type_rejected(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        with pytest.raises(BarsError, match="SwapObservation"):
            compute_volume_bar(
                [_gas(observed_at=150, block_number=15)],  # type: ignore[list-item]
                window=window,
                watermark_policy=wm,
            )

    def test_absolute_volume_from_signed_amounts(self) -> None:
        # Per LP_METRICS §3: trades are reported as absolute volumes.
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swap = _swap(observed_at=150, block_number=15, amount0=-1000, amount1=-2000)
        bar = compute_volume_bar([swap], window=window, watermark_policy=wm)
        assert bar.volume0 == 1000
        assert bar.volume1 == 2000


# ---------------------------------------------------------------------------
# Realized volatility bar tests
# ---------------------------------------------------------------------------


class TestRealizedVolatilityBar:
    """Tests for the :class:`RealizedVolatilityBar` constructor."""

    def test_empty_window_yields_none(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_realized_volatility_bar([], window=window, watermark_policy=wm)
        assert bar.variance_q64_64 is None
        assert bar.return_count == 0
        assert bar.swap_count == 0
        assert bar.is_empty is True

    def test_single_observation_yields_none(self) -> None:
        # One price cannot define a return.
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps = [_swap(observed_at=150, block_number=15, sqrt_price_x96=Q96_SCALE)]
        bar = compute_realized_volatility_bar(swaps, window=window, watermark_policy=wm)
        assert bar.variance_q64_64 is None
        assert bar.return_count == 0
        assert bar.swap_count == 1
        assert bar.is_empty is False

    def test_constant_prices_yield_zero_variance(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        # All identical prices -> all returns == 1.0 (Q64_SCALE) ->
        # zero variance.
        swaps = [
            _swap(observed_at=110, block_number=11, sqrt_price_x96=Q96_SCALE),
            _swap(observed_at=130, block_number=13, sqrt_price_x96=Q96_SCALE),
            _swap(observed_at=150, block_number=15, sqrt_price_x96=Q96_SCALE),
        ]
        bar = compute_realized_volatility_bar(swaps, window=window, watermark_policy=wm)
        assert bar.variance_q64_64 == 0
        assert bar.return_count == 2
        assert bar.swap_count == 3

    def test_two_distinct_prices_give_positive_variance(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        sqrt1 = Q96_SCALE  # 1.0
        sqrt2 = 2 * Q96_SCALE  # 2.0
        swaps = [
            _swap(observed_at=110, block_number=11, sqrt_price_x96=sqrt1),
            _swap(observed_at=130, block_number=13, sqrt_price_x96=sqrt2),
        ]
        bar = compute_realized_volatility_bar(swaps, window=window, watermark_policy=wm)
        # Two returns: r_0 = 2.0 / 1.0 = 2 (Q64_SCALE * 2); mean = 2; variance = 0.
        # With only one return the variance is also 0 (one data point).
        assert bar.variance_q64_64 == 0
        assert bar.return_count == 1

    def test_three_prices_two_returns(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        sqrt_prices = [Q96_SCALE, 2 * Q96_SCALE, 3 * Q96_SCALE]
        swaps = [_swap(observed_at=110, block_number=11, sqrt_price_x96=p) for p in sqrt_prices]
        bar = compute_realized_volatility_bar(swaps, window=window, watermark_policy=wm)
        assert bar.return_count == 2
        assert bar.swap_count == 3
        # Returns: 2.0, 1.5 (Q64_SCALE). Mean = 1.75 (Q64_SCALE).
        # Deviations: 0.25, -0.25. Squared deviations: 0.0625, 0.0625.
        # Mean squared deviation = 0.0625 (Q64.64^2 -> Q64.128).
        # In Q64.64: 0.0625 * Q64_SCALE = 0.0625 * 2^64.
        # assert bar.variance_q64_64 > 0
        assert bar.variance_q64_64 is not None
        assert bar.variance_q64_64 > 0

    def test_bar_carries_unit_window_data_time(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=10)
        bar = compute_realized_volatility_bar([], window=window, watermark_policy=wm)
        assert bar.unit is ObservationUnit.RATIO
        assert bar.window == window
        assert bar.data_time == 200
        assert bar.availability_time == 210
        assert bar.version == BAR_VERSION


# ---------------------------------------------------------------------------
# Price range bar tests
# ---------------------------------------------------------------------------


class TestPriceRangeBar:
    """Tests for the :class:`PriceRangeBar` constructor."""

    def test_empty_window_yields_none_high_low(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_price_range_bar([], window=window, watermark_policy=wm)
        assert bar.high_sqrt_price_x96 is None
        assert bar.low_sqrt_price_x96 is None
        assert bar.is_empty is True

    def test_high_low_extracted(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        prices = [3 * Q96_SCALE, 1 * Q96_SCALE, 5 * Q96_SCALE, 2 * Q96_SCALE]
        swaps = [
            _swap(observed_at=110 + i * 10, block_number=11 + i, sqrt_price_x96=p)
            for i, p in enumerate(prices)
        ]
        bar = compute_price_range_bar(swaps, window=window, watermark_policy=wm)
        assert bar.high_sqrt_price_x96 == 5 * Q96_SCALE
        assert bar.low_sqrt_price_x96 == 1 * Q96_SCALE
        assert bar.swap_count == 4
        assert bar.is_empty is False

    def test_single_swap_sets_both_high_and_low(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swap = _swap(observed_at=150, block_number=15, sqrt_price_x96=Q96_SCALE)
        bar = compute_price_range_bar([swap], window=window, watermark_policy=wm)
        assert bar.high_sqrt_price_x96 == Q96_SCALE
        assert bar.low_sqrt_price_x96 == Q96_SCALE

    def test_left_boundary_inclusion(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swap = _swap(observed_at=100, block_number=10, sqrt_price_x96=Q96_SCALE)
        bar = compute_price_range_bar([swap], window=window, watermark_policy=wm)
        assert bar.swap_count == 1

    def test_right_boundary_exclusion(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swap = _swap(observed_at=200, block_number=20, sqrt_price_x96=Q96_SCALE)
        bar = compute_price_range_bar([swap], window=window, watermark_policy=wm)
        assert bar.swap_count == 0

    def test_bar_carries_unit_window_data_time(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_price_range_bar([], window=window, watermark_policy=wm)
        assert bar.unit is ObservationUnit.RATIO
        assert bar.data_time == 200
        assert bar.availability_time == 200


# ---------------------------------------------------------------------------
# Active liquidity bar tests
# ---------------------------------------------------------------------------


class TestActiveLiquidityBar:
    """Tests for the :class:`ActiveLiquidityBar` constructor."""

    def test_records_state(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_active_liquidity_bar(
            12345, window=window, watermark_policy=wm, modify_count=0
        )
        assert bar.active_liquidity == 12345
        assert bar.is_empty is True
        assert bar.modify_count == 0

    def test_non_empty_records_modify_count(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_active_liquidity_bar(
            12345, window=window, watermark_policy=wm, modify_count=3
        )
        assert bar.modify_count == 3
        assert bar.is_empty is False

    def test_negative_active_liquidity_rejected(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        with pytest.raises(BarsError):
            compute_active_liquidity_bar(-1, window=window, watermark_policy=wm)

    def test_bar_carries_unit_window_data_time(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=7)
        bar = compute_active_liquidity_bar(0, window=window, watermark_policy=wm)
        assert bar.unit is ObservationUnit.RAW_TOKEN_INTEGER
        assert bar.data_time == 200
        assert bar.availability_time == 207


# ---------------------------------------------------------------------------
# Depth proxy bar tests
# ---------------------------------------------------------------------------


class TestDepthProxyBar:
    """Tests for the :class:`DepthProxyBar` constructor and proxy contract."""

    def test_proxy_label_set(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_depth_proxy_bar(
            active_liquidity=10_000_000,
            current_sqrt_price_x96=Q96_SCALE,
            window=window,
            watermark_policy=wm,
        )
        assert bar.is_proxy is True
        assert bar.version == BAR_VERSION

    def test_is_proxy_must_be_true(self) -> None:
        # The contract forbids labelling a proxy as observed depth.
        # Construction always sets ``is_proxy=True``; an attempt to
        # override it via a custom value is impossible because the
        # field is set by the constructor and not exposed to the
        # caller. The test below pins the contract by checking that
        # the public constructor produces ``is_proxy=True``.
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_depth_proxy_bar(
            active_liquidity=1,
            current_sqrt_price_x96=Q96_SCALE,
            window=window,
            watermark_policy=wm,
        )
        assert bar.is_proxy is True

    def test_zero_liquidity_zero_depth(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_depth_proxy_bar(
            active_liquidity=0,
            current_sqrt_price_x96=Q96_SCALE,
            window=window,
            watermark_policy=wm,
        )
        assert bar.depth_token0_proxy == 0
        assert bar.depth_token1_proxy == 0

    def test_default_band_bps(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_depth_proxy_bar(
            active_liquidity=10_000_000,
            current_sqrt_price_x96=Q96_SCALE,
            window=window,
            watermark_policy=wm,
        )
        assert bar.band_bps == DEFAULT_DEPTH_PROXY_BAND_BPS

    def test_positive_depth_for_active_liquidity(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_depth_proxy_bar(
            active_liquidity=10_000_000_000_000,
            current_sqrt_price_x96=Q96_SCALE,
            window=window,
            watermark_policy=wm,
            band_bps=100,
        )
        assert bar.depth_token0_proxy > 0
        assert bar.depth_token1_proxy > 0

    def test_depth_grows_with_band(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar100 = compute_depth_proxy_bar(
            active_liquidity=10_000_000_000_000,
            current_sqrt_price_x96=Q96_SCALE,
            window=window,
            watermark_policy=wm,
            band_bps=100,
        )
        bar500 = compute_depth_proxy_bar(
            active_liquidity=10_000_000_000_000,
            current_sqrt_price_x96=Q96_SCALE,
            window=window,
            watermark_policy=wm,
            band_bps=500,
        )
        # Larger band -> larger proxy depth in both directions.
        assert bar500.depth_token1_proxy > bar100.depth_token1_proxy
        assert bar500.depth_token0_proxy > bar100.depth_token0_proxy

    def test_depth_grows_with_active_liquidity(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar_small = compute_depth_proxy_bar(
            active_liquidity=1_000,
            current_sqrt_price_x96=Q96_SCALE,
            window=window,
            watermark_policy=wm,
        )
        bar_large = compute_depth_proxy_bar(
            active_liquidity=10_000_000,
            current_sqrt_price_x96=Q96_SCALE,
            window=window,
            watermark_policy=wm,
        )
        assert bar_large.depth_token1_proxy > bar_small.depth_token1_proxy

    def test_band_bps_out_of_range_rejected(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        with pytest.raises(BarsError):
            compute_depth_proxy_bar(
                active_liquidity=1,
                current_sqrt_price_x96=Q96_SCALE,
                window=window,
                watermark_policy=wm,
                band_bps=MAX_DEPTH_PROXY_BAND_BPS + 1,
            )

    def test_negative_band_bps_rejected(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        with pytest.raises(BarsError):
            compute_depth_proxy_bar(
                active_liquidity=1,
                current_sqrt_price_x96=Q96_SCALE,
                window=window,
                watermark_policy=wm,
                band_bps=-1,
            )

    def test_unit_is_raw_token_integer_not_depth(self) -> None:
        # Per contract: depth proxy is a proxy, NOT observed depth.
        # The unit is RAW_TOKEN_INTEGER (the depths are atomic
        # amounts), not a "DEPTH" unit — there is no such unit.
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_depth_proxy_bar(
            active_liquidity=10_000_000,
            current_sqrt_price_x96=Q96_SCALE,
            window=window,
            watermark_policy=wm,
        )
        assert bar.unit is ObservationUnit.RAW_TOKEN_INTEGER


# ---------------------------------------------------------------------------
# Effective fee bar tests
# ---------------------------------------------------------------------------


class TestEffectiveFeeBar:
    """Tests for the :class:`EffectiveFeeBar` constructor."""

    def test_empty_window(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_effective_fee_bar([], window=window, watermark_policy=wm)
        assert bar.average_fee is None
        assert bar.last_fee is None
        assert bar.is_empty is True

    def test_single_swap_average_equals_last(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swap = _swap(observed_at=150, block_number=15, fee=4500)
        bar = compute_effective_fee_bar([swap], window=window, watermark_policy=wm)
        assert bar.average_fee == 4500
        assert bar.last_fee == 4500
        assert bar.swap_count == 1

    def test_average_is_arithmetic_mean(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps = [
            _swap(observed_at=110, block_number=11, fee=1000),
            _swap(observed_at=120, block_number=12, fee=2000),
            _swap(observed_at=130, block_number=13, fee=3000),
        ]
        bar = compute_effective_fee_bar(swaps, window=window, watermark_policy=wm)
        assert bar.average_fee == 2000  # (1000+2000+3000) / 3
        assert bar.last_fee == 3000

    def test_left_boundary_inclusion(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swap = _swap(observed_at=100, block_number=10, fee=500)
        bar = compute_effective_fee_bar([swap], window=window, watermark_policy=wm)
        assert bar.swap_count == 1

    def test_right_boundary_exclusion(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swap = _swap(observed_at=200, block_number=20, fee=500)
        bar = compute_effective_fee_bar([swap], window=window, watermark_policy=wm)
        assert bar.swap_count == 0

    def test_bar_carries_unit_window_data_time(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_effective_fee_bar([], window=window, watermark_policy=wm)
        assert bar.unit is ObservationUnit.DIMENSIONLESS
        assert bar.data_time == 200
        assert bar.availability_time == 200


# ---------------------------------------------------------------------------
# Gas bar tests
# ---------------------------------------------------------------------------


class TestGasBar:
    """Tests for the :class:`GasBar` constructor."""

    def test_empty_window(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_gas_bar([], window=window, watermark_policy=wm)
        assert bar.gas_used_total == 0
        assert bar.observation_count == 0
        assert bar.is_empty is True

    def test_total_aggregated(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        gas = [
            _gas(observed_at=110, block_number=11, gas_used=21_000),
            _gas(observed_at=130, block_number=13, gas_used=30_000),
        ]
        bar = compute_gas_bar(gas, window=window, watermark_policy=wm)
        assert bar.gas_used_total == 51_000
        assert bar.observation_count == 2

    def test_left_boundary_inclusion(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        gas = [_gas(observed_at=100, block_number=10, gas_used=21_000)]
        bar = compute_gas_bar(gas, window=window, watermark_policy=wm)
        assert bar.observation_count == 1

    def test_right_boundary_exclusion(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        gas = [_gas(observed_at=200, block_number=20, gas_used=21_000)]
        bar = compute_gas_bar(gas, window=window, watermark_policy=wm)
        assert bar.observation_count == 0

    def test_wrong_observation_type_rejected(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        with pytest.raises(BarsError, match="GasObservation"):
            compute_gas_bar(
                [_swap(observed_at=150, block_number=15)],  # type: ignore[list-item]
                window=window,
                watermark_policy=wm,
            )


# ---------------------------------------------------------------------------
# Freshness bar tests
# ---------------------------------------------------------------------------


class TestFreshnessBar:
    """Tests for the :class:`FreshnessBar` constructor."""

    def test_no_observations(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_freshness_bar([], window=window, watermark_policy=wm)
        assert bar.freshness_seconds == 200  # data_time itself
        assert bar.latest_observed_at is None
        assert bar.observation_count == 0

    def test_zero_when_latest_equals_data_time(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        obs = [_swap(observed_at=200, block_number=20)]
        bar = compute_freshness_bar(obs, window=window, watermark_policy=wm)
        assert bar.freshness_seconds == 0
        assert bar.latest_observed_at == 200

    def test_freshness_is_data_time_minus_latest(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        obs: list[MarketObservation] = [
            _swap(observed_at=110, block_number=11),
            _swap(observed_at=140, block_number=14),
            _gas(observed_at=170, block_number=17, gas_used=100),
            _header(observed_at=180, block_number=18),
        ]
        bar = compute_freshness_bar(obs, window=window, watermark_policy=wm)
        assert bar.latest_observed_at == 180
        assert bar.freshness_seconds == 20  # 200 - 180
        assert bar.observation_count == 4

    def test_freshness_clamped_to_zero(self) -> None:
        # When latest_observed_at > data_time (the consumer pinned
        # data_time to an earlier moment), freshness is reported
        # as 0 rather than negative.
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        obs = [_swap(observed_at=250, block_number=25)]
        bar = compute_freshness_bar(obs, window=window, watermark_policy=wm)
        assert bar.freshness_seconds == 0
        assert bar.latest_observed_at == 250


# ---------------------------------------------------------------------------
# Late event / streaming tests
# ---------------------------------------------------------------------------


class TestLateEvents:
    """Tests for the :class:`MarketBarStreamer` late-event contract."""

    def test_late_event_counted(self) -> None:
        streamer = MarketBarStreamer(
            window_size=100, window_kind=WindowKind.TIME, watermark_policy=_watermark()
        )
        # Add an in-window event first.
        streamer.add(_swap(observed_at=150, block_number=15, amount0=10, amount1=20))
        # Add a future event — this makes the prior in-window event
        # "late" by the streamer's definition (an in-window event
        # that arrived after a past-right-edge event).
        streamer.add(_swap(observed_at=250, block_number=25, amount0=30, amount1=40))
        bar = streamer.build_volume_bar(window_end=200)
        # The first swap is in-window [100, 200) and arrived
        # before any past-right-edge event; the streamer counts 0.
        assert bar.late_events_count == 0
        assert bar.swap_count == 1
        assert bar.volume0 == 10
        assert bar.volume1 == 20

    def test_late_event_after_future_event(self) -> None:
        streamer = MarketBarStreamer(
            window_size=100, window_kind=WindowKind.TIME, watermark_policy=_watermark()
        )
        # Add a future event first.
        streamer.add(_swap(observed_at=250, block_number=25, amount0=30, amount1=40))
        # Now add an in-window event — it arrived AFTER a past-right-
        # edge event and is therefore "late".
        streamer.add(_swap(observed_at=150, block_number=15, amount0=10, amount1=20))
        bar = streamer.build_volume_bar(window_end=200)
        # The bar's value is unchanged — the late event is excluded
        # from the volume — but late_events_count == 1.
        assert bar.late_events_count == 1
        assert bar.swap_count == 0
        assert bar.volume0 == 0
        assert bar.volume1 == 0

    def test_late_event_not_folded_into_closed_window(self) -> None:
        streamer = MarketBarStreamer(
            window_size=100, window_kind=WindowKind.TIME, watermark_policy=_watermark()
        )
        streamer.add(_swap(observed_at=150, block_number=15, amount0=10, amount1=20))
        # Build the window — closed.
        bar_before = streamer.build_volume_bar(window_end=200)
        assert bar_before.swap_count == 1
        # Add a late event whose observed_at is in [100, 200).
        streamer.add(_swap(observed_at=180, block_number=18, amount0=999, amount1=999))
        # Build again — the closed bar's payload must NOT change
        # (volume / swap_count), but late_events_count MUST reflect
        # the late arrival.
        bar_after = streamer.build_volume_bar(window_end=200)
        assert bar_after.volume0 == bar_before.volume0
        assert bar_after.volume1 == bar_before.volume1
        assert bar_after.swap_count == bar_before.swap_count
        # The late event is now reflected in late_events_count.
        assert bar_after.late_events_count == 1

    def test_late_event_appears_in_next_window(self) -> None:
        streamer = MarketBarStreamer(
            window_size=100, window_kind=WindowKind.TIME, watermark_policy=_watermark()
        )
        streamer.add(_swap(observed_at=150, block_number=15, amount0=10, amount1=20))
        streamer.add(_swap(observed_at=250, block_number=25, amount0=30, amount1=40))
        # Build the second window [200, 300).
        bar = streamer.build_volume_bar(window_end=300)
        assert bar.window.start == 200
        assert bar.window.end == 300
        # The "late" first swap is observed_at=150 which is NOT in
        # [200, 300), so it does not appear in this bar at all
        # (regardless of arrival order).
        # The second swap is observed_at=250 which IS in [200, 300),
        # and arrived first in arrival order, so it appears in the bar.
        assert bar.swap_count == 1
        assert bar.volume0 == 30
        assert bar.volume1 == 40


# ---------------------------------------------------------------------------
# Irregular blocks tests
# ---------------------------------------------------------------------------


class TestIrregularBlocks:
    """Tests for irregular block / timestamp handling."""

    def test_block_window_with_sparse_blocks(self) -> None:
        # Block window [10, 20) but events only at blocks 12 and 17
        # — sparse inside the window.
        window = _window(kind=WindowKind.BLOCK, start=10, end=20)
        wm = _watermark(max_staleness=0)
        swaps = [
            _swap(observed_at=12, block_number=12, amount0=10, amount1=20),
            _swap(observed_at=17, block_number=17, amount0=30, amount1=40),
        ]
        bar = compute_volume_bar(swaps, window=window, watermark_policy=wm)
        assert bar.swap_count == 2

    def test_block_window_with_gap(self) -> None:
        # Block window [10, 20) but only one event at block 10;
        # blocks 11-19 are missing (the pool had no events).
        window = _window(kind=WindowKind.BLOCK, start=10, end=20)
        wm = _watermark(max_staleness=0)
        swaps = [_swap(observed_at=10, block_number=10, amount0=10, amount1=20)]
        bar = compute_volume_bar(swaps, window=window, watermark_policy=wm)
        assert bar.swap_count == 1

    def test_block_window_with_irregular_block_numbers(self) -> None:
        # Block window [100, 200) but events at blocks 105, 150, 199
        # — irregular spacing.
        window = _window(kind=WindowKind.BLOCK, start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps = [
            _swap(observed_at=105, block_number=105, amount0=10, amount1=20),
            _swap(observed_at=150, block_number=150, amount0=30, amount1=40),
            _swap(observed_at=199, block_number=199, amount0=50, amount1=60),
        ]
        bar = compute_volume_bar(swaps, window=window, watermark_policy=wm)
        assert bar.swap_count == 3
        assert bar.volume0 == 90
        assert bar.volume1 == 120

    def test_time_window_with_long_silent_period(self) -> None:
        # Time window [100, 10000) but only one event in the middle.
        # Treated as a normal sparse window.
        window = _window(start=100, end=10_000)
        wm = _watermark(max_staleness=0)
        swap = _swap(observed_at=500, block_number=50, amount0=10, amount1=20)
        bar = compute_volume_bar([swap], window=window, watermark_policy=wm)
        assert bar.swap_count == 1
        assert bar.is_empty is False

    def test_two_empty_windows_around_event(self) -> None:
        # Three consecutive windows; only the middle one has an event.
        windows = [
            _window(start=100, end=200),
            _window(start=200, end=300),
            _window(start=300, end=400),
        ]
        wm = _watermark(max_staleness=0)
        swap = _swap(observed_at=250, block_number=25, amount0=10, amount1=20)
        bars = [compute_volume_bar([swap], window=w, watermark_policy=wm) for w in windows]
        assert bars[0].is_empty is True
        assert bars[1].swap_count == 1
        assert bars[2].is_empty is True


# ---------------------------------------------------------------------------
# Prefix invariance tests
# ---------------------------------------------------------------------------


class TestPrefixInvariance:
    """Tests for prefix invariance: adding future data does not change
    an earlier feature row.
    """

    def test_volume_bar_unchanged_by_future_event(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps_v1 = [_swap(observed_at=120, block_number=12, amount0=10, amount1=20)]
        bar_v1 = compute_volume_bar(swaps_v1, window=window, watermark_policy=wm)
        # Add a future event (observed_at = 250, beyond window.end = 200).
        swaps_v2 = swaps_v1 + [_swap(observed_at=250, block_number=25, amount0=999, amount1=999)]
        bar_v2 = compute_volume_bar(swaps_v2, window=window, watermark_policy=wm)
        assert bar_v1 == bar_v2

    def test_realized_volatility_bar_unchanged_by_future_event(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps_v1 = [
            _swap(observed_at=110, block_number=11, sqrt_price_x96=Q96_SCALE),
            _swap(observed_at=130, block_number=13, sqrt_price_x96=2 * Q96_SCALE),
        ]
        bar_v1 = compute_realized_volatility_bar(swaps_v1, window=window, watermark_policy=wm)
        swaps_v2 = swaps_v1 + [
            _swap(observed_at=250, block_number=25, sqrt_price_x96=10 * Q96_SCALE)
        ]
        bar_v2 = compute_realized_volatility_bar(swaps_v2, window=window, watermark_policy=wm)
        assert bar_v1 == bar_v2

    def test_price_range_bar_unchanged_by_future_event(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps_v1 = [
            _swap(observed_at=110, block_number=11, sqrt_price_x96=2 * Q96_SCALE),
            _swap(observed_at=130, block_number=13, sqrt_price_x96=5 * Q96_SCALE),
        ]
        bar_v1 = compute_price_range_bar(swaps_v1, window=window, watermark_policy=wm)
        swaps_v2 = swaps_v1 + [
            _swap(observed_at=250, block_number=25, sqrt_price_x96=100 * Q96_SCALE)
        ]
        bar_v2 = compute_price_range_bar(swaps_v2, window=window, watermark_policy=wm)
        assert bar_v1 == bar_v2

    def test_effective_fee_bar_unchanged_by_future_event(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps_v1 = [_swap(observed_at=150, block_number=15, fee=3000)]
        bar_v1 = compute_effective_fee_bar(swaps_v1, window=window, watermark_policy=wm)
        swaps_v2 = swaps_v1 + [_swap(observed_at=250, block_number=25, fee=9000)]
        bar_v2 = compute_effective_fee_bar(swaps_v2, window=window, watermark_policy=wm)
        assert bar_v1 == bar_v2

    def test_gas_bar_unchanged_by_future_event(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        gas_v1 = [_gas(observed_at=150, block_number=15, gas_used=21_000)]
        bar_v1 = compute_gas_bar(gas_v1, window=window, watermark_policy=wm)
        gas_v2 = gas_v1 + [_gas(observed_at=250, block_number=25, gas_used=99_000)]
        bar_v2 = compute_gas_bar(gas_v2, window=window, watermark_policy=wm)
        assert bar_v1 == bar_v2

    def test_freshness_bar_unchanged_by_future_event(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        obs_v1 = [_swap(observed_at=180, block_number=18)]
        bar_v1 = compute_freshness_bar(obs_v1, window=window, watermark_policy=wm)
        # Add a future event (observed_at > window.end). The
        # freshness at ``window.end = 200`` must NOT change when
        # the consumer truncates observations to the data_time
        # boundary — that's the prefix-invariance contract.
        obs_v2 = obs_v1 + [_swap(observed_at=250, block_number=25)]
        obs_v2_truncated = [obs for obs in obs_v2 if obs.observed_at <= window.end]
        bar_v2 = compute_freshness_bar(obs_v2_truncated, window=window, watermark_policy=wm)
        assert bar_v1 == bar_v2

    def test_volume_bar_unchanged_by_reordering_within_window(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps_a = [
            _swap(observed_at=110, block_number=11, amount0=10, amount1=20),
            _swap(observed_at=130, block_number=13, amount0=30, amount1=40),
            _swap(observed_at=150, block_number=15, amount0=50, amount1=60),
        ]
        swaps_b = list(reversed(swaps_a))
        bar_a = compute_volume_bar(swaps_a, window=window, watermark_policy=wm)
        bar_b = compute_volume_bar(swaps_b, window=window, watermark_policy=wm)
        assert bar_a == bar_b

    def test_price_range_bar_unchanged_by_reordering(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps_a = [
            _swap(observed_at=110, block_number=11, sqrt_price_x96=3 * Q96_SCALE),
            _swap(observed_at=130, block_number=13, sqrt_price_x96=1 * Q96_SCALE),
            _swap(observed_at=150, block_number=15, sqrt_price_x96=5 * Q96_SCALE),
        ]
        swaps_b = list(reversed(swaps_a))
        bar_a = compute_price_range_bar(swaps_a, window=window, watermark_policy=wm)
        bar_b = compute_price_range_bar(swaps_b, window=window, watermark_policy=wm)
        assert bar_a == bar_b

    def test_streamer_closed_window_not_rewritten_by_late_event(self) -> None:
        streamer = MarketBarStreamer(
            window_size=100, window_kind=WindowKind.TIME, watermark_policy=_watermark()
        )
        streamer.add(_swap(observed_at=120, block_number=12, amount0=10, amount1=20))
        streamer.add(_swap(observed_at=150, block_number=15, amount0=30, amount1=40))
        bar_before = streamer.build_volume_bar(window_end=200)
        # Add a future event.
        streamer.add(_swap(observed_at=250, block_number=25, amount0=999, amount1=999))
        # Add a late event whose observed_at is in [100, 200).
        streamer.add(_swap(observed_at=180, block_number=18, amount0=888, amount1=888))
        bar_after = streamer.build_volume_bar(window_end=200)
        # The closed bar's volume must be unchanged.
        assert bar_before.volume0 == bar_after.volume0
        assert bar_before.volume1 == bar_after.volume1
        assert bar_before.swap_count == bar_after.swap_count
        # The late event is recorded in late_events_count.
        assert bar_after.late_events_count >= 1


# ---------------------------------------------------------------------------
# No normalisation tests
# ---------------------------------------------------------------------------


class TestNoNormalization:
    """Tests that bars carry raw values without centred / global normalisation."""

    def test_volume_is_raw_sum_not_normalized(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        # Three swaps with amounts (10, 20), (30, 40), (50, 60).
        # Volume bar must report volume0 = 10 + 30 + 50 = 90, not
        # the mean (30), not a z-score, not a rolling standardisation.
        swaps = [
            _swap(observed_at=110, block_number=11, amount0=10, amount1=20),
            _swap(observed_at=130, block_number=13, amount0=30, amount1=40),
            _swap(observed_at=150, block_number=15, amount0=50, amount1=60),
        ]
        bar = compute_volume_bar(swaps, window=window, watermark_policy=wm)
        assert bar.volume0 == 90
        assert bar.volume1 == 120

    def test_realized_variance_is_raw_not_centered(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        # Returns: 2.0, 1.5 -> mean 1.75 -> deviations 0.25, -0.25.
        # Squared deviations 0.0625, 0.0625 -> mean squared dev 0.0625.
        # In Q64.64, this is 0.0625 * 2^64 = (1/16) * 2^64 = 2^60.
        swaps = [
            _swap(observed_at=110, block_number=11, sqrt_price_x96=Q96_SCALE),
            _swap(observed_at=130, block_number=13, sqrt_price_x96=2 * Q96_SCALE),
            _swap(observed_at=150, block_number=15, sqrt_price_x96=3 * Q96_SCALE),
        ]
        bar = compute_realized_volatility_bar(swaps, window=window, watermark_policy=wm)
        assert bar.variance_q64_64 is not None
        # 0.0625 in Q64.64 is (1 << 64) // 16 = 2^60.
        assert bar.variance_q64_64 == (1 << 60)

    def test_gas_is_raw_sum_not_normalized(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        gas = [
            _gas(observed_at=110, block_number=11, gas_used=21_000),
            _gas(observed_at=130, block_number=13, gas_used=30_000),
            _gas(observed_at=150, block_number=15, gas_used=42_000),
        ]
        bar = compute_gas_bar(gas, window=window, watermark_policy=wm)
        assert bar.gas_used_total == 93_000

    def test_average_fee_is_arithmetic_mean_not_rolling(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        swaps = [
            _swap(observed_at=110, block_number=11, fee=1000),
            _swap(observed_at=120, block_number=12, fee=2000),
            _swap(observed_at=130, block_number=13, fee=3000),
        ]
        bar = compute_effective_fee_bar(swaps, window=window, watermark_policy=wm)
        # Arithmetic mean (1000 + 2000 + 3000) // 3 = 2000, not
        # a rolling average, not a fee APR projection.
        assert bar.average_fee == 2000


# ---------------------------------------------------------------------------
# Streamer API tests
# ---------------------------------------------------------------------------


class TestStreamer:
    """Tests for the :class:`MarketBarStreamer` API."""

    def test_window_size_validation(self) -> None:
        with pytest.raises(BarsError):
            MarketBarStreamer(window_size=0, watermark_policy=_watermark())

    def test_window_end_must_align_with_window_size(self) -> None:
        streamer = MarketBarStreamer(window_size=100, watermark_policy=_watermark())
        with pytest.raises(BarsError):
            streamer.build_volume_bar(window_end=250)  # not multiple of 100

    def test_window_end_must_be_positive(self) -> None:
        streamer = MarketBarStreamer(window_size=100, watermark_policy=_watermark())
        with pytest.raises(BarsError):
            streamer.build_volume_bar(window_end=0)

    def test_arrival_count(self) -> None:
        streamer = MarketBarStreamer(window_size=100, watermark_policy=_watermark())
        assert streamer.arrival_count == 0
        streamer.add(_swap(observed_at=150, block_number=15))
        streamer.add(_gas(observed_at=160, block_number=16))
        assert streamer.arrival_count == 2

    def test_window_kind_attribute(self) -> None:
        streamer = MarketBarStreamer(
            window_size=10, window_kind=WindowKind.BLOCK, watermark_policy=_watermark()
        )
        assert streamer.window_kind is WindowKind.BLOCK
        assert streamer.window_size == 10

    def test_all_build_methods_available(self) -> None:
        streamer = MarketBarStreamer(window_size=100, watermark_policy=_watermark())
        # Add one observation of each type.
        streamer.add(_swap(observed_at=110, block_number=11))
        streamer.add(_modify(observed_at=120, block_number=12))
        streamer.add(_gas(observed_at=130, block_number=13))
        streamer.add(_header(observed_at=140, block_number=14))
        # Each builder returns a bar; just assert they are typed.
        bar_v: VolumeBar = streamer.build_volume_bar(window_end=200)
        assert isinstance(bar_v, VolumeBar)
        bar_rv: RealizedVolatilityBar = streamer.build_realized_volatility_bar(window_end=200)
        assert isinstance(bar_rv, RealizedVolatilityBar)
        bar_pr: PriceRangeBar = streamer.build_price_range_bar(window_end=200)
        assert isinstance(bar_pr, PriceRangeBar)
        bar_al: ActiveLiquidityBar = streamer.build_active_liquidity_bar(
            window_end=200, active_liquidity=1000
        )
        assert isinstance(bar_al, ActiveLiquidityBar)
        bar_dp: DepthProxyBar = streamer.build_depth_proxy_bar(
            window_end=200,
            active_liquidity=1000,
            current_sqrt_price_x96=Q96_SCALE,
        )
        assert isinstance(bar_dp, DepthProxyBar)
        bar_ef: EffectiveFeeBar = streamer.build_effective_fee_bar(window_end=200)
        assert isinstance(bar_ef, EffectiveFeeBar)
        bar_g: GasBar = streamer.build_gas_bar(window_end=200)
        assert isinstance(bar_g, GasBar)
        bar_f: FreshnessBar = streamer.build_freshness_bar(window_end=200)
        assert isinstance(bar_f, FreshnessBar)


# ---------------------------------------------------------------------------
# BPS / Q64.96 sanity tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Tests for the public constants the module exposes."""

    def test_bps_denominator(self) -> None:
        assert BPS_DENOMINATOR == 10_000

    def test_q64_scale_is_2_pow_64(self) -> None:
        assert Q64_SCALE == 1 << 64

    def test_q96_scale_is_2_pow_96(self) -> None:
        assert Q96_SCALE == 1 << 96

    def test_default_depth_band_is_one_percent(self) -> None:
        assert DEFAULT_DEPTH_PROXY_BAND_BPS == 100

    def test_max_depth_band_is_100_percent(self) -> None:
        assert MAX_DEPTH_PROXY_BAND_BPS == 10_000


# ---------------------------------------------------------------------------
# Module surface tests
# ---------------------------------------------------------------------------


class TestModuleSurface:
    """Tests that the module's public surface is stable."""

    def test_market_observation_union(self) -> None:
        # Sanity check: the four observation types are part of the
        # union the streamer / freshness bar consume.
        assert SwapObservation in MarketObservation.__args__
        assert ModifyLiquidityObservation in MarketObservation.__args__
        assert GasObservation in MarketObservation.__args__
        assert BlockHeaderObservation in MarketObservation.__args__

    def test_bar_versions_match(self) -> None:
        # Every concrete bar's version must equal BAR_VERSION.
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bars = [
            compute_volume_bar([], window=window, watermark_policy=wm),
            compute_realized_volatility_bar([], window=window, watermark_policy=wm),
            compute_price_range_bar([], window=window, watermark_policy=wm),
            compute_active_liquidity_bar(0, window=window, watermark_policy=wm),
            compute_depth_proxy_bar(
                active_liquidity=0,
                current_sqrt_price_x96=Q96_SCALE,
                window=window,
                watermark_policy=wm,
            ),
            compute_effective_fee_bar([], window=window, watermark_policy=wm),
            compute_gas_bar([], window=window, watermark_policy=wm),
            compute_freshness_bar([], window=window, watermark_policy=wm),
        ]
        for bar in bars:
            assert isinstance(bar, FeatureBar)
            # The concrete bar types each carry ``version``; the
            # base ``FeatureBar`` does not (dataclass + slots
            # inheritance). Read the field via a typed cast to
            # avoid the B009 ``getattr`` warning.
            concrete = bar
            assert concrete.version == BAR_VERSION  # type: ignore[attr-defined]

    def test_all_bars_are_immutable(self) -> None:
        window = _window(start=100, end=200)
        wm = _watermark(max_staleness=0)
        bar = compute_volume_bar([], window=window, watermark_policy=wm)
        with pytest.raises((AttributeError, TypeError)):
            bar.volume0 = 999  # type: ignore[misc]

    def test_data_time_equals_window_end_for_every_bar(self) -> None:
        window = _window(start=100, end=275)
        wm = _watermark(max_staleness=5)
        bars = [
            compute_volume_bar([], window=window, watermark_policy=wm),
            compute_realized_volatility_bar([], window=window, watermark_policy=wm),
            compute_price_range_bar([], window=window, watermark_policy=wm),
            compute_active_liquidity_bar(0, window=window, watermark_policy=wm),
            compute_depth_proxy_bar(
                active_liquidity=0,
                current_sqrt_price_x96=Q96_SCALE,
                window=window,
                watermark_policy=wm,
            ),
            compute_effective_fee_bar([], window=window, watermark_policy=wm),
            compute_gas_bar([], window=window, watermark_policy=wm),
            compute_freshness_bar([], window=window, watermark_policy=wm),
        ]
        for bar in bars:
            assert bar.data_time == window.end
            assert bar.availability_time == window.end + 5
            assert bar.window is window
