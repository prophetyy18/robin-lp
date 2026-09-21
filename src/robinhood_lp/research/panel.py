"""Panel provenance, member identity and the per-row assembly surface (T101).

The panel module is the storage- / backtest-layer surface that
binds the panel's per-sample provenance together. Three pieces
of provenance travel with every sample:

- **Run identity.** The :class:`robinhood_lp.reports.run_identity.RunIdentity`
  the T069 run record assigned. A panel whose declared run did
  not pass through the T069 lifecycle is refused rather than
  accepted as legacy evidence.
- **Dataset version.** The dataset content hash the T069 run
  bound to. A panel whose dataset is not the validated current
  artifact of the run is refused (per T101's "manifest set that
  does not belong to the declared T069 run" boundary case).
- **Member identity.** Each row carries its
  ``(chain_id, PoolKey)`` plus the dataset revision and source
  checksums the T100 member declaration recorded.
- **Registry revision.** Each row carries the feature-registry
  content hash and the label-schema digest that fixed the
  column / label definitions the row was assembled from. A
  registry drift surfaces here as a content-hash mismatch.

The module also exposes the per-row record
(:class:`PanelProvenanceRow`) the harness consumes to thread
those fields into model training and diagnostic reporting, plus
the panel-provenance record (:class:`PanelProvenance`) that
binds the whole panel to one declared run.

The module is disjoint from execution authority: building a
panel requires no approval, grants no ``HOLD``/``LP`` authority,
and produces no transactions. The module deliberately does not
import RPC, storage, signing, execution, or presentation code;
its only consumer is the model layer (T102) and the research
console (T103).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Final

from robinhood_lp.protocol.ids import PoolId, PoolKey

# The T069 / T105 module import is held to the ``reports`` /
# ``features`` packages, which sit at the same ``backtest`` tier
# per ``tools/check_imports/layer_map.py``. ADR-006 lets ``panel``
# import from a higher-or-equal tier; ``reports`` is at the same
# rank, ``features`` is below. The import is local to ``__post_init__``
# validation paths so the module does not pull reports at
# import time.

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PanelError(ValueError):
    """Base class for panel failures."""


class PanelRunIdentityError(PanelError):
    """The panel's run identity is missing or fails the cross-record checks."""


class PanelMemberIdentityError(PanelError):
    """A panel row's member identity disagrees with the panel's bound member set."""


class PanelDatasetVersionError(PanelError):
    """The panel's dataset version does not match the run's bound dataset."""


class PanelRegistryRevisionError(PanelError):
    """A panel row's registry / schema revision disagrees with the panel's bound revision."""


class PanelLegacyManifestError(PanelError):
    """A legacy pre-registry manifest was offered as current panel input."""


class PanelCancelledRunError(PanelError):
    """The declared run finished in a cancelled state; no panel can be assembled."""


class PanelFailedRunError(PanelError):
    """The declared run finished in a failed state; no panel can be assembled."""


class PanelDuplicateSampleError(PanelError):
    """A sample_id appears twice in the same panel or overlaps an earlier member."""


class PanelUnknownMemberError(PanelError):
    """A panel row carries a member identity not present in the bound member set."""


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


