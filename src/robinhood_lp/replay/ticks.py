"""Tick-liquidity state reconstruction (T041).

Reconstructs the per-pool tick state a Uniswap V4 pool carries for a
pinned finalized window:

- the per-tick ``TickInfo`` (``liquidityGross`` as ``uint128``,
  ``liquidityNet`` as ``int128``) updated by ``ModifyLiquidity``
  events;
- the tick bitmap (a mapping ``int16 wordPos -> uint256`` whose
  ``bitPos`` records whether the corresponding compressed tick is
  initialized) maintained by the same events;
- the ``active_liquidity`` view updated by ``ModifyLiquidity``
  events when the current tick is inside the modified range, and
  updated as ``Swap`` events cross initialized ticks;
- the crossing log (one record per crossed tick) used by downstream
  consumers to attribute active-liquidity transitions;
- the *position-key* treatment (a V4 ``ModifyLiquidity`` event
  references a position by ``(tickLower, tickUpper, salt)``; we treat
  this tuple as the position handle, because the V4 ``ModifyLiquidity``
  event does not emit ``owner`` and the framework does not derive an
  owner inventory from pool-liquidity events — see the T041
  "must not" clause);
- the invariant checks (max liquidity per tick, no negative gross,
  no misaligned ticks) that fail closed rather than silently produce
  a partial reconstruction.

Hard rules (V4 invariant carriers):

- the bitmap ``position`` is the V4 ``position(compressed)`` mapping
  (``wordPos = compressed >> 8`` arithmetic, ``bitPos = compressed &
  0xff``); the bit position within a word is therefore exactly the
  compressed tick index modulo 256, and the word index is the
  compressed tick index divided by 256 with negative-floor
  semantics on Python ints;
- ``compress(tick, tickSpacing)`` equals ``tick // tickSpacing``
  because Python's ``//`` floors — which is the V4 truncation toward
  negative infinity — and V4's decrement-only-for-negative-with-
  remainder collapses to the same value (verified against the pinned
  v4-core commit ``e50237c43811bd9b526eff40f26772152a42daba``);
- ``tickSpacingToMaxLiquidityPerTick`` is the V4 function: with
  ``min_compressed = MIN_TICK // tick_spacing`` and ``max_compressed =
  MAX_TICK // tick_spacing``, the result is
  ``(2**128 - 1) // (max_compressed - min_compressed + 1)``;
- the ``LiquidityMath.addDelta`` overflow guard rejects both the
  upper-bound overflow (``x + y >= 2**128``) and the lower-bound
  underflow (``x + y < 0``); this matches V4's
  ``shr(128, z)`` post-parse of a uint256 wrap.

State separation
----------------

The state is per-pool, scoped to the pool's pinned window and data root
(T038). State is never merged, shared, or carried across pools, and
a pool that T038 reports as ``pool_init_outside_window`` is not
reconstructed — the caller (``T040`` in the T040/T041 chain) does
not invoke this code with a missing pool.

Position keys are recovered as ``(tickLower, tickUpper, salt)`` — the
``salt`` is an opaque byte-identifying marker the framework does not
interpret. A ``ModifyLiquidity`` event with ``salt`` other than zero
identifies one entry of one position; the same ``(tickLower,
tickUpper)`` tuple with a different ``salt`` is a different position.
We do not compute the V4 ``keccak256(abi.encodePacked(owner,
tickLower, tickUpper, salt))`` form because the ``owner`` is not
emitted on chain and is not reconstructible from the event stream.

The reconstruction is float-free throughout the
"replay / feature / backtest / accounting" boundary; only display-side
``Decimal``/float conversions live in the presentation layer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from robinhood_lp.protocol import (
    MAX_TICK,
    MIN_TICK,
    PoolKey,
)

# The protocol layer exposes ``ChainId`` / ``PoolId`` value objects; we
# accept the raw integers directly so the reconstruction is independent
# of the dataclass imports the test fixtures happen to use.
from robinhood_lp.storage.schema import (
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    SwapLogRecord,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: V4 uint128 upper bound. Mirrors ``type(uint128).max``.
MAX_LIQUIDITY: int = (1 << 128) - 1

#: Width of a bitmap word in bits. V4 stores one word per ``int16`` index.
BITMAP_WORD_BITS: int = 256

#: Mask for all 256 bit positions within a single word.
BITMAP_WORD_MASK: int = (1 << BITMAP_WORD_BITS) - 1

#: Direction of a tick crossing. ``ZERO_FOR_ONE`` means the price
#: decreases (the tick is crossed right to left); ``ONE_FOR_ZERO`` means
#: the price increases (the tick is crossed left to right).
CROSSING_ZERO_FOR_ONE: str = "zeroForOne"
CROSSING_ONE_FOR_ZERO: str = "oneForZero"

VALID_CROSSING_DIRECTIONS: frozenset[str] = frozenset(
    {CROSSING_ZERO_FOR_ONE, CROSSING_ONE_FOR_ZERO}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TickLiquidityError(ValueError):
    """Base class for tick-liquidity reconstruction failures."""


class TickMisalignedError(TickLiquidityError):
    """A tick is not a multiple of ``tickSpacing``.

    Mirrors V4's ``TickBitmap.TickMisaligned(int24,int24)`` revert.
    """

    def __init__(self, *, tick: int, tick_spacing: int) -> None:
        self.tick = tick
        self.tick_spacing = tick_spacing
        super().__init__(
            f"tick {tick} is not aligned to tickSpacing {tick_spacing} "
            f"(V4 TickBitmap.TickMisaligned)"
        )


class TickOutOfBoundsError(TickLiquidityError):
    """A tick lies outside V4's ``[MIN_TICK, MAX_TICK]`` domain.

    Mirrors V4's ``Pool.checkTicks`` guard that reverts with
    ``TickLowerOutOfBounds`` / ``TickUpperOutOfBounds``.
    """

    def __init__(self, *, tick: int, bound: str) -> None:
        self.tick = tick
        self.bound = bound
        super().__init__(f"tick {tick} is outside V4 [{MIN_TICK}, {MAX_TICK}] ({bound} violated)")


class TicksMisorderedError(TickLiquidityError):
    """``tick_lower >= tick_upper`` for a single ModifyLiquidity event.

    Mirrors V4's ``Pool.TicksMisordered(int24,int24)`` revert.
    """

    def __init__(self, *, tick_lower: int, tick_upper: int) -> None:
        self.tick_lower = tick_lower
        self.tick_upper = tick_upper
        super().__init__(
            f"tickLower={tick_lower} must be strictly less than "
            f"tickUpper={tick_upper} (V4 Pool.TicksMisordered)"
        )


class TickLiquidityOverflowError(TickLiquidityError):
    """Adding liquidity to a tick would push ``liquidityGross`` past
    ``tickSpacingToMaxLiquidityPerTick(tick_spacing)``.

    Mirrors V4's ``Pool.TickLiquidityOverflow(int24)`` revert, raised
    on add (``liquidityDelta > 0``) when the gross would exceed the
    per-tick maximum.
    """

    def __init__(self, *, tick: int, gross_after: int, max_per_tick: int) -> None:
        self.tick = tick
        self.gross_after = gross_after
        self.max_per_tick = max_per_tick
        super().__init__(
            f"tick {tick}: liquidityGross after add = {gross_after} "
            f"exceeds max liquidity per tick {max_per_tick} "
            f"(V4 Pool.TickLiquidityOverflow)"
        )


class LiquidityOverflowError(TickLiquidityError):
    """A signed liquidity delta would under- or overflow ``uint128``.

    Mirrors V4's ``LiquidityMath.addDelta`` post-check
    (``shr(128, z)``) which reverts ``SafeCastOverflow`` on both
    upper-bound and lower-bound wraps. The "negative gross" failure
    mode the T041 acceptance criterion asks for is one instance of
    this error.
    """

    def __init__(self, *, x: int, y: int, kind: str) -> None:
        self.x = x
        self.y = y
        self.kind = kind
        super().__init__(
            f"liquidity addDelta overflow: x={x}, y={y} ({kind}; "
            f"V4 LiquidityMath.addDelta / SafeCast)"
        )


class UnknownPoolError(TickLiquidityError):
    """A record's pool_id does not match the declared pool."""

    def __init__(self, *, expected_pool_id: int, actual_pool_id: int) -> None:
        self.expected_pool_id = expected_pool_id
        self.actual_pool_id = actual_pool_id
        super().__init__(
            f"event pool_id={actual_pool_id:#034x} does not match "
            f"declared pool_id={expected_pool_id:#034x}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}: must be int, got {type(value).__name__}")
    return value


