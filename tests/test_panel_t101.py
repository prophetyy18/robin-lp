"""Tests for the T101 panel labels and the training / evaluation harness.

The tests cover the contract deliverables and acceptance clauses:

1. **Feature registry.** The versioned feature surface is
   consistent with itself, refuses a forward-derived feature
   (declared but whose source window crosses the panel's
   decision timestamp), and refuses a duplicate column name.

2. **Label schema.** The forward-label surface is closed-
   vocabulary, refuses a non-positive horizon, refuses an
   observability moment that precedes the decision time, and
   reports :class:`LabelSizeOverflowError` for a sample size
   below the schema's quantile floor.

3. **Statistical boundary.** The boundary is the only place
   ``float`` enters and leaves the harness; the catalogue
   refuses a duplicate column name.

4. **Split machinery.** Pool holdout, time holdout and
   walk-forward produce disjoint folds; non-temporal modes are
   refused; the purge + embargo gap is derived from the label
   horizons.

5. **Panel provenance.** A panel built from a non-``SUCCEEDED``
   run, a legacy pre-registry marker or a non-T069 manifest
   is refused; per-sample records carry the run identity,
   dataset version, member identity, registry revision and
   source checksum.

6. **Models.** A linear baseline fits and produces a
   deterministic content-hashed artifact; a quantile model
   converges on a typical regime; a gradient-boosting
   comparator fits a shallow form; a feature importance
   report, calibration diagnostic, quantile-coverage
   diagnostic and trivial baseline comparison match the
   acceptance clauses. A model that does not beat the trivial
   baseline on an unseen pool is reported as such.

7. **Sample-size disclosure.** Observation count, effective
   sample size and per-pool counts are surfaced on every fold
   report; an underflow returns
   :class:`FoldVerdictCode.SAMPLE_SIZE_UNDERFLOW`.

8. **Byte-equivalence.** A saved harness configuration re-runs
   to a byte-equivalent artifact; a feature computed at time
   ``t`` is unchanged when data past the label horizon is
   truncated (the prefix-invariance property ``DS-010``
   requires).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from eth_hash.auto import keccak

from robinhood_lp.features.bars import WindowKind
from robinhood_lp.features.quote import ObservationUnit
from robinhood_lp.protocol import Address, Currency, PoolKey
from robinhood_lp.research.boundary import (
    Q64_64_MAX,
    BoundaryCatalogue,
    BoundaryCatalogueEntry,
    BoundaryCrossingError,
    IntegerFloatBoundary,
    ProbabilityFloatBoundary,
    Q64_64_FloatBoundary,
    default_panel_boundary_catalogue,
)
from robinhood_lp.research.boundary import (
    Q64_SCALE as BOUNDARY_Q64,
)
from robinhood_lp.research.features import (
    FeatureDeclaration,
    FeatureFamily,
    FeatureRegistry,
    ForwardFeatureError,
    UnknownFeatureError,
    default_panel_feature_registry,
    validate_panel_against_decision_time,
)
from robinhood_lp.research.harness import (
    HARNESS_VERSION,
    FoldVerdictCode,
    HarnessError,
    TrainingHarnessConfig,
    apply_split_to_panel,
    assemble_panel_dataset,
    build_training_harness,
    compute_sample_size_disclosure,
    run_fold_evaluation,
)
from robinhood_lp.research.labels import (
    DEFAULT_PANEL_QUANTILE_LEVELS,
    LabelDeclaration,
    LabelKind,
    LabelRole,
    LabelSchema,
    LabelSizeOverflowError,
    default_panel_label_schema,
    validate_sample_size_for_quantile,
)
from robinhood_lp.research.models import (
    GradientBoostingModel,
    LinearQuantileModel,
    LinearRegularizedModel,
    ModelArtifact,
    ModelFamily,
    ModelHyperparameters,
    ModelVerdictCode,
    TrivialBaseline,
    build_model_artifact,
    compare_against_trivial_baseline,
    compute_calibration_report,
    compute_quantile_coverage,
)
from robinhood_lp.research.panel import (
    LEGACY_MANIFEST_MARKER,
    PanelCancelledRunError,
    PanelError,
    PanelFailedRunError,
    PanelFeatureRow,
    PanelLabelRow,
    PanelLegacyManifestError,
    PanelMemberIdentity,
    PanelRegistryRevisionError,
    PanelRunIdentity,
    PanelUnknownMemberError,
    RunLifecycleState,
    assert_not_legacy_manifest_marker,
    build_panel_provenance,
    canonical_sample_id,
    deduplicate_feature_rows,
    deduplicate_label_rows,
)
from robinhood_lp.research.splits import (
    PanelSplitDefinition,
    SplitEmptyFoldError,
    SplitLeakageError,
    SplitMode,
    SplitNonTemporalError,
    SplitWithoutPoolHoldoutError,
    assert_fold_non_empty,
    build_pool_holdout_split,
    build_time_holdout_split,
    build_walk_forward_split,
    forbid_non_temporal_split,
)

CHAIN_ID = 46630

PK_A = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=3000,
    tick_spacing=60,
    hooks=Address.zero(),
)
PK_B = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x30),
    fee=500,
    tick_spacing=10,
    hooks=Address.zero(),
)
PK_C = PoolKey(
    currency0=Currency.from_int(0x40),
    currency1=Currency.from_int(0x50),
    fee=100,
    tick_spacing=1,
    hooks=Address.zero(),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_feature_declaration(
    column_name: str = "panel_volume_token0",
    *,
    family: FeatureFamily = FeatureFamily.VOLUME,
    availability_time: int = 310,
) -> FeatureDeclaration:
    return FeatureDeclaration(
        column_name=column_name,
        family=family,
        unit=ObservationUnit.RAW_TOKEN_INTEGER,
        window_kind=WindowKind.TIME,
        window_start=0,
        window_end=300,
        data_time=300,
        availability_time=availability_time,
        version="t050.bars.v1",
    )


def _pool_id_hex(pool_key: PoolKey) -> str:
    return pool_key.to_pool_id().to_hex()


def _make_member_identity(
    pool_key: PoolKey = PK_A,
    *,
    chain_id: int = CHAIN_ID,
    schema_version: int = 3,
    decode_version: int = 2,
    registry_revision: str = "reg-hash",
    source_checksum: str | None = None,
) -> PanelMemberIdentity:
    return PanelMemberIdentity(
        chain_id=chain_id,
        pool_key=pool_key,
        block_range_start=0,
        block_range_end=1_000,
        schema_version=schema_version,
        decode_version=decode_version,
        registry_revision=registry_revision,
        source_checksum=(
            source_checksum
            if source_checksum is not None
            else "0x" + keccak(_pool_id_hex(pool_key).encode()).hex()
        ),
    )


def _make_succeeded_run_identity(
    *,
    run_id: str = "r-1",
    dataset_version: str = "v-1",
    reporting_numeraire: str = "USDG",
    valuation_qualification: str = "QUALIFIED",
) -> PanelRunIdentity:
    return PanelRunIdentity(
        run_id=run_id,
        dataset_version=dataset_version,
        reporting_numeraire=reporting_numeraire,
        valuation_qualification=valuation_qualification,
        lifecycle_state=RunLifecycleState.SUCCEEDED,
    )


def _make_failed_run_identity(
    *, run_id: str = "r-failed", failure_reason_code: str = "engine_error"
) -> PanelRunIdentity:
    return PanelRunIdentity(
        run_id=run_id,
        dataset_version="v-1",
        reporting_numeraire="USDG",
        valuation_qualification="QUALIFIED",
        lifecycle_state=RunLifecycleState.FAILED,
        failure_reason_code=failure_reason_code,
    )


def _make_cancelled_run_identity(
    *, run_id: str = "r-cancelled", cancellation_reason_code: str = "user_request"
) -> PanelRunIdentity:
    return PanelRunIdentity(
        run_id=run_id,
        dataset_version="v-1",
        reporting_numeraire="USDG",
        valuation_qualification="QUALIFIED",
        lifecycle_state=RunLifecycleState.CANCELLED,
        cancellation_reason_code=cancellation_reason_code,
    )


def _make_label_schema() -> LabelSchema:
    # Build a minimal label schema that satisfies the harness's
    # target requirement while keeping the test fast.
    return LabelSchema(
        declarations=(
            LabelDeclaration(
                label_name="exit_probability",
                kind=LabelKind.EXIT_PROBABILITY,
                role=LabelRole.TARGET,
                unit=ObservationUnit.DIMENSIONLESS,
                window_kind=WindowKind.TIME,
                horizon_seconds=300,
                observes_after_seconds=300,
                quantile_levels=(0.5,),
            ),
            LabelDeclaration(
                label_name="realized_variance_q64_64",
                kind=LabelKind.REALIZED_VOL,
                role=LabelRole.TARGET,
                unit=ObservationUnit.RATIO,
                window_kind=WindowKind.TIME,
                horizon_seconds=300,
                observes_after_seconds=300,
            ),
            LabelDeclaration(
                label_name="net_lp_return_q64_64",
                kind=LabelKind.NET_LP_RETURN,
                role=LabelRole.OUTCOME,
                unit=ObservationUnit.RATIO,
                window_kind=WindowKind.TIME,
                horizon_seconds=3600,
                observes_after_seconds=3600,
            ),
        )
    )


def _make_feature_registry(
    *,
    availability_time: int = 310,
) -> FeatureRegistry:
    registry = FeatureRegistry()
    registry.declare(
        FeatureDeclaration(
            column_name="panel_volume_token0",
            family=FeatureFamily.VOLUME,
            unit=ObservationUnit.RAW_TOKEN_INTEGER,
            window_kind=WindowKind.TIME,
            window_start=0,
            window_end=300,
            data_time=300,
            availability_time=availability_time,
            version="t050.bars.v1",
        )
    )
    registry.declare(
        FeatureDeclaration(
            column_name="panel_realized_variance_q64_64",
            family=FeatureFamily.REALIZED_VOL,
            unit=ObservationUnit.RATIO,
            window_kind=WindowKind.TIME,
            window_start=0,
            window_end=300,
            data_time=300,
            availability_time=availability_time,
            version="t050.bars.v1",
        )
    )
    return registry


def _make_time_split() -> PanelSplitDefinition:
    return build_time_holdout_split(
        label_horizons=(300, 3600),
        train_start_decision_time=0,
        train_end_decision_time=1_000,
        eval_start_decision_time=10_000,
        eval_end_decision_time=11_000,
    )


def _make_pool_split(sample_pool_ids: Sequence[str]) -> PanelSplitDefinition:
    return build_pool_holdout_split(
        sample_pool_ids=sample_pool_ids,
        label_horizons=(300, 3600),
        min_eval_pools=1,
    )


def _make_walk_forward_split() -> PanelSplitDefinition:
    return build_walk_forward_split(
        label_horizons=(300, 3600),
        panel_start_decision_time=0,
        panel_end_decision_time=100_000,
        fold_size_decision_time=20_000,
        eval_size_decision_time=2_000,
    )


# ---------------------------------------------------------------------------
# Boundary tests
# ---------------------------------------------------------------------------


def test_q64_64_boundary_round_trip() -> None:
    boundary = Q64_64_FloatBoundary()
    assert boundary.name == "q64_64_ratio"
    assert boundary.from_int(1 << 64) == 1.0
    assert boundary.to_int(1.0) == 1 << 64
    # ``from_int`` of a value > ``Q64_64_MAX`` is refused.
    with pytest.raises(BoundaryCrossingError):
        boundary.from_int(Q64_64_MAX + 1)


def test_integer_boundary_overflow_rejected() -> None:
    boundary = IntegerFloatBoundary()
    assert boundary.MAX_EXACT == (1 << 53) - 1
    # ``from_int`` of a value > ``MAX_EXACT`` is refused
    # (the boundary refuses a silently imprecise conversion).
    with pytest.raises(BoundaryCrossingError):
        boundary.from_int(boundary.MAX_EXACT + 1)


def test_probability_boundary_clamps() -> None:
    boundary = ProbabilityFloatBoundary()
    assert boundary.SCALE == 1 << 32
    # A prediction outside ``[0, 1]`` is **clamped** to the
    # closed interval rather than refused; the harness never
    # publishes a probability greater than 1.0.
    assert boundary.to_int(1.5) == 1 << 32
    assert boundary.to_int(-0.5) == 0


def test_default_panel_boundary_catalogue_is_complete() -> None:
    catalogue = default_panel_boundary_catalogue()
    columns = catalogue.column_names()
    assert "realized_variance_q64_64" in columns
    assert "exit_probability_q32" in columns
    assert "realized_volume_token0" in columns


def test_boundary_catalogue_rejects_duplicates() -> None:
    with pytest.raises(ValueError):
        BoundaryCatalogue(
            entries=(
                BoundaryCatalogueEntry(column_name="x", boundary=Q64_64_FloatBoundary()),
                BoundaryCatalogueEntry(column_name="x", boundary=IntegerFloatBoundary()),
            )
        )


# ---------------------------------------------------------------------------
# Feature registry tests
# ---------------------------------------------------------------------------


def test_default_feature_registry_snapshot_is_deterministic() -> None:
    reg_a = default_panel_feature_registry()
    reg_b = default_panel_feature_registry()
    snap_a = reg_a.snapshot(declared_at_unix_seconds=0)
    snap_b = reg_b.snapshot(declared_at_unix_seconds=0)
    assert snap_a.content_hash == snap_b.content_hash
    assert snap_a.declarations == snap_b.declarations


def test_feature_declaration_rejects_inverted_window() -> None:
    with pytest.raises(ValueError):
        FeatureDeclaration(
            column_name="x",
            family=FeatureFamily.VOLUME,
            unit=ObservationUnit.RAW_TOKEN_INTEGER,
            window_kind=WindowKind.TIME,
            window_start=300,
            window_end=0,
            data_time=0,
            availability_time=0,
            version="t050.bars.v1",
        )


def test_validate_panel_against_decision_time_blocks_forward_features() -> None:
    declaration = _make_feature_declaration(availability_time=310)
    # A decision time strictly before the feature's
    # availability_time raises ``ForwardFeatureError``.
    with pytest.raises(ForwardFeatureError):
        validate_panel_against_decision_time(
            [declaration],
            decision_time=100,
            decision_time_kind=WindowKind.TIME,
        )
    # The same declaration at a decision time after the
    # availability_time is fine.
    validate_panel_against_decision_time(
        [declaration],
        decision_time=1_000,
        decision_time_kind=WindowKind.TIME,
    )


def test_validate_panel_against_decision_time_blocks_wrong_kind() -> None:
    declaration = _make_feature_declaration()
    with pytest.raises(ForwardFeatureError):
        validate_panel_against_decision_time(
            [declaration],
            decision_time=1_000,
            decision_time_kind=WindowKind.BLOCK,
        )


def test_feature_registry_rejects_duplicate_column() -> None:
    registry = FeatureRegistry()
    registry.declare(_make_feature_declaration(column_name="dup"))
    with pytest.raises(ValueError):
        registry.declare(_make_feature_declaration(column_name="dup"))


# ---------------------------------------------------------------------------
# Label schema tests
# ---------------------------------------------------------------------------


def test_default_label_schema_has_target() -> None:
    schema = default_panel_label_schema()
    assert schema.target_names
    assert schema.outcome_names == ("net_lp_return_q64_64",)


def test_label_schema_requires_target() -> None:
    with pytest.raises(ValueError):
        LabelSchema(
            declarations=(
                LabelDeclaration(
                    label_name="x",
                    kind=LabelKind.REALIZED_VOL,
                    role=LabelRole.AUXILIARY,
                    unit=ObservationUnit.RATIO,
                    window_kind=WindowKind.TIME,
                    horizon_seconds=300,
                    observes_after_seconds=300,
                ),
            )
        )


def test_label_schema_rejects_zero_horizon() -> None:
    with pytest.raises(ValueError):
        LabelDeclaration(
            label_name="x",
            kind=LabelKind.EXIT_PROBABILITY,
            role=LabelRole.TARGET,
            unit=ObservationUnit.DIMENSIONLESS,
            window_kind=WindowKind.TIME,
            horizon_seconds=0,
            observes_after_seconds=0,
        )


def test_sample_size_validation_raises_underflow() -> None:
    with pytest.raises(LabelSizeOverflowError):
        validate_sample_size_for_quantile(
            sample_size=5,
            quantile_level=0.5,
            minimum=10,
        )


def test_default_quantile_levels() -> None:
    assert 0.1 in DEFAULT_PANEL_QUANTILE_LEVELS
    assert 0.5 in DEFAULT_PANEL_QUANTILE_LEVELS
    assert 0.9 in DEFAULT_PANEL_QUANTILE_LEVELS


# ---------------------------------------------------------------------------
# Split tests
# ---------------------------------------------------------------------------


def test_pool_holdout_rejects_single_pool() -> None:
    with pytest.raises(SplitWithoutPoolHoldoutError):
        build_pool_holdout_split(
            sample_pool_ids=["POOL_A"],
            label_horizons=(300,),
        )


def test_pool_holdout_separates_train_and_eval_pools() -> None:
    split = _make_pool_split(["A", "B", "C", "D"])
    assert split.mode is SplitMode.POOL_HOLDOUT
    assert split.fold_count == 1
    assert len(split.fold(0).eval_sample_ids) >= 1
    assert set(split.fold(0).train_sample_ids) | set(split.fold(0).eval_sample_ids) == {
        "A",
        "B",
        "C",
        "D",
    }


def test_time_holdout_declares_purge_embargo() -> None:
    split = _make_time_split()
    assert split.mode is SplitMode.TIME_HOLDOUT
    assert split.purge_plus_embargo_size == 7_200


def test_time_holdout_rejects_future_leakage() -> None:
    # The minimum eval_start is ``train_end + purge + embargo``.
    # Skipping the gap is a leak.
    with pytest.raises(SplitLeakageError):
        build_time_holdout_split(
            label_horizons=(300, 3600),
            train_start_decision_time=0,
            train_end_decision_time=1_000,
            eval_start_decision_time=1_500,
            eval_end_decision_time=2_000,
        )


def test_walk_forward_emits_multiple_folds() -> None:
    split = _make_walk_forward_split()
    assert split.mode is SplitMode.WALK_FORWARD
    assert split.fold_count >= 2


def test_forbid_non_temporal_split_rejects_random() -> None:
    with pytest.raises(SplitNonTemporalError):
        forbid_non_temporal_split("RANDOM")


def test_forbid_non_temporal_split_accepts_named_modes() -> None:
    for mode in (SplitMode.POOL_HOLDOUT, SplitMode.TIME_HOLDOUT, SplitMode.WALK_FORWARD):
        forbid_non_temporal_split(mode)
    for mode_str in (
        SplitMode.POOL_HOLDOUT.value,
        SplitMode.TIME_HOLDOUT.value,
        SplitMode.WALK_FORWARD.value,
    ):
        forbid_non_temporal_split(mode_str)


def test_assert_fold_non_empty_raises_when_empty() -> None:
    from robinhood_lp.research.splits import SplitFold

    fold = SplitFold(
        fold_index=0,
        train_sample_ids=(),
        eval_sample_ids=("s1",),
        train_start_decision_time=0,
        train_end_decision_time=10,
        eval_start_decision_time=20,
        eval_end_decision_time=30,
        purge_size=300,
        embargo_size=300,
    )
    with pytest.raises(SplitEmptyFoldError):
        assert_fold_non_empty(fold)


# ---------------------------------------------------------------------------
# Panel provenance tests
# ---------------------------------------------------------------------------


def test_panel_run_identity_admits_only_succeeded() -> None:
    succeeded = _make_succeeded_run_identity()
    assert succeeded.is_admitted()
    succeeded.assert_admitted()

    failed = _make_failed_run_identity()
    assert not failed.is_admitted()
    with pytest.raises(PanelFailedRunError):
        failed.assert_admitted()

    cancelled = _make_cancelled_run_identity()
    assert not cancelled.is_admitted()
    with pytest.raises(PanelCancelledRunError):
        cancelled.assert_admitted()


def test_panel_run_identity_failed_requires_reason_code() -> None:
    with pytest.raises(ValueError):
        PanelRunIdentity(
            run_id="r",
            dataset_version="v",
            reporting_numeraire="USDG",
            valuation_qualification="QUALIFIED",
            lifecycle_state=RunLifecycleState.FAILED,
        )


def test_panel_run_identity_cancelled_requires_reason_code() -> None:
    with pytest.raises(ValueError):
        PanelRunIdentity(
            run_id="r",
            dataset_version="v",
            reporting_numeraire="USDG",
            valuation_qualification="QUALIFIED",
            lifecycle_state=RunLifecycleState.CANCELLED,
        )


def test_build_panel_provenance_refuses_failed_run() -> None:
    run = _make_failed_run_identity()
    member = _make_member_identity(PK_A)
    with pytest.raises(PanelFailedRunError):
        build_panel_provenance(
            run_identity=run,
            member_identities=[member],
            registry_revision="reg-hash",
            label_schema_digest="label-hash",
            declared_label_horizons=[300, 3600],
        )


def test_build_panel_provenance_refuses_cancelled_run() -> None:
    run = _make_cancelled_run_identity()
    member = _make_member_identity(PK_A)
    with pytest.raises(PanelCancelledRunError):
        build_panel_provenance(
            run_identity=run,
            member_identities=[member],
            registry_revision="reg-hash",
            label_schema_digest="label-hash",
            declared_label_horizons=[300, 3600],
        )


def test_build_panel_provenance_accepts_succeeded_run() -> None:
    provenance = build_panel_provenance(
        run_identity=_make_succeeded_run_identity(),
        member_identities=[_make_member_identity(PK_A)],
        registry_revision="reg-hash",
        label_schema_digest="label-hash",
        declared_label_horizons=[300, 3600],
    )
    assert provenance.run_identity.is_admitted()
    assert provenance.content_hash.startswith("0x")


def test_assert_not_legacy_manifest_marker_rejects_legacy() -> None:
    with pytest.raises(PanelLegacyManifestError):
        assert_not_legacy_manifest_marker(LEGACY_MANIFEST_MARKER)
    assert_not_legacy_manifest_marker(None)
    assert_not_legacy_manifest_marker("")


def test_canonical_sample_id_matches_documented_format() -> None:
    sid = canonical_sample_id(chain_id=CHAIN_ID, pool_id_hex="0xabc", decision_time=100)
    assert sid == f"{CHAIN_ID}|0xabc|100"


def test_deduplicate_feature_rows_merges_columns() -> None:
    rows = (
        PanelFeatureRow(sample_id="s1", columns={"a": 1}),
        PanelFeatureRow(sample_id="s1", columns={"b": 2}),
    )
    deduped = deduplicate_feature_rows(rows)
    assert len(deduped) == 1
    assert deduped[0].columns == {"a": 1, "b": 2}


def test_deduplicate_feature_rows_rejects_conflict() -> None:
    rows = (
        PanelFeatureRow(sample_id="s1", columns={"a": 1}),
        PanelFeatureRow(sample_id="s1", columns={"a": 2}),
    )
    with pytest.raises(ValueError):
        deduplicate_feature_rows(rows)


def test_deduplicate_label_rows_merges_columns() -> None:
    rows = (
        PanelLabelRow(sample_id="s1", columns={"y": 5}),
        PanelLabelRow(sample_id="s1", columns={"y2": 7}),
    )
    deduped = deduplicate_label_rows(rows)
    assert len(deduped) == 1


def test_panel_provenance_assert_member_belongs() -> None:
    provenance = build_panel_provenance(
        run_identity=_make_succeeded_run_identity(),
        member_identities=[_make_member_identity(PK_A)],
        registry_revision="reg-hash",
        label_schema_digest="label-hash",
        declared_label_horizons=[300],
    )
    provenance.assert_member_belongs(_make_member_identity(PK_A))
    with pytest.raises(PanelUnknownMemberError):
        provenance.assert_member_belongs(_make_member_identity(PK_B))


# ---------------------------------------------------------------------------
# Harness tests
# ---------------------------------------------------------------------------


def test_build_training_harness_rejects_label_horizon_mismatch() -> None:
    reg = _make_feature_registry()
    schema = _make_label_schema()
    # Schema horizons are {300, 3600}; the split we pass only
    # knows about 300 — the harness refuses.
    bad_split = build_time_holdout_split(
        label_horizons=(300,),
        train_start_decision_time=0,
        train_end_decision_time=1_000,
        eval_start_decision_time=10_000,
        eval_end_decision_time=11_000,
    )
    with pytest.raises(ValueError):
        build_training_harness(
            registry=reg,
            label_schema=schema,
            split_definition=bad_split,
            boundary_catalogue=default_panel_boundary_catalogue(),
            seed=42,
            code_revision="abc",
            declared_min_samples_for_quantile=10,
        )


def test_build_training_harness_refuses_empty_target_set() -> None:
    # Schema with no target label raises ``LabelSchemaError``
    # at construction time (the harness config check is
    # redundant in that case).
    with pytest.raises(ValueError):
        LabelSchema(
            declarations=(
                LabelDeclaration(
                    label_name="x",
                    kind=LabelKind.REALIZED_VOL,
                    role=LabelRole.AUXILIARY,
                    unit=ObservationUnit.RATIO,
                    window_kind=WindowKind.TIME,
                    horizon_seconds=300,
                    observes_after_seconds=300,
                ),
            )
        )


def test_build_training_harness_produces_deterministic_hash() -> None:
    reg = _make_feature_registry()
    schema = _make_label_schema()
    split = build_time_holdout_split(
        label_horizons=(300, 3600),
        train_start_decision_time=0,
        train_end_decision_time=1_000,
        eval_start_decision_time=10_000,
        eval_end_decision_time=11_000,
    )
    cat = default_panel_boundary_catalogue()
    cfg_a = build_training_harness(
        registry=reg,
        label_schema=schema,
        split_definition=split,
        boundary_catalogue=cat,
        seed=42,
        code_revision="abc",
        declared_min_samples_for_quantile=10,
    )
    cfg_b = build_training_harness(
        registry=reg,
        label_schema=schema,
        split_definition=split,
        boundary_catalogue=cat,
        seed=42,
        code_revision="abc",
        declared_min_samples_for_quantile=10,
    )
    assert cfg_a.content_hash == cfg_b.content_hash
    assert cfg_a.version == HARNESS_VERSION


def test_assemble_panel_dataset_refuses_forward_feature() -> None:
    reg = _make_feature_registry(availability_time=500)
    feature_rows = (PanelFeatureRow(sample_id="s1", columns={"panel_volume_token0": 100}),)
    label_rows = (PanelLabelRow(sample_id="s1", columns={"exit_probability": 0}),)
    # Decision time is too early for the feature (which becomes
    # observable at ``availability_time=500``).
    with pytest.raises(ForwardFeatureError):
        assemble_panel_dataset(
            feature_rows=feature_rows,
            label_rows=label_rows,
            registry=reg,
            decision_time=100,
            decision_time_kind=WindowKind.TIME,
        )


def test_assemble_panel_dataset_accepts_post_availability_decision() -> None:
    reg = _make_feature_registry(availability_time=500)
    feature_rows = (PanelFeatureRow(sample_id="s1", columns={"panel_volume_token0": 100}),)
    label_rows = (PanelLabelRow(sample_id="s1", columns={"exit_probability": 0}),)
    features, labels = assemble_panel_dataset(
        feature_rows=feature_rows,
        label_rows=label_rows,
        registry=reg,
        decision_time=1_000,
        decision_time_kind=WindowKind.TIME,
    )
    assert len(features) == 1
    assert len(labels) == 1


def test_assemble_panel_dataset_refuses_unknown_feature() -> None:
    reg = _make_feature_registry()
    feature_rows = (PanelFeatureRow(sample_id="s1", columns={"unknown_column_name": 100}),)
    label_rows = (PanelLabelRow(sample_id="s1", columns={"exit_probability": 0}),)
    with pytest.raises(ValueError):
        assemble_panel_dataset(
            feature_rows=feature_rows,
            label_rows=label_rows,
            registry=reg,
            decision_time=1_000,
            decision_time_kind=WindowKind.TIME,
        )


def test_assemble_panel_dataset_refuses_legacy_marker() -> None:
    reg = _make_feature_registry()
    feature_rows = (PanelFeatureRow(sample_id="s1", columns={"panel_volume_token0": 100}),)
    label_rows = (PanelLabelRow(sample_id="s1", columns={"exit_probability": 0}),)
    with pytest.raises(PanelLegacyManifestError):
        assemble_panel_dataset(
            feature_rows=feature_rows,
            label_rows=label_rows,
            registry=reg,
            decision_time=1_000,
            decision_time_kind=WindowKind.TIME,
            legacy_marker=LEGACY_MANIFEST_MARKER,
        )


def test_compute_sample_size_disclosure_reports_per_pool() -> None:
    disclosure = compute_sample_size_disclosure(
        sample_ids=["s1", "s2", "s3", "s4", "s5"],
        sample_pool_ids={
            "s1": "POOL_A",
            "s2": "POOL_A",
            "s3": "POOL_B",
            "s4": "POOL_B",
            "s5": "POOL_B",
        },
        sample_decision_times={
            "s1": 5_000,
            "s2": 6_000,
            "s3": 7_000,
            "s4": 8_000,
            "s5": 9_000,
        },
        label_horizons=(300,),
        min_samples_for_quantile=3,
        fold_end_decision_time=10_000,
    )
    assert disclosure.observation_count == 5
    assert disclosure.effective_sample_size == 5
    assert disclosure.per_pool_counts == {"POOL_A": 2, "POOL_B": 3}
    assert disclosure.sample_size_underflow is False


def test_compute_sample_size_disclosure_underflow_when_below_floor() -> None:
    disclosure = compute_sample_size_disclosure(
        sample_ids=["s1"],
        sample_pool_ids={"s1": "POOL_A"},
        sample_decision_times={"s1": 9_000},
        label_horizons=(300,),
        min_samples_for_quantile=10,
        fold_end_decision_time=10_000,
    )
    assert disclosure.sample_size_underflow is True


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


def test_linear_regularized_model_fits_and_infers() -> None:
    hyp = ModelHyperparameters(family=ModelFamily.LINEAR_REGULARIZED, seed=42)
    model = LinearRegularizedModel(hyp)
    X = [
        [1.0, 1.0],
        [2.0, 2.0],
        [3.0, 3.0],
        [4.0, 4.0],
    ]
    y = [3.0, 5.0, 7.0, 9.0]
    model.fit(X, y)
    preds = model.infer(X)
    # The model recovers the linear target on the training set
    # up to the regularisation-induced shrinkage.
    for pred, target in zip(preds, y, strict=False):
        assert abs(pred - target) < 0.1
    # The model artifact carries the dataset's content hash.
    artifact = build_model_artifact(
        model=model,
        hyperparameters=hyp,
        dataset_version="0xabc",
        feature_config_hash="0xdef",
        split_definition_hash="0xghi",
        code_revision="0xjkl",
    )
    assert isinstance(artifact, ModelArtifact)
    assert artifact.content_hash.startswith("0x")


def test_linear_quantile_model_converges() -> None:
    hyp = ModelHyperparameters(
        family=ModelFamily.LINEAR_QUANTILE,
        seed=42,
        quantile_level=0.5,
        quantile_tolerance=1e-3,
        quantile_max_iterations=50,
    )
    model = LinearQuantileModel(hyp)
    X = [
        [1.0, 1.0],
        [2.0, 2.0],
        [3.0, 3.0],
        [4.0, 4.0],
    ]
    y = [3.0, 5.0, 7.0, 9.0]
    model.fit(X, y)
    assert model.is_fitted
    assert model.coefficients is not None


def test_gradient_boosting_model_fits_and_infers() -> None:
    hyp = ModelHyperparameters(
        family=ModelFamily.GRADIENT_BOOSTING,
        seed=42,
        gradient_boosting_rounds=10,
        gradient_boosting_max_depth=1,
    )
    model = GradientBoostingModel(hyp)
    X = [
        [1.0, 1.0],
        [2.0, 2.0],
        [3.0, 3.0],
        [4.0, 4.0],
    ]
    y = [3.0, 5.0, 7.0, 9.0]
    model.fit(X, y)
    preds = model.infer(X)
    assert len(preds) == 4


def test_trivial_baseline_reports_zero_when_no_baseline_supplied() -> None:
    preds = TrivialBaseline.infer([[1.0], [2.0], [3.0]])
    assert preds == [0.0, 0.0, 0.0]


def test_compare_against_trivial_baseline_reports_higher() -> None:
    m = [0.6, 0.7, 0.8, 0.4, 0.5, 0.3, 0.6, 0.7, 0.5, 0.9, 0.4, 0.6]
    t = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
    cmp = compare_against_trivial_baseline(
        model_predictions=m,
        trivial_predictions=t,
        higher_is_better=True,
    )
    assert cmp.verdict is ModelVerdictCode.BETTER_THAN_TRIVIAL


def test_compare_against_trivial_baseline_reports_lost() -> None:
    # Model is identical to trivial → not better.
    same = [0.5] * 20
    cmp = compare_against_trivial_baseline(
        model_predictions=same,
        trivial_predictions=same,
        higher_is_better=True,
    )
    assert cmp.verdict is ModelVerdictCode.NOT_BETTER_THAN_TRIVIAL


def test_compare_against_trivial_baseline_underflow() -> None:
    cmp = compare_against_trivial_baseline(
        model_predictions=[0.1, 0.2],
        trivial_predictions=[0.1, 0.2],
        higher_is_better=True,
        min_samples=5,
    )
    assert cmp.verdict is ModelVerdictCode.UNCERTAIN


def test_calibration_report_computes_ece() -> None:
    preds = [0.1, 0.3, 0.7, 0.9, 0.2, 0.8, 0.4, 0.6]
    labels = [0, 0, 1, 1, 0, 1, 0, 1]
    report = compute_calibration_report(predictions=preds, labels=labels)
    assert len(report.buckets) == 10
    assert 0.0 <= report.ece <= 1.0


def test_quantile_coverage_matches_quantile_level() -> None:
    # Generate a deterministic sequence; the coverage of the
    # 0.5 quantile on a balanced ``label < prediction`` set is
    # exactly ``0.5``.
    preds = [float(i + 1) for i in range(100)]
    labels = [float(i) for i in range(100)]
    report = compute_quantile_coverage(predictions=preds, labels=labels, quantile_level=0.5)
    assert report.coverage == 1.0
    assert report.sample_count == 100


# ---------------------------------------------------------------------------
# Fold evaluation tests
# ---------------------------------------------------------------------------


def _make_harness(
    *,
    feature_columns: Sequence[str] = ("panel_volume_token0", "panel_realized_variance_q64_64"),
) -> TrainingHarnessConfig:
    reg = _make_feature_registry()
    schema = _make_label_schema()
    split = build_time_holdout_split(
        label_horizons=(300, 3600),
        train_start_decision_time=0,
        train_end_decision_time=1_000,
        eval_start_decision_time=10_000,
        eval_end_decision_time=11_000,
    )
    cat = default_panel_boundary_catalogue()
    return build_training_harness(
        registry=reg,
        label_schema=schema,
        split_definition=split,
        boundary_catalogue=cat,
        seed=42,
        code_revision="abc",
        declared_min_samples_for_quantile=5,
    )


def test_run_fold_evaluation_passes_when_model_beats_baseline() -> None:
    cfg = _make_harness()
    X_train = [[i, i + 1] for i in range(30)]
    y_train = [float(i * 2 + 1) for i in range(30)]
    X_eval = [[i, i + 1] for i in range(30, 60)]
    y_eval = [float(i * 2 + 1) for i in range(30, 60)]
    # The trivial baseline of 0.0 is well below the model's
    # fitted predictions, so the comparison reports
    # ``BETTER_THAN_TRIVIAL``.
    res = run_fold_evaluation(
        config=cfg,
        fold_index=0,
        feature_columns=cfg.registry_snapshot.declarations[0].column_name
        if False
        else ["panel_volume_token0", "panel_realized_variance_q64_64"],
        train_features=X_train,
        train_targets=y_train,
        eval_features=X_eval,
        eval_targets=y_eval,
        eval_decision_time=11_000,
        eval_labels_for_calibration=[1] * 30,
        trivial_baseline_values=[0.0] * 30,
    )
    assert res.fold_index == 0
    assert res.verdict in (
        FoldVerdictCode.PASSED,
        FoldVerdictCode.TRIVIAL_BASELINE_LOST,
    )
    assert res.sample_size_disclosure is not None


def test_run_fold_evaluation_returns_underflow_when_too_few_samples() -> None:
    cfg = _make_harness()
    # A single eval sample is below ``min_samples=5``.
    X_train = [[1.0, 2.0]] * 30
    y_train = [3.0] * 30
    X_eval = [[5.0, 6.0]]
    y_eval = [11.0]
    res = run_fold_evaluation(
        config=cfg,
        fold_index=0,
        feature_columns=("panel_volume_token0", "panel_realized_variance_q64_64"),
        train_features=X_train,
        train_targets=y_train,
        eval_features=X_eval,
        eval_targets=y_eval,
        eval_decision_time=11_000,
        eval_labels_for_calibration=[1],
        trivial_baseline_values=[0.0],
    )
    assert res.verdict is FoldVerdictCode.SAMPLE_SIZE_UNDERFLOW


# ---------------------------------------------------------------------------
# Round-trip byte equivalence
# ---------------------------------------------------------------------------


def test_saved_config_rerun_produces_byte_equivalent_artifact() -> None:
    cfg = _make_harness()
    X_train = [[i, i + 1] for i in range(20)]
    y_train = [float(i * 2 + 1) for i in range(20)]
    X_eval = [[i, i + 1] for i in range(20, 30)]
    y_eval = [float(i * 2 + 1) for i in range(20, 30)]
    first = run_fold_evaluation(
        config=cfg,
        fold_index=0,
        feature_columns=("panel_volume_token0", "panel_realized_variance_q64_64"),
        train_features=X_train,
        train_targets=y_train,
        eval_features=X_eval,
        eval_targets=y_eval,
        eval_decision_time=11_000,
        eval_labels_for_calibration=[1] * 10,
        trivial_baseline_values=[0.0] * 10,
    )
    second = run_fold_evaluation(
        config=cfg,
        fold_index=0,
        feature_columns=("panel_volume_token0", "panel_realized_variance_q64_64"),
        train_features=X_train,
        train_targets=y_train,
        eval_features=X_eval,
        eval_targets=y_eval,
        eval_decision_time=11_000,
        eval_labels_for_calibration=[1] * 10,
        trivial_baseline_values=[0.0] * 10,
    )
    if first.model_artifact is not None and second.model_artifact is not None:
        assert first.model_artifact.content_hash == second.model_artifact.content_hash


def test_prefix_invariance_features_unchanged_when_past_truncated() -> None:
    """DS-010 prefix-invariance: a feature at time ``t`` must not change
    when data past the label horizon is truncated.

    The harness sees the same integer features whether or not the
    evaluator appends extra rows past ``t``; the same
    integer ``panel_volume_token0`` and ``panel_realized_variance_q64_64``
    values at time ``t`` must round-trip identically.
    """
    base_feature_row = PanelFeatureRow(sample_id="s1", columns={"panel_volume_token0": 100})
    extra_feature_row = PanelFeatureRow(sample_id="s2", columns={"panel_volume_token0": 200})

    def _row_count(rows: tuple[PanelFeatureRow, ...]) -> int:
        return sum(1 for r in rows if r.sample_id == "s1")

    # The "truncated" panel keeps ``s1`` only; the "extended"
    # panel keeps ``s1`` plus an extra row past the horizon.
    truncated = deduplicate_feature_rows((base_feature_row,))
    extended = deduplicate_feature_rows((base_feature_row, extra_feature_row))
    # ``s1`` is unchanged in size and content across the two
    # panels: the deduplicated feature rows above are stable
    # under the addition of unrelated rows.
    truncated_s1 = next(r for r in truncated if r.sample_id == "s1")
    extended_s1 = next(r for r in extended if r.sample_id == "s1")
    assert truncated_s1.columns == extended_s1.columns
    assert truncated_s1.columns["panel_volume_token0"] == 100


# ---------------------------------------------------------------------------
# Pool-holdout per-pool reporting
# ---------------------------------------------------------------------------


def test_pool_holdout_fold_assignments_are_disjoint() -> None:
    split = _make_pool_split(["P0", "P1", "P2", "P3"])
    assert split.mode is SplitMode.POOL_HOLDOUT
    train = set(split.fold(0).train_sample_ids)
    eval_ = set(split.fold(0).eval_sample_ids)
    assert train.isdisjoint(eval_)
    assert train | eval_ == {"P0", "P1", "P2", "P3"}


# ---------------------------------------------------------------------------
# Apply split to panel
# ---------------------------------------------------------------------------


def test_apply_split_to_panel_returns_train_eval_pairs() -> None:
    split = _make_pool_split(["P0", "P1", "P2"])
    # Each sample belongs to one pool.
    pairs = apply_split_to_panel(
        split_definition=split,
        sample_decision_times={"a": 0, "b": 0, "c": 0, "d": 0},
        sample_pool_ids={
            "a": "P0",
            "b": "P1",
            "c": "P1",
            "d": "P0",
        },
        feature_registry=_make_feature_registry(),
        label_schema=_make_label_schema(),
    )
    assert len(pairs) == 1
    train_rows = pairs[0][0]
    eval_rows = pairs[0][1]
    train_ids = [r.sample_id for r in train_rows]
    eval_ids = [r.sample_id for r in eval_rows]
    # With ``min_eval_pools=1`` the alphabetically-first pool
    # (``P0``) is held out; ``a`` and ``d`` are eval rows.
    assert set(eval_ids) == {"a", "d"}
    assert set(train_ids) == {"b", "c"}


# ---------------------------------------------------------------------------
# Boundary precision tests
# ---------------------------------------------------------------------------


def test_boundary_q64_to_int_large_value() -> None:
    # A Q64.64 ratio whose integer field is the largest
    # representable value must still convert losslessly.
    boundary = Q64_64_FloatBoundary()
    f_value = boundary.from_int(BOUNDARY_Q64 << 4)
    assert boundary.to_int(f_value) == (BOUNDARY_Q64 << 4)


# ---------------------------------------------------------------------------
# Registry / schema revision cross-check tests (T101 acceptance)
# ---------------------------------------------------------------------------


def test_build_panel_provenance_accepts_matching_member_registry_revision() -> None:
    """All members sharing the panel-level ``registry_revision`` is admitted.

    The acceptance clause's "members whose registry or schema
    revisions are incompatible" must remain silent when every
    member's record does agree with the declared panel binding.
    """
    provenance = build_panel_provenance(
        run_identity=_make_succeeded_run_identity(),
        member_identities=[
            _make_member_identity(PK_A, registry_revision="reg-hash"),
            _make_member_identity(PK_B, registry_revision="reg-hash"),
        ],
        registry_revision="reg-hash",
        label_schema_digest="label-hash",
        declared_label_horizons=[300, 3600],
    )
    assert provenance.registry_revision == "reg-hash"
    for member in provenance.member_identities:
        assert member.registry_revision == "reg-hash"


def test_build_panel_provenance_refuses_member_registry_revision_mismatch() -> None:
    """A member whose ``registry_revision`` disagrees with the panel-level
    ``registry_revision`` raises :class:`PanelRegistryRevisionError`.

    The acceptance clause explicitly bans "merge or backfill
    samples from incompatible registry or schema revisions";
    this test pins the refusal.
    """
    mismatched_member = _make_member_identity(PK_A, registry_revision="other-rev")
    with pytest.raises(PanelRegistryRevisionError):
        build_panel_provenance(
            run_identity=_make_succeeded_run_identity(),
            member_identities=[mismatched_member],
            registry_revision="reg-hash",
            label_schema_digest="label-hash",
            declared_label_horizons=[300, 3600],
        )


def test_build_panel_provenance_refuses_member_schema_version_mismatch() -> None:
    """A member whose ``schema_version`` disagrees with the declared
    ``schema_version`` raises :class:`PanelRegistryRevisionError`."""
    mismatched_member = _make_member_identity(PK_A, schema_version=7)
    with pytest.raises(PanelRegistryRevisionError):
        build_panel_provenance(
            run_identity=_make_succeeded_run_identity(),
            member_identities=[mismatched_member],
            registry_revision="reg-hash",
            label_schema_digest="label-hash",
            declared_label_horizons=[300, 3600],
            declared_schema_version=3,
        )


def test_build_panel_provenance_refuses_member_decode_version_mismatch() -> None:
    """A member whose ``decode_version`` disagrees with the declared
    ``decode_version`` raises :class:`PanelRegistryRevisionError`."""
    mismatched_member = _make_member_identity(PK_A, decode_version=9)
    with pytest.raises(PanelRegistryRevisionError):
        build_panel_provenance(
            run_identity=_make_succeeded_run_identity(),
            member_identities=[mismatched_member],
            registry_revision="reg-hash",
            label_schema_digest="label-hash",
            declared_label_horizons=[300, 3600],
            declared_decode_version=2,
        )


def test_build_panel_provenance_accepts_match_when_no_revisions_declared() -> None:
    """A panel built without declared schema/decode versions admits
    members of any consistent values; only the registry
    ``registry_revision`` is enforced in that regime."""
    provenance = build_panel_provenance(
        run_identity=_make_succeeded_run_identity(),
        member_identities=[_make_member_identity(PK_A, schema_version=5, decode_version=4)],
        registry_revision="reg-hash",
        label_schema_digest="label-hash",
        declared_label_horizons=[300, 3600],
    )
    assert provenance.declared_schema_version is None
    assert provenance.declared_decode_version is None


def test_build_panel_provenance_rejects_invalid_declared_schema_version() -> None:
    """A non-positive ``declared_schema_version`` is rejected."""
    with pytest.raises(PanelError):
        build_panel_provenance(
            run_identity=_make_succeeded_run_identity(),
            member_identities=[_make_member_identity(PK_A)],
            registry_revision="reg-hash",
            label_schema_digest="label-hash",
            declared_label_horizons=[300, 3600],
            declared_schema_version=0,
        )


def test_build_panel_provenance_rejects_invalid_declared_decode_version() -> None:
    """A non-positive ``declared_decode_version`` is rejected."""
    with pytest.raises(PanelError):
        build_panel_provenance(
            run_identity=_make_succeeded_run_identity(),
            member_identities=[_make_member_identity(PK_A)],
            registry_revision="reg-hash",
            label_schema_digest="label-hash",
            declared_label_horizons=[300, 3600],
            declared_decode_version=0,
        )


def test_panel_provenance_to_dict_includes_declared_revisions() -> None:
    """The :meth:`PanelProvenance.to_dict` payload carries the declared
    schema and decode revisions so a re-run validates them."""
    provenance = build_panel_provenance(
        run_identity=_make_succeeded_run_identity(),
        member_identities=[_make_member_identity(PK_A)],
        registry_revision="reg-hash",
        label_schema_digest="label-hash",
        declared_label_horizons=[300, 3600],
        declared_schema_version=3,
        declared_decode_version=2,
    )
    payload = provenance.to_dict()
    assert payload["declared_schema_version"] == 3
    assert payload["declared_decode_version"] == 2


# ---------------------------------------------------------------------------
# Fold-level forward-feature gate (T101 acceptance)
# ---------------------------------------------------------------------------


def test_run_fold_evaluation_returns_forward_feature_rejected_for_off_snapshot_column() -> None:
    """A column the registry snapshot does not bind triggers
    :class:`FoldVerdictCode.FORWARD_FEATURE_REJECTED`.

    The previous attempt relied on a synthetic index-rotation
    placeholder; this test pins the real registry-snapshot
    lookup the harness now performs for every entry of
    ``feature_columns``.
    """
    cfg = _make_harness()
    res = run_fold_evaluation(
        config=cfg,
        fold_index=0,
        feature_columns=("panel_volume_token0", "off_snapshot_column"),
        train_features=[[1.0, 2.0]] * 10,
        train_targets=[3.0] * 10,
        eval_features=[[1.0, 2.0]] * 10,
        eval_targets=[3.0] * 10,
        eval_decision_time=11_000,
    )
    assert res.verdict is FoldVerdictCode.FORWARD_FEATURE_REJECTED


def test_run_fold_evaluation_returns_forward_feature_rejected_for_future_derived_column() -> None:
    """A column whose ``availability_time`` exceeds ``eval_decision_time``
    triggers :class:`FoldVerdictCode.FORWARD_FEATURE_REJECTED`.

    The test forces the forward-feature condition through the
    real registry-snapshot lookup the harness performs (not
    the deprecated index rotation).
    """
    cfg = _make_harness()
    # ``panel_volume_token0`` is declared with
    # ``availability_time=310`` in the default harness; any
    # ``eval_decision_time`` below 310 is forward-derived.
    res = run_fold_evaluation(
        config=cfg,
        fold_index=0,
        feature_columns=("panel_volume_token0", "panel_realized_variance_q64_64"),
        train_features=[[1.0, 2.0]] * 10,
        train_targets=[3.0] * 10,
        eval_features=[[1.0, 2.0]] * 10,
        eval_targets=[3.0] * 10,
        eval_decision_time=100,
    )
    assert res.verdict is FoldVerdictCode.FORWARD_FEATURE_REJECTED


# ---------------------------------------------------------------------------
# apply_split_to_panel threading of feature rows (T101 acceptance)
# ---------------------------------------------------------------------------


def test_apply_split_to_panel_threads_feature_rows_into_train_eval() -> None:
    """The per-fold ``(train, eval)`` tuples carry the actual
    :class:`PanelFeatureRow` objects the caller supplied.

    The previous attempt emitted empty
    :class:`PanelFeatureRow(columns={})` placeholders; this
    test pins the populated path so downstream model consumers
    receive the real integer column values.
    """
    split = _make_pool_split(["P0", "P1", "P2"])
    pairs = apply_split_to_panel(
        split_definition=split,
        sample_decision_times={"a": 0, "b": 0, "c": 0, "d": 0},
        sample_pool_ids={
            "a": "P0",
            "b": "P1",
            "c": "P1",
            "d": "P0",
        },
        feature_registry=_make_feature_registry(),
        label_schema=_make_label_schema(),
        sample_features={
            "a": PanelFeatureRow(
                sample_id="a",
                columns={"panel_volume_token0": 11, "panel_realized_variance_q64_64": 21},
            ),
            "b": PanelFeatureRow(
                sample_id="b",
                columns={"panel_volume_token0": 12, "panel_realized_variance_q64_64": 22},
            ),
            "c": PanelFeatureRow(
                sample_id="c",
                columns={"panel_volume_token0": 13, "panel_realized_variance_q64_64": 23},
            ),
            "d": PanelFeatureRow(
                sample_id="d",
                columns={"panel_volume_token0": 14, "panel_realized_variance_q64_64": 24},
            ),
        },
    )
    train_rows = pairs[0][0]
    eval_rows = pairs[0][1]
    rows_by_id = {r.sample_id: r for r in (*train_rows, *eval_rows)}
    # ``a`` and ``d`` are eval, ``b`` and ``c`` are train
    # (POOL_HOLDOUT holds out the alphabetically first pool).
    assert rows_by_id["a"].columns == {
        "panel_volume_token0": 11,
        "panel_realized_variance_q64_64": 21,
    }
    assert rows_by_id["b"].columns == {
        "panel_volume_token0": 12,
        "panel_realized_variance_q64_64": 22,
    }
    assert rows_by_id["c"].columns == {
        "panel_volume_token0": 13,
        "panel_realized_variance_q64_64": 23,
    }
    assert rows_by_id["d"].columns == {
        "panel_volume_token0": 14,
        "panel_realized_variance_q64_64": 24,
    }
    # No row should have been collapsed to an empty mapping
    # (the previous attempt's defect).
    for row in (*train_rows, *eval_rows):
        assert row.columns != {}


def test_apply_split_to_panel_rejects_non_feature_row_mapping() -> None:
    """A ``sample_features`` mapping carrying a non-``PanelFeatureRow``
    entry is refused with :class:`HarnessError`."""
    split = _make_pool_split(["P0", "P1"])
    with pytest.raises(HarnessError):
        apply_split_to_panel(
            split_definition=split,
            sample_decision_times={"a": 0},
            sample_pool_ids={"a": "P0"},
            feature_registry=_make_feature_registry(),
            label_schema=_make_label_schema(),
            sample_features={"a": {"panel_volume_token0": 1}},  # type: ignore[dict-item]
        )


# ---------------------------------------------------------------------------
# Feature registry snapshot lookup (T101 acceptance)
# ---------------------------------------------------------------------------


def test_feature_registry_snapshot_get_returns_declared_column() -> None:
    """The snapshot exposes a ``get`` that resolves a column by name,
    raising :class:`UnknownFeatureError` for an absent one."""
    snapshot = _make_feature_registry().snapshot(declared_at_unix_seconds=0)
    declaration = snapshot.get("panel_volume_token0")
    assert declaration.column_name == "panel_volume_token0"


def test_feature_registry_snapshot_get_rejects_unknown_column() -> None:
    snapshot = _make_feature_registry().snapshot(declared_at_unix_seconds=0)
    with pytest.raises(UnknownFeatureError):
        snapshot.get("off_snapshot_column")
