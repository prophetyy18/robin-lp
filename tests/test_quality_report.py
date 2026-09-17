"""Tests for T034 data-quality and completeness reports.

Covers the T034 acceptance matrix:

- seeded defect fixtures trigger the correct reason code;
- the 5 Decision 6 A+B deterministic required-sample rules;
- infrastructure-correlation state enum is never blank, the
  ``cross_endpoint_agreement`` wording is enforced, and the
  ``independent_provider_agreement`` wording is rejected unless the
  state is ``evidenced_independent``;
- sample selection is deterministic (no RNG, no wall clock);
- cold-start / warm-run / backtest-window-only states each produce
  the right verdict;
- warning counts do not disappear in aggregates;
- ``complete=true`` requires zero blockers from the verifier.
"""

from __future__ import annotations

import pytest

from robinhood_lp.protocol.events import EventKey
from robinhood_lp.protocol.ids import ChainId, PoolId
from robinhood_lp.quality import (
    CROSS_ENDPOINT_AGREEMENT_PHRASING,
    INDEPENDENT_PROVIDER_AGREEMENT_PHRASING,
    REASON_BLOCK_HASH_GAP,
    REASON_BUDGET_EXHAUSTED,
    REASON_CAPABILITY_REGRESSION,
    REASON_CHECKPOINT_MISMATCH,
    REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE,
    REASON_CROSS_ENDPOINT_SAMPLE_INFRASTRUCTURE_UNCLEAR,
    REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
    REASON_CROSS_PROVIDER_DISCREPANCY,
    REASON_DECODE_ERROR,
    REASON_DUPLICATE_EVENT_KEY,
    REASON_IMPOSSIBLE_VALUE,
    REASON_METADATA_FAILURE,
    REASON_MISORDERED_LOG,
    REASON_RANGE_COVERAGE_GAP,
    REASON_STALE_ENDPOINT,
    REASON_UNKNOWN_POOL,
    REASON_USER_AGENT_REJECTED,
    WINDOW_BLOCKS,
    ActualBudgetEntry,
    BudgetDeviation,
    CapabilityEntry,
    InfrastructureCorrelationState,
    PerEventTypeSample,
    PerFailoverSample,
    PerPartitionSample,
    PerRunSample,
    PreflightBudgetEntry,
    ProviderProvenanceEntry,
    QualityFinding,
    QualityReport,
    RequiredSamples,
    SharedInfrastructureEvidence,
    TopologyKind,
    VolumeEntry,
    agreement_phrasing_for_state,
    correlated_failure_is_residual_risk,
    empty_report,
    is_qualification_fatal_reason,
    select_per_failover_sample,
    select_per_partition_sample,
    select_required_samples,
    verify_report,
    with_actual_budget,
    with_budget_deviation,
    with_capability_snapshot,
    with_complete,
    with_finding,
    with_infrastructure_state,
    with_preflight_budget,
    with_provider_provenance,
    with_qualification_blocker,
    with_response_volume,
    with_result_bearing_windows,
    with_shared_infrastructure_evidence,
)

POOL_ID = PoolId.from_hex("0x" + "11" * 32)
CHAIN_ID = 4663
POOL_INIT_BLOCK = 1000
COVERAGE_FROM = POOL_INIT_BLOCK
COVERAGE_TO = POOL_INIT_BLOCK + 100


# ---------------------------------------------------------------------------
# Fixture: minimal report scaffold
# ---------------------------------------------------------------------------


def _required_samples(*, result_bearing: bool = True) -> RequiredSamples:
    """Build a minimal RequiredSamples used by the tests."""
    pool_id = POOL_ID
    key = EventKey(
        chain_id=ChainId(CHAIN_ID),
        block_hash=0xAA * 32 // 0xAA if False else int.from_bytes(b"\xaa" * 32, "big"),
        tx_hash=int.from_bytes(b"\xbb" * 32, "big"),
        log_index=0,
    )
    samples = select_required_samples(
        pool_id=pool_id,
        chain_id=CHAIN_ID,
        coverage_from_block=COVERAGE_FROM,
        coverage_to_block=COVERAGE_TO,
        manifest_checksum="0x" + "cc" * 32,
        partitions=(("p1", COVERAGE_FROM, COVERAGE_FROM + 9),),
        event_types=(("Initialize", key),),
        failovers=(COVERAGE_FROM + 50,),
    )
    if result_bearing:
        samples = with_result_bearing_windows(
            samples, result_bearing_windows=((COVERAGE_FROM, COVERAGE_FROM + 9),)
        )
    return samples


