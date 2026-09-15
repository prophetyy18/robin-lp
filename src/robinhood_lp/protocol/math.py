"""V4 math: tick <-> sqrtPriceX96, liquidity <-> amounts (T012).

This module is a byte-exact port of the V4 reference implementations:

- ``TickMath.sol::getSqrtPriceAtTick`` and ``getTickAtSqrtPrice``
- ``SqrtPriceMath.sol::getAmount0Delta`` and ``getAmount1Delta``
- ``LiquidityAmounts.sol::getLiquidityForAmounts``

The Solidity sources are pinned in ``tools/oracle/lib/v4-core`` and
``tools/oracle/lib/v4-periphery``. Every output is verified against
the pinned vectors in ``tests/fixtures/protocol/math_vectors.json``
which were produced by the Foundry oracle
(``tools/oracle/src/MathOracle.sol``).

Rounding and bounds (carried verbatim from V4):

- ``getSqrtPriceAtTick`` rounds *up* in the final ``(price + 2^32 - 1) >> 32``
  step so that ``getTickAtSqrtPrice`` of the output price is consistent.
- ``getTickAtSqrtPrice`` is strict: ``sqrtPriceX96 < MIN_SQRT_PRICE`` and
  ``sqrtPriceX96 >= MAX_SQRT_PRICE`` are both invalid.
- All intermediate values stay within uint256 / int256 by Solidity's
  bit layout; Python's arbitrary-precision ints replicate the
  exact same arithmetic without overflow.
- ``getAmount0Delta`` / ``getAmount1Delta`` accept ``round_up`` and
  select between Solidity's floor and ceiling branches. The V4
  reference is ``SqrtPriceMath.sol``; the pinned oracle in
  ``tools/oracle/`` emits one vector per rounding direction per
  function.

The display/Decimal boundary lives in
``src/robinhood_lp/presentation/prices.py`` (ADR-009). The protocol
package itself is float-free; do not add float conversions here.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Tick / sqrt-price constants (pinned — see docs/spec/protocol/PROTOCOL_FACTS.md)
# ---------------------------------------------------------------------------
MIN_TICK: Final[int] = -887_272
MAX_TICK: Final[int] = 887_272
MIN_TICK_SPACING: Final[int] = 1
MAX_TICK_SPACING: Final[int] = 32_767  # type(int16).max

MIN_SQRT_PRICE_X96: Final[int] = 4_295_128_739
MAX_SQRT_PRICE_X96: Final[int] = 1_461_446_703_485_210_103_287_273_052_203_988_822_378_723_970_342

#: Maximum value of the difference between the input sqrt price and
#: MIN_SQRT_PRICE for which getTickAtSqrtPrice succeeds.
#: Computed once and pinned; == MAX_SQRT_PRICE - MIN_SQRT_PRICE - 1.
_MAX_SQRT_PRICE_MINUS_MIN_SQRT_PRICE_MINUS_ONE: Final[int] = (
    MAX_SQRT_PRICE_X96 - MIN_SQRT_PRICE_X96 - 1
)

#: Solidity uses unchecked math on uint256; we mirror that with
#: arbitrary-precision ints and explicit masks/guards.
_Q128: Final[int] = 1 << 128
_NOT_Q256: Final[int] = (1 << 256) - 1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TickMathError(ValueError):
    """Raised when a tick or sqrt-price input is outside the V4 domain."""


class InvalidTickError(TickMathError):
    """The supplied tick is outside [MIN_TICK, MAX_TICK]."""


class InvalidSqrtPriceError(TickMathError):
    """The supplied sqrt price is outside [MIN_SQRT_PRICE, MAX_SQRT_PRICE)."""


class AmountDeltaError(ValueError):
    """The supplied liquidity/price range is degenerate (e.g. pa == pb)."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_uint160(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field}: must be non-negative, got {value}")
    if value >= (1 << 160):
        raise ValueError(f"{field}: exceeds uint160, got {value}")
    return value


def _require_int24(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}: must be int, got {type(value).__name__}")
    if value < -(1 << 23) or value >= (1 << 23):
        raise ValueError(f"{field}: out of int24 range, got {value}")
    return value