class RunLifecycleState(StrEnum):
    """The lifecycle states the T069 run record surfaces.

    Only ``SUCCEEDED`` admits panel assembly. ``QUEUED``,
    ``RUNNING``, ``FAILED``, ``CANCELLED`` are declined by the
    panel builder with the dedicated error classes.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ---------------------------------------------------------------------------
# Run identity record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PanelRunIdentity:
    """The subset of :class:`RunIdentity` the panel carries.

    The full ``RunIdentity`` carries member-pool tuples and
    dataset content_hash the panel does not need to repeat;
    this record keeps the panel's binding small while still
    proving it traces back to one declared T069 run.

    Fields:

    - ``run_id`` — non-empty string; the identity key.
    - ``dataset_version`` — non-empty string; the dataset
      version the run consumed.
    - ``reporting_numeraire`` — non-empty string; the dataset's
      reporting numeraire.
    - ``valuation_qualification`` — ``"QUALIFIED"`` or
      ``"RELATIVE_ONLY"``; the dataset's qualification.
    - ``lifecycle_state`` — :class:`RunLifecycleState`.
    - ``failure_reason_code`` — non-empty string iff the
      lifecycle is ``FAILED``; the dedicated reason code.
    - ``cancellation_reason_code`` — non-empty string iff the
      lifecycle is ``CANCELLED``; the dedicated reason code.
    """

    run_id: str
    dataset_version: str
    reporting_numeraire: str
    valuation_qualification: str
    lifecycle_state: RunLifecycleState
    failure_reason_code: str = ""
    cancellation_reason_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise PanelError(f"PanelRunIdentity.run_id: must be non-empty str, got {self.run_id!r}")
        if not isinstance(self.dataset_version, str) or not self.dataset_version:
            raise PanelError(
                f"PanelRunIdentity.dataset_version: must be non-empty str, "
                f"got {self.dataset_version!r}"
            )
        if not isinstance(self.reporting_numeraire, str) or not self.reporting_numeraire:
            raise PanelError(
                f"PanelRunIdentity.reporting_numeraire: must be non-empty "
                f"str, got {self.reporting_numeraire!r}"
            )
        if self.valuation_qualification not in {"QUALIFIED", "RELATIVE_ONLY"}:
            raise PanelError(
                f"PanelRunIdentity.valuation_qualification: must be "
                f"'QUALIFIED' or 'RELATIVE_ONLY', got "
                f"{self.valuation_qualification!r}"
            )
        if not isinstance(self.lifecycle_state, RunLifecycleState):
            raise PanelError(
                f"PanelRunIdentity.lifecycle_state: must be "
                f"RunLifecycleState, got {type(self.lifecycle_state).__name__}"
            )
        if not isinstance(self.failure_reason_code, str):
            raise PanelError(
                f"PanelRunIdentity.failure_reason_code: must be str, got "
                f"{type(self.failure_reason_code).__name__}"
            )
        if not isinstance(self.cancellation_reason_code, str):
            raise PanelError(
                f"PanelRunIdentity.cancellation_reason_code: must be str, "
                f"got {type(self.cancellation_reason_code).__name__}"
            )
        if self.lifecycle_state is RunLifecycleState.SUCCEEDED:
            if self.failure_reason_code:
                raise PanelError(
                    f"PanelRunIdentity: SUCCEEDED runs must not carry a "
                    f"failure_reason_code (got {self.failure_reason_code!r})"
                )
            if self.cancellation_reason_code:
                raise PanelError(
                    f"PanelRunIdentity: SUCCEEDED runs must not carry a "
                    f"cancellation_reason_code (got "
                    f"{self.cancellation_reason_code!r})"
                )
        elif self.lifecycle_state is RunLifecycleState.FAILED and not self.failure_reason_code:
            raise PanelError("PanelRunIdentity: FAILED runs must carry a failure_reason_code")
        elif (
            self.lifecycle_state is RunLifecycleState.CANCELLED
            and not self.cancellation_reason_code
        ):
            raise PanelError(
                "PanelRunIdentity: CANCELLED runs must carry a cancellation_reason_code"
            )

    def is_admitted(self) -> bool:
        """``True`` iff the lifecycle admits a panel assembly.

        Only ``SUCCEEDED`` admits assembly. ``QUEUED`` and
        ``RUNNING`` raise the dedicated error class the
        acceptance clause names; ``FAILED`` and ``CANCELLED``
        raise their own error classes so the rejection reason
        is unambiguous.
        """
        return self.lifecycle_state is RunLifecycleState.SUCCEEDED

    def assert_admitted(self) -> None:
        """Raise the dedicated error class if the run is not admitted."""
        if self.lifecycle_state is RunLifecycleState.SUCCEEDED:
            return
        if self.lifecycle_state is RunLifecycleState.QUEUED:
            raise PanelRunIdentityError(
                f"assert_admitted: run {self.run_id!r} is QUEUED; a panel "
                f"can only be assembled from a SUCCEEDED run"
            )
        if self.lifecycle_state is RunLifecycleState.RUNNING:
            raise PanelRunIdentityError(
                f"assert_admitted: run {self.run_id!r} is RUNNING; a panel "
                f"can only be assembled from a SUCCEEDED run"
            )
        if self.lifecycle_state is RunLifecycleState.FAILED:
            raise PanelFailedRunError(
                f"assert_admitted: run {self.run_id!r} FAILED with reason "
                f"{self.failure_reason_code!r}; no panel can be assembled"
            )
        if self.lifecycle_state is RunLifecycleState.CANCELLED:
            raise PanelCancelledRunError(
                f"assert_admitted: run {self.run_id!r} was CANCELLED with "
                f"reason {self.cancellation_reason_code!r}; no panel can "
                f"be assembled"
            )
        # Defensive: unknown lifecycle states are reported as
        # ``PanelRunIdentityError`` rather than silently accepted.
        raise PanelRunIdentityError(
            f"assert_admitted: run {self.run_id!r} has unknown lifecycle "
            f"state {self.lifecycle_state!r}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset_version": self.dataset_version,
            "reporting_numeraire": self.reporting_numeraire,
            "valuation_qualification": self.valuation_qualification,
            "lifecycle_state": self.lifecycle_state.value,
            "failure_reason_code": self.failure_reason_code,
            "cancellation_reason_code": self.cancellation_reason_code,
        }


# ---------------------------------------------------------------------------
# Member identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PanelMemberIdentity:
    """The (chain_id, PoolKey) member the panel row came from.

    The identity is the panel's per-sample binding to the
    underlying dataset member. ``registry_revision`` and
    ``source_checksum`` are the content hashes the member
    declaration the panel was assembled from recorded; a
    re-run validates them against the live T100 member record.
    """

    chain_id: int
    pool_key: PoolKey
    block_range_start: int
    block_range_end: int
    schema_version: int
    decode_version: int
    registry_revision: str
    source_checksum: str

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise PanelError(
                f"PanelMemberIdentity.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise PanelError(f"PanelMemberIdentity.chain_id: must be > 0, got {self.chain_id}")
        if not isinstance(self.pool_key, PoolKey):
            raise PanelError(
                f"PanelMemberIdentity.pool_key: must be PoolKey, got {type(self.pool_key).__name__}"
            )
        if not isinstance(self.block_range_start, int) or isinstance(self.block_range_start, bool):
            raise PanelError(
                f"PanelMemberIdentity.block_range_start: must be int, got "
                f"{type(self.block_range_start).__name__}"
            )
        if self.block_range_start < 0:
            raise PanelError(
                f"PanelMemberIdentity.block_range_start: must be >= 0, got {self.block_range_start}"
            )
        if not isinstance(self.block_range_end, int) or isinstance(self.block_range_end, bool):
            raise PanelError(
                f"PanelMemberIdentity.block_range_end: must be int, got "
                f"{type(self.block_range_end).__name__}"
            )
        if self.block_range_end < self.block_range_start:
            raise PanelError(
                f"PanelMemberIdentity.block_range_end="
                f"{self.block_range_end} must be >= block_range_start="
                f"{self.block_range_start}"
            )
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise PanelError(
                f"PanelMemberIdentity.schema_version: must be int, got "
                f"{type(self.schema_version).__name__}"
            )
        if self.schema_version < 1:
            raise PanelError(
                f"PanelMemberIdentity.schema_version: must be >= 1, got {self.schema_version}"
            )
        if not isinstance(self.decode_version, int) or isinstance(self.decode_version, bool):
            raise PanelError(
                f"PanelMemberIdentity.decode_version: must be int, got "
                f"{type(self.decode_version).__name__}"
            )
        if self.decode_version < 1:
            raise PanelError(
                f"PanelMemberIdentity.decode_version: must be >= 1, got {self.decode_version}"
            )
        if not isinstance(self.registry_revision, str) or not self.registry_revision:
            raise PanelError(
                f"PanelMemberIdentity.registry_revision: must be non-empty "
                f"str, got {self.registry_revision!r}"
            )
        if not isinstance(self.source_checksum, str) or not self.source_checksum:
            raise PanelError(
                f"PanelMemberIdentity.source_checksum: must be non-empty "
                f"str, got {self.source_checksum!r}"
            )

    @property
    def pool_id(self) -> PoolId:
        """The V4 ``PoolId`` derived from the member's ``PoolKey``."""
        return self.pool_key.to_pool_id()

    @property
    def pool_id_hex(self) -> str:
        """The hex string of :attr:`pool_id`."""
        return self.pool_id.to_hex()

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "pool_id": self.pool_id_hex,
            "block_range_start": self.block_range_start,
            "block_range_end": self.block_range_end,
            "schema_version": self.schema_version,
            "decode_version": self.decode_version,
            "registry_revision": self.registry_revision,
            "source_checksum": self.source_checksum,
        }


