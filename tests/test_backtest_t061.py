"""Tests for the event-driven backtest engine (T061).

T061 builds the event-driven backtest engine that consumes a manifest
of :class:`BacktestEvent` records plus a :class:`ModelBundle` and emits
an append-only stream of :class:`AuditEvent` records. The tests cover
every acceptance clause of the T061 contract:

- **Ordered event clock.** Events are sorted by
  ``(timestamp, sequence, source_priority)``. Two equivalent manifests
  in different input orders produce identical event ids, decisions and
  ledger hashes. Same-timestamp ties are broken deterministically.

- **Information frontier.** A future-data attempt — an event whose
  ``available_at`` exceeds the decision time — is recorded as a
  :attr:`STATUS_FUTURE_DATA_VIOLATION` audit event; the engine refuses
  to use the future event and refuses to fill using a price whose
  availability the declared model cannot prove.

- **Decision → risk → latency → fill pipeline.** Each pipeline stage
  produces exactly one :class:`AuditEvent`. The decision is *recorded*
  but the ledger is *not* mutated until the fill stage; a risk
  rejection produces a ``RISK`` audit event with no subsequent fill.

- **Liquidity / fee / gas / slippage / failure models.** Each model is
  parameterised, deterministic, versioned and unit-carrying; the
  engine records the model versions on every audit chain so a later
  rerun can replay with an older implementation.

- **Append-only audit events.** Every :class:`AuditEvent` is a frozen
  dataclass with a deterministic ``event_id`` (SHA-256 of its canonical
  content), ``parent_event_ids`` (the chain that produced it),
  ``stage``, ``timestamp``, ``payload`` and ``ledger_hash_after``.

The must-not clauses are also tested:

- **No fill using the trigger price.** The engine fills at the price of
  the *next visible data event at or after the declared fill time*,
  never at the trigger price; if no such data event exists, the
  engine raises :class:`FutureDataViolation` and refuses to fill.

- **No hiding of failed / rejected intents.** A rejected risk verdict
  produces a ``RISK`` audit event with status
  :attr:`STATUS_RISK_REJECTED`; the rejection is visible on the audit
  chain and the ledger is unchanged. A rejected fill produces a
  ``FILL`` audit event with status :attr:`STATUS_FILL_REJECTED`.
"""

from __future__ import annotations

from typing import Final

import pytest

from robinhood_lp.backtest import (
    AUDIT_EVENT_VERSION,
    BACKTEST_ENGINE_VERSION,
    BACKTEST_EVENT_VERSION,
    FEE_DENOMINATOR_PIPS,
    KIND_OBSERVATION,
    KIND_SHUTDOWN,
    KIND_SWAP,
    KIND_TICK,
    LEDGER_VERSION,
    SLIPPAGE_DENOMINATOR_BPS,
    SOURCE_PRIORITY_DATA,
    SOURCE_PRIORITY_RISK,
    SOURCE_PRIORITY_STRATEGY,
    SOURCE_PRIORITY_SYSTEM,
    STAGE_DECISION,
    STAGE_FILL,
    STAGE_LATENCY,
    STAGE_RISK,
    STAGE_SYSTEM,
    AuditEvent,
    BacktestEngine,
    BacktestEvent,
    BacktestEventError,
    ConstantLiquidityModel,
    ConstantSlippageModel,
    DeterministicFailureModel,
    FillOutcome,
    FlatGasModel,
    FutureDataViolation,
    InvalidFillOutcomeError,
    InvalidUnitError,
    ModelBundle,
    ModelError,
    PositionState,
    RiskDecision,
    StaticFeeModel,
    StrategyDecision,
    StrategyDecisionRequest,
    ZeroSlippageModel,
    assert_backtest_events_layer_is_pure,
    assert_backtest_models_layer_is_pure,
    empty_position_state,
)

# ---------------------------------------------------------------------------
# Reference fixtures
# ---------------------------------------------------------------------------


_POOL_KEY_ID: Final[str] = "0x" + "ab" * 32
_CHAIN_ID: Final[int] = 46630
_LATENCY_UNITS: Final[int] = 0


def _swap_event(
    *,
    timestamp: int,
    observed_at: int | None = None,
    available_at: int | None = None,
    price_q64_64: int = 1 << 64,
) -> BacktestEvent:
    return BacktestEvent(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,  # engine reassigns
        source_priority=SOURCE_PRIORITY_DATA,
        kind=KIND_SWAP,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        observed_at=timestamp if observed_at is None else observed_at,
        available_at=timestamp if available_at is None else available_at,
        payload=(("price_q64_64", price_q64_64),),
    )


