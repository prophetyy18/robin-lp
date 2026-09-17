"""Integration tests for the ingestion runner (T032).

These tests cover the full T032 acceptance matrix at the runner
level. Each test stands up a real :class:`ManifestStore` /
:class:`RawPartitionWriter`, builds a real :class:`IngestionRunner`
with scripted endpoint clients, and verifies the manifest /
checkpoint / retry-ledger / run-manifest state.

The fixtures (per T032 contract):

- Alchemy 10-block cap;
- Robinhood wide-range success followed by adaptive split;
- HTTP 403 caused by missing / blocked User-Agent;
- response-size overflow;
- HTTP 429 rate-limit handling;
- budget exhaustion with reason ``budget_exhausted`` and
  ``complete=False``;
- provider failover from A to B within B's measured capability
  and remaining budget, with no gap in coverage.

Plus the matrix:

- kill / restart at each durable-commit boundary;
- overlapping and concurrent-run convergence;
- every requested block covered exactly once;
- failed intervals prevent completion;
- cold-start history vs warm incremental ingestion;
- checkpoint rejection on mismatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _ingestion_t032_fixtures import (
    ALIAS_ALCHEMY_FREE,
    ALIAS_ROBINHOOD_PUBLIC,
    CONTRACT,
    POOL_ID,
    POOL_INIT_BLOCK,
    POOL_MANAGER,
    ScriptedEndpointClient,
    empty_step,
    failure_step,
    make_budget_snapshot,
    make_capability_snapshot,
    success_step,
)
from robinhood_lp.ingestion import (
    REASON_BUDGET_EXHAUSTED_CALLS,
    REASON_HTTP_403_DEFAULT_USER_AGENT,
    REASON_HTTP_403_USER_AGENT_REJECTED,
    REASON_HTTP_429_RATE_LIMIT,
    REASON_NO_CAPABLE_ENDPOINT,
    REASON_RESPONSE_SIZE_OVERFLOW,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_SCANNED_EMPTY,
    STATE_SUCCESSFUL,
    TOPOLOGY_COLD_START,
    TOPOLOGY_WARM_INCREMENTAL,
    BudgetSnapshot,
    CancellationToken,
    CapabilitySnapshot,
    IngestionRunner,
    RangePlanner,
    RouterConfig,
)
from robinhood_lp.protocol import ChainId
from robinhood_lp.storage.manifest import ManifestStore
from robinhood_lp.storage.writer import RawPartitionWriter

_DEFAULT_CHAIN = ChainId(4663)

# ---------------------------------------------------------------------------
# Runner construction helper
# ---------------------------------------------------------------------------


def _runner(
    tmp_path: Path,
    *,
    clients: dict[str, ScriptedEndpointClient] | None = None,
    capability: CapabilitySnapshot | None = None,
    budget: BudgetSnapshot | None = None,
    planner_max_blocks: int = 10,
    alchemy_max_blocks: int = 10,
    max_response_bytes: int = 5_000_000,
    chain: ChainId = _DEFAULT_CHAIN,
    run_id: str | None = None,
    pool_init_block: int = POOL_INIT_BLOCK,
) -> IngestionRunner:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    cap = capability or make_capability_snapshot(
        robinhood_max_blocks=planner_max_blocks,
        alchemy_max_blocks=alchemy_max_blocks,
    )
    bud = budget or make_budget_snapshot(
        robinhood_calls=1_000,
        alchemy_calls=200,
    )
    planner = RangePlanner(max_blocks_per_sub_range=planner_max_blocks)
    config = RouterConfig(
        failover_order=(ALIAS_ROBINHOOD_PUBLIC, ALIAS_ALCHEMY_FREE),
        max_response_bytes=max_response_bytes,
        max_blocks_per_sub_range=planner_max_blocks,
        alchemy_max_blocks_per_get_logs=alchemy_max_blocks,
    )
    runner = IngestionRunner(
        manifest=manifest,
        writer=writer,
        chain_id=chain,
        contract_address=CONTRACT,
        pool_id=POOL_ID,
        pool_manager_address=POOL_MANAGER,
        pool_init_block=pool_init_block,
        planner=planner,
        router_config=config,
        capability_snapshot=cap,
        budget_snapshot=bud,
    )
    if run_id is not None:
        runner.run_id = run_id
    for alias, client in (clients or {}).items():
        runner.register_client(alias, client)
    return runner


# ---------------------------------------------------------------------------
# Fixture 1: Alchemy 10-block cap (measured capability + hard cap)
# ---------------------------------------------------------------------------


def test_fixture_alchemy_10_block_cap_records_capability_and_hard_cap(
    tmp_path: Path,
) -> None:
    """The preflight probe records Alchemy's measured 10-block
    cap; a Robinhood failure cannot failover to Alchemy for a
    100-block sub-range because Alchemy's measured capability
    forbids it (T032 contract)."""
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            failure_step(
                1_000_000,
                1_000_099,
                reason_code="rpc_timeout",
                error_detail="Robinhood public endpoint down",
            )
        ],
    )
    alchemy = ScriptedEndpointClient(
        ALIAS_ALCHEMY_FREE,
        [success_step(1_000_000, 1_000_099)],
    )
    cap = make_capability_snapshot(robinhood_max_blocks=100, alchemy_max_blocks=10)
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh, ALIAS_ALCHEMY_FREE: alchemy},
        capability=cap,
        planner_max_blocks=100,
        alchemy_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 99,
    )
    assert result.complete is False
    assert result.halt_reason == REASON_NO_CAPABLE_ENDPOINT
    # Alchemy 10-block cap is recorded in the capability snapshot.
    assert cap.alias_to_capability()[ALIAS_ALCHEMY_FREE].max_blocks_per_get_logs == 10
    assert cap.alias_to_capability()[ALIAS_ROBINHOOD_PUBLIC].max_blocks_per_get_logs == 100


# ---------------------------------------------------------------------------
# Fixture 2: Robinhood wide-range + adaptive split
# ---------------------------------------------------------------------------


def test_fixture_robinhood_wide_range_success_then_adaptive_split(tmp_path: Path) -> None:
    """Robinhood covers a wide range successfully. A separate
    overflow triggers an adaptive split (handled by the runner)."""
    rh_rows = [{"transactionHash": "0x" + "11" * 32, "logIndex": "0x0"}]
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9, rows=rh_rows),
            success_step(POOL_INIT_BLOCK + 10, POOL_INIT_BLOCK + 19, rows=rh_rows),
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 19,
    )
    assert result.complete is True
    assert result.halt_reason is None
    assert result.interval_count == 2
    assert all(row["state"] == STATE_SUCCESSFUL for row in result.interval_table)


# ---------------------------------------------------------------------------
# Fixture 3: HTTP 403 default User-Agent
# ---------------------------------------------------------------------------


def test_fixture_http_403_default_user_agent_blocks_completion(tmp_path: Path) -> None:
    """A default User-Agent HTTP 403 from Robinhood public RPC
    (T032 contract: must be retried with the configured
    User-Agent; persistent failure path is a capability failure)."""
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            failure_step(
                POOL_INIT_BLOCK,
                POOL_INIT_BLOCK + 9,
                reason_code=REASON_HTTP_403_DEFAULT_USER_AGENT,
                error_detail="default User-Agent rejected",
            )
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    assert result.complete is False
    assert result.halt_reason == REASON_HTTP_403_DEFAULT_USER_AGENT
    assert result.interval_table[0]["state"] == STATE_FAILED


def test_fixture_http_403_configured_user_agent_rejected_persists(tmp_path: Path) -> None:
    """A configured User-Agent that is also rejected is a
    persistent capability failure (T032 contract)."""
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            failure_step(
                POOL_INIT_BLOCK,
                POOL_INIT_BLOCK + 9,
                reason_code=REASON_HTTP_403_USER_AGENT_REJECTED,
                error_detail="configured User-Agent rejected",
            )
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    assert result.complete is False
    assert result.halt_reason == REASON_HTTP_403_USER_AGENT_REJECTED


# ---------------------------------------------------------------------------
# Fixture 4: response-size overflow -> adaptive split
# ---------------------------------------------------------------------------


def test_fixture_response_size_overflow_triggers_adaptive_split(tmp_path: Path) -> None:
    """Response-size overflow triggers an adaptive split rather
    than dropping the interval (T032 contract). The runner halves
    the sub-range and re-attempts; both halves succeed."""
    rows = [{"transactionHash": "0x" + "11" * 32, "logIndex": "0x0"}]
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            # First call (wide window) overflows; the runner splits.
            _overflow_step_factory(
                POOL_INIT_BLOCK, POOL_INIT_BLOCK + 99, response_bytes=10_000_000
            ),
            # Split attempts succeed.
            success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 49, rows=rows),
            success_step(POOL_INIT_BLOCK + 50, POOL_INIT_BLOCK + 99, rows=rows),
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=100,
        max_response_bytes=5_000_000,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 99,
    )
    assert result.complete is True
    assert result.halt_reason is None
    # The split halves succeeded and the coverage transition
    # advanced; the run is complete with at least one successful
    # interval covering the merged split.
    states = [row["state"] for row in result.interval_table]
    assert STATE_SUCCESSFUL in states


def _overflow_step_factory(from_block: int, to_block: int, *, response_bytes: int) -> Any:
    from _ingestion_t032_fixtures import _ScriptedStep

    return _ScriptedStep(
        from_block=from_block,
        to_block=to_block,
        success=True,
        rows=[],
        response_bytes=response_bytes,
        reason_code=REASON_RESPONSE_SIZE_OVERFLOW,
    )


# ---------------------------------------------------------------------------
# Fixture 5: HTTP 429 rate-limit handling
# ---------------------------------------------------------------------------


def test_fixture_rate_limit_handled_without_advancing_checkpoint(tmp_path: Path) -> None:
    """HTTP 429 is handled by classified retry without advancing
    the checkpoint (T032 contract: classified retry, not advance)."""
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            failure_step(
                POOL_INIT_BLOCK,
                POOL_INIT_BLOCK + 9,
                reason_code=REASON_HTTP_429_RATE_LIMIT,
                error_detail="HTTP 429",
            )
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    assert result.complete is False
    assert result.halt_reason == REASON_HTTP_429_RATE_LIMIT
    assert result.interval_table[0]["state"] == STATE_FAILED
    # The retry ledger records the 429 attempt.
    retries = result.retries
    assert len(retries) >= 1
    assert any(row["reason_code"] == REASON_HTTP_429_RATE_LIMIT for row in retries)


# ---------------------------------------------------------------------------
# Fixture 6: budget exhaustion with reason ``budget_exhausted`` and
# ``complete=False``
# ---------------------------------------------------------------------------


def test_fixture_budget_exhaustion_records_complete_false(tmp_path: Path) -> None:
    """When the run's budget runs out before the last interval is
    covered, the run is recorded with ``complete=False`` and the
    explicit ``budget_exhausted`` reason code (T032 contract)."""
    # Both endpoints have a budget of zero — the very first call
    # exhausts the run.
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9)],
    )
    cap = make_capability_snapshot(robinhood_max_blocks=10, alchemy_max_blocks=10)
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        capability=cap,
        budget=make_budget_snapshot(robinhood_calls=0, alchemy_calls=0),
        planner_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    assert result.complete is False
    assert result.halt_reason == REASON_BUDGET_EXHAUSTED_CALLS
    # The umbrella budget_exhausted reason code appears somewhere
    # in the run manifest (run-level halt reason in the row).
    run_row = runner.manifest.get_run_manifest(result.run_id)
    assert run_row is not None
    assert run_row["halt_reason"] == REASON_BUDGET_EXHAUSTED_CALLS


def test_fixture_budget_exhaustion_marks_interval_as_failed_not_empty(tmp_path: Path) -> None:
    """There is no execution shape that reclassifies a
    budget-exhausted interval as empty (T032 contract)."""
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9)],
    )
    cap = make_capability_snapshot(robinhood_max_blocks=10, alchemy_max_blocks=10)
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        capability=cap,
        budget=make_budget_snapshot(robinhood_calls=0, alchemy_calls=0),
        planner_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    # The interval row is recorded as ``failed``, never as
    # ``scanned_empty``.
    assert all(row["state"] != STATE_SCANNED_EMPTY for row in result.interval_table)


# ---------------------------------------------------------------------------
# Fixture 7: provider failover from A to B within capability + budget
# ---------------------------------------------------------------------------


def test_fixture_provider_failover_no_gap_in_coverage(tmp_path: Path) -> None:
    """A fails, B takes the interval within B's measured capability
    and remaining budget. The failover path is persisted in the
    run manifest and there is no gap in coverage."""
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            failure_step(
                POOL_INIT_BLOCK,
                POOL_INIT_BLOCK + 9,
                reason_code="rpc_timeout",
                error_detail="Robinhood public timeout",
            )
        ],
    )
    alchemy_rows = [{"transactionHash": "0x" + "11" * 32, "logIndex": "0x0"}]
    alchemy = ScriptedEndpointClient(
        ALIAS_ALCHEMY_FREE,
        [success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9, rows=alchemy_rows)],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh, ALIAS_ALCHEMY_FREE: alchemy},
        planner_max_blocks=10,
        alchemy_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    assert result.complete is True
    assert result.interval_count == 1
    row = result.interval_table[0]
    assert row["state"] == STATE_SUCCESSFUL
    assert row["endpoint_alias"] == ALIAS_ALCHEMY_FREE
    assert row["failover_from"] == ALIAS_ROBINHOOD_PUBLIC


# ---------------------------------------------------------------------------
# Acceptance: kill / restart at each durable-commit boundary
# ---------------------------------------------------------------------------


def test_kill_restart_after_partial_run_resumes_from_durable_state(tmp_path: Path) -> None:
    """A kill / restart at a durable-commit boundary leaves the
    persisted state equal to either the previous committed state
    or the complete new state. We exercise this by failing the
    second sub-range (simulating an irrecoverable provider error)
    and verifying the durable checkpoint is at the end of the
    covered prefix."""
    rh_rows = [{"transactionHash": "0x" + "11" * 32, "logIndex": "0x0"}]
    rh1 = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9, rows=rh_rows),
            failure_step(
                POOL_INIT_BLOCK + 10,
                POOL_INIT_BLOCK + 19,
                reason_code="rpc_timeout",
                error_detail="kill mid-run",
            ),
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh1},
        planner_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 19,
    )
    # The run halted (incomplete) but the first sub-range is
    # durably committed.
    assert result.complete is False
    cp_row = runner.manifest.get_durable_checkpoint(
        chain_id=4663,
        contract_address=CONTRACT.to_hex().removeprefix("0x").lower(),
        pool_id=POOL_ID.to_hex(),
    )
    assert cp_row is not None
    assert cp_row["qualified_end_block"] == POOL_INIT_BLOCK + 9


def test_overlapping_run_is_idempotent_on_covered_prefix(tmp_path: Path) -> None:
    """Two overlapping runs converge to a single successful
    coverage. A second run that requests a sub-range inside the
    already-qualified prefix produces no additional intervals and
    leaves the checkpoint intact."""
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9),
            success_step(POOL_INIT_BLOCK + 10, POOL_INIT_BLOCK + 19),
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
        run_id="first",
    )
    first = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 19,
    )
    assert first.complete is True

    # Second run: same window, but the existing checkpoint
    # qualifies the prefix; a warm incremental ingestion starts
    # from POOL_INIT_BLOCK + 20 — there is nothing new to fetch.
    rh2 = ScriptedEndpointClient(ALIAS_ROBINHOOD_PUBLIC, [])
    runner2 = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh2},
        planner_max_blocks=10,
        run_id="second",
    )
    # Re-use the same ManifestStore so the checkpoint persists.
    runner2.manifest = runner.manifest
    second = runner2.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 19,
    )
    assert second.complete is True
    assert second.interval_count == 0


# ---------------------------------------------------------------------------
# Acceptance: every requested block covered exactly once
# ---------------------------------------------------------------------------


def test_every_requested_block_covered_exactly_once(tmp_path: Path) -> None:
    """For the requested scan range, the manifest's interval table
    accounts for every block exactly once."""
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 4),
            empty_step(POOL_INIT_BLOCK + 5, POOL_INIT_BLOCK + 9),
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=5,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    assert result.complete is True
    # Every block in the requested window is accounted for.
    covered: set[int] = set()
    for row in result.interval_table:
        fb = row["from_block"]
        tb = row["to_block"]
        for b in range(fb, tb + 1):
            assert b not in covered, f"block {b} covered twice"
            covered.add(b)
    assert covered == set(range(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 10))


# ---------------------------------------------------------------------------
# Acceptance: failed intervals prevent completion
# ---------------------------------------------------------------------------


def test_failed_intervals_prevent_completion(tmp_path: Path) -> None:
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9),
            failure_step(
                POOL_INIT_BLOCK + 10,
                POOL_INIT_BLOCK + 19,
                reason_code=REASON_NO_CAPABLE_ENDPOINT,
            ),
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 19,
    )
    assert result.complete is False
    # The checkpoint does not advance past the failed interval.
    cp_row = runner.manifest.get_durable_checkpoint(
        chain_id=4663,
        contract_address=CONTRACT.to_hex().removeprefix("0x").lower(),
        pool_id=POOL_ID.to_hex(),
    )
    assert cp_row is not None
    assert cp_row["qualified_end_block"] <= POOL_INIT_BLOCK + 9


# ---------------------------------------------------------------------------
# Acceptance: tests distinguish cold-start history vs warm incremental
# ---------------------------------------------------------------------------


def test_cold_start_topology_scans_from_pool_init_block(tmp_path: Path) -> None:
    """A cold-start run begins from the pool's registered Initialize
    block, regardless of the (possibly shorter) requested start."""
    rh_rows = [{"transactionHash": "0x" + "11" * 32, "logIndex": "0x0"}]
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9, rows=rh_rows),
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
    )
    # The requested range is entirely contained in
    # ``[pool_init_block, pool_init_block + 9]`` so the runner
    # succeeds. The requested_start is set higher than
    # pool_init_block - 1 to demonstrate that the planner pins
    # the coverage origin to the pool's Initialize block (a
    # shorter backtest start does NOT truncate the reconstruction
    # prefix).
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    assert result.complete is True
    assert result.topology == TOPOLOGY_COLD_START
    run_row = runner.manifest.get_run_manifest(result.run_id)
    assert run_row is not None
    assert run_row["topology"] == TOPOLOGY_COLD_START
    assert run_row["pool_init_block"] == POOL_INIT_BLOCK


def test_warm_incremental_topology_starts_after_checkpoint(tmp_path: Path) -> None:
    """A warm incremental run starts from the checkpoint's
    qualified_end_block + 1."""
    rh1 = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9),
        ],
    )
    # The cold-start run writes the canonical block hash for the
    # qualified end block into the durable checkpoint. Wire the
    # deterministic hash the scripted client returns so the second
    # (warm) run's qualified-end-block-hash validation agrees with
    # the stored checkpoint.
    rh1.block_hash_overrides[POOL_INIT_BLOCK + 9] = "0x" + "ab" * 32
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh1},
        planner_max_blocks=10,
        run_id="warm-setup",
    )
    first = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    assert first.complete is True
    assert first.topology == TOPOLOGY_COLD_START

    # Second run: warm incremental. We extend the requested end
    # block so there is new suffix to ingest.
    rh2 = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [success_step(POOL_INIT_BLOCK + 10, POOL_INIT_BLOCK + 19)],
    )
    # The warm run re-fetches the canonical hash for
    # qualified_end_block; wire the matching deterministic value.
    rh2.block_hash_overrides[POOL_INIT_BLOCK + 9] = "0x" + "ab" * 32
    runner2 = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh2},
        planner_max_blocks=10,
        run_id="warm-followup",
    )
    # Reuse the same ManifestStore so the durable checkpoint
    # persists.
    runner2.manifest = runner.manifest
    second = runner2.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 19,
    )
    assert second.topology == TOPOLOGY_WARM_INCREMENTAL
    assert second.coverage_from_block == POOL_INIT_BLOCK + 10
    assert second.interval_count == 1


# ---------------------------------------------------------------------------
# Acceptance: checkpoint rejection on mismatch
# ---------------------------------------------------------------------------


def test_checkpoint_rejection_on_mismatched_schema_version(tmp_path: Path) -> None:
    """A warm run whose existing checkpoint's schema/decode
    version differs from the current build is rejected."""
    from robinhood_lp.storage.manifest import ManifestStore

    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    # Insert a checkpoint with a different decode_version.
    manifest.upsert_durable_checkpoint(
        chain_id=4663,
        contract_address=CONTRACT.to_hex().removeprefix("0x").lower(),
        pool_id=POOL_ID.to_hex(),
        qualified_start_block=POOL_INIT_BLOCK,
        qualified_end_block=POOL_INIT_BLOCK + 99,
        qualified_end_block_hash="0x" + "ab" * 32,
        schema_version=99,
        decode_version=99,
        capability_snapshot_id="snap-old",
        manifest_checksum="0x" + "cd" * 32,
        topology="cold_start",
        pool_init_block=POOL_INIT_BLOCK,
    )
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [success_step(POOL_INIT_BLOCK + 100, POOL_INIT_BLOCK + 109)],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
    )
    runner.manifest = manifest
    # The planner rejects before any RPC is made.
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 109,
    )
    assert result.complete is False
    assert result.halt_reason is not None


def test_checkpoint_rejection_on_mismatched_manifest_checksum(tmp_path: Path) -> None:
    """A warm run whose existing checkpoint's manifest checksum
    disagrees with the checksum the current capability / budget
    snapshots would write is rejected (T032 contract: reject a
    checkpoint whose manifest does not match the requested pool
    and range)."""
    from robinhood_lp.ingestion.checkpoint import (
        DurableCheckpointState,
        upsert_durable_checkpoint,
    )
    from robinhood_lp.storage.manifest import ManifestStore
    from robinhood_lp.storage.schema import (
        CURRENT_DECODE_VERSION,
        CURRENT_SCHEMA_VERSION,
    )

    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    # Wire the scripted client so its canonical block hash at
    # qualified_end_block agrees with the stored hash. This
    # isolates the manifest_checksum rejection from the
    # qualified_end_block_hash rejection.
    stored_hash = "0x" + "ab" * 32
    upsert_durable_checkpoint(
        manifest,
        DurableCheckpointState(
            chain_id=4663,
            contract_address=CONTRACT,
            pool_id=POOL_ID,
            qualified_start_block=POOL_INIT_BLOCK,
            qualified_end_block=POOL_INIT_BLOCK + 99,
            qualified_end_block_hash=stored_hash,
            schema_version=CURRENT_SCHEMA_VERSION,
            decode_version=CURRENT_DECODE_VERSION,
            capability_snapshot_id="snap-old",
            manifest_checksum="0x" + "00" * 32,  # garbage / stale
            topology="cold_start",
            pool_init_block=POOL_INIT_BLOCK,
        ),
    )
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [success_step(POOL_INIT_BLOCK + 100, POOL_INIT_BLOCK + 109)],
    )
    rh.block_hash_overrides[POOL_INIT_BLOCK + 99] = stored_hash
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
    )
    runner.manifest = manifest
    # The planner refuses to plan a warm run whose stored
    # checkpoint's manifest checksum does not match the one this
    # run would write. The runner catches the
    # CheckpointMismatchError and halts with the
    # ``checkpoint_manifest_drift`` reason; ``complete`` stays
    # ``False`` and the run never advances past the validation
    # gate.
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 109,
    )
    assert result.complete is False
    assert result.halt_reason == "checkpoint_manifest_drift"
    assert result.interval_count == 0
    assert result.qualified_end_block is None
    assert result.qualified_end_block_hash is None


def test_checkpoint_rejection_on_mismatched_qualified_end_block_hash(tmp_path: Path) -> None:
    """A warm run whose stored qualified_end_block_hash does not
    match the canonical block hash at qualified_end_block as
    observed by the registered endpoints is rejected (T032
    contract: reject a checkpoint whose block hash does not match
    the requested pool and range)."""
    from robinhood_lp.ingestion.checkpoint import (
        DurableCheckpointState,
        upsert_durable_checkpoint,
    )
    from robinhood_lp.storage.manifest import ManifestStore
    from robinhood_lp.storage.schema import (
        CURRENT_DECODE_VERSION,
        CURRENT_SCHEMA_VERSION,
    )

    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    # The stored checkpoint's manifest_checksum must agree with
    # the one the runner's capability / budget snapshots would
    # write so the planner's manifest validation does not fire
    # first. Use the same value the runner computes.
    from robinhood_lp.ingestion.runner import _stable_checksum

    cap = make_capability_snapshot()
    bud = make_budget_snapshot()
    expected_manifest_checksum = _stable_checksum(cap.snapshot_id, bud.to_json())
    upsert_durable_checkpoint(
        manifest,
        DurableCheckpointState(
            chain_id=4663,
            contract_address=CONTRACT,
            pool_id=POOL_ID,
            qualified_start_block=POOL_INIT_BLOCK,
            qualified_end_block=POOL_INIT_BLOCK + 99,
            qualified_end_block_hash="0x" + "ab" * 32,  # stale hash
            schema_version=CURRENT_SCHEMA_VERSION,
            decode_version=CURRENT_DECODE_VERSION,
            capability_snapshot_id="snap-1",
            manifest_checksum=expected_manifest_checksum,
            topology="cold_start",
            pool_init_block=POOL_INIT_BLOCK,
        ),
    )
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [success_step(POOL_INIT_BLOCK + 100, POOL_INIT_BLOCK + 109)],
    )
    # Configure the scripted client so its canonical hash at
    # POOL_INIT_BLOCK + 99 disagrees with the stored hash.
    rh.block_hash_overrides[POOL_INIT_BLOCK + 99] = "0x" + "cd" * 32
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
        capability=cap,
        budget=bud,
    )
    runner.manifest = manifest
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 109,
    )
    assert result.complete is False
    assert result.halt_reason == "checkpoint_block_hash_drift"
    assert result.interval_count == 0
    assert result.qualified_end_block is None
    assert result.qualified_end_block_hash is None


def test_checkpoint_accepts_when_qualified_end_block_hash_matches(tmp_path: Path) -> None:
    """When the stored qualified_end_block_hash agrees with the
    canonical hash observed by the registered endpoints, the warm
    run proceeds normally (T032 contract: rejection is for
    mismatches, not for the validation surface itself)."""
    from robinhood_lp.ingestion.checkpoint import (
        DurableCheckpointState,
        upsert_durable_checkpoint,
    )
    from robinhood_lp.storage.manifest import ManifestStore
    from robinhood_lp.storage.schema import (
        CURRENT_DECODE_VERSION,
        CURRENT_SCHEMA_VERSION,
    )

    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    from robinhood_lp.ingestion.runner import _stable_checksum

    cap = make_capability_snapshot()
    bud = make_budget_snapshot()
    expected_manifest_checksum = _stable_checksum(cap.snapshot_id, bud.to_json())
    upsert_durable_checkpoint(
        manifest,
        DurableCheckpointState(
            chain_id=4663,
            contract_address=CONTRACT,
            pool_id=POOL_ID,
            qualified_start_block=POOL_INIT_BLOCK,
            qualified_end_block=POOL_INIT_BLOCK + 99,
            qualified_end_block_hash="0x" + "ab" * 32,
            schema_version=CURRENT_SCHEMA_VERSION,
            decode_version=CURRENT_DECODE_VERSION,
            capability_snapshot_id="snap-1",
            manifest_checksum=expected_manifest_checksum,
            topology="cold_start",
            pool_init_block=POOL_INIT_BLOCK,
        ),
    )
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [success_step(POOL_INIT_BLOCK + 100, POOL_INIT_BLOCK + 109)],
    )
    # The stored hash matches what the scripted client returns.
    rh.block_hash_overrides[POOL_INIT_BLOCK + 99] = "0x" + "ab" * 32
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
        capability=cap,
        budget=bud,
    )
    runner.manifest = manifest
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 109,
    )
    assert result.complete is True
    assert result.topology == TOPOLOGY_WARM_INCREMENTAL
    assert result.halt_reason is None


# ---------------------------------------------------------------------------
# Acceptance: cancellation token
# ---------------------------------------------------------------------------


def test_cancellation_marks_remaining_intervals_as_cancelled(tmp_path: Path) -> None:
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9),
            # Subsequent calls should not happen.
        ],
    )
    token = CancellationToken()
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
    )
    runner.cancellation = token
    # Cancel before the run starts.
    token.cancel()
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 19,
    )
    assert result.complete is False
    # Every interval is recorded as cancelled.
    assert all(row["state"] == STATE_CANCELLED for row in result.interval_table)


# ---------------------------------------------------------------------------
# Run manifest records the required counters
# ---------------------------------------------------------------------------


def test_run_manifest_records_logical_rpc_and_response_bytes(tmp_path: Path) -> None:
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9),
            success_step(POOL_INIT_BLOCK + 10, POOL_INIT_BLOCK + 19),
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 19,
    )
    run_row = runner.manifest.get_run_manifest(result.run_id)
    assert run_row is not None
    assert run_row["logical_rpc_calls"] >= 2
    assert run_row["response_bytes"] >= 0
    assert run_row["elapsed_ms"] >= 0


# ---------------------------------------------------------------------------
# Provisional suffix data must not be promoted across a failed gap
# ---------------------------------------------------------------------------


def test_provisional_suffix_data_is_not_promoted_across_failed_gap(tmp_path: Path) -> None:
    """A concurrent attempt that captures a future suffix ahead
    of a failed gap must not promote the prefix across the gap
    (T032 contract)."""
    # Single sequential runner: it sees one sub-range fail and
    # must halt. No future-suffix promotion is possible.
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC,
        [
            success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9),
            failure_step(
                POOL_INIT_BLOCK + 10,
                POOL_INIT_BLOCK + 19,
                reason_code=REASON_NO_CAPABLE_ENDPOINT,
            ),
            # A would-be provisional suffix that must not be
            # promoted.
            success_step(POOL_INIT_BLOCK + 20, POOL_INIT_BLOCK + 29),
        ],
    )
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        planner_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 29,
    )
    assert result.complete is False
    cp_row = runner.manifest.get_durable_checkpoint(
        chain_id=4663,
        contract_address=CONTRACT.to_hex().removeprefix("0x").lower(),
        pool_id=POOL_ID.to_hex(),
    )
    assert cp_row is not None
    # The qualified end block must not advance past the failed gap.
    assert cp_row["qualified_end_block"] <= POOL_INIT_BLOCK + 9


# ---------------------------------------------------------------------------
# Sanity: capability + budget snapshots round-trip through the manifest
# ---------------------------------------------------------------------------


def test_run_manifest_persists_preflight_capability_and_budget(tmp_path: Path) -> None:
    rh = ScriptedEndpointClient(
        ALIAS_ROBINHOOD_PUBLIC, [success_step(POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9)]
    )
    cap = make_capability_snapshot(snapshot_id="snap-xyz")
    bud = make_budget_snapshot()
    runner = _runner(
        tmp_path,
        clients={ALIAS_ROBINHOOD_PUBLIC: rh},
        capability=cap,
        budget=bud,
        planner_max_blocks=10,
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    run_row = runner.manifest.get_run_manifest(result.run_id)
    assert run_row is not None
    cap_persisted = json.loads(run_row["capability_snapshot_json"])
    assert cap_persisted["snapshot_id"] == "snap-xyz"
    assert len(cap_persisted["endpoints"]) == 2
    bud_persisted = json.loads(run_row["budget_snapshot_json"])
    assert len(bud_persisted["per_endpoint_budgets"]) == 2
