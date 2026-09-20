"""Shared fixtures for T104 fee-growth surface tests.

The fixtures build heterogeneous event sequences for one static-fee
V4 pool (the reference pool from the T039 / T040 / T041 fixture
family) and reconstruct the tick-liquidity state with the T041
entry point. The fixture event sequences are deliberately
hand-crafted so each acceptance boundary the contract enumerates
maps to a specific test sequence:

- a window with a single swap (single-swap window);
- a window with multiple swaps at varying price paths
  (multi-crossing, price-reversal);
- a window whose bounds sit exactly on initialized ticks;
- a window that begins or ends mid-price-movement;
- a pool whose window contains no swaps (zero-swap window);
- a range entirely outside the traded price path;
- a range containing no initialized tick;
- a range spanning the full V4 tick domain.

The fixtures are intentionally narrow — the framework's fee-growth
math is unit-tested at the boundary, and the T104 surface tests
exercise the *artifact* on top of it.
"""

from __future__ import annotations

from typing import Any

from robinhood_lp.protocol import (
    Address,
    ChainId,
    Currency,
    PoolId,
    PoolKey,
)
from robinhood_lp.replay.ticks import (
    ReconstructedPoolTickState,
    reconstruct_tick_liquidity,
)
from robinhood_lp.storage.schema import (
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    SwapLogRecord,
)

# ---------------------------------------------------------------------------
# Reference constants
# ---------------------------------------------------------------------------

CHAIN: ChainId = ChainId(4663)

# Static-fee pool (3000 pips fee = 0.3 %, 60 tick spacing, no hooks).
POOL_KEY: PoolKey = PoolKey(
    currency0=Currency.from_hex("0x" + "11" * 20),
    currency1=Currency.from_hex("0x" + "22" * 20),
    fee=3000,
    tick_spacing=60,
    hooks=Address.zero(),
)
POOL_ID: PoolId = POOL_KEY.to_pool_id()

POOL_MANAGER: Address = Address.from_hex("0x" + "44" * 20)
SENDER: Address = Address.from_hex("0x" + "11" * 20)


# ---------------------------------------------------------------------------
# Deterministic per-block hash helper
# ---------------------------------------------------------------------------


def _hash_for_block(seed: int) -> int:
    return (seed * 0x0101010101010101_0101010101010101_0101010101010101_0101010101010101) & (
        (1 << 256) - 1
    )


def _tx_hash(seed: int) -> int:
    return (seed * 0xDEADBEEFCAFEBABE_DEADBEEFCAFEBABE_DEADBEEFCAFEBABE_DEADBEEFCAFEBABE) & (
        (1 << 256) - 1
    )


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------


def _acq(*, request_from: int, request_to: int) -> Any:
    from robinhood_lp.storage.schema import AcquisitionProvenance

    return AcquisitionProvenance(
        endpoint_alias="robinhood_public",
        retrieval_time="2026-09-17T00:00:00+00:00",
        request_from_block=request_from,
        request_to_block=request_to,
        http_batch_size=None,
        http_batch_position=None,
        request_attempt=None,
    )


def make_initialize(
    *,
    pool_id: PoolId,
    block_number: int,
) -> InitializeLogRecord:
    return InitializeLogRecord(
        chain_id=CHAIN,
        pool_id=pool_id,
        block_number=block_number,
        block_hash=_hash_for_block(block_number),
        transaction_hash=_tx_hash(0xA0 + block_number),
        transaction_index=0,
        log_index=0,
        address=POOL_MANAGER,
        block_timestamp=1_700_000_000 + block_number,
        parent_hash=_hash_for_block(block_number + 1),
        acquisition=_acq(request_from=block_number, request_to=block_number),
    )


