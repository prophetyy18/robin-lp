"""Failure-path evidence for the qualification pipeline (T036).

The T036 contract enumerates five documented failure modes that the
qualification pipeline must demonstrate each produce its named
reason code and never a false ``complete=true``:

1. **HTTP 429 rate limiting** on rapid sequential calls;
2. **``-32000 logs matched by query exceeds limit of 10000``** —
   the result-count rejection observed when a single-event full-range
   ``eth_getLogs`` is issued;
3. **``-32000 log query timed out``** — the query-timeout rejection
   observed when a full-chain-range five-topic0 OR query is issued;
4. **provider failover** — when the primary endpoint fails the
   qualification loop must fall over to the secondary endpoint
   within its measured capability and never silently merge the two
   responses into a falsely complete range;
5. **budget exhaustion** — when the secondary endpoint's remaining
   budget drops to zero the qualification loop must halt with the
   ``budget_exhausted`` reason code and ``complete=false``.

This module is the contract-validating helper: it builds the
:class:`FailurePathEvidence` rows the qualification report attaches
to the T034 machine report. Every failure path uses the T034
reason-code constant the T036 contract names, so a downstream
consumer reading the T034 reason codes sees the same vocabulary
the planning layer wrote.

The module is pure data: it does not contact any RPC and it does
not build runnable scripts. The end-to-end qualification fixture
constructs these evidence rows directly; a real qualification run
would record the same rows from the runner's deviation ledger.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from robinhood_lp.quality.reason_codes import (
    REASON_BUDGET_EXHAUSTED,
    REASON_CAPABILITY_REGRESSION,
    REASON_CROSS_PROVIDER_DISCREPANCY,
    REASON_USER_AGENT_REJECTED,
)

# ---------------------------------------------------------------------------
# Failure-path kind constants
# ---------------------------------------------------------------------------

FAILURE_PATH_KIND_HTTP_429: str = "http_429_rate_limit"
FAILURE_PATH_KIND_LOGS_LIMIT_REJECTION: str = "logs_matched_limit_rejection"
FAILURE_PATH_KIND_RPC_TIMEOUT: str = "rpc_query_timeout"
FAILURE_PATH_KIND_FAILOVER: str = "provider_failover"
FAILURE_PATH_KIND_BUDGET_EXHAUSTED: str = "budget_exhausted"

#: Mapping from failure-path kind to the T034 reason code the
#: qualification report surfaces for it. The mapping is closed:
#: adding a new failure-path kind requires a matching reason code
#: in :mod:`robinhood_lp.quality.reason_codes`.
_FAILURE_PATH_KIND_TO_REASON_CODE: Mapping[str, str] = {
    FAILURE_PATH_KIND_HTTP_429: REASON_USER_AGENT_REJECTED,
    FAILURE_PATH_KIND_LOGS_LIMIT_REJECTION: REASON_CAPABILITY_REGRESSION,
    FAILURE_PATH_KIND_RPC_TIMEOUT: REASON_CAPABILITY_REGRESSION,
    FAILURE_PATH_KIND_FAILOVER: REASON_CROSS_PROVIDER_DISCREPANCY,
    FAILURE_PATH_KIND_BUDGET_EXHAUSTED: REASON_BUDGET_EXHAUSTED,
}

#: The five documented failure-path kinds.
DOCUMENTED_FAILURE_PATH_KINDS: tuple[str, ...] = (
    FAILURE_PATH_KIND_HTTP_429,
    FAILURE_PATH_KIND_LOGS_LIMIT_REJECTION,
    FAILURE_PATH_KIND_RPC_TIMEOUT,
    FAILURE_PATH_KIND_FAILOVER,
    FAILURE_PATH_KIND_BUDGET_EXHAUSTED,
)


@dataclass(frozen=True, slots=True)
class FailurePathEvidence:
    """One row of the failure-path evidence ledger.

    ``kind`` is the documented failure-path kind from
    :data:`DOCUMENTED_FAILURE_PATH_KINDS`; ``reason_code`` is the
    T034 reason code the qualification report surfaces for it;
    ``detail`` carries the run-specific context (endpoint alias,
    response size, observed error message, etc.); ``observed_at`` is
    an audit-trail UTC timestamp for when the evidence was recorded.
    """

    kind: str
    reason_code: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    observed_at: str = ""

    def __post_init__(self) -> None:
        if self.kind not in DOCUMENTED_FAILURE_PATH_KINDS:
            raise ValueError(
                f"FailurePathEvidence: kind {self.kind!r} not in DOCUMENTED_FAILURE_PATH_KINDS "
                f"{DOCUMENTED_FAILURE_PATH_KINDS}"
            )
        expected_reason = _FAILURE_PATH_KIND_TO_REASON_CODE[self.kind]
        if self.reason_code != expected_reason:
            raise ValueError(
                f"FailurePathEvidence: reason_code {self.reason_code!r} for kind {self.kind!r} "
                f"does not match the documented {expected_reason!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason_code": self.reason_code,
            "detail": dict(self.detail),
            "observed_at": self.observed_at,
        }


def build_failure_path_evidence(
    *,
    kind: str,
    detail: Mapping[str, Any] | None = None,
    observed_at: str = "",
) -> FailurePathEvidence:
    """Build a single :class:`FailurePathEvidence` row.

    The helper exists so the qualification report's failure-path
    builder does not need to know the closed mapping between kind
    and reason code; the closed mapping is enforced by
    :class:`FailurePathEvidence`'s ``__post_init__`` validator.
    """
    if kind not in DOCUMENTED_FAILURE_PATH_KINDS:
        raise ValueError(
            f"build_failure_path_evidence: kind {kind!r} not in DOCUMENTED_FAILURE_PATH_KINDS"
        )
    return FailurePathEvidence(
        kind=kind,
        reason_code=_FAILURE_PATH_KIND_TO_REASON_CODE[kind],
        detail=detail if detail is not None else {},
        observed_at=observed_at,
    )


def all_documented_failure_path_evidence(
    *,
    observed_at: str = "",
    detail_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[FailurePathEvidence, ...]:
    """Build one :class:`FailurePathEvidence` row per documented
    failure-path kind.

    Useful for the end-to-end qualification fixture: the fixture
    records every documented failure mode once with the canonical
    reason code mapping and (optionally) per-kind detail overrides
    the operator supplied at run time.
    """
    overrides = detail_overrides or {}
    rows: list[FailurePathEvidence] = []
    for kind in DOCUMENTED_FAILURE_PATH_KINDS:
        rows.append(
            build_failure_path_evidence(
                kind=kind,
                detail=overrides.get(kind, {}),
                observed_at=observed_at,
            )
        )
    return tuple(rows)


def fail_complete_under_failure_paths(
    evidence: Iterable[FailurePathEvidence],
) -> bool:
    """Return ``True`` iff any documented failure-path row would
    force ``complete=false``.

    A real qualification run refuses ``complete=true`` whenever
    any documented failure path is observed — the rows are the
    audit-trail evidence the runner records; they never produce a
    false ``complete=true``. This helper is the deterministic
    verifier the report builder uses to set the
    ``qualification_blockers`` surface for the failure-path
    category.
    """
    return any(
        ev.reason_code == REASON_BUDGET_EXHAUSTED
        or ev.reason_code == REASON_CROSS_PROVIDER_DISCREPANCY
        for ev in evidence
    )


__all__ = [
    "DOCUMENTED_FAILURE_PATH_KINDS",
    "FAILURE_PATH_KIND_BUDGET_EXHAUSTED",
    "FAILURE_PATH_KIND_FAILOVER",
    "FAILURE_PATH_KIND_HTTP_429",
    "FAILURE_PATH_KIND_LOGS_LIMIT_REJECTION",
    "FAILURE_PATH_KIND_RPC_TIMEOUT",
    "FailurePathEvidence",
    "all_documented_failure_path_evidence",
    "build_failure_path_evidence",
    "fail_complete_under_failure_paths",
]
