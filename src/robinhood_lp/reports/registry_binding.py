"""Strategy ↔ strategy-registry binding (T105).

The T105 cutover replaces the T063 hard-coded strategy-name vocabulary
(``VALID_STRATEGY_KINDS``) with T068's :class:`Registry` authority. This
module is the single surface every current publication path consults to
translate a caller-supplied identity + parameter set into a
:class:`StrategyBinding` record the manifest carries, and to verify
that a saved manifest's binding still agrees with the registered
revision that authorised the run.

Design contract (binding):

- **Closed identity vocabulary.** A binding is built only against a
  registered identity. An unregistered identity is rejected by
  :func:`bind_strategy_to_registry` with
  :class:`UnknownStrategyIdentityError`; the manifest authority never
  falls back to a hard-coded vocabulary or a caller-supplied default.

- **Parameter schema enforcement.** A binding validates the caller's
  parameter set against the registered schema before publishing. The
  registry rejects an undeclared parameter, a missing required
  parameter, a value outside the declared type / unit / range, or a
  value that fails the schema's own type guard (per T068 acceptance).

- **Deterministic binding metadata.** Every binding carries the
  ``registry_version`` + ``registry_checksum`` pair, the registered
  identity's ``strategy_version``, the deterministic
  ``parameter_schema_checksum`` and the ``code_provenance`` (module /
  revision / symbol) the registry captured. Two builds against the
  same revision return the same binding metadata so the manifest's
  report checksum stays reproducible.

- **Layer purity.** The module depends on the strategy layer only; it
  does not import the backtest engine, RPC, storage, configuration,
  signing, execution, or presentation code.

References:

- T068 — strategy registry (the source of truth for the binding).
- T063 — experiment manifests (the artifact the binding is recorded
  on; this module replaces the hard-coded strategy vocabulary the
  T063 manifest carried).
- G-STRATEGY-REGISTRY-01 — the registry obligation this binding
  binds to.
- ADR-006 — the strategy layer this module belongs to.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from robinhood_lp.strategy.registry import (
    REGISTRY_VERSION,
    ParameterSchema,
    Registry,
    RegistryError,
    UnknownStrategyIdentityError,
    default_registry,
)

#: Module version. Bumping it is a breaking change for the manifest
#: authority (T105) and any consumer that reads the binding metadata.
BINDING_VERSION: Final[str] = "t105.strategy_binding.v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RegistryBindingError(RegistryError):
    """A binding's registry-derived metadata disagrees with the live registry.

    The manifest authority raises this error when a saved manifest's
    binding cannot be reconciled against the registry that authorised
    the run — e.g. the registry version changed, the registry checksum
    drifted, the registered identity disappeared, the parameter schema
    was revised, or the code provenance was repointed. The error names
    the failing field so a reviewer can localise the drift.
    """

    def __init__(self, *, slot: str, recorded: str, current: str) -> None:
        self.slot: Final[str] = slot
        self.recorded: Final[str] = recorded
        self.current: Final[str] = current
        super().__init__(
            f"RegistryBindingError: {slot} disagrees with the registered "
            f"revision (recorded={recorded!r}, current={current!r})"
        )


# ---------------------------------------------------------------------------
# Per-identity parameter schema checksum
# ---------------------------------------------------------------------------


def compute_parameter_schema_checksum(
    parameter_schemas: tuple[ParameterSchema, ...],
) -> str:
    """Return the SHA-256 hex digest of one identity's parameter schemas.

    The function is the deterministic surface the manifest authority
    binds the per-identity schema to. Two enumerations over the same
    registered entry return the same checksum; a schema revision
    surfaces here as a different digest and the manifest authority
    rejects a binding that names the old checksum.

    The canonical form is the same per-schema line the registry uses
    internally for its own checksum (the parameter section), so a
    reviewer who re-derives the digest from the registered schema
    arrives at the same value the manifest carries.
    """
    canonical_lines = [
        f"PARAM|{schema.name}|{schema.type.value}|{schema.unit}|"
        f"{schema.default}|{schema.lower_bound}|{schema.upper_bound}"
        for schema in parameter_schemas
    ]
    canonical = "\n".join(canonical_lines).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# ---------------------------------------------------------------------------
# StrategyBinding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyBinding:
    """The registry-derived binding a T105 manifest carries.

    Every field the binding carries is supplied by the registry; the
    binding is the single source the manifest's report checksum binds
    to. The ``registry_version`` + ``registry_checksum`` pair is the
    "what revision authorised the run" anchor; the per-identity
    ``strategy_version`` + ``parameter_schema_checksum`` pair is the
    "what schema version and code revision built this run" anchor;
    ``code_provenance_module`` / ``revision`` / ``symbol`` record
    *where* the strategy implementation lived at build time.

    Field units / shapes:

    - ``registry_version`` — registry schema version string.
    - ``registry_checksum`` — SHA-256 hex digest of the registry's
      canonical enumeration.
    - ``strategy_identity`` — the registered identity string
      (e.g., ``"t065.adaptive_range.v1"``).
    - ``strategy_version`` — the per-identity version string the
      registry captured (e.g., ``"t065.adaptive_strategy.v1"``).
    - ``parameter_schema_version`` — the per-identity schema version
      string; mirrors :attr:`strategy_version` because the schema
      lives in the same module.
    - ``parameter_schema_checksum`` — SHA-256 hex digest of the
      schema's canonical form.
    - ``code_provenance_module`` — dotted module path.
    - ``code_provenance_revision`` — git SHA the module was built
      from, or ``"UNKNOWN"``.
    - ``code_provenance_symbol`` — symbol name the registry captured
      (empty string when the entry does not bind a symbol).
    - ``validated_parameters`` — the parameter set the registry
      validated, in the schema's declared order. The manifest carries
      this exact mapping.

    Equality and hashing follow dataclass identity; two bindings
    built against the same registered entry return the same hash.
    """

    registry_version: str
    registry_checksum: str
    strategy_identity: str
    strategy_version: str
    parameter_schema_version: str
    parameter_schema_checksum: str
    code_provenance_module: str
    code_provenance_revision: str
    code_provenance_symbol: str
    validated_parameters: tuple[tuple[str, int | bool | str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.registry_version, str) or not self.registry_version:
            raise RegistryError(
                f"StrategyBinding.registry_version: must be non-empty str, "
                f"got {self.registry_version!r}"
            )
        if not isinstance(self.registry_checksum, str) or not self.registry_checksum:
            raise RegistryError(
                f"StrategyBinding.registry_checksum: must be non-empty str, "
                f"got {self.registry_checksum!r}"
            )
        if not isinstance(self.strategy_identity, str) or not self.strategy_identity:
            raise RegistryError(
                f"StrategyBinding.strategy_identity: must be non-empty str, "
                f"got {self.strategy_identity!r}"
            )
        if not isinstance(self.strategy_version, str) or not self.strategy_version:
            raise RegistryError(
                f"StrategyBinding.strategy_version: must be non-empty str, "
                f"got {self.strategy_version!r}"
            )
        if not isinstance(self.parameter_schema_version, str) or not self.parameter_schema_version:
            raise RegistryError(
                f"StrategyBinding.parameter_schema_version: must be non-empty str, "
                f"got {self.parameter_schema_version!r}"
            )
        if (
            not isinstance(self.parameter_schema_checksum, str)
            or not self.parameter_schema_checksum
        ):
            raise RegistryError(
                f"StrategyBinding.parameter_schema_checksum: must be non-empty str, "
                f"got {self.parameter_schema_checksum!r}"
            )
        if not isinstance(self.code_provenance_module, str) or not self.code_provenance_module:
            raise RegistryError(
                f"StrategyBinding.code_provenance_module: must be non-empty str, "
                f"got {self.code_provenance_module!r}"
            )
        if not isinstance(self.code_provenance_revision, str) or not self.code_provenance_revision:
            raise RegistryError(
                f"StrategyBinding.code_provenance_revision: must be non-empty str, "
                f"got {self.code_provenance_revision!r}"
            )
        if not isinstance(self.code_provenance_symbol, str):
            raise RegistryError(
                f"StrategyBinding.code_provenance_symbol: must be str, "
                f"got {type(self.code_provenance_symbol).__name__}"
            )
        if not isinstance(self.validated_parameters, tuple):
            raise RegistryError(
                f"StrategyBinding.validated_parameters: must be tuple of "
                f"(name, value) pairs, got {type(self.validated_parameters).__name__}"
            )
        for entry in self.validated_parameters:
            if not (
                isinstance(entry, tuple)
                and len(entry) == 2
                and isinstance(entry[0], str)
                and entry[0]
                and isinstance(entry[1], (int, bool, str))
            ):
                raise RegistryError(
                    f"StrategyBinding.validated_parameters: every entry must be "
                    f"(non-empty str, int|bool|str), got {entry!r}"
                )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly representation.

        The mapping preserves the schema's declared parameter order so
        the manifest's canonical serialisation is deterministic.
        """
        return {
            "registry_version": self.registry_version,
            "registry_checksum": self.registry_checksum,
            "strategy_identity": self.strategy_identity,
            "strategy_version": self.strategy_version,
            "parameter_schema_version": self.parameter_schema_version,
            "parameter_schema_checksum": self.parameter_schema_checksum,
            "code_provenance_module": self.code_provenance_module,
            "code_provenance_revision": self.code_provenance_revision,
            "code_provenance_symbol": self.code_provenance_symbol,
            "validated_parameters": [[name, value] for name, value in self.validated_parameters],
        }


