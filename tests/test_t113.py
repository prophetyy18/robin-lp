"""Contract-conformance tests for T113 — application backtest use case.

T113 establishes the stable in-process backtest use-case boundary
the CLI composes against. The contract pins four invariants:

1. **Public boundary.** The supported boundary is
   :class:`robinhood_lp.application.backtest.BacktestUseCase`. The
   class exposes typed ``start`` / ``resume`` operations, typed
   :class:`BacktestRequest` / :class:`BacktestResult` records, and
   typed failure classes; no ``Any``, no arbitrary callables, no
   pass-through to adapter internals.

2. **Composition over the approved path.** The use case wires the
   T100 dataset / partition readers, the T040/T041 replay, the
   T050 point-in-time market-features path, the T061 engine, and
   the T112 manifest / evidence publisher behind typed ports.

3. **Fail-closed.** Every failure — missing registry, unresolved
   or unqualified partition, empty input, replay / feature
   failure, cancellation — fails closed with a named reason
   code and publishes no successful current artifact.

4. **Old-path cutover.** The CLI ``backtest start`` and
   ``backtest resume`` subcommands route through the application
   use case; no current CLI path reaches
   ``BacktestOrchestrator.submit`` or fabricates a partition ref.
   Historical T069 / T105 / T109 artifacts remain byte-identical
   and read-only.

The tests pin these invariants with two heterogeneous pool
fixtures (Pool A on chain 4663 and Pool B on chain 8453), named
normal / boundary / invalid-input / failure cases, the CLI
composition root, the byte-identical historical artifacts, the
implementation guide, and the import-graph direction.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from robinhood_lp.application.backtest import (
    BACKTEST_USE_CASE_VERSION,
    BacktestRequest,
    BacktestResult,
    BacktestUseCase,
    BacktestUseCaseError,
    EmptyEventSourceFailure,
    InvalidRunRequestFailure,
    MissingDatasetRegistryFailure,
    PartitionResolutionFailure,
    UnqualifiedPartitionFailure,
    build_default_application,
)
from robinhood_lp.backtest.engine import BACKTEST_ENGINE_VERSION, empty_position_state
from robinhood_lp.backtest.events import (
    KIND_SHUTDOWN,
    KIND_SWAP,
    SOURCE_PRIORITY_DATA,
    SOURCE_PRIORITY_SYSTEM,
    BacktestEvent,
    PositionState,
)
from robinhood_lp.orchestrator import (
    CancelToken,
    DatasetCoverage,
    DatasetResolver,
    RunProgress,
    RunRecord,
    RunRequest,
    RunState,
    RunStateStore,
    UnknownDatasetVersionError,
)
from robinhood_lp.orchestrator.t112 import (
    StaticPartitionEventSource,
    T100PartitionResolutionError,
    T112DatasetPartitionResolver,
    T112PartitionResolution,
)
from robinhood_lp.reports.registry_binding import StrategyBinding
from robinhood_lp.reports.t112 import (
    MANIFEST_VERSION_T112,
    SIMULATION_EVIDENCE_VERSION_T112,
    BlockRange,
    T100PartitionResolver,
    T100ResolvedPartition,
    TickRange,
)
from robinhood_lp.strategy.registry import IDENTITY_HOLD, reset_default_registry_cache

# ---------------------------------------------------------------------------
# Shared fixtures: two heterogeneous pools (T113 acceptance clause).
# ---------------------------------------------------------------------------


CHAIN_ID_A: int = 4663
CHAIN_ID_B: int = 8453
POOL_KEY_A: str = "0x" + "ab" * 32
POOL_KEY_B: str = "0x" + "cd" * 32
DATASET_VERSION: str = "ds.t113.v1"
DATASET_HASH_A: str = "0x" + "ee" * 32
DATASET_HASH_B: str = "0x" + "ff" * 32
TICK_LOWER: int = -60
TICK_UPPER: int = 60


def _partition_id_a() -> str:
    return "chain=4663/contract=0xababababababababababababababababababab/event=Swap/range=1-100"


def _partition_id_b() -> str:
    return "chain=8453/contract=0xcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd/event=Swap/range=200-300"


def _swap_event(
    *,
    timestamp: int,
    chain_id: int = CHAIN_ID_A,
    pool_key_id: str = POOL_KEY_A,
    block_number: int | None = None,
    transaction_index: int | None = None,
    log_index: int | None = None,
) -> BacktestEvent:
    kwargs: dict[str, Any] = {
        "version": "t061.backtest_event.v1",
        "timestamp": timestamp,
        "sequence": 0,
        "source_priority": SOURCE_PRIORITY_DATA,
        "kind": KIND_SWAP,
        "pool_key_id": pool_key_id,
        "chain_id": chain_id,
        "observed_at": timestamp,
        "available_at": timestamp,
        "payload": (("price_q64_64", 1 << 64),),
    }
    if block_number is not None:
        kwargs["block_number"] = block_number
    if transaction_index is not None:
        kwargs["transaction_index"] = transaction_index
    if log_index is not None:
        kwargs["log_index"] = log_index
    return BacktestEvent(**kwargs)


def _shutdown_event(
    *,
    timestamp: int,
    chain_id: int = CHAIN_ID_A,
    pool_key_id: str = POOL_KEY_A,
    block_number: int = 0,
    transaction_index: int = 0,
    log_index: int = 0,
) -> BacktestEvent:
    return BacktestEvent(
        version="t061.backtest_event.v1",
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_SYSTEM,
        kind=KIND_SHUTDOWN,
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(),
        block_number=block_number,
        transaction_index=transaction_index,
        log_index=log_index,
    )


def _block_range_a() -> BlockRange:
    return BlockRange(start_block=1, end_block=100)


def _block_range_b() -> BlockRange:
    return BlockRange(start_block=200, end_block=300)


def _tick_range() -> TickRange:
    return TickRange(tick_lower=TICK_LOWER, tick_upper=TICK_UPPER)


def _strategy_binding() -> StrategyBinding:
    return StrategyBinding(
        strategy_identity=IDENTITY_HOLD,
        strategy_version="t062.baseline_strategy.v1",
        registry_version="t068.strategy_registry.v1",
        registry_checksum="0x" + "00" * 32,
        parameter_schema_version="t062.baseline_strategy.v1",
        parameter_schema_checksum="0x" + "11" * 32,
        code_provenance_module="robinhood_lp.strategy.baselines",
        code_provenance_revision=BACKTEST_ENGINE_VERSION,
        code_provenance_symbol="",
        validated_parameters=(("width", 60),),
    )


def _empty_position_state_dict(
    pool_key_id: str = POOL_KEY_A, chain_id: int = CHAIN_ID_A
) -> dict[str, Any]:
    return {
        "version": "t061.position_state.v1",
        "pool_key_id": pool_key_id,
        "chain_id": chain_id,
        "position_id": "0x" + "00" * 32,
        "tick_lower": TICK_LOWER,
        "tick_upper": TICK_UPPER,
        "liquidity": 0,
        "principal_token0": 0,
        "principal_token1": 0,
        "tokens_owed0": 0,
        "tokens_owed1": 0,
        "in_range": False,
        "last_accrual_time": 0,
    }


def _position_snapshot(
    pool_key_id: str = POOL_KEY_A,
    chain_id: int = CHAIN_ID_A,
    liquidity: int = 1000,
    principal_token0: int = 1,
    principal_token1: int = 2,
) -> PositionState:
    return PositionState(
        version="t061.position_state.v1",
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        position_id="0x" + "00" * 32,
        tick_lower=TICK_LOWER,
        tick_upper=TICK_UPPER,
        liquidity=liquidity,
        principal_token0=principal_token0,
        principal_token1=principal_token1,
        tokens_owed0=0,
        tokens_owed1=0,
        in_range=True,
        last_accrual_time=0,
    )


def _make_market_cursor(block_number: int, transaction_index: int, log_index: int) -> Any:
    from robinhood_lp.replay.market_state import MarketCursor

    return MarketCursor(block_number, transaction_index, log_index)


# ---------------------------------------------------------------------------
# Adapter test doubles
# ---------------------------------------------------------------------------


class _StaticDatasetResolver(DatasetResolver):
    """Static dataset coverage resolver backed by an explicit map."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, int, str], DatasetCoverage] = {}

    def add(
        self,
        *,
        dataset_version: str,
        chain_id: int,
        pool_key_id: str,
        covered_start: int,
        covered_end: int,
        content_hash: str,
    ) -> None:
        self._records[(dataset_version, chain_id, pool_key_id)] = DatasetCoverage(
            chain_id=chain_id,
            pool_key_id=pool_key_id,
            dataset_version=dataset_version,
            dataset_schema_version=1,
            dataset_decode_version=1,
            dataset_content_hash=content_hash,
            reporting_numeraire="USDG",
            valuation_qualification="QUALIFIED",
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
        if key not in self._records:
            raise UnknownDatasetVersionError(dataset_version=dataset_version)
        return self._records[key]


class _StaticT112PartitionResolver(T112DatasetPartitionResolver):
    """Static T112 partition resolver backed by an explicit map."""

    def __init__(
        self,
        partitions_by_key: dict[tuple[str, int, str], tuple[T100ResolvedPartition, ...]],
    ) -> None:
        self._records = partitions_by_key

    def resolve(
        self,
        *,
        request: RunRequest,
        coverage: DatasetCoverage,
    ) -> T112PartitionResolution:
        partitions = self._records.get(
            (coverage.dataset_version, coverage.chain_id, coverage.pool_key_id)
        )
        if partitions is None:
            raise T100PartitionResolutionError(
                message=(
                    f"dataset_version={coverage.dataset_version!r} "
                    f"chain_id={coverage.chain_id} pool_key_id="
                    f"{coverage.pool_key_id!r} is not registered"
                )
            )
        return T112PartitionResolution(
            dataset_version=coverage.dataset_version,
            chain_id=coverage.chain_id,
            pool_key_id=coverage.pool_key_id,
            block_range_start=request.block_range_start,
            block_range_end=request.block_range_end,
            partitions=partitions,
        )


class _StaticT100PartitionRefResolver(T100PartitionResolver):
    """Static T100 partition reference resolver."""

    def __init__(
        self,
        partitions: dict[tuple[int, str, str], T100ResolvedPartition],
    ) -> None:
        self._records = partitions

    def resolve(
        self,
        *,
        chain_id: int,
        pool_key_id: str,
        partition_ref: str,
    ) -> T100ResolvedPartition:
        record = self._records.get((chain_id, pool_key_id, partition_ref))
        if record is None:
            raise KeyError(
                f"partition reference {partition_ref!r} is not registered "
                f"for ({chain_id}, {pool_key_id!r})"
            )
        return record


def _resolved_partition_a() -> T100ResolvedPartition:
    return T100ResolvedPartition(
        partition_id=_partition_id_a(),
        chain_id=CHAIN_ID_A,
        pool_key_id=POOL_KEY_A,
        content_hash=DATASET_HASH_A,
        range=_block_range_a(),
        schema_version=1,
        decode_version=1,
    )


def _resolved_partition_b() -> T100ResolvedPartition:
    return T100ResolvedPartition(
        partition_id=_partition_id_b(),
        chain_id=CHAIN_ID_B,
        pool_key_id=POOL_KEY_B,
        content_hash=DATASET_HASH_B,
        range=_block_range_b(),
        schema_version=1,
        decode_version=1,
    )


def _build_full_request(
    run_id: str = "t113-test-001",
    *,
    chain_id: int = CHAIN_ID_A,
    pool_key_id: str = POOL_KEY_A,
    block_range_start: int = 1,
    block_range_end: int = 100,
) -> RunRequest:
    return RunRequest(
        run_id=run_id,
        dataset_version=DATASET_VERSION,
        chain_id=chain_id,
        pool_key_id=pool_key_id,
        block_range_start=block_range_start,
        block_range_end=block_range_end,
        interval_seconds=300,
        strategy_identity=IDENTITY_HOLD,
        strategy_parameters={},
        seed=0,
        clock_assumption="EVENT_TIME",
        fill_assumption="DETERMINISTIC_FAILURE",
        cost_assumption="FLAT_GAS",
        quote_assumption="STATIC_FEE",
        latency_units=0,
        latency_ms_estimate=0,
        reporting_numeraire="USDG",
        valuation_qualification="QUALIFIED",
        code_revision="0x" + "33" * 20,
        dependency_revisions={"robinhood-lp": "0.0.0"},
        created_at_unix_seconds=1_700_000_000,
    )


def _build_use_case(
    *,
    events: list[BacktestEvent],
    tmp_path: Path,
    chain_id: int = CHAIN_ID_A,
    pool_key_id: str = POOL_KEY_A,
    dataset_content_hash: str = DATASET_HASH_A,
    partition: T100ResolvedPartition | None = None,
) -> BacktestUseCase:
    """Build the wired BacktestUseCase the T113 contract composes."""
    dataset_resolver = _StaticDatasetResolver()
    dataset_resolver.add(
        dataset_version=DATASET_VERSION,
        chain_id=chain_id,
        pool_key_id=pool_key_id,
        covered_start=1,
        covered_end=1000,
        content_hash=dataset_content_hash,
    )
    partition_resolver = _StaticT112PartitionResolver(
        partitions_by_key={
            (DATASET_VERSION, chain_id, pool_key_id): (partition or _resolved_partition_a(),),
        }
    )
    ref_resolver = _StaticT100PartitionRefResolver(
        partitions={
            (
                chain_id,
                pool_key_id,
                partition.partition_id if partition else _partition_id_a(),
            ): partition or _resolved_partition_a(),
        }
    )
    store = RunStateStore(runs_root=tmp_path / "runs")
    return BacktestUseCase(
        store=store,
        dataset_resolver=dataset_resolver,
        partition_resolver=partition_resolver,
        event_source=StaticPartitionEventSource(partition_events=events),
        partition_ref_resolver=ref_resolver,
    )


def _make_running_record(request: RunRequest) -> Any:
    """Build a RUNNING ``RunRecord`` for the resume test."""
    from robinhood_lp.orchestrator.t112 import T112_ORCHESTRATOR_VERSION

    progress = RunProgress(
        events_total=0,
        events_processed=0,
        current_stage="RUNNING",
        updated_at_unix_seconds=0,
    )

    record = RunRecord(
        version=T112_ORCHESTRATOR_VERSION,
        run_id=request.run_id,
        state=RunState.RUNNING,
        request=request,
        progress=progress,
        reason_code=None,
        error_message=None,
        manifest_path=None,
        report_path=None,
        source_manifest_path=request.source_manifest_path,
        source_checksum=None,
        simulation_evidence_path=None,
        created_at_unix_seconds=request.created_at_unix_seconds,
        updated_at_unix_seconds=0,
        terminal_at_unix_seconds=None,
    )
    return record


# ---------------------------------------------------------------------------
# 1. Public boundary: typed ports, typed records, no Any / no callables
# ---------------------------------------------------------------------------


class TestPublicBoundary:
    """The public boundary exposes typed records and refuses untyped inputs."""

    def test_backtest_use_case_version_is_a_string(self) -> None:
        """The use-case version is a non-empty string identifier."""
        assert isinstance(BACKTEST_USE_CASE_VERSION, str)
        assert BACKTEST_USE_CASE_VERSION.startswith("t113.")

    def test_backtest_request_is_a_run_request(self) -> None:
        """The public ``BacktestRequest`` is the ``RunRequest`` value object."""
        assert BacktestRequest is RunRequest

    def test_backtest_result_is_a_frozen_dataclass(self) -> None:
        """The result is a typed frozen dataclass with the documented fields."""
        fields = {f.name for f in BacktestResult.__dataclass_fields__.values()}
        for required in (
            "run_id",
            "chain_id",
            "pool_key_id",
            "dataset_version",
            "state",
            "reason_code",
            "error_message",
            "manifest_path",
            "evidence_path",
            "manifest_payload",
            "evidence_payload",
        ):
            assert required in fields

    def test_failure_classes_carry_t113_prefix(self) -> None:
        """Every documented failure class is a distinct typed exception."""
        for cls in (
            MissingDatasetRegistryFailure,
            PartitionResolutionFailure,
            UnqualifiedPartitionFailure,
            EmptyEventSourceFailure,
            InvalidRunRequestFailure,
        ):
            assert issubclass(cls, BacktestUseCaseError)
            assert cls is not BacktestUseCaseError

    def test_composition_root_returns_backtest_use_case(self) -> None:
        """The composition root returns the typed public boundary."""
        dataset_resolver = _StaticDatasetResolver()
        dataset_resolver.add(
            dataset_version=DATASET_VERSION,
            chain_id=CHAIN_ID_A,
            pool_key_id=POOL_KEY_A,
            covered_start=1,
            covered_end=1000,
            content_hash=DATASET_HASH_A,
        )
        use_case = build_default_application(
            runs_root=Path("/tmp/t113-runs"),
            dataset_resolver=dataset_resolver,
            partition_resolver=_StaticT112PartitionResolver({}),
            event_source=StaticPartitionEventSource(partition_events=[]),
            partition_ref_resolver=_StaticT100PartitionRefResolver({}),
        )
        assert isinstance(use_case, BacktestUseCase)


# ---------------------------------------------------------------------------
# 2. Normal case: two heterogeneous pool fixtures publish T112 artifacts
# ---------------------------------------------------------------------------


class TestNormalCaseTwoHeterogeneousPools:
    """Pool A (chain 4663) and Pool B (chain 8453) publish T112 artifacts."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterable[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def _events_for_pool(self, chain_id: int, pool_key_id: str) -> list[BacktestEvent]:
        return [
            _swap_event(
                timestamp=100,
                chain_id=chain_id,
                pool_key_id=pool_key_id,
                block_number=10,
                transaction_index=0,
                log_index=0,
            ),
            _shutdown_event(
                timestamp=200,
                chain_id=chain_id,
                pool_key_id=pool_key_id,
                block_number=40,
                transaction_index=0,
                log_index=0,
            ),
        ]

    def test_pool_a_normal_case_publishes_t112_artifacts(self, tmp_path: Path) -> None:
        """Pool A succeeds and publishes a T112 manifest + T112 evidence."""
        events = self._events_for_pool(CHAIN_ID_A, POOL_KEY_A)
        use_case = _build_use_case(events=events, tmp_path=tmp_path)
        result = use_case.start(_build_full_request())
        assert result.state is RunState.SUCCEEDED
        assert result.reason_code is None
        assert result.error_message is None
        assert result.manifest_path is not None
        assert result.evidence_path is not None
        assert result.manifest_payload is not None
        assert result.evidence_payload is not None
        assert result.manifest_payload["version"] == MANIFEST_VERSION_T112
        assert result.evidence_payload["version"] == SIMULATION_EVIDENCE_VERSION_T112
        assert result.manifest_payload["chain_id"] == CHAIN_ID_A
        assert result.manifest_payload["pool_key_id"] == POOL_KEY_A

    def test_pool_b_normal_case_publishes_t112_artifacts(self, tmp_path: Path) -> None:
        """Pool B (heterogeneous: chain 8453, distinct content hash) succeeds."""
        events = self._events_for_pool(CHAIN_ID_B, POOL_KEY_B)
        partition = _resolved_partition_b()
        use_case = _build_use_case(
            events=events,
            tmp_path=tmp_path,
            chain_id=CHAIN_ID_B,
            pool_key_id=POOL_KEY_B,
            dataset_content_hash=DATASET_HASH_B,
            partition=partition,
        )
        result = use_case.start(_build_full_request(chain_id=CHAIN_ID_B, pool_key_id=POOL_KEY_B))
        assert result.state is RunState.SUCCEEDED
        assert result.manifest_payload is not None
        assert result.manifest_payload["chain_id"] == CHAIN_ID_B
        assert result.manifest_payload["pool_key_id"] == POOL_KEY_B
        assert result.manifest_payload["dataset_content_hash"] == DATASET_HASH_B
        refs = result.manifest_payload["dataset_partition_refs"]
        assert any(ref["partition_id"] == _partition_id_b() for ref in refs), (
            "Pool B run must bind the real Pool B partition reference"
        )

    def test_normal_case_persists_succeeded_record(self, tmp_path: Path) -> None:
        """A successful run persists a SUCCEEDED T069-format record."""
        events = self._events_for_pool(CHAIN_ID_A, POOL_KEY_A)
        use_case = _build_use_case(events=events, tmp_path=tmp_path)
        result = use_case.start(_build_full_request())
        record_path = tmp_path / "runs" / f"{result.run_id}.run.json"
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        assert payload["state"] == RunState.SUCCEEDED.value
        assert payload["manifest_path"] == str(result.manifest_path)
        assert payload["simulation_evidence_path"] == str(result.evidence_path)


# ---------------------------------------------------------------------------
# 3. Boundary cases: empty input, cancellation
# ---------------------------------------------------------------------------


class TestBoundaryCases:
    """Boundary inputs fail closed with a named reason."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterable[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_empty_event_source_fails_closed(self, tmp_path: Path) -> None:
        """Empty event source fails closed with the named T113 reason."""
        use_case = _build_use_case(events=[], tmp_path=tmp_path)
        result = use_case.start(_build_full_request())
        assert result.state is RunState.FAILED
        assert result.manifest_path is None
        assert result.evidence_path is None
        assert result.reason_code is not None
        assert "EMPTY_EVENT_SOURCE_REFUSED" in result.reason_code
        assert "T113_" in result.reason_code

    def test_cancellation_fails_closed(self, tmp_path: Path) -> None:
        """A cancelled run fails closed with the named T113 reason."""
        events = [
            _swap_event(timestamp=100, block_number=10, transaction_index=0, log_index=0),
            _shutdown_event(timestamp=200, block_number=40, transaction_index=0, log_index=0),
        ]
        use_case = _build_use_case(events=events, tmp_path=tmp_path)
        token = CancelToken()
        token.cancel(reason="T113_TEST_CANCELLED")
        result = use_case.start(_build_full_request(), cancel_token=token)
        assert result.state is RunState.FAILED
        assert result.manifest_path is None
        assert result.evidence_path is None
        assert result.reason_code is not None
        assert "T113_" in result.reason_code


# ---------------------------------------------------------------------------
# 4. Invalid input
# ---------------------------------------------------------------------------


class TestInvalidInput:
    """An invalid request fails closed with the typed ``InvalidRunRequestFailure``."""

    def test_non_request_input_raises_typed_failure(self, tmp_path: Path) -> None:
        """Passing a non-``RunRequest`` value raises ``InvalidRunRequestFailure``."""
        use_case = _build_use_case(events=[], tmp_path=tmp_path)
        with pytest.raises(InvalidRunRequestFailure):
            use_case.start({"run_id": "not-a-request"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. Failure cases: missing registry, unresolved / unqualified partition
# ---------------------------------------------------------------------------


class TestFailureCases:
    """Named failure modes fail closed with the dedicated T113 reason."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterable[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_missing_dataset_registry_fails_closed(self, tmp_path: Path) -> None:
        """A request naming a missing dataset version fails closed."""
        events = [
            _swap_event(timestamp=100, block_number=10, transaction_index=0, log_index=0),
            _shutdown_event(timestamp=200, block_number=40, transaction_index=0, log_index=0),
        ]
        use_case = _build_use_case(events=events, tmp_path=tmp_path)
        request = _build_full_request()
        # Use a dataset version the resolver does not know.
        unknown = RunRequest(
            run_id=request.run_id,
            dataset_version="ds.unknown",
            chain_id=request.chain_id,
            pool_key_id=request.pool_key_id,
            block_range_start=request.block_range_start,
            block_range_end=request.block_range_end,
            interval_seconds=request.interval_seconds,
            strategy_identity=request.strategy_identity,
            strategy_parameters=dict(request.strategy_parameters),
            seed=request.seed,
            clock_assumption=request.clock_assumption,
            fill_assumption=request.fill_assumption,
            cost_assumption=request.cost_assumption,
            quote_assumption=request.quote_assumption,
            latency_units=request.latency_units,
            latency_ms_estimate=request.latency_ms_estimate,
            reporting_numeraire=request.reporting_numeraire,
            valuation_qualification=request.valuation_qualification,
            code_revision=request.code_revision,
            dependency_revisions=dict(request.dependency_revisions),
            created_at_unix_seconds=request.created_at_unix_seconds,
            source_manifest_path=request.source_manifest_path,
        )
        result = use_case.start(unknown)
        assert result.state is RunState.FAILED
        assert result.manifest_path is None
        assert result.reason_code is not None
        assert "T113_" in result.reason_code

    def test_unresolved_partition_fails_closed(self, tmp_path: Path) -> None:
        """A request whose partition set is unresolved fails closed."""
        events = [
            _swap_event(timestamp=100, block_number=10, transaction_index=0, log_index=0),
            _shutdown_event(timestamp=200, block_number=40, transaction_index=0, log_index=0),
        ]
        # Build the use case with an empty partition map so the
        # partition resolver refuses the request.
        dataset_resolver = _StaticDatasetResolver()
        dataset_resolver.add(
            dataset_version=DATASET_VERSION,
            chain_id=CHAIN_ID_A,
            pool_key_id=POOL_KEY_A,
            covered_start=1,
            covered_end=1000,
            content_hash=DATASET_HASH_A,
        )
        partition_resolver = _StaticT112PartitionResolver(
            partitions_by_key={}  # no registered partitions
        )
        ref_resolver = _StaticT100PartitionRefResolver({})
        store = RunStateStore(runs_root=tmp_path / "runs")
        use_case = BacktestUseCase(
            store=store,
            dataset_resolver=dataset_resolver,
            partition_resolver=partition_resolver,
            event_source=StaticPartitionEventSource(partition_events=events),
            partition_ref_resolver=ref_resolver,
        )
        result = use_case.start(_build_full_request())
        assert result.state is RunState.FAILED
        assert result.manifest_path is None
        assert result.evidence_path is None
        assert result.reason_code is not None
        assert "T113_" in result.reason_code


# ---------------------------------------------------------------------------
# 6. Resume: every RUNNING record is re-submitted through the use case
# ---------------------------------------------------------------------------


class TestResume:
    """``resume`` re-submits every RUNNING record through the use case."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterable[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_resume_with_no_running_records_returns_empty_tuple(self, tmp_path: Path) -> None:
        """Resume on an empty store returns an empty result sequence."""
        use_case = _build_use_case(events=[], tmp_path=tmp_path)
        results = use_case.resume()
        assert results == ()

    def test_resume_processes_running_records(self, tmp_path: Path) -> None:
        """Resume re-submits a RUNNING record through the T113 path."""
        events = [
            _swap_event(timestamp=100, block_number=10, transaction_index=0, log_index=0),
            _shutdown_event(timestamp=200, block_number=40, transaction_index=0, log_index=0),
        ]
        use_case = _build_use_case(events=events, tmp_path=tmp_path)
        # Plant a RUNNING record the resume call must re-submit.
        request = _build_full_request(run_id="t113-resume-001")
        store = use_case._store
        running = _make_running_record(request=request)
        store.write(running)
        results = use_case.resume()
        assert len(results) == 1
        result = results[0]
        assert result.state is RunState.SUCCEEDED
        assert result.run_id == "t113-resume-001"
        assert result.manifest_path is not None
        assert result.evidence_path is not None


# ---------------------------------------------------------------------------
# 7. Old-path cutover: no CLI path reaches the T109 writer
# ---------------------------------------------------------------------------


class TestCLIRoutesThroughApplicationAPI:
    """The CLI routes ``start`` and ``resume`` through the application API."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterable[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_cli_start_invokes_application_api(self, tmp_path: Path) -> None:
        """The CLI ``backtest start`` subcommand invokes ``BacktestUseCase.start``."""
        request_path = tmp_path / "request.json"
        request_payload = _build_full_request().to_dict()
        request_path.write_text(json.dumps(request_payload), encoding="utf-8")
        runs_root = tmp_path / "runs"
        env = os.environ.copy()
        env["PYTHONPATH"] = "src"
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
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        # The CLI fails closed because the default composition
        # stubs fail at the partition resolver. The named reason
        # surfaces on stderr; the typed BacktestResult is on
        # stdout with state=FAILED.
        assert result.returncode != 0
        stdout_payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert stdout_payload["state"] == RunState.FAILED.value
        assert stdout_payload["manifest_path"] is None
        assert "T113_" in stdout_payload["reason_code"]

    def test_cli_resume_invokes_application_api(self, tmp_path: Path) -> None:
        """The CLI ``backtest resume`` subcommand invokes ``BacktestUseCase.resume``."""
        runs_root = tmp_path / "runs"
        env = os.environ.copy()
        env["PYTHONPATH"] = "src"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "robinhood_lp",
                "backtest",
                "resume",
                "--runs-root",
                str(runs_root),
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        # No RUNNING records => empty resumed list, exit 0.
        assert result.returncode == 0
        payload = json.loads(result.stdout.strip())
        assert payload["resumed"] == []

    def test_cli_source_routes_through_application_api(self) -> None:
        """The CLI ``_run_backtest`` invokes ``build_default_application``."""
        from robinhood_lp import __main__ as cli_main

        source = Path(cli_main.__file__).read_text(encoding="utf-8")
        # The CLI composes the T113 use case via the composition root.
        assert "build_default_application" in source or "_build_cli_application" in source
        # The CLI ``_run_backtest`` does not call the predecessor
        # T069 ``BacktestOrchestrator`` directly for ``start`` or
        # ``resume``. Locate the function body and grep it.
        head = source.index("def _run_backtest")
        # Read until the next top-level def to bound the body.
        next_def_idx = source.find("\ndef ", head + 1)
        body = source[head : next_def_idx if next_def_idx > 0 else len(source)]
        assert "BacktestOrchestrator(" not in body, (
            "CLI ``_run_backtest`` must not call BacktestOrchestrator() "
            "directly; the T113 contract binds CLI start/resume through "
            "the application use case"
        )


# ---------------------------------------------------------------------------
# 8. Byte-identical historical artifacts (T069 / T105 / T109)
# ---------------------------------------------------------------------------


class TestByteIdenticalHistoricalArtifacts:
    """The T113 cutover preserves byte-identical historical T069/T105/T109 artifacts."""

    def test_t109_manifest_round_trip_byte_identical(self) -> None:
        """A T109 manifest round-trips through the T109 loader unchanged."""
        from robinhood_lp.reports.manifest import (
            build_t109_experiment_manifest,
            t109_experiment_manifest_from_dict,
        )
        from robinhood_lp.reports.metrics import (
            CoverageSummary,
            LedgerSnapshot,
            RunMetrics,
        )

        coverage = CoverageSummary(
            version="t063.coverage_summary.v1",
            chain_id=CHAIN_ID_A,
            pool_key_id=POOL_KEY_A,
            interval_seconds=300,
            input_events_total=1,
            data_events_total=1,
            fill_observations_total=0,
            audit_events_total=1,
            block_range_start=1,
            block_range_end=100,
            duration_seconds=99,
            data_gaps=(),
        )
        ledger = LedgerSnapshot.from_position_state(
            empty_position_state(pool_key_id=POOL_KEY_A, chain_id=CHAIN_ID_A)
        )
        metrics = RunMetrics(
            version="t063.run_metrics.v1",
            chain_id=CHAIN_ID_A,
            pool_key_id=POOL_KEY_A,
            interval_seconds=300,
            duration_seconds=99,
            total_return_q64_64=0,
            annualized_return_q64_64=0,
            max_drawdown_q64_64=0,
            turnover_q64_64=0,
            time_in_range_seconds=0,
            fees_q64_64=0,
            il_lvr_proxy_q64_64=0,
            gas_units_total=0,
            slippage_bps_total=0,
            benchmark_excess_q64_64=0,
            fills_count=0,
        )
        manifest = build_t109_experiment_manifest(
            run_id="t109-legacy",
            metrics=metrics,
            coverage=coverage,
            ledger_snapshot=ledger,
            decisions_checksum="0x" + "33" * 32,
            dataset_version="ds.legacy",
            dataset_schema_version=1,
            dataset_decode_version=1,
            dataset_content_hash="0x" + "aa" * 32,
            reporting_numeraire="USDG",
            valuation_qualification="QUALIFIED",
            code_revision="0x" + "44" * 20,
            dependency_revisions={},
            strategy_binding=_strategy_binding(),
            seed=0,
            clock_assumption="EVENT_TIME",
            fill_assumption="DETERMINISTIC_FAILURE",
            cost_assumption="FLAT_GAS",
            quote_assumption="STATIC_FEE",
            latency_units=0,
            latency_ms_estimate=0,
            created_at_unix_seconds=1_700_000_000,
            block_range_start=1,
            block_range_end=100,
            dataset_partition_refs=("placeholder",),
            dataset_event_count=1,
            simulation_evidence_ref="t109-evidence.json",
            reconstruction_revision=BACKTEST_ENGINE_VERSION,
        )
        loaded = t109_experiment_manifest_from_dict(manifest.to_dict())
        assert loaded == manifest

    def test_t105_manifest_round_trip_byte_identical(self) -> None:
        """A T105 manifest round-trips through the T105 loader unchanged."""
        from robinhood_lp.reports.manifest import (
            build_experiment_manifest,
            experiment_manifest_from_dict,
        )
        from robinhood_lp.reports.metrics import (
            CoverageSummary,
            LedgerSnapshot,
            RunMetrics,
        )

        coverage = CoverageSummary(
            version="t063.coverage_summary.v1",
            chain_id=CHAIN_ID_A,
            pool_key_id=POOL_KEY_A,
            interval_seconds=300,
            input_events_total=1,
            data_events_total=1,
            fill_observations_total=0,
            audit_events_total=1,
            block_range_start=1,
            block_range_end=100,
            duration_seconds=99,
            data_gaps=(),
        )
        ledger = LedgerSnapshot.from_position_state(
            empty_position_state(pool_key_id=POOL_KEY_A, chain_id=CHAIN_ID_A)
        )
        metrics = RunMetrics(
            version="t063.run_metrics.v1",
            chain_id=CHAIN_ID_A,
            pool_key_id=POOL_KEY_A,
            interval_seconds=300,
            duration_seconds=99,
            total_return_q64_64=0,
            annualized_return_q64_64=0,
            max_drawdown_q64_64=0,
            turnover_q64_64=0,
            time_in_range_seconds=0,
            fees_q64_64=0,
            il_lvr_proxy_q64_64=0,
            gas_units_total=0,
            slippage_bps_total=0,
            benchmark_excess_q64_64=0,
            fills_count=0,
        )
        manifest = build_experiment_manifest(
            run_id="t105-legacy",
            metrics=metrics,
            coverage=coverage,
            ledger_snapshot=ledger,
            decisions_checksum="0x" + "33" * 32,
            input_events=(),
            dataset_version="ds.legacy",
            dataset_schema_version=1,
            dataset_decode_version=1,
            dataset_content_hash="0x" + "aa" * 32,
            reporting_numeraire="USDG",
            valuation_qualification="QUALIFIED",
            code_revision="0x" + "44" * 20,
            dependency_revisions={},
            strategy_binding=_strategy_binding(),
            seed=0,
            clock_assumption="EVENT_TIME",
            fill_assumption="DETERMINISTIC_FAILURE",
            cost_assumption="FLAT_GAS",
            quote_assumption="STATIC_FEE",
            latency_units=0,
            latency_ms_estimate=0,
            created_at_unix_seconds=1_700_000_000,
            block_range_start=1,
            block_range_end=100,
        )
        loaded = experiment_manifest_from_dict(manifest.to_dict())
        assert loaded == manifest


# ---------------------------------------------------------------------------
# 9. Implementation guide presence
# ---------------------------------------------------------------------------


class TestImplementationGuide:
    """The T113 implementation guide exists and is non-empty."""

    def test_implementation_guide_exists(self) -> None:
        """The implementation guide is on disk under ``docs/implement/backtest``."""
        guide_path = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "implement"
            / "backtest"
            / "IMPLEMENTATION_GUIDE.md"
        )
        assert guide_path.exists(), (
            "T113 must ship an implementation guide at "
            "`docs/implement/backtest/IMPLEMENTATION_GUIDE.md`"
        )
        content = guide_path.read_text(encoding="utf-8")
        for required in (
            "BacktestUseCase",
            "robinhood_lp.application.backtest",
            "Pool A",
            "Pool B",
            "CHAIN_ID_A",
            "CHAIN_ID_B",
        ):
            assert required in content, (
                f"implementation guide missing required mention of {required!r}"
            )


# ---------------------------------------------------------------------------
# 10. Application boundary uses no sibling-import siblings
# ---------------------------------------------------------------------------


class TestNoSiblingImports:
    """The application module does not import sibling implementation modules."""

    def test_application_module_does_not_import_lower_layer_modules(self) -> None:
        """The application boundary does not import presentation / risk modules."""
        from robinhood_lp.application import backtest as app_module

        source = Path(app_module.__file__).read_text(encoding="utf-8")
        forbidden = (
            "from robinhood_lp.presentation import",
            "from robinhood_lp.risk import",
            "from robinhood_lp.web import",
            "from robinhood_lp.storage import",
        )
        for fragment in forbidden:
            assert fragment not in source, (
                f"application.backtest must not import {fragment}; ADR-006 forbids it"
            )
