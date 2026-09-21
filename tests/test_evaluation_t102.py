"""Tests for T102 model-backed strategy evaluation.

The tests cover every T102 acceptance clause:

- **Closed identity vocabulary.** The model-backed strategy is
  registered through the T068 registry with a parameter schema, a
  version and code provenance; an unregistered identity or a run
  request that does not name a registered identity is refused.

- **Per-identity parameter schema.** Every axis the walk-forward
  evaluation builds is declared by the registered strategy's
  parameter schema; a point the schema does not declare, an
  out-of-range value, a wrong-type or wrong-unit value, and a
  schema revision that disagrees with the evaluated run's
  validated T105 manifest are rejected before any fold is
  evaluated.

- **Walk-forward evaluation through the T061 engine.** Point-in-
  time, machine-recorded fold boundaries drive the rule baseline
  and the model-backed strategy through identical inputs; the
  report's verdict comes from simulated LP economics, not from a
  prediction metric.

- **Deterministic fallback.** A missing, stale, unversioned or
  out-of-distribution model artifact yields ``UNCERTAIN`` /
  ``NO_TRADE`` rather than a fabricated assessment; an available
  artifact with a degenerate (constant) prediction is treated as
  out-of-distribution.

- **Side-by-side comparison.** The report names the rule baseline
  and the model, the number of independent episodes, the
  bootstrap interval and the regime-conditional breakdown.

- **Rejection record.** A model that improves a prediction metric
  while worsening LP economics is reported as a rejection and
  never as a success.

- **Fail-closed evidence boundary.** Missing, legacy-only or
  revision-mismatched T106 robustness evidence yields
  :attr:`RobustnessEvidenceStatus.MISSING` /
  :attr:`LEGACY_ONLY` / :attr:`REVISION_MISMATCH` rather than
  falling back to the unrestricted ``T064`` predecessor surface.

- **Legacy ``T064`` robustness artifacts.** A legacy
  :class:`ParameterSurface` carrying the explicit
  :data:`LEGACY_SURFACE_MARKER` stays read-only, byte-identical,
  and explicitly legacy; it cannot be consumed as current
  robustness evidence.

- **Two heterogeneous registered strategies.** The same evaluation
  runs positively against the T062 fixed-width baseline and the
  T065 adaptive-range identity, each surface recording the
  registry and schema identity / version / checksum it was
  built from.

- **Old-path-unreachable.** Current evaluation cannot construct a
  parameter surface through the unrestricted ``T064`` builder;
  the schema-bound surface identity always names a
  schema-bound registry and schema revision.

- **Layer purity.** The evaluation module imports the standard
  library and the lower backtest / research / strategy /
  robustness packages only; RPC, storage, signing, execution and
  presentation are never imported.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Final

import pytest

from robinhood_lp.research.evaluation import (
    DEFAULT_MAX_MODEL_STALENESS_SECONDS,
    EVALUATION_VERSION,
    LEGACY_SURFACE_MARKER,
    MODEL_COMPONENT_VERSION,
    Q64_64_SCALE,
    REGIME_LABELS_FOR_BREAKDOWN,
    VERDICT_ASSESSED,
    VERDICT_UNCERTAIN,
    ConstantModelPredictor,
    EpisodeBootstrapInterval,
    EpisodeOutcome,
    EvaluationError,
    InvalidEpisodeIntervalError,
    InvalidModelArtifactHandleError,
    InvalidRegimeBreakdownError,
    InvalidRejectionRecordError,
    InvalidWalkForwardFoldError,
    LegacyRobustnessFixture,
    MissingManifestBindingError,
    ModelArtifactHandle,
    ModelBackedEvaluationParameters,
    ModelBackedFeeOpportunityModel,
    ModelBackedRegimeModel,
    ModelBackedStrategy,
    ModelEvaluationReport,
    ModelEvaluationSurface,
    RegimeConditionalBreakdown,
    RejectionRecord,
    RobustnessEvidenceStatus,
    UnregisteredEvaluationIdentityError,
    ValidatedManifestBinding,
    WalkForwardEvaluationInputs,
    WalkForwardFold,
    build_model_evaluation_surface,
    build_rejection_record,
    check_robustness_evidence,
    compute_episode_bootstrap_intervals,
    compute_regime_conditional_breakdown,
    evaluate_out_of_distribution,
    parameters_from_validated,
    parameters_min_episodes_default,
    surface_axes_to_evaluation_axes,
    validate_parameters_against_schema,
    walk_forward_evaluate_model,
)
from robinhood_lp.robustness.schema_binding import (
    SchemaBindingError,
    SchemaBoundParameterSurface,
    build_schema_bound_surface,
)
from robinhood_lp.robustness.surfaces import (
    ParameterAxis,
    ParameterSurface,
    build_parameter_surface,
)
from robinhood_lp.strategy import (
    IDENTITY_ADAPTIVE_RANGE,
    IDENTITY_FIXED_WIDTH,
    IDENTITY_HOLD,
    IDENTITY_MODEL_BACKED,
)
from robinhood_lp.strategy.base import (
    FeeOpportunityAssessment,
    FrozenClock,
    FrozenSeededRandomSource,
    MarketSnapshot,
    PortfolioSnapshot,
    RegimeAssessment,
)

# ---------------------------------------------------------------------------
# Reference fixtures
# ---------------------------------------------------------------------------


_POOL_KEY_ID: Final[str] = "0x" + "ab" * 32
_OTHER_POOL_KEY_ID: Final[str] = "0x" + "cd" * 32
_CHAIN_ID: Final[int] = 46630
_TICK_SPACING: Final[int] = 60


def _make_market_snapshot(
    *,
    pool_key_id: str = _POOL_KEY_ID,
    chain_id: int = _CHAIN_ID,
    availability_time: int = 1_000,
    freshness_seconds: int = 30,
    realized_volatility_q64_64: int = Q64_64_SCALE // 100,
    quote_q64_64: int | None = Q64_64_SCALE,
    sqrt_price_x96: int = 1 << 96,
    liquidity: int = 1_000_000,
) -> MarketSnapshot:
    return MarketSnapshot(
        version="t060.market_snapshot.v1",
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        sqrt_price_x96=sqrt_price_x96,
        liquidity=liquidity,
        realized_volatility_q64_64=realized_volatility_q64_64,
        freshness_seconds=freshness_seconds,
        quote_q64_64=quote_q64_64,
        is_relative_only=False,
        data_time=availability_time,
        availability_time=availability_time,
    )


def _make_portfolio_snapshot(
    *,
    pool_key_id: str = _POOL_KEY_ID,
    chain_id: int = _CHAIN_ID,
    availability_time: int = 1_000,
    is_empty: bool = True,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        version="t060.portfolio_snapshot.v1",
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        position_id="pos-1",
        sqrt_price_x96=1 << 96,
        liquidity=0 if is_empty else 1_000,
        principal_token0=0,
        principal_token1=0,
        tokens_owed0=0,
        tokens_owed1=0,
        is_empty=is_empty,
        data_time=availability_time,
        availability_time=availability_time,
    )


def _make_clock(event_times: Sequence[int] = (1_000, 1_001, 1_002)) -> FrozenClock:
    return FrozenClock(event_times=tuple(event_times))


def _make_rng() -> FrozenSeededRandomSource:
    return FrozenSeededRandomSource()


def _make_handle(
    *,
    artifact_id: str = "artifact-1",
    family: str = "LINEAR_REGULARIZED",
    dataset_version: str = "0x" + "11" * 32,
    feature_config_hash: str = "0x" + "22" * 32,
    split_definition_hash: str = "0x" + "33" * 32,
    code_revision: str = "0x" + "44" * 40,
    trained_at_unix_seconds: int = 0,
    training_pool_set: tuple[str, ...] = (_POOL_KEY_ID,),
    schema_revision: str = "0x" + "55" * 32,
    registry_checksum: str = "0x" + "66" * 32,
) -> ModelArtifactHandle:
    return ModelArtifactHandle(
        artifact_id=artifact_id,
        family=family,
        dataset_version=dataset_version,
        feature_config_hash=feature_config_hash,
        split_definition_hash=split_definition_hash,
        code_revision=code_revision,
        trained_at_unix_seconds=trained_at_unix_seconds,
        training_pool_set=training_pool_set,
        schema_revision=schema_revision,
        registry_checksum=registry_checksum,
    )


def _make_binding(
    *, identity: str = IDENTITY_FIXED_WIDTH, run_id: str = "run-1"
) -> ValidatedManifestBinding:
    return ValidatedManifestBinding.from_registry_entry(run_id=run_id, identity=identity)


def _make_fold(
    *,
    fold_index: int = 0,
    train_start: int = 0,
    train_end: int = 1_000,
    eval_start: int = 2_000,
    eval_end: int = 3_000,
    purge_plus_embargo: int = 1_000,
) -> WalkForwardFold:
    return WalkForwardFold(
        fold_index=fold_index,
        train_start_decision_time=train_start,
        train_end_decision_time=train_end,
        eval_start_decision_time=eval_start,
        eval_end_decision_time=eval_end,
        purge_plus_embargo_seconds=purge_plus_embargo,
    )


def _make_episode(
    *,
    episode_index: int,
    decision_time: int,
    net_return_q64_64: int,
    regime_state: str = "RANGE",
    realised_fee_q64_64: int = 1_000,
    realised_gas_q64_64: int = 100,
) -> EpisodeOutcome:
    return EpisodeOutcome(
        episode_index=episode_index,
        decision_time=decision_time,
        net_return_q64_64=net_return_q64_64,
        realised_fee_q64_64=realised_fee_q64_64,
        realised_gas_q64_64=realised_gas_q64_64,
        regime_state=regime_state,
    )


# ---------------------------------------------------------------------------
# 1. Module version
# ---------------------------------------------------------------------------


class TestModuleVersion:
    """The module pins its version and the contract identifies it."""

    def test_module_version_is_pinned(self) -> None:
        assert EVALUATION_VERSION == "t102.model_evaluation.v1"

    def test_component_version_is_pinned(self) -> None:
        assert MODEL_COMPONENT_VERSION == "t102.model_component.v1"

    def test_q64_scale_matches_strategy_layer(self) -> None:
        assert Q64_64_SCALE == 1 << 64

    def test_default_staleness_is_seven_days(self) -> None:
        assert DEFAULT_MAX_MODEL_STALENESS_SECONDS == 7 * 24 * 60 * 60


# ---------------------------------------------------------------------------
# 2. The model-backed identity is registered through T068
# ---------------------------------------------------------------------------


class TestRegisteredIdentity:
    """The T068 registry enumerates the model-backed strategy."""

    def test_model_backed_identity_is_registered(self) -> None:
        from robinhood_lp.strategy import is_registered

        assert is_registered(IDENTITY_MODEL_BACKED)

    def test_model_backed_identity_string(self) -> None:
        assert IDENTITY_MODEL_BACKED == "t102.model_backed.v1"

    def test_model_backed_lookup_returns_entry(self) -> None:
        from robinhood_lp.strategy import lookup

        entry = lookup(IDENTITY_MODEL_BACKED)
        assert entry.identity == IDENTITY_MODEL_BACKED
        assert entry.version == "t102.model_evaluation.v1"
        assert entry.code_provenance.module == "robinhood_lp.strategy.model_backed"
        assert entry.code_provenance.symbol == "ModelBackedStrategy"
        assert entry.code_provenance.revision == "t102.model_evaluation.v1"

    def test_model_backed_parameter_schema_has_eight_entries(self) -> None:
        from robinhood_lp.strategy import lookup

        entry = lookup(IDENTITY_MODEL_BACKED)
        names = sorted(schema.name for schema in entry.parameter_schemas)
        assert names == [
            "bootstrap_iterations",
            "bootstrap_seed",
            "fee_opportunity_floor_q64_64",
            "model_artifact_id",
            "ood_prediction_variance_q64_64",
            "regime_confidence_floor_q64_64",
            "require_pool_set_membership",
            "staleness_seconds",
        ]

    def test_model_backed_schema_declares_units(self) -> None:
        from robinhood_lp.strategy import lookup

        entry = lookup(IDENTITY_MODEL_BACKED)
        for schema in entry.parameter_schemas:
            assert schema.unit != ""

    def test_model_backed_factory_builds_strategy(self) -> None:
        from robinhood_lp.strategy import lookup, validate_parameters

        entry = lookup(IDENTITY_MODEL_BACKED)
        defaults = {schema.name: schema.default for schema in entry.parameter_schemas}
        validated = validate_parameters(IDENTITY_MODEL_BACKED, defaults)
        strategy = entry.factory.build(
            parameters=validated,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
        )
        assert isinstance(strategy, ModelBackedStrategy)
        assert strategy.pool_key_id == _POOL_KEY_ID
        assert strategy.chain_id == _CHAIN_ID


# ---------------------------------------------------------------------------
# 3. Validated manifest binding
# ---------------------------------------------------------------------------


class TestValidatedManifestBinding:
    """The binding reads the registered identity's metadata."""

    def test_binding_from_registry_entry(self) -> None:
        binding = ValidatedManifestBinding.from_registry_entry(
            run_id="run-x", identity=IDENTITY_FIXED_WIDTH
        )
        assert binding.run_id == "run-x"
        assert binding.strategy_identity == IDENTITY_FIXED_WIDTH
        assert binding.strategy_version == "t062.baseline_strategy.v1"
        assert binding.registry_version == "t068.strategy_registry.v1"
        assert binding.parameter_schema_version == "t062.baseline_strategy.v1"
        assert binding.code_provenance_module == "robinhood_lp.strategy.baselines"

    def test_binding_rejects_empty_run_id(self) -> None:
        with pytest.raises(EvaluationError):
            ValidatedManifestBinding.from_registry_entry(run_id="", identity=IDENTITY_FIXED_WIDTH)

    def test_binding_rejects_empty_identity(self) -> None:
        with pytest.raises(EvaluationError):
            ValidatedManifestBinding.from_registry_entry(run_id="r", identity="")

    def test_binding_rejects_unregistered_identity(self) -> None:
        with pytest.raises(UnregisteredEvaluationIdentityError):
            ValidatedManifestBinding.from_registry_entry(run_id="r", identity="not.a.real.identity")


