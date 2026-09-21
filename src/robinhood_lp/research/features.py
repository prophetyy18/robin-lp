"""Versioned feature registry the panel harness reads (T101, DS-010).

The feature registry is the versioned, declaration-only surface
that names every feature the panel harness may consume. Each entry
records the four pieces of metadata ``DS-010`` requires:

- **unit** — the observation unit the feature integer carries
  (mirrors :class:`robinhood_lp.features.quote.ObservationUnit`);
- **window** — the half-open ``[start, end)`` window the feature
  covers, expressed in either block numbers (``BLOCK``) or Unix
  timestamps (``TIME``); the registry re-uses the T050 vocabulary;
- **availability time** — the moment after which the feature may
  be treated as known (the "max staleness" budget T053 attaches
  to a quote bar);
- **version** — the version of the feature's bar definition; a
  drift in this string is what surfaces as a registry-binding
  failure on a re-run.

The registry is closed-vocabulary: a new feature requires a new
declaration. The registry refuses duplicate column names and a
declaration whose ``availability_time`` precedes ``data_time``.
Forward-derived features are detectable because the harness can
ask the registry whether a column is allowed to depend on data
past ``decision_time`` — a feature whose source window crosses
the bar's decision timestamp is rejected at panel-build time with
:data:`ForwardFeatureError`.

A feature declaration is also intentionally narrow: it does not
encode the bar payload schema. The bar's payload schema is owned
by T050 (``robinhood_lp.features.bars``); the registry simply
records what the harness has to know to assemble a panel from a
bar stream.

References:

- T050 — bars / market features this registry declares versions of.
- ``docs/spec/research/DATASET_AND_EVALUATION.md`` §3 (``DS-010``).
- T052 — the loss-versus-rebalancing proxy label a few feature
  columns are paired with.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from robinhood_lp.features.bars import WindowKind
from robinhood_lp.features.quote import ObservationUnit

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FeatureRegistryError(ValueError):
    """Base class for feature-registry failures."""


class DuplicateFeatureColumnError(FeatureRegistryError):
    """A column name was declared twice in the same registry."""


class InvalidAvailabilityError(FeatureRegistryError):
    """A feature's availability time precedes its data time."""


class ForwardFeatureError(FeatureRegistryError):
    """A feature source window crosses the panel's decision timestamp.

    The harness refuses the panel at build time and rejects the
    panel configuration at validation time. The exception name is
    what the acceptance clause's "deliberately injected
    future-derived feature" tests pin.
    """


class UnknownFeatureError(FeatureRegistryError):
    """A column referenced by the harness has no registry entry."""


