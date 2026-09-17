"""Versioned raw and normalized storage schemas (T030).

Every record carries:

- the **raw** JSON-RPC fields (block_hash, block_number,
  transaction_hash, log_index, topics, data, address, etc.)
  preserved as-is so a future re-decode against a newer schema does
  not lose information;
- the **normalized** typed fields, parsed by the current decoder;
- **provenance**: ``schema_version`` (the version of this dataclass
  shape), ``decode_version`` (the version of the V4 artifacts in
  use when the record was decoded), the ``AcquisitionProvenance``
  (endpoint alias, retrieval time, request interval, HTTP batch
  metadata) which is observational and excluded from the
  normalized content hash, and the legacy ``source_endpoint`` /
  ``ingestion_time`` fields carried for back-compat with v1 records.

The canonical byte form (``canonical_bytes``) is stable and round-
trippable: ``from_canonical_bytes(canonical_bytes(record)) ==
record`` byte-exactly (with the documented v1→v2 migration applied
on load). New fields added in a future schema version appear in the
canonical form but unknown fields are preserved as
``unknown_fields`` so v(N) decoders can still load v(N+1) records.

The normalized content hash (``normalized_content_hash``) is the
SHA-256 of the canonical record with observational provenance
removed. Two records of the same chain event fetched from
different endpoints (or at different times) produce the same
normalized content hash but retain both acquisition envelopes
separately. Same-height fork logs have different ``block_hash``
fields, so their EventKey identity and normalized content hash
also differ.

This module sits in the storage layer per ADR-006 and depends only
on ``robinhood_lp.protocol`` and the JSON stdlib. It must not
import ``robinhood_lp.config`` or higher layers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from robinhood_lp.protocol import Address, ChainId, EventKey, PoolId

# ---------------------------------------------------------------------------
# Schema / decode versions
# ---------------------------------------------------------------------------


#: Bump whenever the dataclass shape of any record in this module
#: changes. The bump is recorded in every record's ``schema_version``
#: field so a future decoder can branch on it.
#:
#: v1 → v2 migration:
#: - added ``AcquisitionProvenance`` observational envelope
#:   (endpoint alias, retrieval time, request interval, HTTP batch
#:   metadata);
#: - added ``removed`` flag on every log record;
#: - added ``transaction_index`` on every log record;
#: - added ``ProtocolFeeUpdatedLogRecord`` for the inherited
#:   ``IProtocolFees.ProtocolFeeUpdated(bytes32,uint24)`` event
#:   (ADR-010 §"Required data boundary").
#:
#: v2 → v3 migration (T035, ADR-012):
#: - every log record now carries the integer ``block_timestamp``
#:   (uint64, the on-chain UNIX-seconds block time the non-hydrated
#:   block header reports) and the ``parent_hash`` (uint256, the
#:   parent block hash the same header reports). The runner fills
#:   these from the dedup'd ``block_headers`` manifest table before
#:   persisting the record; v2 records migrated forward default to
#:   ``0`` and the framework treats them as legacy rows whose header
#:   was not retained at decode time.
#:
#: v1 and v2 records load successfully through ``migrate_to_current``;
#: the legacy ``source_endpoint``/``ingestion_time`` fields are still
#: carried for back-compat but new code writes the structured
#: ``acquisition`` field instead.
CURRENT_SCHEMA_VERSION: int = 3

#: Bump whenever the V4 artifacts (``EVENT_TOPICS`` /
#: ``FUNCTION_SELECTORS`` / PoolId hashing) change. The bump is
#: recorded in every record's ``decode_version`` field so a future
#: decoder can detect artifacts drift without reading the JSON.
#:
#: v1 → v2 (this task): the pinned artifact
#: ``docs/implement/protocol-artifacts/v4-core-e50237c.json`` adds
#: the ``ProtocolFeeUpdated(bytes32,uint24)`` event from the same
#: pinned v4-core commit. Records decoded before this artifact bump
#: still load through ``migrate_to_current``; their ``decode_version``
#: is preserved so downstream code can branch on artifacts drift
#: without reading the JSON.
CURRENT_DECODE_VERSION: int = 2

#: Field names excluded from ``normalized_content_hash`` because they
#: carry observational provenance rather than chain-event identity.
#: ``raw`` / ``raw_topics`` / ``raw_data`` are also observational
#: (the decoded typed fields are authoritative) but are listed
#: separately because they appear on every record class.
PROVENANCE_FIELD_NAMES: frozenset[str] = frozenset(
    {"acquisition", "ingestion_time", "source_endpoint"}
)
RAW_FIELD_NAMES: frozenset[str] = frozenset({"raw", "raw_topics", "raw_data", "unknown_fields"})


# ---------------------------------------------------------------------------
# Class registry (for ``migrate_to_current``)
# ---------------------------------------------------------------------------


#: Mapping from the canonical ``__class__`` envelope value to the
#: dataclass that should materialise it. Populated at the bottom of
#: this module after every dataclass is defined. ``migrate_to_current``
#: uses it to instantiate the right type without forcing the caller
#: to know about the registry.
_CLASS_REGISTRY: dict[str, type] = {}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO 8601 UTC timestamp with second precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _require_uint(value: int, *, bits: int, field: str) -> int:
    """Validate ``value`` is a non-negative integer fitting in ``bits`` bits."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field}: must be non-negative, got {value}")
    upper = 1 << bits
    if value >= upper:
        raise ValueError(f"{field}: exceeds {bits}-bit width, got {value}")
    return value


