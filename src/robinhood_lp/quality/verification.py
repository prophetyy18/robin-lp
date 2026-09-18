"""Report verification — decides ``complete=true`` vs ``complete=false``.

The T034 acceptance criteria require ``complete=true`` only when **all**
of:

- every gap, error, and capability regression is named with a reason
  code and is either resolved or explicitly recorded as a known
  limitation;
- no ``budget_exhausted`` reason code is raised;
- every required sample from Decision 6 has been executed and the two
  endpoints agreed on every comparison;
- the dataset manifest checksum is recorded in the report and is
  reproducible from the durable manifest store.

This module is the deterministic verifier the report builder calls.
It applies the rules in order and returns the verdict plus a tuple
of ``qualification_blockers`` describing every clause that failed.

The verifier never silently downgrades a passing report. A passing
report that fails any clause is downgraded to ``complete=false``
with the failing clause surfaced as a blocker.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from robinhood_lp.quality.quality_report import (
    QualityFinding,
    QualityReport,
)
from robinhood_lp.quality.reason_codes import (
    REASON_BUDGET_EXHAUSTED,
    REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE,
    REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
    REASON_PARTITION_EVENT_INDEX_PARQUET_MISMATCH,
)

#: The set of reason codes that prevent ``complete=true``. Any
#: finding with one of these codes forces the verifier to return
#: ``complete=false`` and the code is recorded as a blocker.
_BLOCKING_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        REASON_BUDGET_EXHAUSTED,
        REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
        REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE,
        REASON_PARTITION_EVENT_INDEX_PARQUET_MISMATCH,
    }
)


class _ReconciliationLike(Protocol):
    """The minimum surface :func:`verify_report` consumes from a
    T037 partition reconciliation report.

    Defined as a :class:`Protocol` so the verifier does not import
    the concrete :class:`PartitionReconciliationReport` class from
    :mod:`robinhood_lp.storage.reconciliation` (which would couple
    the quality module to the storage module).
    """

    partition_id: str
    consistent: bool
    missing_in_parquet: tuple[tuple[Any, ...], ...]
    missing_in_event_index: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The verifier's verdict.

    ``complete`` mirrors :attr:`QualityReport.complete`. ``blockers``
    lists every clause that contributed to ``complete=false``. The
    verifier records each blocker exactly once; downstream code reads
    the blockers to understand what must be fixed.
    """

    complete: bool
    blockers: tuple[str, ...]
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "blockers": list(self.blockers),
            "details": dict(self.details),
        }


def verify_report(
    report: QualityReport,
    *,
    expected_manifest_checksum: str | None = None,
    sample_agreement: Iterable[bool] | None = None,
    has_result_bearing_sample: bool | None = None,
    empty_capability_probe_only: bool = False,
    partition_reconciliation_results: Sequence[_ReconciliationLike] | None = None,
) -> VerificationResult:
    """Verify ``report`` against the acceptance criteria.

    Parameters
    ----------
    report:
        The :class:`QualityReport` to verify.
    expected_manifest_checksum:
        The manifest checksum the durable manifest store expects.
        When provided, ``report.manifest_checksum`` must equal this
        value exactly. Mismatch produces the ``manifest_checksum``
        blocker.
    sample_agreement:
        One boolean per required Decision 6 sample, in the order
        (per-run, per-partition..., per-event-type..., per-failover...).
        ``True`` means A and B agreed. ``False`` produces the
        ``cross_endpoint_sample_disagree`` blocker. ``None`` produces
        the ``cross_endpoint_sample_missing`` blocker for any
        required sample.
    has_result_bearing_sample:
        When ``False``, the verifier records the
        ``result_bearing_required`` blocker (no result-bearing sample).
    empty_capability_probe_only:
        When ``True``, the verifier records the
        ``empty_capability_probe_only`` blocker — an all-empty
        capability probe proves range acceptance only, never event
        completeness.
    partition_reconciliation_results:
        T037 reconciliation reports, one per partition (the result
        of :func:`robinhood_lp.storage.reconciliation.reconcile_all_partitions`).
        Any report whose ``consistent`` flag is False forces
        ``complete=false`` with the
        ``partition_event_index_parquet_mismatch`` reason code, plus
        a per-partition blocker that names the partition. When the
        parameter is ``None`` the partition reconciliation clause is
        skipped (preserves the pre-T037 caller surface; the
        reconciliation path is opt-in).
    """
    blockers: list[str] = []
    details: dict[str, Any] = {}

    findings_by_reason = _group_findings_by_reason(report.findings)

    # 1. Zero unexplained gaps / errors: every blocker-class code is
    #    either absent or explicitly named with the reason code.
    for code in _BLOCKING_REASON_CODES:
        if code in findings_by_reason:
            blockers.append(code)

    # 2. No exhausted budget: redundant with the blocker set above
    #    but surfaced as an explicit clause per the contract.
    if REASON_BUDGET_EXHAUSTED in findings_by_reason and REASON_BUDGET_EXHAUSTED not in blockers:
        blockers.append(REASON_BUDGET_EXHAUSTED)

    # 3. Successful required A+B samples.
    if sample_agreement is not None:
        sample_blockers = _check_sample_agreement(
            report, sample_agreement, has_result_bearing_sample, empty_capability_probe_only
        )
        blockers.extend(sample_blockers)
    else:
        # No sample agreement supplied: every required sample is
        # recorded as missing.
        blockers.append(REASON_CROSS_ENDPOINT_SAMPLE_MISSING)

    # 4. Dataset manifest checksum recorded and reproducible.
    if not report.manifest_checksum:
        blockers.append("manifest_checksum_unrecorded")
    elif (
        expected_manifest_checksum is not None
        and report.manifest_checksum != expected_manifest_checksum
    ):
        blockers.append("manifest_checksum_drift")

    # 5. Cold start / warm run basis must be satisfied for complete=true.
    topology_blocker = _check_topology_basis(report)
    if topology_blocker is not None:
        blockers.append(topology_blocker)

    # 6. Infrastructure-correlation state must be set (never blank).
    if report.infrastructure_state is None or report.infrastructure_state == "":
        blockers.append("infrastructure_correlation_state_unrecorded")

    # 7. T037 — per-partition reconciliation must be consistent.
    #    Any partition whose ``event_index`` set disagrees with its
    #    Parquet file (the silent row-loss defect) blocks
    #    ``complete=true`` even when no other finding is raised.
    if partition_reconciliation_results is not None:
        reconciliation_blockers, reconciliation_details = _check_partition_reconciliation(
            partition_reconciliation_results
        )
        blockers.extend(reconciliation_blockers)
        details["partition_reconciliation"] = reconciliation_details

    complete = len(blockers) == 0
    return VerificationResult(
        complete=complete,
        blockers=tuple(blockers),
        details=details,
    )


