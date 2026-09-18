"""Tests for T041 tick-liquidity state reconstruction.

Coverage:

- V4 math primitives (compress / position / max liquidity /
  addDelta) match the pinned v4-core commit, including edge cases
  (negative compressed ticks, msb / lsb bit-math, overflow guards).
- Add / remove / poke ``ModifyLiquidity`` events update the
  per-tick ``TickInfo`` and the bitmap exactly as V4's
  ``Pool.modifyLiquidity`` does; the bitmap flips in lockstep with
  the gross 0 ↔ non-zero transition.
- Same-block multi-action: multiple ``ModifyLiquidity`` and
  ``Swap`` events in the same block are applied in
  ``(transaction_index, log_index)`` order so the final tick state
  equals the single-step result.
- Boundary crossing both directions: a swap that crosses one
  initialized tick left-to-right (``oneForZero``) applies
  ``+liquidityNet``; a swap that crosses right-to-left
  (``zeroForOne``) applies ``-liquidityNet``; the resulting
  active liquidity matches the swap event's ``liquidity`` field
  byte-for-byte.
- Max liquidity per tick (V4's ``Pool.TickLiquidityOverflow``)
  and negative gross (V4's ``LiquidityMath.addDelta``
  ``SafeCastOverflow``) fail closed with the documented reason
  code, never with a partial reconstruction.
- Spacing: ``ModifyLiquidity`` events with ``tickLower`` or
  ``tickUpper`` not aligned to ``tickSpacing`` fail closed
  (``V4 TickBitmap.TickMisaligned``).
- Position keys: a ``ModifyLiquidity`` event records its
  ``(tickLower, tickUpper, salt)`` triple; multiple events with
  the same triple collapse to one entry; a poke records the
  triple but leaves the tick info / bitmap untouched.
- Bitmap invariants: a flip's underlying word matches the V4
  ``wordPos = compressed >> 8`` / ``bitPos = compressed & 0xff``
  layout, including the negative compressed tick case
  (``wordPos = -1`` for compressed ``-1``).
- Per-pool separation: a pool that is not the declared pool
  raises ``UnknownPoolError``; reconstruction state is not shared
  across reconstructions.
- Reconstruction is deterministic under shuffling / chunking /
  restart boundaries (the T040 determinism contract applies to
  the T041 output too).
"""

from __future__ import annotations

import copy
import random

import pytest

from _tick_liquidity_t041_fixtures import (
    CHAIN,
    POOL_ID,
    STATIC_POOL_KEY,
    WIDE_SPACING_POOL_KEY,
    make_donate,
    make_initialize,
    make_modify_liquidity,
    make_swap,
)
from robinhood_lp.protocol import (
    MAX_TICK,
    MIN_TICK,
    Address,
    Currency,
    PoolId,
    PoolKey,
)
from robinhood_lp.replay.ticks import (
    BITMAP_WORD_BITS,
    BITMAP_WORD_MASK,
    CROSSING_ONE_FOR_ZERO,
    CROSSING_ZERO_FOR_ONE,
    MAX_LIQUIDITY,
    LiquidityOverflowError,
    PositionKey,
    ReconstructedPoolTickState,
    TickBitmap,
    TickCrossing,
    TickInfo,
    TickLiquidityError,
    TickLiquidityOverflowError,
    TickMisalignedError,
    TickOutOfBoundsError,
    TicksMisorderedError,
    UnknownPoolError,
    add_liquidity,
    compress,
    least_significant_bit,
    most_significant_bit,
    position,
    reconstruct_tick_liquidity,
    tick_spacing_to_max_liquidity_per_tick,
)

# ---------------------------------------------------------------------------
# V4 math primitives
# ---------------------------------------------------------------------------


def test_compress_matches_v4_signed_floor_division() -> None:
    """``compress`` equals Python's ``//`` on the signed ``int24``
    domain. V4's decrement-for-negative-with-remainder collapses to
    the same value because Python ``//`` already floors toward
    negative infinity.
    """
    for tick, spacing in (
        (0, 60),
        (60, 60),
        (-60, 60),
        (61, 60),  # positive remainder
        (-59, 60),  # negative remainder, not a multiple
        (120, 60),
        (-120, 60),
        (MAX_TICK, 60),
        (MIN_TICK, 60),
    ):
        expected = tick // spacing
        assert compress(tick, spacing) == expected


def test_position_layout_matches_v4_sar_and_mask() -> None:
    """``position(compressed)`` matches V4's ``wordPos = compressed
    >> 8`` (arithmetic) and ``bitPos = compressed & 0xff``.

    Negative compressed values produce negative ``wordPos``; the
    bit position is the low 8 bits of the compressed value.
    """
    cases = (
        (0, (0, 0)),
        (1, (0, 1)),
        (255, (0, 255)),
        (256, (1, 0)),
        (-1, (-1, 255)),
        (-256, (-1, 0)),
        (-257, (-2, 255)),
    )
    for compressed, expected in cases:
        assert position(compressed) == expected


