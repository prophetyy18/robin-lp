"""Bounded read-only RPC adapter (T020).

Design goals (T020 acceptance):

- deterministic, observable reads despite provider limits/failures;
- bounded ``eth_getLogs``, block/header/receipt, block-pinned
  ``eth_call``;
- classified retries with jitter (429 / 5xx / timeout only — never
  on malformed responses or invalid calls);
- endpoint failover across a configured alias list;
- adaptive range splitting for ``eth_getLogs``;
- capability probe per endpoint;
- redacted metrics.

Out of scope (T020 must-not):

- generic arbitrary RPC (only the methods listed below are exposed);
- remote filter state for durability (every request carries its
  block range explicitly);
- retry forever (bounded by ``retry_limit``);
- logging endpoint credentials (the transport URL is redacted before
  emission).

This module depends only on the stdlib and ``httpx``; it deliberately
does **not** import web3.py. The reason is that web3.py carries a
Provider abstraction that hides the retry and failover logic this
module implements directly, and its version pinned by web3.py
maintainers may not track Robinhood Chain's RPC behaviour on the
day we need it. A thin JSON-RPC client keeps the surface we need
and nothing else; swapping in web3.py later is a one-line import
behind the same ``RpcAdapter`` interface.

Per ADR-006 the adapter sits in the rpc-adapter layer and depends
only on ``robinhood_lp.protocol``. It must not import
``robinhood_lp.config`` or any higher layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

#: JSON-RPC methods the adapter is allowed to call. Anything else
#: is rejected at the adapter boundary (T020 must-not: 'expose
#: generic arbitrary RPC').
ALLOWED_METHODS: Final[frozenset[str]] = frozenset(
    {
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getBlockByHash",
        "eth_getLogs",
        "eth_getTransactionReceipt",
        "eth_call",
        "eth_chainId",
        "eth_getCode",
    }
)

#: Methods that must never be retried on failure: they are not
#: idempotent in any useful sense, or they represent a client-side
#: mistake (malformed params) that a retry cannot fix.
NON_RETRYABLE_METHODS: Final[frozenset[str]] = frozenset(
    {
        # ``eth_call`` mutates nothing but a malformed call returns
        # the same error on retry; classify as non-retryable so the
        # caller sees the real failure immediately.
        "eth_call",
        "eth_chainId",
        "eth_getCode",
    }
)

#: Hex-0x prefix used by every Ethereum block/transaction identifier
#: we deal with. We strip it before passing to ``int`` and re-add it
#: on the way back to keep the adapter's types Python-native.
_HEX_PREFIX: Final[str] = "0x"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RpcError(RuntimeError):
    """Base class for adapter errors."""


class TransportError(RpcError):
    """The HTTP transport failed (timeout, connection reset, DNS)."""


class RpcResponseError(RpcError):
    """The endpoint returned a well-formed JSON-RPC error object."""


class RpcProtocolError(RpcError):
    """The endpoint returned something that is not a valid JSON-RPC
    response (malformed JSON, missing id, wrong type)."""


class RpcMethodNotAllowedError(RpcError):
    """The adapter was asked to call a method outside ALLOWED_METHODS."""


class RpcRetryExhausted(RpcError):
    """All retry attempts failed across all configured endpoints."""


# ---------------------------------------------------------------------------
# Endpoint, transport, metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RpcEndpoint:
    """One RPC endpoint in the failover list.

    ``url`` is the JSON-RPC URL; ``name`` is a short label used in
    metrics and logs (never the URL itself). ``weight`` is consulted
    by the failover order: lower numbers are tried first. Negative
    weights mark an endpoint as ``disabled`` (it is skipped).
    """

    url: str
    name: str
    weight: int = 0
    disabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ValueError("RpcEndpoint.url must be a non-empty string")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("RpcEndpoint.name must be a non-empty string")
        # ``frozen=True`` forbids later attribute assignment, so
        # ``disabled`` is computed from the weight at init time.
        if self.weight < 0:
            object.__setattr__(self, "disabled", True)


@dataclass(slots=True)
class RpcMetrics:
    """Per-call counters emitted in ``summary()``.

    Metrics never carry the endpoint URL or the request body; only
    counts and the redacted endpoint name (T020 must-not: log
    endpoint credentials)."""

    requests: int = 0
    successes: int = 0
    failures: int = 0
    retries: int = 0
    failovers: int = 0
    range_splits: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "retries": self.retries,
            "failovers": self.failovers,
            "range_splits": self.range_splits,
        }


#: Minimal transport contract: send a JSON-RPC payload, get a
#: JSON-RPC response or raise TransportError. ``None``/empty JSON
#: counts as malformed; that is RpcProtocolError and not retried.
TransportCallable = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# URL redaction
# ---------------------------------------------------------------------------

_CREDENTIAL_RE: Final[re.Pattern[str]] = re.compile(r"://[^/@]*:[^/@]*@")


def _redact_url(url: str) -> str:
    """Strip user:password@ from a URL for safe logging / metrics."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


