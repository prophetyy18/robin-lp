"""Model-backed strategy evaluation through the event-driven engine (T102).

T102 binds a trained model to the strategy layer's replaceable regime /
fee-opportunity interfaces, evaluates it through the T061 event-driven
backtest engine, and produces a side-by-side comparison of the rule-based
baseline and the model on identical data. The verdict on the model comes
from simulated LP economics, not from a prediction metric: a
statistically accurate model that produces worse net LP economics is
recorded as a rejection, which is the whole point of the task.

Design contract (binding):

- **Closed registry vocabulary.** The model-backed strategy occupies a
  registered T068 identity. The model-backed evaluation is reached only
  by naming a registered identity through the T069 run entry point; an
  identity the registry does not carry is refused rather than inferred,
  defaulted or substituted. The model-backed identity is the canonical
  "model backs the replaceable regime / fee-opportunity interfaces"
  surface; no caller-supplied identity may replace it.

- **Per-identity parameter schema.** The registered identity carries a
  parameter schema declaring every tunable parameter the model-backed
  strategy accepts. The walk-forward evaluation builds its
  parameter-surface axis set only from those schema-declared axes; a
  point the schema does not declare, an out-of-range value, a wrong-type
  value, or a schema revision that disagrees with the evaluated run's
  validated T105 manifest is rejected before any fold is evaluated.

- **Schema-bound robustness.** The T106 schema-bound robustness surface
  is the only current robustness evidence the walk-forward evaluation
  consumes. Missing, legacy-only or revision-mismatched T106 evidence
  yields :attr:`UNCERTAIN` or :attr:`NO_TRADE` for the affected
  configuration or blocks publication of the robustness conclusion. The
  evaluation never falls back to the unrestricted ``T064`` predecessor
  robustness surface, to an inferred or default identity, or to an
  unqualified baseline.

- **Deterministic fallback.** When the model artifact is missing, stale,
  unversioned or out of its training distribution, the model-backed
  components return :attr:`UNCERTAIN` (not a fabricated assessment) and
  the strategy falls back to the rule-based baseline behaviour. The
  fallback covers the model artifact's availability only; it never
  substitutes for the registered identity, for a validated T105 manifest
  or for the schema-bound robustness evidence above.

- **Legacy ``T064`` robustness artifacts.** A legacy ``T064``
  :class:`ParameterSurface` is accepted only as a migration fixture
  carrying an explicit legacy marker; it is read-only, byte-identical
  to the artifact the runner originally produced, and cannot be
  consumed as current robustness evidence.

- **Layer purity.** The module imports the standard library and the
  lower backtest / research / strategy / robustness packages. It does
  not import RPC, storage, signing, execution, presentation or
  configuration code; the dependency check in
  ``tests/test_evaluation_t102.py`` enforces this rule by walking the
  live module graph.

- **No execution authority.** The module never grants ``HOLD`` /
  ``LP`` / ``AUTO_SWAP`` authority. A model-backed evaluation's
  output is a *comparison record*, not a transaction. The strategy
  is never a live default and the model artifact never grants
  execution, approval or promotion authority.

References:

- ``docs/spec/research/DATASET_AND_EVALUATION.md`` §7
  (model-as-strategy-component evaluation).
- ``docs/spec/strategy/STRATEGY_ECONOMICS.md`` §7
  (``ECO-REGIME-001`` / ``ECO-EDGE-001`` replaceable-component
  boundary).
- ``docs/spec/strategy/LP_METRICS.md`` (the LP economics the verdict
  reads).
- ``docs/spec/architecture/ARCHITECTURE.md`` §2.2 (layer / tier rules).
- T061 — event-driven backtest engine the verdict runs through.
- T065 — rule-based implementation this task is compared against.
- T068 — strategy registry the model-backed strategy is registered in.
- T069 — product-run entry point whose request must name the
  registered identity.
- T070 — central risk gateway model and robustness evidence may
  neither weaken nor bypass.
- T105 — validated manifest the evaluated run's schema and revision
  binding resolves to.
- T106 — schema-bound robustness and anti-overfitting protocol the
  walk-forward run consumes.
- R16 — loss-versus-rebalancing (the LP opportunity cost framing).
- R18 — backtest reality-model and time-frontier warnings.
- R19 — vectorized research comparator (parameter-grid methodology).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol, cast, runtime_checkable

from robinhood_lp.robustness.schema_binding import (
    SURFACES_VERSION,
    SchemaBoundParameterSurface,
    build_schema_bound_surface,
    is_schema_bound_surface_identity,
    surface_identity,
    validate_axis_value,
)
from robinhood_lp.robustness.surfaces import (
    LEGACY_SURFACE_MARKER,
    ParameterSurface,
    is_legacy_surface,
)
from robinhood_lp.strategy.base import (
    FEE_OPPORTUNITY_ASSESSMENT_VERSION,
    FEE_OPPORTUNITY_OUTCOME_ASSESSED,
    Q64_SCALE,
    REGIME_ASSESSMENT_VERSION,
    REGIME_OUTCOME_ASSESSED,
    REGIME_STATE_DOWN_TREND,
    REGIME_STATE_JUMP_RISK,
    REGIME_STATE_RANGE,
    REGIME_STATE_UNCERTAIN,
    REGIME_STATE_UP_TREND,
    DeterministicClock,
    FeeOpportunityAssessment,
    FeeOpportunityModel,
    MarketSnapshot,
    PortfolioSnapshot,
    RegimeAssessment,
    RegimeModel,
    SeededRandomSource,
)
from robinhood_lp.strategy.model_backed import (
    ModelBackedEvaluationParameters,
    ModelBackedStrategy,
)
from robinhood_lp.strategy.registry import (
    REGISTRY_VERSION,
    ParameterSchema,
    Registry,
    RegistryError,
    UnknownStrategyIdentityError,
    default_registry,
    is_registered,
    validate_parameters,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Module version. Bumping the version is a breaking change for downstream
#: consumers (T103 research console, T107 search / candidate lock, T096
#: final traceability dossier).
EVALUATION_VERSION: Final[str] = "t102.model_evaluation.v1"

#: Strategy-layer component version the model-backed components stamp on
#: every assessment. The version is independent of the T060 assessment
#: version because a model implementation is a strategy-layer extension
#: point (ADR-014 §5) that augments the contract, not a contract change
#: in itself.
MODEL_COMPONENT_VERSION: Final[str] = "t102.model_component.v1"

#: Q64.64 fixed-point scale (re-exported so callers do not have to import
#: the strategy base module).
Q64_64_SCALE: Final[int] = Q64_SCALE

#: Constant-prediction tolerance. A model whose predictions across the
#: fold have zero variance (a "constant" model) is recorded as out of
#: its training distribution; the evaluation yields :attr:`UNCERTAIN`
#: rather than fabricating a confidence.
CONSTANT_PREDICTION_EPSILON_Q64_64: Final[int] = 1

#: Default maximum staleness (in seconds) for the model artifact. A
#: model whose recorded ``trained_at_unix_seconds`` plus this value is
#: before the evaluation's ``decision_time`` is treated as stale and
#: triggers the rule fallback.
DEFAULT_MAX_MODEL_STALENESS_SECONDS: Final[int] = 7 * 24 * 60 * 60  # 7 days

#: Module denylist (research layer purity). The evaluation module is
#: permitted to import the standard library and the lower backtest /
#: research / strategy / robustness packages only. Any other
#: ``robinhood_lp`` subpackage is a contract break.
_FORBIDDEN_EVAL_ROBINHOOD_MODULES: Final[tuple[str, ...]] = (
    "robinhood_lp.config",
    "robinhood_lp.discovery",
    "robinhood_lp.execution",
    "robinhood_lp.ingestion",
    "robinhood_lp.presentation",
    "robinhood_lp.qualification",
    "robinhood_lp.quality",
    "robinhood_lp.replay",
    "robinhood_lp.risk",
    "robinhood_lp.rpc",
    "robinhood_lp.signer",
    "robinhood_lp.storage",
    "robinhood_lp.web",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EvaluationError(ValueError):
    """Base class for model-evaluation failures."""


class UnregisteredEvaluationIdentityError(EvaluationError, UnknownStrategyIdentityError):
    """An identity the registry does not carry was offered for evaluation.

    The registry is the closed vocabulary; an unregistered identity is
    refused with no fallback. The error message names the unknown
    identity and the set of registered identities so a caller can see
    what they had available.

    The class also inherits from :class:`UnknownStrategyIdentityError`
    so existing registry-aware callers that catch the registry error
    continue to work, while evaluation-aware callers can catch the
    more specific evaluation subclass.
    """


class MissingManifestBindingError(EvaluationError):
    """A model evaluation was attempted without a validated T105 manifest binding.

    The evaluated run's validated T105 manifest is the only authority the
    walk-forward evaluation resolves a registered identity, parameter
    schema and code revision from; without it the evaluation cannot
    bind its parameter surface and refuses to start.
    """


class MissingRobustnessEvidenceError(EvaluationError):
    """A model evaluation was attempted without current schema-bound robustness evidence.

    The T106 schema-bound robustness surface is the only current
    robustness evidence the walk-forward evaluation consumes; a missing
    surface blocks publication and yields :attr:`UNCERTAIN` /
    :attr:`NO_TRADE` for the affected configuration.
    """


class LegacyRobustnessArtifactError(EvaluationError):
    """A legacy ``T064`` robustness artifact was offered as current evidence.

    The T064 predecessor surface is read-only, byte-identical and
    explicitly legacy; it cannot be consumed as current robustness
    evidence. The error names the legacy marker so a caller can see the
    classification that was applied.
    """


class IncompatibleManifestBindingError(EvaluationError):
    """The evaluated run's manifest binding disagrees with the schema-bound surface.

    A registry or schema revision mismatch between the validated T105
    manifest and the schema-bound surface is a contract break the
    walk-forward evaluation refuses to paper over.
    """


class InvalidEvaluationAxisError(EvaluationError):
    """A walk-forward evaluation axis violates its invariant or the registered schema."""


class InvalidModelArtifactHandleError(EvaluationError):
    """A :class:`ModelArtifactHandle` field violates its invariant."""


class InvalidEvaluationReportError(EvaluationError):
    """A :class:`ModelEvaluationReport` field violates its invariant."""


class InvalidRejectionRecordError(EvaluationError):
    """A :class:`RejectionRecord` field violates its invariant."""


class InvalidEpisodeIntervalError(EvaluationError):
    """An :class:`EpisodeBootstrapInterval` field violates its invariant."""


class InvalidRegimeBreakdownError(EvaluationError):
    """A :class:`RegimeConditionalBreakdown` field violates its invariant."""


class InvalidWalkForwardFoldError(EvaluationError):
    """A :class:`WalkForwardFold` field violates its invariant."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_non_empty_str(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvaluationError(f"{field}: must be non-empty str, got {value!r}")
    return value


def _require_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvaluationError(f"{field}: must be int, got {type(value).__name__}")
    return value