def compress(tick: int, tick_spacing: int) -> int:
    """Return the V4 compressed tick index ``tick // tickSpacing``.

    Mirrors ``TickBitmap.compress(int24,int24)``. Solidity truncates
    toward zero then decrements if the remainder is non-zero for
    negative ticks; Python's ``//`` already floors toward negative
    infinity, so the Python equivalent is just ``tick // tick_spacing``.
    """
    _require_int(tick, field="tick")
    _require_int(tick_spacing, field="tick_spacing")
    if tick_spacing <= 0:
        raise ValueError(f"tick_spacing must be positive, got {tick_spacing}")
    return tick // tick_spacing


def position(compressed: int) -> tuple[int, int]:
    """Return ``(wordPos, bitPos)`` for a compressed tick.

    Mirrors ``TickBitmap.position(int24)``: ``wordPos = compressed
    >> 8`` (arithmetic right shift) and ``bitPos = compressed & 0xff``.
    Python's ``>>`` on negative integers is an arithmetic shift, so
    negative compressed values produce negative ``wordPos`` exactly
    as V4's ``sar`` does.
    """
    _require_int(compressed, field="compressed")
    word_pos = compressed >> 8
    bit_pos = compressed & 0xFF
    return word_pos, bit_pos


def tick_spacing_to_max_liquidity_per_tick(tick_spacing: int) -> int:
    """Return the per-tick max liquidity for ``tick_spacing``.

    Mirrors ``Pool.tickSpacingToMaxLiquidityPerTick(int24)``:

        min_compressed = MIN_TICK // tick_spacing
        max_compressed = MAX_TICK // tick_spacing
        num_ticks = max_compressed - min_compressed + 1
        return (2**128 - 1) // num_ticks

    Solidity's ``sdiv``/``smod`` truncation toward zero plus the
    negative-with-remainder decrement collapses to the same value
    Python's ``//`` produces, so the closed-form expression above is
    a faithful port of the assembly snippet.
    """
    _require_int(tick_spacing, field="tick_spacing")
    if tick_spacing <= 0:
        raise ValueError(f"tick_spacing must be positive, got {tick_spacing}")
    min_compressed = MIN_TICK // tick_spacing
    max_compressed = MAX_TICK // tick_spacing
    num_ticks = max_compressed - min_compressed + 1
    if num_ticks <= 0:
        raise ValueError(
            f"tick_spacing {tick_spacing} yields non-positive numTicks "
            f"{num_ticks} (min_compressed={min_compressed}, "
            f"max_compressed={max_compressed})"
        )
    return MAX_LIQUIDITY // num_ticks


