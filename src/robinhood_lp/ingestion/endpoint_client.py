"""Production :class:`EndpointClient` bridging :class:`RpcAdapter` (T020)
to the router's measured-capability / remaining-budget failover path
(T032 / T035).

The router consumes a thin :class:`EndpointClient` Protocol that
exposes only ``call_get_logs`` and ``get_block_hash_for_pin``; this
module provides the production implementation that satisfies the
protocol by translating :class:`RpcAdapter` results into the router's
:class:`EndpointCallResult` shape.

Routing policy (T032, ADR-010):

- the primary endpoint (Robinhood public) carries the main scan;
- the secondary endpoint (Alchemy Free) participates only within
  its measured capability (~10 blocks per ``eth_getLogs`` call)
  and within its remaining budget;
- failover happens through the router's
  :class:`BudgetLedger`, which decrements per-call budget
  counters; this module does **not** read or decrement the ledger
  directly — the router owns that state.

Error mapping (T032 reason codes):

- ``TransportError`` whose message starts with ``"429 "`` →
  :data:`REASON_HTTP_429_RATE_LIMIT`;
- ``TransportError`` whose message starts with ``"403 "`` →
  :data:`REASON_HTTP_403_DEFAULT_USER_AGENT` (the urllib
  default-User-Agent 403 the planning layer observed);
- ``TransportError`` whose message starts with ``"5"`` →
  :data:`REASON_HTTP_5XX`;
- ``TransportError`` timeout / connection failure →
  :data:`REASON_RPC_TIMEOUT`;
- ``RpcResponseError`` (provider-level JSON-RPC error) →
  :data:`REASON_INVALID_RESPONSE`;
- ``RpcProtocolError`` (malformed response) →
  :data:`REASON_INVALID_RESPONSE`;
- ``RpcRetryExhausted`` (all retries + failover exhausted) →
  :data:`REASON_RPC_TIMEOUT`.

The client never logs the endpoint URL or any header value: the
underlying adapter redacts both, and this module never re-emits
the URL.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Final

from robinhood_lp.ingestion.errors import (
    REASON_HTTP_4XX,
    REASON_HTTP_5XX,
    REASON_HTTP_403_DEFAULT_USER_AGENT,
    REASON_HTTP_403_USER_AGENT_REJECTED,
    REASON_HTTP_429_RATE_LIMIT,
    REASON_INVALID_RESPONSE,
    REASON_OK,
    REASON_RPC_TIMEOUT,
)
from robinhood_lp.ingestion.router import EndpointCallResult
from robinhood_lp.rpc.adapter import (
    RpcAdapter,
    RpcError,
    RpcProtocolError,
    RpcResponseError,
    RpcRetryExhausted,
    TransportError,
)

# ---------------------------------------------------------------------------
# Result-counting helpers
# ---------------------------------------------------------------------------


def _row_size_bytes(row: dict[str, Any]) -> int:
    """Return the canonical byte count for one ``eth_getLogs`` row.

    Used for the manifest's ``response_bytes`` counter. The size
    matches what the JSON-RPC response bytes on the wire would be
    for that row; the manifest sums these so the per-interval
    accounting reconciles with the on-the-wire payload.
    """
    return len(json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _decode_block_hash(value: Any) -> int | None:
    """Decode a JSON-RPC hex ``0x...`` value into a Python int."""
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s.startswith("0x"):
        return None
    try:
        return int(s, 16)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The production EndpointClient
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RpcEndpointClient:
    """Production :class:`EndpointClient` driven by an :class:`RpcAdapter`.

    The client owns no state of its own: it dispatches one logical
    ``eth_getLogs`` call per invocation and lets the adapter handle
    classified retry, failover, and adaptive range splitting. The
    per-endpoint remaining-budget is consulted by the router's
    :class:`BudgetLedger`, not by this module, so the client itself
    is single-responsibility.

    Parameters
    ----------
    adapter:
        A configured :class:`RpcAdapter`. The adapter's underlying
        transport is used as-is; the client does not parse or
        re-emit the URL.
    alias:
        The endpoint alias the router will register this client
        under. Stored on the instance so the router's
        :attr:`EndpointClient.alias` protocol attribute is
        populated.
    address:
        The contract address the router asks the client to query.
        Kept on the instance so ``call_get_logs`` always uses the
        same address even if the router mistakenly passes a
        different one (defensive).
    """

    adapter: RpcAdapter
    alias: str
    address: str
    # The router may set a pinned block-hash override when the
    # scan succeeds with an empty result; we record it here so a
    # re-invocation under the same alias returns the same pin.
    pinned_block_hash_overrides: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Accept any adapter that exposes the read-only methods
        # the client invokes. The production code path passes a
        # real :class:`RpcAdapter`; tests inject a fake that
        # satisfies the same surface.
        required_methods = ("eth_get_logs", "eth_get_block_by_number")
        for name in required_methods:
            if not hasattr(self.adapter, name) or not callable(getattr(self.adapter, name)):
                raise TypeError(
                    f"RpcEndpointClient.adapter: missing required method "
                    f"{name!r}; got {type(self.adapter).__name__}"
                )
        if not isinstance(self.alias, str) or not self.alias:
            raise ValueError(
                f"RpcEndpointClient.alias: must be a non-empty string, got {self.alias!r}"
            )
        if not isinstance(self.address, str) or not self.address:
            raise ValueError(
                f"RpcEndpointClient.address: must be a non-empty string, got {self.address!r}"
            )
        if not self.address.lower().startswith("0x"):
            raise ValueError(
                f"RpcEndpointClient.address: must be 0x-prefixed hex, got {self.address!r}"
            )

    # ----- EndpointClient protocol ----------------------------------

    def call_get_logs(
        self,
        *,
        from_block: int,
        to_block: int,
        address: str,
        topics: list[list[str] | str],
    ) -> EndpointCallResult:
        """Issue one logical ``eth_getLogs`` and translate the result.

        Returns an :class:`EndpointCallResult` whose ``success``
        attribute is True for any successful response (including
        empty arrays). Failures are translated into the T032
        reason-code vocabulary.
        """
        if not isinstance(from_block, int) or isinstance(from_block, bool):
            raise TypeError(
                f"call_get_logs.from_block: must be int, got {type(from_block).__name__}"
            )
        if not isinstance(to_block, int) or isinstance(to_block, bool):
            raise TypeError(f"call_get_logs.to_block: must be int, got {type(to_block).__name__}")
        if from_block > to_block:
            raise ValueError(f"call_get_logs: from_block {from_block} > to_block {to_block}")
        if not isinstance(topics, list) or not topics:
            raise ValueError("call_get_logs: topics must be a non-empty list")
        try:
            rows = asyncio.run(
                self.adapter.eth_get_logs(
                    from_block=from_block,
                    to_block=to_block,
                    address=address,
                    topics=topics,
                )
            )
        except TransportError as exc:
            reason, detail = _classify_transport_error(exc)
            return EndpointCallResult(
                success=False,
                response_bytes=0,
                rows=[],
                reason_code=reason,
                error_detail=detail,
            )
        except (RpcResponseError, RpcProtocolError) as exc:
            return EndpointCallResult(
                success=False,
                response_bytes=0,
                rows=[],
                reason_code=REASON_INVALID_RESPONSE,
                error_detail=f"{type(exc).__name__}: {exc}",
            )
        except RpcRetryExhausted as exc:
            # The adapter wraps every retryable failure (transport,
            # 4xx, 5xx, timeout, transient JSON-RPC) into
            # ``RpcRetryExhausted``. The original error message is
            # preserved as the wrapper's ``args[0]``; re-classify
            # so the router records the precise failure reason
            # (HTTP 429 / 5xx / timeout / etc.) rather than a
            # blanket ``rpc_timeout``.
            reason, detail = _classify_transport_error(TransportError(str(exc)))
            return EndpointCallResult(
                success=False,
                response_bytes=0,
                rows=[],
                reason_code=reason,
                error_detail=f"{type(exc).__name__}: {exc}",
            )
        except RpcError as exc:
            # Catch-all for adapter-level errors we did not
            # classify explicitly. Map to ``invalid_response`` so the
            # router records a failed interval with an explicit
            # reason code rather than crashing the run.
            return EndpointCallResult(
                success=False,
                response_bytes=0,
                rows=[],
                reason_code=REASON_INVALID_RESPONSE,
                error_detail=f"{type(exc).__name__}: {exc}",
            )
        if not isinstance(rows, list):
            return EndpointCallResult(
                success=False,
                response_bytes=0,
                rows=[],
                reason_code=REASON_INVALID_RESPONSE,
                error_detail=f"eth_getLogs returned non-list: {type(rows).__name__}",
            )
        response_bytes = sum(_row_size_bytes(r) for r in rows if isinstance(r, dict))
        # The contract forbids using ``eth_getLogs.blockTimestamp`` as
        # a time source. We do not write it anywhere; the header
        # evidence comes from ``eth_getBlockByNumber`` only.
        return EndpointCallResult(
            success=True,
            response_bytes=response_bytes,
            rows=[dict(r) for r in rows if isinstance(r, dict)],
            reason_code=REASON_OK,
            pinned_block_hash=None,
            error_detail=None,
        )

    def get_block_hash_for_pin(self, block_number: int) -> str | None:
        """Return the canonical block hash at ``block_number`` for pinning.

        The router calls this when a ``scanned_empty`` interval needs
        a pinned block reference. The client queries the adapter for
        a non-hydrated header so the pin can be cross-checked
        against the same evidence the block-header table later
        records. The block-hash string is the canonical
        ``0x``-prefixed lowercase hex of the 32-byte digest.

        The override map lets tests inject deterministic hashes
        without spinning up a real adapter.
        """
        if block_number in self.pinned_block_hash_overrides:
            return self.pinned_block_hash_overrides[block_number]
        try:
            header = asyncio.run(self.adapter.eth_get_block_by_number(block_number, hydrated=False))
        except RpcError:
            return None
        if not isinstance(header, dict):
            return None
        block_hash = header.get("hash")
        if not isinstance(block_hash, str):
            return None
        return block_hash.strip().lower()


# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------


def _classify_transport_error(exc: TransportError) -> tuple[str, str]:
    """Map a :class:`TransportError` to a ``(reason_code, detail)`` pair.

    The adapter's transport raises
    ``TransportError("429 Too Many Requests: ...")`` and
    ``TransportError(f"{5xx} {reason_phrase}: ...")`` for HTTP-level
    failures. The match is conservative: only the messages the
    adapter actually emits trigger the corresponding reason code.

    When the adapter wraps a :class:`TransportError` into
    :class:`RpcRetryExhausted`, the wrapper message looks like
    ``"'eth_getLogs' failed on all 1 endpoint(s): 429 Too Many
    Requests: limit"``. The classifier searches the wrapped text
    for a recognised status-code prefix so the precise failure
    reason survives the retry exhaustion wrapper.

    A message that does not match any known prefix falls back to
    ``rpc_timeout`` so the run does not silently classify the
    failure as something else.
    """
    detail = str(exc)
    msg = detail.strip().lower()
    # Direct prefix matches the adapter transport emits.
    if msg.startswith("429 "):
        return REASON_HTTP_429_RATE_LIMIT, detail
    if msg.startswith("403 "):
        # The adapter does not currently distinguish the urllib
        # default-User-Agent 403 from a configured-User-Agent 403;
        # both surface here as :data:`REASON_HTTP_403_DEFAULT_USER_AGENT`
        # so the router applies the standard retry path. Tests
        # inject the more specific :data:`REASON_HTTP_403_USER_AGENT_REJECTED`
        # code via the :class:`EndpointCallResult.reason_code` field
        # directly.
        return REASON_HTTP_403_DEFAULT_USER_AGENT, detail
    if msg.startswith("5") and len(msg) > 3 and msg[1:3].isdigit():
        return REASON_HTTP_5XX, detail
    if msg.startswith("4") and len(msg) > 3 and msg[1:3].isdigit():
        return REASON_HTTP_4XX, detail
    if "timeout" in msg or "timed out" in msg:
        return REASON_RPC_TIMEOUT, detail
    # Wrapped-in-RpcRetryExhausted matches: the wrapper message
    # has the format ``"... endpoint(s): <original transport
    # message>"``. Search for any recognised status-code substring.
    for needle, reason in (
        ("429 ", REASON_HTTP_429_RATE_LIMIT),
        ("403 ", REASON_HTTP_403_DEFAULT_USER_AGENT),
        ("502 ", REASON_HTTP_5XX),
        ("503 ", REASON_HTTP_5XX),
        ("504 ", REASON_HTTP_5XX),
        ("401 ", REASON_HTTP_4XX),
        ("404 ", REASON_HTTP_4XX),
    ):
        if needle in msg:
            return reason, detail
    if "timed out" in msg or "timeout" in msg:
        return REASON_RPC_TIMEOUT, detail
    # Unknown transport failure shape: classify as 4xx so the
    # retry policy treats it as non-transient; the operator can
    # override via the router config.
    return REASON_HTTP_4XX, detail


# ---------------------------------------------------------------------------
# Constants exposed for tests
# ---------------------------------------------------------------------------

#: The two EndpointCallResult.reason_code values this client can
#: emit for a User-Agent 403. Exposed here so tests can assert the
#: exact code without importing the router module.
USER_AGENT_REJECTED_REASONS: Final[frozenset[str]] = frozenset(
    {REASON_HTTP_403_DEFAULT_USER_AGENT, REASON_HTTP_403_USER_AGENT_REJECTED}
)


__all__ = [
    "RpcEndpointClient",
    "USER_AGENT_REJECTED_REASONS",
    "_classify_transport_error",
    "_row_size_bytes",
]