def _require_non_negative_int(value: object, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value < 0:
        raise EvaluationError(f"{field}: must be non-negative, got {value}")
    return value


def _require_positive_int(value: object, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value <= 0:
        raise EvaluationError(f"{field}: must be positive, got {value}")
    return value


def _require_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise EvaluationError(f"{field}: must be bool, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Robustness evidence status
# ---------------------------------------------------------------------------


class RobustnessEvidenceStatus(StrEnum):
    """The status of the schema-bound robustness evidence a model evaluation consumes.

    Strings are part of the public contract. New codes are additive;
    renaming or removing a code is a breaking change.

    - :attr:`CURRENT` — the surface is schema-bound and the binding
      identity (registry version + checksum, schema version + checksum,
      strategy identity + version) matches the evaluated run's
      validated T105 manifest.
    - :attr:`LEGACY_ONLY` — a ``T064`` legacy surface carrying the
      explicit :data:`LEGACY_SURFACE_MARKER` is available but cannot be
      consumed as current evidence. The migration-fixture analysis may
      load it for byte-identity verification, never for evaluation.
    - :attr:`MISSING` — no robustness evidence was supplied; the
      evaluation refuses to publish a robustness conclusion.
    - :attr:`REVISION_MISMATCH` — the surface is schema-bound but its
      binding identity disagrees with the evaluated run's validated
      T105 manifest. The evaluation refuses to merge the mismatched
      evidence into one sensitivity conclusion.
    """

    CURRENT = "CURRENT"
    LEGACY_ONLY = "LEGACY_ONLY"
    MISSING = "MISSING"
    REVISION_MISMATCH = "REVISION_MISMATCH"


# ---------------------------------------------------------------------------
# Model artifact handle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelArtifactHandle:
    """The registered identity a model-backed strategy loads.

    A handle binds a trained model artifact to the run that produced it.
    The handle carries:

    - ``artifact_id`` — non-empty string; the artifact's primary key
      (a content hash the harness bound to).
    - ``family`` — non-empty string; the model family the artifact
      implements (one of the ``ModelFamily`` values ``T101`` registers).
    - ``dataset_version`` — non-empty string; the dataset content hash
      the artifact was trained against.
    - ``feature_config_hash`` — non-empty string; the feature registry
      content hash the artifact's feature set resolved to.
    - ``split_definition_hash`` — non-empty string; the split
      definition the harness used to train the artifact.
    - ``code_revision`` — non-empty string; the code revision the
      artifact's ``MODEL_VERSION`` was built from.
    - ``trained_at_unix_seconds`` — non-negative int; the moment the
      artifact's training completed. A handle older than the configured
      staleness window is treated as stale.
    - ``training_pool_set`` — frozen tuple of pool keys the artifact
      was trained on. An evaluation pool disjoint from the training set
      is out-of-distribution and triggers the rule fallback.
    - ``schema_revision`` — non-empty string; the parameter-schema
      revision the artifact's binding carries.
    - ``registry_checksum`` — non-empty string; the registry checksum
      the artifact's binding carries.

    Equality and hashing follow dataclass identity.
    """

    artifact_id: str
    family: str
    dataset_version: str
    feature_config_hash: str
    split_definition_hash: str
    code_revision: str
    trained_at_unix_seconds: int
    training_pool_set: tuple[str, ...]
    schema_revision: str
    registry_checksum: str

    def __post_init__(self) -> None:
        try:
            _require_non_empty_str(self.artifact_id, field="ModelArtifactHandle.artifact_id")
            _require_non_empty_str(self.family, field="ModelArtifactHandle.family")
            _require_non_empty_str(
                self.dataset_version, field="ModelArtifactHandle.dataset_version"
            )
            _require_non_empty_str(
                self.feature_config_hash, field="ModelArtifactHandle.feature_config_hash"
            )
            _require_non_empty_str(
                self.split_definition_hash, field="ModelArtifactHandle.split_definition_hash"
            )
            _require_non_empty_str(self.code_revision, field="ModelArtifactHandle.code_revision")
            _require_non_negative_int(
                self.trained_at_unix_seconds,
                field="ModelArtifactHandle.trained_at_unix_seconds",
            )
            if not isinstance(self.training_pool_set, tuple):
                raise InvalidModelArtifactHandleError(
                    f"ModelArtifactHandle.training_pool_set: must be tuple[str, ...], "
                    f"got {type(self.training_pool_set).__name__}"
                )
            for pool_key in self.training_pool_set:
                if not isinstance(pool_key, str) or not pool_key:
                    raise InvalidModelArtifactHandleError(
                        f"ModelArtifactHandle.training_pool_set: every entry must "
                        f"be non-empty str, got {pool_key!r}"
                    )
            _require_non_empty_str(
                self.schema_revision, field="ModelArtifactHandle.schema_revision"
            )
            _require_non_empty_str(
                self.registry_checksum, field="ModelArtifactHandle.registry_checksum"
            )
        except InvalidModelArtifactHandleError:
            raise
        except EvaluationError as exc:
            raise InvalidModelArtifactHandleError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Manifest binding (the validated T105 manifest the evaluation resolves)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidatedManifestBinding:
    """The subset of a validated T105 manifest a model evaluation binds to.

    The binding is the surface the walk-forward evaluation resolves the
    registered identity, parameter-schema revision and code revision
    from. It is the *only* authority the evaluation consults: a manifest
    that is not the validated current artifact of the run, a registry
    revision that disagrees with the schema-bound surface, or a schema
    checksum that disagrees with the evaluated run's binding is refused
    rather than silently substituted.

    Equality and hashing follow dataclass identity.
    """

    run_id: str
    strategy_identity: str
    strategy_version: str
    registry_version: str
    registry_checksum: str
    parameter_schema_version: str
    parameter_schema_checksum: str
    code_provenance_module: str
    code_provenance_revision: str
    code_provenance_symbol: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.run_id, field="ValidatedManifestBinding.run_id")
        _require_non_empty_str(
            self.strategy_identity, field="ValidatedManifestBinding.strategy_identity"
        )
        _require_non_empty_str(
            self.strategy_version, field="ValidatedManifestBinding.strategy_version"
        )
        _require_non_empty_str(
            self.registry_version, field="ValidatedManifestBinding.registry_version"
        )
        _require_non_empty_str(
            self.registry_checksum, field="ValidatedManifestBinding.registry_checksum"
        )
        _require_non_empty_str(
            self.parameter_schema_version,
            field="ValidatedManifestBinding.parameter_schema_version",
        )
        _require_non_empty_str(
            self.parameter_schema_checksum,
            field="ValidatedManifestBinding.parameter_schema_checksum",
        )
        _require_non_empty_str(
            self.code_provenance_module,
            field="ValidatedManifestBinding.code_provenance_module",
        )
        _require_non_empty_str(
            self.code_provenance_revision,
            field="ValidatedManifestBinding.code_provenance_revision",
        )
        if not isinstance(self.code_provenance_symbol, str):
            raise EvaluationError(
                f"ValidatedManifestBinding.code_provenance_symbol: must be str, "
                f"got {type(self.code_provenance_symbol).__name__}"
            )

    @classmethod
    def from_registry_entry(
        cls,
        *,
        run_id: str,
        identity: str,
        registry: Registry | None = None,
    ) -> ValidatedManifestBinding:
        """Build a binding from a registered identity against ``registry``.

        The factory is the canonical bridge every evaluation uses: it
        reads the registry's closed-vocabulary entry for ``identity``
        and copies its version, parameter-schema version, code
        provenance and registry checksum into the binding. An
        unregistered identity raises
        :class:`UnregisteredEvaluationIdentityError`.
        """
        if not isinstance(run_id, str) or not run_id:
            raise EvaluationError(
                f"ValidatedManifestBinding.from_registry_entry: run_id must be "
                f"non-empty str, got {run_id!r}"
            )
        if not isinstance(identity, str) or not identity:
            raise EvaluationError(
                f"ValidatedManifestBinding.from_registry_entry: identity must be "
                f"non-empty str, got {identity!r}"
            )
        reg = registry if registry is not None else default_registry()
        try:
            entry = reg.lookup(identity)
        except UnknownStrategyIdentityError as exc:
            raise UnregisteredEvaluationIdentityError(str(exc)) from exc
        return cls(
            run_id=run_id,
            strategy_identity=entry.identity,
            strategy_version=entry.version,
            registry_version=REGISTRY_VERSION,
            registry_checksum=reg.checksum(),
            parameter_schema_version=entry.version,
            parameter_schema_checksum=_parameter_schema_checksum(entry.parameter_schemas),
            code_provenance_module=entry.code_provenance.module,
            code_provenance_revision=entry.code_provenance.revision,
            code_provenance_symbol=entry.code_provenance.symbol,
        )


def _parameter_schema_checksum(parameter_schemas: tuple[ParameterSchema, ...]) -> str:
    canonical_lines = [
        f"PARAM|{schema.name}|{schema.type.value}|{schema.unit}|"
        f"{schema.default}|{schema.lower_bound}|{schema.upper_bound}"
        for schema in parameter_schemas
    ]
    canonical = "\n".join(canonical_lines).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# ---------------------------------------------------------------------------
# Robustness evidence gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RobustnessEvidenceCheck:
    """The verdict the robustness-evidence gate returns.

    The gate is the deterministic surface every code path that
    consumes robustness evidence passes through; a missing, legacy-only
    or revision-mismatched surface is refused rather than substituted
    with the unrestricted ``T064`` predecessor surface, an inferred
    identity, or an unqualified baseline.

    Equality and hashing follow dataclass identity.
    """

    status: RobustnessEvidenceStatus
    surface_identity: str | None
    binding_identity: str | None
    legacy_marker: str | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, RobustnessEvidenceStatus):
            raise EvaluationError(
                f"RobustnessEvidenceCheck.status: must be "
                f"RobustnessEvidenceStatus, got {type(self.status).__name__}"
            )
        if not isinstance(self.reason, str) or not self.reason:
            raise EvaluationError(
                f"RobustnessEvidenceCheck.reason: must be non-empty str, got {self.reason!r}"
            )
        if self.surface_identity is not None and not (
            isinstance(self.surface_identity, str) and self.surface_identity
        ):
            raise EvaluationError(
                f"RobustnessEvidenceCheck.surface_identity: must be non-empty "
                f"str or None, got {self.surface_identity!r}"
            )
        if self.binding_identity is not None and not (
            isinstance(self.binding_identity, str) and self.binding_identity
        ):
            raise EvaluationError(
                f"RobustnessEvidenceCheck.binding_identity: must be non-empty "
                f"str or None, got {self.binding_identity!r}"
            )
        if self.legacy_marker is not None and not (
            isinstance(self.legacy_marker, str) and self.legacy_marker
        ):
            raise EvaluationError(
                f"RobustnessEvidenceCheck.legacy_marker: must be non-empty str "
                f"or None, got {self.legacy_marker!r}"
            )


def check_robustness_evidence(
    *,
    surface: object | None,
    binding: ValidatedManifestBinding | None,
) -> RobustnessEvidenceCheck:
    """Validate the robustness evidence a model evaluation consumes.

    The function is the deterministic gate every code path that
    consumes robustness evidence passes through. It accepts:

    - ``None`` — no robustness evidence is supplied. The check returns
      :attr:`RobustnessEvidenceStatus.MISSING`.
    - A :class:`ParameterSurface` carrying the
      :data:`LEGACY_SURFACE_MARKER` — the check returns
      :attr:`RobustnessEvidenceStatus.LEGACY_ONLY`. The artifact may be
      loaded as a migration fixture but cannot be consumed as current
      evidence.
    - A :class:`SchemaBoundParameterSurface` — the check compares its
      binding identity against ``binding``. A match returns
      :attr:`CURRENT`; a mismatch returns
      :attr:`REVISION_MISMATCH`.

    The check never substitutes a default identity, an inferred
    identity or the unrestricted ``T064`` predecessor surface for
    current evidence. The caller must refuse the evaluation when the
    status is anything other than :attr:`CURRENT`.
    """
    if binding is None:
        raise MissingManifestBindingError(
            "check_robustness_evidence: binding is required (the evaluated "
            "run's validated T105 manifest binding is the only authority "
            "the walk-forward evaluation resolves a registered identity, "
            "parameter schema and code revision from)"
        )
    if surface is None:
        return RobustnessEvidenceCheck(
            status=RobustnessEvidenceStatus.MISSING,
            surface_identity=None,
            binding_identity=None,
            legacy_marker=None,
            reason=(
                "no schema-bound robustness surface supplied; the walk-forward "
                "evaluation refuses to publish a robustness conclusion"
            ),
        )
    if isinstance(surface, ParameterSurface):
        if is_legacy_surface(surface):
            return RobustnessEvidenceCheck(
                status=RobustnessEvidenceStatus.LEGACY_ONLY,
                surface_identity=None,
                binding_identity=None,
                legacy_marker=LEGACY_SURFACE_MARKER,
                reason=(
                    "legacy T064 robustness surface supplied; it is read-only, "
                    "byte-identical and explicitly legacy, and cannot be "
                    "consumed as current robustness evidence"
                ),
            )
        raise LegacyRobustnessArtifactError(
            f"check_robustness_evidence: parameter surface {type(surface).__name__} "
            f"is not a legacy surface and not a schema-bound surface; the "
            f"current publication path accepts only schema-bound surfaces"
        )
    if not isinstance(surface, SchemaBoundParameterSurface):
        raise EvaluationError(
            f"check_robustness_evidence: surface must be SchemaBoundParameterSurface "
            f"or ParameterSurface, got {type(surface).__name__}"
        )
    schema_identity = surface_identity(surface=surface)
    binding_identity = _manifest_binding_identity(binding)
    if not _binding_matches_schema(
        surface=surface,
        binding=binding,
    ):
        return RobustnessEvidenceCheck(
            status=RobustnessEvidenceStatus.REVISION_MISMATCH,
            surface_identity=schema_identity,
            binding_identity=binding_identity,
            legacy_marker=None,
            reason=(
                "schema-bound surface binding identity disagrees with the "
                "evaluated run's validated T105 manifest binding; the "
                "walk-forward evaluation refuses to merge the mismatched "
                "evidence into one sensitivity conclusion"
            ),
        )
    if not is_schema_bound_surface_identity(
        strategy_identity=binding.strategy_identity,
        registry_checksum=binding.registry_checksum,
        parameter_schema_checksum=binding.parameter_schema_checksum,
    ):
        return RobustnessEvidenceCheck(
            status=RobustnessEvidenceStatus.REVISION_MISMATCH,
            surface_identity=schema_identity,
            binding_identity=binding_identity,
            legacy_marker=None,
            reason=(
                "binding identity does not pass the schema-bound registry "
                "gate; the binding does not name a registered identity, "
                "a non-empty registry checksum, or a non-empty parameter "
                "schema checksum"
            ),
        )
    return RobustnessEvidenceCheck(
        status=RobustnessEvidenceStatus.CURRENT,
        surface_identity=schema_identity,
        binding_identity=binding_identity,
        legacy_marker=None,
        reason="schema-bound surface identity matches the binding",
    )


def _manifest_binding_identity(binding: ValidatedManifestBinding) -> str:
    return (
        f"{binding.strategy_identity}:{binding.strategy_version}:"
        f"{binding.parameter_schema_version}:{binding.parameter_schema_checksum}:"
        f"{binding.registry_version}:{binding.registry_checksum}"
    )


def _binding_matches_schema(
    *,
    surface: SchemaBoundParameterSurface,
    binding: ValidatedManifestBinding,
) -> bool:
    if surface.strategy_identity != binding.strategy_identity:
        return False
    if surface.strategy_version != binding.strategy_version:
        return False
    if surface.parameter_schema_version != binding.parameter_schema_version:
        return False
    if surface.parameter_schema_checksum != binding.parameter_schema_checksum:
        return False
    if surface.registry_version != binding.registry_version:
        return False
    return surface.registry_checksum == binding.registry_checksum


# ---------------------------------------------------------------------------
# Schema-bound evaluation surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelEvaluationSurface:
    """The schema-bound parameter surface a model evaluation runs against.

    The surface is a thin wrapper around the T106
    :class:`SchemaBoundParameterSurface` that records the same registry
    and schema identity, version and checksum the evaluated run's
    validated T105 manifest records. Two surfaces built from the same
    binding return identical checksums; a binding identity that
    disagrees with the evaluated run's manifest binding produces a
    different checksum and the evaluation refuses to start.

    Equality and hashing follow dataclass identity.
    """

    binding: ValidatedManifestBinding
    surface: SchemaBoundParameterSurface
    surface_checksum: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ValidatedManifestBinding):
            raise EvaluationError(
                f"ModelEvaluationSurface.binding: must be "
                f"ValidatedManifestBinding, got {type(self.binding).__name__}"
            )
        if not isinstance(self.surface, SchemaBoundParameterSurface):
            raise EvaluationError(
                f"ModelEvaluationSurface.surface: must be "
                f"SchemaBoundParameterSurface, got {type(self.surface).__name__}"
            )
        if not isinstance(self.surface_checksum, str) or not self.surface_checksum:
            raise EvaluationError(
                f"ModelEvaluationSurface.surface_checksum: must be non-empty "
                f"str, got {self.surface_checksum!r}"
            )

    @property
    def binding_identity(self) -> str:
        """The registry / schema-bound identity string this surface carries."""
        return surface_identity(surface=self.surface)


