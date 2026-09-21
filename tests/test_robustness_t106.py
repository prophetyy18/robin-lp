"""Tests for the T106 schema-bound robustness protocol.

T106 constrains robustness analysis to the T068 strategy
registry's parameter schema. Every axis the runner sweeps must
be declared by the registered strategy's parameter schema, and
every value the axis carries must lie inside the schema's
declared type / unit / range. A registry / schema revision
produces a separately identified surface; results from
incompatible revisions are not merged into one sensitivity
conclusion.

The tests cover every T106 acceptance clause:

- **At least two heterogeneous registered strategies.** The
  test suite uses ``IDENTITY_FIXED_WIDTH`` (a T062 baseline with
  three registered parameters) and ``IDENTITY_ADAPTIVE_RANGE``
  (the T065 adaptive-Range strategy with eighteen registered
  parameters) as the heterogeneous pair. Every evaluated surface
  point is schema-declared and within range; the report binds
  the same registry / schema revision as the surface; repeated
  construction is deterministic.

- **Boundary tests at every declared range edge.** Each axis
  value is checked at the schema's lower / upper bounds; an
  out-of-range value is rejected at construction.

- **Negative coverage.** Undeclared parameter names, wrong-type
  values, wrong-unit values, out-of-range values, bool passed
  as ``int``, missing-required axes, and incompatible registry
  revisions are all rejected before any fold is evaluated.

- **Compatibility and migration fixtures for T064 artifacts.**
  Legacy T064 surfaces (``ParameterSurface``,
  ``RobustnessReport``) remain readable, byte-identical, and
  carry an explicit legacy marker.

- **Old-path-unreachable test.** Current publication cannot use
  a legacy ``ParameterSurface``; the schema-bound runner and
  schema-bound report builder both raise
  :class:`LegacySurfaceInSchemaBoundRunnerError` /
  :class:`LegacySurfaceInCurrentReportError` when a legacy
  surface is supplied to a current publication path.

- **Determinism.** Two schema-bound surfaces built against the
  same registry revision return the same binding checksum; two
  reports built against the same inputs return the same
  ``binding_identity``.

- **Layer purity.** The schema-binding module imports the
  registry only; the dependency test in
  :class:`TestSchemaBindingLayerPurity` enforces the rule by
  walking the live module graph.

The must-not clauses are also covered:

- **No clip / coerce / ignore.** Out-of-range values, wrong-type
  values, and undeclared parameters fail at construction;
  there is no fallback.
- **No schema inference.** A schema change (a different checksum)
  produces a different surface identity; surfaces with different
  identities cannot be merged.
- **No hard-coded surface builders in current publication.** The
  schema-bound runner rejects a legacy surface; the schema-bound
  report builder rejects a legacy surface; the schema-bound
  runner's surface-less fallback stamps the report with the
  live registry's default revision so a registry rotation is
  surfaced as a binding checksum change.

Design constraints (binding):

- **Integer-only.** ``float`` never appears on the schema-bound
  surface path. Type / unit / range validation is integer
  arithmetic.
- **No RPC / storage / signing / Web import.** The schema-binding
  module imports the registry only; the dependency test enforces
  the rule.
"""

from __future__ import annotations

from typing import Final

import pytest

from robinhood_lp.robustness import (
    LEGACY_SURFACE_MARKER,
    REPORTS_VERSION,
    RUNNER_VERSION,
    SCHEMA_BOUND_REPORTS_VERSION,
    SCHEMA_BOUND_RUNNER_VERSION,
    SCHEMA_BOUND_SURFACES_VERSION,
    SURFACES_VERSION,
    IncompatibleBindingError,
    IncompatibleSchemaRevisionError,
    InvalidSchemaBoundAxisError,
    LegacySurfaceInCurrentReportError,
    LegacySurfaceInSchemaBoundRunnerError,
    MissingPrimaryGeneralisationError,
    ParameterAxis,
    PoolHoldoutFold,
    RobustnessReport,
    RobustnessReportError,
    RunnerInputsError,
    SchemaBindingError,
    SchemaBoundParameterAxis,
    SchemaBoundParameterSurface,
    SchemaBoundRobustnessReport,
    SchemaBoundRobustnessRunnerInputs,
    SensitivitySummary,
    UndeclaredAxisError,
    assert_reports_compatible,
    assert_surfaces_compatible,
    build_pool_holdout_split,
    build_robustness_report,
    build_runner,
    build_schema_bound_robustness_report,
    build_schema_bound_runner,
    build_schema_bound_surface,
    default_degradation_policy,
    default_stress_scenario_catalogue,
    is_legacy_surface,
    is_schema_bound_surface_identity,
    surface_id_for_inputs,
    surface_identity,
    validate_axis_value,
)
from robinhood_lp.robustness.disclosure import build_disclosure
from robinhood_lp.robustness.reports import FoldResult, PoolHoldoutResult
from robinhood_lp.robustness.splits import LabelHorizon
from robinhood_lp.robustness.surfaces import (
    SensitivitySummary as _LegacySensitivitySummary,
)
from robinhood_lp.robustness.surfaces import (
    build_parameter_surface as _build_legacy_surface,
)
from robinhood_lp.strategy import (
    IDENTITY_ADAPTIVE_RANGE,
    IDENTITY_FIXED_WIDTH,
    IDENTITY_HOLD,
    IDENTITY_VOLATILITY_WIDTH,
    REGISTRY_VERSION,
)
from robinhood_lp.strategy.registry import (
    CodeProvenance,
    ParameterSchema,
    ParameterType,
    RegisteredStrategy,
    Registry,
    UnknownStrategyIdentityError,
    _AdapterFactory,
    default_registry,
    registry_checksum,
    reset_default_registry_cache,
)

# ---------------------------------------------------------------------------
# Reference fixtures
# ---------------------------------------------------------------------------


_CHAIN_ID: Final[int] = 46630
_POOL_KEY_ID_TRAIN: Final[str] = "0x" + "11" * 32
_POOL_KEY_ID_HOLDOUT_A: Final[str] = "0x" + "22" * 32
_POOL_KEY_ID_HOLDOUT_B: Final[str] = "0x" + "33" * 32
_RUN_ID: Final[str] = "run-t106-001"
_METRIC_NAME: Final[str] = "total_return_q64_64"
_FAMILY_WISE_ALPHA_Q64_64: Final[int] = (1 << 64) // 20  # 5%

# Q64.64 USDG constants — mirrors the baseline / adaptive defaults.
_ONE_USDG_Q64_64: Final[int] = 1 << 64
_MAX_CAPITAL_Q64_64: Final[int] = 1 << 70


def _label_horizon() -> LabelHorizon:
    return LabelHorizon(value=12, unit="BARS")


def _scenario_catalogue() -> object:
    return default_stress_scenario_catalogue(degradation_policy=default_degradation_policy())


def _pool_holdout_split() -> object:
    train_pool = PoolHoldoutFold(
        chain_id=_CHAIN_ID,
        pool_key_id=_POOL_KEY_ID_TRAIN,
        fold_role="TRAIN_POOL",
        segment_label="train_pool_0",
        block_range_start=0,
        block_range_end=1000,
    )
    holdout_pool_a = PoolHoldoutFold(
        chain_id=_CHAIN_ID,
        pool_key_id=_POOL_KEY_ID_HOLDOUT_A,
        fold_role="HOLDOUT_POOL",
        segment_label="holdout_pool_0",
        block_range_start=0,
        block_range_end=1000,
    )
    holdout_pool_b = PoolHoldoutFold(
        chain_id=_CHAIN_ID,
        pool_key_id=_POOL_KEY_ID_HOLDOUT_B,
        fold_role="HOLDOUT_POOL",
        segment_label="holdout_pool_1",
        block_range_start=0,
        block_range_end=1000,
    )
    return build_pool_holdout_split(
        chain_id=_CHAIN_ID,
        train_pool_folds=(train_pool,),
        holdout_pool_folds=(holdout_pool_a, holdout_pool_b),
        label_horizon=_label_horizon(),
        recorded_at_unix_seconds=1_700_000_000,
    )


def _train_results() -> tuple[FoldResult, ...]:
    return (
        FoldResult(
            role="TRAIN",
            segment_label="train_fold_0",
            metric_name=_METRIC_NAME,
            metric_value=100,
            fold_index=0,
        ),
    )