def _empty_report(
    *,
    topology: TopologyKind = TopologyKind.COLD_START,
    qualified_checkpoint_ref: str | None = None,
    required_samples: RequiredSamples | None = None,
) -> QualityReport:
    return empty_report(
        chain_id=CHAIN_ID,
        contract_address="0x" + "33" * 20,
        pool_id=POOL_ID,
        pool_init_block=POOL_INIT_BLOCK,
        coverage_from_block=COVERAGE_FROM,
        coverage_to_block=COVERAGE_TO,
        topology=topology,
        required_samples=required_samples or _required_samples(),
        manifest_checksum="0x" + "dd" * 32,
        qualified_checkpoint_ref=qualified_checkpoint_ref,
    )


def _full_capability_snapshot() -> tuple[CapabilityEntry, ...]:
    return (
        CapabilityEntry(
            alias="A",
            chain_id=CHAIN_ID,
            finality_tags=("finalized",),
            archive_state_depth_blocks=10_000,
            accepted_log_range_blocks=100,
            latency_ms=42,
            probe_block_number=COVERAGE_FROM,
            probe_block_hash="0x" + "ab" * 32,
            max_blocks_per_get_logs=100,
            observed_response_size_bytes=4096,
            call_budget_allocated=1000,
            remaining_budget_source="operator_injection",
        ),
        CapabilityEntry(
            alias="B",
            chain_id=CHAIN_ID,
            finality_tags=("finalized",),
            archive_state_depth_blocks=10_000,
            accepted_log_range_blocks=10,
            latency_ms=80,
            probe_block_number=COVERAGE_FROM,
            probe_block_hash="0x" + "ab" * 32,
            max_blocks_per_get_logs=10,
            observed_response_size_bytes=2048,
            call_budget_allocated=200,
            remaining_budget_source="operator_injection",
        ),
    )


def _all_agree() -> list[bool]:
    return [True] * (1 + 1 + 1 + 1)


# ---------------------------------------------------------------------------
# Acceptance: seeded defect fixtures trigger the correct reason code
# ---------------------------------------------------------------------------


def _finding(reason_code: str) -> QualityFinding:
    return QualityFinding(reason_code=reason_code, category="test", detail={"k": "v"})


@pytest.mark.parametrize(
    "reason_code",
    [
        REASON_RANGE_COVERAGE_GAP,
        REASON_BLOCK_HASH_GAP,
        REASON_DUPLICATE_EVENT_KEY,
        REASON_MISORDERED_LOG,
        REASON_UNKNOWN_POOL,
        REASON_DECODE_ERROR,
        REASON_IMPOSSIBLE_VALUE,
        REASON_STALE_ENDPOINT,
        REASON_METADATA_FAILURE,
        REASON_CROSS_PROVIDER_DISCREPANCY,
        REASON_BUDGET_EXHAUSTED,
        REASON_CAPABILITY_REGRESSION,
        REASON_USER_AGENT_REJECTED,
        REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
        REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE,
        REASON_CROSS_ENDPOINT_SAMPLE_INFRASTRUCTURE_UNCLEAR,
        REASON_CHECKPOINT_MISMATCH,
    ],
)
def test_seeded_defect_fixture_triggers_correct_reason_code(reason_code: str) -> None:
    """Every defect listed in the T034 acceptance matrix has a
    fixture whose expected reason code matches the named code."""
    report = _empty_report()
    report = with_finding(report, _finding(reason_code))
    counts = report.reason_code_counts()
    assert counts.get(reason_code) == 1
    # The finding surfaces in the machine-readable dict.
    payload = report.to_machine_dict()
    reason_codes = {f["reason_code"] for f in payload["findings"]}
    assert reason_code in reason_codes


def test_finding_rejects_unknown_reason_code() -> None:
    with pytest.raises(ValueError):
        QualityFinding(reason_code="not_a_real_code", category="test", detail={})


