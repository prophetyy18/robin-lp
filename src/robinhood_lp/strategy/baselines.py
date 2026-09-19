"""V1 baseline LP strategies (T062).

This module defines five auditable, pool-agnostic baseline policies the
research / comparison pipeline uses to anchor every later strategy. Each
baseline is a *pure* policy: it consumes the per-decision state the
event-driven backtest engine (T061) hands it and returns a
:class:`StrategyDecision`. No baseline imports RPC, storage,
configuration, signing, execution or presentation code, and no
baseline depends on a specific token symbol, address or numerical
threshold the project has not yet committed to in a Spec document.

Five baselines ship:

- :class:`HoldStrategy` — the zero-action benchmark. Returns
  ``NO_TRADE`` for every decision. Useful as the *HODL* baseline for
  the cash-benchmark comparison in
  ``docs/spec/strategy/STRATEGY_ECONOMICS.md`` §2 (`ECO-OBJ-002`).

- :class:`BroadRangeStrategy` — opens a single position at the widest
  protocol-valid Range the pool's ``tick_spacing`` admits and never
  rebalances. The Range is **not** literally the V4 ``int24`` domain
  ``[-2**23, 2**23 - 1]``; it is the closest aligned Range inside
  the V4 ``[MIN_TICK, MAX_TICK]`` integer-tick domain, which is the
  widest the pool will actually accept on chain (the integer ticks
  ``[-887272, 887272]`` per
  ``docs/spec/protocol/PROTOCOL_FACTS.md``). "Broad range" therefore
  is a *protocol-valid* approximation of full-range — it respects
  the tick-spacing requirement and the per-pool usable-tick
  boundary rather than claiming a literal infinite / full ``int24``
  Range that the pool would reject.

- :class:`FixedWidthStrategy` — opens a symmetric Range
  ``±half_width_ticks`` around the current tick (snapped to
  ``tick_spacing``). If the position leaves the Range, the strategy
  rebalances around the new current tick.

- :class:`VolatilityWidthStrategy` — opens a symmetric Range whose
  width tracks realised volatility. The half-width in ticks is
  ``clamp(round(k * sigma_ticks), min_half_width_ticks,
  max_half_width_ticks)`` where ``sigma_ticks`` is a log-return-based
  estimate derived from the visible price events the engine hands the
  strategy. The strategy rebalances when the position leaves the
  Range.

- :class:`OutOfRangeRebalanceStrategy` — opens a symmetric
  ``±half_width_ticks`` Range around the current tick and *only*
  rebalances when the current tick leaves the position. While in
  range the strategy returns ``WAIT``; only an out-of-range event
  triggers a new ``PROPOSE``.

Every baseline documents its parameters and their defaults. Every
baseline is pool-agnostic: it never branches on a token address,
symbol, decimal width, or hook implementation. Every baseline snaps
its proposal ticks to ``tick_spacing`` and clamps them to the V4
``[MIN_TICK, MAX_TICK]`` domain; ticks it cannot represent produce
a structured ``NO_TRADE`` verdict instead of an out-of-range tick
the pool would reject.

Design constraints (binding):

- **Pure observation -> action.** Each ``__call__`` returns a fresh
  :class:`StrategyDecision`. The baseline never mutates the
  :class:`StrategyDecisionRequest` it receives and never reads
  wall-clock time, unseeded randomness, RPC state, or storage.

- **Integer accounting.** Per ADR-004, no ``float`` appears on the
  protocol / valuation path. Price / tick math is integer-only:
  integer ``isqrt`` maps Q64.64 price to Q64.96 sqrt-price, and the
  V4 ``get_tick_at_sqrt_price`` reference implementation returns the
  tick.

- **No addresses / symbols / token-specific thresholds.** The module
  imports no token metadata. The default half-width is a tick count,
  not a USDG amount; the volatility multiplier is dimensionless.

- **Documented defaults.** Every parameter has a documented default
  in the class docstring. Defaults are *not* tuned on the
  evaluation period (T062 Must-not).

References:

- R8 — Uniswap V4 SDK and the position-minting / range math that
  informs the broad-range and fixed-width policies.
- R15 — Uniswap V3 whitepaper for concentrated-liquidity math and
  the trade-off between range width, fee density and divergence
  loss.
- R20 — Aloe V3 simulator reference for the audit-by-baseline
  methodology (the baselines here are independent implementations,
  not a port).
- ADR-014 §5 — pool-agnostic strategy policy; no token-specific
  branching.
- T061 — event-driven backtest engine that calls these baselines.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from robinhood_lp.backtest.engine import (
    StrategyDecision,
    StrategyDecisionRequest,
)
from robinhood_lp.backtest.events import (
    KIND_OBSERVATION,
    KIND_SWAP,
    SOURCE_PRIORITY_DATA,
    BacktestEvent,
)
from robinhood_lp.protocol.math import (
    MAX_TICK,
    MAX_TICK_SPACING,
    MIN_TICK,
    MIN_TICK_SPACING,
    get_tick_at_sqrt_price,
    max_usable_tick,
    min_usable_tick,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Module version. Bumping it is a breaking change for downstream
#: consumers (the comparison harness, the audit layer).
BASELINE_STRATEGY_VERSION: Final[str] = "t062.baseline_strategy.v1"

#: Default half-width for :class:`FixedWidthStrategy`, expressed in
#: raw ticks. ``600`` ticks correspond to roughly ±6% on each side
#: (``1.0001^600 - 1 ≈ 0.0589``). The default is **not** optimised on
#: the evaluation period (T062 Must-not); it is a round, auditable
#: number that the comparison harness can swap out per experiment.
DEFAULT_FIXED_HALF_WIDTH_TICKS: Final[int] = 600

#: Default volatility multiplier (``k``) for
#: :class:`VolatilityWidthStrategy`. The half-width in ticks is
#: ``round(k * sigma_ticks)`` clamped between the min and max
#: half-width bounds. The default ``k = 2`` matches the canonical
#: "two-sigma" framing common in option-pricing references (R15 §3)
#: and is dimensionally dimensionless.
DEFAULT_VOLATILITY_MULTIPLIER: Final[int] = 2

#: Minimum half-width (in ticks) the volatility strategy will use
#: even when the realised volatility estimate is zero. Prevents a
#: degenerate zero-width Range.
DEFAULT_MIN_HALF_WIDTH_TICKS: Final[int] = 60

#: Maximum half-width (in ticks) the volatility strategy will use
#: when the realised volatility estimate is large. Keeps the
#: volatility strategy from collapsing to the broad-range policy.
DEFAULT_MAX_HALF_WIDTH_TICKS: Final[int] = 5_000

#: Default number of most-recent price events the volatility strategy
#: uses to estimate realised volatility. ``20`` is small enough to be
#: deterministic for a backtest run and large enough to give a
#: stable per-decision sigma estimate.
DEFAULT_VOLATILITY_WINDOW: Final[int] = 20

#: Default half-width for :class:`OutOfRangeRebalanceStrategy`. Same
#: default as :class:`FixedWidthStrategy`; the contract requires the
#: rebalance-only policy to be auditable independently of the
#: fixed-width policy, so the parameter is duplicated explicitly.
DEFAULT_REBALANCE_HALF_WIDTH_TICKS: Final[int] = 600

#: Default liquidity magnitude used when a baseline proposes a
#: position. ``1_000`` is a small positive integer that lets the
#: backtest engine distinguish a real mint from a zero-liquidity
#: ``WAIT``. The comparison harness overrides this per experiment.
DEFAULT_BASELINE_LIQUIDITY: Final[int] = 1_000

#: Default capital envelope, expressed as Q64.64 USDG. ``1 << 64``
#: is exactly one USDG. The comparison harness overrides this per
#: experiment; the baseline never optimises on the evaluation period.
DEFAULT_BASELINE_CAPITAL_Q64_64: Final[int] = 1 << 64

#: Default chain-id field used when the strategy is constructed
#: outside an engine run. ``0`` is rejected by the engine; it is
#: only present so the dataclass satisfies its invariant. Real
#: callers pass the chain id from the PoolKey.
DEFAULT_BASELINE_CHAIN_ID: Final[int] = 1

#: Closed vocabulary of ``notes`` tags the baselines attach to every
#: decision. Strings are part of the public contract; new tags are
#: additive. Each tag identifies the policy that produced the
#: decision in the audit chain.
NOTES_HOLD: Final[str] = "BASELINE_HOLD"
NOTES_BROAD_RANGE_OPEN: Final[str] = "BASELINE_BROAD_RANGE_OPEN"
NOTES_BROAD_RANGE_HOLD: Final[str] = "BASELINE_BROAD_RANGE_HOLD"
NOTES_FIXED_WIDTH_OPEN: Final[str] = "BASELINE_FIXED_WIDTH_OPEN"
NOTES_FIXED_WIDTH_HOLD: Final[str] = "BASELINE_FIXED_WIDTH_HOLD"
NOTES_FIXED_WIDTH_REBALANCE: Final[str] = "BASELINE_FIXED_WIDTH_REBALANCE"
NOTES_VOLATILITY_OPEN: Final[str] = "BASELINE_VOLATILITY_OPEN"
NOTES_VOLATILITY_HOLD: Final[str] = "BASELINE_VOLATILITY_HOLD"
NOTES_VOLATILITY_REBALANCE: Final[str] = "BASELINE_VOLATILITY_REBALANCE"
NOTES_REBALANCE_OPEN: Final[str] = "BASELINE_REBALANCE_OPEN"
NOTES_REBALANCE_HOLD: Final[str] = "BASELINE_REBALANCE_HOLD"
NOTES_REBALANCE_REBALANCE: Final[str] = "BASELINE_REBALANCE_REBALANCE"
NOTES_NO_MARKET_DATA: Final[str] = "BASELINE_NO_MARKET_DATA"
NOTES_INSUFFICIENT_CAPITAL: Final[str] = "BASELINE_INSUFFICIENT_CAPITAL"
NOTES_TICKS_OUT_OF_BOUNDS: Final[str] = "BASELINE_TICKS_OUT_OF_BOUNDS"
NOTES_HOLD_INVALID_TICK: Final[str] = "BASELINE_HOLD_INVALID_TICK"

#: Q64.64 fixed-point scale (matches :data:`Q64_SCALE` in
#: :mod:`robinhood_lp.strategy.base`).
_Q64_SCALE: Final[int] = 1 << 64

#: Q64.96 fixed-point scale used by V4 sqrt-price. The baseline
#: converter multiplies a Q64.64 price ratio by ``2 ** 32`` to move
#: from Q64.64 to Q64.96 once the integer square root is taken.
_Q96_SHIFT: Final[int] = 32


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BaselineStrategyError(ValueError):
    """Base class for baseline-strategy construction failures."""


class InvalidBaselineTickError(BaselineStrategyError):
    """A baseline parameter carries a tick that violates V4 constraints."""


class InvalidBaselineCapitalError(BaselineStrategyError):
    """A baseline capital parameter is non-positive."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a Python ``int`` (``bool`` rejected)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise BaselineStrategyError(f"{field}: must be int, got {type(value).__name__}")
    return value


