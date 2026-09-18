"""Deterministic event replay (T040) and tick-liquidity reconstruction (T041).

This package sits in the reconstruction layer (ADR-006 §2.1). It
replays a per-pool dataset of typed V4 events into a deterministic
sequence of checkpoints (T040) and reconstructs the per-tick
state the V4 pool carries for a pinned finalized window (T041).
Chunking, ingestion order, or restart boundaries cannot change the
observable pool state produced from the same input set.

Public surface:

- :class:`ReplayInput` identifies the per-pool dataset (chain id,
  PoolId, data root, and pinned finalized window). One replay input
  is one pool; mixing two pools' events in one input is rejected.
- :class:`ReplayOutput` is the replay result: the deterministic
  sequence of :class:`PoolCheckpoint` records, the final checkpoint,
  and the exact event count the checkpoint sequence observed.
- :class:`PoolCheckpoint` is a frozen snapshot of the observable
  pool state at the point immediately after the replayed event that
  produced it. Checkpoints carry every integer a downstream
  consumer (T051 in particular) needs to separate protocol fees
  from LP fee growth with pinned V4 rounding.
- :func:`replay` is the canonical entry point. It accepts any
  iterable of typed event records and returns a :class:`ReplayOutput`.
- :class:`Replayer` is the same algorithm with extra hooks for
  tests that need to inspect intermediate state.
- :func:`reconstruct_tick_liquidity` is the T041 entry point: it
  takes a pool's typed event stream and a ``PoolKey`` and produces
  a :class:`ReconstructedPoolTickState` carrying the per-tick
  ``TickInfo``, the tick bitmap, the active liquidity, the crossing
  log and the position-key set. The reconstruction is per-pool,
  scoped to the pool's pinned finalized window, and never merged
  across pools.

Replay is float-free. It depends on the protocol package
(``ChainId`` / ``PoolId`` / ``PoolKey`` / ``EventKey``), the
storage package (typed log records), and the math package (tick /
sqrt-price). It does not import RPC, configuration, or execution.
"""

from __future__ import annotations

from robinhood_lp.replay.checkpoint import PoolCheckpoint
from robinhood_lp.replay.errors import (
    DuplicateEventError,
    ImpossibleTransitionError,
    MissingTransactionIndexError,
    ReplayError,
    UnknownEventTypeError,
    UnknownPoolError,
    WindowBoundsError,
)
from robinhood_lp.replay.input import ReplayInput
from robinhood_lp.replay.output import ReplayOutput, replay_output_fingerprint
from robinhood_lp.replay.protocol_fee import (
    pack_protocol_fee,
    unpack_protocol_fee,
)
from robinhood_lp.replay.replayer import Replayer, replay
from robinhood_lp.replay.ticks import (
    CROSSING_ONE_FOR_ZERO,
    CROSSING_ZERO_FOR_ONE,
    MAX_LIQUIDITY,
    VALID_CROSSING_DIRECTIONS,
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
    add_liquidity,
    compress,
    least_significant_bit,
    most_significant_bit,
    position,
    reconstruct_tick_liquidity,
    tick_spacing_to_max_liquidity_per_tick,
)

__all__ = [
    "CROSSING_ONE_FOR_ZERO",
    "CROSSING_ZERO_FOR_ONE",
    "DuplicateEventError",
    "ImpossibleTransitionError",
    "LiquidityOverflowError",
    "MAX_LIQUIDITY",
    "MissingTransactionIndexError",
    "PoolCheckpoint",
    "PositionKey",
    "ReconstructedPoolTickState",
    "ReplayError",
    "ReplayInput",
    "ReplayOutput",
    "Replayer",
    "TickBitmap",
    "TickCrossing",
    "TickInfo",
    "TickLiquidityError",
    "TickLiquidityOverflowError",
    "TickMisalignedError",
    "TickOutOfBoundsError",
    "TicksMisorderedError",
    "UnknownEventTypeError",
    "UnknownPoolError",
    "VALID_CROSSING_DIRECTIONS",
    "WindowBoundsError",
    "add_liquidity",
    "compress",
    "least_significant_bit",
    "most_significant_bit",
    "pack_protocol_fee",
    "position",
    "reconstruct_tick_liquidity",
    "replay",
    "replay_output_fingerprint",
    "tick_spacing_to_max_liquidity_per_tick",
    "unpack_protocol_fee",
]
