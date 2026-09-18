"""Tests for the raw partition writer (T031).

Covers the writer's acceptance matrix:

- happy path: append_partition writes a Parquet file and manifest rows;
- repeat / overlap ingestion is idempotent (same EventKey + same
  content hash -> no duplicate rows; same partition key; identical
  checksum);
- staging files are cleaned up on write failure;
- a crash at every commit boundary leaves either the previous
  committed state or the complete new state and never an exposed
  half-written partition;
- the writer records per-partition accounting, scanned-empty
  intervals, and reorg journal entries;
- ingestion checkpoints advance monotonically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from _storage_t031_fixtures import (
    CHAIN,
    CONTRACT,
    make_donate_record,
    make_swap_record,
)
from robinhood_lp.storage.manifest import (
    AccountingInterval,
    ManifestStore,
)
from robinhood_lp.storage.reader import (
    RawPartitionReader,
)
from robinhood_lp.storage.writer import (
    RawPartitionWriter,
    derive_partition_key,
)

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_append_partition_writes_parquet_and_manifest(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [
        make_swap_record(block_number=10, log_index=0),
        make_swap_record(block_number=11, log_index=1, transaction_index=1, tx_hash=0xCC),
    ]
    result = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
        accounting=AccountingInterval(
            endpoint_alias="robinhood_public",
            request_from_block=10,
            request_to_block=11,
            logical_rpc_calls=1,
            http_requests=1,
            http_batches=1,
            response_bytes=2048,
            normalized_rows=2,
            parquet_bytes=0,  # writer overrides
        ),
    )
    assert result.rows_appended == 2
    assert result.rows_skipped == 0
    assert result.conflicts == 0
    assert result.qualified is True
    # File exists and is a real Parquet file (not the staging file).
    assert result.file_path.exists()
    assert result.file_path.name == "data.parquet"
    # Manifest row points at the same file.
    assert manifest.get_partition(result.partition_id) is not None
    manifest.close()


def test_append_partition_inserts_event_index_rows(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [
        make_swap_record(block_number=10, log_index=0),
        make_swap_record(block_number=11, log_index=1, transaction_index=1, tx_hash=0xCC),
    ]
    result = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    # Event index has one row per record.
    with manifest.read() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM event_index WHERE partition_id = ?",
            (result.partition_id,),
        ).fetchone()
    assert rows[0] == 2
    manifest.close()


def test_append_partition_records_accounting(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    result = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
        accounting=AccountingInterval(
            endpoint_alias="robinhood_public",
            request_from_block=10,
            request_to_block=10,
            logical_rpc_calls=3,
            http_requests=2,
            http_batches=1,
            response_bytes=1024,
            normalized_rows=1,
            parquet_bytes=0,
            provider_units=60,
        ),
    )
    rows = manifest.accounting_for(result.partition_id)
    assert len(rows) == 1
    assert rows[0]["logical_rpc_calls"] == 3
    assert rows[0]["provider_units"] == 60
    manifest.close()


def test_append_partition_advances_checkpoint(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    writer.append_partition(records, chain_id=CHAIN, contract_address=CONTRACT)
    cp = manifest.get_checkpoint(
        chain_id=CHAIN.value,
        contract_address=CONTRACT.to_hex().removeprefix("0x").lower(),
        event_name="Swap",
    )
    assert cp is not None
    assert cp >= 10
    manifest.close()


# ---------------------------------------------------------------------------
# Idempotent repeat / overlap
# ---------------------------------------------------------------------------


def test_repeat_append_with_same_records_is_idempotent(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [
        make_swap_record(block_number=10, log_index=0),
        make_swap_record(block_number=11, log_index=1, transaction_index=1, tx_hash=0xCC),
    ]
    first = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    second = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    manifest.close()
    assert first.partition_id == second.partition_id
    assert first.file_sha256 == second.file_sha256
    assert second.rows_appended == 0
    assert second.rows_skipped == 2
    assert second.conflicts == 0


def test_overlap_with_partial_range_is_idempotent(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    # First run: blocks 10..11.
    first_batch = [
        make_swap_record(block_number=10, log_index=0),
        make_swap_record(block_number=11, log_index=1, transaction_index=1, tx_hash=0xCC),
    ]
    first = writer.append_partition(
        first_batch,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    # Second run: only block 11 (already in the partition).
    overlap_batch = [
        make_swap_record(block_number=11, log_index=1, transaction_index=1, tx_hash=0xCC),
    ]
    second = writer.append_partition(
        overlap_batch,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    manifest.close()
    assert first.partition_id == second.partition_id
    assert first.file_sha256 == second.file_sha256
    assert second.rows_appended == 0
    assert second.rows_skipped == 1


def test_cross_provider_same_content_hash_is_idempotent(tmp_path: Path) -> None:
    """Same chain event from two endpoints must dedup by content hash."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    a = make_swap_record(
        block_number=10,
        log_index=0,
        endpoint_alias="robinhood_public",
    )
    b = make_swap_record(
        block_number=10,
        log_index=0,
        endpoint_alias="alchemy_free",
    )
    assert a.acquisition.endpoint_alias != b.acquisition.endpoint_alias
    first = writer.append_partition([a], chain_id=CHAIN, contract_address=CONTRACT)
    second = writer.append_partition([b], chain_id=CHAIN, contract_address=CONTRACT)
    manifest.close()
    assert first.partition_id == second.partition_id
    assert second.rows_appended == 0
    assert second.rows_skipped == 1
    assert second.conflicts == 0