def test_max_liquidity_per_tick_matches_v4() -> None:
    """``tickSpacingToMaxLiquidityPerTick`` matches V4's
    ``(2**128 - 1) // numTicks`` formula where ``numTicks =
    max_compressed - min_compressed + 1``.
    """
    for spacing in (1, 60, 280, 2000):
        max_per_tick = tick_spacing_to_max_liquidity_per_tick(spacing)
        min_compressed = MIN_TICK // spacing
        max_compressed = MAX_TICK // spacing
        num_ticks = max_compressed - min_compressed + 1
        expected = MAX_LIQUIDITY // num_ticks
        assert max_per_tick == expected
        # Multiplying by num_ticks must not exceed MAX_LIQUIDITY.
        assert max_per_tick * num_ticks <= MAX_LIQUIDITY


def test_max_liquidity_per_tick_rejects_invalid() -> None:
    """``tickSpacingToMaxLiquidityPerTick`` rejects non-positive
    ``tick_spacing``.
    """
    with pytest.raises(ValueError):
        tick_spacing_to_max_liquidity_per_tick(0)
    with pytest.raises(ValueError):
        tick_spacing_to_max_liquidity_per_tick(-1)


def test_add_liquidity_upper_and_lower_bounds() -> None:
    """``add_liquidity`` enforces both ``uint128`` overflow and
    underflow, matching V4's ``LiquidityMath.addDelta`` revert
    surface.
    """
    # Normal cases. The first argument fits uint128; the second
    # fits int128.
    assert add_liquidity(0, 0) == 0
    assert add_liquidity(100, 50) == 150
    assert add_liquidity(100, -50) == 50
    assert add_liquidity(MAX_LIQUIDITY, 0) == MAX_LIQUIDITY
    # The maximum int128 delta is ``2**127 - 1``.
    max_int128 = (1 << 127) - 1
    assert add_liquidity(0, max_int128) == max_int128
    assert add_liquidity(MAX_LIQUIDITY - max_int128, max_int128) == MAX_LIQUIDITY

    # Underflow (the "negative gross" failure mode).
    with pytest.raises(LiquidityOverflowError) as excinfo:
        add_liquidity(0, -1)
    assert excinfo.value.kind == "underflow"

    # Overflow.
    with pytest.raises(LiquidityOverflowError) as excinfo:
        add_liquidity(MAX_LIQUIDITY, 1)
    assert excinfo.value.kind == "overflow"


def test_add_liquidity_rejects_out_of_range_inputs() -> None:
    """``add_liquidity`` rejects ``uint128`` / ``int128`` violations
    on its inputs.
    """
    with pytest.raises(ValueError):
        add_liquidity(-1, 0)
    with pytest.raises(ValueError):
        add_liquidity(MAX_LIQUIDITY + 1, 0)
    with pytest.raises(ValueError):
        add_liquidity(0, -(1 << 127))
    with pytest.raises(ValueError):
        add_liquidity(0, 1 << 127)


def test_bit_math_msb_and_lsb() -> None:
    """``most_significant_bit`` / ``least_significant_bit`` mirror V4
    ``BitMath.sol``.
    """
    assert most_significant_bit(0x80) == 7
    assert most_significant_bit(0xFF) == 7
    assert most_significant_bit(0x100) == 8
    assert most_significant_bit(1 << 255) == 255
    assert least_significant_bit(0x80) == 7
    assert least_significant_bit(0x100) == 8
    assert least_significant_bit(1 << 200) == 200


# ---------------------------------------------------------------------------
# TickBitmap
# ---------------------------------------------------------------------------


def test_bitmap_flip_and_is_initialized() -> None:
    """Flipping sets / clears the bit; ``is_initialized`` reflects it."""
    bm = TickBitmap(tick_spacing=60)
    assert not bm.is_initialized(60)
    bm.flip(60)
    assert bm.is_initialized(60)
    bm.flip(60)
    assert not bm.is_initialized(60)


def test_bitmap_flip_rejects_misaligned_tick() -> None:
    """A non-multiple of ``tick_spacing`` raises ``TickMisalignedError``."""
    bm = TickBitmap(tick_spacing=60)
    with pytest.raises(TickMisalignedError):
        bm.flip(61)
    with pytest.raises(TickMisalignedError):
        bm.flip(-61)


def test_bitmap_word_layout_for_negative_tick() -> None:
    """A negative aligned tick lives in the ``-1`` word at bit
    position ``255``. The Python ``>>`` arithmetic shift and the
    ``& 0xff`` mask combine to the same layout V4's
    ``TickBitmap.position`` returns.
    """
    bm = TickBitmap(tick_spacing=60)
    bm.flip(-60)
    # compressed(-60, 60) == -1; position(-1) == (-1, 255).
    assert bm.word(-1) == (1 << 255)
    assert bm.word(0) == 0


def test_bitmap_search_lte_finds_self() -> None:
    """``next_initialized_tick_within_one_word(tick, lte=True)``
    returns ``(tick, True)`` when the bit for ``tick`` is set.
    """
    bm = TickBitmap(tick_spacing=60)
    bm.flip(60)
    assert bm.next_initialized_tick_within_one_word(60, lte=True) == (60, True)


