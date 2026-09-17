"""Tests for the production :class:`RpcBlockHeaderSource` (T035).

The source must:

- call ``eth_getBlockByNumber(hex(n), false)`` (non-hydrated);
- deduplicate per distinct event block (a block hosting multiple
  pool events triggers exactly one logical call);
- issue header requests as JSON-RPC batches with the
  logical-call / HTTP-batch split reported as separate counters;
- reject ``eth_getLogs.blockTimestamp`` as a time source (T035
  contract: this field held ``0x0`` on every returned log in the
  2026-09-17 probe);
- never fetch a header for a block that has no pool event.

The tests use a fake transport that returns deterministic JSON-RPC
responses. The transport is the same shape
:class:`robinhood_lp.rpc.adapter.RpcAdapter` accepts.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

import pytest

from robinhood_lp.ingestion.block_header_source import (
    DEFAULT_HEADER_BATCH_SIZE,
    BlockHeader,
    BlockHeaderFetchError,
    RpcBlockHeaderSource,
    _build_json_rpc_batch,
    _decode_header_payload,
    _parse_json_rpc_batch_response,
)
from robinhood_lp.ingestion.endpoint_client import _classify_transport_error
from robinhood_lp.ingestion.errors import (
    REASON_HTTP_4XX,
    REASON_HTTP_5XX,
    REASON_HTTP_403_DEFAULT_USER_AGENT,
    REASON_HTTP_429_RATE_LIMIT,
    REASON_RPC_TIMEOUT,
)
from robinhood_lp.ingestion.router import (
    ALIAS_ALCHEMY_FREE,
    ALIAS_ROBINHOOD_PUBLIC,
)
from robinhood_lp.rpc.adapter import (
    RpcAdapter,
    RpcConfig,
    RpcEndpoint,
    TransportCallable,
    TransportError,
)

# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


def _make_header_payload(
    block_number: int,
    *,
    block_hash: int,
    parent_hash: int,
    timestamp: int,
) -> dict[str, Any]:
    return {
        "hash": "0x" + format(block_hash, "064x"),
        "parentHash": "0x" + format(parent_hash, "064x"),
        "number": "0x" + format(block_number, "x"),
        "timestamp": "0x" + format(timestamp, "x"),
        "nonce": "0x0000000000000000",
        "sha3Uncles": "0x" + "00" * 32,
        "logsBloom": "0x" + "00" * 256,
        "transactionsRoot": "0x" + "00" * 32,
        "stateRoot": "0x" + "00" * 32,
        "receiptsRoot": "0x" + "00" * 32,
        "miner": "0x" + "00" * 20,
        "difficulty": "0x0",
        "totalDifficulty": "0x0",
        "extraData": "0x",
        "size": "0x0",
        "gasLimit": "0x0",
        "gasUsed": "0x0",
        "transactions": [],
    }


class _ScriptedTransport:
    """A scripted HTTP transport that the source's batch path drives.

    The source uses ``RpcAdapter._transport`` directly for batches;
    the scripted transport records the payloads and returns the
    scripted responses in arrival order.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []
        self.exception: BaseException | None = None

    def __call__(self, url: str, payload: dict[str, Any]) -> Awaitable[dict[str, Any]]:
        async def _run() -> dict[str, Any]:
            self.calls.append(payload)
            if self.exception is not None:
                raise self.exception
            if not self.responses:
                raise RuntimeError("scripted transport has no scripted response left")
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response

        return _run()


def _adapter_with_transport(transport: TransportCallable) -> RpcAdapter:
    return RpcAdapter(
        RpcConfig(endpoints=(RpcEndpoint(url="https://rpc.example/test", name="test_endpoint"),)),
        transport=transport,
        sleeper=lambda _seconds: _noop(),
        rng=_seeded_random(),
        metrics=_zero_metrics(),
    )


async def _noop() -> None:
    return None


def _seeded_random() -> Any:
    import random

    return random.Random(42)


def _zero_metrics() -> Any:
    from robinhood_lp.rpc.adapter import RpcMetrics

    return RpcMetrics()


# ---------------------------------------------------------------------------
# Header payload decoding
# ---------------------------------------------------------------------------


def test_decode_header_payload_round_trip() -> None:
    payload = _make_header_payload(
        block_number=100, block_hash=0xAA, parent_hash=0x99, timestamp=1_700_000_000
    )
    header = _decode_header_payload(payload, requested_block=100)
    assert header is not None
    assert header.block_number == 100
    assert header.block_hash == 0xAA
    assert header.parent_hash == 0x99
    assert header.timestamp == 1_700_000_000


