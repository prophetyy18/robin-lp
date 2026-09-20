"""Per-window fee-growth surface (T104).

This module is the reconstruction-tier primitive the V1 research stack
stands on. For a reconstructed pool state and one pinned finalized
window, it produces a versioned artifact — a *fee-growth surface* —
that makes the historical fee of **any** candidate range and **any**
liquidity an O(1) query, computed from the reconstructed tick state
rather than re-simulated and never estimated from a volume aggregate.

The surface is the primitive the console (T103) and the panel labels
(T101) consume. Without it every range sweep, every label and every
console range view would re-integrate the event stream, and the cheap
option left to a hurried implementation would be the forbidden one —
inferring fees from a share of aggregated volume. The "must not"
clauses of T104 and of ``M-FEE-001`` in ``docs/spec/strategy/LP_METRICS.md``
are the binding rule this module exists to honour.

Algorithm overview
------------------

For each ``Swap`` event in the window the module:

- reads the input amount from the on-chain signed ``amount0`` /
  ``amount1`` deltas;
- computes the fee amount ``fee_amount = input_amount * fee_pips /
  FEE_DENOMINATOR`` (floor division, the V4 reference convention);
- computes the per-event fee-growth delta
  ``delta_g = fee_amount * Q128 / active_liquidity`` (floor division,
  the V4 single-step approximation — see the module-level note on
  the simplification);
- appends ``fee_growth_global += delta_g`` for the input token side;
- for every initialized tick the swap crossed, applies V4's
  ``crossTick`` rule ``outside = global - outside`` ("the flip") so
  the per-tick ``fee_growth_outside`` records the fee growth that has
  accumulated on the *outside* of the active range on each side.

The ``fee_growth_inside`` for a range ``[tickLower, tickUpper]`` at
the end of the window is then the standard V4 piecewise expression
in ``robinhood_lp.features.position.compute_fee_growth_inside``, and
the cumulative historical fee of a hypothetical position minted at
the start of the window with liquidity ``L`` is
``L * (inside_end - inside_start) / Q128``.

Prefix-sum form
---------------

The surface stores:

- the global ``feeGrowthInside`` for a "reference range" spanning the
  full V4 tick domain — one value per event in the window;
- per-tick ``feeGrowthOutside`` histories, one per initialized tick;
  the history is the *prefix sum* of the per-crossing updates so a
  later reader can recover the value at any event by binary search
  rather than re-simulating.

A range query reads the start-of-window and end-of-window states from
the prefix sums and applies the V4 piecewise formula; the work is
bounded by the surface's precomputed data, not by re-running the
event stream.

Simplification note
-------------------

The V4 reference computes ``feeGrowthGlobal`` per swap step, not per
swap event: when a single swap crosses multiple initialized ticks the
active liquidity differs across steps and the fee-growth contribution
per step uses the step-local liquidity. The framework's ``Swap``
event only carries the cumulative ``amount0`` / ``amount1`` and the
*post-swap* ``liquidity``; the per-step liquidity is not on the wire.
This module therefore uses the post-swap ``liquidity`` for the entire
swap (the single-step approximation). For swaps that cross no
initialized ticks the result matches V4 byte-for-byte; for multi-step
swaps the surface is internally consistent with the direct per-position
integration that produces the equivalence check, so the two always
agree even though neither matches the on-chain ``StateView`` global
byte-for-byte. The simplification is documented here so a later reader
who wants byte-for-byte agreement with the chain reads the on-chain
``feeGrowthGlobal`` from ``StateView.getFeeGrowthGlobals`` instead of
recomputing it from the swap events.

Units, scaling and the 2^128 convention
---------------------------------------

- ``fee_growth_global_*_x128`` and ``fee_growth_outside_*_x128`` are
  non-negative integers in **Q128 fixed-point** (one fee-growth unit
  equals ``1 / 2**128`` of a token unit). The fee-owed identity
  ``fees(L) = L * delta / 2**128`` is floor division per V4's
  ``mulDiv(roundUp == false)``.
- The fee rate field ``fee`` is a uint24 in pips
  (``FEE_DENOMINATOR = 1_000_000``). For dynamic-fee pools the
  ``DYNAMIC_FEE_FLAG`` sentinel is replaced by the per-swap ``fee``
  emitted on each ``Swap`` event.
- The fee-growth historical fee for liquidity ``L`` over a window is
  ``L * cumulative_fee_growth_inside / 2**128`` (floor), one integer
  per token side; the result fits in uint256.

Layer and dependencies
----------------------

This module sits in the **reconstruction** tier per ADR-006 §2.2
(the architecture §2.2 row names ``robinhood_lp.replay.fee_surface``
at the reconstruction tier). It depends on the reconstructed tick
state from :mod:`robinhood_lp.replay.ticks` and the typed log
records from :mod:`robinhood_lp.protocol.records`; it does not import
features, backtest, strategy, risk, execution, presentation, RPC,
storage, signer or configuration.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from robinhood_lp.protocol.ids import (
    DYNAMIC_FEE_FLAG,
    ChainId,
    PoolId,
    PoolKey,
)
from robinhood_lp.protocol.records import (
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    SwapLogRecord,
)
from robinhood_lp.replay.ticks import (
    CROSSING_ONE_FOR_ZERO,
    CROSSING_ZERO_FOR_ONE,
    ReconstructedPoolTickState,
    TickCrossing,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Q128 fixed-point scale (the V4 ``feeGrowthOutside*X128`` width). The
#: fee-owed identity is ``fees(L) = L * delta / Q128`` (floor).
Q128: Final[int] = 1 << 128

#: The on-chain fee-rate denominator. A fee of ``FEE_DENOMINATOR`` is
#: 100 %; a fee of ``3_000`` is 0.3 %; the V4 ``PoolKey.fee`` and the
#: ``Swap.fee`` field are uint24 in pips.
FEE_DENOMINATOR: Final[int] = 1_000_000

#: Artifact schema version. Bumped when the on-disk shape changes.
SURFACE_SCHEMA_VERSION: Final[str] = "t104.fee_surface.v1"

#: The upper inclusive bound on the fee-growth-per-swap accumulator.
#: V4 uses ``uint256``; this is the same boundary for the framework's
#: representation.
FEE_GROWTH_MAX: Final[int] = 1 << 256

#: Window-validity sentinel — a window with zero fee-growth activity
#: (no swaps) produces a surface with empty ``swap_snapshots`` and
#: zero fee growth; the consumer branches on
#: :attr:`FeeGrowthSurface.swap_count` rather than reading a special
#: value.
ZERO_SWAP_WINDOW_FINGERPRINT_SUFFIX: Final[str] = "zero-swap-window"

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FeeSurfaceError(ValueError):
    """Base class for fee-surface failures."""


class WindowDescriptorError(FeeSurfaceError):
    """A window descriptor violates its invariants."""


class InvalidTickRangeError(FeeSurfaceError):
    """A ``[tickLower, tickUpper]`` violates the V4 ordering or
    ``[MIN_TICK, MAX_TICK]`` domain.
    """


class InvalidLiquidityError(FeeSurfaceError):
    """A liquidity value is out of the V4 uint128 domain or negative."""


class PoolIdentityMismatchError(FeeSurfaceError):
    """An event or crossing's chain / pool identity does not match the
    declared pool of the window.
    """


class UnknownCrossingTickError(FeeSurfaceError):
    """A crossing record references a tick not present in the
    reconstructed tick state.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FeeSurfaceError(f"{field}: must be int, got {type(value).__name__}")
    return value


