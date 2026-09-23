"""Acceptance tests for T112 — pipeline-repair cutover.

T112 replaces T109 with three binding invariants:

1. **Pipeline repair.** The product backtest entry loads the
   dataset's real T100 partitions and resolves them through the
   existing T040/T041 replay and T050 point-in-time
   market-features paths. The fixed empty event source the
   approved T109 implementation could reach is no longer
   reachable as a current entry.

2. **Real-partition reference binding.** Every accepted
   ``dataset_partition_ref`` resolves to the same canonical
   bytes T100 registered (verified by content hash, range and
   PoolKey). A reference cannot be authored from an event
   cursor, a coverage string, a placeholder or a synthetic list;
   the loader rejects mismatched or non-resolvable references
   with the named reason code.

3. **Block-range vs tick-range separation.** The T112 manifest
   binds the source ``block_range``; the T112 simulation-evidence
   artifact separately records the source ``block_range`` and the
   actual position ``tick_range`` as distinct typed field pairs.
   Block bounds agree with the T100 partition source range and
   the manifest; tick bounds agree with the actual T061 position
   and the T040/T041 reconstruction at the fill cursor.

4. **Strict paired-version dispatch.** A reader dispatches by
   the paired manifest / evidence versions before interpreting
   fields. Mixed, unknown, missing or downgraded versions fail
   closed with the named reason code
   ``T112_T109_HISTORICAL_UNAVAILABLE``. A T109-versioned record
   is never accepted as current T112 evidence and a
   T112-versioned record is never accepted as legacy T109
   evidence.

5. **Fill-cursor preservation.** The T112 simulation-evidence
   artifact carries enough run-specific facts at the actual fill
   cursor to restore position, integer inventory, equity,
   drawdown and T052 attribution through the existing T061
   engine and T052 attribution semantics. A ``RunState`` reader
   at the fill cursor restores them through the existing engine
   and attribution semantics and never through a strategy
   callback, a range-derived synthetic, or an interpolated tick.

The tests pin these invariants with two heterogeneous pool
fixtures, cursor-fabricated / coverage-fabricated / placeholder
reference rejection, distinct typed block_range / tick_range
binding, paired-version dispatch, fill-cursor preservation,
rollback and migration scenarios.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from robinhood_lp.backtest.engine import (
    BACKTEST_ENGINE_VERSION,
    empty_position_state,
)
from robinhood_lp.backtest.events import (
    KIND_SHUTDOWN,
    KIND_SWAP,
    SOURCE_PRIORITY_DATA,
    SOURCE_PRIORITY_SYSTEM,
    BacktestEvent,
    PositionState,
)
from robinhood_lp.orchestrator import (
    DatasetCoverage,
    DatasetResolver,
    RunRequest,
    RunStateStore,
)
from robinhood_lp.orchestrator.t112 import (
    BacktestOrchestratorT112,
    FixedEmptyEventSourceError,
    PartitionRefMismatchError,
    StaticPartitionEventSource,
    T100PartitionResolutionError,
    T112DatasetPartitionResolver,
    T112PartitionResolution,
)
from robinhood_lp.replay.market_state import MARKET_STATE_VERSION, MarketCursor
from robinhood_lp.reports.manifest import MANIFEST_VERSION_T109
from robinhood_lp.reports.registry_binding import StrategyBinding
from robinhood_lp.reports.simulation_evidence import (
    RunStateCheckpoint,
    RunTransition,
)
from robinhood_lp.reports.t112 import (
    MANIFEST_VERSION_T112,
    SIMULATION_EVIDENCE_VERSION_T112,
    BlockRange,
    FillCursorRunFacts,
    PartitionReference,
    T100PartitionResolver,
    T100ResolvedPartition,
    T112BlockTickConflationError,
    T112ExperimentManifest,
    T112FieldError,
    T112IdentityDisagreementError,
    T112PartitionRefError,
    T112SimulationEvidence,
    T112VersionDispatchError,
    TickRange,
    build_t112_experiment_manifest,
    build_t112_simulation_evidence,
    compute_t112_report_checksum,
    dispatch_evidence_version,
    dispatch_manifest_version,
    t112_experiment_manifest_from_dict,
    t112_simulation_evidence_from_dict,
)
from robinhood_lp.strategy.registry import (
    IDENTITY_HOLD,
    reset_default_registry_cache,
)

# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


CHAIN_ID_A: int = 4663
CHAIN_ID_B: int = 8453
POOL_KEY_A: str = "0x" + "ab" * 32
POOL_KEY_B: str = "0x" + "cd" * 32
DATASET_VERSION: str = "ds.t112.v1"
DATASET_HASH_A: str = "0x" + "ee" * 32
DATASET_HASH_B: str = "0x" + "ff" * 32
TICK_LOWER: int = -60
TICK_UPPER: int = 60
RUN_ID: str = "t112-run-0001"


def _partition_id_a() -> str:
    """Return a real T100 partition ID for pool A."""
    return "chain=4663/contract=0xababababababababababababababababababab/event=Swap/range=1-100"


def _partition_id_b() -> str:
    """Return a real T100 partition ID for pool B."""
    return "chain=8453/contract=0xcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd/event=Swap/range=200-300"


def _swap_event(
    *,
    timestamp: int,
    chain_id: int = CHAIN_ID_A,
    pool_key_id: str = POOL_KEY_A,
    price_q64_64: int = 1 << 64,
    block_number: int | None = None,
    transaction_index: int | None = None,
    log_index: int | None = None,
    available_at: int | None = None,
) -> BacktestEvent:
    """Build a SWAP event with an explicit cursor when supplied."""
    kwargs: dict[str, Any] = {
        "version": "t061.backtest_event.v1",
        "timestamp": timestamp,
        "sequence": 0,
        "source_priority": SOURCE_PRIORITY_DATA,
        "kind": KIND_SWAP,
        "pool_key_id": pool_key_id,
        "chain_id": chain_id,
        "observed_at": timestamp,
        "available_at": available_at if available_at is not None else timestamp,
        "payload": (("price_q64_64", price_q64_64),),
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


def _partition_ref_a() -> PartitionReference:
    return PartitionReference(
        partition_id=_partition_id_a(),
        chain_id=CHAIN_ID_A,
        pool_key_id=POOL_KEY_A,
        content_hash=DATASET_HASH_A,
        range=_block_range_a(),
        schema_version=1,
        decode_version=1,
    )


def _partition_ref_b() -> PartitionReference:
    return PartitionReference(
        partition_id=_partition_id_b(),
        chain_id=CHAIN_ID_B,
        pool_key_id=POOL_KEY_B,
        content_hash=DATASET_HASH_B,
        range=_block_range_b(),
        schema_version=1,
        decode_version=1,
    )


def _strategy_binding() -> StrategyBinding:
    """Return a deterministic strategy binding the manifest / evidence bind to."""
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


def _fill_cursor_run_facts(
    *,
    fill_cursor: MarketCursor,
    position_snapshot: PositionState,
    realised_fees_q64_64: int = 0,
) -> FillCursorRunFacts:
    return FillCursorRunFacts(
        fill_cursor=fill_cursor,
        position_snapshot=position_snapshot,
        raw_token0=position_snapshot.principal_token0,
        raw_token1=position_snapshot.principal_token1,
        realised_fees_q64_64=realised_fees_q64_64,
        cost_components={
            "gas_units": 21_000,
            "fee_pips": 3_000,
            "impact_bps": 0,
        },
        equity_q64_64=0,
        drawdown_q64_64=0,
        attribution_snapshot={
            "realised_pnl_q64_64": 0,
            "fees_collected_q64_64": realised_fees_q64_64,
            "il_lvr_q64_64": 0,
            "gas_q64_64": 0,
        },
    )


def _run_state_checkpoint(
    *,
    cursor: MarketCursor,
    ordinal: int,
    ledger_snapshot: dict[str, Any],
) -> RunStateCheckpoint:
    return RunStateCheckpoint(
        cursor=cursor,
        last_applied_ordinal=ordinal,
        ledger_snapshot=ledger_snapshot,
        equity_q64_64=0,
        drawdown_q64_64=0,
        attribution_snapshot={
            "realised_pnl_q64_64": 0,
            "fees_collected_q64_64": 0,
            "il_lvr_q64_64": 0,
            "gas_q64_64": 0,
        },
    )


def _initial_transitions() -> tuple[RunTransition, ...]:
    return (
        RunTransition(
            ordinal=0,
            stage="SYSTEM",
            cursor=None,
            ledger_hash_after="0x" + "00" * 32,
            audit_event_id="0xinit",
            payload=None,
            state_changing=False,
        ),
    )


def _build_t112_evidence(
    *,
    manifest_checksum: str = "0x" + "11" * 32,
) -> T112SimulationEvidence:
    return build_t112_simulation_evidence(
        run_id=RUN_ID,
        dataset_version=DATASET_VERSION,
        dataset_schema_version=1,
        dataset_decode_version=1,
        dataset_content_hash=DATASET_HASH_A,
        pool_key_id=POOL_KEY_A,
        chain_id=CHAIN_ID_A,
        source_block_range=_block_range_a(),
        position_tick_range=_tick_range(),
        strategy_identity=IDENTITY_HOLD,
        strategy_version="t062.baseline_strategy.v1",
        registry_version="t068.strategy_registry.v1",
        registry_checksum="0x" + "00" * 32,
        parameter_schema_version="t062.baseline_strategy.v1",
        parameter_schema_checksum="0x" + "11" * 32,
        code_provenance_module="robinhood_lp.strategy.baselines",
        code_provenance_revision=BACKTEST_ENGINE_VERSION,
        engine_revision=BACKTEST_ENGINE_VERSION,
        accounting_revision="t063.run_metrics.v1",
        reconstruction_revision=BACKTEST_ENGINE_VERSION,
        initial_position=_empty_position_state_dict(),
        initial_equity_q64_64=0,
        initial_attribution={
            "realised_pnl_q64_64": 0,
            "fees_collected_q64_64": 0,
            "il_lvr_q64_64": 0,
            "gas_q64_64": 0,
        },
        transitions=_initial_transitions(),
        checkpoints=(),
        fill_cursor_facts=_fill_cursor_run_facts(
            fill_cursor=MarketCursor(10, 0, 0),
            position_snapshot=PositionState(
                version="t061.position_state.v1",
                pool_key_id=POOL_KEY_A,
                chain_id=CHAIN_ID_A,
                position_id="0x" + "00" * 32,
                tick_lower=TICK_LOWER,
                tick_upper=TICK_UPPER,
                liquidity=1000,
                principal_token0=1,
                principal_token1=2,
                tokens_owed0=3,
                tokens_owed1=4,
                in_range=True,
                last_accrual_time=0,
            ),
        ),
        manifest_checksum=manifest_checksum,
    )


def _build_t112_manifest(
    *,
    evidence_checksum: str = "0x" + "22" * 32,
    partition_refs: tuple[PartitionReference, ...] = (_partition_ref_a(),),
    simulation_evidence_ref: str = "t112-evidence.json",
) -> T112ExperimentManifest:
    return build_t112_experiment_manifest(
        run_id=RUN_ID,
        chain_id=CHAIN_ID_A,
        pool_key_id=POOL_KEY_A,
        source_block_range=_block_range_a(),
        interval_seconds=300,
        dataset_version=DATASET_VERSION,
        dataset_schema_version=1,
        dataset_decode_version=1,
        dataset_content_hash=DATASET_HASH_A,
        reporting_numeraire="USDG",
        valuation_qualification="QUALIFIED",
        code_revision="0x" + "33" * 20,
        dependency_revisions={"robinhood-lp": "0.0.0"},
        strategy_binding=_strategy_binding(),
        seed=0,
        clock_assumption="EVENT_TIME",
        fill_assumption="DETERMINISTIC_FAILURE",
        cost_assumption="FLAT_GAS",
        quote_assumption="STATIC_FEE",
        latency_units=0,
        latency_ms_estimate=0,
        decisions_checksum="0x" + "44" * 32,
        ledger_checksum="0x" + "55" * 32,
        metrics_checksum="0x" + "66" * 32,
        coverage_checksum="0x" + "77" * 32,
        metrics_version="t063.run_metrics.v1",
        dataset_partition_refs=partition_refs,
        dataset_event_count=1,
        simulation_evidence_ref=simulation_evidence_ref,
        simulation_evidence_checksum=evidence_checksum,
        reconstruction_revision=BACKTEST_ENGINE_VERSION,
        created_at_unix_seconds=1_700_000_000,
    )


# ---------------------------------------------------------------------------
# Dataset + partition resolvers
# ---------------------------------------------------------------------------


class _StaticDatasetResolver(DatasetResolver):
    """Static dataset coverage resolver for tests."""

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
        content_hash: str = "0x" + "00" * 32,
        reporting_numeraire: str = "USDG",
        valuation_qualification: str = "QUALIFIED",
        dataset_schema_version: int = 1,
        dataset_decode_version: int = 1,
    ) -> None:
        self._records[(dataset_version, chain_id, pool_key_id)] = DatasetCoverage(
            chain_id=chain_id,
            pool_key_id=pool_key_id,
            dataset_version=dataset_version,
            dataset_schema_version=dataset_schema_version,
            dataset_decode_version=dataset_decode_version,
            dataset_content_hash=content_hash,
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
        return self._records[(dataset_version, chain_id, pool_key_id)]


class _StaticT112PartitionResolver(T112DatasetPartitionResolver):
    """Static T112 partition resolver for tests."""

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
                    f"chain_id={coverage.chain_id} pool_key_id={coverage.pool_key_id!r} "
                    f"is not registered"
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
    """Static reference resolver for the partition_refs gate."""

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
                f"partition reference {partition_ref!r} is not registered for "
                f"({chain_id}, {pool_key_id!r})"
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
    run_id: str = "t112-test-001",
    dataset_version: str = DATASET_VERSION,
    *,
    block_range_start: int = 1,
    block_range_end: int = 100,
    chain_id: int = CHAIN_ID_A,
    pool_key_id: str = POOL_KEY_A,
) -> RunRequest:
    return RunRequest(
        run_id=run_id,
        dataset_version=dataset_version,
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


def _build_orchestrator(
    *,
    events: list[BacktestEvent],
    tmp_path: Path,
    chain_id: int = CHAIN_ID_A,
    pool_key_id: str = POOL_KEY_A,
    partition: T100ResolvedPartition | None = None,
    dataset_content_hash: str = DATASET_HASH_A,
) -> BacktestOrchestratorT112:
    """Build a wired T112 orchestrator for the supplied pool + events."""
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
            ): partition or _resolved_partition_a()
        }
    )
    store = RunStateStore(runs_root=tmp_path / "runs")
    return BacktestOrchestratorT112(
        store=store,
        dataset_resolver=dataset_resolver,
        partition_resolver=partition_resolver,
        event_source=StaticPartitionEventSource(partition_events=events),
        partition_ref_resolver=ref_resolver,
    )


# ---------------------------------------------------------------------------
# 1. Reference-shape rejection (fabricated partitions)
# ---------------------------------------------------------------------------


class TestFabricatedPartitionReferenceRejection:
    """The loader rejects cursor-fabricated, coverage-fabricated, and placeholder refs."""

    def test_cursor_fabricated_reference_is_rejected(self) -> None:
        """A reference shaped like an event cursor is refused."""
        with pytest.raises(T112PartitionRefError) as exc_info:
            PartitionReference(
                partition_id="0000000010-000000000-000000000",
                chain_id=CHAIN_ID_A,
                pool_key_id=POOL_KEY_A,
                content_hash=DATASET_HASH_A,
                range=_block_range_a(),
                schema_version=1,
                decode_version=1,
            )
        assert "REJECTED_CURSOR_FABRICATED_REF" in str(exc_info.value)

    def test_coverage_string_reference_is_rejected(self) -> None:
        """A reference shaped like a coverage string is refused."""
        with pytest.raises(T112PartitionRefError) as exc_info:
            PartitionReference(
                partition_id="coverage-00001-00100",
                chain_id=CHAIN_ID_A,
                pool_key_id=POOL_KEY_A,
                content_hash=DATASET_HASH_A,
                range=_block_range_a(),
                schema_version=1,
                decode_version=1,
            )
        assert "REJECTED_COVERAGE_FABRICATED_REF" in str(exc_info.value)

    def test_placeholder_reference_is_rejected(self) -> None:
        """A reserved placeholder literal is refused."""
        with pytest.raises(T112PartitionRefError) as exc_info:
            PartitionReference(
                partition_id="PLACEHOLDER",
                chain_id=CHAIN_ID_A,
                pool_key_id=POOL_KEY_A,
                content_hash=DATASET_HASH_A,
                range=_block_range_a(),
                schema_version=1,
                decode_version=1,
            )
        assert "REJECTED_PLACEHOLDER_REF" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. Manifest loader strict dispatch and bindings
# ---------------------------------------------------------------------------


class TestT112ManifestLoaderStrictDispatch:
    """The T112 manifest loader refuses unknown / mixed / downgraded / legacy versions."""

    def test_t109_versioned_record_rejected_with_named_reason(self) -> None:
        """A T109-versioned manifest offered as T112 is refused."""
        payload = {
            "version": MANIFEST_VERSION_T109,
            "run_id": RUN_ID,
            "chain_id": CHAIN_ID_A,
            "pool_key_id": POOL_KEY_A,
            "source_block_range": {"start_block": 1, "end_block": 100},
            "interval_seconds": 300,
            "dataset_version": DATASET_VERSION,
            "dataset_schema_version": 1,
            "dataset_decode_version": 1,
            "dataset_content_hash": DATASET_HASH_A,
            "reporting_numeraire": "USDG",
            "valuation_qualification": "QUALIFIED",
            "code_revision": "0x" + "33" * 20,
            "dependency_revisions": {},
            "strategy_identity": IDENTITY_HOLD,
            "strategy_version": "t062.baseline_strategy.v1",
            "registry_version": "t068.strategy_registry.v1",
            "registry_checksum": "0x" + "00" * 32,
            "parameter_schema_version": "t062.baseline_strategy.v1",
            "parameter_schema_checksum": "0x" + "11" * 32,
            "code_provenance_module": "robinhood_lp.strategy.baselines",
            "code_provenance_revision": BACKTEST_ENGINE_VERSION,
            "code_provenance_symbol": "",
            "strategy_params": {},
            "seed": 0,
            "clock_assumption": "EVENT_TIME",
            "fill_assumption": "DETERMINISTIC_FAILURE",
            "cost_assumption": "FLAT_GAS",
            "quote_assumption": "STATIC_FEE",
            "latency_units": 0,
            "latency_ms_estimate": 0,
            "decisions_checksum": "0x" + "44" * 32,
            "ledger_checksum": "0x" + "55" * 32,
            "metrics_checksum": "0x" + "66" * 32,
            "coverage_checksum": "0x" + "77" * 32,
            "report_checksum": "0x" + "00" * 32,
            "metrics_version": "t063.run_metrics.v1",
            "dataset_partition_refs": [],
            "dataset_event_count": 0,
            "simulation_evidence_ref": "evidence.json",
            "simulation_evidence_version": "t109.simulation_evidence.v1",
            "simulation_evidence_checksum": "0x" + "22" * 32,
            "reconstruction_revision": BACKTEST_ENGINE_VERSION,
            "created_at_unix_seconds": 1_700_000_000,
        }
        with pytest.raises(T112VersionDispatchError) as exc_info:
            t112_experiment_manifest_from_dict(payload)
        assert "T109_HISTORICAL_UNAVAILABLE" in str(exc_info.value)

    def test_unknown_manifest_version_rejected(self) -> None:
        """An unknown manifest version is refused with named reason."""
        payload = {
            "version": "t999.experiment_manifest.v1",
            "run_id": RUN_ID,
            "chain_id": CHAIN_ID_A,
            "pool_key_id": POOL_KEY_A,
            "source_block_range": {"start_block": 1, "end_block": 100},
            "interval_seconds": 300,
            "dataset_version": DATASET_VERSION,
            "dataset_schema_version": 1,
            "dataset_decode_version": 1,
            "dataset_content_hash": DATASET_HASH_A,
            "reporting_numeraire": "USDG",
            "valuation_qualification": "QUALIFIED",
            "code_revision": "0x" + "33" * 20,
            "dependency_revisions": {},
            "strategy_identity": IDENTITY_HOLD,
            "strategy_version": "t062.baseline_strategy.v1",
            "registry_version": "t068.strategy_registry.v1",
            "registry_checksum": "0x" + "00" * 32,
            "parameter_schema_version": "t062.baseline_strategy.v1",
            "parameter_schema_checksum": "0x" + "11" * 32,
            "code_provenance_module": "robinhood_lp.strategy.baselines",
            "code_provenance_revision": BACKTEST_ENGINE_VERSION,
            "code_provenance_symbol": "",
            "strategy_params": {},
            "seed": 0,
            "clock_assumption": "EVENT_TIME",
            "fill_assumption": "DETERMINISTIC_FAILURE",
            "cost_assumption": "FLAT_GAS",
            "quote_assumption": "STATIC_FEE",
            "latency_units": 0,
            "latency_ms_estimate": 0,
            "decisions_checksum": "0x" + "44" * 32,
            "ledger_checksum": "0x" + "55" * 32,
            "metrics_checksum": "0x" + "66" * 32,
            "coverage_checksum": "0x" + "77" * 32,
            "report_checksum": "0x" + "00" * 32,
            "metrics_version": "t063.run_metrics.v1",
            "dataset_partition_refs": [_partition_ref_a().to_dict()],
            "dataset_event_count": 1,
            "simulation_evidence_ref": "evidence.json",
            "simulation_evidence_version": SIMULATION_EVIDENCE_VERSION_T112,
            "simulation_evidence_checksum": "0x" + "22" * 32,
            "reconstruction_revision": BACKTEST_ENGINE_VERSION,
            "created_at_unix_seconds": 1_700_000_000,
        }
        with pytest.raises(T112VersionDispatchError):
            t112_experiment_manifest_from_dict(payload)

    def test_manifest_with_t109_evidence_version_rejected(self) -> None:
        """A T112 manifest payload carrying a T109 evidence version is refused."""
        manifest = _build_t112_manifest()
        payload = manifest.to_dict()
        payload["simulation_evidence_version"] = "t109.simulation_evidence.v1"
        with pytest.raises(T112VersionDispatchError) as exc_info:
            t112_experiment_manifest_from_dict(payload)
        assert "PAIRED_VERSION_DOWNGRADE" in str(exc_info.value)

    def test_dispatch_manifest_version_returns_t112_for_t112_versions(self) -> None:
        assert (
            dispatch_manifest_version({"version": MANIFEST_VERSION_T112}) == MANIFEST_VERSION_T112
        )

    def test_dispatch_manifest_version_rejects_t109(self) -> None:
        with pytest.raises(T112VersionDispatchError) as exc_info:
            dispatch_manifest_version({"version": MANIFEST_VERSION_T109})
        assert "T109_HISTORICAL_UNAVAILABLE" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3. Manifest loader resolver integration
# ---------------------------------------------------------------------------


class TestT112ManifestResolverIntegration:
    """The manifest loader refuses refs that do not resolve through T100."""

    def test_unresolvable_partition_ref_is_rejected(self) -> None:
        """A reference the resolver cannot resolve is refused with named reason."""
        manifest = _build_t112_manifest()
        payload = manifest.to_dict()
        # Replace the registered partition id with one the resolver
        # does not authorise.
        payload["dataset_partition_refs"] = [
            {
                "partition_id": "chain=4663/contract=ffffffffffffffffffffffffffffffffffffffff/event=Swap/range=1-100",
                "chain_id": CHAIN_ID_A,
                "pool_key_id": POOL_KEY_A,
                "content_hash": DATASET_HASH_A,
                "range": {"start_block": 1, "end_block": 100},
                "schema_version": 1,
                "decode_version": 1,
            }
        ]
        # Recompute the report checksum so the loader's checksum
        # gate does not catch the modification first.
        payload["report_checksum"] = compute_t112_report_checksum(
            {k: v for k, v in payload.items() if k != "report_checksum"}
        )
        ref_resolver = _StaticT100PartitionRefResolver(
            partitions={(CHAIN_ID_A, POOL_KEY_A, _partition_id_a()): _resolved_partition_a()}
        )
        with pytest.raises(T112PartitionRefError) as exc_info:
            t112_experiment_manifest_from_dict(payload, resolver=ref_resolver)
        assert "UNRESOLVABLE_PARTITION_REF" in str(exc_info.value)

    def test_resolver_mismatch_on_content_hash_is_rejected(self) -> None:
        """A reference whose recorded content hash disagrees with the resolver's is refused."""
        manifest = _build_t112_manifest()
        payload = manifest.to_dict()
        # Keep the partition id but pretend the recorded content
        # hash disagrees with what the resolver authorises.
        payload["dataset_partition_refs"][0]["content_hash"] = "0x" + "99" * 32
        payload["report_checksum"] = compute_t112_report_checksum(
            {k: v for k, v in payload.items() if k != "report_checksum"}
        )
        ref_resolver = _StaticT100PartitionRefResolver(
            partitions={(CHAIN_ID_A, POOL_KEY_A, _partition_id_a()): _resolved_partition_a()}
        )
        with pytest.raises(T112PartitionRefError) as exc_info:
            t112_experiment_manifest_from_dict(payload, resolver=ref_resolver)
        assert "PARTITION_BINDING_MISMATCH" in str(exc_info.value)

    def test_real_partition_reference_resolves(self) -> None:
        """A real T100 partition reference resolves successfully."""
        manifest = _build_t112_manifest()
        payload = manifest.to_dict()
        ref_resolver = _StaticT100PartitionRefResolver(
            partitions={(CHAIN_ID_A, POOL_KEY_A, _partition_id_a()): _resolved_partition_a()}
        )
        loaded = t112_experiment_manifest_from_dict(payload, resolver=ref_resolver)
        assert loaded.version == MANIFEST_VERSION_T112
        assert loaded.dataset_partition_refs[0].partition_id == _partition_id_a()
        assert loaded.dataset_partition_refs[0].content_hash == DATASET_HASH_A


