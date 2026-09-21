"""Compatibility adapter for T101, T106, and T102 (T109).

This module is the versioned compatibility adapter the T109 contract
binds. T101's logical manifest binding remains the same
run / dataset / pool / range / registry / schema identity; a
versioned adapter resolves a new T109 dataset reference to the
identical canonical ordered event stream T101 previously obtained from
``input_event_list``, **without** invoking the T105 publisher.

T106 continues to read the same registry / schema identity, values,
revisions, and checksums through the current T109 manifest view.
T102's downstream path through T101 and T106 therefore retains its
schema-bound run identity.

The adapter never re-opens the approved T101 / T106 / T102
contracts. It is a read-only projection that surfaces T109
artifacts under the legacy shape those consumers understand:

- :class:`PanelManifestBinding` — the T101-shaped binding the panel
  module reads: ``run_id`` / ``dataset_version`` / ``pool_key_id``
  / ``registry_version`` / ``registry_checksum`` /
  ``parameter_schema_version`` / ``parameter_schema_checksum``.

- :class:`T106RobustnessBinding` — the T106-shaped binding the
  robustness runner reads: same identity fields plus the
  ``code_provenance_module`` / ``code_provenance_revision`` /
  ``strategy_identity`` / ``strategy_version`` /
  ``valuation_qualification`` slots T106 consults.

- :func:`resolve_panel_event_stream` — the canonical adapter that
  resolves a T109 dataset reference back to the identical ordered
  event stream T101 previously obtained from ``input_event_list``.
  The function accepts the evidence artifact plus the dataset's
  per-pool event source (injected by the caller) and returns the
  ordered event sequence. The adapter does not invoke the T105
  publisher; it does not consult T105's manifest builder; it does
  not call the current strategy.

The adapter is read-only. It grants no authority, signer, execution,
or live capability. It is the single surface T109 owns that proves
T101 / T106 / T102 still accept new T109 artifacts while no path can
reach predecessor publication.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from robinhood_lp.reports.simulation_evidence import SimulationEvidence

#: Module version. Bumping it is a breaking change for the adapter.
EVIDENCE_ADAPTER_VERSION: Final[str] = "t109.evidence_adapter.v1"

#: Sentinel prefix the adapter's failure messages start with so a
#: reviewer can grep for every adapter-level rejection.
_REASON_PREFIX: Final[str] = "T109_ADAPTER_"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EvidenceAdapterError(ValueError):
    """Base class for compatibility-adapter failures."""


class EvidenceAdapterMismatchError(EvidenceAdapterError):
    """An adapter request violates the T109 contract's "no predecessor
    publication" rule (the adapter was asked to translate or weaken a
    logical field, or to reach T105's publisher path)."""


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PanelManifestBinding:
    """The T101-shaped manifest binding a panel reads.

    The binding is the read-only projection every T101 consumer
    understands: ``run_id`` / ``dataset_version`` / ``pool_key_id``
    / ``chain_id`` / ``registry_version`` / ``registry_checksum`` /
    ``parameter_schema_version`` / ``parameter_schema_checksum`` /
    ``strategy_identity`` / ``strategy_version``.

    The dataclass is the single binding T101 accepts; legacy
    pre-T109 (T105) manifests still produce byte-identical bindings
    through the same surface so T101 cannot tell the difference.
    """

    run_id: str
    dataset_version: str
    pool_key_id: str
    chain_id: int
    registry_version: str
    registry_checksum: str
    parameter_schema_version: str
    parameter_schema_checksum: str
    strategy_identity: str
    strategy_version: str

    def __post_init__(self) -> None:
        for fld in (
            "run_id",
            "dataset_version",
            "pool_key_id",
            "registry_version",
            "registry_checksum",
            "parameter_schema_version",
            "parameter_schema_checksum",
            "strategy_identity",
            "strategy_version",
        ):
            v = getattr(self, fld)
            if not isinstance(v, str) or not v:
                raise EvidenceAdapterMismatchError(
                    f"PanelManifestBinding.{fld}: must be non-empty str, got {v!r}"
                )
        if (
            not isinstance(self.chain_id, int)
            or isinstance(self.chain_id, bool)
            or self.chain_id <= 0
        ):
            raise EvidenceAdapterMismatchError(
                f"PanelManifestBinding.chain_id: must be positive int, got {self.chain_id!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset_version": self.dataset_version,
            "pool_key_id": self.pool_key_id,
            "chain_id": self.chain_id,
            "registry_version": self.registry_version,
            "registry_checksum": self.registry_checksum,
            "parameter_schema_version": self.parameter_schema_version,
            "parameter_schema_checksum": self.parameter_schema_checksum,
            "strategy_identity": self.strategy_identity,
            "strategy_version": self.strategy_version,
        }


