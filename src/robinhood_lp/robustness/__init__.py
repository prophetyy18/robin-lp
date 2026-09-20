"""Robustness and anti-overfitting analysis (T064).

This package owns the T064 deliverables:

- :mod:`robinhood_lp.robustness.splits` — split primitives
  (``LabelHorizon``, ``SplitBoundary``, ``WalkForwardSplit``,
  ``TimeHoldoutSplit``, ``PoolHoldoutSplit``). The
  :class:`LabelHorizon` is the single source of the purge/embargo
  length; a hand-chosen length is rejected at construction.

- :mod:`robinhood_lp.robustness.scenarios` — the scenario
  catalogue. Each entry declares a halt-or-degrade outcome before
  the run starts (DS-035). The catalogue is the only place those
  outcomes are recorded.

- :mod:`robinhood_lp.robustness.degradation` — DS-041 enforcement.
  Every ``DEGRADE`` fallback must resolve to a registered,
  deterministic rule-based fallback; undefined behaviour and
  forced trades are forbidden.

- :mod:`robinhood_lp.robustness.surfaces` — parameter surfaces
  and pool/regime segmentation. The surface enumerates the grid
  the runner sweeps; the segmentation is the contract the runner
  reports metrics against.

- :mod:`robinhood_lp.robustness.disclosure` — the
  multiple-comparison disclosure. The disclosure reports the
  number of comparisons, the adjustment method, and the
  sensitivity spread beside the best run.

- :mod:`robinhood_lp.robustness.reports` — the assembled
  :class:`RobustnessReport`. The report separates train /
  validation / test, names pool holdout as the primary
  generalisation test, carries the per-held-out-pool results,
  and refuses to drop a result post hoc.

- :mod:`robinhood_lp.robustness.runner` — the orchestrator.
  The runner reads the catalogue before any data is touched,
  executes the sweep, and assembles the report.

The package is intentionally narrow: it imports the standard
library and the in-package modules only. It does not import the
backtest engine, the manifest layer, RPC, storage, signing,
execution, or presentation code. The robustness runner's
orchestration callables are injected by the caller; the package
itself does not call the engine.

References:

- T064 — robustness and anti-overfitting analysis.
- ``docs/spec/research/DATASET_AND_EVALUATION.md`` DS-021,
  DS-022, DS-035, DS-041.
"""

from __future__ import annotations

from robinhood_lp.robustness import (
    degradation,  # noqa: F401
    disclosure,  # noqa: F401
    reports,  # noqa: F401
    runner,  # noqa: F401
    scenarios,  # noqa: F401
    splits,  # noqa: F401
    surfaces,  # noqa: F401
)
from robinhood_lp.robustness.degradation import (
    DEGRADATION_VERSION,
    FORBIDDEN_FALLBACK_NAMES,
    VALID_FALLBACK_NAMES,
    DegradationError,
    DegradationPolicy,
    FallbackRule,
    ForbiddenFallbackError,
    UnregisteredFallbackError,
    assert_no_degrade_falls_through,
    build_degradation_policy,
    default_degradation_policy,
)
from robinhood_lp.robustness.disclosure import (
    DISCLOSURE_VERSION,
    VALID_ADJUSTMENT_METHODS,
    DisclosureError,
    DisclosureInputsError,
    InvalidAdjustmentMethodError,
    MultipleComparisonDisclosure,
    build_disclosure,
)
from robinhood_lp.robustness.reports import (
    PRIMARY_GENERALISATION_AXIS,
    REPORTS_VERSION,
    VALID_FOLD_RESULT_ROLES,
    FoldResult,
    FoldResultError,
    InvalidReportAxisError,
    MissingPrimaryGeneralisationError,
    PoolHoldoutResult,
    RobustnessReport,
    RobustnessReportError,
    assert_train_validation_test_separation,
    build_robustness_report,
)
from robinhood_lp.robustness.runner import (
    RUNNER_VERSION,
    FoldEvaluator,
    PoolEvaluator,
    RobustnessRunner,
    RobustnessRunnerInputs,
    RunnerError,
    RunnerInputsError,
    build_runner,
)
from robinhood_lp.robustness.scenarios import (
    SCENARIOS_VERSION,
    VALID_CATEGORIES,
    VALID_OUTCOME_KINDS,
    InvalidScenarioCategoryError,
    InvalidScenarioOutcomeError,
    MissingScenarioOutcomeError,
    Scenario,
    ScenarioCatalogue,
    ScenarioError,
    assert_catalogue_complete,
    build_scenario_catalogue,
    default_stress_scenario_catalogue,
    filter_by_categories,
)
from robinhood_lp.robustness.splits import (
    SPLITS_VERSION,
    VALID_AXIS_KINDS,
    VALID_FOLD_ROLES,
    VALID_HORIZON_UNITS,
    VALID_RECORDED_SOURCES,
    VALID_WINDOW_KINDS,
    HandChosenEmbargoError,
    InvalidLabelHorizonError,
    InvalidSplitBoundaryError,
    LabelHorizon,
    PoolHoldoutFold,
    PoolHoldoutSplit,
    RobustnessSplitError,
    SplitBoundary,
    TimeFold,
    TimeHoldoutSplit,
    WalkForwardSplit,
    build_pool_holdout_split,
    build_time_holdout_split,
    build_walk_forward_splits,
    make_boundary_id,
)
from robinhood_lp.robustness.surfaces import (
    SURFACES_VERSION,
    VALID_AXIS_VALUE_KINDS,
    VALID_REGIME_LABELS,
    InvalidSegmentationError,
    ParameterAxis,
    ParameterSurface,
    RegimeSegment,
    RegimeSegmentation,
    SensitivitySummary,
    SurfaceError,
    build_parameter_surface,
    build_regime_segmentation,
    summarize_sensitivity,
)