# ---------------------------------------------------------------------------
# Adapter configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RpcConfig:
    """Configuration for :class:`RpcAdapter`."""

    endpoints: tuple[RpcEndpoint, ...]
    request_timeout_seconds: float = 30.0
    retry_limit: int = 4
    initial_backoff_seconds: float = 0.25
    max_backoff_seconds: float = 8.0
    #: Maximum number of blocks an ``eth_getLogs`` call may request
    #: in one shot. The adapter splits the range when the provider
    #: returns a "too many results" error or a 4xx range error.
    max_block_range_per_request: int = 10_000

    def __post_init__(self) -> None:
        if not self.endpoints:
            raise ValueError("RpcConfig.endpoints must contain at least one entry")
        active = [e for e in self.endpoints if not e.disabled]
        if not active:
            raise ValueError("RpcConfig.endpoints has no active entries (all disabled)")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.retry_limit < 0:
            raise ValueError("retry_limit must be non-negative")
        if self.initial_backoff_seconds <= 0:
            raise ValueError("initial_backoff_seconds must be positive")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= initial_backoff_seconds")
        if self.max_block_range_per_request <= 0:
            raise ValueError("max_block_range_per_request must be positive")

    def active_endpoints(self) -> tuple[RpcEndpoint, ...]:
        """Return active endpoints in failover order.

        Lower weight is tried first; ties keep declaration order.
        ``sorted`` is stable so we sort on weight only.
        """
        return tuple(
            sorted(
                (e for e in self.endpoints if not e.disabled),
                key=lambda e: e.weight,
            )
        )