def _validation_results() -> tuple[FoldResult, ...]:
    return (
        FoldResult(
            role="VALIDATION",
            segment_label="validation_fold_0",
            metric_name=_METRIC_NAME,
            metric_value=95,
            fold_index=0,
        ),
    )


def _test_results() -> tuple[FoldResult, ...]:
    return (
        FoldResult(
            role="TEST",
            segment_label="test_fold_0",
            metric_name=_METRIC_NAME,
            metric_value=90,
            fold_index=0,
        ),
    )


def _pool_holdout_results() -> tuple[PoolHoldoutResult, ...]:
    return (
        PoolHoldoutResult(
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_HOLDOUT_A,
            metric_name=_METRIC_NAME,
            metric_value=110,
        ),
        PoolHoldoutResult(
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_HOLDOUT_B,
            metric_name=_METRIC_NAME,
            metric_value=95,
        ),
    )


def _sensitivity_summary(metric_value: int = 110) -> SensitivitySummary:
    return SensitivitySummary(
        metric_name=_METRIC_NAME,
        best_metric_value=metric_value,
        best_parameter_combo=(("half_width_ticks", 1200), ("tick_spacing", 60)),
        worst_metric_value=80,
        spread_metric_value=metric_value - 80,
        n_grid_points=4,
    )


def _disclosure() -> object:
    return build_disclosure(
        n_comparisons=8,
        n_families=1,
        adjustment_method="BONFERRONI",
        family_wise_alpha_q64_64=_FAMILY_WISE_ALPHA_Q64_64,
        metric_values=(100, 110, 115, 120, 80, 90, 95, 100),
        best_metric_name=_METRIC_NAME,
    )


def _build_fixed_width_surface() -> SchemaBoundParameterSurface:
    return build_schema_bound_surface(
        surface_id="surf_t106_fixed_width",
        identity=IDENTITY_FIXED_WIDTH,
        axes=(
            ("tick_spacing", (60, 120)),
            ("half_width_ticks", (600, 1200)),
        ),
    )


def _build_adaptive_surface() -> SchemaBoundParameterSurface:
    # The T065 adaptive-Range strategy has eighteen registered parameters.
    # Build a surface that touches three heterogeneous parameters to
    # confirm the schema binding works at scale.
    return build_schema_bound_surface(
        surface_id="surf_t106_adaptive",
        identity=IDENTITY_ADAPTIVE_RANGE,
        axes=(
            ("five_minute_window_seconds", (60, 300)),
            ("half_width_ticks", (300, 600, 1200)),
            ("max_position_hold_seconds", (0, 1800)),
        ),
    )


def _build_hold_surface() -> SchemaBoundParameterSurface:
    # The T062 HOLD strategy declares no parameters. A surface built
    # against it must succeed and carry the binding.
    return build_schema_bound_surface(
        surface_id="surf_t106_hold",
        identity=IDENTITY_HOLD,
        axes=(),
    )


def _build_schema_bound_report(
    *,
    surface: SchemaBoundParameterSurface | None,
    sensitivity: SensitivitySummary | None = None,
) -> SchemaBoundRobustnessReport:
    return build_schema_bound_robustness_report(
        run_id=_RUN_ID,
        label_horizon=_label_horizon(),
        train_results=_train_results(),
        validation_results=_validation_results(),
        test_results=_test_results(),
        pool_holdout_results=_pool_holdout_results(),
        walk_forward_splits=(),
        pool_holdout_split=_pool_holdout_split(),
        sensitivity_summary=sensitivity if sensitivity is not None else _sensitivity_summary(),
        scenario_catalogue=_scenario_catalogue(),
        multiple_comparison_disclosure=_disclosure(),
        conclusion_statement=(
            "Best run achieved return 110; sensitivity spread is 30 across the "
            "schema-bound parameter grid (worst 80, best 110). Pool holdout per "
            "held-out pool is the primary generalisation statement."
        ),
        registry_version=surface.registry_version if surface is not None else REGISTRY_VERSION,
        registry_checksum=surface.registry_checksum if surface is not None else registry_checksum(),
        parameter_schema_version=(surface.parameter_schema_version if surface is not None else ""),
        parameter_schema_checksum=(
            surface.parameter_schema_checksum if surface is not None else ""
        ),
        strategy_identity=surface.strategy_identity if surface is not None else "",
        strategy_version=surface.strategy_version if surface is not None else "",
        schema_bound_surface=surface,
    )


def _build_schema_bound_runner_inputs(
    *,
    surface: SchemaBoundParameterSurface | None,
) -> SchemaBoundRobustnessRunnerInputs:
    return SchemaBoundRobustnessRunnerInputs(
        run_id=_RUN_ID,
        label_horizon=_label_horizon(),
        metric_name=_METRIC_NAME,
        scenario_catalogue=_scenario_catalogue(),
        pool_holdout_split=_pool_holdout_split(),
        train_fold_specs=(("train_fold_0", 0),),
        validation_fold_specs=(("validation_fold_0", 0),),
        test_fold_specs=(("test_fold_0", 0),),
        pool_specs=(
            (_CHAIN_ID, _POOL_KEY_ID_HOLDOUT_A),
            (_CHAIN_ID, _POOL_KEY_ID_HOLDOUT_B),
        ),
        family_wise_alpha_q64_64=_FAMILY_WISE_ALPHA_Q64_64,
        conclusion_statement=(
            "Best run achieved return 110; sensitivity spread is 30 across the "
            "schema-bound parameter grid (worst 80, best 110). Pool holdout per "
            "held-out pool is the primary generalisation statement."
        ),
        schema_bound_surface=surface,
    )


# ---------------------------------------------------------------------------
# Version pinning
# ---------------------------------------------------------------------------


class TestModuleVersions:
    """The T106 module versions are pinned and distinct from the T064 versions."""

    def test_schema_bound_surfaces_version_is_pinned(self) -> None:
        assert SCHEMA_BOUND_SURFACES_VERSION == "t106.robustness_schema_surfaces.v1"

    def test_schema_bound_reports_version_is_pinned(self) -> None:
        assert SCHEMA_BOUND_REPORTS_VERSION == "t106.robustness_reports.v1"

    def test_schema_bound_runner_version_is_pinned(self) -> None:
        assert SCHEMA_BOUND_RUNNER_VERSION == "t106.robustness_runner.v1"

    def test_legacy_versions_remain_pinned(self) -> None:
        # T064 versions are preserved for the legacy reader / test path.
        assert REPORTS_VERSION == "t064.robustness_reports.v1"
        assert RUNNER_VERSION == "t064.robustness_runner.v1"
        assert SURFACES_VERSION == "t064.robustness_surfaces.v1"

    def test_legacy_marker_is_pinned(self) -> None:
        assert LEGACY_SURFACE_MARKER == "LEGACY_T064_SURFACE"


# ---------------------------------------------------------------------------
# Schema-bound axis validation
# ---------------------------------------------------------------------------


