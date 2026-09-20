"""USDG-first adaptive-Range rule strategy (T065).

T065 delivers the *first* strategy implementation on top of the T060
contracts: a rule-based USDG-first adaptive-Range policy that uses
replaceable components and produces auditable entry, hold/wait,
rebalance and exit candidates. The module deliberately does **not**
re-implement the T060 contracts; it consumes them through stable
input / output shapes (snapshots / assessments / candidate action)
and contributes the *behavioural* half the T061 engine wires through
its strategy callback.

Layer boundaries (binding):

- **Strategy input snapshots.** ``AdaptiveMarketSnapshot`` /
  ``AdaptivePortfolioSnapshot`` / ``AdaptiveAdmissionSnapshot`` are
  the T065-specific, versioned, immutable, unit-carrying snapshots
  the strategy reads. They mirror T060's snapshot shapes but extend
  ``AdaptiveMarketSnapshot`` with the 5-minute USDG return the
  ``ECO-REGIME-001`` rule requires, and carry ``reason_codes`` /
  ``uncertainty_codes`` the strategy surfaces on its decisions.
  T060's snapshots are not modified; the T065 adapter projects
  engine-visible events and ledger into the T065 snapshot shape.

- **Replaceable components.** ``AdaptiveRegimeModel`` and
  ``AdaptiveFeeOpportunityModel`` are the substitutable interfaces
  (the same vocabulary STRATEGY_ECONOMICS.md §7 records for V1);
  rule-based reference implementations ship with the module.
  Components return ``AdaptiveRegimeAssessment`` /
  ``AdaptiveFeeOpportunityAssessment`` whose ``outcome`` field is
  ``ASSESSED`` or ``UNCERTAIN``. A component without the evidence
  to assess *must* return ``UNCERTAIN`` rather than fabricate.

- **Candidate action.** ``AdaptiveCandidateAction`` is the
  strategy-layer proposal. It carries the closed vocabulary of
  ``kind`` values (``NO_TRADE`` / ``WAIT`` / ``PROPOSE`` /
  ``WAIT_OUT`` / ``RETURN`` / ``REBUILD`` / ``REBUILD_DEFER`` /
  ``EXIT``), the immutable parameter / input / component versions,
  the regime / fee outcomes, and a structured ``reason_code`` for
  ``NO_TRADE`` candidates. The adapter converts the action to the
  T061 engine ``StrategyDecision``; the strategy layer itself
  never imports the engine.

- **Layer purity.** The module depends only on the standard library
  and the lower protocol-domain contracts. It does not import the
  T061 engine, T062 baselines, the backtest models, RPC, storage,
  configuration, signing, execution, or replay; the dependency
  test in ``tests/test_strategy_t065.py`` enforces this by walking
  the live module graph.

Design constraints (binding):

- **Pure observation -> action.** Every method that produces a
  candidate action returns a *fresh* value and never mutates its
  inputs. The strategy does not read wall-clock time and does not
  call unseeded randomness.

- **Integer-only.** Per ADR-004, ``float`` never appears on the
  protocol / valuation path. Q64.64 ratios and integer tick math
  carry every quantitative value; the strategy never converts to
  ``Decimal`` or ``float`` inside its decision logic.

- **No token appreciation dependency.** The strategy never assumes
  the target token appreciates. Returns are evaluated from a
  regime / jump / Range-occupancy perspective, not from a price-
  direction expectation; the ``Must not`` clause is binding.

- **No short-term fundamentals / sentiment signals.** The regime
  classifier reads only the price / volume / liquidity-distribution
  evidence the engine surfaces. There is no place to thread a
  sentiment or fundamentals field through this module.

- **Low liquidity is never equated with opportunity.** The fee
  opportunity model rejects windows whose volume is too low or
  whose trade-count signals wash-like activity; the regime model
  surfaces a liquidity-withdrawal signal when depth drops, and the
  strategy never *forces* a trade.

- **Risk is never bypassed.** ``AdaptiveCandidateAction`` carries
  the per-decision ``reason_code`` and the input / component /
  parameter versions; the central risk layer (T070) reviews the
  proposal and may reject. The strategy never approves its own
  risk.

References:

- ``docs/spec/strategy/STRATEGY_ECONOMICS.md`` §7 (the component
  boundary and the engine-callback adapter).
- R15 — Uniswap V3 whitepaper (concentrated-liquidity math).
- R16 — Loss-Versus-Rebalancing (the LP opportunity cost framing
  the fee opportunity model is built around).
- R17 — NautilusTrader event-time architecture (the input shape
  the adapter projects to).
- ADR-004 — integer-only protocol / valuation path.
- ADR-006 — strategy layer does not import RPC / storage / signing
  / execution.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import ModuleType
from typing import Final, Protocol, runtime_checkable

from robinhood_lp.protocol.contracts import (
    KIND_OBSERVATION,
    KIND_SWAP,
    SOURCE_PRIORITY_DATA,
    BacktestEvent,
)

# ---------------------------------------------------------------------------
# Module version
# ---------------------------------------------------------------------------

#: Strategy module version. Bumping the version is a breaking change for
#: downstream consumers (the comparison harness, the audit layer). The
#: prefix matches the task that introduced the contract.
ADAPTIVE_STRATEGY_VERSION: Final[str] = "t065.adaptive_strategy.v1"

#: Snapshot version strings. The T065 module defines three snapshots,
#: each with its own version stamp so a downstream consumer can detect
#: the contract break when the strategy reads an older snapshot shape.
ADAPTIVE_MARKET_SNAPSHOT_VERSION: Final[str] = "t065.market_snapshot.v1"
ADAPTIVE_PORTFOLIO_SNAPSHOT_VERSION: Final[str] = "t065.portfolio_snapshot.v1"
ADAPTIVE_ADMISSION_SNAPSHOT_VERSION: Final[str] = "t065.admission_snapshot.v1"

#: Assessment version strings.
ADAPTIVE_REGIME_ASSESSMENT_VERSION: Final[str] = "t065.regime_assessment.v1"
ADAPTIVE_FEE_OPPORTUNITY_ASSESSMENT_VERSION: Final[str] = "t065.fee_opportunity_assessment.v1"

#: Candidate action version.
ADAPTIVE_CANDIDATE_ACTION_VERSION: Final[str] = "t065.candidate_action.v1"

#: Q64.64 fixed-point scale; mirrors ``robinhood_lp.strategy.base``.
Q64_SCALE: Final[int] = 1 << 64

#: Window length of the 5-minute USDG bar in seconds. The same value
#: T050 / T053 pin; the rule is consistent with ``CTRL-MARKET-001``.
_FIVE_MINUTE_WINDOW_SECONDS: Final[int] = 300

#: Default 5-minute USDG return threshold for the ``ECO-REGIME-001``
#: rule. The return ``> 100 %`` is encoded in Q64.64 as ``2 * Q64_SCALE``
#: (the spec phrases it as a return strictly greater than 100 %).
FIVE_MINUTE_RETURN_THRESHOLD_Q64_64: Final[int] = Q64_SCALE  # 1.0 in Q64.64

#: Default minimum completed-bar history length (in seconds) the
#: regime model requires before it may declare an ``ASSESSED`` outcome.
#: Below this length the regime returns ``UNCERTAIN``.
DEFAULT_MIN_HISTORY_SECONDS: Final[int] = 900  # 3 bars

#: Default downside-asymmetric trend thresholds. A downward return
#: crosses the ``DOWN_TREND`` band at a smaller magnitude than the
#: upward return required for ``UP_TREND`` (per ``ECO-REGIME-001``:
#: ``DOWN_TREND`` is stricter than ``UP_TREND`` of the same
#: magnitude).
DEFAULT_DOWN_TREND_THRESHOLD_Q64_64: Final[int] = Q64_SCALE // 20  # 5 % down
DEFAULT_UP_TREND_THRESHOLD_Q64_64: Final[int] = Q64_SCALE // 10  # 10 % up

#: Default downside-asymmetric jump thresholds. A downward jump
#: crosses ``JUMP_RISK`` at a smaller magnitude than the upward jump
#: needed for ``JUMP_RISK`` to fire (asymmetric).
DEFAULT_DOWN_JUMP_THRESHOLD_Q64_64: Final[int] = Q64_SCALE // 10  # 10 % down
DEFAULT_UP_JUMP_THRESHOLD_Q64_64: Final[int] = Q64_SCALE // 4  # 25 % up

#: Default Range-occupancy threshold. Below this in-range fraction
#: the regime classifier downgrades a ``RANGE`` assessment to
#: ``UNCERTAIN``.
DEFAULT_RANGE_OCCUPANCY_THRESHOLD_Q64_64: Final[int] = (Q64_SCALE * 60) // 100  # 60 %

#: Default minimum sample count the fee-opportunity model requires
#: before it returns ``ASSESSED``. Below this it returns
#: ``UNCERTAIN``.
DEFAULT_FEE_MIN_SAMPLES: Final[int] = 5

#: Default maximum acceptable impact of own liquidity on the pool,
#: expressed as Q64.64 (e.g., ``0.1`` = ``10 %``). Above this the
#: fee opportunity downgrades to ``UNCERTAIN`` (low-liquidity /
#: wash-like is never equated with opportunity).
DEFAULT_MAX_OWN_LIQUIDITY_SHARE_Q64_64: Final[int] = (Q64_SCALE * 25) // 100  # 25 %

#: Default rebuild-cost threshold: the projected cost of rebuilding
#: the position (gas + slippage, Q64.64 of capital) divided by the
#: expected fee edge. When this ratio exceeds the threshold the
#: strategy defers the rebuild.
DEFAULT_REBUILD_COST_RATIO_THRESHOLD_Q64_64: Final[int] = (
    Q64_SCALE  # rebuild cost == expected fee edge ⇒ 1.0
)

#: Sentinel tick used when a candidate action does not propose a Range.
SENTINEL_TICK: Final[int] = 0
#: Sentinel liquidity used when a candidate action does not propose a mint.
SENTINEL_LIQUIDITY: Final[int] = 0
#: Sentinel capital used when a candidate action does not allocate.
SENTINEL_CAPITAL: Final[int] = 0

# ---------------------------------------------------------------------------
# Module denylist (strategy layer purity)
# ---------------------------------------------------------------------------
#
# The strategy layer is permitted to import the standard library and
# the lower protocol-domain contracts module. Any other ``robinhood_lp``
# subpackage is a contract break that must be reviewed.
_FORBIDDEN_ADAPTIVE_ROBINHOOD_MODULES: Final[tuple[str, ...]] = (
    "robinhood_lp.backtest",
    "robinhood_lp.config",
    "robinhood_lp.discovery",
    "robinhood_lp.features",
    "robinhood_lp.ingestion",
    "robinhood_lp.presentation",
    "robinhood_lp.qualification",
    "robinhood_lp.quality",
    "robinhood_lp.replay",
    "robinhood_lp.risk",
    "robinhood_lp.rpc",
    "robinhood_lp.signer",
    "robinhood_lp.storage",
    "robinhood_lp.strategy.baselines",
    "robinhood_lp.strategy.engine_adapter",
    "robinhood_lp.execution",
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AdaptiveReasonCode(StrEnum):
    """Stable reason codes for ``AdaptiveCandidateAction`` of kind ``NO_TRADE``.

    The enum extends the vocabulary from :class:`ReasonCode` in the T060
    contracts module; the codes listed here are the T065-specific
    additions. Strings are part of the public contract. New codes are
    additive; renaming or removing a code is a breaking change.

    Categories:

    - **Strategy-hold.** The strategy explicitly chose not to act at
      this decision time (``STRATEGY_HOLD`` mirrors the T060
      sentinel; the additional codes name the specific reason the
      hold was chosen).
    - **Regime.** The regime classifier refused to assess or
      downgraded an assessment to ``UNCERTAIN`` (jump down / jump
      up / liquidity withdrawal / Range occupancy / trend).
    - **Fee-opportunity.** The fee-opportunity model refused to
      assess or downgraded because the volume window was too small
      or the trade pattern looked wash-like.
    - **Range lifecycle.** The strategy is in an out-of-Range state
      and the candidate lifecycle is ``WAIT_OUT`` / ``RETURN`` /
      ``REBUILD_DEFER`` rather than a rebalance.
    - **5-minute rule.** The ``ECO-REGIME-001`` rule fired: the
      complete 5-minute USDG return exceeded 100 %.
    - **Risk.** A risk gate explicitly rejected the candidate. The
      strategy layer does not implement central risk; this code
      records a structured risk signal a higher layer may inspect.
    """

    STRATEGY_HOLD = "ADAPTIVE_STRATEGY_HOLD"

    # Regime-driven rejections
    REGIME_UNCERTAIN = "ADAPTIVE_REGIME_UNCERTAIN"
    JUMP_DOWN_RISK = "ADAPTIVE_JUMP_DOWN_RISK"
    JUMP_UP_RISK = "ADAPTIVE_JUMP_UP_RISK"
    DOWN_TREND_ACTIVE = "ADAPTIVE_DOWN_TREND_ACTIVE"
    UP_TREND_ACTIVE = "ADAPTIVE_UP_TREND_ACTIVE"
    LIQUIDITY_WITHDRAWAL = "ADAPTIVE_LIQUIDITY_WITHDRAWAL"
    RANGE_OCCUPANCY_LOW = "ADAPTIVE_RANGE_OCCUPANCY_LOW"

    # Fee-opportunity-driven rejections
    FEE_OPPORTUNITY_UNCERTAIN = "ADAPTIVE_FEE_OPPORTUNITY_UNCERTAIN"
    LOW_VOLUME_WASH = "ADAPTIVE_LOW_VOLUME_WASH"

    # Range lifecycle
    OUT_OF_RANGE_WAIT = "ADAPTIVE_OUT_OF_RANGE_WAIT"
    OUT_OF_RANGE_RETURN = "ADAPTIVE_OUT_OF_RANGE_RETURN"
    REBUILD_DEFERRED = "ADAPTIVE_REBUILD_DEFERRED"
    REBUILD_COST_EXCESSIVE = "ADAPTIVE_REBUILD_COST_EXCESSIVE"

    # 5-minute USDG rule
    EXTREME_UP_MOVE = "ADAPTIVE_EXTREME_UP_MOVE"

    # Invalidation / data quality
    INVALID_TICK = "ADAPTIVE_INVALID_TICK"
    STALE_DATA = "ADAPTIVE_STALE_DATA"


class AdaptiveCandidateKind(StrEnum):
    """The closed vocabulary of kinds an :class:`AdaptiveCandidateAction` may carry.

    The vocabulary extends T060's three-kind set with the lifecycle
    states the adaptive strategy requires:

    - ``NO_TRADE`` — the strategy explicitly declined to act. The
      action carries a reason code; no Range / liquidity / capital.
    - ``WAIT`` — the strategy is in-range and wants to hold. No
      Range / liquidity / capital.
    - ``PROPOSE`` — the strategy wants to mint a position with the
      supplied Range, liquidity and capital envelope.
    - ``WAIT_OUT`` — the strategy is *out* of Range and is waiting
      for the price to return. The action carries no Range / liquidity
      / capital; the in-strategy position reassessment is recorded
      via the ``position_reassessment`` flag the engine surfaces on
      the audit chain.
    - ``RETURN`` — the strategy detected that the price has re-entered
      the existing Range after an out-of-Range episode. No new
      Range; the action is a positive "the position is back in
      Range" signal that the audit layer can attribute to a return
      event.
    - ``REBUILD`` — the strategy wants to burn the existing position
      and mint a new Range at the current price (after the existing
      Range was left and not returned within the rebuild-cost
      window).
    - ``REBUILD_DEFER`` — the strategy is out of Range and the
      rebuild-cost analysis returned above the configured threshold.
      The action is a structured "defer" verdict that records the
      cost ratio on the audit chain.
    - ``EXIT`` — the strategy wants to burn the existing position
      and not re-enter (used when the 5-minute USDG rule fires
      against a held position).
    """

    NO_TRADE = "NO_TRADE"
    WAIT = "WAIT"
    PROPOSE = "PROPOSE"
    WAIT_OUT = "WAIT_OUT"
    RETURN = "RETURN"
    REBUILD = "REBUILD"
    REBUILD_DEFER = "REBUILD_DEFER"
    EXIT = "EXIT"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AdaptiveStrategyError(ValueError):
    """Base class for adaptive-strategy construction / evaluation failures."""


class InvalidAdaptiveParameterError(AdaptiveStrategyError):
    """An :class:`AdaptiveRangeParameters` field violates its invariant."""


class InvalidAdaptiveSnapshotError(AdaptiveStrategyError):
    """A T065 snapshot violates its invariants."""


class InvalidAdaptiveAssessmentError(AdaptiveStrategyError):
    """An adaptive assessment carries an inconsistent set of fields."""


class InvalidAdaptiveCandidateError(AdaptiveStrategyError):
    """An :class:`AdaptiveCandidateAction` violates its invariants."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a Python ``int`` (``bool`` rejected)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise AdaptiveStrategyError(f"{field}: must be int, got {type(value).__name__}")
    return value