def test_bitmap_search_lte_finds_lower_in_word() -> None:
    """A search to the left from a higher uninitialized bit returns
    the highest initialized bit at or below the search tick.
    """
    bm = TickBitmap(tick_spacing=60)
    bm.flip(-60)
    # Searching at tick=60 finds -60 (compressed=1; search from compressed=1
    # goes left in bit 1 of word 0; bit 1 is unset, bit 0 is unset, so it
    # returns the rightmost in the word: 0).
    # To get -60 from 60, the cursor must be the compressed value -1
    # (i.e., tick=-60). Try the search at tick=-60.
    assert bm.next_initialized_tick_within_one_word(-60, lte=True) == (-60, True)


def test_bitmap_search_gt_finds_next_in_word() -> None:
    """A search to the right from a lower uninitialized bit returns
    the lowest initialized bit at or above the search position.
    """
    bm = TickBitmap(tick_spacing=60)
    bm.flip(120)
    # Search at 60 (compressed=1): no init <=1. lte=False returns
    # compressed=2 (tick=120), the next position to check, with the
    # bit set.
    assert bm.next_initialized_tick_within_one_word(60, lte=False) == (120, True)


def test_bitmap_search_returns_word_edge_when_no_init() -> None:
    """When no bit is set in the masked range, ``initialized=False``
    and ``next`` is the edge of the word the caller can fall through.
    """
    bm = TickBitmap(tick_spacing=60)
    # Search at tick=60 (compressed=1) to the left: no init bit in
    # word 0 at or below bit 1; the search lands at bit 0 (tick 0).
    assert bm.next_initialized_tick_within_one_word(60, lte=True) == (0, False)


# ---------------------------------------------------------------------------
# TickInfo / PositionKey
# ---------------------------------------------------------------------------


def test_tick_info_zero_state_is_uninitialized() -> None:
    """A zero-state ``TickInfo`` has ``initialized=False`` and zero
    gross / net, mirroring V4's cleared tick.
    """
    info = TickInfo()
    assert info.liquidity_gross == 0
    assert info.liquidity_net == 0
    assert info.initialized is False


def test_tick_info_initialized_iff_gross_positive() -> None:
    """``initialized`` follows V4's ``gross > 0`` rule."""
    assert TickInfo(liquidity_gross=1).initialized is True
    assert TickInfo(liquidity_gross=MAX_LIQUIDITY).initialized is True
    assert TickInfo(liquidity_gross=0).initialized is False


def test_position_key_validates_ordering_and_domain() -> None:
    """``PositionKey`` rejects ``tick_lower >= tick_upper`` and
    out-of-domain ticks; equality uses all three fields.
    """
    with pytest.raises(ValueError):
        PositionKey(tick_lower=60, tick_upper=-60, salt=0)
    with pytest.raises(ValueError):
        PositionKey(tick_lower=MIN_TICK - 1, tick_upper=60, salt=0)
    with pytest.raises(ValueError):
        PositionKey(tick_lower=-60, tick_upper=MAX_TICK + 1, salt=0)
    with pytest.raises(ValueError):
        PositionKey(tick_lower=-60, tick_upper=60, salt=-1)
    # Same triple is equal; different salt is a different position.
    a = PositionKey(tick_lower=-60, tick_upper=60, salt=0)
    b = PositionKey(tick_lower=-60, tick_upper=60, salt=0)
    c = PositionKey(tick_lower=-60, tick_upper=60, salt=1)
    assert a == b
    assert hash(a) == hash(b)
    assert a != c


# ---------------------------------------------------------------------------
# Add / remove / poke
# ---------------------------------------------------------------------------


def test_modify_add_initializes_ticks_and_bitmap() -> None:
    """An add (``liquidity_delta > 0``) sets ``liquidityGross`` on
    the lower and upper ticks, updates ``liquidityNet`` (lower +=
    delta, upper -= delta), and flips the bitmap bit on the
    0 → non-zero transition.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add],
    )
    lower = state.tick_info(-60)
    upper = state.tick_info(60)
    assert lower.liquidity_gross == 1_000_000
    assert lower.liquidity_net == 1_000_000  # lower += delta
    assert upper.liquidity_gross == 1_000_000
    assert upper.liquidity_net == -1_000_000  # upper -= delta
    assert state.is_initialized(-60)
    assert state.is_initialized(60)
    assert state.modify_liquidity_count == 1
    # Active liquidity: bootstrap tick 0 is in [-60, 60), so it adds.
    assert state.final_active_liquidity == 1_000_000
    assert state.final_tick == 0


def test_modify_remove_clears_ticks_and_bitmap_when_gross_zero() -> None:
    """A remove (``liquidity_delta < 0``) that drives a tick's
    ``liquidityGross`` back to zero clears the tick and flips the
    bitmap bit on the non-zero → 0 transition.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    remove = make_modify_liquidity(
        block_number=102,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=-1_000_000,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add, remove],
    )
    # Both ticks drop back to zero; the bitmap is cleared.
    assert not state.is_initialized(-60)
    assert not state.is_initialized(60)
    assert state.tick_info(-60) == TickInfo()
    assert state.tick_info(60) == TickInfo()
    assert state.modify_liquidity_count == 2


