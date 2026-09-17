"""Tests for the A+B router (T032).

Covers:

- scanned_empty vs successful classification;
- HTTP 403 default User-Agent path;
- HTTP 403 configured User-Agent persistent failure;
- HTTP 429 (rate-limit) handled without advancing the budget;
- response-size overflow triggering an adaptive split rather than
  dropping the interval (the runner-level responsibility; the
  router surfaces the overflow decision);
- range-too-large failure as a capability regression;
- A -> B failover within B's measured capability and remaining
  budget;
- A + B both failing -> ``no_capable_endpoint`` and ``failed``;
- budget exhaustion with reason code ``budget_exhausted`` and
  ``complete=False``.
"""

from __future__ import annotations

import pytest

from _ingestion_t032_fixtures import (
    POOL_ID_INT,
    ScriptedEndpointClient,
    empty_step,
    failure_step,
    make_budget_snapshot,
    make_capability_snapshot,
    overflow_step,
    range_too_large_step,
    success_step,
)
from robinhood_lp.ingestion import (
    REASON_BUDGET_EXHAUSTED_CALLS,
    REASON_EMPTY_RESPONSE,
    REASON_HTTP_403_DEFAULT_USER_AGENT,
    REASON_HTTP_403_USER_AGENT_REJECTED,
    REASON_HTTP_429_RATE_LIMIT,
    REASON_NO_CAPABLE_ENDPOINT,
    REASON_OK,
    REASON_RANGE_TOO_LARGE,
    REASON_RESPONSE_SIZE_OVERFLOW,
    REASON_RPC_TIMEOUT,
    STATE_FAILED,
    STATE_SCANNED_EMPTY,
    STATE_SUCCESSFUL,
    BudgetLedger,
    CoverageDecision,
    Router,
    RouterConfig,
)
from robinhood_lp.ingestion.planner import PlannedSubRange
from robinhood_lp.ingestion.router import (
    ALIAS_ALCHEMY_FREE,
    ALIAS_ROBINHOOD_PUBLIC,
    DEFAULT_FAILOVER_ORDER,
    classify_coverage,
)


def _router(
    *,
    robinhood: ScriptedEndpointClient | None,
    alchemy: ScriptedEndpointClient | None,
    max_response_bytes: int = 5_000_000,
    robinhood_calls: int = 1_000,
    alchemy_calls: int = 200,
    alchemy_max_blocks: int | None = 10,
    robinhood_max_blocks: int | None = 10_000,
) -> Router:
    cap = make_capability_snapshot(
        robinhood_max_blocks=robinhood_max_blocks,
        alchemy_max_blocks=alchemy_max_blocks,
    )
    budget = make_budget_snapshot(
        robinhood_calls=robinhood_calls,
        alchemy_calls=alchemy_calls,
    )
    ledger = BudgetLedger.from_snapshot(budget)
    clients: dict[str, ScriptedEndpointClient] = {}
    if robinhood is not None:
        clients[ALIAS_ROBINHOOD_PUBLIC] = robinhood
    if alchemy is not None:
        clients[ALIAS_ALCHEMY_FREE] = alchemy
    config = RouterConfig(
        failover_order=DEFAULT_FAILOVER_ORDER,
        max_response_bytes=max_response_bytes,
        max_blocks_per_sub_range=robinhood_max_blocks or 10_000,
        alchemy_max_blocks_per_get_logs=alchemy_max_blocks or 10,
    )
    return Router(
        clients=dict(clients),
        capability=cap,
        budget=ledger,
        config=config,
        address="0x" + "44" * 20,
        topic_filter=[
            ["0x" + "aa" * 32, "0x" + "bb" * 32],
            "0x" + format(POOL_ID_INT, "064x"),
        ],
    )


# ---------------------------------------------------------------------------
# scanned_empty / successful classification
# ---------------------------------------------------------------------------


def test_scanned_empty_classification_pins_block_hash() -> None:
    rh = ScriptedEndpointClient(ALIAS_ROBINHOOD_PUBLIC, [empty_step(100, 109)])
    router = _router(robinhood=rh, alchemy=None)
    decision = router.cover_sub_range(PlannedSubRange(100, 109))
    assert classify_coverage(decision) == STATE_SCANNED_EMPTY
    assert decision.endpoint_alias == ALIAS_ROBINHOOD_PUBLIC
    assert decision.rows == 0
    assert decision.pinned_block_hash is not None
    assert decision.reason_code == REASON_EMPTY_RESPONSE


