"""Strategy-layer wrapper for the T102 model-backed strategy (T102).

This module hosts the strategy-layer wrapper the T068 registry's
factory returns for the ``t102.model_backed.v1`` identity. The
wrapper is the deterministic bridge the registry exposes: a factory
build against the registered identity returns an instance bound to
the validated parameters the schema declared.

The wrapper itself does not run a strategy; it carries the parameters,
the pool / chain identity, and the component version the
evaluation uses to construct the model-backed components.

Layer purity (binding):

- The module imports the standard library and the
  protocol-domain contracts only. It does not import RPC,
  storage, signing, execution, presentation, replay, or
  the ``research`` package; the dependency surface is
  deliberately narrower than the strategy layer's general
  denylist because the wrapper is the surface the registry
  factory exercises, and the registry must not pull in the
  backtest / research layer at module load time.

- The wrapper holds a :class:`ModelBackedEvaluationParameters`
  value that the model-backed components in
  :mod:`robinhood_lp.research.evaluation` consume; the
  parameters themselves are imported lazily by the
  factory so the registry's import graph does not gain a
  cycle through the research package.

References:

- T060 — the strategy contracts the wrapper composes.
- T068 — strategy registry the wrapper is registered through.
- T102 — model-backed strategy evaluation that consumes the
  wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass

from robinhood_lp.strategy.base import (
    Q64_SCALE,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Module version. Bumping this is a breaking change for the T068
#: registry (the registry captures the module version at
#: construction time).
MODEL_BACKED_STRATEGY_VERSION: str = "t102.model_backed_strategy.v1"

#: Component version the model-backed components stamp on every
#: assessment. The value matches ``MODEL_COMPONENT_VERSION`` in
#: :mod:`robinhood_lp.research.evaluation`; it is duplicated here
#: so the strategy layer does not depend on the research package
#: at module load time.
MODEL_COMPONENT_VERSION: str = "t102.model_component.v1"

# ---------------------------------------------------------------------------
# Parameters (re-exported for the registry factory)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelBackedEvaluationParameters:
    """The schema-bound parameters a model-backed evaluation accepts.

    The dataclass is the strategy-layer twin of the parameters the
    model-backed components in :mod:`robinhood_lp.research.evaluation`
    consume. The dataclass lives in the strategy layer so the
    registry factory can build a validated wrapper without pulling
    the research package into the strategy layer's import graph.

    Field units are explicit:

    - ``model_artifact_id`` — non-empty str; the
      :class:`ModelArtifactHandle` ``artifact_id`` the components
      load. The literal sentinel ``RULE_FALLBACK`` yields the
      deterministic rule fallback (no artifact, no inferred or
      default identity).
    - ``staleness_seconds`` — non-negative int; the maximum age of
      the artifact before it is treated as stale. ``0`` disables
      the staleness check.
    - ``require_pool_set_membership`` — bool; when ``True``, an
      evaluation pool disjoint from the artifact's
      ``training_pool_set`` is treated as out-of-distribution.
    - ``ood_prediction_variance_q64_64`` — non-negative int
      (Q64.64); the minimum number of unique predictions the
      artifact must produce across a fold.
    - ``regime_confidence_floor_q64_64`` — positive int (Q64.64);
      the minimum confidence the model-backed regime model must
      produce for an ``ASSESSED`` outcome.
    - ``fee_opportunity_floor_q64_64`` — non-negative int (Q64.64);
      the minimum expected fee edge the model-backed fee-opportunity
      model must produce for an ``ASSESSED`` outcome.
    - ``bootstrap_iterations`` — positive int; the number of
      bootstrap resamples the evaluation uses for episode-level
      confidence intervals.
    - ``bootstrap_seed`` — non-negative int; the deterministic seed
      the bootstrap resampler consumes.

    Equality and hashing follow dataclass identity.
    """

    model_artifact_id: str = "RULE_FALLBACK"
    staleness_seconds: int = 7 * 24 * 60 * 60
    require_pool_set_membership: bool = True
    ood_prediction_variance_q64_64: int = 1
    regime_confidence_floor_q64_64: int = Q64_SCALE // 100
    fee_opportunity_floor_q64_64: int = 0
    bootstrap_iterations: int = 1000
    bootstrap_seed: int = 0


# ---------------------------------------------------------------------------
# Strategy wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelBackedStrategy:
    """The strategy-layer wrapper the T068 factory returns for T102.

    The wrapper is the deterministic bridge the registry exposes: a
    factory build against the registered T068 identity returns an
    instance bound to the validated parameters the schema declared. The
    wrapper itself does not run a strategy; it carries the parameters,
    the pool / chain identity, and the component version the
    evaluation uses to construct the model-backed components.

    Equality and hashing follow dataclass identity.
    """

    pool_key_id: str
    chain_id: int
    parameters: ModelBackedEvaluationParameters
    component_version: str
    q64_scale: int

    def __post_init__(self) -> None:
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise ValueError(
                f"ModelBackedStrategy.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise ValueError(
                f"ModelBackedStrategy.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise ValueError(f"ModelBackedStrategy.chain_id: must be positive, got {self.chain_id}")
        if not isinstance(self.parameters, ModelBackedEvaluationParameters):
            raise ValueError(
                f"ModelBackedStrategy.parameters: must be "
                f"ModelBackedEvaluationParameters, got "
                f"{type(self.parameters).__name__}"
            )
        if not isinstance(self.component_version, str) or not self.component_version:
            raise ValueError(
                f"ModelBackedStrategy.component_version: must be non-empty "
                f"str, got {self.component_version!r}"
            )
        if not isinstance(self.q64_scale, int) or isinstance(self.q64_scale, bool):
            raise ValueError(
                f"ModelBackedStrategy.q64_scale: must be int, got {type(self.q64_scale).__name__}"
            )
        if self.q64_scale <= 0:
            raise ValueError(
                f"ModelBackedStrategy.q64_scale: must be positive, got {self.q64_scale}"
            )


__all__ = [
    "MODEL_BACKED_STRATEGY_VERSION",
    "ModelBackedEvaluationParameters",
    "ModelBackedStrategy",
]

__version__: str = "0.0.0"