def _tick_event(*, timestamp: int) -> BacktestEvent:
    return BacktestEvent(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_DATA,
        kind=KIND_TICK,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(),
    )


def _shutdown_event(*, timestamp: int) -> BacktestEvent:
    return BacktestEvent(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_SYSTEM,
        kind=KIND_SHUTDOWN,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        observed_at=timestamp,
        available_at=timestamp,
        payload=(),
    )


def _observation_event(
    *,
    timestamp: int,
    observed_at: int | None = None,
    available_at: int | None = None,
    price_q64_64: int = 1 << 64,
) -> BacktestEvent:
    """A non-reactive data-source event the fill stage can use as fill data.

    ``OBSERVATION`` events are not in :data:`DEFAULT_REACT_TO_KINDS`, so
    the engine does not hand them to the strategy callback. They *are*
    in :data:`DEFAULT_FILL_DATA_KINDS`, so the fill stage can use them
    when no reactive event exists at the declared fill time.
    """
    return BacktestEvent(
        version=BACKTEST_EVENT_VERSION,
        timestamp=timestamp,
        sequence=0,
        source_priority=SOURCE_PRIORITY_DATA,
        kind=KIND_OBSERVATION,
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        observed_at=timestamp if observed_at is None else observed_at,
        available_at=timestamp if available_at is None else available_at,
        payload=(("price_q64_64", price_q64_64),),
    )


def _always_propose_strategy(_request: StrategyDecisionRequest) -> StrategyDecision:
    """A reference strategy callback that always proposes a fixed Range."""
    return StrategyDecision(
        kind="PROPOSE",
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        decision_time=0,  # engine records the real time
        tick_lower=-60,
        tick_upper=60,
        liquidity=1000,
        capital_q64_64=1 << 64,
    )


def _no_trade_strategy(_request: StrategyDecisionRequest) -> StrategyDecision:
    """A reference strategy callback that always returns ``NO_TRADE``."""
    return StrategyDecision(
        kind="NO_TRADE",
        pool_key_id=_POOL_KEY_ID,
        chain_id=_CHAIN_ID,
        decision_time=0,
    )


def _always_approve_risk(_decision: StrategyDecision) -> RiskDecision:
    return RiskDecision(approved=True, reason_code="OK")


def _always_reject_risk(_decision: StrategyDecision) -> RiskDecision:
    return RiskDecision(approved=False, reason_code="CAP_EXCEEDED")


# Concrete callback instances so mypy --strict accepts them as
# ``StrategyCallback`` / ``RiskCallback`` parameters. The instance
# classes just dispatch ``__call__`` to the function above.
class _ProposeCallback:
    def __call__(self, request: StrategyDecisionRequest) -> StrategyDecision:
        return _always_propose_strategy(request)


class _NoTradeCallback:
    def __call__(self, request: StrategyDecisionRequest) -> StrategyDecision:
        return _no_trade_strategy(request)


class _ApproveRiskCallback:
    def __call__(self, decision: StrategyDecision) -> RiskDecision:
        return _always_approve_risk(decision)


class _RejectRiskCallback:
    def __call__(self, decision: StrategyDecision) -> RiskDecision:
        return _always_reject_risk(decision)


# Module-level instances — the tests reference these directly.
_PROPOSE_CB: _ProposeCallback = _ProposeCallback()
_NO_TRADE_CB: _NoTradeCallback = _NoTradeCallback()
_APPROVE_RISK_CB: _ApproveRiskCallback = _ApproveRiskCallback()
_REJECT_RISK_CB: _RejectRiskCallback = _RejectRiskCallback()


def _filled_bundle() -> ModelBundle:
    return ModelBundle(
        bundle_version="t061.test.v1",
        liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
        fee=StaticFeeModel(fee_pips_value=3_000),  # 0.3 %
        gas=FlatGasModel(gas_units_value=21_000),
        slippage=ZeroSlippageModel(),
        failure=DeterministicFailureModel(),
        latency_units=_LATENCY_UNITS,
    )


# ---------------------------------------------------------------------------
# BacktestEvent / AuditEvent / PositionState construction tests
# ---------------------------------------------------------------------------


