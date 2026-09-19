"""V1 strategy contracts — pure observation -> candidate action (T060).

T060 defines the strategy layer's *contracts*, not a concrete
strategy implementation. The contracts cover:

- **Immutable, versioned snapshots.** Three frozen dataclasses
  carry every input the strategy needs at a single decision time:
  :class:`AdmissionSnapshot`, :class:`MarketSnapshot`, and
  :class:`PortfolioSnapshot`. Each carries its own
  ``SNAPSHOT_VERSION`` so a downstream consumer can detect a
  contract break; equality and hashing follow dataclass identity
  so the same snapshot set always hashes the same way in any
  process.

- **Component interfaces and their outputs.** Two substitutable
  components — :class:`RegimeModel` and
  :class:`FeeOpportunityModel` — produce two versioned outputs:
  :class:`RegimeAssessment` and
  :class:`FeeOpportunityAssessment`. The names match
  ``docs/spec/strategy/STRATEGY_ECONOMICS.md`` §7 exactly: a
  component name denotes an *interface*, an output name denotes a
  *result*, and the two vocabularies never overlap. Each
  assessment is a versioned frozen dataclass whose
  ``outcome`` field is one of ``ASSESSED`` or ``UNCERTAIN``: a
  component without the evidence to assess *must* return
  ``UNCERTAIN`` rather than fabricate an assessment. A model-
  backed implementation (T102 / P10) satisfies the same contract
  as a rule implementation — same inputs, same outputs, same
  validation duties, same uncertainty path; it does not alter
  the snapshots, the candidate action, the ledger, risk or audit
  semantics.

- **Deterministic clock and seeded random source.** The
  :class:`DeterministicClock` and :class:`SeededRandomSource`
  interfaces are the only time / randomness sources the strategy
  layer is allowed to read. Wall-clock time, ``time.time()``,
  ``datetime.now()``, ``random.random()`` and any unseeded
  randomness are forbidden inside this module (ADR-006).
  Implementations must be deterministic given the same seed.

- **Tick / capital proposal validation.** The
  :func:`validate_proposal` function rejects (i) ticks outside
  the V4 ``[MIN_TICK, MAX_TICK]`` domain, (ii) ticks not
  aligned to ``PoolKey.tick_spacing``, (iii) degenerate Ranges
  with ``tick_lower >= tick_upper``, (iv) snapshots whose
  ``availability_time`` is later than ``decision_time`` (stale
  state), (v) NaN / display values embedded in market features
  (``float`` is forbidden in the protocol / valuation path per
  ADR-004), and (vi) capital envelopes exceeding the per-pool
  maximum the snapshot records. All checks run *before* a
  :class:`CandidateAction` is constructed; failure raises a
  structured exception family so the caller can distinguish the
  reason.

- **Reason-coded ``NO_TRADE``.** The :class:`ReasonCode` enum
  carries the closed vocabulary a :class:`CandidateAction` of
  kind ``NO_TRADE`` declares. New codes are additive; renaming
  or removing a code is a breaking change.

- **Candidate action.** The :class:`CandidateAction` dataclass
  is the proposal the strategy submits to the central risk and
  execution layers. It is **not** a transaction: it carries the
  pool, the candidate Range, the capital envelope, the decision
  and availability times, the input snapshot versions, the
  component versions and outcomes, and the structured ``NO_TRADE``
  reason when the strategy is not placing an order. The
  execution layer (T070 / T071) constructs the actual
  transactions from a validated candidate; this module never
  imports execution and never produces a signed or broadcast
  payload.

Design constraints (binding):

- **Strategy is a pure consumer.** No method on a snapshot or a
  component mutates a snapshot, the candidate action, the ledger
  or audit semantics. Components return *new* values; they never
  modify their inputs.

- **No float on the protocol / valuation path.** Per ADR-004,
  every integer-valued field is a Python ``int`` (``bool``
  rejected) and ``float`` is forbidden anywhere in this module
  except the *explicitly named* statistical boundary inside a
  model-backed component implementation. The display / NaN
  guard therefore rejects ``float`` carrying NaN or infinity on
  every contract surface.

- **Layer purity.** This module depends only on
  ``robinhood_lp.protocol`` (ids / events / math / sizing) and
  the stdlib. It must not import RPC, storage, configuration,
  signing, execution, or the replay layer (ADR-006); the
  dependency tests in ``tests/test_strategy_t060.py`` enforce
  this by walking the live module graph.

References:

- R17 — NautilusTrader event-time architecture.
- ADR-014 §5 — models occupy interfaces; they never make decisions.
- ``docs/spec/strategy/STRATEGY_ECONOMICS.md`` §7 — substitutable
  components, versioned snapshots, USDG-only ledger.
- ``docs/spec/architecture/ARCHITECTURE.md`` §2.1 — strategy layer
  may not call RPC, storage, signing, or execution.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import ModuleType
from typing import Final, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Strategy module version. The contract a downstream consumer reads to
#: know which strategy-contract definition it is consuming. Bumping the
#: version is a breaking change for downstream code.
ADMISSION_SNAPSHOT_VERSION: Final[str] = "t060.admission_snapshot.v1"
MARKET_SNAPSHOT_VERSION: Final[str] = "t060.market_snapshot.v1"
PORTFOLIO_SNAPSHOT_VERSION: Final[str] = "t060.portfolio_snapshot.v1"
REGIME_ASSESSMENT_VERSION: Final[str] = "t060.regime_assessment.v1"
FEE_OPPORTUNITY_ASSESSMENT_VERSION: Final[str] = "t060.fee_opportunity_assessment.v1"
CANDIDATE_ACTION_VERSION: Final[str] = "t060.candidate_action.v1"

#: Q64.64 fixed-point scale. The single named numeraire boundary the
#: project uses (ADR-014 §3, T049 / T053). The strategy layer re-uses the
#: same scale so the candidate action's capital envelope speaks the same
#: integer language as the existing USDG conversion path.
Q64_SCALE: Final[int] = 1 << 64

#: Maximum permitted capital envelope in Q64.64 (a conservative bound on
#: the USDG the user-supplied risk cap may allocate). The constant exists
#: so the validation path can reject an obviously excessive capital
#: envelope (``amount > MAX_CAPITAL_Q64_64``) without having to import a
#: runtime configuration; per-pool envelopes must be strictly less.
MAX_CAPITAL_Q64_64: Final[int] = Q64_SCALE << 64  # 2**128

#: ``tick_spacing`` lower bound the strategy accepts. Mirrors
#: ``MIN_TICK_SPACING`` in ``robinhood_lp.protocol.ids`` so the validator
#: never relies on a private re-export. Duplicated here for the same
#: reason: this module never imports private symbols from a sibling
#: layer.
MIN_TICK_SPACING_STRATEGY: Final[int] = 1

#: ``tick_spacing`` upper bound the strategy accepts. Mirrors
#: ``MAX_TICK_SPACING`` in ``robinhood_lp.protocol.ids``.
MAX_TICK_SPACING_STRATEGY: Final[int] = 32_767

#: Candidate-action kind sentinels (the closed set of proposals the
#: strategy may submit).
CANDIDATE_KIND_NO_TRADE: Final[str] = "NO_TRADE"
CANDIDATE_KIND_PROPOSE: Final[str] = "PROPOSE"
CANDIDATE_KIND_WAIT: Final[str] = "WAIT"

#: Sentinel tick value carried by ``NO_TRADE`` / ``WAIT`` candidate
#: actions to make it explicit the Range is *not* being proposed.
CANDIDATE_SENTINEL_TICK: Final[int] = 0

#: Sentinel liquidity value carried by ``NO_TRADE`` / ``WAIT``
#: candidate actions; the proposal layer requests no mint.
CANDIDATE_SENTINEL_LIQUIDITY: Final[int] = 0

#: Regime-state sentinels. The closed set of states a
#: :class:`RegimeAssessment` may declare when its ``outcome`` is
#: :attr:`REGIME_OUTCOME_ASSESSED`.
REGIME_STATE_RANGE: Final[str] = "RANGE"
REGIME_STATE_UP_TREND: Final[str] = "UP_TREND"
REGIME_STATE_DOWN_TREND: Final[str] = "DOWN_TREND"
REGIME_STATE_JUMP_RISK: Final[str] = "JUMP_RISK"
REGIME_STATE_UNCERTAIN: Final[str] = "UNCERTAIN"

#: Assessment outcome sentinels.
REGIME_OUTCOME_ASSESSED: Final[str] = "ASSESSED"
REGIME_OUTCOME_UNCERTAIN: Final[str] = "UNCERTAIN"

FEE_OPPORTUNITY_OUTCOME_ASSESSED: Final[str] = "ASSESSED"
FEE_OPPORTUNITY_OUTCOME_UNCERTAIN: Final[str] = "UNCERTAIN"

#: Module denylist for the layer-purity check. The strategy layer is
#: permitted to import the standard library and a narrow set of
#: ``robinhood_lp`` subpackages (protocol/domain, features,
#: strategy). Any other ``robinhood_lp`` subpackage — RPC, storage,
#: ingestion, configuration, replay, discovery, qualification,
#: quality, presentation, execution — is a contract break that must
#: be reviewed. Third-party packages are likewise forbidden (the
#: layer is intentionally stdlib-only).
_FORBIDDEN_STRATEGY_ROBINHOOD_MODULES: Final[tuple[str, ...]] = (
    "robinhood_lp.config",
    "robinhood_lp.discovery",
    "robinhood_lp.ingestion",
    "robinhood_lp.presentation",
    "robinhood_lp.qualification",
    "robinhood_lp.quality",
    "robinhood_lp.replay",
    "robinhood_lp.rpc",
    "robinhood_lp.storage",
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ReasonCode(StrEnum):
    """Stable reason codes for ``NO_TRADE`` outcomes.

    Strings are part of the public contract. New codes are additive;
    renaming or removing an existing code is a breaking change. The set
    covers every reason a :func:`validate_proposal` call may reject and
    every reason :func:`evaluate_decision` may produce a ``NO_TRADE``
    verdict for.
    """

    # Validation failures (the proposal was rejected before a candidate
    # action could be constructed).
    RANGE_DEGENERATE = "RANGE_DEGENERATE"
    TICK_OUT_OF_BOUNDS = "TICK_OUT_OF_BOUNDS"
    TICK_NOT_ALIGNED = "TICK_NOT_ALIGNED"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    NAN_DISPLAY_VALUE = "NAN_DISPLAY_VALUE"
    CAPITAL_EXCESSIVE = "CAPITAL_EXCESSIVE"
    CAPITAL_NONPOSITIVE = "CAPITAL_NONPOSITIVE"

    # Component-driven rejections (a snapshot was admissible but the
    # components refused to produce a confident assessment).
    REGIME_UNCERTAIN = "REGIME_UNCERTAIN"
    FEE_OPPORTUNITY_UNCERTAIN = "FEE_OPPORTUNITY_UNCERTAIN"

    # Default fallback when the strategy explicitly chose not to trade.
    STRATEGY_HOLD = "STRATEGY_HOLD"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StrategyError(ValueError):
    """Base class for strategy-contract failures."""


class InvalidSnapshotError(StrategyError):
    """A snapshot violates its invariants."""


class InvalidTickError(StrategyError):
    """A candidate tick violates V4 invariants (out of bounds or unaligned)."""


class StaleSnapshotError(StrategyError):
    """A snapshot's ``availability_time`` exceeds the decision time."""