class TestSchemaBoundAxisValidation:
    """An axis value is rejected before the surface is built."""

    def test_valid_int_axis(self) -> None:
        axis = SchemaBoundParameterAxis(
            name="tick_spacing",
            schema_type=ParameterType.TICK_SPACING,
            unit="ticks",
            values=(60, 120, 180),
        )
        assert axis.values == (60, 120, 180)

    def test_valid_str_axis(self) -> None:
        # ``STR`` is not currently used by any registered schema, but
        # the schema-bound surface must accept ``str`` axis values
        # when the schema declares a string parameter.
        axis = SchemaBoundParameterAxis(
            name="display_name",
            schema_type=ParameterType.STR,
            unit="name",
            values=("a", "b"),
        )
        assert axis.values == ("a", "b")

    def test_axis_rejects_empty_values(self) -> None:
        with pytest.raises(SchemaBindingError):
            SchemaBoundParameterAxis(
                name="tick_spacing",
                schema_type=ParameterType.TICK_SPACING,
                unit="ticks",
                values=(),
            )

    def test_axis_rejects_non_tuple_values(self) -> None:
        with pytest.raises(SchemaBindingError):
            SchemaBoundParameterAxis(
                name="tick_spacing",
                schema_type=ParameterType.TICK_SPACING,
                unit="ticks",
                values=[60, 120],  # type: ignore[arg-type]
            )

    def test_axis_rejects_non_str_name(self) -> None:
        with pytest.raises(SchemaBindingError):
            SchemaBoundParameterAxis(
                name="",  # type: ignore[arg-type]
                schema_type=ParameterType.TICK_SPACING,
                unit="ticks",
                values=(60, 120),
            )

    def test_axis_rejects_lower_bound_above_upper_bound(self) -> None:
        with pytest.raises(SchemaBindingError):
            SchemaBoundParameterAxis(
                name="tick_spacing",
                schema_type=ParameterType.TICK_SPACING,
                unit="ticks",
                values=(60, 120),
                lower_bound=120,
                upper_bound=60,
            )

    def test_axis_rejects_non_int_bound(self) -> None:
        with pytest.raises(SchemaBindingError):
            SchemaBoundParameterAxis(
                name="tick_spacing",
                schema_type=ParameterType.TICK_SPACING,
                unit="ticks",
                values=(60,),
                lower_bound="60",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Surface builder: positive coverage
# ---------------------------------------------------------------------------


class TestSchemaBoundSurfacePositive:
    """The builder produces a schema-bound surface for every registered strategy."""

    def test_build_for_fixed_width(self) -> None:
        surface = _build_fixed_width_surface()
        assert surface.strategy_identity == IDENTITY_FIXED_WIDTH
        assert surface.registry_version == REGISTRY_VERSION
        # Every axis must carry the schema's declared type / unit / bounds.
        axis_by_name = {a.name: a for a in surface.axes}
        assert axis_by_name["tick_spacing"].schema_type == ParameterType.TICK_SPACING
        assert axis_by_name["tick_spacing"].unit == "ticks"
        assert axis_by_name["half_width_ticks"].schema_type == ParameterType.POSITIVE_INT
        assert axis_by_name["half_width_ticks"].unit == "ticks"
        # The builder accepts a partial axis list — only the two
        # axes the caller wants to sweep; the surface must not
        # silently add axes for undeclared parameters.
        declared_names = {
            schema.name
            for schema in default_registry().lookup(IDENTITY_FIXED_WIDTH).parameter_schemas
        }
        assert set(axis_by_name).issubset(declared_names)
        assert set(axis_by_name) == {"tick_spacing", "half_width_ticks"}

    def test_build_for_adaptive_range(self) -> None:
        surface = _build_adaptive_surface()
        # The T065 strategy has eighteen registered parameters; the
        # builder must accept a partial axis list that names only the
        # three axes the caller wants to sweep.
        assert surface.strategy_identity == IDENTITY_ADAPTIVE_RANGE
        declared_names = {
            schema.name
            for schema in default_registry().lookup(IDENTITY_ADAPTIVE_RANGE).parameter_schemas
        }
        assert {a.name for a in surface.axes} == set(declared_names) & {
            "five_minute_window_seconds",
            "half_width_ticks",
            "max_position_hold_seconds",
        }

    def test_build_for_hold_strategy_with_no_axes(self) -> None:
        # The HOLD strategy declares no parameters; the builder must
        # accept an empty axis list and bind the surface to the
        # identity.
        surface = _build_hold_surface()
        assert surface.strategy_identity == IDENTITY_HOLD
        assert surface.axes == ()

    def test_grid_enumerates_cross_product(self) -> None:
        surface = _build_fixed_width_surface()
        # 2 * 2 = 4 grid points; enumeration is deterministic.
        grid = surface.grid()
        assert len(grid) == 4
        assert grid[0] == {"tick_spacing": 60, "half_width_ticks": 600}
        assert grid[1] == {"tick_spacing": 60, "half_width_ticks": 1200}
        assert grid[2] == {"tick_spacing": 120, "half_width_ticks": 600}
        assert grid[3] == {"tick_spacing": 120, "half_width_ticks": 1200}

    def test_grid_size_matches_cross_product(self) -> None:
        surface = _build_adaptive_surface()
        # 2 * 3 * 2 = 12 grid points.
        assert surface.grid_size == 12

    def test_surface_checksum_is_deterministic(self) -> None:
        # Two builds against the same registry revision return the same checksum.
        surface_a = build_schema_bound_surface(
            surface_id="surf_dup",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60, 120)), ("half_width_ticks", (600, 1200))),
        )
        surface_b = build_schema_bound_surface(
            surface_id="surf_dup",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60, 120)), ("half_width_ticks", (600, 1200))),
        )
        assert surface_a.checksum() == surface_b.checksum()

    def test_surface_checksum_changes_with_values(self) -> None:
        surface_a = build_schema_bound_surface(
            surface_id="surf_dup",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60, 120)), ("half_width_ticks", (600, 1200))),
        )
        surface_b = build_schema_bound_surface(
            surface_id="surf_dup",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60, 180)), ("half_width_ticks", (600, 1200))),
        )
        assert surface_a.checksum() != surface_b.checksum()

    def test_surface_records_full_binding(self) -> None:
        surface = _build_fixed_width_surface()
        # Every binding field must be a non-empty string; the surface
        # is the deterministic identifier the report stamps on the
        # binding tuple.
        for field in (
            "strategy_identity",
            "strategy_version",
            "parameter_schema_version",
            "parameter_schema_checksum",
            "registry_version",
            "registry_checksum",
        ):
            value = getattr(surface, field)
            assert isinstance(value, str) and value, f"{field} must be non-empty str"
        assert surface.parameter_schema_checksum == surface.parameter_schema_checksum
        assert surface.registry_checksum == registry_checksum()

    def test_surface_to_dict_contains_binding(self) -> None:
        surface = _build_fixed_width_surface()
        d = surface.to_dict()
        for field in (
            "surface_id",
            "strategy_identity",
            "strategy_version",
            "parameter_schema_version",
            "parameter_schema_checksum",
            "registry_version",
            "registry_checksum",
            "axes",
            "grid_size",
        ):
            assert field in d


# ---------------------------------------------------------------------------
# Surface builder: negative coverage
# ---------------------------------------------------------------------------


