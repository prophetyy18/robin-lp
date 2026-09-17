"""SQLite manifest store + reorg journal + accounting (T031).

ADR-002 selects a single SQLite database file as the manifest /
checkpoint / reorg journal store. This module implements that store
and the write / read helpers the writer and reader call into.

Tables
------

``partitions`` — one row per persisted partition. The primary key is
the deterministic ``partition_id`` produced by
:class:`robinhood_lp.storage.partition.PartitionKey.partition_id`.

``partition_block_bounds`` — block bounds per partition
(``min_block_number``, ``min_block_hash``, ``max_block_number``,
``max_block_hash``). Stored separately so the per-partition SHA-256 /
row-count bookkeeping is not confused with the chain-event-block
range bounds, which T031 acceptance requires be verified on read.

``partition_qualification`` — per-partition qualification status.
A partition is qualified iff no conflicting observation has been
recorded for any of its events. A conflicting observation halts
qualification but never deletes the partition.

``event_index`` — fast lookup of EventKey → content_hash, used by the
writer to enforce idempotent dedup and to detect conflicting
observations.

``conflicting_observations`` — full record of every conflicting
observation, retained for audit. A conflict does **not** overwrite
the existing event_index row; it appends a new observation with
``observation_kind = 'conflict'`` and the partition is marked
unqualified.

``accounting_intervals`` — manifest accounting split per endpoint
alias. The columns match the T031 deliverable: ``logical_rpc_calls``,
``http_requests``, ``http_batches``, ``response_bytes``,
``normalized_rows``, ``provider_units``, ``parquet_bytes``, plus the
requested block interval.

``scanned_empty_intervals`` — block ranges the caller intentionally
scanned and which returned no events. Recorded separately from
failed or un-fetched intervals per T031 deliverable.

``reorg_journal`` — orphan block markers, replacement block hashes,
and demotion reasons. ADR-002 keeps the bytes themselves
auditable; the reorg journal only marks them as orphans.

``ingestion_checkpoints`` — last successful block per
``(chain_id, contract_address, event_name)``. The writer advances
this row in the same transaction as the partition insert; the reader
uses it to detect holes in coverage.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# Schema (DDL)
# ---------------------------------------------------------------------------

#: Current manifest schema version. Bumped whenever the SQLite schema
#: changes incompatibly. A reader that opens a manifest DB whose
#: version is higher than this constant refuses to load it.
MANIFEST_SCHEMA_VERSION: Final[int] = 1

_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS partitions (
        partition_id TEXT PRIMARY KEY,
        chain_id INTEGER NOT NULL,
        contract_address TEXT NOT NULL,
        event_name TEXT NOT NULL,
        block_from INTEGER NOT NULL,
        block_to INTEGER NOT NULL,
        row_count INTEGER NOT NULL,
        file_path TEXT NOT NULL,
        file_size_bytes INTEGER NOT NULL,
        file_sha256 TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        decode_version INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS partition_block_bounds (
        partition_id TEXT PRIMARY KEY,
        min_block_number INTEGER NOT NULL,
        min_block_hash TEXT NOT NULL,
        max_block_number INTEGER NOT NULL,
        max_block_hash TEXT NOT NULL,
        FOREIGN KEY (partition_id) REFERENCES partitions(partition_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS partition_qualification (
        partition_id TEXT PRIMARY KEY,
        qualified INTEGER NOT NULL,
        halt_reason TEXT,
        halted_at TEXT,
        FOREIGN KEY (partition_id) REFERENCES partitions(partition_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_index (
        chain_id INTEGER NOT NULL,
        block_hash TEXT NOT NULL,
        tx_hash TEXT NOT NULL,
        log_index INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        partition_id TEXT NOT NULL,
        observation_kind TEXT NOT NULL CHECK (observation_kind IN ('consistent','conflict')),
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (chain_id, block_hash, tx_hash, log_index, partition_id, observation_kind, content_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conflicting_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chain_id INTEGER NOT NULL,
        block_hash TEXT NOT NULL,
        tx_hash TEXT NOT NULL,
        log_index INTEGER NOT NULL,
        observed_content_hash TEXT NOT NULL,
        existing_content_hash TEXT NOT NULL,
        partition_id TEXT NOT NULL,
        observed_endpoint_alias TEXT NOT NULL,
        observed_payload_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        UNIQUE (chain_id, block_hash, tx_hash, log_index, partition_id, observed_content_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounting_intervals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        partition_id TEXT NOT NULL,
        endpoint_alias TEXT NOT NULL,
        request_from_block INTEGER NOT NULL,
        request_to_block INTEGER NOT NULL,
        logical_rpc_calls INTEGER NOT NULL,
        http_requests INTEGER NOT NULL,
        http_batches INTEGER NOT NULL,
        response_bytes INTEGER NOT NULL,
        normalized_rows INTEGER NOT NULL,
        provider_units INTEGER,
        parquet_bytes INTEGER NOT NULL,
        recorded_at TEXT NOT NULL,
        FOREIGN KEY (partition_id) REFERENCES partitions(partition_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scanned_empty_intervals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chain_id INTEGER NOT NULL,
        contract_address TEXT NOT NULL,
        event_name TEXT NOT NULL,
        endpoint_alias TEXT NOT NULL,
        request_from_block INTEGER NOT NULL,
        request_to_block INTEGER NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reorg_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chain_id INTEGER NOT NULL,
        block_number INTEGER NOT NULL,
        orphan_block_hash TEXT NOT NULL,
        replacement_block_hash TEXT,
        demotion_reason TEXT NOT NULL,
        partition_id TEXT,
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
        chain_id INTEGER NOT NULL,
        contract_address TEXT NOT NULL,
        event_name TEXT NOT NULL,
        last_successful_block INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (chain_id, contract_address, event_name)
    )
    """,
)


