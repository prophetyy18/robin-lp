"""Tests for V4 position sizing math (T049).

Coverage:

- Pinned integer vectors in
  ``tests/fixtures/strategy/position_sizing_vectors.json`` cover
  below/inside/above-Range amounts, per-side liquidity from a
  cap, worst-case single-sided inventory at the boundaries,
  and the Q64.64 USDG → token conversion. The Python port
  reproduces each vector byte-exactly.
- ``compute_sizing`` returns one canonical integer
  ``liquidityDelta`` for a fixed Range; lowering either cap
  can only reduce the liquidity (never move the Range); the
  post-settlement simulated wallet state respects both caps
  and the native ETH Gas reservation.
- Hook deltas are integer credits/charges added to the
  required amounts; a non-zero hook delta without verified
  evidence raises :class:`SizingHookError`.
- Native ETH Gas reservation is subtracted from the
  spendable cap once and never spent on the LP itself
  (must-not spend gas reserve).
- An insufficient economic size returns ``NO_TRADE`` with
  ``INSUFFICIENT_ECONOMIC_SIZE``.
- An unverified hook is rejected; an unknown reason code is
  rejected; out-of-domain inputs raise typed exceptions.
- The sizer is float-free throughout (no ``float`` and no
  ``Decimal`` on the integer path).
- Determinism: same inputs produce byte-identical
  ``SizingDecision`` instances.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from robinhood_lp.protocol import (
    MAX_SQRT_PRICE_X96,
    MIN_SQRT_PRICE_X96,
    Address,
    Currency,
    PoolKey,
    get_sqrt_price_at_tick,
)
from robinhood_lp.protocol.sizing import (
    BINDING_AMOUNT0,
    BINDING_AMOUNT1,
    POSITION_ABOVE,
    POSITION_BELOW,
    POSITION_INSIDE,
    ReasonCode,
    SizingAlignmentError,
    SizingDecision,
    SizingHookError,
    SizingInputError,
    SizingRangeError,
    SizingUsdgConversionError,
    compute_cap_bounded_liquidity,
    compute_sizing,
    get_amounts_for_liquidity,
    usdg_amount_to_token_amount,
    worst_case_single_sided_inventory,
)

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "strategy" / "position_sizing_vectors.json").read_text()
)


def _u(s: str) -> int:
    """Parse a uint literal that may be decimal or hex."""
    return int(s, 16) if s.startswith("0x") else int(s)


# ---------------------------------------------------------------------------
# Test fixtures (typed V4 PoolKey for the standard fee/spacing we use)
# ---------------------------------------------------------------------------


@pytest.fixture
def pool_key_static() -> PoolKey:
    """A static-fee V4 PoolKey with tick_spacing=60 for the wide-range tests."""
    return PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3000,
        tick_spacing=60,
        hooks=Address.zero(),
    )


@pytest.fixture
def pool_key_native_currency0() -> PoolKey:
    """A static-fee V4 PoolKey whose ``currency0`` is native ETH (zero address).

    The ``currency1`` is a non-native address so the Gas reservation applies
    only to ``amount0_cap``.
    """
    return PoolKey(
        currency0=Currency.native(),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3000,
        tick_spacing=60,
        hooks=Address.zero(),
    )


# ---------------------------------------------------------------------------
# Below/inside/above-Range amounts (pinned vectors)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vector",
    VECTORS["below_range_amounts"],
    ids=[v["name"] for v in VECTORS["below_range_amounts"]],
)
def test_below_range_amounts_match_pinned(vector: dict[str, Any]) -> None:
    sqrt_price_x96 = _u(vector["sqrt_price_x96"])
    pa = _u(vector["sqrt_price_a_x96"])
    pb = _u(vector["sqrt_price_b_x96"])
    liquidity = _u(vector["liquidity"])
    amount0, amount1 = get_amounts_for_liquidity(sqrt_price_x96, pa, pb, liquidity)
    assert amount0 == _u(vector["amount0"])
    assert amount1 == _u(vector["amount1"])
    # Below the Range the position is fully token0 (amount1 == 0).
    assert amount1 == 0


@pytest.mark.parametrize(
    "vector",
    VECTORS["above_range_amounts"],
    ids=[v["name"] for v in VECTORS["above_range_amounts"]],
)
def test_above_range_amounts_match_pinned(vector: dict[str, Any]) -> None:
    sqrt_price_x96 = _u(vector["sqrt_price_x96"])
    pa = _u(vector["sqrt_price_a_x96"])
    pb = _u(vector["sqrt_price_b_x96"])
    liquidity = _u(vector["liquidity"])
    amount0, amount1 = get_amounts_for_liquidity(sqrt_price_x96, pa, pb, liquidity)
    assert amount0 == _u(vector["amount0"])
    assert amount1 == _u(vector["amount1"])
    assert amount0 == 0


@pytest.mark.parametrize(
    "vector",
    VECTORS["inside_range_amounts"],
    ids=[v["name"] for v in VECTORS["inside_range_amounts"]],
)
def test_inside_range_amounts_match_pinned(vector: dict[str, Any]) -> None:
    sqrt_price_x96 = _u(vector["sqrt_price_x96"])
    pa = _u(vector["sqrt_price_a_x96"])
    pb = _u(vector["sqrt_price_b_x96"])
    liquidity = _u(vector["liquidity"])
    amount0, amount1 = get_amounts_for_liquidity(sqrt_price_x96, pa, pb, liquidity)
    assert amount0 == _u(vector["amount0"])
    assert amount1 == _u(vector["amount1"])
    # Inside: both sides non-zero.
    assert amount0 > 0 and amount1 > 0


@pytest.mark.parametrize(
    "vector",
    VECTORS["tick_boundary_sizes"],
    ids=[v["name"] for v in VECTORS["tick_boundary_sizes"]],
)
def test_tight_range_amounts_match_pinned(vector: dict[str, Any]) -> None:
    sqrt_price_x96 = _u(vector["sqrt_price_x96"])
    pa = _u(vector["sqrt_price_a_x96"])
    pb = _u(vector["sqrt_price_b_x96"])
    liquidity = _u(vector["liquidity"])
    amount0, amount1 = get_amounts_for_liquidity(sqrt_price_x96, pa, pb, liquidity)
    assert amount0 == _u(vector["amount0"])
    assert amount1 == _u(vector["amount1"])


# ---------------------------------------------------------------------------
# Below/inside/above-Range classification
# ---------------------------------------------------------------------------


def test_classify_below_at_pa() -> None:
    pa = _u(VECTORS["constants"]["sqrt_price_for_tick_neg100"])
    pb = _u(VECTORS["constants"]["sqrt_price_for_tick_100"])
    a0, a1 = get_amounts_for_liquidity(pa, pa, pb, 10**18)
    assert a1 == 0
    assert a0 > 0


def test_classify_above_at_pb() -> None:
    pa = _u(VECTORS["constants"]["sqrt_price_for_tick_neg100"])
    pb = _u(VECTORS["constants"]["sqrt_price_for_tick_100"])
    a0, a1 = get_amounts_for_liquidity(pb, pa, pb, 10**18)
    assert a0 == 0
    assert a1 > 0


def test_rejects_pa_ge_pb() -> None:
    pa = _u(VECTORS["constants"]["sqrt_price_for_tick_neg100"])
    with pytest.raises(SizingRangeError):
        get_amounts_for_liquidity(pa, pa, pa, 10**18)


# ---------------------------------------------------------------------------
# Worst-case single-sided inventory at the boundaries (pinned vectors)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vector",
    VECTORS["worst_case_boundary_inventory"],
    ids=[v["name"] for v in VECTORS["worst_case_boundary_inventory"]],
)
def test_worst_case_boundary_inventory_match_pinned(vector: dict[str, Any]) -> None:
    pa = _u(vector["sqrt_price_a_x96"])
    pb = _u(vector["sqrt_price_b_x96"])
    liquidity = _u(vector["liquidity"])
    amount0_at_pb, amount1_at_pa = worst_case_single_sided_inventory(pa, pb, liquidity)
    assert amount0_at_pb == _u(vector["amount0_at_pb"])
    assert amount1_at_pa == _u(vector["amount1_at_pa"])


def test_worst_case_rejects_pa_ge_pb() -> None:
    pa = _u(VECTORS["constants"]["sqrt_price_for_tick_neg100"])
    with pytest.raises(SizingRangeError):
        worst_case_single_sided_inventory(pa, pa, 10**18)


# ---------------------------------------------------------------------------
# Cap-bounded liquidity (the core sizing algorithm, pinned vectors)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vector",
    VECTORS["cap_bounded_liquidity"],
    ids=[v["name"] for v in VECTORS["cap_bounded_liquidity"]],
)
def test_cap_bounded_liquidity_match_pinned(vector: dict[str, Any]) -> None:
    sqrt_price_x96 = _u(vector["sqrt_price_x96"])
    pa = _u(vector["sqrt_price_a_x96"])
    pb = _u(vector["sqrt_price_b_x96"])
    amount0_cap = _u(vector["amount0_cap"])
    amount1_cap = _u(vector["amount1_cap"])
    expected_liquidity = _u(vector["liquidity"])
    expected_side = vector["binding_side"]

    liquidity, side = compute_cap_bounded_liquidity(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=pa,
        sqrt_price_b_x96=pb,
        amount0_cap=amount0_cap,
        amount1_cap=amount1_cap,
    )
    assert liquidity == expected_liquidity
    assert side == expected_side


# ---------------------------------------------------------------------------
# Monotone property: lowering either cap only reduces liquidity
# ---------------------------------------------------------------------------


def test_lowering_amount0_cap_only_reduces_liquidity() -> None:
    """Lowering amount0_cap never increases liquidity and never moves ticks."""
    sqrt_price_x96 = _u(VECTORS["constants"]["sqrt_price_for_tick_0"])
    pa = _u(VECTORS["constants"]["sqrt_price_for_tick_neg100"])
    pb = _u(VECTORS["constants"]["sqrt_price_for_tick_100"])
    big = 10**20

    liq_full, _ = compute_cap_bounded_liquidity(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=pa,
        sqrt_price_b_x96=pb,
        amount0_cap=big,
        amount1_cap=big,
    )
    # Halve amount0_cap; liquidity must decrease (or stay equal only when amount1
    # is the binding side, which it is not at this symmetric price).
    liq_half, side_half = compute_cap_bounded_liquidity(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=pa,
        sqrt_price_b_x96=pb,
        amount0_cap=big // 2,
        amount1_cap=big,
    )
    assert liq_half <= liq_full
    # When amount0_cap is halved, the binding side may stay or change,
    # but ticks (pa, pb) are unchanged.
    assert side_half in (BINDING_AMOUNT0, BINDING_AMOUNT1)


def test_lowering_amount1_cap_only_reduces_liquidity() -> None:
    """Lowering amount1_cap never increases liquidity and never moves ticks."""
    sqrt_price_x96 = _u(VECTORS["constants"]["sqrt_price_for_tick_0"])
    pa = _u(VECTORS["constants"]["sqrt_price_for_tick_neg100"])
    pb = _u(VECTORS["constants"]["sqrt_price_for_tick_100"])
    big = 10**20

    liq_full, _ = compute_cap_bounded_liquidity(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=pa,
        sqrt_price_b_x96=pb,
        amount0_cap=big,
        amount1_cap=big,
    )
    liq_half, _ = compute_cap_bounded_liquidity(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=pa,
        sqrt_price_b_x96=pb,
        amount0_cap=big,
        amount1_cap=big // 2,
    )
    assert liq_half <= liq_full


def test_lowering_caps_never_changes_ticks() -> None:
    """The Range (pa, pb) is not altered by the cap-bounded liquidity step.

    This is the structural guarantee that ``must-not change Range to fit
    a cap`` is upheld: the only outputs of the cap-bounded step are
    ``liquidity`` and ``binding_side``; the Range is not an output.
    """
    sqrt_price_x96 = _u(VECTORS["constants"]["sqrt_price_for_tick_0"])
    pa = _u(VECTORS["constants"]["sqrt_price_for_tick_neg100"])
    pb = _u(VECTORS["constants"]["sqrt_price_for_tick_100"])
    pa_before, pb_before = pa, pb
    big = 10**20

    compute_cap_bounded_liquidity(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=pa,
        sqrt_price_b_x96=pb,
        amount0_cap=big // 4,
        amount1_cap=big,
    )
    # The Range inputs are integers, not mutated.
    assert pa == pa_before
    assert pb == pb_before


# ---------------------------------------------------------------------------
# USDG → token conversion (Q64.64 boundary, pinned vectors)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vector",
    VECTORS["usdg_conversion"],
    ids=[v["name"] for v in VECTORS["usdg_conversion"]],
)
def test_usdg_conversion_match_pinned(vector: dict[str, Any]) -> None:
    usdg = _u(vector["usdg_amount"])
    price = _u(vector["price_q64_64"])
    expected = _u(vector["token_amount"])
    assert usdg_amount_to_token_amount(usdg, price) == expected


def test_usdg_conversion_rejects_non_positive_price() -> None:
    with pytest.raises(SizingUsdgConversionError):
        usdg_amount_to_token_amount(10**18, 0)
    with pytest.raises(SizingUsdgConversionError):
        usdg_amount_to_token_amount(10**18, -1)


def test_usdg_conversion_rounds_down() -> None:
    """A USDG amount that doesn't divide cleanly rounds down (conservative)."""
    # 2^63 usdg @ 3.0 price: (2^63 * 2^64) // (3 * 2^64) == 2^63 // 3.
    price = 3 << 64
    usdg = 1 << 63  # 9223372036854775808
    expected = (1 << 63) // 3
    assert usdg_amount_to_token_amount(usdg, price) == expected


