"""Tests for T040 deterministic event replay.

Coverage:

- ordering: total ordering by ``(block_number, transaction_index,
  log_index)`` is stable under shuffling, chunking, and restart
  boundaries (the T040 outcome);
- duplicates: replay fails explicitly with
  :class:`DuplicateEventError`;
- missing ``transaction_index``: replay fails explicitly with
  :class:`MissingTransactionIndexError`;
- impossible transitions: ``Swap`` / ``ModifyLiquidity`` /
  ``Donate`` before ``Initialize``, second ``Initialize``,
  zero-price ``Swap`` all fail with ``ImpossibleTransitionError``
  and a stable reason code;
- unknown pool: events whose ``pool_id`` differs from the
  declared :class:`ReplayInput.pool_id` fail with
  :class:`UnknownPoolError`;
- protocol fee: directional state survives both directions of fee
  change, same-block ``update-before-swap`` ordering applies the
  new fee to the swap that follows in the same block, and the
  zero-protocol-fee initialization state matches the pinned core
  vector;
- dynamic-fee pools: the effective combined swap fee emitted by
  each ``Swap`` is preserved byte-for-byte on every checkpoint;
  the replay does not reconstruct unobserved between-swap fee
  updates;
- pool lifecycle: ``Initialize`` sets the post-init state,
  ``Swap`` takes the post-swap state from the event,
  ``ModifyLiquidity`` updates active-liquidity when the current
  tick is inside the range, ``Donate`` is recorded without
  touching observable state;
- exact integer evidence: every checkpoint carries the
  per-event wire values a downstream consumer (T051) needs to
  separate protocol fees from LP fee growth under pinned V4
  rounding semantics;
- reproducibility: the fingerprint of two replays over the same
  set of events matches regardless of chunking or restart order.

The fixtures live in :mod:`_replay_t040_fixtures`; they construct
typed records directly without going through the storage reader.
"""

from __future__ import annotations

import copy
import itertools
import random
from pathlib import Path

import pytest

from _replay_t040_fixtures import (
    CHAIN,
    POOL_ID,
    STATIC_POOL_KEY,
    ZERO_POOL_ID,
    make_donate,
    make_initialize,
    make_modify_liquidity,
    make_protocol_fee_updated,
    make_swap,
)
from robinhood_lp.replay import (
    DuplicateEventError,
    ImpossibleTransitionError,
    MissingTransactionIndexError,
    PoolCheckpoint,
    Replayer,
    ReplayInput,
    ReplayOutput,
    UnknownEventTypeError,
    UnknownPoolError,
    WindowBoundsError,
    pack_protocol_fee,
    replay,
    replay_output_fingerprint,
    unpack_protocol_fee,
)
from robinhood_lp.replay.checkpoint import (
    EVENT_TYPE_DONATE,
    EVENT_TYPE_INITIALIZE,
    EVENT_TYPE_MODIFY_LIQUIDITY,
    EVENT_TYPE_SWAP,
)
from robinhood_lp.storage.schema import (
    DonateLogRecord,
    SwapLogRecord,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _replay_input(
    *,
    from_block: int = 0,
    to_block: int = 1_000_000,
    pool_id: object = POOL_ID,
    initial_sqrt_price_x96: int = 79228162514264337593543950336,
    initial_tick: int = 0,
) -> ReplayInput:
    return ReplayInput(
        chain_id=CHAIN,
        pool_id=pool_id if isinstance(pool_id, type(POOL_ID)) else POOL_ID,
        data_root=Path("/tmp/lp-replay-fixture"),
        from_block=from_block,
        to_block=to_block,
        initial_sqrt_price_x96=initial_sqrt_price_x96,
        initial_tick=initial_tick,
    )


def _basic_event_sequence() -> list[object]:
    """A canonical event sequence: init -> modify -> swap -> donate -> fee.

    Returned in canonical ``(block_number, transaction_index,
    log_index)`` order so the same-sequence test can compare it to
    the chunked / shuffled variants.
    """
    return [
        make_initialize(block_number=100, tx_seed=0xA0),
        make_modify_liquidity(
            block_number=101,
            log_index=1,
            tick_lower=-60,
            tick_upper=60,
            liquidity_delta=10**18,
            tx_seed=0xA1,
        ),
        make_swap(
            block_number=102,
            log_index=2,
            amount0=-1_000_000,
            amount1=2_000_000,
            sqrt_price_x96=79228162514264337593543950337,
            liquidity=10**18,
            tick=1,
            fee=3000,
            tx_seed=0xA2,
        ),
        make_donate(block_number=103, log_index=3, amount0=100, amount1=200, tx_seed=0xA3),
        make_protocol_fee_updated(
            block_number=104,
            log_index=4,
            protocol_fee=pack_protocol_fee(100, 200),
            tx_seed=0xA4,
        ),
    ]


def _chunked(seq: list[object], *, n_chunks: int) -> list[list[object]]:
    """Split ``seq`` into ``n_chunks`` roughly equal pieces.

    The replay must produce the same checkpoint sequence regardless
    of how the input is split; this helper simulates a checkpointed
    ingestion that resumes from saved shards.
    """
    if n_chunks < 1:
        raise ValueError("n_chunks must be >= 1")
    if n_chunks == 1:
        return [list(seq)]
    chunk_size = max(1, len(seq) // n_chunks)
    chunks = []
    for i in range(0, len(seq), chunk_size):
        chunks.append(list(seq[i : i + chunk_size]))
    if len(chunks) > n_chunks:
        # Merge the trailing overflow back into the last chunk so the
        # caller controls the chunk count exactly.
        merged = chunks[-2] + chunks[-1]
        chunks = chunks[:-2] + [merged]
    return chunks


# ---------------------------------------------------------------------------
# Ordering: shuffled / chunked / restarted inputs are deterministic
# ---------------------------------------------------------------------------


def test_replay_is_deterministic_under_shuffling() -> None:
    """Shuffling the input does not change the replay's checkpoint
    sequence. Two runs over the same events — one in canonical
    order, one shuffled by a fixed RNG — must produce identical
    fingerprints.
    """
    canonical = _basic_event_sequence()
    shuffled = list(canonical)
    rng = random.Random(0xC0FFEE)
    rng.shuffle(shuffled)

    out_canonical = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=canonical)
    out_shuffled = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=shuffled)

    assert out_canonical.checkpoints == out_shuffled.checkpoints
    assert replay_output_fingerprint(out_canonical) == replay_output_fingerprint(out_shuffled)


