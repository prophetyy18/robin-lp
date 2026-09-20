"""V1 central risk and paper-execution gateway (T070).

T070 is the centralised, auditable approve/reject gateway every intent
must traverse before reaching execution or paper simulation. The
module exports the public types the strategy, paper-execution and
manual-write surfaces consume; the implementation lives in
:mod:`robinhood_lp.risk.checks`.

Design constraints (binding):

- **Centralised.** Every intent — from the strategy, from a manual
  reduce-only CLI write, or from a paper driver — reaches the
  execution layer through one and only one gateway. Execution and
  strategy cannot suppress, relax or override a rejection
  (T070 must-not clause).
- **Immutable.** Every public record is a frozen dataclass; equality
  and hashing follow dataclass identity. The audit trail binds the
  exact decision bytes to the intent it approved or rejected.
- **Versioned configuration.** A risk :class:`RiskConfig` carries its
  ``version_id``; every :class:`RiskDecision` records the config
  version it was produced under. A future config change cannot
  retroactively alter a recorded decision.
- **Strategy-independent circuit breaker.** The complete 5-minute
  target-token USDG return ``<= -80%`` is a strategy-independent
  :attr:`RiskVerdict.AUTO_EXIT`; the gateway never lets a
  strategy-side approval suppress it, and an incomplete 5-minute
  bar does not trigger it (PROJECT_GOALS.md ``G-EMERGENCY-01``,
  ``docs/spec/operations/OPERATOR_CONTROL.md`` ``CTRL-MARKET-001``).
- **Deny-by-default.** Any missing / stale / NaN / overflow /
  configuration error fails closed with a reason-coded
  :attr:`RiskVerdict.REJECTED` decision and no side effects.
- **No float on the protocol path.** Per ADR-004 every numeric
  field is a Python ``int`` (Q64.64 for ratios, atomic units for
  amounts, seconds for time).
- **Layer purity.** The risk module may depend on protocol, features
  (T053 quote), strategy (T060 snapshots), and discovery (T025
  admission); it must not import RPC, storage, signing, execution,
  presentation or the Web layer.

References:

- ``todo/phases/P07-risk-and-paper/T070.md`` — frozen contract.
- ``docs/spec/operations/OPERATOR_CONTROL.md`` — ``CTRL-MARKET-001`` /
  ``CTRL-AUTO-001`` / ``CTRL-AUTO-002`` / ``CTRL-MARKET-002`` /
  ``CTRL-CLI-001``.
- ``docs/intent/PROJECT_GOALS.md`` — ``G-RISK-01``,
  ``G-MARKET-RISK-LEVEL-01``, ``G-EMERGENCY-01``,
  ``G-STRATEGY-SPIKE-01``, ``G-LOSS-CONTROL-01``.
- T053 — point-in-time USDG conversion.
- T049 — fixed-Range ``liquidityDelta`` sizing.
"""

from __future__ import annotations

from robinhood_lp.risk.checks import (
    DEFAULT_RISK_CONFIG_VERSION,
    DEFAULT_RISK_VERSION,
    EXPERIMENTAL_TAG,
    EpisodeState,
    FiveMinuteEmergencyBreach,
    GasBudget,
    IntentKind,
    KillSwitch,
    RiskConfig,
    RiskContext,
    RiskDecision,
    RiskError,
    RiskIntent,
    RiskIntentSource,
    RiskScope,
    RiskVerdict,
    evaluate_risk,
    evaluate_risk_with_config,
    is_five_minute_emergency_breach,
)

__all__ = [
    "DEFAULT_RISK_CONFIG_VERSION",
    "DEFAULT_RISK_VERSION",
    "EXPERIMENTAL_TAG",
    "EpisodeState",
    "FiveMinuteEmergencyBreach",
    "GasBudget",
    "IntentKind",
    "KillSwitch",
    "RiskConfig",
    "RiskContext",
    "RiskDecision",
    "RiskError",
    "RiskIntent",
    "RiskIntentSource",
    "RiskScope",
    "RiskVerdict",
    "evaluate_risk",
    "evaluate_risk_with_config",
    "is_five_minute_emergency_breach",
]
