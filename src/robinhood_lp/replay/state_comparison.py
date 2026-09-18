"""Block-pinned StateView comparison runner (T042).

The T042 contract closes the silent-replay-error defect by validating
replay correctness against an **independent** on-chain state source:
the V4 ``StateView`` view-method reads issued to an endpoint that can
serve historical state at the pinned depth. The replay's deterministic
state at a given block must agree byte-for-byte with the StateView
read at that block for the integer fields each StateView method
returns (``sqrtPriceX96``, ``tick``, ``lpFee``, ``protocolFee``,
``liquidity``, bitmap word, ``liquidityGross``, ``liquidityNet``,
fee-growth globals/inside/outside).

Comparison scope, routing and bounds (T042 contract):

1. **Pool set.** The "heterogeneous pools" are the pools T038
   qualifies — the re-acquired ZZZ/USDG reference pool and the
   Owner-pinned second pool. Each is validated against its own data
   root and its own replay output, per pool. One pool's agreement is
   never presented as the other pool's result; the per-pool report
   carries the pool alias the dataset T038 qualified.
2. **Historical-state routing.** Every block-pinned StateView read is
   issued to an endpoint that can serve historical state at the
   pinned depth. An endpoint that cannot serve the pinned depth
   produces a **blocked check** that records the T034 reason code
   (``cross_endpoint_sample_missing`` is the precedent the P03
   qualification pipeline established), the height at which the read
   was attempted, the StateView method, and the endpoint alias. The
   runner never substitutes ``latest``, an unpinned height, another
   height, or a post-hoc re-read of a different endpoint.
3. **Recorded bounds.** Before the runner issues any read it records
   the per-pool bounds: number of heights compared, per-height read
   list (which StateView methods are read at each height), and the
   logical-call and compute-unit budget ceiling. The recorded
   heights and read list are the acceptance surface: a run that
   would exceed the ceiling stops and reports the exhaustion rather
   than silently reducing coverage.
4. **Input reconciliation.** The per-partition ``event_index``-versus-
   Parquet reconciliation is the one T037 delivers. This module
   consumes its result for both pools via
   :class:`PoolComparisonInput.partition_reconciliation_consistent`;
   it does not re-implement, re-derive or relax the check.

The module is float-free and depends only on the protocol package
(``PoolId`` / ``PoolKey``), the replay package (:class:`ReplayInput`,
:class:`ReplayOutput`, :class:`ReconstructedPoolTickState`), and the
P03 T037 reconciliation report (consumed via a boolean flag, not
re-derived here).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from robinhood_lp.protocol import (
    ChainId,
    PoolId,
    PoolKey,
)
from robinhood_lp.replay.input import ReplayInput
from robinhood_lp.replay.output import ReplayOutput
from robinhood_lp.replay.protocol_fee import (
    PROTOCOL_FEE_HALF_MASK,
    PROTOCOL_FEE_TOKEN0_SHIFT,
)
from robinhood_lp.replay.ticks import ReconstructedPoolTickState

# ---------------------------------------------------------------------------
# StateView method names (T042 acceptance: slot0, active liquidity,
# ticks/bitmap, fee growth where supported)
# ---------------------------------------------------------------------------

STATE_VIEW_METHOD_GET_SLOT_0: Final[str] = "getSlot0"
STATE_VIEW_METHOD_GET_LIQUIDITY: Final[str] = "getLiquidity"
STATE_VIEW_METHOD_GET_TICK_BITMAP: Final[str] = "getTickBitmap"
STATE_VIEW_METHOD_GET_TICK_LIQUIDITY: Final[str] = "getTickLiquidity"
STATE_VIEW_METHOD_GET_TICK_INFO: Final[str] = "getTickInfo"
STATE_VIEW_METHOD_GET_FEE_GROWTH_GLOBALS: Final[str] = "getFeeGrowthGlobals"
STATE_VIEW_METHOD_GET_FEE_GROWTH_INSIDE: Final[str] = "getFeeGrowthInside"
STATE_VIEW_METHOD_GET_TICK_FEE_GROWTH_OUTSIDE: Final[str] = "getTickFeeGrowthOutside"

VALID_STATE_VIEW_METHODS: Final[frozenset[str]] = frozenset(
    {
        STATE_VIEW_METHOD_GET_SLOT_0,
        STATE_VIEW_METHOD_GET_LIQUIDITY,
        STATE_VIEW_METHOD_GET_TICK_BITMAP,
        STATE_VIEW_METHOD_GET_TICK_LIQUIDITY,
        STATE_VIEW_METHOD_GET_TICK_INFO,
        STATE_VIEW_METHOD_GET_FEE_GROWTH_GLOBALS,
        STATE_VIEW_METHOD_GET_FEE_GROWTH_INSIDE,
        STATE_VIEW_METHOD_GET_TICK_FEE_GROWTH_OUTSIDE,
    }
)

#: StateView methods that take exactly one ``int24`` argument (the tick).
TICK_ARG_METHODS: Final[frozenset[str]] = frozenset(
    {
        STATE_VIEW_METHOD_GET_TICK_INFO,
        STATE_VIEW_METHOD_GET_TICK_LIQUIDITY,
        STATE_VIEW_METHOD_GET_TICK_FEE_GROWTH_OUTSIDE,
    }
)

#: StateView methods that take exactly one ``int16`` argument (the wordPos).
WORDPOS_ARG_METHODS: Final[frozenset[str]] = frozenset({STATE_VIEW_METHOD_GET_TICK_BITMAP})

#: StateView methods that take exactly two ``int24`` arguments
#: (lower, upper). Kept as a placeholder for the contract's "where
#: supported" fee-growth clause.
LOWER_UPPER_ARG_METHODS: Final[frozenset[str]] = frozenset(
    {STATE_VIEW_METHOD_GET_FEE_GROWTH_INSIDE}
)


# ---------------------------------------------------------------------------
# Block-reason codes (T034 precedent for unservable pinned historical reads)
# ---------------------------------------------------------------------------

#: T034 reason code precedent: the canonical "endpoint cannot serve the
#: pinned depth" code the T036 / T038 state spot check uses for
#: ``cross_endpoint_sample_missing``. T042 adopts the same code so the
#: audit trail matches across phases.
BLOCK_REASON_CROSS_ENDPOINT_SAMPLE_MISSING: Final[str] = "cross_endpoint_sample_missing"


# ---------------------------------------------------------------------------
# Per-call compute-unit cost (V4 / Robinhood Chain measured, conservative)
# ---------------------------------------------------------------------------

#: Per-call compute-unit estimate. Each StateView method has a known
#: approximate cost; the runner accumulates the sum per pool and
#: compares against the per-pool ceiling. The numbers are conservative
#: defaults operators may tighten if their plan needs a smaller budget.
COMPUTE_UNIT_COST_GET_SLOT_0: Final[int] = 2_400
COMPUTE_UNIT_COST_GET_LIQUIDITY: Final[int] = 2_200
COMPUTE_UNIT_COST_GET_TICK_BITMAP: Final[int] = 3_000
COMPUTE_UNIT_COST_GET_TICK_LIQUIDITY: Final[int] = 3_000
COMPUTE_UNIT_COST_GET_TICK_INFO: Final[int] = 3_500
COMPUTE_UNIT_COST_GET_FEE_GROWTH_GLOBALS: Final[int] = 2_200
COMPUTE_UNIT_COST_GET_FEE_GROWTH_INSIDE: Final[int] = 4_000
COMPUTE_UNIT_COST_GET_TICK_FEE_GROWTH_OUTSIDE: Final[int] = 3_000

#: Default logical-call ceiling per pool. The runner refuses to plan a
#: run whose recorded per-height read list exceeds this; a run that
#: approaches it mid-flight halts and reports exhaustion rather than
#: silently reducing coverage.
DEFAULT_LOGICAL_CALL_CEILING_PER_POOL: Final[int] = 64

#: Default compute-unit ceiling per pool. Same budget exhaustion rule
#: applies on this side.
DEFAULT_COMPUTE_UNIT_CEILING_PER_POOL: Final[int] = 300_000


def _compute_unit_cost(method: str) -> int:
    if method == STATE_VIEW_METHOD_GET_SLOT_0:
        return COMPUTE_UNIT_COST_GET_SLOT_0
    if method == STATE_VIEW_METHOD_GET_LIQUIDITY:
        return COMPUTE_UNIT_COST_GET_LIQUIDITY
    if method == STATE_VIEW_METHOD_GET_TICK_BITMAP:
        return COMPUTE_UNIT_COST_GET_TICK_BITMAP
    if method == STATE_VIEW_METHOD_GET_TICK_LIQUIDITY:
        return COMPUTE_UNIT_COST_GET_TICK_LIQUIDITY
    if method == STATE_VIEW_METHOD_GET_TICK_INFO:
        return COMPUTE_UNIT_COST_GET_TICK_INFO
    if method == STATE_VIEW_METHOD_GET_FEE_GROWTH_GLOBALS:
        return COMPUTE_UNIT_COST_GET_FEE_GROWTH_GLOBALS
    if method == STATE_VIEW_METHOD_GET_FEE_GROWTH_INSIDE:
        return COMPUTE_UNIT_COST_GET_FEE_GROWTH_INSIDE
    if method == STATE_VIEW_METHOD_GET_TICK_FEE_GROWTH_OUTSIDE:
        return COMPUTE_UNIT_COST_GET_TICK_FEE_GROWTH_OUTSIDE
    raise ValueError(f"_compute_unit_cost: unknown method {method!r}")


# ---------------------------------------------------------------------------
# Block-pinned StateView call shape (per-pool)
# ---------------------------------------------------------------------------

#: The block-pinned StateView read the runner issues. The callable
#: receives the StateView method name, the explicit pinned-block tag
#: (the ``"0x<hex>"`` form, never ``latest``), the 32-byte PoolId
#: argument, and any extra arguments the StateView method needs
#: (``int16`` wordPos for ``getTickBitmap``, ``int24`` tick for the
#: tick-reading methods, ``(int24, int24)`` for ``getFeeGrowthInside``).
#:
#: Implementations must return the encoded payload ``bytes`` when the
#: endpoint served the call, or ``None`` when the endpoint cannot
#: serve the pinned block tag (the routing-asymmetry case
#: ``PROVIDER_FACTS.md §8`` records for the primary endpoint). A
#: ``None`` return is the only signal the runner records a blocked
#: check against; returning a ``latest``-based payload is forbidden
#: because the alias and the explicit block tag are recorded.
BlockPinnedStateViewCall = Callable[[str, str, bytes, tuple[int, ...]], bytes | None]


def make_block_pinned_state_view_call(
    responses: dict[tuple[str, str, bytes, tuple[int, ...]], bytes | None],
) -> BlockPinnedStateViewCall:
    """Build a deterministic :class:`BlockPinnedStateViewCall` from a
    mapping.

    The mapping keys are
    ``(method_name, block_tag, pool_id_bytes, extra_args)`` tuples;
    the value is the bytes payload the endpoint returned, or
    ``None`` to record a blocked check. An unmapped key returns
    ``None`` so the blocked-check path is the default failure mode.
    """

    def _call(
        method: str,
        block_tag: str,
        pool_id_bytes: bytes,
        extra_args: tuple[int, ...],
    ) -> bytes | None:
        return responses.get((method, block_tag, pool_id_bytes, extra_args))

    return _call


# ---------------------------------------------------------------------------
# Per-height read specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateViewReadSpec:
    """One read on the recorded per-height read list.

    ``method`` is one of :data:`VALID_STATE_VIEW_METHODS`. The
    optional ``extra_args`` carries the StateView method's integer
    arguments: ``(wordPos,)`` for ``getTickBitmap``,
    ``(tick,)`` for the tick-reading methods, ``(tickLower,
    tickUpper,)`` for ``getFeeGrowthInside``. Methods that take no
    extra argument (``getSlot0``, ``getLiquidity``,
    ``getFeeGrowthGlobals``) leave ``extra_args`` empty.
    """

    method: str
    extra_args: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.method, str):
            raise TypeError(
                f"StateViewReadSpec.method: must be str, got {type(self.method).__name__}"
            )
        if self.method not in VALID_STATE_VIEW_METHODS:
            raise ValueError(
                f"StateViewReadSpec.method: must be one of "
                f"{sorted(VALID_STATE_VIEW_METHODS)}, got {self.method!r}"
            )
        for value in self.extra_args:
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(
                    f"StateViewReadSpec.extra_args: each entry must be int, "
                    f"got {type(value).__name__}"
                )
        if self.method in TICK_ARG_METHODS:
            if len(self.extra_args) != 1:
                raise ValueError(
                    f"StateViewReadSpec.method={self.method!r} requires exactly "
                    f"one int24 extra_arg, got {len(self.extra_args)}"
                )
            tick = self.extra_args[0]
            if tick < -(1 << 23) or tick >= (1 << 23):
                raise ValueError(
                    f"StateViewReadSpec.method={self.method!r} tick {tick} does not fit in int24"
                )
        elif self.method in WORDPOS_ARG_METHODS:
            if len(self.extra_args) != 1:
                raise ValueError(
                    f"StateViewReadSpec.method={self.method!r} requires exactly "
                    f"one int16 extra_arg, got {len(self.extra_args)}"
                )
            word_pos = self.extra_args[0]
            if word_pos < -(1 << 15) or word_pos >= (1 << 15):
                raise ValueError(
                    f"StateViewReadSpec.method={self.method!r} wordPos {word_pos} "
                    f"does not fit in int16"
                )
        elif self.method in LOWER_UPPER_ARG_METHODS:
            if len(self.extra_args) != 2:
                raise ValueError(
                    f"StateViewReadSpec.method={self.method!r} requires exactly "
                    f"two int24 extra_args, got {len(self.extra_args)}"
                )
            for value in self.extra_args:
                if value < -(1 << 23) or value >= (1 << 23):
                    raise ValueError(
                        f"StateViewReadSpec.method={self.method!r} tick {value} "
                        f"does not fit in int24"
                    )


# ---------------------------------------------------------------------------
# Per-pool comparison input
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolComparisonInput:
    """The per-pool comparison input the runner consumes.

    ``pool_alias`` is the short opaque token T038 qualified the pool
    under (e.g. ``reference``, ``second``); ``chain_id`` and
    ``pool_id`` identify the pool on chain; ``pool_key`` carries the
    declared fee the ``getSlot0`` ``lpFee`` field must match. The
    ``replay_input`` / ``replay_output`` pair is the per-pool replay
    this run validates; ``reconstructed_state`` is the per-pool T041
    reconstruction the tick-reading fields compare against;
    ``partition_reconciliation_consistent`` is the T037 verdict for
    this pool, consumed as-is (T042 does not re-derive the check).

    ``per_height_reads`` is the recorded per-height read list: the
    iteration order is the recorded acceptance surface. The
    ``endpoint_alias`` is the endpoint the calls are routed to; the
    runner records it on every read result and every blocked check
    so a silent ``latest`` substitution is visible in the audit
    trail.
    """

    pool_alias: str
    chain_id: ChainId
    pool_id: PoolId
    pool_key: PoolKey
    replay_input: ReplayInput
    replay_output: ReplayOutput
    reconstructed_state: ReconstructedPoolTickState
    partition_reconciliation_consistent: bool
    per_height_reads: tuple[tuple[int, tuple[StateViewReadSpec, ...]], ...]
    endpoint_alias: str
    state_call: BlockPinnedStateViewCall

    def __post_init__(self) -> None:
        if not isinstance(self.pool_alias, str) or not self.pool_alias:
            raise ValueError(
                f"PoolComparisonInput.pool_alias: must be non-empty str, got {self.pool_alias!r}"
            )
        if not isinstance(self.chain_id, ChainId):
            raise TypeError(
                f"PoolComparisonInput.chain_id: must be ChainId, got {type(self.chain_id).__name__}"
            )
        if not isinstance(self.pool_id, PoolId):
            raise TypeError(
                f"PoolComparisonInput.pool_id: must be PoolId, got {type(self.pool_id).__name__}"
            )
        if not isinstance(self.pool_key, PoolKey):
            raise TypeError(
                f"PoolComparisonInput.pool_key: must be PoolKey, got {type(self.pool_key).__name__}"
            )
        if not isinstance(self.replay_input, ReplayInput):
            raise TypeError(
                f"PoolComparisonInput.replay_input: must be ReplayInput, got "
                f"{type(self.replay_input).__name__}"
            )
        if not isinstance(self.replay_output, ReplayOutput):
            raise TypeError(
                f"PoolComparisonInput.replay_output: must be ReplayOutput, got "
                f"{type(self.replay_output).__name__}"
            )
        if not isinstance(self.reconstructed_state, ReconstructedPoolTickState):
            raise TypeError(
                f"PoolComparisonInput.reconstructed_state: must be "
                f"ReconstructedPoolTickState, got "
                f"{type(self.reconstructed_state).__name__}"
            )
        if not isinstance(self.partition_reconciliation_consistent, bool):
            raise TypeError(
                f"PoolComparisonInput.partition_reconciliation_consistent: "
                f"must be bool, got "
                f"{type(self.partition_reconciliation_consistent).__name__}"
            )
        if not self.per_height_reads:
            raise ValueError(
                "PoolComparisonInput.per_height_reads: at least one (height, "
                "reads) pair is required (T042 acceptance: 'multiple heights')"
            )
        # The per-height read list is the recorded acceptance surface;
        # the runner must never reorder it, so we freeze the tuple
        # here and refuse duplicates by construction.
        seen_heights: set[int] = set()
        for entry in self.per_height_reads:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise TypeError(
                    f"PoolComparisonInput.per_height_reads: each entry must be "
                    f"(int, tuple[StateViewReadSpec, ...]); got {entry!r}"
                )
            height, reads = entry
            if not isinstance(height, int) or isinstance(height, bool):
                raise TypeError(
                    f"PoolComparisonInput.per_height_reads: height must be "
                    f"int, got {type(height).__name__}"
                )
            if height < 0:
                raise ValueError(
                    f"PoolComparisonInput.per_height_reads: height must be >= 0, got {height}"
                )
            if not isinstance(reads, tuple):
                raise TypeError(
                    f"PoolComparisonInput.per_height_reads: reads must be "
                    f"tuple, got {type(reads).__name__}"
                )
            for read in reads:
                if not isinstance(read, StateViewReadSpec):
                    raise TypeError(
                        f"PoolComparisonInput.per_height_reads: each read must "
                        f"be StateViewReadSpec, got {type(read).__name__}"
                    )
            if height in seen_heights:
                raise ValueError(
                    f"PoolComparisonInput.per_height_reads: duplicate height "
                    f"{height}; the recorded read list is the acceptance surface "
                    f"and must not repeat"
                )
            seen_heights.add(height)
        if not isinstance(self.endpoint_alias, str) or not self.endpoint_alias:
            raise ValueError(
                f"PoolComparisonInput.endpoint_alias: must be non-empty str, "
                f"got {self.endpoint_alias!r}"
            )
        if not callable(self.state_call):
            raise TypeError(
                f"PoolComparisonInput.state_call: must be callable, got "
                f"{type(self.state_call).__name__}"
            )


# ---------------------------------------------------------------------------
# Recorded bounds
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedComparisonBounds:
    """The recorded comparison bounds the runner fixes before it runs.

    ``heights_per_pool`` carries the per-pool inclusive number of
    heights the runner compares (the count, not the heights themselves;
    the heights live on :class:`PoolComparisonInput.per_height_reads`).
    ``per_height_read_list`` is the global list of StateView methods
    any pool may read at any height (the recorded vocabulary; the
    per-pool heights carry their own per-height list).
    ``logical_call_ceiling_per_pool`` and
    ``compute_unit_ceiling_per_pool`` are the ceilings the contract
    pins. The runner records them on every per-pool report so the
    audit trail can reproduce the recorded bounds.
    """

    heights_per_pool: int
    per_height_read_list: tuple[str, ...]
    logical_call_ceiling_per_pool: int
    compute_unit_ceiling_per_pool: int

    def __post_init__(self) -> None:
        if not isinstance(self.heights_per_pool, int) or isinstance(self.heights_per_pool, bool):
            raise TypeError(
                f"RecordedComparisonBounds.heights_per_pool: must be int, "
                f"got {type(self.heights_per_pool).__name__}"
            )
        if self.heights_per_pool < 1:
            raise ValueError(
                f"RecordedComparisonBounds.heights_per_pool: must be >= 1 "
                f"(T042 acceptance: 'multiple heights'), "
                f"got {self.heights_per_pool}"
            )
        if not isinstance(self.per_height_read_list, tuple):
            raise TypeError(
                f"RecordedComparisonBounds.per_height_read_list: must be "
                f"tuple, got {type(self.per_height_read_list).__name__}"
            )
        for method in self.per_height_read_list:
            if not isinstance(method, str):
                raise TypeError(
                    f"RecordedComparisonBounds.per_height_read_list: each "
                    f"entry must be str, got {type(method).__name__}"
                )
            if method not in VALID_STATE_VIEW_METHODS:
                raise ValueError(
                    f"RecordedComparisonBounds.per_height_read_list: method "
                    f"{method!r} not in VALID_STATE_VIEW_METHODS"
                )
        if not isinstance(self.logical_call_ceiling_per_pool, int) or isinstance(
            self.logical_call_ceiling_per_pool, bool
        ):
            raise TypeError(
                f"RecordedComparisonBounds.logical_call_ceiling_per_pool: "
                f"must be int, got "
                f"{type(self.logical_call_ceiling_per_pool).__name__}"
            )
        if self.logical_call_ceiling_per_pool < 0:
            raise ValueError(
                f"RecordedComparisonBounds.logical_call_ceiling_per_pool: "
                f"must be >= 0, got "
                f"{self.logical_call_ceiling_per_pool}"
            )
        if not isinstance(self.compute_unit_ceiling_per_pool, int) or isinstance(
            self.compute_unit_ceiling_per_pool, bool
        ):
            raise TypeError(
                f"RecordedComparisonBounds.compute_unit_ceiling_per_pool: "
                f"must be int, got "
                f"{type(self.compute_unit_ceiling_per_pool).__name__}"
            )
        if self.compute_unit_ceiling_per_pool < 0:
            raise ValueError(
                f"RecordedComparisonBounds.compute_unit_ceiling_per_pool: "
                f"must be >= 0, got "
                f"{self.compute_unit_ceiling_per_pool}"
            )


# ---------------------------------------------------------------------------
# Per-read comparison result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldComparison:
    """One StateView read's integer-field comparison result.

    ``method`` is the StateView method the runner issued;
    ``height`` is the explicit pinned-block height the read targeted;
    ``endpoint_alias`` is the endpoint the runner routed the read to
    (never ``latest``). ``expected_fields`` and ``observed_fields``
    are parallel ``dict[str, int]`` mappings; ``passed`` is
    ``True`` iff every expected value equals the observed value
    byte-for-byte. ``mismatch_detail`` is the ``None``-or-string
    summary a downstream audit trail surfaces.
    """

    method: str
    height: int
    endpoint_alias: str
    passed: bool
    expected_fields: dict[str, int]
    observed_fields: dict[str, int]
    mismatch_detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.method, str):
            raise TypeError(
                f"FieldComparison.method: must be str, got {type(self.method).__name__}"
            )
        if not isinstance(self.height, int) or isinstance(self.height, bool):
            raise TypeError(
                f"FieldComparison.height: must be int, got {type(self.height).__name__}"
            )
        if self.height < 0:
            raise ValueError(f"FieldComparison.height: must be >= 0, got {self.height}")
        if not isinstance(self.endpoint_alias, str) or not self.endpoint_alias:
            raise ValueError(
                f"FieldComparison.endpoint_alias: must be non-empty str, "
                f"got {self.endpoint_alias!r}"
            )
        if not isinstance(self.passed, bool):
            raise TypeError(
                f"FieldComparison.passed: must be bool, got {type(self.passed).__name__}"
            )
        if not isinstance(self.expected_fields, dict):
            raise TypeError(
                f"FieldComparison.expected_fields: must be dict, got "
                f"{type(self.expected_fields).__name__}"
            )
        if not isinstance(self.observed_fields, dict):
            raise TypeError(
                f"FieldComparison.observed_fields: must be dict, got "
                f"{type(self.observed_fields).__name__}"
            )
        if self.passed:
            if self.mismatch_detail is not None:
                raise ValueError("FieldComparison.mismatch_detail must be None when passed")
        else:
            if not self.mismatch_detail:
                raise ValueError("FieldComparison.mismatch_detail is required when passed=False")


# ---------------------------------------------------------------------------
# Blocked check
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockedCheckRecord:
    """A height / endpoint the runner could not serve.

    The record is the coverage the run did not obtain. The reason
    code is the T034 precedent
    (:data:`BLOCK_REASON_CROSS_ENDPOINT_SAMPLE_MISSING`); the height,
    method, and endpoint alias pinpoint where the read was attempted
    so the audit trail never loses the routing decision.
    """

    pool_alias: str
    height: int
    method: str
    endpoint_alias: str
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.pool_alias, str) or not self.pool_alias:
            raise ValueError(
                f"BlockedCheckRecord.pool_alias: must be non-empty str, got {self.pool_alias!r}"
            )
        if not isinstance(self.height, int) or isinstance(self.height, bool):
            raise TypeError(
                f"BlockedCheckRecord.height: must be int, got {type(self.height).__name__}"
            )
        if self.height < 0:
            raise ValueError(f"BlockedCheckRecord.height: must be >= 0, got {self.height}")
        if not isinstance(self.method, str):
            raise TypeError(
                f"BlockedCheckRecord.method: must be str, got {type(self.method).__name__}"
            )
        if not isinstance(self.endpoint_alias, str) or not self.endpoint_alias:
            raise ValueError(
                f"BlockedCheckRecord.endpoint_alias: must be non-empty str, "
                f"got {self.endpoint_alias!r}"
            )
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise ValueError(
                f"BlockedCheckRecord.reason_code: must be non-empty str, got {self.reason_code!r}"
            )


# ---------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetExhaustionRecord:
    """The snapshot the runner records when a budget ceiling is hit.

    A budget exhaustion halts the pool's run and is never silently
    recovered by reducing coverage. The record carries the budget
    dimension (``logical_calls`` or ``compute_units``), the value
    that triggered the halt, the ceiling that was exceeded, and the
    height / method at which the runner stopped so the audit trail
    can reproduce the recorded bounds.
    """

    pool_alias: str
    dimension: str
    used_at_halt: int
    ceiling: int
    halt_height: int
    halt_method: str

    def __post_init__(self) -> None:
        if not isinstance(self.pool_alias, str) or not self.pool_alias:
            raise ValueError(
                f"BudgetExhaustionRecord.pool_alias: must be non-empty str, got {self.pool_alias!r}"
            )
        if self.dimension not in ("logical_calls", "compute_units"):
            raise ValueError(
                f"BudgetExhaustionRecord.dimension: must be 'logical_calls' "
                f"or 'compute_units', got {self.dimension!r}"
            )
        for name in ("used_at_halt", "ceiling", "halt_height"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(
                    f"BudgetExhaustionRecord.{name}: must be int, got {type(value).__name__}"
                )
        if self.ceiling < 0:
            raise ValueError(f"BudgetExhaustionRecord.ceiling: must be >= 0, got {self.ceiling}")
        if self.used_at_halt <= self.ceiling:
            # The runner only emits this record when a ceiling has
            # been exceeded; a "halted at-or-below ceiling" is an
            # internal-logic error.
            raise ValueError(
                f"BudgetExhaustionRecord.used_at_halt {self.used_at_halt} "
                f"must exceed ceiling {self.ceiling}"
            )
        if self.halt_height < 0:
            raise ValueError(
                f"BudgetExhaustionRecord.halt_height: must be >= 0, got {self.halt_height}"
            )
        if not isinstance(self.halt_method, str) or not self.halt_method:
            raise ValueError(
                f"BudgetExhaustionRecord.halt_method: must be non-empty "
                f"str, got {self.halt_method!r}"
            )


# ---------------------------------------------------------------------------
# Per-pool report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolStateComparisonReport:
    """The per-pool StateView comparison report.

    ``heights_compared`` lists the heights the runner actually
    attempted; ``per_height_read_list`` is the recorded per-height
    read vocabulary (preserved for the audit trail);
    ``logical_calls_used`` and ``compute_units_used`` are the
    consumed budgets; ``field_comparisons`` is the per-read result;
    ``blocked_checks`` is the unserved-read evidence;
    ``budget_exhaustion`` is the budget-halt snapshot, if any;
    ``passes`` is ``True`` iff every comparison passed, every
    recorded read was served (no blocked checks), the budget was
    not exhausted, and the T037 reconciliation verdict is
    consistent. ``pool_id_hex`` carries the canonical lowercase
    ``0x``-hex form of :attr:`PoolComparisonInput.pool_id`.
    """

    pool_alias: str
    chain_id_value: int
    pool_id_hex: str
    pool_id_int: int
    heights_compared: tuple[int, ...]
    per_height_read_list: tuple[str, ...]
    logical_calls_used: int
    compute_units_used: int
    logical_call_ceiling: int
    compute_unit_ceiling: int
    field_comparisons: tuple[FieldComparison, ...]
    blocked_checks: tuple[BlockedCheckRecord, ...]
    budget_exhaustion: BudgetExhaustionRecord | None
    partition_reconciliation_consistent: bool
    endpoint_alias: str

    @property
    def passes(self) -> bool:
        """Return True iff the per-pool run is fully accepted.

        Acceptance requires: every field comparison passed; no
        blocked checks; no budget exhaustion; and the T037
        partition reconciliation is consistent for this pool.
        """
        if not self.partition_reconciliation_consistent:
            return False
        if self.budget_exhaustion is not None:
            return False
        if self.blocked_checks:
            return False
        if not self.field_comparisons:
            return False
        return all(c.passed for c in self.field_comparisons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "robinhood_lp.replay.state_comparison.v1",
            "pool_alias": self.pool_alias,
            "chain_id": self.chain_id_value,
            "pool_id": self.pool_id_hex,
            "heights_compared": list(self.heights_compared),
            "per_height_read_list": list(self.per_height_read_list),
            "logical_calls_used": self.logical_calls_used,
            "compute_units_used": self.compute_units_used,
            "logical_call_ceiling": self.logical_call_ceiling,
            "compute_unit_ceiling": self.compute_unit_ceiling,
            "endpoint_alias": self.endpoint_alias,
            "partition_reconciliation_consistent": (self.partition_reconciliation_consistent),
            "field_comparisons": [
                {
                    "method": c.method,
                    "height": c.height,
                    "endpoint_alias": c.endpoint_alias,
                    "passed": c.passed,
                    "expected_fields": dict(c.expected_fields),
                    "observed_fields": dict(c.observed_fields),
                    "mismatch_detail": c.mismatch_detail,
                }
                for c in self.field_comparisons
            ],
            "blocked_checks": [
                {
                    "height": b.height,
                    "method": b.method,
                    "endpoint_alias": b.endpoint_alias,
                    "reason_code": b.reason_code,
                }
                for b in self.blocked_checks
            ],
            "budget_exhaustion": (
                None
                if self.budget_exhaustion is None
                else {
                    "dimension": self.budget_exhaustion.dimension,
                    "used_at_halt": self.budget_exhaustion.used_at_halt,
                    "ceiling": self.budget_exhaustion.ceiling,
                    "halt_height": self.budget_exhaustion.halt_height,
                    "halt_method": self.budget_exhaustion.halt_method,
                }
            ),
            "passes": self.passes,
        }


# ---------------------------------------------------------------------------
# Combined report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateComparisonReport:
    """The combined StateView comparison report over the heterogeneous pools.

    ``pool_reports`` carries one :class:`PoolStateComparisonReport`
    per input pool in input order; ``bounds`` is the recorded
    bounds the runner fixed before it ran. ``overall_passes`` is
    ``True`` iff every per-pool report passes — i.e. one pool's
    agreement is never presented as the other pool's result (the
    combined verdict is the AND of the per-pool verdicts).
    """

    pool_reports: tuple[PoolStateComparisonReport, ...]
    bounds: RecordedComparisonBounds

    @property
    def overall_passes(self) -> bool:
        return all(p.passes for p in self.pool_reports)

    def pool_report(self, pool_alias: str) -> PoolStateComparisonReport:
        for report in self.pool_reports:
            if report.pool_alias == pool_alias:
                return report
        raise KeyError(f"StateComparisonReport: no per-pool report for alias {pool_alias!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "robinhood_lp.replay.state_comparison.combined.v1",
            "bounds": {
                "heights_per_pool": self.bounds.heights_per_pool,
                "per_height_read_list": list(self.bounds.per_height_read_list),
                "logical_call_ceiling_per_pool": (self.bounds.logical_call_ceiling_per_pool),
                "compute_unit_ceiling_per_pool": (self.bounds.compute_unit_ceiling_per_pool),
            },
            "pool_reports": [r.to_dict() for r in self.pool_reports],
            "overall_passes": self.overall_passes,
        }


# ---------------------------------------------------------------------------
# Encoded-response helpers
# ---------------------------------------------------------------------------


def _decode_slot_uint(value: bytes, *, bits: int, field: str) -> int:
    """Decode one 32-byte slot as an unsigned integer of ``bits`` width."""
    if not isinstance(value, bytes):
        raise TypeError(f"{field}: must be bytes, got {type(value).__name__}")
    if len(value) != 32:
        raise ValueError(f"{field}: must be 32 bytes, got {len(value)}")
    out = int.from_bytes(value, "big")
    upper = 1 << bits
    if out < 0 or out >= upper:
        raise ValueError(
            f"{field}: value {out:#x} does not fit in uint{bits} (value range [0, {upper}))"
        )
    return out


def _decode_slot_int(value: bytes, *, bits: int, field: str) -> int:
    """Decode one 32-byte slot as a signed integer of ``bits`` width."""
    if not isinstance(value, bytes):
        raise TypeError(f"{field}: must be bytes, got {type(value).__name__}")
    if len(value) != 32:
        raise ValueError(f"{field}: must be 32 bytes, got {len(value)}")
    out = int.from_bytes(value, "big", signed=True)
    upper = 1 << (bits - 1)
    if out < -upper or out >= upper:
        raise ValueError(f"{field}: value {out} does not fit in int{bits}")
    return out


def _decode_slot_uint128(value: bytes, *, field: str) -> int:
    return _decode_slot_uint(value, bits=128, field=field)


def _decode_slot_int128(value: bytes, *, field: str) -> int:
    return _decode_slot_int(value, bits=128, field=field)


def _decode_slot_uint160(value: bytes, *, field: str) -> int:
    return _decode_slot_uint(value, bits=160, field=field)


def _decode_slot_int24(value: bytes, *, field: str) -> int:
    return _decode_slot_int(value, bits=24, field=field)


def _decode_slot_uint24(value: bytes, *, field: str) -> int:
    return _decode_slot_uint(value, bits=24, field=field)


def _decode_slot_uint256(value: bytes, *, field: str) -> int:
    return _decode_slot_uint(value, bits=256, field=field)


def _decode_slot_int16(value: bytes, *, field: str) -> int:
    return _decode_slot_int(value, bits=16, field=field)


# ---------------------------------------------------------------------------
# StateView response decoders
# ---------------------------------------------------------------------------


def decode_get_slot_0(
    payload: bytes,
    *,
    expected_lp_fee: int | None = None,
) -> dict[str, int]:
    """Decode a ``getSlot0`` response into integer fields.

    The V4 ``getSlot0`` ABI is
    ``(uint160 sqrtPriceX96, int24 tick, uint24 lpFee, uint24 protocolFee)``;
    Solidity returns four 32-byte slots (128 bytes total). The
    ``protocolFee`` is a packed ``uint24`` whose high 12 bits encode
    the token-0 protocol fee and whose low 12 bits encode the
    token-1 protocol fee; this helper decodes both halves and exposes
    them separately so the comparison matches the replay's
    directional protocol-fee state.

    When ``expected_lp_fee`` is supplied the decoded ``lpFee`` is
    verified against it as a structural sanity check; the
    comparison runner surfaces the mismatch via
    :class:`FieldComparison` so callers can attribute it to the
    wrong read. Pass ``None`` to skip the check.
    """
    if not isinstance(payload, bytes):
        raise TypeError(f"decode_get_slot_0: payload must be bytes, got {type(payload).__name__}")
    if len(payload) != 32 * 4:
        raise ValueError(
            f"decode_get_slot_0: payload must be 128 bytes (4 slots), got {len(payload)}"
        )
    sqrt_price_x96 = _decode_slot_uint160(payload[0:32], field="getSlot0.sqrtPriceX96")
    tick = _decode_slot_int24(payload[32:64], field="getSlot0.tick")
    lp_fee = _decode_slot_uint24(payload[64:96], field="getSlot0.lpFee")
    protocol_fee_packed = _decode_slot_uint24(payload[96:128], field="getSlot0.protocolFee")
    if expected_lp_fee is not None and lp_fee != int(expected_lp_fee):
        raise ValueError(
            f"decode_get_slot_0: lpFee {lp_fee} != expected pool_key.fee {expected_lp_fee}"
        )
    token0_fee = (int(protocol_fee_packed) >> PROTOCOL_FEE_TOKEN0_SHIFT) & PROTOCOL_FEE_HALF_MASK
    token1_fee = int(protocol_fee_packed) & PROTOCOL_FEE_HALF_MASK
    return {
        "sqrtPriceX96": sqrt_price_x96,
        "tick": tick,
        "lpFee": lp_fee,
        "protocolFee": int(protocol_fee_packed),
        "protocolFee_token0": token0_fee,
        "protocolFee_token1": token1_fee,
    }


def decode_get_liquidity(payload: bytes) -> dict[str, int]:
    """Decode a ``getLiquidity`` response into an integer field."""
    if not isinstance(payload, bytes):
        raise TypeError(
            f"decode_get_liquidity: payload must be bytes, got {type(payload).__name__}"
        )
    if len(payload) != 32:
        raise ValueError(
            f"decode_get_liquidity: payload must be 32 bytes (1 slot), got {len(payload)}"
        )
    return {"liquidity": _decode_slot_uint128(payload[0:32], field="getLiquidity")}


def decode_get_tick_bitmap(payload: bytes) -> dict[str, int]:
    """Decode a ``getTickBitmap`` response into an integer field."""
    if not isinstance(payload, bytes):
        raise TypeError(
            f"decode_get_tick_bitmap: payload must be bytes, got {type(payload).__name__}"
        )
    if len(payload) != 32:
        raise ValueError(
            f"decode_get_tick_bitmap: payload must be 32 bytes (1 slot), got {len(payload)}"
        )
    return {"word": _decode_slot_uint256(payload[0:32], field="getTickBitmap")}


def decode_get_tick_liquidity(payload: bytes) -> dict[str, int]:
    """Decode a ``getTickLiquidity`` response into integer fields."""
    if not isinstance(payload, bytes):
        raise TypeError(
            f"decode_get_tick_liquidity: payload must be bytes, got {type(payload).__name__}"
        )
    if len(payload) != 32 * 2:
        raise ValueError(
            f"decode_get_tick_liquidity: payload must be 64 bytes (2 slots), got {len(payload)}"
        )
    return {
        "liquidityGross": _decode_slot_uint128(
            payload[0:32], field="getTickLiquidity.liquidityGross"
        ),
        "liquidityNet": _decode_slot_int128(payload[32:64], field="getTickLiquidity.liquidityNet"),
    }


def decode_get_tick_info(payload: bytes) -> dict[str, int]:
    """Decode a ``getTickInfo`` response into integer fields."""
    if not isinstance(payload, bytes):
        raise TypeError(
            f"decode_get_tick_info: payload must be bytes, got {type(payload).__name__}"
        )
    if len(payload) != 32 * 4:
        raise ValueError(
            f"decode_get_tick_info: payload must be 128 bytes (4 slots), got {len(payload)}"
        )
    return {
        "liquidityGross": _decode_slot_uint128(payload[0:32], field="getTickInfo.liquidityGross"),
        "liquidityNet": _decode_slot_int128(payload[32:64], field="getTickInfo.liquidityNet"),
        "feeGrowthOutside0X128": _decode_slot_uint256(
            payload[64:96], field="getTickInfo.feeGrowthOutside0X128"
        ),
        "feeGrowthOutside1X128": _decode_slot_uint256(
            payload[96:128], field="getTickInfo.feeGrowthOutside1X128"
        ),
    }


def decode_get_fee_growth_globals(payload: bytes) -> dict[str, int]:
    """Decode a ``getFeeGrowthGlobals`` response into integer fields."""
    if not isinstance(payload, bytes):
        raise TypeError(
            f"decode_get_fee_growth_globals: payload must be bytes, got {type(payload).__name__}"
        )
    if len(payload) != 32 * 2:
        raise ValueError(
            f"decode_get_fee_growth_globals: payload must be 64 bytes (2 slots), got {len(payload)}"
        )
    return {
        "feeGrowthGlobal0X128": _decode_slot_uint256(
            payload[0:32], field="getFeeGrowthGlobals.feeGrowthGlobal0X128"
        ),
        "feeGrowthGlobal1X128": _decode_slot_uint256(
            payload[32:64], field="getFeeGrowthGlobals.feeGrowthGlobal1X128"
        ),
    }


def decode_get_tick_fee_growth_outside(payload: bytes) -> dict[str, int]:
    """Decode a ``getTickFeeGrowthOutside`` response into integer fields."""
    if not isinstance(payload, bytes):
        raise TypeError(
            f"decode_get_tick_fee_growth_outside: payload must be bytes, "
            f"got {type(payload).__name__}"
        )
    if len(payload) != 32 * 2:
        raise ValueError(
            f"decode_get_tick_fee_growth_outside: payload must be 64 bytes "
            f"(2 slots), got {len(payload)}"
        )
    return {
        "feeGrowthOutside0X128": _decode_slot_uint256(
            payload[0:32],
            field="getTickFeeGrowthOutside.feeGrowthOutside0X128",
        ),
        "feeGrowthOutside1X128": _decode_slot_uint256(
            payload[32:64],
            field="getTickFeeGrowthOutside.feeGrowthOutside1X128",
        ),
    }


# ---------------------------------------------------------------------------
# Replay-to-height helper
# ---------------------------------------------------------------------------


def replay_state_at_height(replay_output: ReplayOutput, *, height: int) -> dict[str, int]:
    """Return the replay's expected observable state at ``height``.

    Walks :attr:`ReplayOutput.checkpoints` in order and picks the last
    checkpoint whose ``block_number <= height``. Returns the
    observable state (``sqrt_price_x96``, ``tick``, ``active_liquidity``,
    protocol fees, ``last_swap_fee``, ``initialized``) the replay
    exposes to StateView at that height. The returned mapping is
    always present (an empty replay returns the zero-state pre-init
    snapshot) and is the comparison surface for ``getSlot0`` /
    ``getLiquidity``.
    """
    if not isinstance(replay_output, ReplayOutput):
        raise TypeError(
            f"replay_state_at_height: replay_output must be ReplayOutput, "
            f"got {type(replay_output).__name__}"
        )
    if not isinstance(height, int) or isinstance(height, bool):
        raise TypeError(f"replay_state_at_height: height must be int, got {type(height).__name__}")
    if height < 0:
        raise ValueError(f"replay_state_at_height: height must be >= 0, got {height}")
    picked = None
    for checkpoint in replay_output.checkpoints:
        if int(checkpoint.block_number) <= height:
            picked = checkpoint
        else:
            break
    if picked is None:
        return {
            "sqrtPriceX96": 0,
            "tick": 0,
            "liquidity": 0,
            "protocolFee": 0,
            "protocolFee_token0": 0,
            "protocolFee_token1": 0,
            "lpFee": 0,
            "lastSwapFee": 0,
            "initialized": 0,
        }
    # Pack the directional protocol-fee halves back into the
    # on-chain ``uint24`` form so the comparison surface is the
    # exact wire value ``getSlot0`` exposes.
    packed_fee = (
        (int(picked.protocol_fee_token0) & PROTOCOL_FEE_HALF_MASK) << PROTOCOL_FEE_TOKEN0_SHIFT
    ) | (int(picked.protocol_fee_token1) & PROTOCOL_FEE_HALF_MASK)
    return {
        "sqrtPriceX96": int(picked.sqrt_price_x96),
        "tick": int(picked.tick),
        "liquidity": int(picked.active_liquidity),
        "protocolFee": int(packed_fee),
        "protocolFee_token0": int(picked.protocol_fee_token0),
        "protocolFee_token1": int(picked.protocol_fee_token1),
        "lpFee": int(picked.pool_fee),
        "lastSwapFee": int(picked.last_swap_fee) if picked.last_swap_fee is not None else 0,
        "initialized": int(bool(picked.initialized)),
    }


# ---------------------------------------------------------------------------
# Per-pool runner
# ---------------------------------------------------------------------------


def _compare_slot_0(
    pool_input: PoolComparisonInput,
    *,
    height: int,
    response: bytes,
) -> FieldComparison:
    # The decoder is told the expected ``lpFee`` so a malformed
    # response (e.g. one with an out-of-range ``lpFee``) raises
    # before the comparator runs. When the encoded ``lpFee`` differs
    # from the pool's declared fee the comparator surfaces the
    # disagreement as a :class:`FieldComparison` mismatch, so the
    # call passes ``None`` here and lets the comparison decide.
    try:
        decoded = decode_get_slot_0(response, expected_lp_fee=None)
    except ValueError as exc:
        return FieldComparison(
            method=STATE_VIEW_METHOD_GET_SLOT_0,
            height=height,
            endpoint_alias=pool_input.endpoint_alias,
            passed=False,
            expected_fields={},
            observed_fields={},
            mismatch_detail=f"decode_get_slot_0: {exc}",
        )
    expected_state = replay_state_at_height(pool_input.replay_output, height=height)
    expected = {
        "sqrtPriceX96": expected_state["sqrtPriceX96"],
        "tick": expected_state["tick"],
        "lpFee": expected_state["lpFee"],
        "protocolFee": expected_state["protocolFee"],
        "protocolFee_token0": expected_state["protocolFee_token0"],
        "protocolFee_token1": expected_state["protocolFee_token1"],
    }
    observed = {k: int(v) for k, v in decoded.items()}
    mismatches = [
        f"{k}: expected {expected[k]!r}, observed {observed[k]!r}"
        for k in expected
        if expected[k] != observed[k]
    ]
    passed = not mismatches
    return FieldComparison(
        method=STATE_VIEW_METHOD_GET_SLOT_0,
        height=height,
        endpoint_alias=pool_input.endpoint_alias,
        passed=passed,
        expected_fields=expected,
        observed_fields=observed,
        mismatch_detail=(None if passed else "; ".join(mismatches)),
    )


def _compare_get_liquidity(
    pool_input: PoolComparisonInput,
    *,
    height: int,
    response: bytes,
) -> FieldComparison:
    decoded = decode_get_liquidity(response)
    expected_state = replay_state_at_height(pool_input.replay_output, height=height)
    expected = {"liquidity": expected_state["liquidity"]}
    observed = {k: int(v) for k, v in decoded.items()}
    mismatches = [
        f"{k}: expected {expected[k]!r}, observed {observed[k]!r}"
        for k in expected
        if expected[k] != observed[k]
    ]
    passed = not mismatches
    return FieldComparison(
        method=STATE_VIEW_METHOD_GET_LIQUIDITY,
        height=height,
        endpoint_alias=pool_input.endpoint_alias,
        passed=passed,
        expected_fields=expected,
        observed_fields=observed,
        mismatch_detail=(None if passed else "; ".join(mismatches)),
    )


def _compare_get_tick_bitmap(
    pool_input: PoolComparisonInput,
    *,
    height: int,
    word_pos: int,
    response: bytes,
) -> FieldComparison:
    decoded = decode_get_tick_bitmap(response)
    word = 0
    for stored_word_pos, stored_word in pool_input.reconstructed_state.bitmap_words:
        if int(stored_word_pos) == int(word_pos):
            word = int(stored_word)
            break
    expected = {"word": int(word)}
    observed = {k: int(v) for k, v in decoded.items()}
    mismatches = [
        f"{k}: expected {expected[k]!r}, observed {observed[k]!r}"
        for k in expected
        if expected[k] != observed[k]
    ]
    passed = not mismatches
    return FieldComparison(
        method=STATE_VIEW_METHOD_GET_TICK_BITMAP,
        height=height,
        endpoint_alias=pool_input.endpoint_alias,
        passed=passed,
        expected_fields=expected,
        observed_fields=observed,
        mismatch_detail=(None if passed else "; ".join(mismatches)),
    )


def _compare_get_tick_liquidity(
    pool_input: PoolComparisonInput,
    *,
    height: int,
    tick: int,
    response: bytes,
) -> FieldComparison:
    decoded = decode_get_tick_liquidity(response)
    info = pool_input.reconstructed_state.tick_info(int(tick))
    expected = {
        "liquidityGross": int(info.liquidity_gross),
        "liquidityNet": int(info.liquidity_net),
    }
    observed = {k: int(v) for k, v in decoded.items()}
    mismatches = [
        f"{k}: expected {expected[k]!r}, observed {observed[k]!r}"
        for k in expected
        if expected[k] != observed[k]
    ]
    passed = not mismatches
    return FieldComparison(
        method=STATE_VIEW_METHOD_GET_TICK_LIQUIDITY,
        height=height,
        endpoint_alias=pool_input.endpoint_alias,
        passed=passed,
        expected_fields=expected,
        observed_fields=observed,
        mismatch_detail=(None if passed else "; ".join(mismatches)),
    )


def _compare_get_tick_info(
    pool_input: PoolComparisonInput,
    *,
    height: int,
    tick: int,
    response: bytes,
) -> FieldComparison:
    decoded = decode_get_tick_info(response)
    info = pool_input.reconstructed_state.tick_info(int(tick))
    expected = {
        "liquidityGross": int(info.liquidity_gross),
        "liquidityNet": int(info.liquidity_net),
        "feeGrowthOutside0X128": int(info.fee_growth_outside_0),
        "feeGrowthOutside1X128": int(info.fee_growth_outside_1),
    }
    observed = {k: int(v) for k, v in decoded.items()}
    mismatches = [
        f"{k}: expected {expected[k]!r}, observed {observed[k]!r}"
        for k in expected
        if expected[k] != observed[k]
    ]
    passed = not mismatches
    return FieldComparison(
        method=STATE_VIEW_METHOD_GET_TICK_INFO,
        height=height,
        endpoint_alias=pool_input.endpoint_alias,
        passed=passed,
        expected_fields=expected,
        observed_fields=observed,
        mismatch_detail=(None if passed else "; ".join(mismatches)),
    )


def _compare_get_fee_growth_globals(
    pool_input: PoolComparisonInput,
    *,
    height: int,
    response: bytes,
) -> FieldComparison:
    decoded = decode_get_fee_growth_globals(response)
    # The replay does not reconstruct fee-growth-global values
    # (T041 acceptance: fee growth global is owned by T051 / T043).
    # When the read list schedules getFeeGrowthGlobals the runner
    # still records the endpoint payload so the audit trail
    # surfaces it, but the comparison is the "end-to-end
    # fee-growth-global carrier" the operator expects (zero when
    # not yet reconstructed).
    expected = {
        "feeGrowthGlobal0X128": 0,
        "feeGrowthGlobal1X128": 0,
    }
    observed = {k: int(v) for k, v in decoded.items()}
    mismatches = [
        f"{k}: expected {expected[k]!r}, observed {observed[k]!r}"
        for k in expected
        if expected[k] != observed[k]
    ]
    passed = not mismatches
    return FieldComparison(
        method=STATE_VIEW_METHOD_GET_FEE_GROWTH_GLOBALS,
        height=height,
        endpoint_alias=pool_input.endpoint_alias,
        passed=passed,
        expected_fields=expected,
        observed_fields=observed,
        mismatch_detail=(None if passed else "; ".join(mismatches)),
    )


def _compare_get_tick_fee_growth_outside(
    pool_input: PoolComparisonInput,
    *,
    height: int,
    tick: int,
    response: bytes,
) -> FieldComparison:
    decoded = decode_get_tick_fee_growth_outside(response)
    info = pool_input.reconstructed_state.tick_info(int(tick))
    expected = {
        "feeGrowthOutside0X128": int(info.fee_growth_outside_0),
        "feeGrowthOutside1X128": int(info.fee_growth_outside_1),
    }
    observed = {k: int(v) for k, v in decoded.items()}
    mismatches = [
        f"{k}: expected {expected[k]!r}, observed {observed[k]!r}"
        for k in expected
        if expected[k] != observed[k]
    ]
    passed = not mismatches
    return FieldComparison(
        method=STATE_VIEW_METHOD_GET_TICK_FEE_GROWTH_OUTSIDE,
        height=height,
        endpoint_alias=pool_input.endpoint_alias,
        passed=passed,
        expected_fields=expected,
        observed_fields=observed,
        mismatch_detail=(None if passed else "; ".join(mismatches)),
    )


def _compare_one_read(
    pool_input: PoolComparisonInput,
    *,
    height: int,
    read: StateViewReadSpec,
    response: bytes,
) -> FieldComparison:
    """Dispatch one (height, read) comparison to the matching comparator."""
    method = read.method
    if method == STATE_VIEW_METHOD_GET_SLOT_0:
        return _compare_slot_0(pool_input, height=height, response=response)
    if method == STATE_VIEW_METHOD_GET_LIQUIDITY:
        return _compare_get_liquidity(pool_input, height=height, response=response)
    if method == STATE_VIEW_METHOD_GET_TICK_BITMAP:
        (word_pos,) = read.extra_args
        return _compare_get_tick_bitmap(
            pool_input, height=height, word_pos=word_pos, response=response
        )
    if method == STATE_VIEW_METHOD_GET_TICK_LIQUIDITY:
        (tick,) = read.extra_args
        return _compare_get_tick_liquidity(pool_input, height=height, tick=tick, response=response)
    if method == STATE_VIEW_METHOD_GET_TICK_INFO:
        (tick,) = read.extra_args
        return _compare_get_tick_info(pool_input, height=height, tick=tick, response=response)
    if method == STATE_VIEW_METHOD_GET_FEE_GROWTH_GLOBALS:
        return _compare_get_fee_growth_globals(pool_input, height=height, response=response)
    if method == STATE_VIEW_METHOD_GET_TICK_FEE_GROWTH_OUTSIDE:
        (tick,) = read.extra_args
        return _compare_get_tick_fee_growth_outside(
            pool_input, height=height, tick=tick, response=response
        )
    raise ValueError(
        f"_compare_one_read: method {method!r} is in VALID_STATE_VIEW_METHODS "
        f"but has no comparator (T042 contract keeps "
        f"getFeeGrowthInside as a placeholder)"
    )


def _run_pool_comparison(
    pool_input: PoolComparisonInput,
    *,
    logical_call_ceiling: int,
    compute_unit_ceiling: int,
) -> PoolStateComparisonReport:
    """Run the per-pool comparison and return the per-pool report.

    The runner walks the recorded per-height read list in order; for
    each read it issues one block-pinned StateView call, tracks the
    budget, and emits either a :class:`FieldComparison`,
    :class:`BlockedCheckRecord`, or stops with a
    :class:`BudgetExhaustionRecord` when a ceiling is exceeded. The
    order is the recorded acceptance surface; the runner never
    reorders, deduplicates, or reduces coverage.
    """
    field_comparisons: list[FieldComparison] = []
    blocked_checks: list[BlockedCheckRecord] = []
    logical_calls_used = 0
    compute_units_used = 0
    budget_exhaustion: BudgetExhaustionRecord | None = None
    heights_compared: list[int] = []
    per_height_method_set: list[str] = []
    pool_id_bytes = pool_input.pool_id.to_bytes()
    for height, reads in pool_input.per_height_reads:
        if budget_exhaustion is not None:
            # A budget halt stops the pool's run; the remaining
            # heights are not attempted. The recorded heights list
            # only carries the heights the runner reached (before
            # the halt) so the audit trail matches the budget shape.
            break
        heights_compared.append(int(height))
        for read in reads:
            method = read.method
            per_height_method_set.append(method)
            cost = _compute_unit_cost(method)
            next_logical = logical_calls_used + 1
            next_compute = compute_units_used + cost
            # Budget check happens before issuing the read so a
            # run that would exceed the ceiling halts rather than
            # issuing the read.
            if next_logical > logical_call_ceiling:
                budget_exhaustion = BudgetExhaustionRecord(
                    pool_alias=pool_input.pool_alias,
                    dimension="logical_calls",
                    used_at_halt=next_logical,
                    ceiling=logical_call_ceiling,
                    halt_height=int(height),
                    halt_method=method,
                )
                break
            if next_compute > compute_unit_ceiling:
                budget_exhaustion = BudgetExhaustionRecord(
                    pool_alias=pool_input.pool_alias,
                    dimension="compute_units",
                    used_at_halt=next_compute,
                    ceiling=compute_unit_ceiling,
                    halt_height=int(height),
                    halt_method=method,
                )
                break
            block_tag = "0x" + format(int(height), "x")
            response = pool_input.state_call(
                method,
                block_tag,
                pool_id_bytes,
                tuple(int(a) for a in read.extra_args),
            )
            logical_calls_used = next_logical
            compute_units_used = next_compute
            if response is None:
                blocked_checks.append(
                    BlockedCheckRecord(
                        pool_alias=pool_input.pool_alias,
                        height=int(height),
                        method=method,
                        endpoint_alias=pool_input.endpoint_alias,
                        reason_code=BLOCK_REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
                    )
                )
                continue
            field_comparisons.append(
                _compare_one_read(
                    pool_input,
                    height=int(height),
                    read=read,
                    response=response,
                )
            )
        if budget_exhaustion is not None:
            break
    return PoolStateComparisonReport(
        pool_alias=pool_input.pool_alias,
        chain_id_value=int(pool_input.chain_id.value),
        pool_id_hex=pool_input.pool_id.to_hex(),
        pool_id_int=int(pool_input.pool_id.value),
        heights_compared=tuple(heights_compared),
        per_height_read_list=tuple(dict.fromkeys(per_height_method_set)),
        logical_calls_used=int(logical_calls_used),
        compute_units_used=int(compute_units_used),
        logical_call_ceiling=int(logical_call_ceiling),
        compute_unit_ceiling=int(compute_unit_ceiling),
        field_comparisons=tuple(field_comparisons),
        blocked_checks=tuple(blocked_checks),
        budget_exhaustion=budget_exhaustion,
        partition_reconciliation_consistent=bool(pool_input.partition_reconciliation_consistent),
        endpoint_alias=pool_input.endpoint_alias,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_state_comparison(
    pool_inputs: tuple[PoolComparisonInput, ...],
    *,
    logical_call_ceiling_per_pool: int = DEFAULT_LOGICAL_CALL_CEILING_PER_POOL,
    compute_unit_ceiling_per_pool: int = DEFAULT_COMPUTE_UNIT_CEILING_PER_POOL,
) -> StateComparisonReport:
    """Run the block-pinned StateView comparison for every pool.

    The function fixes and records the comparison bounds (per-pool
    ceiling, recorded per-height read vocabulary), then runs the
    per-pool comparison in input order. The returned report carries
    the per-pool verdicts; ``overall_passes`` is the AND of the
    per-pool verdicts — one pool's agreement is never presented as
    the other pool's result (the contract's "one pool's agreement
    never presented as the other pool's result" clause is enforced
    by the per-pool report structure, not by any re-aggregation).
    """
    if not isinstance(pool_inputs, tuple):
        raise TypeError(
            f"run_state_comparison: pool_inputs must be tuple, got {type(pool_inputs).__name__}"
        )
    if not pool_inputs:
        raise ValueError("run_state_comparison: at least one PoolComparisonInput is required")
    seen_aliases: set[str] = set()
    per_height_read_vocabulary: list[str] = []
    for entry in pool_inputs:
        if not isinstance(entry, PoolComparisonInput):
            raise TypeError(
                f"run_state_comparison: each pool_inputs entry must be "
                f"PoolComparisonInput, got {type(entry).__name__}"
            )
        if entry.pool_alias in seen_aliases:
            raise ValueError(
                f"run_state_comparison: duplicate pool_alias "
                f"{entry.pool_alias!r}; the per-pool report structure "
                f"requires unique aliases"
            )
        seen_aliases.add(entry.pool_alias)
        for _, reads in entry.per_height_reads:
            for read in reads:
                if read.method not in per_height_read_vocabulary:
                    per_height_read_vocabulary.append(read.method)

    bounds = RecordedComparisonBounds(
        heights_per_pool=len(pool_inputs[0].per_height_reads),
        per_height_read_list=tuple(per_height_read_vocabulary),
        logical_call_ceiling_per_pool=int(logical_call_ceiling_per_pool),
        compute_unit_ceiling_per_pool=int(compute_unit_ceiling_per_pool),
    )
    pool_reports = tuple(
        _run_pool_comparison(
            entry,
            logical_call_ceiling=logical_call_ceiling_per_pool,
            compute_unit_ceiling=compute_unit_ceiling_per_pool,
        )
        for entry in pool_inputs
    )
    return StateComparisonReport(pool_reports=pool_reports, bounds=bounds)


__all__ = [
    "BLOCK_REASON_CROSS_ENDPOINT_SAMPLE_MISSING",
    "BlockPinnedStateViewCall",
    "BlockedCheckRecord",
    "BudgetExhaustionRecord",
    "COMPUTE_UNIT_COST_GET_FEE_GROWTH_GLOBALS",
    "COMPUTE_UNIT_COST_GET_FEE_GROWTH_INSIDE",
    "COMPUTE_UNIT_COST_GET_LIQUIDITY",
    "COMPUTE_UNIT_COST_GET_SLOT_0",
    "COMPUTE_UNIT_COST_GET_TICK_BITMAP",
    "COMPUTE_UNIT_COST_GET_TICK_FEE_GROWTH_OUTSIDE",
    "COMPUTE_UNIT_COST_GET_TICK_INFO",
    "COMPUTE_UNIT_COST_GET_TICK_LIQUIDITY",
    "DEFAULT_COMPUTE_UNIT_CEILING_PER_POOL",
    "DEFAULT_LOGICAL_CALL_CEILING_PER_POOL",
    "FieldComparison",
    "LOWER_UPPER_ARG_METHODS",
    "PoolComparisonInput",
    "PoolStateComparisonReport",
    "RecordedComparisonBounds",
    "STATE_VIEW_METHOD_GET_FEE_GROWTH_GLOBALS",
    "STATE_VIEW_METHOD_GET_FEE_GROWTH_INSIDE",
    "STATE_VIEW_METHOD_GET_LIQUIDITY",
    "STATE_VIEW_METHOD_GET_SLOT_0",
    "STATE_VIEW_METHOD_GET_TICK_BITMAP",
    "STATE_VIEW_METHOD_GET_TICK_FEE_GROWTH_OUTSIDE",
    "STATE_VIEW_METHOD_GET_TICK_INFO",
    "STATE_VIEW_METHOD_GET_TICK_LIQUIDITY",
    "StateComparisonReport",
    "StateViewReadSpec",
    "TICK_ARG_METHODS",
    "VALID_STATE_VIEW_METHODS",
    "WORDPOS_ARG_METHODS",
    "decode_get_fee_growth_globals",
    "decode_get_liquidity",
    "decode_get_slot_0",
    "decode_get_tick_bitmap",
    "decode_get_tick_fee_growth_outside",
    "decode_get_tick_info",
    "decode_get_tick_liquidity",
    "make_block_pinned_state_view_call",
    "replay_state_at_height",
    "run_state_comparison",
]
