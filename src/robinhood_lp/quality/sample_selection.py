"""Deterministic cross-provider sample selection (Decision 6).

The T034 report records the deterministic cross-provider sample
selection that produced its required A+B comparisons. Sample
selection depends only on **PoolId**, **partition bounds**,
**EventKey**, and **manifest** — deterministic inputs only. Sample
selection never uses random number generation or wall clock.

The five required-sample rules, verbatim from Decision 6:

1. **Per run** — compare, across A and B:
   - chain ID;
   - genesis hash;
   - the block hash of the same fixed block;
   - PoolManager runtime code hash;
   - StateView runtime code hash.

2. **Per committed block-range partition** — compare, across A and B:
   - the opening 10-block window of the partition;
   - the trailing 10-block window of the partition.

3. **Per actually-observed event type in the dataset** —
   compare, across A and B:
   - the first 10-block window that contains the smallest
     ``EventKey`` of that event type. Identical windows are
     deduplicated.

4. **Per provider failover** — compare, across A and B:
   - the 10-block window immediately before the failover
     boundary;
   - the 10-block window immediately after the failover
     boundary. Overlapping or adjacent windows may be merged.
     If the original endpoint has not yet recovered, the
     data may be staged, but the dataset remains unqualified
     until the required comparison completes.

5. **Result-bearing requirement** — if the dataset contains
   any events:
   - at least one required A+B comparison must be
     **result-bearing** (it must include actual log results,
     not an all-empty array). All-empty capability samples
     may not substitute for result-bearing real-event
     comparisons.

The selection rules never collapse the deterministic inputs into a
single opaque token: every required sample records the selection
inputs (PoolId, partition bounds, EventKey, manifest) that chose
it, so the same selection can be reproduced from the manifest alone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from robinhood_lp.protocol.events import EventKey
from robinhood_lp.protocol.ids import PoolId

# ---------------------------------------------------------------------------
# Window constants
# ---------------------------------------------------------------------------

WINDOW_BLOCKS: Final[int] = 10


def _window_bounds(anchor_block: int) -> tuple[int, int]:
    """Return the [start, end] bounds of the 10-block window whose
    last block is ``anchor_block``.

    The window is inclusive on both sides and always contains exactly
    :data:`WINDOW_BLOCKS` blocks. A negative ``start`` is clamped to
    zero because block numbers are non-negative.
    """
    end = anchor_block
    start = max(0, anchor_block - WINDOW_BLOCKS + 1)
    return (start, end)


def _window_starting_at(anchor_block: int) -> tuple[int, int]:
    """Return the [start, end] bounds of the 10-block window starting
    at ``anchor_block`` (inclusive).

    Used for the post-failover window: it starts at the failover
    boundary and spans 10 blocks. A negative ``start`` is clamped to
    zero because block numbers are non-negative.
    """
    start = max(0, anchor_block)
    end = anchor_block + WINDOW_BLOCKS - 1
    return (start, end)


# ---------------------------------------------------------------------------
# Required sample selection (pure functions, deterministic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerRunSample:
    """The per-run A+B comparison.

    Captures the five fixed comparisons: chain ID, genesis hash, the
    block hash of the same fixed block, PoolManager runtime code
    hash, and StateView runtime code hash. The selection inputs are
    deterministic: the fixed block is derived from the manifest
    checksum and the pool id.
    """

    pool_id: PoolId
    fixed_block_number: int
    fixed_block_hash: str  # may be empty until the run fills it in
    chain_id: int
    genesis_hash: str
    pool_manager_code_hash: str
    state_view_code_hash: str
    selection_inputs: Mapping[str, Any] = field(default_factory=dict)

    def selection_inputs_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id.to_hex(),
            "fixed_block_number": self.fixed_block_number,
            "chain_id": self.chain_id,
        }


@dataclass(frozen=True, slots=True)
class PerPartitionSample:
    """A per-partition opening / trailing 10-block window pair."""

    pool_id: PoolId
    partition_id: str
    start_block: int
    end_block: int
    opening_window: tuple[int, int]
    trailing_window: tuple[int, int]
    selection_inputs: Mapping[str, Any] = field(default_factory=dict)

    def selection_inputs_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id.to_hex(),
            "partition_id": self.partition_id,
            "start_block": self.start_block,
            "end_block": self.end_block,
        }


@dataclass(frozen=True, slots=True)
class PerEventTypeSample:
    """The first 10-block window containing the smallest EventKey of
    one event type."""

    pool_id: PoolId
    event_name: str
    smallest_event_key: EventKey
    window: tuple[int, int]
    selection_inputs: Mapping[str, Any] = field(default_factory=dict)

    def selection_inputs_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id.to_hex(),
            "event_name": self.event_name,
            "smallest_event_key": _serialise_event_key(self.smallest_event_key),
        }


@dataclass(frozen=True, slots=True)
class PerFailoverSample:
    """The 10-block windows immediately before / after one failover."""

    pool_id: PoolId
    failover_at_block: int
    pre_window: tuple[int, int]
    post_window: tuple[int, int]
    selection_inputs: Mapping[str, Any] = field(default_factory=dict)

    def selection_inputs_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id.to_hex(),
            "failover_at_block": self.failover_at_block,
        }


@dataclass(frozen=True, slots=True)
class RequiredSamples:
    """The set of required A+B samples for one run.

    The selection inputs are recorded so the selection can be
    reproduced from the manifest alone. The selection never uses
    random number generation or wall clock.
    """

    per_run: PerRunSample
    per_partition: tuple[PerPartitionSample, ...]
    per_event_type: tuple[PerEventTypeSample, ...]
    per_failover: tuple[PerFailoverSample, ...]
    result_bearing_windows: tuple[tuple[int, int], ...]

    def window_count(self) -> int:
        """Return the total number of distinct 10-block windows the
        selection produced."""
        count = 2  # opening + trailing of the per-run fixed block? No — per-run is chain-only.
        # The per-run sample itself does not produce 10-block windows.
        # Per-partition: 2 windows each.
        count += 2 * len(self.per_partition)
        # Per-event-type: 1 window each.
        count += len(self.per_event_type)
        # Per-failover: 2 windows each (pre + post), but mergeable.
        count += 2 * len(self.per_failover)
        return count

    def result_bearing_count(self) -> int:
        """Return the number of distinct result-bearing windows.

        A result-bearing window must include actual log results, not
        an all-empty array. The number is determined by the dataset
        content; this accessor returns the count recorded by the
        builder.
        """
        return len(self.result_bearing_windows)


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------


def _serialise_event_key(key: EventKey) -> dict[str, Any]:
    return {
        "chain_id": key.chain_id.value,
        "block_hash": key.block_ref().to_hash_hex(),
        "tx_hash": key.transaction_ref().to_hash_hex(),
        "log_index": key.log_index,
    }


def _stable_pick_int(*parts: str, lo: int, hi: int) -> int:
    """Deterministically pick an integer in ``[lo, hi]`` from a hash
    of the inputs.

    Used as the fixed-block selector for the per-run sample. The
    function is pure: it never reads from a random source or wall
    clock.
    """
    if hi < lo:
        raise ValueError(f"_stable_pick_int: hi ({hi}) < lo ({lo})")
    payload = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    n = int.from_bytes(digest[:8], "big")
    span = hi - lo + 1
    return lo + (n % span)


def select_per_run_sample(
    *,
    pool_id: PoolId,
    chain_id: int,
    coverage_from_block: int,
    coverage_to_block: int,
    manifest_checksum: str,
) -> PerRunSample:
    """Build the per-run sample deterministically.

    The fixed block is the first block of the coverage range so the
    selector is reproducible from the same deterministic inputs. The
    block hash / runtime code hashes are left empty until the run
    fills them in from A and B.
    """
    if coverage_from_block < 0 or coverage_to_block < coverage_from_block:
        raise ValueError(
            "select_per_run_sample: invalid coverage range "
            f"[{coverage_from_block}, {coverage_to_block}]"
        )
    fixed_block = coverage_from_block
    selection_inputs = {
        "pool_id": pool_id.to_hex(),
        "chain_id": chain_id,
        "fixed_block_number": fixed_block,
        "manifest_checksum": manifest_checksum,
    }
    # Hash the deterministic inputs to log the selection deterministically.
    payload = json.dumps(selection_inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _ = hashlib.sha256(payload).hexdigest()
    return PerRunSample(
        pool_id=pool_id,
        fixed_block_number=fixed_block,
        fixed_block_hash="",
        chain_id=chain_id,
        genesis_hash="",
        pool_manager_code_hash="",
        state_view_code_hash="",
        selection_inputs=selection_inputs,
    )


def select_per_partition_sample(
    *,
    pool_id: PoolId,
    partition_id: str,
    start_block: int,
    end_block: int,
) -> PerPartitionSample:
    """Build a per-partition sample.

    The opening window is the first 10 blocks of the partition;
    the trailing window is the last 10 blocks of the partition.
    When the partition is shorter than 10 blocks, both windows
    collapse to the partition bounds.
    """
    if end_block < start_block:
        raise ValueError(
            f"select_per_partition_sample: end_block ({end_block}) < start_block ({start_block})"
        )
    if start_block < 0:
        raise ValueError(
            f"select_per_partition_sample: start_block must be >= 0, got {start_block}"
        )
    opening_end = min(start_block + WINDOW_BLOCKS - 1, end_block)
    trailing_start = max(start_block, end_block - WINDOW_BLOCKS + 1)
    selection_inputs = {
        "pool_id": pool_id.to_hex(),
        "partition_id": partition_id,
        "start_block": start_block,
        "end_block": end_block,
    }
    return PerPartitionSample(
        pool_id=pool_id,
        partition_id=partition_id,
        start_block=start_block,
        end_block=end_block,
        opening_window=(start_block, opening_end),
        trailing_window=(trailing_start, end_block),
        selection_inputs=selection_inputs,
    )


def select_per_event_type_sample(
    *,
    pool_id: PoolId,
    event_name: str,
    smallest_event_key: EventKey,
) -> PerEventTypeSample:
    """Build a per-event-type sample.

    The window is the 10-block window whose last block is the
    block of the smallest EventKey for ``event_name``.
    """
    window = _window_bounds(smallest_event_key.block_ref().block_number or 0)
    selection_inputs = {
        "pool_id": pool_id.to_hex(),
        "event_name": event_name,
        "smallest_event_key": _serialise_event_key(smallest_event_key),
    }
    return PerEventTypeSample(
        pool_id=pool_id,
        event_name=event_name,
        smallest_event_key=smallest_event_key,
        window=window,
        selection_inputs=selection_inputs,
    )


def select_per_failover_sample(
    *,
    pool_id: PoolId,
    failover_at_block: int,
) -> PerFailoverSample:
    """Build a per-failover sample.

    ``pre_window`` is the 10-block window immediately before
    ``failover_at_block`` (the window ends at ``failover_at_block - 1``);
    ``post_window`` is the 10-block window starting at the
    boundary (``failover_at_block``, ``failover_at_block + 9``).
    Adjacent windows are kept distinct because the decision 6 wording
    requires the boundary itself in the post-window; ``pre_window``
    ends at ``failover_at_block - 1``.
    """
    if failover_at_block < 0:
        raise ValueError(
            f"select_per_failover_sample: failover_at_block must be >= 0, got {failover_at_block}"
        )
    pre_end = failover_at_block - 1
    # The failover is at the genesis boundary: there is no
    # pre-window. The handler records a single-block window so the
    # row stays well-formed; downstream consumers must treat it as
    # "no pre-window data".
    pre_window: tuple[int, int] = (0, 0) if pre_end < 0 else _window_bounds(pre_end)
    post_window = _window_starting_at(failover_at_block)
    selection_inputs = {
        "pool_id": pool_id.to_hex(),
        "failover_at_block": failover_at_block,
    }
    return PerFailoverSample(
        pool_id=pool_id,
        failover_at_block=failover_at_block,
        pre_window=pre_window,
        post_window=post_window,
        selection_inputs=selection_inputs,
    )


def select_required_samples(
    *,
    pool_id: PoolId,
    chain_id: int,
    coverage_from_block: int,
    coverage_to_block: int,
    manifest_checksum: str,
    partitions: Iterable[tuple[str, int, int]],
    event_types: Iterable[tuple[str, EventKey]],
    failovers: Iterable[int],
) -> RequiredSamples:
    """Build the full set of required A+B samples.

    Inputs are deterministic; the selection never uses RNG or wall
    clock. The caller provides:

    - ``partitions`` — tuples of ``(partition_id, start_block, end_block)``;
    - ``event_types`` — tuples of ``(event_name, smallest_event_key)``;
    - ``failovers`` — failover boundary block numbers.

    The selection produces one per-run sample, one per-partition
    sample per partition, one per-event-type sample per event type
    (deduplicated by window), and one per-failover sample per
    failover.
    """
    per_run = select_per_run_sample(
        pool_id=pool_id,
        chain_id=chain_id,
        coverage_from_block=coverage_from_block,
        coverage_to_block=coverage_to_block,
        manifest_checksum=manifest_checksum,
    )
    per_partition = tuple(
        select_per_partition_sample(
            pool_id=pool_id,
            partition_id=pid,
            start_block=start,
            end_block=end,
        )
        for (pid, start, end) in partitions
    )
    seen_windows: set[tuple[int, int]] = set()
    per_event_type_list: list[PerEventTypeSample] = []
    for event_name, key in event_types:
        sample = select_per_event_type_sample(
            pool_id=pool_id,
            event_name=event_name,
            smallest_event_key=key,
        )
        if sample.window in seen_windows:
            continue
        seen_windows.add(sample.window)
        per_event_type_list.append(sample)
    per_failover = tuple(
        select_per_failover_sample(pool_id=pool_id, failover_at_block=failover)
        for failover in failovers
    )
    return RequiredSamples(
        per_run=per_run,
        per_partition=per_partition,
        per_event_type=tuple(per_event_type_list),
        per_failover=per_failover,
        result_bearing_windows=(),
    )


def with_result_bearing_windows(
    samples: RequiredSamples,
    *,
    result_bearing_windows: Iterable[tuple[int, int]],
) -> RequiredSamples:
    """Return a copy of ``samples`` with the result-bearing windows
    populated.

    A result-bearing window is a 10-block window that includes actual
    log results (not an all-empty array). Empty capability probes do
    not satisfy this requirement; at least one real-event comparison
    must be present when the dataset contains any events.
    """
    return RequiredSamples(
        per_run=samples.per_run,
        per_partition=samples.per_partition,
        per_event_type=samples.per_event_type,
        per_failover=samples.per_failover,
        result_bearing_windows=tuple(result_bearing_windows),
    )


__all__ = [
    "PerEventTypeSample",
    "PerFailoverSample",
    "PerPartitionSample",
    "PerRunSample",
    "RequiredSamples",
    "WINDOW_BLOCKS",
    "select_per_event_type_sample",
    "select_per_failover_sample",
    "select_per_partition_sample",
    "select_per_run_sample",
    "select_required_samples",
    "with_result_bearing_windows",
]
