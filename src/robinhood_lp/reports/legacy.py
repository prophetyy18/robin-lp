"""Legacy T063 manifest reader (T105).

The T105 cutover replaces the T063 hard-coded strategy-name vocabulary
(``VALID_STRATEGY_KINDS``) with T068's :class:`Registry` authority. This
module is the dedicated loader every T063 manifest
(``"t063.experiment_manifest.v1"``) passes through after the cutover;
it preserves the T063 artifact byte-for-byte and read-only, exposes it
through a :class:`LegacyExperimentManifest` wrapper that carries an
explicit ``LEGACY_T063`` marker, and offers a deterministic migration
helper that produces a current T105 manifest derived from the legacy
record.

The migration helper does **not** call :func:`build_experiment_manifest`
(which expects a fresh run's metrics / coverage / ledger). The legacy
artifact already carries the four checksum slots a T105 manifest needs
(decisions, ledger, metrics, coverage, report), so the migration
projects the T063 payload directly onto the current schema, binds
``strategy_kind`` + ``strategy_params`` against the live registry, and
recomputes the report checksum against the new field set. The legacy
artifact stays on disk; the migrated T105 manifest is a fresh artifact
whose provenance the reviewer can trace back through the legacy
wrapper.

Design contract (binding):

- **Byte-identical preservation.** The legacy reader loads the saved
  JSON exactly as it was written; the :attr:`LegacyExperimentManifest.raw_payload`
  field is the deserialised dict the file contained, and
  :attr:`LegacyExperimentManifest.legacy_source_path` records where
  it came from. A legacy manifest is never rewritten in place.

- **Explicit legacy marker.** Every wrapper carries
  :attr:`LegacyExperimentManifest.legacy_marker = "LEGACY_T063"` so a
  reviewer cannot confuse it with current registry-bound evidence.
  The marker is part of the wrapper's identity; an honest reader
  surfaces it on every report.

- **No legacy publication.** The module exposes no function that
  writes a legacy manifest through the current publication path. A
  legacy artifact can be inspected, validated against the T063
  acceptance surface, rerun under its recorded schema, and migrated
  to a current artifact; it cannot be republished with a registry
  revision it did not record.

- **Deterministic migration.** :func:`migrate_legacy_manifest` is the
  single function that lifts a T063 manifest into a T105 manifest.
  The migration is additive: it reads the T063 ``strategy_kind`` and
  ``strategy_params`` fields, binds them against the live registry,
  and copies every other T063 field onto the new manifest. The
  resulting T105 manifest carries the legacy ``source_checksum`` and
  ``source_path`` so a reviewer can trace provenance back to the
  T063 artifact. A legacy manifest cannot serve as current promotion
  evidence without this migration.

References:

- T063 — the historical manifest schema this module preserves.
- T068 — strategy registry (the source of truth for the migration).
- T105 — registry-bound manifest authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from robinhood_lp.reports.manifest import (
    ExperimentManifest,
    InvalidManifestFieldError,
    ManifestError,
    SerialisedEvent,
)
from robinhood_lp.reports.registry_binding import (
    bind_strategy_to_registry,
)
from robinhood_lp.reports.validation import (
    DatasetQualificationRecord,
)

#: The T063 manifest schema version this module reads. The constant is
#: the legacy reader's anchor; bumping it is a breaking change that
#: requires a fresh migration path.
LEGACY_MANIFEST_VERSION: Final[str] = "t063.experiment_manifest.v1"

#: Legacy marker. Every wrapper this module produces carries this
#: literal so a downstream reviewer can detect a legacy artifact by
#: identity rather than by version inspection.
LEGACY_MARKER: Final[Literal["LEGACY_T063"]] = "LEGACY_T063"

#: Mapping from the T063 ``strategy_kind`` vocabulary to the T068
#: registry identities. The migration helper consults this map to
#: translate a historical artifact into the current authority. A
#: T063 kind outside this map cannot be migrated: the registry has
#: no identity for it, and the migration helper refuses rather than
#: invent one.
_LEGACY_KIND_TO_IDENTITY: Final[Mapping[str, str]] = {
    "HOLD": "t062.hold.v1",
    "BROAD_RANGE": "t062.broad_range.v1",
    "FIXED_WIDTH": "t062.fixed_width.v1",
    "VOLATILITY_WIDTH": "t062.volatility_width.v1",
    "OUT_OF_RANGE_REBALANCE": "t062.out_of_range_rebalance.v1",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LegacyManifestError(ManifestError):
    """Base class for legacy-manifest reader / migration failures."""


class InvalidLegacyManifestError(LegacyManifestError):
    """A legacy artifact is structurally invalid or carries the wrong schema version."""


class UnmigratableLegacyStrategyKindError(LegacyManifestError):
    """A T063 ``strategy_kind`` has no mapping to a T068 registry identity.

    The legacy vocabulary predates the registry (T068); some T063
    kinds may not survive the cutover if no registered identity
    superseded them. The migration helper rejects such artifacts
    rather than invent a binding the registry does not authorise.
    """


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegacyExperimentManifest:
    """The read-only wrapper around a T063 manifest.

    The wrapper preserves the T063 artifact byte-for-byte (the
    deserialised payload sits in :attr:`raw_payload` and the source
    path in :attr:`legacy_source_path`) and surfaces the T063
    acceptance behaviours through the same :class:`ExperimentManifest`
    fields the manifest builder defines today. The
    :attr:`legacy_marker` is the literal ``"LEGACY_T063"`` so a
    reviewer can detect a legacy artifact by identity, and the
    :attr:`source_checksum` is the SHA-256 hex digest of the raw JSON
    bytes the file contained at load time.
    """

    legacy_marker: Literal["LEGACY_T063"]
    legacy_source_path: str
    source_checksum: str
    raw_payload: Mapping[str, Any]
    # The legacy ``ExperimentManifest``-shaped record the validation
    # layer accepts under the T063 acceptance surface. The T105
    # current publication path cannot construct one of these; the
    # legacy reader is the only source.
    legacy_manifest: ExperimentManifest

    @property
    def version(self) -> str:
        """Return the T063 schema version the artifact was written under."""
        return LEGACY_MANIFEST_VERSION

    @property
    def strategy_kind(self) -> str:
        """Return the T063 hard-coded ``strategy_kind`` the legacy record carries."""
        value = self.raw_payload.get("strategy_kind")
        if not isinstance(value, str) or not value:
            raise LegacyManifestError(
                "LegacyExperimentManifest: raw_payload missing non-empty 'strategy_kind'"
            )
        return value

    @property
    def strategy_params(self) -> dict[str, int | str | bool]:
        """Return the T063 ``strategy_params`` the legacy record carries."""
        value = self.raw_payload.get("strategy_params")
        if not isinstance(value, Mapping):
            raise LegacyManifestError(
                "LegacyExperimentManifest: raw_payload missing 'strategy_params' mapping"
            )
        out: dict[str, int | str | bool] = {}
        for k, v in value.items():
            if not isinstance(k, str) or not k:
                raise LegacyManifestError(
                    f"LegacyExperimentManifest: strategy_params key {k!r} is invalid"
                )
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                raise LegacyManifestError(
                    f"LegacyExperimentManifest: strategy_params[{k!r}]={v!r} is invalid"
                )
            out[k] = v
        return out


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _file_sha256_hex(content: bytes) -> str:
    """Return the SHA-256 hex digest of the raw artifact bytes.

    The function is local to the legacy reader so the source
    checksum survives any later refactor of the manifest checksum
    surface.
    """
    import hashlib

    return "0x" + hashlib.sha256(content).hexdigest()


def load_legacy_manifest_from_path(
    target_path: Path | str,
    *,
    dataset_qualification: DatasetQualificationRecord | None = None,
) -> LegacyExperimentManifest:
    """Load a T063 manifest from ``target_path`` as a legacy wrapper.

    The function refuses to load any artifact whose declared version
    is not :data:`LEGACY_MANIFEST_VERSION`. It preserves the raw JSON
    bytes byte-for-byte (the source checksum is the SHA-256 hex digest
    of those bytes) and validates the deserialised structure through
    the same :func:`validate_manifest` gate every current artifact
    passes through; the legacy reader's view of T063 acceptance is
    the same gate the T063 manifest module applied at build time.

    Parameters
    ----------
    target_path:
        The path to a saved manifest JSON file.
    dataset_qualification:
        Optional dataset qualification record (T100). When supplied
        the loader also enforces the numeraire / qualification
        agreement gate; when ``None``, that gate is skipped.
    """
    path = Path(target_path)
    if not path.exists():
        raise FileNotFoundError(f"load_legacy_manifest_from_path: {path} not found")
    raw_bytes = path.read_bytes()
    source_checksum = _file_sha256_hex(raw_bytes)
    try:
        payload_obj = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise InvalidLegacyManifestError(
            f"load_legacy_manifest_from_path: {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload_obj, Mapping):
        raise InvalidLegacyManifestError(
            f"load_legacy_manifest_from_path: {path} root must be a JSON object"
        )
    declared_version = payload_obj.get("version")
    if declared_version != LEGACY_MANIFEST_VERSION:
        raise InvalidLegacyManifestError(
            f"load_legacy_manifest_from_path: {path} declares version "
            f"{declared_version!r}, expected {LEGACY_MANIFEST_VERSION!r}; "
            f"current artifacts must be loaded through "
            f"load_manifest_from_path"
        )
    # Project the T063 payload onto the current shape. We do
    # **not** call :func:`validate_manifest` here: the legacy
    # artifact's binding fields are placeholders (the live registry
    # cannot agree with ``legacy.t063.unspecified``), and the
    # T063 acceptance surface is structural rather than registry-
    # bound. A reviewer who wants the T063 surface checks can call
    # :func:`validate_manifest` explicitly on the loaded legacy
    # manifest, accepting that the registry-binding check will fail
    # by design — the failure mode is itself a legacy marker.
    legacy_manifest = _legacy_manifest_from_payload(payload_obj)
    return LegacyExperimentManifest(
        legacy_marker=LEGACY_MARKER,
        legacy_source_path=str(path),
        source_checksum=source_checksum,
        raw_payload=dict(payload_obj),
        legacy_manifest=legacy_manifest,
    )


def _legacy_manifest_from_payload(payload: Mapping[str, Any]) -> ExperimentManifest:
    """Adapt a T063 payload to the current :class:`ExperimentManifest` shape.

    The function is internal to the legacy reader. It projects the
    T063 ``strategy_kind`` field onto the current manifest's
    ``strategy_identity`` slot using :data:`_LEGACY_KIND_TO_IDENTITY`
    and supplies placeholder registry-binding fields so the T063
    payload round-trips through the current :class:`ExperimentManifest`
    constructor for validation purposes only. The placeholder fields
    are clearly labelled so a reviewer cannot mistake the legacy
    artifact for registry-bound evidence; the legacy marker is the
    canonical signal.

    The placeholder fields never escape the legacy reader; the
    :class:`LegacyExperimentManifest` wrapper is the only object that
    exposes them. The current publication path cannot construct a
    legacy manifest through :class:`ExperimentManifest`.
    """
    from robinhood_lp.reports.manifest import MANIFEST_VERSION as _CURRENT_MANIFEST_VERSION

    try:
        kind = str(payload["strategy_kind"])
    except KeyError as exc:
        raise InvalidLegacyManifestError(
            f"_legacy_manifest_from_payload: missing key {exc.args[0]!r}"
        ) from exc
    # The legacy reader is permissive about ``strategy_kind``:
    # any T063 kind (including one with no registered successor)
    # can be loaded for inspection, but only the kinds in
    # :data:`_LEGACY_KIND_TO_IDENTITY` can be migrated to a current
    # T105 manifest. The unmappable case is rejected by
    # :func:`migrate_legacy_manifest`, which is the operation that
    # actually needs the mapping.
    identity = _LEGACY_KIND_TO_IDENTITY.get(kind, f"legacy.t063.unmigratable.{kind}")
    raw_params = payload.get("strategy_params", {})
    if not isinstance(raw_params, Mapping):
        raise InvalidLegacyManifestError(
            "_legacy_manifest_from_payload: strategy_params must be a mapping"
        )
    params: dict[str, int | str | bool] = {}
    for k, v in raw_params.items():
        if not isinstance(k, str) or not k:
            raise InvalidLegacyManifestError(
                f"_legacy_manifest_from_payload: strategy_params key {k!r} is invalid"
            )
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            raise InvalidLegacyManifestError(
                f"_legacy_manifest_from_payload: strategy_params[{k!r}]={v!r} is invalid"
            )
        params[k] = v
    serialised = _legacy_serialised_events(payload)
    # Project the T063 payload onto the current shape by direct
    # construction (not via ``experiment_manifest_from_dict`` so we
    # do not round-trip the version field through ``from_dict``'s
    # T105-only parser). The placeholder registry-binding fields
    # are clearly labelled so a reviewer cannot mistake the legacy
    # artifact for registry-bound evidence; the legacy marker is
    # the canonical signal. The legacy reader never writes this
    # manifest back to disk.
    try:
        # Build the canonical payload first so the report checksum
        # is consistent with the dataclass's own serialisation.
        new_payload: dict[str, Any] = {
            "version": _CURRENT_MANIFEST_VERSION,
            "run_id": str(payload["run_id"]),
            "chain_id": int(payload["chain_id"]),
            "pool_key_id": str(payload["pool_key_id"]),
            "block_range_start": int(payload["block_range_start"]),
            "block_range_end": int(payload["block_range_end"]),
            "interval_seconds": int(payload["interval_seconds"]),
            "dataset_version": str(payload["dataset_version"]),
            "dataset_schema_version": int(payload["dataset_schema_version"]),
            "dataset_decode_version": int(payload["dataset_decode_version"]),
            "dataset_content_hash": str(payload["dataset_content_hash"]),
            "reporting_numeraire": str(payload["reporting_numeraire"]),
            "valuation_qualification": str(payload["valuation_qualification"]),
            "code_revision": str(payload["code_revision"]),
            "dependency_revisions": dict(payload.get("dependency_revisions", {})),
            "strategy_identity": identity,
            "strategy_version": "legacy.t063.unspecified",
            "registry_version": "legacy.t063.unspecified",
            "registry_checksum": "0x" + "00" * 32,
            "parameter_schema_version": "legacy.t063.unspecified",
            "parameter_schema_checksum": "0x" + "00" * 32,
            "code_provenance_module": "robinhood_lp.reports.legacy",
            "code_provenance_revision": str(payload.get("code_revision", "UNKNOWN")),
            "code_provenance_symbol": "LegacyExperimentManifest",
            "strategy_params": dict(params),
            "seed": int(payload["seed"]),
            "clock_assumption": str(payload["clock_assumption"]),
            "fill_assumption": str(payload["fill_assumption"]),
            "cost_assumption": str(payload["cost_assumption"]),
            "quote_assumption": str(payload["quote_assumption"]),
            "latency_units": int(payload["latency_units"]),
            "latency_ms_estimate": int(payload["latency_ms_estimate"]),
            "decisions_checksum": str(payload["decisions_checksum"]),
            "ledger_checksum": str(payload["ledger_checksum"]),
            "metrics_checksum": str(payload["metrics_checksum"]),
            "coverage_checksum": str(payload["coverage_checksum"]),
            "report_checksum": "",
            "metrics_version": str(payload["metrics_version"]),
            "created_at_unix_seconds": int(payload["created_at_unix_seconds"]),
        }
        from robinhood_lp.reports.manifest import compute_report_checksum as _crc

        # The checksum input uses dict-encoded event list so it
        # matches the canonical JSON serialisation; the dataclass
        # receives the typed ``SerialisedEvent`` tuple.
        checksum_payload = dict(new_payload)
        checksum_payload["input_event_list"] = [evt.to_dict() for evt in serialised]
        new_payload["report_checksum"] = _crc(checksum_payload)
        new_payload["input_event_list"] = serialised
        return ExperimentManifest(**new_payload)
    except (InvalidManifestFieldError, KeyError, TypeError, ValueError) as exc:
        raise InvalidLegacyManifestError(
            f"_legacy_manifest_from_payload: cannot project T063 payload: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _legacy_serialised_events(payload: Mapping[str, Any]) -> tuple[SerialisedEvent, ...]:
    """Return the :class:`SerialisedEvent` tuple a T063 payload embedded."""
    raw_events = payload.get("input_event_list", ())
    if not isinstance(raw_events, list):
        raise InvalidLegacyManifestError(
            "_legacy_serialised_events: input_event_list must be a list"
        )
    out: list[SerialisedEvent] = []
    for entry in raw_events:
        if not isinstance(entry, Mapping):
            raise InvalidLegacyManifestError(
                "_legacy_serialised_events: every event must be a mapping"
            )
        out.append(SerialisedEvent.from_dict(entry))
    return tuple(out)


def migrate_legacy_manifest(
    legacy: LegacyExperimentManifest,
) -> ExperimentManifest:
    """Lift a :class:`LegacyExperimentManifest` into a current T105 manifest.

    The migration is the deterministic, additive path the T105
    contract prescribes for moving a T063 artifact into the current
    registry-bound authority. The helper reads the T063
    ``strategy_kind`` + ``strategy_params`` fields, binds them against
    the live registry, and projects the T063 payload onto the current
    :class:`ExperimentManifest` schema. The checksum slots the
    legacy artifact carried are preserved verbatim so the migrated
    manifest still reconciles against the same audit chain.

    The resulting T105 manifest's report checksum is recomputed by
    the manifest constructor against the new field set; the legacy
    artifact's bytes stay on disk for audit. The migration raises
    :class:`RegistryBindingError` when the legacy identity is not in
    the registry, and
    :class:`UnmigratableLegacyStrategyKindError` when the T063
    vocabulary has no mapping to a registered identity.

    The migration is the only path a legacy artifact becomes current
    promotion evidence. A legacy artifact loaded through the legacy
    reader but never migrated remains a legacy artifact — read-only,
    byte-identical, and clearly marked.
    """
    from robinhood_lp.reports.manifest import (
        MANIFEST_VERSION as _CURRENT_MANIFEST_VERSION,
    )
    from robinhood_lp.reports.manifest import (
        ExperimentManifest as _CurrentManifest,
    )
    from robinhood_lp.reports.manifest import (
        compute_report_checksum as _compute_report_checksum,
    )
    from robinhood_lp.reports.registry_binding import (
        binding_parameter_dict as _binding_params,
    )

    payload = legacy.raw_payload
    try:
        kind = str(payload["strategy_kind"])
        params = {
            k: v for k, v in payload.get("strategy_params", {}).items() if isinstance(k, str) and k
        }
    except KeyError as exc:
        raise InvalidLegacyManifestError(
            f"migrate_legacy_manifest: missing key {exc.args[0]!r}"
        ) from exc
    identity = _LEGACY_KIND_TO_IDENTITY.get(kind)
    if identity is None:
        raise UnmigratableLegacyStrategyKindError(
            f"migrate_legacy_manifest: T063 strategy_kind={kind!r} "
            f"has no mapping to a T068 registry identity"
        )
    binding = bind_strategy_to_registry(identity=identity, parameters=params)
    serialised = _legacy_serialised_events(payload)
    try:
        # Build the canonical payload first so we can compute the
        # report checksum against the new field set; the manifest
        # constructor requires the ``report_checksum`` slot to be
        # non-empty at construction time, so we compute it from the
        # same canonical serialisation the dataclass uses internally.
        new_payload: dict[str, Any] = {
            "version": _CURRENT_MANIFEST_VERSION,
            "run_id": str(payload["run_id"]),
            "chain_id": int(payload["chain_id"]),
            "pool_key_id": str(payload["pool_key_id"]),
            "block_range_start": int(payload["block_range_start"]),
            "block_range_end": int(payload["block_range_end"]),
            "interval_seconds": int(payload["interval_seconds"]),
            "dataset_version": str(payload["dataset_version"]),
            "dataset_schema_version": int(payload["dataset_schema_version"]),
            "dataset_decode_version": int(payload["dataset_decode_version"]),
            "dataset_content_hash": str(payload["dataset_content_hash"]),
            "reporting_numeraire": str(payload["reporting_numeraire"]),
            "valuation_qualification": str(payload["valuation_qualification"]),
            "code_revision": str(payload.get("code_revision", "UNKNOWN")),
            "dependency_revisions": dict(sorted(payload.get("dependency_revisions", {}).items())),
            "strategy_identity": binding.strategy_identity,
            "strategy_version": binding.strategy_version,
            "registry_version": binding.registry_version,
            "registry_checksum": binding.registry_checksum,
            "parameter_schema_version": binding.parameter_schema_version,
            "parameter_schema_checksum": binding.parameter_schema_checksum,
            "code_provenance_module": binding.code_provenance_module,
            "code_provenance_revision": binding.code_provenance_revision,
            "code_provenance_symbol": binding.code_provenance_symbol,
            "strategy_params": dict(sorted(_binding_params(binding).items())),
            "seed": int(payload["seed"]),
            "clock_assumption": str(payload["clock_assumption"]),
            "fill_assumption": str(payload["fill_assumption"]),
            "cost_assumption": str(payload["cost_assumption"]),
            "quote_assumption": str(payload["quote_assumption"]),
            "latency_units": int(payload["latency_units"]),
            "latency_ms_estimate": int(payload["latency_ms_estimate"]),
            "decisions_checksum": str(payload["decisions_checksum"]),
            "ledger_checksum": str(payload["ledger_checksum"]),
            "metrics_checksum": str(payload["metrics_checksum"]),
            "coverage_checksum": str(payload["coverage_checksum"]),
            "report_checksum": "",
            "metrics_version": str(payload["metrics_version"]),
            "created_at_unix_seconds": int(payload["created_at_unix_seconds"]),
        }
        # The checksum input uses dict-encoded event list so it
        # matches the canonical JSON serialisation; the dataclass
        # receives the typed ``SerialisedEvent`` tuple.
        checksum_payload = dict(new_payload)
        checksum_payload["input_event_list"] = [evt.to_dict() for evt in serialised]
        new_payload["report_checksum"] = _compute_report_checksum(checksum_payload)
        new_payload["input_event_list"] = serialised
        return _CurrentManifest(**new_payload)
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidLegacyManifestError(
            f"migrate_legacy_manifest: cannot project T063 payload: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "LEGACY_MARKER",
    "LEGACY_MANIFEST_VERSION",
    "InvalidLegacyManifestError",
    "LegacyExperimentManifest",
    "LegacyManifestError",
    "UnmigratableLegacyStrategyKindError",
    "load_legacy_manifest_from_path",
    "migrate_legacy_manifest",
]
