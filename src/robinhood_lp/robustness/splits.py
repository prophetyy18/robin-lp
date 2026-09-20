"""Robustness split definitions (T064).

This module owns the split primitives T064 binds to the robustness
contract. Every primitive carries an explicit unit, an explicit
purge/embargo length derived from a declared label horizon, and a
machine-recorded boundary record so the split can be reconstructed
without trusting the orchestrator's memory.

The T064 acceptance clauses the module satisfies:

- **Point-in-time boundaries.** A :class:`SplitBoundary` records the
  anchor value (a bar index or block number) the split was taken at;
  the boundary is machine-recorded before the run touches any data,
  and the recorded source names where the anchor came from.

- **Single purge/embargo gap, derived from the label horizon.** The
  :class:`LabelHorizon` is the single source of the embargo length.
  Every split reads :attr:`LabelHorizon.purge_embargo_length` and
  records it alongside the fold; a hand-chosen length is rejected at
  construction.

- **Three axes, pool holdout as primary.** :class:`WalkForwardSplit`
  and :class:`TimeHoldoutSplit` support time-based evaluation;
  :class:`PoolHoldoutSplit` is the pool-axis primary generalisation
  test, with results reported per held-out pool.

- **Untouched test holdout.** The time holdout keeps the test fold
  separate from train and validation; the walk-forward split does
  not consume the untouched test fold.

Design constraints (binding):

- **Determinism.** A given ``(chain_id, pool_key_id, label_horizon,
  fold_index)`` produces byte-identical boundary records in any
  process.

- **Integer-only.** All anchors are non-negative integers; the label
  horizon's unit is a closed-vocabulary string.

- **Layer purity.** This module imports only the standard library.
  It does not import the backtest engine, the manifest layer, the
  strategy layer, RPC, storage, signing, or presentation code.
  The robustness runner (in :mod:`robinhood_lp.robustness.runner`)
  is the integration point.

References:

- T064 — robustness and anti-overfitting analysis.
- ``docs/spec/research/DATASET_AND_EVALUATION.md`` DS-021 / DS-022.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: Module version. Bumping the version is a breaking change for
#: downstream consumers (the scenario catalogue, the report).
SPLITS_VERSION: Final[str] = "t064.robustness_splits.v1"

#: Closed vocabulary for the label horizon unit. The unit is stated
#: alongside the numeric length so a downstream consumer knows how
#: to apply the embargo length; per DS-022 the unit is the embargo
#: unit by construction.
VALID_HORIZON_UNITS: Final[frozenset[str]] = frozenset({"BARS", "BLOCKS"})

#: Closed vocabulary for the split axis. ``POOL_HOLDOUT`` is the
#: primary generalisation test (DS-021); ``TIME_HOLDOUT`` and
#: ``WALK_FORWARD`` are retained beside it.
VALID_AXIS_KINDS: Final[frozenset[str]] = frozenset(
    {"POOL_HOLDOUT", "TIME_HOLDOUT", "WALK_FORWARD"}
)

#: Closed vocabulary for the walk-forward window kind.
VALID_WINDOW_KINDS: Final[frozenset[str]] = frozenset({"ANCHORED", "ROLLING"})

#: Closed vocabulary for the fold role. ``TRAIN`` / ``VALIDATION``
#: are the inner folds a researcher tunes on; ``TEST`` is the
#: untouched holdout.
VALID_FOLD_ROLES: Final[frozenset[str]] = frozenset(
    {"TRAIN", "VALIDATION", "TEST", "TRAIN_POOL", "HOLDOUT_POOL"}
)

#: Closed vocabulary for the boundary recorded source. The source
#: must name where the anchor came from; ``INPUT_EVENT`` is the
#: canonical "first event of the next fold" source and
#: ``DATASET_VERSION`` is the canonical "block range" source.
VALID_RECORDED_SOURCES: Final[frozenset[str]] = frozenset(
    {"INPUT_EVENT", "DATASET_VERSION", "MANIFEST"}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RobustnessSplitError(ValueError):
    """Base class for split construction / validation failures."""


class InvalidLabelHorizonError(RobustnessSplitError):
    """A label horizon value is non-positive or its unit is closed-vocabulary."""


class InvalidSplitBoundaryError(RobustnessSplitError):
    """A split boundary carries an out-of-vocabulary axis / role / source."""


class HandChosenEmbargoError(RobustnessSplitError):
    """A purge/embargo length was supplied that disagrees with the label horizon.

    DS-022 binds a single embargo length per run, derived from the
    label horizon. A caller that supplies an embargo length not equal
    to ``label_horizon.value`` is hand-picking and the constructor
    refuses the construction.
    """


# ---------------------------------------------------------------------------
# Label horizon — single source of embargo length
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LabelHorizon:
    """The label horizon a robustness run is computed against.

    The horizon is the single source of the purge/embargo gap (DS-022).
    The unit is closed vocabulary so a downstream consumer knows
    whether to apply the embargo in bars or blocks; the embargo
    length equals the horizon length by construction, never hand-
    chosen. The unit is the embargo unit because the embargo must be
    expressed in the same unit the labels are measured in.

    Field units:

    - ``value`` — positive integer; the embargo length in
      ``unit`` units.
    - ``unit`` — one of :data:`VALID_HORIZON_UNITS`.
    """

    value: int
    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise InvalidLabelHorizonError(
                f"LabelHorizon.value: must be int, got {type(self.value).__name__}"
            )
        if self.value <= 0:
            raise InvalidLabelHorizonError(
                f"LabelHorizon.value: must be positive, got {self.value}"
            )
        if self.unit not in VALID_HORIZON_UNITS:
            raise InvalidLabelHorizonError(
                f"LabelHorizon.unit: must be one of {sorted(VALID_HORIZON_UNITS)}, "
                f"got {self.unit!r}"
            )

    @property
    def purge_embargo_length(self) -> int:
        """The single purge/embargo length for this horizon.

        The property is the only sanctioned way to obtain the embargo
        length; every split must read this property rather than
        accept a hand-chosen length from the caller.
        """
        return self.value

    @property
    def embargo_unit(self) -> str:
        """The unit the embargo length is expressed in."""
        return self.unit


# ---------------------------------------------------------------------------
# Split boundary — point-in-time, machine-recorded
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SplitBoundary:
    """A single point-in-time split boundary the machine recorded.

    Each boundary pins a single (chain, pool, axis, fold, segment)
    tuple to a specific anchor value. The anchor is recorded before
    any data event after it is consumed; the recorded source names
    where the anchor came from. The boundary is append-only: once
    constructed it is never mutated. Two equivalent constructions
    produce byte-identical records because every field is a closed
    type.

    Field units:

    - ``chain_id`` — positive integer (chain ID).
    - ``pool_key_id`` — non-empty hex string.
    - ``axis_kind`` — one of :data:`VALID_AXIS_KINDS`.
    - ``fold_index`` — non-negative integer.
    - ``fold_role`` — one of :data:`VALID_FOLD_ROLES`.
    - ``segment_label`` — non-empty string, e.g. ``"train_fold_0"``.
    - ``anchor_value`` — non-negative integer; the bar index or
      block number.
    - ``anchor_unit`` — one of :data:`VALID_HORIZON_UNITS`.
    - ``recorded_at_unix_seconds`` — integer Unix-seconds (the
      wall time the boundary was recorded; it is an
      observational field excluded from byte-equivalence).
    - ``recorded_source`` — one of
      :data:`VALID_RECORDED_SOURCES`.
    - ``boundary_id`` — non-empty string; a deterministic ID the
      machine assigns at construction so two equivalent boundaries
      in different processes share an identifier.
    """

    chain_id: int
    pool_key_id: str
    axis_kind: str
    fold_index: int
    fold_role: str
    segment_label: str
    anchor_value: int
    anchor_unit: str
    recorded_at_unix_seconds: int
    recorded_source: str
    boundary_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.chain_id: must be positive, got {self.chain_id}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        if self.axis_kind not in VALID_AXIS_KINDS:
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.axis_kind: must be one of {sorted(VALID_AXIS_KINDS)}, "
                f"got {self.axis_kind!r}"
            )
        if not isinstance(self.fold_index, int) or isinstance(self.fold_index, bool):
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.fold_index: must be int, got {type(self.fold_index).__name__}"
            )
        if self.fold_index < 0:
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.fold_index: must be non-negative, got {self.fold_index}"
            )
        if self.fold_role not in VALID_FOLD_ROLES:
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.fold_role: must be one of {sorted(VALID_FOLD_ROLES)}, "
                f"got {self.fold_role!r}"
            )
        if not isinstance(self.segment_label, str) or not self.segment_label:
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.segment_label: must be non-empty str, got {self.segment_label!r}"
            )
        if not isinstance(self.anchor_value, int) or isinstance(self.anchor_value, bool):
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.anchor_value: must be int, got {type(self.anchor_value).__name__}"
            )
        if self.anchor_value < 0:
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.anchor_value: must be non-negative, got {self.anchor_value}"
            )
        if self.anchor_unit not in VALID_HORIZON_UNITS:
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.anchor_unit: must be one of {sorted(VALID_HORIZON_UNITS)}, "
                f"got {self.anchor_unit!r}"
            )
        if not isinstance(self.recorded_at_unix_seconds, int) or isinstance(
            self.recorded_at_unix_seconds, bool
        ):
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.recorded_at_unix_seconds: must be int, got "
                f"{type(self.recorded_at_unix_seconds).__name__}"
            )
        if self.recorded_at_unix_seconds < 0:
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.recorded_at_unix_seconds: must be non-negative, got "
                f"{self.recorded_at_unix_seconds}"
            )
        if self.recorded_source not in VALID_RECORDED_SOURCES:
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.recorded_source: must be one of "
                f"{sorted(VALID_RECORDED_SOURCES)}, got {self.recorded_source!r}"
            )
        if not isinstance(self.boundary_id, str) or not self.boundary_id:
            raise InvalidSplitBoundaryError(
                f"SplitBoundary.boundary_id: must be non-empty str, got {self.boundary_id!r}"
            )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly ``dict`` representation.

        The serialisation is deterministic so two equivalent boundaries
        produce byte-identical dictionaries. The ``recorded_at_unix_seconds``
        field is included because it is part of the contract, but
        downstream consumers may treat it as observational when comparing
        across processes (it is the wall time the boundary was recorded,
        which the same logical boundary in two processes can have
        different values for).
        """
        return {
            "chain_id": self.chain_id,
            "pool_key_id": self.pool_key_id,
            "axis_kind": self.axis_kind,
            "fold_index": self.fold_index,
            "fold_role": self.fold_role,
            "segment_label": self.segment_label,
            "anchor_value": self.anchor_value,
            "anchor_unit": self.anchor_unit,
            "recorded_at_unix_seconds": self.recorded_at_unix_seconds,
            "recorded_source": self.recorded_source,
            "boundary_id": self.boundary_id,
        }


