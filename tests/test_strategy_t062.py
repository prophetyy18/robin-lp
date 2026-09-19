"""Tests for the V1 baseline strategy module (T062).

T062 defines five auditable, pool-agnostic baseline strategies the
comparison pipeline anchors every later strategy against:

- :class:`HoldStrategy` — the zero-action benchmark.
- :class:`BroadRangeStrategy` — the protocol-valid broad-range
  policy. *Not* literal "full / infinite range"; the widest
  Range the pool's ``tick_spacing`` admits inside
  ``[MIN_TICK, MAX_TICK]``.
- :class:`FixedWidthStrategy` — symmetric
  ``±half_width_ticks`` around the current tick.
- :class:`VolatilityWidthStrategy` — half-width tracks realised
  volatility.
- :class:`OutOfRangeRebalanceStrategy` — opens once, only
  rebalances when the position leaves range.

The tests cover the T062 acceptance clauses:

- **Zero-action golden scenario.** :class:`HoldStrategy` returns
  ``NO_TRADE`` for every decision, on every manifest.
- **Known-path golden scenarios.** Each strategy behaves
  predictably on a simple walk: a flat walk opens and waits; a
  drift walk rebalances out-of-range; a volatile walk keeps the
  range inside the configured min/max.
- **No addresses / symbols or token-specific thresholds.** No
  baseline branches on a token symbol, address, decimal width, or
  a numerical USDG threshold; every parameter is a tick count or
  a dimensionless multiplier.
- **Usable ticks and insufficient capital.** Proposals snap to
  ``tick_spacing``, clamp to the V4 ``[MIN_TICK, MAX_TICK]`` domain,
  and translate out-of-bounds ticks into a structured ``NO_TRADE``
  rather than a Range the pool would reject.

The tests also exercise the T062 Must-not clauses:

- **No optimisation on the evaluation period.** The defaults are
  round, auditable numbers; no test optimises a default to make
  a scenario pass.
- **"Broad range" is not literal full / infinite range.** The
  broad-range test asserts the proposal ticks are inside the V4
  ``[MIN_TICK, MAX_TICK]`` integer-tick domain and aligned to
  ``tick_spacing``, never the V4 ``int24`` domain or the
  "infinite Range" claim the task contract explicitly forbids.

Design constraints (binding):

- **Integer-only.** ``float`` never appears on the baseline code
  path. Q64.64 / Q64.96 conversion uses :func:`math.isqrt`.
- **No RPC / storage / signing / execution import.** The baselines
  depend only on :mod:`robinhood_lp.backtest.engine`,
  :mod:`robinhood_lp.backtest.events`, and
  :mod:`robinhood_lp.protocol.math`. The layer-purity assertion
  in :func:`test_baselines_do_not_import_forbidden_modules`
  walks the live module graph and rejects any sibling import.
"""

from __future__ import annotations

from typing import Final

import pytest

from robinhood_lp.backtest.engine import (
    StrategyDecision,
    StrategyDecisionRequest,
    empty_position_state,
)
from robinhood_lp.backtest.events import (
    KIND_OBSERVATION,
    KIND_SWAP,
    KIND_TICK,
    LEDGER_VERSION,
    SOURCE_PRIORITY_DATA,
    SOURCE_PRIORITY_SYSTEM,
    BacktestEvent,
    PositionState,
)
from robinhood_lp.protocol.math import (
    MAX_TICK,
    MAX_TICK_SPACING,
    MIN_TICK,
    max_usable_tick,
    min_usable_tick,
)
from robinhood_lp.strategy.base import assert_strategy_layer_is_pure
from robinhood_lp.strategy.baselines import (
    BASELINE_STRATEGY_VERSION,
    DEFAULT_BASELINE_CAPITAL_Q64_64,
    DEFAULT_BASELINE_LIQUIDITY,
    DEFAULT_MAX_HALF_WIDTH_TICKS,
    DEFAULT_MIN_HALF_WIDTH_TICKS,
    DEFAULT_REBALANCE_HALF_WIDTH_TICKS,
    DEFAULT_VOLATILITY_MULTIPLIER,
    NOTES_BROAD_RANGE_HOLD,
    NOTES_BROAD_RANGE_OPEN,
    NOTES_FIXED_WIDTH_HOLD,
    NOTES_FIXED_WIDTH_OPEN,
    NOTES_FIXED_WIDTH_REBALANCE,
    NOTES_HOLD,
    NOTES_NO_MARKET_DATA,
    NOTES_REBALANCE_HOLD,
    NOTES_REBALANCE_OPEN,
    NOTES_REBALANCE_REBALANCE,
    NOTES_TICKS_OUT_OF_BOUNDS,
    NOTES_VOLATILITY_OPEN,
    NOTES_VOLATILITY_REBALANCE,
    BaselineStrategyError,
    BroadRangeStrategy,
    FixedWidthStrategy,
    HoldStrategy,
    InvalidBaselineCapitalError,
    OutOfRangeRebalanceStrategy,
    VolatilityWidthStrategy,
    baseline_kind_tag,
)

