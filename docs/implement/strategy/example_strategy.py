"""Documentation example strategy for the T067 authoring guide.

This module lives at ``docs/implement/strategy/example_strategy.py``. It is
a complete, minimal strategy that satisfies the V1 engine-callback contract
declared by ``src/robinhood_lp/backtest/engine.py``:

- the callback receives a :class:`StrategyDecisionRequest` and returns a
  :class:`StrategyDecision`;
- it never imports RPC, storage, signing, execution or presentation code;
- it never reads wall-clock time or unseeded randomness;
- it never mutates the request, the ledger, or any shared mutable object.

Run the example directly with the project Python to see a short audit
summary on stdout:

.. code-block:: bash

    /home/lpdev/miniconda3/envs/robinhood-lp/bin/python \\
        docs/implement/strategy/example_strategy.py

The script builds a small deterministic event manifest, threads the
example through :class:`BacktestEngine` and prints one line per
``DECISION`` / ``FILL`` / ``SYSTEM`` audit event the engine emits.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from math import isqrt
from pathlib import Path
from typing import Final

# Make the ``src/`` layout importable when this file is run directly.
# The repository installs ``robinhood-lp`` in editable mode for normal
# use; this fallback lets a reader invoke the example with a plain
# ``python docs/implement/strategy/example_strategy.py`` and still
# resolve the project package without having to set ``PYTHONPATH``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_DIR = _REPO_ROOT / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from robinhood_lp.backtest.engine import (  # noqa: E402  (sys.path tweak above)
    BacktestEngine,
    RiskDecision,
    StrategyDecision,
    StrategyDecisionRequest,
    empty_position_state,
)
from robinhood_lp.backtest.events import (  # noqa: E402  (sys.path tweak above)
    BACKTEST_EVENT_VERSION,
    KIND_SHUTDOWN,
    KIND_SWAP,
    SOURCE_PRIORITY_DATA,
    SOURCE_PRIORITY_SYSTEM,
    BacktestEvent,
)
from robinhood_lp.backtest.models import (  # noqa: E402  (sys.path tweak above)
    ConstantLiquidityModel,
    DeterministicFailureModel,
    FlatGasModel,
    ModelBundle,
    StaticFeeModel,
    ZeroSlippageModel,
)
from robinhood_lp.protocol.math import get_tick_at_sqrt_price  # noqa: E402  (sys.path tweak above)

#: Fixed half-width in ticks the example proposes around the current
#: tick. ``60`` ticks correspond to roughly +/-0.6% on each side
#: (``1.0001 ** 60 - 1 ~ 0.0059``); it is a round, auditable default
#: that the guide's runnable example does not tune.
EXAMPLE_HALF_WIDTH_TICKS: Final[int] = 60

#: Liquidity the example proposes for ``PROPOSE`` decisions. ``1_000``
#: is a small positive integer that distinguishes a real mint from a
#: zero-liquidity ``WAIT``; the backtest engine validates it.
EXAMPLE_LIQUIDITY: Final[int] = 1_000

#: Capital envelope the example proposes, expressed as Q64.64 USDG.
#: ``1 << 64`` is exactly one USDG (ADR-014 §3, T049 / T053 numeraire
#: boundary).
EXAMPLE_CAPITAL_Q64_64: Final[int] = 1 << 64

#: Notes tag attached to every decision the example emits so the
#: audit chain identifies the policy without re-parsing the engine
#: configuration.
EXAMPLE_NOTES_TAG: Final[str] = "AUTHORING_GUIDE_EXAMPLE"

#: Notes tag for the open path (empty ledger on first reactive event).
EXAMPLE_NOTES_OPEN: Final[str] = "EXAMPLE_OPEN"

#: Notes tag for the in-range wait path.
EXAMPLE_NOTES_WAIT_IN_RANGE: Final[str] = "EXAMPLE_WAIT_IN_RANGE"

#: Notes tag for the rebalance path (current tick outside the ledger
#: range).
EXAMPLE_NOTES_REBALANCE: Final[str] = "EXAMPLE_REBALANCE"

#: Notes tag for the no-market-data path (visible events carry no
#: ``price_q64_64``).
EXAMPLE_NOTES_NO_MARKET_DATA: Final[str] = "EXAMPLE_NO_MARKET_DATA"


@dataclass(frozen=True, slots=True)
class TickFromCurrentPriceStrategy:
    """A minimal fixed-width strategy for the authoring guide.

    The strategy opens a symmetric ``+/-EXAMPLE_HALF_WIDTH_TICKS`` Range
    around the current tick the most recent visible price observation
    implies. Once open it returns ``WAIT`` while the current tick
    stays inside ``[tick_lower, tick_upper)``; an out-of-range tick
    returns ``PROPOSE`` so the engine rebalances around the new tick.
    A request whose visible events carry no price observation returns
    ``NO_TRADE``.

    The class is intentionally a thin policy: no regime model, no fee
    model, no parameter fitting. The reader can copy it, change one
    constant and run it through the committed engine.
    """

    pool_key_id: str
    chain_id: int
    tick_spacing: int

    def __call__(self, request: StrategyDecisionRequest) -> StrategyDecision:
        """Return the engine-callback decision for ``request``."""
        latest_price = _latest_price_q64_64(request.visible_events)
        if latest_price is None:
            return StrategyDecision(
                kind="NO_TRADE",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=(EXAMPLE_NOTES_TAG, EXAMPLE_NOTES_NO_MARKET_DATA),
            )
        current_tick = _price_q64_64_to_tick(latest_price)
        lower = _snap_down_to_spacing(current_tick - EXAMPLE_HALF_WIDTH_TICKS, self.tick_spacing)
        upper = _snap_up_to_spacing(current_tick + EXAMPLE_HALF_WIDTH_TICKS, self.tick_spacing)
        ledger = request.ledger
        if ledger.liquidity == 0:
            action_tag = EXAMPLE_NOTES_OPEN
        elif current_tick < ledger.tick_lower or current_tick >= ledger.tick_upper:
            action_tag = EXAMPLE_NOTES_REBALANCE
        else:
            return StrategyDecision(
                kind="WAIT",
                pool_key_id=self.pool_key_id,
                chain_id=self.chain_id,
                decision_time=request.decision_time,
                notes=(EXAMPLE_NOTES_TAG, EXAMPLE_NOTES_WAIT_IN_RANGE),
            )
        return StrategyDecision(
            kind="PROPOSE",
            pool_key_id=self.pool_key_id,
            chain_id=self.chain_id,
            decision_time=request.decision_time,
            tick_lower=lower,
            tick_upper=upper,
            liquidity=EXAMPLE_LIQUIDITY,
            capital_q64_64=EXAMPLE_CAPITAL_Q64_64,
            notes=(EXAMPLE_NOTES_TAG, action_tag),
        )


# ---------------------------------------------------------------------------
# Internal helpers (pure functions; no module state)
# ---------------------------------------------------------------------------


def _latest_price_q64_64(events: tuple[BacktestEvent, ...]) -> int | None:
    """Return the most recent ``price_q64_64`` from a data-priority event.

    The helper mirrors the equivalent in :mod:`robinhood_lp.strategy.baselines`
    and :mod:`robinhood_lp.strategy.adapter`; the duplication is acceptable
    because the example lives outside the strategy layer and must not import
    a private symbol from a sibling module.
    """
    latest_price: int | None = None
    latest_ts: int = -1
    for evt in events:
        if evt.source_priority != SOURCE_PRIORITY_DATA:
            continue
        if evt.kind != KIND_SWAP:
            continue
        for key, value in evt.payload:
            if key == "price_q64_64":
                if not isinstance(value, int) or isinstance(value, bool):
                    break
                if evt.timestamp >= latest_ts:
                    latest_price = value
                    latest_ts = evt.timestamp
                break
    return latest_price


def _price_q64_64_to_tick(price_q64_64: int) -> int:
    """Map a Q64.64 price ratio to the V4 tick (integer floor).

    The conversion uses :func:`math.isqrt` to move from Q64.64 to the
    V4 Q64.96 sqrt-price wire format and the V4
    :func:`get_tick_at_sqrt_price` reference to land on the tick.
    Floats never appear in the result.
    """
    if price_q64_64 <= 0:
        raise ValueError(
            f"_price_q64_64_to_tick: price_q64_64 must be positive, got {price_q64_64}"
        )
    sqrt_price_x96 = isqrt(price_q64_64 << 128)
    return get_tick_at_sqrt_price(sqrt_price_x96)


def _snap_down_to_spacing(tick: int, tick_spacing: int) -> int:
    """Return the largest tick <= ``tick`` aligned to ``tick_spacing``."""
    if tick_spacing <= 0:
        raise ValueError(
            f"_snap_down_to_spacing: tick_spacing must be positive, got {tick_spacing}"
        )
    if tick >= 0:
        return (tick // tick_spacing) * tick_spacing
    return -((-tick + tick_spacing - 1) // tick_spacing) * tick_spacing


def _snap_up_to_spacing(tick: int, tick_spacing: int) -> int:
    """Return the smallest tick >= ``tick`` aligned to ``tick_spacing``."""
    if tick_spacing <= 0:
        raise ValueError(f"_snap_up_to_spacing: tick_spacing must be positive, got {tick_spacing}")
    if tick >= 0:
        return ((tick + tick_spacing - 1) // tick_spacing) * tick_spacing
    return -((-tick) // tick_spacing) * tick_spacing


# ---------------------------------------------------------------------------
# Deterministic fixture + runnable demonstration
# ---------------------------------------------------------------------------


#: PoolKey id used by the example's committed fixture.
_EXAMPLE_POOL_KEY_ID: Final[str] = "0x" + "ab" * 32

#: Chain id used by the example's committed fixture.
_EXAMPLE_CHAIN_ID: Final[int] = 46630

#: Tick spacing used by the example's committed fixture. ``60`` matches
#: the test fixtures the comparison harness ships with.
_EXAMPLE_TICK_SPACING: Final[int] = 60

#: Price the example's committed fixture reports (1 USDG per raw
#: token, expressed as Q64.64).
_EXAMPLE_PRICE_Q64_64: Final[int] = 1 << 64


def _swap_event(*, timestamp: int, price_q64_64: int) -> BacktestEvent:
    """A reactive ``SWAP`` event carrying ``price_q64_64``."""
    return BacktestEvent(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_DATA,
        kind=KIND_SWAP,
        pool_key_id=_EXAMPLE_POOL_KEY_ID,
        chain_id=_EXAMPLE_CHAIN_ID,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(("price_q64_64", price_q64_64),),
    )


def _shutdown_event(*, timestamp: int) -> BacktestEvent:
    """A cooperative ``SHUTDOWN`` marker."""
    return BacktestEvent(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_SYSTEM,
        kind=KIND_SHUTDOWN,
        pool_key_id=_EXAMPLE_POOL_KEY_ID,
        chain_id=_EXAMPLE_CHAIN_ID,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(),
    )


def build_example_manifest() -> tuple[BacktestEvent, ...]:
    """Build the deterministic event manifest the example runs on.

    The fixture contains two ``SWAP`` events at the same price plus a
    ``SHUTDOWN`` marker. With the example policy this exercises the
    OPEN path on the first ``SWAP`` (empty ledger) and the
    WAIT-IN-RANGE path on the second ``SWAP`` (price unchanged, the
    current tick stays inside the open range).
    """
    return (
        _swap_event(timestamp=10, price_q64_64=_EXAMPLE_PRICE_Q64_64),
        _swap_event(timestamp=70, price_q64_64=_EXAMPLE_PRICE_Q64_64),
        _shutdown_event(timestamp=200),
    )


def _approve_all(_decision: StrategyDecision) -> RiskDecision:
    """A reference risk callback that always approves the strategy decision."""
    return RiskDecision(approved=True, reason_code="OK")


def build_example_engine(
    strategy: TickFromCurrentPriceStrategy,
) -> BacktestEngine:
    """Build a :class:`BacktestEngine` wired with the example strategy.

    The model bundle mirrors the small deterministic bundle the
    comparison harness uses for smoke tests: a constant liquidity
    model, a static fee model, flat gas, zero slippage and the
    always-allow failure model. Latency is zero so the fill time
    equals the decision time.
    """
    return BacktestEngine(
        version="t067.example_engine.v1",
        initial_ledger=empty_position_state(
            pool_key_id=_EXAMPLE_POOL_KEY_ID, chain_id=_EXAMPLE_CHAIN_ID
        ),
        model_bundle=ModelBundle(
            bundle_version="t067.example_models.v1",
            liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
            fee=StaticFeeModel(fee_pips_value=3_000),
            gas=FlatGasModel(gas_units_value=21_000),
            slippage=ZeroSlippageModel(),
            failure=DeterministicFailureModel(),
            latency_units=0,
        ),
        strategy_callback=strategy,
        risk_callback=_approve_all,
    )


def main() -> None:
    """Run the example through the committed engine on the committed fixture.

    The function prints one line per ``DECISION`` / ``FILL`` /
    ``SYSTEM`` audit event the engine emits, in order, so the reader
    can compare the output against the expected sequence the guide
    documents.
    """
    strategy = TickFromCurrentPriceStrategy(
        pool_key_id=_EXAMPLE_POOL_KEY_ID,
        chain_id=_EXAMPLE_CHAIN_ID,
        tick_spacing=_EXAMPLE_TICK_SPACING,
    )
    engine = build_example_engine(strategy)
    result = engine.run(build_example_manifest())
    for audit in result.audit_events:
        payload = dict(audit.payload)
        if audit.stage == "DECISION":
            kind = payload.get("decision_kind", "?")
            print(f"decision_time={audit.timestamp} kind={kind}")
        elif audit.stage == "FILL":
            print(f"fill_time={audit.timestamp} status={payload.get('fill_status', '?')}")
        elif audit.stage == "SYSTEM":
            print(f"system_time={audit.timestamp} stage={audit.stage}")


if __name__ == "__main__":
    main()
