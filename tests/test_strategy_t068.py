"""Tests for the V1 strategy registry (T068).

T068 closes the strategy-identity authority loop: every strategy a
backtest or paper run may execute has one registered identity in
``robinhood_lp.strategy.registry``, and the registry is the only
path the manifest authority (T105) uses to translate an identity
string into its parameter schema, version and code provenance.

The tests cover every T068 acceptance clause:

- **Adaptive identity is registered.** The T065 adaptive-Range
  strategy has a registered identity, and a lookup by that
  identity returns its parameter schema, version and code
  provenance (test).

- **Unregistered / undeclared parameters rejected.** An identity
  the registry does not carry, and a parameter set the registered
  schema does not declare, are both rejected by the registry's
  validation surface instead of being accepted (test).

- **Deterministic enumeration.** Two enumerations over the same
  revision return the same order and the same registry checksum,
  and every registered identity's code provenance resolves to a
  module that exists (test).

- **Layer purity.** The layer check passes with no new
  suppression: ``python -m tools.check_imports check`` reports
  no finding for the registry module, and the registry imports
  neither the RPC adapter, storage, the signer, nor the Web
  entry point (test). The registry's own
  :func:`assert_registry_layer_is_pure` helper walks the live
  module graph and enforces the contract.

The must-not clauses are also covered:

- **No caller-supplied identities.** An unregistered identity is
  rejected; the registry never falls back to a default.
- **No weaker parameter check than the manifest path applies.**
  The validator rejects undeclared / missing / out-of-range /
  wrong-type parameters structurally.
- **No manifest publishing.** The registry publishes no manifest
  and offers no manifest validation; the T105 manifest authority
  owns that surface.
- **No RPC / storage / signing / presentation import.** The
  registry's dependency surface is stdlib + the protocol-domain
  contracts only; the layer-purity check enforces the rule.

Design constraints (binding):

- **Integer-only.** ``float`` never appears on the registry code
  path. Type bounds and Q64.64 ranges are integer arithmetic.
- **No RPC / storage / signing / Web import.** The registry
  depends only on the standard library and the lower
  protocol-domain contracts. The registry's
  :func:`assert_registry_layer_is_pure` helper walks the live
  module graph and rejects forbidden imports.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Mapping
from types import ModuleType
from typing import Final

import pytest

from robinhood_lp.strategy import (
    IDENTITY_ADAPTIVE_RANGE,
    IDENTITY_BROAD_RANGE,
    IDENTITY_FIXED_WIDTH,
    IDENTITY_HOLD,
    IDENTITY_OUT_OF_RANGE_REBALANCE,
    IDENTITY_VOLATILITY_WIDTH,
    REGISTRY_VERSION,
    CodeProvenance,
    InvalidParameterSchemaError,
    InvalidParameterValueError,
    ParameterSchema,
    ParameterType,
    RegisteredStrategy,
    Registry,
    RegistryError,
    UnknownStrategyIdentityError,
    assert_registry_layer_is_pure,
    default_registry,
    is_registered,
    lookup,
    registered_identities,
    registry_checksum,
    reset_default_registry_cache,
    resolve_module_revision,
    validate_parameters,
)

# ---------------------------------------------------------------------------
# Reference fixtures
# ---------------------------------------------------------------------------


_POOL_KEY_ID: Final[str] = "0x" + "ab" * 32
_CHAIN_ID: Final[int] = 46630
_TICK_SPACING: Final[int] = 60


#: The set of all identities the registry must enumerate today. The
#: test asserts the canonical sorted set; any drift is a regression
#: the manifest authority (T105) must hear about. The list is sorted
#: lexicographically because the registry returns identities in
#: sorted order (its deterministic ordering invariant).
EXPECTED_IDENTITIES: Final[tuple[str, ...]] = (
    "t062.broad_range.v1",
    "t062.fixed_width.v1",
    "t062.hold.v1",
    "t062.out_of_range_rebalance.v1",
    "t062.volatility_width.v1",
    "t065.adaptive_range.v1",
    "t102.model_backed.v1",
)


# ---------------------------------------------------------------------------
# 1. The registry enumerates the closed identity vocabulary
# ---------------------------------------------------------------------------


class TestRegistryEnumeration:
    """The default registry enumerates every registered identity."""

    def test_registry_enumerates_expected_identities(self) -> None:
        identities = registered_identities()
        assert identities == EXPECTED_IDENTITIES

    def test_registry_lists_seven_identities(self) -> None:
        # T062 ships five baselines; T065 adds the adaptive-Range
        # strategy; T102 adds the model-backed strategy. The closed
        # vocabulary is therefore seven.
        assert len(default_registry()) == 7

    def test_hold_is_registered(self) -> None:
        assert is_registered(IDENTITY_HOLD)

    def test_broad_range_is_registered(self) -> None:
        assert is_registered(IDENTITY_BROAD_RANGE)

    def test_fixed_width_is_registered(self) -> None:
        assert is_registered(IDENTITY_FIXED_WIDTH)

    def test_volatility_width_is_registered(self) -> None:
        assert is_registered(IDENTITY_VOLATILITY_WIDTH)

    def test_out_of_range_rebalance_is_registered(self) -> None:
        assert is_registered(IDENTITY_OUT_OF_RANGE_REBALANCE)

    def test_adaptive_range_is_registered(self) -> None:
        assert is_registered(IDENTITY_ADAPTIVE_RANGE)

    def test_registry_version_is_pinned(self) -> None:
        assert REGISTRY_VERSION == "t068.strategy_registry.v1"


# ---------------------------------------------------------------------------
# 2. The adaptive-Range identity returns its schema, version and provenance
# ---------------------------------------------------------------------------


class TestAdaptiveIdentityLookup:
    """The T065 adaptive-Range identity carries schema, version and provenance."""

    def test_adaptive_lookup_returns_registered_strategy(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        assert isinstance(entry, RegisteredStrategy)

    def test_adaptive_identity_matches(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        assert entry.identity == IDENTITY_ADAPTIVE_RANGE

    def test_adaptive_version_is_pinned(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        # The T065 module pins the strategy version; the registry
        # mirrors it. A drift is a contract break the manifest
        # authority must hear about.
        assert entry.version == "t065.adaptive_strategy.v1"

    def test_adaptive_code_provenance_module_is_recorded(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        assert isinstance(entry.code_provenance, CodeProvenance)
        assert entry.code_provenance.module == "robinhood_lp.strategy.adaptive"

    def test_adaptive_code_provenance_symbol_is_recorded(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        assert entry.code_provenance.symbol == "AdaptiveStrategy"

    def test_adaptive_code_provenance_revision_resolves(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        # Every registered identity's code provenance must resolve
        # to a module that exists (T068 acceptance). The revision
        # is either a 40-char hex SHA or ``"UNKNOWN"`` when the
        # checkout has no readable HEAD (e.g., an export). Either
        # value is accepted; the helper's contract is "does not
        # raise".
        revision = entry.code_provenance.revision
        assert isinstance(revision, str)
        assert revision != ""
        # When resolved, the revision is a 40-character hex SHA.
        # The worktree-based CI environment always resolves it.
        assert len(revision) == 40 or revision == "UNKNOWN"

    def test_adaptive_provenance_module_exists(self) -> None:
        # The "module that exists" acceptance clause: importable.
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        module = importlib.import_module(entry.code_provenance.module)
        assert hasattr(module, entry.code_provenance.symbol)

    def test_adaptive_parameter_schema_has_eighteen_entries(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        # The T065 parameter schema is the closed 18-field set the
        # adaptive strategy registers (see AdaptiveRangeParameters);
        # the registry's :class:`ParameterSchema` declarations mirror
        # the field-by-field contract.
        assert len(entry.parameter_schemas) == 18

    def test_adaptive_parameter_schema_names_match_adaptive_parameters(
        self,
    ) -> None:
        from robinhood_lp.strategy.adaptive import AdaptiveRangeParameters

        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        schema_names = {schema.name for schema in entry.parameter_schemas}
        # AdaptiveRangeParameters.__dataclass_fields__ is the
        # canonical, stable field list the registry mirrors.
        dataclass_field_names = set(AdaptiveRangeParameters.__dataclass_fields__)
        assert schema_names == dataclass_field_names

    def test_adaptive_parameter_schema_units_are_declared(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        for schema in entry.parameter_schemas:
            assert isinstance(schema.unit, str)
            assert schema.unit != ""

    def test_adaptive_parameter_schema_types_are_registered(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        for schema in entry.parameter_schemas:
            assert isinstance(schema.type, ParameterType)

    def test_adaptive_defaults_match_dataclass_defaults(self) -> None:
        from robinhood_lp.strategy.adaptive import AdaptiveRangeParameters

        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        defaults_instance = AdaptiveRangeParameters()
        for schema in entry.parameter_schemas:
            expected_default = getattr(defaults_instance, schema.name)
            assert schema.default == expected_default

    def test_adaptive_lookup_can_build_strategy(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        defaults = {schema.name: schema.default for schema in entry.parameter_schemas}
        validated = validate_parameters(IDENTITY_ADAPTIVE_RANGE, defaults)
        strategy = entry.factory.build(
            parameters=validated,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
        )
        assert type(strategy).__name__ == "AdaptiveStrategy"


# ---------------------------------------------------------------------------
# 3. Baseline identities return their schema, version and code provenance
# ---------------------------------------------------------------------------


class TestBaselineIdentityLookups:
    """Every T062 baseline identity returns the documented schema."""

    @pytest.fixture(autouse=True)
    def _defaults(self) -> Mapping[str, object]:
        return {"tick_spacing": _TICK_SPACING}

    def test_hold_lookup_has_no_parameters(self) -> None:
        entry = lookup(IDENTITY_HOLD)
        assert entry.parameter_schemas == ()

    def test_broad_range_lookup_has_three_parameters(self) -> None:
        entry = lookup(IDENTITY_BROAD_RANGE)
        names = {schema.name for schema in entry.parameter_schemas}
        assert names == {"tick_spacing", "liquidity", "capital_q64_64"}

    def test_fixed_width_lookup_has_four_parameters(self) -> None:
        entry = lookup(IDENTITY_FIXED_WIDTH)
        names = {schema.name for schema in entry.parameter_schemas}
        assert names == {
            "tick_spacing",
            "half_width_ticks",
            "liquidity",
            "capital_q64_64",
        }

    def test_volatility_width_lookup_has_seven_parameters(self) -> None:
        entry = lookup(IDENTITY_VOLATILITY_WIDTH)
        names = {schema.name for schema in entry.parameter_schemas}
        assert names == {
            "tick_spacing",
            "volatility_multiplier",
            "min_half_width_ticks",
            "max_half_width_ticks",
            "volatility_window",
            "liquidity",
            "capital_q64_64",
        }

    def test_out_of_range_rebalance_lookup_has_four_parameters(self) -> None:
        entry = lookup(IDENTITY_OUT_OF_RANGE_REBALANCE)
        names = {schema.name for schema in entry.parameter_schemas}
        assert names == {
            "tick_spacing",
            "half_width_ticks",
            "liquidity",
            "capital_q64_64",
        }

    def test_each_baseline_provenance_module_exists(self) -> None:
        for identity in (
            IDENTITY_HOLD,
            IDENTITY_BROAD_RANGE,
            IDENTITY_FIXED_WIDTH,
            IDENTITY_VOLATILITY_WIDTH,
            IDENTITY_OUT_OF_RANGE_REBALANCE,
        ):
            entry = lookup(identity)
            module = importlib.import_module(entry.code_provenance.module)
            assert hasattr(module, entry.code_provenance.symbol)

    def test_each_baseline_provenance_module_resolves_to_baselines(self) -> None:
        # Every T062 baseline must point to the T062 module. A
        # refactor that splits the baselines into a different
        # module would surface here, not silently.
        for identity in (
            IDENTITY_HOLD,
            IDENTITY_BROAD_RANGE,
            IDENTITY_FIXED_WIDTH,
            IDENTITY_VOLATILITY_WIDTH,
            IDENTITY_OUT_OF_RANGE_REBALANCE,
        ):
            entry = lookup(identity)
            assert entry.code_provenance.module == "robinhood_lp.strategy.baselines"

    def test_baseline_strategy_version_is_pinned(self) -> None:
        # The baselines all share one module version; the registry
        # surfaces it on every entry.
        for identity in (
            IDENTITY_HOLD,
            IDENTITY_BROAD_RANGE,
            IDENTITY_FIXED_WIDTH,
            IDENTITY_VOLATILITY_WIDTH,
            IDENTITY_OUT_OF_RANGE_REBALANCE,
        ):
            entry = lookup(identity)
            assert entry.version == "t062.baseline_strategy.v1"


# ---------------------------------------------------------------------------
# 4. Unregistered identity is rejected
# ---------------------------------------------------------------------------


class TestUnregisteredIdentityRejection:
    """The registry never accepts an identity outside its closed vocabulary."""

    def test_lookup_unknown_identity_raises(self) -> None:
        with pytest.raises(UnknownStrategyIdentityError):
            lookup("not.a.real.identity")

    def test_lookup_caller_invented_identity_raises(self) -> None:
        # The T068 acceptance clause: a caller cannot supply an
        # identity and have the registry fall back to it. The
        # registry never invents or accepts a caller-supplied
        # identity outside its closed vocabulary.
        with pytest.raises(UnknownStrategyIdentityError):
            lookup("caller_supplied.identity")

    def test_lookup_empty_string_is_rejected(self) -> None:
        with pytest.raises(UnknownStrategyIdentityError):
            lookup("")

    def test_lookup_non_string_is_rejected(self) -> None:
        with pytest.raises(UnknownStrategyIdentityError):
            lookup(12345)  # type: ignore[arg-type]

    def test_is_registered_returns_false_for_unknown(self) -> None:
        assert is_registered("not.a.real.identity") is False

    def test_is_registered_returns_false_for_empty(self) -> None:
        assert is_registered("") is False


# ---------------------------------------------------------------------------
# 5. Parameter validation: undeclared / missing / wrong type / out of range
# ---------------------------------------------------------------------------


class TestParameterValidation:
    """The validator rejects every malformed parameter set."""

    def test_validate_accepts_well_formed_baseline_params(self) -> None:
        params = {
            "tick_spacing": 60,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        result = validate_parameters(IDENTITY_BROAD_RANGE, params)
        assert result == params

    def test_validate_accepts_well_formed_adaptive_params(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        params = {schema.name: schema.default for schema in entry.parameter_schemas}
        result = validate_parameters(IDENTITY_ADAPTIVE_RANGE, params)
        assert result == params

    def test_validate_rejects_undeclared_parameter(self) -> None:
        params = {
            "tick_spacing": 60,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
            "extra_param": 42,
        }
        with pytest.raises(InvalidParameterValueError) as exc_info:
            validate_parameters(IDENTITY_BROAD_RANGE, params)
        assert "extra_param" in str(exc_info.value)
        assert "not declared" in str(exc_info.value)

    def test_validate_rejects_missing_parameter(self) -> None:
        params = {"tick_spacing": 60, "liquidity": 1000}
        with pytest.raises(InvalidParameterValueError) as exc_info:
            validate_parameters(IDENTITY_BROAD_RANGE, params)
        assert "capital_q64_64" in str(exc_info.value)

    def test_validate_rejects_empty_dict_for_baseline(self) -> None:
        with pytest.raises(InvalidParameterValueError) as exc_info:
            validate_parameters(IDENTITY_BROAD_RANGE, {})
        assert "missing" in str(exc_info.value).lower()

    def test_validate_rejects_wrong_type(self) -> None:
        params = {
            "tick_spacing": "not-an-int",
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        with pytest.raises(InvalidParameterValueError):
            validate_parameters(IDENTITY_BROAD_RANGE, params)

    def test_validate_rejects_zero_capital(self) -> None:
        params = {
            "tick_spacing": 60,
            "liquidity": 1000,
            "capital_q64_64": 0,
        }
        with pytest.raises(InvalidParameterValueError):
            validate_parameters(IDENTITY_BROAD_RANGE, params)

    def test_validate_rejects_negative_capital(self) -> None:
        params = {
            "tick_spacing": 60,
            "liquidity": 1000,
            "capital_q64_64": -1,
        }
        with pytest.raises(InvalidParameterValueError):
            validate_parameters(IDENTITY_BROAD_RANGE, params)

    def test_validate_rejects_out_of_range_tick_spacing(self) -> None:
        params = {
            "tick_spacing": 100_000,  # exceeds MAX_TICK_SPACING
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        with pytest.raises(InvalidParameterValueError):
            validate_parameters(IDENTITY_BROAD_RANGE, params)

    def test_validate_rejects_zero_tick_spacing(self) -> None:
        params = {
            "tick_spacing": 0,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        with pytest.raises(InvalidParameterValueError):
            validate_parameters(IDENTITY_BROAD_RANGE, params)

    def test_validate_rejects_bool_passed_as_int(self) -> None:
        # ``bool`` is rejected as ``int`` per the strict integer
        # contract (T068 mirrors the strategy layer's existing
        # type guard).
        params = {
            "tick_spacing": True,  # bool is not an int for the registry
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        with pytest.raises(InvalidParameterValueError):
            validate_parameters(IDENTITY_BROAD_RANGE, params)

    def test_validate_rejects_unknown_identity_with_parameter_dict(self) -> None:
        # An unregistered identity never reaches the parameter
        # checks; the registry raises the dedicated
        # :class:`UnknownStrategyIdentityError` first.
        with pytest.raises(UnknownStrategyIdentityError):
            validate_parameters("not.a.real.identity", {"any": 1})

    def test_validate_rejects_non_mapping_input(self) -> None:
        with pytest.raises(InvalidParameterValueError):
            validate_parameters(IDENTITY_BROAD_RANGE, [("tick_spacing", 60)])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 6. Deterministic enumeration / checksum
# ---------------------------------------------------------------------------


class TestDeterministicEnumeration:
    """Two enumerations over the same revision return the same order and checksum."""

    def test_identities_property_returns_sorted_tuple(self) -> None:
        identities = default_registry().identities
        assert identities == tuple(sorted(identities))
        assert identities == EXPECTED_IDENTITIES

    def test_enumeration_order_is_consistent(self) -> None:
        # Iterating the registry twice yields the same order. The
        # registry stores entries in sorted-by-identity order.
        first = list(default_registry())
        second = list(default_registry())
        assert first == second

    def test_checksum_is_stable_across_calls(self) -> None:
        # The T068 acceptance clause: two enumerations over the
        # same revision return the same registry checksum.
        first = registry_checksum()
        second = registry_checksum()
        assert first == second

    def test_checksum_is_sha256_hex(self) -> None:
        checksum = registry_checksum()
        assert isinstance(checksum, str)
        assert len(checksum) == 64
        # Hex only.
        int(checksum, 16)

    def test_checksum_depends_on_revision(self) -> None:
        # A registry with the same identities but a different
        # revision must produce a different checksum. The
        # experiment builds two registries by hand and checks
        # the property directly; the production checksum
        # captures the same invariant.
        from robinhood_lp.strategy.registry import _AdapterFactory

        def _factory(
            *,
            parameters: Mapping[str, int | bool | str],
            pool_key_id: str,
            chain_id: int,
        ) -> None:
            return None

        adapter = _AdapterFactory(_factory)
        entry_one = RegisteredStrategy(
            identity="t000.demo.v1",
            version="t000.demo.v1",
            parameter_schemas=(),
            code_provenance=CodeProvenance(
                module="robinhood_lp.strategy.registry",
                revision="aaaa" * 10,
                symbol="",
            ),
            factory=adapter,
        )
        entry_two = RegisteredStrategy(
            identity="t000.demo.v1",
            version="t000.demo.v1",
            parameter_schemas=(),
            code_provenance=CodeProvenance(
                module="robinhood_lp.strategy.registry",
                revision="bbbb" * 10,
                symbol="",
            ),
            factory=adapter,
        )
        registry_one = Registry(_entries=(entry_one,))
        registry_two = Registry(_entries=(entry_two,))
        assert registry_one.checksum() != registry_two.checksum()

    def test_checksum_depends_on_schema(self) -> None:
        # Two registries with the same identity / revision but a
        # different parameter schema produce different checksums.
        from robinhood_lp.strategy.registry import _AdapterFactory

        def _factory(
            *,
            parameters: Mapping[str, int | bool | str],
            pool_key_id: str,
            chain_id: int,
        ) -> None:
            return None

        adapter = _AdapterFactory(_factory)
        entry_one = RegisteredStrategy(
            identity="t000.demo.v1",
            version="t000.demo.v1",
            parameter_schemas=(
                ParameterSchema(
                    name="tick_spacing",
                    type=ParameterType.TICK_SPACING,
                    unit="ticks",
                    default=60,
                ),
            ),
            code_provenance=CodeProvenance(
                module="robinhood_lp.strategy.registry",
                revision="aaaa" * 10,
                symbol="",
            ),
            factory=adapter,
        )
        entry_two = RegisteredStrategy(
            identity="t000.demo.v1",
            version="t000.demo.v1",
            parameter_schemas=(
                ParameterSchema(
                    name="tick_spacing",
                    type=ParameterType.TICK_SPACING,
                    unit="ticks",
                    default=120,  # different default
                ),
            ),
            code_provenance=CodeProvenance(
                module="robinhood_lp.strategy.registry",
                revision="aaaa" * 10,
                symbol="",
            ),
            factory=adapter,
        )
        registry_one = Registry(_entries=(entry_one,))
        registry_two = Registry(_entries=(entry_two,))
        assert registry_one.checksum() != registry_two.checksum()

    def test_checksum_includes_registry_version(self) -> None:
        # Two registries with the same entries but a different
        # REGISTRY_VERSION prefix produce different checksums.
        # The experiment rebuilds one of them with a substituted
        # version string and checks the property directly.
        original_version = REGISTRY_VERSION
        try:
            import robinhood_lp.strategy.registry as reg

            object.__setattr__(reg, "REGISTRY_VERSION", "t068.strategy_registry.v2")
            try:
                checksum_with_v2 = reg.registry_checksum()
            finally:
                object.__setattr__(reg, "REGISTRY_VERSION", original_version)
            checksum_with_v1 = reg.registry_checksum()
            assert checksum_with_v1 != checksum_with_v2
        finally:
            # Best-effort restore in case the experiment raised
            # before the inner ``finally`` could run.
            import robinhood_lp.strategy.registry as reg

            object.__setattr__(reg, "REGISTRY_VERSION", original_version)

    def test_default_registry_is_cached(self) -> None:
        # The default registry is process-cached; two calls return
        # the same instance.
        first = default_registry()
        second = default_registry()
        assert first is second

    def test_reset_default_registry_cache_clears_cache(self) -> None:
        first = default_registry()
        reset_default_registry_cache()
        second = default_registry()
        # The reset + rebuild returns an equal-but-not-identical
        # registry: equality holds because the entries are
        # canonical-sorted, identity does not because the cache
        # was cleared.
        assert first is not second
        assert first.checksum() == second.checksum()

    def test_registry_iteration_preserves_sorted_order(self) -> None:
        identities_from_iter = tuple(entry.identity for entry in default_registry())
        assert identities_from_iter == EXPECTED_IDENTITIES

    def test_registry_supports_len(self) -> None:
        assert len(default_registry()) == 7


# ---------------------------------------------------------------------------
# 7. Code provenance module resolution
# ---------------------------------------------------------------------------


class TestCodeProvenance:
    """Every registered identity's code provenance resolves to a module that exists."""

    def test_all_registered_modules_exist(self) -> None:
        for entry in default_registry():
            module = importlib.import_module(entry.code_provenance.module)
            # The module must exist and be importable; the symbol
            # need not exist (the registry may not bind a symbol
            # in every case), but the module must.
            assert module.__name__ == entry.code_provenance.module

    def test_all_registered_symbols_exist_when_declared(self) -> None:
        for entry in default_registry():
            if entry.code_provenance.symbol == "":
                continue
            module = importlib.import_module(entry.code_provenance.module)
            assert hasattr(module, entry.code_provenance.symbol), (
                f"symbol {entry.code_provenance.symbol!r} missing from "
                f"module {entry.code_provenance.module!r}"
            )

    def test_resolve_module_revision_returns_hex_sha_or_unknown(self) -> None:
        for module_name in (
            "robinhood_lp.strategy.baselines",
            "robinhood_lp.strategy.adaptive",
            "robinhood_lp.strategy.base",
        ):
            revision = resolve_module_revision(module_name=module_name)
            assert isinstance(revision, str)
            assert revision != ""
            assert len(revision) == 40 or revision == "UNKNOWN"

    def test_provenance_matches_module(self) -> None:
        provenance = CodeProvenance(
            module="robinhood_lp.strategy.baselines",
            revision="aaaa" * 10,
            symbol="HoldStrategy",
        )
        assert provenance.matches_module(module_name="robinhood_lp.strategy.baselines")
        assert provenance.matches_module(module_name="robinhood_lp.strategy.baselines.foo")
        assert not provenance.matches_module(module_name="robinhood_lp.strategy.baselinesx")
        assert not provenance.matches_module(module_name="")
        assert not provenance.matches_module(module_name=12345)  # type: ignore[arg-type]

    def test_provenance_rejects_invalid_inputs(self) -> None:
        with pytest.raises(RegistryError):
            CodeProvenance(module="", revision="r", symbol="")
        with pytest.raises(RegistryError):
            CodeProvenance(module="robinhood_lp.x", revision="", symbol="")