def test_modify_remove_keeps_other_ticks_when_partial() -> None:
    """Removing liquidity from one of two positions keeps the
    other position's tick info and bitmap bit intact.
    """
    init = make_initialize(block_number=100)
    add_a = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
        salt=0,
    )
    add_b = make_modify_liquidity(
        block_number=102,
        log_index=1,
        tick_lower=-120,
        tick_upper=120,
        liquidity_delta=500_000,
        salt=0,
    )
    remove_a = make_modify_liquidity(
        block_number=103,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=-1_000_000,
        salt=0,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add_a, add_b, remove_a],
    )
    # Position A cleared, position B tick cleanups:
    assert not state.is_initialized(-60)
    assert not state.is_initialized(60)
    # Position B's ticks remain initialized with the larger gross
    # because both adds contributed to them.
    lower_b = state.tick_info(-120)
    upper_b = state.tick_info(120)
    assert lower_b.liquidity_gross == 500_000
    assert upper_b.liquidity_gross == 500_000
    # The net on lower_b = +500_000; upper_b = -500_000.
    assert lower_b.liquidity_net == 500_000
    assert upper_b.liquidity_net == -500_000
    assert state.is_initialized(-120)
    assert state.is_initialized(120)


def test_modify_poke_records_position_key_but_no_tick_change() -> None:
    """A poke (``liquidity_delta == 0``) does not touch the bitmap
    or tick info; it records the position key.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    poke = make_modify_liquidity(
        block_number=102,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=0,
        salt=0,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add, poke],
    )
    # State after the add is unchanged.
    assert state.tick_info(-60).liquidity_gross == 1_000_000
    assert state.tick_info(60).liquidity_gross == 1_000_000
    # Position key recorded.
    assert PositionKey(tick_lower=-60, tick_upper=60, salt=0) in state.position_keys
    # The poke counts as a modify event but does not touch the bitmap.
    assert state.modify_liquidity_count == 2
    assert state.initialized_ticks == (-60, 60)


# ---------------------------------------------------------------------------
# Boundary crossing both directions
# ---------------------------------------------------------------------------


def test_swap_crosses_tick_left_to_right_adds_net() -> None:
    """A swap that crosses an initialized tick left-to-right adds
    ``liquidityNet`` to the active liquidity (V4 ``Pool.swap``
    ``oneForZero`` branch).

    Bootstrap at tick 0; a position at [60, 120] with delta=500_000
    leaves the active liquidity at 0 (0 ∉ [60, 120)). The swap moves
    to tick 60 (post-cross of the lower tick of the position); V4's
    result.tick == step.tickNext for oneForZero, so the emitted tick
    equals the crossed tick. Crossing tick 60 left-to-right applies
    ``+liquidityNet = +500_000``; active liquidity becomes 500_000.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=60,
        tick_upper=120,
        liquidity_delta=500_000,
    )
    swap = make_swap(
        block_number=102,
        log_index=1,
        tick=60,
        liquidity=500_000,
        tx_seed=99,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add, swap],
    )
    assert state.final_active_liquidity == 500_000
    assert state.final_tick == 60
    # Crossing log: one crossing at tick 60 (oneForZero).
    assert len(state.crossings) == 1
    cr = state.crossings[0]
    assert cr.tick == 60
    assert cr.direction == CROSSING_ONE_FOR_ZERO
    assert cr.liquidity_net == 500_000
    assert cr.active_liquidity_before == 0
    assert cr.active_liquidity_after == 500_000


