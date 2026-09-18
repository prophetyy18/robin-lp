"""Tests for T042 block-pinned StateView comparison runner.

Coverage:

- **Multi-height match for both pools.** The runner issues the
  recorded per-height read list at every recorded height for each
  pool and validates the exact integer fields
  (``sqrtPriceX96``, ``tick``, ``lpFee``, ``protocolFee``,
  ``liquidity``, ``liquidityGross``, ``liquidityNet``, bitmap word).
  A mismatch is reported with the field-level detail so the audit
  trail can pinpoint the failing read.
- **Blocked check (no latest substitution).** When the endpoint
  cannot serve a pinned read the runner records a blocked check
  with the T034 reason code ``cross_endpoint_sample_missing``;
  the per-pool report never claims ``passes=True`` for a blocked
  check, and the runner never substitutes ``latest``, an unpinned
  height, or another endpoint.
- **Budget exhaustion halts the run.** A run that would exceed
  either the logical-call or compute-unit ceiling stops and records
  the exhaustion snapshot rather than silently reducing coverage.
- **Per-pool validation isolation.** One pool's mismatch must not
  be presented as the other pool's result. The per-pool reports
  carry independent ``passes`` flags keyed by ``pool_alias``.
- **Reconciliation consumer from T037.** The
  :attr:`PoolComparisonInput.partition_reconciliation_consistent`
  flag flows into the per-pool report; an inconsistent pool never
  passes the run.

The tests use synthetic-but-faithful event sequences from
:mod:`_replay_t042_fixtures`; the StateView golden values are
derived deterministically from the events so the comparison is
exact at the integer level.
"""

from __future__ import annotations

import pytest