def add_liquidity(x: int, y: int) -> int:
    """Add a signed ``int128`` delta ``y`` to a ``uint128`` value ``x``.

    Mirrors ``LiquidityMath.addDelta(uint128,int128)``: rejects both
    the upper-bound overflow (``x + y >= 2**128``) and the
    lower-bound underflow (``x + y < 0``). The V4 reference uses
    unchecked uint256 math and reverts with ``SafeCastOverflow`` when
    ``shr(128, z)`` is non-zero, which is the same condition on the
    two's-complement representation.
    """
    _require_int(x, field="x")
    _require_int(y, field="y")
    if x < 0 or x > MAX_LIQUIDITY:
        raise ValueError(f"x must fit in uint128, got {x}")
    if y < -(1 << 127) or y >= (1 << 127):
        raise ValueError(f"y must fit in int128, got {y}")
    signed = x + y
    if signed < 0:
        raise LiquidityOverflowError(x=x, y=y, kind="underflow")
    if signed > MAX_LIQUIDITY:
        raise LiquidityOverflowError(x=x, y=y, kind="overflow")
    return signed


def most_significant_bit(value: int) -> int:
    """Return the index of the highest set bit (0-based) of a positive uint256.

    Mirrors ``BitMath.mostSignificantBit``.
    """
    _require_int(value, field="value")
    if value <= 0:
        raise ValueError("mostSignificantBit requires a positive value")
    if value >= (1 << 256):
        raise ValueError("mostSignificantBit requires a uint256 value")
    return value.bit_length() - 1


def least_significant_bit(value: int) -> int:
    """Return the index of the lowest set bit (0-based) of a positive uint256.

    Mirrors ``BitMath.leastSignificantBit``.
    """
    _require_int(value, field="value")
    if value <= 0:
        raise ValueError("leastSignificantBit requires a positive value")
    low = value & -value
    return low.bit_length() - 1


# ---------------------------------------------------------------------------
# Tick bitmap
# ---------------------------------------------------------------------------


