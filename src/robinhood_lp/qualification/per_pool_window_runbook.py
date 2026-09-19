"""Per-pool operator runbook (T039 / ADR-015).

The T036 runbook documented the endpoint routing for the
reference-dataset qualification run; the T038 runbook extended that
routing for the two-pool ten-million-block acquisition. The T039
runbook is the per-pool runbook the research dataset registry
consumes: it names the endpoint that carries the per-pool wide
scan, the endpoint that serves the per-pool sampled
cross-validation and the block-pinned StateView read at the pinned
finalized end, and the routing decisions that follow from the
T039 acquisition shape.

The runbook's key difference from the T038 two-pool runbook is the
window rule: under T039 / ADR-015 the window is per pool (not
shared), so each pool's wide scan is bounded by ``min(end,
pool_init_block)`` only — there is no shared extension cap and no
``pool_init_outside_window`` exclusion. A pool whose ``Initialize``
lies after the agreed finalized end halts the run rather than
silently widening it.

The runbook is a value object; ``to_markdown()`` is the
audit-trail record the workflow convention stores next to the
per-pool coverage report. The same value object is the
machine-readable shape the operator runbook fixture compares
against when re-running the per-pool acquisition.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from robinhood_lp.qualification.per_pool_coverage import (
    PerPoolCoverageReport,
)
from robinhood_lp.qualification.per_pool_window import (
    PER_POOL_CHAIN_ID,
    PerPoolWindowPlan,
)

#: Default endpoint aliases the per-pool runbook surfaces. The
#: aliases match the T036 / T038 aliases so a re-running operator
#: who already has the ``RpcAdapter`` configured does not have to
#: change anything.
DEFAULT_PRIMARY_ALIAS: Final[str] = "robinhood_public"
DEFAULT_SECONDARY_ALIAS: Final[str] = "alchemy_free"

#: Default measured per-call capability the per-pool runbook
#: surfaces for the primary endpoint (one pool-filtered
#: ``eth_getLogs`` call covered the wider 2026-09-17 measurement).
DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL: Final[int] = 10_000

#: Default measured per-call capability the per-pool runbook
#: surfaces for the secondary endpoint (``alchemy_free``).
DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL: Final[int] = 10

#: Default budget ceiling the per-pool runbook surfaces.
DEFAULT_PER_POOL_BUDGET_LOGICAL_CALLS: Final[int] = 1_000_000
DEFAULT_PER_POOL_BUDGET_HTTP_REQUESTS: Final[int] = 1_000_000
DEFAULT_PER_POOL_BUDGET_RESPONSE_BYTES: Final[int] = 8 * 1024 * 1024 * 1024
DEFAULT_PER_POOL_BUDGET_ROWS: Final[int] = 1_000_000
DEFAULT_PER_POOL_BUDGET_PROVIDER_UNITS: Final[int] = 1_000_000_000
DEFAULT_PER_POOL_BUDGET_ELAPSED_MS: Final[int] = 24 * 60 * 60 * 1000

#: Roles the per-pool runbook distinguishes.
ROLE_WIDE_POOL_FILTERED_SCAN: Final[str] = "wide_pool_filtered_scan"
ROLE_SAMPLED_CROSS_VALIDATION: Final[str] = "sampled_cross_validation"
ROLE_BLOCK_PINNED_STATE_READ: Final[str] = "block_pinned_state_read"

DOCUMENTED_PER_POOL_RUNBOOK_ROLES: Final[frozenset[str]] = frozenset(
    {
        ROLE_WIDE_POOL_FILTERED_SCAN,
        ROLE_SAMPLED_CROSS_VALIDATION,
        ROLE_BLOCK_PINNED_STATE_READ,
    }
)


# ---------------------------------------------------------------------------
# Runbook value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerPoolEndpointRoutingEntry:
    """One row of the per-pool runbook's endpoint routing table.

    ``role`` is one of the documented per-pool runbook roles;
    ``endpoint_alias`` is the short opaque alias the runner
    configures; ``applies_to_pool_key_ids`` records which pool
    keys the routing decision covers (the T039 contract requires
    the routing be per pool so the audit trail never presents one
    pool's agreement as another pool's result).

    ``measured_capability`` records the bound the endpoint was
    measured to satisfy at run time; ``notes`` records the routing
    decision and any caveats the operator must respect.
    """

    role: str
    endpoint_alias: str
    applies_to_pool_key_ids: tuple[str, ...]
    measured_capability: str
    notes: str = ""

    def __post_init__(self) -> None:
        if self.role not in DOCUMENTED_PER_POOL_RUNBOOK_ROLES:
            raise ValueError(
                f"PerPoolEndpointRoutingEntry: role {self.role!r} not in "
                f"DOCUMENTED_PER_POOL_RUNBOOK_ROLES"
            )
        if not self.endpoint_alias:
            raise ValueError(
                "PerPoolEndpointRoutingEntry: endpoint_alias must be a non-empty string"
            )
        if not self.applies_to_pool_key_ids:
            raise ValueError(
                "PerPoolEndpointRoutingEntry: applies_to_pool_key_ids must be non-empty"
            )
        if not self.measured_capability:
            raise ValueError(
                "PerPoolEndpointRoutingEntry: measured_capability must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "endpoint_alias": self.endpoint_alias,
            "applies_to_pool_key_ids": list(self.applies_to_pool_key_ids),
            "measured_capability": self.measured_capability,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class PerPoolRunbookBudget:
    """The per-pool budget ceiling the runbook records.

    The T039 contract requires that every per-pool cost record be
    compared against a declared budget ceiling. The runbook
    records the budget the operator set; the per-pool cost record
    is the actual consumption. The two are stored side by side so
    the audit trail surfaces both.
    """

    logical_calls: int = DEFAULT_PER_POOL_BUDGET_LOGICAL_CALLS
    http_requests: int = DEFAULT_PER_POOL_BUDGET_HTTP_REQUESTS
    response_bytes: int = DEFAULT_PER_POOL_BUDGET_RESPONSE_BYTES
    rows: int = DEFAULT_PER_POOL_BUDGET_ROWS
    provider_units: int = DEFAULT_PER_POOL_BUDGET_PROVIDER_UNITS
    elapsed_ms: int = DEFAULT_PER_POOL_BUDGET_ELAPSED_MS

    def __post_init__(self) -> None:
        if self.logical_calls < 0:
            raise ValueError(
                f"PerPoolRunbookBudget: logical_calls must be >= 0, got {self.logical_calls}"
            )
        if self.http_requests < 0:
            raise ValueError(
                f"PerPoolRunbookBudget: http_requests must be >= 0, got {self.http_requests}"
            )
        if self.response_bytes < 0:
            raise ValueError(
                f"PerPoolRunbookBudget: response_bytes must be >= 0, got {self.response_bytes}"
            )
        if self.rows < 0:
            raise ValueError(f"PerPoolRunbookBudget: rows must be >= 0, got {self.rows}")
        if self.provider_units < 0:
            raise ValueError(
                f"PerPoolRunbookBudget: provider_units must be >= 0, got {self.provider_units}"
            )
        if self.elapsed_ms < 0:
            raise ValueError(
                f"PerPoolRunbookBudget: elapsed_ms must be >= 0, got {self.elapsed_ms}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_calls": self.logical_calls,
            "http_requests": self.http_requests,
            "response_bytes": self.response_bytes,
            "rows": self.rows,
            "provider_units": self.provider_units,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class PerPoolOperatorRunbook:
    """The T039 per-pool operator runbook value object.

    The runbook carries the endpoint routing, the per-pool data
    roots, the per-pool budget ceiling, the acquisition paths the
    operator must follow when re-running the per-pool acquisition
    from a clean environment, and the limitations every operator
    must respect. The ``per_pool_reports`` field carries one
    per-pool coverage report per pool key the research universe
    consumed; ``coverage_plans`` carries the deterministic window
    plans that produced those reports.
    """

    title: str
    description: str
    endpoints: tuple[PerPoolEndpointRoutingEntry, ...]
    acquisition_paths: tuple[str, ...]
    limitations: tuple[str, ...]
    chain_id: int = PER_POOL_CHAIN_ID
    budget: PerPoolRunbookBudget = field(default_factory=PerPoolRunbookBudget)
    coverage_plans: tuple[PerPoolWindowPlan, ...] = ()
    per_pool_reports: tuple[PerPoolCoverageReport, ...] = ()
    historical_state_endpoint_alias: str = DEFAULT_SECONDARY_ALIAS

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("PerPoolOperatorRunbook: title must be a non-empty string")
        if not self.description:
            raise ValueError("PerPoolOperatorRunbook: description must be a non-empty string")
        if not self.historical_state_endpoint_alias:
            raise ValueError(
                "PerPoolOperatorRunbook: historical_state_endpoint_alias must be non-empty"
            )
        if self.chain_id <= 0:
            raise ValueError(f"PerPoolOperatorRunbook: chain_id must be > 0, got {self.chain_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "robinhood_lp.qualification.per_pool_runbook.v1",
            "title": self.title,
            "description": self.description,
            "chain_id": self.chain_id,
            "budget": self.budget.to_dict(),
            "endpoints": [entry.to_dict() for entry in self.endpoints],
            "acquisition_paths": list(self.acquisition_paths),
            "limitations": list(self.limitations),
            "historical_state_endpoint_alias": self.historical_state_endpoint_alias,
            "coverage_plans": [plan.to_dict() for plan in self.coverage_plans],
            "per_pool_reports": [report.to_dict() for report in self.per_pool_reports],
        }

    def to_markdown(self) -> str:
        """Render the per-pool runbook as Markdown for the audit trail."""
        lines: list[str] = []
        lines.append(f"# {self.title}")
        lines.append("")
        lines.append(self.description)
        lines.append("")
        lines.append("## Historical-state endpoint")
        lines.append("")
        lines.append(
            f"- The endpoint that serves historical ``eth_call`` at the agreed "
            f"window depth is **`{self.historical_state_endpoint_alias}`**."
        )
        lines.append("")
        lines.append("## Budget ceiling (per pool)")
        lines.append("")
        lines.append(
            f"- logical_calls: {self.budget.logical_calls:,} | "
            f"http_requests: {self.budget.http_requests:,} | "
            f"response_bytes: {self.budget.response_bytes:,} | "
            f"rows: {self.budget.rows:,} | "
            f"provider_units: {self.budget.provider_units:,} | "
            f"elapsed_ms: {self.budget.elapsed_ms:,}"
        )
        lines.append("")
        lines.append("## Endpoint routing")
        lines.append("")
        lines.append(
            "| role | endpoint_alias | applies_to_pool_key_ids | measured_capability | notes |"
        )
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in self.endpoints:
            notes = entry.notes.replace("|", "\\|")
            ids = ",".join(entry.applies_to_pool_key_ids)
            lines.append(
                f"| {entry.role} | {entry.endpoint_alias} | {ids} | "
                f"{entry.measured_capability} | {notes} |"
            )
        lines.append("")
        lines.append("## Acquisition paths")
        lines.append("")
        for path in self.acquisition_paths:
            lines.append(f"- {path}")
        lines.append("")
        lines.append("## Per-pool coverage plans")
        lines.append("")
        for plan in self.coverage_plans:
            lines.append(
                f"- **{plan.pool_key_id}**: window_pin="
                f"({plan.window_pin_block_number:,}, `{plan.window_pin_block_hash}`) "
                f"pool_init=({plan.pool_init_block:,}, `{plan.pool_init_block_hash}`) "
                f"coverage=[{plan.coverage_from_block:,}, {plan.coverage_to_block:,}] "
                f"window_width={plan.window_width_blocks:,} outcome={plan.outcome} "
                f"reason_code={plan.reason_code} sub_ranges={len(plan.sub_ranges)}"
            )
        lines.append("")
        lines.append("## Per-pool coverage reports")
        lines.append("")
        for report in self.per_pool_reports:
            lines.append(
                f"- **{report.pool_key_id}**: data_root=`{report.data_root.data_root_path}` "
                f"manifest_checksum=`{report.data_root.manifest_checksum}` "
                f"coverage=[{report.coverage_from_block:,}, {report.coverage_to_block:,}] "
                f"outcome={report.coverage_outcome} complete={report.complete} "
                f"blockers={list(report.qualification_blockers)}"
            )
        lines.append("")
        lines.append("## Limitations")
        lines.append("")
        for limitation in self.limitations:
            lines.append(f"- {limitation}")
        lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_per_pool_operator_runbook(
    *,
    chain_id: int = PER_POOL_CHAIN_ID,
    primary_endpoint_alias: str = DEFAULT_PRIMARY_ALIAS,
    secondary_endpoint_alias: str = DEFAULT_SECONDARY_ALIAS,
    historical_state_endpoint_alias: str | None = None,
    primary_max_blocks_per_call: int = DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL,
    secondary_max_blocks_per_call: int = DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL,
    budget: PerPoolRunbookBudget | None = None,
    coverage_plans: tuple[PerPoolWindowPlan, ...] = (),
    per_pool_reports: tuple[PerPoolCoverageReport, ...] = (),
    archive_state_capability_observed: bool = True,
) -> PerPoolOperatorRunbook:
    """Build the per-pool operator runbook.

    The function is the deterministic assembly point: every input
    is either a pinned default, an operator-supplied measurement,
    or a planner / coverage-report output. The resulting value
    object is what the operator uses to reproduce the per-pool
    acquisition from a clean environment; ``to_markdown()`` is the
    audit-trail record the workflow convention stores next to the
    per-pool coverage report.

    ``historical_state_endpoint_alias`` defaults to
    :data:`DEFAULT_SECONDARY_ALIAS` because the secondary endpoint
    is the one that can serve historical ``eth_call`` at the
    pinned window depth (ADR-010 / ADR-011 measured). When the
    primary endpoint is observed to carry that capability for the
    configured pair, the operator may override the default.

    ``archive_state_capability_observed`` records whether the
    operator's run-time capability probe observed the historical
    endpoint actually serving the pinned window depth. A ``False``
    observation is a documented reason code the planner surfaces:
    the per-pool run halts with
    :data:`REASON_ENDPOINT_HISTORICAL_STATE_UNAVAILABLE` rather
    than issue reads that the endpoint will reject.
    """
    if budget is None:
        budget = PerPoolRunbookBudget()
    if historical_state_endpoint_alias is None:
        historical_state_endpoint_alias = secondary_endpoint_alias

    if not archive_state_capability_observed:
        raise ValueError(
            "build_per_pool_operator_runbook: archive_state_capability_observed=False is "
            "a documented failure path; the run must halt with "
            "endpoint_historical_state_unavailable before a runbook is produced"
        )

    pool_key_ids = tuple(plan.pool_key_id for plan in coverage_plans) or tuple(
        report.pool_key_id for report in per_pool_reports
    )

    endpoints: tuple[PerPoolEndpointRoutingEntry, ...] = (
        PerPoolEndpointRoutingEntry(
            role=ROLE_WIDE_POOL_FILTERED_SCAN,
            endpoint_alias=primary_endpoint_alias,
            applies_to_pool_key_ids=pool_key_ids or ("<pool_key_id>",),
            measured_capability=(
                f"{primary_max_blocks_per_call}-block pool-filtered eth_getLogs per call "
                "(2026-09-17 measurement; reused for the wider per-pool window)"
            ),
            notes=(
                "Carries the wide pool-filtered scan over the pinned per-pool window "
                "for every included pool; the JSON-RPC batching of eth_getBlockByNumber "
                "is supported and the header dedup invariant "
                "(`logical_get_block_by_number_calls == distinct event block count`) "
                "holds across the wider range."
            ),
        ),
        PerPoolEndpointRoutingEntry(
            role=ROLE_SAMPLED_CROSS_VALIDATION,
            endpoint_alias=secondary_endpoint_alias,
            applies_to_pool_key_ids=pool_key_ids or ("<pool_key_id>",),
            measured_capability=(
                f"About {secondary_max_blocks_per_call} blocks per eth_getLogs call "
                "(2026-09-17 measurement; reused for the wider per-pool window)"
            ),
            notes=(
                "Re-acquires start / middle / end sampled windows per pool within "
                f"the measured per-call capability ({secondary_max_blocks_per_call} "
                "blocks); a window this endpoint cannot serve is a blocked check "
                "with reason code cross_endpoint_sample_missing — it is never "
                "silently skipped and never replaced by a primary re-read."
            ),
        ),
        PerPoolEndpointRoutingEntry(
            role=ROLE_BLOCK_PINNED_STATE_READ,
            endpoint_alias=historical_state_endpoint_alias,
            applies_to_pool_key_ids=pool_key_ids or ("<pool_key_id>",),
            measured_capability=(
                "Archive state available at the pinned window depth (2026-09-17 measurement; "
                "re-probed at run start)"
            ),
            notes=(
                "Carries the StateView.getSlot0 + StateView.getLiquidity block-pinned "
                "read at the agreed finalized end for every included pool; an endpoint "
                "that cannot serve the pinned depth records a blocked check with "
                "reason code endpoint_historical_state_unavailable; latest "
                "substitution is refused for every pool."
            ),
        ),
    )

    acquisition_paths: tuple[str, ...] = (
        "One acquisition run covers exactly one PoolKey; the research universe "
        "is built by running the acquisition once per pool, never by widening a "
        "single run to several pools.",
        "At run start, read the `finalized` block tag from both qualified "
        "endpoints; the two readings must agree on block number and block hash. "
        "The agreed block is pinned by number and hash into the run record, the "
        "per-pool coverage report and the evidence pack. `latest`, a "
        "non-finalized block and any wall-clock-derived bound are excluded as "
        "the window end, including as a fallback when the tag is unavailable.",
        "Apply the T039 / ADR-015 per-pool window rule: "
        "`coverage_from_block = pool_init_block`; "
        "`coverage_to_block = pinned_finalized_block`. There is no fixed width, "
        "no extension allowance, and no `pool_init_outside_window` exclusion: a "
        "pool whose `Initialize` cannot be located is a failed resolution that "
        "halts the run; a pool whose `Initialize` lies after the agreed end "
        "halts with `pool_initialized_after_agreed_end`.",
        "One pool-filtered `eth_getLogs` range query per planned sub-range "
        "(primary endpoint, wide pool-filtered scan role) for the included "
        "pool; sub-ranges are split under the measured per-call log ceiling "
        "rather than widened to a single call.",
        "One non-hydrated `eth_getBlockByNumber(hex(n), false)` header per "
        "distinct event block, deduplicated, issued as JSON-RPC batches (either "
        "endpoint; both qualified endpoints support batching).",
        "Sampled cross-validation: re-acquire the start / middle / end windows "
        "per pool through the secondary endpoint within its "
        "`secondary_max_blocks_per_call`-blocks-per-call measured capability; "
        "compare per-`EventKey` normalized content hashes against the primary "
        "envelope. A blocked window is a `cross_endpoint_sample_missing` finding, "
        "never skipped, never replaced.",
        "Block-pinned StateView spot check at the pinned finalized end for the "
        "included pool: issue `getSlot0` and `getLiquidity` at the explicit "
        "pinned block tag to the historical-state endpoint and record the "
        "golden values; `latest` substitution is refused.",
        "Per-pool data root: one data root per pool, with its own per-pool T034 "
        "machine report, its own per-pool reconciliation verdict, and its own "
        "per-pool manifest checksum. One pool's agreement is never presented as "
        "another pool's result.",
        "Per-pool cost record: logical calls, HTTP requests, response bytes, "
        "rows, provider units, elapsed time, and parquet bytes, recorded "
        "separately from header reads and compared against the declared budget "
        "ceiling. The run halts rather than exceeding any dimension silently.",
        "Durable checkpoint: the per-pool durable checkpoint advances only "
        "monotonically; a re-run that re-fetches an already-checkpointed range "
        "issues no new chain reads and reproduces byte-equivalent canonical "
        "output for the covered range.",
        "No `eth_call` during acquisition other than the explicitly enumerated "
        "block-pinned verification reads; no per-block `eth_getLogs`; no header "
        "fetch for a block with no pool event; no widen, swap, or substitute "
        "of the pinned reference pool or the second pool.",
    )

    limitations: tuple[str, ...] = (
        "Every endpoint capability value is a mutable observation; "
        "`docs/spec/protocol/PROVIDER_FACTS.md` is the authoritative source and "
        "every run re-probes at its own start.",
        f"The measured primary capability is {primary_max_blocks_per_call} "
        "blocks per pool-filtered `eth_getLogs` call; the planner splits "
        "sub-ranges to fit and surfaces overflows via the documented reason "
        "codes.",
        f"The measured secondary capability is about "
        f"{secondary_max_blocks_per_call} blocks per call; the fidelity "
        "sampler must respect this bound or the secondary endpoint will "
        "reject the request.",
        f"The historical-state endpoint for this run is "
        f"`{historical_state_endpoint_alias}`; an endpoint that cannot serve "
        "the pinned window depth records a blocked check with reason code "
        "endpoint_historical_state_unavailable.",
        "The window end is pinned by finalized-block evidence, never by "
        "`latest`; an unavailable `finalized` tag halts the run with reason "
        "code `finalized_unavailable`; an endpoint disagreement halts with "
        "reason code `finalized_endpoint_disagreement`.",
        "A pool whose hooks is not `0x0` is classified per T023; its support "
        "level does not exceed `ingestion` until a T043-level evidence pack "
        "exists.",
        "The T037 partition reconciliation path applies per pool; "
        "`event_index` and the on-disk Parquet file must agree for every "
        "partition or `complete=true` is refused for the affected pool.",
        "The declared budget ceiling halts the run rather than being exceeded "
        "silently; the cost record surfaces the dimension that reached the "
        "ceiling.",
        "Rapid sequential calls may produce HTTP 429; the configured "
        "`User-Agent` is the only mitigation, and a persistent 403 surfaces "
        "as the `user_agent_rejected` reason code.",
    )

    return PerPoolOperatorRunbook(
        title="Per-Pool Extended-History Acquisition Runbook (T039)",
        description=(
            "This runbook states the endpoint routing for the real-mainnet "
            "per-pool acquisition run that produces the extended-history "
            "research datasets the T039 contract requires. One acquisition run "
            "covers exactly one PoolKey; the window is bounded by "
            "`min(pinned finalized block, pool_init_block)` only — there is no "
            "fixed width and no exclusion. The primary endpoint carries the wide "
            "pool-filtered scan; the secondary endpoint participates within its "
            "measured per-call capability for sampled cross-validation and the "
            "block-pinned StateView read at the agreed finalized end. The "
            "historical-state endpoint named below is the one that serves the "
            "pinned window depth through `eth_call`."
        ),
        endpoints=endpoints,
        acquisition_paths=acquisition_paths,
        limitations=limitations,
        chain_id=chain_id,
        budget=budget,
        coverage_plans=coverage_plans,
        per_pool_reports=per_pool_reports,
        historical_state_endpoint_alias=historical_state_endpoint_alias,
    )


def endpoints_summary(runbook: PerPoolOperatorRunbook) -> Mapping[str, str]:
    """Return the endpoint alias keyed by role for the runbook.

    The helper is the audit-trail surface the per-pool cost
    record's provider-provenance entries consult when they need to
    record which endpoint carried which acquisition role.
    """
    return {entry.role: entry.endpoint_alias for entry in runbook.endpoints}


__all__ = [
    "DEFAULT_PER_POOL_BUDGET_ELAPSED_MS",
    "DEFAULT_PER_POOL_BUDGET_HTTP_REQUESTS",
    "DEFAULT_PER_POOL_BUDGET_LOGICAL_CALLS",
    "DEFAULT_PER_POOL_BUDGET_PROVIDER_UNITS",
    "DEFAULT_PER_POOL_BUDGET_RESPONSE_BYTES",
    "DEFAULT_PER_POOL_BUDGET_ROWS",
    "DEFAULT_PRIMARY_ALIAS",
    "DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL",
    "DEFAULT_SECONDARY_ALIAS",
    "DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL",
    "DOCUMENTED_PER_POOL_RUNBOOK_ROLES",
    "PerPoolEndpointRoutingEntry",
    "PerPoolOperatorRunbook",
    "PerPoolRunbookBudget",
    "ROLE_BLOCK_PINNED_STATE_READ",
    "ROLE_SAMPLED_CROSS_VALIDATION",
    "ROLE_WIDE_POOL_FILTERED_SCAN",
    "build_per_pool_operator_runbook",
    "endpoints_summary",
]
