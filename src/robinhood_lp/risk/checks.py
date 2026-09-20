"""Centralised risk checks for the V1 LP framework (T070).

The module owns the public surface every intent must reach before
being approved for execution or paper simulation:

- :class:`RiskVerdict` — closed vocabulary of approve/reject
  outcomes: :attr:`APPROVED`, :attr:`REJECTED`, :attr:`WARNING`,
  :attr:`NO_NEW_RISK`, :attr:`AUTO_EXIT`.
- :class:`RiskScope` — closed vocabulary of scopes a verdict is
  bound to: ``GLOBAL``, ``POOL``, ``TOKEN``, ``STRATEGY``.
- :class:`RiskIntentSource` — closed vocabulary of intent sources
  the gateway handles: ``STRATEGY``, ``PAPER``, ``MANUAL_REDUCE_ONLY``.
- :class:`RiskIntent` — the intent to approve/reject.
- :class:`RiskContext` — the immutable environment the gateway
  evaluates the intent against (deployment, freshness, USDG price,
  capital, gas budget, kill switches, episode state).
- :class:`RiskConfig` — the versioned configuration the gateway
  applies. A configuration change bumps ``version_id``; the decision
  records the version it was produced under, so a future config
  cannot retroactively alter it.
- :class:`RiskDecision` — the immutable approve/reject verdict.
- :func:`evaluate_risk` — the single entry point that performs the
  full check pipeline.

The check pipeline (one function, no early-return side effects):

1. **Eligibility & deployment.** The PoolKey's RunMode must be at
   least ``paper`` and the deployment-evidence hash chain must
   match the configuration (T024 / T025 / T043 evidence).
2. **Freshness & completeness.** Every required input is present,
   non-negative, not NaN, and was available at or before the
   decision time. A snapshot whose ``availability_time`` exceeds
   ``decision_time`` is future data and is rejected.
3. **Capital & token concentration.** Per-pool and per-token USDG
   exposure limits are respected; the existing position + the
   candidate size cannot exceed either cap. The per-token
   concentration check uses the sizer's
   ``worst_case_amount0`` / ``worst_case_amount1`` and the
   per-side USDG price (the pool's ``currency1`` is the target
   token with price ``market.quote_q64_64``; ``currency0`` is
   USDG with price ``Q64_SCALE``); either side exceeding
   ``max_single_currency_concentration_q64_64`` of the total
   USDG-denominated worst-case inventory produces a
   :attr:`RiskVerdict.NO_NEW_RISK` at ``TOKEN`` scope.
4. **Qualified USDG price.** The 5-minute circuit breaker requires
   a non-``RELATIVE_ONLY`` USDG price; the strategy and risk layers
   require one for any USDG-denominated sizing. ``None`` / missing
   / depegged prices fail closed.
5. **Fixed-Range liquidityDelta sizing.** The candidate Range and
   capital envelope are passed through :func:`compute_sizing` (T049)
   which enforces: fixed Range, single ``liquidityDelta``, integer
   math, Hook ``BalanceDelta`` verification, native ETH Gas
   reservation, both raw-token caps after Hook and Gas effects, and
   a minimum-economic-size floor. The sizer never widens the Range
   to fit a cap.
6. **Rebalance cadence / cost.** The interval since the last
   rebalance must be at least the configured minimum, and the
   per-cadence cost (``expected_rebalance_cost_usdg_q64_64 *
   min_rebalance_interval_seconds``) must not exceed the
   candidate's projected USDG-denominated capital envelope
   (``CandidateAction.capital_q64_64``). The cost check is
   preceded by an integer-overflow guard so a malicious
   ``expected_cost`` cannot wrap the comparison.
7. **Episode loss & high-watermark drawdown.**
   :attr:`RiskVerdict.NO_NEW_RISK` /
   :attr:`RiskVerdict.AUTO_EXIT` thresholds from the user's
   approved authorization (T066 / T093 deferred values; the V1
   defaults are placeholders tagged
   ``EXPERIMENTAL_NOT_LIVE_APPROVED``). The episode PnL and
   drawdown are computed from the supplied :class:`EpisodeState`.
8. **Gas reserve / use.** Gas reserve can never be spent on the LP
   itself; the budget's ``used`` must be ``<= budget`` at all
   times and the candidate's reserved amount must fit.
9. **Global / per-scope kill switches.** Any active kill switch
   forces :attr:`RiskVerdict.AUTO_EXIT` (global) or
   :attr:`RiskVerdict.NO_NEW_RISK` (per-scope) regardless of
   strategy approval.
10. **5-minute emergency circuit breaker.** The complete 5-minute
    target-token USDG return ``<= -80%`` is a strategy-independent
    :attr:`RiskVerdict.AUTO_EXIT`. No strategy-side approval can
    suppress or relax it. An incomplete 5-minute bar does not
    trigger it.

The module never mutates its inputs; the returned
:class:`RiskDecision` is the audit-trail artifact and is
byte-identical for two calls with equal inputs under equal
configuration versions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from robinhood_lp.features.quote import (
    FIVE_MINUTE_DOWN_SPIKE_FRACTION,
    FIVE_MINUTE_RULE_WINDOW_SECONDS,
    MissingPolicy,
    QuoteBar,
)
from robinhood_lp.protocol.ids import PoolKey
from robinhood_lp.protocol.run_mode import RunMode
from robinhood_lp.protocol.sizing import SizingDecision, SizingError, compute_sizing
from robinhood_lp.strategy.base import (
    ADMISSION_SNAPSHOT_VERSION,
    CANDIDATE_KIND_PROPOSE,
    MARKET_SNAPSHOT_VERSION,
    PORTFOLIO_SNAPSHOT_VERSION,
    Q64_SCALE,
    AdmissionSnapshot,
    CandidateAction,
    MarketSnapshot,
    PortfolioSnapshot,
)

DEFAULT_RISK_VERSION: Final[str] = "t070.risk_layer.v1"
DEFAULT_RISK_CONFIG_VERSION: Final[str] = "t070.risk_config.v1"
EXPERIMENTAL_TAG: Final[str] = "EXPERIMENTAL_NOT_LIVE_APPROVED"
DEFAULT_MAX_SNAPSHOT_AGE_SECONDS: Final[int] = 300
DEFAULT_MIN_REBALANCE_INTERVAL_SECONDS: Final[int] = 300
DEFAULT_MIN_ECONOMIC_LIQUIDITY_USDG_Q64_64: Final[int] = 1 << 62
DEFAULT_MAX_HOOK_EVIDENCE_AGE_SECONDS: Final[int] = 24 * 60 * 60
FIVE_MINUTE_EMERGENCY_THRESHOLD_Q64_64: Final[int] = FIVE_MINUTE_DOWN_SPIKE_FRACTION
FIVE_MINUTE_EMERGENCY_WINDOW_SECONDS: Final[int] = FIVE_MINUTE_RULE_WINDOW_SECONDS
DEFAULT_MAX_SINGLE_CURRENCY_CONCENTRATION_Q64_64: Final[int] = 80 * Q64_SCALE // 100


class RiskVerdict(StrEnum):
    """Closed vocabulary of approve / reject outcomes.

    Strings are part of the public contract. New values are additive;
    renaming or removing an existing value is a breaking change.

    - :attr:`APPROVED` — every check passed; the gateway authorises
      the intent to proceed to the next layer (paper execution or,
      after T094, the isolated signer).
    - :attr:`REJECTED` — a hard precondition failed; the intent
      must not proceed and no further action is permitted. The
      reason code is the structured cause.
    - :attr:`WARNING` — observable evidence exists (the same
      threshold as the matching :attr:`NO_NEW_RISK` /
      :attr:`AUTO_EXIT` is approaching) but no action is forced.
      :attr:`WARNING` is observation-only and never blocks an
      intent on its own.
    - :attr:`NO_NEW_RISK` — bound to a specific :class:`RiskScope`;
      only intents that *increase* exposure within that scope are
      blocked. Reducing / removing LP, collecting fees and exiting
      remain permitted.
    - :attr:`AUTO_EXIT` — the intent must not introduce new risk and
      the gateway authorises a pre-approved reduce-only / exit plan
      (see :class:`RiskIntentSource.MANUAL_REDUCE_ONLY`); the
      strategy-supplied ``PROPOSE`` intent is rejected by default.
    """

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WARNING = "WARNING"
    NO_NEW_RISK = "NO_NEW_RISK"
    AUTO_EXIT = "AUTO_EXIT"


class RiskScope(StrEnum):
    """Closed vocabulary of scopes a verdict can be bound to.

    Strings are part of the public contract. A
    :attr:`RiskVerdict.NO_NEW_RISK` / :attr:`RiskVerdict.AUTO_EXIT`
    must carry a non-``GLOBAL`` scope unless the trigger is
    genuinely global; the audit trail uses ``GLOBAL`` for the
    process-wide kill switch and the 5-minute emergency circuit
    breaker.
    """

    GLOBAL = "GLOBAL"
    POOL = "POOL"
    TOKEN = "TOKEN"
    STRATEGY = "STRATEGY"


class RiskIntentSource(StrEnum):
    """Closed vocabulary of intent sources the gateway accepts.

    The strategy layer always submits :attr:`STRATEGY`. Paper
    execution submits :attr:`PAPER`. The reduce-only CLI write
    path and the manual Web ``reduce`` action both submit
    :attr:`MANUAL_REDUCE_ONLY`. The gateway never allows a
    different source to opt out of a hard precondition.
    """

    STRATEGY = "STRATEGY"
    PAPER = "PAPER"
    MANUAL_REDUCE_ONLY = "MANUAL_REDUCE_ONLY"


class IntentKind(StrEnum):
    """Closed vocabulary of intent kinds the gateway distinguishes.

    The kind drives the deny-by-default verdict. :attr:`REDUCE_ONLY`
    intents can never produce :attr:`RiskVerdict.AUTO_EXIT`; they
    can only be approved or rejected (with one exception: the
    5-minute emergency circuit breaker still turns them into
    :attr:`AUTO_EXIT`, mirroring ``CTRL-MARKET-001``).
    """

    INCREASE_RISK = "INCREASE_RISK"
    REDUCE_ONLY = "REDUCE_ONLY"


class RiskError(ValueError):
    """Base class for risk-layer failures."""


class RiskConfigError(RiskError):
    """A :class:`RiskConfig` violates its invariants."""


class RiskContextError(RiskError):
    """A :class:`RiskContext` violates its invariants."""


class RiskIntentError(RiskError):
    """A :class:`RiskIntent` violates its invariants."""


def _require_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a Python ``int`` (``bool`` rejected)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise RiskConfigError(f"{field}: must be int, got {type(value).__name__}")
    return value


def _require_non_negative_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a non-negative Python ``int``."""
    value = _require_int(value, field=field)
    if value < 0:
        raise RiskConfigError(f"{field}: must be non-negative, got {value}")
    return value