from _replay_t042_fixtures import (
    CHAIN,
    REFERENCE_POOL_ID,
    REFERENCE_POOL_KEY,
    SECOND_POOL_ID,
    SECOND_POOL_KEY,
    compute_state_view_golden,
    per_height_reads_for_reference_pool,
    per_height_reads_for_second_pool,
    reconstruct_pool_state,
    reference_event_sequence,
    second_event_sequence,
)
from robinhood_lp.protocol import PoolId, PoolKey
from robinhood_lp.replay import (
    BLOCK_REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
    BlockedCheckRecord,
    BudgetExhaustionRecord,
    PoolComparisonInput,
    RecordedComparisonBounds,
    StateComparisonReport,
    StateViewReadSpec,
    decode_get_liquidity,
    decode_get_slot_0,
    decode_get_tick_info,
    decode_get_tick_liquidity,
    make_block_pinned_state_view_call,
    replay,
    replay_state_at_height,
    run_state_comparison,
)
from robinhood_lp.replay.input import ReplayInput
from robinhood_lp.replay.output import ReplayOutput
from robinhood_lp.replay.protocol_fee import pack_protocol_fee
from robinhood_lp.replay.state_comparison import (
    COMPUTE_UNIT_COST_GET_SLOT_0,
    DEFAULT_COMPUTE_UNIT_CEILING_PER_POOL,
    DEFAULT_LOGICAL_CALL_CEILING_PER_POOL,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_replay_output(
    *,
    events: list[object],
    pool_id: PoolId,
    initial_sqrt_price_x96: int = 79_228_162_514_264_337_593_543_950_336,
    initial_tick: int = 0,
) -> tuple[ReplayInput, ReplayOutput]:
    """Build the per-pool ReplayInput and replay output the comparison uses."""
    from pathlib import Path

    pool_key = (
        REFERENCE_POOL_KEY
        if int(pool_id.value) == int(REFERENCE_POOL_ID.value)
        else SECOND_POOL_KEY
    )
    replay_input = ReplayInput(
        chain_id=CHAIN,
        pool_id=pool_id,
        data_root=Path("/tmp/lp-t042-fixture"),
        from_block=0,
        to_block=10_000_000,
        initial_sqrt_price_x96=initial_sqrt_price_x96,
        initial_tick=initial_tick,
    )
    output = replay(replay_input, pool_fee=pool_key.fee, events=events)
    return replay_input, output


def _build_pool_input(
    *,
    pool_alias: str,
    pool_key: PoolKey,
    pool_id: PoolId,
    events: list[object],
    per_height_reads: tuple[tuple[int, tuple[StateViewReadSpec, ...]], ...],
    endpoint_alias: str = "alchemy_free",
    golden_responses: dict[tuple[str, str, bytes, tuple[int, ...]], bytes | None] | None = None,
    partition_reconciliation_consistent: bool = True,
    missing_methods: tuple[str, ...] | None = None,
) -> PoolComparisonInput:
    """Build a :class:`PoolComparisonInput` from the fixture data."""
    replay_input, replay_output = _build_replay_output(events=events, pool_id=pool_id)
    reconstructed = reconstruct_pool_state(pool_id=pool_id, pool_key=pool_key, events=events)
    responses: dict[tuple[str, str, bytes, tuple[int, ...]], bytes | None]
    if golden_responses is None:
        responses = dict(compute_state_view_golden(pool_key, pool_id, events))
    else:
        responses = dict(golden_responses)
    if missing_methods:
        responses = {
            key: value for key, value in responses.items() if key[0] not in missing_methods
        }
    state_call = make_block_pinned_state_view_call(responses)
    return PoolComparisonInput(
        pool_alias=pool_alias,
        chain_id=CHAIN,
        pool_id=pool_id,
        pool_key=pool_key,
        replay_input=replay_input,
        replay_output=replay_output,
        reconstructed_state=reconstructed,
        partition_reconciliation_consistent=partition_reconciliation_consistent,
        per_height_reads=per_height_reads,
        endpoint_alias=endpoint_alias,
        state_call=state_call,
    )


# ---------------------------------------------------------------------------
# Recorded bounds
# ---------------------------------------------------------------------------


def test_recorded_bounds_validation_rejects_zero_heights() -> None:
    """The recorded bounds require at least one height per pool."""
    with pytest.raises(ValueError, match="heights_per_pool"):
        RecordedComparisonBounds(
            heights_per_pool=0,
            per_height_read_list=("getSlot0",),
            logical_call_ceiling_per_pool=DEFAULT_LOGICAL_CALL_CEILING_PER_POOL,
            compute_unit_ceiling_per_pool=DEFAULT_COMPUTE_UNIT_CEILING_PER_POOL,
        )


def test_recorded_bounds_validation_rejects_unknown_method() -> None:
    """The recorded per-height read list refuses unknown methods."""
    with pytest.raises(ValueError, match="per_height_read_list"):
        RecordedComparisonBounds(
            heights_per_pool=2,
            per_height_read_list=("getSlot0", "getNotARealView"),
            logical_call_ceiling_per_pool=DEFAULT_LOGICAL_CALL_CEILING_PER_POOL,
            compute_unit_ceiling_per_pool=DEFAULT_COMPUTE_UNIT_CEILING_PER_POOL,
        )


# ---------------------------------------------------------------------------
# Multi-height match for both pools
# ---------------------------------------------------------------------------


def test_multi_height_match_for_reference_and_secondary_pools() -> None:
    """The reference pool and the second pool both pass the comparison
    over their own recorded per-height read list. The combined report's
    ``overall_passes`` is the AND of the two per-pool verdicts — neither
    pool's result is silently substituted for the other."""
    reference_events = reference_event_sequence()
    second_events = second_event_sequence()
    pool_inputs = (
        _build_pool_input(
            pool_alias="reference",
            pool_key=REFERENCE_POOL_KEY,
            pool_id=REFERENCE_POOL_ID,
            events=reference_events,
            per_height_reads=per_height_reads_for_reference_pool(),
        ),
        _build_pool_input(
            pool_alias="second",
            pool_key=SECOND_POOL_KEY,
            pool_id=SECOND_POOL_ID,
            events=second_events,
            per_height_reads=per_height_reads_for_second_pool(),
        ),
    )
    report = run_state_comparison(pool_inputs)
    assert isinstance(report, StateComparisonReport)
    assert report.overall_passes is True
    assert len(report.pool_reports) == 2
    reference_report = report.pool_report("reference")
    second_report = report.pool_report("second")
    assert reference_report.passes is True
    assert second_report.passes is True
    # Every recorded height is reached and every recorded read
    # produces a FieldComparison (no blocked check / no budget halt).
    assert reference_report.heights_compared == (1_000_003, 1_000_005)
    assert second_report.heights_compared == (1_100_002, 1_100_004)
    assert reference_report.blocked_checks == ()
    assert second_report.blocked_checks == ()
    assert reference_report.budget_exhaustion is None
    assert second_report.budget_exhaustion is None
    # Per-pool validation isolation: the per-pool reports carry
    # distinct heights, distinct pool_aliases, distinct endpoint
    # aliases (here both "alchemy_free" but the PoolId is different
    # so the responses are pool-scoped).
    assert reference_report.pool_id_hex != second_report.pool_id_hex
    # Per-pool exact integer field comparisons.
    for field_comparison in reference_report.field_comparisons:
        assert field_comparison.passed is True
        assert field_comparison.expected_fields == field_comparison.observed_fields
        assert field_comparison.mismatch_detail is None
    for field_comparison in second_report.field_comparisons:
        assert field_comparison.passed is True
        assert field_comparison.expected_fields == field_comparison.observed_fields
        assert field_comparison.mismatch_detail is None
    # Recorded bounds surface on every pool report so the audit trail
    # reproduces the same recorded ceilings.
    assert reference_report.logical_call_ceiling == DEFAULT_LOGICAL_CALL_CEILING_PER_POOL
    assert reference_report.compute_unit_ceiling == DEFAULT_COMPUTE_UNIT_CEILING_PER_POOL
    assert second_report.logical_call_ceiling == DEFAULT_LOGICAL_CALL_CEILING_PER_POOL
    assert second_report.compute_unit_ceiling == DEFAULT_COMPUTE_UNIT_CEILING_PER_POOL


def test_field_comparison_surfaces_exact_integer_mismatch() -> None:
    """A mismatch on ``sqrtPriceX96`` at one block is reported with
    the field-level detail so the audit trail pinpoints the failing
    read."""
    reference_events = reference_event_sequence()
    pool_id_bytes = REFERENCE_POOL_ID.to_bytes()
    golden: dict[tuple[str, str, bytes, tuple[int, ...]], bytes | None] = dict(
        compute_state_view_golden(REFERENCE_POOL_KEY, REFERENCE_POOL_ID, reference_events)
    )
    # Tamper with the getSlot0 response at block 1_000_005 — flip a bit
    # in the low byte of the lpFee slot (offset 95). 28001 (0x6D61)
    # becomes 28000 (0x6D60); both are valid, so the tamper does not
    # trip the decoder's domain check and reaches the comparator.
    block_tag = "0x" + format(1_000_005, "x")
    original = golden[("getSlot0", block_tag, pool_id_bytes, ())]
    assert original is not None
    tampered = bytearray(original)
    tampered[95] ^= 0x01  # flip a bit in lpFee low byte
    golden[("getSlot0", block_tag, pool_id_bytes, ())] = bytes(tampered)

    pool_input = _build_pool_input(
        pool_alias="reference",
        pool_key=REFERENCE_POOL_KEY,
        pool_id=REFERENCE_POOL_ID,
        events=reference_events,
        per_height_reads=per_height_reads_for_reference_pool(),
        golden_responses=golden,
    )
    report = run_state_comparison((pool_input,))
    reference_report = report.pool_report("reference")
    assert reference_report.passes is False
    failed = [c for c in reference_report.field_comparisons if not c.passed]
    assert len(failed) == 1
    assert failed[0].method == "getSlot0"
    assert failed[0].height == 1_000_005
    assert "lpFee" in (failed[0].mismatch_detail or "")


def test_slot_0_protocol_fee_direction_is_decoded() -> None:
    """``getSlot0``'s packed ``protocolFee`` is split into the
    directional halves the replay exposes (token0 high 12 bits,
    token1 low 12 bits) and compared individually."""
    slot0_payload = (
        (79_228_162_514_264_337_593_543_950_336).to_bytes(32, "big")
        + (0).to_bytes(32, "big", signed=True)
        + (28_001).to_bytes(32, "big")
        + pack_protocol_fee(100, 200).to_bytes(32, "big")  # 100 << 12 | 200
    )
    decoded = decode_get_slot_0(slot0_payload, expected_lp_fee=28_001)
    assert decoded["sqrtPriceX96"] == 79_228_162_514_264_337_593_543_950_336
    assert decoded["tick"] == 0
    assert decoded["lpFee"] == 28_001
    assert decoded["protocolFee"] == pack_protocol_fee(100, 200)
    assert decoded["protocolFee_token0"] == 100
    assert decoded["protocolFee_token1"] == 200


# ---------------------------------------------------------------------------
# Blocked check (no latest substitution)
# ---------------------------------------------------------------------------


def test_blocked_check_records_reason_code_and_endpoint_alias() -> None:
    """When the endpoint cannot serve a pinned read the runner
    records a blocked check carrying the T034 reason code, the
    height, the StateView method, and the endpoint alias. The
    per-pool report never claims ``passes=True`` for a blocked
    check; the runner never substitutes ``latest`` or another
    height."""
    reference_events = reference_event_sequence()
    pool_input = _build_pool_input(
        pool_alias="reference",
        pool_key=REFERENCE_POOL_KEY,
        pool_id=REFERENCE_POOL_ID,
        events=reference_events,
        per_height_reads=per_height_reads_for_reference_pool(),
        missing_methods=("getLiquidity",),
    )
    report = run_state_comparison((pool_input,))
    reference_report = report.pool_report("reference")
    assert reference_report.passes is False
    blocked = reference_report.blocked_checks
    assert len(blocked) > 0
    for record in blocked:
        assert isinstance(record, BlockedCheckRecord)
        assert record.reason_code == BLOCK_REASON_CROSS_ENDPOINT_SAMPLE_MISSING
        assert record.endpoint_alias == "alchemy_free"
        assert record.method == "getLiquidity"
        assert record.height in (1_000_003, 1_000_005)
    # The blocked check is reported as coverage the run did not
    # obtain; the FieldComparison list does not silently absorb it.
    blocked_methods = {b.method for b in blocked}
    assert "getLiquidity" in blocked_methods


def test_no_latest_substitution_records_blocked_check_at_pinned_height() -> None:
    """The endpoint alias on the blocked check is the routing alias
    the runner used; the height is the explicit pinned-block height.
    A ``latest`` substitution would change the height and would
    therefore be visible in the audit trail."""
    reference_events = reference_event_sequence()
    pool_input = _build_pool_input(
        pool_alias="reference",
        pool_key=REFERENCE_POOL_KEY,
        pool_id=REFERENCE_POOL_ID,
        events=reference_events,
        per_height_reads=per_height_reads_for_reference_pool(),
        missing_methods=("getSlot0",),
    )
    report = run_state_comparison((pool_input,))
    reference_report = report.pool_report("reference")
    blocked = [b for b in reference_report.blocked_checks if b.method == "getSlot0"]
    assert blocked, "expected at least one blocked check for getSlot0"
    heights = sorted({b.height for b in blocked})
    assert heights == [1_000_003, 1_000_005]


# ---------------------------------------------------------------------------
# Budget exhaustion halts the run
# ---------------------------------------------------------------------------


def test_budget_exhaustion_halts_logical_call_run() -> None:
    """When the recorded read list would exceed the logical-call
    ceiling the runner records the exhaustion and stops without
    silently reducing coverage."""
    reference_events = reference_event_sequence()
    pool_input = _build_pool_input(
        pool_alias="reference",
        pool_key=REFERENCE_POOL_KEY,
        pool_id=REFERENCE_POOL_ID,
        events=reference_events,
        per_height_reads=per_height_reads_for_reference_pool(),
    )
    # Set the logical-call ceiling so the first read exceeds it.
    ceiling = 0
    report = run_state_comparison(
        (pool_input,),
        logical_call_ceiling_per_pool=ceiling,
        compute_unit_ceiling_per_pool=DEFAULT_COMPUTE_UNIT_CEILING_PER_POOL,
    )
    reference_report = report.pool_report("reference")
    assert reference_report.passes is False
    assert isinstance(reference_report.budget_exhaustion, BudgetExhaustionRecord)
    assert reference_report.budget_exhaustion.dimension == "logical_calls"
    assert reference_report.budget_exhaustion.ceiling == ceiling
    assert reference_report.budget_exhaustion.used_at_halt > ceiling
    # The halt height is the first recorded height; no FieldComparison
    # was issued because the runner halts before the read.
    assert reference_report.budget_exhaustion.halt_height == 1_000_003
    assert reference_report.budget_exhaustion.halt_method == "getSlot0"
    assert reference_report.field_comparisons == ()


def test_budget_exhaustion_halts_compute_unit_run() -> None:
    """When the recorded read list would exceed the compute-unit
    ceiling the runner records the exhaustion and stops."""
    reference_events = reference_event_sequence()
    pool_input = _build_pool_input(
        pool_alias="reference",
        pool_key=REFERENCE_POOL_KEY,
        pool_id=REFERENCE_POOL_ID,
        events=reference_events,
        per_height_reads=per_height_reads_for_reference_pool(),
    )
    # Set the compute-unit ceiling below the cost of the first read.
    ceiling = COMPUTE_UNIT_COST_GET_SLOT_0 - 1
    report = run_state_comparison(
        (pool_input,),
        logical_call_ceiling_per_pool=DEFAULT_LOGICAL_CALL_CEILING_PER_POOL,
        compute_unit_ceiling_per_pool=ceiling,
    )
    reference_report = report.pool_report("reference")
    assert reference_report.passes is False
    assert reference_report.budget_exhaustion is not None
    assert reference_report.budget_exhaustion.dimension == "compute_units"
    assert reference_report.budget_exhaustion.ceiling == ceiling


# ---------------------------------------------------------------------------
# Per-pool validation isolation
# ---------------------------------------------------------------------------


def test_per_pool_validation_isolation_one_pool_fails_other_passes() -> None:
    """When the reference pool matches and the second pool mismatches,
    the combined ``overall_passes`` is ``False`` but the reference
    pool's per-pool ``passes`` is ``True``. One pool's agreement is
    never silently used as the other pool's result."""
    reference_events = reference_event_sequence()
    second_events = second_event_sequence()
    golden_second: dict[tuple[str, str, bytes, tuple[int, ...]], bytes | None] = dict(
        compute_state_view_golden(SECOND_POOL_KEY, SECOND_POOL_ID, second_events)
    )
    # Tamper with the second pool's getLiquidity at block 1_100_004.
    pool_id_bytes = SECOND_POOL_ID.to_bytes()
    block_tag = "0x" + format(1_100_004, "x")
    original_second = golden_second[("getLiquidity", block_tag, pool_id_bytes, ())]
    assert original_second is not None
    tampered = bytearray(original_second)
    tampered[31] ^= 0xFF
    golden_second[("getLiquidity", block_tag, pool_id_bytes, ())] = bytes(tampered)

    pool_inputs = (
        _build_pool_input(
            pool_alias="reference",
            pool_key=REFERENCE_POOL_KEY,
            pool_id=REFERENCE_POOL_ID,
            events=reference_events,
            per_height_reads=per_height_reads_for_reference_pool(),
        ),
        _build_pool_input(
            pool_alias="second",
            pool_key=SECOND_POOL_KEY,
            pool_id=SECOND_POOL_ID,
            events=second_events,
            per_height_reads=per_height_reads_for_second_pool(),
            golden_responses=golden_second,
        ),
    )
    report = run_state_comparison(pool_inputs)
    assert report.overall_passes is False
    assert report.pool_report("reference").passes is True
    assert report.pool_report("second").passes is False
    # The reference pool's report is independent: it does not
    # absorb the second pool's mismatched FieldComparison.
    reference_mismatched = [
        c for c in report.pool_report("reference").field_comparisons if not c.passed
    ]
    second_mismatched = [c for c in report.pool_report("second").field_comparisons if not c.passed]
    assert reference_mismatched == []
    assert len(second_mismatched) == 1
    assert second_mismatched[0].method == "getLiquidity"
    assert second_mismatched[0].height == 1_100_004


# ---------------------------------------------------------------------------
# Reconciliation consumer from T037
# ---------------------------------------------------------------------------


def test_reconciliation_inconsistent_pool_never_passes() -> None:
    """A pool whose T037 partition reconciliation is not consistent
    never passes the StateView comparison. T042 consumes the T037
    verdict via ``partition_reconciliation_consistent``; it does not
    re-derive, re-implement or relax the check."""
    reference_events = reference_event_sequence()
    pool_input = _build_pool_input(
        pool_alias="reference",
        pool_key=REFERENCE_POOL_KEY,
        pool_id=REFERENCE_POOL_ID,
        events=reference_events,
        per_height_reads=per_height_reads_for_reference_pool(),
        partition_reconciliation_consistent=False,
    )
    report = run_state_comparison((pool_input,))
    reference_report = report.pool_report("reference")
    assert reference_report.passes is False
    assert reference_report.partition_reconciliation_consistent is False
    # The FieldComparisons still ran (the comparison logic does not
    # short-circuit on the reconciliation flag); the verdict comes
    # from the ``passes`` property's AND of all gates.
    assert all(c.passed for c in reference_report.field_comparisons)


def test_reconciliation_consumer_records_t037_verdict() -> None:
    """The per-pool report carries the T037 reconciliation verdict
    explicitly so the audit trail can attribute an
    ``incomplete=true`` dataset to the underlying T037 report rather
    than to the StateView comparison."""
    reference_events = reference_event_sequence()
    pool_input = _build_pool_input(
        pool_alias="reference",
        pool_key=REFERENCE_POOL_KEY,
        pool_id=REFERENCE_POOL_ID,
        events=reference_events,
        per_height_reads=per_height_reads_for_reference_pool(),
        partition_reconciliation_consistent=True,
    )
    report = run_state_comparison((pool_input,))
    reference_report = report.pool_report("reference")
    assert reference_report.partition_reconciliation_consistent is True
    rendered = reference_report.to_dict()
    assert rendered["partition_reconciliation_consistent"] is True


# ---------------------------------------------------------------------------
# Replay-to-height helper
# ---------------------------------------------------------------------------


def test_replay_state_at_height_returns_zero_state_before_initialize() -> None:
    """``replay_state_at_height`` returns the pre-init zero-state
    mapping when no checkpoint with ``block_number <= height`` is
    present in the replay output."""
    reference_events = reference_event_sequence()
    pool_input = _build_pool_input(
        pool_alias="reference",
        pool_key=REFERENCE_POOL_KEY,
        pool_id=REFERENCE_POOL_ID,
        events=reference_events,
        per_height_reads=per_height_reads_for_reference_pool(),
    )
    state = replay_state_at_height(pool_input.replay_output, height=999_999)
    assert state["sqrtPriceX96"] == 0
    assert state["tick"] == 0
    assert state["liquidity"] == 0
    assert state["protocolFee"] == 0
    assert state["initialized"] == 0


def test_replay_state_at_height_picks_last_checkpoint_at_or_below_height() -> None:
    """``replay_state_at_height`` returns the state of the last
    checkpoint whose ``block_number`` is at or below the requested
    height."""
    reference_events = reference_event_sequence()
    pool_input = _build_pool_input(
        pool_alias="reference",
        pool_key=REFERENCE_POOL_KEY,
        pool_id=REFERENCE_POOL_ID,
        events=reference_events,
        per_height_reads=per_height_reads_for_reference_pool(),
    )
    state = replay_state_at_height(pool_input.replay_output, height=1_000_005)
    assert state["initialized"] == 1
    # After the swap, sqrt_price_x96 / tick / liquidity are the
    # values the swap emitted.
    assert state["sqrtPriceX96"] == 79_228_162_514_264_337_593_543_950_337
    assert state["tick"] == 0
    assert state["liquidity"] == 2_000_000
    assert state["lpFee"] == 28_001


# ---------------------------------------------------------------------------
# StateView response decoders
# ---------------------------------------------------------------------------


def test_decode_get_liquidity_returns_uint128() -> None:
    """``decode_get_liquidity`` parses a 32-byte ``uint128``."""
    payload = (2_000_000).to_bytes(32, "big")
    assert decode_get_liquidity(payload) == {"liquidity": 2_000_000}


def test_decode_get_tick_liquidity_returns_typed_pair() -> None:
    """``decode_get_tick_liquidity`` parses ``uint128`` and ``int128``."""
    payload = (10).to_bytes(32, "big") + (-7).to_bytes(32, "big", signed=True)
    decoded = decode_get_tick_liquidity(payload)
    assert decoded == {"liquidityGross": 10, "liquidityNet": -7}


def test_decode_get_tick_info_returns_four_slots() -> None:
    """``decode_get_tick_info`` parses the four 32-byte slots."""
    payload = (
        (10).to_bytes(32, "big")
        + (-7).to_bytes(32, "big", signed=True)
        + (0).to_bytes(32, "big")
        + (0).to_bytes(32, "big")
    )
    decoded = decode_get_tick_info(payload)
    assert decoded["liquidityGross"] == 10
    assert decoded["liquidityNet"] == -7
    assert decoded["feeGrowthOutside0X128"] == 0
    assert decoded["feeGrowthOutside1X128"] == 0


def test_decode_get_slot_0_rejects_wrong_lp_fee() -> None:
    """``decode_get_slot_0`` raises when ``lpFee`` does not match the
    pool's declared fee (a structural mismatch the audit trail must
    surface, not silently coerce)."""
    slot0_payload = (
        (1).to_bytes(32, "big")
        + (0).to_bytes(32, "big", signed=True)
        + (3_000).to_bytes(32, "big")  # declared fee 3_000
        + (0).to_bytes(32, "big")
    )
    with pytest.raises(ValueError, match="lpFee"):
        decode_get_slot_0(slot0_payload, expected_lp_fee=28_001)


# ---------------------------------------------------------------------------
# Runner API
# ---------------------------------------------------------------------------


def test_run_state_comparison_rejects_duplicate_pool_aliases() -> None:
    """Two inputs sharing the same ``pool_alias`` are rejected;
    per-pool reports must remain addressable by alias."""
    reference_events = reference_event_sequence()
    pool_input = _build_pool_input(
        pool_alias="reference",
        pool_key=REFERENCE_POOL_KEY,
        pool_id=REFERENCE_POOL_ID,
        events=reference_events,
        per_height_reads=per_height_reads_for_reference_pool(),
    )
    second_input = _build_pool_input(
        pool_alias="reference",
        pool_key=SECOND_POOL_KEY,
        pool_id=SECOND_POOL_ID,
        events=second_event_sequence(),
        per_height_reads=per_height_reads_for_second_pool(),
    )
    with pytest.raises(ValueError, match="duplicate pool_alias"):
        run_state_comparison((pool_input, second_input))


def test_run_state_comparison_requires_at_least_one_pool() -> None:
    """The runner refuses an empty input set."""
    with pytest.raises(ValueError, match="at least one PoolComparisonInput"):
        run_state_comparison(())


def test_state_view_read_spec_validates_tick_arg_bounds() -> None:
    """A tick outside ``int24`` is rejected by the read spec
    constructor (T042 acceptance: the recorded per-height read
    list is the acceptance surface)."""
    with pytest.raises(ValueError, match="int24"):
        StateViewReadSpec(method="getTickInfo", extra_args=(1 << 23,))
    with pytest.raises(ValueError, match="int24"):
        StateViewReadSpec(method="getTickInfo", extra_args=(-(1 << 23) - 1,))


def test_state_view_read_spec_validates_word_pos_bounds() -> None:
    """A ``wordPos`` outside ``int16`` is rejected."""
    with pytest.raises(ValueError, match="int16"):
        StateViewReadSpec(method="getTickBitmap", extra_args=(1 << 15,))


def test_pool_comparison_input_rejects_duplicate_heights() -> None:
    """The per-height read list is the recorded acceptance surface;
    duplicate heights are refused so the audit trail never hides a
    height that was attempted twice."""
    reference_events = reference_event_sequence()
    replay_input, replay_output = _build_replay_output(
        events=reference_events, pool_id=REFERENCE_POOL_ID
    )
    reconstructed = reconstruct_pool_state(
        pool_id=REFERENCE_POOL_ID,
        pool_key=REFERENCE_POOL_KEY,
        events=reference_events,
    )
    golden: dict[tuple[str, str, bytes, tuple[int, ...]], bytes | None] = dict(
        compute_state_view_golden(REFERENCE_POOL_KEY, REFERENCE_POOL_ID, reference_events)
    )
    state_call = make_block_pinned_state_view_call(golden)
    with pytest.raises(ValueError, match="duplicate height"):
        PoolComparisonInput(
            pool_alias="reference",
            chain_id=CHAIN,
            pool_id=REFERENCE_POOL_ID,
            pool_key=REFERENCE_POOL_KEY,
            replay_input=replay_input,
            replay_output=replay_output,
            reconstructed_state=reconstructed,
            partition_reconciliation_consistent=True,
            per_height_reads=(
                (1_000_003, (StateViewReadSpec(method="getSlot0"),)),
                (1_000_003, (StateViewReadSpec(method="getLiquidity"),)),
            ),
            endpoint_alias="alchemy_free",
            state_call=state_call,
        )
