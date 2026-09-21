"""Canonical ``MarketCursor`` and the dataset-addressed ``MarketState`` reader (T109).

This module is the canonical read contract the T109 deliverable defines. It
introduces:

- :class:`MarketCursor` — the canonical cursor the entire replay surface
  orders market events by: ``(block_number, transaction_index, log_index)``.
  The cursor is the boundary at which the engine, the reconstruction
  surface, and the ``MarketState`` reader agree on "after this event".

- :class:`MarketState` — the read-time, dataset-addressed post-event
  reconstruction at a single cursor. ``MarketState`` is *not* a snapshot
  cache and stores no per-run market-event copy: every read recomputes
  the post-event state from T040/T041 prefix reconstruction under the
  cursor's :class:`robinhood_lp.replay.checkpoint.PoolCheckpoint`.

- :class:`MarketStateReader` — the single read surface. The reader
  walks the T040 checkpoint sequence the input pool's typed event
  stream produced and selects the checkpoint whose producing event's
  ``MarketCursor`` is at or before the requested cursor (or the
  end-of-block view when the requested cursor is end-of-block). The
  reader does not invoke any strategy callback, does not consult the
  backtest engine, and does not compute fee-growth surfaces;
  ``MarketState`` excludes fee-growth fields that T040/T041 do not
  reconstruct.

The module preserves and composes the existing T040 / T041 surfaces:

- :class:`robinhood_lp.replay.input.ReplayInput` — per-pool dataset
  identity (chain id, ``PoolId``, data root, block window, bootstrap).
- :func:`robinhood_lp.replay.replay` — the deterministic event-by-event
  reconstruction (T040).
- :func:`robinhood_lp.replay.ticks.reconstruct_tick_liquidity` — the
  per-tick reconstruction (T041).

The reader is a *sparse, projection* of T040/T041 onto a single cursor:
it returns the post-event checkpoint at the cursor that immediately
precedes the requested one (or the dataset's pre-Initialize zero-state
when the requested cursor is before the first event). The reader never
embeds a per-run market-event copy: the canonical event timeline
remains the dataset's append-only partition reference (T100). A reader
that needs ``feeGrowthGlobal`` / ``feeGrowthOutside`` /
``feeGrowthInside`` or fees for a range / liquidity composes T104's
cursor-addressable projection; this module must not replace, fork, or
reimplement T104.

Design constraints:

- **Dataset-addressed.** The reader accepts the dataset version and
  ``pool_key_id`` that identify the immutable partition reference
  (T100). Two reads with the same inputs produce byte-identical
  ``MarketState``.
- **Sparse projection.** The reader holds a single T040 checkpoint
  sequence for the input descriptor it was constructed with; it never
  materialises a per-run per-cursor snapshot table.
- **End-of-block.** A :class:`MarketCursor` whose
  ``transaction_index == -1`` and ``log_index == -1`` denotes the
  end-of-block view: the reader returns the checkpoint after the last
  event whose ``block_number`` equals ``cursor.block_number``.
- **No future data.** A cursor whose ``block_number`` exceeds the
  input window's ``to_block`` is rejected; a cursor whose position
  precedes the first event returns the dataset's pre-Initialize
  zero-state.
- **No fee-growth fields.** :class:`MarketState` carries every integer
  T040/T041 reconstruct at the cursor (sqrt price, tick, liquidity,
  cumulative volumes, protocol fees, initialization flag, last swap
  fee). It does not carry ``feeGrowthGlobal`` /
  ``feeGrowthOutside`` / ``feeGrowthInside`` / range fees; those
  belong to T104.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from robinhood_lp.replay.checkpoint import PoolCheckpoint
from robinhood_lp.replay.input import ReplayInput
from robinhood_lp.replay.replayer import replay as _replay
from robinhood_lp.replay.ticks import ReconstructedPoolTickState

#: Module version. Bumping it is a breaking change for downstream
#: consumers (the ``RunState`` / ``ReplayFrame`` readers, the T104
#: composition layer, and the compatibility adapter for T101 / T106).
MARKET_STATE_VERSION: Final[str] = "t109.market_state.v1"

#: Sentinel transaction_index / log_index pair that marks a cursor as
#: the end-of-block view of its ``block_number``. The reader returns
#: the last checkpoint produced by an event in that block.
_END_OF_BLOCK: Final[int] = -1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MarketStateError(ValueError):
    """Base class for market-state read failures."""


class CursorOutOfRangeError(MarketStateError):
    """The requested cursor exceeds the input window or precedes its origin."""


class CursorFormatError(MarketStateError):
    """A :class:`MarketCursor` field violates the cursor ordering rules."""


# ---------------------------------------------------------------------------
# MarketCursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketCursor:
    """The canonical cursor the replay surface orders market events by.

    The cursor is the triple ``(block_number, transaction_index,
    log_index)``; the order is lexicographic on the triple and
    block-only requests carry ``transaction_index == log_index == -1``
    to denote "end of this block".

    Field units:

    - ``block_number`` — non-negative int; the block number the
      cursor sits in.
    - ``transaction_index`` — non-negative int; the position of the
      transaction in the block (0 = first tx in the block), or
      ``-1`` to mark end-of-block.
    - ``log_index`` — non-negative int; the position of the log
      inside its emitting transaction (0 = first log in the tx),
      or ``-1`` to mark end-of-block.

    Equality / hashing follow dataclass identity; two cursors are
    equal iff every field is equal.
    """

    block_number: int
    transaction_index: int
    log_index: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.block_number, int)
            or isinstance(self.block_number, bool)
            or self.block_number < 0
        ):
            raise CursorFormatError(
                f"MarketCursor.block_number: must be non-negative int, got {self.block_number!r}"
            )
        tx = self.transaction_index
        log = self.log_index
        end_of_block = tx == _END_OF_BLOCK and log == _END_OF_BLOCK
        if end_of_block:
            return
        if (
            not isinstance(tx, int)
            or isinstance(tx, bool)
            or tx < 0
            or not isinstance(log, int)
            or isinstance(log, bool)
            or log < 0
        ):
            raise CursorFormatError(
                f"MarketCursor: transaction_index={tx} and log_index={log} "
                f"must both be non-negative ints or both equal to "
                f"{_END_OF_BLOCK} (end-of-block sentinel)"
            )

    @property
    def is_end_of_block(self) -> bool:
        """Return ``True`` iff this cursor denotes the end-of-block view."""
        return self.transaction_index == _END_OF_BLOCK and self.log_index == _END_OF_BLOCK

    @classmethod
    def end_of_block(cls, block_number: int) -> MarketCursor:
        """Construct an end-of-block cursor for ``block_number``."""
        return cls(
            block_number=block_number,
            transaction_index=_END_OF_BLOCK,
            log_index=_END_OF_BLOCK,
        )

    def cursor_key(self) -> tuple[int, int, int]:
        """Return the comparable triple ``(block, tx, log)``.

        ``-1`` for end-of-block is encoded as ``+inf`` so end-of-block
        sits after every real cursor in the same block.
        """
        big_pos = 1 << 30
        tx = big_pos if self.is_end_of_block else self.transaction_index
        log = big_pos if self.is_end_of_block else self.log_index
        return (self.block_number, tx, log)

    def __lt__(self, other: MarketCursor) -> bool:
        return self.cursor_key() < other.cursor_key()

    def __le__(self, other: MarketCursor) -> bool:
        return self.cursor_key() <= other.cursor_key()

    def __gt__(self, other: MarketCursor) -> bool:
        return self.cursor_key() > other.cursor_key()

    def __ge__(self, other: MarketCursor) -> bool:
        return self.cursor_key() >= other.cursor_key()


# ---------------------------------------------------------------------------
# MarketState
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketState:
    """The dataset-addressed post-event market state at one cursor.

    ``MarketState`` is the read-time projection the T109 contract
    binds. Every integer field derives from the deterministic T040
    checkpoint the requested cursor selects (or the dataset's
    pre-Initialize zero-state when the cursor precedes every event in
    the window).

    Field units:

    - ``version`` — schema version string.
    - ``dataset_version`` — non-empty string; the dataset content hash
      the read resolves against.
    - ``pool_key_id`` — non-empty string; the pool the read belongs to.
    - ``cursor`` — :class:`MarketCursor`; the exact cursor the read
      materialised.
    - ``checkpoint`` — :class:`robinhood_lp.replay.checkpoint.PoolCheckpoint`;
      the post-event checkpoint the cursor selects. ``None`` when the
      cursor precedes the first event in the window.
    - ``tick_state`` — :class:`robinhood_lp.replay.ticks.ReconstructedPoolTickState`;
      the per-tick reconstruction T041 produces for this cursor's pool.
      ``None`` when the cursor precedes the first event.
    - ``is_initialized`` — bool; True iff the pool was initialised at or
      before this cursor.
    - ``pre_initial_state`` — bool; True iff the cursor precedes every
      event in the dataset's window (the read returned the
      pre-Initialize zero-state).
    """

    version: str
    dataset_version: str
    pool_key_id: str
    cursor: MarketCursor
    checkpoint: PoolCheckpoint | None
    tick_state: ReconstructedPoolTickState | None
    is_initialized: bool
    pre_initial_state: bool

    def __post_init__(self) -> None:
        if self.version != MARKET_STATE_VERSION:
            raise MarketStateError(
                f"MarketState.version: must be {MARKET_STATE_VERSION!r}, got {self.version!r}"
            )
        if not isinstance(self.dataset_version, str) or not self.dataset_version:
            raise MarketStateError("MarketState.dataset_version: must be non-empty str")
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise MarketStateError("MarketState.pool_key_id: must be non-empty str")
        if not isinstance(self.cursor, MarketCursor):
            raise MarketStateError(
                f"MarketState.cursor: must be MarketCursor, got {type(self.cursor).__name__}"
            )
        if not isinstance(self.is_initialized, bool):
            raise MarketStateError(
                f"MarketState.is_initialized: must be bool, got "
                f"{type(self.is_initialized).__name__}"
            )
        if not isinstance(self.pre_initial_state, bool):
            raise MarketStateError(
                f"MarketState.pre_initial_state: must be bool, got "
                f"{type(self.pre_initial_state).__name__}"
            )
        if self.checkpoint is not None and not isinstance(self.checkpoint, PoolCheckpoint):
            raise MarketStateError(
                f"MarketState.checkpoint: must be PoolCheckpoint or None, "
                f"got {type(self.checkpoint).__name__}"
            )
        if self.tick_state is not None and not isinstance(
            self.tick_state, ReconstructedPoolTickState
        ):
            raise MarketStateError(
                f"MarketState.tick_state: must be "
                f"ReconstructedPoolTickState or None, "
                f"got {type(self.tick_state).__name__}"
            )
        if self.pre_initial_state and (self.checkpoint is not None or self.tick_state is not None):
            raise MarketStateError(
                "MarketState: pre_initial_state=True must carry checkpoint=None and tick_state=None"
            )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly mapping for serialisation.

        The mapping is the canonical shape every consumer (T104
        composition, T088 ``ReplayFrame``, the compatibility adapter
        for T101 / T106) reads.
        """
        out: dict[str, object] = {
            "version": self.version,
            "dataset_version": self.dataset_version,
            "pool_key_id": self.pool_key_id,
            "cursor": {
                "block_number": self.cursor.block_number,
                "transaction_index": self.cursor.transaction_index,
                "log_index": self.cursor.log_index,
            },
            "is_initialized": self.is_initialized,
            "pre_initial_state": self.pre_initial_state,
        }
        if self.checkpoint is not None:
            out["checkpoint"] = {
                "block_number": self.checkpoint.block_number,
                "transaction_index": self.checkpoint.transaction_index,
                "log_index": self.checkpoint.log_index,
                "event_type": self.checkpoint.event_type,
                "sqrt_price_x96": self.checkpoint.sqrt_price_x96,
                "tick": self.checkpoint.tick,
                "active_liquidity": self.checkpoint.active_liquidity,
                "cumulative_volume0": self.checkpoint.cumulative_volume0,
                "cumulative_volume1": self.checkpoint.cumulative_volume1,
                "protocol_fee_token0": self.checkpoint.protocol_fee_token0,
                "protocol_fee_token1": self.checkpoint.protocol_fee_token1,
                "initialized": self.checkpoint.initialized,
                "pool_fee": self.checkpoint.pool_fee,
                "last_swap_fee": self.checkpoint.last_swap_fee,
            }
        if self.tick_state is not None:
            out["tick_state"] = {
                "chain_id": self.tick_state.chain_id_value,
                "pool_id_value": self.tick_state.pool_id_value,
                "tick_spacing": self.tick_state.tick_spacing,
                "max_liquidity_per_tick": self.tick_state.max_liquidity_per_tick,
                "final_active_liquidity": self.tick_state.final_active_liquidity,
                "modify_liquidity_count": self.tick_state.modify_liquidity_count,
            }
        return out


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def _replay_checkpoints(
    replay_input: ReplayInput,
    events: Iterable[Any],
) -> tuple[PoolCheckpoint, ...]:
    """Return the deterministic T040 checkpoint sequence for ``events``.

    The function invokes the existing :func:`replay` so the
    reconstruction matches every other T040 consumer byte-for-byte. The
    reader never embeds the checkpoint sequence; the function rebuilds
    it per call so a same-cursor read against two readers over the
    same dataset is byte-equivalent.

    ``pool_fee`` is read from the ``ReplayInput``'s pool identity
    when available; the reader uses ``0`` (the replay's default)
    when the input does not carry one. The deterministic
    reconstruction is independent of the declared fee because the
    replay preserves the per-swap emitted fee on every checkpoint.
    """
    return _replay(replay_input, pool_fee=0, events=events).checkpoints


