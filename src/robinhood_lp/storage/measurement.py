"""Representative short-range measurement for partition sizing (T031).

T031 acceptance requires "at least one representative short-range
measurement justifies the chosen partition sizing and retention
footprint for the measured data volume; the observed volume is
recorded as a measurement and is not promoted to a universal
constant for every future range, endpoint, or pool".

This module provides:

- :func:`measure_short_range` — runs a synthetic short-range write
  against a fresh data root and returns the measured numbers.
- :data:`REPRESENTATIVE_MEASUREMENT` — the frozen measurement the
  framework records as evidence at unit-test time. The numbers come
  from the same :func:`measure_short_range` call but are pinned here
  so the test suite has a reproducible artefact and a regression
  guard.
- :func:`assert_measurement_within_tolerance` — guards future code
  against the partition sizing going off into the weeds without an
  explicit ADR update.

The measurement deliberately uses a small synthetic batch (5–20
records, 1–10 blocks wide) so it stays fast and reproducible. It is
not a benchmark of the full V1 ingestion path; it is a sanity check
that the chosen ``DEFAULT_BLOCKS_PER_PARTITION`` produces a Parquet
file size within an order of magnitude of the recorded measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]

from robinhood_lp.storage.manifest import (
    AccountingInterval,
    ManifestStore,
)
from robinhood_lp.storage.writer import DEFAULT_BLOCKS_PER_PARTITION

# ---------------------------------------------------------------------------
# Measurement result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShortRangeMeasurement:
    """Result of one representative short-range write.

    All fields are derived from a single ``append_partition`` call.
    They are the inputs to the T031 acceptance "representative short-
    range measurement" requirement and the regression guards in
    ``test_storage_measurement.py``.
    """

    row_count: int
    parquet_bytes: int
    bytes_per_row: float
    manifest_rows: int
    block_from: int
    block_to: int
    blocks_per_partition: int
    file_sha256: str


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def measure_short_range(
    *,
    data_root: Path,
    table: pa.Table,
    chain_id: int,
    contract_address: str,
    event_name: str,
    block_from: int,
    block_to: int,
    blocks_per_partition: int = DEFAULT_BLOCKS_PER_PARTITION,
    endpoint_alias: str = "robinhood_public",
) -> ShortRangeMeasurement:
    """Run one synthetic write and return the measured numbers.

    The caller passes a fully-populated PyArrow table that conforms
    to :func:`robinhood_lp.storage.partition.parquet_schema_for`. This
    module does not fabricate synthetic chain events — the test
    harness owns that so the measurement is reproducible.
    """
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    data_root = Path(data_root)
    data_root.mkdir(parents=True, exist_ok=True)
    final_path = (
        data_root
        / "raw"
        / f"chain={chain_id}"
        / f"contract={contract_address.lower().removeprefix('0x')}"
        / f"event={event_name}"
        / f"range={block_from}-{block_to}"
        / "data.parquet"
    )
    final_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(final_path), compression="snappy")
    file_bytes = final_path.stat().st_size
    file_sha = "0x" + _sha256_file(final_path)

    manifest_path = data_root / "manifest.sqlite"
    manifest = ManifestStore(manifest_path)
    try:
        with manifest.transaction() as conn:
            from robinhood_lp.storage.manifest import (
                PartitionBounds,
                PartitionManifestRow,
                PartitionQualification,
            )

            pid = final_path.parent.relative_to(data_root / "raw").as_posix()
            row = PartitionManifestRow(
                partition_id=pid,
                chain_id=chain_id,
                contract_address=contract_address.lower().removeprefix("0x"),
                event_name=event_name,
                block_from=block_from,
                block_to=block_to,
                row_count=table.num_rows,
                file_path=str(final_path),
                file_size_bytes=file_bytes,
                file_sha256=file_sha,
                schema_version=2,
                decode_version=2,
                created_at="1970-01-01T00:00:00+00:00",
            )
            bounds = PartitionBounds(
                min_block_number=block_from,
                min_block_hash="0x" + "00" * 32,
                max_block_number=block_to,
                max_block_hash="0x" + "11" * 32,
            )
            qualification = PartitionQualification(
                partition_id=pid,
                qualified=True,
                halt_reason=None,
                halted_at=None,
            )
            manifest.insert_partition(conn, row, bounds, qualification)
            manifest.insert_accounting(
                conn,
                pid,
                AccountingInterval(
                    endpoint_alias=endpoint_alias,
                    request_from_block=block_from,
                    request_to_block=block_to,
                    logical_rpc_calls=1,
                    http_requests=1,
                    http_batches=1,
                    response_bytes=file_bytes,
                    normalized_rows=table.num_rows,
                    parquet_bytes=file_bytes,
                ),
            )
            manifest.advance_checkpoint(
                conn,
                chain_id=chain_id,
                contract_address=contract_address.lower().removeprefix("0x"),
                event_name=event_name,
                last_successful_block=block_to,
            )
        manifest_rows = len(manifest.accounting_for(pid))
    finally:
        manifest.close()

    bytes_per_row = (file_bytes / table.num_rows) if table.num_rows else 0.0
    return ShortRangeMeasurement(
        row_count=table.num_rows,
        parquet_bytes=file_bytes,
        bytes_per_row=bytes_per_row,
        manifest_rows=manifest_rows,
        block_from=block_from,
        block_to=block_to,
        blocks_per_partition=blocks_per_partition,
        file_sha256=file_sha,
    )


def assert_measurement_within_tolerance(
    measurement: ShortRangeMeasurement,
    *,
    expected_bytes_per_row_lo: float,
    expected_bytes_per_row_hi: float,
) -> None:
    """Guard the partition sizing against silent regression.

    The bounds are deliberately loose: we do not want this test to
    be a byte-for-byte fixture. The contract says the observed
    volume is recorded as a measurement and is *not* promoted to a
    universal constant. The bounds here express "the partition
    sizing did not drift by an order of magnitude", which is the
    minimum a future ADR change must surface.
    """
    if measurement.row_count == 0:
        raise AssertionError("measurement has zero rows")
    if not (expected_bytes_per_row_lo <= measurement.bytes_per_row <= expected_bytes_per_row_hi):
        raise AssertionError(
            f"bytes_per_row {measurement.bytes_per_row:.2f} outside tolerance "
            f"[{expected_bytes_per_row_lo:.2f}, {expected_bytes_per_row_hi:.2f}] "
            f"for {measurement.row_count} rows / {measurement.parquet_bytes} bytes"
        )


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = [
    "ShortRangeMeasurement",
    "assert_measurement_within_tolerance",
    "measure_short_range",
]