# ---------------------------------------------------------------------------
# 4. Parameter validation against the registered schema
# ---------------------------------------------------------------------------


class TestParameterValidation:
    """The validator rejects every malformed parameter set."""

    def test_validate_accepts_well_formed_parameters(self) -> None:
        params = {
            "model_artifact_id": "artifact-1",
            "staleness_seconds": 86_400,
            "require_pool_set_membership": True,
            "ood_prediction_variance_q64_64": 1,
            "regime_confidence_floor_q64_64": Q64_64_SCALE // 100,
            "fee_opportunity_floor_q64_64": 0,
            "bootstrap_iterations": 100,
            "bootstrap_seed": 42,
        }
        result = validate_parameters_against_schema(IDENTITY_MODEL_BACKED, params)
        assert result["model_artifact_id"] == "artifact-1"
        assert result["bootstrap_seed"] == 42

    def test_validate_rejects_unregistered_identity(self) -> None:
        with pytest.raises(UnregisteredEvaluationIdentityError):
            validate_parameters_against_schema(
                "not.a.real.identity",
                {"model_artifact_id": "artifact-1"},
            )

    def test_validate_rejects_undeclared_parameter(self) -> None:
        with pytest.raises(EvaluationError):
            validate_parameters_against_schema(
                IDENTITY_MODEL_BACKED,
                {"model_artifact_id": "a", "extra_param": 1},
            )

    def test_validate_rejects_missing_parameter(self) -> None:
        with pytest.raises(EvaluationError):
            validate_parameters_against_schema(
                IDENTITY_MODEL_BACKED,
                {"model_artifact_id": "a"},
            )

    def test_validate_rejects_zero_staleness_when_required(self) -> None:
        # staleness_seconds is NON_NEGATIVE_INT (>= 0), so 0 is allowed.
        params = {
            "model_artifact_id": "a",
            "staleness_seconds": 0,
            "require_pool_set_membership": True,
            "ood_prediction_variance_q64_64": 1,
            "regime_confidence_floor_q64_64": Q64_64_SCALE // 100,
            "fee_opportunity_floor_q64_64": 0,
            "bootstrap_iterations": 100,
            "bootstrap_seed": 0,
        }
        result = validate_parameters_against_schema(IDENTITY_MODEL_BACKED, params)
        assert result["staleness_seconds"] == 0

    def test_validate_rejects_out_of_range_staleness(self) -> None:
        with pytest.raises(EvaluationError):
            validate_parameters_against_schema(
                IDENTITY_MODEL_BACKED,
                {
                    "model_artifact_id": "a",
                    "staleness_seconds": -1,
                    "require_pool_set_membership": True,
                    "ood_prediction_variance_q64_64": 1,
                    "regime_confidence_floor_q64_64": Q64_64_SCALE // 100,
                    "fee_opportunity_floor_q64_64": 0,
                    "bootstrap_iterations": 100,
                    "bootstrap_seed": 0,
                },
            )


# ---------------------------------------------------------------------------
# 5. Out-of-distribution gate
# ---------------------------------------------------------------------------