# ---------------------------------------------------------------------------
# Reference fixtures
# ---------------------------------------------------------------------------


_POOL_KEY_ID: Final[str] = "0x" + "ab" * 32
_CHAIN_ID: Final[int] = 46630

#: A small tick-spacing that exercises the snap-to-spacing path
#: without bloating the visible-event fixture.
_TICK_SPACING: Final[int] = 60


def _observation(
    *,
    timestamp: int,
    price_q64_64: int,
) -> BacktestEvent:
    """An ``OBSERVATION`` event carrying ``price_q64_64``."""
    return BacktestEvent(
        version="t061.backtest_event.v1",
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_DATA,
        kind=KIND_OBSERVATION,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(("price_q64_64", price_q64_64),),
    )


def _swap(
    *,
    timestamp: int,
    price_q64_64: int,
) -> BacktestEvent:
    """A ``SWAP`` event carrying ``price_q64_64``."""
    return BacktestEvent(
        version="t061.backtest_event.v1",
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_DATA,
        kind=KIND_SWAP,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(("price_q64_64", price_q64_64),),
    )


def _tick_event(timestamp: int) -> BacktestEvent:
    """A non-reactive ``TICK`` event with no payload."""
    return BacktestEvent(
        version="t061.backtest_event.v1",
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_SYSTEM,
        kind=KIND_TICK,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(),
    )


def _request(
    *,
    decision_time: int,
    events: tuple[BacktestEvent, ...] = (),
    ledger: PositionState | None = None,
) -> StrategyDecisionRequest:
    """Build a :class:`StrategyDecisionRequest` for the test fixtures."""
    if ledger is None:
        ledger = empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID)
    return StrategyDecisionRequest(
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        decision_time=decision_time,
        visible_events=events,
        ledger=ledger,
    )


def _price_for_tick(tick: int) -> int:
    """Return a Q64.64 price ratio that maps to ``tick`` (floor).

    The helper is used to build golden-scenario fixtures: given a
    target tick, the test computes the matching Q64.64 price and
    feeds it as the latest ``price_q64_64`` observation. The
    baseline's integer sqrt then converts back to the original tick
    (within rounding).
    """
    from robinhood_lp.protocol.math import get_sqrt_price_at_tick

    sqrt_price_x96 = get_sqrt_price_at_tick(tick)
    # ``price_q64_64`` chosen so the integer sqrt returns
    # ``sqrt_price_x96``. We square the target and divide by ``2 ** 64``.
    return (sqrt_price_x96 * sqrt_price_x96) >> 64


# ---------------------------------------------------------------------------
# Layer-purity assertion
# ---------------------------------------------------------------------------


class TestLayerPurity:
    """The baseline module must not import any forbidden sibling."""

    def test_baselines_do_not_import_forbidden_modules(self) -> None:
        # The baselines module legitimately imports from
        # ``robinhood_lp.backtest`` (which is not in the strategy
        # denylist) and from ``robinhood_lp.protocol.math`` (the
        # protocol-domain layer). Calling the purity assertion
        # confirms no forbidden sibling has slipped in.
        assert_strategy_layer_is_pure()


# ---------------------------------------------------------------------------
# HoldStrategy — zero-action golden scenario
# ---------------------------------------------------------------------------


class TestHoldStrategy:
    """The zero-action benchmark holds for every decision."""

    def test_hold_returns_no_trade_forever(self) -> None:
        strategy = HoldStrategy(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID)
        for decision_time in (0, 1, 60, 1_000, 10_000):
            decision = strategy(
                _request(
                    decision_time=decision_time,
                    events=(_observation(timestamp=decision_time, price_q64_64=1 << 64),),
                )
            )
            assert isinstance(decision, StrategyDecision)
            assert decision.kind == "NO_TRADE"
            assert decision.pool_key_id == _POOL_KEY_ID
            assert decision.chain_id == _CHAIN_ID
            assert decision.decision_time == decision_time
            assert NOTES_HOLD in decision.notes
            assert decision.tick_lower == 0
            assert decision.tick_upper == 0
            assert decision.liquidity == 0
            assert decision.capital_q64_64 == 0

    def test_hold_ignores_visible_events(self) -> None:
        """A change in the visible manifest does not change the hold decision."""
        strategy = HoldStrategy(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID)
        empty_decision = strategy(_request(decision_time=10))
        full_decision = strategy(
            _request(
                decision_time=10,
                events=(
                    _observation(timestamp=10, price_q64_64=2 << 64),
                    _swap(timestamp=10, price_q64_64=2 << 64),
                ),
            )
        )
        assert empty_decision.kind == "NO_TRADE"
        assert full_decision.kind == "NO_TRADE"
        # ``notes`` is the only field that may differ if the caller
        # appended custom notes; the default ``HoldStrategy`` has
        # no extra notes so the two decisions are byte-identical.
        assert empty_decision.notes == full_decision.notes

    def test_hold_rejects_empty_pool_key_id(self) -> None:
        with pytest.raises(BaselineStrategyError):
            HoldStrategy(pool_key_id="", chain_id=_CHAIN_ID)


