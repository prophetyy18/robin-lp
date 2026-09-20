"""Versioned threshold experiments and provisional candidate review (T066).

This package owns the experiment / review layer T066 binds the
candidate-lock gate to. The package is intentionally narrow: it
imports the standard library, the in-package
:mod:`robinhood_lp.robustness` primitives (T064 splits, parameter
surface, sensitivity summary), and the in-package modules defined
here. It does not import the backtest engine, the manifest layer,
RPC, storage, signing, execution, or presentation code.

The package exports:

- :mod:`robinhood_lp.experiments.candidate_lock` — the immutable
  :class:`CandidateLock` record and its content hash / I/O helpers.
  A candidate version's lock is written *before* the test fold is
  read; a parameter change yields a new lock, not an edit.

- :mod:`robinhood_lp.experiments.search` — the
  :class:`ParameterSearchManifest` and the
  :func:`compute_parameter_set_version` deterministic version
  string. Every record (winning, losing, rejected, failed) is
  preserved.

- :mod:`robinhood_lp.experiments.distributions` — the integer
  :class:`DistributionSummary` for PnL, profit probability,
  Expected Shortfall, boundary, time-in-Range, fee and cost. Every
  distribution carries the
  ``EXPERIMENTAL_NOT_LIVE_APPROVED`` flag and the
  ``USDG_CASH`` primary benchmark.

- :mod:`robinhood_lp.experiments.stability` — the
  :class:`NeighborhoodStabilityReport` and the
  :class:`StressReport`. The stability report enumerates every
  neighborhood cell; the stress report enumerates every T064
  catalogue scenario.

- :mod:`robinhood_lp.experiments.harness` — the
  :class:`ExperimentHarness` orchestrator and the
  :class:`ExperimentPlan` configuration type. The harness writes
  the candidate lock before reading the test fold and refuses to
  evaluate when the lock is missing or tampered.

References:

- T066 — Build versioned threshold experiments and provisional
  candidate review.
- T064 — robustness splits, parameter surface, sensitivity
  summary.
- T065 — the USDG-first adaptive-Range strategy the harness can
  parameterise.
- `docs/spec/strategy/STRATEGY_ECONOMICS.md` §6 (threshold /
  experiment discipline).
- `docs/spec/research/DATASET_AND_EVALUATION.md` DS-031
  (candidate locking before test fold).
"""

from __future__ import annotations

from robinhood_lp.experiments.candidate_lock import (
    CANDIDATE_LOCK_VERSION,
    REQUIRED_ELIMINATION_RULES,
    UNKNOWN_CODE_REVISION,
    VALID_COST_ASSUMPTIONS,
    CandidateLock,
    CandidateLockError,
    CandidateLockHashMismatchError,
    CandidateLockMissingRequiredRuleError,
    CandidateLockNotFoundError,
    InvalidCandidateLockError,
    build_candidate_lock,
    compute_candidate_lock_content_hash,
    load_candidate_lock,
    write_candidate_lock,
)
from robinhood_lp.experiments.distributions import (
    DISTRIBUTIONS_VERSION,
    Q64_SCALE,
    VALID_METRIC_KINDS,
    DistributionError,
    DistributionSummary,
    EmptyDistributionError,
    InvalidDistributionError,
    boundary_touch_probability_q64_64,
    build_distribution_summary,
    profit_probability_q64_64,
)
from robinhood_lp.experiments.harness import (
    EXPERIMENT_HARNESS_VERSION,
    VALID_RECOMMENDATIONS,
    CandidateLockHashMismatchOnLoadError,
    ExperimentHarness,
    ExperimentHarnessError,
    ExperimentHarnessResult,
    ExperimentPlan,
    FoldEvaluator,
    HarnessInputsError,
    MissingCandidateLockError,
    NoCandidateEligibleError,
    PoolEvaluator,
    ProvisionalRecommendation,
    StressEvaluator,
    build_no_trade_recommendation,
    build_provisional_recommendation,
)
from robinhood_lp.experiments.search import (
    PARAMETER_SEARCH_VERSION,
    VALID_ELIMINATION_REASONS,
    VALID_RUN_STATUSES,
    EmptySearchError,
    InvalidParameterSearchFieldError,
    ParameterSearchError,
    ParameterSearchManifest,
    SearchRunRecord,
    build_parameter_search_manifest,
    compute_parameter_set_version,
)
from robinhood_lp.experiments.stability import (
    Q64_SCALE as STABILITY_Q64_SCALE,
)
from robinhood_lp.experiments.stability import (
    STABILITY_VERSION,
    VALID_STRESS_OUTCOMES,
    EmptyStabilityError,
    InvalidStabilityError,
    NeighborhoodEntry,
    NeighborhoodStabilityReport,
    StabilityError,
    StressReport,
    StressScenarioOutcome,
    build_neighborhood_stability_report,
    build_stress_report,
)

__all__ = [
    # candidate_lock
    "CANDIDATE_LOCK_VERSION",
    "CandidateLock",
    "CandidateLockError",
    "CandidateLockHashMismatchError",
    "CandidateLockMissingRequiredRuleError",
    "CandidateLockNotFoundError",
    "InvalidCandidateLockError",
    "REQUIRED_ELIMINATION_RULES",
    "UNKNOWN_CODE_REVISION",
    "VALID_COST_ASSUMPTIONS",
    "build_candidate_lock",
    "compute_candidate_lock_content_hash",
    "load_candidate_lock",
    "write_candidate_lock",
    # search
    "EmptySearchError",
    "InvalidParameterSearchFieldError",
    "PARAMETER_SEARCH_VERSION",
    "ParameterSearchError",
    "ParameterSearchManifest",
    "SearchRunRecord",
    "VALID_ELIMINATION_REASONS",
    "VALID_RUN_STATUSES",
    "build_parameter_search_manifest",
    "compute_parameter_set_version",
    # distributions
    "DISTRIBUTIONS_VERSION",
    "DistributionError",
    "DistributionSummary",
    "EmptyDistributionError",
    "InvalidDistributionError",
    "Q64_SCALE",
    "VALID_METRIC_KINDS",
    "boundary_touch_probability_q64_64",
    "build_distribution_summary",
    "profit_probability_q64_64",
    # stability
    "EmptyStabilityError",
    "InvalidStabilityError",
    "NeighborhoodEntry",
    "NeighborhoodStabilityReport",
    "STABILITY_Q64_SCALE",
    "STABILITY_VERSION",
    "StabilityError",
    "StressReport",
    "StressScenarioOutcome",
    "VALID_STRESS_OUTCOMES",
    "build_neighborhood_stability_report",
    "build_stress_report",
    # harness
    "EXPERIMENT_HARNESS_VERSION",
    "ExperimentHarness",
    "ExperimentHarnessError",
    "ExperimentHarnessResult",
    "ExperimentPlan",
    "FoldEvaluator",
    "HarnessInputsError",
    "NoCandidateEligibleError",
    "PoolEvaluator",
    "ProvisionalRecommendation",
    "StressEvaluator",
    "CandidateLockHashMismatchOnLoadError",
    "MissingCandidateLockError",
    "VALID_RECOMMENDATIONS",
    "build_no_trade_recommendation",
    "build_provisional_recommendation",
]

__version__: str = "0.0.0"