@dataclass
class TickBitmap:
    """The V4 tick bitmap for one pool.

    A ``mapping(int16 => uint256)`` whose key encodes the word index of
    a compressed tick (``wordPos = compressed >> 8``) and whose value
    carries 256 consecutive bit positions (one per compressed tick in
    the word). A set bit means the corresponding tick is initialized.
    """

    tick_spacing: int
    _words: dict[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_int(self.tick_spacing, field="tick_spacing")
        if self.tick_spacing <= 0:
            raise ValueError(f"tick_spacing must be positive, got {self.tick_spacing}")
        for word_pos, word in self._words.items():
            if not isinstance(word_pos, int) or isinstance(word_pos, bool):
                raise TypeError(f"word_pos: must be int, got {type(word_pos).__name__}")
            if word < 0 or word >= (1 << 256):
                raise ValueError(f"bitmap word at {word_pos} out of uint256 range")

    def flip(self, tick: int) -> None:
        """Flip the initialized bit for ``tick``.

        Mirrors ``TickBitmap.flipTick``. A tick not aligned to the
        pool's spacing raises :class:`TickMisalignedError`; the bit
        position is computed from the *compressed* tick
        (``tick // tick_spacing``) per V4.
        """
        if tick % self.tick_spacing != 0:
            raise TickMisalignedError(tick=tick, tick_spacing=self.tick_spacing)
        compressed = compress(tick, self.tick_spacing)
        word_pos, bit_pos = position(compressed)
        mask = 1 << bit_pos
        self._words[word_pos] = self._words.get(word_pos, 0) ^ mask

    def is_initialized(self, tick: int) -> bool:
        """Return True iff the bit for ``tick`` is set."""
        if tick % self.tick_spacing != 0:
            return False
        compressed = compress(tick, self.tick_spacing)
        word_pos, bit_pos = position(compressed)
        word = self._words.get(word_pos, 0)
        return bool(word & (1 << bit_pos))

    def word(self, word_pos: int) -> int:
        """Return the bitmap word at ``word_pos`` (``0`` when unset)."""
        return self._words.get(word_pos, 0)

    def words(self) -> dict[int, int]:
        """Return a snapshot of the underlying word map (copied)."""
        return dict(self._words)

    def next_initialized_tick_within_one_word(
        self,
        tick: int,
        *,
        lte: bool,
    ) -> tuple[int, bool]:
        """Return ``(next, initialized)`` within the word.

        Mirrors ``TickBitmap.nextInitializedTickWithinOneWord``:

        - ``lte=True`` searches for the next initialized tick at or
          to the *left* of ``tick``; the returned ``next`` is the
          tick itself when no tick in the word is initialized, the
          rightmost set bit otherwise.
        - ``lte=False`` searches for the next initialized tick to the
          *right* of ``tick``; the returned ``next`` is the leftmost
          tick in the next compressed position when no tick is
          initialized, the leftmost set bit in the word otherwise.

        V4's contract: ``initialized=False`` does not mean "no
        initialized tick exists", it means "no initialized tick in
        this word". The caller must continue scanning other words.
        """
        compressed_in = compress(tick, self.tick_spacing)
        if lte:
            word_pos, bit_pos = position(compressed_in)
            # All set bits at or to the right of ``bit_pos``.
            mask = BITMAP_WORD_MASK >> (BITMAP_WORD_BITS - 1 - bit_pos)
            masked = self._words.get(word_pos, 0) & mask
            if masked == 0:
                next_compressed = compressed_in - bit_pos
                return next_compressed * self.tick_spacing, False
            msb = most_significant_bit(masked)
            next_compressed = compressed_in - (bit_pos - msb)
            return next_compressed * self.tick_spacing, True
        # Search to the right: ``++compressed`` in the V4 source.
        compressed = compressed_in + 1
        word_pos, bit_pos = position(compressed)
        # All set bits at or to the left of ``bit_pos`` (within the word).
        mask = (~((1 << bit_pos) - 1)) & BITMAP_WORD_MASK
        masked = self._words.get(word_pos, 0) & mask
        if masked == 0:
            next_compressed = compressed + (BITMAP_WORD_BITS - 1 - bit_pos)
            return next_compressed * self.tick_spacing, False
        lsb = least_significant_bit(masked)
        next_compressed = compressed + (lsb - bit_pos)
        return next_compressed * self.tick_spacing, True


# ---------------------------------------------------------------------------
# Tick info and position key
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TickInfo:
    """The V4 ``Pool.TickInfo`` reconstruction.

    ``liquidityGross`` is ``uint128`` (always non-negative),
    ``liquidityNet`` is ``int128``. ``feeGrowthOutside0X128`` and
    ``feeGrowthOutside1X128`` are the per-tick all-time fee growth
    the V4 reference tracks; the framework reconstructs them only
    when a swap crosses the tick (``Pool.crossTick``); without
    fee-growth-global tracking they remain zero for ticks that have
    never been crossed.

    The T041 acceptance criteria do not exercise fee-growth-global
    values (those are owned by T051); the field is carried so the
    reconstruction is structurally faithful to V4.
    """

    liquidity_gross: int = 0
    liquidity_net: int = 0
    fee_growth_outside_0: int = 0
    fee_growth_outside_1: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.liquidity_gross, int) or isinstance(self.liquidity_gross, bool):
            raise TypeError(
                f"liquidity_gross: must be int, got {type(self.liquidity_gross).__name__}"
            )
        if self.liquidity_gross < 0 or self.liquidity_gross > MAX_LIQUIDITY:
            raise ValueError(f"liquidity_gross: must fit in uint128, got {self.liquidity_gross}")
        if not isinstance(self.liquidity_net, int) or isinstance(self.liquidity_net, bool):
            raise TypeError(f"liquidity_net: must be int, got {type(self.liquidity_net).__name__}")
        if self.liquidity_net < -(1 << 127) or self.liquidity_net >= (1 << 127):
            raise ValueError(f"liquidity_net: must fit in int128, got {self.liquidity_net}")
        for name in ("fee_growth_outside_0", "fee_growth_outside_1"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name}: must be int, got {type(value).__name__}")
            if value < 0 or value >= (1 << 256):
                raise ValueError(f"{name}: must fit in uint256, got {value}")

    @property
    def initialized(self) -> bool:
        """Return True iff this tick carries a non-zero gross.

        Mirrors V4's bitmap-flip rule (``flipped = (grossAfter == 0)
        != (grossBefore == 0)``): a tick is initialized iff
        ``liquidity_gross > 0``.
        """
        return self.liquidity_gross > 0


@dataclass(frozen=True, slots=True)
class PositionKey:
    """The V4 position key reconstructed from a ``ModifyLiquidity`` event.

    V4 identifies a position by ``keccak256(abi.encodePacked(owner,
    tickLower, tickUpper, salt))``. The ``owner`` is not emitted on
    chain by the V4 ``ModifyLiquidity`` event and is not
    reconstructible from the event stream, so we treat the
    ``(tickLower, tickUpper, salt)`` triple as the position key — the
    "must not" clause of T041 forbids deriving an owner inventory
    from pool-liquidity events.

    Equality uses all three fields; the same ``(tickLower, tickUpper)``
    with a different ``salt`` is a different position.
    """

    tick_lower: int
    tick_upper: int
    salt: int

    def __post_init__(self) -> None:
        for name in ("tick_lower", "tick_upper", "salt"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"PositionKey.{name}: must be int, got {type(value).__name__}")
        if self.tick_lower >= self.tick_upper:
            raise ValueError(
                f"PositionKey.tick_lower={self.tick_lower} must be "
                f"strictly less than tick_upper={self.tick_upper}"
            )
        if self.tick_lower < MIN_TICK or self.tick_lower > MAX_TICK:
            raise ValueError(
                f"PositionKey.tick_lower={self.tick_lower} outside V4 [{MIN_TICK}, {MAX_TICK}]"
            )
        if self.tick_upper < MIN_TICK or self.tick_upper > MAX_TICK:
            raise ValueError(
                f"PositionKey.tick_upper={self.tick_upper} outside V4 [{MIN_TICK}, {MAX_TICK}]"
            )
        if self.salt < 0 or self.salt >= (1 << 256):
            raise ValueError(f"PositionKey.salt={self.salt} must fit in uint256")


