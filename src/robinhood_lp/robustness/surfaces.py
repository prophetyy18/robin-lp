"""Parameter surfaces and pool/regime segmentation (T064).

T064 binds the robustness report to *sensitivity*, not only the
best run. The two primitives the module owns make that contract
machine-readable:

- :class:`ParameterAxis` / :class:`ParameterSurface` — a
  declarative grid of parameter values the runner sweeps; the grid
  is the canonical input to a sensitivity analysis.
- :class:`RegimeSegment` / :class:`RegimeSegmentation` — a
  declarative segmentation of pools / regimes the runner evaluates
  separately so a favourable total does not hide a single-regime
  loss.

The T064 acceptance clauses the module satisfies:

- **Sensitivity not only best run.** The
  :class:`SensitivitySummary` records both the best metric and the
  spread (range or stddev) across the parameter grid; the
  robustness report carries the spread beside the best so the
  reviewer can see whether the best is robust.

- **Pool/regime segmentation.** The segmentation enumerates every
  segment; the runner reports metrics per segment and refuses to
  drop a segment post hoc (the segmentation is the contract).

Design constraints:

- **Integer-only.** Grid values are integer/str/bool; ratios are
  Q64.64 integers; spread values are non-negative integers.

- **Determinism.** The grid is enumerated in a deterministic order;
  two equivalent surfaces in any process produce the same tuple of
  parameter combinations.

- **Layer purity.** The module imports only the standard library
  and the in-package :mod:`robinhood_lp.robustness.splits` module
  for segmentation unit types. It does not import the backtest
  engine, the manifest layer, RPC, storage, signing, or
  presentation code.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from robinhood_lp.robustness.splits import VALID_HORIZON_UNITS

#: Module version.
SURFACES_VERSION: Final[str] = "t064.robustness_surfaces.v1"

#: Closed vocabulary for the axis value kind.
VALID_AXIS_VALUE_KINDS: Final[frozenset[str]] = frozenset({"int", "str", "bool"})

#: Closed vocabulary for the segmentation regime label.
VALID_REGIME_LABELS: Final[frozenset[str]] = frozenset(
    {
        "ALL_REGIMES",
        "LOW_VOL",
        "HIGH_VOL",
        "BOUNDARY_PRICE",
        "STABLE_PRICE",
        "MISSING_DATA",
        "REORG_WINDOW",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SurfaceError(ValueError):
    """Base class for parameter-surface / segmentation construction failures."""


class InvalidAxisError(SurfaceError):  # noqa: F821 - intentional
    """A parameter axis carries an out-of-vocabulary kind or empty values."""


class InvalidSegmentationError(SurfaceError):
    """A regime segmentation carries an out-of-vocabulary label or empty segment."""


# ---------------------------------------------------------------------------
# Parameter axis / surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParameterAxis:
    """A single axis of a parameter surface.

    Field units:

    - ``name`` — non-empty string; the parameter name the runner
      passes to the model bundle.
    - ``kind`` — one of :data:`VALID_AXIS_VALUE_KINDS` (``int``,
      ``str``, ``bool``).
    - ``values`` — non-empty tuple of values; the runner sweeps the
      cross product of every axis.
    """

    name: str
    kind: str
    values: tuple[int | str | bool, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise SurfaceError("ParameterAxis.name: must be non-empty str")
        if self.kind not in VALID_AXIS_VALUE_KINDS:
            raise SurfaceError(
                f"ParameterAxis.kind: must be one of {sorted(VALID_AXIS_VALUE_KINDS)}, "
                f"got {self.kind!r}"
            )
        if not self.values:
            raise SurfaceError("ParameterAxis.values: must be non-empty tuple")
        # Validate that every value matches ``kind``.
        for i, value in enumerate(self.values):
            if isinstance(value, bool):
                actual = "bool"
            elif isinstance(value, int):
                actual = "int"
            elif isinstance(value, str):
                actual = "str"
            else:
                raise SurfaceError(
                    f"ParameterAxis.values[{i}]: must be int|str|bool, got {type(value).__name__}"
                )
            if actual != self.kind:
                raise SurfaceError(
                    f"ParameterAxis.values[{i}]: kind={actual!r} disagrees with "
                    f"axis.kind={self.kind!r}"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "values": list(self.values),
        }


@dataclass(frozen=True, slots=True)
class ParameterSurface:
    """A declarative grid of parameter values the runner sweeps.

    The surface enumerates a cross product of every axis in
    declaration order: for two axes ``a`` and ``b``, the enumeration
    order is ``(a[0], b[0])``, ``(a[0], b[1])``, ...,
    ``(a[1], b[0])``, .... The enumeration is deterministic and
    append-only.

    Field units:

    - ``axes`` — non-empty tuple of :class:`ParameterAxis`.
    - ``surface_id`` — non-empty string; the surface's primary key.
    """

    surface_id: str
    axes: tuple[ParameterAxis, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.surface_id, str) or not self.surface_id:
            raise SurfaceError("ParameterSurface.surface_id: must be non-empty str")
        if not self.axes:
            raise SurfaceError("ParameterSurface.axes: must be non-empty tuple")
        names = [a.name for a in self.axes]
        if len(set(names)) != len(names):
            raise SurfaceError("ParameterSurface.axes: every axis.name must be unique")

    @property
    def grid_size(self) -> int:
        """Return the total number of grid points (the cross-product size)."""
        n = 1
        for axis in self.axes:
            n *= len(axis.values)
        return n

    def grid(self) -> tuple[dict[str, int | str | bool], ...]:
        """Enumerate the cross product in declaration order.

        The enumeration is deterministic: two equivalent surfaces in
        any process produce the same tuple of parameter mappings.
        """
        points: list[dict[str, int | str | bool]] = [{}]
        for axis in self.axes:
            new_points: list[dict[str, int | str | bool]] = []
            for prefix in points:
                for value in axis.values:
                    entry = dict(prefix)
                    entry[axis.name] = value
                    new_points.append(entry)
            points = new_points
        return tuple(points)

    def to_dict(self) -> dict[str, object]:
        return {
            "surface_id": self.surface_id,
            "axes": [a.to_dict() for a in self.axes],
            "grid_size": self.grid_size,
        }


def build_parameter_surface(
    *,
    surface_id: str,
    axes: Iterable[ParameterAxis],
) -> ParameterSurface:
    """Build a :class:`ParameterSurface` from an iterable of axes.

    The function is the canonical builder. The axis order is the
    declaration order of the iterable; the runner enumerates the
    grid in that order.
    """
    return ParameterSurface(surface_id=surface_id, axes=tuple(axes))


# ---------------------------------------------------------------------------
# Sensitivity summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SensitivitySummary:
    """The sensitivity summary a robustness report must carry.

    T064 binds sensitivity, not only best run. The summary records:

    - ``metric_name`` — non-empty string; the metric the runner
      aggregates (e.g. ``"total_return_q64_64"``).
    - ``best_metric_value`` — Q64.64 integer; the maximum value
      across the grid.
    - ``best_parameter_combo`` — the parameter combination that
      achieved the best metric value (a frozen mapping from
      parameter name to value).
    - ``worst_metric_value`` — Q64.64 integer; the minimum value
      across the grid.
    - ``spread_metric_value`` — Q64.64 integer;
      ``best - worst`` (the range is the canonical "spread"
      metric for the robustness contract).
    - ``n_grid_points`` — positive integer; the grid size the
      summary was computed over.

    Field units: every quantity is an integer Q64.64 ratio or a
    positive integer count.
    """

    metric_name: str
    best_metric_value: int
    best_parameter_combo: tuple[tuple[str, int | str | bool], ...]
    worst_metric_value: int
    spread_metric_value: int
    n_grid_points: int

    def __post_init__(self) -> None:
        if not isinstance(self.metric_name, str) or not self.metric_name:
            raise SurfaceError("SensitivitySummary.metric_name: must be non-empty str")
        for name in ("best_metric_value", "worst_metric_value", "spread_metric_value"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise SurfaceError(
                    f"SensitivitySummary.{name}: must be int, got {type(value).__name__}"
                )
            if value < 0:
                raise SurfaceError(f"SensitivitySummary.{name}: must be non-negative, got {value}")
        if not isinstance(self.n_grid_points, int) or isinstance(self.n_grid_points, bool):
            raise SurfaceError(
                f"SensitivitySummary.n_grid_points: must be int, got "
                f"{type(self.n_grid_points).__name__}"
            )
        if self.n_grid_points <= 0:
            raise SurfaceError(
                f"SensitivitySummary.n_grid_points: must be positive, got {self.n_grid_points}"
            )
        if not isinstance(self.best_parameter_combo, tuple):
            raise SurfaceError(
                f"SensitivitySummary.best_parameter_combo: must be "
                f"tuple[(str, int|str|bool)], got {type(self.best_parameter_combo).__name__}"
            )
        for entry in self.best_parameter_combo:
            if not (isinstance(entry, tuple) and len(entry) == 2):
                raise SurfaceError(
                    "SensitivitySummary.best_parameter_combo: every entry must be "
                    "(str, int|str|bool)"
                )
            key, value = entry
            if not isinstance(key, str) or not key:
                raise SurfaceError(
                    "SensitivitySummary.best_parameter_combo: keys must be non-empty str"
                )
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise SurfaceError(
                    "SensitivitySummary.best_parameter_combo: values must be int|str|bool"
                )

    @property
    def best_combo(self) -> dict[str, int | str | bool]:
        """Return the best parameter combo as a plain ``dict``."""
        return dict(self.best_parameter_combo)

    def to_dict(self) -> dict[str, object]:
        return {
            "metric_name": self.metric_name,
            "best_metric_value": self.best_metric_value,
            "best_parameter_combo": [list(entry) for entry in self.best_parameter_combo],
            "worst_metric_value": self.worst_metric_value,
            "spread_metric_value": self.spread_metric_value,
            "n_grid_points": self.n_grid_points,
        }


def summarize_sensitivity(
    *,
    metric_name: str,
    grid_metric_values: Sequence[tuple[dict[str, int | str | bool], int]],
) -> SensitivitySummary:
    """Build a :class:`SensitivitySummary` from a grid of metric values.

    The function reads the input sequence of
    ``(parameter_combo, metric_value)`` pairs, picks the maximum and
    minimum, computes the range, and returns the summary. The
    summary's ``n_grid_points`` is the input sequence length; a
    caller that wants a smaller grid passes a smaller sequence.
    """
    if not isinstance(metric_name, str) or not metric_name:
        raise SurfaceError("summarize_sensitivity: metric_name must be non-empty str")
    if not grid_metric_values:
        raise SurfaceError("summarize_sensitivity: grid_metric_values must be non-empty")
    best_combo: dict[str, int | str | bool] = {}
    best_value = -1
    worst_value = -1
    for i, (combo, value) in enumerate(grid_metric_values):
        if not isinstance(combo, dict):
            raise SurfaceError(f"summarize_sensitivity: grid_metric_values[{i}] combo must be dict")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SurfaceError(
                f"summarize_sensitivity: grid_metric_values[{i}] value must be "
                f"non-negative int, got {value!r}"
            )
        if i == 0 or value > best_value:
            best_value = value
            best_combo = combo
        if i == 0 or value < worst_value:
            worst_value = value
    if best_value < 0 or worst_value < 0:
        raise SurfaceError("summarize_sensitivity: failed to compute best/worst")
    spread = best_value - worst_value
    return SensitivitySummary(
        metric_name=metric_name,
        best_metric_value=best_value,
        best_parameter_combo=tuple(sorted(best_combo.items())),
        worst_metric_value=worst_value,
        spread_metric_value=spread,
        n_grid_points=len(grid_metric_values),
    )


# ---------------------------------------------------------------------------
# Regime segmentation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegimeSegment:
    """A single segment of a regime segmentation.

    A segment binds a pool identity to a regime label. The runner
    reports metrics per segment; the segmentation is the contract,
    so a post-hoc drop of a segment is rejected at construction
    time (an empty segmentation is rejected; a segment with the
    ``ALL_REGIMES`` label is rejected because every pool already
    belongs to ``ALL_REGIMES`` by definition).

    Field units:

    - ``chain_id`` / ``pool_key_id`` — pool identity.
    - ``regime_label`` — one of :data:`VALID_REGIME_LABELS` except
      ``ALL_REGIMES``.
    - ``segment_id`` — non-empty string; the segmentation's
      primary key.
    """

    chain_id: int
    pool_key_id: str
    regime_label: str
    segment_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise SurfaceError(
                f"RegimeSegment.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise SurfaceError(f"RegimeSegment.chain_id: must be positive, got {self.chain_id}")
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise SurfaceError(
                f"RegimeSegment.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        if self.regime_label not in VALID_REGIME_LABELS:
            raise InvalidSegmentationError(
                f"RegimeSegment.regime_label: must be one of "
                f"{sorted(VALID_REGIME_LABELS)}, got {self.regime_label!r}"
            )
        if self.regime_label == "ALL_REGIMES":
            raise InvalidSegmentationError(
                "RegimeSegment.regime_label: 'ALL_REGIMES' is a meta-label and "
                "cannot be assigned to a specific segment"
            )
        if not isinstance(self.segment_id, str) or not self.segment_id:
            raise SurfaceError("RegimeSegment.segment_id: must be non-empty str")


@dataclass(frozen=True, slots=True)
class RegimeSegmentation:
    """A pool/regime segmentation the runner evaluates separately.

    The segmentation enumerates every segment; the runner reports
    metrics per segment and refuses to drop a segment post hoc
    (the segmentation is the contract).
    """

    segmentation_id: str
    anchor_unit: str
    segments: tuple[RegimeSegment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.segmentation_id, str) or not self.segmentation_id:
            raise SurfaceError("RegimeSegmentation.segmentation_id: must be non-empty str")
        if self.anchor_unit not in VALID_HORIZON_UNITS:
            raise InvalidSegmentationError(
                f"RegimeSegmentation.anchor_unit: must be one of "
                f"{sorted(VALID_HORIZON_UNITS)}, got {self.anchor_unit!r}"
            )
        if not self.segments:
            raise InvalidSegmentationError("RegimeSegmentation.segments: must be non-empty tuple")
        ids = [s.segment_id for s in self.segments]
        if len(set(ids)) != len(ids):
            raise InvalidSegmentationError(
                "RegimeSegmentation.segments: segment_id values must be unique"
            )

    def by_regime(self, regime_label: str) -> tuple[RegimeSegment, ...]:
        """Return every segment in ``regime_label``, in declaration order."""
        return tuple(s for s in self.segments if s.regime_label == regime_label)

    def to_dict(self) -> dict[str, object]:
        return {
            "segmentation_id": self.segmentation_id,
            "anchor_unit": self.anchor_unit,
            "segments": [
                {
                    "chain_id": s.chain_id,
                    "pool_key_id": s.pool_key_id,
                    "regime_label": s.regime_label,
                    "segment_id": s.segment_id,
                }
                for s in self.segments
            ],
        }


def build_regime_segmentation(
    *,
    segmentation_id: str,
    anchor_unit: str,
    segments: Iterable[RegimeSegment],
) -> RegimeSegmentation:
    """Build a :class:`RegimeSegmentation` from an iterable of segments."""
    return RegimeSegmentation(
        segmentation_id=segmentation_id,
        anchor_unit=anchor_unit,
        segments=tuple(segments),
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "SURFACES_VERSION",
    "VALID_AXIS_VALUE_KINDS",
    "VALID_REGIME_LABELS",
    "InvalidSegmentationError",
    "ParameterAxis",
    "ParameterSurface",
    "RegimeSegment",
    "RegimeSegmentation",
    "SensitivitySummary",
    "SurfaceError",
    "build_parameter_surface",
    "build_regime_segmentation",
    "summarize_sensitivity",
]