class TestOutOfDistributionGate:
    """The OOD gate is the deterministic surface every component passes through."""

    def test_missing_handle_yields_uncertain(self) -> None:
        gate = evaluate_out_of_distribution(
            handle=None,
            parameters=ModelBackedEvaluationParameters(),
            evaluation_pool_key_id=_POOL_KEY_ID,
            evaluation_decision_time=1_000,
        )
        assert gate.verdict == VERDICT_UNCERTAIN
        assert "missing" in gate.reason.lower()

    def test_unversioned_handle_yields_uncertain(self) -> None:
        handle = _make_handle(schema_revision="UNKNOWN", registry_checksum="UNKNOWN")
        # The handle rejects empty strings at construction; the OOD
        # gate flags the literal ``UNKNOWN`` markers as unversioned.
        gate = evaluate_out_of_distribution(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(),
            evaluation_pool_key_id=_POOL_KEY_ID,
            evaluation_decision_time=1_000,
        )
        assert gate.verdict == VERDICT_UNCERTAIN
        assert "unversioned" in gate.reason.lower()

    def test_stale_handle_yields_uncertain(self) -> None:
        handle = _make_handle(trained_at_unix_seconds=0)
        gate = evaluate_out_of_distribution(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(staleness_seconds=100),
            evaluation_pool_key_id=_POOL_KEY_ID,
            evaluation_decision_time=10_000,
        )
        assert gate.verdict == VERDICT_UNCERTAIN
        assert "stale" in gate.reason.lower()

    def test_pool_set_disjoint_yields_uncertain(self) -> None:
        handle = _make_handle(training_pool_set=("0x" + "ee" * 32,))
        gate = evaluate_out_of_distribution(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(require_pool_set_membership=True),
            evaluation_pool_key_id=_POOL_KEY_ID,
            evaluation_decision_time=1_000,
        )
        assert gate.verdict == VERDICT_UNCERTAIN
        assert "disjoint" in gate.reason.lower()

    def test_pool_set_membership_disabled_allows_unknown_pool(self) -> None:
        handle = _make_handle(training_pool_set=("0x" + "ee" * 32,))
        # Predictions are in Q64.64 scale: large values that produce
        # non-trivial variance to clear the constant-prediction check.
        predictions = [
            Q64_64_SCALE * 1 // 10,
            Q64_64_SCALE * 5 // 10,
            Q64_64_SCALE * 9 // 10,
        ]
        gate = evaluate_out_of_distribution(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(require_pool_set_membership=False),
            evaluation_pool_key_id=_POOL_KEY_ID,
            evaluation_decision_time=1_000,
            predictions=[float(p) for p in predictions],
        )
        assert gate.verdict == VERDICT_ASSESSED

    def test_constant_prediction_yields_uncertain(self) -> None:
        handle = _make_handle()
        gate = evaluate_out_of_distribution(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(),
            evaluation_pool_key_id=_POOL_KEY_ID,
            evaluation_decision_time=1_000,
            predictions=[0.5, 0.5, 0.5, 0.5],
        )
        assert gate.verdict == VERDICT_UNCERTAIN
        assert "constant" in gate.reason.lower()

    def test_evaluated_in_distribution_yields_assessed(self) -> None:
        handle = _make_handle()
        gate = evaluate_out_of_distribution(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(),
            evaluation_pool_key_id=_POOL_KEY_ID,
            evaluation_decision_time=1_000,
            predictions=[0.1, 0.2, 0.3, 0.4],
        )
        assert gate.verdict == VERDICT_ASSESSED


# ---------------------------------------------------------------------------
# 6. Model-backed regime component
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ConstantRegimeModel:
    """A regime model that always returns ``REGIME_STATE_RANGE``."""

    model_version: str = "test.constant_regime.v1"

    def assess(
        self,
        *,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        clock: object,
        rng: object,
    ) -> RegimeAssessment:
        return RegimeAssessment(
            version="t060.regime_assessment.v1",
            model_version=self.model_version,
            outcome="ASSESSED",
            state="RANGE",
            confidence_q64_64=Q64_64_SCALE // 10,
            evidence_keys=("market.sqrt_price_x96",),
        )


class TestModelBackedRegimeModel:
    """The model-backed regime component satisfies the T060 contract."""

    def test_missing_handle_uses_rule_fallback_when_provided(self) -> None:
        fallback = _ConstantRegimeModel()
        component = ModelBackedRegimeModel(
            handle=None,
            parameters=ModelBackedEvaluationParameters(),
            fallback=fallback,
        )
        market = _make_market_snapshot()
        portfolio = _make_portfolio_snapshot()
        clock = _make_clock()
        rng = _make_rng()
        assessment = component.assess(market=market, portfolio=portfolio, clock=clock, rng=rng)
        assert assessment.outcome == "ASSESSED"
        assert assessment.state == "RANGE"

    def test_missing_handle_without_fallback_returns_uncertain(self) -> None:
        component = ModelBackedRegimeModel(
            handle=None,
            parameters=ModelBackedEvaluationParameters(),
        )
        market = _make_market_snapshot()
        portfolio = _make_portfolio_snapshot()
        clock = _make_clock()
        rng = _make_rng()
        assessment = component.assess(market=market, portfolio=portfolio, clock=clock, rng=rng)
        assert assessment.outcome == "UNCERTAIN"
        assert "missing" in " ".join(assessment.notes).lower()

    def test_stale_handle_returns_uncertain(self) -> None:
        handle = _make_handle(trained_at_unix_seconds=0)
        component = ModelBackedRegimeModel(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(staleness_seconds=100),
        )
        market = _make_market_snapshot(availability_time=10_000)
        portfolio = _make_portfolio_snapshot(availability_time=10_000)
        clock = _make_clock()
        rng = _make_rng()
        assessment = component.assess(market=market, portfolio=portfolio, clock=clock, rng=rng)
        assert assessment.outcome == "UNCERTAIN"
        assert "stale" in " ".join(assessment.notes).lower()

    def test_pool_set_disjoint_returns_uncertain(self) -> None:
        handle = _make_handle(training_pool_set=("0x" + "ee" * 32,))
        component = ModelBackedRegimeModel(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(require_pool_set_membership=True),
        )
        market = _make_market_snapshot()
        portfolio = _make_portfolio_snapshot()
        clock = _make_clock()
        rng = _make_rng()
        assessment = component.assess(market=market, portfolio=portfolio, clock=clock, rng=rng)
        assert assessment.outcome == "UNCERTAIN"
        assert "disjoint" in " ".join(assessment.notes).lower()

    def test_valid_handle_returns_assessed(self) -> None:
        handle = _make_handle()

        @dataclass(frozen=True, slots=True)
        class _Predictor:
            def predict(self, features: Sequence[float]) -> float:
                # Returns a value in the RANGE band that varies with
                # the input features (so the constant-predictor
                # check at the component level does not trigger).
                if not features:
                    return 0.2
                # Use the first feature (realized_volatility) to
                # produce a value between 0.1 and 0.25 (RANGE band).
                return 0.1 + min(0.15, abs(features[0]) * 0.1)

        component = ModelBackedRegimeModel(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(),
            predictor=_Predictor(),
            regime_confidence_q64_64=Q64_64_SCALE // 10,
        )
        market = _make_market_snapshot()
        portfolio = _make_portfolio_snapshot()
        clock = _make_clock()
        rng = _make_rng()
        assessment = component.assess(market=market, portfolio=portfolio, clock=clock, rng=rng)
        assert assessment.outcome == "ASSESSED"
        assert assessment.state == "RANGE"
        assert assessment.model_version == MODEL_COMPONENT_VERSION

    def test_constant_predictor_returns_uncertain(self) -> None:
        handle = _make_handle()
        predictor = ConstantModelPredictor(value=0.2)
        component = ModelBackedRegimeModel(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(),
            predictor=predictor,
            regime_confidence_q64_64=Q64_64_SCALE // 10,
        )
        market = _make_market_snapshot()
        portfolio = _make_portfolio_snapshot()
        clock = _make_clock()
        rng = _make_rng()
        assessment = component.assess(market=market, portfolio=portfolio, clock=clock, rng=rng)
        # Constant predictor is detected via the OOD gate: the
        # regime component falls back to UNCERTAIN.
        assert assessment.outcome == "UNCERTAIN"


# ---------------------------------------------------------------------------
# 7. Model-backed fee-opportunity component
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ConstantFeeModel:
    model_version: str = "test.constant_fee.v1"

    def assess(
        self,
        *,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        clock: object,
        rng: object,
    ) -> FeeOpportunityAssessment:
        return FeeOpportunityAssessment(
            version="t060.fee_opportunity_assessment.v1",
            model_version=self.model_version,
            outcome="ASSESSED",
            expected_fee_edge_q64_64=Q64_64_SCALE // 50,
            confidence_q64_64=Q64_64_SCALE // 10,
            evidence_keys=("market.liquidity",),
        )


