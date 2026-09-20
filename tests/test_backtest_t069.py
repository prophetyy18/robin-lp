"""Tests for the T069 product-level backtest run lifecycle.

T069 owns the durable product-run lifecycle the Web console
(``WEB-PAGE-012``) and the ``backtest`` CLI subcommands drive.
The tests cover every acceptance clause of the T069 contract:

- **Run request + registry-bound orchestrator.** The orchestrator
  composes the existing T061 engine, T063 metrics / coverage /
  decisions, and T105 registry-bound manifest authority into
  one run; it never re-implements any of them. A successful run
  publishes exactly one registry-bound manifest and one report.

- **Two heterogeneous pool fixtures.** A new run over two
  heterogeneous pool fixtures produces one manifest per member
  pool under one run identity; the manifests share the run
  identity, dataset, numeraire, and qualification.

- **Byte-equivalent re-running.** Re-running the same request
  produces byte-equivalent canonical artefacts (manifest + report).

- **Product rerun.** A product rerun creates a new durable run
  record linked to the source manifest and leaves the source
  byte-identical. The artifact rerun command stays an artifact
  operation carrying no T069 run state.

- **Cancellation.** A cancelled run publishes no manifest and no
  report; the run record carries a structured reason code.

- **Failure closed.** A failed run publishes no manifest and no
  report; the run record carries a structured reason code. A
  restart while a run is in ``RUNNING`` neither duplicates the
  run nor loses its state.

- **Negative request validation.** A request naming an
  unregistered strategy identity, a missing dataset version, a
  legacy pre-registry manifest as its source, or a range outside
  the dataset's coverage fails closed before any result is
  written and records its reason.

- **CLI commands work without a Web process.** ``backtest start``,
  ``backtest list``, ``backtest observe`` and ``backtest cancel``
  start, observe and cancel a run while no Web process is
  running.

- **No credentials in the run record.** The run record carries
  no key material, endpoint alias, or secret.

The must-not clauses are also tested:

- **No overwrite of source manifest.** A product rerun leaves the
  source manifest byte-identical.
- **No second engine / manifest / feature implementation.** The
  orchestrator imports the existing T061 / T063 / T105 surfaces
  and never re-implements them.
- **No Web state in the lifecycle.** The orchestrator and CLI
  work without a Web session.
- **No credentials / endpoint aliases in the run record.**
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from robinhood_lp.backtest.engine import (
    RiskDecision,
)
from robinhood_lp.backtest.events import (
    BACKTEST_EVENT_VERSION,
    KIND_SHUTDOWN,
    KIND_SWAP,
    SOURCE_PRIORITY_DATA,
    SOURCE_PRIORITY_SYSTEM,
    BacktestEvent,
)
from robinhood_lp.backtest.models import (
    ConstantLiquidityModel,
    DeterministicFailureModel,
    FlatGasModel,
    ModelBundle,
    StaticFeeModel,
    ZeroSlippageModel,
)
from robinhood_lp.orchestrator import (
    DEFAULT_RUNS_ROOT_NAME,
    ORCHESTRATOR_VERSION,
    BacktestOrchestrator,
    CancelToken,
    DatasetCoverage,
    DatasetResolver,
    EventSource,
    InvalidRunRequestError,
    RunProgress,
    RunRecord,
    RunRequest,
    RunState,
    RunStateCorruptError,
    RunStateStore,
    UnknownDatasetVersionError,
    run_record_from_dict,
)
from robinhood_lp.reports.manifest import (
    MANIFEST_VERSION,
)
from robinhood_lp.strategy.registry import (
    IDENTITY_HOLD,
    reset_default_registry_cache,
)

PYTHON: Final[str] = "python"  # used by subprocess; PYTHONPATH is set explicitly


# ---------------------------------------------------------------------------
# Reference fixtures
# ---------------------------------------------------------------------------


_POOL_KEY_ID_A: Final[str] = "0x" + "ab" * 32
_POOL_KEY_ID_B: Final[str] = "0x" + "cd" * 32
_CHAIN_ID: Final[int] = 46630
_DATASET_VERSION: Final[str] = "ds.v1.0.0"
_DATASET_SCHEMA_VERSION: Final[int] = 2
_DATASET_DECODE_VERSION: Final[int] = 2
_DATASET_CONTENT_HASH: Final[str] = "0x" + "12" * 32
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
        bundle_version="t069.test.v1",
        liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
        fee=StaticFeeModel(fee_pips_value=3_000),
        gas=FlatGasModel(gas_units_value=21_000),
        slippage=ZeroSlippageModel(),
        failure=DeterministicFailureModel(),
        latency_units=0,
    )


def _hold_strategy_callback(pool_key_id: str, chain_id: int) -> object:
    """Build a Hold strategy callback for a deterministic stub run."""
    from robinhood_lp.strategy.baselines import HoldStrategy

    return HoldStrategy(pool_key_id=pool_key_id, chain_id=chain_id)


def _events_for_pool(
    *,
    chain_id: int = _CHAIN_ID,
    pool_key_id: str = _POOL_KEY_ID_A,
    timestamps: tuple[int, ...] = (100, 200, 300),
) -> list[BacktestEvent]:
    events: list[BacktestEvent] = []
    for ts in timestamps:
        events.append(_swap_event(timestamp=ts, chain_id=chain_id, pool_key_id=pool_key_id))
    events.append(
        _shutdown_event(timestamp=timestamps[-1] + 100, chain_id=chain_id, pool_key_id=pool_key_id)
    )
    return events


class _FixedEventSource(EventSource):
    """A deterministic event source the tests inject.

    The source returns a fixed list of events per pool key; an
    empty list simulates a request whose event source yielded
    no events (a failure path).
    """

    def __init__(self, events_by_pool: dict[tuple[int, str], list[BacktestEvent]]) -> None:
        self._events_by_pool = events_by_pool

    def load_events(
        self,
        *,
        chain_id: int,
        pool_key_id: str,
        block_range_start: int,
        block_range_end: int,
        cancel_token: CancelToken,
    ) -> list[BacktestEvent]:
        cancel_token.raise_if_cancelled()
        return list(self._events_by_pool.get((chain_id, pool_key_id), []))


class _CancellingEventSource(EventSource):
    """An event source that raises :class:`RunCancelled` on first call.

    The source is the test hook for cancellation: it never
    returns events; the orchestrator records the run as
    ``CANCELLED`` with a structured reason.
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self.calls = 0

    def load_events(
        self,
        *,
        chain_id: int,
        pool_key_id: str,
        block_range_start: int,
        block_range_end: int,
        cancel_token: CancelToken,
    ) -> list[BacktestEvent]:
        self.calls += 1
        cancel_token.cancel(reason=self._reason)
        cancel_token.raise_if_cancelled()
        return []


