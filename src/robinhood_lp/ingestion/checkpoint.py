"""Durable checkpoint state for capability-driven ingestion (T032).

The checkpoint tracks, per pool:

- the qualified block range (start, end);
- the block hash pinned to the qualified end;
- the schema/decode version that produced the prefix;
- the capability snapshot id and manifest checksum the checkpoint
  was written under;
- the topology the run used (cold_start or warm_incremental);
- the pool's registered ``Initialize`` block (the reconstruction
  origin).

The checkpoint only advances after a durable commit of the
corresponding interval and a recorded coverage transition from
uncovered to covered. A warm run whose existing checkpoint does not
match the requested pool / range / schema / decode-version /
manifest / coverage prefix is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from robinhood_lp.ingestion.errors import (
    TOPOLOGY_COLD_START,
    TOPOLOGY_WARM_INCREMENTAL,
    VALID_TOPOLOGIES,
    CheckpointMismatchError,
)
from robinhood_lp.ingestion.planner import ExistingCheckpoint
from robinhood_lp.protocol.ids import Address, PoolId
from robinhood_lp.storage.manifest import ManifestStore
from robinhood_lp.storage.schema import CURRENT_DECODE_VERSION, CURRENT_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class DurableCheckpointState:
    """In-memory shape of one persisted durable checkpoint.

    Mirrors the ``durable_checkpoints`` table row.
    """

    chain_id: int
    contract_address: Address
    pool_id: PoolId
    qualified_start_block: int
    qualified_end_block: int
    qualified_end_block_hash: str
    schema_version: int
    decode_version: int
    capability_snapshot_id: str
    manifest_checksum: str
    topology: str
    pool_init_block: int

    def __post_init__(self) -> None:
        if self.topology not in VALID_TOPOLOGIES:
            raise ValueError(
                f"DurableCheckpointState.topology: must be one of "
                f"{sorted(VALID_TOPOLOGIES)!r}, got {self.topology!r}"
            )
        if self.qualified_start_block < 0:
            raise ValueError(
                f"qualified_start_block: must be >= 0, got {self.qualified_start_block}"
            )
        if self.qualified_end_block < self.qualified_start_block:
            raise ValueError(
                f"qualified_end_block {self.qualified_end_block} < "
                f"qualified_start_block {self.qualified_start_block}"
            )

    def to_existing_checkpoint(self) -> ExistingCheckpoint:
        """Return the subset the range planner validates."""
        return ExistingCheckpoint(
            chain_id=self.chain_id,
            contract_address=self.contract_address.to_hex().lower().removeprefix("0x"),
            pool_id=self.pool_id.to_hex(),
            qualified_start_block=self.qualified_start_block,
            qualified_end_block=self.qualified_end_block,
            qualified_end_block_hash=self.qualified_end_block_hash,
            schema_version=self.schema_version,
            decode_version=self.decode_version,
            capability_snapshot_id=self.capability_snapshot_id,
            manifest_checksum=self.manifest_checksum,
            topology=self.topology,
            pool_init_block=self.pool_init_block,
        )


def load_durable_checkpoint(
    manifest: ManifestStore,
    *,
    chain_id: int,
    contract_address: Address,
    pool_id: PoolId,
) -> DurableCheckpointState | None:
    """Load the durable checkpoint for the pool, if one exists."""
    row = manifest.get_durable_checkpoint(
        chain_id=chain_id,
        contract_address=contract_address.to_hex().removeprefix("0x").lower(),
        pool_id=pool_id.to_hex(),
    )
    if row is None:
        return None
    return DurableCheckpointState(
        chain_id=int(row["chain_id"]),
        contract_address=Address.from_hex(
            "0x" + str(row["contract_address"]).removeprefix("0x").lower()
        ),
        pool_id=PoolId.from_hex(
            str(row["pool_id"])
            if str(row["pool_id"]).startswith("0x")
            else "0x" + str(row["pool_id"])
        ),
        qualified_start_block=int(row["qualified_start_block"]),
        qualified_end_block=int(row["qualified_end_block"]),
        qualified_end_block_hash=str(row["qualified_end_block_hash"]),
        schema_version=int(row["schema_version"]),
        decode_version=int(row["decode_version"]),
        capability_snapshot_id=str(row["capability_snapshot_id"]),
        manifest_checksum=str(row["manifest_checksum"]),
        topology=str(row["topology"]),
        pool_init_block=int(row["pool_init_block"]),
    )


def upsert_durable_checkpoint(
    manifest: ManifestStore,
    state: DurableCheckpointState,
) -> None:
    """Advance the durable checkpoint (monotonically, per the store).

    The store's ``MAX`` semantics ensure a re-run that re-fetches an
    already-checkpointed range does not move the checkpoint backwards.
    """
    if state.schema_version != CURRENT_SCHEMA_VERSION:
        raise CheckpointMismatchError(
            f"checkpoint schema_version {state.schema_version} != current {CURRENT_SCHEMA_VERSION}"
        )
    if state.decode_version != CURRENT_DECODE_VERSION:
        raise CheckpointMismatchError(
            f"checkpoint decode_version {state.decode_version} != current {CURRENT_DECODE_VERSION}"
        )
    manifest.upsert_durable_checkpoint(
        chain_id=state.chain_id,
        contract_address=state.contract_address.to_hex().removeprefix("0x").lower(),
        pool_id=state.pool_id.to_hex(),
        qualified_start_block=state.qualified_start_block,
        qualified_end_block=state.qualified_end_block,
        qualified_end_block_hash=state.qualified_end_block_hash,
        schema_version=state.schema_version,
        decode_version=state.decode_version,
        capability_snapshot_id=state.capability_snapshot_id,
        manifest_checksum=state.manifest_checksum,
        topology=state.topology,
        pool_init_block=state.pool_init_block,
    )


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

EMPTY_CHECKPOINT_HASH: Final[str] = (
    "0x0000000000000000000000000000000000000000000000000000000000000000"
)

__all__ = [
    "DurableCheckpointState",
    "EMPTY_CHECKPOINT_HASH",
    "TOPOLOGY_COLD_START",
    "TOPOLOGY_WARM_INCREMENTAL",
    "load_durable_checkpoint",
    "upsert_durable_checkpoint",
]
