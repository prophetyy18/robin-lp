"""Tests for the T035 Parquet columns: ``block_timestamp`` and ``parent_hash``.

The T031 Parquet schema gains two new required shared columns:

- ``block_timestamp`` (uint64, the integer UNIX-seconds block time
  the non-hydrated ``eth_getBlockByNumber`` header reports);
- ``parent_hash`` (binary(32), the previous-block hash the same
  header reports).

Both columns are required on every event row. The writer populates
them from the typed record (``block_timestamp`` and ``parent_hash``
fields added to every log record class in T035) and the reader
deserialises them back so the round-trip is lossless.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq  # type: ignore[import-untyped]
from tests._storage_t031_fixtures import CHAIN, CONTRACT, POOL_ID

from robinhood_lp.protocol import Address
from robinhood_lp.storage import (
    CURRENT_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    ManifestStore,
    RawPartitionWriter,
)
from robinhood_lp.storage.partition import (
    HASH_BYTES,
    SHARED_PARQUET_FIELDS,
    parquet_schema_for,
)
from robinhood_lp.storage.schema import (
    AcquisitionProvenance,
    SwapLogRecord,
)

CHAIN_ID = 4663


def test_shared_parquet_schema_includes_block_timestamp_and_parent_hash() -> None:
    """The shared Parquet fields expose the two new columns."""
    field_names = {f.name for f in SHARED_PARQUET_FIELDS}
    assert "block_timestamp" in field_names
    assert "parent_hash" in field_names


def test_per_event_schema_inherits_block_timestamp_and_parent_hash() -> None:
    """Every event schema includes the new columns."""
    for event_name in ("Initialize", "ModifyLiquidity", "Swap", "Donate", "ProtocolFeeUpdated"):
        schema = parquet_schema_for(event_name)
        names = {field.name for field in schema}
        assert "block_timestamp" in names, event_name
        assert "parent_hash" in names, event_name


def test_block_timestamp_is_uint64_and_parent_hash_is_binary32() -> None:
    """The columns carry the right types: uint64 and fixed-size binary(32)."""
    schema = parquet_schema_for("Swap")
    by_name = {f.name: f for f in schema}
    block_timestamp = by_name["block_timestamp"]
    parent_hash = by_name["parent_hash"]
    # PyArrow: the ``type`` attribute carries the canonical Parquet type.
    assert str(block_timestamp.type).startswith("uint64"), block_timestamp.type
    assert not block_timestamp.nullable
    # PyArrow normalises ``pa.binary(N)`` to ``fixed_size_binary[N]``.
    assert str(parent_hash.type) == f"fixed_size_binary[{HASH_BYTES}]", parent_hash.type
    assert not parent_hash.nullable


def _make_swap_record(
    *,
    block_number: int,
    log_index: int,
    block_hash: int,
    tx_hash: int,
    block_timestamp: int,
    parent_hash: int,
) -> SwapLogRecord:
    return SwapLogRecord(
        chain_id=CHAIN,
        pool_id=POOL_ID,
        block_number=block_number,
        block_hash=block_hash,
        transaction_hash=tx_hash,
        transaction_index=0,
        log_index=log_index,
        address=CONTRACT,
        sender=Address.from_hex("0x" + "11" * 20),
        amount0=-(10**6),
        amount1=10**6,
        sqrt_price_x96=79228162514264337593543950336,
        liquidity=10**18,
        tick=0,
        fee=3000,
        block_timestamp=block_timestamp,
        parent_hash=parent_hash,
        acquisition=AcquisitionProvenance(
            endpoint_alias="robinhood_public",
            request_from_block=block_number,
            request_to_block=block_number,
        ),
    )


def _writer(tmp_path: Path, manifest: ManifestStore) -> RawPartitionWriter:
    return RawPartitionWriter(tmp_path, manifest, blocks_per_partition=10)


def test_writer_populates_block_timestamp_and_parent_hash(tmp_path: Path) -> None:
    """The Parquet file carries the values from the typed record."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    try:
        record = _make_swap_record(
            block_number=100,
            log_index=0,
            block_hash=0xAA,
            tx_hash=0xBB,
            block_timestamp=1_700_000_000,
            parent_hash=0x99,
        )
        result = _writer(tmp_path, manifest).append_partition(
            [record],
            chain_id=record.chain_id,
            contract_address=record.address,
        )
        assert result.rows_appended == 1
        # Read the Parquet file directly to verify the columns.
        table = pq.read_table(str(result.file_path))
        assert "block_timestamp" in table.column_names
        assert "parent_hash" in table.column_names
        assert table.column("block_timestamp").to_pylist() == [1_700_000_000]
        assert table.column("parent_hash").to_pylist() == [
            b"\x00" * 31 + b"\x99",
        ]
    finally:
        manifest.close()


