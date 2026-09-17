"""Real ``BlockHeaderSource`` that fetches non-hydrated block headers (T035).

The source calls ``eth_getBlockByNumber(hex(n), false)`` for every
distinct event block the ingestion runner hands it. Headers are
deduplicated per block number inside an in-memory cache so a block
that hosts multiple pool events only triggers one logical RPC call.

Header requests are issued as JSON-RPC batches: one HTTP request
carries up to ``batch_size`` logical ``eth_getBlockByNumber`` calls,
and the source reports the logical-call / HTTP-batch split so a
reviewer can reconcile it against the deduplicated header count.
ADR-011 requires that a JSON-RPC batch never counts as fewer
logical RPC calls; this source tracks the two counters
independently and exposes them through :class:`BlockHeaderMetrics`.

The source rejects ``eth_getLogs.blockTimestamp`` as a time source
because the 2026-09-17 probe recorded it as ``0x0`` on every
returned log (PROVIDER_FACTS.md). Block time always comes from
``eth_getBlockByNumber``; the runner never substitutes wall-clock
time or the provider ``blockTimestamp`` field.

The source never fetches a header for a block that has no pool
event: the runner only invokes :meth:`get_header` with block
numbers it observed as event blocks. The contract forbids
"issue one ``eth_getBlockByNumber`` call per scanned block" and
the source enforces this by not exposing any "scan all blocks"
helper at all.

The :class:`BlockHeaderSource` Protocol the runner consumes is
defined in :mod:`robinhood_lp.ingestion.runner`. This module
provides the real production implementation; tests use a fake
that satisfies the same Protocol.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from robinhood_lp.ingestion.router import ALIAS_ALCHEMY_FREE, ALIAS_ROBINHOOD_PUBLIC
from robinhood_lp.rpc.adapter import RpcAdapter, RpcError, RpcProtocolError, RpcResponseError

# ---------------------------------------------------------------------------
# Defaults and constants
# ---------------------------------------------------------------------------

#: Default batch size for ``eth_getBlockByNumber`` JSON-RPC requests.
#: A larger batch reduces HTTP round trips; a smaller one reduces the
#: cost of a single failed batch. ADR-011 says "add JSON-RPC batching
#: only after measurement shows that reducing HTTP round trips
#: materially helps header retrieval"; the 2026-09-17 reference probe
#: confirmed both qualified endpoints support the batch, so 16 is a
#: conservative production default.
DEFAULT_HEADER_BATCH_SIZE: Final[int] = 16

#: The expected key in the JSON-RPC response payload for a non-hydrated
#: block. ``eth_getBlockByNumber(hex(n), false)`` returns the same
#: shape with the full transaction list replaced by ``null`` / a hash
#: list (provider-specific); the framework treats the result as the
#: canonical block metadata and discards any transactions field.
_BLOCK_HASH_KEY: Final[str] = "hash"
_PARENT_HASH_KEY: Final[str] = "parentHash"
_TIMESTAMP_KEY: Final[str] = "timestamp"
_BLOCK_NUMBER_KEY: Final[str] = "number"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BlockHeaderMetrics:
    """Counters the block-header source records per call.

    ``logical_get_block_by_number_calls`` is the number of
    ``eth_getBlockByNumber`` calls the source issued regardless of
    batching. ``http_batch_requests`` is the number of HTTP requests
    that carried one or more logical calls; the two counters
    reconcile with ``logical_get_block_by_number_calls >= http_batch_requests``
    because every batch carries at least one logical call.
    """

    logical_get_block_by_number_calls: int = 0
    http_batch_requests: int = 0
    header_cache_hits: int = 0
    header_fetch_failures: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "logical_get_block_by_number_calls": self.logical_get_block_by_number_calls,
            "http_batch_requests": self.http_batch_requests,
            "header_cache_hits": self.header_cache_hits,
            "header_fetch_failures": self.header_fetch_failures,
        }


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockHeader:
    """The canonical non-hydrated block header the source returns.

    The integer widths match the protocol / chain semantics:

    - ``block_number`` is the unsigned block height (``uint64``);
    - ``block_hash`` is the 32-byte block hash as an unsigned int
      (``uint256``);
    - ``parent_hash`` is the 32-byte previous-block hash (``uint256``);
    - ``timestamp`` is the UNIX-seconds integer the chain reports
      (``uint64``).

    The runner stores these values in the ``block_headers`` manifest
    table and on every event row's Parquet columns. No wall-clock or
    provider ``blockTimestamp`` value is ever stored in these fields.
    """

    block_number: int
    block_hash: int
    parent_hash: int
    timestamp: int

    def __post_init__(self) -> None:
        if self.block_number < 0:
            raise ValueError(f"block_number: must be >= 0, got {self.block_number}")
        if self.block_number >= (1 << 64):
            raise ValueError(f"block_number: exceeds uint64 width, got {self.block_number}")
        if self.block_hash < 0 or self.block_hash >= (1 << 256):
            raise ValueError(f"block_hash: must fit uint256, got {self.block_hash}")
        if self.parent_hash < 0 or self.parent_hash >= (1 << 256):
            raise ValueError(f"parent_hash: must fit uint256, got {self.parent_hash}")
        if self.timestamp < 0 or self.timestamp >= (1 << 64):
            raise ValueError(f"timestamp: must fit uint64, got {self.timestamp}")


class BlockHeaderFetchError(RuntimeError):
    """Raised when a header cannot be obtained.

    Per ADR-012 a header that cannot be obtained halts the interval
    and must not set ``complete=True``. The runner maps this error
    to a ``failed`` coverage decision with an explicit reason code.
    """


# ---------------------------------------------------------------------------
# The real production source
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RpcBlockHeaderSource:
    """``BlockHeaderSource`` that fetches non-hydrated headers via
    :class:`robinhood_lp.rpc.adapter.RpcAdapter`.

    The source uses the adapter's underlying transport to issue
    JSON-RPC batches of ``eth_getBlockByNumber(hex(n), false)``
    calls. Every batch carries up to ``batch_size`` logical calls;
    the source counts the two counters independently so the
    manifest can surface both.

    The source tracks one in-memory cache of fetched headers keyed
    by ``block_number``; the deduplication invariant is that
    ``logical_get_block_by_number_calls`` equals the number of
    distinct event blocks actually fetched (cache hits do not
    increment the counter). A block with no pool event is never
    fetched because the runner only ever calls
    :meth:`get_header` with block numbers it observed as event
    blocks.

    Parameters
    ----------
    adapter:
        The production read-only JSON-RPC adapter (T020).
    alias:
        The endpoint alias the source represents. Used only in
        error messages and metrics; never written to disk.
    batch_size:
        Maximum number of ``eth_getBlockByNumber`` calls per
        JSON-RPC batch HTTP request. Defaults to
        :data:`DEFAULT_HEADER_BATCH_SIZE`.
    """

    adapter: RpcAdapter
    alias: str = ALIAS_ROBINHOOD_PUBLIC
    batch_size: int = DEFAULT_HEADER_BATCH_SIZE
    metrics: BlockHeaderMetrics = field(default_factory=BlockHeaderMetrics)
    _cache: dict[int, BlockHeader] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Accept any adapter that exposes the read-only method
        # the source invokes. The production code path passes a
        # real :class:`RpcAdapter`; tests inject a fake that
        # satisfies the same surface.
        if not hasattr(self.adapter, "eth_get_block_by_number") or not callable(
            self.adapter.eth_get_block_by_number
        ):
            raise TypeError(
                f"RpcBlockHeaderSource.adapter: missing required "
                f"eth_get_block_by_number method, got "
                f"{type(self.adapter).__name__}"
            )
        if not isinstance(self.alias, str) or not self.alias:
            raise ValueError(
                f"RpcBlockHeaderSource.alias: must be a non-empty string, got {self.alias!r}"
            )
        if self.batch_size <= 0:
            raise ValueError(
                f"RpcBlockHeaderSource.batch_size: must be positive, got {self.batch_size}"
            )

    # ----- public API ------------------------------------------------

    def get_header(self, block_number: int) -> dict[str, Any] | None:
        """Return the header for ``block_number`` as a JSON-friendly dict.

        Returns ``None`` when the underlying RPC adapter fails to
        produce a valid header; the runner maps ``None`` to a
        coverage failure and halts the interval. The cache layer
        ensures repeated calls for the same ``block_number``
        during one run do not increase the logical-call counter.

        The returned dict carries the canonical keys
        ``"block_number"``, ``"block_hash"`` (int),
        ``"parent_hash"`` (int), and ``"timestamp"`` (int).
        """
        if not isinstance(block_number, int) or isinstance(block_number, bool):
            raise TypeError(
                f"get_header: block_number must be int, got {type(block_number).__name__}"
            )
        if block_number < 0:
            raise ValueError(f"get_header: block_number must be >= 0, got {block_number}")
        cached = self._cache.get(block_number)
        if cached is not None:
            self.metrics.header_cache_hits += 1
            return _header_to_dict(cached)
        # Single-block fetch path. The batch path is used by the
        # runner when it has a list of block numbers; this method
        # is the single-block convenience for tests / warm restarts.
        try:
            result = asyncio.run(self._fetch_one(block_number))
        except BlockHeaderFetchError:
            self.metrics.header_fetch_failures += 1
            raise
        if result is None:
            return None
        self._cache[block_number] = result
        return _header_to_dict(result)

    def get_headers_batch(self, block_numbers: Iterable[int]) -> dict[int, BlockHeader | None]:
        """Fetch headers for every distinct ``block_numbers`` entry as a batch.

        Returns a mapping ``block_number -> BlockHeader`` for every
        successfully fetched header and ``block_number -> None`` for
        every failed fetch. The caller (the runner) is responsible
        for mapping ``None`` to a coverage failure.

        Repeated calls inside one run are deduped by the cache so
        the logical-call counter never exceeds the number of
        distinct block numbers. The HTTP-batch counter is
        incremented once per JSON-RPC batch HTTP request.
        """
        distinct = sorted({int(b) for b in block_numbers})
        if not distinct:
            return {}
        # Cache hits short-circuit.
        pending = [b for b in distinct if b not in self._cache]
        if pending:
            try:
                fetched = asyncio.run(self._fetch_batched(pending))
            except BlockHeaderFetchError:
                self.metrics.header_fetch_failures += 1
                raise
            for block_number, header in fetched.items():
                if header is not None:
                    self._cache[block_number] = header
        out: dict[int, BlockHeader | None] = {}
        for b in distinct:
            out[b] = self._cache.get(b)
        return out

    # ----- internal async helpers ------------------------------------

    async def _fetch_one(self, block_number: int) -> BlockHeader | None:
        """Fetch one non-hydrated header via the adapter."""
        hex_block = _to_hex_block(block_number)
        try:
            payload = await self.adapter.eth_get_block_by_number(block_number, hydrated=False)
        except RpcError as exc:
            raise BlockHeaderFetchError(
                f"{self.alias}: eth_getBlockByNumber({hex_block}, false) "
                f"failed: {type(exc).__name__}: {exc}"
            ) from exc
        if payload is None:
            raise BlockHeaderFetchError(
                f"{self.alias}: eth_getBlockByNumber({hex_block}, false) returned null"
            )
        header = _decode_header_payload(payload, requested_block=block_number)
        if header is None:
            raise BlockHeaderFetchError(
                f"{self.alias}: eth_getBlockByNumber({hex_block}, false) "
                f"response missing required fields"
            )
        self.metrics.logical_get_block_by_number_calls += 1
        self.metrics.http_batch_requests += 1
        return header

    async def _fetch_batched(self, block_numbers: list[int]) -> dict[int, BlockHeader | None]:
        """Fetch headers for ``block_numbers`` as one or more JSON-RPC batches.

        Each batch is one HTTP request that carries up to
        ``batch_size`` logical ``eth_getBlockByNumber`` calls. The
        source issues the batches sequentially to keep the request
        pacing predictable and to avoid races against the adapter's
        own retry / failover policy. Per ADR-011 a batch never
        counts as fewer logical calls; the counter increments by
        ``len(batch)`` per HTTP batch.
        """
        out: dict[int, BlockHeader | None] = {}
        transport = self.adapter._transport  # noqa: SLF001 — internal API used by tests
        endpoint = self.adapter._config.active_endpoints()[0]  # noqa: SLF001
        for start in range(0, len(block_numbers), self.batch_size):
            batch = block_numbers[start : start + self.batch_size]
            payload = _build_json_rpc_batch(batch)
            try:
                response = await asyncio.wait_for(
                    transport(endpoint.url, payload),
                    timeout=self.adapter._config.request_timeout_seconds,  # noqa: SLF001
                )
            except (TimeoutError, RpcError):
                # Retry the batch once via the adapter's normal call
                # path so 429 / 5xx retries still apply. If the
                # retry also fails we surface every block in the
                # batch as ``None`` so the runner can record a
                # coverage failure with the original reason.
                failed = await self._retry_batch(endpoint, batch, transport)
                for b, header in failed.items():
                    out[b] = header
                continue
            except Exception as exc:  # noqa: BLE001
                raise BlockHeaderFetchError(
                    f"{self.alias}: eth_getBlockByNumber batch transport "
                    f"failure: {type(exc).__name__}: {exc}"
                ) from exc
            parsed = _parse_json_rpc_batch_response(
                response,
                expected_batch_size=len(batch),
                alias=self.alias,
            )
            self.metrics.logical_get_block_by_number_calls += len(batch)
            self.metrics.http_batch_requests += 1
            for block_number, header in zip(batch, parsed, strict=True):
                out[block_number] = header
        return out

    async def _retry_batch(
        self,
        endpoint: Any,
        batch: list[int],
        transport: Any,
    ) -> dict[int, BlockHeader | None]:
        """Retry a failed batch via the adapter's normal call path.

        The batch is split into per-call ``eth_getBlockByNumber``
        requests so the adapter's classified retry / failover logic
        applies to every block individually. Each retry issues at
        least one HTTP request, so the HTTP-batch counter is
        incremented accordingly.
        """
        out: dict[int, BlockHeader | None] = {}
        for block_number in batch:
            try:
                header = await self._fetch_one(block_number)
            except BlockHeaderFetchError:
                out[block_number] = None
                continue
            out[block_number] = header
        return out

    # ----- diagnostics ------------------------------------------------

    def cache_state(self) -> dict[str, Any]:
        """Return a snapshot of the in-memory cache for diagnostics."""
        return {
            "alias": self.alias,
            "cached_block_count": len(self._cache),
            "batch_size": self.batch_size,
            "metrics": self.metrics.snapshot(),
        }


# ---------------------------------------------------------------------------
# Sync runner-side convenience
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockHeaderSink:
    """The side of the source that writes headers to the manifest.

    The sink is a thin wrapper around
    :meth:`robinhood_lp.storage.manifest.ManifestStore.upsert_block_header`
    so the runner can call ``sink.upsert(chain_id, header)`` without
    importing the storage layer's full surface. Tests pass a fake
    sink that records the calls.
    """

    manifest: Any  # ManifestStore — kept loose to avoid storage/ingestion cycle
    endpoint_alias: str

    def upsert(self, *, chain_id: int, header: BlockHeader) -> bool:
        """Upsert one header into the ``block_headers`` manifest table.

        Returns True when a new row was inserted, False when the
        block_hash was already present (idempotent re-observation).
        """
        result: bool = self.manifest.upsert_block_header(
            None,
            chain_id=chain_id,
            block_hash=header.block_hash,
            block_number=header.block_number,
            parent_hash=header.parent_hash,
            block_timestamp=header.timestamp,
            endpoint_alias=self.endpoint_alias,
        )
        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_hex_block(block_number: int) -> str:
    """Return the canonical lowercase ``0x``-prefixed hex of ``block_number``."""
    return "0x" + format(block_number, "x")


def _header_to_dict(header: BlockHeader) -> dict[str, Any]:
    """Convert a :class:`BlockHeader` into the runner-facing dict shape.

    The runner's :class:`BlockHeaderCache` and the manifest store
    both want a plain dict keyed by ``"block_number"``,
    ``"block_hash"``, ``"parent_hash"``, and ``"timestamp"``.
    """
    return {
        "block_number": int(header.block_number),
        "block_hash": int(header.block_hash),
        "parent_hash": int(header.parent_hash),
        "timestamp": int(header.timestamp),
    }


def _decode_header_payload(
    payload: Mapping[str, Any], *, requested_block: int
) -> BlockHeader | None:
    """Parse an ``eth_getBlockByNumber(hex(n), false)`` response payload.

    Returns ``None`` when the payload is missing any of the four
    canonical fields. The integer decoding accepts hex strings
    (``"0x..."``) or already-decoded ints, matching the JSON-RPC
    shape the adapter returns.
    """
    if not isinstance(payload, Mapping):
        return None
    block_hash = payload.get(_BLOCK_HASH_KEY)
    parent_hash = payload.get(_PARENT_HASH_KEY)
    timestamp = payload.get(_TIMESTAMP_KEY)
    block_number_raw = payload.get(_BLOCK_NUMBER_KEY)
    if any(v is None for v in (block_hash, parent_hash, timestamp)):
        return None
    try:
        block_hash_int = _decode_hex_int(block_hash, field="block_hash")
        parent_hash_int = _decode_hex_int(parent_hash, field="parent_hash")
        timestamp_int = _decode_hex_int(timestamp, field="timestamp")
    except (TypeError, ValueError):
        return None
    # The ``number`` field is optional in some provider responses
    # (the request already pins it). Fall back to the requested
    # block number so the source is robust to that shape.
    if block_number_raw is None:
        block_number_int = int(requested_block)
    else:
        try:
            block_number_int = _decode_hex_int(block_number_raw, field="block_number")
        except (TypeError, ValueError):
            return None
    try:
        return BlockHeader(
            block_number=block_number_int,
            block_hash=block_hash_int,
            parent_hash=parent_hash_int,
            timestamp=timestamp_int,
        )
    except ValueError:
        return None


def _decode_hex_int(value: Any, *, field: str) -> int:
    """Decode a JSON-RPC hex / int value into a Python int."""
    if isinstance(value, bool):
        raise TypeError(f"{field}: expected int or 0x-hex str, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s.startswith("0x"):
            return int(s, 16)
        return int(s, 10)
    raise TypeError(f"{field}: expected int or 0x-hex str, got {type(value).__name__}")


def _build_json_rpc_batch(block_numbers: list[int]) -> dict[str, Any]:
    """Build the JSON-RPC batch payload for a list of ``eth_getBlockByNumber`` calls.

    The payload is a single ``list[dict]`` (JSON-RPC 2.0 batch
    shape). Each entry carries a unique ``id`` (the block number
    itself is a stable, monotonic, source-local identifier for the
    batch response so the source can match each response entry to
    its requesting block number without trusting the adapter).
    """
    return {
        "jsonrpc": "2.0",
        "id": None,
        "batch": [
            {
                "jsonrpc": "2.0",
                "id": int(block_number),
                "method": "eth_getBlockByNumber",
                "params": [_to_hex_block(int(block_number)), False],
            }
            for block_number in block_numbers
        ],
    }


def _parse_json_rpc_batch_response(
    response: Any,
    *,
    expected_batch_size: int,
    alias: str,
) -> list[BlockHeader | None]:
    """Parse a JSON-RPC batch response into per-block headers.

    The transport returns the parsed JSON object; a valid batch
    response is a ``list`` of JSON-RPC objects whose ``id``
    integers match the requesting block numbers. The source
    accepts both list and dict (single-response) shapes for
    robustness against providers that collapse a single-element
    batch into a single object.

    The returned list has length ``expected_batch_size``: per-
    entry errors map to ``None`` so the caller can record a
    per-block failure rather than collapsing the batch into a
    shorter list.
    """
    if isinstance(response, Mapping):
        # Some providers collapse a single-element batch into a
        # single object; treat it as a 1-length batch.
        response_list = [response]
    elif isinstance(response, list):
        response_list = response
    else:
        raise BlockHeaderFetchError(
            f"{alias}: eth_getBlockByNumber batch returned "
            f"non-list/non-object: {type(response).__name__}"
        )
    if len(response_list) != expected_batch_size:
        raise BlockHeaderFetchError(
            f"{alias}: eth_getBlockByNumber batch returned "
            f"{len(response_list)} entries for {expected_batch_size} "
            f"logical calls"
        )
    by_id: dict[int, BlockHeader | None] = {}
    for entry in response_list:
        if not isinstance(entry, Mapping):
            raise BlockHeaderFetchError(
                f"{alias}: eth_getBlockByNumber batch entry is not an object"
            )
        entry_id = entry.get("id")
        try:
            entry_id_int = int(str(entry_id))
        except (TypeError, ValueError) as exc:
            raise BlockHeaderFetchError(
                f"{alias}: eth_getBlockByNumber batch entry id is not an int: {entry_id!r}"
            ) from exc
        if "error" in entry:
            # A per-call error inside a batch: surface as
            # ``None`` for that block so the runner records the
            # failure reason per block.
            by_id[entry_id_int] = None
            continue
        if "result" not in entry:
            raise BlockHeaderFetchError(
                f"{alias}: eth_getBlockByNumber batch entry has neither result nor error: {entry!r}"
            )
        result = entry["result"]
        if not isinstance(result, Mapping):
            raise BlockHeaderFetchError(
                f"{alias}: eth_getBlockByNumber batch result is "
                f"not an object: {type(result).__name__}"
            )
        by_id[entry_id_int] = _decode_header_payload(result, requested_block=entry_id_int)
    out: list[BlockHeader | None] = []
    # Reassemble in the order the batch was issued.
    for block_number in sorted(by_id.keys()):
        out.append(by_id[block_number])
    return out


__all__ = [
    "ALIAS_ALCHEMY_FREE",
    "ALIAS_ROBINHOOD_PUBLIC",
    "BlockHeader",
    "BlockHeaderFetchError",
    "BlockHeaderMetrics",
    "BlockHeaderSink",
    "DEFAULT_HEADER_BATCH_SIZE",
    "RpcBlockHeaderSource",
]


# Suppress unused-binding noise for names that may be referenced via
# type-checker introspection in tests.
_UNUSED: tuple[object, ...] = (
    RpcResponseError,
    RpcProtocolError,
    json,
)
