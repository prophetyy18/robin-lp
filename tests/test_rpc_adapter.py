"""Tests for the bounded read-only RPC adapter (T020).

Covers the T020 acceptance matrix:
- 429 / 5xx / timeouts -> retried with backoff
- malformed / partial responses -> not retried
- duplicate / out-of-order logs -> deduplicated and sorted
- inconsistent heads (per-endpoint behaviour) -> surfaced
- single-block oversize failure -> exhausted with clear error
- retry exhaustion -> failover to the next endpoint, then error
- non-idempotent / invalid requests -> not retried
- endpoint credentials never logged

The transport is always a fake: no network, no httpx, no web3.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from robinhood_lp.rpc import (
    RpcAdapter,
    RpcConfig,
    RpcEndpoint,
    RpcMethodNotAllowedError,
    RpcProtocolError,
    RpcResponseError,
    RpcRetryExhausted,
    TransportError,
    to_hex,
)

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _ScriptedTransport:
    """A transport whose response for each call is determined by a
    pre-recorded script.

    The script is a list of either:
    - a dict (a successful JSON-RPC response)
    - an Exception instance (raised as ``TransportError`` /
      ``RpcResponseError`` / ``RpcProtocolError`` / a generic
      ``asyncio.TimeoutError``)

    The transport pops one entry per call. When the script is empty,
    ``out_of_script`` decides what happens (default: raise AssertionError).
    """

    def __init__(
        self,
        script: list[Any],
        *,
        endpoint_url: str = "http://primary",
        out_of_script: Any = None,
    ) -> None:
        self._script = list(script)
        self._url = endpoint_url
        self._out_of_script = out_of_script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, payload))
        if not self._script:
            if self._out_of_script is None:
                raise AssertionError(f"transport called beyond script (call #{len(self.calls)})")
            if isinstance(self._out_of_script, BaseException):
                raise self._out_of_script
            return self._out_of_script  # type: ignore[no-any-return]
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[no-any-return]


def _adapter(
    transport: _ScriptedTransport,
    *,
    endpoints: tuple[RpcEndpoint, ...] = (
        RpcEndpoint(url="http://primary", name="primary"),
        RpcEndpoint(url="http://backup", name="backup"),
    ),
    retry_limit: int = 2,
    initial_backoff_seconds: float = 0.0,
    max_block_range_per_request: int = 10_000,
) -> RpcAdapter:
    """Build an adapter whose sleeps are no-ops so tests are fast."""
    cfg = RpcConfig(
        endpoints=endpoints,
        retry_limit=retry_limit,
        initial_backoff_seconds=initial_backoff_seconds or 0.01,
        max_backoff_seconds=1.0,
        max_block_range_per_request=max_block_range_per_request,
    )
    return RpcAdapter(
        cfg,
        transport=transport,
        sleeper=lambda _seconds: asyncio.sleep(0),
    )


def _resp(result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 0, "result": result}


def _err(code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 0, "error": {"code": code, "message": message}}


def _log_record(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_block_number_round_trip() -> None:
    transport = _ScriptedTransport([_resp("0xa")])
    adapter = _adapter(transport)
    assert await adapter.eth_block_number() == 10
    assert adapter.metrics.summary() == {
        "requests": 1,
        "successes": 1,
        "failures": 0,
        "retries": 0,
        "failovers": 0,
        "range_splits": 0,
    }


async def test_method_not_allowed_is_rejected() -> None:
    """T020 must-not: 'expose generic arbitrary RPC'."""
    transport = _ScriptedTransport([])
    adapter = _adapter(transport)
    with pytest.raises(RpcMethodNotAllowedError):
        # ``eth_sendRawTransaction`` is not in ALLOWED_METHODS.
        await adapter._call_with_failover("eth_sendRawTransaction", [])


async def test_to_hex_encodes_correctly() -> None:
    assert to_hex(0) == "0x0"
    assert to_hex(255) == "0xff"
    assert to_hex(0xDEADBEEF) == "0xdeadbeef"
    with pytest.raises(ValueError):
        to_hex(-1)
    with pytest.raises(TypeError):
        to_hex("0x10")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 429 / 5xx / timeout: retried then either succeeds or fails over
# ---------------------------------------------------------------------------


async def test_429_is_retried_then_succeeds() -> None:
    """One 429 then a success; the retry counter is bumped once."""
    transport = _ScriptedTransport(
        [
            TransportError("429 Too Many Requests"),
            _resp("0x10"),
        ]
    )
    adapter = _adapter(transport, retry_limit=2)
    assert await adapter.eth_block_number() == 16
    summary = adapter.metrics.summary()
    assert summary["requests"] == 2
    assert summary["retries"] == 1
    assert summary["failovers"] == 0


async def test_5xx_is_retried_then_succeeds() -> None:
    transport = _ScriptedTransport(
        [
            TransportError("503 Service Unavailable"),
            TransportError("502 Bad Gateway"),
            _resp("0x1"),
        ]
    )
    adapter = _adapter(transport, retry_limit=3)
    assert await adapter.eth_block_number() == 1
    assert adapter.metrics.summary()["retries"] == 2


async def test_asyncio_timeout_is_retried() -> None:
    transport = _ScriptedTransport(
        [
            TimeoutError(),
            _resp("0x2"),
        ]
    )
    adapter = _adapter(transport, retry_limit=2)
    assert await adapter.eth_block_number() == 2
    assert adapter.metrics.summary()["retries"] == 1


async def test_retry_exhausted_fails_over_to_next_endpoint() -> None:
    """When retry_limit is exhausted on the primary, the adapter
    tries the backup and resets its retry counter there."""
    transport_primary = _ScriptedTransport(
        [
            TransportError("500"),
            TransportError("500"),
            TransportError("500"),
        ]
    )
    transport_backup = _ScriptedTransport([_resp("0x7")])

    # Two endpoints, each with its own transport.
    async def multiplex(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if url == "http://primary":
            return await transport_primary(url, payload)
        return await transport_backup(url, payload)

    cfg = RpcConfig(
        endpoints=(
            RpcEndpoint(url="http://primary", name="primary"),
            RpcEndpoint(url="http://backup", name="backup"),
        ),
        retry_limit=2,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.02,
    )
    adapter = RpcAdapter(cfg, transport=multiplex, sleeper=lambda _: asyncio.sleep(0))
    assert await adapter.eth_block_number() == 7
    summary = adapter.metrics.summary()
    assert summary["failovers"] == 1
    # Primary saw retry_limit + 1 attempts (the initial + 2 retries = 3).
    assert len(transport_primary.calls) == 3
    # Backup saw exactly one successful call.
    assert len(transport_backup.calls) == 1


async def test_all_endpoints_fail_raises_retry_exhausted() -> None:
    transport_primary = _ScriptedTransport([TransportError("500")] * 10)
    transport_backup = _ScriptedTransport([TransportError("500")] * 10)

    async def multiplex(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if url == "http://primary":
            return await transport_primary(url, payload)
        return await transport_backup(url, payload)

    cfg = RpcConfig(
        endpoints=(
            RpcEndpoint(url="http://primary", name="primary"),
            RpcEndpoint(url="http://backup", name="backup"),
        ),
        retry_limit=1,
        initial_backoff_seconds=0.01,
    )
    adapter = RpcAdapter(cfg, transport=multiplex, sleeper=lambda _: asyncio.sleep(0))
    with pytest.raises(RpcRetryExhausted):
        await adapter.eth_block_number()


# ---------------------------------------------------------------------------
# Malformed / partial responses: NOT retried
# ---------------------------------------------------------------------------


async def test_malformed_json_does_not_retry() -> None:
    """A response body that is not a JSON object is not retried."""
    transport = _ScriptedTransport(["not-json-but-string-not-dict"])
    adapter = _adapter(transport, retry_limit=5)
    with pytest.raises(RpcProtocolError):
        await adapter.eth_block_number()
    # Exactly one request, zero retries.
    assert adapter.metrics.summary()["requests"] == 1
    assert adapter.metrics.summary()["retries"] == 0


async def test_response_with_both_error_and_result_does_not_retry() -> None:
    transport = _ScriptedTransport(
        [{"jsonrpc": "2.0", "id": 0, "result": "0x1", "error": {"code": -32000}}]
    )
    adapter = _adapter(transport, retry_limit=5)
    with pytest.raises(RpcProtocolError):
        await adapter.eth_block_number()
    assert adapter.metrics.summary()["retries"] == 0


async def test_non_transient_rpc_error_does_not_retry() -> None:
    """A code that is not transient (-32600 invalid request) is not retried."""
    transport = _ScriptedTransport([_err(-32600, "invalid params")] * 10)
    adapter = _adapter(transport, retry_limit=5)
    with pytest.raises(RpcResponseError):
        await adapter.eth_block_number()
    assert adapter.metrics.summary()["retries"] == 0


# ---------------------------------------------------------------------------
# Non-idempotent methods: not retried
# ---------------------------------------------------------------------------


async def test_eth_call_is_not_retried_on_5xx() -> None:
    """T020 must-not: 'no retry of non-idempotent / invalid requests'.
    ``eth_call`` mutates nothing but a malformed call returns the same
    error on retry; the adapter classifies it as non-retryable so the
    caller sees the real failure immediately."""
    transport = _ScriptedTransport([TransportError("500")] * 10)
    adapter = _adapter(transport, retry_limit=5)
    with pytest.raises(TransportError):
        await adapter.eth_call(to="0x" + "11" * 20, data="0x", block_number=1)
    assert adapter.metrics.summary()["requests"] == 1


# ---------------------------------------------------------------------------
# Duplicate / out-of-order logs: deduplicated and sorted
# ---------------------------------------------------------------------------


def _log(block: int, tx: int, log: int) -> dict[str, Any]:
    return {
        "address": "0x" + "22" * 20,
        "blockHash": "0x" + "00" * 32,
        "blockNumber": to_hex(block),
        "data": "0x",
        "logIndex": to_hex(log),
        "transactionHash": "0x" + "00" * 32,
        "transactionIndex": to_hex(tx),
    }


async def test_logs_are_sorted_and_duplicates_dropped() -> None:
    """Provider returns the same page twice with different ordering;
    the adapter sorts and deduplicates by (block, tx, log)."""
    page = [
        _log(2, 0, 1),
        _log(1, 0, 0),
        _log(2, 0, 1),  # duplicate of the third entry above
        _log(3, 5, 0),
        _log(1, 0, 0),  # duplicate of the second entry above
    ]
    transport = _ScriptedTransport([_resp(page)])
    adapter = _adapter(transport)
    logs = await adapter.eth_get_logs(from_block=1, to_block=3, address="0x" + "22" * 20, topics=[])
    keys = [
        (int(log["blockNumber"], 16), int(log["transactionIndex"], 16), int(log["logIndex"], 16))
        for log in logs
    ]
    assert keys == [(1, 0, 0), (2, 0, 1), (3, 5, 0)]
    assert len(logs) == 3  # two duplicates dropped


async def test_log_entry_with_missing_fields_is_rejected() -> None:
    transport = _ScriptedTransport([_resp([{"blockNumber": "0x1"}])])
    adapter = _adapter(transport)
    with pytest.raises(RpcProtocolError):
        await adapter.eth_get_logs(1, 1, "0x" + "22" * 20, [])


# ---------------------------------------------------------------------------
# Inconsistent heads (per-endpoint behaviour)
# ---------------------------------------------------------------------------


async def test_capability_probe_surfaces_per_endpoint_results() -> None:
    """One endpoint returns a chain id, another raises; both are
    surfaced so T024 can detect the inconsistency."""

    async def multiplex(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if url == "http://primary":
            if payload["method"] == "eth_chainId":
                return _resp("0x1")
            return _resp("0xa")
        if payload["method"] == "eth_chainId":
            raise TransportError("timeout")
        return _resp("0xb")

    cfg = RpcConfig(
        endpoints=(
            RpcEndpoint(url="http://primary", name="primary"),
            RpcEndpoint(url="http://backup", name="backup"),
        ),
        retry_limit=0,
        initial_backoff_seconds=0.01,
    )
    adapter = RpcAdapter(
        cfg,
        transport=multiplex,
        sleeper=lambda _: asyncio.sleep(0),
    )
    cap = await adapter.capability_probe()
    assert cap["primary"]["chain_id"] == "0x1"
    assert "error" in cap["backup"]
    assert "chain_id" not in cap["backup"]


# ---------------------------------------------------------------------------
# Single-block oversize failure -> split, exhausted with clear error
# ---------------------------------------------------------------------------


async def test_single_block_oversize_raises_clear_error() -> None:
    """If even a single block produces a 'too many results' error,
    the splitter cannot halve further; the adapter raises
    RpcRetryExhausted naming the range."""
    transport = _ScriptedTransport(
        [
            _err(-32005, "query returned more than 10000 results"),
        ]
        * 10
    )
    adapter = _adapter(transport, retry_limit=2, max_block_range_per_request=1)
    with pytest.raises(RpcRetryExhausted, match=r"cannot be split further at"):
        await adapter.eth_get_logs(from_block=100, to_block=100, address="0xabcd", topics=[])


async def test_range_split_eventually_returns_all_logs() -> None:
    """A range that triggers a 'too many' error on the first call is
    split in half; each half succeeds; the union covers the full range."""
    # First call returns too many; the split halves 100..200 into
    # 100..150 then 151..200, then both halves return their logs.
    page_left = [_log(110, 0, 0)]
    page_right = [_log(190, 0, 0)]
    transport = _ScriptedTransport(
        [
            _err(-32005, "query returned more than 10000 results"),
            _resp(page_left),
            _resp(page_right),
        ]
    )
    adapter = _adapter(transport, retry_limit=2, max_block_range_per_request=200)
    logs = await adapter.eth_get_logs(
        from_block=100, to_block=200, address="0x" + "22" * 20, topics=[]
    )
    assert adapter.metrics.summary()["range_splits"] == 1
    keys = [(int(log["blockNumber"], 16),) for log in logs]
    assert keys == [(110,), (190,)]


# ---------------------------------------------------------------------------
# Endpoint credentials never logged
# ---------------------------------------------------------------------------


async def test_endpoint_credentials_never_appear_in_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T020 must-not: 'log endpoint credentials'.

    A transport failure for an endpoint whose URL contains
    user:password@host must surface an error log that omits the
    credentials.
    """
    bad_endpoint = RpcEndpoint(url="https://alice:hunter2@primary.example/rpc", name="primary")
    transport = _ScriptedTransport(
        [TransportError("500")] * 10,
        endpoint_url=bad_endpoint.url,
    )
    cfg = RpcConfig(
        endpoints=(bad_endpoint,),
        retry_limit=1,
        initial_backoff_seconds=0.01,
    )
    adapter = RpcAdapter(cfg, transport=transport, sleeper=lambda _: asyncio.sleep(0))
    caplog.set_level(logging.INFO, logger="robinhood_lp.rpc.adapter")
    with pytest.raises(RpcRetryExhausted):
        await adapter.eth_block_number()
    messages = _log_record(caplog)
    # At minimum, no recorded message may contain the credentials.
    assert not any("alice" in m or "hunter2" in m for m in messages), (
        f"endpoint credentials leaked into logs: {messages}"
    )


