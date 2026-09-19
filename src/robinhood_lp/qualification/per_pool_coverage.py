"""Per-pool coverage report and cost record (T039 / ADR-015).

The T039 contract requires that each pool in the research universe
carry:

- its own **data root** (a directory or partition set whose SHA-256
  surfaces in the evidence pack);
- its own **per-pool coverage report** (a T034-shaped machine report
  per pool so one pool's coverage is never presented as another
  pool's result);
- its own **cost record** separating logical calls, HTTP requests,
  bytes, rows, provider units and elapsed time, with a declared
  budget ceiling the run respected.

This module is the deterministic, offline artifact that joins the
T039 :class:`PerPoolWindowPlan` with the per-pool acquisition
outputs the runner records. It is a value object whose ``to_dict``
serialises every pinned fact the research dataset registry
consumes; its ``to_markdown`` rendering is the audit-trail record
the workflow convention stores next to the plan.

The T038 two-pool ``PerPoolT034Report`` remains immutable historical
evidence; this module is the fresh T039 surface that supersedes
the T038 per-pool coverage shape (the T038 shape carried the
``pool_init_outside_window`` outcome, which the T039 / ADR-015
rule no longer admits).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from robinhood_lp.qualification.per_pool_window import (
    OUTCOME_POOL_ACQUIRED,
    OUTCOME_POOL_ACQUIRED_AFTER_RESUME,
    OUTCOME_POOL_INITIALIZED_AFTER_END,
    OUTCOME_POOL_RESOLUTION_FAILED,
    REASON_RANGE_ALREADY_COVERED,
    PerPoolWindowPlan,
)

# ---------------------------------------------------------------------------
# Per-pool data root
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerPoolDataRoot:
    """One pool's data root in the research dataset registry.

    The T039 contract names a per-pool data root so the research
    dataset registry can join the per-pool coverage report against
    the persisted partitions without ambiguity. ``data_root_path``
    is the directory the runner wrote under; ``manifest_checksum``
    is the SHA-256 of the per-pool manifest the runner produced;
    ``partition_ids`` is the ordered tuple of partition identifiers
    the per-pool acquisition produced. The validator rejects an
    empty ``data_root_path`` so a missing data root never silently
    appears as a successful acquisition.
    """

    pool_key_id: str
    data_root_path: str
    manifest_checksum: str
    partition_ids: tuple[str, ...]
    schema_version: int
    decode_version: int

    def __post_init__(self) -> None:
        if not self.pool_key_id:
            raise ValueError("PerPoolDataRoot: pool_key_id must be a non-empty string")
        if not self.data_root_path:
            raise ValueError("PerPoolDataRoot: data_root_path must be a non-empty string")
        if not self.manifest_checksum:
            raise ValueError("PerPoolDataRoot: manifest_checksum must be a non-empty string")
        for partition_id in self.partition_ids:
            if not partition_id:
                raise ValueError("PerPoolDataRoot: partition_ids must be non-empty strings")
        if self.schema_version <= 0:
            raise ValueError(
                f"PerPoolDataRoot: schema_version must be > 0, got {self.schema_version}"
            )
        if self.decode_version <= 0:
            raise ValueError(
                f"PerPoolDataRoot: decode_version must be > 0, got {self.decode_version}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_key_id": self.pool_key_id,
            "data_root_path": self.data_root_path,
            "manifest_checksum": self.manifest_checksum,
            "partition_ids": list(self.partition_ids),
            "schema_version": self.schema_version,
            "decode_version": self.decode_version,
        }


# ---------------------------------------------------------------------------
# Cost record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerPoolCostRecord:
    """The cost record the T039 contract requires per pool.

    The contract separates logical calls, HTTP requests, response
    bytes, rows, provider units and elapsed time so the operator
    can reconcile the observed cost against the declared budget
    ceiling. ``exhausted_budget_dimension`` is the dimension the
    runner observed reaching the ceiling on; an empty string
    records a clean run that finished within budget. The
    ``elapsed_ms`` field is the wall-clock duration the runner
    recorded, exposed for operator-side budgeting only — the
    contract never uses wall clock as a chain-bound input.
    """

    pool_key_id: str
    logical_calls: int
    http_requests: int
    response_bytes: int
    rows: int
    provider_units: int
    elapsed_ms: int
    parquet_bytes: int = 0
    exhausted_budget_dimension: str = ""

    def __post_init__(self) -> None:
        if not self.pool_key_id:
            raise ValueError("PerPoolCostRecord: pool_key_id must be a non-empty string")
        if self.logical_calls < 0:
            raise ValueError(
                f"PerPoolCostRecord: logical_calls must be >= 0, got {self.logical_calls}"
            )
        if self.http_requests < 0:
            raise ValueError(
                f"PerPoolCostRecord: http_requests must be >= 0, got {self.http_requests}"
            )
        if self.response_bytes < 0:
            raise ValueError(
                f"PerPoolCostRecord: response_bytes must be >= 0, got {self.response_bytes}"
            )
        if self.rows < 0:
            raise ValueError(f"PerPoolCostRecord: rows must be >= 0, got {self.rows}")
        if self.provider_units < 0:
            raise ValueError(
                f"PerPoolCostRecord: provider_units must be >= 0, got {self.provider_units}"
            )
        if self.elapsed_ms < 0:
            raise ValueError(f"PerPoolCostRecord: elapsed_ms must be >= 0, got {self.elapsed_ms}")
        if self.parquet_bytes < 0:
            raise ValueError(
                f"PerPoolCostRecord: parquet_bytes must be >= 0, got {self.parquet_bytes}"
            )
        if self.exhausted_budget_dimension not in (
            "",
            "logical_calls",
            "http_requests",
            "response_bytes",
            "rows",
            "provider_units",
            "elapsed_ms",
        ):
            raise ValueError(
                f"PerPoolCostRecord: exhausted_budget_dimension {self.exhausted_budget_dimension!r} "
                "is not a documented budget dimension"
            )

    @property
    def exhausted_budget(self) -> bool:
        """``True`` iff the run halted because a dimension reached its ceiling."""
        return bool(self.exhausted_budget_dimension)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_key_id": self.pool_key_id,
            "logical_calls": self.logical_calls,
            "http_requests": self.http_requests,
            "response_bytes": self.response_bytes,
            "rows": self.rows,
            "provider_units": self.provider_units,
            "elapsed_ms": self.elapsed_ms,
            "parquet_bytes": self.parquet_bytes,
            "exhausted_budget_dimension": self.exhausted_budget_dimension,
        }


# ---------------------------------------------------------------------------
# Reconciliation status
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerPoolReconciliation:
    """The per-pool T037 reconciliation verdict.

    The T039 contract re-uses the T037 partition reconciliation path:
    ``event_index`` and the on-disk Parquet file must agree for
    every partition, or the per-pool coverage report refuses to
    claim ``complete=true``. The reconciliation is recorded per
    pool so one pool's disagreement never silently fails another
    pool's coverage.

    ``disagreements`` lists the partition identifiers the runner
    observed disagreeing; an empty list means the per-pool
    reconciliation agreed on every partition.
    """

    pool_key_id: str
    partition_event_index_parquet_agreement: bool
    partition_reconciliation_agreement: bool
    partitions_checked: int
    disagreements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.pool_key_id:
            raise ValueError("PerPoolReconciliation: pool_key_id must be a non-empty string")
        if self.partitions_checked < 0:
            raise ValueError(
                f"PerPoolReconciliation: partitions_checked must be >= 0, "
                f"got {self.partitions_checked}"
            )
        for partition_id in self.disagreements:
            if not partition_id:
                raise ValueError(
                    "PerPoolReconciliation: disagreements must be non-empty partition ids"
                )

    @property
    def is_clean(self) -> bool:
        return (
            self.partition_event_index_parquet_agreement
            and self.partition_reconciliation_agreement
            and not self.disagreements
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_key_id": self.pool_key_id,
            "partition_event_index_parquet_agreement": (
                self.partition_event_index_parquet_agreement
            ),
            "partition_reconciliation_agreement": (self.partition_reconciliation_agreement),
            "partitions_checked": self.partitions_checked,
            "disagreements": list(self.disagreements),
        }


# ---------------------------------------------------------------------------
# Per-pool coverage report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerPoolCoverageReport:
    """The T039 per-pool coverage report.

    The report satisfies the existing T034 machine-report format:
    every range carries a verdict (``complete`` / ``incomplete`` /
    ``failed_resolution``), the per-pool data root, the per-pool
    cost record, and the per-pool reconciliation verdict. The
    report is a value object: tests construct it directly from
    deterministic inputs and the operator runbook surfaces its
    ``to_dict`` rendering.

    ``coverage_outcome`` is one of:

    - ``complete`` — every interval ``successful`` or
      ``scanned_empty``, reconciliation agrees, no exhausted
      budget, and no unresolved failure path;
    - ``incomplete`` — reconciliation disagreements, exhausted
      budget, or one or more ``failed`` / ``cancelled``
      intervals;
    - ``already_covered`` — the durable checkpoint already
      qualified the requested window so the run issued no new
      chain reads and reproduced byte-equivalent canonical output;
    - ``failed_resolution`` — the planner halted before issuing
      chain reads (the pool's ``Initialize`` could not be located
      or lies after the agreed end).
    """

    pool_key_id: str
    chain_id: int
    pool_id_hex: str
    data_root: PerPoolDataRoot
    coverage_from_block: int
    coverage_to_block: int
    coverage_outcome: str
    plan: PerPoolWindowPlan
    cost_record: PerPoolCostRecord
    reconciliation: PerPoolReconciliation
    findings: tuple[dict[str, Any], ...] = ()
    qualification_blockers: tuple[str, ...] = ()
    complete: bool = False

    _VALID_OUTCOMES: tuple[str, ...] = (
        "complete",
        "incomplete",
        "already_covered",
        "failed_resolution",
    )

    def __post_init__(self) -> None:
        if not self.pool_key_id:
            raise ValueError("PerPoolCoverageReport: pool_key_id must be a non-empty string")
        if self.coverage_outcome not in self._VALID_OUTCOMES:
            raise ValueError(
                f"PerPoolCoverageReport: coverage_outcome {self.coverage_outcome!r} "
                f"not in {self._VALID_OUTCOMES!r}"
            )
        if self.chain_id <= 0:
            raise ValueError(f"PerPoolCoverageReport: chain_id must be > 0, got {self.chain_id}")
        if not self.pool_id_hex:
            raise ValueError("PerPoolCoverageReport: pool_id_hex must be a non-empty 0x-hex string")
        if self.coverage_from_block < 0:
            raise ValueError(
                f"PerPoolCoverageReport: coverage_from_block must be >= 0, "
                f"got {self.coverage_from_block}"
            )
        # ``coverage_from_block > coverage_to_block`` is the empty-window
        # representation the planner emits for a failed-resolution plan
        # (``pool_initialized_after_agreed_end``). The coverage window
        # width is ``0`` in that case; the report is allowed to surface
        # the empty-window shape so the audit trail can show the run
        # halted before any chain read. The ``complete`` verdict the
        # builder derives forbids the empty window because every clean
        # acquisition has a non-empty coverage range.

    @property
    def coverage_window_width(self) -> int:
        return self.coverage_to_block - self.coverage_from_block + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "robinhood_lp.qualification.per_pool_coverage.v1",
            "pool_key_id": self.pool_key_id,
            "chain_id": self.chain_id,
            "pool_id": self.pool_id_hex,
            "data_root": self.data_root.to_dict(),
            "coverage_from_block": self.coverage_from_block,
            "coverage_to_block": self.coverage_to_block,
            "coverage_window_width": self.coverage_window_width,
            "coverage_outcome": self.coverage_outcome,
            "plan": self.plan.to_dict(),
            "cost_record": self.cost_record.to_dict(),
            "reconciliation": self.reconciliation.to_dict(),
            "findings": [dict(f) for f in self.findings],
            "qualification_blockers": list(self.qualification_blockers),
            "complete": bool(self.complete),
        }

    def to_markdown(self) -> str:
        """Render the report as Markdown for the audit trail.

        The rendering names the pinned finalized end, the per-pool
        data root, the cost record dimensions, the reconciliation
        verdict and the per-pool findings. It is the audit-trail
        surface the workflow convention stores next to the
        coverage report.
        """
        lines: list[str] = []
        lines.append(f"# Per-pool coverage report — {self.pool_key_id}")
        lines.append("")
        lines.append(f"- chain_id: `{self.chain_id}`")
        lines.append(f"- pool_id: `{self.pool_id_hex}`")
        lines.append(f"- coverage_from_block: **{self.coverage_from_block:,}**")
        lines.append(f"- coverage_to_block: **{self.coverage_to_block:,}**")
        lines.append(f"- coverage_window_width: **{self.coverage_window_width:,}** blocks")
        lines.append(f"- coverage_outcome: **{self.coverage_outcome}**")
        lines.append(f"- complete: **{self.complete}**")
        lines.append("")
        lines.append("## Pinned window")
        lines.append("")
        lines.append(
            f"- finalized end block: **{self.plan.window_pin_block_number:,}** "
            f"(`{self.plan.window_pin_block_hash}`)"
        )
        lines.append(
            f"- pool Initialize block: **{self.plan.pool_init_block:,}** "
            f"(`{self.plan.pool_init_block_hash}`)"
        )
        lines.append(f"- window outcome: `{self.plan.outcome}`")
        lines.append(f"- reason code: `{self.plan.reason_code}`")
        lines.append("")
        lines.append("## Per-pool data root")
        lines.append("")
        lines.append(f"- data_root_path: `{self.data_root.data_root_path}`")
        lines.append(f"- manifest_checksum: `{self.data_root.manifest_checksum}`")
        lines.append(f"- partition_ids: `{','.join(self.data_root.partition_ids) or '—'}`")
        lines.append("")
        lines.append("## Cost record")
        lines.append("")
        cost = self.cost_record
        lines.append(
            f"- logical_calls: {cost.logical_calls:,} / {self.plan.budget_ceiling.logical_calls:,}"
        )
        lines.append(
            f"- http_requests: {cost.http_requests:,} / {self.plan.budget_ceiling.http_requests:,}"
        )
        lines.append(
            f"- response_bytes: {cost.response_bytes:,} / "
            f"{self.plan.budget_ceiling.response_bytes:,}"
        )
        lines.append(f"- rows: {cost.rows:,} / {self.plan.budget_ceiling.rows:,}")
        lines.append(
            f"- provider_units: {cost.provider_units:,} / "
            f"{self.plan.budget_ceiling.provider_units:,}"
        )
        lines.append(f"- elapsed_ms: {cost.elapsed_ms:,} / {self.plan.budget_ceiling.elapsed_ms:,}")
        if cost.exhausted_budget:
            lines.append(f"- exhausted_budget_dimension: `{cost.exhausted_budget_dimension}`")
        lines.append("")
        lines.append("## Reconciliation")
        lines.append("")
        rec = self.reconciliation
        lines.append(
            f"- partition_event_index_parquet_agreement: "
            f"{rec.partition_event_index_parquet_agreement}"
        )
        lines.append(
            f"- partition_reconciliation_agreement: {rec.partition_reconciliation_agreement}"
        )
        lines.append(f"- partitions_checked: {rec.partitions_checked}")
        if rec.disagreements:
            lines.append("- disagreements: " + ", ".join(f"`{p}`" for p in rec.disagreements))
        lines.append("")
        if self.findings:
            lines.append("## Findings")
            lines.append("")
            for finding in self.findings:
                code = finding.get("reason_code", "unknown")
                category = finding.get("category", "unknown")
                lines.append(f"- `{code}` ({category})")
            lines.append("")
        if self.qualification_blockers:
            lines.append("## Blockers")
            lines.append("")
            for blocker in self.qualification_blockers:
                lines.append(f"- `{blocker}`")
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_per_pool_coverage_report(
    *,
    plan: PerPoolWindowPlan,
    data_root: PerPoolDataRoot,
    cost_record: PerPoolCostRecord,
    reconciliation: PerPoolReconciliation,
    findings: tuple[dict[str, Any], ...] = (),
    pool_id_hex: str = "",
) -> PerPoolCoverageReport:
    """Assemble the per-pool coverage report from the per-pool pipeline.

    The function is deterministic and offline: every input is
    either the planner output, the data root the runner produced,
    the cost record the runner accumulated, or the per-pool
    reconciliation verdict the T037 path emitted. The function
    decides the ``coverage_outcome`` and the ``complete`` verdict
    from those inputs:

    - ``complete`` is ``True`` only when the planner recorded a
      successful acquisition (the window was acquired or resumed
      and finished without exhausting the budget), the
      reconciliation agreed on every partition, and there are no
      qualification blockers;
    - ``already_covered`` is the verbatim reproduction case the
      T039 contract describes: the planner recorded a
      ``range_already_covered`` reason code and the run issued
      no new chain reads;
    - ``failed_resolution`` is the planner's halted outcome
      (``pool_initialized_after_agreed_end`` or
      ``pool_initialize_resolution_failed``); the report records
      the planner's reason code as a blocker;
    - ``incomplete`` covers every other case (exhausted budget,
      partition disagreement, etc.).

    The function never accepts a hidden ``pool_init_outside_window``
    case — the per-pool coverage report refuses to construct that
    outcome because T039 / ADR-015 retired the ten-million-block
    rule that produced it.
    """
    blockers: list[str] = []
    plan_blocker = ""
    if plan.outcome == OUTCOME_POOL_ACQUIRED:
        if plan.reason_code == REASON_RANGE_ALREADY_COVERED:
            coverage_outcome = "already_covered"
        else:
            coverage_outcome = "complete"
    elif plan.outcome == OUTCOME_POOL_ACQUIRED_AFTER_RESUME:
        coverage_outcome = "complete"
    elif plan.outcome in (
        OUTCOME_POOL_INITIALIZED_AFTER_END,
        OUTCOME_POOL_RESOLUTION_FAILED,
    ):
        coverage_outcome = "failed_resolution"
        plan_blocker = plan.reason_code
    else:  # pragma: no cover - new outcomes must update the builder
        coverage_outcome = "incomplete"
        plan_blocker = plan.reason_code

    if not reconciliation.is_clean:
        blockers.append("partition_reconciliation_disagreement")
    if cost_record.exhausted_budget:
        blockers.append("budget_exhausted")
    if plan_blocker:
        blockers.append(plan_blocker)
    for blocker in (
        "finalized_unavailable",
        "finalized_endpoint_disagreement",
        "latest_substitution_refused",
        "wall_clock_bound_refused",
        "non_finalized_block_refused",
        "endpoint_historical_state_unavailable",
        "provider_returned_non_finalized_for_finalized_tag",
        "range_too_large",
        "http_429_rate_limit",
    ):
        if plan.reason_code == blocker and blocker not in blockers:
            blockers.append(blocker)

    explicit_blockers = tuple(b for b in (plan_blocker,) if b and b not in blockers)
    all_blockers = tuple(blockers) + explicit_blockers

    complete = (
        coverage_outcome == "complete"
        and reconciliation.is_clean
        and not cost_record.exhausted_budget
        and not all_blockers
    )

    return PerPoolCoverageReport(
        pool_key_id=plan.pool_key_id,
        chain_id=plan.chain_id,
        pool_id_hex=pool_id_hex,
        data_root=data_root,
        coverage_from_block=plan.coverage_from_block,
        coverage_to_block=plan.coverage_to_block,
        coverage_outcome=coverage_outcome,
        plan=plan,
        cost_record=cost_record,
        reconciliation=reconciliation,
        findings=findings,
        qualification_blockers=tuple(all_blockers),
        complete=complete,
    )


def reconcile_per_pool_reports_against_partitions(
    reports: Mapping[str, PerPoolCoverageReport],
    *,
    partition_owners: Mapping[str, str],
) -> dict[str, list[str]]:
    """Reconcile the per-pool reports against the acquired partitions.

    The function is the audit-trail surface that confirms every
    ``partition_id`` the runner recorded belongs to exactly one
    per-pool report, and every per-pool report's
    ``partition_ids`` matches the partitions the runner recorded.
    A partition appearing under two pool keys, or missing from the
    partition-owner map, is reported as a discrepancy so the audit
    trail detects any cross-pool attribution before claiming
    coverage.

    The return value maps each pool key to the list of discrepancy
    messages the audit trail records; an empty list per pool means
    the per-pool reconciliation against the partitions is clean.
    """
    discrepancies: dict[str, list[str]] = {key: [] for key in reports}
    seen_partitions: dict[str, str] = {}
    for pool_key, partition_id in partition_owners.items():
        if partition_id in seen_partitions and seen_partitions[partition_id] != pool_key:
            discrepancies.setdefault(pool_key, []).append(
                f"partition {partition_id!r} already owned by {seen_partitions[partition_id]!r}"
            )
            other = seen_partitions[partition_id]
            discrepancies.setdefault(other, []).append(
                f"partition {partition_id!r} also owned by {pool_key!r}"
            )
            continue
        seen_partitions[partition_id] = pool_key
        if pool_key not in reports:
            continue
        if partition_id not in reports[pool_key].data_root.partition_ids:
            discrepancies[pool_key].append(
                f"partition {partition_id!r} not in per-pool report's partition_ids"
            )
    for pool_key, report in reports.items():
        declared = set(report.data_root.partition_ids)
        recorded = {pid for pid, owner in partition_owners.items() if owner == pool_key}
        if declared != recorded:
            only_declared = declared - recorded
            only_recorded = recorded - declared
            if only_declared:
                discrepancies[pool_key].append(
                    "partitions declared but not recorded: " + ", ".join(sorted(only_declared))
                )
            if only_recorded:
                discrepancies[pool_key].append(
                    "partitions recorded but not declared: " + ", ".join(sorted(only_recorded))
                )
    return discrepancies


__all__ = [
    "PerPoolCostRecord",
    "PerPoolCoverageReport",
    "PerPoolDataRoot",
    "PerPoolReconciliation",
    "build_per_pool_coverage_report",
    "reconcile_per_pool_reports_against_partitions",
]
