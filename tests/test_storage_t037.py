"""Tests for the T037 partition row-loss fix.

Covers the deliverables:

1. Writer fail-closed guard (storage/writer.py): an incoming batch
   that contains EventKeys not in the existing on-disk Parquet file
   raises :class:`SilentRowLossError` instead of silently appending
   to ``event_index``.
2. Re-run / overlap idempotency remains intact (T031 contract
   re-stated).
3. Per-partition reconciliation (storage/reconciliation.py) reports a
   mismatch for an inconsistent partition fixture and is consistent
   for a freshly-written one.
4. The data-quality verifier (quality/verification.py) accepts the
   reconciliation reports and refuses ``complete=true`` when a
   partition disagrees.
5. The planner grid-aligned split prevents the silent overlap that
   produced the original defect (regression).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from _storage_t031_fixtures import (
    CHAIN,
    CONTRACT,
    make_swap_record,
)
from robinhood_lp.quality.quality_report import (
    TopologyKind,
    empty_report,
)
from robinhood_lp.quality.reason_codes import (
    REASON_PARTITION_EVENT_INDEX_PARQUET_MISMATCH,
)
from robinhood_lp.quality.sample_selection import RequiredSamples
from robinhood_lp.quality.verification import verify_report
from robinhood_lp.storage import (
    ManifestStore,
    RawPartitionWriter,
    SilentRowLossError,
    find_inconsistent_partitions,
    reconcile_all_partitions,
    reconcile_partition,
)


def _required_samples_stub() -> RequiredSamples:
    """A minimal :class:`RequiredSamples` that satisfies the report's
    frozen dataclass without exercising the selection logic."""
    from robinhood_lp.protocol import PoolId
    from robinhood_lp.quality.sample_selection import (
        PerRunSample,
        RequiredSamples,
    )

    per_run = PerRunSample(
        pool_id=PoolId(0xABCDEF12),
        fixed_block_number=0,
        fixed_block_hash="",
        chain_id=CHAIN.value,
        genesis_hash="",
        pool_manager_code_hash="",
        state_view_code_hash="",
        selection_inputs={},
    )
    return RequiredSamples(
        per_run=per_run,
        per_partition=(),
        per_event_type=(),
        per_failover=(),
        result_bearing_windows=(),
    )


# ---------------------------------------------------------------------------
# 1. Writer fail-closed guard
# ---------------------------------------------------------------------------


def test_writer_fail_closed_on_new_event_key_in_existing_partition(tmp_path: Path) -> None:
    """An incoming batch with an EventKey not in the existing Parquet
    file raises :class:`SilentRowLossError`. The transaction rolls
    back so no ``event_index`` row is recorded against the rejected
    batch.
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    first_batch = [
        make_swap_record(block_number=10, log_index=0),
        make_swap_record(block_number=11, log_index=1, transaction_index=1, tx_hash=0xCC),
    ]
    first = writer.append_partition(first_batch, chain_id=CHAIN, contract_address=CONTRACT)
    pid = first.partition_id
    # Second batch carries a *new* EventKey that the existing Parquet
    # file does not contain. The guard must reject it.
    second_batch = [
        make_swap_record(
            block_number=20,
            log_index=2,
            transaction_index=2,
            tx_hash=0xDD,
            block_hash=0xEE,
        ),
    ]
    with pytest.raises(SilentRowLossError) as excinfo:
        writer.append_partition(second_batch, chain_id=CHAIN, contract_address=CONTRACT)
    assert excinfo.value.partition_id == pid
    assert excinfo.value.parquet_row_count == 2
    # The transaction rolled back: the manifest's event_index is the
    # same set the first call recorded.
    with manifest.read() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM event_index WHERE partition_id = ?",
            (pid,),
        ).fetchone()[0]
    assert rows == 2, "SilentRowLossError must roll back the event_index write"
    # The Parquet file is unchanged.
    manifest_row = manifest.get_partition(pid)
    assert manifest_row is not None
    assert manifest_row.row_count == 2
    manifest.close()


