// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.26;

import "forge-std/Test.sol";
import "../src/MathOracle.sol";

/// @notice Generates pinned V4 math vectors for the Python port (T012).
contract MathOracleTest is Test {
    MathOracle internal oracle;

    function setUp() public {
        oracle = new MathOracle();
    }

    // -------------------------------------------------------------------
    // TickMath: getSqrtPriceAtTick and getTickAtSqrtPrice
    // -------------------------------------------------------------------

    function test_TickMath_boundary_vectors() public {
        _emitSqrtPriceAtTick("min_tick", -887272);
        _emitSqrtPriceAtTick("max_tick", 887272);
        _emitSqrtPriceAtTick("zero_tick", 0);
        _emitSqrtPriceAtTick("positive_100", 100);
        _emitSqrtPriceAtTick("negative_100", -100);
        _emitSqrtPriceAtTick("large_positive", 100000);
        _emitSqrtPriceAtTick("large_negative", -100000);
        _emitSqrtPriceAtTick("near_max_positive", 887271);
        _emitSqrtPriceAtTick("near_min_negative", -887271);

        // TickAtSqrtPrice: round-trip the canonical constants.
        // Note: V4's getTickAtSqrtPrice is strict inequality; the exact
        // MAX_SQRT_PRICE constant is *not* a valid input. Use
        // MAX_SQRT_PRICE - 1 to exercise the upper boundary.
        _emitTickAtSqrtPrice("min_sqrt_price", 4295128739);
        _emitTickAtSqrtPrice("max_sqrt_price_minus_one",
            1461446703485210103287273052203988822378723970342 - 1);
        _emitTickAtSqrtPrice("sqrt_price_for_1_1", 79228162514264337593543950336); // 2^96
    }

    function _emitSqrtPriceAtTick(string memory name, int24 tick) internal {
        uint160 sp = oracle.sqrtPriceAtTick(tick);
        emit log_named_string("name", string.concat("tick2sqrt/", name));
        emit log_named_int("tick", tick);
        emit log_named_uint("sqrt_price_x96", sp);
    }

    function _emitTickAtSqrtPrice(string memory name, uint160 sqrtPriceX96) internal {
        int24 tick = oracle.tickAtSqrtPrice(sqrtPriceX96);
        emit log_named_string("name", string.concat("sqrt2tick/", name));
        emit log_named_uint("sqrt_price_x96", sqrtPriceX96);
        emit log_named_int("tick", tick);
    }

    // -------------------------------------------------------------------
    // SqrtPriceMath: amount0 / amount1 from liquidity (roundUp selectable)
    // -------------------------------------------------------------------

    function test_SqrtPriceMath_amount_deltas() public {
        // Range centered around price 1:1 (sqrtPriceX96 = 2^96), spanning
        // ticks -100 .. +100.
        uint160 pa = TickMath.getSqrtPriceAtTick(-100);
        uint160 pb = TickMath.getSqrtPriceAtTick(100);

        _emitAmount0Delta("amount0_liq_1e18_range_pm100", pa, pb, 1e18, false);
        _emitAmount0Delta("amount0_liq_1e18_range_pm100_round_up", pa, pb, 1e18, true);
        _emitAmount0Delta("amount0_liq_1e6_range_pm100",  pa, pb, 1e6, false);
        _emitAmount0Delta("amount0_liq_1e6_range_pm100_round_up",  pa, pb, 1e6, true);
        _emitAmount1Delta("amount1_liq_1e18_range_pm100", pa, pb, 1e18, false);
        _emitAmount1Delta("amount1_liq_1e18_range_pm100_round_up", pa, pb, 1e18, true);
        _emitAmount1Delta("amount1_liq_1e6_range_pm100",  pa, pb, 1e6, false);
        _emitAmount1Delta("amount1_liq_1e6_range_pm100_round_up",  pa, pb, 1e6, true);

        // One-sided: amount0 only when current price equals lower bound.
        uint160 p = TickMath.getSqrtPriceAtTick(0);
        _emitAmount0Delta("amount0_liq_1e18_range_0_to_100", p, pb, 1e18, false);
        _emitAmount0Delta("amount0_liq_1e18_range_0_to_100_round_up", p, pb, 1e18, true);
        _emitAmount1Delta("amount1_liq_1e18_range_neg100_to_0", pa, p, 1e18, false);
        _emitAmount1Delta("amount1_liq_1e18_range_neg100_to_0_round_up", pa, p, 1e18, true);
    }

    function _emitAmount0Delta(
        string memory name,
        uint160 pa,
        uint160 pb,
        uint128 liquidity,
        bool roundUp
    ) internal {
        uint256 a0 = oracle.amount0Delta(pa, pb, liquidity, roundUp);
        emit log_named_string("name", string.concat("a0/", name));
        emit log_named_uint("pa", pa);
        emit log_named_uint("pb", pb);
        emit log_named_uint("liquidity", uint256(liquidity));
        emit log_named_uint("round_up", roundUp ? 1 : 0);
        emit log_named_uint("amount0", a0);
    }

    function _emitAmount1Delta(
        string memory name,
        uint160 pa,
        uint160 pb,
        uint128 liquidity,
        bool roundUp
    ) internal {
        uint256 a1 = oracle.amount1Delta(pa, pb, liquidity, roundUp);
        emit log_named_string("name", string.concat("a1/", name));
        emit log_named_uint("pa", pa);
        emit log_named_uint("pb", pb);
        emit log_named_uint("liquidity", uint256(liquidity));
        emit log_named_uint("round_up", roundUp ? 1 : 0);
        emit log_named_uint("amount1", a1);
    }

    // -------------------------------------------------------------------
    // LiquidityAmounts: liquidity from an (amount0, amount1) pair
    // -------------------------------------------------------------------

    function test_LiquidityAmounts_round_trip() public {
        uint160 p = 79228162514264337593543950336; // 2^96 = price 1:1
        uint160 pa = TickMath.getSqrtPriceAtTick(-100);
        uint160 pb = TickMath.getSqrtPriceAtTick(100);

        _emitLiquidityForAmounts("liq_symmetric_5e17_5e17", p, pa, pb, 5e17, 5e17);

        // Round-trip: amounts_for_liquidity -> liquidity_for_amounts.
        // These vectors verify the Solidity implementation is its own
        // inverse within integer rounding error.
        uint256 a0_1e18 = oracle.amount0Delta(pa, pb, 1e18, false);
        uint256 a1_1e18 = oracle.amount1Delta(pa, pb, 1e18, false);
        _emitLiquidityForAmounts("round_trip_from_1e18", p, pa, pb, a0_1e18, a1_1e18);

        uint256 a0_1e6 = oracle.amount0Delta(pa, pb, 1e6, false);
        uint256 a1_1e6 = oracle.amount1Delta(pa, pb, 1e6, false);
        _emitLiquidityForAmounts("round_trip_from_1e6", p, pa, pb, a0_1e6, a1_1e6);
    }

    function _emitLiquidityForAmounts(
        string memory name,
        uint160 p, uint160 pa, uint160 pb,
        uint256 amount0, uint256 amount1
    ) internal {
        uint128 liq = oracle.liquidityForAmounts(p, pa, pb, amount0, amount1);
        emit log_named_string("name", string.concat("a2l/", name));
        emit log_named_uint("p", p);
        emit log_named_uint("pa", pa);
        emit log_named_uint("pb", pb);
        emit log_named_uint("amount0", amount0);
        emit log_named_uint("amount1", amount1);
        emit log_named_uint("liquidity", uint256(liq));
    }
}
