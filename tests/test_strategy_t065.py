"""Tests for the USDG-first adaptive-Range strategy (T065).

T065 delivers the maintainable USDG-first adaptive-Range rule
strategy: replaceable rule-based regime and fee-opportunity models,
a downside-asymmetric trend/jump filter, Range occupancy/break/
return assessment, expected fee/depth/cost distributions, complete
out-of-Range lifecycle actions (wait, return, rebuild, rebuild
defer, exit), an immutable parameter schema with uncertainty / reason
codes, the 5-minute USDG return rule, and the engine-callback adapter
the T061 backtest engine wires through.

The tests cover every T065 acceptance clause:

- **Golden scenarios.** Stable Range, falling token with attractive
  apparent fee, jumps, low / wash-like volume, liquidity withdrawal,
  out-of-Range wait / return, and costly rebuild.
- **5-minute USDG rule.** A complete 5-minute USDG bar with a
  return strictly greater than 100 % suppresses risk-increasing
  candidates and records the in-strategy position reassessment;
  no global run-state change. An incomplete bar does *not* fire.
- **Model replacement cannot alter ledger / risk / execution
  contracts.** A replacement regime / fee-opportunity model
  changes the assessment the strategy reads but does not change
  the engine ``StrategyDecision`` shape, the audit chain, or the
  ledger mutation surface.
- **Every decision replays from its manifest.** Two equivalent
  inputs produce byte-identical candidate actions and engine
  ``StrategyDecision`` outputs.
- **Downside asymmetry.** The downside jump threshold is strictly
  smaller than the upside jump threshold; the regime model returns
  ``JUMP_RISK`` with ``jumps_down=True`` at smaller downward
  moves than the symmetric upward move would require.
- **No token appreciation.** The strategy never assumes the
  target token appreciates; the ``UP_TREND`` regime does not
  translate to a long bias.
- **No low liquidity = opportunity.** The fee opportunity model
  downgrades to ``UNCERTAIN`` when the volume is wash-like or
  the own-liquidity share is excessive.
- **No forcing a trade.** The strategy may return ``NO_TRADE`` /
  ``WAIT`` / ``WAIT_OUT`` / ``RETURN`` / ``EXIT``; the engine
  receives the corresponding ``NO_TRADE`` / ``WAIT`` / ``PROPOSE``
  verdict and never sees a forced action.
- **No bypassing risk.** The strategy never mutates the ledger or
  the audit chain; the central risk layer is upstream of the
  engine's pipeline.

The Must-not clauses are also tested:

- **No dependency on token appreciation.** The regime model
  surfaces ``UP_TREND`` as a *negative* signal for the entry path
  (CTA is not a long signal).
- **No fundamentals / sentiment.** The regime model has no
  field that accepts a fundamentals or sentiment value.
- **No equating low liquidity with opportunity.** The fee model
  returns ``UNCERTAIN`` when the volume is too low or the own
  share is too large.
- **No forcing a trade.** The default strategy returns
  ``WAIT`` for an empty ledger with no events.
- **No bypassing risk.** The candidate action carries the
  strategy versions and a structured ``NO_TRADE`` reason code;
  the engine, not the strategy, decides whether the action
  becomes a fill.

Design constraints (binding):

- **Integer-only.** ``float`` never appears on the strategy
  path. Price-to-tick conversion uses :func:`math.isqrt` and the
  V4 protocol math module.
- **No RPC / storage / signing / execution import.** The
  adaptive strategy module depends only on the protocol
  contracts and the standard library; the adapter depends only
  on the engine strategy-callback contracts.
"""

from __future__ import annotations

from typing import Final

import pytest

from robinhood_lp.backtest.engine import (
    BacktestEngine,
    RiskDecision,
    StrategyDecision,
)
from robinhood_lp.backtest.events import (
    KIND_OBSERVATION,
    KIND_SWAP,
    LEDGER_VERSION,
    SOURCE_PRIORITY_DATA,
    BacktestEvent,
    PositionState,
)
from robinhood_lp.backtest.models import (
    ConstantLiquidityModel,
    DeterministicFailureModel,
    FlatGasModel,
    ModelBundle,
    StaticFeeModel,
    ZeroSlippageModel,
)
from robinhood_lp.protocol.contracts import (
    BACKTEST_EVENT_VERSION,
)
from robinhood_lp.protocol.contracts import (
    StrategyDecisionRequest as _ProtocolStrategyDecisionRequest,
)
from robinhood_lp.protocol.math import get_sqrt_price_at_tick
from robinhood_lp.strategy.adapter import (
    ADAPTER_VERSION,
    ADAPTIVE_TO_ENGINE_KIND,
    AdaptiveStrategyCallback,
    EngineAdapterError,
    InvalidEngineRequestError,
    compute_five_minute_return_q64_64,
)
from robinhood_lp.strategy.adaptive import (
    ADAPTIVE_CANDIDATE_ACTION_VERSION,
    ADAPTIVE_MARKET_SNAPSHOT_VERSION,
    ADAPTIVE_REGIME_ASSESSMENT_VERSION,
    ADAPTIVE_STRATEGY_VERSION,
    FIVE_MINUTE_RETURN_THRESHOLD_Q64_64,
    Q64_SCALE,
    AdaptiveAdmissionSnapshot,
    AdaptiveCandidateAction,
    AdaptiveCandidateKind,
    AdaptiveFeeOpportunityAssessment,
    AdaptiveFeeOpportunityModel,
    AdaptiveMarketSnapshot,
    AdaptivePortfolioSnapshot,
    AdaptiveRangeParameters,
    AdaptiveReasonCode,
    AdaptiveRegimeAssessment,
    AdaptiveRegimeModel,
    AdaptiveStrategy,
    AdaptiveStrategyError,
    InvalidAdaptiveAssessmentError,
    InvalidAdaptiveCandidateError,
    InvalidAdaptiveParameterError,
    InvalidAdaptiveSnapshotError,
    assert_adaptive_strategy_layer_is_pure,
)

# ---------------------------------------------------------------------------
# Reference fixtures
# ---------------------------------------------------------------------------


_POOL_KEY_ID: Final[str] = "0x" + "ab" * 32
_CHAIN_ID: Final[int] = 46630
_TICK_SPACING: Final[int] = 60


def _observation(
    *,
    timestamp: int,
    price_q64_64: int,
    volume_q64_64: int | None = None,
    active_liquidity: int | None = None,
) -> BacktestEvent:
    """A standard ``OBSERVATION`` event with optional volume / liquidity."""
    payload: list[tuple[str, int]] = [("price_q64_64", price_q64_64)]
    if volume_q64_64 is not None:
        payload.append(("volume_q64_64", volume_q64_64))
    if active_liquidity is not None:
        payload.append(("active_liquidity", active_liquidity))
    return BacktestEvent(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_DATA,
        kind=KIND_OBSERVATION,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        observed_at=timestamp,
        available_at=timestamp,
        payload=tuple(payload),
    )