def _most_significant_bit(value: int) -> int:
    """Return the index of the highest set bit (0-based) of a uint256.

    Mirrors BitMath.sol::mostSignificantBit. Value must be non-zero
    and fit in uint256.
    """
    if value <= 0:
        raise ValueError("mostSignificantBit requires a positive value")
    if value >= (1 << 256):
        raise ValueError("mostSignificantBit requires a uint256 value")
    return value.bit_length() - 1


# ---------------------------------------------------------------------------
# Usable-tick helpers
# ---------------------------------------------------------------------------


def max_usable_tick(tick_spacing: int) -> int:
    """Return the maximum tick usable with the given tick spacing.

    V4 truncates toward zero (Solidity uses unchecked integer math,
    which matches Python's ``//`` for non-negative operands).
    """
    return (MAX_TICK // tick_spacing) * tick_spacing


def min_usable_tick(tick_spacing: int) -> int:
    """Return the minimum tick usable with the given tick spacing.

    V4 uses C-style truncation toward zero on the MIN_TICK/tick_spacing
    division. Python's ``//`` would floor the negative quotient, so the
    implementation divides the positive magnitude and restores the sign.
    """
    return -((-MIN_TICK) // tick_spacing) * tick_spacing


# ---------------------------------------------------------------------------
# TickMath: getSqrtPriceAtTick
# ---------------------------------------------------------------------------


# Coefficients used by getSqrtPriceAtTick (pinned from TickMath.sol).
_TICK_CONSTANTS: Final[tuple[int, ...]] = (
    0xFFFCB933BD6FAD37AA2D162D1A594001,  # bit 0  (low 32 bits; high half is xor mask)
    0xFFF97272373D413259A46990580E213A,
    0xFFF2E50F5F656932EF12357CF3C7FDCC,
    0xFFE5CACA7E10E4E61C3624EAA0941CD0,
    0xFFCB9843D60F6159C9DB58835C926644,
    0xFF973B41FA98C081472E6896DFB254C0,
    0xFF2EA16466C96A3843EC78B326B52861,
    0xFE5DEE046A99A2A811C461F1969C3053,
    0xFCBE86C7900A88AEDCFFC83B479AA3A4,
    0xF987A7253AC413176F2B074CF7815E54,
    0xF3392B0822B70005940C7A398E4B70F3,
    0xE7159475A2C29B7443B29C7FA6E889D9,
    0xD097F3BDFD2022B8845AD8F792AA5825,
    0xA9F746462D870FDF8A65DC1F90E061E5,
    0x70D869A156D2A1B890BB3DF62BAF32F7,
    0x31BE135F97D08FD981231505542FCFA6,
    0x9AA508B5B7A84E1C677DE54F3E99BC9,
    0x5D6AF8DEDB81196699C329225EE604,
    0x2216E584F5FA1EA926041BEDFE98,
    0x48A170391F7DC42444E8FA2,
)


def get_sqrt_price_at_tick(tick: int) -> int:
    """Return ``sqrt(1.0001^tick) * 2^96`` as a uint160.

    Mirrors ``TickMath.sol::getSqrtPriceAtTick``. The final rounding is
    *up* (``(price + 2^32 - 1) >> 32``) so that the resulting sqrt
    price is consistent with ``get_tick_at_sqrt_price``.
    """
    _require_int24(tick, field="tick")
    if tick < MIN_TICK or tick > MAX_TICK:
        raise InvalidTickError(f"tick={tick} outside [{MIN_TICK}, {MAX_TICK}]")

    # Mirror Solidity's signextend and the |tick| bit-trick.
    abs_tick = abs(tick)

    # Initial price: bit 0 of abs_tick selects between ``1 << 128`` and
    # the constant that encodes ``2^128 / sqrt(1.0001)`` (Q128.128).
    # This is a literal port of the assembly trick
    #   price := xor(shl(128, 1),
    #                mul(xor(shl(128, 1), 0xfffcb933...),
    #                    and(absTick, 0x1)))
    # which simplifies to:
    #   bit 0 == 0: price = (1<<128) XOR ((1<<128) XOR c[0]) * 0 = (1<<128) XOR 0 = 1<<128
    #   bit 0 == 1: price = (1<<128) XOR ((1<<128) XOR c[0]) * 1 = (1<<128) XOR (1<<128) XOR c[0] = c[0]
    price = _Q128 if (abs_tick & 0x1 == 0) else _TICK_CONSTANTS[0]

    # Apply each subsequent bit (1..19) by multiplying with the next
    # constant and shifting right by 128 bits. The result is Q128.128
    # which stays within uint256.
    for bit in range(1, 20):
        if abs_tick & (1 << bit):
            price = (price * _TICK_CONSTANTS[bit]) >> 128

    # If tick is positive, take the integer reciprocal within uint256.
    if tick > 0:
        price = _NOT_Q256 // price

    # Convert Q128.128 -> Q128.96 with ceiling rounding
    # (price + (1<<32 - 1)) >> 32. The Solidity comment notes the
    # result fits in 160 bits due to the tick input constraint.
    sqrt_price_x96 = (price + 0xFFFFFFFF) >> 32

    # Final mask to uint160.
    sqrt_price_x96 &= (1 << 160) - 1
    return sqrt_price_x96


# ---------------------------------------------------------------------------
# TickMath: getTickAtSqrtPrice
# ---------------------------------------------------------------------------


_LOG_SQRT10001_MULTIPLIER: Final[int] = 255_738_958_999_603_826_347_141  # Q22.128
_TICK_LOW_OFFSET: Final[int] = 3_402_992_956_809_132_418_596_140_100_660_247_210
_TICK_HI_OFFSET: Final[int] = 291_339_464_771_989_622_907_027_621_153_398_088_495


def get_tick_at_sqrt_price(sqrt_price_x96: int) -> int:
    """Return the greatest tick such that ``get_sqrt_price_at_tick(tick) <= sqrt_price_x96``.

    Mirrors ``TickMath.sol::getTickAtSqrtPrice``. The bounds are strict:
    ``sqrt_price_x96 < MIN_SQRT_PRICE`` and ``sqrt_price_x96 >= MAX_SQRT_PRICE``
    are both invalid.
    """
    _require_uint160(sqrt_price_x96, field="sqrt_price_x96")
    # The V4 boundary check uses uint256 subtraction; an input below
    # MIN_SQRT_PRICE underflows to a huge uint256 value, which is > the
    # threshold and therefore rejected. Python ints do not wrap, so we
    # mask the difference to uint256 explicitly.
    diff = (sqrt_price_x96 - MIN_SQRT_PRICE_X96) & ((1 << 256) - 1)
    if diff > _MAX_SQRT_PRICE_MINUS_MIN_SQRT_PRICE_MINUS_ONE:
        raise InvalidSqrtPriceError(
            f"sqrt_price_x96={sqrt_price_x96} outside [{MIN_SQRT_PRICE_X96}, {MAX_SQRT_PRICE_X96})"
        )

    price = sqrt_price_x96 << 32  # shift into Q128.128
    r = price
    msb = _most_significant_bit(r)

    r = (price >> (msb - 127)) if msb >= 128 else (price << (127 - msb))

    log_2 = (msb - 128) << 64  # signed int256

    # Iteratively extract 14 bits of log_2. Each iteration:
    #   r = (r * r) >> 127      # Q128.128 -> Q127.129 (then truncated to 128)
    #   f = r >> 128            # next bit
    #   log_2 |= f << (63 - i)  # OR into log_2 at the next position
    #   r >>= f
    for i in range(14):
        r = (r * r) >> 127
        f = r >> 128
        log_2 |= f << (63 - i)
        r >>= f

    log_sqrt10001 = log_2 * _LOG_SQRT10001_MULTIPLIER

    tick_low = (log_sqrt10001 - _TICK_LOW_OFFSET) >> 128
    tick_hi = (log_sqrt10001 + _TICK_HI_OFFSET) >> 128

    tick_low = _clamp_int24(tick_low)
    tick_hi = _clamp_int24(tick_hi)

    if tick_low == tick_hi:
        return tick_low
    if get_sqrt_price_at_tick(tick_hi) <= sqrt_price_x96:
        return tick_hi
    return tick_low


def _clamp_int24(value: int) -> int:
    """Clamp an int256 result of the bit-shifts into the int24 domain."""
    if value < MIN_TICK:
        return MIN_TICK
    if value > MAX_TICK:
        return MAX_TICK
    return value


# ---------------------------------------------------------------------------
# SqrtPriceMath: amount0Delta / amount1Delta (roundDown)
# ---------------------------------------------------------------------------


def _require_sorted_pa_pb(pa: int, pb: int) -> tuple[int, int]:
    """Enforce pa < pb and return them in (lower, upper) order.

    V4 calls these with the *unsorted* pair (caller is responsible
    for sorting); the oracle passes them sorted. We match the
    oracle's precondition.
    """
    if not isinstance(pa, int) or isinstance(pa, bool):
        raise TypeError(f"pa: must be int, got {type(pa).__name__}")
    if not isinstance(pb, int) or isinstance(pb, bool):
        raise TypeError(f"pb: must be int, got {type(pb).__name__}")
    _require_uint160(pa, field="pa")
    _require_uint160(pb, field="pb")
    if pa >= pb:
        raise AmountDeltaError(f"pa={pa} must be strictly less than pb={pb} (range is empty)")
    return pa, pb


def get_amount0_delta(
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
    liquidity: int,
    *,
    round_up: bool = False,
) -> int:
    """Compute ``amount0`` for a liquidity position.

    Mirrors ``SqrtPriceMath.sol::getAmount0Delta(p_a, p_b, liquidity, roundUp)``.

    When ``round_up`` is ``False`` (default) the result is the
    Solidity floor value; when ``True`` the result is the ceiling.
    The caller never has to add 1 to a floor result to obtain the
    ceiling: pass ``round_up=True`` instead.

    Domain (V4 uint160/uint128): ``pa < pb``, ``0 <= liquidity < 2**128``.
    """
    pa, pb = _require_sorted_pa_pb(sqrt_price_a_x96, sqrt_price_b_x96)
    if liquidity < 0:
        raise ValueError(f"liquidity must be non-negative, got {liquidity}")
    if liquidity >= (1 << 128):
        raise ValueError(f"liquidity must fit in uint128, got {liquidity}")

    # amount0 = L * (pb - pa) * Q96 / (pa * pb)
    # Multiply first to avoid losing precision; the intermediate
    # fits in 512 bits in Python (arbitrary precision).
    numerator1 = liquidity << 96  # Q128 -> Q? * 2^96
    numerator2 = pb - pa
    denominator = pa * pb
    if round_up:
        # Solidity's mulDivRoundingUp: numerator + denominator - 1 before floor division.
        amount0 = (numerator1 * numerator2 + denominator - 1) // denominator
    else:
        amount0 = (numerator1 * numerator2) // denominator
    return amount0


def get_amount1_delta(
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
    liquidity: int,
    *,
    round_up: bool = False,
) -> int:
    """Compute ``amount1`` for a liquidity position.

    Mirrors ``SqrtPriceMath.sol::getAmount1Delta(p_a, p_b, liquidity, roundUp)``.

    When ``round_up`` is ``False`` (default) the result is the
    Solidity floor value; when ``True`` the result is the ceiling.
    The caller never has to add 1 to a floor result to obtain the
    ceiling: pass ``round_up=True`` instead.

    Domain (V4 uint160/uint128): ``pa < pb``, ``0 <= liquidity < 2**128``.
    """
    pa, pb = _require_sorted_pa_pb(sqrt_price_a_x96, sqrt_price_b_x96)
    if liquidity < 0:
        raise ValueError(f"liquidity must be non-negative, got {liquidity}")
    if liquidity >= (1 << 128):
        raise ValueError(f"liquidity must fit in uint128, got {liquidity}")

    # amount1 = L * (pb - pa) / Q96
    # Solidity does mulDiv(liquidity, pb - pa, Q96) and (when roundUp)
    # mulDivRoundingUp(liquidity, pb - pa, Q96). The Python equivalent
    # is the Q96 floor shift plus, on round_up, the pre-shift carry.
    product = liquidity * (pb - pa)
    return (product + (1 << 96) - 1) >> 96 if round_up else product >> 96


# ---------------------------------------------------------------------------
# LiquidityAmounts: getLiquidityForAmounts
# ---------------------------------------------------------------------------


def get_liquidity_for_amounts(
    sqrt_price_x96: int,
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
    amount0: int,
    amount1: int,
) -> int:
    """Compute liquidity from an (amount0, amount1) pair.

    Mirrors ``LiquidityAmounts.sol::getLiquidityForAmounts``. The
    result is the minimum of the two single-sided liquidities
    (each rounded *down*). This means a round-trip
    ``amounts_for_liquidity`` -> ``liquidity_for_amounts`` may return
    a strictly smaller liquidity than the input; see the
    ``round_trip_*`` vectors in ``math_vectors.json``.
    """
    pa, pb = _require_sorted_pa_pb(sqrt_price_a_x96, sqrt_price_b_x96)
    _require_uint160(sqrt_price_x96, field="sqrt_price_x96")
    if sqrt_price_x96 < pa or sqrt_price_x96 > pb:
        raise AmountDeltaError(f"sqrt_price_x96={sqrt_price_x96} must be in [pa={pa}, pb={pb}]")
    if amount0 < 0 or amount1 < 0:
        raise ValueError("amounts must be non-negative")

    liquidity0 = _liquidity_for_amount0(sqrt_price_x96, pa, pb, amount0)
    liquidity1 = _liquidity_for_amount1(sqrt_price_x96, pa, pb, amount1)
    return min(liquidity0, liquidity1)


def _liquidity_for_amount0(sqrt_price_x96: int, pa: int, pb: int, amount0: int) -> int:
    """Liquidity contribution from amount0 (LiquidityAmounts.getLiquidityForAmount0)."""
    if sqrt_price_x96 <= pa:
        # Entirely token0: liquidity = amount0 * pa * pb / ((pb - pa) * Q96)
        return (amount0 * pa * pb) // ((pb - pa) << 96)
    if sqrt_price_x96 < pb:
        # Mixed: liquidity = amount0 * sqrt_price_x96 * pb / ((pb - sqrt_price_x96) * Q96)
        return (amount0 * sqrt_price_x96 * pb) // ((pb - sqrt_price_x96) << 96)
    # Above the range, amount0 contributes no liquidity.
    return 0


def _liquidity_for_amount1(sqrt_price_x96: int, pa: int, pb: int, amount1: int) -> int:
    """Liquidity contribution from amount1 (LiquidityAmounts.getLiquidityForAmount1)."""
    if sqrt_price_x96 >= pb:
        # Entirely token1: liquidity = amount1 * Q96 / (pb - pa)
        return (amount1 << 96) // (pb - pa)
    if sqrt_price_x96 > pa:
        # Mixed: liquidity = amount1 * Q96 / (sqrt_price_x96 - pa)
        return (amount1 << 96) // (sqrt_price_x96 - pa)
    # Below the range, amount1 contributes no liquidity.
    return 0


# ---------------------------------------------------------------------------
# Display boundary (ADR-009)
# ---------------------------------------------------------------------------
# The protocol package is float-free. The single ``Decimal``-typed display
# helper lives in :mod:`robinhood_lp.presentation.prices` and is the only
# module in V1 permitted to convert between the integer protocol domain
# and the ``Decimal`` display domain.


__all__ = [
    "AmountDeltaError",
    "InvalidSqrtPriceError",
    "InvalidTickError",
    "MAX_SQRT_PRICE_X96",
    "MAX_TICK",
    "MAX_TICK_SPACING",
    "MIN_SQRT_PRICE_X96",
    "MIN_TICK",
    "MIN_TICK_SPACING",
    "TickMathError",
    "get_amount0_delta",
    "get_amount1_delta",
    "get_liquidity_for_amounts",
    "get_sqrt_price_at_tick",
    "get_tick_at_sqrt_price",
    "max_usable_tick",
    "min_usable_tick",
]
