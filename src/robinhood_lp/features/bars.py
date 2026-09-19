"""Time and block bars and market features (T050).

This module is the bar / market-feature half of the V1 features
package. It defines:

- the **window** contract (time or block based, left-inclusive /
  right-exclusive), so the bar's left boundary is inclusive and
  its right boundary is exclusive;
- the **event-time** contract — every observation carries an
  ``observed_at`` (block timestamp for time windows, block number
  for block windows) and the builder filters by ``observed_at``,
  not by arrival order, so a bar depends only on events whose
  ``observed_at`` falls inside the window;
- the **watermark policy** that turns a window's right edge into
  an ``availability_time`` (the moment a consumer may treat the
  bar as known), and a **late-event policy** that controls how
  events whose ``observed_at`` is past the window's right edge are
  recorded (the default is ``DROP``; ``COUNT_AS_GAP`` keeps a
  count of late arrivals without silently folding them into the
  older bar);
- **eight market feature bars**: volume, realized volatility,
  price range, active liquidity, depth proxy, effective fee,
  gas, and freshness. Every bar carries its ``unit``, ``window``,
  ``data_time`` and ``availability_time``, plus the bar-type
  payload.

Per the ownership boundary, T050 produces features and the panel
/ label harness (T101) consumes them: this module never defines,
computes or publishes a label, and no label or label-time signal
appears here. Per the architecture's "no centered / global
normalization" rule (ARCHITECTURE §2.1, LP_METRICS §2), the
bars carry raw integer values, not z-scores or rolling
standardisations, and the depth proxy is explicitly labelled as a
proxy rather than as observed depth (LP_METRICS §8).

The bars are float-free. Window arithmetic, watermark arithmetic
and Q64.64 / Q64.96 fixed-point arithmetic are used for the
units that need them (RATIO, RAW_TOKEN_INTEGER). No
``float`` enters a bar value, an availability time or a window
boundary; the integer / Q64.64 boundary used by T049 / T053 is
the only place a Q64.64 quantity is constructed.

The module sits in the features layer (ADR-006 §2.1). It depends
on ``robinhood_lp.protocol`` (typed events for documentation),
``robinhood_lp.features.quote`` (the shared ``ObservationUnit``
enum, which is the public unit vocabulary) and the stdlib. It
must not import RPC, storage, signing, execution, configuration
or the Web layer, and it never constructs an :class:`Observation`
row (T053 owns that surface).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from robinhood_lp.features.quote import ObservationUnit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Q64.64 fixed-point scale (the single named numeraire boundary
#: used by T049 / T053). Bars whose unit is ``RATIO`` use this
#: scale for the integer value they carry.
Q64_SCALE: Final[int] = 1 << 64

#: Q64.96 scale, the native width of V4 ``sqrtPriceX96``.
#: ``price_raw = (sqrtPriceX96 / 2 ** 96) ** 2``; the depth proxy
#: uses this scale to express the price band.
Q96_SCALE: Final[int] = 1 << 96

#: Bar type version. The version is the contract a downstream
#: consumer reads to know which bar definition it is consuming.
#: Bumping the version is a breaking change for downstream code.
BAR_VERSION: Final[str] = "t050.bars.v1"

#: Default depth-proxy band (1% of mid-price in each direction).
#: The depth proxy is a synthetic proxy, NOT observed depth, so
#: the band is configurable but defaults to ±1 %; LP_METRICS §8
#: forbids labelling it as observed depth.
DEFAULT_DEPTH_PROXY_BAND_BPS: Final[int] = 100

#: Maximum basis-point band the depth proxy accepts (100 %).
#: Larger bands are rejected because the first-order Taylor
#: approximation used by :func:`_band_ratio_q64_64` becomes
#: unboundedly wrong past 100 %.
MAX_DEPTH_PROXY_BAND_BPS: Final[int] = 10_000

#: The number of basis points in one unit (so 1 bps = 1/10_000).
BPS_DENOMINATOR: Final[int] = 10_000

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class WindowKind(StrEnum):
    """The two window kinds the bar builder supports.

    Strings are part of the public contract. New values are
    additive; renaming an existing value is a breaking change.

    - :attr:`TIME` — the window covers
      ``[window.start, window.end)`` seconds of block time.
      ``observed_at`` is a Unix timestamp in seconds.
    - :attr:`BLOCK` — the window covers
      ``[window.start, window.end)`` block numbers. ``observed_at``
      is the block number the event landed in. Block numbers are
      not assumed contiguous: an irregular block sequence simply
      produces an empty or sparse window, never a mis-aligned one.
    """

    TIME = "TIME"
    BLOCK = "BLOCK"


class LateEventPolicy(StrEnum):
    """How the streamer handles events whose ``observed_at`` is past
    the window's right edge.

    - :attr:`DROP` — the event is silently dropped from the older
      bar. The bar carries ``late_events_count = 0``.
    - :attr:`COUNT_AS_GAP` — the event is dropped but counted in
      ``late_events_count`` so the consumer can detect the gap.
      Either way the older bar's values are **not** rewritten;
      the event arrives after the bar is closed, and is never
      folded into the older bar's values.
    """

    DROP = "DROP"
    COUNT_AS_GAP = "COUNT_AS_GAP"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BarsError(ValueError):
    """Base class for T050 bar / market-feature failures."""


class InvalidWindowError(BarsError):
    """A :class:`Window` violates its invariants (``end <= start``,
    non-integer fields, or wrong kind)."""


class InvalidObservationError(BarsError):
    """An observation violates its invariants (negative amounts,
    zero price, etc.)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_int(value: int, *, field_name: str) -> int:
    """Validate ``value`` is a Python ``int`` (``bool`` rejected)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidObservationError(f"{field_name}: must be int, got {type(value).__name__}")
    return value


def _require_non_negative_int(value: int, *, field_name: str) -> int:
    """Validate ``value`` is a non-negative Python ``int``."""
    value = _require_int(value, field_name=field_name)
    if value < 0:
        raise InvalidObservationError(f"{field_name}: must be non-negative, got {value}")
    return value


def _is_market_observation(obj: object) -> bool:
    """Return ``True`` iff ``obj`` is one of the four :class:`MarketObservation` types."""
    return isinstance(
        obj,
        (SwapObservation, ModifyLiquidityObservation, GasObservation, BlockHeaderObservation),
    )


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Window:
    """A half-open window ``[start, end)``.

    The left boundary ``start`` is **inclusive**; the right
    boundary ``end`` is **exclusive**. An event whose
    ``observed_at`` equals ``start`` belongs to this window; an
    event whose ``observed_at`` equals ``end`` belongs to the
    next window. This is the only boundary convention the bar
    builder supports — the asymmetry is recorded here so the
    prefix-invariance tests (which exercise both boundaries) can
    pin it.

    Windows are typed by :class:`WindowKind`. A ``TIME`` window's
    ``start`` and ``end`` are Unix timestamps in seconds; a
    ``BLOCK`` window's ``start`` and ``end`` are non-negative
    block numbers. ``start`` and ``end`` may not be equal
    (a window must have positive length).

    Empty windows are allowed: a window whose ``[start, end)``
    range contains zero events still produces a well-formed bar
    whose values are the empty-window sentinels (zero counts,
    ``None`` for variance / high / low / fee). The empty-window
    semantics are part of the contract; see the per-bar
    constructors.
    """

    kind: WindowKind
    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WindowKind):
            raise InvalidWindowError(
                f"Window.kind: must be WindowKind, got {type(self.kind).__name__}"
            )
        _require_int(self.start, field_name="Window.start")
        _require_int(self.end, field_name="Window.end")
        if self.kind is WindowKind.TIME:
            _require_non_negative_int(self.start, field_name="Window.start")
            _require_non_negative_int(self.end, field_name="Window.end")
        else:
            _require_non_negative_int(self.start, field_name="Window.start")
            _require_non_negative_int(self.end, field_name="Window.end")
        if self.end <= self.start:
            raise InvalidWindowError(
                f"Window.end={self.end} must be strictly greater than "
                f"Window.start={self.start} (window must have positive length)"
            )

    @property
    def length(self) -> int:
        """Return ``end - start`` (the window's length)."""
        return self.end - self.start

    @property
    def right_edge(self) -> int:
        """Return ``end`` (the right boundary, exclusive)."""
        return self.end

    @property
    def left_edge(self) -> int:
        """Return ``start`` (the left boundary, inclusive)."""
        return self.start

    def contains(self, event_time: int) -> bool:
        """Return ``True`` iff ``event_time`` is in ``[start, end)``.

        Boundary convention:

        - ``event_time == start``  → **in** the window (left inclusive);
        - ``event_time == end``    → **out** of the window (right exclusive).
        """
        _require_int(event_time, field_name="event_time")
        return self.start <= event_time < self.end

    def is_left_boundary(self, event_time: int) -> bool:
        """Return ``True`` iff ``event_time`` equals ``start``."""
        _require_int(event_time, field_name="event_time")
        return event_time == self.start

    def is_right_boundary(self, event_time: int) -> bool:
        """Return ``True`` iff ``event_time`` equals ``end``.

        An event on the right boundary belongs to the **next**
        window, never to this one. This property is the explicit
        test the prefix-invariance proof exercises.
        """
        _require_int(event_time, field_name="event_time")
        return event_time == self.end


# ---------------------------------------------------------------------------
# Watermark policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WatermarkPolicy:
    """The watermark policy that turns a window's right edge into an
    ``availability_time``.

    The watermark contract is:

    - the bar's ``data_time`` equals the window's right edge
      (``window.end``) — the moment the bar is *as of*;
    - the bar's ``availability_time`` equals
      ``data_time + max_staleness`` — the moment a downstream
      consumer may treat the bar as known. ``max_staleness`` is
      the per-source staleness budget the framework promises
      (T053's :class:`Observation` carries the same budget as
      ``staleness_seconds``);
    - ``max_staleness = 0`` is the canonical "no delay" choice;
      the framework still records ``availability_time`` so the
      consumer can distinguish "this bar is known *now*" from
      "this bar will be known at time t".

    The late-event policy controls how the streaming builder
    records events that arrive after their window's right edge.
    Either policy **drops** the late event from the older bar's
    values — the older bar is never rewritten; the difference is
    whether the count is preserved on the bar.
    """

    max_staleness: int
    late_event_policy: LateEventPolicy = LateEventPolicy.DROP

    def __post_init__(self) -> None:
        _require_non_negative_int(self.max_staleness, field_name="WatermarkPolicy.max_staleness")
        if not isinstance(self.late_event_policy, LateEventPolicy):
            raise BarsError(
                f"WatermarkPolicy.late_event_policy: must be LateEventPolicy, "
                f"got {type(self.late_event_policy).__name__}"
            )

    def compute_availability_time(self, data_time: int) -> int:
        """Return ``data_time + max_staleness``."""
        _require_int(data_time, field_name="data_time")
        return data_time + self.max_staleness


# ---------------------------------------------------------------------------
# Observation types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SwapObservation:
    """One swap observation the bar builder consumes.

    The fields mirror the canonical V4 ``Swap`` event payload the
    replay / reconstruction layer already produces (see
    :class:`robinhood_lp.storage.schema.SwapLogRecord`), but the
    bar builder defines its own minimal observation type so the
    features layer never imports the storage layer (ADR-006
    §2.1).

    - ``observed_at`` is the block timestamp (for time windows)
      or the block number (for block windows) the swap landed in;
    - ``amount0`` / ``amount1`` are the signed atomic-unit
      deltas the V4 event emitted (per
      ``docs/spec/protocol/PROTOCOL_FACTS.md``);
    - ``sqrt_price_x96`` is the post-swap sqrt-price in Q64.96
      fixed-point (V4 wire format);
    - ``fee`` is the on-chain effective combined swap fee, in
      hundredths of a bip, exactly as the Swap event emitted.
    """

    observed_at: int
    block_number: int
    amount0: int
    amount1: int
    sqrt_price_x96: int
    liquidity: int
    tick: int
    fee: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.observed_at, field_name="SwapObservation.observed_at")
        _require_non_negative_int(self.block_number, field_name="SwapObservation.block_number")
        _require_int(self.amount0, field_name="SwapObservation.amount0")
        _require_int(self.amount1, field_name="SwapObservation.amount1")
        # ``sqrt_price_x96`` must be strictly positive — zero is a
        # degenerate price that breaks the realised-variance divisor.
        # The wider uint160 width is not enforced here so the bar
        # builder remains layer-pure; the reconstruction layer owns
        # the V4 width bounds.
        if self.sqrt_price_x96 <= 0:
            raise InvalidObservationError(
                f"SwapObservation.sqrt_price_x96={self.sqrt_price_x96} must be strictly positive"
            )
        _require_non_negative_int(self.liquidity, field_name="SwapObservation.liquidity")
        _require_int(self.tick, field_name="SwapObservation.tick")
        _require_non_negative_int(self.fee, field_name="SwapObservation.fee")