# ---------------------------------------------------------------------------
# Endpoints with weight / disabled flag
# ---------------------------------------------------------------------------


def test_active_endpoints_skips_disabled() -> None:
    cfg = RpcConfig(
        endpoints=(
            RpcEndpoint(url="http://primary", name="primary", weight=0),
            RpcEndpoint(url="http://broken", name="broken", weight=-1),
            RpcEndpoint(url="http://backup", name="backup", weight=1),
        )
    )
    active = cfg.active_endpoints()
    assert [e.name for e in active] == ["primary", "backup"]


def test_config_rejects_all_endpoints_disabled() -> None:
    with pytest.raises(ValueError, match="no active entries"):
        RpcConfig(
            endpoints=(
                RpcEndpoint(url="http://a", name="a", weight=-1),
                RpcEndpoint(url="http://b", name="b", weight=-1),
            )
        )


def test_config_rejects_no_endpoints() -> None:
    with pytest.raises(ValueError, match="at least one entry"):
        RpcConfig(endpoints=())


def test_config_rejects_zero_max_block_range() -> None:
    with pytest.raises(ValueError):
        RpcConfig(
            endpoints=(RpcEndpoint(url="http://a", name="a"),),
            max_block_range_per_request=0,
        )


def test_config_rejects_negative_timeout() -> None:
    with pytest.raises(ValueError):
        RpcConfig(
            endpoints=(RpcEndpoint(url="http://a", name="a"),),
            request_timeout_seconds=-1.0,
        )