def make_modify(
    *,
    pool_id: PoolId,
    block_number: int,
    log_index: int,
    tick_lower: int,
    tick_upper: int,
    liquidity_delta: int,
    salt: int = 0,
) -> ModifyLiquidityLogRecord:
    return ModifyLiquidityLogRecord(
        chain_id=CHAIN,
        pool_id=pool_id,
        block_number=block_number,
        block_hash=_hash_for_block(block_number),
        transaction_hash=_tx_hash(0xB0 + block_number + log_index),
        transaction_index=0,
        log_index=log_index,
        address=POOL_MANAGER,
        sender=SENDER,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity_delta=liquidity_delta,
        salt=salt,
        block_timestamp=1_700_000_000 + block_number,
        parent_hash=_hash_for_block(block_number + 1),
        acquisition=_acq(request_from=block_number, request_to=block_number),
    )


def make_swap(
    *,
    pool_id: PoolId,
    block_number: int,
    log_index: int,
    sqrt_price_x96: int,
    liquidity: int,
    tick: int,
    amount0: int,
    amount1: int,
    fee: int = 3000,
) -> SwapLogRecord:
    return SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pool_id,
        block_number=block_number,
        block_hash=_hash_for_block(block_number),
        transaction_hash=_tx_hash(0xC0 + block_number + log_index),
        transaction_index=0,
        log_index=log_index,
        address=POOL_MANAGER,
        sender=SENDER,
        amount0=amount0,
        amount1=amount1,
        sqrt_price_x96=sqrt_price_x96,
        liquidity=liquidity,
        tick=tick,
        fee=fee,
        block_timestamp=1_700_000_000 + block_number,
        parent_hash=_hash_for_block(block_number + 1),
        acquisition=_acq(request_from=block_number, request_to=block_number),
    )


def make_donate(
    *,
    pool_id: PoolId,
    block_number: int,
    log_index: int,
    amount0: int,
    amount1: int,
) -> DonateLogRecord:
    return DonateLogRecord(
        chain_id=CHAIN,
        pool_id=pool_id,
        block_number=block_number,
        block_hash=_hash_for_block(block_number),
        transaction_hash=_tx_hash(0xD0 + block_number + log_index),
        transaction_index=0,
        log_index=log_index,
        address=POOL_MANAGER,
        sender=SENDER,
        amount0=amount0,
        amount1=amount1,
        block_timestamp=1_700_000_000 + block_number,
        parent_hash=_hash_for_block(block_number + 1),
        acquisition=_acq(request_from=block_number, request_to=block_number),
    )


# ---------------------------------------------------------------------------
# Standard pool tick prices for the static-fee reference pool
# ---------------------------------------------------------------------------


def sqrt_price_at_tick(tick: int) -> int:
    """Return the V4 ``sqrtPriceX96`` for ``tick`` at spacing 60.

    The function uses the exact closed-form V4 ``TickMath.getSqrtPriceAtTick``
    formula via the framework's ``protocol.math`` module so the
    fixture values match what the replayer / StateView would carry
    for the same price.
    """
    from robinhood_lp.protocol.math import get_sqrt_price_at_tick

    return int(get_sqrt_price_at_tick(tick))


# ---------------------------------------------------------------------------
# Event-sequence builders
# ---------------------------------------------------------------------------


def init_event(block_number: int = 1_000_000) -> InitializeLogRecord:
    return make_initialize(pool_id=POOL_ID, block_number=block_number)


def add_event(
    *,
    block_number: int,
    log_index: int,
    tick_lower: int,
    tick_upper: int,
    liquidity_delta: int,
) -> ModifyLiquidityLogRecord:
    return make_modify(
        pool_id=POOL_ID,
        block_number=block_number,
        log_index=log_index,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity_delta=liquidity_delta,
    )


# A representative active-liquidity that fits the V4 ``uint128``
# domain and produces meaningful fee-growth numbers at the
# FEE_DENOMINATOR (3_000 pips) scale. The framework does not
# interpret this value; it is picked so a single 1_000-token swap
# contributes ~3 fee-growth units of Q128 per unit of liquidity.
DEFAULT_LIQUIDITY: int = 1_000_000