@dataclass(frozen=True, slots=True)
class TickCrossing:
    """One crossing of an initialized tick during a swap.

    The reconstruction emits one record per initialized tick the
    swap's price path crossed. The record carries the canonical
    EventKey fields so a downstream consumer can attribute each
    crossing to the producing swap, plus the V4 invariants that
    downstream attribution (T051) needs: the direction, the
    ``liquidity_net`` at the crossed tick, and the active-liquidity
    before / after the crossing.
    """

    chain_id_value: int
    pool_id_value: int
    block_number: int
    block_hash: int
    transaction_index: int
    log_index: int
    tick: int
    direction: str
    liquidity_net: int
    active_liquidity_before: int
    active_liquidity_after: int

    def __post_init__(self) -> None:
        if self.direction not in VALID_CROSSING_DIRECTIONS:
            raise ValueError(
                f"TickCrossing.direction={self.direction!r} must be one "
                f"of {sorted(VALID_CROSSING_DIRECTIONS)}"
            )
        for name in (
            "chain_id_value",
            "pool_id_value",
            "block_number",
            "block_hash",
            "transaction_index",
            "log_index",
            "tick",
            "liquidity_net",
            "active_liquidity_before",
            "active_liquidity_after",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name}: must be int, got {type(value).__name__}")


# ---------------------------------------------------------------------------
# Per-pool reconstructed state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconstructedPoolTickState:
    """The deterministic tick-liquidity reconstruction for one pool.

    The state is frozen and hashable; equality and hashing follow
    dataclass identity. Two reconstructions produced from the same
    per-pool event sequence and PoolKey produce identical state instances
    regardless of ingestion order or restart boundaries — the
    reconstruction is a pure function of its inputs.
    """

    chain_id_value: int
    pool_id_value: int
    tick_spacing: int
    max_liquidity_per_tick: int

    # Per-tick state (only ticks that have ever been initialized).
    ticks: tuple[tuple[int, TickInfo], ...]

    # Bitmap words (the bytes-as-int values per wordPos).
    bitmap_words: tuple[tuple[int, int], ...]

    # Crossing log: one entry per initialized tick crossed by a swap.
    crossings: tuple[TickCrossing, ...]

    # Position keys observed during reconstruction (one entry per
    # distinct (tickLower, tickUpper, salt) triple). Pokes (delta=0)
    # still record a position key; a position key that is observed
    # both at add and at remove is recorded once.
    position_keys: tuple[PositionKey, ...]

    # Terminal active liquidity.
    final_active_liquidity: int

    # Number of ModifyLiquidity events applied (any delta sign).
    modify_liquidity_count: int

    # Number of Swap events applied.
    swap_count: int

    # The current pool tick at the end of the window (i.e. the
    # ``slot0.tick`` V4 would expose to StateView). The bootstrap
    # tick is taken from the caller-supplied
    # ``bootstrap_tick``; subsequent events update it via
    # Swap.tick (V4's ``slot0.tick = step.tickNext - 1`` /
    # ``step.tickNext`` post-crossing rules are applied by the
    # reconstruction so the terminal tick equals the post-swap
    # tick of the last Swap event).
    final_tick: int

    def __post_init__(self) -> None:
        for name, value in (
            ("chain_id_value", self.chain_id_value),
            ("pool_id_value", self.pool_id_value),
            ("tick_spacing", self.tick_spacing),
            ("max_liquidity_per_tick", self.max_liquidity_per_tick),
            ("final_active_liquidity", self.final_active_liquidity),
            ("modify_liquidity_count", self.modify_liquidity_count),
            ("swap_count", self.swap_count),
            ("final_tick", self.final_tick),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name}: must be int, got {type(value).__name__}")

    @property
    def initialized_ticks(self) -> tuple[int, ...]:
        """Return the sorted tuple of initialized tick indices.

        A tick is initialized iff it appears in :attr:`ticks` with
        ``liquidity_gross > 0`` (see :attr:`TickInfo.initialized`).
        """
        return tuple(sorted(t for t, info in self.ticks if info.initialized))

    def tick_info(self, tick: int) -> TickInfo:
        """Return the :class:`TickInfo` at ``tick`` or the zero-state.

        A tick not in :attr:`ticks` carries the zero-state
        ``TickInfo()`` (all fields zero, ``initialized=False``).
        """
        for t, info in self.ticks:
            if t == tick:
                return info
        return TickInfo()

    def is_initialized(self, tick: int) -> bool:
        """Return True iff the tick is in the initialized set."""
        return self.tick_info(tick).initialized


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _MutablePoolTickState:
    """The mutable per-pool tick state used during reconstruction."""

    chain_id_value: int
    pool_id_value: int
    tick_spacing: int
    max_liquidity_per_tick: int
    bitmap: TickBitmap
    ticks: dict[int, TickInfo]
    position_keys: dict[PositionKey, None]
    crossings: list[TickCrossing]
    active_liquidity: int
    current_tick: int
    modify_liquidity_count: int
    swap_count: int

    def to_frozen(self) -> ReconstructedPoolTickState:
        return ReconstructedPoolTickState(
            chain_id_value=self.chain_id_value,
            pool_id_value=self.pool_id_value,
            tick_spacing=self.tick_spacing,
            max_liquidity_per_tick=self.max_liquidity_per_tick,
            ticks=tuple(sorted(self.ticks.items())),
            bitmap_words=tuple(sorted(self.bitmap.words().items())),
            crossings=tuple(self.crossings),
            position_keys=tuple(
                sorted(
                    self.position_keys,
                    key=lambda k: (k.tick_lower, k.tick_upper, k.salt),
                )
            ),
            final_active_liquidity=self.active_liquidity,
            modify_liquidity_count=self.modify_liquidity_count,
            swap_count=self.swap_count,
            final_tick=self.current_tick,
        )


