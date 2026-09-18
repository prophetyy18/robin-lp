"""Shared fixtures for T042 StateView comparison tests.

The fixtures build two heterogeneous pools (the re-acquired ZZZ/USDG
reference pool and the Owner-pinned second pool) with synthetic
event sequences whose StateView golden values are derived from the
events the replay / reconstruction would emit. Tests inject the
golden values into a deterministic :class:`BlockPinnedStateViewCall`
so the runner can validate exact integer field agreement without
contacting any real RPC.
"""

from __future__ import annotations

import hashlib
from typing import Any

from robinhood_lp.protocol import (
    Address,
    ChainId,
    Currency,
    PoolId,
    PoolKey,
)
from robinhood_lp.replay.protocol_fee import pack_protocol_fee
from robinhood_lp.replay.ticks import (
    ReconstructedPoolTickState,
    reconstruct_tick_liquidity,
)
from robinhood_lp.storage.schema import (
    AcquisitionProvenance,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    SwapLogRecord,
)

# ---------------------------------------------------------------------------
# Reference and second-pool constants
# ---------------------------------------------------------------------------

CHAIN: ChainId = ChainId(4663)

# Re-acquired ZZZ/USDG reference pool (T036 / T038).
REFERENCE_POOL_KEY: PoolKey = PoolKey(
    currency0=Currency.from_hex("0x5fc5360d0400a0fd4f2af552add042d716f1d168"),
    currency1=Currency.from_hex("0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a"),
    fee=28_001,
    tick_spacing=280,
    hooks=Address.zero(),
)
REFERENCE_POOL_ID: PoolId = REFERENCE_POOL_KEY.to_pool_id()

# Owner-pinned second pool. The hook contract address is the lookup
# signal T038 pinned; the on-chain ``PoolKey`` is unverified in this
# fixture (the fixture carries a representative V4 ``PoolKey`` whose
# currency0/currency1 satisfy V4's ordering invariant and whose hook
# address equals the Owner-pinned signal). The fixture is for
# per-pool-validation-isolation testing only.
SECOND_POOL_KEY: PoolKey = PoolKey(
    currency0=Currency.from_hex("0x" + "11" * 20),
    currency1=Currency.from_hex("0x" + "22" * 20),
    fee=3_000,
    tick_spacing=60,
    hooks=Address.from_hex("0xEd50bDeeA8aDC232f159486192a4157281D722ff"),
)
SECOND_POOL_ID: PoolId = SECOND_POOL_KEY.to_pool_id()

# PoolManager address used as the log ``address`` field. The
# fixtures do not exercise the wire; a synthetic placeholder suffices.
POOL_MANAGER: Address = Address.from_hex("0x" + "44" * 20)
SENDER: Address = Address.from_hex("0x" + "11" * 20)


# ---------------------------------------------------------------------------
# Deterministic per-block hash helper
# ---------------------------------------------------------------------------


def _hash_for_block(seed: int) -> int:
    """Deterministic 32-byte hash derived from ``seed``."""
    return (seed * 0x0101010101010101_0101010101010101_0101010101010101_0101010101010101) & (
        (1 << 256) - 1
    )


def _tx_hash(seed: int) -> int:
    """Deterministic 32-byte hash derived from ``seed``."""
    return (seed * 0xDEADBEEFCAFEBABE_DEADBEEFCAFEBABE_DEADBEEFCAFEBABE_DEADBEEFCAFEBABE) & (
        (1 << 256) - 1
    )


# ---------------------------------------------------------------------------
# Acquire / replay / reconstruct helpers
# ---------------------------------------------------------------------------


def _acq(*, request_from: int, request_to: int) -> AcquisitionProvenance:
    return AcquisitionProvenance(
        endpoint_alias="robinhood_public",
        retrieval_time="2026-09-17T00:00:00+00:00",
        request_from_block=request_from,
        request_to_block=request_to,
        http_batch_size=None,
        http_batch_position=None,
        request_attempt=None,
    )


