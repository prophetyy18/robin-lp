"""Parameter-search manifest (T066).

T066 binds a parameter sweep to a deterministic, versioned
manifest. The manifest records the grid the runner sweeps, the
seed the run uses, the elimination rules, and a *per-parameter-set*
record of the metric values the sweep produces — including the
losing / rejected ones. The T066 acceptance clause is explicit: no
post-hoc dropping of losing / rejected runs; the manifest is
append-only on the search side, and every record survives.

The module also exposes :func:`compute_parameter_set_version`, the
deterministic version string the candidate-lock freezes. Two
identical parameter mappings produce identical version strings;
one parameter change yields a new version string — that is the
"parameter change creates a new version" rule the contract binds.

Design constraints (binding):

- **Append-only.** :class:`ParameterSearchManifest` is a frozen
  dataclass; every record the sweep adds is captured on a new
  search manifest rather than mutating the previous one.
- **Integer / closed vocabulary.** Parameter values are
  ``int | str | bool`` (the same vocabulary T064
  :class:`ParameterSurface` uses); metric values are Q64.64
  integers; the seed is a non-negative integer.
- **No wall-clock reads.** The manifest module does not import
  ``time.time``.
- **Layer purity.** This module imports the standard library and
  the in-package robustness / experiment modules only. It does not
  import the backtest engine, the manifest layer, RPC, storage,
  signing, execution, or presentation code.

References:

- T066 — Build versioned threshold experiments and provisional
  candidate review.
- T064 — robustness splits, parameter surface, sensitivity summary.
- T065 — the strategy whose parameter set is being versioned.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from robinhood_lp.robustness.surfaces import ParameterSurface

#: Module version. Bumping it is a breaking change for downstream
#: consumers (the harness, the audit chain).
PARAMETER_SEARCH_VERSION: Final[str] = "t066.parameter_search.v1"

#: Closed vocabulary for run status the search manifest records.
#: The vocabulary mirrors the engine audit chain (``FILLED`` /
#: ``REJECTED``) plus the search-specific ``LOSING`` /
#: ``NO_TRADE`` / ``FAILED`` / ``SUPERSEDED`` outcomes; the
#: harness refuses to drop any record.
VALID_RUN_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "FILLED",
        "REJECTED",
        "LOSING",
        "NO_TRADE",
        "FAILED",
        "SUPERSEDED",
        "DEGRADED",
    }
)

#: Closed vocabulary for the elimination rule category a record
#: carries. A record may carry multiple categories (e.g. ``LOSING``
#: AND ``DEGRADED``); the search manifest stores them as a tuple
#: of strings.
VALID_ELIMINATION_REASONS: Final[frozenset[str]] = frozenset(
    {
        "BELOW_THRESHOLD",
        "ABOVE_THRESHOLD",
        "ABOVE_COST_RATIO",
        "BELOW_SAMPLE_SIZE",
        "MISSING_DATA",
        "HALT_CATALOGUE",
        "DEGRADED_TO_FALLBACK",
        "INCOMPLETE_BAR",
        "NOT_ADMITTED",
        "PROVISIONAL_VALUE",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ParameterSearchError(ValueError):
    """Base class for parameter-search construction / validation failures."""


class InvalidParameterSearchFieldError(ParameterSearchError):
    """A parameter-search field violates its invariant."""


class EmptySearchError(ParameterSearchError):
    """The search manifest has no records; the harness rejects an empty sweep."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_str(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidParameterSearchFieldError(f"{field}: must be str, got {type(value).__name__}")
    return value


def _require_non_empty_str(value: object, *, field: str) -> str:
    s = _require_str(value, field=field)
    if not s:
        raise InvalidParameterSearchFieldError(f"{field}: must be non-empty str")
    return s


def _require_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidParameterSearchFieldError(f"{field}: must be int, got {type(value).__name__}")
    return int(value)


