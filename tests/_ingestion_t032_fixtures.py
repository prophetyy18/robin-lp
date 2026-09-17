"""Shared fixtures for the T032 capability-driven ingestion tests.

Constructs a deterministic chain identity, a pool id, a
``CapabilitySnapshot``, a ``BudgetSnapshot``, and an in-memory
``EndpointClient`` fake per alias.

The fixtures deliberately do **not** touch the network: every
endpoint behaviour (success, 403 default User-Agent, 403
configured User-Agent, response-size overflow, rate-limit, range
too large, budget exhaustion, failover) is exercised by a
scripted response on the fake client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from robinhood_lp.ingestion import (
    BudgetSnapshot,
    CapabilitySnapshot,
    EndpointBudget,
    EndpointCallResult,
    EndpointCapability,
    RemainingBudget,
)
from robinhood_lp.ingestion.errors import (
    REASON_HTTP_403_DEFAULT_USER_AGENT,
    REASON_HTTP_403_USER_AGENT_REJECTED,
    REASON_HTTP_429_RATE_LIMIT,
    REASON_OK,
    REASON_RANGE_TOO_LARGE,
    REASON_RESPONSE_SIZE_OVERFLOW,
    REASON_RPC_TIMEOUT,
)
from robinhood_lp.ingestion.router import (
    ALIAS_ALCHEMY_FREE,
    ALIAS_ROBINHOOD_PUBLIC,
)
from robinhood_lp.protocol import Address, ChainId, PoolId

# ---------------------------------------------------------------------------
# Chain identity
# ---------------------------------------------------------------------------

CHAIN_ID: Final[int] = 4663
CHAIN: Final[ChainId] = ChainId(CHAIN_ID)
CONTRACT: Final[Address] = Address.from_hex("0x" + "44" * 20)
POOL_MANAGER: Final[Address] = Address.from_hex("0x" + "55" * 20)
POOL_ID_INT: Final[int] = 0xABCDEF12
POOL_ID: Final[PoolId] = PoolId(POOL_ID_INT)
POOL_INIT_BLOCK: Final[int] = 1_000_000

# ---------------------------------------------------------------------------
# Capability snapshot
# ---------------------------------------------------------------------------


def make_capability_snapshot(
    *,
    snapshot_id: str = "snap-1",
    captured_at: str = "2026-09-17T00:00:00+00:00",
    robinhood_max_blocks: int | None = 10_000,
    alchemy_max_blocks: int | None = 10,
    robinhood_archive: bool = False,
    alchemy_archive: bool = True,
) -> CapabilitySnapshot:
    robinhood = EndpointCapability(
        alias=ALIAS_ROBINHOOD_PUBLIC,
        chain_id=CHAIN_ID,
        chain_identity="robinhood_public:chain=4663",
        finality_tags=("latest", "finalized"),
        archive_state_depth_blocks=None,
        accepted_log_range_blocks=robinhood_max_blocks,
        latency_ms=42,
        probe_block_number=2_000_000,
        probe_block_hash="0x" + "11" * 32,
        max_blocks_per_get_logs=robinhood_max_blocks,
        observed_response_size_bytes=128,
        observed_rate_limit_headers={},
        header_capability=True,
        archive_capability=robinhood_archive,
        notes=(),
    )
    alchemy = EndpointCapability(
        alias=ALIAS_ALCHEMY_FREE,
        chain_id=CHAIN_ID,
        chain_identity="alchemy_free:chain=4663",
        finality_tags=("latest", "finalized"),
        archive_state_depth_blocks=10_000_000,
        accepted_log_range_blocks=alchemy_max_blocks,
        latency_ms=80,
        probe_block_number=2_000_000,
        probe_block_hash="0x" + "22" * 32,
        max_blocks_per_get_logs=alchemy_max_blocks,
        observed_response_size_bytes=512,
        observed_rate_limit_headers={"x-ratelimit-remaining": "100"},
        header_capability=True,
        archive_capability=alchemy_archive,
        notes=(),
    )
    return CapabilitySnapshot(
        snapshot_id=snapshot_id,
        chain_id=CHAIN_ID,
        contract_address=CONTRACT.to_hex().removeprefix("0x").lower(),
        pool_id=POOL_ID.to_hex(),
        endpoints=(robinhood, alchemy),
        captured_at=captured_at,
    )


def make_budget_snapshot(
    *,
    captured_at: str = "2026-09-17T00:00:00+00:00",
    robinhood_calls: int = 1_000,
    alchemy_calls: int = 200,
    robinhood_seconds: float = 300.0,
    alchemy_seconds: float = 60.0,
    source: str = "operator_injection",
) -> BudgetSnapshot:
    budgets = (
        EndpointBudget(
            alias=ALIAS_ROBINHOOD_PUBLIC,
            max_calls=robinhood_calls,
            max_compute_units=None,
            seconds=robinhood_seconds,
            max_response_bytes=5_000_000,
        ),
        EndpointBudget(
            alias=ALIAS_ALCHEMY_FREE,
            max_calls=alchemy_calls,
            max_compute_units=300_000,
            seconds=alchemy_seconds,
            max_response_bytes=5_000_000,
        ),
    )
    remainings = (
        RemainingBudget(
            alias=ALIAS_ROBINHOOD_PUBLIC,
            remaining_calls=robinhood_calls,
            remaining_compute_units=None,
            remaining_seconds=robinhood_seconds,
            remaining_response_quota=None,
            source=source,  # type: ignore[arg-type]
        ),
        RemainingBudget(
            alias=ALIAS_ALCHEMY_FREE,
            remaining_calls=alchemy_calls,
            remaining_compute_units=300_000,
            remaining_seconds=alchemy_seconds,
            remaining_response_quota=300_000,
            source=source,  # type: ignore[arg-type]
        ),
    )
    return BudgetSnapshot(
        per_endpoint_budgets=budgets,
        per_endpoint_remaining=remainings,
        captured_at=captured_at,
    )


# ---------------------------------------------------------------------------
# Scripted fake endpoint client
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ScriptedStep:
    """One step in the scripted response plan."""

    from_block: int
    to_block: int
    success: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    response_bytes: int = 64
    reason_code: str = REASON_OK
    pinned_block_hash: str | None = None
    error_detail: str | None = None


class ScriptedEndpointClient:
    """An :class:`EndpointClient` that returns scripted responses.

    Tests register one ``ScriptedEndpointClient`` per alias with a
    list of :class:`_ScriptedStep` entries. Each ``call_get_logs``
    looks up the step whose ``[from_block, to_block]`` matches the
    request and returns the scripted result. Out-of-script calls
    raise ``AssertionError`` so test bugs surface loudly.
    """

    def __init__(self, alias: str, steps: list[_ScriptedStep] | None = None) -> None:
        self.alias = alias
        self._steps = list(steps or [])
        self.calls: list[tuple[int, int, str, list[list[str] | str]]] = []

    def add_step(self, step: _ScriptedStep) -> None:
        self._steps.append(step)

    def _match(self, from_block: int, to_block: int) -> _ScriptedStep | None:
        for s in self._steps:
            if s.from_block == from_block and s.to_block == to_block:
                return s
        return None

    def call_get_logs(
        self,
        *,
        from_block: int,
        to_block: int,
        address: str,
        topics: list[list[str] | str],
    ) -> EndpointCallResult:
        self.calls.append((from_block, to_block, address, topics))
        step = self._match(from_block, to_block)
        if step is None:
            raise AssertionError(
                f"{self.alias}: unexpected eth_getLogs[{from_block},{to_block}] "
                f"(scripted: {[(s.from_block, s.to_block) for s in self._steps]})"
            )
        return EndpointCallResult(
            success=step.success,
            response_bytes=step.response_bytes,
            rows=list(step.rows),
            reason_code=step.reason_code,
            pinned_block_hash=step.pinned_block_hash,
            error_detail=step.error_detail,
        )

    def get_block_hash_for_pin(self, block_number: int) -> str | None:
        return "0x" + format(block_number, "064x")


def success_step(
    from_block: int, to_block: int, rows: list[dict[str, Any]] | None = None
) -> _ScriptedStep:
    return _ScriptedStep(
        from_block=from_block,
        to_block=to_block,
        success=True,
        rows=list(rows or []),
        response_bytes=128,
        reason_code=REASON_OK,
        pinned_block_hash=None,
    )


def empty_step(from_block: int, to_block: int) -> _ScriptedStep:
    return _ScriptedStep(
        from_block=from_block,
        to_block=to_block,
        success=True,
        rows=[],
        response_bytes=64,
        reason_code=REASON_OK,
        pinned_block_hash="0x" + format(to_block, "064x"),
    )


def failure_step(
    from_block: int,
    to_block: int,
    *,
    reason_code: str,
    error_detail: str | None = None,
    response_bytes: int = 0,
) -> _ScriptedStep:
    return _ScriptedStep(
        from_block=from_block,
        to_block=to_block,
        success=False,
        response_bytes=response_bytes,
        reason_code=reason_code,
        error_detail=error_detail,
    )


def overflow_step(from_block: int, to_block: int, *, response_bytes: int) -> _ScriptedStep:
    """A successful response that overflows the response-size cap.

    Used by the response-size overflow fixture to trigger an
    adaptive split.
    """
    return _ScriptedStep(
        from_block=from_block,
        to_block=to_block,
        success=True,
        rows=[],
        response_bytes=response_bytes,
        reason_code=REASON_RESPONSE_SIZE_OVERFLOW,
        pinned_block_hash=None,
    )


def range_too_large_step(from_block: int, to_block: int) -> _ScriptedStep:
    return failure_step(
        from_block,
        to_block,
        reason_code=REASON_RANGE_TOO_LARGE,
        error_detail="too many results",
    )


# ---------------------------------------------------------------------------
# Topic filter snapshot for tests
# ---------------------------------------------------------------------------

DEFAULT_TOPIC_FILTER: Final[list[list[str] | str]] = [
    ["0x" + "aa" * 32, "0x" + "bb" * 32, "0x" + "cc" * 32],
    "0x" + format(POOL_ID_INT, "064x"),
]


def json_dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "CHAIN",
    "CHAIN_ID",
    "CONTRACT",
    "DEFAULT_TOPIC_FILTER",
    "POOL_ID",
    "POOL_ID_INT",
    "POOL_INIT_BLOCK",
    "POOL_MANAGER",
    "ScriptedEndpointClient",
    "_ScriptedStep",
    "empty_step",
    "failure_step",
    "json_dump",
    "make_budget_snapshot",
    "make_capability_snapshot",
    "overflow_step",
    "range_too_large_step",
    "REASON_HTTP_403_DEFAULT_USER_AGENT",
    "REASON_HTTP_403_USER_AGENT_REJECTED",
    "REASON_HTTP_429_RATE_LIMIT",
    "REASON_RPC_TIMEOUT",
    "success_step",
    "ALIAS_ALCHEMY_FREE",
    "ALIAS_ROBINHOOD_PUBLIC",
]
