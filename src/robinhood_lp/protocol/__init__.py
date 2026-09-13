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

__all__ = [
    "Address",
    "BlockRef",
    "CanonicalStatus",
    "ChainId",
    "Currency",
    "EventKey",
    "PoolKey",
    "PoolId",
    "TokenMetadata",
    "TransactionRef",
    "encode_pool_key",
    "pool_id_to_bytes",
    "pool_id_to_int",
]

__version__: str = "0.0.0"