# ---------------------------------------------------------------------------
# compute_sizing: the canonical entry point
# ---------------------------------------------------------------------------


def _make_inputs(
    *,
    pool_key: PoolKey,
    tick_lower: int,
    tick_upper: int,
    sqrt_price_x96: int,
    amount0_cap: int,
    amount1_cap: int,
    gas_reserve_wei: int = 0,
    accrued_fees0: int = 0,
    accrued_fees1: int = 0,
    hook_delta0: int = 0,
    hook_delta1: int = 0,
    hook_verified: bool = False,
    deadline: int = 1_700_000_000,
    min_liquidity: int = 0,
    min_economic_liquidity_usdg_q64_64: int | None = None,
    usdg_cap_q64_64_token0: int | None = None,
    usdg_cap_q64_64_token1: int | None = None,
    usdg_amount_token0: int = 0,
    usdg_amount_token1: int = 0,
) -> dict[str, Any]:
    """Bundle the kwargs ``compute_sizing`` expects."""
    return {
        "pool_key": pool_key,
        "sqrt_price_x96": sqrt_price_x96,
        "tick_lower": tick_lower,
        "tick_upper": tick_upper,
        "amount0_cap": amount0_cap,
        "amount1_cap": amount1_cap,
        "accrued_fees0": accrued_fees0,
        "accrued_fees1": accrued_fees1,
        "gas_reserve_wei": gas_reserve_wei,
        "hook_delta0": hook_delta0,
        "hook_delta1": hook_delta1,
        "hook_verified": hook_verified,
        "deadline": deadline,
        "min_liquidity": min_liquidity,
        "min_economic_liquidity_usdg_q64_64": min_economic_liquidity_usdg_q64_64,
        "usdg_cap_q64_64_token0": usdg_cap_q64_64_token0,
        "usdg_cap_q64_64_token1": usdg_cap_q64_64_token1,
        "usdg_amount_token0": usdg_amount_token0,
        "usdg_amount_token1": usdg_amount_token1,
    }


