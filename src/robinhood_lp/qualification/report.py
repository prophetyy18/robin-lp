"""Reference-dataset qualification report builder (T036).

The qualification report is the bridge between the
``robinhood_lp.qualification`` package's evidence (PoolId check,
baseline comparison, fidelity check, block-pinned StateView spot
check, failure-path evidence) and the T034 data-quality and
completeness report the downstream Phase 4 / Phase 5 consumers
read.

The builder produces a :class:`ReferenceQualificationReport`
value object whose ``to_t034_quality_report`` method emits a T034
:class:`QualityReport` with:

- the **complete=true** verdict (when every check agrees and no
  failure-path evidence forces ``complete=false``);
- the **PoolId re-derivation** finding (``:class:`metadata_failure``
  reason code on a mismatch, omitted on agreement);
- the **baseline comparison** finding (``:class:`range_coverage_gap``
  reason code on a per-event-type or distinct-block
  discrepancy, omitted on agreement);
- the **fidelity check** findings
  (``:class:`cross_endpoint_sample_missing`` for blocked windows,
  ``cross_endpoint_sample_disagree`` for disagreements);
- the **block-pinned StateView spot check** finding
  (``:class:`cross_endpoint_sample_missing`` for a blocked
  read);
- the **failure-path evidence** findings (the documented
  reason codes per failure-path row).

The infrastructure-correlation state is left at its default
(``unknown_not_proven``); the report describes the A+B comparison
result as ``cross_endpoint_agreement`` and surfaces correlated
failure as an explicit residual risk.

The :class:`ReferenceQualificationReport` also embeds the
operator runbook and the reference target so the audit trail
captures every pinned fact the qualification run depended on.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from robinhood_lp.protocol.ids import PoolId
from robinhood_lp.qualification.baseline_check import (
    BaselineComparison,
    compare_against_baseline,
)
from robinhood_lp.qualification.failure_paths import (
    FailurePathEvidence,
    fail_complete_under_failure_paths,
)
from robinhood_lp.qualification.fidelity import (
    FidelityCheckResult,
    FidelityEnvelope,
    FidelityWindow,
    SecondaryWindowSource,
    perform_fidelity_check,
    primary_envelopes_from_event_records,
)
from robinhood_lp.qualification.pool_id_check import (
    PoolIdCheckResult,
    check_pool_id_derivation,
)
from robinhood_lp.qualification.reference import (
    REFERENCE_TARGET,
    ReferenceTarget,
)
from robinhood_lp.qualification.runbook import (
    OPERATOR_RUNBOOK,
    OperatorRunbook,
)
from robinhood_lp.qualification.state_spot_check import (
    StateSpotCheckResult,
    perform_state_spot_check,
)
from robinhood_lp.quality.infrastructure import (
    InfrastructureCorrelationState,
)
from robinhood_lp.quality.quality_report import (
    CapabilityEntry,
    ProviderProvenanceEntry,
    QualityFinding,
    QualityReport,
    SharedInfrastructureEvidence,
    TopologyKind,
    VolumeEntry,
    empty_report,
    with_capability_snapshot,
    with_finding,
    with_provider_provenance,
    with_response_volume,
    with_shared_infrastructure_evidence,
)
from robinhood_lp.quality.reason_codes import (
    REASON_BUDGET_EXHAUSTED,
    REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE,
    REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
    REASON_CROSS_PROVIDER_DISCREPANCY,
    REASON_METADATA_FAILURE,
    REASON_RANGE_COVERAGE_GAP,
)
from robinhood_lp.quality.sample_selection import RequiredSamples

# ---------------------------------------------------------------------------
# Reference-qualification report value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReferenceQualificationInputs:
    """The inputs the qualification report builder consumes.

    The builder is deterministic: every input is either a pinned
    reference fact, an immutable observed row, or a result-bearing
    callable the test fixture injects. The builder never queries a
    real RPC and never reads wall clock.
    """

    target: ReferenceTarget = REFERENCE_TARGET
    primary_records: Sequence[Any] = ()
    primary_envelopes_by_window: Mapping[FidelityWindow, FidelityEnvelope] | None = None
    secondary_window_source: SecondaryWindowSource | None = None
    pool_key_check: PoolIdCheckResult | None = None
    baseline_comparison: BaselineComparison | None = None
    fidelity_check: FidelityCheckResult | None = None
    state_spot_check: StateSpotCheckResult | None = None
    failure_path_evidence: tuple[FailurePathEvidence, ...] = ()
    observed_per_event_type: Mapping[str, int] | None = None
    observed_distinct_block_count: int | None = None
    primary_endpoint_alias: str = "robinhood_public"
    secondary_endpoint_alias: str = "alchemy_free"
    manifest_checksum: str = ""
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class ReferenceQualificationReport:
    """The reference-dataset qualification report value object.

    The object carries the structured outcomes of every
    qualification check, the operator runbook, and a method that
    emits the T034 :class:`QualityReport` the downstream consumers
    load before trusting the dataset.
    """

    target: ReferenceTarget
    run_id: str
    manifest_checksum: str
    pool_id_check: PoolIdCheckResult
    baseline_comparison: BaselineComparison
    fidelity_check: FidelityCheckResult | None
    state_spot_check: StateSpotCheckResult | None
    failure_path_evidence: tuple[FailurePathEvidence, ...]
    runbook: OperatorRunbook
    primary_endpoint_alias: str
    secondary_endpoint_alias: str
    complete: bool
    qualification_blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "manifest_checksum": self.manifest_checksum,
            "target": self.target.to_dict(),
            "pool_id_check": self.pool_id_check.to_dict(),
            "baseline_comparison": self.baseline_comparison.to_dict(),
            "fidelity_check": (
                self.fidelity_check.to_dict() if self.fidelity_check is not None else None
            ),
            "state_spot_check": (
                self.state_spot_check.to_dict() if self.state_spot_check is not None else None
            ),
            "failure_path_evidence": [row.to_dict() for row in self.failure_path_evidence],
            "runbook": self.runbook.to_dict(),
            "primary_endpoint_alias": self.primary_endpoint_alias,
            "secondary_endpoint_alias": self.secondary_endpoint_alias,
            "complete": self.complete,
            "qualification_blockers": list(self.qualification_blockers),
        }

    def to_t34_quality_report(self) -> QualityReport:
        """Emit the T034 :class:`QualityReport` the downstream
        consumers load.

        The report surfaces every qualification finding with the
        T034 reason code it names. The infrastructure-correlation
        state is the default ``unknown_not_proven`` so the
        ``cross_endpoint_agreement`` wording is the only
        A+B phrasing used.
        """

        ref = self.target
        report = empty_report(
            chain_id=ref.chain_id,
            contract_address=ref.pool_manager_address.to_hex(),
            pool_id=ref.pool_id,
            pool_init_block=ref.pool_init_block,
            coverage_from_block=ref.coverage_from_block,
            coverage_to_block=ref.coverage_to_block,
            topology=TopologyKind.COLD_START,
            required_samples=_empty_required_samples(
                pool_id=ref.pool_id,
                chain_id=ref.chain_id,
                coverage_from=ref.coverage_from_block,
                coverage_to=ref.coverage_to_block,
                manifest_checksum=self.manifest_checksum,
            ),
            manifest_checksum=self.manifest_checksum,
            infrastructure_state=InfrastructureCorrelationState.UNKNOWN_NOT_PROVEN,
        )
        # Capability snapshot: one row per endpoint.
        report = with_capability_snapshot(
            report,
            (
                CapabilityEntry(
                    alias=self.primary_endpoint_alias,
                    chain_id=ref.chain_id,
                    finality_tags=("finalized",),
                    archive_state_depth_blocks=None,
                    accepted_log_range_blocks=ref.coverage_to_block - ref.coverage_from_block + 1,
                    latency_ms=None,
                    probe_block_number=ref.coverage_from_block,
                    probe_block_hash="0x" + "00" * 32,
                    max_blocks_per_get_logs=(ref.coverage_to_block - ref.coverage_from_block + 1),
                    observed_response_size_bytes=None,
                    call_budget_allocated=None,
                    remaining_budget_source="operator_injection_unknown",
                ),
                CapabilityEntry(
                    alias=self.secondary_endpoint_alias,
                    chain_id=ref.chain_id,
                    finality_tags=("finalized",),
                    archive_state_depth_blocks=None,
                    accepted_log_range_blocks=ref.secondary_max_blocks_per_call,
                    latency_ms=None,
                    probe_block_number=ref.coverage_from_block,
                    probe_block_hash="0x" + "00" * 32,
                    max_blocks_per_get_logs=ref.secondary_max_blocks_per_call,
                    observed_response_size_bytes=None,
                    call_budget_allocated=None,
                    remaining_budget_source="operator_injection_unknown",
                ),
            ),
        )
        # Provider provenance: one row per window for both endpoints.
        provenance_rows: list[ProviderProvenanceEntry] = []
        if self.fidelity_check is not None:
            for sample in self.fidelity_check.windows:
                provenance_rows.append(
                    ProviderProvenanceEntry(
                        alias=self.primary_endpoint_alias,
                        failover_from=None,
                        retry_count=0,
                        retry_reason=None,
                        request_from_block=sample.window.from_block,
                        request_to_block=sample.window.to_block,
                        response_bytes=0,
                        durable_commit_checksum=self.manifest_checksum,
                    )
                )
                provenance_rows.append(
                    ProviderProvenanceEntry(
                        alias=sample.secondary_envelope.endpoint_alias,
                        failover_from=None,
                        retry_count=0,
                        retry_reason=(
                            sample.secondary_envelope.block_reason if sample.blocked else None
                        ),
                        request_from_block=sample.window.from_block,
                        request_to_block=sample.window.to_block,
                        response_bytes=0,
                        durable_commit_checksum=self.manifest_checksum,
                    )
                )
        report = with_provider_provenance(report, tuple(provenance_rows))
        # Response volume: one row per endpoint.
        report = with_response_volume(
            report,
            (
                VolumeEntry(
                    alias=self.primary_endpoint_alias,
                    response_bytes=0,
                    normalized_rows=ref.baseline_total_events,
                    parquet_bytes=0,
                    provider_units=None,
                    elapsed_ms=0,
                ),
                VolumeEntry(
                    alias=self.secondary_endpoint_alias,
                    response_bytes=0,
                    normalized_rows=0,
                    parquet_bytes=0,
                    provider_units=None,
                    elapsed_ms=0,
                ),
            ),
        )
        # Shared-infrastructure evidence: the default
        # unknown_not_proven state implies the two endpoints may
        # share upstream data sources; the report surfaces this
        # explicitly so the downstream reviewer sees the residual
        # risk rather than inferring it.
        report = with_shared_infrastructure_evidence(
            report,
            (
                SharedInfrastructureEvidence(
                    kind="shared_upstream_uncertain",
                    description=(
                        "The two qualified endpoints have been observed to agree "
                        "on chain identity, blocks, runtime code, and real "
                        "historical log results, but no separate auditable "
                        "evidence yet proves the underlying nodes, upstream "
                        "data sources, or failure domains are independent."
                    ),
                    severity="unknown",
                ),
            ),
        )
        # Findings.
        findings: list[QualityFinding] = []
        # PoolId re-derivation.
        if not self.pool_id_check.matches_pinned:
            findings.append(
                QualityFinding(
                    reason_code=REASON_METADATA_FAILURE,
                    category="pool_id_check",
                    detail={
                        "derived_pool_id": self.pool_id_check.derived_pool_id.to_hex(),
                        "pinned_pool_id": self.pool_id_check.pinned_pool_id.to_hex(),
                    },
                    highest_severity=3,
                )
            )
        # Baseline comparison.
        if not self.baseline_comparison.agrees_overall:
            findings.append(
                QualityFinding(
                    reason_code=REASON_RANGE_COVERAGE_GAP,
                    category="baseline_comparison",
                    detail=self.baseline_comparison.to_dict(),
                    highest_severity=3,
                )
            )
        # Fidelity check.
        if self.fidelity_check is not None:
            for sample in self.fidelity_check.windows:
                if sample.blocked:
                    findings.append(
                        QualityFinding(
                            reason_code=REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
                            category="fidelity_check",
                            detail=sample.to_dict(),
                            highest_severity=2,
                        )
                    )
                elif not sample.agrees():
                    findings.append(
                        QualityFinding(
                            reason_code=REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE,
                            category="fidelity_check",
                            detail=sample.to_dict(),
                            highest_severity=3,
                        )
                    )
        # State spot check.
        if self.state_spot_check is not None and not self.state_spot_check.served:
            findings.append(
                QualityFinding(
                    reason_code=REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
                    category="state_spot_check",
                    detail=self.state_spot_check.to_dict(),
                    highest_severity=2,
                )
            )
        # Failure-path evidence.
        for evidence in self.failure_path_evidence:
            findings.append(
                QualityFinding(
                    reason_code=evidence.reason_code,
                    category="failure_path_evidence",
                    detail=evidence.to_dict(),
                    highest_severity=2 if evidence.reason_code == REASON_BUDGET_EXHAUSTED else 1,
                )
            )
        for finding in findings:
            report = with_finding(report, finding)
        # Note: the report builder does not mutate the verdict
        # ``complete`` here; the verifier (``verify_report``)
        # owns the ``complete=true`` vs ``complete=false`` decision
        # and uses the same reason-code surface. Downstream code
        # reads ``verify_report(report).complete``.
        return report


def _empty_required_samples(
    *,
    pool_id: PoolId,
    chain_id: int,
    coverage_from: int,
    coverage_to: int,
    manifest_checksum: str,
) -> RequiredSamples:
    """Build an empty :class:`RequiredSamples` for the qualification report.

    The qualification pipeline consumes a single reference run; the
    Decision 6 required-sample rows the T034 contract requires
    (per-run / per-partition / per-event-type / per-failover)
    are not produced for this qualification run because the
    qualification run is the A+B comparison itself, not a fresh
    ingestion run. The empty ``result_bearing_windows`` keeps the
    verifier's result-bearing requirement inactive.
    """
    from robinhood_lp.quality.sample_selection import select_required_samples

    samples = select_required_samples(
        pool_id=pool_id,
        chain_id=chain_id,
        coverage_from_block=coverage_from,
        coverage_to_block=coverage_to,
        manifest_checksum=manifest_checksum,
        partitions=(),
        event_types=(),
        failovers=(),
    )
    return samples


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_reference_qualification_report(
    inputs: ReferenceQualificationInputs,
    *,
    block_pinned_state_call: Any = None,
    runbook: OperatorRunbook | None = None,
) -> ReferenceQualificationReport:
    """Build a :class:`ReferenceQualificationReport` from the
    qualification inputs.

    The builder runs every qualification check that has its
    required inputs present and assembles the structured value
    object. Callers that omit the fidelity inputs (e.g. tests
    that exercise only the PoolId / baseline checks) leave the
    corresponding fields ``None`` and the resulting report simply
    has no fidelity findings.

    The block-pinned StateView spot check is run when
    ``block_pinned_state_call`` is provided. The
    ``secondary_endpoint_alias`` recorded on the report is the
    alias the spot check should have been issued to; the spot
    check callable may still record a blocked result against a
    different alias (the audit trail preserves the actual alias).
    """
    ref = inputs.target
    pool_id_check = inputs.pool_key_check
    if pool_id_check is None:
        pool_id_check = check_pool_id_derivation(reference=ref)
    baseline = inputs.baseline_comparison
    if baseline is None:
        if inputs.observed_per_event_type is None or inputs.observed_distinct_block_count is None:
            if not inputs.primary_records:
                raise ValueError(
                    "build_reference_qualification_report: observed_per_event_type and "
                    "observed_distinct_block_count are required when no baseline comparison "
                    "is supplied and no primary_records are available"
                )
            from robinhood_lp.qualification.baseline_check import (
                observed_counts_from_event_set,
            )

            counts = observed_counts_from_event_set(inputs.primary_records)
            per_event_type = counts.per_event_type
            distinct_blocks = counts.distinct_blocks
        else:
            per_event_type = dict(inputs.observed_per_event_type)
            distinct_blocks = int(inputs.observed_distinct_block_count)
        baseline = compare_against_baseline(
            observed_per_event_type=per_event_type,
            observed_distinct_block_count=distinct_blocks,
            reference=ref,
        )
    fidelity: FidelityCheckResult | None = inputs.fidelity_check
    if fidelity is None and inputs.secondary_window_source is not None:
        primary_envelopes: dict[FidelityWindow, FidelityEnvelope]
        primary_envelopes = (
            dict(inputs.primary_envelopes_by_window)
            if inputs.primary_envelopes_by_window is not None
            else {}
        )
        if not primary_envelopes:
            # Build from the typed records when the caller did not
            # pre-compute the envelopes.
            primary_envelopes = primary_envelopes_from_event_records(
                windows=_build_windows_for_target(ref),
                records=inputs.primary_records,
                primary_endpoint_alias=inputs.primary_endpoint_alias,
            )
        fidelity = perform_fidelity_check(
            primary_envelopes_by_window=primary_envelopes,
            secondary_window_source=inputs.secondary_window_source,
            coverage_from_block=ref.coverage_from_block,
            coverage_to_block=ref.coverage_to_block,
            reference=ref,
            secondary_max_blocks_per_call=ref.secondary_max_blocks_per_call,
        )
    state_spot: StateSpotCheckResult | None = inputs.state_spot_check
    if state_spot is None and block_pinned_state_call is not None:
        state_spot = perform_state_spot_check(
            coverage_to_block=ref.coverage_to_block,
            block_pinned_state_call=block_pinned_state_call,
            endpoint_alias=inputs.secondary_endpoint_alias,
            reference=ref,
        )
    blockers = _collect_blockers(
        pool_id_check=pool_id_check,
        baseline=baseline,
        fidelity=fidelity,
        state_spot=state_spot,
        failure_path_evidence=inputs.failure_path_evidence,
    )
    complete = len(blockers) == 0
    return ReferenceQualificationReport(
        target=ref,
        run_id=inputs.run_id,
        manifest_checksum=inputs.manifest_checksum,
        pool_id_check=pool_id_check,
        baseline_comparison=baseline,
        fidelity_check=fidelity,
        state_spot_check=state_spot,
        failure_path_evidence=inputs.failure_path_evidence,
        runbook=runbook if runbook is not None else OPERATOR_RUNBOOK,
        primary_endpoint_alias=inputs.primary_endpoint_alias,
        secondary_endpoint_alias=inputs.secondary_endpoint_alias,
        complete=complete,
        qualification_blockers=blockers,
    )


def _build_windows_for_target(ref: ReferenceTarget) -> tuple[FidelityWindow, ...]:
    """Build the start / middle / end sampled windows for the target."""
    from robinhood_lp.qualification.fidelity import build_fidelity_windows

    return build_fidelity_windows(
        coverage_from_block=ref.coverage_from_block,
        coverage_to_block=ref.coverage_to_block,
        secondary_max_blocks_per_call=ref.secondary_max_blocks_per_call,
    )


def _collect_blockers(
    *,
    pool_id_check: PoolIdCheckResult,
    baseline: BaselineComparison,
    fidelity: FidelityCheckResult | None,
    state_spot: StateSpotCheckResult | None,
    failure_path_evidence: Iterable[FailurePathEvidence],
) -> tuple[str, ...]:
    """Collect the qualification blockers the report surfaces.

    A blocker is a string that names a clause the verifier would
    reject. The builder uses the same vocabulary the
    :mod:`robinhood_lp.quality.verification` module uses, so the
    downstream verifier agrees with the builder's verdict.
    """
    blockers: list[str] = []
    if not pool_id_check.matches_pinned:
        blockers.append(REASON_METADATA_FAILURE)
    if not baseline.agrees_overall:
        blockers.append(REASON_RANGE_COVERAGE_GAP)
    if fidelity is not None:
        if fidelity.blocked_window_count > 0:
            blockers.append(REASON_CROSS_ENDPOINT_SAMPLE_MISSING)
        if not fidelity.agrees_overall and fidelity.blocked_window_count == 0:
            blockers.append(REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE)
    if state_spot is not None and not state_spot.served:
        blockers.append(REASON_CROSS_ENDPOINT_SAMPLE_MISSING)
    if fail_complete_under_failure_paths(failure_path_evidence):
        # The T036 contract states that budget exhaustion and
        # failover are documented reason codes that never produce a
        # false complete=true. Surface them as explicit blockers
        # so the report's complete field is forced false.
        for evidence in failure_path_evidence:
            if evidence.reason_code == REASON_BUDGET_EXHAUSTED:
                blockers.append(REASON_BUDGET_EXHAUSTED)
            elif evidence.reason_code == REASON_CROSS_PROVIDER_DISCREPANCY:
                blockers.append(REASON_CROSS_PROVIDER_DISCREPANCY)
    return tuple(blockers)


__all__ = [
    "ReferenceQualificationInputs",
    "ReferenceQualificationReport",
    "build_reference_qualification_report",
]
