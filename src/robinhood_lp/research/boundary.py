"""Statistical / floating-point boundary the model layer declares (T101, DS-012).

The integer / ``Decimal`` policy (``ADR-004``) forbids ``float`` on
protocol, replay, valuation and accounting paths. Models, by
contrast, are float-native (linear, quantile and gradient-boosting
solvers all use floating-point arithmetic). Reconciling the two is
what this module owns.

The boundary is **explicit and named**, not implicit. Every ``float``
the panel harness produces crosses one of two boundaries:

- :class:`Q64_64_FloatBoundary` — for ratios the T050 bars emit
  (volatility, depth-proxy, fee ratios). The boundary converts
  between Q64.64 integers and ``float``, scaling by
  :data:`Q64_SCALE`. The integer value is the only thing the
  protocol / features layer accepts; the float value is what the
  model layer trains on. Crossing the boundary back to integer is
  the rounding step the harness performs before publishing.
- :class:`IntegerFloatBoundary` — for features and labels that
  the harness stores as plain integers (volumes, block counts,
  observation counts). The boundary carries the integer verbatim
  and only yields a ``float`` view for the model layer; publishing
  re-applies ``int(round(float_value))`` rather than the original
  float so a re-run cannot silently widen the integer's width.

The boundary is the **only** place the harness turns ``int`` into
``float`` and back. ``DS-012`` and ADR-014 §5 require this module to
exist and to be named, so a reviewer can grep the research code
base for "this is where the integer stopped" rather than discover it
from a rounding bug.

Design contract (binding):

- **Integer-only output.** The boundary never publishes a ``float``
  outside the named ``to_float`` method; the model artifact carries
  the integer value plus the boundary that produced it so a
  reproduction re-creates the same bytes.
- **Determinism.** The float-to-integer step uses banker's rounding
  (Python's built-in ``round``), so identical input crossings
  produce identical integers on every host.
- **No protocol contamination.** The boundary does not import the
  protocol layer; it owns the Q64.64 scale as a constant and the
  integer / float conversion as plain Python arithmetic.
- **No leakage to features.** A failed boundary never returns a
  ``float`` by accident; ``to_int`` always returns an ``int`` (or
  raises :class:`BoundaryCrossingError` for an unrepresentable
  input).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Protocol

#: Q64.64 scale. The single named numeraire boundary the boundary
#: module uses to convert ratios (T050 volatility, T050 depth
#: proxy, T053 stablecoin ratios) into ``float`` values the model
#: layer trains on.
Q64_SCALE: Final[int] = 1 << 64

#: The largest representable ratio in the Q64.64 boundary. The
#: convention is mirrored from the integer-domain math; ratios that
#: exceed this value are rejected by ``Q64_64_FloatBoundary.from_int``
#: rather than silently truncated.
Q64_64_MAX: Final[int] = (1 << 256) - 1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BoundaryError(ValueError):
    """Base class for boundary-crossing failures."""


class BoundaryCrossingError(BoundaryError):
    """A value the boundary cannot faithfully convert crossed it.

    Crossing a value that the boundary cannot represent losslessly
    is a contract violation: it means a feature row carries an
    out-of-range integer. The model never sees a silently truncated
    value.
    """


# ---------------------------------------------------------------------------
# Boundary protocol
# ---------------------------------------------------------------------------


class StatisticalBoundary(Protocol):
    """The boundary protocol the model layer crosses.

    Every boundary the harness declares implements this protocol.
    The protocol pins three operations:

    - :meth:`from_int` accepts an integer in the boundary's domain
      and returns a ``float`` the model layer trains on;
    - :meth:`to_int` accepts a ``float`` the model produced and
      returns the integer the harness re-stores;
    - :meth:`name` returns the closed-vocabulary string the model
      artifact records.
    """

    def from_int(self, value: int) -> float:
        """Convert ``value`` from the integer domain to a model-tractable ``float``."""
        ...

    def to_int(self, value: float) -> int:
        """Convert a model-produced ``value`` back to the integer domain."""
        ...

    @property
    def name(self) -> str:
        """The closed-vocabulary name of this boundary."""
        ...


# ---------------------------------------------------------------------------
# Q64.64 ratio boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Q64_64_FloatBoundary:
    """The boundary the harness crosses for Q64.64 ratio features.

    Q64.64 is the only fixed-point scale the protocol / features
    layers use for ratios (T050 volatility, T050 depth proxy, T053
    stablecoin ratios). The boundary owns the ``ratio_q64_64`` →
    ``float`` and back conversions.

    The name :attr:`NAME` is the closed-vocabulary token the model
    artifact records; renaming it is a breaking change because a
    saved artifact names the boundary its byte-level hash depends on.
    """

    NAME: Final[str] = "q64_64_ratio"

    @property
    def name(self) -> str:
        """The closed-vocabulary name of this boundary."""
        return self.NAME

    def from_int(self, value: int) -> float:
        """Convert a Q64.64 ``value`` (integer) to a ``float`` ratio."""
        if not isinstance(value, int) or isinstance(value, bool):
            raise BoundaryCrossingError(
                f"Q64_64_FloatBoundary.from_int: value must be int, got {type(value).__name__}"
            )
        if value < 0:
            raise BoundaryCrossingError(
                f"Q64_64_FloatBoundary.from_int: value={value} must be >= 0"
            )
        if value > Q64_64_MAX:
            raise BoundaryCrossingError(
                f"Q64_64_FloatBoundary.from_int: value={value} exceeds Q64_64_MAX={Q64_64_MAX}"
            )
        # Plain Python division keeps the boundary dependency-free
        # and reproducible on every host. The conversion is exact
        # (no rounding) when the value is a multiple of Q64_SCALE.
        return value / float(Q64_SCALE)

    def to_int(self, value: float) -> int:
        """Convert a ``float`` ratio produced by the model layer back to Q64.64."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BoundaryCrossingError(
                f"Q64_64_FloatBoundary.to_int: value must be float|int, got {type(value).__name__}"
            )
        if value < 0:
            raise BoundaryCrossingError(f"Q64_64_FloatBoundary.to_int: value={value} must be >= 0")
        # Round to the nearest integer Q64.64; Q64.64 width is wide
        # enough that the round-trip is the only lossless re-mapping
        # for a float the model produced.
        rounded = int(round(value * Q64_SCALE))
        if rounded < 0 or rounded > Q64_64_MAX:
            raise BoundaryCrossingError(
                f"Q64_64_FloatBoundary.to_int: rounded value {rounded} "
                f"out of Q64_64 range [0, {Q64_64_MAX}]"
            )
        return rounded