@dataclass(frozen=True, slots=True)
class ModifyLiquidityObservation:
    """One ``ModifyLiquidity`` observation the bar builder consumes.

    The fields mirror the canonical V4 ``ModifyLiquidity`` event
    payload (see
    :class:`robinhood_lp.storage.schema.ModifyLiquidityLogRecord`).
    The bar builder defines its own minimal type for the same
    layer-purity reason as :class:`SwapObservation`.
    """

    observed_at: int
    block_number: int
    liquidity_delta: int
    tick_lower: int
    tick_upper: int

    def __post_init__(self) -> None:
        _require_non_negative_int(
            self.observed_at, field_name="ModifyLiquidityObservation.observed_at"
        )
        _require_non_negative_int(
            self.block_number, field_name="ModifyLiquidityObservation.block_number"
        )
        _require_int(self.liquidity_delta, field_name="ModifyLiquidityObservation.liquidity_delta")
        _require_int(self.tick_lower, field_name="ModifyLiquidityObservation.tick_lower")
        _require_int(self.tick_upper, field_name="ModifyLiquidityObservation.tick_upper")


@dataclass(frozen=True, slots=True)
class GasObservation:
    """One gas observation the bar builder consumes.

    V1 does not produce its own gas on the live execution path
    yet — the gas observation here represents the gas a V4
    transaction consumed, captured by the execution / paper layer
    and emitted with its block number and timestamp. The bar
    builder treats it as an integer in atomic gas units.
    """

    observed_at: int
    block_number: int
    gas_used: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.observed_at, field_name="GasObservation.observed_at")
        _require_non_negative_int(self.block_number, field_name="GasObservation.block_number")
        _require_non_negative_int(self.gas_used, field_name="GasObservation.gas_used")


@dataclass(frozen=True, slots=True)
class BlockHeaderObservation:
    """One block-header observation: just the block number and the
    block timestamp.

    The bar builder uses this for the freshness bar, where the
    staleness of market data is measured against the most recent
    block header regardless of whether that block carried a swap
    or a liquidity event.
    """

    observed_at: int
    block_number: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.observed_at, field_name="BlockHeaderObservation.observed_at")
        _require_non_negative_int(
            self.block_number, field_name="BlockHeaderObservation.block_number"
        )


#: The union of every observation type the bar builder accepts.
#: The streamer discriminates at runtime via ``isinstance``; the
#: type alias documents the contract.
MarketObservation = (
    SwapObservation | ModifyLiquidityObservation | GasObservation | BlockHeaderObservation
)


# ---------------------------------------------------------------------------
# Base bar
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureBar:
    """Base class for every market feature bar.

    Carries the cross-cutting metadata the panel / label harness
    (T101) and the strategy layer need to consume the bar
    deterministically:

    - ``unit`` — the unit the bar's payload is measured in
      (``RAW_TOKEN_INTEGER``, ``RATIO``, ``DIMENSIONLESS``);
    - ``window`` — the ``[start, end)`` window the bar covers
      (left inclusive, right exclusive);
    - ``data_time`` — the moment the bar is *as of*, equal to
      ``window.end``;
    - ``availability_time`` — the moment the bar becomes known,
      equal to ``data_time + max_staleness`` from the watermark
      policy.

    Subclasses add the bar-type-specific payload fields, including
    a ``version`` field that carries the bar type version
    (``BAR_VERSION``). Equality and hashing follow the dataclass
    identity and are stable across Python versions, which is the
    property the prefix-invariance tests rely on.
    """

    unit: ObservationUnit
    window: Window
    data_time: int
    availability_time: int

    def __post_init__(self) -> None:
        if not isinstance(self.unit, ObservationUnit):
            raise BarsError(
                f"FeatureBar.unit: must be ObservationUnit, got {type(self.unit).__name__}"
            )
        if not isinstance(self.window, Window):
            raise BarsError(f"FeatureBar.window: must be Window, got {type(self.window).__name__}")
        _require_int(self.data_time, field_name="FeatureBar.data_time")
        _require_int(self.availability_time, field_name="FeatureBar.availability_time")
        if self.data_time != self.window.end:
            raise BarsError(
                f"FeatureBar.data_time={self.data_time} must equal window.end={self.window.end}"
            )
        if self.availability_time < self.data_time:
            raise BarsError(
                f"FeatureBar.availability_time={self.availability_time} must be "
                f">= data_time={self.data_time}"
            )


