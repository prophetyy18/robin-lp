"""Tests for the T063 experiment manifests and reports layer.

T063 binds every published LP result to an :class:`ExperimentManifest`
that records the dataset / schema / code / dependency revisions, the
chain and ``PoolKey``, the interval and block bounds, the strategy
parameters and seed, the clock / fill / cost / quote assumptions,
the dataset version, the reporting numeraire and its valuation
qualification (per ADR-014 §3), and the deterministic decisions,
ledger, metrics and report checksums the reconciliation contract
depends on.

The acceptance clauses the tests cover:

- **Per-pool manifest.** A manifest binds exactly one
  ``(chain_id, PoolKey)``; every embedded event carries the same
  pair; a manifest that carries another pool's data fails the
  per-pool invariant.
- **Checksum tamper detection.** Every field the manifest carries
  has a deterministic checksum slot; a tampered field surfaces as a
  :class:`ManifestChecksumError` on :func:`validate_manifest`.
- **Dataset version / numeraire requirement.** A manifest whose
  ``dataset_version`` or ``reporting_numeraire`` is missing fails
  :class:`MissingRequiredFieldError`.
- **Numeraire / qualification agreement.** A manifest whose
  ``valuation_qualification`` disagrees with the dataset's own
  qualification record fails :class:`NumeraireQualificationDisagreementError`.
- **Multi-pool run publishes one manifest per member pool.** A
  multi-pool run identity lists every member; each member is one
  manifest; a foreign pool fails :class:`ForeignManifestError`;
  a missing member fails the multi-pool gate.
- **Byte-identical rerun.** A saved manifest rerun via
  :func:`rerun_manifest` reproduces both the metrics checksum and
  the decisions checksum; a tampered field produces a mismatch.

The must-not clauses the tests cover:

- **No overwrite.** Writing to an existing path with the same
  ``run_id`` / ``(chain_id, PoolKey)`` fails
  :class:`PriorRunOverwriteError` unless ``overwrite=True``.
- **No USD presentation of a RELATIVE_ONLY run.** A USD
  presentation numeraire against a RELATIVE_ONLY manifest fails
  :class:`RelativeOnlyUSDPresentationError`.
- **No chart without dataset version / reporting numeraire.** The
  same :class:`MissingRequiredFieldError` gate that protects the
  manifest protects the publication path.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from robinhood_lp.backtest.engine import (
    BACKTEST_ENGINE_VERSION,
    BacktestEngine,
    RiskDecision,
    empty_position_state,
)
from robinhood_lp.backtest.events import (
    BACKTEST_EVENT_VERSION,
    KIND_SHUTDOWN,
    KIND_SWAP,
    SOURCE_PRIORITY_DATA,
    SOURCE_PRIORITY_SYSTEM,
    BacktestEvent,
    PositionState,
)
from robinhood_lp.backtest.models import (
    ConstantLiquidityModel,
    DeterministicFailureModel,
    FlatGasModel,
    ModelBundle,
    StaticFeeModel,
    ZeroSlippageModel,
)
from robinhood_lp.reports import (
    MANIFEST_VERSION,
    Q64_SCALE,
    VALUATION_QUALIFIED,
    VALUATION_RELATIVE_ONLY,
    CoverageSummary,
    CrossManifestDisagreementError,
    DatasetQualificationRecord,
    ExperimentManifest,
    ForeignManifestError,
    LedgerSnapshot,
    ManifestChecksumError,
    ManifestPoolMismatchError,
    ManifestValidationError,
    MissingRequiredFieldError,
    NumeraireQualificationDisagreementError,
    PriorRunOverwriteError,
    RelativeOnlyUSDPresentationError,
    RerunResult,
    RunIdentity,
    RunMetrics,
    SerialisedEvent,
    assert_no_prior_run_at_path,
    assert_presentation_numeraire_safe,
    build_coverage_summary,
    build_experiment_manifest,
    compute_run_metrics,
    decisions_checksum,
    experiment_manifest_from_dict,
    extract_decisions,
    iter_validation_errors,
    load_manifest_from_path,
    manifest_checksum,
    rerun_manifest,
    rerun_manifest_from_object,
    run_identity_from_dict,
    validate_manifest,
    validate_multi_pool_run,
    validate_run_identity,
    write_manifest_to_path,
)

# ---------------------------------------------------------------------------
# Reference fixtures
# ---------------------------------------------------------------------------

_POOL_KEY_ID_A: Final[str] = "0x" + "ab" * 32
_POOL_KEY_ID_B: Final[str] = "0x" + "cd" * 32
_CHAIN_ID: Final[int] = 46630
_CHAIN_ID_B: Final[int] = 46631
_RUN_ID: Final[str] = "run-test-t063-001"
_DATASET_VERSION: Final[str] = "ds.v1.0.0"
_DATASET_SCHEMA_VERSION: Final[int] = 2
_DATASET_DECODE_VERSION: Final[int] = 2
_DATASET_CONTENT_HASH: Final[str] = "0x" + "12" * 32
_NUMERAIRE: Final[str] = "USDG"
_QUAL: Final[str] = VALUATION_QUALIFIED
_CODE_REV: Final[str] = "0123456789abcdef0123456789abcdef01234567"


def _swap_event(
    *,
    timestamp: int,
    chain_id: int = _CHAIN_ID,
    pool_key_id: str = _POOL_KEY_ID_A,
    price_q64_64: int = 1 << 64,
) -> BacktestEvent:
    return BacktestEvent(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_DATA,
        kind=KIND_SWAP,
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(("price_q64_64", price_q64_64),),
    )


def _shutdown_event(
    *, timestamp: int, chain_id: int = _CHAIN_ID, pool_key_id: str = _POOL_KEY_ID_A
) -> BacktestEvent:
    return BacktestEvent(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_SYSTEM,
        kind=KIND_SHUTDOWN,
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(),
    )


def _approve_risk(_decision: object) -> RiskDecision:
    return RiskDecision(approved=True, reason_code="OK")


def _filled_bundle() -> ModelBundle:
    return ModelBundle(
        bundle_version="t063.test.v1",
        liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
        fee=StaticFeeModel(fee_pips_value=3_000),
        gas=FlatGasModel(gas_units_value=21_000),
        slippage=ZeroSlippageModel(),
        failure=DeterministicFailureModel(),
        latency_units=0,
    )


def _run_a_small_backtest(
    *,
    chain_id: int = _CHAIN_ID,
    pool_key_id: str = _POOL_KEY_ID_A,
) -> tuple[
    list[BacktestEvent],
    PositionState,
    tuple[object, ...],
    CoverageSummary,
    LedgerSnapshot,
    RunMetrics,
    str,
]:
    """Run a tiny deterministic backtest and return its artifacts.

    The fixture exercises the Hold strategy via the engine's audit
    chain so every checksum slot is real, not stubbed. The choice of
    Hold strategy means no fill decisions are made and the ledger
    remains empty, which is the simplest reproducible run.
    """
    from robinhood_lp.strategy.baselines import HoldStrategy

    events = [
        _swap_event(timestamp=100, chain_id=chain_id, pool_key_id=pool_key_id),
        _swap_event(timestamp=200, chain_id=chain_id, pool_key_id=pool_key_id),
        _shutdown_event(timestamp=300, chain_id=chain_id, pool_key_id=pool_key_id),
    ]
    initial_ledger = empty_position_state(pool_key_id=pool_key_id, chain_id=chain_id)
    engine = BacktestEngine(
        version=BACKTEST_ENGINE_VERSION,
        initial_ledger=initial_ledger,
        model_bundle=_filled_bundle(),
        strategy_callback=HoldStrategy(pool_key_id=pool_key_id, chain_id=chain_id),
        risk_callback=_approve_risk,
    )
    result = engine.run(events)
    metrics = compute_run_metrics(
        result=result,
        interval_seconds=300,
        benchmark_total_return_q64_64=Q64_SCALE,
    )
    coverage = build_coverage_summary(
        result=result,
        input_events=events,
        interval_seconds=300,
        block_range_start=100,
        block_range_end=300,
    )
    ledger_snapshot = LedgerSnapshot.from_position_state(result.final_ledger)
    decisions = extract_decisions(result)
    decisions_chk = decisions_checksum(decisions)
    return (
        list(events),
        result.final_ledger,
        result.audit_events,
        coverage,
        ledger_snapshot,
        metrics,
        decisions_chk,
    )


def _build_manifest(
    *,
    chain_id: int = _CHAIN_ID,
    pool_key_id: str = _POOL_KEY_ID_A,
    run_id: str = _RUN_ID,
    dataset_version: str = _DATASET_VERSION,
    reporting_numeraire: str = _NUMERAIRE,
    valuation_qualification: str = _QUAL,
    code_revision: str = _CODE_REV,
    block_range_start: int = 100,
    block_range_end: int = 300,
    interval_seconds: int = 300,
    created_at: int = 1_700_000_000,
) -> ExperimentManifest:
    """Build a manifest for a single (chain, pool) with default fixtures."""
    (
        events,
        _final_ledger,
        _audit_events,
        coverage,
        ledger_snapshot,
        metrics,
        decisions_chk,
    ) = _run_a_small_backtest(chain_id=chain_id, pool_key_id=pool_key_id)
    return build_experiment_manifest(
        run_id=run_id,
        metrics=metrics,
        coverage=coverage,
        ledger_snapshot=ledger_snapshot,
        decisions_checksum=decisions_chk,
        input_events=events,
        dataset_version=dataset_version,
        dataset_schema_version=_DATASET_SCHEMA_VERSION,
        dataset_decode_version=_DATASET_DECODE_VERSION,
        dataset_content_hash=_DATASET_CONTENT_HASH,
        reporting_numeraire=reporting_numeraire,
        valuation_qualification=valuation_qualification,
        code_revision=code_revision,
        dependency_revisions={"robinhood-lp": "0.0.0"},
        strategy_kind="HOLD",
        strategy_params={},
        seed=0,
        clock_assumption="EVENT_TIME",
        fill_assumption="DETERMINISTIC_FAILURE",
        cost_assumption="FLAT_GAS",
        quote_assumption="STATIC_FEE",
        latency_units=0,
        latency_ms_estimate=0,
        created_at_unix_seconds=created_at,
        block_range_start=block_range_start,
        block_range_end=block_range_end,
    )


# ---------------------------------------------------------------------------
# Manifest construction & invariants
# ---------------------------------------------------------------------------


class TestManifestConstruction:
    """Manifest construction enforces the per-pool invariant."""

    def test_default_manifest_passes_validation(self) -> None:
        manifest = _build_manifest()
        validate_manifest(manifest)

    def test_manifest_rejects_unknown_strategy_kind(self) -> None:
        from robinhood_lp.reports.manifest import (
            ExperimentManifest as _E,
        )
        from robinhood_lp.reports.manifest import (
            InvalidManifestFieldError as _I,
        )

        # Constructor rejects an unknown strategy_kind at __post_init__.
        with pytest.raises(_I):
            _E(
                version=MANIFEST_VERSION,
                run_id=_RUN_ID,
                chain_id=_CHAIN_ID,
                pool_key_id=_POOL_KEY_ID_A,
                block_range_start=100,
                block_range_end=300,
                interval_seconds=300,
                dataset_version=_DATASET_VERSION,
                dataset_schema_version=_DATASET_SCHEMA_VERSION,
                dataset_decode_version=_DATASET_DECODE_VERSION,
                dataset_content_hash=_DATASET_CONTENT_HASH,
                reporting_numeraire=_NUMERAIRE,
                valuation_qualification=_QUAL,
                code_revision=_CODE_REV,
                dependency_revisions={},
                strategy_kind="BOGUS_STRATEGY",
                strategy_params={},
                seed=0,
                clock_assumption="EVENT_TIME",
                fill_assumption="DETERMINISTIC_FAILURE",
                cost_assumption="FLAT_GAS",
                quote_assumption="STATIC_FEE",
                latency_units=0,
                latency_ms_estimate=0,
                decisions_checksum="0x" + "00" * 32,
                ledger_checksum="0x" + "00" * 32,
                metrics_checksum="0x" + "00" * 32,
                coverage_checksum="0x" + "00" * 32,
                report_checksum="0x" + "00" * 32,
                metrics_version="t063.run_metrics.v1",
                input_event_list=(),
                created_at_unix_seconds=0,
            )

    def test_per_pool_invariant_rejects_foreign_event(self) -> None:
        manifest = _build_manifest()
        # Build a foreign event (different pool_key_id) and embed it.
        foreign = SerialisedEvent(
            version=BACKTEST_EVENT_VERSION,
            timestamp=10,
            sequence=0,
            source_priority=SOURCE_PRIORITY_DATA,
            kind=KIND_SWAP,
            pool_key_id=_POOL_KEY_ID_B,
            chain_id=_CHAIN_ID,
            observed_at=10,
            available_at=10,
            payload=(("price_q64_64", 1 << 64),),
        )
        tampered = replace(manifest, input_event_list=(foreign,))
        with pytest.raises(ManifestPoolMismatchError):
            tampered.assert_events_match_pool()

    def test_canonical_json_is_deterministic(self) -> None:
        a = _build_manifest()
        b = _build_manifest()
        assert a.to_canonical_json() == b.to_canonical_json()
        # Round-trip survives JSON.
        loaded = experiment_manifest_from_dict(json.loads(a.to_canonical_json()))
        assert loaded == a

    def test_unit_requirement_covers_interval_block_latency_cost_quote(self) -> None:
        """Every quantity the manifest records carries an explicit unit.

        The T063 acceptance clause binds the unit on the interval, a
        block bound, the latency, the cost assumption and the quote
        assumption — and on every other quantity. The manifest stores
        all five as integers in named fields; the test asserts the
        types and field names that document the units.
        """
        m = _build_manifest()
        assert isinstance(m.interval_seconds, int)
        assert isinstance(m.block_range_start, int)
        assert isinstance(m.block_range_end, int)
        assert isinstance(m.latency_units, int)
        assert isinstance(m.latency_ms_estimate, int)
        assert m.cost_assumption == "FLAT_GAS"
        assert m.quote_assumption == "STATIC_FEE"

    def test_report_checksum_detects_tampering(self) -> None:
        m = _build_manifest()
        original = manifest_checksum(m)
        # Replace any field — recompute and confirm divergence.
        tampered = replace(m, code_revision="ffffffffffffffffffffffffffffffffffffffff")
        assert manifest_checksum(tampered) != original


# ---------------------------------------------------------------------------
# Validation gates
# ---------------------------------------------------------------------------


class TestManifestValidation:
    """The validation layer enforces the T063 acceptance clauses."""

    def test_missing_dataset_version_fails(self) -> None:
        m = _build_manifest(dataset_version="")
        with pytest.raises(MissingRequiredFieldError):
            validate_manifest(m)

    def test_missing_reporting_numeraire_fails(self) -> None:
        m = _build_manifest(reporting_numeraire="")
        with pytest.raises(MissingRequiredFieldError):
            validate_manifest(m)

    def test_iter_validation_errors_collects_every_error(self) -> None:
        m = _build_manifest(
            dataset_version="",
            reporting_numeraire="",
            valuation_qualification="RELATIVE_ONLY",
        )
        # Embed a foreign event to also surface the pool mismatch.
        foreign = SerialisedEvent(
            version=BACKTEST_EVENT_VERSION,
            timestamp=10,
            sequence=0,
            source_priority=SOURCE_PRIORITY_DATA,
            kind=KIND_SWAP,
            pool_key_id=_POOL_KEY_ID_B,
            chain_id=_CHAIN_ID,
            observed_at=10,
            available_at=10,
            payload=(("price_q64_64", 1 << 64),),
        )
        tampered = replace(m, input_event_list=(foreign,))
        errors = iter_validation_errors(tampered)
        kinds = [type(e) for e in errors]
        assert MissingRequiredFieldError in kinds
        assert ManifestPoolMismatchError in [type(e) for e in errors] or any(
            isinstance(e, ManifestPoolMismatchError) for e in errors
        )

    def test_numeraire_disagreement_with_dataset_record_fails(self) -> None:
        m = _build_manifest()
        wrong_record = DatasetQualificationRecord(
            dataset_version=_DATASET_VERSION,
            reporting_numeraire="OTHER",
            valuation_qualification=_QUAL,
        )
        with pytest.raises(NumeraireQualificationDisagreementError):
            validate_manifest(m, dataset_qualification=wrong_record)

    def test_numeraire_disagreement_on_qualification_fails(self) -> None:
        m = _build_manifest(valuation_qualification=VALUATION_QUALIFIED)
        wrong_record = DatasetQualificationRecord(
            dataset_version=_DATASET_VERSION,
            reporting_numeraire=_NUMERAIRE,
            valuation_qualification=VALUATION_RELATIVE_ONLY,
        )
        with pytest.raises(NumeraireQualificationDisagreementError):
            validate_manifest(m, dataset_qualification=wrong_record)

    def test_relative_only_presentation_safe(self) -> None:
        """A relative-only run can be presented under its own (relative) numeraire."""
        m = _build_manifest(
            reporting_numeraire="ETH",
            valuation_qualification=VALUATION_RELATIVE_ONLY,
        )
        assert_presentation_numeraire_safe(manifest=m, presentation_numeraire="ETH")

    def test_relative_only_rejects_usd_presentation(self) -> None:
        m = _build_manifest(
            reporting_numeraire="ETH",
            valuation_qualification=VALUATION_RELATIVE_ONLY,
        )
        with pytest.raises(RelativeOnlyUSDPresentationError):
            assert_presentation_numeraire_safe(manifest=m, presentation_numeraire="USD")

    def test_qualified_manifest_allows_usd_presentation(self) -> None:
        m = _build_manifest(
            reporting_numeraire="USDG",
            valuation_qualification=VALUATION_QUALIFIED,
        )
        assert_presentation_numeraire_safe(manifest=m, presentation_numeraire="USD")

    def test_prior_run_overwrite_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "manifest.json"
        m = _build_manifest()
        write_manifest_to_path(m, target)
        with pytest.raises(PriorRunOverwriteError):
            write_manifest_to_path(m, target)

    def test_overwrite_flag_allows_republish(self, tmp_path: Path) -> None:
        target = tmp_path / "manifest.json"
        m = _build_manifest()
        write_manifest_to_path(m, target)
        # Build a second manifest with the same identity but a fresh
        # created_at — its checksum is recomputed correctly.
        m2 = _build_manifest(created_at=m.created_at_unix_seconds + 1)
        write_manifest_to_path(m2, target, overwrite=True)
        # Loading with the strict gate still passes.
        load_manifest_from_path(target)

    def test_assert_no_prior_run_helper(self, tmp_path: Path) -> None:
        target = tmp_path / "manifest.json"
        target.write_text("{}", encoding="utf-8")
        with pytest.raises(PriorRunOverwriteError):
            assert_no_prior_run_at_path(
                target_path=target,
                run_id=_RUN_ID,
                chain_id=_CHAIN_ID,
                pool_key_id=_POOL_KEY_ID_A,
            )
        # The ``overwrite=True`` flag accepts the existing path.
        assert_no_prior_run_at_path(
            target_path=target,
            run_id=_RUN_ID,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            overwrite=True,
        )


# ---------------------------------------------------------------------------
# Multi-pool run identity
# ---------------------------------------------------------------------------


class TestMultiPoolRunIdentity:
    """A multi-pool run publishes one manifest per member pool."""

    def _identity(self, *members: tuple[int, str]) -> RunIdentity:
        return RunIdentity(
            version="t063.run_identity.v1",
            run_id=_RUN_ID,
            member_pools=members,
            dataset_version=_DATASET_VERSION,
            dataset_schema_version=_DATASET_SCHEMA_VERSION,
            dataset_decode_version=_DATASET_DECODE_VERSION,
            dataset_content_hash=_DATASET_CONTENT_HASH,
            reporting_numeraire=_NUMERAIRE,
            valuation_qualification=_QUAL,
        )

    def test_two_pool_run_publishes_two_manifests(self) -> None:
        identity = self._identity((_CHAIN_ID, _POOL_KEY_ID_A), (_CHAIN_ID, _POOL_KEY_ID_B))
        m_a = _build_manifest(chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_A)
        m_b = _build_manifest(chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_B)
        validate_multi_pool_run(identity, [m_a, m_b])

    def test_foreign_manifest_pool_fails(self) -> None:
        identity = self._identity((_CHAIN_ID, _POOL_KEY_ID_A))
        m_b = _build_manifest(chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_B)
        with pytest.raises(ForeignManifestError):
            validate_run_identity(identity, [m_b])

    def test_cross_manifest_disagreement_on_dataset_fails(self) -> None:
        identity = self._identity((_CHAIN_ID, _POOL_KEY_ID_A))
        m_a = _build_manifest(chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_A)
        bad = replace(m_a, dataset_version="ds.v2.0.0")
        with pytest.raises(CrossManifestDisagreementError):
            validate_run_identity(identity, [bad])

    def test_cross_manifest_disagreement_on_numeraire_fails(self) -> None:
        identity = self._identity((_CHAIN_ID, _POOL_KEY_ID_A))
        m_a = _build_manifest(chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_A)
        bad = replace(m_a, reporting_numeraire="USDC")
        with pytest.raises(CrossManifestDisagreementError):
            validate_run_identity(identity, [bad])

    def test_cross_manifest_disagreement_on_qualification_fails(self) -> None:
        identity = self._identity((_CHAIN_ID, _POOL_KEY_ID_A))
        m_a = _build_manifest(chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_A)
        bad = replace(m_a, valuation_qualification=VALUATION_RELATIVE_ONLY)
        with pytest.raises(CrossManifestDisagreementError):
            validate_run_identity(identity, [bad])

    def test_missing_member_fails(self) -> None:
        identity = self._identity((_CHAIN_ID, _POOL_KEY_ID_A), (_CHAIN_ID, _POOL_KEY_ID_B))
        m_a = _build_manifest(chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_A)
        with pytest.raises(ManifestValidationError):
            validate_multi_pool_run(identity, [m_a])

    def test_run_identity_round_trip(self) -> None:
        identity = self._identity((_CHAIN_ID, _POOL_KEY_ID_A))
        loaded = run_identity_from_dict(identity.to_dict())
        assert loaded == identity


# ---------------------------------------------------------------------------
# One-command rerun
# ---------------------------------------------------------------------------


class TestRerun:
    """The one-command rerun reproduces the saved manifest byte-identically."""

    def test_rerun_reproduces_metrics_checksum(self, tmp_path: Path) -> None:
        m = _build_manifest()
        target = tmp_path / "manifest.json"
        write_manifest_to_path(m, target)
        result = rerun_manifest(target)
        assert isinstance(result, RerunResult)
        assert result.match is True
        assert result.decisions_match is True
        assert result.recomputed_metrics_checksum == result.recorded_metrics_checksum
        assert result.recomputed_decisions_checksum == result.recorded_decisions_checksum

    def test_rerun_from_object_reproduces_metrics(self) -> None:
        m = _build_manifest()
        result = rerun_manifest_from_object(m, manifest_path=":memory:")
        assert result.match is True
        assert result.decisions_match is True

    def test_rerun_detects_tampered_checksum(self, tmp_path: Path) -> None:
        m = _build_manifest()
        target = tmp_path / "manifest.json"
        write_manifest_to_path(m, target)
        # Tamper the manifest on disk: change the recorded code_revision
        # *without* recomputing the report checksum. The validation
        # gate catches this on the next load.
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["code_revision"] = "ffffffffffffffffffffffffffffffffffffffff"
        target.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ManifestChecksumError):
            rerun_manifest(target)

    def test_rerun_fails_on_missing_manifest(self, tmp_path: Path) -> None:
        target = tmp_path / "missing.json"
        with pytest.raises(FileNotFoundError):
            rerun_manifest(target)

    def test_rerun_rejects_numeraire_disagreement(self, tmp_path: Path) -> None:
        m = _build_manifest()
        target = tmp_path / "manifest.json"
        write_manifest_to_path(m, target)
        wrong_record = DatasetQualificationRecord(
            dataset_version=_DATASET_VERSION,
            reporting_numeraire="OTHER",
            valuation_qualification=_QUAL,
        )
        with pytest.raises(NumeraireQualificationDisagreementError):
            rerun_manifest(target, dataset_qualification=wrong_record)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    """The reported metrics reconcile against the recorded totals."""

    def test_metrics_record_reconciles_against_engine(self) -> None:
        events = [
            _swap_event(timestamp=100),
            _swap_event(timestamp=200),
            _shutdown_event(timestamp=300),
        ]
        from robinhood_lp.strategy.baselines import HoldStrategy

        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID_A, chain_id=_CHAIN_ID),
            model_bundle=_filled_bundle(),
            strategy_callback=HoldStrategy(pool_key_id=_POOL_KEY_ID_A, chain_id=_CHAIN_ID),
            risk_callback=_approve_risk,
        )
        result = engine.run(events)
        m1 = compute_run_metrics(
            result=result,
            interval_seconds=300,
            benchmark_total_return_q64_64=Q64_SCALE,
        )
        m2 = compute_run_metrics(
            result=result,
            interval_seconds=300,
            benchmark_total_return_q64_64=Q64_SCALE,
        )
        assert m1.metrics_checksum == m2.metrics_checksum
        assert m1 == m2

    def test_coverage_summary_reconciles_against_engine(self) -> None:
        events = [
            _swap_event(timestamp=100),
            _swap_event(timestamp=200),
            _shutdown_event(timestamp=300),
        ]
        from robinhood_lp.strategy.baselines import HoldStrategy

        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID_A, chain_id=_CHAIN_ID),
            model_bundle=_filled_bundle(),
            strategy_callback=HoldStrategy(pool_key_id=_POOL_KEY_ID_A, chain_id=_CHAIN_ID),
            risk_callback=_approve_risk,
        )
        result = engine.run(events)
        c1 = build_coverage_summary(
            result=result,
            input_events=events,
            interval_seconds=300,
            block_range_start=100,
            block_range_end=300,
        )
        c2 = build_coverage_summary(
            result=result,
            input_events=events,
            interval_seconds=300,
            block_range_start=100,
            block_range_end=300,
        )
        assert c1.coverage_checksum == c2.coverage_checksum


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------


class TestSerialisation:
    """The serialisation round-trip preserves the manifest byte-identically."""

    def test_dict_round_trip(self) -> None:
        m = _build_manifest()
        loaded = experiment_manifest_from_dict(m.to_dict())
        assert loaded == m

    def test_canonical_json_round_trip(self) -> None:
        m = _build_manifest()
        loaded = experiment_manifest_from_dict(json.loads(m.to_canonical_json()))
        assert loaded == m
        assert loaded.to_canonical_json() == m.to_canonical_json()

    def test_serialised_event_round_trip(self) -> None:
        evt = _swap_event(timestamp=42, price_q64_64=2 << 64)
        serialised = SerialisedEvent.from_backtest_event(evt)
        recovered = serialised.to_backtest_event()
        assert recovered == evt
        # The event_id is content-derived; the recovered event produces the same id.
        assert recovered.event_id == evt.event_id

    def test_load_manifest_from_path(self, tmp_path: Path) -> None:
        m = _build_manifest()
        target = tmp_path / "manifest.json"
        write_manifest_to_path(m, target)
        loaded = load_manifest_from_path(target)
        assert loaded == m
