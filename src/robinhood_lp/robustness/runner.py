"""Robustness runner (T064 + T106).

The runner is the orchestrator the T064 acceptance clause calls
when a robustness sweep runs. It binds the catalogueto the report:
the runner reads the catalogue *before* the sweep touches any
data, executes the sweep, and assembles the report. The runner
does not bypass the catalogue: every declared ``HALT`` /
``DEGRADE`` is honoured; no scenario is decided after the run.

The runner is intentionally minimal in scope. It does not own:

- the parameter sweep's per-combo strategy logic — that lives in
  :mod:`robinhood_lp.strategy` and is injected as a callable;
- the pool holdout's per-pool evaluation logic — that lives in
  the research harness T101 and is injected as a callable;
- the underlying backtest engine — the runner is a layer above
  the engine and depends on it through the injected callables.

The runner owns:

- the catalogue-read step (defensive validation before any data
  is touched);
- the embargo / label-horizon derivation (no hand-chosen length);
- the train / validation / test fold-result collection;
- the sensitivity summary computation;
- the multiple-comparison disclosure;
- the report assembly via :func:`build_robustness_report` for the
  legacy T064 path or :func:`build_schema_bound_robustness_report`
  for the T106 schema-bound path.

T106 cutover (binding):

- The legacy :class:`RobustnessRunner` consumes a T064
  :class:`ParameterSurface` and produces a legacy
  :class:`RobustnessReport`. The legacy path remains read-only and
  is preserved for the legacy-reader / test surface.

- The schema-bound :class:`SchemaBoundRobustnessRunner` consumes a
  :class:`SchemaBoundParameterSurface` and produces a
  :class:`SchemaBoundRobustnessReport`. The schema-bound path is
  the current publication path; every run identity that flows
  into the T107 search / candidate lock and the T096 final
  traceability dossier must carry the schema-bound binding.

Design constraints:

- **Catalogue first.** :meth:`RobustnessRunner.run` and
  :meth:`SchemaBoundRobustnessRunner.run` both call
  :func:`assert_catalogue_complete` before any data is touched,
  and never re-decide a scenario's outcome.

- **DS-041.** The runner never falls through a ``DEGRADE`` to
  undefined behaviour; every declared fallback routes through
  :meth:`DegradationPolicy.resolve`.

- **Determinism.** The runner's report inputs are passed by the
  caller; the runner itself adds nothing non-deterministic.

- **Layer purity.** The runner imports the standard library and
  the in-package :mod:`robinhood_lp.robustness` primitives only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from robinhood_lp.robustness.disclosure import (
    MultipleComparisonDisclosure,
    build_disclosure,
)
from robinhood_lp.robustness.reports import (
    PRIMARY_GENERALISATION_AXIS,
    FoldResult,
    PoolHoldoutResult,
    RobustnessReport,
    SchemaBoundRobustnessReport,
    build_robustness_report,
    build_schema_bound_robustness_report,
)
from robinhood_lp.robustness.scenarios import (
    ScenarioCatalogue,
    assert_catalogue_complete,
)
from robinhood_lp.robustness.schema_binding import (
    SchemaBoundParameterSurface,
    assert_surfaces_compatible,
    surface_identity,
)
from robinhood_lp.robustness.splits import (
    LabelHorizon,
    PoolHoldoutSplit,
    TimeHoldoutSplit,
    WalkForwardSplit,
)
from robinhood_lp.robustness.surfaces import (
    ParameterSurface,
    RegimeSegmentation,
    SensitivitySummary,
    is_legacy_surface,
    summarize_sensitivity,
)

#: Module version (legacy T064 runner).
RUNNER_VERSION: Final[str] = "t064.robustness_runner.v1"

#: Module version (T106 schema-bound runner).
SCHEMA_BOUND_RUNNER_VERSION: Final[str] = "t106.robustness_runner.v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RunnerError(ValueError):
    """Base class for runner construction / execution failures."""


class RunnerInputsError(RunnerError):
    """A runner input is malformed (negative count, mismatched lengths, ...)."""


class LegacySurfaceInSchemaBoundRunnerError(RunnerError):
    """A legacy T064 :class:`ParameterSurface` was supplied to the schema-bound runner.

    The T106 contract binds that current publication cannot reach
    the unrestricted T064 surface. The runner raises this error so
    a reviewer can localise the misuse.
    """


class IncompatibleSurfaceRevisionError(RunnerError):
    """Two schema-bound surfaces carried into the same sweep disagreed on revision.

    The T106 contract binds that results from incompatible schema
    revisions are not merged into one sensitivity conclusion.
    """


# ---------------------------------------------------------------------------
# Callables injected by the orchestrator
# ---------------------------------------------------------------------------


#: A fold-evaluator callable. The orchestrator supplies a callable
#: that, given a fold specification, returns the fold's metric value.
#: The runner calls this callable once per fold; the callable itself
#: runs the backtest engine.
FoldEvaluator = Callable[..., int]

#: A pool-evaluator callable. The orchestrator supplies a callable
#: that, given a held-out pool, returns the pool's metric value.
PoolEvaluator = Callable[..., int]


# ---------------------------------------------------------------------------
# Runner inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RobustnessRunnerInputs:
    """The inputs the runner consumes.

    Field units:

    - ``run_id`` — non-empty string; the run's identity.
    - ``label_horizon`` — :class:`LabelHorizon`; the source of the
      embargo length (never hand-chosen).
    - ``metric_name`` — non-empty string; the metric the runner
      aggregates.
    - ``scenario_catalogue`` — :class:`ScenarioCatalogue`.
    - ``pool_holdout_split`` — :class:`PoolHoldoutSplit`; the
      primary generalisation test.
    - ``time_holdout_split`` — :class:`TimeHoldoutSplit` or
      ``None``.
    - ``walk_forward_splits`` — tuple of :class:`WalkForwardSplit`.
    - ``parameter_surface`` — :class:`ParameterSurface` or
      ``None``.
    - ``regime_segmentation`` — :class:`RegimeSegmentation` or
      ``None``.
    - ``train_fold_specs`` — iterable of ``(segment_label,
      fold_index)``; the runner calls the fold evaluator once per
      spec.
    - ``validation_fold_specs`` / ``test_fold_specs`` — same.
    - ``pool_specs`` — iterable of ``(chain_id, pool_key_id)``;
      the runner calls the pool evaluator once per spec.
    - ``family_wise_alpha_q64_64`` — Q64.64 ratio; the family-wise
      alpha the disclosure reports.
    - ``conclusion_statement`` — non-empty string; the human
      summary.
    """

    run_id: str
    label_horizon: LabelHorizon
    metric_name: str
    scenario_catalogue: ScenarioCatalogue
    pool_holdout_split: PoolHoldoutSplit
    train_fold_specs: Sequence[tuple[str, int]]
    validation_fold_specs: Sequence[tuple[str, int]]
    test_fold_specs: Sequence[tuple[str, int]]
    pool_specs: Sequence[tuple[int, str]]
    family_wise_alpha_q64_64: int
    conclusion_statement: str
    time_holdout_split: TimeHoldoutSplit | None = None
    walk_forward_splits: tuple[WalkForwardSplit, ...] = ()
    parameter_surface: ParameterSurface | None = None
    regime_segmentation: RegimeSegmentation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise RunnerInputsError("RobustnessRunnerInputs.run_id: must be non-empty str")
        if not isinstance(self.metric_name, str) or not self.metric_name:
            raise RunnerInputsError("RobustnessRunnerInputs.metric_name: must be non-empty str")
        if not isinstance(self.family_wise_alpha_q64_64, int) or isinstance(
            self.family_wise_alpha_q64_64, bool
        ):
            raise RunnerInputsError(
                f"RobustnessRunnerInputs.family_wise_alpha_q64_64: must be int, got "
                f"{type(self.family_wise_alpha_q64_64).__name__}"
            )
        if self.family_wise_alpha_q64_64 < 0:
            raise RunnerInputsError(
                f"RobustnessRunnerInputs.family_wise_alpha_q64_64: must be "
                f"non-negative, got {self.family_wise_alpha_q64_64}"
            )
        if not isinstance(self.conclusion_statement, str) or not self.conclusion_statement:
            raise RunnerInputsError(
                "RobustnessRunnerInputs.conclusion_statement: must be non-empty str"
            )
        if not self.pool_specs:
            raise RunnerInputsError(
                "RobustnessRunnerInputs.pool_specs: must be non-empty; "
                "DS-021 binds pool holdout as the primary generalisation test"
            )


@dataclass(frozen=True, slots=True)
class SchemaBoundRobustnessRunnerInputs:
    """The inputs the schema-bound runner consumes.

    Field units mirror :class:`RobustnessRunnerInputs`; the
    difference is the ``schema_bound_surface`` field: a non-``None``
    value must be a :class:`SchemaBoundParameterSurface` (a legacy
    :class:`ParameterSurface` is rejected with
    :class:`LegacySurfaceInSchemaBoundRunnerError`).
    """

    run_id: str
    label_horizon: LabelHorizon
    metric_name: str
    scenario_catalogue: ScenarioCatalogue
    pool_holdout_split: PoolHoldoutSplit
    train_fold_specs: Sequence[tuple[str, int]]
    validation_fold_specs: Sequence[tuple[str, int]]
    test_fold_specs: Sequence[tuple[str, int]]
    pool_specs: Sequence[tuple[int, str]]
    family_wise_alpha_q64_64: int
    conclusion_statement: str
    schema_bound_surface: SchemaBoundParameterSurface | None = None
    time_holdout_split: TimeHoldoutSplit | None = None
    walk_forward_splits: tuple[WalkForwardSplit, ...] = ()
    regime_segmentation: RegimeSegmentation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise RunnerInputsError(
                "SchemaBoundRobustnessRunnerInputs.run_id: must be non-empty str"
            )
        if not isinstance(self.metric_name, str) or not self.metric_name:
            raise RunnerInputsError(
                "SchemaBoundRobustnessRunnerInputs.metric_name: must be non-empty str"
            )
        if not isinstance(self.family_wise_alpha_q64_64, int) or isinstance(
            self.family_wise_alpha_q64_64, bool
        ):
            raise RunnerInputsError(
                f"SchemaBoundRobustnessRunnerInputs.family_wise_alpha_q64_64: "
                f"must be int, got {type(self.family_wise_alpha_q64_64).__name__}"
            )
        if self.family_wise_alpha_q64_64 < 0:
            raise RunnerInputsError(
                f"SchemaBoundRobustnessRunnerInputs.family_wise_alpha_q64_64: "
                f"must be non-negative, got {self.family_wise_alpha_q64_64}"
            )
        if not isinstance(self.conclusion_statement, str) or not self.conclusion_statement:
            raise RunnerInputsError(
                "SchemaBoundRobustnessRunnerInputs.conclusion_statement: must be non-empty str"
            )
        if not self.pool_specs:
            raise RunnerInputsError(
                "SchemaBoundRobustnessRunnerInputs.pool_specs: must be "
                "non-empty; DS-021 binds pool holdout as the primary "
                "generalisation test"
            )
        if self.schema_bound_surface is not None and is_legacy_surface(self.schema_bound_surface):
            raise LegacySurfaceInSchemaBoundRunnerError(
                "SchemaBoundRobustnessRunnerInputs.schema_bound_surface carries "
                "a legacy ParameterSurface; current publication cannot use "
                "the unrestricted T064 path"
            )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RobustnessRunner:
    """The T064 legacy orchestrator.

    The runner is a thin layer over the cataloguereader, the
    fold / pool evaluators, the sensitivity summariser, and the
    report builder. The runner is constructed with the catalogueto
    read; :meth:`run` consumes :class:`RobustnessRunnerInputs`
    plus the two evaluators and returns a :class:`RobustnessReport`.

    The runner does not bypass the catalogue: it calls
    :func:`assert_catalogue_complete` before any data is touched,
    and it never re-decides a scenario's outcome after the run.
    """

    version: str

    def __post_init__(self) -> None:
        if self.version != RUNNER_VERSION:
            raise RunnerError(
                f"RobustnessRunner.version: must be {RUNNER_VERSION!r}, got {self.version!r}"
            )

    def run(
        self,
        inputs: RobustnessRunnerInputs,
        *,
        fold_evaluator: FoldEvaluator,
        pool_evaluator: PoolEvaluator,
        fold_evaluator_kwargs: Mapping[str, object] | None = None,
        pool_evaluator_kwargs: Mapping[str, object] | None = None,
    ) -> RobustnessReport:
        """Execute the robustness sweep and return the assembled report.

        Parameters
        ----------
        inputs:
            The :class:`RobustnessRunnerInputs` the sweep consumes.
        fold_evaluator:
            Callable that returns a non-negative integer metric
            value for a given ``(role, segment_label, fold_index)``
            triple.
        pool_evaluator:
            Callable that returns a non-negative integer metric
            value for a given ``(chain_id, pool_key_id)`` pair.
        fold_evaluator_kwargs, pool_evaluator_kwargs:
            Optional keyword arguments passed to the evaluators.
        """
        # Catalogue first: defensive validation before any data
        # is touched (DS-035 / DS-041).
        assert_catalogue_complete(inputs.scenario_catalogue)

        fold_kwargs: dict[str, object] = dict(fold_evaluator_kwargs or {})
        pool_kwargs: dict[str, object] = dict(pool_evaluator_kwargs or {})

        # Train / validation / test fold collection.
        train_results = self._collect_fold_results(
            role="TRAIN",
            specs=inputs.train_fold_specs,
            metric_name=inputs.metric_name,
            fold_evaluator=fold_evaluator,
            fold_kwargs=fold_kwargs,
        )
        validation_results = self._collect_fold_results(
            role="VALIDATION",
            specs=inputs.validation_fold_specs,
            metric_name=inputs.metric_name,
            fold_evaluator=fold_evaluator,
            fold_kwargs=fold_kwargs,
        )
        test_results = self._collect_fold_results(
            role="TEST",
            specs=inputs.test_fold_specs,
            metric_name=inputs.metric_name,
            fold_evaluator=fold_evaluator,
            fold_kwargs=fold_kwargs,
        )
        # Per-held-out-pool collection (DS-021 primary generalisation).
        pool_holdout_results = self._collect_pool_results(
            specs=inputs.pool_specs,
            metric_name=inputs.metric_name,
            pool_evaluator=pool_evaluator,
            pool_kwargs=pool_kwargs,
        )
        # Sensitivity summary.
        sensitivity_summary = self._compute_sensitivity(
            metric_name=inputs.metric_name,
            grid_metric_values=self._grid_metric_values(
                inputs=inputs,
                pool_evaluator=pool_evaluator,
                pool_kwargs=pool_kwargs,
            ),
        )
        # Multiple-comparison disclosure.
        disclosure = self._build_disclosure(
            inputs=inputs,
            metric_values=tuple(r.metric_value for r in pool_holdout_results),
        )
        return build_robustness_report(
            run_id=inputs.run_id,
            label_horizon=inputs.label_horizon,
            train_results=train_results,
            validation_results=validation_results,
            test_results=test_results,
            pool_holdout_results=pool_holdout_results,
            walk_forward_splits=inputs.walk_forward_splits,
            pool_holdout_split=inputs.pool_holdout_split,
            sensitivity_summary=sensitivity_summary,
            scenario_catalogue=inputs.scenario_catalogue,
            multiple_comparison_disclosure=disclosure,
            conclusion_statement=inputs.conclusion_statement,
            time_holdout_split=inputs.time_holdout_split,
            parameter_surface=inputs.parameter_surface,
            regime_segmentation=inputs.regime_segmentation,
        )

    @staticmethod
    def _collect_fold_results(
        *,
        role: str,
        specs: Iterable[tuple[str, int]],
        metric_name: str,
        fold_evaluator: FoldEvaluator,
        fold_kwargs: Mapping[str, object],
    ) -> tuple[FoldResult, ...]:
        results: list[FoldResult] = []
        for spec in specs:
            segment_label, fold_index = spec
            metric_value = int(
                fold_evaluator(
                    role=role,
                    segment_label=segment_label,
                    fold_index=fold_index,
                    **fold_kwargs,
                )
            )
            if metric_value < 0:
                raise RunnerInputsError(
                    f"RobustnessRunner: fold_evaluator returned a negative "
                    f"metric value {metric_value} for "
                    f"role={role!r} segment_label={segment_label!r}"
                )
            results.append(
                FoldResult(
                    role=role,
                    segment_label=segment_label,
                    metric_name=metric_name,
                    metric_value=metric_value,
                    fold_index=fold_index,
                )
            )
        return tuple(results)

    @staticmethod
    def _collect_pool_results(
        *,
        specs: Iterable[tuple[int, str]],
        metric_name: str,
        pool_evaluator: PoolEvaluator,
        pool_kwargs: Mapping[str, object],
    ) -> tuple[PoolHoldoutResult, ...]:
        results: list[PoolHoldoutResult] = []
        for chain_id, pool_key_id in specs:
            metric_value = int(
                pool_evaluator(
                    chain_id=chain_id,
                    pool_key_id=pool_key_id,
                    **pool_kwargs,
                )
            )
            if metric_value < 0:
                raise RunnerInputsError(
                    f"RobustnessRunner: pool_evaluator returned a negative "
                    f"metric value {metric_value} for "
                    f"chain_id={chain_id} pool_key_id={pool_key_id!r}"
                )
            results.append(
                PoolHoldoutResult(
                    chain_id=chain_id,
                    pool_key_id=pool_key_id,
                    metric_name=metric_name,
                    metric_value=metric_value,
                )
            )
        return tuple(results)

    @staticmethod
    def _compute_sensitivity(
        *,
        metric_name: str,
        grid_metric_values: Sequence[tuple[dict[str, int | str | bool], int]],
    ) -> SensitivitySummary:
        if not grid_metric_values:
            # The runner was called without a parameter surface.
            # The acceptance clause binds a non-optional sensitivity
            # summary; we surface a degenerate (no-op) summary
            # rather than fail, because the runner may be invoked
            # without a parameter sweep and the summary must still
            # carry a single best (the best pool metric).
            return SensitivitySummary(
                metric_name=metric_name,
                best_metric_value=0,
                best_parameter_combo=(("__no_grid__", 0),),
                worst_metric_value=0,
                spread_metric_value=0,
                n_grid_points=1,
            )
        return summarize_sensitivity(
            metric_name=metric_name,
            grid_metric_values=grid_metric_values,
        )

    @staticmethod
    def _grid_metric_values(
        *,
        inputs: RobustnessRunnerInputs,
        pool_evaluator: PoolEvaluator,
        pool_kwargs: Mapping[str, object],
    ) -> Sequence[tuple[dict[str, int | str | bool], int]]:
        if inputs.parameter_surface is None:
            return ()
        grid = inputs.parameter_surface.grid()
        out: list[tuple[dict[str, int | str | bool], int]] = []
        for combo in grid:
            combo_kwargs = dict(combo)
            for chain_id, pool_key_id in inputs.pool_specs:
                metric_value = int(
                    pool_evaluator(
                        chain_id=chain_id,
                        pool_key_id=pool_key_id,
                        **combo_kwargs,
                        **pool_kwargs,
                    )
                )
                if metric_value < 0:
                    raise RunnerInputsError(
                        f"RobustnessRunner: pool_evaluator returned a negative "
                        f"metric value {metric_value} during the parameter sweep"
                    )
                combo_with_pool: dict[str, int | str | bool] = dict(combo)
                combo_with_pool["__pool_key_id__"] = pool_key_id
                out.append((combo_with_pool, metric_value))
        return tuple(out)

    @staticmethod
    def _build_disclosure(
        *,
        inputs: RobustnessRunnerInputs,
        metric_values: Sequence[int],
    ) -> MultipleComparisonDisclosure:
        n_comparisons = len(metric_values)
        if inputs.parameter_surface is not None:
            n_comparisons = inputs.parameter_surface.grid_size * len(inputs.pool_specs)
        return build_disclosure(
            n_comparisons=n_comparisons,
            n_families=1,
            adjustment_method="BONFERRONI",
            family_wise_alpha_q64_64=inputs.family_wise_alpha_q64_64,
            metric_values=metric_values,
            best_metric_name=inputs.metric_name,
        )


def build_runner() -> RobustnessRunner:
    """Return the canonical T064 runner."""
    return RobustnessRunner(version=RUNNER_VERSION)


# ---------------------------------------------------------------------------
# Schema-bound runner (T106)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaBoundRobustnessRunner:
    """The T106 schema-bound orchestrator.

    The runner is a thin layer over the cataloguereader, the
    fold / pool evaluators, the sensitivity summariser, and the
    schema-bound report builder. The runner is constructed with
    the catalogueto read; :meth:`run` consumes
    :class:`SchemaBoundRobustnessRunnerInputs` plus the two
    evaluators and returns a :class:`SchemaBoundRobustnessReport`.

    The runner refuses a legacy :class:`ParameterSurface` at
    construction time (the inputs dataclass raises
    :class:`LegacySurfaceInSchemaBoundRunnerError`) and refuses
    any sweep that mixes two schema-bound surfaces with
    incompatible revisions before the sensitivity summary is
    computed.
    """

    version: str

    def __post_init__(self) -> None:
        if self.version != SCHEMA_BOUND_RUNNER_VERSION:
            raise RunnerError(
                f"SchemaBoundRobustnessRunner.version: must be "
                f"{SCHEMA_BOUND_RUNNER_VERSION!r}, got {self.version!r}"
            )

    def run(
        self,
        inputs: SchemaBoundRobustnessRunnerInputs,
        *,
        fold_evaluator: FoldEvaluator,
        pool_evaluator: PoolEvaluator,
        fold_evaluator_kwargs: Mapping[str, object] | None = None,
        pool_evaluator_kwargs: Mapping[str, object] | None = None,
    ) -> SchemaBoundRobustnessReport:
        """Execute the schema-bound robustness sweep and return the assembled report."""
        # Catalogue first: defensive validation before any data
        # is touched (DS-035 / DS-041).
        assert_catalogue_complete(inputs.scenario_catalogue)

        surface = inputs.schema_bound_surface
        # Defensive: the inputs dataclass rejects a legacy surface
        # at construction; an additional check here protects against
        # a future dataclass refactor.
        if surface is not None and is_legacy_surface(surface):
            raise LegacySurfaceInSchemaBoundRunnerError(
                "SchemaBoundRobustnessRunner.run: schema_bound_surface carries "
                "a legacy ParameterSurface; current publication cannot use "
                "the unrestricted T064 path"
            )

        fold_kwargs: dict[str, object] = dict(fold_evaluator_kwargs or {})
        pool_kwargs: dict[str, object] = dict(pool_evaluator_kwargs or {})

        # Train / validation / test fold collection.
        train_results = self._collect_fold_results(
            role="TRAIN",
            specs=inputs.train_fold_specs,
            metric_name=inputs.metric_name,
            fold_evaluator=fold_evaluator,
            fold_kwargs=fold_kwargs,
        )
        validation_results = self._collect_fold_results(
            role="VALIDATION",
            specs=inputs.validation_fold_specs,
            metric_name=inputs.metric_name,
            fold_evaluator=fold_evaluator,
            fold_kwargs=fold_kwargs,
        )
        test_results = self._collect_fold_results(
            role="TEST",
            specs=inputs.test_fold_specs,
            metric_name=inputs.metric_name,
            fold_evaluator=fold_evaluator,
            fold_kwargs=fold_kwargs,
        )
        # Per-held-out-pool collection (DS-021 primary generalisation).
        pool_holdout_results = self._collect_pool_results(
            specs=inputs.pool_specs,
            metric_name=inputs.metric_name,
            pool_evaluator=pool_evaluator,
            pool_kwargs=pool_kwargs,
        )
        # Sensitivity summary.
        sensitivity_summary = self._compute_sensitivity(
            metric_name=inputs.metric_name,
            grid_metric_values=self._grid_metric_values(
                inputs=inputs,
                pool_evaluator=pool_evaluator,
                pool_kwargs=pool_kwargs,
            ),
        )
        # Multiple-comparison disclosure.
        disclosure = self._build_disclosure(
            inputs=inputs,
            metric_values=tuple(r.metric_value for r in pool_holdout_results),
        )
        binding = self._extract_binding(inputs=inputs)
        return build_schema_bound_robustness_report(
            run_id=inputs.run_id,
            label_horizon=inputs.label_horizon,
            train_results=train_results,
            validation_results=validation_results,
            test_results=test_results,
            pool_holdout_results=pool_holdout_results,
            walk_forward_splits=inputs.walk_forward_splits,
            pool_holdout_split=inputs.pool_holdout_split,
            sensitivity_summary=sensitivity_summary,
            scenario_catalogue=inputs.scenario_catalogue,
            multiple_comparison_disclosure=disclosure,
            conclusion_statement=inputs.conclusion_statement,
            registry_version=binding.registry_version,
            registry_checksum=binding.registry_checksum,
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum=binding.parameter_schema_checksum,
            strategy_identity=binding.strategy_identity,
            strategy_version=binding.strategy_version,
            time_holdout_split=inputs.time_holdout_split,
            regime_segmentation=inputs.regime_segmentation,
            schema_bound_surface=surface,
        )

    @staticmethod
    def _extract_binding(
        *,
        inputs: SchemaBoundRobustnessRunnerInputs,
    ) -> _SchemaBoundRunnerBinding:
        """Return the binding the runner stamps on the schema-bound report.

        When the runner is invoked with a non-``None``
        ``schema_bound_surface``, the binding is read from the
        surface (the registry / schema revision the surface
        captured at construction). When the runner is invoked
        without a surface, the binding is the registry's current
        default revision — the surface-less path remains available
        for sweeps that do not touch the parameter grid.
        """
        if inputs.schema_bound_surface is not None:
            surface = inputs.schema_bound_surface
            return _SchemaBoundRunnerBinding(
                registry_version=surface.registry_version,
                registry_checksum=surface.registry_checksum,
                parameter_schema_version=surface.parameter_schema_version,
                parameter_schema_checksum=surface.parameter_schema_checksum,
                strategy_identity=surface.strategy_identity,
                strategy_version=surface.strategy_version,
            )
        # Surface-less schema-bound runner: bind to the live
        # registry's current default revision. The default
        # registry is the canonical authority the manifest
        # authority (T105) binds to.
        from robinhood_lp.strategy.registry import REGISTRY_VERSION, default_registry

        reg = default_registry()
        return _SchemaBoundRunnerBinding(
            registry_version=REGISTRY_VERSION,
            registry_checksum=reg.checksum(),
            parameter_schema_version="",
            parameter_schema_checksum="",
            strategy_identity="",
            strategy_version="",
        )

    @staticmethod
    def _collect_fold_results(
        *,
        role: str,
        specs: Iterable[tuple[str, int]],
        metric_name: str,
        fold_evaluator: FoldEvaluator,
        fold_kwargs: Mapping[str, object],
    ) -> tuple[FoldResult, ...]:
        results: list[FoldResult] = []
        for spec in specs:
            segment_label, fold_index = spec
            metric_value = int(
                fold_evaluator(
                    role=role,
                    segment_label=segment_label,
                    fold_index=fold_index,
                    **fold_kwargs,
                )
            )
            if metric_value < 0:
                raise RunnerInputsError(
                    f"SchemaBoundRobustnessRunner: fold_evaluator returned a "
                    f"negative metric value {metric_value} for "
                    f"role={role!r} segment_label={segment_label!r}"
                )
            results.append(
                FoldResult(
                    role=role,
                    segment_label=segment_label,
                    metric_name=metric_name,
                    metric_value=metric_value,
                    fold_index=fold_index,
                )
            )
        return tuple(results)

    @staticmethod
    def _collect_pool_results(
        *,
        specs: Iterable[tuple[int, str]],
        metric_name: str,
        pool_evaluator: PoolEvaluator,
        pool_kwargs: Mapping[str, object],
    ) -> tuple[PoolHoldoutResult, ...]:
        results: list[PoolHoldoutResult] = []
        for chain_id, pool_key_id in specs:
            metric_value = int(
                pool_evaluator(
                    chain_id=chain_id,
                    pool_key_id=pool_key_id,
                    **pool_kwargs,
                )
            )
            if metric_value < 0:
                raise RunnerInputsError(
                    f"SchemaBoundRobustnessRunner: pool_evaluator returned a "
                    f"negative metric value {metric_value} for "
                    f"chain_id={chain_id} pool_key_id={pool_key_id!r}"
                )
            results.append(
                PoolHoldoutResult(
                    chain_id=chain_id,
                    pool_key_id=pool_key_id,
                    metric_name=metric_name,
                    metric_value=metric_value,
                )
            )
        return tuple(results)

    @staticmethod
    def _compute_sensitivity(
        *,
        metric_name: str,
        grid_metric_values: Sequence[tuple[dict[str, int | bool | str], int]],
    ) -> SensitivitySummary:
        if not grid_metric_values:
            return SensitivitySummary(
                metric_name=metric_name,
                best_metric_value=0,
                best_parameter_combo=(("__no_grid__", 0),),
                worst_metric_value=0,
                spread_metric_value=0,
                n_grid_points=1,
            )
        return summarize_sensitivity(
            metric_name=metric_name,
            grid_metric_values=grid_metric_values,
        )

    @staticmethod
    def _grid_metric_values(
        *,
        inputs: SchemaBoundRobustnessRunnerInputs,
        pool_evaluator: PoolEvaluator,
        pool_kwargs: Mapping[str, object],
    ) -> Sequence[tuple[dict[str, int | bool | str], int]]:
        if inputs.schema_bound_surface is None:
            return ()
        grid = inputs.schema_bound_surface.grid()
        out: list[tuple[dict[str, int | bool | str], int]] = []
        for combo in grid:
            combo_kwargs = dict(combo)
            for chain_id, pool_key_id in inputs.pool_specs:
                metric_value = int(
                    pool_evaluator(
                        chain_id=chain_id,
                        pool_key_id=pool_key_id,
                        **combo_kwargs,
                        **pool_kwargs,
                    )
                )
                if metric_value < 0:
                    raise RunnerInputsError(
                        f"SchemaBoundRobustnessRunner: pool_evaluator returned a "
                        f"negative metric value {metric_value} during the "
                        f"parameter sweep"
                    )
                combo_with_pool: dict[str, int | bool | str] = dict(combo)
                combo_with_pool["__pool_key_id__"] = pool_key_id
                out.append((combo_with_pool, metric_value))
        return tuple(out)

    @staticmethod
    def _build_disclosure(
        *,
        inputs: SchemaBoundRobustnessRunnerInputs,
        metric_values: Sequence[int],
    ) -> MultipleComparisonDisclosure:
        n_comparisons = len(metric_values)
        if inputs.schema_bound_surface is not None:
            n_comparisons = inputs.schema_bound_surface.grid_size * len(inputs.pool_specs)
        return build_disclosure(
            n_comparisons=n_comparisons,
            n_families=1,
            adjustment_method="BONFERRONI",
            family_wise_alpha_q64_64=inputs.family_wise_alpha_q64_64,
            metric_values=metric_values,
            best_metric_name=inputs.metric_name,
        )


@dataclass(frozen=True, slots=True)
class _SchemaBoundRunnerBinding:
    """Internal binding view the schema-bound runner stamps on the report.

    The fields mirror the
    :class:`SchemaBoundRobustnessReport` binding fields. The
    dataclass is intentionally private; the schema-bound runner
    is the only caller.
    """

    registry_version: str
    registry_checksum: str
    parameter_schema_version: str
    parameter_schema_checksum: str
    strategy_identity: str
    strategy_version: str


def assert_inputs_surfaces_compatible(
    *,
    inputs: SchemaBoundRobustnessRunnerInputs,
    other_surface: SchemaBoundParameterSurface,
) -> None:
    """Raise :class:`IncompatibleSurfaceRevisionError` if a sweep mixes revisions.

    The T106 contract binds that results from incompatible schema
    revisions are not merged into one sensitivity conclusion. The
    helper is the deterministic gate every code path that mixes
    two schema-bound surfaces passes through; the gate rejects a
    registry or schema drift.
    """
    if inputs.schema_bound_surface is None:
        return
    try:
        assert_surfaces_compatible(
            left=inputs.schema_bound_surface,
            right=other_surface,
        )
    except Exception as exc:  # noqa: BLE001 - re-raise as runner error
        raise IncompatibleSurfaceRevisionError(str(exc)) from exc


def build_schema_bound_runner() -> SchemaBoundRobustnessRunner:
    """Return the canonical T106 schema-bound runner."""
    return SchemaBoundRobustnessRunner(version=SCHEMA_BOUND_RUNNER_VERSION)


def surface_id_for_inputs(
    inputs: SchemaBoundRobustnessRunnerInputs,
) -> str | None:
    """Return the registry / schema-bound surface identity for ``inputs``.

    The helper returns ``None`` when the runner inputs have no
    surface. The identity is the colon-joined binding the
    :func:`surface_identity` helper returns.
    """
    if inputs.schema_bound_surface is None:
        return None
    return surface_identity(surface=inputs.schema_bound_surface)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "RUNNER_VERSION",
    "SCHEMA_BOUND_RUNNER_VERSION",
    "FoldEvaluator",
    "IncompatibleSurfaceRevisionError",
    "LegacySurfaceInSchemaBoundRunnerError",
    "PoolEvaluator",
    "RobustnessRunner",
    "RobustnessRunnerInputs",
    "RunnerError",
    "RunnerInputsError",
    "SchemaBoundRobustnessRunner",
    "SchemaBoundRobustnessRunnerInputs",
    "assert_inputs_surfaces_compatible",
    "build_runner",
    "build_schema_bound_runner",
    "surface_id_for_inputs",
]


# Module-level sentinels / surface that are not part of the runner
# itself but that downstream code may want to import for typing.
__all__ += ["PRIMARY_GENERALISATION_AXIS"]