def _require_positive_int(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value <= 0:
        raise AdaptiveStrategyError(f"{field}: must be positive, got {value}")
    return value


def _require_non_negative_int(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value < 0:
        raise AdaptiveStrategyError(f"{field}: must be non-negative, got {value}")
    return value


def _require_q64_64(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value < 0:
        raise AdaptiveStrategyError(f"{field}: must be non-negative Q64.64, got {value}")
    return value


def _require_strict_q64_64(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value <= 0:
        raise AdaptiveStrategyError(f"{field}: must be positive Q64.64, got {value}")
    return value


def _require_uint128(value: int, *, field: str) -> int:
    value = _require_int(value, field=field)
    if value < 0 or value >= (1 << 128):
        raise AdaptiveStrategyError(f"{field}: must fit in uint128, got {value}")
    return value


def _require_uint256(value: int, *, field: str) -> int:
    value = _require_non_negative_int(value, field=field)
    if value >= (1 << 256):
        raise AdaptiveStrategyError(f"{field}: exceeds uint256 width, got {value}")
    return value


# ---------------------------------------------------------------------------
# Parameter schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdaptiveRangeParameters:
    """The immutable parameter schema for the USDG-first adaptive-Range strategy.

    Every parameter the rule-based strategy consumes is a named field
    on this dataclass; the strategy never reads configuration from
    any other source. The dataclass is frozen: a new instance is
    constructed for every parameter-set change, and the strategy
    records the parameter version on every candidate action.

    Field units are explicit:

    - ``five_minute_window_seconds`` — non-negative integer seconds
      (the 5-minute USDG bar window).
    - ``five_minute_return_threshold_q64_64`` — Q64.64 dimensionless
      return; defaults to ``1.0`` (``100 %``). Strictly greater
      than this value triggers the rule.
    - ``min_history_seconds`` — non-negative integer seconds. The
      regime model returns ``UNCERTAIN`` below this history length.
    - ``down_trend_threshold_q64_64`` — Q64.64 dimensionless return.
      A negative return whose magnitude exceeds this value triggers
      ``DOWN_TREND``.
    - ``up_trend_threshold_q64_64`` — Q64.64 dimensionless return.
      A positive return above this value triggers ``UP_TREND``.
    - ``down_jump_threshold_q64_64`` — Q64.64 dimensionless return.
      A single-bar negative return above this value triggers
      ``JUMP_RISK`` *with downward asymmetry*.
    - ``up_jump_threshold_q64_64`` — Q64.64 dimensionless return.
      A single-bar positive return above this value triggers
      ``JUMP_RISK`` *with upward asymmetry*; the value must be
      strictly larger than ``down_jump_threshold_q64_64`` to
      satisfy the downside-asymmetric invariant.
    - ``range_occupancy_threshold_q64_64`` — Q64.64 fraction. The
      regime model requires at least this fraction of in-Range
      observations before it returns ``RANGE``.
    - ``fee_min_samples`` — strictly positive integer. The fee
      opportunity model requires at least this many observations
      before it returns ``ASSESSED``.
    - ``max_own_liquidity_share_q64_64`` — Q64.64 fraction. The fee
      opportunity model rejects when the strategy's own share of
      the pool exceeds this threshold (low liquidity is never
      equated with opportunity).
    - ``rebuild_cost_ratio_threshold_q64_64`` — Q64.64 fraction.
      When projected rebuild cost divided by expected fee edge is
      above this value, the lifecycle emits ``REBUILD_DEFER``.
    - ``half_width_ticks`` — non-negative integer ticks. The
      symmetric Range half-width around the current tick. ``0``
      means "do not propose a new Range" (e.g., the strategy is
      holding-only).
    - ``tick_spacing`` — strictly positive integer ticks.
    - ``liquidity`` — strictly positive uint128. The liquidity
      amount the strategy proposes when minting.
    - ``capital_q64_64`` — strictly positive Q64.64 USDG. The
      capital envelope the strategy proposes when minting.
    - ``max_capital_q64_64`` — strictly positive Q64.64 USDG. The
      per-pool maximum capital envelope; the strategy never
      proposes more than this.
    - ``max_position_hold_seconds`` — non-negative integer seconds.
      ``0`` means "no hold bound".
    - ``max_rebuild_wait_seconds`` — non-negative integer seconds.
      When the price is out of Range longer than this bound the
      strategy emits ``REBUILD`` rather than ``WAIT_OUT``.
    """

    five_minute_window_seconds: int = _FIVE_MINUTE_WINDOW_SECONDS
    five_minute_return_threshold_q64_64: int = FIVE_MINUTE_RETURN_THRESHOLD_Q64_64
    min_history_seconds: int = DEFAULT_MIN_HISTORY_SECONDS
    down_trend_threshold_q64_64: int = DEFAULT_DOWN_TREND_THRESHOLD_Q64_64
    up_trend_threshold_q64_64: int = DEFAULT_UP_TREND_THRESHOLD_Q64_64
    down_jump_threshold_q64_64: int = DEFAULT_DOWN_JUMP_THRESHOLD_Q64_64
    up_jump_threshold_q64_64: int = DEFAULT_UP_JUMP_THRESHOLD_Q64_64
    range_occupancy_threshold_q64_64: int = DEFAULT_RANGE_OCCUPANCY_THRESHOLD_Q64_64
    fee_min_samples: int = DEFAULT_FEE_MIN_SAMPLES
    max_own_liquidity_share_q64_64: int = DEFAULT_MAX_OWN_LIQUIDITY_SHARE_Q64_64
    rebuild_cost_ratio_threshold_q64_64: int = DEFAULT_REBUILD_COST_RATIO_THRESHOLD_Q64_64
    half_width_ticks: int = 600
    tick_spacing: int = 60
    liquidity: int = 1_000
    capital_q64_64: int = 1 << 64
    max_capital_q64_64: int = 1 << 70
    max_position_hold_seconds: int = 0
    max_rebuild_wait_seconds: int = 1_800

    def __post_init__(self) -> None:
        _require_positive_int(
            self.five_minute_window_seconds,
            field="AdaptiveRangeParameters.five_minute_window_seconds",
        )
        _require_strict_q64_64(
            self.five_minute_return_threshold_q64_64,
            field="AdaptiveRangeParameters.five_minute_return_threshold_q64_64",
        )
        _require_non_negative_int(
            self.min_history_seconds,
            field="AdaptiveRangeParameters.min_history_seconds",
        )
        _require_strict_q64_64(
            self.down_trend_threshold_q64_64,
            field="AdaptiveRangeParameters.down_trend_threshold_q64_64",
        )
        _require_strict_q64_64(
            self.up_trend_threshold_q64_64,
            field="AdaptiveRangeParameters.up_trend_threshold_q64_64",
        )
        # Downside asymmetry: the *down* jump threshold must be
        # smaller (tighter) than the *up* jump threshold. The
        # asymmetric band is the binding requirement; the check
        # below rejects a parameter set that violates it.
        _require_strict_q64_64(
            self.down_jump_threshold_q64_64,
            field="AdaptiveRangeParameters.down_jump_threshold_q64_64",
        )
        _require_strict_q64_64(
            self.up_jump_threshold_q64_64,
            field="AdaptiveRangeParameters.up_jump_threshold_q64_64",
        )
        if self.up_jump_threshold_q64_64 <= self.down_jump_threshold_q64_64:
            raise InvalidAdaptiveParameterError(
                "AdaptiveRangeParameters: up_jump_threshold must be "
                "strictly greater than down_jump_threshold (downside "
                "asymmetry); got "
                f"up={self.up_jump_threshold_q64_64} "
                f"down={self.down_jump_threshold_q64_64}"
            )
        if self.up_trend_threshold_q64_64 <= self.down_trend_threshold_q64_64:
            raise InvalidAdaptiveParameterError(
                "AdaptiveRangeParameters: up_trend_threshold must be "
                "strictly greater than down_trend_threshold (downside "
                "asymmetry); got "
                f"up={self.up_trend_threshold_q64_64} "
                f"down={self.down_trend_threshold_q64_64}"
            )
        _require_q64_64(
            self.range_occupancy_threshold_q64_64,
            field="AdaptiveRangeParameters.range_occupancy_threshold_q64_64",
        )
        _require_positive_int(
            self.fee_min_samples,
            field="AdaptiveRangeParameters.fee_min_samples",
        )
        _require_q64_64(
            self.max_own_liquidity_share_q64_64,
            field="AdaptiveRangeParameters.max_own_liquidity_share_q64_64",
        )
        _require_strict_q64_64(
            self.rebuild_cost_ratio_threshold_q64_64,
            field="AdaptiveRangeParameters.rebuild_cost_ratio_threshold_q64_64",
        )
        _require_non_negative_int(
            self.half_width_ticks,
            field="AdaptiveRangeParameters.half_width_ticks",
        )
        _require_positive_int(
            self.tick_spacing,
            field="AdaptiveRangeParameters.tick_spacing",
        )
        # ``liquidity`` must be strictly positive (zero liquidity
        # is a sentinel that means "no position"; the parameter
        # describes a *candidate* mint, so it must be > 0).
        if self.liquidity <= 0 or self.liquidity >= (1 << 128):
            raise InvalidAdaptiveParameterError(
                "AdaptiveRangeParameters.liquidity: must be positive "
                "and fit in uint128, got "
                f"{self.liquidity}"
            )
        _require_strict_q64_64(
            self.capital_q64_64,
            field="AdaptiveRangeParameters.capital_q64_64",
        )
        _require_strict_q64_64(
            self.max_capital_q64_64,
            field="AdaptiveRangeParameters.max_capital_q64_64",
        )
        if self.capital_q64_64 > self.max_capital_q64_64:
            raise InvalidAdaptiveParameterError(
                "AdaptiveRangeParameters: capital_q64_64 must be <= "
                "max_capital_q64_64; got "
                f"capital={self.capital_q64_64} max={self.max_capital_q64_64}"
            )
        _require_non_negative_int(
            self.max_position_hold_seconds,
            field="AdaptiveRangeParameters.max_position_hold_seconds",
        )
        _require_non_negative_int(
            self.max_rebuild_wait_seconds,
            field="AdaptiveRangeParameters.max_rebuild_wait_seconds",
        )


# ---------------------------------------------------------------------------
# Snapshots (immutable, versioned, unit-carrying)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdaptiveAdmissionSnapshot:
    """The per-pool admission evidence the adaptive strategy reads.

    The snapshot is the T065-side projection of the T025 admission
    record. It is independent of T060's :class:`AdmissionSnapshot`
    so the strategy can carry the small set of fields it
    actually consumes without inheriting every validation
    constraint the T060 layer imposes.
    """

    version: str
    pool_key_id: str
    chain_id: int
    support_level: str
    is_admitted: bool
    max_capital_q64_64: int
    data_time: int
    availability_time: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptiveAdmissionSnapshot.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptiveAdmissionSnapshot.pool_key_id: must be non-empty "
                f"str, got {self.pool_key_id!r}"
            )
        _require_positive_int(self.chain_id, field="AdaptiveAdmissionSnapshot.chain_id")
        if not isinstance(self.support_level, str) or not self.support_level:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptiveAdmissionSnapshot.support_level: must be non-empty "
                f"str, got {self.support_level!r}"
            )
        if not isinstance(self.is_admitted, bool):
            raise InvalidAdaptiveSnapshotError(
                f"AdaptiveAdmissionSnapshot.is_admitted: must be bool, got "
                f"{type(self.is_admitted).__name__}"
            )
        _require_strict_q64_64(
            self.max_capital_q64_64,
            field="AdaptiveAdmissionSnapshot.max_capital_q64_64",
        )
        _require_non_negative_int(self.data_time, field="AdaptiveAdmissionSnapshot.data_time")
        _require_non_negative_int(
            self.availability_time,
            field="AdaptiveAdmissionSnapshot.availability_time",
        )
        if self.availability_time < self.data_time:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptiveAdmissionSnapshot.availability_time="
                f"{self.availability_time} must be >= "
                f"data_time={self.data_time}"
            )


@dataclass(frozen=True, slots=True)
class AdaptiveMarketSnapshot:
    """The T065 market snapshot with the 5-minute USDG return.

    The snapshot carries every quantitative field the T065 rule
    strategy consumes plus the 5-minute USDG return the
    ``ECO-REGIME-001`` rule requires. ``five_minute_return_q64_64``
    is ``0`` when the most recent 5-minute bar is incomplete or
    no USDG price is available; the ``five_minute_return_complete``
    flag records the availability / completeness state explicitly
    so the strategy never reads a stale value as complete.

    Field units:

    - ``sqrt_price_x96`` — uint256 (V4 wire format).
    - ``liquidity`` — uint128 (active pool liquidity).
    - ``realized_volatility_q64_64`` — Q64.64 dimensionless ratio.
    - ``freshness_seconds`` — non-negative int seconds.
    - ``quote_q64_64`` — Q64.64 USDG per raw token. ``None`` when
      the dataset is ``RELATIVE_ONLY``.
    - ``is_relative_only`` — bool.
    - ``five_minute_return_q64_64`` — Q64.64 dimensionless return.
      ``0`` when the bar is incomplete or no USDG price is
      available.
    - ``five_minute_return_complete`` — bool. ``True`` only when
      the most recent 5-minute bar has fully closed AND the
      USDG conversion is available.
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
    five_minute_return_q64_64: int
    five_minute_return_complete: bool
    data_time: int
    availability_time: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptiveMarketSnapshot.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptiveMarketSnapshot.pool_key_id: must be non-empty str, "
                f"got {self.pool_key_id!r}"
            )
        _require_positive_int(self.chain_id, field="AdaptiveMarketSnapshot.chain_id")
        _require_uint256(self.sqrt_price_x96, field="AdaptiveMarketSnapshot.sqrt_price_x96")
        if self.sqrt_price_x96 == 0:
            raise InvalidAdaptiveSnapshotError(
                "AdaptiveMarketSnapshot.sqrt_price_x96: must be positive, got 0"
            )
        _require_uint128(self.liquidity, field="AdaptiveMarketSnapshot.liquidity")
        _require_q64_64(
            self.realized_volatility_q64_64,
            field="AdaptiveMarketSnapshot.realized_volatility_q64_64",
        )
        _require_non_negative_int(
            self.freshness_seconds,
            field="AdaptiveMarketSnapshot.freshness_seconds",
        )
        if self.quote_q64_64 is not None:
            _require_strict_q64_64(
                self.quote_q64_64,
                field="AdaptiveMarketSnapshot.quote_q64_64",
            )
        if not isinstance(self.is_relative_only, bool):
            raise InvalidAdaptiveSnapshotError(
                f"AdaptiveMarketSnapshot.is_relative_only: must be bool, got "
                f"{type(self.is_relative_only).__name__}"
            )
        if self.is_relative_only and self.quote_q64_64 is not None:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptiveMarketSnapshot: RELATIVE_ONLY snapshots must not "
                f"carry a quote (quote_q64_64={self.quote_q64_64})"
            )
        _require_q64_64(
            self.five_minute_return_q64_64,
            field="AdaptiveMarketSnapshot.five_minute_return_q64_64",
        )
        if not isinstance(self.five_minute_return_complete, bool):
            raise InvalidAdaptiveSnapshotError(
                f"AdaptiveMarketSnapshot.five_minute_return_complete: must be "
                f"bool, got {type(self.five_minute_return_complete).__name__}"
            )
        if self.five_minute_return_complete and (
            self.is_relative_only or self.quote_q64_64 is None
        ):
            raise InvalidAdaptiveSnapshotError(
                "AdaptiveMarketSnapshot: five_minute_return_complete=True "
                "requires USDG availability"
            )
        if self.five_minute_return_complete and self.five_minute_return_q64_64 == 0:
            raise InvalidAdaptiveSnapshotError(
                "AdaptiveMarketSnapshot: five_minute_return_complete=True "
                "requires five_minute_return_q64_64 > 0"
            )
        _require_non_negative_int(self.data_time, field="AdaptiveMarketSnapshot.data_time")
        _require_non_negative_int(
            self.availability_time,
            field="AdaptiveMarketSnapshot.availability_time",
        )
        if self.availability_time < self.data_time:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptiveMarketSnapshot.availability_time="
                f"{self.availability_time} must be >= "
                f"data_time={self.data_time}"
            )


@dataclass(frozen=True, slots=True)
class AdaptivePortfolioSnapshot:
    """The T065 portfolio snapshot.

    Mirrors the engine's :class:`PositionState` semantics: the
    position is identified by ``position_id``, the Range is
    ``[tick_lower, tick_upper)``, and ``is_empty`` records the
    no-position state. The strategy never mutates the snapshot.
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
    is_empty: bool
    in_range: bool
    last_accrual_time: int
    data_time: int
    availability_time: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptivePortfolioSnapshot.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptivePortfolioSnapshot.pool_key_id: must be non-empty "
                f"str, got {self.pool_key_id!r}"
            )
        _require_positive_int(self.chain_id, field="AdaptivePortfolioSnapshot.chain_id")
        if not isinstance(self.position_id, str) or not self.position_id:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptivePortfolioSnapshot.position_id: must be non-empty "
                f"str, got {self.position_id!r}"
            )
        _require_int(self.tick_lower, field="AdaptivePortfolioSnapshot.tick_lower")
        _require_int(self.tick_upper, field="AdaptivePortfolioSnapshot.tick_upper")
        if not self.is_empty and self.tick_lower >= self.tick_upper:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptivePortfolioSnapshot: tick_lower="
                f"{self.tick_lower} must be < tick_upper="
                f"{self.tick_upper} for a non-empty position"
            )
        _require_uint128(self.liquidity, field="AdaptivePortfolioSnapshot.liquidity")
        _require_uint256(
            self.principal_token0,
            field="AdaptivePortfolioSnapshot.principal_token0",
        )
        _require_uint256(
            self.principal_token1,
            field="AdaptivePortfolioSnapshot.principal_token1",
        )
        _require_uint256(self.tokens_owed0, field="AdaptivePortfolioSnapshot.tokens_owed0")
        _require_uint256(self.tokens_owed1, field="AdaptivePortfolioSnapshot.tokens_owed1")
        if not isinstance(self.is_empty, bool):
            raise InvalidAdaptiveSnapshotError(
                f"AdaptivePortfolioSnapshot.is_empty: must be bool, got "
                f"{type(self.is_empty).__name__}"
            )
        if self.is_empty and self.liquidity != 0:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptivePortfolioSnapshot: is_empty=True requires zero "
                f"liquidity, got {self.liquidity}"
            )
        if not isinstance(self.in_range, bool):
            raise InvalidAdaptiveSnapshotError(
                f"AdaptivePortfolioSnapshot.in_range: must be bool, got "
                f"{type(self.in_range).__name__}"
            )
        _require_non_negative_int(
            self.last_accrual_time,
            field="AdaptivePortfolioSnapshot.last_accrual_time",
        )
        _require_non_negative_int(self.data_time, field="AdaptivePortfolioSnapshot.data_time")
        _require_non_negative_int(
            self.availability_time,
            field="AdaptivePortfolioSnapshot.availability_time",
        )
        if self.availability_time < self.data_time:
            raise InvalidAdaptiveSnapshotError(
                f"AdaptivePortfolioSnapshot.availability_time="
                f"{self.availability_time} must be >= "
                f"data_time={self.data_time}"
            )