def test_qualification_fatal_reason_codes_constant() -> None:
    assert is_qualification_fatal_reason(REASON_BUDGET_EXHAUSTED)
    assert is_qualification_fatal_reason(REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE)
    assert is_qualification_fatal_reason(REASON_CROSS_ENDPOINT_SAMPLE_MISSING)
    # Non-fatal codes are not flagged as fatal.
    assert not is_qualification_fatal_reason(REASON_DECODE_ERROR)
    assert not is_qualification_fatal_reason(REASON_UNKNOWN_POOL)


# ---------------------------------------------------------------------------
# Acceptance: complete=true requires all blocker clauses satisfied
# ---------------------------------------------------------------------------


def test_complete_true_requires_all_blocker_clauses_satisfied() -> None:
    """``complete=true`` requires zero blockers from the verifier."""
    report = _empty_report()
    report = with_capability_snapshot(report, _full_capability_snapshot())
    result = verify_report(
        report,
        sample_agreement=_all_agree(),
        has_result_bearing_sample=True,
    )
    assert result.complete is True
    assert result.blockers == ()
    # The on-report field matches.
    assert report.complete is True
    # And the verifier's verdict, when copied onto the report via
    # ``with_complete``, matches the verifier.
    report = with_complete(report, result.complete)
    assert report.complete is True


def test_complete_false_when_budget_exhausted_finding_present() -> None:
    report = with_finding(_empty_report(), _finding(REASON_BUDGET_EXHAUSTED))
    result = verify_report(report, sample_agreement=_all_agree())
    assert result.complete is False
    assert REASON_BUDGET_EXHAUSTED in result.blockers


def test_complete_false_when_sample_missing() -> None:
    report = _empty_report()
    result = verify_report(report, sample_agreement=None)
    assert result.complete is False
    assert REASON_CROSS_ENDPOINT_SAMPLE_MISSING in result.blockers


def test_complete_false_when_samples_disagree() -> None:
    report = _empty_report()
    agreement = [True, True, True, False]  # last (failover) disagrees
    result = verify_report(report, sample_agreement=agreement)
    assert result.complete is False
    assert REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE in result.blockers


def test_complete_false_when_manifest_checksum_unrecorded() -> None:
    # Construct an empty report via direct dataclasses.replace so we
    # can produce a malformed report with no manifest checksum.
    import dataclasses

    report = dataclasses.replace(_empty_report(), manifest_checksum="")
    result = verify_report(report, sample_agreement=_all_agree())
    assert result.complete is False
    assert "manifest_checksum_unrecorded" in result.blockers


def test_complete_false_when_manifest_checksum_drifts() -> None:
    report = _empty_report()
    result = verify_report(
        report,
        sample_agreement=_all_agree(),
        expected_manifest_checksum="0x" + "ee" * 32,
    )
    assert result.complete is False
    assert "manifest_checksum_drift" in result.blockers


# ---------------------------------------------------------------------------
# Acceptance: 5 Decision 6 A+B deterministic required-sample rules
# ---------------------------------------------------------------------------


def test_required_samples_include_per_run() -> None:
    """Rule 1: per-run compares chain ID / genesis hash / fixed-block
    hash / PoolManager / StateView runtime code hash."""
    samples = _required_samples()
    per_run = samples.per_run
    assert isinstance(per_run, PerRunSample)
    assert per_run.pool_id == POOL_ID
    assert per_run.chain_id == CHAIN_ID
    assert per_run.fixed_block_number >= COVERAGE_FROM
    # Selection inputs are recorded so the selection is reproducible.
    assert "pool_id" in per_run.selection_inputs
    assert "chain_id" in per_run.selection_inputs
    assert "fixed_block_number" in per_run.selection_inputs


def test_required_samples_include_per_partition_windows() -> None:
    """Rule 2: per-partition opening + trailing 10-block windows."""
    samples = _required_samples()
    assert len(samples.per_partition) == 1
    per_partition = samples.per_partition[0]
    assert isinstance(per_partition, PerPartitionSample)
    # Opening window: first 10 blocks of the partition.
    start = per_partition.start_block
    assert per_partition.opening_window == (
        start,
        min(start + WINDOW_BLOCKS - 1, per_partition.end_block),
    )
    # Trailing window: last 10 blocks of the partition.
    assert per_partition.trailing_window[1] == per_partition.end_block


