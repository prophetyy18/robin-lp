"""Shared strategy/backtest contracts (T007).

This module is the *lower contracts/domain* home the engine and the
baselines share: the strategy-callback request, the strategy-callback
response, the input :class:`BacktestEvent` record, and the closed
vocabularies of ``kind`` and ``source_priority`` constants both
modules consume. Per ADR-006 §"Decision", sibling implementations do
not import one another; a genuinely shared type moves into a lower
contracts/domain module instead of creating a sideways dependency.

The module sits in the protocol/domain layer; it imports no I/O, no
time, no random sources, no RPC, no storage, no configuration, no
signing, and no execution. It is stdlib-only and deterministic.

The original modules that previously owned these types — the backtest
engine / events modules and the strategy baselines — continue to
expose them under their existing names so the existing tests and
callers do not need to change. The canonical definitions live here;
the original modules re-export from this module so a caller that
imports from ``robinhood_lp.backtest.engine`` and a caller that
imports from ``robinhood_lp.protocol.contracts`` see the same
frozen dataclass instances and the same constant values.

The ``event_id`` field of :class:`BacktestEvent` is a SHA-256 hex
digest of the canonical serialisation the engine's ``_manifest_hash``
helper also computes; the canonical form is owned by this module so
the hash is reproducible from the protocol surface alone.

References:

- ADR-006 §"Decision" — sibling implementations do not import one
  another; a genuinely shared type moves into a lower
  contracts/domain module.
- ADR-006 §"Consequences" — strategy code is a pure function of
  observation, free of RPC / storage / configuration.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# Source priority constants
# ---------------------------------------------------------------------------
#
# Lower value = processed first. The ordering reflects the natural flow of
# information through the engine: market data arrives first, the strategy
# reacts to it, the central risk layer approves or rejects, the execution
# layer fills, and the system records its bookkeeping last.
#
# ``source_priority`` is the third component of the sort key
# ``(timestamp, sequence, source_priority)``; the constants below are the
# closed vocabulary a contributor may assign when constructing an input
# event. New sources are additive; renaming or removing an existing
# constant is a breaking change.

SOURCE_PRIORITY_DATA: Final[int] = 1
SOURCE_PRIORITY_STRATEGY: Final[int] = 2
SOURCE_PRIORITY_RISK: Final[int] = 3
SOURCE_PRIORITY_EXECUTION: Final[int] = 4
SOURCE_PRIORITY_SYSTEM: Final[int] = 5

#: Closed set of source-priority sentinels. An input event whose
#: ``source_priority`` is outside this set is rejected at construction
#: time.
_VALID_SOURCE_PRIORITIES: Final[frozenset[int]] = frozenset(
    {
        SOURCE_PRIORITY_DATA,
        SOURCE_PRIORITY_STRATEGY,
        SOURCE_PRIORITY_RISK,
        SOURCE_PRIORITY_EXECUTION,
        SOURCE_PRIORITY_SYSTEM,
    }
)


# ---------------------------------------------------------------------------
# Event kind constants (input events the engine processes)
# ---------------------------------------------------------------------------
#
# The engine recognises the four canonical on-chain event kinds plus
# the ``TICK`` and ``SHUTDOWN`` sentinels. New kinds are additive;
# renaming or removing an existing kind is a breaking change.
#
# - ``SWAP`` / ``MINT`` / ``BURN`` are *reactive* events: the engine
#   hands them to the strategy callback to produce a decision.
# - ``OBSERVATION`` is a non-reactive data-source event: it carries
#   price information the fill stage may use, but it does not
#   trigger a strategy decision on its own.
# - ``TICK`` is a non-reactive event with no payload; it advances
#   the engine clock and is recorded as bookkeeping but carries no
#   price information.
# - ``SHUTDOWN`` is the cooperative shutdown sentinel.

KIND_SWAP: Final[str] = "SWAP"
KIND_MINT: Final[str] = "MINT"
KIND_BURN: Final[str] = "BURN"
KIND_TICK: Final[str] = "TICK"
KIND_OBSERVATION: Final[str] = "OBSERVATION"
KIND_SHUTDOWN: Final[str] = "SHUTDOWN"

_VALID_KINDS: Final[frozenset[str]] = frozenset(
    {KIND_SWAP, KIND_MINT, KIND_BURN, KIND_TICK, KIND_OBSERVATION, KIND_SHUTDOWN}
)

#: Module version carried by every input event. Bumping it is a
#: breaking change for downstream consumers.
BACKTEST_EVENT_VERSION: Final[str] = "t061.backtest_event.v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BacktestEventError(ValueError):
    """Base class for backtest event-construction failures.

    The error class lives in the lower contracts/domain module so the
    shared input-event primitive :class:`BacktestEvent` can raise it
    without depending on the backtest events module. The backtest
    events module re-exports the same error class under the same
    name so existing callers and tests that import
    ``BacktestEventError`` from ``robinhood_lp.backtest.events`` see
    the same type.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_non_negative_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a non-negative Python ``int`` (``bool`` rejected)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise BacktestEventError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise BacktestEventError(f"{field}: must be non-negative, got {value}")
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
                raise BacktestEventError(
                    f"payload entry: must be (str, scalar) tuple, got {entry!r}"
                )
            items.append((entry[0], entry[1]))
    seen: set[str] = set()
    out: list[tuple[str, int | str | bool]] = []
    for key, value in items:
        if not isinstance(key, str) or not key:
            raise BacktestEventError(f"payload key: must be non-empty str, got {key!r}")
        if key in seen:
            raise BacktestEventError(f"payload: key {key!r} appears more than once")
        seen.add(key)
        if isinstance(value, bool):
            coerced: int | str | bool = value
        elif isinstance(value, (int, str)):
            coerced = value
        else:
            raise BacktestEventError(
                f"payload[{key!r}]: must be int/str/bool scalar, got {type(value).__name__}"
            )
        out.append((key, coerced))
    out.sort(key=lambda kv: kv[0])
    return tuple(out)


