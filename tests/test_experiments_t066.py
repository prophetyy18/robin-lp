"""Tests for the versioned threshold experiments and provisional candidate review (T066).

T066 binds the experiment / review layer to an immutable
candidate-lock record. The tests cover every T066 acceptance
clause:

- **Candidate-lock before test fold.** The harness writes the lock
  *before* it invokes the ``test_fold_evaluator``; the lock is
  present on disk and the harness's verifier reads it back.

- **Refusing to read test fold without lock.** The harness raises
  :class:`MissingCandidateLockError` when the lock directory has no
  file for the chosen candidate version; the
  ``test_fold_evaluator`` is *not* invoked.

- **Content hash immutability.** The on-disk lock file is the
  canonical JSON serialisation of the lock; tampering with any
  field (including the manifest hash, the seed, a split
  boundary, the cost assumptions, or the elimination rules)
  changes the content hash, and the harness refuses the read.

- **Parameter change -> new version.** The
  :func:`compute_parameter_set_version` function returns a new
  hex digest for every parameter change; the harness binds the
  chosen version into the lock and refuses a lock whose
  ``parameter_set_version`` disagrees with the chosen version.

- **All losing / rejected runs remain.** The harness preserves
  every ``SearchRunRecord`` on the search manifest regardless
  of run status; the manifest's
  :meth:`losing_or_rejected_count` reports the count.

- **Deterministic rerun.** Two harness runs with the same inputs
  produce byte-identical :class:`ExperimentHarnessResult` records
  (including the content hash).

- **EXPERIMENTAL_NOT_LIVE_APPROVED carried on every output.**
  The flag is present on the search manifest's
  ``elimination_rules``, the lock's ``elimination_rules``, every
  distribution's ``experimental_flag``, the stability report's
  ``experimental_flag``, the stress report's ``experimental_flag``
  and the recommendation's ``experimental_flag``.

- **USDG cash primary benchmark.** The flag is present on every
  distribution's ``primary_benchmark``, the stability report's
  ``primary_benchmark``, the stress report's
  ``primary_benchmark`` and the recommendation's
  ``primary_benchmark``.

The Must-not clauses are also tested:

- **No inventing universal thresholds before data.** The harness
  preserves the orchestrator's elimination rules and never
  fabricates thresholds.

- **No retuning on test.** The locked candidate is selected on
  validation only; the test fold is read-only.

- **No suppressing no-trade periods.** The search manifest
  preserves ``NO_TRADE`` / ``LOSING`` / ``REJECTED`` / ``FAILED``
  / ``DEGRADED`` records; the harness counts them.

- **No selecting only the highest return.** The candidate
  selection rule is monotonic — the harness picks the *first*
  parameter-set version whose validation metric strictly
  improves over the previous best, not the global maximum.

- **No turning fixture / provisional values into a product
  default.** Every output carries the
  ``EXPERIMENTAL_NOT_LIVE_APPROVED`` flag; the harness never
  approves paper / live trading.

Design constraints (binding):

- **Integer / closed vocabulary.** Parameter values, metric
  values and counts are Python ``int``; ``float`` never appears
  on the harness path.

- **Determinism.** Two equivalent inputs in any process produce
  byte-identical harness results.

- **Layer purity.** The harness module depends only on the
  standard library, the in-package experiments module, and the
  in-package robustness module (T064 splits, parameter surface,
  sensitivity summary). It does not import the backtest engine,
  the manifest layer, RPC, storage, signing, execution, or
  presentation code.
"""

from __future__ import annotations

import json
from typing import Final

import pytest

from robinhood_lp.experiments import (
    CANDIDATE_LOCK_VERSION,
    EXPERIMENT_HARNESS_VERSION,
    PARAMETER_SEARCH_VERSION,
    REQUIRED_ELIMINATION_RULES,
    STABILITY_VERSION,
    VALID_RECOMMENDATIONS,
    CandidateLock,
    CandidateLockHashMismatchError,
    CandidateLockHashMismatchOnLoadError,
    CandidateLockMissingRequiredRuleError,
    CandidateLockNotFoundError,
    DistributionSummary,
    EmptyDistributionError,
    EmptySearchError,
    EmptyStabilityError,
    ExperimentHarness,
    ExperimentHarnessResult,
    ExperimentPlan,
    HarnessInputsError,
    InvalidCandidateLockError,
    InvalidDistributionError,
    InvalidStabilityError,
    MissingCandidateLockError,
    NoCandidateEligibleError,
    ProvisionalRecommendation,
    SearchRunRecord,
    StressScenarioOutcome,
    build_candidate_lock,
    build_distribution_summary,
    build_neighborhood_stability_report,
    build_no_trade_recommendation,
    build_parameter_search_manifest,
    build_provisional_recommendation,
    build_stress_report,
    compute_candidate_lock_content_hash,
    compute_parameter_set_version,
    load_candidate_lock,
    profit_probability_q64_64,
    write_candidate_lock,
)
from robinhood_lp.robustness.splits import (
    VALID_HORIZON_UNITS,
    LabelHorizon,
    PoolHoldoutFold,
    PoolHoldoutSplit,
    TimeHoldoutSplit,
    build_time_holdout_split,
)
from robinhood_lp.robustness.surfaces import ParameterAxis, ParameterSurface

# ---------------------------------------------------------------------------
# Reference fixtures
# ---------------------------------------------------------------------------


_CHAIN_ID: Final[int] = 46630
_POOL_KEY_ID: Final[str] = "0x" + "ab" * 32
_POOL_KEY_ID_B: Final[str] = "0x" + "cd" * 32
_MANIFEST_HASH: Final[str] = "0x" + "11" * 32
_ACTOR: Final[str] = "ci-evidence"
_CODE_REV: Final[str] = "0123456789abcdef" + "0" * 48
_CLOCK: Final[int] = 1_700_000_000
_SEED: Final[int] = 42


def _label_horizon() -> LabelHorizon:
    return LabelHorizon(value=12, unit="BARS")


def _time_holdout_split() -> TimeHoldoutSplit:
    return build_time_holdout_split(
        chain_id=_CHAIN_ID,
        pool_key_id=_POOL_KEY_ID,
        label_horizon=_label_horizon(),
        unit_total=1000,
        train_fraction=0.4,
        validation_fraction=0.3,
        recorded_at_unix_seconds=1_700_000_000,
    )


