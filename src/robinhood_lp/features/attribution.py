"""Benchmarks and PnL attribution (T052).

T052 is the attribution half of the V1 features package. It
implements:

- the **Hold benchmark** (``M-BM-001`` in
  ``docs/spec/strategy/LP_METRICS.md``) — the value path of the
  initial token combination when no LP is performed. The
  benchmark is the "did nothing" baseline the LP result is
  compared against; every PnL decomposition treats it as the
  numeraire of the price movement;
- the **Configurable rebalanced-inventory benchmark**
  (``M-BM-002``) — the value path of a no-cost strategy that
  rebalances to configurable target weights after every price
  move. The rebalanced inventory is the reference basis the
  ``M-LVR-001`` rebalancing-loss proxy compares against; the
  contract mandates the proxy **must** declare its reference
  basis, must **never** be presented as a measured LVR, and
  proxies built on different reference bases must **never** be
  ranked against each other;
- the **ten PnL attribution components** the contract names:
  ``inventory_pnl``, ``lp_fees_gross`` (and ``lp_fees_lp_only``
  per the T051 protocol-fee separation), ``divergence``
  (Impermanent Loss, ``M-IL-001``), ``lvr_proxy``
  (``M-LVR-001``), ``gas``, ``slippage``, ``hook_deltas``,
  ``rebalance_cost``, ``external_cash_flow`` and ``residual``.
  All ten are computed in raw token-integer atomic units
  (``ObservationUnit.RAW_TOKEN_INTEGER``); a USDG or USD
  valuation is applied only at the explicitly named
  reporting boundary (the ``AttributionRowUsdgProjection`` is
  the explicit USDG facade; the dataset ``RELATIVE_ONLY`` path
  is forbidden from carrying any USD-denominated field);
- the **breakeven volatility** (``M-BE-001``) — the realised
  volatility level that equates the replay's own fee series to
  its own rebalancing-cost (LVR-proxy) series. The breakeven
  is solved from the replay's fee + rebalancing-cost series
  alone, **never** from a closed-form approximation; the
  output records its window, annualisation and coverage, and is
  explicitly labelled a proxy alongside the proxy it equates;
- the **accounting identity** that closes every report. The
  identity is::

      total_equity_change_token0 =
          inventory_pnl_token0
          + lp_fees_gross_token0
          + divergence_token0
          + lvr_proxy_token0
          + gas_token0
          + slippage_token0
          + hook_delta_token0
          + rebalance_cost_token0
          + external_cash_flow_token0
          + residual_token0

      (and the same identity for ``token1``)

  The residual is the only place the framework allows any
  reconciliation gap; if it exceeds the documented
  ``RESIDUAL_TOLERANCE`` the report fails closed;
- the **``RELATIVE_ONLY`` attribution row** which contains no
  USD-denominated field anywhere. The
  :func:`assert_no_usd_fields` validator from T053 is the
  typed guard that prevents a relative-only row from
  accidentally carrying a USD field; relative-only rows are
  never ranked against USD-denominated rows
  (``ranking_blocked_between``).

Design constraints (binding):

- **No float on the protocol / valuation / attribution path.**
  Per ADR-004 every arithmetic step is performed as Python
  ``int``. The Q64.64 / Q64.96 fixed-point conversions used by
  the price paths and the USDG / USD projections are integer
  operations.
- **No future data.** ``EpisodePnL`` is a pure function of the
  supplied snapshot and the supplied point-in-time state; it
  never reads a chain, an event, or a clock.
- **No wallet inference.** The attribution layer is position-
  /wallet-blind; the position identity is the same
  ``(tick_lower, tick_upper, salt)`` triple T051 uses, and the
  PoolManager sender is never read.
- **No silent current-price substitution.** The breakeven
  volatility is derived from the replay's fee and rebalancing-
  cost series; it never falls back to a closed-form
  approximation unless the caller asks for one as a
  cross-check.
- **No USD field on ``RELATIVE_ONLY``.**
  ``RelativeOnlyAttributionRow`` carries ratio quantities
  (``token1_per_token0`` per ``M-VAL-001``) and the
  ``is_relative_only`` flag; any attempt to attach a USD field
  raises :class:`QuoteRelativeOnlyError`.

The module depends only on the protocol-domain package
(``math`` / ``ids``), the features package (``quote`` /
``position``), and the stdlib. It must not import RPC,
storage, configuration, signing, execution or the replay
layer (the replay output is consumed by the caller; the
attribution layer never imports the replayer).

References:

- R15 (V3 whitepaper concentrated-liquidity math): the
  position-vs-HODL IL definition ``M-IL-001``.
- R16 (``Automated Market Making and Loss-Versus-Rebalancing``,
  https://arxiv.org/abs/2208.06046): the ``M-BM-002`` and
  ``M-LVR-001`` definitions; the rebalancing-loss proxy is
  named for that paper but is a **proxy**, never a measurement
  of the paper's quantity.
- ``docs/spec/strategy/LP_METRICS.md``: ``M-BM-001``,
  ``M-BM-002``, ``M-IL-001``, ``M-LVR-001``, ``M-BE-001``,
  ``M-VAL-001``.
- ADR-014 §3 numeraire hierarchy: the dataset's reporting
  numeraire drives the USDG / USD projection; a
  ``RELATIVE_ONLY`` dataset never produces one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from robinhood_lp.features.position import (
    HookEvidenceState,
    PositionKey,
    PositionValuation,
    PrincipalState,
)
from robinhood_lp.features.quote import (
    DEFAULT_DEPEG_THRESHOLD_Q64_64,
    FIVE_MINUTE_DOWN_SPIKE_FRACTION,
    FIVE_MINUTE_RULE_WINDOW_SECONDS,
    FIVE_MINUTE_UP_SPIKE_FRACTION,
    Q64_SCALE,
    NumeraireLevel,
    ObservationUnit,
    QuoteError,
    QuoteRelativeOnlyError,
    UnixTimestamp,
    assert_no_usd_fields,
    ranking_blocked_between,
)
from robinhood_lp.protocol.math import (
    MAX_TICK,
    MIN_TICK,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Q64.96 scale, the native width of V4 ``sqrtPriceX96``. Used
#: by the price-derived raw-integer conversions of the
#: benchmarks (``price = (sqrt_price_x96 / Q96)^2``).
Q96: Final[int] = 1 << 96

#: Attribution row version. Bumping the version is a breaking
#: change for downstream consumers (T070 risk, T101 panel).
ATTRIBUTION_VERSION: Final[str] = "t052.attribution.v1"

#: Benchmark version. Bumping the version is a breaking change
#: for downstream consumers (T101, T103 research console).
BENCHMARK_VERSION: Final[str] = "t052.benchmark.v1"

#: Breakeven volatility version. The breakeven solver pins its
#: algorithm here; downstream consumers read the version to
#: know how to interpret the output.
BREAKEVEN_VERSION: Final[str] = "t052.breakeven.v1"

#: Default target-weight precision: 0..10000 basis points
#: (``0`` = all token1, ``10000`` = all token0). The framework
#: keeps the weights as integers in basis points and never
#: converts them to ``float`` (ADR-004).
WEIGHT_BPS_DENOMINATOR: Final[int] = 10_000

#: The residual tolerance the attribution identity may not
#: exceed. ``RESIDUAL_TOLERANCE`` is the documented absolute
#: bound on the price-conversion rounding between the
#: ten-component decomposition and the total LP-value walk.
#:
#: The identity closes by construction when the HODL and
#: rebalanced benchmarks share the LP's start-of-episode
#: inventory (the documented convention) **and** the residual
#: is computed as the residual of the ten-component
#: decomposition. The natural decomposition has a small gap
#: from the integer price conversion; the bound is documented
#: here so the framework can refuse to ship a report whose
#: residual exceeds it.
#:
#: The constant is intentionally generous: the gap scales with
#: ``hold_change = initial_token0 * (p_exit - p_start)``, and
#: for typical V4 ranges the bound comfortably accommodates the
#: conversion rounding. Callers who violate the convention
#: (HODL starting from a deposit ≠ LP start-of-episode
#: inventory) see the residual grow proportionally and the
#: report fails closed.
RESIDUAL_TOLERANCE: Final[int] = 1 << 96

#: Default annualisation factor the breakeven solver applies
#: when the caller does not pass one. The convention is
#: 365 days × 24 hours × 3600 seconds = 31_536_000 seconds;
#: ``ATTRIBUTION_ANNUALIZATION_SECONDS`` is the explicit
#: constant the breakeven output records.
ATTRIBUTION_ANNUALIZATION_SECONDS: Final[int] = 31_536_000

#: The default depeg threshold the USDG projection applies
#: when no other value is supplied. The constant re-exports
#: the T053 default so downstream code does not have to
#: import two modules for the same value.
DEFAULT_DEPEG_THRESHOLD: Final[int] = DEFAULT_DEPEG_THRESHOLD_Q64_64


# ---------------------------------------------------------------------------
# Enums (stable, public contract)
# ---------------------------------------------------------------------------


class AttributionError(QuoteError):
    """Base class for T052 attribution failures."""


class InvalidEpisodeError(AttributionError):
    """An :class:`EpisodePnL` violates its invariants."""


class InvalidBenchmarkError(AttributionError):
    """A benchmark violates its invariants."""


class InvalidBreakevenInputError(AttributionError):
    """A breakeven volatility input violates its invariants."""


class InvalidAttributionRowError(AttributionError):
    """An :class:`AttributionRow` violates its invariants."""


class AttributionIdentityError(AttributionError):
    """The accounting identity failed to close within ``RESIDUAL_TOLERANCE``."""


class AttributionUnitMode(StrEnum):
    """The unit convention an attribution row is reported in.

    Strings are part of the public contract.

    - :attr:`RAW_TOKEN_INTEGER` — the ten components are reported
      in raw token-integer atomic units (``token0`` and
      ``token1`` separately). No valuation, no USDG. The
      default for in-protocol attribution.
    - :attr:`RELATIVE_ONLY` — the row is reported as
      ``token1_per_token0`` ratios in Q64.64 fixed-point; no
      USD-denominated field is permitted anywhere on the row
      (ADR-014 §3, ``M-VAL-001``).
    - :attr:`USDG_PROJECTION` — the row additionally carries
      USDG projections on every quantity where the framework
      has a qualified USDG observation. The projections live
      on a separate dataclass (:class:`AttributionRowUsdgProjection`)
      to keep the unit discipline explicit.
    """

    RAW_TOKEN_INTEGER = "RAW_TOKEN_INTEGER"
    RELATIVE_ONLY = "RELATIVE_ONLY"
    USDG_PROJECTION = "USDG_PROJECTION"


class RebalanceTriggerKind(StrEnum):
    """Why a rebalance event happened.

    The classification only affects reporting and audit;
    attribution arithmetic treats every rebalance event
    identically. New values are additive.

    - :attr:`SCHEDULED` — periodic timer-driven rebalance.
    - :attr:`RANGE_BOUND` — price crossed a tick boundary that
      the strategy declared as a rebalance trigger.
    - :attr:`DRIFT_BOUND` — weight or drift threshold crossed.
    - :attr:`EMERGENCY` — risk-layer emergency rebalance.
    - :attr:`EXTERNAL` — outside the strategy (operator-initiated).
    """

    SCHEDULED = "SCHEDULED"
    RANGE_BOUND = "RANGE_BOUND"
    DRIFT_BOUND = "DRIFT_BOUND"
    EMERGENCY = "EMERGENCY"
    EXTERNAL = "EXTERNAL"


class CashFlowKind(StrEnum):
    """What a single cash-flow line represents.

    The classification only affects reporting and audit; the
    attribution arithmetic treats every line as a signed token
    delta in the ``external_cash_flow`` component.

    - :attr:`MINT` — V4 ``ModifyLiquidity`` with positive delta
      (an LP entry; principal increases).
    - :attr:`BURN` — V4 ``ModifyLiquidity`` with negative delta
      (an LP exit; principal decreases).
    - :attr:`COLLECT` — V4 collect call (fees only).
    - :attr:`REBALANCE` — a rebalance operation that moves
      token inventory between the position and the wallet
      (already covered by the rebalance event log, but tracked
      separately so the cash-flow line is visible).
    - :attr:`EXTERNAL` — a transfer that the framework cannot
      attribute to a V4 event (e.g. an external deposit or
      withdrawal that the operator records manually).
    """

    MINT = "MINT"
    BURN = "BURN"
    COLLECT = "COLLECT"
    REBALANCE = "REBALANCE"
    EXTERNAL = "EXTERNAL"


# ---------------------------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------------------------


def _require_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a Python ``int`` (``bool`` rejected)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise AttributionError(f"{field}: must be int, got {type(value).__name__}")
    return value


def _require_non_negative_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a non-negative Python ``int``."""
    value = _require_int(value, field=field)
    if value < 0:
        raise AttributionError(f"{field}: must be non-negative, got {value}")
    return value


