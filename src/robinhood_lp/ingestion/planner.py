"""Range planner + scan filter + sub-range computation (T032).

The range planner decides the topology for the run (cold-start
history or warm incremental ingestion), picks the single bounded
``eth_getLogs`` filter (one topic0 OR query plus topic1 fixed to the
pool id), and computes the ordered list of sub-ranges the runner
walks.

A cold-start run scans from the pool's registered ``Initialize`` block
(the on-chain block at which that ``PoolKey`` was first created on
Robinhood Chain mainnet) to the requested end block. A shorter
backtest start does **not** truncate the reconstruction prefix.

A warm run only covers the missing suffix after the qualified local
checkpoint and never re-ingests an already qualified prefix. The
planner refuses to start a warm run when the existing checkpoint's
manifest, block hash, schema/decode version, or coverage prefix does
not match the requested pool and range.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from robinhood_lp.ingestion.errors import (
    TOPOLOGY_COLD_START,
    TOPOLOGY_WARM_INCREMENTAL,
    VALID_TOPOLOGIES,
    CheckpointMismatchError,
)
from robinhood_lp.protocol.abi_artifacts import EVENT_TOPICS
from robinhood_lp.protocol.ids import Address, PoolId

# ---------------------------------------------------------------------------
# The four required topic0s + the inherited ProtocolFeeUpdated topic0.
# Order is fixed so JSON-serialised manifests are byte-stable across
# runs (the JSON encoder serialises a list in insertion order).
# ---------------------------------------------------------------------------

_TOPIC0_NAMES: Final[tuple[str, ...]] = (
    "Initialize",
    "ModifyLiquidity",
    "Swap",
    "Donate",
    "ProtocolFeeUpdated",
)

#: The fixed topic0 OR filter the planner emits. A single
#: ``eth_getLogs`` request with these topic0s covers every V4 event
#: the framework consumes; the system must not multiply a scan into
#: one request stream per event type when this bounded OR filter
#: suffices.
REQUIRED_TOPIC0_NAMES: Final[tuple[str, ...]] = _TOPIC0_NAMES


def build_topic0_filter() -> list[str]:
    """Return the canonical topic0 OR filter (lowercase 0x-hex)."""
    return ["0x" + EVENT_TOPICS[name].hex() for name in _TOPIC0_NAMES]


def build_pool_topic_filter(pool_id: PoolId) -> list[list[str] | str]:
    """Return the topic filter for ``eth_getLogs`` covering one pool.

    The shape is ``[[t0_initialize, t0_modify, t0_swap, t0_donate,
    t0_pfu], "0x" + pool_id_hex_32]`` — topic0 OR, topic1 fixed.
    """
    return [build_topic0_filter(), "0x" + pool_id.value.to_bytes(32, "big").hex()]


# ---------------------------------------------------------------------------
# Sub-range computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedSubRange:
    """One sub-range the runner must cover.

    ``from_block`` and ``to_block`` are inclusive on both ends. The
    runner may further split this sub-range when the endpoint's
    measured capability rejects a too-large window, but never below
    one block at a time.
    """

    from_block: int
    to_block: int

    def __post_init__(self) -> None:
        if self.from_block < 0:
            raise ValueError(f"from_block: must be >= 0, got {self.from_block}")
        if self.to_block < self.from_block:
            raise ValueError(f"sub-range: from_block {self.from_block} > to_block {self.to_block}")

    @property
    def is_empty(self) -> bool:
        return self.to_block < self.from_block

    @property
    def width(self) -> int:
        return self.to_block - self.from_block + 1


def split_sub_range(
    sub: PlannedSubRange, *, max_blocks: int
) -> tuple[PlannedSubRange, PlannedSubRange | None]:
    """Halve ``sub`` into two contiguous sub-ranges.

    Used by the adaptive splitter. When ``sub`` is already at or
    below ``max_blocks`` the right-hand side is ``None`` (the
    caller surfaces that as ``single_block_overflow``).
    """
    if max_blocks <= 0:
        raise ValueError(f"max_blocks: must be positive, got {max_blocks}")
    width = sub.width
    if width <= max_blocks:
        return sub, None
    # Round the left half up so a width of 2 always produces a
    # non-empty right half (the split is "left then right").
    mid = sub.from_block + (width // 2) - 1
    if mid < sub.from_block:
        mid = sub.from_block
    if mid >= sub.to_block:
        mid = sub.to_block - 1
    return PlannedSubRange(sub.from_block, mid), PlannedSubRange(mid + 1, sub.to_block)


# ---------------------------------------------------------------------------
# Existing checkpoint shape (the planner validates before warm runs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExistingCheckpoint:
    """The relevant subset of a stored durable checkpoint the planner
    must validate before a warm run.

    ``qualified_end_block_hash`` is the block hash of the highest
    covered block at the time the checkpoint was written. A warm run
    that requests a different prefix (different ``requested_start_block``
    or different ``pool_init_block``) is rejected; a warm run that
    re-requests the same pool and a later end block is accepted.
    """

    chain_id: int
    contract_address: str
    pool_id: str
    qualified_start_block: int
    qualified_end_block: int
    qualified_end_block_hash: str
    schema_version: int
    decode_version: int
    capability_snapshot_id: str
    manifest_checksum: str
    topology: str
    pool_init_block: int


# ---------------------------------------------------------------------------
# Topology + sub-range computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedRun:
    """The plan the runner executes.

    ``topology`` is ``cold_start`` or ``warm_incremental``.
    ``coverage_from_block`` is the first block the runner must cover.
    ``coverage_to_block`` is the last block the runner must cover.
    ``sub_ranges`` is the ordered list of sub-ranges the runner
    walks. The ``existing_checkpoint`` is non-None only when
    ``topology == warm_incremental``.
    """

    topology: str
    pool_init_block: int
    requested_start_block: int
    requested_end_block: int
    coverage_from_block: int
    coverage_to_block: int
    sub_ranges: tuple[PlannedSubRange, ...]
    existing_checkpoint: ExistingCheckpoint | None

    def __post_init__(self) -> None:
        if self.topology not in VALID_TOPOLOGIES:
            raise ValueError(
                f"PlannedRun.topology: must be one of {sorted(VALID_TOPOLOGIES)!r}, "
                f"got {self.topology!r}"
            )
        if self.coverage_from_block > self.coverage_to_block and self.sub_ranges:
            raise ValueError(
                f"PlannedRun: coverage_from_block {self.coverage_from_block} > "
                f"coverage_to_block {self.coverage_to_block}"
            )
        if self.topology == TOPOLOGY_COLD_START and self.existing_checkpoint is not None:
            raise ValueError(
                "PlannedRun: cold_start topology must not carry an existing checkpoint"
            )
        if self.topology == TOPOLOGY_WARM_INCREMENTAL and self.existing_checkpoint is None:
            raise ValueError(
                "PlannedRun: warm_incremental topology requires an existing checkpoint"
            )


# ---------------------------------------------------------------------------
# The planner itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RangePlannerInputs:
    """Inputs the planner needs.

    ``pool_init_block`` is the on-chain block at which the
    ``PoolKey`` was first created on Robinhood Chain mainnet.
    ``requested_start_block`` is the (possibly shorter) backtest
    start the operator asked for. ``requested_end_block`` is the
    latest block the operator asked for. ``existing_checkpoint`` is
    the previously qualified local checkpoint when one exists.
    """

    chain_id: int
    contract_address: Address
    pool_id: PoolId
    pool_init_block: int
    requested_start_block: int
    requested_end_block: int
    existing_checkpoint: ExistingCheckpoint | None = None

    def __post_init__(self) -> None:
        if self.pool_init_block < 0:
            raise ValueError(f"pool_init_block: must be >= 0, got {self.pool_init_block}")
        if self.requested_start_block < 0:
            raise ValueError(
                f"requested_start_block: must be >= 0, got {self.requested_start_block}"
            )
        if self.requested_end_block < self.requested_start_block:
            raise ValueError(
                f"requested_end_block: must be >= requested_start_block, "
                f"got {self.requested_end_block} < {self.requested_start_block}"
            )
        # The pool_init_block may be greater than the requested
        # end block when the operator requests a backtest window
        # that ends before the pool was created on-chain. The cold
        # start run collapses to an empty coverage range in that
        # case (the planner surfaces this as ``coverage_from >
        # coverage_to`` with no sub-ranges and ``complete=True``).
        # This is *not* an input validation failure: the operator
        # is allowed to ask for "nothing", and the runner reports
        # the empty backtest window faithfully.


class RangePlanner:
    """Compute the run topology from pool + requested range +
    (optional) existing checkpoint.

    The planner is a pure function: it does not perform any I/O. The
    runner calls :meth:`plan` once at the start of the run, then
    iterates ``PlannedRun.sub_ranges`` in order.
    """

    def __init__(
        self,
        *,
        max_blocks_per_sub_range: int = 10_000,
    ) -> None:
        if max_blocks_per_sub_range <= 0:
            raise ValueError(
                f"max_blocks_per_sub_range: must be positive, got {max_blocks_per_sub_range}"
            )
        self._max_blocks = int(max_blocks_per_sub_range)

    # ----- public API --------------------------------------------------

    @property
    def max_blocks_per_sub_range(self) -> int:
        return self._max_blocks

    def plan(self, inputs: RangePlannerInputs) -> PlannedRun:
        """Compute the topology + sub-ranges for the run."""
        if inputs.existing_checkpoint is None:
            return self._plan_cold_start(inputs)
        return self._plan_warm(inputs)

    # ----- cold start --------------------------------------------------

    def _plan_cold_start(self, inputs: RangePlannerInputs) -> PlannedRun:
        coverage_from = inputs.pool_init_block
        coverage_to = inputs.requested_end_block
        if coverage_from > coverage_to:
            # Defensive: an empty range collapses to no sub-ranges and
            # complete=True (the run scanned everything requested and
            # the requested range was empty).
            return PlannedRun(
                topology=TOPOLOGY_COLD_START,
                pool_init_block=inputs.pool_init_block,
                requested_start_block=inputs.requested_start_block,
                requested_end_block=inputs.requested_end_block,
                coverage_from_block=coverage_from,
                coverage_to_block=coverage_to,
                sub_ranges=(),
                existing_checkpoint=None,
            )
        return PlannedRun(
            topology=TOPOLOGY_COLD_START,
            pool_init_block=inputs.pool_init_block,
            requested_start_block=inputs.requested_start_block,
            requested_end_block=inputs.requested_end_block,
            coverage_from_block=coverage_from,
            coverage_to_block=coverage_to,
            sub_ranges=tuple(_split_into_windows(coverage_from, coverage_to, self._max_blocks)),
            existing_checkpoint=None,
        )

    # ----- warm --------------------------------------------------------

    def _plan_warm(self, inputs: RangePlannerInputs) -> PlannedRun:
        cp = inputs.existing_checkpoint
        assert cp is not None
        self._validate_warm_checkpoint(inputs, cp)
        coverage_from = cp.qualified_end_block + 1
        coverage_to = inputs.requested_end_block
        if coverage_from > coverage_to:
            return PlannedRun(
                topology=TOPOLOGY_WARM_INCREMENTAL,
                pool_init_block=inputs.pool_init_block,
                requested_start_block=inputs.requested_start_block,
                requested_end_block=inputs.requested_end_block,
                coverage_from_block=coverage_from,
                coverage_to_block=coverage_to,
                sub_ranges=(),
                existing_checkpoint=cp,
            )
        return PlannedRun(
            topology=TOPOLOGY_WARM_INCREMENTAL,
            pool_init_block=inputs.pool_init_block,
            requested_start_block=inputs.requested_start_block,
            requested_end_block=inputs.requested_end_block,
            coverage_from_block=coverage_from,
            coverage_to_block=coverage_to,
            sub_ranges=tuple(_split_into_windows(coverage_from, coverage_to, self._max_blocks)),
            existing_checkpoint=cp,
        )

    # ----- checkpoint validation --------------------------------------

    @staticmethod
    def _validate_warm_checkpoint(inputs: RangePlannerInputs, cp: ExistingCheckpoint) -> None:
        """Reject the checkpoint when any of the locked fields disagree.

        The validation surface (manifest, block hash, schema/decode
        version, coverage prefix) is fixed by the T032 contract. Any
        disagreement raises :class:`CheckpointMismatchError` and the
        runner refuses to advance.
        """
        if cp.chain_id != inputs.chain_id:
            raise CheckpointMismatchError(
                f"warm checkpoint chain_id {cp.chain_id} does not match "
                f"requested chain_id {inputs.chain_id}"
            )
        if cp.contract_address.lower().removeprefix(
            "0x"
        ) != inputs.contract_address.to_hex().lower().removeprefix("0x"):
            raise CheckpointMismatchError(
                f"warm checkpoint contract_address {cp.contract_address} does not "
                f"match requested {inputs.contract_address.to_hex()}"
            )
        if cp.pool_id.lower() != inputs.pool_id.to_hex().lower():
            raise CheckpointMismatchError(
                f"warm checkpoint pool_id {cp.pool_id} does not match "
                f"requested {inputs.pool_id.to_hex()}"
            )
        if cp.pool_init_block != inputs.pool_init_block:
            raise CheckpointMismatchError(
                f"warm checkpoint pool_init_block {cp.pool_init_block} does not "
                f"match requested pool_init_block {inputs.pool_init_block}"
            )
        if inputs.requested_start_block < cp.qualified_start_block:
            raise CheckpointMismatchError(
                f"warm checkpoint qualified_start_block {cp.qualified_start_block} > "
                f"requested_start_block {inputs.requested_start_block}; "
                f"a warm run may not request a prefix earlier than the qualified prefix"
            )
        # The qualified prefix is implicit: the operator cannot ask
        # for a prefix earlier than the checkpoint's qualified_start
        # because reconstruction must reach the pool's Initialize
        # block. A request that demands a start later than the
        # checkpoint's qualified start but earlier than the
        # checkpoint's qualified end is a coverage gap the warm run
        # cannot bridge — the prefix is fixed by the cold-start scan.
        if inputs.requested_start_block > cp.qualified_end_block + 1:
            raise CheckpointMismatchError(
                f"warm checkpoint qualified_end_block {cp.qualified_end_block} < "
                f"requested_start_block - 1 {inputs.requested_start_block - 1}; "
                f"a warm run may not skip blocks inside the qualified prefix"
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_into_windows(
    coverage_from: int, coverage_to: int, max_blocks: int
) -> list[PlannedSubRange]:
    """Greedy contiguous windows covering ``[coverage_from, coverage_to]``."""
    out: list[PlannedSubRange] = []
    cursor = coverage_from
    while cursor <= coverage_to:
        end = min(cursor + max_blocks - 1, coverage_to)
        out.append(PlannedSubRange(cursor, end))
        cursor = end + 1
    return out


__all__ = [
    "ExistingCheckpoint",
    "PlannedRun",
    "PlannedSubRange",
    "RangePlanner",
    "RangePlannerInputs",
    "REQUIRED_TOPIC0_NAMES",
    "build_pool_topic_filter",
    "build_topic0_filter",
    "split_sub_range",
]