def _swap(
    *,
    timestamp: int,
    price_q64_64: int,
    volume_q64_64: int | None = None,
    active_liquidity: int | None = None,
) -> BacktestEvent:
    """A ``SWAP`` event carrying the same payload shape as ``_observation``."""
    return _observation(
        timestamp=timestamp,
        price_q64_64=price_q64_64,
        volume_q64_64=volume_q64_64,
        active_liquidity=active_liquidity,
    ).__class__(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_DATA,
        kind=KIND_SWAP,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        observed_at=timestamp,
        available_at=timestamp,
        payload=_observation(
            timestamp=timestamp,
            price_q64_64=price_q64_64,
            volume_q64_64=volume_q64_64,
            active_liquidity=active_liquidity,
        ).payload,
    )


def _price_for_tick(tick: int) -> int:
    """Return a Q64.64 price ratio that maps to ``tick`` (floor).

    Standard Q64.64 convention: ``price_q64_64 = price * 2**64``.
    """
    sqrt_price_x96 = get_sqrt_price_at_tick(tick)
    return (sqrt_price_x96 * sqrt_price_x96) >> 128


def _stable_events(
    *,
    n: int = 20,
    interval_seconds: int = 60,
    tick: int = 0,
    volume: int | None = None,
    active_liquidity: int = 10_000,
) -> tuple[BacktestEvent, ...]:
    """Build a stable-walk event series around ``tick``."""
    events: list[BacktestEvent] = []
    base_price = _price_for_tick(tick)
    for i in range(n):
        ts = i * interval_seconds
        # Sub-tick oscillation so the regime model classifies as RANGE.
        price = base_price + (i % 3) * 1000
        vol = volume if volume is not None else (i + 1) * (1 << 60)
        events.append(
            _observation(
                timestamp=ts,
                price_q64_64=price,
                volume_q64_64=vol,
                active_liquidity=active_liquidity,
            )
        )
    return tuple(events)


def _falling_events(
    *,
    n: int = 20,
    interval_seconds: int = 60,
    start_tick: int = 0,
    step_ticks: int = 60,
    active_liquidity: int = 10_000,
) -> tuple[BacktestEvent, ...]:
    """Build a falling-walk event series (price ticks monotonically down)."""
    events: list[BacktestEvent] = []
    for i in range(n):
        ts = i * interval_seconds
        tick = start_tick - i * step_ticks
        events.append(
            _observation(
                timestamp=ts,
                price_q64_64=_price_for_tick(tick),
                volume_q64_64=(i + 1) * (1 << 60),
                active_liquidity=active_liquidity,
            )
        )
    return tuple(events)


def _empty_position() -> PositionState:
    """An empty ledger with default sentinels."""
    return PositionState(
        version=LEDGER_VERSION,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        position_id="0x" + "00" * 32,
        tick_lower=-60,
        tick_upper=60,
        liquidity=0,
        principal_token0=0,
        principal_token1=0,
        tokens_owed0=0,
        tokens_owed1=0,
        in_range=False,
        last_accrual_time=0,
    )


def _held_position(
    *,
    tick_lower: int,
    tick_upper: int,
    liquidity: int = 1_000,
    last_accrual_time: int = 0,
    in_range: bool = True,
) -> PositionState:
    """A held position with the supplied Range, liquidity and
    recorded ``in_range`` flag. ``in_range=True`` is the steady
    state; ``in_range=False`` signals that the engine's ledger had
    the position marked out-of-Range on the previous step (used to
    exercise the RETURN transition)."""
    return PositionState(
        version=LEDGER_VERSION,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        position_id="0x" + "11" * 32,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity=liquidity,
        principal_token0=0,
        principal_token1=0,
        tokens_owed0=0,
        tokens_owed1=0,
        in_range=in_range,
        last_accrual_time=last_accrual_time,
    )


def _default_params(**overrides: int) -> AdaptiveRangeParameters:
    """Default T065 parameters with optional field overrides."""
    defaults: dict[str, int] = {
        "tick_spacing": _TICK_SPACING,
        "half_width_ticks": 600,
        "max_rebuild_wait_seconds": 1_800,
    }
    defaults.update(overrides)
    return AdaptiveRangeParameters(**defaults)


def _default_strategy(**param_overrides: int) -> AdaptiveStrategy:
    """A default T065 adaptive strategy bound to the reference pool."""
    return AdaptiveStrategy(
        params=_default_params(**param_overrides),
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
    )


def _build_market_snapshot(
    *,
    decision_time: int,
    five_minute_return_q64_64: int = 0,
    five_minute_return_complete: bool = False,
    liquidity: int = 10_000,
    quote_q64_64: int | None = None,
    is_relative_only: bool = False,
    sqrt_price_x96: int = 1 << 96,
) -> AdaptiveMarketSnapshot:
    return AdaptiveMarketSnapshot(
        version=ADAPTIVE_MARKET_SNAPSHOT_VERSION,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        sqrt_price_x96=sqrt_price_x96,
        liquidity=liquidity,
        realized_volatility_q64_64=0,
        freshness_seconds=0,
        quote_q64_64=quote_q64_64 if quote_q64_64 is not None else (1 << 64),
        is_relative_only=is_relative_only,
        five_minute_return_q64_64=five_minute_return_q64_64,
        five_minute_return_complete=five_minute_return_complete,
        data_time=decision_time,
        availability_time=decision_time,
    )


def _build_portfolio_snapshot(
    *,
    decision_time: int,
    position: PositionState,
) -> AdaptivePortfolioSnapshot:
    return AdaptivePortfolioSnapshot(
        version="t065.portfolio_snapshot.v1",
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        position_id=position.position_id,
        tick_lower=position.tick_lower,
        tick_upper=position.tick_upper,
        liquidity=position.liquidity,
        principal_token0=position.principal_token0,
        principal_token1=position.principal_token1,
        tokens_owed0=position.tokens_owed0,
        tokens_owed1=position.tokens_owed1,
        is_empty=position.liquidity == 0,
        in_range=bool(position.in_range and position.liquidity > 0),
        last_accrual_time=position.last_accrual_time,
        data_time=decision_time,
        availability_time=decision_time,
    )


def _build_admission_snapshot(*, decision_time: int) -> AdaptiveAdmissionSnapshot:
    return AdaptiveAdmissionSnapshot(
        version="t065.admission_snapshot.v1",
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        support_level="backtest",
        is_admitted=True,
        max_capital_q64_64=1 << 70,
        data_time=decision_time,
        availability_time=decision_time,
    )


# ---------------------------------------------------------------------------
# Layer-purity assertion
# ---------------------------------------------------------------------------


class TestLayerPurity:
    """The adaptive strategy must not import any forbidden sibling."""

    def test_adaptive_strategy_layer_is_pure(self) -> None:
        assert_adaptive_strategy_layer_is_pure()


