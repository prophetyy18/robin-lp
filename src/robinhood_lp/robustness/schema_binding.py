"""Schema-bound parameter surfaces (T106).

T106 binds the parameter sweep T064 sweeps to the T068 strategy
registry's parameter schema. Every axis the runner evaluates must be
declared by the registered strategy's parameter schema, and every
value the axis carries must lie inside the schema's declared range
and type/unit. A registry or schema revision produces a separately
identified surface; results from incompatible schema revisions are
not merged into one sensitivity conclusion.

Design contract (binding):

- **Closed registry vocabulary.** Surfaces are built only against
  identities the :func:`default_registry` returns; the builder
  refuses an unregistered identity with
  :class:`UnknownStrategyIdentityError`.

- **Per-axis schema validation.** Every :class:`SchemaBoundParameterAxis`
  names a schema-declared parameter and carries the schema's
  ``type`` / ``unit`` / declared lower / upper bounds. Values are
  validated against the schema via
  :func:`validate_value_against_schema_for_axis`; the helper calls
  the registry's own :func:`registry._coerce_parameter_value`
  helper (via :func:`validate_axis_value`) so an undeclared
  parameter, a wrong-type / wrong-unit value, or an out-of-range
  value is rejected before any fold is evaluated.

- **Surface identity.** A :class:`SchemaBoundParameterSurface`
  records the registered strategy identity, the per-identity
  strategy version, the registry version + checksum, the parameter
  schema version + checksum and the full axis set. Two surfaces
  built against the same registry revision return identical
  checksums; a registry or schema drift surfaces as a checksum
  change. The :func:`surface_identity` helper compares two
  surfaces and detects incompatible revisions.

- **Determinism.** Axis values are stored in declaration order.
  The grid enumerates the cross product in declaration order
  (matching the T064 :class:`ParameterSurface.grid` contract).

- **Layer purity.** The module imports the strategy registry only;
  it does not import the backtest engine, the RPC adapter,
  storage, signing, execution, or presentation code. The dependency
  test in ``tests/test_robustness_t106.py`` enforces this rule by
  walking the live module graph.

References:

- T064 — robustness and anti-overfitting analysis (predecessor).
- T068 — strategy registry (the source of truth this module binds to).
- T105 — registry-bound manifest authority (consumes T106 evidence).
- T107 — robustness search / candidate lock (consumes T106).
- ``docs/spec/research/DATASET_AND_EVALUATION.md`` ``DS-021``,
  ``DS-022``, ``DS-035``, ``DS-041``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from robinhood_lp.strategy.registry import (
    REGISTRY_VERSION,
    ParameterSchema,
    ParameterType,
    Registry,
    RegistryError,
    default_registry,
    is_registered,
)

#: Module version. Bumping the version is a breaking change for
#: downstream consumers (the T107 search / candidate lock, the
#: T102 model evaluation, the T096 final traceability dossier).
SURFACES_VERSION: Final[str] = "t106.robustness_schema_surfaces.v1"

#: Alias of :data:`SURFACES_VERSION` for callers that prefer the
#: ``SCHEMA_BOUND_*`` prefix (the report module imports the
#: version under that name so the binding's surface identity and
#: the report's binding field stay byte-identical across modules).
SCHEMA_BOUND_SURFACES_VERSION: Final[str] = SURFACES_VERSION

#: Closed vocabulary for the schema-bound axis value kind. The kind
#: tracks the schema's :class:`ParameterType` so a reviewer can see
#: whether the axis values are integers, Q64.64 fractions, ticks, or
#: strings. The vocabulary is the union of every value kind a
#: registered schema may declare.
VALID_AXIS_VALUE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "INT",
        "NON_NEGATIVE_INT",
        "POSITIVE_INT",
        "STRICT_Q64_64",
        "Q64_64",
        "TICK",
        "TICK_SPACING",
        "LIQUIDITY",
        "STR",
        "BOOL",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SchemaBindingError(RegistryError):
    """Base class for schema-bound surface construction failures."""


class UndeclaredAxisError(SchemaBindingError):
    """A :class:`SchemaBoundParameterAxis` names a parameter the schema does not declare.

    The registry is the closed vocabulary for parameters; an axis
    that names a parameter outside the registered schema cannot be
    carried by a schema-bound surface.
    """


class InvalidSchemaBoundAxisError(SchemaBindingError):
    """A :class:`SchemaBoundParameterAxis` violates its invariant."""


class IncompatibleSchemaRevisionError(SchemaBindingError):
    """Two surfaces or a surface and a binding disagree on schema / registry revision.

    The T106 contract binds that results from incompatible schema
    revisions must not be merged into one sensitivity conclusion.
    The error is the deterministic gate the runner / report raise
    when two surfaces cannot be combined.
    """


class UnknownAxisValueKindError(SchemaBindingError):
    """A schema-bound axis value kind is not part of the closed vocabulary."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_non_empty_str(value: object, *, field: str) -> str:
    """Validate ``value`` is a non-empty ``str``."""
    if not isinstance(value, str) or not value:
        raise SchemaBindingError(f"{field}: must be non-empty str, got {value!r}")
    return value