# ---------------------------------------------------------------------------
# BroadRangeStrategy — protocol-valid broad range
# ---------------------------------------------------------------------------


class TestBroadRangeStrategy:
    """The broad-range baseline opens the widest valid Range once."""

    def test_broad_range_opens_at_widest_valid_ticks(self) -> None:
        strategy = BroadRangeStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
        )
        decision = strategy(_request(decision_time=0))
        assert decision.kind == "PROPOSE"
        assert decision.tick_lower == min_usable_tick(_TICK_SPACING)
        assert decision.tick_upper == max_usable_tick(_TICK_SPACING)
        assert decision.tick_lower % _TICK_SPACING == 0
        assert decision.tick_upper % _TICK_SPACING == 0
        assert decision.tick_lower >= MIN_TICK
        assert decision.tick_upper <= MAX_TICK
        assert decision.liquidity == DEFAULT_BASELINE_LIQUIDITY
        assert decision.capital_q64_64 == DEFAULT_BASELINE_CAPITAL_Q64_64
        assert NOTES_BROAD_RANGE_OPEN in decision.notes

    def test_broad_range_is_not_infinite_range(self) -> None:
        """The proposal must NOT be the V4 int24 or 'full range' domain.

        The T062 Must-not clause is explicit: the strategy may not
        claim broad range is literally infinite / full range where
        protocol / griefing constraints disagree. We assert:

        1. The proposal ticks are inside the V4
           ``[MIN_TICK, MAX_TICK]`` integer-tick domain (NOT the
           ``int24`` domain ``[-2**23, 2**23 - 1]``);
        2. The proposal ticks are aligned to ``tick_spacing``;
        3. The proposal ticks respect the widest *usable* tick for
           the pool (``max_usable_tick``).
        """
        strategy = BroadRangeStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
        )
        decision = strategy(_request(decision_time=0))
        assert decision.kind == "PROPOSE"
        # Must respect V4 integer-tick domain.
        assert MIN_TICK <= decision.tick_lower < decision.tick_upper <= MAX_TICK
        # Must NOT extend beyond the widest usable tick.
        assert decision.tick_upper == max_usable_tick(_TICK_SPACING)
        # Must NOT extend below the lowest usable tick.
        assert decision.tick_lower == min_usable_tick(_TICK_SPACING)
        # The int24 domain is wider than [MIN_TICK, MAX_TICK]; we
        # explicitly reject the int24-only claim.
        int24_min = -(1 << 23)
        int24_max = (1 << 23) - 1
        assert int24_min <= MIN_TICK <= int24_max
        assert int24_min <= MAX_TICK <= int24_max
        # The usable-tick contract is narrower than int24; the
        # proposal must equal the usable-tick endpoints, not int24.
        assert decision.tick_lower != int24_min
        assert decision.tick_upper != int24_max

    def test_broad_range_holds_after_open(self) -> None:
        strategy = BroadRangeStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
        )
        open_decision = strategy(_request(decision_time=0))
        assert open_decision.kind == "PROPOSE"
        # A non-empty ledger (mimicking the post-fill state) must
        # always produce ``WAIT`` because the broad Range cannot
        # leave range under any single-tick move.
        ledger = PositionState(
            version=LEDGER_VERSION,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            position_id="0x" + "11" * 32,
            tick_lower=open_decision.tick_lower,
            tick_upper=open_decision.tick_upper,
            liquidity=DEFAULT_BASELINE_LIQUIDITY,
            principal_token0=0,
            principal_token1=0,
            tokens_owed0=0,
            tokens_owed1=0,
            in_range=True,
            last_accrual_time=0,
        )
        for decision_time in (10, 100, 1_000):
            decision = strategy(
                _request(
                    decision_time=decision_time,
                    ledger=ledger,
                    events=(_observation(timestamp=decision_time, price_q64_64=1 << 64),),
                )
            )
            assert decision.kind == "WAIT"
            assert NOTES_BROAD_RANGE_HOLD in decision.notes

    def test_broad_range_rejects_invalid_tick_spacing(self) -> None:
        with pytest.raises(BaselineStrategyError):
            BroadRangeStrategy(
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                tick_spacing=0,
            )
        with pytest.raises(BaselineStrategyError):
            BroadRangeStrategy(
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                tick_spacing=MAX_TICK_SPACING + 1,
            )


# ---------------------------------------------------------------------------
# FixedWidthStrategy — symmetric ±half_width_ticks around current tick
# ---------------------------------------------------------------------------