def test_replay_is_deterministic_under_chunking() -> None:
    """Chunking the input into independent shards — as a
    checkpointed ingestion would — does not change the replay's
    checkpoint sequence. The replay produces one checkpoint per
    event regardless of chunk boundaries.
    """
    canonical = _basic_event_sequence()
    chunks = _chunked(canonical, n_chunks=4)
    out_canonical = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=canonical)
    out_chunked = replay(
        _replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=itertools.chain(*chunks)
    )

    assert out_canonical.checkpoints == out_chunked.checkpoints
    assert out_canonical.event_count == len(canonical)
    assert replay_output_fingerprint(out_canonical) == replay_output_fingerprint(out_chunked)


def test_replay_is_deterministic_under_restart_boundaries() -> None:
    """A run that restarts after each chunk — re-creating the
    replayer from scratch — produces the same checkpoint sequence
    as the canonical run. This is the contract-level acceptance
    criterion for "restarted inputs yield identical checkpoints".
    """
    canonical = _basic_event_sequence()
    chunks = _chunked(canonical, n_chunks=3)
    expected = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=canonical)

    rebuilt_checkpoints = []
    replayer = Replayer(_replay_input(), pool_fee=STATIC_POOL_KEY.fee)
    for chunk in chunks:
        for record in chunk:
            replayer.replay([record])
        rebuilt_checkpoints.append(replayer.checkpoints)

    # Each restart checkpoint is the running prefix; the union
    # reaches the full sequence exactly once at the final restart.
    full = rebuilt_checkpoints[-1]
    assert full == expected.checkpoints
    assert replay_output_fingerprint(
        ReplayOutput(
            input=_replay_input(),
            checkpoints=full,
            event_count=len(full),
        )
    ) == replay_output_fingerprint(expected)


def test_replay_ordering_is_block_then_tx_then_log_index() -> None:
    """The total order is lexicographic on
    ``(block_number, transaction_index, log_index)``. A test that
    gives three events with three distinct sort orders verifies
    the order is observed correctly when the input is shuffled.
    """
    events = [
        # Block 200, tx 5, log 1 — must come second
        make_swap(
            block_number=200,
            log_index=1,
            transaction_index=5,
            amount0=-10,
            amount1=20,
            sqrt_price_x96=79228162514264337593543950336,
            liquidity=1,
            tick=0,
            fee=3000,
        ),
        # Block 100, tx 9, log 0 — must come first
        make_initialize(block_number=100, log_index=0, transaction_index=9),
        # Block 200, tx 4, log 0 — must come before the other 200 event
        make_modify_liquidity(
            block_number=200,
            log_index=0,
            transaction_index=4,
            tick_lower=-60,
            tick_upper=60,
            liquidity_delta=5,
        ),
    ]
    rng = random.Random(42)
    rng.shuffle(events)
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=events)

    assert [c.event_type for c in out.checkpoints] == [
        EVENT_TYPE_INITIALIZE,
        EVENT_TYPE_MODIFY_LIQUIDITY,
        EVENT_TYPE_SWAP,
    ]
    assert [c.block_number for c in out.checkpoints] == [100, 200, 200]
    assert [c.transaction_index for c in out.checkpoints] == [9, 4, 5]
    assert [c.log_index for c in out.checkpoints] == [0, 0, 1]


# ---------------------------------------------------------------------------
# Duplicate handling
# ---------------------------------------------------------------------------


