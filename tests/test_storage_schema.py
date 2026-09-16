"""Tests for the storage schemas (T030).

Covers the T030 acceptance matrix:
- raw JSON -> normalized -> canonical serialization round trip
  loses no source bytes (every field in `raw` survives);
- compatibility/migration: unknown fields round-trip via
  ``unknown_fields``;
- integer-valued fields are stored as int (no float);
- token metadata is never required (each record is constructible
  without symbol/name/decimals);
- v1 -> v2 migration preserves v1 records and backfills the new
  acquisition / removed / transaction_index fields with safe
  defaults;
- the same chain event fetched from different endpoints produces
  the same normalized content hash while the two acquisition
  envelopes are retained separately;
- same-height fork logs remain distinct because the EventKey
  identity (T011) keys on ``block_hash``;
- deterministic ordering by ``(block_number, transaction_index,
  log_index)``;
- ``ProtocolFeeUpdated(bytes32,uint24)`` round-trips with exact
  uint24 semantics;
- the endpoint alias is structurally prevented from carrying
  credential-bearing URL content.

All records are constructed with raw JSON-RPC style data and
verified to round-trip byte-exactly through ``canonical_bytes``.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

import pytest

from robinhood_lp.protocol import (
    Address,
    ChainId,
    Currency,
    EventKey,
    PoolId,
    PoolKey,
)
from robinhood_lp.protocol.abi_artifacts import EVENT_TOPICS
from robinhood_lp.protocol.events import BlockRef, TransactionRef
from robinhood_lp.storage.decode_log import LogDecodeContext, decode_log
from robinhood_lp.storage.schema import (
    CURRENT_DECODE_VERSION,
    CURRENT_SCHEMA_VERSION,
    AcquisitionProvenance,
    BlockContext,
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    ProtocolFeeUpdatedLogRecord,
    ReceiptContext,
    SwapLogRecord,
    TransactionContext,
    canonical_bytes,
    from_canonical_bytes,
    migrate_to_current,
    normalized_content_hash,
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
        transaction_index=4,
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
        transaction_index=5,
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
        transaction_index=6,
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
        transaction_index=7,
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
                transaction_index=0,
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
                transaction_index=0,
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
                transaction_index=0,
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
                transaction_index=0,
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
        transaction_index=0,
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
        transaction_index=0,
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
        transaction_index=0,
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
        transaction_index=0,
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


# ---------------------------------------------------------------------------
# T030 amendment additions
# ---------------------------------------------------------------------------


def test_protocol_fee_updated_log_record_round_trip() -> None:
    """The inherited ``ProtocolFeeUpdated(bytes32,uint24)`` event
    (IProtocolFees) is required by ADR-010 §"Required data boundary"
    so the framework records LP-owned protocol fees separately from
    the combined swap fee carried by the Swap event.
    """
    pid = _pool_id_bytes()
    raw_topics = [b"\x00" * 32, pid.to_bytes()]
    raw_data = (0x000ABC).to_bytes(32, "big")
    record = ProtocolFeeUpdatedLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=200,
        block_hash=0xCC,
        transaction_hash=0xDD,
        transaction_index=8,
        log_index=4,
        address=Address.from_hex("0x" + "44" * 20),
        protocol_fee=0x000ABC,
        raw_topics=raw_topics,
        raw_data=raw_data,
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["protocol_fee"] == 0x000ABC
    assert isinstance(parsed["protocol_fee"], int)
    # Exact uint24 width enforcement.
    with pytest.raises(ValueError, match="protocol_fee"):
        ProtocolFeeUpdatedLogRecord(
            chain_id=CHAIN,
            pool_id=pid,
            block_number=200,
            block_hash=0xCC,
            transaction_hash=0xDD,
            transaction_index=8,
            log_index=4,
            address=Address.from_hex("0x" + "44" * 20),
            protocol_fee=1 << 24,  # exceeds 24 bits
        )
    with pytest.raises(ValueError, match="protocol_fee"):
        ProtocolFeeUpdatedLogRecord(
            chain_id=CHAIN,
            pool_id=pid,
            block_number=200,
            block_hash=0xCC,
            transaction_hash=0xDD,
            transaction_index=8,
            log_index=4,
            address=Address.from_hex("0x" + "44" * 20),
            protocol_fee=-1,
        )


def test_acquisition_provenance_validates_alias() -> None:
    """T030 must-not: 'Request provenance never contains a credential-
    bearing URL'. An alias containing ``://``, ``@``, ``?``, path
    separators, or whitespace is rejected.
    """
    with pytest.raises(ValueError, match="alias"):
        AcquisitionProvenance(endpoint_alias="https://x.example.com")
    with pytest.raises(ValueError, match="alias"):
        AcquisitionProvenance(endpoint_alias="alice@example.com")
    with pytest.raises(ValueError, match="alias"):
        AcquisitionProvenance(endpoint_alias="rpc?apiKey=secret")
    with pytest.raises(ValueError, match="alias"):
        AcquisitionProvenance(endpoint_alias="path/with/slashes")
    with pytest.raises(ValueError, match="alias"):
        AcquisitionProvenance(endpoint_alias="has space")
    # A short opaque token is accepted.
    ap = AcquisitionProvenance(
        endpoint_alias="robinhood_public",
        retrieval_time="2026-09-16T00:00:00+00:00",
        request_from_block=1_000_000,
        request_to_block=1_000_999,
        http_batch_size=10,
        http_batch_position=3,
        request_attempt=1,
    )
    assert ap.endpoint_alias == "robinhood_public"
    assert ap.request_from_block == 1_000_000
    assert ap.http_batch_position == 3


def test_acquisition_provenance_validates_request_interval_order() -> None:
    """The request interval must satisfy
    ``request_from_block <= request_to_block``."""
    with pytest.raises(ValueError, match="request_from_block"):
        AcquisitionProvenance(
            endpoint_alias="alchemy_free",
            request_from_block=100,
            request_to_block=99,
        )


def test_acquisition_provenance_validates_batch_position() -> None:
    """http_batch_position must be strictly less than http_batch_size."""
    with pytest.raises(ValueError, match="http_batch_position"):
        AcquisitionProvenance(
            endpoint_alias="alchemy_free",
            http_batch_size=3,
            http_batch_position=3,
        )


def test_event_key_identity_matches_t011() -> None:
    """The log record's ``event_key()`` returns a T011
    ``EventKey(chain_id, block_hash, tx_hash, log_index)``. Two
    records of the same chain event produce the same key regardless
    of acquisition metadata.
    """
    pid = _pool_id_bytes()
    addr = Address.from_hex("0x" + "44" * 20)
    sender = Address.from_hex("0x" + "11" * 20)
    record_a = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=7,
        address=addr,
        sender=sender,
        amount0=-1,
        amount1=1,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
        acquisition=AcquisitionProvenance(
            endpoint_alias="robinhood_public",
            retrieval_time="2026-09-16T00:00:00+00:00",
            request_from_block=1_000_000,
            request_to_block=1_000_999,
        ),
    )
    record_b = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=7,
        address=addr,
        sender=sender,
        amount0=-1,
        amount1=1,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
        acquisition=AcquisitionProvenance(
            endpoint_alias="alchemy_free",
            retrieval_time="2026-09-16T00:00:05+00:00",
            request_from_block=1_000_000,
            request_to_block=1_000_099,
            http_batch_size=10,
            http_batch_position=0,
        ),
    )
    assert record_a.event_key() == record_b.event_key()
    expected = EventKey(chain_id=CHAIN, block_hash=0xAA, tx_hash=0xBB, log_index=7)
    assert record_a.event_key() == expected


def test_same_chain_event_different_endpoints_have_same_normalized_content_hash() -> None:
    """T030 acceptance: 'Re-fetching identical logs from different
    endpoints/times produces the same normalized content hash while
    retaining both raw provenance observations'.
    """
    pid = _pool_id_bytes()
    addr = Address.from_hex("0x" + "44" * 20)
    sender = Address.from_hex("0x" + "11" * 20)
    record_a = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=7,
        address=addr,
        sender=sender,
        amount0=-1,
        amount1=1,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
        acquisition=AcquisitionProvenance(
            endpoint_alias="robinhood_public",
            retrieval_time="2026-09-16T00:00:00+00:00",
            request_from_block=1_000_000,
            request_to_block=1_000_999,
        ),
    )
    record_b = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=7,
        address=addr,
        sender=sender,
        amount0=-1,
        amount1=1,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
        acquisition=AcquisitionProvenance(
            endpoint_alias="alchemy_free",
            retrieval_time="2026-09-16T00:00:05+00:00",
            request_from_block=1_000_000,
            request_to_block=1_000_099,
            http_batch_size=10,
            http_batch_position=0,
        ),
    )
    assert normalized_content_hash(record_a) == normalized_content_hash(record_b)
    # But the canonical (full) bytes differ — both acquisition
    # envelopes are retained separately.
    assert canonical_bytes(record_a) != canonical_bytes(record_b)


def test_same_height_fork_logs_remain_distinct_by_block_hash() -> None:
    """T030 acceptance: 'same-height fork logs remain distinct by
    block hash'. Two records with the same block number, tx hash,
    and log index but different block hashes have different
    EventKey identities and different normalized content hashes.
    """
    pid = _pool_id_bytes()
    addr = Address.from_hex("0x" + "44" * 20)
    sender = Address.from_hex("0x" + "11" * 20)
    fork_a = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,  # fork A
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=0,
        address=addr,
        sender=sender,
        amount0=-1,
        amount1=1,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
    )
    fork_b = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAB,  # fork B (same height, different hash)
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=0,
        address=addr,
        sender=sender,
        amount0=-1,
        amount1=1,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
    )
    assert fork_a.event_key() != fork_b.event_key()
    assert normalized_content_hash(fork_a) != normalized_content_hash(fork_b)


def test_sort_key_is_block_number_then_transaction_index_then_log_index() -> None:
    """T030 acceptance: deterministic ordering by
    ``(block_number, transaction_index, log_index)``.
    """
    pid = _pool_id_bytes()
    addr = Address.from_hex("0x" + "44" * 20)
    sender = Address.from_hex("0x" + "11" * 20)
    records = [
        SwapLogRecord(
            chain_id=CHAIN,
            pool_id=pid,
            block_number=100,
            block_hash=0x10,
            transaction_hash=0xB0,
            transaction_index=5,
            log_index=3,
            address=addr,
            sender=sender,
            amount0=0,
            amount1=0,
            sqrt_price_x96=1,
            liquidity=1,
            tick=0,
            fee=3000,
        ),
        SwapLogRecord(
            chain_id=CHAIN,
            pool_id=pid,
            block_number=100,
            block_hash=0x10,
            transaction_hash=0xB0,
            transaction_index=5,
            log_index=1,
            address=addr,
            sender=sender,
            amount0=0,
            amount1=0,
            sqrt_price_x96=1,
            liquidity=1,
            tick=0,
            fee=3000,
        ),
        SwapLogRecord(
            chain_id=CHAIN,
            pool_id=pid,
            block_number=99,
            block_hash=0x10,
            transaction_hash=0xB0,
            transaction_index=99,
            log_index=99,
            address=addr,
            sender=sender,
            amount0=0,
            amount1=0,
            sqrt_price_x96=1,
            liquidity=1,
            tick=0,
            fee=3000,
        ),
        SwapLogRecord(
            chain_id=CHAIN,
            pool_id=pid,
            block_number=100,
            block_hash=0x10,
            transaction_hash=0xB0,
            transaction_index=4,
            log_index=99,
            address=addr,
            sender=sender,
            amount0=0,
            amount1=0,
            sqrt_price_x96=1,
            liquidity=1,
            tick=0,
            fee=3000,
        ),
    ]
    ordered = sorted(records, key=lambda r: r.sort_key())
    sort_keys = [r.sort_key() for r in ordered]
    assert sort_keys == [
        (99, 99, 99),
        (100, 4, 99),
        (100, 5, 1),
        (100, 5, 3),
    ]


def test_normalized_content_hash_is_stable() -> None:
    """Two records with identical normalized content but different
    acquisition provenance produce identical normalized content
    hashes.
    """
    pid = _pool_id_bytes()
    addr = Address.from_hex("0x" + "44" * 20)
    sender = Address.from_hex("0x" + "11" * 20)
    base = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=7,
        address=addr,
        sender=sender,
        amount0=-42,
        amount1=42,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
    )
    record_a = replace(
        base,
        acquisition=AcquisitionProvenance(
            endpoint_alias="robinhood_public",
            retrieval_time="2026-09-16T00:00:00+00:00",
        ),
    )
    record_b = replace(
        base,
        acquisition=AcquisitionProvenance(
            endpoint_alias="alchemy_free",
            retrieval_time="2026-09-16T01:23:45+00:00",
            request_from_block=100,
            request_to_block=199,
        ),
    )
    assert normalized_content_hash(record_a) == normalized_content_hash(record_b)
    # But the canonical bytes retain both provenance envelopes.
    assert canonical_bytes(record_a) != canonical_bytes(record_b)


def test_normalized_content_hash_changes_on_typed_field_change() -> None:
    """Changing any typed (normalized) field changes the normalized
    content hash. The hash is content-derived, not identity-derived;
    distinct events produce distinct hashes."""
    pid = _pool_id_bytes()
    addr = Address.from_hex("0x" + "44" * 20)
    sender = Address.from_hex("0x" + "11" * 20)
    record_a = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=7,
        address=addr,
        sender=sender,
        amount0=-1,
        amount1=1,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
    )
    record_b = replace(record_a, amount1=2)
    assert normalized_content_hash(record_a) != normalized_content_hash(record_b)


def test_signed_amounts_preserve_exact_sign_and_width() -> None:
    """T030 acceptance: signed amounts and liquidity deltas remain
    integers with exact sign/width semantics. The canonical form
    encodes the same signed int (not a two's-complement hex blob).
    """
    pid = _pool_id_bytes()
    addr = Address.from_hex("0x" + "44" * 20)
    sender = Address.from_hex("0x" + "11" * 20)
    record = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=7,
        address=addr,
        sender=sender,
        # Signed int128 extremes (the V4 Swap event declares
        # amount0/amount1 as int128). Use values that fit comfortably.
        amount0=-(2**127),
        amount1=(2**127) - 1,
        sqrt_price_x96=(1 << 160) - 1,
        liquidity=(1 << 128) - 1,
        tick=-(2**23),
        fee=(1 << 24) - 1,
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["amount0"] == -(2**127)
    assert parsed["amount1"] == (2**127) - 1
    assert parsed["sqrt_price_x96"] == (1 << 160) - 1
    assert parsed["liquidity"] == (1 << 128) - 1
    assert parsed["tick"] == -(2**23)
    assert parsed["fee"] == (1 << 24) - 1
    # No float coercion: every int field is a JSON integer literal.
    blob_text = blob.decode("utf-8")
    assert "1e" not in blob_text
    assert ".0" not in blob_text


def test_removed_flag_round_trips() -> None:
    """T030 acceptance: 'removed flag' is part of the common log
    record fields. A reorged log carries removed=True and survives
    round-trip.
    """
    pid = _pool_id_bytes()
    addr = Address.from_hex("0x" + "44" * 20)
    record = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=7,
        address=addr,
        sender=Address.zero(),
        amount0=1,
        amount1=1,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
        removed=True,
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["removed"] is True


def test_v1_blob_migrates_to_current() -> None:
    """T030 acceptance: 'compatibility/migration tests cover unknown
    fields and old versions'. A v1 canonical blob (pre-acquisition /
    pre-removed / pre-transaction_index) loads through
    ``migrate_to_current`` with safe defaults applied.
    """
    # Synthesise a v1-shaped canonical blob for a SwapLogRecord.
    v1_payload = {
        "__class__": "SwapLogRecord",
        "__schema_version__": 1,
        "__decode_version__": 1,
        "chain_id": CHAIN.value,
        "pool_id": _pool_id_bytes().value,
        "block_number": 100,
        "block_hash": 0xAA,
        "transaction_hash": 0xBB,
        "log_index": 2,
        "address": Address.from_hex("0x" + "44" * 20).value,
        "sender": Address.from_hex("0x" + "11" * 20).value,
        "amount0": -100,
        "amount1": 200,
        "sqrt_price_x96": 1,
        "liquidity": 1,
        "tick": 0,
        "fee": 3000,
        "decode_version": 1,
        "ingestion_time": "2026-09-15T00:00:00+00:00",
        "source_endpoint": "robinhood_public",
        "raw": {"foo": "bar"},
        "unknown_fields": {},
    }
    import json as _json

    v1_blob = _json.dumps(v1_payload, sort_keys=True).encode("utf-8")
    migrated = migrate_to_current(v1_blob)
    # The migration materialises the right dataclass with the v2
    # defaults applied.
    assert isinstance(migrated, SwapLogRecord)
    assert migrated.removed is False
    assert migrated.transaction_index == 0
    # The legacy source_endpoint is copied into the structured
    # acquisition envelope so downstream code can read either form.
    assert isinstance(migrated.acquisition, AcquisitionProvenance)
    assert migrated.acquisition.endpoint_alias == "robinhood_public"
    assert migrated.acquisition.retrieval_time == "2026-09-15T00:00:00+00:00"
    # The legacy fields are still present on the migrated record so a
    # v2 reader can still inspect them.
    assert migrated.source_endpoint == "robinhood_public"
    assert migrated.ingestion_time == "2026-09-15T00:00:00+00:00"
    # The record round-trips through canonical_bytes after migration.
    blob = canonical_bytes(migrated)
    roundtrip = migrate_to_current(blob)
    assert isinstance(roundtrip, SwapLogRecord)
    assert roundtrip.acquisition.endpoint_alias == "robinhood_public"
    assert roundtrip.raw == {"foo": "bar"}


def test_v1_migration_preserves_unknown_fields() -> None:
    """A v1 blob carrying future-shape unknown fields loads them
    through the migration so the dataclass can store them in
    ``unknown_fields``."""
    pid = _pool_id_bytes()
    addr = Address.from_hex("0x" + "44" * 20)
    sender = Address.from_hex("0x" + "11" * 20)
    record = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=7,
        address=addr,
        sender=sender,
        amount0=-1,
        amount1=1,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    # Inject a forward-compat field and re-emit as if it were a v2 blob.
    parsed["future_v3_field"] = {"k": "v"}
    parsed["future_array"] = [1, 2, 3]
    import json as _json

    fwd_blob = _json.dumps(parsed, sort_keys=True).encode("utf-8")
    migrated = migrate_to_current(fwd_blob)
    # The current dataclass must accept the migrated payload by
    # treating the future keys as ``unknown_fields`` extras; the
    # canonical form re-serialises them.
    assert isinstance(migrated, SwapLogRecord)
    assert migrated.unknown_fields["future_v3_field"] == {"k": "v"}
    assert migrated.unknown_fields["future_array"] == [1, 2, 3]


def test_migration_rejects_future_schema_version() -> None:
    """A blob whose ``__schema_version__`` exceeds the current build's
    version is rejected; loading it would silently misrepresent the
    record's shape."""
    import json as _json

    future_blob = _json.dumps(
        {
            "__class__": "SwapLogRecord",
            "__schema_version__": CURRENT_SCHEMA_VERSION + 1,
            "chain_id": 1,
        },
        sort_keys=True,
    ).encode("utf-8")
    with pytest.raises(ValueError, match="future schema version"):
        migrate_to_current(future_blob)


