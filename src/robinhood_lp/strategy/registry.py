"""V1 strategy registry — stable identities, parameter schemas, code provenance (T068).

This module is the deterministic validation surface the T105 manifest
authority binds published manifests to. Every strategy a backtest or
paper run may execute has one registered identity in the registry
returned by :func:`default_registry`; the registry is the only path
the manifest authority uses to translate an identity string into its
parameter schema, version and code provenance.

Design contract (binding):

- **Closed identity vocabulary.** The registry enumerates every
  strategy a backtest or paper run may execute and no others. The
  set is the closed set the strategy layer promises the manifest
  layer; publishing a manifest with an identity outside the
  registry fails validation, and the registry does not accept a
  caller-supplied or caller-invented identity at lookup time.

- **Per-identity parameter schema.** Each entry carries its own
  :class:`ParameterSchema` declaring every parameter the strategy
  accepts (name, type, unit, allowed range / set, default). The
  registry's :func:`validate_parameters` rejects a parameter set
  that names a parameter the schema does not declare, omits a
  parameter the schema requires, or carries a value outside the
  declared type / unit / range. Validation is structural: a value
  that survives the schema's per-field checks is treated as a
  well-formed parameter set for that identity.

- **Version + code provenance.** Each entry carries a
  version string and a :class:`CodeProvenance` record naming the
  module that implements the strategy and the code revision the
  module was built from. The module is resolved at registry
  construction time so the registry is decoupled from a live
  strategy factory at lookup time — a registry built from the same
  revision returns the same provenance, regardless of which
  factory the caller instantiates.

- **Deterministic enumeration.** The registry's enumeration order
  is the sorted insertion order of the identities (Python
  ``dict`` preserves insertion order; the constructor sorts the
  insertion sequence before building the frozen map). The
  :func:`registry_checksum` function returns the SHA-256 hex of a
  canonical, version-tagged serialisation of the registry; two
  enumerations over the same revision return the same checksum.

- **Layer purity.** The registry imports the stdlib and the
  lower protocol-domain contracts only. It does not import RPC,
  storage, configuration, signing, execution, replay, presentation,
  or the Web entry point; the dependency tests in
  ``tests/test_strategy_t068.py`` enforce this by walking the live
  module graph.

References:

- ``docs/spec/architecture/ARCHITECTURE.md`` §2.2 (the strategy
  layer this module belongs to).
- ``docs/spec/strategy/STRATEGY_ECONOMICS.md`` §7 (the component
  boundary; the registry is the surface that binds a registered
  strategy to its replaceable components).
- ``docs/spec/product/WEB_CONSOLE.md`` ``WEB-PAGE-012`` (the
  console surface that lists registered strategies — the
  registry is the backend).
- ADR-006 — the strategy layer may not import RPC, storage,
  signing, or execution; the registry preserves the rule.
- T060 — the strategy contracts whose implementations the registry
  names.
- T062 — the baseline strategies the registry enumerates.
- T065 — the adaptive-Range strategy the registry registers as
  ``t065.adaptive_range.v1``.
- T105 — the manifest authority that consumes this registry.
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import ModuleType
from typing import Final, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Module version
# ---------------------------------------------------------------------------

#: Module version. Bumping the version is a breaking change for
#: downstream consumers (T105, the Web console surface ``WEB-PAGE-012``,
#: the manifest authority). The constant names the registry revision
#: the manifest authority binds to; a release that revises the
#: identity vocabulary or the parameter schemas must bump the
#: version and the manifest authority's release notes.
REGISTRY_VERSION: Final[str] = "t068.strategy_registry.v1"

#: Q64.64 fixed-point scale; mirrors :data:`Q64_SCALE` in
#: :mod:`robinhood_lp.strategy.base`. The registry re-declares the
#: constant so its parameter-schema validators do not import a
#: private symbol from a sibling strategy module.
_Q64_SCALE: Final[int] = 1 << 64

#: Maximum Q64.64 value a parameter the registry accepts may
#: carry. Mirrors the project-wide ceiling :data:`MAX_CAPITAL_Q64_64`
#: in :mod:`robinhood_lp.strategy.base`; the registry duplicates the
#: constant to avoid importing a private symbol from a sibling
#: strategy module.
_MAX_Q64_64_CAPITAL: Final[int] = _Q64_SCALE << 64  # 2**128

#: Lower bound for ``tick_spacing`` parameters (mirrors
#: :data:`MIN_TICK_SPACING` in :mod:`robinhood_lp.protocol.math`).
_MIN_TICK_SPACING_REGISTRY: Final[int] = 1

#: Upper bound for ``tick_spacing`` parameters (mirrors
#: :data:`MAX_TICK_SPACING` in :mod:`robinhood_lp.protocol.math`).
_MAX_TICK_SPACING_REGISTRY: Final[int] = 32_767

#: V4 int24 tick domain lower bound (mirrors :data:`MIN_TICK` in
#: :mod:`robinhood_lp.protocol.math`).
_MIN_TICK_DOMAIN_REGISTRY: Final[int] = -(1 << 23)

#: V4 int24 tick domain upper bound (mirrors :data:`MAX_TICK` in
#: :mod:`robinhood_lp.protocol.math`).
_MAX_TICK_DOMAIN_REGISTRY: Final[int] = (1 << 23) - 1

#: Lower bound for the strategy's ``liquidity`` parameter (V4 uint128).
_MIN_LIQUIDITY: Final[int] = 1

#: Upper bound for the strategy's ``liquidity`` parameter (V4 uint128).
_MAX_LIQUIDITY: Final[int] = (1 << 128) - 1

#: Lower bound for any integer-valued parameter the schema records
#: as ``INT`` with no upper bound. ``INT`` is unbounded in the upper
#: direction; the lower bound is the Python ``int`` floor the
#: validator accepts (``-(2**63)`` is enough to cover every
#: realistic strategy parameter; the validator rejects anything
#: smaller to keep the integer type narrow and the JSON
#: serialisation stable).
_INT_LOWER_BOUND: Final[int] = -(1 << 63)

#: Upper bound for any integer-valued parameter the schema records
#: as ``INT`` with no upper bound.
_INT_UPPER_BOUND: Final[int] = (1 << 63) - 1

# ---------------------------------------------------------------------------
# Module denylist (strategy layer purity)
# ---------------------------------------------------------------------------
#
# The strategy layer is permitted to import the standard library and
# the lower protocol-domain contracts module. Any other
# ``robinhood_lp`` subpackage is a contract break that must be
# reviewed. The registry is intentionally narrower than the strategy
# layer's general denylist — it must not import the implementation
# modules it registers (:mod:`robinhood_lp.strategy.baselines` and
# :mod:`robinhood_lp.strategy.adaptive`) at lookup time, only at
# construction time, so a caller-supplied / caller-invented identity
# cannot bypass the registry's closed vocabulary by referencing an
# arbitrary strategy module.
_FORBIDDEN_REGISTRY_ROBINHOOD_MODULES: Final[tuple[str, ...]] = (
    "robinhood_lp.backtest",
    "robinhood_lp.config",
    "robinhood_lp.discovery",
    "robinhood_lp.execution",
    "robinhood_lp.features",
    "robinhood_lp.ingestion",
    "robinhood_lp.presentation",
    "robinhood_lp.qualification",
    "robinhood_lp.quality",
    "robinhood_lp.replay",
    "robinhood_lp.risk",
    "robinhood_lp.rpc",
    "robinhood_lp.signer",
    "robinhood_lp.storage",
    "robinhood_lp.strategy.adaptive",
    "robinhood_lp.strategy.baselines",
    "robinhood_lp.strategy.adapter",
    "robinhood_lp.web",
)


# ---------------------------------------------------------------------------
# Parameter type vocabulary
# ---------------------------------------------------------------------------


class ParameterType(StrEnum):
    """The closed vocabulary of parameter types a :class:`ParameterSchema` declares.

    The vocabulary covers every type a V1 strategy parameter may
    declare; adding a new type is a breaking change for the registry
    surface. Strings are part of the public contract.

    The type names mirror the validator helpers
    :mod:`robinhood_lp.strategy.base` defines so the registry's
    validation surface agrees with the strategy layer's existing
    type guard.

    - ``INT`` — a Python ``int`` in ``[_INT_LOWER_BOUND, _INT_UPPER_BOUND]``.
      ``bool`` is rejected.
    - ``NON_NEGATIVE_INT`` — ``int >= 0`` (``bool`` rejected).
    - ``POSITIVE_INT`` — ``int > 0`` (``bool`` rejected).
    - ``STRICT_Q64_64`` — ``int > 0`` with no upper bound above
      ``_MAX_Q64_64_CAPITAL``. The Q64.64 fixed-point scale is the
      ADR-014 numeraire boundary; a parameter the strategy treats
      as a USDG amount speaks this type.
    - ``Q64_64`` — ``int >= 0`` with no upper bound above
      ``_MAX_Q64_64_CAPITAL``. Use this for dimensionless
      fractions (e.g., a threshold) where zero is meaningful.
    - ``TICK`` — ``int`` in the V4 ``int24`` tick domain
      ``[_MIN_TICK_DOMAIN_REGISTRY, _MAX_TICK_DOMAIN_REGISTRY]``.
    - ``TICK_SPACING`` — ``int`` in
      ``[_MIN_TICK_SPACING_REGISTRY, _MAX_TICK_SPACING_REGISTRY]``.
    - ``LIQUIDITY`` — ``int`` in V4 uint128 ``[1, _MAX_LIQUIDITY]``.
    - ``STR`` — a non-empty Python ``str``.
    - ``BOOL`` — a Python ``bool``.
    """

    INT = "INT"
    NON_NEGATIVE_INT = "NON_NEGATIVE_INT"
    POSITIVE_INT = "POSITIVE_INT"
    STRICT_Q64_64 = "STRICT_Q64_64"
    Q64_64 = "Q64_64"
    TICK = "TICK"
    TICK_SPACING = "TICK_SPACING"
    LIQUIDITY = "LIQUIDITY"
    STR = "STR"
    BOOL = "BOOL"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RegistryError(ValueError):
    """Base class for registry construction / validation failures."""


class UnknownStrategyIdentityError(RegistryError):
    """An identity string is not present in the registry.

    The registry is the closed vocabulary; an identity outside the
    registry is rejected with no fallback. The error message names
    the unknown identity and the set of registered identities so a
    caller can see what they had available.
    """


class InvalidParameterSchemaError(RegistryError):
    """A :class:`ParameterSchema` field violates its invariant."""


class InvalidParameterValueError(RegistryError):
    """A supplied parameter value violates the registered schema."""


class InvalidCodeProvenanceError(RegistryError):
    """A :class:`CodeProvenance` field violates its invariant."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_non_empty_str(value: str, *, field: str) -> str:
    """Validate ``value`` is a non-empty Python ``str``."""
    if not isinstance(value, str) or not value:
        raise RegistryError(f"{field}: must be non-empty str, got {value!r}")
    return value