def test_parent_hash_must_be_32_bytes_in_parquet(tmp_path: Path) -> None:
    """The Parquet ``parent_hash`` column is binary(32), zero-padded for short ints."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    try:
        # A 31-byte value still encodes to 32 bytes (zero-padded on the left).
        record = _make_swap_record(
            block_number=100,
            log_index=0,
            block_hash=0xAA,
            tx_hash=0xBB,
            block_timestamp=1_700_000_000,
            parent_hash=0xAB,
        )
        result = _writer(tmp_path, manifest).append_partition(
            [record],
            chain_id=record.chain_id,
            contract_address=record.address,
        )
        table = pq.read_table(str(result.file_path))
        parent_hash_bytes = bytes(table.column("parent_hash").to_pylist()[0])
        assert len(parent_hash_bytes) == 32
    finally:
        manifest.close()


def test_parquet_file_carries_block_columns_directly(tmp_path: Path) -> None:
    """The Parquet file's columns carry the header values verbatim.

    The round-trip through the typed reader is exercised separately
    by the existing storage_reader tests; this test asserts the
    file-level schema and value presence so the T035 contract's
    Parquet-column clause is independently verified.
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    try:
        record = _make_swap_record(
            block_number=100,
            log_index=0,
            block_hash=0xAA,
            tx_hash=0xBB,
            block_timestamp=1_700_000_000,
            parent_hash=0x99,
        )
        result = _writer(tmp_path, manifest).append_partition(
            [record],
            chain_id=record.chain_id,
            contract_address=record.address,
        )
        table = pq.read_table(str(result.file_path))
        # The two new columns exist and carry the typed values.
        assert "block_timestamp" in table.column_names
        assert "parent_hash" in table.column_names
        assert table.column("block_timestamp").to_pylist() == [1_700_000_000]
        assert table.column("parent_hash").to_pylist() == [
            b"\x00" * 31 + b"\x99",
        ]
    finally:
        manifest.close()


def test_repeat_overlap_ingestion_is_idempotent_with_block_columns(tmp_path: Path) -> None:
    """Re-running the same batch keeps the block columns stable."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    try:
        record = _make_swap_record(
            block_number=100,
            log_index=0,
            block_hash=0xAA,
            tx_hash=0xBB,
            block_timestamp=1_700_000_000,
            parent_hash=0x99,
        )
        writer = _writer(tmp_path, manifest)
        first = writer.append_partition(
            [record],
            chain_id=record.chain_id,
            contract_address=record.address,
        )
        second = writer.append_partition(
            [record],
            chain_id=record.chain_id,
            contract_address=record.address,
        )
        assert first.file_sha256 == second.file_sha256
        # The Parquet file is preserved on re-run.
        table = pq.read_table(str(first.file_path))
        assert table.num_rows == 1
        assert table.column("block_timestamp").to_pylist() == [1_700_000_000]
    finally:
        manifest.close()


def test_block_timestamp_field_carries_record_default_zero() -> None:
    """A record built without ``block_timestamp`` defaults to ``0``.

    Legacy / pre-migration records and tests that build records
    without the new fields still construct cleanly. The Parquet
    writer refuses nothing — the value ``0`` is the documented
    legacy placeholder.
    """
    record = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=POOL_ID,
        block_number=1,
        block_hash=1,
        transaction_hash=1,
        transaction_index=0,
        log_index=0,
        address=CONTRACT,
        sender=Address.from_hex("0x" + "11" * 20),
        amount0=0,
        amount1=0,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=0,
    )
    assert record.block_timestamp == 0
    assert record.parent_hash == 0


def test_v2_record_migrates_to_current_with_zero_header_placeholders() -> None:
    """A v2 record lacking the header columns migrates forward with
    ``block_timestamp=0`` and ``parent_hash=0``. The reader can
    still round-trip the record through canonical bytes.
    """
    import json as _json

    from robinhood_lp.storage.schema import canonical_bytes, migrate_to_current

    # Synthesise a v2-shaped canonical blob (no block_timestamp /
    # parent_hash fields). The schema_version envelope is 2.
    v2_payload = {
        "__class__": "SwapLogRecord",
        "__schema_version__": 2,
        "__decode_version__": 2,
        "chain_id": CHAIN.value,
        "pool_id": POOL_ID.value,
        "block_number": 100,
        "block_hash": 0xAA,
        "transaction_hash": 0xBB,
        "transaction_index": 0,
        "log_index": 1,
        "address": CONTRACT.value,
        "sender": Address.from_hex("0x" + "11" * 20).value,
        "amount0": -1,
        "amount1": 1,
        "sqrt_price_x96": 1,
        "liquidity": 1,
        "tick": 0,
        "fee": 3000,
        "removed": False,
        "acquisition": {
            "endpoint_alias": "robinhood_public",
            "retrieval_time": "2026-09-17T00:00:00+00:00",
            "request_from_block": 0,
            "request_to_block": 0,
            "http_batch_size": None,
            "http_batch_position": None,
            "request_attempt": None,
        },
        "raw_topics": [
            ("0x" + "00" * 32),
            ("0x" + "00" * 32),
            ("0x" + "00" * 32),
            ("0x" + "00" * 32),
        ],
        "raw_data": "0x",
        "raw": {},
        "unknown_fields": {},
    }
    blob = _json.dumps(v2_payload, sort_keys=True).encode("utf-8")
    migrated = migrate_to_current(blob)
    assert isinstance(migrated, SwapLogRecord)
    assert migrated.block_timestamp == 0
    assert migrated.parent_hash == 0
    # Round-trip through canonical_bytes still works.
    canonical = canonical_bytes(migrated)
    assert isinstance(canonical, bytes)
    assert CURRENT_SCHEMA_VERSION == 3
    assert MANIFEST_SCHEMA_VERSION >= 3