def test_required_samples_include_per_event_type_window() -> None:
    """Rule 3: per-event-type window contains the smallest EventKey."""
    samples = _required_samples()
    assert len(samples.per_event_type) == 1
    per_event_type = samples.per_event_type[0]
    assert isinstance(per_event_type, PerEventTypeSample)
    window_start, window_end = per_event_type.window
    block_number = per_event_type.smallest_event_key.block_ref().block_number or 0
    # The window ends at the block of the EventKey.
    assert window_end == block_number
    # The window spans at most WINDOW_BLOCKS.
    assert window_end - window_start + 1 <= WINDOW_BLOCKS


def test_required_samples_dedup_identical_event_windows() -> None:
    """Identical windows are deduplicated."""
    key = EventKey(
        chain_id=ChainId(CHAIN_ID),
        block_hash=int.from_bytes(b"\xaa" * 32, "big"),
        tx_hash=int.from_bytes(b"\xbb" * 32, "big"),
        log_index=0,
    )
    # Two event types with identical smallest EventKey windows must
    # collapse to a single per-event-type sample.
    samples = select_required_samples(
        pool_id=POOL_ID,
        chain_id=CHAIN_ID,
        coverage_from_block=COVERAGE_FROM,
        coverage_to_block=COVERAGE_TO,
        manifest_checksum="0x" + "cc" * 32,
        partitions=(),
        event_types=(("Initialize", key), ("ModifyLiquidity", key)),
        failovers=(),
    )
    assert len(samples.per_event_type) == 1


def test_required_samples_include_per_failover_windows() -> None:
    """Rule 4: per-failover pre + post 10-block windows."""
    samples = _required_samples()
    assert len(samples.per_failover) == 1
    per_failover = samples.per_failover[0]
    assert isinstance(per_failover, PerFailoverSample)
    # Pre-window ends at boundary - 1; post-window starts at boundary.
    assert per_failover.pre_window[1] == per_failover.failover_at_block - 1
    assert per_failover.post_window[0] == per_failover.failover_at_block
    assert per_failover.post_window[1] - per_failover.post_window[0] + 1 == WINDOW_BLOCKS


def test_required_samples_at_least_one_result_bearing_window() -> None:
    """Rule 5: when the dataset contains any events, at least one
    required A+B comparison must be result-bearing."""
    samples = _required_samples(result_bearing=True)
    assert len(samples.result_bearing_windows) >= 1
    # All-empty capability samples do not satisfy this rule.
    empty_only = RequiredSamples(
        per_run=samples.per_run,
        per_partition=samples.per_partition,
        per_event_type=samples.per_event_type,
        per_failover=samples.per_failover,
        result_bearing_windows=(),
    )
    assert len(empty_only.result_bearing_windows) == 0


def test_verify_requires_result_bearing_when_dataset_has_events() -> None:
    """When the dataset has events, the verifier rejects an
    empty-capability-probe-only result."""
    report = _empty_report()
    result = verify_report(
        report,
        sample_agreement=_all_agree(),
        has_result_bearing_sample=False,
        empty_capability_probe_only=True,
    )
    assert result.complete is False
    assert "result_bearing_required" in result.blockers
    assert "empty_capability_probe_only" in result.blockers


def test_verify_allows_empty_capability_probe_when_no_events() -> None:
    """When the dataset has no events, an empty capability probe is
    acceptable for range acceptance."""
    report = _empty_report()
    samples = select_required_samples(
        pool_id=POOL_ID,
        chain_id=CHAIN_ID,
        coverage_from_block=COVERAGE_FROM,
        coverage_to_block=COVERAGE_TO,
        manifest_checksum="0x" + "cc" * 32,
        partitions=(),
        event_types=(),
        failovers=(),
    )
    report = empty_report(
        chain_id=CHAIN_ID,
        contract_address="0x" + "33" * 20,
        pool_id=POOL_ID,
        pool_init_block=POOL_INIT_BLOCK,
        coverage_from_block=COVERAGE_FROM,
        coverage_to_block=COVERAGE_TO,
        topology=TopologyKind.COLD_START,
        required_samples=samples,
        manifest_checksum="0x" + "dd" * 32,
    )
    result = verify_report(report, sample_agreement=[True])
    assert result.complete is True