def _canonical_event_content(
    *,
    version: str,
    timestamp: int,
    sequence: int,
    source_priority: int,
    kind: str,
    pool_key_id: str,
    chain_id: int,
    observed_at: int,
    available_at: int,
    payload: tuple[tuple[str, int | str | bool], ...],
) -> str:
    """Return the canonical string the event-id hash binds to.

    The format is a single ``|``-separated line of every field, with the
    payload sorted by key. The format is part of the contract: changing
    it invalidates every previously recorded event id. Bumping the
    ``version`` is the supported way to evolve the format.
    """
    payload_str = ";".join(f"{k}={v}" for k, v in payload)
    return (
        f"{version}|{timestamp}|{sequence}|{source_priority}|{kind}|"
        f"{pool_key_id}|{chain_id}|{observed_at}|{available_at}|{payload_str}"
    )


def _hash_hex(content: str) -> str:
    """Return the canonical SHA-256 hex digest with the ``0x`` prefix."""
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_event_id(
    *,
    version: str,
    timestamp: int,
    sequence: int,
    source_priority: int,
    kind: str,
    pool_key_id: str,
    chain_id: int,
    observed_at: int,
    available_at: int,
    payload: tuple[tuple[str, int | str | bool], ...],
) -> str:
    """Return the canonical SHA-256 hex digest that binds an input event.

    Two events with identical inputs always produce the same id in any
    process. The function is the canonical :class:`BacktestEvent.event_id`
    computation; the dataclass below invokes it from ``__post_init__``.
    Exposing it as a module-level helper keeps the contract reproducible
    from the protocol surface alone.
    """
    return _hash_hex(
        _canonical_event_content(
            version=version,
            timestamp=timestamp,
            sequence=sequence,
            source_priority=source_priority,
            kind=kind,
            pool_key_id=pool_key_id,
            chain_id=chain_id,
            observed_at=observed_at,
            available_at=available_at,
            payload=payload,
        )
    )


