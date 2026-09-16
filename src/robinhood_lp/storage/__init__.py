"""Storage layer (T030-T034)."""

from __future__ import annotations

from robinhood_lp.storage.decode_log import LogDecodeContext, decode_log
from robinhood_lp.storage.schema import (
    CURRENT_DECODE_VERSION,
    CURRENT_SCHEMA_VERSION,
    AcquisitionProvenance,
    BlockContext,
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    ProtocolFeeUpdatedLogRecord,
    ReceiptContext,
    SwapLogRecord,
    TransactionContext,
    canonical_bytes,
    from_canonical_bytes,
    migrate_to_current,
    normalized_content_hash,
)

__all__ = [
    "AcquisitionProvenance",
    "BlockContext",
    "CURRENT_DECODE_VERSION",
    "CURRENT_SCHEMA_VERSION",
    "DonateLogRecord",
    "InitializeLogRecord",
    "LogDecodeContext",
    "ModifyLiquidityLogRecord",
    "ProtocolFeeUpdatedLogRecord",
    "ReceiptContext",
    "SwapLogRecord",
    "TransactionContext",
    "canonical_bytes",
    "decode_log",
    "from_canonical_bytes",
    "migrate_to_current",
    "normalized_content_hash",
]
