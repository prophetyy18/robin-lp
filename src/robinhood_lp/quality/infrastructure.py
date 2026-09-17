"""Infrastructure-correlation evidence state (Decision 8).

The T034 report records the evidence state of the two endpoints as
one of three enum values:

- ``unknown_not_proven`` — current evidence state. The two endpoints
  have been observed to agree on chain identity, blocks, runtime code,
  and real historical log results, but no evidence proves the
  underlying nodes, upstream data sources, or failure domains are
  independent;
- ``known_correlated`` — separate evidence has established that the two
  endpoints share operator, infrastructure, or upstream data sources.
  Correlated failure is then a recorded residual risk;
- ``evidenced_independent`` — separate auditable operational or
  infrastructure evidence has established the two endpoints are
  independent.

The current state is :data:`UNKNOWN_NOT_PROVEN`. The state must not
be left blank.

When the state is ``unknown_not_proven`` or ``known_correlated``, the
report must surface correlated failure as an explicit residual risk.

The report describes the A+B comparison result as
**``cross_endpoint_agreement``**. The report must **not** describe the
result as ``independent_provider_agreement`` unless the state is
``evidenced_independent``.
"""

from __future__ import annotations

from enum import StrEnum


class InfrastructureCorrelationState(StrEnum):
    """The three-state enum the T034 report must use for Decision 8."""

    UNKNOWN_NOT_PROVEN = "unknown_not_proven"
    KNOWN_CORRELATED = "known_correlated"
    EVIDENCED_INDEPENDENT = "evidenced_independent"


#: The current (default) infrastructure-correlation state. The system
#: has not yet obtained separate auditable operational or
#: infrastructure evidence; the state may only transition to
#: :data:`InfrastructureCorrelationState.EVIDENCED_INDEPENDENT` after
#: separate evidence is recorded.
DEFAULT_INFRASTRUCTURE_CORRELATION_STATE: InfrastructureCorrelationState = (
    InfrastructureCorrelationState.UNKNOWN_NOT_PROVEN
)


#: The phrasing the report must use to describe the A+B comparison
#: result, unless the infrastructure-correlation state is
#: :data:`InfrastructureCorrelationState.EVIDENCED_INDEPENDENT`.
CROSS_ENDPOINT_AGREEMENT_PHRASING: str = "cross_endpoint_agreement"

#: The phrasing that must NOT appear unless the infrastructure-correlation
#: state is :data:`InfrastructureCorrelationState.EVIDENCED_INDEPENDENT`.
INDEPENDENT_PROVIDER_AGREEMENT_PHRASING: str = "independent_provider_agreement"


def agreement_phrasing_for_state(state: InfrastructureCorrelationState) -> str:
    """Return the wording the report must use for the A+B comparison.

    The wording is always :data:`CROSS_ENDPOINT_AGREEMENT_PHRASING`
    except when the state is :data:`EVIDENCED_INDEPENDENT`, where it
    is :data:`INDEPENDENT_PROVIDER_AGREEMENT_PHRASING`.
    """
    if state == InfrastructureCorrelationState.EVIDENCED_INDEPENDENT:
        return INDEPENDENT_PROVIDER_AGREEMENT_PHRASING
    return CROSS_ENDPOINT_AGREEMENT_PHRASING


def correlated_failure_is_residual_risk(
    state: InfrastructureCorrelationState,
) -> bool:
    """Return True iff the state requires the correlated-failure
    residual-risk callout.

    The callout is required for ``unknown_not_proven`` and
    ``known_correlated``. It is not required for
    ``evidenced_independent`` (the state is the positive evidence).
    """
    return state in (
        InfrastructureCorrelationState.UNKNOWN_NOT_PROVEN,
        InfrastructureCorrelationState.KNOWN_CORRELATED,
    )


__all__ = [
    "CROSS_ENDPOINT_AGREEMENT_PHRASING",
    "DEFAULT_INFRASTRUCTURE_CORRELATION_STATE",
    "INDEPENDENT_PROVIDER_AGREEMENT_PHRASING",
    "InfrastructureCorrelationState",
    "agreement_phrasing_for_state",
    "correlated_failure_is_residual_risk",
]
