"""Append-only reorg journal and critical-incident recorder (T033).

The reorg journal lives inside the same SQLite manifest database the
storage layer already maintains (per ADR-002). It owns two new
tables:

- ``reorg_journal`` (already present from T031) — one row per orphan
  block. The journal only ever appends; existing rows are never
  rewritten. ``reorg_journal`` carries the orphan / replacement hash,
  the demotion reason, and the partition id the demotion affected.
- ``critical_incidents`` (introduced here) — one row per
  ``FINALIZED_ANCESTRY_VIOLATION`` detection. The table is strictly
  append-only: rows are inserted with a unique autoincrement id and
  are never updated or deleted.

The journal module deliberately exposes a narrow surface so callers
cannot mutate raw evidence. The only mutation primitives are:

- :meth:`ReorgJournal.record_orphan` — append one orphan marker;
- :meth:`ReorgJournal.record_critical_incident_candidate` — append
  one ``FINALIZED_ANCESTRY_VIOLATION`` candidate and return the new
  row id.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from robinhood_lp.storage.manifest import (
    ManifestStore,
    validate_endpoint_alias,
)
from robinhood_lp.storage.reorg.policy import (
    FINALIZED_ANCESTRY_VIOLATION,
    VALID_REORG_REASON_CODES,
    FinalityPolicyError,
)

# ---------------------------------------------------------------------------
# Schema additions
# ---------------------------------------------------------------------------

#: Bumped from the T031 value. T033 introduces the critical_incidents
#: table. The reader side never auto-upgrades a manifest whose
#: version is higher than this constant; only an explicit migration
#: path (out of scope for T033) advances the version.
REORG_MANIFEST_SCHEMA_VERSION: Final[int] = 3

_CRITICAL_INCIDENTS_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS critical_incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chain_id INTEGER NOT NULL,
        incident_kind TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        endpoint_alias TEXT,
        block_number INTEGER,
        block_hash TEXT,
        parent_block_hash TEXT,
        detail_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS critical_incidents_chain_kind
        ON critical_incidents (chain_id, incident_kind)
    """,
    """
    CREATE INDEX IF NOT EXISTS critical_incidents_recorded_at
        ON critical_incidents (recorded_at)
    """,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReorgJournalEntry:
    """In-memory mirror of one ``reorg_journal`` row."""

    id: int
    chain_id: int
    block_number: int
    orphan_block_hash: str
    replacement_block_hash: str | None
    demotion_reason: str
    partition_id: str | None
    recorded_at: str


@dataclass(frozen=True, slots=True)
class CriticalIncident:
    """In-memory mirror of one ``critical_incidents`` row."""

    id: int
    chain_id: int
    incident_kind: str
    reason_code: str
    endpoint_alias: str | None
    block_number: int | None
    block_hash: str | None
    parent_block_hash: str | None
    detail_json: str
    recorded_at: str


# ---------------------------------------------------------------------------
# Manifest bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_critical_incidents(conn: sqlite3.Connection) -> None:
    for ddl in _CRITICAL_INCIDENTS_DDL:
        conn.execute(ddl)


# ---------------------------------------------------------------------------
# Reorg journal
# ---------------------------------------------------------------------------


