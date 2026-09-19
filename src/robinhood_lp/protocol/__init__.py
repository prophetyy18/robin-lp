"""Canonical Uniswap V4 protocol identifiers (T010).

This package is the protocol/domain layer per
``docs/spec/architecture/ARCHITECTURE.md`` §2.1: it owns ``ChainId``, ``Address``,
``Currency``, ``PoolKey``, and ``PoolId``. It has zero RPC, storage,
network, time, or random dependencies and is importable in unit tests
without any chain connection.

The corresponding pydantic configuration models live in
:mod:`robinhood_lp.config.models`; they are deliberately separate
because configuration uses string literals while the protocol layer
uses integer-typed values suitable for ABI encoding and keccak256.
"""

from __future__ import annotations

from robinhood_lp.protocol.abi import encode_pool_key, pool_id_to_bytes, pool_id_to_int
from robinhood_lp.protocol.abi_artifacts import (
    EVENT_TOPICS,
    FUNCTION_SELECTORS,
)
from robinhood_lp.protocol.events import (
    BlockRef,
    CanonicalStatus,
    EventKey,
    TokenMetadata,
    TransactionRef,
)
from robinhood_lp.protocol.identity import PoolIdentity
from robinhood_lp.protocol.ids import (
    Address,
    ChainId,
    Currency,
    PoolId,
    PoolKey,
)
from robinhood_lp.protocol.math import (
    MAX_SQRT_PRICE_X96,
    MAX_TICK,
    MAX_TICK_SPACING,
    MIN_SQRT_PRICE_X96,
    MIN_TICK,
    MIN_TICK_SPACING,
    AmountDeltaError,
    InvalidSqrtPriceError,
    InvalidTickError,
    TickMathError,
    get_amount0_delta,
    get_amount1_delta,
    get_liquidity_for_amounts,
    get_sqrt_price_at_tick,
    get_tick_at_sqrt_price,
    max_usable_tick,
    min_usable_tick,
)
from robinhood_lp.protocol.run_mode import RunMode
from robinhood_lp.protocol.sizing import (
    BINDING_AMOUNT0,
    BINDING_AMOUNT1,
    BINDING_BOTH,
    MAX_UINT128,
    MAX_UINT256,
    POSITION_ABOVE,
    POSITION_BELOW,
    POSITION_INSIDE,
    BindingSide,
    PositionRelative,
    ReasonCode,
    SizingAlignmentError,
    SizingCapacityError,
    SizingDecision,
    SizingError,
    SizingGasError,
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

__all__ = [
    "Address",
    "AmountDeltaError",
    "BINDING_AMOUNT0",
    "BINDING_AMOUNT1",
    "BINDING_BOTH",
    "BlockRef",
    "BindingSide",
    "CanonicalStatus",
    "ChainId",
    "Currency",
    "EVENT_TOPICS",
    "FUNCTION_SELECTORS",
    "EventKey",
    "InvalidSqrtPriceError",
    "InvalidTickError",
    "MAX_SQRT_PRICE_X96",
    "MAX_TICK",
    "MAX_TICK_SPACING",
    "MAX_UINT128",
    "MAX_UINT256",
    "MIN_SQRT_PRICE_X96",
    "MIN_TICK",
    "MIN_TICK_SPACING",
    "POSITION_ABOVE",
    "POSITION_BELOW",
    "POSITION_INSIDE",
    "PoolId",
    "PoolIdentity",
    "PoolKey",
    "PositionRelative",
    "ReasonCode",
    "RunMode",
    "SizingAlignmentError",
    "SizingCapacityError",
    "SizingDecision",
    "SizingError",
    "SizingGasError",
    "SizingHookError",
    "SizingInputError",
    "SizingRangeError",
    "SizingUsdgConversionError",
    "TickMathError",
    "TokenMetadata",
    "TransactionRef",
    "compute_cap_bounded_liquidity",
    "compute_sizing",
    "encode_pool_key",
    "get_amount0_delta",
    "get_amount1_delta",
    "get_amounts_for_liquidity",
    "get_liquidity_for_amounts",
    "get_sqrt_price_at_tick",
    "get_tick_at_sqrt_price",
    "max_usable_tick",
    "min_usable_tick",
    "pool_id_to_bytes",
    "pool_id_to_int",
    "usdg_amount_to_token_amount",
    "worst_case_single_sided_inventory",
]

__version__: str = "0.0.0"
