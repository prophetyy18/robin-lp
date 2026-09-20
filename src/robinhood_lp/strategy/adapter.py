"""Engine-callback adapter for the USDG-first adaptive-Range strategy (T065).

The adapter is the bridge the engine-callback contract in
``docs/spec/strategy/STRATEGY_ECONOMICS.md`` §7 assigns to this task.
It takes the per-decision ``StrategyDecisionRequest`` the T061
event-driven backtest engine hands it, projects the visible events
and the ledger into the T065 strategy-layer snapshots, calls the
adaptive strategy, and converts the resulting
:class:`AdaptiveCandidateAction` back to the engine's
:class:`StrategyDecision`. The two boundaries (engine decision
/ strategy candidate) keep their meaning: the engine sees the
engine-level ``kind`` (``NO_TRADE`` / ``WAIT`` / ``PROPOSE``);
the strategy layer sees its richer ``AdaptiveCandidateKind``
vocabulary.

Layer purity (binding):

- The adapter depends on the engine / strategy contracts
  (``StrategyDecisionRequest`` / ``StrategyDecision`` from
  ``robinhood_lp.protocol.contracts``) and the T065 strategy
  module. It does **not** import RPC, storage, signing,
  execution, or replay.

- ``AdaptiveStrategyCallback`` is the engine-callback surface;
  the engine constructs it once and invokes it on every
  reactive event. The adapter is *pure*: it never mutates the
  request, the ledger, the engine state, or any shared mutable
  object.

Design constraints (binding):

- **Pure projection.** The adapter projects visible events into
  strategy snapshots without consuming any future event. An
  event whose ``available_at`` exceeds ``decision_time`` would
  already be invisible to the request; the adapter treats the
  ``visible_events`` tuple as the information frontier the
  engine established.

- **Integer-only.** ``float`` does not appear on the protocol /
  valuation path. Q64.64 returns and integer ticks are the only
  quantitative outputs.

- **Deterministic.** The same request and the same strategy
  instance produce the same :class:`StrategyDecision` in every
  process. The adapter never reads wall-clock time and never
  uses unseeded randomness.

- **No global state mutation.** The 5-minute USDG rule fires
  *inside* the strategy; the adapter does not record it in any
  engine-level state, in any shared cache, or in any run-level
  flag. The rule's effect is bound to the candidate action the
  strategy returns; the engine routes that action into the
  audit chain and the ledger evolves accordingly. The ``Must
  not`` clause ("no global run-state change") is binding.

References:

- ``docs/spec/strategy/STRATEGY_ECONOMICS.md`` §7 — the
  component boundary and the engine-callback adapter.
- T061 — the event-driven backtest engine that consumes the
  callback.
- T050 / T053 — the bar layer and the USDG quote source whose
  results the adapter reads to compute the 5-minute USDG return.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from robinhood_lp.protocol.contracts import (
    KIND_OBSERVATION,
    KIND_SWAP,
    SOURCE_PRIORITY_DATA,
    BacktestEvent,
    StrategyDecision,
    StrategyDecisionRequest,
)
from robinhood_lp.strategy.adaptive import (
    ADAPTIVE_ADMISSION_SNAPSHOT_VERSION,
    ADAPTIVE_MARKET_SNAPSHOT_VERSION,
    ADAPTIVE_PORTFOLIO_SNAPSHOT_VERSION,
    Q64_SCALE,
    AdaptiveAdmissionSnapshot,
    AdaptiveCandidateAction,
    AdaptiveCandidateKind,
    AdaptiveMarketSnapshot,
    AdaptivePortfolioSnapshot,
    AdaptiveStrategy,
)

#: Adapter module version. Bumping the version is a breaking change for
#: downstream consumers (the T061 engine wiring).
ADAPTER_VERSION: Final[str] = "t065.engine_adapter.v1"

#: Notes prefix emitted on every engine-level ``StrategyDecision`` the
#: adapter produces. The prefix lets the audit chain attribute the
#: decision to the T065 strategy without re-parsing the engine
#: configuration.
_NOTES_PREFIX: Final[str] = "ADAPTIVE_STRATEGY"

#: Notes prefix attached to lifecycle decisions the strategy emits
#: but the engine does not have a closed-set value for. The engine
#: records these as ``WAIT`` (or ``NO_TRADE`` for ``REBUILD_DEFER`` /
#  ``EXIT``) with structured notes that preserve the lifecycle state.
_LIFECYCLE_NOTES_PREFIX: Final[str] = "ADAPTIVE_LIFECYCLE"

#: Minimum V4 sqrt-price. Mirrors ``MIN_SQRT_PRICE_X96`` from the
#: protocol math module; we re-declare the constant here because
#: importing a private value from a sibling layer would couple this
#: module to a private symbol.
_MIN_SQRT_PRICE_X96: Final[int] = 4_295_128_739
_MAX_SQRT_PRICE_X96: Final[int] = 1_461_446_703_485_210_103_287_273_052_203_988_822_378_723_970_342


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EngineAdapterError(ValueError):
    """Base class for engine-adapter construction / evaluation failures."""


class InvalidEngineRequestError(EngineAdapterError):
    """The engine-supplied :class:`StrategyDecisionRequest` violates an
    adapter invariant."""


# ---------------------------------------------------------------------------
# Helpers (pure functions; no module state)
# ---------------------------------------------------------------------------


def _extract_price_q64_64(payload: Sequence[tuple[str, int | str | bool]]) -> int | None:
    """Return the ``price_q64_64`` field from a payload, or ``None``.

    The helper mirrors the one in :mod:`robinhood_lp.strategy.adaptive`
    so the adapter does not have to import a private helper.
    Duplication is acceptable: the adapter is a small, layer-bound
    surface.
    """
    for key, value in payload:
        if key == "price_q64_64":
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            return value
    return None


def _data_price_series(
    events: Sequence[BacktestEvent],
) -> tuple[tuple[int, int], ...]:
    """Return the deterministic ``(timestamp, price_q64_64)`` series.

    Only ``DATA`` / ``SWAP`` / ``OBSERVATION`` events contribute.
    The series is sorted ascending by timestamp; ties are broken
    by keeping the first occurrence.
    """
    series: list[tuple[int, int]] = []
    seen_ts: set[int] = set()
    for evt in events:
        if evt.source_priority != SOURCE_PRIORITY_DATA:
            continue
        if evt.kind not in (KIND_OBSERVATION, KIND_SWAP):
            continue
        if evt.timestamp in seen_ts:
            continue
        price = _extract_price_q64_64(evt.payload)
        if price is None or price <= 0:
            continue
        series.append((evt.timestamp, price))
        seen_ts.add(evt.timestamp)
    series.sort(key=lambda pair: pair[0])
    return tuple(series)


def compute_five_minute_return_q64_64(
    events: Sequence[BacktestEvent],
    *,
    decision_time: int,
    window_seconds: int = 300,
) -> tuple[int, bool]:
    """Compute the complete 5-minute USDG return and the completion flag.

    Returns ``(return_q64_64, is_complete)``. ``is_complete`` is
    ``True`` only when the most recent 5-minute bar has fully
    closed at ``decision_time`` *and* the open and close USDG
    prices are both available from the visible events. An
    incomplete bar — one whose close timestamp is strictly
    greater than ``decision_time`` — never triggers the rule
    (the binding ``Must not`` clause).

    The bar-end semantics: a 5-minute bar ``[open, close)`` is
    complete iff ``decision_time >= close``. The most recent
    *complete* bar end is the largest multiple of
    ``window_seconds`` that satisfies ``bar_end <=
    decision_time`` AND ``bar_end >= window_seconds`` (the
    latter ensures the open timestamp is non-negative). When
    ``decision_time`` is itself a multiple of ``window_seconds``
    and at least one full window has passed, the bar that
    *just* ended is the most recent complete bar.

    The window length defaults to ``300`` seconds; an explicit
    :attr:`AdaptiveRangeParameters.five_minute_window_seconds` is
    threaded through the adapter so the rule stays consistent
    with ``CTRL-MARKET-001`` and the ``ECO-REGIME-001``
    requirement.
    """
    if window_seconds <= 0:
        raise EngineAdapterError(
            f"compute_five_minute_return_q64_64: window_seconds must be "
            f"positive, got {window_seconds}"
        )
    if decision_time < window_seconds:
        # No complete bar can exist yet — the first full window
        # has not elapsed.
        return 0, False
    # Bar that *contains* ``decision_time``: ends at
    # ``floor(decision_time / window) * window``.
    bar_containing_end = (decision_time // window_seconds) * window_seconds
    if decision_time % window_seconds == 0 and decision_time > 0:
        # ``decision_time`` is exactly a bar boundary; the bar
        # that just ended (``[decision_time - window, decision_time]``)
        # is the most recent complete bar.
        last_complete_end = decision_time
    else:
        last_complete_end = bar_containing_end
    # The open timestamp must be non-negative for the open
    # price to be available from the visible events.
    if last_complete_end - window_seconds < 0:
        return 0, False
    # The bar is complete iff ``decision_time`` has reached the
    # close timestamp. With the boundary handling above this is
    # always true; the explicit check documents the invariant.
    is_complete = decision_time >= last_complete_end
    if not is_complete:
        return 0, False
    bar_open = last_complete_end - window_seconds
    series = _data_price_series(events)
    if not series:
        return 0, False
    open_price = _latest_price_at_or_before(series, bar_open)
    close_price = _latest_price_at_or_before(series, last_complete_end)
    if open_price is None or close_price is None or open_price <= 0:
        return 0, False
    # ``return_q64_64 = close / open * 2**64 - 2**64`` (signed). The
    # adapter returns a signed value; the consumer compares the
    # magnitude against the threshold for an asymmetric rule.
    ratio = (close_price << 64) // open_price
    return_q64_64 = ratio - Q64_SCALE
    return return_q64_64, True


def _latest_price_at_or_before(
    series: Sequence[tuple[int, int]],
    timestamp: int,
) -> int | None:
    """Return the latest price at or before ``timestamp``."""
    latest: int | None = None
    for ts, price in series:
        if ts > timestamp:
            break
        latest = price
    return latest


def _sqrt_price_x96_from_price_q64_64(price_q64_64: int) -> int:
    """Convert a Q64.64 price to a V4 sqrt-price (integer sqrt).

    The T065 module uses the *standard* Q64.64 convention
    (``price_q64_64 = price * 2**64``) and converts to the V4
    Q64.96 wire format with ``isqrt(price_q64_64 << 128)``. The
    conversion uses :func:`math.isqrt` and stays in integer
    arithmetic; floats never appear in the result.

    Note: the conversion factor is ``2**128`` because
    ``sqrt(price * 2**64) * 2**64 = sqrt(price) * 2**96``, the
    canonical V4 ``sqrt_price_x96``. ``isqrt`` rounds the result
    toward zero; ``get_tick_at_sqrt_price`` accepts the floor
    value the integer square root returns.
    """
    if price_q64_64 <= 0:
        raise EngineAdapterError(
            f"_sqrt_price_x96_from_price_q64_64: price_q64_64 must be positive, got {price_q64_64}"
        )
    return math.isqrt(price_q64_64 << 128)


# ---------------------------------------------------------------------------
# Snapshot projection
# ---------------------------------------------------------------------------


def project_admission_snapshot(
    *,
    request: StrategyDecisionRequest,
) -> AdaptiveAdmissionSnapshot:
    """Project an :class:`AdaptiveAdmissionSnapshot` from the engine request.

    The engine does not provide an admission record; the adapter
    uses the request's ``pool_key_id`` / ``chain_id`` and the
    default ``is_admitted=True`` so the strategy can produce a
    decision. The admission snapshot is the strategy's per-decision
    input; the central risk layer (T070) supplies the real
    admission verdict at the next pipeline stage.
    """
    if not isinstance(request, StrategyDecisionRequest):
        raise InvalidEngineRequestError(
            f"project_admission_snapshot: request must be "
            f"StrategyDecisionRequest, got {type(request).__name__}"
        )
    return AdaptiveAdmissionSnapshot(
        version=ADAPTIVE_ADMISSION_SNAPSHOT_VERSION,
        pool_key_id=request.pool_key_id,
        chain_id=request.chain_id,
        support_level="backtest",
        is_admitted=True,
        max_capital_q64_64=1 << 70,
        data_time=request.decision_time,
        availability_time=request.decision_time,
    )


def project_market_snapshot(
    *,
    request: StrategyDecisionRequest,
    window_seconds: int = 300,
) -> AdaptiveMarketSnapshot:
    """Project an :class:`AdaptiveMarketSnapshot` from the engine request.

    The adapter builds the snapshot from the visible events the
    engine hands the callback. The snapshot is *not* mutated by
    the strategy; the adapter returns a fresh instance for every
    call.

    The 5-minute USDG return is computed from the visible price
    series using the same window alignment T050 records. An
    incomplete bar carries ``five_minute_return_complete=False``
    and ``five_minute_return_q64_64=0``; the strategy never reads
    a stale value as complete.
    """
    if not isinstance(request, StrategyDecisionRequest):
        raise InvalidEngineRequestError(
            f"project_market_snapshot: request must be "
            f"StrategyDecisionRequest, got {type(request).__name__}"
        )
    series = _data_price_series(request.visible_events)
    # Extract the latest ``active_liquidity`` from the visible
    # events; the fee model uses this value as the pool liquidity
    # component of the own-share arithmetic.
    latest_liquidity = 0
    for evt in reversed(request.visible_events):
        if evt.source_priority != SOURCE_PRIORITY_DATA:
            continue
        for key, value in evt.payload:
            if key == "active_liquidity":
                if isinstance(value, bool) or not isinstance(value, int):
                    break
                if value < 0:
                    break
                latest_liquidity = value
                break
        if latest_liquidity > 0:
            break
    if not series:
        # No price observation: the strategy returns UNCERTAIN and
        # the adapter does not synthesise a synthetic price. The
        # snapshot is well-formed with a sentinel sqrt-price.
        return_q64_64, is_complete = compute_five_minute_return_q64_64(
            request.visible_events,
            decision_time=request.decision_time,
            window_seconds=window_seconds,
        )
        return AdaptiveMarketSnapshot(
            version=ADAPTIVE_MARKET_SNAPSHOT_VERSION,
            pool_key_id=request.pool_key_id,
            chain_id=request.chain_id,
            sqrt_price_x96=_MIN_SQRT_PRICE_X96,
            liquidity=latest_liquidity,
            realized_volatility_q64_64=0,
            freshness_seconds=0,
            quote_q64_64=None,
            is_relative_only=True,
            five_minute_return_q64_64=return_q64_64,
            five_minute_return_complete=is_complete,
            data_time=request.decision_time,
            availability_time=request.decision_time,
        )
    latest_ts, latest_price = series[-1]
    freshness_seconds = max(0, request.decision_time - latest_ts)
    try:
        sqrt_x96 = _sqrt_price_x96_from_price_q64_64(latest_price)
    except EngineAdapterError:
        sqrt_x96 = _MIN_SQRT_PRICE_X96
    if sqrt_x96 < _MIN_SQRT_PRICE_X96 or sqrt_x96 >= _MAX_SQRT_PRICE_X96:
        sqrt_x96 = _MIN_SQRT_PRICE_X96
    return_q64_64, is_complete = compute_five_minute_return_q64_64(
        request.visible_events,
        decision_time=request.decision_time,
        window_seconds=window_seconds,
    )
    # The USDG quote: when the latest price carries a USDG
    # conversion, ``quote_q64_64`` is the price itself (Q64.64 USDG
    # per raw token). When the dataset is ``RELATIVE_ONLY``, the
    # quote is ``None`` and the strategy treats the snapshot as
    # relative-only. The engine forwards the conversion status
    # via the ``is_relative_only`` event flag; the adapter
    # defaults to USDG-available because the canonical pool
    # carries a USDG pair (per the project intent).
    quote_q64_64: int | None = latest_price
    return AdaptiveMarketSnapshot(
        version=ADAPTIVE_MARKET_SNAPSHOT_VERSION,
        pool_key_id=request.pool_key_id,
        chain_id=request.chain_id,
        sqrt_price_x96=sqrt_x96,
        liquidity=latest_liquidity,
        realized_volatility_q64_64=0,
        freshness_seconds=freshness_seconds,
        quote_q64_64=quote_q64_64,
        is_relative_only=False,
        five_minute_return_q64_64=return_q64_64,
        five_minute_return_complete=is_complete,
        data_time=latest_ts,
        availability_time=request.decision_time,
    )


def project_portfolio_snapshot(
    *,
    request: StrategyDecisionRequest,
) -> AdaptivePortfolioSnapshot:
    """Project an :class:`AdaptivePortfolioSnapshot` from the engine ledger."""
    if not isinstance(request, StrategyDecisionRequest):
        raise InvalidEngineRequestError(
            f"project_portfolio_snapshot: request must be "
            f"StrategyDecisionRequest, got {type(request).__name__}"
        )
    ledger = request.ledger
    is_empty = ledger.liquidity == 0
    in_range = bool(ledger.in_range) and not is_empty
    return AdaptivePortfolioSnapshot(
        version=ADAPTIVE_PORTFOLIO_SNAPSHOT_VERSION,
        pool_key_id=request.pool_key_id,
        chain_id=request.chain_id,
        position_id=ledger.position_id,
        tick_lower=ledger.tick_lower,
        tick_upper=ledger.tick_upper,
        liquidity=ledger.liquidity,
        principal_token0=ledger.principal_token0,
        principal_token1=ledger.principal_token1,
        tokens_owed0=ledger.tokens_owed0,
        tokens_owed1=ledger.tokens_owed1,
        is_empty=is_empty,
        in_range=in_range,
        last_accrual_time=ledger.last_accrual_time,
        data_time=request.decision_time,
        availability_time=request.decision_time,
    )


# ---------------------------------------------------------------------------
# Engine decision conversion
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EngineDecisionMapping:
    """The mapping table the adapter uses to convert
    :class:`AdaptiveCandidateAction` -> :class:`StrategyDecision`.

    The dataclass is exposed for tests; production code reads
    ``ADAPTIVE_TO_ENGINE_KIND`` (a module-level constant) directly.
    """

    notes_prefix: str = _NOTES_PREFIX
    lifecycle_prefix: str = _LIFECYCLE_NOTES_PREFIX


ADAPTIVE_TO_ENGINE_KIND: Final[dict[str, str]] = {
    AdaptiveCandidateKind.NO_TRADE.value: "NO_TRADE",
    AdaptiveCandidateKind.WAIT.value: "WAIT",
    AdaptiveCandidateKind.PROPOSE.value: "PROPOSE",
    AdaptiveCandidateKind.WAIT_OUT.value: "WAIT",
    AdaptiveCandidateKind.RETURN.value: "WAIT",
    AdaptiveCandidateKind.REBUILD.value: "PROPOSE",
    AdaptiveCandidateKind.REBUILD_DEFER.value: "WAIT",
    AdaptiveCandidateKind.EXIT.value: "PROPOSE",
}


def _to_engine_decision(
    candidate: AdaptiveCandidateAction,
) -> StrategyDecision:
    """Convert an :class:`AdaptiveCandidateAction` to a
    :class:`StrategyDecision` for the T061 engine."""
    engine_kind = ADAPTIVE_TO_ENGINE_KIND.get(
        candidate.kind if isinstance(candidate.kind, str) else candidate.kind.value,
        "WAIT",
    )
    notes_list: list[str] = [
        f"{_NOTES_PREFIX}:KIND={candidate.kind if isinstance(candidate.kind, str) else candidate.kind.value}",
    ]
    if candidate.kind == AdaptiveCandidateKind.REBUILD_DEFER.value or (
        not isinstance(candidate.kind, str)
        and candidate.kind == AdaptiveCandidateKind.REBUILD_DEFER
    ):
        notes_list = [
            f"{_NOTES_PREFIX}:REBUILD_DEFER:cost_ratio={candidate.cost_ratio_q64_64}",
        ]
    if candidate.position_reassessment:
        notes_list.append(f"{_NOTES_PREFIX}:POSITION_REASSESSMENT")
    if candidate.reason_code is not None:
        notes_list.append(f"{_NOTES_PREFIX}:REASON={candidate.reason_code.value}")
    notes = tuple(notes_list)
    # The T061 engine validates ``PROPOSE``; lifecycle kinds that
    # map to ``PROPOSE`` (``REBUILD`` / ``EXIT``) carry valid
    # ``tick_lower < tick_upper`` / ``liquidity > 0`` /
    # ``capital_q64_64 > 0`` payloads the candidate enforces. For
    # kinds that map to ``WAIT`` / ``NO_TRADE`` the engine
    # accepts the sentinel values; we send the candidate's tick /
    # liquidity / capital fields through unchanged so the audit
    # chain preserves the structured lifecycle signal.
    if engine_kind == "PROPOSE":
        return StrategyDecision(
            kind="PROPOSE",
            pool_key_id=candidate.pool_key_id,
            chain_id=candidate.chain_id,
            decision_time=candidate.decision_time,
            tick_lower=candidate.tick_lower,
            tick_upper=candidate.tick_upper,
            liquidity=candidate.liquidity,
            capital_q64_64=candidate.capital_q64_64,
            notes=notes + candidate.notes,
        )
    if engine_kind == "NO_TRADE":
        return StrategyDecision(
            kind="NO_TRADE",
            pool_key_id=candidate.pool_key_id,
            chain_id=candidate.chain_id,
            decision_time=candidate.decision_time,
            notes=notes + candidate.notes,
        )
    # WAIT — preserve sentinels so the audit chain records them.
    return StrategyDecision(
        kind="WAIT",
        pool_key_id=candidate.pool_key_id,
        chain_id=candidate.chain_id,
        decision_time=candidate.decision_time,
        notes=notes + candidate.notes,
    )


# ---------------------------------------------------------------------------
# Engine callback
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdaptiveStrategyCallback:
    """The engine-callback adapter the T061 engine invokes.

    The adapter is a frozen dataclass: a single instance is
    constructed at engine setup time and threaded into the
    ``BacktestEngine`` constructor. The ``__call__`` method is the
    only entry point the engine invokes; every call returns a
    fresh :class:`StrategyDecision` without mutating the engine,
    the strategy, or any shared mutable state.
    """

    strategy: AdaptiveStrategy
    mapping: EngineDecisionMapping = field(default_factory=EngineDecisionMapping)
    window_seconds: int = 300

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, AdaptiveStrategy):
            raise EngineAdapterError(
                f"AdaptiveStrategyCallback.strategy: must be AdaptiveStrategy, "
                f"got {type(self.strategy).__name__}"
            )
        if not isinstance(self.window_seconds, int) or isinstance(self.window_seconds, bool):
            raise EngineAdapterError(
                f"AdaptiveStrategyCallback.window_seconds: must be int, "
                f"got {type(self.window_seconds).__name__}"
            )
        if self.window_seconds <= 0:
            raise EngineAdapterError(
                f"AdaptiveStrategyCallback.window_seconds: must be "
                f"positive, got {self.window_seconds}"
            )

    def __call__(self, request: StrategyDecisionRequest) -> StrategyDecision:
        """Project the request through the strategy and return an engine decision."""
        if not isinstance(request, StrategyDecisionRequest):
            raise InvalidEngineRequestError(
                f"AdaptiveStrategyCallback: request must be "
                f"StrategyDecisionRequest, got {type(request).__name__}"
            )
        admission = project_admission_snapshot(request=request)
        market = project_market_snapshot(
            request=request,
            window_seconds=self.window_seconds,
        )
        portfolio = project_portfolio_snapshot(request=request)
        candidate = self.strategy.evaluate(
            admission=admission,
            market=market,
            portfolio=portfolio,
            visible_events=request.visible_events,
            decision_time=request.decision_time,
        )
        return _to_engine_decision(candidate)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "ADAPTER_VERSION",
    "ADAPTIVE_TO_ENGINE_KIND",
    "AdaptiveStrategyCallback",
    "EngineAdapterError",
    "EngineDecisionMapping",
    "InvalidEngineRequestError",
    "compute_five_minute_return_q64_64",
    "project_admission_snapshot",
    "project_market_snapshot",
    "project_portfolio_snapshot",
]
