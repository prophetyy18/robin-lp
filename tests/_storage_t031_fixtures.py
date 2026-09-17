"""Shared fixtures for the T031 storage layout tests.

This module is not a test module; it exists so the T031 partition /
manifest / writer / reader tests can construct typed log records with
identical structure. It only imports from the public storage layer.
"""

from __future__ import annotations

from robinhood_lp.protocol import Address, ChainId, PoolId
from robinhood_lp.storage.schema import (
    AcquisitionProvenance,
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    ProtocolFeeUpdatedLogRecord,
    SwapLogRecord,
)

CHAIN = ChainId(4663)
CONTRACT = Address.from_hex("0x" + "44" * 20)
POOL_ID = PoolId(0xABCDEF12)


def _acq(endpoint_alias: str, *, request_from: int, request_to: int) -> AcquisitionProvenance:
    return AcquisitionProvenance(
        endpoint_alias=endpoint_alias,
        retrieval_time="2026-09-17T00:00:00+00:00",
        request_from_block=request_from,
        request_to_block=request_to,
        http_batch_size=None,
        http_batch_position=None,
        request_attempt=None,
    )


def make_swap_record(
    *,
    block_number: int,
    log_index: int,
    block_hash: int = 0xAA,
    tx_hash: int = 0xBB,
    transaction_index: int = 0,
    endpoint_alias: str = "robinhood_public",
    amount0: int = -(10**6),
    amount1: int = 20**6,
    sqrt_price_x96: int = 79228162514264337593543950336,
    liquidity: int = 10**18,
    tick: int = 0,
    fee: int = 3000,
) -> SwapLogRecord:
    return SwapLogRecord(
        chain_id=CHAIN,
        pool_id=POOL_ID,
        block_number=block_number,
        block_hash=block_hash,
        transaction_hash=tx_hash,
        transaction_index=transaction_index,
        log_index=log_index,
        address=CONTRACT,
        sender=Address.from_hex("0x" + "11" * 20),
        amount0=amount0,
        amount1=amount1,
        sqrt_price_x96=sqrt_price_x96,
        liquidity=liquidity,
        tick=tick,
        fee=fee,
        acquisition=_acq(
            endpoint_alias,
            request_from=block_number,
            request_to=block_number,
        ),
    )


def make_initialize_record(
    *,
    block_number: int,
    log_index: int,
    block_hash: int = 0xAA,
    tx_hash: int = 0xBB,
    endpoint_alias: str = "robinhood_public",
) -> InitializeLogRecord:
    return InitializeLogRecord(
        chain_id=CHAIN,
        pool_id=POOL_ID,
        block_number=block_number,
        block_hash=block_hash,
        transaction_hash=tx_hash,
        transaction_index=0,
        log_index=log_index,
        address=CONTRACT,
        acquisition=_acq(
            endpoint_alias,
            request_from=block_number,
            request_to=block_number,
        ),
    )


def make_modify_record(
    *,
    block_number: int,
    log_index: int,
    block_hash: int = 0xAA,
    tx_hash: int = 0xBB,
    endpoint_alias: str = "robinhood_public",
    tick_lower: int = -100,
    tick_upper: int = 100,
    liquidity_delta: int = 10**18,
) -> ModifyLiquidityLogRecord:
    return ModifyLiquidityLogRecord(
        chain_id=CHAIN,
        pool_id=POOL_ID,
        block_number=block_number,
        block_hash=block_hash,
        transaction_hash=tx_hash,
        transaction_index=1,
        log_index=log_index,
        address=CONTRACT,
        sender=Address.from_hex("0x" + "11" * 20),
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity_delta=liquidity_delta,
        salt=0xDEADBEEF,
        acquisition=_acq(
            endpoint_alias,
            request_from=block_number,
            request_to=block_number,
        ),
    )


def make_donate_record(
    *,
    block_number: int,
    log_index: int,
    block_hash: int = 0xAA,
    tx_hash: int = 0xBB,
    endpoint_alias: str = "robinhood_public",
) -> DonateLogRecord:
    return DonateLogRecord(
        chain_id=CHAIN,
        pool_id=POOL_ID,
        block_number=block_number,
        block_hash=block_hash,
        transaction_hash=tx_hash,
        transaction_index=2,
        log_index=log_index,
        address=CONTRACT,
        sender=Address.from_hex("0x" + "11" * 20),
        amount0=-(10**6),
        amount1=2 * 10**6,
        acquisition=_acq(
            endpoint_alias,
            request_from=block_number,
            request_to=block_number,
        ),
    )


def make_protocol_fee_updated_record(
    *,
    block_number: int,
    log_index: int,
    block_hash: int = 0xAA,
    tx_hash: int = 0xBB,
    endpoint_alias: str = "robinhood_public",
    protocol_fee: int = 0x0ABCDE,
) -> ProtocolFeeUpdatedLogRecord:
    return ProtocolFeeUpdatedLogRecord(
        chain_id=CHAIN,
        pool_id=POOL_ID,
        block_number=block_number,
        block_hash=block_hash,
        transaction_hash=tx_hash,
        transaction_index=3,
        log_index=log_index,
        address=CONTRACT,
        protocol_fee=protocol_fee,
        acquisition=_acq(
            endpoint_alias,
            request_from=block_number,
            request_to=block_number,
        ),
    )


__all__ = [
    "CHAIN",
    "CONTRACT",
    "POOL_ID",
    "make_donate_record",
    "make_initialize_record",
    "make_modify_record",
    "make_protocol_fee_updated_record",
    "make_swap_record",
]