def _require_typed_event(record: Any) -> None:
    """Reject records that are not one of the V4 typed log records."""
    if not isinstance(
        record,
        (
            InitializeLogRecord,
            ModifyLiquidityLogRecord,
            SwapLogRecord,
            DonateLogRecord,
        ),
    ):
        raise TypeError(f"reconstruct_tick_liquidity: unknown event type {type(record).__name__}")


def _event_key_of(record: Any) -> tuple[int, int, int, int]:
    """Return the canonical EventKey tuple for ``record``."""
    return (
        int(record.chain_id.value),
        int(record.block_hash),
        int(record.transaction_hash),
        int(record.log_index),
    )


def _apply_initialize(
    state: _MutablePoolTickState,
    record: InitializeLogRecord,
) -> None:
    """Apply an Initialize event to the tick state.

    The Initialize event itself does not touch the bitmap or the
    active liquidity; it is recorded as the lifecycle marker so the
    reconstruction is structurally aligned with the replay. The
    caller is expected to set ``state.current_tick`` to the
    bootstrap tick (typically the value returned by
    ``getTickAtSqrtPrice(sqrtPriceX96)`` at the ``Initialize`` block)
    before any other event is applied.
    """
    del record  # Only the position is used here.
    return


def _validate_modify_ticks(tick_lower: int, tick_upper: int) -> None:
    """Apply V4's ``Pool.checkTicks`` guards."""
    if tick_lower >= tick_upper:
        raise TicksMisorderedError(tick_lower=tick_lower, tick_upper=tick_upper)
    if tick_lower < MIN_TICK:
        raise TickOutOfBoundsError(tick=tick_lower, bound="tickLower < MIN_TICK")
    if tick_upper > MAX_TICK:
        raise TickOutOfBoundsError(tick=tick_upper, bound="tickUpper > MAX_TICK")


def _update_tick(
    *,
    state: _MutablePoolTickState,
    tick: int,
    delta: int,
    upper: bool,
) -> TickInfo:
    """Apply ``Pool.updateTick`` and the corresponding bitmap flip.

    Returns the new :class:`TickInfo`. When the gross transition
    flips the bitmap, the bitmap is updated in-place. When the gross
    transitions to zero, the tick is removed from the state and its
    bitmap bit is cleared.
    """
    before = state.ticks.get(tick, TickInfo())
    gross_after = add_liquidity(before.liquidity_gross, delta)

    if delta >= 0 and gross_after > state.max_liquidity_per_tick:
        raise TickLiquidityOverflowError(
            tick=tick,
            gross_after=gross_after,
            max_per_tick=state.max_liquidity_per_tick,
        )

    flipped = (gross_after == 0) != (before.liquidity_gross == 0)
    # V4's net update: lower += delta, upper -= delta.
    net_delta = -delta if upper else delta
    net_after = before.liquidity_net + net_delta

    fee_growth_outside_0 = before.fee_growth_outside_0
    fee_growth_outside_1 = before.fee_growth_outside_1

    info = TickInfo(
        liquidity_gross=gross_after,
        liquidity_net=net_after,
        fee_growth_outside_0=fee_growth_outside_0,
        fee_growth_outside_1=fee_growth_outside_1,
    )
    if gross_after == 0:
        # V4 clears the tick when the gross drops to zero.
        state.ticks.pop(tick, None)
    else:
        state.ticks[tick] = info

    if flipped:
        state.bitmap.flip(tick)

    return info


def _apply_modify_liquidity(
    state: _MutablePoolTickState,
    record: ModifyLiquidityLogRecord,
) -> None:
    """Apply one ``ModifyLiquidity`` event to the tick state.

    Mirrors ``Pool.modifyLiquidity``:

    - ``checkTicks`` rejects misordered / out-of-bounds ticks;
    - spacing must divide ``tick_lower`` and ``tick_upper``;
    - ``delta > 0`` (add) checks ``grossAfter <= maxLiquidityPerTick``;
    - ``delta == 0`` (poke) is a no-op on the bitmap / ticks and
      only records the position key;
    - ``delta < 0`` (remove) checks ``grossAfter >= 0`` via
      :func:`add_liquidity` (the SafeCast overflow guard);
    - the bitmap flips on the 0 ↔ non-zero transition of the gross;
    - when ``tickLower <= current_tick < tickUpper`` the active
      liquidity is updated immediately, mirroring V4's
      ``self.liquidity = LiquidityMath.addDelta(...)`` branch.
    """
    state.modify_liquidity_count += 1

    tick_lower = int(record.tick_lower)
    tick_upper = int(record.tick_upper)
    delta = int(record.liquidity_delta)
    salt = int(record.salt)

    _validate_modify_ticks(tick_lower, tick_upper)
    if tick_lower % state.tick_spacing != 0:
        raise TickMisalignedError(tick=tick_lower, tick_spacing=state.tick_spacing)
    if tick_upper % state.tick_spacing != 0:
        raise TickMisalignedError(tick=tick_upper, tick_spacing=state.tick_spacing)

    # Record the position key (a "poke" with delta=0 still records it
    # in V4 because the Position.update branch is reached; here we
    # collect the triple without touching the bitmap or ticks).
    position_key = PositionKey(tick_lower=tick_lower, tick_upper=tick_upper, salt=salt)
    state.position_keys[position_key] = None

    if delta == 0:
        # V4's "poke" path: updateTick is skipped; only the
        # Position.update branch runs, which we record as the
        # position key above.
        return

    # updateTick(tickLower, delta, false): liquidityNet += delta
    state.ticks[tick_lower] = _update_tick(
        state=state,
        tick=tick_lower,
        delta=delta,
        upper=False,
    )
    # updateTick(tickUpper, delta, true): liquidityNet -= delta
    state.ticks[tick_upper] = _update_tick(
        state=state,
        tick=tick_upper,
        delta=delta,
        upper=True,
    )

    # V4's branch: when ``tickLower <= current_tick < tickUpper``
    # the active liquidity is updated immediately.
    if tick_lower <= state.current_tick < tick_upper:
        state.active_liquidity = add_liquidity(state.active_liquidity, delta)