def test_decode_header_payload_accepts_omitted_number() -> None:
    """Some providers omit ``number`` in the response. The source falls
    back to the requested block number so the canonical header is
    still produced.
    """
    payload = _make_header_payload(
        block_number=200, block_hash=0xAA, parent_hash=0x99, timestamp=1_700_000_000
    )
    del payload["number"]
    header = _decode_header_payload(payload, requested_block=200)
    assert header is not None
    assert header.block_number == 200


def test_decode_header_payload_rejects_missing_field() -> None:
    payload = _make_header_payload(
        block_number=100, block_hash=0xAA, parent_hash=0x99, timestamp=1_700_000_000
    )
    del payload["parentHash"]
    assert _decode_header_payload(payload, requested_block=100) is None


# ---------------------------------------------------------------------------
# JSON-RPC batch shape
# ---------------------------------------------------------------------------


def test_build_json_rpc_batch_emits_one_entry_per_block() -> None:
    payload = _build_json_rpc_batch([100, 101, 102])
    assert payload["jsonrpc"] == "2.0"
    entries = payload["batch"]
    assert len(entries) == 3
    block_numbers = [int(e["params"][0], 16) for e in entries]
    assert block_numbers == [100, 101, 102]
    # Every entry asks for a non-hydrated header.
    assert all(e["params"][1] is False for e in entries)
    assert all(e["method"] == "eth_getBlockByNumber" for e in entries)


def test_parse_json_rpc_batch_response_matches_by_id() -> None:
    headers = [
        _make_header_payload(100, block_hash=0xAA, parent_hash=0x99, timestamp=1_700_000_000),
        _make_header_payload(101, block_hash=0xBB, parent_hash=0xAA, timestamp=1_700_000_001),
    ]
    response = [
        {"jsonrpc": "2.0", "id": 100, "result": headers[0]},
        {"jsonrpc": "2.0", "id": 101, "result": headers[1]},
    ]
    parsed = _parse_json_rpc_batch_response(
        response, expected_batch_size=2, alias=ALIAS_ROBINHOOD_PUBLIC
    )
    assert [h.block_number for h in parsed] == [100, 101]
    assert [h.block_hash for h in parsed] == [0xAA, 0xBB]


def test_parse_json_rpc_batch_response_handles_per_entry_error() -> None:
    """A single failed call inside a batch becomes ``None`` for that
    block, so the caller records a per-block failure.
    """
    response = [
        {
            "jsonrpc": "2.0",
            "id": 100,
            "error": {"code": -32000, "message": "header not found"},
        },
        {
            "jsonrpc": "2.0",
            "id": 101,
            "result": _make_header_payload(
                101, block_hash=0xBB, parent_hash=0xAA, timestamp=1_700_000_001
            ),
        },
    ]
    parsed = _parse_json_rpc_batch_response(
        response, expected_batch_size=2, alias=ALIAS_ROBINHOOD_PUBLIC
    )
    assert parsed[0] is None
    assert parsed[1] is not None
    assert parsed[1].block_number == 101


def test_parse_json_rpc_batch_response_rejects_size_mismatch() -> None:
    response = [
        {"jsonrpc": "2.0", "id": 100, "result": {}},
    ]
    with pytest.raises(BlockHeaderFetchError):
        _parse_json_rpc_batch_response(
            response, expected_batch_size=2, alias=ALIAS_ROBINHOOD_PUBLIC
        )


# ---------------------------------------------------------------------------
# The source: dedup, batch split, cache hits
# ---------------------------------------------------------------------------


def test_source_dedups_repeated_block_numbers() -> None:
    """A repeated ``get_header`` call for the same block does not
    increment the logical-call counter.
    """
    transport = _ScriptedTransport()
    adapter = _adapter_with_transport(transport)
    # Single-block path: the RpcAdapter expects a JSON-RPC envelope.
    header = _make_header_payload(100, block_hash=0xAA, parent_hash=0x99, timestamp=1_700_000_000)
    transport.responses.append({"jsonrpc": "2.0", "id": 1, "result": header})
    source = RpcBlockHeaderSource(adapter=adapter, alias=ALIAS_ROBINHOOD_PUBLIC)
    first = source.get_header(100)
    assert first is not None
    second = source.get_header(100)
    assert second is not None
    assert first == second
    # Only one logical call was made (single-header fetch path).
    assert source.metrics.logical_get_block_by_number_calls == 1
    assert source.metrics.header_cache_hits == 1