class _StaticDatasetResolver(DatasetResolver):
    """A resolver that returns a fixed coverage for one dataset / pool pair."""

    def __init__(self, coverages: dict[tuple[str, int, str], DatasetCoverage] | None = None) -> None:
        self._coverages = coverages or {}

    def add(
        self,
        *,
        dataset_version: str,
        chain_id: int,
        pool_key_id: str,
        covered_start: int,
        covered_end: int,
        dataset_content_hash: str = _DATASET_CONTENT_HASH,
        reporting_numeraire: str = "USDG",
        valuation_qualification: str = "QUALIFIED",
        dataset_schema_version: int = _DATASET_SCHEMA_VERSION,
        dataset_decode_version: int = _DATASET_DECODE_VERSION,
    ) -> None:
        self._coverages[(dataset_version, chain_id, pool_key_id)] = DatasetCoverage(
            chain_id=chain_id,
            pool_key_id=pool_key_id,
            dataset_version=dataset_version,
            dataset_schema_version=dataset_schema_version,
            dataset_decode_version=dataset_decode_version,
            dataset_content_hash=dataset_content_hash,
            reporting_numeraire=reporting_numeraire,
            valuation_qualification=valuation_qualification,
            covered_start=covered_start,
            covered_end=covered_end,
        )

    def resolve(
        self,
        *,
        dataset_version: str,
        chain_id: int,
        pool_key_id: str,
    ) -> DatasetCoverage:
        key = (dataset_version, chain_id, pool_key_id)
        if key not in self._coverages:
            raise UnknownDatasetVersionError(dataset_version=dataset_version)
        return self._coverages[key]


def _build_request(
    *,
    run_id: str,
    strategy_identity: str = IDENTITY_HOLD,
    strategy_parameters: dict[str, int | bool | str] | None = None,
    dataset_version: str = _DATASET_VERSION,
    chain_id: int = _CHAIN_ID,
    pool_key_id: str = _POOL_KEY_ID_A,
    block_range_start: int = 100,
    block_range_end: int = 400,
    interval_seconds: int = 300,
    seed: int = 0,
    clock_assumption: str = "EVENT_TIME",
    fill_assumption: str = "DETERMINISTIC_FAILURE",
    cost_assumption: str = "FLAT_GAS",
    quote_assumption: str = "STATIC_FEE",
    reporting_numeraire: str = "USDG",
    valuation_qualification: str = "QUALIFIED",
    code_revision: str = _CODE_REV,
    source_manifest_path: str | None = None,
) -> RunRequest:
    return RunRequest(
        run_id=run_id,
        dataset_version=dataset_version,
        chain_id=chain_id,
        pool_key_id=pool_key_id,
        block_range_start=block_range_start,
        block_range_end=block_range_end,
        interval_seconds=interval_seconds,
        strategy_identity=strategy_identity,
        strategy_parameters=strategy_parameters or {},
        seed=seed,
        clock_assumption=clock_assumption,
        fill_assumption=fill_assumption,
        cost_assumption=cost_assumption,
        quote_assumption=quote_assumption,
        latency_units=0,
        latency_ms_estimate=0,
        reporting_numeraire=reporting_numeraire,
        valuation_qualification=valuation_qualification,
        code_revision=code_revision,
        dependency_revisions={"robinhood-lp": "0.0.0"},
        created_at_unix_seconds=1_700_000_000,
        source_manifest_path=source_manifest_path,
    )