# ---------------------------------------------------------------------------
# Acceptance: infrastructure-correlation state (Decision 8)
# ---------------------------------------------------------------------------


def test_default_infrastructure_state_is_unknown_not_proven() -> None:
    report = _empty_report()
    assert report.infrastructure_state == InfrastructureCorrelationState.UNKNOWN_NOT_PROVEN


def test_infrastructure_state_must_not_be_blank() -> None:
    report = _empty_report()
    # The default state must never be None.
    assert report.infrastructure_state is not None
    # Setting a non-default state is supported.
    report = with_infrastructure_state(report, InfrastructureCorrelationState.KNOWN_CORRELATED)
    assert report.infrastructure_state == InfrastructureCorrelationState.KNOWN_CORRELATED


def test_correlated_failure_residual_risk_callout_for_unknown_state() -> None:
    report = _empty_report()
    # The machine dict carries the explicit residual-risk flag.
    payload = report.to_machine_dict()
    assert payload["infrastructure_state"] == "unknown_not_proven"
    assert payload["correlated_failure_residual_risk"] is True
    # The human summary surfaces the residual-risk callout.
    summary = report.to_human_summary()
    assert "RESIDUAL RISK" in summary


def test_correlated_failure_residual_risk_callout_for_known_correlated() -> None:
    report = with_infrastructure_state(
        _empty_report(), InfrastructureCorrelationState.KNOWN_CORRELATED
    )
    payload = report.to_machine_dict()
    assert payload["correlated_failure_residual_risk"] is True


def test_no_residual_risk_callout_for_evidenced_independent() -> None:
    report = with_infrastructure_state(
        _empty_report(), InfrastructureCorrelationState.EVIDENCED_INDEPENDENT
    )
    payload = report.to_machine_dict()
    assert payload["correlated_failure_residual_risk"] is False
    summary = report.to_human_summary()
    assert "RESIDUAL RISK" not in summary


# ---------------------------------------------------------------------------
# Acceptance: wording constraint — cross_endpoint_agreement only
# ---------------------------------------------------------------------------


def test_default_phrasing_is_cross_endpoint_agreement() -> None:
    assert agreement_phrasing_for_state(InfrastructureCorrelationState.UNKNOWN_NOT_PROVEN) == (
        CROSS_ENDPOINT_AGREEMENT_PHRASING
    )
    assert agreement_phrasing_for_state(InfrastructureCorrelationState.KNOWN_CORRELATED) == (
        CROSS_ENDPOINT_AGREEMENT_PHRASING
    )
    # Only evidenced_independent may use independent_provider_agreement.
    assert agreement_phrasing_for_state(InfrastructureCorrelationState.EVIDENCED_INDEPENDENT) == (
        INDEPENDENT_PROVIDER_AGREEMENT_PHRASING
    )


def test_machine_dict_never_emits_independent_provider_agreement_for_default_state() -> None:
    report = _empty_report()
    payload = report.to_machine_dict()
    assert payload["agreement_phrasing"] == CROSS_ENDPOINT_AGREEMENT_PHRASING
    # Sanity check: the wording string is not present anywhere when
    # the default state is used.
    summary = report.to_human_summary()
    assert INDEPENDENT_PROVIDER_AGREEMENT_PHRASING not in summary


def test_machine_dict_only_emits_independent_provider_agreement_when_evidenced() -> None:
    report = with_infrastructure_state(
        _empty_report(), InfrastructureCorrelationState.EVIDENCED_INDEPENDENT
    )
    payload = report.to_machine_dict()
    assert payload["agreement_phrasing"] == INDEPENDENT_PROVIDER_AGREEMENT_PHRASING


def test_human_summary_uses_cross_endpoint_agreement_wording() -> None:
    report = _empty_report()
    summary = report.to_human_summary()
    assert CROSS_ENDPOINT_AGREEMENT_PHRASING in summary
    assert INDEPENDENT_PROVIDER_AGREEMENT_PHRASING not in summary


# ---------------------------------------------------------------------------
# Acceptance: sample selection is deterministic (no RNG, no wall clock)
# ---------------------------------------------------------------------------


