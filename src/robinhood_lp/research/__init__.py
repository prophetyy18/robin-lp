"""Research dataset registry, panel harness, and model laboratory (T100, T101).

P10 splits into five modules:

- :mod:`robinhood_lp.research.dataset` — the T100 dataset
  registry, numeraire qualification, and content-hashing
  primitives. Layer assignment: ``storage`` (per
  ``tools/check_imports/layer_map.py``).

- :mod:`robinhood_lp.research.boundary` — the T101
  statistical / floating-point boundary catalog (DS-012).
  Layer assignment: ``backtest`` (the boundary is *named* by
  the harness, not by the protocol layer).

- :mod:`robinhood_lp.research.features` — the T101
  point-in-time feature registry (DS-010). Layer assignment:
  ``backtest``.

- :mod:`robinhood_lp.research.labels` — the T101
  forward-label schema (DS-011). Layer assignment:
  ``backtest``.

- :mod:`robinhood_lp.research.splits` — the T101 deterministic
  dataset splits (DS-020, DS-021, DS-022). Layer assignment:
  ``backtest``.

- :mod:`robinhood_lp.research.panel` — the T101 panel
  provenance and per-sample row record. Layer assignment:
  ``backtest``.

- :mod:`robinhood_lp.research.models` — the T101 model
  families (linear baseline, linear quantile,
  gradient-boosting comparator) and diagnostics
  (DS-030..DS-034, DS-040, DS-043). Layer assignment:
  ``strategy``.

- :mod:`robinhood_lp.research.harness` — the T101 training /
  evaluation harness: it wires the registry, label schema,
  split, boundary, panel and model modules together. Layer
  assignment: ``backtest``.

A research dataset is the unit of research input the backtest
harness, the model layer and the research console all
reference. It is the object that separates the research
universe from the execution scope (ADR-014):

- a research dataset may contain many ``PoolKey`` values;
- a research dataset imposes no token approval: research
  neither holds nor trades;
- a research dataset must never grant, feed or weaken the
  single-active-pool execution path;
- a research dataset must never acquire execution authority,
  appear as an approved pool, or become the live default.

References:

- ``docs/spec/research/DATASET_AND_EVALUATION.md``
- ``docs/spec/strategy/LP_METRICS.md``
- ADR-014 (research universe, numeraire hierarchy, model in
  replaceable interface)
- ADR-006 (dependency direction)
- ADR-004 (integer / decimal precision; the model layer is the
  single named float boundary).
"""

from __future__ import annotations

from robinhood_lp.research.boundary import (
    Q64_64_MAX,
    BoundaryCatalogue,
    BoundaryCatalogueEntry,
    BoundaryCrossingError,
    BoundaryError,
    IntegerFloatBoundary,
    ProbabilityFloatBoundary,
    Q64_64_FloatBoundary,
    StatisticalBoundary,
    default_panel_boundary_catalogue,
)
from robinhood_lp.research.boundary import (
    Q64_SCALE as BOUNDARY_Q64_SCALE,
)