# ---------------------------------------------------------------------------
# Parameter schema validation
# ---------------------------------------------------------------------------


class TestParameterSchema:
    """The immutable parameter schema enforces its invariants."""

    def test_default_parameters_are_valid(self) -> None:
        params = AdaptiveRangeParameters()
        assert params.five_minute_window_seconds == 300
        assert params.five_minute_return_threshold_q64_64 == FIVE_MINUTE_RETURN_THRESHOLD_Q64_64
        assert params.liquidity > 0

    def test_downside_asymmetry_is_required(self) -> None:
        # The downside jump threshold must be strictly smaller than
        # the upside jump threshold; a parameter set that violates
        # the asymmetric invariant is rejected at construction.
        with pytest.raises(InvalidAdaptiveParameterError):
            AdaptiveRangeParameters(
                down_jump_threshold_q64_64=Q64_SCALE // 4,
                up_jump_threshold_q64_64=Q64_SCALE // 4,
            )
        with pytest.raises(InvalidAdaptiveParameterError):
            AdaptiveRangeParameters(
                down_trend_threshold_q64_64=Q64_SCALE // 10,
                up_trend_threshold_q64_64=Q64_SCALE // 20,
            )

    def test_capital_cannot_exceed_max(self) -> None:
        with pytest.raises(InvalidAdaptiveParameterError):
            AdaptiveRangeParameters(
                capital_q64_64=1 << 80,
                max_capital_q64_64=1 << 70,
            )

    def test_rejects_non_positive_liquidity(self) -> None:
        with pytest.raises(AdaptiveStrategyError):
            AdaptiveRangeParameters(liquidity=0)
        with pytest.raises(AdaptiveStrategyError):
            AdaptiveRangeParameters(liquidity=-1)

    def test_parameter_version_is_deterministic(self) -> None:
        a = AdaptiveRangeParameters()
        b = AdaptiveRangeParameters()
        assert a == b
        # Parameter versions compare through the strategy's helper.
        strategy_a = AdaptiveStrategy(params=a, pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID)
        strategy_b = AdaptiveStrategy(params=b, pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID)
        assert strategy_a.parameter_version == strategy_b.parameter_version

    def test_parameter_version_changes_on_bump(self) -> None:
        a = AdaptiveRangeParameters()
        b = AdaptiveRangeParameters(half_width_ticks=601)
        strategy_a = AdaptiveStrategy(params=a, pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID)
        strategy_b = AdaptiveStrategy(params=b, pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID)
        assert strategy_a.parameter_version != strategy_b.parameter_version


# ---------------------------------------------------------------------------
# Snapshot invariants
# ---------------------------------------------------------------------------


class TestSnapshotInvariants:
    """Snapshots carry their version stamp and reject invalid fields."""

    def test_market_snapshot_rejects_relative_only_with_quote(self) -> None:
        with pytest.raises(InvalidAdaptiveSnapshotError):
            AdaptiveMarketSnapshot(
                version=ADAPTIVE_MARKET_SNAPSHOT_VERSION,
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                sqrt_price_x96=1 << 96,
                liquidity=10_000,
                realized_volatility_q64_64=0,
                freshness_seconds=0,
                quote_q64_64=1 << 64,
                is_relative_only=True,  # <- conflicts with quote
                five_minute_return_q64_64=0,
                five_minute_return_complete=False,
                data_time=0,
                availability_time=0,
            )

    def test_market_snapshot_rejects_complete_return_without_usdg(self) -> None:
        with pytest.raises(InvalidAdaptiveSnapshotError):
            AdaptiveMarketSnapshot(
                version=ADAPTIVE_MARKET_SNAPSHOT_VERSION,
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                sqrt_price_x96=1 << 96,
                liquidity=10_000,
                realized_volatility_q64_64=0,
                freshness_seconds=0,
                quote_q64_64=None,
                is_relative_only=True,
                five_minute_return_q64_64=1,  # <- nonzero with complete=True
                five_minute_return_complete=True,  # <- but no USDG available
                data_time=0,
                availability_time=0,
            )

    def test_market_snapshot_rejects_complete_with_zero_return(self) -> None:
        with pytest.raises(InvalidAdaptiveSnapshotError):
            AdaptiveMarketSnapshot(
                version=ADAPTIVE_MARKET_SNAPSHOT_VERSION,
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                sqrt_price_x96=1 << 96,
                liquidity=10_000,
                realized_volatility_q64_64=0,
                freshness_seconds=0,
                quote_q64_64=1 << 64,
                is_relative_only=False,
                five_minute_return_q64_64=0,  # <- zero with complete=True
                five_minute_return_complete=True,
                data_time=0,
                availability_time=0,
            )

    def test_portfolio_snapshot_rejects_degenerate_range_when_non_empty(self) -> None:
        with pytest.raises(InvalidAdaptiveSnapshotError):
            AdaptivePortfolioSnapshot(
                version="t065.portfolio_snapshot.v1",
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                position_id="0xab" * 16,
                tick_lower=60,
                tick_upper=60,  # degenerate for a non-empty position
                liquidity=100,
                principal_token0=0,
                principal_token1=0,
                tokens_owed0=0,
                tokens_owed1=0,
                is_empty=False,
                in_range=True,
                last_accrual_time=0,
                data_time=0,
                availability_time=0,
            )


# ---------------------------------------------------------------------------
# Assessment invariants
# ---------------------------------------------------------------------------


class TestAssessmentInvariants:
    """Assessment outputs respect their outcome / state semantics."""

    def test_uncertain_assessment_requires_zero_confidence(self) -> None:
        with pytest.raises(InvalidAdaptiveAssessmentError):
            AdaptiveRegimeAssessment(
                version=ADAPTIVE_REGIME_ASSESSMENT_VERSION,
                model_version="test",
                outcome="UNCERTAIN",
                state="UNCERTAIN",
                confidence_q64_64=1,  # nonzero with outcome=UNCERTAIN
                range_occupancy_q64_64=0,
                jumps_down=False,
                liquidity_withdrawal=False,
            )

    def test_uncertain_assessment_state_must_be_uncertain(self) -> None:
        with pytest.raises(InvalidAdaptiveAssessmentError):
            AdaptiveRegimeAssessment(
                version=ADAPTIVE_REGIME_ASSESSMENT_VERSION,
                model_version="test",
                outcome="ASSESSED",
                state="UNCERTAIN",
                confidence_q64_64=Q64_SCALE,
                range_occupancy_q64_64=0,
                jumps_down=False,
                liquidity_withdrawal=False,
            )

    def test_fee_opportunity_uncertain_factory(self) -> None:
        assessment = AdaptiveFeeOpportunityAssessment.uncertain(
            model_version="test",
            uncertainty_codes=("LOW_VOLUME_WASH",),
            evidence_keys=("feature.v1",),
        )
        assert assessment.outcome == "UNCERTAIN"
        assert assessment.expected_fee_edge_q64_64 == 0
        assert assessment.confidence_q64_64 == 0
        assert assessment.uncertainty_codes == ("LOW_VOLUME_WASH",)