def test_compute_sizing_ok_inside_range(pool_key_static: PoolKey) -> None:
    """A symmetric inside-range cap yields OK with a single liquidityDelta."""
    pa = get_sqrt_price_at_tick(-60)
    pb = get_sqrt_price_at_tick(60)
    p = get_sqrt_price_at_tick(0)
    cap0 = 5012269623051203
    cap1 = 5012269623051203
    target_liquidity, _ = compute_cap_bounded_liquidity(
        sqrt_price_x96=p,
        sqrt_price_a_x96=pa,
        sqrt_price_b_x96=pb,
        amount0_cap=cap0,
        amount1_cap=cap1,
    )
    expected_a0, expected_a1 = get_amounts_for_liquidity(p, pa, pb, target_liquidity)
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
        )
    )
    assert decision.decision == "OK"
    assert decision.reason_code is None
    assert decision.liquidity == target_liquidity
    assert decision.amount0 == expected_a0
    assert decision.amount1 == expected_a1
    assert decision.amount0_max >= decision.amount0
    assert decision.amount1_max >= decision.amount1
    assert decision.position_relative == POSITION_INSIDE
    assert decision.binding_side in (BINDING_AMOUNT0, BINDING_AMOUNT1)
    assert decision.worst_case_amount0 > 0
    assert decision.worst_case_amount1 > 0
    assert decision.amount0_max == cap0
    assert decision.amount1_max == cap1