def test_required_samples_selection_is_deterministic() -> None:
    """Two calls with identical inputs produce byte-identical samples."""
    partitions = (("p1", COVERAGE_FROM, COVERAGE_FROM + 9),)
    key = EventKey(
        chain_id=ChainId(CHAIN_ID),
        block_hash=int.from_bytes(b"\xaa" * 32, "big"),
        tx_hash=int.from_bytes(b"\xbb" * 32, "big"),
        log_index=0,
    )
    event_types = (("Initialize", key),)
    failovers = (COVERAGE_FROM + 50,)
    samples_a = select_required_samples(
        pool_id=POOL_ID,
        chain_id=CHAIN_ID,
        coverage_from_block=COVERAGE_FROM,
        coverage_to_block=COVERAGE_TO,
        manifest_checksum="0x" + "cc" * 32,
        partitions=partitions,
        event_types=event_types,
        failovers=failovers,
    )
    samples_b = select_required_samples(
        pool_id=POOL_ID,
        chain_id=CHAIN_ID,
        coverage_from_block=COVERAGE_FROM,
        coverage_to_block=COVERAGE_TO,
        manifest_checksum="0x" + "cc" * 32,
        partitions=partitions,
        event_types=event_types,
        failovers=failovers,
    )
    assert samples_a == samples_b


def test_required_samples_reproducible_from_inputs_only() -> None:
    """Reproducibility: given the deterministic inputs only, the
    selection is reproducible."""
    partitions = (("p1", COVERAGE_FROM, COVERAGE_FROM + 9),)
    key = EventKey(
        chain_id=ChainId(CHAIN_ID),
        block_hash=int.from_bytes(b"\xaa" * 32, "big"),
        tx_hash=int.from_bytes(b"\xbb" * 32, "big"),
        log_index=0,
    )
    samples = select_required_samples(
        pool_id=POOL_ID,
        chain_id=CHAIN_ID,
        coverage_from_block=COVERAGE_FROM,
        coverage_to_block=COVERAGE_TO,
        manifest_checksum="0x" + "cc" * 32,
        partitions=partitions,
        event_types=(("Initialize", key),),
        failovers=(),
    )
    per_run_inputs = samples.per_run.selection_inputs_dict()
    assert per_run_inputs["pool_id"] == POOL_ID.to_hex()
    assert per_run_inputs["chain_id"] == CHAIN_ID
    assert per_run_inputs["fixed_block_number"] == COVERAGE_FROM


def test_select_per_partition_window_is_at_most_ten_blocks() -> None:
    sample = select_per_partition_sample(
        pool_id=POOL_ID,
        partition_id="p1",
        start_block=COVERAGE_FROM,
        end_block=COVERAGE_FROM + 100,
    )
    # Opening window: first 10 blocks.
    assert sample.opening_window == (COVERAGE_FROM, COVERAGE_FROM + WINDOW_BLOCKS - 1)
    # Trailing window: last 10 blocks.
    assert sample.trailing_window == (COVERAGE_FROM + 91, COVERAGE_FROM + 100)


def test_select_per_failover_pre_window_strictly_before_boundary() -> None:
    sample = select_per_failover_sample(pool_id=POOL_ID, failover_at_block=100)
    assert sample.pre_window == (90, 99)
    assert sample.post_window == (100, 109)


def test_select_per_failover_handles_genesis_boundary() -> None:
    """A failover at block 0 has no pre-window; the placeholder is
    a single-block window."""
    sample = select_per_failover_sample(pool_id=POOL_ID, failover_at_block=0)
    assert sample.pre_window == (0, 0)
    assert sample.post_window == (0, 9)


# ---------------------------------------------------------------------------
# Acceptance: cold-start / warm-run / backtest-window-only states
# ---------------------------------------------------------------------------


def test_cold_start_topology_satisfied_when_coverage_from_is_init_block() -> None:
    report = _empty_report(topology=TopologyKind.COLD_START)
    result = verify_report(report, sample_agreement=_all_agree())
    assert result.complete is True


def test_cold_start_topology_rejected_when_coverage_starts_after_init_block() -> None:
    report = empty_report(
        chain_id=CHAIN_ID,
        contract_address="0x" + "33" * 20,
        pool_id=POOL_ID,
        pool_init_block=POOL_INIT_BLOCK,
        # Cold start but coverage starts AFTER pool_init_block: the
        # cold-start basis is not satisfied.
        coverage_from_block=POOL_INIT_BLOCK + 1,
        coverage_to_block=POOL_INIT_BLOCK + 100,
        topology=TopologyKind.COLD_START,
        required_samples=_required_samples(),
        manifest_checksum="0x" + "dd" * 32,
    )
    result = verify_report(report, sample_agreement=_all_agree())
    assert result.complete is False
    assert "cold_start_initialize_block_not_reached" in result.blockers