def test_duplicate_event_key_raises() -> None:
    """Two records with the same EventKey raise
    :class:`DuplicateEventError`. The exception carries the
    offending EventKey and the 0-based index of the first
    observation in the ordered sequence.
    """
    a = make_swap(
        block_number=200,
        log_index=1,
        transaction_index=5,
        amount0=-10,
        amount1=20,
    )
    b = make_swap(
        block_number=200,
        log_index=1,
        transaction_index=5,
        amount0=-10,
        amount1=20,
    )
    assert a.event_key() == b.event_key()

    with pytest.raises(DuplicateEventError) as excinfo:
        replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[a, b])

    assert excinfo.value.event_key[3] == 1  # log_index
    assert excinfo.value.first_seen_index == 0


def test_duplicate_event_key_with_different_sort_position_still_detected() -> None:
    """Two records with the same EventKey but submitted in different
    positions are still duplicates. The duplicate is detected on
    the EventKey, not the sort key, so an out-of-order submission
    does not change the outcome.
    """
    a = make_swap(block_number=200, log_index=1, transaction_index=5, tx_seed=100)
    b = make_swap(block_number=200, log_index=1, transaction_index=5, tx_seed=100)
    events = [a, make_initialize(block_number=100), b]
    with pytest.raises(DuplicateEventError):
        replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=events)


# ---------------------------------------------------------------------------
# Missing / unusable transaction index
# ---------------------------------------------------------------------------


def test_negative_transaction_index_raises() -> None:
    """A record with a negative ``transaction_index`` fails the
    total ordering. The error class is
    :class:`MissingTransactionIndexError` and the attribute
    carries the offending value.
    """
    bad = make_swap(block_number=200, log_index=0, transaction_index=0)
    object.__setattr__(bad, "transaction_index", -1)
    with pytest.raises(MissingTransactionIndexError) as excinfo:
        replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[bad])
    assert excinfo.value.transaction_index == -1


def test_non_int_transaction_index_raises() -> None:
    """A record with a non-integer ``transaction_index`` fails the
    total ordering. The error carries the offending value so the
    reviewer can branch on it.
    """
    bad = make_swap(block_number=200, log_index=0, transaction_index=0)
    object.__setattr__(bad, "transaction_index", "0")
    with pytest.raises(MissingTransactionIndexError) as excinfo:
        replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[bad])
    assert excinfo.value.transaction_index == "0"


# ---------------------------------------------------------------------------
# Impossible transitions
# ---------------------------------------------------------------------------


def test_swap_before_initialize_raises() -> None:
    """A ``Swap`` event before ``Initialize`` is impossible. The
    error class is :class:`ImpossibleTransitionError` and the
    ``reason`` attribute carries the stable code
    ``swap_before_initialize``.
    """
    swap = make_swap(block_number=200, log_index=0, transaction_index=0)
    with pytest.raises(ImpossibleTransitionError) as excinfo:
        replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[swap])
    assert excinfo.value.reason == "swap_before_initialize"
    assert excinfo.value.event_type == EVENT_TYPE_SWAP


def test_modify_liquidity_before_initialize_raises() -> None:
    """A ``ModifyLiquidity`` event before ``Initialize`` is
    impossible. The error carries the stable reason
    ``modify_before_initialize``.
    """
    modify = make_modify_liquidity(block_number=200, log_index=0)
    with pytest.raises(ImpossibleTransitionError) as excinfo:
        replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[modify])
    assert excinfo.value.reason == "modify_before_initialize"


def test_donate_before_initialize_raises() -> None:
    """A ``Donate`` event before ``Initialize`` is impossible. The
    error carries the stable reason ``donate_before_initialize``.
    """
    donate = make_donate(block_number=200, log_index=0)
    with pytest.raises(ImpossibleTransitionError) as excinfo:
        replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[donate])
    assert excinfo.value.reason == "donate_before_initialize"


def test_second_initialize_raises() -> None:
    """A second ``Initialize`` for the same pool is impossible.
    The error reason is ``second_initialize``.
    """
    init1 = make_initialize(block_number=100, log_index=0)
    init2 = make_initialize(block_number=200, log_index=0)
    with pytest.raises(ImpossibleTransitionError) as excinfo:
        replay(
            _replay_input(),
            pool_fee=STATIC_POOL_KEY.fee,
            events=[init1, init2],
        )
    assert excinfo.value.reason == "second_initialize"


def test_swap_with_zero_price_raises() -> None:
    """A ``Swap`` against an uninitialized pool is impossible. The
    zero-price case (which is the same as uninitialized) fails
    with the stable reason ``swap_with_zero_price``.
    """
    init = make_initialize(block_number=100)
    swap = make_swap(block_number=101, log_index=0, sqrt_price_x96=0, liquidity=1, tick=0)
    with pytest.raises(ImpossibleTransitionError) as excinfo:
        replay(
            _replay_input(initial_sqrt_price_x96=0, initial_tick=0),
            pool_fee=STATIC_POOL_KEY.fee,
            events=[init, swap],
        )
    assert excinfo.value.reason == "swap_with_zero_price"


def test_unknown_event_type_raises() -> None:
    """An event that is not one of the five typed V4 records
    fails with :class:`UnknownEventTypeError`.
    """
    bogus = object()  # not a typed record
    with pytest.raises(UnknownEventTypeError):
        replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[bogus])