def test_acquisition_envelope_round_trips_and_is_excluded_from_normalized_hash() -> None:
    """The acquisition envelope round-trips through canonical_bytes
    and is excluded from ``normalized_content_hash`` (observational
    metadata, not chain-event content).
    """
    import hashlib
    import json as _json

    pid = _pool_id_bytes()
    addr = Address.from_hex("0x" + "44" * 20)
    sender = Address.from_hex("0x" + "11" * 20)
    acq = AcquisitionProvenance(
        endpoint_alias="alchemy_free",
        retrieval_time="2026-09-16T00:00:00+00:00",
        request_from_block=1_000_000,
        request_to_block=1_000_999,
        http_batch_size=10,
        http_batch_position=3,
        request_attempt=1,
    )
    record = SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pid,
        block_number=100,
        block_hash=0xAA,
        transaction_hash=0xBB,
        transaction_index=2,
        log_index=7,
        address=addr,
        sender=sender,
        amount0=-1,
        amount1=1,
        sqrt_price_x96=1,
        liquidity=1,
        tick=0,
        fee=3000,
        acquisition=acq,
    )
    blob = canonical_bytes(record)
    parsed = from_canonical_bytes(blob)
    assert parsed["acquisition"]["endpoint_alias"] == "alchemy_free"
    assert parsed["acquisition"]["request_from_block"] == 1_000_000
    assert parsed["acquisition"]["http_batch_position"] == 3
    # The normalized hash does not include the acquisition envelope.
    payload = dict(parsed)
    payload.pop("acquisition", None)
    payload.pop("ingestion_time", None)
    payload.pop("source_endpoint", None)
    payload.pop("raw", None)
    payload.pop("raw_topics", None)
    payload.pop("raw_data", None)
    payload.pop("unknown_fields", None)
    canonical = _json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert normalized_content_hash(record) == hashlib.sha256(canonical).digest()


