"""Tests for T036 reference-dataset qualification.

The tests exercise the qualification pipeline that decides whether
the real-mainnet ingestion run that produced the pinned reference
dataset may carry ``complete=true``. The pipeline's deliverables:

1. **Qualified reference dataset** with the T034 machine report,
   ``complete=true``, endpoint mapping, ``PoolId`` re-derivation
   check, and per-event-type count comparison against the
   2026-09-17 baseline;
2. **Independent fidelity check** that re-acquires sampled windows
   through the secondary endpoint and compares per-``EventKey``
   normalized content hashes;
3. **Block-pinned StateView spot check** at the range end via the
   endpoint that can serve historical state at depth;
4. **Failure-path evidence** for HTTP 429, ``-32000`` logs limit,
   ``-32000`` timeout, failover, and budget exhaustion — each
   producing its documented reason code;
5. **Operator runbook** with explicit endpoint routing.

The tests use synthetic-but-faithful envelopes: every typed V4
record is decoded from the pinned ABI, every ``EventKey`` follows
the T011 identity rule, every ``normalized_content_hash`` matches
the storage schema's SHA-256 derivation, and every per-event-type
count and distinct-block count equals the 2026-09-17 baseline.
The fixtures do **not** contact any real RPC.
"""

from __future__ import annotations

import pytest

# Force the test module to be discoverable from the worktree's
# tests/ directory. pytest picks this up automatically.
from _qualification_t036_fixtures import (
    assert_baseline_consistent,
    build_agreeing_secondary_source,
    build_baseline_consistent_event_stream,
    build_blocked_secondary_source,
    build_primary_envelopes_by_window,
    build_state_call_primary_blocked,
    build_state_call_secondary_serving,
    distinct_block_count_from_records,
)
from robinhood_lp.protocol.ids import (
    PoolId,
)
from robinhood_lp.qualification import (
    BASELINE_DISTINCT_BLOCKS,
    BASELINE_DONATE_COUNT,
    BASELINE_INITIALIZE_COUNT,
    BASELINE_MODIFY_LIQUIDITY_COUNT,
    BASELINE_PROTOCOL_FEE_UPDATED_COUNT,
    BASELINE_SWAP_COUNT,
    BASELINE_TOTAL_EVENTS,
    FAILURE_PATH_KIND_BUDGET_EXHAUSTED,
    FAILURE_PATH_KIND_FAILOVER,
    FAILURE_PATH_KIND_HTTP_429,
    FAILURE_PATH_KIND_LOGS_LIMIT_REJECTION,
    FAILURE_PATH_KIND_RPC_TIMEOUT,
    OPERATOR_RUNBOOK,
    REFERENCE_CHAIN_ID,
    REFERENCE_COVERAGE_FROM_BLOCK,
    REFERENCE_COVERAGE_TO_BLOCK,
    REFERENCE_POOL_ID_HEX,
    REFERENCE_POOL_INIT_BLOCK,
    REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL,
    REFERENCE_TARGET,
    BaselineComparison,
    EndpointRoutingEntry,
    FidelityCheckResult,
    OperatorRunbook,
    PoolIdCheckResult,
    ReferenceQualificationInputs,
    ReferenceQualificationReport,
    ReferenceTarget,
    StateSpotCheckResult,
    StateViewGoldenValues,
    build_failure_path_evidence,
    build_operator_runbook,
    build_reference_pool_key,
    build_reference_qualification_report,
    check_pool_id_derivation,
    compare_against_baseline,
    perform_state_spot_check,
)
from robinhood_lp.qualification.baseline_check import (
    QUALIFIED_EVENT_NAMES,
    make_baseline_consistent_observed_counts,
    observed_counts_from_event_set,
)
from robinhood_lp.qualification.failure_paths import (
    DOCUMENTED_FAILURE_PATH_KINDS,
    FailurePathEvidence,
    all_documented_failure_path_evidence,
    fail_complete_under_failure_paths,
)
from robinhood_lp.qualification.fidelity import (
    FidelityEnvelope,
    FidelityWindow,
    build_fidelity_windows,
    perform_fidelity_check,
)
from robinhood_lp.qualification.state_spot_check import (
    make_block_pinned_state_call,
)

# ---------------------------------------------------------------------------
# Reference target — pinned chain identity
# ---------------------------------------------------------------------------