# ---------------------------------------------------------------------------
# Capability probe: each endpoint tried independently
# ---------------------------------------------------------------------------


async def test_capability_probe_does_not_failover_on_error() -> None:
    """A failure on one endpoint in capability_probe must not abort
    the probe; the other endpoints are still queried."""

    async def multiplex(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if url == "http://primary":
            raise TransportError("503")
        return _resp("0x42")

    cfg = RpcConfig(
        endpoints=(
            RpcEndpoint(url="http://primary", name="primary"),
            RpcEndpoint(url="http://backup", name="backup"),
        ),
        retry_limit=0,
        initial_backoff_seconds=0.01,
    )
    adapter = RpcAdapter(cfg, transport=multiplex, sleeper=lambda _: asyncio.sleep(0))
    cap = await adapter.capability_probe()
    assert "error" in cap["primary"]
    assert cap["backup"]["chain_id"] == "0x42"


# ---------------------------------------------------------------------------
# Block-pinned eth_call
# ---------------------------------------------------------------------------


async def test_eth_call_is_block_pinned() -> None:
    """``eth_call`` must include both the call object and the block tag."""
    transport = _ScriptedTransport([_resp("0xdeadbeef")])
    adapter = _adapter(transport)
    out = await adapter.eth_call(to="0x" + "11" * 20, data="0x1234", block_number=42)
    assert out == "0xdeadbeef"
    sent = transport.calls[0][1]
    assert sent["method"] == "eth_call"
    assert sent["params"][0] == {"to": "0x" + "11" * 20, "data": "0x1234"}
    assert sent["params"][1] == "0x2a"  # 42


async def test_eth_call_non_hex_result_is_rejected() -> None:
    transport = _ScriptedTransport([_resp({"unexpected": "object"})])
    adapter = _adapter(transport)
    with pytest.raises(RpcProtocolError):
        await adapter.eth_call(to="0x" + "11" * 20, data="0x", block_number=1)


# ---------------------------------------------------------------------------
# eth_getCode and eth_getTransactionReceipt
# ---------------------------------------------------------------------------


async def test_get_code_returns_hex() -> None:
    transport = _ScriptedTransport([_resp("0xfe")])
    adapter = _adapter(transport)
    out = await adapter.eth_get_code(address="0x" + "33" * 20, block_number=5)
    assert out == "0xfe"


async def test_get_transaction_receipt_returns_object() -> None:
    receipt = {"blockHash": "0x" + "01" * 32, "status": "0x1"}
    transport = _ScriptedTransport([_resp(receipt)])
    adapter = _adapter(transport)
    out = await adapter.eth_get_transaction_receipt("0x" + "ab" * 32)
    assert out == receipt