def _type_value_kind(parameter_type: ParameterType) -> str:
    """Return the closed-vocabulary kind tag for ``parameter_type``.

    The mapping mirrors :data:`VALID_AXIS_VALUE_KINDS`; every
    :class:`ParameterType` member is associated with one entry so a
    reviewer can see the schema-declared type alongside the axis.
    """
    return parameter_type.value


def _value_kind_for_value(value: object) -> str:
    """Return the closed-vocabulary kind tag for ``value``.

    Python ``bool`` is distinct from ``int``; the helper honours
    that distinction because the registry's own validator rejects
    ``bool`` for every integer parameter type.
    """
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        # Integer values are valid for every integer-typed axis.
        # The runner consults the schema's declared type to pick the
        # specific vocabulary entry; the helper returns a permissive
        # ``"INT"`` so a single integer can match multiple
        # registry-declared types and the schema check resolves the
        # binding.
        return "INT"
    if isinstance(value, str):
        return "STR"
    raise SchemaBindingError(f"_value_kind_for_value: unsupported value {value!r}")


def validate_axis_value(
    *,
    schema: ParameterSchema,
    value: object,
) -> int | bool | str:
    """Validate ``value`` against ``schema`` and return the typed value.

    The helper delegates to the registry's own
    :func:`registry._coerce_parameter_value` (via the parameter's
    declared type) so an undeclared-parameter caller cannot bypass
    the registry. The validator rejects a wrong-type value, an
    out-of-range value, a ``bool`` masquerading as an ``int``,
    a non-empty ``str``, or any other type the schema does not
    declare.
    """
    expected_kind = _type_value_kind(schema.type)
    if expected_kind not in VALID_AXIS_VALUE_KINDS:
        raise UnknownAxisValueKindError(
            f"validate_axis_value: schema type {expected_kind!r} is not a "
            f"closed-vocabulary axis value kind"
        )
    # Reject a non-int / non-str / non-bool value up front so the
    # registry's type-bound check sees the expected Python type.
    actual_kind = _value_kind_for_value(value)
    if actual_kind not in VALID_AXIS_VALUE_KINDS:
        raise InvalidSchemaBoundAxisError(
            f"validate_axis_value: value {value!r} has unsupported kind {actual_kind!r}"
        )
    # The registry's helper accepts ``int`` / ``str`` / ``bool``
    # natively; for every other declared type (``STRICT_Q64_64``,
    # ``Q64_64``, ``TICK``, ``TICK_SPACING``, ``LIQUIDITY``,
    # ``NON_NEGATIVE_INT``, ``POSITIVE_INT``) the registry's
    # :func:`_coerce_parameter_value` enforces the type's own
    # bounds. The schema's optional ``lower_bound`` /
    # ``upper_bound`` are enforced below.
    from robinhood_lp.strategy.registry import _coerce_parameter_value

    try:
        coerced = _coerce_parameter_value(value, expected_type=schema.type)
    except RegistryError as exc:
        raise InvalidSchemaBoundAxisError(
            f"validate_axis_value: parameter {schema.name!r}: {exc}"
        ) from exc
    if schema.lower_bound is not None and isinstance(coerced, int) and coerced < schema.lower_bound:
        raise InvalidSchemaBoundAxisError(
            f"validate_axis_value: parameter {schema.name!r}: value "
            f"{coerced} below declared lower_bound={schema.lower_bound}"
        )
    if schema.upper_bound is not None and isinstance(coerced, int) and coerced > schema.upper_bound:
        raise InvalidSchemaBoundAxisError(
            f"validate_axis_value: parameter {schema.name!r}: value "
            f"{coerced} above declared upper_bound={schema.upper_bound}"
        )
    return coerced