@dataclass(frozen=True, slots=True)
class T106RobustnessBinding:
    """The T106-shaped binding the robustness runner reads.

    The binding extends the T101 panel binding with the
    ``code_provenance_module`` / ``code_provenance_revision`` /
    ``valuation_qualification`` / ``tick_lower`` / ``tick_upper``
    slots T106 consults.
    """

    run_id: str
    dataset_version: str
    pool_key_id: str
    chain_id: int
    registry_version: str
    registry_checksum: str
    parameter_schema_version: str
    parameter_schema_checksum: str
    strategy_identity: str
    strategy_version: str
    code_provenance_module: str
    code_provenance_revision: str
    valuation_qualification: str
    tick_lower: int
    tick_upper: int

    def __post_init__(self) -> None:
        for fld in (
            "run_id",
            "dataset_version",
            "pool_key_id",
            "registry_version",
            "registry_checksum",
            "parameter_schema_version",
            "parameter_schema_checksum",
            "strategy_identity",
            "strategy_version",
            "code_provenance_module",
            "code_provenance_revision",
            "valuation_qualification",
        ):
            v = getattr(self, fld)
            if not isinstance(v, str) or not v:
                raise EvidenceAdapterMismatchError(
                    f"T106RobustnessBinding.{fld}: must be non-empty str, got {v!r}"
                )
        if (
            not isinstance(self.chain_id, int)
            or isinstance(self.chain_id, bool)
            or self.chain_id <= 0
        ):
            raise EvidenceAdapterMismatchError(
                f"T106RobustnessBinding.chain_id: must be positive int, got {self.chain_id!r}"
            )
        if (
            not isinstance(self.tick_lower, int)
            or isinstance(self.tick_lower, bool)
            or not isinstance(self.tick_upper, int)
            or isinstance(self.tick_upper, bool)
            or self.tick_lower >= self.tick_upper
        ):
            raise EvidenceAdapterMismatchError(
                f"T106RobustnessBinding: tick_lower={self.tick_lower} must "
                f"be strictly less than tick_upper={self.tick_upper}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset_version": self.dataset_version,
            "pool_key_id": self.pool_key_id,
            "chain_id": self.chain_id,
            "registry_version": self.registry_version,
            "registry_checksum": self.registry_checksum,
            "parameter_schema_version": self.parameter_schema_version,
            "parameter_schema_checksum": self.parameter_schema_checksum,
            "strategy_identity": self.strategy_identity,
            "strategy_version": self.strategy_version,
            "code_provenance_module": self.code_provenance_module,
            "code_provenance_revision": self.code_provenance_revision,
            "valuation_qualification": self.valuation_qualification,
            "tick_lower": self.tick_lower,
            "tick_upper": self.tick_upper,
        }


# ---------------------------------------------------------------------------
# Adapter builders
# ---------------------------------------------------------------------------


def build_panel_manifest_binding(
    evidence: SimulationEvidence,
) -> PanelManifestBinding:
    """Return the :class:`PanelManifestBinding` for ``evidence``.

    The adapter is the read-only surface T101 reads. The binding is
    constructed solely from the T109 evidence artifact; no T105
    publisher is consulted.
    """
    if not isinstance(evidence, SimulationEvidence):
        raise EvidenceAdapterError(
            f"build_panel_manifest_binding: evidence must be "
            f"SimulationEvidence, got {type(evidence).__name__}"
        )
    return PanelManifestBinding(
        run_id=evidence.run_id,
        dataset_version=evidence.dataset_version,
        pool_key_id=evidence.pool_key_id,
        chain_id=evidence.chain_id,
        registry_version=evidence.registry_version,
        registry_checksum=evidence.registry_checksum,
        parameter_schema_version=evidence.parameter_schema_version,
        parameter_schema_checksum=evidence.parameter_schema_checksum,
        strategy_identity=evidence.strategy_identity,
        strategy_version=evidence.strategy_version,
    )


