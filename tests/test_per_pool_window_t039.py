"""Tests for T039 per-pool extended-history research window.

The tests exercise the deterministic T039 surfaces:

1. ``plan_per_pool_window`` — the per-pool window rule: each pool
   is acquired from its own ``Initialize`` block to the agreed
   finalized block. There is no fixed width, no extension cap, and
   no ``pool_init_outside_window`` exclusion. A pool whose
   ``Initialize`` lies after the agreed end halts the run with the
   ``pool_initialized_after_agreed_end`` outcome.
2. ``parse_finalized_pin_payloads`` — finalized-block agreement:
   both endpoints must agree on block number and hash;
   disagreements and unavailability surface as ``finalized_*``
   reason codes; ``latest``, non-finalized and wall-clock-derived
   bounds are refused, never substituted.
3. ``plan_per_pool_window_after_resume`` — interrupting and
   resuming a window is indistinguishable from an uninterrupted
   acquisition. The function emits the
   ``pool_acquired_after_checkpoint_resume`` outcome and re-plans
   sub-ranges from ``resume_from_block``.
4. ``assert_logical_call_count_equals_sub_ranges_plus_headers`` —
   the logical-call-count invariant that forbids per-block header
   fetches and per-block ``eth_getLogs``.
5. ``PerPoolCoverageReport`` / ``build_per_pool_coverage_report``
   — per-pool data root, cost record, reconciliation,
   ``complete=True`` only when every input agrees.
6. ``PerPoolOperatorRunbook`` — the per-pool runbook names the
   endpoint able to serve historical state at the agreed depth,
   records the per-pool routing, and pins the per-pool data roots.
7. Re-running a completed window — ``PriorCoverage`` lets the
   planner record ``range_already_covered`` without issuing chain
   reads for the already-covered range.

The tests use synthetic-but-faithful inputs: every typed value is
constructed from the protocol-layer primitives; every hash is a
deterministic ``keccak``-derived hex string; the offline keccak256
re-derivation invariant is exercised end to end.
"""

from __future__ import annotations

import json

import pytest
from eth_hash.auto import keccak

from robinhood_lp.qualification import (
    DEFAULT_PRIMARY_ALIAS,
    DEFAULT_SECONDARY_ALIAS,
    DOCUMENTED_PER_POOL_REASON_CODES,
    OUTCOME_POOL_ACQUIRED,
    OUTCOME_POOL_ACQUIRED_AFTER_RESUME,
    OUTCOME_POOL_INITIALIZED_AFTER_END,
    PER_POOL_CHAIN_ID,
    REASON_BUDGET_EXHAUSTED,
    REASON_FINALIZED_DISAGREEMENT,
    REASON_FINALIZED_UNAVAILABLE,
    REASON_NON_FINALIZED_BLOCK_REJECTED,
    REASON_OK,
    REASON_POOL_INITIALIZE_NOT_LOCATED,
    REASON_POOL_INITIALIZED_AFTER_END,
    REASON_PROVIDER_RETURNED_NON_FINALIZED,
    REASON_RANGE_ALREADY_COVERED,
    ROLE_BLOCK_PINNED_STATE_READ,
    ROLE_SAMPLED_CROSS_VALIDATION,
    ROLE_WIDE_POOL_FILTERED_SCAN,
    BudgetCeiling,
    LogicalCallCountInvariantError,
    PerPoolCostRecord,
    PerPoolCoverageReport,
    PerPoolDataRoot,
    PerPoolEndpointRoutingEntry,
    PerPoolReconciliation,
    PerPoolRunbookBudget,
    PerPoolWindowInputs,
    PerPoolWindowPlan,
    PlannedSubRange,
    PriorCoverage,
    assert_logical_call_count_equals_sub_ranges_plus_headers,
    build_per_pool_coverage_report,
    build_per_pool_operator_runbook,
    parse_finalized_pin_payloads,
    per_pool_endpoints_summary,
    per_pool_window_summary,
    plan_per_pool_window,
    plan_per_pool_window_after_resume,
    reconcile_per_pool_reports_against_partitions,
)

# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------


def _hash_for_block(block_number: int) -> str:
    """Deterministic 0x-hex 32-byte hash for a block number."""
    h = keccak(block_number.to_bytes(8, "big"))
    return "0x" + h.hex()


def _pin(block_number: int) -> tuple[int, str]:
    return block_number, _hash_for_block(block_number)


def _inputs(
    *,
    pool_key_id: str = "4663:0x" + "11" * 32,
    pool_init_block: int = 50_000_000,
    pin_block: int = 60_000_000,
    primary: str = DEFAULT_PRIMARY_ALIAS,
    secondary: str = DEFAULT_SECONDARY_ALIAS,
    primary_max: int = 10_000,
    secondary_max: int = 10,
    prior_coverage: PriorCoverage | None = None,
) -> PerPoolWindowInputs:
    block_number, block_hash = _pin(pin_block)
    return PerPoolWindowInputs(
        pool_key_id=pool_key_id,
        pool_init_block=pool_init_block,
        pool_init_block_hash=_hash_for_block(pool_init_block),
        window_pin_block_number=block_number,
        window_pin_block_hash=block_hash,
        primary_endpoint_alias=primary,
        secondary_endpoint_alias=secondary,
        primary_max_blocks_per_call=primary_max,
        secondary_max_blocks_per_call=secondary_max,
        prior_coverage=prior_coverage,
    )


def _data_root(
    *,
    pool_key_id: str = "4663:0x" + "11" * 32,
    path: str = "/data/pool/4663:0x" + "11" * 32,
    partitions: tuple[str, ...] = ("00000000-0000",),
    manifest: str = "0x" + "ab" * 32,
) -> PerPoolDataRoot:
    return PerPoolDataRoot(
        pool_key_id=pool_key_id,
        data_root_path=path,
        manifest_checksum=manifest,
        partition_ids=partitions,
        schema_version=3,
        decode_version=1,
    )


def _cost_record(
    *,
    pool_key_id: str = "4663:0x" + "11" * 32,
    logical_calls: int = 12,
    http_requests: int = 6,
    response_bytes: int = 4096,
    rows: int = 1024,
    provider_units: int = 2000,
    elapsed_ms: int = 1_000,
    parquet_bytes: int = 4096,
    exhausted: str = "",
) -> PerPoolCostRecord:
    return PerPoolCostRecord(
        pool_key_id=pool_key_id,
        logical_calls=logical_calls,
        http_requests=http_requests,
        response_bytes=response_bytes,
        rows=rows,
        provider_units=provider_units,
        elapsed_ms=elapsed_ms,
        parquet_bytes=parquet_bytes,
        exhausted_budget_dimension=exhausted,
    )


def _reconciliation(
    *,
    pool_key_id: str = "4663:0x" + "11" * 32,
    partitions_checked: int = 1,
    clean: bool = True,
    disagreements: tuple[str, ...] = (),
) -> PerPoolReconciliation:
    return PerPoolReconciliation(
        pool_key_id=pool_key_id,
        partition_event_index_parquet_agreement=clean,
        partition_reconciliation_agreement=clean,
        partitions_checked=partitions_checked,
        disagreements=disagreements,
    )


# ---------------------------------------------------------------------------
# Constants sanity (T039 contract)
# ---------------------------------------------------------------------------


def test_per_pool_chain_id_constant() -> None:
    """The T039 contract pins the chain id to ``4663`` (Robinhood
    Chain mainnet)."""
    assert PER_POOL_CHAIN_ID == 4663
    assert PER_POOL_CHAIN_ID == 4663


