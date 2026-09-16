// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.26;

import {PoolManager} from "@v4-core/src/PoolManager.sol";
import {IPoolManager} from "@v4-core/src/interfaces/IPoolManager.sol";
import {StateView} from "@v4-periphery/src/lens/StateView.sol";
import {IStateView} from "@v4-periphery/src/interfaces/IStateView.sol";

/// @notice T021 selector oracle: emits the canonical event topics and
///         function selectors consumed by T022 (Initialize / Swap /
///         ModifyLiquidity / Donate on PoolManager) and the StateView
///         read selectors T024 / T042 / T051 actually call.
///
///         The Python port under src/robinhood_lp/protocol/abi_artifacts.py
///         byte-compares every emitted value against the JSON artifact at
///         docs/implement/protocol-artifacts/v4-core-<commit>.json. Any
///         drift in this contract (different compiler version, different
///         pinned commit, different upstream ABI) MUST also be reflected
///         in that artifact, or the test suite fails closed.
contract SelectorOracle {
    // -------------------------------------------------------------------
    // PoolManager events (T022)
    // -------------------------------------------------------------------

    function emitInitializeTopic() external pure returns (bytes32) {
        // event Initialize(PoolId indexed id, Currency indexed currency0,
        //                  Currency indexed currency1, uint24 fee,
        //                  int24 tickSpacing, IHooks hooks,
        //                  uint160 sqrtPriceX96, int24 tick);
        return keccak256(
            "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
        );
    }

    function emitModifyLiquidityTopic() external pure returns (bytes32) {
        // event ModifyLiquidity(PoolId indexed id, address indexed sender,
        //                       int24 tickLower, int24 tickUpper,
        //                       int256 liquidityDelta, bytes32 salt);
        return keccak256(
            "ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)"
        );
    }

    function emitSwapTopic() external pure returns (bytes32) {
        // event Swap(PoolId indexed id, address indexed sender,
        //            int128 amount0, int128 amount1, uint160 sqrtPriceX96,
        //            uint128 liquidity, int24 tick, uint24 fee);
        return keccak256(
            "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
        );
    }

    function emitDonateTopic() external pure returns (bytes32) {
        // event Donate(PoolId indexed id, address indexed sender,
        //              uint256 amount0, uint256 amount1);
        return keccak256(
            "Donate(bytes32,address,uint256,uint256)"
        );
    }

    function emitProtocolFeeUpdatedTopic() external pure returns (bytes32) {
        // event ProtocolFeeUpdated(PoolId indexed id, uint24 protocolFee)
        // — declared in v4-core IProtocolFees.sol and inherited into the
        // PoolManager event surface. Required by T030 (ADR-010 §"Required
        // data boundary") because the fee recorded in a Swap is the
        // combined swap fee, not automatically the LP-owned share.
        return keccak256(
            "ProtocolFeeUpdated(bytes32,uint24)"
        );
    }

    // -------------------------------------------------------------------
    // PoolManager function selectors (T022)
    // -------------------------------------------------------------------

    function emitInitializeSelector() external pure returns (bytes4) {
        // function initialize(PoolKey memory key, uint160 sqrtPriceX96)
        //     external returns (int24 tick);
        return IPoolManager.initialize.selector;
    }

    function emitModifyLiquiditySelector() external pure returns (bytes4) {
        // function modifyLiquidity(PoolKey memory key,
        //                          ModifyLiquidityParams memory params,
        //                          bytes calldata hookData) external returns (...);
        return IPoolManager.modifyLiquidity.selector;
    }

    function emitSwapSelector() external pure returns (bytes4) {
        // function swap(PoolKey memory key, SwapParams memory params,
        //               bytes calldata hookData) external returns (...);
        return IPoolManager.swap.selector;
    }

    function emitDonateSelector() external pure returns (bytes4) {
        // function donate(PoolKey memory key, uint256 amount0, uint256 amount1,
        //                 bytes calldata hookData) external returns (...);
        return IPoolManager.donate.selector;
    }

    // -------------------------------------------------------------------
    // StateView function selectors (T024 / T042 / T051)
    //
    // We expose every read-only entrypoint listed in IStateView.sol at
    // the pinned v4-periphery commit (dce236d4e2057422d0791d9a973a58765eb46f65).
    // Adding a new StateView call elsewhere in the codebase requires
    // a new selector here (and a regenerated artifact).
    // -------------------------------------------------------------------

    function emitGetSlot0Selector() external pure returns (bytes4) {
        return IStateView.getSlot0.selector;
    }

    function emitGetTickInfoSelector() external pure returns (bytes4) {
        return IStateView.getTickInfo.selector;
    }

    function emitGetTickLiquiditySelector() external pure returns (bytes4) {
        return IStateView.getTickLiquidity.selector;
    }

    function emitGetTickFeeGrowthOutsideSelector() external pure returns (bytes4) {
        return IStateView.getTickFeeGrowthOutside.selector;
    }

    function emitGetFeeGrowthGlobalsSelector() external pure returns (bytes4) {
        return IStateView.getFeeGrowthGlobals.selector;
    }

    function emitGetLiquiditySelector() external pure returns (bytes4) {
        return IStateView.getLiquidity.selector;
    }

    function emitGetTickBitmapSelector() external pure returns (bytes4) {
        return IStateView.getTickBitmap.selector;
    }

    function emitGetPositionInfoOwnerSelector() external pure returns (bytes4) {
        // getPositionInfo(PoolId,address,int24,int24,bytes32) — owner-keyed overload.
        return bytes4(keccak256("getPositionInfo(bytes32,address,int24,int24,bytes32)"));
    }

    function emitGetPositionInfoIdSelector() external pure returns (bytes4) {
        // getPositionInfo(PoolId,bytes32) — positionId overload.
        return bytes4(keccak256("getPositionInfo(bytes32,bytes32)"));
    }

    function emitGetPositionLiquiditySelector() external pure returns (bytes4) {
        return IStateView.getPositionLiquidity.selector;
    }

    function emitGetFeeGrowthInsideSelector() external pure returns (bytes4) {
        return IStateView.getFeeGrowthInside.selector;
    }
}