# ---------------------------------------------------------------------------
# Bar types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VolumeBar(FeatureBar):
    """Volume of swap activity in the window.

    Payload:

    - ``volume0`` / ``volume1`` — atomic-unit absolute volume per
      side, summed from ``|amount0|`` / ``|amount1|`` of each
      swap whose ``observed_at`` falls inside ``[window.start,
      window.end)``. The signed wire values are converted to
      absolute magnitudes before accumulation, matching how
      :class:`M-FEE-001` (LP_METRICS §3) treats trade volumes;
    - ``swap_count`` — the number of swaps in the window;
    - ``late_events_count`` — the number of events the streamer
      counted as late arrivals (whose ``observed_at`` was past
      the window's right edge when they arrived). The values of
      this bar are **not** changed by late arrivals — the count
      is for the consumer's awareness;
    - ``is_empty`` — ``True`` iff ``swap_count == 0``;
    - ``version`` — :data:`BAR_VERSION`.

    The empty-window semantics are explicit: ``volume0 = volume1
    = 0``, ``swap_count = 0``, ``is_empty = True``.
    """

    volume0: int
    volume1: int
    swap_count: int
    late_events_count: int
    is_empty: bool
    version: str

    def __post_init__(self) -> None:
        FeatureBar.__post_init__(self)
        _require_non_negative_int(self.volume0, field_name="VolumeBar.volume0")
        _require_non_negative_int(self.volume1, field_name="VolumeBar.volume1")
        _require_non_negative_int(self.swap_count, field_name="VolumeBar.swap_count")
        _require_non_negative_int(self.late_events_count, field_name="VolumeBar.late_events_count")
        if not isinstance(self.version, str) or not self.version:
            raise BarsError(f"VolumeBar.version: must be non-empty str, got {self.version!r}")
        if not isinstance(self.is_empty, bool):
            raise BarsError(f"VolumeBar.is_empty: must be bool, got {type(self.is_empty).__name__}")
        if self.unit is not ObservationUnit.RAW_TOKEN_INTEGER:
            raise BarsError(f"VolumeBar.unit: must be RAW_TOKEN_INTEGER, got {self.unit!r}")
        # Empty-window invariant: volume and count must agree.
        if self.is_empty and (self.volume0 != 0 or self.volume1 != 0 or self.swap_count != 0):
            raise BarsError("VolumeBar: is_empty=True implies volume0=volume1=swap_count=0")
        if not self.is_empty and self.swap_count == 0:
            raise BarsError("VolumeBar: is_empty=False implies swap_count >= 1")


@dataclass(frozen=True, slots=True)
class RealizedVolatilityBar(FeatureBar):
    """Variance of sqrt-price returns over the window.

    Payload:

    - ``variance_q64_64`` — the realised variance (Q64.64). The
      unit is ``RATIO`` and the integer value is in Q64.64
      fixed-point (per the T049 / T053 numeraire boundary).
      ``None`` when fewer than two ``sqrt_price`` observations
      were recorded in the window — a single observation is not
      enough to define a return;
    - ``return_count`` — the number of consecutive return pairs
      used (so ``variance_q64_64 is None iff return_count < 1``);
    - ``swap_count`` — the number of swaps whose ``sqrt_price``
      entered the return sequence;
    - ``late_events_count`` — late arrivals the streamer recorded;
    - ``is_empty`` — ``True`` iff no swap landed in the window;
    - ``version`` — :data:`BAR_VERSION`.

    The variance definition (binding):

    - ``returns_i = sqrt_price_i / sqrt_price_{i-1}`` in Q64.64;
    - ``mean = sum(returns) / N``;
    - ``variance = sum((returns_i - mean) ** 2) / N``;
    - the integer value is converted from Q64.128 (squared Q64.64)
      back to Q64.64 by a right-shift of 64.

    This is "variance of sqrt-price returns", not "variance of
    log returns" — the first-order Taylor approximation the
    framework uses here is exact in the limit and conservative
    under it; a downstream consumer (T101, T051) may take a
    square root in its own float boundary (ADR-014 §5).
    """

    variance_q64_64: int | None
    return_count: int
    swap_count: int
    late_events_count: int
    is_empty: bool
    version: str

    def __post_init__(self) -> None:
        FeatureBar.__post_init__(self)
        if not isinstance(self.version, str) or not self.version:
            raise BarsError(
                f"RealizedVolatilityBar.version: must be non-empty str, got {self.version!r}"
            )
        if self.variance_q64_64 is not None:
            _require_non_negative_int(
                self.variance_q64_64, field_name="RealizedVolatilityBar.variance_q64_64"
            )
        _require_non_negative_int(
            self.return_count, field_name="RealizedVolatilityBar.return_count"
        )
        _require_non_negative_int(self.swap_count, field_name="RealizedVolatilityBar.swap_count")
        _require_non_negative_int(
            self.late_events_count, field_name="RealizedVolatilityBar.late_events_count"
        )
        if not isinstance(self.is_empty, bool):
            raise BarsError(
                f"RealizedVolatilityBar.is_empty: must be bool, got {type(self.is_empty).__name__}"
            )
        if self.unit is not ObservationUnit.RATIO:
            raise BarsError(f"RealizedVolatilityBar.unit: must be RATIO, got {self.unit!r}")
        # Empty-window / variance sentinel invariant.
        if self.is_empty:
            if self.swap_count != 0:
                raise BarsError("RealizedVolatilityBar: is_empty=True implies swap_count=0")
            if self.variance_q64_64 is not None:
                raise BarsError(
                    "RealizedVolatilityBar: is_empty=True implies variance_q64_64 is None"
                )
            if self.return_count != 0:
                raise BarsError("RealizedVolatilityBar: is_empty=True implies return_count=0")
        if self.variance_q64_64 is None and self.return_count >= 1:
            raise BarsError("RealizedVolatilityBar: variance_q64_64 is None iff return_count < 1")


@dataclass(frozen=True, slots=True)
class PriceRangeBar(FeatureBar):
    """High / low sqrt-price in the window.

    Payload:

    - ``high_sqrt_price_x96`` / ``low_sqrt_price_x96`` — the
      max / min sqrt-price over the window's swaps, in Q64.96
      fixed-point. ``None`` for both when no swap landed in the
      window;
    - ``swap_count`` — the number of swaps whose price entered
      the range;
    - ``late_events_count`` — late arrivals the streamer recorded;
    - ``is_empty`` — ``True`` iff ``swap_count == 0``;
    - ``version`` — :data:`BAR_VERSION`.

    The empty-window semantics are explicit: ``high = low =
    None``. The first swap in the window sets both high and low.
    """

    high_sqrt_price_x96: int | None
    low_sqrt_price_x96: int | None
    swap_count: int
    late_events_count: int
    is_empty: bool
    version: str

    def __post_init__(self) -> None:
        FeatureBar.__post_init__(self)
        if not isinstance(self.version, str) or not self.version:
            raise BarsError(f"PriceRangeBar.version: must be non-empty str, got {self.version!r}")
        if self.high_sqrt_price_x96 is not None:
            _require_non_negative_int(
                self.high_sqrt_price_x96, field_name="PriceRangeBar.high_sqrt_price_x96"
            )
        if self.low_sqrt_price_x96 is not None:
            _require_non_negative_int(
                self.low_sqrt_price_x96, field_name="PriceRangeBar.low_sqrt_price_x96"
            )
        _require_non_negative_int(self.swap_count, field_name="PriceRangeBar.swap_count")
        _require_non_negative_int(
            self.late_events_count, field_name="PriceRangeBar.late_events_count"
        )
        if not isinstance(self.is_empty, bool):
            raise BarsError(
                f"PriceRangeBar.is_empty: must be bool, got {type(self.is_empty).__name__}"
            )
        if self.unit is not ObservationUnit.RATIO:
            raise BarsError(f"PriceRangeBar.unit: must be RATIO, got {self.unit!r}")
        if self.is_empty:
            if self.swap_count != 0:
                raise BarsError("PriceRangeBar: is_empty=True implies swap_count=0")
            if self.high_sqrt_price_x96 is not None or self.low_sqrt_price_x96 is not None:
                raise BarsError("PriceRangeBar: is_empty=True implies high/low are None")
        if (self.high_sqrt_price_x96 is None) != (self.low_sqrt_price_x96 is None):
            raise BarsError(
                "PriceRangeBar: high_sqrt_price_x96 and low_sqrt_price_x96 "
                "must both be set or both be None"
            )
        if (
            self.high_sqrt_price_x96 is not None
            and self.low_sqrt_price_x96 is not None
            and self.high_sqrt_price_x96 < self.low_sqrt_price_x96
        ):
            raise BarsError(
                f"PriceRangeBar: high={self.high_sqrt_price_x96} < low={self.low_sqrt_price_x96}"
            )


@dataclass(frozen=True, slots=True)
class ActiveLiquidityBar(FeatureBar):
    """Active liquidity at the window's right edge.

    The active-liquidity bar is **stateful**: the consumer passes
    the active liquidity at ``data_time`` and the bar records it.
    This is the convention documented by
    ``M-INV-001`` (LP_METRICS §4): the bar tracks pool state, not
    an event-derived quantity, so its value is whatever the
    pool's active-liquidity view reports at ``window.end``.

    Payload:

    - ``active_liquidity`` — the pool's active liquidity at
      ``data_time`` (uint128 width — bounded by ``MAX_LIQUIDITY``
      but not enforced here so the bar builder remains layer-pure
      and the reconstruction layer applies the bound);
    - ``modify_count`` — the number of ``ModifyLiquidity``
      observations the consumer recorded inside the window;
    - ``late_events_count`` — late arrivals the streamer recorded;
    - ``is_empty`` — ``True`` iff ``modify_count == 0``. An empty
      window's ``active_liquidity`` is whatever state was carried
      into it; the bar records it transparently;
    - ``version`` — :data:`BAR_VERSION`.
    """

    active_liquidity: int
    modify_count: int
    late_events_count: int
    is_empty: bool
    version: str

    def __post_init__(self) -> None:
        FeatureBar.__post_init__(self)
        if not isinstance(self.version, str) or not self.version:
            raise BarsError(
                f"ActiveLiquidityBar.version: must be non-empty str, got {self.version!r}"
            )
        _require_non_negative_int(
            self.active_liquidity, field_name="ActiveLiquidityBar.active_liquidity"
        )
        _require_non_negative_int(self.modify_count, field_name="ActiveLiquidityBar.modify_count")
        _require_non_negative_int(
            self.late_events_count, field_name="ActiveLiquidityBar.late_events_count"
        )
        if not isinstance(self.is_empty, bool):
            raise BarsError(
                f"ActiveLiquidityBar.is_empty: must be bool, got {type(self.is_empty).__name__}"
            )
        if self.unit is not ObservationUnit.RAW_TOKEN_INTEGER:
            raise BarsError(
                f"ActiveLiquidityBar.unit: must be RAW_TOKEN_INTEGER, got {self.unit!r}"
            )
        if self.is_empty and self.modify_count != 0:
            raise BarsError("ActiveLiquidityBar: is_empty=True implies modify_count=0")


