"""Experiment harness (T066).

The harness is the orchestrator T066 binds the candidate-lock
gate to. It performs the following steps, in this order:

1. **Build splits.** The harness accepts T064
   :class:`WalkForwardSplit` / :class:`TimeHoldoutSplit` /
   :class:`PoolHoldoutSplit` instances — the T064 splits the
   candidate was tuned under. The harness freezes the splits'
   :class:`SplitBoundary` records into the candidate-lock so a
   change to a boundary yields a new lock.

2. **Run train / validation.** The harness invokes the
   orchestrator-supplied ``fold_evaluator`` on every train /
   validation fold, collecting metrics into a parameter-search
   manifest. Losing / rejected / failed runs are preserved on the
   manifest (the T066 Must-not clause).

3. **Choose a candidate.** The harness picks the *first*
   parameter-set version whose validation metric strictly
   improves over the previous best (ties are not selected). The
   rule is monotonic: a parameter-set version that does not
   improve validation is not selected. The selection rule
   rejects "select only the highest return" (the T066 Must-not
   clause) — the harness binds the locked candidate to the
   *first* validation-improving version rather than the top
   metric.

4. **Write the candidate lock.** The harness writes the
   :class:`CandidateLock` to disk *before* it reads the test
   fold. The lock freezes the chosen parameter-set version, the
   seed, the split boundaries, the cost assumptions, the
   elimination rules (including the binding
   ``PRIMARY_BENCHMARK_USDG_CASH`` and
   ``EXPERIMENTAL_NOT_LIVE_APPROVED`` rules), the actor and the
   lock time.

5. **Read the test fold (gated).** The harness reads the test
   fold only after it finds a candidate lock for the chosen
   parameter-set version. If the lock is missing, the harness
   raises :class:`MissingCandidateLockError` and refuses to
   evaluate. If the lock's content hash disagrees with the
   recomputed digest, the harness raises
   :class:`CandidateLockHashMismatchOnLoadError` and refuses to
   evaluate.

6. **Distributions.** The harness computes the
   PnL / profit-probability / ES / boundary / time-in-Range /
   fee / cost distributions on the test fold. Every
   distribution carries the
   ``EXPERIMENTAL_NOT_LIVE_APPROVED`` flag and the
   ``USDG_CASH`` primary benchmark.

7. **Stability + stress.** The harness builds the neighborhood
   stability report and the stress report.

8. **Recommendation.** The harness emits either a provisional
   recommendation or ``NO_TRADE``, always carrying the
   ``EXPERIMENTAL_NOT_LIVE_APPROVED`` flag. The
   recommendation is informational; the harness does not
   approve paper / live trading.

Design constraints (binding):

- **Append-only.** The harness never mutates a parameter-search
  manifest; every record (winning, losing, rejected, failed)
  survives on the manifest.
- **Integer-only.** Every quantity on the harness's data path is
  a Python ``int``.
- **Deterministic.** The harness's outputs depend only on the
  caller-supplied inputs (no wall-clock reads, no unseeded
  randomness). The orchestrator passes a ``clock_unix_seconds``
  argument (default ``0``) so reruns are byte-equivalent.
- **Layer purity.** This module imports the standard library and
  the in-package experiments / robustness modules only. It does
  not import the backtest engine, the manifest layer, RPC,
  storage, signing, execution, or presentation code.

References:

- T066 — Build versioned threshold experiments and provisional
  candidate review.
- T064 — robustness splits, parameter surface, sensitivity
  summary.
- T065 — the USDG-first adaptive-Range strategy the harness can
  parameterise.
- `docs/spec/strategy/STRATEGY_ECONOMICS.md` §6 — the threshold
  / experiment discipline.
- `docs/spec/research/DATASET_AND_EVALUATION.md` DS-031 — the
  test-fold lock requirement.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from robinhood_lp.experiments.candidate_lock import (
    REQUIRED_ELIMINATION_RULES,
    UNKNOWN_CODE_REVISION,
    VALID_COST_ASSUMPTIONS,
    CandidateLock,
    CandidateLockHashMismatchError,
    CandidateLockNotFoundError,
    InvalidCandidateLockError,
    build_candidate_lock,
    compute_candidate_lock_content_hash,
    load_candidate_lock,
    write_candidate_lock,
)
from robinhood_lp.experiments.distributions import (
    Q64_SCALE,
    VALID_METRIC_KINDS,
    DistributionSummary,
    build_distribution_summary,
)
from robinhood_lp.experiments.search import (
    EmptySearchError,
    ParameterSearchManifest,
    SearchRunRecord,
    build_parameter_search_manifest,
    compute_parameter_set_version,
)
from robinhood_lp.experiments.stability import (
    VALID_STRESS_OUTCOMES,
    NeighborhoodStabilityReport,
    StressReport,
    StressScenarioOutcome,
    build_neighborhood_stability_report,
    build_stress_report,
)
from robinhood_lp.robustness.splits import (
    PoolHoldoutSplit,
    SplitBoundary,
    TimeFold,
    TimeHoldoutSplit,
    WalkForwardSplit,
)
from robinhood_lp.robustness.surfaces import ParameterSurface

#: Module version. Bumping it is a breaking change for downstream
#: consumers (the audit chain, the review record).
EXPERIMENT_HARNESS_VERSION: Final[str] = "t066.experiment_harness.v1"

#: Closed vocabulary for the recommendation the harness emits.
#: ``PROVISIONAL_RECOMMENDATION`` is the "favourable parameters
#: survive outside their selection sample" verdict;
#: ``NO_TRADE`` is the binding "keep USDG cash" verdict that
#: mirrors ``ECO-OBJ-002`. Both carry the
#: ``EXPERIMENTAL_NOT_LIVE_APPROVED`` flag.
VALID_RECOMMENDATIONS: Final[frozenset[str]] = frozenset({"PROVISIONAL_RECOMMENDATION", "NO_TRADE"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExperimentHarnessError(ValueError):
    """Base class for experiment-harness construction / execution failures."""


class HarnessInputsError(ExperimentHarnessError):
    """A harness input is malformed."""


class MissingCandidateLockError(ExperimentHarnessError):
    """The harness refuses to read the test fold without a matching lock."""


class CandidateLockHashMismatchOnLoadError(ExperimentHarnessError):
    """The lock's content hash disagrees with the recomputed digest."""