def _require_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a Python ``int`` (``bool`` rejected)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise RegistryError(f"{field}: must be int, got {type(value).__name__}")
    return value


def _require_non_negative_int(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value < 0:
        raise RegistryError(f"{field}: must be non-negative, got {value}")
    return value


def _require_positive_int(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value <= 0:
        raise RegistryError(f"{field}: must be positive, got {value}")
    return value


def _require_bool(value: bool, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise RegistryError(f"{field}: must be bool, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Parameter schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParameterSchema:
    """The declared schema for one named parameter of a registered strategy.

    The schema names a single parameter, declares its type, units,
    default value and (optionally) a numeric lower / upper bound.
    A parameter without ``lower_bound`` / ``upper_bound`` is
    unbounded in that direction (within the type's own bounds). The
    schema is the only place a parameter's allowed range is
    declared; :func:`validate_parameters` consults every schema
    field on every registered parameter.

    Equality and hashing follow dataclass identity. Two schemas
    are equal iff every field is equal.
    """

    name: str
    type: ParameterType
    unit: str
    default: int | bool | str
    lower_bound: int | None = None
    upper_bound: int | None = None
    description: str = ""

    def __post_init__(self) -> None:
        try:
            _require_non_empty_str(self.name, field="ParameterSchema.name")
            _require_non_empty_str(self.unit, field="ParameterSchema.unit")
            if not isinstance(self.type, ParameterType):
                raise InvalidParameterSchemaError(
                    f"ParameterSchema.type: must be ParameterType, got {type(self.type).__name__}"
                )
            # ``lower_bound`` / ``upper_bound`` are optional but bounded
            # together: when both are present, ``lower <= upper``; when
            # one is present, the type's own bounds supply the missing
            # side. ``INT``-typed schemas reject non-``int`` bounds.
            if self.lower_bound is not None:
                _require_int(self.lower_bound, field="ParameterSchema.lower_bound")
            if self.upper_bound is not None:
                _require_int(self.upper_bound, field="ParameterSchema.upper_bound")
            if (
                self.lower_bound is not None
                and self.upper_bound is not None
                and self.lower_bound > self.upper_bound
            ):
                raise InvalidParameterSchemaError(
                    f"ParameterSchema: lower_bound={self.lower_bound} must be <= "
                    f"upper_bound={self.upper_bound}"
                )
            if not isinstance(self.description, str):
                raise InvalidParameterSchemaError(
                    f"ParameterSchema.description: must be str, got "
                    f"{type(self.description).__name__}"
                )
            # Validate ``default`` against the declared type. The
            # validator enforces the type's own bounds plus the optional
            # ``lower_bound`` / ``upper_bound`` range. A default that
            # fails the type check is a contract violation at schema
            # construction time, not a parameter-validation error.
            try:
                coerced = _coerce_parameter_value(self.default, expected_type=self.type)
            except InvalidParameterValueError as exc:
                raise InvalidParameterSchemaError(
                    f"ParameterSchema.default for {self.name!r}: {exc}"
                ) from exc
            if (
                self.lower_bound is not None
                and isinstance(coerced, int)
                and coerced < self.lower_bound
            ):
                raise InvalidParameterSchemaError(
                    f"ParameterSchema.default for {self.name!r}: value {coerced} "
                    f"below declared lower_bound={self.lower_bound}"
                )
            if (
                self.upper_bound is not None
                and isinstance(coerced, int)
                and coerced > self.upper_bound
            ):
                raise InvalidParameterSchemaError(
                    f"ParameterSchema.default for {self.name!r}: value {coerced} "
                    f"above declared upper_bound={self.upper_bound}"
                )
        except InvalidParameterSchemaError:
            raise
        except RegistryError as exc:
            # ``_require_*`` helpers raise :class:`RegistryError`; a
            # malformed schema field is a schema error, not a generic
            # registry error.
            raise InvalidParameterSchemaError(str(exc)) from exc

    @property
    def is_required(self) -> bool:
        """``True`` iff the schema is required by the registered strategy.

        The registry convention is: every parameter a schema
        declares is required. Optional parameters would require
        the registry to model absence ``None`` explicitly; V1
        does not support optional parameters, so the property is
        always ``True`` for a well-formed schema.
        """
        return True


def _type_bounds(parameter_type: ParameterType) -> tuple[int, int]:
    """Return the ``(lower_bound, upper_bound)`` integer range for ``parameter_type``.

    The bounds are inclusive. The function is the single source of
    truth for the integer range every :class:`ParameterType`
    accepts; ``STR`` and ``BOOL`` are not integer-typed and the
    function raises.
    """
    if parameter_type == ParameterType.INT:
        return _INT_LOWER_BOUND, _INT_UPPER_BOUND
    if parameter_type == ParameterType.NON_NEGATIVE_INT:
        return 0, _INT_UPPER_BOUND
    if parameter_type == ParameterType.POSITIVE_INT:
        return 1, _INT_UPPER_BOUND
    if parameter_type == ParameterType.STRICT_Q64_64:
        return 1, _MAX_Q64_64_CAPITAL
    if parameter_type == ParameterType.Q64_64:
        return 0, _MAX_Q64_64_CAPITAL
    if parameter_type == ParameterType.TICK:
        return _MIN_TICK_DOMAIN_REGISTRY, _MAX_TICK_DOMAIN_REGISTRY
    if parameter_type == ParameterType.TICK_SPACING:
        return _MIN_TICK_SPACING_REGISTRY, _MAX_TICK_SPACING_REGISTRY
    if parameter_type == ParameterType.LIQUIDITY:
        return _MIN_LIQUIDITY, _MAX_LIQUIDITY
    raise RegistryError(f"_type_bounds: type {parameter_type!r} is not integer-bounded")


def _coerce_parameter_value(value: object, *, expected_type: ParameterType) -> int | bool | str:
    """Validate ``value`` matches ``expected_type`` and return the typed value.

    The function enforces the type's own bounds plus the optional
    ``lower_bound`` / ``upper_bound`` schema fields the caller
    supplies. ``STR`` values are accepted as non-empty strings;
    ``BOOL`` values are accepted as Python ``bool`` (an ``int``
    that happens to be ``0`` or ``1`` is rejected as a type
    mismatch — a registered strategy asks for ``bool``, not for
    ``int``).
    """
    if expected_type == ParameterType.STR:
        if not isinstance(value, str) or not value:
            raise InvalidParameterValueError(f"expected non-empty str, got {value!r}")
        return value
    if expected_type == ParameterType.BOOL:
        if not isinstance(value, bool):
            raise InvalidParameterValueError(f"expected bool, got {type(value).__name__}")
        return value
    # All remaining types are integer-valued.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidParameterValueError(
            f"expected {expected_type.value} (int), got {type(value).__name__}"
        )
    type_lower, type_upper = _type_bounds(expected_type)
    if value < type_lower or value > type_upper:
        raise InvalidParameterValueError(
            f"value {value} outside type {expected_type.value} bounds [{type_lower}, {type_upper}]"
        )
    return value


def _validate_value_against_schema(
    *,
    value: object,
    schema: ParameterSchema,
    identity: str,
) -> int | bool | str:
    """Validate a single parameter value against its schema entry.

    On success the function returns the coerced value (a Python
    ``int`` for the numeric types, the original ``str`` / ``bool``
    for the corresponding types). On failure it raises
    :class:`InvalidParameterValueError` with the registry identity
    and the parameter name attached, so the caller can build a
    structured, multi-parameter error report.
    """
    try:
        coerced = _coerce_parameter_value(value, expected_type=schema.type)
    except InvalidParameterValueError as exc:
        raise InvalidParameterValueError(
            f"strategy {identity!r}: parameter {schema.name!r}: {exc}"
        ) from exc
    if schema.lower_bound is not None and isinstance(coerced, int) and coerced < schema.lower_bound:
        raise InvalidParameterValueError(
            f"strategy {identity!r}: parameter {schema.name!r}: value "
            f"{coerced} below declared lower_bound={schema.lower_bound}"
        )
    if schema.upper_bound is not None and isinstance(coerced, int) and coerced > schema.upper_bound:
        raise InvalidParameterValueError(
            f"strategy {identity!r}: parameter {schema.name!r}: value "
            f"{coerced} above declared upper_bound={schema.upper_bound}"
        )
    return coerced


# ---------------------------------------------------------------------------
# Code provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CodeProvenance:
    """The code-revision provenance a registered strategy binds to.

    The provenance names the module that implements the strategy
    (e.g., ``robinhood_lp.strategy.baselines``) and the code
    revision that module was built from (the Git commit SHA the
    registry captured at construction time). The module name is
    stored as the dotted import path so the manifest authority can
    resolve the module without a network call.

    Equality and hashing follow dataclass identity. Two provenance
    records are equal iff every field is equal.
    """

    module: str
    revision: str
    symbol: str = ""

    def __post_init__(self) -> None:
        _require_non_empty_str(self.module, field="CodeProvenance.module")
        if not isinstance(self.revision, str):
            raise InvalidCodeProvenanceError(
                f"CodeProvenance.revision: must be str, got {type(self.revision).__name__}"
            )
        if not self.revision:
            raise InvalidCodeProvenanceError(
                f"CodeProvenance.revision: must be non-empty str, got {self.revision!r}"
            )
        if not isinstance(self.symbol, str):
            raise InvalidCodeProvenanceError(
                f"CodeProvenance.symbol: must be str, got {type(self.symbol).__name__}"
            )

    def matches_module(self, *, module_name: str) -> bool:
        """Return ``True`` iff ``module_name`` matches the provenance's module.

        The helper supports prefix matching so the manifest
        authority can compare a module path the strategy surface
        advertises against the registry's stored module name. An
        exact match always succeeds; a strict prefix with a
        trailing ``.`` (e.g., ``robinhood_lp.strategy.baselines.x``
        matches ``robinhood_lp.strategy.baselines``) succeeds;
        a name with no ``.`` boundary (e.g.,
        ``robinhood_lp.strategy.baselinesx``) does not match.
        """
        if not isinstance(module_name, str) or not module_name:
            return False
        if module_name == self.module:
            return True
        return module_name.startswith(self.module + ".")


def resolve_module_revision(*, module_name: str) -> str:
    """Return the current Git HEAD revision for ``module_name``.

    The helper is the canonical way the registry resolves a code
    revision at construction time. It reads the current commit
    SHA from the Git repository that contains the module's source
    file; if the revision cannot be read (no Git checkout, no
    HEAD), the helper returns ``"UNKNOWN"`` and the registry
    records the unknown provenance rather than fabricating a
    value.

    The function walks from the module's ``__file__`` upward to
    the first ``.git`` entry (file or directory) and reads the
    HEAD reference from there. A ``.git`` *file* is the worktree
    marker Git uses for sub-worktrees; its first line
    (``gitdir: <absolute path>``) names the directory that holds
    the actual ``HEAD`` / ``refs`` / ``packed-refs``. The helper
    follows the ``gitdir:`` pointer so a worktree whose
    ``.git`` is a file resolves the same way as a checkout whose
    ``.git`` is a directory. The helper never reads the network,
    never imports the module being queried beyond looking up
    ``__file__``, and never modifies the working tree.
    """

    def _walk_to_worktree(start: Path) -> Path | None:
        """Walk up from ``start`` until a ``.git`` file or directory is found."""
        cursor: Path | None = start
        while cursor is not None:
            git_marker = cursor / ".git"
            if git_marker.is_dir() or git_marker.is_file():
                return cursor
            cursor = cursor.parent
        return None

    def _resolve_git_dir(worktree: Path) -> Path | None:
        """Return the directory that holds the HEAD / refs for ``worktree``.

        Returns ``worktree/.git`` when the marker is a directory;
        follows a ``gitdir:`` pointer when the marker is a worktree
        file.
        """
        marker = worktree / ".git"
        if marker.is_dir():
            return marker
        if marker.is_file():
            try:
                first_line = marker.read_text(encoding="utf-8").splitlines()[0]
            except (OSError, IndexError):
                return None
            if first_line.startswith("gitdir: "):
                pointed = first_line[len("gitdir: ") :].strip()
                # ``gitdir:`` may carry a path relative to the
                # worktree root; resolve against ``worktree``.
                pointed_path = Path(pointed)
                if not pointed_path.is_absolute():
                    pointed_path = (worktree / pointed_path).resolve()
                return pointed_path
        return None

    def _read_head(git_dir: Path) -> str | None:
        head_file = git_dir / "HEAD"
        if not head_file.is_file():
            return None
        try:
            head_text = head_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        # A worktree's gitdir may carry a ``commondir`` entry that
        # points to the directory holding the shared refs / packed-
        # refs. Resolve it once and use the common dir as the
        # primary ref lookup, falling back to the gitdir itself.
        common_dir = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.is_file():
            try:
                common_rel = commondir_file.read_text(encoding="utf-8").strip()
            except OSError:
                common_rel = ""
            if common_rel:
                common_path = Path(common_rel)
                if not common_path.is_absolute():
                    common_path = (git_dir / common_path).resolve()
                if common_path.is_dir():
                    common_dir = common_path
        if head_text.startswith("ref: "):
            ref_name = head_text[len("ref: ") :]
            ref_path = common_dir / ref_name
            if ref_path.is_file():
                try:
                    return ref_path.read_text(encoding="utf-8").strip()
                except OSError:
                    return None
            packed = common_dir / "packed-refs"
            if packed.is_file():
                try:
                    packed_text = packed.read_text(encoding="utf-8")
                except OSError:
                    return None
                for line in packed_text.splitlines():
                    if line.startswith("#"):
                        continue
                    parts = line.split(" ")
                    if len(parts) >= 2 and parts[1] == ref_name:
                        return parts[0]
            return None
        return head_text

    repo_root: Path | None = None
    try:
        from pathlib import Path  # local import: stdlib-only path

        module = importlib.import_module(module_name)
        module_file = getattr(module, "__file__", None)
        if isinstance(module_file, str) and module_file:
            repo_root = _walk_to_worktree(Path(module_file).resolve().parent)
    except (ImportError, OSError):
        repo_root = None
    if repo_root is None:
        return "UNKNOWN"
    git_dir = _resolve_git_dir(repo_root)
    if git_dir is None:
        return "UNKNOWN"
    head = _read_head(git_dir)
    if head is None:
        return "UNKNOWN"
    return head


# ---------------------------------------------------------------------------
# Strategy factory protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StrategyFactory(Protocol):
    """The factory interface the registry's parameter validator consumes.

    The factory is the bridge between a parameter set the registry
    validated and a runtime strategy object the backtest / paper
    pipeline can execute. A registered :class:`RegisteredStrategy`
    carries one factory; the factory's :meth:`build` accepts the
    identity-binding fields (``pool_key_id``, ``chain_id``) and the
    validated parameter set and returns a constructed strategy.

    A model-backed factory (T102 / P10) satisfies the same
    protocol as a rule-based factory. The registry does not branch
    on factory type: the validator calls ``factory.build`` with
    the validated parameters and never inspects the constructed
    strategy's internals.

    The protocol is ``runtime_checkable``; ``isinstance(factory,
    StrategyFactory)`` therefore accepts any object that exposes
    a :meth:`build` method with the documented signature. A bare
    function does **not** satisfy :class:`StrategyFactory`: the
    registry requires a method-bearing object so a caller cannot
    bypass the protocol by handing the registry a free function
    the validator cannot introspect.
    """

    def build(
        self,
        *,
        parameters: Mapping[str, int | bool | str],
        pool_key_id: str,
        chain_id: int,
    ) -> object:
        """Build a strategy instance from ``parameters`` and the binding fields."""
        ...


class _AdapterFactory:
    """Adapter that wraps a plain function as a :class:`StrategyFactory`.

    The registry's :class:`StrategyFactory` protocol requires an
    object that exposes a :meth:`build` method; the registry
    ships plain factory functions for each registered strategy and
    wraps each one in a :class:`_AdapterFactory` instance so the
    ``isinstance`` check at construction time succeeds without
    changing the function-shaped contract the factories expose.
    """

    def __init__(self, function: Callable[..., object]) -> None:
        _require_non_empty_str(
            function.__name__ if hasattr(function, "__name__") else "", field="function.__name__"
        )
        self._function = function

    def build(
        self,
        *,
        parameters: Mapping[str, int | bool | str],
        pool_key_id: str,
        chain_id: int,
    ) -> object:
        return self._function(
            parameters=parameters,
            pool_key_id=pool_key_id,
            chain_id=chain_id,
        )


# ---------------------------------------------------------------------------
# Registered strategy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegisteredStrategy:
    """One entry of the strategy registry.

    The entry binds a stable identity string to a version, a
    parameter schema, a code provenance and a factory. The
    :class:`Registry` is an immutable, ordered mapping from
    identity to :class:`RegisteredStrategy`; the order is
    insertion order, and the registry's :func:`registry_checksum`
    is computed from a sorted-by-identity enumeration so two
    enumerations over the same revision return the same checksum.

    Equality and hashing follow dataclass identity.
    """

    identity: str
    version: str
    parameter_schemas: tuple[ParameterSchema, ...]
    code_provenance: CodeProvenance
    factory: StrategyFactory
    description: str = ""

    def __post_init__(self) -> None:
        _require_non_empty_str(self.identity, field="RegisteredStrategy.identity")
        _require_non_empty_str(self.version, field="RegisteredStrategy.version")
        if not isinstance(self.parameter_schemas, tuple):
            raise RegistryError(
                f"RegisteredStrategy.parameter_schemas: must be tuple, got "
                f"{type(self.parameter_schemas).__name__}"
            )
        seen_names: set[str] = set()
        for schema in self.parameter_schemas:
            if not isinstance(schema, ParameterSchema):
                raise RegistryError(
                    f"RegisteredStrategy.parameter_schemas: every entry must be "
                    f"ParameterSchema, got {type(schema).__name__}"
                )
            if schema.name in seen_names:
                raise RegistryError(
                    f"RegisteredStrategy.parameter_schemas: duplicate parameter "
                    f"name {schema.name!r}"
                )
            seen_names.add(schema.name)
        if not isinstance(self.code_provenance, CodeProvenance):
            raise RegistryError(
                f"RegisteredStrategy.code_provenance: must be CodeProvenance, "
                f"got {type(self.code_provenance).__name__}"
            )
        if not isinstance(self.factory, StrategyFactory):
            raise RegistryError(
                f"RegisteredStrategy.factory: must implement StrategyFactory, "
                f"got {type(self.factory).__name__}"
            )
        if not isinstance(self.description, str):
            raise RegistryError(
                f"RegisteredStrategy.description: must be str, got "
                f"{type(self.description).__name__}"
            )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Registry:
    """The immutable, ordered mapping of registered strategy identities.

    The registry is constructed from an iterable of
    :class:`RegisteredStrategy` entries; the constructor sorts the
    input sequence by identity before building the frozen map so
    two enumerations over the same set return the same checksum
    regardless of the caller's input order. The registry is the
    only path the manifest authority uses to translate an
    identity string into its parameter schema, version and code
    provenance.

    Equality and hashing follow dataclass identity; two registries
    built from the same set of entries return the same hash.
    """

    _entries: tuple[RegisteredStrategy, ...] = field(default_factory=tuple)
    _by_identity: Mapping[str, RegisteredStrategy] = field(default_factory=dict)

    def __post_init__(self) -> None:
        entries_iter = self._entries
        if not isinstance(entries_iter, tuple):
            raise RegistryError(
                f"Registry._entries: must be tuple, got {type(entries_iter).__name__}"
            )
        # Validate every entry *before* the sort so a malformed
        # entry surfaces as :class:`RegistryError` instead of an
        # opaque ``AttributeError`` from ``entry.identity``.
        for entry in entries_iter:
            if not isinstance(entry, RegisteredStrategy):
                raise RegistryError(
                    f"Registry._entries: every entry must be RegisteredStrategy, "
                    f"got {type(entry).__name__}"
                )
        sorted_entries = tuple(sorted(entries_iter, key=lambda entry: entry.identity))
        seen: dict[str, RegisteredStrategy] = {}
        for entry in sorted_entries:
            if entry.identity in seen:
                raise RegistryError(f"Registry: duplicate identity {entry.identity!r}")
            seen[entry.identity] = entry
        # Re-bind the canonical sorted entries / mapping so equality
        # is invariant to caller-side ordering.
        object.__setattr__(self, "_entries", sorted_entries)
        object.__setattr__(self, "_by_identity", seen)

    @property
    def identities(self) -> tuple[str, ...]:
        """The registered identities, sorted lexicographically."""
        return tuple(sorted(self._by_identity))

    def __len__(self) -> int:
        return len(self._by_identity)

    def __iter__(self) -> Iterator[RegisteredStrategy]:
        return iter(self._entries)

    def is_registered(self, identity: str) -> bool:
        """Return ``True`` iff ``identity`` is registered.

        The lookup is the deterministic validation surface the
        manifest authority binds to: a manifest published with an
        unregistered identity fails validation. The function is
        the only path the manifest authority uses to confirm
        registry membership; it never falls back to a default or
        an alias and never reads a strategy's implementation.
        """
        if not isinstance(identity, str) or not identity:
            return False
        return identity in self._by_identity

    def lookup(self, identity: str) -> RegisteredStrategy:
        """Return the :class:`RegisteredStrategy` for ``identity``.

        The lookup is the deterministic surface the manifest
        authority binds to: an unknown identity raises
        :class:`UnknownStrategyIdentityError`. The registry never
        returns ``None`` for an unknown identity and never falls
        back to a default; the closed vocabulary is the binding
        requirement.
        """
        if not isinstance(identity, str) or not identity:
            raise UnknownStrategyIdentityError(f"identity must be non-empty str, got {identity!r}")
        entry = self._by_identity.get(identity)
        if entry is None:
            raise UnknownStrategyIdentityError(
                f"strategy identity {identity!r} is not registered; "
                f"registered identities: {self.identities}"
            )
        return entry

    def validate_parameters(
        self, identity: str, parameters: Mapping[str, object]
    ) -> dict[str, int | bool | str]:
        """Validate ``parameters`` against the registered schema for ``identity``.

        The validator enforces, in order:

        1. ``identity`` is registered (the registry's closed
           vocabulary).
        2. ``parameters`` names no parameter the schema does not
           declare.
        3. ``parameters`` supplies every parameter the schema
           declares (no missing parameters).
        4. Every supplied value matches the declared
           :class:`ParameterType` and lies inside the declared
           ``lower_bound`` / ``upper_bound`` range.

        On success the function returns a fresh ``dict``
        preserving the schema's declared order. On failure it
        raises :class:`UnknownStrategyIdentityError` (rule 1) or
        :class:`InvalidParameterValueError` (rules 2–4).
        """
        if not isinstance(parameters, Mapping):
            raise InvalidParameterValueError(
                f"validate_parameters: parameters must be a Mapping, got "
                f"{type(parameters).__name__}"
            )
        entry = self.lookup(identity)
        schema_names: set[str] = set()
        for schema in entry.parameter_schemas:
            schema_names.add(schema.name)
        supplied_names = set(parameters.keys())
        unknown = sorted(supplied_names - schema_names)
        if unknown:
            raise InvalidParameterValueError(
                f"strategy {identity!r}: parameters not declared by the "
                f"registered schema: {unknown}; declared parameters: "
                f"{sorted(schema_names)}"
            )
        missing = sorted(schema_names - supplied_names)
        if missing:
            raise InvalidParameterValueError(
                f"strategy {identity!r}: missing required parameters: "
                f"{missing}; supplied parameters: {sorted(supplied_names)}"
            )
        result: dict[str, int | bool | str] = {}
        for schema in entry.parameter_schemas:
            result[schema.name] = _validate_value_against_schema(
                value=parameters[schema.name],
                schema=schema,
                identity=identity,
            )
        return result

    def checksum(self) -> str:
        """Return the deterministic SHA-256 hex digest of the registry.

        Two registries built from the same set of registered
        entries return the same checksum. The function is the
        surface the manifest authority binds to: a manifest
        published with a registry revision that disagrees with
        the manifest's recorded revision fails validation.
        """
        canonical_lines: list[str] = []
        canonical_lines.append(f"REGISTRY_VERSION={REGISTRY_VERSION}")
        for entry in self._entries:
            canonical_lines.append(f"IDENTITY={entry.identity}")
            canonical_lines.append(f"VERSION={entry.version}")
            canonical_lines.append(f"MODULE={entry.code_provenance.module}")
            canonical_lines.append(f"REVISION={entry.code_provenance.revision}")
            canonical_lines.append(f"SYMBOL={entry.code_provenance.symbol}")
            for schema in entry.parameter_schemas:
                canonical_lines.append(
                    f"PARAM|{schema.name}|{schema.type.value}|"
                    f"{schema.unit}|{schema.default}|{schema.lower_bound}|"
                    f"{schema.upper_bound}"
                )
        canonical = "\n".join(canonical_lines).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


# ---------------------------------------------------------------------------
# Default registry: closed vocabulary a backtest or paper run may execute
# ---------------------------------------------------------------------------


def _hold_factory(
    *,
    parameters: Mapping[str, int | bool | str],
    pool_key_id: str,
    chain_id: int,
) -> object:
    """The factory for :data:`IDENTITY_HOLD`."""
    from robinhood_lp.strategy.baselines import HoldStrategy

    return HoldStrategy(
        pool_key_id=pool_key_id,
        chain_id=chain_id,
    )


def _broad_range_factory(
    *,
    parameters: Mapping[str, int | bool | str],
    pool_key_id: str,
    chain_id: int,
) -> object:
    """The factory for :data:`IDENTITY_BROAD_RANGE`."""
    from robinhood_lp.strategy.baselines import (
        DEFAULT_BASELINE_CAPITAL_Q64_64,
        DEFAULT_BASELINE_LIQUIDITY,
        BroadRangeStrategy,
    )

    return BroadRangeStrategy(
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        tick_spacing=int(parameters["tick_spacing"]),
        liquidity=int(parameters.get("liquidity", DEFAULT_BASELINE_LIQUIDITY)),
        capital_q64_64=int(parameters.get("capital_q64_64", DEFAULT_BASELINE_CAPITAL_Q64_64)),
    )


def _fixed_width_factory(
    *,
    parameters: Mapping[str, int | bool | str],
    pool_key_id: str,
    chain_id: int,
) -> object:
    """The factory for :data:`IDENTITY_FIXED_WIDTH`."""
    from robinhood_lp.strategy.baselines import (
        DEFAULT_BASELINE_CAPITAL_Q64_64,
        DEFAULT_BASELINE_LIQUIDITY,
        DEFAULT_FIXED_HALF_WIDTH_TICKS,
        FixedWidthStrategy,
    )

    return FixedWidthStrategy(
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        tick_spacing=int(parameters["tick_spacing"]),
        half_width_ticks=int(parameters.get("half_width_ticks", DEFAULT_FIXED_HALF_WIDTH_TICKS)),
        liquidity=int(parameters.get("liquidity", DEFAULT_BASELINE_LIQUIDITY)),
        capital_q64_64=int(parameters.get("capital_q64_64", DEFAULT_BASELINE_CAPITAL_Q64_64)),
    )


def _volatility_width_factory(
    *,
    parameters: Mapping[str, int | bool | str],
    pool_key_id: str,
    chain_id: int,
) -> object:
    """The factory for :data:`IDENTITY_VOLATILITY_WIDTH`."""
    from robinhood_lp.strategy.baselines import (
        DEFAULT_BASELINE_CAPITAL_Q64_64,
        DEFAULT_BASELINE_LIQUIDITY,
        DEFAULT_MAX_HALF_WIDTH_TICKS,
        DEFAULT_MIN_HALF_WIDTH_TICKS,
        DEFAULT_VOLATILITY_MULTIPLIER,
        DEFAULT_VOLATILITY_WINDOW,
        VolatilityWidthStrategy,
    )

    return VolatilityWidthStrategy(
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        tick_spacing=int(parameters["tick_spacing"]),
        volatility_multiplier=int(
            parameters.get("volatility_multiplier", DEFAULT_VOLATILITY_MULTIPLIER)
        ),
        min_half_width_ticks=int(
            parameters.get("min_half_width_ticks", DEFAULT_MIN_HALF_WIDTH_TICKS)
        ),
        max_half_width_ticks=int(
            parameters.get("max_half_width_ticks", DEFAULT_MAX_HALF_WIDTH_TICKS)
        ),
        volatility_window=int(parameters.get("volatility_window", DEFAULT_VOLATILITY_WINDOW)),
        liquidity=int(parameters.get("liquidity", DEFAULT_BASELINE_LIQUIDITY)),
        capital_q64_64=int(parameters.get("capital_q64_64", DEFAULT_BASELINE_CAPITAL_Q64_64)),
    )


def _out_of_range_rebalance_factory(
    *,
    parameters: Mapping[str, int | bool | str],
    pool_key_id: str,
    chain_id: int,
) -> object:
    """The factory for :data:`IDENTITY_OUT_OF_RANGE_REBALANCE`."""
    from robinhood_lp.strategy.baselines import (
        DEFAULT_BASELINE_CAPITAL_Q64_64,
        DEFAULT_BASELINE_LIQUIDITY,
        DEFAULT_REBALANCE_HALF_WIDTH_TICKS,
        OutOfRangeRebalanceStrategy,
    )

    return OutOfRangeRebalanceStrategy(
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        tick_spacing=int(parameters["tick_spacing"]),
        half_width_ticks=int(
            parameters.get("half_width_ticks", DEFAULT_REBALANCE_HALF_WIDTH_TICKS)
        ),
        liquidity=int(parameters.get("liquidity", DEFAULT_BASELINE_LIQUIDITY)),
        capital_q64_64=int(parameters.get("capital_q64_64", DEFAULT_BASELINE_CAPITAL_Q64_64)),
    )


def _adaptive_factory(
    *,
    parameters: Mapping[str, int | bool | str],
    pool_key_id: str,
    chain_id: int,
) -> object:
    """The factory for :data:`IDENTITY_ADAPTIVE_RANGE`."""
    from robinhood_lp.strategy.adaptive import (
        ADAPTIVE_STRATEGY_VERSION,
        AdaptiveRangeParameters,
        AdaptiveStrategy,
    )

    params = AdaptiveRangeParameters(
        five_minute_window_seconds=int(parameters["five_minute_window_seconds"]),
        five_minute_return_threshold_q64_64=int(parameters["five_minute_return_threshold_q64_64"]),
        min_history_seconds=int(parameters["min_history_seconds"]),
        down_trend_threshold_q64_64=int(parameters["down_trend_threshold_q64_64"]),
        up_trend_threshold_q64_64=int(parameters["up_trend_threshold_q64_64"]),
        down_jump_threshold_q64_64=int(parameters["down_jump_threshold_q64_64"]),
        up_jump_threshold_q64_64=int(parameters["up_jump_threshold_q64_64"]),
        range_occupancy_threshold_q64_64=int(parameters["range_occupancy_threshold_q64_64"]),
        fee_min_samples=int(parameters["fee_min_samples"]),
        max_own_liquidity_share_q64_64=int(parameters["max_own_liquidity_share_q64_64"]),
        rebuild_cost_ratio_threshold_q64_64=int(parameters["rebuild_cost_ratio_threshold_q64_64"]),
        half_width_ticks=int(parameters["half_width_ticks"]),
        tick_spacing=int(parameters["tick_spacing"]),
        liquidity=int(parameters["liquidity"]),
        capital_q64_64=int(parameters["capital_q64_64"]),
        max_capital_q64_64=int(parameters["max_capital_q64_64"]),
        max_position_hold_seconds=int(parameters["max_position_hold_seconds"]),
        max_rebuild_wait_seconds=int(parameters["max_rebuild_wait_seconds"]),
    )
    return AdaptiveStrategy(
        params=params,
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        strategy_version=ADAPTIVE_STRATEGY_VERSION,
    )


def _model_backed_factory(
    *,
    parameters: Mapping[str, int | bool | str],
    pool_key_id: str,
    chain_id: int,
) -> object:
    """The factory for :data:`IDENTITY_MODEL_BACKED`.

    The factory is the registry's binding to the T102 model-backed
    strategy surface; it returns a
    :class:`robinhood_lp.strategy.model_backed.ModelBackedStrategy`
    instance bound to the registered identity's parameter schema. The
    strategy occupies the replaceable regime and fee-opportunity
    interfaces the strategy contract already defines (T060); the
    model artifact the strategy loads is recorded on the resulting
    instance so a later evaluation can resolve it.
    """
    from robinhood_lp.strategy.model_backed import (
        MODEL_COMPONENT_VERSION,
        ModelBackedEvaluationParameters,
        ModelBackedStrategy,
    )

    params = ModelBackedEvaluationParameters(
        model_artifact_id=str(parameters["model_artifact_id"]),
        staleness_seconds=int(parameters["staleness_seconds"]),
        require_pool_set_membership=bool(parameters["require_pool_set_membership"]),
        ood_prediction_variance_q64_64=int(parameters["ood_prediction_variance_q64_64"]),
        regime_confidence_floor_q64_64=int(parameters["regime_confidence_floor_q64_64"]),
        fee_opportunity_floor_q64_64=int(parameters["fee_opportunity_floor_q64_64"]),
        bootstrap_iterations=int(parameters["bootstrap_iterations"]),
        bootstrap_seed=int(parameters["bootstrap_seed"]),
    )
    return ModelBackedStrategy(
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        parameters=params,
        component_version=MODEL_COMPONENT_VERSION,
        q64_scale=1 << 64,
    )


#: The closed identity vocabulary a backtest or paper run may execute.
#: T068 publishes the registry with these six identities (the five
#: T062 baselines plus the T065 adaptive-Range strategy). The set is
#: the union of the T062 baselines and the T065 adaptive-Range
#: strategy and is the closed vocabulary T105 binds published
#: manifests to; an identity outside this set is rejected.
IDENTITY_HOLD: Final[str] = "t062.hold.v1"
IDENTITY_BROAD_RANGE: Final[str] = "t062.broad_range.v1"
IDENTITY_FIXED_WIDTH: Final[str] = "t062.fixed_width.v1"
IDENTITY_VOLATILITY_WIDTH: Final[str] = "t062.volatility_width.v1"
IDENTITY_OUT_OF_RANGE_REBALANCE: Final[str] = "t062.out_of_range_rebalance.v1"
IDENTITY_ADAPTIVE_RANGE: Final[str] = "t065.adaptive_range.v1"
IDENTITY_MODEL_BACKED: Final[str] = "t102.model_backed.v1"

#: Module name each registered strategy's implementation lives in.
#: The registry captures the module at construction time so a
#: later refactor that moves a strategy module surfaces as a
#: checksum change.
_MODULE_BASELINES: Final[str] = "robinhood_lp.strategy.baselines"
_MODULE_ADAPTIVE: Final[str] = "robinhood_lp.strategy.adaptive"
_MODULE_MODEL_BACKED: Final[str] = "robinhood_lp.strategy.model_backed"


def _build_default_registry_entries() -> tuple[RegisteredStrategy, ...]:
    """Construct the closed vocabulary the registry publishes."""
    from robinhood_lp.strategy.adaptive import ADAPTIVE_STRATEGY_VERSION
    from robinhood_lp.strategy.baselines import (
        BASELINE_STRATEGY_VERSION,
        DEFAULT_BASELINE_CAPITAL_Q64_64,
        DEFAULT_BASELINE_LIQUIDITY,
        DEFAULT_FIXED_HALF_WIDTH_TICKS,
        DEFAULT_MAX_HALF_WIDTH_TICKS,
        DEFAULT_MIN_HALF_WIDTH_TICKS,
        DEFAULT_REBALANCE_HALF_WIDTH_TICKS,
        DEFAULT_VOLATILITY_MULTIPLIER,
        DEFAULT_VOLATILITY_WINDOW,
    )

    baselines_revision = resolve_module_revision(module_name=_MODULE_BASELINES)
    adaptive_revision = resolve_module_revision(module_name=_MODULE_ADAPTIVE)

    entries: list[RegisteredStrategy] = [
        RegisteredStrategy(
            identity=IDENTITY_HOLD,
            version=BASELINE_STRATEGY_VERSION,
            parameter_schemas=(),
            code_provenance=CodeProvenance(
                module=_MODULE_BASELINES,
                revision=baselines_revision,
                symbol="HoldStrategy",
            ),
            factory=_AdapterFactory(_hold_factory),
            description=(
                "T062 zero-action benchmark. Returns NO_TRADE for every "
                "decision; the HODL / cash-benchmark anchor."
            ),
        ),
        RegisteredStrategy(
            identity=IDENTITY_BROAD_RANGE,
            version=BASELINE_STRATEGY_VERSION,
            parameter_schemas=(
                ParameterSchema(
                    name="tick_spacing",
                    type=ParameterType.TICK_SPACING,
                    unit="ticks",
                    default=60,
                    description="V4 tick spacing the pool enforces.",
                ),
                ParameterSchema(
                    name="liquidity",
                    type=ParameterType.LIQUIDITY,
                    unit="uint128",
                    default=DEFAULT_BASELINE_LIQUIDITY,
                    description="Integer liquidity the strategy proposes.",
                ),
                ParameterSchema(
                    name="capital_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 USDG",
                    default=DEFAULT_BASELINE_CAPITAL_Q64_64,
                    description="Q64.64 USDG capital envelope.",
                ),
            ),
            code_provenance=CodeProvenance(
                module=_MODULE_BASELINES,
                revision=baselines_revision,
                symbol="BroadRangeStrategy",
            ),
            factory=_AdapterFactory(_broad_range_factory),
            description=(
                "T062 protocol-valid broad-range baseline. Opens the "
                "widest aligned Range inside [MIN_TICK, MAX_TICK]; "
                "never rebalances."
            ),
        ),
        RegisteredStrategy(
            identity=IDENTITY_FIXED_WIDTH,
            version=BASELINE_STRATEGY_VERSION,
            parameter_schemas=(
                ParameterSchema(
                    name="tick_spacing",
                    type=ParameterType.TICK_SPACING,
                    unit="ticks",
                    default=60,
                    description="V4 tick spacing the pool enforces.",
                ),
                ParameterSchema(
                    name="half_width_ticks",
                    type=ParameterType.POSITIVE_INT,
                    unit="ticks",
                    default=DEFAULT_FIXED_HALF_WIDTH_TICKS,
                    description="Half-range width around the current tick.",
                ),
                ParameterSchema(
                    name="liquidity",
                    type=ParameterType.LIQUIDITY,
                    unit="uint128",
                    default=DEFAULT_BASELINE_LIQUIDITY,
                    description="Integer liquidity the strategy proposes.",
                ),
                ParameterSchema(
                    name="capital_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 USDG",
                    default=DEFAULT_BASELINE_CAPITAL_Q64_64,
                    description="Q64.64 USDG capital envelope.",
                ),
            ),
            code_provenance=CodeProvenance(
                module=_MODULE_BASELINES,
                revision=baselines_revision,
                symbol="FixedWidthStrategy",
            ),
            factory=_AdapterFactory(_fixed_width_factory),
            description=(
                "T062 symmetric ±half_width_ticks Range around the "
                "current tick; rebalances when the price leaves Range."
            ),
        ),
        RegisteredStrategy(
            identity=IDENTITY_VOLATILITY_WIDTH,
            version=BASELINE_STRATEGY_VERSION,
            parameter_schemas=(
                ParameterSchema(
                    name="tick_spacing",
                    type=ParameterType.TICK_SPACING,
                    unit="ticks",
                    default=60,
                    description="V4 tick spacing the pool enforces.",
                ),
                ParameterSchema(
                    name="volatility_multiplier",
                    type=ParameterType.POSITIVE_INT,
                    unit="dimensionless",
                    default=DEFAULT_VOLATILITY_MULTIPLIER,
                    description="Half-width = clamp(round(k * sigma_ticks), min, max).",
                ),
                ParameterSchema(
                    name="min_half_width_ticks",
                    type=ParameterType.POSITIVE_INT,
                    unit="ticks",
                    default=DEFAULT_MIN_HALF_WIDTH_TICKS,
                    description="Floor on the half-width in ticks.",
                ),
                ParameterSchema(
                    name="max_half_width_ticks",
                    type=ParameterType.POSITIVE_INT,
                    unit="ticks",
                    default=DEFAULT_MAX_HALF_WIDTH_TICKS,
                    description="Ceiling on the half-width in ticks.",
                ),
                ParameterSchema(
                    name="volatility_window",
                    type=ParameterType.POSITIVE_INT,
                    unit="events",
                    default=DEFAULT_VOLATILITY_WINDOW,
                    description="Number of recent price events the sigma estimator consumes.",
                ),
                ParameterSchema(
                    name="liquidity",
                    type=ParameterType.LIQUIDITY,
                    unit="uint128",
                    default=DEFAULT_BASELINE_LIQUIDITY,
                    description="Integer liquidity the strategy proposes.",
                ),
                ParameterSchema(
                    name="capital_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 USDG",
                    default=DEFAULT_BASELINE_CAPITAL_Q64_64,
                    description="Q64.64 USDG capital envelope.",
                ),
            ),
            code_provenance=CodeProvenance(
                module=_MODULE_BASELINES,
                revision=baselines_revision,
                symbol="VolatilityWidthStrategy",
            ),
            factory=_AdapterFactory(_volatility_width_factory),
            description=(
                "T062 volatility-width baseline. Half-width tracks "
                "realised volatility; rebalances on Range exit."
            ),
        ),
        RegisteredStrategy(
            identity=IDENTITY_OUT_OF_RANGE_REBALANCE,
            version=BASELINE_STRATEGY_VERSION,
            parameter_schemas=(
                ParameterSchema(
                    name="tick_spacing",
                    type=ParameterType.TICK_SPACING,
                    unit="ticks",
                    default=60,
                    description="V4 tick spacing the pool enforces.",
                ),
                ParameterSchema(
                    name="half_width_ticks",
                    type=ParameterType.POSITIVE_INT,
                    unit="ticks",
                    default=DEFAULT_REBALANCE_HALF_WIDTH_TICKS,
                    description="Half-range width around the current tick.",
                ),
                ParameterSchema(
                    name="liquidity",
                    type=ParameterType.LIQUIDITY,
                    unit="uint128",
                    default=DEFAULT_BASELINE_LIQUIDITY,
                    description="Integer liquidity the strategy proposes.",
                ),
                ParameterSchema(
                    name="capital_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 USDG",
                    default=DEFAULT_BASELINE_CAPITAL_Q64_64,
                    description="Q64.64 USDG capital envelope.",
                ),
            ),
            code_provenance=CodeProvenance(
                module=_MODULE_BASELINES,
                revision=baselines_revision,
                symbol="OutOfRangeRebalanceStrategy",
            ),
            factory=_AdapterFactory(_out_of_range_rebalance_factory),
            description=(
                "T062 out-of-range rebalance baseline. Opens once; "
                "rebalances only when the price leaves the Range."
            ),
        ),
        RegisteredStrategy(
            identity=IDENTITY_ADAPTIVE_RANGE,
            version=ADAPTIVE_STRATEGY_VERSION,
            parameter_schemas=(
                ParameterSchema(
                    name="five_minute_window_seconds",
                    type=ParameterType.POSITIVE_INT,
                    unit="seconds",
                    default=300,
                    description="5-minute bar window for the USDG return rule.",
                ),
                ParameterSchema(
                    name="five_minute_return_threshold_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 return",
                    default=1 << 64,
                    description=(
                        "Strict-greater-than threshold for the 5-minute "
                        "USDG return rule (1.0 in Q64.64 == 100%)."
                    ),
                ),
                ParameterSchema(
                    name="min_history_seconds",
                    type=ParameterType.NON_NEGATIVE_INT,
                    unit="seconds",
                    default=900,
                    description="Minimum history the regime model requires.",
                ),
                ParameterSchema(
                    name="down_trend_threshold_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 return",
                    default=(1 << 64) // 20,
                    description="Negative-return magnitude that triggers DOWN_TREND.",
                ),
                ParameterSchema(
                    name="up_trend_threshold_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 return",
                    default=(1 << 64) // 10,
                    description="Positive-return magnitude that triggers UP_TREND.",
                ),
                ParameterSchema(
                    name="down_jump_threshold_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 return",
                    default=(1 << 64) // 10,
                    description="Single-bar negative return that triggers JUMP_RISK.",
                ),
                ParameterSchema(
                    name="up_jump_threshold_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 return",
                    default=(1 << 64) // 4,
                    description="Single-bar positive return that triggers JUMP_RISK.",
                ),
                ParameterSchema(
                    name="range_occupancy_threshold_q64_64",
                    type=ParameterType.Q64_64,
                    unit="Q64.64 fraction",
                    default=((1 << 64) * 60) // 100,
                    description="Minimum in-range fraction for a RANGE assessment.",
                ),
                ParameterSchema(
                    name="fee_min_samples",
                    type=ParameterType.POSITIVE_INT,
                    unit="events",
                    default=5,
                    description="Minimum samples for the fee opportunity model.",
                ),
                ParameterSchema(
                    name="max_own_liquidity_share_q64_64",
                    type=ParameterType.Q64_64,
                    unit="Q64.64 fraction",
                    default=((1 << 64) * 25) // 100,
                    description="Maximum own-liquidity share for the fee model.",
                ),
                ParameterSchema(
                    name="rebuild_cost_ratio_threshold_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 fraction",
                    default=1 << 64,
                    description="Rebuild-cost / expected-fee-edge ratio that defers rebuilds.",
                ),
                ParameterSchema(
                    name="half_width_ticks",
                    type=ParameterType.NON_NEGATIVE_INT,
                    unit="ticks",
                    default=600,
                    description="Symmetric half-width around the current tick.",
                ),
                ParameterSchema(
                    name="tick_spacing",
                    type=ParameterType.POSITIVE_INT,
                    unit="ticks",
                    default=60,
                    description="V4 tick spacing the pool enforces.",
                ),
                ParameterSchema(
                    name="liquidity",
                    type=ParameterType.LIQUIDITY,
                    unit="uint128",
                    default=1000,
                    description="Integer liquidity the strategy proposes.",
                ),
                ParameterSchema(
                    name="capital_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 USDG",
                    default=1 << 64,
                    description="Q64.64 USDG capital envelope.",
                ),
                ParameterSchema(
                    name="max_capital_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64 USDG",
                    default=1 << 70,
                    description="Per-pool maximum capital envelope.",
                ),
                ParameterSchema(
                    name="max_position_hold_seconds",
                    type=ParameterType.NON_NEGATIVE_INT,
                    unit="seconds",
                    default=0,
                    description="Position hold bound (0 = no bound).",
                ),
                ParameterSchema(
                    name="max_rebuild_wait_seconds",
                    type=ParameterType.NON_NEGATIVE_INT,
                    unit="seconds",
                    default=1800,
                    description="Max out-of-range wait before forcing REBUILD.",
                ),
            ),
            code_provenance=CodeProvenance(
                module=_MODULE_ADAPTIVE,
                revision=adaptive_revision,
                symbol="AdaptiveStrategy",
            ),
            factory=_AdapterFactory(_adaptive_factory),
            description=(
                "T065 USDG-first adaptive-Range rule strategy. "
                "Replaceable regime / fee-opportunity components; "
                "downside-asymmetric trend / jump filter; complete "
                "out-of-Range lifecycle."
            ),
        ),
        RegisteredStrategy(
            identity=IDENTITY_MODEL_BACKED,
            version="t102.model_evaluation.v1",
            parameter_schemas=(
                ParameterSchema(
                    name="model_artifact_id",
                    type=ParameterType.STR,
                    unit="identifier",
                    default="RULE_FALLBACK",
                    description=(
                        "Identifier of the model artifact the components load; "
                        "the literal sentinel ``RULE_FALLBACK`` yields the "
                        "deterministic rule fallback (no artifact, no "
                        "inferred or default identity)."
                    ),
                ),
                ParameterSchema(
                    name="staleness_seconds",
                    type=ParameterType.NON_NEGATIVE_INT,
                    unit="seconds",
                    default=7 * 24 * 60 * 60,
                    description=(
                        "Maximum age (seconds) of the model artifact before "
                        "the OOD gate treats it as stale; ``0`` disables the "
                        "staleness check."
                    ),
                ),
                ParameterSchema(
                    name="require_pool_set_membership",
                    type=ParameterType.BOOL,
                    unit="bool",
                    default=True,
                    description=(
                        "When ``True``, an evaluation pool disjoint from the "
                        "artifact's ``training_pool_set`` is treated as "
                        "out-of-distribution."
                    ),
                ),
                ParameterSchema(
                    name="ood_prediction_variance_q64_64",
                    type=ParameterType.Q64_64,
                    unit="Q64.64",
                    default=1,
                    description=(
                        "Minimum prediction variance the artifact must "
                        "produce across a fold; a variance below this "
                        "floor triggers the OOD gate."
                    ),
                ),
                ParameterSchema(
                    name="regime_confidence_floor_q64_64",
                    type=ParameterType.STRICT_Q64_64,
                    unit="Q64.64",
                    default=_Q64_SCALE // 100,
                    description=(
                        "Minimum confidence the model-backed regime model "
                        "must produce for an ``ASSESSED`` outcome."
                    ),
                ),
                ParameterSchema(
                    name="fee_opportunity_floor_q64_64",
                    type=ParameterType.Q64_64,
                    unit="Q64.64",
                    default=0,
                    description=(
                        "Minimum expected fee edge the model-backed "
                        "fee-opportunity model must produce for an "
                        "``ASSESSED`` outcome."
                    ),
                ),
                ParameterSchema(
                    name="bootstrap_iterations",
                    type=ParameterType.POSITIVE_INT,
                    unit="iterations",
                    default=1000,
                    description=(
                        "Number of bootstrap resamples the evaluation "
                        "uses for episode-level confidence intervals."
                    ),
                ),
                ParameterSchema(
                    name="bootstrap_seed",
                    type=ParameterType.NON_NEGATIVE_INT,
                    unit="seed",
                    default=0,
                    description=("Deterministic seed the bootstrap resampler consumes."),
                ),
            ),
            code_provenance=CodeProvenance(
                module=_MODULE_MODEL_BACKED,
                revision="t102.model_evaluation.v1",
                symbol="ModelBackedStrategy",
            ),
            factory=_AdapterFactory(_model_backed_factory),
            description=(
                "T102 model-backed strategy. Replaceable regime and "
                "fee-opportunity components consume a registered "
                "model artifact with deterministic rule fallback; the "
                "verdict comes from simulated LP economics through "
                "the T061 event-driven engine."
            ),
        ),
    ]
    return tuple(entries)


_DEFAULT_REGISTRY_CACHE: Registry | None = None


def default_registry() -> Registry:
    """Return the canonical default registry.

    The function is the single entry point the manifest authority
    (T105) and the Web console (``WEB-PAGE-012``) use to read the
    registered identities. The result is cached on the first call
    so repeated lookups return the same instance; the cache is a
    process-local cache and the construction is deterministic so
    two processes reading the same source revision return the
    same registry contents.
    """
    global _DEFAULT_REGISTRY_CACHE
    if _DEFAULT_REGISTRY_CACHE is None:
        entries = _build_default_registry_entries()
        _DEFAULT_REGISTRY_CACHE = Registry(_entries=entries)
    return _DEFAULT_REGISTRY_CACHE


def reset_default_registry_cache() -> None:
    """Clear the process-local cache :func:`default_registry` uses.

    The helper exists so a test (or the manifest authority's
    registry-rotation gate) can force the next :func:`default_registry`
    call to rebuild the registry from the live source modules.
    Production callers should not use this helper.
    """
    global _DEFAULT_REGISTRY_CACHE
    _DEFAULT_REGISTRY_CACHE = None


# ---------------------------------------------------------------------------
# Module-level convenience wrappers
# ---------------------------------------------------------------------------


def is_registered(identity: str) -> bool:
    """Return ``True`` iff ``identity`` is in :func:`default_registry`."""
    return default_registry().is_registered(identity)


def lookup(identity: str) -> RegisteredStrategy:
    """Return the registered entry for ``identity``."""
    return default_registry().lookup(identity)


def validate_parameters(
    identity: str, parameters: Mapping[str, object]
) -> dict[str, int | bool | str]:
    """Validate ``parameters`` against the schema :func:`default_registry` registers for ``identity``."""
    return default_registry().validate_parameters(identity, parameters)


def registry_checksum() -> str:
    """Return the deterministic SHA-256 checksum of :func:`default_registry`."""
    return default_registry().checksum()


def registered_identities() -> tuple[str, ...]:
    """Return the identities :func:`default_registry` enumerates, sorted lexicographically."""
    return default_registry().identities


# ---------------------------------------------------------------------------
# Layer-purity dependency check
# ---------------------------------------------------------------------------


def _is_registry_module(name: str) -> bool:
    """Return ``True`` iff ``name`` belongs to the registry's reachable set."""
    return name == "robinhood_lp.strategy.registry" or name.startswith(
        "robinhood_lp.strategy.registry."
    )


def _walk_registry_imports(roots: tuple[ModuleType, ...]) -> dict[str, tuple[str, ...]]:
    """Walk the live module graph reachable from ``roots`` and collect imports.

    The walker mirrors the strategy layer's
    :func:`collect_strategy_module_imports` helper but scopes the
    reachability to the registry module only (the registry must
    not import the strategy implementation modules it registers at
    lookup time; the walker captures that contract by inspecting
    the live graph).
    """
    seen: set[str] = set()
    stack: list[ModuleType] = list(roots)
    imports: dict[str, set[str]] = {}
    while stack:
        module = stack.pop()
        if module.__name__ in seen:
            continue
        seen.add(module.__name__)
        module_imports: set[str] = set()
        for _attr_name, attr in vars(module).items():
            if isinstance(attr, ModuleType):
                module_imports.add(attr.__name__)
                if _is_registry_module(attr.__name__) or attr.__name__.startswith("robinhood_lp."):
                    stack.append(attr)
        imports[module.__name__] = module_imports
    return {name: tuple(sorted(values)) for name, values in imports.items()}


def assert_registry_layer_is_pure() -> None:
    """Raise :class:`RegistryError` if the registry imports a forbidden module.

    The denylist is the closed set recorded in
    :data:`_FORBIDDEN_REGISTRY_ROBINHOOD_MODULES`. The check is
    narrow on purpose: the registry is the validation surface the
    manifest authority binds to, and any sibling import is a
    contract break the manifest authority cannot reason about.
    The registry's construction-time imports of the registered
    modules (the strategies themselves) are scoped to the factory
    call and are excluded from the runtime imports the walker
    inspects because the factories are not module attributes.
    """
    forbidden: list[tuple[str, str]] = []
    import importlib as _importlib

    registry_module = _importlib.import_module("robinhood_lp.strategy.registry")
    for module_name, imports in _walk_registry_imports((registry_module,)).items():
        for name in imports:
            for forbidden_root in _FORBIDDEN_REGISTRY_ROBINHOOD_MODULES:
                if name == forbidden_root or name.startswith(forbidden_root + "."):
                    forbidden.append((module_name, name))
                    break
    if forbidden:
        rendered = "\n".join(f"  {src}: imports {imp}" for src, imp in forbidden)
        raise RegistryError(f"registry imports forbidden modules:\n{rendered}")


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "IDENTITY_ADAPTIVE_RANGE",
    "IDENTITY_BROAD_RANGE",
    "IDENTITY_FIXED_WIDTH",
    "IDENTITY_HOLD",
    "IDENTITY_OUT_OF_RANGE_REBALANCE",
    "IDENTITY_VOLATILITY_WIDTH",
    "CodeProvenance",
    "InvalidCodeProvenanceError",
    "InvalidParameterSchemaError",
    "InvalidParameterValueError",
    "ParameterSchema",
    "ParameterType",
    "REGISTRY_VERSION",
    "RegisteredStrategy",
    "Registry",
    "RegistryError",
    "StrategyFactory",
    "UnknownStrategyIdentityError",
    "assert_registry_layer_is_pure",
    "default_registry",
    "is_registered",
    "lookup",
    "registered_identities",
    "registry_checksum",
    "reset_default_registry_cache",
    "resolve_module_revision",
    "validate_parameters",
]


__version__: str = "0.0.0"