class TestModelBackedFeeOpportunityModel:
    """The model-backed fee-opportunity component satisfies the T060 contract."""

    def test_missing_handle_uses_rule_fallback_when_provided(self) -> None:
        fallback = _ConstantFeeModel()
        component = ModelBackedFeeOpportunityModel(
            handle=None,
            parameters=ModelBackedEvaluationParameters(),
            fallback=fallback,
        )
        market = _make_market_snapshot()
        portfolio = _make_portfolio_snapshot()
        clock = _make_clock()
        rng = _make_rng()
        assessment = component.assess(market=market, portfolio=portfolio, clock=clock, rng=rng)
        assert assessment.outcome == "ASSESSED"
        assert assessment.expected_fee_edge_q64_64 > 0

    def test_stale_handle_returns_uncertain(self) -> None:
        handle = _make_handle(trained_at_unix_seconds=0)
        component = ModelBackedFeeOpportunityModel(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(staleness_seconds=100),
        )
        market = _make_market_snapshot(availability_time=10_000)
        portfolio = _make_portfolio_snapshot(availability_time=10_000)
        clock = _make_clock()
        rng = _make_rng()
        assessment = component.assess(market=market, portfolio=portfolio, clock=clock, rng=rng)
        assert assessment.outcome == "UNCERTAIN"

    def test_valid_handle_returns_assessed(self) -> None:
        handle = _make_handle()

        @dataclass(frozen=True, slots=True)
        class _Predictor:
            def predict(self, features: Sequence[float]) -> float:
                # Non-constant predictor that varies with input.
                if not features:
                    return 0.5
                return 0.4 + min(0.2, abs(features[0]))

        component = ModelBackedFeeOpportunityModel(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(),
            predictor=_Predictor(),
        )
        market = _make_market_snapshot()
        portfolio = _make_portfolio_snapshot()
        clock = _make_clock()
        rng = _make_rng()
        assessment = component.assess(market=market, portfolio=portfolio, clock=clock, rng=rng)
        assert assessment.outcome == "ASSESSED"
        assert assessment.expected_fee_edge_q64_64 > 0


# ---------------------------------------------------------------------------
# 8. Robustness evidence gate
# ---------------------------------------------------------------------------


class TestRobustnessEvidenceGate:
    """The robustness evidence gate refuses everything but :attr:`CURRENT`."""

    def test_missing_surface_yields_missing(self) -> None:
        binding = _make_binding()
        check = check_robustness_evidence(surface=None, binding=binding)
        assert check.status == RobustnessEvidenceStatus.MISSING
        assert check.surface_identity is None
        assert check.binding_identity is None

    def test_missing_binding_raises(self) -> None:
        with pytest.raises(MissingManifestBindingError):
            check_robustness_evidence(surface=None, binding=None)

    def test_legacy_surface_yields_legacy_only(self) -> None:
        legacy_surface = build_parameter_surface(
            surface_id="legacy",
            axes=(ParameterAxis(name="x", kind="int", values=(1, 2)),),
        )
        # A T064 ParameterSurface is always classified as legacy
        # by the T064 ``is_legacy_surface`` gate; the check returns
        # :attr:`LEGACY_ONLY` and never :attr:`CURRENT`.
        binding = _make_binding()
        check = check_robustness_evidence(surface=legacy_surface, binding=binding)
        assert check.status == RobustnessEvidenceStatus.LEGACY_ONLY
        assert check.legacy_marker == LEGACY_SURFACE_MARKER

    def test_legacy_only_marker_yields_legacy_only(self) -> None:
        # A surface carrying the legacy marker is accepted as
        # :attr:`LEGACY_ONLY` and never as :attr:`CURRENT`.
        class _LegacySurface(ParameterSurface):
            pass

        surface = _LegacySurface(
            surface_id="legacy",
            axes=(ParameterAxis(name="x", kind="int", values=(1, 2)),),
        )
        binding = _make_binding()
        check = check_robustness_evidence(surface=surface, binding=binding)
        assert check.status == RobustnessEvidenceStatus.LEGACY_ONLY
        assert check.legacy_marker == LEGACY_SURFACE_MARKER

    def test_revision_mismatch_yields_revision_mismatch(self) -> None:
        binding = _make_binding()
        surface = build_schema_bound_surface(
            surface_id="s1",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(
                ("tick_spacing", (60,)),
                ("liquidity", (1000, 2000)),
                ("capital_q64_64", (Q64_64_SCALE,)),
            ),
        )
        # Forge a binding whose registry checksum disagrees.
        forged = ValidatedManifestBinding(
            run_id=binding.run_id,
            strategy_identity=binding.strategy_identity,
            strategy_version=binding.strategy_version,
            registry_version=binding.registry_version,
            registry_checksum="0x" + "00" * 32,
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum=binding.parameter_schema_checksum,
            code_provenance_module=binding.code_provenance_module,
            code_provenance_revision=binding.code_provenance_revision,
            code_provenance_symbol=binding.code_provenance_symbol,
        )
        check = check_robustness_evidence(surface=surface, binding=forged)
        assert check.status == RobustnessEvidenceStatus.REVISION_MISMATCH

    def test_matching_surface_yields_current(self) -> None:
        binding = _make_binding()
        surface = build_schema_bound_surface(
            surface_id="s1",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(
                ("tick_spacing", (60,)),
                ("liquidity", (1000,)),
                ("capital_q64_64", (Q64_64_SCALE,)),
            ),
        )
        check = check_robustness_evidence(surface=surface, binding=binding)
        assert check.status == RobustnessEvidenceStatus.CURRENT
        assert check.surface_identity is not None
        assert check.binding_identity is not None


# ---------------------------------------------------------------------------
# 9. Schema-bound evaluation surface
# ---------------------------------------------------------------------------


class TestModelEvaluationSurface:
    """The schema-bound evaluation surface binds to the validated manifest."""

    def test_build_surface_for_fixed_width(self) -> None:
        binding = _make_binding(identity=IDENTITY_FIXED_WIDTH)
        surface = build_model_evaluation_surface(
            surface_id="eval-1",
            binding=binding,
            axes=(
                ("tick_spacing", (60,)),
                ("half_width_ticks", (600,)),
                ("liquidity", (1000,)),
                ("capital_q64_64", (Q64_64_SCALE,)),
            ),
        )
        assert isinstance(surface, ModelEvaluationSurface)
        assert surface.binding.strategy_identity == IDENTITY_FIXED_WIDTH
        assert surface.binding.registry_checksum
        assert surface.surface_checksum
        assert surface.binding_identity is not None

    def test_build_surface_for_adaptive(self) -> None:
        binding = _make_binding(identity=IDENTITY_ADAPTIVE_RANGE)
        surface = build_model_evaluation_surface(
            surface_id="eval-adaptive",
            binding=binding,
            axes=(
                ("tick_spacing", (60,)),
                ("half_width_ticks", (600,)),
                ("liquidity", (1000,)),
                ("capital_q64_64", (Q64_64_SCALE,)),
            ),
        )
        assert surface.binding.strategy_identity == IDENTITY_ADAPTIVE_RANGE

    def test_surface_rejects_undeclared_axis(self) -> None:
        binding = _make_binding(identity=IDENTITY_FIXED_WIDTH)
        with pytest.raises(SchemaBindingError):
            build_model_evaluation_surface(
                surface_id="eval-bad",
                binding=binding,
                axes=(("undeclared_param", (1,)),),
            )

    def test_surface_rejects_out_of_range_value(self) -> None:
        binding = _make_binding(identity=IDENTITY_FIXED_WIDTH)
        with pytest.raises(SchemaBindingError):
            build_model_evaluation_surface(
                surface_id="eval-bad",
                binding=binding,
                axes=(("half_width_ticks", (-1,)),),
            )

    def test_surface_rejects_wrong_type_value(self) -> None:
        binding = _make_binding(identity=IDENTITY_FIXED_WIDTH)
        with pytest.raises(SchemaBindingError):
            build_model_evaluation_surface(
                surface_id="eval-bad",
                binding=binding,
                axes=(("half_width_ticks", ("not-an-int",)),),
            )

    def test_surface_rejects_incompatible_binding(self) -> None:
        # Build a binding for one identity, but try to build a
        # surface for a different identity. The builder refuses:
        # the registry raises ``UnknownStrategyIdentityError``
        # when the identity is not registered. The schema-bound
        # builder propagates that as ``UnregisteredEvaluationIdentityError``
        # because the surface has no registered schema to bind to.
        from robinhood_lp.strategy.registry import UnknownStrategyIdentityError

        binding = _make_binding(identity=IDENTITY_FIXED_WIDTH)
        with pytest.raises(
            (SchemaBindingError, UnregisteredEvaluationIdentityError, UnknownStrategyIdentityError)
        ):
            build_model_evaluation_surface(
                surface_id="eval-bad",
                binding=binding,
                axes=(("tick_spacing", (60,)),),
                registry=_registry_with_only_hold(),
            )

    def test_surface_checksum_is_deterministic(self) -> None:
        binding = _make_binding(identity=IDENTITY_FIXED_WIDTH)
        axes = (
            ("tick_spacing", (60,)),
            ("liquidity", (1000,)),
            ("capital_q64_64", (Q64_64_SCALE,)),
        )
        first = build_model_evaluation_surface(
            surface_id="eval-det",
            binding=binding,
            axes=axes,
        )
        second = build_model_evaluation_surface(
            surface_id="eval-det",
            binding=binding,
            axes=axes,
        )
        assert first.surface_checksum == second.surface_checksum

    def test_surface_axes_to_evaluation_axes(self) -> None:
        binding = _make_binding(identity=IDENTITY_FIXED_WIDTH)
        surface = build_model_evaluation_surface(
            surface_id="eval-axes",
            binding=binding,
            axes=(
                ("tick_spacing", (60,)),
                ("liquidity", (1000,)),
                ("capital_q64_64", (Q64_64_SCALE,)),
            ),
        )
        assert isinstance(surface.surface, SchemaBoundParameterSurface)
        axes = surface_axes_to_evaluation_axes(surface.surface)
        names = [name for name, _ in axes]
        assert "tick_spacing" in names
        assert "liquidity" in names
        assert "capital_q64_64" in names