# ---------------------------------------------------------------------------
# BacktestEvent (input event the engine sorts and processes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BacktestEvent:
    """An immutable input event the engine processes in deterministic order.

    The engine sorts a manifest of :class:`BacktestEvent` records by the
    tuple ``(timestamp, sequence, source_priority)``. The ``sequence``
    field is assigned by the engine at construction time from a fresh
    per-source counter (the engine inspects ``source_priority`` to pick
    the right counter) so two equivalent manifests always produce the
    same ordering even when their input lists are in different orders.

    The ``event_id`` is the SHA-256 hex digest of the canonical
    serialisation of every field, prefixed by ``0x``. Same content
    always produces the same event id in any process.

    Two timing fields bind the information frontier:

    - ``observed_at`` is the moment the *source* (e.g. an RPC node, an
      indexer) first saw the event. The strategy can never see the
      event earlier than ``observed_at``; an attempt to do so is a
      contract break.
    - ``available_at`` is the moment the event becomes *visible* to the
      strategy / engine. ``available_at >= observed_at`` is enforced.

    A :class:`BacktestEvent` with ``kind == KIND_SHUTDOWN`` is a
    cooperative shutdown request: the engine stops processing events at
    or after this event. The engine never silently stops.
    """

    version: str
    timestamp: int
    sequence: int
    source_priority: int
    kind: str
    pool_key_id: str
    chain_id: int
    observed_at: int
    available_at: int
    payload: tuple[tuple[str, int | str | bool], ...] = field(default_factory=tuple)
    event_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise BacktestEventError(
                f"BacktestEvent.version: must be non-empty str, got {self.version!r}"
            )
        _require_non_negative_int(self.timestamp, field="BacktestEvent.timestamp")
        _require_non_negative_int(self.sequence, field="BacktestEvent.sequence")
        if self.source_priority not in _VALID_SOURCE_PRIORITIES:
            raise BacktestEventError(
                f"BacktestEvent.source_priority: must be one of "
                f"{sorted(_VALID_SOURCE_PRIORITIES)}, got {self.source_priority}"
            )
        if self.kind not in _VALID_KINDS:
            raise BacktestEventError(
                f"BacktestEvent.kind: must be one of {sorted(_VALID_KINDS)}, got {self.kind!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise BacktestEventError(
                f"BacktestEvent.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise BacktestEventError(
                f"BacktestEvent.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise BacktestEventError(
                f"BacktestEvent.chain_id: must be positive, got {self.chain_id}"
            )
        _require_non_negative_int(self.observed_at, field="BacktestEvent.observed_at")
        _require_non_negative_int(self.available_at, field="BacktestEvent.available_at")
        if self.available_at < self.observed_at:
            raise BacktestEventError(
                f"BacktestEvent.available_at={self.available_at} must be >= "
                f"observed_at={self.observed_at}"
            )
        # Normalise the payload once at construction time so two equivalent
        # payloads hash to the same id regardless of insertion order.
        normalised = _normalise_payload(self.payload)
        object.__setattr__(self, "payload", normalised)
        if not isinstance(self.event_id, str) or not self.event_id:
            object.__setattr__(
                self,
                "event_id",
                compute_event_id(
                    version=self.version,
                    timestamp=self.timestamp,
                    sequence=self.sequence,
                    source_priority=self.source_priority,
                    kind=self.kind,
                    pool_key_id=self.pool_key_id,
                    chain_id=self.chain_id,
                    observed_at=self.observed_at,
                    available_at=self.available_at,
                    payload=normalised,
                ),
            )

    def is_visible(self, decision_time: int) -> bool:
        """Return ``True`` iff this event is visible at ``decision_time``.

        An event is visible when both ``observed_at`` and ``available_at``
        are at or before ``decision_time``. The engine uses this predicate
        to enforce the information frontier; it never uses wall-clock time
        and never reads an event whose ``available_at`` exceeds
        ``decision_time``.
        """
        _require_non_negative_int(decision_time, field="decision_time")
        return self.observed_at <= decision_time and self.available_at <= decision_time

    def is_data_event(self) -> bool:
        """``True`` iff this event is a market-data input (``source_priority == DATA``)."""
        return self.source_priority == SOURCE_PRIORITY_DATA

    def is_shutdown_marker(self) -> bool:
        """``True`` iff this event is a cooperative shutdown request."""
        return self.kind == KIND_SHUTDOWN


