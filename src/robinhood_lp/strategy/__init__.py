"""V1 strategy layer public surface (T060).

The ``robinhood_lp.strategy`` package is the strategy layer per
``docs/spec/architecture/ARCHITECTURE.md`` §2.1: it is a *pure*
observation -> candidate-action function of timestamped state.

The layer depends only on the protocol-domain package (``ids``,
``events``, ``math``, ``sizing``) and the stdlib. It must not
import RPC, storage, configuration, signing, execution, or the
replay layer (the replay output is consumed by the caller, which
packages it into the :class:`MarketSnapshot` and
:class:`PortfolioSnapshot` the strategy reads).

The T060 module is the *contracts* half of the strategy layer.
It defines the immutable, versioned snapshots the strategy reads;
the substitutable :class:`RegimeModel` and
:class:`FeeOpportunityModel` components with their respective
:class:`RegimeAssessment` and
:class:`FeeOpportunityAssessment` outputs (the sole supported
extension point for a trained model per
``docs/spec/strategy/STRATEGY_ECONOMICS.md`` §7 and ADR-014 §5);
the deterministic :class:`DeterministicClock` and
:class:`SeededRandomSource` interfaces the strategy reads instead
of wall-clock time or unseeded randomness; the tick/capital
proposal validator that rejects invalid ticks, stale state, NaN
display values and excessive capital *before* a candidate action
is constructed; the structured :class:`ReasonCode` vocabulary the
``NO_TRADE`` verdict carries; and the :class:`CandidateAction`
the strategy submits to the central risk and execution layers.
The candidate action is the proposal the strategy submits to
execution; execution constructs the actual transactions.
"""

from __future__ import annotations

from robinhood_lp.strategy.base import (
    ADMISSION_SNAPSHOT_VERSION,
    CANDIDATE_ACTION_VERSION,
    CANDIDATE_KIND_NO_TRADE,
    CANDIDATE_KIND_PROPOSE,
    CANDIDATE_KIND_WAIT,
    CANDIDATE_SENTINEL_LIQUIDITY,
    CANDIDATE_SENTINEL_TICK,
    FEE_OPPORTUNITY_ASSESSMENT_VERSION,
    FEE_OPPORTUNITY_OUTCOME_ASSESSED,
    FEE_OPPORTUNITY_OUTCOME_UNCERTAIN,
    MARKET_SNAPSHOT_VERSION,
    MAX_CAPITAL_Q64_64,
    MAX_TICK_SPACING_STRATEGY,
    MIN_TICK_SPACING_STRATEGY,
    PORTFOLIO_SNAPSHOT_VERSION,
    Q64_SCALE,
    REGIME_ASSESSMENT_VERSION,
    REGIME_OUTCOME_ASSESSED,
    REGIME_OUTCOME_UNCERTAIN,
    REGIME_STATE_DOWN_TREND,
    REGIME_STATE_JUMP_RISK,
    REGIME_STATE_RANGE,
    REGIME_STATE_UNCERTAIN,
    REGIME_STATE_UP_TREND,
    AdmissionSnapshot,
    CandidateAction,
    CandidateActionError,
    DeterministicClock,
    DeterministicClockError,
    FeeOpportunityAssessment,
    FeeOpportunityModel,
    FrozenClock,
    FrozenSeededRandomSource,
    InvalidCapitalError,
    InvalidProposalError,
    InvalidSnapshotError,
    InvalidTickError,
    MarketSnapshot,
    NaNDisplayValueError,
    PortfolioSnapshot,
    ProposalValidationError,
    ReasonCode,
    RegimeAssessment,
    RegimeModel,
    SeededRandomSource,
    StaleSnapshotError,
    StrategyError,
    assert_strategy_layer_is_pure,
    collect_strategy_module_imports,
    evaluate_decision,
    validate_proposal,
)

__all__ = [
    "ADMISSION_SNAPSHOT_VERSION",
    "CANDIDATE_ACTION_VERSION",
    "CANDIDATE_KIND_NO_TRADE",
    "CANDIDATE_KIND_PROPOSE",
    "CANDIDATE_KIND_WAIT",
    "CANDIDATE_SENTINEL_LIQUIDITY",
    "CANDIDATE_SENTINEL_TICK",
    "FEE_OPPORTUNITY_ASSESSMENT_VERSION",
    "FEE_OPPORTUNITY_OUTCOME_ASSESSED",
    "FEE_OPPORTUNITY_OUTCOME_UNCERTAIN",
    "MARKET_SNAPSHOT_VERSION",
    "MAX_CAPITAL_Q64_64",
    "MAX_TICK_SPACING_STRATEGY",
    "MIN_TICK_SPACING_STRATEGY",
    "PORTFOLIO_SNAPSHOT_VERSION",
    "Q64_SCALE",
    "REGIME_ASSESSMENT_VERSION",
    "REGIME_OUTCOME_ASSESSED",
    "REGIME_OUTCOME_UNCERTAIN",
    "REGIME_STATE_DOWN_TREND",
    "REGIME_STATE_JUMP_RISK",
    "REGIME_STATE_RANGE",
    "REGIME_STATE_UNCERTAIN",
    "REGIME_STATE_UP_TREND",
    "AdmissionSnapshot",
    "CandidateAction",
    "CandidateActionError",
    "DeterministicClock",
    "DeterministicClockError",
    "FeeOpportunityAssessment",
    "FeeOpportunityModel",
    "FrozenClock",
    "FrozenSeededRandomSource",
    "InvalidCapitalError",
    "InvalidProposalError",
    "InvalidSnapshotError",
    "InvalidTickError",
    "MarketSnapshot",
    "NaNDisplayValueError",
    "PortfolioSnapshot",
    "ProposalValidationError",
    "RegimeAssessment",
    "RegimeModel",
    "ReasonCode",
    "SeededRandomSource",
    "StaleSnapshotError",
    "StrategyError",
    "assert_strategy_layer_is_pure",
    "collect_strategy_module_imports",
    "evaluate_decision",
    "validate_proposal",
]

__version__: str = "0.0.0"