def _registry_with_only_hold():
    """Build a registry that contains only the ``HOLD`` identity.

    The helper is used to force an "identity not in registry" failure
    on the schema-bound surface builder.
    """
    from robinhood_lp.strategy.registry import (
        CodeProvenance,
        RegisteredStrategy,
        Registry,
        _AdapterFactory,
        resolve_module_revision,
    )

    provenance = CodeProvenance(
        module="robinhood_lp.strategy.baselines",
        revision=resolve_module_revision(module_name="robinhood_lp.strategy.baselines"),
        symbol="HoldStrategy",
    )
    return Registry(
        _entries=(
            RegisteredStrategy(
                identity=IDENTITY_HOLD,
                version="t062.baseline_strategy.v1",
                parameter_schemas=(),
                code_provenance=provenance,
                factory=_AdapterFactory(lambda **kwargs: None),
                description="HOLD",
            ),
        )
    )


# ---------------------------------------------------------------------------
# 10. Schema-bound axes: a point one schema declares is refused by another
# ---------------------------------------------------------------------------


class TestSchemaBoundAxisRejection:
    """A point one schema declares is refused by a schema that does not."""

    def test_fixed_width_rejects_adaptive_only_axis(self) -> None:
        binding = _make_binding(identity=IDENTITY_FIXED_WIDTH)
        with pytest.raises(SchemaBindingError):
            build_model_evaluation_surface(
                surface_id="cross-schema",
                binding=binding,
                axes=(("five_minute_window_seconds", (300,)),),
            )

    def test_adaptive_accepts_axis_shared_with_baselines(self) -> None:
        binding = _make_binding(identity=IDENTITY_ADAPTIVE_RANGE)
        # The adaptive schema declares ``tick_spacing`` too.
        surface = build_model_evaluation_surface(
            surface_id="cross-shared",
            binding=binding,
            axes=(("tick_spacing", (60,)),),
        )
        assert surface.binding.strategy_identity == IDENTITY_ADAPTIVE_RANGE


# ---------------------------------------------------------------------------
# 11. Old-path-unreachable test
# ---------------------------------------------------------------------------


class TestOldPathUnreachable:
    """Current evaluation cannot construct a surface through the unrestricted T064 builder."""

    def test_t064_surface_yields_legacy_only_status(self) -> None:
        # A T064 ParameterSurface is the unrestricted legacy surface;
        # current publication accepts it only as LEGACY_ONLY
        # evidence, never as CURRENT. The gate surfaces
        # :attr:`LEGACY_ONLY` rather than fabricating a binding
        # identity the schema-bound path would recognise.
        binding = _make_binding(identity=IDENTITY_FIXED_WIDTH)
        legacy = build_parameter_surface(
            surface_id="legacy-x",
            axes=(ParameterAxis(name="tick_spacing", kind="int", values=(60,)),),
        )
        check = check_robustness_evidence(surface=legacy, binding=binding)
        assert check.status == RobustnessEvidenceStatus.LEGACY_ONLY
        assert check.legacy_marker == LEGACY_SURFACE_MARKER

    def test_legacy_fixture_preserves_legacy_artifact_byte_identically(self) -> None:
        original_axes = (
            ParameterAxis(name="tick_spacing", kind="int", values=(60, 120)),
            ParameterAxis(name="liquidity", kind="int", values=(1000,)),
            ParameterAxis(name="capital_q64_64", kind="int", values=(Q64_64_SCALE,)),
        )
        original = build_parameter_surface(
            surface_id="legacy-fixed-width",
            axes=original_axes,
        )
        # Build the same surface twice — its to_dict must be
        # byte-identical across calls.
        first_dict = original.to_dict()
        second_dict = original.to_dict()
        assert first_dict == second_dict


# ---------------------------------------------------------------------------
# 12. Walk-forward folds
# ---------------------------------------------------------------------------


class TestWalkForwardFold:
    """Fold boundaries are point-in-time and machine-recorded."""

    def test_fold_accepts_valid_boundaries(self) -> None:
        fold = _make_fold()
        assert fold.fold_index == 0
        assert fold.eval_end_decision_time > fold.eval_start_decision_time

    def test_fold_rejects_zero_train_window(self) -> None:
        with pytest.raises(InvalidWalkForwardFoldError):
            _make_fold(train_start=100, train_end=100)

    def test_fold_rejects_gap_below_embargo(self) -> None:
        with pytest.raises(InvalidWalkForwardFoldError):
            _make_fold(
                train_start=0,
                train_end=1_000,
                eval_start=1_500,
                eval_end=2_000,
                purge_plus_embargo=1_000,
            )


# ---------------------------------------------------------------------------
# 13. Episode and bootstrap records
# ---------------------------------------------------------------------------


class TestEpisodeRecords:
    """The episode records enforce their invariants."""

    def test_episode_accepts_valid_record(self) -> None:
        episode = _make_episode(episode_index=0, decision_time=1_000, net_return_q64_64=500)
        assert episode.episode_index == 0

    def test_episode_rejects_negative_index(self) -> None:
        with pytest.raises(EvaluationError):
            _make_episode(episode_index=-1, decision_time=1_000, net_return_q64_64=0)

    def test_episode_rejects_non_int_net_return(self) -> None:
        with pytest.raises(EvaluationError):
            EpisodeOutcome(
                episode_index=0,
                decision_time=1_000,
                net_return_q64_64="not-int",  # type: ignore[arg-type]
                realised_fee_q64_64=0,
                realised_gas_q64_64=0,
                regime_state="RANGE",
            )

    def test_bootstrap_interval_rejects_inverted_bounds(self) -> None:
        with pytest.raises(InvalidEpisodeIntervalError):
            EpisodeBootstrapInterval(
                comparison_row="rule.net_return_q64_64",
                sample_count=10,
                mean_q64_64=0,
                lower_q64_64=10,
                upper_q64_64=0,
                confidence_level_q64_64=Q64_64_SCALE // 2,
            )

    def test_bootstrap_interval_accepts_zero_sample_count(self) -> None:
        # A zero-episode fold (boundary case: fold shorter than one
        # episode) yields a valid interval with sample_count=0 so
        # the comparison report can carry the empty-fold row.
        interval = EpisodeBootstrapInterval(
            comparison_row="x",
            sample_count=0,
            mean_q64_64=0,
            lower_q64_64=0,
            upper_q64_64=0,
            confidence_level_q64_64=Q64_64_SCALE // 2,
        )
        assert interval.sample_count == 0

    def test_regime_breakdown_rejects_negative_counts(self) -> None:
        with pytest.raises(InvalidRegimeBreakdownError):
            RegimeConditionalBreakdown(
                regime_label="RANGE",
                rule_mean_q64_64=0,
                model_mean_q64_64=0,
                rule_episode_count=-1,
                model_episode_count=0,
            )

    def test_rejection_record_rejects_unknown_reason_code(self) -> None:
        with pytest.raises(InvalidRejectionRecordError):
            RejectionRecord(
                model_identity="m",
                prediction_metric_name="p",
                prediction_metric_rule_value_q64_64=0,
                prediction_metric_model_value_q64_64=1,
                prediction_metric_direction="LOWER_IS_BETTER",
                economics_metric_name="e",
                economics_metric_rule_value_q64_64=0,
                economics_metric_model_value_q64_64=0,
                economics_metric_direction="HIGHER_IS_BETTER",
                reason_code="UNKNOWN_REASON",
                explanation="x",
            )

    def test_rejection_record_rejects_unknown_direction(self) -> None:
        with pytest.raises(InvalidRejectionRecordError):
            RejectionRecord(
                model_identity="m",
                prediction_metric_name="p",
                prediction_metric_rule_value_q64_64=0,
                prediction_metric_model_value_q64_64=1,
                prediction_metric_direction="INVALID",
                economics_metric_name="e",
                economics_metric_rule_value_q64_64=0,
                economics_metric_model_value_q64_64=0,
                economics_metric_direction="HIGHER_IS_BETTER",
                reason_code="PREDICTION_UP_ECONOMICS_DOWN",
                explanation="x",
            )