def _validate_alias(alias: str, *, field: str) -> str:
    """Reject an endpoint alias that looks like a credential-bearing URL.

    T030 must-not: 'Request provenance never contains a credential-bearing
    URL'. The alias is an opaque short token (e.g. ``robinhood_public``,
    ``alchemy_free``); anything containing ``://``, ``@``, ``?``,
    whitespace, or path separators is rejected.
    """
    if not isinstance(alias, str):
        raise TypeError(f"{field}: must be str, got {type(alias).__name__}")
    stripped = alias.strip()
    if not stripped:
        # An empty alias is permitted (record created before any
        # acquisition; downstream code may set it later). Only reject
        # the credential-shaped values.
        return stripped
    bad = ("://", "@", "?", "/", "\\", " ", "\t", "\n")
    for needle in bad:
        if needle in alias:
            raise ValueError(
                f"{field}: alias {alias!r} looks credential-bearing "
                f"(contains {needle!r}); use a short opaque token instead"
            )
    return alias


def _validate_block_timestamp(value: int, *, field: str) -> int:
    """Validate ``block_timestamp`` fits the uint64 width and is non-negative.

    T035 / ADR-012 require the integer block timestamp the
    non-hydrated ``eth_getBlockByNumber`` header returns. Wall-clock
    and provider ``blockTimestamp`` substitutions are rejected
    elsewhere; this validator is the field-level safety net.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field}: must be non-negative, got {value}")
    if value >= (1 << 64):
        raise ValueError(f"{field}: exceeds uint64 width, got {value}")
    return value


def _validate_parent_hash(value: int, *, field: str) -> int:
    """Validate ``parent_hash`` fits the uint256 width and is non-negative.

    The parent hash the non-hydrated header reports is the 32-byte
    previous-block hash. ``0`` is reserved as the legacy / migrated
    placeholder for v1 / v2 records whose header was not retained
    at decode time.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field}: must be non-negative, got {value}")
    if value >= (1 << 256):
        raise ValueError(f"{field}: exceeds uint256 width, got {value}")
    return value


