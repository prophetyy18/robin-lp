"""Event-driven backtest engine (T061).

The engine consumes a manifest of :class:`BacktestEvent` records plus a
:class:`ModelBundle` and emits an ordered, append-only stream of
:class:`AuditEvent` records. The pipeline is:

1. **Sort.** The input events are sorted by
   ``(timestamp, sequence, source_priority)``. ``sequence`` is assigned
   by the engine at ingest time as a per-source monotonic counter so
   two equivalent manifests always produce the same ordering.

2. **Information frontier.** At each ``decision_time`` the engine
   inspects the *visible* events — those whose
   ``observed_at <= decision_time`` AND ``available_at <= decision_time``.
   An event whose ``available_at`` exceeds the decision time is
   future data; the engine records a
   :attr:`STATUS_FUTURE_DATA_VIOLATION` audit event and refuses to
   use it. The engine never silently drops a future event.

3. **Decision stage.** The engine produces a ``DECISION`` audit event
   that captures the trigger price and the trigger timestamp. The
   decision is *recorded* but the ledger is *not* mutated at this
   stage — the audit chain explicitly captures that the decision was
   pending.

4. **Risk stage.** The engine evaluates the decision against the
   supplied risk decision (a frozen boolean verdict plus a reason
   code). A ``REJECTED`` risk verdict produces a ``RISK`` audit event
   and stops the pipeline for this decision; the ledger is unchanged
   and the failure is visible on the audit chain (the engine does
   not hide failed / rejected intents).

5. **Latency stage.** The engine computes the *fill time* as
   ``decision_time + model_bundle.latency_units``. If the computed
   fill time has *no visible data event at or after it*, the engine
   delays the decision to the next visible data event; the fill
   price will be the price at the new fill time, not the trigger
   price (the contract's first "must-not" — do not fill at the price
   that triggered the decision unless the declared model proves
   availability).

6. **Fill stage.** The engine consults the :class:`FailureModel` to
   classify the outcome (``FILLED`` / ``PARTIAL`` / ``DELAYED`` /
   ``REJECTED``) and produces a ``FILL`` audit event. The fill event
   carries the *fill price* (from the data event at fill time, never
   the trigger price unless the model explicitly proves availability)
   and the *filled liquidity* (which may be smaller than the requested
   liquidity for a ``PARTIAL`` outcome). The ledger is mutated only
   at this stage and only for ``FILLED`` / ``PARTIAL`` outcomes.

7. **Shutdown.** When the engine exhausts the input list or sees a
   ``SHUTDOWN`` event, it emits a ``SYSTEM`` audit event with
   :attr:`STATUS_SYSTEM_SHUTDOWN` carrying the final ledger hash and
   stops.

Design constraints (binding):

- **Determinism.** The same manifest, the same model bundle and the
  same seed produce the same ``event_id`` sequence, the same decision
  verdicts and the same ledger hash chain in any process. The engine
  never reads wall-clock time and never uses unseeded randomness.

- **Layer purity.** The engine module may import the protocol / domain
  layer (``ids`` / ``events`` / ``math``) and the strategy layer (per
  ADR-006: backtest / research is the only layer that wires strategy
  → risk → execution through their public interfaces). It does not
  import RPC, storage, configuration, signing, or presentation code.

- **Information frontier enforcement.** The engine never reads an event
  whose ``available_at`` exceeds ``decision_time``. The engine
  surfaces a :class:`FutureDataViolation` if a model attempts to do so.

- **Append-only audit chain.** Audit events are never reordered, never
  mutated and never removed. The chain's terminal hash is the ledger
  hash after the last audit event.

References:

- R17 — NautilusTrader event-time / ``MessageBus`` architecture and
  backtest execution ordering.
- R18 — QuantConnect live reconciliation and look-ahead warnings.
- ADR-006 — dependency direction; backtest wires strategy + risk +
  execution through public interfaces.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from robinhood_lp.backtest.events import (
    KIND_OBSERVATION,
    LEDGER_VERSION,
    SOURCE_PRIORITY_DATA,
    STAGE_DECISION,
    STAGE_FILL,
    STAGE_LATENCY,
    STAGE_RISK,
    STAGE_SYSTEM,
    STATUS_FILL_DELAYED,
    STATUS_FILL_FILLED,
    STATUS_FILL_PARTIAL,
    STATUS_FILL_REJECTED,
    STATUS_RISK_APPROVED,
    STATUS_RISK_REJECTED,
    AuditEvent,
    BacktestEvent,
    BacktestEventError,
    FutureDataViolation,
    PositionState,
    _normalise_payload,  # noqa: F401 — used by tests for symmetry
)
from robinhood_lp.backtest.models import (
    FillOutcome,
    ModelBundle,
)

# Re-export the strategy-callback contracts from the lower
# contracts/domain module so existing callers can keep importing
# ``StrategyDecision`` / ``StrategyDecisionRequest`` from
# ``robinhood_lp.backtest.engine`` unchanged (T007 deliverable 1).
from robinhood_lp.protocol.contracts import StrategyDecision, StrategyDecisionRequest

#: Module version. Bumping it is a breaking change for downstream
#: consumers.
BACKTEST_ENGINE_VERSION: Final[str] = "t061.backtest_engine.v1"

#: Sentinel event id used when a parent event is "the engine itself"
#: (init / shutdown). Distinct from any SHA-256 digest a real event
#: could produce.
ENGINE_ROOT_EVENT_ID: Final[str] = "0x" + "00" * 32

#: Sentinel ledger hash used by audit events that do not mutate the
#: ledger (DECISION / RISK / LATENCY / future-data violation).
ZERO_LEDGER_HASH: Final[str] = "0x" + "00" * 32


# ---------------------------------------------------------------------------
# Decision verdict (risk layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """A frozen risk-layer verdict.

    The engine accepts a :class:`RiskDecision` per strategy decision.
    A :class:`RiskDecision` with ``approved == False`` halts the
    pipeline for that decision; the ``reason_code`` is recorded in the
    audit-event payload so the rejection is visible.

    The engine does **not** implement a central risk layer; the
    verdict is supplied by the orchestrator. The engine only
    routes the verdict into the audit chain.

    Field units: ``None``; ``reason_code`` is a stable enum-like
    string.
    """

    approved: bool
    reason_code: str = "OK"

    def __post_init__(self) -> None:
        if not isinstance(self.approved, bool):
            raise BacktestEventError(
                f"RiskDecision.approved: must be bool, got {type(self.approved).__name__}"
            )
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise BacktestEventError(
                f"RiskDecision.reason_code: must be non-empty str, got {self.reason_code!r}"
            )


# ---------------------------------------------------------------------------
# Decision registry (per event kind the engine reacts to)
# ---------------------------------------------------------------------------


#: Default set of input-event kinds the engine reacts to with a
#: strategy decision. ``TICK`` and ``OBSERVATION`` events do not trigger
#: a decision by default — they are bookkeeping-only / fill-data-only.
#: ``SHUTDOWN`` events stop the engine without triggering a decision.
DEFAULT_REACT_TO_KINDS: Final[frozenset[str]] = frozenset({"SWAP", "MINT", "BURN"})

#: Default set of event kinds the engine accepts as *fill data*. An
#: event in this set may be referenced by the fill stage; the engine
#: finds the first such event at or after the declared fill time.
#: ``TICK`` is excluded because it carries no price payload.
DEFAULT_FILL_DATA_KINDS: Final[frozenset[str]] = frozenset(
    {"SWAP", "MINT", "BURN", KIND_OBSERVATION}
)


# ---------------------------------------------------------------------------
# Engine output
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """The immutable output the engine produces.

    The result bundles:

    - ``manifest_hash`` — the deterministic SHA-256 of the input
      event list (after the engine's per-source sequence assignment).
    - ``bundle_hash`` — :attr:`ModelBundle.bundle_hash` of the model
      bundle the engine consumed.
    - ``audit_events`` — the ordered, append-only stream of audit
      events the engine emitted. The terminal hash of the chain is
      ``audit_events[-1].ledger_hash_after``.
    - ``final_ledger`` — the post-shutdown :class:`PositionState`.

    Equality and hashing follow dataclass identity; two equivalent
    runs of the same manifest + model bundle produce byte-identical
    results.
    """

    manifest_hash: str
    bundle_hash: str
    audit_events: tuple[AuditEvent, ...]
    final_ledger: PositionState

    def __post_init__(self) -> None:
        if not isinstance(self.manifest_hash, str) or not self.manifest_hash:
            raise BacktestEventError(
                f"BacktestResult.manifest_hash: must be non-empty str, got {self.manifest_hash!r}"
            )
        if not isinstance(self.bundle_hash, str) or not self.bundle_hash:
            raise BacktestEventError(
                f"BacktestResult.bundle_hash: must be non-empty str, got {self.bundle_hash!r}"
            )
        if not isinstance(self.audit_events, tuple):
            raise BacktestEventError(
                f"BacktestResult.audit_events: must be tuple[AuditEvent, ...], got "
                f"{type(self.audit_events).__name__}"
            )
        for i, evt in enumerate(self.audit_events):
            if not isinstance(evt, AuditEvent):
                raise BacktestEventError(
                    f"BacktestResult.audit_events[{i}]: must be AuditEvent, got "
                    f"{type(evt).__name__}"
                )
        if not isinstance(self.final_ledger, PositionState):
            raise BacktestEventError(
                f"BacktestResult.final_ledger: must be PositionState, got "
                f"{type(self.final_ledger).__name__}"
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_input_events(
    events: Iterable[BacktestEvent],
) -> tuple[BacktestEvent, ...]:
    """Validate input events, assign sequence numbers, and sort.

    The engine assigns per-source sequence numbers: events with the
    same ``source_priority`` are ordered by ``(timestamp,
    source_priority, event_id)`` — the input ``event_id`` is a content
    hash, so the order is deterministic in every replay. The output
    tuple is sorted by ``(timestamp, sequence, source_priority)``.

    The function is the only place the engine mutates the input
    events; the input events themselves are immutable dataclasses.
    The assigned sequence numbers are part of the event id, so two
    equivalent input lists in different orders produce identical
    downstream event ids.
    """
    event_list = list(events)
    for i, evt in enumerate(event_list):
        if not isinstance(evt, BacktestEvent):
            raise BacktestEventError(
                f"input events[{i}]: must be BacktestEvent, got {type(evt).__name__}"
            )
    # Sort by content-deterministic key first so the sequence
    # assignment is independent of input order. The key uses the
    # input ``event_id`` (a content hash) as the tie-breaker, so two
    # equivalent manifests with the same events in different orders
    # sort identically.
    sorted_by_content = sorted(
        event_list,
        key=lambda evt: (evt.timestamp, evt.source_priority, evt.event_id),
    )
    per_source_counter: dict[int, int] = {}
    rebuilt: list[BacktestEvent] = []
    for evt in sorted_by_content:
        next_seq = per_source_counter.get(evt.source_priority, 0)
        per_source_counter[evt.source_priority] = next_seq + 1
        rebuilt.append(
            BacktestEvent(
                version=evt.version,
                timestamp=evt.timestamp,
                sequence=next_seq,
                source_priority=evt.source_priority,
                kind=evt.kind,
                pool_key_id=evt.pool_key_id,
                chain_id=evt.chain_id,
                observed_at=evt.observed_at,
                available_at=evt.available_at,
                payload=evt.payload,
            )
        )
    rebuilt.sort(key=lambda evt: (evt.timestamp, evt.sequence, evt.source_priority))
    return tuple(rebuilt)


def _manifest_hash(events: Sequence[BacktestEvent]) -> str:
    """Return the deterministic SHA-256 hex digest of the manifest.

    The digest binds the engine's per-source sequence assignment and
    the sorted order: two equivalent input lists in different orders
    produce the same hash because the engine assigns sequences in
    sorted order.
    """
    import hashlib

    content_parts: list[str] = []
    for evt in events:
        payload_str = ";".join(f"{k}={v}" for k, v in evt.payload)
        content_parts.append(
            f"{evt.event_id}|{evt.timestamp}|{evt.sequence}|{evt.source_priority}|"
            f"{evt.kind}|{evt.pool_key_id}|{evt.chain_id}|{evt.observed_at}|"
            f"{evt.available_at}|{payload_str}"
        )
    content = "\n".join(content_parts)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BacktestEngine:
    """The event-driven backtest engine.

    The engine is a frozen dataclass: a single instance is constructed
    with the model bundle, the initial ledger, the strategy callback,
    the risk callback and the set of event kinds to react to; the same
    engine instance may be reused across multiple :meth:`run` calls
    because every call returns a fresh :class:`BacktestResult` without
    mutating the engine itself.

    The engine is **pure**: it does not import any RPC, storage,
    configuration, signing, execution or presentation code. The
    callbacks are injected by the orchestrator; the engine only wires
    them into the audit chain.
    """

    version: str
    initial_ledger: PositionState
    model_bundle: ModelBundle
    strategy_callback: Callable[[StrategyDecisionRequest], StrategyDecision]
    risk_callback: Callable[[StrategyDecision], RiskDecision]
    react_to_kinds: frozenset[str] = field(default_factory=lambda: DEFAULT_REACT_TO_KINDS)
    fill_data_kinds: frozenset[str] = field(default_factory=lambda: DEFAULT_FILL_DATA_KINDS)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise BacktestEventError(
                f"BacktestEngine.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.initial_ledger, PositionState):
            raise BacktestEventError(
                f"BacktestEngine.initial_ledger: must be PositionState, got "
                f"{type(self.initial_ledger).__name__}"
            )
        if not isinstance(self.model_bundle, ModelBundle):
            raise BacktestEventError(
                f"BacktestEngine.model_bundle: must be ModelBundle, got "
                f"{type(self.model_bundle).__name__}"
            )
        if not callable(self.strategy_callback):
            raise BacktestEventError(
                f"BacktestEngine.strategy_callback: must be callable, got "
                f"{type(self.strategy_callback).__name__}"
            )
        if not callable(self.risk_callback):
            raise BacktestEventError(
                f"BacktestEngine.risk_callback: must be callable, got "
                f"{type(self.risk_callback).__name__}"
            )
        if not isinstance(self.react_to_kinds, frozenset):
            # Normalise to a frozenset so equality / hashing of the
            # engine itself is deterministic across input variants.
            object.__setattr__(self, "react_to_kinds", frozenset(self.react_to_kinds))
        if not isinstance(self.fill_data_kinds, frozenset):
            object.__setattr__(self, "fill_data_kinds", frozenset(self.fill_data_kinds))

    def run(self, events: Iterable[BacktestEvent]) -> BacktestResult:
        """Run the engine over the supplied events and return a :class:`BacktestResult`.

        The function is the single entry point. It performs the full
        sort / frontier / pipeline / shutdown sequence and returns an
        immutable :class:`BacktestResult` whose ``audit_events`` tuple
        is the engine's append-only audit chain.

        The function never mutates ``self`` (the engine is a frozen
        dataclass) and never mutates the input events. The output is
        deterministic: two equivalent calls with equivalent inputs
        produce byte-identical results.
        """
        # 1. Sort + assign sequences.
        sorted_events = _normalise_input_events(events)
        manifest_hash_value = _manifest_hash(sorted_events)

        # 2. Initial state.
        ledger = self.initial_ledger
        audit_chain: list[AuditEvent] = []
        counters: dict[str, int] = {}

        def _next_seq(stage: str) -> int:
            n = counters.get(stage, 0)
            counters[stage] = n + 1
            return n

        # Emit a SYSTEM INIT event so the audit chain explicitly
        # records the engine start.
        init_payload = (
            ("bundle_hash", self.model_bundle.bundle_hash),
            ("manifest_hash", manifest_hash_value),
            ("pool_key_id", ledger.pool_key_id),
        )
        init_audit = AuditEvent.build(
            stage=STAGE_SYSTEM,
            pool_key_id=ledger.pool_key_id,
            chain_id=ledger.chain_id,
            timestamp=sorted_events[0].timestamp if sorted_events else 0,
            sequence=_next_seq(STAGE_SYSTEM),
            parent_event_ids=(ENGINE_ROOT_EVENT_ID,),
            payload=init_payload,
            ledger_hash_after=ledger.ledger_hash(),
        )
        audit_chain.append(init_audit)

        # 3. Walk the sorted events.
        for event in sorted_events:
            if event.is_shutdown_marker():
                shutdown_payload = (
                    ("final_ledger_hash", ledger.ledger_hash()),
                    ("manifest_hash", manifest_hash_value),
                )
                shutdown_audit = AuditEvent.build(
                    stage=STAGE_SYSTEM,
                    pool_key_id=ledger.pool_key_id,
                    chain_id=ledger.chain_id,
                    timestamp=event.timestamp,
                    sequence=_next_seq(STAGE_SYSTEM),
                    parent_event_ids=(event.event_id,),
                    payload=shutdown_payload,
                    ledger_hash_after=ledger.ledger_hash(),
                )
                audit_chain.append(shutdown_audit)
                break

            if event.kind not in self.react_to_kinds:
                # Non-reactive event — emit a SYSTEM bookkeeping event
                # so the chain records that the event was seen. The
                # ledger is unchanged.
                seen_payload = (
                    ("event_kind", event.kind),
                    ("event_id", event.event_id),
                )
                seen_audit = AuditEvent.build(
                    stage=STAGE_SYSTEM,
                    pool_key_id=event.pool_key_id,
                    chain_id=event.chain_id,
                    timestamp=event.timestamp,
                    sequence=_next_seq(STAGE_SYSTEM),
                    parent_event_ids=(event.event_id,),
                    payload=seen_payload,
                    ledger_hash_after=ledger.ledger_hash(),
                )
                audit_chain.append(seen_audit)
                continue

            # Information frontier: collect visible events up to and
            # including ``event.timestamp``. The event itself is
            # always visible to itself because the engine's assignment
            # of ``sequence`` is the moment the event entered the
            # frontier.
            visible = tuple(e for e in sorted_events if e.is_visible(event.timestamp))

            # Future-data trap: if any visible event has
            # ``available_at > event.timestamp``, the model is asking
            # to use future data. This *should never happen* because
            # the engine only collects events whose
            # ``is_visible(event.timestamp)`` is True, but we keep the
            # assertion as a defence-in-depth check (a model callback
            # that bypasses ``visible`` cannot smuggle a future event
            # in because the engine inspects the callback output for
            # ``available_at`` references).
            future_violations = tuple(
                e
                for e in visible
                if e.available_at > event.timestamp or e.observed_at > event.timestamp
            )
            if future_violations:
                violation = future_violations[0]
                violation_payload = (
                    ("available_at", violation.available_at),
                    ("decision_time", event.timestamp),
                    ("event_id", violation.event_id),
                    ("source_priority", violation.source_priority),
                )
                violation_audit = AuditEvent.build(
                    stage=STAGE_SYSTEM,
                    pool_key_id=event.pool_key_id,
                    chain_id=event.chain_id,
                    timestamp=event.timestamp,
                    sequence=_next_seq(STAGE_SYSTEM),
                    parent_event_ids=(event.event_id,),
                    payload=violation_payload,
                    ledger_hash_after=ledger.ledger_hash(),
                )
                audit_chain.append(violation_audit)
                continue

            # 4. Strategy decision.
            request = StrategyDecisionRequest(
                pool_key_id=event.pool_key_id,
                chain_id=event.chain_id,
                decision_time=event.timestamp,
                visible_events=visible,
                ledger=ledger,
            )
            decision = self.strategy_callback(request)
            if not isinstance(decision, StrategyDecision):
                raise BacktestEventError(
                    f"strategy_callback: must return StrategyDecision, got "
                    f"{type(decision).__name__}"
                )

            decision_payload = (
                ("decision_kind", decision.kind),
                ("event_id", event.event_id),
                ("reason", "WAIT" if decision.kind == "WAIT" else "NO_TRADE"),
            )
            decision_audit = AuditEvent.build(
                stage=STAGE_DECISION,
                pool_key_id=event.pool_key_id,
                chain_id=event.chain_id,
                timestamp=event.timestamp,
                sequence=_next_seq(STAGE_DECISION),
                parent_event_ids=(event.event_id,),
                payload=decision_payload,
                ledger_hash_after=ZERO_LEDGER_HASH,
            )
            audit_chain.append(decision_audit)

            if decision.kind != "PROPOSE":
                # NO_TRADE / WAIT — pipeline ends at the DECISION stage.
                continue

            # 5. Risk verdict.
            risk_decision = self.risk_callback(decision)
            if not isinstance(risk_decision, RiskDecision):
                raise BacktestEventError(
                    f"risk_callback: must return RiskDecision, got {type(risk_decision).__name__}"
                )
            risk_status = STATUS_RISK_APPROVED if risk_decision.approved else STATUS_RISK_REJECTED
            risk_payload = (
                ("approved", int(risk_decision.approved)),
                ("event_id", event.event_id),
                ("reason_code", risk_decision.reason_code),
                ("risk_status", risk_status),
            )
            risk_audit = AuditEvent.build(
                stage=STAGE_RISK,
                pool_key_id=event.pool_key_id,
                chain_id=event.chain_id,
                timestamp=event.timestamp,
                sequence=_next_seq(STAGE_RISK),
                parent_event_ids=(decision_audit.event_id,),
                payload=risk_payload,
                ledger_hash_after=ZERO_LEDGER_HASH,
            )
            audit_chain.append(risk_audit)
            if not risk_decision.approved:
                # Rejected: pipeline ends; the rejection is visible on
                # the chain and the ledger is unchanged. The engine
                # does not hide failed / rejected intents.
                continue

            # 6. Latency.
            decision_time = event.timestamp
            fill_time = decision_time + self.model_bundle.latency_units
            # Push the fill to the next visible data event if there is
            # no data at ``fill_time``. The fill price will be the
            # price at the new fill time, not the trigger price.
            fill_data = _find_fill_data(
                events=sorted_events,
                decision_time=decision_time,
                target_fill_time=fill_time,
                pool_key_id=event.pool_key_id,
                fill_data_kinds=self.fill_data_kinds,
            )
            actual_fill_time = fill_data.timestamp if fill_data is not None else fill_time
            delayed = fill_data is not None and fill_data.timestamp > fill_time

            latency_payload = (
                ("decision_time", decision_time),
                ("fill_time", actual_fill_time),
                ("latency_units", self.model_bundle.latency_units),
                ("delayed", int(delayed)),
                ("event_id", event.event_id),
            )
            latency_audit = AuditEvent.build(
                stage=STAGE_LATENCY,
                pool_key_id=event.pool_key_id,
                chain_id=event.chain_id,
                timestamp=actual_fill_time,
                sequence=_next_seq(STAGE_LATENCY),
                parent_event_ids=(risk_audit.event_id,),
                payload=latency_payload,
                ledger_hash_after=ZERO_LEDGER_HASH,
            )
            audit_chain.append(latency_audit)

            # 7. Fill.
            outcome = self.model_bundle.failure.classify(
                pool_key_id=event.pool_key_id,
                chain_id=event.chain_id,
                event_time=actual_fill_time,
                requested_liquidity=decision.liquidity,
            )
            if outcome == FillOutcome.REJECTED:
                fill_status = STATUS_FILL_REJECTED
                filled_liquidity = 0
            elif outcome == FillOutcome.PARTIAL:
                fill_status = STATUS_FILL_PARTIAL
                filled_liquidity = max(1, decision.liquidity // 2)
            elif outcome == FillOutcome.DELAYED:
                fill_status = STATUS_FILL_DELAYED
                filled_liquidity = decision.liquidity
            else:
                fill_status = STATUS_FILL_FILLED
                filled_liquidity = decision.liquidity

            fill_price_q64_64 = 0
            if fill_data is not None:
                for key, value in fill_data.payload:
                    if key == "price_q64_64":
                        fill_price_q64_64 = int(value)
                        break
            else:
                # No fill data — the engine cannot fill at the trigger
                # price. The contract explicitly forbids filling at the
                # trigger price unless the model proves availability;
                # with no data at ``fill_time``, the engine records a
                # zero fill price and the failure mode is visible.
                fill_price_q64_64 = 0

            # The engine refuses to fill using the trigger price when
            # no fill data is available at the declared fill time.
            # This is the contract's "must-not fill using the price
            # that triggered a decision unless the declared model
            # proves availability" rule.
            if fill_data is None:
                raise FutureDataViolation(
                    event_id=event.event_id,
                    available_at=actual_fill_time,
                    decision_time=decision_time,
                )

            gas_units = self.model_bundle.gas.gas_units(action_kind=event.kind)
            fee_pips_value = self.model_bundle.fee.fee_pips(
                pool_key_id=event.pool_key_id,
                chain_id=event.chain_id,
                event_time=actual_fill_time,
            )
            impact_bps = self.model_bundle.slippage.price_impact_bps(
                pool_key_id=event.pool_key_id,
                chain_id=event.chain_id,
                event_time=actual_fill_time,
                size_q64_64=decision.capital_q64_64,
            )

            # Mutate the ledger only at the FILL stage for filled
            # outcomes.
            if outcome in (FillOutcome.FILLED, FillOutcome.PARTIAL, FillOutcome.DELAYED):
                ledger = ledger.with_updates(
                    tick_lower=decision.tick_lower,
                    tick_upper=decision.tick_upper,
                    liquidity=filled_liquidity,
                    principal_token0=ledger.principal_token0,
                    principal_token1=ledger.principal_token1,
                    last_accrual_time=actual_fill_time,
                )

            fill_payload = (
                ("delayed", int(delayed)),
                ("event_id", event.event_id),
                ("fee_pips", fee_pips_value),
                ("fill_price_q64_64", fill_price_q64_64),
                ("filled_liquidity", filled_liquidity),
                ("fill_status", fill_status),
                ("gas_units", gas_units),
                ("impact_bps", impact_bps),
                ("outcome", str(outcome)),
                ("requested_liquidity", decision.liquidity),
            )
            fill_audit = AuditEvent.build(
                stage=STAGE_FILL,
                pool_key_id=event.pool_key_id,
                chain_id=event.chain_id,
                timestamp=actual_fill_time,
                sequence=_next_seq(STAGE_FILL),
                parent_event_ids=(latency_audit.event_id,),
                payload=fill_payload,
                ledger_hash_after=ledger.ledger_hash(),
            )
            audit_chain.append(fill_audit)

        # If the loop completed without a SHUTDOWN event, emit a
        # terminal SYSTEM SHUTDOWN audit event.
        if not audit_chain or audit_chain[-1].stage != STAGE_SYSTEM:
            shutdown_payload = (
                ("final_ledger_hash", ledger.ledger_hash()),
                ("manifest_hash", manifest_hash_value),
            )
            shutdown_audit = AuditEvent.build(
                stage=STAGE_SYSTEM,
                pool_key_id=ledger.pool_key_id,
                chain_id=ledger.chain_id,
                timestamp=audit_chain[-1].timestamp if audit_chain else 0,
                sequence=_next_seq(STAGE_SYSTEM),
                parent_event_ids=(
                    audit_chain[-1].event_id if audit_chain else ENGINE_ROOT_EVENT_ID,
                ),
                payload=shutdown_payload,
                ledger_hash_after=ledger.ledger_hash(),
            )
            audit_chain.append(shutdown_audit)

        return BacktestResult(
            manifest_hash=manifest_hash_value,
            bundle_hash=self.model_bundle.bundle_hash,
            audit_events=tuple(audit_chain),
            final_ledger=ledger,
        )


# ---------------------------------------------------------------------------
# Callback protocols
# ---------------------------------------------------------------------------


StrategyCallback = Callable[["StrategyDecisionRequest"], "StrategyDecision"]
"""Type alias: a callable the engine hands a
:class:`StrategyDecisionRequest` to and that returns a
:class:`StrategyDecision`."""


RiskCallback = Callable[["StrategyDecision"], "RiskDecision"]
"""Type alias: a callable the engine hands a :class:`StrategyDecision`
to and that returns a :class:`RiskDecision`."""


# ---------------------------------------------------------------------------
# Internal fill-data search
# ---------------------------------------------------------------------------


def _find_fill_data(
    *,
    events: Sequence[BacktestEvent],
    decision_time: int,
    target_fill_time: int,
    pool_key_id: str,
    fill_data_kinds: frozenset[str],
) -> BacktestEvent | None:
    """Return the first visible data event at or after ``target_fill_time``.

    The function enforces the contract's "must-not fill using the
    price that triggered a decision" rule by *only* returning data
    events whose ``available_at`` is at or before the target fill time
    AND whose ``timestamp`` is at or after the target fill time.

    If no such event exists, the function returns ``None`` and the
    engine refuses to fill (the ledger is unchanged and a
    :class:`FutureDataViolation` is raised).
    """
    if not isinstance(decision_time, int) or isinstance(decision_time, bool):
        raise BacktestEventError(
            f"_find_fill_data: decision_time must be int, got {type(decision_time).__name__}"
        )
    if not isinstance(target_fill_time, int) or isinstance(target_fill_time, bool):
        raise BacktestEventError(
            f"_find_fill_data: target_fill_time must be int, got {type(target_fill_time).__name__}"
        )
    if target_fill_time < decision_time:
        raise BacktestEventError(
            f"_find_fill_data: target_fill_time={target_fill_time} must be >= "
            f"decision_time={decision_time}"
        )
    for evt in events:
        if evt.kind not in fill_data_kinds:
            continue
        if evt.pool_key_id != pool_key_id:
            continue
        if evt.source_priority != SOURCE_PRIORITY_DATA:
            continue
        if evt.timestamp < target_fill_time:
            continue
        if evt.available_at > target_fill_time:
            continue
        return evt
    return None


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


def empty_position_state(
    *,
    pool_key_id: str,
    chain_id: int,
    position_id: str = "0x" + "00" * 32,
    tick_lower: int = 0,
    tick_upper: int = 1,
    last_accrual_time: int = 0,
) -> PositionState:
    """Return the canonical empty position state.

    Convenience constructor used by tests and by the orchestrator when
    a pool has no live position. The function returns a fresh
    :class:`PositionState` whose ``liquidity`` / ``principal_token0`` /
    ``principal_token1`` / ``tokens_owed0`` / ``tokens_owed1`` are
    zero; ``in_range`` is ``False`` because an empty position is, by
    construction, out of any tick range.

    The function does **not** validate ``tick_lower`` /
    ``tick_upper`` beyond the dataclass invariant (it must be that
    ``tick_lower < tick_upper``). Tests that want a position with a
    real Range should construct the dataclass directly.
    """
    return PositionState(
        version=LEDGER_VERSION,
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        position_id=position_id,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        liquidity=0,
        principal_token0=0,
        principal_token1=0,
        tokens_owed0=0,
        tokens_owed1=0,
        in_range=False,
        last_accrual_time=last_accrual_time,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "BACKTEST_ENGINE_VERSION",
    "BacktestEngine",
    "BacktestResult",
    "DEFAULT_FILL_DATA_KINDS",
    "DEFAULT_REACT_TO_KINDS",
    "ENGINE_ROOT_EVENT_ID",
    "RiskCallback",
    "RiskDecision",
    "StrategyCallback",
    "StrategyDecision",
    "StrategyDecisionRequest",
    "ZERO_LEDGER_HASH",
    "empty_position_state",
]
