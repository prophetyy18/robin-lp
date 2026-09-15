"""Display/Decimal boundary for V4 sqrt prices (T012, ADR-009).

This module is the single named display boundary for V4 sqrt
prices. The integer protocol domain (``sqrtPriceX96``, an
unsigned 160-bit integer) is converted here to a ``Decimal``-typed
display price that accounts for the per-token decimal precision.

The only public function in V1 is :func:`to_display_price`. The
inverse direction (display price -> integer protocol domain) does
not exist in V1; users needing an inverse compute it from a USDG
source via T053 (post-P01).

No :class:`float` is used anywhere on the path. The intermediate
arithmetic is integer-only; the result is constructed as a
:class:`decimal.Decimal` from an integer numerator and an integer
power-of-two denominator, then rounded to a fixed 18 fractional
digits via :class:`decimal.Decimal` itself.
"""

from __future__ import annotations

from decimal import Decimal, getcontext

# Pin the working precision high enough to keep 18 fractional digits
# exact after the Q192 reduction. The arithmetic involves at most
# 160-bit numerators and 192-bit denominators, which is well within
# ``getcontext().prec = 78``'s reach; 60 is comfortably enough.
getcontext().prec = 60

#: Fractional digits retained in the returned ``Decimal`` display
#: price. 18 covers every realistic ERC-20 exponent and is the
#: canonical ``Decimal`` "human" precision for V1 reporting.
DISPLAY_PRICE_QUANTUM_EXPONENT: int = 18

#: One quantum at ``DISPLAY_PRICE_QUANTUM_EXPONENT`` fractional
#: digits, used to round the display price to the chosen precision.
_DISPLAY_QUANTUM: Decimal = Decimal(10) ** -DISPLAY_PRICE_QUANTUM_EXPONENT

#: Constant ``1 << 192`` represented as a ``Decimal``. Constructed
#: lazily inside :func:`to_display_price` to avoid paying the
#: conversion cost on module import.


def _q192() -> int:
    """Return ``2 ** 192`` as an int (the Q96 * Q96 normalisation)."""
    return 1 << 192


def to_display_price(
    sqrt_price_x96: int,
    decimals0: int,
    decimals1: int,
) -> Decimal:
    """Convert a ``sqrtPriceX96`` integer to a ``Decimal`` display price.

    The V4 unit ``sqrtPriceX96`` represents ``sqrt(price) * 2**96``
    where ``price`` is the *raw* token1/token0 ratio (i.e. one unit
    of token0 measures ``price`` atomic units of token1). To get a
    *human* price in the natural token units we scale by
    ``10 ** (decimals1 - decimals0)``:

        display_price = (sqrt_price_x96 ** 2) * 10**(decimals1 - decimals0)
                        / 2**192

    Equivalent to:

        display_price = (sqrt_price_x96 * 10**(decimals1 - decimals0) // 2**96)
                        * sqrt_price_x96
                        / 2**96

    but the function performs all intermediate multiplications as
    Python ints before dividing by ``2**192`` to avoid any
    precision loss, then constructs the final ``Decimal`` from the
    integer numerator / ``Decimal(2**192)`` quotient, rounding to
    ``DISPLAY_PRICE_QUANTUM_EXPONENT`` fractional digits.

    Parameters
    ----------
    sqrt_price_x96:
        Unsigned 160-bit V4 sqrt price (``0 <= x < 2**160``).
    decimals0:
        ERC-20 decimals of the base token (token0). Must satisfy
        ``0 <= decimals0 <= 36``; values outside that range are
        rejected.
    decimals1:
        ERC-20 decimals of the quote token (token1). Must satisfy
        ``0 <= decimals1 <= 36``.

    Returns
    -------
    decimal.Decimal
        The human price of one unit of token0 expressed in units of
        token1, rounded to 18 fractional digits.

    Raises
    ------
    TypeError
        If ``sqrt_price_x96`` is not an ``int`` (bool is rejected),
        or if either decimals value is not an ``int``.
    ValueError
        If ``sqrt_price_x96`` is negative, exceeds ``2**160 - 1``,
        or if either decimals value is outside ``[0, 36]``.
    """
    if not isinstance(sqrt_price_x96, int) or isinstance(sqrt_price_x96, bool):
        raise TypeError(f"sqrt_price_x96: must be int, got {type(sqrt_price_x96).__name__}")
    if sqrt_price_x96 < 0:
        raise ValueError(f"sqrt_price_x96: must be non-negative, got {sqrt_price_x96}")
    if sqrt_price_x96 >= (1 << 160):
        raise ValueError(f"sqrt_price_x96: exceeds uint160, got {sqrt_price_x96}")

    if not isinstance(decimals0, int) or isinstance(decimals0, bool):
        raise TypeError(f"decimals0: must be int, got {type(decimals0).__name__}")
    if not isinstance(decimals1, int) or isinstance(decimals1, bool):
        raise TypeError(f"decimals1: must be int, got {type(decimals1).__name__}")
    if decimals0 < 0 or decimals0 > 36:
        raise ValueError(f"decimals0: must be in [0, 36], got {decimals0}")
    if decimals1 < 0 or decimals1 > 36:
        raise ValueError(f"decimals1: must be in [0, 36], got {decimals1}")

    # Apply the decimal-scale shift to the integer numerator using
    # only int arithmetic. We materialise the scale as an int so the
    # entire computation stays in integer space.
    int_numerator = sqrt_price_x96 * sqrt_price_x96 * (10 ** (decimals1 - decimals0))
    q192_int = _q192()
    # Decimal division: integer numerator / integer denominator.
    # Quantize to DISPLAY_PRICE_QUANTUM_EXPONENT fractional digits.
    price = Decimal(int_numerator) / Decimal(q192_int)
    return price.quantize(_DISPLAY_QUANTUM)


__all__ = [
    "DISPLAY_PRICE_QUANTUM_EXPONENT",
    "to_display_price",
]
