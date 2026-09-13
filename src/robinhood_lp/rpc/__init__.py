"""RPC adapter layer (T020).

Re-exports the bounded read-only JSON-RPC adapter. This layer may
depend only on ``robinhood_lp.protocol`` per ADR-006; it must not
import ``robinhood_lp.config`` or higher layers.
"""

from __future__ import annotations

from robinhood_lp.rpc.adapter import (
    ALLOWED_METHODS,
    NON_RETRYABLE_METHODS,
    RpcAdapter,
    RpcConfig,
    RpcEndpoint,
    RpcError,
    RpcMethodNotAllowedError,
    RpcMetrics,
    RpcProtocolError,
    RpcResponseError,
    RpcRetryExhausted,
    TransportCallable,
    TransportError,
    to_hex,
)

__all__ = [
    "ALLOWED_METHODS",
    "NON_RETRYABLE_METHODS",
    "RpcAdapter",
    "RpcConfig",
    "RpcEndpoint",
    "RpcError",
    "RpcMetrics",
    "RpcMethodNotAllowedError",
    "RpcProtocolError",
    "RpcResponseError",
    "RpcRetryExhausted",
    "TransportCallable",
    "TransportError",
    "to_hex",
]
