"""Tests for the T031 partition key + Parquet schema layout.

The partition key + Parquet schema is the physical layout half of
T031. The tests cover:

- partition key derivation from a record batch;
- the partition directory layout (``chain=.../contract=.../event=
  .../range=.../data.parquet``);
- the Parquet schema per event (shared columns plus the per-event
  typed columns);
- input validation (block range, event name, chain id).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _storage_t031_fixtures import (
    CHAIN,
    CONTRACT,
)
from robinhood_lp.storage.partition import (
    EVENT_NAMES,
    SHARED_PARQUET_FIELDS,
    PartitionKey,
    parquet_schema_for,
)

# ---------------------------------------------------------------------------
# PartitionKey construction
# ---------------------------------------------------------------------------


def test_partition_key_constructs_with_valid_inputs() -> None:
    pk = PartitionKey(
        chain_id=CHAIN,
        contract_address=CONTRACT,
        event_name="Swap",
        block_from=0,
        block_to=99,
    )
    assert pk.event_name == "Swap"
    assert pk.block_from == 0
    assert pk.block_to == 99


def test_partition_key_rejects_unknown_event_name() -> None:
    with pytest.raises(ValueError, match="event_name"):
        PartitionKey(
            chain_id=CHAIN,
            contract_address=CONTRACT,
            event_name="NotARealEvent",
            block_from=0,
            block_to=10,
        )


def test_partition_key_rejects_inverted_block_range() -> None:
    with pytest.raises(ValueError, match="block range"):
        PartitionKey(
            chain_id=CHAIN,
            contract_address=CONTRACT,
            event_name="Swap",
            block_from=100,
            block_to=50,
        )


def test_partition_key_rejects_negative_block() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        PartitionKey(
            chain_id=CHAIN,
            contract_address=CONTRACT,
            event_name="Swap",
            block_from=-1,
            block_to=10,
        )


def test_partition_key_rejects_non_int_block() -> None:
    with pytest.raises(TypeError, match="block_from"):
        PartitionKey(
            chain_id=CHAIN,
            contract_address=CONTRACT,
            event_name="Swap",
            block_from="0",  # type: ignore[arg-type]
            block_to=10,
        )


def test_partition_key_rejects_non_chain_id() -> None:
    with pytest.raises(TypeError, match="chain_id"):
        PartitionKey(
            chain_id=4663,  # type: ignore[arg-type]
            contract_address=CONTRACT,
            event_name="Swap",
            block_from=0,
            block_to=10,
        )


# ---------------------------------------------------------------------------
# Path / id derivation
# ---------------------------------------------------------------------------


def test_partition_id_is_deterministic() -> None:
    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    again = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    assert pk.partition_id() == again.partition_id()


def test_partition_id_format_uses_chain_contract_event_range() -> None:
    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 100, 199)
    expected = (
        f"chain=4663/contract={CONTRACT.to_hex().removeprefix('0x').lower()}/"
        f"event=Swap/range=100-199"
    )
    assert pk.partition_id() == expected


def test_partition_dir_under_data_root(tmp_path: Path) -> None:
    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    pdir = pk.partition_dir(tmp_path)
    assert pdir == tmp_path / "raw" / pk.partition_id()


def test_data_file_path_is_partition_dir_slash_data_dot_parquet(tmp_path: Path) -> None:
    pk = PartitionKey(CHAIN, CONTRACT, "Swap", 0, 99)
    expected = tmp_path / "raw" / pk.partition_id() / "data.parquet"
    assert pk.data_file_path(tmp_path) == expected


# ---------------------------------------------------------------------------
# Parquet schema per event
# ---------------------------------------------------------------------------


def test_parquet_schema_for_every_event_has_shared_columns() -> None:
    for ev in EVENT_NAMES:
        schema = parquet_schema_for(ev)
        names = {f.name for f in schema}
        for shared in SHARED_PARQUET_FIELDS:
            assert shared.name in names, f"event {ev!r} missing shared column {shared.name!r}"


def test_parquet_schema_for_swap_has_per_event_columns() -> None:
    schema = parquet_schema_for("Swap")
    names = {f.name for f in schema}
    for col in ("amount0", "amount1", "sqrt_price_x96", "liquidity", "tick", "fee", "sender"):
        assert col in names, f"Swap missing typed column {col!r}"


def test_parquet_schema_for_initialize_has_per_event_columns() -> None:
    schema = parquet_schema_for("Initialize")
    names = {f.name for f in schema}
    for col in ("currency0", "currency1", "fee", "tick_spacing", "hooks", "sqrt_price_x96", "tick"):
        assert col in names, f"Initialize missing typed column {col!r}"


def test_parquet_schema_for_modify_liquidity_has_per_event_columns() -> None:
    schema = parquet_schema_for("ModifyLiquidity")
    names = {f.name for f in schema}
    for col in ("sender", "tick_lower", "tick_upper", "liquidity_delta", "salt"):
        assert col in names, f"ModifyLiquidity missing typed column {col!r}"


def test_parquet_schema_for_donate_has_per_event_columns() -> None:
    schema = parquet_schema_for("Donate")
    names = {f.name for f in schema}
    for col in ("sender", "amount0", "amount1"):
        assert col in names, f"Donate missing typed column {col!r}"


def test_parquet_schema_for_protocol_fee_updated_has_per_event_columns() -> None:
    schema = parquet_schema_for("ProtocolFeeUpdated")
    names = {f.name for f in schema}
    assert "protocol_fee" in names, "ProtocolFeeUpdated missing typed column 'protocol_fee'"


def test_parquet_schema_unknown_event_rejected() -> None:
    with pytest.raises(ValueError, match="event_name"):
        parquet_schema_for("NoSuchEvent")