# ---------------------------------------------------------------------------
# Acquisition provenance (observational envelope)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AcquisitionProvenance:
    """Append-only acquisition envelope — observational provenance.

    Two log records of the same chain event fetched from different
    endpoints (or at different times) carry distinct
    ``AcquisitionProvenance`` instances. The structured envelope
    lives alongside the raw ``raw`` / ``raw_topics`` / ``raw_data``
    evidence and is excluded from ``normalized_content_hash``.

    Every field is metadata about *how* the record was obtained,
    not about *what* chain event was observed. The endpoint alias
    is a short opaque token (e.g. ``robinhood_public``,
    ``alchemy_free``) — never a URL — so credential leakage is
    structurally impossible.

    Attributes:
        endpoint_alias: short opaque endpoint identifier; never a URL
            and never credential-bearing. Validated by the
            constructor.
        retrieval_time: ISO 8601 UTC timestamp at which the RPC call
            returned. Observational; does not enter the normalized
            content hash.
        request_from_block: inclusive lower bound of the JSON-RPC
            log-range request that produced this record.
        request_to_block: inclusive upper bound of the same request.
        http_batch_size: number of JSON-RPC requests in the HTTP
            batch this record came from (1 for non-batched calls);
            ``None`` when batching is unknown.
        http_batch_position: 0-based position of the producing
            request within its HTTP batch; ``None`` when batching is
            unknown.
        request_attempt: 1-based retry counter for the producing
            request; ``None`` when not retried.
    """

    endpoint_alias: str = ""
    retrieval_time: str = field(default_factory=_now_iso)
    request_from_block: int = 0
    request_to_block: int = 0
    http_batch_size: int | None = None
    http_batch_position: int | None = None
    request_attempt: int | None = None

    def __post_init__(self) -> None:
        _validate_alias(self.endpoint_alias, field="AcquisitionProvenance.endpoint_alias")
        for name in ("request_from_block", "request_to_block"):
            value = getattr(self, name)
            _require_uint(value, bits=256, field=f"AcquisitionProvenance.{name}")
        if self.request_from_block > self.request_to_block:
            raise ValueError(
                "AcquisitionProvenance: request_from_block "
                f"{self.request_from_block} > request_to_block "
                f"{self.request_to_block}"
            )
        if self.http_batch_size is not None:
            _require_uint(
                self.http_batch_size, bits=32, field="AcquisitionProvenance.http_batch_size"
            )
        if self.http_batch_position is not None:
            _require_uint(
                self.http_batch_position,
                bits=32,
                field="AcquisitionProvenance.http_batch_position",
            )
        if (
            self.http_batch_size is not None
            and self.http_batch_position is not None
            and self.http_batch_position >= self.http_batch_size
        ):
            raise ValueError(
                "AcquisitionProvenance: http_batch_position "
                f"{self.http_batch_position} >= http_batch_size "
                f"{self.http_batch_size}"
            )
        if self.request_attempt is not None and self.request_attempt < 1:
            raise ValueError(
                f"AcquisitionProvenance.request_attempt: must be >= 1, got {self.request_attempt}"
            )


# ---------------------------------------------------------------------------
# Block and transaction contexts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockContext:
    """A block as observed by ``eth_getBlockByNumber``.

    The raw fields are the subset of the JSON-RPC response the
    framework consumes. ``canonical_bytes`` preserves every field
    byte-for-byte; a future schema version may add fields without
    breaking older records.
    """

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    block_number: int
    block_hash: int
    parent_hash: int
    timestamp: int
    miner: Address
    gas_used: int
    gas_limit: int
    base_fee_per_gas: int | None

    # Provenance and audit
    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    acquisition: AcquisitionProvenance = field(default_factory=AcquisitionProvenance)
    # Raw response fields we did not normalise. A re-decode against a
    # later schema can pick these up without re-querying the chain.
    raw: dict[str, Any] = field(default_factory=dict)
    # Raw fields that the current schema does not know about. They
    # survive round-trip; a future schema version promotes them into
    # typed attributes and removes them from this dict.
    unknown_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TransactionContext:
    """A transaction as observed by ``eth_getTransactionByHash``."""

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    tx_hash: int
    block_hash: int
    block_number: int
    transaction_index: int
    from_address: Address
    to_address: Address | None
    value: int
    input: bytes
    nonce: int

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    acquisition: AcquisitionProvenance = field(default_factory=AcquisitionProvenance)
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReceiptContext:
    """A transaction receipt as observed by ``eth_getTransactionReceipt``.

    ``logs_bloom`` is the raw 256-byte bloom filter (int) the
    framework does not currently interpret; it is kept as raw for
    audit and possible future re-derivation.
    """

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    tx_hash: int
    block_hash: int
    block_number: int
    transaction_index: int
    from_address: Address
    to_address: Address | None
    contract_address: Address | None
    gas_used: int
    cumulative_gas_used: int
    status: int
    logs_bloom: int | None

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    acquisition: AcquisitionProvenance = field(default_factory=AcquisitionProvenance)
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# V4 event records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InitializeLogRecord:
    """The Initialize event emitted by V4 PoolManager.

    The raw fields are the topic/data bytes the decoder consumed; the
    typed fields are the decoded PoolKey. A future schema that learns
    additional event fields can extend this without losing data.
    """

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    transaction_index: int
    log_index: int
    address: Address  # PoolManager address

    # ---- T035 / ADR-012: block header time + parent hash --------------
    # Both fields default to ``0`` so legacy v1 / v2 records migrate
    # forward without losing bytes; the runner enriches every freshly
    # persisted record from the dedup'd ``block_headers`` manifest
    # table before the partition writer commits.
    block_timestamp: int = 0
    parent_hash: int = 0

    # Re-org / removed flag from ``eth_getLogs``; True for log entries
    # the chain rolled back. Default False for fresh records.
    removed: bool = False

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    acquisition: AcquisitionProvenance = field(default_factory=AcquisitionProvenance)
    raw_topics: list[bytes] = field(default_factory=list)
    raw_data: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)

    def event_key(self) -> EventKey:
        """Return the T011 identity for this log entry."""
        return EventKey(
            chain_id=self.chain_id,
            block_hash=self.block_hash,
            tx_hash=self.transaction_hash,
            log_index=self.log_index,
        )

    def sort_key(self) -> tuple[int, int, int]:
        """Deterministic ordering: ``(block_number, transaction_index, log_index)``.

        T030 acceptance: same chain events are deterministically
        orderable; two forks at the same block number remain distinct
        via ``block_hash`` (EventKey), not via the sort key.
        """
        return (self.block_number, self.transaction_index, self.log_index)