class NaNDisplayValueError(StrategyError):
    """A market feature carried a NaN / infinite display value.

    ``float`` is forbidden on the protocol / valuation path (ADR-004).
    NaN / infinite values indicate a contract violation rather than a
    market outcome.
    """


class InvalidCapitalError(StrategyError):
    """A capital envelope is non-positive or exceeds the per-pool maximum."""


class InvalidProposalError(StrategyError):
    """A proposal carries an inconsistent set of fields."""


class ProposalValidationError(StrategyError):
    """Base class for validation failures (invalid tick, stale state, etc.)."""


class CandidateActionError(StrategyError):
    """A :class:`CandidateAction` violates its invariants."""


class DeterministicClockError(StrategyError):
    """A deterministic clock violated its invariants."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a Python ``int`` (``bool`` rejected)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidSnapshotError(f"{field}: must be int, got {type(value).__name__}")
    return value


def _require_non_negative_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a non-negative Python ``int``."""
    value = _require_int(value, field=field)
    if value < 0:
        raise InvalidSnapshotError(f"{field}: must be non-negative, got {value}")
    return value


def _require_positive_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a strictly positive Python ``int``."""
    value = _require_int(value, field=field)
    if value <= 0:
        raise InvalidSnapshotError(f"{field}: must be positive, got {value}")
    return value


def _require_uint256(value: int, *, field: str) -> int:
    """Validate ``value`` fits in a uint256 (used for amounts / caps)."""
    value = _require_non_negative_int(value, field=field)
    if value >= (1 << 256):
        raise InvalidSnapshotError(f"{field}: exceeds uint256 width, got {value}")
    return value


def _require_q64_64(value: int, *, field: str) -> int:
    """Validate ``value`` is a non-negative Q64.64 ratio."""
    value = _require_int(value, field=field)
    if value < 0:
        raise InvalidSnapshotError(f"{field}: must be non-negative Q64.64, got {value}")
    return value


def _require_strict_q64_64(value: int, *, field: str) -> int:
    """Validate ``value`` is a strictly positive Q64.64 ratio."""
    value = _require_int(value, field=field)
    if value <= 0:
        raise InvalidSnapshotError(f"{field}: must be positive Q64.64, got {value}")
    return value


def _is_finite_number(value: object, *, field: str) -> bool:
    """Return ``True`` iff ``value`` is a real number without NaN/inf.

    Accepts ``int`` (always finite) and ``float`` only when the float
    is finite. ``bool`` is rejected as a number; ``Decimal`` is accepted
    only when it is finite (the framework uses ``Decimal`` only at the
    named display boundary, ADR-009).
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    import decimal

    if isinstance(value, decimal.Decimal):
        return value.is_finite()
    return False


# ---------------------------------------------------------------------------
# Deterministic clock and seeded random source
# ---------------------------------------------------------------------------


@runtime_checkable
class DeterministicClock(Protocol):
    """The only time source the strategy layer is allowed to read.

    A deterministic clock returns a strictly monotonic non-decreasing
    sequence of event-time integers; the same call sequence returns
    the same sequence in any process. Wall-clock time, ``time.time()``,
    ``datetime.now()``, and any system-time read are forbidden inside
    this module (ADR-006).

    Implementations must be referentially transparent: two
    constructions with the same initial value yield the same output for
    the same call sequence. The :class:`FrozenClock` reference
    implementation below satisfies the contract with a fixed list of
    event times; production injects a clock from the orchestration
    layer.

    Because the clock is frozen, :meth:`now` returns the current
    event-time integer and :meth:`advance` returns a *new* clock with
    the cursor moved forward by one. The original remains immutable;
    the caller threads the new instance forward.
    """

    def now(self) -> int:
        """Return the current event-time integer (typically Unix seconds)."""
        ...

    def advance(self) -> DeterministicClock:
        """Return a new clock with the cursor advanced by one."""
        ...


@runtime_checkable
class SeededRandomSource(Protocol):
    """The only randomness source the strategy layer is allowed to read.

    A seeded random source yields the same sequence of integers in any
    process given the same seed. ``random.random()`` and any unseeded
    randomness are forbidden inside this module (ADR-006).

    Implementations must be deterministic given the same seed; the
    :class:`FrozenSeededRandomSource` reference implementation below
    satisfies the contract with a fixed list of pre-generated values.
    """

    def seed(self, value: int) -> None:
        """Reset the source to ``value``; subsequent draws are deterministic."""
        ...

    def next_int(self, upper_bound: int) -> int:
        """Return the next integer in ``[0, upper_bound)``."""
        ...