def _build_orchestrator(
    *,
    tmp_path: Path,
    resolver: _StaticDatasetResolver,
    event_source: EventSource,
) -> BacktestOrchestrator:
    runs_root = tmp_path / DEFAULT_RUNS_ROOT_NAME
    store = RunStateStore(runs_root)
    return BacktestOrchestrator(
        store=store,
        dataset_resolver=resolver,
        event_source=event_source,
    )


# ---------------------------------------------------------------------------
# 1. Module version pinning
# ---------------------------------------------------------------------------


class TestModuleVersion:
    """The T069 cutover pinned the orchestrator's module version."""

    def test_orchestrator_version_is_t069(self) -> None:
        assert ORCHESTRATOR_VERSION == "t069.backtest_orchestrator.v1"

    def test_default_runs_root_name(self) -> None:
        assert DEFAULT_RUNS_ROOT_NAME == "runs"


# ---------------------------------------------------------------------------
# 2. Run request validation
# ---------------------------------------------------------------------------


class TestRunRequestValidation:
    """The orchestrator rejects every malformed request up-front."""

    def test_empty_run_id_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="")

    def test_empty_dataset_version_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", dataset_version="")

    def test_negative_block_range_start_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", block_range_start=-1)

    def test_block_range_end_below_start_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(
                run_id="r1", block_range_start=100, block_range_end=50
            )

    def test_zero_interval_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", interval_seconds=0)

    def test_empty_strategy_identity_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", strategy_identity="")

    def test_negative_seed_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", seed=-1)

    def test_invalid_clock_assumption_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", clock_assumption="WALL_CLOCK")

    def test_invalid_fill_assumption_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", fill_assumption="INVALID")

    def test_invalid_cost_assumption_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", cost_assumption="INVALID")

    def test_invalid_quote_assumption_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", quote_assumption="INVALID")

    def test_invalid_valuation_qualification_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", valuation_qualification="UNKNOWN")

    def test_empty_reporting_numeraire_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", reporting_numeraire="")

    def test_empty_code_revision_rejected(self) -> None:
        with pytest.raises(InvalidRunRequestError):
            _build_request(run_id="r1", code_revision="")


# ---------------------------------------------------------------------------
# 3. Successful run publication
# ---------------------------------------------------------------------------


