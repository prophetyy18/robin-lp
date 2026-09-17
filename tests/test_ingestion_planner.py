"""Tests for the range planner (T032).

Covers the cold-start vs warm-incremental topology, the one
topic0 OR filter, the topic1 pool-id pin, the sub-range splitting,
and the warm checkpoint rejection paths.
"""

from __future__ import annotations

import pytest

from _ingestion_t032_fixtures import (
    CHAIN_ID,
    CONTRACT,
    POOL_ID,
    POOL_INIT_BLOCK,
)
from robinhood_lp.ingestion.errors import (
    TOPOLOGY_COLD_START,
    TOPOLOGY_WARM_INCREMENTAL,
    CheckpointMismatchError,
)
from robinhood_lp.ingestion.planner import (
    REQUIRED_TOPIC0_NAMES,
    ExistingCheckpoint,
    PlannedSubRange,
    RangePlanner,
    RangePlannerInputs,
    build_pool_topic_filter,
    build_topic0_filter,
    split_sub_range,
)
from robinhood_lp.protocol.abi_artifacts import EVENT_TOPICS

# ---------------------------------------------------------------------------
# Scan filter
# ---------------------------------------------------------------------------


def test_topic0_filter_covers_every_required_event() -> None:
    """T032 contract: one pool-filtered topic0 OR query covering
    Initialize / ModifyLiquidity / Swap / Donate + the inherited
    ProtocolFeeUpdated event."""
    filter0 = build_topic0_filter()
    assert set(REQUIRED_TOPIC0_NAMES) == {
        "Initialize",
        "ModifyLiquidity",
        "Swap",
        "Donate",
        "ProtocolFeeUpdated",
    }
    expected = {"0x" + EVENT_TOPICS[name].hex() for name in REQUIRED_TOPIC0_NAMES}
    assert set(filter0) == expected
    assert len(filter0) == 5


def test_pool_topic_filter_pins_topic1_to_pool_id() -> None:
    """The topic filter is ``[OR(topic0s), pool_id]`` so one
    ``eth_getLogs`` request covers the pool."""
    topic_filter = build_pool_topic_filter(POOL_ID)
    assert isinstance(topic_filter, list)
    assert len(topic_filter) == 2
    or_list, topic1 = topic_filter
    assert isinstance(or_list, list)
    assert isinstance(topic1, str)
    assert topic1.startswith("0x")
    # topic1 is exactly the 32-byte pool id.
    assert int(topic1, 16) == POOL_ID.value


# ---------------------------------------------------------------------------
# Sub-range splitter
# ---------------------------------------------------------------------------


def test_split_sub_range_halves_a_wide_range() -> None:
    sub = PlannedSubRange(100, 199)
    left, right = split_sub_range(sub, max_blocks=50)
    assert right is not None
    assert left.to_block - left.from_block + 1 == 50
    assert right.from_block == left.to_block + 1
    assert right.to_block == 199


def test_split_sub_range_collapses_on_single_block() -> None:
    sub = PlannedSubRange(100, 100)
    left, right = split_sub_range(sub, max_blocks=1)
    assert left == sub
    assert right is None


def test_split_sub_range_rejects_non_positive_cap() -> None:
    with pytest.raises(ValueError):
        split_sub_range(PlannedSubRange(0, 0), max_blocks=0)


# ---------------------------------------------------------------------------
# Cold-start topology
# ---------------------------------------------------------------------------


def test_cold_start_planner_covers_pool_init_block() -> None:
    """The cold-start scan starts at the pool's registered
    Initialize block, not at the requested (possibly shorter)
    backtest start."""
    planner = RangePlanner(max_blocks_per_sub_range=10)
    plan = planner.plan(
        RangePlannerInputs(
            chain_id=CHAIN_ID,
            contract_address=CONTRACT,
            pool_id=POOL_ID,
            pool_init_block=POOL_INIT_BLOCK,
            requested_start_block=POOL_INIT_BLOCK + 5_000,
            requested_end_block=POOL_INIT_BLOCK + 10_000,
        )
    )
    assert plan.topology == TOPOLOGY_COLD_START
    # coverage_from is the pool init block, not the shorter
    # backtest start.
    assert plan.coverage_from_block == POOL_INIT_BLOCK
    assert plan.coverage_to_block == POOL_INIT_BLOCK + 10_000
    # Sub-ranges cover the entire pool-init-to-end window.
    expected = [
        (POOL_INIT_BLOCK, POOL_INIT_BLOCK + 9),
        (POOL_INIT_BLOCK + 10, POOL_INIT_BLOCK + 19),
    ]
    actual = [(s.from_block, s.to_block) for s in plan.sub_ranges[:2]]
    assert expected[0] == actual[0]
    assert expected[1] == actual[1]


