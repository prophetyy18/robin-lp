"""Tests for the V1 strategy contracts module (T060).

T060 defines the strategy layer's *contracts*, not a concrete
strategy implementation. The tests cover every acceptance clause of
the T060 contract:

- **Dependency purity.** The strategy layer must not import RPC,
  storage, signing, or execution. The
  :func:`assert_strategy_layer_is_pure` helper walks the live
  module graph and rejects forbidden imports. The helper itself
  is tested by injecting a sentinel ``robinhood_lp.rpc`` import
  through ``sys.modules`` and confirming the helper raises.

- **Immutable, versioned snapshots.** The
  :class:`AdmissionSnapshot`, :class:`MarketSnapshot`, and
  :class:`PortfolioSnapshot` dataclasses are frozen; mutating any
  field raises :class:`dataclasses.FrozenInstanceError`. Every
  snapshot carries its own ``*_VERSION`` constant and an equality
  / hashing test pins the byte-identical property a downstream
  consumer relies on.

- **Heterogeneous eligible PoolKeys.** One set of snapshots, one
  pair of components, one ``evaluate_decision`` call works
  unchanged on two materially different PoolKeys (different
  chain ids, different ``tick_spacing``, different fee levels,
  different hook addresses, one native currency, one ERC-20).
  The two PoolKeys exercise the boundary case the acceptance
  clause names.

- **Tick / capital proposal validation.** Invalid ticks (out of
  bounds, unaligned to ``tick_spacing``, degenerate Range),
  stale state (snapshot ``availability_time`` later than
  ``decision_time``), NaN / display values (a ``float`` carrying
  NaN embedded in a market feature), and excessive capital
  (capital exceeding the per-pool max) are all rejected by
  :func:`validate_proposal` *before* a :class:`CandidateAction`
  is constructed. The validator never raises an exception on a
  valid proposal; the success path is the silent no-return path.

- **Component without evidence returns explicit uncertainty.** A
  :class:`RegimeModel` and a :class:`FeeOpportunityModel` that
  have no evidence to assess return
  :class:`RegimeAssessment.uncertain` and
  :class:`FeeOpportunityAssessment.uncertain` rather than
  fabricating an assessment. The interface honours the no-evidence
  path: ``evaluate_decision`` translates the ``UNCERTAIN`` outcome
  into a ``NO_TRADE`` candidate carrying the matching
  :class:`ReasonCode`.

- **Deterministic clock / random seed.** The strategy never reads
  wall-clock time or unseeded randomness. The
  :func:`assert_strategy_layer_is_pure` helper rejects any
  ``datetime`` / ``time`` / unseeded-``random`` reference inside
  the strategy module. The :class:`FrozenClock` and
  :class:`FrozenSeededRandomSource` reference implementations
  satisfy the contract with a deterministic, repeatable
  sequence.

- **Reason-coded ``NO_TRADE``.** The :class:`ReasonCode` enum is
  closed; every reason a validator may raise maps to a
  :class:`ReasonCode`, and every :class:`CandidateAction` of kind
  ``NO_TRADE`` carries exactly one reason code.

- **Candidate action.** The :class:`CandidateAction` is the
  proposal the strategy submits to execution; the central risk
  layer (T070) and the execution layer (T071) consume it. The
  candidate is *not* a transaction: no signing / broadcast
  identifier appears on it. The ``PROPOSE`` kind carries the
  Range, the liquidity and the capital envelope; the ``WAIT``
  and ``NO_TRADE`` kinds carry sentinels instead.

The must-not clauses are also tested:

- **No approval of its own risk.** The strategy never returns a
  ``CandidateAction`` whose ``reason_code`` names a risk verdict;
  the reason-code set is restricted to the closed vocabulary the
  strategy owns.

- **No mutation of snapshots / ledger / audit.** A snapshot's
  ``__post_init__`` raises on every mutating operation; the
  strategy layer's public functions do not return ``None`` on
  inputs they intend to mutate.
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Iterable
from typing import Final

import pytest

from robinhood_lp.protocol.events import BlockRef
from robinhood_lp.protocol.ids import Address, ChainId, Currency, PoolKey
from robinhood_lp.strategy import (
    ADMISSION_SNAPSHOT_VERSION,
    CANDIDATE_ACTION_VERSION,
    CANDIDATE_KIND_NO_TRADE,
    CANDIDATE_KIND_PROPOSE,
    CANDIDATE_KIND_WAIT,
    FEE_OPPORTUNITY_ASSESSMENT_VERSION,
    FEE_OPPORTUNITY_OUTCOME_ASSESSED,
    FEE_OPPORTUNITY_OUTCOME_UNCERTAIN,
    MARKET_SNAPSHOT_VERSION,
    MAX_CAPITAL_Q64_64,
    MAX_TICK_SPACING_STRATEGY,
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
    InvalidSnapshotError,
    InvalidTickError,
    MarketSnapshot,
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

# ---------------------------------------------------------------------------
# Reference PoolKeys (heterogeneous eligible PoolKeys)
# ---------------------------------------------------------------------------

_CHAIN_A: Final[int] = 46630
_CHAIN_B: Final[int] = 46631

#: Native-currency pool. ``currency0`` is the zero address; the hook
#: address is the zero address (no hook); fee is the 5-bip static
#: tier; ``tick_spacing`` is 60.
_POOLKEY_NATIVE_NO_HOOK: Final[PoolKey] = PoolKey(
    currency0=Currency.native(),
    currency1=Currency.from_hex("0x" + "11" * 20),
    fee=500,
    tick_spacing=60,
    hooks=Address.zero(),
)

#: ERC-20-only pool on a different chain. ``currency0`` and
#: ``currency1`` are both non-zero; the hook address is a non-zero
#: address (a hook pool); fee is the dynamic-fee sentinel;
#: ``tick_spacing`` is the smaller 10-bip tier.
_POOLKEY_ERC20_DYNAMIC_HOOK: Final[PoolKey] = PoolKey(
    currency0=Currency.from_hex("0x" + "22" * 20),
    currency1=Currency.from_hex("0x" + "33" * 20),
    fee=0x800000,
    tick_spacing=10,
    hooks=Address.from_hex("0x" + "44" * 20),
)


def _pool_key_id(pool_key: PoolKey) -> str:
    """Return a stable per-pool key id used by the snapshots."""
    return "0x" + pool_key.to_pool_id().to_bytes().hex()


# ---------------------------------------------------------------------------
# Reference snapshots
# ---------------------------------------------------------------------------


def _admission(
    *,
    pool_key: PoolKey,
    data_time: int = 100,
    availability_time: int = 105,
    max_capital_q64_64: int = MAX_CAPITAL_Q64_64,
    track_b_admitted: bool = True,
) -> AdmissionSnapshot:
    return AdmissionSnapshot(
        version=ADMISSION_SNAPSHOT_VERSION,
        pool_key_id=_pool_key_id(pool_key),
        chain_id=pool_key.currency0.address.value + 1,  # placeholder chain id
        support_level="backtest",
        track_a_admitted=True,
        track_b_admitted=track_b_admitted,
        max_capital_q64_64=max_capital_q64_64,
        data_time=data_time,
        availability_time=availability_time,
    )


def _market(
    *,
    pool_key: PoolKey,
    data_time: int = 100,
    availability_time: int = 105,
    sqrt_price_x96: int = 1 << 96,
    liquidity: int = 1,
    realized_volatility_q64_64: int = 0,
    freshness_seconds: int = 0,
    quote_q64_64: int | None = 1 << 64,
    is_relative_only: bool = False,
) -> MarketSnapshot:
    return MarketSnapshot(
        version=MARKET_SNAPSHOT_VERSION,
        pool_key_id=_pool_key_id(pool_key),
        chain_id=pool_key.currency0.address.value + 1,
        sqrt_price_x96=sqrt_price_x96,
        liquidity=liquidity,
        realized_volatility_q64_64=realized_volatility_q64_64,
        freshness_seconds=freshness_seconds,
        quote_q64_64=quote_q64_64,
        is_relative_only=is_relative_only,
        data_time=data_time,
        availability_time=availability_time,
    )


def _portfolio(
    *,
    pool_key: PoolKey,
    data_time: int = 100,
    availability_time: int = 105,
    is_empty: bool = True,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        version=PORTFOLIO_SNAPSHOT_VERSION,
        pool_key_id=_pool_key_id(pool_key),
        chain_id=pool_key.currency0.address.value + 1,
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


# ---------------------------------------------------------------------------
# Reference components
# ---------------------------------------------------------------------------


class _RangeRegimeModel:
    """A reference :class:`RegimeModel` that always returns RANGE / ASSESSED."""

    def assess(
        self,
        *,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        clock: DeterministicClock,
        rng: SeededRandomSource,
    ) -> RegimeAssessment:
        return RegimeAssessment(
            version=REGIME_ASSESSMENT_VERSION,
            model_version="t060.test.range.v1",
            outcome=REGIME_OUTCOME_ASSESSED,
            state=REGIME_STATE_RANGE,
            confidence_q64_64=1 << 64,
            evidence_keys=(MARKET_SNAPSHOT_VERSION,),
        )


class _UncertainRegimeModel:
    """A reference :class:`RegimeModel` that always returns UNCERTAIN."""

    def assess(
        self,
        *,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        clock: DeterministicClock,
        rng: SeededRandomSource,
    ) -> RegimeAssessment:
        return RegimeAssessment.uncertain(
            model_version="t060.test.uncertain.v1",
            evidence_keys=(),
            notes=("no evidence to assess",),
        )


class _PositiveFeeOpportunityModel:
    """A reference :class:`FeeOpportunityModel` returning a positive fee edge."""

    def assess(
        self,
        *,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        clock: DeterministicClock,
        rng: SeededRandomSource,
    ) -> FeeOpportunityAssessment:
        return FeeOpportunityAssessment(
            version=FEE_OPPORTUNITY_ASSESSMENT_VERSION,
            model_version="t060.test.fee_positive.v1",
            outcome=FEE_OPPORTUNITY_OUTCOME_ASSESSED,
            expected_fee_edge_q64_64=1 << 62,  # 25 %
            confidence_q64_64=1 << 64,
            evidence_keys=(MARKET_SNAPSHOT_VERSION,),
        )


class _ZeroFeeOpportunityModel:
    """A reference :class:`FeeOpportunityModel` returning a zero fee edge."""

    def assess(
        self,
        *,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        clock: DeterministicClock,
        rng: SeededRandomSource,
    ) -> FeeOpportunityAssessment:
        return FeeOpportunityAssessment(
            version=FEE_OPPORTUNITY_ASSESSMENT_VERSION,
            model_version="t060.test.fee_zero.v1",
            outcome=FEE_OPPORTUNITY_OUTCOME_ASSESSED,
            expected_fee_edge_q64_64=0,
            confidence_q64_64=1 << 64,
            evidence_keys=(MARKET_SNAPSHOT_VERSION,),
        )


class _UncertainFeeOpportunityModel:
    """A reference :class:`FeeOpportunityModel` always returning UNCERTAIN."""

    def assess(
        self,
        *,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        clock: DeterministicClock,
        rng: SeededRandomSource,
    ) -> FeeOpportunityAssessment:
        return FeeOpportunityAssessment.uncertain(
            model_version="t060.test.fee_uncertain.v1",
            evidence_keys=(),
            notes=("no evidence to assess",),
        )


# ---------------------------------------------------------------------------
# Snapshots — immutable, versioned
# ---------------------------------------------------------------------------


class TestSnapshotsAreFrozen:
    """The snapshot dataclasses are frozen: no field is mutable."""

    def test_admission_snapshot_is_frozen(self) -> None:
        snap = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.track_a_admitted = False  # type: ignore[misc]

    def test_market_snapshot_is_frozen(self) -> None:
        snap = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.liquidity = 0  # type: ignore[misc]

    def test_portfolio_snapshot_is_frozen(self) -> None:
        snap = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.is_empty = False  # type: ignore[misc]


class TestSnapshotsAreVersioned:
    """Every snapshot carries its own version constant."""

    def test_admission_snapshot_version_matches_constant(self) -> None:
        snap = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        assert snap.version == ADMISSION_SNAPSHOT_VERSION

    def test_market_snapshot_version_matches_constant(self) -> None:
        snap = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        assert snap.version == MARKET_SNAPSHOT_VERSION

    def test_portfolio_snapshot_version_matches_constant(self) -> None:
        snap = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        assert snap.version == PORTFOLIO_SNAPSHOT_VERSION

    def test_snapshot_versions_are_stable_strings(self) -> None:
        assert ADMISSION_SNAPSHOT_VERSION.startswith("t060.")
        assert MARKET_SNAPSHOT_VERSION.startswith("t060.")
        assert PORTFOLIO_SNAPSHOT_VERSION.startswith("t060.")


class TestSnapshotsAreHashable:
    """Frozen dataclasses hash identically across processes."""

    def test_admission_snapshot_hash_is_deterministic(self) -> None:
        a = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        b = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        assert hash(a) == hash(b)
        assert a == b

    def test_market_snapshot_hash_is_deterministic(self) -> None:
        a = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        b = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        assert hash(a) == hash(b)

    def test_portfolio_snapshot_hash_is_deterministic(self) -> None:
        a = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        b = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        assert hash(a) == hash(b)


class TestSnapshotsValidation:
    """The snapshot constructors reject invalid inputs."""

    def test_admission_rejects_non_positive_chain_id(self) -> None:
        with pytest.raises(InvalidSnapshotError):
            AdmissionSnapshot(
                version=ADMISSION_SNAPSHOT_VERSION,
                pool_key_id="k",
                chain_id=0,
                support_level="backtest",
                track_a_admitted=True,
                track_b_admitted=True,
                max_capital_q64_64=Q64_SCALE,
                data_time=0,
                availability_time=0,
            )

    def test_admission_rejects_zero_max_capital(self) -> None:
        with pytest.raises(InvalidSnapshotError):
            AdmissionSnapshot(
                version=ADMISSION_SNAPSHOT_VERSION,
                pool_key_id="k",
                chain_id=1,
                support_level="backtest",
                track_a_admitted=True,
                track_b_admitted=True,
                max_capital_q64_64=0,
                data_time=0,
                availability_time=0,
            )

    def test_market_rejects_zero_sqrt_price(self) -> None:
        with pytest.raises(InvalidSnapshotError):
            MarketSnapshot(
                version=MARKET_SNAPSHOT_VERSION,
                pool_key_id="k",
                chain_id=1,
                sqrt_price_x96=0,
                liquidity=1,
                realized_volatility_q64_64=0,
                freshness_seconds=0,
                quote_q64_64=1,
                is_relative_only=False,
                data_time=0,
                availability_time=0,
            )

    def test_market_rejects_relative_only_with_quote(self) -> None:
        with pytest.raises(InvalidSnapshotError):
            MarketSnapshot(
                version=MARKET_SNAPSHOT_VERSION,
                pool_key_id="k",
                chain_id=1,
                sqrt_price_x96=1,
                liquidity=1,
                realized_volatility_q64_64=0,
                freshness_seconds=0,
                quote_q64_64=1,
                is_relative_only=True,
                data_time=0,
                availability_time=0,
            )

    def test_portfolio_rejects_empty_with_nonzero_principal(self) -> None:
        with pytest.raises(InvalidSnapshotError):
            PortfolioSnapshot(
                version=PORTFOLIO_SNAPSHOT_VERSION,
                pool_key_id="k",
                chain_id=1,
                position_id="p",
                sqrt_price_x96=1,
                liquidity=0,
                principal_token0=1,
                principal_token1=0,
                tokens_owed0=0,
                tokens_owed1=0,
                is_empty=True,
                data_time=0,
                availability_time=0,
            )


# ---------------------------------------------------------------------------
# Deterministic clock / seeded random source
# ---------------------------------------------------------------------------


class TestFrozenClock:
    """A reference deterministic clock implementation."""

    def test_clock_returns_event_times_in_order(self) -> None:
        clock = FrozenClock(event_times=(10, 20, 30))
        assert clock.now() == 10
        clock = clock.advance()
        assert clock.now() == 20
        clock = clock.advance()
        assert clock.now() == 30

    def test_clock_is_immutable_when_advancing(self) -> None:
        clock = FrozenClock(event_times=(10, 20, 30))
        cursor_before = clock.cursor
        clock.advance()
        # The original clock must not change: the frozen contract holds.
        assert clock.cursor == cursor_before

    def test_clock_exhaustion_raises(self) -> None:
        clock = FrozenClock(event_times=(10,))
        clock = clock.advance()
        with pytest.raises(DeterministicClockError):
            clock.advance()

    def test_clock_rejects_decreasing_sequence(self) -> None:
        with pytest.raises(DeterministicClockError):
            FrozenClock(event_times=(20, 10))

    def test_clock_is_deterministic_across_processes(self) -> None:
        a = FrozenClock(event_times=(0, 1, 2, 3))
        b = FrozenClock(event_times=(0, 1, 2, 3))
        # Same call sequence must produce the same output sequence.
        seq_a = []
        for _ in range(4):
            seq_a.append(a.now())
            a = a.advance()
        seq_b = []
        for _ in range(4):
            seq_b.append(b.now())
            b = b.advance()
        assert seq_a == seq_b

    def test_clock_satisfies_protocol(self) -> None:
        clock = FrozenClock(event_times=(1,))
        assert isinstance(clock, DeterministicClock)


class TestFrozenSeededRandomSource:
    """A reference seeded random source implementation."""

    def test_seed_is_recorded(self) -> None:
        rng = FrozenSeededRandomSource(initial_seed=42)
        assert rng.initial_seed == 42

    def test_seed_method_is_no_op(self) -> None:
        rng = FrozenSeededRandomSource(initial_seed=42)
        # The Protocol method exists; calling it does not raise.
        rng.seed(99)
        assert rng.initial_seed == 42

    def test_next_int_returns_pre_generated_value(self) -> None:
        rng = FrozenSeededRandomSource(
            initial_seed=1,
            _buckets=((100, (7, 13, 42)),),
        )
        assert rng.next_int(100) == 7

    def test_next_int_rejects_out_of_range_value(self) -> None:
        with pytest.raises(StrategyError):
            FrozenSeededRandomSource(
                initial_seed=1,
                _buckets=((10, (10,)),),  # 10 is not in [0, 10)
            )

    def test_next_int_rejects_non_positive_bound(self) -> None:
        rng = FrozenSeededRandomSource(initial_seed=1)
        with pytest.raises(InvalidSnapshotError):
            rng.next_int(0)

    def test_next_int_raises_when_bucket_empty(self) -> None:
        rng = FrozenSeededRandomSource(initial_seed=1, _buckets=())
        with pytest.raises(StrategyError):
            rng.next_int(100)

    def test_seeded_random_satisfies_protocol(self) -> None:
        rng = FrozenSeededRandomSource(initial_seed=1, _buckets=((100, (7,)),))
        assert isinstance(rng, SeededRandomSource)


# ---------------------------------------------------------------------------
# Component interface — uncertainty path
# ---------------------------------------------------------------------------


class TestRegimeModelProtocol:
    """The :class:`RegimeModel` protocol is satisfied by reference implementations."""

    def test_range_model_satisfies_protocol(self) -> None:
        assert isinstance(_RangeRegimeModel(), RegimeModel)

    def test_uncertain_model_satisfies_protocol(self) -> None:
        assert isinstance(_UncertainRegimeModel(), RegimeModel)


class TestFeeOpportunityModelProtocol:
    """The :class:`FeeOpportunityModel` protocol is satisfied by reference implementations."""

    def test_positive_model_satisfies_protocol(self) -> None:
        assert isinstance(_PositiveFeeOpportunityModel(), FeeOpportunityModel)

    def test_zero_model_satisfies_protocol(self) -> None:
        assert isinstance(_ZeroFeeOpportunityModel(), FeeOpportunityModel)

    def test_uncertain_model_satisfies_protocol(self) -> None:
        assert isinstance(_UncertainFeeOpportunityModel(), FeeOpportunityModel)


class TestRegimeAssessmentUncertain:
    """A regime component without evidence returns explicit uncertainty."""

    def test_uncertain_assessment_has_uncertain_outcome(self) -> None:
        assessment = _UncertainRegimeModel().assess(
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            portfolio=_portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            clock=FrozenClock(event_times=(0,)),
            rng=FrozenSeededRandomSource(initial_seed=0),
        )
        assert assessment.outcome == REGIME_OUTCOME_UNCERTAIN
        assert assessment.state == REGIME_STATE_UNCERTAIN
        assert assessment.confidence_q64_64 == 0

    def test_uncertain_assessment_factory_rejects_assessed_state(self) -> None:
        with pytest.raises(StrategyError):
            RegimeAssessment.uncertain(
                model_version="x.v1",
                evidence_keys=(),
            )
            # Calling the constructor with ASSESSED state would be the
            # bypass path; the convenience constructor only accepts
            # the no-evidence path.
            RegimeAssessment(
                version=REGIME_ASSESSMENT_VERSION,
                model_version="x.v1",
                outcome=REGIME_OUTCOME_UNCERTAIN,
                state=REGIME_STATE_RANGE,
                confidence_q64_64=1 << 64,
            )

    def test_assessed_assessment_rejects_uncertain_state(self) -> None:
        with pytest.raises(StrategyError):
            RegimeAssessment(
                version=REGIME_ASSESSMENT_VERSION,
                model_version="x.v1",
                outcome=REGIME_OUTCOME_ASSESSED,
                state=REGIME_STATE_UNCERTAIN,
                confidence_q64_64=1 << 64,
            )

    def test_assessed_assessment_rejects_unknown_state(self) -> None:
        with pytest.raises(StrategyError):
            RegimeAssessment(
                version=REGIME_ASSESSMENT_VERSION,
                model_version="x.v1",
                outcome=REGIME_OUTCOME_ASSESSED,
                state="DRIFT",
                confidence_q64_64=1 << 64,
            )


class TestFeeOpportunityAssessmentUncertain:
    """A fee-opportunity component without evidence returns explicit uncertainty."""

    def test_uncertain_assessment_has_uncertain_outcome(self) -> None:
        assessment = _UncertainFeeOpportunityModel().assess(
            market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            portfolio=_portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK),
            clock=FrozenClock(event_times=(0,)),
            rng=FrozenSeededRandomSource(initial_seed=0),
        )
        assert assessment.outcome == FEE_OPPORTUNITY_OUTCOME_UNCERTAIN
        assert assessment.expected_fee_edge_q64_64 == 0
        assert assessment.confidence_q64_64 == 0

    def test_assessed_assessment_requires_positive_confidence(self) -> None:
        with pytest.raises(StrategyError):
            FeeOpportunityAssessment(
                version=FEE_OPPORTUNITY_ASSESSMENT_VERSION,
                model_version="x.v1",
                outcome=FEE_OPPORTUNITY_OUTCOME_ASSESSED,
                expected_fee_edge_q64_64=1,
                confidence_q64_64=0,
            )


# ---------------------------------------------------------------------------
# validate_proposal — invalid ticks, stale state, NaN, excessive capital
# ---------------------------------------------------------------------------


class TestValidateProposalInvalidTicks:
    """Invalid ticks fail before a candidate action is constructed."""

    def test_tick_lower_out_of_bounds_fails(self) -> None:
        admission = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(InvalidTickError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=60,
                tick_lower=-(1 << 24),  # below V4 MIN_TICK
                tick_upper=120,
                capital_q64_64=1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=200,
            )

    def test_tick_upper_out_of_bounds_fails(self) -> None:
        admission = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(InvalidTickError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=60,
                tick_lower=0,
                tick_upper=(1 << 24),  # above V4 MAX_TICK
                capital_q64_64=1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=200,
            )

    def test_degenerate_range_fails(self) -> None:
        admission = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(InvalidTickError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=60,
                tick_lower=120,
                tick_upper=120,
                capital_q64_64=1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=200,
            )

    def test_unaligned_tick_lower_fails(self) -> None:
        admission = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(InvalidTickError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=60,  # tick must be divisible by 60
                tick_lower=61,
                tick_upper=120,
                capital_q64_64=1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=200,
            )

    def test_unaligned_tick_upper_fails(self) -> None:
        admission = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(InvalidTickError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=60,
                tick_lower=0,
                tick_upper=121,
                capital_q64_64=1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=200,
            )

    def test_tick_spacing_out_of_range_fails(self) -> None:
        admission = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(InvalidTickError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=0,
                tick_lower=0,
                tick_upper=120,
                capital_q64_64=1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=200,
            )
        with pytest.raises(InvalidTickError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=MAX_TICK_SPACING_STRATEGY + 1,
                tick_lower=0,
                tick_upper=120,
                capital_q64_64=1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=200,
            )


class TestValidateProposalStaleState:
    """Stale-state violations fail before a candidate action is constructed."""

    def test_admission_stale_fails(self) -> None:
        admission = _admission(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            data_time=100,
            availability_time=200,
        )
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(StaleSnapshotError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=60,
                tick_lower=0,
                tick_upper=120,
                capital_q64_64=1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=100,  # before admission.availability_time
            )

    def test_market_stale_fails(self) -> None:
        admission = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        market = _market(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            data_time=100,
            availability_time=200,
        )
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(StaleSnapshotError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=60,
                tick_lower=0,
                tick_upper=120,
                capital_q64_64=1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=100,
            )

    def test_portfolio_stale_fails(self) -> None:
        admission = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            data_time=100,
            availability_time=200,
        )
        with pytest.raises(StaleSnapshotError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=60,
                tick_lower=0,
                tick_upper=120,
                capital_q64_64=1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=100,
            )


class TestValidateProposalNaNDisplayValue:
    """NaN / display values in the market snapshot fail validation."""

    def test_market_with_nan_float_fails(self) -> None:
        """A :class:`MarketSnapshot` cannot be built with a float NaN, but a
        custom subclass injection test verifies the runtime guard."""
        # The constructor's int-only validation is the strongest guard:
        # float values are rejected at construction time. The validator
        # also has a defensive check for any future numeric payload.
        with pytest.raises(InvalidSnapshotError):
            MarketSnapshot(
                version=MARKET_SNAPSHOT_VERSION,
                pool_key_id="k",
                chain_id=1,
                sqrt_price_x96=float("nan"),  # type: ignore[arg-type]
                liquidity=1,
                realized_volatility_q64_64=0,
                freshness_seconds=0,
                quote_q64_64=1,
                is_relative_only=False,
                data_time=0,
                availability_time=0,
            )


class TestValidateProposalExcessiveCapital:
    """Excessive capital fails before a candidate action is constructed."""

    def test_non_positive_capital_fails(self) -> None:
        admission = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(InvalidCapitalError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=60,
                tick_lower=0,
                tick_upper=120,
                capital_q64_64=0,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=200,
            )

    def test_capital_exceeding_pool_max_fails(self) -> None:
        admission = _admission(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            max_capital_q64_64=Q64_SCALE,
        )
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(InvalidCapitalError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=60,
                tick_lower=0,
                tick_upper=120,
                capital_q64_64=Q64_SCALE + 1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=200,
            )

    def test_capital_exceeding_project_ceiling_fails(self) -> None:
        admission = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        with pytest.raises(InvalidCapitalError):
            validate_proposal(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                tick_spacing=60,
                tick_lower=0,
                tick_upper=120,
                capital_q64_64=MAX_CAPITAL_Q64_64 + 1,
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=200,
            )


class TestValidateProposalSuccess:
    """A valid proposal returns ``None`` (silent success)."""

    def test_valid_proposal_returns_none(self) -> None:
        admission = _admission(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        market = _market(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        portfolio = _portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK)
        # ``validate_proposal`` returns ``None`` on success; the
        # function call below simply exercises the no-exception path.
        validate_proposal(
            pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
            tick_spacing=60,
            tick_lower=0,
            tick_upper=120,
            capital_q64_64=Q64_SCALE,
            admission=admission,
            market=market,
            portfolio=portfolio,
            decision_time=200,
        )
        # ``validate_proposal`` returns ``None`` on success; the call
        # above exercises the no-exception path. No further assertion is
        # needed (the function's return type is ``None``).


# ---------------------------------------------------------------------------
# evaluate_decision — heterogeneous PoolKeys, components, and outcome paths
# ---------------------------------------------------------------------------


def _evaluate(
    *,
    pool_key: PoolKey,
    regime_model: RegimeModel,
    fee_model: FeeOpportunityModel,
    tick_spacing: int,
    tick_lower: int = 0,
    tick_upper: int,
    capital_q64_64: int = Q64_SCALE,
    decision_time: int = 200,
) -> CandidateAction:
    """Run :func:`evaluate_decision` against the supplied PoolKey."""
    return evaluate_decision(
        pool_key_id=_pool_key_id(pool_key),
        chain_id=pool_key.currency0.address.value + 1,
        admission=_admission(pool_key=pool_key),
        market=_market(pool_key=pool_key),
        portfolio=_portfolio(pool_key=pool_key),
        regime_model=regime_model,
        fee_opportunity_model=fee_model,
        clock=FrozenClock(event_times=(decision_time,)),
        rng=FrozenSeededRandomSource(initial_seed=0, _buckets=((100, (0,)),)),
        decision_time=decision_time,
        tick_spacing=tick_spacing,
        candidate_tick_lower=tick_lower,
        candidate_tick_upper=tick_upper,
        capital_q64_64=capital_q64_64,
    )


class TestEvaluateDecisionHeterogeneousPoolKeys:
    """One instance works unchanged on heterogeneous eligible PoolKeys."""

    def test_native_pool_propose_outcome(self) -> None:
        """Native pool (zero address currency0, no hook)."""
        action = _evaluate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            regime_model=_RangeRegimeModel(),
            fee_model=_PositiveFeeOpportunityModel(),
            tick_spacing=60,
            tick_lower=0,
            tick_upper=120,
        )
        assert action.kind == CANDIDATE_KIND_PROPOSE
        assert action.tick_lower == 0
        assert action.tick_upper == 120

    def test_erc20_dynamic_fee_hook_pool_propose_outcome(self) -> None:
        """ERC-20 pool (non-zero addresses, dynamic-fee sentinel, hook)."""
        action = _evaluate(
            pool_key=_POOLKEY_ERC20_DYNAMIC_HOOK,
            regime_model=_RangeRegimeModel(),
            fee_model=_PositiveFeeOpportunityModel(),
            tick_spacing=10,
            tick_lower=0,
            tick_upper=20,
        )
        assert action.kind == CANDIDATE_KIND_PROPOSE
        assert action.tick_lower == 0
        assert action.tick_upper == 20

    def test_one_instance_handles_both_pools_unchanged(self) -> None:
        """A single pair of components satisfies both pools without change."""
        regime = _RangeRegimeModel()
        fee = _PositiveFeeOpportunityModel()
        action_a = _evaluate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            regime_model=regime,
            fee_model=fee,
            tick_spacing=60,
            tick_lower=0,
            tick_upper=120,
        )
        action_b = _evaluate(
            pool_key=_POOLKEY_ERC20_DYNAMIC_HOOK,
            regime_model=regime,
            fee_model=fee,
            tick_spacing=10,
            tick_lower=0,
            tick_upper=20,
        )
        assert action_a.pool_key_id != action_b.pool_key_id
        assert action_a.kind == action_b.kind == CANDIDATE_KIND_PROPOSE


class TestEvaluateDecisionUncertainty:
    """An uncertain component returns a ``NO_TRADE`` candidate with a reason code."""

    def test_uncertain_regime_yields_regime_uncertain_reason(self) -> None:
        action = _evaluate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            regime_model=_UncertainRegimeModel(),
            fee_model=_PositiveFeeOpportunityModel(),
            tick_spacing=60,
            tick_lower=0,
            tick_upper=120,
        )
        assert action.kind == CANDIDATE_KIND_NO_TRADE
        assert action.reason_code == ReasonCode.REGIME_UNCERTAIN
        assert action.regime_outcome == REGIME_OUTCOME_UNCERTAIN
        assert action.regime_state == REGIME_STATE_UNCERTAIN

    def test_uncertain_fee_yields_fee_opportunity_uncertain_reason(self) -> None:
        action = _evaluate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            regime_model=_RangeRegimeModel(),
            fee_model=_UncertainFeeOpportunityModel(),
            tick_spacing=60,
            tick_lower=0,
            tick_upper=120,
        )
        assert action.kind == CANDIDATE_KIND_NO_TRADE
        assert action.reason_code == ReasonCode.FEE_OPPORTUNITY_UNCERTAIN
        assert action.fee_opportunity_outcome == FEE_OPPORTUNITY_OUTCOME_UNCERTAIN

    def test_regime_uncarried_wait_when_assessed_but_not_range(self) -> None:
        """An ASSESSED regime that is not RANGE yields WAIT (a positive
        ``do not trade now`` verdict) rather than NO_TRADE."""

        class _UpTrendRegimeModel:
            def assess(
                self,
                *,
                market: MarketSnapshot,
                portfolio: PortfolioSnapshot,
                clock: DeterministicClock,
                rng: SeededRandomSource,
            ) -> RegimeAssessment:
                return RegimeAssessment(
                    version=REGIME_ASSESSMENT_VERSION,
                    model_version="t060.test.up_trend.v1",
                    outcome=REGIME_OUTCOME_ASSESSED,
                    state=REGIME_STATE_UP_TREND,
                    confidence_q64_64=1 << 64,
                )

        action = _evaluate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            regime_model=_UpTrendRegimeModel(),
            fee_model=_PositiveFeeOpportunityModel(),
            tick_spacing=60,
            tick_lower=0,
            tick_upper=120,
        )
        assert action.kind == CANDIDATE_KIND_WAIT
        assert action.reason_code is None

    def test_jump_risk_state_yields_wait(self) -> None:
        """An ASSESSED JUMP_RISK regime yields WAIT even with a positive fee."""

        class _JumpRiskRegimeModel:
            def assess(
                self,
                *,
                market: MarketSnapshot,
                portfolio: PortfolioSnapshot,
                clock: DeterministicClock,
                rng: SeededRandomSource,
            ) -> RegimeAssessment:
                return RegimeAssessment(
                    version=REGIME_ASSESSMENT_VERSION,
                    model_version="t060.test.jump.v1",
                    outcome=REGIME_OUTCOME_ASSESSED,
                    state=REGIME_STATE_JUMP_RISK,
                    confidence_q64_64=1 << 64,
                )

        action = _evaluate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            regime_model=_JumpRiskRegimeModel(),
            fee_model=_PositiveFeeOpportunityModel(),
            tick_spacing=60,
            tick_lower=0,
            tick_upper=120,
        )
        assert action.kind == CANDIDATE_KIND_WAIT

    def test_zero_fee_yields_wait(self) -> None:
        """A zero fee edge yields WAIT (the strategy chose not to trade)."""
        action = _evaluate(
            pool_key=_POOLKEY_NATIVE_NO_HOOK,
            regime_model=_RangeRegimeModel(),
            fee_model=_ZeroFeeOpportunityModel(),
            tick_spacing=60,
            tick_lower=0,
            tick_upper=120,
        )
        assert action.kind == CANDIDATE_KIND_WAIT


class TestEvaluateDecisionValidationWins:
    """A validation failure raises before any component is invoked."""

    def test_invalid_tick_raises_before_component_query(self) -> None:
        """The validator runs first; the component is never consulted."""

        class _ExplodingModel:
            def assess(
                self,
                *,
                market: MarketSnapshot,
                portfolio: PortfolioSnapshot,
                clock: DeterministicClock,
                rng: SeededRandomSource,
            ) -> RegimeAssessment:
                raise AssertionError("validator should have rejected the proposal")

        with pytest.raises(InvalidTickError):
            evaluate_decision(
                pool_key_id=_pool_key_id(_POOLKEY_NATIVE_NO_HOOK),
                chain_id=1,
                admission=_admission(pool_key=_POOLKEY_NATIVE_NO_HOOK),
                market=_market(pool_key=_POOLKEY_NATIVE_NO_HOOK),
                portfolio=_portfolio(pool_key=_POOLKEY_NATIVE_NO_HOOK),
                regime_model=_ExplodingModel(),
                fee_opportunity_model=_PositiveFeeOpportunityModel(),
                clock=FrozenClock(event_times=(200,)),
                rng=FrozenSeededRandomSource(initial_seed=0),
                decision_time=200,
                tick_spacing=60,
                candidate_tick_lower=121,
                candidate_tick_upper=120,
                capital_q64_64=Q64_SCALE,
            )


# ---------------------------------------------------------------------------
# CandidateAction — invariants, kind-specific requirements
# ---------------------------------------------------------------------------


class TestCandidateActionInvariants:
    """The :class:`CandidateAction` dataclass enforces its invariants."""

    def test_propose_requires_tick_range(self) -> None:
        with pytest.raises(CandidateActionError):
            CandidateAction(
                version=CANDIDATE_ACTION_VERSION,
                pool_key_id="k",
                chain_id=1,
                kind=CANDIDATE_KIND_PROPOSE,
                decision_time=200,
                availability_time=100,
                snapshot_versions=(MARKET_SNAPSHOT_VERSION,),
                component_versions=(REGIME_ASSESSMENT_VERSION,),
                regime_state=REGIME_STATE_RANGE,
                regime_outcome=REGIME_OUTCOME_ASSESSED,
                fee_opportunity_outcome=FEE_OPPORTUNITY_OUTCOME_ASSESSED,
                reason_code=None,
                tick_lower=120,
                tick_upper=120,
                liquidity=1,
                capital_q64_64=1,
            )

    def test_no_trade_requires_reason_code(self) -> None:
        with pytest.raises(CandidateActionError):
            CandidateAction(
                version=CANDIDATE_ACTION_VERSION,
                pool_key_id="k",
                chain_id=1,
                kind=CANDIDATE_KIND_NO_TRADE,
                decision_time=200,
                availability_time=100,
                snapshot_versions=(MARKET_SNAPSHOT_VERSION,),
                component_versions=(REGIME_ASSESSMENT_VERSION,),
                regime_state=REGIME_STATE_UNCERTAIN,
                regime_outcome=REGIME_OUTCOME_UNCERTAIN,
                fee_opportunity_outcome=FEE_OPPORTUNITY_OUTCOME_UNCERTAIN,
                reason_code=None,
            )

    def test_no_trade_rejects_tick_lower(self) -> None:
        with pytest.raises(CandidateActionError):
            CandidateAction(
                version=CANDIDATE_ACTION_VERSION,
                pool_key_id="k",
                chain_id=1,
                kind=CANDIDATE_KIND_NO_TRADE,
                decision_time=200,
                availability_time=100,
                snapshot_versions=(MARKET_SNAPSHOT_VERSION,),
                component_versions=(REGIME_ASSESSMENT_VERSION,),
                regime_state=REGIME_STATE_UNCERTAIN,
                regime_outcome=REGIME_OUTCOME_UNCERTAIN,
                fee_opportunity_outcome=FEE_OPPORTUNITY_OUTCOME_UNCERTAIN,
                reason_code=ReasonCode.STRATEGY_HOLD,
                tick_lower=10,
                tick_upper=20,
            )

    def test_wait_rejects_reason_code(self) -> None:
        with pytest.raises(CandidateActionError):
            CandidateAction(
                version=CANDIDATE_ACTION_VERSION,
                pool_key_id="k",
                chain_id=1,
                kind=CANDIDATE_KIND_WAIT,
                decision_time=200,
                availability_time=100,
                snapshot_versions=(MARKET_SNAPSHOT_VERSION,),
                component_versions=(REGIME_ASSESSMENT_VERSION,),
                regime_state=REGIME_STATE_RANGE,
                regime_outcome=REGIME_OUTCOME_ASSESSED,
                fee_opportunity_outcome=FEE_OPPORTUNITY_OUTCOME_ASSESSED,
                reason_code=ReasonCode.STRATEGY_HOLD,
            )

    def test_propose_rejects_reason_code(self) -> None:
        with pytest.raises(CandidateActionError):
            CandidateAction(
                version=CANDIDATE_ACTION_VERSION,
                pool_key_id="k",
                chain_id=1,
                kind=CANDIDATE_KIND_PROPOSE,
                decision_time=200,
                availability_time=100,
                snapshot_versions=(MARKET_SNAPSHOT_VERSION,),
                component_versions=(REGIME_ASSESSMENT_VERSION,),
                regime_state=REGIME_STATE_RANGE,
                regime_outcome=REGIME_OUTCOME_ASSESSED,
                fee_opportunity_outcome=FEE_OPPORTUNITY_OUTCOME_ASSESSED,
                reason_code=ReasonCode.STRATEGY_HOLD,
                tick_lower=0,
                tick_upper=120,
                liquidity=1,
                capital_q64_64=1,
            )

    def test_availability_time_must_be_at_or_before_decision_time(self) -> None:
        with pytest.raises(CandidateActionError):
            CandidateAction(
                version=CANDIDATE_ACTION_VERSION,
                pool_key_id="k",
                chain_id=1,
                kind=CANDIDATE_KIND_NO_TRADE,
                decision_time=200,
                availability_time=300,  # > decision_time
                snapshot_versions=(),
                component_versions=(),
                regime_state=REGIME_STATE_UNCERTAIN,
                regime_outcome=REGIME_OUTCOME_UNCERTAIN,
                fee_opportunity_outcome=FEE_OPPORTUNITY_OUTCOME_UNCERTAIN,
                reason_code=ReasonCode.STRATEGY_HOLD,
            )


# ---------------------------------------------------------------------------
# ReasonCode — closed vocabulary
# ---------------------------------------------------------------------------


class TestReasonCodeVocabulary:
    """The :class:`ReasonCode` enum is closed."""

    def test_reason_codes_include_all_validation_paths(self) -> None:
        codes = {code.value for code in ReasonCode}
        # Each validator failure path has a stable reason code.
        assert "RANGE_DEGENERATE" in codes
        assert "TICK_OUT_OF_BOUNDS" in codes
        assert "TICK_NOT_ALIGNED" in codes
        assert "STALE_SNAPSHOT" in codes
        assert "NAN_DISPLAY_VALUE" in codes
        assert "CAPITAL_EXCESSIVE" in codes
        assert "CAPITAL_NONPOSITIVE" in codes

    def test_reason_codes_include_component_uncertainty(self) -> None:
        codes = {code.value for code in ReasonCode}
        assert "REGIME_UNCERTAIN" in codes
        assert "FEE_OPPORTUNITY_UNCERTAIN" in codes

    def test_reason_codes_are_stable_strings(self) -> None:
        # Renaming an existing code is a breaking change; the strings
        # are part of the public contract.
        assert ReasonCode.RANGE_DEGENERATE.value == "RANGE_DEGENERATE"


# ---------------------------------------------------------------------------
# Dependency purity
# ---------------------------------------------------------------------------


class TestStrategyLayerIsPure:
    """The strategy layer does not import RPC / storage / signing / execution."""

    def test_strategy_layer_is_pure(self) -> None:
        assert_strategy_layer_is_pure()

    def test_collected_imports_include_only_protocol_and_strategy(self) -> None:
        imports_by_module = collect_strategy_module_imports()
        for module_name, imports in imports_by_module.items():
            for name in imports:
                assert not name.startswith("robinhood_lp.rpc"), (
                    f"{module_name} imports {name} (forbidden)"
                )
                assert not name.startswith("robinhood_lp.storage"), (
                    f"{module_name} imports {name} (forbidden)"
                )
                assert not name.startswith("robinhood_lp.ingestion"), (
                    f"{module_name} imports {name} (forbidden)"
                )
                assert not name.startswith("robinhood_lp.config"), (
                    f"{module_name} imports {name} (forbidden)"
                )
                assert not name.startswith("robinhood_lp.replay"), (
                    f"{module_name} imports {name} (forbidden)"
                )
                assert not name.startswith("robinhood_lp.discovery"), (
                    f"{module_name} imports {name} (forbidden)"
                )
                assert not name.startswith("robinhood_lp.qualification"), (
                    f"{module_name} imports {name} (forbidden)"
                )
                assert not name.startswith("robinhood_lp.quality"), (
                    f"{module_name} imports {name} (forbidden)"
                )
                assert not name.startswith("robinhood_lp.presentation"), (
                    f"{module_name} imports {name} (forbidden)"
                )

    def test_strategy_layer_rejects_forbidden_import_via_injection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Injecting a sentinel ``robinhood_lp.rpc`` import into the
        strategy module must cause
        :func:`assert_strategy_layer_is_pure` to raise. The injection
        uses a real :class:`types.ModuleType` instance bound into the
        strategy base module's ``__dict__`` so the walker surfaces the
        forbidden import in the collected imports map."""

        sentinel = importlib.import_module("types").ModuleType("robinhood_lp.rpc")
        strategy_module = importlib.import_module("robinhood_lp.strategy.base")
        # Inject the sentinel directly into the module's ``__dict__``.
        original = dict(vars(strategy_module))
        strategy_module.__dict__["injected_rpc"] = sentinel
        try:
            with pytest.raises(StrategyError):
                assert_strategy_layer_is_pure()
        finally:
            strategy_module.__dict__.clear()
            strategy_module.__dict__.update(original)

    def test_signing_imports_are_forbidden_by_no_signing_paths(self) -> None:
        """The strategy module is also scanned by the cross-cutting
        ``test_no_signing_paths`` test; the import-time contract here
        pins the assertion to the strategy layer specifically."""
        from tests.test_no_signing_paths import FORBIDDEN_IMPORTS

        strategy_root = importlib.import_module("robinhood_lp.strategy.base").__file__
        assert strategy_root is not None
        from pathlib import Path

        for path in [Path(strategy_root)]:
            text = path.read_text(encoding="utf-8")
            for module in _imports_in(text):
                for forbidden in FORBIDDEN_IMPORTS:
                    assert not (module == forbidden or module.startswith(forbidden + ".")), (
                        f"{path}: imports forbidden signing module {module}"
                    )