def single_swap_event_sequence(
    *,
    from_block: int = 1_000_000,
    to_block: int = 1_000_020,
    bootstrap_tick: int = 0,
    add_tick_lower: int = -120,
    add_tick_upper: int = 120,
    add_block: int = 1_000_002,
    swap_tick: int = 0,
    swap_block: int = 1_000_010,
    swap_amount0: int = 1_000,
    swap_amount1: int = -2_000,
    fee_pips: int = 3000,
) -> list[Any]:
    """Build a single-swap window sequence.

    The sequence is ``Initialize`` -> ``ModifyLiquidity`` (adds
    ``DEFAULT_LIQUIDITY`` over ``[add_tick_lower, add_tick_upper]``)
    -> ``Swap`` (a single zero-for-one swap of ``swap_amount0`` /
    ``swap_amount1`` at fee ``fee_pips``). The price path is chosen
    so no initialized tick is crossed (the swap stays at
    ``swap_tick``); the active liquidity is unchanged by the swap.
    """
    events: list[Any] = []
    events.append(init_event(block_number=from_block))
    events.append(
        add_event(
            block_number=add_block,
            log_index=1,
            tick_lower=add_tick_lower,
            tick_upper=add_tick_upper,
            liquidity_delta=DEFAULT_LIQUIDITY,
        )
    )
    # ``sqrt_price_at_tick`` for the swap's tick; the swap stays at
    # ``swap_tick`` and emits the same tick in its post-swap state.
    events.append(
        make_swap(
            pool_id=POOL_ID,
            block_number=swap_block,
            log_index=2,
            sqrt_price_x96=sqrt_price_at_tick(swap_tick),
            liquidity=DEFAULT_LIQUIDITY,
            tick=swap_tick,
            amount0=swap_amount0,
            amount1=swap_amount1,
            fee=fee_pips,
        )
    )
    return events


def multi_swap_event_sequence(
    *,
    from_block: int = 1_000_000,
    to_block: int = 1_000_060,
    bootstrap_tick: int = 0,
) -> list[Any]:
    """Build a multi-crossing, price-reversal window sequence.

    The sequence is ``Initialize`` -> ``ModifyLiquidity`` (adds
    ``DEFAULT_LIQUIDITY`` over ``[-60, 60]``) -> multiple
    ``Swap`` events that drive the price through several
    reversals. Each swap is calibrated so the active liquidity
    in V4's ``liquidity`` field equals the value the
    ``reconstruct_tick_liquidity`` walker infers after the
    crossing.
    """
    events: list[Any] = []
    events.append(init_event(block_number=from_block))
    events.append(
        add_event(
            block_number=from_block + 2,
            log_index=1,
            tick_lower=-60,
            tick_upper=60,
            liquidity_delta=DEFAULT_LIQUIDITY,
        )
    )
    # First swap: zeroForOne (token0 in, amount0 > 0), drive price down
    # from tick 0 to tick -120 (crosses -60 from above to below).
    # After the crossing, active liquidity is 0 (no liquidity below
    # -60).
    events.append(
        make_swap(
            pool_id=POOL_ID,
            block_number=from_block + 4,
            log_index=2,
            sqrt_price_x96=sqrt_price_at_tick(-120),
            liquidity=0,
            tick=-120,
            amount0=2_000,
            amount1=-4_000,
            fee=3000,
        )
    )
    # Second swap: oneForZero (token1 in, amount1 > 0), drive price
    # back up from -120 to 0 (crosses -60 from below to above). The
    # active liquidity returns to DEFAULT_LIQUIDITY at the end.
    events.append(
        make_swap(
            pool_id=POOL_ID,
            block_number=from_block + 6,
            log_index=3,
            sqrt_price_x96=sqrt_price_at_tick(0),
            liquidity=DEFAULT_LIQUIDITY,
            tick=0,
            amount0=-1_000,
            amount1=2_000,
            fee=3000,
        )
    )
    # Third swap: oneForZero, drive price up from 0 to 120 (crosses
    # 60 from below to above). After crossing 60, active liquidity
    # is 0.
    events.append(
        make_swap(
            pool_id=POOL_ID,
            block_number=from_block + 8,
            log_index=4,
            sqrt_price_x96=sqrt_price_at_tick(120),
            liquidity=0,
            tick=120,
            amount0=-2_000,
            amount1=4_000,
            fee=3000,
        )
    )
    # Fourth swap: zeroForOne, drive price down from 120 to 0
    # (crosses 60 from above to below). Active liquidity returns
    # to DEFAULT_LIQUIDITY.
    events.append(
        make_swap(
            pool_id=POOL_ID,
            block_number=from_block + 10,
            log_index=5,
            sqrt_price_x96=sqrt_price_at_tick(0),
            liquidity=DEFAULT_LIQUIDITY,
            tick=0,
            amount0=1_000,
            amount1=-2_000,
            fee=3000,
        )
    )
    return events