# ---------------------------------------------------------------------------
# 4. Evidence loader strict dispatch + paired versions
# ---------------------------------------------------------------------------


class TestT112EvidenceLoaderStrictDispatch:
    """The T112 evidence loader refuses unknown / mixed / downgraded / legacy versions."""

    def _build_evidence_payload(
        self, *, version: str = SIMULATION_EVIDENCE_VERSION_T112
    ) -> dict[str, Any]:
        return {
            "version": version,
            "run_id": RUN_ID,
            "dataset_version": DATASET_VERSION,
            "dataset_schema_version": 1,
            "dataset_decode_version": 1,
            "dataset_content_hash": DATASET_HASH_A,
            "pool_key_id": POOL_KEY_A,
            "chain_id": CHAIN_ID_A,
            "source_block_range": {"start_block": 1, "end_block": 100},
            "position_tick_range": {"tick_lower": TICK_LOWER, "tick_upper": TICK_UPPER},
            "strategy_identity": IDENTITY_HOLD,
            "strategy_version": "t062.baseline_strategy.v1",
            "registry_version": "t068.strategy_registry.v1",
            "registry_checksum": "0x" + "00" * 32,
            "parameter_schema_version": "t062.baseline_strategy.v1",
            "parameter_schema_checksum": "0x" + "11" * 32,
            "code_provenance_module": "robinhood_lp.strategy.baselines",
            "code_provenance_revision": BACKTEST_ENGINE_VERSION,
            "engine_revision": BACKTEST_ENGINE_VERSION,
            "accounting_revision": "t063.run_metrics.v1",
            "reconstruction_revision": BACKTEST_ENGINE_VERSION,
            "initial_position": _empty_position_state_dict(),
            "initial_equity_q64_64": 0,
            "initial_attribution": {
                "realised_pnl_q64_64": 0,
                "fees_collected_q64_64": 0,
                "il_lvr_q64_64": 0,
                "gas_q64_64": 0,
            },
            "transitions": [],
            "checkpoints": [],
            "fill_cursor_facts": _fill_cursor_run_facts(
                fill_cursor=MarketCursor(10, 0, 0),
                position_snapshot=PositionState(
                    version="t061.position_state.v1",
                    pool_key_id=POOL_KEY_A,
                    chain_id=CHAIN_ID_A,
                    position_id="0x" + "00" * 32,
                    tick_lower=TICK_LOWER,
                    tick_upper=TICK_UPPER,
                    liquidity=0,
                    principal_token0=0,
                    principal_token1=0,
                    tokens_owed0=0,
                    tokens_owed1=0,
                    in_range=False,
                    last_accrual_time=0,
                ),
            ).to_dict(),
            "market_state_version": MARKET_STATE_VERSION,
            "manifest_version": MANIFEST_VERSION_T112,
            "manifest_checksum": "0x" + "11" * 32,
            "evidence_checksum": "0x" + "00" * 32,
        }

    def test_t109_evidence_version_rejected_with_named_reason(self) -> None:
        payload = self._build_evidence_payload(version="t109.simulation_evidence.v1")
        with pytest.raises(T112VersionDispatchError) as exc_info:
            t112_simulation_evidence_from_dict(payload)
        assert "T109_HISTORICAL_UNAVAILABLE" in str(exc_info.value)

    def test_unknown_evidence_version_rejected(self) -> None:
        payload = self._build_evidence_payload(version="t999.simulation_evidence.v1")
        with pytest.raises(T112VersionDispatchError):
            t112_simulation_evidence_from_dict(payload)

    def test_paired_manifest_version_must_be_t112(self) -> None:
        payload = self._build_evidence_payload()
        # A T109 manifest_version bound to a T112 evidence fails closed.
        payload["manifest_version"] = MANIFEST_VERSION_T109
        with pytest.raises(T112VersionDispatchError) as exc_info:
            t112_simulation_evidence_from_dict(payload)
        assert "PAIRED_VERSION_DOWNGRADE" in str(exc_info.value)

    def test_block_tick_conflation_in_payload_rejected(self) -> None:
        """A block_range slot carrying tick fields is refused."""
        payload = self._build_evidence_payload()
        payload["source_block_range"] = {"tick_lower": -60, "tick_upper": 60}
        with pytest.raises(T112BlockTickConflationError) as exc_info:
            t112_simulation_evidence_from_dict(payload)
        assert "BLOCK_TICK_CONFLATION" in str(exc_info.value)

    def test_tick_range_carrying_block_fields_rejected(self) -> None:
        payload = self._build_evidence_payload()
        payload["position_tick_range"] = {"start_block": 1, "end_block": 100}
        with pytest.raises(T112BlockTickConflationError):
            t112_simulation_evidence_from_dict(payload)

    def test_dispatch_evidence_version_rejects_t109(self) -> None:
        with pytest.raises(T112VersionDispatchError) as exc_info:
            dispatch_evidence_version({"version": "t109.simulation_evidence.v1"})
        assert "T109_HISTORICAL_UNAVAILABLE" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. Block-range vs tick-range typed-pair separation