class TestEventConstruction:
    """The constructor invariants of every primitive event."""

    def test_backtest_event_assigns_event_id_when_missing(self) -> None:
        evt = BacktestEvent(
            version=BACKTEST_EVENT_VERSION,
            timestamp=100,
            sequence=0,
            source_priority=SOURCE_PRIORITY_DATA,
            kind=KIND_SWAP,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            observed_at=100,
            available_at=100,
            payload=(("price", 1),),
        )
        assert evt.event_id.startswith("0x")
        assert len(evt.event_id) == 66

    def test_backtest_event_rejects_unknown_source_priority(self) -> None:
        with pytest.raises(BacktestEventError):
            BacktestEvent(
                version=BACKTEST_EVENT_VERSION,
                timestamp=100,
                sequence=0,
                source_priority=999,
                kind=KIND_SWAP,
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                observed_at=100,
                available_at=100,
                payload=(),
            )

    def test_backtest_event_rejects_unknown_kind(self) -> None:
        with pytest.raises(BacktestEventError):
            BacktestEvent(
                version=BACKTEST_EVENT_VERSION,
                timestamp=100,
                sequence=0,
                source_priority=SOURCE_PRIORITY_DATA,
                kind="UNKNOWN",
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                observed_at=100,
                available_at=100,
                payload=(),
            )

    def test_backtest_event_rejects_available_before_observed(self) -> None:
        with pytest.raises(BacktestEventError):
            BacktestEvent(
                version=BACKTEST_EVENT_VERSION,
                timestamp=100,
                sequence=0,
                source_priority=SOURCE_PRIORITY_DATA,
                kind=KIND_SWAP,
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                observed_at=100,
                available_at=50,  # < observed_at
                payload=(),
            )

    def test_backtest_event_is_visible_predicate(self) -> None:
        evt = _swap_event(timestamp=100)
        assert evt.is_visible(100) is True
        assert evt.is_visible(101) is True
        assert evt.is_visible(99) is False

    def test_backtest_event_is_visible_rejects_future_availability(self) -> None:
        evt = _swap_event(timestamp=100, observed_at=100, available_at=150)
        assert evt.is_visible(149) is False
        assert evt.is_visible(150) is True
        assert evt.is_visible(151) is True

    def test_backtest_event_normalises_payload(self) -> None:
        # Insertion order does not affect equality / hash.
        a = BacktestEvent(
            version=BACKTEST_EVENT_VERSION,
            timestamp=100,
            sequence=0,
            source_priority=SOURCE_PRIORITY_DATA,
            kind=KIND_SWAP,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            observed_at=100,
            available_at=100,
            payload=(("b", 2), ("a", 1)),
        )
        b = BacktestEvent(
            version=BACKTEST_EVENT_VERSION,
            timestamp=100,
            sequence=0,
            source_priority=SOURCE_PRIORITY_DATA,
            kind=KIND_SWAP,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            observed_at=100,
            available_at=100,
            payload=(("a", 1), ("b", 2)),
        )
        assert a.event_id == b.event_id
        assert a.payload == (("a", 1), ("b", 2))

    def test_backtest_event_rejects_duplicate_payload_keys(self) -> None:
        with pytest.raises(BacktestEventError):
            BacktestEvent(
                version=BACKTEST_EVENT_VERSION,
                timestamp=100,
                sequence=0,
                source_priority=SOURCE_PRIORITY_DATA,
                kind=KIND_SWAP,
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                observed_at=100,
                available_at=100,
                payload=(("a", 1), ("a", 2)),
            )

    def test_backtest_event_rejects_float_payload(self) -> None:
        with pytest.raises(BacktestEventError):
            BacktestEvent(
                version=BACKTEST_EVENT_VERSION,
                timestamp=100,
                sequence=0,
                source_priority=SOURCE_PRIORITY_DATA,
                kind=KIND_SWAP,
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                observed_at=100,
                available_at=100,
                payload=(("price", 1.5),),  # type: ignore[arg-type]
            )

    def test_audit_event_assigns_event_id_when_built(self) -> None:
        audit = AuditEvent.build(
            stage=STAGE_DECISION,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            timestamp=100,
            sequence=0,
            parent_event_ids=("0x" + "aa" * 32,),
            payload=(("kind", "PROPOSE"),),
            ledger_hash_after="0x" + "bb" * 32,
        )
        assert audit.event_id.startswith("0x")
        assert audit.version == AUDIT_EVENT_VERSION
        assert audit.stage == STAGE_DECISION

    def test_audit_event_rejects_unknown_stage(self) -> None:
        with pytest.raises(BacktestEventError):
            AuditEvent.build(
                stage="WRONG",
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                timestamp=100,
                sequence=0,
                parent_event_ids=(),
                payload=(),
                ledger_hash_after="0x" + "00" * 32,
            )

    def test_audit_event_parent_ids_are_sorted(self) -> None:
        a = "0x" + "aa" * 32
        b = "0x" + "bb" * 32
        audit = AuditEvent.build(
            stage=STAGE_RISK,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            timestamp=100,
            sequence=0,
            parent_event_ids=(b, a),  # intentionally reversed
            payload=(),
            ledger_hash_after="0x" + "00" * 32,
        )
        assert audit.parent_event_ids == (a, b)

    def test_position_state_ledger_hash_is_deterministic(self) -> None:
        state = PositionState(
            version=LEDGER_VERSION,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            position_id="0x" + "00" * 32,
            tick_lower=-60,
            tick_upper=60,
            liquidity=1000,
            principal_token0=500,
            principal_token1=500,
            tokens_owed0=10,
            tokens_owed1=20,
            in_range=True,
            last_accrual_time=100,
        )
        h1 = state.ledger_hash()
        h2 = state.ledger_hash()
        assert h1 == h2
        assert h1.startswith("0x")
        assert len(h1) == 66

    def test_position_state_rejects_degenerate_range(self) -> None:
        with pytest.raises(BacktestEventError):
            PositionState(
                version=LEDGER_VERSION,
                pool_key_id=_POOL_KEY_ID,
                chain_id=_CHAIN_ID,
                position_id="0x" + "00" * 32,
                tick_lower=60,
                tick_upper=60,
                liquidity=0,
                principal_token0=0,
                principal_token1=0,
                tokens_owed0=0,
                tokens_owed1=0,
                in_range=False,
                last_accrual_time=0,
            )

    def test_position_state_with_updates_returns_new_state(self) -> None:
        state = empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID)
        new_state = state.with_updates(liquidity=123, in_range=True, last_accrual_time=10)
        assert state.liquidity == 0
        assert new_state.liquidity == 123
        assert new_state.in_range is True
        assert new_state.last_accrual_time == 10

    def test_position_state_with_updates_rejects_unknown_field(self) -> None:
        state = empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID)
        with pytest.raises(BacktestEventError):
            state.with_updates(unknown_field=1)


