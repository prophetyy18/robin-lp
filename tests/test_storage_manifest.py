"""Tests for the SQLite manifest store + reorg journal (T031).

The manifest store is the source of truth for which partitions exist,
what their per-file SHA-256 checksum is, and how the framework
accounted for the bytes / RPC calls / provider units that produced
them. T031 acceptance requires:

- the manifest survives a crash mid-write (BEGIN IMMEDIATE / COMMIT);
- partition rows, bounds, qualification, accounting, scanned-empty
  intervals, and the reorg journal are all queryable;
- the EventKey index supports idempotent dedup and conflicting-
  observation detection;
- the endpoint alias validation rejects credential-bearing URLs.

These tests use a temporary directory per test so the SQLite file is
fresh and the tests are independent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robinhood_lp.storage.manifest import (
    AccountingInterval,
    ConflictingObservation,
    ManifestStore,
    PartitionBounds,
    PartitionManifestRow,
    PartitionQualification,
    validate_endpoint_alias,
)


def _row(
    partition_id: str = "chain=4663/contract=44...44/event=Swap/range=0-99",
    chain_id: int = 4663,
    contract_address: str = "44" * 20,
    event_name: str = "Swap",
    block_from: int = 0,
    block_to: int = 99,
    row_count: int = 3,
    file_path: str = "/tmp/data.parquet",
    file_size_bytes: int = 1024,
    file_sha256: str = "0x" + "ab" * 32,
) -> PartitionManifestRow:
    return PartitionManifestRow(
        partition_id=partition_id,
        chain_id=chain_id,
        contract_address=contract_address,
        event_name=event_name,
        block_from=block_from,
        block_to=block_to,
        row_count=row_count,
        file_path=file_path,
        file_size_bytes=file_size_bytes,
        file_sha256=file_sha256,
        schema_version=2,
        decode_version=2,
        created_at="2026-09-17T00:00:00+00:00",
    )


def _bounds(
    partition_id: str = "chain=4663/contract=44...44/event=Swap/range=0-99",
    min_block_number: int = 0,
    max_block_number: int = 99,
) -> PartitionBounds:
    return PartitionBounds(
        min_block_number=min_block_number,
        min_block_hash="0x" + "00" * 32,
        max_block_number=max_block_number,
        max_block_hash="0x" + "11" * 32,
    )


def _qual(partition_id: str, *, qualified: bool = True) -> PartitionQualification:
    return PartitionQualification(
        partition_id=partition_id,
        qualified=qualified,
        halt_reason=None if qualified else "halt reason",
        halted_at=None if qualified else "2026-09-17T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_manifest_bootstrap_creates_tables(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    try:
        with manifest.read() as conn:
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for required in {
            "partitions",
            "partition_block_bounds",
            "partition_qualification",
            "event_index",
            "conflicting_observations",
            "accounting_intervals",
            "scanned_empty_intervals",
            "reorg_journal",
            "ingestion_checkpoints",
        }:
            assert required in tables, f"missing table {required!r}"
    finally:
        manifest.close()


def test_manifest_schema_version_recorded(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    try:
        assert manifest.schema_version() >= 1
    finally:
        manifest.close()


def test_manifest_reopen_is_idempotent(tmp_path: Path) -> None:
    # Bootstrap once, close, reopen. Tables still exist and are not
    # duplicated.
    path = tmp_path / "manifest.sqlite"
    a = ManifestStore(path)
    a.close()
    b = ManifestStore(path)
    try:
        with b.read() as conn:
            count = conn.execute("SELECT COUNT(*) FROM partitions").fetchone()[0]
        assert count == 0
    finally:
        b.close()


# ---------------------------------------------------------------------------
# Partition rows + bounds + qualification
# ---------------------------------------------------------------------------


def test_insert_partition_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        row = _row()
        bounds = _bounds()
        qual = _qual(row.partition_id)
        with manifest.transaction() as conn:
            manifest.insert_partition(conn, row, bounds, qual)
        fetched = manifest.get_partition(row.partition_id)
        assert fetched is not None
        assert fetched.partition_id == row.partition_id
        assert fetched.file_sha256 == row.file_sha256
        assert fetched.file_path == row.file_path
        fetched_bounds = manifest.get_bounds(row.partition_id)
        assert fetched_bounds == bounds
        fetched_qual = manifest.get_qualification(row.partition_id)
        assert fetched_qual is not None
        assert fetched_qual.qualified is True
    finally:
        manifest.close()


def test_qualification_can_be_halted(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        row = _row()
        bounds = _bounds()
        with manifest.transaction() as conn:
            manifest.insert_partition(
                conn,
                row,
                bounds,
                _qual(row.partition_id, qualified=True),
            )
        manifest.upsert_qualification(
            PartitionQualification(
                partition_id=row.partition_id,
                qualified=False,
                halt_reason="conflicting observation recorded",
                halted_at="2026-09-17T00:00:00+00:00",
            )
        )
        fetched = manifest.get_qualification(row.partition_id)
        assert fetched is not None
        assert fetched.qualified is False
        assert fetched.halt_reason == "conflicting observation recorded"
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Event index + conflict recording
# ---------------------------------------------------------------------------


def test_event_index_first_observation_is_consistent(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        with manifest.transaction() as conn:
            manifest.record_event_observation(
                conn,
                chain_id=4663,
                block_hash=0xAA,
                tx_hash=0xBB,
                log_index=0,
                content_hash="0x" + "11" * 32,
                partition_id="p1",
                observation_kind="consistent",
            )
            existing = manifest.lookup_event(
                conn,
                chain_id=4663,
                block_hash=0xAA,
                tx_hash=0xBB,
                log_index=0,
            )
        assert len(existing) == 1
        assert existing[0]["observation_kind"] == "consistent"
    finally:
        manifest.close()


def test_event_index_rejects_bad_observation_kind(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        with manifest.transaction() as conn, pytest.raises(ValueError, match="observation_kind"):
            manifest.record_event_observation(
                conn,
                chain_id=4663,
                block_hash=0xAA,
                tx_hash=0xBB,
                log_index=0,
                content_hash="0x" + "11" * 32,
                partition_id="p1",
                observation_kind="bogus",
            )
    finally:
        manifest.close()


def test_conflict_is_recorded_in_separate_table(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        with manifest.transaction() as conn:
            manifest.record_conflict(
                conn,
                ConflictingObservation(
                    chain_id=4663,
                    block_hash="0x" + "aa" * 32,
                    tx_hash="0x" + "bb" * 32,
                    log_index=0,
                    observed_content_hash="0x" + "11" * 32,
                    existing_content_hash="0x" + "22" * 32,
                    partition_id="p1",
                    observed_endpoint_alias="robinhood_public",
                    observed_payload_json="{}",
                    recorded_at="2026-09-17T00:00:00+00:00",
                ),
            )
        conflicts = manifest.list_conflicts()
        assert len(conflicts) == 1
        assert conflicts[0].observed_content_hash == "0x" + "11" * 32
        assert conflicts[0].existing_content_hash == "0x" + "22" * 32
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


def test_accounting_insert_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        row = _row()
        bounds = _bounds()
        qual = _qual(row.partition_id)
        with manifest.transaction() as conn:
            manifest.insert_partition(conn, row, bounds, qual)
            manifest.insert_accounting(
                conn,
                row.partition_id,
                AccountingInterval(
                    endpoint_alias="robinhood_public",
                    request_from_block=0,
                    request_to_block=99,
                    logical_rpc_calls=2,
                    http_requests=1,
                    http_batches=1,
                    response_bytes=4096,
                    normalized_rows=10,
                    parquet_bytes=2048,
                    provider_units=120,
                ),
            )
        rows = manifest.accounting_for(row.partition_id)
        assert len(rows) == 1
        assert rows[0]["endpoint_alias"] == "robinhood_public"
        assert rows[0]["logical_rpc_calls"] == 2
        assert rows[0]["provider_units"] == 120
        assert rows[0]["parquet_bytes"] == 2048
    finally:
        manifest.close()


def test_accounting_rejects_credential_url(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        with manifest.transaction() as conn, pytest.raises(ValueError, match="credential-bearing"):
            manifest.insert_accounting(
                conn,
                "p1",
                AccountingInterval(
                    endpoint_alias="https://user:pass@example.com/rpc",
                    request_from_block=0,
                    request_to_block=99,
                    logical_rpc_calls=1,
                    http_requests=1,
                    http_batches=1,
                    response_bytes=1,
                    normalized_rows=0,
                    parquet_bytes=1,
                ),
            )
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Scanned-empty intervals
# ---------------------------------------------------------------------------


def test_scanned_empty_interval_recorded(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        manifest.record_scanned_empty(
            chain_id=4663,
            contract_address="44" * 20,
            event_name="Swap",
            endpoint_alias="robinhood_public",
            request_from_block=100,
            request_to_block=199,
        )
        rows = manifest.list_scanned_empty(event_name="Swap")
        assert len(rows) == 1
        assert rows[0]["request_from_block"] == 100
        assert rows[0]["request_to_block"] == 199
    finally:
        manifest.close()


def test_scanned_empty_rejects_credential_url(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        with pytest.raises(ValueError, match="credential-bearing"):
            manifest.record_scanned_empty(
                chain_id=4663,
                contract_address="44" * 20,
                event_name="Swap",
                endpoint_alias="https://user:pass@example.com/rpc",
                request_from_block=0,
                request_to_block=10,
            )
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Reorg journal
# ---------------------------------------------------------------------------


def test_reorg_journal_records_orphan(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        manifest.record_reorg(
            chain_id=4663,
            block_number=1234,
            orphan_block_hash=0xDEAD,
            replacement_block_hash=0xBEEF,
            demotion_reason="canonical chain switched",
            partition_id="p1",
        )
        rows = manifest.list_reorgs()
        assert len(rows) == 1
        assert rows[0]["orphan_block_hash"] == "0xdead"
        assert rows[0]["replacement_block_hash"] == "0xbeef"
        assert rows[0]["partition_id"] == "p1"
    finally:
        manifest.close()


def test_reorg_journal_accepts_no_replacement(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        manifest.record_reorg(
            chain_id=4663,
            block_number=1234,
            orphan_block_hash=0xDEAD,
            replacement_block_hash=None,
            demotion_reason="no replacement observed",
            partition_id=None,
        )
        rows = manifest.list_reorgs()
        assert len(rows) == 1
        assert rows[0]["replacement_block_hash"] is None
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Ingestion checkpoints
# ---------------------------------------------------------------------------


def test_checkpoint_advances_monotonically(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        with manifest.transaction() as conn:
            manifest.advance_checkpoint(
                conn,
                chain_id=4663,
                contract_address="44" * 20,
                event_name="Swap",
                last_successful_block=100,
            )
        cp = manifest.get_checkpoint(
            chain_id=4663,
            contract_address="44" * 20,
            event_name="Swap",
        )
        assert cp == 100
        # A re-run that lands on block 50 must NOT move the checkpoint
        # backwards.
        with manifest.transaction() as conn:
            manifest.advance_checkpoint(
                conn,
                chain_id=4663,
                contract_address="44" * 20,
                event_name="Swap",
                last_successful_block=50,
            )
        cp = manifest.get_checkpoint(
            chain_id=4663,
            contract_address="44" * 20,
            event_name="Swap",
        )
        assert cp == 100
    finally:
        manifest.close()


def test_checkpoint_unknown_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "manifest.sqlite"
    manifest = ManifestStore(path)
    try:
        cp = manifest.get_checkpoint(
            chain_id=4663,
            contract_address="44" * 20,
            event_name="Swap",
        )
        assert cp is None
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Endpoint alias validation
# ---------------------------------------------------------------------------


def test_validate_endpoint_alias_accepts_short_token() -> None:
    assert validate_endpoint_alias("robinhood_public") == "robinhood_public"
    assert validate_endpoint_alias("alchemy_free") == "alchemy_free"
    assert validate_endpoint_alias("") == ""


def test_validate_endpoint_alias_rejects_url() -> None:
    for bad in (
        "https://example.com/rpc",
        "https://user:pass@example.com/rpc",
        "http://localhost:8545?token=secret",
        "rpc @ https",
        "rpc/path",
        "rpc\\path",
        "rpc?token=x",
        "rpc with space",
        "rpc\nwith\nnewline",
    ):
        with pytest.raises(ValueError, match="credential-bearing"):
            validate_endpoint_alias(bad)


def test_validate_endpoint_alias_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        validate_endpoint_alias(123)  # type: ignore[arg-type]