# ---------------------------------------------------------------------------
# 8. Layer purity
# ---------------------------------------------------------------------------


class TestLayerPurity:
    """The registry module imports only the stdlib and protocol contracts."""

    def test_registry_layer_is_pure(self) -> None:
        # The assertion walks the live module graph and rejects
        # forbidden imports. The contract is binding; the manifest
        # authority cannot bind to a registry that imports RPC,
        # storage, signing, presentation, or the Web entry point.
        assert_registry_layer_is_pure()

    def test_registry_does_not_import_rpc(self) -> None:
        module = importlib.import_module("robinhood_lp.strategy.registry")
        imports = _walk_module_imports(module)
        assert not any(
            name == "robinhood_lp.rpc" or name.startswith("robinhood_lp.rpc.") for name in imports
        )

    def test_registry_does_not_import_storage(self) -> None:
        module = importlib.import_module("robinhood_lp.strategy.registry")
        imports = _walk_module_imports(module)
        assert not any(
            name == "robinhood_lp.storage" or name.startswith("robinhood_lp.storage.")
            for name in imports
        )

    def test_registry_does_not_import_signer(self) -> None:
        module = importlib.import_module("robinhood_lp.strategy.registry")
        imports = _walk_module_imports(module)
        assert not any(
            name == "robinhood_lp.signer" or name.startswith("robinhood_lp.signer.")
            for name in imports
        )

    def test_registry_does_not_import_web(self) -> None:
        module = importlib.import_module("robinhood_lp.strategy.registry")
        imports = _walk_module_imports(module)
        assert not any(
            name == "robinhood_lp.web" or name.startswith("robinhood_lp.web.") for name in imports
        )