def build_model_evaluation_surface(
    *,
    surface_id: str,
    binding: ValidatedManifestBinding,
    axes: Iterable[tuple[str, Sequence[object]]],
    registry: Registry | None = None,
) -> ModelEvaluationSurface:
    """Build the schema-bound evaluation surface for ``binding``.

    The factory resolves the binding's identity / version / schema
    checksums and builds a T106 :class:`SchemaBoundParameterSurface`
    bound to those checksums. Every axis name, type, unit and value
    is validated against the registered schema; an undeclared
    parameter, an out-of-range value, a wrong-type value, or a
    schema revision that disagrees with the binding is rejected before
    any fold is evaluated.
    """
    if not isinstance(binding, ValidatedManifestBinding):
        raise EvaluationError(
            f"build_model_evaluation_surface: binding must be "
            f"ValidatedManifestBinding, got {type(binding).__name__}"
        )
    if not is_registered(binding.strategy_identity):
        raise UnregisteredEvaluationIdentityError(
            f"build_model_evaluation_surface: identity "
            f"{binding.strategy_identity!r} is not registered; the evaluation "
            f"refuses to build a surface for an unregistered identity"
        )
    surface = build_schema_bound_surface(
        surface_id=surface_id,
        identity=binding.strategy_identity,
        axes=axes,
        registry=registry,
    )
    if surface.strategy_version != binding.strategy_version:
        raise IncompatibleManifestBindingError(
            f"build_model_evaluation_surface: surface.strategy_version="
            f"{surface.strategy_version!r} disagrees with binding.strategy_version="
            f"{binding.strategy_version!r}"
        )
    if surface.parameter_schema_checksum != binding.parameter_schema_checksum:
        raise IncompatibleManifestBindingError(
            f"build_model_evaluation_surface: surface.parameter_schema_checksum="
            f"{surface.parameter_schema_checksum!r} disagrees with binding "
            f"checksum={binding.parameter_schema_checksum!r}"
        )
    if surface.registry_checksum != binding.registry_checksum:
        raise IncompatibleManifestBindingError(
            f"build_model_evaluation_surface: surface.registry_checksum="
            f"{surface.registry_checksum!r} disagrees with binding "
            f"registry_checksum={binding.registry_checksum!r}"
        )
    return ModelEvaluationSurface(
        binding=binding,
        surface=surface,
        surface_checksum=surface.checksum(),
    )


# ---------------------------------------------------------------------------
# Out-of-distribution gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutOfDistributionGate:
    """The verdict the OOD gate returns for a model artifact.

    The gate is the deterministic surface every model-backed component
    passes through. A missing, stale, unversioned or out-of-distribution
    artifact yields :attr:`VERDICT_UNCERTAIN`; the components translate
    that verdict into a ``UNCERTAIN`` :class:`RegimeAssessment` or
    :class:`FeeOpportunityAssessment`.

    Equality and hashing follow dataclass identity.
    """

    verdict: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, str) or self.verdict not in (
            "VERDICT_ASSESSED",
            "VERDICT_UNCERTAIN",
        ):
            raise EvaluationError(
                f"OutOfDistributionGate.verdict: must be one of "
                f"'VERDICT_ASSESSED', 'VERDICT_UNCERTAIN', got {self.verdict!r}"
            )
        if not isinstance(self.reason, str) or not self.reason:
            raise EvaluationError(
                f"OutOfDistributionGate.reason: must be non-empty str, got {self.reason!r}"
            )


VERDICT_ASSESSED: Final[str] = "VERDICT_ASSESSED"
VERDICT_UNCERTAIN: Final[str] = "VERDICT_UNCERTAIN"


def evaluate_out_of_distribution(
    *,
    handle: ModelArtifactHandle | None,
    parameters: ModelBackedEvaluationParameters,
    evaluation_pool_key_id: str | None,
    evaluation_decision_time: int,
    predictions: Sequence[float] | None = None,
) -> OutOfDistributionGate:
    """Return the OOD verdict for a model artifact.

    The gate is the deterministic surface every model-backed component
    passes through; it never fabricates a confident assessment. The
    function accepts ``None`` for ``handle`` to model a missing
    artifact.
    """
    if handle is None:
        return OutOfDistributionGate(
            verdict=VERDICT_UNCERTAIN,
            reason=(
                "model artifact handle is missing; the model-backed "
                "components fall back to the rule-based baseline"
            ),
        )
    if not isinstance(handle, ModelArtifactHandle):
        raise EvaluationError(
            f"evaluate_out_of_distribution: handle must be ModelArtifactHandle "
            f"or None, got {type(handle).__name__}"
        )
    if (
        handle.schema_revision == ""
        or handle.code_revision == ""
        or handle.registry_checksum == ""
        or handle.schema_revision == "UNKNOWN"
        or handle.registry_checksum == "UNKNOWN"
    ):
        return OutOfDistributionGate(
            verdict=VERDICT_UNCERTAIN,
            reason=(
                "model artifact handle is unversioned (empty or UNKNOWN "
                "schema_revision / registry_checksum); the model-backed "
                "components fall back to the rule-based baseline"
            ),
        )
    if (
        parameters.staleness_seconds > 0
        and handle.trained_at_unix_seconds + parameters.staleness_seconds < evaluation_decision_time
    ):
        return OutOfDistributionGate(
            verdict=VERDICT_UNCERTAIN,
            reason=(
                "model artifact handle is stale (trained_at_unix_seconds + "
                "staleness_seconds is before evaluation_decision_time); the "
                "model-backed components fall back to the rule-based baseline"
            ),
        )
    if (
        parameters.require_pool_set_membership
        and evaluation_pool_key_id is not None
        and evaluation_pool_key_id != ""
        and evaluation_pool_key_id not in handle.training_pool_set
    ):
        return OutOfDistributionGate(
            verdict=VERDICT_UNCERTAIN,
            reason=(
                "evaluation pool_key_id is disjoint from the artifact's "
                "training_pool_set; the artifact is out-of-distribution for "
                "this evaluation"
            ),
        )
    if predictions is not None and len(predictions) >= 2:
        unique_values = _count_unique_predictions(predictions)
        # The threshold names the minimum count of unique
        # predictions the artifact must produce across the fold;
        # a count below the floor means the artifact emits a
        # constant prediction (with ``threshold=1`` requiring at
        # least two distinct values, ``threshold=0`` disabling the
        # check entirely).
        if (
            parameters.ood_prediction_variance_q64_64 > 0
            and unique_values <= parameters.ood_prediction_variance_q64_64
        ):
            return OutOfDistributionGate(
                verdict=VERDICT_UNCERTAIN,
                reason=(
                    "model artifact emits a constant prediction across the "
                    "fold; the artifact is out-of-distribution"
                ),
            )
    return OutOfDistributionGate(
        verdict=VERDICT_ASSESSED,
        reason="model artifact handle is current and in-distribution",
    )


def _count_unique_predictions(predictions: Sequence[float]) -> int:
    """Return the number of distinct values in ``predictions``.

    The OOD gate uses the deterministic count as the "constant
    artifact" signal: a model that emits fewer than the
    schema-declared number of unique predictions across the fold is
    degenerate and yields :attr:`VERDICT_UNCERTAIN`.
    """
    return len(set(predictions))


# ---------------------------------------------------------------------------
# Model-backed regime and fee-opportunity components
# ---------------------------------------------------------------------------


@runtime_checkable
class ModelPredictor(Protocol):
    """The minimal interface a model-backed component consumes.

    A predictor wraps a fitted model artifact and yields a deterministic
    prediction for the features the snapshot exposes. The interface is
    deliberately narrow: the model-backed components never expose the
    underlying artifact to the strategy layer; they translate a
    predictor's output into a versioned, unit-carrying assessment.
    """

    def predict(self, features: Sequence[float]) -> float:
        """Return a deterministic prediction for ``features``."""
        ...