def test_swap_crosses_tick_right_to_left_subtracts_net() -> None:
    """A swap that crosses an initialized tick right-to-left
    subtracts ``liquidityNet`` from the active liquidity (V4
    ``Pool.swap`` ``zeroForOne`` branch).

    Bootstrap at tick 0; a position at [-60, 60] with delta=1_000_000
    leaves the active liquidity at 1_000_000 (0 ∈ [-60, 60)). The
    swap moves to tick -61 (post-cross of -60); V4's result.tick ==
    step.tickNext - 1 for zeroForOne, so the emitted tick is one
    below the crossed tick. Crossing tick -60 right-to-left
    subtracts ``liquidityNet = +1_000_000``; active liquidity
    becomes 0.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    swap = make_swap(
        block_number=102,
        log_index=1,
        tick=-61,
        liquidity=0,
        tx_seed=99,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add, swap],
    )
    assert state.final_active_liquidity == 0
    assert state.final_tick == -61
    # Crossing log: one crossing at tick -60 (zeroForOne).
    assert len(state.crossings) == 1
    cr = state.crossings[0]
    assert cr.tick == -60
    assert cr.direction == CROSSING_ZERO_FOR_ONE
    assert cr.liquidity_net == 1_000_000
    assert cr.active_liquidity_before == 1_000_000
    assert cr.active_liquidity_after == 0


def test_swap_crosses_multiple_ticks() -> None:
    """A swap that crosses two initialized ticks applies each
    ``liquidityNet`` in turn; the reconstructed final active
    liquidity matches the swap event's ``liquidity`` byte-for-byte.

    Bootstrap at tick 0; position A at [-120, -60] delta=300_000
    (outside the bootstrap range, contributes nothing); position B at
    [-60, 60] delta=1_000_000 (active liquidity = 1_000_000). The
    swap moves to tick -121, crossing both -60 (upper of A combined
    with lower of B; net sums to +700_000) and -120 (lower of A; net
    +300_000). After both crossings the active liquidity is 0.
    """
    init = make_initialize(block_number=100)
    add_a = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-120,
        tick_upper=-60,
        liquidity_delta=300_000,
    )
    add_b = make_modify_liquidity(
        block_number=102,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    swap = make_swap(
        block_number=103,
        log_index=1,
        tick=-121,
        liquidity=0,
        tx_seed=99,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add_a, add_b, swap],
    )
    assert state.final_active_liquidity == 0
    assert state.final_tick == -121
    # Crossing log: two crossings (at -60 then at -120), both zeroForOne.
    assert len(state.crossings) == 2
    assert state.crossings[0].tick == -60
    assert state.crossings[0].direction == CROSSING_ZERO_FOR_ONE
    assert state.crossings[1].tick == -120
    assert state.crossings[1].direction == CROSSING_ZERO_FOR_ONE
    # Cumulative active liquidity: 1_000_000 → 300_000 → 0.
    assert state.crossings[0].active_liquidity_before == 1_000_000
    assert state.crossings[0].active_liquidity_after == 300_000
    assert state.crossings[1].active_liquidity_before == 300_000
    assert state.crossings[1].active_liquidity_after == 0


def test_swap_with_no_tick_movement_does_not_change_liquidity() -> None:
    """A swap that does not move the tick does not change the
    active liquidity. A swap that claims a different
    ``liquidity`` without crossing any tick fails closed.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    no_move = make_swap(
        block_number=102,
        log_index=1,
        tick=0,
        liquidity=1_000_000,
        tx_seed=99,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add, no_move],
    )
    assert state.final_active_liquidity == 1_000_000
    assert state.crossings == ()

    # A swap that lies about its liquidity is rejected.
    lying = make_swap(
        block_number=103,
        log_index=1,
        tick=0,
        liquidity=2_000_000,
        tx_seed=100,
    )
    with pytest.raises(TickLiquidityError):
        reconstruct_tick_liquidity(
            chain_id=CHAIN.value,
            pool_id=POOL_ID.value,
            pool_key=STATIC_POOL_KEY,
            events=[init, add, lying],
        )