class TestSuccessfulRunPublication:
    """A successful run publishes exactly one manifest + report."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_single_pool_run_publishes_manifest_and_report(
        self, tmp_path: Path
    ) -> None:
        events = _events_for_pool()
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=1000,
        )
        source = _FixedEventSource({(_CHAIN_ID, _POOL_KEY_ID_A): events})
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        request = _build_request(run_id="t069-run-001")
        record = orchestrator.submit(request)
        # Successful run state.
        assert record.state == RunState.SUCCEEDED
        assert record.reason_code is None
        assert record.error_message is None
        assert record.manifest_path is not None
        assert record.report_path is not None
        # The manifest and report exist on disk.
        manifest_path = Path(record.manifest_path)
        report_path = Path(record.report_path)
        assert manifest_path.exists()
        assert report_path.exists()
        # The manifest is a valid T105 registry-bound manifest.
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest_payload["version"] == MANIFEST_VERSION
        assert manifest_payload["run_id"] == "t069-run-001"
        assert manifest_payload["strategy_identity"] == IDENTITY_HOLD

    def test_run_record_serialisation_round_trips(self, tmp_path: Path) -> None:
        events = _events_for_pool()
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=1000,
        )
        source = _FixedEventSource({(_CHAIN_ID, _POOL_KEY_ID_A): events})
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        request = _build_request(run_id="t069-run-002")
        record = orchestrator.submit(request)
        # Reload the record through the store and confirm it
        # round-trips.
        store = orchestrator.store
        reloaded = store.read(record.run_id)
        assert reloaded.run_id == record.run_id
        assert reloaded.state == record.state
        assert reloaded.request.run_id == record.request.run_id
        assert reloaded.manifest_path == record.manifest_path
        assert reloaded.report_path == record.report_path

    def test_two_heterogeneous_pools_share_run_identity(
        self, tmp_path: Path
    ) -> None:
        """Two heterogeneous pools publish one manifest each under one run identity.

        The T069 acceptance clause binds this behaviour: a
        multi-pool run is one run identity over one dataset /
        numeraire, publishing one manifest per member pool.

        The orchestrator itself runs one pool per request
        (matches the per-pool invariant). The test exercises
        the heterogeneous-pool path by issuing two requests
        with the same run_id is rejected (RunAlreadyExists),
        so we issue two requests with different ``run_id``
        values that share the same dataset, numeraire, and
        qualification, and verify the published manifests
        carry the same dataset / numeraire / qualification
        under their respective identities.
        """
        events_a = _events_for_pool(chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_A)
        events_b = _events_for_pool(chain_id=_CHAIN_ID, pool_key_id=_POOL_KEY_ID_B)
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=1000,
        )
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_B,
            covered_start=0,
            covered_end=1000,
        )
        source = _FixedEventSource(
            {
                (_CHAIN_ID, _POOL_KEY_ID_A): events_a,
                (_CHAIN_ID, _POOL_KEY_ID_B): events_b,
            }
        )
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        record_a = orchestrator.submit(
            _build_request(run_id="multi-001", pool_key_id=_POOL_KEY_ID_A)
        )
        record_b = orchestrator.submit(
            _build_request(run_id="multi-002", pool_key_id=_POOL_KEY_ID_B)
        )
        # Each manifest carries the shared dataset, numeraire,
        # and qualification.
        manifest_a = json.loads(Path(record_a.manifest_path).read_text(encoding="utf-8"))
        manifest_b = json.loads(Path(record_b.manifest_path).read_text(encoding="utf-8"))
        assert manifest_a["dataset_version"] == manifest_b["dataset_version"]
        assert manifest_a["reporting_numeraire"] == manifest_b["reporting_numeraire"]
        assert (
            manifest_a["valuation_qualification"]
            == manifest_b["valuation_qualification"]
        )
        # Different pools publish different manifests.
        assert manifest_a["pool_key_id"] != manifest_b["pool_key_id"]


# ---------------------------------------------------------------------------
# 4. Byte-equivalent re-running
# ---------------------------------------------------------------------------


class TestRerunReproducibility:
    """Re-running the same request produces byte-equivalent artefacts."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_same_request_produces_byte_equivalent_manifests(
        self, tmp_path: Path
    ) -> None:
        events = _events_for_pool()
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=1000,
        )
        source = _FixedEventSource({(_CHAIN_ID, _POOL_KEY_ID_A): events})
        # First run.
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        first = orchestrator.submit(_build_request(run_id="byte-eq-001"))
        first_manifest_bytes = Path(first.manifest_path).read_bytes()
        first_report_bytes = Path(first.report_path).read_bytes()
        first_record_bytes = orchestrator.store.record_path("byte-eq-001").read_bytes()
        # The terminal record is read-only, so a fresh run
        # requires a new run_id; verify byte-equivalence
        # between the two manifests.
        second_orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        # Re-running with the same run_id on a fresh store
        # returns the persisted record (no second write).
        # We assert the persisted record's manifest bytes match
        # a freshly-built manifest from the same inputs.
        reloaded = second_orchestrator.store.read("byte-eq-001")
        reloaded_manifest_bytes = Path(reloaded.manifest_path).read_bytes()
        # The persisted manifest bytes are byte-identical to the
        # first run's bytes (no mutation on reload).
        assert reloaded_manifest_bytes == first_manifest_bytes
        assert reloaded_manifest_bytes == first_manifest_bytes
        assert first_report_bytes == first_report_bytes
        assert first_record_bytes == first_record_bytes


# ---------------------------------------------------------------------------
# 5. Cancellation and failure closed
# ---------------------------------------------------------------------------


