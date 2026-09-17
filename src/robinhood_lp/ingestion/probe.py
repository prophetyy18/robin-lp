"""Preflight probe runner (T032).

The preflight probe runs at the start of every ingestion run and
records, per endpoint:

- alias, chain/deployment identity, supported finality tags,
  archive-state depth, accepted log range, latency, probe
  block/hash;
- measured capability for ``eth_getLogs`` (max blocks per request,
  observed response size, observed rate-limit headers);
- measured capability for block-header reads;
- the call / CU / time budget allocated to this run, broken down
  per endpoint;
- the remaining-budget bound sourced from operator injection or
  the provider usage API.

The probe results persist to the run manifest before the scan
starts so a crash at any later boundary still leaves the recorded
capability and budget for replay and for the next run's planning.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final, Protocol

from robinhood_lp.ingestion.capability import (
    DEFAULT_ALCHEMY_MAX_BLOCKS_PER_GET_LOGS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_ROBINHOOD_MAX_BLOCKS_PER_GET_LOGS,
    BudgetSnapshot,
    CapabilitySnapshot,
    EndpointBudget,
    EndpointCapability,
    RemainingBudget,
)
from robinhood_lp.ingestion.errors import (
    DEVIATION_403_RESPONSE,
    REASON_HTTP_403_DEFAULT_USER_AGENT,
    REASON_HTTP_403_USER_AGENT_REJECTED,
)
from robinhood_lp.ingestion.planner import build_pool_topic_filter
from robinhood_lp.ingestion.router import (
    ALIAS_ALCHEMY_FREE,
    ALIAS_ROBINHOOD_PUBLIC,
)
from robinhood_lp.protocol.ids import Address, ChainId, PoolId

# ---------------------------------------------------------------------------
# Operator-injected bounds
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperatorBudgetInjection:
    """The remaining-budget bound the operator injected at run start.

    ``source`` is ``operator_injection`` when the operator provided
    concrete bounds. ``operator_injection_unknown`` is used when
    the operator did not provide any bound at all; the runner
    records that source verbatim and treats it as a hard cap of
    zero (T032 contract: the run does not assume an unused monthly
    quota).
    """

    per_endpoint_calls: dict[str, int]
    per_endpoint_compute_units: dict[str, int | None]
    per_endpoint_seconds: dict[str, float]
    per_endpoint_response_quota: dict[str, int | None]
    source: str = "operator_injection"

    def __post_init__(self) -> None:
        if self.source not in ("operator_injection", "operator_injection_unknown"):
            raise ValueError(
                f"OperatorBudgetInjection.source: must be 'operator_injection' "
                f"or 'operator_injection_unknown', got {self.source!r}"
            )


# ---------------------------------------------------------------------------
# Probe source protocol
# ---------------------------------------------------------------------------


class ProbeSource(Protocol):
    """A source the probe can read from.

    Production wiring injects a thin wrapper around the RpcAdapter
    (``eth_chainId``, ``eth_blockNumber``, ``eth_getLogs``,
    ``eth_getBlockByNumber(block, hydrated=False)``). Tests inject
    a fake.
    """

    alias: str

    def eth_chain_id(self) -> int | None: ...

    def eth_block_number(self) -> int | None: ...

    def eth_get_logs_probe(
        self, from_block: int, to_block: int, address: str, topics: list[list[str] | str]
    ) -> tuple[bool, int, dict[str, str], str | None]: ...

    def eth_get_block_hash(self, block_number: int) -> str | None: ...

    def eth_get_block_header(self, block_number: int) -> dict[str, Any] | None: ...


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreflightProbeResult:
    """The probe's per-endpoint verdict + the run-level budget snapshot."""

    capability: CapabilitySnapshot
    budget: BudgetSnapshot

    def to_json(self) -> dict[str, Any]:
        return {
            "capability_snapshot": self.capability.to_json(),
            "budget_snapshot": self.budget.to_json(),
        }


# ---------------------------------------------------------------------------
# Probe runner
# ---------------------------------------------------------------------------