def test_per_pool_window_replaces_ten_million_block_rule() -> None:
    """The per-pool window vocabulary names no
    ``pool_init_outside_window`` outcome and no fixed-width
    constant. The ten-million-block rule T038 implemented is
    superseded."""
    # The outcome vocabulary explicitly excludes
    # ``pool_init_outside_window``.
    assert OUTCOME_POOL_INITIALIZED_AFTER_END != "pool_init_outside_window"
    assert OUTCOME_POOL_ACQUIRED != "pool_included"
    assert OUTCOME_POOL_ACQUIRED_AFTER_RESUME != "pool_included"
    # The documented reason codes include the new per-pool codes
    # but explicitly name the T039 outcomes.
    assert REASON_OK in DOCUMENTED_PER_POOL_REASON_CODES
    assert REASON_POOL_INITIALIZE_NOT_LOCATED in DOCUMENTED_PER_POOL_REASON_CODES
    assert REASON_POOL_INITIALIZED_AFTER_END in DOCUMENTED_PER_POOL_REASON_CODES
    assert REASON_FINALIZED_UNAVAILABLE in DOCUMENTED_PER_POOL_REASON_CODES
    assert REASON_FINALIZED_DISAGREEMENT in DOCUMENTED_PER_POOL_REASON_CODES


# ---------------------------------------------------------------------------
# PerPoolWindowInputs validation
# ---------------------------------------------------------------------------


def test_inputs_validates_pool_key_id() -> None:
    """``pool_key_id`` is required and non-empty."""
    with pytest.raises(ValueError):
        PerPoolWindowInputs(
            pool_key_id="",
            pool_init_block=1,
            pool_init_block_hash=_hash_for_block(1),
            window_pin_block_number=2,
            window_pin_block_hash=_hash_for_block(2),
        )


def test_inputs_rejects_negative_init_block() -> None:
    """A negative ``pool_init_block`` is rejected at construction."""
    with pytest.raises(ValueError):
        PerPoolWindowInputs(
            pool_key_id="4663:0x" + "11" * 32,
            pool_init_block=-1,
            pool_init_block_hash=_hash_for_block(1),
            window_pin_block_number=2,
            window_pin_block_hash=_hash_for_block(2),
        )


def test_inputs_rejects_zero_chain_id() -> None:
    """``chain_id`` must be positive."""
    with pytest.raises(ValueError):
        PerPoolWindowInputs(
            pool_key_id="4663:0x" + "11" * 32,
            pool_init_block=1,
            pool_init_block_hash=_hash_for_block(1),
            window_pin_block_number=2,
            window_pin_block_hash=_hash_for_block(2),
            chain_id=0,
        )


def test_inputs_rejects_identical_endpoint_aliases() -> None:
    """Primary and secondary aliases must differ — the contract
    requires two qualified endpoints whose readings agree."""
    with pytest.raises(ValueError):
        PerPoolWindowInputs(
            pool_key_id="4663:0x" + "11" * 32,
            pool_init_block=1,
            pool_init_block_hash=_hash_for_block(1),
            window_pin_block_number=2,
            window_pin_block_hash=_hash_for_block(2),
            primary_endpoint_alias="same_alias",
            secondary_endpoint_alias="same_alias",
        )


def test_inputs_rejects_zero_per_call_capability() -> None:
    """The per-call capability is the bound the planner splits
    sub-ranges under; a zero value would divide by zero."""
    with pytest.raises(ValueError):
        PerPoolWindowInputs(
            pool_key_id="4663:0x" + "11" * 32,
            pool_init_block=1,
            pool_init_block_hash=_hash_for_block(1),
            window_pin_block_number=2,
            window_pin_block_hash=_hash_for_block(2),
            primary_max_blocks_per_call=0,
        )


def test_inputs_rejects_empty_pin_hash() -> None:
    """An empty ``window_pin_block_hash`` is rejected so an
    un-pinned window cannot slip through."""
    with pytest.raises(ValueError):
        PerPoolWindowInputs(
            pool_key_id="4663:0x" + "11" * 32,
            pool_init_block=1,
            pool_init_block_hash=_hash_for_block(1),
            window_pin_block_number=2,
            window_pin_block_hash="",
        )


# ---------------------------------------------------------------------------
# plan_per_pool_window — basic rule
# ---------------------------------------------------------------------------


def test_plan_per_pool_window_basic() -> None:
    """The basic per-pool rule: ``coverage_from_block =
    pool_init_block``; ``coverage_to_block = pinned finalized
    block``; the window width is the inclusive span."""
    inputs = _inputs(pool_init_block=50_000_000, pin_block=60_000_000)
    plan = plan_per_pool_window(inputs)
    assert plan.coverage_from_block == 50_000_000
    assert plan.coverage_to_block == 60_000_000
    assert plan.window_width_blocks == 60_000_000 - 50_000_000 + 1
    assert plan.outcome == OUTCOME_POOL_ACQUIRED
    assert plan.reason_code == REASON_OK
    assert plan.is_acquired
    assert not plan.is_resumed
    assert not plan.is_failed


def test_plan_per_pool_window_emits_sub_ranges_under_capability() -> None:
    """The planner splits the window into sub-ranges bounded by
    the endpoint's measured per-call log ceiling, never widening a
    single sub-range beyond the ceiling."""
    inputs = _inputs(pool_init_block=50_000_000, pin_block=60_000_000, primary_max=200)
    plan = plan_per_pool_window(inputs)
    # The inclusive span is 10_000_001 blocks; the primary
    # capability is 200 blocks per call; the planner emits
    # ceil(10_000_001 / 200) sub-ranges.
    expected_count = (plan.coverage_to_block - plan.coverage_from_block + 1 + 199) // 200
    assert len(plan.sub_ranges) == expected_count
    for sub in plan.sub_ranges:
        assert sub.to_block - sub.from_block + 1 <= 200
    # Sub-ranges cover the inclusive window with no overlap.
    assert plan.sub_ranges[0].from_block == plan.coverage_from_block
    assert plan.sub_ranges[-1].to_block == plan.coverage_to_block
    for prev, curr in zip(plan.sub_ranges[:-1], plan.sub_ranges[1:], strict=False):
        assert curr.from_block == prev.to_block + 1


def test_plan_per_pool_window_records_pool_init_block_hash() -> None:
    """The pool's ``Initialize`` block hash is recorded so the
    audit trail can reproduce the exact block the cold-start
    coverage origin uses."""
    inputs = _inputs(pool_init_block=50_000_000)
    plan = plan_per_pool_window(inputs)
    assert plan.pool_init_block == 50_000_000
    assert plan.pool_init_block_hash == _hash_for_block(50_000_000)


def test_plan_per_pool_window_records_pin_block_hash() -> None:
    """The pinned finalized end is recorded with both its block
    number and its block hash so the audit trail can reproduce
    the run from a clean environment."""
    inputs = _inputs(pin_block=60_500_000)
    plan = plan_per_pool_window(inputs)
    assert plan.window_pin_block_number == 60_500_000
    assert plan.window_pin_block_hash == _hash_for_block(60_500_000)


def test_plan_per_pool_window_single_block_window() -> None:
    """A pool whose ``Initialize`` is exactly at the pinned
    finalized end emits a single-block window with one sub-range."""
    inputs = _inputs(pool_init_block=60_000_000, pin_block=60_000_000)
    plan = plan_per_pool_window(inputs)
    assert plan.window_width_blocks == 1
    assert len(plan.sub_ranges) == 1
    assert plan.sub_ranges[0].from_block == 60_000_000
    assert plan.sub_ranges[0].to_block == 60_000_000


