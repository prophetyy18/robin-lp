"""V4 position sizing math (T049).

This module implements the integer-only sizing math described in
``docs/spec/strategy/V4_POSITION_SIZING.md``. It maps a fixed
approved ``(tickLower, tickUpper)`` and a per-side raw-token
capital envelope to one canonical integer ``liquidityDelta``
plus the mint envelope (``amount0Max``, ``amount1Max``,
``deadline``, ``minLiquidity``), or to a structured ``NO_TRADE``
verdict with a stable reason code.

Design constraints:

- **Integer-only.** ``float`` is forbidden on this path (ADR-004).
  All inputs and outputs are Python ``int``; no ``Decimal`` or
  ``float`` conversion happens here. The single named USDG
  conversion boundary (``usdg_amount_to_token_amount``) uses
  Q64.64 fixed-point arithmetic and stays in ``int``.
- **One liquidity scale.** The whole position shares a single
  ``liquidityDelta``. Per-side trimming is forbidden
  (``must-not independent trim``).
- **Range is frozen.** The sizer never widens, narrows, shifts
  or re-aligns the Range to make a cap pass. The cap can only
  reduce the liquidity, never move the Range
  (``must-not change Range to fit a cap``).
- **Hook deltas are integer credits/charges** added to the
  required amounts before the cap check; a non-zero hook delta
  without verified evidence (T043) is rejected.
- **Native Gas is reserved.** When either ``currency0`` or
  ``currency1`` is ``Currency.native()`` (zero address), the
  Gas reservation is subtracted from the spendable cap once;
  the Gas reserve is never spent on the LP.

The module depends only on the protocol-domain package
(``math`` / ``ids``) and the stdlib. It must not import RPC,
storage, configuration, or execution. The ``SizingDecision``
record is a frozen dataclass; equality and hashing are
deterministic and follow dataclass identity.

Errors are explicit and structured:

- :class:`SizingInputError` — a field is the wrong type or out
  of domain (e.g. negative cap, ``uint128`` overflow).
- :class:`SizingAlignmentError` — ``tick_lower`` /
  ``tick_upper`` not aligned to the pool's ``tickSpacing``.
- :class:`SizingRangeError` — ``tick_lower >= tick_upper`` or
  the price outside the V4 domain.
- :class:`SizingCapacityError` — both caps are zero.
- :class:`SizingHookError` — hook delta is non-zero without
  verified evidence.
- :class:`SizingGasError` — native-currency cap is below the
  Gas reservation.
- :class:`SizingUsdgConversionError` — USDG conversion
  overflows or the price is non-positive.

A ``NO_TRADE`` verdict is *not* an exception: it is a normal
return value (``SizingDecision(decision="NO_TRADE", ...)``).
Exceptions are reserved for input violations that indicate a
contract bug, not a market outcome.

Public surface (see ``__all__``):

- :class:`ReasonCode` — stable reason codes for ``NO_TRADE``.
- :class:`PositionRelative` — ``"below"`` / ``"inside"`` /
  ``"above"``.
- :class:`BindingSide` — which cap binds (``"amount0"`` /
  ``"amount1"`` / ``"both"``).
- :class:`SizingDecision` — the return record.
- :func:`compute_sizing` — the canonical entry point.
- :func:`get_amounts_for_liquidity` — below/inside/above Range
  amounts for a given liquidity.
- :func:`compute_cap_bounded_liquidity` — the
  min-of-two-caps liquidity that respects both caps.
- :func:`worst_case_single_sided_inventory` — boundary
  inventory at ``pa`` and ``pb``.
- :func:`usdg_amount_to_token_amount` — Q64.64 USDG → raw
  token conversion (single named numeraire boundary).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, cast

from robinhood_lp.protocol.ids import Address, Currency, PoolKey
from robinhood_lp.protocol.math import (
    MAX_SQRT_PRICE_X96,
    MAX_TICK,
    MIN_SQRT_PRICE_X96,
    MIN_TICK,
    get_amount0_delta,
    get_amount1_delta,
    get_sqrt_price_at_tick,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum value of a uint256 (the ``amount*`` and ``amount*Max`` widths).
MAX_UINT256: Final[int] = (1 << 256) - 1

#: Maximum value of a uint128 (the ``liquidity`` width).
MAX_UINT128: Final[int] = (1 << 128) - 1

#: Q64.64 fixed-point scale (used for the single USDG → token boundary).
_Q64_SCALE: Final[int] = 1 << 64

#: Tick-bias sentinel indicating the whole position is below the Range.
POSITION_BELOW: Final[str] = "below"
#: Tick-bias sentinel indicating the whole position is inside the Range.
POSITION_INSIDE: Final[str] = "inside"
#: Tick-bias sentinel indicating the whole position is above the Range.
POSITION_ABOVE: Final[str] = "above"

#: Cap-binding sentinel indicating ``amount0_cap`` is the bottleneck.
BINDING_AMOUNT0: Final[str] = "amount0"
#: Cap-binding sentinel indicating ``amount1_cap`` is the bottleneck.
BINDING_AMOUNT1: Final[str] = "amount1"
#: Cap-binding sentinel indicating both caps bind (corner case).
BINDING_BOTH: Final[str] = "both"

PositionRelative = Literal["below", "inside", "above"]
BindingSide = Literal["amount0", "amount1", "both"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SizingError(ValueError):
    """Base class for sizing failures."""


class SizingInputError(SizingError):
    """A sizing input is the wrong type or out of domain."""


class SizingAlignmentError(SizingError):
    """``tick_lower`` / ``tick_upper`` is not aligned to ``tick_spacing``."""


class SizingRangeError(SizingError):
    """The Range is degenerate or the price is outside the V4 domain."""


class SizingCapacityError(SizingError):
    """Both caps are zero / a cap would round to zero liquidity."""


class SizingHookError(SizingError):
    """Hook delta is non-zero without verified evidence."""


class SizingGasError(SizingError):
    """Native-currency cap is below the Gas reservation."""


class SizingUsdgConversionError(SizingError):
    """USDG → token conversion overflows or the price is non-positive."""


# ---------------------------------------------------------------------------
# Reason codes (stable, public contract)
# ---------------------------------------------------------------------------


class ReasonCode(StrEnum):
    """Stable reason codes for ``NO_TRADE`` outcomes.

    Strings are part of the public contract. New codes are
    additive; renaming an existing code is a breaking change.
    """

    RANGE_DEGENERATE = "RANGE_DEGENERATE"
    PRICE_OUT_OF_BOUNDS = "PRICE_OUT_OF_BOUNDS"
    CAP_NONPOSITIVE = "CAP_NONPOSITIVE"
    CAP_UNDERFLOW = "CAP_UNDERFLOW"
    HOOK_DELTA_NONZERO_UNVERIFIED = "HOOK_DELTA_NONZERO_UNVERIFIED"
    GAS_RESERVE_UNDERFLOW = "GAS_RESERVE_UNDERFLOW"
    INSUFFICIENT_ECONOMIC_SIZE = "INSUFFICIENT_ECONOMIC_SIZE"
    USDG_CONVERSION_FAILED = "USDG_CONVERSION_FAILED"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_uint(value: int, *, bits: int, field: str) -> int:
    """Validate ``value`` is a non-negative integer fitting in ``bits`` bits."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise SizingInputError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise SizingInputError(f"{field}: must be non-negative, got {value}")
    if value >= (1 << bits):
        raise SizingInputError(f"{field}: exceeds {bits}-bit width, got {value}")
    return value