# ---------------------------------------------------------------------------
# Full typed reconstruction from raw JSON-RPC bytes (T030 still-open)
# ---------------------------------------------------------------------------


CHAIN_FOR_DECODE = ChainId(4663)
_POOL_ID_INT = 0xAABBCCDDEEFF00112233445566778899AABBCCDDEEFF0011223344556677889
_POOL_ID_BYTES = (0).to_bytes(32, "big")  # placeholder; overwritten per-test


def _ctx_for_decode(
    *,
    endpoint_alias: str = "robinhood_public",
    raw_response: dict[str, Any] | None = None,
    log_index: int = 0,
    transaction_index: int = 0,
    block_number: int = 100,
    block_hash: int = 0xAA,
    transaction_hash: int = 0xBB,
    removed: bool = False,
) -> LogDecodeContext:
    """Build a LogDecodeContext with sensible defaults."""
    return LogDecodeContext(
        chain_id=CHAIN_FOR_DECODE,
        block_number=block_number,
        block_hash=block_hash,
        transaction_hash=transaction_hash,
        transaction_index=transaction_index,
        log_index=log_index,
        address=Address.from_hex("0x" + "44" * 20),
        removed=removed,
        acquisition=AcquisitionProvenance(
            endpoint_alias=endpoint_alias,
            retrieval_time="2026-09-16T00:00:00+00:00",
        ),
        raw_response=raw_response
        if raw_response is not None
        else {"jsonrpc": "2.0", "id": 1, "result": []},
    )