@dataclass(frozen=True, slots=True)
class ModifyLiquidityLogRecord:
    """The ModifyLiquidity event emitted by V4 PoolManager."""

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    transaction_index: int
    log_index: int
    address: Address
    sender: Address
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    salt: int

    # ---- T035 / ADR-012: block header time + parent hash --------------
    block_timestamp: int = 0
    parent_hash: int = 0

    removed: bool = False

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    acquisition: AcquisitionProvenance = field(default_factory=AcquisitionProvenance)
    raw_topics: list[bytes] = field(default_factory=list)
    raw_data: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)

    def event_key(self) -> EventKey:
        return EventKey(
            chain_id=self.chain_id,
            block_hash=self.block_hash,
            tx_hash=self.transaction_hash,
            log_index=self.log_index,
        )

    def sort_key(self) -> tuple[int, int, int]:
        return (self.block_number, self.transaction_index, self.log_index)


@dataclass(frozen=True, slots=True)
class SwapLogRecord:
    """The Swap event emitted by V4 PoolManager.

    Note: ``fee`` is the fee recorded by the Swap event itself.
    The framework does **not** reconstruct the LP-owned protocol
    fee between swaps — that signal is carried separately by the
    inherited ``ProtocolFeeUpdatedLogRecord`` (ADR-010 §"Required
    data boundary") and reconstructed as evidence by T043.
    """

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    transaction_index: int
    log_index: int
    address: Address
    sender: Address
    amount0: int  # signed delta of currency0 balance of the pool
    amount1: int  # signed delta of currency1 balance of the pool
    sqrt_price_x96: int
    liquidity: int
    tick: int
    fee: int  # the on-chain recorded effective fee, in hundredths of a bip

    # ---- T035 / ADR-012: block header time + parent hash --------------
    block_timestamp: int = 0
    parent_hash: int = 0

    removed: bool = False

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    acquisition: AcquisitionProvenance = field(default_factory=AcquisitionProvenance)
    raw_topics: list[bytes] = field(default_factory=list)
    raw_data: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)

    def event_key(self) -> EventKey:
        return EventKey(
            chain_id=self.chain_id,
            block_hash=self.block_hash,
            tx_hash=self.transaction_hash,
            log_index=self.log_index,
        )

    def sort_key(self) -> tuple[int, int, int]:
        return (self.block_number, self.transaction_index, self.log_index)


