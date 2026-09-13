// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.26;

import {PoolKey} from "@v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "@v4-core/src/types/PoolId.sol";
import {Currency} from "@v4-core/src/types/Currency.sol";
import {IHooks} from "@v4-core/src/interfaces/IHooks.sol";

contract PoolIdOracle {
    using PoolIdLibrary for PoolKey;

    /// @dev Compute the canonical PoolId for a (currency0, currency1, fee,
    ///      tickSpacing, hooks) tuple. Inputs are reordered so that
    ///      currency0 < currency1 as uint160 (matching V4's invariant).
    function derive(
        address a0,
        address a1,
        uint24 fee,
        int24 tickSpacing,
        address hooks
    ) external pure returns (bytes32 id, address ordered0, address ordered1) {
        (ordered0, ordered1) = a0 < a1 ? (a0, a1) : (a1, a0);
        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(ordered0),
            currency1: Currency.wrap(ordered1),
            fee: fee,
            tickSpacing: tickSpacing,
            hooks: IHooks(hooks)
        });
        id = PoolId.unwrap(key.toId());
    }
}