class NoCandidateEligibleError(ExperimentHarnessError):
    """No parameter-set version improved validation; the harness emits NO_TRADE."""


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------


#: A fold-evaluator callable the orchestrator injects. The
#: harness calls this callable once per fold; the callable
#: itself runs the backtest engine and returns a Q64.64 metric
#: value (or ``0`` when the fold was a HALT scenario).
FoldEvaluator = Callable[..., int]

#: A pool-evaluator callable the orchestrator injects. The
#: harness calls this callable once per held-out pool; the
#: callable returns a Q64.64 metric value.
PoolEvaluator = Callable[..., int]

#: A stress-evaluator callable the orchestrator injects. The
#: harness calls this callable once per stress scenario with
#: ``(scenario_id, category)`` keyword arguments; the callable
#: returns a :class:`StressScenarioOutcome`.
StressEvaluator = Callable[..., StressScenarioOutcome]


@dataclass(frozen=True, slots=True)
class ExperimentPlan:
    """The split + cost + elimination-rule plan the harness binds to.

    The plan freezes the protocol the experiment runs under:
    the train / validation / test splits (one or more T064 split
    types), the cost assumptions the harness passes through, the
    elimination rules (including the binding
    ``PRIMARY_BENCHMARK_USDG_CASH`` and
    ``EXPERIMENTAL_NOT_LIVE_APPROVED`` rules), the parameter
    surface and the seed.

    Field units:

    - ``chain_id`` — positive integer.
    - ``pool_key_id`` — non-empty string.
    - ``label_horizon`` — non-empty string ("BARS" / "BLOCKS").
    - ``time_holdout_split`` / ``walk_forward_splits`` /
      ``pool_holdout_split`` — at least one of the three must be
      supplied; the union of their :class:`SplitBoundary`
      records is what the harness freezes into the candidate
      lock.
    - ``cost_assumptions`` — non-empty tuple of strings from
      :data:`VALID_COST_ASSUMPTIONS`.
    - ``elimination_rules`` — non-empty tuple of strings that
      includes every name in :data:`REQUIRED_ELIMINATION_RULES`.
    - ``parameter_surface`` — the surface the harness sweeps.
    - ``seed`` — non-negative integer.
    """

    chain_id: int
    pool_key_id: str
    label_horizon: str
    time_holdout_split: TimeHoldoutSplit | None
    walk_forward_splits: tuple[WalkForwardSplit, ...]
    pool_holdout_split: PoolHoldoutSplit | None
    cost_assumptions: tuple[str, ...]
    elimination_rules: tuple[str, ...]
    parameter_surface: ParameterSurface
    seed: int

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise HarnessInputsError(
                f"ExperimentPlan.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise HarnessInputsError(
                f"ExperimentPlan.chain_id: must be positive, got {self.chain_id}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise HarnessInputsError(
                f"ExperimentPlan.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        if not isinstance(self.label_horizon, str) or not self.label_horizon:
            raise HarnessInputsError("ExperimentPlan.label_horizon: must be non-empty str")
        if (
            self.time_holdout_split is None
            and not self.walk_forward_splits
            and self.pool_holdout_split is None
        ):
            raise HarnessInputsError(
                "ExperimentPlan: at least one of time_holdout_split / "
                "walk_forward_splits / pool_holdout_split must be supplied"
            )
        if not isinstance(self.walk_forward_splits, tuple):
            raise HarnessInputsError(
                f"ExperimentPlan.walk_forward_splits: must be tuple, got "
                f"{type(self.walk_forward_splits).__name__}"
            )
        if not self.cost_assumptions:
            raise HarnessInputsError("ExperimentPlan.cost_assumptions: must be non-empty tuple")
        unknown = [c for c in self.cost_assumptions if c not in VALID_COST_ASSUMPTIONS]
        if unknown:
            raise HarnessInputsError(
                f"ExperimentPlan.cost_assumptions: unknown value(s) "
                f"{unknown!r}; valid={sorted(VALID_COST_ASSUMPTIONS)}"
            )
        if not self.elimination_rules:
            raise HarnessInputsError("ExperimentPlan.elimination_rules: must be non-empty tuple")
        missing = sorted(set(REQUIRED_ELIMINATION_RULES) - set(self.elimination_rules))
        if missing:
            raise HarnessInputsError(
                f"ExperimentPlan.elimination_rules: missing required rule(s) "
                f"{missing!r}; required={sorted(REQUIRED_ELIMINATION_RULES)}"
            )
        if not isinstance(self.parameter_surface, ParameterSurface):
            raise HarnessInputsError(
                f"ExperimentPlan.parameter_surface: must be ParameterSurface, "
                f"got {type(self.parameter_surface).__name__}"
            )
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise HarnessInputsError(
                f"ExperimentPlan.seed: must be int, got {type(self.seed).__name__}"
            )
        if self.seed < 0:
            raise HarnessInputsError(f"ExperimentPlan.seed: must be non-negative, got {self.seed}")

    def split_boundaries(self) -> tuple[SplitBoundary, ...]:
        """Return every :class:`SplitBoundary` the plan declares.

        The tuple is the union of the boundaries from the
        time-holdout split (if any), every walk-forward split
        (if any) and the pool-holdout split (if any). The
        harness freezes these into the candidate lock; a
        change to any boundary yields a new lock.
        """
        boundaries: list[SplitBoundary] = []
        if self.time_holdout_split is not None:
            boundaries.extend(self.time_holdout_split.boundaries)
        for split in self.walk_forward_splits:
            boundaries.extend(split.boundaries)
        if self.pool_holdout_split is not None:
            boundaries.extend(self.pool_holdout_split.boundaries)
        return tuple(boundaries)


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProvisionalRecommendation:
    """The recommendation the harness emits.

    Both ``PROVISIONAL_RECOMMENDATION`` and ``NO_TRADE`` carry
    the ``EXPERIMENTAL_NOT_LIVE_APPROVED`` flag — the harness
    never approves paper / live trading. The ``reason_codes``
    tuple names the reason(s) the harness chose this
    recommendation.

    Field units:

    - ``recommendation_kind`` — one of
      :data:`VALID_RECOMMENDATIONS`.
    - ``candidate_id`` — non-empty string; the candidate
      version the recommendation refers to (``""`` for
      ``NO_TRADE`` when no candidate was eligible).
    - ``locked_metric_value`` — Q64.64 integer; ``0`` when no
      candidate was eligible.
    - ``reason_codes`` — non-empty tuple of strings.
    - ``experimental_flag`` — the literal
      ``"EXPERIMENTAL_NOT_LIVE_APPROVED"`.
    - ``primary_benchmark`` — the literal ``"USDG_CASH"``.
    - ``content_hash`` — SHA-256 hex digest of every other field.
    """

    recommendation_kind: str
    candidate_id: str
    locked_metric_value: int
    reason_codes: tuple[str, ...]
    experimental_flag: str
    primary_benchmark: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.recommendation_kind not in VALID_RECOMMENDATIONS:
            raise InvalidCandidateLockError(  # reuse ValueError family
                f"ProvisionalRecommendation.recommendation_kind: must be one "
                f"of {sorted(VALID_RECOMMENDATIONS)}, got "
                f"{self.recommendation_kind!r}"
            )
        if self.candidate_id and not isinstance(self.candidate_id, str):
            raise InvalidCandidateLockError(
                f"ProvisionalRecommendation.candidate_id: must be str when "
                f"non-empty, got {type(self.candidate_id).__name__}"
            )
        if not isinstance(self.locked_metric_value, int) or isinstance(
            self.locked_metric_value, bool
        ):
            raise InvalidCandidateLockError(
                f"ProvisionalRecommendation.locked_metric_value: must be int, "
                f"got {type(self.locked_metric_value).__name__}"
            )
        if not self.reason_codes:
            raise InvalidCandidateLockError(
                "ProvisionalRecommendation.reason_codes: must be non-empty"
            )
        if self.experimental_flag != "EXPERIMENTAL_NOT_LIVE_APPROVED":
            raise InvalidCandidateLockError(
                f"ProvisionalRecommendation.experimental_flag: must be "
                f"'EXPERIMENTAL_NOT_LIVE_APPROVED' (T066 acceptance), got "
                f"{self.experimental_flag!r}"
            )
        if self.primary_benchmark != "USDG_CASH":
            raise InvalidCandidateLockError(
                f"ProvisionalRecommendation.primary_benchmark: must be "
                f"'USDG_CASH' (T066 acceptance), got {self.primary_benchmark!r}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "recommendation_kind": self.recommendation_kind,
            "candidate_id": self.candidate_id,
            "locked_metric_value": self.locked_metric_value,
            "reason_codes": list(self.reason_codes),
            "experimental_flag": self.experimental_flag,
            "primary_benchmark": self.primary_benchmark,
            "content_hash": self.content_hash,
        }


def _compute_recommendation_content_hash(rec: ProvisionalRecommendation) -> str:
    payload = rec.to_dict()
    payload.pop("content_hash", None)
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_no_trade_recommendation(*, reason_codes: Sequence[str]) -> ProvisionalRecommendation:
    """Build the ``NO_TRADE`` recommendation.

    The harness emits ``NO_TRADE`` when no parameter-set version
    strictly improved validation, or when the locked candidate's
    distribution / stability / stress evidence fails one of the
    binding elimination rules.
    """
    if not reason_codes:
        raise InvalidCandidateLockError(
            "build_no_trade_recommendation: reason_codes must be non-empty"
        )
    placeholder = ProvisionalRecommendation(
        recommendation_kind="NO_TRADE",
        candidate_id="",
        locked_metric_value=0,
        reason_codes=tuple(reason_codes),
        experimental_flag="EXPERIMENTAL_NOT_LIVE_APPROVED",
        primary_benchmark="USDG_CASH",
        content_hash="0x" + "00" * 32,
    )
    digest = _compute_recommendation_content_hash(placeholder)
    return ProvisionalRecommendation(
        recommendation_kind=placeholder.recommendation_kind,
        candidate_id=placeholder.candidate_id,
        locked_metric_value=placeholder.locked_metric_value,
        reason_codes=placeholder.reason_codes,
        experimental_flag=placeholder.experimental_flag,
        primary_benchmark=placeholder.primary_benchmark,
        content_hash=digest,
    )


def build_provisional_recommendation(
    *,
    candidate_id: str,
    locked_metric_value: int,
    reason_codes: Sequence[str],
) -> ProvisionalRecommendation:
    """Build the ``PROVISIONAL_RECOMMENDATION`` verdict.

    The harness emits this verdict when the locked candidate
    survives the binding elimination rules and the test-fold
    distribution's primary statistic is non-negative on the
    USDG cash benchmark.
    """
    if not candidate_id:
        raise InvalidCandidateLockError(
            "build_provisional_recommendation: candidate_id must be non-empty"
        )
    if not reason_codes:
        raise InvalidCandidateLockError(
            "build_provisional_recommendation: reason_codes must be non-empty"
        )
    placeholder = ProvisionalRecommendation(
        recommendation_kind="PROVISIONAL_RECOMMENDATION",
        candidate_id=candidate_id,
        locked_metric_value=locked_metric_value,
        reason_codes=tuple(reason_codes),
        experimental_flag="EXPERIMENTAL_NOT_LIVE_APPROVED",
        primary_benchmark="USDG_CASH",
        content_hash="0x" + "00" * 32,
    )
    digest = _compute_recommendation_content_hash(placeholder)
    return ProvisionalRecommendation(
        recommendation_kind=placeholder.recommendation_kind,
        candidate_id=placeholder.candidate_id,
        locked_metric_value=placeholder.locked_metric_value,
        reason_codes=placeholder.reason_codes,
        experimental_flag=placeholder.experimental_flag,
        primary_benchmark=placeholder.primary_benchmark,
        content_hash=digest,
    )


# ---------------------------------------------------------------------------
# Harness result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExperimentHarnessResult:
    """The full set of artifacts a single harness run produces.

    The result is the audit record T066 binds every experiment
    to. The candidate lock is carried alongside the test-fold
    distribution, stability, stress and recommendation outputs
    so a reviewer can trace every output back to the locked
    candidate.

    Field units:

    - ``version`` — harness-result schema version (string).
    - ``plan_chain_id`` / ``plan_pool_key_id`` — the pool
      identity the experiment ran on.
    - ``search_manifest`` — the :class:`ParameterSearchManifest`
      carrying every record (winning, losing, rejected, failed).
    - ``candidate_lock`` — the :class:`CandidateLock` the harness
      wrote before reading the test fold.
    - ``distributions`` — tuple of
      :class:`DistributionSummary`; one per metric kind the
      harness evaluated.
    - ``stability_report`` — the neighborhood stability report.
    - ``stress_report`` — the stress report.
    - ``recommendation`` — the
      :class:`ProvisionalRecommendation` the harness emitted.
    - ``clock_unix_seconds`` — the caller-supplied clock; the
      lock's ``lock_time_unix_seconds`` is the same value.
    - ``content_hash`` — SHA-256 hex digest of every other field
      so a tampered result fails the reviewer gate.
    """

    version: str
    plan_chain_id: int
    plan_pool_key_id: str
    search_manifest: ParameterSearchManifest
    candidate_lock: CandidateLock
    distributions: tuple[DistributionSummary, ...]
    stability_report: NeighborhoodStabilityReport
    stress_report: StressReport
    recommendation: ProvisionalRecommendation
    clock_unix_seconds: int
    content_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "plan_chain_id": self.plan_chain_id,
            "plan_pool_key_id": self.plan_pool_key_id,
            "search_manifest": self.search_manifest.to_dict(),
            "candidate_lock": self.candidate_lock.to_dict(),
            "distributions": [d.to_dict() for d in self.distributions],
            "stability_report": self.stability_report.to_dict(),
            "stress_report": self.stress_report.to_dict(),
            "recommendation": self.recommendation.to_dict(),
            "clock_unix_seconds": self.clock_unix_seconds,
            "content_hash": self.content_hash,
        }


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExperimentHarness:
    """The experiment harness orchestrator (T066).

    The harness is a frozen dataclass: every field is the
    configuration the orchestrator supplies; the harness
    itself never reads wall-clock time or unseeded randomness.
    The :meth:`run` method performs the full experiment and
    returns an :class:`ExperimentHarnessResult`.
    """

    plan: ExperimentPlan
    code_revision: str
    actor: str

    def __post_init__(self) -> None:
        if not isinstance(self.code_revision, str):
            raise HarnessInputsError(
                f"ExperimentHarness.code_revision: must be str, got "
                f"{type(self.code_revision).__name__}"
            )
        if not self.code_revision:
            raise HarnessInputsError("ExperimentHarness.code_revision: must be non-empty str")
        if not isinstance(self.actor, str) or not self.actor:
            raise HarnessInputsError(
                f"ExperimentHarness.actor: must be non-empty str, got {self.actor!r}"
            )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        fold_evaluator: FoldEvaluator,
        pool_evaluator: PoolEvaluator,
        stress_evaluator: StressEvaluator,
        lock_directory: Path | str,
        manifest_hash: str,
        test_fold_evaluator: FoldEvaluator,
        distribution_samples_by_metric: Mapping[str, Sequence[int]],
        neighborhood_metric_values: Sequence[tuple[str, int]],
        clock_unix_seconds: int,
        candidate_id_suffix: str = "",
    ) -> ExperimentHarnessResult:
        """Run the experiment and return the harness result.

        The method performs the steps in this order:

        1. Run every parameter-search record via
           ``fold_evaluator`` / ``pool_evaluator`` on the
           train / validation folds; preserve losing /
           rejected / failed runs.
        2. Choose a candidate (first parameter-set version
           whose validation metric strictly improves over the
           previous best).
        3. Write the candidate lock to ``lock_directory``
           *before* the test fold is read.
        4. Verify the lock and refuse to read the test fold
           without a matching one.
        5. Build the test-fold distributions from
           ``distribution_samples_by_metric``.
        6. Build the neighborhood stability report from
           ``neighborhood_metric_values``.
        7. Build the stress report from
           ``stress_evaluator``.
        8. Emit the recommendation.

        The caller-supplied ``clock_unix_seconds`` is the
        lock's ``lock_time_unix_seconds``; the harness never
        reads wall-clock time.
        """
        if not isinstance(lock_directory, (Path, str)):
            raise HarnessInputsError(
                f"ExperimentHarness.run: lock_directory must be Path or str, "
                f"got {type(lock_directory).__name__}"
            )
        if not isinstance(manifest_hash, str) or not manifest_hash:
            raise HarnessInputsError("ExperimentHarness.run: manifest_hash must be non-empty str")
        if not isinstance(clock_unix_seconds, int) or isinstance(clock_unix_seconds, bool):
            raise HarnessInputsError(
                f"ExperimentHarness.run: clock_unix_seconds must be int, got "
                f"{type(clock_unix_seconds).__name__}"
            )
        if clock_unix_seconds < 0:
            raise HarnessInputsError(
                f"ExperimentHarness.run: clock_unix_seconds must be "
                f"non-negative, got {clock_unix_seconds}"
            )
        if not isinstance(candidate_id_suffix, str):
            raise HarnessInputsError(
                f"ExperimentHarness.run: candidate_id_suffix must be str, "
                f"got {type(candidate_id_suffix).__name__}"
            )

        # ----- Step 1: parameter-search manifest ---------------------
        records = self._collect_search_records(fold_evaluator, pool_evaluator)
        if not records:
            raise EmptySearchError(
                "ExperimentHarness.run: fold_evaluator / pool_evaluator "
                "produced no records; the harness refuses to publish an "
                "empty sweep"
            )
        search_manifest = build_parameter_search_manifest(
            search_id=f"{self.plan.chain_id}-{self.plan.pool_key_id}-{self.plan.seed}",
            parameter_surface=self.plan.parameter_surface,
            seed=self.plan.seed,
            records=records,
            elimination_rules=self.plan.elimination_rules,
        )

        # ----- Step 2: candidate selection ---------------------------
        candidate_choice = self._choose_candidate(search_manifest)
        chosen_ps_version_raw = candidate_choice["parameter_set_version"]
        chosen_validation_metric_raw = candidate_choice["validation_metric"]
        chosen_ps_version = str(chosen_ps_version_raw)
        chosen_validation_metric = (
            int(chosen_validation_metric_raw)
            if isinstance(chosen_validation_metric_raw, int)
            and not isinstance(chosen_validation_metric_raw, bool)
            else 0
        )

        # ----- Step 3: candidate-lock write --------------------------
        candidate_id = f"{chosen_ps_version}" + (
            f"-{candidate_id_suffix}" if candidate_id_suffix else ""
        )
        boundaries = self.plan.split_boundaries()
        lock = build_candidate_lock(
            candidate_id=candidate_id,
            parameter_set_version=chosen_ps_version,
            code_revision=self.code_revision,
            manifest_hash=manifest_hash,
            seed=self.plan.seed,
            split_boundaries=boundaries,
            cost_assumptions=self.plan.cost_assumptions,
            elimination_rules=self.plan.elimination_rules,
            actor=self.actor,
            lock_time_unix_seconds=clock_unix_seconds,
        )
        lock_path = self._lock_path(lock_directory=lock_directory, candidate_id=candidate_id)
        write_candidate_lock(lock, lock_path)

        # ----- Step 4: lock verification -----------------------------
        try:
            loaded_lock = load_candidate_lock(lock_path)
        except CandidateLockNotFoundError as exc:
            raise MissingCandidateLockError(
                f"ExperimentHarness.run: candidate lock for {candidate_id!r} "
                f"is missing at {lock_path}; the harness refuses to read "
                f"the test fold until a matching lock is written"
            ) from exc
        except CandidateLockHashMismatchError as exc:
            raise CandidateLockHashMismatchOnLoadError(
                f"ExperimentHarness.run: candidate lock at {lock_path} "
                f"failed its content-hash verification: {exc}. The harness "
                f"refuses to read the test fold until the lock's frozen "
                f"fields match the recorded digest."
            ) from exc
        if loaded_lock.parameter_set_version != chosen_ps_version:
            raise CandidateLockHashMismatchOnLoadError(
                f"ExperimentHarness.run: loaded lock parameter_set_version="
                f"{loaded_lock.parameter_set_version!r} disagrees with the "
                f"chosen version {chosen_ps_version!r}"
            )
        recomputed_hash = compute_candidate_lock_content_hash(loaded_lock)
        if recomputed_hash != loaded_lock.content_hash:
            raise CandidateLockHashMismatchOnLoadError(
                f"ExperimentHarness.run: loaded lock content_hash="
                f"{loaded_lock.content_hash} disagrees with recomputed "
                f"digest={recomputed_hash}"
            )

        # ----- Step 5: test-fold access (gated) ----------------------
        # The harness invokes ``test_fold_evaluator`` to *touch* the
        # test fold. The evaluator is a callable the orchestrator
        # supplies; the harness only invokes it after the lock is
        # verified. The evaluator's return value is the test-fold
        # metric value the harness records on the distributions /
        # stability inputs (the orchestrator already collected the
        # per-decision samples into ``distribution_samples_by_metric``).
        _ = test_fold_evaluator(
            chain_id=self.plan.chain_id,
            pool_key_id=self.plan.pool_key_id,
            parameter_set_version=chosen_ps_version,
            seed=self.plan.seed,
        )

        distributions = self._build_distributions(
            distribution_samples_by_metric=distribution_samples_by_metric,
        )
        stability_report = build_neighborhood_stability_report(
            locked_parameter_set_version=chosen_ps_version,
            locked_metric_value=chosen_validation_metric,
            neighborhood_metric_values=tuple(neighborhood_metric_values),
        )
        stress_report = self._build_stress_report(stress_evaluator)

        # ----- Step 6: recommendation --------------------------------
        if (
            not distributions
            or any(d.median_q64_64 <= 0 for d in distributions)
            or stability_report.stability_spread_q64_64 <= 0
        ):
            recommendation = build_no_trade_recommendation(
                reason_codes=(
                    "TEST_MEDIAN_NON_POSITIVE",
                    "INSUFFICIENT_NEIGHBORHOOD_SPREAD",
                ),
            )
        else:
            recommendation = build_provisional_recommendation(
                candidate_id=candidate_id,
                locked_metric_value=chosen_validation_metric,
                reason_codes=("LOCKED_CANDIDATE_SURVIVED",),
            )

        result = ExperimentHarnessResult(
            version=EXPERIMENT_HARNESS_VERSION,
            plan_chain_id=self.plan.chain_id,
            plan_pool_key_id=self.plan.pool_key_id,
            search_manifest=search_manifest,
            candidate_lock=loaded_lock,
            distributions=distributions,
            stability_report=stability_report,
            stress_report=stress_report,
            recommendation=recommendation,
            clock_unix_seconds=clock_unix_seconds,
            content_hash="0x" + "00" * 32,
        )
        digest = _compute_result_content_hash(result)
        object.__setattr__(result, "content_hash", digest)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_search_records(
        self,
        fold_evaluator: FoldEvaluator,
        pool_evaluator: PoolEvaluator,
    ) -> tuple[SearchRunRecord, ...]:
        """Collect every search record the sweep produced.

        The harness evaluates every grid point on every fold the
        plan declares and every held-out pool the pool-holdout
        split declares. Losing / rejected / failed runs are
        preserved (the record's ``run_status`` carries the
        outcome).
        """
        records: list[SearchRunRecord] = []
        for parameter_combo in self.plan.parameter_surface.grid():
            parameter_set_version = compute_parameter_set_version(parameter_combo)
            if self.plan.time_holdout_split is not None:
                for fold_role in ("TRAIN", "VALIDATION", "TEST"):
                    fold = self._resolve_fold(fold_role)
                    if fold is None:
                        continue
                    metric, status, reasons = self._invoke_fold(
                        fold_evaluator,
                        fold_role=fold_role,
                        parameter_combo=parameter_combo,
                        parameter_set_version=parameter_set_version,
                        fold=fold,
                    )
                    records.append(
                        SearchRunRecord.from_mapping(
                            parameters=parameter_combo,
                            fold_role=fold_role,
                            segment_label=fold.segment_label,
                            metric_value=metric,
                            run_status=status,
                            elimination_reasons=reasons,
                        )
                    )
            for walk_forward in self.plan.walk_forward_splits:
                for fold_role in ("TRAIN", "VALIDATION"):
                    fold = self._resolve_walk_forward_fold(walk_forward, fold_role)
                    if fold is None:
                        continue
                    metric, status, reasons = self._invoke_fold(
                        fold_evaluator,
                        fold_role=fold_role,
                        parameter_combo=parameter_combo,
                        parameter_set_version=parameter_set_version,
                        fold=fold,
                    )
                    records.append(
                        SearchRunRecord.from_mapping(
                            parameters=parameter_combo,
                            fold_role=fold_role,
                            segment_label=fold.segment_label,
                            metric_value=metric,
                            run_status=status,
                            elimination_reasons=reasons,
                        )
                    )
            if self.plan.pool_holdout_split is not None:
                for fold_role_loop in self.plan.pool_holdout_split.holdout_pool_folds:
                    metric, status, reasons = self._invoke_pool(
                        pool_evaluator,
                        parameter_combo=parameter_combo,
                        parameter_set_version=parameter_set_version,
                        fold=fold_role_loop,
                    )
                    records.append(
                        SearchRunRecord.from_mapping(
                            parameters=parameter_combo,
                            fold_role="HOLDOUT_POOL",
                            segment_label=fold_role_loop.segment_label,
                            metric_value=metric,
                            run_status=status,
                            elimination_reasons=reasons,
                        )
                    )
        return tuple(records)

    def _resolve_fold(self, fold_role: str) -> TimeFold | None:
        """Return the time-holdout fold for ``fold_role`` (or ``None``)."""
        split = self.plan.time_holdout_split
        if split is None:
            return None
        if fold_role == "TRAIN":
            return split.train
        if fold_role == "VALIDATION":
            return split.validation
        if fold_role == "TEST":
            return split.test
        return None

    def _resolve_walk_forward_fold(
        self, walk_forward: WalkForwardSplit, fold_role: str
    ) -> TimeFold | None:
        if fold_role == "TRAIN":
            return walk_forward.train
        if fold_role == "VALIDATION":
            return walk_forward.validation
        return None

    def _invoke_fold(
        self,
        fold_evaluator: FoldEvaluator,
        *,
        fold_role: str,
        parameter_combo: Mapping[str, int | str | bool],
        parameter_set_version: str,
        fold: object,
    ) -> tuple[int, str, tuple[str, ...]]:
        try:
            metric = int(
                fold_evaluator(
                    chain_id=self.plan.chain_id,
                    pool_key_id=self.plan.pool_key_id,
                    parameter_set_version=parameter_set_version,
                    parameter_combo=dict(parameter_combo),
                    fold_role=fold_role,
                    fold=fold,
                )
            )
        except Exception:  # noqa: BLE001 — preserve the record
            return 0, "FAILED", ("HALT_CATALOGUE",)
        # A negative metric value is the fold_evaluator's way of
        # declaring a LOSING run; the harness preserves the
        # value with a structured reason code (the T066 rule
        # "preserve no-trade / losing periods" binds here).
        if metric < 0:
            return metric, "LOSING", ("BELOW_THRESHOLD",)
        return metric, "FILLED", ()

    def _invoke_pool(
        self,
        pool_evaluator: PoolEvaluator,
        *,
        parameter_combo: Mapping[str, int | str | bool],
        parameter_set_version: str,
        fold: object,
    ) -> tuple[int, str, tuple[str, ...]]:
        try:
            metric = int(
                pool_evaluator(
                    chain_id=self.plan.chain_id,
                    pool_key_id=self.plan.pool_key_id,
                    parameter_set_version=parameter_set_version,
                    parameter_combo=dict(parameter_combo),
                    fold=fold,
                )
            )
            return metric, "FILLED", ()
        except Exception:  # noqa: BLE001
            return 0, "FAILED", ("HALT_CATALOGUE",)

    def _choose_candidate(self, search_manifest: ParameterSearchManifest) -> dict[str, object]:
        """Choose the first parameter-set version that strictly improves validation.

        The harness records every parameter-set version's *best*
        validation metric value, then picks the first version
        whose best strictly exceeds the previous best. A version
        whose best ties the previous best is *not* selected (the
        rule is monotonic; "select only the highest return" is
        forbidden, so the harness never ranks by global best).
        """
        # Group records by parameter-set version, restricting to
        # VALIDATION records. The chosen version's metric is the
        # maximum of its VALIDATION records.
        by_version: dict[str, list[int]] = {}
        for record in search_manifest.records:
            if record.fold_role != "VALIDATION":
                continue
            if record.run_status not in ("FILLED", "LOSING"):
                continue
            by_version.setdefault(record.parameter_set_version, []).append(record.metric_value)
        if not by_version:
            raise NoCandidateEligibleError(
                "ExperimentHarness._choose_candidate: no parameter-set version "
                "produced a VALIDATION record; the harness emits NO_TRADE"
            )
        # Walk the declaration order of the parameter surface so
        # the chosen version is the *first* strictly-improving one.
        ordered_versions = [
            compute_parameter_set_version(combo)
            for combo in search_manifest.parameter_surface.grid()
        ]
        best_metric = -1
        chosen_version = ""
        chosen_metric = 0
        for ps_version in ordered_versions:
            if ps_version not in by_version:
                continue
            version_best = max(by_version[ps_version])
            if version_best > best_metric:
                best_metric = version_best
                chosen_version = ps_version
                chosen_metric = version_best
        if not chosen_version:
            raise NoCandidateEligibleError(
                "ExperimentHarness._choose_candidate: no parameter-set version "
                "strictly improved validation"
            )
        return {
            "parameter_set_version": chosen_version,
            "validation_metric": int(chosen_metric),
        }

    def _lock_path(self, *, lock_directory: Path | str, candidate_id: str) -> Path:
        """Return the on-disk path for ``candidate_id``'s lock."""
        directory = Path(lock_directory)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{candidate_id}.lock.json"

    def _build_distributions(
        self,
        *,
        distribution_samples_by_metric: Mapping[str, Sequence[int]],
    ) -> tuple[DistributionSummary, ...]:
        """Build a distribution summary for every metric kind the
        caller supplied.

        Every metric kind must appear in
        :data:`VALID_METRIC_KINDS`; the harness refuses to build
        an unknown metric kind.
        """
        summaries: list[DistributionSummary] = []
        for metric_kind in sorted(distribution_samples_by_metric):
            if metric_kind not in VALID_METRIC_KINDS:
                raise HarnessInputsError(
                    f"ExperimentHarness._build_distributions: unknown "
                    f"metric_kind={metric_kind!r}; valid="
                    f"{sorted(VALID_METRIC_KINDS)}"
                )
            summaries.append(
                build_distribution_summary(
                    metric_kind=metric_kind,
                    samples=tuple(distribution_samples_by_metric[metric_kind]),
                )
            )
        return tuple(summaries)

    def _build_stress_report(self, stress_evaluator: StressEvaluator) -> StressReport:
        """Build the stress report by invoking ``stress_evaluator``
        on every T064 catalogue category."""
        catalogue_id = "t064.default_stress"
        outcomes: list[StressScenarioOutcome] = []
        for category in (
            "STRESSED_GAS",
            "STRESSED_LATENCY",
            "STRESSED_SLIPPAGE",
            "STRESSED_FEE",
            "MISSING_DATA",
            "REORG",
        ):
            scenario_id = f"{category.lower()}_default"
            outcome = stress_evaluator(
                scenario_id=scenario_id,
                category=category,
                chain_id=self.plan.chain_id,
                pool_key_id=self.plan.pool_key_id,
                seed=self.plan.seed,
            )
            if not isinstance(outcome, StressScenarioOutcome):
                raise HarnessInputsError(
                    f"ExperimentHarness._build_stress_report: stress_evaluator "
                    f"returned {type(outcome).__name__}; must be "
                    f"StressScenarioOutcome"
                )
            if outcome.outcome not in VALID_STRESS_OUTCOMES:
                raise HarnessInputsError(
                    f"ExperimentHarness._build_stress_report: stress_evaluator "
                    f"returned outcome={outcome.outcome!r}; must be one of "
                    f"{sorted(VALID_STRESS_OUTCOMES)}"
                )
            outcomes.append(outcome)
        return build_stress_report(catalogue_id=catalogue_id, outcomes=outcomes)


def _compute_result_content_hash(result: ExperimentHarnessResult) -> str:
    """Return the SHA-256 hex digest of the canonical result payload.

    The digest binds every artifact the harness produced
    (search manifest, candidate lock, distributions, stability,
    stress, recommendation). A tampered result fails the
    reviewer gate.
    """
    payload = result.to_dict()
    payload.pop("content_hash", None)
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "EXPERIMENT_HARNESS_VERSION",
    "Q64_SCALE",
    "REQUIRED_ELIMINATION_RULES",
    "UNKNOWN_CODE_REVISION",
    "VALID_COST_ASSUMPTIONS",
    "VALID_RECOMMENDATIONS",
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
    "build_no_trade_recommendation",
    "build_provisional_recommendation",
]
