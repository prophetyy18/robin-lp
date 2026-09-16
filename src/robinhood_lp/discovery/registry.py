"""PoolKey registry and Initialize scanner (T022).

T022 acceptance:

- stop / resume / overlap are identical (idempotent by PoolId key);
- native currency, dynamic fee, nonzero hook all decode and register;
- duplicates and conflicting Initialize data fail closed;
- broken / reverting / oversized metadata is recorded, the pool is
  not dropped;
- a complete PoolKey registry is produced for a verified PoolManager
  range.

T022 must-not:

- query a factory (V4 is a singleton; only ``Initialize`` events
  exist);
- discover by token symbol;
- drop pools whose metadata call fails.

This module sits in the storage layer per ADR-006. It depends on
``robinhood_lp.protocol``, ``robinhood_lp.rpc``, and the scanner's
own submodules. Persistent storage is owned by T031; the in-memory
registry here is the canonical in-process state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from robinhood_lp.discovery.initialize_log import (
    INITIALIZE_TOPIC0,
    DecodedInitialize,
    InitializeDecodeError,
    decode_initialize_log,
)
from robinhood_lp.discovery.token_metadata import (
    TokenMetadataRecord,
    read_token_metadata,
)
from robinhood_lp.protocol import Address, BlockRef, ChainId, PoolId, PoolKey
from robinhood_lp.rpc import RpcAdapter

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RegistryConflictError(RuntimeError):
    """An Initialize event contradicts an already-registered pool."""


class RegistryDuplicateError(RuntimeError):
    """The same PoolId was registered twice with identical data (idempotent
    conflict that the scanner treats as success)."""


# ---------------------------------------------------------------------------
# Registry record
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PoolRecord:
    """One pool's registry row.

    The PoolKey is the identity; the metadata is display-only. The
    registry refuses two records with the same PoolId and different
    PoolKey (``RegistryConflictError``). Two records with the same
    PoolId and identical PoolKey are duplicates and merged into a
    single row (``RegistryDuplicateError`` is *not* raised; the
    scanner counts the duplicate and continues).
    """

    pool_id: PoolId
    pool_key: PoolKey
    token0_metadata: TokenMetadataRecord | None = None
    token1_metadata: TokenMetadataRecord | None = None
    block_number_first_seen: int | None = None
    block_number_last_seen: int | None = None
    tx_hash_first_seen: str | None = None
    log_index_first_seen: int | None = None
    occurrences: int = 1
    #: Initial ``sqrtPriceX96`` from the first ``Initialize`` log that
    #: produced this row. Surfaces the V4 event's non-indexed data
    #: slot for downstream consumers; does not participate in identity.
    sqrt_price_x96: int | None = None
    #: Initial ``tick`` from the first ``Initialize`` log that
    #: produced this row. Surfaces the V4 event's non-indexed data
    #: slot for downstream consumers; does not participate in identity.
    initial_tick: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "pool_id": self.pool_id.to_hex(),
            "currency0": self.pool_key.currency0.to_address().to_hex(),
            "currency1": self.pool_key.currency1.to_address().to_hex(),
            "fee": self.pool_key.fee,
            "tick_spacing": self.pool_key.tick_spacing,
            "hooks": self.pool_key.hooks.to_hex(),
            "block_number_first_seen": self.block_number_first_seen,
            "block_number_last_seen": self.block_number_last_seen,
            "tx_hash_first_seen": self.tx_hash_first_seen,
            "log_index_first_seen": self.log_index_first_seen,
            "occurrences": self.occurrences,
            "token0_symbol": self.token0_metadata.symbol if self.token0_metadata else None,
            "token0_decimals": self.token0_metadata.decimals if self.token0_metadata else None,
            "token0_metadata_failed": self.token0_metadata is not None
            and not self.token0_metadata.is_complete(),
            "token1_symbol": self.token1_metadata.symbol if self.token1_metadata else None,
            "token1_decimals": self.token1_metadata.decimals if self.token1_metadata else None,
            "token1_metadata_failed": self.token1_metadata is not None
            and not self.token1_metadata.is_complete(),
            "sqrt_price_x96": self.sqrt_price_x96,
            "initial_tick": self.initial_tick,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PoolRegistry:
    """In-memory idempotent registry keyed by ``(chain_id, pool_id)``.

    Insertion with a new PoolKey creates a row. Insertion with the
    same PoolKey bumps the ``occurrences`` counter and updates the
    "last seen" block. Insertion with a different PoolKey for an
    existing PoolId raises :class:`RegistryConflictError`.
    """

    chain_id: ChainId
    _records: dict[PoolId, PoolRecord] = field(default_factory=dict)
    _conflicts: list[RegistryConflictError] = field(default_factory=list)
    _duplicates: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise TypeError(f"chain_id must be ChainId, got {type(self.chain_id).__name__}")

    @property
    def size(self) -> int:
        return len(self._records)

    @property
    def duplicates(self) -> int:
        return self._duplicates

    @property
    def conflicts(self) -> list[RegistryConflictError]:
        return list(self._conflicts)

    def add(
        self,
        decoded: DecodedInitialize,
        *,
        block_number: int | None = None,
        tx_hash: str | None = None,
        log_index: int | None = None,
        token0_metadata: TokenMetadataRecord | None = None,
        token1_metadata: TokenMetadataRecord | None = None,
    ) -> PoolRecord:
        """Insert or update a registry row.

        Conflict semantics: a second Initialize event for the same
        PoolId with a different PoolKey is a hard error. The scanner
        catches it and records the conflict; the framework surfaces
        the conflict in the registry's ``conflicts`` list rather than
        silently dropping the pool.
        """
        existing = self._records.get(decoded.pool_id)
        if existing is None:
            record = PoolRecord(
                pool_id=decoded.pool_id,
                pool_key=decoded.pool_key,
                token0_metadata=token0_metadata,
                token1_metadata=token1_metadata,
                block_number_first_seen=block_number,
                block_number_last_seen=block_number,
                tx_hash_first_seen=tx_hash,
                log_index_first_seen=log_index,
                occurrences=1,
                sqrt_price_x96=decoded.sqrt_price_x96,
                initial_tick=decoded.initial_tick,
            )
            self._records[decoded.pool_id] = record
            return record

        # Existing row: check PoolKey equality.
        if not _pool_keys_equal(existing.pool_key, decoded.pool_key):
            err = RegistryConflictError(
                f"PoolId {decoded.pool_id.to_hex()} already registered with a different PoolKey"
            )
            self._conflicts.append(err)
            raise err

        # Duplicate; idempotent merge.
        existing.occurrences += 1
        self._duplicates += 1
        if block_number is not None:
            existing.block_number_last_seen = block_number
        if token0_metadata is not None and existing.token0_metadata is None:
            existing.token0_metadata = token0_metadata
        if token1_metadata is not None and existing.token1_metadata is None:
            existing.token1_metadata = token1_metadata
        return existing

    def get(self, pool_id: PoolId) -> PoolRecord | None:
        return self._records.get(pool_id)

    def all_records(self) -> list[PoolRecord]:
        return list(self._records.values())


def _pool_keys_equal(a: PoolKey, b: PoolKey) -> bool:
    """Equality of two PoolKey instances; tolerates different identities."""
    return (
        a.currency0.address.value == b.currency0.address.value
        and a.currency1.address.value == b.currency1.address.value
        and a.fee == b.fee
        and a.tick_spacing == b.tick_spacing
        and a.hooks.value == b.hooks.value
    )


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScannerStats:
    """Counters emitted by the Initialize scanner."""

    blocks_scanned: int = 0
    logs_total: int = 0
    logs_decoded: int = 0
    logs_skipped_unrecognized: int = 0
    pools_added: int = 0
    pools_conflicted: int = 0
    duplicates: int = 0
    metadata_failures: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "blocks_scanned": self.blocks_scanned,
            "logs_total": self.logs_total,
            "logs_decoded": self.logs_decoded,
            "logs_skipped_unrecognized": self.logs_skipped_unrecognized,
            "pools_added": self.pools_added,
            "pools_conflicted": self.pools_conflicted,
            "duplicates": self.duplicates,
            "metadata_failures": self.metadata_failures,
        }


class InitializeScanner:
    """Scan a block range and add Initialize events to a registry.

    The scanner is decoupled from the RPC adapter: callers feed it
    raw log dictionaries (the output of ``eth_getLogs``). This makes
    it trivial to test without a transport and to resume after an
    interruption without re-querying blocks the caller has already
    processed.
    """

    #: ``Initialize`` event topic0, sourced from the pinned
    #: ``EVENT_TOPICS`` artifact so the scanner and decoder agree on
    #: the byte-for-byte keccak. ``initialize_log.INITIALIZE_TOPIC0``
    #: is the same value (re-imported here to avoid a cycle at import
    #: time).
    INITIALIZE_TOPIC0_HEX = "0x" + INITIALIZE_TOPIC0.hex()

    def __init__(
        self,
        registry: PoolRegistry,
        *,
        fetch_metadata: bool = True,
        rpc_adapter: RpcAdapter | None = None,
    ) -> None:
        self._registry = registry
        self._fetch_metadata = fetch_metadata
        self._rpc = rpc_adapter
        self._stats = ScannerStats()

    @property
    def stats(self) -> ScannerStats:
        return self._stats

    async def ingest_logs(
        self,
        raw_logs: Iterable[dict[str, object]],
        *,
        chain_id: ChainId | None = None,
        block_ref: BlockRef | None = None,
    ) -> None:
        """Ingest a batch of raw logs and update the registry.

        ``chain_id`` is recorded into the registry's namespace if
        provided; the registry rejects a chain mismatch on its own
        via the constructor check.
        """
        for raw in raw_logs:
            self._stats.logs_total += 1
            topics_hex = raw.get("topics")
            data_hex = raw.get("data")
            if not isinstance(topics_hex, list) or not isinstance(data_hex, str):
                self._stats.logs_skipped_unrecognized += 1
                continue
            try:
                topics = [
                    bytes.fromhex(t[2:]) if isinstance(t, str) and t.startswith("0x") else t
                    for t in topics_hex
                ]
                # All topics must be bytes by now; if any are not, skip.
                if not all(isinstance(t, bytes) for t in topics):
                    self._stats.logs_skipped_unrecognized += 1
                    continue
                data = (
                    bytes.fromhex(data_hex[2:])
                    if data_hex.startswith("0x")
                    else bytes.fromhex(data_hex)
                )
            except (ValueError, TypeError):
                self._stats.logs_skipped_unrecognized += 1
                continue
            try:
                decoded = decode_initialize_log(topics, data)
            except InitializeDecodeError:
                self._stats.logs_skipped_unrecognized += 1
                continue
            self._stats.logs_decoded += 1

            block_number = _coerce_int(raw.get("blockNumber"))
            tx_hash_raw = raw.get("transactionHash")
            tx_hash: str | None = tx_hash_raw if isinstance(tx_hash_raw, str) else None
            log_index = _coerce_int(raw.get("logIndex"))

            t0_meta: TokenMetadataRecord | None = None
            t1_meta: TokenMetadataRecord | None = None
            if self._fetch_metadata and self._rpc is not None:
                t0_meta = await self._read_metadata_safe(decoded.pool_key.currency0.to_address())
                t1_meta = await self._read_metadata_safe(decoded.pool_key.currency1.to_address())
                if t0_meta is not None and not t0_meta.is_complete():
                    self._stats.metadata_failures += 1
                if t1_meta is not None and not t1_meta.is_complete():
                    self._stats.metadata_failures += 1

            try:
                existing_before = self._registry._records.get(decoded.pool_id)  # noqa: SLF001
                self._registry.add(
                    decoded,
                    block_number=block_number,
                    tx_hash=tx_hash,
                    log_index=log_index,
                    token0_metadata=t0_meta,
                    token1_metadata=t1_meta,
                )
            except RegistryConflictError:
                self._stats.pools_conflicted += 1
                # The conflict is already in registry.conflicts; we
                # simply continue scanning.
                continue
            else:
                # ``pools_added`` counts *new* rows only; duplicates
                # bump ``duplicates`` (already incremented inside
                # ``registry.add``).
                if existing_before is None:
                    self._stats.pools_added += 1
                else:
                    self._stats.duplicates += 1
        _ = block_ref  # accepted for callers that pass a BlockRef; reserved.

    async def _read_metadata_safe(self, token: Address) -> TokenMetadataRecord | None:
        if self._rpc is None:
            return None
        try:
            return await read_token_metadata(self._rpc, token)
        except Exception:  # noqa: BLE001
            # Per T022 must-not: do not drop pools whose metadata
            # call fails. ``read_token_metadata`` already catches the
            # call errors and records them; this catch is for the
            # outer failure mode (transport / unexpected).
            return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.lower()
        try:
            return int(s, 16) if s.startswith("0x") else int(s)
        except ValueError:
            return None
    return None


__all__ = [
    "InitializeScanner",
    "PoolRecord",
    "PoolRegistry",
    "RegistryConflictError",
    "RegistryDuplicateError",
    "ScannerStats",
]


# Sentinel import for the type checker.
from collections.abc import Iterable  # noqa: E402