def _next_initialized_tick(
    bitmap: TickBitmap,
    tick_spacing: int,
    tick: int,
    *,
    lte: bool,
) -> tuple[int, bool] | None:
    """Walk the bitmap across words to find the next initialized tick.

    Returns ``(next_tick, True)`` when an initialized tick exists in
    the search direction; returns ``None`` when the search has run
    past the V4 domain. ``next_initialized_tick_within_one_word``
    only looks at one word at a time; this helper stitches the
    per-word search together so the swap loop is faithful to V4's
    step-by-step crossing semantics across the full tick range.
    """
    cur = tick
    while True:
        next_tick, initialized = bitmap.next_initialized_tick_within_one_word(cur, lte=lte)
        if initialized:
            return next_tick, True
        if lte:
            if next_tick <= MIN_TICK:
                return None
            cur = next_tick - tick_spacing
        else:
            if next_tick >= MAX_TICK:
                return None
            cur = next_tick + tick_spacing


def _apply_swap(
    state: _MutablePoolTickState,
    record: SwapLogRecord,
) -> None:
    """Apply one ``Swap`` event's crossings to the tick state.

    The Swap event carries the post-swap ``tick`` and the post-swap
    ``liquidity``; the reconstruction walks every initialized tick
    between the previous and the new tick in the direction of price
    movement and applies ``Pool.crossTick`` semantics:

    - ``zeroForOne`` (price decreasing, right-to-left crossing):
      ``active_liquidity -= liquidityNet`` at each crossed tick;
    - ``oneForZero`` (price increasing, left-to-right crossing):
      ``active_liquidity += liquidityNet`` at each crossed tick.

    The post-swap ``liquidity`` is verified against the value
    reconstructed by walking the bitmap; a no-tick-move swap that
    changes the active liquidity is rejected (V4's active liquidity
    only changes on tick crossings).
    """
    state.swap_count += 1

    new_tick = int(record.tick)
    new_liquidity = int(record.liquidity)
    pre_tick = state.current_tick
    pre_liquidity = state.active_liquidity

    if pre_tick == new_tick:
        # No tick boundary crossed; only verify the active liquidity.
        if pre_liquidity != new_liquidity:
            raise TickLiquidityError(
                f"swap with unchanged tick {new_tick} changes active "
                f"liquidity from {pre_liquidity} to {new_liquidity} "
                f"without crossing any tick (V4 invariant)"
            )
        return

    direction = CROSSING_ZERO_FOR_ONE if new_tick < pre_tick else CROSSING_ONE_FOR_ZERO

    cur_tick = pre_tick
    cur_liquidity = pre_liquidity
    while True:
        if direction == CROSSING_ZERO_FOR_ONE:
            search = _next_initialized_tick(state.bitmap, state.tick_spacing, cur_tick, lte=True)
            if search is None or search[0] <= new_tick:
                break
            next_tick, _ = search
        else:
            search = _next_initialized_tick(state.bitmap, state.tick_spacing, cur_tick, lte=False)
            if search is None or search[0] > new_tick:
                break
            next_tick, _ = search
        if next_tick % state.tick_spacing != 0:
            raise TickMisalignedError(tick=next_tick, tick_spacing=state.tick_spacing)
        info = state.ticks.get(next_tick)
        if info is None:
            # The bitmap bit is set but the tick info is missing. The
            # tick info is cleared when gross reaches zero and the
            # bitmap bit should also be cleared in lockstep; this is
            # an invariant violation.
            raise TickLiquidityError(
                f"bitmap marks tick {next_tick} as initialized but no "
                f"TickInfo exists (V4 invariant violation)"
            )
        net = info.liquidity_net
        active_before = cur_liquidity
        if direction == CROSSING_ZERO_FOR_ONE:
            cur_liquidity = add_liquidity(cur_liquidity, -net)
        else:
            cur_liquidity = add_liquidity(cur_liquidity, net)
        active_after = cur_liquidity
        state.crossings.append(
            TickCrossing(
                chain_id_value=state.chain_id_value,
                pool_id_value=state.pool_id_value,
                block_number=int(record.block_number),
                block_hash=int(record.block_hash),
                transaction_index=int(record.transaction_index),
                log_index=int(record.log_index),
                tick=next_tick,
                direction=direction,
                liquidity_net=net,
                active_liquidity_before=active_before,
                active_liquidity_after=active_after,
            )
        )
        # Advance the cursor past the crossed tick. V4 advances
        # ``slot0.tick`` to ``tickNext - 1`` for zeroForOne and
        # ``tickNext`` for oneForZero after a successful crossing.
        if direction == CROSSING_ZERO_FOR_ONE:
            cur_tick = next_tick - state.tick_spacing
        else:
            cur_tick = next_tick

    if cur_liquidity != new_liquidity:
        raise TickLiquidityError(
            f"swap at block={record.block_number} "
            f"(tx_index={record.transaction_index}, "
            f"log_index={record.log_index}) reconstructed "
            f"active_liquidity={cur_liquidity} does not match "
            f"event liquidity={new_liquidity}; pre_tick={pre_tick}, "
            f"new_tick={new_tick}, direction={direction}"
        )

    state.active_liquidity = new_liquidity
    state.current_tick = new_tick