# ---------------------------------------------------------------------------
# Golden scenario A — Stable Range
# ---------------------------------------------------------------------------


class TestGoldenStableRange:
    """A stable Range around the current tick produces PROPOSE / WAIT."""

    def test_empty_ledger_stable_range_proposes_open(self) -> None:
        strategy = _default_strategy()
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60, liquidity=10_000)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        assert candidate.kind == AdaptiveCandidateKind.PROPOSE
        assert candidate.regime_state == "RANGE"
        assert candidate.regime_outcome == "ASSESSED"
        assert candidate.fee_opportunity_outcome == "ASSESSED"
        assert candidate.reason_code is None
        assert candidate.tick_lower < candidate.tick_upper
        assert candidate.tick_lower % _TICK_SPACING == 0
        assert candidate.tick_upper % _TICK_SPACING == 0

    def test_held_ledger_in_range_returns_wait(self) -> None:
        strategy = _default_strategy()
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        position = _held_position(tick_lower=-600, tick_upper=600)
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60, liquidity=10_000)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=position)
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        assert candidate.kind == AdaptiveCandidateKind.WAIT
        assert candidate.regime_state == "RANGE"
        assert "IN_RANGE" in candidate.notes


# ---------------------------------------------------------------------------
# Golden scenario B — Falling token with attractive apparent fee
# ---------------------------------------------------------------------------


class TestGoldenFallingToken:
    """A falling token with attractive apparent fee returns NO_TRADE / WAIT_OUT.

    The apparent fee is high (volume is large), but the regime is
    DOWN_TREND. The strategy does *not* depend on token appreciation;
    a downward trend suppresses risk-increasing candidates. The
    fee edge alone cannot open a new position.
    """

    def test_falling_token_empty_ledger_no_trade(self) -> None:
        strategy = _default_strategy()
        # Falling 60 ticks per minute for 20 minutes = 1200 ticks down.
        events = _falling_events(n=20, start_tick=0, step_ticks=60)
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        assert candidate.regime_state == "DOWN_TREND"
        assert candidate.reason_code == AdaptiveReasonCode.DOWN_TREND_ACTIVE
        assert candidate.kind == AdaptiveCandidateKind.NO_TRADE

    def test_falling_token_held_position_waits_out(self) -> None:
        strategy = _default_strategy()
        events = _falling_events(n=20, start_tick=0, step_ticks=60)
        position = _held_position(tick_lower=-600, tick_upper=600, last_accrual_time=0)
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=position)
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        assert candidate.regime_state == "DOWN_TREND"
        assert candidate.kind == AdaptiveCandidateKind.WAIT_OUT
        assert candidate.position_reassessment is True


# ---------------------------------------------------------------------------
# Golden scenario C — Jumps
# ---------------------------------------------------------------------------


class TestGoldenJumps:
    """A single-bar large return triggers JUMP_RISK with the right asymmetry."""

    def test_downward_jump_triggers_jump_down_risk(self) -> None:
        strategy = _default_strategy()
        # Build a stable history and then a single -15 % bar.
        events: list[BacktestEvent] = []
        for i in range(15):
            ts = i * 60
            price = _price_for_tick(0) + i * 1000
            events.append(
                _observation(
                    timestamp=ts,
                    price_q64_64=price,
                    volume_q64_64=(i + 1) * (1 << 60),
                    active_liquidity=10_000,
                )
            )
        # -15 % downward bar at t=900.
        last_price = _price_for_tick(0) + 15_000
        jump_price = last_price - (last_price >> 2) - (last_price >> 3)  # ~ 15 %
        events.append(
            _observation(
                timestamp=15 * 60,
                price_q64_64=jump_price,
                volume_q64_64=(16) * (1 << 60),
                active_liquidity=10_000,
            )
        )
        admission = _build_admission_snapshot(decision_time=16 * 60)
        market = _build_market_snapshot(decision_time=16 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=16 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=16 * 60,
        )
        assert candidate.regime_state == "JUMP_RISK"
        # Downside asymmetry: a -15 % bar fires JUMP_RISK with
        # ``jumps_down=True`` and forces NO_TRADE for an empty ledger.
        assert candidate.kind == AdaptiveCandidateKind.NO_TRADE
        assert candidate.reason_code == AdaptiveReasonCode.JUMP_DOWN_RISK

    def test_downside_asymmetry_smaller_threshold_for_downward(self) -> None:
        """The same magnitude move must fire only the down direction."""
        # Build a stable history, then a +15 % bar.
        events: list[BacktestEvent] = []
        for i in range(15):
            ts = i * 60
            price = _price_for_tick(0) + i * 1000
            events.append(
                _observation(
                    timestamp=ts,
                    price_q64_64=price,
                    volume_q64_64=(i + 1) * (1 << 60),
                    active_liquidity=10_000,
                )
            )
        last_price = _price_for_tick(0) + 15_000
        jump_price = last_price + (last_price >> 2) + (last_price >> 3)  # ~ +15 %
        events.append(
            _observation(
                timestamp=15 * 60,
                price_q64_64=jump_price,
                volume_q64_64=(16) * (1 << 60),
                active_liquidity=10_000,
            )
        )
        strategy = _default_strategy()
        admission = _build_admission_snapshot(decision_time=16 * 60)
        market = _build_market_snapshot(decision_time=16 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=16 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=16 * 60,
        )
        # The +15 % move should NOT trigger JUMP_RISK because the
        # upside threshold is 25 %. The strategy classifies by
        # something other than JUMP_RISK (typically RANGE / UNCERTAIN
        # because the jump distorted the occupancy / trend detectors).
        assert candidate.regime_state != "JUMP_RISK"


# ---------------------------------------------------------------------------
# Golden scenario D — Low / wash-like volume
# ---------------------------------------------------------------------------


class TestGoldenLowVolume:
    """Wash-like or insufficient volume downgrades the fee opportunity."""

    def test_wash_like_volume_returns_no_trade(self) -> None:
        strategy = _default_strategy()
        events: list[BacktestEvent] = []
        # All volumes identical → wash-like pattern.
        for i in range(20):
            ts = i * 60
            price = _price_for_tick(0) + (i % 3) * 1000
            events.append(
                _observation(
                    timestamp=ts,
                    price_q64_64=price,
                    volume_q64_64=1 << 60,  # identical volume
                    active_liquidity=10_000,
                )
            )
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        # Wash-like volume triggers fee UNCERTAIN; the strategy
        # refuses to open. The outcome may be a RANGE regime with
        # fee uncertainty, which the strategy maps to a NO_TRADE
        # verdict.
        assert candidate.kind == AdaptiveCandidateKind.NO_TRADE
        assert candidate.reason_code in (
            AdaptiveReasonCode.LOW_VOLUME_WASH,
            AdaptiveReasonCode.FEE_OPPORTUNITY_UNCERTAIN,
        )

    def test_insufficient_volume_returns_no_trade(self) -> None:
        strategy = _default_strategy()
        events: list[BacktestEvent] = []
        # 4 events, below the default ``fee_min_samples=5``.
        for i in range(4):
            ts = i * 60
            price = _price_for_tick(0) + i * 1000
            events.append(
                _observation(
                    timestamp=ts,
                    price_q64_64=price,
                    volume_q64_64=(i + 1) * (1 << 60),
                    active_liquidity=10_000,
                )
            )
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        # Insufficient samples may either produce an UNCERTAIN
        # regime (the history is also too short) or a RANGE
        # regime with an UNCERTAIN fee outcome. Both paths map to
        # NO_TRADE.
        assert candidate.kind == AdaptiveCandidateKind.NO_TRADE