def _require_non_negative_int(value: object, *, field: str) -> int:
    n = _require_int(value, field=field)
    if n < 0:
        raise InvalidParameterSearchFieldError(f"{field}: must be non-negative, got {n}")
    return n


def _require_int_any(value: object, *, field: str) -> int:
    """Accept any Python ``int`` (positive, zero, or negative).

    A losing run can carry a negative Q64.64 metric value
    (e.g. a negative Q64.64 return); the search record
    preserves the signed value rather than collapsing it to
    zero, so a reviewer can audit the loss directly.
    """
    return _require_int(value, field=field)


def _coerce_params(params: object) -> dict[str, int | str | bool]:
    """Normalise ``params`` into a JSON-friendly sorted ``dict``."""
    if not isinstance(params, Mapping):
        raise InvalidParameterSearchFieldError(
            f"_coerce_params: must be Mapping, got {type(params).__name__}"
        )
    out: dict[str, int | str | bool] = {}
    for key, value in params.items():
        if not isinstance(key, str) or not key:
            raise InvalidParameterSearchFieldError(
                f"_coerce_params: key must be non-empty str, got {key!r}"
            )
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise InvalidParameterSearchFieldError(
                f"_coerce_params: value for {key!r} must be int|str|bool, "
                f"got {type(value).__name__}"
            )
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# Parameter-set versioning
# ---------------------------------------------------------------------------