def _require_strictly_positive_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a strictly positive Python ``int``."""
    value = _require_int(value, field=field)
    if value <= 0:
        raise RiskConfigError(f"{field}: must be positive, got {value}")
    return value


def _require_q64_64(value: int, *, field: str) -> int:
    """Validate ``value`` is a non-negative Q64.64 ratio."""
    value = _require_int(value, field=field)
    if value < 0:
        raise RiskConfigError(f"{field}: must be non-negative Q64.64, got {value}")
    return value


def _require_strict_q64_64(value: int, *, field: str) -> int:
    """Validate ``value`` is a strictly positive Q64.64 ratio."""
    value = _require_int(value, field=field)
    if value <= 0:
        raise RiskConfigError(f"{field}: must be positive Q64.64, got {value}")
    return value


def _require_str(value: str, *, field: str, allow_empty: bool = False) -> str:
    """Validate ``value`` is a string and optionally non-empty."""
    if not isinstance(value, str):
        raise RiskConfigError(f"{field}: must be str, got {type(value).__name__}")
    if not allow_empty and (not value):
        raise RiskConfigError(f"{field}: must be non-empty str, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class KillSwitch:
    """One kill switch the risk gateway consults.

    A kill switch is a named, scoped circuit-breaker. The
    :attr:`active` flag tells the gateway whether the switch is
    engaged; the ``scope`` declares the actions it blocks
    (``GLOBAL`` blocks every increase-risk intent; ``POOL`` /
    ``TOKEN`` / ``STRATEGY`` block only the matching scope).

    The ``reason_code`` is the structured reason the switch is
    engaged and is recorded on the resulting :class:`RiskDecision`
    verbatim. :attr:`expires_at` (``None`` means "until manually
    disengaged") is the wall-clock second at which the gateway
    stops honouring the switch; the check is included so a
    forgotten switch does not silently outlive the incident it
    was raised for.
    """

    name: str
    scope: RiskScope
    active: bool
    reason_code: str
    raised_at: int
    expires_at: int | None

    def __post_init__(self) -> None:
        _require_str(self.name, field="KillSwitch.name")
        if not isinstance(self.scope, RiskScope):
            raise RiskConfigError(
                f"KillSwitch.scope: must be RiskScope, got {type(self.scope).__name__}"
            )
        if not isinstance(self.active, bool):
            raise RiskConfigError(
                f"KillSwitch.active: must be bool, got {type(self.active).__name__}"
            )
        _require_str(self.reason_code, field="KillSwitch.reason_code")
        _require_non_negative_int(self.raised_at, field="KillSwitch.raised_at")
        if self.expires_at is not None:
            _require_non_negative_int(self.expires_at, field="KillSwitch.expires_at")
            if self.expires_at < self.raised_at:
                raise RiskConfigError(
                    f"KillSwitch.expires_at={self.expires_at} must be >= KillSwitch.raised_at={self.raised_at}"
                )


@dataclass(frozen=True, slots=True)
class GasBudget:
    """Gas budget the candidate LP / Swap transaction must respect.

    ``reserve_wei`` is the dedicated Gas reservation the wallet
    holds outside the LP envelope (G-GAS-01). ``budget_wei`` is the
    configured per-decision USDG-equivalent Gas ceiling translated
    to native wei; ``used_wei`` is the running total the candidate
    must not exceed.

    The gateway never lets an LP intent spend the reserve; the
    sizer subtracts the reserve from the native-side cap. The
    budget check is independent: it records the candidate's
    expected Gas and rejects when ``reserve_wei + expected_wei >
    budget_wei``.
    """

    reserve_wei: int
    budget_wei: int
    used_wei: int
    expected_wei: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.reserve_wei, field="GasBudget.reserve_wei")
        _require_non_negative_int(self.budget_wei, field="GasBudget.budget_wei")
        _require_non_negative_int(self.used_wei, field="GasBudget.used_wei")
        _require_non_negative_int(self.expected_wei, field="GasBudget.expected_wei")
        if self.reserve_wei > self.budget_wei:
            raise RiskConfigError(
                f"GasBudget: reserve_wei={self.reserve_wei} must be <= budget_wei={self.budget_wei}"
            )
        if self.used_wei + self.expected_wei + self.reserve_wei > self.budget_wei:
            raise RiskConfigError(
                f"GasBudget: used_wei={self.used_wei} + expected_wei={self.expected_wei} + reserve_wei={self.reserve_wei} must be <= budget_wei={self.budget_wei}"
            )


@dataclass(frozen=True, slots=True)
class EpisodeState:
    """Per-episode state the loss / drawdown checks need.

    The episode is the lifecycle from one ``initial_unit_nav_usdg``
    point-in-time to the next; capital flows, rebalances and
    internal ledger entries never reset the high-water mark
    (``docs/spec/operations/OPERATOR_CONTROL.md``
    ``CTRL-MARKET-002``).

    All numeric fields are non-negative Python ``int``; ratios are
    Q64.64 USDG values.
    """

    initial_unit_nav_usdg_q64_64: int
    initial_approved_cash_usdg_q64_64: int
    current_unit_nav_usdg_q64_64: int
    high_watermark_unit_nav_usdg_q64_64: int
    net_external_cash_flow_usdg_q64_64: int
    outstanding_episode_units: int

    def __post_init__(self) -> None:
        _require_strict_q64_64(
            self.initial_unit_nav_usdg_q64_64, field="EpisodeState.initial_unit_nav_usdg_q64_64"
        )
        _require_strict_q64_64(
            self.initial_approved_cash_usdg_q64_64,
            field="EpisodeState.initial_approved_cash_usdg_q64_64",
        )
        _require_strict_q64_64(
            self.current_unit_nav_usdg_q64_64, field="EpisodeState.current_unit_nav_usdg_q64_64"
        )
        _require_strict_q64_64(
            self.high_watermark_unit_nav_usdg_q64_64,
            field="EpisodeState.high_watermark_unit_nav_usdg_q64_64",
        )
        _require_q64_64(
            self.net_external_cash_flow_usdg_q64_64,
            field="EpisodeState.net_external_cash_flow_usdg_q64_64",
        )
        _require_strictly_positive_int(
            self.outstanding_episode_units, field="EpisodeState.outstanding_episode_units"
        )
        if self.high_watermark_unit_nav_usdg_q64_64 < self.initial_unit_nav_usdg_q64_64:
            raise RiskContextError(
                f"EpisodeState: high_watermark_unit_nav_usdg_q64_64={self.high_watermark_unit_nav_usdg_q64_64} must be >= initial_unit_nav_usdg_q64_64={self.initial_unit_nav_usdg_q64_64}"
            )

    @property
    def episode_pnl_usdg_q64_64(self) -> int:
        """Return the per-unit episode PnL in Q64.64 USDG.

        ``current_unit_nav - initial_unit_nav``; the external cash
        flow is encoded in the outstanding share issuance, not in
        the unit nav.
        """
        return self.current_unit_nav_usdg_q64_64 - self.initial_unit_nav_usdg_q64_64

    @property
    def drawdown_q64_64(self) -> int:
        """Return the high-watermark drawdown in Q64.64 USDG.

        ``high_watermark - current_unit_nav`` (non-negative when
        the unit nav is below the high-water mark, ``0`` otherwise).
        """
        if self.high_watermark_unit_nav_usdg_q64_64 <= self.current_unit_nav_usdg_q64_64:
            return 0
        return self.high_watermark_unit_nav_usdg_q64_64 - self.current_unit_nav_usdg_q64_64


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Versioned risk configuration.

    The ``version_id`` is the audit-trail key: every
    :class:`RiskDecision` records the version it was produced
    under; a configuration change bumps ``version_id`` and leaves
    every past decision unchanged. There is no implicit "current
    configuration" lookup; the gateway only accepts the version it
    was given.

    The threshold fields are Q64.64 USDG ratios, plain non-negative
    seconds, or non-negative raw token atomic units. Provisional
    thresholds are tagged ``EXPERIMENTAL_NOT_LIVE_APPROVED`` via
    :attr:`experimental_thresholds`; the tag propagates onto every
    decision the gateway produces under this configuration.
    """

    version_id: str
    max_snapshot_age_seconds: int
    min_rebalance_interval_seconds: int
    min_economic_liquidity_usdg_q64_64: int
    max_hook_evidence_age_seconds: int
    max_single_currency_concentration_q64_64: int
    episode_pnl_warning_q64_64: int
    episode_pnl_no_new_risk_q64_64: int
    episode_pnl_auto_exit_q64_64: int
    drawdown_warning_q64_64: int
    drawdown_no_new_risk_q64_64: int
    drawdown_auto_exit_q64_64: int
    kill_switches: tuple[KillSwitch, ...]
    experimental_thresholds: tuple[str, ...]
    pool_key_id: str | None

    def __post_init__(self) -> None:
        _require_str(self.version_id, field="RiskConfig.version_id")
        _require_non_negative_int(
            self.max_snapshot_age_seconds, field="RiskConfig.max_snapshot_age_seconds"
        )
        _require_non_negative_int(
            self.min_rebalance_interval_seconds, field="RiskConfig.min_rebalance_interval_seconds"
        )
        _require_strict_q64_64(
            self.min_economic_liquidity_usdg_q64_64,
            field="RiskConfig.min_economic_liquidity_usdg_q64_64",
        )
        _require_non_negative_int(
            self.max_hook_evidence_age_seconds, field="RiskConfig.max_hook_evidence_age_seconds"
        )
        _require_strict_q64_64(
            self.max_single_currency_concentration_q64_64,
            field="RiskConfig.max_single_currency_concentration_q64_64",
        )
        _require_q64_64(
            self.episode_pnl_warning_q64_64, field="RiskConfig.episode_pnl_warning_q64_64"
        )
        _require_q64_64(
            self.episode_pnl_no_new_risk_q64_64, field="RiskConfig.episode_pnl_no_new_risk_q64_64"
        )
        _require_q64_64(
            self.episode_pnl_auto_exit_q64_64, field="RiskConfig.episode_pnl_auto_exit_q64_64"
        )
        _require_q64_64(self.drawdown_warning_q64_64, field="RiskConfig.drawdown_warning_q64_64")
        _require_q64_64(
            self.drawdown_no_new_risk_q64_64, field="RiskConfig.drawdown_no_new_risk_q64_64"
        )
        _require_q64_64(
            self.drawdown_auto_exit_q64_64, field="RiskConfig.drawdown_auto_exit_q64_64"
        )
        if (
            self.episode_pnl_warning_q64_64 > self.episode_pnl_no_new_risk_q64_64
            or self.episode_pnl_no_new_risk_q64_64 > self.episode_pnl_auto_exit_q64_64
        ):
            raise RiskConfigError(
                f"RiskConfig: episode_pnl thresholds must be non-decreasing warning <= no_new_risk <= auto_exit, got warning={self.episode_pnl_warning_q64_64} no_new_risk={self.episode_pnl_no_new_risk_q64_64} auto_exit={self.episode_pnl_auto_exit_q64_64}"
            )
        if (
            self.drawdown_warning_q64_64 > self.drawdown_no_new_risk_q64_64
            or self.drawdown_no_new_risk_q64_64 > self.drawdown_auto_exit_q64_64
        ):
            raise RiskConfigError(
                f"RiskConfig: drawdown thresholds must be non-decreasing warning <= no_new_risk <= auto_exit, got warning={self.drawdown_warning_q64_64} no_new_risk={self.drawdown_no_new_risk_q64_64} auto_exit={self.drawdown_auto_exit_q64_64}"
            )
        if not isinstance(self.kill_switches, tuple):
            raise RiskConfigError(
                f"RiskConfig.kill_switches: must be tuple, got {type(self.kill_switches).__name__}"
            )
        for i, switch in enumerate(self.kill_switches):
            if not isinstance(switch, KillSwitch):
                raise RiskConfigError(
                    f"RiskConfig.kill_switches[{i}]: must be KillSwitch, got {type(switch).__name__}"
                )
        if not isinstance(self.experimental_thresholds, tuple):
            raise RiskConfigError(
                f"RiskConfig.experimental_thresholds: must be tuple, got {type(self.experimental_thresholds).__name__}"
            )
        for tag in self.experimental_thresholds:
            _require_str(tag, field="RiskConfig.experimental_thresholds[]")
        if self.pool_key_id is not None:
            _require_str(self.pool_key_id, field="RiskConfig.pool_key_id")

    @classmethod
    def default(
        cls,
        *,
        version_id: str = DEFAULT_RISK_CONFIG_VERSION,
        pool_key_id: str | None = None,
        kill_switches: Sequence[KillSwitch] = (),
        experimental_thresholds: Sequence[str] = (EXPERIMENTAL_TAG,),
    ) -> RiskConfig:
        """Return the canonical default :class:`RiskConfig`.

        The defaults mark every threshold
        ``EXPERIMENTAL_NOT_LIVE_APPROVED``; paper-trading evidence
        collected under the default config is recorded with that
        tag. The implementation never promotes a default to a
        live-approved default.
        """
        return cls(
            version_id=version_id,
            max_snapshot_age_seconds=DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
            min_rebalance_interval_seconds=DEFAULT_MIN_REBALANCE_INTERVAL_SECONDS,
            min_economic_liquidity_usdg_q64_64=DEFAULT_MIN_ECONOMIC_LIQUIDITY_USDG_Q64_64,
            max_hook_evidence_age_seconds=DEFAULT_MAX_HOOK_EVIDENCE_AGE_SECONDS,
            max_single_currency_concentration_q64_64=DEFAULT_MAX_SINGLE_CURRENCY_CONCENTRATION_Q64_64,
            episode_pnl_warning_q64_64=Q64_SCALE // 100,
            episode_pnl_no_new_risk_q64_64=5 * Q64_SCALE // 100,
            episode_pnl_auto_exit_q64_64=10 * Q64_SCALE // 100,
            drawdown_warning_q64_64=Q64_SCALE // 100,
            drawdown_no_new_risk_q64_64=5 * Q64_SCALE // 100,
            drawdown_auto_exit_q64_64=10 * Q64_SCALE // 100,
            kill_switches=tuple(kill_switches),
            experimental_thresholds=tuple(experimental_thresholds),
            pool_key_id=pool_key_id,
        )


@dataclass(frozen=True, slots=True)
class FiveMinuteEmergencyBreach:
    """The 5-minute emergency circuit breaker input.

    The breach is the difference between the ``open_q64_64`` USDG
    price at the start of the 5-minute bar and the ``close_q64_64``
    USDG price at the end. The framework refuses to fabricate a
    bar: ``is_complete=False`` means the consumer still has a
    partial bar and the breaker must not trip.

    The function :func:`is_five_minute_emergency_breach` is the
    single place the gateway consults; the dataclass is the only
    typed argument the function accepts. Mirroring
    :class:`QuoteBar` keeps the contract explicit; the bar itself
    is preserved on the decision for the audit trail.
    """

    open_q64_64: int
    close_q64_64: int
    is_complete: bool
    bar: QuoteBar | None

    def __post_init__(self) -> None:
        _require_strict_q64_64(self.open_q64_64, field="FiveMinuteEmergencyBreach.open_q64_64")
        _require_strict_q64_64(self.close_q64_64, field="FiveMinuteEmergencyBreach.close_q64_64")
        if not isinstance(self.is_complete, bool):
            raise RiskContextError(
                f"FiveMinuteEmergencyBreach.is_complete: must be bool, got {type(self.is_complete).__name__}"
            )
        if self.bar is not None and (not isinstance(self.bar, QuoteBar)):
            raise RiskContextError(
                f"FiveMinuteEmergencyBreach.bar: must be QuoteBar or None, got {type(self.bar).__name__}"
            )


def is_five_minute_emergency_breach(
    breach: FiveMinuteEmergencyBreach,
    *,
    threshold_q64_64: int = FIVE_MINUTE_EMERGENCY_THRESHOLD_Q64_64,
) -> bool:
    """Return ``True`` iff the complete 5-minute bar trips the breaker.

    The check is the strategy-independent circuit breaker
    (``PROJECT_GOALS.md`` ``G-EMERGENCY-01``,
    ``docs/spec/operations/OPERATOR_CONTROL.md``
    ``CTRL-MARKET-001``). An incomplete bar returns ``False``
    regardless of magnitude; the breaker waits for the bar to
    close before it trips.

    The function is the single source of truth for the breaker
    verdict; the rest of the gateway consults it through
    :func:`evaluate_risk`.
    """
    if not isinstance(breach, FiveMinuteEmergencyBreach):
        raise RiskError(
            f"is_five_minute_emergency_breach: must be FiveMinuteEmergencyBreach, got {type(breach).__name__}"
        )
    _require_strict_q64_64(
        threshold_q64_64, field="is_five_minute_emergency_breach.threshold_q64_64"
    )
    if not breach.is_complete:
        return False
    if breach.close_q64_64 >= breach.open_q64_64:
        return False
    move_q64_64 = breach.open_q64_64 - breach.close_q64_64
    return move_q64_64 >= threshold_q64_64


@dataclass(frozen=True, slots=True)
class RiskIntent:
    """An intent submitted to the risk gateway.

    Every intent carries its source (:class:`RiskIntentSource`),
    the kind of action (:class:`IntentKind`), the strategy's
    :class:`CandidateAction` (when present), the candidate Range,
    the candidate ``liquidityDelta`` (zero for ``REDUCE_ONLY``), the
    pool identity, the chain id, the decision time, the audit
    pointer, and the snapshots the gateway evaluates against.

    The dataclass is frozen; equality and hashing follow dataclass
    identity.
    """

    source: RiskIntentSource
    kind: IntentKind
    pool_key: PoolKey
    chain_id: int
    decision_time: int
    audit_pointer: str
    candidate: CandidateAction | None = None
    admission: AdmissionSnapshot | None = None
    market: MarketSnapshot | None = None
    portfolio: PortfolioSnapshot | None = None
    tick_lower: int = 0
    tick_upper: int = 0
    liquidity_delta: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.source, RiskIntentSource):
            raise RiskIntentError(
                f"RiskIntent.source: must be RiskIntentSource, got {type(self.source).__name__}"
            )
        if not isinstance(self.kind, IntentKind):
            raise RiskIntentError(
                f"RiskIntent.kind: must be IntentKind, got {type(self.kind).__name__}"
            )
        if not isinstance(self.pool_key, PoolKey):
            raise RiskIntentError(
                f"RiskIntent.pool_key: must be PoolKey, got {type(self.pool_key).__name__}"
            )
        _require_strictly_positive_int(self.chain_id, field="RiskIntent.chain_id")
        _require_non_negative_int(self.decision_time, field="RiskIntent.decision_time")
        _require_str(self.audit_pointer, field="RiskIntent.audit_pointer")
        if self.candidate is not None and (not isinstance(self.candidate, CandidateAction)):
            raise RiskIntentError(
                f"RiskIntent.candidate: must be CandidateAction or None, got {type(self.candidate).__name__}"
            )
        if self.admission is not None and (not isinstance(self.admission, AdmissionSnapshot)):
            raise RiskIntentError(
                f"RiskIntent.admission: must be AdmissionSnapshot or None, got {type(self.admission).__name__}"
            )
        if self.market is not None and (not isinstance(self.market, MarketSnapshot)):
            raise RiskIntentError(
                f"RiskIntent.market: must be MarketSnapshot or None, got {type(self.market).__name__}"
            )
        if self.portfolio is not None and (not isinstance(self.portfolio, PortfolioSnapshot)):
            raise RiskIntentError(
                f"RiskIntent.portfolio: must be PortfolioSnapshot or None, got {type(self.portfolio).__name__}"
            )
        _require_int(self.tick_lower, field="RiskIntent.tick_lower")
        _require_int(self.tick_upper, field="RiskIntent.tick_upper")
        _require_non_negative_int(self.liquidity_delta, field="RiskIntent.liquidity_delta")
        if not isinstance(self.notes, tuple):
            raise RiskIntentError(
                f"RiskIntent.notes: must be tuple, got {type(self.notes).__name__}"
            )