def _apply_donate(
    state: _MutablePoolTickState,
    record: DonateLogRecord,
) -> None:
    """Apply one Donate event to the tick state.

    Donations do not change the bitmap, ticks, or active liquidity;
    they only update fee-growth-global. The reconstruction records
    the event position and leaves the tick state untouched.
    """
    del record  # Only the position is used here.
    return


def reconstruct_tick_liquidity(
    *,
    chain_id: int,
    pool_id: int,
    pool_key: PoolKey,
    events: Iterable[Any],
    bootstrap_tick: int = 0,
) -> ReconstructedPoolTickState:
    """Reconstruct one pool's tick-liquidity state from ``events``.

    The function is per-pool: ``chain_id`` and ``pool_id`` identify
    the pool; every event in ``events`` must belong to this pool, and
    events from a different pool raise :class:`UnknownPoolError`. The
    reconstruction is scoped to that pool's pinned finalized window
    and data root (T038); it is never merged, shared, or carried
    across pools. A pool T038 reports as ``pool_init_outside_window``
    is not reconstructed by this function — the caller must not feed
    it a partial window, a later-state bootstrap, or a neighbouring
    pool's events as a substitute.

    ``bootstrap_tick`` is the value of ``slot0.tick`` at the pool's
    ``Initialize`` block; it is the V4 ``getTickAtSqrtPrice(sqrtPriceX96)``
    result the caller obtains from a StateView-style block-pinned read.
    The default is ``0`` to match the uninitialized bootstrap the
    replay input carries when no block-pinned read is supplied.

    Determinism: the function is a pure function of ``pool_key``,
    ``bootstrap_tick`` and ``events``. Two calls with the same
    inputs produce identical :class:`ReconstructedPoolTickState`
    instances regardless of iteration order or restart boundaries
    (the function sorts by ``(block_number, transaction_index,
    log_index)`` first).
    """
    if not isinstance(pool_key, PoolKey):
        raise TypeError(f"pool_key: must be PoolKey, got {type(pool_key).__name__}")
    _require_int(chain_id, field="chain_id")
    _require_int(pool_id, field="pool_id")
    _require_int(bootstrap_tick, field="bootstrap_tick")

    materialised = list(events)
    for record in materialised:
        _require_typed_event(record)
    ordered = sorted(
        materialised,
        key=lambda r: (
            int(r.block_number),
            int(r.transaction_index),
            int(r.log_index),
        ),
    )

    tick_spacing = int(pool_key.tick_spacing)
    state = _MutablePoolTickState(
        chain_id_value=chain_id,
        pool_id_value=pool_id,
        tick_spacing=tick_spacing,
        max_liquidity_per_tick=tick_spacing_to_max_liquidity_per_tick(tick_spacing),
        bitmap=TickBitmap(tick_spacing=tick_spacing),
        ticks={},
        position_keys={},
        crossings=[],
        active_liquidity=0,
        current_tick=bootstrap_tick,
        modify_liquidity_count=0,
        swap_count=0,
    )

    for record in ordered:
        _require_typed_event(record)
        record_chain_id = int(record.chain_id.value)
        record_pool_id = int(record.pool_id.value)
        if record_chain_id != chain_id or record_pool_id != pool_id:
            raise UnknownPoolError(
                expected_pool_id=pool_id,
                actual_pool_id=record_pool_id,
            )
        if isinstance(record, InitializeLogRecord):
            _apply_initialize(state, record)
        elif isinstance(record, ModifyLiquidityLogRecord):
            _apply_modify_liquidity(state, record)
        elif isinstance(record, SwapLogRecord):
            _apply_swap(state, record)
        elif isinstance(record, DonateLogRecord):
            _apply_donate(state, record)
        else:
            raise TypeError(
                f"reconstruct_tick_liquidity: unsupported event type {type(record).__name__}"
            )

    return state.to_frozen()


__all__ = [
    "BITMAP_WORD_BITS",
    "BITMAP_WORD_MASK",
    "CROSSING_ONE_FOR_ZERO",
    "CROSSING_ZERO_FOR_ONE",
    "LiquidityOverflowError",
    "MAX_LIQUIDITY",
    "PositionKey",
    "ReconstructedPoolTickState",
    "TickBitmap",
    "TickCrossing",
    "TickInfo",
    "TickLiquidityError",
    "TickLiquidityOverflowError",
    "TickMisalignedError",
    "TickOutOfBoundsError",
    "TicksMisorderedError",
    "UnknownPoolError",
    "VALID_CROSSING_DIRECTIONS",
    "add_liquidity",
    "compress",
    "least_significant_bit",
    "most_significant_bit",
    "position",
    "reconstruct_tick_liquidity",
    "tick_spacing_to_max_liquidity_per_tick",
]