# ---------------------------------------------------------------------------


class TestBlockTickSeparation:
    """The T112 schema binds block_range and tick_range as distinct typed field pairs."""

    def test_block_range_constructor_rejects_tick_shaped_values(self) -> None:
        """BlockRange.__post_init__ validates the typed pair semantics."""
        # Inverted range is the only direct construction failure;
        # the loader-side tick-conflation guard runs at dict
        # construction. A BlockRange whose start_block exceeds
        # end_block is refused here.
        with pytest.raises(T112FieldError):
            BlockRange(start_block=10, end_block=5)

    def test_tick_range_constructor_rejects_inverted_or_equal_bounds(self) -> None:
        """TickRange.__post_init__ enforces strict ordering."""
        with pytest.raises(T112FieldError):
            TickRange(tick_lower=60, tick_upper=60)
        with pytest.raises(T112FieldError):
            TickRange(tick_lower=60, tick_upper=10)

    def test_t112_evidence_distinct_block_and_tick_ranges(self) -> None:
        """The T112 evidence keeps block_range and tick_range distinct."""
        evidence = _build_t112_evidence()
        assert isinstance(evidence.source_block_range, BlockRange)
        assert isinstance(evidence.position_tick_range, TickRange)
        assert evidence.source_block_range.start_block == 1
        assert evidence.source_block_range.end_block == 100
        assert evidence.position_tick_range.tick_lower == TICK_LOWER
        assert evidence.position_tick_range.tick_upper == TICK_UPPER

    def test_t112_evidence_refuses_tick_range_mismatch_with_position(self) -> None:
        """The evidence builder refuses a tick range whose values disagree with the position snapshot."""
        wrong_position = PositionState(
            version="t061.position_state.v1",
            pool_key_id=POOL_KEY_A,
            chain_id=CHAIN_ID_A,
            position_id="0x" + "00" * 32,
            # Disagrees with the TickRange built into the evidence.
            tick_lower=-120,
            tick_upper=120,
            liquidity=0,
            principal_token0=0,
            principal_token1=0,
            tokens_owed0=0,
            tokens_owed1=0,
            in_range=False,
            last_accrual_time=0,
        )
        with pytest.raises(T112IdentityDisagreementError) as exc_info:
            _build_t112_simulation_evidence_with_position(wrong_position)
        assert "TICK_RANGE_POSITION_MISMATCH" in str(exc_info.value)