@dataclass(frozen=True, slots=True)
class DonateLogRecord:
    """The Donate event emitted by V4 PoolManager."""

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    transaction_index: int
    log_index: int
    address: Address
    sender: Address
    amount0: int
    amount1: int

    # ---- T035 / ADR-012: block header time + parent hash --------------
    block_timestamp: int = 0
    parent_hash: int = 0

    removed: bool = False

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    acquisition: AcquisitionProvenance = field(default_factory=AcquisitionProvenance)
    raw_topics: list[bytes] = field(default_factory=list)
    raw_data: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)

    def event_key(self) -> EventKey:
        return EventKey(
            chain_id=self.chain_id,
            block_hash=self.block_hash,
            tx_hash=self.transaction_hash,
            log_index=self.log_index,
        )

    def sort_key(self) -> tuple[int, int, int]:
        return (self.block_number, self.transaction_index, self.log_index)


@dataclass(frozen=True, slots=True)
class ProtocolFeeUpdatedLogRecord:
    """The ``ProtocolFeeUpdated`` event inherited by V4 PoolManager.

    Declared in ``v4-core/src/interfaces/IProtocolFees.sol`` at the
    pinned commit ``e50237c43811bd9b526eff40f26772152a42daba`` and
    emitted by ``ProtocolFees.sol::setProtocolFee`` whenever the
    PoolManager's LP-owned protocol-fee accumulator changes.

    The recorded value is a packed ``uint24``: the high 12 bits
    encode the token-0 protocol fee and the low 12 bits encode the
    token-1 protocol fee (per the IProtocolFees ABI). The framework
    stores the integer exactly as emitted; downstream code splits
    it into the two halves as needed.

    ADR-010 requires this event because the fee in ``Swap`` is the
    *combined* swap fee, not automatically the LP-owned share.
    """

    schema_version: ClassVar[int] = CURRENT_SCHEMA_VERSION

    chain_id: ChainId
    pool_id: PoolId
    block_number: int
    block_hash: int
    transaction_hash: int
    transaction_index: int
    log_index: int
    address: Address
    protocol_fee: int  # uint24 — packed [token0Fee:12 | token1Fee:12]

    # ---- T035 / ADR-012: block header time + parent hash --------------
    block_timestamp: int = 0
    parent_hash: int = 0

    removed: bool = False

    decode_version: int = CURRENT_DECODE_VERSION
    ingestion_time: str = field(default_factory=_now_iso)
    source_endpoint: str = ""
    acquisition: AcquisitionProvenance = field(default_factory=AcquisitionProvenance)
    raw_topics: list[bytes] = field(default_factory=list)
    raw_data: bytes = b""
    raw: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_uint(self.protocol_fee, bits=24, field="ProtocolFeeUpdatedLogRecord.protocol_fee")

    def event_key(self) -> EventKey:
        return EventKey(
            chain_id=self.chain_id,
            block_hash=self.block_hash,
            tx_hash=self.transaction_hash,
            log_index=self.log_index,
        )

    def sort_key(self) -> tuple[int, int, int]:
        return (self.block_number, self.transaction_index, self.log_index)


# ---------------------------------------------------------------------------
# Canonical byte form (round-trippable)
# ---------------------------------------------------------------------------


def _canonical(record: Any) -> dict[str, Any]:
    """Convert a record dataclass to a JSON-friendly dict.

    Bytes are encoded as ``0x``-prefixed lowercase hex so the canonical
    form is human-readable and diff-friendly. ``dataclasses.asdict``
    recurses through nested dataclasses, but it does not handle bytes
    inside lists; we post-process to convert those.
    """

    def _convert(value: Any) -> Any:
        if isinstance(value, bytes):
            return value.hex()
        if isinstance(value, list):
            return [_convert(v) for v in value]
        if isinstance(value, dict):
            # Flatten ``{"value": <int>}`` (the asdict() expansion of a
            # dataclass whose only field is ``value: int`` -- ChainId,
            # PoolId, Address) into the bare int. This keeps the
            # canonical form diff-friendly.
            if set(value.keys()) == {"value"} and isinstance(value["value"], int):
                return value["value"]
            return {k: _convert(v) for k, v in value.items()}
        return value

    data = asdict(record)
    return _convert(data)  # type: ignore[no-any-return]