# ---------------------------------------------------------------------------
# Plain integer boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntegerFloatBoundary:
    """The boundary the harness crosses for plain-integer features.

    Volumetric features (token atomic units, observation counts,
    swap counts, modify counts, block counts) carry plain integers
    in the protocol / features layer. The boundary yields a
    ``float`` view for the model layer that fits exactly in IEEE-754
    up to ``2**53``. The :meth:`to_int` step uses banker's rounding
    so a deterministic re-run produces the same integer.

    For values above ``2**53`` the boundary raises a
    :class:`BoundaryCrossingError` rather than silently losing
    precision; the harness records the rejected column in the
    sample-size disclosure rather than masking it.
    """

    NAME: Final[str] = "plain_integer"

    #: The largest integer exactly representable as a ``float`` under
    #: IEEE-754 double precision. Boundary crossings above this value
    #: would silently lose precision; the harness refuses to attempt
    #: them.
    MAX_EXACT: Final[int] = (1 << 53) - 1

    @property
    def name(self) -> str:
        """The closed-vocabulary name of this boundary."""
        return self.NAME

    def from_int(self, value: int) -> float:
        """Convert a plain integer ``value`` to a ``float`` for the model layer."""
        if not isinstance(value, int) or isinstance(value, bool):
            raise BoundaryCrossingError(
                f"IntegerFloatBoundary.from_int: value must be int, got {type(value).__name__}"
            )
        if value < 0:
            raise BoundaryCrossingError(
                f"IntegerFloatBoundary.from_int: value={value} must be >= 0"
            )
        if value > self.MAX_EXACT:
            raise BoundaryCrossingError(
                f"IntegerFloatBoundary.from_int: value={value} exceeds "
                f"MAX_EXACT={self.MAX_EXACT} (use Q64_64_FloatBoundary or "
                f"rescale the feature column)"
            )
        return float(value)

    def to_int(self, value: float) -> int:
        """Convert a model-produced ``value`` back to an integer."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BoundaryCrossingError(
                f"IntegerFloatBoundary.to_int: value must be float|int, got {type(value).__name__}"
            )
        if value < 0:
            raise BoundaryCrossingError(f"IntegerFloatBoundary.to_int: value={value} must be >= 0")
        rounded = int(round(value))
        if rounded > self.MAX_EXACT:
            raise BoundaryCrossingError(
                f"IntegerFloatBoundary.to_int: rounded value {rounded} "
                f"exceeds MAX_EXACT={self.MAX_EXACT}"
            )
        return rounded


# ---------------------------------------------------------------------------
# Probability boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbabilityFloatBoundary:
    """The boundary for probability and quantile labels.

    Exit-probability labels and quantile-coverage labels live on
    ``[0.0, 1.0]``. The boundary is one-way for the harness: it
    converts a ``float`` probability to a bounded integer in the
    ``[0, 2**32]`` range so the integer stays protocol-friendly
    while the model layer trains in ``float`` space.

    The conversion is exact at the bounds (``0.0 -> 0``,
    ``1.0 -> 2**32``). The :meth:`from_int` step divides by the
    scale; the :meth:`to_int` step clamps into the closed
    interval and rounds, so a model output greater than ``1.0``
    cannot silently become a probability greater than ``1.0``.
    """

    NAME: Final[str] = "probability_q32"

    #: The probability scale: probability ``1.0`` maps to
    #: ``1 << 32``. The integer width is wide enough that the
    #: rounding step produces a probability every protocol /
    #: accounting layer accepts.
    SCALE: Final[int] = 1 << 32

    @property
    def name(self) -> str:
        """The closed-vocabulary name of this boundary."""
        return self.NAME

    def from_int(self, value: int) -> float:
        """Convert a probability integer ``value`` to ``float``."""
        if not isinstance(value, int) or isinstance(value, bool):
            raise BoundaryCrossingError(
                f"ProbabilityFloatBoundary.from_int: value must be int, got {type(value).__name__}"
            )
        if value < 0:
            raise BoundaryCrossingError(
                f"ProbabilityFloatBoundary.from_int: value={value} must be >= 0"
            )
        if value > self.SCALE:
            raise BoundaryCrossingError(
                f"ProbabilityFloatBoundary.from_int: value={value} exceeds SCALE={self.SCALE}"
            )
        return value / float(self.SCALE)

    def to_int(self, value: float) -> int:
        """Convert a model-produced probability back to a bounded integer."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BoundaryCrossingError(
                f"ProbabilityFloatBoundary.to_int: value must be float|int, "
                f"got {type(value).__name__}"
            )
        # Clamp into the closed unit interval rather than reject:
        # a calibrated probability may round to a tiny over / under
        # because of float arithmetic and we want to refuse
        # silently ``> 1`` rather than crash the run.
        clamped = min(max(float(value), 0.0), 1.0)
        return int(round(clamped * self.SCALE))


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BoundaryCatalogueEntry:
    """One named entry of the boundary catalogue.

    The harness declares its boundary catalogue up front; every
    float crossing performed at runtime is keyed by
    :attr:`column_name` and resolved against the catalogue. The
    catalogue refuses a duplicate column name and refuses two
    different boundaries under the same name.
    """

    column_name: str
    boundary: StatisticalBoundary

    def __post_init__(self) -> None:
        if not isinstance(self.column_name, str) or not self.column_name:
            raise BoundaryError(
                f"BoundaryCatalogueEntry.column_name: must be non-empty str, "
                f"got {self.column_name!r}"
            )
        if not isinstance(
            self.boundary, (Q64_64_FloatBoundary, IntegerFloatBoundary, ProbabilityFloatBoundary)
        ):
            raise BoundaryError(
                f"BoundaryCatalogueEntry.boundary: must be a named "
                f"StatisticalBoundary, got {type(self.boundary).__name__}"
            )


