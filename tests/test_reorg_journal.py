"""Tests for the reorg journal and critical-incident recorder (T033).

T033 acceptance requires:

- the reorg journal is append-only and never overwrites or deletes
  existing rows;
- the critical-incident recorder is append-only and never rewrites
  raw evidence, even during a ``FINALIZED_ANCESTRY_VIOLATION``
  incident;
- orphan demotion records are written with the correct reason code
  (``shallow_reorg`` / ``deep_reorg`` / ``provider_disagreement`` /
  ``removed_log`` / ``orphan_reappearance``);
- a pre-existing T031 manifest opens cleanly; the T033 critical-
  incidents table is created lazily.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _reorg_t033_fixtures import CHAIN_ID
from _storage_t031_fixtures import make_swap_record
from robinhood_lp.storage.manifest import ManifestStore, PartitionManifestRow
from robinhood_lp.storage.reorg.journal import (
    REORG_MANIFEST_SCHEMA_VERSION,
    CriticalIncidentRecorder,
    ReorgJournal,
)
from robinhood_lp.storage.reorg.policy import (
    FINALIZED_ANCESTRY_VIOLATION,
    REASON_DEEP_REORG,
    REASON_ORPHAN_REAPPEARANCE,
    REASON_PROVIDER_DISAGREEMENT,
    REASON_REMOVED_LOG,
    REASON_SHALLOW_REORG,
    FinalityPolicyError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(tmp_path: Path) -> ManifestStore:
    return ManifestStore(tmp_path / "manifest.sqlite")


# ---------------------------------------------------------------------------
# Reorg journal — orphan marking
# ---------------------------------------------------------------------------


def test_record_orphan_appends_one_row(tmp_path: Path) -> None:
    manifest = _make_manifest(tmp_path)
    journal = ReorgJournal(manifest)
    try:
        entry = journal.record_orphan(
            chain_id=CHAIN_ID,
            block_number=42,
            orphan_block_hash="0x" + "aa" * 32,
            replacement_block_hash="0x" + "bb" * 32,
            demotion_reason=REASON_SHALLOW_REORG,
            partition_id="partition-1",
        )
        assert entry.id > 0
        assert entry.block_number == 42
        assert entry.orphan_block_hash == "0x" + "aa" * 32
        assert entry.replacement_block_hash == "0x" + "bb" * 32
        assert entry.demotion_reason == REASON_SHALLOW_REORG
        assert entry.partition_id == "partition-1"

        listed = journal.list_reorgs()
        assert [r.id for r in listed] == [entry.id]
    finally:
        manifest.close()


def test_record_orphan_accepts_every_reason_code(tmp_path: Path) -> None:
    manifest = _make_manifest(tmp_path)
    journal = ReorgJournal(manifest)
    try:
        for reason in (
            REASON_SHALLOW_REORG,
            REASON_DEEP_REORG,
            REASON_PROVIDER_DISAGREEMENT,
            REASON_REMOVED_LOG,
            REASON_ORPHAN_REAPPEARANCE,
        ):
            journal.record_orphan(
                chain_id=CHAIN_ID,
                block_number=1,
                orphan_block_hash="0x" + "11" * 32,
                replacement_block_hash=None,
                demotion_reason=reason,
                partition_id=None,
            )
        reasons = [r.demotion_reason for r in journal.list_reorgs()]
        assert reasons == [
            REASON_SHALLOW_REORG,
            REASON_DEEP_REORG,
            REASON_PROVIDER_DISAGREEMENT,
            REASON_REMOVED_LOG,
            REASON_ORPHAN_REAPPEARANCE,
        ]
    finally:
        manifest.close()


def test_record_orphan_rejects_unknown_reason(tmp_path: Path) -> None:
    manifest = _make_manifest(tmp_path)
    journal = ReorgJournal(manifest)
    try:
        with pytest.raises(FinalityPolicyError, match="unknown demotion_reason"):
            journal.record_orphan(
                chain_id=CHAIN_ID,
                block_number=1,
                orphan_block_hash="0x" + "11" * 32,
                replacement_block_hash=None,
                demotion_reason="totally_unknown",
                partition_id=None,
            )
    finally:
        manifest.close()


def test_record_orphan_normalises_hash_case(tmp_path: Path) -> None:
    manifest = _make_manifest(tmp_path)
    journal = ReorgJournal(manifest)
    try:
        entry = journal.record_orphan(
            chain_id=CHAIN_ID,
            block_number=7,
            orphan_block_hash="0x" + "AA" * 32,
            replacement_block_hash="BB" * 32,
            demotion_reason=REASON_SHALLOW_REORG,
            partition_id=None,
        )
        assert entry.orphan_block_hash == "0x" + "aa" * 32
        assert entry.replacement_block_hash == "0x" + "bb" * 32
    finally:
        manifest.close()


def test_record_orphan_rejects_malformed_hash(tmp_path: Path) -> None:
    manifest = _make_manifest(tmp_path)
    journal = ReorgJournal(manifest)
    try:
        with pytest.raises(ValueError, match="not a 32-byte"):
            journal.record_orphan(
                chain_id=CHAIN_ID,
                block_number=1,
                orphan_block_hash="0xabcd",
                replacement_block_hash=None,
                demotion_reason=REASON_SHALLOW_REORG,
                partition_id=None,
            )
    finally:
        manifest.close()


def test_record_orphan_rejects_negative_block_number(tmp_path: Path) -> None:
    manifest = _make_manifest(tmp_path)
    journal = ReorgJournal(manifest)
    try:
        with pytest.raises(ValueError, match="block_number must be >= 0"):
            journal.record_orphan(
                chain_id=CHAIN_ID,
                block_number=-1,
                orphan_block_hash="0x" + "11" * 32,
                replacement_block_hash=None,
                demotion_reason=REASON_SHALLOW_REORG,
                partition_id=None,
            )
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Critical-incident recorder — append-only semantics
# ---------------------------------------------------------------------------


def test_critical_incident_recorder_appends_one_row(tmp_path: Path) -> None:
    manifest = _make_manifest(tmp_path)
    journal = ReorgJournal(manifest)
    recorder = CriticalIncidentRecorder(journal)
    try:
        incident = recorder.record(
            chain_id=CHAIN_ID,
            endpoint_alias="robinhood_public",
            block_number=10,
            block_hash="0x" + "aa" * 32,
            parent_block_hash="0x" + "bb" * 32,
            detail={"evidence": "previously_finalized_block_removed"},
        )
        assert incident.id > 0
        assert incident.incident_kind == FINALIZED_ANCESTRY_VIOLATION
        assert incident.reason_code == FINALIZED_ANCESTRY_VIOLATION
        assert incident.endpoint_alias == "robinhood_public"
        assert incident.block_number == 10
        assert incident.block_hash == "0x" + "aa" * 32

        listed = recorder.list()
        assert [c.id for c in listed] == [incident.id]
    finally:
        manifest.close()


def test_critical_incident_recorder_does_not_overwrite(tmp_path: Path) -> None:
    """Recording two incidents appends two rows; the first row is preserved."""
    manifest = _make_manifest(tmp_path)
    journal = ReorgJournal(manifest)
    recorder = CriticalIncidentRecorder(journal)
    try:
        first = recorder.record(
            chain_id=CHAIN_ID,
            detail={"evidence": "first"},
        )
        second = recorder.record(
            chain_id=CHAIN_ID,
            detail={"evidence": "second"},
        )
        listed = recorder.list()
        assert [c.id for c in listed] == [first.id, second.id]
        # The first row's detail_json is preserved verbatim.
        assert "first" in listed[0].detail_json
        assert "second" in listed[1].detail_json
    finally:
        manifest.close()


def test_critical_incident_recorder_rejects_wrong_kind(tmp_path: Path) -> None:
    manifest = _make_manifest(tmp_path)
    journal = ReorgJournal(manifest)
    try:
        with pytest.raises(FinalityPolicyError, match="incident_kind must be"):
            journal.record_critical_incident_candidate(
                chain_id=CHAIN_ID,
                incident_kind="provider_disagreement",
                reason_code="provider_disagreement",
                detail={"evidence": "wrong kind"},
            )
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Finalized data is never rewritten (golden master check)
# ---------------------------------------------------------------------------


def test_partition_row_is_preserved_across_reorgs(tmp_path: Path) -> None:
    """A pre-existing partition row survives the reorg journal writes.

    T033 acceptance: 'finalized raw evidence is never rewritten,
    including during a critical incident'. The partition manifest row
    is the canonical pointer to the partition file and its SHA-256;
    the journal only writes to ``reorg_journal`` and
    ``critical_incidents``.
    """
    manifest = _make_manifest(tmp_path)
    try:
        # Write a partition row (T031 path).
        from _storage_t031_fixtures import CHAIN, CONTRACT

        record = make_swap_record(block_number=10, log_index=0)
        from robinhood_lp.storage.writer import RawPartitionWriter

        writer = RawPartitionWriter(tmp_path, manifest)
        result = writer.append_partition([record], chain_id=CHAIN, contract_address=CONTRACT)
        partition_id = result.partition_id

        before_row = manifest.get_partition(partition_id)
        assert before_row is not None
        before_sha = before_row.file_sha256

        # Now run several reorg journal writes.
        journal = ReorgJournal(manifest)
        journal.record_orphan(
            chain_id=CHAIN_ID,
            block_number=10,
            orphan_block_hash="0x" + "aa" * 32,
            replacement_block_hash="0x" + "bb" * 32,
            demotion_reason=REASON_SHALLOW_REORG,
            partition_id=partition_id,
        )
        recorder = CriticalIncidentRecorder(journal)
        recorder.record(
            chain_id=CHAIN_ID,
            detail={"evidence": "AFTER_REORG_TEST"},
        )

        after_row = manifest.get_partition(partition_id)
        assert after_row is not None
        assert after_row.file_sha256 == before_sha
        assert after_row.row_count == before_row.row_count
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Reorg journal does not lose partition evidence on a deep-reorg halt
# ---------------------------------------------------------------------------


def test_existing_partition_survives_after_deep_reorg_halt(tmp_path: Path) -> None:
    """A deep-reorg journal entry does not rewrite the partition row."""
    manifest = _make_manifest(tmp_path)
    try:
        from _storage_t031_fixtures import CHAIN, CONTRACT
        from robinhood_lp.storage.writer import RawPartitionWriter

        writer = RawPartitionWriter(tmp_path, manifest)
        record = make_swap_record(block_number=42, log_index=0)
        result = writer.append_partition([record], chain_id=CHAIN, contract_address=CONTRACT)
        partition_id = result.partition_id
        before = manifest.get_partition(partition_id)
        assert before is not None

        journal = ReorgJournal(manifest)
        journal.record_orphan(
            chain_id=CHAIN_ID,
            block_number=42,
            orphan_block_hash=before.file_sha256,
            replacement_block_hash=None,
            demotion_reason=REASON_DEEP_REORG,
            partition_id=partition_id,
        )
        after = manifest.get_partition(partition_id)
        assert after is not None
        assert after.file_sha256 == before.file_sha256
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# ReorgJournal metadata
# ---------------------------------------------------------------------------


def test_reorg_journal_exposes_manifest_and_schema_version(tmp_path: Path) -> None:
    manifest = _make_manifest(tmp_path)
    journal = ReorgJournal(manifest)
    try:
        assert journal.manifest is manifest
        assert REORG_MANIFEST_SCHEMA_VERSION >= 3
    finally:
        manifest.close()


def test_reorg_journal_rejects_non_manifest(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="manifest must be ManifestStore"):
        ReorgJournal("not a manifest")  # type: ignore[arg-type]


def test_critical_incident_recorder_rejects_non_journal(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="journal must be ReorgJournal"):
        CriticalIncidentRecorder("not a journal")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cross-check: existing T031 storage path remains intact
# ---------------------------------------------------------------------------


def test_partition_row_unchanged_when_no_reorg_recorded(tmp_path: Path) -> None:
    """Smoke test: opening a T031 manifest under T033 does not change any
    pre-existing row. The critical_incidents table is created
    lazily.
    """
    manifest = _make_manifest(tmp_path)
    try:
        from _storage_t031_fixtures import CHAIN, CONTRACT
        from robinhood_lp.storage.writer import RawPartitionWriter

        writer = RawPartitionWriter(tmp_path, manifest)
        record = make_swap_record(block_number=1, log_index=0)
        result = writer.append_partition([record], chain_id=CHAIN, contract_address=CONTRACT)
        partition_id = result.partition_id
        before = manifest.get_partition(partition_id)
        assert before is not None

        # Boot the journal (which creates the critical_incidents table).
        ReorgJournal(manifest)
        after = manifest.get_partition(partition_id)
        assert after is not None
        assert after.file_sha256 == before.file_sha256
        assert after.row_count == before.row_count
        # The T031 partition row itself is a PartitionManifestRow;
        # type-narrow check that the dataclass survived.
        assert isinstance(after, PartitionManifestRow)
    finally:
        manifest.close()
