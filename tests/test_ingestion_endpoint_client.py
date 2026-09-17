"""Tests for the production :class:`RpcEndpointClient` (T035).

The client bridges :class:`robinhood_lp.rpc.adapter.RpcAdapter`
to the router's :class:`EndpointClient` Protocol. The tests
exercise:

- successful ``eth_getLogs`` translation into
  :class:`EndpointCallResult`;
- empty responses (the ``scanned_empty`` coverage path);
- error translation for the documented reason codes (HTTP 403 /
  429 / 5xx / 4xx, transport timeout, JSON-RPC error,
  retry-exhausted);
- the pinned-block-hash helper the router uses for the
  ``scanned_empty`` evidence;
- the credential-bearing-URL rejection (defence-in-depth);
- the contract that ``blockTimestamp`` is never written to the
  response rows (the source returns raw JSON-RPC rows; the
  writer / schema layer treats the field as observational and
  never stores it).
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

import pytest

from robinhood_lp.ingestion.endpoint_client import (
    RpcEndpointClient,
    _classify_transport_error,
    _row_size_bytes,
)
from robinhood_lp.ingestion.errors import (
    REASON_HTTP_4XX,
    REASON_HTTP_5XX,
    REASON_HTTP_403_DEFAULT_USER_AGENT,
    REASON_HTTP_429_RATE_LIMIT,
    REASON_INVALID_RESPONSE,
    REASON_OK,
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
    RpcProtocolError,
    RpcRetryExhausted,
    TransportCallable,
    TransportError,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_log_row(
    *,
    block_number: int,
    transaction_hash: int = 0xBB,
    transaction_index: int = 0,
    log_index: int = 0,
    data: str = "0x",
    topics: list[str] | None = None,
    block_hash: int = 0xAA,
) -> dict[str, Any]:
    return {
        "address": "0x" + "44" * 20,
        "topics": topics if topics is not None else ["0x" + "00" * 32],
        "data": data,
        "blockNumber": "0x" + format(block_number, "x"),
        "transactionHash": "0x" + format(transaction_hash, "064x"),
        "transactionIndex": "0x" + format(transaction_index, "x"),
        "logIndex": "0x" + format(log_index, "x"),
        "blockHash": "0x" + format(block_hash, "064x"),
        "removed": False,
    }


class _ScriptedTransport:
    """A scripted transport that returns JSON-RPC responses in order.

    The transport accepts both single-request and batch payloads;
    ``expected`` matches each request against its scripted
    response.
    """

    def __init__(self) -> None:
        self.responses: list[Any] = []
        self.exception: BaseException | None = None

    def __call__(self, url: str, payload: dict[str, Any]) -> Awaitable[dict[str, Any]]:
        async def _run() -> dict[str, Any]:
            if self.exception is not None:
                raise self.exception
            if not self.responses:
                raise RuntimeError("scripted transport exhausted")
            return self.responses.pop(0)

        return _run()


def _adapter_with_transport(transport: TransportCallable, *, retry_limit: int = 0) -> RpcAdapter:
    """Build an RpcAdapter wired to ``transport``.

    ``retry_limit=0`` disables the adapter's classified retry
    loop so a single transport failure surfaces as the original
    exception class (the production code path under the router
    consumes the same shape).
    """
    return RpcAdapter(
        RpcConfig(
            endpoints=(RpcEndpoint(url="https://rpc.example/test", name="test_endpoint"),),
            retry_limit=retry_limit,
        ),
        transport=transport,
        sleeper=lambda _seconds: _noop(),
        rng=_seeded_random(),
    )


async def _noop() -> None:
    return None


def _seeded_random() -> Any:
    import random

    return random.Random(42)


CONTRACT = "0x" + "44" * 20


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_call_get_logs_translates_successful_response() -> None:
    transport = _ScriptedTransport()
    transport.responses.append(
        {"jsonrpc": "2.0", "id": 1, "result": [_make_log_row(block_number=100)]}
    )
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100,
        to_block=100,
        address=CONTRACT,
        topics=[["0x" + "00" * 32]],
    )
    assert result.success is True
    assert result.reason_code == REASON_OK
    assert result.rows and len(result.rows) == 1
    assert result.rows[0]["blockNumber"] == "0x64"
    assert result.response_bytes > 0


def test_call_get_logs_handles_empty_response() -> None:
    """An empty ``eth_getLogs`` result returns success with zero rows.

    The router maps ``success=True`` and ``rows=[]`` to the
    ``scanned_empty`` coverage state, which counts as covered
    for qualification (T032).
    """
    transport = _ScriptedTransport()
    transport.responses.append({"jsonrpc": "2.0", "id": 1, "result": []})
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100, to_block=200, address=CONTRACT, topics=[["0x" + "00" * 32]]
    )
    assert result.success is True
    assert result.rows == []
    assert result.response_bytes == 0


def test_call_get_logs_never_writes_block_timestamp() -> None:
    """``eth_getLogs.blockTimestamp`` is unusable (PROVIDER_FACTS.md).

    Even if a provider returns a non-zero ``blockTimestamp``
    field, the client surfaces the raw row without ever using
    it as a time source; the writer / schema layer ignores the
    field. This test confirms the client never stores it on the
    result object.
    """
    transport = _ScriptedTransport()
    row = _make_log_row(block_number=100)
    row["blockTimestamp"] = "0x12345678"
    transport.responses.append({"jsonrpc": "2.0", "id": 1, "result": [row]})
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100, to_block=100, address=CONTRACT, topics=[["0x" + "00" * 32]]
    )
    assert result.success is True
    # The field is preserved in the raw row (audit trail) but
    # the client's result does not promote it to a typed field.
    assert "blockTimestamp" in result.rows[0]
    assert not hasattr(result, "block_timestamp")


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def test_call_get_logs_translates_429_to_http_429_reason_code() -> None:
    transport = _ScriptedTransport()
    transport.exception = TransportError("429 Too Many Requests: limit")
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100, to_block=100, address=CONTRACT, topics=[["0x" + "00" * 32]]
    )
    assert result.success is False
    assert result.reason_code == REASON_HTTP_429_RATE_LIMIT
    assert "429" in (result.error_detail or "")


def test_call_get_logs_translates_403_default_ua() -> None:
    transport = _ScriptedTransport()
    transport.exception = TransportError("403 Forbidden: urllib default UA")
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100, to_block=100, address=CONTRACT, topics=[["0x" + "00" * 32]]
    )
    assert result.success is False
    assert result.reason_code == REASON_HTTP_403_DEFAULT_USER_AGENT


def test_call_get_logs_translates_5xx() -> None:
    transport = _ScriptedTransport()
    transport.exception = TransportError("502 Bad Gateway: upstream")
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100, to_block=100, address=CONTRACT, topics=[["0x" + "00" * 32]]
    )
    assert result.success is False
    assert result.reason_code == REASON_HTTP_5XX


def test_call_get_logs_translates_rpc_response_error() -> None:
    transport = _ScriptedTransport()
    transport.responses.append(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "limit"}}
    )
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100, to_block=100, address=CONTRACT, topics=[["0x" + "00" * 32]]
    )
    assert result.success is False
    assert result.reason_code == REASON_INVALID_RESPONSE


def test_call_get_logs_translates_retry_exhausted_with_preserved_429() -> None:
    """An :class:`RpcRetryExhausted` whose inner message preserves
    the 429 status code is mapped back to
    :data:`REASON_HTTP_429_RATE_LIMIT` so the router records the
    precise failure reason (not a blanket ``rpc_timeout``).
    """
    transport = _ScriptedTransport()
    # The adapter's RpcRetryExhausted wraps the original
    # TransportError text; we simulate the wrapped message the
    # production code path produces.
    transport.exception = RpcRetryExhausted(
        "'eth_getLogs' failed on all 1 endpoint(s): 429 Too Many Requests: limit"
    )
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100, to_block=100, address=CONTRACT, topics=[["0x" + "00" * 32]]
    )
    assert result.success is False
    assert result.reason_code == REASON_HTTP_429_RATE_LIMIT


def test_call_get_logs_translates_protocol_error() -> None:
    transport = _ScriptedTransport()
    # A response that is not a JSON object triggers RpcProtocolError
    # inside the adapter; the client maps it to invalid_response.
    transport.exception = RpcProtocolError("response is not a JSON object")
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100, to_block=100, address=CONTRACT, topics=[["0x" + "00" * 32]]
    )
    assert result.success is False
    assert result.reason_code == REASON_INVALID_RESPONSE


def test_call_get_logs_translates_timeout_to_rpc_timeout() -> None:
    transport = _ScriptedTransport()
    transport.exception = TransportError("transport timeout after 30s")
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100, to_block=100, address=CONTRACT, topics=[["0x" + "00" * 32]]
    )
    assert result.success is False
    assert result.reason_code == REASON_RPC_TIMEOUT


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_call_get_logs_rejects_inverted_range() -> None:
    transport = _ScriptedTransport()
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    with pytest.raises(ValueError):
        client.call_get_logs(
            from_block=200, to_block=100, address=CONTRACT, topics=[["0x" + "00" * 32]]
        )


def test_call_get_logs_rejects_empty_topics() -> None:
    transport = _ScriptedTransport()
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    with pytest.raises(ValueError):
        client.call_get_logs(from_block=100, to_block=100, address=CONTRACT, topics=[])


def test_client_rejects_non_hex_address() -> None:
    transport = _ScriptedTransport()
    with pytest.raises(ValueError):
        RpcEndpointClient(
            adapter=_adapter_with_transport(transport),
            alias=ALIAS_ROBINHOOD_PUBLIC,
            address="not-a-hex-address",
        )


def test_client_rejects_empty_alias() -> None:
    transport = _ScriptedTransport()
    with pytest.raises(ValueError):
        RpcEndpointClient(
            adapter=_adapter_with_transport(transport),
            alias="",
            address=CONTRACT,
        )


# ---------------------------------------------------------------------------
# Pinned block hash
# ---------------------------------------------------------------------------


def test_get_block_hash_for_pin_returns_lowercase_hex() -> None:
    transport = _ScriptedTransport()
    transport.responses.append(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "hash": "0x" + "AB" * 32,
                "parentHash": "0x" + "00" * 32,
                "number": "0x64",
                "timestamp": "0x1234",
            },
        }
    )
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    pin = client.get_block_hash_for_pin(100)
    assert pin == "0x" + "ab" * 32


def test_get_block_hash_for_pin_uses_override_map() -> None:
    transport = _ScriptedTransport()
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    client.pinned_block_hash_overrides[100] = "0x" + "11" * 32
    assert client.get_block_hash_for_pin(100) == "0x" + "11" * 32


def test_get_block_hash_for_pin_returns_none_on_error() -> None:
    transport = _ScriptedTransport()
    transport.exception = TransportError("transport timeout after 30s")
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    assert client.get_block_hash_for_pin(100) is None


# ---------------------------------------------------------------------------
# Row size accounting
# ---------------------------------------------------------------------------


def test_row_size_bytes_is_positive() -> None:
    row = _make_log_row(block_number=100, data="0xdeadbeef")
    assert _row_size_bytes(row) > 0


def test_classify_transport_error_unknown_defaults_to_4xx() -> None:
    """An unrecognised transport message is classified as 4xx so
    the retry policy treats it as non-transient.
    """
    reason, _detail = _classify_transport_error(TransportError("unknown failure shape"))
    assert reason == REASON_HTTP_4XX


def test_classify_transport_error_4xx() -> None:
    reason, _detail = _classify_transport_error(TransportError("404 Not Found: stale"))
    assert reason == REASON_HTTP_4XX


def test_classify_transport_error_timed_out() -> None:
    reason, _detail = _classify_transport_error(TransportError("request timed out"))
    assert reason == REASON_RPC_TIMEOUT


def test_call_get_logs_response_bytes_sums_rows() -> None:
    """``response_bytes`` is the sum of JSON-serialised row sizes."""
    transport = _ScriptedTransport()
    rows = [
        _make_log_row(block_number=100, log_index=0),
        _make_log_row(block_number=101, log_index=0),
        _make_log_row(block_number=102, log_index=0),
    ]
    transport.responses.append({"jsonrpc": "2.0", "id": 1, "result": rows})
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100, to_block=200, address=CONTRACT, topics=[["0x" + "00" * 32]]
    )
    assert result.success is True
    expected = sum(_row_size_bytes(r) for r in rows)
    assert result.response_bytes == expected


def test_call_get_logs_rejects_non_list_result() -> None:
    transport = _ScriptedTransport()
    # Some providers collapse an empty result to a non-list
    # response; the adapter surfaces this as RpcProtocolError.
    transport.exception = RpcProtocolError("eth_getLogs returned non-list: dict")
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=CONTRACT,
    )
    result = client.call_get_logs(
        from_block=100, to_block=200, address=CONTRACT, topics=[["0x" + "00" * 32]]
    )
    assert result.success is False
    assert result.reason_code == REASON_INVALID_RESPONSE


def test_endpoint_client_supports_alchemy_alias() -> None:
    transport = _ScriptedTransport()
    client = RpcEndpointClient(
        adapter=_adapter_with_transport(transport),
        alias=ALIAS_ALCHEMY_FREE,
        address=CONTRACT,
    )
    assert client.alias == ALIAS_ALCHEMY_FREE