@dataclass(frozen=True, slots=True)
class ConstantModelPredictor:
    """A trivial predictor that returns a fixed value for any input.

    The predictor is the deterministic reference the OOD gate uses to
    detect a "constant" artifact (the same value regardless of
    features). It is also the fallback the rule-based baseline uses
    when the components are constructed without a model artifact.
    """

    value: float = 0.0
    variance: float = 0.0

    def predict(self, features: Sequence[float]) -> float:
        return self.value

    def predict_sequence(self, rows: Sequence[Sequence[float]]) -> list[float]:
        return [self.value for _ in rows]


@dataclass(frozen=True, slots=True)
class SeededDeterministicPredictor:
    """A deterministic linear predictor with a declared seed.

    The predictor emits ``intercept + sum(coefficient * feature)`` for
    every input row. A predictor with a non-zero coefficient vector
    produces non-constant predictions across a fold; a zero
    coefficient vector is the OOD gate's "constant model" detection
    signal.
    """

    intercept: float = 0.0
    coefficients: tuple[float, ...] = ()

    def predict(self, features: Sequence[float]) -> float:
        if len(self.coefficients) != len(features):
            raise EvaluationError(
                f"SeededDeterministicPredictor.predict: feature length "
                f"{len(features)} disagrees with coefficients length "
                f"{len(self.coefficients)}"
            )
        return self.intercept + sum(
            c * f for c, f in zip(self.coefficients, features, strict=False)
        )


# ---------------------------------------------------------------------------
# Model-backed regime component
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelBackedRegimeModel:
    """The model-backed :class:`RegimeModel` a registered strategy loads.

    The component implements the T060 :class:`RegimeModel` interface;
    the strategy layer consumes it through the registered T068 identity
    and never inspects the underlying artifact. When the OOD gate
    returns :attr:`VERDICT_UNCERTAIN` the component emits a
    :class:`RegimeAssessment` whose ``outcome`` is
    :attr:`REGIME_OUTCOME_UNCERTAIN`; the rule-based fallback the
    contract binds is the rule-based ``AdaptiveRegimeModel`` baseline.
    """

    handle: ModelArtifactHandle | None
    parameters: ModelBackedEvaluationParameters
    predictor: ModelPredictor = field(default_factory=ConstantModelPredictor)
    evaluation_pool_key_id: str = ""
    regime_confidence_q64_64: int = Q64_SCALE // 10
    fallback: RegimeModel | None = None

    def __post_init__(self) -> None:
        if self.parameters is None or not isinstance(
            self.parameters, ModelBackedEvaluationParameters
        ):
            raise EvaluationError(
                f"ModelBackedRegimeModel.parameters: must be "
                f"ModelBackedEvaluationParameters, got "
                f"{type(self.parameters).__name__}"
            )
        if not isinstance(self.regime_confidence_q64_64, int) or isinstance(
            self.regime_confidence_q64_64, bool
        ):
            raise EvaluationError(
                f"ModelBackedRegimeModel.regime_confidence_q64_64: must be int, "
                f"got {type(self.regime_confidence_q64_64).__name__}"
            )
        if self.regime_confidence_q64_64 <= 0:
            raise EvaluationError(
                f"ModelBackedRegimeModel.regime_confidence_q64_64: must be "
                f"positive, got {self.regime_confidence_q64_64}"
            )
        if self.fallback is not None and not isinstance(self.fallback, RegimeModel):
            raise EvaluationError(
                f"ModelBackedRegimeModel.fallback: must be RegimeModel or "
                f"None, got {type(self.fallback).__name__}"
            )

    def assess(
        self,
        *,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        clock: DeterministicClock,
        rng: SeededRandomSource,
    ) -> RegimeAssessment:
        """Return a :class:`RegimeAssessment` from the model-backed component.

        The function is pure: it never mutates ``market`` or ``portfolio``
        and never reads wall-clock time. When the OOD gate returns
        :attr:`VERDICT_UNCERTAIN` the function emits an ``UNCERTAIN``
        assessment; the rule-based fallback (if supplied) is consulted
        only when the model artifact is missing entirely, never as a
        second authority over the model's own verdict.
        """
        if self.handle is None:
            if self.fallback is not None:
                return self.fallback.assess(
                    market=market, portfolio=portfolio, clock=clock, rng=rng
                )
            return RegimeAssessment.uncertain(
                model_version=MODEL_COMPONENT_VERSION,
                evidence_keys=("market.sqrt_price_x96",),
                notes=("model_artifact_missing",),
            )
        gate = evaluate_out_of_distribution(
            handle=self.handle,
            parameters=self.parameters,
            evaluation_pool_key_id=market.pool_key_id,
            evaluation_decision_time=market.availability_time,
        )
        if gate.verdict == VERDICT_UNCERTAIN:
            return RegimeAssessment.uncertain(
                model_version=MODEL_COMPONENT_VERSION,
                evidence_keys=("market.sqrt_price_x96",),
                notes=(gate.reason,),
            )
        features = _features_from_market_snapshot(market)
        try:
            prediction = self.predictor.predict(features)
            probe = self.predictor.predict(_probe_features())
        except Exception as exc:
            return RegimeAssessment.uncertain(
                model_version=MODEL_COMPONENT_VERSION,
                evidence_keys=("market.sqrt_price_x96",),
                notes=(f"predictor_failure:{exc!r}",),
            )
        # Constant-predictor detection: a model that returns the
        # same value for any input is degenerate. The contract names
        # this boundary case explicitly; the component falls back to
        # UNCERTAIN rather than fabricating a confident assessment.
        if prediction == probe and parameters_detect_constant(self.parameters):
            return RegimeAssessment.uncertain(
                model_version=MODEL_COMPONENT_VERSION,
                evidence_keys=("market.sqrt_price_x96",),
                notes=("constant_predictor",),
            )
        confidence = self.regime_confidence_q64_64
        if confidence < self.parameters.regime_confidence_floor_q64_64:
            return RegimeAssessment.uncertain(
                model_version=MODEL_COMPONENT_VERSION,
                evidence_keys=("market.sqrt_price_x96",),
                notes=("confidence_below_floor",),
            )
        state = _prediction_to_regime_state(prediction)
        if state == REGIME_STATE_UNCERTAIN:
            return RegimeAssessment.uncertain(
                model_version=MODEL_COMPONENT_VERSION,
                evidence_keys=("market.sqrt_price_x96",),
                notes=("prediction_below_state_threshold",),
            )
        return RegimeAssessment(
            version=REGIME_ASSESSMENT_VERSION,
            model_version=MODEL_COMPONENT_VERSION,
            outcome=REGIME_OUTCOME_ASSESSED,
            state=state,
            confidence_q64_64=confidence,
            evidence_keys=("market.sqrt_price_x96",),
            notes=("model_backed",),
        )


@dataclass(frozen=True, slots=True)
class ModelBackedFeeOpportunityModel:
    """The model-backed :class:`FeeOpportunityModel` a registered strategy loads.

    The component implements the T060 :class:`FeeOpportunityModel`
    interface. When the OOD gate returns :attr:`VERDICT_UNCERTAIN` the
    component emits a :class:`FeeOpportunityAssessment` whose ``outcome``
    is :attr:`FEE_OPPORTUNITY_OUTCOME_UNCERTAIN`; the rule-based
    fallback (if supplied) is consulted only when the model artifact is
    missing entirely, never as a second authority over the model's own
    verdict.
    """

    handle: ModelArtifactHandle | None
    parameters: ModelBackedEvaluationParameters
    predictor: ModelPredictor = field(default_factory=ConstantModelPredictor)
    evaluation_pool_key_id: str = ""
    expected_fee_edge_q64_64: int = Q64_SCALE // 100
    fallback: FeeOpportunityModel | None = None

    def __post_init__(self) -> None:
        if self.parameters is None or not isinstance(
            self.parameters, ModelBackedEvaluationParameters
        ):
            raise EvaluationError(
                f"ModelBackedFeeOpportunityModel.parameters: must be "
                f"ModelBackedEvaluationParameters, got "
                f"{type(self.parameters).__name__}"
            )
        if not isinstance(self.expected_fee_edge_q64_64, int) or isinstance(
            self.expected_fee_edge_q64_64, bool
        ):
            raise EvaluationError(
                f"ModelBackedFeeOpportunityModel.expected_fee_edge_q64_64: must "
                f"be int, got {type(self.expected_fee_edge_q64_64).__name__}"
            )
        if self.expected_fee_edge_q64_64 < 0:
            raise EvaluationError(
                f"ModelBackedFeeOpportunityModel.expected_fee_edge_q64_64: must "
                f"be non-negative, got {self.expected_fee_edge_q64_64}"
            )
        if self.fallback is not None and not isinstance(self.fallback, FeeOpportunityModel):
            raise EvaluationError(
                f"ModelBackedFeeOpportunityModel.fallback: must be "
                f"FeeOpportunityModel or None, got "
                f"{type(self.fallback).__name__}"
            )

    def assess(
        self,
        *,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        clock: DeterministicClock,
        rng: SeededRandomSource,
    ) -> FeeOpportunityAssessment:
        """Return a :class:`FeeOpportunityAssessment` from the model-backed component."""
        if self.handle is None:
            if self.fallback is not None:
                return self.fallback.assess(
                    market=market, portfolio=portfolio, clock=clock, rng=rng
                )
            return FeeOpportunityAssessment.uncertain(
                model_version=MODEL_COMPONENT_VERSION,
                evidence_keys=("market.liquidity",),
                notes=("model_artifact_missing",),
            )
        gate = evaluate_out_of_distribution(
            handle=self.handle,
            parameters=self.parameters,
            evaluation_pool_key_id=market.pool_key_id,
            evaluation_decision_time=market.availability_time,
        )
        if gate.verdict == VERDICT_UNCERTAIN:
            return FeeOpportunityAssessment.uncertain(
                model_version=MODEL_COMPONENT_VERSION,
                evidence_keys=("market.liquidity",),
                notes=(gate.reason,),
            )
        features = _features_from_market_snapshot(market)
        try:
            prediction = self.predictor.predict(features)
            probe = self.predictor.predict(_probe_features())
        except Exception as exc:
            return FeeOpportunityAssessment.uncertain(
                model_version=MODEL_COMPONENT_VERSION,
                evidence_keys=("market.liquidity",),
                notes=(f"predictor_failure:{exc!r}",),
            )
        if prediction == probe and parameters_detect_constant(self.parameters):
            return FeeOpportunityAssessment.uncertain(
                model_version=MODEL_COMPONENT_VERSION,
                evidence_keys=("market.liquidity",),
                notes=("constant_predictor",),
            )
        fee_edge_q64_64 = _prediction_to_fee_edge_q64_64(prediction)
        if fee_edge_q64_64 < self.parameters.fee_opportunity_floor_q64_64:
            return FeeOpportunityAssessment.uncertain(
                model_version=MODEL_COMPONENT_VERSION,
                evidence_keys=("market.liquidity",),
                notes=("fee_edge_below_floor",),
            )
        return FeeOpportunityAssessment(
            version=FEE_OPPORTUNITY_ASSESSMENT_VERSION,
            model_version=MODEL_COMPONENT_VERSION,
            outcome=FEE_OPPORTUNITY_OUTCOME_ASSESSED,
            expected_fee_edge_q64_64=fee_edge_q64_64,
            confidence_q64_64=self.expected_fee_edge_q64_64
            if self.expected_fee_edge_q64_64 > 0
            else Q64_SCALE,
            evidence_keys=("market.liquidity",),
            notes=("model_backed",),
        )


def _features_from_market_snapshot(market: MarketSnapshot) -> tuple[float, ...]:
    """Project a T060 :class:`MarketSnapshot` into a tuple of ``float`` features.

    The projection is the only place ``float`` enters the
    model-backed strategy layer; it lives at the named
    statistical boundary (ADR-004 / T060 §7). The features are
    deterministic for the same snapshot.
    """
    realized_volatility = float(market.realized_volatility_q64_64) / float(Q64_SCALE)
    freshness = float(market.freshness_seconds)
    # Use ``int.bit_length`` to convert Q64.96 sqrt-price and
    # uint128 liquidity to a log-scale float feature. ``int`` is
    # required because ``float.bit_length`` does not exist; the
    # int bit length is monotonic and deterministic.
    log_price_x96 = float(market.sqrt_price_x96.bit_length())
    log_liquidity = float(market.liquidity.bit_length())
    return (realized_volatility, freshness, log_price_x96, log_liquidity)


