"""Data-quality and completeness reports (T034).

The T034 report is the single source of truth downstream code reads
before loading a dataset. It captures:

- **machine-readable** report rows (range coverage, block/hash gaps,
  duplicate / misordered logs, unknown pools, decode errors,
  impossible values, stale endpoints, metadata failures,
  cross-provider discrepancies, etc.), every row carrying a
  reason code;
- **human-readable** summary (qualification verdict, chain identity,
  pool identifier, start / end block, checkpoint reference, manifest
  checksum, infrastructure-correlation state, generation timestamp);
- the **preflight / actual call budget** ledger (paired, with
  deviations surfaced as distinct rows);
- the **endpoint capability snapshot** (per endpoint, captured before
  the scan starts);
- the **provider-to-interval provenance** (which provider served
  each interval, failover path, retry count, response size,
  durable-commit checksum);
- the **response / storage volume**;
- the **deterministic cross-provider sample selection** (Decision 6)
  with the selection inputs recorded per sample;
- the **infrastructure-correlation evidence state** (Decision 8) with
  the ``cross_endpoint_agreement`` wording unless the state is
  ``evidenced_independent``;
- the **recorded known-common operator / upstream infrastructure**.

The qualification verdict is :attr:`QualityReport.complete`. It is
``True`` only when every required sample agrees and no other gap /
error / budget failure exists; otherwise ``False``. Downstream code
must refuse the dataset when ``complete`` is ``False`` or unset.

The report is deliberately a value object. The construction helpers
(:func:`empty_report`, :meth:`QualityReport.with_finding`,
:meth:`QualityReport.with_infrastructure_state`, ...) make it
straightforward to assemble a report from the runner / reorg
handler / storage reader rows without losing any required field.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from robinhood_lp.protocol.ids import PoolId
from robinhood_lp.quality.infrastructure import (
    DEFAULT_INFRASTRUCTURE_CORRELATION_STATE,
    InfrastructureCorrelationState,
    agreement_phrasing_for_state,
    correlated_failure_is_residual_risk,
)
from robinhood_lp.quality.reason_codes import (
    ALL_REASON_CODES,
    REASON_BUDGET_EXHAUSTED,
    REASON_CHECKPOINT_MISMATCH,
    REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE,
    REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
    is_qualification_fatal_reason,
)
from robinhood_lp.quality.sample_selection import RequiredSamples

# ---------------------------------------------------------------------------
# Cold-start / warm-run topology labels
# ---------------------------------------------------------------------------


class TopologyKind(StrEnum):
    """Topology label used by the report for cold-start vs warm-run
    coverage verification.

    The mapping mirrors the T032 ``TOPOLOGY_COLD_START`` /
    ``TOPOLOGY_WARM_INCREMENTAL`` constants; the report uses its own
    enum to keep the qualification module self-contained.
    """

    COLD_START = "cold_start"
    WARM_INCREMENTAL = "warm_incremental"
    BACKTEST_WINDOW_ONLY = "backtest_window_only"


# ---------------------------------------------------------------------------
# Finding dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualityFinding:
    """One row of the machine-readable report.

    Every finding carries a ``reason_code`` and a ``category`` so the
    human report can group findings without parsing prose.
    """

    reason_code: str
    category: str
    detail: Mapping[str, Any]
    highest_severity: int = 1

    def __post_init__(self) -> None:
        if self.reason_code not in ALL_REASON_CODES:
            raise ValueError(
                f"QualityFinding: reason_code {self.reason_code!r} not in ALL_REASON_CODES"
            )
        if not isinstance(self.category, str) or not self.category:
            raise ValueError(
                f"QualityFinding: category must be non-empty str, got {self.category!r}"
            )


# ---------------------------------------------------------------------------
# Budget ledger dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreflightBudgetEntry:
    """The preflight budget allocation per endpoint per role."""

    alias: str
    role: str
    remaining_calls: int
    remaining_compute_units: int | None
    remaining_seconds: float
    remaining_response_quota: int | None
    remaining_budget_source: str


@dataclass(frozen=True, slots=True)
class ActualBudgetEntry:
    """The actual budget consumed per endpoint per interval."""

    alias: str
    interval_id: str
    logical_rpc_calls: int
    http_requests: int
    response_bytes: int
    normalized_rows: int
    provider_units: int | None
    elapsed_ms: int
    parquet_bytes: int


@dataclass(frozen=True, slots=True)
class BudgetDeviation:
    """A single deviation from the preflight plan.

    Each deviation is a distinct row in the ledger; aggregate counters
    are derived from these rows so warning counts cannot disappear
    inside aggregates.
    """

    alias: str
    deviation_kind: str
    detail: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Capability snapshot (per-endpoint)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityEntry:
    """One endpoint's preflight capability snapshot row."""

    alias: str
    chain_id: int
    finality_tags: tuple[str, ...]
    archive_state_depth_blocks: int | None
    accepted_log_range_blocks: int | None
    latency_ms: int | None
    probe_block_number: int | None
    probe_block_hash: str | None
    max_blocks_per_get_logs: int | None
    observed_response_size_bytes: int | None
    call_budget_allocated: int | None
    remaining_budget_source: str