def _require_positive_int(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value <= 0:
        raise BaselineStrategyError(f"{field}: must be positive, got {value}")
    return value


def _require_non_negative_int(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value < 0:
        raise BaselineStrategyError(f"{field}: must be non-negative, got {value}")
    return value


def _require_tick_spacing(tick_spacing: int, *, field: str) -> int:
    value = _require_positive_int(tick_spacing, field=field)
    if tick_spacing < MIN_TICK_SPACING or tick_spacing > MAX_TICK_SPACING:
        raise BaselineStrategyError(
            f"{field}={tick_spacing} outside V4 tick-spacing domain "
            f"[{MIN_TICK_SPACING}, {MAX_TICK_SPACING}]"
        )
    return value


def _require_chain_id(chain_id: int, *, field: str) -> int:
    value = _require_positive_int(chain_id, field=field)
    return value


def _require_capital(capital_q64_64: int, *, field: str) -> int:
    value = _require_non_negative_int(capital_q64_64, field=field)
    if value <= 0:
        raise InvalidBaselineCapitalError(f"{field}={capital_q64_64} must be positive")
    return value


def _require_liquidity(liquidity: int, *, field: str) -> int:
    value = _require_positive_int(liquidity, field=field)
    if value >= (1 << 128):
        raise BaselineStrategyError(f"{field}={liquidity} exceeds uint128 width")
    return value


def _snap_down_to_spacing(tick: int, tick_spacing: int) -> int:
    """Return the largest tick <= ``tick`` that is aligned to ``tick_spacing``.

    Ticks aligned to ``tick_spacing`` are exact multiples of
    ``tick_spacing``. ``0`` is a valid aligned tick; very large
    negative or positive ticks are clamped to the V4 integer domain.
    """
    if tick_spacing <= 0:
        raise BaselineStrategyError(
            f"_snap_down_to_spacing: tick_spacing must be positive, got {tick_spacing}"
        )
    # Truncation toward zero (Python's ``//``) for non-negative ticks
    # gives the right aligned tick. For negative ticks we add the
    # spacing before truncating so the result is also aligned.
    if tick >= 0:
        return (tick // tick_spacing) * tick_spacing
    return -((-tick + tick_spacing - 1) // tick_spacing) * tick_spacing


def _snap_up_to_spacing(tick: int, tick_spacing: int) -> int:
    """Return the smallest tick >= ``tick`` that is aligned to ``tick_spacing``."""
    if tick_spacing <= 0:
        raise BaselineStrategyError(
            f"_snap_up_to_spacing: tick_spacing must be positive, got {tick_spacing}"
        )
    if tick >= 0:
        return ((tick + tick_spacing - 1) // tick_spacing) * tick_spacing
    return -((-tick) // tick_spacing) * tick_spacing


def _price_q64_64_to_sqrt_price_x96(price_q64_64: int) -> int:
    """Return the integer sqrt of ``price_q64_64`` expressed as Q64.96.

    ``price_q64_64`` is a Q64.64 USDG / raw-token ratio. Converting
    to Q64.96 sqrt-price (the V4 wire format) requires taking the
    integer square root of the price scaled by ``2 ** 128``. The
    conversion uses :func:`math.isqrt` and stays in integer
    arithmetic; the result is the floor of the true sqrt, which
    V4's ``get_tick_at_sqrt_price`` accepts.
    """
    if price_q64_64 <= 0:
        raise BaselineStrategyError(
            f"_price_q64_64_to_sqrt_price_x96: price_q64_64 must be positive, got {price_q64_64}"
        )
    # ``math.isqrt`` is exact for non-negative integers; the
    # intermediate is bounded by Python's arbitrary precision, so
    # there is no overflow risk on the protocol path.
    return math.isqrt(price_q64_64 << (_Q96_SHIFT * 2))


def _extract_latest_price_q64_64(events: Sequence[BacktestEvent]) -> int | None:
    """Return the latest ``price_q64_64`` carried by a data-priority event.

    The engine hands the strategy every event whose ``observed_at``
    and ``available_at`` are at or before ``decision_time``; the
    *latest* such event is the current price observation. Events
    whose ``source_priority`` is not :data:`SOURCE_PRIORITY_DATA` are
    excluded; an event without a ``price_q64_64`` payload is also
    excluded (e.g. a ``TICK`` event). The returned price is the
    first match when iterating events in *reverse* chronological
    order, so the result is deterministic across runs with the same
    event manifest.
    """
    latest_price: int | None = None
    latest_timestamp: int = -1
    for evt in events:
        if evt.source_priority != SOURCE_PRIORITY_DATA:
            continue
        if evt.kind not in (KIND_OBSERVATION, KIND_SWAP):
            continue
        if evt.timestamp < latest_timestamp:
            continue
        # Read the first ``price_q64_64`` value from the payload.
        for key, value in evt.payload:
            if key == "price_q64_64":
                if isinstance(value, bool) or not isinstance(value, int):
                    continue
                latest_price = _require_int(value, field="price_q64_64")
                latest_timestamp = evt.timestamp
                break
    return latest_price


def _extract_recent_prices_q64_64(
    events: Sequence[BacktestEvent],
    *,
    window: int,
) -> tuple[int, ...]:
    """Return the most recent ``window`` ``price_q64_64`` values.

    The window is bounded by ``window`` (the caller-supplied lookback).
    The result is ordered from oldest to newest. Events whose
    ``source_priority`` is not :data:`SOURCE_PRIORITY_DATA` and events
    without a ``price_q64_64`` payload are excluded. The function is
    deterministic for the same event manifest and the same ``window``.
    """
    if window <= 0:
        raise BaselineStrategyError(
            f"_extract_recent_prices_q64_64: window must be positive, got {window}"
        )
    prices: list[tuple[int, int]] = []
    for evt in events:
        if evt.source_priority != SOURCE_PRIORITY_DATA:
            continue
        if evt.kind not in (KIND_OBSERVATION, KIND_SWAP):
            continue
        for key, value in evt.payload:
            if key == "price_q64_64":
                if isinstance(value, bool) or not isinstance(value, int):
                    continue
                prices.append((evt.timestamp, _require_int(value, field="price_q64_64")))
                break
    # Sort by timestamp ascending (deterministic tie-break by event_id).
    prices.sort(key=lambda pair: pair[0])
    if len(prices) > window:
        prices = prices[-window:]
    return tuple(price for _ts, price in prices)


def _realised_sigma_ticks(
    prices: Sequence[int],
    *,
    pool_price_q64_64: int,
) -> int:
    """Return the integer-tick standard deviation of the log-return series.

    ``prices`` is a window of Q64.64 prices ordered from oldest to
    newest. ``pool_price_q64_64`` is the *current* price (the latest
    element of the window); the function rescales every price to a
    tick via ``log(price / pool_price_q64_64) / log(1.0001)`` and
    returns the standard deviation of the resulting tick series.

    The function uses integer arithmetic throughout: log price of
    ``1.0001`` is approximated by ``tick = round(log(price_ratio) /
    log(1.0001))`` via the V4 ``get_tick_at_sqrt_price`` reference
    implementation. A stable result requires at least two price
    observations; the function returns ``0`` when the window has
    fewer than two prices (which the volatility strategy treats as
    "no signal" and replaces with the minimum half-width).
    """
    if len(prices) < 2:
        return 0
    # Normalise every price to a tick relative to the current price.
    ticks: list[int] = []
    for price_q64_64 in prices:
        if price_q64_64 <= 0 or pool_price_q64_64 <= 0:
            continue
        # ``price_q64_64 / pool_price_q64_64`` is the dimensionless
        # ratio ``price(t) / price(now)``. Convert to a tick via the
        # V4 reference math.
        ratio_q64_64 = (price_q64_64 << 64) // pool_price_q64_64
        if ratio_q64_64 <= 0:
            continue
        sqrt_ratio_x96 = _price_q64_64_to_sqrt_price_x96(ratio_q64_64)
        tick = get_tick_at_sqrt_price(sqrt_ratio_x96)
        ticks.append(tick)
    if len(ticks) < 2:
        return 0
    mean = sum(ticks) // len(ticks)
    variance_num = sum((t - mean) * (t - mean) for t in ticks)
    variance = variance_num // len(ticks)
    return math.isqrt(variance)


def _clamp_range_to_v4(
    *,
    tick_lower: int,
    tick_upper: int,
    tick_spacing: int,
) -> tuple[int, int] | None:
    """Return a snapped, V4-valid ``(tick_lower, tick_upper)`` or ``None``.

    A returned value is "valid" when:

    1. ``tick_lower`` and ``tick_upper`` are both inside
       ``[MIN_TICK, MAX_TICK]``;
    2. both are aligned to ``tick_spacing``;
    3. ``tick_lower < tick_upper``.

    ``None`` is returned when the proposed Range cannot be represented
    inside the V4 integer-tick domain; callers translate the failure
    into a structured ``NO_TRADE`` verdict.
    """
    snapped_lower = _snap_down_to_spacing(tick_lower, tick_spacing)
    snapped_upper = _snap_up_to_spacing(tick_upper, tick_spacing)
    if snapped_lower < MIN_TICK or snapped_upper > MAX_TICK:
        return None
    if snapped_lower >= snapped_upper:
        return None
    return snapped_lower, snapped_upper


# ---------------------------------------------------------------------------
# Hold / No-LP baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HoldStrategy:
    """The zero-action baseline.

    The policy never proposes a position: every decision returns
    ``NO_TRADE``. This is the canonical HODL / cash-benchmark anchor
    (``docs/spec/strategy/STRATEGY_ECONOMICS.md`` §2 ``ECO-OBJ-002``).

    Parameters
    ----------
    pool_key_id:
        The V4 PoolKey id the strategy is bound to. The id is
        recorded on every decision for audit traceability.
    chain_id:
        The chain id the strategy is bound to. The id is recorded on
        every decision for audit traceability.
    notes:
        Extra structured notes appended to every decision. The
        default empty tuple keeps the decision payload minimal.

    Notes
    -----
    No numerical parameters affect the decision. The strategy is
    fully specified by ``(pool_key_id, chain_id)``; two
    :class:`HoldStrategy` instances with identical fields produce
    identical decisions for the same input manifest.
    """

    pool_key_id: str
    chain_id: int
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise BaselineStrategyError(
                f"HoldStrategy.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        _require_chain_id(self.chain_id, field="HoldStrategy.chain_id")
        if not isinstance(self.notes, tuple):
            raise BaselineStrategyError(
                f"HoldStrategy.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise BaselineStrategyError(
                    f"HoldStrategy.notes: every entry must be str, got {type(n).__name__}"
                )

    def __call__(self, request: StrategyDecisionRequest) -> StrategyDecision:
        """Return ``NO_TRADE`` for every decision.

        The decision carries the pool / chain identity, the decision
        time the engine recorded and a fixed ``BASELINE_HOLD`` notes
        tag so the audit chain can identify the policy that produced
        the decision without re-parsing the engine configuration.
        """
        return StrategyDecision(
            kind="NO_TRADE",
            pool_key_id=self.pool_key_id,
            chain_id=self.chain_id,
            decision_time=request.decision_time,
            notes=self.notes + (NOTES_HOLD,),
        )


# ---------------------------------------------------------------------------
# Protocol-valid broad-range baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BroadRangeStrategy:
    """The protocol-valid broad-range baseline.

    Opens a single position at the widest Range the pool's
    ``tick_spacing`` admits — *not* the V4 ``int24`` domain, which
    would be rejected by the pool, and *not* a literal "infinite
    / full-range" Range that the tick-spacing requirement rules
    out. The widest valid Range is
    ``[min_usable_tick(tick_spacing), max_usable_tick(tick_spacing)]``
    (the largest and smallest aligned ticks inside the V4
    ``[MIN_TICK, MAX_TICK]`` integer-tick domain). After opening,
    the strategy returns ``WAIT`` for every subsequent decision: a
    Range this wide cannot leave range under any single-tick
    market move, so the rebalance policy is "never rebalance".

    Parameters
    ----------
    pool_key_id:
        The V4 PoolKey id the strategy is bound to.
    chain_id:
        The chain id the strategy is bound to.
    tick_spacing:
        The pool's V4 ``tick_spacing``. Required because the widest
        valid Range depends on it (a tick-spacing of ``60`` cannot
        land on tick ``1``, while a tick-spacing of ``1`` can).
    liquidity:
        The integer liquidity the strategy proposes when opening.
        Defaults to :data:`DEFAULT_BASELINE_LIQUIDITY`.
    capital_q64_64:
        The Q64.64 USDG capital envelope the strategy proposes when
        opening. Defaults to :data:`DEFAULT_BASELINE_CAPITAL_Q64_64`.
    notes:
        Extra structured notes appended to every decision.

    Notes
    -----
    "Broad range" is intentionally **not** the V4 ``int24`` domain
    ``[-2**23, 2**23 - 1]`` and **not** the V4 integer-tick domain
    ``[MIN_TICK, MAX_TICK] = [-887272, 887272]`` literally. Both
    fail the tick-spacing alignment requirement the pool enforces on
    mint; the strategy explicitly snaps to ``tick_spacing`` and
    clamps to the V4 ``[MIN_TICK, MAX_TICK]`` domain so the proposal
    is pool-acceptable on chain.
    """

    pool_key_id: str
    chain_id: int
    tick_spacing: int
    liquidity: int = DEFAULT_BASELINE_LIQUIDITY
    capital_q64_64: int = DEFAULT_BASELINE_CAPITAL_Q64_64
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise BaselineStrategyError(
                f"BroadRangeStrategy.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        _require_chain_id(self.chain_id, field="BroadRangeStrategy.chain_id")
        _require_tick_spacing(self.tick_spacing, field="BroadRangeStrategy.tick_spacing")
        _require_liquidity(self.liquidity, field="BroadRangeStrategy.liquidity")
        _require_capital(self.capital_q64_64, field="BroadRangeStrategy.capital_q64_64")
        if not isinstance(self.notes, tuple):
            raise BaselineStrategyError(
                f"BroadRangeStrategy.notes: must be tuple[str, ...], "
                f"got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise BaselineStrategyError(
                    f"BroadRangeStrategy.notes: every entry must be str, got {type(n).__name__}"
                )

    def __call__(self, request: StrategyDecisionRequest) -> StrategyDecision:
        """Return ``PROPOSE`` on the first decision (empty ledger) or ``WAIT`` after."""
        ledger = request.ledger
        if ledger.liquidity == 0:
            lower = min_usable_tick(self.tick_spacing)
            upper = max_usable_tick(self.tick_spacing)
            return StrategyDecision(
                kind="PROPOSE",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                tick_lower=lower,
                tick_upper=upper,
                liquidity=self.liquidity,
                capital_q64_64=self.capital_q64_64,
                notes=self.notes + (NOTES_BROAD_RANGE_OPEN,),
            )
        return StrategyDecision(
            kind="WAIT",
            pool_key_id=self.pool_key_id,
            chain_id=self.chain_id,
            decision_time=request.decision_time,
            notes=self.notes + (NOTES_BROAD_RANGE_HOLD,),
        )


# ---------------------------------------------------------------------------
# Fixed-width baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FixedWidthStrategy:
    """The fixed-width baseline.

    Opens a symmetric Range ``±half_width_ticks`` around the current
    tick (snapped to ``tick_spacing``). If the position leaves the
    Range, the strategy rebalances around the new current tick.

    Parameters
    ----------
    pool_key_id:
        The V4 PoolKey id the strategy is bound to.
    chain_id:
        The chain id the strategy is bound to.
    tick_spacing:
        The pool's V4 ``tick_spacing`` the strategy snaps every
        proposed Range to.
    half_width_ticks:
        Half-range width in raw ticks. Defaults to
        :data:`DEFAULT_FIXED_HALF_WIDTH_TICKS` (``600`` ≈ ±6%).
        The comparison harness overrides this per experiment.
    liquidity:
        The integer liquidity the strategy proposes. Defaults to
        :data:`DEFAULT_BASELINE_LIQUIDITY`.
    capital_q64_64:
        The Q64.64 USDG capital envelope. Defaults to
        :data:`DEFAULT_BASELINE_CAPITAL_Q64_64`.
    notes:
        Extra structured notes appended to every decision.

    Notes
    -----
    The current tick is read from the most recent ``price_q64_64``
    observation the engine hands the strategy. When no price event
    is visible, the strategy falls back to ``WAIT`` (the strategy
    cannot reason about a tick it cannot observe).
    """

    pool_key_id: str
    chain_id: int
    tick_spacing: int
    half_width_ticks: int = DEFAULT_FIXED_HALF_WIDTH_TICKS
    liquidity: int = DEFAULT_BASELINE_LIQUIDITY
    capital_q64_64: int = DEFAULT_BASELINE_CAPITAL_Q64_64
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise BaselineStrategyError(
                f"FixedWidthStrategy.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        _require_chain_id(self.chain_id, field="FixedWidthStrategy.chain_id")
        _require_tick_spacing(self.tick_spacing, field="FixedWidthStrategy.tick_spacing")
        _require_positive_int(self.half_width_ticks, field="FixedWidthStrategy.half_width_ticks")
        _require_liquidity(self.liquidity, field="FixedWidthStrategy.liquidity")
        _require_capital(self.capital_q64_64, field="FixedWidthStrategy.capital_q64_64")
        if not isinstance(self.notes, tuple):
            raise BaselineStrategyError(
                f"FixedWidthStrategy.notes: must be tuple[str, ...], "
                f"got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise BaselineStrategyError(
                    f"FixedWidthStrategy.notes: every entry must be str, got {type(n).__name__}"
                )

    def __call__(self, request: StrategyDecisionRequest) -> StrategyDecision:
        """Return ``PROPOSE`` (open or rebalance) or ``WAIT`` (in range)."""
        current_price = _extract_latest_price_q64_64(request.visible_events)
        if current_price is None:
            return StrategyDecision(
                kind="NO_TRADE",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_HOLD, NOTES_NO_MARKET_DATA),
            )
        try:
            sqrt_price_x96 = _price_q64_64_to_sqrt_price_x96(current_price)
            current_tick = get_tick_at_sqrt_price(sqrt_price_x96)
        except ValueError:
            return StrategyDecision(
                kind="NO_TRADE",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_HOLD, NOTES_HOLD_INVALID_TICK),
            )
        # Determine whether we are opening or rebalancing.
        ledger = request.ledger
        if ledger.liquidity == 0:
            action_tag = NOTES_FIXED_WIDTH_OPEN
        elif current_tick < ledger.tick_lower or current_tick >= ledger.tick_upper:
            action_tag = NOTES_FIXED_WIDTH_REBALANCE
        else:
            return StrategyDecision(
                kind="WAIT",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_FIXED_WIDTH_HOLD,),
            )
        snapped = _clamp_range_to_v4(
            tick_lower=current_tick - self.half_width_ticks,
            tick_upper=current_tick + self.half_width_ticks,
            tick_spacing=self.tick_spacing,
        )
        if snapped is None:
            return StrategyDecision(
                kind="NO_TRADE",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_HOLD, NOTES_TICKS_OUT_OF_BOUNDS),
            )
        tick_lower, tick_upper = snapped
        return StrategyDecision(
            kind="PROPOSE",
            pool_key_id=self.pool_key_id,
            chain_id=self.chain_id,
            decision_time=request.decision_time,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=self.liquidity,
            capital_q64_64=self.capital_q64_64,
            notes=self.notes + (action_tag,),
        )


# ---------------------------------------------------------------------------
# Volatility-width baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VolatilityWidthStrategy:
    """The volatility-width baseline.

    The Range half-width in ticks is
    ``clamp(round(k * sigma_ticks), min_half_width_ticks,
    max_half_width_ticks)``, where ``sigma_ticks`` is the standard
    deviation of the log-return series expressed in ticks and ``k``
    is :attr:`volatility_multiplier`. The default ``k = 2`` matches
    the canonical "two-sigma" framing (R15 §3).

    The realised-volatility estimator consumes a window of the most
    recent :attr:`volatility_window` price events the engine hands
    the strategy; an undersized window returns ``NO_TRADE`` (no
    signal). The position rebalances when the current tick leaves
    the existing Range.

    Parameters
    ----------
    pool_key_id:
        The V4 PoolKey id the strategy is bound to.
    chain_id:
        The chain id the strategy is bound to.
    tick_spacing:
        The pool's V4 ``tick_spacing`` the strategy snaps every
        proposed Range to.
    volatility_multiplier:
        The dimensionless multiplier ``k``. Defaults to
        :data:`DEFAULT_VOLATILITY_MULTIPLIER` (``2``).
    min_half_width_ticks:
        Floor on the half-width in ticks. Defaults to
        :data:`DEFAULT_MIN_HALF_WIDTH_TICKS` (``60``).
    max_half_width_ticks:
        Ceiling on the half-width in ticks. Defaults to
        :data:`DEFAULT_MAX_HALF_WIDTH_TICKS` (``5_000``). Kept well
        below the broad-range policy's width so the volatility
        policy remains distinguishable from the broad-range policy
        in the comparison harness.
    volatility_window:
        Number of recent price events the volatility estimator
        consumes. Defaults to :data:`DEFAULT_VOLATILITY_WINDOW`
        (``20``).
    liquidity:
        The integer liquidity the strategy proposes. Defaults to
        :data:`DEFAULT_BASELINE_LIQUIDITY`.
    capital_q64_64:
        The Q64.64 USDG capital envelope. Defaults to
        :data:`DEFAULT_BASELINE_CAPITAL_Q64_64`.
    notes:
        Extra structured notes appended to every decision.

    Notes
    -----
    Defaults are *not* optimised on the evaluation period (T062
    Must-not). They are round, auditable numbers the comparison
    harness may swap per experiment.
    """

    pool_key_id: str
    chain_id: int
    tick_spacing: int
    volatility_multiplier: int = DEFAULT_VOLATILITY_MULTIPLIER
    min_half_width_ticks: int = DEFAULT_MIN_HALF_WIDTH_TICKS
    max_half_width_ticks: int = DEFAULT_MAX_HALF_WIDTH_TICKS
    volatility_window: int = DEFAULT_VOLATILITY_WINDOW
    liquidity: int = DEFAULT_BASELINE_LIQUIDITY
    capital_q64_64: int = DEFAULT_BASELINE_CAPITAL_Q64_64
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise BaselineStrategyError(
                f"VolatilityWidthStrategy.pool_key_id: must be non-empty str, "
                f"got {self.pool_key_id!r}"
            )
        _require_chain_id(self.chain_id, field="VolatilityWidthStrategy.chain_id")
        _require_tick_spacing(self.tick_spacing, field="VolatilityWidthStrategy.tick_spacing")
        _require_positive_int(
            self.volatility_multiplier,
            field="VolatilityWidthStrategy.volatility_multiplier",
        )
        _require_positive_int(
            self.min_half_width_ticks,
            field="VolatilityWidthStrategy.min_half_width_ticks",
        )
        _require_positive_int(
            self.max_half_width_ticks,
            field="VolatilityWidthStrategy.max_half_width_ticks",
        )
        if self.min_half_width_ticks > self.max_half_width_ticks:
            raise BaselineStrategyError(
                f"VolatilityWidthStrategy: min_half_width_ticks="
                f"{self.min_half_width_ticks} exceeds "
                f"max_half_width_ticks={self.max_half_width_ticks}"
            )
        _require_positive_int(
            self.volatility_window,
            field="VolatilityWidthStrategy.volatility_window",
        )
        _require_liquidity(self.liquidity, field="VolatilityWidthStrategy.liquidity")
        _require_capital(self.capital_q64_64, field="VolatilityWidthStrategy.capital_q64_64")
        if not isinstance(self.notes, tuple):
            raise BaselineStrategyError(
                f"VolatilityWidthStrategy.notes: must be tuple[str, ...], "
                f"got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise BaselineStrategyError(
                    f"VolatilityWidthStrategy.notes: every entry must be str, "
                    f"got {type(n).__name__}"
                )

    def _compute_half_width(self, prices: tuple[int, ...]) -> int:
        """Return the half-width in ticks for the visible price window."""
        if len(prices) < 2:
            return self.min_half_width_ticks
        current_price = prices[-1]
        sigma_ticks = _realised_sigma_ticks(prices, pool_price_q64_64=current_price)
        target = self.volatility_multiplier * sigma_ticks
        if target < self.min_half_width_ticks:
            return self.min_half_width_ticks
        if target > self.max_half_width_ticks:
            return self.max_half_width_ticks
        return target

    def __call__(self, request: StrategyDecisionRequest) -> StrategyDecision:
        """Return ``PROPOSE`` (open or rebalance) or ``WAIT`` (in range)."""
        prices = _extract_recent_prices_q64_64(
            request.visible_events, window=self.volatility_window
        )
        if len(prices) < 2:
            return StrategyDecision(
                kind="NO_TRADE",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_HOLD, NOTES_NO_MARKET_DATA),
            )
        current_price = prices[-1]
        half_width = self._compute_half_width(prices)
        try:
            sqrt_price_x96 = _price_q64_64_to_sqrt_price_x96(current_price)
            current_tick = get_tick_at_sqrt_price(sqrt_price_x96)
        except ValueError:
            return StrategyDecision(
                kind="NO_TRADE",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_HOLD, NOTES_HOLD_INVALID_TICK),
            )
        ledger = request.ledger
        if ledger.liquidity == 0:
            action_tag = NOTES_VOLATILITY_OPEN
        elif current_tick < ledger.tick_lower or current_tick >= ledger.tick_upper:
            action_tag = NOTES_VOLATILITY_REBALANCE
        else:
            return StrategyDecision(
                kind="WAIT",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_VOLATILITY_HOLD,),
            )
        snapped = _clamp_range_to_v4(
            tick_lower=current_tick - half_width,
            tick_upper=current_tick + half_width,
            tick_spacing=self.tick_spacing,
        )
        if snapped is None:
            return StrategyDecision(
                kind="NO_TRADE",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_HOLD, NOTES_TICKS_OUT_OF_BOUNDS),
            )
        tick_lower, tick_upper = snapped
        return StrategyDecision(
            kind="PROPOSE",
            pool_key_id=self.pool_key_id,
            chain_id=self.chain_id,
            decision_time=request.decision_time,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=self.liquidity,
            capital_q64_64=self.capital_q64_64,
            notes=self.notes + (action_tag,),
        )


# ---------------------------------------------------------------------------
# Out-of-range rebalance baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutOfRangeRebalanceStrategy:
    """The rebalance-only baseline.

    Opens a symmetric ``±half_width_ticks`` Range around the current
    tick on the first decision. While in range the strategy returns
    ``WAIT`` for every subsequent decision; *only* a current tick
    outside ``[tick_lower, tick_upper)`` triggers a rebalance. The
    rebalance centres the new Range on the new current tick.

    Parameters
    ----------
    pool_key_id:
        The V4 PoolKey id the strategy is bound to.
    chain_id:
        The chain id the strategy is bound to.
    tick_spacing:
        The pool's V4 ``tick_spacing`` the strategy snaps every
        proposed Range to.
    half_width_ticks:
        Half-range width in raw ticks. Defaults to
        :data:`DEFAULT_REBALANCE_HALF_WIDTH_TICKS` (``600`` ≈ ±6%).
    liquidity:
        The integer liquidity the strategy proposes. Defaults to
        :data:`DEFAULT_BASELINE_LIQUIDITY`.
    capital_q64_64:
        The Q64.64 USDG capital envelope. Defaults to
        :data:`DEFAULT_BASELINE_CAPITAL_Q64_64`.
    notes:
        Extra structured notes appended to every decision.
    """

    pool_key_id: str
    chain_id: int
    tick_spacing: int
    half_width_ticks: int = DEFAULT_REBALANCE_HALF_WIDTH_TICKS
    liquidity: int = DEFAULT_BASELINE_LIQUIDITY
    capital_q64_64: int = DEFAULT_BASELINE_CAPITAL_Q64_64
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise BaselineStrategyError(
                f"OutOfRangeRebalanceStrategy.pool_key_id: must be non-empty str, "
                f"got {self.pool_key_id!r}"
            )
        _require_chain_id(self.chain_id, field="OutOfRangeRebalanceStrategy.chain_id")
        _require_tick_spacing(self.tick_spacing, field="OutOfRangeRebalanceStrategy.tick_spacing")
        _require_positive_int(
            self.half_width_ticks,
            field="OutOfRangeRebalanceStrategy.half_width_ticks",
        )
        _require_liquidity(self.liquidity, field="OutOfRangeRebalanceStrategy.liquidity")
        _require_capital(
            self.capital_q64_64,
            field="OutOfRangeRebalanceStrategy.capital_q64_64",
        )
        if not isinstance(self.notes, tuple):
            raise BaselineStrategyError(
                f"OutOfRangeRebalanceStrategy.notes: must be tuple[str, ...], "
                f"got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise BaselineStrategyError(
                    f"OutOfRangeRebalanceStrategy.notes: every entry must be str, "
                    f"got {type(n).__name__}"
                )

    def __call__(self, request: StrategyDecisionRequest) -> StrategyDecision:
        """Return ``PROPOSE`` on open / rebalance or ``WAIT`` otherwise."""
        current_price = _extract_latest_price_q64_64(request.visible_events)
        if current_price is None:
            return StrategyDecision(
                kind="NO_TRADE",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_HOLD, NOTES_NO_MARKET_DATA),
            )
        try:
            sqrt_price_x96 = _price_q64_64_to_sqrt_price_x96(current_price)
            current_tick = get_tick_at_sqrt_price(sqrt_price_x96)
        except ValueError:
            return StrategyDecision(
                kind="NO_TRADE",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_HOLD, NOTES_HOLD_INVALID_TICK),
            )
        ledger = request.ledger
        if ledger.liquidity == 0:
            action_tag = NOTES_REBALANCE_OPEN
        elif current_tick < ledger.tick_lower or current_tick >= ledger.tick_upper:
            action_tag = NOTES_REBALANCE_REBALANCE
        else:
            return StrategyDecision(
                kind="WAIT",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_REBALANCE_HOLD,),
            )
        snapped = _clamp_range_to_v4(
            tick_lower=current_tick - self.half_width_ticks,
            tick_upper=current_tick + self.half_width_ticks,
            tick_spacing=self.tick_spacing,
        )
        if snapped is None:
            return StrategyDecision(
                kind="NO_TRADE",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=self.notes + (NOTES_HOLD, NOTES_TICKS_OUT_OF_BOUNDS),
            )
        tick_lower, tick_upper = snapped
        return StrategyDecision(
            kind="PROPOSE",
            pool_key_id=self.pool_key_id,
            chain_id=self.chain_id,
            decision_time=request.decision_time,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=self.liquidity,
            capital_q64_64=self.capital_q64_64,
            notes=self.notes + (action_tag,),
        )


