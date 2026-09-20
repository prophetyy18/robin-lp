"""Neighborhood stability and stress reports (T066).

T066 requires the experiment harness to publish a *neighborhood
stability* report (the spread of metric values around the locked
candidate's parameter set) and a *stress report* (the per-stress
scenario the candidate survived / failed). The two reports
together give a reviewer the sensitivity information the T066
acceptance clause binds: "challenge favorable parameters outside
their selection sample" (the same intent T064 binds in
``RobustnessReport``).

Both reports are integer-valued. They carry the same primary
benchmark + experimental-flag fields the T066 acceptance clause
pins on every output.

Design constraints (binding):

- **Integer-only.** Every quantity is a Python ``int``.
- **Deterministic.** Two equivalent inputs in any process produce
  byte-identical reports.
- **No wall-clock reads.** This module does not import
  ``time.time``.
- **Layer purity.** This module imports the standard library and
  the in-package search module only. It does not import the
  backtest engine, the manifest layer, RPC, storage, signing,
  execution, or presentation code.

References:

- T066 — Build versioned threshold experiments and provisional
  candidate review.
- T064 — robustness splits, parameter surface, sensitivity summary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

#: Module version. Bumping it is a breaking change for downstream
#: consumers (the harness, the audit chain).
STABILITY_VERSION: Final[str] = "t066.stability.v1"

#: Q64.64 fixed-point scale (1.0 in Q64.64 terms).
Q64_SCALE: Final[int] = 1 << 64

#: Closed vocabulary for the outcome a stress scenario produced.
#: The vocabulary mirrors the T064 scenario catalogue
#: (HALT / DEGRADE / SURVIVED) plus the experiment-specific
#: ``NO_DATA`` / ``BELOW_THRESHOLD`` outcomes the harness records.
VALID_STRESS_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        "SURVIVED",
        "DEGRADED",
        "HALTED",
        "NO_DATA",
        "BELOW_THRESHOLD",
        "INCOMPLETE",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StabilityError(ValueError):
    """Base class for stability / stress construction failures."""


class InvalidStabilityError(StabilityError):
    """A stability / stress field violates its invariant."""


class EmptyStabilityError(StabilityError):
    """A stability report cannot be built from an empty grid."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_str(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidStabilityError(f"{field}: must be str, got {type(value).__name__}")
    return value


def _require_non_empty_str(value: object, *, field: str) -> str:
    s = _require_str(value, field=field)
    if not s:
        raise InvalidStabilityError(f"{field}: must be non-empty str")
    return s


def _require_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidStabilityError(f"{field}: must be int, got {type(value).__name__}")
    return int(value)


def _require_non_negative_int(value: object, *, field: str) -> int:
    n = _require_int(value, field=field)
    if n < 0:
        raise InvalidStabilityError(f"{field}: must be non-negative, got {n}")
    return n


def _compute_content_hash(payload: Mapping[str, object]) -> str:
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Neighborhood entry / report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NeighborhoodEntry:
    """A single neighborhood grid cell around the locked candidate.

    A neighborhood entry carries the parameter-set version the
    cell was run with, the metric value, and the deviation from
    the locked candidate's metric. The deviation is the integer
    ``metric - locked_candidate_metric``; a positive deviation
    means the neighborhood cell outperformed the locked
    candidate, a negative deviation means it underperformed.

    Field units:

    - ``parameter_set_version`` — non-empty hex digest.
    - ``metric_value`` — Q64.64 integer.
    - ``deviation_from_locked_q64_64`` — Q64.64 signed integer.
    """

    parameter_set_version: str
    metric_value: int
    deviation_from_locked_q64_64: int

    def __post_init__(self) -> None:
        _require_non_empty_str(
            self.parameter_set_version,
            field="NeighborhoodEntry.parameter_set_version",
        )
        _require_int(self.metric_value, field="NeighborhoodEntry.metric_value")
        _require_int(
            self.deviation_from_locked_q64_64,
            field="NeighborhoodEntry.deviation_from_locked_q64_64",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "parameter_set_version": self.parameter_set_version,
            "metric_value": self.metric_value,
            "deviation_from_locked_q64_64": self.deviation_from_locked_q64_64,
        }