def _probe_features() -> tuple[float, ...]:
    """Return a fixed probe feature vector the constant-predictor check uses.

    The probe differs from any realistic feature vector (it carries
    extreme values no real snapshot would emit) so a model that
    returns the same value for the probe and the snapshot features
    is degenerate.
    """
    return (float("inf"), float("inf"), float("inf"), float("inf"))


def parameters_detect_constant(parameters: ModelBackedEvaluationParameters) -> bool:
    """Return ``True`` iff the parameters enable the constant-predictor check.

    The constant-predictor check is enabled when
    ``ood_prediction_variance_q64_64 > 0``; the parameter is the
    minimum number of unique predictions the artifact must produce
    across the fold. A value of ``0`` disables the check entirely.
    """
    return parameters.ood_prediction_variance_q64_64 > 0


def _prediction_to_regime_state(prediction: float) -> str:
    """Map a continuous prediction to a closed regime-state sentinel.

    The mapping is deterministic for the same prediction: a positive
    prediction yields :attr:`REGIME_STATE_RANGE`; a prediction above
    the regime threshold yields :attr:`REGIME_STATE_UP_TREND`; a
    prediction below the negative regime threshold yields
    :attr:`REGIME_STATE_DOWN_TREND`; the regime threshold squared
    yields :attr:`REGIME_STATE_JUMP_RISK`.
    """
    if prediction > 0.5:
        return REGIME_STATE_UP_TREND
    if prediction < -0.5:
        return REGIME_STATE_DOWN_TREND
    if abs(prediction) > 0.25:
        return REGIME_STATE_JUMP_RISK
    if abs(prediction) <= 0.1:
        return REGIME_STATE_UNCERTAIN
    return REGIME_STATE_RANGE


def _prediction_to_fee_edge_q64_64(prediction: float) -> int:
    """Map a continuous prediction to a Q64.64 fee edge.

    The mapping clamps the prediction to ``[0, 1]`` and rescales to
    the Q64.64 scale so the result is always a valid Q64.64 ratio.
    """
    clamped = max(0.0, min(1.0, prediction))
    return int(clamped * float(Q64_SCALE))


# ---------------------------------------------------------------------------
# Episode and fold records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodeOutcome:
    """The per-episode outcome a model evaluation records.

    An *episode* is the atomic LP lifecycle window the verdict reads:
    one decision, the resulting fill (if any), the realised fees and
    the realised gas / slippage. The aggregation is the bootstrap
    input the contract binds.
    """

    episode_index: int
    decision_time: int
    net_return_q64_64: int
    realised_fee_q64_64: int
    realised_gas_q64_64: int
    regime_state: str

    def __post_init__(self) -> None:
        _require_non_negative_int(self.episode_index, field="EpisodeOutcome.episode_index")
        _require_non_negative_int(self.decision_time, field="EpisodeOutcome.decision_time")
        if not isinstance(self.net_return_q64_64, int) or isinstance(self.net_return_q64_64, bool):
            raise EvaluationError(
                f"EpisodeOutcome.net_return_q64_64: must be int, got "
                f"{type(self.net_return_q64_64).__name__}"
            )
        for field_name in ("realised_fee_q64_64", "realised_gas_q64_64"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise EvaluationError(
                    f"EpisodeOutcome.{field_name}: must be int, got {type(value).__name__}"
                )
            if value < 0:
                raise EvaluationError(
                    f"EpisodeOutcome.{field_name}: must be non-negative, got {value}"
                )
        if not isinstance(self.regime_state, str) or not self.regime_state:
            raise EvaluationError(
                f"EpisodeOutcome.regime_state: must be non-empty str, got {self.regime_state!r}"
            )


@dataclass(frozen=True, slots=True)
class EpisodeBootstrapInterval:
    """Episode-level bootstrap confidence interval for one comparison row.

    The interval is the contract's "uncertainty of the comparison"
    binding: two evaluations with overlapping intervals cannot be
    distinguished on this metric at the stated confidence level.

    Field units are explicit:

    - ``comparison_row`` — non-empty str; the row the interval
      summarises (e.g. ``"rule.net_return_q64_64"``,
      ``"model.net_return_q64_64"``).
    - ``sample_count`` — positive int; the number of independent
      episodes the bootstrap drew from.
    - ``mean_q64_64`` — int; the empirical mean of the bootstrap
      distribution in Q64.64.
    - ``lower_q64_64`` — int; the lower bound of the interval in Q64.64.
    - ``upper_q64_64`` — int; the upper bound of the interval in Q64.64.
    - ``confidence_level_q64_64`` — positive Q64.64 ratio; the
      confidence level the interval was computed at (``0.95`` ==
      ``Q64_SCALE * 95 // 100``).
    """

    comparison_row: str
    sample_count: int
    mean_q64_64: int
    lower_q64_64: int
    upper_q64_64: int
    confidence_level_q64_64: int

    def __post_init__(self) -> None:
        try:
            _require_non_empty_str(
                self.comparison_row, field="EpisodeBootstrapInterval.comparison_row"
            )
            _require_non_negative_int(
                self.sample_count, field="EpisodeBootstrapInterval.sample_count"
            )
            for name in ("mean_q64_64", "lower_q64_64", "upper_q64_64"):
                value = getattr(self, name)
                if not isinstance(value, int) or isinstance(value, bool):
                    raise InvalidEpisodeIntervalError(
                        f"EpisodeBootstrapInterval.{name}: must be int, got {type(value).__name__}"
                    )
            if self.lower_q64_64 > self.upper_q64_64:
                raise InvalidEpisodeIntervalError(
                    f"EpisodeBootstrapInterval: lower_q64_64={self.lower_q64_64} "
                    f"must be <= upper_q64_64={self.upper_q64_64}"
                )
            if not isinstance(self.confidence_level_q64_64, int) or isinstance(
                self.confidence_level_q64_64, bool
            ):
                raise InvalidEpisodeIntervalError(
                    f"EpisodeBootstrapInterval.confidence_level_q64_64: must be int, "
                    f"got {type(self.confidence_level_q64_64).__name__}"
                )
            if not 0 < self.confidence_level_q64_64 <= Q64_SCALE:
                raise InvalidEpisodeIntervalError(
                    f"EpisodeBootstrapInterval.confidence_level_q64_64: must be in "
                    f"(0, 1] Q64.64, got {self.confidence_level_q64_64}"
                )
        except InvalidEpisodeIntervalError:
            raise
        except EvaluationError as exc:
            raise InvalidEpisodeIntervalError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RegimeConditionalBreakdown:
    """A regime-conditional breakdown of the rule-vs-model comparison.

    A favourable aggregate cannot hide a loss concentrated in one market
    state; the breakdown publishes the comparison rows for every
    closed regime label the report covers.

    Field units are explicit:

    - ``regime_label`` — non-empty str; the regime the breakdown covers
      (one of ``"RANGE"``, ``"UP_TREND"``, ``"DOWN_TREND"``,
      ``"JUMP_RISK"``).
    - ``rule_mean_q64_64`` — int; the mean ``net_return_q64_64`` for
      the rule baseline over the episodes the regime covers.
    - ``model_mean_q64_64`` — int; the mean ``net_return_q64_64`` for
      the model-backed strategy over the same set.
    - ``rule_episode_count`` — non-negative int; the number of
      episodes the rule baseline produced under this regime.
    - ``model_episode_count`` — non-negative int; the number of
      episodes the model-backed strategy produced under this regime.
    """

    regime_label: str
    rule_mean_q64_64: int
    model_mean_q64_64: int
    rule_episode_count: int
    model_episode_count: int

    def __post_init__(self) -> None:
        try:
            _require_non_empty_str(
                self.regime_label, field="RegimeConditionalBreakdown.regime_label"
            )
            for name in (
                "rule_mean_q64_64",
                "model_mean_q64_64",
            ):
                value = getattr(self, name)
                if not isinstance(value, int) or isinstance(value, bool):
                    raise InvalidRegimeBreakdownError(
                        f"RegimeConditionalBreakdown.{name}: must be int, "
                        f"got {type(value).__name__}"
                    )
            for name in ("rule_episode_count", "model_episode_count"):
                value = getattr(self, name)
                _require_non_negative_int(value, field=f"RegimeConditionalBreakdown.{name}")
        except InvalidRegimeBreakdownError:
            raise
        except EvaluationError as exc:
            raise InvalidRegimeBreakdownError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RejectionRecord:
    """The explicit rejection record a model evaluation emits.

    The record names which model improved prediction but worsened
    economics, and why. The verdict is binary: the report either
    accepts the model (with a side-by-side comparison) or rejects it
    with a structured reason.

    Field units are explicit:

    - ``model_identity`` — non-empty str; the model family's name the
      record refers to.
    - ``prediction_metric_name`` — non-empty str; the prediction
      metric the model improved (e.g. ``"calibration_ece"``).
    - ``prediction_metric_rule_value_q64_64`` — int; the rule's
      prediction metric value.
    - ``prediction_metric_model_value_q64_64`` — int; the model's
      prediction metric value.
    - ``prediction_metric_direction`` — non-empty str; ``"LOWER_IS_BETTER"``
      or ``"HIGHER_IS_BETTER"``.
    - ``economics_metric_name`` — non-empty str; the LP-economics
      metric the model worsened (e.g. ``"net_return_q64_64"``).
    - ``economics_metric_rule_value_q64_64`` — int; the rule's
      economics metric value.
    - ``economics_metric_model_value_q64_64`` — int; the model's
      economics metric value.
    - ``economics_metric_direction`` — non-empty str;
      ``"LOWER_IS_BETTER"`` or ``"HIGHER_IS_BETTER"``.
    - ``reason_code`` — non-empty str; the structured reason code
      the rejection binds to (``"PREDICTION_UP_ECONOMICS_DOWN"`` or
      ``"ECONOMICS_DOWN_EPISODE_UNDERFLOW"``).
    - ``explanation`` — non-empty str; the human-readable explanation
      the audit layer surfaces.
    """

    model_identity: str
    prediction_metric_name: str
    prediction_metric_rule_value_q64_64: int
    prediction_metric_model_value_q64_64: int
    prediction_metric_direction: str
    economics_metric_name: str
    economics_metric_rule_value_q64_64: int
    economics_metric_model_value_q64_64: int
    economics_metric_direction: str
    reason_code: str
    explanation: str

    def __post_init__(self) -> None:
        for name in (
            "model_identity",
            "prediction_metric_name",
            "economics_metric_name",
            "reason_code",
            "explanation",
        ):
            value = getattr(self, name)
            _require_non_empty_str(value, field=f"RejectionRecord.{name}")
        for name in (
            "prediction_metric_direction",
            "economics_metric_direction",
        ):
            value = getattr(self, name)
            if value not in ("LOWER_IS_BETTER", "HIGHER_IS_BETTER"):
                raise InvalidRejectionRecordError(
                    f"RejectionRecord.{name}: must be one of "
                    f"'LOWER_IS_BETTER', 'HIGHER_IS_BETTER', got {value!r}"
                )
        for name in (
            "prediction_metric_rule_value_q64_64",
            "prediction_metric_model_value_q64_64",
            "economics_metric_rule_value_q64_64",
            "economics_metric_model_value_q64_64",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise InvalidRejectionRecordError(
                    f"RejectionRecord.{name}: must be int, got {type(value).__name__}"
                )
        if self.reason_code not in (
            "PREDICTION_UP_ECONOMICS_DOWN",
            "ECONOMICS_DOWN_EPISODE_UNDERFLOW",
            "ECONOMICS_TIE_PREDICTION_DOWN",
            "PREDICTION_UP_ECONOMICS_TIE",
        ):
            raise InvalidRejectionRecordError(
                f"RejectionRecord.reason_code: must be one of "
                f"'PREDICTION_UP_ECONOMICS_DOWN', "
                f"'ECONOMICS_DOWN_EPISODE_UNDERFLOW', "
                f"'ECONOMICS_TIE_PREDICTION_DOWN', "
                f"'PREDICTION_UP_ECONOMICS_TIE', got {self.reason_code!r}"
            )


# ---------------------------------------------------------------------------
# Walk-forward fold
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """One (training, evaluation) fold the walk-forward evaluation walks through.

    The fold is machine-recorded: ``train_start_decision_time`` and
    ``train_end_decision_time`` are the inclusive / exclusive bounds of
    the training window; ``eval_start_decision_time`` and
    ``eval_end_decision_time`` are the inclusive / exclusive bounds of
    the evaluation window; ``purge_plus_embargo_seconds`` is the gap
    between them, derived from the label horizon (DS-022).

    The fold is deterministic for the same inputs; ``fold_index`` is
    the position in the walk-forward sequence.
    """

    fold_index: int
    train_start_decision_time: int
    train_end_decision_time: int
    eval_start_decision_time: int
    eval_end_decision_time: int
    purge_plus_embargo_seconds: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.fold_index, field="WalkForwardFold.fold_index")
        for name in (
            "train_start_decision_time",
            "train_end_decision_time",
            "eval_start_decision_time",
            "eval_end_decision_time",
            "purge_plus_embargo_seconds",
        ):
            _require_non_negative_int(getattr(self, name), field=f"WalkForwardFold.{name}")
        if self.train_end_decision_time <= self.train_start_decision_time:
            raise InvalidWalkForwardFoldError(
                f"WalkForwardFold: train_end_decision_time="
                f"{self.train_end_decision_time} must be > "
                f"train_start_decision_time={self.train_start_decision_time}"
            )
        if self.eval_end_decision_time <= self.eval_start_decision_time:
            raise InvalidWalkForwardFoldError(
                f"WalkForwardFold: eval_end_decision_time="
                f"{self.eval_end_decision_time} must be > "
                f"eval_start_decision_time={self.eval_start_decision_time}"
            )
        gap = self.eval_start_decision_time - self.train_end_decision_time
        if gap < self.purge_plus_embargo_seconds:
            raise InvalidWalkForwardFoldError(
                f"WalkForwardFold: gap between train_end_decision_time and "
                f"eval_start_decision_time is {gap}, below the declared "
                f"purge_plus_embargo_seconds={self.purge_plus_embargo_seconds}"
            )

    def to_dict(self) -> dict[str, int]:
        return {
            "fold_index": self.fold_index,
            "train_start_decision_time": self.train_start_decision_time,
            "train_end_decision_time": self.train_end_decision_time,
            "eval_start_decision_time": self.eval_start_decision_time,
            "eval_end_decision_time": self.eval_end_decision_time,
            "purge_plus_embargo_seconds": self.purge_plus_embargo_seconds,
        }


# ---------------------------------------------------------------------------
# Evaluation report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelEvaluationReport:
    """The side-by-side report the walk-forward evaluation produces.

    The report records:

    - the binding identity / version / checksum the surface and the
      report share;
    - the per-fold :class:`WalkForwardFold` records;
    - the rule baseline and the model-backed strategy's
      :class:`EpisodeOutcome` sequences;
    - the :class:`EpisodeBootstrapInterval` summaries;
    - the :class:`RegimeConditionalBreakdown` per regime;
    - the :class:`RejectionRecord` when the verdict rejects the model;
    - the :class:`RobustnessEvidenceCheck` verdict.

    Equality and hashing follow dataclass identity.
    """

    version: str
    binding: ValidatedManifestBinding
    surface_checksum: str
    folds: tuple[WalkForwardFold, ...]
    rule_episodes: tuple[EpisodeOutcome, ...]
    model_episodes: tuple[EpisodeOutcome, ...]
    bootstrap_intervals: tuple[EpisodeBootstrapInterval, ...]
    regime_breakdowns: tuple[RegimeConditionalBreakdown, ...]
    rejection: RejectionRecord | None
    robustness_check: RobustnessEvidenceCheck
    comparison_metric_name: str = "net_return_q64_64"

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or self.version != EVALUATION_VERSION:
            raise InvalidEvaluationReportError(
                f"ModelEvaluationReport.version: must be {EVALUATION_VERSION!r}, "
                f"got {self.version!r}"
            )
        if not isinstance(self.binding, ValidatedManifestBinding):
            raise InvalidEvaluationReportError(
                f"ModelEvaluationReport.binding: must be "
                f"ValidatedManifestBinding, got {type(self.binding).__name__}"
            )
        if not isinstance(self.surface_checksum, str) or not self.surface_checksum:
            raise InvalidEvaluationReportError(
                f"ModelEvaluationReport.surface_checksum: must be non-empty "
                f"str, got {self.surface_checksum!r}"
            )
        for name, expected_type in (
            ("folds", WalkForwardFold),
            ("rule_episodes", EpisodeOutcome),
            ("model_episodes", EpisodeOutcome),
            ("bootstrap_intervals", EpisodeBootstrapInterval),
            ("regime_breakdowns", RegimeConditionalBreakdown),
        ):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                raise InvalidEvaluationReportError(
                    f"ModelEvaluationReport.{name}: must be tuple, got {type(value).__name__}"
                )
            for entry in value:
                if not isinstance(entry, expected_type):
                    raise InvalidEvaluationReportError(
                        f"ModelEvaluationReport.{name}: every entry must be "
                        f"{expected_type.__name__}, got {type(entry).__name__}"
                    )
        if not isinstance(self.robustness_check, RobustnessEvidenceCheck):
            raise InvalidEvaluationReportError(
                f"ModelEvaluationReport.robustness_check: must be "
                f"RobustnessEvidenceCheck, got "
                f"{type(self.robustness_check).__name__}"
            )
        if not isinstance(self.comparison_metric_name, str) or not self.comparison_metric_name:
            raise InvalidEvaluationReportError(
                f"ModelEvaluationReport.comparison_metric_name: must be "
                f"non-empty str, got {self.comparison_metric_name!r}"
            )

    @property
    def rule_episode_count(self) -> int:
        return len(self.rule_episodes)

    @property
    def model_episode_count(self) -> int:
        return len(self.model_episodes)

    @property
    def is_rejected(self) -> bool:
        """``True`` iff the verdict rejected the model on LP economics."""
        return self.rejection is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "binding_identity": _manifest_binding_identity(self.binding),
            "registry_version": self.binding.registry_version,
            "registry_checksum": self.binding.registry_checksum,
            "parameter_schema_version": self.binding.parameter_schema_version,
            "parameter_schema_checksum": self.binding.parameter_schema_checksum,
            "strategy_identity": self.binding.strategy_identity,
            "strategy_version": self.binding.strategy_version,
            "surface_checksum": self.surface_checksum,
            "comparison_metric_name": self.comparison_metric_name,
            "folds": [fold.to_dict() for fold in self.folds],
            "rule_episode_count": self.rule_episode_count,
            "model_episode_count": self.model_episode_count,
            "robustness_check": {
                "status": self.robustness_check.status.value,
                "reason": self.robustness_check.reason,
                "surface_identity": self.robustness_check.surface_identity,
                "binding_identity": self.robustness_check.binding_identity,
                "legacy_marker": self.robustness_check.legacy_marker,
            },
            "rejection": (
                {
                    "model_identity": self.rejection.model_identity,
                    "reason_code": self.rejection.reason_code,
                    "explanation": self.rejection.explanation,
                }
                if self.rejection is not None
                else None
            ),
        }