def test_cold_start_with_shorter_backtest_does_not_truncate_prefix() -> None:
    """A shorter requested start MUST NOT truncate the
    reconstruction prefix (T032 contract)."""
    planner = RangePlanner(max_blocks_per_sub_range=10)
    plan = planner.plan(
        RangePlannerInputs(
            chain_id=CHAIN_ID,
            contract_address=CONTRACT,
            pool_id=POOL_ID,
            pool_init_block=POOL_INIT_BLOCK,
            requested_start_block=POOL_INIT_BLOCK + 5_000,
            requested_end_block=POOL_INIT_BLOCK + 10_000,
        )
    )
    assert plan.coverage_from_block == POOL_INIT_BLOCK


def test_cold_start_with_empty_range_collapses_to_complete() -> None:
    """A requested range that ends before the pool's registered
    Initialize block collapses to no sub-ranges and ``complete=True``.
    """
    planner = RangePlanner(max_blocks_per_sub_range=10)
    plan = planner.plan(
        RangePlannerInputs(
            chain_id=CHAIN_ID,
            contract_address=CONTRACT,
            pool_id=POOL_ID,
            pool_init_block=POOL_INIT_BLOCK,
            requested_start_block=POOL_INIT_BLOCK - 1_000,
            requested_end_block=POOL_INIT_BLOCK - 500,
        )
    )
    assert plan.sub_ranges == ()
    assert plan.coverage_from_block == POOL_INIT_BLOCK
    assert plan.coverage_to_block == POOL_INIT_BLOCK - 500


# ---------------------------------------------------------------------------
# Warm topology
# ---------------------------------------------------------------------------


def test_warm_planner_covers_only_the_missing_suffix() -> None:
    planner = RangePlanner(max_blocks_per_sub_range=10)
    cp = ExistingCheckpoint(
        chain_id=CHAIN_ID,
        contract_address=CONTRACT.to_hex().removeprefix("0x").lower(),
        pool_id=POOL_ID.to_hex(),
        qualified_start_block=POOL_INIT_BLOCK,
        qualified_end_block=POOL_INIT_BLOCK + 1_000,
        qualified_end_block_hash="0x" + "ab" * 32,
        schema_version=2,
        decode_version=2,
        capability_snapshot_id="snap-1",
        manifest_checksum="0x" + "cd" * 32,
        topology=TOPOLOGY_COLD_START,
        pool_init_block=POOL_INIT_BLOCK,
    )
    plan = planner.plan(
        RangePlannerInputs(
            chain_id=CHAIN_ID,
            contract_address=CONTRACT,
            pool_id=POOL_ID,
            pool_init_block=POOL_INIT_BLOCK,
            requested_start_block=POOL_INIT_BLOCK,
            requested_end_block=POOL_INIT_BLOCK + 1_500,
            existing_checkpoint=cp,
            expected_manifest_checksum=cp.manifest_checksum,
        )
    )
    assert plan.topology == TOPOLOGY_WARM_INCREMENTAL
    # Warm covers the missing suffix only (the prefix is already
    # qualified).
    assert plan.coverage_from_block == POOL_INIT_BLOCK + 1_001
    assert plan.coverage_to_block == POOL_INIT_BLOCK + 1_500
    assert plan.existing_checkpoint == cp


def test_warm_planner_with_no_missing_suffix_is_empty() -> None:
    planner = RangePlanner(max_blocks_per_sub_range=10)
    cp = ExistingCheckpoint(
        chain_id=CHAIN_ID,
        contract_address=CONTRACT.to_hex().removeprefix("0x").lower(),
        pool_id=POOL_ID.to_hex(),
        qualified_start_block=POOL_INIT_BLOCK,
        qualified_end_block=POOL_INIT_BLOCK + 5_000,
        qualified_end_block_hash="0x" + "ab" * 32,
        schema_version=2,
        decode_version=2,
        capability_snapshot_id="snap-1",
        manifest_checksum="0x" + "cd" * 32,
        topology=TOPOLOGY_COLD_START,
        pool_init_block=POOL_INIT_BLOCK,
    )
    plan = planner.plan(
        RangePlannerInputs(
            chain_id=CHAIN_ID,
            contract_address=CONTRACT,
            pool_id=POOL_ID,
            pool_init_block=POOL_INIT_BLOCK,
            requested_start_block=POOL_INIT_BLOCK,
            requested_end_block=POOL_INIT_BLOCK + 4_000,  # entirely inside prefix
            existing_checkpoint=cp,
            expected_manifest_checksum=cp.manifest_checksum,
        )
    )
    assert plan.sub_ranges == ()


