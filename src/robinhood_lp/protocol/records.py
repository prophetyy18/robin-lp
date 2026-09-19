"""V4 log-record dataclasses the reconstruction layer consumes (T007).

The :class:`InitializeLogRecord`, :class:`ModifyLiquidityLogRecord`,
:class:`SwapLogRecord`, :class:`DonateLogRecord` and
:class:`ProtocolFeeUpdatedLogRecord` dataclasses were originally
declared in :mod:`robinhood_lp.storage.schema`. Per ADR-006 §"Decision"
the reconstruction layer must consume injected read ports, not concrete
storage / RPC modules; a record type is a *protocol/domain* value
object the reconstruction layer and the storage adapter both depend on,
not a storage-side implementation detail.

This module sits in the protocol/domain layer (per the
``robinhood_lp.protocol`` package default in T006's layer map) and
carries the **record dataclasses only**. It has no I/O, no RPC, no
parsing, no canonical-byte / migration helpers, and no
:class:`AcquisitionProvenance` envelope handling — those concerns live
in :mod:`robinhood_lp.storage.schema`, which imports the dataclasses
from here and adds the storage-side machinery (raw envelopes, schema /
decode version migrations, canonical byte form, the class registry, the
observational ``AcquisitionProvenance``).

The contract for the record shapes is unchanged: every field, default,
ordering rule, and ``sort_key`` is preserved so the engine / replay
output is byte-identical before and after the move. The storage
adapter produces the same records for the same raw input because the
field set and field order are the same.

References:

- ADR-006 §"Decision" — reconstruction and features consume injected
  read ports, not concrete RPC / storage modules.
- T030 — versioned raw and normalized storage schemas.
"""

from __future__ import annotations

from dataclasses import dataclass

from robinhood_lp.protocol import Address, ChainId, EventKey, PoolId

# ---------------------------------------------------------------------------
# V4 event records (consumed by the reconstruction layer; produced by storage)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InitializeLogRecord:
    """The Initialize event emitted by V4 PoolManager.

    The raw fields are the topic/data bytes the decoder consumed; the
    typed fields are the decoded PoolKey. A future schema that learns
    additional event fields can extend this without losing data.

    The reconstruction layer treats the dataclass shape as the contract.
    Storage-side metadata (the ``schema_version`` class variable, the
    raw envelope, the ``AcquisitionProvenance``, decode / ingest
    versions) lives in the storage-side subclass
    :class:`robinhood_lp.storage.schema.InitializeLogRecord`.
    """

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    transaction_index: int
    log_index: int
    address: Address  # PoolManager address

    # ---- T035 / ADR-012: block header time + parent hash --------------
    # Both fields default to ``0`` so legacy v1 / v2 records migrate
    # forward without losing bytes; the runner enriches every freshly
    # persisted record from the dedup'd ``block_headers`` manifest
    # table before the partition writer commits.
    block_timestamp: int = 0
    parent_hash: int = 0

    # Re-org / removed flag from ``eth_getLogs``; True for log entries
    # the chain rolled back. Default False for fresh records.
    removed: bool = False

    def event_key(self) -> EventKey:
        """Return the T011 identity for this log entry."""
        return EventKey(
            chain_id=self.chain_id,
            block_hash=self.block_hash,
            tx_hash=self.transaction_hash,
            log_index=self.log_index,
        )

    def sort_key(self) -> tuple[int, int, int]:
        """Deterministic ordering: ``(block_number, transaction_index, log_index)``.

        T030 acceptance: same chain events are deterministically
        orderable; two forks at the same block number remain distinct
        via ``block_hash`` (EventKey), not via the sort key.
        """
        return (self.block_number, self.transaction_index, self.log_index)


@dataclass(frozen=True, slots=True)
class ModifyLiquidityLogRecord:
    """The ModifyLiquidity event emitted by V4 PoolManager."""

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    transaction_index: int
    log_index: int
    address: Address
    sender: Address
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    salt: int

    # ---- T035 / ADR-012: block header time + parent hash --------------
    block_timestamp: int = 0
    parent_hash: int = 0

    removed: bool = False

    def event_key(self) -> EventKey:
        return EventKey(
            chain_id=self.chain_id,
            block_hash=self.block_hash,
            tx_hash=self.transaction_hash,
            log_index=self.log_index,
        )

    def sort_key(self) -> tuple[int, int, int]:
        return (self.block_number, self.transaction_index, self.log_index)