@dataclass(frozen=True, slots=True)
class NeighborhoodStabilityReport:
    """The neighborhood stability report a locked candidate produces.

    The report enumerates every neighborhood cell (the locked
    candidate is *included*; it produces a zero deviation). The
    ``stability_spread_q64_64`` is the integer range
    ``max - min`` over the neighborhood cells; the
    ``outperformed_count`` counts the cells that strictly beat
    the locked candidate (a positive deviation). The "no
    selecting only the highest return" rule (T066 Must-not)
    means the harness surfaces the outperformed count rather
    than ranking the locked candidate above its neighbors.

    Field units:

    - ``locked_parameter_set_version`` — non-empty hex digest.
    - ``locked_metric_value`` — Q64.64 integer.
    - ``neighborhood`` — non-empty tuple of
      :class:`NeighborhoodEntry`.
    - ``stability_spread_q64_64`` — Q64.64 integer
      (``max - min`` over the neighborhood).
    - ``outperformed_count`` — non-negative integer (cells that
      strictly beat the locked candidate).
    - ``primary_benchmark`` — the literal ``"USDG_CASH"``.
    - ``experimental_flag`` — the literal
      ``"EXPERIMENTAL_NOT_LIVE_APPROVED"``.
    - ``content_hash`` — SHA-256 hex digest of every other field.
    """

    locked_parameter_set_version: str
    locked_metric_value: int
    neighborhood: tuple[NeighborhoodEntry, ...]
    stability_spread_q64_64: int
    outperformed_count: int
    primary_benchmark: str
    experimental_flag: str
    content_hash: str

    def __post_init__(self) -> None:
        _require_non_empty_str(
            self.locked_parameter_set_version,
            field="NeighborhoodStabilityReport.locked_parameter_set_version",
        )
        _require_int(
            self.locked_metric_value,
            field="NeighborhoodStabilityReport.locked_metric_value",
        )
        if not self.neighborhood:
            raise EmptyStabilityError("NeighborhoodStabilityReport.neighborhood: must be non-empty")
        if not isinstance(self.neighborhood, tuple):
            raise InvalidStabilityError(
                f"NeighborhoodStabilityReport.neighborhood: must be tuple, "
                f"got {type(self.neighborhood).__name__}"
            )
        _require_int(
            self.stability_spread_q64_64,
            field="NeighborhoodStabilityReport.stability_spread_q64_64",
        )
        _require_non_negative_int(
            self.outperformed_count,
            field="NeighborhoodStabilityReport.outperformed_count",
        )
        if self.outperformed_count > len(self.neighborhood):
            raise InvalidStabilityError(
                f"NeighborhoodStabilityReport.outperformed_count="
                f"{self.outperformed_count} cannot exceed len(neighborhood)="
                f"{len(self.neighborhood)}"
            )
        if self.primary_benchmark != "USDG_CASH":
            raise InvalidStabilityError(
                f"NeighborhoodStabilityReport.primary_benchmark: must be "
                f"'USDG_CASH' (T066 acceptance), got {self.primary_benchmark!r}"
            )
        if self.experimental_flag != "EXPERIMENTAL_NOT_LIVE_APPROVED":
            raise InvalidStabilityError(
                f"NeighborhoodStabilityReport.experimental_flag: must be "
                f"'EXPERIMENTAL_NOT_LIVE_APPROVED' (T066 acceptance), got "
                f"{self.experimental_flag!r}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "locked_parameter_set_version": self.locked_parameter_set_version,
            "locked_metric_value": self.locked_metric_value,
            "neighborhood": [entry.to_dict() for entry in self.neighborhood],
            "stability_spread_q64_64": self.stability_spread_q64_64,
            "outperformed_count": self.outperformed_count,
            "primary_benchmark": self.primary_benchmark,
            "experimental_flag": self.experimental_flag,
            "content_hash": self.content_hash,
        }


