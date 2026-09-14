"""Tests for the storage schemas (T030).

Covers the T030 acceptance matrix:
- raw JSON -> normalized -> canonical serialization round trip
  loses no source bytes (every field in `raw` survives);
- compatibility/migration: unknown fields round-trip via
  ``unknown_fields``;
- integer-valued fields are stored as int (no float);
- token metadata is never required (each record is constructible
  without symbol/name/decimals).

All records are constructed with raw JSON-RPC style data and
verified to round-trip byte-exactly through ``canonical_bytes``.
"""

from __future__ import annotations

from dataclasses import asdict

from robinhood_lp.protocol import Address, ChainId, Currency, PoolId, PoolKey
from robinhood_lp.storage.schema import (
    CURRENT_DECODE_VERSION,
    CURRENT_SCHEMA_VERSION,
    BlockContext,
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    ReceiptContext,
    SwapLogRecord,
    TransactionContext,
    canonical_bytes,
    from_canonical_bytes,
)

CHAIN = ChainId(4663)


def _pk_bytes() -> PoolKey:
    pk = PoolKey(
        currency0=Currency.from_int(0x10),
        currency1=Currency.from_int(0x20),
        fee=3000,
        tick_spacing=60,
        hooks=Address.zero(),
    )
    return pk


def _pool_id_bytes() -> PoolId:
    return _pk_bytes().to_pool_id()


# ---------------------------------------------------------------------------
# Block / Transaction / Receipt contexts
# ---------------------------------------------------------------------------