# ---------------------------------------------------------------------------
# Warm checkpoint rejection
# ---------------------------------------------------------------------------


def _existing_checkpoint(**overrides: object) -> ExistingCheckpoint:
    base: dict[str, object] = dict(
        chain_id=CHAIN_ID,
        contract_address=CONTRACT.to_hex().removeprefix("0x").lower(),
        pool_id=POOL_ID.to_hex(),
        qualified_start_block=POOL_INIT_BLOCK,
        qualified_end_block=POOL_INIT_BLOCK + 1_000,
        qualified_end_block_hash="0x" + "ab" * 32,
        schema_version=2,
        decode_version=2,
        capability_snapshot_id="snap-1",
        manifest_checksum="0x" + "cd" * 32,
        topology=TOPOLOGY_COLD_START,
        pool_init_block=POOL_INIT_BLOCK,
    )
    base.update(overrides)
    return ExistingCheckpoint(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value",
    [
        ("chain_id", 9999),
        ("contract_address", "ff" * 20),
        ("pool_id", "0x" + "ee" * 32),
        ("pool_init_block", POOL_INIT_BLOCK + 1),
    ],
)
def test_warm_planner_rejects_mismatched_pool_or_chain(field: str, value: object) -> None:
    planner = RangePlanner()
    cp = _existing_checkpoint(**{field: value})
    with pytest.raises(CheckpointMismatchError):
        planner.plan(
            RangePlannerInputs(
                chain_id=CHAIN_ID,
                contract_address=CONTRACT,
                pool_id=POOL_ID,
                pool_init_block=POOL_INIT_BLOCK,
                requested_start_block=POOL_INIT_BLOCK,
                requested_end_block=POOL_INIT_BLOCK + 2_000,
                existing_checkpoint=cp,
                expected_manifest_checksum=cp.manifest_checksum,
            )
        )


def test_warm_planner_rejects_requested_start_inside_qualified_prefix() -> None:
    """A warm run may not request a start earlier than the qualified
    prefix (the prefix is already qualified)."""
    planner = RangePlanner()
    cp = _existing_checkpoint(
        qualified_start_block=POOL_INIT_BLOCK + 500,
        qualified_end_block=POOL_INIT_BLOCK + 1_000,
    )
    with pytest.raises(CheckpointMismatchError):
        planner.plan(
            RangePlannerInputs(
                chain_id=CHAIN_ID,
                contract_address=CONTRACT,
                pool_id=POOL_ID,
                pool_init_block=POOL_INIT_BLOCK,
                # requested start is before qualified_start
                requested_start_block=POOL_INIT_BLOCK + 100,
                requested_end_block=POOL_INIT_BLOCK + 2_000,
                existing_checkpoint=cp,
                expected_manifest_checksum=cp.manifest_checksum,
            )
        )


def test_warm_planner_rejects_requested_start_skipping_into_prefix() -> None:
    """A warm run may not skip blocks inside the qualified prefix."""
    planner = RangePlanner()
    cp = _existing_checkpoint(
        qualified_start_block=POOL_INIT_BLOCK,
        qualified_end_block=POOL_INIT_BLOCK + 1_000,
    )
    with pytest.raises(CheckpointMismatchError):
        planner.plan(
            RangePlannerInputs(
                chain_id=CHAIN_ID,
                contract_address=CONTRACT,
                pool_id=POOL_ID,
                pool_init_block=POOL_INIT_BLOCK,
                # requested start is past qualified end + 1 (skipping blocks)
                requested_start_block=POOL_INIT_BLOCK + 1_500,
                requested_end_block=POOL_INIT_BLOCK + 2_000,
                existing_checkpoint=cp,
                expected_manifest_checksum=cp.manifest_checksum,
            )
        )


# ---------------------------------------------------------------------------
# Inputs validation
# ---------------------------------------------------------------------------


def test_range_planner_inputs_rejects_negative_block() -> None:
    with pytest.raises(ValueError):
        RangePlannerInputs(
            chain_id=CHAIN_ID,
            contract_address=CONTRACT,
            pool_id=POOL_ID,
            pool_init_block=-1,
            requested_start_block=0,
            requested_end_block=10,
        )


def test_range_planner_inputs_rejects_end_before_start() -> None:
    with pytest.raises(ValueError):
        RangePlannerInputs(
            chain_id=CHAIN_ID,
            contract_address=CONTRACT,
            pool_id=POOL_ID,
            pool_init_block=0,
            requested_start_block=10,
            requested_end_block=5,
        )


def test_range_planner_rejects_non_positive_window() -> None:
    with pytest.raises(ValueError):
        RangePlanner(max_blocks_per_sub_range=0)
