// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.26;

import "forge-std/Test.sol";
import "../src/SelectorOracle.sol";

/// @notice T021 selector-oracle test (T013/T021 oracle).
///
///         For every event/function consumed by the framework, the
///         oracle emits the canonical topic0 / 4-byte selector as
///         structured logs. The Python test suite under
///         tests/test_abi_artifacts.py runs `forge test --json -vv`
///         against this contract and byte-compares the parsed values
///         against docs/implement/protocol-artifacts/v4-core-<commit>.json.
///         Any mismatch fails the test.
contract SelectorOracleTest is Test {
    SelectorOracle internal oracle;

    function setUp() public {
        oracle = new SelectorOracle();
    }

    // -------------------------------------------------------------------
    // PoolManager event topics
    // -------------------------------------------------------------------

    function test_PoolManager_Initialize_topic() public {
        bytes32 topic = oracle.emitInitializeTopic();
        emit log_named_string("kind", "event");
        emit log_named_string("contract", "PoolManager");
        emit log_named_string("name", "Initialize");
        emit log_named_bytes32("topic0", topic);
    }

    function test_PoolManager_ModifyLiquidity_topic() public {
        bytes32 topic = oracle.emitModifyLiquidityTopic();
        emit log_named_string("kind", "event");
        emit log_named_string("contract", "PoolManager");
        emit log_named_string("name", "ModifyLiquidity");
        emit log_named_bytes32("topic0", topic);
    }

    function test_PoolManager_Swap_topic() public {
        bytes32 topic = oracle.emitSwapTopic();
        emit log_named_string("kind", "event");
        emit log_named_string("contract", "PoolManager");
        emit log_named_string("name", "Swap");
        emit log_named_bytes32("topic0", topic);
    }

    function test_PoolManager_Donate_topic() public {
        bytes32 topic = oracle.emitDonateTopic();
        emit log_named_string("kind", "event");
        emit log_named_string("contract", "PoolManager");
        emit log_named_string("name", "Donate");
        emit log_named_bytes32("topic0", topic);
    }

    // -------------------------------------------------------------------
    // PoolManager function selectors
    // -------------------------------------------------------------------

    function test_PoolManager_initialize_selector() public {
        bytes4 sel = oracle.emitInitializeSelector();
        _logSelector("PoolManager", "initialize", sel);
    }

    function test_PoolManager_modifyLiquidity_selector() public {
        bytes4 sel = oracle.emitModifyLiquiditySelector();
        _logSelector("PoolManager", "modifyLiquidity", sel);
    }

    function test_PoolManager_swap_selector() public {
        bytes4 sel = oracle.emitSwapSelector();
        _logSelector("PoolManager", "swap", sel);
    }

    function test_PoolManager_donate_selector() public {
        bytes4 sel = oracle.emitDonateSelector();
        _logSelector("PoolManager", "donate", sel);
    }

    // -------------------------------------------------------------------
    // StateView function selectors
    // -------------------------------------------------------------------

    function test_StateView_getSlot0_selector() public {
        _logSelector("StateView", "getSlot0", oracle.emitGetSlot0Selector());
    }

    function test_StateView_getTickInfo_selector() public {
        _logSelector("StateView", "getTickInfo", oracle.emitGetTickInfoSelector());
    }

    function test_StateView_getTickLiquidity_selector() public {
        _logSelector("StateView", "getTickLiquidity", oracle.emitGetTickLiquiditySelector());
    }

    function test_StateView_getTickFeeGrowthOutside_selector() public {
        _logSelector(
            "StateView", "getTickFeeGrowthOutside", oracle.emitGetTickFeeGrowthOutsideSelector()
        );
    }

    function test_StateView_getFeeGrowthGlobals_selector() public {
        _logSelector(
            "StateView", "getFeeGrowthGlobals", oracle.emitGetFeeGrowthGlobalsSelector()
        );
    }

    function test_StateView_getLiquidity_selector() public {
        _logSelector("StateView", "getLiquidity", oracle.emitGetLiquiditySelector());
    }

    function test_StateView_getTickBitmap_selector() public {
        _logSelector("StateView", "getTickBitmap", oracle.emitGetTickBitmapSelector());
    }

    function test_StateView_getPositionInfo_owner_selector() public {
        _logSelector(
            "StateView", "getPositionInfo_owner", oracle.emitGetPositionInfoOwnerSelector()
        );
    }

    function test_StateView_getPositionInfo_id_selector() public {
        _logSelector(
            "StateView", "getPositionInfo_id", oracle.emitGetPositionInfoIdSelector()
        );
    }

    function test_StateView_getPositionLiquidity_selector() public {
        _logSelector(
            "StateView", "getPositionLiquidity", oracle.emitGetPositionLiquiditySelector()
        );
    }

    function test_StateView_getFeeGrowthInside_selector() public {
        _logSelector(
            "StateView", "getFeeGrowthInside", oracle.emitGetFeeGrowthInsideSelector()
        );
    }

    // -------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------

    function _logSelector(string memory contractName, string memory name, bytes4 sel) internal {
        emit log_named_string("kind", "function");
        emit log_named_string("contract", contractName);
        emit log_named_string("name", name);
        // bytes32 form makes forge JSON output consistent across log helpers.
        emit log_named_bytes32("selector_bytes32", bytes32(sel));
        emit log_named_uint("selector_uint", uint256(uint32(sel)));
    }
}