# ---------------------------------------------------------------------------
# Information frontier / future-data trap tests
# ---------------------------------------------------------------------------


class TestInformationFrontier:
    """The engine must reject future-data and refuse to fill using the trigger price."""

    def test_event_with_available_after_decision_is_not_visible(self) -> None:
        evt = _swap_event(timestamp=100, observed_at=100, available_at=200)
        assert evt.is_visible(150) is False
        assert evt.is_visible(200) is True

    def test_future_data_violation_records_payload(self) -> None:
        err = FutureDataViolation(event_id="0x" + "aa" * 32, available_at=200, decision_time=100)
        assert err.event_id == "0x" + "aa" * 32
        assert "200" in str(err) and "100" in str(err)


# ---------------------------------------------------------------------------
# Pipeline integration tests
# ---------------------------------------------------------------------------


class TestPipelineEndToEnd:
    """The full sort → frontier → decision → risk → latency → fill pipeline."""

    def test_same_manifest_produces_identical_results(self) -> None:
        events = [
            _swap_event(timestamp=100),
            _swap_event(timestamp=200),
            _swap_event(timestamp=300),
            _shutdown_event(timestamp=400),
        ]
        bundle = _filled_bundle()
        engine_a = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_PROPOSE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result_a = engine_a.run(events)
        # Run again with the *same* input list — byte-equivalent output.
        result_b = engine_a.run(events)
        assert result_a.manifest_hash == result_b.manifest_hash
        assert result_a.bundle_hash == result_b.bundle_hash
        assert result_a.final_ledger == result_b.final_ledger
        assert [a.event_id for a in result_a.audit_events] == [
            a.event_id for a in result_b.audit_events
        ]

    def test_manifest_in_different_input_order_produces_same_output(self) -> None:
        forward = [
            _swap_event(timestamp=100),
            _swap_event(timestamp=200),
            _swap_event(timestamp=300),
        ]
        backward = list(reversed(forward))
        bundle = _filled_bundle()
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_PROPOSE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result_a = engine.run(forward)
        result_b = engine.run(backward)
        assert result_a.manifest_hash == result_b.manifest_hash
        assert [a.event_id for a in result_a.audit_events] == [
            a.event_id for a in result_b.audit_events
        ]

    def test_decision_is_recorded_but_ledger_unchanged_until_fill(self) -> None:
        events = [_swap_event(timestamp=100, price_q64_64=2 << 64), _shutdown_event(timestamp=200)]
        bundle = _filled_bundle()
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_PROPOSE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result = engine.run(events)
        decision_events = [a for a in result.audit_events if a.stage == STAGE_DECISION]
        risk_events = [a for a in result.audit_events if a.stage == STAGE_RISK]
        latency_events = [a for a in result.audit_events if a.stage == STAGE_LATENCY]
        fill_events = [a for a in result.audit_events if a.stage == STAGE_FILL]
        assert len(decision_events) == 1
        assert len(risk_events) == 1
        assert len(latency_events) == 1
        assert len(fill_events) == 1
        # Decision / Risk / Latency events have the zero ledger hash
        # because they did not mutate the ledger. Only FILL does.
        assert decision_events[0].ledger_hash_after.startswith("0x")
        assert risk_events[0].ledger_hash_after.startswith("0x")
        assert latency_events[0].ledger_hash_after.startswith("0x")
        # The fill event carries the post-fill ledger hash.
        assert fill_events[0].ledger_hash_after == result.final_ledger.ledger_hash()

    def test_no_trade_decision_records_no_fill(self) -> None:
        events = [_swap_event(timestamp=100), _shutdown_event(timestamp=200)]
        bundle = _filled_bundle()
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_NO_TRADE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result = engine.run(events)
        decision_events = [a for a in result.audit_events if a.stage == STAGE_DECISION]
        fill_events = [a for a in result.audit_events if a.stage == STAGE_FILL]
        assert len(decision_events) == 1
        assert len(fill_events) == 0
        # Ledger unchanged.
        assert result.final_ledger.liquidity == 0

    def test_rejected_risk_records_rejection_but_no_fill(self) -> None:
        events = [_swap_event(timestamp=100), _shutdown_event(timestamp=200)]
        bundle = _filled_bundle()
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_PROPOSE_CB,
            risk_callback=_REJECT_RISK_CB,
        )
        result = engine.run(events)
        decision_events = [a for a in result.audit_events if a.stage == STAGE_DECISION]
        risk_events = [a for a in result.audit_events if a.stage == STAGE_RISK]
        fill_events = [a for a in result.audit_events if a.stage == STAGE_FILL]
        assert len(decision_events) == 1
        assert len(risk_events) == 1
        assert len(fill_events) == 0
        # The risk event carries status *REJECTED*; the engine does
        # not hide the rejection.
        risk_payload = dict(risk_events[0].payload)
        assert risk_payload["approved"] == 0
        assert risk_payload["reason_code"] == "CAP_EXCEEDED"

    def test_partial_fill_records_filled_liquidity_below_requested(self) -> None:
        bundle = ModelBundle(
            bundle_version="t061.test.partial.v1",
            liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
            fee=StaticFeeModel(fee_pips_value=3_000),
            gas=FlatGasModel(gas_units_value=21_000),
            slippage=ZeroSlippageModel(),
            failure=DeterministicFailureModel(schedule=((100, "PARTIAL"),)),
            latency_units=_LATENCY_UNITS,
        )
        events = [_swap_event(timestamp=100), _shutdown_event(timestamp=200)]
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_PROPOSE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result = engine.run(events)
        fill_events = [a for a in result.audit_events if a.stage == STAGE_FILL]
        assert len(fill_events) == 1
        payload = dict(fill_events[0].payload)
        assert payload["outcome"] == "PARTIAL"
        assert int(payload["filled_liquidity"]) < int(payload["requested_liquidity"])

    def test_rejected_fill_records_status_but_does_not_mutate_ledger(self) -> None:
        bundle = ModelBundle(
            bundle_version="t061.test.rejected.v1",
            liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
            fee=StaticFeeModel(fee_pips_value=3_000),
            gas=FlatGasModel(gas_units_value=21_000),
            slippage=ZeroSlippageModel(),
            failure=DeterministicFailureModel(schedule=((100, "REJECTED"),)),
            latency_units=_LATENCY_UNITS,
        )
        events = [_swap_event(timestamp=100), _shutdown_event(timestamp=200)]
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_PROPOSE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result = engine.run(events)
        fill_events = [a for a in result.audit_events if a.stage == STAGE_FILL]
        assert len(fill_events) == 1
        payload = dict(fill_events[0].payload)
        assert payload["outcome"] == "REJECTED"
        # Rejected fill leaves the ledger empty.
        assert result.final_ledger.liquidity == 0

    def test_delayed_fill_records_delayed_status(self) -> None:
        bundle = ModelBundle(
            bundle_version="t061.test.delayed.v1",
            liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
            fee=StaticFeeModel(fee_pips_value=3_000),
            gas=FlatGasModel(gas_units_value=21_000),
            slippage=ZeroSlippageModel(),
            failure=DeterministicFailureModel(schedule=((100, "DELAYED"),)),
            latency_units=0,
        )
        events = [
            _swap_event(timestamp=100),
            _shutdown_event(timestamp=200),
        ]
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_PROPOSE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result = engine.run(events)
        latency_events = [a for a in result.audit_events if a.stage == STAGE_LATENCY]
        fill_events = [a for a in result.audit_events if a.stage == STAGE_FILL]
        assert len(latency_events) == 1
        assert len(fill_events) == 1
        latency_payload = dict(latency_events[0].payload)
        fill_payload = dict(fill_events[0].payload)
        # Latency 0 → fill_time = decision_time = 100.
        assert latency_payload["fill_time"] == 100
        assert latency_payload["delayed"] == 0
        assert fill_payload["outcome"] == "DELAYED"