def _build_raw_log(
    topic0: bytes,
    extra_topics: list[bytes],
    data: bytes,
    *,
    removed: bool = False,
) -> dict[str, Any]:
    return {
        "address": "0x" + "44" * 20,
        "topics": ["0x" + t.hex() for t in [topic0, *extra_topics]],
        "data": "0x" + data.hex(),
        "blockNumber": "0x64",
        "transactionHash": "0x" + "bb" * 32,
        "transactionIndex": "0x0",
        "blockHash": "0x" + "aa" * 32,
        "logIndex": "0x0",
        "removed": removed,
    }


def test_decode_initialize_event() -> None:
    """T030 still-open: full typed reconstruction for the Initialize event.

    The decoder takes raw JSON-RPC topics + data and produces a typed
    ``InitializeLogRecord`` whose pool_id matches the indexed topic and
    whose raw bytes survive on the record (raw_topics / raw_data / raw).
    """
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    curr0 = int.from_bytes(b"\x22" * 20, "big").to_bytes(32, "big")
    curr1 = int.from_bytes(b"\x33" * 20, "big").to_bytes(32, "big")
    data = (
        (3000).to_bytes(32, "big")
        + (60).to_bytes(32, "big", signed=True)
        + (0).to_bytes(32, "big")
        + ((1 << 160) - 1).to_bytes(32, "big")
        + (0).to_bytes(32, "big", signed=True)
    )
    raw_log = _build_raw_log(EVENT_TOPICS["Initialize"], [pool_id_bytes, curr0, curr1], data)
    record = decode_log(raw_log, _ctx_for_decode())
    assert isinstance(record, InitializeLogRecord)
    assert record.pool_id.value == _POOL_ID_INT
    # Raw bytes survive verbatim — never overwritten by typed values.
    assert record.raw_topics[0] == EVENT_TOPICS["Initialize"]
    assert record.raw_topics[1] == pool_id_bytes
    assert record.raw_data == data
    assert record.raw["data"] == "0x" + data.hex()


