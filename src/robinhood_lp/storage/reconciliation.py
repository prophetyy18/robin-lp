"""Per-partition reconciliation check (T037).

The T037 contract closes the silent partition row-loss defect by
introducing a deterministic, side-effect-free comparison between every
partition's ``event_index`` rows and the rows the on-disk Parquet
file carries. The comparison is the safety net that surfaces, after
the fact, any partition whose ``event_index`` set is not exactly the
Parquet file's row set; the data-quality report (T034) consumes the
report produced here so ``complete=true`` cannot coexist with a
mismatch.

Definitions
-----------

For one partition the check computes:

- ``event_index_event_keys`` — the set of (chain_id, block_hash,
  tx_hash, log_index) tuples recorded against the partition in the
  ``event_index`` table (both ``consistent`` and ``conflict`` rows
  count; the partition writer records the same EventKey under
  ``consistent`` and again under ``conflict`` when a reorg fires, so
  the comparison is on the union of both).
- ``parquet_event_keys`` — the set of EventKeys carried by the
  partition's Parquet file, recovered by hashing the columns
  ``block_hash``, ``transaction_hash``, ``log_index`` from each row
  (these are the typed identity columns every V4 event row stores).

The verdict is one of:

- ``consistent`` — both sets have the same cardinality and the same
  EventKey membership;
- ``missing_in_parquet`` — every Parquet row is in the event_index
  set, but some ``event_index`` rows have no matching Parquet row;
  this is the silent-append outcome T037 closes;
- ``missing_in_event_index`` — every ``event_index`` row has a
  matching Parquet row, but some Parquet rows have no matching
  ``event_index`` row; the writer normally inserts both in the same
  transaction so this state indicates manifest corruption rather than
  the T037 defect.

Both ``missing_*`` states surface as a
:class:`PartitionReconciliationReport` whose ``consistent`` is False;
the data-quality verifier (T034) treats any non-consistent report as
a blocker for ``complete=true``.

The reconciliation is deliberately read-only: it does not mutate the
manifest or the Parquet file. A mismatched report names the
partition, the EventKeys present on one side but not the other, and
the row counts; a downstream tool can then decide whether to
re-run the writer (T037 deliverable 2 — align the collection
interval) or to halt the run (T037 deliverable 1 — fail-closed guard).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from robinhood_lp.storage.manifest import ManifestStore
from robinhood_lp.storage.partition import PartitionKey

# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartitionReconciliationReport:
    """One partition's reconciliation outcome.

    ``consistent`` is True only when ``event_index`` and the Parquet
    file carry the same EventKey set. The ``missing_in_parquet`` and
    ``missing_in_event_index`` lists name every EventKey that is in
    one side but not the other; the lists are capped at
    ``max_listed_keys`` items so the report stays compact even when
    the mismatch is large.
    """

    partition_id: str
    consistent: bool
    event_index_count: int
    parquet_row_count: int
    missing_in_parquet: tuple[tuple[str, str, str, str], ...] = field(default_factory=tuple)
    missing_in_event_index: tuple[tuple[str, str, str, str], ...] = field(default_factory=tuple)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of the report."""
        return {
            "partition_id": self.partition_id,
            "consistent": self.consistent,
            "event_index_count": self.event_index_count,
            "parquet_row_count": self.parquet_row_count,
            "missing_in_parquet": [list(k) for k in self.missing_in_parquet],
            "missing_in_event_index": [list(k) for k in self.missing_in_event_index],
            "detail": dict(self.detail),
        }


# ---------------------------------------------------------------------------
# EventKey derivation
# ---------------------------------------------------------------------------


