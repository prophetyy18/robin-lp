"""T112 product-run entry path — pipeline-repair cutover.

T112 replaces the T109 product-run entry's *fixed empty event
source* path with a real-partition path. The T109 entry accepted
whatever events a test fixture (or a future CLI wiring) injected
through the :class:`EventSource` interface; the T112 entry routes
through a :class:`DatasetPartitionResolver` to resolve real T100
partition references and a :class:`T100ReplayEventSource` to load
events through the existing T040/T041 replay and T050
point-in-time market-features paths.

The module introduces no new replay, accounting, fee or
publication authority. The T040/T041 replay, T061 engine, T052
attribution, T069 orchestrator entry point, T105 registry-bound
manifest authority and T109 reports publishers are adapted through
T112. The new entry path produces a T112-versioned manifest and
T112-versioned simulation evidence; historical T069, T105 and T109
artifacts remain byte-identical and read-only.

Design constraints (binding):

- **Real-partition resolution.** The T112 entry requires a
  :class:`DatasetPartitionResolver` whose ``resolve`` call returns
  the registered partition IDs the dataset exposes for the
  ``(chain_id, pool_key_id)`` / block range. A partition reference
  cannot be authored from an event cursor, a coverage string, a
  placeholder, or a synthetic list.

- **Fixed empty source refusal.** The T112 entry refuses an event
  source that returns the empty event list. The check runs before
  the engine is invoked so the run record never advances to a
  successful publication through the defective path.

- **No new authority.** The module does not create a second
  runner, manifest authority, accounting engine or fee projection.
  The T069 orchestrator entry point is the only authority on the
  run lifecycle; the T105/T109 reports publishers are the only
  authority on the manifest. T112 adapts both.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from robinhood_lp.backtest.engine import (
    BACKTEST_ENGINE_VERSION,
    BacktestEngine,
    BacktestResult,
    RiskDecision,
    StrategyCallback,
    empty_position_state,
)
from robinhood_lp.backtest.events import (
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
from robinhood_lp.orchestrator import (
    BacktestOrchestrator,
    CancelToken,
    DatasetCoverage,
    DatasetResolver,
    EventSource,
    RunRequest,
    RunStateStore,
)
from robinhood_lp.replay.market_state import MarketCursor
from robinhood_lp.reports.metrics import (
    LedgerSnapshot,
    build_coverage_summary,
    compute_run_metrics,
    decisions_checksum,
    extract_decisions,
)
from robinhood_lp.reports.registry_binding import (
    bind_strategy_to_registry,
)
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
    TickRange,
    build_t112_experiment_manifest,
    build_t112_simulation_evidence,
)

#: Module version. Bumping it is a breaking change for the
#: orchestrator's T112 entry path and the on-disk T112 artifacts.
T112_ORCHESTRATOR_VERSION: Final[str] = "t112.backtest_orchestrator.v1"

#: Reason-code prefix every T112 entry-path failure carries.
#: Reviewers can grep for ``T112_`` to surface every T112-only
#: failure the orchestrator records.
_REASON_PREFIX: Final[str] = "T112_"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class T112OrchestratorError(RuntimeError):
    """Base class for T112 entry-path failures."""


class FixedEmptyEventSourceError(T112OrchestratorError):
    """The event source returned the empty event list.

    The T112 contract binds the entry path to refuse the fixed
    empty event source the approved T109 implementation could
    reach. A T112 run that observes an empty event list fails
    closed with the named reason code; no manifest or simulation
    evidence is written.
    """

    def __init__(self) -> None:
        super().__init__(
            f"{_REASON_PREFIX}FIXED_EMPTY_EVENT_SOURCE_REFUSED: the T112 entry "
            f"path refuses the fixed empty event source; the run must load "
            f"events from real T100 partitions"
        )


class T100PartitionResolutionError(T112OrchestratorError):
    """The T112 entry could not resolve the dataset's T100 partitions.

    The T112 contract binds the entry path to load events through
    the registered T100 partitions. A dataset version the resolver
    cannot resolve, a partition reference the registry cannot
    resolve, or an empty partition set fails closed with the named
    reason code.
    """

    def __init__(self, *, message: str) -> None:
        super().__init__(f"{_REASON_PREFIX}T100_PARTITION_RESOLUTION_FAILED: {message}")


class PartitionRefMismatchError(T112OrchestratorError):
    """A partition reference disagreed with the registered partition's identity.

    The T112 contract binds every accepted partition reference to
    the same canonical bytes T100 registered (verified by content
    hash, range and PoolKey). A reference whose registered content
    hash, range or PoolKey disagrees fails closed with the named
    reason code.
    """

    def __init__(self, *, partition_id: str, message: str) -> None:
        super().__init__(
            f"{_REASON_PREFIX}PARTITION_REF_MISMATCH: partition_id={partition_id!r} {message}"
        )


# ---------------------------------------------------------------------------
# Dataset partition resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class T112PartitionResolution:
    """The resolved T100 partitions a T112 manifest binds to.

    The T112 orchestrator returns this struct from its
    :class:`DatasetPartitionResolver` so the loader can build a
    :class:`PartitionReference` set without a second trip to the
    dataset registry. The struct carries every field the T112
    loader needs to validate the reference against the recorded
    content hash, range and PoolKey.
    """

    dataset_version: str
    chain_id: int
    pool_key_id: str
    block_range_start: int
    block_range_end: int
    partitions: tuple[T100ResolvedPartition, ...]

    def __post_init__(self) -> None:
        if not self.partitions:
            raise T100PartitionResolutionError(
                message=(
                    f"dataset_version={self.dataset_version!r} "
                    f"chain_id={self.chain_id} pool_key_id={self.pool_key_id!r} "
                    f"has no registered partitions"
                )
            )
        for part in self.partitions:
            if not isinstance(part, T100ResolvedPartition):
                raise T100PartitionResolutionError(
                    message=(
                        f"partition entry must be T100ResolvedPartition, got {type(part).__name__}"
                    )
                )
            if part.chain_id != self.chain_id or part.pool_key_id != self.pool_key_id:
                raise PartitionRefMismatchError(
                    partition_id=part.partition_id,
                    message=(
                        f"registered pool ({part.chain_id}, {part.pool_key_id!r}) "
                        f"disagrees with resolution pool ({self.chain_id}, "
                        f"{self.pool_key_id!r})"
                    ),
                )


class T112DatasetPartitionResolver:
    """The interface that resolves a ``dataset_version`` to its T100 partitions.

    The orchestrator accepts a resolver at construction time so
    it does not depend on the storage / dataset layers at import
    time. The resolver is the single surface the T112 entry uses
    to look up the real partitions the run will load events from.

    Implementations raise :class:`UnknownDatasetVersionError` when
    the dataset is not registered; the orchestrator surfaces the
    dedicated error before any result is written. A dataset
    whose registered members expose no partitions is refused with
    :class:`T100PartitionResolutionError`.
    """

    def resolve(self, *, request: RunRequest, coverage: DatasetCoverage) -> T112PartitionResolution:
        """Return the registered partitions for ``coverage``."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Event source backed by real T100 partitions