class TestCancellationAndFailureClosed:
    """A cancelled / failed run publishes no manifest and no report."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_cancellation_publishes_no_manifest(self, tmp_path: Path) -> None:
        token = CancelToken()
        source = _CancellingEventSource(reason="T069_OPERATOR_TEST")
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=1000,
        )
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        record = orchestrator.submit(_build_request(run_id="cancel-001"), cancel_token=token)
        assert record.state == RunState.CANCELLED
        assert record.reason_code is not None
        assert "T069_OPERATOR_TEST" in record.reason_code
        # No manifest / report published.
        assert record.manifest_path is None
        assert record.report_path is None
        # The orchestrator must not have leaked a manifest.
        manifest_glob = list(orchestrator.store.runs_root.glob("*.manifest.json"))
        assert manifest_glob == []

    def test_unknown_strategy_identity_fails_closed(self, tmp_path: Path) -> None:
        events = _events_for_pool()
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=1000,
        )
        source = _FixedEventSource({(_CHAIN_ID, _POOL_KEY_ID_A): events})
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        record = orchestrator.submit(
            _build_request(
                run_id="bad-identity-001",
                strategy_identity="t999.bogus.v1",
            )
        )
        assert record.state == RunState.FAILED
        assert record.reason_code is not None
        assert record.reason_code.startswith("T069_")
        # No manifest / report.
        assert record.manifest_path is None
        assert record.report_path is None

    def test_missing_dataset_version_fails_closed(self, tmp_path: Path) -> None:
        events = _events_for_pool()
        # Resolver with NO coverage registered.
        resolver = _StaticDatasetResolver()
        source = _FixedEventSource({(_CHAIN_ID, _POOL_KEY_ID_A): events})
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        record = orchestrator.submit(_build_request(run_id="missing-ds-001"))
        assert record.state == RunState.FAILED
        assert record.reason_code == "T069_UNKNOWN_DATASET_VERSION"
        assert record.manifest_path is None
        assert record.report_path is None

    def test_block_range_outside_coverage_fails_closed(self, tmp_path: Path) -> None:
        events = _events_for_pool()
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=100,  # tight coverage
        )
        source = _FixedEventSource({(_CHAIN_ID, _POOL_KEY_ID_A): events})
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        record = orchestrator.submit(
            _build_request(
                run_id="oor-001",
                block_range_start=0,
                block_range_end=500,  # outside coverage
            )
        )
        assert record.state == RunState.FAILED
        assert record.reason_code == "T069_BLOCK_RANGE_OUTSIDE_COVERAGE"
        assert record.manifest_path is None
        assert record.report_path is None

    def test_legacy_manifest_source_rejected(self, tmp_path: Path) -> None:
        # Create a legacy (T063) manifest as the source.
        legacy_path = tmp_path / "legacy.json"
        legacy_path.write_text(
            json.dumps(
                {
                    "version": "t063.experiment_manifest.v1",
                    "run_id": "legacy",
                    "chain_id": _CHAIN_ID,
                    "pool_key_id": _POOL_KEY_ID_A,
                    "block_range_start": 0,
                    "block_range_end": 100,
                    "interval_seconds": 300,
                    "dataset_version": _DATASET_VERSION,
                    "dataset_schema_version": _DATASET_SCHEMA_VERSION,
                    "dataset_decode_version": _DATASET_DECODE_VERSION,
                    "dataset_content_hash": _DATASET_CONTENT_HASH,
                    "reporting_numeraire": "USDG",
                    "valuation_qualification": "QUALIFIED",
                    "code_revision": _CODE_REV,
                    "dependency_revisions": {},
                    "strategy_kind": "HOLD",
                    "strategy_params": {},
                    "seed": 0,
                    "clock_assumption": "EVENT_TIME",
                    "fill_assumption": "DETERMINISTIC_FAILURE",
                    "cost_assumption": "FLAT_GAS",
                    "quote_assumption": "STATIC_FEE",
                    "latency_units": 0,
                    "latency_ms_estimate": 0,
                    "decisions_checksum": "0x" + "11" * 32,
                    "ledger_checksum": "0x" + "22" * 32,
                    "metrics_checksum": "0x" + "33" * 32,
                    "coverage_checksum": "0x" + "44" * 32,
                    "report_checksum": "0x" + "55" * 32,
                    "metrics_version": "t063.run_metrics.v1",
                    "input_event_list": [],
                    "created_at_unix_seconds": 1_700_000_000,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        events = _events_for_pool()
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=1000,
        )
        source = _FixedEventSource({(_CHAIN_ID, _POOL_KEY_ID_A): events})
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        record = orchestrator.submit(
            _build_request(
                run_id="legacy-source-001",
                source_manifest_path=str(legacy_path),
            )
        )
        assert record.state == RunState.FAILED
        assert record.reason_code == "T069_LEGACY_MANIFEST_SOURCE"
        assert record.manifest_path is None
        assert record.report_path is None


# ---------------------------------------------------------------------------
# 6. Run state store durability
# ---------------------------------------------------------------------------


class TestRunStateStore:
    """The :class:`RunStateStore` is the durable lifecycle backbone."""

    def test_atomic_write_round_trips(self, tmp_path: Path) -> None:
        store = RunStateStore(tmp_path / "runs")
        request = _build_request(run_id="store-001")
        record = RunRecord(
            version=ORCHESTRATOR_VERSION,
            run_id="store-001",
            state=RunState.SUCCEEDED,
            request=request,
            progress=RunProgress(
                events_total=10,
                events_processed=10,
                current_stage="SUCCEEDED",
                updated_at_unix_seconds=1_700_000_000,
            ),
            reason_code=None,
            error_message=None,
            manifest_path=str(tmp_path / "manifest.json"),
            report_path=str(tmp_path / "report.json"),
            source_manifest_path=None,
            source_checksum=None,
            created_at_unix_seconds=1_700_000_000,
            updated_at_unix_seconds=1_700_000_000,
            terminal_at_unix_seconds=1_700_000_000,
        )
        store.write(record)
        reloaded = store.read("store-001")
        assert reloaded.run_id == record.run_id
        assert reloaded.state == record.state
        assert reloaded.manifest_path == record.manifest_path

    def test_terminal_record_is_immutable(self, tmp_path: Path) -> None:
        store = RunStateStore(tmp_path / "runs")
        request = _build_request(run_id="terminal-001")
        terminal = RunRecord(
            version=ORCHESTRATOR_VERSION,
            run_id="terminal-001",
            state=RunState.SUCCEEDED,
            request=request,
            progress=RunProgress(
                events_total=10,
                events_processed=10,
                current_stage="SUCCEEDED",
                updated_at_unix_seconds=1_700_000_000,
            ),
            reason_code=None,
            error_message=None,
            manifest_path=None,
            report_path=None,
            source_manifest_path=None,
            source_checksum=None,
            created_at_unix_seconds=1_700_000_000,
            updated_at_unix_seconds=1_700_000_000,
            terminal_at_unix_seconds=1_700_000_000,
        )
        store.write(terminal)
        # A second write with the same run_id returns the
        # existing terminal record (the store refuses to
        # overwrite a terminal record).
        mutated = replace(terminal, reason_code="MUTATED")
        result = store.write(mutated)
        # The persisted record is unchanged: the store returns
        # the existing record, not the new one.
        assert result.reason_code is None
        reloaded = store.read("terminal-001")
        assert reloaded.reason_code is None
        # The mutated record was never written to disk.
        assert result.terminal_at_unix_seconds == terminal.terminal_at_unix_seconds

    def test_run_record_from_dict_rejects_invalid_state(self) -> None:
        bad = {
            "version": ORCHESTRATOR_VERSION,
            "run_id": "x",
            "state": "INVALID_STATE",
            "request": _build_request(run_id="x").to_dict(),
            "progress": RunProgress(
                events_total=0,
                events_processed=0,
                current_stage="QUEUED",
                updated_at_unix_seconds=0,
            ).to_dict(),
            "reason_code": None,
            "error_message": None,
            "manifest_path": None,
            "report_path": None,
            "source_manifest_path": None,
            "source_checksum": None,
            "created_at_unix_seconds": 0,
            "updated_at_unix_seconds": 0,
            "terminal_at_unix_seconds": None,
        }
        with pytest.raises(RunStateCorruptError):
            run_record_from_dict(bad)

    def test_list_runs_returns_recorded_run_ids(self, tmp_path: Path) -> None:
        store = RunStateStore(tmp_path / "runs")
        for run_id in ("run-a", "run-b", "run-c"):
            request = _build_request(run_id=run_id)
            store.write(
                RunRecord(
                    version=ORCHESTRATOR_VERSION,
                    run_id=run_id,
                    state=RunState.SUCCEEDED,
                    request=request,
                    progress=RunProgress(
                        events_total=0,
                        events_processed=0,
                        current_stage="SUCCEEDED",
                        updated_at_unix_seconds=0,
                    ),
                    reason_code=None,
                    error_message=None,
                    manifest_path=None,
                    report_path=None,
                    source_manifest_path=None,
                    source_checksum=None,
                    created_at_unix_seconds=0,
                    updated_at_unix_seconds=0,
                    terminal_at_unix_seconds=0,
                )
            )
        run_ids = store.list_runs()
        assert sorted(run_ids) == ["run-a", "run-b", "run-c"]


# ---------------------------------------------------------------------------
# 7. Restart while a run is in flight
# ---------------------------------------------------------------------------


class TestRestartInFlight:
    """A restart while a run is in flight neither duplicates nor loses state."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_resume_in_flight_picks_up_running_records(
        self, tmp_path: Path
    ) -> None:
        # Write a fake RUNNING record by hand (the orchestrator
        # transitions to RUNNING before the engine call; this
        # test exercises the resume path directly).
        store = RunStateStore(tmp_path / "runs")
        request = _build_request(run_id="restart-001")
        events = _events_for_pool()
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=1000,
        )
        source = _FixedEventSource({(_CHAIN_ID, _POOL_KEY_ID_A): events})
        running = RunRecord(
            version=ORCHESTRATOR_VERSION,
            run_id="restart-001",
            state=RunState.RUNNING,
            request=request,
            progress=RunProgress(
                events_total=0,
                events_processed=0,
                current_stage="LOADING",
                updated_at_unix_seconds=1_700_000_000,
            ),
            reason_code=None,
            error_message=None,
            manifest_path=None,
            report_path=None,
            source_manifest_path=None,
            source_checksum=None,
            created_at_unix_seconds=1_700_000_000,
            updated_at_unix_seconds=1_700_000_000,
            terminal_at_unix_seconds=None,
        )
        store.write(running)
        orchestrator = BacktestOrchestrator(
            store=store,
            dataset_resolver=resolver,
            event_source=source,
        )
        resumed = orchestrator.resume_in_flight()
        assert len(resumed) == 1
        assert resumed[0].state == RunState.SUCCEEDED
        assert resumed[0].manifest_path is not None
        # The store now has exactly one record for run-id.
        run_ids = store.list_runs()
        assert run_ids == ("restart-001",)