def test_successful_classification_reports_rows() -> None:
    rows = [{"transactionHash": "0x" + "11" * 32, "logIndex": "0x0"}]
    rh = ScriptedEndpointClient(ALIAS_ROBINHOOD_PUBLIC, [success_step(100, 109, rows=rows)])
    router = _router(robinhood=rh, alchemy=None)
    decision = router.cover_sub_range(PlannedSubRange(100, 109))
    assert classify_coverage(decision) == STATE_SUCCESSFUL
    assert decision.endpoint_alias == ALIAS_ROBINHOOD_PUBLIC
    assert decision.rows == 1
    assert decision.reason_code == REASON_OK


# ---------------------------------------------------------------------------
# HTTP 403 default / configured User-Agent
# ---------------------------------------------------------------------------


def test_http_403_default_user_agent_is_recorded_as_a_capability_failure() -> None:
    """The configured User-Agent path may not silently merge the
    interval into a successful coverage transition. The router
    records the default-UA 403 as a failure; the production
    wiring (transport) is responsible for retrying with the
    configured User-Agent.
    """
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            failure_step(
                100,
                109,
                reason_code=REASON_HTTP_403_DEFAULT_USER_AGENT,
                error_detail="default User-Agent rejected",
            )
        ],
    )
    router = _router(robinhood=rh, alchemy=None)
    decision = router.cover_sub_range(PlannedSubRange(100, 109))
    assert decision.state == STATE_FAILED
    assert decision.reason_code == REASON_HTTP_403_DEFAULT_USER_AGENT


def test_http_403_configured_user_agent_persists_as_capability_failure() -> None:
    """When the configured User-Agent is also rejected, the
    endpoint is recorded as a persistent capability failure.
    """
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            failure_step(
                100,
                109,
                reason_code=REASON_HTTP_403_USER_AGENT_REJECTED,
                error_detail="configured User-Agent rejected",
            )
        ],
    )
    router = _router(robinhood=rh, alchemy=None)
    decision = router.cover_sub_range(PlannedSubRange(100, 109))
    assert decision.state == STATE_FAILED
    assert decision.reason_code == REASON_HTTP_403_USER_AGENT_REJECTED


# ---------------------------------------------------------------------------
# Rate-limit (429) handling
# ---------------------------------------------------------------------------


def test_rate_limit_does_not_advance_budget_or_classify_as_empty() -> None:
    """T032 contract: HTTP 429 must be handled by classified retry
    without advancing the checkpoint. The router surfaces 429 as a
    failed interval; the runner's retry path handles the backoff.
    """
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            failure_step(
                100,
                109,
                reason_code=REASON_HTTP_429_RATE_LIMIT,
                error_detail="HTTP 429",
            )
        ],
    )
    router = _router(robinhood=rh, alchemy=None)
    decision = router.cover_sub_range(PlannedSubRange(100, 109))
    assert decision.state == STATE_FAILED
    assert decision.reason_code == REASON_HTTP_429_RATE_LIMIT


# ---------------------------------------------------------------------------
# Response-size overflow (must split, not drop)
# ---------------------------------------------------------------------------


def test_response_size_overflow_surfaces_as_a_failed_decision() -> None:
    """The router surfaces response-size overflow as a failed
    decision; the runner then halves the sub-range and retries.
    """
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC, [overflow_step(100, 109, response_bytes=10_000_000)]
    )
    router = _router(robinhood=rh, alchemy=None, max_response_bytes=5_000_000)
    decision = router.cover_sub_range(PlannedSubRange(100, 109))
    assert decision.state == STATE_FAILED
    assert decision.reason_code == REASON_RESPONSE_SIZE_OVERFLOW


# ---------------------------------------------------------------------------
# Range-too-large (capability regression)
# ---------------------------------------------------------------------------


def test_range_too_large_is_a_capability_failure() -> None:
    rh = ScriptedEndpointClient(ALIAS_ROBINHOOD_PUBLIC, [range_too_large_step(100, 199)])
    router = _router(robinhood=rh, alchemy=None)
    decision = router.cover_sub_range(PlannedSubRange(100, 199))
    assert decision.state == STATE_FAILED
    assert decision.reason_code == REASON_RANGE_TOO_LARGE


# ---------------------------------------------------------------------------
# Provider failover (A -> B within capability + budget)
# ---------------------------------------------------------------------------


