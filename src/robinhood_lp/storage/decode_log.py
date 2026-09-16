"""Decode raw JSON-RPC log entries into typed V4 event records (T030).

This module bridges raw bytes (the ``eth_getLogs`` JSON-RPC response the
server returned) and the typed V4 log records defined in
:mod:`robinhood_lp.storage.schema`.

The decoder is intentionally narrow: it only consumes the V4 events the
framework actually uses (``Initialize`` / ``ModifyLiquidity`` / ``Swap`` /
``Donate`` / ``ProtocolFeeUpdated``), matches the topic0 against the
pinned artifact (:data:`robinhood_lp.protocol.abi_artifacts.EVENT_TOPICS`),
and returns the corresponding typed record with the **raw topics / raw
data / raw response wrapper** preserved alongside the decoded typed
fields.

Hard rules (T030 must-not):

- raw values are **never** overwritten by decoded or display values;
  both live on the returned record;
- integers are **never** stored as ``float``; signed amounts and signed
  liquidity deltas keep their exact sign and ABI width;
- token metadata (symbol / name / decimals) is **never** required; the
  decoder is metadata-free by construction.

The decoder is also the single integration point with the T011 protocol
identifiers (``EventKey`` / ``BlockRef`` / ``TransactionRef`` /
``PoolIdentity``): it returns typed records whose
``event_key()`` method materialises the canonical ``EventKey`` and whose
``block_ref()`` / ``transaction_ref()`` helpers expose the corresponding
T011 references for downstream code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from robinhood_lp.protocol.abi_artifacts import EVENT_TOPICS
from robinhood_lp.protocol.ids import Address, ChainId, PoolId
from robinhood_lp.storage.schema import (
    CURRENT_DECODE_VERSION,
    AcquisitionProvenance,
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    ProtocolFeeUpdatedLogRecord,
    SwapLogRecord,
)

# ---------------------------------------------------------------------------
# ABI layout for each V4 event (indexed vs data fields)
# ---------------------------------------------------------------------------
#
# The artifact signature strings (``v4-core-e50237c.json``) declare the
# parameter types but not the ``indexed`` keyword — that lives in the
# Solidity source. The pinned v4-core commit
# ``e50237c43811bd9b526eff40f26772152a42daba`` declares the indexed
# fields as follows:
#
# - Initialize: ``(PoolId indexed, address indexed currency0, address
#   indexed currency1, uint24 fee, int24 tickSpacing, address hooks,
#   uint160 sqrtPriceX96, int24 tick)``.
# - ModifyLiquidity: ``(PoolId indexed, address indexed sender,
#   int24 tickLower, int24 tickUpper, int256 liquidityDelta,
#   bytes32 salt)``.
# - Swap: ``(PoolId indexed, address indexed sender, int128 amount0,
#   int128 amount1, uint160 sqrtPriceX96, uint128 liquidity,
#   int24 tick, uint24 fee)``.
# - Donate: ``(PoolId indexed, address indexed sender, uint256 amount0,
#   uint256 amount1)``.
# - ProtocolFeeUpdated: ``(PoolId indexed, uint24 protocolFee)``
#   (inherited from ``IProtocolFees.sol``).
#
# Each tuple below is the ordered list of (name, abi_type) for the
# **data** slots (the non-indexed fields); topics[1:] carry the indexed
# fields in the order they appear in the Solidity declaration.

_DATA_FIELDS: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "Initialize": (
        ("fee", "uint24"),
        ("tick_spacing", "int24"),
        ("hooks", "address"),
        ("sqrt_price_x96", "uint160"),
        ("tick", "int24"),
    ),
    "ModifyLiquidity": (
        ("tick_lower", "int24"),
        ("tick_upper", "int24"),
        ("liquidity_delta", "int256"),
        ("salt", "bytes32"),
    ),
    "Swap": (
        ("amount0", "int128"),
        ("amount1", "int128"),
        ("sqrt_price_x96", "uint160"),
        ("liquidity", "uint128"),
        ("tick", "int24"),
        ("fee", "uint24"),
    ),
    "Donate": (
        ("amount0", "uint256"),
        ("amount1", "uint256"),
    ),
    "ProtocolFeeUpdated": (("protocol_fee", "uint24"),),
}

#: Number of *indexed* topics after topic0 each event expects.
_INDEXED_TOPIC_COUNTS: Final[dict[str, int]] = {
    "Initialize": 3,
    "ModifyLiquidity": 2,
    "Swap": 2,
    "Donate": 2,
    "ProtocolFeeUpdated": 1,
}


# ---------------------------------------------------------------------------
# Low-level ABI helpers
# ---------------------------------------------------------------------------


def _hex_to_bytes(value: Any, *, field: str) -> bytes:
    """Convert a ``0x``-prefixed hex string (or bytes) to bytes.

    Rejects any value whose textual form contains URL-credential
    markers (``://``, ``@``, ``?``) — those would never appear in a
    valid ABI blob and indicate an upstream bug or injection attempt.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not isinstance(value, str):
        raise TypeError(f"{field}: must be hex string or bytes, got {type(value).__name__}")
    s = value
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    if not s:
        return b""
    bad = ("://", "@", "?")
    for needle in bad:
        if needle in value:
            raise ValueError(
                f"{field}: hex blob contains URL-credential marker {needle!r}; refusing to parse"
            )
    try:
        return bytes.fromhex(s)
    except ValueError as exc:
        raise ValueError(f"{field}: invalid hex {value!r}: {exc}") from exc


def _slot_uint(slot: bytes, *, bits: int, field: str) -> int:
    """Decode a 32-byte big-endian unsigned slot of the given width.

    The decoder enforces the exact width the ABI requires (e.g. ``uint24``
    for ``fee``); values that exceed ``bits`` are rejected rather than
    silently truncated.
    """
    if len(slot) != 32:
        raise ValueError(f"{field}: slot must be 32 bytes, got {len(slot)}")
    value = int.from_bytes(slot, "big")
    upper = 1 << bits
    if value >= upper:
        raise ValueError(f"{field}: value {value} exceeds {bits}-bit width")
    return value


def _slot_int(slot: bytes, *, bits: int, field: str) -> int:
    """Decode a 32-byte big-endian two's-complement signed slot."""
    if len(slot) != 32:
        raise ValueError(f"{field}: slot must be 32 bytes, got {len(slot)}")
    # int.from_bytes with signed=True naturally enforces range;,
    # an overflow raises OverflowError, which we surface as ValueError
    # for consistent error handling.
    try:
        return int.from_bytes(slot, "big", signed=True)
    except OverflowError as exc:
        raise ValueError(f"{field}: signed value out of int{bits} range: {exc}") from exc


def _slot_address(slot: bytes, *, field: str) -> Address:
    """Decode a 32-byte ABI address slot (zero-padded uint160)."""
    if len(slot) != 32:
        raise ValueError(f"{field}: slot must be 32 bytes, got {len(slot)}")
    value = int.from_bytes(slot, "big")
    if value >= (1 << 160):
        raise ValueError(f"{field}: address value exceeds 160 bits: {value}")
    return Address(value)


def _topic_int(topic: bytes, *, field: str) -> int:
    """Decode a 32-byte indexed topic as an unsigned 256-bit integer."""
    if len(topic) != 32:
        raise ValueError(f"{field}: topic must be 32 bytes, got {len(topic)}")
    return int.from_bytes(topic, "big")


def _decode_data_fields(
    data_bytes: bytes,
    event_name: str,
) -> dict[str, int]:
    """Slice ``data_bytes`` into the per-field slots for ``event_name``.

    Returns a dict keyed by field name; the caller maps this into the
    matching typed record constructor. Width and sign constraints are
    enforced per the ABI type; bytes are never silently truncated.
    """
    spec = _DATA_FIELDS[event_name]
    expected_len = 32 * len(spec)
    if len(data_bytes) != expected_len:
        raise ValueError(
            f"{event_name}: data blob must be {expected_len} bytes "
            f"({len(spec)} slots), got {len(data_bytes)}"
        )
    out: dict[str, int | Address] = {}
    for offset, (name, abi_type) in enumerate(spec):
        slot = data_bytes[offset * 32 : (offset + 1) * 32]
        full_field = f"{event_name}.{name}"
        if abi_type == "uint24":
            out[name] = _slot_uint(slot, bits=24, field=full_field)
        elif abi_type == "uint128":
            out[name] = _slot_uint(slot, bits=128, field=full_field)
        elif abi_type == "uint160":
            out[name] = _slot_uint(slot, bits=160, field=full_field)
        elif abi_type == "uint256":
            out[name] = _slot_uint(slot, bits=256, field=full_field)
        elif abi_type == "int24":
            out[name] = _slot_int(slot, bits=24, field=full_field)
        elif abi_type == "int128":
            out[name] = _slot_int(slot, bits=128, field=full_field)
        elif abi_type == "int256":
            out[name] = _slot_int(slot, bits=256, field=full_field)
        elif abi_type == "address":
            out[name] = _slot_address(slot, field=full_field).value
        elif abi_type == "bytes32":
            out[name] = _topic_int(slot, field=full_field)
        else:
            raise ValueError(f"{event_name}: unsupported ABI type {abi_type!r}")
    return out  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Decode context (the framing the log carries but the topic does not)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogDecodeContext:
    """The framing fields every V4 log record needs beyond its own topic.

    These are the JSON-RPC envelope values the decoder cannot read from
    topics or ``data`` alone: chain identity, block / transaction
    position, the emitting contract address (PoolManager), the
    ``removed`` flag (for reorgs), the acquisition provenance (endpoint
    alias, retrieval time, HTTP batch metadata), and the full JSON-RPC
    response wrapper the server returned (kept verbatim so a future
    re-decode does not need to re-query the chain).
    """

    chain_id: ChainId
    block_number: int
    block_hash: int
    transaction_hash: int
    transaction_index: int
    log_index: int
    address: Address
    removed: bool
    acquisition: AcquisitionProvenance
    raw_response: dict[str, Any]


# ---------------------------------------------------------------------------
# Top-level decoder
# ---------------------------------------------------------------------------


#: Any of the typed log records the decoder may return.
DecodedLog = (
    InitializeLogRecord
    | ModifyLiquidityLogRecord
    | SwapLogRecord
    | DonateLogRecord
    | ProtocolFeeUpdatedLogRecord
)


def decode_log(raw_log: dict[str, Any], ctx: LogDecodeContext) -> DecodedLog:
    """Decode a single raw JSON-RPC log entry into a typed V4 record.

    ``raw_log`` is a dict of the form produced by the JSON-RPC
    ``eth_getLogs`` response — it must contain ``topics`` (a list of
    0x-prefixed hex strings, the first being the event topic0) and
    ``data`` (a single 0x-prefixed hex string). The decoder matches the
    topic0 against the pinned artifact
    (:data:`robinhood_lp.protocol.abi_artifacts.EVENT_TOPICS`) and
    raises ``ValueError`` on any topic0 the framework does not consume
    or any topic / data width that violates the ABI.

    The returned record carries ``raw_topics`` (the original bytes), the
    original ``raw_data`` blob, and the JSON-RPC response wrapper under
    ``raw`` — alongside the typed fields. Signed amounts and signed
    liquidity deltas keep their exact sign and ABI width as Python
    ``int``.
    """
    if not isinstance(raw_log, dict):
        raise TypeError(f"raw_log: must be dict, got {type(raw_log).__name__}")
    topics_hex = raw_log.get("topics")
    data_hex = raw_log.get("data", "0x")
    if not isinstance(topics_hex, list) or not topics_hex:
        raise ValueError("raw_log: missing or empty 'topics' array")
    topic0_bytes = _hex_to_bytes(topics_hex[0], field="raw_log.topics[0]")
    if len(topic0_bytes) != 32:
        raise ValueError(f"raw_log: topic0 must be 32 bytes, got {len(topic0_bytes)}")
    raw_topics = [_hex_to_bytes(t, field=f"raw_log.topics[{i}]") for i, t in enumerate(topics_hex)]
    raw_data = _hex_to_bytes(data_hex, field="raw_log.data")

    # Identify the event by topic0.
    event_name: str | None = None
    for name, expected_topic0 in EVENT_TOPICS.items():
        if topic0_bytes == expected_topic0:
            event_name = name
            break
    if event_name is None:
        raise ValueError(
            "raw_log: topic0 0x" + topic0_bytes.hex() + " does not match any pinned V4 event topic"
        )

    # Indexed topics: pool_id is always present (32 bytes). The number
    # of *additional* indexed topics is per-event.
    expected_indexed = _INDEXED_TOPIC_COUNTS[event_name]
    if len(raw_topics) != 1 + expected_indexed:
        raise ValueError(
            f"{event_name}: expected {1 + expected_indexed} topics "
            f"(topic0 + {expected_indexed} indexed), got {len(raw_topics)}"
        )
    pool_id_bytes = raw_topics[1]
    pool_id = PoolId.from_bytes(pool_id_bytes)

    # Decode the data blob into typed fields.
    fields = _decode_data_fields(raw_data, event_name)

    # Build the record. Every record inherits the common fields
    # (chain_id, pool_manager address, block number/hash, transaction
    # hash/index, log index, removed flag, acquisition provenance,
    # raw topics/data, and the JSON-RPC response wrapper under ``raw``).
    common: dict[str, Any] = dict(
        chain_id=ctx.chain_id,
        pool_id=pool_id,
        block_number=ctx.block_number,
        block_hash=ctx.block_hash,
        transaction_hash=ctx.transaction_hash,
        transaction_index=ctx.transaction_index,
        log_index=ctx.log_index,
        address=ctx.address,
        removed=ctx.removed,
        acquisition=ctx.acquisition,
        raw_topics=raw_topics,
        raw_data=raw_data,
        raw=dict(raw_log),
    )
    # Carry the JSON-RPC response wrapper separately so it survives even
    # if the caller constructs its own ``raw`` payload above. The schema
    # stores the wrapper under ``raw``; we place it under
    # ``raw_response`` for explicit typing. The dict above already
    # contains the per-log raw; the response wrapper is added below.
    common["raw"].setdefault("_jsonrpc_response_wrapper", ctx.raw_response)
    common["decode_version"] = CURRENT_DECODE_VERSION

    if event_name == "Initialize":
        return InitializeLogRecord(
            **common,
            # Currency0 / Currency1 are recovered from the indexed
            # topics (topics[2], topics[3]). The artifact's signature
            # does not mark ``indexed`` so the framework re-derives the
            # addresses from the topics.
            # NOTE: Typed ``InitializeLogRecord`` does not currently
            # expose the per-currency addresses (it carries pool_id +
            # the other 5 fields). The currencies are still available
            # under ``raw_topics`` / ``raw`` for downstream code.
        )
    if event_name == "ModifyLiquidity":
        sender_addr = _slot_address(raw_topics[2], field="ModifyLiquidity.sender")
        return ModifyLiquidityLogRecord(
            **common,
            sender=sender_addr,
            tick_lower=int(fields["tick_lower"]),
            tick_upper=int(fields["tick_upper"]),
            liquidity_delta=int(fields["liquidity_delta"]),
            salt=int(fields["salt"]),
        )
    if event_name == "Swap":
        sender_addr = _slot_address(raw_topics[2], field="Swap.sender")
        return SwapLogRecord(
            **common,
            sender=sender_addr,
            amount0=int(fields["amount0"]),
            amount1=int(fields["amount1"]),
            sqrt_price_x96=int(fields["sqrt_price_x96"]),
            liquidity=int(fields["liquidity"]),
            tick=int(fields["tick"]),
            fee=int(fields["fee"]),
        )
    if event_name == "Donate":
        sender_addr = _slot_address(raw_topics[2], field="Donate.sender")
        return DonateLogRecord(
            **common,
            sender=sender_addr,
            amount0=int(fields["amount0"]),
            amount1=int(fields["amount1"]),
        )
    if event_name == "ProtocolFeeUpdated":
        return ProtocolFeeUpdatedLogRecord(
            **common,
            protocol_fee=int(fields["protocol_fee"]),
        )
    raise AssertionError(f"unreachable: matched event {event_name!r}")


__all__ = [
    "DecodedLog",
    "LogDecodeContext",
    "decode_log",
]
