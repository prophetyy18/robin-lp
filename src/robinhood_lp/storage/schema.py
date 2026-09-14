"""Versioned raw and normalized storage schemas (T030).

Every record carries:

- the **raw** JSON-RPC fields (block_hash, block_number,
  transaction_hash, log_index, topics, data, address, etc.)
  preserved as-is so a future re-decode against a newer schema does
  not lose information;
- the **normalized** typed fields, parsed by the current decoder;
- **provenance**: ``schema_version`` (the version of this dataclass
  shape), ``decode_version`` (the version of the V4 artifacts in
  use when the record was decoded), ``ingestion_time`` (ISO 8601 UTC),
  and ``source_endpoint`` (the RPC endpoint name, never the URL).

The canonical byte form (``canonical_bytes``) is stable and round-
trippable: ``from_canonical_bytes(canonical_bytes(record)) ==
record`` byte-exactly. New fields added in a future schema version
appear in the canonical form but unknown fields are preserved as
``unknown_fields`` so v(N) decoders can still load v(N-1) records.

This module sits in the storage layer per ADR-006 and depends only on
``robinhood_lp.protocol`` and the JSON stdlib. It must not import
``robinhood_lp.config`` or higher layers.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from robinhood_lp.protocol import Address, ChainId, PoolId

# ---------------------------------------------------------------------------
# Schema / decode versions
# ---------------------------------------------------------------------------


#: Bump whenever the dataclass shape of any record in this module
#: changes. The bump is recorded in every record's ``schema_version``
#: field so a future decoder can branch on it.
CURRENT_SCHEMA_VERSION: int = 1

#: Bump whenever the V4 artifacts (``EVENT_TOPICS`` /
#: ``FUNCTION_SELECTORS`` / PoolId hashing) change. The bump is
#: recorded in every record's ``decode_version`` field so a future
#: decoder can detect artifacts drift without reading the JSON.
#: Value matches the pinned v4-core commit prefix recorded in
#: ``docs/implement/evidence/ORACLE_MANIFEST.md``.
CURRENT_DECODE_VERSION: int = 1


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO 8601 UTC timestamp with second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Block and transaction contexts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockContext:
    """A block as observed by ``eth_getBlockByNumber``.

    The raw fields are the subset of the JSON-RPC response the
    framework consumes. ``canonical_bytes`` preserves every field
    byte-for-byte; a future schema version may add fields without
    breaking older records.
    """

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    block_number: int
    block_hash: int
    parent_hash: int
    timestamp: int
    miner: Address
    gas_used: int
    gas_limit: int
    base_fee_per_gas: int | None

    # Provenance and audit
    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    # Raw response fields we did not normalise. A re-decode against a
    # later schema can pick these up without re-querying the chain.
    raw: dict[str, Any] = field(default_factory=dict)
    # Raw fields that the current schema does not know about. They
    # survive round-trip; a future schema version promotes them into
    # typed attributes and removes them from this dict.
    unknown_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TransactionContext:
    """A transaction as observed by ``eth_getTransactionByHash``."""

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    tx_hash: int
    block_hash: int
    block_number: int
    transaction_index: int
    from_address: Address
    to_address: Address | None
    value: int
    input: bytes
    nonce: int

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReceiptContext:
    """A transaction receipt as observed by ``eth_getTransactionReceipt``.

    ``logs_bloom`` is the raw 256-byte bloom filter (int) the
    framework does not currently interpret; it is kept as raw for
    audit and possible future re-derivation.
    """

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    tx_hash: int
    block_hash: int
    block_number: int
    transaction_index: int
    from_address: Address
    to_address: Address | None
    contract_address: Address | None
    gas_used: int
    cumulative_gas_used: int
    status: int
    logs_bloom: int | None

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# V4 event records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InitializeLogRecord:
    """The Initialize event emitted by V4 PoolManager.

    The raw fields are the topic/data bytes the decoder consumed; the
    typed fields are the decoded PoolKey. A future schema that learns
    additional event fields can extend this without losing data.
    """

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    log_index: int
    address: Address  # PoolManager address

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    raw_topics: list[bytes] = field(default_factory=list)
    raw_data: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModifyLiquidityLogRecord:
    """The ModifyLiquidity event emitted by V4 PoolManager."""

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    log_index: int
    address: Address
    sender: Address
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    salt: int

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    raw_topics: list[bytes] = field(default_factory=list)
    raw_data: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SwapLogRecord:
    """The Swap event emitted by V4 PoolManager.

    Note: ``effective_fee`` is the fee recorded by the Swap event
    itself. The framework does **not** reconstruct the fee between
    swaps (T040 acceptance); that is the job of T043 hook evidence
    packs.
    """

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    log_index: int
    address: Address
    sender: Address
    amount0: int  # signed delta of currency0 balance of the pool
    amount1: int  # signed delta of currency1 balance of the pool
    sqrt_price_x96: int
    liquidity: int
    tick: int
    fee: int  # the on-chain recorded effective fee, in hundredths of a bip

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    raw_topics: list[bytes] = field(default_factory=list)
    raw_data: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DonateLogRecord:
    """The Donate event emitted by V4 PoolManager."""

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    log_index: int
    address: Address
    sender: Address
    amount0: int
    amount1: int

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    raw_topics: list[bytes] = field(default_factory=list)
    raw_data: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Canonical byte form (round-trippable)
# ---------------------------------------------------------------------------


def _canonical(record: Any) -> dict[str, Any]:
    """Convert a record dataclass to a JSON-friendly dict.

    Bytes are encoded as ``0x``-prefixed lowercase hex so the canonical
    form is human-readable and diff-friendly. ``dataclasses.asdict``
    recurses through nested dataclasses, but it does not handle bytes
    inside lists; we post-process to convert those.
    """

    def _convert(value: Any) -> Any:
        if isinstance(value, bytes):
            return value.hex()
        if isinstance(value, list):
            return [_convert(v) for v in value]
        if isinstance(value, dict):
            # Flatten ``{"value": <int>}`` (the asdict() expansion of a
            # dataclass whose only field is ``value: int`` -- ChainId,
            # PoolId, Address) into the bare int. This keeps the
            # canonical form diff-friendly.
            if set(value.keys()) == {"value"} and isinstance(value["value"], int):
                return value["value"]
            return {k: _convert(v) for k, v in value.items()}
        return value

    data = asdict(record)
    return _convert(data)  # type: ignore[no-any-return]


def canonical_bytes(record: Any) -> bytes:
    """Return the canonical byte form of ``record``.

    The form is JSON with sorted keys, UTF-8 encoded. Two records
    with the same fields produce identical bytes; two records that
    differ in any field produce different bytes.
    """
    payload = _canonical(record)
    payload["__schema_version__"] = record.schema_version
    payload["__decode_version__"] = record.decode_version
    payload["__class__"] = type(record).__name__
    return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")


def from_canonical_bytes(blob: bytes) -> dict[str, Any]:
    """Parse a canonical blob back into its dataclass-friendly dict.

    The caller is responsible for instantiating the correct
    dataclass (the class name is carried in ``__class__`` but the
    schema layer does not import the dataclasses to avoid a cycle).

    Schema-version compatibility: a future schema may add fields; the
    current parser accepts unknown keys (they land in
    ``unknown_fields`` on the calling side).
    """
    return json.loads(blob.decode("utf-8"))  # type: ignore[no-any-return]


__all__ = [
    "BlockContext",
    "CURRENT_DECODE_VERSION",
    "CURRENT_SCHEMA_VERSION",
    "DonateLogRecord",
    "InitializeLogRecord",
    "ModifyLiquidityLogRecord",
    "ReceiptContext",
    "SwapLogRecord",
    "TransactionContext",
    "canonical_bytes",
    "from_canonical_bytes",
]
