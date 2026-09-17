"""Preflight probe results, capability snapshot, and budget ledger (T032).

The capability snapshot is the per-endpoint measurement the preflight
probe records before any RPC traffic is sent. The budget ledger is the
remaining-budget bound (calls / CU / time / response quota) the run
must not exceed. Both are persisted in the run manifest so a crash at
any later boundary still leaves the recorded capability and budget for
replay and for the next run's planning.

A run does **not** assume an unused monthly quota; an unspecified or
unobservable remaining budget is recorded as
``remaining_budget_source = operator_injection_unknown`` or
``remaining_budget_source = provider_usage_api_unavailable`` and is
treated as a hard cap of zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final, Literal

RemainingBudgetSource = Literal[
    "operator_injection",
    "operator_injection_unknown",
    "provider_usage_api",
    "provider_usage_api_unavailable",
]

VALID_REMAINING_BUDGET_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "operator_injection",
        "operator_injection_unknown",
        "provider_usage_api",
        "provider_usage_api_unavailable",
    }
)


def validate_remaining_budget_source(value: str, *, field: str = "remaining_budget_source") -> str:
    """Reject a remaining-budget source outside the allowed vocabulary."""
    if value not in VALID_REMAINING_BUDGET_SOURCES:
        raise ValueError(
            f"{field}: must be one of {sorted(VALID_REMAINING_BUDGET_SOURCES)!r}, got {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Per-endpoint capability snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EndpointCapability:
    """Measured capability of one endpoint as observed by preflight.

    ``max_blocks_per_get_logs`` is the largest block range the
    endpoint accepted in a measured ``eth_getLogs`` call during the
    preflight probe. ``observed_response_size_bytes`` is the response
    size (raw JSON bytes) the largest probe call returned.
    ``observed_rate_limit_headers`` records any rate-limit headers the
    endpoint emitted (when the transport exposes them).
    """

    alias: str
    chain_id: int | None = None
    chain_identity: str = ""
    finality_tags: tuple[str, ...] = ()
    archive_state_depth_blocks: int | None = None
    accepted_log_range_blocks: int | None = None
    latency_ms: int | None = None
    probe_block_number: int | None = None
    probe_block_hash: str | None = None
    max_blocks_per_get_logs: int | None = None
    observed_response_size_bytes: int | None = None
    observed_rate_limit_headers: dict[str, str] = field(default_factory=dict)
    header_capability: bool = False
    archive_capability: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "chain_id": self.chain_id,
            "chain_identity": self.chain_identity,
            "finality_tags": list(self.finality_tags),
            "archive_state_depth_blocks": self.archive_state_depth_blocks,
            "accepted_log_range_blocks": self.accepted_log_range_blocks,
            "latency_ms": self.latency_ms,
            "probe_block_number": self.probe_block_number,
            "probe_block_hash": self.probe_block_hash,
            "max_blocks_per_get_logs": self.max_blocks_per_get_logs,
            "observed_response_size_bytes": self.observed_response_size_bytes,
            "observed_rate_limit_headers": dict(self.observed_rate_limit_headers),
            "header_capability": self.header_capability,
            "archive_capability": self.archive_capability,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EndpointCapability:
        return cls(
            alias=str(data["alias"]),
            chain_id=(int(data["chain_id"]) if data.get("chain_id") is not None else None),
            chain_identity=str(data.get("chain_identity", "")),
            finality_tags=tuple(data.get("finality_tags", ())),
            archive_state_depth_blocks=(
                int(data["archive_state_depth_blocks"])
                if data.get("archive_state_depth_blocks") is not None
                else None
            ),
            accepted_log_range_blocks=(
                int(data["accepted_log_range_blocks"])
                if data.get("accepted_log_range_blocks") is not None
                else None
            ),
            latency_ms=(int(data["latency_ms"]) if data.get("latency_ms") is not None else None),
            probe_block_number=(
                int(data["probe_block_number"])
                if data.get("probe_block_number") is not None
                else None
            ),
            probe_block_hash=(
                str(data["probe_block_hash"]) if data.get("probe_block_hash") is not None else None
            ),
            max_blocks_per_get_logs=(
                int(data["max_blocks_per_get_logs"])
                if data.get("max_blocks_per_get_logs") is not None
                else None
            ),
            observed_response_size_bytes=(
                int(data["observed_response_size_bytes"])
                if data.get("observed_response_size_bytes") is not None
                else None
            ),
            observed_rate_limit_headers=dict(data.get("observed_rate_limit_headers", {})),
            header_capability=bool(data.get("header_capability", False)),
            archive_capability=bool(data.get("archive_capability", False)),
            notes=tuple(data.get("notes", ())),
        )


# ---------------------------------------------------------------------------
# Capability snapshot (the entire preflight probe result)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """The full preflight probe result for one run.

    ``snapshot_id`` is a stable opaque token (UUID-like) that ties
    every later manifest row to the capability the run planned
    against. ``endpoints`` is the ordered tuple of measured
    capabilities, one per probed endpoint.
    """

    snapshot_id: str
    chain_id: int
    contract_address: str
    pool_id: str
    endpoints: tuple[EndpointCapability, ...]
    captured_at: str

    def alias_to_capability(self) -> dict[str, EndpointCapability]:
        return {ep.alias: ep for ep in self.endpoints}

    def to_json(self) -> str:
        return json.dumps(
            {
                "snapshot_id": self.snapshot_id,
                "chain_id": self.chain_id,
                "contract_address": self.contract_address,
                "pool_id": self.pool_id,
                "captured_at": self.captured_at,
                "endpoints": [ep.to_dict() for ep in self.endpoints],
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, blob: str) -> CapabilitySnapshot:
        data = json.loads(blob)
        return cls(
            snapshot_id=str(data["snapshot_id"]),
            chain_id=int(data["chain_id"]),
            contract_address=str(data["contract_address"]),
            pool_id=str(data["pool_id"]),
            captured_at=str(data["captured_at"]),
            endpoints=tuple(EndpointCapability.from_dict(ep) for ep in data.get("endpoints", [])),
        )


# ---------------------------------------------------------------------------
# Per-endpoint budget allocation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EndpointBudget:
    """The measured budget allocated to one endpoint for this run.

    ``max_calls`` is the maximum number of logical RPC calls (one
    ``eth_getLogs`` request counts as one logical call regardless of
    whether HTTP batches were used). ``max_compute_units`` is the
    provider-side CU ceiling when the provider reports one. ``seconds``
    is the wall-clock budget for the run on this endpoint.
    ``max_response_bytes`` is a soft cap that triggers an adaptive
    split when observed (T032 contract: response-size overflow must
    trigger an adaptive split rather than dropping the interval).
    """

    alias: str
    max_calls: int
    max_compute_units: int | None
    seconds: float
    max_response_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.max_calls < 0:
            raise ValueError(f"EndpointBudget.max_calls: must be >= 0, got {self.max_calls}")
        if self.max_compute_units is not None and self.max_compute_units < 0:
            raise ValueError(
                f"EndpointBudget.max_compute_units: must be >= 0, got {self.max_compute_units}"
            )
        if self.seconds < 0:
            raise ValueError(f"EndpointBudget.seconds: must be >= 0, got {self.seconds}")
        if self.max_response_bytes is not None and self.max_response_bytes < 0:
            raise ValueError(
                f"EndpointBudget.max_response_bytes: must be >= 0, got {self.max_response_bytes}"
            )


# ---------------------------------------------------------------------------
# Remaining-budget ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemainingBudget:
    """The remaining-budget bound for one endpoint.

    ``remaining_calls`` is the number of logical RPC calls still
    available. ``remaining_compute_units`` is the provider-side CU
    remaining. ``remaining_seconds`` is the wall-clock time remaining.
    ``remaining_response_quota`` is the optional response-side quota
    (e.g. monthly Alchemy CU) remaining.

    The ``source`` is one of the four ``VALID_REMAINING_BUDGET_SOURCES``
    values. ``operator_injection_unknown`` and
    ``provider_usage_api_unavailable`` are treated as a hard cap of
    zero: the budget fields are clamped to ``0`` and the run is forced
    to halt at the first interval.
    """

    alias: str
    remaining_calls: int
    remaining_compute_units: int | None
    remaining_seconds: float
    remaining_response_quota: int | None
    source: RemainingBudgetSource

    def __post_init__(self) -> None:
        validate_remaining_budget_source(self.source, field="RemainingBudget.source")
        if self.remaining_calls < 0:
            raise ValueError(
                f"RemainingBudget.remaining_calls: must be >= 0, got {self.remaining_calls}"
            )
        if self.remaining_compute_units is not None and self.remaining_compute_units < 0:
            raise ValueError(
                f"RemainingBudget.remaining_compute_units: must be >= 0, got {self.remaining_compute_units}"
            )
        if self.remaining_seconds < 0:
            raise ValueError(
                f"RemainingBudget.remaining_seconds: must be >= 0, got {self.remaining_seconds}"
            )
        if self.remaining_response_quota is not None and self.remaining_response_quota < 0:
            raise ValueError(
                f"RemainingBudget.remaining_response_quota: must be >= 0, got {self.remaining_response_quota}"
            )

    @property
    def is_unobservable(self) -> bool:
        """True when the source is operator_injection_unknown or
        provider_usage_api_unavailable. The runner treats this as a
        hard cap of zero and refuses to start.
        """
        return self.source in ("operator_injection_unknown", "provider_usage_api_unavailable")

    def has_calls(self, n: int = 1) -> bool:
        return self.remaining_calls >= n

    def has_compute_units(self, n: int) -> bool:
        if self.remaining_compute_units is None:
            return True
        return self.remaining_compute_units >= n

    def has_time(self, seconds: float) -> bool:
        return self.remaining_seconds >= seconds

    def has_response_quota(self, n: int = 1) -> bool:
        if self.remaining_response_quota is None:
            return True
        return self.remaining_response_quota >= n


# ---------------------------------------------------------------------------
# Per-run budget ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """The full preflight budget allocation for one run.

    ``per_endpoint_budgets`` and ``per_endpoint_remaining`` are aligned
    by ``alias`` (same ordering as the capability snapshot's
    ``endpoints``).
    """

    per_endpoint_budgets: tuple[EndpointBudget, ...]
    per_endpoint_remaining: tuple[RemainingBudget, ...]
    captured_at: str

    def alias_to_budget(self) -> dict[str, EndpointBudget]:
        return {b.alias: b for b in self.per_endpoint_budgets}

    def alias_to_remaining(self) -> dict[str, RemainingBudget]:
        return {b.alias: b for b in self.per_endpoint_remaining}

    def to_json(self) -> str:
        return json.dumps(
            {
                "captured_at": self.captured_at,
                "per_endpoint_budgets": [
                    {
                        "alias": b.alias,
                        "max_calls": b.max_calls,
                        "max_compute_units": b.max_compute_units,
                        "seconds": b.seconds,
                        "max_response_bytes": b.max_response_bytes,
                    }
                    for b in self.per_endpoint_budgets
                ],
                "per_endpoint_remaining": [
                    {
                        "alias": b.alias,
                        "remaining_calls": b.remaining_calls,
                        "remaining_compute_units": b.remaining_compute_units,
                        "remaining_seconds": b.remaining_seconds,
                        "remaining_response_quota": b.remaining_response_quota,
                        "source": b.source,
                    }
                    for b in self.per_endpoint_remaining
                ],
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, blob: str) -> BudgetSnapshot:
        data = json.loads(blob)
        return cls(
            captured_at=str(data["captured_at"]),
            per_endpoint_budgets=tuple(
                EndpointBudget(
                    alias=str(b["alias"]),
                    max_calls=int(b["max_calls"]),
                    max_compute_units=(
                        int(b["max_compute_units"])
                        if b.get("max_compute_units") is not None
                        else None
                    ),
                    seconds=float(b["seconds"]),
                    max_response_bytes=(
                        int(b["max_response_bytes"])
                        if b.get("max_response_bytes") is not None
                        else None
                    ),
                )
                for b in data.get("per_endpoint_budgets", [])
            ),
            per_endpoint_remaining=tuple(
                RemainingBudget(
                    alias=str(b["alias"]),
                    remaining_calls=int(b["remaining_calls"]),
                    remaining_compute_units=(
                        int(b["remaining_compute_units"])
                        if b.get("remaining_compute_units") is not None
                        else None
                    ),
                    remaining_seconds=float(b["remaining_seconds"]),
                    remaining_response_quota=(
                        int(b["remaining_response_quota"])
                        if b.get("remaining_response_quota") is not None
                        else None
                    ),
                    source=str(b["source"]),  # type: ignore[arg-type]
                )
                for b in data.get("per_endpoint_remaining", [])
            ),
        )


# ---------------------------------------------------------------------------
# Operational defaults (sane defaults for tests + production)
# ---------------------------------------------------------------------------

DEFAULT_MAX_RESPONSE_BYTES: Final[int] = 5_000_000
DEFAULT_ALCHEMY_MAX_BLOCKS_PER_GET_LOGS: Final[int] = 10
DEFAULT_ROBINHOOD_MAX_BLOCKS_PER_GET_LOGS: Final[int] = 10_000


__all__ = [
    "BudgetSnapshot",
    "DEFAULT_ALCHEMY_MAX_BLOCKS_PER_GET_LOGS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_ROBINHOOD_MAX_BLOCKS_PER_GET_LOGS",
    "EndpointBudget",
    "EndpointCapability",
    "CapabilitySnapshot",
    "RemainingBudget",
    "RemainingBudgetSource",
    "VALID_REMAINING_BUDGET_SOURCES",
    "validate_remaining_budget_source",
]