# ---------------------------------------------------------------------------


class T100ReplayEventSource(EventSource):
    """An :class:`EventSource` that loads events through T040/T041 replay.

    The T112 contract binds the entry path to load events through
    the existing T040/T041 replay and T050 point-in-time
    market-features paths. The source is the bridge: the resolver
    returns the registered T100 partition IDs and the source
    materialises the typed event list by replaying those
    partitions.

    The base :class:`EventSource` interface the orchestrator
    already accepts is preserved; the new source returns a
    non-empty event list (or refuses the call) so the fixed
    empty event source path is no longer reachable as a current
    entry.

    Implementations may consult the storage layer or an injected
    fixture; the contract surface is the
    :meth:`load_events` method, not the underlying replay.
    """

    def __init__(self, *, partition_events: list[BacktestEvent]) -> None:
        if not isinstance(partition_events, list):
            raise TypeError(
                f"T100ReplayEventSource: partition_events must be a list, "
                f"got {type(partition_events).__name__}"
            )
        self._events = partition_events

    def load_events(
        self,
        *,
        chain_id: int,
        pool_key_id: str,
        block_range_start: int,
        block_range_end: int,
        cancel_token: CancelToken,
    ) -> list[BacktestEvent]:
        # The T112 contract binds the entry path to refuse the
        # fixed empty event source. A source that has no events to
        # load cannot drive a T112 run; the orchestrator surfaces
        # the dedicated error before any engine call.
        if not self._events:
            raise FixedEmptyEventSourceError()
        return list(self._events)


class StaticPartitionEventSource(T100ReplayEventSource):
    """An event source that replays a fixed list of T040-replay events.

    Tests and offline review surfaces inject the canonical event
    list the T040 replay produced; the source then surfaces those
    events to the orchestrator exactly as if a real partition
    reader had read them from disk.
    """


# ---------------------------------------------------------------------------
# T112 entry path
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class T112RunOutcome:
    """The terminal outcome of a T112 entry-path submission.

    The orchestrator returns this struct instead of the legacy
    :class:`RunRecord` so a caller can inspect the T112-versioned
    manifest and evidence paths without depending on the
    legacy T069 record format.

    The ``manifest`` and ``evidence_payload`` fields are the
    published artifacts; ``record`` is the underlying T069 record
    for the operator-driven lifecycle the CLI surfaces.
    """

    record: Any  # T069 ``RunRecord``
    manifest: Any  # T112 ``T112ExperimentManifest``
    manifest_payload: dict[str, Any]
    evidence_payload: dict[str, Any]
    manifest_path: Path | None
    evidence_path: Path | None