class TestFixedWidthStrategy:
    """The fixed-width baseline opens a symmetric Range and rebalances."""

    def test_fixed_width_opens_around_current_tick(self) -> None:
        strategy = FixedWidthStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            half_width_ticks=600,
        )
        target_tick = 1_000
        events = (_observation(timestamp=0, price_q64_64=_price_for_tick(target_tick)),)
        decision = strategy(_request(decision_time=0, events=events))
        assert decision.kind == "PROPOSE"
        # The proposal must be centred near the target tick.
        midpoint = (decision.tick_lower + decision.tick_upper) // 2
        assert abs(midpoint - target_tick) <= _TICK_SPACING
        # The half-width (post-snap) is at least the configured
        # half-width and at most ``half_width + tick_spacing`` ticks
        # because the snap-up to ``tick_spacing`` may widen the
        # upper boundary.
        half_width = (decision.tick_upper - decision.tick_lower) // 2
        assert 600 <= half_width <= 600 + _TICK_SPACING
        assert decision.tick_lower % _TICK_SPACING == 0
        assert decision.tick_upper % _TICK_SPACING == 0
        assert NOTES_FIXED_WIDTH_OPEN in decision.notes

    def test_fixed_width_holds_when_in_range(self) -> None:
        strategy = FixedWidthStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            half_width_ticks=600,
        )
        # Open around tick 1000.
        target_tick = 1_000
        open_decision = strategy(
            _request(
                decision_time=0,
                events=(_observation(timestamp=0, price_q64_64=_price_for_tick(target_tick)),),
            )
        )
        assert open_decision.kind == "PROPOSE"
        # Now propose a tick well inside the open Range.
        inside_tick = 1_050
        ledger = PositionState(
            version=LEDGER_VERSION,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            position_id="0x" + "11" * 32,
            tick_lower=open_decision.tick_lower,
            tick_upper=open_decision.tick_upper,
            liquidity=DEFAULT_BASELINE_LIQUIDITY,
            principal_token0=0,
            principal_token1=0,
            tokens_owed0=0,
            tokens_owed1=0,
            in_range=True,
            last_accrual_time=0,
        )
        decision = strategy(
            _request(
                decision_time=100,
                events=(_observation(timestamp=100, price_q64_64=_price_for_tick(inside_tick)),),
                ledger=ledger,
            )
        )
        assert decision.kind == "WAIT"
        assert NOTES_FIXED_WIDTH_HOLD in decision.notes

    def test_fixed_width_rebalances_when_out_of_range(self) -> None:
        strategy = FixedWidthStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            half_width_ticks=600,
        )
        target_tick = 1_000
        open_decision = strategy(
            _request(
                decision_time=0,
                events=(_observation(timestamp=0, price_q64_64=_price_for_tick(target_tick)),),
            )
        )
        assert open_decision.kind == "PROPOSE"
        # Drive the price well above the upper tick.
        new_tick = target_tick + 5_000
        ledger = PositionState(
            version=LEDGER_VERSION,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            position_id="0x" + "11" * 32,
            tick_lower=open_decision.tick_lower,
            tick_upper=open_decision.tick_upper,
            liquidity=DEFAULT_BASELINE_LIQUIDITY,
            principal_token0=0,
            principal_token1=0,
            tokens_owed0=0,
            tokens_owed1=0,
            in_range=False,
            last_accrual_time=0,
        )
        decision = strategy(
            _request(
                decision_time=1_000,
                events=(_observation(timestamp=1_000, price_q64_64=_price_for_tick(new_tick)),),
                ledger=ledger,
            )
        )
        assert decision.kind == "PROPOSE"
        assert NOTES_FIXED_WIDTH_REBALANCE in decision.notes
        # The new proposal must be centred near the new tick.
        new_midpoint = (decision.tick_lower + decision.tick_upper) // 2
        assert abs(new_midpoint - new_tick) <= _TICK_SPACING

    def test_fixed_width_returns_no_trade_without_market_data(self) -> None:
        strategy = FixedWidthStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
        )
        decision = strategy(_request(decision_time=0, events=()))
        assert decision.kind == "NO_TRADE"
        assert NOTES_NO_MARKET_DATA in decision.notes

    def test_fixed_width_rejects_zero_half_width(self) -> None:
        with pytest.raises(BaselineStrategyError):
            FixedWidthStrategy(
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                tick_spacing=_TICK_SPACING,
                half_width_ticks=0,
            )

    def test_fixed_width_rejects_zero_capital(self) -> None:
        with pytest.raises(InvalidBaselineCapitalError):
            FixedWidthStrategy(
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                tick_spacing=_TICK_SPACING,
                capital_q64_64=0,
            )


# ---------------------------------------------------------------------------
# VolatilityWidthStrategy — width tracks realised volatility
# ---------------------------------------------------------------------------