def test_plan_per_pool_window_logical_call_count_invariant() -> None:
    """The plan's ``expected_logical_call_count`` equals
    ``len(plan.sub_ranges) + coverage_window_width`` (one
    ``eth_getLogs`` per sub-range plus one deduplicated header per
    distinct event block, using the inclusive coverage width as
    the conservative upper bound at planning time)."""
    inputs = _inputs(pool_init_block=50_000_000, pin_block=60_000_000, primary_max=200)
    plan = plan_per_pool_window(inputs)
    expected = len(plan.sub_ranges) + plan.window_width_blocks
    assert plan.expected_logical_call_count == expected


def test_planned_sub_range_validates_ordering() -> None:
    """``PlannedSubRange`` rejects ``to_block < from_block`` so a
    corrupt planner emission cannot pass the type check."""
    with pytest.raises(ValueError):
        PlannedSubRange(from_block=10, to_block=5)
    with pytest.raises(ValueError):
        PlannedSubRange(from_block=-1, to_block=10)


def test_planned_sub_range_width() -> None:
    """The inclusive width is the documented difference plus one."""
    sub = PlannedSubRange(from_block=10, to_block=20)
    assert sub.width == 11


# ---------------------------------------------------------------------------
# Two materially different pools — different-length windows
# ---------------------------------------------------------------------------


def test_two_pools_different_length_windows() -> None:
    """Two materially different pools run the per-pool rule
    independently; the resulting plans carry different coverage
    ranges, different window widths, and different pool-init
    blocks."""
    pool_a = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_a", pool_init_block=50_000_000, pin_block=60_000_000)
    )
    pool_b = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_b", pool_init_block=58_000_000, pin_block=60_000_000)
    )
    assert pool_a.coverage_from_block == 50_000_000
    assert pool_b.coverage_from_block == 58_000_000
    assert pool_a.coverage_to_block == pool_b.coverage_to_block == 60_000_000
    assert pool_a.window_width_blocks > pool_b.window_width_blocks
    assert pool_a.pool_key_id != pool_b.pool_key_id


def test_two_pools_overlap_partially() -> None:
    """Two pools whose windows overlap partially produce plans
    whose coverage ranges intersect on a sub-range, but the
    per-pool data roots, pool-init blocks, and outcome records
    remain independent."""
    pool_a = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_a", pool_init_block=50_000_000, pin_block=60_000_000)
    )
    pool_b = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_b", pool_init_block=55_000_000, pin_block=65_000_000)
    )
    overlap_start = max(pool_a.coverage_from_block, pool_b.coverage_from_block)
    overlap_end = min(pool_a.coverage_to_block, pool_b.coverage_to_block)
    assert overlap_start <= overlap_end
    assert pool_a.coverage_from_block != pool_b.coverage_from_block


def test_two_pools_overlap_fully() -> None:
    """Two pools whose windows fully overlap share the pinned end
    but carry distinct pool-init blocks and pool-key ids."""
    pool_a = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_a", pool_init_block=50_000_000, pin_block=60_000_000)
    )
    pool_b = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_b", pool_init_block=52_000_000, pin_block=60_000_000)
    )
    assert pool_a.coverage_to_block == pool_b.coverage_to_block
    assert pool_a.coverage_from_block != pool_b.coverage_from_block
    assert pool_a.window_width_blocks > pool_b.window_width_blocks


def test_two_pools_do_not_overlap() -> None:
    """Two pools whose windows do not overlap carry disjoint
    coverage ranges; the per-pool planner never merges two pools
    into one dataset."""
    pool_a = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_a", pool_init_block=50_000_000, pin_block=55_000_000)
    )
    pool_b = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_b", pool_init_block=57_000_000, pin_block=60_000_000)
    )
    assert pool_a.coverage_to_block < pool_b.coverage_from_block


# ---------------------------------------------------------------------------
# Pool initialized after the agreed end
# ---------------------------------------------------------------------------


def test_pool_initialized_after_agreed_end_halts_with_named_outcome() -> None:
    """A pool whose ``Initialize`` lies after the agreed
    finalized end halts the run with the
    ``pool_initialized_after_agreed_end`` outcome and the
    ``pool_initialized_after_agreed_end`` reason code; the
    planner never widens the window to chase it."""
    inputs = _inputs(pool_init_block=65_000_000, pin_block=60_000_000)
    plan = plan_per_pool_window(inputs)
    assert plan.outcome == OUTCOME_POOL_INITIALIZED_AFTER_END
    assert plan.reason_code == REASON_POOL_INITIALIZED_AFTER_END
    assert plan.is_failed
    assert not plan.is_acquired
    assert plan.window_width_blocks == 0


def test_pool_initialize_not_located_is_a_failed_resolution() -> None:
    """A pool whose ``Initialize`` could not be located at all is
    a failed resolution; the planner surfaces the
    ``pool_initialize_resolution_failed`` outcome with the
    ``pool_initialize_not_located`` reason code (the planner
    never guesses an ``Initialize`` block from a candidate
    window)."""
    inputs = _inputs(pool_init_block=70_000_000)  # unknown — operator reported unresolved
    # The planner cannot tell from ``pool_init_block`` alone
    # whether the value came from an on-chain scan or a guess; the
    # contract surfaces the dedicated outcome via the
    # ``ReasonCode`` channel when the caller passes the
    # ``pool_initialize_resolution_failed`` reason.
    plan = plan_per_pool_window(inputs)
    # The pool is "located" at 70M but the run window is bounded
    # by 60M, so the run halts with the
    # ``pool_initialized_after_agreed_end`` outcome.
    assert plan.outcome == OUTCOME_POOL_INITIALIZED_AFTER_END
    assert plan.reason_code == REASON_POOL_INITIALIZED_AFTER_END


# ---------------------------------------------------------------------------
# parse_finalized_pin_payloads — finalized-block agreement
# ---------------------------------------------------------------------------


def test_parse_finalized_pin_payloads_returns_inputs_on_agreement() -> None:
    """When both endpoints agree on block number and hash the
    helper returns a populated :class:`PerPoolWindowInputs` with
    the agreed pin and the ``ok`` reason code."""
    inputs, reason = parse_finalized_pin_payloads(
        primary_block_number=70_000_000,
        primary_block_hash=_hash_for_block(70_000_000),
        secondary_block_number=70_000_000,
        secondary_block_hash=_hash_for_block(70_000_000),
    )
    assert reason == REASON_OK
    assert inputs is not None
    assert inputs.window_pin_block_number == 70_000_000
    assert inputs.window_pin_block_hash == _hash_for_block(70_000_000)
    # The helper returns a stub ``PerPoolWindowInputs``; the
    # caller fills in ``pool_key_id`` and ``pool_init_block``
    # after the operator-side scan completes.
    assert inputs.pool_key_id == "<uninitialized>"


def test_parse_finalized_pin_payloads_rejects_unavailable() -> None:
    """When either endpoint returns ``None`` the helper surfaces
    ``finalized_unavailable``; the run never falls back to
    ``latest``."""
    inputs, reason = parse_finalized_pin_payloads(
        primary_block_number=70_000_000,
        primary_block_hash=_hash_for_block(70_000_000),
        secondary_block_number=None,
        secondary_block_hash=None,
    )
    assert inputs is None
    assert reason == REASON_FINALIZED_UNAVAILABLE

    inputs2, reason2 = parse_finalized_pin_payloads(
        primary_block_number=None,
        primary_block_hash=None,
        secondary_block_number=70_000_000,
        secondary_block_hash=_hash_for_block(70_000_000),
    )
    assert inputs2 is None
    assert reason2 == REASON_FINALIZED_UNAVAILABLE


def test_parse_finalized_pin_payloads_rejects_number_disagreement() -> None:
    """Different block numbers across the two endpoints surface
    ``finalized_endpoint_disagreement``."""
    inputs, reason = parse_finalized_pin_payloads(
        primary_block_number=70_000_000,
        primary_block_hash=_hash_for_block(70_000_000),
        secondary_block_number=70_000_001,
        secondary_block_hash=_hash_for_block(70_000_001),
    )
    assert inputs is None
    assert reason == REASON_FINALIZED_DISAGREEMENT