# ---------------------------------------------------------------------------
# Builders and matchers
# ---------------------------------------------------------------------------


def _validated_parameters_as_tuple(
    validated: Mapping[str, int | bool | str],
    *,
    schema_order: tuple[str, ...],
) -> tuple[tuple[str, int | bool | str], ...]:
    """Return the validated parameter mapping as a schema-ordered tuple.

    The dataclass stores parameters as a tuple so equality and hashing
    follow the schema's declared order rather than the caller's
    insertion order; the tuple's determinism is part of the binding
    contract.
    """
    return tuple((name, validated[name]) for name in schema_order if name in validated)


def bind_strategy_to_registry(
    identity: str,
    parameters: Mapping[str, object],
    *,
    registry: Registry | None = None,
) -> StrategyBinding:
    """Bind ``identity`` + ``parameters`` against the registry.

    The function is the canonical builder every current publication
    path uses. It performs the registry's closed-vocabulary lookup
    (rejects unregistered identities with
    :class:`UnknownStrategyIdentityError`) and the per-identity
    parameter validation (rejects undeclared / missing / wrong-type /
    out-of-range parameters with :class:`InvalidParameterValueError`).
    On success it returns a fully-populated :class:`StrategyBinding`.

    The caller may supply a custom ``registry`` for tests; the default
    is :func:`robinhood_lp.strategy.registry.default_registry`.
    """
    reg = registry if registry is not None else default_registry()
    # ``reg.lookup`` raises :class:`UnknownStrategyIdentityError` for
    # an unregistered identity; the manifest authority never falls back
    # to a hard-coded vocabulary, so the call site sees the dedicated
    # error and a reviewer can localise the unknown identity.
    entry = reg.lookup(identity)
    # ``reg.validate_parameters`` raises
    # :class:`InvalidParameterValueError` for an undeclared /
    # missing / wrong-type / out-of-range parameter. The binding
    # contract requires the validated set; the dataclass stores the
    # validated mapping as a schema-ordered tuple so equality and
    # hashing are deterministic.
    validated = reg.validate_parameters(identity, parameters)
    schema_order = tuple(schema.name for schema in entry.parameter_schemas)
    return StrategyBinding(
        registry_version=REGISTRY_VERSION,
        registry_checksum=reg.checksum(),
        strategy_identity=identity,
        strategy_version=entry.version,
        parameter_schema_version=entry.version,
        parameter_schema_checksum=compute_parameter_schema_checksum(entry.parameter_schemas),
        code_provenance_module=entry.code_provenance.module,
        code_provenance_revision=entry.code_provenance.revision,
        code_provenance_symbol=entry.code_provenance.symbol,
        validated_parameters=_validated_parameters_as_tuple(validated, schema_order=schema_order),
    )