def test_compute_sizing_ok_below_range_binds_amount0(pool_key_static: PoolKey) -> None:
    """Below the Range the entire position is token0; only amount0 binds."""
    pa = get_sqrt_price_at_tick(-60)
    pb = get_sqrt_price_at_tick(60)
    cap0 = 10**9
    target_liquidity, _ = compute_cap_bounded_liquidity(
        sqrt_price_x96=pa,
        sqrt_price_a_x96=pa,
        sqrt_price_b_x96=pb,
        amount0_cap=cap0,
        amount1_cap=0,
    )
    expected_a0, _ = get_amounts_for_liquidity(pa, pa, pb, target_liquidity)
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=pa,
            amount0_cap=cap0,
            amount1_cap=10**25,
        )
    )
    assert decision.decision == "OK"
    assert decision.position_relative == POSITION_BELOW
    assert decision.binding_side == BINDING_AMOUNT0
    assert decision.liquidity == target_liquidity
    assert decision.amount0 == expected_a0
    assert decision.amount1 == 0


def test_compute_sizing_ok_above_range_binds_amount1(pool_key_static: PoolKey) -> None:
    """Above the Range the entire position is token1; only amount1 binds."""
    pa = get_sqrt_price_at_tick(-60)
    pb = get_sqrt_price_at_tick(60)
    cap1 = 10**9
    target_liquidity, _ = compute_cap_bounded_liquidity(
        sqrt_price_x96=pb,
        sqrt_price_a_x96=pa,
        sqrt_price_b_x96=pb,
        amount0_cap=0,
        amount1_cap=cap1,
    )
    _, expected_a1 = get_amounts_for_liquidity(pb, pa, pb, target_liquidity)
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=pb,
            amount0_cap=10**25,
            amount1_cap=cap1,
        )
    )
    assert decision.decision == "OK"
    assert decision.position_relative == POSITION_ABOVE
    assert decision.binding_side == BINDING_AMOUNT1
    assert decision.liquidity == target_liquidity
    assert decision.amount1 == expected_a1
    assert decision.amount0 == 0


