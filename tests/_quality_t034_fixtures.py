"""Shared fixtures for the T034 quality report tests."""

from __future__ import annotations

from robinhood_lp.protocol.events import EventKey
from robinhood_lp.protocol.ids import ChainId, PoolId
from robinhood_lp.quality import (
    CapabilityEntry,
    QualityReport,
    RequiredSamples,
    TopologyKind,
    empty_report,
    select_required_samples,
    with_result_bearing_windows,
)

CHAIN_ID = 4663
POOL_ID = PoolId.from_hex("0x" + "11" * 32)
POOL_INIT_BLOCK = 1000
COVERAGE_FROM = POOL_INIT_BLOCK
COVERAGE_TO = POOL_INIT_BLOCK + 100


def make_required_samples(*, include_events: bool = True) -> RequiredSamples:
    """Return a baseline :class:`RequiredSamples` for tests."""
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
        partitions=(("p1", COVERAGE_FROM, COVERAGE_FROM + 9),),
        event_types=(("Initialize", key),) if include_events else (),
        failovers=(COVERAGE_FROM + 50,),
    )
    if include_events:
        samples = with_result_bearing_windows(
            samples, result_bearing_windows=((COVERAGE_FROM, COVERAGE_FROM + 9),)
        )
    return samples


def make_quality_report(
    *,
    topology: TopologyKind = TopologyKind.COLD_START,
    qualified_checkpoint_ref: str | None = None,
) -> QualityReport:
    return empty_report(
        chain_id=CHAIN_ID,
        contract_address="0x" + "33" * 20,
        pool_id=POOL_ID,
        pool_init_block=POOL_INIT_BLOCK,
        coverage_from_block=COVERAGE_FROM,
        coverage_to_block=COVERAGE_TO,
        topology=topology,
        required_samples=make_required_samples(),
        manifest_checksum="0x" + "dd" * 32,
        qualified_checkpoint_ref=qualified_checkpoint_ref,
    )


def make_capability_snapshot() -> tuple[CapabilityEntry, ...]:
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


__all__ = [
    "CHAIN_ID",
    "COVERAGE_FROM",
    "COVERAGE_TO",
    "POOL_ID",
    "POOL_INIT_BLOCK",
    "make_capability_snapshot",
    "make_quality_report",
    "make_required_samples",
]
