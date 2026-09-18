"""Directional packed protocol-fee helpers (T040).

The V4 ``ProtocolFeeUpdated`` event emits a packed ``uint24`` whose
high 12 bits encode the token-0 protocol fee and whose low 12 bits
encode the token-1 protocol fee. The packing is the on-chain ABI
view; the *directional* state tracks the two halves independently
so a checkpoint carries ``(token0_fee, token1_fee)`` separately and
can re-pack them back to the same ``uint24`` value with no loss.

Both halves are 12-bit unsigned integers in ``[0, 4095]``. Values
outside that range are rejected — V4 would have emitted a packed
``uint24`` that overflowed the wire format.

The ``PROTOCOL_FEE_HALF_MASK`` constant is the bit mask the wire
format uses; it is exposed so downstream tests can compare against
the V4 ABI directly.
"""

from __future__ import annotations

from typing import Final

PROTOCOL_FEE_TOKEN0_SHIFT: Final[int] = 12
PROTOCOL_FEE_HALF_BITS: Final[int] = 12
PROTOCOL_FEE_HALF_MAX: Final[int] = (1 << 12) - 1  # 4095
PROTOCOL_FEE_HALF_MASK: Final[int] = (1 << 12) - 1  # 4095


def _require_half(value: int, *, field: str) -> int:
    """Validate a 12-bit unsigned protocol-fee half."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field}: must be non-negative, got {value}")
    if value > PROTOCOL_FEE_HALF_MAX:
        raise ValueError(
            f"{field}: exceeds 12-bit width, got {value} (max {PROTOCOL_FEE_HALF_MAX})"
        )
    return value


def pack_protocol_fee(token0_fee: int, token1_fee: int) -> int:
    """Pack two 12-bit halves into the on-chain ``uint24`` format.

    The packing matches the V4 IProtocolFees ABI: high 12 bits are
    the token-0 fee, low 12 bits are the token-1 fee.
    """
    t0 = _require_half(token0_fee, field="token0_fee")
    t1 = _require_half(token1_fee, field="token1_fee")
    return (t0 << PROTOCOL_FEE_TOKEN0_SHIFT) | t1


def unpack_protocol_fee(packed: int) -> tuple[int, int]:
    """Unpack a ``uint24`` packed protocol fee into ``(token0, token1)``.

    The input is validated as a ``uint24`` (non-negative,
    fits in 24 bits). Returns ``(token0_fee, token1_fee)`` where
    each is a 12-bit unsigned integer.

    :func:`pack_protocol_fee` and :func:`unpack_protocol_fee` are
    mutual inverses on the valid domain:
    ``unpack_protocol_fee(pack_protocol_fee(a, b)) == (a, b)`` and
    ``pack_protocol_fee(*unpack_protocol_fee(p)) == p`` for every
    valid 24-bit ``p``.
    """
    if not isinstance(packed, int) or isinstance(packed, bool):
        raise TypeError(f"packed: must be int, got {type(packed).__name__}")
    if packed < 0:
        raise ValueError(f"packed: must be non-negative, got {packed}")
    if packed >= (1 << 24):
        raise ValueError(f"packed: exceeds uint24, got {packed}")
    token0 = (packed >> PROTOCOL_FEE_TOKEN0_SHIFT) & PROTOCOL_FEE_HALF_MASK
    token1 = packed & PROTOCOL_FEE_HALF_MASK
    return token0, token1


__all__ = [
    "PROTOCOL_FEE_HALF_BITS",
    "PROTOCOL_FEE_HALF_MASK",
    "PROTOCOL_FEE_HALF_MAX",
    "PROTOCOL_FEE_TOKEN0_SHIFT",
    "pack_protocol_fee",
    "unpack_protocol_fee",
]