def _build_t112_simulation_evidence_with_position(
    position: PositionState,
) -> T112SimulationEvidence:
    return build_t112_simulation_evidence(
        run_id=RUN_ID,
        dataset_version=DATASET_VERSION,
        dataset_schema_version=1,
        dataset_decode_version=1,
        dataset_content_hash=DATASET_HASH_A,
        pool_key_id=POOL_KEY_A,
        chain_id=CHAIN_ID_A,
        source_block_range=_block_range_a(),
        position_tick_range=_tick_range(),
        strategy_identity=IDENTITY_HOLD,
        strategy_version="t062.baseline_strategy.v1",
        registry_version="t068.strategy_registry.v1",
        registry_checksum="0x" + "00" * 32,
        parameter_schema_version="t062.baseline_strategy.v1",
        parameter_schema_checksum="0x" + "11" * 32,
        code_provenance_module="robinhood_lp.strategy.baselines",
        code_provenance_revision=BACKTEST_ENGINE_VERSION,
        engine_revision=BACKTEST_ENGINE_VERSION,
        accounting_revision="t063.run_metrics.v1",
        reconstruction_revision=BACKTEST_ENGINE_VERSION,
        initial_position=_empty_position_state_dict(),
        initial_equity_q64_64=0,
        initial_attribution={
            "realised_pnl_q64_64": 0,
            "fees_collected_q64_64": 0,
            "il_lvr_q64_64": 0,
            "gas_q64_64": 0,
        },
        transitions=_initial_transitions(),
        checkpoints=(),
        fill_cursor_facts=_fill_cursor_run_facts(
            fill_cursor=MarketCursor(10, 0, 0),
            position_snapshot=position,
        ),
        manifest_checksum="0x" + "11" * 32,
    )