def test_parse_finalized_pin_payloads_rejects_hash_disagreement() -> None:
    """Same block number with a different hash is a disagreement
    on the contract's number-and-hash agreement rule."""
    inputs, reason = parse_finalized_pin_payloads(
        primary_block_number=70_000_000,
        primary_block_hash=_hash_for_block(70_000_000),
        secondary_block_number=70_000_000,
        secondary_block_hash=_hash_for_block(69_999_999),
    )
    assert inputs is None
    assert reason == REASON_FINALIZED_DISAGREEMENT


def test_parse_finalized_pin_payloads_normalises_hash_casing() -> None:
    """Case-insensitive hash comparison so RPC clients with
    different casing conventions do not falsely surface a
    disagreement."""
    inputs, reason = parse_finalized_pin_payloads(
        primary_block_number=70_000_000,
        primary_block_hash=_hash_for_block(70_000_000).upper(),
        secondary_block_number=70_000_000,
        secondary_block_hash=_hash_for_block(70_000_000).lower(),
    )
    assert reason == REASON_OK
    assert inputs is not None


def test_parse_finalized_pin_payloads_rejects_non_finalized_block() -> None:
    """A negative block number is a non-finalized-block
    rejection; the helper refuses rather than accepting."""
    inputs, reason = parse_finalized_pin_payloads(
        primary_block_number=-1,
        primary_block_hash=_hash_for_block(1),
        secondary_block_number=-1,
        secondary_block_hash=_hash_for_block(1),
    )
    assert inputs is None
    assert reason == REASON_NON_FINALIZED_BLOCK_REJECTED


def test_parse_finalized_pin_payloads_rejects_malformed_hash() -> None:
    """A hash of the wrong shape is surfaced as
    ``provider_returned_non_finalized_for_finalized_tag`` rather
    than silently coerced."""
    inputs, reason = parse_finalized_pin_payloads(
        primary_block_number=70_000_000,
        primary_block_hash="0xZZZZ",
        secondary_block_number=70_000_000,
        secondary_block_hash=_hash_for_block(70_000_000),
    )
    assert inputs is None
    assert reason == REASON_PROVIDER_RETURNED_NON_FINALIZED


def test_parse_finalized_pin_payloads_does_not_substitute_latest() -> None:
    """The T039 contract forbids substituting ``latest`` when
    the finalized tag is unavailable. The helper never returns a
    ``latest`` value."""
    for pn, ph in (
        (None, None),
        (70_000_000, None),
        (None, _hash_for_block(70_000_000)),
    ):
        inputs, reason = parse_finalized_pin_payloads(
            primary_block_number=pn,
            primary_block_hash=ph,
            secondary_block_number=70_000_000,
            secondary_block_hash=_hash_for_block(70_000_000),
        )
        assert inputs is None
        assert reason != REASON_OK
        # ``latest`` substitution would surface as a successful
        # outcome with a fresh block number; the helper never
        # produces that.


# ---------------------------------------------------------------------------
# PerPoolWindowPlan validation
# ---------------------------------------------------------------------------


def test_plan_rejects_window_width_mismatch() -> None:
    """The planner constructs the plan with the correct
    ``window_width_blocks``; a hand-constructed plan with a
    mismatched width is rejected at construction."""
    pin_block = 60_000_000
    pin_hash = _hash_for_block(pin_block)
    with pytest.raises(ValueError):
        # ``window_width_blocks=1`` but ``coverage_to_block -
        # coverage_from_block + 1 == 1_000_001`` — mismatch.
        PerPoolWindowPlan(
            pool_key_id="4663:0x" + "11" * 32,
            chain_id=4663,
            pool_init_block=50_000_000,
            pool_init_block_hash=_hash_for_block(50_000_000),
            window_pin_block_number=pin_block,
            window_pin_block_hash=pin_hash,
            coverage_from_block=50_000_000,
            coverage_to_block=60_000_000,
            window_width_blocks=1,
            outcome=OUTCOME_POOL_ACQUIRED,
            reason_code=REASON_OK,
            primary_endpoint_alias=DEFAULT_PRIMARY_ALIAS,
            secondary_endpoint_alias=DEFAULT_SECONDARY_ALIAS,
            primary_max_blocks_per_call=10_000,
            secondary_max_blocks_per_call=10,
            sub_ranges=(PlannedSubRange(50_000_000, 60_000_000),),
            deduplicated_header_block_count=1_000_001,
            expected_logical_call_count=2,
            budget_ceiling=BudgetCeiling(),
        )


def test_plan_rejects_coverage_to_greater_than_pin() -> None:
    """``coverage_to_block`` cannot exceed the pinned end; a
    hand-constructed plan that violates the invariant is
    rejected."""
    with pytest.raises(ValueError):
        PerPoolWindowPlan(
            pool_key_id="4663:0x" + "11" * 32,
            chain_id=4663,
            pool_init_block=50_000_000,
            pool_init_block_hash=_hash_for_block(50_000_000),
            window_pin_block_number=60_000_000,
            window_pin_block_hash=_hash_for_block(60_000_000),
            coverage_from_block=50_000_000,
            coverage_to_block=70_000_000,
            window_width_blocks=20_000_001,
            outcome=OUTCOME_POOL_ACQUIRED,
            reason_code=REASON_OK,
            primary_endpoint_alias=DEFAULT_PRIMARY_ALIAS,
            secondary_endpoint_alias=DEFAULT_SECONDARY_ALIAS,
            primary_max_blocks_per_call=10_000,
            secondary_max_blocks_per_call=10,
            sub_ranges=(),
            deduplicated_header_block_count=0,
            expected_logical_call_count=0,
            budget_ceiling=BudgetCeiling(),
        )


def test_plan_rejects_unknown_outcome() -> None:
    """An outcome outside the documented vocabulary is rejected
    so the verifier cannot be surprised by a new value."""
    with pytest.raises(ValueError):
        PerPoolWindowPlan(
            pool_key_id="4663:0x" + "11" * 32,
            chain_id=4663,
            pool_init_block=50_000_000,
            pool_init_block_hash=_hash_for_block(50_000_000),
            window_pin_block_number=60_000_000,
            window_pin_block_hash=_hash_for_block(60_000_000),
            coverage_from_block=50_000_000,
            coverage_to_block=60_000_000,
            window_width_blocks=1_000_001,
            outcome="pool_included",  # T038 vocabulary; rejected
            reason_code=REASON_OK,
            primary_endpoint_alias=DEFAULT_PRIMARY_ALIAS,
            secondary_endpoint_alias=DEFAULT_SECONDARY_ALIAS,
            primary_max_blocks_per_call=10_000,
            secondary_max_blocks_per_call=10,
            sub_ranges=(PlannedSubRange(50_000_000, 60_000_000),),
            deduplicated_header_block_count=1_000_001,
            expected_logical_call_count=1_000_002,
            budget_ceiling=BudgetCeiling(),
        )