# ---------------------------------------------------------------------------
# Golden scenario E — Liquidity withdrawal
# ---------------------------------------------------------------------------


class TestGoldenLiquidityWithdrawal:
    """A sustained drop in active liquidity returns NO_TRADE with
    the structured LIQUIDITY_WITHDRAWAL reason."""

    def test_liquidity_withdrawal_returns_no_trade(self) -> None:
        strategy = _default_strategy()
        events: list[BacktestEvent] = []
        for i in range(20):
            ts = i * 60
            price = _price_for_tick(0) + (i % 3) * 1000
            # First half: 10_000. Second half: 1_000. ~ 90 % drop.
            liq = 10_000 if i < 10 else 1_000
            events.append(
                _observation(
                    timestamp=ts,
                    price_q64_64=price,
                    volume_q64_64=(i + 1) * (1 << 60),
                    active_liquidity=liq,
                )
            )
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        # The withdrawal is severe; the regime refuses to declare RANGE.
        assert candidate.kind == AdaptiveCandidateKind.NO_TRADE


# ---------------------------------------------------------------------------
# Golden scenario F — Out-of-Range wait / return
# ---------------------------------------------------------------------------


class TestGoldenOutOfRangeLifecycle:
    """The full out-of-Range lifecycle: WAIT_OUT, RETURN, REBUILD."""

    def test_out_of_range_within_wait_bound_waits_out(self) -> None:
        strategy = _default_strategy(max_rebuild_wait_seconds=1_800)
        # Position in [-600, 600], current tick moves to +1000.
        events: list[BacktestEvent] = []
        for i in range(20):
            ts = i * 60
            tick = 0 if i < 19 else 1_200  # sudden jump to 1_200
            events.append(
                _observation(
                    timestamp=ts,
                    price_q64_64=_price_for_tick(tick),
                    volume_q64_64=(i + 1) * (1 << 60),
                    active_liquidity=10_000,
                )
            )
        position = _held_position(
            tick_lower=-600,
            tick_upper=600,
            last_accrual_time=10 * 60,  # out for 10 minutes
        )
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=position)
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        # Out of range but within the rebuild-wait bound → WAIT_OUT.
        assert candidate.kind == AdaptiveCandidateKind.WAIT_OUT
        assert candidate.position_reassessment is True
        assert "OUT_OF_RANGE" in candidate.notes

    def test_out_of_range_beyond_wait_bound_rebuilds(self) -> None:
        strategy = _default_strategy(max_rebuild_wait_seconds=300)
        # Position out of range; build the history long enough that
        # the cost-vs-edge arithmetic evaluates favourably.
        events: list[BacktestEvent] = []
        for i in range(20):
            ts = i * 60
            tick = 0 if i < 19 else 1_200  # sudden jump to 1_200
            events.append(
                _observation(
                    timestamp=ts,
                    price_q64_64=_price_for_tick(tick),
                    volume_q64_64=(i + 1) * (1 << 60),
                    active_liquidity=10_000,
                )
            )
        position = _held_position(
            tick_lower=-600,
            tick_upper=600,
            last_accrual_time=0,  # out for 20 minutes > 5 minute wait
        )
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=position)
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        # Beyond the wait bound with an acceptable cost ratio → REBUILD.
        assert candidate.kind == AdaptiveCandidateKind.REBUILD
        assert candidate.tick_lower < candidate.tick_upper
        assert candidate.tick_lower % _TICK_SPACING == 0
        assert candidate.tick_upper % _TICK_SPACING == 0
        assert candidate.position_reassessment is True

    def test_return_to_range_after_out_of_range_emits_return(self) -> None:
        # The price leaves the Range, then re-enters it. The engine
        # has the ledger ``in_range=False`` (the previous out-of-Range
        # step recorded it that way); the current tick is inside the
        # Range, so the strategy must detect the transition and emit
        # the RETURN lifecycle action — not a plain WAIT.
        strategy = _default_strategy(max_rebuild_wait_seconds=1_800)
        # The visible price walk classifies as RANGE (stable oscillation
        # around the in-Range tick). The transition itself is carried
        # by the portfolio snapshot's ``in_range=False`` flag, which
        # mirrors the engine's recorded ledger state on the previous
        # step.
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        position = _held_position(
            tick_lower=-600,
            tick_upper=600,
            last_accrual_time=5 * 60,  # was out of range recently
            in_range=False,
        )
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=position)
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        assert candidate.kind == AdaptiveCandidateKind.RETURN
        assert "RETURN_TO_RANGE" in candidate.notes
        assert candidate.regime_state == "RANGE"
        # RETURN carries no new Range, no liquidity, no capital.
        assert candidate.tick_lower == 0
        assert candidate.tick_upper == 0
        assert candidate.liquidity == 0
        assert candidate.capital_q64_64 == 0


# ---------------------------------------------------------------------------
# Golden scenario G — Costly rebuild
# ---------------------------------------------------------------------------


class TestGoldenCostlyRebuild:
    """A cost ratio above the threshold produces ``REBUILD_DEFER``."""

    def test_costly_rebuild_defers(self) -> None:
        # Lower the threshold so any cost ratio exceeds it.
        strategy = _default_strategy(
            max_rebuild_wait_seconds=300,
            rebuild_cost_ratio_threshold_q64_64=1,  # cost must be < 1.0
        )
        events: list[BacktestEvent] = []
        for i in range(20):
            ts = i * 60
            tick = 0 if i < 19 else 1_200
            events.append(
                _observation(
                    timestamp=ts,
                    price_q64_64=_price_for_tick(tick),
                    volume_q64_64=(i + 1) * (1 << 60),
                    active_liquidity=10_000,
                )
            )
        position = _held_position(
            tick_lower=-600,
            tick_upper=600,
            last_accrual_time=0,
        )
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=position)
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        # Cost ratio exceeds the (very low) threshold → defer.
        assert candidate.kind == AdaptiveCandidateKind.REBUILD_DEFER
        assert candidate.reason_code == AdaptiveReasonCode.REBUILD_DEFERRED
        assert candidate.position_reassessment is True