# ---------------------------------------------------------------------------
# Provider-to-interval provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderProvenanceEntry:
    """One row of the provider-to-interval provenance ledger."""

    alias: str
    failover_from: str | None
    retry_count: int
    retry_reason: str | None
    request_from_block: int
    request_to_block: int
    response_bytes: int
    durable_commit_checksum: str


# ---------------------------------------------------------------------------
# Response / storage volume
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VolumeEntry:
    alias: str
    response_bytes: int
    normalized_rows: int
    parquet_bytes: int
    provider_units: int | None
    elapsed_ms: int


# ---------------------------------------------------------------------------
# Known-common operator / upstream infrastructure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SharedInfrastructureEvidence:
    """Recorded evidence of common operator / upstream infrastructure
    between endpoints A and B.

    Each entry is a single piece of evidence. The full list is
    surfaced in the report so downstream reviewers can audit the
    infrastructure-correlation state.
    """

    kind: str
    description: str
    severity: str = "unknown"


# ---------------------------------------------------------------------------
# The QualityReport value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualityReport:
    """The T034 data-quality and completeness report.

    The verdict is :attr:`complete`. The report is a value object;
    the construction helpers (:func:`empty_report`,
    :meth:`with_finding`, ...) build it incrementally so the runner
    / reorg handler / storage reader do not need to know the report's
    final shape.
    """

    chain_id: int
    contract_address: str
    pool_id: PoolId
    coverage_from_block: int
    coverage_to_block: int
    pool_init_block: int
    topology: TopologyKind
    capability_snapshot: tuple[CapabilityEntry, ...]
    preflight_budget: tuple[PreflightBudgetEntry, ...]
    actual_budget: tuple[ActualBudgetEntry, ...]
    budget_deviations: tuple[BudgetDeviation, ...]
    provider_provenance: tuple[ProviderProvenanceEntry, ...]
    response_volume: tuple[VolumeEntry, ...]
    findings: tuple[QualityFinding, ...]
    required_samples: RequiredSamples
    infrastructure_state: InfrastructureCorrelationState
    shared_infrastructure_evidence: tuple[SharedInfrastructureEvidence, ...]
    manifest_checksum: str
    qualified_checkpoint_ref: str | None
    complete: bool
    qualification_blockers: tuple[str, ...]
    generated_at: str

    def reason_code_counts(self) -> dict[str, int]:
        """Return the count of findings per reason code.

        The aggregate counters are derived from the findings
        themselves; warning counts cannot disappear in aggregates.
        """
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.reason_code] = counts.get(finding.reason_code, 0) + 1
        return counts

    def findings_by_category(self) -> dict[str, tuple[QualityFinding, ...]]:
        """Group findings by category (preserving order)."""
        grouped: dict[str, list[QualityFinding]] = {}
        for finding in self.findings:
            grouped.setdefault(finding.category, []).append(finding)
        return {key: tuple(value) for key, value in grouped.items()}

    def highest_severity_finding_per_category(self) -> dict[str, QualityFinding]:
        """Return the highest-severity finding per category."""
        out: dict[str, QualityFinding] = {}
        for finding in self.findings:
            existing = out.get(finding.category)
            if existing is None or finding.highest_severity > existing.highest_severity:
                out[finding.category] = finding
        return out

    def to_machine_dict(self) -> dict[str, Any]:
        """Serialise the report to a machine-readable dict.

        The dict mirrors every section the T034 contract requires;
        downstream code reads the dict to decide whether to load the
        dataset.
        """
        return {
            "schema": "robinhood_lp.quality.v1",
            "chain_id": self.chain_id,
            "contract_address": self.contract_address,
            "pool_id": self.pool_id.to_hex(),
            "coverage_from_block": self.coverage_from_block,
            "coverage_to_block": self.coverage_to_block,
            "pool_init_block": self.pool_init_block,
            "topology": self.topology.value,
            "complete": bool(self.complete),
            "manifest_checksum": self.manifest_checksum,
            "qualified_checkpoint_ref": self.qualified_checkpoint_ref,
            "qualification_blockers": list(self.qualification_blockers),
            "infrastructure_state": self.infrastructure_state.value,
            "agreement_phrasing": agreement_phrasing_for_state(self.infrastructure_state),
            "correlated_failure_residual_risk": correlated_failure_is_residual_risk(
                self.infrastructure_state
            ),
            "shared_infrastructure_evidence": [
                {"kind": e.kind, "description": e.description, "severity": e.severity}
                for e in self.shared_infrastructure_evidence
            ],
            "generated_at": self.generated_at,
            "capability_snapshot": [
                {
                    "alias": c.alias,
                    "chain_id": c.chain_id,
                    "finality_tags": list(c.finality_tags),
                    "archive_state_depth_blocks": c.archive_state_depth_blocks,
                    "accepted_log_range_blocks": c.accepted_log_range_blocks,
                    "latency_ms": c.latency_ms,
                    "probe_block_number": c.probe_block_number,
                    "probe_block_hash": c.probe_block_hash,
                    "max_blocks_per_get_logs": c.max_blocks_per_get_logs,
                    "observed_response_size_bytes": c.observed_response_size_bytes,
                    "call_budget_allocated": c.call_budget_allocated,
                    "remaining_budget_source": c.remaining_budget_source,
                }
                for c in self.capability_snapshot
            ],
            "preflight_budget": [
                {
                    "alias": p.alias,
                    "role": p.role,
                    "remaining_calls": p.remaining_calls,
                    "remaining_compute_units": p.remaining_compute_units,
                    "remaining_seconds": p.remaining_seconds,
                    "remaining_response_quota": p.remaining_response_quota,
                    "remaining_budget_source": p.remaining_budget_source,
                }
                for p in self.preflight_budget
            ],
            "actual_budget": [
                {
                    "alias": a.alias,
                    "interval_id": a.interval_id,
                    "logical_rpc_calls": a.logical_rpc_calls,
                    "http_requests": a.http_requests,
                    "response_bytes": a.response_bytes,
                    "normalized_rows": a.normalized_rows,
                    "provider_units": a.provider_units,
                    "elapsed_ms": a.elapsed_ms,
                    "parquet_bytes": a.parquet_bytes,
                }
                for a in self.actual_budget
            ],
            "budget_deviations": [
                {
                    "alias": d.alias,
                    "deviation_kind": d.deviation_kind,
                    "detail": dict(d.detail),
                }
                for d in self.budget_deviations
            ],
            "provider_provenance": [
                {
                    "alias": p.alias,
                    "failover_from": p.failover_from,
                    "retry_count": p.retry_count,
                    "retry_reason": p.retry_reason,
                    "request_from_block": p.request_from_block,
                    "request_to_block": p.request_to_block,
                    "response_bytes": p.response_bytes,
                    "durable_commit_checksum": p.durable_commit_checksum,
                }
                for p in self.provider_provenance
            ],
            "response_volume": [
                {
                    "alias": v.alias,
                    "response_bytes": v.response_bytes,
                    "normalized_rows": v.normalized_rows,
                    "parquet_bytes": v.parquet_bytes,
                    "provider_units": v.provider_units,
                    "elapsed_ms": v.elapsed_ms,
                }
                for v in self.response_volume
            ],
            "findings": [
                {
                    "reason_code": f.reason_code,
                    "category": f.category,
                    "detail": dict(f.detail),
                    "highest_severity": f.highest_severity,
                }
                for f in self.findings
            ],
            "required_samples": {
                "per_run": {
                    "pool_id": self.required_samples.per_run.pool_id.to_hex(),
                    "fixed_block_number": self.required_samples.per_run.fixed_block_number,
                    "fixed_block_hash": self.required_samples.per_run.fixed_block_hash,
                    "chain_id": self.required_samples.per_run.chain_id,
                    "genesis_hash": self.required_samples.per_run.genesis_hash,
                    "pool_manager_code_hash": self.required_samples.per_run.pool_manager_code_hash,
                    "state_view_code_hash": self.required_samples.per_run.state_view_code_hash,
                    "selection_inputs": dict(self.required_samples.per_run.selection_inputs),
                },
                "per_partition": [
                    {
                        "pool_id": s.pool_id.to_hex(),
                        "partition_id": s.partition_id,
                        "start_block": s.start_block,
                        "end_block": s.end_block,
                        "opening_window": list(s.opening_window),
                        "trailing_window": list(s.trailing_window),
                        "selection_inputs": dict(s.selection_inputs),
                    }
                    for s in self.required_samples.per_partition
                ],
                "per_event_type": [
                    {
                        "pool_id": s.pool_id.to_hex(),
                        "event_name": s.event_name,
                        "smallest_event_key": {
                            "chain_id": s.smallest_event_key.chain_id.value,
                            "block_hash": s.smallest_event_key.block_ref().to_hash_hex(),
                            "tx_hash": s.smallest_event_key.transaction_ref().to_hash_hex(),
                            "log_index": s.smallest_event_key.log_index,
                        },
                        "window": list(s.window),
                        "selection_inputs": dict(s.selection_inputs),
                    }
                    for s in self.required_samples.per_event_type
                ],
                "per_failover": [
                    {
                        "pool_id": s.pool_id.to_hex(),
                        "failover_at_block": s.failover_at_block,
                        "pre_window": list(s.pre_window),
                        "post_window": list(s.post_window),
                        "selection_inputs": dict(s.selection_inputs),
                    }
                    for s in self.required_samples.per_failover
                ],
                "result_bearing_windows": [
                    list(w) for w in self.required_samples.result_bearing_windows
                ],
            },
        }

    def to_human_summary(self) -> str:
        """Render a short human-readable summary.

        The summary surfaces the qualification verdict, the
        chain / pool identity, the coverage range, the checkpoint
        reference, the manifest checksum, the infrastructure state
        with the explicit residual-risk callout, and the cold-start /
        warm-run basis. Downstream operators can read it directly; the
        machine-readable dict is for downstream code.
        """
        verdict = "PASS" if self.complete else "FAIL"
        lines: list[str] = []
        lines.append(f"Qualification: {verdict}")
        lines.append(
            f"Chain: {self.chain_id} | Pool: {self.pool_id.to_hex()} | "
            f"Coverage: [{self.coverage_from_block}, {self.coverage_to_block}]"
        )
        lines.append(f"Topology: {self.topology.value}")
        if self.pool_init_block is not None:
            lines.append(f"Pool Initialize Block: {self.pool_init_block}")
        lines.append(f"Manifest Checksum: {self.manifest_checksum}")
        if self.qualified_checkpoint_ref:
            lines.append(f"Qualified Checkpoint: {self.qualified_checkpoint_ref}")
        # Surface every reason code raised, grouped by category, with the
        # count and the highest-severity sample per category.
        grouped = self.findings_by_category()
        for category, findings in sorted(grouped.items()):
            codes = sorted({f.reason_code for f in findings})
            counts = self.reason_code_counts()
            count_summary = ", ".join(f"{code}={counts.get(code, 0)}" for code in codes)
            lines.append(f"Findings[{category}]: {count_summary}")
        # Infrastructure-correlation state with the explicit
        # residual-risk callout when the state is not
        # evidenced_independent.
        phrasing = agreement_phrasing_for_state(self.infrastructure_state)
        lines.append(f"Infrastructure Correlation: {self.infrastructure_state.value}")
        lines.append(f"A+B Wording: {phrasing}")
        if correlated_failure_is_residual_risk(self.infrastructure_state):
            lines.append(
                "RESIDUAL RISK: correlated failure between endpoints A and B "
                f"is not excluded; current state is {self.infrastructure_state.value}."
            )
        # Cold-start / warm-run basis.
        if self.topology == TopologyKind.COLD_START:
            lines.append(
                f"Cold start: coverage begins at pool's Initialize block "
                f"({self.pool_init_block}); verified."
            )
        elif self.topology == TopologyKind.WARM_INCREMENTAL:
            lines.append(
                f"Warm run: qualified checkpoint ({self.qualified_checkpoint_ref}) "
                "verified; complete suffix appended."
            )
        elif self.topology == TopologyKind.BACKTEST_WINDOW_ONLY:
            lines.append(
                "Backtest-window-only download: no Initialize or qualified "
                "checkpoint within scope; coverage incomplete."
            )
        # List the blockers when qualification failed.
        if not self.complete and self.qualification_blockers:
            lines.append("Qualification Blockers:")
            for blocker in self.qualification_blockers:
                lines.append(f"  - {blocker}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Construction helpers
# ---------------------------------------------------------------------------


def _placeholder_per_run() -> Any:
    """A placeholder per-run sample used to bootstrap an empty report.

    The placeholder exists so :func:`empty_report` can construct an
    empty :class:`QualityReport` before the deterministic selection has
    produced the real per-run row. Callers must replace it via
    :func:`empty_report` with a proper :class:`PerRunSample` whose
    selection inputs come from deterministic sources.
    """
    from robinhood_lp.quality.sample_selection import PerRunSample

    return PerRunSample(
        pool_id=PoolId(0),
        fixed_block_number=0,
        fixed_block_hash="",
        chain_id=0,
        genesis_hash="",
        pool_manager_code_hash="",
        state_view_code_hash="",
        selection_inputs={},
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _compute_manifest_checksum(payload: Mapping[str, Any]) -> str:
    """Compute the SHA-256 of a JSON-serialised manifest payload.

    The checksum is included in the report and is reproducible from
    the durable manifest store. The function is deterministic — it
    never reads wall clock or random sources.
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return "0x" + hashlib.sha256(blob).hexdigest()


def empty_report(
    *,
    chain_id: int,
    contract_address: str,
    pool_id: PoolId,
    pool_init_block: int,
    coverage_from_block: int,
    coverage_to_block: int,
    topology: TopologyKind,
    required_samples: RequiredSamples,
    manifest_checksum: str,
    qualified_checkpoint_ref: str | None = None,
    infrastructure_state: InfrastructureCorrelationState = (
        DEFAULT_INFRASTRUCTURE_CORRELATION_STATE
    ),
) -> QualityReport:
    """Return an empty ``QualityReport`` for the given dataset.

    The caller fills in the capability snapshot, the budget ledger,
    the provider provenance, the response volume, and the findings
    via ``with_*`` helpers. ``complete`` starts as ``True`` and is
    downgraded to ``False`` as soon as a blocking finding is added or
    the budget is exhausted.
    """
    if coverage_from_block < 0 or coverage_to_block < coverage_from_block:
        raise ValueError(
            f"empty_report: invalid coverage range [{coverage_from_block}, {coverage_to_block}]"
        )
    if not isinstance(topology, TopologyKind):
        raise TypeError(
            f"empty_report: topology must be TopologyKind, got {type(topology).__name__}"
        )
    return QualityReport(
        chain_id=chain_id,
        contract_address=contract_address,
        pool_id=pool_id,
        coverage_from_block=coverage_from_block,
        coverage_to_block=coverage_to_block,
        pool_init_block=pool_init_block,
        topology=topology,
        capability_snapshot=(),
        preflight_budget=(),
        actual_budget=(),
        budget_deviations=(),
        provider_provenance=(),
        response_volume=(),
        findings=(),
        required_samples=required_samples,
        infrastructure_state=infrastructure_state,
        shared_infrastructure_evidence=(),
        manifest_checksum=manifest_checksum,
        qualified_checkpoint_ref=qualified_checkpoint_ref,
        complete=True,
        qualification_blockers=(),
        generated_at=_now_iso(),
    )


def _merge_findings(
    existing: Iterable[QualityFinding],
    additions: Iterable[QualityFinding],
) -> tuple[QualityFinding, ...]:
    """Append ``additions`` to ``existing`` while preserving every
    occurrence.

    Reason codes are distinct surfaces; the report never collapses
    multiple distinct failure modes into a single counter. The
    append-only merge means every occurrence of the same reason
    code counts in the aggregate.
    """
    return tuple(list(existing) + list(additions))


def with_finding(report: QualityReport, finding: QualityFinding) -> QualityReport:
    """Return a copy of ``report`` with ``finding`` appended.

    When ``finding`` is qualification-fatal or its reason code forces
    ``complete=False``, the new ``complete`` value is ``False`` and
    ``qualification_blockers`` records the blocker. Warning counts do
    not disappear in aggregates: every distinct finding is appended,
    and ``reason_code_counts`` aggregates them transparently.
    """
    findings = _merge_findings(report.findings, (finding,))
    blockers = list(report.qualification_blockers)
    complete = report.complete
    if not complete:
        pass  # already false
    elif (
        is_qualification_fatal_reason(finding.reason_code)
        or finding.reason_code == REASON_BUDGET_EXHAUSTED
        or finding.reason_code == REASON_CHECKPOINT_MISMATCH
        or finding.reason_code == REASON_CROSS_ENDPOINT_SAMPLE_MISSING
        or finding.reason_code == REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE
    ):
        complete = False
        if finding.reason_code not in blockers:
            blockers.append(finding.reason_code)
    return _replace(
        report, findings=findings, complete=complete, qualification_blockers=tuple(blockers)
    )


def with_capability_snapshot(
    report: QualityReport,
    entries: Iterable[CapabilityEntry],
) -> QualityReport:
    return _replace(report, capability_snapshot=tuple(entries))


def with_preflight_budget(
    report: QualityReport,
    entries: Iterable[PreflightBudgetEntry],
) -> QualityReport:
    return _replace(report, preflight_budget=tuple(entries))


def with_actual_budget(
    report: QualityReport,
    entries: Iterable[ActualBudgetEntry],
) -> QualityReport:
    return _replace(report, actual_budget=tuple(entries))


def with_budget_deviation(
    report: QualityReport,
    deviation: BudgetDeviation,
) -> QualityReport:
    """Return a copy of ``report`` with ``deviation`` appended.

    Each deviation is a distinct row in the ledger; aggregate
    counters are derived from these rows so warning counts cannot
    disappear in aggregates.
    """
    return _replace(report, budget_deviations=report.budget_deviations + (deviation,))


def with_provider_provenance(
    report: QualityReport,
    entries: Iterable[ProviderProvenanceEntry],
) -> QualityReport:
    return _replace(report, provider_provenance=tuple(entries))


def with_response_volume(
    report: QualityReport,
    entries: Iterable[VolumeEntry],
) -> QualityReport:
    return _replace(report, response_volume=tuple(entries))


def with_infrastructure_state(
    report: QualityReport,
    state: InfrastructureCorrelationState,
) -> QualityReport:
    """Return a copy of ``report`` with the infrastructure state set.

    The state must not be left blank; the caller chooses the enum
    value. Transitioning to ``evidenced_independent`` requires
    separate auditable evidence; the ``QualityReport`` itself does
    not enforce the evidence requirement because that is a process
    gate owned by Phase 8 / operator procedure.
    """
    if not isinstance(state, InfrastructureCorrelationState):
        raise TypeError(
            f"with_infrastructure_state: state must be "
            f"InfrastructureCorrelationState, got {type(state).__name__}"
        )
    return _replace(report, infrastructure_state=state)


def with_shared_infrastructure_evidence(
    report: QualityReport,
    evidence: Iterable[SharedInfrastructureEvidence],
) -> QualityReport:
    return _replace(report, shared_infrastructure_evidence=tuple(evidence))


def with_qualification_blocker(report: QualityReport, blocker: str) -> QualityReport:
    """Return a copy of ``report`` with ``blocker`` appended and
    ``complete`` set to ``False``.

    Use this for blockers that do not have a corresponding finding
    (for example, a top-level checkpoint mismatch surfaced from the
    warm-run path).
    """
    blockers = list(report.qualification_blockers)
    if blocker not in blockers:
        blockers.append(blocker)
    return _replace(report, qualification_blockers=tuple(blockers), complete=False)


def with_complete(report: QualityReport, complete: bool) -> QualityReport:
    """Return a copy of ``report`` with ``complete`` explicitly set.

    The verifier normally relies on ``with_finding`` /
    ``with_qualification_blocker`` to downgrade ``complete``. This
    helper is for the rare case where the verifier wants to assert
    the value (for example, after replaying a stored manifest).
    """
    return _replace(report, complete=bool(complete))


def _replace(report: QualityReport, **changes: Any) -> QualityReport:
    """Return a copy of ``report`` with the named fields replaced.

    The dataclass is frozen; ``dataclasses.replace`` would do this,
    but a single explicit helper avoids polluting call sites with the
    long import.
    """
    import dataclasses

    return dataclasses.replace(report, **changes)


__all__ = [
    "ActualBudgetEntry",
    "BudgetDeviation",
    "CapabilityEntry",
    "PreflightBudgetEntry",
    "ProviderProvenanceEntry",
    "QualityFinding",
    "QualityReport",
    "SharedInfrastructureEvidence",
    "TopologyKind",
    "VolumeEntry",
    "_compute_manifest_checksum",
    "empty_report",
    "with_actual_budget",
    "with_budget_deviation",
    "with_capability_snapshot",
    "with_complete",
    "with_finding",
    "with_infrastructure_state",
    "with_preflight_budget",
    "with_provider_provenance",
    "with_qualification_blocker",
    "with_response_volume",
    "with_shared_infrastructure_evidence",
]