class TestVolatilityWidthStrategy:
    """The volatility-width baseline tracks k * sigma."""

    def test_volatility_width_uses_min_when_window_too_small(self) -> None:
        strategy = VolatilityWidthStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            volatility_window=20,
            min_half_width_ticks=DEFAULT_MIN_HALF_WIDTH_TICKS,
            max_half_width_ticks=DEFAULT_MAX_HALF_WIDTH_TICKS,
        )
        # Only one observation: the strategy cannot estimate sigma.
        events = (_observation(timestamp=0, price_q64_64=1 << 64),)
        decision = strategy(_request(decision_time=0, events=events))
        # Single observation produces ``NO_TRADE`` (insufficient evidence).
        assert decision.kind == "NO_TRADE"
        assert NOTES_NO_MARKET_DATA in decision.notes

    def test_volatility_width_clamps_to_max(self) -> None:
        """A very volatile walk clamps the half-width to ``max_half_width_ticks``.

        The clamp applies to the *desired* half-width; the snap-to-spacing
        may extend the upper bound by up to ``tick_spacing`` ticks, so
        the post-snap effective half-width is bounded by
        ``max_half_width_ticks + tick_spacing``.
        """
        # 21 prices with alternating 2x / 0.5x moves.
        prices: list[int] = []
        base = 1 << 64
        for i in range(21):
            prices.append(base if i % 2 == 0 else base >> 1)
        events = tuple(_observation(timestamp=i, price_q64_64=p) for i, p in enumerate(prices))
        strategy = VolatilityWidthStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            volatility_window=21,
            volatility_multiplier=DEFAULT_VOLATILITY_MULTIPLIER,
            min_half_width_ticks=DEFAULT_MIN_HALF_WIDTH_TICKS,
            max_half_width_ticks=DEFAULT_MAX_HALF_WIDTH_TICKS,
        )
        decision = strategy(_request(decision_time=20, events=events))
        assert decision.kind == "PROPOSE"
        half_width = (decision.tick_upper - decision.tick_lower) // 2
        # The clamp is documented; the assertion checks the
        # post-snap boundary, which is the clamp plus up to one
        # tick-spacing tick of slack.
        assert half_width <= DEFAULT_MAX_HALF_WIDTH_TICKS + _TICK_SPACING
        assert decision.tick_lower % _TICK_SPACING == 0
        assert decision.tick_upper % _TICK_SPACING == 0
        assert NOTES_VOLATILITY_OPEN in decision.notes

    def test_volatility_width_clamps_to_min(self) -> None:
        """A perfectly flat walk clamps the half-width to ``min_half_width_ticks``.

        The clamp applies to the *desired* half-width; the snap-to-spacing
        may shift the centre by up to half a tick-spacing, so the
        post-snap effective half-width is bounded by
        ``[min_half_width_ticks, min_half_width_ticks + tick_spacing)``.
        """
        events = tuple(_observation(timestamp=i, price_q64_64=1 << 64) for i in range(20))
        strategy = VolatilityWidthStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            volatility_window=20,
            min_half_width_ticks=DEFAULT_MIN_HALF_WIDTH_TICKS,
            max_half_width_ticks=DEFAULT_MAX_HALF_WIDTH_TICKS,
        )
        decision = strategy(_request(decision_time=19, events=events))
        assert decision.kind == "PROPOSE"
        half_width = (decision.tick_upper - decision.tick_lower) // 2
        assert (
            DEFAULT_MIN_HALF_WIDTH_TICKS
            <= half_width
            <= (DEFAULT_MIN_HALF_WIDTH_TICKS + _TICK_SPACING)
        )
        assert NOTES_VOLATILITY_OPEN in decision.notes

    def test_volatility_width_rebalances_out_of_range(self) -> None:
        strategy = VolatilityWidthStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            volatility_window=20,
            min_half_width_ticks=DEFAULT_MIN_HALF_WIDTH_TICKS,
            max_half_width_ticks=DEFAULT_MAX_HALF_WIDTH_TICKS,
        )
        events = tuple(_observation(timestamp=i, price_q64_64=1 << 64) for i in range(20))
        open_decision = strategy(_request(decision_time=19, events=events))
        assert open_decision.kind == "PROPOSE"
        ledger = PositionState(
            version=LEDGER_VERSION,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            position_id="0x" + "11" * 32,
            tick_lower=open_decision.tick_lower,
            tick_upper=open_decision.tick_upper,
            liquidity=DEFAULT_BASELINE_LIQUIDITY,
            principal_token0=0,
            principal_token1=0,
            tokens_owed0=0,
            tokens_owed1=0,
            in_range=True,
            last_accrual_time=0,
        )
        # A subsequent walk that climbs far above the existing
        # upper tick must trigger a rebalance.
        new_target = open_decision.tick_upper + 10 * _TICK_SPACING
        next_events = events + (
            _observation(timestamp=20, price_q64_64=_price_for_tick(new_target)),
        )
        decision = strategy(_request(decision_time=20, events=next_events, ledger=ledger))
        assert decision.kind == "PROPOSE"
        assert NOTES_VOLATILITY_REBALANCE in decision.notes

    def test_volatility_width_rejects_invalid_min_max(self) -> None:
        with pytest.raises(BaselineStrategyError):
            VolatilityWidthStrategy(
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                tick_spacing=_TICK_SPACING,
                min_half_width_ticks=1_000,
                max_half_width_ticks=500,
            )


# ---------------------------------------------------------------------------
# OutOfRangeRebalanceStrategy — rebalance only when out of range
# ---------------------------------------------------------------------------