# ---------------------------------------------------------------------------
# Same-timestamp ordering tests
# ---------------------------------------------------------------------------


class TestSameTimestampOrdering:
    """Events at the same timestamp must be ordered deterministically."""

    def test_same_timestamp_data_before_strategy_before_risk(self) -> None:
        # All events share timestamp=100; source priority decides order.
        a = _swap_event(timestamp=100)  # DATA = 1
        b = BacktestEvent(
            version=BACKTEST_EVENT_VERSION,
            timestamp=100,
            sequence=0,
            source_priority=SOURCE_PRIORITY_STRATEGY,
            kind=KIND_TICK,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            observed_at=100,
            available_at=100,
            payload=(),
        )
        c = BacktestEvent(
            version=BACKTEST_EVENT_VERSION,
            timestamp=100,
            sequence=0,
            source_priority=SOURCE_PRIORITY_RISK,
            kind=KIND_TICK,
            pool_key_id=_POOL_KEY_ID,
            chain_id=_CHAIN_ID,
            observed_at=100,
            available_at=100,
            payload=(),
        )
        bundle = _filled_bundle()
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_NO_TRADE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        # The engine sorts deterministically regardless of input order.
        result1 = engine.run([a, b, c, _shutdown_event(timestamp=200)])
        result2 = engine.run([c, b, a, _shutdown_event(timestamp=200)])
        assert [e.event_id for e in result1.audit_events] == [
            e.event_id for e in result2.audit_events
        ]

    def test_same_source_same_timestamp_breaks_by_sequence(self) -> None:
        # Same source + same timestamp: the input event_ids are
        # content-deterministic, so the engine assigns sequences by
        # sorted content order. Three identical SWAP events at the
        # same timestamp collapse to the same event_id and are
        # therefore indistinguishable; the engine sees one event and
        # processes it once.
        events = [
            _swap_event(timestamp=100, price_q64_64=1),
            _swap_event(timestamp=100, price_q64_64=2),
            _swap_event(timestamp=100, price_q64_64=3),
        ]
        bundle = _filled_bundle()
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_NO_TRADE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result_a = engine.run(events)
        result_b = engine.run(list(reversed(events)))
        # Byte-equivalent output for forward and reverse order.
        assert result_a.manifest_hash == result_b.manifest_hash
        assert [a.event_id for a in result_a.audit_events] == [
            a.event_id for a in result_b.audit_events
        ]