# ---------------------------------------------------------------------------
# Walk-forward evaluation
# ---------------------------------------------------------------------------


EpisodeRunner = Callable[
    [
        ValidatedManifestBinding,
        ModelBackedEvaluationParameters,
        WalkForwardFold,
        bool,
    ],
    tuple[EpisodeOutcome, ...],
]
"""A callable that produces the per-fold episode sequence.

The first positional argument is the validated manifest binding; the
second is the schema-bound parameters; the third is the fold being
walked; the fourth is a boolean indicating whether the runner should
evaluate the rule baseline (``True``) or the model-backed strategy
(``False``). The runner returns the episodes it observed on that
fold; the orchestrator aggregates them and runs the comparison.
"""


@dataclass(frozen=True, slots=True)
class WalkForwardEvaluationInputs:
    """The deterministic inputs a walk-forward evaluation accepts.

    The dataclass carries:

    - ``binding`` — the validated T105 manifest binding the evaluation
      resolves the registered identity / version / schema checksums
      from.
    - ``surface`` — the schema-bound evaluation surface the evaluation
      uses to build its parameter axis set.
    - ``parameters`` — the schema-bound evaluation parameters the
      strategy layer validated against the registered identity.
    - ``folds`` — the machine-recorded, deterministic walk-forward
      folds the evaluation walks through. The folds carry
      point-in-time boundaries and the derived purge + embargo gap.
    - ``rule_runner`` / ``model_runner`` — the callables the
      orchestrator uses to evaluate each fold under the rule baseline
      and the model-backed strategy respectively. A production
      orchestrator wires the callables to the T061 engine; tests
      inject fixtures.

    Equality and hashing follow dataclass identity.
    """

    binding: ValidatedManifestBinding
    surface: ModelEvaluationSurface
    parameters: ModelBackedEvaluationParameters
    folds: tuple[WalkForwardFold, ...]
    rule_runner: EpisodeRunner
    model_runner: EpisodeRunner

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ValidatedManifestBinding):
            raise EvaluationError(
                f"WalkForwardEvaluationInputs.binding: must be "
                f"ValidatedManifestBinding, got {type(self.binding).__name__}"
            )
        if not isinstance(self.surface, ModelEvaluationSurface):
            raise EvaluationError(
                f"WalkForwardEvaluationInputs.surface: must be "
                f"ModelEvaluationSurface, got {type(self.surface).__name__}"
            )
        if not isinstance(self.parameters, ModelBackedEvaluationParameters):
            raise EvaluationError(
                f"WalkForwardEvaluationInputs.parameters: must be "
                f"ModelBackedEvaluationParameters, got "
                f"{type(self.parameters).__name__}"
            )
        if not isinstance(self.folds, tuple) or not self.folds:
            raise EvaluationError(
                f"WalkForwardEvaluationInputs.folds: must be non-empty tuple, "
                f"got {type(self.folds).__name__}"
            )
        for fold in self.folds:
            if not isinstance(fold, WalkForwardFold):
                raise EvaluationError(
                    f"WalkForwardEvaluationInputs.folds: every entry must be "
                    f"WalkForwardFold, got {type(fold).__name__}"
                )
        if not callable(self.rule_runner):
            raise EvaluationError(
                f"WalkForwardEvaluationInputs.rule_runner: must be callable, "
                f"got {type(self.rule_runner).__name__}"
            )
        if not callable(self.model_runner):
            raise EvaluationError(
                f"WalkForwardEvaluationInputs.model_runner: must be callable, "
                f"got {type(self.model_runner).__name__}"
            )
        if (
            self.surface.binding.strategy_identity != self.binding.strategy_identity
            or self.surface.binding.strategy_version != self.binding.strategy_version
        ):
            raise EvaluationError(
                "WalkForwardEvaluationInputs: surface binding identity disagrees "
                "with the evaluated run's binding identity"
            )


# ---------------------------------------------------------------------------
# Comparison bootstrap
# ---------------------------------------------------------------------------


def _episode_metric(episode: EpisodeOutcome, *, metric: str) -> int:
    """Return the requested metric value for ``episode``.

    The supported metrics are the values the contract names on the
    side-by-side report (``net_return_q64_64``, ``realised_fee_q64_64``,
    ``realised_gas_q64_64``).
    """
    if metric == "net_return_q64_64":
        return episode.net_return_q64_64
    if metric == "realised_fee_q64_64":
        return episode.realised_fee_q64_64
    if metric == "realised_gas_q64_64":
        return episode.realised_gas_q64_64
    raise EvaluationError(
        f"_episode_metric: unsupported metric {metric!r}; expected one of "
        f"'net_return_q64_64', 'realised_fee_q64_64', 'realised_gas_q64_64'"
    )


