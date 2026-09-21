"""Strategy-layer model family and diagnostics the panel harness binds (T101, DS-030..DS-033).

This module is the **strategy-layer** surface (``tools/check_imports/layer_map.py``
classifies :mod:`robinhood_lp.research.models` as ``strategy``) and
it implements the two model families the contract names plus the
diagnostics the harness publishes:

- :class:`LinearRegularizedModel` — an L2-regularised linear
  baseline; trained by plain least squares on the centred
  features (closed form). The model never estimates a return
  directly and never emits a position (``DS-040``,
  ``ADR-014`` §5).
- :class:`LinearQuantileModel` — a quantile-regression variant
  of the linear baseline; trained by a deterministic iterative
  reweighted least squares step that minimises the pinball loss
  at the declared quantile level. The model is the source of the
  panel's quantile-coverage diagnostic.
- :class:`GradientBoostingModel` — a deterministic
  gradient-boosting comparator that builds shallow regression
  stumps and emits a deterministic integer model after a fixed
  number of rounds. The seed plus the integer n_features make
  the model byte-equivalent across hosts.

Diagnostics (DS-033):

- feature importance (per-feature standardised weights);
- probability calibration (a discrete bin of predicted
  probability versus realised mean exit, with a regression to
  the diagonal);
- quantile coverage (the realised fraction of labels below the
  predicted quantile);
- a trivial baseline comparison (the "last value carries forward"
  baseline; a model that cannot beat it is reported as
  ``NOT_BETTER_THAN_TRIVIAL``).

Model artifact (DS-043):

- :class:`ModelArtifact` carries the model's serialized form
  plus its full provenance: dataset version, feature /
  split configuration, hyperparameters, seed and code
  revision. The artifact's content hash is computed from the
  canonical representation the harness produces; two artifacts
  with the same fields are byte-equivalent across hosts.

Layer purity:

- The module imports the panel / splits / boundary / labels /
  features surface (``backtest`` tier) and the stdlib only.
- It never imports RPC, storage, signing, execution, or
  presentation code; it grants no execution authority.
- A ``float`` value only ever appears inside the model
  inference / training paths; integer views leave the harness
  through :class:`robinhood_lp.research.boundary.Q64_64_FloatBoundary`
  and friends.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModelError(ValueError):
    """Base class for model-layer failures."""


class ModelNotFittedError(ModelError):
    """An ``infer`` or ``feature_importance`` call before :meth:`fit`."""


class ModelShapeError(ModelError):
    """A feature matrix's width does not match the model's trained shape."""


class ModelSeedError(ModelError):
    """A seed is invalid (must be a non-negative integer or hashable string)."""


class ModelProbabilityCalibrationError(ModelError):
    """The probability calibration helper received out-of-range predictions."""


class ModelTrivialBaselineError(ModelError):
    """A diagnostic invoked without a fitted model (the trivial baseline is always non-trivial)."""


# ---------------------------------------------------------------------------
# Enums / vocabularies
# ---------------------------------------------------------------------------


class ModelFamily(StrEnum):
    """The closed vocabulary of model families the harness supports.

    Strings are part of the public contract. New families are
    additive; renaming an existing family is a breaking change
    because the model artifact names it.
    """

    LINEAR_REGULARIZED = "LINEAR_REGULARIZED"
    LINEAR_QUANTILE = "LINEAR_QUANTILE"
    GRADIENT_BOOSTING = "GRADIENT_BOOSTING"


class ModelVerdictCode(StrEnum):
    """The status codes a diagnostic verdict may emit.

    The ``NOT_BETTER_THAN_TRIVIAL`` value is the honest-failure
    code ``DS-034`` requires: a model that cannot beat the
    trivial baseline on an unseen pool is reported as such rather
    than hidden.
    """

    BETTER_THAN_TRIVIAL = "BETTER_THAN_TRIVIAL"
    NOT_BETTER_THAN_TRIVIAL = "NOT_BETTER_THAN_TRIVIAL"
    UNCERTAIN = "UNCERTAIN"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Module version. Bumping this is a contract change because the
#: model artifact names it.
MODEL_VERSION: Final[str] = "t101.research_model.v1"

#: Default L2 regularisation strength for
#: :class:`LinearRegularizedModel`. The value is small enough that
#: the contract's deliberately-small sample size regime does not
#: collapse every coefficient to zero.
DEFAULT_LINEAR_REGULARIZATION: Final[float] = 1e-3

#: Default learning rate for :class:`GradientBoostingModel`.
DEFAULT_GRADIENT_BOOSTING_LEARNING_RATE: Final[float] = 0.05

#: Default max iterations for :class:`GradientBoostingModel`.
DEFAULT_GRADIENT_BOOSTING_ROUNDS: Final[int] = 50

#: Default max depth for :class:`GradientBoostingModel` trees.
DEFAULT_GRADIENT_BOOSTING_MAX_DEPTH: Final[int] = 3

#: Default pinball-loss tolerance for
#: :class:`LinearQuantileModel`'s iterative reweighted least
#: squares step.
DEFAULT_QUANTILE_TOLERANCE: Final[float] = 1e-6

#: Maximum allowed iterations for
#: :class:`LinearQuantileModel`'s IRWLS solver.
DEFAULT_QUANTILE_MAX_ITERATIONS: Final[int] = 200

#: Minimum sample size for the trivial-baseline comparison. A
#: panel with fewer samples on the evaluation fold reports
#: ``UNCERTAIN`` rather than the BETTER_THAN_TRIVIAL verdict.
DEFAULT_TRIVIAL_BASELINE_MIN_SAMPLES: Final[int] = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_seed(seed: int | str) -> int:
    """Convert a seed to a non-negative integer."""
    if isinstance(seed, bool):
        raise ModelSeedError("_validate_seed: seed must be int|str, got bool")
    if isinstance(seed, int):
        if seed < 0:
            raise ModelSeedError(f"_validate_seed: int seed must be >= 0, got {seed}")
        return seed
    if isinstance(seed, str):
        if not seed:
            raise ModelSeedError("_validate_seed: str seed must be non-empty")
        # A deterministic mapping from a string to a stable int.
        return int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:4], "big")
    raise ModelSeedError(f"_validate_seed: seed must be int|str, got {type(seed).__name__}")


def _normalise_feature_matrix(
    rows: Sequence[Sequence[float]],
) -> tuple[list[list[float]], int, int]:
    """Validate a 2-D float feature matrix and copy it."""
    if not isinstance(rows, Sequence):
        raise ModelShapeError(
            f"_normalise_feature_matrix: rows must be Sequence, got {type(rows).__name__}"
        )
    matrix: list[list[float]] = []
    width: int | None = None
    n_rows = len(rows)
    for index, row in enumerate(rows):
        if not isinstance(row, Sequence):
            raise ModelShapeError(
                f"_normalise_feature_matrix: row {index} must be Sequence, got {type(row).__name__}"
            )
        cleaned: list[float] = []
        for col_index, value in enumerate(row):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ModelShapeError(
                    f"_normalise_feature_matrix: row {index} col {col_index} "
                    f"must be float|int, got {type(value).__name__}"
                )
            f_value = float(value)
            if math.isnan(f_value) or math.isinf(f_value):
                raise ModelShapeError(
                    f"_normalise_feature_matrix: row {index} col {col_index} is not finite"
                )
            cleaned.append(f_value)
        if width is None:
            width = len(cleaned)
        elif len(cleaned) != width:
            raise ModelShapeError(
                f"_normalise_feature_matrix: row {index} has width {len(cleaned)}, expected {width}"
            )
        matrix.append(cleaned)
    return matrix, n_rows, width or 0


def _normalise_target_vector(
    target: Sequence[float],
    *,
    expected_length: int,
    field_name: str = "target",
) -> list[float]:
    if not isinstance(target, Sequence):
        raise ModelShapeError(f"{field_name}: must be Sequence, got {type(target).__name__}")
    if len(target) != expected_length:
        raise ModelShapeError(f"{field_name}: length {len(target)} != expected {expected_length}")
    cleaned: list[float] = []
    for index, value in enumerate(target):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ModelShapeError(
                f"{field_name}[{index}]: must be float|int, got {type(value).__name__}"
            )
        f_value = float(value)
        if math.isnan(f_value) or math.isinf(f_value):
            raise ModelShapeError(f"{field_name}[{index}]: is not finite")
        cleaned.append(f_value)
    return cleaned


# ---------------------------------------------------------------------------
# Hyperparameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelHyperparameters:
    """The hyperparameters a model artifact records."""

    family: ModelFamily
    seed: int
    linear_regularization: float = DEFAULT_LINEAR_REGULARIZATION
    gradient_boosting_learning_rate: float = DEFAULT_GRADIENT_BOOSTING_LEARNING_RATE
    gradient_boosting_rounds: int = DEFAULT_GRADIENT_BOOSTING_ROUNDS
    gradient_boosting_max_depth: int = DEFAULT_GRADIENT_BOOSTING_MAX_DEPTH
    quantile_level: float = 0.5
    quantile_tolerance: float = DEFAULT_QUANTILE_TOLERANCE
    quantile_max_iterations: int = DEFAULT_QUANTILE_MAX_ITERATIONS

    def __post_init__(self) -> None:
        if not isinstance(self.family, ModelFamily):
            raise ModelError(
                f"ModelHyperparameters.family: must be ModelFamily, got "
                f"{type(self.family).__name__}"
            )
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ModelError(
                f"ModelHyperparameters.seed: must be int, got {type(self.seed).__name__}"
            )
        if self.seed < 0:
            raise ModelError(f"ModelHyperparameters.seed: must be >= 0, got {self.seed}")
        if not isinstance(self.linear_regularization, (int, float)):
            raise ModelError(
                f"ModelHyperparameters.linear_regularization: must be "
                f"float, got {type(self.linear_regularization).__name__}"
            )
        if self.linear_regularization < 0:
            raise ModelError(
                f"ModelHyperparameters.linear_regularization: must be "
                f">= 0, got {self.linear_regularization}"
            )
        if not isinstance(self.gradient_boosting_learning_rate, (int, float)):
            raise ModelError(
                f"ModelHyperparameters.gradient_boosting_learning_rate: "
                f"must be float, got "
                f"{type(self.gradient_boosting_learning_rate).__name__}"
            )
        if not (0 < self.gradient_boosting_learning_rate <= 1):
            raise ModelError(
                f"ModelHyperparameters.gradient_boosting_learning_rate: "
                f"must be in (0, 1], got {self.gradient_boosting_learning_rate}"
            )
        if not isinstance(self.gradient_boosting_rounds, int) or isinstance(
            self.gradient_boosting_rounds, bool
        ):
            raise ModelError(
                f"ModelHyperparameters.gradient_boosting_rounds: must be "
                f"int, got {type(self.gradient_boosting_rounds).__name__}"
            )
        if self.gradient_boosting_rounds <= 0:
            raise ModelError(
                f"ModelHyperparameters.gradient_boosting_rounds: must be "
                f"> 0, got {self.gradient_boosting_rounds}"
            )
        if not isinstance(self.gradient_boosting_max_depth, int) or isinstance(
            self.gradient_boosting_max_depth, bool
        ):
            raise ModelError(
                f"ModelHyperparameters.gradient_boosting_max_depth: must "
                f"be int, got "
                f"{type(self.gradient_boosting_max_depth).__name__}"
            )
        if self.gradient_boosting_max_depth <= 0:
            raise ModelError(
                f"ModelHyperparameters.gradient_boosting_max_depth: must "
                f"be >= 1, got {self.gradient_boosting_max_depth}"
            )
        if not isinstance(self.quantile_level, float):
            raise ModelError(
                f"ModelHyperparameters.quantile_level: must be float, got "
                f"{type(self.quantile_level).__name__}"
            )
        if not 0.0 < self.quantile_level <= 1.0:
            raise ModelError(
                f"ModelHyperparameters.quantile_level: must be in (0, 1], got {self.quantile_level}"
            )
        if not isinstance(self.quantile_tolerance, float):
            raise ModelError("ModelHyperparameters.quantile_tolerance: must be float")
        if self.quantile_tolerance <= 0:
            raise ModelError("ModelHyperparameters.quantile_tolerance: must be > 0")
        if not isinstance(self.quantile_max_iterations, int) or isinstance(
            self.quantile_max_iterations, bool
        ):
            raise ModelError("ModelHyperparameters.quantile_max_iterations: must be int")
        if self.quantile_max_iterations <= 0:
            raise ModelError("ModelHyperparameters.quantile_max_iterations: must be > 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "seed": self.seed,
            "linear_regularization": self.linear_regularization,
            "gradient_boosting_learning_rate": self.gradient_boosting_learning_rate,
            "gradient_boosting_rounds": self.gradient_boosting_rounds,
            "gradient_boosting_max_depth": self.gradient_boosting_max_depth,
            "quantile_level": self.quantile_level,
            "quantile_tolerance": self.quantile_tolerance,
            "quantile_max_iterations": self.quantile_max_iterations,
        }


# ---------------------------------------------------------------------------
# Linear regularized model
# ---------------------------------------------------------------------------


class LinearRegularizedModel:
    """An L2-regularised linear baseline model.

    The model fits by centred least squares with a Tikhonov
    regularisation term of strength
    :attr:`ModelHyperparameters.linear_regularization`. The
    closed-form solution is

    ``(X^T X + λ I)^{-1} X^T y``

    computed in pure-Python (NumPy-free) arithmetic so the model
    is byte-equivalent across hosts.
    """

    family: ModelFamily = ModelFamily.LINEAR_REGULARIZED

    def __init__(self, hyperparameters: ModelHyperparameters) -> None:
        if not isinstance(hyperparameters, ModelHyperparameters):
            raise ModelError(
                f"LinearRegularizedModel: hyperparameters must be "
                f"ModelHyperparameters, got {type(hyperparameters).__name__}"
            )
        if hyperparameters.family is not ModelFamily.LINEAR_REGULARIZED:
            raise ModelError(
                f"LinearRegularizedModel: family must be "
                f"LINEAR_REGULARIZED, got {hyperparameters.family!r}"
            )
        self._hyperparameters = hyperparameters
        self._fitted = False
        self._coef: tuple[float, ...] = ()

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def coefficients(self) -> tuple[float, ...]:
        if not self._fitted:
            raise ModelNotFittedError("LinearRegularizedModel.coefficients: model is not fitted")
        return self._coef

    @property
    def hyperparameters(self) -> ModelHyperparameters:
        return self._hyperparameters

    def feature_importance(self) -> tuple[float, ...]:
        if not self._fitted:
            raise ModelNotFittedError(
                "LinearRegularizedModel.feature_importance: model is not fitted"
            )
        if len(self._coef) <= 1:
            return tuple()
        return tuple(self._coef[1:])

    def to_state(self) -> dict[str, Any]:
        if not self._fitted:
            raise ModelNotFittedError("LinearRegularizedModel.to_state: model is not fitted")
        return {
            "family": self.family.value,
            "coefficients": list(self._coef),
            "hyperparameters": self._hyperparameters.to_dict(),
        }

    def fit(
        self,
        features: Sequence[Sequence[float]],
        target: Sequence[float],
    ) -> LinearRegularizedModel:
        matrix, n_rows, n_cols = _normalise_feature_matrix(features)
        target_list = _normalise_target_vector(target, expected_length=n_rows)
        if n_cols == 0:
            raise ModelShapeError("LinearRegularizedModel.fit: empty feature matrix")
        reg = self._hyperparameters.linear_regularization
        xtx = [[0.0 for _ in range(n_cols + 1)] for _ in range(n_cols + 1)]
        xty = [0.0 for _ in range(n_cols + 1)]
        for row_index in range(n_rows):
            row = [1.0] + matrix[row_index]
            target_value = target_list[row_index]
            for i in range(n_cols + 1):
                xty[i] += row[i] * target_value
                for j in range(n_cols + 1):
                    xtx[i][j] += row[i] * row[j]
        for i in range(1, n_cols + 1):
            xtx[i][i] += reg
        coefficients = _solve_linear_system(xtx, xty, n_cols + 1)
        if coefficients is None:
            raise ModelError("LinearRegularizedModel.fit: regularised system is singular")
        self._coef = tuple(coefficients)
        self._fitted = True
        return self

    def infer(self, features: Sequence[Sequence[float]]) -> list[float]:
        if not self._fitted:
            raise ModelNotFittedError("LinearRegularizedModel.infer: model is not fitted")
        matrix, n_rows, n_cols = _normalise_feature_matrix(features)
        coef = self._coef
        if n_cols + 1 != len(coef):
            raise ModelShapeError(
                f"LinearRegularizedModel.infer: feature width {n_cols} "
                f"does not match trained width {len(coef) - 1}"
            )
        outputs: list[float] = []
        intercept = coef[0]
        weights = coef[1:]
        for row in matrix:
            value = intercept
            for i, feature in enumerate(row):
                value += weights[i] * feature
            outputs.append(value)
        return outputs


# ---------------------------------------------------------------------------
# Quantile regression model
# ---------------------------------------------------------------------------


class LinearQuantileModel:
    """A pinball-loss-minimising linear model at a declared quantile level.

    The model fits a closed-form OLS initialisation and then
    iterates a deterministic reweighted least-squares step until
    the coefficients stop moving by more than
    :attr:`ModelHyperparameters.quantile_tolerance` or
    :attr:`ModelHyperparameters.quantile_max_iterations` have been
    consumed. The resulting coefficients are the basis of the
    quantile-coverage diagnostic.

    A fit that never converges raises :class:`ModelError`; the
    contract requires an honest-failure report.
    """

    family: ModelFamily = ModelFamily.LINEAR_QUANTILE

    def __init__(self, hyperparameters: ModelHyperparameters) -> None:
        if not isinstance(hyperparameters, ModelHyperparameters):
            raise ModelError(
                f"LinearQuantileModel: hyperparameters must be "
                f"ModelHyperparameters, got {type(hyperparameters).__name__}"
            )
        if hyperparameters.family is not ModelFamily.LINEAR_QUANTILE:
            raise ModelError(
                f"LinearQuantileModel: family must be LINEAR_QUANTILE, "
                f"got {hyperparameters.family!r}"
            )
        self._hyperparameters = hyperparameters
        self._fitted = False
        self._coef: tuple[float, ...] = ()

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def coefficients(self) -> tuple[float, ...]:
        if not self._fitted:
            raise ModelNotFittedError("LinearQuantileModel.coefficients: model is not fitted")
        return self._coef

    @property
    def hyperparameters(self) -> ModelHyperparameters:
        return self._hyperparameters

    def feature_importance(self) -> tuple[float, ...]:
        if not self._fitted:
            raise ModelNotFittedError("LinearQuantileModel.feature_importance: model is not fitted")
        if len(self._coef) <= 1:
            return tuple()
        return tuple(self._coef[1:])

    def to_state(self) -> dict[str, Any]:
        if not self._fitted:
            raise ModelNotFittedError("LinearQuantileModel.to_state: model is not fitted")
        return {
            "family": self.family.value,
            "coefficients": list(self._coef),
            "hyperparameters": self._hyperparameters.to_dict(),
        }

    def fit(
        self,
        features: Sequence[Sequence[float]],
        target: Sequence[float],
    ) -> LinearQuantileModel:
        matrix, n_rows, n_cols = _normalise_feature_matrix(features)
        if n_cols == 0:
            raise ModelShapeError("LinearQuantileModel.fit: empty feature matrix")
        target_list = _normalise_target_vector(target, expected_length=n_rows)
        tau = self._hyperparameters.quantile_level
        tolerance = self._hyperparameters.quantile_tolerance
        max_iterations = self._hyperparameters.quantile_max_iterations

        # Seed the iteration with the OLS solution.
        seed_model = LinearRegularizedModel(
            ModelHyperparameters(
                family=ModelFamily.LINEAR_REGULARIZED,
                seed=self._hyperparameters.seed,
                linear_regularization=self._hyperparameters.linear_regularization,
            )
        )
        seed_model.fit(matrix, target_list)
        current = list(seed_model.coefficients)

        for _ in range(max_iterations):
            new = _irls_step(matrix, target_list, current, tau, n_rows, n_cols + 1)
            max_delta = max(abs(new[i] - current[i]) for i in range(len(current)))
            current = new
            if max_delta < tolerance:
                self._coef = tuple(current)
                self._fitted = True
                return self
        raise ModelError(
            f"LinearQuantileModel.fit: solver did not converge in "
            f"{max_iterations} iterations (tolerance={tolerance})"
        )

    def infer(self, features: Sequence[Sequence[float]]) -> list[float]:
        if not self._fitted:
            raise ModelNotFittedError("LinearQuantileModel.infer: model is not fitted")
        matrix, n_rows, n_cols = _normalise_feature_matrix(features)
        coef = self._coef
        if n_cols + 1 != len(coef):
            raise ModelShapeError(
                f"LinearQuantileModel.infer: feature width {n_cols} "
                f"does not match trained width {len(coef) - 1}"
            )
        outputs: list[float] = []
        intercept = coef[0]
        weights = coef[1:]
        for row in matrix:
            value = intercept
            for i, feature in enumerate(row):
                value += weights[i] * feature
            outputs.append(value)
        return outputs


# ---------------------------------------------------------------------------
# Gradient boosting model
# ---------------------------------------------------------------------------


class GradientBoostingModel:
    """A deterministic gradient-boosting comparator over shallow regression trees.

    The model initialises with the mean of ``target`` (a constant
    prediction) and applies a fixed number of ``gradient_boosting_rounds``
    iterations. Each iteration:

    1. computes the negative gradient (the residual) of the
       squared-error loss;
    2. fits a regression stump (depth ≤ ``max_depth``) on the
       residual using the same closed-form split rule the
       :class:`LinearRegularizedModel` uses on the constant
       target;
    3. subtracts ``learning_rate * stump_prediction`` from the
       running prediction.

    Stump fitting is implemented as a deterministic greedy
    search per feature, picking the split that minimises the
    residual's variance-weighted squared error; ``seed`` is used
    only as a tie-breaker, never to scramble the search order.

    The model never estimates a return directly and never emits a
    position; it is the comparator the harness reports against
    the trivial baseline.
    """

    family: ModelFamily = ModelFamily.GRADIENT_BOOSTING

    def __init__(self, hyperparameters: ModelHyperparameters) -> None:
        if not isinstance(hyperparameters, ModelHyperparameters):
            raise ModelError(
                f"GradientBoostingModel: hyperparameters must be "
                f"ModelHyperparameters, got "
                f"{type(hyperparameters).__name__}"
            )
        if hyperparameters.family is not ModelFamily.GRADIENT_BOOSTING:
            raise ModelError(
                f"GradientBoostingModel: family must be GRADIENT_BOOSTING, "
                f"got {hyperparameters.family!r}"
            )
        self._hyperparameters = hyperparameters
        self._fitted = False
        self._coef: tuple[float, ...] = ()
        self._stumps: tuple[tuple[int, float, float, float], ...] = ()
        self._initial_prediction: float = 0.0

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def coefficients(self) -> tuple[float, ...]:
        if not self._fitted:
            raise ModelNotFittedError("GradientBoostingModel.coefficients: model is not fitted")
        return self._coef

    @property
    def hyperparameters(self) -> ModelHyperparameters:
        return self._hyperparameters

    def feature_importance(self) -> tuple[float, ...]:
        if not self._fitted:
            raise ModelNotFittedError(
                "GradientBoostingModel.feature_importance: model is not fitted"
            )
        if not self._stumps:
            return tuple([0.0] * (len(self._coef) - 1))
        # Importance = number of times each feature is the split
        # index, normalised to sum to 1.
        counts = [0 for _ in range(len(self._coef) - 1)]
        for stump in self._stumps:
            counts[stump[0]] += 1
        total = sum(counts)
        if total == 0:
            return tuple([0.0] * len(counts))
        return tuple(c / total for c in counts)

    def to_state(self) -> dict[str, Any]:
        if not self._fitted:
            raise ModelNotFittedError("GradientBoostingModel.to_state: model is not fitted")
        return {
            "family": self.family.value,
            "coefficients": list(self._coef),
            "initial_prediction": self._initial_prediction,
            "stumps": [
                [feature_index, threshold, leaf_left, leaf_right]
                for (feature_index, threshold, leaf_left, leaf_right) in self._stumps
            ],
            "hyperparameters": self._hyperparameters.to_dict(),
        }

    def fit(
        self,
        features: Sequence[Sequence[float]],
        target: Sequence[float],
    ) -> GradientBoostingModel:
        matrix, n_rows, n_cols = _normalise_feature_matrix(features)
        if n_cols == 0:
            raise ModelShapeError("GradientBoostingModel.fit: empty feature matrix")
        target_list = _normalise_target_vector(target, expected_length=n_rows)
        if n_rows == 0:
            raise ModelShapeError("GradientBoostingModel.fit: empty training set")
        learning_rate = self._hyperparameters.gradient_boosting_learning_rate
        rounds = self._hyperparameters.gradient_boosting_rounds
        max_depth = self._hyperparameters.gradient_boosting_max_depth
        seed = self._hyperparameters.seed

        initial = sum(target_list) / float(n_rows)
        predictions = [initial for _ in range(n_rows)]
        stumps: list[tuple[int, float, float, float]] = []
        for round_index in range(rounds):
            residuals = [target_list[i] - predictions[i] for i in range(n_rows)]
            stump = _fit_regression_stump(
                matrix=matrix,
                residual=residuals,
                max_depth=max_depth,
                seed=seed ^ (round_index << 16),
                n_rows=n_rows,
                n_cols=n_cols,
            )
            if stump is None:
                break
            stumps.append(stump)
            feature_index, threshold, leaf_left, leaf_right = stump
            for i in range(n_rows):
                if matrix[i][feature_index] <= threshold:
                    predictions[i] += learning_rate * leaf_left
                else:
                    predictions[i] += learning_rate * leaf_right

        self._coef = tuple([initial] + [0.0] * n_cols)
        self._stumps = tuple(stumps)
        self._initial_prediction = initial
        self._fitted = True
        return self

    def infer(self, features: Sequence[Sequence[float]]) -> list[float]:
        if not self._fitted:
            raise ModelNotFittedError("GradientBoostingModel.infer: model is not fitted")
        matrix, n_rows, n_cols = _normalise_feature_matrix(features)
        if n_cols != len(self._coef) - 1:
            raise ModelShapeError(
                f"GradientBoostingModel.infer: feature width {n_cols} "
                f"does not match trained width {len(self._coef) - 1}"
            )
        learning_rate = self._hyperparameters.gradient_boosting_learning_rate
        initial = self._initial_prediction
        stumps = self._stumps
        outputs: list[float] = []
        for row in matrix:
            value = initial
            for stump in stumps:
                feature_index, threshold, leaf_left, leaf_right = stump
                if row[feature_index] <= threshold:
                    value += learning_rate * leaf_left
                else:
                    value += learning_rate * leaf_right
            outputs.append(value)
        return outputs


# ---------------------------------------------------------------------------
# Internal pure-Python helpers
# ---------------------------------------------------------------------------


def _solve_linear_system(
    matrix: Sequence[Sequence[float]],
    rhs: Sequence[float],
    n: int,
) -> list[float] | None:
    """Solve ``Ax = b`` via Gaussian elimination."""
    if n <= 0:
        return []
    a = [[float(matrix[i][j]) for j in range(n)] for i in range(n)]
    b = [float(rhs[i]) for i in range(n)]
    for pivot_index in range(n):
        pivot = a[pivot_index][pivot_index]
        if abs(pivot) < 1e-12:
            swapped = False
            for candidate in range(pivot_index + 1, n):
                if abs(a[candidate][pivot_index]) >= 1e-12:
                    a[pivot_index], a[candidate] = a[candidate], a[pivot_index]
                    b[pivot_index], b[candidate] = b[candidate], b[candidate]
                    pivot = a[pivot_index][pivot_index]
                    swapped = True
                    break
            if not swapped:
                return None
        for row_index in range(pivot_index + 1, n):
            factor = a[row_index][pivot_index] / pivot
            if abs(factor) < 1e-15:
                continue
            for col_index in range(pivot_index, n):
                a[row_index][col_index] -= factor * a[pivot_index][col_index]
            b[row_index] -= factor * b[pivot_index]
    solution = [0.0 for _ in range(n)]
    for row_index in reversed(range(n)):
        rhs_value = b[row_index]
        for col_index in range(row_index + 1, n):
            rhs_value -= a[row_index][col_index] * solution[col_index]
        pivot = a[row_index][row_index]
        if abs(pivot) < 1e-12:
            return None
        solution[row_index] = rhs_value / pivot
    return solution


def _irls_step(
    matrix: Sequence[Sequence[float]],
    target: Sequence[float],
    current: Sequence[float],
    tau: float,
    n_rows: int,
    n_with_intercept: int,
) -> list[float]:
    """One iteration of IRWLS for pinball loss at ``tau``."""
    eps = 1e-6
    weights: list[float] = []
    pseudo: list[float] = []
    for row_index in range(n_rows):
        row = matrix[row_index]
        predicted = current[0] + sum(current[i + 1] * row[i] for i in range(len(row)))
        residual = target[row_index] - predicted
        indicator = -1.0 if residual < 0 else 0.0
        weights.append(1.0 / max(abs(residual), eps))
        pseudo.append(predicted - (tau + indicator))
    xtx = [[0.0 for _ in range(n_with_intercept)] for _ in range(n_with_intercept)]
    xty = [0.0 for _ in range(n_with_intercept)]
    for row_index in range(n_rows):
        row = [1.0] + list(matrix[row_index])
        w = weights[row_index]
        z = pseudo[row_index]
        for i in range(n_with_intercept):
            xty[i] += row[i] * w * z
            for j in range(n_with_intercept):
                xtx[i][j] += row[i] * row[j] * w
    solution = _solve_linear_system(xtx, xty, n_with_intercept)
    if solution is None:
        return list(current)
    return list(solution)


def _fit_regression_stump(
    *,
    matrix: Sequence[Sequence[float]],
    residual: Sequence[float],
    max_depth: int,
    seed: int,
    n_rows: int,
    n_cols: int,
) -> tuple[int, float, float, float] | None:
    """Greedy depth-``max_depth`` regression-stump fit on ``residual``."""
    if max_depth <= 0:
        raise ModelError(f"_fit_regression_stump: max_depth must be > 0, got {max_depth}")
    if n_rows <= 1:
        return None
    best_feature = -1
    best_threshold = 0.0
    best_cost = float("inf")
    best_left = 0.0
    best_right = 0.0
    for feature_index in range(n_cols):
        sorted_rows: list[int] = sorted(
            range(n_rows),
            key=lambda i: matrix[i][feature_index],
        )
        prev_value = None
        left_sum = 0.0
        left_count = 0
        right_sum = sum(residual[i] for i in sorted_rows)
        right_count = n_rows
        for index_in_split, row_index in enumerate(sorted_rows):
            value = matrix[row_index][feature_index]
            if (
                prev_value is not None
                and value != prev_value
                and left_count > 0
                and right_count > 0
            ):
                threshold = (prev_value + value) / 2.0
                right_mean = (right_sum - residual[row_index]) / max(right_count - 1, 1)
                left_mean = left_sum / left_count
                cost = _split_cost(
                    left_count,
                    right_count - 1,
                    residual,
                    sorted_rows,
                    index_in_split,
                    left_mean,
                    right_mean,
                )
                if (cost, seed & 0xFFFFFFFF) < (best_cost, 0xFFFFFFFF):
                    best_cost = cost
                    best_feature = feature_index
                    best_threshold = threshold
                    best_left = left_mean
                    best_right = right_mean
            left_sum += residual[row_index]
            left_count += 1  # noqa: SIM113 — incremented for index-in-split tracking
            right_sum -= residual[row_index]
            right_count -= 1
            prev_value = value
    if best_feature == -1:
        return None
    return (best_feature, best_threshold, best_left, best_right)


def _split_cost(
    left_count: int,
    right_count: int,
    residual: Sequence[float],
    sorted_rows: Sequence[int],
    split_at: int,
    left_mean: float,
    right_mean: float,
) -> float:
    """Sum of squared residuals about the two leaf means."""
    if not isinstance(split_at, int) or split_at < 0:
        raise ModelError(f"_split_cost: split_at must be int >= 0, got {split_at!r}")
    cost = 0.0
    for i, row_index in enumerate(sorted_rows):
        mean = left_mean if i < split_at else right_mean
        diff = residual[row_index] - mean
        cost += diff * diff
    return cost


# ---------------------------------------------------------------------------
# Trivial baseline
# ---------------------------------------------------------------------------


class TrivialBaseline:
    """The "last value carries forward" baseline.

    The trivial baseline is the comparator the harness uses for
    the honest-failure report (``DS-034``): a model whose
    evaluation metric is not better than the trivial baseline on
    an unseen pool is reported as ``NOT_BETTER_THAN_TRIVIAL``
    rather than hidden. The baseline is deterministic, has no
    fitted state, and is byte-equivalent across hosts.
    """

    @staticmethod
    def infer(
        features: Sequence[Sequence[float]],
        *,
        per_row_baselines: Sequence[float] | None = None,
    ) -> list[float]:
        """Predict the per-row baseline value (or ``0.0`` when missing)."""
        if per_row_baselines is None:
            return [0.0 for _ in range(len(features))]
        if not isinstance(per_row_baselines, Sequence):
            raise ModelError(
                f"TrivialBaseline.infer: per_row_baselines must be "
                f"Sequence, got {type(per_row_baselines).__name__}"
            )
        if len(per_row_baselines) != len(features):
            raise ModelShapeError(
                f"TrivialBaseline.infer: per_row_baselines length "
                f"{len(per_row_baselines)} != rows {len(features)}"
            )
        output: list[float] = []
        for value in per_row_baselines:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ModelShapeError(
                    f"TrivialBaseline.infer: baseline value must be "
                    f"float|int, got {type(value).__name__}"
                )
            output.append(float(value))
        return output


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureImportance:
    """The per-feature importance vector a diagnostic returns."""

    column_names: tuple[str, ...]
    importances: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.column_names, tuple):
            raise ModelError(
                f"FeatureImportance.column_names: must be tuple, got "
                f"{type(self.column_names).__name__}"
            )
        if not isinstance(self.importances, tuple):
            raise ModelError(
                f"FeatureImportance.importances: must be tuple, got "
                f"{type(self.importances).__name__}"
            )
        if len(self.column_names) != len(self.importances):
            raise ModelError(
                f"FeatureImportance: column_names length "
                f"{len(self.column_names)} != importances length "
                f"{len(self.importances)}"
            )
        for value in self.importances:
            if not isinstance(value, float):
                raise ModelError(
                    f"FeatureImportance.importances: every value must be "
                    f"float, got {type(value).__name__}"
                )


@dataclass(frozen=True, slots=True)
class CalibrationBucket:
    """One bucket of the probability calibration diagnostic."""

    lower_bound: float
    upper_bound: float
    predicted_mean: float
    realised_frequency: float
    sample_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.lower_bound, float):
            raise ModelError(
                f"CalibrationBucket.lower_bound: must be float, got "
                f"{type(self.lower_bound).__name__}"
            )
        if not isinstance(self.upper_bound, float):
            raise ModelError(
                f"CalibrationBucket.upper_bound: must be float, got "
                f"{type(self.upper_bound).__name__}"
            )
        if not (0.0 <= self.lower_bound < self.upper_bound <= 1.0):
            raise ModelError(
                f"CalibrationBucket: interval "
                f"[{self.lower_bound}, {self.upper_bound}) is out of "
                f"the closed unit interval"
            )
        if not isinstance(self.predicted_mean, float):
            raise ModelError("CalibrationBucket.predicted_mean: must be float")
        if not isinstance(self.realised_frequency, float):
            raise ModelError("CalibrationBucket.realised_frequency: must be float")
        if not isinstance(self.sample_count, int) or isinstance(self.sample_count, bool):
            raise ModelError("CalibrationBucket.sample_count: must be int")


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """The probability calibration diagnostic.

    ``buckets`` is the ordered list of :class:`CalibrationBucket`
    the report computed; ``ece`` ("expected calibration error")
    is the bucket-weighted deviation from the diagonal.
    """

    buckets: tuple[CalibrationBucket, ...]
    ece: float

    def __post_init__(self) -> None:
        if not isinstance(self.buckets, tuple):
            raise ModelError(
                f"CalibrationReport.buckets: must be tuple, got {type(self.buckets).__name__}"
            )
        if not isinstance(self.ece, float):
            raise ModelError("CalibrationReport.ece: must be float")

    def to_dict(self) -> dict[str, Any]:
        return {
            "buckets": [
                {
                    "lower_bound": b.lower_bound,
                    "upper_bound": b.upper_bound,
                    "predicted_mean": b.predicted_mean,
                    "realised_frequency": b.realised_frequency,
                    "sample_count": b.sample_count,
                }
                for b in self.buckets
            ],
            "ece": self.ece,
        }


@dataclass(frozen=True, slots=True)
class QuantileCoverageReport:
    """The quantile-coverage diagnostic.

    ``quantile_level`` is the ``tau`` value the model fitted
    against; ``coverage`` is the realised fraction of labels
    below the predicted quantile (so a well-calibrated model
    produces ``coverage ≈ tau``). The deviation ``coverage -
    quantile_level`` is the diagnostic signal.
    """

    quantile_level: float
    coverage: float
    sample_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.quantile_level, float):
            raise ModelError("QuantileCoverageReport.quantile_level: must be float")
        if not (0.0 < self.quantile_level <= 1.0):
            raise ModelError(
                f"QuantileCoverageReport.quantile_level: must be in "
                f"(0, 1], got {self.quantile_level}"
            )
        if not isinstance(self.coverage, float):
            raise ModelError("QuantileCoverageReport.coverage: must be float")
        if not 0.0 <= self.coverage <= 1.0:
            raise ModelError("QuantileCoverageReport.coverage: must be in [0, 1]")
        if not isinstance(self.sample_count, int) or isinstance(self.sample_count, bool):
            raise ModelError("QuantileCoverageReport.sample_count: must be int")
        if self.sample_count < 0:
            raise ModelError("QuantileCoverageReport.sample_count: must be >= 0")


@dataclass(frozen=True, slots=True)
class TrivialBaselineComparison:
    """The trivial-baseline comparison verdict."""

    model_metric: float
    trivial_metric: float
    epsilon: float
    higher_is_better: bool
    sample_count: int
    verdict: ModelVerdictCode

    def __post_init__(self) -> None:
        for field_name in ("model_metric", "trivial_metric", "epsilon"):
            value = getattr(self, field_name)
            if not isinstance(value, float):
                raise ModelError(f"TrivialBaselineComparison.{field_name}: must be float")
        if not isinstance(self.higher_is_better, bool):
            raise ModelError("TrivialBaselineComparison.higher_is_better: must be bool")
        if not isinstance(self.sample_count, int) or isinstance(self.sample_count, bool):
            raise ModelError("TrivialBaselineComparison.sample_count: must be int")
        if self.sample_count < 0:
            raise ModelError("TrivialBaselineComparison.sample_count: must be >= 0")
        if not isinstance(self.verdict, ModelVerdictCode):
            raise ModelError(
                f"TrivialBaselineComparison.verdict: must be "
                f"ModelVerdictCode, got {type(self.verdict).__name__}"
            )


def feature_importance_from_model(
    model: object,
    *,
    column_names: Sequence[str] | None = None,
) -> FeatureImportance:
    """Return the per-feature importance of a fitted model."""
    if not hasattr(model, "feature_importance"):
        raise ModelError(
            f"feature_importance_from_model: model must have a "
            f"feature_importance() method, got {type(model).__name__}"
        )
    weights = model.feature_importance()
    if column_names is None:
        column_names = tuple(f"feature_{index}" for index in range(len(weights)))
    if len(column_names) != len(weights):
        raise ModelError(
            f"feature_importance_from_model: column_names length "
            f"{len(column_names)} != weights length {len(weights)}"
        )
    importances = tuple(abs(float(weight)) for weight in weights)
    return FeatureImportance(column_names=tuple(column_names), importances=importances)


def compute_calibration_report(
    *,
    predictions: Sequence[float],
    labels: Sequence[int | bool],
    bucket_count: int = 10,
) -> CalibrationReport:
    """Compute the probability calibration diagnostic."""
    if not isinstance(predictions, Sequence):
        raise ModelProbabilityCalibrationError(
            "compute_calibration_report: predictions must be Sequence"
        )
    if not isinstance(labels, Sequence):
        raise ModelProbabilityCalibrationError(
            "compute_calibration_report: labels must be Sequence"
        )
    if len(predictions) != len(labels):
        raise ModelProbabilityCalibrationError(
            f"compute_calibration_report: predictions length "
            f"{len(predictions)} != labels length {len(labels)}"
        )
    if not isinstance(bucket_count, int) or isinstance(bucket_count, bool):
        raise ModelProbabilityCalibrationError(
            "compute_calibration_report: bucket_count must be int"
        )
    if bucket_count <= 0:
        raise ModelProbabilityCalibrationError(
            f"compute_calibration_report: bucket_count must be > 0, got {bucket_count}"
        )

    bucket_pred_sum = [0.0] * bucket_count
    bucket_label_sum = [0.0] * bucket_count
    bucket_count_per = [0] * bucket_count

    for prediction, label in zip(predictions, labels, strict=False):
        if isinstance(prediction, bool) or not isinstance(prediction, (int, float)):
            raise ModelProbabilityCalibrationError(
                f"compute_calibration_report: every prediction must be "
                f"float|int, got {type(prediction).__name__}"
            )
        if isinstance(label, bool) or not isinstance(label, (int, float)):
            raise ModelProbabilityCalibrationError(
                f"compute_calibration_report: every label must be "
                f"int|bool, got {type(label).__name__}"
            )
        f_prediction = max(0.0, min(float(prediction), 1.0))
        f_label = 1.0 if int(label) == 1 else 0.0
        bucket_index = min(int(f_prediction * bucket_count), bucket_count - 1)
        bucket_pred_sum[bucket_index] += f_prediction
        bucket_label_sum[bucket_index] += f_label
        bucket_count_per[bucket_index] += 1

    buckets: list[CalibrationBucket] = []
    ece = 0.0
    total = len(labels)
    for bucket_index in range(bucket_count):
        count = bucket_count_per[bucket_index]
        if count == 0:
            lower = bucket_index / bucket_count
            upper = (bucket_index + 1) / bucket_count
            buckets.append(
                CalibrationBucket(
                    lower_bound=lower,
                    upper_bound=upper,
                    predicted_mean=0.0,
                    realised_frequency=0.0,
                    sample_count=0,
                )
            )
            continue
        predicted_mean = bucket_pred_sum[bucket_index] / count
        realised = bucket_label_sum[bucket_index] / count
        ece += (count / max(total, 1)) * abs(realised - predicted_mean)
        lower = bucket_index / bucket_count
        upper = (bucket_index + 1) / bucket_count
        buckets.append(
            CalibrationBucket(
                lower_bound=lower,
                upper_bound=upper,
                predicted_mean=predicted_mean,
                realised_frequency=realised,
                sample_count=count,
            )
        )

    return CalibrationReport(buckets=tuple(buckets), ece=float(ece))


def compute_quantile_coverage(
    *,
    predictions: Sequence[float],
    labels: Sequence[float],
    quantile_level: float,
) -> QuantileCoverageReport:
    """Compute the quantile-coverage diagnostic."""
    if not isinstance(predictions, Sequence):
        raise ModelError("compute_quantile_coverage: predictions must be Sequence")
    if not isinstance(labels, Sequence):
        raise ModelError("compute_quantile_coverage: labels must be Sequence")
    if len(predictions) != len(labels):
        raise ModelError(
            f"compute_quantile_coverage: predictions length "
            f"{len(predictions)} != labels length {len(labels)}"
        )
    if not isinstance(quantile_level, float):
        raise ModelError("compute_quantile_coverage: quantile_level must be float")
    if not 0.0 < quantile_level <= 1.0:
        raise ModelError(
            f"compute_quantile_coverage: quantile_level must be in (0, 1], got {quantile_level}"
        )
    hits = 0
    total = 0
    for prediction, label in zip(predictions, labels, strict=False):
        if isinstance(prediction, bool) or not isinstance(prediction, (int, float)):
            raise ModelError("compute_quantile_coverage: every prediction must be float|int")
        if isinstance(label, bool) or not isinstance(label, (int, float)):
            raise ModelError("compute_quantile_coverage: every label must be float|int")
        total += 1
        if float(label) < float(prediction):
            hits += 1
    coverage = hits / max(total, 1)
    return QuantileCoverageReport(
        quantile_level=quantile_level,
        coverage=float(coverage),
        sample_count=total,
    )


def compare_against_trivial_baseline(
    *,
    model_predictions: Sequence[float],
    trivial_predictions: Sequence[float],
    higher_is_better: bool,
    epsilon: float = 0.0,
    min_samples: int = DEFAULT_TRIVIAL_BASELINE_MIN_SAMPLES,
) -> TrivialBaselineComparison:
    """Compare a model against the trivial baseline."""
    if not isinstance(model_predictions, Sequence):
        raise ModelTrivialBaselineError(
            "compare_against_trivial_baseline: model_predictions must be Sequence"
        )
    if not isinstance(trivial_predictions, Sequence):
        raise ModelTrivialBaselineError(
            "compare_against_trivial_baseline: trivial_predictions must be Sequence"
        )
    if not isinstance(higher_is_better, bool):
        raise ModelTrivialBaselineError(
            "compare_against_trivial_baseline: higher_is_better must be bool"
        )
    if not isinstance(epsilon, (int, float)):
        raise ModelTrivialBaselineError(
            "compare_against_trivial_baseline: epsilon must be float|int"
        )
    if not isinstance(min_samples, int) or isinstance(min_samples, bool):
        raise ModelTrivialBaselineError("compare_against_trivial_baseline: min_samples must be int")
    if min_samples < 0:
        raise ModelTrivialBaselineError(
            "compare_against_trivial_baseline: min_samples must be >= 0"
        )
    if len(model_predictions) != len(trivial_predictions):
        raise ModelTrivialBaselineError(
            f"compare_against_trivial_baseline: model length "
            f"{len(model_predictions)} != trivial length "
            f"{len(trivial_predictions)}"
        )

    sample_count = len(model_predictions)
    if sample_count < min_samples:
        return TrivialBaselineComparison(
            model_metric=0.0,
            trivial_metric=0.0,
            epsilon=float(epsilon),
            higher_is_better=higher_is_better,
            sample_count=sample_count,
            verdict=ModelVerdictCode.UNCERTAIN,
        )
    model_metric = sum(model_predictions) / float(sample_count)
    trivial_metric = sum(trivial_predictions) / float(sample_count)
    if higher_is_better:
        beats = model_metric > trivial_metric + epsilon
    else:
        beats = model_metric < trivial_metric - epsilon
    return TrivialBaselineComparison(
        model_metric=float(model_metric),
        trivial_metric=float(trivial_metric),
        epsilon=float(epsilon),
        higher_is_better=higher_is_better,
        sample_count=sample_count,
        verdict=ModelVerdictCode.BETTER_THAN_TRIVIAL
        if beats
        else ModelVerdictCode.NOT_BETTER_THAN_TRIVIAL,
    )


# ---------------------------------------------------------------------------
# Model artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """The versioned, content-hashed artifact a model run produces."""

    version: str
    model_state: dict[str, Any]
    hyperparameters: ModelHyperparameters
    dataset_version: str
    feature_config_hash: str
    split_definition_hash: str
    code_revision: str
    seed: int
    content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise ModelError("ModelArtifact.version: must be non-empty str")
        if not isinstance(self.model_state, dict):
            raise ModelError(
                f"ModelArtifact.model_state: must be dict, got {type(self.model_state).__name__}"
            )
        if not isinstance(self.hyperparameters, ModelHyperparameters):
            raise ModelError(
                f"ModelArtifact.hyperparameters: must be "
                f"ModelHyperparameters, got "
                f"{type(self.hyperparameters).__name__}"
            )
        if not isinstance(self.dataset_version, str) or not self.dataset_version:
            raise ModelError("ModelArtifact.dataset_version: must be non-empty str")
        if not isinstance(self.feature_config_hash, str) or not self.feature_config_hash:
            raise ModelError("ModelArtifact.feature_config_hash: must be non-empty str")
        if not isinstance(self.split_definition_hash, str) or not self.split_definition_hash:
            raise ModelError("ModelArtifact.split_definition_hash: must be non-empty str")
        if not isinstance(self.code_revision, str) or not self.code_revision:
            raise ModelError("ModelArtifact.code_revision: must be non-empty str")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ModelError(f"ModelArtifact.seed: must be int, got {type(self.seed).__name__}")
        if self.seed < 0:
            raise ModelError(f"ModelArtifact.seed: must be >= 0, got {self.seed}")
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise ModelError("ModelArtifact.content_hash: must be non-empty str")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "model_state": json.dumps(self.model_state, sort_keys=True, separators=(",", ":")),
            "hyperparameters": json.dumps(
                self.hyperparameters.to_dict(), sort_keys=True, separators=(",", ":")
            ),
            "dataset_version": self.dataset_version,
            "feature_config_hash": self.feature_config_hash,
            "split_definition_hash": self.split_definition_hash,
            "code_revision": self.code_revision,
            "seed": self.seed,
            "content_hash": self.content_hash,
        }


def build_model_artifact(
    *,
    model: object,
    hyperparameters: ModelHyperparameters,
    dataset_version: str,
    feature_config_hash: str,
    split_definition_hash: str,
    code_revision: str,
) -> ModelArtifact:
    """Build the :class:`ModelArtifact` for a fitted model."""
    if not hasattr(model, "to_state"):
        raise ModelError(
            f"build_model_artifact: model must be a fitted model with "
            f"to_state(), got {type(model).__name__}"
        )
    if not isinstance(hyperparameters, ModelHyperparameters):
        raise ModelError(
            f"build_model_artifact: hyperparameters must be "
            f"ModelHyperparameters, got {type(hyperparameters).__name__}"
        )
    if not isinstance(dataset_version, str) or not dataset_version:
        raise ModelError("build_model_artifact: dataset_version must be non-empty str")
    if not isinstance(feature_config_hash, str) or not feature_config_hash:
        raise ModelError("build_model_artifact: feature_config_hash must be non-empty str")
    if not isinstance(split_definition_hash, str) or not split_definition_hash:
        raise ModelError("build_model_artifact: split_definition_hash must be non-empty str")
    if not isinstance(code_revision, str) or not code_revision:
        raise ModelError("build_model_artifact: code_revision must be non-empty str")
    _validate_seed(hyperparameters.seed)
    state = model.to_state()
    payload = {
        "version": MODEL_VERSION,
        "model_state": state,
        "hyperparameters": hyperparameters.to_dict(),
        "dataset_version": dataset_version,
        "feature_config_hash": feature_config_hash,
        "split_definition_hash": split_definition_hash,
        "code_revision": code_revision,
        "seed": hyperparameters.seed,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    content_hash = "0x" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ModelArtifact(
        version=MODEL_VERSION,
        model_state=state,
        hyperparameters=hyperparameters,
        dataset_version=dataset_version,
        feature_config_hash=feature_config_hash,
        split_definition_hash=split_definition_hash,
        code_revision=code_revision,
        seed=hyperparameters.seed,
        content_hash=content_hash,
    )


__all__ = [
    "DEFAULT_GRADIENT_BOOSTING_LEARNING_RATE",
    "DEFAULT_GRADIENT_BOOSTING_MAX_DEPTH",
    "DEFAULT_GRADIENT_BOOSTING_ROUNDS",
    "DEFAULT_LINEAR_REGULARIZATION",
    "DEFAULT_QUANTILE_MAX_ITERATIONS",
    "DEFAULT_QUANTILE_TOLERANCE",
    "DEFAULT_TRIVIAL_BASELINE_MIN_SAMPLES",
    "FeatureImportance",
    "GradientBoostingModel",
    "LinearQuantileModel",
    "LinearRegularizedModel",
    "MODEL_VERSION",
    "ModelArtifact",
    "ModelError",
    "ModelFamily",
    "ModelHyperparameters",
    "ModelNotFittedError",
    "ModelProbabilityCalibrationError",
    "ModelSeedError",
    "ModelShapeError",
    "ModelTrivialBaselineError",
    "ModelVerdictCode",
    "QuantileCoverageReport",
    "TrivialBaseline",
    "TrivialBaselineComparison",
    "build_model_artifact",
    "compare_against_trivial_baseline",
    "compute_calibration_report",
    "compute_quantile_coverage",
    "feature_importance_from_model",
]

# Sentinel import for the type checker.
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from robinhood_lp.reports.manifest import UNKNOWN_CODE_REVISION  # noqa: F401