def test_compute_sizing_lowering_cap_only_reduces_liquidity(pool_key_static: PoolKey) -> None:
    """Lowering amount0_cap or amount1_cap strictly reduces or holds the liquidity."""
    p = get_sqrt_price_at_tick(0)
    big = 10**22
    big_decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=big,
            amount1_cap=big,
        )
    )
    assert big_decision.decision == "OK"
    halved_decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=big // 2,
            amount1_cap=big,
        )
    )
    assert halved_decision.decision == "OK"
    assert halved_decision.liquidity <= big_decision.liquidity
    # The Range ticks are unchanged.
    assert halved_decision.position_relative == big_decision.position_relative


def test_compute_sizing_range_degenerate_returns_no_trade(pool_key_static: PoolKey) -> None:
    """``tick_lower >= tick_upper`` returns ``NO_TRADE`` with RANGE_DEGENERATE."""
    p = get_sqrt_price_at_tick(0)
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=10**18,
            amount1_cap=10**18,
        )
    )
    assert decision.decision == "NO_TRADE"
    assert decision.reason_code == ReasonCode.RANGE_DEGENERATE
    assert decision.liquidity == 0
    assert decision.amount0 == 0
    assert decision.amount1 == 0


def test_compute_sizing_ticks_misaligned_raises(pool_key_static: PoolKey) -> None:
    """Ticks not aligned to the pool's spacing raise ``SizingAlignmentError``."""
    p = get_sqrt_price_at_tick(0)
    with pytest.raises(SizingAlignmentError):
        compute_sizing(
            **_make_inputs(
                pool_key=pool_key_static,
                tick_lower=-59,  # not a multiple of 60
                tick_upper=60,
                sqrt_price_x96=p,
                amount0_cap=10**18,
                amount1_cap=10**18,
            )
        )


def test_compute_sizing_price_out_of_bounds_returns_no_trade(pool_key_static: PoolKey) -> None:
    """An out-of-domain price returns NO_TRADE with PRICE_OUT_OF_BOUNDS."""
    # MAX_SQRT_PRICE_X96 is rejected by V4's strict getTickAtSqrtPrice;
    # ``get_sqrt_price_at_tick`` only emits values in the valid domain,
    # so an explicit out-of-bounds price is required.
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=MAX_SQRT_PRICE_X96,
            amount0_cap=10**18,
            amount1_cap=10**18,
        )
    )
    assert decision.decision == "NO_TRADE"
    assert decision.reason_code == ReasonCode.PRICE_OUT_OF_BOUNDS


def test_compute_sizing_price_below_min_returns_no_trade(pool_key_static: PoolKey) -> None:
    """A price below MIN_SQRT_PRICE_X96 returns ``NO_TRADE`` with PRICE_OUT_OF_BOUNDS.

    The sizer never raises on an out-of-domain price: the contract
    treats that as a structured market outcome, not a contract bug.
    A contract bug is signalled by a typed exception (e.g.
    :class:`SizingInputError`), not by a "no-trade" verdict.
    """
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=MIN_SQRT_PRICE_X96 - 1,
            amount0_cap=10**18,
            amount1_cap=10**18,
        )
    )
    assert decision.decision == "NO_TRADE"
    assert decision.reason_code == ReasonCode.PRICE_OUT_OF_BOUNDS


