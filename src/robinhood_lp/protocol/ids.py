"""Canonical protocol identifiers (T010).

Hard rules (from ``docs/protocol-facts.md``):

- all values are immutable value objects;
- ``Address`` is exactly 20 bytes (160 bits); the zero address
  represents native currency per ``Currency.sol``;
- ``Currency`` is an EVM address and compares as ``uint160``;
- ``ChainId`` is a positive integer and is part of every identity's
  namespace; identities from different chains never collide;
- ``PoolKey`` enforces the V4 ordering invariant:
  ``currency0 < currency1`` as ``uint160``;
- ``fee`` is ``uint24`` and is either ``<= MAX_LP_FEE`` or exactly the
  ``DYNAMIC_FEE_FLAG`` sentinel;
- ``tick_spacing`` is a positive ``int24`` in ``[1, 32_767]``;
- ``PoolId`` is ``bytes32`` (keccak256 of the ABI-encoded PoolKey);
- equality and hashing use only the raw bytes, never EIP-55 checksum
  form and never token metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from robinhood_lp.protocol.abi import compute_pool_id

# ---------------------------------------------------------------------------
# Protocol constants (pinned — see docs/protocol-facts.md)
# ---------------------------------------------------------------------------
MAX_LP_FEE: Final[int] = 1_000_000
DYNAMIC_FEE_FLAG: Final[int] = 0x800000
MAX_TICK_SPACING: Final[int] = 32_767
MIN_TICK_SPACING: Final[int] = 1

ADDRESS_BYTES: Final[int] = 20
POOL_ID_BYTES: Final[int] = 32


def _require_uint(value: int, *, bits: int, field: str) -> int:
    """Validate ``value`` is a non-negative integer fitting in ``bits`` bits."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field}: must be non-negative, got {value}")
    upper = 1 << bits
    if value >= upper:
        raise ValueError(f"{field}: exceeds {bits}-bit width, got {value}")
    return value