def make_boundary_id(
    *,
    chain_id: int,
    pool_key_id: str,
    axis_kind: str,
    fold_index: int,
    fold_role: str,
    segment_label: str,
    anchor_value: int,
    anchor_unit: str,
    recorded_source: str,
) -> str:
    """Return the deterministic boundary ID the machine assigns.

    The function is the canonical constructor for the boundary ID;
    the caller passes every structural field and the function
    computes a SHA-256 hex digest. Two equivalent inputs in any
    process produce the same boundary ID.
    """
    import hashlib

    content = (
        f"{chain_id}|{pool_key_id}|{axis_kind}|{fold_index}|{fold_role}|"
        f"{segment_label}|{anchor_value}|{anchor_unit}|{recorded_source}"
    )
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Time fold
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimeFold:
    """A single time-bounded fold a split uses.

    A fold is half-open: ``[anchor_start, anchor_end)`` in the unit
    the split declared (BARS or BLOCKS). Two folds of a split must
    not overlap; the embargo gap between them is declared by the
    parent split, not by the fold.

    Field units:

    - ``role`` — one of :data:`VALID_FOLD_ROLES`.
    - ``segment_label`` — non-empty string.
    - ``anchor_start`` — non-negative integer.
    - ``anchor_end`` — non-negative integer (``> anchor_start``).
    - ``anchor_unit`` — one of :data:`VALID_HORIZON_UNITS`.
    """

    role: str
    segment_label: str
    anchor_start: int
    anchor_end: int
    anchor_unit: str

    def __post_init__(self) -> None:
        if self.role not in VALID_FOLD_ROLES:
            raise InvalidSplitBoundaryError(
                f"TimeFold.role: must be one of {sorted(VALID_FOLD_ROLES)}, got {self.role!r}"
            )
        if not isinstance(self.segment_label, str) or not self.segment_label:
            raise InvalidSplitBoundaryError(
                f"TimeFold.segment_label: must be non-empty str, got {self.segment_label!r}"
            )
        if not isinstance(self.anchor_start, int) or isinstance(self.anchor_start, bool):
            raise InvalidSplitBoundaryError(
                f"TimeFold.anchor_start: must be int, got {type(self.anchor_start).__name__}"
            )
        if self.anchor_start < 0:
            raise InvalidSplitBoundaryError(
                f"TimeFold.anchor_start: must be non-negative, got {self.anchor_start}"
            )
        if not isinstance(self.anchor_end, int) or isinstance(self.anchor_end, bool):
            raise InvalidSplitBoundaryError(
                f"TimeFold.anchor_end: must be int, got {type(self.anchor_end).__name__}"
            )
        if self.anchor_end <= self.anchor_start:
            raise InvalidSplitBoundaryError(
                f"TimeFold.anchor_end={self.anchor_end} must be > anchor_start={self.anchor_start}"
            )
        if self.anchor_unit not in VALID_HORIZON_UNITS:
            raise InvalidSplitBoundaryError(
                f"TimeFold.anchor_unit: must be one of {sorted(VALID_HORIZON_UNITS)}, "
                f"got {self.anchor_unit!r}"
            )

    @property
    def length(self) -> int:
        """Return the fold length in the fold's anchor unit."""
        return self.anchor_end - self.anchor_start