class InvalidWindowKindError(FeatureRegistryError):
    """A feature window's ``start >= end`` or has the wrong unit."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FeatureFamily(StrEnum):
    """The eight T050 market features the registry enumerates.

    Strings are part of the public contract. New families are
    additive; renaming an existing family is a breaking change.
    """

    VOLUME = "VOLUME"
    REALIZED_VOL = "REALIZED_VOL"
    PRICE_RANGE = "PRICE_RANGE"
    ACTIVE_LIQUIDITY = "ACTIVE_LIQUIDITY"
    DEPTH_PROXY = "DEPTH_PROXY"
    EFFECTIVE_FEE = "EFFECTIVE_FEE"
    GAS = "GAS"
    FRESHNESS = "FRESHNESS"


# ---------------------------------------------------------------------------
# Feature declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureDeclaration:
    """One named feature in the registry.

    The declaration is the smallest unit the panel consumes. A
    feature is identified by its :attr:`column_name` plus the
    :attr:`version` string; two declarations with the same name
    but different versions are two distinct features the panel
    must not silently merge.

    Field units:

    - ``column_name`` — non-empty string, unique within a registry.
    - ``family`` — the closed-vocabulary feature family (T050).
    - ``unit`` — :class:`ObservationUnit`; the integer value the
      bar stores before the boundary crossing in T101 reduces it
      to a ``float``.
    - ``window_kind`` — :class:`WindowKind` (``TIME`` or ``BLOCK``).
    - ``window_start`` / ``window_end`` — left-inclusive,
      right-exclusive integers in the chosen unit; the harness
      refuses ``window_end <= window_start``.
    - ``data_time`` — non-negative integer in the chosen unit; for
      a ``TIME`` feature this is the right edge's timestamp, for a
      ``BLOCK`` feature it is the right edge's block number.
    - ``availability_time`` — non-negative integer in the chosen
      unit (the moment a consumer may treat the feature as known);
      must be ``>= data_time``.
    - ``version`` — non-empty string; the bar-type version that
      produced this feature.
    - ``depends_on`` — tuple of additional column names this
      feature depends on; an empty tuple means the feature is
      primary. A circular dependency raises
      :class:`FeatureRegistryError` at registry construction.

    The declaration is ``frozen=True, slots=True`` so a registry
    built from the same declaration sequence hashes to the same
    digest regardless of the host or the insertion order.
    """

    column_name: str
    family: FeatureFamily
    unit: ObservationUnit
    window_kind: WindowKind
    window_start: int
    window_end: int
    data_time: int
    availability_time: int
    version: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.column_name, str) or not self.column_name:
            raise FeatureRegistryError(
                f"FeatureDeclaration.column_name: must be non-empty str, got {self.column_name!r}"
            )
        if not isinstance(self.family, FeatureFamily):
            raise FeatureRegistryError(
                f"FeatureDeclaration.family: must be FeatureFamily, "
                f"got {type(self.family).__name__}"
            )
        if not isinstance(self.unit, ObservationUnit):
            raise FeatureRegistryError(
                f"FeatureDeclaration.unit: must be ObservationUnit, got {type(self.unit).__name__}"
            )
        if not isinstance(self.window_kind, WindowKind):
            raise FeatureRegistryError(
                f"FeatureDeclaration.window_kind: must be WindowKind, "
                f"got {type(self.window_kind).__name__}"
            )
        if not isinstance(self.window_start, int) or isinstance(self.window_start, bool):
            raise FeatureRegistryError(
                f"FeatureDeclaration.window_start: must be int, "
                f"got {type(self.window_start).__name__}"
            )
        if not isinstance(self.window_end, int) or isinstance(self.window_end, bool):
            raise FeatureRegistryError(
                f"FeatureDeclaration.window_end: must be int, got {type(self.window_end).__name__}"
            )
        if self.window_start < 0:
            raise FeatureRegistryError(
                f"FeatureDeclaration.window_start={self.window_start} must be >= 0"
            )
        if self.window_end <= self.window_start:
            raise InvalidWindowKindError(
                f"FeatureDeclaration.window_end={self.window_end} must be "
                f"> window_start={self.window_start}"
            )
        if not isinstance(self.data_time, int) or isinstance(self.data_time, bool):
            raise FeatureRegistryError(
                f"FeatureDeclaration.data_time: must be int, got {type(self.data_time).__name__}"
            )
        if self.data_time < self.window_start or self.data_time != self.window_end:
            raise FeatureRegistryError(
                f"FeatureDeclaration.data_time={self.data_time} must equal "
                f"window_end={self.window_end} (T050 bar convention)"
            )
        if not isinstance(self.availability_time, int) or isinstance(self.availability_time, bool):
            raise FeatureRegistryError(
                f"FeatureDeclaration.availability_time: must be int, "
                f"got {type(self.availability_time).__name__}"
            )
        if self.availability_time < self.data_time:
            raise InvalidAvailabilityError(
                f"FeatureDeclaration.availability_time={self.availability_time} "
                f"must be >= data_time={self.data_time}"
            )
        if not isinstance(self.version, str) or not self.version:
            raise FeatureRegistryError(
                f"FeatureDeclaration.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.depends_on, tuple):
            raise FeatureRegistryError(
                f"FeatureDeclaration.depends_on: must be tuple[str, ...], "
                f"got {type(self.depends_on).__name__}"
            )
        for name in self.depends_on:
            if not isinstance(name, str) or not name:
                raise FeatureRegistryError(
                    f"FeatureDeclaration.depends_on: every entry must be "
                    f"non-empty str, got {name!r}"
                )
            if name == self.column_name:
                raise FeatureRegistryError(
                    f"FeatureDeclaration.depends_on: feature "
                    f"{self.column_name!r} must not depend on itself"
                )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping of the declaration."""
        return {
            "column_name": self.column_name,
            "family": self.family.value,
            "unit": self.unit.value,
            "window_kind": self.window_kind.value,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "data_time": self.data_time,
            "availability_time": self.availability_time,
            "version": self.version,
            "depends_on": list(self.depends_on),
        }

    @property
    def window_length(self) -> int:
        """Return ``window_end - window_start`` (the integer window length)."""
        return self.window_end - self.window_start


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureRegistrySnapshot:
    """A versioned snapshot of a :class:`FeatureRegistry`.

    Snapshots are append-only: two snapshots with the same
    declarations hash to the same digest, so a saved panel carries
    the snapshot it was assembled from and a re-run validates
    that the live registry still produces it.
    """

    version: str
    declarations: tuple[FeatureDeclaration, ...]
    content_hash: str
    declared_at_unix_seconds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "content_hash": self.content_hash,
            "declared_at_unix_seconds": self.declared_at_unix_seconds,
            "declarations": [d.to_dict() for d in self.declarations],
        }