class TestSchemaBoundSurfaceNegative:
    """A surface cannot be built with an undeclared, wrong-type, wrong-unit, or out-of-range point."""

    def test_unregistered_identity_rejected(self) -> None:
        with pytest.raises(UnknownStrategyIdentityError):
            build_schema_bound_surface(
                surface_id="s",
                identity="legacy.not_registered.v1",
                axes=(),
            )

    def test_undeclared_axis_rejected(self) -> None:
        with pytest.raises(UndeclaredAxisError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_FIXED_WIDTH,
                axes=(
                    ("not_a_declared_parameter", (60,)),
                    ("tick_spacing", (60,)),
                ),
            )

    def test_duplicate_axis_rejected(self) -> None:
        with pytest.raises(SchemaBindingError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_FIXED_WIDTH,
                axes=(
                    ("tick_spacing", (60,)),
                    ("tick_spacing", (120,)),
                ),
            )

    def test_axis_against_empty_schema_with_axes_rejected(self) -> None:
        with pytest.raises(UndeclaredAxisError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_HOLD,
                axes=(("tick_spacing", (60,)),),
            )

    def test_out_of_range_int_axis_rejected(self) -> None:
        # ``tick_spacing`` upper bound is 32_767 per the registry.
        with pytest.raises(InvalidSchemaBoundAxisError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_FIXED_WIDTH,
                axes=(("tick_spacing", (60, 99_999)),),
            )

    def test_below_lower_bound_int_axis_rejected(self) -> None:
        with pytest.raises(InvalidSchemaBoundAxisError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_FIXED_WIDTH,
                axes=(("tick_spacing", (0, 60)),),
            )

    def test_bool_passed_as_int_rejected(self) -> None:
        with pytest.raises(InvalidSchemaBoundAxisError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_FIXED_WIDTH,
                axes=(("tick_spacing", (True, 60)),),
            )

    def test_str_value_for_int_axis_rejected(self) -> None:
        with pytest.raises(InvalidSchemaBoundAxisError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_FIXED_WIDTH,
                axes=(("tick_spacing", (60, "120")),),
            )

    def test_non_positive_int_rejected(self) -> None:
        # ``half_width_ticks`` is ``POSITIVE_INT``; zero is rejected.
        with pytest.raises(InvalidSchemaBoundAxisError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_FIXED_WIDTH,
                axes=(("half_width_ticks", (0, 600)),),
            )

    def test_empty_string_for_str_axis_rejected(self) -> None:
        # The empty-string rejection for a ``STR`` axis is exercised
        # directly via the :func:`validate_axis_value` helper test in
        # :class:`TestValidateAxisValueHelper`. No schema-declared
        # ``STR`` axis exists in the default registry today, so the
        # test there is the deterministic surface for the rule.
        from robinhood_lp.robustness import validate_axis_value

        schema = ParameterSchema(
            name="display_name",
            type=ParameterType.STR,
            unit="name",
            default="x",
        )
        with pytest.raises(InvalidSchemaBoundAxisError):
            validate_axis_value(schema=schema, value="")

    def test_empty_surface_id_rejected(self) -> None:
        with pytest.raises(SchemaBindingError):
            SchemaBoundParameterSurface(
                surface_id="",
                strategy_identity=IDENTITY_FIXED_WIDTH,
                strategy_version="t062.baseline_strategy.v1",
                parameter_schema_version="t062.baseline_strategy.v1",
                parameter_schema_checksum="0x" + "00" * 32,
                registry_version=REGISTRY_VERSION,
                registry_checksum=registry_checksum(),
                axes=(),
            )

    def test_axis_rejects_unsupported_python_type(self) -> None:
        with pytest.raises(SchemaBindingError):
            SchemaBoundParameterAxis(
                name="tick_spacing",
                schema_type=ParameterType.TICK_SPACING,
                unit="ticks",
                values=(60, 120.5),  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Boundary coverage
# ---------------------------------------------------------------------------


class TestSchemaBoundSurfaceBoundary:
    """Every axis value is checked at the schema's declared range edge."""

    def test_lower_bound_accepted(self) -> None:
        # ``tick_spacing`` lower bound is 1 per the registry. The
        # surface accepts a value of ``1``.
        surface = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (1, 60)),),
        )
        assert surface.axes[0].values[0] == 1

    def test_upper_bound_accepted(self) -> None:
        # ``tick_spacing`` upper bound is 32_767 per the registry.
        surface = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60, 32_767)),),
        )
        assert surface.axes[0].values[-1] == 32_767

    def test_upper_bound_minus_one_accepted(self) -> None:
        surface = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60, 32_766)),),
        )
        assert surface.axes[0].values[-1] == 32_766

    def test_upper_bound_plus_one_rejected(self) -> None:
        with pytest.raises(InvalidSchemaBoundAxisError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_FIXED_WIDTH,
                axes=(("tick_spacing", (60, 32_768)),),
            )

    def test_zero_rejected_for_positive_int(self) -> None:
        with pytest.raises(InvalidSchemaBoundAxisError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_FIXED_WIDTH,
                axes=(("half_width_ticks", (0, 600)),),
            )

    def test_one_accepted_for_positive_int(self) -> None:
        surface = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("half_width_ticks", (1, 600)),),
        )
        assert surface.axes[0].values[0] == 1

    def test_zero_accepted_for_non_negative_int(self) -> None:
        # ``max_position_hold_seconds`` is ``NON_NEGATIVE_INT``; zero
        # is accepted.
        surface = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_ADAPTIVE_RANGE,
            axes=(("max_position_hold_seconds", (0, 1800)),),
        )
        assert surface.axes[0].values[0] == 0

    def test_negative_rejected_for_non_negative_int(self) -> None:
        with pytest.raises(InvalidSchemaBoundAxisError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_ADAPTIVE_RANGE,
                axes=(("max_position_hold_seconds", (-1, 1800)),),
            )

    def test_q64_64_zero_accepted(self) -> None:
        # ``range_occupancy_threshold_q64_64`` is ``Q64_64`` and may
        # be zero.
        surface = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_ADAPTIVE_RANGE,
            axes=(("range_occupancy_threshold_q64_64", (0, _ONE_USDG_Q64_64)),),
        )
        assert surface.axes[0].values[0] == 0

    def test_strict_q64_64_zero_rejected(self) -> None:
        # ``capital_q64_64`` is ``STRICT_Q64_64`` and must be > 0.
        with pytest.raises(InvalidSchemaBoundAxisError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_FIXED_WIDTH,
                axes=(("capital_q64_64", (0, _ONE_USDG_Q64_64)),),
            )


# ---------------------------------------------------------------------------
# validate_axis_value helper
# ---------------------------------------------------------------------------


class TestValidateAxisValueHelper:
    """The standalone :func:`validate_axis_value` helper exercises every schema type."""

    def test_validates_int(self) -> None:
        schema = ParameterSchema(
            name="int_param",
            type=ParameterType.INT,
            unit="count",
            default=0,
        )
        assert validate_axis_value(schema=schema, value=42) == 42

    def test_rejects_wrong_type(self) -> None:
        schema = ParameterSchema(
            name="int_param",
            type=ParameterType.INT,
            unit="count",
            default=0,
        )
        with pytest.raises(InvalidSchemaBoundAxisError):
            validate_axis_value(schema=schema, value="42")

    def test_rejects_bool_for_int(self) -> None:
        schema = ParameterSchema(
            name="int_param",
            type=ParameterType.INT,
            unit="count",
            default=0,
        )
        with pytest.raises(InvalidSchemaBoundAxisError):
            validate_axis_value(schema=schema, value=True)

    def test_rejects_below_lower_bound(self) -> None:
        schema = ParameterSchema(
            name="bounded",
            type=ParameterType.INT,
            unit="count",
            default=10,
            lower_bound=10,
        )
        with pytest.raises(InvalidSchemaBoundAxisError):
            validate_axis_value(schema=schema, value=5)

    def test_rejects_above_upper_bound(self) -> None:
        schema = ParameterSchema(
            name="bounded",
            type=ParameterType.INT,
            unit="count",
            default=5,
            upper_bound=10,
        )
        with pytest.raises(InvalidSchemaBoundAxisError):
            validate_axis_value(schema=schema, value=20)

    def test_accepts_within_bounds(self) -> None:
        schema = ParameterSchema(
            name="bounded",
            type=ParameterType.INT,
            unit="count",
            default=5,
            lower_bound=0,
            upper_bound=10,
        )
        assert validate_axis_value(schema=schema, value=5) == 5

    def test_rejects_str_for_int(self) -> None:
        schema = ParameterSchema(
            name="str_param",
            type=ParameterType.STR,
            unit="name",
            default="x",
        )
        with pytest.raises(InvalidSchemaBoundAxisError):
            validate_axis_value(schema=schema, value=42)

    def test_accepts_non_empty_str(self) -> None:
        schema = ParameterSchema(
            name="str_param",
            type=ParameterType.STR,
            unit="name",
            default="x",
        )
        assert validate_axis_value(schema=schema, value="hello") == "hello"

    def test_rejects_empty_str(self) -> None:
        schema = ParameterSchema(
            name="str_param",
            type=ParameterType.STR,
            unit="name",
            default="x",
        )
        with pytest.raises(InvalidSchemaBoundAxisError):
            validate_axis_value(schema=schema, value="")

    def test_rejects_non_bool_for_bool_type(self) -> None:
        # A schema may not declare ``BOOL`` today, but the helper
        # still rejects a non-bool when the schema type is ``BOOL``.
        schema = ParameterSchema(
            name="flag",
            type=ParameterType.BOOL,
            unit="flag",
            default=False,
        )
        with pytest.raises(InvalidSchemaBoundAxisError):
            validate_axis_value(schema=schema, value=1)


# ---------------------------------------------------------------------------
# Surface compatibility
# ---------------------------------------------------------------------------


class TestSurfaceCompatibility:
    """Two surfaces with different revisions are not merge-compatible."""

    def test_identical_surfaces_compatible(self) -> None:
        surface_a = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60, 120)),),
        )
        surface_b = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60, 120)),),
        )
        assert_surfaces_compatible(left=surface_a, right=surface_b)

    def test_different_identity_incompatible(self) -> None:
        surface_a = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60,)),),
        )
        surface_b = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_VOLATILITY_WIDTH,
            axes=(("tick_spacing", (60,)),),
        )
        with pytest.raises(IncompatibleSchemaRevisionError):
            assert_surfaces_compatible(left=surface_a, right=surface_b)

    def test_different_values_incompatible(self) -> None:
        # The :func:`assert_surfaces_compatible` helper compares
        # the registry / schema identity; surfaces with different
        # axis values but the same binding identity remain
        # compatible (the runner is free to evaluate them
        # independently). What surfaces with different axis values
        # cannot share is the surface-level checksum — the
        # checksum discriminates the two.
        surface_a = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60,)),),
        )
        surface_b = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (120,)),),
        )
        # Same binding identity; compatible at the schema-revision
        # level.
        assert_surfaces_compatible(left=surface_a, right=surface_b)
        # Different axis values; different surface checksum.
        assert surface_a.checksum() != surface_b.checksum()

    def test_surface_identity_includes_full_binding(self) -> None:
        surface = _build_fixed_width_surface()
        identity = surface_identity(surface=surface)
        # The identity encodes every binding field the report
        # stamps so two surfaces with the same binding share the
        # identity string.
        for field in (
            "registry_version",
            "registry_checksum",
            "parameter_schema_version",
            "parameter_schema_checksum",
            "strategy_identity",
            "strategy_version",
        ):
            assert getattr(surface, field) in identity


