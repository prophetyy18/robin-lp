"""Replay input descriptor (T040).

A :class:`ReplayInput` is the contract-level identifier of one
per-pool dataset. It is the boundary at which the replay enforces:

- the chain id and PoolId of the dataset (one replay input = one
  pool; mixing events from a second pool is rejected at the first
  mismatching record);
- the data root from which the events are loaded (the contract
  says replay consumes the per-pool datasets T038 qualifies, one
  replay input per data root);
- the pinned finalized window (``from_block`` ≤ ``to_block``)
  spanned by the dataset. Events outside that window are rejected
  with :class:`WindowBoundsError` so a misidentified dataset
  cannot contribute to the checkpoint state;
- the *bootstrap snapshot* (initial ``sqrt_price_x96`` and
  ``tick``) the replay applies at the ``Initialize`` event.
  These values are not in the V4 ``Initialize`` log itself
  (V4 returns them from the ``initialize()`` call but does not
  emit them on chain); the replay accepts them from a
  StateView-style block-pinned read at the pool's ``Initialize``
  block. When ``initial_sqrt_price_x96`` is ``0`` the replay
  treats the pool as uninitialized until a ``Swap`` event arrives
  with non-zero ``sqrt_price_x96``; this is the natural
  representation of "no bootstrap snapshot supplied".

The data root is a path-like string; the replay does not interpret
it as a URL or load anything from disk at this layer
(see :func:`load_replay_events` for the data-root loader). The
superseded 2026-09-18 reference dataset
(``run-680e65f4a59842d98b1712a45280779d``) is **not** a replay
input: the contract forbids it, and the replay's input descriptor
does not provide a back door for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from robinhood_lp.protocol import ChainId, PoolId


@dataclass(frozen=True, slots=True)
class ReplayInput:
    """Identifies one per-pool dataset the replay consumes.

    Equality and hashing use every field; two replay inputs that
    differ only in the window or the bootstrap snapshot are not
    equal.
    """

    chain_id: ChainId
    pool_id: PoolId
    data_root: Path
    from_block: int
    to_block: int
    # Bootstrap snapshot. ``initial_sqrt_price_x96`` defaults to 0
    # so an uninitialized pool is the natural fallback; the replay
    # accepts a non-zero value when a block-pinned StateView read
    # is supplied. ``initial_tick`` defaults to 0 for the same
    # reason (the tick returned by V4's ``initialize()`` is
    # information the wire does not carry).
    initial_sqrt_price_x96: int = 0
    initial_tick: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise TypeError(
                f"ReplayInput.chain_id: must be ChainId, got {type(self.chain_id).__name__}"
            )
        if not isinstance(self.pool_id, PoolId):
            raise TypeError(
                f"ReplayInput.pool_id: must be PoolId, got {type(self.pool_id).__name__}"
            )
        if not isinstance(self.data_root, Path):
            raise TypeError(
                f"ReplayInput.data_root: must be pathlib.Path, got {type(self.data_root).__name__}"
            )
        for field_name in ("from_block", "to_block"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(
                    f"ReplayInput.{field_name}: must be int, got {type(value).__name__}"
                )
            if value < 0:
                raise ValueError(f"ReplayInput.{field_name}: must be non-negative, got {value}")
        if self.from_block > self.to_block:
            raise ValueError(
                f"ReplayInput.from_block={self.from_block} > "
                f"to_block={self.to_block}; the window must satisfy "
                f"from_block <= to_block"
            )
        if not isinstance(self.initial_sqrt_price_x96, int) or isinstance(
            self.initial_sqrt_price_x96, bool
        ):
            raise TypeError(
                f"ReplayInput.initial_sqrt_price_x96: must be int, "
                f"got {type(self.initial_sqrt_price_x96).__name__}"
            )
        if self.initial_sqrt_price_x96 < 0:
            raise ValueError(
                f"ReplayInput.initial_sqrt_price_x96: must be non-negative, "
                f"got {self.initial_sqrt_price_x96}"
            )
        if self.initial_sqrt_price_x96 >= (1 << 160):
            raise ValueError(
                f"ReplayInput.initial_sqrt_price_x96: exceeds uint160, "
                f"got {self.initial_sqrt_price_x96}"
            )
        if not isinstance(self.initial_tick, int) or isinstance(self.initial_tick, bool):
            raise TypeError(
                f"ReplayInput.initial_tick: must be int, got {type(self.initial_tick).__name__}"
            )
        # V4 TickMath accepts any int24 for ``getTickAtSqrtPrice``;
        # we mirror that domain here.
        if self.initial_tick < -(1 << 23) or self.initial_tick >= (1 << 23):
            raise ValueError(
                f"ReplayInput.initial_tick: out of int24 range, got {self.initial_tick}"
            )

    @property
    def window_blocks(self) -> int:
        """Return the inclusive number of blocks the window spans."""
        return self.to_block - self.from_block + 1


__all__ = ["ReplayInput"]