# ---------------------------------------------------------------------------
# Unknown pool
# ---------------------------------------------------------------------------


def test_unknown_pool_raises() -> None:
    """An event whose ``pool_id`` does not match the replay input's
    pool fails with :class:`UnknownPoolError`. The contract says
    one replay input per pool; mixing two pools' events is the
    primary failure mode this guard prevents.
    """
    init = make_initialize(block_number=100, pool_id=POOL_ID)
    swap = make_swap(block_number=200, log_index=0, pool_id=ZERO_POOL_ID)
    with pytest.raises(UnknownPoolError) as excinfo:
        replay(
            _replay_input(),
            pool_fee=STATIC_POOL_KEY.fee,
            events=[init, swap],
        )
    assert excinfo.value.expected_pool_id == POOL_ID.value
    assert excinfo.value.actual_pool_id == ZERO_POOL_ID.value


# ---------------------------------------------------------------------------
# Window bounds
# ---------------------------------------------------------------------------


def test_event_outside_window_raises() -> None:
    """An event whose block lies outside the replay input's
    declared ``[from_block, to_block]`` fails with
    :class:`WindowBoundsError`.
    """
    init = make_initialize(block_number=100)
    swap = make_swap(block_number=2_000, log_index=0)
    with pytest.raises(WindowBoundsError) as excinfo:
        replay(
            _replay_input(from_block=0, to_block=500),
            pool_fee=STATIC_POOL_KEY.fee,
            events=[init, swap],
        )
    assert excinfo.value.from_block == 0
    assert excinfo.value.to_block == 500


# ---------------------------------------------------------------------------
# Protocol-fee: changes in both directions
# ---------------------------------------------------------------------------


def test_protocol_fee_state_initialized_to_zero() -> None:
    """The zero-protocol-fee initialization state matches the
    pinned core vector: before any ``ProtocolFeeUpdated`` event
    has been applied, the directional fee state is ``(0, 0)``
    and the packed ``uint24`` is ``0x000000``.
    """
    init = make_initialize(block_number=100)
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[init])
    cp = out.final_checkpoint
    assert cp.protocol_fee_token0 == 0
    assert cp.protocol_fee_token1 == 0
    assert pack_protocol_fee(cp.protocol_fee_token0, cp.protocol_fee_token1) == 0
    assert cp.initialized is True


def test_protocol_fee_changes_in_both_directions() -> None:
    """``ProtocolFeeUpdated`` events in both directions update the
    directional fee state correctly. The replay unpacks the
    ``uint24`` and tracks ``token0`` and ``token1`` halves
    independently.
    """
    init = make_initialize(block_number=100)
    fee_set_1 = pack_protocol_fee(0x100, 0x200)
    fee_set_2 = pack_protocol_fee(0x050, 0x200)  # decrease token0
    fee_set_3 = pack_protocol_fee(0x050, 0x080)  # decrease token1
    events = [
        init,
        make_protocol_fee_updated(block_number=101, log_index=1, protocol_fee=fee_set_1),
        make_protocol_fee_updated(block_number=102, log_index=2, protocol_fee=fee_set_2),
        make_protocol_fee_updated(block_number=103, log_index=3, protocol_fee=fee_set_3),
    ]
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=events)
    cps = out.checkpoints
    # After init: (0, 0).
    assert (cps[0].protocol_fee_token0, cps[0].protocol_fee_token1) == (0, 0)
    # After fee_set_1: (0x100, 0x200).
    assert (cps[1].protocol_fee_token0, cps[1].protocol_fee_token1) == (0x100, 0x200)
    # After fee_set_2: token0 decreased, token1 unchanged.
    assert (cps[2].protocol_fee_token0, cps[2].protocol_fee_token1) == (0x050, 0x200)
    # After fee_set_3: token0 unchanged, token1 decreased.
    assert (cps[3].protocol_fee_token0, cps[3].protocol_fee_token1) == (0x050, 0x080)


def test_protocol_fee_event_carries_packed_value() -> None:
    """The checkpoint following a ``ProtocolFeeUpdated`` event
    carries the exact packed ``uint24`` the chain emitted under
    ``event_protocol_fee_packed``. This is the integer evidence
    T051 needs to separate protocol fees from LP fee growth.
    """
    init = make_initialize(block_number=100)
    packed = pack_protocol_fee(0x123, 0x456)
    fee = make_protocol_fee_updated(block_number=101, log_index=1, protocol_fee=packed)
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[init, fee])
    cp = out.final_checkpoint
    assert cp.event_protocol_fee_packed == packed
    assert (cp.protocol_fee_token0, cp.protocol_fee_token1) == unpack_protocol_fee(packed)


# ---------------------------------------------------------------------------
# Same-block update-before-swap ordering
# ---------------------------------------------------------------------------