@dataclass(frozen=True, slots=True)
class DepthProxyBar(FeatureBar):
    """A synthetic depth proxy, **not** observed depth.

    Per the contract "no labelling a proxy as observed depth",
    this bar is explicitly tagged as a proxy (``is_proxy =
    True``, ``version = BAR_VERSION``, ``unit =
    RATIO``): it is a synthetic quantity computed from the
    pool's active liquidity and the current sqrt-price under the
    assumption that liquidity is **uniformly** distributed across
    the ``±band_bps`` price band, which is **not** how
    concentrated-liquidity pools actually carry liquidity.

    Payload:

    - ``depth_token0_proxy`` — atomic units of token0 the active
      liquidity can absorb if price moves **up** by
      ``band_bps`` (computed from the V4 formula
      ``amount0 = L * (1/sqrtP - 1/sqrtP_upper)``, first-order
      Taylor approximation);
    - ``depth_token1_proxy`` — atomic units of token1 the active
      liquidity can absorb if price moves **down** by
      ``band_bps`` (``amount1 = L * (sqrtP - sqrtP_lower)``);
    - ``band_bps`` — the band in basis points (default 100 = ±1 %);
    - ``is_proxy`` — always ``True``. The flag is part of the
      public contract so a downstream consumer can refuse to
      treat the value as observed depth;
    - ``version`` — :data:`BAR_VERSION`.

    The proxy is ``(0, 0)`` when ``active_liquidity == 0``;
    ``is_empty`` is ``False`` (depth is well-defined at zero
    liquidity) and the consumer is still expected to treat the
    zero as a proxy, not as observed depth.
    """

    depth_token0_proxy: int
    depth_token1_proxy: int
    band_bps: int
    is_proxy: bool
    version: str

    def __post_init__(self) -> None:
        FeatureBar.__post_init__(self)
        if not isinstance(self.version, str) or not self.version:
            raise BarsError(f"DepthProxyBar.version: must be non-empty str, got {self.version!r}")
        _require_non_negative_int(
            self.depth_token0_proxy, field_name="DepthProxyBar.depth_token0_proxy"
        )
        _require_non_negative_int(
            self.depth_token1_proxy, field_name="DepthProxyBar.depth_token1_proxy"
        )
        _require_non_negative_int(self.band_bps, field_name="DepthProxyBar.band_bps")
        if self.band_bps > MAX_DEPTH_PROXY_BAND_BPS:
            raise BarsError(
                f"DepthProxyBar.band_bps={self.band_bps} exceeds "
                f"MAX_DEPTH_PROXY_BAND_BPS={MAX_DEPTH_PROXY_BAND_BPS}"
            )
        if not isinstance(self.is_proxy, bool):
            raise BarsError(
                f"DepthProxyBar.is_proxy: must be bool, got {type(self.is_proxy).__name__}"
            )
        if not self.is_proxy:
            raise BarsError(
                "DepthProxyBar.is_proxy: must be True; the contract forbids "
                "labelling a proxy as observed depth"
            )
        if self.unit is not ObservationUnit.RAW_TOKEN_INTEGER:
            raise BarsError(f"DepthProxyBar.unit: must be RAW_TOKEN_INTEGER, got {self.unit!r}")


@dataclass(frozen=True, slots=True)
class EffectiveFeeBar(FeatureBar):
    """Effective combined swap fee observed in the window.

    Payload:

    - ``average_fee`` — the arithmetic mean of ``fee`` across
      the window's swaps, in the same unit the Swap event emits
      (hundredths of a bip). ``None`` when no swap landed in
      the window;
    - ``last_fee`` — the fee of the last swap in the window.
      ``None`` when no swap landed in the window;
    - ``swap_count`` — the number of swaps in the window;
    - ``late_events_count`` — late arrivals the streamer recorded;
    - ``is_empty`` — ``True`` iff ``swap_count == 0``;
    - ``version`` — :data:`BAR_VERSION`.

    The bar reports the on-chain effective combined fee as
    recorded by the Swap event; dynamic-fee pools contribute the
    fee the chain emitted, not the PoolKey's declared fee.
    """

    average_fee: int | None
    last_fee: int | None
    swap_count: int
    late_events_count: int
    is_empty: bool
    version: str

    def __post_init__(self) -> None:
        FeatureBar.__post_init__(self)
        if not isinstance(self.version, str) or not self.version:
            raise BarsError(f"EffectiveFeeBar.version: must be non-empty str, got {self.version!r}")
        if self.average_fee is not None:
            _require_non_negative_int(self.average_fee, field_name="EffectiveFeeBar.average_fee")
        if self.last_fee is not None:
            _require_non_negative_int(self.last_fee, field_name="EffectiveFeeBar.last_fee")
        _require_non_negative_int(self.swap_count, field_name="EffectiveFeeBar.swap_count")
        _require_non_negative_int(
            self.late_events_count, field_name="EffectiveFeeBar.late_events_count"
        )
        if not isinstance(self.is_empty, bool):
            raise BarsError(
                f"EffectiveFeeBar.is_empty: must be bool, got {type(self.is_empty).__name__}"
            )
        if self.unit is not ObservationUnit.DIMENSIONLESS:
            raise BarsError(f"EffectiveFeeBar.unit: must be DIMENSIONLESS, got {self.unit!r}")
        if self.is_empty:
            if self.swap_count != 0:
                raise BarsError("EffectiveFeeBar: is_empty=True implies swap_count=0")
            if self.average_fee is not None or self.last_fee is not None:
                raise BarsError("EffectiveFeeBar: is_empty=True implies average/last are None")
        if (self.average_fee is None) != (self.last_fee is None):
            raise BarsError(
                "EffectiveFeeBar: average_fee and last_fee must both be set or both be None"
            )


@dataclass(frozen=True, slots=True)
class GasBar(FeatureBar):
    """Gas consumed by V4 transactions in the window.

    Payload:

    - ``gas_used_total`` — the sum of ``gas_used`` across the
      window's gas observations, in atomic gas units;
    - ``observation_count`` — the number of gas observations in
      the window;
    - ``late_events_count`` — late arrivals the streamer recorded;
    - ``is_empty`` — ``True`` iff ``observation_count == 0``;
    - ``version`` — :data:`BAR_VERSION`.
    """

    gas_used_total: int
    observation_count: int
    late_events_count: int
    is_empty: bool
    version: str

    def __post_init__(self) -> None:
        FeatureBar.__post_init__(self)
        if not isinstance(self.version, str) or not self.version:
            raise BarsError(f"GasBar.version: must be non-empty str, got {self.version!r}")
        _require_non_negative_int(self.gas_used_total, field_name="GasBar.gas_used_total")
        _require_non_negative_int(self.observation_count, field_name="GasBar.observation_count")
        _require_non_negative_int(self.late_events_count, field_name="GasBar.late_events_count")
        if not isinstance(self.is_empty, bool):
            raise BarsError(f"GasBar.is_empty: must be bool, got {type(self.is_empty).__name__}")
        if self.unit is not ObservationUnit.RAW_TOKEN_INTEGER:
            raise BarsError(f"GasBar.unit: must be RAW_TOKEN_INTEGER, got {self.unit!r}")
        if self.is_empty and (self.observation_count != 0 or self.gas_used_total != 0):
            raise BarsError(
                "GasBar: is_empty=True implies observation_count=0 and gas_used_total=0"
            )


