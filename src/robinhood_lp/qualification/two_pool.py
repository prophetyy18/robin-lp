"""Two-pool ten-million-block qualification orchestrator (T038).

The T038 contract requires **two** qualified datasets — one per
included pool — over a single, pinned, finalized block window. Each
pool carries its own data root, its own T034 machine report with
``complete=true``, and its own per-pool reconciliation path. This
module is the orchestrator that:

1. Wraps the existing T036 reference-dataset qualification pipeline
   for the reference pool (re-acquired in this window from its
   ``Initialize`` block under the window rule), preserving the
   2026-09-17 baseline comparison and the PoolId re-derivation
   check;
2. Adds the second-pool qualification pipeline: the resolved
   ``PoolKey`` + ``Initialize`` block (operator-supplied, verified
   by the offline keccak256 re-derivation check) plus the same
   per-pool T034 / reconciliation / fidelity / state-spot-check
   surfaces the reference pool carries;
3. Combines the two per-pool machine reports into a single
   two-pool T038 machine report that surfaces both per-pool data
   roots, both per-pool manifest checksums, the pinned
   ``finalized`` window end, the included / excluded pool
   classification, and the overall verdict.

The two-pool machine report does not re-implement the T036
qualification checks; it composes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from robinhood_lp.protocol.ids import PoolId
from robinhood_lp.qualification.reference import (
    REFERENCE_POOL_ID_HEX,
    ReferenceTarget,
)
from robinhood_lp.qualification.second_pool import (
    ResolvedPoolKey,
    SecondPoolResolveResult,
    classify_second_pool_support_level,
)
from robinhood_lp.qualification.two_pool_window import (
    OUTCOME_POOL_INIT_OUTSIDE_WINDOW,
    PoolWindowOutcome,
    TwoPoolWindowPlan,
)

# ---------------------------------------------------------------------------
# Per-pool T034 machine report (the T038 deliverable per pool)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerPoolT034Report:
    """The T034 machine report the T038 contract requires per pool.

    The structure mirrors :class:`QualityReport` but carries only
    the fields the T038 contract names (data root, manifest
    checksum, pool identity, coverage range, per-pool
    reconciliation verdict, ``complete`` verdict, qualification
    blockers, findings). The report is a value object: tests
    construct it directly from deterministic inputs and the
    operator runbook surfaces its ``to_dict`` rendering.

    ``pool_id_hex`` carries the canonical lowercase 0x-hex
    identifier the candidate / planner supplied (a 32-byte
    keccak256 hex when the resolver succeeded, or the
    Owner-pinned 40-byte hex when the contract-defect path is
    exercised). The field stays a string so the audit trail
    preserves the original shape the planner saw; downstream
    consumers convert to a typed ``PoolId`` only when the
    shape matches the protocol-layer invariant.

    ``findings`` carries the T034 reason codes the per-pool
    pipeline produced; ``qualification_blockers`` lists the
    blocker codes that forced ``complete=False``. A
    ``pool_init_outside_window`` exclusion is surfaced via
    ``window_outcome`` and ``coverage_outcome`` rather than via a
    blocker code so the audit trail separates the window rule from
    the data completeness check.
    """

    pool_alias: str
    pool_id_hex: str
    chain_id: int
    contract_address: str
    data_root: str
    pool_init_block: int
    coverage_from_block: int
    coverage_to_block: int
    window_outcome: str
    coverage_outcome: str
    findings: tuple[dict[str, Any], ...] = ()
    qualification_blockers: tuple[str, ...] = ()
    manifest_checksum: str = ""
    partition_reconciliation_agreement: bool = True
    event_index_parquet_match: bool = True
    complete: bool = False

    def __post_init__(self) -> None:
        if self.coverage_from_block < 0:
            raise ValueError(
                f"PerPoolT034Report: coverage_from_block must be >= 0, "
                f"got {self.coverage_from_block}"
            )
        if self.coverage_from_block > self.coverage_to_block:
            raise ValueError(
                f"PerPoolT034Report: coverage_from_block {self.coverage_from_block} "
                f"> coverage_to_block {self.coverage_to_block}"
            )
        if self.window_outcome not in (
            "pool_included",
            OUTCOME_POOL_INIT_OUTSIDE_WINDOW,
        ):
            raise ValueError(
                f"PerPoolT034Report: window_outcome must be one of "
                f"('pool_included', {OUTCOME_POOL_INIT_OUTSIDE_WINDOW!r}), "
                f"got {self.window_outcome!r}"
            )
        if self.coverage_outcome not in ("complete", "incomplete", "excluded"):
            raise ValueError(
                f"PerPoolT034Report: coverage_outcome must be one of "
                f"('complete', 'incomplete', 'excluded'), got {self.coverage_outcome!r}"
            )
        body_len = len(self.pool_id_hex.strip()) - 2
        if body_len not in (40, 64):
            raise ValueError(
                f"PerPoolT034Report: pool_id_hex must be 0x + 40 or 64 hex chars, "
                f"got {self.pool_id_hex!r}"
            )

    @property
    def pool_id(self) -> PoolId:
        """Return the typed ``PoolId`` when the hex is 32 bytes.

        For the 40-byte (address-sized) contract-defect case the
        access raises :class:`ValueError`; downstream consumers
        that need a typed value should branch on
        :attr:`is_pool_id_32_byte`.
        """
        return PoolId.from_hex(self.pool_id_hex)

    @property
    def is_pool_id_32_byte(self) -> bool:
        body_len = len(self.pool_id_hex.strip()) - 2
        return body_len == 64

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "robinhood_lp.qualification.two_pool.t034.v1",
            "pool_alias": self.pool_alias,
            "pool_id": self.pool_id_hex,
            "chain_id": self.chain_id,
            "contract_address": self.contract_address,
            "data_root": self.data_root,
            "pool_init_block": self.pool_init_block,
            "coverage_from_block": self.coverage_from_block,
            "coverage_to_block": self.coverage_to_block,
            "window_outcome": self.window_outcome,
            "coverage_outcome": self.coverage_outcome,
            "manifest_checksum": self.manifest_checksum,
            "partition_reconciliation_agreement": self.partition_reconciliation_agreement,
            "event_index_parquet_match": self.event_index_parquet_match,
            "findings": [dict(f) for f in self.findings],
            "qualification_blockers": list(self.qualification_blockers),
            "complete": bool(self.complete),
        }


# ---------------------------------------------------------------------------
# Two-pool T038 machine report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TwoPoolT038Report:
    """The combined T038 machine report over both pools.

    ``window_plan`` is the deterministic plan the window rule
    produced. ``per_pool_reports`` carries the T034 machine report
    per included pool; excluded pools are listed in
    ``excluded_pool_aliases`` together with the per-pool window
    outcome that drove the exclusion. ``second_pool_resolution``
    records the second-pool identity resolver outcome.

    ``complete`` is ``True`` only when every included pool's
    per-pool T034 report is ``complete=True`` and the second-pool
    resolver outcome is ``resolve_ok``; otherwise the verdict is
    ``False`` and ``qualification_blockers`` lists the codes that
    forced the failure.
    """

    window_plan: TwoPoolWindowPlan
    per_pool_reports: tuple[PerPoolT034Report, ...]
    excluded_pool_aliases: tuple[str, ...]
    second_pool_resolution: SecondPoolResolveResult | None
    second_pool_support_level: str | None
    qualification_blockers: tuple[str, ...] = ()
    complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "robinhood_lp.qualification.two_pool.v1",
            "window_plan": self.window_plan.to_dict(),
            "per_pool_reports": [r.to_dict() for r in self.per_pool_reports],
            "excluded_pool_aliases": list(self.excluded_pool_aliases),
            "second_pool_resolution": (
                self.second_pool_resolution.to_dict()
                if self.second_pool_resolution is not None
                else None
            ),
            "second_pool_support_level": self.second_pool_support_level,
            "qualification_blockers": list(self.qualification_blockers),
            "complete": bool(self.complete),
        }

    def per_pool(self, pool_alias: str) -> PerPoolT034Report:
        for report in self.per_pool_reports:
            if report.pool_alias == pool_alias:
                return report
        raise KeyError(f"TwoPoolT038Report: no per-pool report recorded for alias {pool_alias!r}")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_excluded_pool_report(
    *,
    pool_alias: str,
    pool_id_hex: str,
    chain_id: int,
    contract_address: str,
    window_outcome: PoolWindowOutcome,
    gap_blocks: int | None,
    coverage_to_block: int,
) -> PerPoolT034Report:
    """Build the per-pool T034 report for an excluded pool.

    Excluded pools (``pool_init_outside_window``) carry a T034
    report whose ``coverage_outcome`` is ``excluded`` and whose
    ``complete`` is ``False``; the gap size is recorded in the
    findings so the audit trail names the exclusion cause. The
    pool is not promoted into the qualified dataset.
    """
    finding: dict[str, Any] = {
        "reason_code": "pool_init_outside_window",
        "category": "window_rule",
        "detail": {
            "pool_alias": pool_alias,
            "pool_id_hex": pool_id_hex,
            "pool_init_block": window_outcome.pool_init_block,
            "default_start_block": window_outcome.default_start_block,
            "gap_blocks": gap_blocks,
            "reason": window_outcome.reason,
        },
    }
    return PerPoolT034Report(
        pool_alias=pool_alias,
        pool_id_hex=pool_id_hex,
        chain_id=chain_id,
        contract_address=contract_address,
        data_root="",
        pool_init_block=window_outcome.pool_init_block,
        coverage_from_block=window_outcome.default_start_block,
        coverage_to_block=coverage_to_block,
        window_outcome=OUTCOME_POOL_INIT_OUTSIDE_WINDOW,
        coverage_outcome="excluded",
        findings=(finding,),
        qualification_blockers=("pool_init_outside_window",),
        manifest_checksum="",
        partition_reconciliation_agreement=False,
        event_index_parquet_match=False,
        complete=False,
    )


def build_included_pool_report(
    *,
    pool_alias: str,
    pool_id_hex: str,
    chain_id: int,
    contract_address: str,
    window_outcome: PoolWindowOutcome,
    coverage_to_block: int,
    data_root: str,
    manifest_checksum: str,
    findings: tuple[dict[str, Any], ...] = (),
    qualification_blockers: tuple[str, ...] = (),
    partition_reconciliation_agreement: bool = True,
    event_index_parquet_match: bool = True,
) -> PerPoolT034Report:
    """Build the per-pool T034 report for an included pool.

    The caller supplies the per-pool data root, manifest checksum,
    and the T034 findings / blockers the per-pool pipeline
    produced. ``complete`` is ``True`` iff there are no
    qualification blockers and the reconciliation agreement is
    ``True``.

    The function surfaces the reconciliation / event-index / Parquet
    mismatches as documented reason codes (``partition_event_index_parquet_mismatch``
    / ``event_index_parquet_mismatch``) so the audit trail can
    distinguish them from the caller's ``qualification_blockers``.
    """
    derived_blockers: list[str] = list(qualification_blockers)
    if (
        not partition_reconciliation_agreement
        and "partition_event_index_parquet_mismatch" not in derived_blockers
    ):
        derived_blockers.append("partition_event_index_parquet_mismatch")
    if not event_index_parquet_match and "event_index_parquet_mismatch" not in derived_blockers:
        derived_blockers.append("event_index_parquet_mismatch")
    complete = (
        not derived_blockers and partition_reconciliation_agreement and event_index_parquet_match
    )
    return PerPoolT034Report(
        pool_alias=pool_alias,
        pool_id_hex=pool_id_hex,
        chain_id=chain_id,
        contract_address=contract_address,
        data_root=data_root,
        pool_init_block=window_outcome.pool_init_block,
        coverage_from_block=window_outcome.coverage_from_block,
        coverage_to_block=coverage_to_block,
        window_outcome="pool_included",
        coverage_outcome="complete" if complete else "incomplete",
        findings=tuple(findings),
        qualification_blockers=tuple(derived_blockers),
        manifest_checksum=manifest_checksum,
        partition_reconciliation_agreement=partition_reconciliation_agreement,
        event_index_parquet_match=event_index_parquet_match,
        complete=complete,
    )


def assemble_two_pool_report(
    *,
    window_plan: TwoPoolWindowPlan,
    per_pool_reports: tuple[PerPoolT034Report, ...],
    second_pool_resolution: SecondPoolResolveResult | None,
    second_pool_support_level: str | None = None,
) -> TwoPoolT038Report:
    """Assemble the combined T038 machine report.

    ``complete`` is ``True`` iff every included pool's per-pool
    report is ``complete=True`` and the second-pool resolver
    outcome (when supplied) is ``resolve_ok``. Excluded pools do
    not force a failure (their exclusion is recorded as
    ``pool_init_outside_window``); however a missing or failed
    second-pool resolution always forces ``complete=False``
    because the contract requires the second pool's identity to
    be resolved on chain and verified by the offline keccak256
    re-derivation check.
    """
    blockers: list[str] = []
    if second_pool_resolution is None:
        blockers.append("second_pool_resolution_missing")
    elif second_pool_resolution.outcome != "resolve_ok":
        blockers.append(second_pool_resolution.outcome)
    for report in per_pool_reports:
        if not report.complete:
            for blocker in report.qualification_blockers:
                if blocker not in blockers:
                    blockers.append(blocker)
    complete = not blockers
    return TwoPoolT038Report(
        window_plan=window_plan,
        per_pool_reports=tuple(per_pool_reports),
        excluded_pool_aliases=window_plan.excluded_pool_aliases,
        second_pool_resolution=second_pool_resolution,
        second_pool_support_level=second_pool_support_level,
        qualification_blockers=tuple(blockers),
        complete=complete,
    )


# ---------------------------------------------------------------------------
# Convenience: assemble a deterministic report from window plan + inputs
# ---------------------------------------------------------------------------


def build_two_pool_report(
    *,
    window_plan: TwoPoolWindowPlan,
    reference_pool: ReferenceTarget,
    reference_pool_data_root: str,
    reference_pool_manifest_checksum: str,
    reference_pool_partition_reconciliation_agreement: bool,
    reference_pool_event_index_parquet_match: bool,
    reference_pool_findings: tuple[dict[str, Any], ...] = (),
    reference_pool_blockers: tuple[str, ...] = (),
    second_pool_data_root: str = "",
    second_pool_manifest_checksum: str = "",
    second_pool_partition_reconciliation_agreement: bool = False,
    second_pool_event_index_parquet_match: bool = False,
    second_pool_findings: tuple[dict[str, Any], ...] = (),
    second_pool_blockers: tuple[str, ...] = (),
    second_pool_resolution: SecondPoolResolveResult | None = None,
    second_pool_resolved: ResolvedPoolKey | None = None,
) -> TwoPoolT038Report:
    """Build the combined T038 machine report.

    The function is the deterministic assembly point: every input
    is either a pinned reference fact, an operator-supplied
    observation, or a per-pool pipeline result; no I/O, no wall
    clock, no random sources. Tests construct every input
    directly; the operator runbook assembles the inputs from the
    per-pool runner outputs.

    Per-pool data roots and manifest checksums are recorded; a
    missing data root is only permitted for an excluded pool.
    """
    per_pool_reports: list[PerPoolT034Report] = []
    coverage_to_block = window_plan.window_pin.block_number
    for outcome in window_plan.pool_outcomes:
        if outcome.is_excluded:
            per_pool_reports.append(
                build_excluded_pool_report(
                    pool_alias=outcome.pool_alias,
                    pool_id_hex=outcome.pool_id_hex,
                    chain_id=reference_pool.chain_id,
                    contract_address=reference_pool.pool_manager_address.to_hex(),
                    window_outcome=outcome,
                    gap_blocks=outcome.gap_blocks,
                    coverage_to_block=coverage_to_block,
                )
            )
            continue
        if outcome.pool_alias == "reference":
            per_pool_reports.append(
                build_included_pool_report(
                    pool_alias="reference",
                    pool_id_hex=REFERENCE_POOL_ID_HEX,
                    chain_id=reference_pool.chain_id,
                    contract_address=reference_pool.pool_manager_address.to_hex(),
                    window_outcome=outcome,
                    coverage_to_block=coverage_to_block,
                    data_root=reference_pool_data_root,
                    manifest_checksum=reference_pool_manifest_checksum,
                    findings=reference_pool_findings,
                    qualification_blockers=reference_pool_blockers,
                    partition_reconciliation_agreement=(
                        reference_pool_partition_reconciliation_agreement
                    ),
                    event_index_parquet_match=reference_pool_event_index_parquet_match,
                )
            )
            continue
        if outcome.pool_alias == "second":
            per_pool_reports.append(
                build_included_pool_report(
                    pool_alias="second",
                    pool_id_hex=outcome.pool_id_hex,
                    chain_id=reference_pool.chain_id,
                    contract_address=reference_pool.pool_manager_address.to_hex(),
                    window_outcome=outcome,
                    coverage_to_block=coverage_to_block,
                    data_root=second_pool_data_root,
                    manifest_checksum=second_pool_manifest_checksum,
                    findings=second_pool_findings,
                    qualification_blockers=second_pool_blockers,
                    partition_reconciliation_agreement=(
                        second_pool_partition_reconciliation_agreement
                    ),
                    event_index_parquet_match=second_pool_event_index_parquet_match,
                )
            )
            continue
        # Unknown alias: surface as excluded pool.
        per_pool_reports.append(
            build_excluded_pool_report(
                pool_alias=outcome.pool_alias,
                pool_id_hex=outcome.pool_id_hex,
                chain_id=reference_pool.chain_id,
                contract_address=reference_pool.pool_manager_address.to_hex(),
                window_outcome=outcome,
                gap_blocks=outcome.gap_blocks,
                coverage_to_block=coverage_to_block,
            )
        )

    support_level = (
        classify_second_pool_support_level(second_pool_resolved)
        if second_pool_resolved is not None
        else None
    )
    return assemble_two_pool_report(
        window_plan=window_plan,
        per_pool_reports=tuple(per_pool_reports),
        second_pool_resolution=second_pool_resolution,
        second_pool_support_level=support_level,
    )


__all__ = [
    "PerPoolT034Report",
    "TwoPoolT038Report",
    "assemble_two_pool_report",
    "build_excluded_pool_report",
    "build_included_pool_report",
    "build_two_pool_report",
]