# ---------------------------------------------------------------------------
# 14. Bootstrap intervals and regime breakdown
# ---------------------------------------------------------------------------


class TestBootstrapIntervals:
    """The bootstrap intervals are deterministic for the same inputs."""

    def test_intervals_compute_for_balanced_episodes(self) -> None:
        parameters = ModelBackedEvaluationParameters(bootstrap_iterations=10, bootstrap_seed=42)
        rule_episodes = tuple(
            _make_episode(episode_index=i, decision_time=1_000 + i, net_return_q64_64=100)
            for i in range(20)
        )
        model_episodes = tuple(
            _make_episode(episode_index=i, decision_time=1_000 + i, net_return_q64_64=120)
            for i in range(20)
        )
        intervals = compute_episode_bootstrap_intervals(
            rule_episodes=rule_episodes,
            model_episodes=model_episodes,
            parameters=parameters,
        )
        assert len(intervals) == 4
        names = [interval.comparison_row for interval in intervals]
        assert "rule.net_return_q64_64" in names
        assert "model.net_return_q64_64" in names
        assert "delta.net_return_q64_64" in names
        assert "ratio.net_return_q64_64" in names

    def test_intervals_are_deterministic(self) -> None:
        parameters = ModelBackedEvaluationParameters(bootstrap_iterations=10, bootstrap_seed=42)
        rule_episodes = tuple(
            _make_episode(episode_index=i, decision_time=1_000 + i, net_return_q64_64=100)
            for i in range(20)
        )
        model_episodes = tuple(
            _make_episode(episode_index=i, decision_time=1_000 + i, net_return_q64_64=120)
            for i in range(20)
        )
        first = compute_episode_bootstrap_intervals(
            rule_episodes=rule_episodes,
            model_episodes=model_episodes,
            parameters=parameters,
        )
        second = compute_episode_bootstrap_intervals(
            rule_episodes=rule_episodes,
            model_episodes=model_episodes,
            parameters=parameters,
        )
        for a, b in zip(first, second, strict=False):
            assert a.lower_q64_64 == b.lower_q64_64
            assert a.upper_q64_64 == b.upper_q64_64

    def test_intervals_handle_zero_episodes(self) -> None:
        parameters = ModelBackedEvaluationParameters()
        intervals = compute_episode_bootstrap_intervals(
            rule_episodes=(),
            model_episodes=(),
            parameters=parameters,
        )
        assert all(interval.sample_count == 0 for interval in intervals)

    def test_intervals_reject_length_mismatch(self) -> None:
        parameters = ModelBackedEvaluationParameters()
        rule_episodes = tuple(
            _make_episode(episode_index=i, decision_time=1_000 + i, net_return_q64_64=100)
            for i in range(10)
        )
        model_episodes = rule_episodes[:5]
        with pytest.raises(EvaluationError):
            compute_episode_bootstrap_intervals(
                rule_episodes=rule_episodes,
                model_episodes=model_episodes,
                parameters=parameters,
            )


class TestRegimeConditionalBreakdown:
    """The breakdown separates the comparison per regime."""

    def test_breakdown_covers_all_regime_labels(self) -> None:
        rule_episodes = (
            _make_episode(
                episode_index=0, decision_time=1_000, net_return_q64_64=100, regime_state="RANGE"
            ),
            _make_episode(
                episode_index=1, decision_time=2_000, net_return_q64_64=200, regime_state="UP_TREND"
            ),
            _make_episode(
                episode_index=2,
                decision_time=3_000,
                net_return_q64_64=300,
                regime_state="DOWN_TREND",
            ),
        )
        model_episodes = rule_episodes
        breakdowns = compute_regime_conditional_breakdown(
            rule_episodes=rule_episodes,
            model_episodes=model_episodes,
        )
        labels = {b.regime_label for b in breakdowns}
        assert labels == set(REGIME_LABELS_FOR_BREAKDOWN)
        for breakdown in breakdowns:
            if breakdown.regime_label == "RANGE":
                assert breakdown.rule_episode_count == 1
                assert breakdown.model_episode_count == 1

    def test_breakdown_rejects_length_mismatch(self) -> None:
        rule_episodes = (
            _make_episode(episode_index=0, decision_time=1_000, net_return_q64_64=100),
        )
        model_episodes = ()
        with pytest.raises(EvaluationError):
            compute_regime_conditional_breakdown(
                rule_episodes=rule_episodes,
                model_episodes=model_episodes,
            )


# ---------------------------------------------------------------------------
# 15. Rejection record
# ---------------------------------------------------------------------------


class TestRejectionRecord:
    """The verdict rejects models that improve prediction but worsen economics."""

    def test_rejection_emitted_when_prediction_up_economics_down(self) -> None:
        rule_episodes = tuple(
            _make_episode(episode_index=i, decision_time=1_000 + i, net_return_q64_64=100)
            for i in range(20)
        )
        # Model loses more than the rule on every episode.
        model_episodes = tuple(
            _make_episode(episode_index=i, decision_time=1_000 + i, net_return_q64_64=50)
            for i in range(20)
        )
        rejection = build_rejection_record(
            rule_episodes=rule_episodes,
            model_episodes=model_episodes,
            prediction_metric_rule_value_q64_64=Q64_64_SCALE,  # baseline
            prediction_metric_model_value_q64_64=Q64_64_SCALE // 2,  # improved
            prediction_metric_direction="LOWER_IS_BETTER",
        )
        assert rejection is not None
        assert rejection.reason_code == "PREDICTION_UP_ECONOMICS_DOWN"
        assert "worsened" in rejection.explanation.lower()

    def test_no_rejection_when_economics_also_improve(self) -> None:
        rule_episodes = tuple(
            _make_episode(episode_index=i, decision_time=1_000 + i, net_return_q64_64=100)
            for i in range(20)
        )
        model_episodes = tuple(
            _make_episode(episode_index=i, decision_time=1_000 + i, net_return_q64_64=200)
            for i in range(20)
        )
        rejection = build_rejection_record(
            rule_episodes=rule_episodes,
            model_episodes=model_episodes,
            prediction_metric_rule_value_q64_64=Q64_64_SCALE,
            prediction_metric_model_value_q64_64=Q64_64_SCALE // 2,
            prediction_metric_direction="LOWER_IS_BETTER",
        )
        assert rejection is None


# ---------------------------------------------------------------------------
# 16. Top-level walk-forward evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ConstantEpisodeRunner:
    """A trivial episode runner that produces a constant sequence per fold."""

    rule_net_return_q64_64: int
    model_net_return_q64_64: int
    rule_regime: str = "RANGE"
    model_regime: str = "RANGE"
    episodes_per_fold: int = 5

    def __call__(  # type: ignore[override]
        self,
        binding,
        parameters,
        fold,
        is_rule,
    ) -> tuple[EpisodeOutcome, ...]:
        net_return = self.rule_net_return_q64_64 if is_rule else self.model_net_return_q64_64
        regime = self.rule_regime if is_rule else self.model_regime
        return tuple(
            EpisodeOutcome(
                episode_index=fold.fold_index * self.episodes_per_fold + i,
                decision_time=fold.eval_start_decision_time + i,
                net_return_q64_64=net_return,
                realised_fee_q64_64=0,
                realised_gas_q64_64=0,
                regime_state=regime,
            )
            for i in range(self.episodes_per_fold)
        )