class ReorgJournal:
    """Append-only reorg journal wrapped around the manifest store.

    The journal exposes only the write / read helpers the handler
    needs. It never deletes rows, never updates existing rows, and
    never exposes a mutating handle to the underlying SQLite
    connection (caller code only ever uses the manifest's read /
    transaction context managers).
    """

    def __init__(self, manifest: ManifestStore) -> None:
        if not isinstance(manifest, ManifestStore):
            raise TypeError(
                f"ReorgJournal: manifest must be ManifestStore, got {type(manifest).__name__}"
            )
        self._manifest = manifest
        # The T033 critical-incident table is created lazily so a
        # pre-existing T031 manifest opens cleanly.
        with self._manifest.transaction() as conn:
            _bootstrap_critical_incidents(conn)

    @property
    def manifest(self) -> ManifestStore:
        return self._manifest

    # ----- orphan marking ------------------------------------------------

    def record_orphan(
        self,
        *,
        chain_id: int,
        block_number: int,
        orphan_block_hash: str,
        replacement_block_hash: str | None,
        demotion_reason: str,
        partition_id: str | None,
    ) -> ReorgJournalEntry:
        """Append one ``reorg_journal`` row.

        ``demotion_reason`` must be a member of
        :data:`VALID_REORG_REASON_CODES`; anything else is rejected
        so the manifest never records an undocumented reason. The
        orphan / replacement hashes are normalised to lowercase
        ``0x``-prefixed hex before they hit the journal.
        """
        if not isinstance(chain_id, int) or isinstance(chain_id, bool):
            raise TypeError(
                f"ReorgJournal.record_orphan: chain_id must be int, got {type(chain_id).__name__}"
            )
        if not isinstance(block_number, int) or isinstance(block_number, bool):
            raise TypeError(
                f"ReorgJournal.record_orphan: block_number must be int, "
                f"got {type(block_number).__name__}"
            )
        if block_number < 0:
            raise ValueError(
                f"ReorgJournal.record_orphan: block_number must be >= 0, got {block_number}"
            )
        if demotion_reason not in VALID_REORG_REASON_CODES:
            raise FinalityPolicyError(
                f"ReorgJournal.record_orphan: unknown demotion_reason {demotion_reason!r}; "
                f"valid options are {sorted(VALID_REORG_REASON_CODES)!r}"
            )
        if partition_id is not None:
            validate_endpoint_alias("", field="reorg_journal.partition_id")
        normalized_orphan_str = _normalize_hash(orphan_block_hash, field="orphan_block_hash")
        # ``orphan_block_hash`` is typed as ``str`` (not Optional) on the
        # method signature, so ``_normalize_hash`` cannot return ``None``
        # here; the assertion documents the invariant for mypy.
        assert normalized_orphan_str is not None
        normalized_orphan: str = normalized_orphan_str
        normalized_replacement: str | None = None
        if replacement_block_hash is not None:
            normalized_replacement = _normalize_hash(
                replacement_block_hash, field="replacement_block_hash"
            )

        with self._manifest.transaction() as conn:
            cur = conn.execute(
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
                    normalized_orphan,
                    normalized_replacement,
                    demotion_reason,
                    partition_id,
                    _now_iso(),
                ),
            )
            row_id_raw = cur.lastrowid
            # SQLite's ``lastrowid`` is always populated for an
            # INSERT against a table with an INTEGER PRIMARY KEY;
            # assert before narrowing for mypy.
            assert row_id_raw is not None
            row_id = int(row_id_raw)
        return ReorgJournalEntry(
            id=row_id,
            chain_id=chain_id,
            block_number=block_number,
            orphan_block_hash=normalized_orphan,
            replacement_block_hash=normalized_replacement,
            demotion_reason=demotion_reason,
            partition_id=partition_id,
            recorded_at=self._fetch_recorded_at(row_id),
        )

    def _fetch_recorded_at(self, row_id: int) -> str:
        with self._manifest.read() as conn:
            row = conn.execute(
                "SELECT recorded_at FROM reorg_journal WHERE id = ?", (row_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError(
                f"ReorgJournal: row {row_id} disappeared after insert (manifest corruption)"
            )
        return str(row["recorded_at"])

    def list_reorgs(self) -> list[ReorgJournalEntry]:
        with self._manifest.read() as conn:
            rows = conn.execute("SELECT * FROM reorg_journal ORDER BY id").fetchall()
        return [
            ReorgJournalEntry(
                id=int(r["id"]),
                chain_id=int(r["chain_id"]),
                block_number=int(r["block_number"]),
                orphan_block_hash=str(r["orphan_block_hash"]),
                replacement_block_hash=(
                    str(r["replacement_block_hash"]) if r["replacement_block_hash"] else None
                ),
                demotion_reason=str(r["demotion_reason"]),
                partition_id=r["partition_id"],
                recorded_at=str(r["recorded_at"]),
            )
            for r in rows
        ]

    # ----- critical incidents -------------------------------------------

    def record_critical_incident_candidate(
        self,
        *,
        chain_id: int,
        incident_kind: str,
        reason_code: str,
        detail: dict[str, Any],
        endpoint_alias: str | None = None,
        block_number: int | None = None,
        block_hash: str | None = None,
        parent_block_hash: str | None = None,
    ) -> CriticalIncident:
        """Append one ``FINALIZED_ANCESTRY_VIOLATION`` candidate.

        The recorder is append-only: rows are never updated or deleted
        in place. The handler is responsible for surfacing the
        candidate to the Phase 8 operator / incident workflow; T033
        detects, halts, journals, and escalates, and stops short of
        the operator declaration (per the contract must-not).
        """
        if not isinstance(chain_id, int) or isinstance(chain_id, bool):
            raise TypeError(
                f"CriticalIncidentRecorder: chain_id must be int, got {type(chain_id).__name__}"
            )
        if incident_kind != FINALIZED_ANCESTRY_VIOLATION:
            raise FinalityPolicyError(
                "CriticalIncidentRecorder: incident_kind must be "
                f"{FINALIZED_ANCESTRY_VIOLATION!r}, got {incident_kind!r}"
            )
        if reason_code not in (FINALIZED_ANCESTRY_VIOLATION,):
            raise FinalityPolicyError(
                f"CriticalIncidentRecorder: reason_code must be {FINALIZED_ANCESTRY_VIOLATION!r}, "
                f"got {reason_code!r}"
            )
        if endpoint_alias is not None:
            validate_endpoint_alias(endpoint_alias, field="critical_incidents.endpoint_alias")
        if block_number is not None and (
            not isinstance(block_number, int) or isinstance(block_number, bool)
        ):
            raise TypeError(
                "CriticalIncidentRecorder: block_number must be int or None, "
                f"got {type(block_number).__name__}"
            )
        normalized_hash = _normalize_hash(block_hash, field="block_hash")
        normalized_parent = _normalize_hash(parent_block_hash, field="parent_block_hash")
        detail_json = _serialize_detail(detail)

        with self._manifest.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO critical_incidents (
                    chain_id, incident_kind, reason_code, endpoint_alias,
                    block_number, block_hash, parent_block_hash, detail_json,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain_id,
                    incident_kind,
                    reason_code,
                    endpoint_alias,
                    block_number,
                    normalized_hash,
                    normalized_parent,
                    detail_json,
                    _now_iso(),
                ),
            )
            row_id_raw = cur.lastrowid
            assert row_id_raw is not None
            row_id = int(row_id_raw)
        return self._fetch_critical_incident(row_id)

    def _fetch_critical_incident(self, row_id: int) -> CriticalIncident:
        with self._manifest.read() as conn:
            row = conn.execute(
                "SELECT * FROM critical_incidents WHERE id = ?", (row_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError(f"CriticalIncidentRecorder: row {row_id} disappeared after insert")
        return CriticalIncident(
            id=int(row["id"]),
            chain_id=int(row["chain_id"]),
            incident_kind=str(row["incident_kind"]),
            reason_code=str(row["reason_code"]),
            endpoint_alias=row["endpoint_alias"],
            block_number=(int(row["block_number"]) if row["block_number"] is not None else None),
            block_hash=row["block_hash"],
            parent_block_hash=row["parent_block_hash"],
            detail_json=str(row["detail_json"]),
            recorded_at=str(row["recorded_at"]),
        )

    def list_critical_incidents(self) -> list[CriticalIncident]:
        with self._manifest.read() as conn:
            rows = conn.execute("SELECT * FROM critical_incidents ORDER BY id").fetchall()
        return [
            CriticalIncident(
                id=int(r["id"]),
                chain_id=int(r["chain_id"]),
                incident_kind=str(r["incident_kind"]),
                reason_code=str(r["reason_code"]),
                endpoint_alias=r["endpoint_alias"],
                block_number=(int(r["block_number"]) if r["block_number"] is not None else None),
                block_hash=r["block_hash"],
                parent_block_hash=r["parent_block_hash"],
                detail_json=str(r["detail_json"]),
                recorded_at=str(r["recorded_at"]),
            )
            for r in rows
        ]

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Expose the manifest's read-only connection for ad-hoc tests."""
        with self._manifest.read() as conn:
            yield conn


# ---------------------------------------------------------------------------
# Critical-incident recorder
# ---------------------------------------------------------------------------


class CriticalIncidentRecorder:
    """Convenience wrapper that exposes the critical-incident API only.

    Storage-layer callers that want to record a critical incident
    without taking a dependency on the full :class:`ReorgJournal`
    can construct this with the same :class:`ManifestStore`. The
    recorder shares the journal's append-only semantics.
    """

    def __init__(self, journal: ReorgJournal) -> None:
        if not isinstance(journal, ReorgJournal):
            raise TypeError(
                "CriticalIncidentRecorder: journal must be ReorgJournal, "
                f"got {type(journal).__name__}"
            )
        self._journal = journal

    def record(
        self,
        *,
        chain_id: int,
        detail: dict[str, Any],
        endpoint_alias: str | None = None,
        block_number: int | None = None,
        block_hash: str | None = None,
        parent_block_hash: str | None = None,
    ) -> CriticalIncident:
        return self._journal.record_critical_incident_candidate(
            chain_id=chain_id,
            incident_kind=FINALIZED_ANCESTRY_VIOLATION,
            reason_code=FINALIZED_ANCESTRY_VIOLATION,
            detail=detail,
            endpoint_alias=endpoint_alias,
            block_number=block_number,
            block_hash=block_hash,
            parent_block_hash=parent_block_hash,
        )

    def list(self) -> list[CriticalIncident]:
        return self._journal.list_critical_incidents()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_hash(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field}: must be str, got {type(value).__name__}")
    s = value.strip().lower()
    if not s.startswith("0x"):
        s = "0x" + s
    body = s[2:]
    if len(body) != 64:
        raise ValueError(f"{field}: not a 32-byte 0x-prefixed hex string ({value!r})")
    try:
        int(body, 16)
    except ValueError as exc:
        raise ValueError(f"{field}: not a hex string ({value!r})") from exc
    return s


def _serialize_detail(detail: dict[str, Any]) -> str:
    import json

    def _default(obj: Any) -> Any:
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj
        raise TypeError(
            f"critical_incidents.detail: unserialisable value of type {type(obj).__name__}"
        )

    return json.dumps(detail, sort_keys=True, ensure_ascii=False, default=_default)


__all__ = [
    "CriticalIncident",
    "CriticalIncidentRecorder",
    "REORG_MANIFEST_SCHEMA_VERSION",
    "ReorgJournal",
    "ReorgJournalEntry",
]