__all__ = [
    # versions
    "DEGRADATION_VERSION",
    "DISCLOSURE_VERSION",
    "PRIMARY_GENERALISATION_AXIS",
    "REPORTS_VERSION",
    "RUNNER_VERSION",
    "SCENARIOS_VERSION",
    "SPLITS_VERSION",
    "SURFACES_VERSION",
    # vocabularies
    "FORBIDDEN_FALLBACK_NAMES",
    "VALID_ADJUSTMENT_METHODS",
    "VALID_AXIS_KINDS",
    "VALID_AXIS_VALUE_KINDS",
    "VALID_CATEGORIES",
    "VALID_FALLBACK_NAMES",
    "VALID_FOLD_RESULT_ROLES",
    "VALID_FOLD_ROLES",
    "VALID_HORIZON_UNITS",
    "VALID_OUTCOME_KINDS",
    "VALID_RECORDED_SOURCES",
    "VALID_REGIME_LABELS",
    "VALID_WINDOW_KINDS",
    # errors
    "DegradationError",
    "DisclosureError",
    "DisclosureInputsError",
    "FoldResultError",
    "ForbiddenFallbackError",
    "HandChosenEmbargoError",
    "InvalidAdjustmentMethodError",
    "InvalidLabelHorizonError",
    "InvalidReportAxisError",
    "InvalidScenarioCategoryError",
    "InvalidScenarioOutcomeError",
    "InvalidSegmentationError",
    "InvalidSplitBoundaryError",
    "MissingPrimaryGeneralisationError",
    "MissingScenarioOutcomeError",
    "RobustnessReportError",
    "RobustnessSplitError",
    "RunnerError",
    "RunnerInputsError",
    "ScenarioError",
    "SurfaceError",
    "UnregisteredFallbackError",
    # dataclasses / value objects
    "DegradationPolicy",
    "FallbackRule",
    "FoldResult",
    "LabelHorizon",
    "MultipleComparisonDisclosure",
    "ParameterAxis",
    "ParameterSurface",
    "PoolHoldoutFold",
    "PoolHoldoutResult",
    "PoolHoldoutSplit",
    "RegimeSegment",
    "RegimeSegmentation",
    "RobustnessReport",
    "RobustnessRunner",
    "RobustnessRunnerInputs",
    "Scenario",
    "ScenarioCatalogue",
    "SensitivitySummary",
    "SplitBoundary",
    "TimeFold",
    "TimeHoldoutSplit",
    "WalkForwardSplit",
    # callables
    "FoldEvaluator",
    "PoolEvaluator",
    # builders
    "assert_catalogue_complete",
    "assert_no_degrade_falls_through",
    "assert_train_validation_test_separation",
    "build_degradation_policy",
    "build_disclosure",
    "build_parameter_surface",
    "build_pool_holdout_split",
    "build_regime_segmentation",
    "build_robustness_report",
    "build_runner",
    "build_scenario_catalogue",
    "build_time_holdout_split",
    "build_walk_forward_splits",
    "default_degradation_policy",
    "default_stress_scenario_catalogue",
    "filter_by_categories",
    "make_boundary_id",
    "summarize_sensitivity",
]

__version__: str = "0.0.0"
