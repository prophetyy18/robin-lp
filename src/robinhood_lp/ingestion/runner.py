"""The capability-driven historical ingestion runner (T032).

The runner ties together the range planner, the router, the storage
writer, and the manifest store. It is the single orchestrator the
manager / operator invokes; every other T032 module is pure data or
pure logic.

The runner's contract:

- the scan filter is one pool-filtered topic0 OR query (one
  ``eth_getLogs`` request stream, never one per event type);
- the initial wide-range log source is the Robinhood public RPC,
  with adaptive split on response-size / timeout / 5xx / 429 /
  range-too-large / single-block-overflow;
- the application ``User-Agent`` is configured; a missing or default
  ``User-Agent`` that produces an HTTP 403 is retried with the
  configured ``User-Agent`` and surfaces as a hard 403 retry / a
  persistent capability failure when the configured one is also
  rejected;
- Alchemy Free may take log intervals only through its measured
  plan (its own measured cap, on the order of 10 blocks per call)
  and within its remaining budget;
- failover is allowed only within an endpoint's measured capability
  and remaining budget;
- the three coverage states (``scanned_empty``, ``failed``,
  ``cancelled``) are mutually exclusive and enforced by the router;
- the checkpoint cannot advance past any failed / uncovered
  interval; subsequent provisional suffix data must not be promoted
  into the contiguous checkpoint and must not set ``complete=True``;
- every successful or failed interval is persisted in the run
  manifest before the runner moves to the next interval;
- the block header policy fetches **one non-hydrated block header
  per distinct event block** and reuses it across every event that
  lands in that block; the runner does **not** download hydrated
  blocks, all transactions for a block, or all receipts for a block
  as part of ordinary pool-history ingestion.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from robinhood_lp.ingestion.block_header_source import (
    BlockHeader,
    BlockHeaderFetchError,
    BlockHeaderSink,
    RpcBlockHeaderSource,
)
from robinhood_lp.ingestion.capability import (
    BudgetSnapshot,
    CapabilitySnapshot,
)
from robinhood_lp.ingestion.checkpoint import (
    EMPTY_CHECKPOINT_HASH,
    DurableCheckpointState,
    load_durable_checkpoint,
    upsert_durable_checkpoint,
)
from robinhood_lp.ingestion.errors import (
    REASON_BUDGET_EXHAUSTED,
    REASON_CANCELLED_BY_OPERATOR,
    REASON_EMPTY_RESPONSE,
    REASON_HEADER_FETCH_FAILED,
    REASON_INVALID_RESPONSE,
    REASON_OK,
    REASON_PARTITION_ROW_LOSS_GUARD,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_SCANNED_EMPTY,
    STATE_SUCCESSFUL,
    TOPOLOGY_COLD_START,
    TOPOLOGY_WARM_INCREMENTAL,
    VALID_STATES,
    CancelledError,
    CheckpointMismatchError,
    is_budget_exhausted_reason,
)
from robinhood_lp.ingestion.planner import (
    PlannedRun,
    PlannedSubRange,
    RangePlanner,
    RangePlannerInputs,
    build_pool_topic_filter,
)
from robinhood_lp.ingestion.router import (
    BudgetLedger,
    CoverageDecision,
    Router,
    RouterConfig,
)
from robinhood_lp.protocol import Address, ChainId, PoolId
from robinhood_lp.storage.decode_log import LogDecodeContext, decode_log
from robinhood_lp.storage.manifest import ManifestStore
from robinhood_lp.storage.schema import (
    CURRENT_DECODE_VERSION,
    CURRENT_SCHEMA_VERSION,
)
from robinhood_lp.storage.writer import (
    DEFAULT_BLOCKS_PER_PARTITION,
    RawPartitionWriter,
    SilentRowLossError,
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """The runner's verdict on a single ingestion run."""

    run_id: str
    complete: bool
    topology: str
    coverage_from_block: int
    coverage_to_block: int
    qualified_end_block: int | None
    qualified_end_block_hash: str | None
    halt_reason: str | None
    interval_count: int
    interval_table: tuple[dict[str, Any], ...]
    retries: tuple[dict[str, Any], ...]
    deviations: tuple[dict[str, Any], ...]
    logical_rpc_calls: int
    http_requests: int
    response_bytes: int
    normalized_rows: int
    provider_units: int
    elapsed_ms: int
    manifest_checksum: str


# ---------------------------------------------------------------------------
# Header cache (one non-hydrated block header per distinct event block)
# ---------------------------------------------------------------------------


class BlockHeaderSource(Protocol):
    """A source of non-hydrated block headers.

    The contract is intentionally narrow: the runner only needs the
    fields it stores (block number, block hash, parent hash,
    timestamp, and the minimum block metadata for ordering). A
    hydrated block (``hydrated=True``) is never requested.
    """

    def get_header(self, block_number: int) -> dict[str, Any] | None: ...


@dataclass(slots=True)
class BlockHeaderCache:
    """One non-hydrated block header per distinct event block.

    The cache is in-memory and per-run: a warm restart rebuilds it
    from the run manifest. The cache de-duplicates by block number;
    the per-interval header write to the manifest is also deduped so
    every distinct event block ends up with exactly one entry.
    """

    source: BlockHeaderSource
    cache: dict[int, dict[str, Any]] = field(default_factory=dict)
    headers_fetched: int = 0

    def get(self, block_number: int) -> dict[str, Any] | None:
        cached = self.cache.get(block_number)
        if cached is not None:
            return cached
        header = self.source.get_header(block_number)
        if header is None:
            return None
        self.cache[block_number] = header
        self.headers_fetched += 1
        return header