# ---------------------------------------------------------------------------
# Component interfaces and outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdaptiveRegimeAssessment:
    """The versioned regime-assessment output.

    The state is one of ``RANGE`` / ``UP_TREND`` / ``DOWN_TREND`` /
    ``JUMP_RISK`` / ``UNCERTAIN``. The ``jumps_down`` flag is
    ``True`` when the jump was a downward move; it is only
    meaningful when ``state == JUMP_RISK``. ``evidence_keys``
    is the closed feature-version tuple the strategy surfaces
    on the audit chain.
    """

    version: str
    model_version: str
    outcome: str  # ASSESSED / UNCERTAIN
    state: str  # RANGE / UP_TREND / DOWN_TREND / JUMP_RISK / UNCERTAIN
    confidence_q64_64: int
    range_occupancy_q64_64: int
    jumps_down: bool
    liquidity_withdrawal: bool
    evidence_keys: tuple[str, ...] = field(default_factory=tuple)
    uncertainty_codes: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise InvalidAdaptiveAssessmentError(
                f"AdaptiveRegimeAssessment.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.model_version, str) or not self.model_version:
            raise InvalidAdaptiveAssessmentError(
                f"AdaptiveRegimeAssessment.model_version: must be non-empty "
                f"str, got {self.model_version!r}"
            )
        if self.outcome not in ("ASSESSED", "UNCERTAIN"):
            raise InvalidAdaptiveAssessmentError(
                f"AdaptiveRegimeAssessment.outcome: must be ASSESSED or "
                f"UNCERTAIN, got {self.outcome!r}"
            )
        valid_states = (
            "RANGE",
            "UP_TREND",
            "DOWN_TREND",
            "JUMP_RISK",
            "UNCERTAIN",
        )
        if self.state not in valid_states:
            raise InvalidAdaptiveAssessmentError(
                f"AdaptiveRegimeAssessment.state: must be one of {valid_states}, got {self.state!r}"
            )
        if self.outcome == "UNCERTAIN" and self.state != "UNCERTAIN":
            raise InvalidAdaptiveAssessmentError(
                f"AdaptiveRegimeAssessment: outcome=UNCERTAIN requires "
                f"state=UNCERTAIN, got state={self.state!r}"
            )
        if self.outcome == "ASSESSED" and self.state == "UNCERTAIN":
            raise InvalidAdaptiveAssessmentError(
                f"AdaptiveRegimeAssessment: outcome=ASSESSED requires a "
                f"non-UNCERTAIN state, got state={self.state!r}"
            )
        if self.outcome == "ASSESSED":
            _require_strict_q64_64(
                self.confidence_q64_64,
                field="AdaptiveRegimeAssessment.confidence_q64_64",
            )
        else:
            if self.confidence_q64_64 != 0:
                raise InvalidAdaptiveAssessmentError(
                    "AdaptiveRegimeAssessment: outcome=UNCERTAIN requires "
                    "confidence_q64_64=0, got "
                    f"{self.confidence_q64_64}"
                )
        _require_q64_64(
            self.range_occupancy_q64_64,
            field="AdaptiveRegimeAssessment.range_occupancy_q64_64",
        )
        if not isinstance(self.jumps_down, bool):
            raise InvalidAdaptiveAssessmentError(
                f"AdaptiveRegimeAssessment.jumps_down: must be bool, got "
                f"{type(self.jumps_down).__name__}"
            )
        if self.state == "JUMP_RISK" and not self.jumps_down and not (self.outcome == "ASSESSED"):
            raise InvalidAdaptiveAssessmentError(
                "AdaptiveRegimeAssessment: state=JUMP_RISK requires ASSESSED"
            )
        if not isinstance(self.liquidity_withdrawal, bool):
            raise InvalidAdaptiveAssessmentError(
                f"AdaptiveRegimeAssessment.liquidity_withdrawal: must be "
                f"bool, got {type(self.liquidity_withdrawal).__name__}"
            )
        if not isinstance(self.evidence_keys, tuple):
            raise InvalidAdaptiveAssessmentError(
                "AdaptiveRegimeAssessment.evidence_keys: must be tuple, "
                f"got {type(self.evidence_keys).__name__}"
            )
        if not isinstance(self.uncertainty_codes, tuple):
            raise InvalidAdaptiveAssessmentError(
                "AdaptiveRegimeAssessment.uncertainty_codes: must be tuple, "
                f"got {type(self.uncertainty_codes).__name__}"
            )
        if not isinstance(self.notes, tuple):
            raise InvalidAdaptiveAssessmentError(
                f"AdaptiveRegimeAssessment.notes: must be tuple, got {type(self.notes).__name__}"
            )

    @classmethod
    def uncertain(
        cls,
        *,
        model_version: str,
        uncertainty_codes: Sequence[str] = (),
        evidence_keys: Sequence[str] = (),
        notes: Sequence[str] = (),
    ) -> AdaptiveRegimeAssessment:
        """Construct an ``UNCERTAIN`` assessment with confidence=0."""
        return cls(
            version=ADAPTIVE_REGIME_ASSESSMENT_VERSION,
            model_version=model_version,
            outcome="UNCERTAIN",
            state="UNCERTAIN",
            confidence_q64_64=0,
            range_occupancy_q64_64=0,
            jumps_down=False,
            liquidity_withdrawal=False,
            evidence_keys=tuple(evidence_keys),
            uncertainty_codes=tuple(uncertainty_codes),
            notes=tuple(notes),
        )


@dataclass(frozen=True, slots=True)
class AdaptiveFeeOpportunityAssessment:
    """The versioned fee-opportunity assessment.

    The assessment carries the dimensionless expected fee edge
    (``expected_fee_edge_q64_64``), the expected range-in fraction
    (``expected_in_range_fraction_q64_64``), the projected own-
    liquidity share (``projected_own_liquidity_share_q64_64``),
    the expected cost ratio (``expected_cost_ratio_q64_64`` =
    cost / fee_edge), and the evidence / uncertainty surface.

    A model without enough evidence returns ``UNCERTAIN`` and
    ``expected_fee_edge_q64_64 == 0``.
    """

    version: str
    model_version: str
    outcome: str  # ASSESSED / UNCERTAIN
    expected_fee_edge_q64_64: int
    expected_in_range_fraction_q64_64: int
    projected_own_liquidity_share_q64_64: int
    expected_cost_ratio_q64_64: int
    sample_count: int
    confidence_q64_64: int
    wash_like: bool
    evidence_keys: tuple[str, ...] = field(default_factory=tuple)
    uncertainty_codes: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.outcome not in ("ASSESSED", "UNCERTAIN"):
            raise InvalidAdaptiveAssessmentError(
                f"AdaptiveFeeOpportunityAssessment.outcome: must be "
                f"ASSESSED or UNCERTAIN, got {self.outcome!r}"
            )
        if not isinstance(self.version, str) or not self.version:
            raise InvalidAdaptiveAssessmentError(
                "AdaptiveFeeOpportunityAssessment.version: must be "
                f"non-empty str, got {self.version!r}"
            )
        if not isinstance(self.model_version, str) or not self.model_version:
            raise InvalidAdaptiveAssessmentError(
                "AdaptiveFeeOpportunityAssessment.model_version: must be "
                f"non-empty str, got {self.model_version!r}"
            )
        if self.outcome == "UNCERTAIN":
            if self.expected_fee_edge_q64_64 != 0:
                raise InvalidAdaptiveAssessmentError(
                    "AdaptiveFeeOpportunityAssessment: outcome=UNCERTAIN "
                    "requires expected_fee_edge_q64_64=0, got "
                    f"{self.expected_fee_edge_q64_64}"
                )
            if self.confidence_q64_64 != 0:
                raise InvalidAdaptiveAssessmentError  # noqa: F821
        _require_q64_64(
            self.expected_fee_edge_q64_64,
            field="AdaptiveFeeOpportunityAssessment.expected_fee_edge_q64_64",
        )
        _require_q64_64(
            self.expected_in_range_fraction_q64_64,
            field="AdaptiveFeeOpportunityAssessment.expected_in_range_fraction_q64_64",
        )
        _require_q64_64(
            self.projected_own_liquidity_share_q64_64,
            field="AdaptiveFeeOpportunityAssessment.projected_own_liquidity_share_q64_64",
        )
        _require_q64_64(
            self.expected_cost_ratio_q64_64,
            field="AdaptiveFeeOpportunityAssessment.expected_cost_ratio_q64_64",
        )
        _require_non_negative_int(
            self.sample_count,
            field="AdaptiveFeeOpportunityAssessment.sample_count",
        )
        if self.outcome == "ASSESSED":
            _require_strict_q64_64(
                self.confidence_q64_64,
                field="AdaptiveFeeOpportunityAssessment.confidence_q64_64",
            )
        if not isinstance(self.wash_like, bool):
            raise InvalidAdaptiveAssessmentError(
                "AdaptiveFeeOpportunityAssessment.wash_like: must be bool, "
                f"got {type(self.wash_like).__name__}"
            )

    @classmethod
    def uncertain(
        cls,
        *,
        model_version: str,
        uncertainty_codes: Sequence[str] = (),
        evidence_keys: Sequence[str] = (),
        notes: Sequence[str] = (),
    ) -> AdaptiveFeeOpportunityAssessment:
        """Construct an ``UNCERTAIN`` assessment with all-zero edges."""
        return cls(
            version=ADAPTIVE_FEE_OPPORTUNITY_ASSESSMENT_VERSION,
            model_version=model_version,
            outcome="UNCERTAIN",
            expected_fee_edge_q64_64=0,
            expected_in_range_fraction_q64_64=0,
            projected_own_liquidity_share_q64_64=0,
            expected_cost_ratio_q64_64=0,
            sample_count=0,
            confidence_q64_64=0,
            wash_like=False,
            evidence_keys=tuple(evidence_keys),
            uncertainty_codes=tuple(uncertainty_codes),
            notes=tuple(notes),
        )


@runtime_checkable
class AdaptiveRegimeModel(Protocol):
    """The replaceable regime-model interface.

    The interface is the *sole* supported extension point for a
    trained regime model. A model-backed implementation must
    satisfy the same contract as the rule implementation: same
    inputs, same outputs, same uncertainty duties.
    """

    @property
    def model_version(self) -> str:
        """The version string of this model instance."""
        ...

    def assess(
        self,
        *,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        visible_events: Sequence[BacktestEvent],
        params: AdaptiveRangeParameters,
    ) -> AdaptiveRegimeAssessment:
        """Return the regime assessment for the supplied state."""
        ...


@runtime_checkable
class AdaptiveFeeOpportunityModel(Protocol):
    """The replaceable fee-opportunity interface."""

    @property
    def model_version(self) -> str:
        """The version string of this model instance."""
        ...

    def assess(
        self,
        *,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        visible_events: Sequence[BacktestEvent],
        params: AdaptiveRangeParameters,
    ) -> AdaptiveFeeOpportunityAssessment:
        """Return the fee-opportunity assessment for the supplied state."""
        ...


# ---------------------------------------------------------------------------
# Rule-based reference implementations
# ---------------------------------------------------------------------------


def _extract_price_q64_64(payload: Sequence[tuple[str, int | str | bool]]) -> int | None:
    """Return the ``price_q64_64`` field from a payload, or ``None``."""
    for key, value in payload:
        if key == "price_q64_64":
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            return value
    return None


def _extract_volume_q64_64(payload: Sequence[tuple[str, int | str | bool]]) -> int | None:
    """Return the ``volume_q64_64`` field from a payload, or ``None``."""
    for key, value in payload:
        if key == "volume_q64_64":
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            return value
    return None


def _data_price_series(
    events: Sequence[BacktestEvent],
) -> tuple[tuple[int, int], ...]:
    """Return the deterministic ``(timestamp, price_q64_64)`` data series.

    Only ``DATA`` / ``SWAP`` / ``OBSERVATION`` events contribute. The
    series is sorted ascending by timestamp and deduplicated on
    identical timestamps by keeping the first occurrence.
    """
    series: list[tuple[int, int]] = []
    seen_ts: set[int] = set()
    for evt in events:
        if evt.source_priority != SOURCE_PRIORITY_DATA:
            continue
        if evt.kind not in (KIND_OBSERVATION, KIND_SWAP):
            continue
        if evt.timestamp in seen_ts:
            continue
        price = _extract_price_q64_64(evt.payload)
        if price is None or price <= 0:
            continue
        series.append((evt.timestamp, price))
        seen_ts.add(evt.timestamp)
    series.sort(key=lambda pair: pair[0])
    return tuple(series)


def _data_volume_series(
    events: Sequence[BacktestEvent],
) -> tuple[tuple[int, int], ...]:
    """Return the deterministic ``(timestamp, volume_q64_64)`` series.

    Events without a ``volume_q64_64`` payload are excluded.
    """
    series: list[tuple[int, int]] = []
    seen_ts: set[int] = set()
    for evt in events:
        if evt.source_priority != SOURCE_PRIORITY_DATA:
            continue
        if evt.kind not in (KIND_OBSERVATION, KIND_SWAP):
            continue
        if evt.timestamp in seen_ts:
            continue
        volume = _extract_volume_q64_64(evt.payload)
        if volume is None:
            continue
        series.append((evt.timestamp, volume))
        seen_ts.add(evt.timestamp)
    series.sort(key=lambda pair: pair[0])
    return tuple(series)


def _data_liquidity_series(
    events: Sequence[BacktestEvent],
) -> tuple[tuple[int, int], ...]:
    """Return the deterministic ``(timestamp, liquidity)`` series.

    Reads the ``active_liquidity`` field from data events.
    """
    series: list[tuple[int, int]] = []
    seen_ts: set[int] = set()
    for evt in events:
        if evt.source_priority != SOURCE_PRIORITY_DATA:
            continue
        if evt.kind not in (KIND_OBSERVATION, KIND_SWAP):
            continue
        if evt.timestamp in seen_ts:
            continue
        for key, value in evt.payload:
            if key == "active_liquidity":
                if isinstance(value, bool) or not isinstance(value, int):
                    break
                if value < 0:
                    break
                series.append((evt.timestamp, value))
                seen_ts.add(evt.timestamp)
                break
    series.sort(key=lambda pair: pair[0])
    return tuple(series)


def _range_occupancy_q64_64(
    series: Sequence[tuple[int, int]],
    *,
    decision_time: int,
    tick_lower: int,
    tick_upper: int,
    pool_price_q64_64: int,
) -> int:
    """Return the in-Range time fraction as a Q64.64 ratio.

    ``0`` when the series is empty or the price cannot be mapped.
    The T065 module uses the *standard* Q64.64 convention
    (``price_q64_64 = price * 2**64``) and converts to the V4
    Q64.96 sqrt-price format with ``isqrt(price_q64_64 << 128)``.
    """
    if not series or tick_lower >= tick_upper or pool_price_q64_64 <= 0:
        return 0
    try:
        from robinhood_lp.protocol.math import get_sqrt_price_at_tick

        sqrt_lower = get_sqrt_price_at_tick(tick_lower)
        sqrt_upper = get_sqrt_price_at_tick(tick_upper)
    except Exception:  # noqa: BLE001 — degrade to 0 on any tick mapping failure
        return 0
    in_range_count = 0
    total = 0
    for _ts, price_q64_64 in series:
        try:
            sqrt_x96 = math.isqrt(price_q64_64 << 128)
        except Exception:  # noqa: BLE001 — degrade to skip
            continue
        total += 1
        if sqrt_lower <= sqrt_x96 < sqrt_upper:
            in_range_count += 1
    if total == 0:
        return 0
    return (in_range_count << 64) // total


def _tick_from_price_q64_64(price_q64_64: int) -> int:
    """Convert a Q64.64 price to a V4 tick via the protocol math module."""
    from robinhood_lp.protocol.math import get_tick_at_sqrt_price

    sqrt_x96 = math.isqrt(price_q64_64 << 128)
    return get_tick_at_sqrt_price(sqrt_x96)


def _snap_to_spacing(tick: int, spacing: int) -> int:
    """Snap ``tick`` to the nearest multiple of ``spacing`` (floor)."""
    if spacing <= 0:
        raise AdaptiveStrategyError(f"_snap_to_spacing: spacing must be positive, got {spacing}")
    if tick >= 0:
        return (tick // spacing) * spacing
    return -((-tick + spacing - 1) // spacing) * spacing


def _compute_returns_q64_64(
    series: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    """Return ``(timestamp, return_q64_64)`` pairs between consecutive prices.

    ``return_q64_64 = (price_t / price_{t-1} - 1) * 2**64``. The
    return is positive for an increase and negative for a decrease;
    the Q64.64 representation is signed only by ``price_t >=
    price_{t-1}``, so consumers must compare against an unsigned
    threshold they hold separately.

    A negative value means ``price_t < price_{t-1}``. To make the
    unsigned comparisons the regime model performs straightforward,
    this helper stores the *signed* delta in a separate field; the
    Q64.64 *positive* scale is used for the absolute value when
    needed.
    """
    out: list[tuple[int, int]] = []
    prev_price: int | None = None
    for ts, price in series:
        if prev_price is not None and prev_price > 0:
            delta = price - prev_price
            ret = (delta << 64) // prev_price if delta >= 0 else -(((-delta) << 64) // prev_price)
            out.append((ts, ret))
        prev_price = price
    return tuple(out)


def _magnitude_q64_64(value: int) -> int:
    """Return the magnitude of a Q64.64 value as a non-negative Q64.64."""
    return value if value >= 0 else -value


@dataclass(frozen=True, slots=True)
class RuleBasedAdaptiveRegimeModel:
    """The reference rule-based regime model.

    The model inspects the visible price history to classify the
    regime as one of ``RANGE`` / ``UP_TREND`` / ``DOWN_TREND`` /
    ``JUMP_RISK`` / ``UNCERTAIN``. Downside asymmetry is enforced
    by the parameter set: a downward jump crosses ``JUMP_RISK`` at
    a smaller magnitude than an upward jump. The model surfaces
    ``range_occupancy_q64_64`` and ``liquidity_withdrawal`` on
    every ``ASSESSED`` outcome.
    """

    model_version: str = ADAPTIVE_STRATEGY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.model_version, str) or not self.model_version:
            raise InvalidAdaptiveParameterError(
                "RuleBasedAdaptiveRegimeModel.model_version: must be "
                f"non-empty str, got {self.model_version!r}"
            )

    @property
    def is_replaceable(self) -> bool:
        """Components are replaceable; the property is a marker for tests."""
        return True

    def assess(
        self,
        *,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        visible_events: Sequence[BacktestEvent],
        params: AdaptiveRangeParameters,
    ) -> AdaptiveRegimeAssessment:
        """Return the rule-based regime assessment."""
        price_series = _data_price_series(visible_events)
        if len(price_series) < 2:
            return AdaptiveRegimeAssessment.uncertain(
                model_version=self.model_version,
                uncertainty_codes=("INSUFFICIENT_HISTORY",),
                evidence_keys=(market.version, ADAPTIVE_MARKET_SNAPSHOT_VERSION),
            )
        history_seconds = price_series[-1][0] - price_series[0][0]
        if history_seconds < params.min_history_seconds:
            return AdaptiveRegimeAssessment.uncertain(
                model_version=self.model_version,
                uncertainty_codes=("INSUFFICIENT_HISTORY",),
                evidence_keys=(market.version, ADAPTIVE_MARKET_SNAPSHOT_VERSION),
            )

        returns = _compute_returns_q64_64(price_series)
        if not returns:
            return AdaptiveRegimeAssessment.uncertain(
                model_version=self.model_version,
                uncertainty_codes=("NO_RETURNS",),
                evidence_keys=(market.version,),
            )

        last_ts, last_ret = returns[-1]
        # Single-bar jump detection with downside asymmetry.
        down_mag = _magnitude_q64_64(last_ret) if last_ret < 0 else 0
        up_mag = last_ret if last_ret > 0 else 0
        if down_mag >= params.down_jump_threshold_q64_64 and (
            up_mag < params.up_jump_threshold_q64_64 or last_ret < 0
        ):
            return AdaptiveRegimeAssessment(
                version=ADAPTIVE_REGIME_ASSESSMENT_VERSION,
                model_version=self.model_version,
                outcome="ASSESSED",
                state="JUMP_RISK",
                confidence_q64_64=Q64_SCALE // 2,
                range_occupancy_q64_64=0,
                jumps_down=True,
                liquidity_withdrawal=False,
                evidence_keys=(market.version,),
                notes=(f"jump_ts={last_ts}",),
            )
        if up_mag >= params.up_jump_threshold_q64_64:
            return AdaptiveRegimeAssessment(
                version=ADAPTIVE_REGIME_ASSESSMENT_VERSION,
                model_version=self.model_version,
                outcome="ASSESSED",
                state="JUMP_RISK",
                confidence_q64_64=Q64_SCALE // 2,
                range_occupancy_q64_64=0,
                jumps_down=False,
                liquidity_withdrawal=False,
                evidence_keys=(market.version,),
                notes=(f"jump_ts={last_ts}",),
            )

        # Trend detection over the full window. The threshold
        # applies to the *cumulative* return: a sustained move of
        # the configured magnitude in either direction classifies
        # the regime as a trend.
        cumulative_q64_64 = sum(r for _ts, r in returns)
        if cumulative_q64_64 < 0 and _magnitude_q64_64(cumulative_q64_64) >= (
            params.down_trend_threshold_q64_64
        ):
            return AdaptiveRegimeAssessment(
                version=ADAPTIVE_REGIME_ASSESSMENT_VERSION,
                model_version=self.model_version,
                outcome="ASSESSED",
                state="DOWN_TREND",
                confidence_q64_64=Q64_SCALE // 2,
                range_occupancy_q64_64=0,
                jumps_down=False,
                liquidity_withdrawal=False,
                evidence_keys=(market.version,),
            )
        if cumulative_q64_64 >= params.up_trend_threshold_q64_64:
            return AdaptiveRegimeAssessment(
                version=ADAPTIVE_REGIME_ASSESSMENT_VERSION,
                model_version=self.model_version,
                outcome="ASSESSED",
                state="UP_TREND",
                confidence_q64_64=Q64_SCALE // 2,
                range_occupancy_q64_64=0,
                jumps_down=False,
                liquidity_withdrawal=False,
                evidence_keys=(market.version,),
            )

        # Range detection: occupancy above the threshold.
        current_price = price_series[-1][1]
        occupancy = _range_occupancy_q64_64(
            price_series,
            decision_time=market.availability_time,
            tick_lower=portfolio.tick_lower,
            tick_upper=portfolio.tick_upper,
            pool_price_q64_64=current_price,
        )
        if occupancy < params.range_occupancy_threshold_q64_64:
            return AdaptiveRegimeAssessment.uncertain(
                model_version=self.model_version,
                uncertainty_codes=("RANGE_OCCUPANCY_LOW",),
                evidence_keys=(market.version,),
            )

        # Liquidity-withdrawal signal: a sustained drop in active
        # liquidity across the window.
        liquidity_series = _data_liquidity_series(visible_events)
        liquidity_withdrawal = False
        if len(liquidity_series) >= 4:
            half = len(liquidity_series) // 2
            early_avg = sum(v for _ts, v in liquidity_series[:half]) // max(half, 1)
            late_avg = sum(v for _ts, v in liquidity_series[half:]) // max(
                len(liquidity_series) - half, 1
            )
            if early_avg > 0:
                drop_q64_64 = ((early_avg - late_avg) << 64) // early_avg
                if drop_q64_64 > Q64_SCALE // 4:  # > 25 % drop
                    liquidity_withdrawal = True

        return AdaptiveRegimeAssessment(
            version=ADAPTIVE_REGIME_ASSESSMENT_VERSION,
            model_version=self.model_version,
            outcome="ASSESSED",
            state="RANGE",
            confidence_q64_64=Q64_SCALE // 2,
            range_occupancy_q64_64=occupancy,
            jumps_down=False,
            liquidity_withdrawal=liquidity_withdrawal,
            evidence_keys=(market.version,),
        )


@dataclass(frozen=True, slots=True)
class RuleBasedAdaptiveFeeOpportunityModel:
    """The reference rule-based fee-opportunity model.

    The model inspects the visible volume / trade-count evidence to
    produce an expected fee edge. The model is *replaceable*; a
    model-backed implementation must satisfy the same contract as
    this rule implementation.

    The model downgrades to ``UNCERTAIN`` when:

    - fewer than :attr:`AdaptiveRangeParameters.fee_min_samples`
      observations are visible (low-volume / wash-like);
    - the projected own-liquidity share exceeds
      :attr:`AdaptiveRangeParameters.max_own_liquidity_share_q64_64`
      (low liquidity is never equated with opportunity);
    - the visible volume sequence carries too few trades to compute
      a meaningful fee edge.
    """

    model_version: str = ADAPTIVE_STRATEGY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.model_version, str) or not self.model_version:
            raise InvalidAdaptiveParameterError(
                "RuleBasedAdaptiveFeeOpportunityModel.model_version: must be "
                f"non-empty str, got {self.model_version!r}"
            )

    @property
    def is_replaceable(self) -> bool:
        """Components are replaceable; the property is a marker for tests."""
        return True

    def assess(
        self,
        *,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        visible_events: Sequence[BacktestEvent],
        params: AdaptiveRangeParameters,
    ) -> AdaptiveFeeOpportunityAssessment:
        """Return the rule-based fee-opportunity assessment."""
        volume_series = _data_volume_series(visible_events)
        sample_count = len(volume_series)
        if sample_count < params.fee_min_samples:
            return AdaptiveFeeOpportunityAssessment.uncertain(
                model_version=self.model_version,
                uncertainty_codes=("LOW_VOLUME_WASH", "INSUFFICIENT_SAMPLES"),
                evidence_keys=(market.version,),
            )

        # Detect wash-like activity: every observation carries the
        # exact same volume.
        distinct_volumes = {v for _ts, v in volume_series}
        wash_like = len(distinct_volumes) <= 1
        if wash_like:
            return AdaptiveFeeOpportunityAssessment.uncertain(
                model_version=self.model_version,
                uncertainty_codes=("LOW_VOLUME_WASH",),
                evidence_keys=(market.version,),
            )

        # Total volume over the window (Q64.64).
        total_volume = sum(v for _ts, v in volume_series)

        # Projected own-liquidity share: own_liquidity /
        # (own_liquidity + pool_liquidity). The pool liquidity is
        # taken from the snapshot; the own liquidity is taken from
        # the portfolio.
        own_liq = portfolio.liquidity
        pool_liq = market.liquidity
        denom = own_liq + pool_liq
        if denom <= 0:
            return AdaptiveFeeOpportunityAssessment.uncertain(
                model_version=self.model_version,
                uncertainty_codes=("ZERO_LIQUIDITY",),
                evidence_keys=(market.version,),
            )
        own_share_q64_64 = (own_liq << 64) // denom
        if own_share_q64_64 > params.max_own_liquidity_share_q64_64:
            return AdaptiveFeeOpportunityAssessment.uncertain(
                model_version=self.model_version,
                uncertainty_codes=("OWN_SHARE_TOO_LARGE",),
                evidence_keys=(market.version,),
            )

        # Expected fee edge: total_volume * share * fee_pips /
        # 1_000_000 (FEE_DENOMINATOR_PIPS). When no fee tier is
        # available in the market snapshot we degrade gracefully by
        # assuming a 30 bp fee tier.
        fee_pips_value = 30_000  # 3 % sentinel; replaced by the engine
        # in production via the FeeModel on every fill. The T065
        # rule uses this only for cost-vs-edge arithmetic.
        expected_fee_edge_q64_64 = (total_volume * fee_pips_value) // 1_000_000
        if expected_fee_edge_q64_64 <= 0:
            return AdaptiveFeeOpportunityAssessment.uncertain(
                model_version=self.model_version,
                uncertainty_codes=("ZERO_FEE_EDGE",),
                evidence_keys=(market.version,),
            )

        # Cost ratio: a 21_000-gas * 100 wei gas price sentinel
        # compared against the expected edge. The strategy uses
        # this ratio to decide REBUILD vs REBUILD_DEFER.
        gas_units_sentinel = 21_000
        gas_price_sentinel = 100
        expected_cost = gas_units_sentinel * gas_price_sentinel
        expected_cost_ratio_q64_64 = (
            ((expected_cost << 64) // expected_fee_edge_q64_64)
            if expected_fee_edge_q64_64 > 0
            else 0
        )

        return AdaptiveFeeOpportunityAssessment(
            version=ADAPTIVE_FEE_OPPORTUNITY_ASSESSMENT_VERSION,
            model_version=self.model_version,
            outcome="ASSESSED",
            expected_fee_edge_q64_64=expected_fee_edge_q64_64,
            expected_in_range_fraction_q64_64=Q64_SCALE // 2,
            projected_own_liquidity_share_q64_64=own_share_q64_64,
            expected_cost_ratio_q64_64=expected_cost_ratio_q64_64,
            sample_count=sample_count,
            confidence_q64_64=Q64_SCALE // 2,
            wash_like=False,
            evidence_keys=(market.version,),
        )


# ---------------------------------------------------------------------------
# Candidate action
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdaptiveCandidateAction:
    """The strategy-layer proposal.

    The action carries the immutable parameter / input / component
    versions, the regime / fee outcomes, the structured reason
    code for ``NO_TRADE`` candidates, the per-decision
    ``position_reassessment`` flag, and the structured
    ``cost_ratio_q64_64`` for ``REBUILD_DEFER``.

    The action is *not* an engine transaction. The adapter
    (:mod:`robinhood_lp.strategy.adapter`) converts the action to
    the engine's :class:`StrategyDecision`.
    """

    version: str
    pool_key_id: str
    chain_id: int
    kind: AdaptiveCandidateKind | str
    decision_time: int
    parameter_version: str
    snapshot_versions: tuple[str, ...]
    component_versions: tuple[str, ...]
    regime_outcome: str
    regime_state: str
    fee_opportunity_outcome: str
    reason_code: AdaptiveReasonCode | None
    position_reassessment: bool
    cost_ratio_q64_64: int
    tick_lower: int = SENTINEL_TICK
    tick_upper: int = SENTINEL_TICK
    liquidity: int = SENTINEL_LIQUIDITY
    capital_q64_64: int = SENTINEL_CAPITAL
    uncertainty_codes: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # ``kind`` accepts both the enum and the underlying string.
        if isinstance(self.kind, AdaptiveCandidateKind):
            kind_str = self.kind.value
        elif isinstance(self.kind, str):
            kind_str = self.kind
            valid = {k.value for k in AdaptiveCandidateKind}
            if kind_str not in valid:
                raise InvalidAdaptiveCandidateError(
                    f"AdaptiveCandidateAction.kind: must be one of "
                    f"{sorted(valid)}, got {kind_str!r}"
                )
        else:
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.kind: must be "
                f"AdaptiveCandidateKind or str, got {type(self.kind).__name__}"
            )
        if not isinstance(self.version, str) or not self.version:
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.version: must be non-empty str, got {self.version!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.pool_key_id: must be non-empty "
                f"str, got {self.pool_key_id!r}"
            )
        _require_positive_int(self.chain_id, field="AdaptiveCandidateAction.chain_id")
        _require_non_negative_int(self.decision_time, field="AdaptiveCandidateAction.decision_time")
        if not isinstance(self.parameter_version, str) or not self.parameter_version:
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.parameter_version: must be "
                f"non-empty str, got {self.parameter_version!r}"
            )
        if not isinstance(self.snapshot_versions, tuple):
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.snapshot_versions: must be "
                f"tuple[str, ...], got {type(self.snapshot_versions).__name__}"
            )
        if not isinstance(self.component_versions, tuple):
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.component_versions: must be "
                f"tuple[str, ...], got {type(self.component_versions).__name__}"
            )
        if self.regime_outcome not in ("ASSESSED", "UNCERTAIN"):
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.regime_outcome: must be one of "
                f"ASSESSED/UNCERTAIN, got {self.regime_outcome!r}"
            )
        if self.fee_opportunity_outcome not in ("ASSESSED", "UNCERTAIN"):
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.fee_opportunity_outcome: must be "
                f"one of ASSESSED/UNCERTAIN, got "
                f"{self.fee_opportunity_outcome!r}"
            )
        if self.reason_code is not None and not isinstance(self.reason_code, AdaptiveReasonCode):
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.reason_code: must be "
                f"AdaptiveReasonCode or None, got "
                f"{type(self.reason_code).__name__}"
            )
        if not isinstance(self.position_reassessment, bool):
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.position_reassessment: must be "
                f"bool, got {type(self.position_reassessment).__name__}"
            )
        _require_q64_64(
            self.cost_ratio_q64_64,
            field="AdaptiveCandidateAction.cost_ratio_q64_64",
        )
        if not isinstance(self.uncertainty_codes, tuple):
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.uncertainty_codes: must be tuple, "
                f"got {type(self.uncertainty_codes).__name__}"
            )
        if not isinstance(self.notes, tuple):
            raise InvalidAdaptiveCandidateError(
                f"AdaptiveCandidateAction.notes: must be tuple, got {type(self.notes).__name__}"
            )

        # Per-kind invariants.
        if kind_str == AdaptiveCandidateKind.PROPOSE.value:
            if self.tick_lower >= self.tick_upper:
                raise InvalidAdaptiveCandidateError(
                    f"AdaptiveCandidateAction: PROPOSE requires tick_lower "
                    f"< tick_upper, got "
                    f"{self.tick_lower} >= {self.tick_upper}"
                )
            if self.liquidity <= 0:
                raise InvalidAdaptiveCandidateError(
                    "AdaptiveCandidateAction: PROPOSE requires liquidity > 0"
                )
            if self.capital_q64_64 <= 0:
                raise InvalidAdaptiveCandidateError(
                    "AdaptiveCandidateAction: PROPOSE requires capital_q64_64 > 0"
                )
            if self.reason_code is not None:
                raise InvalidAdaptiveCandidateError(
                    "AdaptiveCandidateAction: PROPOSE must not carry a reason_code"
                )
        elif kind_str == AdaptiveCandidateKind.NO_TRADE.value:
            if self.reason_code is None:
                raise InvalidAdaptiveCandidateError(
                    "AdaptiveCandidateAction: NO_TRADE requires a reason_code"
                )
        elif kind_str == AdaptiveCandidateKind.REBUILD.value:
            if self.tick_lower >= self.tick_upper:
                raise InvalidAdaptiveCandidateError(
                    "AdaptiveCandidateAction: REBUILD requires tick_lower "
                    f"< tick_upper, got {self.tick_lower} >= {self.tick_upper}"
                )
            if self.liquidity <= 0 or self.capital_q64_64 <= 0:
                raise InvalidAdaptiveCandidateError(
                    "AdaptiveCandidateAction: REBUILD requires liquidity > 0 and capital_q64_64 > 0"
                )
        elif kind_str == AdaptiveCandidateKind.EXIT.value:
            if self.liquidity != SENTINEL_LIQUIDITY:
                raise InvalidAdaptiveCandidateError(
                    "AdaptiveCandidateAction: EXIT must carry sentinel "
                    f"liquidity=0, got {self.liquidity}"
                )
            if self.tick_lower != SENTINEL_TICK or self.tick_upper != SENTINEL_TICK:
                raise InvalidAdaptiveCandidateError(
                    "AdaptiveCandidateAction: EXIT must carry sentinel ticks"
                )
        else:
            # WAIT / WAIT_OUT / RETURN / REBUILD_DEFER
            if self.tick_lower != SENTINEL_TICK or self.tick_upper != SENTINEL_TICK:
                raise InvalidAdaptiveCandidateError(
                    f"AdaptiveCandidateAction: {kind_str} requires sentinel "
                    f"ticks, got tick_lower={self.tick_lower} "
                    f"tick_upper={self.tick_upper}"
                )
            if self.liquidity != SENTINEL_LIQUIDITY:
                raise InvalidAdaptiveCandidateError(
                    f"AdaptiveCandidateAction: {kind_str} requires sentinel "
                    f"liquidity=0, got {self.liquidity}"
                )
            if self.capital_q64_64 != SENTINEL_CAPITAL:
                raise InvalidAdaptiveCandidateError(
                    f"AdaptiveCandidateAction: {kind_str} requires sentinel "
                    f"capital_q64_64=0, got {self.capital_q64_64}"
                )


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