# ---------------------------------------------------------------------------
# Per-sample panel row
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PanelSampleProvenance:
    """The per-sample provenance a panel row carries.

    Field units:

    - ``sample_id`` — non-empty string; the row's unique key.
      The harness derives the canonical key as
      ``f"{chain_id}|{pool_id_hex}|{decision_time}"`` so
      re-runs agree about deduplication.
    - ``decision_time`` — non-negative integer; the moment the
      panel recorded this sample.
    - ``run_identity`` — :class:`PanelRunIdentity`; the run
      the sample's data came from.
    - ``member_identity`` — :class:`PanelMemberIdentity`; the
      dataset member the sample traces back to.
    - ``registry_revision`` — non-empty string; the feature-
      registry content hash that fixed the column definitions
      this row was assembled from.
    - ``label_schema_digest`` — non-empty string; the digest of
      the label-schema definition the row's labels came from.
    - ``label_horizon_seconds`` — non-negative integer; the
      maximum horizon any of the row's labels spans. Stored on
      the row so a re-run can verify the splice + embargo
      computed at split time.
    """

    sample_id: str
    decision_time: int
    run_identity: PanelRunIdentity
    member_identity: PanelMemberIdentity
    registry_revision: str
    label_schema_digest: str
    label_horizon_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.sample_id, str) or not self.sample_id:
            raise PanelError(
                f"PanelSampleProvenance.sample_id: must be non-empty str, got {self.sample_id!r}"
            )
        if not isinstance(self.decision_time, int) or isinstance(self.decision_time, bool):
            raise PanelError(
                f"PanelSampleProvenance.decision_time: must be int, "
                f"got {type(self.decision_time).__name__}"
            )
        if self.decision_time < 0:
            raise PanelError(
                f"PanelSampleProvenance.decision_time: must be >= 0, got {self.decision_time}"
            )
        if not isinstance(self.run_identity, PanelRunIdentity):
            raise PanelError(
                f"PanelSampleProvenance.run_identity: must be "
                f"PanelRunIdentity, got "
                f"{type(self.run_identity).__name__}"
            )
        if not isinstance(self.member_identity, PanelMemberIdentity):
            raise PanelError(
                f"PanelSampleProvenance.member_identity: must be "
                f"PanelMemberIdentity, got "
                f"{type(self.member_identity).__name__}"
            )
        if not isinstance(self.registry_revision, str) or not self.registry_revision:
            raise PanelError(
                f"PanelSampleProvenance.registry_revision: must be "
                f"non-empty str, got {self.registry_revision!r}"
            )
        if not isinstance(self.label_schema_digest, str) or not self.label_schema_digest:
            raise PanelError(
                f"PanelSampleProvenance.label_schema_digest: must be "
                f"non-empty str, got {self.label_schema_digest!r}"
            )
        if not isinstance(self.label_horizon_seconds, int) or isinstance(
            self.label_horizon_seconds, bool
        ):
            raise PanelError(
                f"PanelSampleProvenance.label_horizon_seconds: must be "
                f"int, got {type(self.label_horizon_seconds).__name__}"
            )
        if self.label_horizon_seconds < 0:
            raise PanelError(
                f"PanelSampleProvenance.label_horizon_seconds: must be "
                f">= 0, got {self.label_horizon_seconds}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "decision_time": self.decision_time,
            "run_identity": self.run_identity.to_dict(),
            "member_identity": self.member_identity.to_dict(),
            "registry_revision": self.registry_revision,
            "label_schema_digest": self.label_schema_digest,
            "label_horizon_seconds": self.label_horizon_seconds,
        }