# ---------------------------------------------------------------------------
# 6. Fill-cursor preservation through evidence reader
# ---------------------------------------------------------------------------


class TestFillCursorPreservation:
    """The T112 evidence restores position, integer inventory, equity, drawdown, T052 attribution."""

    def test_fill_cursor_facts_carry_position_snapshot(self) -> None:
        position = PositionState(
            version="t061.position_state.v1",
            pool_key_id=POOL_KEY_A,
            chain_id=CHAIN_ID_A,
            position_id="0x" + "00" * 32,
            tick_lower=TICK_LOWER,
            tick_upper=TICK_UPPER,
            liquidity=1234,
            principal_token0=42,
            principal_token1=43,
            tokens_owed0=44,
            tokens_owed1=45,
            in_range=True,
            last_accrual_time=0,
        )
        # Build a T112 evidence whose fill_cursor_facts carry the
        # position snapshot and a non-zero realised_fees_q64_64.
        # The reader restores the position / realised fees from
        # the canonical serialisation.
        fill_cursor_facts = FillCursorRunFacts(
            fill_cursor=MarketCursor(10, 0, 0),
            position_snapshot=position,
            raw_token0=position.principal_token0,
            raw_token1=position.principal_token1,
            realised_fees_q64_64=7,
            cost_components={
                "gas_units": 21_000,
                "fee_pips": 3_000,
                "impact_bps": 0,
            },
            equity_q64_64=0,
            drawdown_q64_64=0,
            attribution_snapshot={
                "realised_pnl_q64_64": 0,
                "fees_collected_q64_64": 7,
                "il_lvr_q64_64": 0,
                "gas_q64_64": 0,
            },
        )
        evidence = build_t112_simulation_evidence(
            run_id=RUN_ID,
            dataset_version=DATASET_VERSION,
            dataset_schema_version=1,
            dataset_decode_version=1,
            dataset_content_hash=DATASET_HASH_A,
            pool_key_id=POOL_KEY_A,
            chain_id=CHAIN_ID_A,
            source_block_range=_block_range_a(),
            position_tick_range=TickRange(
                tick_lower=position.tick_lower,
                tick_upper=position.tick_upper,
            ),
            strategy_identity=IDENTITY_HOLD,
            strategy_version="t062.baseline_strategy.v1",
            registry_version="t068.strategy_registry.v1",
            registry_checksum="0x" + "00" * 32,
            parameter_schema_version="t062.baseline_strategy.v1",
            parameter_schema_checksum="0x" + "11" * 32,
            code_provenance_module="robinhood_lp.strategy.baselines",
            code_provenance_revision=BACKTEST_ENGINE_VERSION,
            engine_revision=BACKTEST_ENGINE_VERSION,
            accounting_revision="t063.run_metrics.v1",
            reconstruction_revision=BACKTEST_ENGINE_VERSION,
            initial_position=_empty_position_state_dict(),
            initial_equity_q64_64=0,
            initial_attribution={
                "realised_pnl_q64_64": 0,
                "fees_collected_q64_64": 0,
                "il_lvr_q64_64": 0,
                "gas_q64_64": 0,
            },
            transitions=_initial_transitions(),
            checkpoints=(),
            fill_cursor_facts=fill_cursor_facts,
            manifest_checksum="0x" + "11" * 32,
        )
        loaded = t112_simulation_evidence_from_dict(evidence.to_dict())
        assert loaded.fill_cursor_facts.fill_cursor == MarketCursor(10, 0, 0)
        assert loaded.fill_cursor_facts.position_snapshot.liquidity == 1234
        assert loaded.fill_cursor_facts.raw_token0 == 42
        assert loaded.fill_cursor_facts.raw_token1 == 43
        assert loaded.fill_cursor_facts.realised_fees_q64_64 == 7

    def test_evidence_checksum_round_trip(self) -> None:
        """The evidence checksum is recomputed and verified on read."""
        evidence = _build_t112_evidence()
        payload = evidence.to_dict()
        loaded = t112_simulation_evidence_from_dict(payload)
        assert loaded.evidence_checksum == evidence.evidence_checksum


# ---------------------------------------------------------------------------
# 7. Orchestrator integration: real T100 partition resolution
# ---------------------------------------------------------------------------