def _walk_module_imports(module: ModuleType) -> set[str]:
    """Return every module name reachable from ``module`` via ``__dict__`` lookup."""
    seen: set[str] = set()
    stack = [module]
    out: set[str] = set()
    while stack:
        current = stack.pop()
        if current.__name__ in seen:
            continue
        seen.add(current.__name__)
        for _attr, attr in vars(current).items():
            if isinstance(attr, type(sys)):
                out.add(attr.__name__)
                if (
                    attr.__name__.startswith("robinhood_lp.")
                    or attr.__name__ == "robinhood_lp.strategy.registry"
                ):
                    stack.append(attr)
    return out


# ---------------------------------------------------------------------------
# 9. Schema validation surface
# ---------------------------------------------------------------------------


class TestParameterSchemaValidation:
    """The schema's __post_init__ rejects malformed declarations."""

    def test_schema_rejects_empty_name(self) -> None:
        with pytest.raises(InvalidParameterSchemaError):
            ParameterSchema(
                name="",
                type=ParameterType.TICK_SPACING,
                unit="ticks",
                default=60,
            )

    def test_schema_rejects_empty_unit(self) -> None:
        with pytest.raises(InvalidParameterSchemaError):
            ParameterSchema(
                name="tick_spacing",
                type=ParameterType.TICK_SPACING,
                unit="",
                default=60,
            )

    def test_schema_rejects_default_below_lower_bound(self) -> None:
        with pytest.raises(InvalidParameterSchemaError):
            ParameterSchema(
                name="half_width_ticks",
                type=ParameterType.POSITIVE_INT,
                unit="ticks",
                default=0,  # below POSITIVE_INT's lower bound
                lower_bound=1,
            )

    def test_schema_rejects_default_above_upper_bound(self) -> None:
        with pytest.raises(InvalidParameterSchemaError):
            ParameterSchema(
                name="tick_spacing",
                type=ParameterType.TICK_SPACING,
                unit="ticks",
                default=60,
                upper_bound=10,
            )

    def test_schema_rejects_lower_bound_above_upper_bound(self) -> None:
        with pytest.raises(InvalidParameterSchemaError):
            ParameterSchema(
                name="x",
                type=ParameterType.INT,
                unit="unitless",
                default=0,
                lower_bound=10,
                upper_bound=5,
            )

    def test_schema_rejects_default_wrong_type(self) -> None:
        with pytest.raises(InvalidParameterSchemaError):
            ParameterSchema(
                name="tick_spacing",
                type=ParameterType.TICK_SPACING,
                unit="ticks",
                default="abc",  # str is not a valid TICK_SPACING
            )

    def test_schema_accepts_valid_q64_64_zero_default(self) -> None:
        # ``Q64_64`` accepts ``0``; ``STRICT_Q64_64`` does not.
        schema = ParameterSchema(
            name="some_fraction",
            type=ParameterType.Q64_64,
            unit="Q64.64",
            default=0,
        )
        assert schema.default == 0

    def test_schema_rejects_strict_q64_64_zero_default(self) -> None:
        with pytest.raises(InvalidParameterSchemaError):
            ParameterSchema(
                name="capital",
                type=ParameterType.STRICT_Q64_64,
                unit="Q64.64",
                default=0,
            )