def no_swap_event_sequence(
    *,
    from_block: int = 1_000_000,
    to_block: int = 1_000_020,
) -> list[Any]:
    """Build a window with zero swap events.

    The sequence is ``Initialize`` -> ``ModifyLiquidity``. No swap
    is emitted; the surface's ``swap_snapshots`` is empty and the
    fee-growth global is zero throughout.
    """
    events: list[Any] = []
    events.append(init_event(block_number=from_block))
    events.append(
        add_event(
            block_number=from_block + 2,
            log_index=1,
            tick_lower=-60,
            tick_upper=60,
            liquidity_delta=DEFAULT_LIQUIDITY,
        )
    )
    return events


def bounds_on_initialized_ticks_sequence(
    *,
    from_block: int = 1_000_000,
    to_block: int = 1_000_020,
) -> list[Any]:
    """Build a window whose bounds sit exactly on initialized ticks.

    The sequence adds liquidity over ``[-120, -60]`` and ``[60,
    120]`` (two disjoint ranges) and adds another range ``[-120,
    120]`` so the active liquidity at tick 0 is the third
    addition. A swap at tick 0 then exercises a range ``[-120,
    120]`` whose bounds sit exactly on initialized ticks.
    """
    events: list[Any] = []
    events.append(init_event(block_number=from_block))
    events.append(
        add_event(
            block_number=from_block + 2,
            log_index=1,
            tick_lower=-120,
            tick_upper=-60,
            liquidity_delta=DEFAULT_LIQUIDITY,
        )
    )
    events.append(
        add_event(
            block_number=from_block + 4,
            log_index=2,
            tick_lower=60,
            tick_upper=120,
            liquidity_delta=DEFAULT_LIQUIDITY,
        )
    )
    events.append(
        add_event(
            block_number=from_block + 6,
            log_index=3,
            tick_lower=-120,
            tick_upper=120,
            liquidity_delta=DEFAULT_LIQUIDITY,
        )
    )
    events.append(
        make_swap(
            pool_id=POOL_ID,
            block_number=from_block + 8,
            log_index=4,
            sqrt_price_x96=sqrt_price_at_tick(0),
            liquidity=DEFAULT_LIQUIDITY,
            tick=0,
            amount0=1_000,
            amount1=-2_000,
            fee=3000,
        )
    )
    return events


def reconstruct_state(events: list[Any]) -> ReconstructedPoolTickState:
    """Reconstruct the tick state for ``events`` (T041 entry point)."""
    return reconstruct_tick_liquidity(
        chain_id=int(CHAIN.value),
        pool_id=int(POOL_ID.value),
        pool_key=POOL_KEY,
        events=events,
        bootstrap_tick=0,
    )


__all__ = [
    "CHAIN",
    "DEFAULT_LIQUIDITY",
    "POOL_ID",
    "POOL_KEY",
    "POOL_MANAGER",
    "SENDER",
    "add_event",
    "bounds_on_initialized_ticks_sequence",
    "init_event",
    "make_donate",
    "make_initialize",
    "make_modify",
    "make_swap",
    "multi_swap_event_sequence",
    "no_swap_event_sequence",
    "reconstruct_state",
    "single_swap_event_sequence",
    "sqrt_price_at_tick",
]
