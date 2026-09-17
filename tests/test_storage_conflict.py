"""Tests for cross-provider dedup and conflicting observation handling (T031).

T031 acceptance requires:

- the same on-chain event fetched from two different endpoints at
  two different retrieval times yields the **same** normalized
  content hash while both raw acquisition envelopes are still
  retained and checksummed;
- dedup is by T011 ``EventKey`` plus normalized content. Dedup is
  not driven by ingestion time, endpoint, block number alone, or
  filename;
- conflicting observations (same EventKey but disagreeing normalized
  content) are retained as separate observations and halt dataset
  qualification instead of one silently overwriting the other.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from _storage_t031_fixtures import (
    CHAIN,
    CONTRACT,
    make_modify_record,
    make_swap_record,
)
from robinhood_lp.storage.manifest import (
    ManifestStore,
)
from robinhood_lp.storage.reader import (
    RawPartitionReader,
    UnqualifiedDatasetError,
)
from robinhood_lp.storage.schema import (
    normalized_content_hash,
)
from robinhood_lp.storage.writer import RawPartitionWriter

# ---------------------------------------------------------------------------
# Same content from two endpoints -> idempotent
# ---------------------------------------------------------------------------


def test_same_event_two_endpoints_same_content_hash() -> None:
    """The normalized content hash is identical for two records with
    the same typed fields but different acquisition envelopes.
    """
    from dataclasses import replace

    a = make_swap_record(
        block_number=10,
        log_index=0,
        endpoint_alias="robinhood_public",
    )
    b_acq = replace(
        a.acquisition,
        endpoint_alias="alchemy_free",
        retrieval_time="2026-09-18T01:02:03+00:00",
    )
    b = replace(a, acquisition=b_acq)
    assert a.acquisition.endpoint_alias != b.acquisition.endpoint_alias
    # Normalized content hash must be identical (observational fields excluded).
    assert normalized_content_hash(a) == normalized_content_hash(b)
    # But canonical bytes still differ (acquisition envelope is retained).
    from robinhood_lp.storage.schema import canonical_bytes

    assert canonical_bytes(a) != canonical_bytes(b)


def test_cross_provider_run_dedups_by_content(tmp_path: Path) -> None:
    """Re-running the same event from a different provider is idempotent."""
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
    first = writer.append_partition([a], chain_id=CHAIN, contract_address=CONTRACT)
    second = writer.append_partition([b], chain_id=CHAIN, contract_address=CONTRACT)
    assert first.partition_id == second.partition_id
    assert first.file_sha256 == second.file_sha256
    assert second.rows_appended == 0
    assert second.rows_skipped == 1
    assert second.conflicts == 0
    manifest.close()


def test_endpoint_alias_does_not_drive_dedup(tmp_path: Path) -> None:
    """Dedup is by EventKey + content hash. Two records that differ
    ONLY in their endpoint alias (the acquisition envelope) are the
    same chain event and must dedup.
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    a = make_swap_record(block_number=10, log_index=0, endpoint_alias="robinhood_public")
    b = make_swap_record(block_number=10, log_index=0, endpoint_alias="alchemy_free")
    _ = writer.append_partition([a], chain_id=CHAIN, contract_address=CONTRACT)
    second = writer.append_partition([b], chain_id=CHAIN, contract_address=CONTRACT)
    assert second.rows_skipped == 1
    assert second.conflicts == 0
    manifest.close()


def test_block_number_alone_does_not_drive_dedup(tmp_path: Path) -> None:
    """Two records with different block_number but the same EventKey
    are still dedup'd. The block_number is part of the typed body
    that enters the content hash, so this is the *content* match.
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    a = make_swap_record(
        block_number=10,
        log_index=0,
        block_hash=0xAA,
        tx_hash=0xBB,
    )
    b = make_swap_record(
        block_number=10,
        log_index=0,
        block_hash=0xAA,
        tx_hash=0xBB,
    )
    first = writer.append_partition([a], chain_id=CHAIN, contract_address=CONTRACT)
    second = writer.append_partition([b], chain_id=CHAIN, contract_address=CONTRACT)
    assert second.rows_skipped == 1
    assert second.conflicts == 0
    assert first.partition_id == second.partition_id
    manifest.close()


def test_ingestion_time_does_not_drive_dedup(tmp_path: Path) -> None:
    """Two records that differ only in their ``AcquisitionProvenance.retrieval_time``
    (and nothing else) are the same chain event and must dedup.
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    a = make_swap_record(block_number=10, log_index=0, endpoint_alias="robinhood_public")
    b_acq = replace(
        a.acquisition,
        retrieval_time="2099-01-01T00:00:00+00:00",
    )
    b = replace(a, acquisition=b_acq)
    first = writer.append_partition([a], chain_id=CHAIN, contract_address=CONTRACT)
    second = writer.append_partition([b], chain_id=CHAIN, contract_address=CONTRACT)
    assert second.rows_skipped == 1
    assert second.conflicts == 0
    assert first.partition_id == second.partition_id
    manifest.close()


# ---------------------------------------------------------------------------
# Conflicting observations
# ---------------------------------------------------------------------------


