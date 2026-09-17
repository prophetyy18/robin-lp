"""Reason codes and exception types for capability-driven historical
ingestion (T032).

Every coverage decision and every failure classification is reduced to
a stable ``REASON_*`` constant. The runner persists these in the run
manifest's interval table and the retry ledger so the evidence trail
explains why each interval landed where it did.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Coverage states (mutually exclusive per interval)
# ---------------------------------------------------------------------------

STATE_SCANNED_EMPTY: Final[str] = "scanned_empty"
STATE_SUCCESSFUL: Final[str] = "successful"
STATE_FAILED: Final[str] = "failed"
STATE_CANCELLED: Final[str] = "cancelled"

VALID_STATES: Final[frozenset[str]] = frozenset(
    {STATE_SCANNED_EMPTY, STATE_SUCCESSFUL, STATE_FAILED, STATE_CANCELLED}
)

# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

# scanned_empty is recorded when the response was a valid empty array
# (no reason code sub-tag). For diagnostic clarity the manifest may
# also carry REASON_EMPTY_RESPONSE to distinguish "RPC succeeded and
# returned empty" from a downstream classification.
REASON_EMPTY_RESPONSE: Final[str] = "empty_response"

# Successful intervals do not carry a reason code; the placeholder
# below is reserved for diagnostic breadcrumbs.
REASON_OK: Final[str] = "ok"

# Failures
REASON_RPC_TIMEOUT: Final[str] = "rpc_timeout"
REASON_HTTP_403_DEFAULT_USER_AGENT: Final[str] = "http_403_default_user_agent"
REASON_HTTP_403_USER_AGENT_REJECTED: Final[str] = "http_403_user_agent_rejected"
REASON_HTTP_429_RATE_LIMIT: Final[str] = "http_429_rate_limit"
REASON_HTTP_5XX: Final[str] = "http_5xx"
REASON_HTTP_4XX: Final[str] = "http_4xx"
REASON_RESPONSE_SIZE_OVERFLOW: Final[str] = "response_size_overflow"
REASON_SINGLE_BLOCK_OVERFLOW: Final[str] = "single_block_overflow"
REASON_RANGE_TOO_LARGE: Final[str] = "range_too_large"
REASON_INVALID_RESPONSE: Final[str] = "invalid_response"
REASON_BUDGET_EXHAUSTED_CALLS: Final[str] = "budget_exhausted_calls"
REASON_BUDGET_EXHAUSTED_CU: Final[str] = "budget_exhausted_cu"
REASON_BUDGET_EXHAUSTED_TIME: Final[str] = "budget_exhausted_time"
REASON_BUDGET_EXHAUSTED_RESPONSE_QUOTA: Final[str] = "budget_exhausted_response_quota"
REASON_NO_CAPABLE_ENDPOINT: Final[str] = "no_capable_endpoint"
REASON_FORK_MISMATCH: Final[str] = "fork_mismatch"
REASON_INTERNAL: Final[str] = "internal_error"
# T035 — header fetch failure surfaced by the runner when
# ``RpcBlockHeaderSource.get_headers_batch`` returns ``None`` for
# a distinct event block the ingestion cannot proceed without.
# Per ADR-012 the missing-header path halts the interval with
# ``complete=False`` rather than zero-defaulting the
# ``block_timestamp`` / ``parent_hash`` fields.
REASON_HEADER_FETCH_FAILED: Final[str] = "header_fetch_failed"

# The umbrella budget_exhausted reason code that the manifest records
# at the run level when the qualification halt is triggered by budget
# depletion. Sub-tags identify which budget ran out (calls / CU / time
# / response quota) and are recorded alongside the interval row.
REASON_BUDGET_EXHAUSTED: Final[str] = "budget_exhausted"

# Cancellation / skip
REASON_CANCELLED_BY_OPERATOR: Final[str] = "cancelled_by_operator"
REASON_SKIPPED_NOT_REQUIRED: Final[str] = "skipped_not_required"

ALL_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        REASON_EMPTY_RESPONSE,
        REASON_OK,
        REASON_RPC_TIMEOUT,
        REASON_HTTP_403_DEFAULT_USER_AGENT,
        REASON_HTTP_403_USER_AGENT_REJECTED,
        REASON_HTTP_429_RATE_LIMIT,
        REASON_HTTP_5XX,
        REASON_HTTP_4XX,
        REASON_RESPONSE_SIZE_OVERFLOW,
        REASON_SINGLE_BLOCK_OVERFLOW,
        REASON_RANGE_TOO_LARGE,
        REASON_INVALID_RESPONSE,
        REASON_BUDGET_EXHAUSTED_CALLS,
        REASON_BUDGET_EXHAUSTED_CU,
        REASON_BUDGET_EXHAUSTED_TIME,
        REASON_BUDGET_EXHAUSTED_RESPONSE_QUOTA,
        REASON_NO_CAPABLE_ENDPOINT,
        REASON_FORK_MISMATCH,
        REASON_INTERNAL,
        REASON_HEADER_FETCH_FAILED,
        REASON_BUDGET_EXHAUSTED,
        REASON_CANCELLED_BY_OPERATOR,
        REASON_SKIPPED_NOT_REQUIRED,
    }
)

# ---------------------------------------------------------------------------
# Retry ledger outcomes
# ---------------------------------------------------------------------------

RETRY_OUTCOME_SUCCESS: Final[str] = "success"
RETRY_OUTCOME_RETRY: Final[str] = "retry"
RETRY_OUTCOME_FAILOVER: Final[str] = "failover"
RETRY_OUTCOME_FAILED: Final[str] = "failed"
RETRY_OUTCOME_GIVE_UP: Final[str] = "give_up"

VALID_RETRY_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        RETRY_OUTCOME_SUCCESS,
        RETRY_OUTCOME_RETRY,
        RETRY_OUTCOME_FAILOVER,
        RETRY_OUTCOME_FAILED,
        RETRY_OUTCOME_GIVE_UP,
    }
)

# ---------------------------------------------------------------------------
# Topology labels
# ---------------------------------------------------------------------------

TOPOLOGY_COLD_START: Final[str] = "cold_start"
TOPOLOGY_WARM_INCREMENTAL: Final[str] = "warm_incremental"

VALID_TOPOLOGIES: Final[frozenset[str]] = frozenset(
    {TOPOLOGY_COLD_START, TOPOLOGY_WARM_INCREMENTAL}
)

# ---------------------------------------------------------------------------
# Deviation kinds (manifest)
# ---------------------------------------------------------------------------

DEVIATION_CAPABILITY_REGRESSION: Final[str] = "capability_regression"
DEVIATION_BUDGET_OVERRUN: Final[str] = "budget_overrun"
DEVIATION_OBSERVED_CAP_BELOW_MEASURED: Final[str] = "observed_cap_below_measured"
DEVIATION_ALCHEMY_10_BLOCK_CAP: Final[str] = "alchemy_10_block_cap"
DEVIATION_ROBINHOOD_UA_403: Final[str] = "robinhood_user_agent_403"
DEVIATION_RESPONSE_SIZE_OVERFLOW: Final[str] = "response_size_overflow"
DEVIATION_RATE_LIMIT_RETRY: Final[str] = "rate_limit_retry"
DEVIATION_PROVIDER_FAILOVER: Final[str] = "provider_failover"
DEVIATION_RETRY_BURST: Final[str] = "retry_burst"
DEVIATION_429_RESPONSE: Final[str] = "http_429_response"
DEVIATION_403_RESPONSE: Final[str] = "http_403_response"
DEVIATION_5XX_RESPONSE: Final[str] = "http_5xx_response"
DEVIATION_BUDGET_EXHAUSTED: Final[str] = "budget_exhausted"

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IngestionError(RuntimeError):
    """Base class for T032 ingestion errors."""


class CheckpointMismatchError(IngestionError):
    """The existing durable checkpoint does not match the requested
    pool / range / schema / decode-version / manifest checksum. The
    warm-run path raises this and refuses to advance."""


class BudgetExhaustedError(IngestionError):
    """The run's measured budget (calls / CU / time / response quota)
    has been exhausted. The interval is marked failed and the
    qualification halt is triggered; ``complete`` stays ``False``."""


class NoCapableEndpointError(IngestionError):
    """Neither endpoint has the measured capability to cover an
    interval. The interval is marked failed and the qualification
    halt is triggered; ``complete`` stays ``False``."""


class CancelledError(IngestionError):
    """The operator marked the run cancelled. The remaining intervals
    are recorded with state ``cancelled``; ``complete`` stays
    ``False``."""


def is_budget_exhausted_reason(reason_code: str | None) -> bool:
    """Return True iff the reason code is a budget-exhausted variant.

    The umbrella code ``budget_exhausted`` (the run-level halt reason)
    and every sub-tag (``budget_exhausted_calls`` /
    ``budget_exhausted_cu`` / ``budget_exhausted_time`` /
    ``budget_exhausted_response_quota``) are recognised.
    """
    if reason_code is None:
        return False
    return reason_code.startswith("budget_exhausted")


__all__ = [
    "BudgetExhaustedError",
    "CancelledError",
    "CheckpointMismatchError",
    "DEVIATION_403_RESPONSE",
    "DEVIATION_429_RESPONSE",
    "DEVIATION_5XX_RESPONSE",
    "DEVIATION_ALCHEMY_10_BLOCK_CAP",
    "DEVIATION_BUDGET_EXHAUSTED",
    "DEVIATION_BUDGET_OVERRUN",
    "DEVIATION_CAPABILITY_REGRESSION",
    "DEVIATION_OBSERVED_CAP_BELOW_MEASURED",
    "DEVIATION_PROVIDER_FAILOVER",
    "DEVIATION_RATE_LIMIT_RETRY",
    "DEVIATION_RESPONSE_SIZE_OVERFLOW",
    "DEVIATION_RETRY_BURST",
    "DEVIATION_ROBINHOOD_UA_403",
    "IngestionError",
    "NoCapableEndpointError",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_BUDGET_EXHAUSTED_CALLS",
    "REASON_BUDGET_EXHAUSTED_CU",
    "REASON_BUDGET_EXHAUSTED_RESPONSE_QUOTA",
    "REASON_BUDGET_EXHAUSTED_TIME",
    "REASON_CANCELLED_BY_OPERATOR",
    "REASON_EMPTY_RESPONSE",
    "REASON_FORK_MISMATCH",
    "REASON_HEADER_FETCH_FAILED",
    "REASON_HTTP_403_DEFAULT_USER_AGENT",
    "REASON_HTTP_403_USER_AGENT_REJECTED",
    "REASON_HTTP_429_RATE_LIMIT",
    "REASON_HTTP_4XX",
    "REASON_HTTP_5XX",
    "REASON_INTERNAL",
    "REASON_INVALID_RESPONSE",
    "REASON_NO_CAPABLE_ENDPOINT",
    "REASON_OK",
    "REASON_RANGE_TOO_LARGE",
    "REASON_RESPONSE_SIZE_OVERFLOW",
    "REASON_RPC_TIMEOUT",
    "REASON_SINGLE_BLOCK_OVERFLOW",
    "REASON_SKIPPED_NOT_REQUIRED",
    "RETRY_OUTCOME_FAILOVER",
    "RETRY_OUTCOME_FAILED",
    "RETRY_OUTCOME_GIVE_UP",
    "RETRY_OUTCOME_RETRY",
    "RETRY_OUTCOME_SUCCESS",
    "STATE_CANCELLED",
    "STATE_FAILED",
    "STATE_SCANNED_EMPTY",
    "STATE_SUCCESSFUL",
    "TOPOLOGY_COLD_START",
    "TOPOLOGY_WARM_INCREMENTAL",
    "VALID_RETRY_OUTCOMES",
    "VALID_STATES",
    "VALID_TOPOLOGIES",
    "is_budget_exhausted_reason",
]
