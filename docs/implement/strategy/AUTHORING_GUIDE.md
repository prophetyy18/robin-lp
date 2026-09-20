# V1 Strategy Authoring Guide

> **Audience.** A strategy author who has only this repository, no other context.
> **Goal.** Write a strategy the event-driven backtest engine actually runs.
> **Scope.** V1 (Robinhood Chain / Uniswap V4 LP research).
>
> **Authority statement.** This guide restates rules and contracts that live in the
> cited source documents (`docs/spec/strategy/STRATEGY_ECONOMICS.md` §7, the ADRs
> the engine and contracts rest on, and task contracts `T060`, `T061`, `T062`,
> `T065`). Where this guide and a cited source disagree, the cited source wins.
> This guide does not introduce a new rule, and it does not restate a cited
> clause as the guide's own authority. Citations use the form
> `(see <source>)` next to each normative statement.
>
> **Adapter ownership.** The bridge between the engine-callback boundary this
> guide describes and the strategy-layer component boundary `STRATEGY_ECONOMICS.md`
> §7 defines is owned by `T065`. This guide does not deliver that adapter; it
> names `T065` so the reader knows where to look when the two boundaries need to
> be reconciled in code (see `todo/phases/P06-backtesting-and-strategy/T065.md`).

The guide covers five blocks. Each block ends with a citation to the source
document the rules are taken from.