def build_neighborhood_stability_report(
    *,
    locked_parameter_set_version: str,
    locked_metric_value: int,
    neighborhood_metric_values: Sequence[tuple[str, int]],
) -> NeighborhoodStabilityReport:
    """Build a :class:`NeighborhoodStabilityReport` from neighborhood cells.

    ``neighborhood_metric_values`` is a sequence of
    ``(parameter_set_version, metric_value)`` pairs in
    declaration order. The locked candidate's parameter-set
    version MUST appear in the input (with its metric value);
    the harness includes it so the report's
    ``outperformed_count`` excludes the locked candidate itself.

    The function computes the deviation for each cell
    (``metric - locked_metric``), the spread
    (``max - min``), and the outperformed count (cells that
    strictly beat the locked candidate). The content hash binds
    every other field.
    """
    if not isinstance(locked_parameter_set_version, str) or not locked_parameter_set_version:
        raise InvalidStabilityError(
            f"build_neighborhood_stability_report: locked_parameter_set_version "
            f"must be non-empty str, got {locked_parameter_set_version!r}"
        )
    if not isinstance(locked_metric_value, int) or isinstance(locked_metric_value, bool):
        raise InvalidStabilityError(
            f"build_neighborhood_stability_report: locked_metric_value must be "
            f"int, got {type(locked_metric_value).__name__}"
        )
    if not neighborhood_metric_values:
        raise EmptyStabilityError(
            "build_neighborhood_stability_report: neighborhood_metric_values must be non-empty"
        )
    locked_seen = False
    neighbourhood_entries: list[NeighborhoodEntry] = []
    for i, entry in enumerate(neighborhood_metric_values):
        if not (isinstance(entry, tuple) and len(entry) == 2):
            raise InvalidStabilityError(
                f"build_neighborhood_stability_report: "
                f"neighborhood_metric_values[{i}] must be (str, int)"
            )
        ps_version, metric_value = entry
        if not isinstance(ps_version, str) or not ps_version:
            raise InvalidStabilityError(
                f"build_neighborhood_stability_report: "
                f"neighborhood_metric_values[{i}][0] must be non-empty str"
            )
        if not isinstance(metric_value, int) or isinstance(metric_value, bool):
            raise InvalidStabilityError(
                f"build_neighborhood_stability_report: "
                f"neighborhood_metric_values[{i}][1] must be int"
            )
        if ps_version == locked_parameter_set_version:
            locked_seen = True
        deviation = metric_value - locked_metric_value
        neighbourhood_entries.append(
            NeighborhoodEntry(
                parameter_set_version=ps_version,
                metric_value=metric_value,
                deviation_from_locked_q64_64=deviation,
            )
        )
    if not locked_seen:
        raise InvalidStabilityError(
            "build_neighborhood_stability_report: locked_parameter_set_version "
            "must appear in neighborhood_metric_values; the harness always "
            "includes the locked candidate so the report surfaces the "
            "neighborhood relative to it"
        )
    metric_values = [e.metric_value for e in neighbourhood_entries]
    spread = max(metric_values) - min(metric_values)
    outperformed = sum(1 for e in neighbourhood_entries if e.deviation_from_locked_q64_64 > 0)
    placeholder = NeighborhoodStabilityReport(
        locked_parameter_set_version=locked_parameter_set_version,
        locked_metric_value=locked_metric_value,
        neighborhood=tuple(neighbourhood_entries),
        stability_spread_q64_64=spread,
        outperformed_count=outperformed,
        primary_benchmark="USDG_CASH",
        experimental_flag="EXPERIMENTAL_NOT_LIVE_APPROVED",
        content_hash="0x" + "00" * 32,
    )
    payload = placeholder.to_dict()
    payload.pop("content_hash", None)
    digest = _compute_content_hash(payload)
    return NeighborhoodStabilityReport(
        locked_parameter_set_version=placeholder.locked_parameter_set_version,
        locked_metric_value=placeholder.locked_metric_value,
        neighborhood=placeholder.neighborhood,
        stability_spread_q64_64=placeholder.stability_spread_q64_64,
        outperformed_count=placeholder.outperformed_count,
        primary_benchmark=placeholder.primary_benchmark,
        experimental_flag=placeholder.experimental_flag,
        content_hash=digest,
    )