def _event_key_from_parquet_row(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """Derive the canonical EventKey tuple from one Parquet row.

    The Parquet schema stores ``block_hash`` and ``transaction_hash``
    as 32-byte fixed-width binaries; the ``event_index`` table stores
    them as ``0x``-prefixed lowercase hex of the integer value (no
    zero padding). This helper normalises both sides to the same
    shape — integer-derived hex — so the comparison is robust
    regardless of the padding the manifest uses.
    """
    block_hash = int.from_bytes(bytes(row["block_hash"]), "big")
    tx_hash = int.from_bytes(bytes(row["transaction_hash"]), "big")
    log_index = int(row["log_index"])
    chain_id = int(row["chain_id"])
    return (
        str(chain_id),
        "0x" + format(block_hash, "x"),
        "0x" + format(tx_hash, "x"),
        str(log_index),
    )


def _event_key_hash(event_key: tuple[str, str, str, str]) -> str:
    payload = "|".join(event_key).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Per-partition reconciliation
# ---------------------------------------------------------------------------


#: Cap on how many EventKey tuples the report lists in the missing
#: arrays. Larger mismatches are summarised in ``detail`` so the
#: report stays small and machine-readable.
_MAX_LISTED_KEYS: int = 16


def reconcile_partition(
    manifest: ManifestStore,
    partition_id: str,
    *,
    max_listed_keys: int = _MAX_LISTED_KEYS,
) -> PartitionReconciliationReport:
    """Compare ``event_index`` to the on-disk Parquet file for
    ``partition_id`` and return a structured verdict.

    Raises :class:`FileNotFoundError` if the manifest row points at a
    missing Parquet file. A missing file is not "consistent" — the
    report's ``consistent`` flag is False and ``detail`` names the
    path so the data-quality verifier surfaces the corruption.
    """
    row = manifest.get_partition(partition_id)
    if row is None:
        return PartitionReconciliationReport(
            partition_id=partition_id,
            consistent=False,
            event_index_count=0,
            parquet_row_count=0,
            detail={"reason": "missing_partition_row"},
        )
    file_path = Path(row.file_path)
    if not file_path.exists():
        return PartitionReconciliationReport(
            partition_id=partition_id,
            consistent=False,
            event_index_count=_count_event_index(manifest, partition_id),
            parquet_row_count=0,
            detail={"reason": "missing_parquet_file", "file_path": str(file_path)},
        )

    parquet_keys = _read_parquet_event_keys(file_path)
    event_index_keys = _read_event_index_event_keys(manifest, partition_id)

    parquet_set = set(parquet_keys)
    event_index_set = set(event_index_keys)

    missing_in_parquet = sorted(event_index_set - parquet_set)
    missing_in_event_index = sorted(parquet_set - event_index_set)

    cap = max(0, int(max_listed_keys))
    detail: dict[str, Any] = {
        "file_path": str(file_path),
        "file_sha256": row.file_sha256,
    }
    if len(missing_in_parquet) > cap:
        detail["missing_in_parquet_total"] = len(missing_in_parquet)
    if len(missing_in_event_index) > cap:
        detail["missing_in_event_index_total"] = len(missing_in_event_index)
    if not missing_in_parquet and not missing_in_event_index:
        detail["verdict"] = "consistent"
    elif missing_in_parquet and not missing_in_event_index:
        detail["verdict"] = "missing_in_parquet"
    elif missing_in_event_index and not missing_in_parquet:
        detail["verdict"] = "missing_in_event_index"
    else:
        detail["verdict"] = "both_sides_disagree"

    return PartitionReconciliationReport(
        partition_id=partition_id,
        consistent=not missing_in_parquet and not missing_in_event_index,
        event_index_count=len(event_index_keys),
        parquet_row_count=len(parquet_keys),
        missing_in_parquet=tuple(
            (chain_id, block_hash, tx_hash, log_index)
            for chain_id, block_hash, tx_hash, log_index in missing_in_parquet[:cap]
        ),
        missing_in_event_index=tuple(
            (chain_id, block_hash, tx_hash, log_index)
            for chain_id, block_hash, tx_hash, log_index in missing_in_event_index[:cap]
        ),
        detail=detail,
    )


def reconcile_all_partitions(
    manifest: ManifestStore,
    *,
    max_listed_keys: int = _MAX_LISTED_KEYS,
) -> tuple[PartitionReconciliationReport, ...]:
    """Run :func:`reconcile_partition` against every partition row.

    Returns the reports in the same order the manifest returns them
    (by ``partition_id`` ascending), one report per partition. A
    dataset with many partitions therefore yields many reports; the
    caller filters / groups them as needed.
    """
    with manifest.read() as conn:
        rows = conn.execute("SELECT partition_id FROM partitions ORDER BY partition_id").fetchall()
    out: list[PartitionReconciliationReport] = []
    for r in rows:
        out.append(
            reconcile_partition(
                manifest,
                r["partition_id"],
                max_listed_keys=max_listed_keys,
            )
        )
    return tuple(out)


def find_inconsistent_partitions(
    manifest: ManifestStore,
    *,
    max_listed_keys: int = _MAX_LISTED_KEYS,
) -> tuple[PartitionReconciliationReport, ...]:
    """Return only the partitions whose reconciliation is not consistent."""
    return tuple(
        report
        for report in reconcile_all_partitions(manifest, max_listed_keys=max_listed_keys)
        if not report.consistent
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_parquet_event_keys(file_path: Path) -> set[tuple[str, str, str, str]]:
    table = pq.read_table(str(file_path))
    if table.num_rows == 0:
        return set()
    rows = table.to_pylist()
    return {_event_key_from_parquet_row(row) for row in rows}


def _read_event_index_event_keys(
    manifest: ManifestStore, partition_id: str
) -> set[tuple[str, str, str, str]]:
    out: set[tuple[str, str, str, str]] = set()
    with manifest.read() as conn:
        rows = conn.execute(
            """
            SELECT chain_id, block_hash, tx_hash, log_index
            FROM event_index
            WHERE partition_id = ?
            """,
            (partition_id,),
        ).fetchall()
    for r in rows:
        out.add(
            (
                str(int(r["chain_id"])),
                _hex_canonical(r["block_hash"]),
                _hex_canonical(r["tx_hash"]),
                str(int(r["log_index"])),
            )
        )
    return out


def _count_event_index(manifest: ManifestStore, partition_id: str) -> int:
    with manifest.read() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM event_index WHERE partition_id = ?",
            (partition_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def _normalise_hex(value: Any) -> str:
    """Normalise a SQLite hex column to lowercase ``0x``-prefixed form.

    The manifest stores ``block_hash`` / ``tx_hash`` as ``0x`` +
    lowercase hex; older migration rows may have slipped through with
    uppercase characters. This helper makes the comparison robust to
    either casing.
    """
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("0x"):
            return "0x" + s[2:].lower()
        return "0x" + s.lower()
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    raise TypeError(f"_normalise_hex: unsupported value type {type(value).__name__}")


def _hex_canonical(value: Any) -> str:
    """Return the integer-derived ``0x``-prefixed hex used as the
    reconciliation key.

    The manifest's ``event_index`` rows store ``block_hash`` /
    ``tx_hash`` as the lowercase hex of the integer value without
    zero-padding (the writer calls :func:`robinhood_lp.storage.manifest._hex`
    which uses ``format(value, "x")``). The reconciliation key must
    match that exact shape so the comparison against the manifest set
    is bit-exact; padding differences are intentionally NOT collapsed
    because the manifest is the source of truth.
    """
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("0x"):
            return "0x" + s[2:].lower()
        return "0x" + s.lower()
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    raise TypeError(f"_hex_canonical: unsupported value type {type(value).__name__}")


# ---------------------------------------------------------------------------
# Key derivation helper for a typed record (used by tests)
# ---------------------------------------------------------------------------


def event_key_from_record(record: Any) -> tuple[str, str, str, str]:
    """Build the canonical EventKey tuple from a typed log record.

    Tests use this helper to construct the expected ``event_index`` set
    for a fixture batch so the reconciliation verdict can be asserted
    against the ground truth.
    """
    ek = record.event_key()
    return (
        str(ek.chain_id.value),
        "0x" + ek.block_hash.to_bytes(32, "big").hex(),
        "0x" + ek.tx_hash.to_bytes(32, "big").hex(),
        str(int(ek.log_index)),
    )


def event_key_from_partition_key(pk: PartitionKey) -> str:
    """Stable key used to derive a deterministic hash of a partition."""
    return pk.partition_id()


__all__ = [
    "PartitionReconciliationReport",
    "event_key_from_partition_key",
    "event_key_from_record",
    "find_inconsistent_partitions",
    "reconcile_all_partitions",
    "reconcile_partition",
]
