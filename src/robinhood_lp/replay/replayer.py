"""Deterministic event replay (T040).

The replay consumes an iterable of typed V4 log records
(:class:`InitializeLogRecord`, :class:`ModifyLiquidityLogRecord`,
:class:`SwapLogRecord`, :class:`DonateLogRecord`,
:class:`ProtocolFeeUpdatedLogRecord`) and produces a deterministic
sequence of :class:`PoolCheckpoint` records.

Determinism contract
--------------------

The replay produces identical :class:`ReplayOutput` for any
iteration order, chunking, or restart boundary as long as the
*set* of input records is the same and every record carries the
canonical ``(block_number, transaction_index, log_index)`` tuple.
The total ordering is the lexicographic order on that tuple;
ties are impossible because ``log_index`` is unique within a
transaction. Records with identical EventKeys raise
:class:`DuplicateEventError`.

Pool lifecycle
--------------

The replay enforces the V4 pool lifecycle:

- ``Initialize`` may occur at most once. A second ``Initialize``
  is :class:`ImpossibleTransitionError`;
- ``ModifyLiquidity`` / ``Swap`` / ``Donate`` cannot occur before
  ``Initialize``;
- ``Swap`` updates ``sqrt_price_x96``, ``tick``, and
  ``active_liquidity`` directly from the event's emitted values
  (V4's ``Swap`` event carries the post-swap state);
- ``ModifyLiquidity`` updates the running active-liquidity view
  by adding ``liquidity_delta`` when the current tick is inside
  ``[tick_lower, tick_upper]``;
- ``ProtocolFeeUpdated`` updates the directional fee state
  (``token0``, ``token1``) from the packed ``uint24``;
- ``Donate`` is recorded but does not change the protocol-level
  observable state (it carries only accounting amounts).

Dynamic-fee pools
-----------------

For dynamic-fee pools (``pool.fee == DYNAMIC_FEE_FLAG``) the
``Swap`` event's ``fee`` field is the effective combined swap fee
emitted by the pool. The replay preserves this value byte-for-byte
on every checkpoint (``last_swap_fee`` and ``event_swap_fee``).
It does **not** reconstruct unobserved between-swap fee updates —
that is a contract-level "must not".

``ProtocolFeeUpdated`` updates the LP-owned protocol-fee accumulator
independently of the dynamic fee; same-block ordering between a
``ProtocolFeeUpdated`` and a ``Swap`` is resolved by
``(transaction_index, log_index)`` so an update-before-swap
ordering applies the new fee state to the swap.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, TypeGuard

from robinhood_lp.protocol.records import (
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    ProtocolFeeUpdatedLogRecord,
    SwapLogRecord,
)
from robinhood_lp.replay.checkpoint import (
    EVENT_TYPE_DONATE,
    EVENT_TYPE_INITIALIZE,
    EVENT_TYPE_MODIFY_LIQUIDITY,
    EVENT_TYPE_PROTOCOL_FEE_UPDATED,
    EVENT_TYPE_SWAP,
    PoolCheckpoint,
)
from robinhood_lp.replay.errors import (
    DuplicateEventError,
    ImpossibleTransitionError,
    MissingTransactionIndexError,
    UnknownEventTypeError,
    UnknownPoolError,
    WindowBoundsError,
)
from robinhood_lp.replay.input import ReplayInput
from robinhood_lp.replay.output import ReplayOutput
from robinhood_lp.replay.protocol_fee import unpack_protocol_fee

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PoolState:
    """The replay's mutable per-pool state.

    Separate from :class:`PoolCheckpoint` because it carries the
    mutable slots the replayer updates between events. The
    checkpoint is the frozen snapshot taken *after* each event.
    """

    initialized: bool = False
    sqrt_price_x96: int = 0
    tick: int = 0
    active_liquidity: int = 0
    cumulative_volume0: int = 0
    cumulative_volume1: int = 0
    protocol_fee_token0: int = 0
    protocol_fee_token1: int = 0
    last_swap_fee: int | None = None


# ---------------------------------------------------------------------------
# Type-narrowing helpers
# ---------------------------------------------------------------------------


def _is_initialize(record: Any) -> TypeGuard[InitializeLogRecord]:
    return isinstance(record, InitializeLogRecord)


def _is_modify_liquidity(record: Any) -> TypeGuard[ModifyLiquidityLogRecord]:
    return isinstance(record, ModifyLiquidityLogRecord)


def _is_swap(record: Any) -> TypeGuard[SwapLogRecord]:
    return isinstance(record, SwapLogRecord)


def _is_donate(record: Any) -> TypeGuard[DonateLogRecord]:
    return isinstance(record, DonateLogRecord)


def _is_protocol_fee_updated(record: Any) -> TypeGuard[ProtocolFeeUpdatedLogRecord]:
    return isinstance(record, ProtocolFeeUpdatedLogRecord)


# ---------------------------------------------------------------------------
# Event-position helpers
# ---------------------------------------------------------------------------


def _event_position(
    record: Any,
) -> tuple[int, int]:
    """Return the deterministic ``(block_number, transaction_index)`` pair.

    The pair is incomplete (it omits ``log_index``); the replayer
    uses it for duplicate detection and the impossibility checks.
    The full order key ``(block_number, transaction_index,
    log_index)`` is computed separately for sorting.
    """
    return (int(record.block_number), int(record.transaction_index))


def _sort_key(
    record: Any,
) -> tuple[int, int, int]:
    """Return the deterministic ``(block_number, transaction_index,
    log_index)`` sort key.

    Validates ``transaction_index`` is a non-negative int along the
    way. Any record with a missing or unusable
    ``transaction_index`` raises
    :class:`MissingTransactionIndexError`.
    """
    block_number = int(record.block_number)
    transaction_index = record.transaction_index
    log_index = int(record.log_index)
    if not isinstance(transaction_index, int) or isinstance(transaction_index, bool):
        raise MissingTransactionIndexError(
            block_number=block_number,
            transaction_index=transaction_index,
            log_index=log_index,
        )
    if transaction_index < 0:
        raise MissingTransactionIndexError(
            block_number=block_number,
            transaction_index=transaction_index,
            log_index=log_index,
        )
    return (block_number, transaction_index, log_index)


# ---------------------------------------------------------------------------
# Replayer
# ---------------------------------------------------------------------------


class Replayer:
    """Stateful replayer for one :class:`ReplayInput`.

    The same algorithm is exposed via :func:`replay`. The class form
    is convenient for tests that need to inspect intermediate state
    (e.g. to assert that a specific transition failed with a
    specific reason code).
    """

    def __init__(
        self,
        replay_input: ReplayInput,
        *,
        pool_fee: int,
    ) -> None:
        if not isinstance(replay_input, ReplayInput):
            raise TypeError(
                f"Replayer: replay_input must be ReplayInput, got {type(replay_input).__name__}"
            )
        if not isinstance(pool_fee, int) or isinstance(pool_fee, bool):
            raise TypeError(f"Replayer: pool_fee must be int, got {type(pool_fee).__name__}")
        if pool_fee < 0 or pool_fee >= (1 << 24):
            raise ValueError(f"Replayer: pool_fee must fit in uint24, got {pool_fee}")
        self._input = replay_input
        self._pool_fee = pool_fee
        # Seed the pool state from the bootstrap snapshot the
        # ``ReplayInput`` carries. The first ``Initialize`` event
        # observed by the replay is what flips the ``initialized``
        # flag — the bootstrap is the *post-initialize* state a
        # StateView-style read produced, so it is not a "second
        # initialize". The replay takes the post-init price and
        # tick from the snapshot, then the matching Initialize
        # event confirms the lifecycle transition.
        self._state = _PoolState(
            initialized=False,
            sqrt_price_x96=int(replay_input.initial_sqrt_price_x96),
            tick=int(replay_input.initial_tick),
        )
        self._checkpoints: list[PoolCheckpoint] = []
        # EventKey tracking for duplicate detection. The full tuple
        # ``(chain_id, block_hash, tx_hash, log_index)`` matches the
        # protocol layer's EventKey; we use a frozenset for O(1) lookup.
        self._seen_event_keys: set[tuple[int, int, int, int]] = set()
        # Ordered-index map for :class:`DuplicateEventError`.
        self._event_key_to_index: dict[tuple[int, int, int, int], int] = {}

    # ----- public API ---------------------------------------------------

    @property
    def replay_input(self) -> ReplayInput:
        return self._input

    @property
    def pool_fee(self) -> int:
        return self._pool_fee

    @property
    def checkpoints(self) -> tuple[PoolCheckpoint, ...]:
        """Return the checkpoint sequence observed so far."""
        return tuple(self._checkpoints)

    @property
    def state(self) -> _PoolState:
        """Return the current mutable state.

        Exposed for tests; downstream consumers use
        :attr:`checkpoints` instead.
        """
        return self._state

    def replay(self, events: Iterable[Any]) -> ReplayOutput:
        """Replay ``events`` into a :class:`ReplayOutput`.

        The iterable may yield records in any order. The replay
        sorts them by ``(block_number, transaction_index,
        log_index)`` before applying them. ``None`` elements and
        elements outside :data:`VALID_EVENT_TYPES` raise
        :class:`UnknownEventTypeError`.
        """
        ordered = self._order(events)
        for record in ordered:
            self._apply(record)
        return ReplayOutput(
            input=self._input,
            checkpoints=tuple(self._checkpoints),
            event_count=len(self._checkpoints),
        )

    # ----- ordering -----------------------------------------------------

    def _order(self, events: Iterable[Any]) -> tuple[Any, ...]:
        """Sort ``events`` deterministically and validate positions.

        The order is lexicographic on
        ``(block_number, transaction_index, log_index)``. Records
        with the same tuple are emitted in input order (Python's
        sort is stable). Duplicate detection runs here on the
        ordered sequence so a duplicate is reported even when the
        first occurrence would fail a pool-lifecycle check; the
        contract requires duplicates to fail explicitly
        regardless of what other checks would do.

        Non-typed events (anything that is not one of the five
        V4 log record dataclasses) raise
        :class:`UnknownEventTypeError` here so the failure is
        attributable to a specific record before the apply loop
        runs.
        """
        materialised = list(events)
        for record in materialised:
            # Surface unknown-type and position problems before the
            # apply loop runs so the failure is attributable to a
            # specific record.
            _classify(record)
            _sort_key(record)
        materialised.sort(key=_sort_key)
        # Scan for duplicates across the whole ordered sequence.
        # Two records that share the EventKey tuple
        # ``(chain_id, block_hash, tx_hash, log_index)`` are
        # duplicates regardless of their relative positions in
        # the input. The scan runs after sorting so the
        # ``first_seen_index`` matches the ordered position.
        seen_event_keys: dict[tuple[int, int, int, int], int] = {}
        for ordered_index, record in enumerate(materialised):
            event_key = (
                int(record.chain_id.value),
                int(record.block_hash),
                int(record.transaction_hash),
                int(record.log_index),
            )
            if event_key in seen_event_keys:
                first_index = seen_event_keys[event_key]
                raise DuplicateEventError(
                    event_key=event_key,
                    first_seen_index=first_index,
                    block_number=int(record.block_number),
                    transaction_index=int(record.transaction_index),
                    log_index=int(record.log_index),
                )
            seen_event_keys[event_key] = ordered_index
        return tuple(materialised)

    # ----- per-event validation ----------------------------------------

    def _validate_event_identity(self, record: Any) -> None:
        """Reject records that do not belong to the declared pool.

        Also rejects records whose ``chain_id`` differs from
        :attr:`ReplayInput.chain_id` and records outside the
        declared window.
        """
        if record.chain_id != self._input.chain_id:
            raise UnknownPoolError(
                expected_pool_id=self._input.pool_id.value,
                actual_pool_id=_pool_id_of(record),
                block_number=int(record.block_number),
                transaction_index=int(record.transaction_index),
                log_index=int(record.log_index),
            )
        record_pool_id = _pool_id_of(record)
        if record_pool_id != self._input.pool_id.value:
            raise UnknownPoolError(
                expected_pool_id=self._input.pool_id.value,
                actual_pool_id=record_pool_id,
                block_number=int(record.block_number),
                transaction_index=int(record.transaction_index),
                log_index=int(record.log_index),
            )
        block_number = int(record.block_number)
        if block_number < self._input.from_block or block_number > self._input.to_block:
            raise WindowBoundsError(
                from_block=self._input.from_block,
                to_block=self._input.to_block,
                block_number=block_number,
                transaction_index=int(record.transaction_index),
                log_index=int(record.log_index),
            )

    def _detect_duplicate(self, record: Any) -> None:
        """Raise :class:`DuplicateEventError` if the EventKey was seen.

        The EventKey tuple is the canonical identity from
        :class:`robinhood_lp.protocol.events.EventKey`. Records that
        share a ``(block_number, transaction_index, log_index)``
        sort key but differ on block hash / tx hash / chain id are
        not duplicates — the sort-key tie is impossible in V4 but
        the duplicate check uses the wire identity, not the sort
        key.
        """
        event_key = (
            int(record.chain_id.value),
            int(record.block_hash),
            int(record.transaction_hash),
            int(record.log_index),
        )
        if event_key in self._seen_event_keys:
            first_index = self._event_key_to_index[event_key]
            raise DuplicateEventError(
                event_key=event_key,
                first_seen_index=first_index,
                block_number=int(record.block_number),
                transaction_index=int(record.transaction_index),
                log_index=int(record.log_index),
            )
        self._seen_event_keys.add(event_key)
        self._event_key_to_index[event_key] = len(self._checkpoints)

    # ----- apply -------------------------------------------------------

    def _apply(self, record: Any) -> None:
        """Dispatch one ordered record to the appropriate handler."""
        event_type = _classify(record)
        self._validate_event_identity(record)
        self._detect_duplicate(record)
        if event_type == EVENT_TYPE_INITIALIZE:
            self._apply_initialize(record)
        elif event_type == EVENT_TYPE_MODIFY_LIQUIDITY:
            self._apply_modify_liquidity(record)
        elif event_type == EVENT_TYPE_SWAP:
            self._apply_swap(record)
        elif event_type == EVENT_TYPE_DONATE:
            self._apply_donate(record)
        elif event_type == EVENT_TYPE_PROTOCOL_FEE_UPDATED:
            self._apply_protocol_fee_updated(record)
        else:  # pragma: no cover - defensive
            raise UnknownEventTypeError(
                event_type=str(event_type),
                block_number=int(record.block_number),
                transaction_index=int(record.transaction_index),
                log_index=int(record.log_index),
            )
        self._checkpoints.append(self._snapshot(event_type, record))

    # ----- per-type handlers -------------------------------------------

    def _apply_initialize(self, record: InitializeLogRecord) -> None:
        if self._state.initialized:
            raise ImpossibleTransitionError(
                reason="second_initialize",
                block_number=record.block_number,
                transaction_index=record.transaction_index,
                log_index=record.log_index,
                event_type=EVENT_TYPE_INITIALIZE,
            )
        # The Initialize event in V4 does not carry the post-init
        # ``sqrtPriceX96`` / ``tick`` on chain (those are returned
        # from the ``initialize()`` call). The replay consumes
        # them from the bootstrap snapshot on the ``ReplayInput``
        # and only marks the pool as initialized here.
        self._state.initialized = True
        # Liquidity starts at zero; the active-liquidity view
        # builds up from ModifyLiquidity / Swap events.

    def _apply_modify_liquidity(self, record: ModifyLiquidityLogRecord) -> None:
        if not self._state.initialized:
            raise ImpossibleTransitionError(
                reason="modify_before_initialize",
                block_number=record.block_number,
                transaction_index=record.transaction_index,
                log_index=record.log_index,
                event_type=EVENT_TYPE_MODIFY_LIQUIDITY,
            )
        # The ModifyLiquidity event carries the requested tick
        # range. When the current tick is inside the range, the
        # active-liquidity view updates by ``liquidity_delta``.
        # Outside the range the active-liquidity view is unchanged
        # but the framework still records the event so a downstream
        # tick-bitmap model can integrate it later.
        tick_lower = int(record.tick_lower)
        tick_upper = int(record.tick_upper)
        if tick_lower <= self._state.tick <= tick_upper:
            self._state.active_liquidity = self._state.active_liquidity + int(
                record.liquidity_delta
            )

    def _apply_swap(self, record: SwapLogRecord) -> None:
        if not self._state.initialized:
            raise ImpossibleTransitionError(
                reason="swap_before_initialize",
                block_number=record.block_number,
                transaction_index=record.transaction_index,
                log_index=record.log_index,
                event_type=EVENT_TYPE_SWAP,
            )
        if self._state.sqrt_price_x96 == 0:
            raise ImpossibleTransitionError(
                reason="swap_with_zero_price",
                block_number=record.block_number,
                transaction_index=record.transaction_index,
                log_index=record.log_index,
                event_type=EVENT_TYPE_SWAP,
            )
        # The Swap event carries the post-swap state. Replay takes
        # it byte-for-byte from the chain; the replay never
        # reconstructs the swap from prior state.
        self._state.sqrt_price_x96 = int(record.sqrt_price_x96)
        self._state.tick = int(record.tick)
        self._state.active_liquidity = int(record.liquidity)
        # Cumulative swap volumes (positive integers). The signed
        # ``amount0`` / ``amount1`` deltas the V4 swap emits are
        # magnitudes when treated as volume, so we accumulate the
        # absolute value.
        amount0 = int(record.amount0)
        amount1 = int(record.amount1)
        self._state.cumulative_volume0 += amount0 if amount0 >= 0 else -amount0
        self._state.cumulative_volume1 += amount1 if amount1 >= 0 else -amount1
        # The effective combined swap fee is taken byte-for-byte from
        # the Swap event. For dynamic-fee pools this is the only
        # observation we ever have of the fee at swap time; we do
        # not reconstruct unobserved between-swap fee updates.
        self._state.last_swap_fee = int(record.fee)

    def _apply_donate(self, record: DonateLogRecord) -> None:
        if not self._state.initialized:
            raise ImpossibleTransitionError(
                reason="donate_before_initialize",
                block_number=record.block_number,
                transaction_index=record.transaction_index,
                log_index=record.log_index,
                event_type=EVENT_TYPE_DONATE,
            )
        # Donate does not change protocol-level observable state;
        # the event is recorded so a future tick-liquidity model
        # can integrate the donation if it needs to. The replay
        # does not invent volume from donations.

    def _apply_protocol_fee_updated(self, record: ProtocolFeeUpdatedLogRecord) -> None:
        # ProtocolFeeUpdated can occur before or after Initialize
        # (the IProtocolFees accumulator is independent of the
        # pool's lifecycle). Same-block ordering between a
        # ProtocolFeeUpdated and a Swap is resolved by the
        # (transaction_index, log_index) tie-break, so an
        # update-before-swap ordering applies the new fee state
        # to the swap that follows in the same block.
        token0, token1 = unpack_protocol_fee(int(record.protocol_fee))
        self._state.protocol_fee_token0 = token0
        self._state.protocol_fee_token1 = token1

    # ----- checkpoint construction -------------------------------------

    def _snapshot(self, event_type: str, record: Any) -> PoolCheckpoint:
        """Construct the checkpoint that follows ``record``.

        The snapshot freezes the post-event state plus the
        per-event wire values downstream consumers (T051) need.
        """
        event_swap_fee: int | None
        event_protocol_fee_packed: int | None
        if _is_swap(record):
            event_swap_fee = int(record.fee)
            event_protocol_fee_packed = None
        elif _is_protocol_fee_updated(record):
            event_swap_fee = None
            event_protocol_fee_packed = int(record.protocol_fee)
        else:
            event_swap_fee = None
            event_protocol_fee_packed = None
        return PoolCheckpoint(
            chain_id=self._input.chain_id,
            pool_id=self._input.pool_id,
            block_number=int(record.block_number),
            block_hash=int(record.block_hash),
            transaction_index=int(record.transaction_index),
            log_index=int(record.log_index),
            event_type=event_type,
            sqrt_price_x96=self._state.sqrt_price_x96,
            tick=self._state.tick,
            active_liquidity=self._state.active_liquidity,
            cumulative_volume0=self._state.cumulative_volume0,
            cumulative_volume1=self._state.cumulative_volume1,
            protocol_fee_token0=self._state.protocol_fee_token0,
            protocol_fee_token1=self._state.protocol_fee_token1,
            initialized=self._state.initialized,
            pool_fee=self._pool_fee,
            last_swap_fee=self._state.last_swap_fee,
            event_swap_fee=event_swap_fee,
            event_protocol_fee_packed=event_protocol_fee_packed,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def replay(
    replay_input: ReplayInput,
    *,
    pool_fee: int,
    events: Iterable[Any],
) -> ReplayOutput:
    """Replay ``events`` for ``replay_input`` and return a :class:`ReplayOutput`.

    This is the canonical entry point. It constructs a
    :class:`Replayer`, applies every record, and returns the
    deterministic checkpoint sequence.

    ``pool_fee`` is the pool's declared fee from the ``PoolKey``
    (uint24, possibly the ``DYNAMIC_FEE_FLAG`` sentinel). The
    replay preserves it on every checkpoint for downstream
    dynamic-fee detection.
    """
    replayer = Replayer(replay_input, pool_fee=pool_fee)
    return replayer.replay(events)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pool_id_of(record: Any) -> int:
    """Return the integer PoolId of ``record``.

    Records carry ``pool_id: PoolId``; the integer form is used by
    :class:`UnknownPoolError` for diagnostic purposes.
    """
    return int(record.pool_id.value)


def _classify(record: Any) -> str:
    """Return the V4 event-type name for ``record``.

    Raises :class:`UnknownEventTypeError` for anything else.
    """
    if _is_initialize(record):
        return EVENT_TYPE_INITIALIZE
    if _is_modify_liquidity(record):
        return EVENT_TYPE_MODIFY_LIQUIDITY
    if _is_swap(record):
        return EVENT_TYPE_SWAP
    if _is_donate(record):
        return EVENT_TYPE_DONATE
    if _is_protocol_fee_updated(record):
        return EVENT_TYPE_PROTOCOL_FEE_UPDATED
    # ``None`` and any foreign object fall into this branch.
    raise UnknownEventTypeError(
        event_type=type(record).__name__,
        block_number=int(getattr(record, "block_number", -1)),
        transaction_index=int(getattr(record, "transaction_index", -1)),
        log_index=int(getattr(record, "log_index", -1)),
    )


__all__ = ["Replayer", "replay"]