@dataclass(frozen=True, slots=True)
class SwapLogRecord:
    """The Swap event emitted by V4 PoolManager.

    Note: ``fee`` is the fee recorded by the Swap event itself.
    The framework does **not** reconstruct the LP-owned protocol
    fee between swaps — that signal is carried separately by the
    inherited ``ProtocolFeeUpdatedLogRecord`` (ADR-010 §"Required
    data boundary") and reconstructed as evidence by T043.
    """

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    transaction_index: int
    log_index: int
    address: Address
    sender: Address
    amount0: int  # signed delta of currency0 balance of the pool
    amount1: int  # signed delta of currency1 balance of the pool
    sqrt_price_x96: int
    liquidity: int
    tick: int
    fee: int  # the on-chain recorded effective fee, in hundredths of a bip

    # ---- T035 / ADR-012: block header time + parent hash --------------
    block_timestamp: int = 0
    parent_hash: int = 0

    removed: bool = False

    def event_key(self) -> EventKey:
        return EventKey(
            chain_id=self.chain_id,
            block_hash=self.block_hash,
            tx_hash=self.transaction_hash,
            log_index=self.log_index,
        )

    def sort_key(self) -> tuple[int, int, int]:
        return (self.block_number, self.transaction_index, self.log_index)


@dataclass(frozen=True, slots=True)
class DonateLogRecord:
    """The Donate event emitted by V4 PoolManager."""

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    transaction_index: int
    log_index: int
    address: Address
    sender: Address
    amount0: int
    amount1: int

    # ---- T035 / ADR-012: block header time + parent hash --------------
    block_timestamp: int = 0
    parent_hash: int = 0

    removed: bool = False

    def event_key(self) -> EventKey:
        return EventKey(
            chain_id=self.chain_id,
            block_hash=self.block_hash,
            tx_hash=self.transaction_hash,
            log_index=self.log_index,
        )

    def sort_key(self) -> tuple[int, int, int]:
        return (self.block_number, self.transaction_index, self.log_index)


@dataclass(frozen=True, slots=True)
class ProtocolFeeUpdatedLogRecord:
    """The ``ProtocolFeeUpdated`` event inherited by V4 PoolManager.

    Declared in ``v4-core/src/interfaces/IProtocolFees.sol`` at the
    pinned commit ``e50237c43811bd9b526eff40f26772152a42daba`` and
    emitted by ``ProtocolFees.sol::setProtocolFee`` whenever the
    PoolManager's LP-owned protocol-fee accumulator changes.

    The recorded value is a packed ``uint24``: the high 12 bits
    encode the token-0 protocol fee and the low 12 bits encode the
    token-1 protocol fee (per the IProtocolFees ABI). The framework
    stores the integer exactly as emitted; downstream code splits
    it into the two halves as needed.

    ADR-010 requires this event because the fee in ``Swap`` is the
    *combined* swap fee, not automatically the LP-owned share.
    """

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    transaction_index: int
    log_index: int
    address: Address
    protocol_fee: int  # uint24 — packed [token0Fee:12 | token1Fee:12]

    # ---- T035 / ADR-012: block header time + parent hash --------------
    block_timestamp: int = 0
    parent_hash: int = 0

    removed: bool = False

    def event_key(self) -> EventKey:
        return EventKey(
            chain_id=self.chain_id,
            block_hash=self.block_hash,
            tx_hash=self.transaction_hash,
            log_index=self.log_index,
        )

    def sort_key(self) -> tuple[int, int, int]:
        return (self.block_number, self.transaction_index, self.log_index)


__all__ = [
    "DonateLogRecord",
    "InitializeLogRecord",
    "ModifyLiquidityLogRecord",
    "ProtocolFeeUpdatedLogRecord",
    "SwapLogRecord",
]