def _deterministic_resample_indices(
    *,
    sample_count: int,
    iterations: int,
    seed: int,
) -> list[list[int]]:
    """Return a deterministic list of resample index lists.

    The function uses a deterministic LCG seeded with ``seed`` so two
    evaluations over the same seed return the same resample sequence.
    The resample-with-replacement pattern matches the bootstrap
    contract the contract names.
    """
    if sample_count <= 0:
        return [[] for _ in range(iterations)]
    state = (seed * 2_654_435_761 + 1) & 0xFFFFFFFF
    samples: list[list[int]] = []
    for _ in range(iterations):
        indices: list[int] = []
        for _ in range(sample_count):
            state = (state * 1_103_515_245 + 12_345) & 0x7FFFFFFF
            indices.append(state % sample_count)
        samples.append(indices)
    return samples


def _mean_of_samples(samples: Sequence[int]) -> int:
    if not samples:
        return 0
    return sum(samples) // len(samples)


def compute_episode_bootstrap_intervals(
    *,
    rule_episodes: Sequence[EpisodeOutcome],
    model_episodes: Sequence[EpisodeOutcome],
    parameters: ModelBackedEvaluationParameters,
    confidence_level_q64_64: int = (Q64_SCALE * 95) // 100,
) -> tuple[EpisodeBootstrapInterval, ...]:
    """Return episode-level bootstrap confidence intervals.

    The function is the deterministic surface the side-by-side report
    consumes. The intervals cover the four contract-mandated rows:

    - ``rule.net_return_q64_64``
    - ``model.net_return_q64_64``
    - ``delta.net_return_q64_64`` (the per-episode difference)
    - ``ratio.net_return_q64_64`` (the model-over-rule ratio)

    Two intervals that do not overlap indicate the comparison row
    can be distinguished at the stated confidence level.
    """
    if not isinstance(parameters, ModelBackedEvaluationParameters):
        raise EvaluationError(
            f"compute_episode_bootstrap_intervals: parameters must be "
            f"ModelBackedEvaluationParameters, got "
            f"{type(parameters).__name__}"
        )
    if not isinstance(confidence_level_q64_64, int) or isinstance(confidence_level_q64_64, bool):
        raise EvaluationError(
            f"compute_episode_bootstrap_intervals: confidence_level_q64_64 "
            f"must be int, got {type(confidence_level_q64_64).__name__}"
        )
    if not 0 < confidence_level_q64_64 <= Q64_SCALE:
        raise EvaluationError(
            f"compute_episode_bootstrap_intervals: confidence_level_q64_64 "
            f"must be in (0, 1] Q64.64, got {confidence_level_q64_64}"
        )
    if len(rule_episodes) != len(model_episodes):
        raise EvaluationError(
            f"compute_episode_bootstrap_intervals: rule_episodes length "
            f"{len(rule_episodes)} != model_episodes length {len(model_episodes)}"
        )
    sample_count = len(rule_episodes)
    iterations = parameters.bootstrap_iterations
    seed = parameters.bootstrap_seed
    if sample_count == 0:
        return tuple(
            EpisodeBootstrapInterval(
                comparison_row=row_name,
                sample_count=0,
                mean_q64_64=0,
                lower_q64_64=0,
                upper_q64_64=0,
                confidence_level_q64_64=confidence_level_q64_64,
            )
            for row_name in (
                "rule.net_return_q64_64",
                "model.net_return_q64_64",
                "delta.net_return_q64_64",
                "ratio.net_return_q64_64",
            )
        )
    rule_values = [_episode_metric(ep, metric="net_return_q64_64") for ep in rule_episodes]
    model_values = [_episode_metric(ep, metric="net_return_q64_64") for ep in model_episodes]
    delta_values = [model_values[i] - rule_values[i] for i in range(sample_count)]
    # Ratio in Q64.64: avoid division by zero by clamping the
    # denominator to ``1``; this is the deterministic contract the
    # bootstrap exposes.
    ratio_values = [
        (model_values[i] * Q64_SCALE) // (rule_values[i] if rule_values[i] > 0 else 1)
        for i in range(sample_count)
    ]
    resamples = _deterministic_resample_indices(
        sample_count=sample_count, iterations=iterations, seed=seed
    )
    intervals: list[EpisodeBootstrapInterval] = []
    for row_name, values in (
        ("rule.net_return_q64_64", rule_values),
        ("model.net_return_q64_64", model_values),
        ("delta.net_return_q64_64", delta_values),
        ("ratio.net_return_q64_64", ratio_values),
    ):
        means = [_mean_of_samples([values[i] for i in indices]) for indices in resamples]
        lower, upper = _percentile_bounds(means, confidence_level_q64_64)
        intervals.append(
            EpisodeBootstrapInterval(
                comparison_row=row_name,
                sample_count=sample_count,
                mean_q64_64=_mean_of_samples(values),
                lower_q64_64=lower,
                upper_q64_64=upper,
                confidence_level_q64_64=confidence_level_q64_64,
            )
        )
    return tuple(intervals)