def _group_findings_by_reason(
    findings: Iterable[QualityFinding],
) -> dict[str, list[QualityFinding]]:
    out: dict[str, list[QualityFinding]] = {}
    for f in findings:
        out.setdefault(f.reason_code, []).append(f)
    return out


def _check_sample_agreement(
    report: QualityReport,
    sample_agreement: Iterable[bool],
    has_result_bearing_sample: bool | None,
    empty_capability_probe_only: bool,
) -> list[str]:
    """Verify the Decision 6 sample agreement iterable."""
    blockers: list[str] = []
    # The required sample count: per-run + per-partition + per-event-type + per-failover.
    required = (
        1
        + len(report.required_samples.per_partition)
        + len(report.required_samples.per_event_type)
        + len(report.required_samples.per_failover)
    )
    agreement_list = list(sample_agreement)
    if len(agreement_list) != required:
        blockers.append(REASON_CROSS_ENDPOINT_SAMPLE_MISSING)
        return blockers
    if not all(agreement_list):
        blockers.append(REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE)
    # Result-bearing requirement: at least one real-event comparison
    # must be present when the dataset contains any events.
    dataset_has_events = bool(report.required_samples.result_bearing_windows)
    if dataset_has_events:
        if has_result_bearing_sample is False:
            blockers.append("result_bearing_required")
        if empty_capability_probe_only:
            blockers.append("empty_capability_probe_only")
    return blockers


def _check_topology_basis(report: QualityReport) -> str | None:
    """Return a blocker if the cold-start / warm-run basis is not
    satisfied.

    The cold-start basis: ``coverage_from_block <= pool_init_block``.
    The warm-run basis: a qualified checkpoint ref is recorded.
    The backtest-window-only basis: a backtest window without
    Initialize or qualified checkpoint is incomplete.
    """
    from robinhood_lp.quality.quality_report import TopologyKind

    if report.topology == TopologyKind.COLD_START:
        if report.coverage_from_block > report.pool_init_block:
            return "cold_start_initialize_block_not_reached"
        return None
    if report.topology == TopologyKind.WARM_INCREMENTAL:
        if not report.qualified_checkpoint_ref:
            return "warm_run_qualified_checkpoint_missing"
        return None
    if report.topology == TopologyKind.BACKTEST_WINDOW_ONLY:
        # A backtest-window-only download without an Initialize or
        # qualified checkpoint is incomplete; the verifier returns a
        # blocker so downstream code refuses the dataset.
        if (
            report.coverage_from_block > report.pool_init_block
            and not report.qualified_checkpoint_ref
        ):
            return "backtest_window_only_no_initialize_or_checkpoint"
        return None
    return None


def _check_partition_reconciliation(
    reports: Sequence[_ReconciliationLike],
) -> tuple[list[str], dict[str, Any]]:
    """Return the verifier-side blockers and details for the T037
    partition reconciliation clause.

    The function records the umbrella reason code in the blocker
    list (once, even when multiple partitions disagree) and the
    per-partition partition id + verdict in the returned details
    dict so downstream tooling can show which partition failed.
    """
    inconsistent = [r for r in reports if not r.consistent]
    if not inconsistent:
        return [], {
            "checked_partitions": len(reports),
            "inconsistent_partitions": [],
        }
    blockers: list[str] = [REASON_PARTITION_EVENT_INDEX_PARQUET_MISMATCH]
    per_partition: list[dict[str, Any]] = []
    for r in inconsistent:
        per_partition.append(
            {
                "partition_id": r.partition_id,
                "missing_in_parquet_count": len(r.missing_in_parquet),
                "missing_in_event_index_count": len(r.missing_in_event_index),
            }
        )
    return blockers, {
        "checked_partitions": len(reports),
        "inconsistent_partitions": per_partition,
    }


__all__ = [
    "VerificationResult",
    "verify_report",
]