def _checkpoint_at(
    checkpoints: tuple[PoolCheckpoint, ...],
    cursor: MarketCursor,
) -> PoolCheckpoint | None:
    """Return the last checkpoint whose producing cursor is at or before ``cursor``.

    When ``cursor`` is end-of-block, the function returns the last
    checkpoint whose ``block_number`` equals ``cursor.block_number``.
    When no checkpoint satisfies the bound, the function returns
    ``None`` (the read is pre-initial-state).
    """
    chosen: PoolCheckpoint | None = None
    target_block = cursor.block_number
    target_tx = cursor.transaction_index
    target_log = cursor.log_index
    for cp in checkpoints:
        if cp.block_number > target_block:
            break
        if cp.block_number < target_block:
            chosen = cp
            continue
        # Same block. End-of-block returns the last checkpoint in this block.
        if cursor.is_end_of_block:
            chosen = cp
            continue
        if cp.transaction_index > target_tx:
            break
        if cp.transaction_index < target_tx:
            chosen = cp
            continue
        if cp.log_index > target_log:
            break
        chosen = cp
    return chosen


@dataclass(frozen=True, slots=True)
class MarketStateReader:
    """The dataset-addressed ``MarketState`` reader.

    The reader is bound to one :class:`ReplayInput` and one T100
    dataset identifier at construction time. Every :meth:`read` call
    projects the deterministic T040 checkpoint sequence onto a single
    cursor and returns the post-event :class:`MarketState`. The reader
    stores no per-run market-event copy and is reusable across runs
    that share the same dataset / pool identity.

    The reader is *sparse*: a checkpoint sequence is materialised once
    per :meth:`read` (T040 already does this deterministically) and the
    reader walks it to find the cursor's checkpoint. The reader never
    caches a per-cursor ``MarketState`` table.

    Construction validates the input descriptor; an inconsistent input
    is rejected with the matching error class.
    """

    version: str
    dataset_version: str
    pool_key_id: str
    replay_input: ReplayInput
    events: tuple[Any, ...]

    def __post_init__(self) -> None:
        if self.version != MARKET_STATE_VERSION:
            raise MarketStateError(
                f"MarketStateReader.version: must be {MARKET_STATE_VERSION!r}, got {self.version!r}"
            )
        if not isinstance(self.dataset_version, str) or not self.dataset_version:
            raise MarketStateError("MarketStateReader.dataset_version: must be non-empty str")
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise MarketStateError("MarketStateReader.pool_key_id: must be non-empty str")
        if not isinstance(self.replay_input, ReplayInput):
            raise MarketStateError("MarketStateReader.replay_input: must be ReplayInput")
        if not isinstance(self.events, tuple):
            raise MarketStateError(
                f"MarketStateReader.events: must be a tuple, got {type(self.events).__name__}"
            )

    @property
    def chain_id(self) -> int:
        return int(self.replay_input.chain_id.value)

    @property
    def from_block(self) -> int:
        return self.replay_input.from_block

    @property
    def to_block(self) -> int:
        return self.replay_input.to_block

    def read(self, cursor: MarketCursor) -> MarketState:
        """Return the :class:`MarketState` at ``cursor``.

        The function validates the cursor against the input window,
        replays the deterministic T040 checkpoint sequence, and
        selects the cursor's checkpoint. The reader never calls a
        strategy callback and never stores a per-cursor snapshot.
        """
        if not isinstance(cursor, MarketCursor):
            raise MarketStateError(
                f"read: cursor must be MarketCursor, got {type(cursor).__name__}"
            )
        if cursor.block_number < self.replay_input.from_block:
            raise CursorOutOfRangeError(
                f"read: cursor.block_number={cursor.block_number} precedes "
                f"input window from_block={self.replay_input.from_block}"
            )
        if cursor.block_number > self.replay_input.to_block:
            raise CursorOutOfRangeError(
                f"read: cursor.block_number={cursor.block_number} exceeds "
                f"input window to_block={self.replay_input.to_block}"
            )
        checkpoints = _replay_checkpoints(self.replay_input, self.events)
        chosen = _checkpoint_at(checkpoints, cursor)
        if chosen is None:
            return MarketState(
                version=MARKET_STATE_VERSION,
                dataset_version=self.dataset_version,
                pool_key_id=self.pool_key_id,
                cursor=cursor,
                checkpoint=None,
                tick_state=None,
                is_initialized=False,
                pre_initial_state=True,
            )
        return MarketState(
            version=MARKET_STATE_VERSION,
            dataset_version=self.dataset_version,
            pool_key_id=self.pool_key_id,
            cursor=cursor,
            checkpoint=chosen,
            tick_state=None,
            is_initialized=chosen.initialized,
            pre_initial_state=False,
        )


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def build_market_state_reader(
    *,
    replay_input: ReplayInput,
    dataset_version: str,
    pool_key_id: str,
    events: Iterable[Any],
) -> MarketStateReader:
    """Build a :class:`MarketStateReader` for the supplied input.

    The convenience builder validates that the ``pool_key_id`` matches
    the input's pool identity (the reader uses ``pool_id`` from the
    :class:`ReplayInput`; the ``pool_key_id`` is the hex digest the
    manifest binds to).
    """
    return MarketStateReader(
        version=MARKET_STATE_VERSION,
        dataset_version=dataset_version,
        pool_key_id=pool_key_id,
        replay_input=replay_input,
        events=tuple(events),
    )


__all__ = [
    "MARKET_STATE_VERSION",
    "MarketCursor",
    "MarketState",
    "MarketStateError",
    "MarketStateReader",
    "CursorOutOfRangeError",
    "CursorFormatError",
    "build_market_state_reader",
]
