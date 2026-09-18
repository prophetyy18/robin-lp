"""Two-pool ten-million-block operator runbook (T038).

The T036 runbook documents the endpoint routing for the
reference-dataset qualification run. The T038 runbook extends that
with the routing decisions the two-pool ten-million-block window
introduces:

- which endpoint carries the **wide pool-filtered scan** for each
  included pool (the same primary endpoint that served the T036
  reference run, because its measured per-call capability on
  2026-09-17 covered the wider range);
- which endpoint serves the **sampled cross-validation** for the
  second pool's newly resolved identity (the secondary endpoint,
  within its measured per-call capability — the new pool is a
  fresh PoolKey whose unknown hook / fees / tick spacing cannot
  widen the secondary's measured cap);
- which endpoint carries the **block-pinned StateView spot check**
  at the pinned window end (the secondary endpoint, whose archive
  capability was measured to cover the reference depth and is
  expected to cover the same depth in this window);
- the **pinned finalized end block** (window end);
- the **resolved second-pool identity** (offline keccak256
  re-derived ``PoolKey`` + ``Initialize`` block);
- the **per-pool data roots** and the per-pool T034
  ``complete=true`` requirement;
- the **measured per-endpoint capability bounds** that ground
  every routing decision.

The runbook is a value object; ``to_markdown()`` is the audit-trail
record the workflow convention stores next to the two-pool T038
report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from robinhood_lp.qualification.two_pool import PerPoolT034Report
from robinhood_lp.qualification.two_pool_window import (
    TwoPoolWindowPlan,
)

#: Default endpoint aliases the runbook surfaces.
DEFAULT_PRIMARY_ALIAS: Final[str] = "robinhood_public"
DEFAULT_SECONDARY_ALIAS: Final[str] = "alchemy_free"

#: Default measured per-call capability the runbook surfaces for the
#: primary endpoint (one pool-filtered ``eth_getLogs`` call covered
#: the wider 2026-09-17 measurement).
DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL: Final[int] = 10_000

#: Default measured per-call capability the runbook surfaces for the
#: secondary endpoint (``alchemy_free``).
DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL: Final[int] = 10


@dataclass(frozen=True, slots=True)
class TwoPoolEndpointRoutingEntry:
    """One row of the two-pool runbook's endpoint routing table.

    Mirrors the T036 :class:`EndpointRoutingEntry` shape but carries
    the per-pool qualifier so a single routing entry may apply to
    one or both pools. ``measured_capability`` records the bound
    the endpoint was measured to satisfy; ``notes`` records the
    routing decision and any caveats the operator must respect.
    """

    role: str
    endpoint_alias: str
    applies_to_pool_aliases: tuple[str, ...]
    measured_capability: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "endpoint_alias": self.endpoint_alias,
            "applies_to_pool_aliases": list(self.applies_to_pool_aliases),
            "measured_capability": self.measured_capability,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class TwoPoolOperatorRunbook:
    """The T038 two-pool operator runbook value object.

    The runbook carries the endpoint routing, the pinned window
    end, the resolved second-pool identity, the per-pool data
    roots, the measured per-endpoint capability bounds and the
    acquisition paths the operator must follow when re-running the
    acquisition from a clean environment.
    """

    title: str
    description: str
    endpoints: tuple[TwoPoolEndpointRoutingEntry, ...]
    acquisition_paths: tuple[str, ...]
    limitations: tuple[str, ...]
    per_pool_reports: tuple[PerPoolT034Report, ...] = ()
    window_plan: TwoPoolWindowPlan | None = None
    second_pool_resolution_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "endpoints": [entry.to_dict() for entry in self.endpoints],
            "acquisition_paths": list(self.acquisition_paths),
            "limitations": list(self.limitations),
            "per_pool_reports": [r.to_dict() for r in self.per_pool_reports],
            "window_plan": (self.window_plan.to_dict() if self.window_plan is not None else None),
            "second_pool_resolution_summary": dict(self.second_pool_resolution_summary),
        }

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append(f"# {self.title}")
        lines.append("")
        lines.append(self.description)
        lines.append("")
        lines.append("## Window pin (pinned finalized end block)")
        lines.append("")
        if self.window_plan is not None:
            pin = self.window_plan.window_pin
            lines.append(f"- Window end block: **{pin.block_number:,}** (`{pin.block_hash}`)")
            lines.append(
                f"- Default start block (end - 10,000,000): "
                f"**{self.window_plan.default_start_block:,}**"
            )
            lines.append(
                f"- Window width (inclusive): **{self.window_plan.window_width_blocks:,}** blocks"
            )
            lines.append(
                f"- Primary endpoint: `{pin.primary_endpoint}`; "
                f"secondary endpoint: `{pin.secondary_endpoint}`"
            )
            lines.append("")
            lines.append("## Per-pool window outcome")
            lines.append("")
            lines.append(
                "| alias | pool_id | pool_init_block | outcome | coverage_from | "
                "extension_below_default | gap_blocks |"
            )
            lines.append("| --- | --- | ---: | --- | ---: | ---: | ---: |")
            for outcome in self.window_plan.pool_outcomes:
                gap_display = f"{outcome.gap_blocks:,}" if outcome.gap_blocks is not None else "—"
                lines.append(
                    f"| {outcome.pool_alias} | `{outcome.pool_id_hex}` | {outcome.pool_init_block:,} | {outcome.outcome} | {outcome.coverage_from_block:,} | {outcome.extension_below_default_start:,} | {gap_display} |"
                )
            lines.append("")
        lines.append("## Endpoint routing")
        lines.append("")
        lines.append(
            "| role | endpoint_alias | applies_to_pool_aliases | measured_capability | notes |"
        )
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in self.endpoints:
            notes = entry.notes.replace("|", "\\|")
            lines.append(
                f"| {entry.role} | {entry.endpoint_alias} | "
                f"{','.join(entry.applies_to_pool_aliases)} | "
                f"{entry.measured_capability} | {notes} |"
            )
        lines.append("")
        lines.append("## Acquisition paths")
        lines.append("")
        for path in self.acquisition_paths:
            lines.append(f"- {path}")
        lines.append("")
        lines.append("## Per-pool T034 reports")
        lines.append("")
        for report in self.per_pool_reports:
            lines.append(
                f"- **{report.pool_alias}** (`{report.pool_id_hex}`): "
                f"data_root=`{report.data_root}` coverage=["
                f"{report.coverage_from_block:,}, {report.coverage_to_block:,}] "
                f"window_outcome={report.window_outcome} "
                f"coverage_outcome={report.coverage_outcome} "
                f"complete={report.complete} "
                f"blockers={list(report.qualification_blockers)}"
            )
        lines.append("")
        if self.second_pool_resolution_summary:
            lines.append("## Second-pool identity resolution")
            lines.append("")
            for key, value in sorted(self.second_pool_resolution_summary.items()):
                lines.append(f"- **{key}**: `{value}`")
            lines.append("")
        lines.append("## Limitations")
        lines.append("")
        for limitation in self.limitations:
            lines.append(f"- {limitation}")
        lines.append("")
        return "\n".join(lines)


def build_two_pool_operator_runbook(
    *,
    primary_endpoint_alias: str = DEFAULT_PRIMARY_ALIAS,
    secondary_endpoint_alias: str = DEFAULT_SECONDARY_ALIAS,
    primary_max_blocks_per_call: int = DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL,
    secondary_max_blocks_per_call: int = DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL,
    per_pool_reports: tuple[PerPoolT034Report, ...] = (),
    window_plan: TwoPoolWindowPlan | None = None,
    second_pool_resolution_summary: dict[str, Any] | None = None,
) -> TwoPoolOperatorRunbook:
    """Build the T038 two-pool operator runbook.

    The function is the deterministic assembly point: every input
    is either a pinned default, an operator-supplied measurement,
    or a T038 report. The resulting value object is what the
    operator uses to reproduce the acquisition from a clean
    environment; ``to_markdown()`` is the audit-trail record.
    """
    endpoints: tuple[TwoPoolEndpointRoutingEntry, ...] = (
        TwoPoolEndpointRoutingEntry(
            role="wide_pool_filtered_scan",
            endpoint_alias=primary_endpoint_alias,
            applies_to_pool_aliases=("reference", "second"),
            measured_capability=(
                f"{primary_max_blocks_per_call}-block pool-filtered eth_getLogs per call "
                "(2026-09-17 measurement; reused for the wider ten-million-block window)"
            ),
            notes=(
                "Carries the wide pool-filtered scan over the pinned window for every "
                "included pool; the JSON-RPC batching of eth_getBlockByNumber is "
                "supported and the header dedup invariant "
                "(`logical_get_block_by_number_calls == distinct event block count`) "
                "holds across the wider range."
            ),
        ),
        TwoPoolEndpointRoutingEntry(
            role="sampled_cross_validation",
            endpoint_alias=secondary_endpoint_alias,
            applies_to_pool_aliases=("reference", "second"),
            measured_capability=(
                f"About {secondary_max_blocks_per_call} blocks per eth_getLogs call "
                "(2026-09-17 measurement; reused for the wider window)"
            ),
            notes=(
                "Re-acquires start / middle / end sampled windows per pool within the "
                "measured per-call capability; a window this endpoint cannot serve is "
                "a blocked check with reason code cross_endpoint_sample_missing — it "
                "is never silently skipped and never replaced by a primary re-read."
            ),
        ),
        TwoPoolEndpointRoutingEntry(
            role="block_pinned_state_read",
            endpoint_alias=secondary_endpoint_alias,
            applies_to_pool_aliases=("reference", "second"),
            measured_capability=(
                "Archive state available at the reference depth and at the wider "
                "ten-million-block end (2026-09-17 measurement)"
            ),
            notes=(
                "Carries the StateView.getSlot0 + StateView.getLiquidity block-pinned "
                "read at the pinned window end for every included pool; an endpoint "
                "that cannot serve the pinned depth records a blocked check with "
                "reason code cross_endpoint_sample_missing; latest substitution is "
                "refused for every pool."
            ),
        ),
    )
    acquisition_paths: tuple[str, ...] = (
        "At run start, read the `finalized` block tag from both qualified "
        "endpoints; the two readings must agree on block number and block hash. "
        "The agreed block is pinned by number and hash into the run manifest, the "
        "T034 report and the evidence pack. `latest`, a non-finalized block and "
        "any wall-clock-derived bound are excluded as the window end, including "
        "as a fallback when the tag is unavailable.",
        "Apply the T038 window rule: `default_start = end - 10_000_000`; for each "
        "included pool `coverage_from = min(default_start, Initialize block)`; "
        "extend below the default only when the gap is within "
        "`TWO_POOL_INIT_EXTENSION_CAP_BLOCKS` (2,000,000). A pool whose Initialize "
        "lies further below is reported as `pool_init_outside_window` and is "
        "excluded from the qualified dataset.",
        "One pool-filtered `eth_getLogs` range query per planned sub-range "
        "(primary endpoint, wide pool-filtered scan role) for each included pool. "
        "Re-acquire the reference pool cold from its Initialize block under the "
        "window rule; the 2026-09-18 dataset is left untouched as immutable "
        "historical evidence.",
        "One non-hydrated `eth_getBlockByNumber(hex(n), false)` header per distinct "
        "event block, deduplicated, issued as JSON-RPC batches (either endpoint; "
        "both qualified endpoints support batching).",
        "Sampled cross-validation: re-acquire the start / middle / end windows "
        "per pool through the secondary endpoint within its "
        "`secondary_max_blocks_per_call`-blocks-per-call measured capability; "
        "compare per-`EventKey` normalized content hashes against the primary "
        "envelope. A blocked window is a `cross_endpoint_sample_missing` finding, "
        "never skipped, never replaced.",
        "Block-pinned StateView spot check at the pinned window end for every "
        "included pool: issue `getSlot0` and `getLiquidity` at the explicit pinned "
        "block tag to the secondary endpoint and record the golden values; "
        "`latest` substitution is refused.",
        "Per-pool data roots: one data root per included pool, each with its own "
        "T034 machine report `complete=true`, its own per-pool reconciliation "
        "agreement, and its own per-pool manifest checksum. The 2026-09-18 dataset "
        "is not merged into or repaired under this runbook.",
        "No `eth_call` during acquisition other than the explicitly enumerated "
        "block-pinned verification reads; no per-block `eth_getBlockByNumber`; "
        "no header fetch for a block with no pool event; logical RPC calls, HTTP "
        "requests, bytes, rows, provider units, storage size and elapsed time are "
        "reported per pool and in total, separately from header reads.",
    )
    limitations: tuple[str, ...] = (
        "Every endpoint capability value is a mutable observation; "
        "`docs/spec/protocol/PROVIDER_FACTS.md` is the authoritative source.",
        "The measured secondary capability is about "
        f"{secondary_max_blocks_per_call} blocks per call; the fidelity sampler "
        "must respect this bound for both included pools or the secondary endpoint "
        "will reject the request.",
        "The primary endpoint's wider pool-filtered scan is bounded by the "
        "measured `primary_max_blocks_per_call`; the planner splits sub-ranges to "
        "fit and surfaces overflows via the documented reason codes.",
        "The window end is pinned by finalized-block evidence, never by `latest`; "
        "an unavailable `finalized` tag halts the run with reason code "
        "`finalized_unavailable`; an endpoint disagreement halts with reason code "
        "`finalized_endpoint_disagreement`.",
        "Rapid sequential calls may produce HTTP 429; the configured `User-Agent` "
        "is the only mitigation, and a persistent 403 surfaces as the "
        "`user_agent_rejected` reason code.",
        "A pool whose hooks is not `0x0` is classified per T023; its support level "
        "does not exceed `ingestion` until a T043-level evidence pack exists.",
        "The T037 partition reconciliation path applies per pool; "
        "`event_index` and the on-disk Parquet file must agree for every "
        "partition or `complete=true` is refused for the affected pool.",
    )
    return TwoPoolOperatorRunbook(
        title="Two-Pool Ten-Million-Block Acquisition Runbook (T038)",
        description=(
            "This runbook states the endpoint routing for the real-mainnet "
            "ingestion run that produces the two-pool ten-million-block qualified "
            "dataset the T038 contract requires. The window end is a pinned "
            "finalized block, the primary endpoint carries the wide pool-filtered "
            "scan, the secondary endpoint participates within its measured "
            "per-call capability for sampled cross-validation and the block-pinned "
            "StateView read, and the per-pool data roots + per-pool T034 machine "
            "reports carry the qualification verdict per pool."
        ),
        endpoints=endpoints,
        acquisition_paths=acquisition_paths,
        limitations=limitations,
        per_pool_reports=per_pool_reports,
        window_plan=window_plan,
        second_pool_resolution_summary=second_pool_resolution_summary or {},
    )


__all__ = [
    "DEFAULT_PRIMARY_ALIAS",
    "DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL",
    "DEFAULT_SECONDARY_ALIAS",
    "DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL",
    "TwoPoolEndpointRoutingEntry",
    "TwoPoolOperatorRunbook",
    "build_two_pool_operator_runbook",
]