def test_same_event_key_different_content_is_conflict(tmp_path: Path) -> None:
    """Two records with the same EventKey but different typed bodies
    are a conflicting observation (a reorg). Both are retained; the
    partition is halted.
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    a = make_swap_record(
        block_number=10,
        log_index=0,
        amount0=-(10**6),
        amount1=2 * 10**6,
    )
    b = make_swap_record(
        block_number=10,
        log_index=0,
        amount0=-(10**7),  # different amount0 -> different content hash
        amount1=3 * 10**7,
    )
    # Sanity: same EventKey, different content hash.
    assert a.event_key() == b.event_key()
    assert normalized_content_hash(a) != normalized_content_hash(b)
    first = writer.append_partition([a], chain_id=CHAIN, contract_address=CONTRACT)
    second = writer.append_partition([b], chain_id=CHAIN, contract_address=CONTRACT)
    assert first.partition_id == second.partition_id
    assert first.file_sha256 == second.file_sha256  # file unchanged on conflict
    assert second.conflicts == 1
    assert second.qualified is False
    # The conflict is recorded in conflicting_observations.
    conflicts = manifest.list_conflicts(partition_id=first.partition_id)
    assert len(conflicts) == 1
    assert conflicts[0].existing_content_hash != conflicts[0].observed_content_hash
    manifest.close()


def test_conflict_halts_qualification(tmp_path: Path) -> None:
    """A halted partition raises UnqualifiedDatasetError on read."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    a = make_swap_record(block_number=10, log_index=0, amount0=-(10**6), amount1=2 * 10**6)
    b = make_swap_record(block_number=10, log_index=0, amount0=-(10**7), amount1=2 * 10**7)
    _ = writer.append_partition([a], chain_id=CHAIN, contract_address=CONTRACT)
    writer.append_partition([b], chain_id=CHAIN, contract_address=CONTRACT)
    from robinhood_lp.storage.partition import PartitionKey

    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    reader = RawPartitionReader(tmp_path, manifest)
    with pytest.raises(UnqualifiedDatasetError, match="halted"):
        reader.read_partition(pk)
    manifest.close()


def test_conflict_retains_both_observations(tmp_path: Path) -> None:
    """A conflicting observation is retained alongside the original
    consistent observation; neither is overwritten.
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    a = make_swap_record(
        block_number=10,
        log_index=0,
        endpoint_alias="robinhood_public",
        amount0=-(10**6),
        amount1=2 * 10**6,
    )
    b = make_swap_record(
        block_number=10,
        log_index=0,
        endpoint_alias="alchemy_free",
        amount0=-(10**7),
        amount1=3 * 10**7,
    )
    writer.append_partition([a], chain_id=CHAIN, contract_address=CONTRACT)
    writer.append_partition([b], chain_id=CHAIN, contract_address=CONTRACT)
    # event_index has two rows for this EventKey: one consistent,
    # one conflict.
    with manifest.read() as conn:
        rows = conn.execute(
            """
            SELECT observation_kind FROM event_index
            WHERE chain_id = ? AND block_hash = ? AND tx_hash = ?
              AND log_index = ?
            ORDER BY observation_kind
            """,
            (
                a.chain_id.value,
                "0x" + format(a.block_hash, "x"),
                "0x" + format(a.transaction_hash, "x"),
                a.log_index,
            ),
        ).fetchall()
    assert len(rows) == 2
    assert {r["observation_kind"] for r in rows} == {"consistent", "conflict"}
    # conflicting_observations has one row with the diff.
    conflicts = manifest.list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].observed_endpoint_alias == "alchemy_free"
    manifest.close()


def test_modify_liquidity_conflict_detection(tmp_path: Path) -> None:
    """Conflict detection works for non-Swap event types."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    a = make_modify_record(
        block_number=10,
        log_index=0,
        tick_lower=-100,
        tick_upper=100,
        liquidity_delta=10**18,
    )
    b = make_modify_record(
        block_number=10,
        log_index=0,
        tick_lower=-200,
        tick_upper=200,
        liquidity_delta=10**19,
    )
    assert a.event_key() == b.event_key()
    assert normalized_content_hash(a) != normalized_content_hash(b)
    _ = writer.append_partition([a], chain_id=CHAIN, contract_address=CONTRACT)
    second = writer.append_partition([b], chain_id=CHAIN, contract_address=CONTRACT)
    assert second.conflicts == 1
    assert second.qualified is False
    manifest.close()


def test_idempotent_conflict_re_recording(tmp_path: Path) -> None:
    """Re-recording the same conflicting observation does not double-count."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    a = make_swap_record(block_number=10, log_index=0, amount0=-(10**6), amount1=2 * 10**6)
    b = make_swap_record(block_number=10, log_index=0, amount0=-(10**7), amount1=3 * 10**7)
    c = make_swap_record(block_number=10, log_index=0, amount0=-(10**8), amount1=4 * 10**8)
    writer.append_partition([a], chain_id=CHAIN, contract_address=CONTRACT)
    second = writer.append_partition([b], chain_id=CHAIN, contract_address=CONTRACT)
    third = writer.append_partition([c], chain_id=CHAIN, contract_address=CONTRACT)
    # Each new observation is a new conflict.
    assert second.conflicts == 1
    assert third.conflicts == 1
    manifest.close()