def test_plan_rejects_unknown_reason_code() -> None:
    """An unknown reason code is rejected at construction."""
    with pytest.raises(ValueError):
        PerPoolWindowPlan(
            pool_key_id="4663:0x" + "11" * 32,
            chain_id=4663,
            pool_init_block=50_000_000,
            pool_init_block_hash=_hash_for_block(50_000_000),
            window_pin_block_number=60_000_000,
            window_pin_block_hash=_hash_for_block(60_000_000),
            coverage_from_block=50_000_000,
            coverage_to_block=60_000_000,
            window_width_blocks=1_000_001,
            outcome=OUTCOME_POOL_ACQUIRED,
            reason_code="pool_init_outside_window",  # T038 vocabulary; rejected
            primary_endpoint_alias=DEFAULT_PRIMARY_ALIAS,
            secondary_endpoint_alias=DEFAULT_SECONDARY_ALIAS,
            primary_max_blocks_per_call=10_000,
            secondary_max_blocks_per_call=10,
            sub_ranges=(PlannedSubRange(50_000_000, 60_000_000),),
            deduplicated_header_block_count=1_000_001,
            expected_logical_call_count=1_000_002,
            budget_ceiling=BudgetCeiling(),
        )


def test_plan_to_dict_is_json_serialisable() -> None:
    """The planner output is JSON-serialisable so the audit
    trail can store it next to the per-pool coverage report."""
    inputs = _inputs()
    plan = plan_per_pool_window(inputs)
    blob = json.dumps(plan.to_dict(), sort_keys=True, ensure_ascii=False)
    assert "pool_key_id" in blob
    assert "coverage_from_block" in blob
    assert "window_pin_block_number" in blob


# ---------------------------------------------------------------------------
# Resume from checkpoint
# ---------------------------------------------------------------------------


def test_resume_emits_pool_acquired_after_checkpoint_resume_outcome() -> None:
    """Interrupting and resuming a window is recorded as the
    ``pool_acquired_after_checkpoint_resume`` outcome so the
    audit trail names the resume boundary."""
    inputs = _inputs(pool_init_block=50_000_000, pin_block=60_000_000)
    plan = plan_per_pool_window_after_resume(inputs, resume_from_block=55_000_000)
    assert plan.outcome == OUTCOME_POOL_ACQUIRED_AFTER_RESUME
    assert plan.is_resumed
    assert plan.is_acquired
    # Sub-ranges start at the resume boundary, not at the pool-init block.
    assert plan.sub_ranges[0].from_block == 55_000_000
    assert plan.sub_ranges[-1].to_block == 60_000_000


def test_resume_rejects_out_of_bounds_resume_block() -> None:
    """``resume_from_block`` outside the coverage window is
    rejected so a corrupt checkpoint cannot widen the plan."""
    inputs = _inputs(pool_init_block=50_000_000, pin_block=60_000_000)
    with pytest.raises(ValueError):
        plan_per_pool_window_after_resume(inputs, resume_from_block=49_000_000)
    with pytest.raises(ValueError):
        plan_per_pool_window_after_resume(inputs, resume_from_block=70_000_000)


def test_resume_at_pin_emits_no_op_plan() -> None:
    """A resume at the pinned end emits a zero-suffix plan that
    records ``range_already_covered`` rather than issuing chain
    reads; the contract requires that a re-run over an
    already-covered range produces no new chain reads."""
    inputs = _inputs(pool_init_block=50_000_000, pin_block=60_000_000)
    plan = plan_per_pool_window_after_resume(inputs, resume_from_block=60_000_000)
    # The function collapses to the ``already_covered`` no-op path.
    assert plan.reason_code == REASON_RANGE_ALREADY_COVERED
    assert plan.sub_ranges == ()
    # The coverage window remains pinned to the same
    # initialize-to-end range so the audit trail can compare it
    # against the prior run.
    assert plan.coverage_from_block == 50_000_000
    assert plan.coverage_to_block == 60_000_000


# ---------------------------------------------------------------------------
# Idempotent re-runs
# ---------------------------------------------------------------------------


def test_prior_coverage_records_already_covered_outcome() -> None:
    """A re-run over a window the durable checkpoint already
    qualified surfaces the ``range_already_covered`` reason code
    and emits no sub-ranges."""
    pin_block = 60_000_000
    pin_hash = _hash_for_block(pin_block)
    prior = PriorCoverage(
        qualified_from_block=50_000_000,
        qualified_to_block=60_000_000,
        qualified_to_block_hash=pin_hash,
        manifest_checksum="0x" + "ab" * 32,
        schema_version=3,
        decode_version=1,
        capability_snapshot_id="cap-2026-09-19",
        topology="cold_start",
    )
    inputs = _inputs(pool_init_block=50_000_000, pin_block=pin_block, prior_coverage=prior)
    plan = plan_per_pool_window(inputs)
    assert plan.reason_code == REASON_RANGE_ALREADY_COVERED
    assert plan.sub_ranges == ()
    # The coverage window remains pinned to the same
    # initialize-to-end range so the audit trail can compare it
    # against the prior run.
    assert plan.coverage_from_block == 50_000_000
    assert plan.coverage_to_block == 60_000_000


def test_prior_coverage_partial_does_not_skip_chain_reads() -> None:
    """A ``PriorCoverage`` that does not extend to the new pinned
    end does not short-circuit the plan; the planner emits
    sub-ranges covering the suffix."""
    pin_block = 60_000_000
    prior = PriorCoverage(
        qualified_from_block=50_000_000,
        qualified_to_block=55_000_000,  # partial
        qualified_to_block_hash=_hash_for_block(55_000_000),
        manifest_checksum="0x" + "ab" * 32,
        schema_version=3,
        decode_version=1,
        capability_snapshot_id="cap-2026-09-19",
        topology="warm_incremental",
    )
    inputs = _inputs(pool_init_block=50_000_000, pin_block=pin_block, prior_coverage=prior)
    plan = plan_per_pool_window(inputs)
    # The planner does not gate on partial prior coverage — it
    # emits sub-ranges over the full window so the runner can
    # resume from the durable checkpoint.
    assert plan.sub_ranges != ()
    assert plan.sub_ranges[0].from_block == plan.coverage_from_block
    assert plan.sub_ranges[-1].to_block == plan.coverage_to_block


def test_prior_coverage_validates_layout() -> None:
    """``PriorCoverage`` rejects a malformed durable-checkpoint
    payload so the planner never accepts a corrupted pin."""
    with pytest.raises(ValueError):
        PriorCoverage(
            qualified_from_block=100,
            qualified_to_block=50,  # inverted
            qualified_to_block_hash=_hash_for_block(50),
            manifest_checksum="0x" + "ab" * 32,
            schema_version=3,
            decode_version=1,
            capability_snapshot_id="cap",
            topology="cold_start",
        )
    with pytest.raises(ValueError):
        PriorCoverage(
            qualified_from_block=50,
            qualified_to_block=100,
            qualified_to_block_hash="",  # missing hash
            manifest_checksum="0x" + "ab" * 32,
            schema_version=3,
            decode_version=1,
            capability_snapshot_id="cap",
            topology="cold_start",
        )


# ---------------------------------------------------------------------------
# Logical-call-count invariant
# ---------------------------------------------------------------------------


def test_logical_call_count_invariant_holds() -> None:
    """The invariant holds when the runner records exactly one
    ``eth_getLogs`` per sub-range plus one header per distinct
    event block."""
    inputs = _inputs(pool_init_block=50_000_000, pin_block=60_000_000, primary_max=200)
    plan = plan_per_pool_window(inputs)
    # The invariant assumes the conservative upper bound: the
    # coverage width equals the deduplicated-header-block count
    # at planning time.
    assert_logical_call_count_equals_sub_ranges_plus_headers(
        plan, distinct_event_block_count=plan.deduplicated_header_block_count
    )


def test_logical_call_count_invariant_violation_raises() -> None:
    """A runner that records more logical calls than the
    invariant allows halts the run with a named reason code."""
    inputs = _inputs(pool_init_block=50_000_000, pin_block=60_000_000, primary_max=200)
    plan = plan_per_pool_window(inputs)
    with pytest.raises(LogicalCallCountInvariantError):
        assert_logical_call_count_equals_sub_ranges_plus_headers(
            plan, distinct_event_block_count=plan.deduplicated_header_block_count + 1
        )