@dataclass(frozen=True, slots=True)
class FreshnessBar(FeatureBar):
    """How stale the market data is at ``data_time``.

    Payload:

    - ``freshness_seconds`` — ``data_time - latest_observed_at``
      across every market observation the consumer knows about.
      ``0`` when ``latest_observed_at == data_time``; positive
      and unbounded otherwise. ``data_time`` itself when no
      observation has ever been recorded (the window is fully
      empty of market data);
    - ``latest_observed_at`` — the largest ``observed_at`` across
      all observations the consumer has, ``None`` if no
      observation has been recorded;
    - ``observation_count`` — the number of observations the
      consumer inspected when computing freshness;
    - ``late_events_count`` — late arrivals the streamer recorded;
    - ``version`` — :data:`BAR_VERSION``.

    Freshness is a single integer in seconds (DIMENSIONLESS unit);
    it is **not** the consumer's decision-time delta — that delta
    belongs to the strategy layer (T101 / T051).
    """

    freshness_seconds: int
    latest_observed_at: int | None
    observation_count: int
    late_events_count: int
    version: str

    def __post_init__(self) -> None:
        FeatureBar.__post_init__(self)
        if not isinstance(self.version, str) or not self.version:
            raise BarsError(f"FreshnessBar.version: must be non-empty str, got {self.version!r}")
        _require_non_negative_int(
            self.freshness_seconds, field_name="FreshnessBar.freshness_seconds"
        )
        if self.latest_observed_at is not None:
            _require_non_negative_int(
                self.latest_observed_at, field_name="FreshnessBar.latest_observed_at"
            )
        _require_non_negative_int(
            self.observation_count, field_name="FreshnessBar.observation_count"
        )
        _require_non_negative_int(
            self.late_events_count, field_name="FreshnessBar.late_events_count"
        )
        if self.unit is not ObservationUnit.DIMENSIONLESS:
            raise BarsError(f"FreshnessBar.unit: must be DIMENSIONLESS, got {self.unit!r}")
        if self.latest_observed_at is not None:
            expected = self.data_time - self.latest_observed_at
            if expected < 0:
                # data_time may be earlier than the latest observation
                # if the consumer pinned ``data_time`` to an earlier
                # moment; in that case freshness is reported as 0.
                expected = 0
            if self.freshness_seconds != expected:
                raise BarsError(
                    f"FreshnessBar: freshness_seconds={self.freshness_seconds} "
                    f"does not match data_time - latest_observed_at={expected}"
                )


# ---------------------------------------------------------------------------
# Stateless bar constructors
# ---------------------------------------------------------------------------


def _filter_by_window(
    observations: Sequence[MarketObservation], window: Window
) -> list[MarketObservation]:
    """Return observations whose ``observed_at`` lies in ``[start, end)``."""
    return [obs for obs in observations if window.contains(obs.observed_at)]


def compute_volume_bar(
    swaps: Sequence[SwapObservation],
    *,
    window: Window,
    watermark_policy: WatermarkPolicy,
    late_events_count: int = 0,
) -> VolumeBar:
    """Build a :class:`VolumeBar` from a sequence of swap observations.

    The caller is responsible for ordering and filtering. The
    function re-filters by :meth:`Window.contains` so a stray
    observation whose ``observed_at`` falls outside the window is
    ignored. ``late_events_count`` is taken as supplied — it is
    the streamer's count of events that arrived after the
    window's right edge, not the consumer's filter.
    """
    if not isinstance(window, Window):
        raise BarsError(f"compute_volume_bar.window: must be Window, got {type(window).__name__}")
    if not isinstance(watermark_policy, WatermarkPolicy):
        raise BarsError(
            f"compute_volume_bar.watermark_policy: must be WatermarkPolicy, "
            f"got {type(watermark_policy).__name__}"
        )
    _require_non_negative_int(late_events_count, field_name="compute_volume_bar.late_events_count")

    volume0 = 0
    volume1 = 0
    swap_count = 0
    for swap in swaps:
        if not isinstance(swap, SwapObservation):
            raise BarsError(
                f"compute_volume_bar: every entry must be SwapObservation, "
                f"got {type(swap).__name__}"
            )
        if not window.contains(swap.observed_at):
            continue
        # ``abs`` is well-defined because SwapObservation's __post_init__
        # rejects bool and any non-int. ``amount0`` / ``amount1`` are
        # signed on the V4 wire; we accumulate the absolute magnitudes
        # to record traded volume (LP_METRICS §3 ``M-FEE-001``).
        volume0 += abs(swap.amount0)
        volume1 += abs(swap.amount1)
        swap_count += 1
    is_empty = swap_count == 0
    return VolumeBar(
        unit=ObservationUnit.RAW_TOKEN_INTEGER,
        window=window,
        data_time=window.end,
        availability_time=watermark_policy.compute_availability_time(window.end),
        volume0=volume0,
        volume1=volume1,
        swap_count=swap_count,
        late_events_count=late_events_count,
        is_empty=is_empty,
        version=BAR_VERSION,
    )


def compute_realized_volatility_bar(
    swaps: Sequence[SwapObservation],
    *,
    window: Window,
    watermark_policy: WatermarkPolicy,
    late_events_count: int = 0,
) -> RealizedVolatilityBar:
    """Build a :class:`RealizedVolatilityBar` from a sequence of swaps."""
    if not isinstance(window, Window):
        raise BarsError(
            f"compute_realized_volatility_bar.window: must be Window, got {type(window).__name__}"
        )
    if not isinstance(watermark_policy, WatermarkPolicy):
        raise BarsError(
            f"compute_realized_volatility_bar.watermark_policy: must be "
            f"WatermarkPolicy, got {type(watermark_policy).__name__}"
        )
    _require_non_negative_int(
        late_events_count, field_name="compute_realized_volatility_bar.late_events_count"
    )

    sqrt_prices: list[int] = []
    for swap in swaps:
        if not isinstance(swap, SwapObservation):
            raise BarsError(
                f"compute_realized_volatility_bar: every entry must be "
                f"SwapObservation, got {type(swap).__name__}"
            )
        if window.contains(swap.observed_at):
            sqrt_prices.append(swap.sqrt_price_x96)

    variance_q64_64 = _realized_variance_q64_64(sqrt_prices)
    swap_count = len(sqrt_prices)
    is_empty = swap_count == 0
    return RealizedVolatilityBar(
        unit=ObservationUnit.RATIO,
        window=window,
        data_time=window.end,
        availability_time=watermark_policy.compute_availability_time(window.end),
        variance_q64_64=variance_q64_64,
        return_count=max(0, swap_count - 1),
        swap_count=swap_count,
        late_events_count=late_events_count,
        is_empty=is_empty,
        version=BAR_VERSION,
    )


def compute_price_range_bar(
    swaps: Sequence[SwapObservation],
    *,
    window: Window,
    watermark_policy: WatermarkPolicy,
    late_events_count: int = 0,
) -> PriceRangeBar:
    """Build a :class:`PriceRangeBar` from a sequence of swaps."""
    if not isinstance(window, Window):
        raise BarsError(
            f"compute_price_range_bar.window: must be Window, got {type(window).__name__}"
        )
    if not isinstance(watermark_policy, WatermarkPolicy):
        raise BarsError(
            f"compute_price_range_bar.watermark_policy: must be "
            f"WatermarkPolicy, got {type(watermark_policy).__name__}"
        )
    _require_non_negative_int(
        late_events_count, field_name="compute_price_range_bar.late_events_count"
    )

    high: int | None = None
    low: int | None = None
    swap_count = 0
    for swap in swaps:
        if not isinstance(swap, SwapObservation):
            raise BarsError(
                f"compute_price_range_bar: every entry must be "
                f"SwapObservation, got {type(swap).__name__}"
            )
        if not window.contains(swap.observed_at):
            continue
        price = swap.sqrt_price_x96
        if high is None or price > high:
            high = price
        if low is None or price < low:
            low = price
        swap_count += 1
    is_empty = swap_count == 0
    return PriceRangeBar(
        unit=ObservationUnit.RATIO,
        window=window,
        data_time=window.end,
        availability_time=watermark_policy.compute_availability_time(window.end),
        high_sqrt_price_x96=high,
        low_sqrt_price_x96=low,
        swap_count=swap_count,
        late_events_count=late_events_count,
        is_empty=is_empty,
        version=BAR_VERSION,
    )


def compute_active_liquidity_bar(
    active_liquidity: int,
    *,
    window: Window,
    watermark_policy: WatermarkPolicy,
    modify_count: int = 0,
    late_events_count: int = 0,
) -> ActiveLiquidityBar:
    """Build an :class:`ActiveLiquidityBar` from a state value.

    ``active_liquidity`` is the pool's active liquidity at
    ``data_time``; the consumer obtains it from the
    reconstruction layer (T041) — the bar builder does not
    re-derive it from the observation stream because the
    liquidity value is a pool state, not an event aggregate.
    """
    if not isinstance(window, Window):
        raise BarsError(
            f"compute_active_liquidity_bar.window: must be Window, got {type(window).__name__}"
        )
    if not isinstance(watermark_policy, WatermarkPolicy):
        raise BarsError(
            f"compute_active_liquidity_bar.watermark_policy: must be "
            f"WatermarkPolicy, got {type(watermark_policy).__name__}"
        )
    _require_non_negative_int(
        active_liquidity, field_name="compute_active_liquidity_bar.active_liquidity"
    )
    _require_non_negative_int(modify_count, field_name="compute_active_liquidity_bar.modify_count")
    _require_non_negative_int(
        late_events_count, field_name="compute_active_liquidity_bar.late_events_count"
    )

    is_empty = modify_count == 0
    return ActiveLiquidityBar(
        unit=ObservationUnit.RAW_TOKEN_INTEGER,
        window=window,
        data_time=window.end,
        availability_time=watermark_policy.compute_availability_time(window.end),
        active_liquidity=active_liquidity,
        modify_count=modify_count,
        late_events_count=late_events_count,
        is_empty=is_empty,
        version=BAR_VERSION,
    )


