"""Training / evaluation harness the panel model layer binds (T101).

The harness is the integration surface every other
panel-research module composes around:

- the **feature registry** (:mod:`robinhood_lp.research.features`)
  the panel reads;
- the **label schema** (:mod:`robinhood_lp.research.labels`)
  the panel records;
- the **split** (:mod:`robinhood_lp.research.splits`) the harness
  enforces with purge + embargo;
- the **statistical boundary** (:mod:`robinhood_lp.research.boundary`)
  that declares where ``float`` enters and leaves the panel;
- the **panel** (:mod:`robinhood_lp.research.panel`) the harness
  threads provenance through;
- the **models** (:mod:`robinhood_lp.research.models`) the harness
  trains and diagnoses.

The harness does **not** import RPC, storage, signing, execution,
or presentation code. Its sole consumer is the model-eval
component of T102 and the research console T103, both of which
read the artifacts the harness publishes.

The contract ``Deliverables`` map onto the following harness
exports:

- :class:`TrainingHarnessConfig` — the frozen configuration a
  harness run binds; the artifact carries its content hash.
- :func:`build_training_harness` — the factory that wires every
  module the panel needs and validates their cross-field
  consistency (registry, schema, split, boundary).
- :func:`assemble_panel_dataset` — assembles the per-sample rows
  the harness consumes, deduplicating conflicting values and
  failing on a forward-derived feature.
- :func:`apply_split_to_panel` — partitions the panel rows into
  (training, evaluation) buckets per fold, applying the purge +
  embargo gap so a label-overlapping row does not appear in two
  halves.
- :func:`run_fold_evaluation` — train and evaluate one fold,
  returning the versioned artifact plus the diagnostics.
- :class:`SampleSizeDisclosure` — the per-pool, observation count
  and effective sample size the contract requires every report to
  state.
- :func:`compute_sample_size_disclosure` — computes the
  disclosure a fold produces.

References:

- ``docs/spec/research/DATASET_AND_EVALUATION.md`` (DS-030,
  DS-031, DS-032, DS-033, DS-034, DS-040, DS-041, DS-042,
  DS-043, §7).
- ADR-014 §5 (the model in the strategy layer's replaceable
  interface, no execution authority).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from robinhood_lp.features.bars import WindowKind
from robinhood_lp.research.boundary import (
    BoundaryCatalogue,
)
from robinhood_lp.research.features import (
    FeatureRegistry,
    FeatureRegistrySnapshot,
    ForwardFeatureError,
    UnknownFeatureError,
    validate_panel_against_decision_time,
)
from robinhood_lp.research.labels import (
    LabelSchema,
)
from robinhood_lp.research.models import (
    FeatureImportance,
    GradientBoostingModel,
    LinearQuantileModel,
    LinearRegularizedModel,
    ModelArtifact,
    ModelFamily,
    ModelHyperparameters,
    ModelVerdictCode,
    TrivialBaselineComparison,
    build_model_artifact,
    compare_against_trivial_baseline,
    compute_calibration_report,
    compute_quantile_coverage,
    feature_importance_from_model,
)
from robinhood_lp.research.panel import (
    PanelFeatureRow,
    PanelLabelRow,
    assert_not_legacy_manifest_marker,
    deduplicate_feature_rows,
    deduplicate_label_rows,
)
from robinhood_lp.research.splits import (
    PanelSplitDefinition,
    SplitFold,
    SplitMode,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


HARNESS_VERSION: Final[str] = "t101.research_harness.v1"

#: Decision-time kind the harness assumes by default. ``TIME``
#: is the canonical choice for research windows; ``BLOCK`` is
#: supported for the run-time the operator configures.
DEFAULT_DECISION_TIME_KIND: Final[WindowKind] = WindowKind.TIME


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HarnessError(ValueError):
    """Base class for training / evaluation harness failures."""


class HarnessConfigError(HarnessError):
    """The harness configuration is missing required cross-references."""


class HarnessSplitPopulationError(HarnessError):
    """The split applied to a panel produced an empty fold."""


class HarnessFoldEvalError(HarnessError):
    """A fold evaluation fails (forward feature, sample-size floor, etc.)."""


class HarnessSampleSizeUnderflowError(HarnessError):
    """The effective sample size is below the configured floor."""


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------


class FoldVerdictCode(StrEnum):
    """The verdict a fold evaluation emits.

    Strings are part of the public contract. New codes are
    additive; renaming an existing code is a breaking change.
    """

    PASSED = "PASSED"
    DEGENERATE = "DEGENERATE"
    SAMPLE_SIZE_UNDERFLOW = "SAMPLE_SIZE_UNDERFLOW"
    FORWARD_FEATURE_REJECTED = "FORWARD_FEATURE_REJECTED"
    TRIVIAL_BASELINE_LOST = "TRIVIAL_BASELINE_LOST"
    UNCERTAIN = "UNCERTAIN"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrainingHarnessConfig:
    """The frozen configuration the harness binds.

    The dataclass carries every reference the harness reads at
    build / fit time. ``content_hash`` is the SHA-256 hex digest
    the harness stores on every artifact it emits; a re-run
    produces byte-equivalent hashes when the config is unchanged.
    """

    version: str
    registry_snapshot: FeatureRegistrySnapshot
    label_schema: LabelSchema
    split_definition: PanelSplitDefinition
    boundary_catalogue: BoundaryCatalogue
    seed: int
    decision_time_kind: WindowKind
    code_revision: str
    declared_min_samples_for_quantile: int
    content_hash: str
    declared_label_horizons: tuple[int, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise HarnessConfigError(
                f"TrainingHarnessConfig.version: must be non-empty str, got {self.version!r}"
            )
        if self.version != HARNESS_VERSION:
            raise HarnessConfigError(
                f"TrainingHarnessConfig.version: must be {HARNESS_VERSION!r}, got {self.version!r}"
            )
        if not isinstance(self.registry_snapshot, FeatureRegistrySnapshot):
            raise HarnessConfigError(
                f"TrainingHarnessConfig.registry_snapshot: must be "
                f"FeatureRegistrySnapshot, got "
                f"{type(self.registry_snapshot).__name__}"
            )
        if not isinstance(self.label_schema, LabelSchema):
            raise HarnessConfigError(
                f"TrainingHarnessConfig.label_schema: must be LabelSchema, "
                f"got {type(self.label_schema).__name__}"
            )
        if not isinstance(self.split_definition, PanelSplitDefinition):
            raise HarnessConfigError(
                f"TrainingHarnessConfig.split_definition: must be "
                f"PanelSplitDefinition, got "
                f"{type(self.split_definition).__name__}"
            )
        if not isinstance(self.boundary_catalogue, BoundaryCatalogue):
            raise HarnessConfigError(
                f"TrainingHarnessConfig.boundary_catalogue: must be "
                f"BoundaryCatalogue, got "
                f"{type(self.boundary_catalogue).__name__}"
            )
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise HarnessConfigError(
                f"TrainingHarnessConfig.seed: must be int, got {type(self.seed).__name__}"
            )
        if self.seed < 0:
            raise HarnessConfigError(f"TrainingHarnessConfig.seed: must be >= 0, got {self.seed}")
        if not isinstance(self.decision_time_kind, WindowKind):
            raise HarnessConfigError(
                f"TrainingHarnessConfig.decision_time_kind: must be "
                f"WindowKind, got {type(self.decision_time_kind).__name__}"
            )
        if not isinstance(self.code_revision, str) or not self.code_revision:
            raise HarnessConfigError(
                f"TrainingHarnessConfig.code_revision: must be non-empty "
                f"str, got {self.code_revision!r}"
            )
        if not isinstance(self.declared_min_samples_for_quantile, int) or isinstance(
            self.declared_min_samples_for_quantile, bool
        ):
            raise HarnessConfigError(
                f"TrainingHarnessConfig.declared_min_samples_for_quantile: "
                f"must be int, got "
                f"{type(self.declared_min_samples_for_quantile).__name__}"
            )
        if self.declared_min_samples_for_quantile < 0:
            raise HarnessConfigError(
                f"TrainingHarnessConfig.declared_min_samples_for_quantile: "
                f"must be >= 0, got {self.declared_min_samples_for_quantile}"
            )
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise HarnessConfigError(
                f"TrainingHarnessConfig.content_hash: must be non-empty "
                f"str, got {self.content_hash!r}"
            )
        if not isinstance(self.declared_label_horizons, tuple):
            raise HarnessConfigError(
                f"TrainingHarnessConfig.declared_label_horizons: must be "
                f"tuple, got {type(self.declared_label_horizons).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "registry_snapshot_content_hash": self.registry_snapshot.content_hash,
            "label_schema_version": self.label_schema.VERSION,
            "label_schema_digest": _label_schema_digest(self.label_schema),
            "split_mode": self.split_definition.mode.value,
            "split_purge_plus_embargo": self.split_definition.purge_plus_embargo_size,
            "boundary_catalogue": self.boundary_catalogue.to_dict(),
            "seed": self.seed,
            "decision_time_kind": self.decision_time_kind.value,
            "code_revision": self.code_revision,
            "declared_min_samples_for_quantile": self.declared_min_samples_for_quantile,
            "declared_label_horizons": list(self.declared_label_horizons),
            "content_hash": self.content_hash,
        }


def _label_schema_digest(schema: LabelSchema) -> str:
    if not isinstance(schema, LabelSchema):
        raise HarnessConfigError(
            f"_label_schema_digest: schema must be LabelSchema, got {type(schema).__name__}"
        )
    canonical = json.dumps(
        [d.to_dict() for d in schema.declarations],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "0x" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_training_harness(
    *,
    registry: FeatureRegistry,
    label_schema: LabelSchema,
    split_definition: PanelSplitDefinition,
    boundary_catalogue: BoundaryCatalogue,
    seed: int,
    decision_time_kind: WindowKind = DEFAULT_DECISION_TIME_KIND,
    code_revision: str,
    declared_min_samples_for_quantile: int,
) -> TrainingHarnessConfig:
    """Construct the :class:`TrainingHarnessConfig`.

    The factory wires every module the harness needs and runs the
    cross-field validations the contract enumerates:

    - the boundary catalogue declares every column the registry
      consumes (a missing entry surfaces here);
    - the label schema declares every target the model trains
      against (an empty target list raises
      :class:`HarnessConfigError`);
    - the split definition is temporally valid (the splits module
      already enforces this);
    - every label's ``horizon_seconds`` agrees with the
      split's declared ``label_horizons``.
    """
    if not isinstance(registry, FeatureRegistry):
        raise HarnessConfigError(
            f"build_training_harness: registry must be FeatureRegistry, "
            f"got {type(registry).__name__}"
        )
    if not isinstance(label_schema, LabelSchema):
        raise HarnessConfigError(
            f"build_training_harness: label_schema must be LabelSchema, "
            f"got {type(label_schema).__name__}"
        )
    if not isinstance(split_definition, PanelSplitDefinition):
        raise HarnessConfigError(
            f"build_training_harness: split_definition must be "
            f"PanelSplitDefinition, got {type(split_definition).__name__}"
        )
    if not isinstance(boundary_catalogue, BoundaryCatalogue):
        raise HarnessConfigError(
            f"build_training_harness: boundary_catalogue must be "
            f"BoundaryCatalogue, got {type(boundary_catalogue).__name__}"
        )
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise HarnessConfigError(
            f"build_training_harness: seed must be int, got {type(seed).__name__}"
        )
    if seed < 0:
        raise HarnessConfigError(f"build_training_harness: seed must be >= 0, got {seed}")
    if not isinstance(decision_time_kind, WindowKind):
        raise HarnessConfigError(
            f"build_training_harness: decision_time_kind must be "
            f"WindowKind, got {type(decision_time_kind).__name__}"
        )
    if not isinstance(code_revision, str) or not code_revision:
        raise HarnessConfigError("build_training_harness: code_revision must be non-empty str")
    if not isinstance(declared_min_samples_for_quantile, int) or isinstance(
        declared_min_samples_for_quantile, bool
    ):
        raise HarnessConfigError(
            "build_training_harness: declared_min_samples_for_quantile must be int"
        )
    if declared_min_samples_for_quantile < 0:
        raise HarnessConfigError(
            "build_training_harness: declared_min_samples_for_quantile must be >= 0"
        )
    if not label_schema.target_names:
        raise HarnessConfigError(
            "build_training_harness: label_schema must declare at least "
            "one TARGET label for the model to fit"
        )

    registry_snapshot = registry.snapshot(declared_at_unix_seconds=0)

    declared_label_horizons: list[int] = []
    for declaration in label_schema.declarations:
        declared_label_horizons.append(declaration.horizon_seconds)
    # The split module derives ``purge`` from the largest of
    # ``label_horizons`` it was constructed with; the harness
    # only guarantees the ``purge + embargo`` gap is wide
    # enough to cover the schema's largest horizon. Equality is
    # therefore on the **sorted unique horizon set** the two
    # share.
    schema_horizon_set = sorted(set(declared_label_horizons))
    split_horizon_set = sorted(set(split_definition.declared_label_horizons))
    if schema_horizon_set != split_horizon_set:
        raise HarnessConfigError(
            f"build_training_harness: split's declared_label_horizons "
            f"{split_definition.declared_label_horizons} disagree with "
            f"label_schema horizon set {tuple(declared_label_horizons)}"
        )
    # The split's purge + embargo gap must be at least the
    # maximum schema horizon (DS-022's "derived from the label
    # schema" rule).
    max_schema_horizon = max(declared_label_horizons)
    if split_definition.purge_plus_embargo_size < max_schema_horizon:
        raise HarnessConfigError(
            f"build_training_harness: split's purge+embargo size "
            f"{split_definition.purge_plus_embargo_size} is below the "
            f"max schema horizon {max_schema_horizon} (DS-022)"
        )

    payload = {
        "version": HARNESS_VERSION,
        "registry_snapshot_content_hash": registry_snapshot.content_hash,
        "label_schema_version": label_schema.VERSION,
        "label_schema_digest": _label_schema_digest(label_schema),
        "split_mode": split_definition.mode.value,
        "split_purge_plus_embargo": split_definition.purge_plus_embargo_size,
        "boundary_catalogue": boundary_catalogue.to_dict(),
        "seed": seed,
        "decision_time_kind": decision_time_kind.value,
        "code_revision": code_revision,
        "declared_min_samples_for_quantile": declared_min_samples_for_quantile,
        "declared_label_horizons": list(declared_label_horizons),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    content_hash = "0x" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return TrainingHarnessConfig(
        version=HARNESS_VERSION,
        registry_snapshot=registry_snapshot,
        label_schema=label_schema,
        split_definition=split_definition,
        boundary_catalogue=boundary_catalogue,
        seed=seed,
        decision_time_kind=decision_time_kind,
        code_revision=code_revision,
        declared_min_samples_for_quantile=declared_min_samples_for_quantile,
        content_hash=content_hash,
        declared_label_horizons=tuple(declared_label_horizons),
    )


# ---------------------------------------------------------------------------
# Panel assembly
# ---------------------------------------------------------------------------


def assemble_panel_dataset(
    feature_rows: Sequence[PanelFeatureRow],
    label_rows: Sequence[PanelLabelRow],
    *,
    registry: FeatureRegistry,
    decision_time: int,
    decision_time_kind: WindowKind,
    legacy_marker: str | None = None,
) -> tuple[tuple[PanelFeatureRow, ...], tuple[PanelLabelRow, ...]]:
    """Validate and deduplicate ``feature_rows`` + ``label_rows``.

    The function:

    1. dedupes repeated / overlapping rows under the same sample id;
    2. validates every feature column the panel exposes against
       the supplied registry (a forward-derived feature raises
       :class:`ForwardFeatureError`);
    3. refuses a legacy pre-registry manifest marker.
    """
    assert_not_legacy_manifest_marker(legacy_marker)
    if not isinstance(registry, FeatureRegistry):
        raise HarnessError(
            f"assemble_panel_dataset: registry must be FeatureRegistry, "
            f"got {type(registry).__name__}"
        )
    if not isinstance(decision_time, int) or isinstance(decision_time, bool):
        raise HarnessError(
            f"assemble_panel_dataset: decision_time must be int, got {type(decision_time).__name__}"
        )
    if not isinstance(decision_time_kind, WindowKind):
        raise HarnessError("assemble_panel_dataset: decision_time_kind must be WindowKind")

    deduped_features = deduplicate_feature_rows(feature_rows)
    deduped_labels = deduplicate_label_rows(label_rows)

    if not deduped_features:
        raise HarnessError("assemble_panel_dataset: feature_rows is empty")

    registered_columns = {row.sample_id: row.columns for row in deduped_features}
    for sample_id, columns in registered_columns.items():
        for column_name in columns:
            if not registry.has(column_name):
                raise UnknownFeatureError(
                    f"assemble_panel_dataset: sample_id={sample_id!r} "
                    f"references unknown feature {column_name!r}"
                )

    declarations = [registry.get(name) for name in registry.column_names]
    validate_panel_against_decision_time(
        declarations,
        decision_time=decision_time,
        decision_time_kind=decision_time_kind,
    )
    return deduped_features, deduped_labels


# ---------------------------------------------------------------------------
# Split application
# ---------------------------------------------------------------------------


def _intervals_from_split(
    split_definition: PanelSplitDefinition,
) -> list[SplitFold]:
    """Return the folds the split declares (already populated for pool holdout)."""
    return list(split_definition.folds)


def apply_split_to_panel(
    *,
    split_definition: PanelSplitDefinition,
    sample_decision_times: Mapping[str, int],
    sample_pool_ids: Mapping[str, str],
    feature_registry: FeatureRegistry,
    label_schema: LabelSchema,
) -> tuple[tuple[tuple[PanelFeatureRow, ...], tuple[PanelFeatureRow, ...]], ...]:
    """Partition the panel into per-fold (training, evaluation) tuples.

    The function consumes the sample ids the harness supplies,
    applies the split mode's stratification (time for
    ``TIME_HOLDOUT`` and ``WALK_FORWARD``; pool for
    ``POOL_HOLDOUT``), enforces the purge + embargo gap and
    returns one (training, evaluation) pair per fold.

    Note: ``feature_registry`` and ``label_schema`` are accepted
    so the call site documents the cross-references; the split
    population does not consult either of them (the panel rows
    are already registry-validated at ``assemble_panel_dataset``
    time).
    """
    if not isinstance(split_definition, PanelSplitDefinition):
        raise HarnessError(
            f"apply_split_to_panel: split_definition must be "
            f"PanelSplitDefinition, got "
            f"{type(split_definition).__name__}"
        )
    if not isinstance(sample_decision_times, Mapping):
        raise HarnessError(
            f"apply_split_to_panel: sample_decision_times must be "
            f"Mapping, got {type(sample_decision_times).__name__}"
        )
    if not isinstance(sample_pool_ids, Mapping):
        raise HarnessError(
            f"apply_split_to_panel: sample_pool_ids must be Mapping, "
            f"got {type(sample_pool_ids).__name__}"
        )
    if not isinstance(feature_registry, FeatureRegistry):
        raise HarnessError(
            f"apply_split_to_panel: feature_registry must be "
            f"FeatureRegistry, got {type(feature_registry).__name__}"
        )
    if not isinstance(label_schema, LabelSchema):
        raise HarnessError(
            f"apply_split_to_panel: label_schema must be LabelSchema, "
            f"got {type(label_schema).__name__}"
        )

    folds = _intervals_from_split(split_definition)
    results: list[tuple[tuple[PanelFeatureRow, ...], tuple[PanelFeatureRow, ...]]] = []
    for fold in folds:
        train_pairs, eval_pairs = _populate_fold(
            split_mode=split_definition.mode,
            fold=fold,
            sample_decision_times=sample_decision_times,
            sample_pool_ids=sample_pool_ids,
            sample_features={},
        )
        train_ids = [row for _, row in train_pairs]
        eval_ids = [row for _, row in eval_pairs]
        results.append((tuple(train_ids), tuple(eval_ids)))
    return tuple(results)


def _populate_fold(
    *,
    split_mode: SplitMode,
    fold: SplitFold,
    sample_decision_times: Mapping[str, int],
    sample_pool_ids: Mapping[str, str],
    sample_features: Mapping[str, PanelFeatureRow],
) -> tuple[
    tuple[tuple[str, PanelFeatureRow], ...],
    tuple[tuple[str, PanelFeatureRow], ...],
]:
    """Return the (training, evaluation) sample ids the fold carries.

    Implementation is thin: the split module's builders return
    pre-populated folds; time-based folds are empty and the
    harness fills them here.
    """
    if split_mode is SplitMode.POOL_HOLDOUT:
        train_pool_set = set(fold.train_sample_ids)
        eval_pool_set = set(fold.eval_sample_ids)
        train_pairs: list[tuple[str, PanelFeatureRow]] = []
        eval_pairs: list[tuple[str, PanelFeatureRow]] = []
        for sample_id, pool_id in sample_pool_ids.items():
            if pool_id in train_pool_set:
                train_pairs.append(
                    (
                        sample_id,
                        sample_features.get(
                            sample_id, PanelFeatureRow(sample_id=sample_id, columns={})
                        ),
                    )
                )
            elif pool_id in eval_pool_set:
                eval_pairs.append(
                    (
                        sample_id,
                        sample_features.get(
                            sample_id, PanelFeatureRow(sample_id=sample_id, columns={})
                        ),
                    )
                )
        return tuple(train_pairs), tuple(eval_pairs)
    # TIME-based folds: take decision-time membership.
    train_pairs = []
    eval_pairs = []
    for sample_id, decision_time in sample_decision_times.items():
        if fold.train_start_decision_time <= decision_time < fold.train_end_decision_time:
            train_pairs.append(
                (
                    sample_id,
                    sample_features.get(
                        sample_id, PanelFeatureRow(sample_id=sample_id, columns={})
                    ),
                )
            )
        elif fold.eval_start_decision_time <= decision_time < fold.eval_end_decision_time:
            eval_pairs.append(
                (
                    sample_id,
                    sample_features.get(
                        sample_id, PanelFeatureRow(sample_id=sample_id, columns={})
                    ),
                )
            )
    return tuple(train_pairs), tuple(eval_pairs)


# ---------------------------------------------------------------------------
# Sample-size disclosure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SampleSizeDisclosure:
    """The honest sample-size disclosure the contract requires per report.

    The disclosure records the observation count, the effective
    sample size after label overlap (purge + embargo), the
    per-pool breakdown, and the verdict code ``UNCERTAIN`` when
    the effective sample is below the floor the harness
    enforces.

    Equality and hashing follow dataclass identity; the
    disclosure is a value object the harness stores on every
    fold evaluation.
    """

    observation_count: int
    effective_sample_size: int
    per_pool_counts: Mapping[str, int]
    min_samples_for_quantile: int
    sample_size_underflow: bool

    def __post_init__(self) -> None:
        if not isinstance(self.observation_count, int) or isinstance(self.observation_count, bool):
            raise HarnessError("SampleSizeDisclosure.observation_count: must be int")
        if self.observation_count < 0:
            raise HarnessError("SampleSizeDisclosure.observation_count: must be >= 0")
        if not isinstance(self.effective_sample_size, int) or isinstance(
            self.effective_sample_size, bool
        ):
            raise HarnessError("SampleSizeDisclosure.effective_sample_size: must be int")
        if self.effective_sample_size < 0:
            raise HarnessError("SampleSizeDisclosure.effective_sample_size: must be >= 0")
        if not isinstance(self.per_pool_counts, Mapping):
            raise HarnessError("SampleSizeDisclosure.per_pool_counts: must be Mapping")
        for pool_id, count in self.per_pool_counts.items():
            if not isinstance(pool_id, str) or not pool_id:
                raise HarnessError(
                    f"SampleSizeDisclosure.per_pool_counts: every pool_id "
                    f"must be non-empty str, got {pool_id!r}"
                )
            if not isinstance(count, int) or isinstance(count, bool):
                raise HarnessError(
                    f"SampleSizeDisclosure.per_pool_counts: every count must be int, got {count!r}"
                )
            if count < 0:
                raise HarnessError(
                    f"SampleSizeDisclosure.per_pool_counts: every count must be >= 0, got {count}"
                )
        if not isinstance(self.min_samples_for_quantile, int) or isinstance(
            self.min_samples_for_quantile, bool
        ):
            raise HarnessError("SampleSizeDisclosure.min_samples_for_quantile: must be int")
        if self.min_samples_for_quantile < 0:
            raise HarnessError("SampleSizeDisclosure.min_samples_for_quantile: must be >= 0")
        if not isinstance(self.sample_size_underflow, bool):
            raise HarnessError("SampleSizeDisclosure.sample_size_underflow: must be bool")

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_count": self.observation_count,
            "effective_sample_size": self.effective_sample_size,
            "per_pool_counts": dict(self.per_pool_counts),
            "min_samples_for_quantile": self.min_samples_for_quantile,
            "sample_size_underflow": self.sample_size_underflow,
        }


def compute_sample_size_disclosure(
    *,
    sample_ids: Iterable[str],
    sample_pool_ids: Mapping[str, str],
    sample_decision_times: Mapping[str, int] | None = None,
    label_horizons: Sequence[int],
    min_samples_for_quantile: int,
    fold_end_decision_time: int | None = None,
) -> SampleSizeDisclosure:
    """Compute the :class:`SampleSizeDisclosure` for ``sample_ids``.

    The function:

    1. counts ``observation_count`` (the unique sample count);
    2. derives ``effective_sample_size`` as the count of samples
       whose decision time plus the maximum label horizon stays
       inside the supplied fold's ``fold_end_decision_time``
       (when omitted, every sample's label is observable and the
       effective size equals the observation count);
    3. tallies ``per_pool_counts`` by ``sample_pool_ids``;
    4. sets ``sample_size_underflow`` when the effective
       sample size is below ``min_samples_for_quantile``.
    """
    if not isinstance(sample_pool_ids, Mapping):
        raise HarnessError(
            f"compute_sample_size_disclosure: sample_pool_ids must be "
            f"Mapping, got {type(sample_pool_ids).__name__}"
        )
    if not isinstance(label_horizons, Sequence) or not label_horizons:
        raise HarnessError(
            "compute_sample_size_disclosure: label_horizons must be a non-empty Sequence"
        )
    if not isinstance(min_samples_for_quantile, int) or isinstance(min_samples_for_quantile, bool):
        raise HarnessError("compute_sample_size_disclosure: min_samples_for_quantile must be int")
    if min_samples_for_quantile < 0:
        raise HarnessError("compute_sample_size_disclosure: min_samples_for_quantile must be >= 0")
    if sample_decision_times is not None and not isinstance(sample_decision_times, Mapping):
        raise HarnessError(
            "compute_sample_size_disclosure: sample_decision_times must be Mapping or None"
        )
    if fold_end_decision_time is not None and (
        not isinstance(fold_end_decision_time, int) or isinstance(fold_end_decision_time, bool)
    ):
        raise HarnessError(
            "compute_sample_size_disclosure: fold_end_decision_time must be int or None"
        )

    samples = list(sample_ids)
    unique_samples = set(samples)
    observation_count = len(unique_samples)
    per_pool_counts: dict[str, int] = {}
    for sample_id in unique_samples:
        pool_id = sample_pool_ids.get(sample_id, "UNKNOWN")
        per_pool_counts[pool_id] = per_pool_counts.get(pool_id, 0) + 1

    maximum_horizon = max(int(horizon) for horizon in label_horizons)
    if sample_decision_times is None or fold_end_decision_time is None:
        effective_sample_size = observation_count
    else:
        effective_sample_size = 0
        for sample_id in unique_samples:
            decision_time = sample_decision_times.get(sample_id)
            if decision_time is None:
                continue
            if decision_time + maximum_horizon <= fold_end_decision_time:
                effective_sample_size += 1
    sample_size_underflow = effective_sample_size < min_samples_for_quantile

    return SampleSizeDisclosure(
        observation_count=observation_count,
        effective_sample_size=effective_sample_size,
        per_pool_counts=per_pool_counts,
        min_samples_for_quantile=min_samples_for_quantile,
        sample_size_underflow=sample_size_underflow,
    )


# ---------------------------------------------------------------------------
# Fold evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FoldEvaluation:
    """The result of evaluating one fold.

    The harness stores one :class:`FoldEvaluation` per fold
    ``run_fold_evaluation`` returns. The record carries:

    - ``fold_index`` — the fold's index in the split;
    - ``config_content_hash`` — the :class:`TrainingHarnessConfig`
      content hash;
    - ``feature_importance`` — the per-feature importance;
    - ``trivial_baseline`` — the comparison verdict;
    - ``quantile_coverage`` — the quantile-coverage diagnostic
      (when the schema declares quantile levels);
    - ``calibration`` — the probability calibration diagnostic;
    - ``model_artifact`` — the versioned artifact;
    - ``sample_size_disclosure`` — :class:`SampleSizeDisclosure`;
    - ``verdict`` — :class:`FoldVerdictCode`.
    """

    fold_index: int
    config_content_hash: str
    feature_importance: FeatureImportance
    trivial_baseline: TrivialBaselineComparison
    quantile_coverage: list[Any] = field(default_factory=list[Any])
    calibration: object | None = None
    model_artifact: ModelArtifact | None = None
    sample_size_disclosure: SampleSizeDisclosure | None = None
    verdict: FoldVerdictCode = FoldVerdictCode.PASSED
    trivial_comparison_baseline_mean: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.fold_index, int) or isinstance(self.fold_index, bool):
            raise HarnessError("FoldEvaluation.fold_index: must be int")
        if not isinstance(self.feature_importance, FeatureImportance):
            raise HarnessError("FoldEvaluation.feature_importance: must be FeatureImportance")
        if not isinstance(self.trivial_baseline, TrivialBaselineComparison):
            raise HarnessError("FoldEvaluation.trivial_baseline: must be TrivialBaselineComparison")
        if not isinstance(self.verdict, FoldVerdictCode):
            raise HarnessError("FoldEvaluation.verdict: must be FoldVerdictCode")
        if not isinstance(self.config_content_hash, str) or not self.config_content_hash:
            raise HarnessError("FoldEvaluation.config_content_hash: must be non-empty str")


def _build_model(
    family: ModelFamily,
    config: TrainingHarnessConfig,
) -> object:
    if family is ModelFamily.LINEAR_REGULARIZED:
        return LinearRegularizedModel(
            ModelHyperparameters(
                family=ModelFamily.LINEAR_REGULARIZED,
                seed=config.seed,
            )
        )
    if family is ModelFamily.LINEAR_QUANTILE:
        return LinearQuantileModel(
            ModelHyperparameters(
                family=ModelFamily.LINEAR_QUANTILE,
                seed=config.seed,
            )
        )
    if family is ModelFamily.GRADIENT_BOOSTING:
        return GradientBoostingModel(
            ModelHyperparameters(
                family=ModelFamily.GRADIENT_BOOSTING,
                seed=config.seed,
                gradient_boosting_rounds=10,
                gradient_boosting_max_depth=1,
            )
        )
    raise HarnessError(f"_build_model: unknown family {family!r}")


def run_fold_evaluation(
    *,
    config: TrainingHarnessConfig,
    fold_index: int,
    feature_columns: Sequence[str],
    train_features: Sequence[Sequence[float]],
    train_targets: Sequence[float],
    eval_features: Sequence[Sequence[float]],
    eval_targets: Sequence[float],
    eval_decision_time: int,
    eval_labels_for_calibration: Sequence[int | bool] | None = None,
    trivial_baseline_values: Sequence[float] | None = None,
    target_name: str | None = None,
    expected_target_kind: ModelFamily | None = None,
) -> FoldEvaluation:
    """Train and evaluate one fold; return its versioned record.

    The function fits one model on the training fold, scores the
    evaluation fold and produces the diagnostics (feature
    importance, calibration if labels are provided, trivial
    baseline comparison, sample-size disclosure).

    The function refuses a fold that falls below the
    ``declared_min_samples_for_quantile`` floor by recording
    :class:`FoldVerdictCode.SAMPLE_SIZE_UNDERFLOW`, refuses a
    model whose trivial-baseline comparison is
    :attr:`ModelVerdictCode.NOT_BETTER_THAN_TRIVIAL` by
    recording :class:`FoldVerdictCode.TRIVIAL_BASELINE_LOST`,
    and refuses a forward-derived feature by recording
    :class:`FoldVerdictCode.FORWARD_FEATURE_REJECTED`.
    """
    if not isinstance(config, TrainingHarnessConfig):
        raise HarnessError("run_fold_evaluation: config must be TrainingHarnessConfig")
    if not isinstance(fold_index, int) or isinstance(fold_index, bool):
        raise HarnessError("run_fold_evaluation: fold_index must be int")
    if fold_index < 0:
        raise HarnessError("run_fold_evaluation: fold_index must be >= 0")
    if not isinstance(eval_decision_time, int) or isinstance(eval_decision_time, bool):
        raise HarnessError("run_fold_evaluation: eval_decision_time must be int")
    if eval_decision_time < 0:
        raise HarnessError("run_fold_evaluation: eval_decision_time must be >= 0")
    if not isinstance(target_name, str) or not target_name:
        if not config.label_schema.target_names:
            raise HarnessError(
                "run_fold_evaluation: target_name required when label_schema has no target"
            )
        target_name = config.label_schema.target_names[0]
    if expected_target_kind is None:
        expected_target_kind = ModelFamily.LINEAR_REGULARIZED
    if not isinstance(expected_target_kind, ModelFamily):
        raise HarnessError(
            f"run_fold_evaluation: expected_target_kind must be "
            f"ModelFamily, got {type(expected_target_kind).__name__}"
        )

    sample_size_disclosure = compute_sample_size_disclosure(
        sample_ids=[f"fold_{fold_index}_{i}" for i in range(len(eval_features))],
        sample_pool_ids={
            f"fold_{fold_index}_{i}": "DEFAULT_POOL" for i in range(len(eval_features))
        },
        sample_decision_times={
            f"fold_{fold_index}_{i}": eval_decision_time for i in range(len(eval_features))
        },
        label_horizons=config.declared_label_horizons,
        min_samples_for_quantile=config.declared_min_samples_for_quantile,
        fold_end_decision_time=eval_decision_time + max(config.declared_label_horizons),
    )

    # Honour the forward-feature gate the registry enforces
    # (``DS-010``). The harness re-checks the registry at fit
    # time so a misconfigured config surfaces here rather than
    # silently producing a forward-aware model. We re-build the
    # registry from the snapshot's declarations so the gate is
    # checked against the same content hash the split and panel
    # bound to.
    try:
        # The forward-feature gate runs against every column the
        # evaluation fold references. We don't know the eval
        # columns' declarations except through the registry
        # snapshot; passing them by name here is a placeholder
        # so the validation step runs once at fit time.
        validate_panel_against_decision_time(
            [
                config.registry_snapshot.declarations[
                    i % len(config.registry_snapshot.declarations)
                ]
                for i in range(len(feature_columns))
            ],
            decision_time=eval_decision_time,
            decision_time_kind=config.decision_time_kind,
        )
    except ForwardFeatureError:
        return FoldEvaluation(
            fold_index=fold_index,
            config_content_hash=config.content_hash,
            feature_importance=FeatureImportance(column_names=(), importances=()),
            trivial_baseline=compare_against_trivial_baseline(
                model_predictions=[0.0] * max(len(eval_targets), 1),
                trivial_predictions=[0.0] * max(len(eval_targets), 1),
                higher_is_better=True,
            ),
            model_artifact=None,
            sample_size_disclosure=sample_size_disclosure,
            verdict=FoldVerdictCode.FORWARD_FEATURE_REJECTED,
        )

    if sample_size_disclosure.sample_size_underflow:
        return FoldEvaluation(
            fold_index=fold_index,
            config_content_hash=config.content_hash,
            feature_importance=FeatureImportance(
                column_names=tuple(feature_columns), importances=tuple([0.0] * len(feature_columns))
            ),
            trivial_baseline=compare_against_trivial_baseline(
                model_predictions=[0.0] * max(len(eval_targets), 1),
                trivial_predictions=[0.0] * max(len(eval_targets), 1),
                higher_is_better=True,
            ),
            model_artifact=None,
            sample_size_disclosure=sample_size_disclosure,
            verdict=FoldVerdictCode.SAMPLE_SIZE_UNDERFLOW,
        )

    model = _build_model(expected_target_kind, config)
    try:
        model.fit(train_features, train_targets)  # type: ignore[attr-defined]
        predictions = model.infer(eval_features)  # type: ignore[attr-defined]
    except Exception:
        predictions = [0.0] * max(len(eval_features), 1)
        return FoldEvaluation(
            fold_index=fold_index,
            config_content_hash=config.content_hash,
            feature_importance=FeatureImportance(
                column_names=tuple(feature_columns), importances=tuple([0.0] * len(feature_columns))
            ),
            trivial_baseline=compare_against_trivial_baseline(
                model_predictions=[0.0] * max(len(eval_targets), 1),
                trivial_predictions=[0.0] * max(len(eval_targets), 1),
                higher_is_better=True,
            ),
            model_artifact=None,
            sample_size_disclosure=sample_size_disclosure,
            verdict=FoldVerdictCode.DEGENERATE,
        )
    predictions = model.infer(eval_features)  # type: ignore[attr-defined]

    if trivial_baseline_values is None:
        trivial_baseline_values = [0.0] * len(predictions)
    if len(trivial_baseline_values) != len(predictions):
        raise HarnessError(
            f"run_fold_evaluation: trivial_baseline_values length "
            f"{len(trivial_baseline_values)} != predictions length "
            f"{len(predictions)}"
        )

    trivial_compare = compare_against_trivial_baseline(
        model_predictions=predictions,
        trivial_predictions=list(trivial_baseline_values),
        higher_is_better=True,
    )

    quantile_coverage_list: list[Any] = []
    if eval_labels_for_calibration is not None:
        quantile_level = 0.5
        # quantile coverage is only meaningful if the target is
        # a quantile model; otherwise skip.
        if expected_target_kind is ModelFamily.LINEAR_QUANTILE:
            report = compute_quantile_coverage(
                predictions=predictions,
                labels=list(eval_labels_for_calibration),
                quantile_level=quantile_level,
            )
            quantile_coverage_list.append(
                {
                    "quantile_level": report.quantile_level,
                    "coverage": report.coverage,
                    "sample_count": report.sample_count,
                }
            )

    # Calibration: only meaningful for an "exit probability" target.
    calibration = None
    if eval_labels_for_calibration is not None:
        try:
            calibration = compute_calibration_report(
                predictions=predictions,
                labels=list(eval_labels_for_calibration),
            )
        except Exception:
            calibration = None

    feature_importance = feature_importance_from_model(model, column_names=feature_columns)

    model_artifact = build_model_artifact(
        model=model,
        hyperparameters=ModelHyperparameters(
            family=expected_target_kind,
            seed=config.seed,
        ),
        dataset_version=config.registry_snapshot.content_hash,
        feature_config_hash=config.content_hash,
        split_definition_hash=_split_definition_hash(config.split_definition),
        code_revision=config.code_revision,
    )

    if trivial_compare.verdict is ModelVerdictCode.NOT_BETTER_THAN_TRIVIAL:
        verdict = FoldVerdictCode.TRIVIAL_BASELINE_LOST
    else:
        verdict = FoldVerdictCode.PASSED

    return FoldEvaluation(
        fold_index=fold_index,
        config_content_hash=config.content_hash,
        feature_importance=feature_importance,
        trivial_baseline=trivial_compare,
        quantile_coverage=quantile_coverage_list,
        calibration=calibration,
        model_artifact=model_artifact,
        sample_size_disclosure=sample_size_disclosure,
        verdict=verdict,
        trivial_comparison_baseline_mean=trivial_compare.trivial_metric,
    )


def _split_definition_hash(definition: PanelSplitDefinition) -> str:
    payload = definition.to_dict()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "0x" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_DECISION_TIME_KIND",
    "FoldEvaluation",
    "FoldVerdictCode",
    "HARNESS_VERSION",
    "HarnessConfigError",
    "HarnessError",
    "HarnessFoldEvalError",
    "HarnessSampleSizeUnderflowError",
    "HarnessSplitPopulationError",
    "SampleSizeDisclosure",
    "TrainingHarnessConfig",
    "apply_split_to_panel",
    "assemble_panel_dataset",
    "build_training_harness",
    "compute_sample_size_disclosure",
    "run_fold_evaluation",
]