def test_decode_modify_liquidity_event() -> None:
    """ModifyLiquidity: signed liquidity_delta must keep its sign and int width."""
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    sender_topic = int.from_bytes(b"\x11" * 20, "big").to_bytes(32, "big")
    data = (
        (-100).to_bytes(32, "big", signed=True)
        + (100).to_bytes(32, "big", signed=True)
        + (-(10**18)).to_bytes(32, "big", signed=True)
        + (0xDEADBEEF).to_bytes(32, "big")
    )
    raw_log = _build_raw_log(EVENT_TOPICS["ModifyLiquidity"], [pool_id_bytes, sender_topic], data)
    record = decode_log(raw_log, _ctx_for_decode())
    assert isinstance(record, ModifyLiquidityLogRecord)
    assert record.tick_lower == -100
    assert record.tick_upper == 100
    assert record.liquidity_delta == -(10**18)
    assert record.salt == 0xDEADBEEF
    assert record.sender.value == int.from_bytes(b"\x11" * 20, "big")
    # Signed int preserved as int (no float coercion).
    assert isinstance(record.liquidity_delta, int)


def test_decode_swap_event_preserves_signed_amounts() -> None:
    """Swap: signed amount0 / amount1 keep their sign and exact int width.

    Both extremes of int128 are accepted by the decoder; no value is
    truncated to a float.
    """
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    sender_topic = int.from_bytes(b"\x11" * 20, "big").to_bytes(32, "big")
    data = (
        (-(2**127)).to_bytes(32, "big", signed=True)
        + ((2**127) - 1).to_bytes(32, "big", signed=True)
        + ((1 << 160) - 1).to_bytes(32, "big")
        + ((1 << 128) - 1).to_bytes(32, "big")
        + (-(2**23)).to_bytes(32, "big", signed=True)
        + ((1 << 24) - 1).to_bytes(32, "big")
    )
    raw_log = _build_raw_log(EVENT_TOPICS["Swap"], [pool_id_bytes, sender_topic], data)
    record = decode_log(raw_log, _ctx_for_decode())
    assert isinstance(record, SwapLogRecord)
    assert record.amount0 == -(2**127)
    assert record.amount1 == (2**127) - 1
    assert record.sqrt_price_x96 == (1 << 160) - 1
    assert record.liquidity == (1 << 128) - 1
    assert record.tick == -(2**23)
    assert record.fee == (1 << 24) - 1
    assert isinstance(record.amount0, int)
    assert isinstance(record.amount1, int)