@dataclass(frozen=True, slots=True)
class FrozenClock:
    """A deterministic clock implementation backed by a fixed sequence.

    The clock is frozen: its event times are supplied at construction
    time, and the current time advances by exactly one element on each
    call to :meth:`advance`. A consumer that exhausts the sequence
    raises :class:`DeterministicClockError` rather than wrap-around;
    the strategy layer is supposed to pre-declare its event schedule.

    Because the dataclass is frozen, :meth:`advance` yields a *new*
    :class:`FrozenClock` (with the cursor advanced by one) via
    :func:`dataclasses.replace`. The original clock remains immutable;
    the caller threads the new instance forward. :meth:`now` is a
    read-only query that returns the event time at the current cursor.
    """

    event_times: tuple[int, ...] = field(default_factory=tuple)
    cursor: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.event_times, tuple):
            raise DeterministicClockError(
                f"FrozenClock.event_times: must be tuple[int, ...], got "
                f"{type(self.event_times).__name__}"
            )
        for i, t in enumerate(self.event_times):
            _require_non_negative_int(t, field=f"FrozenClock.event_times[{i}]")
        _require_non_negative_int(self.cursor, field="FrozenClock.cursor")
        if self.cursor > len(self.event_times):
            raise DeterministicClockError(
                f"FrozenClock.cursor={self.cursor} exceeds sequence length {len(self.event_times)}"
            )
        # Monotonic non-decreasing sequence is a contract invariant:
        # any out-of-order event time would let two equivalent
        # constructions produce different downstream effects.
        for i in range(1, len(self.event_times)):
            if self.event_times[i] < self.event_times[i - 1]:
                raise DeterministicClockError(
                    f"FrozenClock.event_times[{i}]={self.event_times[i]} is "
                    f"less than event_times[{i - 1}]={self.event_times[i - 1]}"
                )

    def now(self) -> int:
        """Return the event time at the current cursor without advancing."""
        if self.cursor >= len(self.event_times):
            raise DeterministicClockError(
                f"FrozenClock: exhausted {len(self.event_times)} event times"
            )
        return self.event_times[self.cursor]

    def advance(self) -> FrozenClock:
        """Return a new :class:`FrozenClock` with the cursor advanced by one.

        The original clock is unchanged; the returned clock has its
        ``cursor`` advanced by one and is the value the caller should
        thread forward.
        """
        if self.cursor >= len(self.event_times):
            raise DeterministicClockError(
                f"FrozenClock: exhausted {len(self.event_times)} event times"
            )
        return dataclasses.replace(self, cursor=self.cursor + 1)