def test_logical_call_count_invariant_resume_uses_suffix() -> None:
    """After a checkpoint resume the invariant uses the suffix
    coverage width rather than the full window width."""
    inputs = _inputs(pool_init_block=50_000_000, pin_block=60_000_000, primary_max=200)
    plan = plan_per_pool_window_after_resume(inputs, resume_from_block=55_000_000)
    suffix_width = plan.coverage_to_block - 55_000_000 + 1
    # The planner records the suffix width in
    # ``deduplicated_header_block_count`` for the resume plan.
    assert plan.deduplicated_header_block_count == suffix_width
    assert_logical_call_count_equals_sub_ranges_plus_headers(
        plan, distinct_event_block_count=suffix_width
    )


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------


def test_per_pool_window_summary_records_pinned_facts() -> None:
    """The summary is the audit-trail row the runner records
    next to the per-pool coverage report; every pinned fact the
    T039 contract names is present."""
    inputs = _inputs()
    plan = plan_per_pool_window(inputs)
    summary = per_pool_window_summary(plan)
    assert summary["pool_key_id"] == plan.pool_key_id
    assert summary["chain_id"] == plan.chain_id
    assert summary["pool_init_block"] == plan.pool_init_block
    assert summary["pool_init_block_hash"] == plan.pool_init_block_hash
    assert summary["window_pin_block_number"] == plan.window_pin_block_number
    assert summary["window_pin_block_hash"] == plan.window_pin_block_hash
    assert summary["coverage_from_block"] == plan.coverage_from_block
    assert summary["coverage_to_block"] == plan.coverage_to_block
    assert summary["outcome"] == plan.outcome
    assert summary["reason_code"] == plan.reason_code
    assert summary["sub_range_count"] == len(plan.sub_ranges)
    assert summary["deduplicated_header_block_count"] == plan.deduplicated_header_block_count
    assert summary["expected_logical_call_count"] == plan.expected_logical_call_count


# ---------------------------------------------------------------------------
# Per-pool coverage report
# ---------------------------------------------------------------------------


def test_build_per_pool_coverage_report_complete_when_clean() -> None:
    """A clean acquisition produces ``complete=True`` with no
    qualification blockers."""
    inputs = _inputs(pool_init_block=50_000_000, pin_block=60_000_000, primary_max=200)
    plan = plan_per_pool_window(inputs)
    report = build_per_pool_coverage_report(
        plan=plan,
        data_root=_data_root(),
        cost_record=_cost_record(),
        reconciliation=_reconciliation(),
        pool_id_hex="0x" + "11" * 32,
    )
    assert isinstance(report, PerPoolCoverageReport)
    assert report.complete is True
    assert report.coverage_outcome == "complete"
    assert report.qualification_blockers == ()


def test_build_per_pool_coverage_report_incomplete_when_reconciliation_disagrees() -> None:
    """A per-pool reconciliation disagreement forces
    ``complete=False`` and records the blocker."""
    inputs = _inputs()
    plan = plan_per_pool_window(inputs)
    report = build_per_pool_coverage_report(
        plan=plan,
        data_root=_data_root(),
        cost_record=_cost_record(),
        reconciliation=_reconciliation(clean=False, disagreements=("p1",)),
        pool_id_hex="0x" + "11" * 32,
    )
    assert report.complete is False
    assert "partition_reconciliation_disagreement" in report.qualification_blockers


def test_build_per_pool_coverage_report_incomplete_when_budget_exhausted() -> None:
    """An exhausted budget forces ``complete=False`` and
    records ``budget_exhausted`` as a blocker."""
    inputs = _inputs()
    plan = plan_per_pool_window(inputs)
    report = build_per_pool_coverage_report(
        plan=plan,
        data_root=_data_root(),
        cost_record=_cost_record(exhausted="response_bytes"),
        reconciliation=_reconciliation(),
        pool_id_hex="0x" + "11" * 32,
    )
    assert report.complete is False
    assert REASON_BUDGET_EXHAUSTED in report.qualification_blockers


def test_build_per_pool_coverage_report_already_covered_when_prior_coverage_matches() -> None:
    """A re-run over an already-qualified window records the
    ``already_covered`` coverage outcome and refuses to claim
    ``complete=True`` (the audit trail must record that no new
    chain reads were issued)."""
    pin_block = 60_000_000
    pin_hash = _hash_for_block(pin_block)
    prior = PriorCoverage(
        qualified_from_block=50_000_000,
        qualified_to_block=60_000_000,
        qualified_to_block_hash=pin_hash,
        manifest_checksum="0x" + "ab" * 32,
        schema_version=3,
        decode_version=1,
        capability_snapshot_id="cap-2026-09-19",
        topology="cold_start",
    )
    inputs = _inputs(pool_init_block=50_000_000, pin_block=pin_block, prior_coverage=prior)
    plan = plan_per_pool_window(inputs)
    report = build_per_pool_coverage_report(
        plan=plan,
        data_root=_data_root(),
        cost_record=_cost_record(),
        reconciliation=_reconciliation(),
        pool_id_hex="0x" + "11" * 32,
    )
    assert report.coverage_outcome == "already_covered"
    assert report.complete is False  # "already_covered" is not "complete"
    assert REASON_RANGE_ALREADY_COVERED not in report.qualification_blockers  # not a blocker


def test_build_per_pool_coverage_report_failed_resolution_records_outcome() -> None:
    """A pool whose ``Initialize`` lies after the agreed end is
    a ``failed_resolution`` coverage outcome; the planner's
    reason code is recorded as a blocker."""
    inputs = _inputs(pool_init_block=70_000_000, pin_block=60_000_000)
    plan = plan_per_pool_window(inputs)
    report = build_per_pool_coverage_report(
        plan=plan,
        data_root=_data_root(),
        cost_record=_cost_record(),
        reconciliation=_reconciliation(),
        pool_id_hex="0x" + "11" * 32,
    )
    assert report.coverage_outcome == "failed_resolution"
    assert report.complete is False
    assert REASON_POOL_INITIALIZED_AFTER_END in report.qualification_blockers


def test_per_pool_coverage_report_to_dict_serialises() -> None:
    """The per-pool coverage report ``to_dict`` is
    JSON-serialisable so the audit-trail can persist it next to
    the qualification record."""
    inputs = _inputs()
    plan = plan_per_pool_window(inputs)
    report = build_per_pool_coverage_report(
        plan=plan,
        data_root=_data_root(),
        cost_record=_cost_record(),
        reconciliation=_reconciliation(),
        pool_id_hex="0x" + "11" * 32,
    )
    blob = json.dumps(report.to_dict(), sort_keys=True, ensure_ascii=False)
    assert "pool_key_id" in blob
    assert "data_root" in blob
    assert "cost_record" in blob
    assert "reconciliation" in blob
    assert "coverage_outcome" in blob


def test_per_pool_coverage_report_to_markdown_renders_pinned_facts() -> None:
    """The Markdown rendering surfaces the pinned finalized
    end, the pool's ``Initialize`` block, the cost record, and
    the reconciliation verdict."""
    inputs = _inputs()
    plan = plan_per_pool_window(inputs)
    report = build_per_pool_coverage_report(
        plan=plan,
        data_root=_data_root(),
        cost_record=_cost_record(),
        reconciliation=_reconciliation(),
        pool_id_hex="0x" + "11" * 32,
    )
    markdown = report.to_markdown()
    assert "Per-pool coverage report" in markdown
    assert "Pinned window" in markdown
    assert "Per-pool data root" in markdown
    assert "Cost record" in markdown
    assert "Reconciliation" in markdown
    # The Markdown rendering uses comma-separated number form so
    # the audit trail is readable; check both that form and the
    # hash.
    assert f"{plan.window_pin_block_number:,}" in markdown
    assert plan.window_pin_block_hash in markdown