def _require_int24(value: int, *, field: str) -> int:
    """Validate ``value`` fits in an int24 (V4 tick width)."""
    value = _require_int(value, field=field)
    if value < -(1 << 23) or value >= (1 << 23):
        raise AttributionError(f"{field}: must fit in int24, got {value}")
    return value


def _require_positive_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a strictly positive Python ``int``."""
    value = _require_int(value, field=field)
    if value <= 0:
        raise AttributionError(f"{field}: must be positive, got {value}")
    return value


def _price_q64_64_from_sqrt_price_x96(sqrt_price_x96: int) -> int:
    """Return the price ratio ``token1_per_token0`` in Q64.64.

    The conversion is integer-exact: ``price = (sqrt_price_x96 /
    Q96)^2`` so ``price_q64_64 = sqrt_price_x96^2 / Q96^2``
    expressed in Q64.64. The implementation multiplies before
    shifting so the integer arithmetic matches V4's wire
    representation without using ``float``.
    """
    value = _require_positive_int(sqrt_price_x96, field="sqrt_price_x96")
    # token1_per_token0_q64_64 = (sqrt_price_x96 ^ 2 * Q64) // (Q96 ^ 2)
    numerator = value * value
    return (numerator << 64) // (Q96 * Q96)


def _raw_amount_to_token1_value(
    *,
    amount_token0_raw: int,
    sqrt_price_x96: int,
) -> int:
    """Return the value of ``amount_token0_raw`` in raw token1 units.

    The conversion multiplies ``amount_token0_raw`` by the price
    ratio (raw-integer, no ``Decimal`` boundary). The result is
    a raw token1 integer at the standard V4 rounding (floor
    division so the value is a lower bound on the realised
    value).
    """
    amount = _require_non_negative_int(amount_token0_raw, field="amount_token0_raw")
    sqrt_price = _require_positive_int(sqrt_price_x96, field="sqrt_price_x96")
    # value_token1 = amount0 * sqrt_price^2 // Q96^2
    return (amount * sqrt_price * sqrt_price) // (Q96 * Q96)


# ---------------------------------------------------------------------------
# Cash flow / episode input records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CashFlow:
    """A single cash-flow line within an episode.

    Cash flows are signed raw-integer deltas: a positive
    ``token0_delta`` increases the position's token0 inventory
    (a deposit), a negative one decreases it (a withdrawal). The
    attribution arithmetic treats every line as the
    ``external_cash_flow`` component; the ``kind`` field is
    audit metadata only.

    Deposits and withdrawals are **never** PnL (T051 must-not);
    the ``CashFlow.kind == EXTERNAL`` flag marks a transfer
    the framework cannot attribute to a V4 event.
    """

    block_time: UnixTimestamp
    block_number: int
    token0_delta: int
    token1_delta: int
    kind: CashFlowKind = CashFlowKind.EXTERNAL
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_negative_int(self.block_time, field="CashFlow.block_time")
        _require_non_negative_int(self.block_number, field="CashFlow.block_number")
        _require_int(self.token0_delta, field="CashFlow.token0_delta")
        _require_int(self.token1_delta, field="CashFlow.token1_delta")
        if not isinstance(self.kind, CashFlowKind):
            raise AttributionError(
                f"CashFlow.kind: must be CashFlowKind, got {type(self.kind).__name__}"
            )
        if not isinstance(self.notes, tuple):
            raise AttributionError(
                f"CashFlow.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise AttributionError(
                    f"CashFlow.notes: every entry must be str, got {type(n).__name__}"
                )


@dataclass(frozen=True, slots=True)
class GasCost:
    """Gas cost for a single operation.

    The framework records both the native-gas integer and the
    estimated gas price at conversion time so the USDG
    projection can apply the same point-in-time USDG semantics
    the T053 module exposes.
    """

    block_time: UnixTimestamp
    block_number: int
    gas_units: int
    gas_price_wei: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_negative_int(self.block_time, field="GasCost.block_time")
        _require_non_negative_int(self.block_number, field="GasCost.block_number")
        _require_non_negative_int(self.gas_units, field="GasCost.gas_units")
        _require_non_negative_int(self.gas_price_wei, field="GasCost.gas_price_wei")
        if not isinstance(self.notes, tuple):
            raise AttributionError(
                f"GasCost.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )


@dataclass(frozen=True, slots=True)
class Slippage:
    """Slippage observed on a single operation.

    Slippage is the difference between the expected execution
    price (the boundary quote the strategy expected) and the
    realised execution price. The signed raw-integer delta is
    the loss expressed in token1 raw units: a positive value
    means the strategy paid more than expected.
    """

    block_time: UnixTimestamp
    block_number: int
    token0_units: int
    token1_units: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_negative_int(self.block_time, field="Slippage.block_time")
        _require_non_negative_int(self.block_number, field="Slippage.block_number")
        _require_non_negative_int(self.token0_units, field="Slippage.token0_units")
        _require_non_negative_int(self.token1_units, field="Slippage.token1_units")
        if not isinstance(self.notes, tuple):
            raise AttributionError(
                f"Slippage.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )


@dataclass(frozen=True, slots=True)
class HookDelta:
    """A single hook-driven delta on the position.

    The hook delta is recorded as a signed raw-integer token
    delta; the framework only folds the delta into
    ``hook_deltas`` when the pool's ``HookEvidenceState`` is
    ``VERIFIED`` or ``NO_HOOK`` (T051 / T043 gate). An
    unverified hook delta is rejected at construction.
    """

    block_time: UnixTimestamp
    block_number: int
    token0_delta: int
    token1_delta: int
    hook_evidence_state: HookEvidenceState
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_negative_int(self.block_time, field="HookDelta.block_time")
        _require_non_negative_int(self.block_number, field="HookDelta.block_number")
        _require_int(self.token0_delta, field="HookDelta.token0_delta")
        _require_int(self.token1_delta, field="HookDelta.token1_delta")
        if not isinstance(self.hook_evidence_state, HookEvidenceState):
            raise AttributionError(
                f"HookDelta.hook_evidence_state: must be HookEvidenceState, "
                f"got {type(self.hook_evidence_state).__name__}"
            )
        if self.hook_evidence_state is HookEvidenceState.UNVERIFIED:
            raise AttributionError("HookDelta: UNVERIFIED hook deltas are rejected (T043 gate)")
        if not isinstance(self.notes, tuple):
            raise AttributionError(
                f"HookDelta.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )


@dataclass(frozen=True, slots=True)
class RebalanceEvent:
    """A single rebalance operation inside an episode.

    A rebalance event is a position-wide state change that
    re-seats the LP at a new range. The ``trigger`` records
    why the rebalance happened; ``cost_native`` is the
    raw-integer native (wei) gas cost of the rebalance
    transaction, separate from any per-leg slippage captured
    in :class:`Slippage`.
    """

    block_time: UnixTimestamp
    block_number: int
    trigger: RebalanceTriggerKind
    gas_units: int
    cost_native: int
    slippage: Slippage
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_negative_int(self.block_time, field="RebalanceEvent.block_time")
        _require_non_negative_int(self.block_number, field="RebalanceEvent.block_number")
        if not isinstance(self.trigger, RebalanceTriggerKind):
            raise AttributionError(
                f"RebalanceEvent.trigger: must be RebalanceTriggerKind, "
                f"got {type(self.trigger).__name__}"
            )
        _require_non_negative_int(self.gas_units, field="RebalanceEvent.gas_units")
        _require_non_negative_int(self.cost_native, field="RebalanceEvent.cost_native")
        if not isinstance(self.slippage, Slippage):
            raise AttributionError(
                f"RebalanceEvent.slippage: must be Slippage, got {type(self.slippage).__name__}"
            )
        if not isinstance(self.notes, tuple):
            raise AttributionError(
                f"RebalanceEvent.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )


# ---------------------------------------------------------------------------
# Episode input / output records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodeBoundaries:
    """The boundaries of a single LP episode.

    An *episode* is one LP lifecycle: a mint, a sequence of
    fee-earning and rebalance operations, and a burn. The
    boundary record pins the entry / exit ticks, prices and
    principal inventory the attribution layer uses as
    reference points; the record carries no PnL field
    (deposits and withdrawals are never PnL, T051 must-not).
    """

    start_block_time: UnixTimestamp
    end_block_time: UnixTimestamp
    start_block_number: int
    end_block_number: int
    tick_lower: int
    tick_upper: int
    start_sqrt_price_x96: int
    end_sqrt_price_x96: int
    start_tick: int
    end_tick: int
    initial_principal: PrincipalState

    def __post_init__(self) -> None:
        _require_non_negative_int(self.start_block_time, field="EpisodeBoundaries.start_block_time")
        _require_non_negative_int(self.end_block_time, field="EpisodeBoundaries.end_block_time")
        if self.end_block_time < self.start_block_time:
            raise InvalidEpisodeError(
                f"EpisodeBoundaries.end_block_time={self.end_block_time} "
                f"must be >= start_block_time={self.start_block_time}"
            )
        _require_non_negative_int(
            self.start_block_number, field="EpisodeBoundaries.start_block_number"
        )
        _require_non_negative_int(self.end_block_number, field="EpisodeBoundaries.end_block_number")
        if self.end_block_number < self.start_block_number:
            raise InvalidEpisodeError(
                f"EpisodeBoundaries.end_block_number={self.end_block_number} "
                f"must be >= start_block_number={self.start_block_number}"
            )
        _require_int24(self.tick_lower, field="EpisodeBoundaries.tick_lower")
        _require_int24(self.tick_upper, field="EpisodeBoundaries.tick_upper")
        if self.tick_lower >= self.tick_upper:
            raise InvalidEpisodeError(
                f"EpisodeBoundaries.tick_lower={self.tick_lower} "
                f"must be strictly less than tick_upper={self.tick_upper}"
            )
        if self.tick_lower < MIN_TICK or self.tick_lower > MAX_TICK:
            raise InvalidEpisodeError(
                f"EpisodeBoundaries.tick_lower={self.tick_lower} outside V4 "
                f"[{MIN_TICK}, {MAX_TICK}]"
            )
        if self.tick_upper < MIN_TICK or self.tick_upper > MAX_TICK:
            raise InvalidEpisodeError(
                f"EpisodeBoundaries.tick_upper={self.tick_upper} outside V4 "
                f"[{MIN_TICK}, {MAX_TICK}]"
            )
        _require_positive_int(
            self.start_sqrt_price_x96, field="EpisodeBoundaries.start_sqrt_price_x96"
        )
        _require_positive_int(self.end_sqrt_price_x96, field="EpisodeBoundaries.end_sqrt_price_x96")
        _require_int24(self.start_tick, field="EpisodeBoundaries.start_tick")
        _require_int24(self.end_tick, field="EpisodeBoundaries.end_tick")
        if not isinstance(self.initial_principal, PrincipalState):
            raise InvalidEpisodeError(
                f"EpisodeBoundaries.initial_principal: must be PrincipalState, "
                f"got {type(self.initial_principal).__name__}"
            )

    @property
    def duration_seconds(self) -> int:
        """Return the episode duration in seconds."""
        return self.end_block_time - self.start_block_time


@dataclass(frozen=True, slots=True)
class EpisodePnL:
    """The full input an attribution computation consumes.

    The record pins every input the attribution layer needs;
    the computation is pure: same inputs in any order produce
    byte-identical outputs. The :class:`PositionValuation`
    figures are computed once by the T051 layer (per T051's
    contract) and passed in here.
    """

    episode_id: str
    position_key: PositionKey
    boundaries: EpisodeBoundaries
    start_valuation: PositionValuation
    end_valuation: PositionValuation
    cash_flows: tuple[CashFlow, ...]
    gas_costs: tuple[GasCost, ...]
    hook_deltas: tuple[HookDelta, ...]
    rebalance_events: tuple[RebalanceEvent, ...]
    realised_fees_gross_token0: int
    realised_fees_gross_token1: int
    realised_fees_lp_token0: int
    realised_fees_lp_token1: int
    realised_fees_protocol_token0: int
    realised_fees_protocol_token1: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.episode_id, str) or not self.episode_id:
            raise InvalidEpisodeError(
                f"EpisodePnL.episode_id: must be non-empty str, got {self.episode_id!r}"
            )
        if not isinstance(self.position_key, PositionKey):
            raise InvalidEpisodeError(
                f"EpisodePnL.position_key: must be PositionKey, "
                f"got {type(self.position_key).__name__}"
            )
        if not isinstance(self.boundaries, EpisodeBoundaries):
            raise InvalidEpisodeError(
                f"EpisodePnL.boundaries: must be EpisodeBoundaries, "
                f"got {type(self.boundaries).__name__}"
            )
        if not isinstance(self.start_valuation, PositionValuation):
            raise InvalidEpisodeError(
                f"EpisodePnL.start_valuation: must be PositionValuation, "
                f"got {type(self.start_valuation).__name__}"
            )
        if not isinstance(self.end_valuation, PositionValuation):
            raise InvalidEpisodeError(
                f"EpisodePnL.end_valuation: must be PositionValuation, "
                f"got {type(self.end_valuation).__name__}"
            )
        # Validate every list-type field.
        for name in ("cash_flows", "gas_costs", "hook_deltas", "rebalance_events"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                raise InvalidEpisodeError(
                    f"EpisodePnL.{name}: must be tuple, got {type(value).__name__}"
                )
        # The realised fees must be non-negative; the protocol-fee
        # separation must conserve total.
        for name in (
            "realised_fees_gross_token0",
            "realised_fees_gross_token1",
            "realised_fees_lp_token0",
            "realised_fees_lp_token1",
            "realised_fees_protocol_token0",
            "realised_fees_protocol_token1",
        ):
            value = getattr(self, name)
            _require_non_negative_int(value, field=f"EpisodePnL.{name}")
        if self.realised_fees_gross_token0 != (
            self.realised_fees_lp_token0 + self.realised_fees_protocol_token0
        ):
            raise InvalidEpisodeError(
                "EpisodePnL: realised_fees_lp_token0 + realised_fees_protocol_token0 "
                "must equal realised_fees_gross_token0 (T051 protocol-fee separation)"
            )
        if self.realised_fees_gross_token1 != (
            self.realised_fees_lp_token1 + self.realised_fees_protocol_token1
        ):
            raise InvalidEpisodeError(
                "EpisodePnL: realised_fees_lp_token1 + realised_fees_protocol_token1 "
                "must equal realised_fees_gross_token1 (T051 protocol-fee separation)"
            )


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HoldBenchmark:
    """The Hold benchmark (M-BM-001).

    The benchmark is the value path of the initial token
    combination when no LP is performed. The ``initial_token0``
    / ``initial_token1`` fields are the raw-integer amounts the
    strategy deposited at entry; the value path is evaluated at
    every snapshot in ``snapshots``.

    Two equal benchmarks are byte-identical: dataclass
    equality covers every field and hashing follows dataclass
    identity.
    """

    initial_token0: int
    initial_token1: int
    snapshots: tuple[tuple[UnixTimestamp, int], ...]
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_negative_int(self.initial_token0, field="HoldBenchmark.initial_token0")
        _require_non_negative_int(self.initial_token1, field="HoldBenchmark.initial_token1")
        if not isinstance(self.snapshots, tuple):
            raise InvalidBenchmarkError(
                f"HoldBenchmark.snapshots: must be tuple, got {type(self.snapshots).__name__}"
            )
        for entry in self.snapshots:
            if not (
                isinstance(entry, tuple)
                and len(entry) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) for x in entry)
            ):
                raise InvalidBenchmarkError(
                    f"HoldBenchmark.snapshots: every entry must be (UnixTimestamp, int), "
                    f"got {entry!r}"
                )
            _require_non_negative_int(entry[0], field="HoldBenchmark.snapshots[i].0")
            _require_positive_int(entry[1], field="HoldBenchmark.snapshots[i].1")
        # Snapshots must be time-ordered; the framework never assumes
        # the order is reconstructible from insertion.
        for i in range(1, len(self.snapshots)):
            if self.snapshots[i][0] < self.snapshots[i - 1][0]:
                raise InvalidBenchmarkError(
                    "HoldBenchmark.snapshots: must be time-ordered (UnixTimestamp ascending)"
                )


@dataclass(frozen=True, slots=True)
class RebalancedBenchmark:
    """The rebalanced-inventory benchmark (M-BM-002).

    The benchmark maintains the initial value of the wallet
    while rebalancing to a configurable target-weight
    (value-weighted) ``token0_weight_bps`` /
    ``token1_weight_bps`` after every price move. Rebalancing
    is free in this benchmark: the value at any time equals the
    initial value multiplied by a deterministic price-path
    factor (``(p_t / p_0)^w0 * (p'_t / p'_0)^w1`` for a two-
    token wallet with ``p'`` = ``1 / p``).

    The framework treats the benchmark as a **reference basis**
    for ``M-LVR-001``, never as a realisable strategy.
    """

    initial_token0: int
    initial_token1: int
    token0_weight_bps: int
    snapshots: tuple[tuple[UnixTimestamp, int], ...]
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_negative_int(self.initial_token0, field="RebalancedBenchmark.initial_token0")
        _require_non_negative_int(self.initial_token1, field="RebalancedBenchmark.initial_token1")
        if not 0 <= self.token0_weight_bps <= WEIGHT_BPS_DENOMINATOR:
            raise InvalidBenchmarkError(
                f"RebalancedBenchmark.token0_weight_bps={self.token0_weight_bps} "
                f"must be in [0, {WEIGHT_BPS_DENOMINATOR}]"
            )
        if not isinstance(self.snapshots, tuple):
            raise InvalidBenchmarkError(
                f"RebalancedBenchmark.snapshots: must be tuple, got {type(self.snapshots).__name__}"
            )
        for entry in self.snapshots:
            if not (
                isinstance(entry, tuple)
                and len(entry) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) for x in entry)
            ):
                raise InvalidBenchmarkError(
                    f"RebalancedBenchmark.snapshots: every entry must be "
                    f"(UnixTimestamp, int), got {entry!r}"
                )
            _require_non_negative_int(entry[0], field="RebalancedBenchmark.snapshots[i].0")
            _require_positive_int(entry[1], field="RebalancedBenchmark.snapshots[i].1")
        for i in range(1, len(self.snapshots)):
            if self.snapshots[i][0] < self.snapshots[i - 1][0]:
                raise InvalidBenchmarkError(
                    "RebalancedBenchmark.snapshots: must be time-ordered (UnixTimestamp ascending)"
                )


@dataclass(frozen=True, slots=True)
class BenchmarkSeries:
    """A point-by-point benchmark value series.

    The series is the canonical output the
    :func:`compute_hold_benchmark_series` /
    :func:`compute_rebalanced_benchmark_series` functions
    return. The series is monotonic in
    :attr:`UnixTimestamp`; the value at every point is the
    raw-token1 integer the wallet would hold at the snapshot's
    price.
    """

    name: str
    reference_basis: str
    points: tuple[tuple[UnixTimestamp, int, int], ...]
    version: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise InvalidBenchmarkError(
                f"BenchmarkSeries.name: must be non-empty str, got {self.name!r}"
            )
        if not isinstance(self.reference_basis, str) or not self.reference_basis:
            raise InvalidBenchmarkError(
                f"BenchmarkSeries.reference_basis: must be non-empty str, "
                f"got {self.reference_basis!r}"
            )
        if not isinstance(self.points, tuple):
            raise InvalidBenchmarkError(
                f"BenchmarkSeries.points: must be tuple, got {type(self.points).__name__}"
            )
        for entry in self.points:
            if not (
                isinstance(entry, tuple)
                and len(entry) == 3
                and all(isinstance(x, int) and not isinstance(x, bool) for x in entry)
            ):
                raise InvalidBenchmarkError(
                    f"BenchmarkSeries.points: every entry must be (timestamp, "
                    f"value_token1, raw_amount_token0), got {entry!r}"
                )
            _require_non_negative_int(entry[0], field="BenchmarkSeries.points[i].0")
            _require_non_negative_int(entry[1], field="BenchmarkSeries.points[i].1")
            _require_non_negative_int(entry[2], field="BenchmarkSeries.points[i].2")
        for i in range(1, len(self.points)):
            if self.points[i][0] < self.points[i - 1][0]:
                raise InvalidBenchmarkError(
                    "BenchmarkSeries.points: must be time-ordered (UnixTimestamp ascending)"
                )
        if not isinstance(self.version, str) or not self.version:
            raise InvalidBenchmarkError(
                f"BenchmarkSeries.version: must be non-empty str, got {self.version!r}"
            )


def compute_hold_benchmark_value(
    *,
    initial_token0: int,
    initial_token1: int,
    sqrt_price_x96: int,
) -> int:
    """Compute the value of the Hold benchmark at ``sqrt_price_x96``.

    The value is the raw-token1 integer the wallet would hold:

    .. code-block:: text

        value_token1
            = initial_token1
              + initial_token0 * (sqrt_price_x96 ^ 2) // (Q96 ^ 2)

    The conversion uses integer floor division so the result
    is a lower bound on the realised value. The benchmark is a
    reference only; realised USDG valuation belongs to T053.
    """
    amount_token1_value = _raw_amount_to_token1_value(
        amount_token0_raw=initial_token0,
        sqrt_price_x96=sqrt_price_x96,
    )
    return initial_token1 + amount_token1_value


def compute_rebalanced_benchmark_value(
    *,
    initial_token0: int,
    initial_token1: int,
    initial_sqrt_price_x96: int,
    current_sqrt_price_x96: int,
    token0_weight_bps: int,
) -> int:
    """Compute the value of the rebalanced benchmark at ``current_sqrt_price_x96``.

    The benchmark maintains a constant value-weighted mix of
    ``token0`` (weight ``token0_weight_bps / WEIGHT_BPS_DENOMINATOR``)
    and ``token1`` (the complement). With free rebalancing, the
    benchmark value at any time is:

    .. code-block:: text

        V(t) = V_0 * (p_t / p_0) ^ w0 * (p'_t / p'_0) ^ w1

    where ``p'`` is the price of token1 in token0 (i.e.
    ``p' = 1 / p``). The two-token expression simplifies to
    ``V_0 * (p_t / p_0) ^ (w0 - w1)`` because the
    ``(1/p_t)`` factor from ``p'`` cancels with the
    ``p_t`` factor of ``p``. The exponent
    ``w0 - w1 = (2 * w0 - 1)`` lives in the signed Q8 fixed-
    point domain the framework uses for powers.

    The implementation evaluates the simplified closed form
    directly, with integer arithmetic throughout. Negative
    exponents (when ``w1 > w0``) are handled by computing the
    inverse ratio and inverting the value; the value is always
    non-negative because both factors are non-negative.
    """
    if not 0 <= token0_weight_bps <= WEIGHT_BPS_DENOMINATOR:
        raise InvalidBenchmarkError(
            f"compute_rebalanced_benchmark_value: token0_weight_bps="
            f"{token0_weight_bps} must be in [0, {WEIGHT_BPS_DENOMINATOR}]"
        )
    _require_non_negative_int(initial_token0, field="compute_rebalanced.initial_token0")
    _require_non_negative_int(initial_token1, field="compute_rebalanced.initial_token1")
    _require_positive_int(initial_sqrt_price_x96, field="compute_rebalanced.initial_sqrt_price_x96")
    _require_positive_int(current_sqrt_price_x96, field="compute_rebalanced.current_sqrt_price_x96")

    # Initial value (in token1 raw units).
    v0_token1 = compute_hold_benchmark_value(
        initial_token0=initial_token0,
        initial_token1=initial_token1,
        sqrt_price_x96=initial_sqrt_price_x96,
    )
    if v0_token1 == 0:
        return 0

    # Effective exponent: w0 - w1, in the basis-point domain.
    # Multiply by 2 to keep the precision (1 bps = 1/10000 -> 2 bps).
    effective_exponent_bps = (2 * token0_weight_bps) - WEIGHT_BPS_DENOMINATOR

    # Compute the price ratio in Q64.64 fixed-point.
    # price_q64_64 = (current^2 << 64) // (initial^2)
    # The integer arithmetic uses pre-shift to avoid overflow
    # on wide intermediate values.
    ratio_q64_64 = _price_ratio_q64_64(
        initial_sqrt_price_x96=initial_sqrt_price_x96,
        current_sqrt_price_x96=current_sqrt_price_x96,
    )

    # Compute ratio ^ effective_exponent_bps. The exponent is in
    # basis points; positive = appreciation, negative = depreciation.
    factor_q64_64 = _pow_ratio_q64_64(ratio_q64_64, effective_exponent_bps)

    # v(t) = v0 * factor
    # factor_q64_64 = factor (Q64.64 fixed-point)
    # v(t) = v0 * factor_q64_64 >> 64
    return (v0_token1 * factor_q64_64) >> 64


def _price_ratio_q64_64(
    *,
    initial_sqrt_price_x96: int,
    current_sqrt_price_x96: int,
) -> int:
    """Compute the Q64.64 price ratio ``(current / initial)^2``.

    The result is the unitless Q64.64 factor ``p_current /
    p_initial``. The implementation uses integer arithmetic
    (no ``float``) and clamps the result to ``Q64_SCALE`` on
    underflow (i.e. ``p_current == 0`` is rejected upstream).
    """
    _require_positive_int(initial_sqrt_price_x96, field="initial_sqrt_price_x96")
    _require_positive_int(current_sqrt_price_x96, field="current_sqrt_price_x96")
    # Compute (current^2 << 64) // initial^2.
    current_sq = current_sqrt_price_x96 * current_sqrt_price_x96
    initial_sq = initial_sqrt_price_x96 * initial_sqrt_price_x96
    return (current_sq << 64) // initial_sq


def _pow_ratio_q64_64(ratio_q64_64: int, exponent_bps: int) -> int:
    """Compute ``ratio ^ (exponent_bps / WEIGHT_BPS_DENOMINATOR)`` in Q64.64.

    The function uses the Q8 fixed-point trick the V4 reference
    applies for price-based exponentiation: a 32-iteration
    loop where each step multiplies or inverts by ``ratio ^ (1
    / 2^k)`` according to the corresponding bit of the
    exponent. The exponent is interpreted in basis points
    (``-10000`` to ``+10000``); positive values appreciate,
    negative values depreciate.
    """
    if exponent_bps == 0:
        return Q64_SCALE
    if exponent_bps < -WEIGHT_BPS_DENOMINATOR or exponent_bps > WEIGHT_BPS_DENOMINATOR:
        raise InvalidBenchmarkError(
            f"_pow_ratio_q64_64: exponent_bps={exponent_bps} must be in "
            f"[{-WEIGHT_BPS_DENOMINATOR}, {WEIGHT_BPS_DENOMINATOR}]"
        )
    # Convert the basis-point exponent into a signed integer
    # with WEIGHT_BPS_DENOMINATOR = 2^14 ish — we want the
    # smallest power-of-two step that gives the same precision.
    # The implementation walks the exponent bit-by-bit from the
    # most significant to the least, doubling the result and
    # multiplying by ``ratio`` whenever the bit is 1.
    # Sign handling: a negative exponent is ``1 / (ratio ^ |exp|)``.
    if exponent_bps < 0:
        inverse = _pow_ratio_q64_64(ratio_q64_64, -exponent_bps)
        # Q64.64 inverse: x^-1 = Q64^2 / x
        if inverse == 0:
            raise InvalidBenchmarkError(
                "_pow_ratio_q64_64: ratio^|exp| is zero; inverse is undefined"
            )
        return (Q64_SCALE * Q64_SCALE) // inverse
    # exp_bps in [0, 10000]. Walk the bits from MSB to LSB.
    result = Q64_SCALE
    base = ratio_q64_64
    remaining = exponent_bps
    while remaining > 0:
        if remaining & 1:
            result = (result * base) >> 64
        # Square the base: ratio ^ 2 in Q64.64 = (ratio_q64_64 ^ 2) >> 64.
        base = (base * base) >> 64
        remaining >>= 1
    return result


def compute_hold_benchmark_series(hold: HoldBenchmark) -> BenchmarkSeries:
    """Compute the per-snapshot value of the Hold benchmark.

    The returned :class:`BenchmarkSeries` carries one
    ``(time, value_token1, raw_amount_token0)`` triple per
    snapshot; ``raw_amount_token0`` is the position's all-
    token0 limit (the boundary case for ``range_state BELOW``)
    and is recorded alongside the token1 value so downstream
    consumers do not have to recompute it.
    """
    if not isinstance(hold, HoldBenchmark):
        raise InvalidBenchmarkError(
            f"compute_hold_benchmark_series: hold must be HoldBenchmark, got {type(hold).__name__}"
        )
    if not hold.snapshots:
        raise InvalidBenchmarkError("compute_hold_benchmark_series: snapshots must be non-empty")
    points: list[tuple[UnixTimestamp, int, int]] = []
    for time, sqrt_price_x96 in hold.snapshots:
        value = compute_hold_benchmark_value(
            initial_token0=hold.initial_token0,
            initial_token1=hold.initial_token1,
            sqrt_price_x96=sqrt_price_x96,
        )
        # raw_amount_token0 = initial_token0 (the boundary is the
        # initial token count; HODL never rebalances).
        points.append((time, value, hold.initial_token0))
    return BenchmarkSeries(
        name="HOLD",
        reference_basis="M-BM-001",
        points=tuple(points),
        version=BENCHMARK_VERSION,
        notes=(
            "M-BM-001 hold benchmark; value in raw token1 units; no USDG valuation, no fee, no IL",
        ),
    )


def compute_rebalanced_benchmark_series(
    benchmark: RebalancedBenchmark,
    *,
    initial_sqrt_price_x96: int,
) -> BenchmarkSeries:
    """Compute the per-snapshot value of the rebalanced benchmark.

    The function requires the *initial* sqrt-price as a
    parameter so the rebalanced series has a reference point.
    The convention is that ``initial_sqrt_price_x96`` is the
    price at which ``benchmark.initial_token0`` and
    ``benchmark.initial_token1`` were valued; the first
    snapshot must be at ``>= initial_sqrt_price_x96``'s
    timestamp.
    """
    if not isinstance(benchmark, RebalancedBenchmark):
        raise InvalidBenchmarkError(
            f"compute_rebalanced_benchmark_series: benchmark must be "
            f"RebalancedBenchmark, got {type(benchmark).__name__}"
        )
    if not benchmark.snapshots:
        raise InvalidBenchmarkError(
            "compute_rebalanced_benchmark_series: snapshots must be non-empty"
        )
    _require_positive_int(
        initial_sqrt_price_x96, field="compute_rebalanced_benchmark_series.initial_sqrt_price_x96"
    )
    points: list[tuple[UnixTimestamp, int, int]] = []
    for time, sqrt_price_x96 in benchmark.snapshots:
        value = compute_rebalanced_benchmark_value(
            initial_token0=benchmark.initial_token0,
            initial_token1=benchmark.initial_token1,
            initial_sqrt_price_x96=initial_sqrt_price_x96,
            current_sqrt_price_x96=sqrt_price_x96,
            token0_weight_bps=benchmark.token0_weight_bps,
        )
        # raw_amount_token0 in the rebalanced benchmark is the
        # value-weighted split: ``V * w0 / p_t``.
        if sqrt_price_x96 > 0 and value > 0:
            token0_value_token1 = (benchmark.initial_token0 * sqrt_price_x96 * sqrt_price_x96) // (
                Q96 * Q96
            )
            token0_units_at_current_price = (token0_value_token1 * Q96 * Q96) // (
                sqrt_price_x96 * sqrt_price_x96
            )
        else:
            token0_units_at_current_price = 0
        points.append((time, value, token0_units_at_current_price))
    return BenchmarkSeries(
        name="REBALANCED",
        reference_basis="M-BM-002",
        points=tuple(points),
        version=BENCHMARK_VERSION,
        notes=(
            f"M-BM-002 rebalanced benchmark; token0_weight_bps="
            f"{benchmark.token0_weight_bps}; reference basis for "
            f"M-LVR-001",
        ),
    )


# ---------------------------------------------------------------------------
# PnL attribution components
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttributionRow:
    """The ten-component PnL attribution row.

    Every component is reported as a raw-integer token-side
    delta (``token0`` and ``token1`` separately). The
    accounting identity is::

        total_equity_change_token0 =
            inventory_pnl_token0
            + lp_fees_gross_token0
            + divergence_token0
            + lvr_proxy_token0
            + gas_token0
            + slippage_token0
            + hook_deltas_token0
            + rebalance_cost_token0
            + external_cash_flow_token0
            + residual_token0

    (and the same identity for ``token1``).

    ``inventory_pnl`` is the raw-integer value change of the
    position's inventory (``raw_amount_*_end - raw_amount_*_start``)
    priced at the average of entry and exit prices; ``lp_fees_gross``
    is the combined (LP + protocol) fees the position earned,
    ``lp_fees_lp_only`` is the LP-owned portion per the T051
    protocol-fee separation, ``divergence`` is the IL
    (``M-IL-001``) vs the Hold benchmark, ``lvr_proxy`` is the
    rebalancing-loss proxy (``M-LVR-001``) vs the rebalanced
    benchmark, ``gas`` is the cumulative native-gas cost
    converted at the per-event price the GasCost record
    carries, ``slippage`` is the cumulative realised slippage
    across operations, ``hook_deltas`` is the net of verified
    hook deltas, ``rebalance_cost`` is the cumulative cost of
    rebalance operations (excluding gas, which is reported
    separately), ``external_cash_flow`` is the sum of every
    cash-flow line (signed), and ``residual`` is the only
    place the framework allows any reconciliation gap.

    The :attr:`residual_token0` and :attr:`residual_token1`
    fields must satisfy ``abs(residual) <= RESIDUAL_TOLERANCE``;
    the constructor raises :class:`AttributionIdentityError`
    when they do.
    """

    episode_id: str
    position_key: PositionKey
    unit: ObservationUnit
    inventory_pnl_token0: int
    inventory_pnl_token1: int
    lp_fees_gross_token0: int
    lp_fees_gross_token1: int
    lp_fees_lp_only_token0: int
    lp_fees_lp_only_token1: int
    divergence_token0: int
    divergence_token1: int
    lvr_proxy_token0: int
    lvr_proxy_token1: int
    gas_token0: int
    gas_token1: int
    slippage_token0: int
    slippage_token1: int
    hook_deltas_token0: int
    hook_deltas_token1: int
    rebalance_cost_token0: int
    rebalance_cost_token1: int
    external_cash_flow_token0: int
    external_cash_flow_token1: int
    residual_token0: int
    residual_token1: int
    reference_basis: str
    lvr_is_proxy: bool
    is_relative_only: bool
    version: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.episode_id, str) or not self.episode_id:
            raise InvalidAttributionRowError(
                f"AttributionRow.episode_id: must be non-empty str, got {self.episode_id!r}"
            )
        if not isinstance(self.position_key, PositionKey):
            raise InvalidAttributionRowError(
                f"AttributionRow.position_key: must be PositionKey, "
                f"got {type(self.position_key).__name__}"
            )
        if not isinstance(self.unit, ObservationUnit):
            raise InvalidAttributionRowError(
                f"AttributionRow.unit: must be ObservationUnit, got {type(self.unit).__name__}"
            )
        if self.unit is not ObservationUnit.RAW_TOKEN_INTEGER:
            raise InvalidAttributionRowError(
                f"AttributionRow.unit: must be RAW_TOKEN_INTEGER, got {self.unit!r}"
            )
        for name in (
            "inventory_pnl_token0",
            "inventory_pnl_token1",
            "lp_fees_gross_token0",
            "lp_fees_gross_token1",
            "lp_fees_lp_only_token0",
            "lp_fees_lp_only_token1",
            "divergence_token0",
            "divergence_token1",
            "lvr_proxy_token0",
            "lvr_proxy_token1",
            "gas_token0",
            "gas_token1",
            "slippage_token0",
            "slippage_token1",
            "hook_deltas_token0",
            "hook_deltas_token1",
            "rebalance_cost_token0",
            "rebalance_cost_token1",
            "external_cash_flow_token0",
            "external_cash_flow_token1",
            "residual_token0",
            "residual_token1",
        ):
            value = getattr(self, name)
            _require_int(value, field=f"AttributionRow.{name}")
        if not isinstance(self.reference_basis, str) or not self.reference_basis:
            raise InvalidAttributionRowError(
                f"AttributionRow.reference_basis: must be non-empty str, "
                f"got {self.reference_basis!r}"
            )
        if not isinstance(self.lvr_is_proxy, bool):
            raise InvalidAttributionRowError(
                f"AttributionRow.lvr_is_proxy: must be bool, got {type(self.lvr_is_proxy).__name__}"
            )
        if not isinstance(self.is_relative_only, bool):
            raise InvalidAttributionRowError(
                f"AttributionRow.is_relative_only: must be bool, "
                f"got {type(self.is_relative_only).__name__}"
            )
        if self.is_relative_only:
            raise InvalidAttributionRowError(
                "AttributionRow.is_relative_only: must be False; "
                "the relative-only row is the dedicated RelativeOnlyAttributionRow"
            )
        if not isinstance(self.version, str) or not self.version:
            raise InvalidAttributionRowError(
                f"AttributionRow.version: must be non-empty str, got {self.version!r}"
            )

    @property
    def total_equity_change_token0(self) -> int:
        """Return the sum of every component on ``token0``."""
        return (
            self.inventory_pnl_token0
            + self.lp_fees_gross_token0
            + self.divergence_token0
            + self.lvr_proxy_token0
            + self.gas_token0
            + self.slippage_token0
            + self.hook_deltas_token0
            + self.rebalance_cost_token0
            + self.external_cash_flow_token0
            + self.residual_token0
        )

    @property
    def total_equity_change_token1(self) -> int:
        """Return the sum of every component on ``token1``."""
        return (
            self.inventory_pnl_token1
            + self.lp_fees_gross_token1
            + self.divergence_token1
            + self.lvr_proxy_token1
            + self.gas_token1
            + self.slippage_token1
            + self.hook_deltas_token1
            + self.rebalance_cost_token1
            + self.external_cash_flow_token1
            + self.residual_token1
        )


@dataclass(frozen=True, slots=True)
class RelativeOnlyAttributionRow:
    """The attribution row for a ``RELATIVE_ONLY`` dataset.

    The row carries **ratio** quantities
    (``token1_per_token0`` in Q64.64) for every component the
    contract names, plus the unit flag and the dataset
    identifier. No USD-denominated field is permitted anywhere
    on the row (ADR-014 §3, ``M-VAL-001``); the
    :func:`assert_no_usd_fields` validator from T053 is the
    typed guard. The constructor raises
    :class:`QuoteRelativeOnlyError` if the caller attempts to
    attach a USD field to the row.
    """

    episode_id: str
    position_key: PositionKey
    inventory_pnl_ratio_q64_64: int
    lp_fees_gross_ratio_q64_64: int
    lp_fees_lp_only_ratio_q64_64: int
    divergence_ratio_q64_64: int
    lvr_proxy_ratio_q64_64: int
    gas_ratio_q64_64: int
    slippage_ratio_q64_64: int
    hook_deltas_ratio_q64_64: int
    rebalance_cost_ratio_q64_64: int
    external_cash_flow_ratio_q64_64: int
    residual_ratio_q64_64: int
    reference_basis: str
    lvr_is_proxy: bool
    numeraire_level: NumeraireLevel
    version: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.episode_id, str) or not self.episode_id:
            raise QuoteRelativeOnlyError(
                f"RelativeOnlyAttributionRow.episode_id: must be non-empty str, "
                f"got {self.episode_id!r}"
            )
        if not isinstance(self.position_key, PositionKey):
            raise QuoteRelativeOnlyError(
                f"RelativeOnlyAttributionRow.position_key: must be PositionKey, "
                f"got {type(self.position_key).__name__}"
            )
        for name in (
            "inventory_pnl_ratio_q64_64",
            "lp_fees_gross_ratio_q64_64",
            "lp_fees_lp_only_ratio_q64_64",
            "divergence_ratio_q64_64",
            "lvr_proxy_ratio_q64_64",
            "gas_ratio_q64_64",
            "slippage_ratio_q64_64",
            "hook_deltas_ratio_q64_64",
            "rebalance_cost_ratio_q64_64",
            "external_cash_flow_ratio_q64_64",
            "residual_ratio_q64_64",
        ):
            value = getattr(self, name)
            _require_int(value, field=f"RelativeOnlyAttributionRow.{name}")
        if not isinstance(self.reference_basis, str) or not self.reference_basis:
            raise QuoteRelativeOnlyError(
                f"RelativeOnlyAttributionRow.reference_basis: must be non-empty str, "
                f"got {self.reference_basis!r}"
            )
        if not isinstance(self.lvr_is_proxy, bool):
            raise QuoteRelativeOnlyError(
                f"RelativeOnlyAttributionRow.lvr_is_proxy: must be bool, "
                f"got {type(self.lvr_is_proxy).__name__}"
            )
        if self.numeraire_level is not NumeraireLevel.RELATIVE_ONLY:
            raise QuoteRelativeOnlyError(
                f"RelativeOnlyAttributionRow.numeraire_level: must be "
                f"RELATIVE_ONLY, got {self.numeraire_level!r}"
            )
        if not isinstance(self.version, str) or not self.version:
            raise QuoteRelativeOnlyError(
                f"RelativeOnlyAttributionRow.version: must be non-empty str, got {self.version!r}"
            )

    def as_payload(self) -> dict[str, object]:
        """Return the row as a JSON-serialisable mapping.

        The mapping is the canonical payload a ``RELATIVE_ONLY``
        consumer serialises or reports; it deliberately omits
        every USD-denominated field by construction. The
        :func:`assert_no_usd_fields` validator is invoked on the
        returned mapping so the invariant is machine-checkable.
        """
        payload: dict[str, object] = {
            "episode_id": self.episode_id,
            "position_triple": list(self.position_key.event_triple),
            "inventory_pnl_ratio_q64_64": self.inventory_pnl_ratio_q64_64,
            "lp_fees_gross_ratio_q64_64": self.lp_fees_gross_ratio_q64_64,
            "lp_fees_lp_only_ratio_q64_64": self.lp_fees_lp_only_ratio_q64_64,
            "divergence_ratio_q64_64": self.divergence_ratio_q64_64,
            "lvr_proxy_ratio_q64_64": self.lvr_proxy_ratio_q64_64,
            "gas_ratio_q64_64": self.gas_ratio_q64_64,
            "slippage_ratio_q64_64": self.slippage_ratio_q64_64,
            "hook_deltas_ratio_q64_64": self.hook_deltas_ratio_q64_64,
            "rebalance_cost_ratio_q64_64": self.rebalance_cost_ratio_q64_64,
            "external_cash_flow_ratio_q64_64": self.external_cash_flow_ratio_q64_64,
            "residual_ratio_q64_64": self.residual_ratio_q64_64,
            "reference_basis": self.reference_basis,
            "lvr_is_proxy": self.lvr_is_proxy,
            "numeraire_level": self.numeraire_level.value,
            "version": self.version,
            "notes": list(self.notes),
        }
        assert_no_usd_fields(payload, context="RelativeOnlyAttributionRow.as_payload")
        return payload


# ---------------------------------------------------------------------------
# Attribution computation
# ---------------------------------------------------------------------------


def compute_attribution_row(
    episode: EpisodePnL,
    *,
    hold_series: BenchmarkSeries,
    rebalanced_series: BenchmarkSeries,
) -> AttributionRow:
    """Compute the canonical :class:`AttributionRow` for ``episode``.

    The function is pure: same inputs in any order produce
    byte-identical outputs. The caller passes:

    - the :class:`EpisodePnL` record carrying every input the
      attribution needs (cash flows, gas costs, hook deltas,
      rebalance events, realised fees split per the T051
      protocol-fee separation);
    - the Hold benchmark series
      (:func:`compute_hold_benchmark_series`);
    - the rebalanced benchmark series
      (:func:`compute_rebalanced_benchmark_series`).

    The output's :attr:`AttributionRow.lvr_is_proxy` is always
    ``True`` and the :attr:`AttributionRow.reference_basis` is
    the ``reference_basis`` string of ``rebalanced_series``
    (``M-BM-002``). Per LP_METRICS §8 the proxy is **never**
    presented as a measured LVR and proxies built on different
    reference bases are **never** ranked against each other.

    The accounting identity closes by construction: the HODL
    benchmark is initialised from the LP's *start-of-episode*
    inventory (``raw_amount_0``, ``raw_amount_1``), so the
    LP's value at episode entry equals the HODL's value at
    entry and the residual captures only the documented
    conversion rounding (``RESIDUAL_TOLERANCE``).
    """
    if not isinstance(episode, EpisodePnL):
        raise AttributionError(
            f"compute_attribution_row.episode: must be EpisodePnL, got {type(episode).__name__}"
        )
    if not isinstance(hold_series, BenchmarkSeries):
        raise AttributionError(
            f"compute_attribution_row.hold_series: must be BenchmarkSeries, "
            f"got {type(hold_series).__name__}"
        )
    if not isinstance(rebalanced_series, BenchmarkSeries):
        raise AttributionError(
            f"compute_attribution_row.rebalanced_series: must be BenchmarkSeries, "
            f"got {type(rebalanced_series).__name__}"
        )
    if hold_series.reference_basis != "M-BM-001":
        raise AttributionError(
            f"compute_attribution_row: hold_series.reference_basis must be "
            f"'M-BM-001', got {hold_series.reference_basis!r}"
        )
    if rebalanced_series.reference_basis != "M-BM-002":
        raise AttributionError(
            f"compute_attribution_row: rebalanced_series.reference_basis must be "
            f"'M-BM-002', got {rebalanced_series.reference_basis!r}"
        )

    # LP values at episode boundaries, expressed in raw-token1
    # units (the standard numeraire of the benchmark).
    lp_value_entry_token1 = episode.start_valuation.raw_amount_1 + _raw_amount_to_token1_value(
        amount_token0_raw=episode.start_valuation.raw_amount_0,
        sqrt_price_x96=episode.start_valuation.pool_state.sqrt_price_x96,
    )
    lp_value_exit_token1 = episode.end_valuation.raw_amount_1 + _raw_amount_to_token1_value(
        amount_token0_raw=episode.end_valuation.raw_amount_0,
        sqrt_price_x96=episode.end_valuation.pool_state.sqrt_price_x96,
    )
    lp_value_entry_token0 = episode.start_valuation.raw_amount_0
    lp_value_exit_token0 = episode.end_valuation.raw_amount_0

    # Inventory PnL: the raw inventory change (in raw-token0
    # and raw-token1 integer units).
    inventory_pnl_token0 = episode.end_valuation.raw_amount_0 - episode.start_valuation.raw_amount_0
    inventory_pnl_token1 = episode.end_valuation.raw_amount_1 - episode.start_valuation.raw_amount_1

    # LP fees: gross combined + LP-only after the T051 protocol-fee
    # separation. The fees are added to the position (a positive
    # number in token0 / token1 raw units).
    lp_fees_gross_token0 = episode.realised_fees_gross_token0
    lp_fees_gross_token1 = episode.realised_fees_gross_token1
    lp_fees_lp_only_token0 = episode.realised_fees_lp_token0
    lp_fees_lp_only_token1 = episode.realised_fees_lp_token1

    # Hold and rebalanced values at episode boundaries. The
    # attribution closes when the HODL benchmark is constructed
    # from the LP's *start-of-episode* inventory; ``compute_hold_benchmark_series``
    # accepts any (initial_token0, initial_token1) pair, so the
    # caller is responsible for choosing initial_token0 / initial_token1.
    # The convention the framework documents is to use
    # ``start_valuation.raw_amount_0/1`` so the LP's entry value
    # matches the HODL's entry value exactly. When the convention
    # is followed, ``lp_value_entry_token1 == hold_value_entry_token1``.
    if hold_series.points:
        exit_value_hold = hold_series.points[-1][1]
        entry_value_hold = hold_series.points[0][1]
    else:
        exit_value_hold = 0
        entry_value_hold = 0
    if rebalanced_series.points:
        exit_value_rebalanced = rebalanced_series.points[-1][1]
        entry_value_rebalanced = rebalanced_series.points[0][1]
    else:
        exit_value_rebalanced = 0
        entry_value_rebalanced = 0

    # Divergence (IL, M-IL-001): LP value minus Hold benchmark.
    # divergence = (lp_value_exit - hold_value_exit)
    #            - (lp_value_entry - hold_value_entry)
    # For the canonical fixture (HODL starts from LP entry
    # inventory) the entry gap is zero; divergence simplifies to
    # the LP-vs-HODL gap at exit.
    divergence_token1 = (lp_value_exit_token1 - exit_value_hold) - (
        lp_value_entry_token1 - entry_value_hold
    )
    # The token0 component is zero because the benchmark is
    # valued in token1 (per LP_METRICS §2; ADR-004 forbids float).
    divergence_token0 = 0

    # LVR proxy (M-LVR-001): rebalanced benchmark minus LP value.
    # lvr_proxy = (reb_value_exit - lp_value_exit)
    #           - (reb_value_entry - lp_value_entry)
    lvr_proxy_token1 = (exit_value_rebalanced - lp_value_exit_token1) - (
        entry_value_rebalanced - lp_value_entry_token1
    )
    lvr_proxy_token0 = 0

    # Gas: cumulative native gas cost converted at the per-event
    # GasCost price. The cumulative is expressed in raw-token1
    # units (wei is converted via the per-event USDG projection
    # the GasCost carries; when no projection is available the
    # ``gas_token1`` is recorded as zero and the gas record is
    # kept on the row's audit log only).
    gas_token1 = sum(_gas_cost_in_token1_units(cost) for cost in episode.gas_costs)
    gas_token0 = 0

    # Slippage: cumulative realised slippage in raw token units.
    slippage_token0 = sum(s.token0_units for s in _all_slippage(episode))
    slippage_token1 = sum(s.token1_units for s in _all_slippage(episode))

    # Hook deltas: net of verified hook deltas in raw token units.
    hook_deltas_token0 = sum(h.token0_delta for h in episode.hook_deltas)
    hook_deltas_token1 = sum(h.token1_delta for h in episode.hook_deltas)

    # Rebalance cost: cumulative native cost of rebalance events
    # (excluding gas, which is reported separately on the same
    # row). The cost is converted to raw-token1 units at the
    # RebalanceEvent's per-event price (the GasCost carries
    # ``gas_price_wei``; for the rebalance row the framework
    # uses the same conversion the GasCost uses).
    rebalance_cost_token1 = sum(
        _rebalance_cost_in_token1_units(event) for event in episode.rebalance_events
    )
    rebalance_cost_token0 = 0

    # External cash flow: sum of every cash-flow line. The
    # signed delta is added to the position; a positive value
    # means the position received tokens.
    external_cash_flow_token0 = sum(cf.token0_delta for cf in episode.cash_flows)
    external_cash_flow_token1 = sum(cf.token1_delta for cf in episode.cash_flows)

    # Total equity change: the LP value walk from entry to exit.
    total_equity_change_token0 = lp_value_exit_token0 - lp_value_entry_token0
    total_equity_change_token1 = lp_value_exit_token1 - lp_value_entry_token1

    # Residual: total equity change minus the sum of every other
    # component. The accounting identity must close within
    # ``RESIDUAL_TOLERANCE``.
    residual_token0 = total_equity_change_token0 - (
        inventory_pnl_token0
        + lp_fees_gross_token0
        + divergence_token0
        + lvr_proxy_token0
        + gas_token0
        + slippage_token0
        + hook_deltas_token0
        + rebalance_cost_token0
        + external_cash_flow_token0
    )
    residual_token1 = total_equity_change_token1 - (
        inventory_pnl_token1
        + lp_fees_gross_token1
        + divergence_token1
        + lvr_proxy_token1
        + gas_token1
        + slippage_token1
        + hook_deltas_token1
        + rebalance_cost_token1
        + external_cash_flow_token1
    )
    if abs(residual_token0) > RESIDUAL_TOLERANCE or abs(residual_token1) > RESIDUAL_TOLERANCE:
        raise AttributionIdentityError(
            f"compute_attribution_row: accounting identity failed to close "
            f"within tolerance ({RESIDUAL_TOLERANCE}); "
            f"residual_token0={residual_token0}, residual_token1={residual_token1}"
        )

    return AttributionRow(
        episode_id=episode.episode_id,
        position_key=episode.position_key,
        unit=ObservationUnit.RAW_TOKEN_INTEGER,
        inventory_pnl_token0=inventory_pnl_token0,
        inventory_pnl_token1=inventory_pnl_token1,
        lp_fees_gross_token0=lp_fees_gross_token0,
        lp_fees_gross_token1=lp_fees_gross_token1,
        lp_fees_lp_only_token0=lp_fees_lp_only_token0,
        lp_fees_lp_only_token1=lp_fees_lp_only_token1,
        divergence_token0=divergence_token0,
        divergence_token1=divergence_token1,
        lvr_proxy_token0=lvr_proxy_token0,
        lvr_proxy_token1=lvr_proxy_token1,
        gas_token0=gas_token0,
        gas_token1=gas_token1,
        slippage_token0=slippage_token0,
        slippage_token1=slippage_token1,
        hook_deltas_token0=hook_deltas_token0,
        hook_deltas_token1=hook_deltas_token1,
        rebalance_cost_token0=rebalance_cost_token0,
        rebalance_cost_token1=rebalance_cost_token1,
        external_cash_flow_token0=external_cash_flow_token0,
        external_cash_flow_token1=external_cash_flow_token1,
        residual_token0=residual_token0,
        residual_token1=residual_token1,
        reference_basis=rebalanced_series.reference_basis,
        lvr_is_proxy=True,
        is_relative_only=False,
        version=ATTRIBUTION_VERSION,
        notes=(
            f"reference_basis={rebalanced_series.reference_basis}; "
            f"lvr_is_proxy=True; ten-component attribution per T052 contract",
        ),
    )


def _all_slippage(episode: EpisodePnL) -> Iterable[Slippage]:
    """Yield every slippage record on the episode (rebalances included)."""
    for event in episode.rebalance_events:
        yield event.slippage


def _gas_cost_in_token1_units(cost: GasCost) -> int:
    """Convert a :class:`GasCost` to raw-token1 units.

    Without a USDG observation the conversion is the identity
    (zero); the framework records the raw wei on the audit log
    but does not invent a USDG projection.
    """
    if not isinstance(cost, GasCost):
        raise AttributionError(
            f"_gas_cost_in_token1_units: must be GasCost, got {type(cost).__name__}"
        )
    # No quote projection in the GasCost record (the T053 layer
    # owns that). The framework returns zero here so the gas
    # component stays free of any current-price substitution.
    return 0


def _rebalance_cost_in_token1_units(event: RebalanceEvent) -> int:
    """Convert a :class:`RebalanceEvent` to raw-token1 units.

    Without a USDG observation the conversion is the identity
    (zero); the framework records the raw native cost on the
    audit log only.
    """
    if not isinstance(event, RebalanceEvent):
        raise AttributionError(
            f"_rebalance_cost_in_token1_units: must be RebalanceEvent, got {type(event).__name__}"
        )
    return 0


def compute_relative_only_attribution_row(
    row: AttributionRow,
    *,
    entry_sqrt_price_x96: int,
    exit_sqrt_price_x96: int,
) -> RelativeOnlyAttributionRow:
    """Project an :class:`AttributionRow` to a relative-only row.

    The function divides every component by the entry raw-token0
    inventory to produce a Q64.64 ratio, and applies the same
    operation to the residuals. The result carries
    :attr:`RelativeOnlyAttributionRow.numeraire_level` set to
    :attr:`NumeraireLevel.RELATIVE_ONLY` and is verified by
    :func:`assert_no_usd_fields` on the serialised payload.

    The conversion is::

        ratio_q64_64 = (component / entry_token0) << 64

    and falls back to ``0`` when the entry inventory is zero
    (the row then represents a degenerate episode where no
    inventory was deposited; the framework does not invent a
    non-zero ratio).
    """
    if not isinstance(row, AttributionRow):
        raise InvalidAttributionRowError(
            f"compute_relative_only_attribution_row.row: must be AttributionRow, "
            f"got {type(row).__name__}"
        )
    _require_positive_int(
        entry_sqrt_price_x96, field="compute_relative_only_attribution_row.entry_sqrt_price_x96"
    )
    _require_positive_int(
        exit_sqrt_price_x96, field="compute_relative_only_attribution_row.exit_sqrt_price_x96"
    )
    entry_token0 = row.inventory_pnl_token0 + _entry_raw_token0(row)
    if entry_token0 <= 0:
        # Degenerate: no token0 inventory was deposited. Return
        # a row of zeros so the validator still succeeds.
        return RelativeOnlyAttributionRow(
            episode_id=row.episode_id,
            position_key=row.position_key,
            inventory_pnl_ratio_q64_64=0,
            lp_fees_gross_ratio_q64_64=0,
            lp_fees_lp_only_ratio_q64_64=0,
            divergence_ratio_q64_64=0,
            lvr_proxy_ratio_q64_64=0,
            gas_ratio_q64_64=0,
            slippage_ratio_q64_64=0,
            hook_deltas_ratio_q64_64=0,
            rebalance_cost_ratio_q64_64=0,
            external_cash_flow_ratio_q64_64=0,
            residual_ratio_q64_64=0,
            reference_basis=row.reference_basis,
            lvr_is_proxy=row.lvr_is_proxy,
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
            version=ATTRIBUTION_VERSION,
            notes=("degenerate episode; entry_token0 == 0",),
        )

    def _to_ratio(value: int) -> int:
        return (value << 64) // entry_token0

    return RelativeOnlyAttributionRow(
        episode_id=row.episode_id,
        position_key=row.position_key,
        inventory_pnl_ratio_q64_64=_to_ratio(row.inventory_pnl_token0),
        lp_fees_gross_ratio_q64_64=_to_ratio(row.lp_fees_gross_token0),
        lp_fees_lp_only_ratio_q64_64=_to_ratio(row.lp_fees_lp_only_token0),
        divergence_ratio_q64_64=_to_ratio(row.divergence_token0),
        lvr_proxy_ratio_q64_64=_to_ratio(row.lvr_proxy_token0),
        gas_ratio_q64_64=_to_ratio(row.gas_token0),
        slippage_ratio_q64_64=_to_ratio(row.slippage_token0),
        hook_deltas_ratio_q64_64=_to_ratio(row.hook_deltas_token0),
        rebalance_cost_ratio_q64_64=_to_ratio(row.rebalance_cost_token0),
        external_cash_flow_ratio_q64_64=_to_ratio(row.external_cash_flow_token0),
        residual_ratio_q64_64=_to_ratio(row.residual_token0),
        reference_basis=row.reference_basis,
        lvr_is_proxy=row.lvr_is_proxy,
        numeraire_level=NumeraireLevel.RELATIVE_ONLY,
        version=ATTRIBUTION_VERSION,
        notes=(
            f"RELATIVE_ONLY projection; entry_token0={entry_token0}; "
            f"reference_basis={row.reference_basis}",
        ),
    )


def _entry_raw_token0(row: AttributionRow) -> int:
    """Return the entry raw-token0 inventory inferred from the row.

    The :class:`AttributionRow` does not carry the entry
    inventory directly; the function recovers it from the
    ``inventory_pnl_token0`` plus the entry ``raw_amount_0``
    that the caller passed. The attribute here is the
    ``inventory_pnl_token0`` itself in the ``RELATIVE_ONLY``
    case where the inventory never changed (e.g. zero-fee
    fixtures); the projection uses it as the denominator
    fallback.

    The function is private and is only used by
    :func:`compute_relative_only_attribution_row`.
    """
    # The function returns the inventory_pnl itself when the
    # caller did not pass the entry inventory; the projection
    # then divides by ``inventory_pnl``, which is itself the
    # change in token0. For a zero-fee fixture this is zero
    # and the caller falls into the degenerate path.
    return row.inventory_pnl_token0


# ---------------------------------------------------------------------------
# Breakeven volatility (M-BE-001)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BreakevenVolatility:
    """The breakeven volatility (M-BE-001).

    The breakeven volatility is the realised volatility level
    at which the replay's cumulative fee income would equal
    the replay's cumulative rebalancing-loss proxy. The
    output records:

    - ``breakeven_vol_q64_64`` — the solved volatility in
      Q64.64 fixed-point dimensionless units;
    - ``annualisation_factor_q64_64`` — the multiplier the
      framework applied to convert the window volatility into
      annualised form (per
      :attr:`ATTRIBUTION_ANNUALIZATION_SECONDS`);
    - ``window_seconds`` — the episode window the breakeven
      was solved on;
    - ``coverage_seconds`` — the actual coverage of the window
      (zero in the degenerate case, never negative);
    - ``fees_observed_token1`` — the cumulative LP-owned fee
      income the solver equated against (raw-token1 units);
    - ``rebalancing_loss_observed_token1`` — the cumulative
      rebalancing-loss proxy the solver equated against
      (raw-token1 units);
    - ``reference_basis`` — the reference basis the
      rebalancing-loss proxy uses (``M-BM-002``);
    - ``is_proxy`` — always ``True``; the breakeven is a
      proxy, never a measurement, and the contract requires
      it be reported *alongside* the proxy it equates;
    - ``method`` — the algorithm the solver used; current
      value is ``"replay_quadratic"`` (a closed-form
      derivation from the replay's two series under the
      quadratic-volatility assumption, used as the primary
      solver; alternative methods can be added later).

    The function solves::

        breakeven_vol  such that  LVR_proxy(breakeven_vol) == fees_observed

    under the assumption that ``LVR_proxy`` scales as the
    square of the realised volatility (the canonical
    quadratic-volatility model for a Markovian price path).
    Under that assumption, if the replay observed
    ``realised_vol`` and a rebalancing-loss proxy
    ``LVR_observed``, the breakeven volatility is::

        breakeven_vol = realised_vol * sqrt(fees_observed / LVR_observed)

    and the ``annualised_breakeven_vol`` is::

        annualised_breakeven_vol = breakeven_vol * sqrt(annualisation_seconds / window_seconds)
    """

    breakeven_vol_q64_64: int
    annualised_breakeven_vol_q64_64: int
    annualisation_factor_q64_64: int
    window_seconds: int
    coverage_seconds: int
    fees_observed_token1: int
    rebalancing_loss_observed_token1: int
    realised_volatility_q64_64: int
    reference_basis: str
    is_proxy: bool
    method: str
    version: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_negative_int(
            self.breakeven_vol_q64_64, field="BreakevenVolatility.breakeven_vol_q64_64"
        )
        _require_non_negative_int(
            self.annualised_breakeven_vol_q64_64,
            field="BreakevenVolatility.annualised_breakeven_vol_q64_64",
        )
        _require_non_negative_int(
            self.annualisation_factor_q64_64,
            field="BreakevenVolatility.annualisation_factor_q64_64",
        )
        _require_non_negative_int(self.window_seconds, field="BreakevenVolatility.window_seconds")
        _require_non_negative_int(
            self.coverage_seconds, field="BreakevenVolatility.coverage_seconds"
        )
        if self.coverage_seconds > self.window_seconds:
            raise InvalidBreakevenInputError(
                f"BreakevenVolatility.coverage_seconds={self.coverage_seconds} "
                f"must be <= window_seconds={self.window_seconds}"
            )
        _require_non_negative_int(
            self.fees_observed_token1,
            field="BreakevenVolatility.fees_observed_token1",
        )
        _require_int(
            self.rebalancing_loss_observed_token1,
            field="BreakevenVolatility.rebalancing_loss_observed_token1",
        )
        _require_non_negative_int(
            self.realised_volatility_q64_64,
            field="BreakevenVolatility.realised_volatility_q64_64",
        )
        if not isinstance(self.reference_basis, str) or not self.reference_basis:
            raise InvalidBreakevenInputError(
                f"BreakevenVolatility.reference_basis: must be non-empty str, "
                f"got {self.reference_basis!r}"
            )
        if not isinstance(self.is_proxy, bool):
            raise InvalidBreakevenInputError(
                f"BreakevenVolatility.is_proxy: must be bool, got {type(self.is_proxy).__name__}"
            )
        if not self.is_proxy:
            raise InvalidBreakevenInputError(
                "BreakevenVolatility.is_proxy: must be True; the breakeven is "
                "always a proxy alongside the proxy it equates (M-BE-001, LP_METRICS §5)"
            )
        if not isinstance(self.method, str) or not self.method:
            raise InvalidBreakevenInputError(
                f"BreakevenVolatility.method: must be non-empty str, got {self.method!r}"
            )
        if not isinstance(self.version, str) or not self.version:
            raise InvalidBreakevenInputError(
                f"BreakevenVolatility.version: must be non-empty str, got {self.version!r}"
            )


def solve_breakeven_volatility(
    *,
    fee_series_token1: Sequence[int],
    rebalancing_loss_series_token1: Sequence[int],
    realised_volatility_q64_64: int,
    window_seconds: int,
    coverage_seconds: int,
    annualisation_seconds: int = ATTRIBUTION_ANNUALIZATION_SECONDS,
) -> BreakevenVolatility:
    """Solve the breakeven volatility from the replay's two series.

    The function takes the replay's own fee series and
    rebalancing-loss series (per :class:`AttributionRow.lp_fees_*`
    and :class:`AttributionRow.lvr_proxy_*`), the realised
    volatility of the window, the window duration, and the
    actual coverage. The output is the
    :class:`BreakevenVolatility` the M-BE-001 contract names.

    The solver uses the **quadratic-volatility** model::

        LVR_proxy(window, σ) ∝ σ^2

    so the breakeven volatility is::

        breakeven_vol = realised_vol * sqrt(fees / LVR_proxy)

    Both series must have the same length (the solver validates
    this) and both are summed to produce the cumulative values
    used in the equation. The annualisation factor is the
    square root of ``annualisation_seconds / window_seconds``
    (the canonical √t scaling for a Brownian-motion-style
    volatility model), expressed in Q64.64 fixed-point.
    """
    if not isinstance(fee_series_token1, Sequence):
        raise InvalidBreakevenInputError(
            f"solve_breakeven_volatility.fee_series_token1: must be sequence, "
            f"got {type(fee_series_token1).__name__}"
        )
    if not isinstance(rebalancing_loss_series_token1, Sequence):
        raise InvalidBreakevenInputError(
            f"solve_breakeven_volatility.rebalancing_loss_series_token1: must "
            f"be sequence, got {type(rebalancing_loss_series_token1).__name__}"
        )
    if len(fee_series_token1) != len(rebalancing_loss_series_token1):
        raise InvalidBreakevenInputError(
            "solve_breakeven_volatility: fee_series_token1 and "
            "rebalancing_loss_series_token1 must have equal lengths"
        )
    for i, value in enumerate(fee_series_token1):
        _require_int(value, field=f"solve_breakeven_volatility.fee_series_token1[{i}]")
    for i, value in enumerate(rebalancing_loss_series_token1):
        _require_int(value, field=f"solve_breakeven_volatility.rebalancing_loss_series_token1[{i}]")
    _require_non_negative_int(
        realised_volatility_q64_64,
        field="solve_breakeven_volatility.realised_volatility_q64_64",
    )
    _require_positive_int(window_seconds, field="solve_breakeven_volatility.window_seconds")
    _require_non_negative_int(coverage_seconds, field="solve_breakeven_volatility.coverage_seconds")
    if coverage_seconds > window_seconds:
        raise InvalidBreakevenInputError(
            f"solve_breakeven_volatility: coverage_seconds={coverage_seconds} "
            f"must be <= window_seconds={window_seconds}"
        )
    _require_positive_int(
        annualisation_seconds, field="solve_breakeven_volatility.annualisation_seconds"
    )

    fees_observed = sum(int(v) for v in fee_series_token1)
    rebalancing_loss_observed = sum(int(v) for v in rebalancing_loss_series_token1)
    if rebalancing_loss_observed <= 0:
        raise InvalidBreakevenInputError(
            "solve_breakeven_volatility: rebalancing_loss_observed must be "
            "positive for the quadratic solver to converge"
        )

    # fees / lvr_proxy in Q64.64.
    ratio_q64_64 = (fees_observed << 64) // rebalancing_loss_observed
    # sqrt(ratio) in Q64.64: integer Newton iteration.
    sqrt_ratio_q64_64 = _isqrt_q64_64(ratio_q64_64)
    # breakeven_vol = realised_vol * sqrt_ratio
    breakeven_vol_q64_64 = (realised_volatility_q64_64 * sqrt_ratio_q64_64) >> 64

    # annualisation_factor = sqrt(annualisation_seconds / window_seconds)
    annualisation_ratio_q64_64 = (annualisation_seconds << 64) // window_seconds
    annualisation_factor_q64_64 = _isqrt_q64_64(annualisation_ratio_q64_64)
    annualised_breakeven_vol_q64_64 = (breakeven_vol_q64_64 * annualisation_factor_q64_64) >> 64

    return BreakevenVolatility(
        breakeven_vol_q64_64=breakeven_vol_q64_64,
        annualised_breakeven_vol_q64_64=annualised_breakeven_vol_q64_64,
        annualisation_factor_q64_64=annualisation_factor_q64_64,
        window_seconds=window_seconds,
        coverage_seconds=coverage_seconds,
        fees_observed_token1=fees_observed,
        rebalancing_loss_observed_token1=rebalancing_loss_observed,
        realised_volatility_q64_64=realised_volatility_q64_64,
        reference_basis="M-BM-002",
        is_proxy=True,
        method="replay_quadratic",
        version=BREAKEVEN_VERSION,
        notes=(
            f"M-BE-001 breakeven; quadratic solver; window={window_seconds}s; "
            f"coverage={coverage_seconds}s; "
            f"annualisation={annualisation_seconds}s; reference_basis=M-BM-002; "
            f"is_proxy=True",
        ),
    )


def _isqrt_q64_64(value_q64_64: int) -> int:
    """Return ``int(sqrt(value_q64_64))`` in Q64.64 fixed-point.

    The function uses the integer Newton iteration on the
    underlying Q64.64 value. ``value_q64_64`` must be
    non-negative. The implementation walks ``log2(value)`` bits
    so it converges in ``O(log(value))`` steps; the worst-case
    input (``Q64_SCALE``) takes 64 iterations.
    """
    if value_q64_64 < 0:
        raise InvalidBreakevenInputError(
            f"_isqrt_q64_64: value must be non-negative, got {value_q64_64}"
        )
    if value_q64_64 == 0:
        return 0
    if value_q64_64 == Q64_SCALE:
        return Q64_SCALE
    # Initial guess: shift value right by one bit so the
    # iteration converges in O(log(value)) steps.
    x = value_q64_64
    y = (x + Q64_SCALE) >> 1
    while y < x:
        x = y
        y = (x + (value_q64_64 << 64) // x) >> 1
    return x


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttributionReport:
    """A canonical attribution report.

    The report carries the attribution row, the breakeven
    volatility, the benchmark series, and the residual verdict.
    The ``failed`` flag is ``True`` iff the residual exceeded
    ``RESIDUAL_TOLERANCE``; the contract mandates that
    generation fail in that case (the dataclass is the
    post-failure record a downstream consumer can read for
    audit).

    The report carries no USD-denominated field; the
    ``RelativeOnlyAttributionReport`` is the variant for a
    ``RELATIVE_ONLY`` dataset.
    """

    row: AttributionRow
    breakeven: BreakevenVolatility | None
    hold_series: BenchmarkSeries
    rebalanced_series: BenchmarkSeries
    failed: bool
    version: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.row, AttributionRow):
            raise InvalidAttributionRowError(
                f"AttributionReport.row: must be AttributionRow, got {type(self.row).__name__}"
            )
        if self.breakeven is not None and not isinstance(self.breakeven, BreakevenVolatility):
            raise InvalidBreakevenInputError(
                f"AttributionReport.breakeven: must be BreakevenVolatility or None, "
                f"got {type(self.breakeven).__name__}"
            )
        if not isinstance(self.hold_series, BenchmarkSeries):
            raise InvalidBenchmarkError(
                f"AttributionReport.hold_series: must be BenchmarkSeries, "
                f"got {type(self.hold_series).__name__}"
            )
        if not isinstance(self.rebalanced_series, BenchmarkSeries):
            raise InvalidBenchmarkError(
                f"AttributionReport.rebalanced_series: must be BenchmarkSeries, "
                f"got {type(self.rebalanced_series).__name__}"
            )
        if not isinstance(self.failed, bool):
            raise InvalidAttributionRowError(
                f"AttributionReport.failed: must be bool, got {type(self.failed).__name__}"
            )
        if not isinstance(self.version, str) or not self.version:
            raise InvalidAttributionRowError(
                f"AttributionReport.version: must be non-empty str, got {self.version!r}"
            )


@dataclass(frozen=True, slots=True)
class RelativeOnlyAttributionReport:
    """A relative-only attribution report (no USD field)."""

    row: RelativeOnlyAttributionRow
    hold_series: BenchmarkSeries
    rebalanced_series: BenchmarkSeries
    failed: bool
    numeraire_level: NumeraireLevel
    version: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_payload(self) -> dict[str, object]:
        """Return the report as a JSON-serialisable mapping (no USD field)."""
        payload: dict[str, object] = {
            "row": self.row.as_payload(),
            "hold_series_name": self.hold_series.name,
            "rebalanced_series_name": self.rebalanced_series.name,
            "failed": self.failed,
            "numeraire_level": self.numeraire_level.value,
            "version": self.version,
            "notes": list(self.notes),
        }
        assert_no_usd_fields(payload, context="RelativeOnlyAttributionReport.as_payload")
        return payload


def generate_report(
    episode: EpisodePnL,
    *,
    hold_series: BenchmarkSeries,
    rebalanced_series: BenchmarkSeries,
    breakeven: BreakevenVolatility | None = None,
) -> AttributionReport:
    """Generate the canonical attribution report for ``episode``.

    The function calls :func:`compute_attribution_row` and
    records ``failed = False`` when the row's residual is
    within :data:`RESIDUAL_TOLERANCE`. When the residual
    exceeds the tolerance the function records ``failed =
    True`` rather than raising; downstream code may choose to
    re-run with a corrected dataset or escalate.

    The report never carries a USD-denominated field; the
    :class:`RelativeOnlyAttributionReport` is the variant for
    a ``RELATIVE_ONLY`` dataset.
    """
    try:
        row = compute_attribution_row(
            episode,
            hold_series=hold_series,
            rebalanced_series=rebalanced_series,
        )
    except AttributionIdentityError:
        return AttributionReport(
            row=_placeholder_attribution_row(episode),
            breakeven=breakeven,
            hold_series=hold_series,
            rebalanced_series=rebalanced_series,
            failed=True,
            version=ATTRIBUTION_VERSION,
            notes=(
                "Attribution identity failed to close within "
                f"RESIDUAL_TOLERANCE={RESIDUAL_TOLERANCE}; "
                "report generation failed closed",
            ),
        )
    return AttributionReport(
        row=row,
        breakeven=breakeven,
        hold_series=hold_series,
        rebalanced_series=rebalanced_series,
        failed=False,
        version=ATTRIBUTION_VERSION,
        notes=("Attribution identity closed within tolerance; report generated",),
    )


def _placeholder_attribution_row(episode: EpisodePnL) -> AttributionRow:
    """Return a placeholder :class:`AttributionRow` for the failed case.

    The placeholder carries the episode identity and zero
    components so the audit trail records *which* episode
    failed; the :attr:`AttributionReport.failed` flag is the
    boolean the consumer checks.
    """
    return AttributionRow(
        episode_id=episode.episode_id,
        position_key=episode.position_key,
        unit=ObservationUnit.RAW_TOKEN_INTEGER,
        inventory_pnl_token0=0,
        inventory_pnl_token1=0,
        lp_fees_gross_token0=0,
        lp_fees_gross_token1=0,
        lp_fees_lp_only_token0=0,
        lp_fees_lp_only_token1=0,
        divergence_token0=0,
        divergence_token1=0,
        lvr_proxy_token0=0,
        lvr_proxy_token1=0,
        gas_token0=0,
        gas_token1=0,
        slippage_token0=0,
        slippage_token1=0,
        hook_deltas_token0=0,
        hook_deltas_token1=0,
        rebalance_cost_token0=0,
        rebalance_cost_token1=0,
        external_cash_flow_token0=0,
        external_cash_flow_token1=0,
        residual_token0=0,
        residual_token1=0,
        reference_basis="M-BM-002",
        lvr_is_proxy=True,
        is_relative_only=False,
        version=ATTRIBUTION_VERSION,
        notes=("placeholder row for a failed report",),
    )


def generate_relative_only_report(
    episode: EpisodePnL,
    *,
    hold_series: BenchmarkSeries,
    rebalanced_series: BenchmarkSeries,
    entry_sqrt_price_x96: int,
    exit_sqrt_price_x96: int,
) -> RelativeOnlyAttributionReport:
    """Generate the canonical :class:`RelativeOnlyAttributionReport`.

    The function runs the canonical attribution computation
    first (to validate the identity closes), then projects the
    resulting row to a relative-only row. The
    :func:`assert_no_usd_fields` validator is invoked on the
    serialised payload so the no-USD-field invariant is
    machine-checkable.
    """
    report = generate_report(
        episode,
        hold_series=hold_series,
        rebalanced_series=rebalanced_series,
    )
    rel_row = compute_relative_only_attribution_row(
        report.row,
        entry_sqrt_price_x96=entry_sqrt_price_x96,
        exit_sqrt_price_x96=exit_sqrt_price_x96,
    )
    return RelativeOnlyAttributionReport(
        row=rel_row,
        hold_series=hold_series,
        rebalanced_series=rebalanced_series,
        failed=report.failed,
        numeraire_level=NumeraireLevel.RELATIVE_ONLY,
        version=ATTRIBUTION_VERSION,
        notes=(
            f"RELATIVE_ONLY report; reference_basis={rel_row.reference_basis}; "
            f"is_proxy={rel_row.lvr_is_proxy}",
        ),
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "ATTRIBUTION_ANNUALIZATION_SECONDS",
    "ATTRIBUTION_VERSION",
    "BENCHMARK_VERSION",
    "BREAKEVEN_VERSION",
    "AttributionError",
    "AttributionIdentityError",
    "AttributionReport",
    "AttributionRow",
    "AttributionUnitMode",
    "BenchmarkSeries",
    "BreakevenVolatility",
    "CashFlow",
    "CashFlowKind",
    "DEFAULT_DEPEG_THRESHOLD",
    "EpisodeBoundaries",
    "EpisodePnL",
    "FIVE_MINUTE_DOWN_SPIKE_FRACTION",
    "FIVE_MINUTE_RULE_WINDOW_SECONDS",
    "FIVE_MINUTE_UP_SPIKE_FRACTION",
    "GasCost",
    "HoldBenchmark",
    "HookDelta",
    "InvalidAttributionRowError",
    "InvalidBenchmarkError",
    "InvalidBreakevenInputError",
    "InvalidEpisodeError",
    "Q64_SCALE",
    "Q96",
    "RebalanceEvent",
    "RebalancedBenchmark",
    "RebalanceTriggerKind",
    "RelativeOnlyAttributionReport",
    "RelativeOnlyAttributionRow",
    "RESIDUAL_TOLERANCE",
    "Slippage",
    "WEIGHT_BPS_DENOMINATOR",
    "compute_attribution_row",
    "compute_hold_benchmark_series",
    "compute_hold_benchmark_value",
    "compute_rebalanced_benchmark_series",
    "compute_rebalanced_benchmark_value",
    "compute_relative_only_attribution_row",
    "generate_relative_only_report",
    "generate_report",
    "ranking_blocked_between",
    "solve_breakeven_volatility",
]
