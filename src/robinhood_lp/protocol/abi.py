"""Canonical ABI encoding and PoolId derivation for V4 PoolKey (T010).

The Solidity reference implementation
(``src/types/PoolId.sol::toId``) is:

```solidity
function toId(PoolKey memory poolKey) internal pure returns (PoolId poolId) {
    assembly ("memory-safe") {
        poolId := keccak256(poolKey, 0xa0)
    }
}
```

The 0xa0 (= 160) bytes are the size of the ABI-encoded ``PoolKey``
struct: 5 fields × 32 bytes per slot. The order of fields is the order
declared in ``PoolKey.sol``: ``(Currency, Currency, uint24, int24, IHooks)``.

ABI encoding rules used here:

- ``Currency`` is ``address`` → encoded as ``uint256(uint160(address))``
  → zero-padded 32 bytes;
- ``uint24 fee`` → encoded as ``uint256`` → zero-padded 32 bytes;
- ``int24 tick_spacing`` → encoded as ``int256`` → sign-extended 32 bytes;
- ``address hooks`` → encoded as ``uint256(uint160(address))`` →
  zero-padded 32 bytes (the zero address is the canonical "no hooks").

This module is intentionally pure: no I/O, no time, no random, no
logging. It depends only on ``eth_hash`` (keccak256) and the
``PoolKey`` value object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eth_hash.auto import keccak

if TYPE_CHECKING:
    from robinhood_lp.protocol.ids import PoolId, PoolKey


def _slot_uint(value: int) -> bytes:
    """Encode a non-negative integer as a 32-byte big-endian slot."""
    if value < 0:
        raise ValueError(f"_slot_uint: negative value {value}")
    return value.to_bytes(32, "big")


def _slot_int(value: int) -> bytes:
    """Encode a signed integer as a 32-byte big-endian two's-complement slot."""
    if value < -(1 << 255) or value >= (1 << 255):
        raise ValueError(f"_slot_int: out of int256 range: {value}")
    return value.to_bytes(32, "big", signed=True)


def encode_pool_key(pool_key: PoolKey) -> bytes:
    """Return the canonical ABI encoding of ``pool_key`` (5 × 32 = 160 bytes).

    Field order matches the Solidity struct declaration in
    ``PoolKey.sol``: ``(Currency, Currency, uint24, int24, IHooks)``.
    """
    return b"".join(
        [
            _slot_uint(pool_key.currency0.address.value),
            _slot_uint(pool_key.currency1.address.value),
            _slot_uint(pool_key.fee),
            _slot_int(pool_key.tick_spacing),
            _slot_uint(pool_key.hooks.value),
        ]
    )


def compute_pool_id(pool_key: PoolKey) -> int:
    """Return the canonical V4 ``PoolId`` as an unsigned 256-bit integer."""
    encoded = encode_pool_key(pool_key)
    assert len(encoded) == 160, f"encoded length {len(encoded)} != 160"
    digest = keccak(encoded)
    return int.from_bytes(digest, "big")


def pool_id_to_bytes(pool_id: PoolId) -> bytes:
    """Return the raw 32-byte big-endian encoding of a PoolId."""
    return pool_id.to_bytes()


def pool_id_to_int(pool_id: PoolId) -> int:
    """Return the unsigned 256-bit integer form of a PoolId."""
    return pool_id.value


__all__ = [
    "compute_pool_id",
    "encode_pool_key",
    "pool_id_to_bytes",
    "pool_id_to_int",
]