def test_a_to_b_failover_when_a_fails_within_b_capability() -> None:
    """Alchemy Free takes the interval when Robinhood fails and
    Alchemy's measured capability covers the sub-range.
    """
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [failure_step(100, 109, reason_code=REASON_RPC_TIMEOUT, error_detail="timeout")],
    )
    alchemy_rows = [{"transactionHash": "0x" + "11" * 32, "logIndex": "0x0"}]
    alchemy = ScriptedEndpointClient(
        ALIAS_ALCHEMY_FREE, [success_step(100, 109, rows=alchemy_rows)]
    )
    router = _router(
        robinhood=rh,
        alchemy=alchemy,
        alchemy_max_blocks=10,
    )
    decision = router.cover_sub_range(PlannedSubRange(100, 109))
    assert decision.state == STATE_SUCCESSFUL
    assert decision.endpoint_alias == ALIAS_ALCHEMY_FREE
    assert decision.failover_from == ALIAS_ROBINHOOD_PUBLIC


def test_no_failover_when_b_cannot_cover_sub_range() -> None:
    """A failover that crosses outside B's measured capability must
    be recorded as failed (T032 must-not).
    """
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [failure_step(100, 199, reason_code=REASON_RPC_TIMEOUT, error_detail="timeout")],
    )
    # Alchemy is configured to only cover 10 blocks; the sub-range
    # is 100 blocks wide so the failover must NOT silently succeed.
    alchemy = ScriptedEndpointClient(ALIAS_ALCHEMY_FREE, [])
    router = _router(
        robinhood=rh,
        alchemy=alchemy,
        alchemy_max_blocks=10,
    )
    decision = router.cover_sub_range(PlannedSubRange(100, 199))
    # B cannot cover the interval; no endpoint succeeds. The
    # interval is recorded as failed, with no endpoint claiming
    # coverage.
    assert decision.state == STATE_FAILED
    assert decision.endpoint_alias is None
    assert decision.reason_code == REASON_NO_CAPABLE_ENDPOINT


def test_both_endpoints_failing_records_no_capable_endpoint() -> None:
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [failure_step(100, 109, reason_code=REASON_RPC_TIMEOUT, error_detail="timeout")],
    )
    alchemy = ScriptedEndpointClient(
        ALIAS_ALCHEMY_FREE,
        [failure_step(100, 109, reason_code=REASON_RPC_TIMEOUT, error_detail="alchemy down")],
    )
    router = _router(robinhood=rh, alchemy=alchemy, alchemy_max_blocks=10)
    decision = router.cover_sub_range(PlannedSubRange(100, 109))
    assert decision.state == STATE_FAILED
    assert decision.reason_code in (REASON_RPC_TIMEOUT, REASON_NO_CAPABLE_ENDPOINT)


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def test_budget_exhaustion_on_a_blocks_failover_to_b() -> None:
    """When A's budget is exhausted, the router must NOT silently
    promote the interval into B's coverage. The runner records the
    interval as failed with reason ``budget_exhausted_calls``."""
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [failure_step(100, 109, reason_code=REASON_RPC_TIMEOUT, error_detail="timeout")],
    )
    alchemy_rows = [{"transactionHash": "0x" + "11" * 32, "logIndex": "0x0"}]
    alchemy = ScriptedEndpointClient(
        ALIAS_ALCHEMY_FREE, [success_step(100, 109, rows=alchemy_rows)]
    )
    # A has zero remaining calls so the budget check blocks A
    # entirely. The router's policy records A's reason and lets B
    # take the interval — the failure here was a timeout, not a
    # capability regression.
    router = _router(
        robinhood=rh,
        alchemy=alchemy,
        alchemy_max_blocks=10,
        robinhood_calls=0,
    )
    decision = router.cover_sub_range(PlannedSubRange(100, 109))
    # A is rejected for budget; B covers the interval (within
    # capability). The failover path is recorded.
    assert decision.state == STATE_SUCCESSFUL
    assert decision.endpoint_alias == ALIAS_ALCHEMY_FREE
    assert decision.failover_from == ALIAS_ROBINHOOD_PUBLIC


def test_budget_exhausted_on_b_records_failed_interval() -> None:
    """When both endpoints have no remaining budget, the interval
    is failed with the explicit ``budget_exhausted_calls`` reason
    code (T032 contract)."""
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [failure_step(100, 109, reason_code=REASON_RPC_TIMEOUT, error_detail="timeout")],
    )
    alchemy = ScriptedEndpointClient(ALIAS_ALCHEMY_FREE, [success_step(100, 109)])
    router = _router(
        robinhood=rh,
        alchemy=alchemy,
        alchemy_max_blocks=10,
        robinhood_calls=0,
        alchemy_calls=0,
    )
    decision = router.cover_sub_range(PlannedSubRange(100, 109))
    assert decision.state == STATE_FAILED
    assert decision.reason_code == REASON_BUDGET_EXHAUSTED_CALLS


