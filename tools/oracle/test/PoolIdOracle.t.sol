// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.26;

import "forge-std/Test.sol";
import "../src/PoolIdOracle.sol";

/// @notice Generates pinned V4 PoolId vectors for the Python port (T013 oracle).
///         These vectors are intentionally computed by Solidity / keccak256,
///         never by the Python implementation under test.
contract PoolIdOracleTest is Test {
    PoolIdOracle internal oracle;

    function setUp() public {
        oracle = new PoolIdOracle();
    }

    function _emit(
        string memory name,
        address a0,
        address a1,
        uint24 fee,
        int24 tickSpacing,
        address hooks
    ) internal {
        (bytes32 id, address o0, address o1) = oracle.derive(a0, a1, fee, tickSpacing, hooks);
        emit log_named_string("name", name);
        emit log_named_address("ordered0", o0);
        emit log_named_address("ordered1", o1);
        emit log_named_uint("fee", fee);
        emit log_named_int("tickSpacing", tickSpacing);
        emit log_named_address("hooks", hooks);
        emit log_named_bytes32("poolId", id);
    }

    function test_V1_pinned_vectors() public {
        // V1: no hooks, 0.3% fee, 60 tick spacing
        _emit("v1_static_3000_60", address(0x10), address(0x20), 3000, 60, address(0));
        // Native currency as currency0
        _emit("native_currency0", address(0), address(0x20), 3000, 60, address(0));
        // Dynamic fee (must have non-zero hook address with valid flags)
        _emit("dynamic_fee_with_hook",
              address(0x10), address(0x20), uint24(0x800000), 60, address(uint160(1) << 7));
        // Max static fee
        _emit("max_static_fee", address(0x10), address(0x20), 1_000_000, 60, address(0));
        // Max tick spacing (32767)
        _emit("max_tick_spacing", address(0x10), address(0x20), 3000, 32767, address(0));
        // Hook with delta + action flags
        _emit("hook_with_delta_action",
              address(0x10), address(0x20), 3000, 60,
              address((uint160(1) << 7) | (uint160(1) << 3))); // BEFORE_SWAP + BEFORE_SWAP_RETURNS_DELTA
        // Reordered inputs (oracle sorts internally)
        _emit("reordered_inputs",
              address(0x20), address(0x10), 3000, 60, address(0));
    }
}