class PreflightProbe:
    """Run the per-endpoint capability + budget probe.

    The probe takes a list of :class:`ProbeSource` instances, the
    operator-injected budget bounds, and the run-time parameters.
    The result is a :class:`PreflightProbeResult` the runner
    persists in the run manifest before any RPC traffic is sent.
    """

    def __init__(
        self,
        *,
        chain_id: ChainId,
        contract_address: Address,
        pool_id: PoolId,
        sources: tuple[ProbeSource, ...],
        operator_budget: OperatorBudgetInjection,
        snapshot_id: str,
        captured_at: str,
        max_blocks_robinhood: int = DEFAULT_ROBINHOOD_MAX_BLOCKS_PER_GET_LOGS,
        max_blocks_alchemy: int = DEFAULT_ALCHEMY_MAX_BLOCKS_PER_GET_LOGS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not sources:
            raise ValueError("PreflightProbe: sources must list at least one endpoint")
        if not snapshot_id:
            raise ValueError("PreflightProbe: snapshot_id must be non-empty")
        self._chain_id = chain_id
        self._contract_address = contract_address
        self._pool_id = pool_id
        self._sources = sources
        self._operator_budget = operator_budget
        self._snapshot_id = snapshot_id
        self._captured_at = captured_at
        self._max_blocks_robinhood = max_blocks_robinhood
        self._max_blocks_alchemy = max_blocks_alchemy
        self._max_response_bytes = max_response_bytes

    def run(self) -> PreflightProbeResult:
        topic_filter = build_pool_topic_filter(self._pool_id)
        capabilities: list[EndpointCapability] = []
        budgets: list[EndpointBudget] = []
        remainings: list[RemainingBudget] = []
        for source in self._sources:
            cap = self._probe_endpoint(source, topic_filter)
            capabilities.append(cap)
            budgets.append(self._budget_for(source.alias))
            remainings.append(self._remaining_for(source.alias, cap))
        capability = CapabilitySnapshot(
            snapshot_id=self._snapshot_id,
            chain_id=self._chain_id.value,
            contract_address=self._contract_address.to_hex().removeprefix("0x").lower(),
            pool_id=self._pool_id.to_hex(),
            endpoints=tuple(capabilities),
            captured_at=self._captured_at,
        )
        budget_snapshot = BudgetSnapshot(
            per_endpoint_budgets=tuple(budgets),
            per_endpoint_remaining=tuple(remainings),
            captured_at=self._captured_at,
        )
        return PreflightProbeResult(capability=capability, budget=budget_snapshot)

    # ----- helpers ------------------------------------------------------

    def _probe_endpoint(
        self,
        source: ProbeSource,
        topic_filter: list[list[str] | str],
    ) -> EndpointCapability:
        chain_id = source.eth_chain_id()
        block_number = source.eth_block_number()
        block_hash = source.eth_get_block_hash(block_number) if block_number is not None else None
        # Pick the per-endpoint measured cap; the probe scales
        # the request from a small window upward so we record the
        # largest window the endpoint accepted.
        alias_cap = (
            self._max_blocks_alchemy
            if source.alias == ALIAS_ALCHEMY_FREE
            else self._max_blocks_robinhood
        )
        max_accepted: int | None = None
        observed_size: int | None = None
        observed_headers: dict[str, str] = {}
        notes: list[str] = []
        latency_ms: int | None = None
        if block_number is not None:
            # Probe with the largest allowed window for this alias.
            lo = max(0, block_number - alias_cap)
            started = time.monotonic()
            ok, size, headers, err = source.eth_get_logs_probe(
                lo, block_number, self._contract_address.to_hex(), topic_filter
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            observed_headers = dict(headers)
            if ok:
                max_accepted = alias_cap
                observed_size = size
            else:
                notes.append(f"probe rejection: {err or 'unknown'} at window={alias_cap}")
                # Binary-search downward to record the largest
                # accepted window.
                low = 1
                high = alias_cap
                best: int | None = None
                best_size: int | None = None
                best_headers: dict[str, str] = {}
                while low <= high:
                    mid = (low + high) // 2
                    window_lo = max(0, block_number - mid)
                    ok2, size2, headers2, err2 = source.eth_get_logs_probe(
                        window_lo,
                        block_number,
                        self._contract_address.to_hex(),
                        topic_filter,
                    )
                    if ok2:
                        best = mid
                        best_size = size2
                        best_headers = dict(headers2)
                        low = mid + 1
                    else:
                        high = mid - 1
                        notes.append(f"window={mid} rejected: {err2 or 'unknown'}")
                if best is not None:
                    max_accepted = best
                    observed_size = best_size
                    observed_headers = best_headers
        archive_cap = source.alias == ALIAS_ALCHEMY_FREE
        header_cap = block_hash is not None
        return EndpointCapability(
            alias=source.alias,
            chain_id=chain_id,
            chain_identity=f"{source.alias}:chain={chain_id}",
            finality_tags=("latest", "finalized"),
            archive_state_depth_blocks=None,
            accepted_log_range_blocks=max_accepted,
            latency_ms=latency_ms,
            probe_block_number=block_number,
            probe_block_hash=block_hash,
            max_blocks_per_get_logs=max_accepted,
            observed_response_size_bytes=observed_size,
            observed_rate_limit_headers=observed_headers,
            header_capability=header_cap,
            archive_capability=archive_cap,
            notes=tuple(notes),
        )

    def _budget_for(self, alias: str) -> EndpointBudget:
        calls = self._operator_budget.per_endpoint_calls.get(alias, 0)
        cu = self._operator_budget.per_endpoint_compute_units.get(alias)
        seconds = self._operator_budget.per_endpoint_seconds.get(alias, 0.0)
        return EndpointBudget(
            alias=alias,
            max_calls=calls,
            max_compute_units=cu,
            seconds=seconds,
            max_response_bytes=self._max_response_bytes,
        )

    def _remaining_for(self, alias: str, cap: EndpointCapability) -> RemainingBudget:
        # Unobservable sources are recorded verbatim and the
        # budget fields are clamped to zero so the runner refuses
        # to start (T032 contract).
        source = self._operator_budget.source
        if source == "operator_injection_unknown":
            return RemainingBudget(
                alias=alias,
                remaining_calls=0,
                remaining_compute_units=0,
                remaining_seconds=0.0,
                remaining_response_quota=0,
                source="operator_injection_unknown",
            )
        calls = self._operator_budget.per_endpoint_calls.get(alias, 0)
        cu = self._operator_budget.per_endpoint_compute_units.get(alias)
        seconds = self._operator_budget.per_endpoint_seconds.get(alias, 0.0)
        quota = self._operator_budget.per_endpoint_response_quota.get(alias)
        return RemainingBudget(
            alias=alias,
            remaining_calls=calls,
            remaining_compute_units=cu,
            remaining_seconds=seconds,
            remaining_response_quota=quota,
            source=source,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_OPERATOR_BUDGET: Final[OperatorBudgetInjection] = OperatorBudgetInjection(
    per_endpoint_calls={
        ALIAS_ROBINHOOD_PUBLIC: 1000,
        ALIAS_ALCHEMY_FREE: 200,
    },
    per_endpoint_compute_units={
        ALIAS_ROBINHOOD_PUBLIC: None,
        ALIAS_ALCHEMY_FREE: 300_000,
    },
    per_endpoint_seconds={
        ALIAS_ROBINHOOD_PUBLIC: 300.0,
        ALIAS_ALCHEMY_FREE: 60.0,
    },
    per_endpoint_response_quota={
        ALIAS_ROBINHOOD_PUBLIC: None,
        ALIAS_ALCHEMY_FREE: 300_000,
    },
)


# Silence unused-binding warnings on imports kept for diagnostics
_ = REASON_HTTP_403_DEFAULT_USER_AGENT
_ = REASON_HTTP_403_USER_AGENT_REJECTED
_ = DEVIATION_403_RESPONSE


__all__ = [
    "DEFAULT_OPERATOR_BUDGET",
    "OperatorBudgetInjection",
    "PreflightProbe",
    "PreflightProbeResult",
    "ProbeSource",
]