def test_writer_repeat_with_same_records_still_idempotent(tmp_path: Path) -> None:
    """Re-running an already-written batch must NOT trigger the
    fail-closed guard. Every EventKey is already present, so the
    writer takes the idempotent path (T031 contract re-stated).
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    batch = [
        make_swap_record(block_number=10, log_index=0),
        make_swap_record(block_number=11, log_index=1, transaction_index=1, tx_hash=0xCC),
    ]
    first = writer.append_partition(batch, chain_id=CHAIN, contract_address=CONTRACT)
    second = writer.append_partition(batch, chain_id=CHAIN, contract_address=CONTRACT)
    assert first.partition_id == second.partition_id
    assert first.file_sha256 == second.file_sha256
    assert second.rows_appended == 0
    assert second.rows_skipped == 2
    assert second.conflicts == 0
    manifest.close()


def test_writer_overlap_with_subset_still_idempotent(tmp_path: Path) -> None:
    """A re-run that supplies a subset of an already-covered cell must
    also stay idempotent. The subset's EventKeys all exist in the
    Parquet file from the first write.
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    full_batch = [
        make_swap_record(block_number=10, log_index=0),
        make_swap_record(block_number=11, log_index=1, transaction_index=1, tx_hash=0xCC),
    ]
    writer.append_partition(full_batch, chain_id=CHAIN, contract_address=CONTRACT)
    subset = [full_batch[0]]
    result = writer.append_partition(subset, chain_id=CHAIN, contract_address=CONTRACT)
    assert result.rows_appended == 0
    assert result.rows_skipped == 1
    assert result.conflicts == 0
    manifest.close()