# ---------------------------------------------------------------------------
# Dataclasses used by the writer and reader
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountingInterval:
    """Manifest accounting for one requested interval.

    Per the T031 deliverable the manifest tracks endpoint alias,
    logical RPC calls, HTTP requests / batches, response bytes,
    normalized rows, provider units (when reported), Parquet bytes,
    plus the requested interval bounds.
    """

    endpoint_alias: str
    request_from_block: int
    request_to_block: int
    logical_rpc_calls: int
    http_requests: int
    http_batches: int
    response_bytes: int
    normalized_rows: int
    parquet_bytes: int
    provider_units: int | None = None


@dataclass(frozen=True, slots=True)
class PartitionManifestRow:
    """In-memory mirror of one ``partitions`` row."""

    partition_id: str
    chain_id: int
    contract_address: str
    event_name: str
    block_from: int
    block_to: int
    row_count: int
    file_path: str
    file_size_bytes: int
    file_sha256: str
    schema_version: int
    decode_version: int
    created_at: str


@dataclass(frozen=True, slots=True)
class PartitionBounds:
    """Block-number / block-hash bounds for one partition."""

    min_block_number: int
    min_block_hash: str
    max_block_number: int
    max_block_hash: str


@dataclass(frozen=True, slots=True)
class PartitionQualification:
    """Qualification status of one partition."""

    partition_id: str
    qualified: bool
    halt_reason: str | None
    halted_at: str | None


@dataclass(frozen=True, slots=True)
class ConflictingObservation:
    """One recorded conflicting observation."""

    chain_id: int
    block_hash: str
    tx_hash: str
    log_index: int
    observed_content_hash: str
    existing_content_hash: str
    partition_id: str
    observed_endpoint_alias: str
    observed_payload_json: str
    recorded_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _hex(value: int) -> str:
    """Lowercase ``0x``-prefixed hex of a non-negative integer."""
    if value < 0:
        raise ValueError(f"_hex: negative integer {value}")
    return "0x" + format(value, "x")


def validate_endpoint_alias(alias: str, *, field: str = "endpoint_alias") -> str:
    """Reject any alias that looks credential-bearing.

    The same rule the T030 schema applies to ``AcquisitionProvenance``:
    a ``://``, ``@``, ``?``, whitespace, or path separator in the alias
    is rejected. The storage layer must not record a URL or a
    URL-with-credentials anywhere.
    """
    if not isinstance(alias, str):
        raise TypeError(f"{field}: must be str, got {type(alias).__name__}")
    stripped = alias.strip()
    if not stripped:
        return stripped
    bad = ("://", "@", "?", "/", "\\", " ", "\t", "\n")
    for needle in bad:
        if needle in alias:
            raise ValueError(
                f"{field}: alias {alias!r} looks credential-bearing "
                f"(contains {needle!r}); use a short opaque token instead"
            )
    return alias