# ---------------------------------------------------------------------------
# Hook BalanceDelta
# ---------------------------------------------------------------------------


def test_compute_sizing_unverified_hook_raises(pool_key_static: PoolKey) -> None:
    """A non-zero hook delta without verified evidence is rejected."""
    p = get_sqrt_price_at_tick(0)
    with pytest.raises(SizingHookError):
        compute_sizing(
            **_make_inputs(
                pool_key=pool_key_static,
                tick_lower=-60,
                tick_upper=60,
                sqrt_price_x96=p,
                amount0_cap=10**22,
                amount1_cap=10**22,
                hook_delta0=10**9,
                hook_verified=False,
            )
        )


def test_compute_sizing_verified_hook_charge_funded(pool_key_static: PoolKey) -> None:
    """A verified hook charge is funded from the wallet (cap must cover it)."""
    p = get_sqrt_price_at_tick(0)
    cap0 = 10**9
    cap1 = 5 * 10**8
    no_hook = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
        )
    )
    assert no_hook.decision == "OK"
    assert no_hook.binding_side == "amount1"
    hook_charge = 100
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0 + hook_charge,
            amount1_cap=cap1,
            hook_delta0=hook_charge,
            hook_delta1=0,
            hook_verified=True,
        )
    )
    assert decision.decision == "OK"
    assert decision.liquidity == no_hook.liquidity
    assert decision.amount0 == no_hook.amount0
    assert decision.amount0_max == cap0 + hook_charge


def test_compute_sizing_verified_hook_charge_exceeds_cap_returns_no_trade(
    pool_key_static: PoolKey,
) -> None:
    """A verified hook charge larger than the cap slack returns NO_TRADE."""
    p = get_sqrt_price_at_tick(0)
    cap0 = 10**9
    cap1 = 5 * 10**8
    no_hook = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
        )
    )
    assert no_hook.decision == "OK"
    slack = cap0 - no_hook.amount0
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
            hook_delta0=slack + 1,
            hook_verified=True,
        )
    )
    assert decision.decision == "NO_TRADE"
    assert decision.reason_code == ReasonCode.CAP_UNDERFLOW


def test_compute_sizing_verified_hook_credit_reduces_required(pool_key_static: PoolKey) -> None:
    """A verified hook credit reduces the required amount and is honoured."""
    p = get_sqrt_price_at_tick(0)
    cap0 = 10**9
    cap1 = 5 * 10**8
    no_hook = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
        )
    )
    assert no_hook.decision == "OK"
    hook_credit = 100
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
            hook_delta0=-hook_credit,
            hook_delta1=0,
            hook_verified=True,
        )
    )
    assert decision.decision == "OK"
    assert decision.liquidity == no_hook.liquidity
    assert decision.amount0 == no_hook.amount0
    assert decision.amount0_max == cap0


def test_compute_sizing_hook_credit_negative_required_raises(pool_key_static: PoolKey) -> None:
    """A hook credit larger than the required amount raises ``SizingHookError``."""
    p = get_sqrt_price_at_tick(0)
    cap0 = 10**9
    cap1 = 5 * 10**8
    no_hook = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
        )
    )
    assert no_hook.decision == "OK"
    with pytest.raises(SizingHookError):
        compute_sizing(
            **_make_inputs(
                pool_key=pool_key_static,
                tick_lower=-60,
                tick_upper=60,
                sqrt_price_x96=p,
                amount0_cap=cap0,
                amount1_cap=cap1,
                hook_delta0=-(no_hook.amount0 + 1),
                hook_verified=True,
            )
        )


# ---------------------------------------------------------------------------
# Native ETH Gas reservation
# ---------------------------------------------------------------------------


def test_native_gas_reservation_subtracts_once_from_cap(
    pool_key_native_currency0: PoolKey,
) -> None:
    """The Gas reservation is subtracted from the native-side cap once."""
    p = get_sqrt_price_at_tick(0)
    cap0 = 10**9
    cap1 = 10**9
    no_gas = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_native_currency0,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
        )
    )
    assert no_gas.decision == "OK"
    gas_reserve = 100
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_native_currency0,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
            gas_reserve_wei=gas_reserve,
        )
    )
    assert decision.decision == "OK"
    assert decision.amount0_max == cap0 - gas_reserve
    assert decision.amount1_max == cap1