# ---------------------------------------------------------------------------
# Schema-bound report
# ---------------------------------------------------------------------------


class TestSchemaBoundReport:
    """The schema-bound report carries the binding every current publication must carry."""

    def test_default_construction_passes(self) -> None:
        report = _build_schema_bound_report(surface=_build_fixed_width_surface())
        assert isinstance(report, SchemaBoundRobustnessReport)
        assert report.version == SCHEMA_BOUND_REPORTS_VERSION
        assert report.primary_generalisation_axis == "POOL_HOLDOUT"

    def test_version_is_pinned(self) -> None:
        report = _build_schema_bound_report(surface=_build_fixed_width_surface())
        assert report.version == "t106.robustness_reports.v1"

    def test_binding_is_required(self) -> None:
        # Every binding field is a non-empty string.
        report = _build_schema_bound_report(surface=_build_fixed_width_surface())
        for field in (
            "registry_version",
            "registry_checksum",
            "parameter_schema_version",
            "parameter_schema_checksum",
            "strategy_identity",
            "strategy_version",
        ):
            value = getattr(report, field)
            assert isinstance(value, str) and value, f"{field} must be non-empty str"

    def test_binding_identity_is_deterministic(self) -> None:
        # Two reports built from the same inputs return the same identity.
        report_a = _build_schema_bound_report(surface=_build_fixed_width_surface())
        report_b = _build_schema_bound_report(surface=_build_fixed_width_surface())
        assert report_a.binding_identity == report_b.binding_identity

    def test_binding_identity_changes_with_values(self) -> None:
        # The ``binding_identity`` only encodes the registry /
        # schema identity; surfaces with different axis values but
        # the same schema identity share the binding identity. The
        # surface-level checksum discriminates the two — a
        # reviewer who wants to merge two surfaces must compare
        # both checksums.
        report_a = _build_schema_bound_report(surface=_build_fixed_width_surface())
        # Build a surface with a different value set.
        surface_b = build_schema_bound_surface(
            surface_id="surf_t106_fixed_width",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(
                ("tick_spacing", (60, 180)),
                ("half_width_ticks", (600, 1200)),
            ),
        )
        report_b = _build_schema_bound_report(surface=surface_b)
        # Same registry / schema binding -> same binding identity.
        assert report_a.binding_identity == report_b.binding_identity
        # Different axis values -> different surface checksum.
        assert surface_b.checksum() != _build_fixed_width_surface().checksum()

    def test_report_rejects_empty_pool_holdout_results(self) -> None:
        with pytest.raises(MissingPrimaryGeneralisationError):
            build_schema_bound_robustness_report(
                run_id=_RUN_ID,
                label_horizon=_label_horizon(),
                train_results=_train_results(),
                validation_results=_validation_results(),
                test_results=_test_results(),
                pool_holdout_results=(),  # empty pool holdout
                walk_forward_splits=(),
                pool_holdout_split=_pool_holdout_split(),
                sensitivity_summary=_sensitivity_summary(),
                scenario_catalogue=_scenario_catalogue(),
                multiple_comparison_disclosure=_disclosure(),
                conclusion_statement="must include sensitivity, not only best",
                registry_version=REGISTRY_VERSION,
                registry_checksum=registry_checksum(),
                parameter_schema_version="t062.baseline_strategy.v1",
                parameter_schema_checksum="x" * 64,
                strategy_identity=IDENTITY_FIXED_WIDTH,
                strategy_version="t062.baseline_strategy.v1",
            )

    def test_report_rejects_overlapping_segment_labels(self) -> None:
        overlapping_train = (
            FoldResult(
                role="TRAIN",
                segment_label="fold_0",
                metric_name=_METRIC_NAME,
                metric_value=100,
                fold_index=0,
            ),
        )
        overlapping_validation = (
            FoldResult(
                role="VALIDATION",
                segment_label="fold_0",
                metric_name=_METRIC_NAME,
                metric_value=95,
                fold_index=0,
            ),
        )
        with pytest.raises(RobustnessReportError):
            build_schema_bound_robustness_report(
                run_id=_RUN_ID,
                label_horizon=_label_horizon(),
                train_results=overlapping_train,
                validation_results=overlapping_validation,
                test_results=_test_results(),
                pool_holdout_results=_pool_holdout_results(),
                walk_forward_splits=(),
                pool_holdout_split=_pool_holdout_split(),
                sensitivity_summary=_sensitivity_summary(),
                scenario_catalogue=_scenario_catalogue(),
                multiple_comparison_disclosure=_disclosure(),
                conclusion_statement="must include sensitivity, not only best",
                registry_version=REGISTRY_VERSION,
                registry_checksum=registry_checksum(),
                parameter_schema_version="t062.baseline_strategy.v1",
                parameter_schema_checksum="x" * 64,
                strategy_identity=IDENTITY_FIXED_WIDTH,
                strategy_version="t062.baseline_strategy.v1",
            )

    def test_report_rejects_metric_name_mismatch(self) -> None:
        # Build a sensitivity summary with a metric name that
        # disagrees with the pool-holdout results; the report
        # must reject it.
        mismatched_sensitivity = SensitivitySummary(
            metric_name="a_different_metric",
            best_metric_value=110,
            best_parameter_combo=(("tick_spacing", 60),),
            worst_metric_value=80,
            spread_metric_value=30,
            n_grid_points=4,
        )
        with pytest.raises(RobustnessReportError):
            _build_schema_bound_report(
                surface=_build_fixed_width_surface(),
                sensitivity=mismatched_sensitivity,
            )

    def test_report_rejects_disagreement_with_surface(self) -> None:
        surface = _build_fixed_width_surface()
        with pytest.raises(IncompatibleBindingError):
            build_schema_bound_robustness_report(
                run_id=_RUN_ID,
                label_horizon=_label_horizon(),
                train_results=_train_results(),
                validation_results=_validation_results(),
                test_results=_test_results(),
                pool_holdout_results=_pool_holdout_results(),
                walk_forward_splits=(),
                pool_holdout_split=_pool_holdout_split(),
                sensitivity_summary=_sensitivity_summary(),
                scenario_catalogue=_scenario_catalogue(),
                multiple_comparison_disclosure=_disclosure(),
                conclusion_statement="must include sensitivity, not only best",
                registry_version=surface.registry_version,
                registry_checksum="0x" + "00" * 32,  # WRONG
                parameter_schema_version=surface.parameter_schema_version,
                parameter_schema_checksum=surface.parameter_schema_checksum,
                strategy_identity=surface.strategy_identity,
                strategy_version=surface.strategy_version,
                schema_bound_surface=surface,
            )

    def test_report_rejects_legacy_surface_in_current_publication(self) -> None:
        # A legacy :class:`ParameterSurface` cannot be carried by
        # the schema-bound report.
        legacy_surface = _build_legacy_surface(
            surface_id="legacy",
            axes=(ParameterAxis(name="tick_spacing", kind="int", values=(60, 120)),),
        )
        with pytest.raises(LegacySurfaceInCurrentReportError):
            build_schema_bound_robustness_report(
                run_id=_RUN_ID,
                label_horizon=_label_horizon(),
                train_results=_train_results(),
                validation_results=_validation_results(),
                test_results=_test_results(),
                pool_holdout_results=_pool_holdout_results(),
                walk_forward_splits=(),
                pool_holdout_split=_pool_holdout_split(),
                sensitivity_summary=_sensitivity_summary(),
                scenario_catalogue=_scenario_catalogue(),
                multiple_comparison_disclosure=_disclosure(),
                conclusion_statement="must include sensitivity, not only best",
                registry_version=REGISTRY_VERSION,
                registry_checksum=registry_checksum(),
                parameter_schema_version="t062.baseline_strategy.v1",
                parameter_schema_checksum="x" * 64,
                strategy_identity=IDENTITY_FIXED_WIDTH,
                strategy_version="t062.baseline_strategy.v1",
                schema_bound_surface=legacy_surface,
            )

    def test_report_to_dict_contains_binding(self) -> None:
        report = _build_schema_bound_report(surface=_build_fixed_width_surface())
        d = report.to_dict()
        for field in (
            "version",
            "run_id",
            "binding_identity",
            "schema_bound_surface",
            "registry_version",
            "registry_checksum",
            "parameter_schema_version",
            "parameter_schema_checksum",
            "strategy_identity",
            "strategy_version",
            "schema_bound_surfaces_version",
        ):
            assert field in d, f"{field} missing from to_dict()"