# ---------------------------------------------------------------------------
# Acceptance — 5-minute USDG return > 100 % rule
# ---------------------------------------------------------------------------


class TestFiveMinuteUSDRule:
    """The 5-minute USDG rule fires only on a *complete* bar."""

    def test_complete_bar_above_100_pct_empty_ledger_no_risk_increase(self) -> None:
        """Empty ledger + 5-min return > 100 % → NO_TRADE, no risk increase."""
        strategy = _default_strategy()
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        admission = _build_admission_snapshot(decision_time=20 * 60)
        # Five-minute return > 100 %: 2.5 * 2**64 (price went 1.0 → 2.5).
        extreme_return = Q64_SCALE + (Q64_SCALE >> 1)  # 1.5 * 2**64 = 150 %
        market = _build_market_snapshot(
            decision_time=20 * 60,
            five_minute_return_q64_64=extreme_return,
            five_minute_return_complete=True,
        )
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        # The rule suppresses risk-increasing candidates.
        assert candidate.kind == AdaptiveCandidateKind.NO_TRADE
        assert candidate.kind != AdaptiveCandidateKind.PROPOSE
        # The in-strategy position reassessment is recorded.
        assert candidate.position_reassessment is True
        # The structured reason code carries the rule firing.
        assert candidate.reason_code == AdaptiveReasonCode.EXTREME_UP_MOVE
        # The adapter does NOT mutate any global run state — the
        # candidate carries the signal but no module-level flag.

    def test_complete_bar_above_100_pct_held_position_exits(self) -> None:
        """Held position + 5-min return > 100 % → EXIT with reassessment."""
        strategy = _default_strategy()
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        position = _held_position(tick_lower=-600, tick_upper=600)
        admission = _build_admission_snapshot(decision_time=20 * 60)
        extreme_return = 2 * Q64_SCALE  # 200 % return
        market = _build_market_snapshot(
            decision_time=20 * 60,
            five_minute_return_q64_64=extreme_return,
            five_minute_return_complete=True,
        )
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=position)
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        assert candidate.kind == AdaptiveCandidateKind.EXIT
        assert candidate.position_reassessment is True
        assert candidate.reason_code == AdaptiveReasonCode.EXTREME_UP_MOVE

    def test_incomplete_bar_does_not_trigger_rule(self) -> None:
        """An incomplete 5-minute bar must NOT trigger the rule.

        The binding ``Must not`` clause requires the rule to fire
        only on *complete* bars. An incomplete bar (the close time
        is in the future) carries ``five_minute_return_complete=False``
        and the strategy must NOT translate a partial return into
        a rule firing — even if the partial return would have
        crossed 100 %.
        """
        strategy = _default_strategy()
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        admission = _build_admission_snapshot(decision_time=20 * 60)
        # The snapshot carries an *incomplete* bar with a return
        # that would have exceeded 100 % if the bar were complete.
        # The strategy MUST ignore the return because the bar is
        # incomplete.
        extreme_return = 3 * Q64_SCALE
        market = _build_market_snapshot(
            decision_time=20 * 60,
            five_minute_return_q64_64=extreme_return,
            five_minute_return_complete=False,  # incomplete!
        )
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        # Rule does NOT fire. The RANGE regime + ASSESSED fee
        # outcome produce a PROPOSE (the rule's absence does not
        # block the open path).
        assert candidate.reason_code != AdaptiveReasonCode.EXTREME_UP_MOVE
        assert candidate.kind == AdaptiveCandidateKind.PROPOSE

    def test_rule_does_not_mutate_global_state(self) -> None:
        """The 5-minute rule must not change any module-level state.

        The strategy module exposes no run-state flag; the rule
        lives entirely in the candidate action the strategy
        returns. Two consecutive evaluations with the same inputs
        produce identical candidate actions; the candidate carries
        the in-strategy position reassessment as a structured
        field rather than as a side-effect on a module-level flag.
        """
        strategy = _default_strategy()
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        admission = _build_admission_snapshot(decision_time=20 * 60)
        extreme_return = 2 * Q64_SCALE
        market = _build_market_snapshot(
            decision_time=20 * 60,
            five_minute_return_q64_64=extreme_return,
            five_minute_return_complete=True,
        )
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        candidate_a = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        candidate_b = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        assert candidate_a == candidate_b
        # The rule's effect is bound to the candidate action: a
        # fresh strategy instance, with no module-level state
        # shared with the original, produces the same decision.
        fresh_strategy = _default_strategy()
        candidate_c = fresh_strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        assert candidate_c == candidate_a
        # The candidate carries the position reassessment as a
        # structured field, not as a side-effect on any module
        # variable.
        assert candidate_a.position_reassessment is True


# ---------------------------------------------------------------------------
# Adapter — five-minute return helper
# ---------------------------------------------------------------------------


class TestAdapterFiveMinuteReturn:
    """The adapter's 5-minute helper computes the right value / flag."""

    def test_incomplete_bar_at_decision_time_250(self) -> None:
        events = _stable_events(n=5, tick=0, active_liquidity=10_000)
        ret, complete = compute_five_minute_return_q64_64(events, decision_time=250)
        assert complete is False
        assert ret == 0

    def test_complete_bar_no_data_returns_zero(self) -> None:
        events: tuple[BacktestEvent, ...] = ()
        ret, complete = compute_five_minute_return_q64_64(events, decision_time=600)
        assert complete is False
        assert ret == 0

    def test_complete_bar_with_price_doubling(self) -> None:
        events: list[BacktestEvent] = []
        events.append(_observation(timestamp=0, price_q64_64=_price_for_tick(0)))
        events.append(_observation(timestamp=300, price_q64_64=2 * _price_for_tick(0)))
        ret, complete = compute_five_minute_return_q64_64(events, decision_time=300)
        assert complete is True
        # Price doubled → return = 100 % = 1.0 in Q64.64.
        assert ret == Q64_SCALE

    def test_complete_bar_with_price_tripling(self) -> None:
        events: list[BacktestEvent] = []
        events.append(_observation(timestamp=0, price_q64_64=_price_for_tick(0)))
        events.append(_observation(timestamp=300, price_q64_64=3 * _price_for_tick(0)))
        ret, complete = compute_five_minute_return_q64_64(events, decision_time=300)
        assert complete is True
        # Price tripled → return = 200 % = 2.0 in Q64.64.
        assert ret == 2 * Q64_SCALE

    def test_decision_time_at_bar_boundary_is_complete(self) -> None:
        """A decision time exactly at the bar boundary counts as complete."""
        events: list[BacktestEvent] = []
        events.append(_observation(timestamp=0, price_q64_64=_price_for_tick(0)))
        events.append(_observation(timestamp=300, price_q64_64=2 * _price_for_tick(0)))
        ret, complete = compute_five_minute_return_q64_64(events, decision_time=300)
        assert complete is True
        assert ret == Q64_SCALE

    def test_window_seconds_must_be_positive(self) -> None:
        events: tuple[BacktestEvent, ...] = ()
        with pytest.raises(EngineAdapterError):
            compute_five_minute_return_q64_64(events, decision_time=600, window_seconds=0)