def _require_int(value: int, *, bits: int, field: str) -> int:
    """Validate ``value`` is a signed integer fitting in ``bits`` bits."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}: must be int, got {type(value).__name__}")
    lower = -(1 << (bits - 1))
    upper = 1 << (bits - 1)
    if value < lower or value >= upper:
        raise ValueError(f"{field}: out of int{bits} range, got {value}")
    return value


def _address_bytes(addr: Address) -> bytes:
    return addr.value.to_bytes(ADDRESS_BYTES, "big")


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Address:
    """An EVM address: 20 bytes (uint160).

    The zero address (``0x0000...0000``) is permitted; per V4
    ``Currency.sol`` it represents native currency when used as a
    pool currency.
    """

    value: int = field(hash=True, compare=True)

    def __post_init__(self) -> None:
        _require_uint(self.value, bits=160, field="Address.value")

    @classmethod
    def zero(cls) -> Address:
        """The canonical zero address (native currency sentinel)."""
        return cls(0)

    @classmethod
    def from_hex(cls, s: str) -> Address:
        """Parse a 0x-prefixed 40-hex-character string into an Address."""
        if not isinstance(s, str):
            raise TypeError("from_hex: input must be a string")
        s_clean = s.lower()
        if not s_clean.startswith("0x") or len(s_clean) != 2 + 2 * ADDRESS_BYTES:
            raise ValueError(f"from_hex: expected 0x + {2 * ADDRESS_BYTES} hex chars, got {s!r}")
        try:
            return cls(int(s_clean, 16))
        except ValueError as e:
            raise ValueError(f"from_hex: invalid hex in {s!r}: {e}") from e

    def to_hex(self) -> str:
        """Return the canonical lowercase 0x-prefixed 40-hex representation."""
        return "0x" + self.value.to_bytes(ADDRESS_BYTES, "big").hex()

    def to_bytes(self) -> bytes:
        """Return the raw 20-byte big-endian encoding."""
        return self.value.to_bytes(ADDRESS_BYTES, "big")

    def __repr__(self) -> str:
        return f"Address({self.to_hex()})"


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Currency:
    """A V4 ``Currency``: an EVM address used to identify a pool token.

    Per ``docs/protocol-facts.md`` and ``Currency.sol``, ``Currency`` is
    structurally identical to ``Address``; the zero address represents
    native currency. ``Currency`` is a distinct type so that
    ``PoolKey.currency0 < PoolKey.currency1`` is checked at the type
    level rather than relying on convention.
    """

    address: Address

    @classmethod
    def native(cls) -> Currency:
        """The native currency (zero address)."""
        return cls(Address.zero())

    @classmethod
    def from_address(cls, addr: Address) -> Currency:
        return cls(addr)

    @classmethod
    def from_int(cls, value: int) -> Currency:
        return cls(Address(value))

    @classmethod
    def from_hex(cls, s: str) -> Currency:
        return cls(Address.from_hex(s))

    def is_native(self) -> bool:
        return self.address.value == 0

    def to_address(self) -> Address:
        return self.address

    def __repr__(self) -> str:
        if self.is_native():
            return "Currency(native)"
        return f"Currency({self.address.to_hex()})"


# ---------------------------------------------------------------------------
# ChainId
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainId:
    """An EIP-155 chain identifier: a positive integer.

    ``ChainId`` participates in cross-chain identity namespacing:
    identities from different chains are distinct even when their raw
    bytes match.
    """

    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise TypeError(f"ChainId: must be int, got {type(self.value).__name__}")
        if self.value <= 0:
            raise ValueError(f"ChainId: must be positive, got {self.value}")

    def __repr__(self) -> str:
        return f"ChainId({self.value})"


# ---------------------------------------------------------------------------
# PoolKey
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolKey:
    """A canonical V4 ``PoolKey`` (T010).

    All five fields participate in identity; equality and hashing use
    only these bytes, never token metadata.
    """

    currency0: Currency
    currency1: Currency
    fee: int
    tick_spacing: int
    hooks: Address

    def __post_init__(self) -> None:
        # currency ordering: currency0 < currency1 as uint160
        if self.currency0.address.value >= self.currency1.address.value:
            raise ValueError(
                "PoolKey.currency0 must be strictly less than PoolKey.currency1 "
                f"as uint160 (got {self.currency0.address.value:#x} >= "
                f"{self.currency1.address.value:#x})"
            )

        # fee: uint24 in [0, MAX_LP_FEE] OR exactly DYNAMIC_FEE_FLAG
        _require_uint(self.fee, bits=24, field="PoolKey.fee")
        if self.fee > MAX_LP_FEE and self.fee != DYNAMIC_FEE_FLAG:
            raise ValueError(
                f"PoolKey.fee={self.fee} is invalid: must be in [0, {MAX_LP_FEE}] "
                f"or exactly DYNAMIC_FEE_FLAG ({DYNAMIC_FEE_FLAG:#x})"
            )

        # tick_spacing: int24 in [MIN_TICK_SPACING, MAX_TICK_SPACING]
        _require_int(self.tick_spacing, bits=24, field="PoolKey.tick_spacing")
        if self.tick_spacing < MIN_TICK_SPACING or self.tick_spacing > MAX_TICK_SPACING:
            raise ValueError(
                f"PoolKey.tick_spacing={self.tick_spacing} must be in "
                f"[{MIN_TICK_SPACING}, {MAX_TICK_SPACING}]"
            )

        # hooks address: uint160 (already enforced by Address)
        # No further validation here; full hook-address validity is
        # exercised at the PoolKey model level (config layer) per ADR-005.
        # The protocol layer only encodes the raw bytes.

    def to_pool_id(self) -> PoolId:
        """Derive the canonical V4 ``PoolId`` (keccak256 of ABI-encoded key)."""
        return PoolId(compute_pool_id(self))


# ---------------------------------------------------------------------------
# PoolId
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolId:
    """A V4 ``PoolId``: a 32-byte value (keccak256 of an ABI-encoded PoolKey).

    Equality and hashing use the raw 32 bytes; the integer form is for
    arithmetic; the hex form is for display.
    """

    value: int = field(hash=True, compare=True)

    def __post_init__(self) -> None:
        _require_uint(self.value, bits=256, field="PoolId.value")

    @classmethod
    def from_bytes(cls, b: bytes) -> PoolId:
        if not isinstance(b, bytes):
            raise TypeError("from_bytes: input must be bytes")
        if len(b) != POOL_ID_BYTES:
            raise ValueError(f"from_bytes: expected {POOL_ID_BYTES} bytes, got {len(b)}")
        return cls(int.from_bytes(b, "big"))

    @classmethod
    def from_hex(cls, s: str) -> PoolId:
        if not isinstance(s, str):
            raise TypeError("from_hex: input must be a string")
        s_clean = s.lower()
        if not s_clean.startswith("0x") or len(s_clean) != 2 + 2 * POOL_ID_BYTES:
            raise ValueError(f"from_hex: expected 0x + {2 * POOL_ID_BYTES} hex chars, got {s!r}")
        try:
            return cls(int(s_clean, 16))
        except ValueError as e:
            raise ValueError(f"from_hex: invalid hex in {s!r}: {e}") from e

    def to_bytes(self) -> bytes:
        return self.value.to_bytes(POOL_ID_BYTES, "big")

    def to_hex(self) -> str:
        return "0x" + self.to_bytes().hex()

    def __repr__(self) -> str:
        return f"PoolId({self.to_hex()})"


__all__ = [
    "ADDRESS_BYTES",
    "Address",
    "ChainId",
    "Currency",
    "DYNAMIC_FEE_FLAG",
    "MAX_LP_FEE",
    "MAX_TICK_SPACING",
    "MIN_TICK_SPACING",
    "POOL_ID_BYTES",
    "PoolId",
    "PoolKey",
]