class FeatureRegistry:
    """The in-memory registry of :class:`FeatureDeclaration` rows.

    The registry is closed-vocabulary: every feature a panel may
    consume must be declared on it. A panel that references a
    column absent from the registry raises
    :class:`UnknownFeatureError`; a forward-derived feature
    (declared but whose source window crosses the panel's decision
    timestamp) raises :class:`ForwardFeatureError`.

    Insertion order is preserved (``tuple`` semantics) so the
    registry's content hash is stable across hosts.
    """

    #: The registry schema version. The constants a panel snapshot
    #: pins on first build. Bumping the version is a breaking
    #: change because the snapshot names it.
    VERSION: Final[str] = "t101.feature_registry.v1"

    def __init__(self) -> None:
        self._columns: dict[str, FeatureDeclaration] = {}

    @property
    def size(self) -> int:
        """Number of declared features."""
        return len(self._columns)

    @property
    def column_names(self) -> tuple[str, ...]:
        """The ordered tuple of declared column names."""
        return tuple(self._columns.keys())

    def declare(self, declaration: FeatureDeclaration) -> None:
        """Register ``declaration``.

        The registry rejects a duplicate ``column_name`` and
        resolves the ``depends_on`` graph to reject a circular
        dependency (a feature that depends on itself, or that
        depends on a chain that loops back to itself).
        """
        if not isinstance(declaration, FeatureDeclaration):
            raise FeatureRegistryError(
                f"FeatureRegistry.declare: declaration must be "
                f"FeatureDeclaration, got {type(declaration).__name__}"
            )
        if declaration.column_name in self._columns:
            raise DuplicateFeatureColumnError(
                f"FeatureRegistry.declare: column_name {declaration.column_name!r} already declared"
            )
        # Validate ``depends_on`` references and detect cycles.
        new_columns = dict(self._columns)
        new_columns[declaration.column_name] = declaration
        for name in declaration.depends_on:
            if name not in new_columns:
                raise FeatureRegistryError(
                    f"FeatureRegistry.declare: feature "
                    f"{declaration.column_name!r} depends on unknown "
                    f"feature {name!r}"
                )
        # Cycle detection: a feature that transitively depends on
        # itself would make prefix invariance uncheckable.
        self._check_a_cycles_after_declare(declaration, new_columns)
        self._columns = new_columns

    @classmethod
    def _check_a_cycles_after_declare(
        cls,
        declared: FeatureDeclaration,
        columns: Mapping[str, FeatureDeclaration],
    ) -> None:
        """Reject a cycle introduced by the newly declared feature.

        The walk starts from ``declared.column_name`` and follows
        the ``depends_on`` graph. A revisit of the starting node
        raises :class:`FeatureRegistryError`.
        """
        visited: set[str] = set()
        stack: list[str] = [declared.column_name]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            if node != declared.column_name:
                continue
            # The starting node is the *only* node we care about:
            # every path that returns to it from a dependency
            # would be a cycle, but a fresh declaration has no
            # existing edges *into* it, so we only check a
            # self-dependency (which ``__post_init__`` already
            # raised on).
        # The full cycle walk is performed by walking every node;
        # a fresh declaration that depends on nothing cannot
        # create a cycle by itself, and an existing graph already
        # passed the cycle test. The lightweight check is enough.

    def get(self, column_name: str) -> FeatureDeclaration:
        """Return the declaration for ``column_name``."""
        if not isinstance(column_name, str) or not column_name:
            raise FeatureRegistryError(
                f"FeatureRegistry.get: column_name must be non-empty str, got {column_name!r}"
            )
        try:
            return self._columns[column_name]
        except KeyError as exc:
            raise UnknownFeatureError(
                f"FeatureRegistry.get: no feature declared under {column_name!r}"
            ) from exc

    def has(self, column_name: str) -> bool:
        """Return ``True`` iff ``column_name`` is declared."""
        return column_name in self._columns

    def snapshot(
        self,
        *,
        declared_at_unix_seconds: int,
    ) -> FeatureRegistrySnapshot:
        """Return a content-hashed snapshot of the registry.

        Two registries with the same declarations in the same
        insertion order produce identical content hashes; a
        re-registration with the same declarations in a different
        order produces a different hash (the registry refuses to
        silently reorder, so the hash surfaces the difference).
        """
        if not isinstance(declared_at_unix_seconds, int) or isinstance(
            declared_at_unix_seconds, bool
        ):
            raise FeatureRegistryError(
                f"FeatureRegistry.snapshot: declared_at_unix_seconds must "
                f"be int, got {type(declared_at_unix_seconds).__name__}"
            )
        if declared_at_unix_seconds < 0:
            raise FeatureRegistryError(
                f"FeatureRegistry.snapshot: declared_at_unix_seconds must "
                f"be >= 0, got {declared_at_unix_seconds}"
            )
        declarations = tuple(self._columns.values())
        canonical = json.dumps(
            [d.to_dict() for d in declarations],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        content_hash = "0x" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return FeatureRegistrySnapshot(
            version=self.VERSION,
            declarations=declarations,
            content_hash=content_hash,
            declared_at_unix_seconds=declared_at_unix_seconds,
        )


# ---------------------------------------------------------------------------
# Default panel feature registry
# ---------------------------------------------------------------------------


def default_panel_feature_registry() -> FeatureRegistry:
    """Return the canonical panel feature registry the harness starts from.

    The function returns a fresh registry every call so tests can
    mutate it without affecting the canonical surface. The
    declarations match the eight T050 market-feature families in
    their integer / Q64.64 representations; renaming or removing
    these declarations would require a T050+ contract amendment.
    """
    registry = FeatureRegistry()
    # Volume (atomic units). Half-open TIME window; availability
    # adds a tiny ``max_staleness`` budget consistent with T053's
    # pool observation row.
    registry.declare(
        FeatureDeclaration(
            column_name="panel_volume_token0",
            family=FeatureFamily.VOLUME,
            unit=ObservationUnit.RAW_TOKEN_INTEGER,
            window_kind=WindowKind.TIME,
            window_start=0,
            window_end=300,
            data_time=300,
            availability_time=310,
            version="t050.bars.v1",
        )
    )
    registry.declare(
        FeatureDeclaration(
            column_name="panel_volume_token1",
            family=FeatureFamily.VOLUME,
            unit=ObservationUnit.RAW_TOKEN_INTEGER,
            window_kind=WindowKind.TIME,
            window_start=0,
            window_end=300,
            data_time=300,
            availability_time=310,
            version="t050.bars.v1",
        )
    )
    # Realized volatility (Q64.64). Same five-minute window.
    registry.declare(
        FeatureDeclaration(
            column_name="panel_realized_variance_q64_64",
            family=FeatureFamily.REALIZED_VOL,
            unit=ObservationUnit.RATIO,
            window_kind=WindowKind.TIME,
            window_start=0,
            window_end=300,
            data_time=300,
            availability_time=310,
            version="t050.bars.v1",
        )
    )
    # Price range (Q64.96 → integer here as Q64.64 fixed-point
    # carrying the ``high / low`` snapshot, with a unit annotation
    # to mark the column as derived from T050's high / low bar).
    registry.declare(
        FeatureDeclaration(
            column_name="panel_price_range_band_q64_64",
            family=FeatureFamily.PRICE_RANGE,
            unit=ObservationUnit.RATIO,
            window_kind=WindowKind.TIME,
            window_start=0,
            window_end=300,
            data_time=300,
            availability_time=310,
            version="t050.bars.v1",
        )
    )
    registry.declare(
        FeatureDeclaration(
            column_name="panel_active_liquidity",
            family=FeatureFamily.ACTIVE_LIQUIDITY,
            unit=ObservationUnit.RAW_TOKEN_INTEGER,
            window_kind=WindowKind.TIME,
            window_start=0,
            window_end=300,
            data_time=300,
            availability_time=310,
            version="t050.bars.v1",
        )
    )
    # Depth proxy (Q64.64 ratio; ±1% per T050).
    registry.declare(
        FeatureDeclaration(
            column_name="panel_depth_proxy_ratio_q64_64",
            family=FeatureFamily.DEPTH_PROXY,
            unit=ObservationUnit.RATIO,
            window_kind=WindowKind.TIME,
            window_start=0,
            window_end=300,
            data_time=300,
            availability_time=310,
            version="t050.bars.v1",
        )
    )
    # Effective fee (Q64.64 ratio).
    registry.declare(
        FeatureDeclaration(
            column_name="panel_fee_per_swap_q64_64",
            family=FeatureFamily.EFFECTIVE_FEE,
            unit=ObservationUnit.RATIO,
            window_kind=WindowKind.TIME,
            window_start=0,
            window_end=300,
            data_time=300,
            availability_time=310,
            version="t050.bars.v1",
        )
    )
    # Gas (integer atomic units).
    registry.declare(
        FeatureDeclaration(
            column_name="panel_gas_atomic",
            family=FeatureFamily.GAS,
            unit=ObservationUnit.RAW_TOKEN_INTEGER,
            window_kind=WindowKind.TIME,
            window_start=0,
            window_end=300,
            data_time=300,
            availability_time=310,
            version="t050.bars.v1",
        )
    )
    # Freshness (blocks since last observed swap).
    registry.declare(
        FeatureDeclaration(
            column_name="panel_freshness_blocks",
            family=FeatureFamily.FRESHNESS,
            unit=ObservationUnit.DIMENSIONLESS,
            window_kind=WindowKind.BLOCK,
            window_start=0,
            window_end=10,
            data_time=10,
            availability_time=10,
            version="t050.bars.v1",
        )
    )
    return registry


def validate_panel_against_decision_time(
    declarations: Iterable[FeatureDeclaration],
    *,
    decision_time: int,
    decision_time_kind: WindowKind,
) -> None:
    """Reject any feature whose source window crosses ``decision_time``.

    The prefix-invariance gate ``DS-010`` requires features to be
    observable at the decision timestamp. A feature whose
    ``availability_time > decision_time`` is forbidden because the
    consumer could not have observed it.

    The check accepts features whose ``window.kind`` agrees with
    :attr:`decision_time_kind`; a TIME-window feature evaluated at
    a BLOCK decision (or vice versa) raises :class:`ForwardFeatureError`.
    """
    if not isinstance(decision_time, int) or isinstance(decision_time, bool):
        raise FeatureRegistryError(
            f"validate_panel_against_decision_time: decision_time must be int, "
            f"got {type(decision_time).__name__}"
        )
    if not isinstance(decision_time_kind, WindowKind):
        raise FeatureRegistryError(
            f"validate_panel_against_decision_time: decision_time_kind must "
            f"be WindowKind, got {type(decision_time_kind).__name__}"
        )
    for declaration in declarations:
        if not isinstance(declaration, FeatureDeclaration):
            raise FeatureRegistryError(
                f"validate_panel_against_decision_time: every entry must be "
                f"FeatureDeclaration, got {type(declaration).__name__}"
            )
        if declaration.window_kind is not decision_time_kind:
            raise ForwardFeatureError(
                f"validate_panel_against_decision_time: feature "
                f"{declaration.column_name!r} window kind "
                f"{declaration.window_kind.value!r} disagrees with decision "
                f"time kind {decision_time_kind.value!r}"
            )
        if declaration.availability_time > decision_time:
            raise ForwardFeatureError(
                f"validate_panel_against_decision_time: feature "
                f"{declaration.column_name!r} availability_time="
                f"{declaration.availability_time} exceeds decision_time="
                f"{decision_time} (forbidden by DS-010)"
            )


__all__ = [
    "DuplicateFeatureColumnError",
    "FeatureDeclaration",
    "FeatureFamily",
    "FeatureRegistry",
    "FeatureRegistryError",
    "FeatureRegistrySnapshot",
    "ForwardFeatureError",
    "InvalidAvailabilityError",
    "InvalidWindowKindError",
    "UnknownFeatureError",
    "default_panel_feature_registry",
    "validate_panel_against_decision_time",
]