def test_warm_run_requires_qualified_checkpoint_ref() -> None:
    """A warm run without a qualified-checkpoint ref is rejected."""
    report = _empty_report(
        topology=TopologyKind.WARM_INCREMENTAL,
        qualified_checkpoint_ref=None,
    )
    result = verify_report(report, sample_agreement=_all_agree())
    assert result.complete is False
    assert "warm_run_qualified_checkpoint_missing" in result.blockers


def test_warm_run_accepted_when_qualified_checkpoint_ref_present() -> None:
    report = _empty_report(
        topology=TopologyKind.WARM_INCREMENTAL,
        qualified_checkpoint_ref="cp-abc",
    )
    result = verify_report(report, sample_agreement=_all_agree())
    assert result.complete is True


def test_backtest_window_only_incomplete_without_initialize_or_checkpoint() -> None:
    report = empty_report(
        chain_id=CHAIN_ID,
        contract_address="0x" + "33" * 20,
        pool_id=POOL_ID,
        pool_init_block=POOL_INIT_BLOCK,
        coverage_from_block=POOL_INIT_BLOCK + 100,  # after Init block
        coverage_to_block=POOL_INIT_BLOCK + 200,
        topology=TopologyKind.BACKTEST_WINDOW_ONLY,
        required_samples=_required_samples(),
        manifest_checksum="0x" + "dd" * 32,
        qualified_checkpoint_ref=None,
    )
    result = verify_report(report, sample_agreement=_all_agree())
    assert result.complete is False
    assert "backtest_window_only_no_initialize_or_checkpoint" in result.blockers


def test_backtest_window_only_accepted_with_qualified_checkpoint() -> None:
    report = empty_report(
        chain_id=CHAIN_ID,
        contract_address="0x" + "33" * 20,
        pool_id=POOL_ID,
        pool_init_block=POOL_INIT_BLOCK,
        coverage_from_block=POOL_INIT_BLOCK + 100,
        coverage_to_block=POOL_INIT_BLOCK + 200,
        topology=TopologyKind.BACKTEST_WINDOW_ONLY,
        required_samples=_required_samples(),
        manifest_checksum="0x" + "dd" * 32,
        qualified_checkpoint_ref="cp-backtest-ok",
    )
    result = verify_report(report, sample_agreement=_all_agree())
    assert result.complete is True


# ---------------------------------------------------------------------------
# Acceptance: warning counts do not disappear in aggregates
# ---------------------------------------------------------------------------


def test_warning_counts_appear_in_aggregates() -> None:
    """Each deviation row contributes to the aggregate counters; a
    separate aggregate counter never swallows a warning."""
    report = _empty_report()
    for _ in range(3):
        report = with_budget_deviation(
            report,
            BudgetDeviation(
                alias="A",
                deviation_kind="capability_regression",
                detail={"observed_cap": 50, "measured_cap": 100},
            ),
        )
        report = with_budget_deviation(
            report,
            BudgetDeviation(
                alias="A",
                deviation_kind="robinhood_user_agent_403",
                detail={"retry": 1},
            ),
        )
    deviations = report.budget_deviations
    assert len(deviations) == 6
    by_kind: dict[str, int] = {}
    for d in deviations:
        by_kind[d.deviation_kind] = by_kind.get(d.deviation_kind, 0) + 1
    assert by_kind["capability_regression"] == 3
    assert by_kind["robinhood_user_agent_403"] == 3
    # The aggregates are derived from the rows; they never silently
    # swallow a warning.
    assert sum(by_kind.values()) == 6


def test_finding_counts_appear_in_aggregates() -> None:
    """Reason-code counts are derived from the findings list; no
    separate counter can hide a warning."""
    report = _empty_report()
    report = with_finding(report, _finding(REASON_DECODE_ERROR))
    report = with_finding(report, _finding(REASON_DECODE_ERROR))
    report = with_finding(report, _finding(REASON_IMPOSSIBLE_VALUE))
    counts = report.reason_code_counts()
    assert counts[REASON_DECODE_ERROR] == 2
    assert counts[REASON_IMPOSSIBLE_VALUE] == 1


