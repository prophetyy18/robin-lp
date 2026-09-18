"""Two-pool failure-path evidence (T038).

The T038 contract enumerates five documented failure modes the
two-pool acquisition must demonstrate each produce its named
reason code and never a false ``complete=true``:

1. ``finalized_unavailable`` — neither endpoint returned the
   ``finalized`` tag at run start; the window end cannot be
   pinned and the run halts without acquiring anything;
2. ``finalized_endpoint_disagreement`` — the two endpoints
   returned different finalized blocks; the run halts;
3. ``request_wider_than_per_call_capability`` — a single
   ``eth_getLogs`` request wider than the endpoint's measured
   per-call capability; the documented reason code is
   ``range_too_large``;
4. ``http_429_rate_limit`` — rapid sequential calls produced
   HTTP 429; the documented reason code is ``http_429_rate_limit``;
5. ``budget_exhausted`` — the secondary endpoint's remaining
   budget dropped to zero before the window was fully covered.

The module is the deterministic, offline helper that builds the
:class:`TwoPoolFailurePathEvidence` rows the two-pool T038 machine
report attaches. Every failure path uses a closed mapping from
``kind`` to ``reason_code`` that ``__post_init__`` enforces.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# Failure-path kind constants
# ---------------------------------------------------------------------------

FAILURE_PATH_KIND_FINALIZED_UNAVAILABLE: str = "finalized_unavailable"
FAILURE_PATH_KIND_FINALIZED_DISAGREEMENT: str = "finalized_endpoint_disagreement"
FAILURE_PATH_KIND_REQUEST_WIDER_THAN_CAPABILITY: str = "request_wider_than_per_call_capability"
FAILURE_PATH_KIND_HTTP_429: str = "http_429_rate_limit"
FAILURE_PATH_KIND_BUDGET_EXHAUSTED: str = "budget_exhausted"

#: Mapping from failure-path kind to the T034 reason code the
#: two-pool T038 machine report surfaces for it. The mapping is
#: closed: adding a new failure-path kind requires a matching
#: reason code in :mod:`robinhood_lp.quality.reason_codes` and a
#: corresponding failure-path-kind constant in this module.
_FAILURE_PATH_KIND_TO_REASON_CODE: Mapping[str, str] = {
    FAILURE_PATH_KIND_FINALIZED_UNAVAILABLE: "finalized_unavailable",
    FAILURE_PATH_KIND_FINALIZED_DISAGREEMENT: "finalized_endpoint_disagreement",
    FAILURE_PATH_KIND_REQUEST_WIDER_THAN_CAPABILITY: "range_too_large",
    FAILURE_PATH_KIND_HTTP_429: "http_429_rate_limit",
    FAILURE_PATH_KIND_BUDGET_EXHAUSTED: "budget_exhausted",
}

DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS: Final[tuple[str, ...]] = (
    FAILURE_PATH_KIND_FINALIZED_UNAVAILABLE,
    FAILURE_PATH_KIND_FINALIZED_DISAGREEMENT,
    FAILURE_PATH_KIND_REQUEST_WIDER_THAN_CAPABILITY,
    FAILURE_PATH_KIND_HTTP_429,
    FAILURE_PATH_KIND_BUDGET_EXHAUSTED,
)


@dataclass(frozen=True, slots=True)
class TwoPoolFailurePathEvidence:
    """One row of the T038 failure-path evidence ledger.

    ``kind`` is the documented failure-path kind from
    :data:`DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS`; ``reason_code``
    is the reason code the two-pool machine report surfaces for it;
    ``detail`` carries the run-specific context (endpoint alias,
    block number / hash, observed error message, etc.);
    ``observed_at`` is the audit-trail UTC timestamp for when the
    evidence was recorded.
    """

    kind: str
    reason_code: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    observed_at: str = ""

    def __post_init__(self) -> None:
        if self.kind not in DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS:
            raise ValueError(
                f"TwoPoolFailurePathEvidence: kind {self.kind!r} not in "
                f"DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS"
            )
        expected = _FAILURE_PATH_KIND_TO_REASON_CODE[self.kind]
        if self.reason_code != expected:
            raise ValueError(
                f"TwoPoolFailurePathEvidence: reason_code {self.reason_code!r} for "
                f"kind {self.kind!r} does not match the documented {expected!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason_code": self.reason_code,
            "detail": dict(self.detail),
            "observed_at": self.observed_at,
        }


def build_two_pool_failure_path_evidence(
    *,
    kind: str,
    detail: Mapping[str, Any] | None = None,
    observed_at: str = "",
) -> TwoPoolFailurePathEvidence:
    """Build a single :class:`TwoPoolFailurePathEvidence` row.

    The helper exists so the two-pool machine report's failure-path
    builder does not need to know the closed mapping between kind
    and reason code; the closed mapping is enforced by
    :class:`TwoPoolFailurePathEvidence`'s ``__post_init__``
    validator.
    """
    if kind not in DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS:
        raise ValueError(
            f"build_two_pool_failure_path_evidence: kind {kind!r} not in "
            f"DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS"
        )
    return TwoPoolFailurePathEvidence(
        kind=kind,
        reason_code=_FAILURE_PATH_KIND_TO_REASON_CODE[kind],
        detail=detail if detail is not None else {},
        observed_at=observed_at,
    )


def all_documented_two_pool_failure_path_evidence(
    *,
    observed_at: str = "",
    detail_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[TwoPoolFailurePathEvidence, ...]:
    """Build one :class:`TwoPoolFailurePathEvidence` row per documented
    failure-path kind.

    The fixture injects every documented failure mode once with the
    canonical reason-code mapping; a real run records the same rows
    from the runner's deviation ledger.
    """
    overrides = detail_overrides or {}
    rows: list[TwoPoolFailurePathEvidence] = []
    for kind in DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS:
        rows.append(
            build_two_pool_failure_path_evidence(
                kind=kind,
                detail=overrides.get(kind, {}),
                observed_at=observed_at,
            )
        )
    return tuple(rows)


def fail_complete_under_two_pool_failure_paths(
    evidence: Iterable[TwoPoolFailurePathEvidence],
) -> bool:
    """Return ``True`` iff any documented two-pool failure-path row
    would force ``complete=false``.

    The two-pool contract states that a ``finalized_unavailable`` or
    ``finalized_endpoint_disagreement`` outcome always forces
    ``complete=false`` because the run cannot pin a window end and
    therefore cannot claim any coverage. ``budget_exhausted`` and
    ``range_too_large`` similarly force ``complete=false`` for the
    pool whose budget ran out or whose single call exceeded the
    endpoint's measured capability.
    """
    for ev in evidence:
        if ev.reason_code in (
            "finalized_unavailable",
            "finalized_endpoint_disagreement",
            "range_too_large",
            "budget_exhausted",
        ):
            return True
    return False


__all__ = [
    "DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS",
    "FAILURE_PATH_KIND_BUDGET_EXHAUSTED",
    "FAILURE_PATH_KIND_FINALIZED_DISAGREEMENT",
    "FAILURE_PATH_KIND_FINALIZED_UNAVAILABLE",
    "FAILURE_PATH_KIND_HTTP_429",
    "FAILURE_PATH_KIND_REQUEST_WIDER_THAN_CAPABILITY",
    "TwoPoolFailurePathEvidence",
    "all_documented_two_pool_failure_path_evidence",
    "build_two_pool_failure_path_evidence",
    "fail_complete_under_two_pool_failure_paths",
]