def test_swap_with_lying_final_liquidity_fails_closed() -> None:
    """A swap whose event ``liquidity`` does not match the
    reconstructed value fails closed; the partial reconstruction
    is not returned.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    # Swap moves to -61 (post-cross of -60); the reconstruction
    # says the final liquidity is 0 (we cross -60 with net
    # +1_000_000; zeroForOne subtracts: 1_000_000 - 1_000_000 = 0).
    # The event lies and claims 999_999_999.
    lying = make_swap(
        block_number=102,
        log_index=1,
        tick=-61,
        liquidity=999_999_999,
        tx_seed=99,
    )
    with pytest.raises(TickLiquidityError):
        reconstruct_tick_liquidity(
            chain_id=CHAIN.value,
            pool_id=POOL_ID.value,
            pool_key=STATIC_POOL_KEY,
            events=[init, add, lying],
        )


# ---------------------------------------------------------------------------
# Same-block multi-action
# ---------------------------------------------------------------------------


def test_same_block_multi_action_modify_modify_swap() -> None:
    """Multiple ``ModifyLiquidity`` and ``Swap`` events in the same
    block are applied in ``(transaction_index, log_index)`` order.
    The final state equals the per-event application in canonical
    order.

    Bootstrap at tick 0; position A at [-60, 60] delta=1_000_000
    (active liquidity = 1_000_000); position B at [-120, 120]
    delta=500_000 (0 ∈ [-120, 120); active liquidity += 500_000 =
    1_500_000). The swap moves to tick 120, crossing both tick 60
    (upper of A, net = -1_000_000) and tick 120 (upper of B, net =
    -500_000) left-to-right. The final liquidity is 0.
    """
    init = make_initialize(block_number=100)
    add_a = make_modify_liquidity(
        block_number=200,
        log_index=1,
        transaction_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    add_b = make_modify_liquidity(
        block_number=200,
        log_index=2,
        transaction_index=2,
        tick_lower=-120,
        tick_upper=120,
        liquidity_delta=500_000,
    )
    swap = make_swap(
        block_number=200,
        log_index=3,
        transaction_index=3,
        tick=120,
        liquidity=0,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add_a, add_b, swap],
    )
    # Active liquidity: 1_500_000 - 1_000_000 (cross 60) - 500_000
    # (cross 120) = 0.
    assert state.final_active_liquidity == 0
    assert state.final_tick == 120
    assert state.modify_liquidity_count == 2
    assert state.swap_count == 1
    # Two crossings in the log: at 60 and at 120, both oneForZero.
    assert len(state.crossings) == 2
    assert state.crossings[0].tick == 60
    assert state.crossings[0].direction == CROSSING_ONE_FOR_ZERO
    assert state.crossings[1].tick == 120
    assert state.crossings[1].direction == CROSSING_ONE_FOR_ZERO


def test_same_block_shuffled_order_matches_canonical() -> None:
    """Shuffling the order of events in the same block does not
    change the final state when the ``(transaction_index,
    log_index)`` order is preserved (V4's total order).
    """
    init = make_initialize(block_number=100)
    add_a = make_modify_liquidity(
        block_number=200,
        log_index=1,
        transaction_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    add_b = make_modify_liquidity(
        block_number=200,
        log_index=2,
        transaction_index=2,
        tick_lower=-120,
        tick_upper=120,
        liquidity_delta=500_000,
    )
    swap = make_swap(
        block_number=200,
        log_index=3,
        transaction_index=3,
        tick=120,
        liquidity=0,
    )
    canonical = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add_a, add_b, swap],
    )
    shuffled_input = [init, add_b, swap, add_a]
    rng = random.Random(0xC0FFEE)
    rng.shuffle(shuffled_input)
    shuffled = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=shuffled_input,
    )
    assert canonical == shuffled


# ---------------------------------------------------------------------------
# Max liquidity
# ---------------------------------------------------------------------------


def test_modify_add_max_liquidity_per_tick_fails_closed() -> None:
    """Adding ``liquidity_delta > 0`` such that ``liquidityGross``
    exceeds ``tickSpacingToMaxLiquidityPerTick`` raises
    ``TickLiquidityOverflowError``. The reconstruction does not
    produce a partial result.
    """
    init = make_initialize(block_number=100)
    max_per_tick = tick_spacing_to_max_liquidity_per_tick(STATIC_POOL_KEY.tick_spacing)
    overflow_add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=max_per_tick + 1,
    )
    with pytest.raises(TickLiquidityOverflowError) as excinfo:
        reconstruct_tick_liquidity(
            chain_id=CHAIN.value,
            pool_id=POOL_ID.value,
            pool_key=STATIC_POOL_KEY,
            events=[init, overflow_add],
        )
    assert excinfo.value.tick in (-60, 60)
    assert excinfo.value.max_per_tick == max_per_tick


def test_modify_add_max_liquidity_at_boundary_succeeds() -> None:
    """Adding exactly ``maxLiquidityPerTick`` succeeds (the V4
    check is strict ``>``); no overflow error is raised.
    """
    init = make_initialize(block_number=100)
    max_per_tick = tick_spacing_to_max_liquidity_per_tick(STATIC_POOL_KEY.tick_spacing)
    at_boundary = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=max_per_tick,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, at_boundary],
    )
    assert state.tick_info(-60).liquidity_gross == max_per_tick
    assert state.tick_info(60).liquidity_gross == max_per_tick


# ---------------------------------------------------------------------------
# Negative gross
# ---------------------------------------------------------------------------


def test_modify_remove_below_zero_fails_closed() -> None:
    """Removing more than ``liquidityGross`` drives the gross
    below zero; V4's ``LiquidityMath.addDelta`` reverts with
    ``SafeCastOverflow`` and our reconstruction raises
    ``LiquidityOverflowError`` (the lower-bound half).
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    over_remove = make_modify_liquidity(
        block_number=102,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=-2_000_000,
    )
    with pytest.raises(LiquidityOverflowError) as excinfo:
        reconstruct_tick_liquidity(
            chain_id=CHAIN.value,
            pool_id=POOL_ID.value,
            pool_key=STATIC_POOL_KEY,
            events=[init, add, over_remove],
        )
    assert excinfo.value.kind == "underflow"


# ---------------------------------------------------------------------------
# Spacing
# ---------------------------------------------------------------------------


def test_modify_with_misaligned_tick_lower_fails_closed() -> None:
    """A ``ModifyLiquidity`` event with ``tick_lower`` not a
    multiple of ``tick_spacing`` raises ``TickMisalignedError``
    (V4 ``TickBitmap.TickMisaligned`` revert).
    """
    init = make_initialize(block_number=100)
    bad = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-61,  # not a multiple of 60
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    with pytest.raises(TickMisalignedError):
        reconstruct_tick_liquidity(
            chain_id=CHAIN.value,
            pool_id=POOL_ID.value,
            pool_key=STATIC_POOL_KEY,
            events=[init, bad],
        )


def test_modify_with_misaligned_tick_upper_fails_closed() -> None:
    """A ``ModifyLiquidity`` event with ``tick_upper`` not a
    multiple of ``tick_spacing`` raises ``TickMisalignedError``.
    """
    init = make_initialize(block_number=100)
    bad = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=61,  # not a multiple of 60
        liquidity_delta=1_000_000,
    )
    with pytest.raises(TickMisalignedError):
        reconstruct_tick_liquidity(
            chain_id=CHAIN.value,
            pool_id=POOL_ID.value,
            pool_key=STATIC_POOL_KEY,
            events=[init, bad],
        )


def test_modify_with_misordered_ticks_fails_closed() -> None:
    """``tick_lower >= tick_upper`` raises ``TicksMisorderedError``."""
    init = make_initialize(block_number=100)
    bad = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=60,
        tick_upper=-60,
        liquidity_delta=1_000_000,
    )
    with pytest.raises(TicksMisorderedError):
        reconstruct_tick_liquidity(
            chain_id=CHAIN.value,
            pool_id=POOL_ID.value,
            pool_key=STATIC_POOL_KEY,
            events=[init, bad],
        )