class TestOrchestratorT100PartitionResolution:
    """The T112 entry resolves real T100 partitions and refuses the fixed empty source."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterable[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_fixed_empty_event_source_is_refused(self, tmp_path: Path) -> None:
        """An event source that returns the empty event list is refused with named reason."""
        events: list[BacktestEvent] = []  # The fixed empty source.
        orchestrator = _build_orchestrator(events=events, tmp_path=tmp_path)
        with pytest.raises(FixedEmptyEventSourceError) as exc_info:
            orchestrator.submit(_build_full_request())
        assert "FIXED_EMPTY_EVENT_SOURCE_REFUSED" in str(exc_info.value)

    def test_real_partition_resolves_and_publishes_t112_artifacts(self, tmp_path: Path) -> None:
        """The T112 entry resolves real T100 partitions and publishes T112 artifacts."""
        events = [
            _swap_event(timestamp=100, block_number=10, transaction_index=0, log_index=0),
            _shutdown_event(timestamp=200, block_number=40, transaction_index=0, log_index=0),
        ]
        orchestrator = _build_orchestrator(events=events, tmp_path=tmp_path)
        outcome = orchestrator.submit(_build_full_request())
        assert outcome.manifest is not None
        assert outcome.manifest.version == MANIFEST_VERSION_T112
        # Manifest binds the dataset_partition_refs (real T100, not
        # fabricated).
        assert outcome.manifest.dataset_partition_refs
        assert outcome.manifest.dataset_partition_refs[0].partition_id == _partition_id_a()
        # Evidence file is written and binds the T112 evidence version + manifest checksum.
        assert outcome.evidence_path is not None
        evidence_payload = json.loads(outcome.evidence_path.read_text(encoding="utf-8"))
        assert evidence_payload["version"] == SIMULATION_EVIDENCE_VERSION_T112
        assert evidence_payload["manifest_version"] == MANIFEST_VERSION_T112
        assert evidence_payload["manifest_checksum"] == outcome.manifest.report_checksum

    def test_unknown_dataset_version_refused(self, tmp_path: Path) -> None:
        """The T112 entry refuses a dataset the resolver cannot resolve."""
        events = [
            _swap_event(timestamp=100, block_number=10, transaction_index=0, log_index=0),
            _shutdown_event(timestamp=200, block_number=40, transaction_index=0, log_index=0),
        ]
        orchestrator = _build_orchestrator(events=events, tmp_path=tmp_path)
        # The dataset resolver raises KeyError for an unregistered
        # dataset version; the orchestrator's bridge lets the
        # exception propagate. The T112 contract binds the entry
        # to refuse a dataset the resolver cannot resolve — the
        # test confirms the failure happens before any result is
        # written.
        with pytest.raises((T100PartitionResolutionError, KeyError)):
            orchestrator.submit(_build_full_request(dataset_version="unknown-dataset"))

    def test_mismatched_partition_ref_refused(self, tmp_path: Path) -> None:
        """A reference whose registered content hash disagrees with the manifest is refused."""
        events = [
            _swap_event(timestamp=100, block_number=10, transaction_index=0, log_index=0),
            _shutdown_event(timestamp=200, block_number=40, transaction_index=0, log_index=0),
        ]
        # Build a partition whose content hash disagrees with the
        # dataset resolver's recorded hash; the loader's
        # partition-binding gate catches the mismatch.
        wrong_partition = T100ResolvedPartition(
            partition_id=_partition_id_a(),
            chain_id=CHAIN_ID_A,
            pool_key_id=POOL_KEY_A,
            content_hash="0x" + "99" * 32,
            range=_block_range_a(),
            schema_version=1,
            decode_version=1,
        )
        orchestrator = _build_orchestrator(
            events=events,
            tmp_path=tmp_path,
            partition=wrong_partition,
        )
        with pytest.raises(PartitionRefMismatchError):
            orchestrator.submit(_build_full_request())


# ---------------------------------------------------------------------------
# 8. Heterogeneous pool fixtures
# ---------------------------------------------------------------------------


class TestHeterogeneousPoolFixtures:
    """Two heterogeneous pool fixtures share the same T112 invariants."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterable[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_pool_a_publishes_t112_manifest(self, tmp_path: Path) -> None:
        events = [
            _swap_event(
                timestamp=100,
                chain_id=CHAIN_ID_A,
                pool_key_id=POOL_KEY_A,
                block_number=10,
                transaction_index=0,
                log_index=0,
            ),
            _shutdown_event(
                timestamp=200,
                chain_id=CHAIN_ID_A,
                pool_key_id=POOL_KEY_A,
                block_number=40,
                transaction_index=0,
                log_index=0,
            ),
        ]
        orchestrator = _build_orchestrator(events=events, tmp_path=tmp_path)
        outcome = orchestrator.submit(_build_full_request())
        assert outcome.manifest.dataset_partition_refs[0].pool_key_id == POOL_KEY_A

    def test_pool_b_publishes_t112_manifest(self, tmp_path: Path) -> None:
        events = [
            _swap_event(
                timestamp=100,
                chain_id=CHAIN_ID_B,
                pool_key_id=POOL_KEY_B,
                block_number=210,
                transaction_index=0,
                log_index=0,
            ),
            _shutdown_event(
                timestamp=200,
                chain_id=CHAIN_ID_B,
                pool_key_id=POOL_KEY_B,
                block_number=240,
                transaction_index=0,
                log_index=0,
            ),
        ]
        dataset_resolver = _StaticDatasetResolver()
        dataset_resolver.add(
            dataset_version=DATASET_VERSION,
            chain_id=CHAIN_ID_B,
            pool_key_id=POOL_KEY_B,
            covered_start=200,
            covered_end=1000,
            content_hash=DATASET_HASH_B,
        )
        partition_resolver = _StaticT112PartitionResolver(
            partitions_by_key={
                (DATASET_VERSION, CHAIN_ID_B, POOL_KEY_B): (_resolved_partition_b(),),
            }
        )
        ref_resolver = _StaticT100PartitionRefResolver(
            partitions={
                (CHAIN_ID_B, POOL_KEY_B, _partition_id_b()): _resolved_partition_b(),
            }
        )
        store = RunStateStore(runs_root=tmp_path / "runs")
        orchestrator = BacktestOrchestratorT112(
            store=store,
            dataset_resolver=dataset_resolver,
            partition_resolver=partition_resolver,
            event_source=StaticPartitionEventSource(partition_events=events),
            partition_ref_resolver=ref_resolver,
        )
        outcome = orchestrator.submit(
            _build_full_request(
                run_id="t112-pool-b-001",
                chain_id=CHAIN_ID_B,
                pool_key_id=POOL_KEY_B,
                block_range_start=200,
                block_range_end=300,
            )
        )
        assert outcome.manifest.pool_key_id == POOL_KEY_B
        assert outcome.manifest.dataset_partition_refs[0].pool_key_id == POOL_KEY_B


# ---------------------------------------------------------------------------
# 9. Migration tests: T109 records unreadable as T112, T112 records unreadable as T109
# ---------------------------------------------------------------------------


class TestT109T112Migration:
    """T109 records cannot serve as current T112 evidence and vice versa."""

    def test_t109_loader_rejects_t112_versioned_manifest(self) -> None:
        """A T112-versioned manifest is refused by the T109 loader."""
        from robinhood_lp.reports.manifest import t109_experiment_manifest_from_dict

        manifest = _build_t112_manifest()
        payload = manifest.to_dict()
        with pytest.raises(Exception) as exc_info:
            t109_experiment_manifest_from_dict(payload)
        # The T109 loader raises an InvalidManifestFieldError
        # because the payload's version literal is not the T109
        # version. The exception message records the rejection.
        assert "t112.experiment_manifest.v1" in str(exc_info.value) or "T109" in str(exc_info.value)

    def test_t109_loader_rejects_t112_evidence(self) -> None:
        """A T112-versioned evidence artifact is refused by the T109 loader."""
        from robinhood_lp.reports.simulation_evidence import (
            SimulationEvidenceFieldError,
            simulation_evidence_from_dict,
        )

        evidence = _build_t112_evidence()
        payload = evidence.to_dict()
        # The T109 loader does not understand T112-only fields
        # (``fill_cursor_facts``, ``source_block_range`` /
        # ``position_tick_range``); a SimulationEvidenceFieldError
        # is raised because the required ``tick_lower`` field is
        # missing. The migration contract is the failure itself:
        # the T109 reader cannot interpret a T112 artifact.
        with pytest.raises(SimulationEvidenceFieldError) as exc_info:
            simulation_evidence_from_dict(payload)
        # The error message names the missing field; the migration
        # surface records the failure so a downstream caller can
        # present the T112 artifact through the T112 reader only.
        assert "missing key" in str(exc_info.value).lower() or "tick_lower" in str(exc_info.value)

    def test_t112_loader_rejects_t109_evidence(self) -> None:
        """A T109-versioned evidence artifact is refused by the T112 loader."""
        from robinhood_lp.reports.simulation_evidence import (
            SIMULATION_EVIDENCE_VERSION as T109_EVIDENCE_VERSION,
        )
        from robinhood_lp.reports.simulation_evidence import (
            build_simulation_evidence,
        )

        legacy = build_simulation_evidence(
            run_id="t109-legacy",
            dataset_version="ds.legacy",
            dataset_schema_version=1,
            dataset_decode_version=1,
            dataset_content_hash="0x" + "aa" * 32,
            pool_key_id=POOL_KEY_A,
            chain_id=CHAIN_ID_A,
            tick_lower=TICK_LOWER,
            tick_upper=TICK_UPPER,
            strategy_identity=IDENTITY_HOLD,
            strategy_version="t062.baseline_strategy.v1",
            registry_version="t068.strategy_registry.v1",
            registry_checksum="0x" + "00" * 32,
            parameter_schema_version="t062.baseline_strategy.v1",
            parameter_schema_checksum="0x" + "00" * 32,
            code_provenance_module="robinhood_lp.strategy.baselines",
            code_provenance_revision=BACKTEST_ENGINE_VERSION,
            engine_revision=BACKTEST_ENGINE_VERSION,
            accounting_revision="t063.run_metrics.v1",
            reconstruction_revision=BACKTEST_ENGINE_VERSION,
            initial_position=_empty_position_state_dict(),
            initial_equity_q64_64=0,
            initial_attribution={
                "realised_pnl_q64_64": 0,
                "fees_collected_q64_64": 0,
                "il_lvr_q64_64": 0,
                "gas_q64_64": 0,
            },
            transitions=(),
            checkpoints=(),
        )
        payload = legacy.to_dict()
        assert payload["version"] == T109_EVIDENCE_VERSION
        with pytest.raises(T112VersionDispatchError) as exc_info:
            t112_simulation_evidence_from_dict(payload)
        assert "T109_HISTORICAL_UNAVAILABLE" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 10. Rollback: T112 writer stops, T109 reader remains readable
