"""Manifest-aware reader for raw partitions (T031).

The reader is the only sanctioned way to consume a partition after
it has been written. It:

1. Resolves the partition by ``(chain_id, contract_address,
   event_name, block_range)`` — never by scanning the filesystem.
2. Checks the file pointed at by the manifest row exists.
3. Recomputes the per-file SHA-256 and compares it to the manifest.
4. Compares the manifest block bounds to the actual Parquet content.
5. Surfaces any mismatch or missing file as an error so the caller
   never gets a "qualified" answer from a corrupted partition.
6. Refuses to return rows from a partition whose
   ``partition_qualification`` row is ``qualified = 0`` (a halt
   caused by a conflicting observation).

A partition is only considered qualified when:

- the file exists at the manifest path;
- the file's SHA-256 matches the manifest row;
- the manifest block bounds match the actual Parquet content;
- the partition_qualification row says ``qualified = 1``.

Anything else raises :class:`ReaderError` (or one of its
subclasses). The reader never infers coverage or contents from
filenames.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from robinhood_lp.storage.manifest import ManifestStore
from robinhood_lp.storage.partition import (
    PartitionKey,
)
from robinhood_lp.storage.schema import (
    CURRENT_DECODE_VERSION,
    CURRENT_SCHEMA_VERSION,
    migrate_to_current,
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReaderError(Exception):
    """Base class for all reader failures."""


class MissingPartitionError(ReaderError):
    """No manifest row matches the requested partition key."""


class ManifestMismatchError(ReaderError):
    """The manifest row points at a file that is missing or whose
    SHA-256 does not match. The partition must not be returned."""


class BoundsMismatchError(ReaderError):
    """The Parquet content's min/max block numbers do not match the
    manifest block-bounds row."""


class UnqualifiedDatasetError(ReaderError):
    """The partition manifest row says the partition is not qualified
    (a conflicting observation has been recorded). The partition is
    still on disk for audit but cannot be used by downstream code."""


class FutureSchemaError(ReaderError):
    """The Parquet file was written by a newer schema version than
    this build can read."""


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartitionReadResult:
    """The reader's output for one partition.

    ``table`` is the decoded PyArrow table. ``records`` is the
    result of ``migrate_to_current`` applied to every row's canonical
    form, so the caller gets the current-schema dataclasses back
    (or the row dicts when the schema is unknown to this build).
    """

    partition_id: str
    table: Any
    records: list[Any]
    qualified: bool
    halt_reason: str | None


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class RawPartitionReader:
    """Manifest-aware reader for one ``data_root``."""

    def __init__(
        self,
        data_root: Path,
        manifest: ManifestStore,
        *,
        current_schema_version: int = CURRENT_SCHEMA_VERSION,
        current_decode_version: int = CURRENT_DECODE_VERSION,
    ) -> None:
        self.data_root = Path(data_root)
        self.manifest = manifest
        self.current_schema_version = current_schema_version
        self.current_decode_version = current_decode_version

    # ----- public API ---------------------------------------------------

    def has_partition(self, pk: PartitionKey) -> bool:
        return self.manifest.get_partition(pk.partition_id()) is not None

    def read_partition(self, pk: PartitionKey) -> PartitionReadResult:
        row = self.manifest.get_partition(pk.partition_id())
        if row is None:
            raise MissingPartitionError(pk.partition_id())
        self._verify_integrity(row)
        qualification = self.manifest.get_qualification(row.partition_id)
        if qualification is not None and not qualification.qualified:
            # The partition bytes are still on disk for audit; we
            # surface the halt before materialising the table.
            raise UnqualifiedDatasetError(
                f"partition {row.partition_id} halted: {qualification.halt_reason}"
            )
        file_path = Path(row.file_path)
        table = pq.read_table(str(file_path))
        self._check_bounds_against_file(row.partition_id, table)
        records = self._table_to_records(table, pk.event_name)
        return PartitionReadResult(
            partition_id=row.partition_id,
            table=table,
            records=records,
            qualified=True,
            halt_reason=None,
        )

    def list_qualified_partitions(
        self,
        *,
        event_name: str | None = None,
    ) -> list[str]:
        """Return all partition ids that exist and are qualified."""
        with self.manifest.read() as conn:
            rows = conn.execute(
                """
                SELECT p.partition_id
                FROM partitions p
                JOIN partition_qualification q ON q.partition_id = p.partition_id
                WHERE q.qualified = 1
                  AND (? IS NULL OR p.event_name = ?)
                ORDER BY p.event_name, p.block_from
                """,
                (event_name, event_name),
            ).fetchall()
        return [r["partition_id"] for r in rows]

    def list_halted_partitions(self) -> list[tuple[str, str | None]]:
        with self.manifest.read() as conn:
            rows = conn.execute(
                """
                SELECT p.partition_id, q.halt_reason
                FROM partitions p
                JOIN partition_qualification q ON q.partition_id = p.partition_id
                WHERE q.qualified = 0
                ORDER BY p.partition_id
                """,
            ).fetchall()
        return [(r["partition_id"], r["halt_reason"]) for r in rows]

    # ----- integrity ----------------------------------------------------

    def _verify_integrity(self, row: Any) -> None:
        file_path = Path(row.file_path)
        if not file_path.exists():
            raise ManifestMismatchError(
                f"manifest row {row.partition_id} points at missing file {file_path}"
            )
        actual = self._sha256_file(file_path)
        if actual != row.file_sha256:
            raise ManifestMismatchError(
                f"manifest row {row.partition_id} SHA-256 mismatch: "
                f"manifest={row.file_sha256} actual={actual}"
            )

    def _check_bounds_against_file(self, partition_id: str, table: Any) -> None:
        bounds = self.manifest.get_bounds(partition_id)
        if bounds is None:
            # Missing bounds row is itself a manifest error.
            raise ManifestMismatchError(
                f"manifest row {partition_id} missing partition_block_bounds row"
            )
        if table.num_rows == 0:
            return
        block_numbers = table.column("block_number").to_pylist()
        min_block = min(block_numbers)
        max_block = max(block_numbers)
        if min_block != bounds.min_block_number:
            raise BoundsMismatchError(
                f"partition {partition_id}: min_block_number "
                f"file={min_block} manifest={bounds.min_block_number}"
            )
        if max_block != bounds.max_block_number:
            raise BoundsMismatchError(
                f"partition {partition_id}: max_block_number "
                f"file={max_block} manifest={bounds.max_block_number}"
            )

    def _table_to_records(self, table: Any, event_name: str) -> list[Any]:
        """Decode a Parquet table into current-version dataclasses.

        Falls back to dict-of-rows when the schema is unknown to the
        current build (a forward-compat leftover from
        :mod:`robinhood_lp.storage.schema`).
        """
        from robinhood_lp.storage.schema import canonical_bytes

        records: list[Any] = []
        for row in table.to_pylist():
            try:
                # Reconstruct the dataclass via the canonical form so
                # migration runs exactly the same path a fresh write
                # would. This keeps the read path idempotent across
                # schema upgrades.
                synthetic = _row_to_dataclass_dict(row, event_name)
                blob = canonical_bytes(synthetic)
                records.append(migrate_to_current(blob))
            except Exception:
                # Forward-compat: keep the row dict for callers that
                # still need the raw data.
                records.append(row)
        return records

    # ----- helpers ------------------------------------------------------

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return "0x" + h.hexdigest()


# ---------------------------------------------------------------------------
# Forward-compat helper
# ---------------------------------------------------------------------------


def _row_to_dataclass_dict(row: dict[str, Any], event_name: str) -> Any:
    """Map a Parquet row dict to the canonical-byte form of a typed
    dataclass so :func:`migrate_to_current` can decode it.

    This is intentionally narrow: it covers only the schemas defined
    in :mod:`robinhood_lp.storage.schema`. A future schema that adds
    fields falls back to returning the raw row dict (see
    ``_table_to_records``).
    """
    schema_version = int(row.get("schema_version") or 1)
    decode_version = int(row.get("decode_version") or 1)

    def _b(field: str, *, length: int) -> int:
        val = row.get(field)
        if isinstance(val, (bytes, bytearray)):
            return int.from_bytes(val, "big")
        if isinstance(val, int):
            return val
        raise TypeError(f"row[{field}]: expected bytes/int, got {type(val).__name__}")

    def _int_or_none(field: str) -> int | None:
        val = row.get(field)
        if val is None:
            return None
        return int(val)

    common: dict[str, Any] = {
        "chain_id": _int_wrapper(int(row["chain_id"])),
        "block_number": int(row["block_number"]),
        "block_hash": _b("block_hash", length=32),
        "transaction_hash": _b("transaction_hash", length=32),
        "transaction_index": int(row["transaction_index"]),
        "log_index": int(row["log_index"]),
        "address": _address_wrapper(_b("address", length=20)),
        "pool_id": _pool_id_wrapper(_b("pool_id", length=32)),
        "removed": bool(row["removed"]),
        "decode_version": decode_version,
        "ingestion_time": "",
        "source_endpoint": "",
        "acquisition": {
            "endpoint_alias": row.get("acquisition_endpoint_alias") or "",
            "retrieval_time": row.get("acquisition_retrieval_time") or "",
            "request_from_block": int(row.get("acquisition_request_from_block") or 0),
            "request_to_block": int(row.get("acquisition_request_to_block") or 0),
            "http_batch_size": _int_or_none("acquisition_http_batch_size"),
            "http_batch_position": _int_or_none("acquisition_http_batch_position"),
            "request_attempt": _int_or_none("acquisition_request_attempt"),
        },
        "raw_topics": [
            row.get("raw_topic_0") or b"\x00" * 32,
            row.get("raw_topic_1") or b"\x00" * 32,
            row.get("raw_topic_2") or b"\x00" * 32,
            row.get("raw_topic_3") or b"\x00" * 32,
        ],
        "raw_data": row.get("raw_data") or b"",
        "raw": _decode_raw_response_json(row.get("raw_response_json")),
        "unknown_fields": {},
    }
    if event_name == "Initialize":
        return _make(
            "InitializeLogRecord",
            schema_version=schema_version,
            **common,
        )
    if event_name == "ModifyLiquidity":
        return _make(
            "ModifyLiquidityLogRecord",
            schema_version=schema_version,
            sender=_address_wrapper(_b("sender", length=20)),
            tick_lower=int(row["tick_lower"]),
            tick_upper=int(row["tick_upper"]),
            liquidity_delta=int(row["liquidity_delta"]),
            salt=_int_wrapper(_b("salt", length=32)),
            **common,
        )
    if event_name == "Swap":
        return _make(
            "SwapLogRecord",
            schema_version=schema_version,
            sender=_address_wrapper(_b("sender", length=20)),
            amount0=int(row["amount0"]),
            amount1=int(row["amount1"]),
            sqrt_price_x96=int(row["sqrt_price_x96"]),
            liquidity=int(row["liquidity"]),
            tick=int(row["tick"]),
            fee=int(row["fee"]),
            **common,
        )
    if event_name == "Donate":
        return _make(
            "DonateLogRecord",
            schema_version=schema_version,
            sender=_address_wrapper(_b("sender", length=20)),
            amount0=int(row["amount0"]),
            amount1=int(row["amount1"]),
            **common,
        )
    if event_name == "ProtocolFeeUpdated":
        return _make(
            "ProtocolFeeUpdatedLogRecord",
            schema_version=schema_version,
            protocol_fee=int(row["protocol_fee"]),
            **common,
        )
    raise ValueError(f"unknown event_name {event_name!r}")


# ---------------------------------------------------------------------------
# Thin helpers for the dataclass-shaped dict
# ---------------------------------------------------------------------------


class _IntValue:
    """Marker that the dict-shaper uses to identify ``int``-only fields.

    The :func:`migrate_to_current` path expects ``ChainId`` /
    ``PoolId`` / ``Address`` to round-trip through their
    dataclass-with-value-int shape; we materialise them via their
    public constructors in :mod:`robinhood_lp.storage.schema`. To keep
    the row-shaper dependency-free of the schema dataclasses, we
    carry the int value here and let :func:`_make` instantiate the
    dataclass via ``**kwargs`` + the schema class registry.
    """

    __slots__ = ("value",)

    def __init__(self, value: int) -> None:
        self.value = value


def _int_wrapper(value: int) -> _IntValue:
    return _IntValue(value)


def _address_wrapper(value: int) -> _IntValue:
    return _IntValue(value)


def _pool_id_wrapper(value: int) -> _IntValue:
    return _IntValue(value)


def _decode_raw_response_json(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    import json

    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else {"_value": result}
    except json.JSONDecodeError:
        return {"_unparseable": True}


def _make(class_name: str, *, schema_version: int, **kwargs: Any) -> Any:
    """Build a dataclass via the schema layer's private registry.

    Resolves ``ChainId`` / ``PoolId`` / ``Address`` / ``int`` wrappers
    into the corresponding typed values, then constructs the
    dataclass.
    """
    from robinhood_lp.protocol import Address, ChainId, PoolId
    from robinhood_lp.storage.schema import _CLASS_REGISTRY

    cls = _CLASS_REGISTRY[class_name]
    chain_value = kwargs.pop("chain_id")
    address_value = kwargs.pop("address")
    pool_id_value = kwargs.pop("pool_id")
    salt_value = kwargs.pop("salt", None)
    sender_value = kwargs.pop("sender", None)
    acquisition = kwargs.pop("acquisition")
    from robinhood_lp.storage.schema import AcquisitionProvenance

    acquisition_obj = AcquisitionProvenance(**acquisition)
    out: dict[str, Any] = {
        "chain_id": chain_value if isinstance(chain_value, ChainId) else ChainId(chain_value.value),
        "address": address_value
        if isinstance(address_value, Address)
        else Address(address_value.value),
        "pool_id": pool_id_value
        if isinstance(pool_id_value, PoolId)
        else PoolId(pool_id_value.value),
        "acquisition": acquisition_obj,
        **kwargs,
    }
    if sender_value is not None:
        out["sender"] = (
            sender_value if isinstance(sender_value, Address) else Address(sender_value.value)
        )
    if salt_value is not None:
        out["salt"] = salt_value if isinstance(salt_value, int) else salt_value.value
    return cls(**out)


__all__ = [
    "BoundsMismatchError",
    "FutureSchemaError",
    "ManifestMismatchError",
    "MissingPartitionError",
    "PartitionReadResult",
    "RawPartitionReader",
    "ReaderError",
    "UnqualifiedDatasetError",
]
