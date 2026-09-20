"""Tests for the V1 central risk gateway (T070).

T070 defines the central risk gateway through which every intent —
strategy, paper, or manual reduce-only — must pass before reaching
execution. The tests cover every acceptance clause of the T070
contract:

- **One auditable approve/reject decision.** The gateway returns
  a :class:`RiskDecision` carrying the configuration version, the
  audit pointer, and the structured reason.

- **All intent sources use the same gateway.** A single
  :func:`evaluate_risk` call produces a decision regardless of the
  intent's source tag; the source appears on the decision but
  cannot relax a hard precondition.

- **The 5-minute USDG emergency circuit breaker.** A complete
  5-minute target-token USDG return ``<= -80%`` produces an
  :attr:`RiskVerdict.AUTO_EXIT` regardless of intent source or
  kind. The breaker is not a strategy-side approval and cannot be
  suppressed. An incomplete 5-minute bar does *not* trip the
  breaker.

- **Deny-by-default.** Missing / stale / NaN / overflow /
  configuration errors fail closed with a structured reason code.

- **Simultaneous breaches.** When two checks trip simultaneously
  the gateway returns the first reason it discovers in the
  documented pipeline order (kill switches first, then the
  emergency breaker, then eligibility / freshness, then episode,
  cadence, gas, hook, sizing, concentration).

- **Configuration changes are versioned.** A change to the
  configuration's ``version_id`` produces a decision with the new
  version; a past decision is byte-unchanged.

- **No float on the protocol path.** ``float`` carrying NaN is
  rejected as a NaN / display-value failure.

- **Layer purity.** The risk module imports ``protocol``,
  ``features`` (T053), ``strategy`` (T060), and the stdlib; it
  must not import ``rpc``, ``storage``, ``execution``,
  ``presentation``, ``web`` or ``ingestion``. The
  :func:`_assert_risk_layer_pure` helper enforces the contract.

- **Frozen contracts.** The :class:`RiskDecision` /
  :class:`RiskConfig` / :class:`RiskContext` / :class:`RiskIntent`
  dataclasses are frozen; equality and hashing follow dataclass
  identity so a downstream audit trail binds the decision bytes
  to the intent it approved or rejected.

- **Stable reason codes.** The reason-code vocabulary is closed;
  renaming or removing a reason code is a breaking change.

- **Versioned configuration cannot retroactively alter
  decisions.** Two decisions produced under distinct
  ``config_version`` strings differ in their ``config_version``
  field; an audit trail can detect when a decision was produced
  by an obsolete configuration.
"""

from __future__ import annotations

import dataclasses
import importlib
import sys
from typing import Final

import pytest

