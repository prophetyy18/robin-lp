"""Pinned reference target and 2026-09-17 baseline counts (T036).

The reference target is the chain / PoolKey / PoolId / inclusive range
that the Owner pinned for T036. It is not a planning choice and must
not be widened, swapped, or substituted:

- chain id ``4663`` (Robinhood Chain mainnet);
- PoolManager
  ``0x8366a39cc670b4001a1121b8f6a443a643e40951``, StateView
  ``0xf3334192d15450cdd385c8b70e03f9a6bd9e673b``;
- PoolKey ``currency0``
  ``0x5fc5360d0400a0fd4f2af552add042d716f1d168`` (USDG, 6
  decimals), ``currency1``
  ``0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a`` (ZZZ, 18 decimals),
  ``fee`` 28001, ``tickSpacing`` 280, ``hooks`` ``0x0``;
- PoolId
  ``0x6c614c38c65fea492f4cb2b90fd664f924a7b828c7384620662217e2e2df43ed``;
- inclusive reference range: blocks ``54946237..55946237``.

The 2026-09-17 baseline is a measured comparison target, never
evidence in place of the run's own counts. It records:

- ``BASELINE_TOTAL_EVENTS = 3739`` events across
- ``BASELINE_DISTINCT_BLOCKS = 3266`` distinct blocks;
- ``BASELINE_INITIALIZE_COUNT = 1``;
- ``BASELINE_MODIFY_LIQUIDITY_COUNT = 578``;
- ``BASELINE_SWAP_COUNT = 3159``;
- ``BASELINE_PROTOCOL_FEE_UPDATED_COUNT = 1``;
- ``BASELINE_DONATE_COUNT = 0``.

The secondary endpoint's measured per-call capability on 2026-09-17
(``~10`` blocks per ``eth_getLogs`` call) is the bound the fidelity
sampler honours.

The pool's ``Initialize`` event sits inside the pinned range, so the
cold-start coverage rule is satisfied by this range without a wider
scan; the runtime ``pool_init_block`` for the qualification pipeline
is the lower bound of the range itself, not an externally fetched
block number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from robinhood_lp.protocol import Address, Currency, PoolId, PoolKey

# ---------------------------------------------------------------------------
# Pinned chain identity
# ---------------------------------------------------------------------------

REFERENCE_CHAIN_ID: Final[int] = 4663

# PoolManager — ``0x8366a39cc670b4001a1121b8f6a443a643e40951``
REFERENCE_POOL_MANAGER_ADDRESS_HEX: Final[str] = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
# StateView — ``0xf3334192d15450cdd385c8b70e03f9a6bd9e673b``
REFERENCE_STATE_VIEW_ADDRESS_HEX: Final[str] = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"

# Pool currencies
# currency0 — USDG (6 decimals) — ``0x5fc5360d0400a0fd4f2af552add042d716f1d168``
REFERENCE_CURRENCY0_ADDRESS_HEX: Final[str] = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
# currency1 — ZZZ (18 decimals) — ``0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a``
REFERENCE_CURRENCY1_ADDRESS_HEX: Final[str] = "0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a"
# hooks — zero address (no hooks)
REFERENCE_HOOKS_ADDRESS_HEX: Final[str] = "0x" + "00" * 20

# PoolKey fields
REFERENCE_FEE: Final[int] = 28001
REFERENCE_TICK_SPACING: Final[int] = 280

# Pinned PoolId — ``0x6c614c38c65fea492f4cb2b90fd664f924a7b828c7384620662217e2e2df43ed``
REFERENCE_POOL_ID_HEX: Final[str] = (
    "0x6c614c38c65fea492f4cb2b90fd664f924a7b828c7384620662217e2e2df43ed"
)

# Inclusive reference range
REFERENCE_COVERAGE_FROM_BLOCK: Final[int] = 54_946_237
REFERENCE_COVERAGE_TO_BLOCK: Final[int] = 55_946_237

# The pool's ``Initialize`` event lies inside the pinned range, so
# the cold-start coverage rule is satisfied. The pool_init_block for
# the qualification pipeline is therefore the inclusive lower bound
# of the range itself; the runner's cold-start coverage origin equals
# the range origin.
REFERENCE_POOL_INIT_BLOCK: Final[int] = REFERENCE_COVERAGE_FROM_BLOCK

# ---------------------------------------------------------------------------
# 2026-09-17 baseline counts (measured, immutable history)
# ---------------------------------------------------------------------------

BASELINE_TOTAL_EVENTS: Final[int] = 3739
BASELINE_DISTINCT_BLOCKS: Final[int] = 3266
BASELINE_INITIALIZE_COUNT: Final[int] = 1
BASELINE_MODIFY_LIQUIDITY_COUNT: Final[int] = 578
BASELINE_SWAP_COUNT: Final[int] = 3159
BASELINE_PROTOCOL_FEE_UPDATED_COUNT: Final[int] = 1
BASELINE_DONATE_COUNT: Final[int] = 0

# ---------------------------------------------------------------------------
# Secondary endpoint measured per-call capability (2026-09-17)
# ---------------------------------------------------------------------------

#: The second endpoint accepts about 10 blocks per ``eth_getLogs`` call;
#: the fidelity sampler honours this bound when re-acquiring sampled
#: windows for the cross-endpoint fidelity check.
REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL: Final[int] = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_reference_pool_key() -> PoolKey:
    """Build the pinned ``PoolKey`` from its canonical fields.

    The function constructs the currencies in the V4 ordering
    invariant (``currency0 < currency1`` as ``uint160``). The pinned
    addresses already satisfy the invariant by construction; the
    ``PoolKey`` constructor validates the order.
    """
    currency0 = Currency.from_hex(REFERENCE_CURRENCY0_ADDRESS_HEX)
    currency1 = Currency.from_hex(REFERENCE_CURRENCY1_ADDRESS_HEX)
    hooks = Address.from_hex(REFERENCE_HOOKS_ADDRESS_HEX)
    return PoolKey(
        currency0=currency0,
        currency1=currency1,
        fee=REFERENCE_FEE,
        tick_spacing=REFERENCE_TICK_SPACING,
        hooks=hooks,
    )


def build_reference_pool_id() -> PoolId:
    """Derive the canonical ``PoolId`` from the pinned ``PoolKey``.

    The derivation is the contract-validating function
    ``keccak256(abi.encode(PoolKey))`` whose Solidity reference is
    ``PoolId.sol::toId``. The ``PoolKey.to_pool_id`` method is the
    Python equivalent (T010).
    """
    return build_reference_pool_key().to_pool_id()


def build_reference_state_view_address() -> Address:
    """Return the pinned StateView address."""
    return Address.from_hex(REFERENCE_STATE_VIEW_ADDRESS_HEX)


def build_reference_pool_manager_address() -> Address:
    """Return the pinned PoolManager address."""
    return Address.from_hex(REFERENCE_POOL_MANAGER_ADDRESS_HEX)


# ---------------------------------------------------------------------------
# Reference target value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReferenceTarget:
    """The pinned reference target and its 2026-09-17 baseline.

    The baseline counts are a measured comparison target, never
    evidence in place of the run's own counts. The qualification
    pipeline compares the run's own per-event-type counts and
    distinct event-block count against these values and surfaces
    any difference as a discrepancy rather than reconciling it.
    """

    chain_id: int = REFERENCE_CHAIN_ID
    pool_manager_address: Address = field(default_factory=build_reference_pool_manager_address)
    state_view_address: Address = field(default_factory=build_reference_state_view_address)
    pool_key: PoolKey = field(default_factory=build_reference_pool_key)
    pool_id: PoolId = field(default_factory=build_reference_pool_id)
    coverage_from_block: int = REFERENCE_COVERAGE_FROM_BLOCK
    coverage_to_block: int = REFERENCE_COVERAGE_TO_BLOCK
    pool_init_block: int = REFERENCE_POOL_INIT_BLOCK
    baseline_total_events: int = BASELINE_TOTAL_EVENTS
    baseline_distinct_blocks: int = BASELINE_DISTINCT_BLOCKS
    baseline_initialize_count: int = BASELINE_INITIALIZE_COUNT
    baseline_modify_liquidity_count: int = BASELINE_MODIFY_LIQUIDITY_COUNT
    baseline_swap_count: int = BASELINE_SWAP_COUNT
    baseline_protocol_fee_updated_count: int = BASELINE_PROTOCOL_FEE_UPDATED_COUNT
    baseline_donate_count: int = BASELINE_DONATE_COUNT
    secondary_max_blocks_per_call: int = REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL

    def __post_init__(self) -> None:
        if self.pool_id != self.pool_key.to_pool_id():
            raise ValueError(
                "ReferenceTarget: pinned PoolId does not equal keccak256(abi.encode(PoolKey)); "
                "the frozen reference target must derive the PoolId from the pinned PoolKey"
            )
        if self.coverage_from_block > self.coverage_to_block:
            raise ValueError(
                f"ReferenceTarget: coverage_from_block {self.coverage_from_block} "
                f"> coverage_to_block {self.coverage_to_block}"
            )
        if self.pool_init_block < self.coverage_from_block:
            raise ValueError(
                "ReferenceTarget: pool_init_block must be >= coverage_from_block "
                "(cold-start coverage begins at pool's Initialize block)"
            )
        total_baseline = (
            self.baseline_initialize_count
            + self.baseline_modify_liquidity_count
            + self.baseline_swap_count
            + self.baseline_protocol_fee_updated_count
            + self.baseline_donate_count
        )
        if total_baseline != self.baseline_total_events:
            raise ValueError(
                f"ReferenceTarget: per-event-type baseline sums to {total_baseline} "
                f"!= baseline_total_events {self.baseline_total_events}"
            )

    def per_event_type_baseline(self) -> dict[str, int]:
        """Return the baseline counts keyed by V4 event name."""
        return {
            "Initialize": self.baseline_initialize_count,
            "ModifyLiquidity": self.baseline_modify_liquidity_count,
            "Swap": self.baseline_swap_count,
            "ProtocolFeeUpdated": self.baseline_protocol_fee_updated_count,
            "Donate": self.baseline_donate_count,
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialise the reference target for the evidence pack.

        The dict is the audit-trail row recorded next to the
        qualification report. The ``pin`` field is the locked
        target the operator must reproduce when re-running
        qualification.
        """
        return {
            "chain_id": self.chain_id,
            "pool_manager_address": self.pool_manager_address.to_hex(),
            "state_view_address": self.state_view_address.to_hex(),
            "currency0_address": self.pool_key.currency0.address.to_hex(),
            "currency1_address": self.pool_key.currency1.address.to_hex(),
            "fee": self.pool_key.fee,
            "tick_spacing": self.pool_key.tick_spacing,
            "hooks_address": self.pool_key.hooks.to_hex(),
            "pool_id": self.pool_id.to_hex(),
            "coverage_from_block": self.coverage_from_block,
            "coverage_to_block": self.coverage_to_block,
            "pool_init_block": self.pool_init_block,
            "baseline_total_events": self.baseline_total_events,
            "baseline_distinct_blocks": self.baseline_distinct_blocks,
            "baseline_initialize_count": self.baseline_initialize_count,
            "baseline_modify_liquidity_count": self.baseline_modify_liquidity_count,
            "baseline_swap_count": self.baseline_swap_count,
            "baseline_protocol_fee_updated_count": self.baseline_protocol_fee_updated_count,
            "baseline_donate_count": self.baseline_donate_count,
            "secondary_max_blocks_per_call": self.secondary_max_blocks_per_call,
        }