def compute_depth_proxy_bar(
    *,
    active_liquidity: int,
    current_sqrt_price_x96: int,
    window: Window,
    watermark_policy: WatermarkPolicy,
    band_bps: int = DEFAULT_DEPTH_PROXY_BAND_BPS,
    late_events_count: int = 0,
) -> DepthProxyBar:
    """Build a :class:`DepthProxyBar`.

    The proxy is computed under the assumption that liquidity is
    uniformly distributed across ``±band_bps``, which is **not**
    how concentrated-liquidity pools actually carry liquidity;
    the result is therefore explicitly tagged as a proxy via
    ``is_proxy=True`` and ``unit = RAW_TOKEN_INTEGER``.
    """
    if not isinstance(window, Window):
        raise BarsError(
            f"compute_depth_proxy_bar.window: must be Window, got {type(window).__name__}"
        )
    if not isinstance(watermark_policy, WatermarkPolicy):
        raise BarsError(
            f"compute_depth_proxy_bar.watermark_policy: must be "
            f"WatermarkPolicy, got {type(watermark_policy).__name__}"
        )
    _require_non_negative_int(
        active_liquidity, field_name="compute_depth_proxy_bar.active_liquidity"
    )
    _require_non_negative_int(
        current_sqrt_price_x96, field_name="compute_depth_proxy_bar.current_sqrt_price_x96"
    )
    _require_non_negative_int(band_bps, field_name="compute_depth_proxy_bar.band_bps")
    if band_bps > MAX_DEPTH_PROXY_BAND_BPS:
        raise BarsError(
            f"compute_depth_proxy_bar.band_bps={band_bps} exceeds "
            f"MAX_DEPTH_PROXY_BAND_BPS={MAX_DEPTH_PROXY_BAND_BPS}"
        )
    _require_non_negative_int(
        late_events_count, field_name="compute_depth_proxy_bar.late_events_count"
    )

    depth_token0_proxy, depth_token1_proxy = _depth_proxy_pair(
        active_liquidity=active_liquidity,
        current_sqrt_price_x96=current_sqrt_price_x96,
        band_bps=band_bps,
    )
    return DepthProxyBar(
        unit=ObservationUnit.RAW_TOKEN_INTEGER,
        window=window,
        data_time=window.end,
        availability_time=watermark_policy.compute_availability_time(window.end),
        depth_token0_proxy=depth_token0_proxy,
        depth_token1_proxy=depth_token1_proxy,
        band_bps=band_bps,
        is_proxy=True,
        version=BAR_VERSION,
    )


def compute_effective_fee_bar(
    swaps: Sequence[SwapObservation],
    *,
    window: Window,
    watermark_policy: WatermarkPolicy,
    late_events_count: int = 0,
) -> EffectiveFeeBar:
    """Build an :class:`EffectiveFeeBar` from a sequence of swaps."""
    if not isinstance(window, Window):
        raise BarsError(
            f"compute_effective_fee_bar.window: must be Window, got {type(window).__name__}"
        )
    if not isinstance(watermark_policy, WatermarkPolicy):
        raise BarsError(
            f"compute_effective_fee_bar.watermark_policy: must be "
            f"WatermarkPolicy, got {type(watermark_policy).__name__}"
        )
    _require_non_negative_int(
        late_events_count, field_name="compute_effective_fee_bar.late_events_count"
    )

    total = 0
    last: int | None = None
    swap_count = 0
    for swap in swaps:
        if not isinstance(swap, SwapObservation):
            raise BarsError(
                f"compute_effective_fee_bar: every entry must be "
                f"SwapObservation, got {type(swap).__name__}"
            )
        if not window.contains(swap.observed_at):
            continue
        total += swap.fee
        last = swap.fee
        swap_count += 1
    is_empty = swap_count == 0
    average: int | None = total // swap_count if swap_count > 0 else None
    return EffectiveFeeBar(
        unit=ObservationUnit.DIMENSIONLESS,
        window=window,
        data_time=window.end,
        availability_time=watermark_policy.compute_availability_time(window.end),
        average_fee=average,
        last_fee=last,
        swap_count=swap_count,
        late_events_count=late_events_count,
        is_empty=is_empty,
        version=BAR_VERSION,
    )


def compute_gas_bar(
    gas_observations: Sequence[GasObservation],
    *,
    window: Window,
    watermark_policy: WatermarkPolicy,
    late_events_count: int = 0,
) -> GasBar:
    """Build a :class:`GasBar` from a sequence of gas observations."""
    if not isinstance(window, Window):
        raise BarsError(f"compute_gas_bar.window: must be Window, got {type(window).__name__}")
    if not isinstance(watermark_policy, WatermarkPolicy):
        raise BarsError(
            f"compute_gas_bar.watermark_policy: must be "
            f"WatermarkPolicy, got {type(watermark_policy).__name__}"
        )
    _require_non_negative_int(late_events_count, field_name="compute_gas_bar.late_events_count")

    gas_total = 0
    observation_count = 0
    for obs in gas_observations:
        if not isinstance(obs, GasObservation):
            raise BarsError(
                f"compute_gas_bar: every entry must be GasObservation, got {type(obs).__name__}"
            )
        if not window.contains(obs.observed_at):
            continue
        gas_total += obs.gas_used
        observation_count += 1
    is_empty = observation_count == 0
    return GasBar(
        unit=ObservationUnit.RAW_TOKEN_INTEGER,
        window=window,
        data_time=window.end,
        availability_time=watermark_policy.compute_availability_time(window.end),
        gas_used_total=gas_total,
        observation_count=observation_count,
        late_events_count=late_events_count,
        is_empty=is_empty,
        version=BAR_VERSION,
    )


def compute_freshness_bar(
    observations: Iterable[MarketObservation],
    *,
    window: Window,
    watermark_policy: WatermarkPolicy,
    late_events_count: int = 0,
) -> FreshnessBar:
    """Build a :class:`FreshnessBar` from the observation stream.

    The freshness bar uses ``latest_observed_at = max(observed_at)``
    across **all** market observations the consumer has seen
    (swaps, modify-liquidity, gas and block headers — every
    :class:`MarketObservation` subtype). The freshness value is
    ``data_time - latest_observed_at`` (zero if the latest
    observation is at or after ``data_time``).

    When no observation has ever been recorded the bar carries
    ``latest_observed_at = None`` and
    ``freshness_seconds = data_time - 0`` (a fully-empty
    dataset's freshness equals the data time itself).
    """
    if not isinstance(window, Window):
        raise BarsError(
            f"compute_freshness_bar.window: must be Window, got {type(window).__name__}"
        )
    if not isinstance(watermark_policy, WatermarkPolicy):
        raise BarsError(
            f"compute_freshness_bar.watermark_policy: must be "
            f"WatermarkPolicy, got {type(watermark_policy).__name__}"
        )
    _require_non_negative_int(
        late_events_count, field_name="compute_freshness_bar.late_events_count"
    )

    latest: int | None = None
    observation_count = 0
    for obs in observations:
        if not _is_market_observation(obs):
            raise BarsError(
                f"compute_freshness_bar: every entry must be a MarketObservation, "
                f"got {type(obs).__name__}"
            )
        if latest is None or obs.observed_at > latest:
            latest = obs.observed_at
        observation_count += 1
    freshness = window.end if latest is None else max(0, window.end - latest)
    return FreshnessBar(
        unit=ObservationUnit.DIMENSIONLESS,
        window=window,
        data_time=window.end,
        availability_time=watermark_policy.compute_availability_time(window.end),
        freshness_seconds=freshness,
        latest_observed_at=latest,
        observation_count=observation_count,
        late_events_count=late_events_count,
        version=BAR_VERSION,
    )


# ---------------------------------------------------------------------------
# Streaming builder
# ---------------------------------------------------------------------------