def _versioned_parameter_id(params: AdaptiveRangeParameters) -> str:
    """Return a deterministic parameter-version string for the audit chain.

    The string is the SHA-256 hex of the canonical parameter
    serialisation; a bump in any field changes the version. Two
    identical parameter sets produce identical version strings.
    """
    import hashlib

    content = "|".join(
        [
            str(params.five_minute_window_seconds),
            str(params.five_minute_return_threshold_q64_64),
            str(params.min_history_seconds),
            str(params.down_trend_threshold_q64_64),
            str(params.up_trend_threshold_q64_64),
            str(params.down_jump_threshold_q64_64),
            str(params.up_jump_threshold_q64_64),
            str(params.range_occupancy_threshold_q64_64),
            str(params.fee_min_samples),
            str(params.max_own_liquidity_share_q64_64),
            str(params.rebuild_cost_ratio_threshold_q64_64),
            str(params.half_width_ticks),
            str(params.tick_spacing),
            str(params.liquidity),
            str(params.capital_q64_64),
            str(params.max_capital_q64_64),
            str(params.max_position_hold_seconds),
            str(params.max_rebuild_wait_seconds),
        ]
    )
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _is_five_minute_rule_triggered(
    *,
    market: AdaptiveMarketSnapshot,
    params: AdaptiveRangeParameters,
) -> bool:
    """Return ``True`` iff the 5-minute USDG rule fires on the snapshot.

    The rule fires only when the most recent complete 5-minute bar's
    USDG return strictly exceeds the configured threshold. An
    incomplete bar never triggers the rule; this is the binding
    requirement the task contract records.
    """
    if not market.five_minute_return_complete:
        return False
    return market.five_minute_return_q64_64 > params.five_minute_return_threshold_q64_64


