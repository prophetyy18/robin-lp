"""A+B routing, capability-driven failover, and interval classification (T032).

The router decides which endpoint covers a planned sub-range and how
to classify the outcome. The routing policy per interval is:

- try the primary endpoint (Robinhood public RPC) within its measured
  capability and remaining budget;
- if A fails and B (Alchemy Free) has the measured capability **and**
  the remaining budget, B may take the interval;
- if A and B both fail, no endpoint has the measured capability, or
  the remaining budget is exhausted before the interval can be covered,
  the interval is recorded as **failed / uncovered**;
- the routing decision per interval, including the failover path
  used, is persisted in the run manifest.

The router distinguishes the three coverage states
(:func:`classify_coverage`) and writes each to the run manifest's
interval table with explicit reason codes.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from robinhood_lp.ingestion.capability import (
    DEFAULT_ALCHEMY_MAX_BLOCKS_PER_GET_LOGS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_ROBINHOOD_MAX_BLOCKS_PER_GET_LOGS,
    BudgetSnapshot,
    CapabilitySnapshot,
    EndpointCapability,
    RemainingBudget,
)
from robinhood_lp.ingestion.errors import (
    REASON_BUDGET_EXHAUSTED,
    REASON_BUDGET_EXHAUSTED_CALLS,
    REASON_BUDGET_EXHAUSTED_CU,
    REASON_BUDGET_EXHAUSTED_RESPONSE_QUOTA,
    REASON_BUDGET_EXHAUSTED_TIME,
    REASON_CANCELLED_BY_OPERATOR,
    REASON_EMPTY_RESPONSE,
    REASON_HTTP_5XX,
    REASON_HTTP_403_DEFAULT_USER_AGENT,
    REASON_HTTP_403_USER_AGENT_REJECTED,
    REASON_HTTP_429_RATE_LIMIT,
    REASON_NO_CAPABLE_ENDPOINT,
    REASON_OK,
    REASON_RANGE_TOO_LARGE,
    REASON_RESPONSE_SIZE_OVERFLOW,
    REASON_RPC_TIMEOUT,
    REASON_SINGLE_BLOCK_OVERFLOW,
    REASON_SKIPPED_NOT_REQUIRED,
    RETRY_OUTCOME_FAILED,
    RETRY_OUTCOME_GIVE_UP,
    RETRY_OUTCOME_SUCCESS,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_SCANNED_EMPTY,
    STATE_SUCCESSFUL,
    is_budget_exhausted_reason,
)
from robinhood_lp.ingestion.planner import PlannedSubRange

# ---------------------------------------------------------------------------
# Default aliases (resolved through configuration in production).
# ---------------------------------------------------------------------------

ALIAS_ROBINHOOD_PUBLIC: Final[str] = "robinhood_public"
ALIAS_ALCHEMY_FREE: Final[str] = "alchemy_free"
DEFAULT_FAILOVER_ORDER: Final[tuple[str, ...]] = (ALIAS_ROBINHOOD_PUBLIC, ALIAS_ALCHEMY_FREE)


# ---------------------------------------------------------------------------
# Coverage decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageDecision:
    """The router's verdict on one sub-range.

    ``state`` is one of ``scanned_empty``, ``successful``, ``failed``,
    ``cancelled``. ``endpoint_alias`` is the alias of the endpoint
    that produced the outcome (``None`` when no endpoint could cover
    the interval). ``failover_from`` is the alias the router
    attempted first when the winning endpoint was the failover target
    (``None`` when A succeeded outright). ``rows`` is the number of
    canonical events persisted. ``pinned_block_hash`` is the block
    hash pinned to the interval for ``scanned_empty`` evidence.

    ``raw_rows`` is the raw ``eth_getLogs`` rows the winning endpoint
    returned (T035). It is empty for every non-success state. The
    runner feeds it through the block-header source, the decoder, and
    the Parquet writer so the persisted events carry the
    ``block_timestamp`` / ``parent_hash`` evidence the contract
    requires. The field is intentionally immutable (``tuple`` of
    ``dict``) so the router never shares mutable list state with the
    runner.
    """

    state: str
    endpoint_alias: str | None
    failover_from: str | None
    reason_code: str
    rows: int
    response_bytes: int
    logical_rpc_calls: int
    http_requests: int
    pinned_block_hash: str | None
    error_detail: str | None = None
    raw_rows: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# Endpoint probe result / runtime error surfaces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EndpointCallResult:
    """The outcome of one logical ``eth_getLogs`` call against one endpoint."""

    success: bool
    response_bytes: int
    rows: list[dict[str, Any]] = field(default_factory=list)
    reason_code: str = REASON_OK
    pinned_block_hash: str | None = None
    error_detail: str | None = None


# ---------------------------------------------------------------------------
# The endpoint client protocol (testable)
# ---------------------------------------------------------------------------


class EndpointClient(Protocol):
    """One endpoint the router can call.

    The router does not import the RPC adapter directly so the unit
    tests can substitute a fake. ``call_get_logs`` returns the raw
    response rows (possibly empty); ``get_block_hash_for_pin``
    returns the block hash the runner pins to a ``scanned_empty``
    interval for cross-checkable evidence.
    """

    alias: str

    def call_get_logs(
        self,
        *,
        from_block: int,
        to_block: int,
        address: str,
        topics: list[list[str] | str],
    ) -> EndpointCallResult: ...

    def get_block_hash_for_pin(self, block_number: int) -> str | None: ...


# ---------------------------------------------------------------------------
# Mutating budget helper (the runner owns the ledger; the router reads)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BudgetLedger:
    """The mutable view of remaining budgets the router reads and
    decrements.

    The ledger is single-run; a fresh ledger is constructed per
    :class:`BudgetSnapshot`. ``consume_*`` mutators return ``True``
    on success and ``False`` when the budget is exhausted; the caller
    surfaces the failure as a budget-exhausted reason code.
    """

    snapshot: BudgetSnapshot
    _remaining_calls: dict[str, int] = field(default_factory=dict)
    _remaining_cu: dict[str, int | None] = field(default_factory=dict)
    _remaining_seconds: dict[str, float] = field(default_factory=dict)
    _remaining_response_quota: dict[str, int | None] = field(default_factory=dict)
    _sources: dict[str, str] = field(default_factory=dict)
    _started_monotonic: float = field(default_factory=time.monotonic)

    @classmethod
    def from_snapshot(cls, snapshot: BudgetSnapshot) -> BudgetLedger:
        ledger = cls(snapshot=snapshot)
        for rb in snapshot.per_endpoint_remaining:
            ledger._remaining_calls[rb.alias] = rb.remaining_calls
            ledger._remaining_cu[rb.alias] = rb.remaining_compute_units
            ledger._remaining_seconds[rb.alias] = rb.remaining_seconds
            ledger._remaining_response_quota[rb.alias] = rb.remaining_response_quota
            ledger._sources[rb.alias] = rb.source
        return ledger

    def remaining_for(self, alias: str) -> RemainingBudget:
        try:
            return RemainingBudget(
                alias=alias,
                remaining_calls=self._remaining_calls[alias],
                remaining_compute_units=self._remaining_cu[alias],
                remaining_seconds=self._remaining_seconds[alias],
                remaining_response_quota=self._remaining_response_quota[alias],
                source=self._sources[alias],  # type: ignore[arg-type]
            )
        except KeyError as exc:
            raise KeyError(
                f"BudgetLedger: no remaining budget recorded for alias {alias!r}"
            ) from exc

    def has_budget(
        self, alias: str, *, calls: int = 1, seconds: float = 0.0
    ) -> tuple[bool, str | None]:
        """Return ``(ok, reason_code_if_not_ok)``.

        ``reason_code`` is one of the ``REASON_BUDGET_EXHAUSTED_*``
        constants identifying which budget ran out, or ``None`` when
        the budget is sufficient.
        """
        if alias not in self._remaining_calls:
            return False, REASON_NO_CAPABLE_ENDPOINT
        # Unobservable sources are a hard cap of zero (T032 contract).
        if self._sources[alias] in ("operator_injection_unknown", "provider_usage_api_unavailable"):
            return False, REASON_BUDGET_EXHAUSTED_CALLS
        if self._remaining_calls[alias] < calls:
            return False, REASON_BUDGET_EXHAUSTED_CALLS
        cu_left = self._remaining_cu[alias]
        if cu_left is not None and cu_left < calls:
            return False, REASON_BUDGET_EXHAUSTED_CU
        elapsed = time.monotonic() - self._started_monotonic
        if self._remaining_seconds[alias] - elapsed < seconds:
            return False, REASON_BUDGET_EXHAUSTED_TIME
        quota_left = self._remaining_response_quota[alias]
        if quota_left is not None and quota_left < calls:
            return False, REASON_BUDGET_EXHAUSTED_RESPONSE_QUOTA
        return True, None

    def consume(
        self,
        alias: str,
        *,
        calls: int = 1,
        response_bytes: int = 0,
        compute_units: int | None = None,
    ) -> None:
        """Deduct budget after a successful call.

        The runner does not call this on failures; the failed path
        records its reason code without consuming budget (the budget
        was consumed by the failed HTTP request itself).
        """
        self._remaining_calls[alias] = self._remaining_calls[alias] - calls
        cu_left = self._remaining_cu[alias]
        if cu_left is not None and compute_units is not None:
            self._remaining_cu[alias] = cu_left - compute_units
        # response_bytes decrements the optional response quota
        # proportionally when it is set.
        quota = self._remaining_response_quota[alias]
        if quota is not None and response_bytes:
            # We do not over-engineer the conversion: 1 response byte
            # is 1 quota unit. Real providers do not expose this
            # granularity; the value is a placeholder the operator
            # can override.
            self._remaining_response_quota[alias] = max(0, quota - response_bytes)

    def snapshot_remaining(self) -> dict[str, dict[str, Any]]:
        """Return the current remaining budget per alias (for the manifest)."""
        elapsed = time.monotonic() - self._started_monotonic
        out: dict[str, dict[str, Any]] = {}
        for alias in self._remaining_calls:
            out[alias] = {
                "remaining_calls": self._remaining_calls[alias],
                "remaining_compute_units": self._remaining_cu[alias],
                "remaining_seconds": max(0.0, self._remaining_seconds[alias] - elapsed),
                "remaining_response_quota": self._remaining_response_quota[alias],
                "source": self._sources[alias],
            }
        return out


# ---------------------------------------------------------------------------
# Routing policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouterConfig:
    """Operator-tunable knobs for the router.

    ``max_response_bytes`` is the response-size cap that triggers an
    adaptive split rather than dropping the interval (T032 contract).
    ``max_blocks_per_sub_range`` is the per-request cap the runner
    slices sub-ranges to before the first call. The runtime may further
    halve the window when the endpoint returns a range-too-large
    error.
    """

    failover_order: tuple[str, ...] = DEFAULT_FAILOVER_ORDER
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    max_blocks_per_sub_range: int = DEFAULT_ROBINHOOD_MAX_BLOCKS_PER_GET_LOGS
    alchemy_max_blocks_per_get_logs: int = DEFAULT_ALCHEMY_MAX_BLOCKS_PER_GET_LOGS

    def __post_init__(self) -> None:
        if not self.failover_order:
            raise ValueError("RouterConfig.failover_order must list at least one alias")
        if self.max_response_bytes <= 0:
            raise ValueError(
                f"RouterConfig.max_response_bytes: must be positive, got {self.max_response_bytes}"
            )
        if self.max_blocks_per_sub_range <= 0:
            raise ValueError(
                f"RouterConfig.max_blocks_per_sub_range: must be positive, got {self.max_blocks_per_sub_range}"
            )
        if self.alchemy_max_blocks_per_get_logs <= 0:
            raise ValueError(
                f"RouterConfig.alchemy_max_blocks_per_get_logs: must be positive, got {self.alchemy_max_blocks_per_get_logs}"
            )


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Router:
    """The A+B routing engine.

    The router holds the per-endpoint clients, the budget ledger, and
    the capability snapshot. ``cover_sub_range`` is the single entry
    point the runner calls per planned sub-range.
    """

    clients: Mapping[str, EndpointClient]
    capability: CapabilitySnapshot
    budget: BudgetLedger
    config: RouterConfig
    address: str
    topic_filter: list[list[str] | str]
    # Optional deviation recorder (the runner wires this to the manifest).
    on_deviation: Any = None  # Callable[[str, dict[str, Any]], None] | None

    def cover_sub_range(self, sub: PlannedSubRange) -> CoverageDecision:
        """Cover ``sub`` and return the coverage decision.

        The router walks the failover order. Each endpoint is asked to
        cover the sub-range within its measured capability and
        remaining budget. The first endpoint that succeeds with a
        valid (possibly empty) response wins. Otherwise the interval
        is recorded as failed.
        """
        last_error: str | None = None
        first_alias: str | None = None
        failover_from: str | None = None
        for alias in self.config.failover_order:
            if first_alias is None:
                first_alias = alias
            client = self.clients.get(alias)
            if client is None:
                continue
            cap = self.capability.alias_to_capability().get(alias)
            if cap is None:
                continue
            # Capability check: A (Robinhood) uses
            # ``max_blocks_per_sub_range``; B (Alchemy) uses its own
            # measured cap (typically 10). If the sub-range is larger
            # than the endpoint can cover, we split until the window
            # fits. The router does not split here; it surfaces a
            # "no_capable_endpoint" verdict and the runner re-plans
            # the sub-range. This keeps the router single-pass.
            if not self._can_cover(alias, sub, cap):
                self._record_deviation(
                    "no_capable_endpoint",
                    {"alias": alias, "sub_range": [sub.from_block, sub.to_block]},
                )
                last_error = REASON_NO_CAPABLE_ENDPOINT
                continue
            ok, reason = self.budget.has_budget(alias, calls=1)
            if not ok:
                # No remaining budget on this endpoint — try the next.
                last_error = reason or REASON_BUDGET_EXHAUSTED
                continue
            result = client.call_get_logs(
                from_block=sub.from_block,
                to_block=sub.to_block,
                address=self.address,
                topics=self.topic_filter,
            )
            if not result.success:
                last_error = result.reason_code
                self._record_deviation(
                    _deviation_for_reason(result.reason_code),
                    {
                        "alias": alias,
                        "sub_range": [sub.from_block, sub.to_block],
                        "reason_code": result.reason_code,
                        "detail": result.error_detail or "",
                    },
                )
                if first_alias == alias:
                    failover_from = alias
                # Try the next endpoint in the failover order.
                continue
            # Successful response: check size and classify.
            if result.response_bytes > self.config.max_response_bytes:
                # Response-size overflow must trigger an adaptive split
                # rather than dropping the interval. Surface as
                # ``response_size_overflow`` and let the caller split.
                last_error = REASON_RESPONSE_SIZE_OVERFLOW
                self._record_deviation(
                    "response_size_overflow",
                    {
                        "alias": alias,
                        "sub_range": [sub.from_block, sub.to_block],
                        "response_bytes": result.response_bytes,
                        "max_response_bytes": self.config.max_response_bytes,
                    },
                )
                return CoverageDecision(
                    state=STATE_FAILED,
                    endpoint_alias=alias,
                    failover_from=failover_from,
                    reason_code=REASON_RESPONSE_SIZE_OVERFLOW,
                    rows=0,
                    response_bytes=result.response_bytes,
                    logical_rpc_calls=1,
                    http_requests=1,
                    pinned_block_hash=None,
                    error_detail="response exceeded max_response_bytes; split and retry",
                )
            # Success path. Consume the budget and classify.
            self.budget.consume(alias, calls=1, response_bytes=result.response_bytes)
            if first_alias != alias:
                failover_from = first_alias
                self._record_deviation(
                    "provider_failover",
                    {
                        "from_alias": first_alias,
                        "to_alias": alias,
                        "sub_range": [sub.from_block, sub.to_block],
                    },
                )
            if not result.rows:
                # scanned_empty: the response was a valid empty array.
                pinned = result.pinned_block_hash
                if pinned is None:
                    pinned = client.get_block_hash_for_pin(sub.to_block)
                return CoverageDecision(
                    state=STATE_SCANNED_EMPTY,
                    endpoint_alias=alias,
                    failover_from=failover_from,
                    reason_code=REASON_EMPTY_RESPONSE,
                    rows=0,
                    response_bytes=result.response_bytes,
                    logical_rpc_calls=1,
                    http_requests=1,
                    pinned_block_hash=pinned,
                    raw_rows=(),
                )
            return CoverageDecision(
                state=STATE_SUCCESSFUL,
                endpoint_alias=alias,
                failover_from=failover_from,
                reason_code=REASON_OK,
                rows=len(result.rows),
                response_bytes=result.response_bytes,
                logical_rpc_calls=1,
                http_requests=1,
                pinned_block_hash=None,
                raw_rows=tuple(dict(r) for r in result.rows),
            )
        # No endpoint could cover the interval.
        if last_error is None:
            last_error = REASON_NO_CAPABLE_ENDPOINT
        return CoverageDecision(
            state=STATE_FAILED,
            endpoint_alias=None,
            failover_from=failover_from,
            reason_code=last_error,
            rows=0,
            response_bytes=0,
            logical_rpc_calls=0,
            http_requests=0,
            pinned_block_hash=None,
            error_detail=last_error,
        )

    # ----- helpers ------------------------------------------------------

    def _can_cover(self, alias: str, sub: PlannedSubRange, cap: EndpointCapability) -> bool:
        width = sub.to_block - sub.from_block + 1
        if alias == ALIAS_ROBINHOOD_PUBLIC:
            return width <= self.config.max_blocks_per_sub_range and (
                cap.max_blocks_per_get_logs is None or width <= cap.max_blocks_per_get_logs
            )
        if alias == ALIAS_ALCHEMY_FREE:
            alchemy_cap = (
                cap.max_blocks_per_get_logs
                if cap.max_blocks_per_get_logs is not None
                else self.config.alchemy_max_blocks_per_get_logs
            )
            return width <= alchemy_cap
        # Unknown alias: fall back to the planner's cap.
        return cap.max_blocks_per_get_logs is None or width <= cap.max_blocks_per_get_logs

    def _record_deviation(self, kind: str, detail: dict[str, Any]) -> None:
        if self.on_deviation is not None:
            with contextlib.suppress(Exception):
                self.on_deviation(kind, detail)


# ---------------------------------------------------------------------------
# Reason / outcome mapping helpers
# ---------------------------------------------------------------------------


def retry_outcome_for_reason(reason_code: str) -> str:
    """Map a reason code to the retry-ledger outcome to record."""
    if reason_code == REASON_OK:
        return RETRY_OUTCOME_SUCCESS
    if is_budget_exhausted_reason(reason_code):
        return RETRY_OUTCOME_GIVE_UP
    if reason_code == REASON_NO_CAPABLE_ENDPOINT:
        return RETRY_OUTCOME_GIVE_UP
    return RETRY_OUTCOME_FAILED


def _deviation_for_reason(reason_code: str) -> str:
    """Map a failure reason to the deviation kind recorded in the manifest."""
    if reason_code == REASON_HTTP_403_DEFAULT_USER_AGENT:
        return "robinhood_user_agent_403"
    if reason_code == REASON_HTTP_403_USER_AGENT_REJECTED:
        return "robinhood_user_agent_403"
    if reason_code == REASON_HTTP_429_RATE_LIMIT:
        return "http_429_response"
    if reason_code in (REASON_HTTP_5XX,):
        return "http_5xx_response"
    if reason_code == REASON_RESPONSE_SIZE_OVERFLOW:
        return "response_size_overflow"
    if reason_code == REASON_RANGE_TOO_LARGE:
        return "capability_regression"
    if is_budget_exhausted_reason(reason_code):
        return "budget_exhausted"
    if reason_code == REASON_RPC_TIMEOUT:
        return "retry_burst"
    return "capability_regression"


def classify_coverage(decision: CoverageDecision) -> str:
    """Return the canonical state for a router decision.

    Centralised so tests and the runner cannot drift on the spelling.
    """
    if decision.state not in (STATE_SCANNED_EMPTY, STATE_SUCCESSFUL, STATE_FAILED, STATE_CANCELLED):
        raise ValueError(f"classify_coverage: unknown state {decision.state!r}")
    return decision.state


# ---------------------------------------------------------------------------
# Reason -> log-friendly hint (for the retry ledger / diagnostics only)
# ---------------------------------------------------------------------------

_REASON_HINTS: Final[dict[str, str]] = {
    REASON_HTTP_403_DEFAULT_USER_AGENT: "default User-Agent rejected by Robinhood public endpoint",
    REASON_HTTP_403_USER_AGENT_REJECTED: "configured application User-Agent rejected",
    REASON_HTTP_429_RATE_LIMIT: "HTTP 429 rate-limited",
    REASON_RESPONSE_SIZE_OVERFLOW: "response exceeded max_response_bytes; split and retry",
    REASON_SINGLE_BLOCK_OVERFLOW: "single block overflowed; cannot split further",
    REASON_RANGE_TOO_LARGE: "range too large for endpoint's measured cap",
}


def reason_hint(reason_code: str) -> str:
    """Return a short human-readable hint for ``reason_code`` (best effort)."""
    return _REASON_HINTS.get(reason_code, reason_code)


__all__ = [
    "ALIAS_ALCHEMY_FREE",
    "ALIAS_ROBINHOOD_PUBLIC",
    "BudgetLedger",
    "CoverageDecision",
    "DEFAULT_FAILOVER_ORDER",
    "EndpointCallResult",
    "EndpointClient",
    "REASON_CANCELLED_BY_OPERATOR",
    "REASON_SKIPPED_NOT_REQUIRED",
    "Router",
    "RouterConfig",
    "classify_coverage",
    "reason_hint",
    "retry_outcome_for_reason",
]