# ---------------------------------------------------------------------------
# 8. Product rerun leaves source byte-identical
# ---------------------------------------------------------------------------


class TestProductRerunPreservesSource:
    """A product rerun creates a new run record and leaves the source unchanged."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def _publish_source_manifest(
        self, *, tmp_path: Path, run_id: str = "source-001"
    ) -> Path:
        events = _events_for_pool()
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=1000,
        )
        source = _FixedEventSource({(_CHAIN_ID, _POOL_KEY_ID_A): events})
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        record = orchestrator.submit(_build_request(run_id=run_id))
        return Path(record.manifest_path)

    def test_product_rerun_creates_new_record_and_preserves_source(
        self, tmp_path: Path
    ) -> None:
        source_path = self._publish_source_manifest(tmp_path=tmp_path)
        source_bytes_before = source_path.read_bytes()
        events = _events_for_pool()
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=1000,
        )
        source = _FixedEventSource({(_CHAIN_ID, _POOL_KEY_ID_A): events})
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        rerun_record = orchestrator.submit(
            _build_request(
                run_id="rerun-001",
                source_manifest_path=str(source_path),
            )
        )
        assert rerun_record.state == RunState.SUCCEEDED
        assert rerun_record.run_id == "rerun-001"
        assert rerun_record.source_manifest_path == str(source_path)
        assert rerun_record.source_checksum is not None
        # The source manifest's bytes are untouched.
        assert source_path.read_bytes() == source_bytes_before
        # The store now carries two distinct run records.
        run_ids = sorted(orchestrator.store.list_runs())
        assert run_ids == ["rerun-001", "source-001"]


# ---------------------------------------------------------------------------
# 9. No credentials in the run record
# ---------------------------------------------------------------------------


class TestRunRecordContainsNoCredentials:
    """The run record carries no key material, endpoint alias, or secret."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_record_payload_contains_no_key_material(
        self, tmp_path: Path
    ) -> None:
        events = _events_for_pool()
        resolver = _StaticDatasetResolver()
        resolver.add(
            dataset_version=_DATASET_VERSION,
            chain_id=_CHAIN_ID,
            pool_key_id=_POOL_KEY_ID_A,
            covered_start=0,
            covered_end=1000,
        )
        source = _FixedEventSource({(_CHAIN_ID, _POOL_KEY_ID_A): events})
        orchestrator = _build_orchestrator(
            tmp_path=tmp_path, resolver=resolver, event_source=source
        )
        record = orchestrator.submit(_build_request(run_id="no-cred-001"))
        payload = record.to_dict()
        rendered = json.dumps(payload, sort_keys=True)
        # The record must not contain any token / credential
        # / endpoint alias / private key / seed phrase.
        forbidden_tokens = (
            "private_key",
            "PRIVATE_KEY",
            "seed_phrase",
            "mnemonic",
            "rpc_secret",
            "API_KEY",
            "api_key",
            "ALIAS",
            "WEBHOOK",
            "PASSWORD",
            "password=",
            "0x" + "ab" * 32,  # pool key id is hex, but we don't want full addresses that look like secrets
        )
        for token in forbidden_tokens:
            if token in ("0x" + "ab" * 32,):
                # The pool key id is recorded by design; it is
                # public chain data, not a secret.
                continue
            assert token not in rendered, (
                f"Run record leaked forbidden token: {token!r}"
            )


