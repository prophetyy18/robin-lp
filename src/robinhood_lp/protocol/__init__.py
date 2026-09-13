"""Canonical Uniswap V4 protocol identifiers (T010).

This package is the protocol/domain layer per
``docs/architecture.md`` §2.1: it owns ``ChainId``, ``Address``,
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
    price_to_sqrt_price_x96,
    sqrt_price_x96_to_price,
)
from robinhood_lp.protocol.run_mode import RunMode

__all__ = [
    "Address",
    "AmountDeltaError",
    "BlockRef",
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
    "MIN_SQRT_PRICE_X96",
    "MIN_TICK",
    "MIN_TICK_SPACING",
    "PoolId",
    "PoolKey",
    "RunMode",
    "TickMathError",
    "TokenMetadata",
    "TransactionRef",
    "encode_pool_key",
    "get_amount0_delta",
    "get_amount1_delta",
    "get_liquidity_for_amounts",
    "get_sqrt_price_at_tick",
    "get_tick_at_sqrt_price",
    "max_usable_tick",
    "min_usable_tick",
    "pool_id_to_bytes",
    "pool_id_to_int",
    "price_to_sqrt_price_x96",
    "sqrt_price_x96_to_price",
]

__version__: str = "0.0.0"