1. [Callback shape](#1-callback-shape) — what the engine hands the callback
   and what the callback must return.
2. [Field semantics and units](#2-field-semantics-and-units) — what every
   field means, its scale and its point-in-time markers.
3. [Fill and information-frontier assumptions](#3-fill-and-information-frontier-assumptions) —
   what the callback may assume and what it is forbidden to assume.
4. [Strategy-layer prohibitions](#4-strategy-layer-prohibitions) — the rules
   the strategy layer must follow.
5. [Self-check](#5-self-check) — running the example through the committed
   engine on committed fixtures, and what a correct and a rejected run look like.

---

## 1. Callback shape

The engine consults the strategy through a single callback. The callback
receives a `StrategyDecisionRequest` and returns a `StrategyDecision`
(see `src/robinhood_lp/backtest/engine.py` and `src/robinhood_lp/protocol/contracts.py`).

### 1.1 Request

The request carries five fields (see
[`StrategyDecisionRequest`](../../../src/robinhood_lp/protocol/contracts.py)):

- `pool_key_id: str` — V4 `PoolKey` identity; opaque string.
- `chain_id: int` — chain identity; positive integer.
- `decision_time: int` — engine-clock event time, monotonic non-decreasing.
- `visible_events: tuple[BacktestEvent, ...]` — the information frontier the
  engine established for this decision time (see §3).
- `ledger: PositionState` — current ledger snapshot; an empty position has
  `liquidity == 0` and `in_range == False`.

### 1.2 Response

The response carries seven fields (see
[`StrategyDecision`](../../../src/robinhood_lp/protocol/contracts.py)):

- `kind: str` — exactly one of `"NO_TRADE"`, `"WAIT"`, `"PROPOSE"`.
- `pool_key_id`, `chain_id`, `decision_time` — echo back from the request.
- For `PROPOSE`: `tick_lower < tick_upper`, `liquidity > 0`,
  `capital_q64_64 > 0` are required.
- For `NO_TRADE` / `WAIT`: tick / liquidity / capital default to `0` (the
  engine accepts the sentinels).

### 1.3 Decision kinds

Three closed kinds (see
[`docs/spec/strategy/STRATEGY_ECONOMICS.md` §7](../../../docs/spec/strategy/STRATEGY_ECONOMICS.md)
and `src/robinhood_lp/backtest/engine.py`):

- `NO_TRADE` — the strategy declined to act. The engine records a `DECISION`
  audit event with `decision_kind="NO_TRADE"`; the pipeline ends here.
- `WAIT` — the strategy wants to wait for a future event. The engine records
  the decision and ends the pipeline.
- `PROPOSE` — the strategy wants to act. The engine continues to the
  risk → latency → fill stages. A risk rejection stops the pipeline; a fill
  mutates the ledger.

The kind is the only behavioural switch the engine reads. Anything else
the strategy wants to communicate (in-range, out-of-range, lifecycle) goes
into the structured `notes` tuple.

### 1.4 When the engine consults

The engine consults the callback once per *reactive* event. The default
reactive set is `{"SWAP", "MINT", "BURN"}` (see
`DEFAULT_REACT_TO_KINDS` in `src/robinhood_lp/backtest/engine.py`).
`OBSERVATION` and `TICK` events are non-reactive by default — the engine
records them as bookkeeping audit events but does not call the strategy.
A `SHUTDOWN` event stops the engine without invoking the callback.

The engine sorts the input events by `(timestamp, sequence, source_priority)`
before iterating (see `_normalise_input_events` in the same module). The
order the callback sees inside `visible_events` is the engine's order, not
the input order.

---

## 2. Field semantics and units

The strategy layer is integer-only on the protocol / valuation path
(see [`ADR-004`](../../../docs/spec/architecture/adr/ADR-004-integer-decimal-precision.md)).
Every integer-valued field is a Python `int` (`bool` is rejected by the
constructors). `float` does not appear in the request, the response, the
ledger, or the events the engine hands the callback.

### 2.1 Identity fields

- `pool_key_id` — opaque V4 `PoolKey` identity; never branches on token
  symbol, address or numeric value (see
  [`STRATEGY_ECONOMICS.md` §7](../../../docs/spec/strategy/STRATEGY_ECONOMICS.md)
  and [`ADR-014`](../../../docs/spec/architecture/adr/ADR-014-research-universe-and-numeraire.md)
  §5).
- `chain_id` — chain identity; positive integer.

### 2.2 Time fields

- `decision_time` — engine-clock event time (typically Unix seconds).
  Monotonic non-decreasing across the run.
- `BacktestEvent.timestamp` — the event's own event time.
- `BacktestEvent.observed_at` — when the data source recorded the event.
- `BacktestEvent.available_at` — when the event became available for
  decisions. An event is *visible* at `decision_time` iff
  `observed_at <= decision_time` AND `available_at <= decision_time`.

### 2.3 Event payload fields

The engine does not interpret the payload; the callback does. The
canonical payload keys today:

- `price_q64_64: int` — Q64.64 USDG-per-raw-token price ratio (fixed-point;
  `1 << 64` represents one USDG per raw token). The integer square root maps
  to the V4 Q64.96 sqrt-price (see
  [`src/robinhood_lp/protocol/math.py`](../../../src/robinhood_lp/protocol/math.py)).
- `active_liquidity: int` — uint128 active liquidity the data source recorded.
- Other keys are data-source-specific; the callback must reject unknown
  shape rather than guess (see
  [`T061` acceptance](../../../todo/phases/P06-backtesting-and-strategy/T061.md)).

### 2.4 Ledger fields (`PositionState`)

- `tick_lower`, `tick_upper` — V4 `int24` ticks; integer. Valid ticks are
  inside `[-2**23, 2**23 - 1]` and aligned to the pool's `tick_spacing`.
- `liquidity` — uint128; zero means *no position*.
- `principal_token0`, `principal_token1` — uint256 atomic token units.
- `tokens_owed0`, `tokens_owed1` — uint256 uncollected fees (atomic units).
- `in_range: bool` — whether the current tick lies in `[tick_lower, tick_upper)`.
- `last_accrual_time` — integer event time.

### 2.5 Decision response fields

- `tick_lower`, `tick_upper` — same int24 constraints as the ledger.
- `liquidity` — uint128.
- `capital_q64_64` — Q64.64 USDG capital envelope; `1 << 64` is exactly
  one USDG. The Q64.64 boundary is the project's named numeraire
  boundary (see [`ADR-014`](../../../docs/spec/architecture/adr/ADR-014-research-universe-and-numeraire.md)
  §3 and `T049` / `T053`).
- `notes: tuple[str, ...]` — structured tags the callback adds so the audit
  chain can identify the policy without re-parsing engine configuration.

### 2.6 Point-in-time markers

The strategy snapshot semantics (see
[`T060` snapshots](../../../todo/phases/P06-backtesting-and-strategy/T060.md)
and `src/robinhood_lp/strategy/base.py`) require two distinct times:

- `data_time` — the moment the snapshot is *as of*.
- `availability_time` — the moment the snapshot becomes known.

A snapshot whose `availability_time` exceeds `decision_time` is *future
data* and is rejected as a `StaleSnapshotError`. The engine-level callback
surface already enforces this: events whose `available_at > decision_time`
are not visible, and the engine refuses to call a strategy with a future
event (see §3).

---

## 3. Fill and information-frontier assumptions

The engine makes the following guarantees to the callback (see
[`src/robinhood_lp/backtest/engine.py`](../../../src/robinhood_lp/backtest/engine.py),
[`T061`](../../../todo/phases/P06-backtesting-and-strategy/T061.md)):

- **Visible events only.** The `visible_events` tuple contains only events
  whose `observed_at <= decision_time` AND `available_at <= decision_time`.
  The order is the engine's sorted order
  `(timestamp, sequence, source_priority)`.
- **No future data.** An attempt to read an event whose `available_at >
  decision_time` is recorded as a `STATUS_FUTURE_DATA_VIOLATION` audit event
  and the engine refuses to use the future event (see
  `_normalise_input_events` and `FutureDataViolation` in the same module).
- **Fills are the engine's.** The callback returns an *intent* (`PROPOSE`).
  The engine computes the fill time as `decision_time + latency_units`,
  locates the first visible data event at or after the fill time, applies
  the failure model and produces the `FILL` audit event. The fill price is
  the price of that data event — never the trigger price — unless the
  declared model proves availability (see
  `_find_fill_data` in the same module).
- **Latency is the engine's.** `latency_units` is supplied by the model
  bundle, not by the strategy.
- **Costs are the engine's.** Gas (`gas_units`), fee (`fee_pips`), slippage
  (`impact_bps`) and MEV/tax are all computed by the model bundle.
- **Failures are the engine's.** The `FailureModel` classifies the outcome
  as `FILLED` / `PARTIAL` / `DELAYED` / `REJECTED`. The audit chain
  records the classification; rejected / failed / delayed fills are
  visible, never hidden (see `T061` must-not).

What this *forbids* an author from assuming:

- The callback may **not** assume the fill price equals the trigger price.
- The callback may **not** assume its `PROPOSE` will be filled.
- The callback may **not** assume the ledger will be mutated.
- The callback may **not** access events the engine has not presented.
- The callback may **not** rely on `time`, `datetime`, `random` or any
  wall-clock / unseeded source for the decision (see
  [`ADR-006`](../../../docs/spec/architecture/adr/ADR-006-dependency-direction.md)
  and `T060`).

---

## 4. Strategy-layer prohibitions

The strategy layer is a pure consumer of timestamped state
(see [`ADR-006`](../../../docs/spec/architecture/adr/ADR-006-dependency-direction.md),
[`T060`](../../../todo/phases/P06-backtesting-and-strategy/T060.md),
[`STRATEGY_ECONOMICS.md` §7](../../../docs/spec/strategy/STRATEGY_ECONOMICS.md),
and `src/robinhood_lp/strategy/base.py`). The prohibitions are:

- **No RPC, storage, signing or execution imports.** The strategy layer
  depends only on the protocol / domain layer and the stdlib. A
  committed dependency test
  (`assert_strategy_layer_is_pure` in `src/robinhood_lp/strategy/base.py`)
  walks the live module graph and rejects any sibling-layer import
  (see [`ADR-006`](../../../docs/spec/architecture/adr/ADR-006-dependency-direction.md)
  and `T060`).
- **No ledger / state mutation.** A strategy returns a decision; the engine
  mutates the ledger. Strategies never mutate `request`, the ledger, or
  any shared mutable object. The strategy is a pure function from
  `(request) → decision` (see
  [`T060` must-not](../../../todo/phases/P06-backtesting-and-strategy/T060.md)).
- **No wall-clock time, no unseeded randomness.** Time and randomness
  come from the engine-supplied context, not from `time.time()`,
  `datetime.now()`, or `random.random()` (see `T060` and
  [`ADR-006`](../../../docs/spec/architecture/adr/ADR-006-dependency-direction.md)).
- **No future observation.** The `visible_events` tuple is the information
  frontier. Reading events outside it is a future-data violation
  (see §3 and `T061`).
- **No bypassing the risk gateway.** The engine consults the risk callback
  after every `PROPOSE`; the strategy cannot skip this step. The risk
  verdict is supplied by the orchestrator, not by the strategy (see
  `RiskDecision` and `risk_callback` in `src/robinhood_lp/backtest/engine.py`).
- **No emitting a position directly.** A `StrategyDecision` carries
  `tick_lower` / `tick_upper` / `liquidity` / `capital_q64_64`; it does not
  emit a transaction, a signed payload, a transaction hash, or an order
  object (see `T060` and
  [`STRATEGY_ECONOMICS.md` §3](../../../docs/spec/strategy/STRATEGY_ECONOMICS.md)).
  Signing, broadcasting and bundle submission belong to Phase 9
  (T090–T095), gated by the documented promotion route.
- **No labelling a model as the activity default.** Per
  [`ADR-014`](../../../docs/spec/architecture/adr/ADR-014-research-universe-and-numeraire.md)
  §5, a trained model must satisfy the same contract as a rule, must not
  alter the USDG ledger / candidate action / risk / execution semantics,
  must not gain paper or live authority, and must not become the active
  default. The model's verdict is the LP economic result the event-driven
  engine produces, not a stand-alone prediction metric.
- **The naming rule.** A component name never denotes an output; an output
  name never denotes a component (see
  [`STRATEGY_ECONOMICS.md` §7](../../../docs/spec/strategy/STRATEGY_ECONOMICS.md)
  and `T060`). `RegimeModel` is a *component* (the substitutable
  interface); `RegimeAssessment` is an *output* (the versioned value the
  component returns). Renaming a component to end with `Assessment`, or
  an output to end with `Model`, is a contract break.

---

## 5. Self-check

The committed example at
[`example_strategy.py`](./example_strategy.py) is the canonical "first run"
for a new author. The example satisfies every block above:

- It implements the documented callback shape
  (`StrategyDecisionRequest` → `StrategyDecision`).
- It uses only the engine's integer-only fields and units.
- It reads only `visible_events` and the ledger; it does not read
  wall-clock time or unseeded randomness, and it does not mutate state.
- It returns `NO_TRADE` / `WAIT` / `PROPOSE` from the closed kind set
  and never emits a position directly.

### 5.1 Run the example directly

From the repository root, with the project Python:

```bash
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python \
    docs/implement/strategy/example_strategy.py
```

A correct run prints a short, deterministic audit summary:

```
system_time=10 stage=SYSTEM
decision_time=10 kind=PROPOSE
fill_time=10 status=FILL_FILLED
decision_time=70 kind=WAIT
system_time=200 stage=SYSTEM
```

The four lines are: the engine's `SYSTEM` init event, the first
`DECISION` (the example opens a fixed-width range around the current
tick on the first reactive event), the `FILL` audit event that follows
(price, fee, gas, impact), the second `DECISION` (the example waits
because the price has not moved), and the final `SYSTEM` shutdown event
the engine emits at the `SHUTDOWN` marker.

### 5.2 Re-run the example as a committed test

```bash
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest \
    tests/test_authoring_guide_t067.py -q
```

A correct test run reports `4 passed`. The committed test asserts:

1. The guide contains every content block of §1–§5.
2. The example file is committed and importable.
3. The example runs through the committed engine on the committed
   fixture and emits the expected decision sequence (`PROPOSE`, `WAIT`).
4. The example's `__call__` returns the documented kinds for the
   documented input shapes (no-data, in-range, rebalance, open).

The test also extracts the Python fenced block in §5.3 below and asserts
it is byte-identical to the committed example. If the guide and the
example drift apart, the test fails — a guide whose commands or quoted
example have gone stale is reported as a regression, not as a hint.

### 5.3 What the example looks like in full

The committed example is the source of truth for the code below. If the
quoted block and `example_strategy.py` differ, the example file wins and
the test fails.

```python
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
```

### 5.4 What a rejected run looks like

The example is constructed so a correct run always succeeds. A rejected
run shows up in one of three failure modes:

- **`FutureDataViolation`** — the manifest contained an event whose
  `available_at > decision_time`. The example does not include such an
  event; this is the failure mode if a contributor adds a future
  timestamp by mistake. The engine refuses to fill (see
  `_find_fill_data` in `src/robinhood_lp/backtest/engine.py`).
- **`BacktestEventError`** — a malformed event (unknown `source_priority`,
  unknown `kind`, `available_at < observed_at`, etc.). The example does
  not include such an event; this is the failure mode if the manifest
  helper is corrupted.
- **`pytest` reports a failing assertion** — the test
  (`tests/test_authoring_guide_t067.py`) extracts the §5.3 fenced block
  and asserts it is byte-identical to `example_strategy.py`. A guide
  whose quoted example has drifted from the file fails the assertion,
  with the diff reported by pytest.

Each failure mode names the location of the violation: the engine-level
failure modes point to the offending event's `event_id` and timestamps;
the test failure points to the specific assertion that failed.

---

## Citations summary

Every normative statement in this guide cites one of:

- [`docs/spec/strategy/STRATEGY_ECONOMICS.md`](../../../docs/spec/strategy/STRATEGY_ECONOMICS.md)
  — V1 USDG-first LP strategy economics, including the component /
  output naming rule (§7) and the engine-callback boundary (§7).
- [`docs/spec/architecture/adr/ADR-004-integer-decimal-precision.md`](../../../docs/spec/architecture/adr/ADR-004-integer-decimal-precision.md)
  — integer / decimal precision policy: `float` is forbidden on the
  protocol / valuation path.
- [`docs/spec/architecture/adr/ADR-006-dependency-direction.md`](../../../docs/spec/architecture/adr/ADR-006-dependency-direction.md)
  — dependency direction between layers; the strategy layer must not
  import RPC, storage, signing or execution.
- [`docs/spec/architecture/adr/ADR-014-research-universe-and-numeraire.md`](../../../docs/spec/architecture/adr/ADR-014-research-universe-and-numeraire.md)
  — research universe, numeraire hierarchy, and the execution boundary
  (Q64.64 USDG boundary, model-as-interface rule in §5).
- [`src/robinhood_lp/backtest/engine.py`](../../../src/robinhood_lp/backtest/engine.py)
  — the event-driven backtest engine; the boundary a backtest actually
  crosses today (per `STRATEGY_ECONOMICS.md` §7).
- [`src/robinhood_lp/protocol/contracts.py`](../../../src/robinhood_lp/protocol/contracts.py)
  — the `StrategyDecisionRequest` / `StrategyDecision` dataclasses and
  the closed `kind` vocabulary.
- [`todo/phases/P06-backtesting-and-strategy/T060.md`](../../../todo/phases/P06-backtesting-and-strategy/T060.md)
  — strategy contracts: snapshots, components, outputs, naming rule,
  layer-purity duty.
- [`todo/phases/P06-backtesting-and-strategy/T061.md`](../../../todo/phases/P06-backtesting-and-strategy/T061.md)
  — event-driven backtest engine: information frontier, decision →
  risk → latency → fill pipeline, model-bundle separation.
- [`todo/phases/P06-backtesting-and-strategy/T062.md`](../../../todo/phases/P06-backtesting-and-strategy/T062.md)
  — baseline strategies: pool-agnostic, parameterised, auditable.
- [`todo/phases/P06-backtesting-and-strategy/T065.md`](../../../todo/phases/P06-backtesting-and-strategy/T065.md)
  — adapter owner for the bridge between the engine-callback boundary
  and the strategy-layer component boundary.