# ---------------------------------------------------------------------------
# Convenience: registry of baseline tags
# ---------------------------------------------------------------------------


def baseline_kind_tag(decision: StrategyDecision) -> str | None:
    """Return the first ``BASELINE_*`` notes tag on a decision.

    Returns ``None`` when the notes do not carry a baseline tag;
    downstream code can use this to attribute an audit event to the
    baseline that produced it without re-parsing the engine
    configuration.
    """
    for note in decision.notes:
        if note.startswith("BASELINE_"):
            return note
    return None


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "BASELINE_STRATEGY_VERSION",
    "BroadRangeStrategy",
    "DEFAULT_BASELINE_CAPITAL_Q64_64",
    "DEFAULT_BASELINE_CHAIN_ID",
    "DEFAULT_BASELINE_LIQUIDITY",
    "DEFAULT_FIXED_HALF_WIDTH_TICKS",
    "DEFAULT_MAX_HALF_WIDTH_TICKS",
    "DEFAULT_MIN_HALF_WIDTH_TICKS",
    "DEFAULT_REBALANCE_HALF_WIDTH_TICKS",
    "DEFAULT_VOLATILITY_MULTIPLIER",
    "DEFAULT_VOLATILITY_WINDOW",
    "FixedWidthStrategy",
    "HoldStrategy",
    "InvalidBaselineCapitalError",
    "InvalidBaselineTickError",
    "BaselineStrategyError",
    "NOTES_BROAD_RANGE_HOLD",
    "NOTES_BROAD_RANGE_OPEN",
    "NOTES_FIXED_WIDTH_HOLD",
    "NOTES_FIXED_WIDTH_OPEN",
    "NOTES_FIXED_WIDTH_REBALANCE",
    "NOTES_HOLD",
    "NOTES_HOLD_INVALID_TICK",
    "NOTES_INSUFFICIENT_CAPITAL",
    "NOTES_NO_MARKET_DATA",
    "NOTES_REBALANCE_HOLD",
    "NOTES_REBALANCE_OPEN",
    "NOTES_REBALANCE_REBALANCE",
    "NOTES_TICKS_OUT_OF_BOUNDS",
    "NOTES_VOLATILITY_HOLD",
    "NOTES_VOLATILITY_OPEN",
    "NOTES_VOLATILITY_REBALANCE",
    "OutOfRangeRebalanceStrategy",
    "VolatilityWidthStrategy",
    "baseline_kind_tag",
]