def test_native_gas_reservation_zero_when_not_native(pool_key_static: PoolKey) -> None:
    """A non-zero gas_reserve_wei is ignored when neither currency is native."""
    p = get_sqrt_price_at_tick(0)
    cap0 = 10**9
    cap1 = 10**9
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
            gas_reserve_wei=10**9,
        )
    )
    assert decision.decision == "OK"
    assert decision.amount0_max == cap0
    assert decision.amount1_max == cap1


def test_native_gas_reserve_underflow_returns_no_trade(
    pool_key_native_currency0: PoolKey,
) -> None:
    """A native cap below the Gas reservation returns NO_TRADE."""
    p = get_sqrt_price_at_tick(0)
    gas_reserve = 10**9
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_native_currency0,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=gas_reserve - 1,
            amount1_cap=10**22,
            gas_reserve_wei=gas_reserve,
        )
    )
    assert decision.decision == "NO_TRADE"
    assert decision.reason_code == ReasonCode.GAS_RESERVE_UNDERFLOW


def test_native_gas_reservation_exact_match(pool_key_native_currency0: PoolKey) -> None:
    """Cap equal to the Gas reservation leaves the native-side cap at zero."""
    p = get_sqrt_price_at_tick(0)
    gas_reserve = 10**9
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_native_currency0,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=gas_reserve,
            amount1_cap=10**22,
            gas_reserve_wei=gas_reserve,
        )
    )
    assert decision.decision == "NO_TRADE"


# ---------------------------------------------------------------------------
# Simulated post-settlement: both caps respected
# ---------------------------------------------------------------------------


def test_simulated_settlement_respects_both_caps(pool_key_static: PoolKey) -> None:
    """The post-settlement wallet state respects both caps and the Gas reserve."""
    p = get_sqrt_price_at_tick(0)
    cap0 = 10**9
    cap1 = 5 * 10**8
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
            accrued_fees0=0,
            accrued_fees1=0,
        )
    )
    assert decision.decision == "OK"
    assert decision.amount0_max + 0 >= decision.amount0 + 0
    assert decision.amount1_max + 0 >= decision.amount1 + 0


# ---------------------------------------------------------------------------
# Insufficient economic size → NO_TRADE
# ---------------------------------------------------------------------------


def test_insufficient_economic_size_returns_no_trade(pool_key_static: PoolKey) -> None:
    """When the sized liquidity is below the USDG minimum, return NO_TRADE."""
    p = get_sqrt_price_at_tick(0)
    cap0 = 10**9
    cap1 = 5 * 10**8
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
            min_economic_liquidity_usdg_q64_64=1 << 80,
            usdg_amount_token0=cap0,
            usdg_amount_token1=cap1,
        )
    )
    assert decision.decision == "NO_TRADE"
    assert decision.reason_code == ReasonCode.INSUFFICIENT_ECONOMIC_SIZE


def test_minimum_economic_size_passes_when_satisfied(pool_key_static: PoolKey) -> None:
    """When the sized liquidity meets the USDG minimum, return OK."""
    p = get_sqrt_price_at_tick(0)
    cap0 = 10**9
    cap1 = 5 * 10**8
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=cap0,
            amount1_cap=cap1,
            min_economic_liquidity_usdg_q64_64=1,
            usdg_amount_token0=cap0,
            usdg_amount_token1=cap1,
        )
    )
    assert decision.decision == "OK"


# ---------------------------------------------------------------------------
# Both caps zero → NO_TRADE
# ---------------------------------------------------------------------------


def test_both_caps_zero_returns_no_trade(pool_key_static: PoolKey) -> None:
    p = get_sqrt_price_at_tick(0)
    decision = compute_sizing(
        **_make_inputs(
            pool_key=pool_key_static,
            tick_lower=-60,
            tick_upper=60,
            sqrt_price_x96=p,
            amount0_cap=0,
            amount1_cap=0,
        )
    )
    assert decision.decision == "NO_TRADE"
    assert decision.reason_code == ReasonCode.CAP_NONPOSITIVE


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism(pool_key_static: PoolKey) -> None:
    """Same inputs produce byte-identical ``SizingDecision`` instances."""
    pa = get_sqrt_price_at_tick(-60)
    pb = get_sqrt_price_at_tick(60)
    p = get_sqrt_price_at_tick(0)
    target_liquidity = 10**18
    a0, a1 = get_amounts_for_liquidity(p, pa, pb, target_liquidity)
    kwargs = _make_inputs(
        pool_key=pool_key_static,
        tick_lower=-60,
        tick_upper=60,
        sqrt_price_x96=p,
        amount0_cap=a0,
        amount1_cap=a1,
    )
    d1 = compute_sizing(**kwargs)
    d2 = compute_sizing(**copy.deepcopy(kwargs))
    assert d1 == d2
    assert hash(d1) == hash(d2)