def test_reference_target_pinned_chain_identity() -> None:
    """The pinned reference target carries the exact chain id,
    PoolKey, PoolId, and inclusive range the T036 contract names."""
    assert REFERENCE_TARGET.chain_id == REFERENCE_CHAIN_ID == 4663
    assert REFERENCE_TARGET.pool_manager_address.to_hex() == (
        "0x8366a39cc670b4001a1121b8f6a443a643e40951"
    )
    assert REFERENCE_TARGET.state_view_address.to_hex() == (
        "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
    )
    assert REFERENCE_TARGET.pool_id.to_hex() == REFERENCE_POOL_ID_HEX
    assert REFERENCE_TARGET.pool_id.to_hex() == (
        "0x6c614c38c65fea492f4cb2b90fd664f924a7b828c7384620662217e2e2df43ed"
    )
    assert REFERENCE_TARGET.coverage_from_block == REFERENCE_COVERAGE_FROM_BLOCK
    assert REFERENCE_TARGET.coverage_to_block == REFERENCE_COVERAGE_TO_BLOCK
    assert REFERENCE_TARGET.coverage_from_block == 54_946_237
    assert REFERENCE_TARGET.coverage_to_block == 55_946_237
    assert REFERENCE_TARGET.pool_init_block == REFERENCE_POOL_INIT_BLOCK
    assert REFERENCE_TARGET.pool_init_block == REFERENCE_COVERAGE_FROM_BLOCK
    # The pool's Initialize event lies inside the pinned range
    # (cold-start coverage rule satisfied by this range).
    assert REFERENCE_TARGET.pool_init_block >= REFERENCE_TARGET.coverage_from_block
    assert REFERENCE_TARGET.pool_init_block <= REFERENCE_TARGET.coverage_to_block


def test_reference_target_baseline_counts_match_contract() -> None:
    """The pinned baseline counts equal the 2026-09-17 measurement
    the T036 contract records."""
    assert BASELINE_TOTAL_EVENTS == 3739
    assert BASELINE_DISTINCT_BLOCKS == 3266
    assert BASELINE_INITIALIZE_COUNT == 1
    assert BASELINE_MODIFY_LIQUIDITY_COUNT == 578
    assert BASELINE_SWAP_COUNT == 3159
    assert BASELINE_PROTOCOL_FEE_UPDATED_COUNT == 1
    assert BASELINE_DONATE_COUNT == 0
    # Sum-check: the per-event-type baseline sums to the total.
    assert (
        BASELINE_INITIALIZE_COUNT
        + BASELINE_MODIFY_LIQUIDITY_COUNT
        + BASELINE_SWAP_COUNT
        + BASELINE_PROTOCOL_FEE_UPDATED_COUNT
        + BASELINE_DONATE_COUNT
    ) == BASELINE_TOTAL_EVENTS
    # ReferenceTarget constructor validates the same sum.
    target = ReferenceTarget()
    assert target.baseline_total_events == BASELINE_TOTAL_EVENTS
    # Secondary endpoint's measured per-call capability is honored.
    assert REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL == 10
    assert target.secondary_max_blocks_per_call == 10


# ---------------------------------------------------------------------------
# PoolId re-derivation check
# ---------------------------------------------------------------------------


def test_pool_id_rederivation_matches_pinned_pool_id() -> None:
    """The pinned PoolId equals ``keccak256(abi.encode(PoolKey))``."""
    pool_key = build_reference_pool_key()
    result = check_pool_id_derivation(pool_key=pool_key)
    assert isinstance(result, PoolIdCheckResult)
    assert result.derived_pool_id.to_hex() == REFERENCE_POOL_ID_HEX
    assert result.pinned_pool_id.to_hex() == REFERENCE_POOL_ID_HEX
    assert result.matches_pinned is True


def test_pool_id_rederivation_default_uses_reference_target() -> None:
    """Calling :func:`check_pool_id_derivation` with no arguments
    uses the pinned reference ``PoolKey`` and ``PoolId``."""
    result = check_pool_id_derivation()
    assert result.matches_pinned is True
    assert result.derived_pool_id == REFERENCE_TARGET.pool_id


def test_pool_id_rederivation_halts_qualification_on_mismatch() -> None:
    """A mismatch halts qualification: the test seam lets us inject
    a wrong pinned ``PoolId`` and the check returns
    ``matches_pinned=False``."""
    wrong_pool_id = PoolId(int.from_bytes(b"\x99" * 32, "big"))
    result = check_pool_id_derivation(pinned_pool_id=wrong_pool_id)
    assert result.matches_pinned is False
    assert result.derived_pool_id.to_hex() == REFERENCE_POOL_ID_HEX
    assert result.pinned_pool_id == wrong_pool_id


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------


def test_baseline_comparison_agrees_when_observed_matches_baseline() -> None:
    """An observation that exactly matches the 2026-09-17 baseline
    produces ``agrees_overall=True``."""
    observed = make_baseline_consistent_observed_counts()
    comparison = compare_against_baseline(
        observed_per_event_type=observed.per_event_type,
        observed_distinct_block_count=observed.distinct_blocks,
    )
    assert isinstance(comparison, BaselineComparison)
    assert comparison.agrees_overall is True
    for event_name in QUALIFIED_EVENT_NAMES:
        row = comparison.per_event_type[event_name]
        assert row.agrees, (
            f"{event_name} disagrees: observed={row.observed} baseline={row.baseline}"
        )
        assert row.delta == 0