def canonical_bytes(record: Any) -> bytes:
    """Return the canonical byte form of ``record``.

    The form is JSON with sorted keys, UTF-8 encoded. Two records
    with the same fields produce identical bytes; two records that
    differ in any field produce different bytes. Every typed field
    plus the entire ``raw`` / ``unknown_fields`` dicts survive the
    round-trip.
    """
    payload = _canonical(record)
    payload["__schema_version__"] = record.schema_version
    payload["__decode_version__"] = record.decode_version
    payload["__class__"] = type(record).__name__
    return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")


def from_canonical_bytes(blob: bytes) -> dict[str, Any]:
    """Parse a canonical blob back into its dataclass-friendly dict.

    The caller is responsible for instantiating the correct
    dataclass (the class name is carried in ``__class__`` but the
    schema layer does not import the dataclasses to avoid a cycle).

    Schema-version compatibility: a future schema may add fields; the
    current parser accepts unknown keys (they land in
    ``unknown_fields`` on the calling side).
    """
    return json.loads(blob.decode("utf-8"))  # type: ignore[no-any-return]


def migrate_to_current(blob: bytes) -> Any:
    """Parse a canonical blob and return the corresponding current-version record.

    Accepts blobs produced by ``canonical_bytes`` at any past schema
    version up to and including the current one. Returns a
    constructed dataclass instance whose type is read from the
    ``__class__`` envelope field. Missing fields are backfilled with
    safe defaults; fields the current schema does not know about
    are merged into ``unknown_fields`` so the record can still be
    serialised back to canonical bytes without losing data.

    v1 → v2 migration (this task):

    - ``removed`` defaults to ``False`` on every log record;
    - ``transaction_index`` defaults to ``0`` on every log record;
    - ``acquisition`` is populated from the legacy ``source_endpoint``
      and ``ingestion_time`` fields when they are present, otherwise
      from a default ``AcquisitionProvenance()``;
    - the legacy ``source_endpoint`` / ``ingestion_time`` strings are
      preserved on the record so v2 readers can still inspect them.

    A future blob (schema version greater than current) is rejected —
    the caller must upgrade before loading.
    """
    import dataclasses

    data = json.loads(blob.decode("utf-8"))
    schema_version = data.get("__schema_version__", 1)
    if schema_version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"future schema version {schema_version} is not supported "
            f"by this build (CURRENT_SCHEMA_VERSION={CURRENT_SCHEMA_VERSION})"
        )
    class_name = data.get("__class__")
    if class_name is None:
        raise ValueError("canonical blob missing __class__ envelope field")
    cls = _CLASS_REGISTRY.get(class_name)
    if cls is None:
        raise ValueError(
            f"unknown record class {class_name!r}; known classes: {sorted(_CLASS_REGISTRY)}"
        )
    if schema_version < 2:
        data = _migrate_v1_to_v2(data)
    if schema_version < 3:
        data = _migrate_v2_to_v3(data)
    # Build the dataclass-ready payload: strip envelope, materialise
    # the acquisition envelope, and route unknown top-level keys into
    # ``unknown_fields``.
    payload = {k: v for k, v in data.items() if not k.startswith("__")}
    if isinstance(payload.get("acquisition"), dict):
        payload["acquisition"] = AcquisitionProvenance(**payload["acquisition"])
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = {k: payload.pop(k) for k in list(payload) if k not in known}
    if unknown:
        existing = payload.get("unknown_fields") or {}
        if not isinstance(existing, dict):
            existing = {"_replaced": existing}
        existing.update(unknown)
        payload["unknown_fields"] = existing
    return cls(**payload)


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Apply the v1 → v2 migration to a parsed canonical dict.

    The v1 record shape did not carry ``removed`` / ``transaction_index``
    on log records or an ``acquisition`` envelope. The legacy
    ``source_endpoint`` / ``ingestion_time`` strings are preserved on
    the record (so a v2 reader can still read them) and copied into a
    default ``AcquisitionProvenance`` so the structured envelope is
    populated without requiring the caller to redo the migration.
    """
    log_record_classes = {
        "InitializeLogRecord",
        "ModifyLiquidityLogRecord",
        "SwapLogRecord",
        "DonateLogRecord",
        "ProtocolFeeUpdatedLogRecord",
    }
    class_name = data.get("__class__")
    if class_name in log_record_classes:
        if "removed" not in data:
            data["removed"] = False
        if "transaction_index" not in data:
            data["transaction_index"] = 0
    if "acquisition" not in data:
        data["acquisition"] = {
            "endpoint_alias": data.get("source_endpoint", ""),
            "retrieval_time": data.get("ingestion_time", ""),
            "request_from_block": 0,
            "request_to_block": 0,
            "http_batch_size": None,
            "http_batch_position": None,
            "request_attempt": None,
        }
    elif isinstance(data["acquisition"], dict):
        # Already a v2-shaped acquisition: leave it.
        pass
    return data


def _migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """Apply the v2 → v3 migration (T035, ADR-012).

    The v2 record shape did not carry ``block_timestamp`` or
    ``parent_hash``; those fields arrive from the dedup'd
    ``block_headers`` manifest table on every freshly persisted
    event. v2 records migrated forward default to ``0`` for both;
    a record whose block_timestamp is still ``0`` after migration
    is a legacy row whose header was not retained at decode time
    and the downstream consumer treats it accordingly.
    """
    log_record_classes = {
        "InitializeLogRecord",
        "ModifyLiquidityLogRecord",
        "SwapLogRecord",
        "DonateLogRecord",
        "ProtocolFeeUpdatedLogRecord",
    }
    class_name = data.get("__class__")
    if class_name in log_record_classes:
        if "block_timestamp" not in data:
            data["block_timestamp"] = 0
        if "parent_hash" not in data:
            data["parent_hash"] = 0
    return data


# ---------------------------------------------------------------------------
# Normalized content hash (cross-provider equality)
# ---------------------------------------------------------------------------


def normalized_content_hash(record: Any) -> bytes:
    """Return the SHA-256 of the normalized (provenance-free) record.

    The hash covers every typed field and the schema/decode versions
    but excludes:

    - ``acquisition`` (observational envelope),
    - ``ingestion_time`` / ``source_endpoint`` (legacy provenance),
    - ``raw`` / ``raw_topics`` / ``raw_data`` (raw evidence, the
      decoded typed fields are authoritative),
    - ``unknown_fields`` (forward-compat leftovers).

    Two records of the same chain event — fetched from different
    endpoints, at different times, or with different HTTP batch
    metadata — produce the same normalized content hash. The full
    ``canonical_bytes(record)`` still differs because it carries the
    acquisition envelope. Same-height fork logs have different
    ``block_hash`` fields, so their EventKey identity and this hash
    also differ.
    """
    payload = _canonical(record)
    payload["__schema_version__"] = record.schema_version
    payload["__decode_version__"] = record.decode_version
    payload["__class__"] = type(record).__name__
    for name in PROVENANCE_FIELD_NAMES:
        payload.pop(name, None)
    for name in RAW_FIELD_NAMES:
        payload.pop(name, None)
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).digest()


__all__ = [
    "AcquisitionProvenance",
    "BlockContext",
    "CURRENT_DECODE_VERSION",
    "CURRENT_SCHEMA_VERSION",
    "DonateLogRecord",
    "InitializeLogRecord",
    "ModifyLiquidityLogRecord",
    "PROVENANCE_FIELD_NAMES",
    "ProtocolFeeUpdatedLogRecord",
    "RAW_FIELD_NAMES",
    "ReceiptContext",
    "SwapLogRecord",
    "TransactionContext",
    "canonical_bytes",
    "from_canonical_bytes",
    "migrate_to_current",
    "normalized_content_hash",
]


# Populate the class registry after every dataclass is defined.
_CLASS_REGISTRY.update(
    {
        "BlockContext": BlockContext,
        "DonateLogRecord": DonateLogRecord,
        "InitializeLogRecord": InitializeLogRecord,
        "ModifyLiquidityLogRecord": ModifyLiquidityLogRecord,
        "ProtocolFeeUpdatedLogRecord": ProtocolFeeUpdatedLogRecord,
        "ReceiptContext": ReceiptContext,
        "SwapLogRecord": SwapLogRecord,
        "TransactionContext": TransactionContext,
    }
)