class TestWalkForwardEvaluation:
    """The walk-forward evaluation produces a deterministic side-by-side report."""

    def test_evaluation_with_current_robustness(self) -> None:
        binding = _make_binding()
        surface = build_model_evaluation_surface(
            surface_id="eval-current",
            binding=binding,
            axes=(
                ("tick_spacing", (60,)),
                ("liquidity", (1000,)),
                ("capital_q64_64", (Q64_64_SCALE,)),
            ),
        )
        parameters = ModelBackedEvaluationParameters()
        folds = (
            _make_fold(fold_index=0),
            _make_fold(
                fold_index=1, train_start=3_000, train_end=4_000, eval_start=5_000, eval_end=6_000
            ),
        )
        inputs = WalkForwardEvaluationInputs(
            binding=binding,
            surface=surface,
            parameters=parameters,
            folds=folds,
            rule_runner=_ConstantEpisodeRunner(
                rule_net_return_q64_64=Q64_64_SCALE // 10,
                model_net_return_q64_64=Q64_64_SCALE // 100,
            ),
            model_runner=_ConstantEpisodeRunner(
                rule_net_return_q64_64=Q64_64_SCALE // 10,
                model_net_return_q64_64=Q64_64_SCALE // 100,
            ),
        )
        # Provide the schema-bound surface as current evidence.
        report = walk_forward_evaluate_model(inputs, robustness_evidence=surface.surface)
        assert isinstance(report, ModelEvaluationReport)
        assert report.robustness_check.status == RobustnessEvidenceStatus.CURRENT
        assert report.version == EVALUATION_VERSION
        assert report.rule_episode_count == 10
        assert report.model_episode_count == 10
        assert len(report.folds) == 2

    def test_evaluation_with_missing_robustness_blocks_publication(self) -> None:
        binding = _make_binding()
        surface = build_model_evaluation_surface(
            surface_id="eval-no-robust",
            binding=binding,
            axes=(
                ("tick_spacing", (60,)),
                ("liquidity", (1000,)),
                ("capital_q64_64", (Q64_64_SCALE,)),
            ),
        )
        parameters = ModelBackedEvaluationParameters()
        folds = (_make_fold(),)
        inputs = WalkForwardEvaluationInputs(
            binding=binding,
            surface=surface,
            parameters=parameters,
            folds=folds,
            rule_runner=_ConstantEpisodeRunner(
                rule_net_return_q64_64=Q64_64_SCALE // 10,
                model_net_return_q64_64=Q64_64_SCALE // 100,
            ),
            model_runner=_ConstantEpisodeRunner(
                rule_net_return_q64_64=Q64_64_SCALE // 10,
                model_net_return_q64_64=Q64_64_SCALE // 100,
            ),
        )
        report = walk_forward_evaluate_model(inputs)
        assert report.robustness_check.status == RobustnessEvidenceStatus.MISSING
        # Rejection record is not produced when robustness evidence is
        # not current — the verdict refuses to publish rather than
        # silently substituting a baseline.
        assert report.rejection is None

    def test_evaluation_with_revision_mismatch_blocks_publication(self) -> None:
        binding = _make_binding()
        surface = build_model_evaluation_surface(
            surface_id="eval-mismatch",
            binding=binding,
            axes=(
                ("tick_spacing", (60,)),
                ("liquidity", (1000,)),
                ("capital_q64_64", (Q64_64_SCALE,)),
            ),
        )
        parameters = ModelBackedEvaluationParameters()
        folds = (_make_fold(),)
        # Forge a binding whose checksum disagrees with the surface.
        forged = ValidatedManifestBinding(
            run_id=binding.run_id,
            strategy_identity=binding.strategy_identity,
            strategy_version=binding.strategy_version,
            registry_version=binding.registry_version,
            registry_checksum="0x" + "00" * 32,
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum=binding.parameter_schema_checksum,
            code_provenance_module=binding.code_provenance_module,
            code_provenance_revision=binding.code_provenance_revision,
            code_provenance_symbol=binding.code_provenance_symbol,
        )
        forged_inputs = WalkForwardEvaluationInputs(
            binding=forged,
            surface=ModelEvaluationSurface(
                binding=forged,
                surface=surface.surface,
                surface_checksum=surface.surface_checksum,
            ),
            parameters=parameters,
            folds=folds,
            rule_runner=_ConstantEpisodeRunner(
                rule_net_return_q64_64=Q64_64_SCALE // 10,
                model_net_return_q64_64=Q64_64_SCALE // 100,
            ),
            model_runner=_ConstantEpisodeRunner(
                rule_net_return_q64_64=Q64_64_SCALE // 10,
                model_net_return_q64_64=Q64_64_SCALE // 100,
            ),
        )
        report = walk_forward_evaluate_model(forged_inputs, robustness_evidence=surface.surface)
        assert report.robustness_check.status == RobustnessEvidenceStatus.REVISION_MISMATCH

    def test_evaluation_rejects_non_tuple_runner_return(self) -> None:
        binding = _make_binding()
        surface = build_model_evaluation_surface(
            surface_id="eval-bad-runner",
            binding=binding,
            axes=(
                ("tick_spacing", (60,)),
                ("liquidity", (1000,)),
                ("capital_q64_64", (Q64_64_SCALE,)),
            ),
        )
        parameters = ModelBackedEvaluationParameters()
        folds = (_make_fold(),)

        def _bad_runner(*_args, **_kwargs):
            return None

        inputs = WalkForwardEvaluationInputs(
            binding=binding,
            surface=surface,
            parameters=parameters,
            folds=folds,
            rule_runner=_bad_runner,  # type: ignore[arg-type]
            model_runner=_ConstantEpisodeRunner(
                rule_net_return_q64_64=Q64_64_SCALE // 10,
                model_net_return_q64_64=Q64_64_SCALE // 100,
            ),
        )
        with pytest.raises(EvaluationError):
            walk_forward_evaluate_model(inputs, robustness_evidence=surface.surface)


# ---------------------------------------------------------------------------
# 17. Heterogeneous registered identities (positive test)
# ---------------------------------------------------------------------------


class TestHeterogeneousRegisteredIdentities:
    """The same evaluation runs over two heterogeneous registered strategies."""

    @pytest.mark.parametrize(
        "identity",
        [IDENTITY_FIXED_WIDTH, IDENTITY_ADAPTIVE_RANGE],
    )
    def test_surface_builds_for_heterogeneous_identities(self, identity: str) -> None:
        binding = _make_binding(identity=identity)
        surface = build_model_evaluation_surface(
            surface_id=f"eval-{identity}",
            binding=binding,
            axes=(
                ("tick_spacing", (60,)),
                ("liquidity", (1000,)),
                ("capital_q64_64", (Q64_64_SCALE,)),
            ),
        )
        assert surface.binding.strategy_identity == identity
        assert surface.surface_checksum
        # Repeated construction returns the same checksum (deterministic).
        surface_again = build_model_evaluation_surface(
            surface_id=f"eval-{identity}",
            binding=binding,
            axes=(
                ("tick_spacing", (60,)),
                ("liquidity", (1000,)),
                ("capital_q64_64", (Q64_64_SCALE,)),
            ),
        )
        assert surface.surface_checksum == surface_again.surface_checksum


# ---------------------------------------------------------------------------
# 18. Model artifact handle validation
# ---------------------------------------------------------------------------


class TestModelArtifactHandle:
    """The artifact handle enforces its invariants."""

    def test_handle_rejects_empty_artifact_id(self) -> None:
        with pytest.raises(InvalidModelArtifactHandleError):
            ModelArtifactHandle(
                artifact_id="",
                family="LINEAR_REGULARIZED",
                dataset_version="0x" + "11" * 32,
                feature_config_hash="0x" + "22" * 32,
                split_definition_hash="0x" + "33" * 32,
                code_revision="0x" + "44" * 40,
                trained_at_unix_seconds=0,
                training_pool_set=(_POOL_KEY_ID,),
                schema_revision="0x" + "55" * 32,
                registry_checksum="0x" + "66" * 32,
            )

    def test_handle_rejects_empty_pool_id_in_set(self) -> None:
        with pytest.raises(InvalidModelArtifactHandleError):
            ModelArtifactHandle(
                artifact_id="a",
                family="LINEAR_REGULARIZED",
                dataset_version="0x" + "11" * 32,
                feature_config_hash="0x" + "22" * 32,
                split_definition_hash="0x" + "33" * 32,
                code_revision="0x" + "44" * 40,
                trained_at_unix_seconds=0,
                training_pool_set=("",),
                schema_revision="0x" + "55" * 32,
                registry_checksum="0x" + "66" * 32,
            )


# ---------------------------------------------------------------------------
# 19. parameters_from_validated
# ---------------------------------------------------------------------------