def _is_in_range(portfolio: AdaptivePortfolioSnapshot, current_tick: int) -> bool:
    """Return ``True`` iff the current tick is inside the position Range."""
    if portfolio.is_empty:
        return False
    return portfolio.tick_lower <= current_tick < portfolio.tick_upper


@dataclass(frozen=True, slots=True)
class AdaptiveStrategy:
    """The USDG-first adaptive-Range rule strategy.

    The strategy combines the rule-based regime / fee-opportunity
    components with the lifecycle / 5-minute USDG / rebuild-cost
    logic to produce an :class:`AdaptiveCandidateAction`. The
    ``regime_model`` and ``fee_opportunity_model`` are replaceable;
    replacing either component does not alter the ledger, risk or
    audit semantics — only the assessment the strategy reads.
    """

    params: AdaptiveRangeParameters
    regime_model: AdaptiveRegimeModel = field(default_factory=RuleBasedAdaptiveRegimeModel)
    fee_opportunity_model: AdaptiveFeeOpportunityModel = field(
        default_factory=RuleBasedAdaptiveFeeOpportunityModel
    )
    pool_key_id: str = ""
    chain_id: int = 0
    strategy_version: str = ADAPTIVE_STRATEGY_VERSION
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.params, AdaptiveRangeParameters):
            raise AdaptiveStrategyError(
                f"AdaptiveStrategy.params: must be "
                f"AdaptiveRangeParameters, got {type(self.params).__name__}"
            )
        if not isinstance(self.regime_model, AdaptiveRegimeModel):
            raise AdaptiveStrategyError(
                f"AdaptiveStrategy.regime_model: must implement "
                f"AdaptiveRegimeModel, got {type(self.regime_model).__name__}"
            )
        if not isinstance(self.fee_opportunity_model, AdaptiveFeeOpportunityModel):
            raise AdaptiveStrategyError(
                f"AdaptiveStrategy.fee_opportunity_model: must implement "
                f"AdaptiveFeeOpportunityModel, got "
                f"{type(self.fee_opportunity_model).__name__}"
            )
        if not isinstance(self.pool_key_id, str):
            raise AdaptiveStrategyError(
                f"AdaptiveStrategy.pool_key_id: must be str, got {type(self.pool_key_id).__name__}"
            )
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise AdaptiveStrategyError(
                f"AdaptiveStrategy.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id < 0:
            raise AdaptiveStrategyError(
                f"AdaptiveStrategy.chain_id: must be non-negative, got {self.chain_id}"
            )
        if not isinstance(self.strategy_version, str) or not self.strategy_version:
            raise AdaptiveStrategyError(
                f"AdaptiveStrategy.strategy_version: must be non-empty str, "
                f"got {self.strategy_version!r}"
            )

    @property
    def parameter_version(self) -> str:
        """Return the deterministic parameter-version string."""
        return _versioned_parameter_id(self.params)

    def evaluate(
        self,
        *,
        admission: AdaptiveAdmissionSnapshot,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        visible_events: Sequence[BacktestEvent],
        decision_time: int,
    ) -> AdaptiveCandidateAction:
        """Run the decision pipeline and return the candidate action."""
        _require_non_negative_int(decision_time, field="decision_time")
        if admission.pool_key_id != market.pool_key_id:
            raise AdaptiveStrategyError(
                f"AdaptiveStrategy.evaluate: admission.pool_key_id="
                f"{admission.pool_key_id!r} != market.pool_key_id="
                f"{market.pool_key_id!r}"
            )
        if market.pool_key_id != portfolio.pool_key_id:
            raise AdaptiveStrategyError(
                f"AdaptiveStrategy.evaluate: market.pool_key_id="
                f"{market.pool_key_id!r} != portfolio.pool_key_id="
                f"{portfolio.pool_key_id!r}"
            )
        if not admission.is_admitted:
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.STRATEGY_HOLD,
                regime_outcome="UNCERTAIN",
                fee_outcome="UNCERTAIN",
                uncertainty_codes=("NOT_ADMITTED",),
            )

        # 5-minute USDG rule. The rule is read against the
        # snapshot's pre-computed 5-minute return. The strategy
        # does NOT mutate the snapshot.
        rule_triggered = _is_five_minute_rule_triggered(market=market, params=self.params)

        # Run the components.
        regime = self.regime_model.assess(
            market=market,
            portfolio=portfolio,
            visible_events=visible_events,
            params=self.params,
        )
        fee = self.fee_opportunity_model.assess(
            market=market,
            portfolio=portfolio,
            visible_events=visible_events,
            params=self.params,
        )

        snapshot_versions = (
            admission.version,
            market.version,
            portfolio.version,
        )
        component_versions = (
            f"{regime.version}:{regime.model_version}",
            f"{fee.version}:{fee.model_version}",
        )

        # Current tick from the latest price observation.
        current_tick = 0
        price_series = _data_price_series(visible_events)
        if price_series:
            try:
                current_tick = _tick_from_price_q64_64(price_series[-1][1])
            except Exception:  # noqa: BLE001 — fall back to 0
                current_tick = 0

        # 1. Regime uncertain → NO_TRADE.
        if regime.outcome == "UNCERTAIN":
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.REGIME_UNCERTAIN,
                regime_outcome=regime.outcome,
                fee_outcome=fee.outcome,
                uncertainty_codes=regime.uncertainty_codes,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )

        # 2. JUMP_RISK with downside asymmetry: JUMP_DOWN forces a
        # WAIT_OUT for held positions and NO_TRADE for empty ledgers.
        if regime.state == "JUMP_RISK" and regime.jumps_down:
            if portfolio.is_empty:
                return self._no_trade(
                    admission=admission,
                    market=market,
                    portfolio=portfolio,
                    decision_time=decision_time,
                    reason=AdaptiveReasonCode.JUMP_DOWN_RISK,
                    regime_outcome=regime.outcome,
                    regime_state=regime.state,
                    fee_outcome=fee.outcome,
                    uncertainty_codes=regime.uncertainty_codes,
                    component_versions=component_versions,
                    snapshot_versions=snapshot_versions,
                )
            return self._wait_out(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                regime_state=regime.state,
                regime_outcome=regime.outcome,
                fee_outcome=fee.outcome,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
                notes=("JUMP_DOWN_RISK",),
                position_reassessment=True,
            )

        # 3. 5-minute USDG rule fires against an existing position
        # → EXIT (with in-strategy position reassessment recorded).
        if rule_triggered and not portfolio.is_empty:
            return self._exit(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                regime_state=regime.state,
                regime_outcome=regime.outcome,
                fee_outcome=fee.outcome,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
                reason=AdaptiveReasonCode.EXTREME_UP_MOVE,
                position_reassessment=True,
            )

        # 4. 5-minute USDG rule fires against an empty ledger →
        # NO_TRADE (suppresses risk-increasing candidates).
        if rule_triggered and portfolio.is_empty:
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.EXTREME_UP_MOVE,
                regime_outcome=regime.outcome,
                fee_outcome=fee.outcome,
                uncertainty_codes=("EXTREME_UP_MOVE",),
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
                position_reassessment=True,
            )

        # 5. Range-occupancy / liquidity-withdrawal refusals.
        if regime.state == "RANGE" and regime.liquidity_withdrawal:
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.LIQUIDITY_WITHDRAWAL,
                regime_outcome=regime.outcome,
                fee_outcome=fee.outcome,
                uncertainty_codes=regime.uncertainty_codes,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )

        # 6. DOWN_TREND: WAIT for empty ledger; for held positions,
        # reassess (WAIT_OUT) without re-entering.
        if regime.state == "DOWN_TREND":
            if portfolio.is_empty:
                return self._no_trade(
                    admission=admission,
                    market=market,
                    portfolio=portfolio,
                    decision_time=decision_time,
                    reason=AdaptiveReasonCode.DOWN_TREND_ACTIVE,
                    regime_outcome=regime.outcome,
                    regime_state=regime.state,
                    fee_outcome=fee.outcome,
                    uncertainty_codes=regime.uncertainty_codes,
                    component_versions=component_versions,
                    snapshot_versions=snapshot_versions,
                )
            return self._wait_out(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                regime_state=regime.state,
                regime_outcome=regime.outcome,
                fee_outcome=fee.outcome,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
                notes=("DOWN_TREND_ACTIVE",),
                position_reassessment=True,
            )

        # 7. UP_TREND: avoid "CTA is bullish ⇒ add LP"; refuse
        # risk-increasing candidates for empty ledgers. For held
        # positions: if the current tick is inside the Range, hold
        # (WAIT); if the current tick has left the Range, follow
        # the out-of-Range lifecycle — WAIT_OUT within the
        # rebuild-wait bound, REBUILD / REBUILD_DEFER beyond it.
        if regime.state == "UP_TREND":
            if portfolio.is_empty:
                return self._no_trade(
                    admission=admission,
                    market=market,
                    portfolio=portfolio,
                    decision_time=decision_time,
                    reason=AdaptiveReasonCode.UP_TREND_ACTIVE,
                    regime_outcome=regime.outcome,
                    regime_state=regime.state,
                    fee_outcome=fee.outcome,
                    uncertainty_codes=regime.uncertainty_codes,
                    component_versions=component_versions,
                    snapshot_versions=snapshot_versions,
                )
            # Held position: check out-of-Range state.
            if not _is_in_range(portfolio, current_tick):
                out_of_range_seconds = max(0, decision_time - portfolio.last_accrual_time)
                if out_of_range_seconds <= self.params.max_rebuild_wait_seconds:
                    return self._wait_out(
                        admission=admission,
                        market=market,
                        portfolio=portfolio,
                        decision_time=decision_time,
                        regime_state=regime.state,
                        regime_outcome=regime.outcome,
                        fee_outcome=fee.outcome,
                        component_versions=component_versions,
                        snapshot_versions=snapshot_versions,
                        notes=("OUT_OF_RANGE",),
                        position_reassessment=True,
                    )
                # Beyond the rebuild-wait bound: cost analysis decides.
                if (
                    fee.outcome == "ASSESSED"
                    and fee.expected_cost_ratio_q64_64
                    > self.params.rebuild_cost_ratio_threshold_q64_64
                ):
                    return self._rebuild_defer(
                        admission=admission,
                        market=market,
                        portfolio=portfolio,
                        decision_time=decision_time,
                        regime_state=regime.state,
                        regime_outcome=regime.outcome,
                        fee_outcome=fee.outcome,
                        fee=fee,
                        component_versions=component_versions,
                        snapshot_versions=snapshot_versions,
                    )
                return self._rebuild(
                    admission=admission,
                    market=market,
                    portfolio=portfolio,
                    decision_time=decision_time,
                    regime_state=regime.state,
                    regime_outcome=regime.outcome,
                    fee_outcome=fee.outcome,
                    component_versions=component_versions,
                    snapshot_versions=snapshot_versions,
                    tick_lower=_snap_to_spacing(
                        current_tick - self.params.half_width_ticks,
                        self.params.tick_spacing,
                    ),
                    tick_upper=_snap_to_spacing(
                        current_tick + self.params.half_width_ticks,
                        self.params.tick_spacing,
                    ),
                )
            return self._wait(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                regime_state=regime.state,
                regime_outcome=regime.outcome,
                fee_outcome=fee.outcome,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )

        # 8. RANGE regime.
        if regime.state == "RANGE":
            return self._range_path(
                admission=admission,
                market=market,
                portfolio=portfolio,
                visible_events=visible_events,
                decision_time=decision_time,
                regime_state=regime.state,
                regime_outcome=regime.outcome,
                fee_outcome=fee.outcome,
                fee=fee,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
                current_tick=current_tick,
            )

        # Unknown ASSESSED state — treat as UNCERTAIN for safety.
        return self._no_trade(
            admission=admission,
            market=market,
            portfolio=portfolio,
            decision_time=decision_time,
            reason=AdaptiveReasonCode.REGIME_UNCERTAIN,
            regime_outcome=regime.outcome,
            fee_outcome=fee.outcome,
            uncertainty_codes=("UNKNOWN_STATE",),
            component_versions=component_versions,
            snapshot_versions=snapshot_versions,
        )

    # ------------------------------------------------------------------ helpers

    def _range_path(
        self,
        *,
        admission: AdaptiveAdmissionSnapshot,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        visible_events: Sequence[BacktestEvent],
        decision_time: int,
        regime_state: str,
        regime_outcome: str,
        fee_outcome: str,
        fee: AdaptiveFeeOpportunityAssessment,
        component_versions: tuple[str, ...],
        snapshot_versions: tuple[str, ...],
        current_tick: int,
    ) -> AdaptiveCandidateAction:
        """Handle the RANGE-regime branch with the complete lifecycle."""
        # Empty ledger → evaluate the open path.
        if portfolio.is_empty:
            return self._open_path(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                regime_state=regime_state,
                regime_outcome=regime_outcome,
                fee_outcome=fee_outcome,
                fee=fee,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
                current_tick=current_tick,
            )

        in_range = _is_in_range(portfolio, current_tick)
        if in_range:
            # The portfolio snapshot carries the ledger's recorded
            # ``in_range`` flag from the engine. ``portfolio.in_range=False``
            # with the current tick inside the Range is the RETURN
            # transition — the price has re-entered after an
            # out-of-Range episode. ``portfolio.in_range=True`` is the
            # steady-state "already in Range" case (a plain WAIT).
            if not portfolio.in_range:
                return self._return(
                    admission=admission,
                    market=market,
                    portfolio=portfolio,
                    decision_time=decision_time,
                    regime_state=regime_state,
                    regime_outcome=regime_outcome,
                    fee_outcome=fee_outcome,
                    component_versions=component_versions,
                    snapshot_versions=snapshot_versions,
                )
            return self._wait(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                regime_state=regime_state,
                regime_outcome=regime_outcome,
                fee_outcome=fee_outcome,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
                notes=("IN_RANGE",),
            )

        # Out-of-Range path. Determine whether the price is returning,
        # waiting, or whether the rebuild cost analysis defers.
        out_of_range_seconds = max(0, decision_time - portfolio.last_accrual_time)
        if out_of_range_seconds <= self.params.max_rebuild_wait_seconds:
            return self._wait_out(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                regime_state=regime_state,
                regime_outcome=regime_outcome,
                fee_outcome=fee_outcome,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
                notes=("OUT_OF_RANGE",),
                position_reassessment=True,
            )

        # Beyond the rebuild-wait bound: cost analysis decides.
        if (
            fee.outcome == "ASSESSED"
            and fee.expected_cost_ratio_q64_64 > self.params.rebuild_cost_ratio_threshold_q64_64
        ):
            return self._rebuild_defer(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                regime_state=regime_state,
                regime_outcome=regime_outcome,
                fee_outcome=fee_outcome,
                fee=fee,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )

        # Rebuild path. The half-width is snapped to tick_spacing.
        spacing = self.params.tick_spacing
        half_width = self.params.half_width_ticks
        if half_width <= 0 or spacing <= 0:
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.INVALID_TICK,
                regime_outcome=regime_outcome,
                fee_outcome=fee_outcome,
                uncertainty_codes=("INVALID_TICK",),
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )
        lower = _snap_to_spacing(current_tick - half_width, spacing)
        upper = _snap_to_spacing(current_tick + half_width, spacing)
        if lower >= upper:
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.INVALID_TICK,
                regime_outcome=regime_outcome,
                fee_outcome=fee_outcome,
                uncertainty_codes=("INVALID_TICK",),
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )

        # Fee opportunity gate.
        if fee.outcome == "UNCERTAIN":
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.FEE_OPPORTUNITY_UNCERTAIN,
                regime_outcome=regime_outcome,
                fee_outcome=fee.outcome,
                uncertainty_codes=fee.uncertainty_codes,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )
        if fee.expected_fee_edge_q64_64 <= 0:
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.LOW_VOLUME_WASH,
                regime_outcome=regime_outcome,
                fee_outcome=fee.outcome,
                uncertainty_codes=fee.uncertainty_codes,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )

        return self._rebuild(
            admission=admission,
            market=market,
            portfolio=portfolio,
            decision_time=decision_time,
            regime_state=regime_state,
            regime_outcome=regime_outcome,
            fee_outcome=fee.outcome,
            component_versions=component_versions,
            snapshot_versions=snapshot_versions,
            tick_lower=lower,
            tick_upper=upper,
        )

    def _open_path(
        self,
        *,
        admission: AdaptiveAdmissionSnapshot,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        decision_time: int,
        regime_state: str,
        regime_outcome: str,
        fee_outcome: str,
        fee: AdaptiveFeeOpportunityAssessment,
        component_versions: tuple[str, ...],
        snapshot_versions: tuple[str, ...],
        current_tick: int,
    ) -> AdaptiveCandidateAction:
        """Handle the empty-ledger open path."""
        spacing = self.params.tick_spacing
        half_width = self.params.half_width_ticks
        if half_width <= 0 or spacing <= 0:
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.INVALID_TICK,
                regime_outcome=regime_outcome,
                fee_outcome=fee_outcome,
                uncertainty_codes=("INVALID_TICK",),
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )
        lower = _snap_to_spacing(current_tick - half_width, spacing)
        upper = _snap_to_spacing(current_tick + half_width, spacing)
        if lower >= upper:
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.INVALID_TICK,
                regime_outcome=regime_outcome,
                fee_outcome=fee_outcome,
                uncertainty_codes=("INVALID_TICK",),
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )
        if fee.outcome == "UNCERTAIN":
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.FEE_OPPORTUNITY_UNCERTAIN,
                regime_outcome=regime_outcome,
                fee_outcome=fee.outcome,
                uncertainty_codes=fee.uncertainty_codes,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )
        if fee.expected_fee_edge_q64_64 <= 0:
            return self._no_trade(
                admission=admission,
                market=market,
                portfolio=portfolio,
                decision_time=decision_time,
                reason=AdaptiveReasonCode.LOW_VOLUME_WASH,
                regime_outcome=regime_outcome,
                fee_outcome=fee.outcome,
                uncertainty_codes=fee.uncertainty_codes,
                component_versions=component_versions,
                snapshot_versions=snapshot_versions,
            )
        return self._propose(
            admission=admission,
            market=market,
            portfolio=portfolio,
            decision_time=decision_time,
            regime_state=regime_state,
            regime_outcome=regime_outcome,
            fee_outcome=fee_outcome,
            component_versions=component_versions,
            snapshot_versions=snapshot_versions,
            tick_lower=lower,
            tick_upper=upper,
        )

    def _no_trade(
        self,
        *,
        admission: AdaptiveAdmissionSnapshot,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        decision_time: int,
        reason: AdaptiveReasonCode,
        regime_outcome: str,
        fee_outcome: str,
        regime_state: str = "UNCERTAIN",
        uncertainty_codes: tuple[str, ...] = (),
        component_versions: tuple[str, ...] = (),
        snapshot_versions: tuple[str, ...] = (),
        position_reassessment: bool = False,
    ) -> AdaptiveCandidateAction:
        """Return a structured ``NO_TRADE`` candidate."""
        return AdaptiveCandidateAction(
            version=ADAPTIVE_CANDIDATE_ACTION_VERSION,
            pool_key_id=market.pool_key_id,
            chain_id=market.chain_id,
            kind=AdaptiveCandidateKind.NO_TRADE,
            decision_time=decision_time,
            parameter_version=self.parameter_version,
            snapshot_versions=(
                snapshot_versions
                if snapshot_versions
                else (
                    admission.version,
                    market.version,
                    portfolio.version,
                )
            ),
            component_versions=component_versions,
            regime_outcome=regime_outcome,
            regime_state=regime_state,
            fee_opportunity_outcome=fee_outcome,
            reason_code=reason,
            position_reassessment=position_reassessment,
            cost_ratio_q64_64=0,
            uncertainty_codes=uncertainty_codes,
            notes=self.notes,
        )

    def _wait(
        self,
        *,
        admission: AdaptiveAdmissionSnapshot,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        decision_time: int,
        regime_state: str,
        regime_outcome: str,
        fee_outcome: str,
        component_versions: tuple[str, ...],
        snapshot_versions: tuple[str, ...],
        notes: tuple[str, ...] = (),
    ) -> AdaptiveCandidateAction:
        return AdaptiveCandidateAction(
            version=ADAPTIVE_CANDIDATE_ACTION_VERSION,
            pool_key_id=market.pool_key_id,
            chain_id=market.chain_id,
            kind=AdaptiveCandidateKind.WAIT,
            decision_time=decision_time,
            parameter_version=self.parameter_version,
            snapshot_versions=snapshot_versions,
            component_versions=component_versions,
            regime_outcome=regime_outcome,
            regime_state=regime_state,
            fee_opportunity_outcome=fee_outcome,
            reason_code=None,
            position_reassessment=False,
            cost_ratio_q64_64=0,
            notes=self.notes + notes,
        )

    def _wait_out(
        self,
        *,
        admission: AdaptiveAdmissionSnapshot,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        decision_time: int,
        regime_state: str,
        regime_outcome: str,
        fee_outcome: str,
        component_versions: tuple[str, ...],
        snapshot_versions: tuple[str, ...],
        notes: tuple[str, ...] = (),
        position_reassessment: bool,
    ) -> AdaptiveCandidateAction:
        return AdaptiveCandidateAction(
            version=ADAPTIVE_CANDIDATE_ACTION_VERSION,
            pool_key_id=market.pool_key_id,
            chain_id=market.chain_id,
            kind=AdaptiveCandidateKind.WAIT_OUT,
            decision_time=decision_time,
            parameter_version=self.parameter_version,
            snapshot_versions=snapshot_versions,
            component_versions=component_versions,
            regime_outcome=regime_outcome,
            regime_state=regime_state,
            fee_opportunity_outcome=fee_outcome,
            reason_code=None,
            position_reassessment=position_reassessment,
            cost_ratio_q64_64=0,
            notes=self.notes + notes,
        )

    def _return(
        self,
        *,
        admission: AdaptiveAdmissionSnapshot,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        decision_time: int,
        regime_state: str,
        regime_outcome: str,
        fee_outcome: str,
        component_versions: tuple[str, ...],
        snapshot_versions: tuple[str, ...],
    ) -> AdaptiveCandidateAction:
        return AdaptiveCandidateAction(
            version=ADAPTIVE_CANDIDATE_ACTION_VERSION,
            pool_key_id=market.pool_key_id,
            chain_id=market.chain_id,
            kind=AdaptiveCandidateKind.RETURN,
            decision_time=decision_time,
            parameter_version=self.parameter_version,
            snapshot_versions=snapshot_versions,
            component_versions=component_versions,
            regime_outcome=regime_outcome,
            regime_state=regime_state,
            fee_opportunity_outcome=fee_outcome,
            reason_code=None,
            position_reassessment=False,
            cost_ratio_q64_64=0,
            notes=self.notes + ("RETURN_TO_RANGE",),
        )

    def _rebuild(
        self,
        *,
        admission: AdaptiveAdmissionSnapshot,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        decision_time: int,
        regime_state: str,
        regime_outcome: str,
        fee_outcome: str,
        component_versions: tuple[str, ...],
        snapshot_versions: tuple[str, ...],
        tick_lower: int,
        tick_upper: int,
    ) -> AdaptiveCandidateAction:
        return AdaptiveCandidateAction(
            version=ADAPTIVE_CANDIDATE_ACTION_VERSION,
            pool_key_id=market.pool_key_id,
            chain_id=market.chain_id,
            kind=AdaptiveCandidateKind.REBUILD,
            decision_time=decision_time,
            parameter_version=self.parameter_version,
            snapshot_versions=snapshot_versions,
            component_versions=component_versions,
            regime_outcome=regime_outcome,
            regime_state=regime_state,
            fee_opportunity_outcome=fee_outcome,
            reason_code=None,
            position_reassessment=True,
            cost_ratio_q64_64=0,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=self.params.liquidity,
            capital_q64_64=self.params.capital_q64_64,
            notes=self.notes + ("REBUILD",),
        )

    def _rebuild_defer(
        self,
        *,
        admission: AdaptiveAdmissionSnapshot,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        decision_time: int,
        regime_state: str,
        regime_outcome: str,
        fee_outcome: str,
        fee: AdaptiveFeeOpportunityAssessment,
        component_versions: tuple[str, ...],
        snapshot_versions: tuple[str, ...],
    ) -> AdaptiveCandidateAction:
        return AdaptiveCandidateAction(
            version=ADAPTIVE_CANDIDATE_ACTION_VERSION,
            pool_key_id=market.pool_key_id,
            chain_id=market.chain_id,
            kind=AdaptiveCandidateKind.REBUILD_DEFER,
            decision_time=decision_time,
            parameter_version=self.parameter_version,
            snapshot_versions=snapshot_versions,
            component_versions=component_versions,
            regime_outcome=regime_outcome,
            regime_state=regime_state,
            fee_opportunity_outcome=fee_outcome,
            reason_code=AdaptiveReasonCode.REBUILD_DEFERRED,
            position_reassessment=True,
            cost_ratio_q64_64=fee.expected_cost_ratio_q64_64,
            notes=self.notes + ("REBUILD_DEFER",),
        )

    def _exit(
        self,
        *,
        admission: AdaptiveAdmissionSnapshot,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        decision_time: int,
        regime_state: str,
        regime_outcome: str,
        fee_outcome: str,
        component_versions: tuple[str, ...],
        snapshot_versions: tuple[str, ...],
        reason: AdaptiveReasonCode,
        position_reassessment: bool,
    ) -> AdaptiveCandidateAction:
        return AdaptiveCandidateAction(
            version=ADAPTIVE_CANDIDATE_ACTION_VERSION,
            pool_key_id=market.pool_key_id,
            chain_id=market.chain_id,
            kind=AdaptiveCandidateKind.EXIT,
            decision_time=decision_time,
            parameter_version=self.parameter_version,
            snapshot_versions=snapshot_versions,
            component_versions=component_versions,
            regime_outcome=regime_outcome,
            regime_state=regime_state,
            fee_opportunity_outcome=fee_outcome,
            reason_code=reason,
            position_reassessment=position_reassessment,
            cost_ratio_q64_64=0,
            notes=self.notes + ("EXIT",),
        )

    def _propose(
        self,
        *,
        admission: AdaptiveAdmissionSnapshot,
        market: AdaptiveMarketSnapshot,
        portfolio: AdaptivePortfolioSnapshot,
        decision_time: int,
        regime_state: str,
        regime_outcome: str,
        fee_outcome: str,
        component_versions: tuple[str, ...],
        snapshot_versions: tuple[str, ...],
        tick_lower: int,
        tick_upper: int,
    ) -> AdaptiveCandidateAction:
        return AdaptiveCandidateAction(
            version=ADAPTIVE_CANDIDATE_ACTION_VERSION,
            pool_key_id=market.pool_key_id,
            chain_id=market.chain_id,
            kind=AdaptiveCandidateKind.PROPOSE,
            decision_time=decision_time,
            parameter_version=self.parameter_version,
            snapshot_versions=snapshot_versions,
            component_versions=component_versions,
            regime_outcome=regime_outcome,
            regime_state=regime_state,
            fee_opportunity_outcome=fee_outcome,
            reason_code=None,
            position_reassessment=False,
            cost_ratio_q64_64=0,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            liquidity=self.params.liquidity,
            capital_q64_64=self.params.capital_q64_64,
            notes=self.notes + ("OPEN",),
        )