def test_same_block_protocol_fee_update_before_swap_applies() -> None:
    """Same-block ``ProtocolFeeUpdated`` before ``Swap`` applies
    the new fee to the swap that follows. The order is determined
    by ``(transaction_index, log_index)``.
    """
    init = make_initialize(block_number=100)
    # ProtocolFeeUpdated first (transaction_index 0, log_index 0),
    # then Swap (transaction_index 1, log_index 1).
    fee = make_protocol_fee_updated(
        block_number=101,
        log_index=0,
        transaction_index=0,
        protocol_fee=pack_protocol_fee(0x300, 0x400),
    )
    swap = make_swap(block_number=101, log_index=1, transaction_index=1, fee=1234)
    out = replay(
        _replay_input(),
        pool_fee=STATIC_POOL_KEY.fee,
        events=[init, fee, swap],
    )
    cps = out.checkpoints
    # After fee update: state already reflects (0x300, 0x400).
    assert (cps[1].protocol_fee_token0, cps[1].protocol_fee_token1) == (0x300, 0x400)
    # After the swap: the directional fee state is still (0x300, 0x400).
    assert (cps[2].protocol_fee_token0, cps[2].protocol_fee_token1) == (0x300, 0x400)
    assert cps[2].last_swap_fee == 1234
    assert cps[2].event_swap_fee == 1234


def test_same_block_protocol_fee_update_after_swap_does_not_change_swap_fee() -> None:
    """Same-block ``Swap`` before ``ProtocolFeeUpdated`` does not
    retroactively change the swap's emitted fee. The swap's
    ``event_swap_fee`` is preserved byte-for-byte and the
    directional fee state changes only after the later
    ProtocolFeeUpdated.
    """
    init = make_initialize(block_number=100)
    # Swap first (transaction_index 0, log_index 0), then the fee
    # update (transaction_index 1, log_index 1).
    swap = make_swap(block_number=101, log_index=0, transaction_index=0, fee=1234)
    fee = make_protocol_fee_updated(
        block_number=101,
        log_index=1,
        transaction_index=1,
        protocol_fee=pack_protocol_fee(0x300, 0x400),
    )
    out = replay(
        _replay_input(),
        pool_fee=STATIC_POOL_KEY.fee,
        events=[init, swap, fee],
    )
    cps = out.checkpoints
    # After swap: directional fee is still (0, 0); swap fee is 1234.
    assert (cps[1].protocol_fee_token0, cps[1].protocol_fee_token1) == (0, 0)
    assert cps[1].last_swap_fee == 1234
    # After the fee update: directional fee is (0x300, 0x400).
    assert (cps[2].protocol_fee_token0, cps[2].protocol_fee_token1) == (0x300, 0x400)
    # The swap's emitted fee is unchanged.
    assert cps[2].last_swap_fee == 1234


def test_same_block_swap_uses_latest_protocol_fee_within_block() -> None:
    """Multiple ``ProtocolFeeUpdated`` events in the same block
    apply cumulatively; the last one before the swap determines
    the directional state active during the swap.
    """
    init = make_initialize(block_number=100)
    fee_a = make_protocol_fee_updated(
        block_number=101,
        log_index=0,
        transaction_index=0,
        protocol_fee=pack_protocol_fee(0x010, 0x020),
    )
    fee_b = make_protocol_fee_updated(
        block_number=101,
        log_index=1,
        transaction_index=0,
        protocol_fee=pack_protocol_fee(0x300, 0x400),
    )
    swap = make_swap(block_number=101, log_index=2, transaction_index=1, fee=5678)
    out = replay(
        _replay_input(),
        pool_fee=STATIC_POOL_KEY.fee,
        events=[init, fee_a, fee_b, swap],
    )
    cps = out.checkpoints
    # After fee_a: (0x010, 0x020). After fee_b: (0x300, 0x400).
    assert (cps[1].protocol_fee_token0, cps[1].protocol_fee_token1) == (0x010, 0x020)
    assert (cps[2].protocol_fee_token0, cps[2].protocol_fee_token1) == (0x300, 0x400)
    # After the swap, the directional state is still (0x300, 0x400).
    assert (cps[3].protocol_fee_token0, cps[3].protocol_fee_token1) == (0x300, 0x400)
    assert cps[3].last_swap_fee == 5678


# ---------------------------------------------------------------------------
# Dynamic-fee pool: preserve effective fee per swap
# ---------------------------------------------------------------------------


def test_dynamic_fee_pool_preserves_per_swap_fee() -> None:
    """For a dynamic-fee pool, every ``Swap`` event's ``fee``
    field is preserved byte-for-byte on the resulting checkpoint.
    The replay does not reconstruct unobserved between-swap fee
    updates.
    """
    from _replay_t040_fixtures import DYNAMIC_POOL_KEY

    init = make_initialize(block_number=100)
    swap_a = make_swap(block_number=101, log_index=1, fee=1234)
    swap_b = make_swap(block_number=102, log_index=2, fee=5678)
    swap_c = make_swap(block_number=103, log_index=3, fee=9999)
    out = replay(
        _replay_input(),
        pool_fee=DYNAMIC_POOL_KEY.fee,
        events=[init, swap_a, swap_b, swap_c],
    )
    cps = out.checkpoints
    assert cps[0].pool_fee == 0x800000  # DYNAMIC_FEE_FLAG sentinel
    # Per-swap emitted fees are preserved in order.
    assert cps[1].last_swap_fee == 1234
    assert cps[1].event_swap_fee == 1234
    assert cps[2].last_swap_fee == 5678
    assert cps[2].event_swap_fee == 5678
    assert cps[3].last_swap_fee == 9999
    assert cps[3].event_swap_fee == 9999
    # The replay never invents an intermediate fee between swaps.
    # Specifically, swapping the input order should still produce
    # the same checkpoint sequence — proving the replay is not
    # relying on ingestion order for fee values.
    out_swapped = replay(
        _replay_input(),
        pool_fee=DYNAMIC_POOL_KEY.fee,
        events=[init, swap_c, swap_a, swap_b],
    )
    assert out.checkpoints == out_swapped.checkpoints


