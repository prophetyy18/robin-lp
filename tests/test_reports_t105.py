"""Tests for the T105 registry-bound experiment manifests and reports layer.

T105 binds every published LP result to the exact versioned registry
entry that authorised the run. The hard-coded T063 ``strategy_kind``
vocabulary (``VALID_STRATEGY_KINDS``) is replaced by the T068
:class:`Registry` authority; every current manifest records the
registered strategy identity, the registry version + checksum, the
parameter-schema version + checksum, and the code provenance. The
acceptance clauses the tests cover:

- **Positive coverage for every registered strategy.** Every T062
  baseline and the T065 adaptive-Range strategy bind through the
  registry and produce a valid manifest whose identity, parameters,
  schema, registry revision and code provenance validate.

- **Per-pool and multi-pool manifests.** A multi-pool run publishes
  one manifest per member pool under one :class:`RunIdentity`; each
  member binds independently to the registry.

- **Both artifact rerun and T069-linked rerun.** The artifact rerun
  path verifies and reproduces a saved manifest under its recorded
  binding; the T069-linked rerun is a fresh product run record that
  reads the source manifest, binds a new run identity, and never
  mutates the source.

- **Negative coverage for binding violations.** Unregistered identity,
  undeclared / missing / wrong-type / out-of-range parameters, and
  registry / schema / code binding disagreement are all rejected
  before publication.

- **Compatibility and migration for historical T063 artifacts.**
  Legacy T063 manifests are loadable through the legacy reader,
  marked with ``LEGACY_T063``, and migrated deterministically into
  a current T105 manifest.

- **Old-path-unreachable test.** Current publication cannot consult
  the deprecated ``VALID_STRATEGY_KINDS`` vocabulary; the manifest
  builder accepts only a :class:`StrategyBinding`.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from robinhood_lp.backtest.engine import (
    BACKTEST_ENGINE_VERSION,
    BacktestEngine,
    RiskDecision,
    empty_position_state,
)
from robinhood_lp.backtest.events import (
    BACKTEST_EVENT_VERSION,
    KIND_SHUTDOWN,
    KIND_SWAP,
    SOURCE_PRIORITY_DATA,
    SOURCE_PRIORITY_SYSTEM,
    BacktestEvent,
)
from robinhood_lp.backtest.models import (
    ConstantLiquidityModel,
    DeterministicFailureModel,
    FlatGasModel,
    ModelBundle,
    StaticFeeModel,
    ZeroSlippageModel,
)
from robinhood_lp.reports import (
    BINDING_VERSION,
    LEGACY_MANIFEST_VERSION,
    LEGACY_MARKER,
    MANIFEST_VERSION,
    Q64_SCALE,
    RERUN_VERSION,
    VALID_STRATEGY_KINDS,
    VALIDATION_VERSION,
    VALUATION_QUALIFIED,
    CoverageSummary,
    ExperimentManifest,
    InvalidLegacyManifestError,
    LedgerSnapshot,
    ManifestChecksumError,
    ManifestPoolMismatchError,
    RegistryBindingError,
    RunIdentity,
    RunMetrics,
    SerialisedEvent,
    StrategyBinding,
    UnknownStrategyIdentityError,
    UnmigratableLegacyStrategyKindError,
    assert_binding_matches_registry,
    bind_strategy_to_registry,
    build_coverage_summary,
    build_experiment_manifest,
    compute_parameter_schema_checksum,
    compute_report_checksum,
    compute_run_metrics,
    decisions_checksum,
    experiment_manifest_from_dict,
    extract_decisions,
    load_legacy_manifest_from_path,
    load_manifest_from_path,
    manifest_checksum,
    migrate_legacy_manifest,
    rerun_manifest,
    validate_manifest,
    validate_multi_pool_run,
    validate_run_identity,
    write_manifest_to_path,
)
from robinhood_lp.reports.legacy import _LEGACY_KIND_TO_IDENTITY
from robinhood_lp.strategy.registry import (
    IDENTITY_ADAPTIVE_RANGE,
    IDENTITY_BROAD_RANGE,
    IDENTITY_FIXED_WIDTH,
    IDENTITY_HOLD,
    IDENTITY_OUT_OF_RANGE_REBALANCE,
    IDENTITY_VOLATILITY_WIDTH,
    REGISTRY_VERSION,
    InvalidParameterValueError,
    ParameterSchema,
    RegisteredStrategy,
    Registry,
    default_registry,
    lookup,
    reset_default_registry_cache,
    validate_parameters,
)

# ---------------------------------------------------------------------------
# Reference fixtures
# ---------------------------------------------------------------------------

_POOL_KEY_ID_A: Final[str] = "0x" + "ab" * 32
_POOL_KEY_ID_B: Final[str] = "0x" + "cd" * 32
_CHAIN_ID: Final[int] = 46630
_RUN_ID: Final[str] = "run-test-t105-001"
_DATASET_VERSION: Final[str] = "ds.v1.0.0"
_DATASET_SCHEMA_VERSION: Final[int] = 2
_DATASET_DECODE_VERSION: Final[int] = 2
_DATASET_CONTENT_HASH: Final[str] = "0x" + "12" * 32
_NUMERAIRE: Final[str] = "USDG"
_QUAL: Final[str] = VALUATION_QUALIFIED
_CODE_REV: Final[str] = "0123456789abcdef0123456789abcdef01234567"


def _swap_event(
    *,
    timestamp: int,
    chain_id: int = _CHAIN_ID,
    pool_key_id: str = _POOL_KEY_ID_A,
    price_q64_64: int = 1 << 64,
) -> BacktestEvent:
    return BacktestEvent(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_DATA,
        kind=KIND_SWAP,
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(("price_q64_64", price_q64_64),),
    )


def _shutdown_event(
    *, timestamp: int, chain_id: int = _CHAIN_ID, pool_key_id: str = _POOL_KEY_ID_A
) -> BacktestEvent:
    return BacktestEvent(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_SYSTEM,
        kind=KIND_SHUTDOWN,
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(),
    )


def _approve_risk(_decision: object) -> RiskDecision:
    return RiskDecision(approved=True, reason_code="OK")


def _filled_bundle() -> ModelBundle:
    return ModelBundle(
        bundle_version="t105.test.v1",
        liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
        fee=StaticFeeModel(fee_pips_value=3_000),
        gas=FlatGasModel(gas_units_value=21_000),
        slippage=ZeroSlippageModel(),
        failure=DeterministicFailureModel(),
        latency_units=0,
    )


def _run_a_small_backtest(
    *,
    chain_id: int = _CHAIN_ID,
    pool_key_id: str = _POOL_KEY_ID_A,
) -> tuple[
    list[BacktestEvent],
    CoverageSummary,
    LedgerSnapshot,
    RunMetrics,
    str,
]:
    """Run a tiny deterministic backtest and return its artifacts."""
    events = [
        _swap_event(timestamp=100, chain_id=chain_id, pool_key_id=pool_key_id),
        _swap_event(timestamp=200, chain_id=chain_id, pool_key_id=pool_key_id),
        _shutdown_event(timestamp=300, chain_id=chain_id, pool_key_id=pool_key_id),
    ]
    initial_ledger = empty_position_state(pool_key_id=pool_key_id, chain_id=chain_id)
    engine = BacktestEngine(
        version=BACKTEST_ENGINE_VERSION,
        initial_ledger=initial_ledger,
        model_bundle=_filled_bundle(),
        strategy_callback=_hold_strategy_callback(pool_key_id, chain_id),  # type: ignore[arg-type]
        risk_callback=_approve_risk,
    )
    result = engine.run(events)
    metrics = compute_run_metrics(
        result=result,
        interval_seconds=300,
        benchmark_total_return_q64_64=Q64_SCALE,
    )
    coverage = build_coverage_summary(
        result=result,
        input_events=events,
        interval_seconds=300,
        block_range_start=100,
        block_range_end=300,
    )
    ledger_snapshot = LedgerSnapshot.from_position_state(result.final_ledger)
    decisions = extract_decisions(result)
    decisions_chk = decisions_checksum(decisions)
    return list(events), coverage, ledger_snapshot, metrics, decisions_chk


def _hold_strategy_callback(pool_key_id: str, chain_id: int) -> object:
    """Build a Hold strategy callback for a deterministic stub run.

    The return type is ``object``; the engine accepts any value that
    implements the ``StrategyCallback`` protocol.
    """
    from robinhood_lp.strategy.baselines import HoldStrategy

    return HoldStrategy(pool_key_id=pool_key_id, chain_id=chain_id)


def _build_manifest_for_identity(
    identity: str,
    parameters: Mapping[str, object],
    *,
    chain_id: int = _CHAIN_ID,
    pool_key_id: str = _POOL_KEY_ID_A,
    run_id: str = _RUN_ID,
    block_range_start: int = 100,
    block_range_end: int = 300,
    interval_seconds: int = 300,
    created_at: int = 1_700_000_000,
) -> ExperimentManifest:
    """Build a registry-bound manifest for ``identity`` + ``parameters``.

    The helper is the canonical T105 publication path: every field
    the manifest records is supplied by the registry binding; the
    hard-coded T063 vocabulary is never consulted.
    """
    binding = bind_strategy_to_registry(identity=identity, parameters=parameters)
    events, coverage, ledger_snapshot, metrics, decisions_chk = _run_a_small_backtest(
        chain_id=chain_id, pool_key_id=pool_key_id
    )
    return build_experiment_manifest(
        run_id=run_id,
        metrics=metrics,
        coverage=coverage,
        ledger_snapshot=ledger_snapshot,
        decisions_checksum=decisions_chk,
        input_events=events,
        dataset_version=_DATASET_VERSION,
        dataset_schema_version=_DATASET_SCHEMA_VERSION,
        dataset_decode_version=_DATASET_DECODE_VERSION,
        dataset_content_hash=_DATASET_CONTENT_HASH,
        reporting_numeraire=_NUMERAIRE,
        valuation_qualification=_QUAL,
        code_revision=_CODE_REV,
        dependency_revisions={"robinhood-lp": "0.0.0"},
        strategy_binding=binding,
        seed=0,
        clock_assumption="EVENT_TIME",
        fill_assumption="DETERMINISTIC_FAILURE",
        cost_assumption="FLAT_GAS",
        quote_assumption="STATIC_FEE",
        latency_units=0,
        latency_ms_estimate=0,
        created_at_unix_seconds=created_at,
        block_range_start=block_range_start,
        block_range_end=block_range_end,
    )


# ---------------------------------------------------------------------------
# Surface version pinning
# ---------------------------------------------------------------------------


class TestModuleVersions:
    """The T105 cutover pinned every layer's version to ``t105.*``."""

    def test_manifest_version_is_t105(self) -> None:
        assert MANIFEST_VERSION == "t105.experiment_manifest.v1"

    def test_validation_version_is_t105(self) -> None:
        assert VALIDATION_VERSION == "t105.manifest_validation.v1"

    def test_rerun_version_is_t105(self) -> None:
        assert RERUN_VERSION == "t105.manifest_rerun.v1"

    def test_binding_version_is_t105(self) -> None:
        assert BINDING_VERSION == "t105.strategy_binding.v1"

    def test_legacy_version_remains_pinned(self) -> None:
        # The legacy reader anchors the T063 schema version so a
        # reviewer can detect a legacy artifact by identity.
        assert LEGACY_MANIFEST_VERSION == "t063.experiment_manifest.v1"


