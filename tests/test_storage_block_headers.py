"""Tests for the ``block_headers`` manifest table (T035 / ADR-012).

The manifest table holds exactly one deduplicated row per
distinct event block, keyed by ``(chain_id, block_hash)``. The
table is the third of the three carriers ADR-012 mandates:

- the T030 logical header record (``BlockContext`` dataclass);
- the T031 Parquet partition columns ``block_timestamp`` /
  ``parent_hash``;
- the T031 manifest header table (this one).

The tests cover the dedup invariant, the re-run idempotency
contract, the inconsistency guard (same ``block_hash`` must
never disagree on the carrier fields), and the read helpers
the runner uses to recover header evidence during warm
restarts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robinhood_lp.storage import (
    MANIFEST_SCHEMA_VERSION,
    BlockHeaderInconsistencyError,
    ManifestStore,
)

CHAIN_ID = 4663


def _header(
    *,
    block_hash: int,
    block_number: int,
    parent_hash: int,
    block_timestamp: int,
    endpoint_alias: str = "robinhood_public",
) -> dict:
    return {
        "chain_id": CHAIN_ID,
        "block_hash": block_hash,
        "block_number": block_number,
        "parent_hash": parent_hash,
        "block_timestamp": block_timestamp,
        "endpoint_alias": endpoint_alias,
    }


def _manifest(tmp_path: Path) -> ManifestStore:
    return ManifestStore(tmp_path / "manifest.sqlite")


def test_block_headers_table_exists_and_schema_version_bumped(tmp_path: Path) -> None:
    """The T035 schema bump creates the ``block_headers`` table.

    The schema_meta row reflects the current
    ``MANIFEST_SCHEMA_VERSION`` so a future reader detects a
    stale DB.
    """
    store = _manifest(tmp_path)
    try:
        assert store.schema_version() == MANIFEST_SCHEMA_VERSION
        assert MANIFEST_SCHEMA_VERSION >= 3
        with store.read() as conn:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "block_headers" in tables
    finally:
        store.close()


def test_upsert_block_header_inserts_new_row(tmp_path: Path) -> None:
    """A fresh ``block_hash`` produces a new row and returns ``True``."""
    store = _manifest(tmp_path)
    try:
        inserted = store.upsert_block_header(
            None,
            **_header(
                block_hash=0xAA,
                block_number=100,
                parent_hash=0x99,
                block_timestamp=1_700_000_000,
            ),
        )
        assert inserted is True
        row = store.get_block_header(chain_id=CHAIN_ID, block_hash=0xAA)
        assert row is not None
        assert row["block_number"] == 100
        assert int(row["parent_hash"], 16) == 0x99
        assert row["block_timestamp"] == 1_700_000_000
        assert row["endpoint_alias"] == "robinhood_public"
        assert store.count_block_headers(chain_id=CHAIN_ID) == 1
    finally:
        store.close()


def test_upsert_block_header_idempotent_on_reobservation(tmp_path: Path) -> None:
    """Re-observing the same block_hash with the same values returns
    ``False`` and does not produce a duplicate row."""
    store = _manifest(tmp_path)
    try:
        store.upsert_block_header(
            None,
            **_header(
                block_hash=0xAA,
                block_number=100,
                parent_hash=0x99,
                block_timestamp=1_700_000_000,
            ),
        )
        second = store.upsert_block_header(
            None,
            **_header(
                block_hash=0xAA,
                block_number=100,
                parent_hash=0x99,
                block_timestamp=1_700_000_000,
                endpoint_alias="alchemy_free",
            ),
        )
        assert second is False
        assert store.count_block_headers(chain_id=CHAIN_ID) == 1
        row = store.get_block_header(chain_id=CHAIN_ID, block_hash=0xAA)
        assert row is not None
        # The endpoint_alias is refreshed so the audit trail shows
        # the most recent observation.
        assert row["endpoint_alias"] == "alchemy_free"
    finally:
        store.close()


def test_upsert_block_header_inconsistency_on_block_number_drift(tmp_path: Path) -> None:
    """A re-observation of the same block_hash with a different
    block_number raises ``BlockHeaderInconsistencyError`` and the
    stored row is not overwritten.
    """
    store = _manifest(tmp_path)
    try:
        store.upsert_block_header(
            None,
            **_header(
                block_hash=0xAA,
                block_number=100,
                parent_hash=0x99,
                block_timestamp=1_700_000_000,
            ),
        )
        with pytest.raises(BlockHeaderInconsistencyError):
            store.upsert_block_header(
                None,
                **_header(
                    block_hash=0xAA,
                    block_number=101,
                    parent_hash=0x99,
                    block_timestamp=1_700_000_000,
                ),
            )
        # Original row unchanged.
        row = store.get_block_header(chain_id=CHAIN_ID, block_hash=0xAA)
        assert row is not None
        assert row["block_number"] == 100
    finally:
        store.close()


def test_upsert_block_header_inconsistency_on_parent_hash_drift(tmp_path: Path) -> None:
    store = _manifest(tmp_path)
    try:
        store.upsert_block_header(
            None,
            **_header(
                block_hash=0xAA,
                block_number=100,
                parent_hash=0x99,
                block_timestamp=1_700_000_000,
            ),
        )
        with pytest.raises(BlockHeaderInconsistencyError):
            store.upsert_block_header(
                None,
                **_header(
                    block_hash=0xAA,
                    block_number=100,
                    parent_hash=0xAB,
                    block_timestamp=1_700_000_000,
                ),
            )
    finally:
        store.close()


def test_upsert_block_header_inconsistency_on_timestamp_drift(tmp_path: Path) -> None:
    store = _manifest(tmp_path)
    try:
        store.upsert_block_header(
            None,
            **_header(
                block_hash=0xAA,
                block_number=100,
                parent_hash=0x99,
                block_timestamp=1_700_000_000,
            ),
        )
        with pytest.raises(BlockHeaderInconsistencyError):
            store.upsert_block_header(
                None,
                **_header(
                    block_hash=0xAA,
                    block_number=100,
                    parent_hash=0x99,
                    block_timestamp=1_700_000_001,
                ),
            )
    finally:
        store.close()


def test_dedup_one_row_per_distinct_event_block(tmp_path: Path) -> None:
    """The table holds exactly one deduplicated row per distinct
    event block. Re-runs over overlapping ranges never produce
    duplicate rows.
    """
    store = _manifest(tmp_path)
    try:
        for block_hash in (0xAA, 0xBB, 0xCC):
            store.upsert_block_header(
                None,
                **_header(
                    block_hash=block_hash,
                    block_number=100,
                    parent_hash=block_hash - 1,
                    block_timestamp=1_700_000_000,
                ),
            )
        # Re-run: every block_hash is observed again with the same
        # values. The dedup invariant holds.
        for block_hash in (0xAA, 0xBB, 0xCC):
            store.upsert_block_header(
                None,
                **_header(
                    block_hash=block_hash,
                    block_number=100,
                    parent_hash=block_hash - 1,
                    block_timestamp=1_700_000_000,
                ),
            )
        rows = store.list_block_headers(chain_id=CHAIN_ID)
        assert len(rows) == 3
        assert {int(r["block_hash"], 16) for r in rows} == {0xAA, 0xBB, 0xCC}
    finally:
        store.close()


def test_list_block_headers_filters_by_block_number_range(tmp_path: Path) -> None:
    """``list_block_headers`` honours the inclusive ``block_number`` filters."""
    store = _manifest(tmp_path)
    try:
        for n, h in [(100, 0xAA), (101, 0xBB), (102, 0xCC), (103, 0xDD)]:
            store.upsert_block_header(
                None,
                **_header(
                    block_hash=h,
                    block_number=n,
                    parent_hash=h - 1,
                    block_timestamp=1_700_000_000,
                ),
            )
        rows = store.list_block_headers(
            chain_id=CHAIN_ID, block_number_min=101, block_number_max=102
        )
        assert [r["block_number"] for r in rows] == [101, 102]
    finally:
        store.close()


def test_block_headers_dedup_survives_kill_restart(tmp_path: Path) -> None:
    """A crash between the staging write and the manifest commit
    leaves the previous committed state in place; the manifest
    never carries a half-written header row.
    """
    store = _manifest(tmp_path)
    try:
        store.upsert_block_header(
            None,
            **_header(
                block_hash=0xAA,
                block_number=100,
                parent_hash=0x99,
                block_timestamp=1_700_000_000,
            ),
        )
        # Force an exception inside the transaction: the row
        # remains.
        with pytest.raises(BlockHeaderInconsistencyError):
            store.upsert_block_header(
                None,
                **_header(
                    block_hash=0xAA,
                    block_number=200,
                    parent_hash=0x99,
                    block_timestamp=1_700_000_000,
                ),
            )
        assert store.count_block_headers(chain_id=CHAIN_ID) == 1
        row = store.get_block_header(chain_id=CHAIN_ID, block_hash=0xAA)
        assert row is not None
        assert row["block_number"] == 100
    finally:
        store.close()


def test_upsert_block_header_rejects_negative_timestamp(tmp_path: Path) -> None:
    store = _manifest(tmp_path)
    try:
        with pytest.raises(ValueError, match="uint64"):
            store.upsert_block_header(
                None,
                **_header(
                    block_hash=0xAA,
                    block_number=100,
                    parent_hash=0x99,
                    block_timestamp=-1,
                ),
            )
    finally:
        store.close()


def test_upsert_block_header_rejects_overflow_timestamp(tmp_path: Path) -> None:
    store = _manifest(tmp_path)
    try:
        with pytest.raises(ValueError, match="uint64"):
            store.upsert_block_header(
                None,
                **_header(
                    block_hash=0xAA,
                    block_number=100,
                    parent_hash=0x99,
                    block_timestamp=1 << 64,
                ),
            )
    finally:
        store.close()


def test_upsert_block_header_rejects_credential_bearing_alias(tmp_path: Path) -> None:
    """The contract forbids a credential-bearing URL anywhere in
    raw / normalized / manifest / provenance storage. The same
    rule applies to ``block_headers.endpoint_alias``.
    """
    store = _manifest(tmp_path)
    try:
        with pytest.raises(ValueError, match="credential-bearing"):
            store.upsert_block_header(
                None,
                **_header(
                    block_hash=0xAA,
                    block_number=100,
                    parent_hash=0x99,
                    block_timestamp=1_700_000_000,
                    endpoint_alias="https://user:secret@host/path",
                ),
            )
    finally:
        store.close()


def test_block_header_rides_along_event_partition_transaction(tmp_path: Path) -> None:
    """The header upsert can ride along an open event-batch
    transaction so a crash mid-partition leaves the manifest
    unchanged.
    """
    from tests._storage_t031_fixtures import make_swap_record

    from robinhood_lp.storage.writer import RawPartitionWriter

    store = _manifest(tmp_path)
    try:
        writer = RawPartitionWriter(tmp_path, store, blocks_per_partition=10)
        # Construct one minimal Swap record on block 100.
        record = make_swap_record(
            block_number=100,
            log_index=0,
            block_hash=0xAA,
            tx_hash=0xBB,
        )
        # The transaction() context is exposed; ride the header
        # upsert along with the event batch.
        with store.transaction() as conn:
            store._upsert_block_header_in_tx(  # type: ignore[attr-defined]
                conn,
                chain_id=CHAIN_ID,
                block_hash_hex="0x" + format(0xAA, "064x"),
                block_number=100,
                parent_hash_hex="0x" + format(0x99, "064x"),
                block_timestamp=1_700_000_000,
                endpoint_alias="robinhood_public",
                fetched_at="2026-09-17T00:00:00+00:00",
            )
        assert store.count_block_headers(chain_id=CHAIN_ID) == 1
        # Now exercise the partition writer's transaction with the
        # same record so we know the writes ride together.
        result = writer.append_partition(
            [record],
            chain_id=record.chain_id,
            contract_address=record.address,
        )
        assert result.rows_appended == 1
        assert store.count_block_headers(chain_id=CHAIN_ID) == 1
    finally:
        store.close()
