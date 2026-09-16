"""V4 Initialize event log decoding (T022).

The PoolManager emits ``Initialize(PoolId indexed id, Currency indexed
currency0, Currency indexed currency1, uint24 fee, int24 tickSpacing,
IHooks hooks, uint160 sqrtPriceX96, int24 tick)`` exactly once per
pool, at the block where the pool is first modified. The scanner
decodes that log topic by topic.

The 5-arg signature description above is the 8-field Initialize
event pinned in ``docs/implement/protocol-artifacts/v4-core-e50237c.json``
(T021 redo, byte-compared against a Foundry SelectorOracle).

T022 acceptance (todo/README.md T022):

- decoded ``PoolKey`` is bit-exactly what the canonical V4 reference
  produces;
- decoded ``PoolId`` matches ``PoolKey.to_pool_id()``;
- native currency (zero address) is preserved without alteration;
- dynamic-fee sentinel (``0x800000``) is preserved;
- nonzero hook addresses are preserved;
- a single log entry with all fields is sufficient to reconstruct
  the registry row.

T022 must-not:

- query a factory (V4 is a singleton; the framework emits no
  ``NewPool`` events to scrape, only ``Initialize``);
- discover by token symbol (currency0/currency1 are addresses, not
  symbols);
- drop pools whose metadata call fails (T022 emits the pool record
  with ``token_metadata_failed=True`` and continues; the metadata
  reader is a separate module).
"""

from __future__ import annotations

from dataclasses import dataclass

from robinhood_lp.protocol import (
    EVENT_TOPICS,
    MAX_SQRT_PRICE_X96,
    MAX_TICK,
    MIN_SQRT_PRICE_X96,
    MIN_TICK,
    Address,
    Currency,
    PoolId,
    PoolKey,
)

#: topics[0] is the event signature, topics[1] is the indexed PoolId.
#: topics[2] / topics[3] are the indexed Currency0 / Currency1
#: addresses. Data carries fee, tickSpacing, hooks, sqrtPriceX96, tick.
INITIALIZE_TOPIC0: bytes = EVENT_TOPICS["Initialize"]

#: The 4-byte function selector for ``getHook(address)`` is not used
#: here; the Initialize event already carries the hooks address.

#: The Initialize event payload is ABI-encoded as:
#:   (uint24 fee, int24 tickSpacing, address hooks, uint160 sqrtPriceX96, int24 tick)
#: five 32-byte slots.
INITIALIZE_DATA_SLOTS: int = 5


class InitializeDecodeError(ValueError):
    """The log could not be decoded as an Initialize event."""


@dataclass(frozen=True, slots=True)
class DecodedInitialize:
    """The output of :func:`decode_initialize_log`.

    ``pool_id`` is what V4 emits as the indexed topic and must equal
    ``pool_key.to_pool_id()``; the constructor below enforces the
    equality as a safety check.
    """

    pool_key: PoolKey
    pool_id: PoolId
    #: The block number that emitted this log, set by the scanner.
    block_number: int | None = None
    #: The transaction hash that emitted this log, set by the scanner.
    tx_hash: str | None = None
    #: The log index within the transaction, set by the scanner.
    log_index: int | None = None
    #: The pool's initial ``sqrtPriceX96`` as emitted in the
    #: ``Initialize`` event's non-indexed data. Validated to fit the V4
    #: ``[MIN_SQRT_PRICE_X96, MAX_SQRT_PRICE_X96)`` domain; non-zero
    #: and non-``uint160`` values are rejected by the decoder.
    sqrt_price_x96: int | None = None
    #: The pool's initial ``tick`` as emitted in the ``Initialize``
    #: event's non-indexed data. Validated to fit the V4
    #: ``[MIN_TICK, MAX_TICK]`` domain; values outside that range are
    #: rejected by the decoder. Signed ``int24``.
    initial_tick: int | None = None


def _decode_address(topic_or_word: bytes) -> Address:
    """Convert a 32-byte log topic (or 32-byte data slot) to Address.

    Log topics are the 20-byte address left-padded with zeros; data
    slots for ``address`` follow the same convention. We accept the
    full 32-byte slot and also the unpadded 20-byte form (some test
    fixtures and convenience callers supply the shorter form).
    """
    if len(topic_or_word) == 32 or len(topic_or_word) == 20:
        value = int.from_bytes(topic_or_word, "big")
    elif len(topic_or_word) < 20:
        # Left-pad to 20 bytes.
        value = int.from_bytes(b"\x00" * (20 - len(topic_or_word)) + topic_or_word, "big")
    else:
        raise InitializeDecodeError(
            f"address slot must be 20 or 32 bytes, got {len(topic_or_word)}"
        )
    if value >= (1 << 160):
        raise InitializeDecodeError(f"slot value {value:#x} does not fit in uint160")
    return Address(value)


def _decode_uint24(word: bytes) -> int:
    if len(word) != 32:
        raise InitializeDecodeError(f"expected 32-byte slot for fee, got {len(word)} bytes")
    value = int.from_bytes(word, "big")
    if value >= (1 << 24):
        raise InitializeDecodeError(f"fee slot {value:#x} does not fit in uint24")
    return value