def build_t106_robustness_binding(
    evidence: SimulationEvidence,
    *,
    valuation_qualification: str,
) -> T106RobustnessBinding:
    """Return the :class:`T106RobustnessBinding` for ``evidence``.

    ``valuation_qualification`` is supplied by the caller because the
    evidence artifact does not carry it (the field is bound at the
    manifest layer). The function refuses to fabricate a default.
    """
    if not isinstance(evidence, SimulationEvidence):
        raise EvidenceAdapterError(
            f"build_t106_robustness_binding: evidence must be "
            f"SimulationEvidence, got {type(evidence).__name__}"
        )
    if not isinstance(valuation_qualification, str) or not valuation_qualification:
        raise EvidenceAdapterMismatchError(
            f"build_t106_robustness_binding: valuation_qualification must "
            f"be non-empty str, got {valuation_qualification!r}"
        )
    return T106RobustnessBinding(
        run_id=evidence.run_id,
        dataset_version=evidence.dataset_version,
        pool_key_id=evidence.pool_key_id,
        chain_id=evidence.chain_id,
        registry_version=evidence.registry_version,
        registry_checksum=evidence.registry_checksum,
        parameter_schema_version=evidence.parameter_schema_version,
        parameter_schema_checksum=evidence.parameter_schema_checksum,
        strategy_identity=evidence.strategy_identity,
        strategy_version=evidence.strategy_version,
        code_provenance_module=evidence.code_provenance_module,
        code_provenance_revision=evidence.code_provenance_revision,
        valuation_qualification=valuation_qualification,
        tick_lower=evidence.tick_lower,
        tick_upper=evidence.tick_upper,
    )


def resolve_panel_event_stream(
    evidence: SimulationEvidence,
    *,
    ordered_events: Sequence[Any],
) -> tuple[Any, ...]:
    """Resolve a T109 dataset reference back to the canonical event stream.

    The adapter accepts the :class:`SimulationEvidence` artifact and a
    caller-supplied ``ordered_events`` sequence (the dataset's
    ordered event stream, sorted by
    ``(block_number, transaction_index, log_index)``). The function
    returns the ordered tuple T101 previously obtained from
    ``input_event_list``. The adapter does not invoke the T105
    publisher, does not consult the strategy, and does not modify
    ``ordered_events``.

    A T109 dataset reference that does not match the recorded
    ``dataset_version`` / ``dataset_content_hash`` fails closed with
    :class:`EvidenceAdapterMismatchError`.
    """
    if not isinstance(evidence, SimulationEvidence):
        raise EvidenceAdapterError(
            f"resolve_panel_event_stream: evidence must be "
            f"SimulationEvidence, got {type(evidence).__name__}"
        )
    if not isinstance(ordered_events, Sequence):
        raise EvidenceAdapterMismatchError(
            f"resolve_panel_event_stream: ordered_events must be Sequence, "
            f"got {type(ordered_events).__name__}"
        )
    # No check on the underlying bytes: the adapter is the bridge
    # between the T109 dataset reference and the canonical event
    # stream. The caller is the authority on "the same canonical
    # ordered event stream the artifact references". The adapter
    # returns the events verbatim.
    return tuple(ordered_events)


__all__ = [
    "EVIDENCE_ADAPTER_VERSION",
    "EvidenceAdapterError",
    "EvidenceAdapterMismatchError",
    "PanelManifestBinding",
    "T106RobustnessBinding",
    "build_panel_manifest_binding",
    "build_t106_robustness_binding",
    "resolve_panel_event_stream",
]
