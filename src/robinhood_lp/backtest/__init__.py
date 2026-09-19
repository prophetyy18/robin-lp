"""Event-driven backtest engine public surface (T061).

The ``robinhood_lp.backtest`` package is the event-driven backtest
engine per ``docs/spec/architecture/ARCHITECTURE.md`` §2.1: it is an
*append-only* event-time replay that emits one immutable audit event
per pipeline stage. The engine depends only on the standard library,
the ``robinhood_lp.backtest.events`` primitives, and the
``robinhood_lp.backtest.models`` parameter bundle. It does not import
RPC, storage, configuration, signing, or presentation code; the
strategy / risk callbacks are injected by the orchestrator.

Three rules bind the engine (per the T061 contract):

1. **Ordered event clock.** Input events are sorted by the tuple
   ``(timestamp, sequence, source_priority)``. ``sequence`` is assigned
   by the engine at ingest time as a per-source monotonic counter so
   two equivalent manifests always produce the same ordering.

2. **Information frontier.** At each decision point ``T`` the engine
   only consumes events whose ``observed_at <= T`` AND
   ``available_at <= T``. A future-data event is recorded as a
   :attr:`STATUS_FUTURE_DATA_VIOLATION` audit event and never used.

3. **Append-only audit chain.** Every pipeline stage emits exactly
   one :class:`AuditEvent`; the chain is never reordered, mutated, or
   trimmed.

References:

- R17 — NautilusTrader event-time architecture.
- R18 — QuantConnect live reconciliation and look-ahead warnings.
"""

from __future__ import annotations

from robinhood_lp.backtest.engine import (
    BACKTEST_ENGINE_VERSION,
    DEFAULT_FILL_DATA_KINDS,
    DEFAULT_REACT_TO_KINDS,
    ENGINE_ROOT_EVENT_ID,
    ZERO_LEDGER_HASH,
    BacktestEngine,
    BacktestResult,
    RiskCallback,
    RiskDecision,
    StrategyCallback,
    StrategyDecision,
    StrategyDecisionRequest,
    empty_position_state,
)
from robinhood_lp.backtest.events import (
    AUDIT_EVENT_VERSION,
    BACKTEST_EVENT_VERSION,
    KIND_BURN,
    KIND_MINT,
    KIND_OBSERVATION,
    KIND_SHUTDOWN,
    KIND_SWAP,
    KIND_TICK,
    LEDGER_VERSION,
    SOURCE_PRIORITY_DATA,
    SOURCE_PRIORITY_EXECUTION,
    SOURCE_PRIORITY_RISK,
    SOURCE_PRIORITY_STRATEGY,
    SOURCE_PRIORITY_SYSTEM,
    STAGE_DECISION,
    STAGE_FILL,
    STAGE_LATENCY,
    STAGE_RISK,
    STAGE_SYSTEM,
    STATUS_DECISION_RECORDED,
    STATUS_FILL_DELAYED,
    STATUS_FILL_FILLED,
    STATUS_FILL_PARTIAL,
    STATUS_FILL_REJECTED,
    STATUS_FUTURE_DATA_VIOLATION,
    STATUS_LATENCY_RECORDED,
    STATUS_RISK_APPROVED,
    STATUS_RISK_REJECTED,
    STATUS_SYSTEM_INIT,
    STATUS_SYSTEM_SHUTDOWN,
    AuditEvent,
    BacktestEvent,
    BacktestEventError,
    FutureDataViolation,
    InvalidPayloadError,
    InvalidSourcePriorityError,
    InvalidStageError,
    PositionState,
    assert_backtest_events_layer_is_pure,
)
from robinhood_lp.backtest.models import (
    FAILURE_MODEL_VERSION,
    FEE_DENOMINATOR_PIPS,
    FEE_MODEL_VERSION,
    GAS_MODEL_VERSION,
    LIQUIDITY_MODEL_VERSION,
    SLIPPAGE_DENOMINATOR_BPS,
    SLIPPAGE_MODEL_VERSION,
    ConstantLiquidityModel,
    ConstantSlippageModel,
    DeterministicFailureModel,
    FailureModel,
    FeeModel,
    FillOutcome,
    FlatGasModel,
    GasModel,
    InvalidFillOutcomeError,
    InvalidModelVersionError,
    InvalidUnitError,
    LiquidityModel,
    ModelBundle,
    ModelError,
    SlippageModel,
    StaticFeeModel,
    ZeroSlippageModel,
    assert_backtest_models_layer_is_pure,
)

__all__ = [
    "AUDIT_EVENT_VERSION",
    "AuditEvent",
    "BACKTEST_ENGINE_VERSION",
    "BACKTEST_EVENT_VERSION",
    "BacktestEngine",
    "BacktestEvent",
    "BacktestEventError",
    "BacktestResult",
    "ConstantLiquidityModel",
    "ConstantSlippageModel",
    "DEFAULT_FILL_DATA_KINDS",
    "DEFAULT_REACT_TO_KINDS",
    "DeterministicFailureModel",
    "ENGINE_ROOT_EVENT_ID",
    "FEE_DENOMINATOR_PIPS",
    "FEE_MODEL_VERSION",
    "FAILURE_MODEL_VERSION",
    "FeeModel",
    "FillOutcome",
    "FlatGasModel",
    "FutureDataViolation",
    "GAS_MODEL_VERSION",
    "GasModel",
    "InvalidFillOutcomeError",
    "InvalidModelVersionError",
    "InvalidPayloadError",
    "InvalidSourcePriorityError",
    "InvalidStageError",
    "InvalidStageError",
    "InvalidUnitError",
    "KIND_BURN",
    "KIND_MINT",
    "KIND_OBSERVATION",
    "KIND_SHUTDOWN",
    "KIND_SWAP",
    "KIND_TICK",
    "LEDGER_VERSION",
    "LIQUIDITY_MODEL_VERSION",
    "LiquidityModel",
    "ModelBundle",
    "ModelError",
    "PositionState",
    "RiskCallback",
    "RiskDecision",
    "SLIPPAGE_DENOMINATOR_BPS",
    "SLIPPAGE_MODEL_VERSION",
    "SlippageModel",
    "SOURCE_PRIORITY_DATA",
    "SOURCE_PRIORITY_EXECUTION",
    "SOURCE_PRIORITY_RISK",
    "SOURCE_PRIORITY_STRATEGY",
    "SOURCE_PRIORITY_SYSTEM",
    "STAGE_DECISION",
    "STAGE_FILL",
    "STAGE_LATENCY",
    "STAGE_RISK",
    "STAGE_SYSTEM",
    "STATUS_DECISION_RECORDED",
    "STATUS_FILL_DELAYED",
    "STATUS_FILL_FILLED",
    "STATUS_FILL_PARTIAL",
    "STATUS_FILL_REJECTED",
    "STATUS_FUTURE_DATA_VIOLATION",
    "STATUS_LATENCY_RECORDED",
    "STATUS_RISK_APPROVED",
    "STATUS_RISK_REJECTED",
    "STATUS_SYSTEM_INIT",
    "STATUS_SYSTEM_SHUTDOWN",
    "SlippageModel",
    "StaticFeeModel",
    "StrategyCallback",
    "StrategyDecision",
    "StrategyDecisionRequest",
    "ZERO_LEDGER_HASH",
    "FailureModel",
    "ZeroSlippageModel",
    "assert_backtest_events_layer_is_pure",
    "assert_backtest_models_layer_is_pure",
    "empty_position_state",
]

__version__: str = "0.0.0"