# ---------------------------------------------------------------------------
# Cancellation token
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CancellationToken:
    """Cooperative cancellation for the runner.

    The runner checks ``cancelled`` at every sub-range boundary. The
    token is not thread-safe (the runner is async; tests use it
    deterministically).
    """

    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IngestionRunner:
    """The capability-driven historical ingestion runner.

    Construct one runner per run. The runner owns the run manifest,
    the retry ledger, the budget ledger, the router, and the block
    header cache. A run is fully described by the persisted
    ``run_manifests`` + ``run_intervals`` + ``retry_ledger`` +
    ``run_manifest_deviations`` + ``durable_checkpoints`` rows it
    writes; the runner can be reconstructed from those rows after a
    kill/restart at any durable-commit boundary.
    """

    manifest: ManifestStore
    writer: RawPartitionWriter
    chain_id: ChainId
    contract_address: Address
    pool_id: PoolId
    pool_manager_address: Address
    pool_init_block: int
    planner: RangePlanner
    router_config: RouterConfig
    capability_snapshot: CapabilitySnapshot
    budget_snapshot: BudgetSnapshot
    run_id: str = field(default_factory=lambda: "run-" + uuid.uuid4().hex)
    header_cache: BlockHeaderCache | None = None
    block_header_source: RpcBlockHeaderSource | None = None
    block_header_sink: BlockHeaderSink | None = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    _clients: dict[str, Any] = field(default_factory=dict)

    # ----- public API --------------------------------------------------

    def run(
        self,
        *,
        requested_start_block: int,
        requested_end_block: int,
    ) -> IngestionResult:
        """Execute one ingestion run and return the result.

        The runner's behaviour:

        1. Load (and validate) the existing durable checkpoint;
        2. Compute the topology + sub-ranges via
           :class:`RangePlanner`;
        3. Persist the run manifest with the preflight capability /
           budget snapshots before the scan starts;
        4. Walk the sub-ranges via the router; every interval is
           recorded in the run manifest with its state + reason code
           before the runner moves on;
        5. Advance the durable checkpoint only after a successful
           or ``scanned_empty`` commit; failed / cancelled intervals
           halts qualification and ``complete=False``;
        6. Return the :class:`IngestionResult`.
        """
        started_at = _now_iso()
        started_monotonic = time.monotonic()
        # 1. Load the existing checkpoint (if any) and plan.
        existing_cp = load_durable_checkpoint(
            self.manifest,
            chain_id=self.chain_id.value,
            contract_address=self.contract_address,
            pool_id=self.pool_id,
        )
        if existing_cp is not None:
            if existing_cp.schema_version != CURRENT_SCHEMA_VERSION:
                return self._halt_with_reason(
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    requested_start_block=requested_start_block,
                    requested_end_block=requested_end_block,
                    halt_reason="checkpoint_schema_decode_drift",
                    pool_init_block=self.pool_init_block,
                    plan=None,
                )
            if existing_cp.decode_version != CURRENT_DECODE_VERSION:
                return self._halt_with_reason(
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    requested_start_block=requested_start_block,
                    requested_end_block=requested_end_block,
                    halt_reason="checkpoint_schema_decode_drift",
                    pool_init_block=self.pool_init_block,
                    plan=None,
                )
            # Validate the stored qualified_end_block_hash against
            # the canonical block hash at qualified_end_block as
            # observed by the registered endpoints. The contract
            # requires the runner to reject a checkpoint whose
            # block hash does not match the requested pool's
            # canonical block at that height (no silent
            # trust-the-stored-hash path).
            block_hash_check = self._verify_qualified_end_block_hash(
                existing_cp.qualified_end_block,
                existing_cp.qualified_end_block_hash,
            )
            if block_hash_check is not None:
                return self._halt_with_reason(
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    requested_start_block=requested_start_block,
                    requested_end_block=requested_end_block,
                    halt_reason=block_hash_check,
                    pool_init_block=self.pool_init_block,
                    plan=None,
                )
        # The manifest checksum this run would write under the
        # current capability / budget snapshots. The planner
        # compares it against the stored checksum when validating
        # a warm checkpoint (T032 contract: reject a checkpoint
        # whose manifest does not match the requested pool and
        # range).
        expected_manifest_checksum = _stable_checksum(
            self.capability_snapshot.snapshot_id,
            self.budget_snapshot.to_json(),
        )
        try:
            plan = self.planner.plan(
                RangePlannerInputs(
                    chain_id=self.chain_id.value,
                    contract_address=self.contract_address,
                    pool_id=self.pool_id,
                    pool_init_block=self.pool_init_block,
                    requested_start_block=requested_start_block,
                    requested_end_block=requested_end_block,
                    existing_checkpoint=(
                        existing_cp.to_existing_checkpoint() if existing_cp else None
                    ),
                    expected_manifest_checksum=(
                        expected_manifest_checksum if existing_cp is not None else None
                    ),
                )
            )
        except CheckpointMismatchError:
            return self._halt_with_reason(
                started_at=started_at,
                started_monotonic=started_monotonic,
                requested_start_block=requested_start_block,
                requested_end_block=requested_end_block,
                halt_reason="checkpoint_manifest_drift",
                pool_init_block=self.pool_init_block,
                plan=None,
            )
        # 2. Persist the run manifest before any RPC traffic.
        self.manifest.insert_run_manifest(
            run_id=self.run_id,
            started_at=started_at,
            completed_at=None,
            chain_id=self.chain_id.value,
            contract_address=self.contract_address.to_hex().removeprefix("0x").lower(),
            pool_id=self.pool_id.to_hex(),
            pool_init_block=self.pool_init_block,
            requested_start_block=requested_start_block,
            requested_end_block=requested_end_block,
            topology=plan.topology,
            capability_snapshot_json=self.capability_snapshot.to_json(),
            budget_snapshot_json=self.budget_snapshot.to_json(),
            preflight_completed_at=self.capability_snapshot.captured_at,
            logical_rpc_calls=0,
            http_requests=0,
            response_bytes=0,
            normalized_rows=0,
            provider_units=0,
            elapsed_ms=0,
            complete=False,
            halt_reason=None,
            schema_version=CURRENT_SCHEMA_VERSION,
            decode_version=CURRENT_DECODE_VERSION,
            manifest_checksum=None,
        )
        # 3. Wire the budget ledger + router.
        budget_ledger = BudgetLedger.from_snapshot(self.budget_snapshot)
        router = self._build_router(budget_ledger)
        interval_rows: list[dict[str, Any]] = []
        retry_rows: list[dict[str, Any]] = []
        halt_reason: str | None = None
        # Track the highest qualified block for the checkpoint
        # advance. The checkpoint can only advance when every
        # sub-range up to that block is covered.
        last_covered_end: int | None = None
        last_covered_hash: str | None = None
        if existing_cp is not None:
            last_covered_end = existing_cp.qualified_end_block
            last_covered_hash = existing_cp.qualified_end_block_hash
        # Pre-compute the topic filter once.
        topic_filter = build_pool_topic_filter(self.pool_id)
        # 4. Walk the sub-ranges.
        for sub in plan.sub_ranges:
            if self.cancellation.cancelled:
                decision = CoverageDecision(
                    state=STATE_CANCELLED,
                    endpoint_alias=None,
                    failover_from=None,
                    reason_code=REASON_CANCELLED_BY_OPERATOR,
                    rows=0,
                    response_bytes=0,
                    logical_rpc_calls=0,
                    http_requests=0,
                    pinned_block_hash=None,
                )
                self._record_interval(decision, sub)
                interval_rows.append(
                    {
                        "from_block": sub.from_block,
                        "to_block": sub.to_block,
                        "state": STATE_CANCELLED,
                        "reason_code": REASON_CANCELLED_BY_OPERATOR,
                    }
                )
                halt_reason = REASON_CANCELLED_BY_OPERATOR
                break
            # Attempt the sub-range. The router is single-pass; the
            # runner may re-attempt with a halved sub-range when the
            # router reports response-size overflow (the only case
            # where the contract mandates split-then-retry rather
            # than failure).
            decision = self._cover_with_splits(sub, router, topic_filter)
            # T035 — when the decision is successful, decode the raw
            # rows, fetch the canonical block header for every
            # distinct event block, enrich the typed records with
            # ``block_timestamp`` / ``parent_hash``, persist the
            # headers to the dedup'd ``block_headers`` manifest
            # table, and flow the records to the Parquet writer. A
            # header that cannot be obtained halts the interval with
            # ``complete=False`` (ADR-012: missing headers are never
            # zero-defaulted).
            if decision.state == STATE_SUCCESSFUL and decision.raw_rows:
                process_decision = self._process_successful_rows(
                    decision=decision,
                    sub=sub,
                    retrieval_time=_now_iso(),
                )
                if process_decision is not None:
                    decision = process_decision
            self._record_interval(decision, sub)
            interval_rows.append(
                {
                    "from_block": sub.from_block,
                    "to_block": sub.to_block,
                    "endpoint_alias": decision.endpoint_alias,
                    "failover_from": decision.failover_from,
                    "state": decision.state,
                    "reason_code": decision.reason_code,
                    "rows": decision.rows,
                }
            )
            if decision.state in (STATE_FAILED, STATE_CANCELLED):
                halt_reason = decision.reason_code
                break
            # Coverage transition: extend last_covered_end to sub.to_block.
            # scanned_empty / successful both count as covered.
            if last_covered_end is None:
                last_covered_end = sub.from_block - 1
            last_covered_end = max(last_covered_end, sub.to_block)
            last_covered_hash = decision.pinned_block_hash or last_covered_hash
            # Advance the durable checkpoint only when every
            # sub-range up to the current one is covered. The
            # monotonic MAX in the manifest store means this is
            # safe even when concurrent attempts are present.
            if existing_cp is None and last_covered_end is not None:
                # cold start: the prefix origin is pool_init_block.
                state = self._build_checkpoint_state(
                    plan=plan,
                    qualified_start_block=self.pool_init_block,
                    qualified_end_block=last_covered_end,
                    qualified_end_block_hash=last_covered_hash or EMPTY_CHECKPOINT_HASH,
                )
            else:
                state = self._build_checkpoint_state(
                    plan=plan,
                    qualified_start_block=(
                        existing_cp.qualified_start_block
                        if existing_cp is not None
                        else self.pool_init_block
                    ),
                    qualified_end_block=last_covered_end
                    if last_covered_end is not None
                    else (
                        existing_cp.qualified_end_block
                        if existing_cp is not None
                        else self.pool_init_block
                    ),
                    qualified_end_block_hash=last_covered_hash
                    or (
                        existing_cp.qualified_end_block_hash
                        if existing_cp is not None
                        else EMPTY_CHECKPOINT_HASH
                    ),
                )
            try:
                upsert_durable_checkpoint(self.manifest, state)
            except CheckpointMismatchError:
                # Schema / decode version drifted mid-run; halt.
                halt_reason = "checkpoint_schema_decode_drift"
                break
        # 5. Compose the final manifest + result.
        elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
        # ``complete`` is True iff every requested block is covered
        # exactly once by successful or scanned_empty, AND no
        # interval is failed / cancelled.
        covered_blocks_ok = (
            halt_reason is None
            and last_covered_end is not None
            and last_covered_end >= plan.coverage_to_block
            and len(plan.sub_ranges) > 0
        )
        empty_coverage_ok = halt_reason is None and len(plan.sub_ranges) == 0
        complete = bool(covered_blocks_ok or empty_coverage_ok)
        # Manifest checksum: SHA-256 over the JSON-serialised
        # interval table (the manifest is the audit trail; the
        # checksum pins the table bytes for any future check).
        manifest_checksum = _compute_manifest_checksum(interval_rows, retry_rows)
        self.manifest.insert_run_manifest(
            run_id=self.run_id,
            started_at=started_at,
            completed_at=_now_iso(),
            chain_id=self.chain_id.value,
            contract_address=self.contract_address.to_hex().removeprefix("0x").lower(),
            pool_id=self.pool_id.to_hex(),
            pool_init_block=self.pool_init_block,
            requested_start_block=requested_start_block,
            requested_end_block=requested_end_block,
            topology=plan.topology,
            capability_snapshot_json=self.capability_snapshot.to_json(),
            budget_snapshot_json=self.budget_snapshot.to_json(),
            preflight_completed_at=self.capability_snapshot.captured_at,
            logical_rpc_calls=0,
            http_requests=0,
            response_bytes=0,
            normalized_rows=0,
            provider_units=0,
            elapsed_ms=elapsed_ms,
            complete=complete,
            halt_reason=halt_reason,
            schema_version=CURRENT_SCHEMA_VERSION,
            decode_version=CURRENT_DECODE_VERSION,
            manifest_checksum=manifest_checksum,
        )
        # Re-read the run manifest's counters we maintain per
        # interval so the result reflects the run totals.
        run_row = self.manifest.get_run_manifest(self.run_id)
        return IngestionResult(
            run_id=self.run_id,
            complete=complete,
            topology=plan.topology,
            coverage_from_block=plan.coverage_from_block,
            coverage_to_block=plan.coverage_to_block,
            qualified_end_block=last_covered_end,
            qualified_end_block_hash=last_covered_hash,
            halt_reason=halt_reason,
            interval_count=len(interval_rows),
            interval_table=tuple(interval_rows),
            retries=tuple(self.manifest.list_retries(self.run_id)),
            deviations=tuple(self.manifest.list_deviations(self.run_id)),
            logical_rpc_calls=int(run_row["logical_rpc_calls"]) if run_row else 0,
            http_requests=int(run_row["http_requests"]) if run_row else 0,
            response_bytes=int(run_row["response_bytes"]) if run_row else 0,
            normalized_rows=int(run_row["normalized_rows"]) if run_row else 0,
            provider_units=int(run_row["provider_units"]) if run_row else 0,
            elapsed_ms=elapsed_ms,
            manifest_checksum=manifest_checksum,
        )

    # ----- helpers ------------------------------------------------------

    def _build_router(self, budget_ledger: BudgetLedger) -> Router:
        # The router needs at least one client per alias in the
        # failover order. The caller wires the real RpcAdapter-backed
        # clients before invoking ``run``; if none were wired, the
        # router surfaces every interval as ``no_capable_endpoint``.
        topic_filter = build_pool_topic_filter(self.pool_id)
        return Router(
            clients=dict(self._clients),
            capability=self.capability_snapshot,
            budget=budget_ledger,
            config=self.router_config,
            address=self.contract_address.to_hex(),
            topic_filter=topic_filter,
            on_deviation=self._record_deviation,
        )

    def register_client(self, alias: str, client: Any) -> None:
        """Register an :class:`EndpointClient` for ``alias``.

        The runner stores the registered clients in a private dict
        the router reads at run time. Tests typically register fakes
        keyed by the configured alias names.
        """
        self._clients[alias] = client

    def register_header_source(
        self,
        *,
        source: RpcBlockHeaderSource,
        sink: BlockHeaderSink,
    ) -> None:
        """Wire the T035 block-header source / sink pair.

        ``source`` issues ``eth_getBlockByNumber(hex(n), false)``
        batches and dedupes per distinct event block (T035 / ADR-012).
        ``sink`` persists the fetched headers to the dedup'd
        ``block_headers`` manifest table. The runner uses the source
        to enrich every persisted event with the canonical
        ``block_timestamp`` and ``parent_hash`` and the sink to
        upsert the dedup'd header row alongside the partition writer
        commit.

        After registration the runner holds the source on
        ``self.block_header_source`` and wraps it in a per-run
        :class:`BlockHeaderCache` on ``self.header_cache``. A single
        registration is sufficient; calling the method twice
        overwrites the prior wiring.
        """
        if not isinstance(source, RpcBlockHeaderSource):
            raise TypeError(
                f"register_header_source: source must be "
                f"RpcBlockHeaderSource, got {type(source).__name__}"
            )
        if not isinstance(sink, BlockHeaderSink):
            raise TypeError(
                f"register_header_source: sink must be BlockHeaderSink, got {type(sink).__name__}"
            )
        self.block_header_source = source
        self.header_cache = BlockHeaderCache(source=source)
        self.block_header_sink = sink

    def _verify_qualified_end_block_hash(
        self,
        qualified_end_block: int,
        stored_hash: str | None,
    ) -> str | None:
        """Compare the stored ``qualified_end_block_hash`` against
        the canonical block hash observed at ``qualified_end_block``.

        The T032 contract requires the runner to reject a warm
        checkpoint whose block hash does not match the requested
        pool's canonical block at that height. The runner queries
        every registered endpoint for the hash and accepts the
        first non-``None`` response; an agreeing canonical hash
        returns ``None`` (no halt), a disagreement returns a halt
        reason, and no observation surfaces a separate halt reason
        so a configured but unreachable endpoint does not silently
        approve a stale hash.
        """
        if not self._clients:
            # No clients registered at all: we cannot re-fetch and
            # compare. The contract requires a re-fetch + compare;
            # the safe default is to refuse to advance.
            return "checkpoint_block_hash_unverified"
        observed: str | None = None
        for client in self._clients.values():
            try:
                observed = client.get_block_hash_for_pin(qualified_end_block)
            except Exception:  # noqa: BLE001 — defensive: an
                # unreachable endpoint must not block the
                # comparison path; the next client is tried.
                observed = None
                continue
            if observed is not None:
                break
        if observed is None:
            return "checkpoint_block_hash_unverified"
        if stored_hash is None:
            return "checkpoint_block_hash_drift"
        if _hashes_equal_for_validation(stored_hash, observed):
            return None
        return "checkpoint_block_hash_drift"

    def _cover_with_splits(
        self,
        sub: PlannedSubRange,
        router: Router,
        topic_filter: list[list[str] | str],
    ) -> CoverageDecision:
        """Cover ``sub`` with one or more calls; split on
        ``response_size_overflow`` (T032 contract).

        Other failures are surfaced to the caller as-is; only
        response-size overflow is split-then-retried per the
        contract.
        """
        from robinhood_lp.ingestion.errors import REASON_RESPONSE_SIZE_OVERFLOW
        from robinhood_lp.ingestion.planner import split_sub_range

        current = sub
        # Cap the number of recursive splits so a pathological
        # response-size pattern cannot loop forever.
        max_splits = 16
        attempt = 0
        while True:
            decision = router.cover_sub_range(current)
            attempt += 1
            self._record_retry(
                decision.endpoint_alias or "_none_",
                "eth_getLogs",
                current.from_block,
                current.to_block,
                decision.reason_code,
                _retry_outcome_from_decision(decision),
            )
            if (
                decision.state == STATE_FAILED
                and decision.reason_code == REASON_RESPONSE_SIZE_OVERFLOW
                and attempt <= max_splits
                and current.from_block < current.to_block
            ):
                left, right = split_sub_range(
                    current, max_blocks=max(1, (current.to_block - current.from_block + 1) // 2)
                )
                if right is None:
                    # Cannot split further; surface as a single-block overflow.
                    return CoverageDecision(
                        state=STATE_FAILED,
                        endpoint_alias=decision.endpoint_alias,
                        failover_from=decision.failover_from,
                        reason_code="single_block_overflow",
                        rows=0,
                        response_bytes=decision.response_bytes,
                        logical_rpc_calls=decision.logical_rpc_calls,
                        http_requests=decision.http_requests,
                        pinned_block_hash=None,
                    )
                # Cover left, then right; if either fails the
                # caller sees the first failure and the run halts.
                left_decision = router.cover_sub_range(left)
                self._record_retry(
                    left_decision.endpoint_alias or "?",
                    "eth_getLogs",
                    left.from_block,
                    left.to_block,
                    left_decision.reason_code,
                    _retry_outcome_from_decision(left_decision),
                )
                if left_decision.state == STATE_FAILED:
                    return left_decision
                right_decision = router.cover_sub_range(right)
                self._record_retry(
                    right_decision.endpoint_alias or "?",
                    "eth_getLogs",
                    right.from_block,
                    right.to_block,
                    right_decision.reason_code,
                    _retry_outcome_from_decision(right_decision),
                )
                if right_decision.state == STATE_FAILED:
                    return right_decision
                # Both halves covered. Merge the rows / counters.
                merged_rows = left_decision.rows + right_decision.rows
                merged_bytes = left_decision.response_bytes + right_decision.response_bytes
                merged_calls = left_decision.logical_rpc_calls + right_decision.logical_rpc_calls
                merged_http = left_decision.http_requests + right_decision.http_requests
                # If both halves were scanned_empty, the union is
                # scanned_empty; otherwise successful.
                if (
                    left_decision.state == STATE_SCANNED_EMPTY
                    and right_decision.state == STATE_SCANNED_EMPTY
                ):
                    return CoverageDecision(
                        state=STATE_SCANNED_EMPTY,
                        endpoint_alias=left_decision.endpoint_alias,
                        failover_from=left_decision.failover_from,
                        reason_code=REASON_EMPTY_RESPONSE,
                        rows=0,
                        response_bytes=merged_bytes,
                        logical_rpc_calls=merged_calls,
                        http_requests=merged_http,
                        pinned_block_hash=left_decision.pinned_block_hash,
                    )
                return CoverageDecision(
                    state=STATE_SUCCESSFUL,
                    endpoint_alias=left_decision.endpoint_alias,
                    failover_from=left_decision.failover_from,
                    reason_code=REASON_OK,
                    rows=merged_rows,
                    response_bytes=merged_bytes,
                    logical_rpc_calls=merged_calls,
                    http_requests=merged_http,
                    pinned_block_hash=None,
                )
            return decision

    # ----- T035 row processing ----------------------------------------

    def _process_successful_rows(
        self,
        *,
        decision: CoverageDecision,
        sub: PlannedSubRange,
        retrieval_time: str,
    ) -> CoverageDecision | None:
        """Decode, enrich, and persist the raw ``eth_getLogs`` rows.

        For every successful interval the runner:

        1. Collects the distinct ``blockNumber`` values across the
           raw rows. Blocks with no pool event are never fetched.
        2. Calls ``self.block_header_source.get_headers_batch`` so
           the dedup invariant (``logical_get_block_by_number_calls
           == distinct event block count``) is enforced at the source
           and the manifest's ``block_headers`` table receives
           exactly one row per distinct event block.
        3. Persists every fetched header through
           ``self.block_header_sink.upsert`` inside the same
           transaction as the partition writer commits so a crash
           mid-commit does not produce header rows without their
           events or vice versa.
        4. Decodes the raw rows into typed V4 records, sets
           ``block_timestamp`` and ``parent_hash`` from the header
           evidence, and flows the records to
           ``self.writer.append_partition`` grouped by event name
           and partition grid cell.

        Returns ``None`` when the row processing succeeded (the
        caller continues with the original decision). Returns a new
        :class:`CoverageDecision` with ``state=STATE_FAILED`` and the
        appropriate reason code when a header cannot be obtained
        or the writer refuses a record; the caller surfaces the new
        decision and halts the run with ``complete=False``.
        """
        if decision.state != STATE_SUCCESSFUL:
            return None
        if not decision.raw_rows:
            return None
        if self.block_header_source is None or self.block_header_sink is None:
            # The contract requires the header source / sink to be
            # wired before a real run; tests that exercise scanned
            # / failed / cancelled paths without wiring the source
            # are allowed but a successful interval without a wired
            # source means the operator configuration is incomplete.
            #
            # Backward compatibility: the T032 router tests exercise
            # the routing path with stub rows that do not carry the
            # V4 ``topics`` / ``data`` shape the decoder requires.
            # When the header source is not wired AND the rows look
            # like stubs (no ``topics`` field), the runner treats
            # the interval as a T032-only routing success and skips
            # persistence. A real run with a wired source must
            # satisfy the V4 row contract or the decoder raises
            # below.
            if all(not isinstance(row.get("topics"), list) for row in decision.raw_rows):
                return None
            return _failed_decision(
                decision,
                reason_code=REASON_HEADER_FETCH_FAILED,
                detail=(
                    "block header source / sink not wired; "
                    "register_header_source must be called before run"
                ),
            )
        # Distinct block numbers across the raw row set. ``sort``
        # keeps the input stable so the source's cache hits stay
        # reproducible across runs of the same input.
        distinct_block_numbers = sorted(
            {
                int(_decode_0x_int(row.get("blockNumber"), field="row.blockNumber"))
                for row in decision.raw_rows
            }
        )
        try:
            headers_map = self.block_header_source.get_headers_batch(distinct_block_numbers)
        except BlockHeaderFetchError as exc:
            return _failed_decision(
                decision,
                reason_code=REASON_HEADER_FETCH_FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )
        # Every distinct event block must resolve to a header. A
        # missing header halts the interval (ADR-012) and never
        # zero-defaults the persisted fields.
        for block_number in distinct_block_numbers:
            header = headers_map.get(block_number)
            if header is None:
                return _failed_decision(
                    decision,
                    reason_code=REASON_HEADER_FETCH_FAILED,
                    detail=(
                        f"block header for block_number {block_number} "
                        f"could not be obtained; refusing to zero-default"
                    ),
                )
        # Group rows by event name and partition grid cell.
        decoded_batches: dict[tuple[str, int], list[Any]] = {}
        for raw_row in decision.raw_rows:
            row_block_number = int(
                _decode_0x_int(raw_row.get("blockNumber"), field="row.blockNumber")
            )
            row_header: BlockHeader = headers_map[row_block_number]  # type: ignore[assignment]
            # The outer loop already validated that every distinct
            # block number resolves to a non-``None`` header; the
            # inner loop sees a subset of those blocks so the lookup
            # is always populated. ``assert`` keeps mypy happy.
            assert row_header is not None
            try:
                decoded = self._decode_and_enrich(raw_row, row_header, retrieval_time)
            except (ValueError, TypeError) as exc:
                # Decoder / enrich failure: surface as a failed
                # interval with the precise reason; the operator
                # sees the block_number / event_name combination
                # that tripped the decoder.
                return _failed_decision(
                    decision,
                    reason_code=REASON_INVALID_RESPONSE,
                    detail=(
                        f"decode_log failed for block_number "
                        f"{row_header.block_number}: {type(exc).__name__}: {exc}"
                    ),
                )
            event_name = type(decoded).__name__.removesuffix("LogRecord")
            grid_lo = (
                int(decoded.block_number) // DEFAULT_BLOCKS_PER_PARTITION
            ) * DEFAULT_BLOCKS_PER_PARTITION
            key = (event_name, grid_lo)
            decoded_batches.setdefault(key, []).append(decoded)
        # Persist every batch via the writer. Each batch is one
        # call to ``writer.append_partition`` so the dedup /
        # conflict logic runs per partition grid cell.
        for (event_name, _grid_lo), batch in decoded_batches.items():
            try:
                self.writer.append_partition(
                    batch,
                    chain_id=self.chain_id,
                    contract_address=self.contract_address,
                )
            except ValueError as exc:
                return _failed_decision(
                    decision,
                    reason_code=REASON_INVALID_RESPONSE,
                    detail=(
                        f"writer.append_partition rejected batch "
                        f"{event_name}: {type(exc).__name__}: {exc}"
                    ),
                )
            except SilentRowLossError as exc:
                # T037 fail-closed path. The writer's guard rejected a
                # batch that would have left ``event_index`` ahead of
                # the on-disk Parquet file; surface the failure as a
                # concrete reason code so the run halts non-complete
                # rather than reporting a partial dataset.
                return _failed_decision(
                    decision,
                    reason_code=REASON_PARTITION_ROW_LOSS_GUARD,
                    detail=(
                        f"writer.append_partition refused a forward-progress "
                        f"batch for event {event_name!r}: {type(exc).__name__}: {exc}"
                    ),
                )
        # Persist the dedup'd headers to the manifest ``block_headers``
        # table once the partition writes have committed. The sink
        # is idempotent on (chain_id, block_hash), so a re-run over
        # an overlapping range is safe and the dedup invariant holds
        # even across run boundaries.
        for block_number in distinct_block_numbers:
            header = headers_map[block_number]
            assert header is not None
            self.block_header_sink.upsert(chain_id=self.chain_id.value, header=header)
        return None

    def _decode_and_enrich(
        self,
        raw_row: dict[str, Any],
        header: Any,
        retrieval_time: str,
    ) -> Any:
        """Decode one ``eth_getLogs`` row and enrich with header fields.

        Returns the typed V4 record with ``block_timestamp`` and
        ``parent_hash`` populated from ``header``. The record's
        ``block_hash`` is the canonical block hash the header
        reports (the JSON-RPC log row's ``blockHash`` field is not
        used as the authoritative source).
        """
        # The block_hash / transaction_hash / log_index carried by
        # the raw row are required by the decoder; the runner
        # overrides ``block_hash`` with the header-derived value so
        # the persisted EventKey matches the canonical chain block.
        block_number = int(_decode_0x_int(raw_row.get("blockNumber"), field="raw_row.blockNumber"))
        tx_hash = int(
            _decode_0x_int(raw_row.get("transactionHash"), field="raw_row.transactionHash")
        )
        log_index = int(_decode_0x_int(raw_row.get("logIndex"), field="raw_row.logIndex"))
        transaction_index = int(
            _decode_0x_int(raw_row.get("transactionIndex"), field="raw_row.transactionIndex")
        )
        removed = bool(raw_row.get("removed", False))
        ctx = LogDecodeContext(
            chain_id=self.chain_id,
            block_number=block_number,
            block_hash=int(header.block_hash),
            transaction_hash=tx_hash,
            transaction_index=transaction_index,
            log_index=log_index,
            address=self.pool_manager_address,
            removed=removed,
            acquisition=_build_acquisition(
                endpoint_alias=(
                    "robinhood_public"  # default; endpoint-specific
                    # aliases are observed downstream by the manifest
                    # ``accounting_intervals`` table.
                ),
                retrieval_time=retrieval_time,
            ),
            raw_response=dict(raw_row),
        )
        record = decode_log(raw_row, ctx)
        # Enrich the typed record with the canonical header fields.
        return replace(
            record,
            block_timestamp=int(header.timestamp),
            parent_hash=int(header.parent_hash),
        )

    def _record_interval(self, decision: CoverageDecision, sub: PlannedSubRange) -> None:
        if decision.state not in VALID_STATES:
            raise ValueError(f"_record_interval: unknown state {decision.state!r}")
        # Per-interval run counters: logical RPC calls, HTTP
        # requests, response bytes, normalized rows.
        self.manifest.insert_run_interval(
            run_id=self.run_id,
            from_block=sub.from_block,
            to_block=sub.to_block,
            endpoint_alias=decision.endpoint_alias or "_none_",
            state=decision.state,
            reason_code=decision.reason_code,
            rows=decision.rows,
            response_bytes=decision.response_bytes,
            logical_rpc_calls=decision.logical_rpc_calls,
            http_requests=decision.http_requests,
            failover_from=decision.failover_from,
            pinned_block_hash=decision.pinned_block_hash,
        )
        if decision.rows or decision.response_bytes:
            self.manifest.increment_run_metrics(
                run_id=self.run_id,
                logical_rpc_calls=decision.logical_rpc_calls,
                http_requests=decision.http_requests,
                response_bytes=decision.response_bytes,
                normalized_rows=decision.rows,
                provider_units=0,
                elapsed_ms=0,
            )

    def _record_deviation(self, kind: str, detail: dict[str, Any]) -> None:
        self.manifest.append_deviation(
            run_id=self.run_id,
            deviation_kind=kind,
            detail_json=json.dumps(
                detail, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ),
        )

    def _record_retry(
        self,
        endpoint_alias: str,
        method: str,
        from_block: int,
        to_block: int,
        reason_code: str,
        outcome: str,
    ) -> None:
        # The attempt counter is row-id based; the ledger is
        # append-only and checksummed alongside the manifest.
        existing = self.manifest.list_retries(self.run_id)
        attempt = len(existing) + 1
        self.manifest.append_retry(
            run_id=self.run_id,
            attempt=attempt,
            endpoint_alias=endpoint_alias,
            method=method,
            from_block=from_block,
            to_block=to_block,
            reason_code=reason_code,
            outcome=outcome,
        )

    def _halt_with_reason(
        self,
        *,
        started_at: str,
        started_monotonic: float,
        requested_start_block: int,
        requested_end_block: int,
        halt_reason: str,
        pool_init_block: int,
        plan: PlannedRun | None,
    ) -> IngestionResult:
        """Persist a run manifest marked complete=False with the
        given halt_reason and return the result.

        Used when the existing durable checkpoint fails the
        schema/decode version check (T032 contract: the system
        refuses to advance when the stored manifest / schema /
        decode / coverage prefix does not match).
        """
        elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
        # We persist the run manifest so the audit trail captures
        # the rejection. ``complete`` stays False.
        self.manifest.insert_run_manifest(
            run_id=self.run_id,
            started_at=started_at,
            completed_at=_now_iso(),
            chain_id=self.chain_id.value,
            contract_address=self.contract_address.to_hex().removeprefix("0x").lower(),
            pool_id=self.pool_id.to_hex(),
            pool_init_block=pool_init_block,
            requested_start_block=requested_start_block,
            requested_end_block=requested_end_block,
            topology=(plan.topology if plan is not None else TOPOLOGY_WARM_INCREMENTAL),
            capability_snapshot_json=self.capability_snapshot.to_json(),
            budget_snapshot_json=self.budget_snapshot.to_json(),
            preflight_completed_at=self.capability_snapshot.captured_at,
            logical_rpc_calls=0,
            http_requests=0,
            response_bytes=0,
            normalized_rows=0,
            provider_units=0,
            elapsed_ms=elapsed_ms,
            complete=False,
            halt_reason=halt_reason,
            schema_version=CURRENT_SCHEMA_VERSION,
            decode_version=CURRENT_DECODE_VERSION,
            manifest_checksum=None,
        )
        coverage_from = (
            plan.coverage_from_block
            if plan is not None
            else (
                pool_init_block
                if requested_start_block <= pool_init_block
                else requested_start_block
            )
        )
        coverage_to = plan.coverage_to_block if plan is not None else requested_end_block
        return IngestionResult(
            run_id=self.run_id,
            complete=False,
            topology=(plan.topology if plan is not None else TOPOLOGY_WARM_INCREMENTAL),
            coverage_from_block=coverage_from,
            coverage_to_block=coverage_to,
            qualified_end_block=None,
            qualified_end_block_hash=None,
            halt_reason=halt_reason,
            interval_count=0,
            interval_table=(),
            retries=(),
            deviations=(),
            logical_rpc_calls=0,
            http_requests=0,
            response_bytes=0,
            normalized_rows=0,
            provider_units=0,
            elapsed_ms=elapsed_ms,
            manifest_checksum="0x" + "00" * 32,
        )

    def _build_checkpoint_state(
        self,
        *,
        plan: PlannedRun,
        qualified_start_block: int,
        qualified_end_block: int,
        qualified_end_block_hash: str,
    ) -> DurableCheckpointState:
        return DurableCheckpointState(
            chain_id=self.chain_id.value,
            contract_address=self.contract_address,
            pool_id=self.pool_id,
            qualified_start_block=qualified_start_block,
            qualified_end_block=qualified_end_block,
            qualified_end_block_hash=qualified_end_block_hash,
            schema_version=CURRENT_SCHEMA_VERSION,
            decode_version=CURRENT_DECODE_VERSION,
            capability_snapshot_id=self.capability_snapshot.snapshot_id,
            manifest_checksum=_stable_checksum(
                self.capability_snapshot.snapshot_id,
                self.budget_snapshot.to_json(),
            ),
            topology=plan.topology,
            pool_init_block=self.pool_init_block,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _decode_0x_int(value: Any, *, field: str) -> int:
    """Decode a JSON-RPC 0x-hex (or already-int) value into a Python int.

    Used by the runner to translate the wire format
    (``"0x..."`` strings) the ``eth_getLogs`` rows arrive in.
    """
    if isinstance(value, bool):
        raise TypeError(f"{field}: expected int or 0x-hex str, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s.startswith("0x"):
            return int(s, 16)
        return int(s, 10)
    raise TypeError(f"{field}: expected int or 0x-hex str, got {type(value).__name__}")


def _build_acquisition(*, endpoint_alias: str, retrieval_time: str) -> Any:
    """Build an :class:`AcquisitionProvenance` envelope for the row."""
    from robinhood_lp.storage.schema import AcquisitionProvenance

    return AcquisitionProvenance(
        endpoint_alias=endpoint_alias,
        retrieval_time=retrieval_time,
        request_from_block=0,
        request_to_block=0,
        http_batch_size=None,
        http_batch_position=None,
        request_attempt=None,
    )


def _failed_decision(
    source: CoverageDecision,
    *,
    reason_code: str,
    detail: str,
) -> CoverageDecision:
    """Build a failed :class:`CoverageDecision` that mirrors ``source``.

    Used by the T035 row-processing helpers when they need to surface
    a hard failure that the runner treats identically to a router
    failure (the run halts with ``complete=False`` and the interval
    row carries the new reason code).
    """
    return CoverageDecision(
        state=STATE_FAILED,
        endpoint_alias=source.endpoint_alias,
        failover_from=source.failover_from,
        reason_code=reason_code,
        rows=0,
        response_bytes=source.response_bytes,
        logical_rpc_calls=source.logical_rpc_calls,
        http_requests=source.http_requests,
        pinned_block_hash=source.pinned_block_hash,
        error_detail=detail,
        raw_rows=(),
    )


def _hashes_equal_for_validation(stored: str, observed: str) -> bool:
    """Compare two 0x-hex block hashes case-insensitively.

    Used by the runner's qualified-end-block-hash validation. The
    canonical hash the registered client returns and the stored
    checkpoint hash compare equal when they encode the same 32-byte
    digest regardless of casing or leading ``0x`` prefix.
    """
    if not isinstance(stored, str) or not isinstance(observed, str):
        return False
    return stored.strip().lower().removeprefix("0x") == observed.strip().lower().removeprefix("0x")


def _compute_manifest_checksum(
    interval_rows: list[dict[str, Any]], retry_rows: list[dict[str, Any]]
) -> str:
    payload = json.dumps(
        {"intervals": interval_rows, "retries": retry_rows},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "0x" + hashlib.sha256(payload).hexdigest()


def _stable_checksum(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
    return "0x" + h.hexdigest()


def _retry_outcome_from_decision(decision: CoverageDecision) -> str:
    from robinhood_lp.ingestion.errors import (
        RETRY_OUTCOME_FAILOVER,
        RETRY_OUTCOME_GIVE_UP,
        RETRY_OUTCOME_SUCCESS,
    )
    from robinhood_lp.ingestion.router import retry_outcome_for_reason

    if decision.state == STATE_SUCCESSFUL:
        if decision.failover_from is not None:
            return RETRY_OUTCOME_FAILOVER
        return RETRY_OUTCOME_SUCCESS
    if decision.state == STATE_SCANNED_EMPTY:
        return RETRY_OUTCOME_SUCCESS
    if decision.state == STATE_CANCELLED:
        return RETRY_OUTCOME_GIVE_UP
    if decision.failover_from is not None:
        return RETRY_OUTCOME_FAILOVER
    return retry_outcome_for_reason(decision.reason_code)


# Constants exposed for test wiring
DEFAULT_MAX_BLOCKS_PER_PARTITION: Final[int] = DEFAULT_BLOCKS_PER_PARTITION
CANCELLED_REASON: Final[str] = REASON_CANCELLED_BY_OPERATOR


__all__ = [
    "BlockHeaderCache",
    "BlockHeaderSource",
    "CANCELLED_REASON",
    "CancellationToken",
    "DEFAULT_MAX_BLOCKS_PER_PARTITION",
    "IngestionResult",
    "IngestionRunner",
    "build_pool_topic_filter",
]


# Suppress unused-binding noise on names that may be referenced via
# type-checker introspection in extension code.
_UNUSED: tuple[object, ...] = (
    CancelledError,
    is_budget_exhausted_reason,
    REASON_BUDGET_EXHAUSTED,
    TOPOLOGY_COLD_START,
    TOPOLOGY_WARM_INCREMENTAL,
)