# ---------------------------------------------------------------------------
# Feature + label rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PanelFeatureRow:
    """The feature columns the model layer consumes.

    Every column entry is an integer (the protocol / features
    layer is integer-clean) or ``None`` (the column is missing
    for this sample). The model's boundary catalogue
    (:class:`robinhood_lp.research.boundary.BoundaryCatalogue`)
    is the only place integers are converted to ``float`` for
    the model layer.

    Two feature rows with the same ``sample_id`` are deduplicated
    by the panel builder; the panel refuses a payload that
    carries two different integer values under the same
    ``(sample_id, column_name)`` pair.
    """

    sample_id: str
    columns: Mapping[str, int | None]

    def __post_init__(self) -> None:
        if not isinstance(self.sample_id, str) or not self.sample_id:
            raise PanelError(
                f"PanelFeatureRow.sample_id: must be non-empty str, got {self.sample_id!r}"
            )
        if not isinstance(self.columns, Mapping):
            raise PanelError(
                f"PanelFeatureRow.columns: must be Mapping, got {type(self.columns).__name__}"
            )
        for key, value in self.columns.items():
            if not isinstance(key, str) or not key:
                raise PanelError(
                    f"PanelFeatureRow.columns: every key must be non-empty str, got {key!r}"
                )
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise PanelError(
                    f"PanelFeatureRow.columns: every value must be int or "
                    f"None, got {key!r} -> {type(value).__name__}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {"sample_id": self.sample_id, "columns": dict(self.columns)}


@dataclass(frozen=True, slots=True)
class PanelLabelRow:
    """The forward labels the model layer consumes.

    Labels follow the same integer / None convention as
    :class:`PanelFeatureRow`. ``None`` means "the label is not
    observable for this sample" (the sample's decision time is
    too close to the dataset's end, for example).
    """

    sample_id: str
    columns: Mapping[str, int | None]

    def __post_init__(self) -> None:
        if not isinstance(self.sample_id, str) or not self.sample_id:
            raise PanelError(
                f"PanelLabelRow.sample_id: must be non-empty str, got {self.sample_id!r}"
            )
        if not isinstance(self.columns, Mapping):
            raise PanelError(
                f"PanelLabelRow.columns: must be Mapping, got {type(self.columns).__name__}"
            )
        for key, value in self.columns.items():
            if not isinstance(key, str) or not key:
                raise PanelError(
                    f"PanelLabelRow.columns: every key must be non-empty str, got {key!r}"
                )
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise PanelError(
                    f"PanelLabelRow.columns: every value must be int or "
                    f"None, got {key!r} -> {type(value).__name__}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {"sample_id": self.sample_id, "columns": dict(self.columns)}


# ---------------------------------------------------------------------------
# Panel (the unit the harness consumes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PanelProvenance:
    """The panel-level binding for a model run.

    The record carries:

    - ``version`` — the panel schema version the panel was
      built with;
    - ``run_identity`` — the T069 run the panel binds to;
    - ``member_identities`` — the bound ``(chain_id, PoolKey)``
      identities; every panel row's :attr:`PanelSampleProvenance.member_identity`
      must be present here;
    - ``registry_revision`` — the feature registry content hash;
    - ``label_schema_digest`` — the label-schema digest;
    - ``content_hash`` — SHA-256 hex digest of the canonical
      representation of the panel's binding.

    A :class:`PanelProvenance` admits only a run whose lifecycle
    is ``SUCCEEDED``. Building a panel from a legacy
    pre-registry manifest raises :class:`PanelLegacyManifestError`.
    """

    #: Schema version. Bumping is a breaking change.
    VERSION: ClassVar[str] = "t101.panel_provenance.v1"

    run_identity: PanelRunIdentity
    member_identities: tuple[PanelMemberIdentity, ...]
    registry_revision: str
    label_schema_digest: str
    declared_label_horizons: tuple[int, ...]
    content_hash: str
    version: str = VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise PanelError(
                f"PanelProvenance.version: must be non-empty str, got {self.version!r}"
            )
        if self.version != self.VERSION:
            raise PanelError(
                f"PanelProvenance.version: must be {self.VERSION!r}, got {self.version!r}"
            )
        if not isinstance(self.run_identity, PanelRunIdentity):
            raise PanelError(
                f"PanelProvenance.run_identity: must be PanelRunIdentity, "
                f"got {type(self.run_identity).__name__}"
            )
        if not isinstance(self.member_identities, tuple):
            raise PanelError(
                f"PanelProvenance.member_identities: must be tuple, got "
                f"{type(self.member_identities).__name__}"
            )
        if not self.member_identities:
            raise PanelError("PanelProvenance.member_identities: must be non-empty")
        seen_pool_ids: set[str] = set()
        for member in self.member_identities:
            if not isinstance(member, PanelMemberIdentity):
                raise PanelError(
                    f"PanelProvenance.member_identities: every entry must "
                    f"be PanelMemberIdentity, got {type(member).__name__}"
                )
            if member.pool_id_hex in seen_pool_ids:
                raise PanelError(
                    f"PanelProvenance.member_identities: duplicate pool_id {member.pool_id_hex!r}"
                )
            seen_pool_ids.add(member.pool_id_hex)
        if not isinstance(self.registry_revision, str) or not self.registry_revision:
            raise PanelError(
                f"PanelProvenance.registry_revision: must be non-empty "
                f"str, got {self.registry_revision!r}"
            )
        if not isinstance(self.label_schema_digest, str) or not self.label_schema_digest:
            raise PanelError(
                f"PanelProvenance.label_schema_digest: must be non-empty "
                f"str, got {self.label_schema_digest!r}"
            )
        if not isinstance(self.declared_label_horizons, tuple):
            raise PanelError(
                f"PanelProvenance.declared_label_horizons: must be tuple, "
                f"got {type(self.declared_label_horizons).__name__}"
            )
        for horizon in self.declared_label_horizons:
            if not isinstance(horizon, int) or isinstance(horizon, bool):
                raise PanelError(
                    f"PanelProvenance.declared_label_horizons: every "
                    f"entry must be int, got {horizon!r}"
                )
            if horizon <= 0:
                raise PanelError(
                    f"PanelProvenance.declared_label_horizons: every "
                    f"entry must be > 0, got {horizon}"
                )
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise PanelError(
                f"PanelProvenance.content_hash: must be non-empty str, got {self.content_hash!r}"
            )

    def member_set(self) -> frozenset[str]:
        """The pool_id hex set the panel binds to."""
        return frozenset(m.pool_id_hex for m in self.member_identities)

    def assert_member_belongs(self, member: PanelMemberIdentity) -> None:
        """Raise :class:`PanelUnknownMemberError` if ``member`` is not bound."""
        if not isinstance(member, PanelMemberIdentity):
            raise PanelError(
                f"PanelProvenance.assert_member_belongs: member must be "
                f"PanelMemberIdentity, got {type(member).__name__}"
            )
        if member.pool_id_hex not in self.member_set():
            raise PanelUnknownMemberError(
                f"PanelProvenance.assert_member_belongs: member "
                f"{member.pool_id_hex!r} is not in the panel's bound "
                f"member set"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_identity": self.run_identity.to_dict(),
            "member_identities": [m.to_dict() for m in self.member_identities],
            "registry_revision": self.registry_revision,
            "label_schema_digest": self.label_schema_digest,
            "declared_label_horizons": list(self.declared_label_horizons),
            "content_hash": self.content_hash,
        }


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def compute_member_identity(content_hash: str) -> str:
    """Return the canonical content hash for a member-identity record.

    The content hash is ``"0x" + sha256_hex(payload)`` where the
    payload is the canonical JSON serialisation of the identity
    dict. The constant prefix matches every other content hash
    in the project (T100, T069). Renaming the prefix is a
    contract change.
    """
    if not isinstance(content_hash, str) or not content_hash:
        raise PanelError(
            f"compute_member_identity: content_hash must be non-empty str, got {content_hash!r}"
        )
    return content_hash


def build_panel_provenance(
    *,
    run_identity: PanelRunIdentity,
    member_identities: Sequence[PanelMemberIdentity],
    registry_revision: str,
    label_schema_digest: str,
    declared_label_horizons: Iterable[int],
) -> PanelProvenance:
    """Build the :class:`PanelProvenance` for one run.

    The function refuses a non-``SUCCEEDED`` run identity,
    refuses a legacy pre-registry manifest, and produces a
    content hash from the canonical representation so a
    re-derivation validates.
    """
    if not isinstance(run_identity, PanelRunIdentity):
        raise PanelError(
            f"build_panel_provenance: run_identity must be "
            f"PanelRunIdentity, got {type(run_identity).__name__}"
        )
    if not isinstance(member_identities, Sequence):
        raise PanelError(
            f"build_panel_provenance: member_identities must be Sequence, "
            f"got {type(member_identities).__name__}"
        )
    if not isinstance(registry_revision, str) or not registry_revision:
        raise PanelError(
            f"build_panel_provenance: registry_revision must be "
            f"non-empty str, got {registry_revision!r}"
        )
    if not isinstance(label_schema_digest, str) or not label_schema_digest:
        raise PanelError(
            f"build_panel_provenance: label_schema_digest must be "
            f"non-empty str, got {label_schema_digest!r}"
        )
    if not isinstance(declared_label_horizons, Iterable):
        raise PanelError(
            f"build_panel_provenance: declared_label_horizons must be "
            f"Iterable, got {type(declared_label_horizons).__name__}"
        )

    run_identity.assert_admitted()

    members = tuple(member_identities)
    horizons = tuple(declared_label_horizons)

    payload = {
        "version": PanelProvenance.VERSION,
        "run_identity": run_identity.to_dict(),
        "member_identities": [m.to_dict() for m in members],
        "registry_revision": registry_revision,
        "label_schema_digest": label_schema_digest,
        "declared_label_horizons": list(horizons),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    content_hash = "0x" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return PanelProvenance(
        version=PanelProvenance.VERSION,
        run_identity=run_identity,
        member_identities=members,
        registry_revision=registry_revision,
        label_schema_digest=label_schema_digest,
        declared_label_horizons=horizons,
        content_hash=content_hash,
    )


# ---------------------------------------------------------------------------
# Dedup helpers
# ---------------------------------------------------------------------------


def canonical_sample_id(*, chain_id: int, pool_id_hex: str, decision_time: int) -> str:
    """Return the canonical sample-id string for a row.

    The format is ``"{chain_id}|{pool_id_hex}|{decision_time}"``;
    the fields are joined with ``"|"`` so a re-run validates
    the same identifier on a different host.
    """
    if not isinstance(chain_id, int) or isinstance(chain_id, bool):
        raise PanelError(
            f"canonical_sample_id: chain_id must be int, got {type(chain_id).__name__}"
        )
    if not isinstance(pool_id_hex, str) or not pool_id_hex:
        raise PanelError(
            f"canonical_sample_id: pool_id_hex must be non-empty str, got {pool_id_hex!r}"
        )
    if not isinstance(decision_time, int) or isinstance(decision_time, bool):
        raise PanelError(
            f"canonical_sample_id: decision_time must be int, got {type(decision_time).__name__}"
        )
    if decision_time < 0:
        raise PanelError(f"canonical_sample_id: decision_time must be >= 0, got {decision_time}")
    return f"{chain_id}|{pool_id_hex}|{decision_time}"


def deduplicate_feature_rows(
    rows: Iterable[PanelFeatureRow],
) -> tuple[PanelFeatureRow, ...]:
    """Deduplicate ``rows`` by sample_id; raise on conflicting columns.

    Two rows that share a ``sample_id`` but disagree about a
    column's value are a hard error: the harness refuses to
    silently pick a winner. The output preserves the first
    occurrence's row.
    """
    by_sample: dict[str, PanelFeatureRow] = {}
    for row in rows:
        if not isinstance(row, PanelFeatureRow):
            raise PanelError(
                f"deduplicate_feature_rows: every entry must be "
                f"PanelFeatureRow, got {type(row).__name__}"
            )
        existing = by_sample.get(row.sample_id)
        if existing is None:
            by_sample[row.sample_id] = row
            continue
        for key, value in row.columns.items():
            previous = existing.columns.get(key)
            if previous is None:
                existing_dict = dict(existing.columns)
                existing_dict[key] = value
                by_sample[existing.sample_id] = PanelFeatureRow(
                    sample_id=existing.sample_id,
                    columns=existing_dict,
                )
            elif previous != value and value is not None:
                raise PanelDuplicateSampleError(
                    f"deduplicate_feature_rows: sample_id "
                    f"{row.sample_id!r} has conflicting values for "
                    f"column {key!r}: existing={previous!r}, new={value!r}"
                )
    return tuple(by_sample.values())


def deduplicate_label_rows(
    rows: Iterable[PanelLabelRow],
) -> tuple[PanelLabelRow, ...]:
    """Deduplicate ``rows`` by sample_id; raise on conflicting columns."""
    by_sample: dict[str, PanelLabelRow] = {}
    for row in rows:
        if not isinstance(row, PanelLabelRow):
            raise PanelError(
                f"deduplicate_label_rows: every entry must be "
                f"PanelLabelRow, got {type(row).__name__}"
            )
        existing = by_sample.get(row.sample_id)
        if existing is None:
            by_sample[row.sample_id] = row
            continue
        for key, value in row.columns.items():
            previous = existing.columns.get(key)
            if previous is None:
                existing_dict = dict(existing.columns)
                existing_dict[key] = value
                by_sample[existing.sample_id] = PanelLabelRow(
                    sample_id=existing.sample_id,
                    columns=existing_dict,
                )
            elif previous != value and value is not None:
                raise PanelDuplicateSampleError(
                    f"deduplicate_label_rows: sample_id "
                    f"{row.sample_id!r} has conflicting label values for "
                    f"{key!r}: existing={previous!r}, new={value!r}"
                )
    return tuple(by_sample.values())


# ---------------------------------------------------------------------------
# Manifest compatibility
# ---------------------------------------------------------------------------


LEGACY_MANIFEST_MARKER: Final[str] = "LEGACY_T063"


def is_legacy_pre_registry_marker(marker: str) -> bool:
    """Return ``True`` iff ``marker`` is the documented legacy pre-registry marker."""
    if not isinstance(marker, str):
        raise PanelError(
            f"is_legacy_pre_registry_marker: marker must be str, got {type(marker).__name__}"
        )
    return marker == LEGACY_MANIFEST_MARKER


def assert_not_legacy_manifest_marker(marker: str | None) -> None:
    """Raise :class:`PanelLegacyManifestError` iff ``marker`` is the legacy pre-registry marker."""
    if marker is None:
        return
    if not isinstance(marker, str):
        raise PanelError(
            f"assert_not_legacy_manifest_marker: marker must be str or "
            f"None, got {type(marker).__name__}"
        )
    if is_legacy_pre_registry_marker(marker):
        raise PanelLegacyManifestError(
            f"assert_not_legacy_manifest_marker: legacy pre-registry "
            f"manifest marker {marker!r} was offered as current panel "
            f"input; the panel refuses legacy artifacts"
        )


__all__ = [
    "LEGACY_MANIFEST_MARKER",
    "PanelCancelledRunError",
    "PanelDatasetVersionError",
    "PanelDuplicateSampleError",
    "PanelError",
    "PanelFailedRunError",
    "PanelFeatureRow",
    "PanelLabelRow",
    "PanelLegacyManifestError",
    "PanelMemberIdentity",
    "PanelMemberIdentityError",
    "PanelProvenance",
    "PanelRegistryRevisionError",
    "PanelRunIdentity",
    "PanelRunIdentityError",
    "PanelSampleProvenance",
    "PanelUnknownMemberError",
    "RunLifecycleState",
    "assert_not_legacy_manifest_marker",
    "build_panel_provenance",
    "canonical_sample_id",
    "compute_member_identity",
    "deduplicate_feature_rows",
    "deduplicate_label_rows",
    "is_legacy_pre_registry_marker",
]

# Sentinel import for the type checker.
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from robinhood_lp.protocol.ids import ChainId  # noqa: F401