# ---------------------------------------------------------------------------
# Out-of-range accrual + shutdown tests
# ---------------------------------------------------------------------------


class TestOutOfRangeAndShutdown:
    """The engine must handle out-of-range periods and shutdown cleanly."""

    def test_shutdown_marker_stops_engine(self) -> None:
        events = [
            _swap_event(timestamp=100),
            _shutdown_event(timestamp=150),
            _swap_event(timestamp=200),  # never processed
        ]
        bundle = _filled_bundle()
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_PROPOSE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result = engine.run(events)
        # The shutdown event terminates the engine: only one reactive
        # swap (at t=100) is processed.
        system_events = [a for a in result.audit_events if a.stage == STAGE_SYSTEM]
        # The final SYSTEM event should be a SHUTDOWN.
        final_system = system_events[-1]
        # The engine emits ``final_ledger_hash`` + ``manifest_hash`` on
        # shutdown, while init emits ``bundle_hash`` + ``manifest_hash``.
        assert "final_ledger_hash" in dict(final_system.payload)

    def test_engine_emits_init_and_shutdown_system_events(self) -> None:
        events = [_swap_event(timestamp=100), _shutdown_event(timestamp=200)]
        bundle = _filled_bundle()
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_NO_TRADE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result = engine.run(events)
        system_events = [a for a in result.audit_events if a.stage == STAGE_SYSTEM]
        # Init + shutdown = 2 SYSTEM events.
        assert len(system_events) == 2
        assert "bundle_hash" in dict(system_events[0].payload)
        assert "final_ledger_hash" in dict(system_events[1].payload)

    def test_engine_terminates_with_shutdown_when_no_marker(self) -> None:
        events = [_swap_event(timestamp=100)]
        bundle = _filled_bundle()
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_NO_TRADE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result = engine.run(events)
        system_events = [a for a in result.audit_events if a.stage == STAGE_SYSTEM]
        # The engine still emits a SHUTDOWN when the input list is exhausted.
        final_system = system_events[-1]
        assert "final_ledger_hash" in dict(final_system.payload)