# ---------------------------------------------------------------------------
# Strategy callback contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyDecisionRequest:
    """The input the engine passes to the strategy callback.

    The engine collects a list of visible data events up to
    ``decision_time`` and packages them with the decision time. The
    callback returns a :class:`StrategyDecision` describing the
    strategy's intent; the engine turns that intent into the audit
    chain.

    The callback is the *only* place the engine consults the strategy
    layer. The callback is supplied as a ``callable`` parameter, not
    imported, so the engine module is decoupled from any specific
    strategy implementation.
    """

    pool_key_id: str
    chain_id: int
    decision_time: int
    visible_events: tuple[BacktestEvent, ...]
    ledger: Any  # PositionState — typed as Any here to avoid a cycle with backtest.events


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """The strategy callback's response to a :class:`StrategyDecisionRequest`.

    The callback returns one of three kinds:

    - ``NO_TRADE`` — the strategy chose not to trade at this decision
      time. The engine records a ``DECISION`` audit event with status
      ``DECISION_RECORDED`` and ``kind="NO_TRADE"``; no further pipeline
      stages run.
    - ``WAIT`` — the strategy wants to wait for a future event. The
      engine records the decision and stops the pipeline for this
      event.
    - ``PROPOSE`` — the strategy wants to act. The engine continues to
      the risk / latency / fill stages. ``tick_lower`` /
      ``tick_upper`` / ``liquidity`` / ``capital_q64_64`` are required
      and validated.
    """

    kind: str  # NO_TRADE / WAIT / PROPOSE
    pool_key_id: str
    chain_id: int
    decision_time: int
    tick_lower: int = 0
    tick_upper: int = 0
    liquidity: int = 0
    capital_q64_64: int = 0
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in ("NO_TRADE", "WAIT", "PROPOSE"):
            raise BacktestEventError(
                f"StrategyDecision.kind: must be one of NO_TRADE/WAIT/PROPOSE, got {self.kind!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise BacktestEventError(
                f"StrategyDecision.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        if self.kind == "PROPOSE":
            if self.tick_lower >= self.tick_upper:
                raise BacktestEventError(
                    f"StrategyDecision: PROPOSE requires tick_lower < "
                    f"tick_upper, got {self.tick_lower} >= {self.tick_upper}"
                )
            if self.liquidity <= 0:
                raise BacktestEventError(
                    f"StrategyDecision: PROPOSE requires liquidity > 0, got {self.liquidity}"
                )
            if self.capital_q64_64 <= 0:
                raise BacktestEventError(
                    f"StrategyDecision: PROPOSE requires capital_q64_64 > 0, "
                    f"got {self.capital_q64_64}"
                )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "BACKTEST_EVENT_VERSION",
    "BacktestEvent",
    "BacktestEventError",
    "KIND_BURN",
    "KIND_MINT",
    "KIND_OBSERVATION",
    "KIND_SHUTDOWN",
    "KIND_SWAP",
    "KIND_TICK",
    "SOURCE_PRIORITY_DATA",
    "SOURCE_PRIORITY_EXECUTION",
    "SOURCE_PRIORITY_RISK",
    "SOURCE_PRIORITY_STRATEGY",
    "SOURCE_PRIORITY_SYSTEM",
    "StrategyDecision",
    "StrategyDecisionRequest",
    "compute_event_id",
]