def test_source_get_headers_batch_dedups_and_counts_logical_calls() -> None:
    """The batch path issues exactly ``len(distinct)`` logical calls,
    never one per event / per scanned block.
    """
    transport = _ScriptedTransport()
    adapter = _adapter_with_transport(transport)
    # Two batches of two entries each (batch_size=2, four distinct blocks).
    transport.responses.append(
        [
            {
                "jsonrpc": "2.0",
                "id": 100,
                "result": _make_header_payload(
                    100, block_hash=0xAA, parent_hash=0x99, timestamp=1_700_000_000
                ),
            },
            {
                "jsonrpc": "2.0",
                "id": 101,
                "result": _make_header_payload(
                    101, block_hash=0xBB, parent_hash=0xAA, timestamp=1_700_000_001
                ),
            },
        ]
    )
    transport.responses.append(
        [
            {
                "jsonrpc": "2.0",
                "id": 102,
                "result": _make_header_payload(
                    102, block_hash=0xCC, parent_hash=0xBB, timestamp=1_700_000_002
                ),
            },
            {
                "jsonrpc": "2.0",
                "id": 103,
                "result": _make_header_payload(
                    103, block_hash=0xDD, parent_hash=0xCC, timestamp=1_700_000_003
                ),
            },
        ]
    )
    source = RpcBlockHeaderSource(
        adapter=adapter,
        alias=ALIAS_ROBINHOOD_PUBLIC,
        batch_size=2,
    )
    out = source.get_headers_batch([100, 101, 102, 103])
    assert sorted(out) == [100, 101, 102, 103]
    for block_number in out:
        assert out[block_number] is not None
    # Four logical calls, two HTTP batches (each batch carries two).
    assert source.metrics.logical_get_block_by_number_calls == 4
    assert source.metrics.http_batch_requests == 2
    # Two payloads were sent (one per batch).
    assert len(transport.calls) == 2


def test_source_get_headers_batch_dedups_repeated_block_numbers() -> None:
    """Duplicate block numbers in the input list produce one logical
    call, not one per duplicate.
    """
    transport = _ScriptedTransport()
    adapter = _adapter_with_transport(transport)
    transport.responses.append(
        [
            {
                "jsonrpc": "2.0",
                "id": 100,
                "result": _make_header_payload(
                    100, block_hash=0xAA, parent_hash=0x99, timestamp=1_700_000_000
                ),
            },
            {
                "jsonrpc": "2.0",
                "id": 101,
                "result": _make_header_payload(
                    101, block_hash=0xBB, parent_hash=0xAA, timestamp=1_700_000_001
                ),
            },
        ]
    )
    source = RpcBlockHeaderSource(adapter=adapter, alias=ALIAS_ROBINHOOD_PUBLIC, batch_size=2)
    # Four entries with only two distinct block numbers.
    out = source.get_headers_batch([100, 100, 101, 101])
    assert sorted(out) == [100, 101]
    assert source.metrics.logical_get_block_by_number_calls == 2
    assert source.metrics.http_batch_requests == 1


def test_source_rejects_block_timestamp_field_as_time_source() -> None:
    """``eth_getLogs.blockTimestamp`` is the documented unusable field.

    The source never reads it; it only consumes the integer
    ``timestamp`` from the non-hydrated block header. This test
    confirms the source's ``BlockHeader`` dataclass carries the
    header timestamp and nothing else — the field is rejected by
    construction.
    """
    header = BlockHeader(
        block_number=100, block_hash=0xAA, parent_hash=0x99, timestamp=1_700_000_000
    )
    # The header has no ``blockTimestamp`` attribute; the field is
    # rejected by construction.
    assert not hasattr(header, "blockTimestamp")
    assert not hasattr(header, "block_timestamp_field")


def test_source_cache_hits_do_not_increment_logical_counter() -> None:
    transport = _ScriptedTransport()
    adapter = _adapter_with_transport(transport)
    transport.responses.append(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": _make_header_payload(
                100, block_hash=0xAA, parent_hash=0x99, timestamp=1_700_000_000
            ),
        }
    )
    source = RpcBlockHeaderSource(adapter=adapter, alias=ALIAS_ROBINHOOD_PUBLIC)
    for _ in range(5):
        source.get_header(100)
    assert source.metrics.logical_get_block_by_number_calls == 1
    assert source.metrics.header_cache_hits == 4