# ---------------------------------------------------------------------------
# 10. End-to-end: factory build from validated parameters
# ---------------------------------------------------------------------------


class TestFactoryBuild:
    """Every registered strategy can be built from a validated parameter set."""

    def test_hold_factory_builds_hold_strategy(self) -> None:
        entry = lookup(IDENTITY_HOLD)
        validated = validate_parameters(IDENTITY_HOLD, {})
        strategy = entry.factory.build(
            parameters=validated,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
        )
        assert type(strategy).__name__ == "HoldStrategy"

    def test_broad_range_factory_builds_broad_range_strategy(self) -> None:
        entry = lookup(IDENTITY_BROAD_RANGE)
        validated = validate_parameters(
            IDENTITY_BROAD_RANGE,
            {"tick_spacing": 60, "liquidity": 1000, "capital_q64_64": 1 << 64},
        )
        strategy = entry.factory.build(
            parameters=validated,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
        )
        assert type(strategy).__name__ == "BroadRangeStrategy"

    def test_fixed_width_factory_builds_fixed_width_strategy(self) -> None:
        entry = lookup(IDENTITY_FIXED_WIDTH)
        validated = validate_parameters(
            IDENTITY_FIXED_WIDTH,
            {
                "tick_spacing": 60,
                "half_width_ticks": 600,
                "liquidity": 1000,
                "capital_q64_64": 1 << 64,
            },
        )
        strategy = entry.factory.build(
            parameters=validated,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
        )
        assert type(strategy).__name__ == "FixedWidthStrategy"

    def test_volatility_width_factory_builds_volatility_width_strategy(self) -> None:
        entry = lookup(IDENTITY_VOLATILITY_WIDTH)
        validated = validate_parameters(
            IDENTITY_VOLATILITY_WIDTH,
            {
                "tick_spacing": 60,
                "volatility_multiplier": 2,
                "min_half_width_ticks": 60,
                "max_half_width_ticks": 5_000,
                "volatility_window": 20,
                "liquidity": 1000,
                "capital_q64_64": 1 << 64,
            },
        )
        strategy = entry.factory.build(
            parameters=validated,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
        )
        assert type(strategy).__name__ == "VolatilityWidthStrategy"

    def test_out_of_range_rebalance_factory_builds_strategy(self) -> None:
        entry = lookup(IDENTITY_OUT_OF_RANGE_REBALANCE)
        validated = validate_parameters(
            IDENTITY_OUT_OF_RANGE_REBALANCE,
            {
                "tick_spacing": 60,
                "half_width_ticks": 600,
                "liquidity": 1000,
                "capital_q64_64": 1 << 64,
            },
        )
        strategy = entry.factory.build(
            parameters=validated,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
        )
        assert type(strategy).__name__ == "OutOfRangeRebalanceStrategy"

    def test_adaptive_factory_builds_adaptive_strategy(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        defaults = {schema.name: schema.default for schema in entry.parameter_schemas}
        validated = validate_parameters(IDENTITY_ADAPTIVE_RANGE, defaults)
        strategy = entry.factory.build(
            parameters=validated,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
        )
        assert type(strategy).__name__ == "AdaptiveStrategy"


# ---------------------------------------------------------------------------
# 11. Public surface: module-level helpers
# ---------------------------------------------------------------------------


class TestModuleLevelHelpers:
    """The module-level helpers wrap the default registry."""

    def test_is_registered_helper(self) -> None:
        assert is_registered(IDENTITY_HOLD) is True
        assert is_registered("not.a.real.identity") is False

    def test_lookup_helper(self) -> None:
        entry = lookup(IDENTITY_HOLD)
        assert entry.identity == IDENTITY_HOLD

    def test_validate_parameters_helper(self) -> None:
        params = {
            "tick_spacing": 60,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        result = validate_parameters(IDENTITY_BROAD_RANGE, params)
        assert result == params

    def test_registry_checksum_helper(self) -> None:
        assert registry_checksum() == default_registry().checksum()

    def test_registered_identities_helper(self) -> None:
        assert registered_identities() == default_registry().identities


# ---------------------------------------------------------------------------
# 12. Registry construction-time invariants
# ---------------------------------------------------------------------------


class TestRegistryConstruction:
    """The :class:`Registry` constructor enforces its invariants."""

    def test_registry_rejects_duplicate_identity(self) -> None:
        from robinhood_lp.strategy.registry import _AdapterFactory

        def _factory(
            *,
            parameters: Mapping[str, int | bool | str],
            pool_key_id: str,
            chain_id: int,
        ) -> None:
            return None

        adapter = _AdapterFactory(_factory)
        entry = RegisteredStrategy(
            identity="t000.dupe.v1",
            version="v1",
            parameter_schemas=(),
            code_provenance=CodeProvenance(
                module="robinhood_lp.strategy.registry",
                revision="aaaa" * 10,
                symbol="",
            ),
            factory=adapter,
        )
        with pytest.raises(RegistryError):
            Registry(_entries=(entry, entry))

    def test_registry_sorts_entries_by_identity(self) -> None:
        from robinhood_lp.strategy.registry import _AdapterFactory

        def _factory(
            *,
            parameters: Mapping[str, int | bool | str],
            pool_key_id: str,
            chain_id: int,
        ) -> None:
            return None

        adapter = _AdapterFactory(_factory)
        a_entry = RegisteredStrategy(
            identity="a.test.v1",
            version="v1",
            parameter_schemas=(),
            code_provenance=CodeProvenance(
                module="robinhood_lp.strategy.registry",
                revision="aaaa" * 10,
                symbol="",
            ),
            factory=adapter,
        )
        b_entry = RegisteredStrategy(
            identity="b.test.v1",
            version="v1",
            parameter_schemas=(),
            code_provenance=CodeProvenance(
                module="robinhood_lp.strategy.registry",
                revision="bbbb" * 10,
                symbol="",
            ),
            factory=adapter,
        )
        # Insert in reverse alphabetical order; the registry must
        # surface them sorted.
        registry = Registry(_entries=(b_entry, a_entry))
        identities = [entry.identity for entry in registry]
        assert identities == ["a.test.v1", "b.test.v1"]

    def test_registry_rejects_non_registered_strategy_entry(self) -> None:
        with pytest.raises(RegistryError):
            Registry(_entries=("not-a-registered-strategy",))  # type: ignore[arg-type]

    def test_registered_strategy_rejects_non_code_provenance(self) -> None:
        from robinhood_lp.strategy.registry import _AdapterFactory

        def _factory(
            *,
            parameters: Mapping[str, int | bool | str],
            pool_key_id: str,
            chain_id: int,
        ) -> None:
            return None

        adapter = _AdapterFactory(_factory)
        with pytest.raises(RegistryError):
            RegisteredStrategy(
                identity="x",
                version="v1",
                parameter_schemas=(),
                code_provenance="not-a-provenance",  # type: ignore[arg-type]
                factory=adapter,
            )

    def test_registered_strategy_rejects_non_strategy_factory(self) -> None:
        with pytest.raises(RegistryError):
            RegisteredStrategy(
                identity="x",
                version="v1",
                parameter_schemas=(),
                code_provenance=CodeProvenance(
                    module="robinhood_lp.strategy.registry",
                    revision="aaaa" * 10,
                    symbol="",
                ),
                factory="not-a-factory",  # type: ignore[arg-type]
            )