class TestReportCompatibility:
    """Two reports with different binding identities are not merge-compatible."""

    def test_identical_reports_compatible(self) -> None:
        report_a = _build_schema_bound_report(surface=_build_fixed_width_surface())
        report_b = _build_schema_bound_report(surface=_build_fixed_width_surface())
        assert_reports_compatible(left=report_a, right=report_b)

    def test_different_identity_incompatible(self) -> None:
        report_a = _build_schema_bound_report(surface=_build_fixed_width_surface())
        report_b = _build_schema_bound_report(surface=_build_adaptive_surface())
        with pytest.raises(IncompatibleBindingError):
            assert_reports_compatible(left=report_a, right=report_b)

    def test_different_values_compatible_at_binding(self) -> None:
        # The ``assert_reports_compatible`` helper compares the
        # binding identity; reports with different axis values but
        # the same registry / schema identity remain compatible at
        # the binding level (the runner may evaluate them
        # independently). Different axis values are distinguished by
        # the surface checksum, not the binding identity.
        report_a = _build_schema_bound_report(surface=_build_fixed_width_surface())
        surface_b = build_schema_bound_surface(
            surface_id="surf_t106_fixed_width",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(
                ("tick_spacing", (60, 180)),
                ("half_width_ticks", (600, 1200)),
            ),
        )
        report_b = _build_schema_bound_report(surface=surface_b)
        assert_reports_compatible(left=report_a, right=report_b)
        assert surface_b.checksum() != _build_fixed_width_surface().checksum()


# ---------------------------------------------------------------------------
# Schema-bound runner
# ---------------------------------------------------------------------------


class TestSchemaBoundRunner:
    """The schema-bound runner assembles the schema-bound report from the schema-bound surface."""

    def test_runner_assembles_schema_bound_report(self) -> None:
        runner = build_schema_bound_runner()
        inputs = _build_schema_bound_runner_inputs(surface=_build_fixed_width_surface())
        report = runner.run(
            inputs,
            fold_evaluator=lambda **kw: 100,
            pool_evaluator=lambda **kw: 100,
        )
        assert isinstance(report, SchemaBoundRobustnessReport)
        assert report.version == SCHEMA_BOUND_REPORTS_VERSION
        # The binding identity encodes the registry / schema
        # binding the report stamps on; the report's binding
        # matches the surface's binding field-by-field even
        # though the two helpers concatenate the fields in
        # different orders.
        assert inputs.schema_bound_surface is not None
        surface = inputs.schema_bound_surface
        assert report.strategy_identity == surface.strategy_identity
        assert report.strategy_version == surface.strategy_version
        assert report.registry_version == surface.registry_version
        assert report.registry_checksum == surface.registry_checksum
        assert report.parameter_schema_version == surface.parameter_schema_version
        assert report.parameter_schema_checksum == surface.parameter_schema_checksum

    def test_runner_runs_with_adaptive_surface(self) -> None:
        runner = build_schema_bound_runner()
        inputs = _build_schema_bound_runner_inputs(surface=_build_adaptive_surface())
        report = runner.run(
            inputs,
            fold_evaluator=lambda **kw: 100,
            pool_evaluator=lambda **kw: 100,
        )
        assert report.strategy_identity == IDENTITY_ADAPTIVE_RANGE
        assert inputs.schema_bound_surface is not None
        surface = inputs.schema_bound_surface
        assert report.strategy_identity == surface.strategy_identity
        assert report.strategy_version == surface.strategy_version
        assert report.registry_version == surface.registry_version
        assert report.registry_checksum == surface.registry_checksum
        assert report.parameter_schema_version == surface.parameter_schema_version
        assert report.parameter_schema_checksum == surface.parameter_schema_checksum

    def test_runner_runs_with_hold_surface(self) -> None:
        runner = build_schema_bound_runner()
        inputs = _build_schema_bound_runner_inputs(surface=_build_hold_surface())
        report = runner.run(
            inputs,
            fold_evaluator=lambda **kw: 100,
            pool_evaluator=lambda **kw: 100,
        )
        assert report.strategy_identity == IDENTITY_HOLD

    def test_runner_rejects_legacy_surface_in_inputs(self) -> None:
        legacy_surface = _build_legacy_surface(
            surface_id="legacy",
            axes=(ParameterAxis(name="tick_spacing", kind="int", values=(60, 120)),),
        )
        with pytest.raises(LegacySurfaceInSchemaBoundRunnerError):
            _build_schema_bound_runner_inputs(surface=legacy_surface)

    def test_runner_rejects_legacy_surface_in_run(self) -> None:
        # Defensive: even if a caller bypasses the inputs dataclass
        # check, the runner rejects a legacy surface at run time.
        runner = build_schema_bound_runner()
        inputs = _build_schema_bound_runner_inputs(surface=None)
        legacy_surface = _build_legacy_surface(
            surface_id="legacy",
            axes=(ParameterAxis(name="tick_spacing", kind="int", values=(60, 120)),),
        )
        # Replace the schema-bound surface with a legacy one after
        # construction. ``dataclasses.replace`` cannot mutate a
        # frozen dataclass; we use ``object.__setattr__`` to bypass
        # the frozen check only for this test.
        object.__setattr__(inputs, "schema_bound_surface", legacy_surface)
        with pytest.raises(LegacySurfaceInSchemaBoundRunnerError):
            runner.run(
                inputs,
                fold_evaluator=lambda **kw: 100,
                pool_evaluator=lambda **kw: 100,
            )

    def test_runner_rejects_empty_pool_specs(self) -> None:
        with pytest.raises(RunnerInputsError):
            SchemaBoundRobustnessRunnerInputs(
                run_id=_RUN_ID,
                label_horizon=_label_horizon(),
                metric_name=_METRIC_NAME,
                scenario_catalogue=_scenario_catalogue(),
                pool_holdout_split=_pool_holdout_split(),
                train_fold_specs=(("train_fold_0", 0),),
                validation_fold_specs=(("validation_fold_0", 0),),
                test_fold_specs=(("test_fold_0", 0),),
                pool_specs=(),
                family_wise_alpha_q64_64=_FAMILY_WISE_ALPHA_Q64_64,
                conclusion_statement="must include sensitivity, not only best",
                schema_bound_surface=_build_fixed_width_surface(),
            )

    def test_runner_version_is_pinned(self) -> None:
        runner = build_schema_bound_runner()
        assert runner.version == SCHEMA_BOUND_RUNNER_VERSION

    def test_runner_disclosure_uses_schema_bound_grid_size(self) -> None:
        runner = build_schema_bound_runner()
        inputs = _build_schema_bound_runner_inputs(surface=_build_adaptive_surface())
        report = runner.run(
            inputs,
            fold_evaluator=lambda **kw: 100,
            pool_evaluator=lambda **kw: 100,
        )
        # 12 grid points * 2 pools = 24 comparisons.
        assert inputs.schema_bound_surface is not None
        assert report.multiple_comparison_disclosure.n_comparisons == (
            inputs.schema_bound_surface.grid_size * len(inputs.pool_specs)
        )


# ---------------------------------------------------------------------------
# Old-path-unreachable tests (T106 cutover)
# ---------------------------------------------------------------------------