class MarketBarStreamer:
    """A streaming bar builder that detects late events.

    The streamer is the only entry point that exposes
    "late-event handling": it tracks every event's arrival order
    separately from its ``observed_at``, so an event whose
    ``observed_at`` falls in a window that has already been
    closed is counted as late and **not** folded into the older
    bar.

    Usage:

    1. construct with the window size, the window kind (time or
       block) and the watermark policy;
    2. ``add(observation)`` for every observation in arrival
       order (which may differ from ``observed_at`` order);
    3. ``build_volume_bar(current_window)`` (or any of the other
       ``build_*_bar`` methods) for the window whose right edge
       is now closed; the returned bar carries the
       ``late_events_count`` the streamer has detected so far.

    The streamer never re-emits a closed window — once
    ``build_*_bar`` returns a bar for window W, no further event
    with ``observed_at`` in W will be folded into W, even if the
    caller passes it to ``add``. This is the prefix-invariance
    guarantee the contract asks the implementation to prove.
    """

    def __init__(
        self,
        *,
        window_size: int,
        window_kind: WindowKind = WindowKind.TIME,
        watermark_policy: WatermarkPolicy,
    ) -> None:
        if not isinstance(window_kind, WindowKind):
            raise BarsError(
                f"MarketBarStreamer.window_kind: must be WindowKind, "
                f"got {type(window_kind).__name__}"
            )
        if not isinstance(watermark_policy, WatermarkPolicy):
            raise BarsError(
                f"MarketBarStreamer.watermark_policy: must be WatermarkPolicy, "
                f"got {type(watermark_policy).__name__}"
            )
        _require_int(window_size, field_name="MarketBarStreamer.window_size")
        if window_size <= 0:
            raise BarsError(f"MarketBarStreamer.window_size={window_size} must be positive")
        self._window_size: int = window_size
        self._window_kind: WindowKind = window_kind
        self._watermark_policy: WatermarkPolicy = watermark_policy
        self._arrivals: list[tuple[int, MarketObservation]] = []
        self._arrival_count: int = 0
        # The arrival_count recorded when ``build_*_bar`` first
        # closes each window_end. Subsequent ``add`` calls with a
        # higher arrival_count are "late for that window" — they
        # never enter the closed bar's values, only its
        # ``late_events_count``.
        self._closed_at: dict[int, int] = {}

    @property
    def window_size(self) -> int:
        return self._window_size

    @property
    def window_kind(self) -> WindowKind:
        return self._window_kind

    @property
    def watermark_policy(self) -> WatermarkPolicy:
        return self._watermark_policy

    @property
    def arrival_count(self) -> int:
        return self._arrival_count

    def add(self, observation: MarketObservation) -> None:
        """Record ``observation`` in arrival order.

        ``observed_at`` is stored alongside the arrival index so
        a subsequent ``build_*_bar`` call can detect which events
        arrived after the window's right edge.
        """
        if not _is_market_observation(observation):
            raise BarsError(
                f"MarketBarStreamer.add: must be MarketObservation, "
                f"got {type(observation).__name__}"
            )
        self._arrivals.append((self._arrival_count, observation))
        self._arrival_count += 1

    def _window_for(self, end: int) -> Window:
        """Return the canonical window ``[..., end)``."""
        return Window(kind=self._window_kind, start=end - self._window_size, end=end)

    def _close_window(self, window_end: int) -> int:
        """Record the current ``arrival_count`` as the closure of
        ``window_end``. Returns the max arrival index the bar
        builder will consider for this window.
        """
        existing = self._closed_at.get(window_end)
        if existing is not None:
            return existing
        self._closed_at[window_end] = self._arrival_count
        return self._arrival_count

    def _split_by_window(
        self, window: Window, max_arrival_index: int
    ) -> tuple[list[MarketObservation], int]:
        """Return ``(in-window observations, late count)``.

        Two distinct sources of lateness are counted:

        - **Arrival-time late.** An in-window observation whose
          arrival happened once the streamer's watermark had
          already passed the window's right edge. Concretely:
          an observation with ``observed_at`` in
          ``[window.start, window.end)`` whose arrival followed
          another observation with ``observed_at >= window.end``.
          The watermark is updated only by arrivals whose
          ``observed_at > watermark`` (and not by in-window
          arrivals, which don't move the watermark past the
          window's right edge).
        - **Post-closure late.** An in-window observation whose
          arrival index is ``>= max_arrival_index`` — it was
          added **after** the streamer was asked to close the
          window. The closed bar's values are never rewritten;
          the late observation is recorded in
          ``late_events_count`` so the consumer can detect the
          gap.

        Both kinds of late observation are excluded from the
        bar's values; both contribute to ``late_events_count``.
        The split is prefix-invariant: adding a future
        observation after the window was closed cannot change
        the closed bar's payload.
        """
        in_window: list[MarketObservation] = []
        late = 0
        watermark = 0
        for arrival_index, obs in self._arrivals:
            if arrival_index >= max_arrival_index:
                break
            if window.contains(obs.observed_at):
                if watermark >= window.end:
                    late += 1
                    continue
                in_window.append(obs)
            else:
                if obs.observed_at > watermark:
                    watermark = obs.observed_at
        # Post-closure arrivals whose observed_at falls inside
        # the window are "late for the closed window": the bar
        # has already been finalised but the data the consumer
        # expected has only just arrived. The bar's values are
        # unchanged; the count is for the consumer's awareness.
        for arrival_index, obs in self._arrivals:
            if arrival_index >= max_arrival_index and window.contains(obs.observed_at):
                late += 1
        return in_window, late

    def build_volume_bar(self, *, window_end: int) -> VolumeBar:
        """Build the :class:`VolumeBar` for the window ending at ``window_end``."""
        if not isinstance(window_end, int) or isinstance(window_end, bool):
            raise BarsError(
                f"MarketBarStreamer.build_volume_bar.window_end: must be int, "
                f"got {type(window_end).__name__}"
            )
        if window_end <= 0:
            raise BarsError(
                f"MarketBarStreamer.build_volume_bar.window_end={window_end} must be positive"
            )
        if window_end % self._window_size != 0:
            raise BarsError(
                f"MarketBarStreamer.build_volume_bar.window_end={window_end} "
                f"must be a multiple of window_size={self._window_size}"
            )
        window = self._window_for(window_end)
        max_arrival = self._close_window(window_end)
        in_window, late = self._split_by_window(window, max_arrival)
        swaps = [obs for obs in in_window if isinstance(obs, SwapObservation)]
        # late events include in-window non-swap events too
        late_non_swap = sum(1 for obs in in_window if not isinstance(obs, SwapObservation))
        late_events_count = late + late_non_swap
        return compute_volume_bar(
            swaps,
            window=window,
            watermark_policy=self._watermark_policy,
            late_events_count=late_events_count,
        )

    def build_realized_volatility_bar(self, *, window_end: int) -> RealizedVolatilityBar:
        """Build the :class:`RealizedVolatilityBar` for the window ending at ``window_end``."""
        if not isinstance(window_end, int) or isinstance(window_end, bool):
            raise BarsError(
                f"MarketBarStreamer.build_realized_volatility_bar.window_end: "
                f"must be int, got {type(window_end).__name__}"
            )
        if window_end <= 0 or window_end % self._window_size != 0:
            raise BarsError(
                f"MarketBarStreamer.build_realized_volatility_bar.window_end="
                f"{window_end} must be a positive multiple of "
                f"window_size={self._window_size}"
            )
        window = self._window_for(window_end)
        max_arrival = self._close_window(window_end)
        in_window, late = self._split_by_window(window, max_arrival)
        swaps = [obs for obs in in_window if isinstance(obs, SwapObservation)]
        late_non_swap = sum(1 for obs in in_window if not isinstance(obs, SwapObservation))
        late_events_count = late + late_non_swap
        return compute_realized_volatility_bar(
            swaps,
            window=window,
            watermark_policy=self._watermark_policy,
            late_events_count=late_events_count,
        )

    def build_price_range_bar(self, *, window_end: int) -> PriceRangeBar:
        """Build the :class:`PriceRangeBar` for the window ending at ``window_end``."""
        if not isinstance(window_end, int) or isinstance(window_end, bool):
            raise BarsError(
                f"MarketBarStreamer.build_price_range_bar.window_end: "
                f"must be int, got {type(window_end).__name__}"
            )
        if window_end <= 0 or window_end % self._window_size != 0:
            raise BarsError(
                f"MarketBarStreamer.build_price_range_bar.window_end="
                f"{window_end} must be a positive multiple of "
                f"window_size={self._window_size}"
            )
        window = self._window_for(window_end)
        max_arrival = self._close_window(window_end)
        in_window, late = self._split_by_window(window, max_arrival)
        swaps = [obs for obs in in_window if isinstance(obs, SwapObservation)]
        late_non_swap = sum(1 for obs in in_window if not isinstance(obs, SwapObservation))
        late_events_count = late + late_non_swap
        return compute_price_range_bar(
            swaps,
            window=window,
            watermark_policy=self._watermark_policy,
            late_events_count=late_events_count,
        )

    def build_active_liquidity_bar(
        self,
        *,
        window_end: int,
        active_liquidity: int,
        modify_count: int = 0,
    ) -> ActiveLiquidityBar:
        """Build the :class:`ActiveLiquidityBar` for the window ending at ``window_end``."""
        if not isinstance(window_end, int) or isinstance(window_end, bool):
            raise BarsError(
                f"MarketBarStreamer.build_active_liquidity_bar.window_end: "
                f"must be int, got {type(window_end).__name__}"
            )
        if window_end <= 0 or window_end % self._window_size != 0:
            raise BarsError(
                f"MarketBarStreamer.build_active_liquidity_bar.window_end="
                f"{window_end} must be a positive multiple of "
                f"window_size={self._window_size}"
            )
        window = self._window_for(window_end)
        max_arrival = self._close_window(window_end)
        in_window, late = self._split_by_window(window, max_arrival)
        late_events_count = late
        return compute_active_liquidity_bar(
            active_liquidity,
            window=window,
            watermark_policy=self._watermark_policy,
            modify_count=modify_count,
            late_events_count=late_events_count,
        )

    def build_depth_proxy_bar(
        self,
        *,
        window_end: int,
        active_liquidity: int,
        current_sqrt_price_x96: int,
        band_bps: int = DEFAULT_DEPTH_PROXY_BAND_BPS,
    ) -> DepthProxyBar:
        """Build the :class:`DepthProxyBar` for the window ending at ``window_end``."""
        if not isinstance(window_end, int) or isinstance(window_end, bool):
            raise BarsError(
                f"MarketBarStreamer.build_depth_proxy_bar.window_end: "
                f"must be int, got {type(window_end).__name__}"
            )
        if window_end <= 0 or window_end % self._window_size != 0:
            raise BarsError(
                f"MarketBarStreamer.build_depth_proxy_bar.window_end="
                f"{window_end} must be a positive multiple of "
                f"window_size={self._window_size}"
            )
        window = self._window_for(window_end)
        max_arrival = self._close_window(window_end)
        _, late = self._split_by_window(window, max_arrival)
        return compute_depth_proxy_bar(
            active_liquidity=active_liquidity,
            current_sqrt_price_x96=current_sqrt_price_x96,
            window=window,
            watermark_policy=self._watermark_policy,
            band_bps=band_bps,
            late_events_count=late,
        )

    def build_effective_fee_bar(self, *, window_end: int) -> EffectiveFeeBar:
        """Build the :class:`EffectiveFeeBar` for the window ending at ``window_end``."""
        if not isinstance(window_end, int) or isinstance(window_end, bool):
            raise BarsError(
                f"MarketBarStreamer.build_effective_fee_bar.window_end: "
                f"must be int, got {type(window_end).__name__}"
            )
        if window_end <= 0 or window_end % self._window_size != 0:
            raise BarsError(
                f"MarketBarStreamer.build_effective_fee_bar.window_end="
                f"{window_end} must be a positive multiple of "
                f"window_size={self._window_size}"
            )
        window = self._window_for(window_end)
        max_arrival = self._close_window(window_end)
        in_window, late = self._split_by_window(window, max_arrival)
        swaps = [obs for obs in in_window if isinstance(obs, SwapObservation)]
        late_non_swap = sum(1 for obs in in_window if not isinstance(obs, SwapObservation))
        late_events_count = late + late_non_swap
        return compute_effective_fee_bar(
            swaps,
            window=window,
            watermark_policy=self._watermark_policy,
            late_events_count=late_events_count,
        )

    def build_gas_bar(self, *, window_end: int) -> GasBar:
        """Build the :class:`GasBar` for the window ending at ``window_end``."""
        if not isinstance(window_end, int) or isinstance(window_end, bool):
            raise BarsError(
                f"MarketBarStreamer.build_gas_bar.window_end: "
                f"must be int, got {type(window_end).__name__}"
            )
        if window_end <= 0 or window_end % self._window_size != 0:
            raise BarsError(
                f"MarketBarStreamer.build_gas_bar.window_end="
                f"{window_end} must be a positive multiple of "
                f"window_size={self._window_size}"
            )
        window = self._window_for(window_end)
        max_arrival = self._close_window(window_end)
        in_window, late = self._split_by_window(window, max_arrival)
        gas = [obs for obs in in_window if isinstance(obs, GasObservation)]
        late_non_gas = sum(1 for obs in in_window if not isinstance(obs, GasObservation))
        late_events_count = late + late_non_gas
        return compute_gas_bar(
            gas,
            window=window,
            watermark_policy=self._watermark_policy,
            late_events_count=late_events_count,
        )

    def build_freshness_bar(self, *, window_end: int) -> FreshnessBar:
        """Build the :class:`FreshnessBar` for the window ending at ``window_end``."""
        if not isinstance(window_end, int) or isinstance(window_end, bool):
            raise BarsError(
                f"MarketBarStreamer.build_freshness_bar.window_end: "
                f"must be int, got {type(window_end).__name__}"
            )
        if window_end <= 0 or window_end % self._window_size != 0:
            raise BarsError(
                f"MarketBarStreamer.build_freshness_bar.window_end="
                f"{window_end} must be a positive multiple of "
                f"window_size={self._window_size}"
            )
        window = self._window_for(window_end)
        max_arrival = self._close_window(window_end)
        in_window, late = self._split_by_window(window, max_arrival)
        late_events_count = late
        return compute_freshness_bar(
            in_window,
            window=window,
            watermark_policy=self._watermark_policy,
            late_events_count=late_events_count,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _realized_variance_q64_64(sqrt_prices: Sequence[int]) -> int | None:
    """Return the variance of sqrt-price returns in Q64.64.

    Returns ``None`` when fewer than two prices are available —
    a single observation cannot define a return. Skips any
    ``sqrt_price == 0`` observation (an invalid price input)
    and recomputes the count accordingly.
    """
    if len(sqrt_prices) < 2:
        return None
    returns_q64: list[int] = []
    for i in range(1, len(sqrt_prices)):
        prev = sqrt_prices[i - 1]
        cur = sqrt_prices[i]
        if prev <= 0 or cur <= 0:
            continue
        # Return ratio ``cur / prev`` in Q64.64.
        returns_q64.append((cur << 64) // prev)
    n = len(returns_q64)
    if n < 1:
        return None
    total = sum(returns_q64)
    mean = total // n
    sum_sq_dev = 0
    for r in returns_q64:
        dev = r - mean
        sum_sq_dev += dev * dev
    # ``sum_sq_dev`` is in Q64.128 (Q64.64 squared); convert to
    # Q64.64 by shifting right by 64.
    variance_q64_128 = sum_sq_dev // n
    return variance_q64_128 >> 64


def _band_ratio_q64_64(band_bps: int, *, upward: bool) -> int:
    """Return the sqrt-price ratio ``sqrt(1 ± band_bps/10_000)`` in Q64.64.

    Uses a first-order Taylor approximation
    ``sqrt(1 + x) ≈ 1 + x / 2`` valid for ``|x| < 1`` (so
    ``band_bps <= 10_000``). The approximation is documented as a
    proxy in :class:`DepthProxyBar`.
    """
    _require_non_negative_int(band_bps, field_name="band_bps")
    if band_bps > MAX_DEPTH_PROXY_BAND_BPS:
        raise BarsError(
            f"_band_ratio_q64_64: band_bps={band_bps} exceeds "
            f"MAX_DEPTH_PROXY_BAND_BPS={MAX_DEPTH_PROXY_BAND_BPS}"
        )
    x_q64 = (Q64_SCALE * band_bps) // BPS_DENOMINATOR  # band_bps / 10000 in Q64.64
    half_x_q64 = x_q64 >> 1
    if upward:
        return Q64_SCALE + half_x_q64
    # Downward: 1 - x/2 (clamped at 1 to avoid non-positive values
    # when the Taylor expansion underflows the integer boundary).
    result = Q64_SCALE - half_x_q64
    if result <= 0:
        return 1  # Q64.64 sentinel; treat as "no liquidity band"
    return result


def _depth_proxy_pair(
    *,
    active_liquidity: int,
    current_sqrt_price_x96: int,
    band_bps: int,
) -> tuple[int, int]:
    """Compute ``(depth_token0_proxy, depth_token1_proxy)``.

    First-order Taylor approximation:

    - ``depth_token1_proxy`` (token1 absorbed if price moves down
      by ``band_bps``) ≈ ``L * sqrtP * (band_bps/10000) / 2``;
    - ``depth_token0_proxy`` (token0 absorbed if price moves up by
      ``band_bps``) ≈ ``L * (band_bps/10000) / (2 * sqrtP)``.

    All values are integer atomic units (the function never
    converts to display decimals). Both depths are zero when
    ``active_liquidity == 0``.
    """
    if active_liquidity == 0:
        return (0, 0)
    if current_sqrt_price_x96 <= 0:
        return (0, 0)
    # half-band ratio in Q64.64 = band_bps / 20000
    half_band_q64 = (Q64_SCALE * band_bps) // (2 * BPS_DENOMINATOR)
    # depth_token1_proxy = L * sqrtP_x96 * (band_bps/10000) / 2
    # = L * sqrtP_x96 * half_band_q64 / Q64_SCALE
    depth_token1_proxy = (active_liquidity * current_sqrt_price_x96 * half_band_q64) >> 64
    # depth_token0_proxy = L / sqrtP_x96 * (band_bps/10000) / 2
    # In integer math: L * (Q96_SCALE / sqrtP_x96) * half_band_q64 / Q64_SCALE
    # = (L * Q96_SCALE * half_band_q64) // (sqrtP_x96 * Q64_SCALE)
    depth_token0_proxy = (active_liquidity * Q96_SCALE * half_band_q64) // (
        current_sqrt_price_x96 * Q64_SCALE
    )
    return (depth_token0_proxy, depth_token1_proxy)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "BAR_VERSION",
    "BPS_DENOMINATOR",
    "DEFAULT_DEPTH_PROXY_BAND_BPS",
    "MAX_DEPTH_PROXY_BAND_BPS",
    "Q64_SCALE",
    "Q96_SCALE",
    "ActiveLiquidityBar",
    "BarsError",
    "BlockHeaderObservation",
    "DepthProxyBar",
    "EffectiveFeeBar",
    "FeatureBar",
    "FreshnessBar",
    "GasBar",
    "GasObservation",
    "InvalidObservationError",
    "InvalidWindowError",
    "LateEventPolicy",
    "MarketBarStreamer",
    "MarketObservation",
    "ModifyLiquidityObservation",
    "PriceRangeBar",
    "Q64_SCALE",
    "Q96_SCALE",
    "RealizedVolatilityBar",
    "SwapObservation",
    "VolumeBar",
    "WatermarkPolicy",
    "Window",
    "WindowKind",
    "compute_active_liquidity_bar",
    "compute_depth_proxy_bar",
    "compute_effective_fee_bar",
    "compute_freshness_bar",
    "compute_gas_bar",
    "compute_price_range_bar",
    "compute_realized_volatility_bar",
    "compute_volume_bar",
]
