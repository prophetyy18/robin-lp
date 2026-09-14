"""Block, transaction, and raw event identity (T011).

Identity rules (per ``docs/spec/protocol/PROTOCOL_FACTS.md`` and todo/README.md T011):

- every observation carries its ``ChainId``; ``(chain_id, ...)`` is the
  global namespace and prevents collisions across chains;
- ``BlockRef`` is identified by ``(chain_id, block_hash)``; the block
  number is a display/lookup field that may be reused across forks;
- ``TransactionRef`` is identified by ``(chain_id, tx_hash)``;
- ``EventKey`` is identified by
  ``(chain_id, block_hash, tx_hash, log_index)`` — every field is
  required. ``(tx_hash, log_index)`` alone is **not** sufficient
  because it collides across chains and across reorgs;
- the canonical/orphaned/removed status is part of the *event record*
  (carried alongside the key), not part of the key itself: an
  orphaned event still identifies the same log index, and rewriting
  the key would discard audit evidence;
- token metadata is *display*; an ERC-20 with missing symbol or
  decimals remains representable and keeps its identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from robinhood_lp.protocol.ids import Address, ChainId

BLOCK_HASH_BYTES: Final[int] = 32
TX_HASH_BYTES: Final[int] = 32


def _require_uint(value: int, *, bits: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field}: must be non-negative, got {value}")
    if value >= (1 << bits):
        raise ValueError(f"{field}: exceeds {bits}-bit width, got {value}")
    return value


def _hash_bytes(value: int, *, expected_bytes: int, field: str) -> bytes:
    """Validate a 32-byte hash value and return its big-endian bytes."""
    _require_uint(value, bits=expected_bytes * 8, field=field)
    return value.to_bytes(expected_bytes, "big")


# ---------------------------------------------------------------------------
# BlockRef
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockRef:
    """A canonical reference to a block.

    Identity is ``(chain_id, block_hash)``; the block number is an
    optional lookup/display field. Two blocks with the same number on
    different forks have different hashes and therefore different
    identities. The number may be ``None`` when only the hash is
    known (e.g. when a transaction receipt carries a block hash but
    the caller has not yet resolved it to a number).
    """

    chain_id: ChainId
    block_hash: int
    block_number: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise TypeError(
                f"BlockRef.chain_id: must be ChainId, got {type(self.chain_id).__name__}"
            )
        _require_uint(self.block_hash, bits=BLOCK_HASH_BYTES * 8, field="BlockRef.block_hash")
        if self.block_number is not None:
            if not isinstance(self.block_number, int) or isinstance(self.block_number, bool):
                raise TypeError(
                    f"BlockRef.block_number: must be int or None, got {type(self.block_number).__name__}"
                )
            if self.block_number < 0:
                raise ValueError(
                    f"BlockRef.block_number: must be non-negative, got {self.block_number}"
                )

    def to_hash_bytes(self) -> bytes:
        return self.block_hash.to_bytes(BLOCK_HASH_BYTES, "big")

    def to_hash_hex(self) -> str:
        return "0x" + self.to_hash_bytes().hex()

    def __repr__(self) -> str:
        return (
            f"BlockRef(chain={self.chain_id.value}, "
            f"number={self.block_number}, hash={self.to_hash_hex()})"
        )


# ---------------------------------------------------------------------------
# TransactionRef
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransactionRef:
    """A canonical reference to a transaction.

    Identity is ``(chain_id, tx_hash)``. The transaction's *block* is
    part of the containing ``EventKey`` rather than this reference,
    because the same transaction cannot appear in two blocks on a
    single chain (a reorg that drops the block also drops the tx).
    """

    chain_id: ChainId
    tx_hash: int

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise TypeError(
                f"TransactionRef.chain_id: must be ChainId, got {type(self.chain_id).__name__}"
            )
        _require_uint(self.tx_hash, bits=TX_HASH_BYTES * 8, field="TransactionRef.tx_hash")

    def to_hash_bytes(self) -> bytes:
        return self.tx_hash.to_bytes(TX_HASH_BYTES, "big")

    def to_hash_hex(self) -> str:
        return "0x" + self.to_hash_bytes().hex()

    def __repr__(self) -> str:
        return f"TransactionRef(chain={self.chain_id.value}, hash={self.to_hash_hex()})"


# ---------------------------------------------------------------------------
# CanonicalStatus
# ---------------------------------------------------------------------------


class CanonicalStatus(StrEnum):
    """Where a block, transaction, or event sits in the chain history.

    These are part of the *record* carried alongside an identity, not
    the identity itself; see module docstring.
    """

    CANONICAL = "canonical"
    ORPHANED = "orphaned"  # once part of canonical chain, displaced by reorg
    REMOVED = "removed"  # explicitly discarded (replaced or never canonical)


# ---------------------------------------------------------------------------
# EventKey
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventKey:
    """Identity of a single EVM log entry.

    Identity is the 5-tuple
    ``(chain_id, block_hash, tx_hash, log_index)``. The chain id is
    mandatory; using ``(tx_hash, log_index)`` alone is rejected by the
    constructor. ``log_index`` is the position of the log within the
    transaction's receipt (zero-based).
    """

    chain_id: ChainId
    block_hash: int
    tx_hash: int
    log_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise TypeError(
                f"EventKey.chain_id: must be ChainId, got {type(self.chain_id).__name__}"
            )
        _require_uint(self.block_hash, bits=BLOCK_HASH_BYTES * 8, field="EventKey.block_hash")
        _require_uint(self.tx_hash, bits=TX_HASH_BYTES * 8, field="EventKey.tx_hash")
        if not isinstance(self.log_index, int) or isinstance(self.log_index, bool):
            raise TypeError(f"EventKey.log_index: must be int, got {type(self.log_index).__name__}")
        if self.log_index < 0:
            raise ValueError(f"EventKey.log_index: must be non-negative, got {self.log_index}")

    def block_ref(self) -> BlockRef:
        """Return the block reference for this event.

        The block number is left ``None`` here because an EventKey
        does not carry one; callers that know the number can pass it
        to ``BlockRef(chain_id=..., block_hash=..., block_number=N)``
        explicitly.
        """
        return BlockRef(
            chain_id=self.chain_id,
            block_hash=self.block_hash,
            block_number=None,
        )

    def transaction_ref(self) -> TransactionRef:
        """Return the transaction reference for this event."""
        return TransactionRef(chain_id=self.chain_id, tx_hash=self.tx_hash)

    def __repr__(self) -> str:
        return (
            f"EventKey(chain={self.chain_id.value}, "
            f"block=0x{self.block_hash.to_bytes(32, 'big').hex()}, "
            f"tx=0x{self.tx_hash.to_bytes(32, 'big').hex()}, "
            f"log_index={self.log_index})"
        )


# ---------------------------------------------------------------------------
# Token metadata (display only; never part of identity)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Display-only metadata for an ERC-20-like token (T011).

    Identity is the token's contract address on a specific chain;
    this record is metadata that may be partial or absent. The
    framework must continue to work when symbol or decimals are
    missing — see ``ADM-TECH-002``.
    """

    chain_id: ChainId
    address: Address
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise TypeError(
                f"TokenMetadata.chain_id: must be ChainId, got {type(self.chain_id).__name__}"
            )
        if not isinstance(self.address, Address):
            raise TypeError(
                f"TokenMetadata.address: must be Address, got {type(self.address).__name__}"
            )
        if self.symbol is not None and not isinstance(self.symbol, str):
            raise TypeError(
                f"TokenMetadata.symbol: must be str or None, got {type(self.symbol).__name__}"
            )
        if self.name is not None and not isinstance(self.name, str):
            raise TypeError(
                f"TokenMetadata.name: must be str or None, got {type(self.name).__name__}"
            )
        if self.decimals is not None:
            if not isinstance(self.decimals, int) or isinstance(self.decimals, bool):
                raise TypeError(
                    f"TokenMetadata.decimals: must be int or None, got {type(self.decimals).__name__}"
                )
            if not 0 <= self.decimals <= 255:
                raise ValueError(
                    f"TokenMetadata.decimals: must be in [0, 255], got {self.decimals}"
                )

    def is_complete(self) -> bool:
        """A complete record has symbol, name, and decimals populated."""
        return self.symbol is not None and self.name is not None and self.decimals is not None

    def __repr__(self) -> str:
        sym = self.symbol if self.symbol is not None else "<unknown>"
        return f"TokenMetadata(chain={self.chain_id.value}, addr={self.address.to_hex()}, symbol={sym})"


__all__ = [
    "BLOCK_HASH_BYTES",
    "BlockRef",
    "CanonicalStatus",
    "EventKey",
    "TokenMetadata",
    "TX_HASH_BYTES",
    "TransactionRef",
]