# ---------------------------------------------------------------------------
# Layer-purity check
# ---------------------------------------------------------------------------


def _is_strategy_module(name: str) -> bool:
    """Return ``True`` iff ``name`` belongs to the adaptive strategy layer."""
    return name == "robinhood_lp.strategy.adaptive" or name.startswith(
        "robinhood_lp.strategy.adaptive."
    )


def assert_adaptive_strategy_layer_is_pure() -> None:
    """Raise :class:`AdaptiveStrategyError` if the adaptive strategy layer
    imports a forbidden module.

    The denylist is the closed set recorded in
    :data:`_FORBIDDEN_ADAPTIVE_ROBINHOOD_MODULES`. The check is
    conservative on purpose: the adaptive strategy is supposed to
    be a *pure* observation -> candidate-action function, so any
    import from a forbidden layer is a contract break.
    """
    import importlib

    module = importlib.import_module("robinhood_lp.strategy.adaptive")
    forbidden: list[tuple[str, str]] = []
    seen: set[str] = set()
    stack: list[ModuleType] = [module]
    while stack:
        current = stack.pop()
        if current.__name__ in seen:
            continue
        seen.add(current.__name__)
        for _attr_name, attr in vars(current).items():
            mod_name = getattr(attr, "__name__", None)
            if not isinstance(mod_name, str):
                continue
            if mod_name in seen:
                continue
            if mod_name.startswith("robinhood_lp."):
                stack.append(attr)
            for forbidden_root in _FORBIDDEN_ADAPTIVE_ROBINHOOD_MODULES:
                if mod_name == forbidden_root or mod_name.startswith(forbidden_root + "."):
                    forbidden.append((current.__name__, mod_name))
                    break
    if forbidden:
        rendered = "\n".join(f"  {src}: imports {imp}" for src, imp in forbidden)
        raise AdaptiveStrategyError(
            f"adaptive strategy layer imports forbidden modules:\n{rendered}"
        )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "ADAPTIVE_ADMISSION_SNAPSHOT_VERSION",
    "ADAPTIVE_CANDIDATE_ACTION_VERSION",
    "ADAPTIVE_FEE_OPPORTUNITY_ASSESSMENT_VERSION",
    "ADAPTIVE_MARKET_SNAPSHOT_VERSION",
    "ADAPTIVE_PORTFOLIO_SNAPSHOT_VERSION",
    "ADAPTIVE_REGIME_ASSESSMENT_VERSION",
    "ADAPTIVE_STRATEGY_VERSION",
    "AdaptiveAdmissionSnapshot",
    "AdaptiveCandidateAction",
    "AdaptiveCandidateKind",
    "AdaptiveFeeOpportunityAssessment",
    "AdaptiveFeeOpportunityModel",
    "AdaptiveMarketSnapshot",
    "AdaptivePortfolioSnapshot",
    "AdaptiveRangeParameters",
    "AdaptiveReasonCode",
    "AdaptiveRegimeAssessment",
    "AdaptiveRegimeModel",
    "AdaptiveStrategy",
    "AdaptiveStrategyError",
    "FIVE_MINUTE_RETURN_THRESHOLD_Q64_64",
    "InvalidAdaptiveAssessmentError",
    "InvalidAdaptiveCandidateError",
    "InvalidAdaptiveParameterError",
    "InvalidAdaptiveSnapshotError",
    "Q64_SCALE",
    "RuleBasedAdaptiveFeeOpportunityModel",
    "RuleBasedAdaptiveRegimeModel",
    "SENTINEL_CAPITAL",
    "SENTINEL_LIQUIDITY",
    "SENTINEL_TICK",
    "assert_adaptive_strategy_layer_is_pure",
]