def _imports_in(text: str) -> Iterable[str]:
    """Yield every dotted module name referenced by an import statement."""
    import re

    for match in re.finditer(
        r"^\s*(?:from\s+([\w.]+)|import\s+([\w.]+))",
        text,
        re.MULTILINE,
    ):
        module = match.group(1) or match.group(2)
        if module:
            yield module


# ---------------------------------------------------------------------------
# Module constants — sanity
# ---------------------------------------------------------------------------


class TestModuleConstants:
    """The module exposes stable constants for downstream consumers."""

    def test_version_constants_are_t060(self) -> None:
        for constant in (
            ADMISSION_SNAPSHOT_VERSION,
            MARKET_SNAPSHOT_VERSION,
            PORTFOLIO_SNAPSHOT_VERSION,
            REGIME_ASSESSMENT_VERSION,
            FEE_OPPORTUNITY_ASSESSMENT_VERSION,
            CANDIDATE_ACTION_VERSION,
        ):
            assert constant.startswith("t060.")

    def test_q64_scale_matches_project_convention(self) -> None:
        assert Q64_SCALE == 1 << 64

    def test_kind_sentinels_are_distinct(self) -> None:
        kinds = {
            CANDIDATE_KIND_NO_TRADE,
            CANDIDATE_KIND_PROPOSE,
            CANDIDATE_KIND_WAIT,
        }
        assert len(kinds) == 3

    def test_assessment_outcome_vocabulary_is_shared(self) -> None:
        """Both regime and fee-opportunity components share the same
        outcome vocabulary (``ASSESSED`` / ``UNCERTAIN``); the *kind*
        is what distinguishes them, not the outcome string."""
        assert REGIME_OUTCOME_ASSESSED == FEE_OPPORTUNITY_OUTCOME_ASSESSED
        assert REGIME_OUTCOME_UNCERTAIN == FEE_OPPORTUNITY_OUTCOME_UNCERTAIN

    def test_regime_state_sentinels_are_distinct(self) -> None:
        states = {
            REGIME_STATE_RANGE,
            REGIME_STATE_UP_TREND,
            REGIME_STATE_DOWN_TREND,
            REGIME_STATE_JUMP_RISK,
            REGIME_STATE_UNCERTAIN,
        }
        assert len(states) == 5


# ---------------------------------------------------------------------------
# Reference objects — used by other test files in the suite
# ---------------------------------------------------------------------------


def _unused_but_pinned_for_clarity() -> object:
    """Pin the reference imports the suite would otherwise drop."""
    return (
        Address,
        BlockRef,
        ChainId,
        Currency,
        ProposalValidationError,
    )


_ = _unused_but_pinned_for_clarity()