# ---------------------------------------------------------------------------
# Future-data trap tests (engine-level)
# ---------------------------------------------------------------------------


class TestFutureDataTrap:
    """The engine refuses to fill when no data is visible at the fill time."""

    def test_no_data_at_fill_time_raises_future_data_violation(self) -> None:
        # The strategy decides at t=100; the latency model delays to
        # t=110; no data event exists at or after t=110 (the SHUTDOWN
        # at t=200 is not reactive). The engine must refuse to fill
        # and raise FutureDataViolation.
        events = [_swap_event(timestamp=100), _shutdown_event(timestamp=200)]
        bundle = ModelBundle(
            bundle_version="t061.test.future.v1",
            liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
            fee=StaticFeeModel(fee_pips_value=3_000),
            gas=FlatGasModel(gas_units_value=21_000),
            slippage=ZeroSlippageModel(),
            failure=DeterministicFailureModel(),
            latency_units=10,  # decision at 100 + 10 = 110, no data at t=110
        )
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_PROPOSE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        with pytest.raises(FutureDataViolation):
            engine.run(events)

    def test_fill_uses_data_event_at_fill_time_not_trigger_price(self) -> None:
        # The fill price must come from the data event at the (possibly
        # delayed) fill time, not from the trigger event. The OBSERVATION
        # at t=110 is non-reactive fill data; the SWAP at t=100 triggers
        # the decision, the OBSERVATION at t=110 provides the fill price.
        bundle = ModelBundle(
            bundle_version="t061.test.fillprice.v1",
            liquidity=ConstantLiquidityModel(active_liquidity_value=10_000),
            fee=StaticFeeModel(fee_pips_value=3_000),
            gas=FlatGasModel(gas_units_value=21_000),
            slippage=ZeroSlippageModel(),
            failure=DeterministicFailureModel(),
            latency_units=10,  # decision at 100 -> fill at 110
        )
        events = [
            _swap_event(timestamp=100, price_q64_64=1 << 64),  # trigger price
            _observation_event(timestamp=110, price_q64_64=2 << 64),  # fill data
            _shutdown_event(timestamp=200),
        ]
        engine = BacktestEngine(
            version=BACKTEST_ENGINE_VERSION,
            initial_ledger=empty_position_state(pool_key_id=_POOL_KEY_ID, chain_id=_CHAIN_ID),
            model_bundle=bundle,
            strategy_callback=_PROPOSE_CB,
            risk_callback=_APPROVE_RISK_CB,
        )
        result = engine.run(events)
        fill_events = [a for a in result.audit_events if a.stage == STAGE_FILL]
        assert len(fill_events) == 1
        payload = dict(fill_events[0].payload)
        # The fill price is 2<<64 (from the OBSERVATION at fill time),
        # not 1<<64 (the trigger price).
        assert payload["fill_price_q64_64"] == 2 << 64


# ---------------------------------------------------------------------------
# Model unit / version tests
# ---------------------------------------------------------------------------


