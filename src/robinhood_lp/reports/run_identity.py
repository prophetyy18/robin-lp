"""Multi-pool run identity (T063).

A research run that covers more than one ``(chain_id, PoolKey)`` is
still a single run identity — one ``run_id``, one dataset version,
one reporting numeraire, one valuation qualification — but it
publishes *one manifest per member pool*. The run identity is the
record that binds the member manifests together; without it a
reviewer cannot tell whether two pool manifests belong to the same
run (and therefore share the same dataset / numeraire / seed) or
to two independent runs that happen to share a name.

The acceptance clause T063 binds explicitly:

- "A run covering more than one pool is one run identity over one
  dataset version and one reporting numeraire, publishing one
  manifest per member pool."
- "The manifest is per pool: it records exactly one
  ``(chain_id, PoolKey)``, that pool's own block range and interval,
  its own decisions, ledger, metrics and report checksums, and
  never another pool's."

The :class:`RunIdentity` dataclass is the record; :func:`validate_run_identity`
is the gate. A manifest whose ``(chain_id, PoolKey)`` is not in
the identity's ``member_pools`` set fails validation; a manifest
whose dataset version / numeraire / qualification disagrees with the
identity's shared values fails validation; a manifest that carries
data for another pool fails validation (the per-pool invariant the
manifest enforces independently).

References:

- T063 — Add experiment manifests and reports.
- ADR-014 §3 — numeraire hierarchy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from robinhood_lp.reports.manifest import (
    InvalidManifestFieldError,
)
from robinhood_lp.reports.metrics import (
    _VALID_VALUATION_QUALIFICATIONS,
    VALUATION_QUALIFIED,
    VALUATION_RELATIVE_ONLY,
)

#: Module version. Bumping it is a breaking change for downstream
#: consumers.
RUN_IDENTITY_VERSION: Final[str] = "t063.run_identity.v1"


def _require_non_empty_str(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidManifestFieldError(f"{field_name}: must be non-empty str, got {value!r}")
    return value


def _require_positive_int(value: int, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise InvalidManifestFieldError(f"{field_name}: must be positive int, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """The shared record that ties a multi-pool run's manifests together.

    Field units:

    - ``version`` — schema version string.
    - ``run_id`` — non-empty string. The identity key.
    - ``member_pools`` — non-empty tuple of ``(chain_id, pool_key_id)``
      pairs. Every member must be unique.
    - ``dataset_version`` — non-empty string; the shared dataset the
      whole run consumes.
    - ``dataset_schema_version`` — non-negative integer; the shared
      schema version the whole run consumes.
    - ``dataset_decode_version`` — non-negative integer; the shared
      decode version the whole run consumes.
    - ``dataset_content_hash`` — non-empty hex digest; the shared
      content hash the whole run consumes.
    - ``reporting_numeraire`` — non-empty string; the shared reporting
      numeraire for the whole run.
    - ``valuation_qualification`` — ``"QUALIFIED"`` or
      ``"RELATIVE_ONLY"``; the shared qualification.

    The acceptance clause binds every "shared" field: a manifest
    whose value disagrees with the identity's value fails the
    cross-manifest consistency gate.
    """

    version: str
    run_id: str
    member_pools: tuple[tuple[int, str], ...]
    dataset_version: str
    dataset_schema_version: int
    dataset_decode_version: int
    dataset_content_hash: str
    reporting_numeraire: str
    valuation_qualification: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.version, field_name="RunIdentity.version")
        if self.version != RUN_IDENTITY_VERSION:
            raise InvalidManifestFieldError(
                f"RunIdentity.version: must be {RUN_IDENTITY_VERSION!r}, got {self.version!r}"
            )
        _require_non_empty_str(self.run_id, field_name="RunIdentity.run_id")
        if not isinstance(self.member_pools, tuple) or not self.member_pools:
            raise InvalidManifestFieldError(
                "RunIdentity.member_pools: must be a non-empty tuple of "
                "(chain_id, pool_key_id) pairs"
            )
        seen: set[tuple[int, str]] = set()
        for i, entry in enumerate(self.member_pools):
            if not (
                isinstance(entry, tuple)
                and len(entry) == 2
                and isinstance(entry[0], int)
                and not isinstance(entry[0], bool)
                and isinstance(entry[1], str)
                and entry[1]
            ):
                raise InvalidManifestFieldError(
                    f"RunIdentity.member_pools[{i}]: must be (int, str) pair, got {entry!r}"
                )
            if entry[0] <= 0:
                raise InvalidManifestFieldError(
                    f"RunIdentity.member_pools[{i}].chain_id: must be positive, got {entry[0]}"
                )
            if entry in seen:
                raise InvalidManifestFieldError(
                    f"RunIdentity.member_pools: duplicate member {entry!r}"
                )
            seen.add(entry)
        _require_non_empty_str(self.dataset_version, field_name="RunIdentity.dataset_version")
        if not isinstance(self.dataset_schema_version, int) or isinstance(
            self.dataset_schema_version, bool
        ):
            raise InvalidManifestFieldError(
                f"RunIdentity.dataset_schema_version: must be int, "
                f"got {type(self.dataset_schema_version).__name__}"
            )
        if self.dataset_schema_version < 0:
            raise InvalidManifestFieldError(
                f"RunIdentity.dataset_schema_version: must be non-negative, "
                f"got {self.dataset_schema_version}"
            )
        if not isinstance(self.dataset_decode_version, int) or isinstance(
            self.dataset_decode_version, bool
        ):
            raise InvalidManifestFieldError(
                f"RunIdentity.dataset_decode_version: must be int, "
                f"got {type(self.dataset_decode_version).__name__}"
            )
        if self.dataset_decode_version < 0:
            raise InvalidManifestFieldError(
                f"RunIdentity.dataset_decode_version: must be non-negative, "
                f"got {self.dataset_decode_version}"
            )
        _require_non_empty_str(
            self.dataset_content_hash, field_name="RunIdentity.dataset_content_hash"
        )
        _require_non_empty_str(
            self.reporting_numeraire, field_name="RunIdentity.reporting_numeraire"
        )
        if self.valuation_qualification not in _VALID_VALUATION_QUALIFICATIONS:
            raise InvalidManifestFieldError(
                f"RunIdentity.valuation_qualification: must be one of "
                f"{sorted(_VALID_VALUATION_QUALIFICATIONS)}, "
                f"got {self.valuation_qualification!r}"
            )

    def member_set(self) -> frozenset[tuple[int, str]]:
        """Return the member-pool set for ``in`` checks."""
        return frozenset(self.member_pools)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly representation."""
        return {
            "version": self.version,
            "run_id": self.run_id,
            "member_pools": [
                [chain_id, pool_key_id] for chain_id, pool_key_id in sorted(self.member_pools)
            ],
            "dataset_version": self.dataset_version,
            "dataset_schema_version": self.dataset_schema_version,
            "dataset_decode_version": self.dataset_decode_version,
            "dataset_content_hash": self.dataset_content_hash,
            "reporting_numeraire": self.reporting_numeraire,
            "valuation_qualification": self.valuation_qualification,
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RunIdentityError(ValueError):
    """Base class for run-identity / cross-manifest consistency failures."""


class ForeignManifestError(RunIdentityError):
    """A manifest's ``(chain_id, PoolKey)`` is not in the identity's member set."""


class CrossManifestDisagreementError(RunIdentityError):
    """A manifest's shared value disagrees with the identity's value."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_run_identity(
    identity: RunIdentity,
    manifests: Iterable[object],
) -> None:
    """Validate that ``manifests`` are consistent with ``identity``.

    Every manifest must:

    - have the same ``run_id`` as the identity;
    - have a ``(chain_id, pool_key_id)`` listed in the identity's
      ``member_pools`` set;
    - share the identity's ``dataset_version``,
      ``dataset_schema_version``, ``dataset_decode_version``,
      ``dataset_content_hash``, ``reporting_numeraire``, and
      ``valuation_qualification``.

    A failed check raises :class:`ForeignManifestError` or
    :class:`CrossManifestDisagreementError` with a message that names
    the failing manifest and the disagreement.

    The function does *not* verify checksum integrity (that's
    :func:`robinhood_lp.reports.validation.validate_manifest`); it
    only enforces the cross-manifest invariants the identity binds.
    """
    from typing import cast

    from robinhood_lp.reports.manifest import ExperimentManifest

    manifests_list = list(manifests)
    if not manifests_list:
        raise RunIdentityError("validate_run_identity: manifests must be a non-empty iterable")
    member_set = identity.member_set()
    for i, manifest_raw in enumerate(manifests_list):
        manifest = cast(ExperimentManifest, manifest_raw)
        run_id = getattr(manifest, "run_id", None)
        if run_id != identity.run_id:
            raise CrossManifestDisagreementError(
                f"validate_run_identity: manifest[{i}].run_id={run_id!r} "
                f"disagrees with identity.run_id={identity.run_id!r}"
            )
        pool = (getattr(manifest, "chain_id", None), getattr(manifest, "pool_key_id", None))
        if pool not in member_set:
            raise ForeignManifestError(
                f"validate_run_identity: manifest[{i}] "
                f"({pool[0]}, {pool[1]!r}) is not a member of identity "
                f"(member set has {len(member_set)} pool(s))"
            )
        if manifest.dataset_version != identity.dataset_version:
            raise CrossManifestDisagreementError(
                f"validate_run_identity: manifest[{i}].dataset_version="
                f"{manifest.dataset_version!r} disagrees with identity "
                f"dataset_version={identity.dataset_version!r}"
            )
        if manifest.dataset_schema_version != identity.dataset_schema_version:
            raise CrossManifestDisagreementError(
                f"validate_run_identity: manifest[{i}].dataset_schema_version="
                f"{manifest.dataset_schema_version} disagrees with identity "
                f"dataset_schema_version={identity.dataset_schema_version}"
            )
        if manifest.dataset_decode_version != identity.dataset_decode_version:
            raise CrossManifestDisagreementError(
                f"validate_run_identity: manifest[{i}].dataset_decode_version="
                f"{manifest.dataset_decode_version} disagrees with identity "
                f"dataset_decode_version={identity.dataset_decode_version}"
            )
        if manifest.dataset_content_hash != identity.dataset_content_hash:
            raise CrossManifestDisagreementError(
                f"validate_run_identity: manifest[{i}].dataset_content_hash="
                f"{manifest.dataset_content_hash!r} disagrees with identity "
                f"dataset_content_hash={identity.dataset_content_hash!r}"
            )
        if manifest.reporting_numeraire != identity.reporting_numeraire:
            raise CrossManifestDisagreementError(
                f"validate_run_identity: manifest[{i}].reporting_numeraire="
                f"{manifest.reporting_numeraire!r} disagrees with identity "
                f"reporting_numeraire={identity.reporting_numeraire!r}"
            )
        if manifest.valuation_qualification != identity.valuation_qualification:
            raise CrossManifestDisagreementError(
                f"validate_run_identity: manifest[{i}].valuation_qualification="
                f"{manifest.valuation_qualification!r} disagrees with identity "
                f"valuation_qualification={identity.valuation_qualification!r}"
            )


def run_identity_from_dict(payload: Mapping[str, object]) -> RunIdentity:
    """Reconstruct a :class:`RunIdentity` from a JSON-friendly ``dict``."""
    from typing import Any, cast

    if not isinstance(payload, Mapping):
        raise InvalidManifestFieldError(
            f"run_identity_from_dict: payload must be Mapping, got {type(payload).__name__}"
        )
    payload_dict = cast("dict[str, Any]", dict(payload))
    try:
        raw_members = payload_dict["member_pools"]
    except KeyError as exc:
        raise InvalidManifestFieldError(
            f"run_identity_from_dict: missing key {exc.args[0]!r}"
        ) from exc
    if not isinstance(raw_members, list):
        raise InvalidManifestFieldError(
            "run_identity_from_dict: member_pools must be a list of [chain_id, pool_key_id] pairs"
        )
    members: list[tuple[int, str]] = []
    for entry in raw_members:
        if not isinstance(entry, list) or len(entry) != 2:
            raise InvalidManifestFieldError(
                "run_identity_from_dict: member_pools entry must be a [chain_id, pool_key_id] pair"
            )
        members.append((int(entry[0]), str(entry[1])))
    try:
        return RunIdentity(
            version=str(payload_dict["version"]),
            run_id=str(payload_dict["run_id"]),
            member_pools=tuple(members),
            dataset_version=str(payload_dict["dataset_version"]),
            dataset_schema_version=int(payload_dict["dataset_schema_version"]),
            dataset_decode_version=int(payload_dict["dataset_decode_version"]),
            dataset_content_hash=str(payload_dict["dataset_content_hash"]),
            reporting_numeraire=str(payload_dict["reporting_numeraire"]),
            valuation_qualification=str(payload_dict["valuation_qualification"]),
        )
    except KeyError as exc:
        raise InvalidManifestFieldError(
            f"run_identity_from_dict: missing key {exc.args[0]!r}"
        ) from exc


__all__ = [
    "RUN_IDENTITY_VERSION",
    "CrossManifestDisagreementError",
    "ForeignManifestError",
    "RunIdentity",
    "RunIdentityError",
    "VALUATION_QUALIFIED",
    "VALUATION_RELATIVE_ONLY",
    "run_identity_from_dict",
    "validate_run_identity",
]
