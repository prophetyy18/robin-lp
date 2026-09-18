"""Replay checkpoint (T040).

A :class:`PoolCheckpoint` is a frozen snapshot of the observable
pool state at the point *immediately after* the replayed event
that produced it. It carries:

- the chain id and PoolId (so checkpoints from two pools are not
  confusable);
- the block number, block hash, transaction index, and log index
  of the producing event (so checkpoints can be cross-referenced
  with the input EventKey);
- the event-type name (one of ``Initialize``, ``ModifyLiquidity``,
  ``Swap``, ``Donate``, ``ProtocolFeeUpdated``);
- the protocol-level observable state: ``sqrt_price_x96``, ``tick``,
  ``active_liquidity``, cumulative swap volumes per token, and the
  directional protocol fee state;
- the ``initialized`` flag so the zero-state pre-Initialize
  checkpoint is observable (zero protocol-fee initialization state
  is one of the pinned core vectors);
- the per-swap effective combined fee (``last_swap_fee``) and the
  pool's declared fee (``pool_fee``). For dynamic-fee pools
  ``pool_fee`` is the sentinel ``DYNAMIC_FEE_FLAG`` and the
  effective fee is taken from each Swap event byte-for-byte; the
  replay does **not** reconstruct unobserved between-swap fee
  updates, it preserves the emitted value.

The checkpoint is the smallest object downstream consumers (T051
in particular) need to separate protocol fees from LP fee growth
under pinned V4 rounding semantics: every integer field that the
separation needs is preserved with no loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from robinhood_lp.protocol import ChainId, PoolId

#: Sentinel event type names. The replay accepts only events whose
#: ``event_name`` field equals one of these strings. Other values
#: raise :class:`UnknownEventTypeError`.
EVENT_TYPE_INITIALIZE: Final[str] = "Initialize"
EVENT_TYPE_MODIFY_LIQUIDITY: Final[str] = "ModifyLiquidity"
EVENT_TYPE_SWAP: Final[str] = "Swap"
EVENT_TYPE_DONATE: Final[str] = "Donate"
EVENT_TYPE_PROTOCOL_FEE_UPDATED: Final[str] = "ProtocolFeeUpdated"

VALID_EVENT_TYPES: Final[tuple[str, ...]] = (
    EVENT_TYPE_INITIALIZE,
    EVENT_TYPE_MODIFY_LIQUIDITY,
    EVENT_TYPE_SWAP,
    EVENT_TYPE_DONATE,
    EVENT_TYPE_PROTOCOL_FEE_UPDATED,
)


@dataclass(frozen=True, slots=True)
class PoolCheckpoint:
    """A deterministic snapshot of the observable pool state.

    Two checkpoints are equal iff every field is equal. Hashing
    follows the dataclass identity. Ordering is intentionally
    lexicographic on ``(block_number, transaction_index, log_index,
    event_type)`` so a checkpoint sequence can be diffed and
    re-ordered externally without losing meaning.
    """

    chain_id: ChainId
    pool_id: PoolId

    # Event-position fields (the producing event's identity).
    block_number: int
    block_hash: int
    transaction_index: int
    log_index: int
    event_type: str

    # Protocol-level observable state.
    sqrt_price_x96: int
    tick: int
    active_liquidity: int
    cumulative_volume0: int
    cumulative_volume1: int

    # Directional protocol-fee state (both halves unpacked).
    protocol_fee_token0: int
    protocol_fee_token1: int

    # Initialization flag; True once an Initialize event has been
    # observed for this pool. The pre-Initialize zero-state
    # checkpoint carries False.
    initialized: bool

    # The pool's declared fee from the PoolKey (uint24 or the
    # DYNAMIC_FEE_FLAG sentinel). Preserved on every checkpoint
    # so dynamic-fee detection is unambiguous downstream.
    pool_fee: int

    # The effective combined swap fee emitted by the most recent
    # Swap event (uint24). ``None`` before the first Swap. The
    # replay does not reconstruct between-swap fee updates; it
    # only preserves the value the chain emitted on each Swap.
    last_swap_fee: int | None

    # The per-event raw fields downstream consumers (T051) need to
    # separate protocol fees from LP fee growth with pinned V4
    # rounding semantics. Both are the integer wire values the
    # event carried (not converted via display decimals).
    #
    # ``event_swap_fee`` is the fee field of the producing Swap
    # event when ``event_type == "Swap"``; ``None`` otherwise.
    # ``event_protocol_fee_packed`` is the packed uint24 from the
    # producing ProtocolFeeUpdated event when ``event_type ==
    # "ProtocolFeeUpdated"``; ``None`` otherwise.
    event_swap_fee: int | None
    event_protocol_fee_packed: int | None


__all__ = [
    "EVENT_TYPE_DONATE",
    "EVENT_TYPE_INITIALIZE",
    "EVENT_TYPE_MODIFY_LIQUIDITY",
    "EVENT_TYPE_PROTOCOL_FEE_UPDATED",
    "EVENT_TYPE_SWAP",
    "PoolCheckpoint",
    "VALID_EVENT_TYPES",
]