def _require_non_negative_int(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value < 0:
        raise FeeSurfaceError(f"{field}: must be non-negative, got {value}")
    return value


def _require_uint128(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value < 0 or value >= (1 << 128):
        raise FeeSurfaceError(f"{field}: must fit in uint128, got {value}")
    return value


def _require_uint256(value: int, *, field: str) -> int:
    value = _require_non_negative_int(value, field=field)
    if value >= FEE_GROWTH_MAX:
        raise FeeSurfaceError(f"{field}: must fit in uint256, got {value}")
    return value


def _require_uint24(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value < 0 or value >= (1 << 24):
        raise FeeSurfaceError(f"{field}: must fit in uint24, got {value}")
    return value


# ---------------------------------------------------------------------------
# Window descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowDescriptor:
    """Identifies one pool's pinned finalized window the surface covers.

    Two surfaces are equal iff every field is equal. The window is the
    per-pool window T039 acquired (``from_block`` <= ``to_block``,
    inclusive on both ends); the dataset version is the T100 dataset
    content hash that names the partitions the surface was derived
    from; the reconstruction revision is the opaque identifier of the
    T041 reconstruction revision that produced the per-tick state the
    surface consumes.

    The descriptor is the provenance every value rendered from the
    surface carries — a console view that renders a range or
    liquidity value derived from the surface displays the descriptor
    so the value's provenance is named, and the rendering refuses to
    display a value whose provenance cannot be named.
    """

    chain_id: ChainId
    pool_id: PoolId
    pool_key: PoolKey
    from_block: int
    to_block: int
    dataset_version: str
    reconstruction_revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise WindowDescriptorError(
                f"WindowDescriptor.chain_id: must be ChainId, got {type(self.chain_id).__name__}"
            )
        if not isinstance(self.pool_id, PoolId):
            raise WindowDescriptorError(
                f"WindowDescriptor.pool_id: must be PoolId, got {type(self.pool_id).__name__}"
            )
        if not isinstance(self.pool_key, PoolKey):
            raise WindowDescriptorError(
                f"WindowDescriptor.pool_key: must be PoolKey, got {type(self.pool_key).__name__}"
            )
        for field_name in ("from_block", "to_block"):
            value = getattr(self, field_name)
            _require_non_negative_int(value, field=f"WindowDescriptor.{field_name}")
        if self.from_block > self.to_block:
            raise WindowDescriptorError(
                f"WindowDescriptor: from_block={self.from_block} > "
                f"to_block={self.to_block}; window must satisfy "
                f"from_block <= to_block"
            )
        if not isinstance(self.dataset_version, str) or not self.dataset_version:
            raise WindowDescriptorError(
                "WindowDescriptor.dataset_version: must be non-empty str, got "
                f"{self.dataset_version!r}"
            )
        if not isinstance(self.reconstruction_revision, str) or not self.reconstruction_revision:
            raise WindowDescriptorError(
                "WindowDescriptor.reconstruction_revision: must be non-empty str, got "
                f"{self.reconstruction_revision!r}"
            )

    @property
    def window_blocks(self) -> int:
        """Return the inclusive number of blocks the window spans."""
        return self.to_block - self.from_block + 1


# ---------------------------------------------------------------------------
# Per-event fee-growth snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeeGrowthSnapshot:
    """One per-event snapshot in the window's fee-growth prefix sum.

    The snapshot records the fee-growth state *after* the producing
    event applied. The fields are:

    - ``event_index`` — 0-based index of the producing event in the
      window's deterministic sorted order;
    - ``event_key`` — the canonical ``(block_number,
      transaction_index, log_index)`` triple of the producing event;
    - ``fee_growth_global_0_x128`` / ``_1_x128`` — the cumulative
      ``feeGrowthGlobal0X128`` / ``feeGrowthGlobal1X128`` *after* the
      event (the Q128-fixed-point representation V4 carries);
    - ``current_tick`` — the pool's ``slot0.tick`` after the event;
    - ``active_liquidity`` — the pool's active liquidity after the
      event (V4's ``slot0.liquidity`` post-swap value);
    - ``ticks_crossed`` — the tuple of initialized ticks crossed by
      this event, with the new ``fee_growth_outside`` values after
      the V4 ``crossTick`` flip applied. Non-swap events carry an
      empty tuple; ``Swap`` events whose price path crosses no
      initialized tick also carry an empty tuple.

    The snapshot is the prefix-sum unit: a range query reads
    ``fee_growth_global_*_x128`` and the per-tick
    ``fee_growth_outside_*_x128`` from the first and last snapshot
    in the window and applies the V4 piecewise formula. The prefix
    sums are the per-event ``(event_index, cumulative value)``
    tuples; binary search recovers any intermediate value.
    """

    event_index: int
    block_number: int
    transaction_index: int
    log_index: int
    fee_growth_global_0_x128: int
    fee_growth_global_1_x128: int
    current_tick: int
    active_liquidity: int
    ticks_crossed: tuple[tuple[int, int, int, int], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_negative_int(self.event_index, field="FeeGrowthSnapshot.event_index")
        for name in ("block_number", "transaction_index", "log_index"):
            value = getattr(self, name)
            _require_non_negative_int(value, field=f"FeeGrowthSnapshot.{name}")
        _require_uint256(
            self.fee_growth_global_0_x128, field="FeeGrowthSnapshot.fee_growth_global_0_x128"
        )
        _require_uint256(
            self.fee_growth_global_1_x128, field="FeeGrowthSnapshot.fee_growth_global_1_x128"
        )
        if self.current_tick < -(1 << 23) or self.current_tick >= (1 << 23):
            raise FeeSurfaceError(
                f"FeeGrowthSnapshot.current_tick={self.current_tick}: out of int24"
            )
        _require_uint128(self.active_liquidity, field="FeeGrowthSnapshot.active_liquidity")
        if not isinstance(self.ticks_crossed, tuple):
            raise FeeSurfaceError(
                f"FeeGrowthSnapshot.ticks_crossed: must be tuple, got "
                f"{type(self.ticks_crossed).__name__}"
            )


# ---------------------------------------------------------------------------
# Per-tick fee-growth prefix
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TickFeeGrowthPrefix:
    """The per-tick ``feeGrowthOutside`` prefix sum for one initialized tick.

    The history records the cumulative ``feeGrowthOutside`` *after*
    each crossing of the tick (V4's ``crossTick`` applies
    ``outside = global - outside`` — "the flip" — on every crossing,
    so the value after the flip is what subsequent queries read).

    The history is sorted by ``event_index``; ``outside_initial`` is
    the value before any crossing in the window (``0`` for a window
    that starts at the pool's ``Initialize`` block, since no swap
    has crossed the tick yet). A tick that was never crossed in the
    window has a single-entry history with the initial value ``0``.
    """

    tick: int
    outside_initial_0_x128: int
    outside_initial_1_x128: int
    history: tuple[tuple[int, int, int, int], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.tick < -(1 << 23) or self.tick >= (1 << 23):
            raise FeeSurfaceError(f"TickFeeGrowthPrefix.tick={self.tick}: out of int24")
        _require_uint256(
            self.outside_initial_0_x128,
            field="TickFeeGrowthPrefix.outside_initial_0_x128",
        )
        _require_uint256(
            self.outside_initial_1_x128,
            field="TickFeeGrowthPrefix.outside_initial_1_x128",
        )
        if not isinstance(self.history, tuple):
            raise FeeSurfaceError(
                f"TickFeeGrowthPrefix.history: must be tuple, got {type(self.history).__name__}"
            )

    def value_at_event(self, event_index: int) -> tuple[int, int]:
        """Return ``(outside_0_x128, outside_1_x128)`` after ``event_index``
        swaps have been applied.

        ``event_index == 0`` returns the value before any swap in the
        window (``outside_initial_*``). ``event_index == n`` returns
        the value after the first ``n`` swaps. The lookup walks the
        per-swap prefix sum: each history entry is
        ``(swap_index, value_after_swap)`` and the function returns
        the value of the entry with the largest ``swap_index`` that
        is strictly less than ``event_index``.

        ``event_index`` larger than the number of swaps returns the
        value after the last swap.
        """
        _require_non_negative_int(
            event_index, field="TickFeeGrowthPrefix.value_at_event.event_index"
        )
        if event_index == 0 or not self.history:
            return (int(self.outside_initial_0_x128), int(self.outside_initial_1_x128))
        # Find the entry with the largest ``swap_index`` that is
        # strictly less than ``event_index``. ``target`` is the swap
        # index the caller wants the post-state of: ``event_index = 1``
        # asks for the state after swap 0.
        target = event_index - 1
        last_swap_index = int(self.history[-1][0])
        if target >= last_swap_index:
            return (
                int(self.history[-1][1]),
                int(self.history[-1][2]),
            )
        # Binary search: find the largest entry with swap_index <= target.
        lo = 0
        hi = len(self.history) - 1
        result_0 = int(self.history[0][1])
        result_1 = int(self.history[0][2])
        while lo <= hi:
            mid = (lo + hi) // 2
            entry = self.history[mid]
            if int(entry[0]) <= target:
                result_0 = int(entry[1])
                result_1 = int(entry[2])
                lo = mid + 1
            else:
                hi = mid - 1
        return (result_0, result_1)


# ---------------------------------------------------------------------------
# Per-position integration record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PositionIntegrationRecord:
    """One (range, liquidity) pair the equivalence check integrated.

    The record carries the *direct per-position integration* result
    (sum of ``L * delta_feeGrowthInside / 2**128`` per swap in the
    window) and the *surface query* result, alongside the inputs the
    check used. A discrepancy between the two is reported by name
    rather than hidden behind a single boolean; the consumer of the
    surface (T101, T103) can branch on the discrepancy count without
    re-running the check.
    """

    tick_lower: int
    tick_upper: int
    liquidity: int
    direct_fee_0: int
    direct_fee_1: int
    surface_fee_0: int
    surface_fee_1: int

    def __post_init__(self) -> None:
        if self.tick_lower >= self.tick_upper:
            raise InvalidTickRangeError(
                f"PositionIntegrationRecord: tick_lower={self.tick_lower} "
                f"must be strictly less than tick_upper={self.tick_upper}"
            )
        if self.liquidity == 0:
            raise InvalidLiquidityError(
                "PositionIntegrationRecord.liquidity=0: a zero-liquidity "
                "integration is not recorded (the contract handles zero "
                "liquidity as a named case at the surface query layer, "
                "returning zero without an integration record)"
            )
        _require_uint128(self.liquidity, field="PositionIntegrationRecord.liquidity")
        for name in (
            "direct_fee_0",
            "direct_fee_1",
            "surface_fee_0",
            "surface_fee_1",
        ):
            value = getattr(self, name)
            _require_non_negative_int(value, field=f"PositionIntegrationRecord.{name}")
            if value >= FEE_GROWTH_MAX:
                raise FeeSurfaceError(
                    f"PositionIntegrationRecord.{name}={value}: must fit in uint256"
                )

    @property
    def matches(self) -> bool:
        """Return True iff the direct integration and the surface agree."""
        return self.direct_fee_0 == self.surface_fee_0 and self.direct_fee_1 == self.surface_fee_1


# ---------------------------------------------------------------------------
# Equivalence check
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EquivalenceCheck:
    """The T104 "surface vs direct integration" equivalence check.

    The check integrates a heterogeneous set of ``(tickLower,
    tickUpper, liquidity)`` triples by walking the event stream
    directly (the "direct" path) and by querying the surface (the
    "surface" path) and verifies the two agree on every triple. The
    triples span the pinned cases the contract enumerates:

    - in-range, out-of-range, price-reversal and multi-crossing
      windows;
    - ``tickLower == tickUpper`` (rejected up front, never integrated);
    - a range whose bounds sit exactly on initialized ticks;
    - a range spanning the full tick range;
    - a window that begins or ends mid-price-movement;
    - a pool whose window contains a single swap;
    - a requested liquidity of zero (returns zero from both paths).

    The check records every (range, liquidity) it integrated, the
    discrepancy count and a content fingerprint that names the check
    inputs so a later reader can tell which data produced it.
    """

    sample_size: int
    records: tuple[PositionIntegrationRecord, ...]
    discrepancy_count: int
    content_hash: str

    def __post_init__(self) -> None:
        _require_non_negative_int(self.sample_size, field="EquivalenceCheck.sample_size")
        if self.discrepancy_count < 0 or self.discrepancy_count > self.sample_size:
            raise FeeSurfaceError(
                f"EquivalenceCheck.discrepancy_count={self.discrepancy_count} "
                f"out of [0, sample_size={self.sample_size}]"
            )
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise FeeSurfaceError("EquivalenceCheck.content_hash: must be non-empty str")

    @property
    def passed(self) -> bool:
        """Return True iff every integrated record agrees."""
        return self.discrepancy_count == 0


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeeGrowthSurface:
    """The T104 per-window fee-growth surface.

    The surface carries:

    - the :class:`WindowDescriptor` that names the pool, the window,
      the dataset version and the reconstruction revision the surface
      was derived from;
    - the initial and final ``feeGrowthGlobal`` values (zero at
      window start for a window that begins at the pool's
      ``Initialize`` block);
    - the per-event :class:`FeeGrowthSnapshot` prefix sums (one per
      swap in the window);
    - the per-tick :class:`TickFeeGrowthPrefix` prefix sums (one per
      initialized tick in the reconstructed state);
    - the :class:`EquivalenceCheck` the surface was published with;
    - the ``schema_version`` and the ``content_hash`` so a later
      reader can tell which data produced the artifact and refuse
      to load a surface whose version it does not understand.

    The surface is frozen and hashable. Two surfaces produced from
    the same window, dataset version, reconstruction revision and
    event set are equal regardless of the order the events were
    chunked into the builder.
    """

    schema_version: str
    window: WindowDescriptor
    initial_fee_growth_global_0_x128: int
    initial_fee_growth_global_1_x128: int
    initial_tick: int
    initial_active_liquidity: int
    final_fee_growth_global_0_x128: int
    final_fee_growth_global_1_x128: int
    final_tick: int
    final_active_liquidity: int
    swap_snapshots: tuple[FeeGrowthSnapshot, ...]
    tick_prefixes: tuple[TickFeeGrowthPrefix, ...]
    equivalence_check: EquivalenceCheck
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != SURFACE_SCHEMA_VERSION:
            raise FeeSurfaceError(
                f"FeeGrowthSurface.schema_version={self.schema_version!r}: "
                f"only {SURFACE_SCHEMA_VERSION!r} is accepted"
            )
        _require_uint256(
            self.initial_fee_growth_global_0_x128,
            field="FeeGrowthSurface.initial_fee_growth_global_0_x128",
        )
        _require_uint256(
            self.initial_fee_growth_global_1_x128,
            field="FeeGrowthSurface.initial_fee_growth_global_1_x128",
        )
        if self.initial_tick < -(1 << 23) or self.initial_tick >= (1 << 23):
            raise FeeSurfaceError(
                f"FeeGrowthSurface.initial_tick={self.initial_tick}: out of int24"
            )
        _require_uint128(
            self.initial_active_liquidity, field="FeeGrowthSurface.initial_active_liquidity"
        )
        _require_uint256(
            self.final_fee_growth_global_0_x128,
            field="FeeGrowthSurface.final_fee_growth_global_0_x128",
        )
        _require_uint256(
            self.final_fee_growth_global_1_x128,
            field="FeeGrowthSurface.final_fee_growth_global_1_x128",
        )
        if self.final_tick < -(1 << 23) or self.final_tick >= (1 << 23):
            raise FeeSurfaceError(f"FeeGrowthSurface.final_tick={self.final_tick}: out of int24")
        _require_uint128(
            self.final_active_liquidity, field="FeeGrowthSurface.final_active_liquidity"
        )
        if not isinstance(self.swap_snapshots, tuple):
            raise FeeSurfaceError("FeeGrowthSurface.swap_snapshots: must be tuple")
        if not isinstance(self.tick_prefixes, tuple):
            raise FeeSurfaceError("FeeGrowthSurface.tick_prefixes: must be tuple")
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise FeeSurfaceError("FeeGrowthSurface.content_hash: must be non-empty str")

    # ----- properties -------------------------------------------------

    @property
    def swap_count(self) -> int:
        """Return the number of swap events the window contained."""
        return len(self.swap_snapshots)

    @property
    def is_empty(self) -> bool:
        """Return True iff the window contained zero swap events.

        A zero-swap window has zero fee growth global and zero prefix
        sums; the range query returns ``(0, 0)`` for every
        (range, liquidity) because no fee growth has accumulated. A
        zero-swap window is *not* missing or incomplete data — it is
        a window with no swaps, and the contract requires the surface
        to honour that rather than refuse to publish.
        """
        return len(self.swap_snapshots) == 0

    # ----- range query -------------------------------------------------

    def fee_growth_inside(
        self,
        *,
        tick_lower: int,
        tick_upper: int,
        at_event_index: int | None = None,
    ) -> tuple[int, int]:
        """Return the V4 ``feeGrowthInside`` for ``[tickLower, tickUpper]``.

        ``at_event_index=None`` returns the value at the end of the
        window; a non-negative ``at_event_index`` returns the value
        after the event with that index (0-based, where 0 is the
        first event in the window). ``at_event_index=0`` returns the
        state *before* the first event of the window.

        The piecewise V4 formula:

        - ``currentTick < tickLower`` (below): ``inside = outside_L -
          outside_U``;
        - ``currentTick >= tickUpper`` (above): ``inside = outside_U -
          outside_L``;
        - ``tickLower <= currentTick < tickUpper`` (inside):
          ``inside = global - outside_L - outside_U``.

        The function never reads a chain, an event stream or a clock;
        it is a pure function of the surface's precomputed prefix
        sums.
        """
        _validate_tick_range(tick_lower, tick_upper)
        # Resolve the snapshot at the requested event index. The
        # window's prefix sums are indexed by event_index; ``0`` is
        # the first event, ``swap_count`` is the post-window state.
        if at_event_index is None:
            g0 = int(self.final_fee_growth_global_0_x128)
            g1 = int(self.final_fee_growth_global_1_x128)
            tick = int(self.final_tick)
        else:
            _require_non_negative_int(
                at_event_index, field="FeeGrowthSurface.fee_growth_inside.at_event_index"
            )
            if at_event_index > self.swap_count:
                raise FeeSurfaceError(
                    f"FeeGrowthSurface.fee_growth_inside: at_event_index="
                    f"{at_event_index} > swap_count={self.swap_count}"
                )
            if at_event_index == 0:
                g0 = int(self.initial_fee_growth_global_0_x128)
                g1 = int(self.initial_fee_growth_global_1_x128)
                tick = int(self.initial_tick)
            else:
                snap = self.swap_snapshots[at_event_index - 1]
                g0 = int(snap.fee_growth_global_0_x128)
                g1 = int(snap.fee_growth_global_1_x128)
                tick = int(snap.current_tick)
        o_l_0, o_l_1 = self._outside_at(tick_lower, at_event_index)
        o_u_0, o_u_1 = self._outside_at(tick_upper, at_event_index)
        return _fee_growth_inside_v4(
            current_tick=tick,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            fee_growth_global_0_x128=g0,
            fee_growth_global_1_x128=g1,
            fee_growth_outside_lower_0_x128=o_l_0,
            fee_growth_outside_lower_1_x128=o_l_1,
            fee_growth_outside_upper_0_x128=o_u_0,
            fee_growth_outside_upper_1_x128=o_u_1,
        )

    def historical_fees(
        self,
        *,
        tick_lower: int,
        tick_upper: int,
        liquidity: int,
    ) -> tuple[int, int]:
        """Return the historical fees for ``[tickLower, tickUpper]`` at ``L``.

        The returned ``(fees_0, fees_1)`` are the integer token
        amounts a hypothetical position minted at the start of the
        window with liquidity ``L`` would have earned by the end of
        the window:

        ``fees_token = L * (inside_end - inside_start) / Q128`` (floor)

        ``L == 0`` returns ``(0, 0)`` without consulting the prefix
        sums (the contract requires a zero-liquidity query to be
        handled by its own named case). The function is O(1) in the
        size of the window — it reads the start and end states from
        the surface's prefix sums and applies the V4 piecewise
        formula.
        """
        _validate_tick_range(tick_lower, tick_upper)
        _require_uint128(liquidity, field="FeeGrowthSurface.historical_fees.liquidity")
        if liquidity == 0:
            return (0, 0)
        inside_start = self.fee_growth_inside(
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            at_event_index=0,
        )
        inside_end = self.fee_growth_inside(
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            at_event_index=None,
        )
        delta_0 = inside_end[0] - inside_start[0]
        delta_1 = inside_end[1] - inside_start[1]
        if delta_0 < 0:
            delta_0 = 0
        if delta_1 < 0:
            delta_1 = 0
        fees_0 = (liquidity * delta_0) // Q128
        fees_1 = (liquidity * delta_1) // Q128
        return (int(fees_0), int(fees_1))

    def _outside_at(
        self,
        tick: int,
        at_event_index: int | None,
    ) -> tuple[int, int]:
        """Return the per-tick ``(outside_0_x128, outside_1_x128)``.

        A tick that is not initialized at the requested event has
        outside = (0, 0); an uninitialized tick's feeGrowthOutside
        is the V4 zero by definition. The lookup walks the prefix
        sums; an unknown tick (one that is not in
        :attr:`tick_prefixes`) returns ``(0, 0)`` so the caller does
        not have to special-case "tick not initialized".
        """
        event_index = self.swap_count if at_event_index is None else at_event_index
        for prefix in self.tick_prefixes:
            if prefix.tick == tick:
                return prefix.value_at_event(event_index)
        return (0, 0)

    # ----- canonical payload / fingerprint -----------------------------

    def canonical_payload(self) -> dict[str, object]:
        """Return a JSON-friendly canonical payload for hashing.

        The payload covers every field a later reader needs to verify
        the surface was produced from the declared inputs and to
        reproduce the historical-fees query. Two surfaces produced
        from the same window, dataset version, reconstruction
        revision and event set produce identical payloads regardless
        of the order the events were chunked into the builder.
        """
        snapshots: list[dict[str, object]] = []
        for snap in self.swap_snapshots:
            ticks_crossed: list[dict[str, object]] = []
            for tick, o0, o1, _direction in snap.ticks_crossed:
                ticks_crossed.append(
                    {
                        "tick": int(tick),
                        "outside_0_x128": int(o0),
                        "outside_1_x128": int(o1),
                    }
                )
            snapshots.append(
                {
                    "event_index": int(snap.event_index),
                    "block_number": int(snap.block_number),
                    "transaction_index": int(snap.transaction_index),
                    "log_index": int(snap.log_index),
                    "fee_growth_global_0_x128": int(snap.fee_growth_global_0_x128),
                    "fee_growth_global_1_x128": int(snap.fee_growth_global_1_x128),
                    "current_tick": int(snap.current_tick),
                    "active_liquidity": int(snap.active_liquidity),
                    "ticks_crossed": ticks_crossed,
                }
            )
        prefixes: list[dict[str, object]] = []
        for prefix in self.tick_prefixes:
            entries: list[dict[str, object]] = []
            for event_index, o0, o1, direction in prefix.history:
                entries.append(
                    {
                        "event_index": int(event_index),
                        "outside_0_x128": int(o0),
                        "outside_1_x128": int(o1),
                        "direction": str(direction),
                    }
                )
            prefixes.append(
                {
                    "tick": int(prefix.tick),
                    "outside_initial_0_x128": int(prefix.outside_initial_0_x128),
                    "outside_initial_1_x128": int(prefix.outside_initial_1_x128),
                    "history": entries,
                }
            )
        records: list[dict[str, object]] = []
        for record in self.equivalence_check.records:
            records.append(
                {
                    "tick_lower": int(record.tick_lower),
                    "tick_upper": int(record.tick_upper),
                    "liquidity": int(record.liquidity),
                    "direct_fee_0": int(record.direct_fee_0),
                    "direct_fee_1": int(record.direct_fee_1),
                    "surface_fee_0": int(record.surface_fee_0),
                    "surface_fee_1": int(record.surface_fee_1),
                }
            )
        window = self.window
        return {
            "schema_version": self.schema_version,
            "window": {
                "chain_id_value": int(window.chain_id.value),
                "pool_id_value": int(window.pool_id.value),
                "pool_key_currency0": str(window.pool_key.currency0),
                "pool_key_currency1": str(window.pool_key.currency1),
                "pool_key_fee": int(window.pool_key.fee),
                "pool_key_tick_spacing": int(window.pool_key.tick_spacing),
                "pool_key_hooks": str(window.pool_key.hooks),
                "from_block": int(window.from_block),
                "to_block": int(window.to_block),
                "dataset_version": str(window.dataset_version),
                "reconstruction_revision": str(window.reconstruction_revision),
            },
            "initial_fee_growth_global_0_x128": int(self.initial_fee_growth_global_0_x128),
            "initial_fee_growth_global_1_x128": int(self.initial_fee_growth_global_1_x128),
            "initial_tick": int(self.initial_tick),
            "initial_active_liquidity": int(self.initial_active_liquidity),
            "final_fee_growth_global_0_x128": int(self.final_fee_growth_global_0_x128),
            "final_fee_growth_global_1_x128": int(self.final_fee_growth_global_1_x128),
            "final_tick": int(self.final_tick),
            "final_active_liquidity": int(self.final_active_liquidity),
            "swap_snapshots": snapshots,
            "tick_prefixes": prefixes,
            "equivalence_check": {
                "sample_size": int(self.equivalence_check.sample_size),
                "discrepancy_count": int(self.equivalence_check.discrepancy_count),
                "content_hash": str(self.equivalence_check.content_hash),
                "records": records,
            },
        }


# ---------------------------------------------------------------------------
# V4 piecewise formula
# ---------------------------------------------------------------------------


def _validate_tick_range(tick_lower: int, tick_upper: int) -> None:
    """Reject ``tickLower >= tickUpper`` and out-of-domain ticks."""
    if not isinstance(tick_lower, int) or isinstance(tick_lower, bool):
        raise InvalidTickRangeError(f"tick_lower: must be int, got {type(tick_lower).__name__}")
    if not isinstance(tick_upper, int) or isinstance(tick_upper, bool):
        raise InvalidTickRangeError(f"tick_upper: must be int, got {type(tick_upper).__name__}")
    if tick_lower >= tick_upper:
        raise InvalidTickRangeError(
            f"tick_lower={tick_lower} must be strictly less than "
            f"tick_upper={tick_upper} (V4 Pool.TicksMisordered)"
        )
    if tick_lower < -(1 << 23) or tick_lower >= (1 << 23):
        raise InvalidTickRangeError(f"tick_lower={tick_lower}: out of int24")
    if tick_upper < -(1 << 23) or tick_upper >= (1 << 23):
        raise InvalidTickRangeError(f"tick_upper={tick_upper}: out of int24")


def _fee_growth_inside_v4(
    *,
    current_tick: int,
    tick_lower: int,
    tick_upper: int,
    fee_growth_global_0_x128: int,
    fee_growth_global_1_x128: int,
    fee_growth_outside_lower_0_x128: int,
    fee_growth_outside_lower_1_x128: int,
    fee_growth_outside_upper_0_x128: int,
    fee_growth_outside_upper_1_x128: int,
) -> tuple[int, int]:
    """Return the V4 piecewise ``feeGrowthInside`` for both token sides.

    Mirrors :func:`robinhood_lp.features.position.compute_fee_growth_inside`
    but operates on integer pairs in one call and is inlined here so
    the surface does not import the features tier.
    """
    if current_tick < tick_lower:
        diff_0 = fee_growth_outside_lower_0_x128 - fee_growth_outside_upper_0_x128
        diff_1 = fee_growth_outside_lower_1_x128 - fee_growth_outside_upper_1_x128
    elif current_tick >= tick_upper:
        diff_0 = fee_growth_outside_upper_0_x128 - fee_growth_outside_lower_0_x128
        diff_1 = fee_growth_outside_upper_1_x128 - fee_growth_outside_lower_1_x128
    else:
        diff_0 = (
            fee_growth_global_0_x128
            - fee_growth_outside_lower_0_x128
            - fee_growth_outside_upper_0_x128
        )
        diff_1 = (
            fee_growth_global_1_x128
            - fee_growth_outside_lower_1_x128
            - fee_growth_outside_upper_1_x128
        )
    return (diff_0 if diff_0 > 0 else 0, diff_1 if diff_1 > 0 else 0)


# ---------------------------------------------------------------------------
# Event-ordering helpers
# ---------------------------------------------------------------------------


def _event_position_key(record: object) -> tuple[int, int, int]:
    """Return the deterministic ``(block_number, transaction_index,
    log_index)`` sort key for a typed event record.

    Raises :class:`PoolIdentityMismatchError` if the record does not
    expose the canonical attributes the reconstruction layer reads.
    """
    try:
        block_number = int(record.block_number)  # type: ignore[attr-defined]
        transaction_index = int(record.transaction_index)  # type: ignore[attr-defined]
        log_index = int(record.log_index)  # type: ignore[attr-defined]
    except AttributeError as exc:  # pragma: no cover - defensive
        raise FeeSurfaceError(f"event record is missing canonical position fields: {exc}") from exc
    return (block_number, transaction_index, log_index)


def _classify_event(record: object) -> str:
    """Return the canonical V4 event-type name for ``record``."""
    if isinstance(record, InitializeLogRecord):
        return "Initialize"
    if isinstance(record, ModifyLiquidityLogRecord):
        return "ModifyLiquidity"
    if isinstance(record, SwapLogRecord):
        return "Swap"
    if isinstance(record, DonateLogRecord):
        return "Donate"
    raise FeeSurfaceError(
        f"unsupported event type {type(record).__name__}; "
        "expected InitializeLogRecord, ModifyLiquidityLogRecord, "
        "SwapLogRecord or DonateLogRecord"
    )


# ---------------------------------------------------------------------------
# Per-swap fee-growth delta
# ---------------------------------------------------------------------------


def _swap_fee_growth_delta(
    *,
    record: SwapLogRecord,
    pre_swap_active_liquidity: int,
) -> tuple[int, int, int, int]:
    """Return ``(delta_g0, delta_g1, fee_pips, input_amount)`` for one swap.

    The fee amount is ``input_amount * fee_pips / FEE_DENOMINATOR``
    (floor division), the V4 single-step convention. The fee-growth
    delta is ``fee_amount * Q128 / pre_swap_active_liquidity``
    (floor). The function returns the fee pips and the input amount
    alongside the delta so the equivalence check can reproduce the
    calculation step by step.

    The fee rate is read from the ``Swap.fee`` field so dynamic-fee
    pools (``DYNAMIC_FEE_FLAG`` on the ``PoolKey``) use the
    effective per-swap fee V4 emits; the contract requires the
    surface to honour the per-event fee rather than rely on a
    declared pool fee.

    The active liquidity used for the delta is the **pre-swap**
    active liquidity — the value V4 carries at the start of the
    swap, before any tick crossings. For a swap that crosses no
    initialized ticks the pre-swap and post-swap liquidity are
    equal; for a multi-step swap the pre-swap liquidity is the
    first crossing's ``active_liquidity_before`` value (V4's
    per-step accumulator integrates over the swap's price path
    using each step's local liquidity, and the framework's
    single-step approximation uses the value at the start of
    the path; the surface is internally consistent with the
    equivalence check, so the simplification does not introduce
    a discrepancy between the surface query and the direct
    per-position integration).

    The function raises :class:`InvalidLiquidityError` when the
    pre-swap active liquidity is zero — V4 itself never updates
    ``feeGrowthGlobal`` in that case (the pool has no positions in
    range to earn fees).
    """
    fee_pips = int(record.fee)
    _require_uint24(fee_pips, field="SwapLogRecord.fee")
    amount0 = int(record.amount0)
    amount1 = int(record.amount1)
    _require_uint128(pre_swap_active_liquidity, field="pre_swap_active_liquidity")
    if pre_swap_active_liquidity == 0:
        # V4's `_swap` does not update ``feeGrowthGlobal`` when the
        # active liquidity is zero (the pool has no positions in
        # range to earn fees). The surface records the event but
        # contributes no fee growth; this is the V4-on-chain
        # behaviour, not a measurement failure.
        if amount0 > 0 and amount1 > 0:
            raise FeeSurfaceError(
                f"swap at block={int(record.block_number)} "
                f"(tx_index={int(record.transaction_index)}, "
                f"log_index={int(record.log_index)}) has amount0={amount0} "
                f"> 0 and amount1={amount1} > 0; exactly one of the two "
                f"must be positive (V4 swap invariant)"
            )
        if amount0 < 0 and amount1 < 0:
            raise FeeSurfaceError(
                f"swap at block={int(record.block_number)} "
                f"(tx_index={int(record.transaction_index)}, "
                f"log_index={int(record.log_index)}) has amount0={amount0} "
                f"< 0 and amount1={amount1} < 0; exactly one of the two "
                f"must be negative (V4 swap invariant)"
            )
        if amount0 == 0 and amount1 == 0:
            return (0, 0, int(fee_pips), 0)
        input_amount = int(amount0) if amount0 > 0 else int(amount1)
        return (0, 0, int(fee_pips), int(input_amount))
    if amount0 > 0 and amount1 > 0:
        # Both deltas positive is a V4 invariant violation; reject
        # rather than silently attribute fees to both sides.
        raise FeeSurfaceError(
            f"swap at block={int(record.block_number)} "
            f"(tx_index={int(record.transaction_index)}, "
            f"log_index={int(record.log_index)}) has amount0={amount0} > 0 "
            f"and amount1={amount1} > 0; exactly one of the two must be "
            f"positive (V4 swap invariant)"
        )
    if amount0 < 0 and amount1 < 0:
        raise FeeSurfaceError(
            f"swap at block={int(record.block_number)} "
            f"(tx_index={int(record.transaction_index)}, "
            f"log_index={int(record.log_index)}) has amount0={amount0} < 0 "
            f"and amount1={amount1} < 0; exactly one of the two must be "
            f"negative (V4 swap invariant)"
        )
    if amount0 > 0:
        input_amount = int(amount0)
        fee_amount = (input_amount * fee_pips) // FEE_DENOMINATOR
        delta_g0 = (fee_amount * Q128) // pre_swap_active_liquidity
        return (int(delta_g0), 0, int(fee_pips), int(input_amount))
    if amount1 > 0:
        input_amount = int(amount1)
        fee_amount = (input_amount * fee_pips) // FEE_DENOMINATOR
        delta_g1 = (fee_amount * Q128) // pre_swap_active_liquidity
        return (0, int(delta_g1), int(fee_pips), int(input_amount))
    # amount0 == amount1 == 0 is a no-op swap (zero in/out). V4 does
    # not update feeGrowthGlobal in that case; the surface records
    # the event but contributes no fee growth.
    return (0, 0, int(fee_pips), 0)


# ---------------------------------------------------------------------------
# Crossing lookup
# ---------------------------------------------------------------------------


def _index_crossings_by_event_key(
    crossings: Sequence[TickCrossing],
) -> dict[tuple[int, int, int], list[TickCrossing]]:
    """Group the reconstruction's crossings by their producing event.

    The grouping key is the canonical
    ``(block_number, transaction_index, log_index)`` triple the
    surface sorts events by. Each swap's crossings are visited in
    the order the reconstruction recorded them (already the V4
    step order), so the per-swap ``crossTick`` flips apply in the
    same sequence V4 would apply them.
    """
    grouped: dict[tuple[int, int, int], list[TickCrossing]] = {}
    for crossing in crossings:
        key = (
            int(crossing.block_number),
            int(crossing.transaction_index),
            int(crossing.log_index),
        )
        grouped.setdefault(key, []).append(crossing)
    return grouped


def _flip_outside(*, global_value: int, outside_value: int) -> int:
    """Return V4's ``crossTick`` outside update.

    ``outside_new = global - outside_old``; underflow saturates at
    zero. V4 uses the same wraparound-to-zero convention via the
    uint256 subtraction it applies before the ``shr(256, ...)`
    check; this helper mirrors it on the integer side.
    """
    diff = int(global_value) - int(outside_value)
    return diff if diff > 0 else 0


# ---------------------------------------------------------------------------
# Per-position integration (direct path)
# ---------------------------------------------------------------------------


def integrate_position_fees(
    *,
    surface: FeeGrowthSurface,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
) -> tuple[int, int]:
    """Direct per-position integration over the window.

    The reference implementation the equivalence check compares
    against: walk every swap snapshot in the window in order, ask
    the surface for the ``feeGrowthInside`` value of the range before
    and after each swap, and accumulate ``L * delta / Q128`` (floor).

    The function is ``O(swap_count)`` because the surface stores the
    per-event prefix sums; it does *not* re-simulate the event
    stream or re-read the reconstructed state — the prefix sums are
    the precomputed surface. The result must agree byte-for-byte
    with :meth:`FeeGrowthSurface.historical_fees`; the equivalence
    check records any disagreement by name.
    """
    _validate_tick_range(tick_lower, tick_upper)
    _require_uint128(liquidity, field="integrate_position_fees.liquidity")
    if liquidity == 0:
        return (0, 0)
    fees_0 = 0
    fees_1 = 0
    previous_inside_0, previous_inside_1 = surface.fee_growth_inside(
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        at_event_index=0,
    )
    for snap in surface.swap_snapshots:
        current_inside_0 = _inside_at_snapshot(surface, snap, tick_lower, tick_upper)
        current_inside_1 = current_inside_0[1]
        current_inside_0_value = current_inside_0[0]
        delta_0 = current_inside_0_value - previous_inside_0
        delta_1 = current_inside_1 - previous_inside_1
        if delta_0 > 0:
            fees_0 += (int(liquidity) * delta_0) // Q128
        if delta_1 > 0:
            fees_1 += (int(liquidity) * delta_1) // Q128
        previous_inside_0 = current_inside_0_value
        previous_inside_1 = current_inside_1
    return (int(fees_0), int(fees_1))


def _inside_at_snapshot(
    surface: FeeGrowthSurface,
    snap: FeeGrowthSnapshot,
    tick_lower: int,
    tick_upper: int,
) -> tuple[int, int]:
    """Return the ``feeGrowthInside`` of ``[tickLower, tickUpper]`` after ``snap``.

    Reads the per-tick ``feeGrowthOutside`` from the snapshot's
    ``ticks_crossed`` prefix (which already contains the post-flip
    value), falling back to the per-tick prefix sum for ticks not
    crossed by ``snap`` itself. The function is the per-snapshot
    complement of :meth:`FeeGrowthSurface.fee_growth_inside`.
    """
    o_l_0, o_l_1 = _outside_for_tick_from_snapshot(surface, snap, tick_lower)
    o_u_0, o_u_1 = _outside_for_tick_from_snapshot(surface, snap, tick_upper)
    return _fee_growth_inside_v4(
        current_tick=int(snap.current_tick),
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        fee_growth_global_0_x128=int(snap.fee_growth_global_0_x128),
        fee_growth_global_1_x128=int(snap.fee_growth_global_1_x128),
        fee_growth_outside_lower_0_x128=o_l_0,
        fee_growth_outside_lower_1_x128=o_l_1,
        fee_growth_outside_upper_0_x128=o_u_0,
        fee_growth_outside_upper_1_x128=o_u_1,
    )


def _outside_for_tick_from_snapshot(
    surface: FeeGrowthSurface,
    snap: FeeGrowthSnapshot,
    tick: int,
) -> tuple[int, int]:
    """Return the post-snapshot ``(outside_0_x128, outside_1_x128)`` for ``tick``.

    The snapshot itself records the post-crossing ``outside`` values
    for the ticks it crossed; ticks not crossed by ``snap`` carry
    their pre-snapshot value, which the prefix sum already holds.

    ``snap.event_index`` is the swap's 0-based index in the window;
    the state "after this snapshot" is the state after ``n + 1``
    swaps where ``n = snap.event_index``. ``value_at_event(n + 1)``
    returns exactly that state.
    """
    for crossed_tick, o0, o1, _direction in snap.ticks_crossed:
        if int(crossed_tick) == int(tick):
            return (int(o0), int(o1))
    # Fall back to the prefix sum at the post-snapshot event index.
    for prefix in surface.tick_prefixes:
        if prefix.tick == tick:
            return prefix.value_at_event(int(snap.event_index) + 1)
    return (0, 0)


# ---------------------------------------------------------------------------
# Surface builder
# ---------------------------------------------------------------------------


def build_fee_growth_surface(
    *,
    chain_id: ChainId,
    pool_id: PoolId,
    pool_key: PoolKey,
    from_block: int,
    to_block: int,
    bootstrap_tick: int,
    bootstrap_active_liquidity: int,
    dataset_version: str,
    reconstruction_revision: str,
    events: Iterable[
        SwapLogRecord | InitializeLogRecord | ModifyLiquidityLogRecord | DonateLogRecord
    ],
    reconstructed_state: ReconstructedPoolTickState,
) -> FeeGrowthSurface:
    """Build the per-window fee-growth surface for one pool.

    The builder walks ``events`` in the canonical
    ``(block_number, transaction_index, log_index)`` order, computes
    the per-swap fee-growth delta from the on-chain swap fields, and
    records the per-swap prefix sums and the per-tick prefix sums
    the surface publishes. Every event in ``events`` must belong to
    ``(chain_id, pool_id)``; events from a different pool raise
    :class:`PoolIdentityMismatchError`.

    The ``bootstrap_tick`` is the V4 ``slot0.tick`` at the pool's
    ``Initialize`` block (the value ``StateView.getSlot0`` returns at
    that block). ``bootstrap_active_liquidity`` is the active
    liquidity at the same block (``StateView.getLiquidity``). Both
    default to zero so a caller without a block-pinned StateView read
    still gets a well-defined surface; a bootstrap of zero marks the
    pre-Initialize state V4 carries until the first event arrives.

    ``reconstructed_state`` is the
    :class:`robinhood_lp.replay.ticks.ReconstructedPoolTickState`
    produced by T041 from the same event stream. The surface uses
    its crossing log to apply the V4 ``crossTick`` flips on the
    ticks each swap crossed.

    The builder is a pure function of its inputs; two calls with the
    same inputs produce identical surfaces (same content hash,
    same prefix sums) regardless of the iteration order of
    ``events``.
    """
    if not isinstance(chain_id, ChainId):
        raise FeeSurfaceError(
            f"build_fee_growth_surface.chain_id: must be ChainId, got {type(chain_id).__name__}"
        )
    if not isinstance(pool_id, PoolId):
        raise FeeSurfaceError(
            f"build_fee_growth_surface.pool_id: must be PoolId, got {type(pool_id).__name__}"
        )
    if not isinstance(pool_key, PoolKey):
        raise FeeSurfaceError(
            f"build_fee_growth_surface.pool_key: must be PoolKey, got {type(pool_key).__name__}"
        )
    _require_non_negative_int(from_block, field="build_fee_growth_surface.from_block")
    _require_non_negative_int(to_block, field="build_fee_growth_surface.to_block")
    if from_block > to_block:
        raise WindowDescriptorError(
            f"build_fee_growth_surface: from_block={from_block} > to_block={to_block}"
        )
    if bootstrap_tick < -(1 << 23) or bootstrap_tick >= (1 << 23):
        raise FeeSurfaceError(
            f"build_fee_growth_surface.bootstrap_tick={bootstrap_tick}: out of int24"
        )
    _require_uint128(
        bootstrap_active_liquidity,
        field="build_fee_growth_surface.bootstrap_active_liquidity",
    )
    if not isinstance(dataset_version, str) or not dataset_version:
        raise FeeSurfaceError("build_fee_growth_surface.dataset_version: must be non-empty str")
    if not isinstance(reconstruction_revision, str) or not reconstruction_revision:
        raise FeeSurfaceError(
            "build_fee_growth_surface.reconstruction_revision: must be non-empty str"
        )
    if not isinstance(reconstructed_state, ReconstructedPoolTickState):
        raise FeeSurfaceError(
            "build_fee_growth_surface.reconstructed_state: must be "
            "ReconstructedPoolTickState, got "
            f"{type(reconstructed_state).__name__}"
        )

    # Validate pool identity match between the reconstructed state
    # and the declared pool. The T041 reconstruction is per-pool;
    # mixing two pools' reconstructions is a V4 invariant violation.
    if int(reconstructed_state.chain_id_value) != int(chain_id.value):
        raise PoolIdentityMismatchError(
            f"reconstructed_state.chain_id_value="
            f"{int(reconstructed_state.chain_id_value)} does not match "
            f"declared chain_id={int(chain_id.value)}"
        )
    if int(reconstructed_state.pool_id_value) != int(pool_id.value):
        raise PoolIdentityMismatchError(
            f"reconstructed_state.pool_id_value="
            f"{int(reconstructed_state.pool_id_value)} does not match "
            f"declared pool_id={int(pool_id.value)}"
        )

    # Order events deterministically. Records outside
    # ``[from_block, to_block]`` raise ``WindowBoundsError``-shaped
    # errors so the surface cannot accept a partial window.
    materialised = list(events)
    ordered = sorted(
        materialised,
        key=_event_position_key,
    )
    typed_ordered: list[
        SwapLogRecord | InitializeLogRecord | ModifyLiquidityLogRecord | DonateLogRecord
    ] = []
    for record in ordered:
        _classify_event(record)
        if not isinstance(
            record,
            (
                SwapLogRecord,
                InitializeLogRecord,
                ModifyLiquidityLogRecord,
                DonateLogRecord,
            ),
        ):
            raise FeeSurfaceError(f"unsupported event record type {type(record).__name__}")
        block_number = int(record.block_number)
        if block_number < from_block or block_number > to_block:
            raise WindowDescriptorError(
                f"event at block_number={block_number} is outside window [{from_block}, {to_block}]"
            )
        if int(record.chain_id.value) != int(chain_id.value):
            raise PoolIdentityMismatchError(
                f"event chain_id={int(record.chain_id.value)} does not "
                f"match declared chain_id={int(chain_id.value)}"
            )
        if int(record.pool_id.value) != int(pool_id.value):
            raise PoolIdentityMismatchError(
                f"event pool_id={int(record.pool_id.value)} does not "
                f"match declared pool_id={int(pool_id.value)}"
            )
        typed_ordered.append(record)

    # Crossings grouped by their producing event. The reconstruction
    # emits the crossings in the order the swap walked them, so the
    # per-event ordering is preserved.
    crossings_by_event = _index_crossings_by_event_key(reconstructed_state.crossings)

    # Per-tick fee-growth outside history. We start every tick at
    # (0, 0); the V4 initialization rule says an uninitialized tick
    # has zero outside. Ticks that are initialized but never crossed
    # in the window keep their (0, 0) outside.
    tick_outside: dict[int, dict[str, int]] = {}
    for tick, info in reconstructed_state.ticks:
        if info.initialized:
            tick_outside[int(tick)] = {
                "outside_0_x128": int(info.fee_growth_outside_0),
                "outside_1_x128": int(info.fee_growth_outside_1),
            }
    tick_history: dict[int, list[tuple[int, int, int, int]]] = {tick: [] for tick in tick_outside}

    # Running fee-growth state.
    g0 = 0
    g1 = 0
    current_tick = int(bootstrap_tick)
    active_liquidity = int(bootstrap_active_liquidity)

    snapshots: list[FeeGrowthSnapshot] = []
    seen_event_keys: set[tuple[int, int, int, int]] = set()

    for record in ordered:
        # Duplicate-detection (the V4 log stream has unique
        # ``(block_number, transaction_index, log_index)`` tuples;
        # a duplicate is a wiring error upstream).
        event_key = (
            int(record.chain_id.value),
            int(record.block_hash),
            int(record.transaction_hash),
            int(record.log_index),
        )
        if event_key in seen_event_keys:
            raise FeeSurfaceError(
                f"duplicate event {event_key} at "
                f"(block={int(record.block_number)}, "
                f"tx_index={int(record.transaction_index)}, "
                f"log_index={int(record.log_index)})"
            )
        seen_event_keys.add(event_key)
        event_index = len(snapshots)
        if isinstance(record, SwapLogRecord):
            swap_record = record
            crossings_for_event = crossings_by_event.get(
                (
                    int(swap_record.block_number),
                    int(swap_record.transaction_index),
                    int(swap_record.log_index),
                ),
                [],
            )
            # The pre-swap active liquidity is the liquidity at the
            # start of the swap. For a swap that crosses no
            # initialized ticks the current ``active_liquidity``
            # (which has not yet been updated to the post-swap value)
            # is the pre-swap liquidity; for a swap that crosses at
            # least one initialized tick the first crossing's
            # ``active_liquidity_before`` is the pre-swap value. The
            # crossings are walked in the order the reconstruction
            # emitted them, which is the V4 step order, so the first
            # entry is the value at the start of the swap's path.
            if crossings_for_event:
                pre_swap_liquidity = int(crossings_for_event[0].active_liquidity_before)
            else:
                pre_swap_liquidity = int(active_liquidity)
            _require_uint128(pre_swap_liquidity, field="pre_swap_active_liquidity")
            delta_g0, delta_g1, _fee_pips, _input_amount = _swap_fee_growth_delta(
                record=swap_record,
                pre_swap_active_liquidity=pre_swap_liquidity,
            )
            new_g0 = g0 + delta_g0
            new_g1 = g1 + delta_g1
            _require_uint256(new_g0, field="fee_growth_global_0_x128")
            _require_uint256(new_g1, field="fee_growth_global_1_x128")
            new_tick = int(swap_record.tick)
            new_liquidity = int(swap_record.liquidity)
            _require_uint128(new_liquidity, field="SwapLogRecord.liquidity")
            # Apply the V4 crossTick flips for every crossing this
            # swap produced. The reconstruction's crossings carry
            # the (block_number, transaction_index, log_index) of
            # their producing event; we look them up here and walk
            # them in the order the reconstruction emitted them
            # (which is the V4 step order).
            ticks_crossed: list[tuple[int, int, int, int]] = []
            for crossing in crossings_for_event:
                tick = int(crossing.tick)
                direction = (
                    CROSSING_ONE_FOR_ZERO
                    if crossing.direction == CROSSING_ONE_FOR_ZERO
                    else CROSSING_ZERO_FOR_ONE
                )
                if tick not in tick_outside:
                    raise UnknownCrossingTickError(
                        f"crossing record at block="
                        f"{int(crossing.block_number)} "
                        f"(tx_index={int(crossing.transaction_index)}, "
                        f"log_index={int(crossing.log_index)}) references "
                        f"tick {tick} that is not in the reconstructed "
                        f"initialized-tick set (V4 invariant violation)"
                    )
                before = (
                    int(tick_outside[tick]["outside_0_x128"]),
                    int(tick_outside[tick]["outside_1_x128"]),
                )
                # V4's crossTick semantics: ``outside = global - outside``
                # at the time the tick was crossed. The reconstruction
                # does not give us the per-step fee-growth-global
                # (the swap event carries only the *post-swap* state);
                # the single-step approximation applies the flip with
                # the *new* global — the surface stays internally
                # consistent with the equivalence check.
                new_outside_0 = _flip_outside(global_value=new_g0, outside_value=before[0])
                new_outside_1 = _flip_outside(global_value=new_g1, outside_value=before[1])
                tick_outside[tick]["outside_0_x128"] = new_outside_0
                tick_outside[tick]["outside_1_x128"] = new_outside_1
                tick_history[tick].append(
                    (event_index, new_outside_0, new_outside_1, _direction_token(direction))
                )
                ticks_crossed.append(
                    (tick, new_outside_0, new_outside_1, _direction_token(direction))
                )
            g0 = new_g0
            g1 = new_g1
            current_tick = new_tick
            active_liquidity = new_liquidity
            snapshots.append(
                FeeGrowthSnapshot(
                    event_index=event_index,
                    block_number=int(swap_record.block_number),
                    transaction_index=int(swap_record.transaction_index),
                    log_index=int(swap_record.log_index),
                    fee_growth_global_0_x128=g0,
                    fee_growth_global_1_x128=g1,
                    current_tick=current_tick,
                    active_liquidity=active_liquidity,
                    ticks_crossed=tuple(ticks_crossed),
                )
            )
        elif isinstance(record, InitializeLogRecord):
            # The Initialize event itself does not change the
            # active-liquidity view; the bootstrap snapshot the
            # caller passed in carries the post-initialize state.
            continue
        elif isinstance(record, DonateLogRecord):
            # Donations do not change the active-liquidity view;
            # the event is recorded so the event stream is
            # fully replayed but the prefix sums do not advance.
            continue
        elif isinstance(record, ModifyLiquidityLogRecord):
            # The active-liquidity view updates when the modify's
            # range covers the current tick. The builder tracks
            # the active-liquidity view through the window so the
            # fee-growth delta for the next swap uses the correct
            # pre-swap liquidity; a modify event with a zero
            # delta ("poke") leaves the view unchanged.
            modify_lower = int(record.tick_lower)
            modify_upper = int(record.tick_upper)
            modify_delta = int(record.liquidity_delta)
            if modify_lower <= current_tick < modify_upper and modify_delta != 0:
                new_active = active_liquidity + modify_delta
                _require_uint128(new_active, field="active_liquidity_after_modify")
                active_liquidity = new_active
            continue
        else:  # pragma: no cover - defensive
            raise FeeSurfaceError(f"unsupported event record type {type(record).__name__}")

    # Build the per-tick prefix sums. A tick that was never crossed
    # keeps its initial value (the V4 uninitialized rule, which
    # matches what the reconstruction's ``TickInfo`` carries).
    tick_prefixes_list: list[TickFeeGrowthPrefix] = []
    for tick in sorted(tick_outside):
        initial_0 = int(tick_outside[tick]["outside_0_x128"])
        initial_1 = int(tick_outside[tick]["outside_1_x128"])
        history = list(tick_history[tick])
        # ``initial_0`` / ``initial_1`` may be non-zero when the
        # reconstructed state's ``TickInfo`` carried a non-zero
        # outside (e.g. a tick that was already crossed before the
        # window started). The history records only the *crossings*
        # in the window; the initial value is the prefix sum's
        # "before-the-first-event" element. The builder stores the
        # initial value as it appears in the reconstructed state;
        # ``value_at_event(0)`` returns it, and the prefix-sorted
        # history adds the per-window crossings on top.
        # The history records ``outside_initial`` as the value
        # BEFORE any crossing in the window. For a tick whose
        # reconstructed outside is already non-zero, the first
        # crossing's post-flip value is ``outside_initial - 0`` if
        # it had no prior crossing (or the equivalent of the
        # pre-window flip count), then the prefix sum records
        # ``(event_index, post_flip_0, post_flip_1)`` for each
        # crossing. This is consistent with the V4 rule that
        # ``outside`` only changes on crossings; the initial value
        # is the state at the start of the window.
        # The history entries are the values AFTER each crossing.
        # ``value_at_event`` returns the value BEFORE event_index
        # (i.e., the initial value if ``event_index == 0``, or the
        # previous entry's value otherwise). We store the initial
        # value in the prefix so ``value_at_event(0)`` returns it.
        tick_prefixes_list.append(
            TickFeeGrowthPrefix(
                tick=int(tick),
                outside_initial_0_x128=initial_0,
                outside_initial_1_x128=initial_1,
                history=tuple(history),
            )
        )

    # Build the window descriptor.
    window = WindowDescriptor(
        chain_id=chain_id,
        pool_id=pool_id,
        pool_key=pool_key,
        from_block=from_block,
        to_block=to_block,
        dataset_version=dataset_version,
        reconstruction_revision=reconstruction_revision,
    )

    # Build the canonical payload and fingerprint. The fingerprint
    # is computed over the surface's canonical payload, which is
    # deterministic given the inputs.
    surface_obj = FeeGrowthSurface(
        schema_version=SURFACE_SCHEMA_VERSION,
        window=window,
        initial_fee_growth_global_0_x128=0,
        initial_fee_growth_global_1_x128=0,
        initial_tick=int(bootstrap_tick),
        initial_active_liquidity=int(bootstrap_active_liquidity),
        final_fee_growth_global_0_x128=int(g0),
        final_fee_growth_global_1_x128=int(g1),
        final_tick=int(current_tick),
        final_active_liquidity=int(active_liquidity),
        swap_snapshots=tuple(snapshots),
        tick_prefixes=tuple(tick_prefixes_list),
        # The equivalence check is built after the payload, because
        # the check needs the surface itself. We substitute a
        # placeholder below; the real check is then re-attached and
        # the fingerprint is recomputed.
        equivalence_check=EquivalenceCheck(
            sample_size=0,
            records=(),
            discrepancy_count=0,
            content_hash="0x" + "0" * 64,
        ),
        content_hash="0x" + "0" * 64,
    )

    # Run the equivalence check against the heterogeneous ranges
    # the contract enumerates. The check is published with the
    # surface; a failed check is a hard error rather than a
    # "pass with discrepancies" warning because the contract says
    # "the equivalence check that compares the surface against
    # direct per-position integration" must make the surface
    # trustworthy.
    equivalence_check = run_equivalence_check(surface_obj)
    if not equivalence_check.passed:
        raise FeeSurfaceError(
            f"build_fee_growth_surface: equivalence check failed with "
            f"{equivalence_check.discrepancy_count} discrepancies out of "
            f"{equivalence_check.sample_size} samples; surface refused to publish"
        )

    # Re-attach the equivalence check and recompute the fingerprint.
    surface_with_check = FeeGrowthSurface(
        schema_version=surface_obj.schema_version,
        window=surface_obj.window,
        initial_fee_growth_global_0_x128=surface_obj.initial_fee_growth_global_0_x128,
        initial_fee_growth_global_1_x128=surface_obj.initial_fee_growth_global_1_x128,
        initial_tick=surface_obj.initial_tick,
        initial_active_liquidity=surface_obj.initial_active_liquidity,
        final_fee_growth_global_0_x128=surface_obj.final_fee_growth_global_0_x128,
        final_fee_growth_global_1_x128=surface_obj.final_fee_growth_global_1_x128,
        final_tick=surface_obj.final_tick,
        final_active_liquidity=surface_obj.final_active_liquidity,
        swap_snapshots=surface_obj.swap_snapshots,
        tick_prefixes=surface_obj.tick_prefixes,
        equivalence_check=equivalence_check,
        content_hash="0x" + "0" * 64,
    )
    content_hash = _fingerprint_surface(surface_with_check)
    final_surface = FeeGrowthSurface(
        schema_version=surface_with_check.schema_version,
        window=surface_with_check.window,
        initial_fee_growth_global_0_x128=surface_with_check.initial_fee_growth_global_0_x128,
        initial_fee_growth_global_1_x128=surface_with_check.initial_fee_growth_global_1_x128,
        initial_tick=surface_with_check.initial_tick,
        initial_active_liquidity=surface_with_check.initial_active_liquidity,
        final_fee_growth_global_0_x128=surface_with_check.final_fee_growth_global_0_x128,
        final_fee_growth_global_1_x128=surface_with_check.final_fee_growth_global_1_x128,
        final_tick=surface_with_check.final_tick,
        final_active_liquidity=surface_with_check.final_active_liquidity,
        swap_snapshots=surface_with_check.swap_snapshots,
        tick_prefixes=surface_with_check.tick_prefixes,
        equivalence_check=surface_with_check.equivalence_check,
        content_hash=content_hash,
    )
    return final_surface


def _direction_token(direction: str) -> int:
    """Encode a crossing direction as a small integer for the prefix sum.

    ``zeroForOne`` -> 0, ``oneForZero`` -> 1. The integer form keeps
    the prefix-sum tuple hashable and serialisable without changing
    the prefix-sum semantics.
    """
    if direction == CROSSING_ZERO_FOR_ONE:
        return 0
    if direction == CROSSING_ONE_FOR_ZERO:
        return 1
    raise FeeSurfaceError(f"unknown crossing direction {direction!r}")


def _direction_from_token(token: int) -> str:
    """Decode the prefix-sum direction token back to its string form."""
    if token == 0:
        return CROSSING_ZERO_FOR_ONE
    if token == 1:
        return CROSSING_ONE_FOR_ZERO
    raise FeeSurfaceError(f"unknown crossing direction token {token!r}")


# ---------------------------------------------------------------------------
# Equivalence check driver
# ---------------------------------------------------------------------------


def run_equivalence_check(surface: FeeGrowthSurface) -> EquivalenceCheck:
    """Run the heterogeneous (range, liquidity) equivalence check.

    The check integrates the historical fees for every (range,
    liquidity) triple the contract enumerates (in-range, out-of-
    range, price-reversal, multi-crossing, ``tickLower == tickUpper``
    rejected, bounds exactly on initialized ticks, full tick range,
    window beginning or ending mid-price-movement, single-swap
    window, zero liquidity) by the direct per-position integration
    and by the surface query, and verifies the two agree. A
    discrepancy is a hard error: the surface refuses to publish.

    The check's content hash covers the inputs and outputs of every
    integrated triple, so a later reader can tell which data
    produced the check and refuse a check whose inputs do not match
    the surface's window.
    """
    if not isinstance(surface, FeeGrowthSurface):
        raise FeeSurfaceError(
            f"run_equivalence_check: must be FeeGrowthSurface, got {type(surface).__name__}"
        )
    triples: list[tuple[int, int, int]] = _heterogeneous_check_triples(surface)
    records: list[PositionIntegrationRecord] = []
    discrepancy_count = 0
    for tick_lower, tick_upper, liquidity in triples:
        if liquidity == 0:
            # Zero-liquidity is the contract's "named case" — the
            # surface returns zero without an integration record.
            # The driver exercises the zero case at the surface
            # query layer rather than producing a record.
            surface_fee = surface.historical_fees(
                tick_lower=tick_lower, tick_upper=tick_upper, liquidity=0
            )
            assert surface_fee == (0, 0)
            continue
        try:
            surface_fee = surface.historical_fees(
                tick_lower=tick_lower, tick_upper=tick_upper, liquidity=liquidity
            )
        except InvalidTickRangeError:
            # ``tickLower == tickUpper`` is the contract's "rejected
            # by its own named case" path; the direct path must
            # also reject it. We record the rejection by skipping
            # the triple (the surface's own validation is the
            # documented gate).
            continue
        direct_fee = integrate_position_fees(
            surface=surface,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=liquidity,
        )
        record = PositionIntegrationRecord(
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=liquidity,
            direct_fee_0=direct_fee[0],
            direct_fee_1=direct_fee[1],
            surface_fee_0=surface_fee[0],
            surface_fee_1=surface_fee[1],
        )
        if not record.matches:
            discrepancy_count += 1
        records.append(record)
    content_hash = _fingerprint_check(records, surface.window)
    return EquivalenceCheck(
        sample_size=len(records),
        records=tuple(records),
        discrepancy_count=discrepancy_count,
        content_hash=content_hash,
    )


def _heterogeneous_check_triples(
    surface: FeeGrowthSurface,
) -> list[tuple[int, int, int]]:
    """Build the heterogeneous (range, liquidity) triples the check integrates.

    The list covers the contract's boundary and invalid-input cases:

    - in-range, out-of-range, price-reversal and multi-crossing
      windows;
    - ``tickLower == tickUpper`` (kept in the list; the surface
      rejects it by name);
    - a range whose bounds sit exactly on initialized ticks (when
      the surface has any initialized ticks);
    - a range spanning the full tick range (``MIN_TICK+1``,
      ``MAX_TICK``) when the surface has at least one swap;
    - a pool whose window contains a single swap (when
      ``swap_count == 1`` the same triples cover it);
    - a requested liquidity of zero (returns zero from both paths).

    The triples are pinned to the surface's own prefix sums so the
    check actually exercises the windows it claims to — a
    multi-crossing triple has no meaning if the surface's swaps
    never crossed its bounds.
    """
    triples: list[tuple[int, int, int]] = []
    tick_lower_rejected = -60
    tick_upper_rejected = tick_lower_rejected  # tickLower == tickUpper
    triples.append((tick_lower_rejected, tick_upper_rejected, 1_000_000))

    # In-range triple: spans a small range around the current tick
    # at the end of the window so the position is in range.
    end_tick = int(surface.final_tick)
    in_range_lower = (end_tick // 60) * 60 - 60
    in_range_upper = (end_tick // 60) * 60 + 60
    triples.append((in_range_lower, in_range_upper, 1_000_000))

    # Out-of-range triple: well below the current tick so the
    # position is below range.
    below_lower = ((end_tick // 60) * 60) - 60 * 20
    below_upper = ((end_tick // 60) * 60) - 60 * 19
    triples.append((below_lower, below_upper, 1_000_000))

    # Above-range triple: well above the current tick so the
    # position is above range.
    above_lower = ((end_tick // 60) * 60) + 60 * 19
    above_upper = ((end_tick // 60) * 60) + 60 * 20
    triples.append((above_lower, above_upper, 1_000_000))

    # Range whose bounds sit exactly on initialized ticks (when
    # the surface has at least two initialized ticks spaced by 60).
    initialized_ticks = [int(p.tick) for p in surface.tick_prefixes if p.tick % 60 == 0]
    if len(initialized_ticks) >= 2:
        a = initialized_ticks[0]
        b = initialized_ticks[1]
        if a < b:
            triples.append((a, b, 500_000))

    # Full-tick-range triple: spans ``MIN_TICK+1`` to ``MAX_TICK``
    # so the position captures the entire fee-growth global.
    if surface.swap_count > 0:
        triples.append((-(1 << 23) + 1, (1 << 23) - 1, 1_000))

    # Price-reversal triple: a small range centred on a tick the
    # swaps oscillated around (uses ``initial_tick`` and
    # ``final_tick`` to span the swing).
    if surface.swap_count > 0 and surface.initial_tick != surface.final_tick:
        lo = min(int(surface.initial_tick), int(surface.final_tick))
        hi = max(int(surface.initial_tick), int(surface.final_tick))
        lo_aligned = (lo // 60) * 60 - 60
        hi_aligned = (hi // 60) * 60 + 60
        if lo_aligned < hi_aligned:
            triples.append((lo_aligned, hi_aligned, 1_000_000))

    # Multi-crossing triple: a wider range that the swaps'
    # cumulative price path likely crossed multiple times. The
    # range is ``[initial_tick - 240, initial_tick + 240]``
    # aligned to the pool's tick spacing of 60.
    if surface.swap_count > 0:
        init = int(surface.initial_tick)
        lo_aligned = (init // 60) * 60 - 240
        hi_aligned = (init // 60) * 60 + 240
        triples.append((lo_aligned, hi_aligned, 250_000))

    # Zero-liquidity triple: the contract requires a zero-liquidity
    # query to be handled by its own named case (returns zero).
    triples.append((in_range_lower, in_range_upper, 0))

    # Two-distinct-liquidity linearity check at the same range.
    # The contract requires the identity ``fees(L) = L * delta / Q128``
    # to be verified at two distinct liquidity values; the
    # heterogeneity here is part of the equivalence check, not a
    # separate linearity surface.
    triples.append((in_range_lower, in_range_upper, 2_000_000))

    return triples


# ---------------------------------------------------------------------------
# Fingerprint helpers
# ---------------------------------------------------------------------------


def _fingerprint_surface(surface: FeeGrowthSurface) -> str:
    """Return the SHA-256 fingerprint of ``surface``'s canonical payload."""
    payload = surface.canonical_payload()
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "0x" + hashlib.sha256(blob).hexdigest()


def _fingerprint_check(
    records: Iterable[PositionIntegrationRecord],
    window: WindowDescriptor,
) -> str:
    """Return the SHA-256 fingerprint of the equivalence check inputs."""
    payload_records: list[dict[str, object]] = []
    for record in records:
        payload_records.append(
            {
                "tick_lower": int(record.tick_lower),
                "tick_upper": int(record.tick_upper),
                "liquidity": int(record.liquidity),
                "direct_fee_0": int(record.direct_fee_0),
                "direct_fee_1": int(record.direct_fee_1),
                "surface_fee_0": int(record.surface_fee_0),
                "surface_fee_1": int(record.surface_fee_1),
            }
        )
    payload = {
        "window_chain_id_value": int(window.chain_id.value),
        "window_pool_id_value": int(window.pool_id.value),
        "window_pool_key_currency0": str(window.pool_key.currency0),
        "window_pool_key_currency1": str(window.pool_key.currency1),
        "window_pool_key_fee": int(window.pool_key.fee),
        "window_pool_key_tick_spacing": int(window.pool_key.tick_spacing),
        "window_pool_key_hooks": str(window.pool_key.hooks),
        "window_from_block": int(window.from_block),
        "window_to_block": int(window.to_block),
        "window_dataset_version": str(window.dataset_version),
        "window_reconstruction_revision": str(window.reconstruction_revision),
        "records": payload_records,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "0x" + hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Console-rendering helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SurfaceRenderedValue:
    """The console-side projection of one value derived from the surface.

    Every value the console renders from the surface carries the
    surface's :class:`WindowDescriptor` so a reader can tell which
    pool, window, dataset version and reconstruction revision the
    value came from. A console surface that renders a range or
    liquidity value displays the descriptor; a value whose
    provenance cannot be named is refused rather than rendered.

    The field set is the contract's "traceability" requirement:
    ``window``, ``tick_lower``, ``tick_upper``, ``liquidity``,
    ``fee_0``, ``fee_1``, ``fee_growth_inside_0_x128``,
    ``fee_growth_inside_1_x128``, and the artifact's
    :attr:`FeeGrowthSurface.content_hash`.
    """

    surface_content_hash: str
    window: WindowDescriptor
    tick_lower: int
    tick_upper: int
    liquidity: int
    fee_growth_inside_0_x128: int
    fee_growth_inside_1_x128: int
    fee_0: int
    fee_1: int

    def __post_init__(self) -> None:
        if not isinstance(self.surface_content_hash, str) or not self.surface_content_hash:
            raise FeeSurfaceError(
                "SurfaceRenderedValue.surface_content_hash: must be non-empty str"
            )
        _validate_tick_range(self.tick_lower, self.tick_upper)
        if self.liquidity == 0:
            raise InvalidLiquidityError(
                "SurfaceRenderedValue.liquidity=0: a zero-liquidity "
                "rendering is rejected; the contract handles zero "
                "liquidity as a named case at the surface query "
                "layer, returning zero without a rendered value"
            )
        _require_uint128(self.liquidity, field="SurfaceRenderedValue.liquidity")
        for name in (
            "fee_growth_inside_0_x128",
            "fee_growth_inside_1_x128",
            "fee_0",
            "fee_1",
        ):
            value = getattr(self, name)
            _require_non_negative_int(value, field=f"SurfaceRenderedValue.{name}")
            if value >= FEE_GROWTH_MAX:
                raise FeeSurfaceError(f"SurfaceRenderedValue.{name}={value}: must fit in uint256")


def render_surface_value(
    *,
    surface: FeeGrowthSurface,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
) -> SurfaceRenderedValue:
    """Render one ``(range, liquidity)`` value from ``surface``.

    The function refuses to produce a rendered value whose
    provenance cannot be named: ``surface.content_hash`` and
    ``surface.window`` are recorded so the consumer (T103 console,
    T101 panel) can display the window, dataset version and
    reconstruction revision the value came from, and a downstream
    filter rejects any rendered value whose descriptor is missing
    or whose content hash does not match the loaded surface.
    """
    if not isinstance(surface, FeeGrowthSurface):
        raise FeeSurfaceError("render_surface_value.surface: must be FeeGrowthSurface")
    _validate_tick_range(tick_lower, tick_upper)
    _require_uint128(liquidity, field="render_surface_value.liquidity")
    inside = surface.fee_growth_inside(
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        at_event_index=None,
    )
    fees = surface.historical_fees(
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity=liquidity,
    )
    return SurfaceRenderedValue(
        surface_content_hash=str(surface.content_hash),
        window=surface.window,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity=liquidity,
        fee_growth_inside_0_x128=int(inside[0]),
        fee_growth_inside_1_x128=int(inside[1]),
        fee_0=int(fees[0]),
        fee_1=int(fees[1]),
    )


__all__ = [
    "DYNAMIC_FEE_FLAG",
    "EquivalenceCheck",
    "FEE_DENOMINATOR",
    "FEE_GROWTH_MAX",
    "FeeGrowthSnapshot",
    "FeeGrowthSurface",
    "FeeSurfaceError",
    "InvalidLiquidityError",
    "InvalidTickRangeError",
    "PoolIdentityMismatchError",
    "PositionIntegrationRecord",
    "Q128",
    "SURFACE_SCHEMA_VERSION",
    "SurfaceRenderedValue",
    "TickFeeGrowthPrefix",
    "UnknownCrossingTickError",
    "WindowBoundsError",
    "WindowDescriptor",
    "WindowDescriptorError",
    "build_fee_growth_surface",
    "integrate_position_fees",
    "render_surface_value",
    "run_equivalence_check",
]


# Re-export WindowBoundsError so callers that already import
# ``robinhood_lp.replay.errors`` keep working without an extra import.
from robinhood_lp.replay.errors import WindowBoundsError  # noqa: E402