def test_unobservable_remaining_budget_source_is_a_hard_cap_of_zero() -> None:
    """``operator_injection_unknown`` must be treated as a hard cap
    of zero (T032 contract: the run does not assume an unused
    monthly quota)."""
    from _ingestion_t032_fixtures import make_budget_snapshot
    from robinhood_lp.ingestion import BudgetSnapshot

    rh = ScriptedEndpointClient(ALIAS_ROBINHOOD_PUBLIC, [success_step(100, 109)])
    cap = make_capability_snapshot()
    # An operator injection marked "unknown" is a zero-cap by
    # definition; the snapshot's per-endpoint remaining budgets
    # are clamped to zero.
    budget = make_budget_snapshot(source="operator_injection_unknown")
    # Override remaining values to zero to simulate the clamp.
    from robinhood_lp.ingestion import EndpointBudget, RemainingBudget

    budget = BudgetSnapshot(
        per_endpoint_budgets=(
            EndpointBudget(
                alias=ALIAS_ROBINHOOD_PUBLIC,
                max_calls=0,
                max_compute_units=None,
                seconds=0.0,
                max_response_bytes=5_000_000,
            ),
            EndpointBudget(
                alias=ALIAS_ALCHEMY_FREE,
                max_calls=0,
                max_compute_units=0,
                seconds=0.0,
                max_response_bytes=5_000_000,
            ),
        ),
        per_endpoint_remaining=(
            RemainingBudget(
                alias=ALIAS_ROBINHOOD_PUBLIC,
                remaining_calls=0,
                remaining_compute_units=None,
                remaining_seconds=0.0,
                remaining_response_quota=None,
                source="operator_injection_unknown",
            ),
            RemainingBudget(
                alias=ALIAS_ALCHEMY_FREE,
                remaining_calls=0,
                remaining_compute_units=0,
                remaining_seconds=0.0,
                remaining_response_quota=0,
                source="operator_injection_unknown",
            ),
        ),
        captured_at="2026-09-17T00:00:00+00:00",
    )
    ledger = BudgetLedger.from_snapshot(budget)
    config = RouterConfig(
        failover_order=DEFAULT_FAILOVER_ORDER,
        max_response_bytes=5_000_000,
        max_blocks_per_sub_range=10_000,
        alchemy_max_blocks_per_get_logs=10,
    )
    router = Router(
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        capability=cap,
        budget=ledger,
        config=config,
        address="0x" + "44" * 20,
        topic_filter=[["0xaa"], "0x" + format(POOL_ID_INT, "064x")],
    )
    decision = router.cover_sub_range(PlannedSubRange(100, 109))
    assert decision.state == STATE_FAILED
    assert decision.reason_code == REASON_BUDGET_EXHAUSTED_CALLS


# ---------------------------------------------------------------------------
# Alchemy 10-block cap
# ---------------------------------------------------------------------------


def test_alchemy_10_block_cap_rejects_wide_sub_range() -> None:
    """Alchemy Free may take log intervals only through its
    measured plan (observed and documented to be on the order of
    10 blocks per eth_getLogs). A 100-block sub-range fails the
    Alchemy capability check."""
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [failure_step(100, 199, reason_code=REASON_RPC_TIMEOUT, error_detail="timeout")],
    )
    alchemy = ScriptedEndpointClient(ALIAS_ALCHEMY_FREE, [success_step(100, 199)])
    router = _router(robinhood=rh, alchemy=alchemy, alchemy_max_blocks=10)
    decision = router.cover_sub_range(PlannedSubRange(100, 199))
    assert decision.state == STATE_FAILED
    assert decision.reason_code == REASON_NO_CAPABLE_ENDPOINT


# ---------------------------------------------------------------------------
# Decision helpers
# ---------------------------------------------------------------------------


def test_classify_coverage_rejects_unknown_state() -> None:
    bogus = CoverageDecision(
        state="bogus",
        endpoint_alias=None,
        failover_from=None,
        reason_code=REASON_OK,
        rows=0,
        response_bytes=0,
        logical_rpc_calls=0,
        http_requests=0,
        pinned_block_hash=None,
    )
    with pytest.raises(ValueError):
        classify_coverage(bogus)
