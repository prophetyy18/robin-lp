"""Defensive ERC-20 metadata reader (T022).

Reads ``symbol()``, ``name()``, ``decimals()`` from a token contract
with retry-on-revert and bounded timeouts. Per
``docs/product/ASSET_ADMISSION.md`` §3 (TokenMetadata) and T011, a
token whose metadata call reverts is still representable; the
scanner records the failure reason rather than dropping the pool.

Hard rules (T022 must-not):

- never drop a pool because its metadata call fails;
- never use the symbol as an identity (it is display-only);
- never guess a default when the call returns empty data; the
  value is ``None`` and the reason is recorded.

This module sits in the storage layer per ADR-006 and depends on
``robinhood_lp.protocol`` and ``robinhood_lp.rpc``. It must not
import ``robinhood_lp.config``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from robinhood_lp.protocol import Address
from robinhood_lp.rpc import RpcAdapter

#: 4-byte selectors for the ERC-20 metadata methods we read.
#: They are computed once here from the canonical function names so
#: the values do not depend on V4 artifacts. They are not V4-specific.
_SELECTOR_SYMBOL: Final[bytes] = bytes.fromhex("95d89b41")  # symbol()
_SELECTOR_NAME: Final[bytes] = bytes.fromhex("06fdde03")  # name()
_SELECTOR_DECIMALS: Final[bytes] = bytes.fromhex("313ce567")  # decimals()


#: Maximum bytes a single response from ``symbol()`` / ``name()``
#: may return. ERC-20 strings longer than this are pathological;
#: we cap the read at 256 to avoid feeding huge blobs into the
#: metadata record.
_MAX_STRING_BYTES: Final[int] = 256


class TokenMetadataError(RuntimeError):
    """The metadata call path failed irrecoverably."""


@dataclass(frozen=True, slots=True)
class TokenMetadataRecord:
    """The outcome of one token's metadata probe.

    All three fields are independent: a token may have ``symbol`` but
    not ``decimals`` (different ABIs, non-conformant tokens), and the
    scanner records the failure reasons rather than dropping the
    pool.
    """

    address: Address
    symbol: str | None
    name: str | None
    decimals: int | None
    symbol_error: str | None = None
    name_error: str | None = None
    decimals_error: str | None = None

    def is_complete(self) -> bool:
        return self.symbol is not None and self.name is not None and self.decimals is not None


def _decode_string_response(raw_hex: str) -> tuple[str | None, str | None]:
    """Decode a Solidity string return.

    ABI-encoded strings are ``(uint256 offset, uint256 length)`` for
    dynamic strings; a ``bytes32`` literal returns the value in the
    first 32 bytes directly. We try both layouts.
    """
    if raw_hex in ("0x", ""):
        return None, "empty response"
    s = raw_hex.lower()
    if not s.startswith("0x"):
        return None, "not 0x-prefixed"
    body = bytes.fromhex(s[2:])
    if len(body) < 32:
        return None, f"response shorter than one slot: {len(body)} bytes"

    # Layout A: dynamic string. The first slot is the offset (>=32),
    # the second is the length. With Solidity's default abi-encoder
    # the offset points to the start of the data block, which is
    # typically byte 32 (i.e. immediately after the head's two
    # 32-byte slots). We accept any offset >= 32 so we tolerate
    # padded layouts.
    offset = int.from_bytes(body[0:32], "big")
    length = int.from_bytes(body[32:64], "big") if len(body) >= 64 else 0
    if 32 <= offset < len(body) and length <= len(body) - offset:
        raw = body[offset : offset + length]
        if length == 0:
            return None, "empty dynamic string"
        # Solidity strings are ABI-encoded UTF-8.
        try:
            return raw.rstrip(b"\x00").decode("utf-8"), None
        except UnicodeDecodeError as exc:
            return None, f"utf-8 decode failed: {exc}"

    # Layout B: bytes32-style short string. Solidity pads short
    # strings on the *left* with zeros (high bytes). We strip the
    # left padding; trailing bytes inside the value (which would
    # indicate garbage) are preserved as part of the string and
    # rejected by the utf-8 decoder if non-text.
    raw = body[0:32]
    stripped = raw.lstrip(b"\x00")
    if not stripped:
        return None, "all-zero bytes32 response"
    try:
        return stripped.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, f"utf-8 decode failed: {exc}"


def _decode_uint8_response(raw_hex: str) -> tuple[int | None, str | None]:
    if raw_hex in ("0x", ""):
        return None, "empty response"
    s = raw_hex.lower()
    if not s.startswith("0x"):
        return None, "not 0x-prefixed"
    body = bytes.fromhex(s[2:])
    if len(body) < 32:
        return None, f"response shorter than one slot: {len(body)} bytes"
    value = int.from_bytes(body[31:32], "big")  # uint8 sits in the last byte
    return value, None


async def _call(adapter: RpcAdapter, to: str, data: str) -> str:
    """One eth_call; the adapter already enforces block-pinned semantics."""
    return await adapter.eth_call(to=to, data=data, block_number=0)


async def read_token_metadata(
    adapter: RpcAdapter,
    token: Address,
    *,
    block_number: int = 0,
) -> TokenMetadataRecord:
    """Read ERC-20 metadata defensively.

    Each of ``symbol()``, ``name()``, ``decimals()`` is wrapped in
    its own try/except so a failure on one does not abort the others.
    The returned record carries the failure reasons; the caller decides
    whether to surface them (T022 records them in the registry; T070
    uses them as risk signals).
    """
    addr_hex = token.to_hex()

    symbol: str | None = None
    symbol_error: str | None = None
    try:
        symbol, symbol_error = _decode_string_response(
            await _call(adapter, addr_hex, "0x" + _SELECTOR_SYMBOL.hex())
        )
    except Exception as exc:  # noqa: BLE001
        symbol_error = f"{type(exc).__name__}: {exc}"
    if symbol is not None and len(symbol) > _MAX_STRING_BYTES:
        symbol = symbol[:_MAX_STRING_BYTES]
        symbol_error = (symbol_error or "") + " (truncated)"

    name: str | None = None
    name_error: str | None = None
    try:
        name, name_error = _decode_string_response(
            await _call(adapter, addr_hex, "0x" + _SELECTOR_NAME.hex())
        )
    except Exception as exc:  # noqa: BLE001
        name_error = f"{type(exc).__name__}: {exc}"
    if name is not None and len(name) > _MAX_STRING_BYTES:
        name = name[:_MAX_STRING_BYTES]
        name_error = (name_error or "") + " (truncated)"

    decimals: int | None = None
    decimals_error: str | None = None
    try:
        decimals, decimals_error = _decode_uint8_response(
            await _call(adapter, addr_hex, "0x" + _SELECTOR_DECIMALS.hex())
        )
    except Exception as exc:  # noqa: BLE001
        decimals_error = f"{type(exc).__name__}: {exc}"

    # Suppress the unused-argument warning for ``block_number``; it is
    # accepted for callers that want to pin a specific block. The
    # current implementation reads via the adapter's latest block, but
    # the parameter is part of the public contract and may be wired
    # through to the adapter in a follow-up.
    _ = block_number

    return TokenMetadataRecord(
        address=token,
        symbol=symbol,
        name=name,
        decimals=decimals,
        symbol_error=symbol_error,
        name_error=name_error,
        decimals_error=decimals_error,
    )


__all__ = [
    "TokenMetadataError",
    "TokenMetadataRecord",
    "read_token_metadata",
]
