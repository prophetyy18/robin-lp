"""Experiment manifests and reports (T063).

The ``robinhood_lp.reports`` package owns the per-run record every
published result depends on: the :class:`ExperimentManifest` that
binds a run to its inputs, the :class:`RunIdentity` that ties a
multi-pool run's manifests together, the validation layer that
enforces the T063 acceptance clauses (required fields, checksums,
numeraire / qualification agreement, per-pool invariant, no
overwrite), and the :func:`rerun_manifest` one-command rerun
entry point.

The package is intentionally narrow: it depends on the backtest
layer (engine / events) for the audit chain and the position
state, and on the strategy layer for the reconstructible
baselines; it does not import RPC, storage, configuration,
signing, execution, or presentation code. Its sole consumers are
the ``validate-multi-pool`` and ``rerun-manifest`` CLI subcommands
in :mod:`robinhood_lp.__main__` and the research harness T101.

References:

- T063 — Add experiment manifests and reports.
- ADR-014 §3 — numeraire hierarchy and qualification.
- `docs/spec/research/DATASET_AND_EVALUATION.md` DS-001 / DS-003.
"""

from __future__ import annotations

from robinhood_lp.reports.manifest import (
    MANIFEST_VERSION,
    UNKNOWN_CODE_REVISION,
    UNKNOWN_DEPENDENCY_REVISION,
    VALID_CLOCK_ASSUMPTIONS,
    VALID_COST_ASSUMPTIONS,
    VALID_FILL_ASSUMPTIONS,
    VALID_QUOTE_ASSUMPTIONS,
    VALID_STRATEGY_KINDS,
    VALUATION_QUALIFIED,
    VALUATION_RELATIVE_ONLY,
    ExperimentManifest,
    InvalidManifestFieldError,
    ManifestError,
    ManifestPoolMismatchError,
    SerialisedEvent,
    build_experiment_manifest,
    compute_report_checksum,
    experiment_manifest_from_dict,
    manifest_checksum,
    serialised_event_from_dict,
)
from robinhood_lp.reports.metrics import (
    METRICS_VERSION,
    Q64_SCALE,
    SECONDS_PER_YEAR,
    STAGE_FILL,
    CoverageSummary,
    LedgerSnapshot,
    MetricsError,
    RunMetrics,
    build_coverage_summary,
    compute_run_metrics,
    decisions_checksum,
    extract_decisions,
)
from robinhood_lp.reports.rerun import (
    RERUN_VERSION,
    RerunResult,
    rerun_manifest,
    rerun_manifest_from_object,
)
from robinhood_lp.reports.run_identity import (
    RUN_IDENTITY_VERSION,
    CrossManifestDisagreementError,
    ForeignManifestError,
    RunIdentity,
    RunIdentityError,
    run_identity_from_dict,
    validate_run_identity,
)
from robinhood_lp.reports.validation import (
    VALIDATION_VERSION,
    DatasetQualificationRecord,
    ManifestChecksumError,
    ManifestValidationError,
    MissingRequiredFieldError,
    NumeraireQualificationDisagreementError,
    PriorRunOverwriteError,
    RelativeOnlyUSDPresentationError,
    assert_no_prior_run_at_path,
    assert_presentation_numeraire_safe,
    file_sha256,
    iter_validation_errors,
    load_manifest_from_path,
    validate_manifest,
    validate_multi_pool_run,
    write_manifest_to_path,
)

__all__ = [
    # versions
    "MANIFEST_VERSION",
    "METRICS_VERSION",
    "RERUN_VERSION",
    "RUN_IDENTITY_VERSION",
    "VALIDATION_VERSION",
    # sentinels / vocabularies
    "Q64_SCALE",
    "SECONDS_PER_YEAR",
    "STAGE_FILL",
    "UNKNOWN_CODE_REVISION",
    "UNKNOWN_DEPENDENCY_REVISION",
    "VALUATION_QUALIFIED",
    "VALUATION_RELATIVE_ONLY",
    "VALID_CLOCK_ASSUMPTIONS",
    "VALID_COST_ASSUMPTIONS",
    "VALID_FILL_ASSUMPTIONS",
    "VALID_QUOTE_ASSUMPTIONS",
    "VALID_STRATEGY_KINDS",
    # errors
    "CrossManifestDisagreementError",
    "ForeignManifestError",
    "InvalidManifestFieldError",
    "ManifestChecksumError",
    "ManifestError",
    "ManifestPoolMismatchError",
    "ManifestValidationError",
    "MetricsError",
    "MissingRequiredFieldError",
    "NumeraireQualificationDisagreementError",
    "PriorRunOverwriteError",
    "RelativeOnlyUSDPresentationError",
    "RunIdentityError",
    # dataclasses / value objects
    "CoverageSummary",
    "DatasetQualificationRecord",
    "ExperimentManifest",
    "LedgerSnapshot",
    "RerunResult",
    "RunIdentity",
    "RunMetrics",
    "SerialisedEvent",
    # builders
    "assert_no_prior_run_at_path",
    "assert_presentation_numeraire_safe",
    "build_coverage_summary",
    "build_experiment_manifest",
    "compute_report_checksum",
    "compute_run_metrics",
    "decisions_checksum",
    "experiment_manifest_from_dict",
    "extract_decisions",
    "file_sha256",
    "iter_validation_errors",
    "load_manifest_from_path",
    "manifest_checksum",
    "rerun_manifest",
    "rerun_manifest_from_object",
    "run_identity_from_dict",
    "serialised_event_from_dict",
    "validate_manifest",
    "validate_multi_pool_run",
    "validate_run_identity",
    "write_manifest_to_path",
]

__version__: str = "0.0.0"