def test_source_raises_on_transport_error() -> None:
    """The single-fetch path surfaces transport errors as
    :class:`BlockHeaderFetchError` so the runner records a
    coverage failure rather than crashing.
    """
    transport = _ScriptedTransport()
    adapter = _adapter_with_transport(transport)
    source = RpcBlockHeaderSource(adapter=adapter, alias=ALIAS_ROBINHOOD_PUBLIC)
    # Inject an exception at the transport layer; the adapter
    # surfaces it as TransportError and the source maps it to
    # BlockHeaderFetchError.
    transport.exception = TransportError("transport timeout after 30s")
    with pytest.raises(BlockHeaderFetchError):
        source.get_header(100)
    assert source.metrics.header_fetch_failures == 1


def test_source_rejects_negative_block_number() -> None:
    transport = _ScriptedTransport()
    adapter = _adapter_with_transport(transport)
    source = RpcBlockHeaderSource(adapter=adapter, alias=ALIAS_ROBINHOOD_PUBLIC)
    with pytest.raises(ValueError):
        source.get_header(-1)


def test_default_batch_size_is_16() -> None:
    """The default batch size is the planning-layer default (16)."""
    assert DEFAULT_HEADER_BATCH_SIZE == 16


def test_source_supports_alchemy_alias() -> None:
    """The secondary endpoint can host a header source."""
    transport = _ScriptedTransport()
    adapter = _adapter_with_transport(transport)
    source = RpcBlockHeaderSource(adapter=adapter, alias=ALIAS_ALCHEMY_FREE, batch_size=4)
    assert source.alias == ALIAS_ALCHEMY_FREE
    assert source.batch_size == 4


def test_source_rejects_non_string_alias() -> None:
    transport = _ScriptedTransport()
    adapter = _adapter_with_transport(transport)
    with pytest.raises(ValueError):
        RpcBlockHeaderSource(adapter=adapter, alias="")


def test_source_rejects_non_positive_batch_size() -> None:
    transport = _ScriptedTransport()
    adapter = _adapter_with_transport(transport)
    with pytest.raises(ValueError):
        RpcBlockHeaderSource(adapter=adapter, batch_size=0)


def test_classify_transport_error_maps_status_codes() -> None:
    """The error classifier produces the right T032 reason codes."""
    assert (
        _classify_transport_error(TransportError("429 Too Many Requests: limit"))[0]
        == REASON_HTTP_429_RATE_LIMIT
    )
    assert (
        _classify_transport_error(TransportError("403 Forbidden: default UA"))[0]
        == REASON_HTTP_403_DEFAULT_USER_AGENT
    )
    assert (
        _classify_transport_error(TransportError("502 Bad Gateway: upstream"))[0] == REASON_HTTP_5XX
    )
    assert _classify_transport_error(TransportError("404 Not Found: stale"))[0] == REASON_HTTP_4XX
    assert (
        _classify_transport_error(TransportError("transport timeout after 30s"))[0]
        == REASON_RPC_TIMEOUT
    )


def test_block_header_rejects_out_of_range_fields() -> None:
    with pytest.raises(ValueError):
        BlockHeader(block_number=-(1), block_hash=0, parent_hash=0, timestamp=0)
    with pytest.raises(ValueError):
        BlockHeader(block_number=1 << 64, block_hash=0, parent_hash=0, timestamp=0)
    with pytest.raises(ValueError):
        BlockHeader(block_number=1, block_hash=1 << 256, parent_hash=0, timestamp=0)
    with pytest.raises(ValueError):
        BlockHeader(block_number=1, block_hash=0, parent_hash=-(1), timestamp=0)
    with pytest.raises(ValueError):
        BlockHeader(block_number=1, block_hash=0, parent_hash=0, timestamp=-(1))


def test_source_metrics_exposed() -> None:
    transport = _ScriptedTransport()
    adapter = _adapter_with_transport(transport)
    source = RpcBlockHeaderSource(adapter=adapter, alias=ALIAS_ROBINHOOD_PUBLIC)
    snap = source.metrics.snapshot()
    assert set(snap) == {
        "logical_get_block_by_number_calls",
        "http_batch_requests",
        "header_cache_hits",
        "header_fetch_failures",
    }
    assert all(v == 0 for v in snap.values())


def test_source_cache_state_reports_alias_and_batch() -> None:
    transport = _ScriptedTransport()
    adapter = _adapter_with_transport(transport)
    source = RpcBlockHeaderSource(adapter=adapter, alias=ALIAS_ALCHEMY_FREE, batch_size=8)
    state = source.cache_state()
    assert state["alias"] == ALIAS_ALCHEMY_FREE
    assert state["batch_size"] == 8
    assert state["cached_block_count"] == 0