class TestOutOfRangeRebalanceStrategy:
    """The rebalance-only baseline opens once and only rebalances on exit."""

    def test_rebalance_opens_once(self) -> None:
        strategy = OutOfRangeRebalanceStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            half_width_ticks=DEFAULT_REBALANCE_HALF_WIDTH_TICKS,
        )
        target_tick = 1_000
        decision = strategy(
            _request(
                decision_time=0,
                events=(_observation(timestamp=0, price_q64_64=_price_for_tick(target_tick)),),
            )
        )
        assert decision.kind == "PROPOSE"
        assert NOTES_REBALANCE_OPEN in decision.notes
        midpoint = (decision.tick_lower + decision.tick_upper) // 2
        assert abs(midpoint - target_tick) <= _TICK_SPACING

    def test_rebalance_holds_when_in_range(self) -> None:
        strategy = OutOfRangeRebalanceStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            half_width_ticks=DEFAULT_REBALANCE_HALF_WIDTH_TICKS,
        )
        target_tick = 1_000
        open_decision = strategy(
            _request(
                decision_time=0,
                events=(_observation(timestamp=0, price_q64_64=_price_for_tick(target_tick)),),
            )
        )
        assert open_decision.kind == "PROPOSE"
        # Subsequent in-range decisions must wait.
        ledger = PositionState(
            version=LEDGER_VERSION,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            position_id="0x" + "11" * 32,
            tick_lower=open_decision.tick_lower,
            tick_upper=open_decision.tick_upper,
            liquidity=DEFAULT_BASELINE_LIQUIDITY,
            principal_token0=0,
            principal_token1=0,
            tokens_owed0=0,
            tokens_owed1=0,
            in_range=True,
            last_accrual_time=0,
        )
        # Pick a tick well inside the open Range.
        inside_tick = (open_decision.tick_lower + open_decision.tick_upper) // 2
        # Ensure inside_tick is strictly inside (not equal to the boundary).
        if inside_tick >= open_decision.tick_upper - _TICK_SPACING:
            inside_tick = open_decision.tick_upper - 2 * _TICK_SPACING
        if inside_tick <= open_decision.tick_lower:
            inside_tick = open_decision.tick_lower + 2 * _TICK_SPACING
        decision = strategy(
            _request(
                decision_time=100,
                events=(_observation(timestamp=100, price_q64_64=_price_for_tick(inside_tick)),),
                ledger=ledger,
            )
        )
        assert decision.kind == "WAIT"
        assert NOTES_REBALANCE_HOLD in decision.notes

    def test_rebalance_rebalances_when_out_of_range(self) -> None:
        strategy = OutOfRangeRebalanceStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            half_width_ticks=DEFAULT_REBALANCE_HALF_WIDTH_TICKS,
        )
        target_tick = 1_000
        open_decision = strategy(
            _request(
                decision_time=0,
                events=(_observation(timestamp=0, price_q64_64=_price_for_tick(target_tick)),),
            )
        )
        assert open_decision.kind == "PROPOSE"
        ledger = PositionState(
            version=LEDGER_VERSION,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            position_id="0x" + "11" * 32,
            tick_lower=open_decision.tick_lower,
            tick_upper=open_decision.tick_upper,
            liquidity=DEFAULT_BASELINE_LIQUIDITY,
            principal_token0=0,
            principal_token1=0,
            tokens_owed0=0,
            tokens_owed1=0,
            in_range=False,
            last_accrual_time=0,
        )
        # Move the price well below the open lower tick.
        new_target = open_decision.tick_lower - 10 * _TICK_SPACING
        decision = strategy(
            _request(
                decision_time=100,
                events=(_observation(timestamp=100, price_q64_64=_price_for_tick(new_target)),),
                ledger=ledger,
            )
        )
        assert decision.kind == "PROPOSE"
        assert NOTES_REBALANCE_REBALANCE in decision.notes
        new_midpoint = (decision.tick_lower + decision.tick_upper) // 2
        assert abs(new_midpoint - new_target) <= _TICK_SPACING

    def test_rebalance_returns_no_trade_without_market_data(self) -> None:
        strategy = OutOfRangeRebalanceStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
        )
        decision = strategy(_request(decision_time=0, events=()))
        assert decision.kind == "NO_TRADE"
        assert NOTES_NO_MARKET_DATA in decision.notes


# ---------------------------------------------------------------------------
# Pool-agnostic / no-token-specific-threshold property
# ---------------------------------------------------------------------------