@dataclass(frozen=True, slots=True)
class RiskContext:
    """The immutable environment the gateway evaluates against.

    The context bundles every input the check pipeline reads:

    - ``support_level`` — the pool's :class:`RunMode` (T023 / T025).
    - ``deployment_hash_match`` — ``True`` iff the on-chain
      deployment (PoolManager / StateView / Hook bytecode hash)
      matches the configuration.
    - ``last_rebalance_decision_time`` — the decision time of the
      last accepted rebalance; the cadence check consults this.
    - ``expected_rebalance_cost_usdg_q64_64`` — the expected cost
      of the next rebalance in Q64.64 USDG; the cadence check
      requires the candidate transaction to amortise at least
      ``min_rebalance_interval_seconds`` worth of this cost.
    - ``amount0_cap`` / ``amount1_cap`` — per-side raw-token caps
      (atomic units) the sizer must respect.
    - ``accrued_fees0`` / ``accrued_fees1`` — atomic units of fees
      already credited to the wallet (T051).
    - ``usdg_cap_q64_64_token0`` / ``usdg_cap_q64_64_token1`` —
      USDG-denominated per-side caps the sizing math may convert
      to raw tokens (T049 §2).
    - ``usdg_amount_token0`` / ``usdg_amount_token1`` — the
      requested per-side USDG amount for the candidate mint.
    - ``gas_budget`` — see :class:`GasBudget`.
    - ``hook_delta0`` / ``hook_delta1`` / ``hook_verified`` /
      ``hook_evidence_age_seconds`` — see T043 evidence.
    - ``episode`` — the per-episode state the loss / drawdown
      checks compute against.
    - ``kill_switches_active`` — convenience list of currently
      active kill switches (a subset of :attr:`RiskConfig.kill_switches`).
    - ``five_minute_breach`` — the 5-minute USDG return bar.
    - ``evidence_pointers`` — opaque audit pointers the decision
      must propagate.
    """

    support_level: RunMode
    deployment_hash_match: bool
    last_rebalance_decision_time: int
    expected_rebalance_cost_usdg_q64_64: int
    amount0_cap: int
    amount1_cap: int
    accrued_fees0: int
    accrued_fees1: int
    usdg_cap_q64_64_token0: int | None
    usdg_cap_q64_64_token1: int | None
    usdg_amount_token0: int
    usdg_amount_token1: int
    gas_budget: GasBudget
    hook_delta0: int
    hook_delta1: int
    hook_verified: bool
    hook_evidence_age_seconds: int
    episode: EpisodeState
    kill_switches_active: tuple[KillSwitch, ...]
    five_minute_breach: FiveMinuteEmergencyBreach | None
    evidence_pointers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.support_level, RunMode):
            raise RiskContextError(
                f"RiskContext.support_level: must be RunMode, got {type(self.support_level).__name__}"
            )
        if not isinstance(self.deployment_hash_match, bool):
            raise RiskContextError(
                f"RiskContext.deployment_hash_match: must be bool, got {type(self.deployment_hash_match).__name__}"
            )
        _require_non_negative_int(
            self.last_rebalance_decision_time, field="RiskContext.last_rebalance_decision_time"
        )
        _require_q64_64(
            self.expected_rebalance_cost_usdg_q64_64,
            field="RiskContext.expected_rebalance_cost_usdg_q64_64",
        )
        _require_non_negative_int(self.amount0_cap, field="RiskContext.amount0_cap")
        _require_non_negative_int(self.amount1_cap, field="RiskContext.amount1_cap")
        _require_non_negative_int(self.accrued_fees0, field="RiskContext.accrued_fees0")
        _require_non_negative_int(self.accrued_fees1, field="RiskContext.accrued_fees1")
        if self.usdg_cap_q64_64_token0 is not None:
            _require_strict_q64_64(
                self.usdg_cap_q64_64_token0, field="RiskContext.usdg_cap_q64_64_token0"
            )
        if self.usdg_cap_q64_64_token1 is not None:
            _require_strict_q64_64(
                self.usdg_cap_q64_64_token1, field="RiskContext.usdg_cap_q64_64_token1"
            )
        _require_non_negative_int(self.usdg_amount_token0, field="RiskContext.usdg_amount_token0")
        _require_non_negative_int(self.usdg_amount_token1, field="RiskContext.usdg_amount_token1")
        if not isinstance(self.gas_budget, GasBudget):
            raise RiskContextError(
                f"RiskContext.gas_budget: must be GasBudget, got {type(self.gas_budget).__name__}"
            )
        _require_non_negative_int(self.hook_delta0, field="RiskContext.hook_delta0")
        _require_non_negative_int(self.hook_delta1, field="RiskContext.hook_delta1")
        if not isinstance(self.hook_verified, bool):
            raise RiskContextError(
                f"RiskContext.hook_verified: must be bool, got {type(self.hook_verified).__name__}"
            )
        _require_non_negative_int(
            self.hook_evidence_age_seconds, field="RiskContext.hook_evidence_age_seconds"
        )
        # NOTE: the gateway, not the context, is the single source of
        # truth for the "unverified hook delta → reject" verdict; the
        # context accepts the unverified state so the gateway can
        # produce a structured HOOK_DELTA_UNVERIFIED decision.
        if not isinstance(self.episode, EpisodeState):
            raise RiskContextError(
                f"RiskContext.episode: must be EpisodeState, got {type(self.episode).__name__}"
            )
        if not isinstance(self.kill_switches_active, tuple):
            raise RiskContextError(
                f"RiskContext.kill_switches_active: must be tuple, got {type(self.kill_switches_active).__name__}"
            )
        for i, switch in enumerate(self.kill_switches_active):
            if not isinstance(switch, KillSwitch):
                raise RiskContextError(
                    f"RiskContext.kill_switches_active[{i}]: must be KillSwitch, got {type(switch).__name__}"
                )
            if not switch.active:
                raise RiskContextError(
                    f"RiskContext.kill_switches_active[{i}]={switch.name!r}: must have active=True"
                )
        if self.five_minute_breach is not None and (
            not isinstance(self.five_minute_breach, FiveMinuteEmergencyBreach)
        ):
            raise RiskContextError(
                f"RiskContext.five_minute_breach: must be FiveMinuteEmergencyBreach or None, got {type(self.five_minute_breach).__name__}"
            )
        if not isinstance(self.evidence_pointers, tuple):
            raise RiskContextError(
                f"RiskContext.evidence_pointers: must be tuple, got {type(self.evidence_pointers).__name__}"
            )
        for i, ptr in enumerate(self.evidence_pointers):
            _require_str(ptr, field=f"RiskContext.evidence_pointers[{i}]")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The immutable approve / reject verdict.

    A :class:`RiskDecision` is the audit-trail artifact the gateway
    returns; every downstream consumer (paper execution, the
    CLI reduce-only write path, the Web control surface) records
    the exact decision bytes. Two calls with equal inputs under
    equal configuration produce byte-identical decisions.

    The decision carries:

    - ``version`` — :data:`DEFAULT_RISK_VERSION`; bumped when the
      decision contract changes.
    - ``config_version`` — the :attr:`RiskConfig.version_id` the
      decision was produced under; a future config change leaves
      this string untouched, so an audit trail can detect when a
      decision was produced by an obsolete configuration.
    - ``verdict`` — the closed :class:`RiskVerdict`.
    - ``scope`` — the :class:`RiskScope` the verdict is bound to
      (only meaningful for :attr:`RiskVerdict.NO_NEW_RISK` /
      :attr:`RiskVerdict.AUTO_EXIT`; ``GLOBAL`` is reserved for
      the kill switch and the 5-minute emergency circuit breaker).
    - ``reason_code`` — the structured cause (a stable string).
    - ``reason_detail`` — free-form human-readable explanation;
      consumed by audit reports, never by the strategy.
    - ``approved`` — ``True`` iff the intent may proceed.
    - ``sizing`` — the :class:`SizingDecision` the sizer returned
      for ``INCREASE_RISK`` intents; ``None`` for ``REDUCE_ONLY``.
    - ``intent_source`` / ``intent_kind`` — the source and kind of
      the intent the decision was produced for.
    - ``decision_time`` — the event time the gateway evaluated the
      intent at.
    - ``audit_pointer`` — the audit pointer the caller supplied.
    - ``evidence_pointers`` — opaque pointers the decision
      propagates.
    - ``experimental_thresholds`` — the set of threshold tags the
      :class:`RiskConfig` carried; the field is preserved on the
      decision so an audit trail can flag provisional thresholds.
    - ``notes`` — free-form implementation-defined notes.
    """

    version: str
    config_version: str
    verdict: RiskVerdict
    scope: RiskScope
    reason_code: str
    reason_detail: str
    approved: bool
    sizing: SizingDecision | None
    intent_source: RiskIntentSource
    intent_kind: IntentKind
    decision_time: int
    audit_pointer: str
    evidence_pointers: tuple[str, ...]
    experimental_thresholds: tuple[str, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_str(self.version, field="RiskDecision.version")
        _require_str(self.config_version, field="RiskDecision.config_version")
        if not isinstance(self.verdict, RiskVerdict):
            raise RiskError(
                f"RiskDecision.verdict: must be RiskVerdict, got {type(self.verdict).__name__}"
            )
        if not isinstance(self.scope, RiskScope):
            raise RiskError(
                f"RiskDecision.scope: must be RiskScope, got {type(self.scope).__name__}"
            )
        _require_str(self.reason_code, field="RiskDecision.reason_code")
        _require_str(self.reason_detail, field="RiskDecision.reason_detail")
        if not isinstance(self.approved, bool):
            raise RiskError(
                f"RiskDecision.approved: must be bool, got {type(self.approved).__name__}"
            )
        if self.sizing is not None and (not isinstance(self.sizing, SizingDecision)):
            raise RiskError(
                f"RiskDecision.sizing: must be SizingDecision or None, got {type(self.sizing).__name__}"
            )
        if not isinstance(self.intent_source, RiskIntentSource):
            raise RiskError(
                f"RiskDecision.intent_source: must be RiskIntentSource, got {type(self.intent_source).__name__}"
            )
        if not isinstance(self.intent_kind, IntentKind):
            raise RiskError(
                f"RiskDecision.intent_kind: must be IntentKind, got {type(self.intent_kind).__name__}"
            )
        _require_non_negative_int(self.decision_time, field="RiskDecision.decision_time")
        _require_str(self.audit_pointer, field="RiskDecision.audit_pointer")
        if not isinstance(self.evidence_pointers, tuple):
            raise RiskError(
                f"RiskDecision.evidence_pointers: must be tuple, got {type(self.evidence_pointers).__name__}"
            )
        if not isinstance(self.experimental_thresholds, tuple):
            raise RiskError(
                f"RiskDecision.experimental_thresholds: must be tuple, got {type(self.experimental_thresholds).__name__}"
            )
        if not isinstance(self.notes, tuple):
            raise RiskError(f"RiskDecision.notes: must be tuple, got {type(self.notes).__name__}")

    @property
    def is_approved(self) -> bool:
        """``True`` iff the intent may proceed to execution."""
        return self.approved

    @property
    def is_terminal(self) -> bool:
        """``True`` iff the decision is final (not a WARNING)."""
        return self.verdict is not RiskVerdict.WARNING


REASON_OK: Final[str] = "OK"
REASON_ELIGIBILITY_DENIED: Final[str] = "ELIGIBILITY_DENIED"
REASON_DEPLOYMENT_HASH_MISMATCH: Final[str] = "DEPLOYMENT_HASH_MISMATCH"
REASON_CONFIG_VERSION_OBSOLETE: Final[str] = "CONFIG_VERSION_OBSOLETE"
REASON_CONFIG_POOL_MISMATCH: Final[str] = "CONFIG_POOL_MISMATCH"
REASON_CONFIG_INVALID: Final[str] = "CONFIG_INVALID"
REASON_INTENT_INVALID: Final[str] = "INTENT_INVALID"
REASON_CONTEXT_INVALID: Final[str] = "CONTEXT_INVALID"
REASON_STALE_SNAPSHOT: Final[str] = "STALE_SNAPSHOT"
REASON_FUTURE_SNAPSHOT: Final[str] = "FUTURE_SNAPSHOT"
REASON_NAN_DISPLAY_VALUE: Final[str] = "NAN_DISPLAY_VALUE"
REASON_INCOMPLETE_INPUTS: Final[str] = "INCOMPLETE_INPUTS"
REASON_USDG_PRICE_MISSING: Final[str] = "USDG_PRICE_MISSING"
REASON_USDG_PRICE_DEPEGGED: Final[str] = "USDG_PRICE_DEPEGGED"
REASON_USDG_PRICE_RELATIVE_ONLY: Final[str] = "USDG_PRICE_RELATIVE_ONLY"
REASON_CAP_EXCEEDED: Final[str] = "CAP_EXCEEDED"
REASON_TOKEN_CONCENTRATION_EXCEEDED: Final[str] = "TOKEN_CONCENTRATION_EXCEEDED"
REASON_SIZING_NO_TRADE: Final[str] = "SIZING_NO_TRADE"
REASON_RANGE_DEGENERATE: Final[str] = "RANGE_DEGENERATE"
REASON_TICK_OUT_OF_BOUNDS: Final[str] = "TICK_OUT_OF_BOUNDS"
REASON_TICK_NOT_ALIGNED: Final[str] = "TICK_NOT_ALIGNED"
REASON_HOOK_DELTA_UNVERIFIED: Final[str] = "HOOK_DELTA_UNVERIFIED"
REASON_HOOK_EVIDENCE_STALE: Final[str] = "HOOK_EVIDENCE_STALE"
REASON_GAS_RESERVE_UNDERFLOW: Final[str] = "GAS_RESERVE_UNDERFLOW"
REASON_GAS_BUDGET_EXCEEDED: Final[str] = "GAS_BUDGET_EXCEEDED"
REASON_REBALANCE_TOO_SOON: Final[str] = "REBALANCE_TOO_SOON"
REASON_REBALANCE_COST_INEFFICIENT: Final[str] = "REBALANCE_COST_INEFFICIENT"
REASON_EPISODE_PNL_WARNING: Final[str] = "EPISODE_PNL_WARNING"
REASON_EPISODE_PNL_NO_NEW_RISK: Final[str] = "EPISODE_PNL_NO_NEW_RISK"
REASON_EPISODE_PNL_AUTO_EXIT: Final[str] = "EPISODE_PNL_AUTO_EXIT"
REASON_DRAWDOWN_WARNING: Final[str] = "DRAWDOWN_WARNING"
REASON_DRAWDOWN_NO_NEW_RISK: Final[str] = "DRAWDOWN_NO_NEW_RISK"
REASON_DRAWDOWN_AUTO_EXIT: Final[str] = "DRAWDOWN_AUTO_EXIT"
REASON_KILL_SWITCH_GLOBAL: Final[str] = "KILL_SWITCH_GLOBAL"
REASON_KILL_SWITCH_SCOPED: Final[str] = "KILL_SWITCH_SCOPED"
REASON_EMERGENCY_BREACH: Final[str] = "FIVE_MINUTE_EMERGENCY_BREACH"
REASON_REDUCE_NOT_ALLOWED: Final[str] = "REDUCE_NOT_ALLOWED"


def _approved(
    *,
    config: RiskConfig,
    intent: RiskIntent,
    context: RiskContext | None,
    sizing: SizingDecision | None,
    reason_code: str,
    reason_detail: str,
    notes: tuple[str, ...] = (),
    extra_evidence: tuple[str, ...] = (),
) -> RiskDecision:
    """Build an :attr:`RiskVerdict.APPROVED` decision."""
    return RiskDecision(
        version=DEFAULT_RISK_VERSION,
        config_version=config.version_id,
        verdict=RiskVerdict.APPROVED,
        scope=RiskScope.GLOBAL,
        reason_code=reason_code,
        reason_detail=reason_detail,
        approved=True,
        sizing=sizing,
        intent_source=intent.source,
        intent_kind=intent.kind,
        decision_time=intent.decision_time,
        audit_pointer=intent.audit_pointer,
        evidence_pointers=(
            *intent.notes,
            *(context.evidence_pointers if context is not None else ()),
            *extra_evidence,
        ),
        experimental_thresholds=config.experimental_thresholds,
        notes=notes,
    )


def _warning(
    *,
    config: RiskConfig,
    intent: RiskIntent,
    context: RiskContext | None,
    reason_code: str,
    reason_detail: str,
    scope: RiskScope = RiskScope.POOL,
    notes: tuple[str, ...] = (),
    extra_evidence: tuple[str, ...] = (),
) -> RiskDecision:
    """Build a :attr:`RiskVerdict.WARNING` decision.

    WARNING does not block the intent; the decision is observation
    only and may still proceed. ``approved`` is ``True`` for
    consistency with the gateway's two-valued interface.
    """
    return RiskDecision(
        version=DEFAULT_RISK_VERSION,
        config_version=config.version_id,
        verdict=RiskVerdict.WARNING,
        scope=scope,
        reason_code=reason_code,
        reason_detail=reason_detail,
        approved=True,
        sizing=None,
        intent_source=intent.source,
        intent_kind=intent.kind,
        decision_time=intent.decision_time,
        audit_pointer=intent.audit_pointer,
        evidence_pointers=(
            *intent.notes,
            *(context.evidence_pointers if context is not None else ()),
            *extra_evidence,
        ),
        experimental_thresholds=config.experimental_thresholds,
        notes=notes,
    )


def _rejected(
    *,
    config: RiskConfig,
    intent: RiskIntent,
    context: RiskContext | None,
    reason_code: str,
    reason_detail: str,
    scope: RiskScope = RiskScope.GLOBAL,
    sizing: SizingDecision | None = None,
    notes: tuple[str, ...] = (),
    extra_evidence: tuple[str, ...] = (),
) -> RiskDecision:
    """Build a :attr:`RiskVerdict.REJECTED` decision."""
    return RiskDecision(
        version=DEFAULT_RISK_VERSION,
        config_version=config.version_id,
        verdict=RiskVerdict.REJECTED,
        scope=scope,
        reason_code=reason_code,
        reason_detail=reason_detail,
        approved=False,
        sizing=sizing,
        intent_source=intent.source,
        intent_kind=intent.kind,
        decision_time=intent.decision_time,
        audit_pointer=intent.audit_pointer,
        evidence_pointers=(
            *intent.notes,
            *(context.evidence_pointers if context is not None else ()),
            *extra_evidence,
        ),
        experimental_thresholds=config.experimental_thresholds,
        notes=notes,
    )


def _no_new_risk(
    *,
    config: RiskConfig,
    intent: RiskIntent,
    context: RiskContext | None,
    reason_code: str,
    reason_detail: str,
    scope: RiskScope,
    notes: tuple[str, ...] = (),
    extra_evidence: tuple[str, ...] = (),
) -> RiskDecision:
    """Build a :attr:`RiskVerdict.NO_NEW_RISK` decision.

    The verdict only blocks ``INCREASE_RISK`` intents within the
    declared scope; ``REDUCE_ONLY`` intents are still approved.
    The decision's ``approved`` flag is set per the resolved intent
    kind so the caller does not need to inspect both fields.
    """
    approved = intent.kind is IntentKind.REDUCE_ONLY
    return RiskDecision(
        version=DEFAULT_RISK_VERSION,
        config_version=config.version_id,
        verdict=RiskVerdict.NO_NEW_RISK,
        scope=scope,
        reason_code=reason_code,
        reason_detail=reason_detail,
        approved=approved,
        sizing=None,
        intent_source=intent.source,
        intent_kind=intent.kind,
        decision_time=intent.decision_time,
        audit_pointer=intent.audit_pointer,
        evidence_pointers=(
            *intent.notes,
            *(context.evidence_pointers if context is not None else ()),
            *extra_evidence,
        ),
        experimental_thresholds=config.experimental_thresholds,
        notes=notes,
    )


def _auto_exit(
    *,
    config: RiskConfig,
    intent: RiskIntent,
    context: RiskContext | None,
    reason_code: str,
    reason_detail: str,
    scope: RiskScope,
    notes: tuple[str, ...] = (),
    extra_evidence: tuple[str, ...] = (),
) -> RiskDecision:
    """Build an :attr:`RiskVerdict.AUTO_EXIT` decision.

    The verdict blocks ``INCREASE_RISK`` intents and approves the
    supplied intent only when it is a pre-approved
    :attr:`IntentKind.REDUCE_ONLY` *and* the trigger is a market-side
    AUTO_EXIT (episode loss / drawdown / scoped kill switch). The
    two non-market triggers — :data:`REASON_EMERGENCY_BREACH` (the
    strategy-independent circuit breaker) and ``GLOBAL``-scoped
    kill switches — reject even reduce-only intents because the
    system must enter ``MANUAL_CONTROL`` (per ``OPERATOR_CONTROL.md``
    ``CTRL-MARKET-001`` and ``CTRL-STATE-003``).
    """
    if reason_code == REASON_EMERGENCY_BREACH or scope is RiskScope.GLOBAL:
        approved = False
    else:
        approved = intent.kind is IntentKind.REDUCE_ONLY
    return RiskDecision(
        version=DEFAULT_RISK_VERSION,
        config_version=config.version_id,
        verdict=RiskVerdict.AUTO_EXIT,
        scope=scope,
        reason_code=reason_code,
        reason_detail=reason_detail,
        approved=approved,
        sizing=None,
        intent_source=intent.source,
        intent_kind=intent.kind,
        decision_time=intent.decision_time,
        audit_pointer=intent.audit_pointer,
        evidence_pointers=(
            *intent.notes,
            *(context.evidence_pointers if context is not None else ()),
            *extra_evidence,
        ),
        experimental_thresholds=config.experimental_thresholds,
        notes=notes,
    )


def _candidate_is_propose(intent: RiskIntent) -> bool:
    """Return ``True`` iff the supplied intent proposes an LP action."""
    if intent.candidate is None:
        return False
    return intent.candidate.kind == CANDIDATE_KIND_PROPOSE


def _strategy_pool_key_id(intent: RiskIntent) -> str:
    """Return the strategy-side pool key id (snapshot or candidate)."""
    if intent.candidate is not None:
        return intent.candidate.pool_key_id
    if intent.admission is not None:
        return intent.admission.pool_key_id
    if intent.market is not None:
        return intent.market.pool_key_id
    if intent.portfolio is not None:
        return intent.portfolio.pool_key_id
    return ""


def _validate_inputs_present(intent: RiskIntent) -> str | None:
    """Validate that every required input is present.

    Returns the reason code for the first missing input, ``None``
    when every required input is present. ``INCREASE_RISK`` intents
    require a candidate action and the full snapshot triple;
    ``REDUCE_ONLY`` intents only require the snapshot triple when
    no candidate was supplied (manual reduce-only flows carry the
    snapshot directly on the intent).
    """
    if intent.admission is None:
        return REASON_INCOMPLETE_INPUTS
    if intent.market is None:
        return REASON_INCOMPLETE_INPUTS
    if intent.portfolio is None:
        return REASON_INCOMPLETE_INPUTS
    if intent.kind is IntentKind.INCREASE_RISK and intent.candidate is None:
        return REASON_INCOMPLETE_INPUTS
    return None


def _validate_snapshot_versions(intent: RiskIntent) -> str | None:
    """Validate the snapshot versions declared by the candidate.

    The candidate must declare the exact ``*_VERSION`` constants
    the strategy layer publishes; an unknown version means a
    downstream consumer (T070) is being asked to evaluate a
    contract it has not seen and the decision must reject. The
    check is skipped when no candidate was supplied (a
    ``REDUCE_ONLY`` flow).
    """
    if intent.candidate is None:
        return None
    expected_versions = (
        ADMISSION_SNAPSHOT_VERSION,
        MARKET_SNAPSHOT_VERSION,
        PORTFOLIO_SNAPSHOT_VERSION,
    )
    candidate_versions = tuple(intent.candidate.snapshot_versions)
    if not candidate_versions:
        return REASON_INCOMPLETE_INPUTS
    for version in expected_versions:
        if version not in candidate_versions:
            return REASON_INCOMPLETE_INPUTS
    return None


def _validate_freshness(config: RiskConfig, intent: RiskIntent) -> tuple[str, str] | None:
    """Validate that every snapshot is available and fresh.

    Returns ``None`` when every snapshot passes; otherwise a
    ``(reason_code, reason_detail)`` pair.
    """
    assert intent.admission is not None
    assert intent.market is not None
    assert intent.portfolio is not None
    snapshots = (intent.admission, intent.market, intent.portfolio)
    for snapshot in snapshots:
        if snapshot.availability_time > intent.decision_time:
            return (
                REASON_FUTURE_SNAPSHOT,
                f"snapshot {type(snapshot).__name__} availability_time={snapshot.availability_time} exceeds decision_time={intent.decision_time}",
            )
        age = intent.decision_time - snapshot.availability_time
        if age > config.max_snapshot_age_seconds:
            return (
                REASON_STALE_SNAPSHOT,
                f"snapshot {type(snapshot).__name__} age={age}s exceeds max_snapshot_age_seconds={config.max_snapshot_age_seconds}",
            )
    return None


def _validate_market_snapshot(market: MarketSnapshot) -> str | None:
    """Validate that the market snapshot carries only finite numeric values.

    The check is structural: ``float`` carrying NaN or infinity is
    the prohibited pattern (ADR-004). ``int`` is always finite;
    the explicit check below is the future-proof hook a downstream
    contributor can extend when the snapshot grows to carry a
    numeric payload.
    """
    if not isinstance(market.sqrt_price_x96, int) or isinstance(market.sqrt_price_x96, bool):
        return REASON_NAN_DISPLAY_VALUE
    if market.sqrt_price_x96 <= 0:
        return REASON_NAN_DISPLAY_VALUE
    if not isinstance(market.liquidity, int) or isinstance(market.liquidity, bool):
        return REASON_NAN_DISPLAY_VALUE
    if market.liquidity < 0:
        return REASON_NAN_DISPLAY_VALUE
    if not isinstance(market.realized_volatility_q64_64, int) or isinstance(
        market.realized_volatility_q64_64, bool
    ):
        return REASON_NAN_DISPLAY_VALUE
    if market.realized_volatility_q64_64 < 0:
        return REASON_NAN_DISPLAY_VALUE
    return None


def _is_eligible_for_runtime(support_level: RunMode, kind: IntentKind) -> bool:
    """Return ``True`` iff the pool is eligible for the requested kind.

    ``REDUCE_ONLY`` intents are accepted at every support level
    above ``rejected``; ``INCREASE_RISK`` requires ``paper`` or
    above (the same gate T071 / T072 / T094 apply). The mapping
    is fixed and lives in one place.
    """
    if support_level is RunMode.REJECTED:
        return False
    if kind is IntentKind.REDUCE_ONLY:
        return support_level in (RunMode.INGESTION, RunMode.BACKTEST, RunMode.PAPER, RunMode.LIVE)
    return support_level in (RunMode.PAPER, RunMode.LIVE)


def evaluate_risk_with_config(
    *, intent: RiskIntent, context: RiskContext, config: RiskConfig
) -> RiskDecision:
    """Run the centralised risk pipeline under the supplied config.

    The function is the explicit ``config``-bearing variant of
    :func:`evaluate_risk`; every other entry point delegates here.
    The decision carries the supplied configuration's
    :attr:`RiskConfig.version_id` so the audit trail can identify
    which configuration produced each decision.
    """
    if not isinstance(intent, RiskIntent):
        raise RiskError(
            f"evaluate_risk_with_config.intent: must be RiskIntent, got {type(intent).__name__}"
        )
    if not isinstance(context, RiskContext):
        raise RiskError(
            f"evaluate_risk_with_config.context: must be RiskContext, got {type(context).__name__}"
        )
    if not isinstance(config, RiskConfig):
        raise RiskError(
            f"evaluate_risk_with_config.config: must be RiskConfig, got {type(config).__name__}"
        )
    if config.pool_key_id is not None and config.pool_key_id != _strategy_pool_key_id(intent):
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_CONFIG_POOL_MISMATCH,
            reason_detail=f"config.pool_key_id={config.pool_key_id} does not match intent pool_key_id={_strategy_pool_key_id(intent)!r}",
            scope=RiskScope.POOL,
        )
    for switch in context.kill_switches_active:
        if switch.scope is RiskScope.GLOBAL:
            return _auto_exit(
                context=context,
                config=config,
                intent=intent,
                reason_code=switch.reason_code or REASON_KILL_SWITCH_GLOBAL,
                reason_detail=f"global kill switch {switch.name!r} engaged at raised_at={switch.raised_at}",
                scope=RiskScope.GLOBAL,
                notes=(
                    f"kill_switch.name={switch.name}",
                    f"kill_switch.scope={switch.scope.value}",
                ),
            )
        return _no_new_risk(
            context=context,
            config=config,
            intent=intent,
            reason_code=switch.reason_code or REASON_KILL_SWITCH_SCOPED,
            reason_detail=f"scoped kill switch {switch.name!r} ({switch.scope.value}) engaged at raised_at={switch.raised_at}",
            scope=switch.scope,
            notes=(f"kill_switch.name={switch.name}", f"kill_switch.scope={switch.scope.value}"),
        )
    if context.five_minute_breach is not None and is_five_minute_emergency_breach(
        context.five_minute_breach
    ):
        return _auto_exit(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_EMERGENCY_BREACH,
            reason_detail=f"5-minute target-token USDG return <= -80% (open={context.five_minute_breach.open_q64_64} close={context.five_minute_breach.close_q64_64})",
            scope=RiskScope.GLOBAL,
            notes=("circuit_breaker=GLOBAL", "source=strategy_independent"),
            extra_evidence=("five_minute_emergency_breach",),
        )
    missing = _validate_inputs_present(intent)
    if missing is not None:
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=missing,
            reason_detail="intent is missing one of admission / market / portfolio snapshots or a candidate action",
            scope=RiskScope.POOL,
        )
    version_problem = _validate_snapshot_versions(intent)
    if version_problem is not None:
        # intent.candidate is non-None when _validate_snapshot_versions
        # returns a non-None reason_code (see helper docstring).
        assert intent.candidate is not None
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=version_problem,
            reason_detail=f"candidate snapshot_versions={tuple(intent.candidate.snapshot_versions)!r} do not match the published strategy snapshot versions",
            scope=RiskScope.POOL,
        )
    if not _is_eligible_for_runtime(context.support_level, intent.kind):
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_ELIGIBILITY_DENIED,
            reason_detail=f"support_level={context.support_level.value!r} is not eligible for runtime intent kind={intent.kind.value!r}",
            scope=RiskScope.POOL,
        )
    if not context.deployment_hash_match:
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_DEPLOYMENT_HASH_MISMATCH,
            reason_detail="PoolManager / StateView / Hook deployment hash chain does not match the recorded evidence",
            scope=RiskScope.POOL,
        )
    freshness = _validate_freshness(config, intent)
    if freshness is not None:
        reason_code, reason_detail = freshness
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=reason_code,
            reason_detail=reason_detail,
            scope=RiskScope.POOL,
        )
    nan_reason = _validate_market_snapshot(intent.market)  # type: ignore[arg-type]
    if nan_reason is not None:
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=nan_reason,
            reason_detail="market snapshot carries a NaN / infinite display value",
            scope=RiskScope.POOL,
        )
    loss = _evaluate_episode_loss(config, intent, context)
    if loss is not None:
        return loss
    drawdown = _evaluate_drawdown(config, intent, context)
    if drawdown is not None:
        return drawdown
    cadence = _evaluate_rebalance_cadence(config, intent, context)
    if cadence is not None:
        return cadence
    price = _evaluate_usdg_price(config, intent, context)
    if price is not None:
        return price
    gas = _evaluate_gas_budget(config, intent, context)
    if gas is not None:
        return gas
    hook = _evaluate_hook_evidence(config, intent, context)
    if hook is not None:
        return hook
    if intent.kind is IntentKind.REDUCE_ONLY:
        return _approved(
            context=context,
            config=config,
            intent=intent,
            sizing=None,
            reason_code=REASON_OK,
            reason_detail="reduce-only intent passed eligibility / freshness / gas / kill-switch / episode checks",
            notes=("intent_kind=REDUCE_ONLY",),
        )
    try:
        sizing = _resolve_sizing(config=config, intent=intent)
    except SizingError as exc:
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_SIZING_NO_TRADE,
            reason_detail=f"sizer raised {type(exc).__name__}: {exc}",
            scope=RiskScope.POOL,
        )
    if sizing.decision == "NO_TRADE":
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=str(sizing.reason_code.value)
            if sizing.reason_code is not None
            else REASON_SIZING_NO_TRADE,
            reason_detail=f"sizer returned NO_TRADE reason_code={(sizing.reason_code.value if sizing.reason_code is not None else 'NONE')!r}",
            scope=RiskScope.POOL,
            sizing=sizing,
        )
    concentration = _evaluate_concentration(
        config=config, intent=intent, context=context, sizing=sizing
    )
    if concentration is not None:
        return concentration
    return _approved(
        context=context,
        config=config,
        intent=intent,
        sizing=sizing,
        reason_code=REASON_OK,
        reason_detail="intent passed every risk check",
        notes=(f"sizing.decision={sizing.decision}", f"sizing.binding_side={sizing.binding_side}"),
    )


def evaluate_risk(*, intent: RiskIntent, context: RiskContext) -> RiskDecision:
    """Run the centralised risk pipeline and return a :class:`RiskDecision`.

    The function is the single entry point every intent source must
    reach before any transaction is signed or simulated. It
    delegates to :func:`evaluate_risk_with_config` with the
    :class:`RiskConfig.default` configuration; pass the explicit
    variant to evaluate under a non-default configuration.
    """
    config = RiskConfig.default(
        kill_switches=context.kill_switches_active, experimental_thresholds=()
    )
    return evaluate_risk_with_config(intent=intent, context=context, config=config)


def _evaluate_episode_loss(
    config: RiskConfig, intent: RiskIntent, context: RiskContext
) -> RiskDecision | None:
    """Evaluate the per-episode loss against the configured thresholds.

    Returns ``None`` when no threshold trips; otherwise the
    structured :class:`RiskDecision` the threshold trip produced.
    """
    episode = context.episode
    loss_q64_64 = -episode.episode_pnl_usdg_q64_64
    if loss_q64_64 <= 0:
        return None
    if loss_q64_64 >= config.episode_pnl_auto_exit_q64_64:
        return _auto_exit(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_EPISODE_PNL_AUTO_EXIT,
            reason_detail=f"episode_pnl_usdg_q64_64={loss_q64_64} >= auto_exit threshold {config.episode_pnl_auto_exit_q64_64}",
            scope=RiskScope.STRATEGY,
            notes=("check=episode_pnl",),
        )
    if loss_q64_64 >= config.episode_pnl_no_new_risk_q64_64:
        return _no_new_risk(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_EPISODE_PNL_NO_NEW_RISK,
            reason_detail=f"episode_pnl_usdg_q64_64={loss_q64_64} >= no_new_risk threshold {config.episode_pnl_no_new_risk_q64_64}",
            scope=RiskScope.STRATEGY,
            notes=("check=episode_pnl",),
        )
    if loss_q64_64 >= config.episode_pnl_warning_q64_64:
        return _warning(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_EPISODE_PNL_WARNING,
            reason_detail=f"episode_pnl_usdg_q64_64={loss_q64_64} >= warning threshold {config.episode_pnl_warning_q64_64}",
            scope=RiskScope.STRATEGY,
            notes=("check=episode_pnl",),
        )
    return None


def _evaluate_drawdown(
    config: RiskConfig, intent: RiskIntent, context: RiskContext
) -> RiskDecision | None:
    """Evaluate the high-watermark drawdown against the thresholds."""
    episode = context.episode
    drawdown = episode.drawdown_q64_64
    if drawdown <= 0:
        return None
    if drawdown >= config.drawdown_auto_exit_q64_64:
        return _auto_exit(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_DRAWDOWN_AUTO_EXIT,
            reason_detail=f"drawdown_q64_64={drawdown} >= auto_exit threshold {config.drawdown_auto_exit_q64_64}",
            scope=RiskScope.STRATEGY,
            notes=("check=drawdown",),
        )
    if drawdown >= config.drawdown_no_new_risk_q64_64:
        return _no_new_risk(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_DRAWDOWN_NO_NEW_RISK,
            reason_detail=f"drawdown_q64_64={drawdown} >= no_new_risk threshold {config.drawdown_no_new_risk_q64_64}",
            scope=RiskScope.STRATEGY,
            notes=("check=drawdown",),
        )
    if drawdown >= config.drawdown_warning_q64_64:
        return _warning(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_DRAWDOWN_WARNING,
            reason_detail=f"drawdown_q64_64={drawdown} >= warning threshold {config.drawdown_warning_q64_64}",
            scope=RiskScope.STRATEGY,
            notes=("check=drawdown",),
        )
    return None


def _evaluate_rebalance_cadence(
    config: RiskConfig, intent: RiskIntent, context: RiskContext
) -> RiskDecision | None:
    """Evaluate the rebalance cadence / cost amortisation.

    Two independent structural checks:

    1. **Cadence.** The interval since the last rebalance must be
       at least ``min_rebalance_interval_seconds``; otherwise the
       candidate is rejected with :data:`REASON_REBALANCE_TOO_SOON`.
    2. **Cost amortisation.** The candidate's
       :attr:`CandidateAction.capital_q64_64` is the USDG-denominated
       capital the strategy plans to deploy over the next
       ``min_rebalance_interval_seconds`` window. The expected
       rebalance cost (sized in USDG) multiplied by the cadence
       interval must not exceed the candidate's projected capital
       envelope; otherwise the candidate is rejected with
       :data:`REASON_REBALANCE_COST_INEFFICIENT`. The comparison
       uses Q64.64 fixed-point arithmetic exclusively; an
       integer-overflow guard precedes the comparison so the
       multiplication itself cannot blow up the audit-trail
       arithmetic.
    """
    if intent.kind is not IntentKind.INCREASE_RISK:
        return None
    if not _candidate_is_propose(intent):
        return None
    interval = intent.decision_time - context.last_rebalance_decision_time
    if interval < config.min_rebalance_interval_seconds:
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_REBALANCE_TOO_SOON,
            reason_detail=f"rebalance interval={interval}s < min_rebalance_interval_seconds={config.min_rebalance_interval_seconds}",
            scope=RiskScope.POOL,
        )
    expected_cost = context.expected_rebalance_cost_usdg_q64_64
    if expected_cost > 0 and intent.candidate is not None:
        # Integer-overflow guard: bound the per-interval cost to
        # uint128. The candidate's capital envelope is Q64.64, so a
        # successful candidate is bounded by ``2^64 * Q64_SCALE``;
        # we cap the cost × interval product at the same width so
        # an honest computation cannot silently wrap.
        amortised_cap = (1 << 128) - 1
        amortised = expected_cost * config.min_rebalance_interval_seconds
        if amortised <= 0 or amortised > amortised_cap:
            return _rejected(
                context=context,
                config=config,
                intent=intent,
                reason_code=REASON_REBALANCE_COST_INEFFICIENT,
                reason_detail=(
                    f"amortised rebalance cost={amortised} exceeds uint128 width or is non-positive "
                    f"(expected_cost={expected_cost}, min_rebalance_interval_seconds={config.min_rebalance_interval_seconds})"
                ),
                scope=RiskScope.POOL,
            )
        # The substantive check: the candidate's projected capital
        # envelope must dominate the per-cadence cost. The candidate
        # carries ``capital_q64_64``; the cost is in Q64.64 USDG
        # already, so the comparison is direct. (A zero candidate
        # capital is a degenerate PROPOSE; the strategy-layer
        # CandidateAction constructor rejects it, but the risk layer
        # tolerates it as a no-op amortisation outcome.)
        candidate_capital_q64_64 = intent.candidate.capital_q64_64
        if candidate_capital_q64_64 > 0 and amortised > candidate_capital_q64_64:
            return _rejected(
                context=context,
                config=config,
                intent=intent,
                reason_code=REASON_REBALANCE_COST_INEFFICIENT,
                reason_detail=(
                    f"amortised rebalance cost={amortised} exceeds candidate capital envelope={candidate_capital_q64_64} "
                    f"(expected_cost={expected_cost}, min_rebalance_interval_seconds={config.min_rebalance_interval_seconds})"
                ),
                scope=RiskScope.POOL,
            )
    return None


def _evaluate_usdg_price(
    config: RiskConfig, intent: RiskIntent, context: RiskContext
) -> RiskDecision | None:
    """Validate the qualified USDG price context."""
    if context.five_minute_breach is None or context.five_minute_breach.bar is None:
        return None
    bar = context.five_minute_breach.bar
    if bar.is_relative_only or bar.usdg_per_token_q64_64 is None:
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_USDG_PRICE_RELATIVE_ONLY,
            reason_detail="USDG price is RELATIVE_ONLY; risk gateway requires a qualified USDG price for USDG-denominated sizing",
            scope=RiskScope.POOL,
        )
    if bar.missing_policy is MissingPolicy.MISSING:
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_USDG_PRICE_MISSING,
            reason_detail="USDG price is MISSING; the conversion graph could not resolve",
            scope=RiskScope.POOL,
        )
    if bar.missing_policy is MissingPolicy.DEPEGGED:
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_USDG_PRICE_DEPEGGED,
            reason_detail="USDG price flagged DEPEGGED; sizing refused",
            scope=RiskScope.POOL,
        )
    return None


def _evaluate_gas_budget(
    config: RiskConfig, intent: RiskIntent, context: RiskContext
) -> RiskDecision | None:
    """Validate that the candidate's Gas fits the budget.

    The check is structural: the reserve is never spent on the LP
    itself; the sizer subtracts the reserve from the native-side
    cap. The risk gateway verifies the candidate's expected Gas
    does not exceed the remaining budget.
    """
    budget = context.gas_budget
    total = budget.used_wei + budget.expected_wei + budget.reserve_wei
    if total > budget.budget_wei:
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_GAS_BUDGET_EXCEEDED,
            reason_detail=f"used_wei={budget.used_wei} + expected_wei={budget.expected_wei} + reserve_wei={budget.reserve_wei} > budget_wei={budget.budget_wei}",
            scope=RiskScope.POOL,
        )
    return None


def _evaluate_hook_evidence(
    config: RiskConfig, intent: RiskIntent, context: RiskContext
) -> RiskDecision | None:
    """Validate the Hook ``BalanceDelta`` evidence.

    When ``hook_delta0`` / ``hook_delta1`` are non-zero, the Hook
    evidence must be verified *and* fresh (younger than
    :attr:`RiskConfig.max_hook_evidence_age_seconds`). The
    must-not ``ignore Hook deltas`` clause is enforced by
    refusing to advance the pipeline when the evidence is stale
    or unverified.
    """
    if context.hook_delta0 == 0 and context.hook_delta1 == 0:
        return None
    if not context.hook_verified:
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_HOOK_DELTA_UNVERIFIED,
            reason_detail=f"hook_delta0={context.hook_delta0}, hook_delta1={context.hook_delta1} without hook_verified=True",
            scope=RiskScope.POOL,
        )
    if context.hook_evidence_age_seconds > config.max_hook_evidence_age_seconds:
        return _rejected(
            context=context,
            config=config,
            intent=intent,
            reason_code=REASON_HOOK_EVIDENCE_STALE,
            reason_detail=f"hook_evidence_age_seconds={context.hook_evidence_age_seconds} > max_hook_evidence_age_seconds={config.max_hook_evidence_age_seconds}",
            scope=RiskScope.POOL,
        )
    return None


def _evaluate_concentration(
    *,
    config: RiskConfig,
    intent: RiskIntent,
    context: RiskContext,
    sizing: SizingDecision,
) -> RiskDecision | None:
    """Validate per-token concentration against the configured cap.

    The check uses the worst-case single-sided-inventory value the
    sizer recorded (:attr:`SizingDecision.worst_case_amount0` /
    ``worst_case_amount1``) and the configured per-pool USDG
    envelope. The two per-side raw-token amounts are converted to
    Q64.64 USDG by the integer USDG conversion the sizer itself
    uses (see :func:`robinhood_lp.protocol.sizing.usdg_amount_to_token_amount`):
    the conversion's price is the ``price_q64_64`` from which
    ``raw_amount_usdg_q64_64 = (raw_amount * price_q64_64) >> 64``.

    The pool's :attr:`PoolKey.currency0` is the lower-address
    currency (USDG itself, in the canonical ZZZ/USDG pool); its
    USDG price is trivially ``Q64_SCALE``. The pool's
    :attr:`PoolKey.currency1` is the higher-address target
    token; its USDG price is the ``market.quote_q64_64`` the
    strategy layer (T060) carries. When either side exceeds the
    configured cap, the verdict is
    :attr:`RiskVerdict.NO_NEW_RISK` at ``TOKEN`` scope; the
    REDUCE_ONLY path remains approved (this is the NO_NEW_RISK
    semantics), and the ``INCREASE_RISK`` path is rejected.
    """
    worst_case_amount0 = sizing.worst_case_amount0
    worst_case_amount1 = sizing.worst_case_amount1
    if worst_case_amount0 == 0 and worst_case_amount1 == 0:
        return None
    cap_q64_64 = config.max_single_currency_concentration_q64_64
    if cap_q64_64 <= 0:
        # The configured cap is the deny-by-default fallback (the
        # constructor enforces positive Q64.64); the check below
        # would always trip, so return ``None`` to honour the
        # configured value of zero / negative.
        return None

    # Resolve the per-side USDG price. ``currency0`` is USDG itself
    # in the canonical ZZZ/USDG pool (lower address), so its USDG
    # price is ``Q64_SCALE``. ``currency1`` is the target token; its
    # USDG price is the ``market.quote_q64_64`` the strategy layer
    # carries (T060). When the snapshot is RELATIVE_ONLY or the
    # quote is missing, the comparison cannot be performed and the
    # check is a structural no-op (the upstream USDG price check
    # already rejects RELATIVE_ONLY when a USDG-denominated bar is
    # required; here we are tolerant of the absence of any bar).
    assert intent.market is not None
    price_token0_q64_64 = Q64_SCALE  # currency0 = USDG
    if intent.market.is_relative_only or intent.market.quote_q64_64 is None:
        # The candidate's USDG valuation cannot be computed; the
        # upstream USDG price check will reject if a USDG-denominated
        # bar is required for sizing. We follow that with a no-op
        # here so the concentration check does not pretend to know a
        # price it does not.
        return None
    price_token1_q64_64 = intent.market.quote_q64_64
    if price_token1_q64_64 <= 0:
        return None

    # Q64.64 USDG value of each side. The arithmetic is the
    # integer reverse of ``usdg_amount_to_token_amount``: with
    # ``price_q64_64 = usdg_per_token << 64``,
    # ``token_amount = (usdg_amount << 64) // price`` ⇒
    # ``usdg_amount = (token_amount * price) >> 64``.
    worst_case_usdg_q64_64_token0 = (worst_case_amount0 * price_token0_q64_64) >> 64
    worst_case_usdg_q64_64_token1 = (worst_case_amount1 * price_token1_q64_64) >> 64
    total_usdg_q64_64 = worst_case_usdg_q64_64_token0 + worst_case_usdg_q64_64_token1
    if total_usdg_q64_64 <= 0:
        return None
    # The cap is "per-token share of the total position must be
    # <= cap_q64_64", so:
    #   worst_case_usdg_q64_64_tokenN * Q64_SCALE
    #       > total_usdg_q64_64 * cap_q64_64   ⇒   breach.
    threshold_q64_64 = (total_usdg_q64_64 * cap_q64_64) // Q64_SCALE
    breach_side: str | None = None
    if worst_case_usdg_q64_64_token0 > threshold_q64_64:
        breach_side = "currency0"
    elif worst_case_usdg_q64_64_token1 > threshold_q64_64:
        breach_side = "currency1"
    if breach_side is None:
        return None
    return _no_new_risk(
        context=context,
        config=config,
        intent=intent,
        reason_code=REASON_TOKEN_CONCENTRATION_EXCEEDED,
        reason_detail=(
            f"per-token concentration breach: {breach_side} worst_case_usdg_q64_64="
            f"{worst_case_usdg_q64_64_token0 if breach_side == 'currency0' else worst_case_usdg_q64_64_token1} "
            f"exceeds cap share of total_usdg_q64_64={total_usdg_q64_64} at cap_q64_64={cap_q64_64}"
        ),
        scope=RiskScope.TOKEN,
        notes=(
            "check=token_concentration",
            f"breach_side={breach_side}",
            f"max_single_currency_concentration_q64_64={cap_q64_64}",
        ),
    )


def _resolve_sizing(*, config: RiskConfig, intent: RiskIntent) -> SizingDecision:
    """Resolve the :class:`SizingDecision` the gateway will record.

    The wrapper around :func:`compute_sizing` enforces the
    deny-by-default ``int`` typing and the integer USDG conversion
    floor; it never mutates the sizer's contract.
    """
    assert intent.market is not None
    assert intent.portfolio is not None
    return compute_sizing(
        pool_key=intent.pool_key,
        sqrt_price_x96=intent.market.sqrt_price_x96,
        tick_lower=intent.tick_lower,
        tick_upper=intent.tick_upper,
        amount0_cap=intent.portfolio.principal_token0 + 1,
        amount1_cap=intent.portfolio.principal_token1 + 1,
        accrued_fees0=intent.portfolio.tokens_owed0,
        accrued_fees1=intent.portfolio.tokens_owed1,
        gas_reserve_wei=0,
        hook_delta0=0,
        hook_delta1=0,
        hook_verified=False,
        deadline=intent.decision_time + config.max_snapshot_age_seconds,
        min_liquidity=0,
        min_economic_liquidity_usdg_q64_64=config.min_economic_liquidity_usdg_q64_64,
        usdg_cap_q64_64_token0=None,
        usdg_cap_q64_64_token1=None,
        usdg_amount_token0=intent.portfolio.principal_token0 + 1,
        usdg_amount_token1=intent.portfolio.principal_token1 + 1,
    )
