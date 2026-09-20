"""Robustness report (T064).

The robustness report is the artifact the T064 acceptance clause
binds. It is the single output the reviewer reads to confirm:

- **Train / validation / test are prominently separated.** The
  report carries three disjoint fold-result tuples; the reviewer
  cannot confuse a validation metric for a test metric.
- **Pool-holdout is the primary generalisation statement.** The
  report names the primary axis (``POOL_HOLDOUT``) and carries a
  per-held-out-pool result tuple.
- **Sensitivity is reported beside the best run.** The
  :class:`SensitivitySummary` is on the report and is not optional.
- **The embargo length is derived from the label horizon and
  recorded on every split.** The report carries the embargo length
  and the label horizon unit.
- **The scenario catalogue is recorded on the report.** The
  reviewercan audit every declared outcome; no scenario is decided
  after the run.

The module owns:

- :class:`FoldResult` — one fold's per-fold metrics.
- :class:`RobustnessReport` — the assembled report.
- :func:`build_robustness_report` — the canonical builder.
- :func:`assert_train_validation_test_separation` — the
  acceptance-clause gate.

Design constraints:

- **Integer accounting.** Every metric value is a non-negative
  integer; the embargo length is a non-negative integer; the
  family-wise alpha is a Q64.64 integer.

- **Determinism.** Two equivalent reports in any process produce
  byte-identical JSON.

- **Layer purity.** The module imports the standard library and
  the in-package :mod:`robinhood_lp.robustness` primitives only. It
  does not import the backtest engine, the manifest layer, RPC,
  storage, signing, or presentation code.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from robinhood_lp.robustness.disclosure import (
    MultipleComparisonDisclosure,
)
from robinhood_lp.robustness.scenarios import (
    ScenarioCatalogue,
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
)

#: Module version.
REPORTS_VERSION: Final[str] = "t064.robustness_reports.v1"

#: The primary generalisation axis the report names. Per DS-021,
#: ``POOL_HOLDOUT`` is the primary axis; time holdout and
#: walk-forward are retained beside it.
PRIMARY_GENERALISATION_AXIS: Final[str] = "POOL_HOLDOUT"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RobustnessReportError(ValueError):
    """Base class for robustness-report construction / validation failures."""


class FoldResultError(RobustnessReportError):
    """A fold result carries an out-of-vocabulary role or negative metric."""


class MissingPrimaryGeneralisationError(RobustnessReportError):
    """The report does not carry per-held-out-pool results for the pool holdout.

    DS-021 binds this rejection: pool holdout is the primary
    generalisation test; a report that does not name per-pool
    results cannot be the primary statement.
    """


class InvalidReportAxisError(RobustnessReportError):
    """The report names a primary axis other than ``POOL_HOLDOUT``."""


# ---------------------------------------------------------------------------
# Fold result
# ---------------------------------------------------------------------------


#: Closed vocabulary for the fold result role.
VALID_FOLD_RESULT_ROLES: Final[frozenset[str]] = frozenset(
    {"TRAIN", "VALIDATION", "TEST", "HOLDOUT_POOL"}
)


@dataclass(frozen=True, slots=True)
class FoldResult:
    """The result of running the strategy on one fold.

    Field units:

    - ``role`` — one of :data:`VALID_FOLD_RESULT_ROLES`.
    - ``segment_label`` — non-empty string; e.g. ``"train_fold_0"``.
    - ``metric_name`` — non-empty string; the metric this fold
      result records.
    - ``metric_value`` — non-negative integer (Q64.64 ratio or
      raw count, depending on the metric).
    - ``fold_index`` — non-negative integer.
    """

    role: str
    segment_label: str
    metric_name: str
    metric_value: int
    fold_index: int

    def __post_init__(self) -> None:
        if self.role not in VALID_FOLD_RESULT_ROLES:
            raise FoldResultError(
                f"FoldResult.role: must be one of {sorted(VALID_FOLD_RESULT_ROLES)}, "
                f"got {self.role!r}"
            )
        if not isinstance(self.segment_label, str) or not self.segment_label:
            raise FoldResultError(
                f"FoldResult.segment_label: must be non-empty str, got {self.segment_label!r}"
            )
        if not isinstance(self.metric_name, str) or not self.metric_name:
            raise FoldResultError(
                f"FoldResult.metric_name: must be non-empty str, got {self.metric_name!r}"
            )
        if not isinstance(self.metric_value, int) or isinstance(self.metric_value, bool):
            raise FoldResultError(
                f"FoldResult.metric_value: must be int, got {type(self.metric_value).__name__}"
            )
        if self.metric_value < 0:
            raise FoldResultError(
                f"FoldResult.metric_value: must be non-negative, got {self.metric_value}"
            )
        if not isinstance(self.fold_index, int) or isinstance(self.fold_index, bool):
            raise FoldResultError(
                f"FoldResult.fold_index: must be int, got {type(self.fold_index).__name__}"
            )
        if self.fold_index < 0:
            raise FoldResultError(
                f"FoldResult.fold_index: must be non-negative, got {self.fold_index}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "segment_label": self.segment_label,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
            "fold_index": self.fold_index,
        }


# ---------------------------------------------------------------------------
# Pool holdout result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolHoldoutResult:
    """A single held-out pool's result.

    The report carries one of these per held-out pool; the
    T064 acceptance clause binds the per-pool disclosure (a
    primary generalisation statement that hides per-pool losses
    is not primary).

    Field units:

    - ``pool_key_id`` — non-empty hex string.
    - ``chain_id`` — positive integer.
    - ``metric_name`` — non-empty string.
    - ``metric_value`` — non-negative integer.
    """

    chain_id: int
    pool_key_id: str
    metric_name: str
    metric_value: int

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise FoldResultError(
                f"PoolHoldoutResult.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise FoldResultError(
                f"PoolHoldoutResult.chain_id: must be positive, got {self.chain_id}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise FoldResultError(
                f"PoolHoldoutResult.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        if not isinstance(self.metric_name, str) or not self.metric_name:
            raise FoldResultError("PoolHoldoutResult.metric_name: must be non-empty str")
        if not isinstance(self.metric_value, int) or isinstance(self.metric_value, bool):
            raise FoldResultError(
                f"PoolHoldoutResult.metric_value: must be int, got "
                f"{type(self.metric_value).__name__}"
            )
        if self.metric_value < 0:
            raise FoldResultError(
                f"PoolHoldoutResult.metric_value: must be non-negative, got {self.metric_value}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "chain_id": self.chain_id,
            "pool_key_id": self.pool_key_id,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
        }


# ---------------------------------------------------------------------------
# Robustness report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RobustnessReport:
    """The full robustness report the T064 acceptance clause binds.

    The report carries:

    - :attr:`version` — the report schema version.
    - :attr:`run_id` — non-empty string; the run's identity.
    - :attr:`primary_generalisation_axis` — always
      ``PRIMARY_GENERALISATION_AXIS`` (``"POOL_HOLDOUT"``) per
      DS-021.
    - :attr:`label_horizon` — :class:`LabelHorizon`; the source of
      the embargo length.
    - :attr:`purge_embargo_length` — non-negative integer; equals
      :attr:`LabelHorizon.value`.
    - :attr:`embargo_unit` — closed vocabulary string.
    - :attr:`train_results` / :attr:`validation_results` /
      :attr:`test_results` — disjoint fold-result tuples; the
      acceptance clause binds the three-tuples are disjoint.
    - :attr:`pool_holdout_results` — non-empty tuple of
      :class:`PoolHoldoutResult`; one per held-out pool.
    - :attr:`walk_forward_splits` — tuple of :class:`WalkForwardSplit`.
    - :attr:`time_holdout_split` — :class:`TimeHoldoutSplit` or
      ``None``.
    - :attr:`pool_holdout_split` — :class:`PoolHoldoutSplit`.
    - :attr:`parameter_surface` — :class:`ParameterSurface` or
      ``None``.
    - :attr:`regime_segmentation` — :class:`RegimeSegmentation` or
      ``None``.
    - :attr:`sensitivity_summary` — :class:`SensitivitySummary`.
    - :attr:`scenario_catalogue` — :class:`ScenarioCatalogue`.
    - :attr:`multiple_comparison_disclosure` —
      :class:`MultipleComparisonDisclosure`.
    - :attr:`conclusion_statement` — non-empty string; the human
      summary that must include sensitivity, not only the best run.
    """

    version: str
    run_id: str
    label_horizon: LabelHorizon
    train_results: tuple[FoldResult, ...]
    validation_results: tuple[FoldResult, ...]
    test_results: tuple[FoldResult, ...]
    pool_holdout_results: tuple[PoolHoldoutResult, ...]
    walk_forward_splits: tuple[WalkForwardSplit, ...]
    time_holdout_split: TimeHoldoutSplit | None
    pool_holdout_split: PoolHoldoutSplit
    parameter_surface: ParameterSurface | None
    regime_segmentation: RegimeSegmentation | None
    sensitivity_summary: SensitivitySummary
    scenario_catalogue: ScenarioCatalogue
    multiple_comparison_disclosure: MultipleComparisonDisclosure
    conclusion_statement: str

    def __post_init__(self) -> None:
        if self.version != REPORTS_VERSION:
            raise RobustnessReportError(
                f"RobustnessReport.version: must be {REPORTS_VERSION!r}, got {self.version!r}"
            )
        if not isinstance(self.run_id, str) or not self.run_id:
            raise RobustnessReportError("RobustnessReport.run_id: must be non-empty str")
        if self.purge_embargo_length != self.label_horizon.purge_embargo_length:
            raise RobustnessReportError(
                f"RobustnessReport: purge_embargo_length={self.purge_embargo_length} "
                f"disagrees with label_horizon.purge_embargo_length="
                f"{self.label_horizon.purge_embargo_length}"
            )
        if self.embargo_unit != self.label_horizon.embargo_unit:
            raise RobustnessReportError(
                f"RobustnessReport: embargo_unit={self.embargo_unit!r} disagrees "
                f"with label_horizon.embargo_unit={self.label_horizon.embargo_unit!r}"
            )
        if not isinstance(self.conclusion_statement, str) or not self.conclusion_statement:
            raise RobustnessReportError(
                "RobustnessReport.conclusion_statement: must be non-empty str"
            )
        # DS-021: pool holdout is the primary axis; the report must
        # carry per-held-out-pool results.
        if not self.pool_holdout_results:
            raise MissingPrimaryGeneralisationError(
                "RobustnessReport: pool_holdout_results must be non-empty; "
                "DS-021 binds pool holdout as the primary generalisation "
                "test and per-pool results as the primary disclosure"
            )
        # The test, validation, and train fold sets must be disjoint
        # by segment_label so the three disclosures cannot be
        # confused.
        assert_train_validation_test_separation(
            train=self.train_results,
            validation=self.validation_results,
            test=self.test_results,
        )
        # The per-pool metric_name should be the same as the
        # sensitivity_summary metric_name so the reviewer can read
        # the sensitivity beside the per-pool result.
        for result in self.pool_holdout_results:
            if result.metric_name != self.sensitivity_summary.metric_name:
                raise RobustnessReportError(
                    f"RobustnessReport: pool_holdout_results[{result.pool_key_id!r}] "
                    f"metric_name={result.metric_name!r} disagrees with "
                    f"sensitivity_summary.metric_name="
                    f"{self.sensitivity_summary.metric_name!r}"
                )

    @property
    def primary_generalisation_axis(self) -> str:
        return PRIMARY_GENERALISATION_AXIS

    @property
    def purge_embargo_length(self) -> int:
        return self.label_horizon.purge_embargo_length

    @property
    def embargo_unit(self) -> str:
        return self.label_horizon.embargo_unit

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "primary_generalisation_axis": self.primary_generalisation_axis,
            "label_horizon": {
                "value": self.label_horizon.value,
                "unit": self.label_horizon.unit,
            },
            "purge_embargo_length": self.purge_embargo_length,
            "embargo_unit": self.embargo_unit,
            "train_results": [r.to_dict() for r in self.train_results],
            "validation_results": [r.to_dict() for r in self.validation_results],
            "test_results": [r.to_dict() for r in self.test_results],
            "pool_holdout_results": [r.to_dict() for r in self.pool_holdout_results],
            "walk_forward_splits": [
                {
                    "fold_index": s.fold_index,
                    "window_kind": s.window_kind,
                }
                for s in self.walk_forward_splits
            ],
            "time_holdout_split": (
                {
                    "train_segment_label": self.time_holdout_split.train.segment_label,
                    "validation_segment_label": self.time_holdout_split.validation.segment_label,
                    "test_segment_label": self.time_holdout_split.test.segment_label,
                }
                if self.time_holdout_split is not None
                else None
            ),
            "pool_holdout_split": {
                "train_pool_ids": sorted(
                    (f.chain_id, f.pool_key_id) for f in self.pool_holdout_split.train_pool_folds
                ),
                "holdout_pool_ids": sorted(
                    (f.chain_id, f.pool_key_id) for f in self.pool_holdout_split.holdout_pool_folds
                ),
            },
            "parameter_surface": (
                self.parameter_surface.to_dict() if self.parameter_surface is not None else None
            ),
            "regime_segmentation": (
                self.regime_segmentation.to_dict() if self.regime_segmentation is not None else None
            ),
            "sensitivity_summary": self.sensitivity_summary.to_dict(),
            "scenario_catalogue": self.scenario_catalogue.to_dict(),
            "multiple_comparison_disclosure": self.multiple_comparison_disclosure.to_dict(),
            "conclusion_statement": self.conclusion_statement,
        }


def assert_train_validation_test_separation(
    *,
    train: Sequence[FoldResult],
    validation: Sequence[FoldResult],
    test: Sequence[FoldResult],
) -> None:
    """Raise :class:`RobustnessReportError` if any segment_label appears in two roles.

    The T064 acceptance clause binds this rejection: the report
    must prominently separate train / validation / test; a segment
    label that appears in two roles would make the separation
    impossible to read.
    """
    train_labels = {r.segment_label for r in train}
    validation_labels = {r.segment_label for r in validation}
    test_labels = {r.segment_label for r in test}
    overlap_tv = train_labels & validation_labels
    if overlap_tv:
        raise RobustnessReportError(
            f"assert_train_validation_test_separation: segment_labels appear in "
            f"both train and validation: {sorted(overlap_tv)}"
        )
    overlap_tt = train_labels & test_labels
    if overlap_tt:
        raise RobustnessReportError(
            f"assert_train_validation_test_separation: segment_labels appear in "
            f"both train and test: {sorted(overlap_tt)}"
        )
    overlap_vt = validation_labels & test_labels
    if overlap_vt:
        raise RobustnessReportError(
            f"assert_train_validation_test_separation: segment_labels appear in "
            f"both validation and test: {sorted(overlap_vt)}"
        )


def build_robustness_report(
    *,
    run_id: str,
    label_horizon: LabelHorizon,
    train_results: Iterable[FoldResult],
    validation_results: Iterable[FoldResult],
    test_results: Iterable[FoldResult],
    pool_holdout_results: Iterable[PoolHoldoutResult],
    walk_forward_splits: Iterable[WalkForwardSplit],
    pool_holdout_split: PoolHoldoutSplit,
    sensitivity_summary: SensitivitySummary,
    scenario_catalogue: ScenarioCatalogue,
    multiple_comparison_disclosure: MultipleComparisonDisclosure,
    conclusion_statement: str,
    time_holdout_split: TimeHoldoutSplit | None = None,
    parameter_surface: ParameterSurface | None = None,
    regime_segmentation: RegimeSegmentation | None = None,
) -> RobustnessReport:
    """Build a :class:`RobustnessReport`.

    The function is the canonical builder; it enforces the
    T064 acceptance clauses that are independent of the schema
    (separation, primary generalisation, embargo derivation).
    """
    train = tuple(train_results)
    validation = tuple(validation_results)
    test = tuple(test_results)
    pool_holdout = tuple(pool_holdout_results)
    walk_forward = tuple(walk_forward_splits)
    if not pool_holdout:
        raise MissingPrimaryGeneralisationError(
            "build_robustness_report: pool_holdout_results must be non-empty; "
            "DS-021 binds pool holdout as the primary generalisation test"
        )
    assert_train_validation_test_separation(train=train, validation=validation, test=test)
    return RobustnessReport(
        version=REPORTS_VERSION,
        run_id=run_id,
        label_horizon=label_horizon,
        train_results=train,
        validation_results=validation,
        test_results=test,
        pool_holdout_results=pool_holdout,
        walk_forward_splits=walk_forward,
        time_holdout_split=time_holdout_split,
        pool_holdout_split=pool_holdout_split,
        parameter_surface=parameter_surface,
        regime_segmentation=regime_segmentation,
        sensitivity_summary=sensitivity_summary,
        scenario_catalogue=scenario_catalogue,
        multiple_comparison_disclosure=multiple_comparison_disclosure,
        conclusion_statement=conclusion_statement,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "PRIMARY_GENERALISATION_AXIS",
    "REPORTS_VERSION",
    "VALID_FOLD_RESULT_ROLES",
    "FoldResult",
    "FoldResultError",
    "InvalidReportAxisError",
    "MissingPrimaryGeneralisationError",
    "PoolHoldoutResult",
    "RobustnessReport",
    "RobustnessReportError",
    "assert_train_validation_test_separation",
    "build_robustness_report",
]