def assert_binding_matches_registry(
    binding: StrategyBinding,
    *,
    registry: Registry | None = None,
) -> None:
    """Reject ``binding`` whose registry metadata disagrees with the registry.

    The function is the gate every manifest publication passes through
    after the binding was bound at build time and recovered at load
    time. It rejects any disagreement in:

    - ``registry_version`` vs :data:`REGISTRY_VERSION`.
    - ``registry_checksum`` vs the live registry's checksum.
    - ``strategy_version`` vs the registered entry's version.
    - ``parameter_schema_checksum`` vs the recomputed schema digest.
    - ``code_provenance_module`` / ``revision`` / ``symbol`` vs the
      registered entry's code provenance.

    On the first disagreement the function raises
    :class:`RegistryBindingError` with the failing slot, the recorded
    value and the current value attached so a reviewer can localise
    the drift.
    """
    reg = registry if registry is not None else default_registry()
    if binding.registry_version != REGISTRY_VERSION:
        raise RegistryBindingError(
            slot="registry_version",
            recorded=binding.registry_version,
            current=REGISTRY_VERSION,
        )
    current_checksum = reg.checksum()
    if binding.registry_checksum != current_checksum:
        raise RegistryBindingError(
            slot="registry_checksum",
            recorded=binding.registry_checksum,
            current=current_checksum,
        )
    # ``reg.lookup`` raises :class:`UnknownStrategyIdentityError`
    # when the binding names an identity the registry no longer
    # carries; the manifest authority never re-creates the identity
    # silently, so the call site sees the dedicated error.
    entry = reg.lookup(binding.strategy_identity)
    if binding.strategy_version != entry.version:
        raise RegistryBindingError(
            slot="strategy_version",
            recorded=binding.strategy_version,
            current=entry.version,
        )
    current_schema_checksum = compute_parameter_schema_checksum(entry.parameter_schemas)
    if binding.parameter_schema_checksum != current_schema_checksum:
        raise RegistryBindingError(
            slot="parameter_schema_checksum",
            recorded=binding.parameter_schema_checksum,
            current=current_schema_checksum,
        )
    if binding.code_provenance_module != entry.code_provenance.module:
        raise RegistryBindingError(
            slot="code_provenance_module",
            recorded=binding.code_provenance_module,
            current=entry.code_provenance.module,
        )
    if binding.code_provenance_revision != entry.code_provenance.revision:
        raise RegistryBindingError(
            slot="code_provenance_revision",
            recorded=binding.code_provenance_revision,
            current=entry.code_provenance.revision,
        )
    if binding.code_provenance_symbol != entry.code_provenance.symbol:
        raise RegistryBindingError(
            slot="code_provenance_symbol",
            recorded=binding.code_provenance_symbol,
            current=entry.code_provenance.symbol,
        )


def binding_parameter_dict(
    binding: StrategyBinding,
) -> dict[str, int | bool | str]:
    """Return the binding's validated parameters as a plain ``dict``.

    The helper is the bridge the manifest builder uses to copy the
    binding's parameter mapping onto the manifest's
    ``strategy_params`` field. The returned mapping is a fresh dict
    the caller may mutate without affecting the binding.
    """
    return {name: value for name, value in binding.validated_parameters}


__all__ = [
    "BINDING_VERSION",
    "RegistryBindingError",
    "StrategyBinding",
    "UnknownStrategyIdentityError",
    "assert_binding_matches_registry",
    "bind_strategy_to_registry",
    "binding_parameter_dict",
    "compute_parameter_schema_checksum",
]