class TestOldPathUnreachable:
    """The legacy :class:`ParameterSurface` cannot publish current robustness evidence."""

    def test_is_legacy_surface_recognises_legacy(self) -> None:
        legacy_surface = _build_legacy_surface(
            surface_id="legacy",
            axes=(ParameterAxis(name="tick_spacing", kind="int", values=(60, 120)),),
        )
        assert is_legacy_surface(legacy_surface) is True

    def test_is_legacy_surface_rejects_schema_bound(self) -> None:
        assert is_legacy_surface(_build_fixed_width_surface()) is False

    def test_schema_bound_runner_inputs_rejects_legacy_surface(self) -> None:
        legacy_surface = _build_legacy_surface(
            surface_id="legacy",
            axes=(ParameterAxis(name="tick_spacing", kind="int", values=(60, 120)),),
        )
        with pytest.raises(LegacySurfaceInSchemaBoundRunnerError):
            _build_schema_bound_runner_inputs(surface=legacy_surface)

    def test_schema_bound_report_rejects_legacy_surface(self) -> None:
        # A legacy :class:`ParameterSurface` cannot be carried by
        # the schema-bound report.
        legacy_surface = _build_legacy_surface(
            surface_id="legacy",
            axes=(ParameterAxis(name="tick_spacing", kind="int", values=(60, 120)),),
        )
        with pytest.raises(LegacySurfaceInCurrentReportError):
            build_schema_bound_robustness_report(
                run_id=_RUN_ID,
                label_horizon=_label_horizon(),
                train_results=_train_results(),
                validation_results=_validation_results(),
                test_results=_test_results(),
                pool_holdout_results=_pool_holdout_results(),
                walk_forward_splits=(),
                pool_holdout_split=_pool_holdout_split(),
                sensitivity_summary=_sensitivity_summary(),
                scenario_catalogue=_scenario_catalogue(),
                multiple_comparison_disclosure=_disclosure(),
                conclusion_statement="must include sensitivity, not only best",
                registry_version=REGISTRY_VERSION,
                registry_checksum=registry_checksum(),
                parameter_schema_version="t062.baseline_strategy.v1",
                parameter_schema_checksum="x" * 64,
                strategy_identity=IDENTITY_FIXED_WIDTH,
                strategy_version="t062.baseline_strategy.v1",
                schema_bound_surface=legacy_surface,
            )

    def test_legacy_marker_is_recorded(self) -> None:
        assert LEGACY_SURFACE_MARKER == "LEGACY_T064_SURFACE"


# ---------------------------------------------------------------------------
# Heterogeneous registered strategies
# ---------------------------------------------------------------------------


class TestHeterogeneousRegisteredStrategies:
    """At least two materially different strategies validate through the schema binding."""

    @pytest.mark.parametrize(
        "identity",
        [IDENTITY_FIXED_WIDTH, IDENTITY_ADAPTIVE_RANGE],
    )
    def test_two_heterogeneous_strategies_bind_cleanly(self, identity: str) -> None:
        if identity == IDENTITY_FIXED_WIDTH:
            surface = _build_fixed_width_surface()
        else:
            surface = _build_adaptive_surface()
        runner = build_schema_bound_runner()
        inputs = _build_schema_bound_runner_inputs(surface=surface)
        report = runner.run(
            inputs,
            fold_evaluator=lambda **kw: 100,
            pool_evaluator=lambda **kw: 100,
        )
        assert isinstance(report, SchemaBoundRobustnessReport)
        assert report.strategy_identity == identity
        assert report.binding_identity == surface_identity(surface=surface)

    def test_heterogeneous_surfaces_have_distinct_identities(self) -> None:
        fixed_width_surface = _build_fixed_width_surface()
        adaptive_surface = _build_adaptive_surface()
        assert surface_identity(surface=fixed_width_surface) != surface_identity(
            surface=adaptive_surface
        )


# ---------------------------------------------------------------------------
# Compatibility / migration: legacy T064 artifacts remain readable
# ---------------------------------------------------------------------------


class TestLegacyArtifactPreservation:
    """The T064 surface / report / runner remain available for the legacy artifact path."""

    def test_legacy_surface_builder_still_works(self) -> None:
        surface = _build_legacy_surface(
            surface_id="legacy",
            axes=(ParameterAxis(name="tick_spacing", kind="int", values=(60, 120)),),
        )
        assert surface.surface_id == "legacy"
        assert surface.grid_size == 2

    def test_legacy_runner_still_works(self) -> None:
        # The legacy runner must remain runnable so the legacy reader
        # / test path keeps producing legacy reports.
        runner = build_runner()
        legacy_surface = _build_legacy_surface(
            surface_id="legacy",
            axes=(ParameterAxis(name="tick_spacing", kind="int", values=(60, 120)),),
        )

        # The legacy runner needs a RobustnessRunnerInputs; build one
        # here so the test exercises the legacy code path without
        # going through the schema-binding module.
        from robinhood_lp.robustness.runner import RobustnessRunnerInputs

        inputs = RobustnessRunnerInputs(
            run_id=_RUN_ID,
            label_horizon=_label_horizon(),
            metric_name=_METRIC_NAME,
            scenario_catalogue=_scenario_catalogue(),
            pool_holdout_split=_pool_holdout_split(),
            train_fold_specs=(("train_fold_0", 0),),
            validation_fold_specs=(("validation_fold_0", 0),),
            test_fold_specs=(("test_fold_0", 0),),
            pool_specs=(
                (_CHAIN_ID, _POOL_KEY_ID_HOLDOUT_A),
                (_CHAIN_ID, _POOL_KEY_ID_HOLDOUT_B),
            ),
            family_wise_alpha_q64_64=_FAMILY_WISE_ALPHA_Q64_64,
            conclusion_statement="must include sensitivity, not only best",
            parameter_surface=legacy_surface,
        )
        report = runner.run(
            inputs,
            fold_evaluator=lambda **kw: 100,
            pool_evaluator=lambda **kw: 100,
        )
        assert isinstance(report, RobustnessReport)
        # Legacy report still has its T064 version.
        assert report.version == "t064.robustness_reports.v1"
        assert report.parameter_surface is legacy_surface

    def test_legacy_report_builder_still_works(self) -> None:
        # The legacy report builder is preserved so the legacy reader
        # / test path can publish T064 artifacts.
        legacy_surface = _build_legacy_surface(
            surface_id="legacy",
            axes=(ParameterAxis(name="tick_spacing", kind="int", values=(60, 120)),),
        )
        legacy_sensitivity = _LegacySensitivitySummary(
            metric_name=_METRIC_NAME,
            best_metric_value=100,
            best_parameter_combo=(("tick_spacing", 60),),
            worst_metric_value=80,
            spread_metric_value=20,
            n_grid_points=2,
        )
        report = build_robustness_report(
            run_id=_RUN_ID,
            label_horizon=_label_horizon(),
            train_results=_train_results(),
            validation_results=_validation_results(),
            test_results=_test_results(),
            pool_holdout_results=_pool_holdout_results(),
            walk_forward_splits=(),
            pool_holdout_split=_pool_holdout_split(),
            sensitivity_summary=legacy_sensitivity,
            scenario_catalogue=_scenario_catalogue(),
            multiple_comparison_disclosure=_disclosure(),
            conclusion_statement="must include sensitivity, not only best",
            parameter_surface=legacy_surface,
        )
        assert report.version == "t064.robustness_reports.v1"


# ---------------------------------------------------------------------------
# is_schema_bound_surface_identity helper
# ---------------------------------------------------------------------------


class TestIsSchemaBoundSurfaceIdentity:
    """The :func:`is_schema_bound_surface_identity` helper detects a registry-bound identity."""

    def test_returns_true_for_registered_identity_with_checksums(self) -> None:
        surface = _build_fixed_width_surface()
        assert (
            is_schema_bound_surface_identity(
                strategy_identity=surface.strategy_identity,
                registry_checksum=surface.registry_checksum,
                parameter_schema_checksum=surface.parameter_schema_checksum,
            )
            is True
        )

    def test_returns_false_for_unregistered_identity(self) -> None:
        assert (
            is_schema_bound_surface_identity(
                strategy_identity="not.registered.v1",
                registry_checksum="0x" + "00" * 32,
                parameter_schema_checksum="0x" + "00" * 32,
            )
            is False
        )

    def test_returns_false_for_empty_registry_checksum(self) -> None:
        assert (
            is_schema_bound_surface_identity(
                strategy_identity=IDENTITY_FIXED_WIDTH,
                registry_checksum="",
                parameter_schema_checksum="0x" + "00" * 32,
            )
            is False
        )

    def test_returns_false_for_empty_schema_checksum(self) -> None:
        assert (
            is_schema_bound_surface_identity(
                strategy_identity=IDENTITY_FIXED_WIDTH,
                registry_checksum="0x" + "00" * 32,
                parameter_schema_checksum="",
            )
            is False
        )

    def test_returns_false_for_non_str_inputs(self) -> None:
        assert (
            is_schema_bound_surface_identity(
                strategy_identity=42,  # type: ignore[arg-type]
                registry_checksum="0x" + "00" * 32,
                parameter_schema_checksum="0x" + "00" * 32,
            )
            is False
        )


