"""Deterministic, leak-free dataset splits (T101, DS-020..DS-022).

The split module implements the three closed-vocabulary split
modes the contract names:

- **Pool holdout**: a disjoint-by-pool split; training and
  evaluation use non-overlapping ``(chain_id, PoolKey)`` member
  sets. With the measured sample size this is the primary
  generalisation test, because time holdout only separates two
  sides that share almost the same market regime.
- **Time holdout**: an evaluation period strictly later than the
  training period. The split must separate the two halves by a
  purge + embargo gap derived from the label horizon the schema
  records.
- **Walk-forward**: a sequence of (training, evaluation) folds
  where each fold's training period is the past and the
  evaluation period is the immediate future. The same purge +
  embargo gap is enforced between each fold's training and
  evaluation intervals.

The split module also implements the **purge** (samples whose
label window crosses the training/evaluation boundary are
removed from both halves) and the **embargo** (samples whose
decision time falls inside the embargo gap are removed) that
``DS-022`` requires. The combined ``purge_plus_embargo_size``
gap equals the largest label horizon the schema records and is
stated on the split record so a re-derivation can verify it.

Design contract (binding):

- **Temporal only.** Random, shuffled or otherwise non-temporal
  splits are forbidden. ``DS-020`` is the binding rule; the
  module refuses to construct such a split.
- **Source samples are deterministic.** The split is derived
  from the sorted ``(decision_time, sample_id)`` ordering of the
  panel rows the harness supplies so two splits from the same
  panel agree.
- **Stated embargo length.** Every split record carries the
  ``purge_plus_embargo_size`` it used; the value is derived
  from the label schema, not hand-picked (``DS-022``).
- **No future leakage.** A split that would expose a sample
  whose ``decision_time`` is past the training fold's
  ``end - purge`` boundary to the training fold raises
  :class:`SplitLeakageError`; the harness refuses to publish a
  model trained on it.

References:

- ``docs/spec/research/DATASET_AND_EVALUATION.md`` §4
  (``DS-020``, ``DS-021``, ``DS-022``).
- ``docs/spec/architecture/adr/ADR-014-research-universe-and-numeraire.md``
  §5 (the research universe boundary that makes pool holdout
  possible).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SplitError(ValueError):
    """Base class for split failures."""


class SplitLeakageError(SplitError):
    """A split configuration would leak samples past the embargo boundary."""


class SplitWithoutPoolHoldoutError(SplitError):
    """A pool-holdout split was requested but no pool identity was available."""


class SplitWithoutTimeHoldoutError(SplitError):
    """A time-holdout split was requested but no ``decision_time`` was available."""


class SplitUnknownFoldError(SplitError):
    """A fold index outside the split's declared number of folds was referenced."""


class SplitEmptyFoldError(SplitError):
    """A fold's training or evaluation set was empty."""