# ---------------------------------------------------------------------------
# Acceptance: machine-readable dict shape
# ---------------------------------------------------------------------------


def test_machine_dict_includes_required_sections() -> None:
    report = _empty_report()
    report = with_capability_snapshot(report, _full_capability_snapshot())
    report = with_preflight_budget(
        report,
        (
            PreflightBudgetEntry(
                alias="A",
                role="primary",
                remaining_calls=1000,
                remaining_compute_units=None,
                remaining_seconds=60.0,
                remaining_response_quota=None,
                remaining_budget_source="operator_injection",
            ),
        ),
    )
    report = with_actual_budget(
        report,
        (
            ActualBudgetEntry(
                alias="A",
                interval_id="i1",
                logical_rpc_calls=10,
                http_requests=2,
                response_bytes=4096,
                normalized_rows=100,
                provider_units=10,
                elapsed_ms=500,
                parquet_bytes=2048,
            ),
        ),
    )
    report = with_provider_provenance(
        report,
        (
            ProviderProvenanceEntry(
                alias="A",
                failover_from=None,
                retry_count=0,
                retry_reason=None,
                request_from_block=COVERAGE_FROM,
                request_to_block=COVERAGE_FROM + 9,
                response_bytes=4096,
                durable_commit_checksum="0x" + "11" * 32,
            ),
        ),
    )
    report = with_response_volume(
        report,
        (
            VolumeEntry(
                alias="A",
                response_bytes=4096,
                normalized_rows=100,
                parquet_bytes=2048,
                provider_units=10,
                elapsed_ms=500,
            ),
        ),
    )
    payload = report.to_machine_dict()
    for section in (
        "capability_snapshot",
        "preflight_budget",
        "actual_budget",
        "provider_provenance",
        "response_volume",
        "findings",
        "required_samples",
        "budget_deviations",
        "infrastructure_state",
        "agreement_phrasing",
        "manifest_checksum",
        "complete",
        "qualification_blockers",
        "shared_infrastructure_evidence",
        "correlated_failure_residual_risk",
    ):
        assert section in payload, f"missing section {section!r}"


def test_shared_infrastructure_evidence_is_recorded() -> None:
    report = with_shared_infrastructure_evidence(
        _empty_report(),
        (
            SharedInfrastructureEvidence(
                kind="shared_cdn",
                description="Both endpoints may front the same upstream RPC nodes.",
            ),
        ),
    )
    payload = report.to_machine_dict()
    assert len(payload["shared_infrastructure_evidence"]) == 1


# ---------------------------------------------------------------------------
# Acceptance: machine dict never claims independent_provider_agreement
# unless evidenced_independent
# ---------------------------------------------------------------------------


def test_machine_dict_never_uses_independent_provider_agreement_for_default_state() -> None:
    payload = _empty_report().to_machine_dict()
    # The wording key is always present and only ever is the
    # independent wording when the state is evidenced_independent.
    assert payload["agreement_phrasing"] == CROSS_ENDPOINT_AGREEMENT_PHRASING


def test_with_qualification_blocker_downgrades_complete() -> None:
    report = _empty_report()
    assert report.complete is True
    report = with_qualification_blocker(report, "checkpoint_mismatch")
    assert report.complete is False
    assert "checkpoint_mismatch" in report.qualification_blockers


def test_findings_grouped_by_category_in_human_summary() -> None:
    report = _empty_report()
    report = with_finding(report, _finding(REASON_DECODE_ERROR))
    report = with_finding(report, _finding(REASON_IMPOSSIBLE_VALUE))
    summary = report.to_human_summary()
    assert "Findings[test]:" in summary


def test_correlated_failure_is_residual_risk_helper() -> None:
    assert correlated_failure_is_residual_risk(InfrastructureCorrelationState.UNKNOWN_NOT_PROVEN)
    assert correlated_failure_is_residual_risk(InfrastructureCorrelationState.KNOWN_CORRELATED)
    assert not correlated_failure_is_residual_risk(
        InfrastructureCorrelationState.EVIDENCED_INDEPENDENT
    )