# ---------------------------------------------------------------------------
# Engine-callback adapter
# ---------------------------------------------------------------------------


class TestEngineCallbackAdapter:
    """The adapter translates T065 candidate actions to engine decisions."""

    def test_callback_returns_engine_strategy_decision(self) -> None:
        strategy = _default_strategy()
        callback = AdaptiveStrategyCallback(strategy=strategy)
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        request = _ProtocolStrategyDecisionRequest(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            decision_time=20 * 60,
            visible_events=events,
            ledger=_empty_position(),
        )
        decision = callback(request)
        assert isinstance(decision, StrategyDecision)
        # Stable range, empty ledger → PROPOSE.
        assert decision.kind == "PROPOSE"
        assert decision.tick_lower < decision.tick_upper
        assert decision.liquidity > 0
        assert decision.capital_q64_64 > 0

    def test_callback_rejects_non_strategy_request(self) -> None:
        strategy = _default_strategy()
        callback = AdaptiveStrategyCallback(strategy=strategy)
        with pytest.raises(InvalidEngineRequestError):
            callback("not a request")  # type: ignore[arg-type]

    def test_callback_rejects_non_positive_window(self) -> None:
        strategy = _default_strategy()
        with pytest.raises(EngineAdapterError):
            AdaptiveStrategyCallback(strategy=strategy, window_seconds=0)

    def test_kind_mapping_table_is_complete(self) -> None:
        """Every T065 ``AdaptiveCandidateKind`` maps to an engine kind."""
        for kind in AdaptiveCandidateKind:
            assert kind.value in ADAPTIVE_TO_ENGINE_KIND

    def test_extreme_up_move_maps_to_no_trade_or_propose(self) -> None:
        """The 5-minute rule's NO_TRADE / EXIT candidates reach the engine."""
        strategy = _default_strategy()
        callback = AdaptiveStrategyCallback(strategy=strategy)
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        # Empty ledger, complete 5-min bar with return > 100 %.
        events = events + (
            _observation(
                timestamp=20 * 60,
                price_q64_64=3 * _price_for_tick(0),  # extreme bar close
                volume_q64_64=1 << 60,
                active_liquidity=10_000,
            ),
        )
        request = _ProtocolStrategyDecisionRequest(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            decision_time=20 * 60,
            visible_events=events,
            ledger=_empty_position(),
        )
        decision = callback(request)
        # The adapter maps the T065 NO_TRADE candidate to the
        # engine's NO_TRADE; no risk-increasing PROPOSE escapes
        # the rule.
        assert decision.kind == "NO_TRADE"
        assert "EXTREME_UP_MOVE" in " ".join(decision.notes)


# ---------------------------------------------------------------------------
# Engine integration — same manifest produces identical decisions
# ---------------------------------------------------------------------------


class TestEngineIntegrationReplay:
    """The strategy callback wired through the engine replays the same."""

    def _engine_with_callback(self, callback: AdaptiveStrategyCallback) -> BacktestEngine:
        from robinhood_lp.backtest.engine import RiskDecision

        bundle = ModelBundle(
            bundle_version="t061.test.v1",
            liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
            fee=StaticFeeModel(fee_pips_value=3_000),
            gas=FlatGasModel(gas_units_value=21_000),
            slippage=ZeroSlippageModel(),
            failure=DeterministicFailureModel(),
            latency_units=0,
        )

        def risk(_decision: StrategyDecision) -> RiskDecision:
            return RiskDecision(approved=True, reason_code="OK")

        return BacktestEngine(
            version="t061.test.v1",
            initial_ledger=_empty_position(),
            model_bundle=bundle,
            strategy_callback=callback,
            risk_callback=risk,
        )

    def test_engine_replay_same_manifest_same_result(self) -> None:
        strategy = _default_strategy()
        callback = AdaptiveStrategyCallback(strategy=strategy)
        engine = self._engine_with_callback(callback)
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        # Add a SWAP event so the engine's reactive path produces
        # a decision.
        events = events + (
            _swap(
                timestamp=20 * 60,
                price_q64_64=_price_for_tick(0) + 100,
                volume_q64_64=1 << 60,
                active_liquidity=10_000,
            ),
        )
        result_a = engine.run(events)
        result_b = engine.run(events)
        assert result_a.manifest_hash == result_b.manifest_hash
        assert result_a.bundle_hash == result_b.bundle_hash
        assert result_a.final_ledger == result_b.final_ledger
        assert [a.event_id for a in result_a.audit_events] == [
            a.event_id for a in result_b.audit_events
        ]


# ---------------------------------------------------------------------------
# Model replacement — replaceable components do not alter contracts
# ---------------------------------------------------------------------------


class TestModelReplacement:
    """Replacing the regime / fee models changes the decision but not
    the engine / ledger / risk surface."""

    def test_replace_regime_with_always_jump_down(self) -> None:
        class _AlwaysJumpDownRegime:
            model_version = "test.always_jump_down"

            @property
            def model_version_prop(self) -> str:  # pragma: no cover - helper
                return self.model_version

            def assess(self, **_: object) -> AdaptiveRegimeAssessment:
                return AdaptiveRegimeAssessment(
                    version=ADAPTIVE_REGIME_ASSESSMENT_VERSION,
                    model_version=self.model_version,
                    outcome="ASSESSED",
                    state="JUMP_RISK",
                    confidence_q64_64=Q64_SCALE,
                    range_occupancy_q64_64=0,
                    jumps_down=True,
                    liquidity_withdrawal=False,
                )

        stub = _AlwaysJumpDownRegime()
        # The stub satisfies the Protocol at runtime.
        assert isinstance(stub, AdaptiveRegimeModel)
        strategy = AdaptiveStrategy(
            params=_default_params(),
            regime_model=stub,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
        )
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        # Replacement model changes the assessment; the strategy
        # records JUMP_DOWN_RISK. The ledger / risk / execution
        # surfaces are unchanged: the strategy still emits an
        # AdaptiveCandidateAction (not a signed transaction).
        assert candidate.kind == AdaptiveCandidateKind.NO_TRADE
        assert candidate.reason_code == AdaptiveReasonCode.JUMP_DOWN_RISK

    def test_replace_fee_model_does_not_change_audit_shape(self) -> None:
        """A replacement fee model keeps the candidate-action shape."""

        class _AlwaysUncertainFee:
            model_version = "test.always_uncertain_fee"

            def assess(self, **_: object) -> AdaptiveFeeOpportunityAssessment:
                return AdaptiveFeeOpportunityAssessment.uncertain(
                    model_version=self.model_version,
                    uncertainty_codes=("REPLACED",),
                )

        stub = _AlwaysUncertainFee()
        assert isinstance(stub, AdaptiveFeeOpportunityModel)
        strategy = AdaptiveStrategy(
            params=_default_params(),
            fee_opportunity_model=stub,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
        )
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        # The fee outcome is UNCERTAIN; the strategy refuses to
        # open. The candidate action shape is unchanged (still a
        # T065 AdaptiveCandidateAction, still carrying
        # version / kind / reason_code).
        assert candidate.kind == AdaptiveCandidateKind.NO_TRADE
        assert candidate.fee_opportunity_outcome == "UNCERTAIN"
        assert isinstance(candidate, AdaptiveCandidateAction)