def make_initialize_record(
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


def make_modify_record(
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


def make_swap_record(
    *,
    pool_id: PoolId,
    block_number: int,
    log_index: int,
    sqrt_price_x96: int,
    liquidity: int,
    tick: int,
    amount0: int = -1_000,
    amount1: int = 2_000,
    fee: int = 28_001,
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


# ---------------------------------------------------------------------------
# Synthetic event sequences
# ---------------------------------------------------------------------------

#: Reference-pool event sequence: init, adds liquidity, swaps
#: produce the deterministic state the StateView golden values
#: encode.
REFERENCE_SEQUENCE_BLOCKS: list[int] = [1_000_000, 1_000_003, 1_000_005]


def reference_event_sequence() -> list[object]:
    """Build the reference-pool event sequence used by the tests.

    The sequence is intentionally narrow: one ``Initialize`` at
    block 1_000_000, one ``ModifyLiquidity`` at block 1_000_003
    (the active liquidity is the active ``liquidity`` at the next
    swap), and one ``Swap`` at block 1_000_005 (the swap emits the
    post-swap state). The ``sqrtPriceX96`` / ``tick`` / ``liquidity``
    values are picked so the replay's terminal state and the
    StateView golden values are easy to verify by inspection.
    """
    events: list[object] = []
    events.append(make_initialize_record(pool_id=REFERENCE_POOL_ID, block_number=1_000_000))
    events.append(
        make_modify_record(
            pool_id=REFERENCE_POOL_ID,
            block_number=1_000_003,
            log_index=1,
            tick_lower=-280 * 10,
            tick_upper=280 * 10,
            liquidity_delta=2_000_000,
        )
    )
    # The current tick (0) sits inside [-2800, 2800] so the active
    # liquidity updates to 2_000_000. The next swap is at tick 0
    # (no crossing) so the active liquidity is unchanged.
    events.append(
        make_swap_record(
            pool_id=REFERENCE_POOL_ID,
            block_number=1_000_005,
            log_index=2,
            sqrt_price_x96=79228162514264337593543950337,
            liquidity=2_000_000,
            tick=0,
            amount0=-1_000,
            amount1=2_000,
            fee=28_001,
        )
    )
    return events


def second_event_sequence() -> list[object]:
    """Build the second-pool event sequence.

    Distinct ``PoolKey`` (different fee / tickSpacing / hooks) and a
    different active-liquidity value so the per-pool validation
    isolation test can verify one pool's agreement is not silently
    used as the other pool's result.
    """
    events: list[object] = []
    events.append(make_initialize_record(pool_id=SECOND_POOL_ID, block_number=1_100_000))
    events.append(
        make_modify_record(
            pool_id=SECOND_POOL_ID,
            block_number=1_100_002,
            log_index=1,
            tick_lower=-60,
            tick_upper=60,
            liquidity_delta=5_000_000,
        )
    )
    events.append(
        make_swap_record(
            pool_id=SECOND_POOL_ID,
            block_number=1_100_004,
            log_index=2,
            sqrt_price_x96=79228162514264337593543950340,
            liquidity=5_000_000,
            tick=0,
            amount0=-2_000,
            amount1=4_000,
            fee=3_000,
        )
    )
    return events


# ---------------------------------------------------------------------------
# Golden value computation
# ---------------------------------------------------------------------------


def compute_state_view_golden(
    pool_key: PoolKey,
    pool_id: PoolId,
    events: list[object],
) -> dict[tuple[str, str, bytes, tuple[int, ...]], bytes]:
    """Compute the StateView golden values the events would emit.

    The runner uses these as the expected responses at every
    (method, block_tag, pool_id, extra_args) the comparison reads.
    The values are derived deterministically from the events; tests
    that exercise the disagreement path mutate one of these
    responses to force a per-pool mismatch.
    """

    def _slot_uint(value: int, *, bits: int) -> bytes:
        return int(value).to_bytes(32, "big")

    def _slot_int(value: int, *, bits: int) -> bytes:
        return int(value).to_bytes(32, "big", signed=True)

    def _slot_uint_to_32(value: int) -> bytes:
        return int(value).to_bytes(32, "big")

    def _slot_int24(value: int) -> bytes:
        return _slot_int(value, bits=24)

    def _slot_uint24(value: int) -> bytes:
        return int(value).to_bytes(32, "big")

    def _slot_uint128(value: int) -> bytes:
        return int(value).to_bytes(32, "big")

    def _slot_int128(value: int) -> bytes:
        return _slot_int(value, bits=128)

    responses: dict[tuple[str, str, bytes, tuple[int, ...]], bytes] = {}
    pool_id_bytes = pool_id.to_bytes()

    # Walk events, maintaining the observable StateView state.
    sqrt_price_x96 = 0
    tick = 0
    active_liquidity = 0
    protocol_fee_token0 = 0
    protocol_fee_token1 = 0
    bitmap_words_map: dict[int, int] = {}
    tick_info_map: dict[int, dict[str, int]] = {}

    last_block = 0
    for event in events:
        block = int(event.block_number)  # type: ignore[attr-defined]
        last_block = max(last_block, block)

    for event in events:
        block = int(event.block_number)  # type: ignore[attr-defined]
        block_tag = "0x" + format(block, "x")
        if isinstance(event, InitializeLogRecord):
            sqrt_price_x96 = 79228162514264337593543950336
            tick = 0
            active_liquidity = 0
        elif isinstance(event, ModifyLiquidityLogRecord):
            tick_lower = int(event.tick_lower)
            tick_upper = int(event.tick_upper)
            delta = int(event.liquidity_delta)
            # Update both ends' TickInfo + bitmap (mirrors V4:
            # both tickLower and tickUpper get ``liquidityGross += |delta|``;
            # the net is signed differently for the two ends).
            abs_delta = abs(delta)
            for tick_idx, is_upper in ((tick_lower, False), (tick_upper, True)):
                info = tick_info_map.setdefault(tick_idx, {"liquidityGross": 0, "liquidityNet": 0})
                gross_after = info["liquidityGross"] + abs_delta
                net_delta = -delta if is_upper else delta
                net_after = info["liquidityNet"] + net_delta
                info["liquidityGross"] = gross_after
                info["liquidityNet"] = net_after
                if gross_after == 0:
                    tick_info_map.pop(tick_idx, None)
                    # Clear the bitmap bit.
                    compressed = tick_idx // pool_key.tick_spacing
                    word_pos = compressed >> 8
                    bit_pos = compressed & 0xFF
                    word = bitmap_words_map.get(word_pos, 0)
                    word &= ~(1 << bit_pos)
                    if word == 0:
                        bitmap_words_map.pop(word_pos, None)
                    else:
                        bitmap_words_map[word_pos] = word
                else:
                    compressed = tick_idx // pool_key.tick_spacing
                    word_pos = compressed >> 8
                    bit_pos = compressed & 0xFF
                    word = bitmap_words_map.get(word_pos, 0)
                    word |= 1 << bit_pos
                    bitmap_words_map[word_pos] = word
            # Active-liquidity view: update only when current tick
            # sits inside the modified range (mirrors V4).
            if tick_lower <= tick < tick_upper:
                active_liquidity = active_liquidity + delta
        elif isinstance(event, SwapLogRecord):
            sqrt_price_x96 = int(event.sqrt_price_x96)
            tick = int(event.tick)
            active_liquidity = int(event.liquidity)
        else:
            raise ValueError(f"compute_state_view_golden: unsupported event {type(event).__name__}")
        # Capture the StateView view at this block (the per-height
        # read list schedules at the block numbers the events use,
        # so this is the exact wire the runner compares against).
        protocol_fee_packed = pack_protocol_fee(protocol_fee_token0, protocol_fee_token1)
        slot_0_payload = (
            _slot_uint160(sqrt_price_x96)
            + _slot_int24(tick)
            + _slot_uint24(int(pool_key.fee))
            + _slot_uint24(protocol_fee_packed)
        )
        responses[("getSlot0", block_tag, pool_id_bytes, ())] = slot_0_payload
        responses[("getLiquidity", block_tag, pool_id_bytes, ())] = _slot_uint128(active_liquidity)
        responses[("getFeeGrowthGlobals", block_tag, pool_id_bytes, ())] = _slot_uint(
            0, bits=256
        ) + _slot_uint(0, bits=256)
        for word_pos, word in bitmap_words_map.items():
            responses[
                (
                    "getTickBitmap",
                    block_tag,
                    pool_id_bytes,
                    (int(word_pos),),
                )
            ] = _slot_uint(word, bits=256)
        for tick_idx, info in tick_info_map.items():
            tick_lower_int = int(tick_idx)
            responses[
                (
                    "getTickLiquidity",
                    block_tag,
                    pool_id_bytes,
                    (tick_lower_int,),
                )
            ] = _slot_uint128(info["liquidityGross"]) + _slot_int128(info["liquidityNet"])
            responses[
                (
                    "getTickInfo",
                    block_tag,
                    pool_id_bytes,
                    (tick_lower_int,),
                )
            ] = (
                _slot_uint128(info["liquidityGross"])
                + _slot_int128(info["liquidityNet"])
                + _slot_uint(0, bits=256)
                + _slot_uint(0, bits=256)
            )
            responses[
                (
                    "getTickFeeGrowthOutside",
                    block_tag,
                    pool_id_bytes,
                    (tick_lower_int,),
                )
            ] = _slot_uint(0, bits=256) + _slot_uint(0, bits=256)
        # T042 records ``initialized`` is ``True`` after the first
        # ``Initialize`` event; the comparison surfaces it via
        # the replay's ``initialized`` flag at this height.
    return responses


def _slot_uint160(value: int) -> bytes:
    return int(value).to_bytes(32, "big")


# ---------------------------------------------------------------------------
# Reconstruction helper
# ---------------------------------------------------------------------------


def reconstruct_pool_state(
    *,
    pool_id: PoolId,
    pool_key: PoolKey,
    events: list[object],
) -> ReconstructedPoolTickState:
    """Run :func:`reconstruct_tick_liquidity` over the events.

    The events must include an ``Initialize`` event with a valid
    pool_id (the T041 reconstruction enforces ``UnknownPoolError``
    for cross-pool records). The bootstrap tick is the initial
    tick the V4 ``initialize()`` call returns; the fixture uses
    ``0`` as a representative.
    """
    return reconstruct_tick_liquidity(
        chain_id=int(CHAIN.value),
        pool_id=int(pool_id.value),
        pool_key=pool_key,
        events=events,
        bootstrap_tick=0,
    )


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def per_height_reads_for_reference_pool() -> tuple[tuple[int, tuple[Any, ...]], ...]:
    """Build the recorded per-height read list for the reference pool."""
    from robinhood_lp.replay.state_comparison import (
        STATE_VIEW_METHOD_GET_FEE_GROWTH_GLOBALS,
        STATE_VIEW_METHOD_GET_LIQUIDITY,
        STATE_VIEW_METHOD_GET_SLOT_0,
        STATE_VIEW_METHOD_GET_TICK_BITMAP,
        STATE_VIEW_METHOD_GET_TICK_INFO,
        STATE_VIEW_METHOD_GET_TICK_LIQUIDITY,
        StateViewReadSpec,
    )

    return (
        (
            1_000_003,
            (
                StateViewReadSpec(method=STATE_VIEW_METHOD_GET_SLOT_0),
                StateViewReadSpec(method=STATE_VIEW_METHOD_GET_LIQUIDITY),
                StateViewReadSpec(
                    method=STATE_VIEW_METHOD_GET_TICK_INFO,
                    extra_args=(-280 * 10,),
                ),
            ),
        ),
        (
            1_000_005,
            (
                StateViewReadSpec(method=STATE_VIEW_METHOD_GET_SLOT_0),
                StateViewReadSpec(method=STATE_VIEW_METHOD_GET_LIQUIDITY),
                StateViewReadSpec(
                    method=STATE_VIEW_METHOD_GET_TICK_BITMAP,
                    extra_args=(0,),
                ),
                StateViewReadSpec(
                    method=STATE_VIEW_METHOD_GET_TICK_LIQUIDITY,
                    extra_args=(-280 * 10,),
                ),
                StateViewReadSpec(method=STATE_VIEW_METHOD_GET_FEE_GROWTH_GLOBALS),
            ),
        ),
    )


def per_height_reads_for_second_pool() -> tuple[tuple[int, tuple[Any, ...]], ...]:
    """Build the recorded per-height read list for the second pool."""
    from robinhood_lp.replay.state_comparison import (
        STATE_VIEW_METHOD_GET_LIQUIDITY,
        STATE_VIEW_METHOD_GET_SLOT_0,
        STATE_VIEW_METHOD_GET_TICK_INFO,
        StateViewReadSpec,
    )

    return (
        (
            1_100_002,
            (
                StateViewReadSpec(method=STATE_VIEW_METHOD_GET_SLOT_0),
                StateViewReadSpec(method=STATE_VIEW_METHOD_GET_LIQUIDITY),
            ),
        ),
        (
            1_100_004,
            (
                StateViewReadSpec(method=STATE_VIEW_METHOD_GET_SLOT_0),
                StateViewReadSpec(method=STATE_VIEW_METHOD_GET_LIQUIDITY),
                StateViewReadSpec(
                    method=STATE_VIEW_METHOD_GET_TICK_INFO,
                    extra_args=(-60,),
                ),
            ),
        ),
    )


def hash_partition_id(partition_id: str) -> str:
    """Stable hash of a partition id (used by T037 reconciliation evidence)."""
    return "0x" + hashlib.sha256(partition_id.encode("utf-8")).hexdigest()


__all__ = [
    "CHAIN",
    "POOL_MANAGER",
    "REFERENCE_POOL_ID",
    "REFERENCE_POOL_KEY",
    "REFERENCE_SEQUENCE_BLOCKS",
    "SENDER",
    "SECOND_POOL_ID",
    "SECOND_POOL_KEY",
    "compute_state_view_golden",
    "hash_partition_id",
    "make_initialize_record",
    "make_modify_record",
    "make_swap_record",
    "per_height_reads_for_reference_pool",
    "per_height_reads_for_second_pool",
    "reconstruct_pool_state",
    "reference_event_sequence",
    "second_event_sequence",
]