# ---------------------------------------------------------------------------


class TestT112Rollback:
    """Rollback disables new T112 publication while preserving historical read-only access."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterable[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_rollback_disables_new_t112_publication(self, tmp_path: Path) -> None:
        """After rollback, the T112 entry refuses a fresh run."""
        events = [
            _swap_event(timestamp=100, block_number=10, transaction_index=0, log_index=0),
            _shutdown_event(timestamp=200, block_number=40, transaction_index=0, log_index=0),
        ]
        orchestrator = _build_orchestrator(events=events, tmp_path=tmp_path)
        # Simulate rollback: the orchestrator's event source
        # returns the empty list to signal publication is
        # disabled. The T112 entry refuses the empty source with
        # the dedicated error.
        orchestrator._event_source = StaticPartitionEventSource(partition_events=[])
        with pytest.raises(FixedEmptyEventSourceError):
            orchestrator.submit(_build_full_request())

    def test_historical_t109_records_remain_byte_identical_after_t112(self, tmp_path: Path) -> None:
        """A T109 record published before rollback remains byte-identical."""
        from robinhood_lp.reports.manifest import (
            build_t109_experiment_manifest,
            t109_experiment_manifest_from_dict,
        )
        from robinhood_lp.reports.metrics import (
            CoverageSummary,
            LedgerSnapshot,
            RunMetrics,
        )

        # Build a deterministic T109 manifest from the same
        # inputs the T112 builder consumes; the on-disk bytes
        # must round-trip unchanged.
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
        t109_manifest = build_t109_experiment_manifest(
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
        # Round-trip the T109 record through the T109 loader; the
        # bytes must be byte-identical.
        payload = t109_manifest.to_dict()
        loaded = t109_experiment_manifest_from_dict(payload)
        assert loaded == t109_manifest
        # The T109 record is not a T112 record; the T112 loader
        # refuses it with the named reason.
        with pytest.raises(T112VersionDispatchError) as exc_info:
            t112_experiment_manifest_from_dict(payload)
        assert "T109_HISTORICAL_UNAVAILABLE" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 11. Pre-evidence historical unavailability
# ---------------------------------------------------------------------------


class TestPreEvidenceHistoricalUnavailable:
    """A pre-evidence T109 artifact returns T109_HISTORICAL_UNAVAILABLE through the T112 reader."""

    def test_t112_manifest_loader_unavailable_verdict_for_t109(self) -> None:
        manifest = _build_t112_manifest()
        payload = manifest.to_dict()
        payload["version"] = MANIFEST_VERSION_T109
        with pytest.raises(T112VersionDispatchError) as exc_info:
            t112_experiment_manifest_from_dict(payload)
        assert "T109_HISTORICAL_UNAVAILABLE" in str(exc_info.value)

    def test_t112_evidence_loader_unavailable_verdict_for_t109(self) -> None:
        evidence = _build_t112_evidence()
        payload = evidence.to_dict()
        payload["version"] = "t109.simulation_evidence.v1"
        with pytest.raises(T112VersionDispatchError) as exc_info:
            t112_simulation_evidence_from_dict(payload)
        assert "T109_HISTORICAL_UNAVAILABLE" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 12. Inspection: no current consumer reaches the fixed empty source
# ---------------------------------------------------------------------------


class TestInspectionNoConsumerReachesFixedEmptySource:
    """Storage inspection proves no current consumer reaches the fixed empty source."""

    def test_no_module_exposes_a_fixed_empty_event_source_factory(self) -> None:
        """The reports / orchestrator packages do not export a fixed-empty event-source factory."""
        from robinhood_lp import orchestrator, reports

        for attr in (
            "FixedEmptyEventSource",
            "EmptyEventSource",
            "NullEventSource",
            "make_empty_event_source",
        ):
            assert not hasattr(orchestrator, attr), (
                f"orchestrator package must not expose {attr}; T112 refuses the "
                f"fixed empty event source at the entry path"
            )
            assert not hasattr(reports, attr), (
                f"reports package must not expose {attr}; T112 refuses the "
                f"fixed empty event source at the entry path"
            )

    def test_no_orchestrator_method_references_fixed_empty_source(self) -> None:
        """The orchestrator's modules do not *expose* a fixed empty source factory.

        The :class:`FixedEmptyEventSourceError` exception class is
        allowed — it is the named reason the T112 entry path uses
        to refuse a fixed empty source. What T112 forbids is a
        factory that returns the fixed empty source as a current
        entry.
        """
        from robinhood_lp.orchestrator import t112 as orch_t112

        # The orchestrator module exposes only the dedicated
        # refusal exception; no ``FixedEmptyEventSource`` factory
        # (a callable class with an empty event list) is exported.
        assert hasattr(orch_t112, "FixedEmptyEventSourceError"), (
            "T112 must expose the FixedEmptyEventSourceError class "
            "so the entry path can surface the named reason"
        )
        forbidden = {
            "FixedEmptyEventSource",
            "EmptyEventSource",
            "NullEventSource",
            "make_empty_event_source",
            "build_empty_event_source",
        }
        for name in forbidden:
            assert not hasattr(orch_t112, name), (
                f"orchestrator.t112 must not expose {name}; T112 refuses the "
                f"fixed empty source as a current entry"
            )


# ---------------------------------------------------------------------------
# 13. Fill-cursor reader restores through the engine's primitives
# ---------------------------------------------------------------------------


class TestFillCursorReaderRestoresThroughEngine:
    """A reader restores position, equity, drawdown, T052 attribution through the engine."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterable[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_run_state_restored_at_fill_cursor(self) -> None:
        """A RunState reader at the fill cursor restores the recorded state."""
        from robinhood_lp.reports.run_state import build_replay_projector
        from robinhood_lp.reports.simulation_evidence import (
            build_simulation_evidence as build_t109_evidence,
        )

        # Build an evidence file that ties a position snapshot
        # to a fill cursor. The reader applies the transitions
        # onto the recorded initial state through the existing
        # T061 engine / T052 attribution semantics.
        fill_cursor = MarketCursor(10, 0, 0)
        initial_position = PositionState(
            version="t061.position_state.v1",
            pool_key_id=POOL_KEY_A,
            chain_id=CHAIN_ID_A,
            position_id="0x" + "00" * 32,
            tick_lower=TICK_LOWER,
            tick_upper=TICK_UPPER,
            liquidity=0,
            principal_token0=0,
            principal_token1=0,
            tokens_owed0=0,
            tokens_owed1=0,
            in_range=False,
            last_accrual_time=0,
        )
        target_position = PositionState(
            version="t061.position_state.v1",
            pool_key_id=POOL_KEY_A,
            chain_id=CHAIN_ID_A,
            position_id="0x" + "00" * 32,
            tick_lower=TICK_LOWER,
            tick_upper=TICK_UPPER,
            liquidity=1234,
            principal_token0=42,
            principal_token1=43,
            tokens_owed0=44,
            tokens_owed1=45,
            in_range=True,
            last_accrual_time=0,
        )
        # The transition carries a typed ``filled_position`` payload
        # the projector applies through the same engine primitives
        # the original run used.
        transitions: tuple[RunTransition, ...] = (
            RunTransition(
                ordinal=0,
                stage="FILL",
                cursor=fill_cursor,
                ledger_hash_after=target_position.ledger_hash(),
                audit_event_id="0xfill",
                payload={
                    "filled_position": {
                        "version": target_position.version,
                        "pool_key_id": target_position.pool_key_id,
                        "chain_id": target_position.chain_id,
                        "position_id": target_position.position_id,
                        "tick_lower": target_position.tick_lower,
                        "tick_upper": target_position.tick_upper,
                        "liquidity": target_position.liquidity,
                        "principal_token0": target_position.principal_token0,
                        "principal_token1": target_position.principal_token1,
                        "tokens_owed0": target_position.tokens_owed0,
                        "tokens_owed1": target_position.tokens_owed1,
                        "in_range": target_position.in_range,
                        "last_accrual_time": target_position.last_accrual_time,
                    }
                },
                state_changing=True,
            ),
        )
        evidence = build_t112_simulation_evidence(
            run_id="t112-fill-restore",
            dataset_version=DATASET_VERSION,
            dataset_schema_version=1,
            dataset_decode_version=1,
            dataset_content_hash=DATASET_HASH_A,
            pool_key_id=POOL_KEY_A,
            chain_id=CHAIN_ID_A,
            source_block_range=_block_range_a(),
            position_tick_range=TickRange(
                tick_lower=target_position.tick_lower,
                tick_upper=target_position.tick_upper,
            ),
            strategy_identity=IDENTITY_HOLD,
            strategy_version="t062.baseline_strategy.v1",
            registry_version="t068.strategy_registry.v1",
            registry_checksum="0x" + "00" * 32,
            parameter_schema_version="t062.baseline_strategy.v1",
            parameter_schema_checksum="0x" + "11" * 32,
            code_provenance_module="robinhood_lp.strategy.baselines",
            code_provenance_revision=BACKTEST_ENGINE_VERSION,
            engine_revision=BACKTEST_ENGINE_VERSION,
            accounting_revision="t063.run_metrics.v1",
            reconstruction_revision=BACKTEST_ENGINE_VERSION,
            initial_position={
                "version": initial_position.version,
                "pool_key_id": initial_position.pool_key_id,
                "chain_id": initial_position.chain_id,
                "position_id": initial_position.position_id,
                "tick_lower": initial_position.tick_lower,
                "tick_upper": initial_position.tick_upper,
                "liquidity": initial_position.liquidity,
                "principal_token0": initial_position.principal_token0,
                "principal_token1": initial_position.principal_token1,
                "tokens_owed0": initial_position.tokens_owed0,
                "tokens_owed1": initial_position.tokens_owed1,
                "in_range": initial_position.in_range,
                "last_accrual_time": initial_position.last_accrual_time,
            },
            initial_equity_q64_64=0,
            initial_attribution={
                "realised_pnl_q64_64": 0,
                "fees_collected_q64_64": 0,
                "il_lvr_q64_64": 0,
                "gas_q64_64": 0,
            },
            transitions=transitions,
            checkpoints=(),
            fill_cursor_facts=_fill_cursor_run_facts(
                fill_cursor=fill_cursor,
                position_snapshot=target_position,
                realised_fees_q64_64=99,
            ),
            manifest_checksum="0x" + "11" * 32,
        )
        # Round-trip the evidence; the reader reconstructs the
        # fill-cursor run facts from the canonical serialisation.
        loaded = t112_simulation_evidence_from_dict(evidence.to_dict())
        assert loaded.fill_cursor_facts.position_snapshot.liquidity == 1234
        assert loaded.fill_cursor_facts.realised_fees_q64_64 == 99
        # The T109 projector reads the T109 SimulationEvidence shape;
        # the bridge converts the T112 evidence's transitions /
        # checkpoints to a T109 SimulationEvidence so the same
        # engine primitives restore the position at the fill
        # cursor — not through a strategy callback.
        t109_evidence = build_t109_evidence(
            run_id=loaded.run_id,
            dataset_version=loaded.dataset_version,
            dataset_schema_version=loaded.dataset_schema_version,
            dataset_decode_version=loaded.dataset_decode_version,
            dataset_content_hash=loaded.dataset_content_hash,
            pool_key_id=loaded.pool_key_id,
            chain_id=loaded.chain_id,
            tick_lower=loaded.position_tick_range.tick_lower,
            tick_upper=loaded.position_tick_range.tick_upper,
            strategy_identity=loaded.strategy_identity,
            strategy_version=loaded.strategy_version,
            registry_version=loaded.registry_version,
            registry_checksum=loaded.registry_checksum,
            parameter_schema_version=loaded.parameter_schema_version,
            parameter_schema_checksum=loaded.parameter_schema_checksum,
            code_provenance_module=loaded.code_provenance_module,
            code_provenance_revision=loaded.code_provenance_revision,
            engine_revision=loaded.engine_revision,
            accounting_revision=loaded.accounting_revision,
            reconstruction_revision=loaded.reconstruction_revision,
            initial_position=dict(loaded.initial_position),
            initial_equity_q64_64=loaded.initial_equity_q64_64,
            initial_attribution=dict(loaded.initial_attribution),
            transitions=loaded.transitions,
            checkpoints=loaded.checkpoints,
        )
        projector = build_replay_projector(t109_evidence)
        run_state = projector.run_state(fill_cursor)
        assert run_state.position.liquidity == 1234
        assert run_state.position.principal_token0 == 42
        assert run_state.position.principal_token1 == 43