# ---------------------------------------------------------------------------
# Manifest connection
# ---------------------------------------------------------------------------


class ManifestStore:
    """Wrapper around the SQLite manifest database.

    The wrapper hides the DDL bootstrap and exposes typed helpers for
    the writer / reader. All multi-statement operations are wrapped in
    ``BEGIN IMMEDIATE`` / ``COMMIT`` so a crash mid-operation rolls
    back the manifest to the previous committed state and never leaves
    a partition manifest row pointing at a half-written payload
    (which is the staging / atomic-commit contract T031 acceptance
    requires).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._bootstrap()

    # ----- lifecycle -----------------------------------------------------

    def _bootstrap(self) -> None:
        with self.transaction() as conn:
            for ddl in _DDL:
                conn.execute(ddl)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(MANIFEST_SCHEMA_VERSION),),
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ManifestStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction with rollback on exception."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Run a read-only query helper. Auto-rolls back any BEGIN."""
        import contextlib

        conn = self._conn
        in_tx = False
        try:
            conn.execute("BEGIN")
            in_tx = True
            yield conn
            conn.execute("ROLLBACK")
        finally:
            if in_tx:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("ROLLBACK")

    # ----- schema metadata ----------------------------------------------

    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise RuntimeError("manifest DB missing schema_version row")
        version = int(row["value"])
        if version > MANIFEST_SCHEMA_VERSION:
            raise RuntimeError(
                f"manifest schema version {version} is newer than this "
                f"build (max supported = {MANIFEST_SCHEMA_VERSION})"
            )
        return version

    # ----- partition rows -----------------------------------------------

    def insert_partition(
        self,
        conn: sqlite3.Connection,
        row: PartitionManifestRow,
        bounds: PartitionBounds,
        qualification: PartitionQualification,
    ) -> bool:
        """Insert one partition manifest row + bounds + qualification.

        Returns True if a new partition row was inserted. Returns
        False if the partition already existed (idempotent re-run).
        Existing partition rows are never overwritten; the row / bounds
        are left untouched so the file SHA-256 the reader checks
        against is preserved across re-runs.
        """
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO partitions (
                partition_id, chain_id, contract_address, event_name,
                block_from, block_to, row_count, file_path, file_size_bytes,
                file_sha256, schema_version, decode_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.partition_id,
                row.chain_id,
                row.contract_address,
                row.event_name,
                row.block_from,
                row.block_to,
                row.row_count,
                row.file_path,
                row.file_size_bytes,
                row.file_sha256,
                row.schema_version,
                row.decode_version,
                row.created_at,
            ),
        )
        inserted = cur.rowcount > 0
        if inserted:
            conn.execute(
                """
                INSERT INTO partition_block_bounds (
                    partition_id, min_block_number, min_block_hash,
                    max_block_number, max_block_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row.partition_id,
                    bounds.min_block_number,
                    bounds.min_block_hash,
                    bounds.max_block_number,
                    bounds.max_block_hash,
                ),
            )
            self._upsert_qualification(conn, qualification)
        return inserted

    def upsert_qualification(self, qualification: PartitionQualification) -> None:
        """Public helper for the writer: update qualification in its own tx."""
        with self.transaction() as conn:
            self._upsert_qualification(conn, qualification)

    def _upsert_qualification(self, conn: sqlite3.Connection, q: PartitionQualification) -> None:
        conn.execute(
            """
            INSERT INTO partition_qualification
                (partition_id, qualified, halt_reason, halted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(partition_id) DO UPDATE SET
                qualified = excluded.qualified,
                halt_reason = excluded.halt_reason,
                halted_at = excluded.halted_at
            """,
            (q.partition_id, 1 if q.qualified else 0, q.halt_reason, q.halted_at),
        )

    def get_partition(self, partition_id: str) -> PartitionManifestRow | None:
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM partitions WHERE partition_id = ?", (partition_id,)
            ).fetchone()
        if row is None:
            return None
        return PartitionManifestRow(**dict(row))

    def get_bounds(self, partition_id: str) -> PartitionBounds | None:
        with self.read() as conn:
            row = conn.execute(
                "SELECT min_block_number, min_block_hash, max_block_number, max_block_hash "
                "FROM partition_block_bounds WHERE partition_id = ?",
                (partition_id,),
            ).fetchone()
        if row is None:
            return None
        return PartitionBounds(**dict(row))

    def get_qualification(self, partition_id: str) -> PartitionQualification | None:
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM partition_qualification WHERE partition_id = ?",
                (partition_id,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        return PartitionQualification(
            partition_id=data["partition_id"],
            qualified=bool(data["qualified"]),
            halt_reason=data["halt_reason"],
            halted_at=data["halted_at"],
        )

    # ----- event index (dedup + conflict detection) ---------------------

    def lookup_event(
        self,
        conn: sqlite3.Connection,
        *,
        chain_id: int,
        block_hash: int,
        tx_hash: int,
        log_index: int,
    ) -> list[sqlite3.Row]:
        """Return existing event_index rows for an EventKey.

        Returns both ``consistent`` and ``conflict`` rows so the caller
        can distinguish "first observation" from "idempotent re-run"
        from "conflicting observation".
        """
        return list(
            conn.execute(
                """
                SELECT content_hash, observation_kind, partition_id
                FROM event_index
                WHERE chain_id = ? AND block_hash = ? AND tx_hash = ?
                  AND log_index = ?
                """,
                (chain_id, _hex(block_hash), _hex(tx_hash), log_index),
            ).fetchall()
        )

    def record_event_observation(
        self,
        conn: sqlite3.Connection,
        *,
        chain_id: int,
        block_hash: int,
        tx_hash: int,
        log_index: int,
        content_hash: str,
        partition_id: str,
        observation_kind: str,
    ) -> bool:
        """Insert one ``event_index`` row.

        ``observation_kind`` is ``'consistent'`` for the first /
        idempotent re-run observations and ``'conflict'`` for
        observations whose content hash disagrees with an existing
        one. A conflicting observation does **not** overwrite the
        consistent row; both are retained. The PK includes
        ``content_hash`` so the same content hash is not recorded
        twice for the same EventKey + partition + observation_kind.

        Returns True if a row was inserted, False if the
        (EventKey, content_hash, observation_kind) was already
        present.
        """
        if observation_kind not in ("consistent", "conflict"):
            raise ValueError(f"unknown observation_kind {observation_kind!r}")
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO event_index (
                chain_id, block_hash, tx_hash, log_index, content_hash,
                partition_id, observation_kind, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chain_id,
                _hex(block_hash),
                _hex(tx_hash),
                log_index,
                content_hash,
                partition_id,
                observation_kind,
                _now_iso(),
            ),
        )
        return cur.rowcount > 0

    def record_conflict(
        self,
        conn: sqlite3.Connection,
        conflict: ConflictingObservation,
    ) -> None:
        """Insert one ``conflicting_observations`` row."""
        conn.execute(
            """
            INSERT INTO conflicting_observations (
                chain_id, block_hash, tx_hash, log_index,
                observed_content_hash, existing_content_hash, partition_id,
                observed_endpoint_alias, observed_payload_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conflict.chain_id,
                conflict.block_hash,
                conflict.tx_hash,
                conflict.log_index,
                conflict.observed_content_hash,
                conflict.existing_content_hash,
                conflict.partition_id,
                conflict.observed_endpoint_alias,
                conflict.observed_payload_json,
                conflict.recorded_at,
            ),
        )

    def list_conflicts(self, partition_id: str | None = None) -> list[ConflictingObservation]:
        query = (
            "SELECT chain_id, block_hash, tx_hash, log_index, "
            "observed_content_hash, existing_content_hash, partition_id, "
            "observed_endpoint_alias, observed_payload_json, recorded_at "
            "FROM conflicting_observations"
        )
        params: tuple[str, ...] = ()
        if partition_id is not None:
            query += " WHERE partition_id = ?"
            params = (partition_id,)
        query += " ORDER BY id"
        with self.read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [ConflictingObservation(**dict(r)) for r in rows]

    # ----- accounting ---------------------------------------------------

    def insert_accounting(
        self,
        conn: sqlite3.Connection,
        partition_id: str,
        record: AccountingInterval,
    ) -> None:
        """Insert one ``accounting_intervals`` row."""
        validate_endpoint_alias(record.endpoint_alias)
        conn.execute(
            """
            INSERT INTO accounting_intervals (
                partition_id, endpoint_alias, request_from_block,
                request_to_block, logical_rpc_calls, http_requests,
                http_batches, response_bytes, normalized_rows,
                provider_units, parquet_bytes, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                partition_id,
                record.endpoint_alias,
                record.request_from_block,
                record.request_to_block,
                record.logical_rpc_calls,
                record.http_requests,
                record.http_batches,
                record.response_bytes,
                record.normalized_rows,
                record.provider_units,
                record.parquet_bytes,
                _now_iso(),
            ),
        )

    def accounting_for(self, partition_id: str) -> list[dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM accounting_intervals WHERE partition_id = ? ORDER BY id",
                (partition_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----- scanned-empty intervals --------------------------------------

    def record_scanned_empty(
        self,
        *,
        chain_id: int,
        contract_address: str,
        event_name: str,
        endpoint_alias: str,
        request_from_block: int,
        request_to_block: int,
    ) -> None:
        validate_endpoint_alias(endpoint_alias)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO scanned_empty_intervals (
                    chain_id, contract_address, event_name,
                    endpoint_alias, request_from_block, request_to_block,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain_id,
                    contract_address,
                    event_name,
                    endpoint_alias,
                    request_from_block,
                    request_to_block,
                    _now_iso(),
                ),
            )

    def list_scanned_empty(self, *, event_name: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM scanned_empty_intervals"
        params: tuple[str, ...] = ()
        if event_name is not None:
            query += " WHERE event_name = ?"
            params = (event_name,)
        query += " ORDER BY id"
        with self.read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ----- reorg journal ------------------------------------------------

    def record_reorg(
        self,
        *,
        chain_id: int,
        block_number: int,
        orphan_block_hash: int,
        replacement_block_hash: int | None,
        demotion_reason: str,
        partition_id: str | None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO reorg_journal (
                    chain_id, block_number, orphan_block_hash,
                    replacement_block_hash, demotion_reason, partition_id,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain_id,
                    block_number,
                    _hex(orphan_block_hash),
                    (_hex(replacement_block_hash) if replacement_block_hash is not None else None),
                    demotion_reason,
                    partition_id,
                    _now_iso(),
                ),
            )

    def list_reorgs(self) -> list[dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM reorg_journal ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ----- ingestion checkpoints ----------------------------------------

    def advance_checkpoint(
        self,
        conn: sqlite3.Connection,
        *,
        chain_id: int,
        contract_address: str,
        event_name: str,
        last_successful_block: int,
    ) -> None:
        """Advance the checkpoint monotonically.

        A re-run that re-fetches an already-checkpointed range must
        not move the checkpoint backwards; the SQL ``MAX`` enforces
        monotonic advance inside a transaction.
        """
        conn.execute(
            """
            INSERT INTO ingestion_checkpoints (
                chain_id, contract_address, event_name,
                last_successful_block, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chain_id, contract_address, event_name) DO UPDATE SET
                last_successful_block = MAX(
                    last_successful_block, excluded.last_successful_block
                ),
                updated_at = excluded.updated_at
            """,
            (
                chain_id,
                contract_address,
                event_name,
                last_successful_block,
                _now_iso(),
            ),
        )

    def get_checkpoint(
        self,
        *,
        chain_id: int,
        contract_address: str,
        event_name: str,
    ) -> int | None:
        with self.read() as conn:
            row = conn.execute(
                """
                SELECT last_successful_block FROM ingestion_checkpoints
                WHERE chain_id = ? AND contract_address = ? AND event_name = ?
                """,
                (chain_id, contract_address, event_name),
            ).fetchone()
        if row is None:
            return None
        return int(row["last_successful_block"])


__all__ = [
    "AccountingInterval",
    "ConflictingObservation",
    "MANIFEST_SCHEMA_VERSION",
    "ManifestStore",
    "PartitionBounds",
    "PartitionManifestRow",
    "PartitionQualification",
    "validate_endpoint_alias",
]
