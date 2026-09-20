"""Manifest validation (T063).

The validation layer is the gate every published manifest passes
through before it counts as evidence. It binds four acceptance
clauses of T063:

1. **Tampered inputs fail checksum validation.** Every field the
   manifest carries has a deterministic checksum slot; a
   :func:`validate_manifest` call recomputes the report checksum
   and compares it to the recorded slot. A mismatch is a hard fail.

2. **Missing dataset version or reporting numeraire fails.** A
   manifest whose ``dataset_version`` or ``reporting_numeraire`` is
   empty / ``None`` is rejected before publication.

3. **Numeraire disagrees with the dataset's qualification record
   fails.** The validation layer accepts an optional
   :class:`DatasetQualificationRecord` (the dataset's own
   self-assertion); a manifest whose ``valuation_qualification``
   disagrees with the dataset's qualification is rejected.

4. **Multi-pool run publishes one manifest per pool under one run
   identity, and a manifest that carries another pool's data fails.**
   The per-pool invariant (:meth:`ExperimentManifest.assert_events_match_pool`)
   and the cross-manifest gate (:func:`robinhood_lp.reports.run_identity.validate_run_identity`)
   together enforce this rule.

The "no presenting a ``RELATIVE_ONLY`` run as USD-denominated"
must-not is enforced by :func:`assert_presentation_numeraire_safe`,
which refuses to publish a chart or report under a presentation
numeraire that suggests USD denomination when the run is
``RELATIVE_ONLY``.

The "no overwrite a prior run" must-not is enforced by
:func:`assert_no_prior_run_at_path`, which refuses to write a
manifest over an existing file at the same ``run_id`` and the same
``(chain_id, pool_key_id)`` unless the ``overwrite=True`` flag is
explicit. The CLI defaults to ``overwrite=False``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from robinhood_lp.reports.manifest import (
    ExperimentManifest,
    InvalidManifestFieldError,
    ManifestError,
    compute_report_checksum,
    experiment_manifest_from_dict,
)
from robinhood_lp.reports.metrics import (
    _VALID_VALUATION_QUALIFICATIONS,
    VALUATION_QUALIFIED,
    VALUATION_RELATIVE_ONLY,
)
from robinhood_lp.reports.run_identity import (
    RunIdentity,
    validate_run_identity,
)

#: Module version.
VALIDATION_VERSION: Final[str] = "t063.manifest_validation.v1"

#: Substrings that, when present in the *presentation* numeraire of
#: a ``RELATIVE_ONLY`` run, trigger the "do not present a relative
#: run as USD-denominated" must-not. The set is intentionally narrow:
#: any numeraire string that contains ``"USD"``, ``"USDC"``,
#: ``"USDT"``, or ``"USDG"`` is treated as a USD-denominated
#: presentation. The strings are matched case-insensitively.
_USD_PRESENTATION_TOKENS: Final[frozenset[str]] = frozenset({"USD", "USDC", "USDT", "USDG", "DAI"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ManifestValidationError(ManifestError):
    """Base class for manifest validation failures."""


class ManifestChecksumError(ManifestValidationError):
    """A checksum slot disagrees with the recomputed value.

    A tampered manifest field surfaces here: the validation layer
    recomputes every checksum from the canonical serialisation and
    raises when one slot disagrees. The exception names the failing
    slot so the reviewer can localise the corruption.
    """

    def __init__(self, *, slot: str, recorded: str, recomputed: str) -> None:
        self.slot: Final[str] = slot
        self.recorded: Final[str] = recorded
        self.recomputed: Final[str] = recomputed
        super().__init__(
            f"ManifestChecksumError: {slot} mismatch (recorded={recorded}, recomputed={recomputed})"
        )


class MissingRequiredFieldError(ManifestValidationError):
    """A required field is empty or missing.

    The T063 acceptance clause binds ``dataset_version`` and
    ``reporting_numeraire``; the validation layer raises this error
    when either is missing.
    """

    def __init__(self, *, field: str) -> None:
        self.field: Final[str] = field
        super().__init__(f"MissingRequiredFieldError: {field} must be a non-empty value")


class NumeraireQualificationDisagreementError(ManifestValidationError):
    """The manifest's numeraire / qualification disagrees with the dataset's.

    ADR-014 §3 binds a single qualification per dataset; a manifest
    whose ``valuation_qualification`` is ``QUALIFIED`` while the
    dataset's own record says ``RELATIVE_ONLY`` (or vice versa) is
    rejected here.
    """

    def __init__(
        self,
        *,
        manifest_numeraire: str,
        manifest_qual: str,
        dataset_numeraire: str,
        dataset_qual: str,
    ) -> None:
        self.manifest_numeraire = manifest_numeraire
        self.manifest_qual = manifest_qual
        self.dataset_numeraire = dataset_numeraire
        self.dataset_qual = dataset_qual
        super().__init__(
            f"NumeraireQualificationDisagreementError: manifest reports "
            f"numeraire={manifest_numeraire!r} qualification={manifest_qual!r} "
            f"but dataset record reports numeraire={dataset_numeraire!r} "
            f"qualification={dataset_qual!r}"
        )


class RelativeOnlyUSDPresentationError(ManifestValidationError):
    """A ``RELATIVE_ONLY`` run is being presented under a USD numeraire.

    The must-not clause binds this rejection: a run whose valuation
    qualification is ``RELATIVE_ONLY`` must not be presented as
    USD-denominated. The presentation numeraire must be the same
    relative token, or a labelled volatile numeraire like
    ``"ETH"`` (which is itself marked as volatile per ADR-014 §3).
    """

    def __init__(self, *, presentation_numeraire: str, manifest_numeraire: str) -> None:
        self.presentation_numeraire = presentation_numeraire
        self.manifest_numeraire = manifest_numeraire
        super().__init__(
            f"RelativeOnlyUSDPresentationError: presentation_numeraire="
            f"{presentation_numeraire!r} is a USD denomination, but "
            f"the manifest's reporting numeraire "
            f"{manifest_numeraire!r} is RELATIVE_ONLY. A relative run "
            f"may not be presented as USD-denominated."
        )


class PriorRunOverwriteError(ManifestValidationError):
    """A prior run already exists at the target path / identity.

    The must-not clause binds this rejection: a published manifest
    must not overwrite a prior run. The caller must pick a new
    ``run_id`` / ``(chain_id, pool_key_id)`` or pass the
    ``overwrite=True`` flag explicitly.
    """

    def __init__(self, *, path: str, run_id: str, chain_id: int, pool_key_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.chain_id = chain_id
        self.pool_key_id = pool_key_id
        super().__init__(
            f"PriorRunOverwriteError: a manifest for run_id={run_id!r} "
            f"chain_id={chain_id} pool_key_id={pool_key_id!r} already "
            f"exists at {path!r}; refusing to overwrite"
        )


# ---------------------------------------------------------------------------
# Dataset qualification record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetQualificationRecord:
    """The dataset's own qualification record a manifest must agree with.

    The dataset registry (T100) is the authoritative source for
    which numeraire a dataset qualifies and how; the manifest is
    required to agree with that record, not assert an independent
    view. This dataclass is the minimum the validation layer needs
    to enforce the agreement; the registry module supplies the
    real record at the call site.
    """

    dataset_version: str
    reporting_numeraire: str
    valuation_qualification: str

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_version, str) or not self.dataset_version:
            raise InvalidManifestFieldError(
                "DatasetQualificationRecord.dataset_version: must be non-empty str"
            )
        if not isinstance(self.reporting_numeraire, str) or not self.reporting_numeraire:
            raise InvalidManifestFieldError(
                "DatasetQualificationRecord.reporting_numeraire: must be non-empty str"
            )
        if self.valuation_qualification not in _VALID_VALUATION_QUALIFICATIONS:
            raise InvalidManifestFieldError(
                f"DatasetQualificationRecord.valuation_qualification: must be one of "
                f"{sorted(_VALID_VALUATION_QUALIFICATIONS)}, "
                f"got {self.valuation_qualification!r}"
            )


# ---------------------------------------------------------------------------
# Validation primitives
# ---------------------------------------------------------------------------


def _check_required_non_empty(manifest: ExperimentManifest, *, field: str) -> None:
    value = getattr(manifest, field, None)
    if not isinstance(value, str) or not value:
        raise MissingRequiredFieldError(field=field)


def _check_pool_invariant(manifest: ExperimentManifest) -> None:
    """Re-run the per-pool invariant: every embedded event must match."""
    manifest.assert_events_match_pool()


def _check_report_checksum(manifest: ExperimentManifest) -> None:
    """Recompute the report checksum and compare to the recorded slot."""
    payload = manifest.to_dict()
    recomputed = compute_report_checksum(payload)
    if recomputed != manifest.report_checksum:
        raise ManifestChecksumError(
            slot="report_checksum",
            recorded=manifest.report_checksum,
            recomputed=recomputed,
        )


# ---------------------------------------------------------------------------
# Public validation entry points
# ---------------------------------------------------------------------------


def validate_manifest(
    manifest: ExperimentManifest,
    *,
    dataset_qualification: DatasetQualificationRecord | None = None,
) -> None:
    """Validate a single manifest against the T063 acceptance clauses.

    The function raises on the first failure; callers that want to
    surface every issue should call :func:`iter_validation_errors`
    instead. The check is exhaustive on the single-manifest clauses:
    required fields, report checksum, numeraire / qualification
    agreement (when a :class:`DatasetQualificationRecord` is
    supplied), and the per-pool invariant.

    Cross-manifest invariants (multi-pool run identity) are enforced
    by :func:`robinhood_lp.reports.run_identity.validate_run_identity`;
    call that function separately on the full manifest set.
    """
    # 1. Required fields.
    for field in (
        "dataset_version",
        "reporting_numeraire",
        "dataset_content_hash",
        "code_revision",
    ):
        _check_required_non_empty(manifest, field=field)

    # 2. Per-pool invariant.
    _check_pool_invariant(manifest)

    # 3. Report checksum.
    _check_report_checksum(manifest)

    # 4. Numeraire / qualification agreement with the dataset's record.
    if dataset_qualification is not None and (
        manifest.reporting_numeraire != dataset_qualification.reporting_numeraire
        or manifest.valuation_qualification != dataset_qualification.valuation_qualification
        or manifest.dataset_version != dataset_qualification.dataset_version
    ):
        raise NumeraireQualificationDisagreementError(
            manifest_numeraire=manifest.reporting_numeraire,
            manifest_qual=manifest.valuation_qualification,
            dataset_numeraire=dataset_qualification.reporting_numeraire,
            dataset_qual=dataset_qualification.valuation_qualification,
        )


def iter_validation_errors(
    manifest: ExperimentManifest,
    *,
    dataset_qualification: DatasetQualificationRecord | None = None,
) -> list[ManifestError]:
    """Return the list of every validation error a manifest carries.

    Unlike :func:`validate_manifest`, this function does not raise
    on the first failure; it collects every error and returns the
    list so a reviewer can see the full picture in one pass.
    """
    errors: list[ManifestError] = []
    for field in (
        "dataset_version",
        "reporting_numeraire",
        "dataset_content_hash",
        "code_revision",
    ):
        value = getattr(manifest, field, None)
        if not isinstance(value, str) or not value:
            errors.append(MissingRequiredFieldError(field=field))
    try:
        _check_pool_invariant(manifest)
    except ManifestError as exc:
        errors.append(exc)
    try:
        _check_report_checksum(manifest)
    except ManifestError as exc:
        errors.append(exc)
    if dataset_qualification is not None:
        try:
            if (
                manifest.reporting_numeraire != dataset_qualification.reporting_numeraire
                or manifest.valuation_qualification != dataset_qualification.valuation_qualification
                or manifest.dataset_version != dataset_qualification.dataset_version
            ):
                raise NumeraireQualificationDisagreementError(
                    manifest_numeraire=manifest.reporting_numeraire,
                    manifest_qual=manifest.valuation_qualification,
                    dataset_numeraire=dataset_qualification.reporting_numeraire,
                    dataset_qual=dataset_qualification.valuation_qualification,
                )
        except ManifestError as exc:
            errors.append(exc)
    return errors


# ---------------------------------------------------------------------------
# Presentation guard
# ---------------------------------------------------------------------------


def assert_presentation_numeraire_safe(
    *,
    manifest: ExperimentManifest,
    presentation_numeraire: str,
) -> None:
    """Reject a USD-denominated presentation of a ``RELATIVE_ONLY`` run.

    The must-not clause binds this rejection. The function is the
    single gate every chart, dossier or report that uses the
    manifest must call before it picks a presentation numeraire.

    Parameters
    ----------
    manifest:
        The manifest whose presentation is being checked.
    presentation_numeraire:
        The numeraire the chart / report wants to display the
        result under. An empty string or a USD token is treated as
        a USD denomination for the purpose of this check.
    """
    if not isinstance(presentation_numeraire, str) or not presentation_numeraire:
        raise RelativeOnlyUSDPresentationError(
            presentation_numeraire=str(presentation_numeraire),
            manifest_numeraire=manifest.reporting_numeraire,
        )
    if manifest.valuation_qualification != VALUATION_RELATIVE_ONLY:
        return
    upper = presentation_numeraire.upper()
    for token in _USD_PRESENTATION_TOKENS:
        if token in upper:
            raise RelativeOnlyUSDPresentationError(
                presentation_numeraire=presentation_numeraire,
                manifest_numeraire=manifest.reporting_numeraire,
            )


# ---------------------------------------------------------------------------
# No-overwrite guard
# ---------------------------------------------------------------------------


def assert_no_prior_run_at_path(
    *,
    target_path: Path,
    run_id: str,
    chain_id: int,
    pool_key_id: str,
    overwrite: bool = False,
) -> None:
    """Refuse to write a manifest over an existing run.

    The must-not clause binds this rejection: "do not overwrite a
    prior run". The function raises :class:`PriorRunOverwriteError`
    when ``target_path`` already exists and ``overwrite`` is
    ``False``. The ``overwrite=True`` flag is the explicit escape
    hatch the CLI surfaces for the rare re-publication case; the
    default is to refuse.
    """
    if not overwrite and target_path.exists():
        raise PriorRunOverwriteError(
            path=str(target_path),
            run_id=run_id,
            chain_id=chain_id,
            pool_key_id=pool_key_id,
        )


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------


def write_manifest_to_path(
    manifest: ExperimentManifest,
    target_path: Path,
    *,
    overwrite: bool = False,
) -> None:
    """Validate, then write a manifest to ``target_path``.

    The function is the publish gate. It calls
    :func:`assert_no_prior_run_at_path` first, then writes the
    canonical JSON. The caller is responsible for passing a valid
    manifest (call :func:`validate_manifest` first if the caller
    wants a separate validation pass).
    """
    assert_no_prior_run_at_path(
        target_path=target_path,
        run_id=manifest.run_id,
        chain_id=manifest.chain_id,
        pool_key_id=manifest.pool_key_id,
        overwrite=overwrite,
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        json.dumps(
            manifest.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def load_manifest_from_path(
    target_path: Path,
    *,
    dataset_qualification: DatasetQualificationRecord | None = None,
) -> ExperimentManifest:
    """Load and validate a manifest from ``target_path``.

    The function raises :class:`InvalidManifestFieldError` on a
    structural failure (missing key / wrong type) and a
    :class:`ManifestValidationError` on a checksum / required-field
    failure. The returned manifest is guaranteed to have passed
    :func:`validate_manifest` against the supplied
    :class:`DatasetQualificationRecord` (``None`` skips that step).
    """
    if not target_path.exists():
        raise FileNotFoundError(f"load_manifest_from_path: {target_path} not found")
    raw = target_path.read_text(encoding="utf-8")
    payload_obj = json.loads(raw)
    if not isinstance(payload_obj, Mapping):
        raise InvalidManifestFieldError(
            f"load_manifest_from_path: {target_path} root must be a JSON object"
        )
    manifest = experiment_manifest_from_dict(payload_obj)
    validate_manifest(manifest, dataset_qualification=dataset_qualification)
    return manifest


# ---------------------------------------------------------------------------
# Multi-pool validation entry point
# ---------------------------------------------------------------------------


def validate_multi_pool_run(
    identity: RunIdentity,
    manifests: Sequence[ExperimentManifest],
) -> None:
    """Validate a multi-pool run: identity × per-manifest × cross-manifest.

    The function is the single gate a multi-pool publication passes
    through. It (1) calls :func:`validate_manifest` on every member,
    (2) calls :func:`validate_run_identity` to enforce the cross-
    manifest invariants, and (3) refuses to publish when any member
    has a different ``run_id`` from the identity, or when the
    identity's ``member_pools`` set does not equal the manifest
    set the call site supplies (the run identity declares every
    member; a missing manifest is a publication failure).
    """
    if not manifests:
        raise ManifestValidationError(
            "validate_multi_pool_run: manifests must be a non-empty sequence"
        )
    declared_members = identity.member_set()
    seen_members: set[tuple[int, str]] = set()
    for _i, manifest in enumerate(manifests):
        validate_manifest(manifest)
        seen_members.add((manifest.chain_id, manifest.pool_key_id))
    if seen_members != declared_members:
        missing = declared_members - seen_members
        extra = seen_members - declared_members
        raise ManifestValidationError(
            f"validate_multi_pool_run: manifest set does not match identity "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )
    validate_run_identity(identity, manifests)


# ---------------------------------------------------------------------------
# SHA-256 helper (re-exported for tests)
# ---------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's content."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65_536), b""):
            digest.update(chunk)
    return "0x" + digest.hexdigest()


__all__ = [
    "VALIDATION_VERSION",
    "DatasetQualificationRecord",
    "ManifestChecksumError",
    "ManifestValidationError",
    "MissingRequiredFieldError",
    "NumeraireQualificationDisagreementError",
    "PriorRunOverwriteError",
    "RelativeOnlyUSDPresentationError",
    "RunIdentity",
    "VALUATION_QUALIFIED",
    "VALUATION_RELATIVE_ONLY",
    "assert_no_prior_run_at_path",
    "assert_presentation_numeraire_safe",
    "file_sha256",
    "iter_validation_errors",
    "load_manifest_from_path",
    "validate_manifest",
    "validate_multi_pool_run",
    "write_manifest_to_path",
]