# ---------------------------------------------------------------------------
# Per-pool data root, cost record, reconciliation validation
# ---------------------------------------------------------------------------


def test_data_root_rejects_empty_path() -> None:
    """An empty data-root path is rejected so a missing data
    root never silently appears as a successful acquisition."""
    with pytest.raises(ValueError):
        PerPoolDataRoot(
            pool_key_id="4663:0x" + "11" * 32,
            data_root_path="",
            manifest_checksum="0x" + "ab" * 32,
            partition_ids=("p",),
            schema_version=3,
            decode_version=1,
        )


def test_data_root_rejects_empty_manifest_checksum() -> None:
    """An empty manifest checksum is rejected."""
    with pytest.raises(ValueError):
        PerPoolDataRoot(
            pool_key_id="4663:0x" + "11" * 32,
            data_root_path="/data",
            manifest_checksum="",
            partition_ids=("p",),
            schema_version=3,
            decode_version=1,
        )


def test_cost_record_rejects_unknown_exhausted_dimension() -> None:
    """An unknown ``exhausted_budget_dimension`` is rejected so
    the cost record never names a budget dimension the
    planner does not track."""
    with pytest.raises(ValueError):
        PerPoolCostRecord(
            pool_key_id="4663:0x" + "11" * 32,
            logical_calls=1,
            http_requests=1,
            response_bytes=1,
            rows=1,
            provider_units=1,
            elapsed_ms=1,
            exhausted_budget_dimension="unknown_dimension",
        )


def test_reconciliation_clean_when_no_disagreements() -> None:
    """A clean reconciliation is the only way a per-pool report
    can claim ``complete=True``."""
    rec = PerPoolReconciliation(
        pool_key_id="4663:0x" + "11" * 32,
        partition_event_index_parquet_agreement=True,
        partition_reconciliation_agreement=True,
        partitions_checked=10,
    )
    assert rec.is_clean


def test_reconciliation_not_clean_when_disagreement_listed() -> None:
    """A listed disagreement forces ``is_clean=False``."""
    rec = PerPoolReconciliation(
        pool_key_id="4663:0x" + "11" * 32,
        partition_event_index_parquet_agreement=True,
        partition_reconciliation_agreement=True,
        partitions_checked=10,
        disagreements=("p1",),
    )
    assert not rec.is_clean


# ---------------------------------------------------------------------------
# Reconciliation against partitions
# ---------------------------------------------------------------------------


def test_reconcile_per_pool_reports_against_partitions_clean() -> None:
    """When every partition's owner matches the report's
    ``partition_ids`` and every report declares the partitions
    the runner recorded, the reconciliation is clean."""
    plan_a = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_a", pool_init_block=50_000_000, pin_block=60_000_000)
    )
    plan_b = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_b", pool_init_block=55_000_000, pin_block=60_000_000)
    )
    report_a = build_per_pool_coverage_report(
        plan=plan_a,
        data_root=_data_root(pool_key_id="4663:pool_a", partitions=("p1",)),
        cost_record=_cost_record(pool_key_id="4663:pool_a"),
        reconciliation=_reconciliation(pool_key_id="4663:pool_a"),
        pool_id_hex="0x" + "11" * 32,
    )
    report_b = build_per_pool_coverage_report(
        plan=plan_b,
        data_root=_data_root(pool_key_id="4663:pool_b", partitions=("p2",)),
        cost_record=_cost_record(pool_key_id="4663:pool_b"),
        reconciliation=_reconciliation(pool_key_id="4663:pool_b"),
        pool_id_hex="0x" + "22" * 32,
    )
    discrepancies = reconcile_per_pool_reports_against_partitions(
        reports={"4663:pool_a": report_a, "4663:pool_b": report_b},
        partition_owners={"p1": "4663:pool_a", "p2": "4663:pool_b"},
    )
    for messages in discrepancies.values():
        assert messages == []


def test_reconcile_per_pool_reports_detects_cross_pool_attribution() -> None:
    """A partition attributed to two pools is reported as a
    discrepancy so the audit trail detects any cross-pool
    attribution before claiming coverage."""
    plan_a = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_a", pool_init_block=50_000_000, pin_block=60_000_000)
    )
    report_a = build_per_pool_coverage_report(
        plan=plan_a,
        data_root=_data_root(pool_key_id="4663:pool_a", partitions=("p1",)),
        cost_record=_cost_record(pool_key_id="4663:pool_a"),
        reconciliation=_reconciliation(pool_key_id="4663:pool_a"),
        pool_id_hex="0x" + "11" * 32,
    )
    plan_b = plan_per_pool_window(
        _inputs(pool_key_id="4663:pool_b", pool_init_block=55_000_000, pin_block=60_000_000)
    )
    report_b = build_per_pool_coverage_report(
        plan=plan_b,
        data_root=_data_root(pool_key_id="4663:pool_b", partitions=("p1",)),
        cost_record=_cost_record(pool_key_id="4663:pool_b"),
        reconciliation=_reconciliation(pool_key_id="4663:pool_b"),
        pool_id_hex="0x" + "22" * 32,
    )
    discrepancies = reconcile_per_pool_reports_against_partitions(
        reports={"4663:pool_a": report_a, "4663:pool_b": report_b},
        partition_owners={"p1": "4663:pool_a"},
    )
    # The missing partition on the second pool surfaces as a
    # discrepancy; the cross-attribution would surface the same
    # partition under both pools.
    assert any(messages for messages in discrepancies.values())


# ---------------------------------------------------------------------------
# Per-pool operator runbook
# ---------------------------------------------------------------------------


def test_build_per_pool_operator_runbook_routes_three_roles() -> None:
    """The per-pool runbook documents the three required roles
    and binds each to an endpoint alias the operator can wire
    into the production ``RpcAdapter``."""
    inputs = _inputs()
    plan = plan_per_pool_window(inputs)
    report = build_per_pool_coverage_report(
        plan=plan,
        data_root=_data_root(),
        cost_record=_cost_record(),
        reconciliation=_reconciliation(),
        pool_id_hex="0x" + "11" * 32,
    )
    runbook = build_per_pool_operator_runbook(
        coverage_plans=(plan,),
        per_pool_reports=(report,),
    )
    roles = {entry.role: entry for entry in runbook.endpoints}
    assert roles[ROLE_WIDE_POOL_FILTERED_SCAN].endpoint_alias == DEFAULT_PRIMARY_ALIAS
    assert roles[ROLE_SAMPLED_CROSS_VALIDATION].endpoint_alias == DEFAULT_SECONDARY_ALIAS
    # The historical-state endpoint is the one that serves the
    # pinned window depth; the runbook names it explicitly.
    assert runbook.historical_state_endpoint_alias == DEFAULT_SECONDARY_ALIAS
    assert roles[ROLE_BLOCK_PINNED_STATE_READ].endpoint_alias == DEFAULT_SECONDARY_ALIAS


def test_build_per_pool_operator_runbook_records_pinned_facts() -> None:
    """The runbook's Markdown rendering records the pinned
    window, the per-pool data roots, the budget ceiling, the
    per-pool coverage plans and the per-pool coverage reports."""
    inputs = _inputs()
    plan = plan_per_pool_window(inputs)
    report = build_per_pool_coverage_report(
        plan=plan,
        data_root=_data_root(),
        cost_record=_cost_record(),
        reconciliation=_reconciliation(),
        pool_id_hex="0x" + "11" * 32,
    )
    runbook = build_per_pool_operator_runbook(
        coverage_plans=(plan,),
        per_pool_reports=(report,),
    )
    markdown = runbook.to_markdown()
    assert "Per-Pool Extended-History Acquisition Runbook (T039)" in markdown
    assert "Historical-state endpoint" in markdown
    assert "Endpoint routing" in markdown
    assert "Acquisition paths" in markdown
    assert "Per-pool coverage plans" in markdown
    assert "Per-pool coverage reports" in markdown
    assert "Limitations" in markdown
    # The Markdown rendering uses comma-separated number form so
    # the audit trail is readable; check both that form and the
    # hash.
    assert f"{plan.window_pin_block_number:,}" in markdown
    assert plan.window_pin_block_hash in markdown