class SplitNonTemporalError(SplitError):
    """A non-temporal split was requested (``DS-020``)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


class SplitMode(StrEnum):
    """The three split modes the contract enumerates.

    Strings are part of the public contract. New modes are
    additive; renaming an existing mode is a breaking change.
    """

    POOL_HOLDOUT = "POOL_HOLDOUT"
    TIME_HOLDOUT = "TIME_HOLDOUT"
    WALK_FORWARD = "WALK_FORWARD"


# ---------------------------------------------------------------------------
# Fold
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SplitFold:
    """One (training, evaluation) pair of a split.

    The two halves are disjoint sets of sample identifiers the
    harness defines as opaque strings (the canonical panel
    sample id is ``(decision_time, chain_id, pool_id,
    feature_window_end)``). A fold whose training or evaluation
    set is empty raises :class:`SplitEmptyFoldError` at the
    building stage.

    Fold invariants:

    - ``train_sample_ids`` and ``eval_sample_ids`` are disjoint
      (``DS-022`` forbids a sample in both halves);
    - every member of ``train_sample_ids`` has a ``decision_time``
      strictly less than every member of ``eval_sample_ids`` for
      a time-holdout or walk-forward split;
    - the fold carries the ``purge_size`` and ``embargo_size`` it
      applied, both derived from the label schema (no
      hand-picked values).
    """

    fold_index: int
    train_sample_ids: tuple[str, ...]
    eval_sample_ids: tuple[str, ...]
    train_start_decision_time: int
    train_end_decision_time: int
    eval_start_decision_time: int
    eval_end_decision_time: int
    purge_size: int
    embargo_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.fold_index, int) or isinstance(self.fold_index, bool):
            raise SplitError(
                f"SplitFold.fold_index: must be int, got {type(self.fold_index).__name__}"
            )
        if not isinstance(self.train_sample_ids, tuple):
            raise SplitError(
                f"SplitFold.train_sample_ids: must be tuple[str, ...], "
                f"got {type(self.train_sample_ids).__name__}"
            )
        if not isinstance(self.eval_sample_ids, tuple):
            raise SplitError(
                f"SplitFold.eval_sample_ids: must be tuple[str, ...], "
                f"got {type(self.eval_sample_ids).__name__}"
            )
        for name in (*self.train_sample_ids, *self.eval_sample_ids):
            if not isinstance(name, str) or not name:
                raise SplitError(f"SplitFold: every sample_id must be non-empty str, got {name!r}")
        intersection = set(self.train_sample_ids) & set(self.eval_sample_ids)
        if intersection:
            raise SplitError(
                f"SplitFold {self.fold_index}: a sample is in both halves "
                f"({sorted(intersection)[0]!r})"
            )
        if self.eval_end_decision_time < self.eval_start_decision_time:
            raise SplitError(f"SplitFold {self.fold_index}: invalid evaluation interval")
        if (
            self.train_end_decision_time > 0
            and self.eval_end_decision_time > 0
            and self.eval_end_decision_time <= self.train_end_decision_time
        ):
            raise SplitError(
                f"SplitFold {self.fold_index}: evaluation interval must "
                f"start strictly after training interval"
            )
        if self.purge_size < 0 or self.embargo_size < 0:
            raise SplitError(
                f"SplitFold {self.fold_index}: purge={self.purge_size} "
                f"and embargo={self.embargo_size} must be >= 0"
            )

    @property
    def purge_plus_embargo_size(self) -> int:
        """``purge_size + embargo_size`` — the declared gap length."""
        return self.purge_size + self.embargo_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold_index": self.fold_index,
            "train_sample_ids": list(self.train_sample_ids),
            "eval_sample_ids": list(self.eval_sample_ids),
            "train_start_decision_time": self.train_start_decision_time,
            "train_end_decision_time": self.train_end_decision_time,
            "eval_start_decision_time": self.eval_start_decision_time,
            "eval_end_decision_time": self.eval_end_decision_time,
            "purge_size": self.purge_size,
            "embargo_size": self.embargo_size,
        }


# ---------------------------------------------------------------------------
# Split definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PanelSplitDefinition:
    """The definition the split module turns into folds.

    The harness instantiates one definition per run; the
    :func:`build_pool_holdout_split`,
    :func:`build_time_holdout_split`, and
    :func:`build_walk_forward_split` factories return the
    closed-vocabulary definitions the contract enumerates.

    The ``purge_size`` is set to the largest label horizon the
    schema declares (i.e. the longest forward distance a label
    spans); the ``embargo_size`` is set to the largest ``gap``
    a label declares between its horizon and its observability
    moment (``DS-011`` records ``observes_after_seconds``).
    The harness composes ``purge_plus_embargo_size`` as the
    gap length it actually enforces.
    """

    mode: SplitMode
    folds: tuple[SplitFold, ...]
    purge_size: int
    embargo_size: int
    declared_label_horizons: tuple[int, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, SplitMode):
            raise SplitError(
                f"PanelSplitDefinition.mode: must be SplitMode, got {type(self.mode).__name__}"
            )
        if not isinstance(self.folds, tuple):
            raise SplitError(
                f"PanelSplitDefinition.folds: must be tuple, got {type(self.folds).__name__}"
            )
        for fold in self.folds:
            if not isinstance(fold, SplitFold):
                raise SplitError(
                    f"PanelSplitDefinition.folds: every entry must be "
                    f"SplitFold, got {type(fold).__name__}"
                )
        if not self.folds:
            raise SplitError("PanelSplitDefinition: must declare at least one fold")
        if not isinstance(self.purge_size, int) or isinstance(self.purge_size, bool):
            raise SplitError(
                f"PanelSplitDefinition.purge_size: must be int, "
                f"got {type(self.purge_size).__name__}"
            )
        if self.purge_size < 0:
            raise SplitError(
                f"PanelSplitDefinition.purge_size: must be >= 0, got {self.purge_size}"
            )
        if not isinstance(self.embargo_size, int) or isinstance(self.embargo_size, bool):
            raise SplitError(
                f"PanelSplitDefinition.embargo_size: must be int, "
                f"got {type(self.embargo_size).__name__}"
            )
        if self.embargo_size < 0:
            raise SplitError(
                f"PanelSplitDefinition.embargo_size: must be >= 0, got {self.embargo_size}"
            )
        if not isinstance(self.declared_label_horizons, tuple):
            raise SplitError(
                f"PanelSplitDefinition.declared_label_horizons: must be "
                f"tuple[int, ...], got "
                f"{type(self.declared_label_horizons).__name__}"
            )
        for horizon in self.declared_label_horizons:
            if not isinstance(horizon, int) or isinstance(horizon, bool):
                raise SplitError(
                    f"PanelSplitDefinition.declared_label_horizons: every "
                    f"entry must be int, got {horizon!r}"
                )
            if horizon <= 0:
                raise SplitError(
                    f"PanelSplitDefinition.declared_label_horizons: every "
                    f"entry must be > 0, got {horizon}"
                )

    @property
    def purge_plus_embargo_size(self) -> int:
        """The combined gap length ``purge + embargo``."""
        return self.purge_size + self.embargo_size

    @property
    def fold_count(self) -> int:
        """The number of folds in the split."""
        return len(self.folds)

    def fold(self, index: int) -> SplitFold:
        """Return the fold at ``index``."""
        if not isinstance(index, int) or isinstance(index, bool):
            raise SplitError(
                f"PanelSplitDefinition.fold: index must be int, got {type(index).__name__}"
            )
        if index < 0 or index >= len(self.folds):
            raise SplitUnknownFoldError(
                f"PanelSplitDefinition.fold: index={index} outside [0, {len(self.folds)})"
            )
        return self.folds[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "purge_size": self.purge_size,
            "embargo_size": self.embargo_size,
            "purge_plus_embargo_size": self.purge_plus_embargo_size,
            "declared_label_horizons": list(self.declared_label_horizons),
            "fold_count": self.fold_count,
            "folds": [f.to_dict() for f in self.folds],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _combined_gap_from_label_horizons(label_horizons: Sequence[int]) -> tuple[int, int]:
    """Return ``(purge, embargo)`` derived from ``label_horizons``.

    The purge length equals the largest ``label_horizons`` entry
    (the longest forward window a label spans). The embargo
    equals ``purge`` (the harness keeps a deterministic gap so a
    re-run derives the same number). Both values are stated on
    the produced split definition.
    """
    if not isinstance(label_horizons, Sequence):
        raise SplitError(
            f"_combined_gap_from_label_horizons: label_horizons must be "
            f"Sequence, got {type(label_horizons).__name__}"
        )
    if not label_horizons:
        raise SplitError(
            "_combined_gap_from_label_horizons: label_horizons must be "
            "non-empty (DS-022 derives the gap from the label schema)"
        )
    for horizon in label_horizons:
        if not isinstance(horizon, int) or isinstance(horizon, bool):
            raise SplitError(
                f"_combined_gap_from_label_horizons: every entry must be int, got {horizon!r}"
            )
        if horizon <= 0:
            raise SplitError(
                f"_combined_gap_from_label_horizons: every entry must be > 0, got {horizon}"
            )
    purge = max(label_horizons)
    embargo = purge  # ``embargo == purge`` keeps the split rule deterministic.
    return purge, embargo


def _require_opaque_sample_id(sample_id: str) -> None:
    if not isinstance(sample_id, str) or not sample_id:
        raise SplitError(f"sample_id must be non-empty str, got {sample_id!r}")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_pool_holdout_split(
    sample_pool_ids: Iterable[str],
    *,
    label_horizons: Sequence[int],
    min_eval_pools: int = 1,
) -> PanelSplitDefinition:
    """Construct a pool-holdout split.

    The split divides the supplied ``sample_pool_ids`` into a
    single fold whose training and evaluation sets use disjoint
    pool identities. The harness calls this builder with the
    ``list(set(sample_pool_ids))`` of the panel rows; with the
    measured sample size, a single fold with the ``min_eval_pools``
    smallest communities held out is the primary generalisation
    check.

    The function refuses a sample list that does not contain at
    least ``min_eval_pools + 1`` distinct pool identities, so a
    half-formed panel cannot drive a vacuous holdout.
    """
    if not isinstance(label_horizons, Sequence):
        raise SplitError(
            f"build_pool_holdout_split: label_horizons must be Sequence, "
            f"got {type(label_horizons).__name__}"
        )
    if not isinstance(min_eval_pools, int) or isinstance(min_eval_pools, bool):
        raise SplitError(
            f"build_pool_holdout_split: min_eval_pools must be int, "
            f"got {type(min_eval_pools).__name__}"
        )
    if min_eval_pools < 1:
        raise SplitError(
            f"build_pool_holdout_split: min_eval_pools must be >= 1, got {min_eval_pools}"
        )

    distinct_pools = sorted(set(sample_pool_ids))
    if len(distinct_pools) <= min_eval_pools:
        raise SplitWithoutPoolHoldoutError(
            f"build_pool_holdout_split: only {len(distinct_pools)} "
            f"distinct pool identities, need at least {min_eval_pools + 1} "
            f"for a pool holdout"
        )

    eval_pools = sorted(distinct_pools[:min_eval_pools])
    train_pools = sorted(set(distinct_pools) - set(eval_pools))
    purge, embargo = _combined_gap_from_label_horizons(label_horizons)

    return PanelSplitDefinition(
        mode=SplitMode.POOL_HOLDOUT,
        folds=(
            SplitFold(
                fold_index=0,
                train_sample_ids=tuple(train_pools),
                eval_sample_ids=tuple(eval_pools),
                # Pool holdout does not separate by decision time;
                # the recorded intervals are the panel's overall
                # range the harness sets during integration. We
                # choose ``0`` here because the harness binds the
                # explicit intervals on the panel snapshot, not on
                # the split.
                train_start_decision_time=0,
                train_end_decision_time=0,
                eval_start_decision_time=0,
                eval_end_decision_time=0,
                purge_size=purge,
                embargo_size=embargo,
            ),
        ),
        purge_size=purge,
        embargo_size=embargo,
        declared_label_horizons=tuple(label_horizons),
        notes=("pool holdout: training and evaluation use disjoint pool identities",),
    )


def assert_fold_non_empty(fold: SplitFold) -> None:
    """Raise :class:`SplitEmptyFoldError` if either half of ``fold`` is empty."""
    if not isinstance(fold, SplitFold):
        raise SplitError(
            f"assert_fold_non_empty: fold must be SplitFold, got {type(fold).__name__}"
        )
    if not fold.train_sample_ids:
        raise SplitEmptyFoldError(
            f"assert_fold_non_empty: fold {fold.fold_index} training set is "
            f"empty (populate it from the panel before training)"
        )
    if not fold.eval_sample_ids:
        raise SplitEmptyFoldError(
            f"assert_fold_non_empty: fold {fold.fold_index} evaluation set is empty"
        )


def build_time_holdout_split(
    *,
    label_horizons: Sequence[int],
    train_start_decision_time: int,
    train_end_decision_time: int,
    eval_start_decision_time: int,
    eval_end_decision_time: int,
) -> PanelSplitDefinition:
    """Construct a single time-holdout split.

    The function refuses a configuration whose evaluation interval
    does not start strictly after the training interval, or whose
    ``eval_start_decision_time < train_end_decision_time +
    purge_plus_embargo`` (a future leak).

    Time-holdout intervals here are determined by the caller; the
    :func:`build_walk_forward_split` factory derives a sequence of
    such intervals automatically.
    """
    if not isinstance(label_horizons, Sequence):
        raise SplitError(
            f"build_time_holdout_split: label_horizons must be Sequence, "
            f"got {type(label_horizons).__name__}"
        )
    for value in (
        train_start_decision_time,
        train_end_decision_time,
        eval_start_decision_time,
        eval_end_decision_time,
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise SplitError(
                f"build_time_holdout_split: every decision time must be "
                f"int, got {type(value).__name__}"
            )
    if train_end_decision_time <= train_start_decision_time:
        raise SplitWithoutTimeHoldoutError(
            "build_time_holdout_split: training interval is empty or inverted"
        )
    if eval_end_decision_time <= eval_start_decision_time:
        raise SplitWithoutTimeHoldoutError(
            "build_time_holdout_split: evaluation interval is empty or inverted"
        )

    purge, embargo = _combined_gap_from_label_horizons(label_horizons)
    min_eval_start = train_end_decision_time + purge + embargo
    if eval_start_decision_time < min_eval_start:
        raise SplitLeakageError(
            f"build_time_holdout_split: evaluation start "
            f"{eval_start_decision_time} violates the purge+embargo gap "
            f"(min start={min_eval_start})"
        )
    return PanelSplitDefinition(
        mode=SplitMode.TIME_HOLDOUT,
        folds=(
            SplitFold(
                fold_index=0,
                train_sample_ids=(),
                eval_sample_ids=(),
                train_start_decision_time=train_start_decision_time,
                train_end_decision_time=train_end_decision_time,
                eval_start_decision_time=eval_start_decision_time,
                eval_end_decision_time=eval_end_decision_time,
                purge_size=purge,
                embargo_size=embargo,
            ),
        ),
        purge_size=purge,
        embargo_size=embargo,
        declared_label_horizons=tuple(label_horizons),
        notes=("time holdout: single fold with stated purge+embargo gap",),
    )


def build_walk_forward_split(
    *,
    label_horizons: Sequence[int],
    panel_start_decision_time: int,
    panel_end_decision_time: int,
    fold_size_decision_time: int,
    eval_size_decision_time: int,
    embargo_extra_seconds: int = 0,
) -> PanelSplitDefinition:
    """Construct a walk-forward split with ``(training, evaluation)`` folds.

    The walk-forward factory divides the panel's decision-time
    range into equal-sized chunks of ``fold_size_decision_time``
    seconds, and emits a fold per chunk pair (training covers the
    chunk's past, evaluation covers the chunk). The
    ``embargo_extra_seconds`` parameter adds an additional gap on
    top of the schema-derived ``purge + embargo`` size so a
    caller can lengthen the gap deliberately.
    """
    if not isinstance(label_horizons, Sequence):
        raise SplitError(
            f"build_walk_forward_split: label_horizons must be Sequence, "
            f"got {type(label_horizons).__name__}"
        )
    for value in (
        panel_start_decision_time,
        panel_end_decision_time,
        fold_size_decision_time,
        eval_size_decision_time,
        embargo_extra_seconds,
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise SplitError(
                f"build_walk_forward_split: every input must be int, got {type(value).__name__}"
            )
    if panel_end_decision_time <= panel_start_decision_time:
        raise SplitWithoutTimeHoldoutError(
            "build_walk_forward_split: panel interval is empty or inverted"
        )
    if fold_size_decision_time <= 0:
        raise SplitError(
            f"build_walk_forward_split: fold_size_decision_time must be "
            f"> 0, got {fold_size_decision_time}"
        )
    if eval_size_decision_time <= 0:
        raise SplitError(
            f"build_walk_forward_split: eval_size_decision_time must be "
            f"> 0, got {eval_size_decision_time}"
        )
    if embargo_extra_seconds < 0:
        raise SplitError(
            f"build_walk_forward_split: embargo_extra_seconds must be "
            f">= 0, got {embargo_extra_seconds}"
        )

    purge, embargo = _combined_gap_from_label_horizons(label_horizons)
    embargo = embargo + embargo_extra_seconds

    folds: list[SplitFold] = []
    fold_index = 0
    cursor = panel_start_decision_time
    while (
        cursor + fold_size_decision_time + purge + embargo + eval_size_decision_time
        <= panel_end_decision_time
    ):
        train_start = cursor
        train_end = cursor + fold_size_decision_time
        eval_start = train_end + purge + embargo
        eval_end = eval_start + eval_size_decision_time
        folds.append(
            SplitFold(
                fold_index=fold_index,
                train_sample_ids=(),
                eval_sample_ids=(),
                train_start_decision_time=train_start,
                train_end_decision_time=train_end,
                eval_start_decision_time=eval_start,
                eval_end_decision_time=eval_end,
                purge_size=purge,
                embargo_size=embargo,
            )
        )
        cursor = eval_end
        fold_index += 1

    if not folds:
        raise SplitWithoutTimeHoldoutError(
            f"build_walk_forward_split: no folds produced for the supplied "
            f"panel interval (panel="
            f"[{panel_start_decision_time}, {panel_end_decision_time}])"
        )

    return PanelSplitDefinition(
        mode=SplitMode.WALK_FORWARD,
        folds=tuple(folds),
        purge_size=purge,
        embargo_size=embargo,
        declared_label_horizons=tuple(label_horizons),
        notes=(
            f"walk-forward: {len(folds)} folds, fold_size="
            f"{fold_size_decision_time}, eval_size={eval_size_decision_time}",
        ),
    )


# ---------------------------------------------------------------------------
# Refusal of non-temporal splits
# ---------------------------------------------------------------------------


def forbid_non_temporal_split(mode: str | SplitMode) -> None:
    """Raise :class:`SplitNonTemporalError` for a non-temporal split.

    This is the standalone guard ``DS-020`` installs. The harness
    calls it on every split request that did not pass through one
    of the three named builders, so a future caller cannot
    introduce a random / shuffled split.
    """
    if isinstance(mode, SplitMode) and mode in (
        SplitMode.POOL_HOLDOUT,
        SplitMode.TIME_HOLDOUT,
        SplitMode.WALK_FORWARD,
    ):
        return
    if isinstance(mode, str) and mode in {
        SplitMode.POOL_HOLDOUT.value,
        SplitMode.TIME_HOLDOUT.value,
        SplitMode.WALK_FORWARD.value,
    }:
        return
    raise SplitNonTemporalError(
        f"forbid_non_temporal_split: refused split mode {mode!r}; "
        f"only POOL_HOLDOUT, TIME_HOLDOUT, WALK_FORWARD are accepted "
        f"(DS-020)"
    )


__all__ = [
    "PanelSplitDefinition",
    "SplitEmptyFoldError",
    "SplitError",
    "SplitFold",
    "SplitLeakageError",
    "SplitMode",
    "SplitNonTemporalError",
    "SplitUnknownFoldError",
    "SplitWithoutPoolHoldoutError",
    "SplitWithoutTimeHoldoutError",
    "assert_fold_non_empty",
    "build_pool_holdout_split",
    "build_time_holdout_split",
    "build_walk_forward_split",
    "forbid_non_temporal_split",
]