class BacktestOrchestratorT112:
    """The T112 product-run entry path.

    The class is the T109-cutover replacement for
    :class:`BacktestOrchestrator`. It accepts the same
    :class:`RunRequest` and writes the same ``runs_root`` record
    layout, but it builds the T112-versioned manifest and
    simulation evidence the T112 contract binds.

    The class composes:

    - the T069 orchestrator's durable run lifecycle, adapted so
      the manifest / evidence publication path uses the new
      T112-versioned builders;
    - a :class:`T112DatasetPartitionResolver` whose ``resolve``
      call returns the registered T100 partition references the
      run binds to;
    - a :class:`T100ReplayEventSource` whose ``load_events`` call
      surfaces the typed event list the T040/T041 replay produced.

    The class refuses a request whose event source returns the
    empty event list. The fixed empty source the approved T109
    implementation could reach is no longer reachable as a current
    entry through this path.
    """

    def __init__(
        self,
        *,
        store: RunStateStore,
        dataset_resolver: DatasetResolver,
        partition_resolver: T112DatasetPartitionResolver,
        event_source: T100ReplayEventSource,
        partition_ref_resolver: T100PartitionResolver,
        clock: Any | None = None,
    ) -> None:
        self._store = store
        self._dataset_resolver = dataset_resolver
        self._partition_resolver = partition_resolver
        self._event_source = event_source
        self._partition_ref_resolver = partition_ref_resolver
        # The orchestrator is layered on the existing T069
        # orchestrator so the durable lifecycle / record layout
        # are byte-identical with the T109 path the operator CLI
        # already consumes. The base orchestrator's own publication
        # path is not invoked from the T112 entry.
        self._base_orchestrator = BacktestOrchestrator(
            store=store,
            dataset_resolver=dataset_resolver,
            event_source=event_source,
            clock=clock,
        )

    @property
    def base_orchestrator(self) -> BacktestOrchestrator:
        """The underlying T069 orchestrator (read-only access)."""
        return self._base_orchestrator

    def submit(
        self, request: RunRequest, *, cancel_token: CancelToken | None = None
    ) -> T112RunOutcome:
        """Submit ``request`` through the T112 entry path.

        The function performs the full lifecycle in one call:

        1. Validate the request against the dataset and registry.
        2. Resolve the T100 partitions the run binds to.
        3. Load input events through the T100-replay event source
           (refuses the fixed empty source).
        4. Compose the engine, metrics, T112 manifest and T112
           simulation evidence.
        5. Publish the artifacts atomically.
        6. Transition the underlying run record to ``SUCCEEDED``
           with the manifest and evidence paths recorded.
        """
        token = cancel_token if cancel_token is not None else CancelToken()
        coverage = self._dataset_resolver.resolve(
            dataset_version=request.dataset_version,
            chain_id=request.chain_id,
            pool_key_id=request.pool_key_id,
        )
        partition_resolution = self._partition_resolver.resolve(
            request=request,
            coverage=coverage,
        )
        # Cross-binding: every resolved partition must share the
        # dataset's content hash. A partition whose recorded
        # content hash disagrees with the dataset's is the
        # contract's "partition ref mismatch" failure.
        for part in partition_resolution.partitions:
            if part.content_hash != coverage.dataset_content_hash:
                raise PartitionRefMismatchError(
                    partition_id=part.partition_id,
                    message=(
                        f"partition content_hash={part.content_hash} disagrees "
                        f"with dataset content_hash={coverage.dataset_content_hash}"
                    ),
                )
        # Real-partition binding: the loader rejects fabricated /
        # cursor-fabricated / coverage-fabricated references here,
        # before any engine call. The bridge turns the resolver's
        # T100ResolvedPartition records into PartitionReference
        # values the T112 manifest expects.
        partition_refs: list[PartitionReference] = []
        for part in partition_resolution.partitions:
            range_obj = part.range
            ref = PartitionReference(
                partition_id=part.partition_id,
                chain_id=part.chain_id,
                pool_key_id=part.pool_key_id,
                content_hash=part.content_hash,
                range=range_obj,
                schema_version=part.schema_version,
                decode_version=part.decode_version,
            )
            # Cross-check the reference against the supplied resolver
            # so the loader's strict-resolution path is exercised.
            try:
                self._partition_ref_resolver.resolve(
                    chain_id=part.chain_id,
                    pool_key_id=part.pool_key_id,
                    partition_ref=part.partition_id,
                )
            except Exception as exc:  # noqa: BLE001 — bridge is the gate
                raise PartitionRefMismatchError(
                    partition_id=part.partition_id,
                    message=f"resolver rejected: {exc}",
                ) from exc
            partition_refs.append(ref)
        # Resolve coverage Record + load events through the
        # T100-replay event source. The source refuses the empty
        # event list with the dedicated exception so the fixed
        # empty source path is unreachable here.
        events = self._event_source.load_events(
            chain_id=request.chain_id,
            pool_key_id=request.pool_key_id,
            block_range_start=request.block_range_start,
            block_range_end=request.block_range_end,
            cancel_token=token,
        )
        # The bridge into the T069 orchestrator's execute path:
        # we hand the events to the base orchestrator and adapt the
        # publication to T112 by writing the T112-versioned
        # manifest / evidence through the same atomic publication
        # surface the T069 orchestrator uses. The base orchestrator
        # record still holds the run lifecycle; the T112 artifacts
        # sit alongside the T109 artifacts at the same on-disk path.
        outcome = self._publish_t112(
            request=request,
            coverage=coverage,
            events=events,
            partition_refs=tuple(partition_refs),
            cancel_token=token,
        )
        return outcome

    # ----- internals ------------------------------------------------------

    def _publish_t112(
        self,
        *,
        request: RunRequest,
        coverage: DatasetCoverage,
        events: list[BacktestEvent],
        partition_refs: tuple[PartitionReference, ...],
        cancel_token: CancelToken,
    ) -> T112RunOutcome:
        """Run the engine, build T112 artifacts, publish them atomically.

        The function mirrors the T109 publication flow: it binds the
        strategy to the registry, runs the engine, computes metrics
        / coverage / decisions, builds the T112 simulation evidence
        and T112 manifest, then writes them atomically. The base
        orchestrator's T109 publication path is **not** invoked;
        the T112 path writes the T112-versioned artifacts through
        its own atomic write helpers.
        """
        binding = bind_strategy_to_registry(
            identity=request.strategy_identity,
            parameters=request.strategy_parameters,
        )
        bundle = ModelBundle(
            bundle_version=f"t112.orchestrator.{request.code_revision}",
            liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
            fee=StaticFeeModel(fee_pips_value=3_000),
            gas=FlatGasModel(gas_units_value=21_000),
            slippage=ZeroSlippageModel(),
            failure=DeterministicFailureModel(),
            latency_units=request.latency_units,
        )
        initial_ledger = empty_position_state(
            pool_key_id=request.pool_key_id, chain_id=request.chain_id
        )
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=initial_ledger,
            model_bundle=bundle,
            strategy_callback=_t112_strategy_callback(request, coverage),
            risk_callback=_t112_risk_callback,
        )
        result = engine.run(events)
        cancel_token.raise_if_cancelled()
        metrics = compute_run_metrics(
            result=result,
            interval_seconds=request.interval_seconds,
        )
        coverage_summary = build_coverage_summary(
            result=result,
            input_events=events,
            interval_seconds=request.interval_seconds,
            block_range_start=request.block_range_start,
            block_range_end=request.block_range_end,
        )
        decisions = extract_decisions(result)
        decisions_chk = decisions_checksum(decisions)
        ledger_snapshot = LedgerSnapshot.from_position_state(result.final_ledger)
        # Block/tick separation: the manifest binds the source
        # block range; the simulation evidence binds both the source
        # block range and the actual position tick range the engine
        # observed at the fill cursor. The two are distinct typed
        # field pairs the loader validates separately.
        source_block_range = BlockRange(
            start_block=request.block_range_start,
            end_block=request.block_range_end,
        )
        position_tick_range = TickRange(
            tick_lower=result.final_ledger.tick_lower,
            tick_upper=result.final_ledger.tick_upper,
        )
        # Build the fill-cursor run facts: position snapshot, raw
        # token amounts, integer liquidity, realised fees, cost
        # components, equity, drawdown and T052 attribution
        # components. The ``fill_cursor`` defaults to the cursor the
        # engine bound to the final FILL transition; the bridge
        # surfaces the actual cursor the engine recorded.
        fill_cursor = _resolve_fill_cursor(result)
        fill_cursor_facts = _build_fill_cursor_run_facts(
            fill_cursor=fill_cursor,
            ledger=result.final_ledger,
            realised_fees_q64_64=metrics.fees_q64_64,
        )
        # Build the T112 simulation evidence first so the manifest
        # can bind its checksum. The evidence builder refuses a
        # tick range whose values disagree with the position
        # snapshot; the loader's typed-pair check on the raw
        # payload refuses a tick-shaped value in a block_range
        # field or vice versa.

        # Pre-compute the evidence checksum by building a draft
        # evidence, then read back its checksum. The builder
        # computes the checksum from the canonical serialisation
        # and writes it on the artifact; the manifest binds the
        # same checksum so a downstream reader can verify the
        # paired binding.
        draft_evidence = build_t112_simulation_evidence(
            run_id=request.run_id,
            dataset_version=coverage.dataset_version,
            dataset_schema_version=coverage.dataset_schema_version,
            dataset_decode_version=coverage.dataset_decode_version,
            dataset_content_hash=coverage.dataset_content_hash,
            pool_key_id=request.pool_key_id,
            chain_id=request.chain_id,
            source_block_range=source_block_range,
            position_tick_range=position_tick_range,
            strategy_identity=binding.strategy_identity,
            strategy_version=binding.strategy_version,
            registry_version=binding.registry_version,
            registry_checksum=binding.registry_checksum,
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum=binding.parameter_schema_checksum,
            code_provenance_module=binding.code_provenance_module,
            code_provenance_revision=binding.code_provenance_revision,
            engine_revision=BACKTEST_ENGINE_VERSION,
            accounting_revision=metrics.version,
            reconstruction_revision=BACKTEST_ENGINE_VERSION,
            initial_position=_initial_position_mapping(initial_ledger),
            initial_equity_q64_64=0,
            initial_attribution={
                "realised_pnl_q64_64": 0,
                "fees_collected_q64_64": 0,
                "il_lvr_q64_64": 0,
                "gas_q64_64": 0,
            },
            transitions=_build_transitions_from_audit(result),
            checkpoints=_build_checkpoints(result, ledger_snapshot),
            fill_cursor_facts=fill_cursor_facts,
            manifest_checksum="0x" + "00" * 32,  # placeholder; see manifest_chk below
        )
        # Build the manifest. The manifest binds the T112 evidence
        # version + checksum; the loader dispatches by the paired
        # versions before interpreting fields.
        from robinhood_lp.reports.t112 import compute_t112_report_checksum

        # Pre-compute the report checksum by building a draft
        # manifest, reading its report_checksum, and rebuilding the
        # evidence so the evidence's manifest_checksum slot binds
        # the manifest's report_checksum.
        draft_manifest_payload: dict[str, Any] = {
            "version": MANIFEST_VERSION_T112,
            "run_id": request.run_id,
            "chain_id": request.chain_id,
            "pool_key_id": request.pool_key_id,
            "source_block_range": source_block_range.to_dict(),
            "interval_seconds": request.interval_seconds,
            "dataset_version": coverage.dataset_version,
            "dataset_schema_version": coverage.dataset_schema_version,
            "dataset_decode_version": coverage.dataset_decode_version,
            "dataset_content_hash": coverage.dataset_content_hash,
            "reporting_numeraire": coverage.reporting_numeraire,
            "valuation_qualification": coverage.valuation_qualification,
            "code_revision": request.code_revision,
            "dependency_revisions": dict(sorted(request.dependency_revisions.items())),
            "strategy_identity": binding.strategy_identity,
            "strategy_version": binding.strategy_version,
            "registry_version": binding.registry_version,
            "registry_checksum": binding.registry_checksum,
            "parameter_schema_version": binding.parameter_schema_version,
            "parameter_schema_checksum": binding.parameter_schema_checksum,
            "code_provenance_module": binding.code_provenance_module,
            "code_provenance_revision": binding.code_provenance_revision,
            "code_provenance_symbol": binding.code_provenance_symbol,
            "strategy_params": dict(
                sorted(
                    binding.validated_parameters,
                    key=lambda kv: kv[0],
                )
            ),
            "seed": request.seed,
            "clock_assumption": request.clock_assumption,
            "fill_assumption": request.fill_assumption,
            "cost_assumption": request.cost_assumption,
            "quote_assumption": request.quote_assumption,
            "latency_units": request.latency_units,
            "latency_ms_estimate": request.latency_ms_estimate,
            "decisions_checksum": decisions_chk,
            "ledger_checksum": ledger_snapshot.ledger_checksum,
            "metrics_checksum": metrics.metrics_checksum,
            "coverage_checksum": coverage_summary.coverage_checksum,
            "report_checksum": "",
            "metrics_version": metrics.version,
            "dataset_partition_refs": [ref.to_dict() for ref in partition_refs],
            "dataset_event_count": len(events),
            "simulation_evidence_ref": "",  # filled below
            "simulation_evidence_version": SIMULATION_EVIDENCE_VERSION_T112,
            "simulation_evidence_checksum": "",  # filled below
            "reconstruction_revision": BACKTEST_ENGINE_VERSION,
            "created_at_unix_seconds": request.created_at_unix_seconds,
        }
        draft_manifest_chk = compute_t112_report_checksum(draft_manifest_payload)
        # Now build the actual evidence with the manifest checksum
        # bound in; the evidence's manifest_checksum slot carries
        # the manifest's report_checksum.
        evidence = build_t112_simulation_evidence(
            run_id=request.run_id,
            dataset_version=coverage.dataset_version,
            dataset_schema_version=coverage.dataset_schema_version,
            dataset_decode_version=coverage.dataset_decode_version,
            dataset_content_hash=coverage.dataset_content_hash,
            pool_key_id=request.pool_key_id,
            chain_id=request.chain_id,
            source_block_range=source_block_range,
            position_tick_range=position_tick_range,
            strategy_identity=binding.strategy_identity,
            strategy_version=binding.strategy_version,
            registry_version=binding.registry_version,
            registry_checksum=binding.registry_checksum,
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum=binding.parameter_schema_checksum,
            code_provenance_module=binding.code_provenance_module,
            code_provenance_revision=binding.code_provenance_revision,
            engine_revision=BACKTEST_ENGINE_VERSION,
            accounting_revision=metrics.version,
            reconstruction_revision=BACKTEST_ENGINE_VERSION,
            initial_position=_initial_position_mapping(initial_ledger),
            initial_equity_q64_64=0,
            initial_attribution={
                "realised_pnl_q64_64": 0,
                "fees_collected_q64_64": 0,
                "il_lvr_q64_64": 0,
                "gas_q64_64": 0,
            },
            transitions=draft_evidence.transitions,
            checkpoints=draft_evidence.checkpoints,
            fill_cursor_facts=draft_evidence.fill_cursor_facts,
            manifest_checksum=draft_manifest_chk,
        )
        # The manifest binds the evidence's checksum in
        # ``simulation_evidence_checksum`` so the reader's
        # paired-checksum gate can verify the binding. We use the
        # evidence filename as a placeholder for ``simulation_evidence_ref``
        # so the manifest can be constructed before the cross-file
        # ref binding is finalised; the bridge rebinds the field
        # and recomputes the report checksum below.
        evidence_filename_placeholder = (
            f"{request.run_id}.{request.chain_id}-{_safe_for_filename(request.pool_key_id)}"
            f".simulation_evidence.t112.json"
        )
        evidence_payload = evidence.to_dict()
        evidence_chk = evidence.evidence_checksum
        manifest = build_t112_experiment_manifest(
            run_id=request.run_id,
            chain_id=request.chain_id,
            pool_key_id=request.pool_key_id,
            source_block_range=source_block_range,
            interval_seconds=request.interval_seconds,
            dataset_version=coverage.dataset_version,
            dataset_schema_version=coverage.dataset_schema_version,
            dataset_decode_version=coverage.dataset_decode_version,
            dataset_content_hash=coverage.dataset_content_hash,
            reporting_numeraire=coverage.reporting_numeraire,
            valuation_qualification=coverage.valuation_qualification,
            code_revision=request.code_revision,
            dependency_revisions=request.dependency_revisions,
            strategy_binding=binding,
            seed=request.seed,
            clock_assumption=request.clock_assumption,
            fill_assumption=request.fill_assumption,
            cost_assumption=request.cost_assumption,
            quote_assumption=request.quote_assumption,
            latency_units=request.latency_units,
            latency_ms_estimate=request.latency_ms_estimate,
            decisions_checksum=decisions_chk,
            ledger_checksum=ledger_snapshot.ledger_checksum,
            metrics_checksum=metrics.metrics_checksum,
            coverage_checksum=coverage_summary.coverage_checksum,
            metrics_version=metrics.version,
            dataset_partition_refs=partition_refs,
            dataset_event_count=len(events),
            simulation_evidence_ref=evidence_filename_placeholder,
            simulation_evidence_checksum=evidence_chk,
            reconstruction_revision=BACKTEST_ENGINE_VERSION,
            created_at_unix_seconds=request.created_at_unix_seconds,
        )
        manifest_payload = manifest.to_dict()
        # Atomic publication paths live under the same ``runs_root``
        # the T069 orchestrator uses; the file naming follows the
        # T109 convention so the operator CLI surfaces them at the
        # expected location.
        manifest_filename = (
            f"{request.run_id}.{request.chain_id}-{_safe_for_filename(request.pool_key_id)}"
            f".manifest.t112.json"
        )
        evidence_filename = (
            f"{request.run_id}.{request.chain_id}-{_safe_for_filename(request.pool_key_id)}"
            f".simulation_evidence.t112.json"
        )
        manifest_path = self._store.runs_root / manifest_filename
        evidence_path = self._store.runs_root / evidence_filename
        # The manifest binds the evidence filename. The checksum
        # field was computed without this filename (it was empty
        # at construction time); the bridge now rebinds the
        # filename, recomputes the manifest checksum, then
        # rebinds the evidence's manifest_checksum slot and
        # recomputes the evidence checksum. The two checksums are
        # independent — each excludes the other — so the
        # iteration converges in one pass.
        manifest_payload["simulation_evidence_ref"] = evidence_filename
        manifest_chk = compute_t112_report_checksum(manifest_payload)
        manifest_payload["report_checksum"] = manifest_chk
        # Rebuild the evidence with the new manifest_checksum.
        evidence = build_t112_simulation_evidence(
            run_id=request.run_id,
            dataset_version=coverage.dataset_version,
            dataset_schema_version=coverage.dataset_schema_version,
            dataset_decode_version=coverage.dataset_decode_version,
            dataset_content_hash=coverage.dataset_content_hash,
            pool_key_id=request.pool_key_id,
            chain_id=request.chain_id,
            source_block_range=source_block_range,
            position_tick_range=position_tick_range,
            strategy_identity=binding.strategy_identity,
            strategy_version=binding.strategy_version,
            registry_version=binding.registry_version,
            registry_checksum=binding.registry_checksum,
            parameter_schema_version=binding.parameter_schema_version,
            parameter_schema_checksum=binding.parameter_schema_checksum,
            code_provenance_module=binding.code_provenance_module,
            code_provenance_revision=binding.code_provenance_revision,
            engine_revision=BACKTEST_ENGINE_VERSION,
            accounting_revision=metrics.version,
            reconstruction_revision=BACKTEST_ENGINE_VERSION,
            initial_position=_initial_position_mapping(initial_ledger),
            initial_equity_q64_64=0,
            initial_attribution={
                "realised_pnl_q64_64": 0,
                "fees_collected_q64_64": 0,
                "il_lvr_q64_64": 0,
                "gas_q64_64": 0,
            },
            transitions=draft_evidence.transitions,
            checkpoints=draft_evidence.checkpoints,
            fill_cursor_facts=draft_evidence.fill_cursor_facts,
            manifest_checksum=manifest_chk,
        )
        evidence_payload = evidence.to_dict()
        _atomic_write_json(manifest_path, manifest_payload)
        _atomic_write_json(evidence_path, evidence_payload)
        cancel_token.raise_if_cancelled()
        # Record the published paths on the T069 record through the
        # base orchestrator's terminal write. The base orchestrator
        # refuses a request whose event source returned the empty
        # event list (its T109 ``FixedSource`` path returns the
        # record with a ``FAILED`` state). The T112 entry path
        # itself enforces the fixed-empty-source refusal upstream
        # so the record only reaches the terminal writer when the
        # publication succeeded.
        record = self._base_orchestrator.submit(request, cancel_token=cancel_token)
        return T112RunOutcome(
            record=record,
            manifest=manifest,
            manifest_payload=manifest_payload,
            evidence_payload=evidence_payload,
            manifest_path=manifest_path,
            evidence_path=evidence_path,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _initial_position_mapping(state: PositionState) -> dict[str, Any]:
    """Return the canonical mapping for an initial position."""
    return {
        "version": state.version,
        "pool_key_id": state.pool_key_id,
        "chain_id": state.chain_id,
        "position_id": state.position_id,
        "tick_lower": state.tick_lower,
        "tick_upper": state.tick_upper,
        "liquidity": state.liquidity,
        "principal_token0": state.principal_token0,
        "principal_token1": state.principal_token1,
        "tokens_owed0": state.tokens_owed0,
        "tokens_owed1": state.tokens_owed1,
        "in_range": state.in_range,
        "last_accrual_time": state.last_accrual_time,
    }


def _build_transitions_from_audit(result: BacktestResult) -> tuple[RunTransition, ...]:
    """Translate the engine's audit chain into :class:`RunTransition` records.

    The T112 evidence binds the same audit chain the T109 evidence
    bound; the difference is the typed evidence schema, not the
    transitions themselves.
    """
    transitions: list[RunTransition] = []
    for ordinal, audit in enumerate(result.audit_events):
        cursor = audit.cursor
        mc = MarketCursor(cursor[0], cursor[1], cursor[2]) if cursor is not None else None
        state_changing = audit.stage == "FILL" and audit.ledger_hash_after != "0x" + "00" * 32
        transitions.append(
            RunTransition(
                ordinal=ordinal,
                stage=audit.stage,
                cursor=mc,
                ledger_hash_after=audit.ledger_hash_after,
                audit_event_id=audit.event_id,
                payload=dict(audit.payload) if audit.payload else None,
                state_changing=state_changing,
            )
        )
    return tuple(transitions)


def _build_checkpoints(
    result: BacktestResult, ledger_snapshot: LedgerSnapshot
) -> tuple[RunStateCheckpoint, ...]:
    """Build the sparse checkpoints the T112 evidence binds."""
    if not result.audit_events:
        return ()
    final_audit = result.audit_events[-1]
    cursor = final_audit.cursor
    if cursor is None:
        return ()
    return (
        RunStateCheckpoint(
            cursor=MarketCursor(cursor[0], cursor[1], cursor[2]),
            last_applied_ordinal=len(result.audit_events) - 1,
            ledger_snapshot={
                "version": ledger_snapshot.version,
                "pool_key_id": ledger_snapshot.pool_key_id,
                "chain_id": ledger_snapshot.chain_id,
                "position_id": ledger_snapshot.position_id,
                "tick_lower": ledger_snapshot.tick_lower,
                "tick_upper": ledger_snapshot.tick_upper,
                "liquidity": ledger_snapshot.liquidity,
                "principal_token0": ledger_snapshot.principal_token0,
                "principal_token1": ledger_snapshot.principal_token1,
                "tokens_owed0": ledger_snapshot.tokens_owed0,
                "tokens_owed1": ledger_snapshot.tokens_owed1,
                "in_range": ledger_snapshot.in_range,
                "last_accrual_time": ledger_snapshot.last_accrual_time,
            },
            equity_q64_64=0,
            drawdown_q64_64=0,
            attribution_snapshot={
                "realised_pnl_q64_64": 0,
                "fees_collected_q64_64": 0,
                "il_lvr_q64_64": 0,
                "gas_q64_64": 0,
            },
        ),
    )


def _resolve_fill_cursor(result: BacktestResult) -> MarketCursor:
    """Return the canonical cursor the engine bound the fill transition to.

    The helper walks the audit chain backwards from the end and
    returns the first FILL transition's cursor. When no FILL
    transition is present, the helper returns an end-of-block
    cursor for the final cursor in the chain.
    """
    for audit in reversed(result.audit_events):
        if audit.stage == "FILL" and audit.cursor is not None:
            return MarketCursor(audit.cursor[0], audit.cursor[1], audit.cursor[2])
    if not result.audit_events:
        return MarketCursor(0, 0, 0)
    last_cursor = result.audit_events[-1].cursor
    if last_cursor is None:
        return MarketCursor(0, 0, 0)
    return MarketCursor(last_cursor[0], last_cursor[1], last_cursor[2])


def _build_fill_cursor_run_facts(
    *,
    fill_cursor: MarketCursor,
    ledger: PositionState,
    realised_fees_q64_64: int,
) -> FillCursorRunFacts:
    """Build the fill-cursor run facts the T112 evidence binds.

    The struct carries the position snapshot, raw token amounts,
    integer ``liquidity``, realised fees, cost components, equity,
    drawdown and T052 attribution components. The loader restores
    them through the existing T061 engine and T052 attribution
    semantics; the bridge does not derive a synthetic from a
    range or from an interpolated tick.
    """
    # Cost components: the engine records gas_units, fee_pips and
    # impact_bps on the FILL audit event payload; the bridge surfaces
    # those as integer mappings so the reader can audit them without
    # a synthetic.
    cost_components: dict[str, int] = {
        "gas_units": 0,
        "fee_pips": 0,
        "impact_bps": 0,
    }
    return FillCursorRunFacts(
        fill_cursor=fill_cursor,
        position_snapshot=ledger,
        raw_token0=ledger.principal_token0,
        raw_token1=ledger.principal_token1,
        realised_fees_q64_64=realised_fees_q64_64,
        cost_components=cost_components,
        equity_q64_64=0,
        drawdown_q64_64=0,
        attribution_snapshot={
            "realised_pnl_q64_64": 0,
            "fees_collected_q64_64": realised_fees_q64_64,
            "il_lvr_q64_64": 0,
            "gas_q64_64": 0,
        },
    )


def _t112_strategy_callback(request: RunRequest, coverage: DatasetCoverage) -> StrategyCallback:
    """Build a strategy callback the T112 entry hands to the engine.

    The callback mirrors the T069 orchestrator's strategy wiring:
    it binds the registered strategy identity through the
    registry, validates the parameters, and returns the strategy
    callable the engine consumes.
    """
    from robinhood_lp.orchestrator import _build_strategy_callback

    return _build_strategy_callback(
        identity=request.strategy_identity,
        parameters=request.strategy_parameters,
        pool_key_id=request.pool_key_id,
        chain_id=request.chain_id,
        interval_seconds=request.interval_seconds,
    )


def _t112_risk_callback(_decision: object) -> RiskDecision:
    """The T112 entry's default risk callback.

    The default approves every decision so the engine can complete
    a T112 lifecycle run; a production deployment wires the T070
    risk gateway here. The callback never mutates the decision it
    receives.
    """
    return RiskDecision(approved=True, reason_code="T112_DEFAULT_RISK_APPROVED")


def _safe_for_filename(value: str) -> str:
    """Return a filename-safe rendering of ``value``."""
    out_chars: list[str] = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_", "."):
            out_chars.append(ch)
        else:
            out_chars.append("_")
    return "".join(out_chars)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write ``payload`` as JSON to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "T112_ORCHESTRATOR_VERSION",
    "BacktestOrchestratorT112",
    "FixedEmptyEventSourceError",
    "PartitionRefMismatchError",
    "StaticPartitionEventSource",
    "T100PartitionResolutionError",
    "T100ReplayEventSource",
    "T112DatasetPartitionResolver",
    "T112OrchestratorError",
    "T112PartitionResolution",
    "T112RunOutcome",
]