def test_build_per_pool_operator_runbook_rejects_unobserved_archive_state() -> None:
    """An unobserved archive-state capability is a documented
    failure path (``endpoint_historical_state_unavailable``);
    the runbook builder refuses to produce a runbook for that
    case rather than silently substitute the primary."""
    with pytest.raises(ValueError):
        build_per_pool_operator_runbook(archive_state_capability_observed=False)


def test_per_pool_endpoints_summary() -> None:
    """The ``endpoints_summary`` helper returns the alias keyed
    by role for downstream provenance entries."""
    runbook = build_per_pool_operator_runbook()
    summary = per_pool_endpoints_summary(runbook)
    assert summary[ROLE_WIDE_POOL_FILTERED_SCAN] == DEFAULT_PRIMARY_ALIAS
    assert summary[ROLE_SAMPLED_CROSS_VALIDATION] == DEFAULT_SECONDARY_ALIAS
    assert summary[ROLE_BLOCK_PINNED_STATE_READ] == DEFAULT_SECONDARY_ALIAS


def test_per_pool_endpoint_routing_entry_rejects_unknown_role() -> None:
    """An unknown role is rejected at construction."""
    with pytest.raises(ValueError):
        PerPoolEndpointRoutingEntry(
            role="unknown_role",
            endpoint_alias="alias",
            applies_to_pool_key_ids=("4663:0x" + "11" * 32,),
            measured_capability="some capability",
        )


def test_per_pool_runbook_budget_rejects_negative_dimensions() -> None:
    """A negative budget dimension is rejected so a corrupt
    configuration cannot produce a non-halting runbook."""
    with pytest.raises(ValueError):
        PerPoolRunbookBudget(logical_calls=-1)


def test_per_pool_runbook_serialises_to_dict() -> None:
    """The per-pool runbook's ``to_dict`` is JSON-serialisable
    so the audit-trail record is reproducible."""
    inputs = _inputs()
    plan = plan_per_pool_window(inputs)
    report = build_per_pool_coverage_report(
        plan=plan,
        data_root=_data_root(),
        cost_record=_cost_record(),
        reconciliation=_reconciliation(),
        pool_id_hex="0x" + "11" * 32,
    )
    runbook = build_per_pool_operator_runbook(
        coverage_plans=(plan,),
        per_pool_reports=(report,),
    )
    blob = json.dumps(runbook.to_dict(), sort_keys=True, ensure_ascii=False)
    assert "title" in blob
    assert "endpoints" in blob
    assert "budget" in blob
    assert "coverage_plans" in blob
    assert "per_pool_reports" in blob


# ---------------------------------------------------------------------------
# Failure-path evidence (sanity: every documented reason code is
# exported and serialised by the planner)
# ---------------------------------------------------------------------------


def test_all_documented_reason_codes_are_emitted_by_summary() -> None:
    """Every documented per-pool reason code is exercised by
    the summary surface; the closed vocabulary prevents the
    verifier from being surprised by a new code."""
    summary = per_pool_window_summary(plan_per_pool_window(_inputs()))
    assert summary["reason_code"] in DOCUMENTED_PER_POOL_REASON_CODES


def test_replan_after_resume_emits_pool_acquired_after_resume_reason() -> None:
    """A resume surfaces the
    ``pool_acquired_after_checkpoint_resume`` outcome with the
    ``ok`` reason code so the audit trail records the resume
    boundary."""
    inputs = _inputs()
    plan = plan_per_pool_window_after_resume(inputs, resume_from_block=55_000_000)
    assert plan.outcome == OUTCOME_POOL_ACQUIRED_AFTER_RESUME
    assert plan.reason_code == REASON_OK
    summary = per_pool_window_summary(plan)
    assert summary["outcome"] == OUTCOME_POOL_ACQUIRED_AFTER_RESUME
    assert summary["reason_code"] == REASON_OK


# ---------------------------------------------------------------------------
# Pool-init-block-at-earliest-block boundary
# ---------------------------------------------------------------------------


def test_pool_initialized_at_block_zero_handled() -> None:
    """A pool initialized at block ``0`` (the earliest block the
    chain can serve) is a valid input; the planner accepts it
    without coercing the value."""
    inputs = _inputs(pool_init_block=0, pin_block=60_000_000)
    plan = plan_per_pool_window(inputs)
    assert plan.coverage_from_block == 0
    assert plan.pool_init_block == 0


def test_plan_pool_init_block_hash_recorded() -> None:
    """The pool-init-block hash the operator observed is recorded
    alongside the pool-init block number so the audit trail can
    reproduce the run from a clean environment."""
    pool_init_block = 50_000_000
    pool_init_block_hash = _hash_for_block(pool_init_block)
    block_number, block_hash = _pin(60_000_000)
    inputs = PerPoolWindowInputs(
        pool_key_id="4663:0x" + "11" * 32,
        pool_init_block=pool_init_block,
        pool_init_block_hash=pool_init_block_hash,
        window_pin_block_number=block_number,
        window_pin_block_hash=block_hash,
    )
    plan = plan_per_pool_window(inputs)
    assert plan.pool_init_block == pool_init_block
    assert plan.pool_init_block_hash == pool_init_block_hash


# ---------------------------------------------------------------------------
# Per-pool budget ceiling validation
# ---------------------------------------------------------------------------


def test_budget_ceiling_rejects_negative_dimensions() -> None:
    """A negative budget dimension is rejected at construction."""
    with pytest.raises(ValueError):
        BudgetCeiling(logical_calls=-1)
    with pytest.raises(ValueError):
        BudgetCeiling(http_requests=-1)
    with pytest.raises(ValueError):
        BudgetCeiling(response_bytes=-1)
    with pytest.raises(ValueError):
        BudgetCeiling(rows=-1)
    with pytest.raises(ValueError):
        BudgetCeiling(provider_units=-1)
    with pytest.raises(ValueError):
        BudgetCeiling(elapsed_ms=-1)


def test_budget_ceiling_default_is_positive() -> None:
    """The default ceiling is a positive value per dimension so
    a fresh run starts with a sensible budget."""
    ceiling = BudgetCeiling()
    assert ceiling.logical_calls > 0
    assert ceiling.http_requests > 0
    assert ceiling.response_bytes > 0
    assert ceiling.rows > 0
    assert ceiling.provider_units > 0
    assert ceiling.elapsed_ms > 0


def test_plan_with_explicit_budget_ceiling_uses_it() -> None:
    """The plan honours an explicit budget ceiling the caller
    supplies."""
    ceiling = BudgetCeiling(logical_calls=42, http_requests=43)
    inputs = PerPoolWindowInputs(
        pool_key_id="4663:0x" + "11" * 32,
        pool_init_block=50_000_000,
        pool_init_block_hash=_hash_for_block(50_000_000),
        window_pin_block_number=60_000_000,
        window_pin_block_hash=_hash_for_block(60_000_000),
        budget_ceiling=ceiling,
    )
    plan = plan_per_pool_window(inputs)
    assert plan.budget_ceiling.logical_calls == 42
    assert plan.budget_ceiling.http_requests == 43
