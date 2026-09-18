"""Shared fixtures for T040 replay tests.

This module is not a test module; it exists so the T040 replay
tests can construct typed log records with identical structure.
It only imports from the public protocol / storage layers.
"""

from __future__ import annotations

from typing import Final

from robinhood_lp.protocol import Address, ChainId, Currency, PoolId, PoolKey
from robinhood_lp.storage.schema import (
    AcquisitionProvenance,
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    ProtocolFeeUpdatedLogRecord,
    SwapLogRecord,
)

CHAIN: Final[ChainId] = ChainId(4663)
POOL_MANAGER: Final[Address] = Address.from_hex("0x" + "44" * 20)
SENDER: Final[Address] = Address.from_hex("0x" + "11" * 20)
POOL_ID: Final[PoolId] = PoolId(
    int.from_bytes(
        bytes.fromhex("6c614c38c65fea492f4cb2b90fd664f924a7b828c7384620662217e2e2df43ed"),
        "big",
    )
)
ZERO_POOL_ID: Final[PoolId] = PoolId(0xDEADBEEF)
ZERO_ADDRESS: Final[Address] = Address.zero()

# Static-fee pool key (3000 fee, 60 tick spacing, no hooks).
STATIC_POOL_KEY: Final[PoolKey] = PoolKey(
    currency0=Currency.from_hex("0x" + "11" * 20),
    currency1=Currency.from_hex("0x" + "22" * 20),
    fee=3000,
    tick_spacing=60,
    hooks=Address.zero(),
)

# Dynamic-fee pool key. The fee is the DYNAMIC_FEE_FLAG sentinel
# (bit 23 set); individual swap fees are taken from each Swap event.
DYNAMIC_POOL_KEY: Final[PoolKey] = PoolKey(
    currency0=Currency.from_hex("0x" + "11" * 20),
    currency1=Currency.from_hex("0x" + "22" * 20),
    fee=0x800000,  # DYNAMIC_FEE_FLAG
    tick_spacing=60,
    hooks=Address.zero(),
)


def _acq(*, request_from: int, request_to: int) -> AcquisitionProvenance:
    return AcquisitionProvenance(
        endpoint_alias="robinhood_public",
        retrieval_time="2026-09-17T00:00:00+00:00",
        request_from_block=request_from,
        request_to_block=request_to,
        http_batch_size=None,
        http_batch_position=None,
        request_attempt=None,
    )


def _hash_for_block(seed: int) -> int:
    """Return a deterministic 32-byte hash value derived from seed.

    The seed is expanded across the 256-bit width so successive
    seeds are obviously distinct.
    """
    return (seed * 0x0101010101010101_0101010101010101_0101010101010101_0101010101010101) & (
        (1 << 256) - 1
    )


def _tx_hash(seed: int) -> int:
    return (seed * 0xDEADBEEFCAFEBABE_DEADBEEFCAFEBABE_DEADBEEFCAFEBABE_DEADBEEFCAFEBABE) & (
        (1 << 256) - 1
    )


def make_initialize(
    *,
    block_number: int,
    log_index: int = 0,
    transaction_index: int = 0,
    pool_id: PoolId = POOL_ID,
    tx_seed: int = 0,
    block_seed: int | None = None,
) -> InitializeLogRecord:
    """Build an Initialize log record.

    V4's ``Initialize`` event only emits the PoolId; the initial
    ``sqrtPriceX96`` and ``tick`` are returned by the
    ``initialize()`` call but not on chain. Replays consume the
    initial price/tick from a StateView-style block-pinned read
    supplied on the :class:`ReplayInput`, so the test fixture
    does not synthesise them.
    """
    return InitializeLogRecord(
        chain_id=CHAIN,
        pool_id=pool_id,
        block_number=block_number,
        block_hash=_hash_for_block(block_seed if block_seed is not None else block_number),
        transaction_hash=_tx_hash(tx_seed),
        transaction_index=transaction_index,
        log_index=log_index,
        address=POOL_MANAGER,
        block_timestamp=0,
        parent_hash=0,
        removed=False,
        acquisition=_acq(request_from=block_number, request_to=block_number),
    )


