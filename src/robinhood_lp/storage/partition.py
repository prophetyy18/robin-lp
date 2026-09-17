"""Parquet physical layout for append-only raw partitions (T031).

This module implements the **physical layout** half of T031. The logical
schema (record dataclasses, canonical bytes, normalized content hash)
lives in :mod:`robinhood_lp.storage.schema` and is owned by T030; the
columns, partition directory layout, and Parquet schema definitions in
this module are T031's responsibility and are justified by ADR-002
(append-only raw partitions, typed columns, schema/decode versioning)
and ADR-010 (raw acquisition envelope kept apart from normalized event
identity and normalized content hash).

Layout
------

A partition is identified by ``(chain_id, contract_address, event_name,
block_range)``. On disk it lives at::

    <data_root>/raw/
        chain=<chain_id>/
            contract=<contract_hex>/
                event=<event_name>/
                    range=<block_from>-<block_to>/
                        data.parquet
                        .staging-<uuid>.parquet   (transient)

The manifest database (``<data_root>/manifest.sqlite``) is the single
source of truth for which partitions exist, how many rows they hold,
and what their per-file SHA-256 checksum is. Filenames are not
trusted: a partition is only visible to the reader if the manifest row
points at an existing file whose checksum matches.

The ``.staging-<uuid>.parquet`` suffix is a write-time detail. The
reader never enumerates the directory; it only resolves partitions
listed in the manifest.

Parquet schema
--------------

Every row carries the typed identity fields, the per-event typed
fields, the original raw ``topics`` / ``data`` / JSON-RPC response
wrapper, the structured acquisition envelope, the schema/decode
versions, and the SHA-256 of the T011 ``EventKey`` plus the
``normalized_content_hash`` so dedup queries do not have to deserialise
the row to check identity.

The Parquet schema is per event name. The shared columns are common
across every event; the typed fields differ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]

from robinhood_lp.protocol import Address, ChainId

# ---------------------------------------------------------------------------
# Canonical event names
# ---------------------------------------------------------------------------

#: The set of V4 event names the framework persists. Mirrors the keys
#: of :data:`robinhood_lp.protocol.abi_artifacts.EVENT_TOPICS` and the
#: record classes in :mod:`robinhood_lp.storage.schema`.
EVENT_NAMES: Final[tuple[str, ...]] = (
    "Donate",
    "Initialize",
    "ModifyLiquidity",
    "ProtocolFeeUpdated",
    "Swap",
)

#: Hex of ``0x``-prefixed ``contract_address`` characters not allowed in
#: a partition path component. The contract address is already
#: validated to be a 20-byte integer by :class:`Address`, so the only
#: thing we forbid here is non-hex / non-canonical forms the caller
#: might try to inject.
_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")


def _contract_slug(address: Address) -> str:
    """Return the path-safe slug for a contract address.

    Lowercase, ``0x``-stripped, 40 hex characters. The ``Address`` type
    already validates the underlying integer; this just produces the
    stable path component.
    """
    return address.to_hex().removeprefix("0x").lower()


def _chain_slug(chain_id: ChainId) -> str:
    """Return the path-safe slug for a chain id."""
    return str(chain_id.value)


def _check_block_range(block_from: int, block_to: int) -> None:
    """Validate the inclusive block range."""
    if not isinstance(block_from, int) or isinstance(block_from, bool):
        raise TypeError(f"block_from: must be int, got {type(block_from).__name__}")
    if not isinstance(block_to, int) or isinstance(block_to, bool):
        raise TypeError(f"block_to: must be int, got {type(block_to).__name__}")
    if block_from < 0:
        raise ValueError(f"block_from: must be non-negative, got {block_from}")
    if block_to < block_from:
        raise ValueError(f"block range: block_from {block_from} > block_to {block_to}")


def _check_event_name(event_name: str) -> None:
    if not isinstance(event_name, str):
        raise TypeError(f"event_name: must be str, got {type(event_name).__name__}")
    if event_name not in EVENT_NAMES:
        raise ValueError(f"event_name: must be one of {EVENT_NAMES!r}, got {event_name!r}")


# ---------------------------------------------------------------------------
# Partition key
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartitionKey:
    """Identity of one append-only Parquet partition.

    ADR-002 requires partitioning by
    ``(chain_id, contract_address, event_name, block_range)``. The
    block range is inclusive on both ends. The partition path on disk
    is derived deterministically from these fields.
    """

    chain_id: ChainId
    contract_address: Address
    event_name: str
    block_from: int
    block_to: int

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise TypeError(
                f"PartitionKey.chain_id: must be ChainId, got {type(self.chain_id).__name__}"
            )
        if not isinstance(self.contract_address, Address):
            raise TypeError(
                f"PartitionKey.contract_address: must be Address, "
                f"got {type(self.contract_address).__name__}"
            )
        _check_event_name(self.event_name)
        _check_block_range(self.block_from, self.block_to)

    # ----- Path derivation ------------------------------------------------

    def partition_id(self) -> str:
        """Stable identifier used as the primary key in the manifest."""
        return (
            f"chain={_chain_slug(self.chain_id)}/"
            f"contract={_contract_slug(self.contract_address)}/"
            f"event={self.event_name}/"
            f"range={self.block_from}-{self.block_to}"
        )

    def partition_dir(self, data_root: Any) -> Any:
        """Return the on-disk directory for this partition.

        ``data_root`` is accepted as ``Any`` so the storage layer does
        not pin itself to a specific pathlib flavour; in practice the
        caller passes ``pathlib.Path``.
        """
        from pathlib import Path

        root = Path(data_root)
        return root / "raw" / self.partition_id()

    def data_file_path(self, data_root: Any) -> Any:
        """Return the final on-disk file path for this partition."""
        from pathlib import Path

        return Path(self.partition_dir(data_root)) / "data.parquet"

    # ----- Comparison ----------------------------------------------------

    def __str__(self) -> str:
        return self.partition_id()


# ---------------------------------------------------------------------------
# Parquet schema (per event)
# ---------------------------------------------------------------------------

#: Shared columns present in every event partition. The per-event
#: typed columns are appended after these. Keeping the shared columns
#: in one place makes it trivial to compare the schemas across event
#: names and to reason about reader behaviour.
#:
#: Integer widths follow the V4 ABI widths exactly:
#:
#: - block / transaction hashes: 32-byte fixed binary (uint256);
#: - pool id, event_key_hash, content_hash, salt, currency: 32-byte
#:   fixed binary (uint256 or bytes32);
#: - sender / hooks / address (PoolManager): 20-byte fixed binary
#:   (address);
#: - block_number / chain_id: int64 (uint64 is enough for V1 horizon
#:   and int64 plays nicely with downstream tooling);
#: - log_index / transaction_index: int32 (single-receipt counts fit);
#: - fee / protocol_fee: uint32 (uint24 stored as uint32 for padding);
#: - tick_lower / tick_upper / tick: int32 (signed 24-bit in the ABI,
#:   stored as int32 for safety);
#: - amount0 / amount1 / liquidity_delta: 32-byte fixed binary
#:   (int256 in the ABI);
#: - sqrt_price_x96: 12-byte fixed binary (uint96 in the ABI);
#: - liquidity: 16-byte fixed binary (uint128 in the ABI).
#:
#: The fixed-width binary choice is deliberate: Parquet has no native
#: uint128 / int256 type and a Python int that overflows uint64 must
#: not be silently coerced. Storing the canonical fixed-width bytes
#: preserves the exact ABI value round-trip.
HASH_BYTES: Final[int] = 32
ADDRESS_BYTES: Final[int] = 20
UINT256_BYTES: Final[int] = 32
UINT160_BYTES: Final[int] = 20
UINT128_BYTES: Final[int] = 16


SHARED_PARQUET_FIELDS: Final[tuple[pa.Field, ...]] = (
    # ---- Raw evidence (preserved byte-for-byte) ----------------------
    pa.field("raw_topic_0", pa.binary(HASH_BYTES)),
    pa.field("raw_topic_1", pa.binary(HASH_BYTES)),
    pa.field("raw_topic_2", pa.binary(HASH_BYTES)),
    pa.field("raw_topic_3", pa.binary(HASH_BYTES)),
    pa.field("raw_data", pa.binary()),
    pa.field("raw_response_json", pa.utf8()),
    # ---- Typed identity ---------------------------------------------
    pa.field("chain_id", pa.int64(), nullable=False),
    pa.field("block_number", pa.int64(), nullable=False),
    pa.field("block_hash", pa.binary(HASH_BYTES), nullable=False),
    # ---- Block header time / parent hash (T035, ADR-012) ------------
    # ``block_timestamp`` is uint64: the V4 / Ethereum block timestamp
    # is a UNIX-seconds integer. ``parent_hash`` is the 32-byte
    # previous-block hash. Both are required on every event row so a
    # downstream consumer can re-order events in chain time and
    # re-check the header against the stored block_hash without
    # touching the manifest header table.
    pa.field("block_timestamp", pa.uint64(), nullable=False),
    pa.field("parent_hash", pa.binary(HASH_BYTES), nullable=False),
    # ---- Transaction + receipt identity ------------------------------
    pa.field("transaction_hash", pa.binary(HASH_BYTES), nullable=False),
    pa.field("transaction_index", pa.int32(), nullable=False),
    pa.field("log_index", pa.int32(), nullable=False),
    pa.field("address", pa.binary(ADDRESS_BYTES), nullable=False),
    pa.field("pool_id", pa.binary(HASH_BYTES), nullable=False),
    pa.field("removed", pa.bool_(), nullable=False),
    # ---- Per-event discriminator ------------------------------------
    pa.field("event_name", pa.utf8(), nullable=False),
    # ---- Schema / decode versions (T030 contract) -------------------
    pa.field("schema_version", pa.int32(), nullable=False),
    pa.field("decode_version", pa.int32(), nullable=False),
    # ---- Acquisition envelope (observational, NOT part of identity) -
    pa.field("acquisition_endpoint_alias", pa.utf8()),
    pa.field("acquisition_retrieval_time", pa.utf8()),
    pa.field("acquisition_request_from_block", pa.int64()),
    pa.field("acquisition_request_to_block", pa.int64()),
    pa.field("acquisition_http_batch_size", pa.int32()),
    pa.field("acquisition_http_batch_position", pa.int32()),
    pa.field("acquisition_request_attempt", pa.int32()),
    # ---- Fast dedup columns (EventKey + content hash) ---------------
    pa.field("event_key_hash", pa.binary(HASH_BYTES), nullable=False),
    pa.field("content_hash", pa.binary(HASH_BYTES), nullable=False),
)


#: Per-event typed columns, keyed by event name. Every entry is the
#: set of fields *added on top of* :data:`SHARED_PARQUET_FIELDS` for
#: that event.
PER_EVENT_PARQUET_FIELDS: Final[dict[str, tuple[pa.Field, ...]]] = {
    "Initialize": (
        pa.field("currency0", pa.binary(HASH_BYTES), nullable=False),
        pa.field("currency1", pa.binary(HASH_BYTES), nullable=False),
        pa.field("fee", pa.uint32(), nullable=False),
        pa.field("tick_spacing", pa.int32(), nullable=False),
        pa.field("hooks", pa.binary(ADDRESS_BYTES), nullable=False),
        pa.field("sqrt_price_x96", pa.binary(UINT160_BYTES), nullable=False),
        pa.field("tick", pa.int32(), nullable=False),
    ),
    "ModifyLiquidity": (
        pa.field("sender", pa.binary(ADDRESS_BYTES), nullable=False),
        pa.field("tick_lower", pa.int32(), nullable=False),
        pa.field("tick_upper", pa.int32(), nullable=False),
        pa.field("liquidity_delta", pa.binary(UINT256_BYTES), nullable=False),
        pa.field("salt", pa.binary(HASH_BYTES), nullable=False),
    ),
    "Swap": (
        pa.field("sender", pa.binary(ADDRESS_BYTES), nullable=False),
        pa.field("amount0", pa.binary(UINT256_BYTES), nullable=False),
        pa.field("amount1", pa.binary(UINT256_BYTES), nullable=False),
        pa.field("sqrt_price_x96", pa.binary(UINT160_BYTES), nullable=False),
        pa.field("liquidity", pa.binary(UINT128_BYTES), nullable=False),
        pa.field("tick", pa.int32(), nullable=False),
        pa.field("fee", pa.uint32(), nullable=False),
    ),
    "Donate": (
        pa.field("sender", pa.binary(ADDRESS_BYTES), nullable=False),
        pa.field("amount0", pa.binary(UINT256_BYTES), nullable=False),
        pa.field("amount1", pa.binary(UINT256_BYTES), nullable=False),
    ),
    "ProtocolFeeUpdated": (pa.field("protocol_fee", pa.uint32(), nullable=False),),
}


def parquet_schema_for(event_name: str) -> pa.Schema:
    """Return the Parquet schema for ``event_name``.

    The schema is the union of :data:`SHARED_PARQUET_FIELDS` and the
    per-event typed columns defined in
    :data:`PER_EVENT_PARQUET_FIELDS`. The reader uses this exact schema
    to deserialise the partition file; the writer uses it to encode
    the in-memory record list.
    """
    _check_event_name(event_name)
    return pa.schema(list(SHARED_PARQUET_FIELDS) + list(PER_EVENT_PARQUET_FIELDS[event_name]))


__all__ = [
    "ADDRESS_BYTES",
    "EVENT_NAMES",
    "HASH_BYTES",
    "PartitionKey",
    "PER_EVENT_PARQUET_FIELDS",
    "SHARED_PARQUET_FIELDS",
    "UINT128_BYTES",
    "UINT160_BYTES",
    "UINT256_BYTES",
    "parquet_schema_for",
]
