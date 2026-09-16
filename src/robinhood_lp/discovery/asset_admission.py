"""Two-track asset admission records (T025).

Consumes the T022 ``PoolRegistry``, T023 ``EligibilityDecision`` per row,
and T024 ``ChainCapabilityReport`` and produces a per-(chain, PoolKey)
admission record with two independent tracks:

* **Track A** — Technical admission, derived deterministically from the
  T022 / T023 / T024 evidence. Track A does NOT promote any pool to
  ``live``; it only certifies whether the technical evidence is
  consistent and complete (or why it is not).
* **Track B** — Operator admission, gated on an explicit Owner
  decision. The Live block is empty by default; Track B is
  ``pending_owner`` until the Owner supplies a recorded
  :class:`OperatorDecision`. Track B does NOT directly promote ``live``
  either (Live promotion is G-LIVE-01 / G-LIVE-GATE-01 territory and is
  out of scope for T025).

The closeout report (:func:`build_p02_closeout_report`) records the
three P02 README exit-gate conditions:

1. two providers (or provider plus fixture node) give equivalent
   results — sourced from the T024 ``ChainCapabilityReport.cross_endpoint``
2. deployment report passes — sourced from the T024 ``ChainCapabilityReport.passed``
3. every discovered pool has an explicit support reason — sourced from
   the T023 ``EligibilityDecision.reasons`` per ``PoolRecord``

T025 acceptance (todo/phases/P02-chain-access-and-discovery/T025.md):

* technical-eligibility / project-risk / user-decision records per
  candidate pool
* ``HOLD`` / ``LP`` / ``AUTO_SWAP`` permissions plus USDG exposure
* one-active-PoolKey selection + switching audit
* invalidation on code / proxy / admin / hook evidence changes

T025 must-not:

* auto-select / switch token or PoolKey
* infer ``AUTO_SWAP`` from ``LP``
* let a Token approval approve its pools
* promote unknown Token / Hook accounting semantics
* mint a synthetic OWNER_DECISION; Track B stays ``pending_owner`` until
  the Owner actually decides.

This module sits in the storage layer per ADR-006 and depends on
``robinhood_lp.protocol``, ``robinhood_lp.discovery.registry`` (T022),
``robinhood_lp.discovery.eligibility`` (T023), and
``robinhood_lp.discovery.chain_capability`` (T024). It must not import
``robinhood_lp.config`` or higher layers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from robinhood_lp.discovery.chain_capability import ChainCapabilityReport
from robinhood_lp.discovery.eligibility import (
    EligibilityDecision,
    EligibilityReasonCode,
)
from robinhood_lp.discovery.registry import PoolRecord, PoolRegistry
from robinhood_lp.protocol import ChainId, PoolId, RunMode

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Stable reason-code strings for Track A outcomes. Mirrors the
#: T023 ``EligibilityReasonCode`` vocabulary where possible and adds a
#: few Track-A-specific codes that describe capability-side outcomes.
TRACK_A_REJECTED_BY_PROXY: Final[str] = "rejected_by_proxy"
TRACK_A_REJECTED_BY_CAPABILITY: Final[str] = "rejected_by_capability"
TRACK_A_INGESTION_ONLY_PENDING_REVIEW: Final[str] = "ingestion_only_pending_review"
TRACK_A_PENDING_CAPABILITY: Final[str] = "pending_capability"
TRACK_A_ADMITTED: Final[str] = "admitted"


#: Track B statuses. ``pending_owner`` is the default until an
#: explicit Owner decision arrives; ``admitted`` only after the Owner
#: decision is recorded. Track B does NOT directly promote ``live`` —
#: the ``live`` promotion is the G-LIVE-GATE-01 workflow gate and is
#: intentionally out of scope for T025.
TRACK_B_PENDING_OWNER: Final[str] = "pending_owner"
TRACK_B_ADMITTED: Final[str] = "admitted"
TRACK_B_REJECTED_BY_OWNER: Final[str] = "rejected_by_owner"


#: Operator-decision outcome values. The set is closed; only one of
#: these four strings may appear on an :class:`OperatorDecision`.
class OperatorDecisionOutcome(StrEnum):
    """Closed set of recorded operator decisions."""

    #: The pool has not yet been considered by the Owner.
    PENDING = "pending"
    #: The Owner approves Track B admission for this pool.
    APPROVED = "approved"
    #: The Owner explicitly rejects Track B admission.
    REJECTED = "rejected"
    #: The Owner revokes a previous Track B admission.
    REVOKED = "revoked"


#: Decode-rule version is recorded on every admission row so the
#: audit trail can identify which artifact version produced the
#: decision. T022 + T023 + T024 are version-pinned elsewhere; this
#: constant is the symbol the asset-admission module binds its output
#: to.
DEFAULT_DECODE_RULE_VERSION: Final[str] = (
    "v4-core-e50237c/T022+registry+log_decoder; T023/eligibility; T024/capability"
)


# ---------------------------------------------------------------------------
# Operator decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperatorDecision:
    """An explicit Owner decision for one (chain, PoolKey).

    T025 must NOT mint a synthetic Owner decision; this record is the
    artifact an external Owner-driven workflow would supply. The
    decision is keyed on ``(chain_id, pool_id)`` and overrides Track B
    for that pool only.
    """

    decision_id: str
    chain_id: int
    pool_id: str  # hex
    outcome: OperatorDecisionOutcome
    #: Free-form reference back to the OWNER_DECISION_REQUIRED workflow
    #: route (URL, ticket id, or schema reference). Required: the audit
    #: trail must be traceable to an external Owner process.
    owner_decision_ref: str
    decided_at: str  # ISO-8601 UTC timestamp
    evidence_pointers: list[str] = field(default_factory=list)
    reason: str | None = None


# ---------------------------------------------------------------------------
# Pool admission record
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PoolAdmissionRecord:
    """Two-track admission record for one (chain, PoolKey).

    The record is the audit-trail artifact; every field is recorded
    even when its value is empty / pending. Track A and Track B are
    independent: a row can have Track A ``admitted`` and Track B
    ``pending_owner`` simultaneously (the common case for pools whose
    technical evidence is complete but the Owner has not yet decided).
    """

    chain_id: int
    pool_id: str  # hex
    pool_key: dict[str, Any]
    eligibility_level: str  # the RunMode assigned by T023
    reason_codes: list[str]
    evidence_pointers: list[str]
    track_a_status: str
    track_a_reasons: list[str]
    track_b_status: str
    track_b_owner_decision_ref: str | None
    operator_decision: OperatorDecision | None
    #: The live-promotion block. Always ``None`` for T025: Track B
    #: does NOT directly promote ``live`` (G-LIVE-01 / G-LIVE-GATE-01
    #: territory). Surfaced as an explicit field so downstream callers
    #: have a single uniform shape.
    live_block: dict[str, Any] | None = None
    decode_rule_version: str = DEFAULT_DECODE_RULE_VERSION


# ---------------------------------------------------------------------------
# Closeout report
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class P02CloseoutReport:
    """The P02 README exit-gate report.

    The three exit-gate conditions are computed from the inputs:

    1. ``two_provider_agreement`` — sourced from
       :class:`ChainCapabilityReport.cross_endpoint`. The T024 report
       records per-endpoint chain_id / pool_manager / state_view
       bytecode-hash agreement; the closeout surfaces that as a single
       boolean.
    2. ``deployment_report_passed`` — sourced from
       :class:`ChainCapabilityReport.passed`. ``True`` when the
       report has zero errors AND every pool's capability section is
       fail-closed on drift.
    3. ``every_pool_has_support_reason`` — ``True`` when every
       ``PoolRecord`` produced by T022 has a non-empty
       ``EligibilityDecision.reasons`` list from T023.

    The aggregate ``p02_exit_gate_met`` is the logical AND of the
    three. It does NOT promote any pool to ``live``; promotion is the
    G-LIVE-GATE-01 territory and is out of scope for T025.
    """

    chain_id: int
    pool_admissions: list[PoolAdmissionRecord]
    two_provider_agreement: bool
    deployment_report_passed: bool
    every_pool_has_support_reason: bool
    p02_exit_gate_met: bool
    evidence_pointers: list[str]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "pool_admissions": [_admission_to_dict(a) for a in self.pool_admissions],
            "two_provider_agreement": self.two_provider_agreement,
            "deployment_report_passed": self.deployment_report_passed,
            "every_pool_has_support_reason": self.every_pool_has_support_reason,
            "p02_exit_gate_met": self.p02_exit_gate_met,
            "evidence_pointers": list(self.evidence_pointers),
            "generated_at": self.generated_at,
        }


def _admission_to_dict(record: PoolAdmissionRecord) -> dict[str, Any]:
    return {
        "chain_id": record.chain_id,
        "pool_id": record.pool_id,
        "pool_key": record.pool_key,
        "eligibility_level": record.eligibility_level,
        "reason_codes": list(record.reason_codes),
        "evidence_pointers": list(record.evidence_pointers),
        "track_a_status": record.track_a_status,
        "track_a_reasons": list(record.track_a_reasons),
        "track_b_status": record.track_b_status,
        "track_b_owner_decision_ref": record.track_b_owner_decision_ref,
        "operator_decision": (
            None
            if record.operator_decision is None
            else {
                "decision_id": record.operator_decision.decision_id,
                "chain_id": record.operator_decision.chain_id,
                "pool_id": record.operator_decision.pool_id,
                "outcome": record.operator_decision.outcome.value,
                "owner_decision_ref": record.operator_decision.owner_decision_ref,
                "decided_at": record.operator_decision.decided_at,
                "evidence_pointers": list(record.operator_decision.evidence_pointers),
                "reason": record.operator_decision.reason,
            }
        ),
        "live_block": record.live_block,
        "decode_rule_version": record.decode_rule_version,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pool_key_to_dict(pool_key: Any) -> dict[str, Any]:
    """Serialise a ``PoolKey`` to the canonical audit-trail dict.

    Only fields that participate in identity are included. Token
    metadata is intentionally absent (metadata is display-only per
    ADM-TECH-002).
    """
    return {
        "currency0": pool_key.currency0.to_address().to_hex(),
        "currency1": pool_key.currency1.to_address().to_hex(),
        "fee": pool_key.fee,
        "tick_spacing": pool_key.tick_spacing,
        "hooks": pool_key.hooks.to_hex(),
    }


def _pool_record_evidence_pointers(record: PoolRecord) -> list[str]:
    """Build the registry-side evidence pointers for one row."""
    pointers: list[str] = [
        f"pool_id={record.pool_id.to_hex()}",
        f"tx_hash_first_seen={record.tx_hash_first_seen}",
        f"log_index_first_seen={record.log_index_first_seen}",
        f"block_number_first_seen={record.block_number_first_seen}",
    ]
    if record.sqrt_price_x96 is not None:
        pointers.append(f"sqrt_price_x96={record.sqrt_price_x96}")
    if record.initial_tick is not None:
        pointers.append(f"initial_tick={record.initial_tick}")
    return pointers


def _has_bytecode_drift(report: ChainCapabilityReport | None) -> bool:
    """A T024 report records bytecode drift via its ``errors`` list.

    The probe surfaces ``"bytecode drift: observed=X expected=Y"`` on
    drift (T024 chain_capability.py line 444/471). We use the substring
    match because the report is the authoritative artifact.
    """
    if report is None:
        return False
    return any("bytecode drift" in err for err in report.errors)


def _compute_track_a(
    record: PoolRecord,
    decision: EligibilityDecision,
    capability_report: ChainCapabilityReport | None,
    artifact_sha256: str | None,
    deployment_tx_hash: str | None,
    decode_rule_version: str,
) -> tuple[str, list[str], list[str]]:
    """Compute the Track A status, reasons, and evidence pointers.

    Track A is the deterministic technical-admission derivation. It is
    *fail-closed* by construction: any single disqualifying signal
    short-circuits the rest of the chain.

    Precedence (most-restrictive wins):

    1. Missing capability report -> ``pending_capability``
    2. Bytecode drift in capability report -> ``rejected_by_capability``
    3. T023 ``proxy_detected`` -> ``rejected_by_proxy``
    4. T023 ``hook_bytecode_unavailable`` -> ``ingestion_only_pending_review``
    5. T023 ``rejected`` level -> ``rejected_by_proxy`` (T023 only
       reaches ``rejected`` via the proxy path, but the mapping is
       explicit so the audit trail is unambiguous).
    6. T023 level ``ingestion`` or higher -> ``admitted``.
    """
    reasons: list[str] = []
    evidence: list[str] = list(_pool_record_evidence_pointers(record))
    evidence.extend(list(decision.evidence_pointers))
    if artifact_sha256 is not None:
        evidence.append(f"artifact_sha256={artifact_sha256}")
    if deployment_tx_hash is not None:
        evidence.append(f"deployment_tx_hash={deployment_tx_hash}")
    evidence.append(f"decode_rule_version={decode_rule_version}")

    if capability_report is None:
        reasons.append("capability_report_missing")
        return TRACK_A_PENDING_CAPABILITY, reasons, evidence

    if not capability_report.passed:
        reasons.append("capability_report_failed")
        evidence.append("capability_report.passed=false")
        return TRACK_A_REJECTED_BY_CAPABILITY, reasons, evidence

    if _has_bytecode_drift(capability_report):
        reasons.append("bytecode_drift_in_capability_report")
        evidence.append("capability_report.bytecode_drift=true")
        return TRACK_A_REJECTED_BY_CAPABILITY, reasons, evidence

    # T023 signal: EIP-1167 minimal-proxy hook bytecode is a hard reject.
    if decision.has(EligibilityReasonCode.PROXY_DETECTED):
        reasons.append(EligibilityReasonCode.PROXY_DETECTED.value)
        evidence.append("hook_bytecode_shape=eip1167_minimal_proxy")
        return TRACK_A_REJECTED_BY_PROXY, reasons, evidence

    # T023 signal: hook bytecode unavailable -> "no permission to
    # simulate" but still on the discovery surface; Track A reflects
    # this with a dedicated, non-admitted status.
    if decision.has(EligibilityReasonCode.HOOK_BYTECODE_UNAVAILABLE):
        reasons.append(EligibilityReasonCode.HOOK_BYTECODE_UNAVAILABLE.value)
        return TRACK_A_INGESTION_ONLY_PENDING_REVIEW, reasons, evidence

    # T023 only assigns ``rejected`` via the proxy path; the explicit
    # mapping preserves the audit trail even if future reason codes
    # add new reject sources.
    if decision.level == RunMode.REJECTED:
        reasons.append("eligibility_level=rejected")
        return TRACK_A_REJECTED_BY_PROXY, reasons, evidence

    # Otherwise: technical evidence is consistent and the eligibility
    # level is at least ingestion. Track A admits the row; Track B is
    # still pending_owner by default.
    reasons.append(f"eligibility_level={decision.level.value}")
    return TRACK_A_ADMITTED, reasons, evidence


def _compute_track_b(
    record: PoolRecord,
    operator_decision: OperatorDecision | None,
) -> tuple[str, str | None, OperatorDecision | None, dict[str, Any] | None]:
    """Compute Track B from the operator-decision artifact.

    The default is ``pending_owner`` for every pool. Track B does NOT
    directly promote ``live``; the ``live_block`` is always ``None``
    in this attempt. Promotion to ``live`` is G-LIVE-GATE-01 territory
    and requires a separate decision beyond Track B.
    """
    if operator_decision is None:
        return TRACK_B_PENDING_OWNER, None, None, None

    if operator_decision.outcome == OperatorDecisionOutcome.APPROVED:
        return (
            TRACK_B_ADMITTED,
            operator_decision.owner_decision_ref,
            operator_decision,
            None,
        )

    if operator_decision.outcome == OperatorDecisionOutcome.REJECTED:
        return (
            TRACK_B_REJECTED_BY_OWNER,
            operator_decision.owner_decision_ref,
            operator_decision,
            None,
        )

    if operator_decision.outcome == OperatorDecisionOutcome.REVOKED:
        # A revocation transitions Track B back to pending. The
        # operator_decision is retained for the audit trail.
        return (
            TRACK_B_PENDING_OWNER,
            operator_decision.owner_decision_ref,
            operator_decision,
            None,
        )

    # PENDING outcome on a recorded decision is equivalent to "no
    # decision yet" for Track B purposes.
    return TRACK_B_PENDING_OWNER, operator_decision.owner_decision_ref, operator_decision, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_pool_admission(
    *,
    record: PoolRecord,
    decision: EligibilityDecision,
    chain_id: ChainId,
    capability_report: ChainCapabilityReport | None = None,
    artifact_sha256: str | None = None,
    deployment_tx_hash: str | None = None,
    decode_rule_version: str = DEFAULT_DECODE_RULE_VERSION,
    operator_decision: OperatorDecision | None = None,
) -> PoolAdmissionRecord:
    """Build one ``PoolAdmissionRecord`` from the upstream artifacts.

    The function is pure (no I/O, no clock except via the supplied
    ``decoded_at`` field on the operator decision); two calls with
    byte-identical inputs produce byte-identical records. The Track B
    live_block is always ``None`` for T025.
    """
    track_a_status, track_a_reasons, evidence_pointers = _compute_track_a(
        record,
        decision,
        capability_report,
        artifact_sha256,
        deployment_tx_hash,
        decode_rule_version,
    )
    track_b_status, track_b_ref, operator_record, live_block = _compute_track_b(
        record, operator_decision
    )
    return PoolAdmissionRecord(
        chain_id=chain_id.value,
        pool_id=record.pool_id.to_hex(),
        pool_key=_pool_key_to_dict(record.pool_key),
        eligibility_level=decision.level.value,
        reason_codes=[code.value for code in decision.reason_codes()],
        evidence_pointers=evidence_pointers,
        track_a_status=track_a_status,
        track_a_reasons=track_a_reasons,
        track_b_status=track_b_status,
        track_b_owner_decision_ref=track_b_ref,
        operator_decision=operator_record,
        live_block=live_block,
        decode_rule_version=decode_rule_version,
    )


def build_p02_closeout_report(
    *,
    chain_id: ChainId,
    registry: PoolRegistry,
    eligibility_decisions: dict[PoolId, EligibilityDecision],
    capability_report: ChainCapabilityReport | None = None,
    artifact_sha256: str | None = None,
    deployment_tx_hash: str | None = None,
    decode_rule_version: str = DEFAULT_DECODE_RULE_VERSION,
    operator_decisions: dict[PoolId, OperatorDecision] | None = None,
) -> P02CloseoutReport:
    """Build the P02 closeout report from the upstream artifacts.

    The function consumes the T022 ``PoolRegistry``, the per-pool
    T023 ``EligibilityDecision`` map, and the T024
    ``ChainCapabilityReport`` and emits the closeout report plus the
    per-pool admission records. The function is deterministic: two
    calls with byte-identical inputs produce byte-identical reports
    except for ``generated_at`` (an explicit observational timestamp).
    """
    operator_decisions = operator_decisions if operator_decisions is not None else {}
    admissions: list[PoolAdmissionRecord] = []
    every_pool_has_support_reason = True
    for record in registry.all_records():
        decision = eligibility_decisions.get(record.pool_id)
        if decision is None:
            every_pool_has_support_reason = False
            continue
        if not decision.reasons:
            every_pool_has_support_reason = False
        admission = build_pool_admission(
            record=record,
            decision=decision,
            chain_id=chain_id,
            capability_report=capability_report,
            artifact_sha256=artifact_sha256,
            deployment_tx_hash=deployment_tx_hash,
            decode_rule_version=decode_rule_version,
            operator_decision=operator_decisions.get(record.pool_id),
        )
        admissions.append(admission)

    two_provider_agreement = _compute_two_provider_agreement(capability_report)
    deployment_report_passed = _compute_deployment_report_passed(capability_report)
    p02_exit_gate_met = (
        two_provider_agreement
        and deployment_report_passed
        and every_pool_has_support_reason
        and len(admissions) > 0
    )

    evidence: list[str] = []
    if artifact_sha256 is not None:
        evidence.append(f"artifact_sha256={artifact_sha256}")
    if deployment_tx_hash is not None:
        evidence.append(f"deployment_tx_hash={deployment_tx_hash}")
    evidence.append(f"decode_rule_version={decode_rule_version}")
    if capability_report is not None:
        evidence.append(
            f"capability_report.source_retrieval_time={capability_report.source_retrieval_time}"
        )

    return P02CloseoutReport(
        chain_id=chain_id.value,
        pool_admissions=admissions,
        two_provider_agreement=two_provider_agreement,
        deployment_report_passed=deployment_report_passed,
        every_pool_has_support_reason=every_pool_has_support_reason,
        p02_exit_gate_met=p02_exit_gate_met,
        evidence_pointers=evidence,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _compute_two_provider_agreement(report: ChainCapabilityReport | None) -> bool:
    """``True`` iff the T024 cross-endpoint agreement summary is consistent.

    The T024 ``CrossEndpointAgreement`` records agreement on chain_id,
    pool_manager / state_view bytecode hashes, latest block (within a
    small propagation tolerance), and pinned block hash. A pool that
    fails any of those checks fails the closeout's two-provider
    agreement condition.
    """
    if report is None or report.cross_endpoint is None:
        return False
    return (
        report.cross_endpoint.chain_id_agree
        and report.cross_endpoint.pool_manager_code_hash_agree
        and report.cross_endpoint.state_view_code_hash_agree
        and report.cross_endpoint.latest_block_agree
        and report.cross_endpoint.pinned_block_hash_agree
    )


def _compute_deployment_report_passed(report: ChainCapabilityReport | None) -> bool:
    """``True`` iff the T024 deployment report passes (no errors).

    The T024 reporter sets ``passed = not errors``; we surface that
    directly. When the report is missing, the deployment gate is not
    met.
    """
    if report is None:
        return False
    return report.passed and not report.errors


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def report_to_json(report: P02CloseoutReport) -> str:
    """Canonical JSON serialiser used by the T025 test surface.

    The output is byte-identical across runs given byte-identical
    inputs and a pinned ``generated_at`` value; the closeout report
    calls ``datetime.now(UTC)`` so two calls within the same wall-clock
    second produce byte-identical output.
    """
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


def sha256_of_report(report_dict: dict[str, Any]) -> str:
    """SHA-256 of a closeout report dict (audit hash)."""
    return hashlib.sha256(json.dumps(report_dict, sort_keys=True).encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_DECODE_RULE_VERSION",
    "OperatorDecision",
    "OperatorDecisionOutcome",
    "P02CloseoutReport",
    "PoolAdmissionRecord",
    "TRACK_A_ADMITTED",
    "TRACK_A_INGESTION_ONLY_PENDING_REVIEW",
    "TRACK_A_PENDING_CAPABILITY",
    "TRACK_A_REJECTED_BY_CAPABILITY",
    "TRACK_A_REJECTED_BY_PROXY",
    "TRACK_B_ADMITTED",
    "TRACK_B_PENDING_OWNER",
    "TRACK_B_REJECTED_BY_OWNER",
    "build_p02_closeout_report",
    "build_pool_admission",
    "report_to_json",
    "sha256_of_report",
]