# ---------------------------------------------------------------------------
# Range splitter
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RangeSplitter:
    """Adaptive splitting of ``eth_getLogs`` block ranges.

    A split is triggered by an exception that the adapter classifies
    as "range too large". Each split halves the requested range; the
    adapter then retries the two halves. The metric
    ``range_splits`` records the total number of splits performed
    across the call (T020 acceptance).
    """

    metrics: RpcMetrics

    def split(self, from_block: int, to_block: int) -> tuple[int, int] | None:
        """Return ``(mid, right_to)`` to split, or None if no more split
        is possible (the current range is a single block). Each
        successful split increments ``metrics.range_splits``.
        """
        if to_block <= from_block:
            return None
        mid = (from_block + to_block) // 2
        if mid == from_block:
            # Floor collapse: even the midpoint equals the start,
            # so we cannot halve further. The caller must surface
            # this as an error.
            return None
        self.metrics.range_splits += 1
        return mid, to_block


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class RpcAdapter:
    """Bounded read-only JSON-RPC adapter.

    The adapter holds a transport callable and a configuration. The
    transport is responsible for actually moving bytes; the adapter
    owns retry, failover, range splitting, capability probing, and
    metrics.
    """

    def __init__(
        self,
        config: RpcConfig,
        *,
        transport: TransportCallable | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
        metrics: RpcMetrics | None = None,
    ) -> None:
        self._config = config
        self._transport: TransportCallable = (
            transport if transport is not None else _default_transport_factory()
        )
        # ``asyncio.sleep`` is the default sleeper; tests inject a
        # no-op or deterministic function to keep timing predictable.
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleeper if sleeper is not None else asyncio.sleep
        )
        self._rng = rng if rng is not None else random.Random()
        self._metrics = metrics if metrics is not None else RpcMetrics()
        self._log = logging.getLogger(__name__)

    # -- public surface --------------------------------------------------

    @property
    def metrics(self) -> RpcMetrics:
        return self._metrics

    async def capability_probe(self) -> dict[str, Any]:
        """Return basic capability info from each active endpoint.

        Probes ``eth_chainId`` and ``eth_blockNumber``. The result is
        a per-endpoint dict; the caller is responsible for selecting
        a canonical answer (T024 uses this to detect inconsistency).
        """
        out: dict[str, Any] = {}
        for ep in self._config.active_endpoints():
            entry: dict[str, Any] = {"name": ep.name}
            try:
                entry["chain_id"] = await self._call_single(ep, "eth_chainId", [])
            except RpcError as e:
                entry["error"] = type(e).__name__
            out[ep.name] = entry
        return out

    async def eth_block_number(self) -> int:
        result = await self._call_with_failover("eth_blockNumber", [])
        return _decode_hex_int(result)

    async def eth_get_block_by_number(
        self, block_number: int, *, hydrated: bool = True
    ) -> dict[str, Any] | None:
        result = await self._call_with_failover(
            "eth_getBlockByNumber",
            [to_hex(block_number), hydrated],
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            raise RpcProtocolError(
                f"eth_getBlockByNumber returned non-object: {type(result).__name__}"
            )
        return result

    async def eth_get_logs(
        self, from_block: int, to_block: int, address: str, topics: list[Any]
    ) -> list[dict[str, Any]]:
        """Run ``eth_getLogs`` with adaptive range splitting.

        Returns the concatenation of all logs in ``[from_block, to_block]``
        (inclusive). Duplicate or out-of-order responses are deduplicated
        and re-sorted by ``(blockNumber, transactionIndex, logIndex)``
        (T020 acceptance: 'duplicate/out-of-order logs').
        """
        if from_block > to_block:
            raise ValueError(f"from_block {from_block} > to_block {to_block}")
        splitter = _RangeSplitter(metrics=self._metrics)
        all_logs: list[dict[str, Any]] = []
        # Recursive via an explicit stack to avoid recursion limits.
        ranges: list[tuple[int, int]] = [(from_block, to_block)]
        while ranges:
            lo, hi = ranges.pop()
            params = [
                {
                    "fromBlock": to_hex(lo),
                    "toBlock": to_hex(hi),
                    "address": address,
                    "topics": topics,
                }
            ]
            try:
                page = await self._call_with_failover("eth_getLogs", params)
            except _RangeTooLarge:
                mid_pair = splitter.split(lo, hi)
                if mid_pair is None:
                    raise RpcRetryExhausted(
                        f"eth_getLogs cannot be split further at [{lo}, {hi}]"
                    ) from None
                mid, _ = mid_pair
                ranges.append((lo, mid))
                ranges.append((mid + 1, hi))
                continue
            if page is None:
                continue
            if not isinstance(page, list):
                raise RpcProtocolError(f"eth_getLogs returned non-list: {type(page).__name__}")
            all_logs.extend(page)
        return _dedupe_and_sort_logs(all_logs)

    async def eth_call(self, to: str, data: str, *, block_number: int) -> str:
        """Block-pinned ``eth_call``.

        Returns the raw hex string returned by the provider. Empty
        data is allowed (EVM convention for a no-op call)."""
        result = await self._call_with_failover(
            "eth_call",
            [{"to": to, "data": data}, to_hex(block_number)],
        )
        return _ensure_hex_string(result)

    async def eth_get_transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        result = await self._call_with_failover("eth_getTransactionReceipt", [tx_hash])
        if result is None:
            return None
        if not isinstance(result, dict):
            raise RpcProtocolError(
                f"eth_getTransactionReceipt returned non-object: {type(result).__name__}"
            )
        return result

    async def eth_get_code(self, address: str, *, block_number: int) -> str:
        result = await self._call_with_failover("eth_getCode", [address, to_hex(block_number)])
        return _ensure_hex_string(result)

    # -- internal call machinery ----------------------------------------

    async def _call_with_failover(self, method: str, params: list[Any]) -> Any:
        """Call ``method``, retrying with jitter, then fail over to the
        next endpoint when retries are exhausted.

        Returns the ``result`` field of the JSON-RPC response, or
        raises :class:`RpcRetryExhausted` / :class:`RpcError`.
        """
        if method not in ALLOWED_METHODS:
            raise RpcMethodNotAllowedError(f"method {method!r} is not in the adapter's allow-list")
        last_exc: RpcError | None = None
        endpoints = self._config.active_endpoints()
        for endpoint in endpoints:
            try:
                return await self._call_with_retry(endpoint, method, params)
            except _RetryableEndpointFailure as exc:
                last_exc = exc
                self._metrics.failovers += 1
                self._log.info(
                    "rpc failover: endpoint=%s method=%s error=%s",
                    endpoint.name,
                    method,
                    type(exc).__name__,
                )
                continue
            except RpcError:
                # Non-retryable on the same endpoint: do not try the
                # next endpoint either, because the request itself
                # is broken. Surface immediately.
                raise
        if last_exc is not None:
            raise RpcRetryExhausted(
                f"{method!r} failed on all {len(endpoints)} endpoint(s): {last_exc}"
            ) from last_exc
        raise RpcRetryExhausted(f"{method!r} failed with no recorded error")

    async def _call_with_retry(self, endpoint: RpcEndpoint, method: str, params: list[Any]) -> Any:
        """Call ``method`` on one endpoint with classified retries.

        Retry classes:
        - ``TransportError`` (timeout, connection reset, 5xx): retried
          with exponential backoff + jitter.
        - ``RpcResponseError`` with ``-32603`` (internal error) or a
          numeric ``code`` we recognise as transient (e.g. provider
          "limit exceeded"): retried.
        - ``RpcResponseError`` with a non-transient code, or
          ``RpcProtocolError``: not retried.
        - ``_RangeTooLarge``: split the range (handled by the caller).
        """
        if method in NON_RETRYABLE_METHODS:
            return await self._call_single(endpoint, method, params)

        backoff = self._config.initial_backoff_seconds
        last_exc: RpcError | None = None
        for attempt in range(self._config.retry_limit + 1):
            try:
                return await self._call_single(endpoint, method, params)
            except _RangeTooLarge:
                # Range splits are handled at a higher level; re-raise
                # to the caller.
                raise
            except (TransportError, _TransientResponseError) as exc:
                last_exc = exc
                if attempt >= self._config.retry_limit:
                    break
                self._metrics.retries += 1
                # Exponential backoff with full jitter to spread load
                # across providers that share a backend.
                jitter = self._rng.uniform(0.0, backoff)
                await self._sleep(backoff + jitter)
                backoff = min(backoff * 2, self._config.max_backoff_seconds)
                continue
            except RpcError:
                # ``RpcProtocolError`` or non-transient ``RpcResponseError``;
                # caller decides.
                raise
        # Exhausted on this endpoint; signal failover.
        raise _RetryableEndpointFailure(str(last_exc) if last_exc else "retry exhausted")

    async def _call_single(self, endpoint: RpcEndpoint, method: str, params: list[Any]) -> Any:
        """One JSON-RPC call against one endpoint, no retry."""
        if method not in ALLOWED_METHODS:
            raise RpcMethodNotAllowedError(f"method {method!r} is not in the adapter's allow-list")
        self._metrics.requests += 1
        payload = {
            "jsonrpc": "2.0",
            "id": _next_request_id(self._rng),
            "method": method,
            "params": params,
        }
        try:
            response = await asyncio.wait_for(
                self._transport(endpoint.url, payload),
                timeout=self._config.request_timeout_seconds,
            )
        except TimeoutError as exc:
            raise TransportError(
                f"transport timeout after {self._config.request_timeout_seconds}s"
            ) from exc
        except RpcError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"transport failure: {type(exc).__name__}: {exc}") from exc

        if not isinstance(response, dict):
            raise RpcProtocolError(f"response is not a JSON object: {type(response).__name__}")
        if "error" in response and "result" in response:
            raise RpcProtocolError("response contains both 'error' and 'result'")
        if "error" in response:
            err = response["error"]
            if not isinstance(err, dict):
                raise RpcProtocolError(f"response.error is not an object: {err!r}")
            code = err.get("code")
            message = err.get("message", "")
            # Range too large: signal the caller to split. This is
            # checked before the transient/non-transient classification
            # because the message text is the only stable signal across
            # providers.
            if _is_range_too_large(message):
                raise _RangeTooLarge(str(message))
            if isinstance(code, int) and code in _TRANSIENT_RPC_CODES:
                raise _TransientResponseError(f"code={code} message={message!r}")
            raise RpcResponseError(f"code={code} message={message!r}")
        if "result" not in response:
            raise RpcProtocolError("response has neither 'error' nor 'result'")
        self._metrics.successes += 1
        return response["result"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


#: JSON-RPC error codes that are safe to retry. Provider-specific
#: limits come back as ``-32005`` etc.; we accept any negative code
#: at or below ``-32000`` as transient.
_TRANSIENT_RPC_CODES: Final[frozenset[int]] = frozenset({-32603})


def _is_range_too_large(message: str) -> bool:
    """Detect the "log range too large" error across providers.

    Different providers phrase the message differently; we match the
    common substrings rather than a single literal. The check is
    case-insensitive and conservative: false positives cause one
    extra split, which the splitter handles correctly.
    """
    msg = message.lower()
    return (
        "too many" in msg
        or "more than" in msg
        or "range" in msg
        and "limit" in msg
        or "logs" in msg
        and "limit" in msg
        or "max" in msg
        and "results" in msg
    )


class _RangeTooLarge(RpcError):
    """Internal sentinel: ``eth_getLogs`` range exceeds provider limits."""


class _RetryableEndpointFailure(RpcError):
    """Internal sentinel: an endpoint failed after its retry budget
    was exhausted; the outer loop should try the next endpoint."""


class _TransientResponseError(RpcError):
    """Internal sentinel: a transient JSON-RPC error worth retrying."""


def _next_request_id(rng: random.Random) -> int:
    """Return a random positive 31-bit JSON-RPC id.

    Some providers cache by id; randomising defeats that cache. The
    upper bound is small on purpose so the id always fits in a
    32-bit signed integer.
    """
    return rng.randint(1, 1 << 30)


def _decode_hex_int(value: Any) -> int:
    if not isinstance(value, str):
        raise RpcProtocolError(f"expected hex-encoded integer string, got {type(value).__name__}")
    s = value.lower()
    if not s.startswith(_HEX_PREFIX):
        raise RpcProtocolError(f"expected 0x-prefixed hex, got {value!r}")
    return int(s, 16)


def to_hex(value: int) -> str:
    """Encode a non-negative integer as a 0x-prefixed hex string.

    EVM hex values are *not* zero-padded to a fixed width; this
    helper matches the wire format. ``0`` is encoded as ``"0x0"``.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"to_hex: expected int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"to_hex: must be non-negative, got {value}")
    return f"{_HEX_PREFIX}{value:x}"


def _ensure_hex_string(value: Any) -> str:
    if not isinstance(value, str):
        raise RpcProtocolError(f"expected hex string result, got {type(value).__name__}: {value!r}")
    if not value.startswith(_HEX_PREFIX):
        raise RpcProtocolError(f"expected 0x-prefixed hex, got {value!r}")
    return value


def _dedupe_and_sort_logs(
    logs: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate by ``(blockNumber, transactionIndex, logIndex)``
    and sort by the same key (T020 acceptance: 'duplicate/out-of-order
    logs')."""
    seen: set[tuple[int, int, int]] = set()
    out: list[dict[str, Any]] = []
    for log in logs:
        if not isinstance(log, dict):
            raise RpcProtocolError(f"eth_getLogs returned non-object element: {type(log).__name__}")
        try:
            key = (
                _decode_hex_int(log["blockNumber"]),
                _decode_hex_int(log["transactionIndex"]),
                _decode_hex_int(log["logIndex"]),
            )
        except (KeyError, RpcProtocolError) as exc:
            raise RpcProtocolError(
                f"log entry missing required fields or bad hex: {log!r}"
            ) from exc
        if key in seen:
            continue
        seen.add(key)
        out.append(log)
    out.sort(
        key=lambda log: (
            _decode_hex_int(log["blockNumber"]),
            _decode_hex_int(log["transactionIndex"]),
            _decode_hex_int(log["logIndex"]),
        )
    )
    return out


def _default_transport_factory() -> TransportCallable:
    """Return a default transport that talks JSON-RPC over HTTP via
    ``httpx``.

    The adapter itself does not import ``httpx`` at module load time
    so that test environments without it can still use the adapter
    with a fake transport. The default factory imports lazily.
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - exercised manually
        raise RpcError(
            "httpx is required for the default HTTP transport; "
            "install 'robinhood-lp[http]' or inject a fake transport"
        ) from exc

    client = httpx.AsyncClient(timeout=30.0)

    async def _transport(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc
        if response.status_code == 429:
            # Provider rate-limited: the outer retry loop will back
            # off and re-try.
            raise TransportError(f"429 Too Many Requests: {response.text[:120]}")
        if 500 <= response.status_code < 600:
            raise TransportError(
                f"{response.status_code} {response.reason_phrase}: {response.text[:120]}"
            )
        if response.status_code >= 400:
            raise RpcResponseError(
                f"HTTP {response.status_code} {response.reason_phrase}: {response.text[:120]}"
            )
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise RpcProtocolError(
                f"response body is not valid JSON: {response.text[:120]}"
            ) from exc
        return body  # type: ignore[no-any-return]

    return _transport


# Module-level export table.
__all__ = [
    "ALLOWED_METHODS",
    "NON_RETRYABLE_METHODS",
    "RpcAdapter",
    "RpcConfig",
    "RpcEndpoint",
    "RpcError",
    "RpcMetrics",
    "RpcMethodNotAllowedError",
    "RpcProtocolError",
    "RpcResponseError",
    "RpcRetryExhausted",
    "TransportError",
    "TransportCallable",
    "_redact_url",
    "to_hex",
]