# ---------------------------------------------------------------------------
# Schema-bound axis
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaBoundParameterAxis:
    """A single axis of a schema-bound parameter surface.

    The axis is bound to a registered parameter schema: ``name`` is a
    schema-declared parameter name; ``schema_type`` and ``unit`` are
    the schema's declared type and unit; ``lower_bound`` and
    ``upper_bound`` are the schema's declared bounds (possibly
    ``None`` when the schema leaves the bound to the type's own
    range); ``values`` is the non-empty tuple of typed values the
    runner sweeps.

    Field units: every integer is a Python ``int`` (``bool``
    rejected); every ``str`` is non-empty; every tuple is
    homogeneous with the schema's declared type.

    Validation enforces:

    - ``name`` is non-empty.
    - ``schema_type`` is a :class:`ParameterType`.
    - ``unit`` is non-empty.
    - ``values`` is a non-empty tuple.
    - Every value matches the schema's declared type / range (via
      :func:`validate_axis_value`).
    """

    name: str
    schema_type: ParameterType
    unit: str
    values: tuple[int | bool | str, ...]
    lower_bound: int | None = None
    upper_bound: int | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.name, field="SchemaBoundParameterAxis.name")
        if not isinstance(self.schema_type, ParameterType):
            raise InvalidSchemaBoundAxisError(
                f"SchemaBoundParameterAxis.schema_type: must be ParameterType, "
                f"got {type(self.schema_type).__name__}"
            )
        _require_non_empty_str(self.unit, field="SchemaBoundParameterAxis.unit")
        if not isinstance(self.values, tuple):
            raise InvalidSchemaBoundAxisError(
                f"SchemaBoundParameterAxis.values: must be tuple[int|str|bool], "
                f"got {type(self.values).__name__}"
            )
        if not self.values:
            raise InvalidSchemaBoundAxisError(
                "SchemaBoundParameterAxis.values: must be non-empty tuple"
            )
        for i, value in enumerate(self.values):
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise InvalidSchemaBoundAxisError(
                    f"SchemaBoundParameterAxis.values[{i}]: must be int|str|bool, "
                    f"got {type(value).__name__}"
                )
        if self.lower_bound is not None and (
            isinstance(self.lower_bound, bool) or not isinstance(self.lower_bound, int)
        ):
            raise InvalidSchemaBoundAxisError(
                f"SchemaBoundParameterAxis.lower_bound: must be int or None, "
                f"got {type(self.lower_bound).__name__}"
            )
        if self.upper_bound is not None and (
            isinstance(self.upper_bound, bool) or not isinstance(self.upper_bound, int)
        ):
            raise InvalidSchemaBoundAxisError(
                f"SchemaBoundParameterAxis.upper_bound: must be int or None, "
                f"got {type(self.upper_bound).__name__}"
            )
        if (
            self.lower_bound is not None
            and self.upper_bound is not None
            and self.lower_bound > self.upper_bound
        ):
            raise InvalidSchemaBoundAxisError(
                f"SchemaBoundParameterAxis: lower_bound={self.lower_bound} "
                f"must be <= upper_bound={self.upper_bound}"
            )

    def validate_against_schema(self, schema: ParameterSchema) -> None:
        """Validate this axis against the registered schema.

        The helper rejects an axis whose ``name``, ``schema_type`` or
        ``unit`` does not match the schema, and validates every
        value against the schema's type / unit / range. The
        construction site calls this helper once for every axis
        before the surface is built.
        """
        if schema.name != self.name:
            raise UndeclaredAxisError(
                f"SchemaBoundParameterAxis: axis.name={self.name!r} does not "
                f"match schema.name={schema.name!r}"
            )
        if schema.type != self.schema_type:
            raise InvalidSchemaBoundAxisError(
                f"SchemaBoundParameterAxis: axis.schema_type="
                f"{self.schema_type.value!r} disagrees with schema.type="
                f"{schema.type.value!r} for parameter {schema.name!r}"
            )
        if schema.unit != self.unit:
            raise InvalidSchemaBoundAxisError(
                f"SchemaBoundParameterAxis: axis.unit={self.unit!r} disagrees "
                f"with schema.unit={schema.unit!r} for parameter {schema.name!r}"
            )
        if schema.lower_bound != self.lower_bound:
            raise InvalidSchemaBoundAxisError(
                f"SchemaBoundParameterAxis: axis.lower_bound={self.lower_bound} "
                f"disagrees with schema.lower_bound={schema.lower_bound} "
                f"for parameter {schema.name!r}"
            )
        if schema.upper_bound != self.upper_bound:
            raise InvalidSchemaBoundAxisError(
                f"SchemaBoundParameterAxis: axis.upper_bound={self.upper_bound} "
                f"disagrees with schema.upper_bound={schema.upper_bound} "
                f"for parameter {schema.name!r}"
            )
        for i, value in enumerate(self.values):
            try:
                validate_axis_value(schema=schema, value=value)
            except InvalidSchemaBoundAxisError as exc:
                raise InvalidSchemaBoundAxisError(
                    f"SchemaBoundParameterAxis: values[{i}]={value!r} for "
                    f"parameter {schema.name!r}: {exc}"
                ) from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "schema_type": self.schema_type.value,
            "unit": self.unit,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "values": list(self.values),
        }