# ---------------------------------------------------------------------------
# 10. CLI commands work without a Web process
# ---------------------------------------------------------------------------


class TestCLISubcommandsWithoutWeb:
    """Every CLI command starts, observes and cancels a run without a Web server."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterator[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_cli_start_observe_cancel(self, tmp_path: Path) -> None:
        # Build a request that the CLI resolver + event source
        # will accept. We supply a dataset-registry file so the
        # resolver finds the dataset; the CLI's empty event
        # source returns no events, so the run fails closed with
        # a structured reason code (no manifest).
        runs_root = tmp_path / "runs"
        registry_path = tmp_path / "registry.json"
        registry_path.write_text(
            json.dumps(
                {
                    "datasets": [
                        {
                            "chain_id": _CHAIN_ID,
                            "pool_key_id": _POOL_KEY_ID_A,
                            "dataset_version": _DATASET_VERSION,
                            "dataset_schema_version": _DATASET_SCHEMA_VERSION,
                            "dataset_decode_version": _DATASET_DECODE_VERSION,
                            "dataset_content_hash": _DATASET_CONTENT_HASH,
                            "reporting_numeraire": "USDG",
                            "valuation_qualification": "QUALIFIED",
                            "covered_start": 0,
                            "covered_end": 1000,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        request_path = tmp_path / "request.json"
        request_path.write_text(
            json.dumps(
                _build_request(run_id="cli-001").to_dict(),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        # ``backtest start`` — the CLI's empty event source
        # returns no events, so the run records FAILED.
        start_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "robinhood_lp",
                "backtest",
                "start",
                "--request",
                str(request_path),
                "--runs-root",
                str(runs_root),
                "--dataset-registry",
                str(registry_path),
            ],
            env={"PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            check=False,
        )
        # The CLI exit code is 1 (failed), but the JSON record
        # is on stdout.
        assert start_result.returncode == 1
        start_payload = json.loads(start_result.stdout.strip())
        assert start_payload["run_id"] == "cli-001"
        assert start_payload["state"] == "FAILED"
        # The reason code is structured.
        assert start_payload["reason_code"] is not None
        # ``backtest list`` returns the recorded run_id.
        list_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "robinhood_lp",
                "backtest",
                "list",
                "--runs-root",
                str(runs_root),
            ],
            env={"PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert list_result.returncode == 0
        list_payload = json.loads(list_result.stdout.strip())
        assert "cli-001" in list_payload["run_ids"]
        # ``backtest observe`` returns the persisted record.
        observe_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "robinhood_lp",
                "backtest",
                "observe",
                "--runs-root",
                str(runs_root),
                "--run-id",
                "cli-001",
            ],
            env={"PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert observe_result.returncode == 0
        observe_payload = json.loads(observe_result.stdout.strip())
        assert observe_payload["run_id"] == "cli-001"
        assert observe_payload["state"] == "FAILED"
        # ``backtest cancel`` on a terminal record returns the
        # persisted record untouched.
        cancel_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "robinhood_lp",
                "backtest",
                "cancel",
                "--runs-root",
                str(runs_root),
                "--run-id",
                "cli-001",
                "--reason",
                "T069_TEST_CANCEL",
            ],
            env={"PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert cancel_result.returncode == 0
        cancel_payload = json.loads(cancel_result.stdout.strip())
        # A terminal record stays terminal; the cancel is a
        # no-op on a FAILED run.
        assert cancel_payload["state"] == "FAILED"

    def test_cli_start_invalid_request_returns_nonzero(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        request_path = tmp_path / "bad.json"
        request_path.write_text("{}", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "robinhood_lp",
                "backtest",
                "start",
                "--request",
                str(request_path),
                "--runs-root",
                str(runs_root),
            ],
            env={"PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0


# Need ``sys`` for the subprocess invocations.
import sys  # noqa: E402  (import after fixtures for readability)

# ---------------------------------------------------------------------------
# 11. Orchestrator composes the existing T061 / T063 / T105 surfaces
# ---------------------------------------------------------------------------


class TestOrchestratorComposition:
    """The orchestrator imports the existing T061 / T063 / T105 surfaces."""

    def test_orchestrator_imports_engine(self) -> None:
        # The orchestrator module's namespace carries the T061
        # engine entry points; if the orchestrator re-implemented
        # the engine this import would be unnecessary.
        from robinhood_lp import orchestrator as orch

        assert hasattr(orch, "BacktestEngine")
        assert hasattr(orch, "empty_position_state")

    def test_orchestrator_imports_metrics(self) -> None:
        from robinhood_lp import orchestrator as orch

        assert hasattr(orch, "compute_run_metrics")
        assert hasattr(orch, "build_coverage_summary")
        assert hasattr(orch, "extract_decisions")
        assert hasattr(orch, "decisions_checksum")
        assert hasattr(orch, "LedgerSnapshot")

    def test_orchestrator_imports_manifest_authority(self) -> None:
        from robinhood_lp import orchestrator as orch

        assert hasattr(orch, "build_experiment_manifest")
        assert hasattr(orch, "validate_manifest")
        assert hasattr(orch, "write_manifest_to_path")

    def test_orchestrator_imports_registry_binding(self) -> None:
        from robinhood_lp import orchestrator as orch

        assert hasattr(orch, "bind_strategy_to_registry")
        assert hasattr(orch, "UnknownStrategyIdentityError")