def compute_parameter_set_version(
    params: Mapping[str, int | str | bool],
) -> str:
    """Return the deterministic SHA-256 hex digest of a parameter set.

    The version string is the candidate's primary key on the
    search side: two identical parameter mappings produce
    identical version strings, and a single-parameter change
    yields a new version string. The candidate-lock module binds
    this version into the lock's frozen fields.

    The hash is over the canonical ``"key=value"`` representation
    sorted by key so a re-ordered mapping produces the same
    digest.
    """
    if not isinstance(params, Mapping):
        raise InvalidParameterSearchFieldError(
            f"compute_parameter_set_version: must be Mapping, got {type(params).__name__}"
        )
    normalised = _coerce_params(params)
    parts = sorted(f"{k}={normalised[k]}" for k in normalised)
    content = ";".join(parts)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Search record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchRunRecord:
    """A single (parameter-set, fold) result the sweep produces.

    The record captures every piece of evidence the harness needs
    to (a) preserve losing / rejected runs, (b) identify the
    parameter-set version, and (c) report elimination reasons.

    Field units:

    - ``parameter_set_version`` — non-empty hex digest of the
      parameter set (see :func:`compute_parameter_set_version`).
    - ``parameters`` — the parameter mapping the record was run
      with; sorted-by-key serialisation.
    - ``fold_role`` — ``"TRAIN"`` / ``"VALIDATION"`` /
      ``"TEST"`` / ``"HOLDOUT_POOL"``; mirrors the T064 fold-role
      vocabulary.
    - ``segment_label`` — non-empty string identifying the
      fold / pool.
    - ``metric_value`` — Q64.64 integer; ``0`` when no metric
      was produced (e.g. a HALT scenario).
    - ``run_status`` — one of :data:`VALID_RUN_STATUSES`.
    - ``elimination_reasons`` — tuple of strings from
      :data:`VALID_ELIMINATION_REASONS` (empty when not
      eliminated).
    - ``notes`` — tuple of strings for human-readable context;
      the harness never filters by notes.
    """

    parameter_set_version: str
    parameters: tuple[tuple[str, int | str | bool], ...]
    fold_role: str
    segment_label: str
    metric_value: int
    run_status: str
    elimination_reasons: tuple[str, ...]
    notes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(
            self.parameter_set_version,
            field="SearchRunRecord.parameter_set_version",
        )
        if not isinstance(self.parameters, tuple):
            raise InvalidParameterSearchFieldError(
                f"SearchRunRecord.parameters: must be tuple[(str, int|str|bool)], "
                f"got {type(self.parameters).__name__}"
            )
        for entry in self.parameters:
            if not (isinstance(entry, tuple) and len(entry) == 2):
                raise InvalidParameterSearchFieldError(
                    "SearchRunRecord.parameters: every entry must be (str, int|str|bool)"
                )
            key, value = entry
            if not isinstance(key, str) or not key:
                raise InvalidParameterSearchFieldError(
                    "SearchRunRecord.parameters: keys must be non-empty str"
                )
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise InvalidParameterSearchFieldError(
                    "SearchRunRecord.parameters: values must be int|str|bool"
                )
        _require_non_empty_str(self.fold_role, field="SearchRunRecord.fold_role")
        _require_non_empty_str(self.segment_label, field="SearchRunRecord.segment_label")
        _require_int_any(self.metric_value, field="SearchRunRecord.metric_value")
        if self.run_status not in VALID_RUN_STATUSES:
            raise InvalidParameterSearchFieldError(
                f"SearchRunRecord.run_status: must be one of "
                f"{sorted(VALID_RUN_STATUSES)}, got {self.run_status!r}"
            )
        if not isinstance(self.elimination_reasons, tuple):
            raise InvalidParameterSearchFieldError(
                f"SearchRunRecord.elimination_reasons: must be tuple[str, ...], "
                f"got {type(self.elimination_reasons).__name__}"
            )
        for i, reason in enumerate(self.elimination_reasons):
            if reason not in VALID_ELIMINATION_REASONS:
                raise InvalidParameterSearchFieldError(
                    f"SearchRunRecord.elimination_reasons[{i}]: must be one of "
                    f"{sorted(VALID_ELIMINATION_REASONS)}, got {reason!r}"
                )
        if not isinstance(self.notes, tuple):
            raise InvalidParameterSearchFieldError(
                f"SearchRunRecord.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )

    @classmethod
    def from_mapping(
        cls,
        *,
        parameters: Mapping[str, int | str | bool],
        fold_role: str,
        segment_label: str,
        metric_value: int,
        run_status: str,
        elimination_reasons: Sequence[str] = (),
        notes: Sequence[str] = (),
    ) -> SearchRunRecord:
        """Build a :class:`SearchRunRecord` from a parameter mapping.

        The helper computes ``parameter_set_version`` from the
        mapping and sorts the parameters by key so the
        serialisation is deterministic.
        """
        params = _coerce_params(parameters)
        return cls(
            parameter_set_version=compute_parameter_set_version(params),
            parameters=tuple(sorted(params.items())),
            fold_role=fold_role,
            segment_label=segment_label,
            metric_value=metric_value,
            run_status=run_status,
            elimination_reasons=tuple(elimination_reasons),
            notes=tuple(notes),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "parameter_set_version": self.parameter_set_version,
            "parameters": [list(entry) for entry in self.parameters],
            "fold_role": self.fold_role,
            "segment_label": self.segment_label,
            "metric_value": self.metric_value,
            "run_status": self.run_status,
            "elimination_reasons": list(self.elimination_reasons),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Parameter-search manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParameterSearchManifest:
    """The frozen manifest a parameter sweep produces.

    The manifest is the experiment / review layer's record of what
    was swept, with what parameters, on what seed, and what each
    run returned. The harness binds a candidate version (the
    best-fitting record's ``parameter_set_version``) to the
    candidate-lock it writes; a parameter change creates a new
    candidate version, and the harness writes a new lock rather
    than editing the old one.

    Field units:

    - ``version`` — manifest schema version (string).
    - ``search_id`` — non-empty string; the search's primary key.
    - ``parameter_surface`` — the :class:`ParameterSurface` the
      runner swept.
    - ``seed`` — non-negative integer; the random seed the run
      used.
    - ``records`` — non-empty tuple of
      :class:`SearchRunRecord`. Every record is preserved; the
      manifest never drops a losing / rejected / failed run.
    - ``elimination_rules`` — non-empty tuple of strings;
      semantics captured on the audit chain alongside every
      record's ``elimination_reasons``.
    """

    version: str
    search_id: str
    parameter_surface: ParameterSurface
    seed: int
    records: tuple[SearchRunRecord, ...]
    elimination_rules: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.version != PARAMETER_SEARCH_VERSION:
            raise InvalidParameterSearchFieldError(
                f"ParameterSearchManifest.version: must be "
                f"{PARAMETER_SEARCH_VERSION!r}, got {self.version!r}"
            )
        _require_non_empty_str(self.search_id, field="ParameterSearchManifest.search_id")
        if not isinstance(self.parameter_surface, ParameterSurface):
            raise InvalidParameterSearchFieldError(
                f"ParameterSearchManifest.parameter_surface: must be "
                f"ParameterSurface, got {type(self.parameter_surface).__name__}"
            )
        _require_non_negative_int(self.seed, field="ParameterSearchManifest.seed")
        if not self.records:
            raise EmptySearchError(
                "ParameterSearchManifest.records: must be non-empty tuple; "
                "an empty search violates T066 ('all losing/rejected runs "
                "remain')"
            )
        if not self.elimination_rules:
            raise InvalidParameterSearchFieldError(
                "ParameterSearchManifest.elimination_rules: must be non-empty tuple"
            )

    def by_parameter_set_version(self, parameter_set_version: str) -> tuple[SearchRunRecord, ...]:
        """Return every record carrying ``parameter_set_version``.

        The helper preserves order (declaration order on the
        sweep) so two equivalent manifests in any process return
        the same tuple.
        """
        return tuple(r for r in self.records if r.parameter_set_version == parameter_set_version)

    def record_count(self) -> int:
        """Return the number of records the manifest carries."""
        return len(self.records)

    def losing_or_rejected_count(self) -> int:
        """Return the count of records with status ``LOSING`` / ``REJECTED`` /
        ``FAILED`` / ``DEGRADED`` / ``SUPERSEDED``.

        The harness surfaces this count on every output as
        evidence that losing / rejected runs are preserved.
        """
        losing_statuses = {"LOSING", "REJECTED", "FAILED", "DEGRADED", "SUPERSEDED"}
        return sum(1 for r in self.records if r.run_status in losing_statuses)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "search_id": self.search_id,
            "parameter_surface": self.parameter_surface.to_dict(),
            "seed": self.seed,
            "records": [r.to_dict() for r in self.records],
            "elimination_rules": list(self.elimination_rules),
        }


def build_parameter_search_manifest(
    *,
    search_id: str,
    parameter_surface: ParameterSurface,
    seed: int,
    records: Iterable[SearchRunRecord],
    elimination_rules: Sequence[str],
) -> ParameterSearchManifest:
    """Build a :class:`ParameterSearchManifest` from a sequence of records.

    The records are accepted in declaration order; the builder
    does *not* drop a losing / rejected record (the T066 Must-not
    clause). The builder rejects an empty record list because an
    empty sweep cannot satisfy the "all losing/rejected runs
    remain" rule.
    """
    record_tuple = tuple(records)
    if not record_tuple:
        raise EmptySearchError(
            "build_parameter_search_manifest: records must be non-empty; "
            "an empty sweep violates T066 ('all losing/rejected runs "
            "remain')"
        )
    return ParameterSearchManifest(
        version=PARAMETER_SEARCH_VERSION,
        search_id=search_id,
        parameter_surface=parameter_surface,
        seed=seed,
        records=record_tuple,
        elimination_rules=tuple(elimination_rules),
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "PARAMETER_SEARCH_VERSION",
    "VALID_ELIMINATION_REASONS",
    "VALID_RUN_STATUSES",
    "EmptySearchError",
    "InvalidParameterSearchFieldError",
    "ParameterSearchError",
    "ParameterSearchManifest",
    "SearchRunRecord",
    "build_parameter_search_manifest",
    "compute_parameter_set_version",
]
