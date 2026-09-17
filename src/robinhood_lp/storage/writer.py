"""Staging / atomic-commit writer for raw partitions (T031).

This module glues the Parquet schema (``partition.py``), the manifest
store (``manifest.py``), and the T030 logical records (``schema.py``)
into a single idempotent write operation that satisfies the T031
acceptance matrix.

Commit protocol
---------------

For every ``append_partition`` call:

1. The records are encoded into a Parquet file written to a
   ``.staging-<uuid>.parquet`` file inside the partition directory.
2. The SHA-256 of the staging file is computed.
3. ``os.rename`` atomically moves the staging file to its final
   ``data.parquet`` name on POSIX filesystems.
4. A single ``BEGIN IMMEDIATE`` SQLite transaction inserts:

   - one ``partitions`` row,
   - one ``partition_block_bounds`` row,
   - one ``partition_qualification`` row,
   - one ``accounting_intervals`` row,
   - one ``event_index`` row per record,
   - one ``conflicting_observations`` row per detected conflict,
   - an ``ingestion_checkpoints`` advance.

5. ``COMMIT`` makes the manifest visible to the reader.

A crash between step 1 and step 3 leaves the staging file on disk
and no manifest change: the reader ignores it (filenames are not
trusted, the manifest is). A crash between step 3 and step 5 leaves
the final file on disk and no manifest change: the reader still
ignores it. A crash during step 5 rolls back: the manifest is
unchanged and the final file remains on disk but unread. None of
these intermediate states expose a half-written partition or a
manifest row pointing at missing / partial bytes.

Dedup
-----

Dedup is by T011 ``EventKey`` (``chain_id``, ``block_hash``,
``tx_hash``, ``log_index``) plus the normalized content hash from
:func:`robinhood_lp.storage.schema.normalized_content_hash`. It is
explicitly **not** driven by ingestion time, endpoint alias, block
number alone, or filename. Two records with the same EventKey and
the same content hash are the same chain event and are idempotently
skipped on re-run. Two records with the same EventKey and a
different content hash are conflicting observations (a reorg) and
halt qualification of every partition that holds either observation.

Each V4 event record's typed fields and the schema/decode versions
enter the normalized content hash; the raw evidence columns, the
``AcquisitionProvenance`` envelope, and the legacy
``source_endpoint`` / ``ingestion_time`` strings do not.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]

from robinhood_lp.protocol import Address, ChainId
from robinhood_lp.storage.manifest import (
    AccountingInterval,
    ConflictingObservation,
    ManifestStore,
    PartitionBounds,
    PartitionManifestRow,
    PartitionQualification,
    validate_endpoint_alias,
)
from robinhood_lp.storage.partition import (
    PartitionKey,
    parquet_schema_for,
)
from robinhood_lp.storage.schema import (
    AcquisitionProvenance,
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    ProtocolFeeUpdatedLogRecord,
    SwapLogRecord,
    normalized_content_hash,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default number of blocks per partition. Used by the default
#: :func:`partition_records_by_block_range` helper. The chosen default
#: is justified by the representative short-range measurement recorded
#: in :mod:`robinhood_lp.storage.measurement`.
DEFAULT_BLOCKS_PER_PARTITION: Final[int] = 100

# ---------------------------------------------------------------------------
# Record dispatch (any typed log record -> PartitionKey + Parquet row)
# ---------------------------------------------------------------------------

_LogRecord = (
    InitializeLogRecord
    | ModifyLiquidityLogRecord
    | SwapLogRecord
    | DonateLogRecord
    | ProtocolFeeUpdatedLogRecord
)


def _topic_bytes(record: _LogRecord) -> list[bytes]:
    """Return the four 32-byte topics as bytes (zero-padded when absent)."""
    out: list[bytes] = []
    for topic in record.raw_topics:
        if not isinstance(topic, (bytes, bytearray)):
            raise TypeError(f"raw_topics entry must be bytes, got {type(topic).__name__}")
        if len(topic) != 32:
            raise ValueError(f"raw_topics entry must be 32 bytes, got {len(topic)} bytes")
        out.append(bytes(topic))
    while len(out) < 4:
        out.append(b"\x00" * 32)
    return out[:4]


def _acquisition_fields(record: _LogRecord) -> dict[str, Any]:
    acq: AcquisitionProvenance = record.acquisition
    return {
        "acquisition_endpoint_alias": acq.endpoint_alias,
        "acquisition_retrieval_time": acq.retrieval_time,
        "acquisition_request_from_block": acq.request_from_block,
        "acquisition_request_to_block": acq.request_to_block,
        "acquisition_http_batch_size": acq.http_batch_size,
        "acquisition_http_batch_position": acq.http_batch_position,
        "acquisition_request_attempt": acq.request_attempt,
    }


def _event_key_hash(record: _LogRecord) -> bytes:
    ek = record.event_key()
    payload = json.dumps(
        {
            "chain_id": ek.chain_id.value,
            "block_hash": ek.block_hash,
            "tx_hash": ek.tx_hash,
            "log_index": ek.log_index,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).digest()


def _record_to_row(record: _LogRecord) -> dict[str, Any]:
    """Convert one typed log record into a Parquet row dict.

    Common columns are filled in by this helper. Per-event columns are
    filled in by the event-specific helper called by the writer.
    """
    ek = record.event_key()
    topics = _topic_bytes(record)
    raw_response = json.dumps(record.raw, sort_keys=True, ensure_ascii=False)
    acq = _acquisition_fields(record)
    shared: dict[str, Any] = {
        "raw_topic_0": topics[0],
        "raw_topic_1": topics[1],
        "raw_topic_2": topics[2],
        "raw_topic_3": topics[3],
        "raw_data": bytes(record.raw_data),
        "raw_response_json": raw_response,
        "chain_id": ek.chain_id.value,
        "block_number": record.block_number,
        "block_hash": record.block_hash.to_bytes(32, "big"),
        # T035 / ADR-012: every persisted event must carry the
        # integer block timestamp and the parent block hash. The
        # runner populates these from the dedup'd ``block_headers``
        # manifest table before the record reaches the writer. A
        # legacy v1/v2 record migrated forward carries ``0`` for
        # both — the writer does not refuse that, it simply
        # persists the same value.
        "block_timestamp": int(record.block_timestamp),
        "parent_hash": record.parent_hash.to_bytes(32, "big"),
        "transaction_hash": record.transaction_hash.to_bytes(32, "big"),
        "transaction_index": record.transaction_index,
        "log_index": record.log_index,
        "address": record.address.value.to_bytes(20, "big"),
        "pool_id": record.pool_id.value.to_bytes(32, "big"),
        "removed": record.removed,
        "event_name": type(record).__name__.removesuffix("LogRecord"),
        "schema_version": record.schema_version,
        "decode_version": record.decode_version,
        "event_key_hash": _event_key_hash(record),
        "content_hash": normalized_content_hash(record),
        **acq,
    }
    return shared


def _record_typed_columns(record: _LogRecord) -> dict[str, Any]:
    """Return the per-event typed columns for one record.

    V4 integer widths are preserved as fixed-width bytes so the
    Parquet round-trip never silently truncates a uint128 / int256 /
    uint96 value. See :data:`SHARED_PARQUET_FIELDS` for the byte
    widths.
    """
    from robinhood_lp.storage.partition import (
        ADDRESS_BYTES,
        HASH_BYTES,
        UINT128_BYTES,
        UINT160_BYTES,
        UINT256_BYTES,
    )

    def _uint(value: int, width: int) -> bytes:
        if value < 0:
            raise ValueError(f"uint{width * 8} value must be non-negative, got {value}")
        return int(value).to_bytes(width, "big")

    def _int(value: int, width: int) -> bytes:
        upper = 1 << (width * 8 - 1)
        lower = -upper
        if value < lower or value >= upper:
            raise ValueError(f"int{width * 8} value out of range, got {value}")
        return int(value).to_bytes(width, "big", signed=True)

    if isinstance(record, InitializeLogRecord):
        return {
            "currency0": _uint(record.pool_id.value, HASH_BYTES),
            "currency1": _uint(record.pool_id.value, HASH_BYTES),
            "fee": 0,
            "tick_spacing": 0,
            "hooks": b"\x00" * ADDRESS_BYTES,
            "sqrt_price_x96": _uint(0, UINT160_BYTES),
            "tick": 0,
        }
    if isinstance(record, ModifyLiquidityLogRecord):
        return {
            "sender": _uint(record.sender.value, ADDRESS_BYTES),
            "tick_lower": int(record.tick_lower),
            "tick_upper": int(record.tick_upper),
            "liquidity_delta": _int(record.liquidity_delta, UINT256_BYTES),
            "salt": _uint(record.salt, HASH_BYTES),
        }
    if isinstance(record, SwapLogRecord):
        return {
            "sender": _uint(record.sender.value, ADDRESS_BYTES),
            "amount0": _int(record.amount0, UINT256_BYTES),
            "amount1": _int(record.amount1, UINT256_BYTES),
            "sqrt_price_x96": _uint(record.sqrt_price_x96, UINT160_BYTES),
            "liquidity": _uint(record.liquidity, UINT128_BYTES),
            "tick": int(record.tick),
            "fee": int(record.fee),
        }
    if isinstance(record, DonateLogRecord):
        return {
            "sender": _uint(record.sender.value, ADDRESS_BYTES),
            "amount0": _int(record.amount0, UINT256_BYTES),
            "amount1": _int(record.amount1, UINT256_BYTES),
        }
    if isinstance(record, ProtocolFeeUpdatedLogRecord):
        return {"protocol_fee": int(record.protocol_fee)}
    raise TypeError(f"unsupported record type: {type(record).__name__}")


def records_to_arrow_table(records: Sequence[_LogRecord], event_name: str) -> pa.Table:
    """Build the Parquet table for a homogeneous batch of records.

    Every record must be the same event name (the partition key
    fixes this; the writer asserts it for safety). The schema is the
    shared columns plus the per-event columns for ``event_name``.
    """
    if not records:
        raise ValueError("records_to_arrow_table: no records")
    for r in records:
        ev = type(r).__name__.removesuffix("LogRecord")
        if ev != event_name:
            raise ValueError(
                f"records_to_arrow_table: record event_name {ev!r} "
                f"does not match requested {event_name!r}"
            )
    schema = parquet_schema_for(event_name)
    rows: list[dict[str, Any]] = []
    for r in records:
        row = _record_to_row(r)
        row.update(_record_typed_columns(r))
        rows.append(row)
    table = pa.Table.from_pylist(rows, schema=schema)
    return table


def derive_partition_key(
    records: Iterable[_LogRecord],
    *,
    chain_id: ChainId,
    contract_address: Address,
    blocks_per_partition: int = DEFAULT_BLOCKS_PER_PARTITION,
) -> PartitionKey:
    """Derive a :class:`PartitionKey` from a batch of records.

    The partition's block range is the grid cell that contains the
    first record's ``block_number``. All records in the batch must
    fall within that cell; the caller is expected to slice the
    record stream by grid cell before calling this function. A
    re-run over an overlapping range lands on the same partition
    key, which is what makes repeat / overlap ingestion idempotent.
    """
    if blocks_per_partition <= 0:
        raise ValueError(f"blocks_per_partition: must be positive, got {blocks_per_partition}")
    first: _LogRecord | None = None
    event_name: str | None = None
    for r in records:
        if first is None:
            first = r
            event_name = type(r).__name__.removesuffix("LogRecord")
            continue
        ev = type(r).__name__.removesuffix("LogRecord")
        if ev != event_name:
            raise ValueError(f"derive_partition_key: mixed event names {event_name!r} vs {ev!r}")
        # Every record's block must fit in the same grid cell as the
        # first record's. The caller is expected to slice by grid
        # cell before calling this function; anything else is a
        # caller bug.
        cell_lo = (int(r.block_number) // blocks_per_partition) * blocks_per_partition
        if cell_lo != (int(first.block_number) // blocks_per_partition) * blocks_per_partition:
            raise ValueError(
                f"derive_partition_key: record at block {r.block_number} "
                f"is outside the first record's grid cell "
                f"(starting at block {first.block_number}); "
                f"slice the record stream by grid cell first"
            )
    if first is None or event_name is None:
        raise ValueError("derive_partition_key: no records")
    grid_lo = (int(first.block_number) // blocks_per_partition) * blocks_per_partition
    return PartitionKey(
        chain_id=chain_id,
        contract_address=contract_address,
        event_name=event_name,
        block_from=grid_lo,
        block_to=grid_lo + blocks_per_partition - 1,
    )


# ---------------------------------------------------------------------------
# Commit result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppendResult:
    """Result of an ``append_partition`` call.

    ``rows_appended`` is the number of records that survived dedup
    (i.e. were newly observed or re-observed with the same content
    hash). ``rows_skipped`` is the number of records that were
    idempotently identical to an existing observation. ``conflicts``
    is the number of conflicting observations that were retained.
    ``qualified`` is False iff at least one conflicting observation
    was recorded for this partition (the partition is still on disk
    but its qualification is halted per T031 acceptance).
    """

    partition_id: str
    file_path: Path
    file_sha256: str
    file_size_bytes: int
    rows_appended: int
    rows_skipped: int
    conflicts: int
    qualified: bool
    halt_reason: str | None = None
    record_partition_id: str = ""


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class RawPartitionWriter:
    """Staging / atomic-commit writer for raw partitions.

    A writer is bound to one ``data_root`` and one ``ManifestStore``.
    Every ``append_partition`` call is a single idempotent write
    transaction.
    """

    def __init__(
        self,
        data_root: Path,
        manifest: ManifestStore,
        *,
        blocks_per_partition: int = DEFAULT_BLOCKS_PER_PARTITION,
    ) -> None:
        self.data_root = Path(data_root)
        self.manifest = manifest
        self.blocks_per_partition = int(blocks_per_partition)

    # ----- public API ---------------------------------------------------

    def append_partition(
        self,
        records: Sequence[_LogRecord],
        *,
        chain_id: ChainId,
        contract_address: Address,
        blocks_per_partition: int | None = None,
        accounting: AccountingInterval | None = None,
    ) -> AppendResult:
        if not records:
            raise ValueError("append_partition: no records")
        bpp = blocks_per_partition or self.blocks_per_partition
        pk = derive_partition_key(
            records,
            chain_id=chain_id,
            contract_address=contract_address,
            blocks_per_partition=bpp,
        )
        # Sanity: the records all share the same event name (already
        # enforced by derive_partition_key). Double-check here too
        # because contract_address / chain_id were provided by the
        # caller and we want a hard failure on mismatch.
        for r in records:
            if r.chain_id.value != chain_id.value:
                raise ValueError(
                    f"record chain_id {r.chain_id.value} != writer chain_id {chain_id.value}"
                )
            if r.address.value != contract_address.value:
                raise ValueError(
                    f"record address {r.address.to_hex()} != writer contract {contract_address.to_hex()}"
                )
            acq_alias = r.acquisition.endpoint_alias
            if acq_alias:
                validate_endpoint_alias(acq_alias, field="record.acquisition.endpoint_alias")
        # If the partition already exists, do not re-write the file.
        # Re-running the same / overlapping batch lands on the same
        # partition key (T031 acceptance: repeat / overlap is
        # idempotent); we only need to update event_index and
        # accounting. We also assert the on-disk file still matches
        # its manifest checksum so a tampered partition does not get
        # silently accepted.
        existing_row = self.manifest.get_partition(pk.partition_id())
        if existing_row is not None:
            final_path = Path(existing_row.file_path)
            if not final_path.exists():
                from robinhood_lp.storage.reader import ManifestMismatchError

                raise ManifestMismatchError(
                    f"manifest row {pk.partition_id()} points at missing "
                    f"file {final_path} on re-run"
                )
            actual_sha = _sha256_file(final_path)
            if actual_sha != existing_row.file_sha256:
                from robinhood_lp.storage.reader import ManifestMismatchError

                raise ManifestMismatchError(
                    f"manifest row {pk.partition_id()} SHA-256 mismatch "
                    f"on re-run: manifest={existing_row.file_sha256} "
                    f"actual={actual_sha}"
                )
            return self._merge_into_existing_partition(
                records,
                pk=pk,
                accounting=accounting,
            )
        # 1. Build the Parquet table from records.
        table = records_to_arrow_table(records, pk.event_name)
        # 2. Write to staging file.
        partition_dir = pk.partition_dir(self.data_root)
        partition_dir.mkdir(parents=True, exist_ok=True)
        staging_path = partition_dir / f".staging-{uuid.uuid4().hex}.parquet"
        final_path = pk.data_file_path(self.data_root)
        try:
            _write_parquet(table, staging_path)
            # 3. Compute SHA-256 of the staging file.
            file_sha = _sha256_file(staging_path)
            file_size = staging_path.stat().st_size
            # 4. Atomic rename to the final path. POSIX rename is
            #    atomic within the same filesystem; the staging and
            #    final paths live in the same directory so this holds.
            os.rename(staging_path, final_path)
            # 5. Manifest transaction.
            with self.manifest.transaction() as conn:
                row = PartitionManifestRow(
                    partition_id=pk.partition_id(),
                    chain_id=pk.chain_id.value,
                    contract_address=pk.contract_address.to_hex().removeprefix("0x").lower(),
                    event_name=pk.event_name,
                    block_from=pk.block_from,
                    block_to=pk.block_to,
                    row_count=len(records),
                    file_path=str(final_path),
                    file_size_bytes=file_size,
                    file_sha256=file_sha,
                    schema_version=records[0].schema_version,
                    decode_version=records[0].decode_version,
                    created_at=_now_iso(),
                )
                bounds = _compute_bounds(records)
                qualification = PartitionQualification(
                    partition_id=pk.partition_id(),
                    qualified=True,
                    halt_reason=None,
                    halted_at=None,
                )
                self.manifest.insert_partition(conn, row, bounds, qualification)
                appended, skipped, conflicts = self._record_event_batch(
                    conn,
                    pk=pk,
                    records=records,
                )
                if accounting is not None:
                    self.manifest.insert_accounting(
                        conn,
                        pk.partition_id(),
                        accounting,
                    )
                self.manifest.advance_checkpoint(
                    conn,
                    chain_id=pk.chain_id.value,
                    contract_address=pk.contract_address.to_hex().removeprefix("0x").lower(),
                    event_name=pk.event_name,
                    last_successful_block=pk.block_to,
                )
            return AppendResult(
                partition_id=pk.partition_id(),
                file_path=final_path,
                file_sha256=file_sha,
                file_size_bytes=file_size,
                rows_appended=appended,
                rows_skipped=skipped,
                conflicts=conflicts,
                qualified=conflicts == 0,
                halt_reason=("conflicting observation recorded" if conflicts else None),
                record_partition_id=pk.partition_id(),
            )
        except BaseException:
            # On any failure, clean up the staging file. The final
            # file (if rename already happened) is left on disk: a
            # future recovery pass / manual intervention can deal
            # with it. The reader will not see it because the manifest
            # rolled back.
            if staging_path.exists():
                with contextlib.suppress(OSError):
                    staging_path.unlink()
            raise

    def _merge_into_existing_partition(
        self,
        records: Sequence[_LogRecord],
        *,
        pk: PartitionKey,
        accounting: AccountingInterval | None,
    ) -> AppendResult:
        """Idempotent re-run path: append only new event_index rows.

        The on-disk Parquet file is not modified; the contract is
        that repeat / overlap ingestion lands on the same partition
        and the file content is preserved.
        """
        existing_row = self.manifest.get_partition(pk.partition_id())
        assert existing_row is not None
        file_path = Path(existing_row.file_path)
        file_size = file_path.stat().st_size if file_path.exists() else 0
        with self.manifest.transaction() as conn:
            appended, skipped, conflicts = self._record_event_batch(
                conn,
                pk=pk,
                records=records,
            )
            if accounting is not None:
                self.manifest.insert_accounting(
                    conn,
                    pk.partition_id(),
                    accounting,
                )
            self.manifest.advance_checkpoint(
                conn,
                chain_id=pk.chain_id.value,
                contract_address=pk.contract_address.to_hex().removeprefix("0x").lower(),
                event_name=pk.event_name,
                last_successful_block=pk.block_to,
            )
        return AppendResult(
            partition_id=pk.partition_id(),
            file_path=file_path,
            file_sha256=existing_row.file_sha256,
            file_size_bytes=file_size,
            rows_appended=appended,
            rows_skipped=skipped,
            conflicts=conflicts,
            qualified=conflicts == 0,
            halt_reason=("conflicting observation recorded" if conflicts else None),
            record_partition_id=pk.partition_id(),
        )

    def _record_event_batch(
        self,
        conn: sqlite3.Connection,
        *,
        pk: PartitionKey,
        records: Sequence[_LogRecord],
    ) -> tuple[int, int, int]:
        """Run dedup for a batch of records in one transaction.

        Returns ``(appended, skipped, conflicts)``.
        """
        appended = 0
        skipped = 0
        conflicts = 0
        for r in records:
            res = self._record_event_observation(conn, pk=pk, record=r)
            if res == "appended":
                appended += 1
            elif res == "skipped":
                skipped += 1
            elif res == "conflict":
                conflicts += 1
        if conflicts:
            self.manifest._upsert_qualification(
                conn,
                PartitionQualification(
                    partition_id=pk.partition_id(),
                    qualified=False,
                    halt_reason="conflicting observation recorded",
                    halted_at=_now_iso(),
                ),
            )
        return appended, skipped, conflicts

    # ----- dedup helpers ------------------------------------------------

    def _record_event_observation(
        self,
        conn: sqlite3.Connection,
        *,
        pk: PartitionKey,
        record: _LogRecord,
    ) -> str:
        """Record one observation in the event_index.

        Returns ``'appended'`` if this is the first observation for
        the EventKey, ``'skipped'`` if an identical observation
        already exists (idempotent re-run), or ``'conflict'`` if the
        EventKey was observed with a different content hash (a
        conflicting observation; the partition is halted).
        """
        ek = record.event_key()
        content_hash_hex = "0x" + normalized_content_hash(record).hex()
        existing = self.manifest.lookup_event(
            conn,
            chain_id=ek.chain_id.value,
            block_hash=ek.block_hash,
            tx_hash=ek.tx_hash,
            log_index=ek.log_index,
        )
        # An existing row may already be from this same partition
        # (the same chain event re-encoded in the same run, e.g. when
        # the caller passes duplicates). The same-partition same-
        # content case is skipped silently — no double count.
        prior_hashes = {row["content_hash"] for row in existing}
        if not existing:
            self.manifest.record_event_observation(
                conn,
                chain_id=ek.chain_id.value,
                block_hash=ek.block_hash,
                tx_hash=ek.tx_hash,
                log_index=ek.log_index,
                content_hash=content_hash_hex,
                partition_id=pk.partition_id(),
                observation_kind="consistent",
            )
            return "appended"
        if content_hash_hex in prior_hashes:
            # Idempotent re-observation (same content hash).
            return "skipped"
        # Conflict path. Record the conflict and a 'conflict' row in
        # event_index so the index is auditable.
        existing_hash_hex = next(iter(prior_hashes))
        conflict = ConflictingObservation(
            chain_id=ek.chain_id.value,
            block_hash="0x" + ek.block_hash.to_bytes(32, "big").hex(),
            tx_hash="0x" + ek.tx_hash.to_bytes(32, "big").hex(),
            log_index=ek.log_index,
            observed_content_hash=content_hash_hex,
            existing_content_hash=existing_hash_hex,
            partition_id=pk.partition_id(),
            observed_endpoint_alias=record.acquisition.endpoint_alias,
            observed_payload_json=json.dumps(
                {
                    "schema_version": record.schema_version,
                    "decode_version": record.decode_version,
                    "event_name": pk.event_name,
                    "block_number": record.block_number,
                },
                sort_keys=True,
                ensure_ascii=False,
            ),
            recorded_at=_now_iso(),
        )
        self.manifest.record_conflict(conn, conflict)
        self.manifest.record_event_observation(
            conn,
            chain_id=ek.chain_id.value,
            block_hash=ek.block_hash,
            tx_hash=ek.tx_hash,
            log_index=ek.log_index,
            content_hash=content_hash_hex,
            partition_id=pk.partition_id(),
            observation_kind="conflict",
        )
        return "conflict"

    def _last_conflict_for_record(
        self,
        pk: PartitionKey,
        record: _LogRecord,
    ) -> ConflictingObservation:
        # Convenience: build a minimal ConflictingObservation marker
        # so the caller can count conflicts without re-reading the DB.
        ek = record.event_key()
        return ConflictingObservation(
            chain_id=ek.chain_id.value,
            block_hash="0x" + ek.block_hash.to_bytes(32, "big").hex(),
            tx_hash="0x" + ek.tx_hash.to_bytes(32, "big").hex(),
            log_index=ek.log_index,
            observed_content_hash="0x" + normalized_content_hash(record).hex(),
            existing_content_hash="",
            partition_id=pk.partition_id(),
            observed_endpoint_alias=record.acquisition.endpoint_alias,
            observed_payload_json="",
            recorded_at=_now_iso(),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_parquet(table: pa.Table, path: Path) -> None:
    """Write a Parquet table to ``path`` atomically with the staging rename.

    The ``pyarrow.parquet.write_table`` call fully writes the file
    before returning. The caller then ``os.rename`` moves the staging
    file to its final name.
    """
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    pq.write_table(table, str(path), compression="snappy")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "0x" + h.hexdigest()


def _compute_bounds(records: Sequence[_LogRecord]) -> PartitionBounds:
    """Compute block bounds (number + hash) for a batch of records."""
    if not records:
        raise ValueError("_compute_bounds: no records")
    sorted_by_block = sorted(
        records, key=lambda r: (r.block_number, r.transaction_index, r.log_index)
    )
    lo = sorted_by_block[0]
    hi = sorted_by_block[-1]
    return PartitionBounds(
        min_block_number=lo.block_number,
        min_block_hash="0x" + lo.block_hash.to_bytes(32, "big").hex(),
        max_block_number=hi.block_number,
        max_block_hash="0x" + hi.block_hash.to_bytes(32, "big").hex(),
    )


__all__ = [
    "AppendResult",
    "DEFAULT_BLOCKS_PER_PARTITION",
    "RawPartitionWriter",
    "derive_partition_key",
    "records_to_arrow_table",
]
