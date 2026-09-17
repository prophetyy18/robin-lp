"""Tests for the manifest-aware reader (T031).

T031 acceptance requires:

- the reader resolves a partition by
  ``(chain_id, contract_address, event_name, block_range)`` and
  never by enumerating the filesystem;
- the reader surfaces manifest mismatch and corruption as errors
  (missing file, SHA-256 mismatch, block-bounds mismatch);
- a partition whose qualification is halted raises
  :class:`UnqualifiedDatasetError`;
- a partition whose file is on disk but not in the manifest is
  not visible to the reader.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _storage_t031_fixtures import (
    CHAIN,
    CONTRACT,
    make_initialize_record,
    make_swap_record,
)
from robinhood_lp.storage.manifest import (
    ManifestStore,
    PartitionQualification,
)
from robinhood_lp.storage.partition import PartitionKey
from robinhood_lp.storage.reader import (
    BoundsMismatchError,
    ManifestMismatchError,
    MissingPartitionError,
    RawPartitionReader,
    UnqualifiedDatasetError,
)
from robinhood_lp.storage.writer import RawPartitionWriter

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_reader_resolves_partition_by_key(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    writer.append_partition(records, chain_id=CHAIN, contract_address=CONTRACT)
    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    reader = RawPartitionReader(tmp_path, manifest)
    out = reader.read_partition(pk)
    assert out.qualified is True
    assert out.partition_id == pk.partition_id()
    assert out.table.num_rows == 1
    manifest.close()


def test_reader_returns_initialize_records(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [
        make_initialize_record(block_number=10, log_index=0),
    ]
    writer.append_partition(records, chain_id=CHAIN, contract_address=CONTRACT)
    pk = PartitionKey(CHAIN, CONTRACT, "Initialize", 0, 99)
    reader = RawPartitionReader(tmp_path, manifest)
    out = reader.read_partition(pk)
    assert out.table.num_rows == 1
    manifest.close()


def test_reader_list_qualified_partitions(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    writer.append_partition(
        [make_swap_record(block_number=10, log_index=0)],
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    writer.append_partition(
        [
            make_initialize_record(
                block_number=110,
                log_index=0,
                block_hash=0xEE,
                tx_hash=0xFF,
            )
        ],
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    reader = RawPartitionReader(tmp_path, manifest)
    out = reader.list_qualified_partitions()
    assert len(out) == 2
    assert any("event=Initialize" in pid for pid in out)
    assert any("event=Swap" in pid for pid in out)
    manifest.close()


def test_reader_list_qualified_partitions_filters_by_event(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    writer.append_partition(
        [make_swap_record(block_number=10, log_index=0)],
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    writer.append_partition(
        [
            make_initialize_record(
                block_number=110,
                log_index=0,
                block_hash=0xEE,
                tx_hash=0xFF,
            )
        ],
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    reader = RawPartitionReader(tmp_path, manifest)
    out = reader.list_qualified_partitions(event_name="Swap")
    assert len(out) == 1
    assert "event=Swap" in out[0]
    manifest.close()


# ---------------------------------------------------------------------------
# Missing / mismatched partitions
# ---------------------------------------------------------------------------


def test_reader_raises_on_missing_partition(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    reader = RawPartitionReader(tmp_path, manifest)
    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    with pytest.raises(MissingPartitionError):
        reader.read_partition(pk)
    manifest.close()


def test_reader_raises_when_manifest_file_missing(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    result = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    # Delete the file but leave the manifest row.
    Path(result.file_path).unlink()
    reader = RawPartitionReader(tmp_path, manifest)
    with pytest.raises(ManifestMismatchError, match="missing"):
        reader.read_partition(pk)
    manifest.close()


def test_reader_raises_on_sha_mismatch(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    result = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    # Corrupt the file by overwriting it with garbage.
    Path(result.file_path).write_bytes(b"this is not a parquet file")
    reader = RawPartitionReader(tmp_path, manifest)
    with pytest.raises(ManifestMismatchError, match="SHA-256"):
        reader.read_partition(pk)
    manifest.close()


def test_reader_raises_on_bounds_mismatch(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    result = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    # Tamper the manifest bounds row to disagree with the file.
    manifest_path = tmp_path / "manifest.sqlite"
    import sqlite3

    conn = sqlite3.connect(str(manifest_path))
    conn.execute(
        "UPDATE partition_block_bounds SET min_block_number = ?, "
        "max_block_number = ? WHERE partition_id = ?",
        (999, 999, result.partition_id),
    )
    conn.commit()
    conn.close()
    reader = RawPartitionReader(tmp_path, manifest)
    with pytest.raises(BoundsMismatchError, match="min_block_number"):
        reader.read_partition(pk)
    manifest.close()


def test_reader_raises_on_unqualified_partition(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    result = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    manifest.upsert_qualification(
        PartitionQualification(
            partition_id=result.partition_id,
            qualified=False,
            halt_reason="conflicting observation recorded",
            halted_at="2026-09-17T00:00:00+00:00",
        )
    )
    reader = RawPartitionReader(tmp_path, manifest)
    with pytest.raises(UnqualifiedDatasetError, match="halted"):
        reader.read_partition(pk)
    manifest.close()


def test_reader_list_halted_partitions(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    result = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    manifest.upsert_qualification(
        PartitionQualification(
            partition_id=result.partition_id,
            qualified=False,
            halt_reason="manual halt for audit",
            halted_at="2026-09-17T00:00:00+00:00",
        )
    )
    reader = RawPartitionReader(tmp_path, manifest)
    halted = reader.list_halted_partitions()
    assert len(halted) == 1
    assert halted[0][0] == result.partition_id
    assert halted[0][1] == "manual halt for audit"
    manifest.close()


def test_reader_does_not_see_files_not_in_manifest(tmp_path: Path) -> None:
    """A parquet file on disk with no manifest row is invisible."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [make_swap_record(block_number=10, log_index=0)]
    _ = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    # Plant an extra Parquet file in another partition directory.
    other_pk = PartitionKey(CHAIN, CONTRACT, "Swap", 100, 199)
    other_dir = other_pk.partition_dir(tmp_path)
    other_dir.mkdir(parents=True, exist_ok=True)
    import pyarrow as pa  # type: ignore[import-untyped]
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    table = pa.table({"x": [1, 2, 3]})
    pq.write_table(table, str(other_dir / "data.parquet"))
    reader = RawPartitionReader(tmp_path, manifest)
    # The phantom partition has no manifest row, so the reader
    # reports it as missing — never trusts the file's existence.
    with pytest.raises(MissingPartitionError):
        reader.read_partition(other_pk)
    # The real partition is still readable.
    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    out = reader.read_partition(pk)
    assert out.qualified is True
    manifest.close()