def test_block_context_round_trip() -> None:
    raw = {
        "mixHash": "0x" + "ab" * 32,
        "nonce": "0x0000000000000042",
        "sha3Uncles": "0x" + "cd" * 32,
        "size": "0x21c",
        "transactionsRoot": "0x" + "ef" * 32,
        "stateRoot": "0x" + "12" * 32,
        "receiptsRoot": "0x" + "34" * 32,
        "logsBloom": "0x" + "00" * 256,
        "difficulty": "0x0",
        "totalDifficulty": "0x0",
        "extraData": "0x00",
        "transactions": ["0x" + "ab" * 32],
        "uncles": [],
        "baseFeePerGas": "0x5d21dba00",
    }
    record = BlockContext(
        chain_id=CHAIN,
        block_number=21_000_000,
        block_hash=0xAABBCCDDEEFF00112233445566778899AABBCCDDEEFF0011223344556677889,
        parent_hash=0x112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEFF0,
        timestamp=1_700_000_000,
        miner=Address.from_hex("0x" + "ab" * 20),
        gas_used=21_000,
        gas_limit=30_000_000,
        base_fee_per_gas=25_000_000_000,
        raw=raw,
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    # The canonical form records every typed field plus the entire
    # raw JSON. The round-trip preserves every raw key byte-for-byte.
    assert parsed["raw"] == raw
    # The schema_version and decode_version are embedded so a future
    # decoder can branch on them.
    assert parsed["__schema_version__"] == CURRENT_SCHEMA_VERSION
    assert parsed["__decode_version__"] == CURRENT_DECODE_VERSION
    assert parsed["__class__"] == "BlockContext"
    assert parsed["chain_id"] == CHAIN.value
    assert parsed["block_number"] == 21_000_000
    # Integers are stored as int, never as float.
    assert isinstance(parsed["block_number"], int)
    assert isinstance(parsed["timestamp"], int)


def test_block_context_accepts_optional_base_fee() -> None:
    record = BlockContext(
        chain_id=CHAIN,
        block_number=1,
        block_hash=1,
        parent_hash=2,
        timestamp=0,
        miner=Address.zero(),
        gas_used=0,
        gas_limit=30_000_000,
        base_fee_per_gas=None,  # pre-EIP-1559 chain
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["base_fee_per_gas"] is None


def test_transaction_context_round_trip() -> None:
    raw = {
        "v": "0x1b",
        "r": "0x" + "ab" * 32,
        "s": "0x" + "cd" * 32,
        "type": "0x2",
        "blockTimestamp": "0x6192e09a",
        "transactionHash": "0x" + "ee" * 32,
    }
    record = TransactionContext(
        chain_id=CHAIN,
        tx_hash=0xDEADBEEFCAFEBABE,
        block_hash=0xAB,
        block_number=100,
        transaction_index=2,
        from_address=Address.from_hex("0x" + "11" * 20),
        to_address=Address.from_hex("0x" + "22" * 20),
        value=10**18,
        input=bytes.fromhex("aabbccdd"),
        nonce=7,
        raw=raw,
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["raw"] == raw
    assert parsed["tx_hash"] == 0xDEADBEEFCAFEBABE
    assert parsed["nonce"] == 7
    assert parsed["__schema_version__"] == CURRENT_SCHEMA_VERSION
    assert parsed["__decode_version__"] == CURRENT_DECODE_VERSION


def test_transaction_context_with_no_to_address() -> None:
    """Contract-creation transactions have no ``to``."""
    record = TransactionContext(
        chain_id=CHAIN,
        tx_hash=1,
        block_hash=2,
        block_number=1,
        transaction_index=0,
        from_address=Address.from_hex("0x" + "11" * 20),
        to_address=None,
        value=0,
        input=b"",
        nonce=0,
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["to_address"] is None


def test_receipt_context_round_trip() -> None:
    record = ReceiptContext(
        chain_id=CHAIN,
        tx_hash=0xDEAD,
        block_hash=0xBEEF,
        block_number=42,
        transaction_index=3,
        from_address=Address.from_hex("0x" + "11" * 20),
        to_address=Address.from_hex("0x" + "22" * 20),
        contract_address=Address.from_hex("0x" + "33" * 20),
        gas_used=21_000,
        cumulative_gas_used=21_000,
        status=1,
        logs_bloom=0xAABBCC * (1 << 200),
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["status"] == 1
    assert parsed["logs_bloom"] == 0xAABBCC * (1 << 200)


# ---------------------------------------------------------------------------
# V4 event records
# ---------------------------------------------------------------------------


def test_initialize_log_record_round_trip() -> None:
    pk = _pk_bytes()
    pid = pk.to_pool_id()
    raw_topics = [
        b"\x3f\xd5\x53\xdb" + b"\x00" * 28,  # truncated topic0
        pid.to_bytes(),
        pk.currency0.to_address().to_bytes(),
        pk.currency1.to_address().to_bytes(),
    ]
    raw_data = (
        (3000).to_bytes(32, "big") + (60).to_bytes(32, "big", signed=True) + (0).to_bytes(32, "big")
    )
    record = InitializeLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        log_index=0,
        address=Address.from_hex("0x" + "44" * 20),
        raw_topics=raw_topics,
        raw_data=raw_data,
        raw={"removed": True},
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["pool_id"] == pid.value
    assert parsed["address"] == Address.from_hex("0x" + "44" * 20).value
    # Raw bytes survive as hex strings.
    assert parsed["raw_topics"] == [t.hex() for t in raw_topics]
    assert parsed["raw_data"] == raw_data.hex()
    assert parsed["raw"] == {"removed": True}
    # The schema_version and decode_version are embedded.
    assert parsed["__schema_version__"] == CURRENT_SCHEMA_VERSION
    assert parsed["__decode_version__"] == CURRENT_DECODE_VERSION


def test_modify_liquidity_log_record_round_trip() -> None:
    record = ModifyLiquidityLogRecord(
        chain_id=CHAIN,
        pool_id=_pool_id_bytes(),
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        log_index=1,
        address=Address.from_hex("0x" + "44" * 20),
        sender=Address.from_hex("0x" + "11" * 20),
        tick_lower=-100,
        tick_upper=100,
        liquidity_delta=10**18,
        salt=0xDEADBEEF,
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["tick_lower"] == -100
    assert parsed["tick_upper"] == 100
    assert parsed["liquidity_delta"] == 10**18
    assert parsed["salt"] == 0xDEADBEEF


def test_swap_log_record_round_trip() -> None:
    record = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=_pool_id_bytes(),
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        log_index=2,
        address=Address.from_hex("0x" + "44" * 20),
        sender=Address.from_hex("0x" + "11" * 20),
        amount0=-(10**6),
        amount1=20**6,
        sqrt_price_x96=79228162514264337593543950336,  # price 1:1
        liquidity=10**18,
        tick=0,
        fee=3000,
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["amount0"] == -(10**6)
    assert parsed["amount1"] == 20**6
    assert parsed["sqrt_price_x96"] == 79228162514264337593543950336
    assert parsed["fee"] == 3000
    # Signed deltas preserved (no float coercion).
    assert isinstance(parsed["amount0"], int)
    assert isinstance(parsed["amount1"], int)


def test_donate_log_record_round_trip() -> None:
    record = DonateLogRecord(
        chain_id=CHAIN,
        pool_id=_pool_id_bytes(),
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        log_index=3,
        address=Address.from_hex("0x" + "44" * 20),
        sender=Address.from_hex("0x" + "11" * 20),
        amount0=10**6,
        amount1=20**6,
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["amount0"] == 10**6
    assert parsed["amount1"] == 20**6


# ---------------------------------------------------------------------------
# Round-trip invariants
# ---------------------------------------------------------------------------


def test_round_trip_preserves_every_field_for_every_record_type() -> None:
    """For each record class, every typed field plus the raw and
    unknown_fields dicts survive a round-trip. The forward-compat
    unknown_fields mechanism is part of T030 acceptance."""
    unknown = {"new_v5_field": "preserved", "v5_array": [1, 2, 3]}

    samples: list[tuple[type, dict[str, object]]] = [
        (
            BlockContext,
            dict(
                chain_id=CHAIN,
                block_number=1,
                block_hash=2,
                parent_hash=3,
                timestamp=4,
                miner=Address.zero(),
                gas_used=5,
                gas_limit=6,
                base_fee_per_gas=7,
                raw={"foo": "bar"},
                unknown_fields=unknown,
            ),
        ),
        (
            TransactionContext,
            dict(
                chain_id=CHAIN,
                tx_hash=1,
                block_hash=2,
                block_number=3,
                transaction_index=0,
                from_address=Address.zero(),
                to_address=None,
                value=0,
                input=b"",
                nonce=0,
                raw={"foo": "bar"},
                unknown_fields=unknown,
            ),
        ),
        (
            ReceiptContext,
            dict(
                chain_id=CHAIN,
                tx_hash=1,
                block_hash=2,
                block_number=3,
                transaction_index=0,
                from_address=Address.zero(),
                to_address=None,
                contract_address=None,
                gas_used=0,
                cumulative_gas_used=0,
                status=1,
                logs_bloom=None,
                raw={"foo": "bar"},
                unknown_fields=unknown,
            ),
        ),
        (
            InitializeLogRecord,
            dict(
                chain_id=CHAIN,
                pool_id=_pool_id_bytes(),
                block_number=1,
                block_hash=2,
                transaction_hash=3,
                log_index=0,
                address=Address.zero(),
                raw_topics=[b"\x00" * 32],
                raw_data=b"\x00" * 96,
                raw={"foo": "bar"},
                unknown_fields=unknown,
            ),
        ),
        (
            ModifyLiquidityLogRecord,
            dict(
                chain_id=CHAIN,
                pool_id=_pool_id_bytes(),
                block_number=1,
                block_hash=2,
                transaction_hash=3,
                log_index=0,
                address=Address.zero(),
                sender=Address.zero(),
                tick_lower=-100,
                tick_upper=100,
                liquidity_delta=10**18,
                salt=0,
                raw={"foo": "bar"},
                unknown_fields=unknown,
            ),
        ),
        (
            SwapLogRecord,
            dict(
                chain_id=CHAIN,
                pool_id=_pool_id_bytes(),
                block_number=1,
                block_hash=2,
                transaction_hash=3,
                log_index=0,
                address=Address.zero(),
                sender=Address.zero(),
                amount0=-1,
                amount1=1,
                sqrt_price_x96=1,
                liquidity=1,
                tick=0,
                fee=3000,
                raw={"foo": "bar"},
                unknown_fields=unknown,
            ),
        ),
        (
            DonateLogRecord,
            dict(
                chain_id=CHAIN,
                pool_id=_pool_id_bytes(),
                block_number=1,
                block_hash=2,
                transaction_hash=3,
                log_index=0,
                address=Address.zero(),
                sender=Address.zero(),
                amount0=1,
                amount1=1,
                raw={"foo": "bar"},
                unknown_fields=unknown,
            ),
        ),
    ]

    for cls, kwargs in samples:
        record = cls(**kwargs)
        blob = canonical_bytes(record)
        parsed = from_canonical_bytes(blob)
        # Every typed field round-trips with the same value. Bytes
        # become hex strings; dataclass value objects (ChainId, PoolId,
        # Address) are flattened to their ``.value`` int in the
        # canonical form, so asdict(record)[k] for those fields returns
        # a one-key dict and we extract ``["value"]`` for comparison.
        original = asdict(record)
        for k, v in original.items():
            if k == "schema_version":
                continue
            expected: object
            if isinstance(v, dict) and set(v.keys()) == {"value"} and isinstance(v["value"], int):
                expected = v["value"]
            elif isinstance(v, bytes):
                expected = v.hex()
            elif isinstance(v, list):
                expected = [x.hex() if isinstance(x, bytes) else x for x in v]
            else:
                expected = v
            assert parsed[k] == expected, (
                f"{cls.__name__}.{k}: round-trip mismatch: {parsed[k]!r} != {expected!r}"
            )
        assert parsed["raw"] == kwargs["raw"]
        assert parsed["unknown_fields"] == unknown


def test_records_have_default_provenance() -> None:
    """Records have a default ingestion_time and decode_version."""
    record = BlockContext(
        chain_id=CHAIN,
        block_number=1,
        block_hash=2,
        parent_hash=3,
        timestamp=4,
        miner=Address.zero(),
        gas_used=5,
        gas_limit=6,
        base_fee_per_gas=7,
    )
    assert record.decode_version == CURRENT_DECODE_VERSION
    assert record.schema_version == CURRENT_SCHEMA_VERSION
    assert record.ingestion_time.endswith("Z") or "+" in record.ingestion_time
    assert record.source_endpoint == ""


def test_records_carry_no_float() -> None:
    """T030 must-not: 'store integers as float'.

    The canonical form must represent every int-typed field as int
    in JSON (not 1.0 or 1e0).
    """
    record = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=_pool_id_bytes(),
        block_number=1,
        block_hash=2,
        transaction_hash=3,
        log_index=0,
        address=Address.zero(),
        sender=Address.zero(),
        amount0=10**18,
        amount1=10**18,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
    )
    blob = canonical_bytes(record).decode("utf-8")
    # JSON numbers like ``1`` are not preceded by a decimal point.
    assert "1e" not in blob
    assert ".0" not in blob


def test_token_metadata_is_not_required_for_event_records() -> None:
    """T030 must-not: 'do not make token metadata required'.

    Each event record is constructible without symbol/name/decimals;
    the framework defers metadata to T022.
    """
    pk = _pool_id_bytes()
    record = InitializeLogRecord(
        chain_id=CHAIN,
        pool_id=pk,
        block_number=1,
        block_hash=2,
        transaction_hash=3,
        log_index=0,
        address=Address.zero(),
    )
    assert record.raw == {}
    assert record.unknown_fields == {}


def test_canonical_bytes_are_stable() -> None:
    """Two records with identical fields produce identical bytes."""
    common: dict[str, object] = dict(
        chain_id=CHAIN,
        pool_id=_pool_id_bytes(),
        block_number=1,
        block_hash=2,
        transaction_hash=3,
        log_index=0,
        address=Address.zero(),
        sender=Address.zero(),
        tick_lower=0,
        tick_upper=0,
        liquidity_delta=0,
        salt=0,
    )
    a = ModifyLiquidityLogRecord(**common)  # type: ignore[arg-type]
    b = ModifyLiquidityLogRecord(**common)  # type: ignore[arg-type]
    assert canonical_bytes(a) == canonical_bytes(b)


def test_canonical_bytes_change_when_a_field_changes() -> None:
    base: dict[str, object] = dict(
        chain_id=CHAIN,
        pool_id=_pool_id_bytes(),
        block_number=1,
        block_hash=2,
        transaction_hash=3,
        log_index=0,
        address=Address.zero(),
        sender=Address.zero(),
        tick_lower=0,
        tick_upper=0,
        liquidity_delta=0,
        salt=0,
    )
    a = ModifyLiquidityLogRecord(**base)  # type: ignore[arg-type]
    b = ModifyLiquidityLogRecord(**{**base, "tick_lower": -1})  # type: ignore[arg-type]
    assert canonical_bytes(a) != canonical_bytes(b)
