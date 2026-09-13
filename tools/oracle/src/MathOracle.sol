// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.26;

import {TickMath} from "@v4-core/src/libraries/TickMath.sol";
import {SqrtPriceMath} from "@v4-core/src/libraries/SqrtPriceMath.sol";
import {LiquidityAmounts} from "@v4-periphery/src/libraries/LiquidityAmounts.sol";

/// @notice Generates pinned V4 math vectors for the Python port (T012).
///         The Python implementation under src/robinhood_lp/protocol/
///         must reproduce every vector byte-exactly.
contract MathOracle {
    // -------------------------------------------------------------------
    // TickMath: getSqrtPriceAtTick / getTickAtSqrtPrice
    // -------------------------------------------------------------------

    function sqrtPriceAtTick(int24 tick) external pure returns (uint160) {
        return TickMath.getSqrtPriceAtTick(tick);
    }

    function tickAtSqrtPrice(uint160 sqrtPriceX96) external pure returns (int24) {
        return TickMath.getTickAtSqrtPrice(sqrtPriceX96);
    }

    // -------------------------------------------------------------------
    // SqrtPriceMath: amount0 / amount1 from liquidity (rounding down)
    // -------------------------------------------------------------------

    function amount0Delta(
        uint160 sqrtPriceAX96,
        uint160 sqrtPriceBX96,
        uint128 liquidity
    ) external pure returns (uint256) {
        return SqrtPriceMath.getAmount0Delta(sqrtPriceAX96, sqrtPriceBX96, liquidity, false);
    }

    function amount1Delta(
        uint160 sqrtPriceAX96,
        uint160 sqrtPriceBX96,
        uint128 liquidity
    ) external pure returns (uint256) {
        return SqrtPriceMath.getAmount1Delta(sqrtPriceAX96, sqrtPriceBX96, liquidity, false);
    }

    // -------------------------------------------------------------------
    // LiquidityAmounts: liquidity from an (amount0, amount1) pair.
    // -------------------------------------------------------------------

    function liquidityForAmounts(
        uint160 sqrtPriceX96,
        uint160 sqrtPriceAX96,
        uint160 sqrtPriceBX96,
        uint256 amount0,
        uint256 amount1
    ) external pure returns (uint128 liquidity) {
        return LiquidityAmounts.getLiquidityForAmounts(
            sqrtPriceX96,
            sqrtPriceAX96,
            sqrtPriceBX96,
            amount0,
            amount1
        );
    }
}