# ---------------------------------------------------------------------------
# Partition key derivation
# ---------------------------------------------------------------------------


def test_derive_partition_key_expands_to_grid(tmp_path: Path) -> None:
    records = [
        make_swap_record(block_number=350, log_index=0),
        make_swap_record(
            block_number=399,
            log_index=1,
            transaction_index=1,
            tx_hash=0xCC,
        ),
    ]
    pk = derive_partition_key(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
        blocks_per_partition=100,
    )
    assert pk.block_from == 300
    assert pk.block_to == 399


def test_derive_partition_key_rejects_records_outside_first_grid_cell(tmp_path: Path) -> None:
    records = [
        make_swap_record(block_number=350, log_index=0),
        make_swap_record(
            block_number=420,
            log_index=1,
            transaction_index=1,
            tx_hash=0xCC,
        ),
    ]
    with pytest.raises(ValueError, match="outside the first record's grid cell"):
        derive_partition_key(
            records,
            chain_id=CHAIN,
            contract_address=CONTRACT,
            blocks_per_partition=100,
        )


def test_derive_partition_key_rejects_mixed_event_names() -> None:
    records: list[object] = [
        make_swap_record(block_number=10, log_index=0),
        make_donate_record(block_number=10, log_index=1),
    ]
    with pytest.raises(ValueError, match="mixed event names"):
        derive_partition_key(
            records,  # type: ignore[arg-type]
            chain_id=CHAIN,
            contract_address=CONTRACT,
        )


def test_derive_partition_key_rejects_empty_records() -> None:
    with pytest.raises(ValueError, match="no records"):
        derive_partition_key(
            [],
            chain_id=CHAIN,
            contract_address=CONTRACT,
        )


def test_derive_partition_key_rejects_invalid_blocks_per_partition() -> None:
    with pytest.raises(ValueError, match="blocks_per_partition"):
        derive_partition_key(
            [make_swap_record(block_number=10, log_index=0)],
            chain_id=CHAIN,
            contract_address=CONTRACT,
            blocks_per_partition=0,
        )


# ---------------------------------------------------------------------------
# Atomic commit / crash boundaries
# ---------------------------------------------------------------------------