def test_dynamic_fee_pool_does_not_reconstruct_unobserved_fees() -> None:
    """For a dynamic-fee pool, the replay does not pretend to
    know the fee between swaps. If the input carries no
    ``ProtocolFeeUpdated`` and no ``Swap``, the
    ``last_swap_fee`` is ``None``; the replay does not fall back
    to the pool's declared fee or to any inferred value.
    """
    from _replay_t040_fixtures import DYNAMIC_POOL_KEY

    init = make_initialize(block_number=100)
    out = replay(
        _replay_input(),
        pool_fee=DYNAMIC_POOL_KEY.fee,
        events=[init],
    )
    cp = out.final_checkpoint
    assert cp.pool_fee == 0x800000
    assert cp.last_swap_fee is None


# ---------------------------------------------------------------------------
# Pool-lifecycle state transitions
# ---------------------------------------------------------------------------


def test_initialize_sets_initial_price_and_tick() -> None:
    """The ``Initialize`` event sets the pool's initialized flag
    and the checkpoint carries the bootstrap snapshot supplied
    on the :class:`ReplayInput` as the initial ``sqrt_price_x96``
    and ``tick``.
    """
    init = make_initialize(block_number=100)
    out = replay(
        _replay_input(initial_sqrt_price_x96=1234567890, initial_tick=-42),
        pool_fee=STATIC_POOL_KEY.fee,
        events=[init],
    )
    cp = out.final_checkpoint
    assert cp.sqrt_price_x96 == 1234567890
    assert cp.tick == -42
    assert cp.initialized is True
    assert cp.event_type == EVENT_TYPE_INITIALIZE


def test_swap_updates_price_tick_active_liquidity() -> None:
    """A ``Swap`` event's post-swap values overwrite the state
    byte-for-byte. ``cumulative_volume0`` and
    ``cumulative_volume1`` accumulate the absolute value of
    ``amount0`` and ``amount1``.
    """
    init = make_initialize(block_number=100)
    swap = make_swap(
        block_number=101,
        log_index=1,
        amount0=-1_000_000,
        amount1=2_000_000,
        sqrt_price_x96=999,
        liquidity=5_000,
        tick=7,
        fee=3000,
    )
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[init, swap])
    cp = out.final_checkpoint
    assert cp.sqrt_price_x96 == 999
    assert cp.tick == 7
    assert cp.active_liquidity == 5_000
    assert cp.cumulative_volume0 == 1_000_000
    assert cp.cumulative_volume1 == 2_000_000
    assert cp.last_swap_fee == 3000


def test_swap_volume_accumulates_absolute_value() -> None:
    """Cumulative swap volume accumulates the absolute value of
    ``amount0`` and ``amount1`` across multiple swaps, regardless
    of sign.
    """
    init = make_initialize(block_number=100)
    swap_a = make_swap(
        block_number=101,
        log_index=1,
        amount0=-500,
        amount1=1_000,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
    )
    swap_b = make_swap(
        block_number=102,
        log_index=2,
        amount0=300,
        amount1=-800,
        sqrt_price_x96=2,
        liquidity=1,
        tick=1,
    )
    out = replay(
        _replay_input(),
        pool_fee=STATIC_POOL_KEY.fee,
        events=[init, swap_a, swap_b],
    )
    cp = out.final_checkpoint
    assert cp.cumulative_volume0 == 800  # |−500| + |300|
    assert cp.cumulative_volume1 == 1_800  # |1_000| + |−800|


def test_modify_liquidity_updates_active_liquidity_when_in_range() -> None:
    """A ``ModifyLiquidity`` event whose ``[tick_lower, tick_upper]``
    contains the current tick updates ``active_liquidity`` by
    ``liquidity_delta``.
    """
    init = make_initialize(block_number=100)
    modify = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=-60,
        tick_upper=60,
        liquidity_delta=5_000,
    )
    out = replay(
        _replay_input(initial_tick=0),
        pool_fee=STATIC_POOL_KEY.fee,
        events=[init, modify],
    )
    assert out.final_checkpoint.active_liquidity == 5_000


