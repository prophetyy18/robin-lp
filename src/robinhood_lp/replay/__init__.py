"""Deterministic event replay (T040).

This package sits in the reconstruction layer (ADR-006 §2.1). It
replays a per-pool dataset of typed V4 events into a deterministic
sequence of checkpoints. Chunking, ingestion order, or restart
boundaries cannot change the observable pool state produced from the
same input set.

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
- :func:`load_replay_events` reads a per-pool dataset from the
  qualified partitions under a data root. It uses the
  :mod:`robinhood_lp.storage` reader and never touches the
  superseded 2026-09-18 reference dataset (``run-680e65f4...``):
  the contract that inputs must be one pool's qualified dataset
  is enforced at the :class:`ReplayInput` boundary.

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

__all__ = [
    "DuplicateEventError",
    "ImpossibleTransitionError",
    "MissingTransactionIndexError",
    "PoolCheckpoint",
    "ReplayError",
    "ReplayInput",
    "ReplayOutput",
    "Replayer",
    "UnknownEventTypeError",
    "UnknownPoolError",
    "WindowBoundsError",
    "pack_protocol_fee",
    "replay",
    "replay_output_fingerprint",
    "unpack_protocol_fee",
]