@dataclass(frozen=True, slots=True)
class BoundaryCatalogue:
    """The named, versioned catalogue of boundary entries.

    A run instantiates one catalogue and declares it on every
    panel / harness artifact. The catalogue's content hash is
    part of the model artifact so a saved artifact's float view
    is reproducible on a different host.
    """

    VERSION: Final[str] = "t101.statistical_boundary_catalogue.v1"
    entries: tuple[BoundaryCatalogueEntry, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise BoundaryError(
                f"BoundaryCatalogue.entries: must be tuple, got {type(self.entries).__name__}"
            )
        seen: set[str] = set()
        for entry in self.entries:
            if not isinstance(entry, BoundaryCatalogueEntry):
                raise BoundaryError(
                    f"BoundaryCatalogue.entries: every entry must be "
                    f"BoundaryCatalogueEntry, got {type(entry).__name__}"
                )
            if entry.column_name in seen:
                raise BoundaryError(
                    f"BoundaryCatalogue.entries: duplicate column_name {entry.column_name!r}"
                )
            seen.add(entry.column_name)

    def get(self, column_name: str) -> StatisticalBoundary:
        """Return the boundary for ``column_name`` or raise."""
        if not isinstance(column_name, str) or not column_name:
            raise BoundaryError(
                f"BoundaryCatalogue.get: column_name must be non-empty str, got {column_name!r}"
            )
        for entry in self.entries:
            if entry.column_name == column_name:
                return entry.boundary
        raise BoundaryError(
            f"BoundaryCatalogue.get: no boundary declared for column_name {column_name!r}"
        )

    def column_names(self) -> tuple[str, ...]:
        """Return the ordered column names the catalogue declares."""
        return tuple(entry.column_name for entry in self.entries)

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-friendly ``{column_name: boundary_name}`` mapping."""
        return {entry.column_name: entry.boundary.name for entry in self.entries}

    def extend(self, extras: Iterable[BoundaryCatalogueEntry]) -> BoundaryCatalogue:
        """Return a new catalogue with additional entries.

        Existing column names take precedence; ``extras`` is
        rejected when it duplicates a declared name (the harness
        refuses to quietly extend a closed-vocabulary catalogue).
        """
        if not isinstance(extras, Iterable):
            raise BoundaryError(
                f"BoundaryCatalogue.extend: extras must be Iterable, got {type(extras).__name__}"
            )
        existing = {entry.column_name for entry in self.entries}
        merged: list[BoundaryCatalogueEntry] = list(self.entries)
        for entry in extras:
            if not isinstance(entry, BoundaryCatalogueEntry):
                raise BoundaryError(
                    f"BoundaryCatalogue.extend: every extra must be "
                    f"BoundaryCatalogueEntry, got {type(entry).__name__}"
                )
            if entry.column_name in existing:
                raise BoundaryError(
                    f"BoundaryCatalogue.extend: column_name {entry.column_name!r} already declared"
                )
            existing.add(entry.column_name)
            merged.append(entry)
        return BoundaryCatalogue(entries=tuple(merged))


def default_panel_boundary_catalogue() -> BoundaryCatalogue:
    """Return the canonical boundary catalogue the panel harness uses.

    The catalogue names two integer boundaries (Q64.64 ratio and
    plain integer) and one probability boundary. A feature
    matrix whose column is missing here is reported as
    "boundary not declared" rather than silently converted at
    the model layer.
    """
    return BoundaryCatalogue(
        entries=(
            BoundaryCatalogueEntry(
                column_name="realized_variance_q64_64",
                boundary=Q64_64_FloatBoundary(),
            ),
            BoundaryCatalogueEntry(
                column_name="depth_proxy_ratio_q64_64",
                boundary=Q64_64_FloatBoundary(),
            ),
            BoundaryCatalogueEntry(
                column_name="fee_per_swap_q64_64",
                boundary=Q64_64_FloatBoundary(),
            ),
            BoundaryCatalogueEntry(
                column_name="exit_probability_q32",
                boundary=ProbabilityFloatBoundary(),
            ),
            BoundaryCatalogueEntry(
                column_name="realized_volume_token0",
                boundary=IntegerFloatBoundary(),
            ),
            BoundaryCatalogueEntry(
                column_name="realized_volume_token1",
                boundary=IntegerFloatBoundary(),
            ),
            BoundaryCatalogueEntry(
                column_name="swap_count",
                boundary=IntegerFloatBoundary(),
            ),
            BoundaryCatalogueEntry(
                column_name="net_lp_return_q64_64",
                boundary=Q64_64_FloatBoundary(),
            ),
        )
    )


__all__ = [
    "Q64_SCALE",
    "Q64_64_MAX",
    "BoundaryCatalogue",
    "BoundaryCatalogueEntry",
    "BoundaryCrossingError",
    "BoundaryError",
    "IntegerFloatBoundary",
    "ProbabilityFloatBoundary",
    "Q64_64_FloatBoundary",
    "StatisticalBoundary",
    "default_panel_boundary_catalogue",
]

# Sentinel import for the type checker.
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    # A closed-vocabulary alias for the three named boundaries.
    # Consumers can write ``Boundary = Q64_64_FloatBoundary | IntegerFloatBoundary
    # | ProbabilityFloatBoundary`` when they want to type the boundary
    # catalogue's entries.
    Boundary = Q64_64_FloatBoundary | IntegerFloatBoundary | ProbabilityFloatBoundary
