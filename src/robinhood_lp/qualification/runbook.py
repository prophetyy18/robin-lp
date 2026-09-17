"""Operator runbook for the reference-dataset qualification (T036).

The runbook states the endpoint routing explicitly so an operator
reproducing the reference-dataset qualification run knows exactly
which endpoint carries each acquisition path:

- the **primary public endpoint** (``robinhood_public``) carries
  the wide pool-filtered scan because it served the 1,000,001-block
  reference range in one ``eth_getLogs`` call on 2026-09-17;
- the **secondary endpoint** (``alchemy_free``) participates only
  within its measured per-call capability (~10 blocks per
  ``eth_getLogs`` call) for sampled cross-validation and for the
  block-pinned StateView read — the secondary endpoint is the one
  that **can** serve historical state at the reference depth while
  the primary cannot.

The runbook is a value object (``OperatorRunbook``) whose
``to_markdown()`` rendering is the audit-trail record stored next
to the qualification report. The same value object is the
machine-readable shape the operator runbook fixture compares
against when re-running qualification, so a downstream reviewer
can confirm the routing is unchanged across runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EndpointRoutingEntry:
    """One row of the operator runbook's endpoint routing table.

    ``role`` names the acquisition path the endpoint carries;
    ``endpoint_alias`` is the short opaque alias the runner
    configures; ``measured_capability`` records the bound the
    endpoint was measured to satisfy on 2026-09-17;
    ``notes`` records the routing decision and any caveats.
    """

    role: str
    endpoint_alias: str
    measured_capability: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "endpoint_alias": self.endpoint_alias,
            "measured_capability": self.measured_capability,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class OperatorRunbook:
    """The pinned operator runbook for the reference-dataset
    qualification run.

    The ``endpoints`` field is the routing table the operator uses
    when wiring the production ``RpcAdapter`` and ``RpcEndpointClient``
    pairs. The ``acquisition_paths`` field describes the acquisition
    shape that produces the qualified dataset. The ``limitations``
    field records the constraints the planning layer measured on
    2026-09-17 and that the operator must respect on re-runs.
    """

    endpoints: tuple[EndpointRoutingEntry, ...]
    acquisition_paths: tuple[str, ...]
    limitations: tuple[str, ...]
    title: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "endpoints": [entry.to_dict() for entry in self.endpoints],
            "acquisition_paths": list(self.acquisition_paths),
            "limitations": list(self.limitations),
        }

    def to_markdown(self) -> str:
        """Render the runbook as a Markdown document for the audit trail."""
        lines: list[str] = []
        lines.append(f"# {self.title}")
        lines.append("")
        lines.append(self.description)
        lines.append("")
        lines.append("## Endpoint routing")
        lines.append("")
        lines.append("| role | endpoint_alias | measured_capability | notes |")
        lines.append("| --- | --- | --- | --- |")
        for entry in self.endpoints:
            notes = entry.notes.replace("|", "\\|")
            lines.append(
                f"| {entry.role} | {entry.endpoint_alias} | {entry.measured_capability} | {notes} |"
            )
        lines.append("")
        lines.append("## Acquisition paths")
        lines.append("")
        for path in self.acquisition_paths:
            lines.append(f"- {path}")
        lines.append("")
        lines.append("## Limitations")
        lines.append("")
        for limitation in self.limitations:
            lines.append(f"- {limitation}")
        lines.append("")
        return "\n".join(lines)


def build_operator_runbook(
    *,
    primary_endpoint_alias: str = "robinhood_public",
    secondary_endpoint_alias: str = "alchemy_free",
) -> OperatorRunbook:
    """Build the pinned operator runbook for the reference-dataset
    qualification run.

    The function returns a deterministic value object whose
    ``to_markdown()`` rendering is the audit-trail record the
    workflow convention records next to the qualification report.
    """
    endpoints: tuple[EndpointRoutingEntry, ...] = (
        EndpointRoutingEntry(
            role="wide_pool_filtered_scan",
            endpoint_alias=primary_endpoint_alias,
            measured_capability=(
                "1,000,001-block pool-filtered eth_getLogs served in one call (2026-09-17)"
            ),
            notes=(
                "Carries the main scan over the pinned reference range; "
                "JSON-RPC batching of eth_getBlockByNumber is supported."
            ),
        ),
        EndpointRoutingEntry(
            role="sampled_cross_validation",
            endpoint_alias=secondary_endpoint_alias,
            measured_capability=("About 10 blocks per eth_getLogs call (2026-09-17)"),
            notes=(
                "Participates only within the measured per-call capability; "
                "the fidelity sampler re-acquires start / middle / end windows "
                "no larger than 10 blocks each. A window this endpoint cannot "
                "serve is a blocked check with reason code "
                "cross_endpoint_sample_missing; it is never silently skipped "
                "and never replaced by a primary re-read."
            ),
        ),
        EndpointRoutingEntry(
            role="block_pinned_state_read",
            endpoint_alias=secondary_endpoint_alias,
            measured_capability=(
                "Archive state available at the reference-range depth; "
                "primary cannot serve historical state at that depth (2026-09-17)"
            ),
            notes=(
                "Carries the StateView.getSlot0 + StateView.getLiquidity "
                "block-pinned read at the range end; an endpoint that "
                "cannot serve the pinned depth records a blocked check "
                "with reason code cross_endpoint_sample_missing; the "
                "qualifier never falls back to latest."
            ),
        ),
    )
    acquisition_paths: tuple[str, ...] = (
        "One pool-filtered eth_getLogs range query per planned sub-range "
        "(primary endpoint, wide pool-filtered scan role).",
        "One non-hydrated eth_getBlockByNumber(hex(n), false) header per "
        "distinct event block, deduplicated, issued as JSON-RPC batches "
        "(either endpoint; both qualified endpoints support batching).",
        "Sampled cross-validation: re-acquire the start / middle / end "
        "windows through the secondary endpoint within its "
        "~10-blocks-per-call measured capability; compare per-EventKey "
        "normalized content hashes against the primary envelope.",
        "Block-pinned StateView spot check at the range end: issue "
        "getSlot0 and getLiquidity at the explicit pinned block tag to "
        "the secondary endpoint and record the golden values; "
        "latest substitution is refused.",
        "No eth_call during acquisition; no block-by-block scans;"
        " no per-block eth_getBlockByNumber; no header for a block "
        "without a pool event.",
    )
    limitations: tuple[str, ...] = (
        "Every value is a mutable observation of a third-party endpoint, "
        "not a contract constant; the planning layer's "
        "docs/spec/protocol/PROVIDER_FACTS.md is the authoritative source.",
        "The measured secondary capability is about 10 blocks per call; "
        "the fidelity sampler must respect this bound or the secondary "
        "endpoint will reject the request.",
        "The primary endpoint cannot serve historical state through "
        "eth_call at the reference-range depth; block-pinned reads "
        "must go to the secondary endpoint.",
        "The secondary endpoint's response budget is the bound on the "
        "fidelity sample size; budget exhaustion is a documented "
        "failure path that forces complete=false.",
        "Rapid sequential calls produce HTTP 429; the runner's "
        "configured User-Agent is the only mitigation, and a "
        "persistent 403 surfaces as the user_agent_rejected reason code.",
    )
    return OperatorRunbook(
        endpoints=endpoints,
        acquisition_paths=acquisition_paths,
        limitations=limitations,
        title="Reference-Dataset Qualification Runbook (T036)",
        description=(
            "This runbook states the endpoint routing for the real-mainnet "
            "ingestion run that produces the pinned reference dataset. The "
            "primary endpoint carries the wide pool-filtered scan; the "
            "secondary endpoint participates within its measured per-call "
            "capability for sampled cross-validation and the block-pinned "
            "StateView read. The measured per-endpoint capability bounds "
            "are the planning inputs that decide this routing."
        ),
    )


#: The pinned operator runbook singleton. The Markdown rendering
#: is the audit-trail record stored next to the qualification
#: report.
OPERATOR_RUNBOOK: OperatorRunbook = build_operator_runbook()


__all__ = [
    "EndpointRoutingEntry",
    "OPERATOR_RUNBOOK",
    "OperatorRunbook",
    "build_operator_runbook",
]