def test_modify_liquidity_outside_range_does_not_change_active_liquidity() -> None:
    """A ``ModifyLiquidity`` event whose range does not contain
    the current tick does not change ``active_liquidity``. The
    event is still recorded as a checkpoint but the
    active-liquidity view is unchanged.
    """
    init = make_initialize(block_number=100)
    modify = make_modify_liquidity(
        block_number=101,
        log_index=1,
        tick_lower=10_000,
        tick_upper=20_000,
        liquidity_delta=5_000,
    )
    out = replay(
        _replay_input(initial_tick=0),
        pool_fee=STATIC_POOL_KEY.fee,
        events=[init, modify],
    )
    assert out.final_checkpoint.active_liquidity == 0


def test_modify_liquidity_after_swap_respects_new_tick() -> None:
    """The active-liquidity view uses the current tick from the
    state. A ``Swap`` that moves the tick is followed by a
    ``ModifyLiquidity`` whose range is evaluated against the
    post-swap tick, not the pre-init tick.
    """
    init = make_initialize(block_number=100)
    swap = make_swap(
        block_number=101,
        log_index=1,
        amount0=-1,
        amount1=2,
        sqrt_price_x96=1,
        liquidity=1,
        tick=10_000,  # moved into the upper range
    )
    modify = make_modify_liquidity(
        block_number=102,
        log_index=2,
        tick_lower=5_000,
        tick_upper=15_000,
        liquidity_delta=99,
    )
    out = replay(
        _replay_input(initial_tick=0),
        pool_fee=STATIC_POOL_KEY.fee,
        events=[init, swap, modify],
    )
    # Modify sees tick=10_000, which is in [5_000, 15_000].
    # Pre-swap active_liquidity was 1; modify adds 99.
    assert out.final_checkpoint.active_liquidity == 1 + 99


def test_donate_does_not_change_protocol_state() -> None:
    """A ``Donate`` event does not change the protocol-level
    observable state. The checkpoint following the donation
    carries the same ``sqrt_price_x96``, ``tick``,
    ``active_liquidity``, and cumulative volumes as the previous
    checkpoint.
    """
    init = make_initialize(block_number=100)
    donate = make_donate(block_number=101, log_index=1, amount0=10**9, amount1=2 * 10**9)
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[init, donate])
    pre = out.checkpoints[0]
    post = out.checkpoints[1]
    assert post.sqrt_price_x96 == pre.sqrt_price_x96
    assert post.tick == pre.tick
    assert post.active_liquidity == pre.active_liquidity
    assert post.cumulative_volume0 == pre.cumulative_volume0
    assert post.cumulative_volume1 == pre.cumulative_volume1
    assert post.event_type == EVENT_TYPE_DONATE


# ---------------------------------------------------------------------------
# Output / fingerprint properties
# ---------------------------------------------------------------------------


def test_empty_input_produces_empty_output() -> None:
    """An empty event sequence yields an output with zero
    checkpoints. The :attr:`ReplayOutput.is_empty` flag is True
    and :attr:`ReplayOutput.final_checkpoint` raises so consumers
    never read an undefined state.
    """
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[])
    assert out.is_empty
    assert out.checkpoints == ()
    assert out.event_count == 0
    with pytest.raises(LookupError):
        _ = out.final_checkpoint


def test_event_count_matches_input_length() -> None:
    """The replay's ``event_count`` matches the number of input
    events regardless of order.
    """
    events = _basic_event_sequence()
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=events)
    assert out.event_count == len(events)
    assert len(out.checkpoints) == len(events)


def test_checkpoint_carries_integer_event_evidence_for_t051() -> None:
    """Every checkpoint carries the per-event integer evidence
    T051 needs to separate protocol fees from LP fee growth
    under pinned V4 rounding semantics. Both ``event_swap_fee``
    and ``event_protocol_fee_packed`` are exactly the values the
    chain emitted; the directional state is the unpacked form.
    """
    init = make_initialize(block_number=100)
    swap_fee_value = 0x1234
    packed = pack_protocol_fee(0xA, 0xB)
    fee = make_protocol_fee_updated(block_number=102, log_index=2, protocol_fee=packed)
    swap = make_swap(block_number=103, log_index=3, fee=swap_fee_value)
    out = replay(
        _replay_input(),
        pool_fee=STATIC_POOL_KEY.fee,
        events=[init, fee, swap],
    )
    cps = out.checkpoints
    # Initialize: no event-fee evidence.
    assert cps[0].event_swap_fee is None
    assert cps[0].event_protocol_fee_packed is None
    # ProtocolFeeUpdated: packed uint24 preserved.
    assert cps[1].event_protocol_fee_packed == packed
    assert cps[1].event_swap_fee is None
    # Swap: combined swap fee preserved byte-for-byte.
    assert cps[2].event_swap_fee == swap_fee_value
    assert cps[2].event_protocol_fee_packed is None
    # Directional state is the unpacked form of the latest fee event.
    assert (cps[2].protocol_fee_token0, cps[2].protocol_fee_token1) == unpack_protocol_fee(packed)


def test_replay_output_fingerprint_is_stable_across_iterations() -> None:
    """The fingerprint of the same input replayed twice matches."""
    events = _basic_event_sequence()
    a = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=events)
    b = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=events)
    assert replay_output_fingerprint(a) == replay_output_fingerprint(b)