def test_decode_donate_event() -> None:
    """Donate: signed-or-uint amounts decode as int with exact width."""
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    sender_topic = int.from_bytes(b"\x11" * 20, "big").to_bytes(32, "big")
    data = (10**6).to_bytes(32, "big") + (20**6).to_bytes(32, "big")
    raw_log = _build_raw_log(EVENT_TOPICS["Donate"], [pool_id_bytes, sender_topic], data)
    record = decode_log(raw_log, _ctx_for_decode())
    assert isinstance(record, DonateLogRecord)
    assert record.amount0 == 10**6
    assert record.amount1 == 20**6


def test_decode_protocol_fee_updated_event() -> None:
    """ProtocolFeeUpdated(bytes32,uint24): exact uint24 width enforced."""
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    data = (0x000ABC).to_bytes(32, "big")
    raw_log = _build_raw_log(EVENT_TOPICS["ProtocolFeeUpdated"], [pool_id_bytes], data)
    record = decode_log(raw_log, _ctx_for_decode())
    assert isinstance(record, ProtocolFeeUpdatedLogRecord)
    assert record.protocol_fee == 0x000ABC
    assert isinstance(record.protocol_fee, int)


def test_decode_protocol_fee_updated_rejects_uint24_overflow() -> None:
    """The decoder rejects ``protocol_fee`` values that exceed 24 bits."""
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    data = (1 << 24).to_bytes(32, "big")  # exceeds uint24
    raw_log = _build_raw_log(EVENT_TOPICS["ProtocolFeeUpdated"], [pool_id_bytes], data)
    with pytest.raises(ValueError, match="protocol_fee"):
        decode_log(raw_log, _ctx_for_decode())