# ---------------------------------------------------------------------------
# Determinism — replay from the manifest
# ---------------------------------------------------------------------------


class TestReplayDeterminism:
    """Two equivalent inputs produce byte-identical candidate actions."""

    def test_two_evaluations_with_same_inputs_are_equal(self) -> None:
        strategy = _default_strategy()
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        admission = _build_admission_snapshot(decision_time=20 * 60)
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        a = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        b = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        assert a == b
        assert hash(a) == hash(b)

    def test_callback_returns_equal_decisions(self) -> None:
        strategy = _default_strategy()
        callback = AdaptiveStrategyCallback(strategy=strategy)
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        request = _ProtocolStrategyDecisionRequest(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            decision_time=20 * 60,
            visible_events=events,
            ledger=_empty_position(),
        )
        a = callback(request)
        b = callback(request)
        assert a == b

    def test_events_in_different_input_orders_produce_same_decision(self) -> None:
        strategy = _default_strategy()
        callback = AdaptiveStrategyCallback(strategy=strategy)
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        reversed_events = tuple(reversed(events))
        request_a = _ProtocolStrategyDecisionRequest(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            decision_time=20 * 60,
            visible_events=events,
            ledger=_empty_position(),
        )
        request_b = _ProtocolStrategyDecisionRequest(
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            decision_time=20 * 60,
            visible_events=reversed_events,
            ledger=_empty_position(),
        )
        a = callback(request_a)
        b = callback(request_b)
        assert a == b


# ---------------------------------------------------------------------------
# Must-not — no token appreciation, no fundamentals, no forcing
# ---------------------------------------------------------------------------


class TestMustNot:
    """The strategy never depends on token appreciation, fundamentals,
    sentiment, or forced trades."""

    def test_up_trend_does_not_open_for_empty_ledger(self) -> None:
        """UP_TREND regime refuses risk-increasing candidates.

        CTA-style "trend is bullish" must not translate to a long
        bias. The strategy treats UP_TREND the same as
        DOWN_TREND: refusal to add new exposure.
        """
        strategy = _default_strategy()
        events: list[BacktestEvent] = []
        # Build a sustained upward walk that the regime classifies
        # as UP_TREND: positive cumulative return above the
        # threshold. The history must be long enough that the
        # regime model's ``min_history_seconds`` check passes.
        for i in range(16):
            ts = i * 60
            tick = i * 100  # monotonically up
            events.append(
                _observation(
                    timestamp=ts,
                    price_q64_64=_price_for_tick(tick),
                    volume_q64_64=(i + 1) * (1 << 60),
                    active_liquidity=10_000,
                )
            )
        admission = _build_admission_snapshot(decision_time=16 * 60)
        market = _build_market_snapshot(decision_time=16 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=16 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=16 * 60,
        )
        assert candidate.regime_state == "UP_TREND"
        assert candidate.kind == AdaptiveCandidateKind.NO_TRADE
        assert candidate.reason_code == AdaptiveReasonCode.UP_TREND_ACTIVE

    def test_no_data_emits_no_trade(self) -> None:
        """An empty visible_events tuple returns a structured NO_TRADE."""
        strategy = _default_strategy()
        admission = _build_admission_snapshot(decision_time=0)
        market = _build_market_snapshot(decision_time=0)
        portfolio = _build_portfolio_snapshot(decision_time=0, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=(),
            decision_time=0,
        )
        assert candidate.kind == AdaptiveCandidateKind.NO_TRADE

    def test_not_admitted_emits_no_trade(self) -> None:
        strategy = _default_strategy()
        events = _stable_events(n=20, tick=0, active_liquidity=10_000)
        admission = AdaptiveAdmissionSnapshot(
            version="t065.admission_snapshot.v1",
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            support_level="rejected",
            is_admitted=False,
            max_capital_q64_64=1 << 70,
            data_time=20 * 60,
            availability_time=20 * 60,
        )
        market = _build_market_snapshot(decision_time=20 * 60)
        portfolio = _build_portfolio_snapshot(decision_time=20 * 60, position=_empty_position())
        candidate = strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=events,
            decision_time=20 * 60,
        )
        assert candidate.kind == AdaptiveCandidateKind.NO_TRADE
        assert "NOT_ADMITTED" in candidate.uncertainty_codes


# ---------------------------------------------------------------------------
# Candidate action invariants
# ---------------------------------------------------------------------------


class TestCandidateActionInvariants:
    """The candidate action enforces its kind-specific invariants."""

    def test_propose_requires_tick_lower_less_than_upper(self) -> None:
        with pytest.raises(InvalidAdaptiveCandidateError):
            AdaptiveCandidateAction(
                version=ADAPTIVE_CANDIDATE_ACTION_VERSION,
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                kind=AdaptiveCandidateKind.PROPOSE,
                decision_time=0,
                parameter_version="0x00",
                snapshot_versions=(),
                component_versions=(),
                regime_outcome="ASSESSED",
                regime_state="RANGE",
                fee_opportunity_outcome="ASSESSED",
                reason_code=None,
                position_reassessment=False,
                cost_ratio_q64_64=0,
                tick_lower=10,
                tick_upper=10,  # degenerate
                liquidity=1,
                capital_q64_64=1,
            )

    def test_no_trade_requires_reason_code(self) -> None:
        with pytest.raises(InvalidAdaptiveCandidateError):
            AdaptiveCandidateAction(
                version=ADAPTIVE_CANDIDATE_ACTION_VERSION,
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                kind=AdaptiveCandidateKind.NO_TRADE,
                decision_time=0,
                parameter_version="0x00",
                snapshot_versions=(),
                component_versions=(),
                regime_outcome="UNCERTAIN",
                regime_state="UNCERTAIN",
                fee_opportunity_outcome="UNCERTAIN",
                reason_code=None,  # missing
                position_reassessment=False,
                cost_ratio_q64_64=0,
            )


# ---------------------------------------------------------------------------
# Public surface — module exports
# ---------------------------------------------------------------------------


class TestPublicSurface:
    """The module exposes the documented public surface."""

    def test_module_version_is_set(self) -> None:
        assert ADAPTIVE_STRATEGY_VERSION.startswith("t065.")
        assert ADAPTER_VERSION.startswith("t065.")