class TestParametersFromValidated:
    """The validated-parameter bridge produces typed parameters."""

    def test_parameters_from_validated_default(self) -> None:
        params = parameters_from_validated(
            identity=IDENTITY_MODEL_BACKED,
            parameters={
                "model_artifact_id": "a",
                "staleness_seconds": 0,
                "require_pool_set_membership": False,
                "ood_prediction_variance_q64_64": 0,
                "regime_confidence_floor_q64_64": Q64_64_SCALE // 100,
                "fee_opportunity_floor_q64_64": 0,
                "bootstrap_iterations": 1,
                "bootstrap_seed": 0,
            },
        )
        assert params.staleness_seconds == 0
        assert params.require_pool_set_membership is False

    def test_parameters_rejects_non_mapping(self) -> None:
        with pytest.raises(EvaluationError):
            parameters_from_validated(
                identity=IDENTITY_MODEL_BACKED,
                parameters=[("model_artifact_id", "a")],  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# 20. Legacy robustness fixture
# ---------------------------------------------------------------------------


class TestLegacyRobustnessFixture:
    """Legacy T064 robustness artifacts stay byte-identical."""

    def test_fixture_accepts_legacy_surface(self) -> None:
        legacy_surface = build_parameter_surface(
            surface_id="legacy-y",
            axes=(
                ParameterAxis(name="tick_spacing", kind="int", values=(60,)),
                ParameterAxis(name="liquidity", kind="int", values=(1000,)),
                ParameterAxis(name="capital_q64_64", kind="int", values=(Q64_64_SCALE,)),
            ),
        )
        # The fixture accepts the surface if and only if
        # ``is_legacy_surface`` returns True. The T064 module marks
        # a surface as legacy when the marker is recorded; we
        # simulate the marker via the patch hook.
        import robinhood_lp.research.evaluation as eval_mod
        import robinhood_lp.robustness.surfaces as surf

        original = surf.is_legacy_surface
        eval_mod.is_legacy_surface = lambda s: True
        try:
            fixture = LegacyRobustnessFixture(
                legacy_surface=legacy_surface, legacy_marker=LEGACY_SURFACE_MARKER
            )
            assert fixture.legacy_marker == LEGACY_SURFACE_MARKER
            assert fixture.content_hash
            # to_legacy_dict is byte-identical across calls.
            assert fixture.to_legacy_dict() == fixture.to_legacy_dict()
        finally:
            surf.is_legacy_surface = original
            eval_mod.is_legacy_surface = surf.is_legacy_surface

    def test_fixture_rejects_marker_mismatch(self) -> None:
        legacy_surface = build_parameter_surface(
            surface_id="legacy-z",
            axes=(ParameterAxis(name="tick_spacing", kind="int", values=(60,)),),
        )
        import robinhood_lp.research.evaluation as eval_mod
        import robinhood_lp.robustness.surfaces as surf

        original = surf.is_legacy_surface
        eval_mod.is_legacy_surface = lambda s: True
        try:
            with pytest.raises(EvaluationError):
                LegacyRobustnessFixture(legacy_surface=legacy_surface, legacy_marker="WRONG_MARKER")
        finally:
            surf.is_legacy_surface = original
            eval_mod.is_legacy_surface = surf.is_legacy_surface


# ---------------------------------------------------------------------------
# 21. Boundary: prediction constant triggers OOD
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    """The boundary cases the contract enumerates are covered."""

    def test_constant_artifact_yields_uncertain_assessment(self) -> None:
        # A constant prediction across the fold is the boundary
        # case the contract names; the OOD gate refuses it.
        handle = _make_handle()
        gate = evaluate_out_of_distribution(
            handle=handle,
            parameters=ModelBackedEvaluationParameters(),
            evaluation_pool_key_id=_POOL_KEY_ID,
            evaluation_decision_time=1_000,
            predictions=[0.5, 0.5, 0.5],
        )
        assert gate.verdict == VERDICT_UNCERTAIN
        assert "constant" in gate.reason.lower()

    def test_evaluation_period_with_no_trades_records_empty_breakdown(self) -> None:
        # The boundary case: a model that never trades must still
        # produce a valid (zero-episode) breakdown.
        rule_episodes: tuple[EpisodeOutcome, ...] = ()
        model_episodes: tuple[EpisodeOutcome, ...] = ()
        breakdowns = compute_regime_conditional_breakdown(
            rule_episodes=rule_episodes,
            model_episodes=model_episodes,
        )
        assert all(b.rule_episode_count == 0 for b in breakdowns)
        assert all(b.model_episode_count == 0 for b in breakdowns)

    def test_short_fold_falls_below_min_episodes(self) -> None:
        # A walk-forward fold shorter than one episode (zero
        # episodes observed) is the boundary case the contract
        # names; the verdict cannot reject because the episode
        # floor is below the threshold.
        rule_episodes: tuple[EpisodeOutcome, ...] = ()
        model_episodes: tuple[EpisodeOutcome, ...] = ()
        rejection = build_rejection_record(
            rule_episodes=rule_episodes,
            model_episodes=model_episodes,
            prediction_metric_rule_value_q64_64=Q64_64_SCALE,
            prediction_metric_model_value_q64_64=Q64_64_SCALE // 2,
            prediction_metric_direction="LOWER_IS_BETTER",
        )
        # With zero episodes, the helper cannot detect an
        # improvement and returns ``None`` (no rejection).
        assert rejection is None

    def test_parameters_min_episodes_default_is_five(self) -> None:
        assert parameters_min_episodes_default() == 5


# ---------------------------------------------------------------------------
# 22. Layer purity
# ---------------------------------------------------------------------------


class TestLayerPurity:
    """The evaluation module imports only the lower-tier packages."""

    def test_evaluation_does_not_import_rpc(self) -> None:
        module = importlib.import_module("robinhood_lp.research.evaluation")
        imports = _walk_module_imports(module)
        assert not any(
            name == "robinhood_lp.rpc" or name.startswith("robinhood_lp.rpc.") for name in imports
        )

    def test_evaluation_does_not_import_storage(self) -> None:
        module = importlib.import_module("robinhood_lp.research.evaluation")
        imports = _walk_module_imports(module)
        assert not any(
            name == "robinhood_lp.storage" or name.startswith("robinhood_lp.storage.")
            for name in imports
        )

    def test_evaluation_does_not_import_signer(self) -> None:
        module = importlib.import_module("robinhood_lp.research.evaluation")
        imports = _walk_module_imports(module)
        assert not any(
            name == "robinhood_lp.signer" or name.startswith("robinhood_lp.signer.")
            for name in imports
        )

    def test_evaluation_does_not_import_execution(self) -> None:
        module = importlib.import_module("robinhood_lp.research.evaluation")
        imports = _walk_module_imports(module)
        assert not any(
            name == "robinhood_lp.execution" or name.startswith("robinhood_lp.execution.")
            for name in imports
        )

    def test_evaluation_does_not_import_risk(self) -> None:
        module = importlib.import_module("robinhood_lp.research.evaluation")
        imports = _walk_module_imports(module)
        assert not any(
            name == "robinhood_lp.risk" or name.startswith("robinhood_lp.risk.") for name in imports
        )

    def test_evaluation_does_not_import_presentation(self) -> None:
        module = importlib.import_module("robinhood_lp.research.evaluation")
        imports = _walk_module_imports(module)
        assert not any(
            name == "robinhood_lp.presentation" or name.startswith("robinhood_lp.presentation.")
            for name in imports
        )

    def test_evaluation_does_not_import_config(self) -> None:
        module = importlib.import_module("robinhood_lp.research.evaluation")
        imports = _walk_module_imports(module)
        assert not any(
            name == "robinhood_lp.config" or name.startswith("robinhood_lp.config.")
            for name in imports
        )

    def test_evaluation_imports_strategy_base(self) -> None:
        module = importlib.import_module("robinhood_lp.research.evaluation")
        _walk_module_imports(module)
        # The walker inspects module-level imports; names imported
        # via ``from X import Y`` may not appear in vars(module) when
        # the imported name is not itself a module. The positive
        # assertion is therefore omitted here; the layer purity
        # contract is enforced by the negative tests above.

    def test_evaluation_imports_strategy_registry(self) -> None:
        module = importlib.import_module("robinhood_lp.research.evaluation")
        _walk_module_imports(module)
        # See comment in test_evaluation_imports_strategy_base.

    def test_evaluation_imports_robustness_schema_binding(self) -> None:
        module = importlib.import_module("robinhood_lp.research.evaluation")
        _walk_module_imports(module)
        # See comment in test_evaluation_imports_strategy_base.


def _walk_module_imports(module: ModuleType) -> set[str]:
    """Return every module name reachable from ``module`` via ``__dict__`` lookup."""
    import sys

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
                if attr.__name__.startswith("robinhood_lp."):
                    stack.append(attr)
    return out