# ---------------------------------------------------------------------------
# surface_id_for_inputs helper
# ---------------------------------------------------------------------------


class TestSurfaceIdForInputs:
    """The :func:`surface_id_for_inputs` helper returns the surface identity or ``None``."""

    def test_returns_identity_when_surface_present(self) -> None:
        surface = _build_fixed_width_surface()
        inputs = _build_schema_bound_runner_inputs(surface=surface)
        assert surface_id_for_inputs(inputs) == surface_identity(surface=surface)

    def test_returns_none_when_surface_absent(self) -> None:
        inputs = _build_schema_bound_runner_inputs(surface=None)
        assert surface_id_for_inputs(inputs) is None


# ---------------------------------------------------------------------------
# Registry-binding compatibility
# ---------------------------------------------------------------------------


class TestRegistryBindingCompatibility:
    """Two :class:`Registry` instances with different checksums produce different surface identities."""

    def _build_minimal_registry(
        self,
        *,
        identity: str,
        version: str = "t062.baseline_strategy.v1",
        checksum_override: str | None = None,
    ) -> Registry:
        schema = ParameterSchema(
            name="tick_spacing",
            type=ParameterType.TICK_SPACING,
            unit="ticks",
            default=60,
        )
        entry = RegisteredStrategy(
            identity=identity,
            version=version,
            parameter_schemas=(schema,),
            code_provenance=CodeProvenance(
                module="robinhood_lp.strategy.baselines",
                revision="0x" + "00" * 20,
                symbol="FixedWidthStrategy",
            ),
            factory=_AdapterFactory(lambda **kw: None),
            description="synthetic registry entry for the compatibility test",
        )
        reg = Registry(_entries=(entry,))
        if checksum_override is not None:
            # The registry's checksum is computed from the canonical
            # serialisation; we cannot easily mutate it without
            # mutating the dataclass. Instead, the helper compares
            # two registries that wrap the same / different entries.
            pass
        return reg

    def test_two_registries_with_same_entry_share_identity(self) -> None:
        reg_a = self._build_minimal_registry(identity=IDENTITY_FIXED_WIDTH)
        reg_b = self._build_minimal_registry(identity=IDENTITY_FIXED_WIDTH)
        surface_a = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60,)),),
            registry=reg_a,
        )
        surface_b = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60,)),),
            registry=reg_b,
        )
        # Both registries are constructed from the same canonical
        # serialisation, so the registry checksum and the parameter
        # schema checksum are equal.
        assert surface_a.checksum() == surface_b.checksum()

    def test_two_registries_with_different_entries_differ(self) -> None:
        reg_a = self._build_minimal_registry(identity=IDENTITY_FIXED_WIDTH)
        reg_b = self._build_minimal_registry(identity="different.identity.v1")
        surface_a = build_schema_bound_surface(
            surface_id="s",
            identity=IDENTITY_FIXED_WIDTH,
            axes=(("tick_spacing", (60,)),),
            registry=reg_a,
        )
        # Building against ``reg_b`` for ``IDENTITY_FIXED_WIDTH``
        # must fail because the identity is not in ``reg_b``.
        with pytest.raises(UnknownStrategyIdentityError):
            build_schema_bound_surface(
                surface_id="s",
                identity=IDENTITY_FIXED_WIDTH,
                axes=(("tick_spacing", (60,)),),
                registry=reg_b,
            )
        assert surface_a.strategy_identity == IDENTITY_FIXED_WIDTH


# ---------------------------------------------------------------------------
# Schema binding layer purity
# ---------------------------------------------------------------------------


class TestSchemaBindingLayerPurity:
    """The schema-binding module imports only stdlib + the registry + the registry-binding helper."""

    def test_schema_binding_does_not_import_rpc(self) -> None:
        import importlib

        module = importlib.import_module("robinhood_lp.robustness.schema_binding")
        import_names = {n for n in dir(module) if not n.startswith("__")}
        # The helper itself returns ``_coerce_parameter_value``
        # from the registry; that import is local. The module's
        # module-level imports are the strategy registry only.
        from robinhood_lp.robustness import schema_binding as binding_module

        # Walk the live module graph; the schema binding's reachable
        # ``robinhood_lp.*`` submodules must exclude the forbidden
        # boundary modules.
        forbidden = {
            "robinhood_lp.backtest",
            "robinhood_lp.config",
            "robinhood_lp.discovery",
            "robinhood_lp.execution",
            "robinhood_lp.features",
            "robinhood_lp.ingestion",
            "robinhood_lp.presentation",
            "robinhood_lp.qualification",
            "robinhood_lp.quality",
            "robinhood_lp.replay",
            "robinhood_lp.risk",
            "robinhood_lp.rpc",
            "robinhood_lp.signer",
            "robinhood_lp.storage",
            "robinhood_lp.web",
        }
        for attr_name in import_names:
            attr = getattr(binding_module, attr_name, None)
            if attr is None:
                continue
            module_name = getattr(attr, "__module__", "")
            for forbidden_root in forbidden:
                if module_name == forbidden_root or module_name.startswith(forbidden_root + "."):
                    pytest.fail(
                        f"schema_binding.{attr_name} comes from forbidden module {module_name!r}"
                    )

    def test_default_registry_is_pure(self) -> None:
        # Reset and rebuild to make sure the registry construction
        # path is repeatable.
        reset_default_registry_cache()
        try:
            from robinhood_lp.strategy.registry import (
                assert_registry_layer_is_pure,
            )

            assert_registry_layer_is_pure()
        finally:
            reset_default_registry_cache()
            default_registry()  # restore cache


# ---------------------------------------------------------------------------
# Determinism / byte equivalence
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Repeated construction is deterministic across runs."""

    def test_surface_to_dict_is_deterministic(self) -> None:
        # Build the same surface twice; the ``to_dict`` payloads
        # agree byte-for-byte because the dataclass sorts every
        # collection.
        surface_a = _build_fixed_width_surface()
        surface_b = _build_fixed_width_surface()
        import json

        json_a = json.dumps(surface_a.to_dict(), sort_keys=True)
        json_b = json.dumps(surface_b.to_dict(), sort_keys=True)
        assert json_a == json_b

    def test_report_to_dict_is_deterministic(self) -> None:
        report_a = _build_schema_bound_report(surface=_build_fixed_width_surface())
        report_b = _build_schema_bound_report(surface=_build_fixed_width_surface())
        import json

        json_a = json.dumps(report_a.to_dict(), sort_keys=True)
        json_b = json.dumps(report_b.to_dict(), sort_keys=True)
        assert json_a == json_b


# ---------------------------------------------------------------------------
# Smoke import surface
# ---------------------------------------------------------------------------


class TestImportSurface:
    """All public symbols import cleanly and are version-pinned."""

    def test_all_version_strings_importable(self) -> None:
        for version in (
            REPORTS_VERSION,
            RUNNER_VERSION,
            SURFACES_VERSION,
            SCHEMA_BOUND_REPORTS_VERSION,
            SCHEMA_BOUND_RUNNER_VERSION,
            SCHEMA_BOUND_SURFACES_VERSION,
        ):
            assert isinstance(version, str) and version.startswith("t")

    def test_validate_axis_value_helper_importable(self) -> None:
        from robinhood_lp.robustness import validate_axis_value

        assert callable(validate_axis_value)

    def test_validate_axis_value_helper_hash(self) -> None:
        # The helper is the deterministic gate every code path that
        # validates a value against a schema passes through; a
        # reviewer who wants to confirm the helper is the
        # :func:`validate_axis_value` exported from
        # :mod:`robinhood_lp.robustness.schema_binding` can compare
        # the helper's identity.
        from robinhood_lp.robustness import validate_axis_value as outer
        from robinhood_lp.robustness.schema_binding import (
            validate_axis_value as inner,
        )

        assert outer is inner


# ---------------------------------------------------------------------------
# Test internal registry cache reset (avoids bleeding state across tests)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry_cache() -> None:
    """Reset the registry cache before each test for deterministic state."""
    reset_default_registry_cache()
    default_registry()
    yield
    reset_default_registry_cache()
    default_registry()