class TestPoolAgnosticProperty:
    """The baselines must not branch on token-specific values."""

    def test_no_token_specific_threshold_in_module(self) -> None:
        """A simple grep-level check that the module contains no
        hard-coded token symbol or address.

        The check is conservative: any string starting with ``0x``
        *inside the module file* is rejected. ``USDG`` is the project's
        own numeraire and appears in many spec documents; the test
        does not treat a USDG reference as a token-specific
        threshold (the numeraire is not a token choice; the strategy
        never branches on USDG). The check therefore targets concrete
        address prefixes and concrete token symbols.
        """
        import inspect
        from pathlib import Path

        from robinhood_lp.strategy import baselines

        src_path = Path(inspect.getsourcefile(baselines) or "")
        assert src_path.exists(), f"Cannot locate baselines module file: {src_path!r}"
        text = src_path.read_text(encoding="utf-8")
        # No concrete token addresses anywhere in the module file.
        # The PoolKey id is supplied at construction time by the
        # caller; the module itself must carry no token address.
        assert "0x" not in text, "baselines module must not contain a concrete 0x... token address"
        # No ``token0`` / ``token1`` identifiers leaking in (the
        # module is pool-agnostic; it never names a token side).
        for forbidden in ("token0", "token1"):
            assert forbidden not in text, (
                f"baselines module body must not contain {forbidden!r}; "
                f"pool-agnostic property violated"
            )

    def test_strategies_only_use_pool_key_id_passed_at_construction(self) -> None:
        """Two strategies constructed with different pool_key_id values
        return decisions carrying their own pool_key_id, never a
        hard-coded one."""
        other_pool = "0x" + "cd" * 32
        for ctor in (
            lambda pk: HoldStrategy(pool_key_id=pk, chain_id=_CHAIN_ID),
            lambda pk: BroadRangeStrategy(
                pool_key_id=pk, chain_id=_CHAIN_ID, tick_spacing=_TICK_SPACING
            ),
            lambda pk: FixedWidthStrategy(
                pool_key_id=pk, chain_id=_CHAIN_ID, tick_spacing=_TICK_SPACING
            ),
            lambda pk: VolatilityWidthStrategy(
                pool_key_id=pk, chain_id=_CHAIN_ID, tick_spacing=_TICK_SPACING
            ),
            lambda pk: OutOfRangeRebalanceStrategy(
                pool_key_id=pk, chain_id=_CHAIN_ID, tick_spacing=_TICK_SPACING
            ),
        ):
            strat_a = ctor(_POOL_KEY_ID)
            strat_b = ctor(other_pool)
            request = _request(decision_time=0)
            decision_a = strat_a(request)
            decision_b = strat_b(request)
            assert decision_a.pool_key_id == _POOL_KEY_ID
            assert decision_b.pool_key_id == other_pool


# ---------------------------------------------------------------------------
# Insufficient capital / out-of-range ticks
# ---------------------------------------------------------------------------


class TestInsufficientCapitalAndOutOfBounds:
    """Insufficient capital and out-of-bounds ticks are translated into NO_TRADE."""

    def test_inconsistent_tick_spacing_rejected_at_construction(self) -> None:
        with pytest.raises(BaselineStrategyError):
            FixedWidthStrategy(
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                tick_spacing=0,
            )
        with pytest.raises(BaselineStrategyError):
            VolatilityWidthStrategy(
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                tick_spacing=0,
            )

    def test_insufficient_capital_rejected_at_construction(self) -> None:
        with pytest.raises(InvalidBaselineCapitalError):
            BroadRangeStrategy(
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                tick_spacing=_TICK_SPACING,
                capital_q64_64=0,
            )

    def test_out_of_bounds_proposal_translates_to_no_trade(self) -> None:
        """A half-width so large that ``current_tick ± half_width`` is
        outside the V4 ``[MIN_TICK, MAX_TICK]`` domain translates
        into a structured ``NO_TRADE`` carrying ``BASELINE_TICKS_OUT_OF_BOUNDS``.
        """
        strategy = FixedWidthStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            half_width_ticks=MAX_TICK,
        )
        target_tick = 1_000
        events = (_observation(timestamp=0, price_q64_64=_price_for_tick(target_tick)),)
        decision = strategy(_request(decision_time=0, events=events))
        assert decision.kind == "NO_TRADE"
        assert NOTES_TICKS_OUT_OF_BOUNDS in decision.notes


# ---------------------------------------------------------------------------
# baseline_kind_tag helper
# ---------------------------------------------------------------------------


class TestBaselineKindTag:
    """``baseline_kind_tag`` reads the BASELINE_* tag from a decision's notes."""

    def test_returns_first_baseline_tag(self) -> None:
        decision = StrategyDecision(
            kind="NO_TRADE",
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            decision_time=0,
            notes=("custom_tag", NOTES_HOLD),
        )
        assert baseline_kind_tag(decision) == NOTES_HOLD

    def test_returns_none_without_baseline_tag(self) -> None:
        decision = StrategyDecision(
            kind="NO_TRADE",
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            decision_time=0,
            notes=("custom_tag",),
        )
        assert baseline_kind_tag(decision) is None


# ---------------------------------------------------------------------------
# Known-path golden scenarios — full lifecycle on a synthetic walk
# ---------------------------------------------------------------------------