def test_writer_rejects_records_with_mismatched_chain_id(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    bad = make_swap_record(block_number=10, log_index=0)
    # Force a different chain id
    from dataclasses import replace

    from robinhood_lp.protocol import ChainId

    bad = replace(bad, chain_id=ChainId(1))
    with pytest.raises(ValueError, match="chain_id"):
        writer.append_partition([bad], chain_id=CHAIN, contract_address=CONTRACT)
    manifest.close()


def test_writer_rejects_records_with_mismatched_contract_address(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    bad = make_swap_record(block_number=10, log_index=0)
    from dataclasses import replace

    from robinhood_lp.protocol import Address

    bad = replace(bad, address=Address.from_hex("0x" + "99" * 20))
    with pytest.raises(ValueError, match="address"):
        writer.append_partition([bad], chain_id=CHAIN, contract_address=CONTRACT)
    manifest.close()


def test_writer_rejects_records_with_credential_bearing_endpoint(tmp_path: Path) -> None:
    """A record whose acquisition.endpoint_alias is credential-bearing
    is rejected before it can land in the raw / manifest / provenance
    storage. The T030 ``AcquisitionProvenance`` constructor is the
    first line of defence; the writer's own ``validate_endpoint_alias``
    call is the second.
    """
    from robinhood_lp.storage.schema import AcquisitionProvenance

    # Pre-constructing the AcquisitionProvenance with a URL must fail.
    with pytest.raises(ValueError, match="credential-bearing"):
        AcquisitionProvenance(endpoint_alias="https://user:pass@example.com")
    with pytest.raises(ValueError, match="credential-bearing"):
        AcquisitionProvenance(endpoint_alias="rpc?token=secret")
    # The writer's own validate_endpoint_alias call also rejects the
    # same aliases (covers the import path of a record built without
    # the constructor — e.g. an externally-decoded JSON-RPC payload).
    from robinhood_lp.storage.manifest import validate_endpoint_alias

    for bad in (
        "https://user:pass@example.com",
        "rpc?token=secret",
        "rpc/foo",
        "rpc with space",
    ):
        with pytest.raises(ValueError, match="credential-bearing"):
            validate_endpoint_alias(bad)


def test_writer_cleans_up_staging_file_on_failure(tmp_path: Path) -> None:
    """If the manifest transaction raises, no .staging-* file remains."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    # Patch insert_partition to raise after the staging file is written.
    from unittest.mock import patch

    records = [make_swap_record(block_number=10, log_index=0)]
    with (
        patch.object(
            manifest,
            "insert_partition",
            side_effect=RuntimeError("forced failure"),
        ),
        pytest.raises(RuntimeError, match="forced failure"),
    ):
        writer.append_partition(records, chain_id=CHAIN, contract_address=CONTRACT)
    manifest.close()
    # The partition directory exists (mkdir happens before staging),
    # but no .staging-* file should remain.
    pk = derive_partition_key(records, chain_id=CHAIN, contract_address=CONTRACT)
    partition_dir = pk.partition_dir(tmp_path)
    assert partition_dir.exists()
    staging_files = list(partition_dir.glob(".staging-*.parquet"))
    assert staging_files == [], f"orphan staging files: {staging_files}"


def test_writer_recovery_from_prior_staging_files(tmp_path: Path) -> None:
    """An orphan .staging-*.parquet file from a prior crash is not exposed.

    The reader never enumerates the partition directory; it only reads
    the manifest. After a crash mid-write, the file is left on disk
    but the reader does not see it.
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    # Plant an orphan staging file from a hypothetical earlier crash.
    pk = derive_partition_key(records, chain_id=CHAIN, contract_address=CONTRACT)
    partition_dir = pk.partition_dir(tmp_path)
    partition_dir.mkdir(parents=True, exist_ok=True)
    orphan = partition_dir / ".staging-deadbeef.parquet"
    orphan.write_bytes(b"not a real parquet file")
    # The writer still succeeds; the reader never looks at .staging-* files.
    result = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    assert result.file_path.exists()
    assert result.file_sha256  # non-empty
    manifest.close()
    # The reader resolves the manifest row and does NOT consult
    # .staging-* files. The final manifest points at the real file
    # with a valid SHA-256.
    manifest2 = ManifestStore(tmp_path / "manifest.sqlite")
    reader = RawPartitionReader(tmp_path, manifest2)
    out = reader.read_partition(pk)
    assert out.qualified is True
    manifest2.close()


def test_manifest_transaction_failure_does_not_corrupt_state(tmp_path: Path) -> None:
    """A failure inside the manifest transaction leaves the manifest unchanged."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    # First successful write.
    first = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    pid = first.partition_id
    # Force a manifest failure mid-transaction: monkey-patch
    # record_event_observation to raise on the second call onwards.
    # The transaction must roll back so the partition row from the
    # first write survives.
    #
    # The second writer call lands in a *different* partition cell
    # (block 200 is in the ``range=200-299`` cell, while block 10 is
    # in ``range=0-99``). The T037 fail-closed guard rejects batches
    # that would add EventKeys to an already-written cell; placing
    # the second batch in a fresh cell keeps that guard out of the
    # way so this test exercises only the manifest rollback path.
    original = manifest.record_event_observation
    call_count = {"n": 0}

    def faulty(*args: Any, **kwargs: Any) -> bool:
        call_count["n"] += 1
        # Fail the first observation in the second writer call so we
        # can verify the manifest rolls back and the first partition
        # remains qualified.
        if call_count["n"] >= 1:
            raise RuntimeError("forced event_index failure")
        return original(*args, **kwargs)

    manifest.record_event_observation = faulty  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="forced event_index failure"):
            writer.append_partition(
                [
                    make_swap_record(
                        block_number=200,
                        log_index=2,
                        transaction_index=2,
                        tx_hash=0xDD,
                        block_hash=0xEE,
                    )
                ],
                chain_id=CHAIN,
                contract_address=CONTRACT,
            )
    finally:
        manifest.record_event_observation = original  # type: ignore[method-assign]
    manifest.close()
    # Manifest still points at the first partition. The second
    # transaction rolled back; the file for the second call is on
    # disk but never referenced.
    manifest2 = ManifestStore(tmp_path / "manifest.sqlite")
    try:
        from robinhood_lp.storage.reader import RawPartitionReader

        reader = RawPartitionReader(tmp_path, manifest2)
        rows = reader.list_qualified_partitions()
        assert rows == [pid]
    finally:
        manifest2.close()


# ---------------------------------------------------------------------------
# Scanned-empty / reorg
# ---------------------------------------------------------------------------


def test_scanned_empty_intervals_recorded(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    writer.append_partition(records, chain_id=CHAIN, contract_address=CONTRACT)
    # Caller records a separately-scanned empty range.
    manifest.record_scanned_empty(
        chain_id=CHAIN.value,
        contract_address=CONTRACT.to_hex().removeprefix("0x").lower(),
        event_name="Swap",
        endpoint_alias="robinhood_public",
        request_from_block=20,
        request_to_block=29,
    )
    empty = manifest.list_scanned_empty(event_name="Swap")
    assert len(empty) == 1
    assert empty[0]["request_from_block"] == 20
    assert empty[0]["request_to_block"] == 29
    manifest.close()


def test_reorg_journal_marks_orphan(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    result = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    manifest.record_reorg(
        chain_id=CHAIN.value,
        block_number=10,
        orphan_block_hash=0xDEAD,
        replacement_block_hash=0xBEEF,
        demotion_reason="canonical chain switched",
        partition_id=result.partition_id,
    )
    reorgs = manifest.list_reorgs()
    assert len(reorgs) == 1
    assert reorgs[0]["orphan_block_hash"] == "0xdead"
    assert reorgs[0]["partition_id"] == result.partition_id
    manifest.close()