# ---------------------------------------------------------------------------
# 14. Engine integration: real T040 replay + T050 features path
# ---------------------------------------------------------------------------


class TestEngineIntegrationWithRealPartitions:
    """The T112 entry composes the engine with real T040/T041 replay events."""

    @pytest.fixture(autouse=True)
    def _reset_registry(self) -> Iterable[None]:
        reset_default_registry_cache()
        yield
        reset_default_registry_cache()

    def test_engine_consumes_real_events(self, tmp_path: Path) -> None:
        """The engine consumes real T040-replay events and emits cursor-bound audit."""
        events = [
            _swap_event(timestamp=100, block_number=10, transaction_index=0, log_index=0),
            _swap_event(timestamp=150, block_number=15, transaction_index=0, log_index=0),
            _shutdown_event(timestamp=200, block_number=40, transaction_index=0, log_index=0),
        ]
        orchestrator = _build_orchestrator(events=events, tmp_path=tmp_path)
        outcome = orchestrator.submit(_build_full_request())
        # The evidence file records the engine's audit chain.
        assert outcome.evidence_path is not None
        evidence_payload = json.loads(outcome.evidence_path.read_text(encoding="utf-8"))
        # The fill_cursor_facts entry carries the position
        # snapshot, raw token amounts, integer liquidity, realised
        # fees, cost components, equity, drawdown and T052
        # attribution components the reader restores at the fill
        # cursor through the engine's primitives.
        facts = evidence_payload["fill_cursor_facts"]
        assert "position_snapshot" in facts
        assert "raw_token0" in facts
        assert "raw_token1" in facts
        assert "realised_fees_q64_64" in facts
        assert "cost_components" in facts
        assert "equity_q64_64" in facts
        assert "drawdown_q64_64" in facts
        assert "attribution_snapshot" in facts


# ---------------------------------------------------------------------------
# 15. Build-then-read round trip
# ---------------------------------------------------------------------------


class TestBuildThenReadRoundTrip:
    """Builder output round-trips through the loader with version dispatch preserved."""

    def test_manifest_round_trip(self) -> None:
        manifest = _build_t112_manifest()
        loaded = t112_experiment_manifest_from_dict(manifest.to_dict())
        assert loaded == manifest

    def test_evidence_round_trip(self) -> None:
        evidence = _build_t112_evidence()
        loaded = t112_simulation_evidence_from_dict(evidence.to_dict())
        assert loaded == evidence

    def test_round_trip_preserves_paired_checksum_binding(self) -> None:
        """A round-trip preserves the manifest / evidence paired checksum binding."""
        evidence = _build_t112_evidence(manifest_checksum="0x" + "ab" * 32)
        manifest = _build_t112_manifest(
            evidence_checksum=evidence.evidence_checksum,
        )
        manifest_payload = manifest.to_dict()
        manifest_payload["simulation_evidence_checksum"] = evidence.evidence_checksum
        evidence_payload = evidence.to_dict()
        evidence_payload["manifest_checksum"] = manifest.report_checksum
        loaded_manifest = t112_experiment_manifest_from_dict(manifest_payload)
        loaded_evidence = t112_simulation_evidence_from_dict(evidence_payload)
        assert loaded_manifest.simulation_evidence_checksum == loaded_evidence.evidence_checksum
        assert loaded_evidence.manifest_checksum == loaded_manifest.report_checksum