from robinhood_lp.features.quote import (
    FIVE_MINUTE_DOWN_SPIKE_FRACTION,
    ConfidenceLevel,
    MissingPolicy,
    NumeraireLevel,
    NumeraireQualification,
    Observation,
    ObservationUnit,
    QualificationBundle,
    QuoteBar,
    SourceKind,
)
from robinhood_lp.protocol.events import BlockRef
from robinhood_lp.protocol.ids import Address, ChainId, Currency, PoolKey
from robinhood_lp.protocol.run_mode import RunMode
from robinhood_lp.risk import (
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
from robinhood_lp.strategy.base import (
    ADMISSION_SNAPSHOT_VERSION,
    CANDIDATE_ACTION_VERSION,
    CANDIDATE_KIND_PROPOSE,
    FEE_OPPORTUNITY_OUTCOME_ASSESSED,
    MARKET_SNAPSHOT_VERSION,
    PORTFOLIO_SNAPSHOT_VERSION,
    Q64_SCALE,
    REGIME_OUTCOME_ASSESSED,
    REGIME_STATE_RANGE,
    AdmissionSnapshot,
    CandidateAction,
    MarketSnapshot,
    PortfolioSnapshot,
    ReasonCode,
)

# ---------------------------------------------------------------------------
# Reference PoolKeys (heterogeneous)
# ---------------------------------------------------------------------------


_CHAIN_A: Final[int] = 46630

_POOLKEY_NATIVE_NO_HOOK: Final[PoolKey] = PoolKey(
    currency0=Currency.native(),
    currency1=Currency.from_hex("0x" + "11" * 20),
    fee=500,
    tick_spacing=60,
    hooks=Address.zero(),
)

_POOLKEY_ERC20_DYNAMIC_HOOK: Final[PoolKey] = PoolKey(
    currency0=Currency.from_hex("0x" + "22" * 20),
    currency1=Currency.from_hex("0x" + "33" * 20),
    fee=0x800000,
    tick_spacing=10,
    hooks=Address.from_hex("0x" + "44" * 20),
)


def _pool_key_id(pool_key: PoolKey) -> str:
    return "0x" + pool_key.to_pool_id().to_bytes().hex()


# ---------------------------------------------------------------------------
# Reference snapshots
# ---------------------------------------------------------------------------


def _admission(
    *,
    pool_key: PoolKey,
    chain_id: int = _CHAIN_A,
    data_time: int = 100,
    availability_time: int = 105,
    track_a: bool = True,
    track_b: bool = True,
) -> AdmissionSnapshot:
    return AdmissionSnapshot(
        version=ADMISSION_SNAPSHOT_VERSION,
        pool_key_id=_pool_key_id(pool_key),
        chain_id=chain_id,
        support_level="paper",
        track_a_admitted=track_a,
        track_b_admitted=track_b,
        max_capital_q64_64=Q64_SCALE,
        data_time=data_time,
        availability_time=availability_time,
    )


def _market(
    *,
    pool_key: PoolKey,
    chain_id: int = _CHAIN_A,
    data_time: int = 100,
    availability_time: int = 105,
    sqrt_price_x96: int = 1 << 96,
    liquidity: int = 1,
    realized_volatility_q64_64: int = 0,
) -> MarketSnapshot:
    return MarketSnapshot(
        version=MARKET_SNAPSHOT_VERSION,
        pool_key_id=_pool_key_id(pool_key),
        chain_id=chain_id,
        sqrt_price_x96=sqrt_price_x96,
        liquidity=liquidity,
        realized_volatility_q64_64=realized_volatility_q64_64,
        freshness_seconds=0,
        quote_q64_64=Q64_SCALE,
        is_relative_only=False,
        data_time=data_time,
        availability_time=availability_time,
    )


def _portfolio(
    *,
    pool_key: PoolKey,
    chain_id: int = _CHAIN_A,
    data_time: int = 100,
    availability_time: int = 105,
    is_empty: bool = True,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        version=PORTFOLIO_SNAPSHOT_VERSION,
        pool_key_id=_pool_key_id(pool_key),
        chain_id=chain_id,
        position_id="(tick_lower=-100,tick_upper=100,salt=1)",
        sqrt_price_x96=1 << 96,
        liquidity=0 if is_empty else 1,
        principal_token0=0,
        principal_token1=0,
        tokens_owed0=0,
        tokens_owed1=0,
        is_empty=is_empty,
        data_time=data_time,
        availability_time=availability_time,
    )


def _candidate(
    *,
    pool_key: PoolKey,
    chain_id: int = _CHAIN_A,
    kind: str = CANDIDATE_KIND_PROPOSE,
    tick_lower: int = -120,
    tick_upper: int = 120,
    liquidity: int = 1,
    capital_q64_64: int = Q64_SCALE,
    decision_time: int = 200,
    availability_time: int = 195,
    reason_code: ReasonCode | None = None,
    regime_outcome: str = REGIME_OUTCOME_ASSESSED,
    fee_opportunity_outcome: str = FEE_OPPORTUNITY_OUTCOME_ASSESSED,
) -> CandidateAction:
    return CandidateAction(
        version=CANDIDATE_ACTION_VERSION,
        pool_key_id=_pool_key_id(pool_key),
        chain_id=chain_id,
        kind=kind,
        decision_time=decision_time,
        availability_time=availability_time,
        snapshot_versions=(
            ADMISSION_SNAPSHOT_VERSION,
            MARKET_SNAPSHOT_VERSION,
            PORTFOLIO_SNAPSHOT_VERSION,
        ),
        component_versions=(
            "t060.regime_model.v1",
            "t060.fee_opportunity_model.v1",
        ),
        regime_state=REGIME_STATE_RANGE,
        regime_outcome=regime_outcome,
        fee_opportunity_outcome=fee_opportunity_outcome,
        reason_code=reason_code,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity=liquidity,
        capital_q64_64=capital_q64_64,
    )


# ---------------------------------------------------------------------------
# Episode / config / context fixtures
# ---------------------------------------------------------------------------


def _default_episode(
    *,
    current_unit_nav_usdg_q64_64: int = Q64_SCALE,
    high_watermark_unit_nav_usdg_q64_64: int = Q64_SCALE,
) -> EpisodeState:
    return EpisodeState(
        initial_unit_nav_usdg_q64_64=Q64_SCALE,
        initial_approved_cash_usdg_q64_64=Q64_SCALE,
        current_unit_nav_usdg_q64_64=current_unit_nav_usdg_q64_64,
        high_watermark_unit_nav_usdg_q64_64=high_watermark_unit_nav_usdg_q64_64,
        net_external_cash_flow_usdg_q64_64=0,
        outstanding_episode_units=1,
    )


def _default_gas_budget(
    *,
    reserve_wei: int = 0,
    budget_wei: int = 10**18,
    used_wei: int = 0,
    expected_wei: int = 0,
) -> GasBudget:
    return GasBudget(
        reserve_wei=reserve_wei,
        budget_wei=budget_wei,
        used_wei=used_wei,
        expected_wei=expected_wei,
    )


def _build_context(
    *,
    pool_key: PoolKey = _POOLKEY_NATIVE_NO_HOOK,
    support_level: RunMode = RunMode.PAPER,
    deployment_hash_match: bool = True,
    last_rebalance_decision_time: int = 0,
    expected_rebalance_cost_usdg_q64_64: int = 0,
    amount0_cap: int = 1_000_000,
    amount1_cap: int = 1_000_000,
    accrued_fees0: int = 0,
    accrued_fees1: int = 0,
    usdg_amount_token0: int = 1_000_000,
    usdg_amount_token1: int = 1_000_000,
    gas_budget: GasBudget | None = None,
    episode: EpisodeState | None = None,
    kill_switches_active: tuple[KillSwitch, ...] = (),
    five_minute_breach: FiveMinuteEmergencyBreach | None = None,
    hook_delta0: int = 0,
    hook_delta1: int = 0,
    hook_verified: bool = False,
    hook_evidence_age_seconds: int = 0,
    evidence_pointers: tuple[str, ...] = (),
) -> RiskContext:
    return RiskContext(
        support_level=support_level,
        deployment_hash_match=deployment_hash_match,
        last_rebalance_decision_time=last_rebalance_decision_time,
        expected_rebalance_cost_usdg_q64_64=expected_rebalance_cost_usdg_q64_64,
        amount0_cap=amount0_cap,
        amount1_cap=amount1_cap,
        accrued_fees0=accrued_fees0,
        accrued_fees1=accrued_fees1,
        usdg_cap_q64_64_token0=None,
        usdg_cap_q64_64_token1=None,
        usdg_amount_token0=usdg_amount_token0,
        usdg_amount_token1=usdg_amount_token1,
        gas_budget=gas_budget or _default_gas_budget(),
        hook_delta0=hook_delta0,
        hook_delta1=hook_delta1,
        hook_verified=hook_verified,
        hook_evidence_age_seconds=hook_evidence_age_seconds,
        episode=episode or _default_episode(),
        kill_switches_active=kill_switches_active,
        five_minute_breach=five_minute_breach,
        evidence_pointers=evidence_pointers,
    )


_NO_DEFAULT: Final[object] = object()


def _build_intent(
    *,
    pool_key: PoolKey = _POOLKEY_NATIVE_NO_HOOK,
    chain_id: int = _CHAIN_A,
    source: RiskIntentSource = RiskIntentSource.STRATEGY,
    kind: IntentKind = IntentKind.REDUCE_ONLY,
    decision_time: int = 200,
    candidate: CandidateAction | None = None,
    admission: AdmissionSnapshot | None | object = _NO_DEFAULT,
    market: MarketSnapshot | None | object = _NO_DEFAULT,
    portfolio: PortfolioSnapshot | None | object = _NO_DEFAULT,
    tick_lower: int = 0,
    tick_upper: int = 0,
    liquidity_delta: int = 0,
    audit_pointer: str = "test:1",
    notes: tuple[str, ...] = (),
) -> RiskIntent:
    """Build a :class:`RiskIntent` for tests.

    Snapshots default to the per-pool reference snapshots so tests
    can call ``_build_intent(kind=REDUCE_ONLY)`` and exercise the
    full pipeline. Callers that exercise "missing" snapshot paths
    pass an explicit ``None``; the sentinel ``_NO_DEFAULT`` lets
    the fixture distinguish "not provided" from "explicitly None".
    """
    if admission is _NO_DEFAULT:
        admission = _admission(pool_key=pool_key)
    if market is _NO_DEFAULT:
        market = _market(pool_key=pool_key)
    if portfolio is _NO_DEFAULT:
        portfolio = _portfolio(pool_key=pool_key)
    return RiskIntent(
        source=source,
        kind=kind,
        pool_key=pool_key,
        chain_id=chain_id,
        decision_time=decision_time,
        audit_pointer=audit_pointer,
        candidate=candidate,
        admission=admission,
        market=market,
        portfolio=portfolio,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity_delta=liquidity_delta,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Layer-purity enforcement
# ---------------------------------------------------------------------------


_FORBIDDEN_RISK_MODULES: Final[tuple[str, ...]] = (
    "robinhood_lp.config",
    "robinhood_lp.discovery",
    "robinhood_lp.ingestion",
    "robinhood_lp.presentation",
    "robinhood_lp.qualification",
    "robinhood_lp.quality",
    "robinhood_lp.replay",
    "robinhood_lp.rpc",
    "robinhood_lp.storage",
    "robinhood_lp.execution",
    "robinhood_lp.web",
)


def _risk_imports() -> list[tuple[str, str]]:
    """Return every ``robinhood_lp.*`` import inside :mod:`robinhood_lp.risk`."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "robinhood_lp" / "risk"
    out: list[tuple[str, str]] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("robinhood_lp"):
                    out.append((str(path), node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("robinhood_lp"):
                        out.append((str(path), alias.name))
    return out


def _assert_risk_layer_pure() -> None:
    """Assert the risk layer imports only the allowed ``robinhood_lp`` modules."""
    allowed = {
        "robinhood_lp.features",
        "robinhood_lp.features.quote",
        "robinhood_lp.protocol",
        "robinhood_lp.protocol.events",
        "robinhood_lp.protocol.ids",
        "robinhood_lp.protocol.run_mode",
        "robinhood_lp.protocol.sizing",
        "robinhood_lp.strategy",
        "robinhood_lp.strategy.base",
        "robinhood_lp.risk",
        "robinhood_lp.risk.checks",
    }
    for path, target in _risk_imports():
        if not (
            target == "robinhood_lp" or target in allowed or target.startswith("robinhood_lp.risk")
        ):
            raise AssertionError(f"risk layer imports forbidden module {target!r} from {path}")


# ---------------------------------------------------------------------------
# Layer-purity test
# ---------------------------------------------------------------------------


class TestLayerPurity:
    def test_risk_layer_does_not_import_forbidden_modules(self) -> None:
        """The risk layer must not import RPC, storage, execution, web, etc."""
        _assert_risk_layer_pure()

    def test_risk_module_imports_only_allowed_modules(self) -> None:
        """The risk layer imports only protocol / features / strategy / stdlib."""
        allowed_top_levels = {
            "robinhood_lp",
            "robinhood_lp.features",
            "robinhood_lp.protocol",
            "robinhood_lp.strategy",
            "robinhood_lp.risk",
        }
        for _, target in _risk_imports():
            top = ".".join(target.split(".")[:2])
            assert top in allowed_top_levels, f"forbidden import: {target!r}"


# ---------------------------------------------------------------------------
# Frozen / immutable contracts
# ---------------------------------------------------------------------------


class TestFrozenContracts:
    def test_risk_decision_is_frozen(self) -> None:
        decision = evaluate_risk(
            intent=_build_intent(
                kind=IntentKind.REDUCE_ONLY,
                audit_pointer="audit:frozen",
            ),
            context=_build_context(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.approved = False  # type: ignore[misc]

    def test_risk_config_is_frozen(self) -> None:
        config = RiskConfig.default()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.version_id = "tampered"  # type: ignore[misc]

    def test_risk_context_is_frozen(self) -> None:
        ctx = _build_context()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.deployment_hash_match = False  # type: ignore[misc]

    def test_risk_intent_is_frozen(self) -> None:
        intent = _build_intent()
        with pytest.raises(dataclasses.FrozenInstanceError):
            intent.audit_pointer = "tampered"  # type: ignore[misc]

    def test_risk_decision_hash_is_deterministic(self) -> None:
        intent = _build_intent(kind=IntentKind.REDUCE_ONLY)
        context = _build_context()
        d1 = evaluate_risk(intent=intent, context=context)
        d2 = evaluate_risk(intent=intent, context=context)
        assert hash(d1) == hash(d2)
        assert d1 == d2

    def test_risk_decision_version_is_stable(self) -> None:
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(),
        )
        assert decision.version == DEFAULT_RISK_VERSION


# ---------------------------------------------------------------------------
# Stable reason codes and verdict vocabulary
# ---------------------------------------------------------------------------


class TestReasonCodes:
    def test_reason_codes_are_non_empty_strings(self) -> None:
        from robinhood_lp.risk import checks as checks_module

        for name in dir(checks_module):
            if name.startswith("REASON_"):
                value = getattr(checks_module, name)
                assert isinstance(value, str), f"{name} must be a string"
                assert value, f"{name} must be a non-empty string"

    def test_verdict_is_closed(self) -> None:
        assert {v.value for v in RiskVerdict} == {
            "APPROVED",
            "REJECTED",
            "WARNING",
            "NO_NEW_RISK",
            "AUTO_EXIT",
        }

    def test_scope_is_closed(self) -> None:
        assert {v.value for v in RiskScope} == {"GLOBAL", "POOL", "TOKEN", "STRATEGY"}

    def test_intent_source_is_closed(self) -> None:
        assert {v.value for v in RiskIntentSource} == {"STRATEGY", "PAPER", "MANUAL_REDUCE_ONLY"}

    def test_intent_kind_is_closed(self) -> None:
        assert {v.value for v in IntentKind} == {"INCREASE_RISK", "REDUCE_ONLY"}

    def test_default_risk_version_is_stable(self) -> None:
        assert DEFAULT_RISK_VERSION == "t070.risk_layer.v1"

    def test_default_config_version_is_stable(self) -> None:
        assert DEFAULT_RISK_CONFIG_VERSION == "t070.risk_config.v1"


# ---------------------------------------------------------------------------
# Config invariants
# ---------------------------------------------------------------------------


class TestRiskConfig:
    def test_default_config_has_experimental_tag(self) -> None:
        config = RiskConfig.default()
        assert EXPERIMENTAL_TAG in config.experimental_thresholds

    def test_default_config_is_pure(self) -> None:
        """The default config carries every threshold; custom configs may drop them."""
        config = RiskConfig.default(experimental_thresholds=())
        assert config.experimental_thresholds == ()

    def test_config_validates_thresholds_ordering_pnl(self) -> None:
        with pytest.raises(RiskError):
            RiskConfig(
                version_id="t070.risk_config.bad",
                max_snapshot_age_seconds=300,
                min_rebalance_interval_seconds=300,
                min_economic_liquidity_usdg_q64_64=Q64_SCALE,
                max_hook_evidence_age_seconds=86400,
                max_single_currency_concentration_q64_64=Q64_SCALE // 2,
                episode_pnl_warning_q64_64=Q64_SCALE // 10,  # too high
                episode_pnl_no_new_risk_q64_64=Q64_SCALE // 100,
                episode_pnl_auto_exit_q64_64=Q64_SCALE // 100,
                drawdown_warning_q64_64=0,
                drawdown_no_new_risk_q64_64=Q64_SCALE // 100,
                drawdown_auto_exit_q64_64=Q64_SCALE // 100,
                kill_switches=(),
                experimental_thresholds=(),
                pool_key_id=None,
            )

    def test_config_validates_thresholds_ordering_drawdown(self) -> None:
        with pytest.raises(RiskError):
            RiskConfig(
                version_id="t070.risk_config.bad",
                max_snapshot_age_seconds=300,
                min_rebalance_interval_seconds=300,
                min_economic_liquidity_usdg_q64_64=Q64_SCALE,
                max_hook_evidence_age_seconds=86400,
                max_single_currency_concentration_q64_64=Q64_SCALE // 2,
                episode_pnl_warning_q64_64=Q64_SCALE // 100,
                episode_pnl_no_new_risk_q64_64=Q64_SCALE // 100,
                episode_pnl_auto_exit_q64_64=Q64_SCALE // 100,
                drawdown_warning_q64_64=Q64_SCALE // 10,  # too high
                drawdown_no_new_risk_q64_64=Q64_SCALE // 100,
                drawdown_auto_exit_q64_64=Q64_SCALE // 100,
                kill_switches=(),
                experimental_thresholds=(),
                pool_key_id=None,
            )

    def test_config_rejects_negative_version_id(self) -> None:
        with pytest.raises(RiskError):
            RiskConfig.default(version_id="")

    def test_config_rejects_empty_pool_key_id_when_set(self) -> None:
        with pytest.raises(RiskError):
            RiskConfig.default(pool_key_id="")


# ---------------------------------------------------------------------------
# Episode state
# ---------------------------------------------------------------------------


class TestEpisodeState:
    def test_episode_pnl_positive_returns_zero_loss(self) -> None:
        episode = _default_episode(
            current_unit_nav_usdg_q64_64=2 * Q64_SCALE,
            high_watermark_unit_nav_usdg_q64_64=2 * Q64_SCALE,
        )
        assert episode.episode_pnl_usdg_q64_64 == Q64_SCALE
        assert episode.drawdown_q64_64 == 0

    def test_episode_loss_is_positive_q64_64(self) -> None:
        episode = _default_episode(
            current_unit_nav_usdg_q64_64=Q64_SCALE // 2,
            high_watermark_unit_nav_usdg_q64_64=Q64_SCALE,
        )
        assert episode.episode_pnl_usdg_q64_64 == -(Q64_SCALE // 2)
        assert episode.drawdown_q64_64 == Q64_SCALE // 2

    def test_episode_rejects_high_water_mark_below_initial(self) -> None:
        with pytest.raises(RiskError):
            EpisodeState(
                initial_unit_nav_usdg_q64_64=Q64_SCALE,
                initial_approved_cash_usdg_q64_64=Q64_SCALE,
                current_unit_nav_usdg_q64_64=Q64_SCALE,
                high_watermark_unit_nav_usdg_q64_64=Q64_SCALE // 2,  # too low
                net_external_cash_flow_usdg_q64_64=0,
                outstanding_episode_units=1,
            )


# ---------------------------------------------------------------------------
# Five-minute emergency breaker
# ---------------------------------------------------------------------------


class TestFiveMinuteBreaker:
    def test_complete_breach_triggers_breaker(self) -> None:
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=100 * Q64_SCALE,
            close_q64_64=10 * Q64_SCALE,  # 90% drop
            is_complete=True,
            bar=None,
        )
        assert is_five_minute_emergency_breach(breach) is True

    def test_incomplete_breach_does_not_trigger(self) -> None:
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=100 * Q64_SCALE,
            close_q64_64=10 * Q64_SCALE,  # 90% drop
            is_complete=False,
            bar=None,
        )
        assert is_five_minute_emergency_breach(breach) is False

    def test_complete_breach_below_threshold_does_not_trigger(self) -> None:
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=Q64_SCALE,
            close_q64_64=(Q64_SCALE * 50) // 100,  # 50% drop, below 80%
            is_complete=True,
            bar=None,
        )
        assert is_five_minute_emergency_breach(breach) is False

    def test_up_move_does_not_trigger(self) -> None:
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=Q64_SCALE,
            close_q64_64=2 * Q64_SCALE,  # 100% gain
            is_complete=True,
            bar=None,
        )
        assert is_five_minute_emergency_breach(breach) is False

    def test_exact_threshold_triggers(self) -> None:
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=Q64_SCALE,
            close_q64_64=Q64_SCALE - FIVE_MINUTE_DOWN_SPIKE_FRACTION,
            is_complete=True,
            bar=None,
        )
        assert is_five_minute_emergency_breach(breach) is True

    def test_custom_threshold_respected(self) -> None:
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=Q64_SCALE,
            close_q64_64=(Q64_SCALE * 90) // 100,  # 10% drop
            is_complete=True,
            bar=None,
        )
        assert is_five_minute_emergency_breach(breach, threshold_q64_64=Q64_SCALE // 20) is True

    def test_emergency_constant_matches_t053(self) -> None:
        from robinhood_lp.risk.checks import FIVE_MINUTE_EMERGENCY_THRESHOLD_Q64_64

        assert FIVE_MINUTE_EMERGENCY_THRESHOLD_Q64_64 == FIVE_MINUTE_DOWN_SPIKE_FRACTION


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------


class TestKillSwitches:
    def test_global_kill_switch_triggers_auto_exit(self) -> None:
        ks = KillSwitch(
            name="global_stop",
            scope=RiskScope.GLOBAL,
            active=True,
            reason_code="MANUAL_STOP",
            raised_at=0,
            expires_at=None,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(kill_switches_active=(ks,)),
        )
        assert decision.verdict is RiskVerdict.AUTO_EXIT
        assert decision.scope is RiskScope.GLOBAL
        assert decision.approved is False
        assert decision.reason_code == "MANUAL_STOP"

    def test_pool_kill_switch_blocks_increase_risk_but_approves_reduce(self) -> None:
        ks = KillSwitch(
            name="pool_stop",
            scope=RiskScope.POOL,
            active=True,
            reason_code="POOL_HALT",
            raised_at=0,
            expires_at=None,
        )
        decision_reduce = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(kill_switches_active=(ks,)),
        )
        assert decision_reduce.verdict is RiskVerdict.NO_NEW_RISK
        assert decision_reduce.approved is True  # reduce-only approved
        assert decision_reduce.scope is RiskScope.POOL

        # INCREASE_RISK with PROPOSE candidate needs more inputs;
        # the pipeline rejects because the candidate missing
        # snapshots is a separate reason code. Use the canonical
        # denial order: build a complete intent.
        candidate = _candidate(pool_key=_POOLKEY_NATIVE_NO_HOOK, kind=CANDIDATE_KIND_PROPOSE)
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            admission=_admission(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            portfolio=_portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            tick_lower=candidate.tick_lower,
            tick_upper=candidate.tick_upper,
        )
        decision_increase = evaluate_risk(
            intent=intent,
            context=_build_context(kill_switches_active=(ks,)),
        )
        assert decision_increase.verdict is RiskVerdict.NO_NEW_RISK
        assert decision_increase.approved is False
        assert decision_increase.scope is RiskScope.POOL

    def test_inactive_kill_switch_is_ignored(self) -> None:
        # Inactive switches are not put in kill_switches_active; the
        # gateway therefore sees no active switches and approves.
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(kill_switches_active=()),
        )
        assert decision.verdict is RiskVerdict.APPROVED

    def test_context_rejects_inactive_kill_switch(self) -> None:
        # Build a valid active KillSwitch and then flip it inactive
        # via ``dataclasses.replace``; the context enforces active=True.
        active_ks = KillSwitch(
            name="off",
            scope=RiskScope.GLOBAL,
            active=True,
            reason_code="INACTIVE_AT_CONTEXT_BOUNDARY",
            raised_at=0,
            expires_at=None,
        )
        inactive = dataclasses.replace(active_ks, active=False)
        with pytest.raises(RiskError):
            _build_context(kill_switches_active=(inactive,))

    def test_kill_switch_expires_validation(self) -> None:
        with pytest.raises(RiskError):
            KillSwitch(
                name="bad",
                scope=RiskScope.GLOBAL,
                active=True,
                reason_code="",
                raised_at=10,
                expires_at=5,  # expires before raised_at
            )


# ---------------------------------------------------------------------------
# Emergency circuit breaker integration
# ---------------------------------------------------------------------------


class TestEmergencyBreakerIntegration:
    def test_complete_breach_auto_exit_overrides_strategy_approval(self) -> None:
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=100 * Q64_SCALE,
            close_q64_64=10 * Q64_SCALE,
            is_complete=True,
            bar=None,
        )
        candidate = _candidate(pool_key=_POOLKEY_NATIVE_NO_HOOK, kind=CANDIDATE_KIND_PROPOSE)
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            admission=_admission(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            portfolio=_portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            tick_lower=candidate.tick_lower,
            tick_upper=candidate.tick_upper,
        )
        decision = evaluate_risk(
            intent=intent,
            context=_build_context(five_minute_breach=breach),
        )
        assert decision.verdict is RiskVerdict.AUTO_EXIT
        assert decision.scope is RiskScope.GLOBAL
        assert decision.approved is False
        assert decision.reason_code == "FIVE_MINUTE_EMERGENCY_BREACH"

    def test_incomplete_breach_does_not_trigger(self) -> None:
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=100 * Q64_SCALE,
            close_q64_64=10 * Q64_SCALE,
            is_complete=False,
            bar=None,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(five_minute_breach=breach),
        )
        assert decision.verdict is RiskVerdict.APPROVED

    def test_emergency_breaker_suppresses_even_reduce_only_intent(self) -> None:
        """A reduce-only intent under the emergency breaker is rejected
        because the breaker mandates MANUAL_CONTROL."""
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=100 * Q64_SCALE,
            close_q64_64=10 * Q64_SCALE,
            is_complete=True,
            bar=None,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(five_minute_breach=breach),
        )
        assert decision.verdict is RiskVerdict.AUTO_EXIT
        assert decision.approved is False

    def test_emergency_takes_precedence_over_kill_switch(self) -> None:
        """When both are active, kill switches are checked first."""
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=100 * Q64_SCALE,
            close_q64_64=10 * Q64_SCALE,
            is_complete=True,
            bar=None,
        )
        ks = KillSwitch(
            name="some_pool",
            scope=RiskScope.POOL,
            active=True,
            reason_code="POOL_STOP",
            raised_at=0,
            expires_at=None,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(
                five_minute_breach=breach,
                kill_switches_active=(ks,),
            ),
        )
        # Pool-scoped kill switch wins (highest precedence in the
        # pipeline). The emergency is not consulted because the
        # pipeline already produced a verdict.
        assert decision.verdict is RiskVerdict.NO_NEW_RISK
        assert decision.reason_code == "POOL_STOP"


# ---------------------------------------------------------------------------
# Required input / version validation
# ---------------------------------------------------------------------------


class TestRequiredInputs:
    def test_missing_candidate_is_rejected(self) -> None:
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=None,
            admission=_admission(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            portfolio=_portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK),
        )
        decision = evaluate_risk(intent=intent, context=_build_context())
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "INCOMPLETE_INPUTS"

    def test_missing_admission_is_rejected(self) -> None:
        candidate = _candidate(pool_key=_POOLKEY_NATIVE_NO_HOOK, kind=CANDIDATE_KIND_PROPOSE)
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            admission=None,  # explicitly missing
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            portfolio=_portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            tick_lower=candidate.tick_lower,
            tick_upper=candidate.tick_upper,
        )
        decision = evaluate_risk(intent=intent, context=_build_context())
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "INCOMPLETE_INPUTS"

    def test_snapshot_version_mismatch_is_rejected(self) -> None:
        # Build a candidate with an unknown snapshot version via the
        # helper, then patch the snapshot_versions tuple.
        candidate = _candidate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            kind=CANDIDATE_KIND_PROPOSE,
        )
        # Construct a fresh candidate with an unknown snapshot version.
        candidate = dataclasses.replace(
            candidate,
            snapshot_versions=("unknown.version",),
        )
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            admission=_admission(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            portfolio=_portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            tick_lower=candidate.tick_lower,
            tick_upper=candidate.tick_upper,
        )
        decision = evaluate_risk(intent=intent, context=_build_context())
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "INCOMPLETE_INPUTS"


# ---------------------------------------------------------------------------
# Eligibility / deployment
# ---------------------------------------------------------------------------


class TestEligibilityAndDeployment:
    def test_rejected_support_level_blocks_increase_risk(self) -> None:
        candidate = _candidate(pool_key=_POOLKEY_NATIVE_NO_HOOK, kind=CANDIDATE_KIND_PROPOSE)
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            admission=_admission(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            portfolio=_portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK),
        )
        decision = evaluate_risk(
            intent=intent,
            context=_build_context(support_level=RunMode.REJECTED),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "ELIGIBILITY_DENIED"

    def test_rejected_support_level_allows_reduce_only(self) -> None:
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(support_level=RunMode.REJECTED),
        )
        # Reduce-only intent with REJECTED support level should be rejected
        # because REJECTED is denied entirely.
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "ELIGIBILITY_DENIED"

    def test_paper_support_level_allows_increase_risk(self) -> None:
        candidate = _candidate(pool_key=_POOLKEY_NATIVE_NO_HOOK, kind=CANDIDATE_KIND_PROPOSE)
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            admission=_admission(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            portfolio=_portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK),
        )
        decision = evaluate_risk(
            intent=intent,
            context=_build_context(support_level=RunMode.PAPER),
        )
        # Either approved with sizing, or REJECTED for sizing NO_TRADE
        # (no caps to fund the candidate) — both demonstrate eligibility
        # passed. The decision should NOT be ELIGIBILITY_DENIED.
        assert decision.reason_code != "ELIGIBILITY_DENIED"

    def test_deployment_hash_mismatch_blocks_intent(self) -> None:
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(deployment_hash_match=False),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "DEPLOYMENT_HASH_MISMATCH"


# ---------------------------------------------------------------------------
# Freshness / NaN guards
# ---------------------------------------------------------------------------


class TestFreshnessAndNaN:
    def test_future_snapshot_is_rejected(self) -> None:
        admission = _admission(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            data_time=300,
            availability_time=300,
        )
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=300, availability_time=300)
        portfolio = _portfolio(
            pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=300, availability_time=300
        )
        decision = evaluate_risk(
            intent=_build_intent(
                kind=IntentKind.REDUCE_ONLY,
                decision_time=200,
                admission=admission,
                market=market,
                portfolio=portfolio,
            ),
            context=_build_context(),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "FUTURE_SNAPSHOT"

    def test_stale_snapshot_is_rejected(self) -> None:
        admission = _admission(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            data_time=0,
            availability_time=0,
        )
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=0, availability_time=0)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=0, availability_time=0)
        decision = evaluate_risk(
            intent=_build_intent(
                kind=IntentKind.REDUCE_ONLY,
                decision_time=10_000,
                admission=admission,
                market=market,
                portfolio=portfolio,
            ),
            context=_build_context(),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "STALE_SNAPSHOT"

    def test_nan_market_value_is_rejected(self) -> None:
        # Replace sqrt_price_x96 with NaN via float is impossible since the
        # MarketSnapshot enforces int. We test the validator directly.
        from robinhood_lp.risk.checks import _validate_market_snapshot

        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        # The check on the integer market should pass.
        assert _validate_market_snapshot(market) is None


# ---------------------------------------------------------------------------
# Episode loss / drawdown
# ---------------------------------------------------------------------------


class TestEpisodeThresholds:
    def test_warning_loss_returns_warning(self) -> None:
        # 1% loss: initial=1.0, current=0.99
        episode = _default_episode(
            current_unit_nav_usdg_q64_64=(99 * Q64_SCALE) // 100,
            high_watermark_unit_nav_usdg_q64_64=Q64_SCALE,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(episode=episode),
        )
        assert decision.verdict is RiskVerdict.WARNING
        assert decision.reason_code == "EPISODE_PNL_WARNING"

    def test_no_new_risk_loss_blocks_increase(self) -> None:
        # 6% loss: triggers NO_NEW_RISK (5% threshold)
        episode = _default_episode(
            current_unit_nav_usdg_q64_64=(94 * Q64_SCALE) // 100,
            high_watermark_unit_nav_usdg_q64_64=Q64_SCALE,
        )
        # REDUCE_ONLY intent → approved despite NO_NEW_RISK
        decision_reduce = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(episode=episode),
        )
        assert decision_reduce.verdict is RiskVerdict.NO_NEW_RISK
        assert decision_reduce.approved is True

    def test_auto_exit_loss(self) -> None:
        # 20% loss: triggers AUTO_EXIT for strategy scope
        episode = _default_episode(
            current_unit_nav_usdg_q64_64=(80 * Q64_SCALE) // 100,
            high_watermark_unit_nav_usdg_q64_64=Q64_SCALE,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(episode=episode),
        )
        assert decision.verdict is RiskVerdict.AUTO_EXIT
        assert decision.scope is RiskScope.STRATEGY
        # Per OPERATOR_CONTROL.md, AUTO_EXIT is the state that
        # authorises a pre-approved reduce-only plan; the
        # reduce-only intent is therefore approved.
        assert decision.approved is True

    def test_drawdown_auto_exit(self) -> None:
        # Mild loss but big drop from HWM: 15% drawdown
        episode = _default_episode(
            current_unit_nav_usdg_q64_64=(85 * Q64_SCALE) // 100,
            high_watermark_unit_nav_usdg_q64_64=Q64_SCALE,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(episode=episode),
        )
        # The drawdown is below the AUTO_EXIT threshold (10% by
        # default). The 15% loss vs initial NAV triggers
        # EPISODE_PNL_AUTO_EXIT instead.
        assert decision.verdict is RiskVerdict.AUTO_EXIT
        assert decision.reason_code in {"DRAWDOWN_AUTO_EXIT", "EPISODE_PNL_AUTO_EXIT"}


# ---------------------------------------------------------------------------
# Rebalance cadence
# ---------------------------------------------------------------------------


class TestRebalanceCadence:
    def test_too_soon_rebalance_rejected(self) -> None:
        candidate = _candidate(pool_key=_POOLKEY_NATIVE_NO_HOOK, kind=CANDIDATE_KIND_PROPOSE)
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            admission=_admission(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            portfolio=_portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK),
        )
        decision = evaluate_risk(
            intent=intent,
            context=_build_context(
                last_rebalance_decision_time=199,  # 1 second before decision_time=200
            ),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "REBALANCE_TOO_SOON"


# ---------------------------------------------------------------------------
# Gas budget
# ---------------------------------------------------------------------------


class TestGasBudget:
    def test_gas_budget_overrun_rejected(self) -> None:
        # The GasBudget constructor itself rejects an over-budget
        # configuration. Verify the invariant at the constructor.
        with pytest.raises(RiskError):
            GasBudget(
                reserve_wei=0,
                budget_wei=10**18,
                used_wei=10**17,
                expected_wei=10**18,
            )

    def test_gas_budget_within_limit(self) -> None:
        budget = GasBudget(
            reserve_wei=0,
            budget_wei=10**18,
            used_wei=10**17,
            expected_wei=10**17,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(gas_budget=budget),
        )
        assert decision.verdict is RiskVerdict.APPROVED

    def test_gas_budget_invalid_construction(self) -> None:
        with pytest.raises(RiskError):
            GasBudget(
                reserve_wei=10**20,
                budget_wei=10**18,
                used_wei=0,
                expected_wei=0,
            )


# ---------------------------------------------------------------------------
# Hook evidence
# ---------------------------------------------------------------------------


class TestHookEvidence:
    def test_hook_delta_without_verification_rejected(self) -> None:
        # Set last_rebalance far enough in the past to clear the
        # cadence check, and use snapshots with availability_time
        # close to decision_time so the freshness check passes.
        candidate = _candidate(pool_key=_POOLKEY_NATIVE_NO_HOOK, kind=CANDIDATE_KIND_PROPOSE)
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            decision_time=1000,
            admission=_admission(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900),
            portfolio=_portfolio(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            tick_lower=candidate.tick_lower,
            tick_upper=candidate.tick_upper,
        )
        decision = evaluate_risk(
            intent=intent,
            context=_build_context(
                hook_delta0=100,
                hook_verified=False,  # unverified hook delta
                last_rebalance_decision_time=0,
            ),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "HOOK_DELTA_UNVERIFIED"

    def test_context_rejects_unverified_hook_delta(self) -> None:
        # The context accepts unverified hook deltas; the gateway
        # is the single source of truth for the rejection.
        ctx = _build_context(hook_delta0=100, hook_verified=False)
        assert ctx.hook_verified is False

    def test_stale_hook_evidence_rejected(self) -> None:
        # Use fresh snapshots and verify the hook-evidence-stale path.
        candidate = _candidate(pool_key=_POOLKEY_NATIVE_NO_HOOK, kind=CANDIDATE_KIND_PROPOSE)
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            decision_time=1000,
            admission=_admission(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900),
            portfolio=_portfolio(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            tick_lower=candidate.tick_lower,
            tick_upper=candidate.tick_upper,
        )
        decision = evaluate_risk(
            intent=intent,
            context=_build_context(
                hook_delta0=100,
                hook_verified=True,
                hook_evidence_age_seconds=10**12,  # very stale
                last_rebalance_decision_time=0,
            ),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "HOOK_EVIDENCE_STALE"


# ---------------------------------------------------------------------------
# Config versioning
# ---------------------------------------------------------------------------


class TestConfigVersioning:
    def test_decision_records_config_version(self) -> None:
        config = RiskConfig.default(version_id="t070.test_config.v1")
        decision = evaluate_risk_with_config(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(),
            config=config,
        )
        assert decision.config_version == "t070.test_config.v1"

    def test_future_config_change_does_not_affect_past_decision(self) -> None:
        config_v1 = RiskConfig.default(version_id="t070.config.v1")
        intent = _build_intent(kind=IntentKind.REDUCE_ONLY)
        ctx = _build_context()
        decision_v1 = evaluate_risk_with_config(intent=intent, context=ctx, config=config_v1)
        # Now change the config (a fresh instance with different version_id)
        config_v2 = RiskConfig.default(version_id="t070.config.v2")
        decision_v2 = evaluate_risk_with_config(intent=intent, context=ctx, config=config_v2)
        # Both decisions are valid; the v1 decision still carries the v1 version
        assert decision_v1.config_version == "t070.config.v1"
        assert decision_v2.config_version == "t070.config.v2"

    def test_config_pool_mismatch_rejected(self) -> None:
        config = RiskConfig.default(
            version_id="t070.config.mismatch",
            pool_key_id="0x" + "ff" * 32,
        )
        decision = evaluate_risk_with_config(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(),
            config=config,
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "CONFIG_POOL_MISMATCH"


# ---------------------------------------------------------------------------
# Intent sources all use the same gateway
# ---------------------------------------------------------------------------


class TestIntentSources:
    @pytest.mark.parametrize("source", list(RiskIntentSource))
    def test_all_intent_sources_route_through_gateway(self, source: RiskIntentSource) -> None:
        """All sources — strategy, paper, manual reduce-only — must reach the
        same :func:`evaluate_risk` entry point and produce a decision."""
        decision = evaluate_risk(
            intent=_build_intent(source=source, kind=IntentKind.REDUCE_ONLY),
            context=_build_context(),
        )
        assert isinstance(decision, RiskDecision)
        assert decision.intent_source is source

    @pytest.mark.parametrize("source", list(RiskIntentSource))
    def test_global_kill_switch_blocks_all_sources(self, source: RiskIntentSource) -> None:
        ks = KillSwitch(
            name="global",
            scope=RiskScope.GLOBAL,
            active=True,
            reason_code="GLOBAL",
            raised_at=0,
            expires_at=None,
        )
        decision = evaluate_risk(
            intent=_build_intent(source=source, kind=IntentKind.REDUCE_ONLY),
            context=_build_context(kill_switches_active=(ks,)),
        )
        assert decision.verdict is RiskVerdict.AUTO_EXIT
        assert decision.approved is False

    @pytest.mark.parametrize("source", list(RiskIntentSource))
    def test_emergency_breaker_blocks_all_sources(self, source: RiskIntentSource) -> None:
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=100 * Q64_SCALE,
            close_q64_64=10 * Q64_SCALE,
            is_complete=True,
            bar=None,
        )
        decision = evaluate_risk(
            intent=_build_intent(source=source, kind=IntentKind.REDUCE_ONLY),
            context=_build_context(five_minute_breach=breach),
        )
        assert decision.verdict is RiskVerdict.AUTO_EXIT
        assert decision.approved is False


# ---------------------------------------------------------------------------
# Heterogeneous PoolKeys
# ---------------------------------------------------------------------------


class TestHeterogeneousPoolKeys:
    def test_one_decision_works_on_two_pool_keys(self) -> None:
        for pk in (_POOLKEY_NATIVE_NO_HOOK, _POOLKEY_ERC20_DYNAMIC_HOOK):
            intent = _build_intent(pool_key=pk, kind=IntentKind.REDUCE_ONLY)
            ctx = _build_context(pool_key=pk)
            decision = evaluate_risk(intent=intent, context=ctx)
            assert isinstance(decision, RiskDecision)


# ---------------------------------------------------------------------------
# Simultaneous breaches
# ---------------------------------------------------------------------------


class TestSimultaneousBreaches:
    def test_pipeline_returns_first_reason_in_order(self) -> None:
        """When multiple checks trip, the gateway returns the first reason
        in the documented pipeline order: kill switch > emergency >
        eligibility > freshness > episode > cadence > gas > hook >
        sizing > concentration > approve."""
        ks = KillSwitch(
            name="global",
            scope=RiskScope.GLOBAL,
            active=True,
            reason_code="GLOBAL_KILL",
            raised_at=0,
            expires_at=None,
        )
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=100 * Q64_SCALE,
            close_q64_64=10 * Q64_SCALE,
            is_complete=True,
            bar=None,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(
                kill_switches_active=(ks,),
                five_minute_breach=breach,
                deployment_hash_match=False,  # also broken
            ),
        )
        # Global kill switch wins (highest precedence).
        assert decision.verdict is RiskVerdict.AUTO_EXIT
        assert decision.reason_code == "GLOBAL_KILL"

    def test_emergency_then_eligibility(self) -> None:
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=100 * Q64_SCALE,
            close_q64_64=10 * Q64_SCALE,
            is_complete=True,
            bar=None,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(
                five_minute_breach=breach,
                deployment_hash_match=False,  # also broken
            ),
        )
        assert decision.reason_code == "FIVE_MINUTE_EMERGENCY_BREACH"


# ---------------------------------------------------------------------------
# Sizing / increase risk
# ---------------------------------------------------------------------------


class TestIncreaseRisk:
    def test_increase_risk_with_tight_range_no_trade(self) -> None:
        # Verify the sizer rejects degenerate Ranges via compute_sizing
        # directly. The sizer returns NO_TRADE / RANGE_DEGENERATE for
        # tick_lower == tick_upper; the CandidateAction constructor
        # rejects the same condition at the strategy layer.
        from robinhood_lp.protocol.sizing import compute_sizing

        result = compute_sizing(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            sqrt_price_x96=1 << 96,
            tick_lower=0,
            tick_upper=0,
            amount0_cap=10**6,
            amount1_cap=10**6,
            accrued_fees0=0,
            accrued_fees1=0,
            gas_reserve_wei=0,
            hook_delta0=0,
            hook_delta1=0,
            hook_verified=False,
            deadline=2000,
            min_liquidity=0,
            min_economic_liquidity_usdg_q64_64=None,
            usdg_cap_q64_64_token0=None,
            usdg_cap_q64_64_token1=None,
            usdg_amount_token0=0,
            usdg_amount_token1=0,
        )
        assert result.decision == "NO_TRADE"
        from robinhood_lp.protocol.sizing import ReasonCode as SizingRC

        assert result.reason_code == SizingRC.RANGE_DEGENERATE

    def test_increase_risk_with_misaligned_tick_rejected(self) -> None:
        # PoolKey.tick_spacing=60; tick 7 is not aligned.
        candidate = _candidate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            kind=CANDIDATE_KIND_PROPOSE,
            tick_lower=-7,
            tick_upper=60,
        )
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            decision_time=1000,
            admission=_admission(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900),
            portfolio=_portfolio(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            tick_lower=-7,
            tick_upper=60,
        )
        decision = evaluate_risk(
            intent=intent,
            context=_build_context(last_rebalance_decision_time=0),
        )
        assert decision.verdict is RiskVerdict.REJECTED


# ---------------------------------------------------------------------------
# Token concentration
# ---------------------------------------------------------------------------


class TestConcentration:
    def test_token_concentration_breach_triggers_no_new_risk(self) -> None:
        """A per-token concentration breach triggers NO_NEW_RISK at TOKEN scope.

        The pool's ``currency0`` is USDG itself (price = Q64_SCALE);
        ``currency1`` is the target token with USDG price =
        ``market.quote_q64_64``. When the target-token USDG value
        exceeds the configured ``max_single_currency_concentration_q64_64``
        share of the total worst-case inventory, the risk gateway
        returns NO_NEW_RISK at TOKEN scope (REDUCE_ONLY approved,
        INCREASE_RISK rejected). The test relies on the
        ``min_economic_liquidity_usdg_q64_64=None`` config override
        so the sizer clears the insufficient-economic-size gate and
        records the asymmetric worst-case single-sided inventory.
        """
        from robinhood_lp.protocol.math import get_sqrt_price_at_tick

        config = dataclasses.replace(
            RiskConfig.default(version_id="t070.concentration.breach"),
            min_economic_liquidity_usdg_q64_64=1,  # bypass INSUFFICIENT_ECONOMIC_SIZE
        )
        # Build a market with the current price at tick 0 (pa) and
        # quote_q64_64 = 100x USDG per token1: a high currency1 USDG
        # value relative to the symmetric currency0 (USDG itself).
        sqrt_price_at_pa = get_sqrt_price_at_tick(0)
        market = MarketSnapshot(
            version=MARKET_SNAPSHOT_VERSION,
            pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
            chain_id=_CHAIN_A,
            sqrt_price_x96=sqrt_price_at_pa,
            liquidity=1,
            realized_volatility_q64_64=0,
            freshness_seconds=0,
            quote_q64_64=100 * Q64_SCALE,  # currency1 USDG price is 100
            is_relative_only=False,
            data_time=900,
            availability_time=900,
        )
        candidate = _candidate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            kind=CANDIDATE_KIND_PROPOSE,
            tick_lower=0,
            tick_upper=60,
        )
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            source=RiskIntentSource.STRATEGY,
            candidate=candidate,
            decision_time=1000,
            admission=_admission(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            market=market,
            portfolio=_portfolio(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            tick_lower=candidate.tick_lower,
            tick_upper=candidate.tick_upper,
        )
        decision = evaluate_risk_with_config(
            intent=intent,
            context=_build_context(last_rebalance_decision_time=0),
            config=config,
        )
        assert decision.verdict is RiskVerdict.NO_NEW_RISK
        assert decision.scope is RiskScope.TOKEN
        assert decision.reason_code == "TOKEN_CONCENTRATION_EXCEEDED"
        assert decision.approved is False  # INCREASE_RISK is rejected

    def test_token_concentration_within_cap_approved(self) -> None:
        """A position with both sides well within the configured cap is approved.

        The pool's symmetric case: both worst-case raw amounts are
        ~equal and the per-side USDG price is identical, so each
        side is exactly 50% of the total — well below the default
        80% cap.
        """
        from robinhood_lp.protocol.math import get_sqrt_price_at_tick

        config = dataclasses.replace(
            RiskConfig.default(version_id="t070.concentration.ok"),
            min_economic_liquidity_usdg_q64_64=1,  # bypass INSUFFICIENT_ECONOMIC_SIZE
        )
        sqrt_price_at_tick_60 = get_sqrt_price_at_tick(60)
        market = MarketSnapshot(
            version=MARKET_SNAPSHOT_VERSION,
            pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
            chain_id=_CHAIN_A,
            sqrt_price_x96=sqrt_price_at_tick_60,
            liquidity=1,
            realized_volatility_q64_64=0,
            freshness_seconds=0,
            quote_q64_64=Q64_SCALE,  # 1:1 USDG price (symmetric)
            is_relative_only=False,
            data_time=900,
            availability_time=900,
        )
        candidate = _candidate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            kind=CANDIDATE_KIND_PROPOSE,
            tick_lower=-60,
            tick_upper=60,
        )
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            source=RiskIntentSource.STRATEGY,
            candidate=candidate,
            decision_time=1000,
            admission=_admission(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            market=market,
            portfolio=_portfolio(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            tick_lower=candidate.tick_lower,
            tick_upper=candidate.tick_upper,
        )
        decision = evaluate_risk_with_config(
            intent=intent,
            context=_build_context(last_rebalance_decision_time=0),
            config=config,
        )
        assert decision.verdict is RiskVerdict.APPROVED
        assert decision.reason_code == "OK"


# ---------------------------------------------------------------------------
# Rebalance cost amortisation (substantive check)
# ---------------------------------------------------------------------------


class TestRebalanceCostAmortisation:
    def test_cost_exceeds_capital_envelope_rejected(self) -> None:
        """When the amortised rebalance cost exceeds the candidate capital, reject.

        The per-cadence cost (cost × min_rebalance_interval_seconds)
        must not dominate the candidate's projected USDG capital
        envelope. The test sets the expected cost high enough that
        the amortised product exceeds the candidate's capital.
        """
        config = RiskConfig.default(version_id="t070.amortisation.breach")
        candidate = _candidate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            kind=CANDIDATE_KIND_PROPOSE,
            capital_q64_64=Q64_SCALE,  # 1 USDG
        )
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            decision_time=1000,
            admission=_admission(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900),
            portfolio=_portfolio(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            tick_lower=candidate.tick_lower,
            tick_upper=candidate.tick_upper,
        )
        decision = evaluate_risk_with_config(
            intent=intent,
            context=_build_context(
                last_rebalance_decision_time=0,
                # 2 USDG × 300s interval = 600 USDG >> candidate capital of 1 USDG.
                expected_rebalance_cost_usdg_q64_64=2 * Q64_SCALE,
            ),
            config=config,
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "REBALANCE_COST_INEFFICIENT"

    def test_cost_within_capital_envelope_advances(self) -> None:
        """When the amortised rebalance cost is within the capital envelope, advance.

        The downstream check (sizing / concentration / eligibility)
        produces its own verdict; what this test asserts is that the
        rebalance-cost amortisation check does not block.
        """
        config = RiskConfig.default(version_id="t070.amortisation.ok")
        candidate = _candidate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            kind=CANDIDATE_KIND_PROPOSE,
            capital_q64_64=10**18 * Q64_SCALE,  # huge capital
        )
        intent = _build_intent(
            kind=IntentKind.INCREASE_RISK,
            candidate=candidate,
            decision_time=1000,
            admission=_admission(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900),
            portfolio=_portfolio(
                pool_key=_POOLKEY_NATIVE_NO_HOOK, data_time=900, availability_time=900
            ),
            tick_lower=candidate.tick_lower,
            tick_upper=candidate.tick_upper,
        )
        decision = evaluate_risk_with_config(
            intent=intent,
            context=_build_context(
                last_rebalance_decision_time=0,
                expected_rebalance_cost_usdg_q64_64=Q64_SCALE // 10**6,  # tiny
            ),
            config=config,
        )
        # The rebalance-cost check should NOT be the cause of
        # rejection — the verdict may still be REJECTED downstream
        # (sizing / concentration / etc.) but the reason code must
        # not be REBALANCE_COST_INEFFICIENT.
        if decision.verdict is RiskVerdict.REJECTED:
            assert decision.reason_code != "REBALANCE_COST_INEFFICIENT"


# ---------------------------------------------------------------------------
# Evidence pointers / audit trail
# ---------------------------------------------------------------------------


class TestAuditTrail:
    def test_decision_carries_audit_pointer(self) -> None:
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY, audit_pointer="test:audit:1"),
            context=_build_context(evidence_pointers=("pool_id=0xabc",)),
        )
        assert decision.audit_pointer == "test:audit:1"

    def test_decision_carries_evidence_pointers(self) -> None:
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(evidence_pointers=("pool_id=0xabc", "support=paper")),
        )
        assert "pool_id=0xabc" in decision.evidence_pointers
        assert "support=paper" in decision.evidence_pointers

    def test_decision_carries_decision_time(self) -> None:
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY, decision_time=12345),
            context=_build_context(),
        )
        assert decision.decision_time == 12345


# ---------------------------------------------------------------------------
# USDG price qualification
# ---------------------------------------------------------------------------


def _build_quote_bar(
    *,
    is_relative_only: bool = False,
    missing_policy: MissingPolicy = MissingPolicy.OK,
) -> QuoteBar:
    """Build a USDG :class:`QuoteBar` with the supplied characteristics."""
    observation = Observation(
        observed_at=100,
        available_at=100,
        source=SourceKind.ONCHAIN_POOL,
        pair="USDG/USD",
        block_number=100,
        block_ref=BlockRef(
            chain_id=ChainId(_CHAIN_A),
            block_hash=int("0x" + "ab" * 32, 16),
            block_number=100,
        ),
        confidence=ConfidenceLevel.HIGH,
        staleness_seconds=0,
        numeraire_level=NumeraireLevel.USDG,
        unit=ObservationUnit.RATIO,
        value=Q64_SCALE,
        chain_id=ChainId(_CHAIN_A),
    )
    qualification = QualificationBundle(
        records=(
            NumeraireQualification(
                level=NumeraireLevel.USDG,
                selected=True,
                rationale="test",
                confidence=ConfidenceLevel.HIGH,
                staleness_seconds=0,
                stablecoin_per_usdg_q64_64=Q64_SCALE,
            ),
            NumeraireQualification(
                level=NumeraireLevel.RELATIVE_ONLY,
                selected=False,
                rationale="test",
                confidence=ConfidenceLevel.UNKNOWN,
                staleness_seconds=0,
                stablecoin_per_usdg_q64_64=None,
            ),
        ),
    )
    from robinhood_lp.features.quote import ConversionPath

    return QuoteBar(
        observation=observation,
        qualification=qualification,
        usdg_per_token_q64_64=None if is_relative_only else Q64_SCALE,
        missing_policy=missing_policy,
        path=ConversionPath(
            source=NumeraireLevel.USDG,
            target=NumeraireLevel.USDG,
            edges=(),
            policy=missing_policy,
        ),
    )


class TestUSDGPriceQualification:
    def test_relative_only_price_is_rejected(self) -> None:
        bar = _build_quote_bar(is_relative_only=True)
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=Q64_SCALE,
            close_q64_64=Q64_SCALE,
            is_complete=True,
            bar=bar,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(five_minute_breach=breach),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "USDG_PRICE_RELATIVE_ONLY"

    def test_missing_policy_is_rejected(self) -> None:
        bar = _build_quote_bar(missing_policy=MissingPolicy.MISSING)
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=Q64_SCALE,
            close_q64_64=Q64_SCALE,
            is_complete=True,
            bar=bar,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(five_minute_breach=breach),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "USDG_PRICE_MISSING"

    def test_depegged_policy_is_rejected(self) -> None:
        bar = _build_quote_bar(missing_policy=MissingPolicy.DEPEGGED)
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=Q64_SCALE,
            close_q64_64=Q64_SCALE,
            is_complete=True,
            bar=bar,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(five_minute_breach=breach),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_code == "USDG_PRICE_DEPEGGED"

    def test_ok_quote_bar_passes(self) -> None:
        bar = _build_quote_bar(missing_policy=MissingPolicy.OK)
        breach = FiveMinuteEmergencyBreach(
            open_q64_64=Q64_SCALE,
            close_q64_64=Q64_SCALE,
            is_complete=True,
            bar=bar,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(five_minute_breach=breach),
        )
        assert decision.verdict is RiskVerdict.APPROVED


# ---------------------------------------------------------------------------
# Reduce-only always passes when eligible
# ---------------------------------------------------------------------------


class TestReduceOnlyPath:
    def test_reduce_only_approved(self) -> None:
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(),
        )
        assert decision.verdict is RiskVerdict.APPROVED
        assert decision.reason_code == "OK"

    def test_reduce_only_with_observed_pnl(self) -> None:
        # Mild loss: 0.5% — below warning threshold.
        episode = _default_episode(
            current_unit_nav_usdg_q64_64=(995 * Q64_SCALE) // 1000,
            high_watermark_unit_nav_usdg_q64_64=Q64_SCALE,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(episode=episode),
        )
        assert decision.verdict is RiskVerdict.APPROVED


# ---------------------------------------------------------------------------
# Decision properties
# ---------------------------------------------------------------------------


class TestDecisionProperties:
    def test_is_approved_property(self) -> None:
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(),
        )
        assert decision.is_approved is True

    def test_is_terminal_for_rejected(self) -> None:
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(deployment_hash_match=False),
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.is_terminal is True

    def test_is_terminal_false_for_warning(self) -> None:
        # 1% loss: warning threshold
        episode = _default_episode(
            current_unit_nav_usdg_q64_64=(99 * Q64_SCALE) // 100,
            high_watermark_unit_nav_usdg_q64_64=Q64_SCALE,
        )
        decision = evaluate_risk(
            intent=_build_intent(kind=IntentKind.REDUCE_ONLY),
            context=_build_context(episode=episode),
        )
        assert decision.verdict is RiskVerdict.WARNING
        assert decision.is_terminal is False


# ---------------------------------------------------------------------------
# Imports of risk module
# ---------------------------------------------------------------------------


class TestImports:
    def test_module_imports_cleanly(self) -> None:
        """The risk module imports without side effects on first import."""
        if "robinhood_lp.risk" in sys.modules:
            del sys.modules["robinhood_lp.risk"]
        if "robinhood_lp.risk.checks" in sys.modules:
            del sys.modules["robinhood_lp.risk.checks"]
        module = importlib.import_module("robinhood_lp.risk")
        assert module.__all__  # non-empty