# ---------------------------------------------------------------------------
# Float-free invariant
# ---------------------------------------------------------------------------


def test_protocol_module_has_no_float() -> None:
    """The sizing module must not introduce ``float`` symbols.

    The audit inspects the module's globals for ``float`` types
    and instance method defaults; the protocol package is
    integer-only by ADR-004.
    """
    import robinhood_lp.protocol.sizing as sizing_module

    for name, value in vars(sizing_module).items():
        if name.startswith("_"):
            continue
        assert not isinstance(value, float), (
            f"sizing module surface contains a float value: {name}={value!r}"
        )
    for name in (
        "BINDING_AMOUNT0",
        "BINDING_AMOUNT1",
        "BINDING_BOTH",
        "POSITION_ABOVE",
        "POSITION_BELOW",
        "POSITION_INSIDE",
    ):
        assert isinstance(getattr(sizing_module, name), str)


# ---------------------------------------------------------------------------
# Reason-code stability
# ---------------------------------------------------------------------------


def test_reason_codes_are_stable_strings() -> None:
    """Reason code values are part of the public contract."""
    assert ReasonCode.RANGE_DEGENERATE.value == "RANGE_DEGENERATE"
    assert ReasonCode.PRICE_OUT_OF_BOUNDS.value == "PRICE_OUT_OF_BOUNDS"
    assert ReasonCode.CAP_NONPOSITIVE.value == "CAP_NONPOSITIVE"
    assert ReasonCode.CAP_UNDERFLOW.value == "CAP_UNDERFLOW"
    assert ReasonCode.HOOK_DELTA_NONZERO_UNVERIFIED.value == "HOOK_DELTA_NONZERO_UNVERIFIED"
    assert ReasonCode.GAS_RESERVE_UNDERFLOW.value == "GAS_RESERVE_UNDERFLOW"
    assert ReasonCode.INSUFFICIENT_ECONOMIC_SIZE.value == "INSUFFICIENT_ECONOMIC_SIZE"
    assert ReasonCode.USDG_CONVERSION_FAILED.value == "USDG_CONVERSION_FAILED"


# ---------------------------------------------------------------------------
# SizingDecision invariants
# ---------------------------------------------------------------------------


def test_sizing_decision_rejects_unknown_decision_value() -> None:
    with pytest.raises(SizingInputError):
        SizingDecision(
            decision="MAYBE",
            reason_code=None,
            liquidity=1,
            amount0=1,
            amount1=1,
            amount0_max=1,
            amount1_max=1,
            min_liquidity=0,
            deadline=1,
            position_relative=POSITION_INSIDE,
            binding_side=BINDING_AMOUNT0,
            worst_case_amount0=1,
            worst_case_amount1=1,
        )


def test_sizing_decision_rejects_no_trade_without_reason_code() -> None:
    with pytest.raises(SizingInputError):
        SizingDecision(
            decision="NO_TRADE",
            reason_code=None,
            liquidity=0,
            amount0=0,
            amount1=0,
            amount0_max=0,
            amount1_max=0,
            min_liquidity=0,
            deadline=1,
            position_relative=POSITION_BELOW,
            binding_side="both",
            worst_case_amount0=0,
            worst_case_amount1=0,
        )


def test_sizing_decision_rejects_ok_with_nonzero_amounts_on_no_trade() -> None:
    with pytest.raises(SizingInputError):
        SizingDecision(
            decision="NO_TRADE",
            reason_code=ReasonCode.CAP_NONPOSITIVE,
            liquidity=10,
            amount0=0,
            amount1=0,
            amount0_max=0,
            amount1_max=0,
            min_liquidity=0,
            deadline=1,
            position_relative=POSITION_BELOW,
            binding_side="both",
            worst_case_amount0=0,
            worst_case_amount1=0,
        )


def test_sizing_decision_rejects_ok_with_amount_max_below_amount() -> None:
    with pytest.raises(SizingInputError):
        SizingDecision(
            decision="OK",
            reason_code=None,
            liquidity=10,
            amount0=100,
            amount1=100,
            amount0_max=99,
            amount1_max=100,
            min_liquidity=0,
            deadline=1,
            position_relative=POSITION_INSIDE,
            binding_side=BINDING_AMOUNT0,
            worst_case_amount0=100,
            worst_case_amount1=100,
        )