# Also re-export the T100 dataset surface (T100 sits in storage
# and is the unit of research input the rest of P10 binds to).
from robinhood_lp.research.dataset import (  # noqa: E402
    DATASET_SCHEMA_VERSION,
    DEFAULT_DATASET_HASH_ALGORITHM,
    RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN,
    RELATIVE_ONLY_REJECTED_REASONS,
    SUPPORT_LEVEL_BACKTEST_OR_ABOVE,
    DatasetAcceptanceVerdict,
    DatasetAcceptanceVerdictCode,
    DatasetAlreadyExistsError,
    DatasetBlockRange,
    DatasetCandidate,
    DatasetContentHasher,
    DatasetDeclaration,
    DatasetError,
    DatasetId,
    DatasetMember,
    DatasetMemberNotFoundError,
    DatasetRegistry,
    DatasetRegistryQuery,
    DatasetRegistrySnapshot,
    DatasetRevision,
    DatasetVersion,
    EmptyDatasetError,
    InactiveResearchDatasetError,
    InvalidSupportLevelError,
    InvertedBlockRangeError,
    NumeraireProvenance,
    NumeraireQualification,
    PoolNotResearchMemberError,
    QualificationBundle,
    RawBlockRange,
    UnknownNumeraireRouteError,
    build_dataset_acceptance_verdict,
    default_content_hasher,
    hash_dataset_declaration,
    validate_numeraire_route,
)
from robinhood_lp.research.dataset import (
    USD_DENOMINATED_FORBIDDEN_FIELDS as RESEARCH_USD_DENOMINATED_FORBIDDEN_FIELDS,
)
from robinhood_lp.research.dataset import (
    ConfidenceLevel as DatasetConfidenceLevel,
)
from robinhood_lp.research.dataset import (
    NumeraireLevel as DatasetNumeraireLevel,
)
from robinhood_lp.research.dataset import (
    assert_no_usd_fields as assert_dataset_no_usd_fields,
)
from robinhood_lp.research.features import (
    DuplicateFeatureColumnError,
    FeatureDeclaration,
    FeatureFamily,
    FeatureRegistry,
    FeatureRegistryError,
    FeatureRegistrySnapshot,
    ForwardFeatureError,
    InvalidAvailabilityError,
    InvalidWindowKindError,
    UnknownFeatureError,
    default_panel_feature_registry,
    validate_panel_against_decision_time,
)
from robinhood_lp.research.harness import (
    DEFAULT_DECISION_TIME_KIND,
    HARNESS_VERSION,
    FoldEvaluation,
    FoldVerdictCode,
    HarnessConfigError,
    HarnessError,
    HarnessFoldEvalError,
    HarnessSampleSizeUnderflowError,
    HarnessSplitPopulationError,
    SampleSizeDisclosure,
    TrainingHarnessConfig,
    apply_split_to_panel,
    assemble_panel_dataset,
    build_training_harness,
    compute_sample_size_disclosure,
    run_fold_evaluation,
)
from robinhood_lp.research.labels import (
    DEFAULT_LABEL_HORIZON_MIN,
    DEFAULT_LABEL_UNIT,
    DEFAULT_LABEL_WINDOW_KIND,
    DEFAULT_PANEL_QUANTILE_LEVELS,
    DEFAULT_QUANTILE_MIN_SAMPLE_SIZE,
    InvalidLabelHorizonError,
    InvalidLabelObservabilityError,
    LabelDeclaration,
    LabelKind,
    LabelRole,
    LabelRoleMismatchError,
    LabelSchema,
    LabelSchemaError,
    LabelSizeOverflowError,
    UnknownLabelError,
    default_panel_label_schema,
    validate_sample_size_for_quantile,
)
from robinhood_lp.research.models import (
    DEFAULT_GRADIENT_BOOSTING_LEARNING_RATE,
    DEFAULT_GRADIENT_BOOSTING_MAX_DEPTH,
    DEFAULT_GRADIENT_BOOSTING_ROUNDS,
    DEFAULT_LINEAR_REGULARIZATION,
    DEFAULT_QUANTILE_MAX_ITERATIONS,
    DEFAULT_QUANTILE_TOLERANCE,
    DEFAULT_TRIVIAL_BASELINE_MIN_SAMPLES,
    MODEL_VERSION,
    FeatureImportance,
    GradientBoostingModel,
    LinearQuantileModel,
    LinearRegularizedModel,
    ModelArtifact,
    ModelError,
    ModelFamily,
    ModelHyperparameters,
    ModelNotFittedError,
    ModelProbabilityCalibrationError,
    ModelSeedError,
    ModelShapeError,
    ModelTrivialBaselineError,
    ModelVerdictCode,
    QuantileCoverageReport,
    TrivialBaseline,
    TrivialBaselineComparison,
    build_model_artifact,
    compare_against_trivial_baseline,
    compute_calibration_report,
    compute_quantile_coverage,
    feature_importance_from_model,
)
from robinhood_lp.research.panel import (
    LEGACY_MANIFEST_MARKER,
    PanelCancelledRunError,
    PanelDatasetVersionError,
    PanelDuplicateSampleError,
    PanelError,
    PanelFailedRunError,
    PanelFeatureRow,
    PanelLabelRow,
    PanelLegacyManifestError,
    PanelMemberIdentity,
    PanelMemberIdentityError,
    PanelProvenance,
    PanelRegistryRevisionError,
    PanelRunIdentity,
    PanelRunIdentityError,
    PanelSampleProvenance,
    PanelUnknownMemberError,
    RunLifecycleState,
    assert_not_legacy_manifest_marker,
    build_panel_provenance,
    canonical_sample_id,
    compute_member_identity,
    deduplicate_feature_rows,
    deduplicate_label_rows,
    is_legacy_pre_registry_marker,
)
from robinhood_lp.research.splits import (
    PanelSplitDefinition,
    SplitEmptyFoldError,
    SplitError,
    SplitFold,
    SplitLeakageError,
    SplitMode,
    SplitNonTemporalError,
    SplitUnknownFoldError,
    SplitWithoutPoolHoldoutError,
    SplitWithoutTimeHoldoutError,
    assert_fold_non_empty,
    build_pool_holdout_split,
    build_time_holdout_split,
    build_walk_forward_split,
    forbid_non_temporal_split,
)