def make_modify_liquidity(
    *,
    block_number: int,
    log_index: int,
    transaction_index: int = 0,
    tick_lower: int = -60,
    tick_upper: int = 60,
    liquidity_delta: int = 1_000_000,
    salt: int = 0,
    pool_id: PoolId = POOL_ID,
    tx_seed: int = 1,
    block_seed: int | None = None,
) -> ModifyLiquidityLogRecord:
    return ModifyLiquidityLogRecord(
        chain_id=CHAIN,
        pool_id=pool_id,
        block_number=block_number,
        block_hash=_hash_for_block(block_seed if block_seed is not None else block_number),
        transaction_hash=_tx_hash(tx_seed),
        transaction_index=transaction_index,
        log_index=log_index,
        address=POOL_MANAGER,
        sender=SENDER,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity_delta=liquidity_delta,
        salt=salt,
        block_timestamp=0,
        parent_hash=0,
        removed=False,
        acquisition=_acq(request_from=block_number, request_to=block_number),
    )


def make_swap(
    *,
    block_number: int,
    log_index: int,
    transaction_index: int = 0,
    amount0: int = -1_000_000,
    amount1: int = 2_000_000,
    sqrt_price_x96: int = 79228162514264337593543950336,
    liquidity: int = 1_000_000,
    tick: int = 0,
    fee: int = 3000,
    pool_id: PoolId = POOL_ID,
    tx_seed: int = 2,
    block_seed: int | None = None,
) -> SwapLogRecord:
    return SwapLogRecord(
        chain_id=CHAIN,
        pool_id=pool_id,
        block_number=block_number,
        block_hash=_hash_for_block(block_seed if block_seed is not None else block_number),
        transaction_hash=_tx_hash(tx_seed),
        transaction_index=transaction_index,
        log_index=log_index,
        address=POOL_MANAGER,
        sender=SENDER,
        amount0=amount0,
        amount1=amount1,
        sqrt_price_x96=sqrt_price_x96,
        liquidity=liquidity,
        tick=tick,
        fee=fee,
        block_timestamp=0,
        parent_hash=0,
        removed=False,
        acquisition=_acq(request_from=block_number, request_to=block_number),
    )


def make_donate(
    *,
    block_number: int,
    log_index: int,
    transaction_index: int = 0,
    amount0: int = 100,
    amount1: int = 200,
    pool_id: PoolId = POOL_ID,
    tx_seed: int = 3,
    block_seed: int | None = None,
) -> DonateLogRecord:
    return DonateLogRecord(
        chain_id=CHAIN,
        pool_id=pool_id,
        block_number=block_number,
        block_hash=_hash_for_block(block_seed if block_seed is not None else block_number),
        transaction_hash=_tx_hash(tx_seed),
        transaction_index=transaction_index,
        log_index=log_index,
        address=POOL_MANAGER,
        sender=SENDER,
        amount0=amount0,
        amount1=amount1,
        block_timestamp=0,
        parent_hash=0,
        removed=False,
        acquisition=_acq(request_from=block_number, request_to=block_number),
    )


def make_protocol_fee_updated(
    *,
    block_number: int,
    log_index: int,
    transaction_index: int = 0,
    protocol_fee: int = 0x000000,
    pool_id: PoolId = POOL_ID,
    tx_seed: int = 4,
    block_seed: int | None = None,
) -> ProtocolFeeUpdatedLogRecord:
    return ProtocolFeeUpdatedLogRecord(
        chain_id=CHAIN,
        pool_id=pool_id,
        block_number=block_number,
        block_hash=_hash_for_block(block_seed if block_seed is not None else block_number),
        transaction_hash=_tx_hash(tx_seed),
        transaction_index=transaction_index,
        log_index=log_index,
        address=POOL_MANAGER,
        protocol_fee=protocol_fee,
        block_timestamp=0,
        parent_hash=0,
        removed=False,
        acquisition=_acq(request_from=block_number, request_to=block_number),
    )


__all__ = [
    "CHAIN",
    "DYNAMIC_POOL_KEY",
    "POOL_ID",
    "POOL_MANAGER",
    "SENDER",
    "STATIC_POOL_KEY",
    "ZERO_ADDRESS",
    "ZERO_POOL_ID",
    "make_donate",
    "make_initialize",
    "make_modify_liquidity",
    "make_protocol_fee_updated",
    "make_swap",
]