def test_decode_swap_rejects_fee_overflow() -> None:
    """A ``Swap`` event whose ``fee`` field exceeds uint24 is rejected."""
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    sender_topic = int.from_bytes(b"\x11" * 20, "big").to_bytes(32, "big")
    data = (
        (0).to_bytes(32, "big", signed=True)
        + (0).to_bytes(32, "big", signed=True)
        + (0).to_bytes(32, "big")
        + (0).to_bytes(32, "big")
        + (0).to_bytes(32, "big", signed=True)
        + (1 << 24).to_bytes(32, "big")  # exceeds uint24
    )
    raw_log = _build_raw_log(EVENT_TOPICS["Swap"], [pool_id_bytes, sender_topic], data)
    with pytest.raises(ValueError, match="Swap.fee"):
        decode_log(raw_log, _ctx_for_decode())


def test_decode_rejects_unknown_topic0() -> None:
    """A topic0 the framework does not consume is rejected."""
    raw_log = {
        "topics": ["0x" + ("00" * 32)],
        "data": "0x",
        "logIndex": "0x0",
    }
    with pytest.raises(ValueError, match="topic0"):
        decode_log(raw_log, _ctx_for_decode())


def test_decode_rejects_wrong_topic_count_for_event() -> None:
    """ModifyLiquidity expects 1 topic0 + 2 indexed topics. A blob with
    only 1 topic is rejected."""
    raw_log = {
        "topics": ["0x" + EVENT_TOPICS["ModifyLiquidity"].hex()],
        "data": "0x",
        "logIndex": "0x0",
    }
    with pytest.raises(ValueError, match="expected 3 topics"):
        decode_log(raw_log, _ctx_for_decode())


def test_decode_rejects_data_length_mismatch() -> None:
    """A ``Swap`` blob with the wrong number of data slots is rejected."""
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    sender_topic = int.from_bytes(b"\x11" * 20, "big").to_bytes(32, "big")
    bad_data = b"\x00" * 32  # 1 slot, Swap needs 6
    raw_log = _build_raw_log(EVENT_TOPICS["Swap"], [pool_id_bytes, sender_topic], bad_data)
    with pytest.raises(ValueError, match="data blob must be"):
        decode_log(raw_log, _ctx_for_decode())