#: The singleton pinned reference target.
REFERENCE_TARGET: Final[ReferenceTarget] = ReferenceTarget()


__all__ = [
    "BASELINE_DONATE_COUNT",
    "BASELINE_DISTINCT_BLOCKS",
    "BASELINE_INITIALIZE_COUNT",
    "BASELINE_MODIFY_LIQUIDITY_COUNT",
    "BASELINE_PROTOCOL_FEE_UPDATED_COUNT",
    "BASELINE_SWAP_COUNT",
    "BASELINE_TOTAL_EVENTS",
    "REFERENCE_CHAIN_ID",
    "REFERENCE_COVERAGE_FROM_BLOCK",
    "REFERENCE_COVERAGE_TO_BLOCK",
    "REFERENCE_CURRENCY0_ADDRESS_HEX",
    "REFERENCE_CURRENCY1_ADDRESS_HEX",
    "REFERENCE_FEE",
    "REFERENCE_HOOKS_ADDRESS_HEX",
    "REFERENCE_POOL_ID_HEX",
    "REFERENCE_POOL_INIT_BLOCK",
    "REFERENCE_POOL_MANAGER_ADDRESS_HEX",
    "REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL",
    "REFERENCE_STATE_VIEW_ADDRESS_HEX",
    "REFERENCE_TICK_SPACING",
    "REFERENCE_TARGET",
    "ReferenceTarget",
    "build_reference_pool_id",
    "build_reference_pool_key",
    "build_reference_pool_manager_address",
    "build_reference_state_view_address",
]
