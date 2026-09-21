"""Event clock, audit events, and the information frontier (T061).

The backtest engine processes events in a strictly deterministic order and
emits one immutable :class:`AuditEvent` per pipeline stage. The clock and
ordering rules are the contract every consumer and reproducer relies on.

Three rules bind the engine (per the T061 contract):

1. **Ordered event clock.** Input events are sorted by the tuple
   ``(timestamp, sequence, source_priority)``. ``timestamp`` is event time
   (typically Unix seconds, but any non-negative integer the manifest
   fixes); ``sequence`` is a per-source monotonic counter that disambiguates
   events from the same source at the same timestamp; ``source_priority``
   breaks ties between sources at the same timestamp. The lower the tuple,
   the earlier the event is processed. Same-timestamp ordering is
   deterministic in every replay.

2. **Information frontier.** At every decision point ``T``, the engine
   only consumes events whose ``observed_at <= T`` AND
   ``available_at <= T``. An event whose ``available_at`` exceeds ``T`` is
   *future data*; the engine records a :class:`FutureDataViolation` audit
   event and refuses to consume it.

3. **Immutable audit events.** Every state transition in the pipeline
   (decision, risk, latency, fill, system) produces exactly one
   :class:`AuditEvent`. The event is a frozen dataclass with a
   deterministically computed ``event_id`` (SHA-256 of its canonical
   content), its ``parent_event_ids`` (the chain that produced it), its
   ``stage``, its ``timestamp``, its ``payload`` and a
   ``ledger_hash_after`` that pins the post-event ledger. Audit events are
   append-only: once written, they are never mutated or removed.

Design constraints (binding):

- **Integer-only accounting path.** Per ADR-004, no ``float`` appears on
  the protocol / accounting path. All Q64.64 / pip / basis-point values
  are Python ``int``.

- **Determinism.** The same manifest — same input event list, same model
  versions, same seed — produces the same ``event_id`` sequence, the same
  decisions, and the same ledger hash chain in any process. ``id()``,
  ``set`` iteration order, ``dict`` ordering and any wall-clock read are
  forbidden inside this module.

- **Layer purity.** The module depends only on the standard library and
  on the protocol-domain contracts module
  (:mod:`robinhood_lp.protocol.contracts`) which carries the input-event
  shape both the engine and the strategy layer genuinely share. The
  engine module wires strategy / risk / execution together, but the
  events themselves are protocol-level value objects. The dependency
  tests walk the live module graph and reject any sibling import.

References:

- R17 — NautilusTrader event-time / ``MessageBus`` architecture
  (``https://nautilustrader.io/docs/latest/concepts/overview/`` and the
  backtest execution ordering page).
- R18 — QuantConnect live reconciliation and look-ahead warnings
  (``https://www.quantconnect.com/docs/v1/live-trading/live-reconciliation``).
- ADR-006 — dependency direction; backtest wires strategy + risk +
  execution but the event primitives stay layer-independent.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

# Re-export the input-event primitives and the closed vocabularies the
# engine consumes from the lower contracts/domain module. The canonical
# definitions live in :mod:`robinhood_lp.protocol.contracts` so the
# strategy layer can import them without depending on this module
# (ADR-006 §"Decision": sibling implementations do not import one
# another; a genuinely shared type moves into a lower contracts/domain
# module).
from robinhood_lp.protocol.contracts import (
    BACKTEST_EVENT_VERSION,
    KIND_BURN,
    KIND_MINT,
    KIND_OBSERVATION,
    KIND_SHUTDOWN,
    KIND_SWAP,
    KIND_TICK,
    SOURCE_PRIORITY_DATA,
    SOURCE_PRIORITY_EXECUTION,
    SOURCE_PRIORITY_RISK,
    SOURCE_PRIORITY_STRATEGY,
    SOURCE_PRIORITY_SYSTEM,
    BacktestEvent,
    BacktestEventError,
)

# ---------------------------------------------------------------------------
# Stage constants (audit events)
# ---------------------------------------------------------------------------
#
# The pipeline stages that produce audit events. The order is the natural
# flow: a strategy decision produces a ``DECISION`` event; the risk layer
# produces a ``RISK`` event; the latency model produces a ``LATENCY``
# event; the fill model produces a ``FILL`` event; the engine itself
# produces ``SYSTEM`` events for initialisation, shutdown, and violations.

STAGE_DECISION: Final[str] = "DECISION"
STAGE_RISK: Final[str] = "RISK"
STAGE_LATENCY: Final[str] = "LATENCY"
STAGE_FILL: Final[str] = "FILL"
STAGE_SYSTEM: Final[str] = "SYSTEM"

_VALID_STAGES: Final[frozenset[str]] = frozenset(
    {STAGE_DECISION, STAGE_RISK, STAGE_LATENCY, STAGE_FILL, STAGE_SYSTEM}
)

#: Status sentinels for ``AuditEvent`` payloads. The ``DECISION`` /
#: ``RISK`` / ``LATENCY`` / ``FILL`` stages each carry a status that the
#: downstream reader can summarise without re-parsing the payload.
STATUS_DECISION_RECORDED: Final[str] = "DECISION_RECORDED"
STATUS_RISK_APPROVED: Final[str] = "RISK_APPROVED"
STATUS_RISK_REJECTED: Final[str] = "RISK_REJECTED"
STATUS_LATENCY_RECORDED: Final[str] = "LATENCY_RECORDED"
STATUS_FILL_FILLED: Final[str] = "FILL_FILLED"
STATUS_FILL_PARTIAL: Final[str] = "FILL_PARTIAL"
STATUS_FILL_DELAYED: Final[str] = "FILL_DELAYED"
STATUS_FILL_REJECTED: Final[str] = "FILL_REJECTED"  # by execution-layer model
STATUS_SYSTEM_INIT: Final[str] = "SYSTEM_INIT"
STATUS_SYSTEM_SHUTDOWN: Final[str] = "SYSTEM_SHUTDOWN"
STATUS_FUTURE_DATA_VIOLATION: Final[str] = "FUTURE_DATA_VIOLATION"

#: Module denylist for the layer-purity check. The backtest events
#: module depends only on the standard library and on the protocol-domain
#: contracts module; importing any other ``robinhood_lp`` subpackage
#: here is a contract break that must be reviewed.
_FORBIDDEN_BACKTEST_EVENTS_ROBINHOOD_MODULES: Final[tuple[str, ...]] = (
    "robinhood_lp.backtest.engine",
    "robinhood_lp.backtest.models",
    "robinhood_lp.backtest.pipeline",
    "robinhood_lp.config",
    "robinhood_lp.discovery",
    "robinhood_lp.features",
    "robinhood_lp.ingestion",
    "robinhood_lp.presentation",
    "robinhood_lp.qualification",
    "robinhood_lp.quality",
    "robinhood_lp.replay",
    "robinhood_lp.rpc",
    "robinhood_lp.risk",
    "robinhood_lp.storage",
    "robinhood_lp.strategy",
    "robinhood_lp.execution",
)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------
#
# ``BacktestEventError`` is re-exported from the lower contracts/domain
# module :mod:`robinhood_lp.protocol.contracts` above; the audit-event
# / ledger specific subclasses are declared here.


class InvalidSourcePriorityError(BacktestEventError):
    """A ``source_priority`` value is outside the closed set."""


class InvalidStageError(BacktestEventError):
    """An audit-event ``stage`` value is outside the closed set."""


class InvalidPayloadError(BacktestEventError):
    """A payload carries a non-scalar value, a duplicate key, or a non-string key."""


class FutureDataViolation(BacktestEventError):
    """A model attempted to consume an event whose ``available_at`` exceeds the
    decision time. The engine records a violation audit event and refuses to
    use the future event; the engine does not silently drop the event."""

    def __init__(self, *, event_id: str, available_at: int, decision_time: int) -> None:
        self.event_id: Final[str] = event_id
        self.available_at: Final[int] = available_at
        self.decision_time: Final[int] = decision_time
        super().__init__(
            f"future-data violation: event_id={event_id} has available_at="
            f"{available_at} > decision_time={decision_time}"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_non_negative_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BacktestEventError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise BacktestEventError(f"{field}: must be non-negative, got {value}")
    return value


def _require_positive_int(value: int, *, field: str) -> int:
    value = _require_non_negative_int(value, field=field)
    if value == 0:
        raise BacktestEventError(f"{field}: must be positive, got 0")
    return value


def _normalise_payload(
    payload: Mapping[str, int | str | bool] | Sequence[tuple[str, int | str | bool]],
) -> tuple[tuple[str, int | str | bool], ...]:
    """Return a canonical, deterministically ordered payload.

    The engine accepts either a ``Mapping`` or a ``Sequence`` of pairs.
    The returned tuple is sorted by key (string) so two equivalent payloads
    in different insertion orders hash to the same event id. Keys must be
    non-empty strings; values must be ``int`` / ``str`` / ``bool``. ``float``
    is rejected outright so a non-statistical consumer cannot smuggle an
    NaN or infinite value into an audit event.
    """
    if isinstance(payload, Mapping):
        items: Iterable[tuple[str, object]] = payload.items()
    else:
        items = []
        for entry in payload:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise InvalidPayloadError(
                    f"payload entry: must be (str, scalar) tuple, got {entry!r}"
                )
            items.append((entry[0], entry[1]))
    seen: set[str] = set()
    out: list[tuple[str, int | str | bool]] = []
    for key, value in items:
        if not isinstance(key, str) or not key:
            raise InvalidPayloadError(f"payload key: must be non-empty str, got {key!r}")
        if key in seen:
            raise InvalidPayloadError(f"payload: key {key!r} appears more than once")
        seen.add(key)
        if isinstance(value, bool):
            coerced: int | str | bool = value
        elif isinstance(value, (int, str)):
            coerced = value
        else:
            raise InvalidPayloadError(
                f"payload[{key!r}]: must be int/str/bool scalar, got {type(value).__name__}"
            )
        out.append((key, coerced))
    out.sort(key=lambda kv: kv[0])
    return tuple(out)


# ---------------------------------------------------------------------------
# Content-addressed event identifiers
# ---------------------------------------------------------------------------


def _canonical_audit_content(
    *,
    version: str,
    stage: str,
    timestamp: int,
    sequence: int,
    pool_key_id: str,
    chain_id: int,
    parent_event_ids: tuple[str, ...],
    payload: tuple[tuple[str, int | str | bool], ...],
    ledger_hash_after: str,
) -> str:
    """Return the canonical string the audit-event-id hash binds to.

    ``parent_event_ids`` are sorted lexicographically before hashing so two
    equivalent event chains in different parent order produce the same id.
    The sort is stable and Python's tuple comparison is deterministic.
    """
    payload_str = ";".join(f"{k}={v}" for k, v in payload)
    parents_str = ",".join(sorted(parent_event_ids))
    return (
        f"{version}|{stage}|{timestamp}|{sequence}|{pool_key_id}|{chain_id}|"
        f"{parents_str}|{ledger_hash_after}|{payload_str}"
    )


def _hash_hex(content: str) -> str:
    """Return the canonical SHA-256 hex digest with the ``0x`` prefix."""
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Ledger (per-position state the engine maintains)
# ---------------------------------------------------------------------------


#: Version string carried by every ledger snapshot. Bumping it is a
#: breaking change.
LEDGER_VERSION: Final[str] = "t061.position_state.v1"


@dataclass(frozen=True, slots=True)
class PositionState:
    """The per-position ledger state the engine maintains.

    The state is a frozen dataclass; equality and hashing follow dataclass
    identity so two equivalent states hash the same way in any process.
    The ``in_range`` flag distinguishes out-of-range (zero accrual)
    from in-range (accrual active) periods; the engine records an
    explicit audit event whenever the flag flips.

    ``tokens_owed0`` / ``tokens_owed1`` carry the *uncollected* fees
    (atomic token units). The engine accumulates them during in-range
    periods and never spends them at the fill stage — fills are
    liquidity movements; fees are tracked separately and surfaced by
    attribution in a later task.
    """

    version: str
    pool_key_id: str
    chain_id: int
    position_id: str
    tick_lower: int
    tick_upper: int
    liquidity: int
    principal_token0: int
    principal_token1: int
    tokens_owed0: int
    tokens_owed1: int
    in_range: bool
    last_accrual_time: int

    def __post_init__(self) -> None:
        if self.version != LEDGER_VERSION:
            raise BacktestEventError(
                f"PositionState.version: must be {LEDGER_VERSION!r}, got {self.version!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise BacktestEventError(
                f"PositionState.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        _require_positive_int(self.chain_id, field="PositionState.chain_id")
        if not isinstance(self.position_id, str) or not self.position_id:
            raise BacktestEventError(
                f"PositionState.position_id: must be non-empty str, got {self.position_id!r}"
            )
        if self.tick_lower >= self.tick_upper:
            raise BacktestEventError(
                f"PositionState: tick_lower={self.tick_lower} must be strictly "
                f"less than tick_upper={self.tick_upper}"
            )
        _require_non_negative_int(self.liquidity, field="PositionState.liquidity")
        _require_non_negative_int(self.principal_token0, field="PositionState.principal_token0")
        _require_non_negative_int(self.principal_token1, field="PositionState.principal_token1")
        _require_non_negative_int(self.tokens_owed0, field="PositionState.tokens_owed0")
        _require_non_negative_int(self.tokens_owed1, field="PositionState.tokens_owed1")
        if not isinstance(self.in_range, bool):
            raise BacktestEventError(
                f"PositionState.in_range: must be bool, got {type(self.in_range).__name__}"
            )
        _require_non_negative_int(self.last_accrual_time, field="PositionState.last_accrual_time")

    def ledger_hash(self) -> str:
        """Return the deterministic SHA-256 hex digest of the ledger state."""
        content = (
            f"{self.version}|{self.pool_key_id}|{self.chain_id}|{self.position_id}|"
            f"{self.tick_lower}|{self.tick_upper}|{self.liquidity}|"
            f"{self.principal_token0}|{self.principal_token1}|"
            f"{self.tokens_owed0}|{self.tokens_owed1}|"
            f"{int(self.in_range)}|{self.last_accrual_time}"
        )
        return _hash_hex(content)

    def with_updates(self, **changes: int | bool) -> PositionState:
        """Return a new state with the supplied fields replaced.

        Fields not listed are inherited. ``None`` is rejected: an explicit
        ``False`` / ``0`` is the supported way to "zero" a field. The
        function is the only supported mutation surface; the dataclass
        itself is frozen.
        """
        version: str = self.version
        pool_key_id: str = self.pool_key_id
        chain_id: int = self.chain_id
        position_id: str = self.position_id
        tick_lower: int = self.tick_lower
        tick_upper: int = self.tick_upper
        liquidity: int = self.liquidity
        principal_token0: int = self.principal_token0
        principal_token1: int = self.principal_token1
        tokens_owed0: int = self.tokens_owed0
        tokens_owed1: int = self.tokens_owed1
        in_range: bool = self.in_range
        last_accrual_time: int = self.last_accrual_time
        valid = {
            "version",
            "pool_key_id",
            "chain_id",
            "position_id",
            "tick_lower",
            "tick_upper",
            "liquidity",
            "principal_token0",
            "principal_token1",
            "tokens_owed0",
            "tokens_owed1",
            "in_range",
            "last_accrual_time",
        }
        for k in changes:
            if k not in valid:
                raise BacktestEventError(f"PositionState.with_updates: unknown field {k!r}")
        if "version" in changes:
            assert isinstance(changes["version"], str)
            version = changes["version"]
        if "pool_key_id" in changes:
            assert isinstance(changes["pool_key_id"], str)
            pool_key_id = changes["pool_key_id"]
        if "chain_id" in changes:
            assert isinstance(changes["chain_id"], int)
            chain_id = changes["chain_id"]
        if "position_id" in changes:
            assert isinstance(changes["position_id"], str)
            position_id = changes["position_id"]
        if "tick_lower" in changes:
            assert isinstance(changes["tick_lower"], int)
            tick_lower = changes["tick_lower"]
        if "tick_upper" in changes:
            assert isinstance(changes["tick_upper"], int)
            tick_upper = changes["tick_upper"]
        if "liquidity" in changes:
            assert isinstance(changes["liquidity"], int)
            liquidity = changes["liquidity"]
        if "principal_token0" in changes:
            assert isinstance(changes["principal_token0"], int)
            principal_token0 = changes["principal_token0"]
        if "principal_token1" in changes:
            assert isinstance(changes["principal_token1"], int)
            principal_token1 = changes["principal_token1"]
        if "tokens_owed0" in changes:
            assert isinstance(changes["tokens_owed0"], int)
            tokens_owed0 = changes["tokens_owed0"]
        if "tokens_owed1" in changes:
            assert isinstance(changes["tokens_owed1"], int)
            tokens_owed1 = changes["tokens_owed1"]
        if "in_range" in changes:
            assert isinstance(changes["in_range"], bool)
            in_range = changes["in_range"]
        if "last_accrual_time" in changes:
            assert isinstance(changes["last_accrual_time"], int)
            last_accrual_time = changes["last_accrual_time"]
        return PositionState(
            version=version,
            pool_key_id=pool_key_id,
            chain_id=chain_id,
            position_id=position_id,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=liquidity,
            principal_token0=principal_token0,
            principal_token1=principal_token1,
            tokens_owed0=tokens_owed0,
            tokens_owed1=tokens_owed1,
            in_range=in_range,
            last_accrual_time=last_accrual_time,
        )


# ---------------------------------------------------------------------------
# AuditEvent (state-transition record the engine emits)
# ---------------------------------------------------------------------------


#: Version string carried by every audit event. Bumping it is a breaking
#: change.
AUDIT_EVENT_VERSION: Final[str] = "t061.audit_event.v1"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """An immutable state-transition record the engine emits.

    The engine produces exactly one :class:`AuditEvent` per pipeline
    stage. The event is append-only: once written it is never mutated or
    removed, and the engine never reorders two events of the same stage
    and pool. The ``event_id`` is the SHA-256 hex digest of the
    canonical serialisation of every field, prefixed by ``0x``.

    The fields are:

    - ``event_id`` — deterministic hash; identical content always
      produces the same id in any process.
    - ``parent_event_ids`` — the event ids that *produced* this event
      (input events for a ``DECISION``; the decision for the ``RISK``
      it ran against; the risk verdict for the ``LATENCY`` it computed;
      the latency event for the ``FILL`` it produced; or an empty tuple
      for ``SYSTEM`` init / shutdown). Sorted lexicographically before
      hashing so equivalent chains hash identically.
    - ``stage`` — :data:`STAGE_DECISION` / :data:`STAGE_RISK` /
      :data:`STAGE_LATENCY` / :data:`STAGE_FILL` / :data:`STAGE_SYSTEM`.
    - ``timestamp`` — when the stage completed. For ``RISK`` /
      ``DECISION`` stages this is the decision time; for ``LATENCY``
      and ``FILL`` it is the (possibly delayed) fill time.
    - ``sequence`` — per-stage per-pool monotonic counter.
    - ``ledger_hash_after`` — :meth:`PositionState.ledger_hash` of the
      post-event ledger; ``"0x" + 64 zeros`` if the stage did not touch
      the ledger (decision / risk / latency).
    - ``payload`` — stage-specific structured fields, sorted by key.

    Equality and hashing follow dataclass identity.
    """

    version: str
    event_id: str
    parent_event_ids: tuple[str, ...]
    stage: str
    pool_key_id: str
    chain_id: int
    timestamp: int
    sequence: int
    payload: tuple[tuple[str, int | str | bool], ...]
    ledger_hash_after: str
    cursor: tuple[int, int, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise BacktestEventError(
                f"AuditEvent.version: must be non-empty str, got {self.version!r}"
            )
        if self.stage not in _VALID_STAGES:
            raise InvalidStageError(
                f"AuditEvent.stage: must be one of {sorted(_VALID_STAGES)}, got {self.stage!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise BacktestEventError(
                f"AuditEvent.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        _require_positive_int(self.chain_id, field="AuditEvent.chain_id")
        _require_non_negative_int(self.timestamp, field="AuditEvent.timestamp")
        _require_non_negative_int(self.sequence, field="AuditEvent.sequence")
        if not isinstance(self.parent_event_ids, tuple):
            raise BacktestEventError(
                f"AuditEvent.parent_event_ids: must be tuple[str, ...], got "
                f"{type(self.parent_event_ids).__name__}"
            )
        for parent in self.parent_event_ids:
            if not isinstance(parent, str) or not parent:
                raise BacktestEventError(
                    f"AuditEvent.parent_event_ids: every entry must be non-empty "
                    f"str, got {parent!r}"
                )
        if not isinstance(self.ledger_hash_after, str) or not self.ledger_hash_after:
            raise BacktestEventError(
                f"AuditEvent.ledger_hash_after: must be non-empty str, got "
                f"{self.ledger_hash_after!r}"
            )
        if not isinstance(self.event_id, str) or not self.event_id:
            raise BacktestEventError(
                f"AuditEvent.event_id: must be non-empty str, got {self.event_id!r}"
            )
        # Normalise the cursor: ``-1`` sentinel for end-of-block; otherwise
        # ``(block_number, transaction_index, log_index)``. The cursor
        # field does **not** participate in ``event_id`` hashing so legacy
        # audit chains remain byte-identical; the cursor is metadata the
        # T109 simulation-evidence artifact binds to its
        # :class:`RunTransition` records.
        if self.cursor is not None:
            if not isinstance(self.cursor, tuple) or len(self.cursor) != 3:
                raise BacktestEventError(
                    f"AuditEvent.cursor: must be (block, tx, log) tuple or "
                    f"None, got {self.cursor!r}"
                )
            for value in self.cursor:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise BacktestEventError(
                        f"AuditEvent.cursor: every entry must be int, got {value!r}"
                    )
                if value < -1:
                    raise BacktestEventError(
                        f"AuditEvent.cursor: every entry must be >= -1, got {value}"
                    )
        # Normalise the payload once at construction time so two equivalent
        # payloads hash to the same id regardless of insertion order.
        normalised = _normalise_payload(self.payload)
        object.__setattr__(self, "payload", normalised)

    @classmethod
    def build(
        cls,
        *,
        stage: str,
        pool_key_id: str,
        chain_id: int,
        timestamp: int,
        sequence: int,
        parent_event_ids: Sequence[str],
        payload: Mapping[str, int | str | bool] | Sequence[tuple[str, int | str | bool]],
        ledger_hash_after: str,
        cursor: tuple[int, int, int] | None = None,
    ) -> AuditEvent:
        """Construct an :class:`AuditEvent` with a deterministic event id.

        The event id is computed from the canonical serialisation of the
        supplied fields. Two constructions with equal inputs always
        produce the same id in any process.

        ``cursor`` is the optional T109 canonical cursor binding. The
        cursor does **not** participate in ``event_id`` hashing so legacy
        audit chains remain byte-identical across the T109 cutover; the
        cursor is the canonical ``(block_number, transaction_index,
        log_index)`` triple the engine observed at emission time.
        """
        if stage not in _VALID_STAGES:
            raise InvalidStageError(
                f"AuditEvent.build: stage must be one of {sorted(_VALID_STAGES)}, got {stage!r}"
            )
        normalised_parents = tuple(sorted(parent_event_ids))
        normalised_payload = _normalise_payload(payload)
        event_id = _hash_hex(
            _canonical_audit_content(
                version=AUDIT_EVENT_VERSION,
                stage=stage,
                timestamp=timestamp,
                sequence=sequence,
                pool_key_id=pool_key_id,
                chain_id=chain_id,
                parent_event_ids=normalised_parents,
                payload=normalised_payload,
                ledger_hash_after=ledger_hash_after,
            )
        )
        return cls(
            version=AUDIT_EVENT_VERSION,
            event_id=event_id,
            parent_event_ids=normalised_parents,
            stage=stage,
            pool_key_id=pool_key_id,
            chain_id=chain_id,
            timestamp=timestamp,
            sequence=sequence,
            payload=normalised_payload,
            ledger_hash_after=ledger_hash_after,
            cursor=cursor,
        )


# ---------------------------------------------------------------------------
# Layer-purity check (mirrors robinhood_lp.strategy.base.assert_*)
# ---------------------------------------------------------------------------


def _is_backtest_events_module(name: str) -> bool:
    return (
        name == "robinhood_lp.backtest.events"
        or name.startswith("robinhood_lp.backtest.events.")
        or name == "robinhood_lp.protocol.contracts"
        or name.startswith("robinhood_lp.protocol.contracts.")
    )


def assert_backtest_events_layer_is_pure() -> None:
    """Raise :class:`BacktestEventError` if this module imports a forbidden sibling.

    The denylist is the closed set recorded in
    :data:`_FORBIDDEN_BACKTEST_EVENTS_ROBINHOOD_MODULES`. The check is
    conservative on purpose; the backtest events module is supposed to
    be a stdlib + protocol-contracts-only layer, so any ``robinhood_lp``
    sibling import other than the protocol-domain contracts module is a
    contract break that must be reviewed.
    """
    import importlib

    module = importlib.import_module("robinhood_lp.backtest.events")
    forbidden: list[tuple[str, str]] = []
    for _attr_name, attr in vars(module).items():
        mod_name = getattr(attr, "__name__", None)
        if not isinstance(mod_name, str):
            continue
        if _is_backtest_events_module(mod_name):
            continue
        for forbidden_root in _FORBIDDEN_BACKTEST_EVENTS_ROBINHOOD_MODULES:
            if mod_name == forbidden_root or mod_name.startswith(forbidden_root + "."):
                forbidden.append(("robinhood_lp.backtest.events", mod_name))
                break
    if forbidden:
        rendered = "\n".join(f"  {src}: imports {imp}" for src, imp in forbidden)
        raise BacktestEventError(f"backtest events module imports forbidden modules:\n{rendered}")


# ---------------------------------------------------------------------------
# Cursor extraction helper (T109)
# ---------------------------------------------------------------------------


def extract_event_cursor(event: BacktestEvent) -> tuple[int, int, int] | None:
    """Return the canonical :class:`MarketCursor` triple for ``event``.

    The T109 contract binds every transition to the exact canonical
    cursor the engine observed at emission time, never derived later
    from the integer timestamp. A :class:`BacktestEvent` may carry
    its cursor through the optional ``block_number`` /
    ``transaction_index`` / ``log_index`` payload attributes; when
    absent, the engine falls back to the legacy
    ``(timestamp, 0, 0)`` derivation only when the event is a
    system-level bookkeeping entry whose cursor is structurally
    undefined (the engine emits the cursor as ``None`` so the
    evidence layer treats it as the system-init / shutdown marker).

    Returns ``None`` for events that carry no market binding (e.g.
    ``SHUTDOWN`` / non-reactive bookkeeping events the engine skips).
    """
    if not isinstance(event, BacktestEvent):
        raise BacktestEventError(
            f"extract_event_cursor: event must be BacktestEvent, got {type(event).__name__}"
        )
    block_number = getattr(event, "block_number", None)
    transaction_index = getattr(event, "transaction_index", None)
    log_index = getattr(event, "log_index", None)
    if (
        isinstance(block_number, int)
        and not isinstance(block_number, bool)
        and block_number >= 0
        and isinstance(transaction_index, int)
        and not isinstance(transaction_index, bool)
        and transaction_index >= 0
        and isinstance(log_index, int)
        and not isinstance(log_index, bool)
        and log_index >= 0
    ):
        return (block_number, transaction_index, log_index)
    if event.kind == KIND_SHUTDOWN:
        # SHUTDOWN is a system marker; the engine already broke out of
        # the main loop so the cursor is not used.
        return None
    return None


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "AUDIT_EVENT_VERSION",
    "AuditEvent",
    "BACKTEST_EVENT_VERSION",
    "BacktestEvent",
    "BacktestEventError",
    "FutureDataViolation",
    "InvalidPayloadError",
    "InvalidSourcePriorityError",
    "InvalidStageError",
    "KIND_BURN",
    "KIND_MINT",
    "KIND_OBSERVATION",
    "KIND_SHUTDOWN",
    "KIND_SWAP",
    "KIND_TICK",
    "LEDGER_VERSION",
    "PositionState",
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
    "assert_backtest_events_layer_is_pure",
    "extract_event_cursor",
]
