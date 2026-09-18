"""Replay output (T040).

A :class:`ReplayOutput` is the deterministic result of replaying
one :class:`ReplayInput`. It carries:

- the input descriptor (so a downstream consumer never has to
  cross-reference another table to know which pool / data root /
  window the checkpoint sequence belongs to);
- the ordered sequence of :class:`PoolCheckpoint` records, one
  per replayed event. Two replay outputs produced from the same
  input descriptor and the same set of events must produce
  checkpoint sequences whose :func:`replay_output_fingerprint`
  values match byte-for-byte;
- the final checkpoint, exposed separately because downstream
  consumers (T041 in particular) only need the terminal state;
- the exact event count the checkpoint sequence observed, so
  coverage evidence can be cross-checked against the input.

The output is frozen and hashable. Hashing follows the dataclass
identity and is therefore consistent with equality.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from robinhood_lp.replay.checkpoint import PoolCheckpoint

if TYPE_CHECKING:
    from robinhood_lp.replay.input import ReplayInput


@dataclass(frozen=True, slots=True)
class ReplayOutput:
    """The deterministic result of replaying one per-pool dataset.

    Two :class:`ReplayOutput` objects are equal iff every field is
    equal — including the checkpoint sequence. The
    :func:`replay_output_fingerprint` helper exposes a content hash
    for cross-host comparison.
    """

    input: ReplayInput
    checkpoints: tuple[PoolCheckpoint, ...]
    event_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.checkpoints, tuple):
            # The replay always produces a tuple of PoolCheckpoint;
            # refuse accidental lists so equality/hashing stay stable.
            raise TypeError(
                f"ReplayOutput.checkpoints: must be a tuple, got {type(self.checkpoints).__name__}"
            )
        if self.event_count != len(self.checkpoints):
            raise ValueError(
                f"ReplayOutput.event_count={self.event_count} does not "
                f"match len(checkpoints)={len(self.checkpoints)}"
            )

    @property
    def final_checkpoint(self) -> PoolCheckpoint:
        """Return the terminal checkpoint, or the zero-state sentinel
        when no events were replayed.

        The zero-state sentinel is the :class:`PoolCheckpoint`
        constructed directly from the replay's initial state; it
        carries ``initialized=False`` and zero protocol fees, so a
        consumer comparing final states never sees a missing field.
        """
        if not self.checkpoints:
            raise LookupError(
                "ReplayOutput has no checkpoints; cannot return a final "
                "checkpoint from an empty replay"
            )
        return self.checkpoints[-1]

    @property
    def is_empty(self) -> bool:
        """True iff the replay consumed zero events.

        A run against an empty dataset is permitted (the contract
        requires the replay to be deterministic, including the
        deterministic empty case); the operator runbook can branch
        on this property without re-querying the input.
        """
        return self.event_count == 0


def _canonical_payload(output: ReplayOutput) -> dict[str, object]:
    """Return the JSON-friendly canonical payload of ``output``.

    The form is stable: two :class:`ReplayOutput` instances
    produced from the same input descriptor and the same event set
    produce identical payloads, regardless of chunking order or
    restart boundaries.
    """

    replay_input: ReplayInput = output.input
    checkpoints: list[dict[str, object]] = []
    for cp in output.checkpoints:
        checkpoints.append(
            {
                "chain_id": cp.chain_id.value,
                "pool_id": cp.pool_id.value,
                "block_number": cp.block_number,
                "block_hash": cp.block_hash,
                "transaction_index": cp.transaction_index,
                "log_index": cp.log_index,
                "event_type": cp.event_type,
                "sqrt_price_x96": cp.sqrt_price_x96,
                "tick": cp.tick,
                "active_liquidity": cp.active_liquidity,
                "cumulative_volume0": cp.cumulative_volume0,
                "cumulative_volume1": cp.cumulative_volume1,
                "protocol_fee_token0": cp.protocol_fee_token0,
                "protocol_fee_token1": cp.protocol_fee_token1,
                "initialized": cp.initialized,
                "pool_fee": cp.pool_fee,
                "last_swap_fee": cp.last_swap_fee,
                "event_swap_fee": cp.event_swap_fee,
                "event_protocol_fee_packed": cp.event_protocol_fee_packed,
            }
        )
    return {
        "chain_id": replay_input.chain_id.value,
        "pool_id": replay_input.pool_id.value,
        "data_root": str(replay_input.data_root),
        "from_block": replay_input.from_block,
        "to_block": replay_input.to_block,
        "event_count": output.event_count,
        "checkpoints": checkpoints,
    }


def replay_output_fingerprint(output: ReplayOutput) -> str:
    """Return the SHA-256 fingerprint of ``output`` as ``0x...`` hex.

    The fingerprint covers every checkpoint field plus the input
    descriptor, with JSON keys sorted so the form is stable across
    Python versions and platforms. Two replay outputs that produce
    byte-for-byte identical checkpoints produce identical
    fingerprints regardless of how the input events were chunked,
    shuffled, or replayed in pieces.
    """
    payload = _canonical_payload(output)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "0x" + hashlib.sha256(blob).hexdigest()


__all__ = ["ReplayOutput", "replay_output_fingerprint"]