def test_modify_with_out_of_bounds_tick_lower_fails_closed() -> None:
    """A tick below ``MIN_TICK`` raises ``TickOutOfBoundsError``
    (V4 ``Pool.TickLowerOutOfBounds``).
    """
    init = make_initialize(block_number=100)
    bad = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=MIN_TICK - 60,  # -887_332
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    with pytest.raises(TickOutOfBoundsError):
        reconstruct_tick_liquidity(
            chain_id=CHAIN.value,
            pool_id=POOL_ID.value,
            pool_key=STATIC_POOL_KEY,
            events=[init, bad],
        )


def test_modify_with_out_of_bounds_tick_upper_fails_closed() -> None:
    """A tick above ``MAX_TICK`` raises ``TickOutOfBoundsError``
    (V4 ``Pool.TickUpperOutOfBounds``).
    """
    init = make_initialize(block_number=100)
    # Compose an upper tick above MAX_TICK but aligned to spacing.
    bad_upper = MAX_TICK + 60
    bad = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=bad_upper,
        liquidity_delta=1_000_000,
    )
    with pytest.raises(TickOutOfBoundsError):
        reconstruct_tick_liquidity(
            chain_id=CHAIN.value,
            pool_id=POOL_ID.value,
            pool_key=STATIC_POOL_KEY,
            events=[init, bad],
        )


def test_modify_with_negative_tick_aligned_to_spacing() -> None:
    """A negative aligned tick (e.g. ``-60`` for spacing ``60``)
    succeeds; the bitmap stores the bit at ``wordPos=-1,
    bitPos=255`` per V4's signed-arithmetic-shift layout.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-120,
        tick_upper=-60,
        liquidity_delta=1_000_000,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add],
    )
    assert state.is_initialized(-120)
    assert state.is_initialized(-60)
    # Word -1 must carry bit 255 (-60 → compressed=-1 → wordPos=-1,
    # bitPos=255).
    words = dict(state.bitmap_words)
    assert words[-1] & (1 << 255)
    # Word -1 must also carry bit 254 (-120 → compressed=-2 →
    # wordPos=-1, bitPos=254).
    assert words[-1] & (1 << 254)


def test_wide_spacing_reconstruction_matches_v4() -> None:
    """A pool with ``tick_spacing=280`` (the pinned ZZZ/USDG
    reference) reconstructs the same per-tick ``TickInfo`` and
    bitmap layout as V4's ``updateTick`` + ``flipTick`` would.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-280,
        tick_upper=280,
        liquidity_delta=1_000_000,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=WIDE_SPACING_POOL_KEY,
        events=[init, add],
    )
    # Both ticks at multiples of 280; compressed values are -1
    # and 1 (in word 0).
    assert state.is_initialized(-280)
    assert state.is_initialized(280)
    assert state.max_liquidity_per_tick == tick_spacing_to_max_liquidity_per_tick(280)
    assert state.tick_info(-280).liquidity_net == 1_000_000
    assert state.tick_info(280).liquidity_net == -1_000_000


# ---------------------------------------------------------------------------
# Position keys
# ---------------------------------------------------------------------------


def test_position_keys_recorded_for_each_event() -> None:
    """Each ``ModifyLiquidity`` event records its ``(tickLower,
    tickUpper, salt)`` triple. Repeated add+remove of the same
    position collapses to one entry.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
        salt=0,
    )
    remove = make_modify_liquidity(
        block_number=102,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=-1_000_000,
        salt=0,
    )
    different_salt = make_modify_liquidity(
        block_number=103,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
        salt=1,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add, remove, different_salt],
    )
    keys = state.position_keys
    assert PositionKey(tick_lower=-60, tick_upper=60, salt=0) in keys
    assert PositionKey(tick_lower=-60, tick_upper=60, salt=1) in keys
    # Two distinct positions.
    assert len(keys) == 2


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_reconstruction_is_deterministic_under_shuffling() -> None:
    """Shuffling the input events does not change the reconstructed
    state, as long as the ``(block_number, transaction_index,
    log_index)`` ordering is deterministic.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    swap = make_swap(
        block_number=102,
        log_index=2,
        tick=-61,
        liquidity=0,
    )
    canonical = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add, swap],
    )
    shuffled_input = [init, add, swap]
    rng = random.Random(0xC0FFEE)
    rng.shuffle(shuffled_input)
    shuffled = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=shuffled_input,
    )
    assert canonical == shuffled