def _decode_int24(word: bytes) -> int:
    if len(word) != 32:
        raise InitializeDecodeError(f"expected 32-byte slot for tickSpacing, got {len(word)} bytes")
    value = int.from_bytes(word, "big", signed=True)
    if value < -(1 << 23) or value >= (1 << 23):
        raise InitializeDecodeError(f"tickSpacing slot {value} does not fit in int24")
    return value


def _decode_uint160(word: bytes) -> int:
    """Decode a 32-byte slot as V4 ``sqrtPriceX96`` (``uint160``).

    The V4 domain is ``[MIN_SQRT_PRICE_X96, MAX_SQRT_PRICE_X96)``:
    zero is not a valid pool price, and ``MAX_SQRT_PRICE_X96`` would
    invert back to the minimum tick with no headroom. We keep this
    check purely integer — no float conversions per ADR-009.
    """
    if len(word) != 32:
        raise InitializeDecodeError(
            f"expected 32-byte slot for sqrtPriceX96, got {len(word)} bytes"
        )
    value = int.from_bytes(word, "big")
    if value >= (1 << 160):
        raise InitializeDecodeError(f"sqrtPriceX96 slot {value:#x} does not fit in uint160")
    if value == 0:
        raise InitializeDecodeError("sqrtPriceX96 slot is zero, which is not a valid pool price")
    if value < MIN_SQRT_PRICE_X96 or value >= MAX_SQRT_PRICE_X96:
        raise InitializeDecodeError(
            f"sqrtPriceX96 slot {value} outside V4 domain "
            f"[{MIN_SQRT_PRICE_X96}, {MAX_SQRT_PRICE_X96})"
        )
    return value


def decode_initialize_log(
    topics: list[bytes],
    data: bytes,
) -> DecodedInitialize:
    """Decode one ``Initialize`` event log.

    The ``topics`` list must have at least 4 entries (event signature
    + 3 indexed fields). The ``data`` field is 5 × 32 = 160 bytes
    long; the value of the pool_id topic must equal
    ``keccak256(abi.encode(pool_key))``.

    The two trailing non-indexed data slots — ``uint160 sqrtPriceX96``
    and ``int24 tick`` — are decoded and surfaced via
    ``DecodedInitialize.sqrt_price_x96`` / ``initial_tick``. They do
    **not** participate in PoolId derivation; ``PoolId.to_pool_id()``
    continues to hash only the 5-slot ``PoolKey``.
    """
    if len(topics) < 4:
        raise InitializeDecodeError(
            f"Initialize event has at least 4 topics (sig + 3 indexed), got {len(topics)}"
        )
    if topics[0] != INITIALIZE_TOPIC0:
        raise InitializeDecodeError(
            f"topic0 {topics[0].hex()} is not the Initialize signature ({INITIALIZE_TOPIC0.hex()})"
        )
    pool_id = PoolId.from_bytes(topics[1])
    currency0 = Currency.from_address(_decode_address(topics[2]))
    currency1 = Currency.from_address(_decode_address(topics[3]))
    if len(data) != 32 * INITIALIZE_DATA_SLOTS:
        raise InitializeDecodeError(
            f"Initialize data must be {32 * INITIALIZE_DATA_SLOTS} bytes, got {len(data)}"
        )
    fee = _decode_uint24(data[0:32])
    tick_spacing = _decode_int24(data[32:64])
    hooks = _decode_address(data[64:96])
    sqrt_price_x96 = _decode_uint160(data[96:128])
    initial_tick = _decode_int24(data[128:160])
    # V4 also constrains the initial tick to [MIN_TICK, MAX_TICK] in
    # the same way sqrtPriceX96 is bounded by its V4 domain; enforce
    # it here so an obviously-out-of-range tick (e.g. an int24 value
    # outside the V4 tick window) is rejected before reaching the
    # registry, mirroring the V4 PoolManager revert.
    if initial_tick < MIN_TICK or initial_tick > MAX_TICK:
        raise InitializeDecodeError(
            f"initial tick {initial_tick} outside V4 domain [{MIN_TICK}, {MAX_TICK}]"
        )

    pool_key = PoolKey(
        currency0=currency0,
        currency1=currency1,
        fee=fee,
        tick_spacing=tick_spacing,
        hooks=hooks,
    )

    # T022 acceptance: derived PoolId must equal the indexed PoolId.
    derived = pool_key.to_pool_id()
    if derived.value != pool_id.value:
        raise InitializeDecodeError(
            f"derived PoolId {derived.to_hex()} != indexed PoolId "
            f"{pool_id.to_hex()} (the event payload is internally "
            f"inconsistent)"
        )

    return DecodedInitialize(
        pool_key=pool_key,
        pool_id=pool_id,
        sqrt_price_x96=sqrt_price_x96,
        initial_tick=initial_tick,
    )


__all__ = [
    "INITIALIZE_DATA_SLOTS",
    "INITIALIZE_TOPIC0",
    "DecodedInitialize",
    "InitializeDecodeError",
    "decode_initialize_log",
]