__version__: str = "0.0.0"

__all__ = [
    # T101 boundary
    "BOUNDARY_Q64_SCALE",
    "BoundaryCatalogue",
    "BoundaryCatalogueEntry",
    "BoundaryCrossingError",
    "BoundaryError",
    "IntegerFloatBoundary",
    "ProbabilityFloatBoundary",
    "Q64_64_FloatBoundary",
    "Q64_64_MAX",
    "StatisticalBoundary",
    "default_panel_boundary_catalogue",
    # T101 features
    "DuplicateFeatureColumnError",
    "FeatureDeclaration",
    "FeatureFamily",
    "FeatureRegistry",
    "FeatureRegistryError",
    "FeatureRegistrySnapshot",
    "ForwardFeatureError",
    "InvalidAvailabilityError",
    "InvalidWindowKindError",
    "UnknownFeatureError",
    "default_panel_feature_registry",
    "validate_panel_against_decision_time",
    # T101 labels
    "DEFAULT_LABEL_HORIZON_MIN",
    "DEFAULT_LABEL_UNIT",
    "DEFAULT_LABEL_WINDOW_KIND",
    "DEFAULT_PANEL_QUANTILE_LEVELS",
    "DEFAULT_QUANTILE_MIN_SAMPLE_SIZE",
    "InvalidLabelHorizonError",
    "InvalidLabelObservabilityError",
    "LabelDeclaration",
    "LabelKind",
    "LabelRole",
    "LabelRoleMismatchError",
    "LabelSchema",
    "LabelSchemaError",
    "LabelSizeOverflowError",
    "UnknownLabelError",
    "default_panel_label_schema",
    "validate_sample_size_for_quantile",
    # T101 splits
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
    # T101 panel
    "LEGACY_MANIFEST_MARKER",
    "PanelCancelledRunError",
    "PanelDatasetVersionError",
    "PanelDuplicateSampleError",
    "PanelError",
    "PanelFailedRunError",
    "PanelFeatureRow",
    "PanelLabelRow",
    "PanelLegacyManifestError",
    "PanelMemberIdentity",
    "PanelMemberIdentityError",
    "PanelProvenance",
    "PanelRegistryRevisionError",
    "PanelRunIdentity",
    "PanelRunIdentityError",
    "PanelSampleProvenance",
    "PanelUnknownMemberError",
    "RunLifecycleState",
    "assert_not_legacy_manifest_marker",
    "build_panel_provenance",
    "canonical_sample_id",
    "compute_member_identity",
    "deduplicate_feature_rows",
    "deduplicate_label_rows",
    "is_legacy_pre_registry_marker",
    # T101 models
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
    # T101 harness
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
    # T100 dataset
    "DATASET_SCHEMA_VERSION",
    "DEFAULT_DATASET_HASH_ALGORITHM",
    "RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN",
    "RELATIVE_ONLY_REJECTED_REASONS",
    "SUPPORT_LEVEL_BACKTEST_OR_ABOVE",
    "RESEARCH_USD_DENOMINATED_FORBIDDEN_FIELDS",
    "DatasetConfidenceLevel",
    "DatasetAcceptanceVerdict",
    "DatasetAcceptanceVerdictCode",
    "DatasetAlreadyExistsError",
    "DatasetBlockRange",
    "DatasetCandidate",
    "DatasetContentHasher",
    "DatasetDeclaration",
    "DatasetError",
    "DatasetId",
    "DatasetMember",
    "DatasetMemberNotFoundError",
    "DatasetNumeraireLevel",
    "DatasetRegistry",
    "DatasetRegistryQuery",
    "DatasetRegistrySnapshot",
    "DatasetRevision",
    "DatasetVersion",
    "EmptyDatasetError",
    "InactiveResearchDatasetError",
    "InvalidSupportLevelError",
    "InvertedBlockRangeError",
    "NumeraireProvenance",
    "NumeraireQualification",
    "PoolNotResearchMemberError",
    "QualificationBundle",
    "RawBlockRange",
    "UnknownNumeraireRouteError",
    "assert_dataset_no_usd_fields",
    "build_dataset_acceptance_verdict",
    "default_content_hasher",
    "hash_dataset_declaration",
    "validate_numeraire_route",
]