# ---------------------------------------------------------------------------
# Walk-forward split (anchored / rolling)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    """An anchored or rolling walk-forward split (DS-021).

    A walk-forward split repeatedly trains on a past window and
    immediately evaluates on a forward window, optionally with an
    embargo gap between them. ``ANCHORED`` keeps the train start at
    the dataset origin across folds; ``ROLLING`` slides the train
    window forward by ``train_step`` units between folds.

    The embargo gap is read from the label horizon and recorded with
    the split. A caller that supplies a hand-chosen embargo length
    is rejected at construction.

    Field units:

    - ``chain_id`` / ``pool_key_id`` — pool identity.
    - ``fold_index`` — non-negative integer.
    - ``window_kind`` — ``"ANCHORED"`` or ``"ROLLING"``.
    - ``train`` / ``validation`` — :class:`TimeFold`.
    - ``label_horizon`` — :class:`LabelHorizon`.
    - ``boundaries`` — tuple of :class:`SplitBoundary`; one per
      fold edge.
    """

    chain_id: int
    pool_key_id: str
    fold_index: int
    window_kind: str
    train: TimeFold
    validation: TimeFold
    label_horizon: LabelHorizon
    boundaries: tuple[SplitBoundary, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise InvalidSplitBoundaryError(
                f"WalkForwardSplit.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise InvalidSplitBoundaryError(
                f"WalkForwardSplit.chain_id: must be positive, got {self.chain_id}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise InvalidSplitBoundaryError(
                f"WalkForwardSplit.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        if not isinstance(self.fold_index, int) or isinstance(self.fold_index, bool):
            raise InvalidSplitBoundaryError(
                f"WalkForwardSplit.fold_index: must be int, got {type(self.fold_index).__name__}"
            )
        if self.fold_index < 0:
            raise InvalidSplitBoundaryError(
                f"WalkForwardSplit.fold_index: must be non-negative, got {self.fold_index}"
            )
        if self.window_kind not in VALID_WINDOW_KINDS:
            raise InvalidSplitBoundaryError(
                f"WalkForwardSplit.window_kind: must be one of "
                f"{sorted(VALID_WINDOW_KINDS)}, got {self.window_kind!r}"
            )
        # Validate the train/validation pair.
        if self.train.anchor_unit != self.validation.anchor_unit:
            raise InvalidSplitBoundaryError(
                f"WalkForwardSplit: train.unit={self.train.anchor_unit} disagrees "
                f"with validation.unit={self.validation.anchor_unit}"
            )
        if self.train.anchor_unit != self.label_horizon.unit:
            raise InvalidSplitBoundaryError(
                f"WalkForwardSplit: train.unit={self.train.anchor_unit} disagrees "
                f"with label_horizon.unit={self.label_horizon.unit}"
            )
        # Validation must come strictly after train; the embargo is
        # implicit in the validation start - train end gap.
        gap = self.validation.anchor_start - self.train.anchor_end
        if gap < 0:
            raise InvalidSplitBoundaryError(
                f"WalkForwardSplit: validation.anchor_start="
                f"{self.validation.anchor_start} starts before "
                f"train.anchor_end={self.train.anchor_end}"
            )
        if gap != self.label_horizon.purge_embargo_length:
            raise HandChosenEmbargoError(
                f"WalkForwardSplit: gap={gap} between train and validation "
                f"disagrees with the derived label-horizon embargo "
                f"length={self.label_horizon.purge_embargo_length}; "
                f"the gap must be derived, not hand-chosen"
            )

    @property
    def embargo_length(self) -> int:
        """The single purge/embargo length this split uses."""
        return self.label_horizon.purge_embargo_length

    @property
    def embargo_unit(self) -> str:
        """The unit the embargo length is expressed in."""
        return self.label_horizon.embargo_unit


# ---------------------------------------------------------------------------
# Time holdout split
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimeHoldoutSplit:
    """A time-holdout split: train → embargo → validation → embargo → test.

    The test fold is the *untouched* holdout T064 binds as a
    deliverable. The split is the time-axis analogue of the
    pool-holdout split and is retained beside the pool holdout (per
    DS-021, time holdout is not the primary generalisation test).

    The embargo length is read from the label horizon and recorded
    with the split. Hand-chosen embargoes are rejected.

    Field units:

    - ``chain_id`` / ``pool_key_id`` — pool identity.
    - ``train`` / ``validation`` / ``test`` — :class:`TimeFold`.
    - ``label_horizon`` — :class:`LabelHorizon`.
    - ``boundaries`` — tuple of :class:`SplitBoundary`.
    """

    chain_id: int
    pool_key_id: str
    train: TimeFold
    validation: TimeFold
    test: TimeFold
    label_horizon: LabelHorizon
    boundaries: tuple[SplitBoundary, ...]

    def __post_init__(self) -> None:
        if self.train.role != "TRAIN":
            raise InvalidSplitBoundaryError(
                f"TimeHoldoutSplit: train.role must be 'TRAIN', got {self.train.role!r}"
            )
        if self.validation.role != "VALIDATION":
            raise InvalidSplitBoundaryError(
                f"TimeHoldoutSplit: validation.role must be 'VALIDATION', got "
                f"{self.validation.role!r}"
            )
        if self.test.role != "TEST":
            raise InvalidSplitBoundaryError(
                f"TimeHoldoutSplit: test.role must be 'TEST', got {self.test.role!r}"
            )
        unit = self.train.anchor_unit
        if self.validation.anchor_unit != unit or self.test.anchor_unit != unit:
            raise InvalidSplitBoundaryError(
                f"TimeHoldoutSplit: anchor units disagree "
                f"(train={self.train.anchor_unit}, "
                f"validation={self.validation.anchor_unit}, "
                f"test={self.test.anchor_unit})"
            )
        if unit != self.label_horizon.unit:
            raise InvalidSplitBoundaryError(
                f"TimeHoldoutSplit: train.unit={unit} disagrees with "
                f"label_horizon.unit={self.label_horizon.unit}"
            )
        # Ordering: train.end ≤ validation.start - embargo ≤ validation.end
        # ≤ test.start - embargo ≤ test.end
        gap_train_val = self.validation.anchor_start - self.train.anchor_end
        gap_val_test = self.test.anchor_start - self.validation.anchor_end
        if gap_train_val != self.label_horizon.purge_embargo_length:
            raise HandChosenEmbargoError(
                f"TimeHoldoutSplit: train→validation gap={gap_train_val} disagrees "
                f"with derived embargo length={self.label_horizon.purge_embargo_length}"
            )
        if gap_val_test != self.label_horizon.purge_embargo_length:
            raise HandChosenEmbargoError(
                f"TimeHoldoutSplit: validation→test gap={gap_val_test} disagrees "
                f"with derived embargo length={self.label_horizon.purge_embargo_length}"
            )
        if self.test.anchor_end <= self.validation.anchor_end:
            raise InvalidSplitBoundaryError(
                "TimeHoldoutSplit: test must come strictly after validation"
            )

    @property
    def embargo_length(self) -> int:
        return self.label_horizon.purge_embargo_length

    @property
    def embargo_unit(self) -> str:
        return self.label_horizon.embargo_unit

    @property
    def test_is_untouched(self) -> bool:
        """Return ``True`` if the test fold is strictly after validation.

        The T064 acceptance clause binds that the test fold is the
        untouched holdout; the property is the public assertion.
        """
        return self.test.anchor_start > self.validation.anchor_end


# ---------------------------------------------------------------------------
# Pool holdout split (primary generalisation test)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolHoldoutFold:
    """A single (chain, pool) fold a pool-holdout split uses.

    A pool-holdout fold is identified by its pool identity rather than
    a time interval; the time interval attached to the fold is the
    pool's own block range. The fold role is ``TRAIN_POOL`` (a pool
    used in training) or ``HOLDOUT_POOL`` (a pool used as the
    generalisation target).

    Field units:

    - ``chain_id`` / ``pool_key_id`` — pool identity.
    - ``fold_role`` — ``"TRAIN_POOL"`` or ``"HOLDOUT_POOL"``.
    - ``segment_label`` — non-empty string.
    - ``block_range_start`` / ``block_range_end`` — block numbers
      (``block_range_end >= block_range_start``).
    """

    chain_id: int
    pool_key_id: str
    fold_role: str
    segment_label: str
    block_range_start: int
    block_range_end: int

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutFold.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutFold.chain_id: must be positive, got {self.chain_id}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutFold.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        if self.fold_role not in ("TRAIN_POOL", "HOLDOUT_POOL"):
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutFold.fold_role: must be 'TRAIN_POOL' or "
                f"'HOLDOUT_POOL', got {self.fold_role!r}"
            )
        if not isinstance(self.segment_label, str) or not self.segment_label:
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutFold.segment_label: must be non-empty str, got {self.segment_label!r}"
            )
        if not isinstance(self.block_range_start, int) or isinstance(self.block_range_start, bool):
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutFold.block_range_start: must be int, got "
                f"{type(self.block_range_start).__name__}"
            )
        if self.block_range_start < 0:
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutFold.block_range_start: must be non-negative, got "
                f"{self.block_range_start}"
            )
        if not isinstance(self.block_range_end, int) or isinstance(self.block_range_end, bool):
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutFold.block_range_end: must be int, got "
                f"{type(self.block_range_end).__name__}"
            )
        if self.block_range_end < self.block_range_start:
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutFold.block_range_end={self.block_range_end} must be "
                f">= block_range_start={self.block_range_start}"
            )


@dataclass(frozen=True, slots=True)
class PoolHoldoutSplit:
    """Pool-holdout split — the primary generalisation test (DS-021).

    The train and holdout pool sets are disjoint: ``train_pool_folds``
    and ``holdout_pool_folds`` share no ``(chain_id, pool_key_id)``
    pair. Results are reported per held-out pool; the time holdout
    and walk-forward are retained beside it but never replace it as
    the primary generalisation statement.

    The embargo length is the single length derived from the label
    horizon and recorded with the split. Hand-chosen embargoes are
    rejected.

    Field units:

    - ``chain_id`` — positive integer.
    - ``train_pool_folds`` — non-empty tuple of
      :class:`PoolHoldoutFold` with role ``TRAIN_POOL``.
    - ``holdout_pool_folds`` — non-empty tuple of
      :class:`PoolHoldoutFold` with role ``HOLDOUT_POOL``.
    - ``label_horizon`` — :class:`LabelHorizon`.
    - ``boundaries`` — tuple of :class:`SplitBoundary`; one per
      held-out pool's first block.
    """

    chain_id: int
    train_pool_folds: tuple[PoolHoldoutFold, ...]
    holdout_pool_folds: tuple[PoolHoldoutFold, ...]
    label_horizon: LabelHorizon
    boundaries: tuple[SplitBoundary, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutSplit.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutSplit.chain_id: must be positive, got {self.chain_id}"
            )
        if not self.train_pool_folds:
            raise InvalidSplitBoundaryError("PoolHoldoutSplit: train_pool_folds must be non-empty")
        if not self.holdout_pool_folds:
            raise InvalidSplitBoundaryError(
                "PoolHoldoutSplit: holdout_pool_folds must be non-empty"
            )
        train_ids = {(f.chain_id, f.pool_key_id) for f in self.train_pool_folds}
        holdout_ids = {(f.chain_id, f.pool_key_id) for f in self.holdout_pool_folds}
        if train_ids & holdout_ids:
            shared = sorted(train_ids & holdout_ids)
            raise InvalidSplitBoundaryError(
                f"PoolHoldoutSplit: train and holdout pool sets overlap: {shared}"
            )
        for fold in self.train_pool_folds:
            if fold.fold_role != "TRAIN_POOL":
                raise InvalidSplitBoundaryError(
                    f"PoolHoldoutSplit: train_pool_folds contains a fold with "
                    f"role={fold.fold_role!r}"
                )
        for fold in self.holdout_pool_folds:
            if fold.fold_role != "HOLDOUT_POOL":
                raise InvalidSplitBoundaryError(
                    f"PoolHoldoutSplit: holdout_pool_folds contains a fold with "
                    f"role={fold.fold_role!r}"
                )

    @property
    def embargo_length(self) -> int:
        return self.label_horizon.purge_embargo_length

    @property
    def embargo_unit(self) -> str:
        return self.label_horizon.embargo_unit

    def holdout_pool_ids(self) -> tuple[tuple[int, str], ...]:
        """Return the disjoint held-out pool identities, in declaration order."""
        return tuple((f.chain_id, f.pool_key_id) for f in self.holdout_pool_folds)


# ---------------------------------------------------------------------------
# Split helpers — building splits from data
# ---------------------------------------------------------------------------


def build_time_holdout_split(
    *,
    chain_id: int,
    pool_key_id: str,
    label_horizon: LabelHorizon,
    unit_total: int,
    train_fraction: float,
    validation_fraction: float,
    recorded_at_unix_seconds: int,
    recorded_source: str = "MANIFEST",
) -> TimeHoldoutSplit:
    """Build a :class:`TimeHoldoutSplit` from a labelled total length.

    The total length ``unit_total`` is split into three contiguous
    blocks separated by embargoes derived from the label horizon.
    The ``train_fraction`` and ``validation_fraction`` are interpreted
    as fractions of the *pre-test* budget (train + validation); the
    test fold takes whatever remains. The fractions are closed
    vocabulary floats — the only floats the module uses, documented
    here as the layout input.

    Parameters
    ----------
    chain_id, pool_key_id:
        The pool identity.
    label_horizon:
        The :class:`LabelHorizon`; the embargo length is read from
        it and recorded with the split.
    unit_total:
        Total length of the pool's data in ``label_horizon.unit``
        units.
    train_fraction, validation_fraction:
        Non-negative floats summing to ≤ 1.0; they split the
        pre-test budget.
    recorded_at_unix_seconds:
        Wall-time the split was recorded.
    recorded_source:
        Where the anchor came from (defaults to ``"MANIFEST"``).
    """
    if not isinstance(unit_total, int) or isinstance(unit_total, bool):
        raise InvalidSplitBoundaryError(
            f"build_time_holdout_split: unit_total must be int, got {type(unit_total).__name__}"
        )
    if unit_total <= 0:
        raise InvalidSplitBoundaryError(
            f"build_time_holdout_split: unit_total must be positive, got {unit_total}"
        )
    if not isinstance(train_fraction, (int, float)) or isinstance(train_fraction, bool):
        raise InvalidSplitBoundaryError(
            f"build_time_holdout_split: train_fraction must be numeric, got "
            f"{type(train_fraction).__name__}"
        )
    if not isinstance(validation_fraction, (int, float)) or isinstance(validation_fraction, bool):
        raise InvalidSplitBoundaryError(
            f"build_time_holdout_split: validation_fraction must be numeric, got "
            f"{type(validation_fraction).__name__}"
        )
    if train_fraction < 0 or validation_fraction < 0:
        raise InvalidSplitBoundaryError(
            "build_time_holdout_split: train/validation fractions must be non-negative"
        )
    if train_fraction + validation_fraction > 1.0 + 1e-9:
        raise InvalidSplitBoundaryError(
            "build_time_holdout_split: train + validation fractions must sum to ≤ 1.0"
        )
    embargo = label_horizon.purge_embargo_length
    # Compute pre-test budget = total - test_length. We allocate the
    # embargo from each side and let the test fold take the rest.
    pre_test_total = int(unit_total * (train_fraction + validation_fraction))
    # Defensive: the layout may not leave enough room for two
    # embargoes plus the test fold. The minimum length requirement
    # is checked at the end so the user gets a clean error.
    test_total = max(0, unit_total - pre_test_total - 2 * embargo)
    train_total = int(unit_total * train_fraction)
    val_total = pre_test_total - train_total
    # Snap to integer lengths.
    if train_total <= 0 or val_total <= 0 or test_total <= 0:
        raise InvalidSplitBoundaryError(
            f"build_time_holdout_split: layout leaves a fold with no data "
            f"(train_total={train_total}, val_total={val_total}, "
            f"test_total={test_total}, embargo={embargo}, unit_total={unit_total})"
        )
    # Construct the three folds and the boundary records.
    unit = label_horizon.embargo_unit
    boundaries: list[SplitBoundary] = []
    train = TimeFold(
        role="TRAIN",
        segment_label="train_fold_0",
        anchor_start=0,
        anchor_end=train_total,
        anchor_unit=unit,
    )
    val_start = train.anchor_end + embargo
    val_end = val_start + val_total
    validation = TimeFold(
        role="VALIDATION",
        segment_label="validation_fold_0",
        anchor_start=val_start,
        anchor_end=val_end,
        anchor_unit=unit,
    )
    test_start = validation.anchor_end + embargo
    test_end = test_start + test_total
    test = TimeFold(
        role="TEST",
        segment_label="test_fold_0",
        anchor_start=test_start,
        anchor_end=test_end,
        anchor_unit=unit,
    )
    for role, seg_label, anchor in (
        ("TRAIN", train.segment_label, train.anchor_end),
        ("VALIDATION", validation.segment_label, validation.anchor_start),
        ("VALIDATION", validation.segment_label, validation.anchor_end),
        ("TEST", test.segment_label, test.anchor_start),
    ):
        boundary_id = make_boundary_id(
            chain_id=chain_id,
            pool_key_id=pool_key_id,
            axis_kind="TIME_HOLDOUT",
            fold_index=0,
            fold_role=role,
            segment_label=seg_label,
            anchor_value=anchor,
            anchor_unit=unit,
            recorded_source=recorded_source,
        )
        boundaries.append(
            SplitBoundary(
                chain_id=chain_id,
                pool_key_id=pool_key_id,
                axis_kind="TIME_HOLDOUT",
                fold_index=0,
                fold_role=role,
                segment_label=seg_label,
                anchor_value=anchor,
                anchor_unit=unit,
                recorded_at_unix_seconds=recorded_at_unix_seconds,
                recorded_source=recorded_source,
                boundary_id=boundary_id,
            )
        )
    return TimeHoldoutSplit(
        chain_id=chain_id,
        pool_key_id=pool_key_id,
        train=train,
        validation=validation,
        test=test,
        label_horizon=label_horizon,
        boundaries=tuple(boundaries),
    )


def build_walk_forward_splits(
    *,
    chain_id: int,
    pool_key_id: str,
    label_horizon: LabelHorizon,
    unit_total: int,
    train_length: int,
    validation_length: int,
    fold_step: int,
    window_kind: str,
    recorded_at_unix_seconds: int,
    recorded_source: str = "MANIFEST",
) -> tuple[WalkForwardSplit, ...]:
    """Build a series of :class:`WalkForwardSplit` records.

    The function returns one :class:`WalkForwardSplit` per fold; each
    fold covers a ``train_length`` past window followed by an
    embargo then a ``validation_length`` forward window. The
    ``fold_step`` controls how much the train window slides between
    folds (``ANCHORED`` keeps the train start at 0 across folds;
    ``ROLLING`` advances it by ``fold_step``).

    Parameters
    ----------
    chain_id, pool_key_id:
        Pool identity.
    label_horizon:
        :class:`LabelHorizon`; embargo length is read from it.
    unit_total:
        Total data length in ``label_horizon.unit`` units.
    train_length, validation_length:
        Per-fold window lengths in the same unit.
    fold_step:
        Slide between folds in the same unit.
    window_kind:
        ``"ANCHORED"`` or ``"ROLLING"``.
    recorded_at_unix_seconds:
        Wall time the splits were recorded.
    recorded_source:
        Where the anchor came from.
    """
    if window_kind not in VALID_WINDOW_KINDS:
        raise InvalidSplitBoundaryError(
            f"build_walk_forward_splits: window_kind must be one of "
            f"{sorted(VALID_WINDOW_KINDS)}, got {window_kind!r}"
        )
    for name, value in (
        ("unit_total", unit_total),
        ("train_length", train_length),
        ("validation_length", validation_length),
        ("fold_step", fold_step),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidSplitBoundaryError(
                f"build_walk_forward_splits: {name} must be int, got {type(value).__name__}"
            )
        if value <= 0:
            raise InvalidSplitBoundaryError(
                f"build_walk_forward_splits: {name} must be positive, got {value}"
            )
    embargo = label_horizon.purge_embargo_length
    unit = label_horizon.embargo_unit
    splits: list[WalkForwardSplit] = []
    fold_index = 0
    # The first fold starts at index 0; subsequent folds advance by
    # ``fold_step`` (rolling) or keep the train anchored at 0 and grow
    # the train window by ``fold_step`` each iteration (anchored).
    anchored_train_start = 0
    anchored_train_length = train_length
    rolling_train_start = 0
    while True:
        if window_kind == "ANCHORED":
            train_start = anchored_train_start
            current_train_length = anchored_train_length
        else:  # ROLLING
            train_start = rolling_train_start
            current_train_length = train_length
        train_end = train_start + current_train_length
        val_start = train_end + embargo
        val_end = val_start + validation_length
        if val_end > unit_total:
            break
        train = TimeFold(
            role="TRAIN",
            segment_label=f"train_fold_{fold_index}",
            anchor_start=train_start,
            anchor_end=train_end,
            anchor_unit=unit,
        )
        validation = TimeFold(
            role="VALIDATION",
            segment_label=f"validation_fold_{fold_index}",
            anchor_start=val_start,
            anchor_end=val_end,
            anchor_unit=unit,
        )
        boundaries: list[SplitBoundary] = []
        for role, seg_label, anchor in (
            ("TRAIN", train.segment_label, train.anchor_end),
            ("VALIDATION", validation.segment_label, validation.anchor_start),
            ("VALIDATION", validation.segment_label, validation.anchor_end),
        ):
            boundary_id = make_boundary_id(
                chain_id=chain_id,
                pool_key_id=pool_key_id,
                axis_kind="WALK_FORWARD",
                fold_index=fold_index,
                fold_role=role,
                segment_label=seg_label,
                anchor_value=anchor,
                anchor_unit=unit,
                recorded_source=recorded_source,
            )
            boundaries.append(
                SplitBoundary(
                    chain_id=chain_id,
                    pool_key_id=pool_key_id,
                    axis_kind="WALK_FORWARD",
                    fold_index=fold_index,
                    fold_role=role,
                    segment_label=seg_label,
                    anchor_value=anchor,
                    anchor_unit=unit,
                    recorded_at_unix_seconds=recorded_at_unix_seconds,
                    recorded_source=recorded_source,
                    boundary_id=boundary_id,
                )
            )
        splits.append(
            WalkForwardSplit(
                chain_id=chain_id,
                pool_key_id=pool_key_id,
                fold_index=fold_index,
                window_kind=window_kind,
                train=train,
                validation=validation,
                label_horizon=label_horizon,
                boundaries=tuple(boundaries),
            )
        )
        if window_kind == "ANCHORED":
            anchored_train_length += fold_step
        else:  # ROLLING
            rolling_train_start += fold_step
        fold_index += 1
    if not splits:
        raise InvalidSplitBoundaryError(
            f"build_walk_forward_splits: no fold fits in unit_total={unit_total} "
            f"(train_length={train_length}, validation_length={validation_length}, "
            f"embargo={embargo})"
        )
    return tuple(splits)


def build_pool_holdout_split(
    *,
    chain_id: int,
    train_pool_folds: Sequence[PoolHoldoutFold],
    holdout_pool_folds: Sequence[PoolHoldoutFold],
    label_horizon: LabelHorizon,
    recorded_at_unix_seconds: int,
    recorded_source: str = "MANIFEST",
) -> PoolHoldoutSplit:
    """Build a :class:`PoolHoldoutSplit` from disjoint pool sets.

    Parameters
    ----------
    chain_id:
        Chain ID all pools belong to.
    train_pool_folds, holdout_pool_folds:
        The disjoint pool sets; the function verifies the sets do
        not overlap before constructing the split.
    label_horizon:
        :class:`LabelHorizon`; embargo length is read from it.
    recorded_at_unix_seconds:
        Wall time the split was recorded.
    recorded_source:
        Where the anchor came from.
    """
    boundaries: list[SplitBoundary] = []
    for fold in holdout_pool_folds:
        boundary_id = make_boundary_id(
            chain_id=fold.chain_id,
            pool_key_id=fold.pool_key_id,
            axis_kind="POOL_HOLDOUT",
            fold_index=0,
            fold_role="HOLDOUT_POOL",
            segment_label=fold.segment_label,
            anchor_value=fold.block_range_start,
            anchor_unit="BLOCKS",
            recorded_source=recorded_source,
        )
        boundaries.append(
            SplitBoundary(
                chain_id=fold.chain_id,
                pool_key_id=fold.pool_key_id,
                axis_kind="POOL_HOLDOUT",
                fold_index=0,
                fold_role="HOLDOUT_POOL",
                segment_label=fold.segment_label,
                anchor_value=fold.block_range_start,
                anchor_unit="BLOCKS",
                recorded_at_unix_seconds=recorded_at_unix_seconds,
                recorded_source=recorded_source,
                boundary_id=boundary_id,
            )
        )
    return PoolHoldoutSplit(
        chain_id=chain_id,
        train_pool_folds=tuple(train_pool_folds),
        holdout_pool_folds=tuple(holdout_pool_folds),
        label_horizon=label_horizon,
        boundaries=tuple(boundaries),
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "SPLITS_VERSION",
    "VALID_AXIS_KINDS",
    "VALID_FOLD_ROLES",
    "VALID_HORIZON_UNITS",
    "VALID_RECORDED_SOURCES",
    "VALID_WINDOW_KINDS",
    "HandChosenEmbargoError",
    "InvalidLabelHorizonError",
    "InvalidSplitBoundaryError",
    "LabelHorizon",
    "PoolHoldoutFold",
    "PoolHoldoutSplit",
    "RobustnessSplitError",
    "SplitBoundary",
    "TimeFold",
    "TimeHoldoutSplit",
    "WalkForwardSplit",
    "build_pool_holdout_split",
    "build_time_holdout_split",
    "build_walk_forward_splits",
    "make_boundary_id",
]