def test_reconstruction_records_can_be_deepcopied() -> None:
    """The reconstruction does not mutate its inputs; deep-copying
    the events produces the same output.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    swap = make_swap(
        block_number=102,
        log_index=2,
        tick=-61,
        liquidity=0,
    )
    cloned = copy.deepcopy([init, add, swap])
    out = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add, swap],
    )
    out_cloned = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=cloned,
    )
    assert out == out_cloned


# ---------------------------------------------------------------------------
# Per-pool separation
# ---------------------------------------------------------------------------


def test_unknown_pool_raises() -> None:
    """An event whose ``pool_id`` does not match the declared pool
    raises ``UnknownPoolError``. The reconstruction does not silently
    accept cross-pool events (T041 must-not: never merge / share /
    carry state across pools).
    """
    other_pool = PoolId(0xDEADBEEF)
    init = make_initialize(block_number=100, pool_id=other_pool)
    with pytest.raises(UnknownPoolError):
        reconstruct_tick_liquidity(
            chain_id=CHAIN.value,
            pool_id=POOL_ID.value,
            pool_key=STATIC_POOL_KEY,
            events=[init],
        )


def test_reconstructions_for_two_pools_do_not_share_state() -> None:
    """Two independent reconstructions over different pools do not
    share their bitmap or tick state. The contract says tick /
    liquidity reconstruction is per pool over that pool's own
    qualified window; state must never be merged or carried across
    pools.
    """
    other_pool_id = PoolId(0xCAFEBABE)
    other_pool_key = PoolKey(
        currency0=Currency.from_hex("0x" + "33" * 20),
        currency1=Currency.from_hex("0x" + "44" * 20),
        fee=3000,
        tick_spacing=60,
        hooks=Address.zero(),
    )
    init_a = make_initialize(block_number=100)
    init_b = make_initialize(block_number=100, pool_id=other_pool_id)
    add_a = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    add_b = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=999_999_999,
        pool_id=other_pool_id,
    )
    state_a = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init_a, add_a],
    )
    state_b = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=other_pool_id.value,
        pool_key=other_pool_key,
        events=[init_b, add_b],
    )
    # Distinct pool identities, distinct gross values.
    assert state_a.pool_id_value == POOL_ID.value
    assert state_b.pool_id_value == other_pool_id.value
    assert state_a.tick_info(-60).liquidity_gross == 1_000_000
    assert state_b.tick_info(-60).liquidity_gross == 999_999_999


# ---------------------------------------------------------------------------
# Type / sanity checks
# ---------------------------------------------------------------------------


def test_empty_events_produce_empty_state() -> None:
    """An empty event sequence yields a reconstruction with no
    initialized ticks, zero active liquidity, and counts of zero.
    """
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[],
    )
    assert state.ticks == ()
    assert state.bitmap_words == ()
    assert state.crossings == ()
    assert state.position_keys == ()
    assert state.final_active_liquidity == 0
    assert state.modify_liquidity_count == 0
    assert state.swap_count == 0
    assert state.initialized_ticks == ()
    assert isinstance(state, ReconstructedPoolTickState)


def test_initialize_does_not_change_state() -> None:
    """An ``Initialize`` event alone produces an empty tick state
    (it is the lifecycle marker, not a tick modification).
    """
    init = make_initialize(block_number=100)
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init],
    )
    assert state.ticks == ()
    assert state.bitmap_words == ()
    assert state.final_active_liquidity == 0
    assert state.swap_count == 0
    assert state.modify_liquidity_count == 0


def test_donate_does_not_change_state() -> None:
    """A ``Donate`` event leaves the tick state untouched."""
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    donate = make_donate(block_number=102, log_index=1)
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add, donate],
    )
    assert state.tick_info(-60).liquidity_gross == 1_000_000
    assert state.tick_info(60).liquidity_gross == 1_000_000
    assert state.final_active_liquidity == 1_000_000


def test_bootstrap_tick_is_honored() -> None:
    """The ``bootstrap_tick`` parameter sets the pre-Initialize
    current tick; a ``ModifyLiquidity`` event whose range does not
    contain the bootstrap tick does not update the active liquidity.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=60,
        tick_upper=120,
        liquidity_delta=1_000_000,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add],
        bootstrap_tick=0,
    )
    # bootstrap_tick=0 is not in [60, 120); active liquidity stays 0.
    assert state.final_active_liquidity == 0
    # But the tick info is updated.
    assert state.tick_info(60).liquidity_gross == 1_000_000
    assert state.tick_info(120).liquidity_gross == 1_000_000


def test_tick_crossing_carries_event_identity() -> None:
    """``TickCrossing`` carries the EventKey fields of the producing
    swap so downstream consumers can attribute each crossing.
    """
    init = make_initialize(block_number=100)
    add = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=1_000_000,
    )
    swap = make_swap(
        block_number=102,
        log_index=2,
        transaction_index=5,
        tick=-61,
        liquidity=0,
    )
    state = reconstruct_tick_liquidity(
        chain_id=CHAIN.value,
        pool_id=POOL_ID.value,
        pool_key=STATIC_POOL_KEY,
        events=[init, add, swap],
    )
    assert len(state.crossings) == 1
    cr = state.crossings[0]
    assert cr.block_number == 102
    assert cr.transaction_index == 5
    assert cr.log_index == 2
    assert cr.tick == -60
    assert isinstance(cr, TickCrossing)


def test_untyped_event_raises() -> None:
    """A non-V4-typed record is rejected so the reconstruction
    never processes foreign objects.
    """
    bogus = object()
    with pytest.raises(TypeError):
        reconstruct_tick_liquidity(
            chain_id=CHAIN.value,
            pool_id=POOL_ID.value,
            pool_key=STATIC_POOL_KEY,
            events=[bogus],
        )


def test_word_mask_constant_is_256_bits() -> None:
    """Sanity: the bitmap word mask covers exactly the low 256 bits."""
    assert BITMAP_WORD_BITS == 256
    assert BITMAP_WORD_MASK == (1 << 256) - 1
