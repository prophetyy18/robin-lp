"""Tests for the representative short-range measurement (T031).

T031 acceptance requires at least one representative short-range
measurement justifying the chosen partition sizing and retention
footprint. The numbers must be reproducible and must NOT be promoted
to a universal constant. These tests verify the measurement helpers
plus a regression guard for partition sizing.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from _storage_t031_fixtures import (
    CHAIN,
    CONTRACT,
    make_swap_record,
)
from robinhood_lp.storage.manifest import ManifestStore
from robinhood_lp.storage.measurement import (
    ShortRangeMeasurement,
    assert_measurement_within_tolerance,
    measure_short_range,
)
from robinhood_lp.storage.partition import (
    parquet_schema_for,
)
from robinhood_lp.storage.writer import (
    DEFAULT_BLOCKS_PER_PARTITION,
    RawPartitionWriter,
)

# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def _build_swap_table(rows: int, event_name: str = "Swap") -> pa.Table:
    """Build a synthetic Swap Parquet table for measurement."""
    schema = parquet_schema_for(event_name)
    columns: dict[str, list[object]] = {f.name: [] for f in schema}
    for i in range(rows):
        columns["raw_topic_0"].append(b"\x00" * 32)
        columns["raw_topic_1"].append(b"\x00" * 32)
        columns["raw_topic_2"].append(b"\x00" * 32)
        columns["raw_topic_3"].append(b"\x00" * 32)
        columns["raw_data"].append(b"")
        columns["raw_response_json"].append("{}")
        columns["chain_id"].append(4663)
        columns["block_number"].append(10 + i)
        columns["block_hash"].append(b"\x00" * 32)
        # T035 / ADR-012: every persisted event row carries the integer
        # block timestamp and the parent hash. The measurement helper
        # populates both with the per-row offset the synthetic table
        # uses for ``block_number`` so the helper continues to round-
        # trip the production ``parquet_schema_for`` shape.
        columns["block_timestamp"].append(1_700_000_000 + i)
        columns["parent_hash"].append(b"\x00" * 32)
        columns["transaction_hash"].append(b"\x00" * 32)
        columns["transaction_index"].append(0)
        columns["log_index"].append(i)
        columns["address"].append(b"\x44" * 20)
        columns["pool_id"].append(b"\x00" * 32)
        columns["removed"].append(False)
        columns["event_name"].append(event_name)
        columns["schema_version"].append(2)
        columns["decode_version"].append(2)
        columns["acquisition_endpoint_alias"].append("robinhood_public")
        columns["acquisition_retrieval_time"].append("2026-09-17T00:00:00+00:00")
        columns["acquisition_request_from_block"].append(10 + i)
        columns["acquisition_request_to_block"].append(10 + i)
        columns["acquisition_http_batch_size"].append(None)
        columns["acquisition_http_batch_position"].append(None)
        columns["acquisition_request_attempt"].append(None)
        columns["event_key_hash"].append(b"\x00" * 32)
        columns["content_hash"].append(b"\x00" * 32)
        # Per-event typed columns (Swap).
        columns["sender"].append(b"\x11" * 20)
        columns["amount0"].append(int(-(10**6)).to_bytes(32, "big", signed=True))
        columns["amount1"].append(int(2 * 10**6).to_bytes(32, "big", signed=True))
        columns["sqrt_price_x96"].append((2**96).to_bytes(20, "big"))
        columns["liquidity"].append((10**18).to_bytes(16, "big"))
        columns["tick"].append(0)
        columns["fee"].append(3000)
    return pa.table(columns, schema=schema)


def test_measure_short_range_returns_numbers(tmp_path: Path) -> None:
    table = _build_swap_table(rows=10)
    m = measure_short_range(
        data_root=tmp_path,
        table=table,
        chain_id=4663,
        contract_address="0x" + "44" * 20,
        event_name="Swap",
        block_from=10,
        block_to=19,
        blocks_per_partition=DEFAULT_BLOCKS_PER_PARTITION,
    )
    assert isinstance(m, ShortRangeMeasurement)
    assert m.row_count == 10
    assert m.parquet_bytes > 0
    assert m.bytes_per_row > 0
    assert m.manifest_rows >= 1
    assert m.block_from == 10
    assert m.block_to == 19


def test_assert_measurement_within_tolerance_passes_for_reasonable_values(tmp_path: Path) -> None:
    m = ShortRangeMeasurement(
        row_count=10,
        parquet_bytes=5000,
        bytes_per_row=500.0,
        manifest_rows=1,
        block_from=10,
        block_to=19,
        blocks_per_partition=DEFAULT_BLOCKS_PER_PARTITION,
        file_sha256="0x" + "ab" * 32,
    )
    assert_measurement_within_tolerance(
        m,
        expected_bytes_per_row_lo=10.0,
        expected_bytes_per_row_hi=10_000.0,
    )


def test_assert_measurement_within_tolerance_fails_outside_range() -> None:
    m = ShortRangeMeasurement(
        row_count=10,
        parquet_bytes=5000,
        bytes_per_row=500_000_000.0,  # way out of range
        manifest_rows=1,
        block_from=10,
        block_to=19,
        blocks_per_partition=DEFAULT_BLOCKS_PER_PARTITION,
        file_sha256="0x" + "ab" * 32,
    )
    with pytest.raises(AssertionError, match="outside tolerance"):
        assert_measurement_within_tolerance(
            m,
            expected_bytes_per_row_lo=10.0,
            expected_bytes_per_row_hi=10_000.0,
        )


def test_assert_measurement_within_tolerance_rejects_zero_rows() -> None:
    m = ShortRangeMeasurement(
        row_count=0,
        parquet_bytes=0,
        bytes_per_row=0.0,
        manifest_rows=0,
        block_from=10,
        block_to=19,
        blocks_per_partition=DEFAULT_BLOCKS_PER_PARTITION,
        file_sha256="0x" + "ab" * 32,
    )
    with pytest.raises(AssertionError, match="zero rows"):
        assert_measurement_within_tolerance(
            m,
            expected_bytes_per_row_lo=10.0,
            expected_bytes_per_row_hi=10_000.0,
        )


def test_measure_short_range_integration_with_writer(tmp_path: Path) -> None:
    """End-to-end: writer -> manifest -> measurement is consistent."""
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    records = [
        make_swap_record(block_number=10 + i, log_index=i, transaction_index=0, tx_hash=0xB0 + i)
        for i in range(5)
    ]
    result = writer.append_partition(
        records,
        chain_id=CHAIN,
        contract_address=CONTRACT,
    )
    # The bytes written by the writer match the manifest's recorded size.
    assert result.file_size_bytes == result.file_path.stat().st_size
    # The manifest row's SHA-256 matches the file.
    import hashlib

    h = hashlib.sha256()
    with result.file_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    assert "0x" + h.hexdigest() == result.file_sha256
    manifest.close()


def test_partition_sizing_measurement_documented(tmp_path: Path) -> None:
    """The default partition sizing produces a measurement within the
    tolerance band recorded at test time. This is the regression
    guard for the T031 acceptance clause.
    """
    table = _build_swap_table(rows=20)
    m = measure_short_range(
        data_root=tmp_path,
        table=table,
        chain_id=4663,
        contract_address="0x" + "44" * 20,
        event_name="Swap",
        block_from=10,
        block_to=29,
        blocks_per_partition=DEFAULT_BLOCKS_PER_PARTITION,
    )
    # 100 blocks per partition with ~20 rows worth of Swap data
    # fits comfortably under the per-row budget. The tolerance band
    # is deliberately loose: a future schema bump should land well
    # inside it.
    assert_measurement_within_tolerance(
        m,
        expected_bytes_per_row_lo=10.0,
        expected_bytes_per_row_hi=10_000.0,
    )


def test_default_blocks_per_partition_is_documented() -> None:
    """The default partition sizing is exposed for downstream callers
    (e.g. backfill drivers) to consult without re-deriving it.
    """
    assert DEFAULT_BLOCKS_PER_PARTITION > 0
    # The default is not a universal constant. A future ADR can move
    # it; the test only asserts it is a positive integer and matches
    # the documented representative measurement fixture.
    assert DEFAULT_BLOCKS_PER_PARTITION == 100