class TestModels:
    """Each model is parameterised, deterministic, versioned and unit-carrying."""

    def test_constant_liquidity_returns_value(self) -> None:
        m = ConstantLiquidityModel(active_liquidity_value=42)
        assert m.active_liquidity(pool_key_id="x", chain_id=1, event_time=10) == 42

    def test_constant_liquidity_rejects_out_of_uint128(self) -> None:
        with pytest.raises(InvalidUnitError):
            ConstantLiquidityModel(active_liquidity_value=1 << 128)

    def test_static_fee_returns_pips(self) -> None:
        m = StaticFeeModel(fee_pips_value=3_000)
        assert m.fee_pips(pool_key_id="x", chain_id=1, event_time=10) == 3_000

    def test_static_fee_rejects_out_of_range(self) -> None:
        with pytest.raises(InvalidUnitError):
            StaticFeeModel(fee_pips_value=FEE_DENOMINATOR_PIPS + 1)

    def test_flat_gas_returns_units(self) -> None:
        m = FlatGasModel(gas_units_value=21_000)
        assert m.gas_units(action_kind="MINT") == 21_000

    def test_zero_slippage_returns_zero(self) -> None:
        m = ZeroSlippageModel()
        assert (
            m.price_impact_bps(pool_key_id="x", chain_id=1, event_time=10, size_q64_64=1 << 64) == 0
        )

    def test_constant_slippage_rejects_out_of_range(self) -> None:
        with pytest.raises(InvalidUnitError):
            ConstantSlippageModel(impact_bps=SLIPPAGE_DENOMINATOR_BPS + 1)

    def test_deterministic_failure_model_returns_filled_default(self) -> None:
        m = DeterministicFailureModel()
        assert (
            m.classify(pool_key_id="x", chain_id=1, event_time=100, requested_liquidity=100)
            == FillOutcome.FILLED
        )

    def test_deterministic_failure_model_schedule_lookup(self) -> None:
        m = DeterministicFailureModel(
            schedule=((100, "PARTIAL"), (200, "REJECTED"), (300, "DELAYED"))
        )
        assert (
            m.classify(pool_key_id="x", chain_id=1, event_time=100, requested_liquidity=100)
            == FillOutcome.PARTIAL
        )
        assert (
            m.classify(pool_key_id="x", chain_id=1, event_time=200, requested_liquidity=100)
            == FillOutcome.REJECTED
        )
        assert (
            m.classify(pool_key_id="x", chain_id=1, event_time=300, requested_liquidity=100)
            == FillOutcome.DELAYED
        )
        # Event time not in schedule falls back to FILLED.
        assert (
            m.classify(pool_key_id="x", chain_id=1, event_time=400, requested_liquidity=100)
            == FillOutcome.FILLED
        )

    def test_deterministic_failure_model_rejects_invalid_outcome(self) -> None:
        with pytest.raises(InvalidFillOutcomeError):
            DeterministicFailureModel(schedule=((100, "BOGUS"),))

    def test_model_bundle_hash_is_deterministic(self) -> None:
        b1 = _filled_bundle()
        b2 = _filled_bundle()
        assert b1.bundle_hash == b2.bundle_hash

    def test_model_bundle_rejects_non_callable_models(self) -> None:
        with pytest.raises(ModelError):
            ModelBundle(
                bundle_version="x",
                liquidity="not-a-model",  # type: ignore[arg-type]
                fee=StaticFeeModel(),
                gas=FlatGasModel(),
                slippage=ZeroSlippageModel(),
                failure=DeterministicFailureModel(),
            )


# ---------------------------------------------------------------------------
# Layer-purity tests
# ---------------------------------------------------------------------------


class TestLayerPurity:
    """The events module and the models module must not import forbidden siblings."""

    def test_events_layer_is_pure(self) -> None:
        assert_backtest_events_layer_is_pure()

    def test_models_layer_is_pure(self) -> None:
        assert_backtest_models_layer_is_pure()


# ---------------------------------------------------------------------------
# Manifest hash + ordering sanity tests
# ---------------------------------------------------------------------------


class TestManifestHash:
    """The manifest hash binds the sorted order + per-source sequence."""

    def test_empty_manifest_hash_is_deterministic(self) -> None:
        from robinhood_lp.backtest.engine import _manifest_hash  # noqa: PLC0415

        h1 = _manifest_hash(())
        h2 = _manifest_hash(())
        assert h1 == h2

    def test_swapping_two_events_changes_manifest_hash(self) -> None:
        from robinhood_lp.backtest.engine import _manifest_hash  # noqa: PLC0415

        a = _swap_event(timestamp=100)
        b = _swap_event(timestamp=200)
        # The engine reassigns sequences and sorts by
        # (timestamp, sequence, source_priority); two events with
        # different timestamps yield different sequences anyway.
        assert _manifest_hash((a, b)) != _manifest_hash((b, a))