# ---------------------------------------------------------------------------
# Stress scenario report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StressScenarioOutcome:
    """A single stress-scenario outcome the harness records.

    Field units:

    - ``scenario_id`` — non-empty string (the catalogue id).
    - ``category`` — non-empty string (the T064 category).
    - ``outcome`` — one of :data:`VALID_STRESS_OUTCOMES`.
    - ``metric_value`` — Q64.64 integer; ``0`` when no metric
      was produced (e.g. ``HALTED``).
    - ``notes`` — tuple of strings; human-readable context.
    """

    scenario_id: str
    category: str
    outcome: str
    metric_value: int
    notes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(self.scenario_id, field="StressScenarioOutcome.scenario_id")
        _require_non_empty_str(self.category, field="StressScenarioOutcome.category")
        if self.outcome not in VALID_STRESS_OUTCOMES:
            raise InvalidStabilityError(
                f"StressScenarioOutcome.outcome: must be one of "
                f"{sorted(VALID_STRESS_OUTCOMES)}, got {self.outcome!r}"
            )
        _require_int(self.metric_value, field="StressScenarioOutcome.metric_value")
        if not isinstance(self.notes, tuple):
            raise InvalidStabilityError(
                f"StressScenarioOutcome.notes: must be tuple, got {type(self.notes).__name__}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "category": self.category,
            "outcome": self.outcome,
            "metric_value": self.metric_value,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class StressReport:
    """The stress report a locked candidate produces.

    The report enumerates every scenario the T064 catalogue
    declares; the harness refuses to drop a scenario post hoc
    (the catalogue is the contract, mirrored from T064's
    ``ScenarioCatalogue``). The ``halted_count`` /
    ``degraded_count`` / ``survived_count`` integer totals
    surface the headline counts a reviewer sees first.

    Field units:

    - ``catalogue_id`` — non-empty string; the catalogue the
      outcomes derive from.
    - ``scenarios`` — non-empty tuple of
      :class:`StressScenarioOutcome`.
    - ``halted_count`` — non-negative integer.
    - ``degraded_count`` — non-negative integer.
    - ``survived_count`` — non-negative integer.
    - ``primary_benchmark`` — the literal ``"USDG_CASH"``.
    - ``experimental_flag`` — the literal
      ``"EXPERIMENTAL_NOT_LIVE_APPROVED"``.
    - ``content_hash`` — SHA-256 hex digest of every other field.
    """

    catalogue_id: str
    scenarios: tuple[StressScenarioOutcome, ...]
    halted_count: int
    degraded_count: int
    survived_count: int
    primary_benchmark: str
    experimental_flag: str
    content_hash: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.catalogue_id, field="StressReport.catalogue_id")
        if not isinstance(self.scenarios, tuple):
            raise InvalidStabilityError(
                f"StressReport.scenarios: must be tuple, got {type(self.scenarios).__name__}"
            )
        if not self.scenarios:
            raise EmptyStabilityError(
                "StressReport.scenarios: must be non-empty tuple; an empty "
                "report violates T066 ('all losing/rejected runs remain')"
            )
        for name in ("halted_count", "degraded_count", "survived_count"):
            _require_non_negative_int(getattr(self, name), field=f"StressReport.{name}")
        if self.primary_benchmark != "USDG_CASH":
            raise InvalidStabilityError(
                f"StressReport.primary_benchmark: must be 'USDG_CASH', "
                f"got {self.primary_benchmark!r}"
            )
        if self.experimental_flag != "EXPERIMENTAL_NOT_LIVE_APPROVED":
            raise InvalidStabilityError(
                f"StressReport.experimental_flag: must be "
                f"'EXPERIMENTAL_NOT_LIVE_APPROVED', got {self.experimental_flag!r}"
            )
        for i, scenario in enumerate(self.scenarios):
            if not isinstance(scenario, StressScenarioOutcome):
                raise InvalidStabilityError(
                    f"StressReport.scenarios[{i}]: must be "
                    f"StressScenarioOutcome, got {type(scenario).__name__}"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "catalogue_id": self.catalogue_id,
            "scenarios": [s.to_dict() for s in self.scenarios],
            "halted_count": self.halted_count,
            "degraded_count": self.degraded_count,
            "survived_count": self.survived_count,
            "primary_benchmark": self.primary_benchmark,
            "experimental_flag": self.experimental_flag,
            "content_hash": self.content_hash,
        }


def build_stress_report(
    *,
    catalogue_id: str,
    outcomes: Iterable[StressScenarioOutcome],
) -> StressReport:
    """Build a :class:`StressReport` from a sequence of stress outcomes.

    The function computes the headline counts (halted, degraded,
    survived) from the outcome vocabulary; the "all
    losing/rejected runs remain" rule is enforced by rejecting
    an empty outcome list at construction.
    """
    scenario_tuple = tuple(outcomes)
    halted = sum(1 for s in scenario_tuple if s.outcome == "HALTED")
    degraded = sum(1 for s in scenario_tuple if s.outcome == "DEGRADED")
    survived = sum(1 for s in scenario_tuple if s.outcome == "SURVIVED")
    placeholder = StressReport(
        catalogue_id=catalogue_id,
        scenarios=scenario_tuple,
        halted_count=halted,
        degraded_count=degraded,
        survived_count=survived,
        primary_benchmark="USDG_CASH",
        experimental_flag="EXPERIMENTAL_NOT_LIVE_APPROVED",
        content_hash="0x" + "00" * 32,
    )
    payload = placeholder.to_dict()
    payload.pop("content_hash", None)
    digest = _compute_content_hash(payload)
    return StressReport(
        catalogue_id=placeholder.catalogue_id,
        scenarios=placeholder.scenarios,
        halted_count=placeholder.halted_count,
        degraded_count=placeholder.degraded_count,
        survived_count=placeholder.survived_count,
        primary_benchmark=placeholder.primary_benchmark,
        experimental_flag=placeholder.experimental_flag,
        content_hash=digest,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "Q64_SCALE",
    "STABILITY_VERSION",
    "VALID_STRESS_OUTCOMES",
    "EmptyStabilityError",
    "InvalidStabilityError",
    "NeighborhoodEntry",
    "NeighborhoodStabilityReport",
    "StabilityError",
    "StressReport",
    "StressScenarioOutcome",
    "build_neighborhood_stability_report",
    "build_stress_report",
]