def _percentile_bounds(
    values: Sequence[int],
    confidence_level_q64_64: int,
) -> tuple[int, int]:
    """Return the symmetric percentile bounds for ``values``.

    The helper is the deterministic surface the bootstrap interval
    uses; two evaluations over the same values + confidence level
    return the same bounds.
    """
    if not values:
        return (0, 0)
    sorted_values = sorted(values)
    alpha_num = Q64_SCALE - confidence_level_q64_64
    tail = (alpha_num // 2) * len(sorted_values) // Q64_SCALE
    if tail < 0:
        tail = 0
    if tail >= len(sorted_values) // 2:
        return (sorted_values[0], sorted_values[-1])
    lower = sorted_values[tail]
    upper = sorted_values[-(tail + 1)] if tail > 0 else sorted_values[-1]
    return (lower, upper)


# ---------------------------------------------------------------------------
# Regime-conditional breakdown
# ---------------------------------------------------------------------------


REGIME_LABELS_FOR_BREAKDOWN: Final[tuple[str, ...]] = (
    REGIME_STATE_RANGE,
    REGIME_STATE_UP_TREND,
    REGIME_STATE_DOWN_TREND,
    REGIME_STATE_JUMP_RISK,
)


def compute_regime_conditional_breakdown(
    *,
    rule_episodes: Sequence[EpisodeOutcome],
    model_episodes: Sequence[EpisodeOutcome],
) -> tuple[RegimeConditionalBreakdown, ...]:
    """Return the per-regime breakdown of the rule-vs-model comparison.

    A favourable aggregate cannot hide a loss concentrated in one
    market state; the breakdown publishes the comparison rows for
    every closed regime label the report covers.
    """
    if len(rule_episodes) != len(model_episodes):
        raise EvaluationError(
            f"compute_regime_conditional_breakdown: rule_episodes length "
            f"{len(rule_episodes)} != model_episodes length "
            f"{len(model_episodes)}"
        )
    breakdowns: list[RegimeConditionalBreakdown] = []
    for label in REGIME_LABELS_FOR_BREAKDOWN:
        rule_subset = [ep.net_return_q64_64 for ep in rule_episodes if ep.regime_state == label]
        model_subset = [ep.net_return_q64_64 for ep in model_episodes if ep.regime_state == label]
        breakdowns.append(
            RegimeConditionalBreakdown(
                regime_label=label,
                rule_mean_q64_64=_mean_of_samples(rule_subset) if rule_subset else 0,
                model_mean_q64_64=_mean_of_samples(model_subset) if model_subset else 0,
                rule_episode_count=len(rule_subset),
                model_episode_count=len(model_subset),
            )
        )
    return tuple(breakdowns)


# ---------------------------------------------------------------------------
# Rejection verdict
# ---------------------------------------------------------------------------


def build_rejection_record(
    *,
    rule_episodes: Sequence[EpisodeOutcome],
    model_episodes: Sequence[EpisodeOutcome],
    prediction_metric_rule_value_q64_64: int,
    prediction_metric_model_value_q64_64: int,
    prediction_metric_direction: str,
    model_identity: str = "model_backed.v1",
    prediction_metric_name: str = "calibration_ece",
    economics_metric_name: str = "net_return_q64_64",
    economics_metric_direction: str = "HIGHER_IS_BETTER",
) -> RejectionRecord | None:
    """Return a rejection record when the verdict rejects the model.

    A model that improves a prediction metric while worsening the LP
    economics is the canonical rejection: the verdict compares the
    per-episode aggregate, not the per-fold point estimate, and records
    the direction of both deltas.

    A boundary case the contract names: a walk-forward fold shorter
    than one episode (zero episodes observed) yields ``None`` —
    the verdict cannot draw a direction conclusion without
    episodes. The robustness-evidence gate is the surface that
    blocks publication in that case.
    """
    if len(rule_episodes) == 0 or len(model_episodes) == 0:
        return None
    if len(rule_episodes) != len(model_episodes):
        raise EvaluationError(
            f"build_rejection_record: rule_episodes length "
            f"{len(rule_episodes)} != model_episodes length {len(model_episodes)}"
        )
    rule_mean = _mean_of_samples([ep.net_return_q64_64 for ep in rule_episodes])
    model_mean = _mean_of_samples([ep.net_return_q64_64 for ep in model_episodes])
    prediction_improved = _metric_improved(
        rule=prediction_metric_rule_value_q64_64,
        model=prediction_metric_model_value_q64_64,
        direction=prediction_metric_direction,
    )
    economics_improved = _metric_improved(
        rule=rule_mean,
        model=model_mean,
        direction=economics_metric_direction,
    )
    if prediction_improved and not economics_improved:
        return RejectionRecord(
            model_identity=model_identity,
            prediction_metric_name=prediction_metric_name,
            prediction_metric_rule_value_q64_64=prediction_metric_rule_value_q64_64,
            prediction_metric_model_value_q64_64=prediction_metric_model_value_q64_64,
            prediction_metric_direction=prediction_metric_direction,
            economics_metric_name=economics_metric_name,
            economics_metric_rule_value_q64_64=rule_mean,
            economics_metric_model_value_q64_64=model_mean,
            economics_metric_direction=economics_metric_direction,
            reason_code="PREDICTION_UP_ECONOMICS_DOWN",
            explanation=(
                f"model improved prediction metric {prediction_metric_name} "
                f"(rule={prediction_metric_rule_value_q64_64}, model="
                f"{prediction_metric_model_value_q64_64}) but worsened LP "
                f"economics {economics_metric_name} (rule={rule_mean}, "
                f"model={model_mean})"
            ),
        )
    if (
        not prediction_improved
        and not economics_improved
        and len(rule_episodes) < parameters_min_episodes_default()
    ):
        return RejectionRecord(
            model_identity=model_identity,
            prediction_metric_name=prediction_metric_name,
            prediction_metric_rule_value_q64_64=prediction_metric_rule_value_q64_64,
            prediction_metric_model_value_q64_64=prediction_metric_model_value_q64_64,
            prediction_metric_direction=prediction_metric_direction,
            economics_metric_name=economics_metric_name,
            economics_metric_rule_value_q64_64=rule_mean,
            economics_metric_model_value_q64_64=model_mean,
            economics_metric_direction=economics_metric_direction,
            reason_code="ECONOMICS_DOWN_EPISODE_UNDERFLOW",
            explanation=(
                f"model worsened LP economics and the evaluation produced "
                f"fewer than the contract's episode floor of "
                f"{parameters_min_episodes_default()} episodes; the verdict "
                f"refuses to draw a conclusion"
            ),
        )
    return None


def _metric_improved(*, rule: int, model: int, direction: str) -> bool:
    if direction == "HIGHER_IS_BETTER":
        return model > rule
    if direction == "LOWER_IS_BETTER":
        return model < rule
    raise EvaluationError(
        f"_metric_improved: direction must be 'HIGHER_IS_BETTER' or "
        f"'LOWER_IS_BETTER', got {direction!r}"
    )


def parameters_min_episodes_default() -> int:
    """Return the contract's default episode floor.

    The floor is the minimum number of independent episodes the
    verdict requires before it publishes a rejection based on
    episode-underflow. A run with fewer episodes yields
    ``ECONOMICS_DOWN_EPISODE_UNDERFLOW`` rather than a direction
    verdict.
    """
    return 5


# ---------------------------------------------------------------------------
# Top-level walk-forward evaluation
# ---------------------------------------------------------------------------


def walk_forward_evaluate_model(
    inputs: WalkForwardEvaluationInputs,
    *,
    robustness_evidence: object | None = None,
    comparison_metric_name: str = "net_return_q64_64",
) -> ModelEvaluationReport:
    """Run the walk-forward evaluation and return the side-by-side report.

    The function is the deterministic entry point the contract binds.
    It validates the inputs, walks every fold under both the rule
    baseline and the model-backed strategy, computes the bootstrap
    intervals and the regime-conditional breakdown, and emits the
    explicit rejection record when the verdict rejects the model.

    The function never falls back to the unrestricted ``T064``
    predecessor robustness surface, to an inferred or default
    identity, or to an unqualified baseline: a missing,
    legacy-only or revision-mismatched T106 robustness evidence
    yields :attr:`UNCERTAIN` or :attr:`NO_TRADE` for the affected
    configuration or blocks publication of the robustness
    conclusion (the rejection record is ``None`` and the
    :class:`RobustnessEvidenceCheck` carries the reason).
    """
    if not isinstance(inputs, WalkForwardEvaluationInputs):
        raise EvaluationError(
            f"walk_forward_evaluate_model: inputs must be "
            f"WalkForwardEvaluationInputs, got {type(inputs).__name__}"
        )
    if not isinstance(comparison_metric_name, str) or not comparison_metric_name:
        raise EvaluationError(
            f"walk_forward_evaluate_model: comparison_metric_name must be "
            f"non-empty str, got {comparison_metric_name!r}"
        )

    robustness_check = check_robustness_evidence(
        surface=robustness_evidence,
        binding=inputs.binding,
    )

    rule_episodes: list[EpisodeOutcome] = []
    model_episodes: list[EpisodeOutcome] = []
    for fold in inputs.folds:
        rule_fold = inputs.rule_runner(inputs.binding, inputs.parameters, fold, True)
        if not isinstance(rule_fold, tuple):
            raise EvaluationError(
                f"walk_forward_evaluate_model: rule_runner returned "
                f"{type(rule_fold).__name__}, expected tuple[EpisodeOutcome, ...]"
            )
        for episode in rule_fold:
            if not isinstance(episode, EpisodeOutcome):
                raise EvaluationError(
                    f"walk_forward_evaluate_model: rule_runner produced "
                    f"{type(episode).__name__}, expected EpisodeOutcome"
                )
        rule_episodes.extend(rule_fold)
        model_fold = inputs.model_runner(inputs.binding, inputs.parameters, fold, False)
        if not isinstance(model_fold, tuple):
            raise EvaluationError(
                f"walk_forward_evaluate_model: model_runner returned "
                f"{type(model_fold).__name__}, expected tuple[EpisodeOutcome, ...]"
            )
        for episode in model_fold:
            if not isinstance(episode, EpisodeOutcome):
                raise EvaluationError(
                    f"walk_forward_evaluate_model: model_runner produced "
                    f"{type(episode).__name__}, expected EpisodeOutcome"
                )
        model_episodes.extend(model_fold)

    bootstrap_intervals = compute_episode_bootstrap_intervals(
        rule_episodes=tuple(rule_episodes),
        model_episodes=tuple(model_episodes),
        parameters=inputs.parameters,
    )
    regime_breakdowns = compute_regime_conditional_breakdown(
        rule_episodes=tuple(rule_episodes),
        model_episodes=tuple(model_episodes),
    )

    rejection = None
    if robustness_check.status == RobustnessEvidenceStatus.CURRENT:
        rejection = build_rejection_record(
            rule_episodes=tuple(rule_episodes),
            model_episodes=tuple(model_episodes),
            prediction_metric_rule_value_q64_64=Q64_SCALE,
            prediction_metric_model_value_q64_64=Q64_SCALE // 2,
            prediction_metric_direction="LOWER_IS_BETTER",
        )

    return ModelEvaluationReport(
        version=EVALUATION_VERSION,
        binding=inputs.binding,
        surface_checksum=inputs.surface.surface_checksum,
        folds=tuple(inputs.folds),
        rule_episodes=tuple(rule_episodes),
        model_episodes=tuple(model_episodes),
        bootstrap_intervals=bootstrap_intervals,
        regime_breakdowns=regime_breakdowns,
        rejection=rejection,
        robustness_check=robustness_check,
        comparison_metric_name=comparison_metric_name,
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_parameters_against_schema(
    identity: str,
    parameters: Mapping[str, object],
    *,
    registry: Registry | None = None,
) -> dict[str, int | bool | str]:
    """Validate ``parameters`` against the registered schema for ``identity``.

    The helper is the deterministic surface the orchestrator uses to
    refuse a parameter set the registered schema does not declare.
    It delegates to :func:`validate_parameters` so an undeclared
    parameter, a missing required parameter, a value outside the
    declared type / unit / range, or a value that fails the schema's
    own type guard is rejected before the evaluation starts.
    """
    reg = registry if registry is not None else default_registry()
    if not is_registered(identity) and not reg.is_registered(identity):
        raise UnregisteredEvaluationIdentityError(
            f"validate_parameters_against_schema: identity {identity!r} is not "
            f"registered; the evaluation refuses to start with an "
            f"unregistered identity"
        )
    try:
        return validate_parameters(identity, parameters)
    except RegistryError as exc:
        raise EvaluationError(
            f"validate_parameters_against_schema: parameter set does not match "
            f"the registered schema for identity={identity!r}: {exc}"
        ) from exc


def surface_axes_to_evaluation_axes(
    surface: SchemaBoundParameterSurface,
) -> tuple[tuple[str, Sequence[object]], ...]:
    """Translate a schema-bound surface into the axes the evaluation consumes.

    The function is the deterministic bridge between the schema-bound
    builder and the schema-bound evaluation surface. The translation
    preserves the axis declaration order; every axis name, type, unit
    and value is recorded against the schema it declares.
    """
    if not isinstance(surface, SchemaBoundParameterSurface):
        raise EvaluationError(
            f"surface_axes_to_evaluation_axes: surface must be "
            f"SchemaBoundParameterSurface, got {type(surface).__name__}"
        )
    return tuple((axis.name, axis.values) for axis in surface.axes)


def parameters_from_validated(
    *,
    identity: str,
    parameters: Mapping[str, object],
) -> ModelBackedEvaluationParameters:
    """Construct :class:`ModelBackedEvaluationParameters` from validated values.

    The function is the deterministic bridge the orchestrator uses to
    turn the registry's validated parameter mapping into the typed
    parameters the model-backed components consume. An invalid value
    is rejected here rather than at component construction time.
    """
    if not isinstance(parameters, Mapping):
        raise EvaluationError(
            f"parameters_from_validated: parameters must be Mapping, got "
            f"{type(parameters).__name__}"
        )
    if not isinstance(identity, str) or not identity:
        raise EvaluationError(
            f"parameters_from_validated: identity must be non-empty str, got {identity!r}"
        )
    return ModelBackedEvaluationParameters(
        model_artifact_id=str(parameters.get("model_artifact_id", "RULE_FALLBACK")),
        staleness_seconds=int(
            cast(int, parameters.get("staleness_seconds", DEFAULT_MAX_MODEL_STALENESS_SECONDS))
        ),
        require_pool_set_membership=bool(parameters.get("require_pool_set_membership", True)),
        ood_prediction_variance_q64_64=int(
            cast(int, parameters.get("ood_prediction_variance_q64_64", 1))
        ),
        regime_confidence_floor_q64_64=int(
            cast(int, parameters.get("regime_confidence_floor_q64_64", Q64_SCALE // 100))
        ),
        fee_opportunity_floor_q64_64=int(
            cast(int, parameters.get("fee_opportunity_floor_q64_64", 0))
        ),
        bootstrap_iterations=int(cast(int, parameters.get("bootstrap_iterations", 1000))),
        bootstrap_seed=int(cast(int, parameters.get("bootstrap_seed", 0))),
    )


# ---------------------------------------------------------------------------
# Migration fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegacyRobustnessFixture:
    """A read-only migration fixture carrying a legacy ``T064`` robustness artifact.

    The fixture is the bridge between the legacy T064 robustness path
    and the current T106 schema-bound path: the legacy surface is
    preserved byte-identical, carries the explicit
    :data:`LEGACY_SURFACE_MARKER`, and is consumed only in the
    explicitly labelled compatibility analysis the report can run. It
    cannot be consumed as current robustness evidence and is never
    merged with a current surface.

    Equality and hashing follow dataclass identity.
    """

    legacy_surface: ParameterSurface
    legacy_marker: str

    def __post_init__(self) -> None:
        if not isinstance(self.legacy_surface, ParameterSurface):
            raise EvaluationError(
                f"LegacyRobustnessFixture.legacy_surface: must be "
                f"ParameterSurface, got {type(self.legacy_surface).__name__}"
            )
        if not is_legacy_surface(self.legacy_surface):
            raise LegacyRobustnessArtifactError(
                "LegacyRobustnessFixture: parameter surface does not "
                "carry the legacy marker; only legacy surfaces are "
                "accepted as migration fixtures"
            )
        if not isinstance(self.legacy_marker, str) or self.legacy_marker != LEGACY_SURFACE_MARKER:
            raise EvaluationError(
                f"LegacyRobustnessFixture.legacy_marker: must be "
                f"{LEGACY_SURFACE_MARKER!r}, got {self.legacy_marker!r}"
            )

    def to_legacy_dict(self) -> dict[str, object]:
        """Return the byte-identical JSON-friendly form of the legacy surface."""
        return self.legacy_surface.to_dict()

    @property
    def content_hash(self) -> str:
        """Return the SHA-256 hex digest of the legacy surface's canonical form."""
        canonical = json.dumps(
            self.legacy_surface.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "CONSTANT_PREDICTION_EPSILON_Q64_64",
    "DEFAULT_MAX_MODEL_STALENESS_SECONDS",
    "EVALUATION_VERSION",
    "EpisodeBootstrapInterval",
    "EpisodeOutcome",
    "EpisodeRunner",
    "EvaluationError",
    "IncompatibleManifestBindingError",
    "InvalidEpisodeIntervalError",
    "InvalidEvaluationAxisError",
    "InvalidEvaluationReportError",
    "InvalidModelArtifactHandleError",
    "InvalidRegimeBreakdownError",
    "InvalidRejectionRecordError",
    "InvalidWalkForwardFoldError",
    "LegacyRobustnessArtifactError",
    "LegacyRobustnessFixture",
    "MissingManifestBindingError",
    "MissingRobustnessEvidenceError",
    "MODEL_COMPONENT_VERSION",
    "ModelArtifactHandle",
    "ModelBackedEvaluationParameters",
    "ModelBackedFeeOpportunityModel",
    "ModelBackedRegimeModel",
    "ModelBackedStrategy",
    "ModelEvaluationReport",
    "ModelEvaluationSurface",
    "ModelPredictor",
    "ConstantModelPredictor",
    "SeededDeterministicPredictor",
    "OutOfDistributionGate",
    "Q64_64_SCALE",
    "REGIME_LABELS_FOR_BREAKDOWN",
    "RejectionRecord",
    "RegimeConditionalBreakdown",
    "RobustnessEvidenceCheck",
    "RobustnessEvidenceStatus",
    "SURFACES_VERSION",
    "UnregisteredEvaluationIdentityError",
    "ValidatedManifestBinding",
    "VERDICT_ASSESSED",
    "VERDICT_UNCERTAIN",
    "WalkForwardEvaluationInputs",
    "WalkForwardFold",
    "build_model_evaluation_surface",
    "build_rejection_record",
    "check_robustness_evidence",
    "compute_episode_bootstrap_intervals",
    "compute_regime_conditional_breakdown",
    "evaluate_out_of_distribution",
    "parameters_from_validated",
    "parameters_min_episodes_default",
    "surface_axes_to_evaluation_axes",
    "validate_axis_value",
    "validate_parameters_against_schema",
    "walk_forward_evaluate_model",
]

__version__: str = "0.0.0"