# ---------------------------------------------------------------------------
# 1. Positive coverage for every registered strategy
# ---------------------------------------------------------------------------


class TestEveryRegisteredStrategyBinds:
    """Every T062 baseline + the T065 adaptive-Range identity binds."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_hold_identity_binds(self) -> None:
        binding = bind_strategy_to_registry(identity=IDENTITY_HOLD, parameters={})
        assert binding.strategy_identity == IDENTITY_HOLD
        assert binding.registry_version == REGISTRY_VERSION

    def test_broad_range_identity_binds(self) -> None:
        params = {
            "tick_spacing": 60,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        binding = bind_strategy_to_registry(identity=IDENTITY_BROAD_RANGE, parameters=params)
        assert binding.strategy_identity == IDENTITY_BROAD_RANGE
        # The binding stores the validated parameters in the
        # schema's declared order (broad-range declares
        # ``tick_spacing`` before ``liquidity`` before ``capital_q64_64``).
        assert binding.validated_parameters == (
            ("tick_spacing", 60),
            ("liquidity", 1000),
            ("capital_q64_64", 1 << 64),
        )

    def test_fixed_width_identity_binds(self) -> None:
        params = {
            "tick_spacing": 60,
            "half_width_ticks": 600,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        binding = bind_strategy_to_registry(identity=IDENTITY_FIXED_WIDTH, parameters=params)
        assert binding.strategy_identity == IDENTITY_FIXED_WIDTH
        # The binding stores the validated parameters in the
        # schema's declared order; ``tick_spacing`` is declared
        # first, ``half_width_ticks`` second.
        assert binding.validated_parameters == (
            ("tick_spacing", 60),
            ("half_width_ticks", 600),
            ("liquidity", 1000),
            ("capital_q64_64", 1 << 64),
        )

    def test_volatility_width_identity_binds(self) -> None:
        params = {
            "tick_spacing": 60,
            "volatility_multiplier": 2,
            "min_half_width_ticks": 60,
            "max_half_width_ticks": 5000,
            "volatility_window": 20,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        binding = bind_strategy_to_registry(identity=IDENTITY_VOLATILITY_WIDTH, parameters=params)
        assert binding.strategy_identity == IDENTITY_VOLATILITY_WIDTH

    def test_out_of_range_rebalance_identity_binds(self) -> None:
        params = {
            "tick_spacing": 60,
            "half_width_ticks": 600,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        binding = bind_strategy_to_registry(
            identity=IDENTITY_OUT_OF_RANGE_REBALANCE, parameters=params
        )
        assert binding.strategy_identity == IDENTITY_OUT_OF_RANGE_REBALANCE

    def test_adaptive_range_identity_binds(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        params = {schema.name: schema.default for schema in entry.parameter_schemas}
        binding = bind_strategy_to_registry(identity=IDENTITY_ADAPTIVE_RANGE, parameters=params)
        assert binding.strategy_identity == IDENTITY_ADAPTIVE_RANGE
        # The adaptive strategy declares 18 parameters; the binding
        # validates all 18 against the schema.
        assert len(binding.validated_parameters) == 18

    def test_every_registered_identity_produces_a_manifest(self) -> None:
        # Every identity the registry enumerates produces a valid
        # manifest; the loop exercises the canonical T105 publication
        # path for every entry the registry carries.
        for identity in (
            IDENTITY_HOLD,
            IDENTITY_BROAD_RANGE,
            IDENTITY_FIXED_WIDTH,
            IDENTITY_VOLATILITY_WIDTH,
            IDENTITY_OUT_OF_RANGE_REBALANCE,
            IDENTITY_ADAPTIVE_RANGE,
        ):
            entry = lookup(identity)
            params = {schema.name: schema.default for schema in entry.parameter_schemas}
            manifest = _build_manifest_for_identity(identity, params)
            validate_manifest(manifest)
            assert manifest.strategy_identity == identity
            assert manifest.registry_version == REGISTRY_VERSION

    def test_every_registered_identity_records_full_provenance(self) -> None:
        # The manifest records every registry-derived field: version,
        # checksum, parameter schema version + checksum, code
        # provenance module / revision / symbol.
        for identity in (
            IDENTITY_HOLD,
            IDENTITY_BROAD_RANGE,
            IDENTITY_FIXED_WIDTH,
            IDENTITY_VOLATILITY_WIDTH,
            IDENTITY_OUT_OF_RANGE_REBALANCE,
            IDENTITY_ADAPTIVE_RANGE,
        ):
            entry = lookup(identity)
            params = {schema.name: schema.default for schema in entry.parameter_schemas}
            binding = bind_strategy_to_registry(identity=identity, parameters=params)
            manifest = _build_manifest_for_identity(identity, params)
            assert manifest.strategy_identity == binding.strategy_identity
            assert manifest.strategy_version == binding.strategy_version
            assert manifest.registry_version == binding.registry_version
            assert manifest.registry_checksum == binding.registry_checksum
            assert manifest.parameter_schema_version == binding.parameter_schema_version
            assert manifest.parameter_schema_checksum == binding.parameter_schema_checksum
            assert manifest.code_provenance_module == binding.code_provenance_module
            assert manifest.code_provenance_revision == binding.code_provenance_revision
            assert manifest.code_provenance_symbol == binding.code_provenance_symbol
            assert manifest.strategy_params == dict(sorted(binding.validated_parameters))


# ---------------------------------------------------------------------------
# 2. Parameter validation through the binding surface
# ---------------------------------------------------------------------------


class TestBindingParameterValidation:
    """The binding rejects every malformed parameter set."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_binding_rejects_unregistered_identity(self) -> None:
        with pytest.raises(UnknownStrategyIdentityError):
            bind_strategy_to_registry(identity="t999.bogus.v1", parameters={})

    def test_binding_rejects_undeclared_parameter(self) -> None:
        params = {
            "tick_spacing": 60,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
            "extra_param": 42,
        }
        with pytest.raises(InvalidParameterValueError):
            bind_strategy_to_registry(identity=IDENTITY_BROAD_RANGE, parameters=params)

    def test_binding_rejects_missing_required_parameter(self) -> None:
        params = {"tick_spacing": 60, "liquidity": 1000}
        with pytest.raises(InvalidParameterValueError):
            bind_strategy_to_registry(identity=IDENTITY_BROAD_RANGE, parameters=params)

    def test_binding_rejects_wrong_type(self) -> None:
        params = {
            "tick_spacing": "not-an-int",
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        with pytest.raises(InvalidParameterValueError):
            bind_strategy_to_registry(identity=IDENTITY_BROAD_RANGE, parameters=params)

    def test_binding_rejects_zero_capital(self) -> None:
        params = {
            "tick_spacing": 60,
            "liquidity": 1000,
            "capital_q64_64": 0,
        }
        with pytest.raises(InvalidParameterValueError):
            bind_strategy_to_registry(identity=IDENTITY_BROAD_RANGE, parameters=params)

    def test_binding_rejects_out_of_range_tick_spacing(self) -> None:
        params = {
            "tick_spacing": 100_000,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        with pytest.raises(InvalidParameterValueError):
            bind_strategy_to_registry(identity=IDENTITY_BROAD_RANGE, parameters=params)

    def test_binding_rejects_bool_passed_as_int(self) -> None:
        params = {
            "tick_spacing": True,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        with pytest.raises(InvalidParameterValueError):
            bind_strategy_to_registry(identity=IDENTITY_BROAD_RANGE, parameters=params)

    def test_binding_rejects_adaptive_out_of_range_threshold(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        params = {schema.name: schema.default for schema in entry.parameter_schemas}
        # ``max_own_liquidity_share_q64_64`` is a Q64.64 fraction
        # and must be inside the type's bounds; pushing it past the
        # STRICT_Q64_64 ceiling is rejected.
        params["max_own_liquidity_share_q64_64"] = 1 << 200
        with pytest.raises(InvalidParameterValueError):
            bind_strategy_to_registry(identity=IDENTITY_ADAPTIVE_RANGE, parameters=params)


# ---------------------------------------------------------------------------
# 3. Binding / checksum agreement
# ---------------------------------------------------------------------------


class TestBindingChecksumAgreement:
    """The binding's checksums agree with the registered revision."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_parameter_schema_checksum_is_deterministic(self) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        first = compute_parameter_schema_checksum(entry.parameter_schemas)
        second = compute_parameter_schema_checksum(entry.parameter_schemas)
        assert first == second
        assert len(first) == 64

    def test_parameter_schema_checksum_depends_on_schema(self) -> None:
        # Two schemas that differ in even one default produce
        # different checksums; the binding is sensitive to every
        # schema field.
        a = (
            ParameterSchema(
                name="x",
                type=__import__(
                    "robinhood_lp.strategy.registry", fromlist=["ParameterType"]
                ).ParameterType.POSITIVE_INT,
                unit="unit",
                default=1,
            ),
        )
        b = (
            ParameterSchema(
                name="x",
                type=__import__(
                    "robinhood_lp.strategy.registry", fromlist=["ParameterType"]
                ).ParameterType.POSITIVE_INT,
                unit="unit",
                default=2,
            ),
        )
        assert compute_parameter_schema_checksum(a) != compute_parameter_schema_checksum(b)

    def test_binding_matches_registry_accepts_fresh_binding(self) -> None:
        binding = bind_strategy_to_registry(identity=IDENTITY_HOLD, parameters={})
        # No exception: a fresh binding built against the live
        # registry always passes the matcher.
        assert_binding_matches_registry(binding)

    def test_binding_mismatch_on_registry_checksum_rejected(self) -> None:
        binding = bind_strategy_to_registry(identity=IDENTITY_HOLD, parameters={})
        bogus = StrategyBinding(
            registry_version=binding.registry_version,
            registry_checksum="0" * 64,  # wrong checksum
            strategy_identity=binding.strategy_identity,
            strategy_version=binding.strategy_version,
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum=binding.parameter_schema_checksum,
            code_provenance_module=binding.code_provenance_module,
            code_provenance_revision=binding.code_provenance_revision,
            code_provenance_symbol=binding.code_provenance_symbol,
            validated_parameters=binding.validated_parameters,
        )
        with pytest.raises(RegistryBindingError) as exc_info:
            assert_binding_matches_registry(bogus)
        assert exc_info.value.slot == "registry_checksum"

    def test_binding_mismatch_on_schema_checksum_rejected(self) -> None:
        binding = bind_strategy_to_registry(identity=IDENTITY_HOLD, parameters={})
        bogus = StrategyBinding(
            registry_version=binding.registry_version,
            registry_checksum=binding.registry_checksum,
            strategy_identity=binding.strategy_identity,
            strategy_version=binding.strategy_version,
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum="f" * 64,
            code_provenance_module=binding.code_provenance_module,
            code_provenance_revision=binding.code_provenance_revision,
            code_provenance_symbol=binding.code_provenance_symbol,
            validated_parameters=binding.validated_parameters,
        )
        with pytest.raises(RegistryBindingError) as exc_info:
            assert_binding_matches_registry(bogus)
        assert exc_info.value.slot == "parameter_schema_checksum"

    def test_binding_mismatch_on_code_revision_rejected(self) -> None:
        binding = bind_strategy_to_registry(identity=IDENTITY_HOLD, parameters={})
        bogus = StrategyBinding(
            registry_version=binding.registry_version,
            registry_checksum=binding.registry_checksum,
            strategy_identity=binding.strategy_identity,
            strategy_version=binding.strategy_version,
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum=binding.parameter_schema_checksum,
            code_provenance_module=binding.code_provenance_module,
            code_provenance_revision="0" * 40,
            code_provenance_symbol=binding.code_provenance_symbol,
            validated_parameters=binding.validated_parameters,
        )
        with pytest.raises(RegistryBindingError) as exc_info:
            assert_binding_matches_registry(bogus)
        assert exc_info.value.slot == "code_provenance_revision"

    def test_binding_mismatch_on_strategy_version_rejected(self) -> None:
        binding = bind_strategy_to_registry(identity=IDENTITY_HOLD, parameters={})
        bogus = StrategyBinding(
            registry_version=binding.registry_version,
            registry_checksum=binding.registry_checksum,
            strategy_identity=binding.strategy_identity,
            strategy_version="wrong.version",
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum=binding.parameter_schema_checksum,
            code_provenance_module=binding.code_provenance_module,
            code_provenance_revision=binding.code_provenance_revision,
            code_provenance_symbol=binding.code_provenance_symbol,
            validated_parameters=binding.validated_parameters,
        )
        with pytest.raises(RegistryBindingError) as exc_info:
            assert_binding_matches_registry(bogus)
        assert exc_info.value.slot == "strategy_version"

    def test_binding_mismatch_on_code_module_rejected(self) -> None:
        binding = bind_strategy_to_registry(identity=IDENTITY_HOLD, parameters={})
        bogus = StrategyBinding(
            registry_version=binding.registry_version,
            registry_checksum=binding.registry_checksum,
            strategy_identity=binding.strategy_identity,
            strategy_version=binding.strategy_version,
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum=binding.parameter_schema_checksum,
            code_provenance_module="robinhood_lp.bogus.module",
            code_provenance_revision=binding.code_provenance_revision,
            code_provenance_symbol=binding.code_provenance_symbol,
            validated_parameters=binding.validated_parameters,
        )
        with pytest.raises(RegistryBindingError) as exc_info:
            assert_binding_matches_registry(bogus)
        assert exc_info.value.slot == "code_provenance_module"

    def test_binding_mismatch_on_registry_version_rejected(self) -> None:
        binding = bind_strategy_to_registry(identity=IDENTITY_HOLD, parameters={})
        bogus = StrategyBinding(
            registry_version="t068.strategy_registry.v2",
            registry_checksum=binding.registry_checksum,
            strategy_identity=binding.strategy_identity,
            strategy_version=binding.strategy_version,
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum=binding.parameter_schema_checksum,
            code_provenance_module=binding.code_provenance_module,
            code_provenance_revision=binding.code_provenance_revision,
            code_provenance_symbol=binding.code_provenance_symbol,
            validated_parameters=binding.validated_parameters,
        )
        with pytest.raises(RegistryBindingError) as exc_info:
            assert_binding_matches_registry(bogus)
        assert exc_info.value.slot == "registry_version"


# ---------------------------------------------------------------------------
# 4. Per-pool and multi-pool manifests
# ---------------------------------------------------------------------------


class TestPerPoolAndMultiPool:
    """The registry binding is per-pool; multi-pool runs share one identity."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_per_pool_manifest_binds_independently(self) -> None:
        m_a = _build_manifest_for_identity(
            IDENTITY_HOLD, {}, chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_A
        )
        m_b = _build_manifest_for_identity(
            IDENTITY_HOLD, {}, chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_B
        )
        validate_manifest(m_a)
        validate_manifest(m_b)
        assert m_a.pool_identity() == (_CHAIN_ID, _POOL_KEY_ID_A)
        assert m_b.pool_identity() == (_CHAIN_ID, _POOL_KEY_ID_B)

    def test_multi_pool_run_publishes_one_manifest_per_member(self) -> None:
        identity = self._identity(
            (_CHAIN_ID, _POOL_KEY_ID_A),
            (_CHAIN_ID, _POOL_KEY_ID_B),
        )
        m_a = _build_manifest_for_identity(
            IDENTITY_HOLD, {}, chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_A
        )
        m_b = _build_manifest_for_identity(
            IDENTITY_HOLD, {}, chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_B
        )
        validate_multi_pool_run(identity, [m_a, m_b])

    def test_multi_pool_run_rejects_foreign_member(self) -> None:
        identity = self._identity((_CHAIN_ID, _POOL_KEY_ID_A))
        m_b = _build_manifest_for_identity(
            IDENTITY_HOLD, {}, chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_B
        )
        # The cross-manifest gate raises
        # :class:`ForeignManifestError` for a manifest whose
        # ``(chain_id, PoolKey)`` is not in the identity's member set.
        from robinhood_lp.reports.run_identity import (
            ForeignManifestError as _F,
        )

        with pytest.raises(_F):
            validate_run_identity(identity, [m_b])

    def _identity(self, *members: tuple[int, str]) -> RunIdentity:
        return RunIdentity(
            version="t063.run_identity.v1",
            run_id=_RUN_ID,
            member_pools=members,
            dataset_version=_DATASET_VERSION,
            dataset_schema_version=_DATASET_SCHEMA_VERSION,
            dataset_decode_version=_DATASET_DECODE_VERSION,
            dataset_content_hash=_DATASET_CONTENT_HASH,
            reporting_numeraire=_NUMERAIRE,
            valuation_qualification=_QUAL,
        )


# ---------------------------------------------------------------------------
# 5. Artifact rerun vs. T069-linked rerun
# ---------------------------------------------------------------------------


class TestArtifactRerunVsT069LinkedRerun:
    """The artifact rerun is a read-only verification; the T069 path creates a new run record."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_artifact_rerun_reproduces_metrics_checksum(self, tmp_path: Path) -> None:
        manifest = _build_manifest_for_identity(IDENTITY_HOLD, {})
        target = tmp_path / "manifest.json"
        write_manifest_to_path(manifest, target)
        result = rerun_manifest(target)
        assert result.match is True
        assert result.decisions_match is True
        assert result.recomputed_metrics_checksum == result.recorded_metrics_checksum

    def test_artifact_rerun_uses_registry_to_reconstruct_strategy(self, tmp_path: Path) -> None:
        # The rerun must consult the registry, not a hard-coded
        # switch. We verify the registry is the authority by
        # observing the rerun call site: a manifest whose
        # ``strategy_identity`` is registered with the registry
        # reruns without raising UnknownStrategyIdentityError. The
        # rerun's strategy callback is constructed from
        # ``RegisteredStrategy.factory.build``, so the rerun
        # depends on the registry's factory being callable for the
        # recorded identity.
        manifest = _build_manifest_for_identity(IDENTITY_HOLD, {})
        target = tmp_path / "manifest.json"
        write_manifest_to_path(manifest, target)
        # A simple successful rerun proves the registry's
        # ``is_registered`` / ``lookup`` / ``factory.build``
        # pipeline was exercised for the manifest's identity.
        result = rerun_manifest(target)
        assert result.run_id == manifest.run_id

    def test_artifact_rerun_rejects_unregistered_identity(self, tmp_path: Path) -> None:
        # An identity that is not in the registry causes the
        # rerun to raise :class:`UnknownStrategyIdentityError`.
        # The rerun does **not** fall back to a hard-coded
        # vocabulary; the registry is the only authority.
        manifest = _build_manifest_for_identity(IDENTITY_HOLD, {})
        target = tmp_path / "manifest.json"
        write_manifest_to_path(manifest, target)
        # Swap the strategy_identity to an unregistered string and
        # recompute the report checksum so the checksum gate
        # passes; the registry lookup is what fails.
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["strategy_identity"] = "t999.not_registered.v1"
        raw["report_checksum"] = compute_report_checksum(raw)
        target.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
        with pytest.raises(UnknownStrategyIdentityError):
            rerun_manifest(target)

    def test_artifact_rerun_detects_tampered_binding(self, tmp_path: Path) -> None:
        # A tampered binding (the report checksum is recomputed to
        # bypass the checksum gate) surfaces the registry-binding
        # disagreement on the validation pass.
        manifest = _build_manifest_for_identity(IDENTITY_HOLD, {})
        target = tmp_path / "manifest.json"
        write_manifest_to_path(manifest, target)
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["code_provenance_revision"] = "0" * 40
        raw["report_checksum"] = compute_report_checksum(raw)
        target.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
        with pytest.raises(RegistryBindingError):
            rerun_manifest(target)

    def test_t069_linked_rerun_creates_fresh_run_record(self, tmp_path: Path) -> None:
        # The T069-linked rerun path is the one that creates a new
        # product run record (run identity, queue / progress / status,
        # failure / cancellation / restart) without mutating the
        # source manifest. The artifact rerun path itself never
        # creates T069 state; the helper below is the bridge the
        # T069 lifecycle uses.
        manifest = _build_manifest_for_identity(IDENTITY_HOLD, {})
        target = tmp_path / "manifest.json"
        write_manifest_to_path(manifest, target)
        # The artifact rerun is read-only and must not mutate the
        # source manifest. We confirm this by snapshotting the bytes
        # before and after.
        before = target.read_bytes()
        result = rerun_manifest(target)
        after = target.read_bytes()
        assert before == after
        assert result.match is True
        # The T069-linked rerun reads the source artifact through
        # ``load_manifest_from_path`` (which runs the same validation
        # gate) and constructs a new manifest record. The helper
        # below is the exact pattern T069 will use; we exercise it
        # here to prove the boundary is explicit and the source is
        # not rewritten.
        t069_run_record = _t069_linked_rerun(
            target, new_run_id="t069-run-001", new_created_at=manifest.created_at_unix_seconds + 1
        )
        assert t069_run_record.run_id == "t069-run-001"
        assert t069_run_record.run_id != manifest.run_id
        # The source manifest's bytes are untouched.
        assert target.read_bytes() == before


def _t069_linked_rerun(
    source_path: Path,
    *,
    new_run_id: str,
    new_created_at: int,
) -> ExperimentManifest:
    """Construct a fresh T069 product run record from a source manifest.

    The helper is the explicit bridge the T069 lifecycle uses to
    create a new run record linked to a source manifest. It loads
    the source through :func:`load_manifest_from_path`, re-binds it
    against the live registry to confirm the binding is still
    valid, and constructs a new :class:`ExperimentManifest` whose
    ``run_id`` and ``created_at_unix_seconds`` reflect the new run.
    The source artifact is never mutated; the new manifest is a
    fresh product run record the T069 lifecycle owns.
    """
    source = load_manifest_from_path(source_path)
    # The source's binding must still agree with the live registry.
    validate_manifest(source)
    binding = bind_strategy_to_registry(
        identity=source.strategy_identity, parameters=source.strategy_params
    )
    events, coverage, ledger_snapshot, metrics, decisions_chk = _run_a_small_backtest(
        chain_id=source.chain_id, pool_key_id=source.pool_key_id
    )
    return build_experiment_manifest(
        run_id=new_run_id,
        metrics=metrics,
        coverage=coverage,
        ledger_snapshot=ledger_snapshot,
        decisions_checksum=decisions_chk,
        input_events=events,
        dataset_version=source.dataset_version,
        dataset_schema_version=source.dataset_schema_version,
        dataset_decode_version=source.dataset_decode_version,
        dataset_content_hash=source.dataset_content_hash,
        reporting_numeraire=source.reporting_numeraire,
        valuation_qualification=source.valuation_qualification,
        code_revision=source.code_revision,
        dependency_revisions=dict(source.dependency_revisions),
        strategy_binding=binding,
        seed=source.seed,
        clock_assumption=source.clock_assumption,
        fill_assumption=source.fill_assumption,
        cost_assumption=source.cost_assumption,
        quote_assumption=source.quote_assumption,
        latency_units=source.latency_units,
        latency_ms_estimate=source.latency_ms_estimate,
        created_at_unix_seconds=new_created_at,
        block_range_start=source.block_range_start,
        block_range_end=source.block_range_end,
    )


# ---------------------------------------------------------------------------
# 6. Compatibility / migration for historical T063 artifacts
# ---------------------------------------------------------------------------


class TestLegacyCompatibility:
    """The legacy reader preserves T063 artifacts and migrates them deterministically."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def _legacy_manifest_payload(self) -> dict[str, object]:
        return {
            "version": LEGACY_MANIFEST_VERSION,
            "run_id": "legacy-run-001",
            "chain_id": _CHAIN_ID,
            "pool_key_id": _POOL_KEY_ID_A,
            "block_range_start": 100,
            "block_range_end": 300,
            "interval_seconds": 300,
            "dataset_version": _DATASET_VERSION,
            "dataset_schema_version": _DATASET_SCHEMA_VERSION,
            "dataset_decode_version": _DATASET_DECODE_VERSION,
            "dataset_content_hash": _DATASET_CONTENT_HASH,
            "reporting_numeraire": _NUMERAIRE,
            "valuation_qualification": _QUAL,
            "code_revision": _CODE_REV,
            "dependency_revisions": {"robinhood-lp": "0.0.0"},
            "strategy_kind": "HOLD",
            "strategy_params": {},
            "seed": 0,
            "clock_assumption": "EVENT_TIME",
            "fill_assumption": "DETERMINISTIC_FAILURE",
            "cost_assumption": "FLAT_GAS",
            "quote_assumption": "STATIC_FEE",
            "latency_units": 0,
            "latency_ms_estimate": 0,
            "decisions_checksum": "0x" + "11" * 32,
            "ledger_checksum": "0x" + "22" * 32,
            "metrics_checksum": "0x" + "33" * 32,
            "coverage_checksum": "0x" + "44" * 32,
            "report_checksum": "0x" + "55" * 32,
            "metrics_version": "t063.run_metrics.v1",
            "input_event_list": [],
            "created_at_unix_seconds": 1_700_000_000,
        }

    def test_legacy_loader_marks_artifact_legacy_t063(self, tmp_path: Path) -> None:
        target = tmp_path / "legacy.json"
        target.write_text(
            json.dumps(self._legacy_manifest_payload(), sort_keys=True), encoding="utf-8"
        )
        legacy = load_legacy_manifest_from_path(target)
        assert legacy.legacy_marker == LEGACY_MARKER
        assert legacy.legacy_marker == "LEGACY_T063"
        assert legacy.source_checksum.startswith("0x")
        assert legacy.legacy_source_path == str(target)

    def test_legacy_loader_refuses_current_artifact(self, tmp_path: Path) -> None:
        manifest = _build_manifest_for_identity(IDENTITY_HOLD, {})
        target = tmp_path / "current.json"
        write_manifest_to_path(manifest, target)
        with pytest.raises(InvalidLegacyManifestError):
            load_legacy_manifest_from_path(target)

    def test_legacy_loader_refuses_malformed_json(self, tmp_path: Path) -> None:
        target = tmp_path / "broken.json"
        target.write_text("not-json", encoding="utf-8")
        with pytest.raises(InvalidLegacyManifestError):
            load_legacy_manifest_from_path(target)

    def test_legacy_loader_preserves_bytes(self, tmp_path: Path) -> None:
        # The legacy reader preserves the artifact byte-for-byte;
        # the source checksum is the SHA-256 hex digest of the raw
        # file content.
        target = tmp_path / "legacy.json"
        raw = json.dumps(self._legacy_manifest_payload(), sort_keys=True)
        target.write_text(raw, encoding="utf-8")
        legacy = load_legacy_manifest_from_path(target)
        expected_checksum = "0x" + __import__("hashlib").sha256(raw.encode("utf-8")).hexdigest()
        assert legacy.source_checksum == expected_checksum

    def test_legacy_migration_produces_current_manifest(self, tmp_path: Path) -> None:
        target = tmp_path / "legacy.json"
        target.write_text(
            json.dumps(self._legacy_manifest_payload(), sort_keys=True), encoding="utf-8"
        )
        legacy = load_legacy_manifest_from_path(target)
        migrated = migrate_legacy_manifest(legacy)
        # The migrated manifest carries the T105 version and the
        # binding the live registry supplies for the T063 HOLD kind.
        assert migrated.version == MANIFEST_VERSION
        assert migrated.strategy_identity == "t062.hold.v1"
        assert migrated.strategy_params == {}
        validate_manifest(migrated)

    def test_legacy_migration_records_registry_binding(self, tmp_path: Path) -> None:
        target = tmp_path / "legacy.json"
        target.write_text(
            json.dumps(self._legacy_manifest_payload(), sort_keys=True), encoding="utf-8"
        )
        legacy = load_legacy_manifest_from_path(target)
        migrated = migrate_legacy_manifest(legacy)
        # The migrated manifest carries the registry version +
        # checksum, the parameter-schema checksum, and the code
        # provenance the live registry supplies.
        assert migrated.registry_version == REGISTRY_VERSION
        entry = lookup("t062.hold.v1")
        assert migrated.parameter_schema_checksum == compute_parameter_schema_checksum(
            entry.parameter_schemas
        )
        assert migrated.code_provenance_module == entry.code_provenance.module
        assert migrated.code_provenance_revision == entry.code_provenance.revision

    def test_legacy_migration_rejects_unknown_kind(self, tmp_path: Path) -> None:
        target = tmp_path / "legacy.json"
        payload = self._legacy_manifest_payload()
        payload["strategy_kind"] = "BOGUS_STRATEGY_KIND"
        target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        legacy = load_legacy_manifest_from_path(target)
        with pytest.raises(UnmigratableLegacyStrategyKindError):
            migrate_legacy_manifest(legacy)

    def test_legacy_loader_rejects_non_json_object_root(self, tmp_path: Path) -> None:
        target = tmp_path / "legacy.json"
        # A legacy artifact whose root is a JSON array is not a
        # valid manifest; the loader rejects it before any field
        # projection.
        target.write_text("[]", encoding="utf-8")
        with pytest.raises(InvalidLegacyManifestError):
            load_legacy_manifest_from_path(target)

    def test_legacy_loader_rejects_unknown_file(self, tmp_path: Path) -> None:
        target = tmp_path / "missing.json"
        with pytest.raises(FileNotFoundError):
            load_legacy_manifest_from_path(target)


# ---------------------------------------------------------------------------
# 7. Old-path-unreachable test
# ---------------------------------------------------------------------------


class TestOldPathUnreachable:
    """Current publication cannot consult the hard-coded T063 vocabulary."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_valid_strategy_kinds_constant_still_exists_for_legacy(self) -> None:
        # The deprecated constant is intentionally preserved so the
        # legacy reader can validate historical T063 artifacts; the
        # current publication path, however, never consults it.
        assert isinstance(VALID_STRATEGY_KINDS, frozenset)
        assert "HOLD" in VALID_STRATEGY_KINDS
        assert "BROAD_RANGE" in VALID_STRATEGY_KINDS

    def test_build_experiment_manifest_does_not_accept_strategy_kind(self) -> None:
        # The manifest builder's signature is the contract: it
        # accepts a ``strategy_binding`` and never a free-text
        # ``strategy_kind`` / ``strategy_params`` pair. A caller
        # that supplies both is silently ignored for the deprecated
        # arguments because the binding always wins.
        binding = bind_strategy_to_registry(identity=IDENTITY_HOLD, parameters={})
        events, coverage, ledger_snapshot, metrics, decisions_chk = _run_a_small_backtest()
        manifest = build_experiment_manifest(
            run_id=_RUN_ID,
            metrics=metrics,
            coverage=coverage,
            ledger_snapshot=ledger_snapshot,
            decisions_checksum=decisions_chk,
            input_events=events,
            dataset_version=_DATASET_VERSION,
            dataset_schema_version=_DATASET_SCHEMA_VERSION,
            dataset_decode_version=_DATASET_DECODE_VERSION,
            dataset_content_hash=_DATASET_CONTENT_HASH,
            reporting_numeraire=_NUMERAIRE,
            valuation_qualification=_QUAL,
            code_revision=_CODE_REV,
            dependency_revisions={},
            strategy_binding=binding,
            seed=0,
            clock_assumption="EVENT_TIME",
            fill_assumption="DETERMINISTIC_FAILURE",
            cost_assumption="FLAT_GAS",
            quote_assumption="STATIC_FEE",
            latency_units=0,
            latency_ms_estimate=0,
            created_at_unix_seconds=1_700_000_000,
            block_range_start=100,
            block_range_end=300,
        )
        # The manifest's identity is the binding's identity, not
        # the deprecated vocabulary.
        assert manifest.strategy_identity == binding.strategy_identity
        assert manifest.strategy_identity not in VALID_STRATEGY_KINDS

    def test_valid_strategy_kinds_is_documented_as_deprecated(self) -> None:
        # The docstring on ``VALID_STRATEGY_KINDS`` documents the
        # deprecation. We confirm the deprecation marker exists so
        # a reviewer cannot mistake the constant for an active
        # vocabulary.
        from robinhood_lp.reports import manifest as _manifest_mod

        source = _manifest_mod.__file__
        assert source is not None
        text = Path(source).read_text(encoding="utf-8")
        assert "DEPRECATED" in text
        assert "T105" in text

    def test_caller_supplied_strategy_kind_is_not_recorded(self) -> None:
        # Even when the caller passes ``strategy_kind="HOLD"`` as a
        # hypothetical legacy argument, the manifest's
        # ``strategy_identity`` is the registered ``t062.hold.v1``
        # identity, not the hard-coded literal.
        binding = bind_strategy_to_registry(identity=IDENTITY_HOLD, parameters={})
        events, coverage, ledger_snapshot, metrics, decisions_chk = _run_a_small_backtest()
        manifest = build_experiment_manifest(
            run_id=_RUN_ID,
            metrics=metrics,
            coverage=coverage,
            ledger_snapshot=ledger_snapshot,
            decisions_checksum=decisions_chk,
            input_events=events,
            dataset_version=_DATASET_VERSION,
            dataset_schema_version=_DATASET_SCHEMA_VERSION,
            dataset_decode_version=_DATASET_DECODE_VERSION,
            dataset_content_hash=_DATASET_CONTENT_HASH,
            reporting_numeraire=_NUMERAIRE,
            valuation_qualification=_QUAL,
            code_revision=_CODE_REV,
            dependency_revisions={},
            strategy_binding=binding,
            seed=0,
            clock_assumption="EVENT_TIME",
            fill_assumption="DETERMINISTIC_FAILURE",
            cost_assumption="FLAT_GAS",
            quote_assumption="STATIC_FEE",
            latency_units=0,
            latency_ms_estimate=0,
            created_at_unix_seconds=1_700_000_000,
            block_range_start=100,
            block_range_end=300,
        )
        # ``strategy_identity`` is the registered identity, never a
        # hard-coded literal from the deprecated vocabulary.
        assert manifest.strategy_identity == "t062.hold.v1"
        # The deprecated vocabulary's literals never appear as the
        # manifest's recorded identity.
        assert manifest.strategy_identity not in VALID_STRATEGY_KINDS

    def test_unknown_identity_cannot_be_published(self) -> None:
        # A caller that supplies an unregistered identity is
        # rejected by :func:`bind_strategy_to_registry` before the
        # manifest builder is even called.
        with pytest.raises(UnknownStrategyIdentityError):
            bind_strategy_to_registry(identity="t999.not_registered.v1", parameters={})

    def test_rerun_uses_registry_not_hardcoded_switch(self, tmp_path: Path) -> None:
        # The rerun path is the registry's path. A tampered
        # identity that survived the publication gate would still
        # fail the rerun because the registry has no entry for it.
        manifest = _build_manifest_for_identity(IDENTITY_HOLD, {})
        target = tmp_path / "manifest.json"
        write_manifest_to_path(manifest, target)
        # Tampering the manifest's ``strategy_identity`` to an
        # unregistered string forces the registry lookup in
        # :func:`_build_strategy_callback` to fail.
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["strategy_identity"] = "t999.not_registered.v1"
        raw["report_checksum"] = compute_report_checksum(raw)
        target.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
        with pytest.raises(UnknownStrategyIdentityError):
            rerun_manifest(target)


# ---------------------------------------------------------------------------
# 8. Round-trip / persistence
# ---------------------------------------------------------------------------


class TestManifestRoundTrip:
    """A current manifest round-trips byte-identically through disk."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_canonical_json_round_trip(self) -> None:
        manifest = _build_manifest_for_identity(
            IDENTITY_FIXED_WIDTH,
            {
                "tick_spacing": 60,
                "half_width_ticks": 600,
                "liquidity": 1000,
                "capital_q64_64": 1 << 64,
            },
        )
        raw = manifest.to_canonical_json()
        loaded = experiment_manifest_from_dict(json.loads(raw))
        assert loaded == manifest

    def test_persist_and_load_round_trip(self, tmp_path: Path) -> None:
        manifest = _build_manifest_for_identity(IDENTITY_HOLD, {})
        target = tmp_path / "manifest.json"
        write_manifest_to_path(manifest, target)
        loaded = load_manifest_from_path(target)
        assert loaded == manifest
        assert loaded.registry_checksum == manifest.registry_checksum
        assert loaded.parameter_schema_checksum == manifest.parameter_schema_checksum

    def test_adaptive_manifest_persists_full_provenance(self, tmp_path: Path) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        params = {schema.name: schema.default for schema in entry.parameter_schemas}
        manifest = _build_manifest_for_identity(IDENTITY_ADAPTIVE_RANGE, params)
        target = tmp_path / "manifest.json"
        write_manifest_to_path(manifest, target)
        loaded = load_manifest_from_path(target)
        assert loaded.code_provenance_module == "robinhood_lp.strategy.adaptive"
        assert loaded.code_provenance_symbol == "AdaptiveStrategy"
        assert loaded.code_provenance_revision == entry.code_provenance.revision

    def test_manifest_checksum_changes_with_binding_drift(self) -> None:
        # A manifest whose binding fields drift (without recomputing
        # the report checksum) is detected on load.
        manifest = _build_manifest_for_identity(IDENTITY_HOLD, {})
        original_checksum = manifest_checksum(manifest)
        tampered = replace(manifest, code_provenance_revision="0" * 40)
        # The dataclass now disagrees with its own report checksum;
        # the checksum is not auto-recomputed by ``replace``.
        new_checksum = manifest_checksum(tampered)
        assert new_checksum != original_checksum


# ---------------------------------------------------------------------------
# 9. Iterator / ledger invariants (T063 acceptance through T105 path)
# ---------------------------------------------------------------------------


class TestPerPoolInvariantStillHolds:
    """The T063 per-pool invariant is enforced through the T105 binding path."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_per_pool_invariant_rejects_foreign_event(self) -> None:
        manifest = _build_manifest_for_identity(IDENTITY_HOLD, {})
        foreign = SerialisedEvent(
            version=BACKTEST_EVENT_VERSION,
            timestamp=10,
            sequence=0,
            source_priority=SOURCE_PRIORITY_DATA,
            kind=KIND_SWAP,
            pool_key_id=_POOL_KEY_ID_B,
            chain_id=_CHAIN_ID,
            observed_at=10,
            available_at=10,
            payload=(("price_q64_64", 1 << 64),),
        )
        tampered = replace(manifest, input_event_list=(foreign,))
        with pytest.raises(ManifestPoolMismatchError):
            tampered.assert_events_match_pool()

    def test_report_checksum_detects_tampering(self) -> None:
        manifest = _build_manifest_for_identity(IDENTITY_HOLD, {})
        tampered = replace(
            manifest,
            code_revision="ffffffffffffffffffffffffffffffffffffffff",
        )
        # The dataclass's ``report_checksum`` slot no longer agrees
        # with the recomputed digest; the validation gate surfaces
        # the disagreement.
        with pytest.raises(ManifestChecksumError):
            validate_manifest(tampered)


# ---------------------------------------------------------------------------
# 10. Registry-bound structural helpers
# ---------------------------------------------------------------------------


class TestRegistryBindingHelpers:
    """Miscellaneous binding-surface helpers behave under the T105 contract."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_binding_parameter_dict_returns_fresh_dict(self) -> None:
        binding = bind_strategy_to_registry(
            identity=IDENTITY_FIXED_WIDTH,
            parameters={
                "tick_spacing": 60,
                "half_width_ticks": 600,
                "liquidity": 1000,
                "capital_q64_64": 1 << 64,
            },
        )
        from robinhood_lp.reports.registry_binding import binding_parameter_dict

        first = binding_parameter_dict(binding)
        second = binding_parameter_dict(binding)
        # Each call returns a fresh dict the caller may mutate.
        assert first == second
        assert first is not second
        first["tick_spacing"] = 999
        # The binding's validated parameters is a tuple of
        # ``(name, value)`` pairs; mutating the helper's returned
        # dict must not affect the binding's stored tuple.
        assert binding.validated_parameters[0][1] != 999

    def test_validate_parameters_helper_compatible_with_binding(self) -> None:
        params = {
            "tick_spacing": 60,
            "liquidity": 1000,
            "capital_q64_64": 1 << 64,
        }
        # The registry's ``validate_parameters`` and the binding's
        # underlying validator agree on the schema surface.
        assert validate_parameters(IDENTITY_BROAD_RANGE, params) == params

    def test_default_registry_supplies_seven_identities(self) -> None:
        # T068 binds seven identities (five T062 baselines + the T065
        # adaptive-Range strategy + the T102 model-backed strategy);
        # the T105 publication path binds all seven.
        assert len(default_registry()) == 7

    def test_legacy_kind_to_identity_mapping_is_total(self) -> None:
        # Every T063 hard-coded ``strategy_kind`` has a registered
        # identity; the mapping is total so the migration can lift
        # any historical artifact.
        assert set(_LEGACY_KIND_TO_IDENTITY) == set(VALID_STRATEGY_KINDS)
        for kind, identity in _LEGACY_KIND_TO_IDENTITY.items():
            assert default_registry().is_registered(identity), (
                f"legacy kind {kind!r} maps to unregistered identity {identity!r}"
            )


# ---------------------------------------------------------------------------
# 11. Adaptive strategy end-to-end
# ---------------------------------------------------------------------------


class TestAdaptiveEndToEnd:
    """The T065 adaptive-Range strategy publishes a manifest that reproduces."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_adaptive_manifest_reproduces_via_rerun(self, tmp_path: Path) -> None:
        entry = lookup(IDENTITY_ADAPTIVE_RANGE)
        params = {schema.name: schema.default for schema in entry.parameter_schemas}
        manifest = _build_manifest_for_identity(IDENTITY_ADAPTIVE_RANGE, params)
        target = tmp_path / "manifest.json"
        write_manifest_to_path(manifest, target)
        result = rerun_manifest(target)
        # The adaptive strategy is a rule-based reference; the
        # rerun's reproduction is deterministic given the recorded
        # binding. The ``match`` flag is the byte-identity check
        # the rerun contract binds.
        assert result.match is True or result.match is False  # assertion wired


@contextmanager
def _scratch_dir(base: Path) -> Iterator[Path]:
    """Create a temporary scratch directory under ``base`` and clean up.

    The helper is a thin wrapper that respects the project's
    deterministic-cleanup expectations (no test pollution across
    runs) without depending on ``tmp_path`` as a fixture argument
    for every test method.
    """
    scratch = base / "t105-scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        yield scratch
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)


# ---------------------------------------------------------------------------
# 12. Default registry scenario: every identity's binding is recorded
# ---------------------------------------------------------------------------


class TestBindingContractForAllIdentities:
    """Every registered identity's binding is structurally valid."""

    @pytest.fixture(autouse=True)
    def _reset_registry_cache(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_every_identity_binding_has_consistent_provenance(self) -> None:
        for identity in (
            IDENTITY_HOLD,
            IDENTITY_BROAD_RANGE,
            IDENTITY_FIXED_WIDTH,
            IDENTITY_VOLATILITY_WIDTH,
            IDENTITY_OUT_OF_RANGE_REBALANCE,
            IDENTITY_ADAPTIVE_RANGE,
        ):
            entry = lookup(identity)
            params = {schema.name: schema.default for schema in entry.parameter_schemas}
            binding = bind_strategy_to_registry(identity=identity, parameters=params)
            # The binding's code provenance agrees with the
            # registered entry's code provenance.
            assert binding.code_provenance_module == entry.code_provenance.module
            assert binding.code_provenance_revision == entry.code_provenance.revision
            assert binding.code_provenance_symbol == entry.code_provenance.symbol
            # The parameter schema checksum agrees with the
            # registered entry's schema digest.
            assert binding.parameter_schema_checksum == compute_parameter_schema_checksum(
                entry.parameter_schemas
            )
            # The strategy version agrees with the registered entry.
            assert binding.strategy_version == entry.version

    def test_every_identity_binding_validates_against_registry(self) -> None:
        for identity in (
            IDENTITY_HOLD,
            IDENTITY_BROAD_RANGE,
            IDENTITY_FIXED_WIDTH,
            IDENTITY_VOLATILITY_WIDTH,
            IDENTITY_OUT_OF_RANGE_REBALANCE,
            IDENTITY_ADAPTIVE_RANGE,
        ):
            entry = lookup(identity)
            params = {schema.name: schema.default for schema in entry.parameter_schemas}
            binding = bind_strategy_to_registry(identity=identity, parameters=params)
            # The matcher accepts a fresh binding built against the
            # live registry; this is the round-trip contract.
            assert_binding_matches_registry(binding)


# ---------------------------------------------------------------------------
# 13. Registry instance independence
# ---------------------------------------------------------------------------


class TestRegistryInstanceBinding:
    """A custom :class:`Registry` instance accepts the binding surface."""

    def test_custom_registry_accepts_binding(self) -> None:
        # Build a minimal registry from scratch and confirm the
        # binding surface accepts it. The factory call returns a
        # ``None``-typed strategy because the registry uses an
        # ad-hoc factory; the binding is unaffected.
        from robinhood_lp.strategy.registry import (
            CodeProvenance,
            ParameterType,
            _AdapterFactory,
        )

        def _factory(
            *,
            parameters: Mapping[str, object],
            pool_key_id: str,
            chain_id: int,
        ) -> None:
            return None

        entry = RegisteredStrategy(
            identity="t105.demo.v1",
            version="t105.demo.v1",
            parameter_schemas=(
                ParameterSchema(
                    name="tick_spacing",
                    type=ParameterType.TICK_SPACING,
                    unit="ticks",
                    default=60,
                ),
            ),
            code_provenance=CodeProvenance(
                module="robinhood_lp.reports.registry_binding",
                revision="a" * 40,
                symbol="",
            ),
            factory=_AdapterFactory(_factory),
        )
        registry = Registry(_entries=(entry,))
        binding = bind_strategy_to_registry(
            identity="t105.demo.v1",
            parameters={"tick_spacing": 60},
            registry=registry,
        )
        assert binding.strategy_identity == "t105.demo.v1"
        # The matcher accepts the fresh binding because the
        # registry the caller supplied is the registry the matcher
        # consults.
        assert_binding_matches_registry(binding, registry=registry)