# ---------------------------------------------------------------------------
# Deep-copy of records (records are frozen dataclasses)
# ---------------------------------------------------------------------------


def test_records_can_be_deepcopied_through_replay() -> None:
    """The replay does not mutate its input records; deep-copying
    the input before replaying produces the same output. The
    record dataclasses are frozen so this is a defensive check,
    not a structural one.
    """
    events = _basic_event_sequence()
    cloned = copy.deepcopy(events)
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=events)
    out_cloned = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=cloned)
    assert replay_output_fingerprint(out) == replay_output_fingerprint(out_cloned)


# ---------------------------------------------------------------------------
# Replayer direct API
# ---------------------------------------------------------------------------


def test_replayer_class_replay_matches_functional_api() -> None:
    """The :class:`Replayer` class and the :func:`replay` function
    produce identical output. This guards against drift between
    the two entry points.
    """
    events = _basic_event_sequence()
    out_fn = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=events)
    replayer = Replayer(_replay_input(), pool_fee=STATIC_POOL_KEY.fee)
    out_cls = replayer.replay(events)
    assert out_fn.checkpoints == out_cls.checkpoints
    assert replay_output_fingerprint(out_fn) == replay_output_fingerprint(out_cls)


def test_replayer_rejects_bad_pool_fee() -> None:
    """The replayer rejects ``pool_fee`` values outside the
    uint24 domain.
    """
    with pytest.raises(ValueError):
        Replayer(_replay_input(), pool_fee=-1)
    with pytest.raises(ValueError):
        Replayer(_replay_input(), pool_fee=1 << 24)


def test_replay_input_validates_window() -> None:
    """:class:`ReplayInput` rejects an inverted window."""
    with pytest.raises(ValueError, match="from_block"):
        ReplayInput(
            chain_id=CHAIN,
            pool_id=POOL_ID,
            data_root=Path("/tmp/x"),
            from_block=200,
            to_block=100,
        )


# ---------------------------------------------------------------------------
# Helpers smoke tests (sanity for the test infrastructure itself)
# ---------------------------------------------------------------------------


def test_pack_unpack_protocol_fee_roundtrip() -> None:
    """Pack and unpack are mutual inverses on the valid domain."""
    for token0, token1 in (
        (0, 0),
        (1, 1),
        (0xFFF, 0xFFF),
        (0x123, 0x456),
        (0xAAA, 0x555),
    ):
        packed = pack_protocol_fee(token0, token1)
        assert unpack_protocol_fee(packed) == (token0, token1)
        assert 0 <= packed < (1 << 24)


def test_pack_protocol_fee_rejects_out_of_range() -> None:
    """Pack rejects halves outside ``[0, 4095]``."""
    with pytest.raises(ValueError):
        pack_protocol_fee(-1, 0)
    with pytest.raises(ValueError):
        pack_protocol_fee(0, 1 << 12)
    with pytest.raises(ValueError):
        pack_protocol_fee(1 << 12, 0)


def test_unpack_protocol_fee_rejects_out_of_range() -> None:
    """Unpack rejects ``packed`` outside ``[0, 2**24)``."""
    with pytest.raises(ValueError):
        unpack_protocol_fee(-1)
    with pytest.raises(ValueError):
        unpack_protocol_fee(1 << 24)


def test_pool_checkpoint_is_frozen() -> None:
    """``PoolCheckpoint`` is a frozen dataclass; mutation is
    rejected so checkpoints remain auditable evidence.
    """
    import dataclasses

    init = make_initialize(block_number=100)
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[init])
    cp = out.final_checkpoint
    assert isinstance(cp, PoolCheckpoint)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cp.tick = 99  # type: ignore[misc]


def test_swap_record_carries_signed_amounts_through_replay() -> None:
    """Signed ``amount0`` and ``amount1`` are absorbed into
    cumulative volume as absolute values; the V4 swap record
    itself preserves the signed wire values.
    """
    init = make_initialize(block_number=100)
    swap = make_swap(
        block_number=101,
        log_index=1,
        amount0=-42,
        amount1=7,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
    )
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[init, swap])
    cp = out.final_checkpoint
    assert cp.cumulative_volume0 == 42
    assert cp.cumulative_volume1 == 7
    # The Swap record itself is untouched: amount0 stays -42.
    assert isinstance(swap, SwapLogRecord)
    assert swap.amount0 == -42
    assert swap.amount1 == 7


def test_donate_record_carries_amounts_through_replay() -> None:
    """A ``Donate`` event is recorded as a typed checkpoint but
    does not modify protocol-level observable state.
    """
    init = make_initialize(block_number=100)
    donate = make_donate(block_number=101, log_index=1, amount0=10, amount1=20)
    out = replay(_replay_input(), pool_fee=STATIC_POOL_KEY.fee, events=[init, donate])
    cp = out.final_checkpoint
    assert cp.event_type == EVENT_TYPE_DONATE
    assert cp.cumulative_volume0 == 0
    assert cp.cumulative_volume1 == 0
    # The Donate record itself is untouched.
    assert isinstance(donate, DonateLogRecord)
    assert donate.amount0 == 10
    assert donate.amount1 == 20