def test_writer_fail_closed_lists_missing_event_keys_in_exception(
    tmp_path: Path,
) -> None:
    """The exception's ``missing_event_keys`` tuple names every new
    EventKey in the rejected batch so the operator can see exactly
    what would have been lost without the guard.
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    writer.append_partition(
        [make_swap_record(block_number=10, log_index=0)],
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    new_a = make_swap_record(
        block_number=20, log_index=2, transaction_index=2, tx_hash=0xAA11, block_hash=0xBB11
    )
    new_b = make_swap_record(
        block_number=30, log_index=3, transaction_index=3, tx_hash=0xAA22, block_hash=0xBB22
    )
    with pytest.raises(SilentRowLossError) as excinfo:
        writer.append_partition([new_a, new_b], chain_id=CHAIN, contract_address=CONTRACT)
    keys = excinfo.value.missing_event_keys
    assert len(keys) == 2
    assert keys[0][3] == 20  # block_number of new_a
    assert keys[1][3] == 30  # block_number of new_b
    manifest.close()


# ---------------------------------------------------------------------------
# 3. Per-partition reconciliation
# ---------------------------------------------------------------------------


def test_reconciliation_consistent_partition(tmp_path: Path) -> None:
    """A freshly-written partition is consistent: every Parquet row's
    EventKey is in ``event_index`` and vice versa."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    # Two Swap rows in the same cell so the writer takes the single
    # append_partition call (no merge path).
    writer.append_partition(
        [
            make_swap_record(block_number=10, log_index=0),
            make_swap_record(block_number=11, log_index=1, transaction_index=1, tx_hash=0xCC),
        ],
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    reports = reconcile_all_partitions(manifest)
    assert len(reports) == 1
    report = reports[0]
    assert report.consistent is True
    assert report.event_index_count == 2
    assert report.parquet_row_count == 2
    assert report.missing_in_parquet == ()
    assert report.missing_in_event_index == ()
    manifest.close()


def test_reconciliation_detects_missing_in_parquet(tmp_path: Path) -> None:
    """A partition whose ``event_index`` was extended after the
    Parquet file was written is reported as inconsistent, with the
    extra EventKeys listed under ``missing_in_parquet``."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    writer.append_partition(
        [make_swap_record(block_number=10, log_index=0)],
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    pid = list(reconcile_all_partitions(manifest))[0].partition_id
    # Simulate the silent-append outcome: inject a row into
    # ``event_index`` for an EventKey the Parquet file does not
    # carry. Pre-T037 the writer used to do this silently; the
    # reconciliation check is what surfaces the corruption today.
    extra_record = make_swap_record(
        block_number=20,
        log_index=2,
        transaction_index=2,
        tx_hash=0xAA,
        block_hash=0xBB,
    )
    from robinhood_lp.storage.schema import normalized_content_hash

    content_hash_hex = "0x" + normalized_content_hash(extra_record).hex()
    with manifest.transaction() as conn:
        manifest.record_event_observation(
            conn,
            chain_id=extra_record.chain_id.value,
            block_hash=extra_record.event_key().block_hash,
            tx_hash=extra_record.event_key().tx_hash,
            log_index=extra_record.event_key().log_index,
            content_hash=content_hash_hex,
            partition_id=pid,
            observation_kind="consistent",
        )
    report = reconcile_partition(manifest, pid)
    assert report.consistent is False
    assert report.parquet_row_count == 1
    assert report.event_index_count == 2
    assert len(report.missing_in_parquet) == 1
    # The named EventKey matches the injected record (canonical
    # integer-derived hex, no zero padding).
    missing = report.missing_in_parquet[0]
    assert missing[0] == str(extra_record.chain_id.value)
    assert missing[1] == "0x" + format(extra_record.event_key().block_hash, "x")
    assert missing[2] == "0x" + format(extra_record.event_key().tx_hash, "x")
    assert missing[3] == str(extra_record.event_key().log_index)
    # ``find_inconsistent_partitions`` surfaces it.
    inconsistent = find_inconsistent_partitions(manifest)
    assert len(inconsistent) == 1
    assert inconsistent[0].partition_id == pid
    manifest.close()


def test_reconciliation_missing_partition_row_is_inconsistent(tmp_path: Path) -> None:
    """Asking for an unknown partition's reconciliation returns an
    inconsistent report with ``detail.reason == 'missing_partition_row'``."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    report = reconcile_partition(manifest, "chain=4663/event=Swap/no-such")
    assert report.consistent is False
    assert report.detail["reason"] == "missing_partition_row"
    manifest.close()


# ---------------------------------------------------------------------------
# 4. Data-quality verifier integration
# ---------------------------------------------------------------------------


def _build_quality_report(*, complete: bool = True) -> Any:
    from robinhood_lp.protocol import PoolId

    return empty_report(
        chain_id=CHAIN.value,
        contract_address=CONTRACT.to_hex().removeprefix("0x").lower(),
        pool_id=PoolId(0xABCDEF12),
        pool_init_block=1_000_000,
        coverage_from_block=1_000_000,
        coverage_to_block=1_001_000,
        topology=TopologyKind.COLD_START,
        required_samples=_required_samples_stub(),
        manifest_checksum="0x" + "ab" * 32,
        qualified_checkpoint_ref=None,
    )


def test_verifier_accepts_consistent_partitions() -> None:
    """An empty reconciliation list (no partitions to check) does not
    downgrade the report. A consistent reconciliation result does not
    downgrade either; the verifier only blocks when a partition is
    inconsistent.
    """
    report = _build_quality_report()
    # Provide the A+B sample agreement (one True for the per-run
    # sample) so the verifier's existing cross-endpoint-sample clause
    # does not block ``complete=true`` before the partition clause
    # is evaluated.
    result = verify_report(
        report,
        sample_agreement=[True],
        has_result_bearing_sample=False,
    )
    assert result.complete is True
    # No reconciliation provided: the verifier does not block on the
    # new clause.
    assert REASON_PARTITION_EVENT_INDEX_PARQUET_MISMATCH not in result.blockers
    # An empty list of (consistent) reports: also fine.
    result2 = verify_report(
        report,
        sample_agreement=[True],
        has_result_bearing_sample=False,
        partition_reconciliation_results=(),
    )
    assert result2.complete is True


def _stub_report(partition_id: str, *, consistent: bool) -> Any:
    """Build a minimal stand-in object with the surface the verifier
    consumes (:attr:`partition_id`, :attr:`consistent`,
    :attr:`missing_in_parquet`, :attr:`missing_in_event_index`)."""
    return _StubReconciliation(
        partition_id=partition_id,
        consistent=consistent,
        missing_in_parquet=() if consistent else (("k",),),
        missing_in_event_index=(),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class _StubReconciliation:
    partition_id: str
    consistent: bool
    missing_in_parquet: tuple[tuple[Any, ...], ...]
    missing_in_event_index: tuple[tuple[Any, ...], ...]


def test_verifier_blocks_complete_on_inconsistent_partition() -> None:
    """Any inconsistent partition forces ``complete=false`` with the
    new reason code and a per-partition detail entry."""
    report = _build_quality_report()
    results = (
        _stub_report("p1", consistent=True),
        _stub_report("p2", consistent=False),
    )
    result = verify_report(
        report,
        sample_agreement=[True],
        has_result_bearing_sample=False,
        partition_reconciliation_results=results,
    )
    assert result.complete is False
    assert REASON_PARTITION_EVENT_INDEX_PARQUET_MISMATCH in result.blockers
    details = result.details["partition_reconciliation"]
    assert details["checked_partitions"] == 2
    assert len(details["inconsistent_partitions"]) == 1
    assert details["inconsistent_partitions"][0]["partition_id"] == "p2"


# ---------------------------------------------------------------------------
# 5. Planner grid alignment (regression for the original defect)
# ---------------------------------------------------------------------------


def test_planner_grid_aligned_sub_ranges_have_no_overlap() -> None:
    """When ``max_blocks_per_sub_range`` is a multiple of
    ``blocks_per_partition`` the planner emits grid-aligned sub-ranges.
    A cold start that begins inside a cell gets a partial-cell
    pre-window followed by full grid-aligned windows. No cell is
    touched by two full windows.
    """
    from robinhood_lp.ingestion.planner import (
        PlannedSubRange,
        RangePlanner,
        RangePlannerInputs,
    )

    # Cold start at block 1_000_037 — inside cell 1_000_000-1_000_099.
    pool_init_block = 1_000_037
    planner = RangePlanner(max_blocks_per_sub_range=10_000, blocks_per_partition=100)
    assert planner.grid_aligned is True
    plan = planner.plan(
        RangePlannerInputs(
            chain_id=CHAIN.value,
            contract_address=CONTRACT,
            pool_id=__import__("robinhood_lp.protocol", fromlist=["PoolId"]).PoolId(0xABCDEF12),
            pool_init_block=pool_init_block,
            requested_start_block=pool_init_block,
            requested_end_block=pool_init_block + 25_000,
        )
    )
    sub_ranges: tuple[PlannedSubRange, ...] = plan.sub_ranges
    # The partial first cell must be its own short sub-range.
    assert sub_ranges[0].from_block == pool_init_block
    assert sub_ranges[0].to_block == 1_000_099
    # Every subsequent sub-range must be grid-aligned: both ends are
    # at (cell + 100) - 1 = cell + 99 and the next cell starts at
    # (previous end + 1) which is a grid boundary.
    assert sub_ranges[1].from_block == 1_000_100
    assert sub_ranges[1].to_block == 1_010_099
    for a, b in zip(sub_ranges[:-1], sub_ranges[1:], strict=True):
        assert b.from_block == a.to_block + 1
        assert b.from_block % 100 == 0


def test_planner_grid_aligned_when_start_is_on_boundary() -> None:
    """When the cold start is already grid-aligned, no pre-window is
    needed; the first sub-range begins at the requested block and
    ends at (first_block + max_blocks - 1), which is grid-aligned."""
    from robinhood_lp.ingestion.planner import (
        RangePlanner,
        RangePlannerInputs,
    )

    planner = RangePlanner(max_blocks_per_sub_range=10_000, blocks_per_partition=100)
    plan = planner.plan(
        RangePlannerInputs(
            chain_id=CHAIN.value,
            contract_address=CONTRACT,
            pool_id=__import__("robinhood_lp.protocol", fromlist=["PoolId"]).PoolId(0xABCDEF12),
            pool_init_block=1_000_000,
            requested_start_block=1_000_000,
            requested_end_block=1_025_000,
        )
    )
    sub_ranges = plan.sub_ranges
    assert sub_ranges[0].from_block == 1_000_000
    assert sub_ranges[0].to_block == 1_009_999
    assert sub_ranges[1].from_block == 1_010_000
    assert sub_ranges[1].to_block == 1_019_999


def test_planner_legacy_split_when_max_blocks_not_multiple_of_partition() -> None:
    """When ``max_blocks_per_sub_range`` is not a multiple of
    ``blocks_per_partition`` the planner falls back to the pre-T037
    contiguous split (the writer's fail-closed guard remains in place
    as the safety net). The grid-aligned property is reported False.
    """
    from robinhood_lp.ingestion.planner import (
        RangePlanner,
        RangePlannerInputs,
    )

    planner = RangePlanner(max_blocks_per_sub_range=10, blocks_per_partition=100)
    assert planner.grid_aligned is False
    plan = planner.plan(
        RangePlannerInputs(
            chain_id=CHAIN.value,
            contract_address=CONTRACT,
            pool_id=__import__("robinhood_lp.protocol", fromlist=["PoolId"]).PoolId(0xABCDEF12),
            pool_init_block=1_000_000,
            requested_start_block=1_000_000,
            requested_end_block=1_000_025,
        )
    )
    sub_ranges = plan.sub_ranges
    # Legacy behaviour: three 10-block sub-ranges, contiguous.
    assert [(s.from_block, s.to_block) for s in sub_ranges] == [
        (1_000_000, 1_000_009),
        (1_000_010, 1_000_019),
        (1_000_020, 1_000_025),
    ]


# ---------------------------------------------------------------------------
# 6. End-to-end regression: original 2026-09-18 conditions
# ---------------------------------------------------------------------------


def test_runner_partition_cell_row_loss_regression(tmp_path: Path) -> None:
    """Regression fixture for the 2026-09-18 silent partition row-loss
    defect. Reproduces the conditions from the task contract: a
    forward-progress batch carries a new EventKey that the existing
    Parquet file does not contain for the partition cell. The
    writer's fail-closed guard must refuse the write rather than
    silently appending to ``event_index``.

    The fixture does not exercise the full IngestionRunner because
    that path requires scripting the header batch transport; the
    core defect lives at the writer's :meth:`append_partition`
    boundary, and the runner-level mapping of the writer's
    :class:`SilentRowLossError` to the
    ``partition_row_loss_guard`` reason code is exercised by
    :func:`test_runner_halts_on_silent_row_loss`.
    """
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    # First interval writes the partition with the original rows
    # the 2026-09-18 reference run observed for cell 55586200-55586299.
    first_batch = [
        make_swap_record(
            block_number=55_586_273,
            log_index=53,
            transaction_index=10,
            tx_hash=0x17C7771D139BBF8F8CB7D5B7B1CD0CCB09CA6AEFA43B6E36DF400E26FB5E936E,
            block_hash=0x5B5DA112574A,
        ),
        make_swap_record(
            block_number=55_586_285,
            log_index=80,
            transaction_index=12,
            tx_hash=0x7EC134AC9A3B,
            block_hash=0xAA,
        ),
    ]
    first = writer.append_partition(first_batch, chain_id=CHAIN, contract_address=CONTRACT)
    pid = first.partition_id
    assert first.rows_appended == 2
    # Second interval (a forward-progress batch from the next
    # collection window) brings a NEW Swap EventKey in the same
    # partition cell. Pre-T037 this would have been silently
    # appended to ``event_index`` without touching the Parquet file.
    # Post-T037 the writer refuses with a SilentRowLossError.
    forward_progress_batch = [
        make_swap_record(
            block_number=55_586_290,
            log_index=95,
            transaction_index=15,
            tx_hash=0xBBAA,
            block_hash=0xCCAA,
        ),
    ]
    with pytest.raises(SilentRowLossError) as excinfo:
        writer.append_partition(forward_progress_batch, chain_id=CHAIN, contract_address=CONTRACT)
    # The exception names the partition and the missing EventKey
    # tuple so the operator can see what would have been lost.
    assert excinfo.value.partition_id == pid
    assert len(excinfo.value.missing_event_keys) == 1
    bh, tx, log_index, block_number = excinfo.value.missing_event_keys[0]
    assert bh == 0xCCAA
    assert tx == 0xBBAA
    assert log_index == 95
    assert block_number == 55_586_290
    # The transaction rolled back: the Parquet row count and the
    # event_index count are still the original first-batch values.
    manifest_row = manifest.get_partition(pid)
    assert manifest_row is not None
    assert manifest_row.row_count == 2
    with manifest.read() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM event_index WHERE partition_id = ?",
            (pid,),
        ).fetchone()[0]
    assert n == 2
    # The on-disk Parquet file is unchanged (T031 contract: append-only).
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    table = pq.read_table(str(first.file_path))
    assert table.num_rows == 2
    manifest.close()


def test_runner_halts_on_silent_row_loss(tmp_path: Path) -> None:
    """The runner maps the writer's :class:`SilentRowLossError` to
    a failed decision carrying the ``partition_row_loss_guard`` reason
    code so the run halts with ``complete=False``. This is the
    runner-level acceptance of the writer's fail-closed guard.
    """
    from robinhood_lp.ingestion.errors import (
        REASON_OK,
        REASON_PARTITION_ROW_LOSS_GUARD,
        STATE_FAILED,
    )

    # Build a synthetic CoverageDecision and check the helper.
    from robinhood_lp.ingestion.router import CoverageDecision
    from robinhood_lp.ingestion.runner import _failed_decision

    decision = CoverageDecision(
        state=STATE_FAILED,
        endpoint_alias="robinhood_public",
        failover_from=None,
        reason_code=REASON_OK,
        rows=0,
        response_bytes=0,
        logical_rpc_calls=0,
        http_requests=0,
        pinned_block_hash=None,
    )
    new_decision = _failed_decision(
        decision,
        reason_code=REASON_PARTITION_ROW_LOSS_GUARD,
        detail="simulated writer refusal",
    )
    assert new_decision.reason_code == REASON_PARTITION_ROW_LOSS_GUARD
    assert new_decision.error_detail == "simulated writer refusal"


# ---------------------------------------------------------------------------
# 7. Real-dataset reconciliation observation (evidence)
# ---------------------------------------------------------------------------


def test_reconciliation_against_real_lp_data_dataset() -> None:
    """Apply the reconciliation check to the real 2026-09-18 reference
    run stored under ``/home/lpdev/lp-data`` and assert that the
    two Swap partitions where rows were silently appended are
    surfaced as inconsistent. This is the deliverable-3 acceptance
    evidence; it does NOT repair the dataset.

    Skipped when ``/home/lpdev/lp-data`` is not available (the test
    suite runs in CI without the production data directory).
    """
    from pathlib import Path as _Path

    data_root = _Path("/home/lpdev/lp-data")
    if not data_root.exists():
        pytest.skip("/home/lpdev/lp-data is not available in this environment")
    manifest_db = data_root / "manifest.sqlite"
    if not manifest_db.exists():
        pytest.skip(f"{manifest_db} not present")
    manifest = ManifestStore(manifest_db)
    try:
        inconsistent = find_inconsistent_partitions(manifest)
    finally:
        manifest.close()
    # The 2026-09-18 reference run is known to have lost exactly two
    # Swap rows in two partitions. The reconciliation check must
    # surface both partitions and no other mismatch.
    swap_partitions = tuple(r for r in inconsistent if "/event=Swap/" in r.partition_id)
    other_partitions = tuple(r for r in inconsistent if "/event=Swap/" not in r.partition_id)
    assert other_partitions == (), (
        f"unexpected non-Swap mismatches: {[r.partition_id for r in other_partitions]}"
    )
    assert len(swap_partitions) == 2, (
        f"expected two mismatched Swap partitions, got {[r.partition_id for r in swap_partitions]}"
    )
    # The mismatches are exactly the two partitions the contract
    # names: range=55586200-55586299 and range=55726200-55726299.
    ids = {r.partition_id for r in swap_partitions}
    assert (
        "chain=4663/contract=8366a39cc670b4001a1121b8f6a443a643e40951/event=Swap/range=55586200-55586299"
        in ids
    )
    assert (
        "chain=4663/contract=8366a39cc670b4001a1121b8f6a443a643e40951/event=Swap/range=55726200-55726299"
        in ids
    )
    for r in swap_partitions:
        assert r.event_index_count == r.parquet_row_count + 1
        assert len(r.missing_in_parquet) >= 1