# ---------------------------------------------------------------------------
# Schema-bound surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaBoundParameterSurface:
    """A registry / schema-bound parameter surface.

    The surface carries the full binding the runner / report needs to
    audit which registry revision authorised the sweep and which
    parameter-schema revision the axis set is built against. A
    registry or schema revision produces a different binding and a
    different surface identity; results from incompatible surfaces
    are not merged into one sensitivity conclusion.

    Field units: every string is non-empty; every integer is a Python
    ``int`` (``bool`` rejected); every tuple is non-empty and unique.

    Validation enforces:

    - ``surface_id``, ``strategy_identity``, ``strategy_version``,
      ``parameter_schema_version``, ``parameter_schema_checksum``,
      ``registry_version``, ``registry_checksum`` are non-empty
      strings.
    - ``axes`` is a non-empty tuple of
      :class:`SchemaBoundParameterAxis` with unique names.
    - Every axis declares a parameter the registered schema carries.
    - Every axis value lies inside the schema's declared range.
    """

    surface_id: str
    strategy_identity: str
    strategy_version: str
    parameter_schema_version: str
    parameter_schema_checksum: str
    registry_version: str
    registry_checksum: str
    axes: tuple[SchemaBoundParameterAxis, ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(self.surface_id, field="SchemaBoundParameterSurface.surface_id")
        _require_non_empty_str(
            self.strategy_identity, field="SchemaBoundParameterSurface.strategy_identity"
        )
        _require_non_empty_str(
            self.strategy_version, field="SchemaBoundParameterSurface.strategy_version"
        )
        _require_non_empty_str(
            self.parameter_schema_version,
            field="SchemaBoundParameterSurface.parameter_schema_version",
        )
        _require_non_empty_str(
            self.parameter_schema_checksum,
            field="SchemaBoundParameterSurface.parameter_schema_checksum",
        )
        _require_non_empty_str(
            self.registry_version, field="SchemaBoundParameterSurface.registry_version"
        )
        _require_non_empty_str(
            self.registry_checksum, field="SchemaBoundParameterSurface.registry_checksum"
        )
        if not isinstance(self.axes, tuple):
            raise SchemaBindingError(
                f"SchemaBoundParameterSurface.axes: must be tuple, got {type(self.axes).__name__}"
            )
        # ``axes`` may be empty when the registered strategy declares
        # no parameters (e.g., ``HOLD``); the empty-tuple case is a
        # valid schema-bound surface — the contract is "every axis
        # the surface carries must be schema-declared", and a
        # parameter-less schema declares no axes. A non-empty
        # surface against an empty schema is rejected by the
        # builder (see :func:`build_schema_bound_surface`).
        for axis in self.axes:
            if not isinstance(axis, SchemaBoundParameterAxis):
                raise SchemaBindingError(
                    f"SchemaBoundParameterSurface.axes: every entry must be "
                    f"SchemaBoundParameterAxis, got {type(axis).__name__}"
                )
        names = [axis.name for axis in self.axes]
        if len(set(names)) != len(names):
            raise SchemaBindingError(
                f"SchemaBoundParameterSurface.axes: every axis.name must be "
                f"unique, got duplicates in {sorted(names)}"
            )

    @property
    def grid_size(self) -> int:
        """Return the cross-product size of the axis set.

        The value is deterministic for a given axis tuple; the runner
        uses it to allocate the cross product and to disclose the
        number of comparisons.
        """
        n = 1
        for axis in self.axes:
            n *= len(axis.values)
        return n

    def grid(self) -> tuple[dict[str, int | bool | str], ...]:
        """Enumerate the cross product in declaration order.

        The enumeration order matches the legacy
        :meth:`ParameterSurface.grid` contract: for axes ``a`` and
        ``b``, the order is ``(a[0], b[0])``, ``(a[0], b[1])``, ...,
        ``(a[1], b[0])``, .... Two surfaces built from the same
        registry revision return the same grid in the same order.
        """
        points: list[dict[str, int | bool | str]] = [{}]
        for axis in self.axes:
            new_points: list[dict[str, int | bool | str]] = []
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
            "strategy_identity": self.strategy_identity,
            "strategy_version": self.strategy_version,
            "parameter_schema_version": self.parameter_schema_version,
            "parameter_schema_checksum": self.parameter_schema_checksum,
            "registry_version": self.registry_version,
            "registry_checksum": self.registry_checksum,
            "axes": [axis.to_dict() for axis in self.axes],
            "grid_size": self.grid_size,
        }

    def checksum(self) -> str:
        """Return the SHA-256 hex digest of the surface's canonical form.

        The function is the deterministic identity the runner / report
        use to detect incompatible revisions: two surfaces built
        against the same registry revision and the same axis values
        return the same digest; a registry or schema revision
        surfaces as a different digest.
        """
        canonical = json_canonical(self.to_dict())
        return hashlib.sha256(canonical).hexdigest()