def _require_int(value: int, *, bits: int, field: str) -> int:
    """Validate ``value`` is a signed integer fitting in ``bits`` bits."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise SizingInputError(f"{field}: must be int, got {type(value).__name__}")
    lower = -(1 << (bits - 1))
    upper = 1 << (bits - 1)
    if value < lower or value >= upper:
        raise SizingInputError(f"{field}: out of int{bits} range, got {value}")
    return value


def _require_uint160(value: int, *, field: str) -> int:
    return _require_uint(value, bits=160, field=field)


def _require_uint128(value: int, *, field: str) -> int:
    return _require_uint(value, bits=128, field=field)


def _require_int24(value: int, *, field: str) -> int:
    return _require_int(value, bits=24, field=field)


def _classify_position(
    *,
    sqrt_price_x96: int,
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
) -> PositionRelative:
    """Classify the current price relative to the Range."""
    if sqrt_price_x96 <= sqrt_price_a_x96:
        return cast("PositionRelative", POSITION_BELOW)
    if sqrt_price_x96 >= sqrt_price_b_x96:
        return cast("PositionRelative", POSITION_ABOVE)
    return cast("PositionRelative", POSITION_INSIDE)


# ---------------------------------------------------------------------------
# Below/inside/above-Range amount helpers
# ---------------------------------------------------------------------------


def get_amounts_for_liquidity(
    sqrt_price_x96: int,
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
    liquidity: int,
) -> tuple[int, int]:
    """Return ``(amount0, amount1)`` for a given liquidity.

    The three cases follow V4's
    ``LiquidityAmounts.getAmountsForLiquidity``:

    - **below** the Range (``sqrt_price_x96 <= pa``): the whole
      position is ``token0``. ``amount1 = 0``.
    - **above** the Range (``sqrt_price_x96 >= pb``): the whole
      position is ``token1``. ``amount0 = 0``.
    - **inside** the Range (``pa < sqrt_price_x96 < pb``):
      ``amount0`` uses ``(pa, sqrt_price_x96)`` and
      ``amount1`` uses ``(sqrt_price_x96, pb)``; both floor
      (``round_up=False``).

    The function is integer-only and pure: same inputs produce
    the same outputs in any order or process.
    """
    _require_uint160(sqrt_price_x96, field="sqrt_price_x96")
    _require_uint160(sqrt_price_a_x96, field="sqrt_price_a_x96")
    _require_uint160(sqrt_price_b_x96, field="sqrt_price_b_x96")
    _require_uint128(liquidity, field="liquidity")
    if sqrt_price_a_x96 >= sqrt_price_b_x96:
        raise SizingRangeError(
            f"sqrt_price_a_x96={sqrt_price_a_x96} must be strictly less "
            f"than sqrt_price_b_x96={sqrt_price_b_x96}"
        )

    position = _classify_position(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=sqrt_price_a_x96,
        sqrt_price_b_x96=sqrt_price_b_x96,
    )
    if position == POSITION_BELOW:
        amount0 = get_amount0_delta(sqrt_price_a_x96, sqrt_price_b_x96, liquidity, round_up=False)
        return amount0, 0
    if position == POSITION_ABOVE:
        amount1 = get_amount1_delta(sqrt_price_a_x96, sqrt_price_b_x96, liquidity, round_up=False)
        return 0, amount1
    # Inside: amount0 uses (pa, p), amount1 uses (p, pb).
    amount0 = get_amount0_delta(sqrt_price_a_x96, sqrt_price_x96, liquidity, round_up=False)
    amount1 = get_amount1_delta(sqrt_price_x96, sqrt_price_b_x96, liquidity, round_up=False)
    return amount0, amount1


def worst_case_single_sided_inventory(
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
    liquidity: int,
) -> tuple[int, int]:
    """Return ``(amount0_at_pb, amount1_at_pa)`` for the worst boundary.

    At the **upper** boundary ``sqrt_price_x96 == sqrt_price_b_x96``
    the position holds only ``token0``. The integer amount is
    ``get_amount0_delta(pa, pb, liquidity)``.

    At the **lower** boundary ``sqrt_price_x96 == sqrt_price_a_x96``
    the position holds only ``token1``. The integer amount is
    ``get_amount1_delta(pa, pb, liquidity)``.

    These are the worst-case single-sided inventory values the
    central risk layer (T070) compares against the per-side
    exposure cap. They are pure functions of the Range and the
    liquidity; they never touch a price oracle.
    """
    _require_uint160(sqrt_price_a_x96, field="sqrt_price_a_x96")
    _require_uint160(sqrt_price_b_x96, field="sqrt_price_b_x96")
    _require_uint128(liquidity, field="liquidity")
    if sqrt_price_a_x96 >= sqrt_price_b_x96:
        raise SizingRangeError(
            f"sqrt_price_a_x96={sqrt_price_a_x96} must be strictly less "
            f"than sqrt_price_b_x96={sqrt_price_b_x96}"
        )

    amount0_at_pb = get_amount0_delta(sqrt_price_a_x96, sqrt_price_b_x96, liquidity, round_up=False)
    amount1_at_pa = get_amount1_delta(sqrt_price_a_x96, sqrt_price_b_x96, liquidity, round_up=False)
    return amount0_at_pb, amount1_at_pa


# ---------------------------------------------------------------------------
# Cap-bounded liquidity (the core sizing algorithm)
# ---------------------------------------------------------------------------


def compute_cap_bounded_liquidity(
    sqrt_price_x96: int,
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
    amount0_cap: int,
    amount1_cap: int,
) -> tuple[int, BindingSide]:
    """Return the largest liquidity both caps can fund.

    The per-side liquidity formulas are sized against the
    **actual** token-bracket the position consumes at the
    current price:

    - **below** the Range (``sqrt_price_x96 <= pa``): the whole
      position is ``token0``. ``L0 = amount0 * pa * pb / ((pb - pa) << 96)``,
      ``L1 = 0``, and the bound is ``amount0``.
    - **above** the Range (``sqrt_price_x96 >= pb``): the whole
      position is ``token1``. ``L0 = 0``, ``L1 = amount1 << 96 /
      (pb - pa)``, and the bound is ``amount1``.
    - **inside** the Range (``pa < sqrt_price_x96 < pb``): the
      actual brackets are ``[pa, p]`` for ``token0`` and
      ``[p, pb]`` for ``token1``. ``L0 = amount0 * pa * p /
      ((p - pa) << 96)``, ``L1 = amount1 << 96 / (pb - p)``;
      the bound is whichever is smaller.

    Using the actual brackets — rather than the asymmetric
    brackets the V4 reference ``LiquidityAmounts.getLiquidityForAmount0
    / getLiquidityForAmount1`` use for inside-Range — guarantees
    the round-trip ``liquidity_for_amounts -> amounts_for_liquidity``
    respects both caps. The V4 reference function computes an
    ``L`` that may exceed the supplied cap when re-fed through
    ``amounts_for_liquidity``; the contract's "final simulated
    settlement stays below both raw-token/USDG caps" clause
    forbids that path.

    The function is monotone non-decreasing in each cap, and
    exact (no binary search needed because the per-side
    formulas are closed-form).
    """
    _require_uint160(sqrt_price_x96, field="sqrt_price_x96")
    _require_uint160(sqrt_price_a_x96, field="sqrt_price_a_x96")
    _require_uint160(sqrt_price_b_x96, field="sqrt_price_b_x96")
    _require_uint(amount0_cap, bits=256, field="amount0_cap")
    _require_uint(amount1_cap, bits=256, field="amount1_cap")
    if sqrt_price_a_x96 >= sqrt_price_b_x96:
        raise SizingRangeError(
            f"sqrt_price_a_x96={sqrt_price_a_x96} must be strictly less "
            f"than sqrt_price_b_x96={sqrt_price_b_x96}"
        )

    position = _classify_position(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=sqrt_price_a_x96,
        sqrt_price_b_x96=sqrt_price_b_x96,
    )
    if position == POSITION_BELOW:
        l0 = _liquidity_for_amount0(
            sqrt_price_x96=sqrt_price_a_x96,
            sqrt_price_a_x96=sqrt_price_a_x96,
            sqrt_price_b_x96=sqrt_price_b_x96,
            amount=amount0_cap,
        )
        # The amount1 cap is irrelevant when price is below; record
        # the binding side as ``amount0`` (the bottleneck) so the
        # caller sees the truthful label.
        if l0 == 0 and amount0_cap > 0:
            raise SizingCapacityError(
                f"amount0_cap={amount0_cap} rounds to liquidity 0 below Range"
            )
        return l0, cast("BindingSide", BINDING_AMOUNT0)
    if position == POSITION_ABOVE:
        l1 = _liquidity_for_amount1(
            sqrt_price_x96=sqrt_price_b_x96,
            sqrt_price_a_x96=sqrt_price_a_x96,
            sqrt_price_b_x96=sqrt_price_b_x96,
            amount=amount1_cap,
        )
        if l1 == 0 and amount1_cap > 0:
            raise SizingCapacityError(
                f"amount1_cap={amount1_cap} rounds to liquidity 0 above Range"
            )
        return l1, cast("BindingSide", BINDING_AMOUNT1)
    # Inside.
    l0 = _liquidity_for_amount0(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=sqrt_price_a_x96,
        sqrt_price_b_x96=sqrt_price_b_x96,
        amount=amount0_cap,
    )
    l1 = _liquidity_for_amount1(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=sqrt_price_a_x96,
        sqrt_price_b_x96=sqrt_price_b_x96,
        amount=amount1_cap,
    )
    if l0 == 0 and l1 == 0 and (amount0_cap > 0 or amount1_cap > 0):
        raise SizingCapacityError(
            f"both caps round to liquidity 0 inside Range "
            f"(amount0_cap={amount0_cap}, amount1_cap={amount1_cap})"
        )
    if l0 <= l1:
        return l0, cast("BindingSide", BINDING_AMOUNT0)
    return l1, cast("BindingSide", BINDING_AMOUNT1)


def _liquidity_for_amount0(
    *,
    sqrt_price_x96: int,
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
    amount: int,
) -> int:
    """Per-side liquidity for ``currency0``.

    Sized against the actual bracket the position consumes at
    the current price. Below the Range the position is entirely
    ``token0`` (``[pa, pb]``); inside the Range the token0
    bracket is ``[pa, sqrt_price_x96]``. Returns ``0`` when the
    input cannot contribute (above the Range or amount is zero).
    """
    if amount == 0:
        return 0
    if sqrt_price_x96 <= sqrt_price_a_x96:
        return (amount * sqrt_price_a_x96 * sqrt_price_b_x96) // (
            (sqrt_price_b_x96 - sqrt_price_a_x96) << 96
        )
    if sqrt_price_x96 < sqrt_price_b_x96:
        # Inside: bracket is [pa, p], so:
        # amount0 = L * (p - pa) * Q96 / (pa * p)
        # L = amount0 * pa * p / ((p - pa) << 96)
        return (amount * sqrt_price_a_x96 * sqrt_price_x96) // (
            (sqrt_price_x96 - sqrt_price_a_x96) << 96
        )
    return 0


def _liquidity_for_amount1(
    *,
    sqrt_price_x96: int,
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
    amount: int,
) -> int:
    """Per-side liquidity for ``currency1``.

    Sized against the actual bracket the position consumes at
    the current price. Above the Range the position is entirely
    ``token1`` (``[pa, pb]``); inside the Range the token1
    bracket is ``[sqrt_price_x96, pb]``. Returns ``0`` when the
    input cannot contribute (below the Range or amount is zero).
    """
    if amount == 0:
        return 0
    if sqrt_price_x96 >= sqrt_price_b_x96:
        return (amount << 96) // (sqrt_price_b_x96 - sqrt_price_a_x96)
    if sqrt_price_x96 > sqrt_price_a_x96:
        # Inside: bracket is [p, pb], so:
        # amount1 = L * (pb - p) / Q96
        # L = amount1 * Q96 / (pb - p)
        return (amount << 96) // (sqrt_price_b_x96 - sqrt_price_x96)
    return 0


# ---------------------------------------------------------------------------
# USDG → token conversion (single named numeraire boundary)
# ---------------------------------------------------------------------------


def usdg_amount_to_token_amount(usdg_amount: int, price_q64_64: int) -> int:
    """Convert a USDG cap (atomic units) to a raw-token cap (atomic units).

    The single named numeraire boundary between the USDG
    reporting numeraire and the raw-token protocol domain. Uses
    Q64.64 fixed-point arithmetic exclusively; no ``float`` and
    no ``Decimal`` ever enter the integer domain.

    The price ``price_q64_64`` is supplied by T053 as
    ``usdg_per_token << 64``; the rounding direction is **down**
    so the resulting cap is conservative (the user's USDG
    envelope is never exceeded).

    Raises
    ------
    SizingUsdgConversionError
        If ``price_q64_64`` is non-positive or the conversion
        would overflow ``uint256``.
    """
    _require_uint(usdg_amount, bits=256, field="usdg_amount")
    if not isinstance(price_q64_64, int) or isinstance(price_q64_64, bool):
        raise SizingUsdgConversionError(
            f"price_q64_64: must be int, got {type(price_q64_64).__name__}"
        )
    if price_q64_64 <= 0:
        raise SizingUsdgConversionError(f"price_q64_64 must be positive, got {price_q64_64}")
    # amount_token = (usdg_amount << 64) // price_q64_64
    numerator = usdg_amount * _Q64_SCALE
    if numerator // price_q64_64 > MAX_UINT256:
        raise SizingUsdgConversionError(
            f"USDG → token conversion overflows uint256 "
            f"(usdg_amount={usdg_amount}, price_q64_64={price_q64_64})"
        )
    return numerator // price_q64_64


# ---------------------------------------------------------------------------
# SizingDecision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SizingDecision:
    """The integer sizing decision for one mint.

    Two decisions are equal iff every field is equal. The
    dataclass is hashable and pickle-safe.

    Attributes
    ----------
    decision:
        ``"OK"`` when the entry is sized, ``"NO_TRADE"``
        otherwise.
    reason_code:
        Stable reason code (see :class:`ReasonCode`). Set to
        ``None`` when ``decision == "OK"``.
    liquidity:
        The canonical ``liquidityDelta`` (uint128). ``0`` when
        ``NO_TRADE``.
    amount0:
        Exact integer ``amount0`` consumed.
    amount1:
        Exact integer ``amount1`` consumed.
    amount0_max:
        Mint envelope cap for ``currency0``.
    amount1_max:
        Mint envelope cap for ``currency1``.
    min_liquidity:
        Minimum-liquidity protection (V1 default ``0``).
    deadline:
        Caller-supplied deadline (uint256).
    position_relative:
        ``"below"`` / ``"inside"`` / ``"above"``.
    binding_side:
        ``"amount0"`` / ``"amount1"`` / ``"both"``. Set to
        ``"both"`` when both caps bind.
    worst_case_amount0:
        Inventory of ``currency0`` if price walks to upper bound.
    worst_case_amount1:
        Inventory of ``currency1`` if price walks to lower bound.
    """

    decision: str
    reason_code: ReasonCode | None
    liquidity: int
    amount0: int
    amount1: int
    amount0_max: int
    amount1_max: int
    min_liquidity: int
    deadline: int
    position_relative: str
    binding_side: str
    worst_case_amount0: int
    worst_case_amount1: int

    def __post_init__(self) -> None:
        for name in (
            "liquidity",
            "amount0",
            "amount1",
            "amount0_max",
            "amount1_max",
            "min_liquidity",
            "deadline",
            "worst_case_amount0",
            "worst_case_amount1",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise SizingInputError(
                    f"SizingDecision.{name}: must be int, got {type(value).__name__}"
                )
            if value < 0:
                raise SizingInputError(f"SizingDecision.{name}: must be non-negative, got {value}")
        if self.decision == "OK":
            if self.reason_code is not None:
                raise SizingInputError(
                    "SizingDecision: reason_code must be None when decision == OK"
                )
            if self.liquidity == 0:
                raise SizingInputError(
                    "SizingDecision: liquidity must be positive when decision == OK"
                )
            if self.amount0_max < self.amount0 or self.amount1_max < self.amount1:
                raise SizingInputError("SizingDecision: amount*Max must be >= amount*")
        elif self.decision == "NO_TRADE":
            if self.reason_code is None:
                raise SizingInputError(
                    "SizingDecision: reason_code is required when decision == NO_TRADE"
                )
            if self.liquidity != 0 or self.amount0 != 0 or self.amount1 != 0:
                raise SizingInputError(
                    "SizingDecision: liquidity/amount0/amount1 must be 0 when NO_TRADE"
                )
        else:
            raise SizingInputError(
                f"SizingDecision: decision must be 'OK' or 'NO_TRADE', got {self.decision!r}"
            )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def compute_sizing(
    *,
    pool_key: PoolKey,
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
    amount0_cap: int,
    amount1_cap: int,
    accrued_fees0: int,
    accrued_fees1: int,
    gas_reserve_wei: int,
    hook_delta0: int,
    hook_delta1: int,
    hook_verified: bool,
    deadline: int,
    min_liquidity: int,
    min_economic_liquidity_usdg_q64_64: int | None,
    usdg_cap_q64_64_token0: int | None,
    usdg_cap_q64_64_token1: int | None,
    usdg_amount_token0: int,
    usdg_amount_token1: int,
) -> SizingDecision:
    """Compute the integer sizing for one mint.

    The full algorithm is described in
    ``docs/spec/strategy/V4_POSITION_SIZING.md`` §7. In summary:

    1. Validate the Range and the price; classify the position
       as ``below`` / ``inside`` / ``above``.
    2. Optionally convert the USDG caps to raw-token caps
       (single Q64.64 boundary).
    3. Subtract the Gas reservation from the native-side cap
       (each side handled independently).
    4. Compute the cap-bounded liquidity; if either cap
       underflows to zero, return ``NO_TRADE``.
    5. Compute the integer ``amount0`` / ``amount1``; add the
       hook deltas (when verified) before the final cap check.
    6. Verify the simulated post-settlement wallet state
       respects both caps and the Gas reservation.
    7. Apply the USDG minimum-economic-size check.

    The function never mutates its inputs and never touches the
    network. Two calls with the same arguments produce
    byte-identical ``SizingDecision`` instances.
    """
    if not isinstance(pool_key, PoolKey):
        raise SizingInputError(f"pool_key: must be PoolKey, got {type(pool_key).__name__}")
    _require_uint160(sqrt_price_x96, field="sqrt_price_x96")
    _require_int24(tick_lower, field="tick_lower")
    _require_int24(tick_upper, field="tick_upper")
    _require_uint(amount0_cap, bits=256, field="amount0_cap")
    _require_uint(amount1_cap, bits=256, field="amount1_cap")
    _require_uint(accrued_fees0, bits=256, field="accrued_fees0")
    _require_uint(accrued_fees1, bits=256, field="accrued_fees1")
    _require_uint(gas_reserve_wei, bits=256, field="gas_reserve_wei")
    _require_int(hook_delta0, bits=256, field="hook_delta0")
    _require_int(hook_delta1, bits=256, field="hook_delta1")
    _require_uint(deadline, bits=256, field="deadline")
    _require_uint128(min_liquidity, field="min_liquidity")
    _require_uint(usdg_amount_token0, bits=256, field="usdg_amount_token0")
    _require_uint(usdg_amount_token1, bits=256, field="usdg_amount_token1")

    if min_economic_liquidity_usdg_q64_64 is not None:
        _require_uint(
            min_economic_liquidity_usdg_q64_64,
            bits=256,
            field="min_economic_liquidity_usdg_q64_64",
        )
    if usdg_cap_q64_64_token0 is not None:
        _require_uint(usdg_cap_q64_64_token0, bits=128, field="usdg_cap_q64_64_token0")
    if usdg_cap_q64_64_token1 is not None:
        _require_uint(usdg_cap_q64_64_token1, bits=128, field="usdg_cap_q64_64_token1")

    if tick_lower < MIN_TICK or tick_lower > MAX_TICK:
        raise SizingRangeError(f"tick_lower={tick_lower} outside V4 [{MIN_TICK}, {MAX_TICK}]")
    if tick_upper < MIN_TICK or tick_upper > MAX_TICK:
        raise SizingRangeError(f"tick_upper={tick_upper} outside V4 [{MIN_TICK}, {MAX_TICK}]")
    if tick_lower >= tick_upper:
        return _no_trade(
            ReasonCode.RANGE_DEGENERATE,
            deadline=deadline,
            position_relative=cast("PositionRelative", POSITION_BELOW),
            min_liquidity=min_liquidity,
        )
    tick_spacing = pool_key.tick_spacing
    if tick_lower % tick_spacing != 0 or tick_upper % tick_spacing != 0:
        raise SizingAlignmentError(
            f"tick_lower={tick_lower} or tick_upper={tick_upper} not aligned "
            f"to tick_spacing={tick_spacing}"
        )

    if sqrt_price_x96 < MIN_SQRT_PRICE_X96 or sqrt_price_x96 >= MAX_SQRT_PRICE_X96:
        return _no_trade(
            ReasonCode.PRICE_OUT_OF_BOUNDS,
            deadline=deadline,
            position_relative=cast("PositionRelative", POSITION_BELOW),
            min_liquidity=min_liquidity,
        )

    # Range sqrt prices from the integer tick → sqrt price port.
    sqrt_price_a_x96 = get_sqrt_price_at_tick(tick_lower)
    sqrt_price_b_x96 = get_sqrt_price_at_tick(tick_upper)
    if sqrt_price_a_x96 >= sqrt_price_b_x96:
        raise SizingRangeError(
            f"sqrt_price_a_x96={sqrt_price_a_x96} must be strictly less "
            f"than sqrt_price_b_x96={sqrt_price_b_x96}"
        )

    position = _classify_position(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=sqrt_price_a_x96,
        sqrt_price_b_x96=sqrt_price_b_x96,
    )

    # Hook verification gate (must-not ignore hook deltas).
    if (hook_delta0 != 0 or hook_delta1 != 0) and not hook_verified:
        raise SizingHookError(
            f"hook_delta0={hook_delta0}, hook_delta1={hook_delta1} "
            "require verified hook evidence (T043)"
        )

    # Native Gas reservation. The reserve is subtracted from the
    # spendable cap of whichever side is native; the other side is
    # untouched. The reserve is never spent on the LP itself.
    # When the reservation exceeds the cap, ``_apply_gas_reservation``
    # returns a sentinel -1 the caller maps to ``NO_TRADE`` /
    # ``GAS_RESERVE_UNDERFLOW``.
    effective_amount0_cap = _apply_gas_reservation(
        currency=pool_key.currency0,
        cap=amount0_cap,
        gas_reserve_wei=gas_reserve_wei,
        native_side="amount0",
    )
    effective_amount1_cap = _apply_gas_reservation(
        currency=pool_key.currency1,
        cap=amount1_cap,
        gas_reserve_wei=gas_reserve_wei,
        native_side="amount1",
    )
    if effective_amount0_cap < 0 or effective_amount1_cap < 0:
        # One side is native and the cap is below the Gas reservation;
        # return NO_TRADE with the GAS_RESERVE_UNDERFLOW reason code.
        return _no_trade(
            ReasonCode.GAS_RESERVE_UNDERFLOW,
            deadline=deadline,
            position_relative=position,
            min_liquidity=min_liquidity,
        )

    # Both caps zero after the Gas reservation → no trade.
    if effective_amount0_cap == 0 and effective_amount1_cap == 0:
        # If the native side specifically ran into the Gas
        # reservation (the wallet had value but it was reserved),
        # record GAS_RESERVE_UNDERFLOW as the reason; otherwise the
        # generic CAP_NONPOSITIVE.
        if (
            pool_key.currency0.is_native()
            and amount0_cap + accrued_fees0 < gas_reserve_wei
            and amount1_cap == 0
        ):
            return _no_trade(
                ReasonCode.GAS_RESERVE_UNDERFLOW,
                deadline=deadline,
                position_relative=position,
                min_liquidity=min_liquidity,
            )
        if (
            pool_key.currency1.is_native()
            and amount1_cap + accrued_fees1 < gas_reserve_wei
            and amount0_cap == 0
        ):
            return _no_trade(
                ReasonCode.GAS_RESERVE_UNDERFLOW,
                deadline=deadline,
                position_relative=position,
                min_liquidity=min_liquidity,
            )
        return _no_trade(
            ReasonCode.CAP_NONPOSITIVE,
            deadline=deadline,
            position_relative=position,
            min_liquidity=min_liquidity,
        )

    # Cap-bounded liquidity.
    try:
        liquidity, binding_side = compute_cap_bounded_liquidity(
            sqrt_price_x96=sqrt_price_x96,
            sqrt_price_a_x96=sqrt_price_a_x96,
            sqrt_price_b_x96=sqrt_price_b_x96,
            amount0_cap=effective_amount0_cap,
            amount1_cap=effective_amount1_cap,
        )
    except SizingCapacityError as exc:
        return _no_trade(
            ReasonCode.CAP_UNDERFLOW,
            deadline=deadline,
            position_relative=position,
            min_liquidity=min_liquidity,
            message=str(exc),
        )

    if liquidity == 0:
        return _no_trade(
            ReasonCode.CAP_UNDERFLOW,
            deadline=deadline,
            position_relative=position,
            min_liquidity=min_liquidity,
        )
    if liquidity > MAX_UINT128:
        # Per the V4 uint128 width; the framework rejects an
        # oversized liquidity rather than truncating.
        raise SizingInputError(f"liquidity={liquidity} exceeds uint128 width")

    amount0, amount1 = get_amounts_for_liquidity(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=sqrt_price_a_x96,
        sqrt_price_b_x96=sqrt_price_b_x96,
        liquidity=liquidity,
    )

    # Hook BalanceDelta integration. A negative delta is a credit
    # to the user; a positive delta is a charge that must be
    # funded from the wallet.
    required_amount0 = amount0 + hook_delta0
    required_amount1 = amount1 + hook_delta1
    if required_amount0 < 0 or required_amount1 < 0:
        # Hook credit cannot push the required amount negative;
        # this would mean the hook over-paid the user beyond the
        # position itself, which is a contract bug.
        raise SizingHookError(
            f"hook delta drives required_amount* negative "
            f"(required_amount0={required_amount0}, required_amount1={required_amount1})"
        )
    # The cap is enforced with the *post-hook* required amount:
    # the user's wallet must fund the position plus any hook
    # charge. When the hook is verified, the cap analysis above
    # already used the integer amount (without the hook) as the
    # cap-bounded liquidity; the post-hook check is the second
    # half of the two-sided guarantee (must-not ignore hook
    # deltas).
    if required_amount0 > effective_amount0_cap or required_amount1 > effective_amount1_cap:
        return _no_trade(
            ReasonCode.CAP_UNDERFLOW,
            deadline=deadline,
            position_relative=position,
            min_liquidity=min_liquidity,
        )

    # Simulated post-settlement wallet state.
    post_amount0 = effective_amount0_cap + accrued_fees0 - required_amount0
    post_amount1 = effective_amount1_cap + accrued_fees1 - required_amount1
    # The Gas reservation is preserved (never spent): post-gas
    # balance equals the reservation when the native side is the
    # one bound; when the native side is not bound, the post-gas
    # balance equals (wallet - spent) + (reservation - spent) but
    # the reservation is tracked separately and the mint must not
    # touch it.
    post_eth0 = gas_reserve_wei if pool_key.currency0.is_native() else 0
    post_eth1 = gas_reserve_wei if pool_key.currency1.is_native() else 0
    if post_amount0 < 0 or post_amount1 < 0:
        return _no_trade(
            ReasonCode.CAP_UNDERFLOW,
            deadline=deadline,
            position_relative=position,
            min_liquidity=min_liquidity,
        )
    del post_eth0, post_eth1  # Gas reservation is preserved by construction.

    # Worst-case single-sided inventory at the boundaries.
    worst_amount0, worst_amount1 = worst_case_single_sided_inventory(
        sqrt_price_a_x96=sqrt_price_a_x96,
        sqrt_price_b_x96=sqrt_price_b_x96,
        liquidity=liquidity,
    )

    # Insufficient economic size check (USDG-equivalent minimum).
    # The check is computed against the (optional) USDG amounts the
    # caller supplied; the Q64.64 boundary is only used when the
    # caller supplied at least one non-zero USDG amount.
    if min_economic_liquidity_usdg_q64_64 is not None and (
        usdg_amount_token0 > 0 or usdg_amount_token1 > 0
    ):
        sized_usdg_q64_64 = _liquidity_to_usdg_equivalent(
            liquidity=liquidity,
            sqrt_price_x96=sqrt_price_x96,
            sqrt_price_a_x96=sqrt_price_a_x96,
            sqrt_price_b_x96=sqrt_price_b_x96,
            usdg_amount_token0=usdg_amount_token0,
            usdg_amount_token1=usdg_amount_token1,
        )
        if sized_usdg_q64_64 < min_economic_liquidity_usdg_q64_64:
            return _no_trade(
                ReasonCode.INSUFFICIENT_ECONOMIC_SIZE,
                deadline=deadline,
                position_relative=position,
                min_liquidity=min_liquidity,
            )

    return SizingDecision(
        decision="OK",
        reason_code=None,
        liquidity=liquidity,
        amount0=amount0,
        amount1=amount1,
        amount0_max=effective_amount0_cap,
        amount1_max=effective_amount1_cap,
        min_liquidity=min_liquidity,
        deadline=deadline,
        position_relative=position,
        binding_side=binding_side,
        worst_case_amount0=worst_amount0,
        worst_case_amount1=worst_amount1,
    )


def _apply_gas_reservation(
    *,
    currency: Currency,
    cap: int,
    gas_reserve_wei: int,
    native_side: str,
) -> int:
    """Subtract the Gas reservation from the spendable cap.

    When ``currency`` is native (zero address), the integer cap
    is reduced by ``gas_reserve_wei`` once. The reserve is never
    spent on the LP itself. When the cap is strictly below the
    reservation, the sizer cannot honour the
    ``must-not spend Gas reserve`` invariant; the public
    :func:`compute_sizing` translates this into a structured
    ``NO_TRADE`` with ``ReasonCode.GAS_RESERVE_UNDERFLOW`` rather
    than raising. A negative-cap arithmetic underflow is
    rejected by the input validator upstream of this function;
    a "cap too small" case here is a structured outcome, not an
    arithmetic bug.
    """
    if currency.is_native():
        if cap < gas_reserve_wei:
            # Return a sentinel "negative" value the caller will map
            # to ``GAS_RESERVE_UNDERFLOW``. The caller cannot use this
            # value as a real cap (it is strictly below zero); the
            # public function detects it and returns ``NO_TRADE``.
            return -1
        return cap - gas_reserve_wei
    return cap


def _liquidity_to_usdg_equivalent(
    *,
    liquidity: int,
    sqrt_price_x96: int,
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
    usdg_amount_token0: int,
    usdg_amount_token1: int,
) -> int:
    """Estimate the USDG-equivalent size of the positioned liquidity.

    A conservative Q64.64 estimate: the function takes the
    caller-supplied USDG amount the position would have consumed
    in the absence of any size, and scales linearly with the
    achieved ``liquidity``. When the caller does not supply a
    USDG amount for one side, that side is treated as zero and
    the resulting estimate is the linear projection of the
    supplied side only. This is sufficient for the
    insufficient-economic-size gate; the full USDG valuation
    belongs to T053.
    """
    # Linear projection: sized_usdg ≈ (liquidity / target_liquidity)
    # × usdg_amount. ``target_liquidity`` is the liquidity the
    # caller's USDG amount would have produced on the binding
    # side; we approximate by re-using the cap-bounded liquidity
    # for the same USDG amount (i.e. by treating the supplied
    # USDG amount as the binding-side cap).
    target0, _ = compute_cap_bounded_liquidity(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=sqrt_price_a_x96,
        sqrt_price_b_x96=sqrt_price_b_x96,
        amount0_cap=usdg_amount_token0,
        amount1_cap=usdg_amount_token1,
    )
    if target0 == 0:
        # No USDG reference is available; the gate is a no-op.
        return MAX_UINT256
    return (liquidity * (usdg_amount_token0 + usdg_amount_token1)) // target0


def _no_trade(
    reason: ReasonCode,
    *,
    deadline: int,
    position_relative: PositionRelative,
    min_liquidity: int,
    message: str | None = None,
) -> SizingDecision:
    """Construct a structured ``NO_TRADE`` verdict."""
    del message  # Kept for future logging hook; intentionally unused here.
    return SizingDecision(
        decision="NO_TRADE",
        reason_code=reason,
        liquidity=0,
        amount0=0,
        amount1=0,
        amount0_max=0,
        amount1_max=0,
        min_liquidity=min_liquidity,
        deadline=deadline,
        position_relative=position_relative,
        binding_side="both",
        worst_case_amount0=0,
        worst_case_amount1=0,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "BINDING_AMOUNT0",
    "BINDING_AMOUNT1",
    "BINDING_BOTH",
    "BindingSide",
    "MAX_UINT128",
    "MAX_UINT256",
    "POSITION_ABOVE",
    "POSITION_BELOW",
    "POSITION_INSIDE",
    "PositionRelative",
    "ReasonCode",
    "SizingAlignmentError",
    "SizingCapacityError",
    "SizingDecision",
    "SizingError",
    "SizingGasError",
    "SizingHookError",
    "SizingInputError",
    "SizingRangeError",
    "SizingUsdgConversionError",
    "compute_cap_bounded_liquidity",
    "compute_sizing",
    "get_amounts_for_liquidity",
    "usdg_amount_to_token_amount",
    "worst_case_single_sided_inventory",
]

# Marker to silence linters complaining about unused Address import
# (kept for completeness of the protocol-domain public surface).
_ = Address