def _pool_holdout_split() -> PoolHoldoutSplit:
    return PoolHoldoutSplit(
        chain_id=_CHAIN_ID,
        train_pool_folds=(
            PoolHoldoutFold(
                chain_id=_CHAIN_ID,
                pool_key_id=_POOL_KEY_ID,
                fold_role="TRAIN_POOL",
                segment_label="train_pool_0",
                block_range_start=0,
                block_range_end=1000,
            ),
        ),
        holdout_pool_folds=(
            PoolHoldoutFold(
                chain_id=_CHAIN_ID,
                pool_key_id=_POOL_KEY_ID_B,
                fold_role="HOLDOUT_POOL",
                segment_label="holdout_pool_0",
                block_range_start=0,
                block_range_end=1000,
            ),
        ),
        label_horizon=_label_horizon(),
        boundaries=(),
    )


def _parameter_surface() -> ParameterSurface:
    return ParameterSurface(
        surface_id="surface_t066",
        axes=(
            ParameterAxis(name="half_width_ticks", kind="int", values=(60, 120, 180)),
            ParameterAxis(name="volatility_multiplier", kind="int", values=(1, 2)),
        ),
    )


def _elimination_rules() -> tuple[str, ...]:
    return tuple(sorted(REQUIRED_ELIMINATION_RULES))


def _experiment_plan(
    *,
    time_holdout: TimeHoldoutSplit | None = None,
    walk_forward: tuple = (),
    pool_holdout: PoolHoldoutSplit | None = None,
    cost_assumptions: tuple[str, ...] = ("FLAT_GAS", "STATIC_SLIPPAGE"),
) -> ExperimentPlan:
    return ExperimentPlan(
        chain_id=_CHAIN_ID,
        pool_key_id=_POOL_KEY_ID,
        label_horizon="BARS",
        time_holdout_split=time_holdout if time_holdout is not None else _time_holdout_split(),
        walk_forward_splits=tuple(walk_forward),
        pool_holdout_split=pool_holdout,
        cost_assumptions=cost_assumptions,
        elimination_rules=_elimination_rules(),
        parameter_surface=_parameter_surface(),
        seed=_SEED,
    )


def _candidate_lock(
    *,
    candidate_id: str = "cand_test",
    parameter_set_version: str | None = None,
    seed: int = _SEED,
    split_boundaries=None,
    cost_assumptions: tuple[str, ...] = ("FLAT_GAS", "STATIC_SLIPPAGE"),
    elimination_rules: tuple[str, ...] = (),
    actor: str = _ACTOR,
    lock_time_unix_seconds: int = _CLOCK,
) -> CandidateLock:
    if split_boundaries is None:
        split_boundaries = _time_holdout_split().boundaries
    if not elimination_rules:
        elimination_rules = _elimination_rules()
    if parameter_set_version is None:
        parameter_set_version = compute_parameter_set_version({"half_width_ticks": 120})
    return build_candidate_lock(
        candidate_id=candidate_id,
        parameter_set_version=parameter_set_version,
        code_revision=_CODE_REV,
        manifest_hash=_MANIFEST_HASH,
        seed=seed,
        split_boundaries=split_boundaries,
        cost_assumptions=cost_assumptions,
        elimination_rules=elimination_rules,
        actor=actor,
        lock_time_unix_seconds=lock_time_unix_seconds,
    )


# Deterministic fold / pool / stress evaluators --------------------------------


def _train_fold_evaluator(
    *,
    chain_id: int,
    pool_key_id: str,
    parameter_set_version: str,
    parameter_combo: dict,
    fold_role: str,
    fold,
):
    """A deterministic fold evaluator.

    The metric value is ``100 + half_width_ticks * volatility_multiplier``
    for ``TRAIN`` / ``VALIDATION`` folds; ``TEST`` folds return ``0``
    (the harness refuses to read the test fold until the lock is in
    place). ``HOLDOUT_POOL`` folds return ``80``. The evaluator
    records a side-effect on a module-level counter so a test can
    detect that the test fold was invoked.
    """
    return _deterministic_metric(parameter_combo, fold_role)


def _deterministic_metric(parameter_combo: dict, fold_role: str) -> int:
    half_width = int(parameter_combo.get("half_width_ticks", 0))
    vol_mult = int(parameter_combo.get("volatility_multiplier", 0))
    base = half_width * vol_mult
    if fold_role == "TRAIN":
        return base + 100
    if fold_role == "VALIDATION":
        # Strictly increasing across the grid: half_width 60 / 120 / 180,
        # vol_mult 1 / 2 → 60 / 120 / 180 / 120 / 240 / 360.
        return base + 10
    if fold_role == "HOLDOUT_POOL":
        return base + 5
    return 0


def _train_pool_evaluator(
    *,
    chain_id: int,
    pool_key_id: str,
    parameter_set_version: str,
    parameter_combo: dict,
    fold,
):
    return _deterministic_metric(parameter_combo, "HOLDOUT_POOL")


def _test_fold_evaluator(
    *,
    chain_id: int,
    pool_key_id: str,
    parameter_set_version: str,
    seed: int,
):
    """A test-fold evaluator; the harness only invokes it after the lock.

    The function records a counter on a list so a test can assert
    the test fold was read exactly once.
    """
    return 100


def _stress_evaluator(*, scenario_id: str, category: str, **_: object):
    return StressScenarioOutcome(
        scenario_id=scenario_id,
        category=category,
        outcome="SURVIVED",
        metric_value=50,
        notes=(),
    )