def test_baseline_comparison_surfaces_per_event_type_discrepancy() -> None:
    """A per-event-type discrepancy halts qualification."""
    observed = make_baseline_consistent_observed_counts(
        modify_liquidity_count=BASELINE_MODIFY_LIQUIDITY_COUNT + 1
    )
    comparison = compare_against_baseline(
        observed_per_event_type=observed.per_event_type,
        observed_distinct_block_count=observed.distinct_blocks,
    )
    assert comparison.agrees_overall is False
    row = comparison.per_event_type["ModifyLiquidity"]
    assert row.agrees is False
    assert row.delta == 1


def test_baseline_comparison_surfaces_distinct_block_discrepancy() -> None:
    """A distinct-block discrepancy halts qualification with the
    range_coverage_gap reason code."""
    observed = make_baseline_consistent_observed_counts(
        distinct_blocks=BASELINE_DISTINCT_BLOCKS - 1
    )
    comparison = compare_against_baseline(
        observed_per_event_type=observed.per_event_type,
        observed_distinct_block_count=observed.distinct_blocks,
    )
    assert comparison.agrees_overall is False


def test_baseline_comparison_rejects_missing_event_type_key() -> None:
    """An observation dict missing a baseline event-type key is
    rejected rather than silently treated as ``0`` observed."""
    observed = {
        "Initialize": 1,
        "ModifyLiquidity": 578,
        "Swap": 3159,
        # ProtocolFeeUpdated intentionally missing
        "Donate": 0,
    }
    with pytest.raises(ValueError, match="missing keys"):
        compare_against_baseline(
            observed_per_event_type=observed,
            observed_distinct_block_count=BASELINE_DISTINCT_BLOCKS,
        )


def test_baseline_comparison_observed_counts_from_event_stream() -> None:
    """The event-set helper derives the per-event-type counts and
    distinct-block count deterministically from the typed records."""
    records = build_baseline_consistent_event_stream()
    counts = observed_counts_from_event_set(records)
    assert counts.per_event_type["Initialize"] == BASELINE_INITIALIZE_COUNT
    assert counts.per_event_type["ModifyLiquidity"] == BASELINE_MODIFY_LIQUIDITY_COUNT
    assert counts.per_event_type["Swap"] == BASELINE_SWAP_COUNT
    assert counts.per_event_type["ProtocolFeeUpdated"] == BASELINE_PROTOCOL_FEE_UPDATED_COUNT
    assert counts.per_event_type["Donate"] == BASELINE_DONATE_COUNT
    assert counts.distinct_blocks == BASELINE_DISTINCT_BLOCKS


# ---------------------------------------------------------------------------
# Fidelity check
# ---------------------------------------------------------------------------


def test_fidelity_window_construction_respects_secondary_capability() -> None:
    """Each sampled window fits inside the secondary endpoint's
    measured per-call capability (10 blocks per call)."""
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    assert len(windows) == 3
    labels = {w.label for w in windows}
    assert labels == {"start", "middle", "end"}
    for window in windows:
        span = window.to_block - window.from_block + 1
        assert span <= REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL


def test_fidelity_check_agrees_when_secondary_matches_primary() -> None:
    """A secondary source whose envelope matches the primary
    envelope on every ``EventKey`` produces
    ``agrees_overall=True`` and ``blocked_window_count=0``."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    secondary_source = build_agreeing_secondary_source(primary_envelopes)
    result = perform_fidelity_check(
        primary_envelopes_by_window=primary_envelopes,
        secondary_window_source=secondary_source,
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    assert isinstance(result, FidelityCheckResult)
    assert result.agrees_overall is True
    assert result.blocked_window_count == 0
    assert result.sample_size == 3
    assert set(result.sample_coverage) == {"start", "middle", "end"}
    # At least one result-bearing window must exist (the dataset
    # contains events).
    assert result.result_bearing_window_count >= 1


def test_fidelity_check_halts_on_blocked_window() -> None:
    """A window the secondary endpoint cannot serve is reported as
    a blocked check with the ``cross_endpoint_sample_missing``
    reason code; it is never silently skipped and never replaced
    by a primary re-read."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    end_window = next(w for w in windows if w.label == "end")
    secondary_source = build_blocked_secondary_source(end_window)
    result = perform_fidelity_check(
        primary_envelopes_by_window=primary_envelopes,
        secondary_window_source=secondary_source,
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    assert result.agrees_overall is False
    assert result.blocked_window_count == 1
    blocked_sample = next(s for s in result.windows if s.blocked)
    assert blocked_sample.window == end_window
    assert blocked_sample.secondary_envelope.can_serve is False
    assert blocked_sample.secondary_envelope.block_reason is not None


def test_fidelity_check_halts_on_normalized_hash_disagreement() -> None:
    """A per-``EventKey`` normalized hash disagreement halts
    qualification with the ``cross_endpoint_sample_disagree``
    reason code; the disagreeing ``EventKey`` is recorded in the
    audit trail."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    # Pick a sampled window that actually carries primary-event keys;
    # the synthetic stream's "middle" window may be empty when the
    # spread skips that exact block range. Use the start window as
    # the disagreement target.
    start_window = next(w for w in windows if w.label == "start")
    primary = primary_envelopes[start_window]
    assert primary.event_keys, (
        "synthetic stream must produce at least one EventKey in the "
        "start window for the disagreement test"
    )

    def _disagreeing_source(window: FidelityWindow) -> FidelityEnvelope:
        if window != start_window:
            return FidelityEnvelope(
                endpoint_alias="alchemy_free",
                event_keys=primary_envelopes[window].event_keys,
                normalized_hashes=dict(primary_envelopes[window].normalized_hashes),
                raw_payload={},
            )
        # Flip every byte in every normalized hash so every
        # EventKey the primary reported disagrees with the
        # secondary.
        bad_hashes = {
            key: bytes((b ^ 0xFF) & 0xFF for b in hash_bytes)
            for key, hash_bytes in primary.normalized_hashes.items()
        }
        return FidelityEnvelope(
            endpoint_alias="alchemy_free",
            event_keys=primary.event_keys,
            normalized_hashes=bad_hashes,
            raw_payload={"disagreed": True},
        )

    result = perform_fidelity_check(
        primary_envelopes_by_window=primary_envelopes,
        secondary_window_source=_disagreeing_source,
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    start_sample = next(s for s in result.windows if s.window == start_window)
    assert start_sample.agrees() is False
    assert len(start_sample.disagreeing_event_keys) > 0
    assert result.agrees_overall is False


def test_fidelity_check_retains_both_acquisition_envelopes() -> None:
    """Both raw acquisition envelopes (primary + secondary) are
    retained per sampled window for the audit trail."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    secondary_source = build_agreeing_secondary_source(primary_envelopes)
    result = perform_fidelity_check(
        primary_envelopes_by_window=primary_envelopes,
        secondary_window_source=secondary_source,
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    for sample in result.windows:
        assert (
            sample.primary_envelope.endpoint_alias.startswith("robinhood")
            or sample.primary_envelope.endpoint_alias != "alchemy_free"
        )
        assert sample.secondary_envelope.endpoint_alias == "alchemy_free"
        # The cross-endpoint equality of the normalized hash must
        # not erase the raw envelope evidence; the secondary
        # envelope carries its own raw_payload.
        assert sample.secondary_envelope.raw_payload is not None


# ---------------------------------------------------------------------------
# Block-pinned StateView spot check
# ---------------------------------------------------------------------------


def test_state_spot_check_serves_when_endpoint_can_match_depth() -> None:
    """When the secondary endpoint serves the pinned block tag,
    the spot check records the golden values with the explicit
    block tag and the secondary endpoint's alias."""
    state_call = build_state_call_secondary_serving()
    result = perform_state_spot_check(
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
        block_pinned_state_call=state_call,
        endpoint_alias="alchemy_free",
    )
    assert isinstance(result, StateSpotCheckResult)
    assert result.served is True
    assert result.golden_values is not None
    assert isinstance(result.golden_values, StateViewGoldenValues)
    # The block tag is the explicit pinned-block hex form.
    assert result.block_tag == "0x" + format(REFERENCE_COVERAGE_TO_BLOCK, "x")
    assert result.endpoint_alias == "alchemy_free"
    # The golden values record both StateView method results.
    assert isinstance(result.golden_values.get_slot_0_result, bytes)
    assert isinstance(result.golden_values.get_liquidity_result, bytes)


def test_state_spot_check_blocks_when_endpoint_cannot_serve_depth() -> None:
    """When the primary endpoint cannot serve the pinned depth,
    the spot check records a blocked check; no ``latest``
    substitution is allowed."""
    state_call = build_state_call_primary_blocked()
    result = perform_state_spot_check(
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
        block_pinned_state_call=state_call,
        endpoint_alias="robinhood_public",
    )
    assert result.served is False
    assert result.golden_values is None
    assert result.block_reason is not None
    # The endpoint alias recorded on the blocked check is the
    # primary endpoint's alias; the audit trail makes the routing
    # explicit.
    assert result.endpoint_alias == "robinhood_public"


def test_state_spot_check_block_pinned_callable_uses_default_endpoint() -> None:
    """The :func:`make_block_pinned_state_call` helper binds to the
    named default endpoint so the spot check knows which endpoint
    the calls were issued to."""
    from robinhood_lp.qualification.state_spot_check import (
        STATE_VIEW_METHOD_GET_SLOT_0 as M1,
    )

    payload = b"\xab" * 32
    block_tag = "0x" + format(REFERENCE_COVERAGE_TO_BLOCK, "x")
    call = make_block_pinned_state_call(
        {(M1, block_tag, "alchemy_free"): payload},
        default_endpoint="alchemy_free",
    )
    out = call(M1, block_tag, b"\x00" * 32)
    assert out == payload
    # An unmapped (method, block_tag, default_endpoint) returns None.
    assert call("nonexistent_method", block_tag, b"\x00" * 32) is None


# ---------------------------------------------------------------------------
# Failure-path evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,expected_reason",
    [
        (FAILURE_PATH_KIND_HTTP_429, "user_agent_rejected"),
        (FAILURE_PATH_KIND_LOGS_LIMIT_REJECTION, "capability_regression"),
        (FAILURE_PATH_KIND_RPC_TIMEOUT, "capability_regression"),
        (FAILURE_PATH_KIND_FAILOVER, "cross_provider_discrepancy"),
        (FAILURE_PATH_KIND_BUDGET_EXHAUSTED, "budget_exhausted"),
    ],
)
def test_failure_path_evidence_emits_named_reason_code(kind: str, expected_reason: str) -> None:
    """Each documented failure-path kind emits its named T034
    reason code."""
    evidence = build_failure_path_evidence(
        kind=kind,
        detail={"endpoint_alias": "robinhood_public"},
    )
    assert isinstance(evidence, FailurePathEvidence)
    assert evidence.kind == kind
    assert evidence.reason_code == expected_reason


def test_failure_path_evidence_rejects_unknown_kind() -> None:
    """An unknown failure-path kind is rejected."""
    with pytest.raises(ValueError, match="not in DOCUMENTED_FAILURE_PATH_KINDS"):
        build_failure_path_evidence(kind="not_a_real_kind")


def test_failure_path_evidence_rejects_mismatched_reason_code() -> None:
    """A failure-path kind whose ``reason_code`` does not match the
    documented mapping is rejected."""
    with pytest.raises(ValueError, match="does not match the documented"):
        FailurePathEvidence(
            kind=FAILURE_PATH_KIND_BUDGET_EXHAUSTED,
            reason_code="wrong_reason_code",
        )


def test_all_documented_failure_path_evidence_covers_all_five() -> None:
    """The helper that produces one row per documented failure-path
    kind returns exactly five rows covering every documented kind."""
    rows = all_documented_failure_path_evidence()
    assert len(rows) == len(DOCUMENTED_FAILURE_PATH_KINDS) == 5
    produced_kinds = {row.kind for row in rows}
    assert produced_kinds == set(DOCUMENTED_FAILURE_PATH_KINDS)


def test_failure_path_evidence_blocks_complete_when_budget_exhausted() -> None:
    """Budget-exhausted evidence forces ``complete=false``."""
    evidence = all_documented_failure_path_evidence()
    assert fail_complete_under_failure_paths(evidence) is True


def test_failure_path_evidence_blocks_complete_when_failover_recorded() -> None:
    """Failover evidence forces ``complete=false``."""
    evidence = (
        build_failure_path_evidence(
            kind=FAILURE_PATH_KIND_FAILOVER,
            detail={"from_alias": "robinhood_public", "to_alias": "alchemy_free"},
        ),
    )
    assert fail_complete_under_failure_paths(evidence) is True


def test_failure_path_evidence_does_not_block_for_non_blocking_kinds() -> None:
    """The non-blocking failure paths (HTTP 429 / logs limit /
    timeout) do not by themselves force ``complete=false``;
    they are recorded as findings the operator can review."""
    evidence = (
        build_failure_path_evidence(kind=FAILURE_PATH_KIND_HTTP_429),
        build_failure_path_evidence(kind=FAILURE_PATH_KIND_LOGS_LIMIT_REJECTION),
        build_failure_path_evidence(kind=FAILURE_PATH_KIND_RPC_TIMEOUT),
    )
    assert fail_complete_under_failure_paths(evidence) is False


# ---------------------------------------------------------------------------
# Operator runbook
# ---------------------------------------------------------------------------


def test_operator_runbook_records_endpoint_routing_explicitly() -> None:
    """The pinned operator runbook names the endpoint that carries
    the wide scan, the endpoint that carries the sampled
    cross-validation, and the endpoint that carries the
    block-pinned state read."""
    assert isinstance(OPERATOR_RUNBOOK, OperatorRunbook)
    roles = {entry.role for entry in OPERATOR_RUNBOOK.endpoints}
    assert "wide_pool_filtered_scan" in roles
    assert "sampled_cross_validation" in roles
    assert "block_pinned_state_read" in roles
    for entry in OPERATOR_RUNBOOK.endpoints:
        assert isinstance(entry, EndpointRoutingEntry)
    # The Markdown rendering is non-empty and includes the routing table.
    md = OPERATOR_RUNBOOK.to_markdown()
    assert "wide_pool_filtered_scan" in md
    assert "sampled_cross_validation" in md
    assert "block_pinned_state_read" in md
    assert "alchemy_free" in md
    assert "robinhood_public" in md


def test_operator_runbook_is_deterministic() -> None:
    """Two calls return identical value objects."""
    a = build_operator_runbook()
    b = build_operator_runbook()
    assert a == b
    assert a.to_markdown() == b.to_markdown()


# ---------------------------------------------------------------------------
# End-to-end qualification fixture
# ---------------------------------------------------------------------------


def test_e2e_qualification_baseline_consistent_event_stream() -> None:
    """The synthetic-but-faithful event stream the end-to-end
    fixture relies on matches the 2026-09-17 baseline on every
    per-event-type count and the distinct-block count."""
    records = build_baseline_consistent_event_stream()
    assert_baseline_consistent(records)
    assert len(records) == BASELINE_TOTAL_EVENTS
    assert distinct_block_count_from_records(records) == BASELINE_DISTINCT_BLOCKS


def test_e2e_qualification_reference_dataset_passes_with_complete_true() -> None:
    """The full reference-dataset qualification pipeline passes
    with ``complete=true`` when the synthetic-but-faithful event
    stream is run through it with agreeing secondary envelopes
    and a serving secondary endpoint.

    The test exercises every acceptance clause:

    - the ``PoolId`` re-derivation check matches the pinned
      ``PoolId``;
    - the baseline comparison agrees on every per-event-type count
      and the distinct-block count;
    - the fidelity check agrees on every sampled window (start /
      middle / end) and the result-bearing requirement is met;
    - the block-pinned StateView spot check serves the pinned
      depth and records the golden values;
    - the T034 machine report has ``complete=true`` and zero
      qualification blockers;
    - the runbook is recorded as the operator runbook.
    """
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    secondary_source = build_agreeing_secondary_source(primary_envelopes)
    state_call = build_state_call_secondary_serving()
    report = build_reference_qualification_report(
        inputs=ReferenceQualificationInputs(
            target=REFERENCE_TARGET,
            primary_records=records,
            primary_envelopes_by_window=primary_envelopes,
            secondary_window_source=secondary_source,
            manifest_checksum="0x" + "ab" * 32,
            run_id="e2e-t036-real",
        ),
        block_pinned_state_call=state_call,
    )
    assert isinstance(report, ReferenceQualificationReport)
    assert report.complete is True
    assert report.qualification_blockers == ()
    # PoolId re-derivation agrees.
    assert report.pool_id_check.matches_pinned is True
    # Baseline comparison agrees on every dimension.
    assert report.baseline_comparison.agrees_overall is True
    # Fidelity check agrees and meets the result-bearing requirement.
    assert report.fidelity_check is not None
    assert report.fidelity_check.agrees_overall is True
    assert report.fidelity_check.blocked_window_count == 0
    assert report.fidelity_check.result_bearing_window_count >= 1
    assert set(report.fidelity_check.sample_coverage) == {"start", "middle", "end"}
    # StateView spot check serves the pinned depth.
    assert report.state_spot_check is not None
    assert report.state_spot_check.served is True
    # The T034 machine report has complete=true and the recorded
    # PoolId re-derivation + baseline comparison + fidelity + spot
    # check + runbook surface in the machine dict.
    t34_report = report.to_t34_quality_report()
    assert t34_report.complete is True
    payload = t34_report.to_machine_dict()
    assert payload["complete"] is True
    assert payload["manifest_checksum"] == "0x" + "ab" * 32
    assert payload["chain_id"] == REFERENCE_CHAIN_ID
    assert payload["pool_id"] == REFERENCE_POOL_ID_HEX
    # The default infrastructure-correlation state surfaces in the
    # machine dict, with the cross_endpoint_agreement wording.
    assert payload["infrastructure_state"] == "unknown_not_proven"
    assert payload["agreement_phrasing"] == "cross_endpoint_agreement"
    assert payload["correlated_failure_residual_risk"] is True
    # The runbook is recorded.
    capability_snapshot = payload["capability_snapshot"]
    assert any(entry["alias"] == "robinhood_public" for entry in capability_snapshot)
    assert any(entry["alias"] == "alchemy_free" for entry in capability_snapshot)
    # The provider provenance includes one row per sampled window.
    assert len(payload["provider_provenance"]) >= 6


def test_e2e_qualification_halts_on_baseline_discrepancy() -> None:
    """A per-event-type baseline discrepancy halts qualification
    with the ``range_coverage_gap`` reason code on the T034
    machine report."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    state_call = build_state_call_secondary_serving()
    # Drop one ModifyLiquidity record so the baseline comparison
    # disagrees on the per-event-type count. The synthetic stream
    # has 578 ModifyLiquidity records; dropping one yields 577,
    # which is one below the baseline.
    short_records = list(records)
    for i, r in enumerate(short_records):
        if type(r).__name__ == "ModifyLiquidityLogRecord":
            del short_records[i]
            break
    primary_envelopes_short = build_primary_envelopes_by_window(short_records, windows)
    secondary_source_short = build_agreeing_secondary_source(primary_envelopes_short)
    report = build_reference_qualification_report(
        inputs=ReferenceQualificationInputs(
            target=REFERENCE_TARGET,
            primary_records=short_records,
            primary_envelopes_by_window=primary_envelopes_short,
            secondary_window_source=secondary_source_short,
            manifest_checksum="0x" + "ab" * 32,
            run_id="e2e-t036-discrepancy",
        ),
        block_pinned_state_call=state_call,
    )
    assert report.complete is False
    assert "range_coverage_gap" in report.qualification_blockers
    t34_report = report.to_t34_quality_report()
    payload = t34_report.to_machine_dict()
    reason_codes = {f["reason_code"] for f in payload["findings"]}
    assert "range_coverage_gap" in reason_codes


def test_e2e_qualification_halts_on_blocked_fidelity_window() -> None:
    """A blocked fidelity check halts qualification with the
    ``cross_endpoint_sample_missing`` reason code; the blocked
    window is never silently skipped and never replaced by a
    primary re-read."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    end_window = next(w for w in windows if w.label == "end")
    secondary_source = build_blocked_secondary_source(end_window)
    state_call = build_state_call_secondary_serving()
    report = build_reference_qualification_report(
        inputs=ReferenceQualificationInputs(
            target=REFERENCE_TARGET,
            primary_records=records,
            primary_envelopes_by_window=primary_envelopes,
            secondary_window_source=secondary_source,
            manifest_checksum="0x" + "ab" * 32,
            run_id="e2e-t036-blocked",
        ),
        block_pinned_state_call=state_call,
    )
    assert report.complete is False
    assert "cross_endpoint_sample_missing" in report.qualification_blockers
    t34_report = report.to_t34_quality_report()
    payload = t34_report.to_machine_dict()
    reason_codes = {f["reason_code"] for f in payload["findings"]}
    assert "cross_endpoint_sample_missing" in reason_codes


def test_e2e_qualification_halts_on_blocked_state_read() -> None:
    """A block-pinned StateView read against an endpoint that
    cannot serve the pinned depth halts qualification with the
    ``cross_endpoint_sample_missing`` reason code; no ``latest``
    substitution is allowed."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    secondary_source = build_agreeing_secondary_source(primary_envelopes)
    state_call = build_state_call_primary_blocked()
    report = build_reference_qualification_report(
        inputs=ReferenceQualificationInputs(
            target=REFERENCE_TARGET,
            primary_records=records,
            primary_envelopes_by_window=primary_envelopes,
            secondary_window_source=secondary_source,
            manifest_checksum="0x" + "ab" * 32,
            run_id="e2e-t036-state-blocked",
        ),
        block_pinned_state_call=state_call,
    )
    assert report.complete is False
    assert "cross_endpoint_sample_missing" in report.qualification_blockers
    assert report.state_spot_check is not None
    assert report.state_spot_check.served is False


def test_e2e_qualification_halts_on_pool_id_mismatch() -> None:
    """A ``PoolId`` re-derivation mismatch halts qualification
    with the ``metadata_failure`` reason code on the T034
    machine report."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    secondary_source = build_agreeing_secondary_source(primary_envelopes)
    state_call = build_state_call_secondary_serving()
    wrong_pool_id = PoolId(int.from_bytes(b"\x99" * 32, "big"))
    pool_id_check = check_pool_id_derivation(pinned_pool_id=wrong_pool_id)
    report = build_reference_qualification_report(
        inputs=ReferenceQualificationInputs(
            target=REFERENCE_TARGET,
            primary_records=records,
            primary_envelopes_by_window=primary_envelopes,
            secondary_window_source=secondary_source,
            pool_key_check=pool_id_check,
            manifest_checksum="0x" + "ab" * 32,
            run_id="e2e-t036-pool-id-mismatch",
        ),
        block_pinned_state_call=state_call,
    )
    assert report.complete is False
    assert "metadata_failure" in report.qualification_blockers
    t34_report = report.to_t34_quality_report()
    payload = t34_report.to_machine_dict()
    reason_codes = {f["reason_code"] for f in payload["findings"]}
    assert "metadata_failure" in reason_codes


def test_e2e_qualification_failure_path_evidence_does_not_force_complete_false_for_non_blocking_kinds() -> (
    None
):
    """The non-blocking failure-path kinds (HTTP 429, logs limit,
    timeout) are recorded as findings but do not by themselves
    force ``complete=false``."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    secondary_source = build_agreeing_secondary_source(primary_envelopes)
    state_call = build_state_call_secondary_serving()
    failure_paths = (
        build_failure_path_evidence(kind=FAILURE_PATH_KIND_HTTP_429),
        build_failure_path_evidence(kind=FAILURE_PATH_KIND_LOGS_LIMIT_REJECTION),
        build_failure_path_evidence(kind=FAILURE_PATH_KIND_RPC_TIMEOUT),
    )
    report = build_reference_qualification_report(
        inputs=ReferenceQualificationInputs(
            target=REFERENCE_TARGET,
            primary_records=records,
            primary_envelopes_by_window=primary_envelopes,
            secondary_window_source=secondary_source,
            failure_path_evidence=failure_paths,
            manifest_checksum="0x" + "ab" * 32,
            run_id="e2e-t036-non-blocking-failure-paths",
        ),
        block_pinned_state_call=state_call,
    )
    # The report still completes; the non-blocking failure paths
    # are findings on the T034 machine report.
    assert report.complete is True
    t34_report = report.to_t34_quality_report()
    payload = t34_report.to_machine_dict()
    reason_codes = {f["reason_code"] for f in payload["findings"]}
    assert "user_agent_rejected" in reason_codes
    assert "capability_regression" in reason_codes


def test_e2e_qualification_budget_exhaustion_blocks_complete() -> None:
    """Budget-exhaustion evidence forces ``complete=false`` and
    surfaces the ``budget_exhausted`` reason code."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    secondary_source = build_agreeing_secondary_source(primary_envelopes)
    state_call = build_state_call_secondary_serving()
    failure_paths = (
        build_failure_path_evidence(
            kind=FAILURE_PATH_KIND_BUDGET_EXHAUSTED,
            detail={"endpoint_alias": "alchemy_free", "remaining_calls": 0},
        ),
    )
    report = build_reference_qualification_report(
        inputs=ReferenceQualificationInputs(
            target=REFERENCE_TARGET,
            primary_records=records,
            primary_envelopes_by_window=primary_envelopes,
            secondary_window_source=secondary_source,
            failure_path_evidence=failure_paths,
            manifest_checksum="0x" + "ab" * 32,
            run_id="e2e-t036-budget-exhausted",
        ),
        block_pinned_state_call=state_call,
    )
    assert report.complete is False
    assert "budget_exhausted" in report.qualification_blockers
    t34_report = report.to_t34_quality_report()
    payload = t34_report.to_machine_dict()
    reason_codes = {f["reason_code"] for f in payload["findings"]}
    assert "budget_exhausted" in reason_codes


def test_e2e_qualification_failover_blocks_complete() -> None:
    """Failover evidence forces ``complete=false`` and surfaces
    the ``cross_provider_discrepancy`` reason code."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    secondary_source = build_agreeing_secondary_source(primary_envelopes)
    state_call = build_state_call_secondary_serving()
    failure_paths = (
        build_failure_path_evidence(
            kind=FAILURE_PATH_KIND_FAILOVER,
            detail={"from_alias": "robinhood_public", "to_alias": "alchemy_free"},
        ),
    )
    report = build_reference_qualification_report(
        inputs=ReferenceQualificationInputs(
            target=REFERENCE_TARGET,
            primary_records=records,
            primary_envelopes_by_window=primary_envelopes,
            secondary_window_source=secondary_source,
            failure_path_evidence=failure_paths,
            manifest_checksum="0x" + "ab" * 32,
            run_id="e2e-t036-failover",
        ),
        block_pinned_state_call=state_call,
    )
    assert report.complete is False
    assert "cross_provider_discrepancy" in report.qualification_blockers


def test_e2e_qualification_runbook_is_recorded_in_machine_dict() -> None:
    """The operator runbook is recorded in the report value
    object so the audit trail captures the routing decision."""
    records = build_baseline_consistent_event_stream()
    windows = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    primary_envelopes = build_primary_envelopes_by_window(records, windows)
    secondary_source = build_agreeing_secondary_source(primary_envelopes)
    state_call = build_state_call_secondary_serving()
    report = build_reference_qualification_report(
        inputs=ReferenceQualificationInputs(
            target=REFERENCE_TARGET,
            primary_records=records,
            primary_envelopes_by_window=primary_envelopes,
            secondary_window_source=secondary_source,
            manifest_checksum="0x" + "ab" * 32,
            run_id="e2e-t036-runbook",
        ),
        block_pinned_state_call=state_call,
    )
    assert report.runbook is OPERATOR_RUNBOOK
    # The audit-trail dict carries the runbook.
    report_dict = report.to_dict()
    assert "endpoints" in report_dict["runbook"]
    assert any(
        entry["role"] == "wide_pool_filtered_scan" for entry in report_dict["runbook"]["endpoints"]
    )