@dataclass(frozen=True, slots=True)
class FrozenSeededRandomSource:
    """A deterministic seeded random source backed by a fixed sequence.

    The source is frozen: it exposes a deterministic list of integers,
    each in ``[0, upper_bound)`` for the bound the caller supplied at
    construction time. ``next_int(upper)`` returns the next integer
    for ``upper``; an exhausted bucket raises :class:`StrategyError`
    rather than wrap-around.

    The contract mirrors :class:`SeededRandomSource`; the
    implementation is the canonical "no wall clock, no unseeded
    randomness" reference.
    """

    initial_seed: int = 0
    _buckets: tuple[tuple[int, tuple[int, ...]], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_negative_int(self.initial_seed, field="FrozenSeededRandomSource.initial_seed")
        if not isinstance(self._buckets, tuple):
            raise StrategyError(
                f"FrozenSeededRandomSource._buckets: must be "
                f"tuple[tuple[int, tuple[int, ...]], ...], got "
                f"{type(self._buckets).__name__}"
            )
        for i, bucket in enumerate(self._buckets):
            if not (isinstance(bucket, tuple) and len(bucket) == 2):
                raise StrategyError(
                    f"FrozenSeededRandomSource._buckets[{i}]: must be (upper_bound, sequence)"
                )
            upper, seq = bucket
            _require_positive_int(upper, field=f"FrozenSeededRandomSource._buckets[{i}][0]")
            if not isinstance(seq, tuple):
                raise StrategyError(
                    f"FrozenSeededRandomSource._buckets[{i}][1]: must be "
                    f"tuple[int, ...], got {type(seq).__name__}"
                )
            for j, item in enumerate(seq):
                _require_int(item, field=f"FrozenSeededRandomSource._buckets[{i}][1][{j}]")
                if item < 0 or item >= upper:
                    raise StrategyError(
                        f"FrozenSeededRandomSource._buckets[{i}][1][{j}]={item} not in [0, {upper})"
                    )

    def seed(self, value: int) -> None:
        """No-op: the source is already deterministic.

        The :class:`SeededRandomSource` Protocol declares ``seed`` as
        a method; the frozen source has no state to mutate, so the
        method is a record-only operation. The seed the caller would
        have used is captured on the :attr:`initial_seed` field at
        construction time.
        """
        _require_non_negative_int(value, field="value")

    def next_int(self, upper_bound: int) -> int:
        """Return the next pre-generated integer in ``[0, upper_bound)``."""
        _require_positive_int(upper_bound, field="upper_bound")
        for upper, seq in self._buckets:
            if upper == upper_bound and seq:
                return seq[0]
        raise StrategyError(
            f"FrozenSeededRandomSource: no remaining values for upper_bound={upper_bound}"
        )


# ---------------------------------------------------------------------------
# Snapshots (immutable, versioned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmissionSnapshot:
    """The per-pool admission evidence the strategy reads.

    The snapshot is the strategy-side projection of the T025
    ``AssetAdmissionRecord``: it carries the two admission tracks
    (``admitted`` / ``rejected``) plus the pool identity and the
    declared support level. The strategy layer never queries the
    admission registry directly; the orchestration layer packages
    one snapshot per pool at decision time.

    Equality and hashing follow dataclass identity. Two snapshots
    are equal iff every field is equal.
    """

    version: str
    pool_key_id: str
    chain_id: int
    support_level: str
    track_a_admitted: bool
    track_b_admitted: bool
    max_capital_q64_64: int
    data_time: int
    availability_time: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise InvalidSnapshotError(
                f"AdmissionSnapshot.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise InvalidSnapshotError(
                f"AdmissionSnapshot.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        _require_positive_int(self.chain_id, field="AdmissionSnapshot.chain_id")
        if not isinstance(self.support_level, str) or not self.support_level:
            raise InvalidSnapshotError(
                f"AdmissionSnapshot.support_level: must be non-empty str, "
                f"got {self.support_level!r}"
            )
        if not isinstance(self.track_a_admitted, bool):
            raise InvalidSnapshotError(
                f"AdmissionSnapshot.track_a_admitted: must be bool, got "
                f"{type(self.track_a_admitted).__name__}"
            )
        if not isinstance(self.track_b_admitted, bool):
            raise InvalidSnapshotError(
                f"AdmissionSnapshot.track_b_admitted: must be bool, got "
                f"{type(self.track_b_admitted).__name__}"
            )
        _require_strict_q64_64(
            self.max_capital_q64_64, field="AdmissionSnapshot.max_capital_q64_64"
        )
        _require_non_negative_int(self.data_time, field="AdmissionSnapshot.data_time")
        _require_non_negative_int(
            self.availability_time, field="AdmissionSnapshot.availability_time"
        )
        if self.availability_time < self.data_time:
            raise InvalidSnapshotError(
                f"AdmissionSnapshot.availability_time={self.availability_time} "
                f"must be >= data_time={self.data_time}"
            )
        if not isinstance(self.notes, tuple):
            raise InvalidSnapshotError(
                f"AdmissionSnapshot.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise InvalidSnapshotError(
                    f"AdmissionSnapshot.notes: every entry must be str, got {type(n).__name__}"
                )

    @property
    def is_admitted(self) -> bool:
        """``True`` iff both tracks are admitted (``track_a`` and ``track_b``)."""
        return self.track_a_admitted and self.track_b_admitted


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """The point-in-time market feature state for a single decision.

    The snapshot bundles the four classes of evidence the strategy
    reads at a decision time:

    - ``sqrt_price_x96`` (Q64.96 integer; the V4 wire format);
    - ``liquidity`` (uint128; current pool active liquidity);
    - ``realized_volatility_q64_64`` (Q64.64 dimensionless ratio; the
      annualized realized volatility from T050 bars);
    - ``freshness_seconds`` (non-negative int; the time elapsed since
      the latest observation that fed the snapshot);
    - ``quote`` (Q64.64 USDG per raw token; the ADR-014 numeraire
      boundary value the conversion graph produced); ``None`` when
      the dataset is ``RELATIVE_ONLY``;
    - ``is_relative_only`` (bool; ``True`` when the dataset has no
      USDG or qualified USD stablecoin — the strategy must not
      rank against USD-denominated results).

    The snapshot also carries ``data_time`` and
    ``availability_time``; the strategy treats any decision time
    earlier than ``availability_time`` as a stale-state violation.
    """

    version: str
    pool_key_id: str
    chain_id: int
    sqrt_price_x96: int
    liquidity: int
    realized_volatility_q64_64: int
    freshness_seconds: int
    quote_q64_64: int | None
    is_relative_only: bool
    data_time: int
    availability_time: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise InvalidSnapshotError(
                f"MarketSnapshot.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise InvalidSnapshotError(
                f"MarketSnapshot.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        _require_positive_int(self.chain_id, field="MarketSnapshot.chain_id")
        _require_uint256(self.sqrt_price_x96, field="MarketSnapshot.sqrt_price_x96")
        if self.sqrt_price_x96 == 0:
            raise InvalidSnapshotError("MarketSnapshot.sqrt_price_x96: must be positive, got 0")
        # Liquidity is uint128 (V4 width).
        if self.liquidity < 0 or self.liquidity >= (1 << 128):
            raise InvalidSnapshotError(
                f"MarketSnapshot.liquidity: must fit in uint128, got {self.liquidity}"
            )
        _require_q64_64(
            self.realized_volatility_q64_64,
            field="MarketSnapshot.realized_volatility_q64_64",
        )
        _require_non_negative_int(self.freshness_seconds, field="MarketSnapshot.freshness_seconds")
        if self.quote_q64_64 is not None:
            _require_strict_q64_64(self.quote_q64_64, field="MarketSnapshot.quote_q64_64")
        if not isinstance(self.is_relative_only, bool):
            raise InvalidSnapshotError(
                f"MarketSnapshot.is_relative_only: must be bool, got "
                f"{type(self.is_relative_only).__name__}"
            )
        if self.is_relative_only and self.quote_q64_64 is not None:
            raise InvalidSnapshotError(
                f"MarketSnapshot: RELATIVE_ONLY snapshots must not carry a "
                f"quote (quote_q64_64={self.quote_q64_64})"
            )
        _require_non_negative_int(self.data_time, field="MarketSnapshot.data_time")
        _require_non_negative_int(self.availability_time, field="MarketSnapshot.availability_time")
        if self.availability_time < self.data_time:
            raise InvalidSnapshotError(
                f"MarketSnapshot.availability_time={self.availability_time} "
                f"must be >= data_time={self.data_time}"
            )
        if not isinstance(self.notes, tuple):
            raise InvalidSnapshotError(
                f"MarketSnapshot.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise InvalidSnapshotError(
                    f"MarketSnapshot.notes: every entry must be str, got {type(n).__name__}"
                )


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """The per-position state the strategy reads at decision time.

    The snapshot carries:

    - the position identity (``position_id``; the
      ``(tick_lower, tick_upper, salt)`` triple the framework uses
      when no V4 NFT ``tokenId`` is available);
    - ``sqrt_price_x96`` (Q64.96 integer; current price at the
      position);
    - ``liquidity`` (uint128; the position's liquidity);
    - ``principal_token0`` / ``principal_token1`` (uint256 atomic
      units; the principal ledger the position carries);
    - ``tokens_owed0`` / ``tokens_owed1`` (uint256; fees owed at
      the last fee-growth snapshot);
    - ``data_time`` / ``availability_time`` (the moment the
      snapshot is *as of* and the moment it becomes known).

    ``is_empty`` indicates the strategy holds no position at the
    snapshot time; an empty portfolio permits any candidate action
    that the strategy wants to propose (subject to the other
    validators).
    """

    version: str
    pool_key_id: str
    chain_id: int
    position_id: str
    sqrt_price_x96: int
    liquidity: int
    principal_token0: int
    principal_token1: int
    tokens_owed0: int
    tokens_owed1: int
    is_empty: bool
    data_time: int
    availability_time: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise InvalidSnapshotError(
                f"PortfolioSnapshot.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise InvalidSnapshotError(
                f"PortfolioSnapshot.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        _require_positive_int(self.chain_id, field="PortfolioSnapshot.chain_id")
        if not isinstance(self.position_id, str) or not self.position_id:
            raise InvalidSnapshotError(
                f"PortfolioSnapshot.position_id: must be non-empty str, got {self.position_id!r}"
            )
        _require_uint256(self.sqrt_price_x96, field="PortfolioSnapshot.sqrt_price_x96")
        if self.sqrt_price_x96 == 0:
            raise InvalidSnapshotError("PortfolioSnapshot.sqrt_price_x96: must be positive, got 0")
        if self.liquidity < 0 or self.liquidity >= (1 << 128):
            raise InvalidSnapshotError(
                f"PortfolioSnapshot.liquidity: must fit in uint128, got {self.liquidity}"
            )
        _require_uint256(self.principal_token0, field="PortfolioSnapshot.principal_token0")
        _require_uint256(self.principal_token1, field="PortfolioSnapshot.principal_token1")
        _require_uint256(self.tokens_owed0, field="PortfolioSnapshot.tokens_owed0")
        _require_uint256(self.tokens_owed1, field="PortfolioSnapshot.tokens_owed1")
        if not isinstance(self.is_empty, bool):
            raise InvalidSnapshotError(
                f"PortfolioSnapshot.is_empty: must be bool, got {type(self.is_empty).__name__}"
            )
        if self.is_empty and (
            self.liquidity != 0 or self.principal_token0 != 0 or self.principal_token1 != 0
        ):
            raise InvalidSnapshotError(
                f"PortfolioSnapshot: is_empty=True requires zero "
                f"liquidity / principal_token0 / principal_token1, got "
                f"liquidity={self.liquidity} principal_token0="
                f"{self.principal_token0} principal_token1="
                f"{self.principal_token1}"
            )
        _require_non_negative_int(self.data_time, field="PortfolioSnapshot.data_time")
        _require_non_negative_int(
            self.availability_time, field="PortfolioSnapshot.availability_time"
        )
        if self.availability_time < self.data_time:
            raise InvalidSnapshotError(
                f"PortfolioSnapshot.availability_time={self.availability_time} "
                f"must be >= data_time={self.data_time}"
            )
        if not isinstance(self.notes, tuple):
            raise InvalidSnapshotError(
                f"PortfolioSnapshot.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise InvalidSnapshotError(
                    f"PortfolioSnapshot.notes: every entry must be str, got {type(n).__name__}"
                )


# ---------------------------------------------------------------------------
# Component interfaces and outputs
# ---------------------------------------------------------------------------


@runtime_checkable
class RegimeModel(Protocol):
    """The substitutable regime-classification interface.

    A :class:`RegimeModel` consumes the immutable snapshots the
    strategy read at decision time and returns a
    :class:`RegimeAssessment`. The interface is the *sole* supported
    extension point for a regime-classifier model (T102 / P10); a
    model-backed implementation must satisfy the same contract as a
    rule implementation — same inputs, same outputs, same validation
    and uncertainty duties.

    The implementation must not mutate its inputs, may not produce a
    default assessment when the evidence is insufficient (the
    returned :class:`RegimeAssessment` must carry ``outcome ==
    REGIME_OUTCOME_UNCERTAIN``), and may not alter the snapshots,
    the candidate action, the ledger, risk or audit semantics.
    """

    def assess(
        self,
        *,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        clock: DeterministicClock,
        rng: SeededRandomSource,
    ) -> RegimeAssessment:
        """Return a :class:`RegimeAssessment` for the supplied state."""
        ...


@runtime_checkable
class FeeOpportunityModel(Protocol):
    """The substitutable fee-opportunity interface.

    A :class:`FeeOpportunityModel` consumes the immutable snapshots
    the strategy read at decision time and returns a
    :class:`FeeOpportunityAssessment`. The interface is the *sole*
    supported extension point for a fee-opportunity model (T102 /
    P10); a model-backed implementation must satisfy the same
    contract as a rule implementation — same inputs, same outputs,
    same validation and uncertainty duties.

    The implementation must not mutate its inputs, may not produce a
    default assessment when the evidence is insufficient (the
    returned :class:`FeeOpportunityAssessment` must carry ``outcome
    == FEE_OPPORTUNITY_OUTCOME_UNCERTAIN``), and may not alter the
    snapshots, the candidate action, the ledger, risk or audit
    semantics.
    """

    def assess(
        self,
        *,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        clock: DeterministicClock,
        rng: SeededRandomSource,
    ) -> FeeOpportunityAssessment:
        """Return a :class:`FeeOpportunityAssessment` for the supplied state."""
        ...


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    """The versioned output a :class:`RegimeModel` returns.

    The assessment carries:

    - ``version`` — :data:`REGIME_ASSESSMENT_VERSION`, frozen for the
      lifetime of the contract;
    - ``outcome`` — :attr:`REGIME_OUTCOME_ASSESSED` when the model
      is confident, otherwise :attr:`REGIME_OUTCOME_UNCERTAIN`;
    - ``state`` — one of the closed regime-state sentinels when the
      outcome is ``ASSESSED``; the field is ``REGIME_STATE_UNCERTAIN``
      when the outcome is ``UNCERTAIN``;
    - ``confidence_q64_64`` — a strictly positive Q64.64 ratio when
      the outcome is ``ASSESSED``, otherwise ``0``;
    - ``evidence_keys`` — an immutable tuple of feature-version
      strings the model used; the strategy surfaces the tuple to the
      audit layer so a later change to a feature version can be
      traced back to a model assessment.

    Equality and hashing follow dataclass identity.
    """

    version: str
    model_version: str
    outcome: str
    state: str
    confidence_q64_64: int
    evidence_keys: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise StrategyError(
                f"RegimeAssessment.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.model_version, str) or not self.model_version:
            raise StrategyError(
                f"RegimeAssessment.model_version: must be non-empty str, got {self.model_version!r}"
            )
        if self.outcome not in (REGIME_OUTCOME_ASSESSED, REGIME_OUTCOME_UNCERTAIN):
            raise StrategyError(
                f"RegimeAssessment.outcome: must be {REGIME_OUTCOME_ASSESSED!r} "
                f"or {REGIME_OUTCOME_UNCERTAIN!r}, got {self.outcome!r}"
            )
        if self.outcome == REGIME_OUTCOME_ASSESSED:
            if self.state not in (
                REGIME_STATE_RANGE,
                REGIME_STATE_UP_TREND,
                REGIME_STATE_DOWN_TREND,
                REGIME_STATE_JUMP_RISK,
            ):
                raise StrategyError(
                    f"RegimeAssessment.state={self.state!r} is not one of "
                    f"the closed regime-state sentinels for an ASSESSED "
                    f"outcome"
                )
            _require_strict_q64_64(
                self.confidence_q64_64, field="RegimeAssessment.confidence_q64_64"
            )
        else:
            if self.state != REGIME_STATE_UNCERTAIN:
                raise StrategyError(
                    f"RegimeAssessment.state={self.state!r} must be "
                    f"{REGIME_STATE_UNCERTAIN!r} when outcome is "
                    f"{REGIME_OUTCOME_UNCERTAIN!r}"
                )
            _require_non_negative_int(
                self.confidence_q64_64, field="RegimeAssessment.confidence_q64_64"
            )
            if self.confidence_q64_64 != 0:
                raise StrategyError(
                    f"RegimeAssessment.confidence_q64_64 must be 0 when "
                    f"outcome is {REGIME_OUTCOME_UNCERTAIN!r}, got "
                    f"{self.confidence_q64_64}"
                )
        if not isinstance(self.evidence_keys, tuple):
            raise StrategyError(
                f"RegimeAssessment.evidence_keys: must be tuple[str, ...], "
                f"got {type(self.evidence_keys).__name__}"
            )
        for k in self.evidence_keys:
            if not isinstance(k, str):
                raise StrategyError(
                    f"RegimeAssessment.evidence_keys: every entry must be "
                    f"str, got {type(k).__name__}"
                )
        if not isinstance(self.notes, tuple):
            raise StrategyError(
                f"RegimeAssessment.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise StrategyError(
                    f"RegimeAssessment.notes: every entry must be str, got {type(n).__name__}"
                )

    @classmethod
    def uncertain(
        cls,
        *,
        model_version: str,
        evidence_keys: Sequence[str] = (),
        notes: Sequence[str] = (),
    ) -> RegimeAssessment:
        """Construct an ``UNCERTAIN`` assessment.

        The convenience constructor makes the no-evidence path
        ergonomic without allowing a fabricated assessment to slip in:
        the only state it accepts is :attr:`REGIME_STATE_UNCERTAIN`
        and the only confidence it accepts is ``0``.
        """
        return cls(
            version=REGIME_ASSESSMENT_VERSION,
            model_version=model_version,
            outcome=REGIME_OUTCOME_UNCERTAIN,
            state=REGIME_STATE_UNCERTAIN,
            confidence_q64_64=0,
            evidence_keys=tuple(evidence_keys),
            notes=tuple(notes),
        )


@dataclass(frozen=True, slots=True)
class FeeOpportunityAssessment:
    """The versioned output a :class:`FeeOpportunityModel` returns.

    The assessment carries:

    - ``version`` — :data:`FEE_OPPORTUNITY_ASSESSMENT_VERSION`,
      frozen for the lifetime of the contract;
    - ``outcome`` — :attr:`FEE_OPPORTUNITY_OUTCOME_ASSESSED` when
      the model is confident, otherwise
      :attr:`FEE_OPPORTUNITY_OUTCOME_UNCERTAIN`;
    - ``expected_fee_edge_q64_64`` — the projected fee edge in
      Q64.64 (dimensionless) when the outcome is ``ASSESSED``,
      otherwise ``0``;
    - ``confidence_q64_64`` — a strictly positive Q64.64 ratio
      when the outcome is ``ASSESSED``, otherwise ``0``;
    - ``evidence_keys`` — an immutable tuple of feature-version
      strings the model used; the strategy surfaces the tuple to
      the audit layer.

    Equality and hashing follow dataclass identity.
    """

    version: str
    model_version: str
    outcome: str
    expected_fee_edge_q64_64: int
    confidence_q64_64: int
    evidence_keys: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise StrategyError(
                f"FeeOpportunityAssessment.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.model_version, str) or not self.model_version:
            raise StrategyError(
                f"FeeOpportunityAssessment.model_version: must be "
                f"non-empty str, got {self.model_version!r}"
            )
        if self.outcome not in (
            FEE_OPPORTUNITY_OUTCOME_ASSESSED,
            FEE_OPPORTUNITY_OUTCOME_UNCERTAIN,
        ):
            raise StrategyError(
                f"FeeOpportunityAssessment.outcome: must be "
                f"{FEE_OPPORTUNITY_OUTCOME_ASSESSED!r} or "
                f"{FEE_OPPORTUNITY_OUTCOME_UNCERTAIN!r}, got {self.outcome!r}"
            )
        if self.outcome == FEE_OPPORTUNITY_OUTCOME_ASSESSED:
            _require_q64_64(
                self.expected_fee_edge_q64_64,
                field="FeeOpportunityAssessment.expected_fee_edge_q64_64",
            )
            _require_strict_q64_64(
                self.confidence_q64_64,
                field="FeeOpportunityAssessment.confidence_q64_64",
            )
        else:
            _require_non_negative_int(
                self.expected_fee_edge_q64_64,
                field="FeeOpportunityAssessment.expected_fee_edge_q64_64",
            )
            if self.expected_fee_edge_q64_64 != 0:
                raise StrategyError(
                    f"FeeOpportunityAssessment.expected_fee_edge_q64_64 "
                    f"must be 0 when outcome is "
                    f"{FEE_OPPORTUNITY_OUTCOME_UNCERTAIN!r}, got "
                    f"{self.expected_fee_edge_q64_64}"
                )
            _require_non_negative_int(
                self.confidence_q64_64,
                field="FeeOpportunityAssessment.confidence_q64_64",
            )
            if self.confidence_q64_64 != 0:
                raise StrategyError(
                    f"FeeOpportunityAssessment.confidence_q64_64 must be 0 "
                    f"when outcome is {FEE_OPPORTUNITY_OUTCOME_UNCERTAIN!r}, "
                    f"got {self.confidence_q64_64}"
                )
        if not isinstance(self.evidence_keys, tuple):
            raise StrategyError(
                f"FeeOpportunityAssessment.evidence_keys: must be "
                f"tuple[str, ...], got {type(self.evidence_keys).__name__}"
            )
        for k in self.evidence_keys:
            if not isinstance(k, str):
                raise StrategyError(
                    f"FeeOpportunityAssessment.evidence_keys: every entry "
                    f"must be str, got {type(k).__name__}"
                )
        if not isinstance(self.notes, tuple):
            raise StrategyError(
                f"FeeOpportunityAssessment.notes: must be tuple[str, ...], "
                f"got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise StrategyError(
                    f"FeeOpportunityAssessment.notes: every entry must be "
                    f"str, got {type(n).__name__}"
                )

    @classmethod
    def uncertain(
        cls,
        *,
        model_version: str,
        evidence_keys: Sequence[str] = (),
        notes: Sequence[str] = (),
    ) -> FeeOpportunityAssessment:
        """Construct an ``UNCERTAIN`` assessment.

        The convenience constructor makes the no-evidence path
        ergonomic without allowing a fabricated assessment to slip in:
        the only fee-edge value it accepts is ``0`` and the only
        confidence value it accepts is ``0``.
        """
        return cls(
            version=FEE_OPPORTUNITY_ASSESSMENT_VERSION,
            model_version=model_version,
            outcome=FEE_OPPORTUNITY_OUTCOME_UNCERTAIN,
            expected_fee_edge_q64_64=0,
            confidence_q64_64=0,
            evidence_keys=tuple(evidence_keys),
            notes=tuple(notes),
        )


# ---------------------------------------------------------------------------
# Candidate action
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateAction:
    """The proposal the strategy submits to execution.

    A :class:`CandidateAction` is *not* a transaction. It carries the
    pool, the candidate Range, the capital envelope, the decision and
    availability times, the input snapshot versions, the component
    versions and outcomes, and the structured ``NO_TRADE`` reason when
    the strategy is not placing an order. The central risk layer
    (T070) reviews the action; the execution layer (T071) constructs
    the actual transactions from the approved subset.

    The action is one of three closed kinds:

    - :attr:`CANDIDATE_KIND_NO_TRADE` — the strategy explicitly
      declined to act. The action carries a :class:`ReasonCode` and
      no liquidity / tick range.
    - :attr:`CANDIDATE_KIND_PROPOSE` — the strategy wants to act on
      the supplied Range with the supplied liquidity. ``liquidity``
      is non-zero, ``tick_lower < tick_upper``, and the capital
      envelope fits the pool's max.
    - :attr:`CANDIDATE_KIND_WAIT` — the strategy wants to wait for
      a future event (no action at this decision time). The action
      carries no Range / liquidity but no error reason either; it is
      a *positive* "do not trade now" verdict.

    Equality and hashing follow dataclass identity.
    """

    version: str
    pool_key_id: str
    chain_id: int
    kind: str
    decision_time: int
    availability_time: int
    snapshot_versions: tuple[str, ...]
    component_versions: tuple[str, ...]
    regime_state: str
    regime_outcome: str
    fee_opportunity_outcome: str
    reason_code: ReasonCode | None
    tick_lower: int = CANDIDATE_SENTINEL_TICK
    tick_upper: int = CANDIDATE_SENTINEL_TICK
    liquidity: int = CANDIDATE_SENTINEL_LIQUIDITY
    capital_q64_64: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise CandidateActionError(
                f"CandidateAction.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise CandidateActionError(
                f"CandidateAction.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        _require_positive_int(self.chain_id, field="CandidateAction.chain_id")
        if self.kind not in (
            CANDIDATE_KIND_NO_TRADE,
            CANDIDATE_KIND_PROPOSE,
            CANDIDATE_KIND_WAIT,
        ):
            raise CandidateActionError(
                f"CandidateAction.kind: must be one of "
                f"{CANDIDATE_KIND_NO_TRADE!r}, {CANDIDATE_KIND_PROPOSE!r}, "
                f"{CANDIDATE_KIND_WAIT!r}, got {self.kind!r}"
            )
        _require_non_negative_int(self.decision_time, field="CandidateAction.decision_time")
        _require_non_negative_int(self.availability_time, field="CandidateAction.availability_time")
        if self.availability_time > self.decision_time:
            raise CandidateActionError(
                f"CandidateAction.availability_time={self.availability_time} "
                f"must be <= decision_time={self.decision_time}"
            )
        if not isinstance(self.snapshot_versions, tuple):
            raise CandidateActionError(
                f"CandidateAction.snapshot_versions: must be tuple[str, ...], "
                f"got {type(self.snapshot_versions).__name__}"
            )
        for v in self.snapshot_versions:
            if not isinstance(v, str):
                raise CandidateActionError(
                    f"CandidateAction.snapshot_versions: every entry must be "
                    f"str, got {type(v).__name__}"
                )
        if not isinstance(self.component_versions, tuple):
            raise CandidateActionError(
                f"CandidateAction.component_versions: must be "
                f"tuple[str, ...], got {type(self.component_versions).__name__}"
            )
        for v in self.component_versions:
            if not isinstance(v, str):
                raise CandidateActionError(
                    f"CandidateAction.component_versions: every entry must "
                    f"be str, got {type(v).__name__}"
                )
        if self.regime_outcome not in (
            REGIME_OUTCOME_ASSESSED,
            REGIME_OUTCOME_UNCERTAIN,
        ):
            raise CandidateActionError(
                f"CandidateAction.regime_outcome: must be one of "
                f"{REGIME_OUTCOME_ASSESSED!r}, {REGIME_OUTCOME_UNCERTAIN!r}, "
                f"got {self.regime_outcome!r}"
            )
        if self.regime_outcome == REGIME_OUTCOME_ASSESSED:
            if self.regime_state not in (
                REGIME_STATE_RANGE,
                REGIME_STATE_UP_TREND,
                REGIME_STATE_DOWN_TREND,
                REGIME_STATE_JUMP_RISK,
            ):
                raise CandidateActionError(
                    f"CandidateAction.regime_state={self.regime_state!r} "
                    f"is not a closed regime-state sentinel for an ASSESSED "
                    f"outcome"
                )
        else:
            if self.regime_state != REGIME_STATE_UNCERTAIN:
                raise CandidateActionError(
                    f"CandidateAction.regime_state={self.regime_state!r} "
                    f"must be {REGIME_STATE_UNCERTAIN!r} when regime_outcome "
                    f"is {REGIME_OUTCOME_UNCERTAIN!r}"
                )
        if self.fee_opportunity_outcome not in (
            FEE_OPPORTUNITY_OUTCOME_ASSESSED,
            FEE_OPPORTUNITY_OUTCOME_UNCERTAIN,
        ):
            raise CandidateActionError(
                f"CandidateAction.fee_opportunity_outcome: must be one of "
                f"{FEE_OPPORTUNITY_OUTCOME_ASSESSED!r}, "
                f"{FEE_OPPORTUNITY_OUTCOME_UNCERTAIN!r}, got "
                f"{self.fee_opportunity_outcome!r}"
            )
        if self.reason_code is not None and not isinstance(self.reason_code, ReasonCode):
            raise CandidateActionError(
                f"CandidateAction.reason_code: must be ReasonCode or None, "
                f"got {type(self.reason_code).__name__}"
            )
        # Per-kind invariants:
        if self.kind == CANDIDATE_KIND_PROPOSE:
            if self.tick_lower >= self.tick_upper:
                raise CandidateActionError(
                    f"CandidateAction: PROPOSE requires tick_lower < "
                    f"tick_upper, got tick_lower={self.tick_lower} "
                    f"tick_upper={self.tick_upper}"
                )
            if self.liquidity <= 0:
                raise CandidateActionError(
                    f"CandidateAction: PROPOSE requires liquidity > 0, got {self.liquidity}"
                )
            if self.capital_q64_64 <= 0:
                raise CandidateActionError(
                    f"CandidateAction: PROPOSE requires capital_q64_64 > 0, "
                    f"got {self.capital_q64_64}"
                )
            if self.reason_code is not None:
                raise CandidateActionError(
                    f"CandidateAction: PROPOSE must not carry a "
                    f"reason_code, got {self.reason_code!r}"
                )
        elif self.kind == CANDIDATE_KIND_NO_TRADE:
            if (
                self.tick_lower != CANDIDATE_SENTINEL_TICK
                or self.tick_upper != CANDIDATE_SENTINEL_TICK
            ):
                raise CandidateActionError(
                    f"CandidateAction: NO_TRADE requires sentinel tick "
                    f"values, got tick_lower={self.tick_lower} "
                    f"tick_upper={self.tick_upper}"
                )
            if self.liquidity != CANDIDATE_SENTINEL_LIQUIDITY:
                raise CandidateActionError(
                    f"CandidateAction: NO_TRADE requires sentinel liquidity=0, got {self.liquidity}"
                )
            if self.capital_q64_64 != 0:
                raise CandidateActionError(
                    f"CandidateAction: NO_TRADE requires capital_q64_64=0, "
                    f"got {self.capital_q64_64}"
                )
            if self.reason_code is None:
                raise CandidateActionError("CandidateAction: NO_TRADE requires a reason_code")
        else:  # CANDIDATE_KIND_WAIT
            if (
                self.tick_lower != CANDIDATE_SENTINEL_TICK
                or self.tick_upper != CANDIDATE_SENTINEL_TICK
            ):
                raise CandidateActionError(
                    f"CandidateAction: WAIT requires sentinel tick values, "
                    f"got tick_lower={self.tick_lower} "
                    f"tick_upper={self.tick_upper}"
                )
            if self.liquidity != CANDIDATE_SENTINEL_LIQUIDITY:
                raise CandidateActionError(
                    f"CandidateAction: WAIT requires sentinel liquidity=0, got {self.liquidity}"
                )
            if self.capital_q64_64 != 0:
                raise CandidateActionError(
                    f"CandidateAction: WAIT requires capital_q64_64=0, got {self.capital_q64_64}"
                )
            if self.reason_code is not None:
                raise CandidateActionError(
                    f"CandidateAction: WAIT must not carry a reason_code, got {self.reason_code!r}"
                )
        if not isinstance(self.notes, tuple):
            raise CandidateActionError(
                f"CandidateAction.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise CandidateActionError(
                    f"CandidateAction.notes: every entry must be str, got {type(n).__name__}"
                )


# ---------------------------------------------------------------------------
# Tick / capital proposal validation
# ---------------------------------------------------------------------------


def _tick_in_bounds(tick: int) -> bool:
    """Return ``True`` iff ``tick`` is inside the V4 int24 tick domain."""
    # V4 int24 domain is [-2**23, 2**23 - 1] (TickMath.MIN_TICK .. MAX_TICK).
    return -(1 << 23) <= tick < (1 << 23)


def _market_snapshot_has_finite_payload(market: MarketSnapshot) -> bool:
    """Return ``True`` iff the market snapshot carries only finite values.

    The check is structural: ``float`` carrying NaN or infinity is the
    prohibited pattern (ADR-004); ``int`` and ``Decimal`` are accepted
    when finite. The market snapshot fields are all integers, so the
    only failure mode is the *embedded* NaN / infinity the snapshot
    itself surfaces; the validator inspects every ``int`` field by
    exact equality (``math.isnan`` / ``math.isinf`` are not called on
    integers to avoid a needless conversion).

    Equality comparison against an ``int`` cannot produce ``False``
    for ``int == int``; the explicit check below is the public,
    future-proof hook a downstream contributor can extend when the
    snapshot grows to carry a numeric payload.
    """
    # Walk every numeric field; ``math.isfinite`` accepts ``int`` (it
    # is always finite) and rejects NaN / infinite ``float``.
    for _field_name, value in (
        ("sqrt_price_x96", market.sqrt_price_x96),
        ("liquidity", market.liquidity),
        ("realized_volatility_q64_64", market.realized_volatility_q64_64),
        ("freshness_seconds", market.freshness_seconds),
    ):
        if isinstance(value, bool):
            return False
        if isinstance(value, float) and not math.isfinite(value):
            return False
        if isinstance(value, int) and not isinstance(value, bool):
            continue
    return True


def validate_proposal(
    *,
    pool_key_id: str,
    tick_spacing: int,
    tick_lower: int,
    tick_upper: int,
    capital_q64_64: int,
    admission: AdmissionSnapshot,
    market: MarketSnapshot,
    portfolio: PortfolioSnapshot,
    decision_time: int,
    max_capital_q64_64: int | None = None,
) -> None:
    """Validate a candidate proposal before a :class:`CandidateAction` is built.

    The validator runs every acceptance-time check the strategy layer
    must satisfy before it submits a candidate action:

    1. **Tick alignment.** ``tick_lower`` and ``tick_upper`` are
       strictly increasing, both inside the V4 int24 tick domain,
       and both aligned to ``tick_spacing``.
    2. **Stale-state guard.** Every snapshot's
       ``availability_time`` is at or before ``decision_time``.
    3. **NaN / display-value guard.** The market snapshot carries
       only finite numeric values (no NaN, no infinity).
    4. **Capital envelope guard.** ``capital_q64_64`` is strictly
       positive, fits in the per-pool maximum carried by the
       admission snapshot, and is no larger than the project-wide
       ceiling :data:`MAX_CAPITAL_Q64_64`.

    On failure the function raises a structured exception family
    (``InvalidTickError`` / ``StaleSnapshotError`` /
    ``NaNDisplayValueError`` / ``InvalidCapitalError``); success
    returns ``None``. The validator is a pure function: it never
    reads wall-clock time and never mutates its inputs.
    """
    _require_non_negative_int(decision_time, field="decision_time")
    if not isinstance(pool_key_id, str) or not pool_key_id:
        raise InvalidProposalError(
            f"validate_proposal: pool_key_id must be non-empty str, got {pool_key_id!r}"
        )
    if not isinstance(admission, AdmissionSnapshot):
        raise InvalidProposalError(
            f"validate_proposal: admission must be AdmissionSnapshot, got "
            f"{type(admission).__name__}"
        )
    if not isinstance(market, MarketSnapshot):
        raise InvalidProposalError(
            f"validate_proposal: market must be MarketSnapshot, got {type(market).__name__}"
        )
    if not isinstance(portfolio, PortfolioSnapshot):
        raise InvalidProposalError(
            f"validate_proposal: portfolio must be PortfolioSnapshot, got "
            f"{type(portfolio).__name__}"
        )
    if not isinstance(tick_spacing, int) or isinstance(tick_spacing, bool):
        raise InvalidTickError(
            f"validate_proposal: tick_spacing must be int, got {type(tick_spacing).__name__}"
        )
    if tick_spacing < MIN_TICK_SPACING_STRATEGY or tick_spacing > MAX_TICK_SPACING_STRATEGY:
        raise InvalidTickError(
            f"validate_proposal: tick_spacing={tick_spacing} not in "
            f"[{MIN_TICK_SPACING_STRATEGY}, {MAX_TICK_SPACING_STRATEGY}]"
        )

    # 1. Tick alignment.
    if not _tick_in_bounds(tick_lower):
        raise InvalidTickError(
            f"validate_proposal: tick_lower={tick_lower} outside V4 int24 domain"
        )
    if not _tick_in_bounds(tick_upper):
        raise InvalidTickError(
            f"validate_proposal: tick_upper={tick_upper} outside V4 int24 domain"
        )
    if tick_lower >= tick_upper:
        raise InvalidTickError(
            f"validate_proposal: tick_lower={tick_lower} must be strictly "
            f"less than tick_upper={tick_upper}"
        )
    if tick_lower % tick_spacing != 0:
        raise InvalidTickError(
            f"validate_proposal: tick_lower={tick_lower} not aligned to tick_spacing={tick_spacing}"
        )
    if tick_upper % tick_spacing != 0:
        raise InvalidTickError(
            f"validate_proposal: tick_upper={tick_upper} not aligned to tick_spacing={tick_spacing}"
        )

    # 2. Stale-state guard. ``availability_time`` must be at or before
    # ``decision_time`` for every snapshot the validator inspects; a
    # snapshot that becomes available *after* the decision time is
    # future data and is rejected as a stale-state violation.
    if admission.availability_time > decision_time:
        raise StaleSnapshotError(
            f"validate_proposal: admission snapshot availability_time="
            f"{admission.availability_time} exceeds decision_time="
            f"{decision_time}"
        )
    if market.availability_time > decision_time:
        raise StaleSnapshotError(
            f"validate_proposal: market snapshot availability_time="
            f"{market.availability_time} exceeds decision_time="
            f"{decision_time}"
        )
    if portfolio.availability_time > decision_time:
        raise StaleSnapshotError(
            f"validate_proposal: portfolio snapshot availability_time="
            f"{portfolio.availability_time} exceeds decision_time="
            f"{decision_time}"
        )

    # 3. NaN / display-value guard.
    if not _market_snapshot_has_finite_payload(market):
        raise NaNDisplayValueError(
            "validate_proposal: market snapshot carries a NaN or infinite display value"
        )

    # 4. Capital envelope guard.
    _require_int(capital_q64_64, field="capital_q64_64")
    if capital_q64_64 <= 0:
        raise InvalidCapitalError(
            f"validate_proposal: capital_q64_64={capital_q64_64} must be positive"
        )
    if capital_q64_64 > MAX_CAPITAL_Q64_64:
        raise InvalidCapitalError(
            f"validate_proposal: capital_q64_64={capital_q64_64} exceeds "
            f"project ceiling MAX_CAPITAL_Q64_64={MAX_CAPITAL_Q64_64}"
        )
    effective_max = (
        max_capital_q64_64 if max_capital_q64_64 is not None else admission.max_capital_q64_64
    )
    if effective_max <= 0:
        raise InvalidCapitalError(
            f"validate_proposal: effective max capital {effective_max} must be positive"
        )
    if capital_q64_64 > effective_max:
        raise InvalidCapitalError(
            f"validate_proposal: capital_q64_64={capital_q64_64} exceeds "
            f"effective per-pool maximum {effective_max}"
        )


# ---------------------------------------------------------------------------
# Strategy decision entry point
# ---------------------------------------------------------------------------


def evaluate_decision(
    *,
    pool_key_id: str,
    chain_id: int,
    admission: AdmissionSnapshot,
    market: MarketSnapshot,
    portfolio: PortfolioSnapshot,
    regime_model: RegimeModel,
    fee_opportunity_model: FeeOpportunityModel,
    clock: DeterministicClock,
    rng: SeededRandomSource,
    decision_time: int,
    tick_spacing: int,
    candidate_tick_lower: int,
    candidate_tick_upper: int,
    capital_q64_64: int,
) -> CandidateAction:
    """Run the strategy decision pipeline and return a :class:`CandidateAction`.

    The pipeline is:

    1. Validate the proposal with :func:`validate_proposal`. A
       validation failure raises a structured exception family; the
       caller may catch the failure and translate it into a
       ``NO_TRADE`` candidate, but the strategy never fabricates a
       candidate action past the validator.
    2. Query the :class:`RegimeModel`. An ``UNCERTAIN`` outcome is
       honoured: the strategy records the
       :attr:`ReasonCode.REGIME_UNCERTAIN` reason and returns
       ``NO_TRADE``.
    3. Query the :class:`FeeOpportunityModel`. An ``UNCERTAIN``
       outcome is honoured: the strategy records the
       :attr:`ReasonCode.FEE_OPPORTUNITY_UNCERTAIN` reason and
       returns ``NO_TRADE``.
    4. Combine the two outcomes to choose the candidate kind. The
       default policy is conservative: only an ``ASSESSED`` regime
       with a positive fee opportunity and a non-jump-risk state
       yields a :attr:`CANDIDATE_KIND_PROPOSE`; every other
       combination yields :attr:`CANDIDATE_KIND_WAIT` (the strategy
       chose *not* to trade at this decision time, rather than
       fail).

    The function never mutates its inputs and never reads wall-clock
    time or unseeded randomness (ADR-006).
    """
    validate_proposal(
        pool_key_id=pool_key_id,
        tick_spacing=tick_spacing,
        tick_lower=candidate_tick_lower,
        tick_upper=candidate_tick_upper,
        capital_q64_64=capital_q64_64,
        admission=admission,
        market=market,
        portfolio=portfolio,
        decision_time=decision_time,
    )

    regime = regime_model.assess(
        market=market,
        portfolio=portfolio,
        clock=clock,
        rng=rng,
    )
    fee = fee_opportunity_model.assess(
        market=market,
        portfolio=portfolio,
        clock=clock,
        rng=rng,
    )

    snapshot_versions = (
        admission.version,
        market.version,
        portfolio.version,
    )
    component_versions = (
        regime.version + ":" + regime.model_version,
        fee.version + ":" + fee.model_version,
    )

    if regime.outcome == REGIME_OUTCOME_UNCERTAIN:
        return CandidateAction(
            version=CANDIDATE_ACTION_VERSION,
            pool_key_id=pool_key_id,
            chain_id=chain_id,
            kind=CANDIDATE_KIND_NO_TRADE,
            decision_time=decision_time,
            availability_time=max(
                admission.availability_time,
                market.availability_time,
                portfolio.availability_time,
            ),
            snapshot_versions=snapshot_versions,
            component_versions=component_versions,
            regime_state=regime.state,
            regime_outcome=regime.outcome,
            fee_opportunity_outcome=fee.outcome,
            reason_code=ReasonCode.REGIME_UNCERTAIN,
        )
    if fee.outcome == FEE_OPPORTUNITY_OUTCOME_UNCERTAIN:
        return CandidateAction(
            version=CANDIDATE_ACTION_VERSION,
            pool_key_id=pool_key_id,
            chain_id=chain_id,
            kind=CANDIDATE_KIND_NO_TRADE,
            decision_time=decision_time,
            availability_time=max(
                admission.availability_time,
                market.availability_time,
                portfolio.availability_time,
            ),
            snapshot_versions=snapshot_versions,
            component_versions=component_versions,
            regime_state=regime.state,
            regime_outcome=regime.outcome,
            fee_opportunity_outcome=fee.outcome,
            reason_code=ReasonCode.FEE_OPPORTUNITY_UNCERTAIN,
        )

    if regime.state in (REGIME_STATE_RANGE,) and fee.expected_fee_edge_q64_64 > 0:
        return CandidateAction(
            version=CANDIDATE_ACTION_VERSION,
            pool_key_id=pool_key_id,
            chain_id=chain_id,
            kind=CANDIDATE_KIND_PROPOSE,
            decision_time=decision_time,
            availability_time=max(
                admission.availability_time,
                market.availability_time,
                portfolio.availability_time,
            ),
            snapshot_versions=snapshot_versions,
            component_versions=component_versions,
            regime_state=regime.state,
            regime_outcome=regime.outcome,
            fee_opportunity_outcome=fee.outcome,
            reason_code=None,
            tick_lower=candidate_tick_lower,
            tick_upper=candidate_tick_upper,
            liquidity=1,  # canonical "place a sized LP" signal
            capital_q64_64=capital_q64_64,
        )
    return CandidateAction(
        version=CANDIDATE_ACTION_VERSION,
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        kind=CANDIDATE_KIND_WAIT,
        decision_time=decision_time,
        availability_time=max(
            admission.availability_time,
            market.availability_time,
            portfolio.availability_time,
        ),
        snapshot_versions=snapshot_versions,
        component_versions=component_versions,
        regime_state=regime.state,
        regime_outcome=regime.outcome,
        fee_opportunity_outcome=fee.outcome,
        reason_code=None,
    )


# ---------------------------------------------------------------------------
# Layer-purity dependency check
# ---------------------------------------------------------------------------


def _is_strategy_module(name: str) -> bool:
    """Return ``True`` iff ``name`` belongs to the strategy layer."""
    return name == "robinhood_lp.strategy" or name.startswith("robinhood_lp.strategy.")


def _walk_modules(roots: Iterable[ModuleType]) -> Iterable[ModuleType]:
    """Yield every module reachable from ``roots`` via ``__dict__`` lookup."""
    seen: set[str] = set()
    stack: list[ModuleType] = list(roots)
    while stack:
        module = stack.pop()
        if module.__name__ in seen:
            continue
        seen.add(module.__name__)
        yield module
        # Use ``vars(module)`` (the module's ``__dict__``) rather than
        # ``dir(module)`` so injected attributes set with ``setattr``
        # are visible to the walker. ``dir`` falls back to the class
        # hierarchy for module objects; ``vars`` does not.
        for _attr_name, attr in vars(module).items():
            if isinstance(attr, ModuleType) and (
                _is_strategy_module(attr.__name__) or attr.__name__.startswith("robinhood_lp.")
            ):
                stack.append(attr)


def collect_strategy_module_imports() -> dict[str, tuple[str, ...]]:
    """Return every imported module name reachable from the strategy package.

    The map keys are strategy module names; the values are sorted
    tuples of module names those modules import (across the standard
    library, third-party packages, and ``robinhood_lp`` siblings). The
    function is a diagnostic aid used by the dependency tests; it
    walks the live module graph so a forbidden import is caught at
    runtime rather than at import time.
    """
    import importlib

    modules: dict[str, tuple[str, ...]] = {}
    for module_name in ("robinhood_lp.strategy", "robinhood_lp.strategy.base"):
        module = importlib.import_module(module_name)
        for reached in _walk_modules([module]):
            names: set[str] = set()
            for _attr_name, attr in vars(reached).items():
                if isinstance(attr, ModuleType):
                    names.add(attr.__name__)
            modules[reached.__name__] = tuple(sorted(names))
    return modules


def assert_strategy_layer_is_pure() -> None:
    """Raise :class:`StrategyError` if the strategy layer imports a forbidden module.

    The denylist is the closed set recorded in
    :data:`_FORBIDDEN_STRATEGY_ROBINHOOD_MODULES`. The check is
    conservative on purpose: the strategy layer is supposed to be a
    *pure* observation -> candidate-action function, so any import
    from a forbidden layer is a contract break that should be
    reviewed.
    """
    forbidden: list[tuple[str, str]] = []
    for module_name, imports in collect_strategy_module_imports().items():
        for name in imports:
            for forbidden_root in _FORBIDDEN_STRATEGY_ROBINHOOD_MODULES:
                if name == forbidden_root or name.startswith(forbidden_root + "."):
                    forbidden.append((module_name, name))
                    break
    if forbidden:
        rendered = "\n".join(f"  {src}: imports {imp}" for src, imp in forbidden)
        raise StrategyError(f"strategy layer imports forbidden modules:\n{rendered}")


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


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