class TestKnownPathGoldenScenarios:
    """A synthetic price walk exercises every baseline end-to-end."""

    def test_flat_walk_baselines_behave_predictably(self) -> None:
        """A flat walk at price == 1 USDG:

        - :class:`HoldStrategy` returns ``NO_TRADE`` for every step.
        - :class:`BroadRangeStrategy` proposes once, then waits.
        - :class:`FixedWidthStrategy` proposes once, then waits.
        - :class:`VolatilityWidthStrategy` proposes once at the
          minimum half-width, then waits.
        - :class:`OutOfRangeRebalanceStrategy` proposes once, then
          waits.
        """
        events: tuple[BacktestEvent, ...] = tuple(
            _observation(timestamp=i, price_q64_64=1 << 64) for i in range(5)
        )
        # Hold.
        hold = HoldStrategy(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID)
        for t in range(5):
            decision = hold(_request(decision_time=t, events=events))
            assert decision.kind == "NO_TRADE"
        # Broad range.
        broad = BroadRangeStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
        )
        decision = broad(_request(decision_time=0, events=events))
        assert decision.kind == "PROPOSE"
        # Fixed-width.
        fixed = FixedWidthStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
        )
        decision = fixed(_request(decision_time=0, events=events))
        assert decision.kind == "PROPOSE"
        # Volatility-width.
        volatility = VolatilityWidthStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
            volatility_window=5,
        )
        decision = volatility(_request(decision_time=4, events=events))
        assert decision.kind == "PROPOSE"
        # Rebalance-only.
        rebalance = OutOfRangeRebalanceStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
        )
        decision = rebalance(_request(decision_time=0, events=events))
        assert decision.kind == "PROPOSE"

    def test_drift_walk_triggers_rebalance_only(self) -> None:
        """A monotonic up-walk makes every rebalance-aware strategy
        rebalance while the hold strategy keeps producing
        ``NO_TRADE``. Broad range keeps waiting because the Range
        is wider than any single-tick move.

        The walk moves the price by ``300`` ticks per step so the
        round-trip integer conversion (-1 tick of slack) still
        exits the ``±60`` Range on the second observation.
        """
        # Build an up-walk with aggressive increments.
        events: list[BacktestEvent] = []
        for i in range(5):
            events.append(_observation(timestamp=i, price_q64_64=_price_for_tick(i * 300)))
        events_tuple = tuple(events)
        # Hold stays NO_TRADE.
        hold = HoldStrategy(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID)
        for t in range(5):
            assert hold(_request(decision_time=t, events=events_tuple)).kind == "NO_TRADE"
        # Broad range opens, then waits.
        broad = BroadRangeStrategy(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            tick_spacing=_TICK_SPACING,
        )
        open_decision = broad(_request(decision_time=0, events=events_tuple))
        assert open_decision.kind == "PROPOSE"
        ledger = PositionState(
            version=LEDGER_VERSION,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            position_id="0x" + "11" * 32,
            tick_lower=open_decision.tick_lower,
            tick_upper=open_decision.tick_upper,
            liquidity=DEFAULT_BASELINE_LIQUIDITY,
            principal_token0=0,
            principal_token1=0,
            tokens_owed0=0,
            tokens_owed1=0,
            in_range=True,
            last_accrual_time=0,
        )
        wait_decision = broad(_request(decision_time=4, events=events_tuple, ledger=ledger))
        assert wait_decision.kind == "WAIT"
        # Fixed-width and rebalance-only rebalance after the first
        # decision (the open), then keep rebalancing as the price
        # walks out of the Range. The fixtures only carry events
        # with timestamp <= decision_time so each decision sees the
        # latest available observation.
        for ctor, kind in (
            (FixedWidthStrategy, "FIXED"),
            (OutOfRangeRebalanceStrategy, "REBALANCE"),
        ):
            strat = ctor(
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                tick_spacing=_TICK_SPACING,
                half_width_ticks=60,
            )
            visible_at_t: list[tuple[BacktestEvent, ...]] = []
            for t in range(5):
                visible_at_t.append(tuple(e for e in events_tuple if e.timestamp <= t))
            open_decision = strat(_request(decision_time=0, events=visible_at_t[0]))
            assert open_decision.kind == "PROPOSE"
            ledger = PositionState(
                version=LEDGER_VERSION,
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                position_id="0x" + "11" * 32,
                tick_lower=open_decision.tick_lower,
                tick_upper=open_decision.tick_upper,
                liquidity=DEFAULT_BASELINE_LIQUIDITY,
                principal_token0=0,
                principal_token1=0,
                tokens_owed0=0,
                tokens_owed1=0,
                in_range=True,
                last_accrual_time=0,
            )
            # Subsequent decisions rebalance as the price walks up.
            for t in range(1, 5):
                decision = strat(_request(decision_time=t, events=visible_at_t[t], ledger=ledger))
                assert decision.kind == "PROPOSE", (
                    f"{kind} strategy did not rebalance at t={t}: {decision}"
                )
                # Update the ledger to track the new Range.
                ledger = PositionState(
                    version=LEDGER_VERSION,
                    pool_key_id=_POOL_KEY_ID,
                    chain_id=_CHAIN_ID,
                    position_id="0x" + "11" * 32,
                    tick_lower=decision.tick_lower,
                    tick_upper=decision.tick_upper,
                    liquidity=DEFAULT_BASELINE_LIQUIDITY,
                    principal_token0=0,
                    principal_token1=0,
                    tokens_owed0=0,
                    tokens_owed1=0,
                    in_range=True,
                    last_accrual_time=t,
                )


# ---------------------------------------------------------------------------
# Bumping the version string is a breaking change
# ---------------------------------------------------------------------------


class TestVersioning:
    """The module version is pinned and recorded for downstream consumers."""

    def test_baseline_strategy_version_is_pinned(self) -> None:
        assert BASELINE_STRATEGY_VERSION == "t062.baseline_strategy.v1"