def test_decode_preserves_raw_response_wrapper() -> None:
    """The original JSON-RPC response wrapper survives on the record
    alongside the typed fields and the raw per-log dict."""
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    sender_topic = int.from_bytes(b"\x11" * 20, "big").to_bytes(32, "big")
    data = (10**6).to_bytes(32, "big") + (20**6).to_bytes(32, "big")
    raw_log = _build_raw_log(EVENT_TOPICS["Donate"], [pool_id_bytes, sender_topic], data)
    wrapper = {"jsonrpc": "2.0", "id": 7, "result": [raw_log]}
    record = decode_log(raw_log, _ctx_for_decode(raw_response=wrapper))
    assert record.raw["_jsonrpc_response_wrapper"] == wrapper


def test_decode_propagates_removed_flag() -> None:
    """The decoder honours ``ctx.removed`` so reorged logs carry
    ``removed=True`` on the returned record."""
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    sender_topic = int.from_bytes(b"\x11" * 20, "big").to_bytes(32, "big")
    data = (10**6).to_bytes(32, "big") + (20**6).to_bytes(32, "big")
    raw_log = _build_raw_log(EVENT_TOPICS["Donate"], [pool_id_bytes, sender_topic], data)
    record = decode_log(raw_log, _ctx_for_decode(removed=True))
    assert record.removed is True


def test_decode_event_key_uses_t011_identity() -> None:
    """Decoded records expose the T011 ``EventKey`` via ``event_key()``.

    T030 still-open: 'dependency verification against T011 EventKey
    / BlockRef / TransactionRef / PoolIdentity surface'.
    """
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    sender_topic = int.from_bytes(b"\x11" * 20, "big").to_bytes(32, "big")
    data = (10**6).to_bytes(32, "big") + (20**6).to_bytes(32, "big")
    raw_log = _build_raw_log(EVENT_TOPICS["Donate"], [pool_id_bytes, sender_topic], data)
    record = decode_log(
        raw_log,
        _ctx_for_decode(block_hash=0xCAFE, transaction_hash=0xBABE, log_index=3),
    )
    expected = EventKey(chain_id=CHAIN_FOR_DECODE, block_hash=0xCAFE, tx_hash=0xBABE, log_index=3)
    assert record.event_key() == expected
    # The T011 ``block_ref()`` and ``transaction_ref()`` helpers also
    # surface on the EventKey.
    assert record.event_key().block_ref() == BlockRef(
        chain_id=CHAIN_FOR_DECODE, block_hash=0xCAFE, block_number=None
    )
    assert record.event_key().transaction_ref() == TransactionRef(
        chain_id=CHAIN_FOR_DECODE, tx_hash=0xBABE
    )


# ---------------------------------------------------------------------------
# Provenance: credential-bearing URL prohibition (T030 acceptance)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alias",
    [
        "https://x.example.com",
        "alice@example.com",
        "rpc?apiKey=secret",
        "path/with/slashes",
        "has space",
        "http://user:pass@host:1234/path",
        "rpc.example.com/?apiKey=secret",
    ],
)
def test_acquisition_alias_rejects_credential_bearing_url(alias: str) -> None:
    """T030 must-not / acceptance: a credential-bearing URL is never
    accepted as an endpoint alias. Both ``userinfo`` and secret query
    markers fail at the schema boundary."""
    with pytest.raises(ValueError):
        AcquisitionProvenance(endpoint_alias=alias)


def test_canonical_bytes_exclude_jsonrpc_response_wrapper_from_content_hash() -> None:
    """Two records of the same chain event fetched with different
    JSON-RPC response wrappers (different ``id`` values) still produce
    the same normalized content hash. The wrapper is observational and
    must not leak into content identity."""
    pool_id_bytes = _POOL_ID_INT.to_bytes(32, "big")
    sender_topic = int.from_bytes(b"\x11" * 20, "big").to_bytes(32, "big")
    data = (10**6).to_bytes(32, "big") + (20**6).to_bytes(32, "big")
    raw_log = _build_raw_log(EVENT_TOPICS["Donate"], [pool_id_bytes, sender_topic], data)
    wrapper_a = {"jsonrpc": "2.0", "id": 1, "result": [raw_log]}
    wrapper_b = {"jsonrpc": "2.0", "id": 2, "result": [raw_log]}
    record_a = decode_log(
        raw_log, _ctx_for_decode(endpoint_alias="robinhood_public", raw_response=wrapper_a)
    )
    record_b = decode_log(
        raw_log, _ctx_for_decode(endpoint_alias="alchemy_free", raw_response=wrapper_b)
    )
    assert normalized_content_hash(record_a) == normalized_content_hash(record_b)
    # But the canonical bytes retain both the wrapper and the alias.
    assert canonical_bytes(record_a) != canonical_bytes(record_b)