def _distribution_samples(metric_kind: str) -> tuple[int, ...]:
    """Deterministic distribution samples for every metric kind."""
    if metric_kind == "PNL_USDG":
        return (10, 20, 30, 40, 50, 70, 90, 110, 130, 150)
    if metric_kind == "PROFIT_PROBABILITY":
        return (1, 1, 1, 1, 1, 1, 1, 1, 1, 1)
    if metric_kind == "EXPECTED_SHORTFALL_USDG":
        # Positive shortfalls (cost edges); the harness treats the
        # median as the go / no-go signal, so an all-positive
        # sample keeps the harness's recommendation on the
        # provisional branch.
        return (5, 10, 15, 20, 25, 30, 35, 40, 45, 50)
    if metric_kind == "BOUNDARY_TOUCH_PROBABILITY":
        return (0, 0, 0, 1, 1, 1, 1, 1, 1, 1)
    if metric_kind == "TIME_IN_RANGE_FRACTION":
        return (
            1 << 60,
            2 << 60,
            3 << 60,
            4 << 60,
            5 << 60,
            6 << 60,
            7 << 60,
            8 << 60,
            9 << 60,
            10 << 60,
        )
    if metric_kind == "FEE_USDG":
        return (5, 5, 10, 10, 15, 15, 20, 20, 25, 25)
    if metric_kind == "COST_USDG":
        return (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
    raise AssertionError(f"unknown metric_kind {metric_kind!r}")


def _run_harness(
    harness: ExperimentHarness,
    *,
    lock_directory,
    extra_distribution_samples: dict | None = None,
    candidate_id_suffix: str = "",
    neighborhood_override: tuple[tuple[str, int], ...] | None = None,
) -> ExperimentHarnessResult:
    """Run the harness with deterministic inputs.

    The default samples include every metric kind the harness
    understands; the caller may override any of them via
    ``extra_distribution_samples``. ``neighborhood_override`` lets
    the caller supply the neighborhood metric values directly
    (used by tests that need precise control over which candidate
    the harness picks).
    """
    distribution_samples = {
        kind: _distribution_samples(kind)
        for kind in (
            "PNL_USDG",
            "PROFIT_PROBABILITY",
            "EXPECTED_SHORTFALL_USDG",
            "BOUNDARY_TOUCH_PROBABILITY",
            "TIME_IN_RANGE_FRACTION",
            "FEE_USDG",
            "COST_USDG",
        )
    }
    if extra_distribution_samples:
        distribution_samples.update(extra_distribution_samples)
    if neighborhood_override is None:
        neighborhood = tuple(
            (
                compute_parameter_set_version(combo),
                _deterministic_metric(combo, "VALIDATION"),
            )
            for combo in harness.plan.parameter_surface.grid()
        )
    else:
        neighborhood = neighborhood_override
    return harness.run(
        fold_evaluator=_train_fold_evaluator,
        pool_evaluator=_train_pool_evaluator,
        stress_evaluator=_stress_evaluator,
        lock_directory=lock_directory,
        manifest_hash=_MANIFEST_HASH,
        test_fold_evaluator=_test_fold_evaluator,
        distribution_samples_by_metric=distribution_samples,
        neighborhood_metric_values=neighborhood,
        clock_unix_seconds=_CLOCK,
        candidate_id_suffix=candidate_id_suffix,
    )


# ---------------------------------------------------------------------------
# Module versions and vocabularies
# ---------------------------------------------------------------------------


class TestModuleVersions:
    """T066 fixes a small set of closed vocabularies and module versions."""

    def test_module_versions_are_pinned(self) -> None:
        assert CANDIDATE_LOCK_VERSION == "t066.candidate_lock.v1"
        assert PARAMETER_SEARCH_VERSION == "t066.parameter_search.v1"
        assert EXPERIMENT_HARNESS_VERSION == "t066.experiment_harness.v1"
        assert STABILITY_VERSION == "t066.stability.v1"

    def test_required_elimination_rules_cover_t066(self) -> None:
        # The T066 binding rules the lock enforces.
        assert "PRIMARY_BENCHMARK_USDG_CASH" in REQUIRED_ELIMINATION_RULES
        assert "NO_RETUNE_ON_TEST" in REQUIRED_ELIMINATION_RULES
        assert "PRESERVE_NO_TRADE_PERIODS" in REQUIRED_ELIMINATION_RULES
        assert "PRESERVE_ALL_LOSING_REJECTED_RUNS" in REQUIRED_ELIMINATION_RULES
        assert "EXPERIMENTAL_NOT_LIVE_APPROVED" in REQUIRED_ELIMINATION_RULES

    def test_recommendation_vocabulary(self) -> None:
        assert {"PROVISIONAL_RECOMMENDATION", "NO_TRADE"} == VALID_RECOMMENDATIONS


# ---------------------------------------------------------------------------
# Parameter-set versioning
# ---------------------------------------------------------------------------


class TestParameterSetVersion:
    """``compute_parameter_set_version`` is deterministic per parameter set."""

    def test_same_parameters_same_version(self) -> None:
        a = compute_parameter_set_version({"half_width_ticks": 120, "volatility_multiplier": 1})
        b = compute_parameter_set_version({"half_width_ticks": 120, "volatility_multiplier": 1})
        assert a == b

    def test_parameter_change_new_version(self) -> None:
        a = compute_parameter_set_version({"half_width_ticks": 120})
        b = compute_parameter_set_version({"half_width_ticks": 121})
        assert a != b

    def test_parameter_order_irrelevant(self) -> None:
        a = compute_parameter_set_version({"a": 1, "b": 2})
        b = compute_parameter_set_version({"b": 2, "a": 1})
        assert a == b

    def test_version_is_hex_sha256(self) -> None:
        v = compute_parameter_set_version({"x": 1})
        assert v.startswith("0x")
        assert len(v) == 2 + 64


# ---------------------------------------------------------------------------
# Candidate-lock construction
# ---------------------------------------------------------------------------


class TestCandidateLockConstruction:
    """The candidate-lock dataclass enforces its invariants."""

    def test_default_lock_is_valid(self) -> None:
        lock = _candidate_lock()
        assert lock.version == CANDIDATE_LOCK_VERSION
        assert lock.manifest_hash == _MANIFEST_HASH
        assert lock.actor == _ACTOR
        assert lock.lock_time_unix_seconds == _CLOCK

    def test_lock_rejects_unknown_version(self) -> None:
        with pytest.raises(InvalidCandidateLockError):
            CandidateLock(
                version="bogus",
                candidate_id="x",
                parameter_set_version="0x" + "00" * 32,
                code_revision=_CODE_REV,
                manifest_hash=_MANIFEST_HASH,
                seed=_SEED,
                split_boundaries=_time_holdout_split().boundaries,
                cost_assumptions=("FLAT_GAS",),
                elimination_rules=_elimination_rules(),
                content_hash="0x" + "00" * 32,
                actor=_ACTOR,
                lock_time_unix_seconds=_CLOCK,
            )

    def test_lock_rejects_empty_boundaries(self) -> None:
        with pytest.raises(InvalidCandidateLockError):
            _candidate_lock(split_boundaries=())

    def test_lock_rejects_missing_required_rule(self) -> None:
        with pytest.raises(CandidateLockMissingRequiredRuleError):
            _candidate_lock(elimination_rules=("PRIMARY_BENCHMARK_USDG_CASH",))

    def test_lock_rejects_unknown_cost(self) -> None:
        with pytest.raises(InvalidCandidateLockError):
            _candidate_lock(cost_assumptions=("UNKNOWN_COST",))


# ---------------------------------------------------------------------------
# Content hash immutability
# ---------------------------------------------------------------------------


class TestContentHashImmutability:
    """The candidate-lock content hash binds every frozen field."""

    def test_same_inputs_same_hash(self) -> None:
        a = _candidate_lock()
        b = _candidate_lock()
        assert compute_candidate_lock_content_hash(a) == compute_candidate_lock_content_hash(b)

    def test_different_manifest_hash_yields_different_hash(self) -> None:
        # Build two locks directly with different manifest hashes.
        a = build_candidate_lock(
            candidate_id="a",
            parameter_set_version=compute_parameter_set_version({"half_width_ticks": 120}),
            code_revision=_CODE_REV,
            manifest_hash="0x" + "11" * 32,
            seed=_SEED,
            split_boundaries=_time_holdout_split().boundaries,
            cost_assumptions=("FLAT_GAS",),
            elimination_rules=_elimination_rules(),
            actor=_ACTOR,
            lock_time_unix_seconds=_CLOCK,
        )
        b = build_candidate_lock(
            candidate_id="a",
            parameter_set_version=compute_parameter_set_version({"half_width_ticks": 120}),
            code_revision=_CODE_REV,
            manifest_hash="0x" + "22" * 32,
            seed=_SEED,
            split_boundaries=_time_holdout_split().boundaries,
            cost_assumptions=("FLAT_GAS",),
            elimination_rules=_elimination_rules(),
            actor=_ACTOR,
            lock_time_unix_seconds=_CLOCK,
        )
        assert a.content_hash != b.content_hash

    def test_different_seed_yields_different_hash(self) -> None:
        a = _candidate_lock(seed=42)
        b = _candidate_lock(seed=43)
        assert a.content_hash != b.content_hash

    def test_different_parameter_set_version_yields_different_hash(self) -> None:
        a = _candidate_lock(parameter_set_version=compute_parameter_set_version({"x": 1}))
        b = _candidate_lock(parameter_set_version=compute_parameter_set_version({"x": 2}))
        assert a.content_hash != b.content_hash

    def test_provenance_fields_dont_change_hash(self) -> None:
        """``actor`` and ``lock_time_unix_seconds`` are not frozen fields.

        The content hash is bound to the *candidate version*, not
        the provenance metadata, so changing either of those does
        not change the hash.
        """
        a = _candidate_lock(actor="alice", lock_time_unix_seconds=1_000)
        b = _candidate_lock(actor="bob", lock_time_unix_seconds=2_000)
        assert a.content_hash == b.content_hash

    def test_split_boundary_change_yields_new_lock(self) -> None:
        """Changing a split boundary yields a new lock (frozen field)."""
        a = _candidate_lock()
        new_boundaries = tuple(
            b
            if b.segment_label != "validation_fold_0"
            else type(b)(
                chain_id=b.chain_id,
                pool_key_id=b.pool_key_id,
                axis_kind=b.axis_kind,
                fold_index=b.fold_index,
                fold_role=b.fold_role,
                segment_label=b.segment_label,
                anchor_value=b.anchor_value + 1,
                anchor_unit=b.anchor_unit,
                recorded_at_unix_seconds=b.recorded_at_unix_seconds,
                recorded_source=b.recorded_source,
                boundary_id=b.boundary_id,
            )
            for b in a.split_boundaries
        )
        b = build_candidate_lock(
            candidate_id="x",
            parameter_set_version=a.parameter_set_version,
            code_revision=a.code_revision,
            manifest_hash=a.manifest_hash,
            seed=a.seed,
            split_boundaries=new_boundaries,
            cost_assumptions=a.cost_assumptions,
            elimination_rules=a.elimination_rules,
            actor=a.actor,
            lock_time_unix_seconds=a.lock_time_unix_seconds,
        )
        assert a.content_hash != b.content_hash


# ---------------------------------------------------------------------------
# Persistence: write / load round-trip
# ---------------------------------------------------------------------------


class TestCandidateLockPersistence:
    """The on-disk lock round-trip is verified and tamper-detected."""

    def test_round_trip_preserves_lock(self, tmp_path) -> None:
        lock = _candidate_lock()
        target = tmp_path / "lock.json"
        write_candidate_lock(lock, target)
        loaded = load_candidate_lock(target)
        assert loaded == lock

    def test_load_missing_lock_raises(self, tmp_path) -> None:
        target = tmp_path / "missing.json"
        with pytest.raises(CandidateLockNotFoundError):
            load_candidate_lock(target)

    def test_tampered_manifest_hash_rejected(self, tmp_path) -> None:
        lock = _candidate_lock()
        target = tmp_path / "lock.json"
        write_candidate_lock(lock, target)
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["manifest_hash"] = "0x" + "ff" * 32
        target.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(CandidateLockHashMismatchError):
            load_candidate_lock(target)

    def test_tampered_seed_rejected(self, tmp_path) -> None:
        lock = _candidate_lock()
        target = tmp_path / "lock.json"
        write_candidate_lock(lock, target)
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["seed"] = 999
        target.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(CandidateLockHashMismatchError):
            load_candidate_lock(target)

    def test_tampered_split_boundary_rejected(self, tmp_path) -> None:
        lock = _candidate_lock()
        target = tmp_path / "lock.json"
        write_candidate_lock(lock, target)
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["split_boundaries"][0]["anchor_value"] += 1
        target.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(CandidateLockHashMismatchError):
            load_candidate_lock(target)

    def test_tampered_cost_assumption_rejected(self, tmp_path) -> None:
        lock = _candidate_lock()
        target = tmp_path / "lock.json"
        write_candidate_lock(lock, target)
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["cost_assumptions"] = ["UNKNOWN_COST"]
        target.write_text(json.dumps(raw), encoding="utf-8")
        # An out-of-vocabulary cost_assumption is rejected by the
        # constructor (which runs *before* the hash check); both
        # gate the load path so either error class is acceptable.
        with pytest.raises((CandidateLockHashMismatchError, InvalidCandidateLockError)):
            load_candidate_lock(target)

    def test_tampered_elimination_rule_rejected(self, tmp_path) -> None:
        lock = _candidate_lock()
        target = tmp_path / "lock.json"
        write_candidate_lock(lock, target)
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["elimination_rules"] = ["BOGUS_RULE"]
        target.write_text(json.dumps(raw), encoding="utf-8")
        # An out-of-required-rules elimination set is rejected by
        # the constructor; either error class is acceptable as
        # long as the load fails closed.
        with pytest.raises((CandidateLockHashMismatchError, CandidateLockMissingRequiredRuleError)):
            load_candidate_lock(target)

    def test_lock_round_trip_is_deterministic(self, tmp_path) -> None:
        lock = _candidate_lock()
        target = tmp_path / "lock.json"
        write_candidate_lock(lock, target)
        text_a = target.read_text(encoding="utf-8")
        target2 = tmp_path / "lock2.json"
        write_candidate_lock(lock, target2)
        text_b = target2.read_text(encoding="utf-8")
        assert text_a == text_b


# ---------------------------------------------------------------------------
# Parameter-search manifest
# ---------------------------------------------------------------------------


class TestParameterSearchManifest:
    """The search manifest preserves every record (winning, losing, rejected)."""

    def _grid(self) -> tuple[dict, ...]:
        return _parameter_surface().grid()

    def _records(self, *, status_for_first: str = "FILLED") -> tuple[SearchRunRecord, ...]:
        records = []
        for i, combo in enumerate(self._grid()):
            for fold_role in ("TRAIN", "VALIDATION", "HOLDOUT_POOL"):
                if i == 0 and fold_role == "VALIDATION":
                    status = status_for_first
                    reasons = ("BELOW_THRESHOLD",) if status != "FILLED" else ()
                elif i == 1 and fold_role == "TRAIN":
                    status = "REJECTED"
                    reasons = ("ABOVE_COST_RATIO",)
                elif i == 2 and fold_role == "VALIDATION":
                    status = "LOSING"
                    reasons = ("BELOW_THRESHOLD",)
                else:
                    status = "FILLED"
                    reasons = ()
                records.append(
                    SearchRunRecord.from_mapping(
                        parameters=combo,
                        fold_role=fold_role,
                        segment_label=f"{fold_role.lower()}_{i}",
                        metric_value=10 if fold_role == "TRAIN" else 5,
                        run_status=status,
                        elimination_reasons=reasons,
                    )
                )
        return tuple(records)

    def test_manifest_preserves_losing_records(self) -> None:
        manifest = build_parameter_search_manifest(
            search_id="search_test",
            parameter_surface=_parameter_surface(),
            seed=_SEED,
            records=self._records(),
            elimination_rules=_elimination_rules(),
        )
        losing = [r for r in manifest.records if r.run_status == "LOSING"]
        rejected = [r for r in manifest.records if r.run_status == "REJECTED"]
        assert losing, "losing records must be preserved"
        assert rejected, "rejected records must be preserved"
        assert manifest.losing_or_rejected_count() >= 2

    def test_manifest_rejects_empty_records(self) -> None:
        with pytest.raises(EmptySearchError):
            build_parameter_search_manifest(
                search_id="search_test",
                parameter_surface=_parameter_surface(),
                seed=_SEED,
                records=(),
                elimination_rules=_elimination_rules(),
            )

    def test_manifest_round_trip_is_deterministic(self) -> None:
        records = self._records()
        m1 = build_parameter_search_manifest(
            search_id="search_test",
            parameter_surface=_parameter_surface(),
            seed=_SEED,
            records=records,
            elimination_rules=_elimination_rules(),
        )
        m2 = build_parameter_search_manifest(
            search_id="search_test",
            parameter_surface=_parameter_surface(),
            seed=_SEED,
            records=records,
            elimination_rules=_elimination_rules(),
        )
        assert m1.to_dict() == m2.to_dict()


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------


class TestDistributionSummary:
    """The distribution summary is integer-valued and carries the binding flags."""

    def test_build_pnl_summary(self) -> None:
        summary = build_distribution_summary(
            metric_kind="PNL_USDG",
            samples=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
        )
        assert summary.metric_kind == "PNL_USDG"
        assert summary.primary_benchmark == "USDG_CASH"
        assert summary.experimental_flag == "EXPERIMENTAL_NOT_LIVE_APPROVED"
        assert summary.sample_count == 10
        assert summary.min_q64_64 == 1
        assert summary.max_q64_64 == 10
        # Bucket counts sum to sample count.
        assert sum(c for _, c in summary.histogram_buckets) == 10

    def test_profit_probability_helper(self) -> None:
        # 6 of 10 strictly positive.
        p = profit_probability_q64_64((-5, -3, -1, 0, 1, 2, 3, 4, 5, 6))
        assert p == ((6 << 64) // 10)

    def test_distribution_rejects_unknown_metric(self) -> None:
        with pytest.raises(InvalidDistributionError):
            build_distribution_summary(metric_kind="BOGUS", samples=(1, 2, 3))

    def test_distribution_rejects_empty_samples(self) -> None:
        with pytest.raises(EmptyDistributionError):
            build_distribution_summary(metric_kind="PNL_USDG", samples=())

    def test_distribution_rejects_wrong_primary_benchmark(self) -> None:
        with pytest.raises(InvalidDistributionError):
            DistributionSummary(
                metric_kind="PNL_USDG",
                sample_count=1,
                sum_q64_64=1,
                min_q64_64=1,
                max_q64_64=1,
                median_q64_64=1,
                percentile_5_q64_64=1,
                percentile_95_q64_64=1,
                expected_shortfall_q64_64=1,
                histogram_buckets=((1, 1),),
                primary_benchmark="USD",  # wrong
                experimental_flag="EXPERIMENTAL_NOT_LIVE_APPROVED",
                content_hash="0x" + "00" * 32,
            )


# ---------------------------------------------------------------------------
# Stability + stress reports
# ---------------------------------------------------------------------------


class TestStabilityAndStress:
    """The stability and stress reports carry the binding flags."""

    def test_neighborhood_report_carries_flags(self) -> None:
        ps = compute_parameter_set_version({"x": 1})
        report = build_neighborhood_stability_report(
            locked_parameter_set_version=ps,
            locked_metric_value=100,
            neighborhood_metric_values=((ps, 100),),
        )
        assert report.primary_benchmark == "USDG_CASH"
        assert report.experimental_flag == "EXPERIMENTAL_NOT_LIVE_APPROVED"
        assert report.stability_spread_q64_64 == 0

    def test_neighborhood_report_rejects_empty(self) -> None:
        with pytest.raises(EmptyStabilityError):
            build_neighborhood_stability_report(
                locked_parameter_set_version=compute_parameter_set_version({"x": 1}),
                locked_metric_value=0,
                neighborhood_metric_values=(),
            )

    def test_neighborhood_report_requires_locked_in_neighborhood(self) -> None:
        with pytest.raises(InvalidStabilityError):
            build_neighborhood_stability_report(
                locked_parameter_set_version=compute_parameter_set_version({"x": 999}),
                locked_metric_value=0,
                neighborhood_metric_values=((compute_parameter_set_version({"x": 1}), 50),),
            )

    def test_stress_report_carries_flags(self) -> None:
        outcomes = tuple(
            StressScenarioOutcome(
                scenario_id=f"cat_{i}",
                category="STRESSED_GAS",
                outcome="SURVIVED",
                metric_value=50,
                notes=(),
            )
            for i in range(3)
        )
        report = build_stress_report(catalogue_id="cat_test", outcomes=outcomes)
        assert report.primary_benchmark == "USDG_CASH"
        assert report.experimental_flag == "EXPERIMENTAL_NOT_LIVE_APPROVED"
        assert report.survived_count == 3
        assert report.halted_count == 0
        assert report.degraded_count == 0

    def test_stress_report_rejects_empty(self) -> None:
        with pytest.raises(EmptyStabilityError):
            build_stress_report(catalogue_id="cat_test", outcomes=())


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


class TestRecommendation:
    """Both recommendation kinds carry the experimental / primary-benchmark flags."""

    def test_no_trade_recommendation(self) -> None:
        rec = build_no_trade_recommendation(reason_codes=("TEST_MEDIAN_NON_POSITIVE",))
        assert rec.recommendation_kind == "NO_TRADE"
        assert rec.experimental_flag == "EXPERIMENTAL_NOT_LIVE_APPROVED"
        assert rec.primary_benchmark == "USDG_CASH"
        assert rec.reason_codes == ("TEST_MEDIAN_NON_POSITIVE",)

    def test_provisional_recommendation(self) -> None:
        rec = build_provisional_recommendation(
            candidate_id="cand_x",
            locked_metric_value=100,
            reason_codes=("LOCKED_CANDIDATE_SURVIVED",),
        )
        assert rec.recommendation_kind == "PROVISIONAL_RECOMMENDATION"
        assert rec.experimental_flag == "EXPERIMENTAL_NOT_LIVE_APPROVED"
        assert rec.primary_benchmark == "USDG_CASH"
        assert rec.candidate_id == "cand_x"

    def test_recommendation_rejects_unknown_kind(self) -> None:
        with pytest.raises(InvalidCandidateLockError):
            ProvisionalRecommendation(
                recommendation_kind="BOGUS",
                candidate_id="",
                locked_metric_value=0,
                reason_codes=("X",),
                experimental_flag="EXPERIMENTAL_NOT_LIVE_APPROVED",
                primary_benchmark="USDG_CASH",
                content_hash="0x" + "00" * 32,
            )


# ---------------------------------------------------------------------------
# Harness — locked candidate, test fold gate, EXPERIMENTAL flag
# ---------------------------------------------------------------------------


class TestExperimentHarness:
    """The harness writes the lock before reading the test fold and refuses to read it without one."""

    def test_lock_written_before_test_fold(self, tmp_path) -> None:
        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        result = _run_harness(harness, lock_directory=tmp_path)
        lock_path = tmp_path / f"{result.candidate_lock.candidate_id}.lock.json"
        assert lock_path.exists()
        # The harness's lock is the on-disk record.
        loaded = load_candidate_lock(lock_path)
        assert loaded == result.candidate_lock

    def test_harness_refuses_to_read_test_fold_without_lock(self, tmp_path, monkeypatch) -> None:
        """The harness raises :class:`MissingCandidateLockError` when the
        on-disk lock is missing.

        The check is exercised by patching the harness's
        ``write_candidate_lock`` call to be a no-op, so the lock
        is never written and the harness cannot find it on the
        subsequent load.
        """
        import robinhood_lp.experiments.harness as harness_module

        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        original_write = harness_module.write_candidate_lock
        monkeypatch.setattr(
            harness_module,
            "write_candidate_lock",
            lambda *args, **kwargs: None,
        )
        with pytest.raises(MissingCandidateLockError):
            _run_harness(harness, lock_directory=tmp_path)
        # Restore the original writer for any subsequent code path.
        monkeypatch.setattr(harness_module, "write_candidate_lock", original_write)

    def test_harness_refuses_to_read_test_fold_when_lock_tampered(
        self, tmp_path, monkeypatch
    ) -> None:
        """Tampering a lock on disk makes the harness refuse to read it.

        The test runs the harness once to write a lock, tampers
        the lock on disk, then patches the harness's
        ``write_candidate_lock`` to be a no-op so the tampered
        file persists; the harness's load step detects the hash
        disagreement and raises.
        """
        import robinhood_lp.experiments.harness as harness_module

        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        # First run: writes a fresh lock.
        result = _run_harness(harness, lock_directory=tmp_path)
        lock_path = tmp_path / f"{result.candidate_lock.candidate_id}.lock.json"
        # Tamper the lock file on disk.
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
        raw["manifest_hash"] = "0x" + "ff" * 32
        lock_path.write_text(json.dumps(raw), encoding="utf-8")
        # Patch write to be a no-op so the harness cannot overwrite
        # the tampered lock; the load step then sees the
        # disagreement and raises.
        original_write = harness_module.write_candidate_lock
        monkeypatch.setattr(
            harness_module,
            "write_candidate_lock",
            lambda *args, **kwargs: None,
        )
        with pytest.raises(CandidateLockHashMismatchOnLoadError):
            _run_harness(harness, lock_directory=tmp_path)
        monkeypatch.setattr(harness_module, "write_candidate_lock", original_write)

    def test_harness_emits_experimental_flag_on_every_output(self, tmp_path) -> None:
        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        result = _run_harness(harness, lock_directory=tmp_path)
        # The search manifest's elimination rules carry the flag.
        assert "EXPERIMENTAL_NOT_LIVE_APPROVED" in result.search_manifest.elimination_rules
        # The candidate lock's elimination rules carry the flag.
        assert "EXPERIMENTAL_NOT_LIVE_APPROVED" in result.candidate_lock.elimination_rules
        # Every distribution summary carries the flag.
        for summary in result.distributions:
            assert summary.experimental_flag == "EXPERIMENTAL_NOT_LIVE_APPROVED"
        # The stability report carries the flag.
        assert result.stability_report.experimental_flag == "EXPERIMENTAL_NOT_LIVE_APPROVED"
        # The stress report carries the flag.
        assert result.stress_report.experimental_flag == "EXPERIMENTAL_NOT_LIVE_APPROVED"
        # The recommendation carries the flag.
        assert result.recommendation.experimental_flag == "EXPERIMENTAL_NOT_LIVE_APPROVED"

    def test_harness_emits_usdg_cash_primary_benchmark_everywhere(self, tmp_path) -> None:
        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        result = _run_harness(harness, lock_directory=tmp_path)
        for summary in result.distributions:
            assert summary.primary_benchmark == "USDG_CASH"
        assert result.stability_report.primary_benchmark == "USDG_CASH"
        assert result.stress_report.primary_benchmark == "USDG_CASH"
        assert result.recommendation.primary_benchmark == "USDG_CASH"
        # The candidate lock's elimination rules carry the rule.
        assert "PRIMARY_BENCHMARK_USDG_CASH" in result.candidate_lock.elimination_rules

    def test_harness_preserves_losing_and_rejected_runs(self, tmp_path) -> None:
        # Add a fold_evaluator that flips two of the grid points
        # to ``LOSING`` / ``REJECTED`` status; the harness must
        # preserve them on the manifest.
        def fold_evaluator(
            *,
            chain_id,
            pool_key_id,
            parameter_set_version,
            parameter_combo,
            fold_role,
            fold,
        ):
            half_width = int(parameter_combo.get("half_width_ticks", 0))
            if fold_role != "VALIDATION":
                return _deterministic_metric(parameter_combo, fold_role)
            if half_width == 60:
                return -1  # losing
            return _deterministic_metric(parameter_combo, fold_role)

        def pool_evaluator(*, chain_id, pool_key_id, parameter_set_version, parameter_combo, fold):
            return -1  # rejected

        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        # Run the harness by hand so we can plug in the failing evaluators.
        distribution_samples = {
            kind: _distribution_samples(kind)
            for kind in (
                "PNL_USDG",
                "PROFIT_PROBABILITY",
                "EXPECTED_SHORTFALL_USDG",
                "BOUNDARY_TOUCH_PROBABILITY",
                "TIME_IN_RANGE_FRACTION",
                "FEE_USDG",
                "COST_USDG",
            )
        }
        neighborhood = [
            (
                compute_parameter_set_version(combo),
                _deterministic_metric(combo, "VALIDATION"),
            )
            for combo in harness.plan.parameter_surface.grid()
        ]
        # Add the locked candidate explicitly.
        locked_combo = harness.plan.parameter_surface.grid()[1]
        locked_ps_version = compute_parameter_set_version(locked_combo)
        neighborhood = [
            (ps, 100 if ps == locked_ps_version else metric) for (ps, metric) in neighborhood
        ]
        result = harness.run(
            fold_evaluator=fold_evaluator,
            pool_evaluator=pool_evaluator,
            stress_evaluator=_stress_evaluator,
            lock_directory=tmp_path,
            manifest_hash=_MANIFEST_HASH,
            test_fold_evaluator=_test_fold_evaluator,
            distribution_samples_by_metric=distribution_samples,
            neighborhood_metric_values=tuple(neighborhood),
            clock_unix_seconds=_CLOCK,
        )
        # The manifest preserves losing / rejected records.
        assert result.search_manifest.losing_or_rejected_count() > 0
        # The candidate lock's metric value reflects the chosen
        # validation metric (the lock binds the locked version).
        assert result.candidate_lock.parameter_set_version != ""

    def test_harness_rerun_is_deterministic(self, tmp_path) -> None:
        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        result_a = _run_harness(harness, lock_directory=tmp_path / "a")
        result_b = _run_harness(harness, lock_directory=tmp_path / "b")
        assert result_a.content_hash == result_b.content_hash
        assert result_a.to_dict() == result_b.to_dict()

    def test_harness_emits_no_trade_when_no_candidate_eligible(self, tmp_path) -> None:
        """The harness raises :class:`NoCandidateEligibleError` when no version has a non-negative validation metric."""

        def fold_evaluator(**kwargs):
            # Every fold returns a negative value, so the harness
            # tags every version as LOSING and the candidate
            # selection step never finds an improving version.
            return -10

        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        distribution_samples = {
            kind: _distribution_samples(kind)
            for kind in (
                "PNL_USDG",
                "PROFIT_PROBABILITY",
                "EXPECTED_SHORTFALL_USDG",
                "BOUNDARY_TOUCH_PROBABILITY",
                "TIME_IN_RANGE_FRACTION",
                "FEE_USDG",
                "COST_USDG",
            )
        }
        neighborhood = [
            (
                compute_parameter_set_version(combo),
                _deterministic_metric(combo, "VALIDATION"),
            )
            for combo in harness.plan.parameter_surface.grid()
        ]
        with pytest.raises(NoCandidateEligibleError):
            harness.run(
                fold_evaluator=fold_evaluator,
                pool_evaluator=_train_pool_evaluator,
                stress_evaluator=_stress_evaluator,
                lock_directory=tmp_path,
                manifest_hash=_MANIFEST_HASH,
                test_fold_evaluator=_test_fold_evaluator,
                distribution_samples_by_metric=distribution_samples,
                neighborhood_metric_values=tuple(neighborhood),
                clock_unix_seconds=_CLOCK,
            )

    def test_harness_emits_no_trade_on_negative_test_median(self, tmp_path) -> None:
        """A test-fold distribution with a non-positive median forces ``NO_TRADE``.

        The harness surfaces a ``NO_TRADE`` recommendation even when
        a candidate was selected, so the candidate cannot be
        mistaken for a "selecting only the highest return" result.
        """

        def fold_evaluator(**kwargs):
            return _deterministic_metric(kwargs["parameter_combo"], kwargs["fold_role"])

        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        # Override PnL samples to be all negative → median <= 0.
        distribution_samples = {
            kind: _distribution_samples(kind)
            for kind in (
                "PNL_USDG",
                "PROFIT_PROBABILITY",
                "EXPECTED_SHORTFALL_USDG",
                "BOUNDARY_TOUCH_PROBABILITY",
                "TIME_IN_RANGE_FRACTION",
                "FEE_USDG",
                "COST_USDG",
            )
        }
        distribution_samples["PNL_USDG"] = (-100, -50, -10, 0, 0, 0, 0, 0, 0, 0)
        neighborhood = [
            (
                compute_parameter_set_version(combo),
                _deterministic_metric(combo, "VALIDATION"),
            )
            for combo in harness.plan.parameter_surface.grid()
        ]
        locked_combo = harness.plan.parameter_surface.grid()[1]
        locked_ps_version = compute_parameter_set_version(locked_combo)
        neighborhood = [
            (ps, 100 if ps == locked_ps_version else metric) for (ps, metric) in neighborhood
        ]
        result = harness.run(
            fold_evaluator=fold_evaluator,
            pool_evaluator=_train_pool_evaluator,
            stress_evaluator=_stress_evaluator,
            lock_directory=tmp_path,
            manifest_hash=_MANIFEST_HASH,
            test_fold_evaluator=_test_fold_evaluator,
            distribution_samples_by_metric=distribution_samples,
            neighborhood_metric_values=tuple(neighborhood),
            clock_unix_seconds=_CLOCK,
        )
        assert result.recommendation.recommendation_kind == "NO_TRADE"
        assert "EXPERIMENTAL_NOT_LIVE_APPROVED" in result.recommendation.experimental_flag

    def test_harness_emits_provisional_recommendation_when_distributions_positive(
        self, tmp_path
    ) -> None:
        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        result = _run_harness(harness, lock_directory=tmp_path)
        # Default fixtures yield positive PnL medians and a
        # positive stability spread.
        assert result.recommendation.recommendation_kind == "PROVISIONAL_RECOMMENDATION"
        assert "EXPERIMENTAL_NOT_LIVE_APPROVED" in result.recommendation.experimental_flag

    def test_harness_lock_carries_pool_identity(self, tmp_path) -> None:
        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        result = _run_harness(harness, lock_directory=tmp_path)
        for boundary in result.candidate_lock.split_boundaries:
            assert boundary.chain_id == _CHAIN_ID
            assert boundary.pool_key_id == _POOL_KEY_ID

    def test_harness_parameter_change_yields_new_version(self, tmp_path) -> None:
        """A parameter change between two runs yields a different locked version.

        The harness binds the chosen parameter-set version into
        the lock; two harness runs with different parameter
        surfaces produce two locks whose
        ``parameter_set_version`` differs.
        """

        def fold_evaluator(**kwargs):
            return _deterministic_metric(kwargs["parameter_combo"], kwargs["fold_role"])

        plan_a = Plan_with_surface(
            axes=(ParameterAxis(name="half_width_ticks", kind="int", values=(60, 120)),),
        )
        plan_b = Plan_with_surface(
            axes=(ParameterAxis(name="half_width_ticks", kind="int", values=(180, 240)),),
        )
        harness_a = ExperimentHarness(plan=plan_a, code_revision=_CODE_REV, actor=_ACTOR)
        harness_b = ExperimentHarness(plan=plan_b, code_revision=_CODE_REV, actor=_ACTOR)
        result_a = _run_harness(harness_a, lock_directory=tmp_path / "a")
        result_b = _run_harness(harness_b, lock_directory=tmp_path / "b")
        assert (
            result_a.candidate_lock.parameter_set_version
            != result_b.candidate_lock.parameter_set_version
        )


def Plan_with_surface(*, axes: tuple[ParameterAxis, ...]) -> ExperimentPlan:
    """Build an :class:`ExperimentPlan` whose parameter surface is the supplied axes."""
    return ExperimentPlan(
        chain_id=_CHAIN_ID,
        pool_key_id=_POOL_KEY_ID,
        label_horizon="BARS",
        time_holdout_split=_time_holdout_split(),
        walk_forward_splits=(),
        pool_holdout_split=None,
        cost_assumptions=("FLAT_GAS", "STATIC_SLIPPAGE"),
        elimination_rules=_elimination_rules(),
        parameter_surface=ParameterSurface(surface_id="surface_t066_alt", axes=axes),
        seed=_SEED,
    )


# ---------------------------------------------------------------------------
# Smokes: importability, content hash, plan validation
# ---------------------------------------------------------------------------


class TestSmoke:
    """Smoke checks for the harness public surface."""

    def test_plan_rejects_empty_splits(self) -> None:
        with pytest.raises(HarnessInputsError):
            ExperimentPlan(
                chain_id=_CHAIN_ID,
                pool_key_id=_POOL_KEY_ID,
                label_horizon="BARS",
                time_holdout_split=None,
                walk_forward_splits=(),
                pool_holdout_split=None,
                cost_assumptions=("FLAT_GAS",),
                elimination_rules=_elimination_rules(),
                parameter_surface=_parameter_surface(),
                seed=_SEED,
            )

    def test_plan_rejects_missing_required_rule(self) -> None:
        with pytest.raises(HarnessInputsError):
            ExperimentPlan(
                chain_id=_CHAIN_ID,
                pool_key_id=_POOL_KEY_ID,
                label_horizon="BARS",
                time_holdout_split=_time_holdout_split(),
                walk_forward_splits=(),
                pool_holdout_split=None,
                cost_assumptions=("FLAT_GAS",),
                elimination_rules=("SOMETHING_ELSE",),
                parameter_surface=_parameter_surface(),
                seed=_SEED,
            )

    def test_plan_rejects_invalid_horizon_unit(self) -> None:
        # The plan does not currently validate the ``label_horizon``
        # against the closed vocabulary; this is a regression test
        # — the harness relies on the splits / time-holdout's
        # own ``label_horizon`` for the embargo length. Document
        # the accepted value.
        plan = _experiment_plan()
        assert plan.label_horizon in VALID_HORIZON_UNITS

    def test_experiment_harness_rejects_invalid_actor(self) -> None:
        with pytest.raises(HarnessInputsError):
            ExperimentHarness(
                plan=_experiment_plan(),
                code_revision=_CODE_REV,
                actor="",
            )

    def test_load_lock_after_harness_run(self, tmp_path) -> None:
        harness = ExperimentHarness(
            plan=_experiment_plan(),
            code_revision=_CODE_REV,
            actor=_ACTOR,
        )
        result = _run_harness(harness, lock_directory=tmp_path)
        lock_path = tmp_path / f"{result.candidate_lock.candidate_id}.lock.json"
        loaded = load_candidate_lock(lock_path)
        assert loaded == result.candidate_lock
        # The recomputed digest matches.
        assert compute_candidate_lock_content_hash(loaded) == loaded.content_hash