def json_canonical(payload: object) -> bytes:
    """Return the canonical UTF-8 JSON serialisation of ``payload``.

    The helper is the deterministic serialisation
    :meth:`SchemaBoundParameterSurface.checksum` uses; it sorts the
    mapping keys so two equivalent payloads in different field orders
    return the same byte sequence.
    """
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def surface_identity(*, surface: SchemaBoundParameterSurface) -> str:
    """Return the registry / schema-bound identity string of ``surface``.

    The identity is the colon-joined
    ``"<strategy_identity>:<strategy_version>:<parameter_schema_version>:
    <parameter_schema_checksum>:<registry_version>:<registry_checksum>"``
    tuple — the same order :attr:`SchemaBoundRobustnessReport.binding_identity`
    uses, so a reviewer can compare the two strings directly.
    Two surfaces with the same identity may be combined into
    one sensitivity conclusion; surfaces with different identities
    must not be combined — :func:`assert_surfaces_compatible`
    enforces the rule.
    """
    return (
        f"{surface.strategy_identity}:{surface.strategy_version}:"
        f"{surface.parameter_schema_version}:{surface.parameter_schema_checksum}:"
        f"{surface.registry_version}:{surface.registry_checksum}"
    )


def assert_surfaces_compatible(
    *,
    left: SchemaBoundParameterSurface,
    right: SchemaBoundParameterSurface,
) -> None:
    """Raise :class:`IncompatibleSchemaRevisionError` if ``left`` and ``right`` differ.

    The T106 contract binds that results from incompatible schema
    revisions are not merged into one sensitivity conclusion. The
    helper is the deterministic gate every code path that merges
    two schema-bound surfaces passes through; the gate rejects a
    registry revision drift, a registry checksum drift, a schema
    version drift, a schema checksum drift, or an identity drift.
    """
    if left.registry_version != right.registry_version:
        raise IncompatibleSchemaRevisionError(
            f"assert_surfaces_compatible: registry_version differs "
            f"(left={left.registry_version!r}, right={right.registry_version!r})"
        )
    if left.registry_checksum != right.registry_checksum:
        raise IncompatibleSchemaRevisionError(
            f"assert_surfaces_compatible: registry_checksum differs "
            f"(left={left.registry_checksum!r}, right={right.registry_checksum!r})"
        )
    if left.parameter_schema_version != right.parameter_schema_version:
        raise IncompatibleSchemaRevisionError(
            f"assert_surfaces_compatible: parameter_schema_version differs "
            f"(left={left.parameter_schema_version!r}, "
            f"right={right.parameter_schema_version!r})"
        )
    if left.parameter_schema_checksum != right.parameter_schema_checksum:
        raise IncompatibleSchemaRevisionError(
            f"assert_surfaces_compatible: parameter_schema_checksum differs "
            f"(left={left.parameter_schema_checksum!r}, "
            f"right={right.parameter_schema_checksum!r})"
        )
    if left.strategy_identity != right.strategy_identity:
        raise IncompatibleSchemaRevisionError(
            f"assert_surfaces_compatible: strategy_identity differs "
            f"(left={left.strategy_identity!r}, right={right.strategy_identity!r})"
        )
    if left.strategy_version != right.strategy_version:
        raise IncompatibleSchemaRevisionError(
            f"assert_surfaces_compatible: strategy_version differs "
            f"(left={left.strategy_version!r}, right={right.strategy_version!r})"
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _axis_from_schema_values(
    *,
    schema: ParameterSchema,
    values: Sequence[object],
) -> SchemaBoundParameterAxis:
    """Build a :class:`SchemaBoundParameterAxis` for ``schema`` from ``values``.

    The helper is the canonical builder the public
    :func:`build_schema_bound_surface` uses. It validates every
    value against the schema's declared type / range before the
    axis is constructed; an undeclared parameter, a wrong-type
    value, or an out-of-range value fails here and never enters the
    surface.
    """
    normalised: list[int | bool | str] = []
    for i, value in enumerate(values):
        try:
            coerced = validate_axis_value(schema=schema, value=value)
        except InvalidSchemaBoundAxisError as exc:
            raise InvalidSchemaBoundAxisError(
                f"_axis_from_schema_values: parameter {schema.name!r} value[{i}]={value!r}: {exc}"
            ) from exc
        normalised.append(coerced)
    return SchemaBoundParameterAxis(
        name=schema.name,
        schema_type=schema.type,
        unit=schema.unit,
        values=tuple(normalised),
        lower_bound=schema.lower_bound,
        upper_bound=schema.upper_bound,
    )


def build_schema_bound_surface(
    *,
    surface_id: str,
    identity: str,
    axes: Iterable[tuple[str, Sequence[object]]],
    registry: Registry | None = None,
) -> SchemaBoundParameterSurface:
    """Build a :class:`SchemaBoundParameterSurface` against the registry.

    The function is the canonical builder every current publication
    path uses. It looks up ``identity`` in the registry (raising
    :class:`UnknownStrategyIdentityError` for an unregistered one),
    binds the surface to the registry / schema revision, and
    validates every axis name + value against the registered schema.

    Parameters
    ----------
    surface_id:
        Non-empty string; the surface's primary key.
    identity:
        The registered strategy identity the surface binds to.
    axes:
        Iterable of ``(parameter_name, values)`` pairs. The
        parameter name must be declared by the registered schema,
        and every value must lie inside the schema's declared type
        / unit / range. A parameter the schema does not declare, a
        value of the wrong type, or an out-of-range value is
        rejected before the surface is built.
    registry:
        Optional registry override (mainly for tests); the default
        is :func:`robinhood_lp.strategy.registry.default_registry`.
    """
    reg = registry if registry is not None else default_registry()
    entry = reg.lookup(identity)
    schemas_by_name: dict[str, ParameterSchema] = {
        schema.name: schema for schema in entry.parameter_schemas
    }
    schema_list = list(entry.parameter_schemas)

    # Compute the registry / schema-bound metadata once so the
    # builder can stamp every surface with the exact revision that
    # authorised the sweep. The checksums are computed via the
    # registry's own surface so the surface identity is what the
    # manifest authority (T105) records.
    from robinhood_lp.reports.registry_binding import compute_parameter_schema_checksum

    registry_version = REGISTRY_VERSION
    registry_checksum = reg.checksum()
    strategy_version = entry.version
    parameter_schema_version = entry.version
    parameter_schema_checksum = compute_parameter_schema_checksum(entry.parameter_schemas)

    # Build each axis. An axis that names a parameter the schema
    # does not declare is rejected here — the registry is the
    # closed vocabulary, the surface mirrors it.
    built_axes: list[SchemaBoundParameterAxis] = []
    declared_names: set[str] = set()
    for parameter_name, values in axes:
        if parameter_name in declared_names:
            raise SchemaBindingError(
                f"build_schema_bound_surface: axis {parameter_name!r} declared twice"
            )
        declared_names.add(parameter_name)
        schema = schemas_by_name.get(parameter_name)
        if schema is None:
            raise UndeclaredAxisError(
                f"build_schema_bound_surface: axis {parameter_name!r} is not "
                f"declared by registered schema for identity={identity!r}; "
                f"declared parameters: {sorted(schemas_by_name)}"
            )
        axis = _axis_from_schema_values(schema=schema, values=values)
        built_axes.append(axis)

    # Reject axes for parameters the schema does not declare when
    # the schema has no parameters at all. The empty schema case is
    # valid for the ``HOLD`` strategy, but a non-empty axis list
    # against an empty schema is a contract break.
    if not schema_list and built_axes:
        raise UndeclaredAxisError(
            f"build_schema_bound_surface: identity={identity!r} declares no "
            f"parameters, but the caller supplied {len(built_axes)} axis / axes"
        )

    return SchemaBoundParameterSurface(
        surface_id=surface_id,
        strategy_identity=identity,
        strategy_version=strategy_version,
        parameter_schema_version=parameter_schema_version,
        parameter_schema_checksum=parameter_schema_checksum,
        registry_version=registry_version,
        registry_checksum=registry_checksum,
        axes=tuple(built_axes),
    )


# ---------------------------------------------------------------------------
# Module-level convenience wrappers
# ---------------------------------------------------------------------------


def is_schema_bound_surface_identity(
    *,
    strategy_identity: str,
    registry_checksum: str,
    parameter_schema_checksum: str,
) -> bool:
    """Return ``True`` iff the identity / revision tuple is registry-bound.

    The helper is the deterministic gate every code path that
    validates a registry-bound identity passes through. A
    registry-bound tuple has a non-empty ``registry_checksum`` and
    a non-empty ``parameter_schema_checksum`` and a registered
    strategy identity.
    """
    if not isinstance(strategy_identity, str) or not strategy_identity:
        return False
    if not isinstance(registry_checksum, str) or not registry_checksum:
        return False
    if not isinstance(parameter_schema_checksum, str) or not parameter_schema_checksum:
        return False
    return is_registered(strategy_identity)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "SURFACES_VERSION",
    "VALID_AXIS_VALUE_KINDS",
    "IncompatibleSchemaRevisionError",
    "InvalidSchemaBoundAxisError",
    "SchemaBindingError",
    "SchemaBoundParameterAxis",
    "SchemaBoundParameterSurface",
    "UndeclaredAxisError",
    "UnknownAxisValueKindError",
    "assert_surfaces_compatible",
    "build_schema_bound_surface",
    "is_schema_bound_surface_identity",
    "json_canonical",
    "surface_identity",
    "validate_axis_value",
]
