"""LP position valuation at any point (T051).

T051 is the position-valuation half of the V1 features package. It
implements:

- the **position identity** the framework uses when no V4 NFT
  ``tokenId`` is available — the
  ``(tick_lower, tick_upper, salt)`` triple the V4 ``ModifyLiquidity``
  event emits. The triple is the only position identity the
  framework can reconstruct from on-chain events (the NFT
  ``tokenId`` and ``owner`` are not in the event payload and are
  not inferable from the ``PoolManager`` sender alone, per the
  T051 must-not clause);
- the **point-in-time inventory** of ``token0`` and ``token1`` the
  position holds at a price, computed from the position's
  liquidity, the V4 ``[tick_lower, tick_upper]`` Range, and the
  current ``sqrt_price_x96``. The values are the same integer
  amounts the V4 ``ModifyLiquidity`` event uses for mint / burn;
- the **in-range / out-of-range state** the position is in — the
  same ``"below"`` / ``"inside"`` / ``"above"`` classification the
  T049 sizer produces, but here expressed as a typed enum and
  re-derived from the position's tick boundaries and the current
  tick so a downstream consumer does not have to re-run the V4
  classification by hand;
- the **principal inventory** the position carries separately from
  earned fees. Principal inventory is the inventory the position
  deposited at mint, plus any **donation** or **verified-hook
  credit** the position was attributed, minus any **collected**
  amount. Deposits and withdrawals are **never** counted as PnL
  (T051 must-not): principal is a balance ledger, not a return;
- the **fee-growth accounting** that turns the V4
  ``fee_growth_global*`` accumulator minus the position's
  ``fee_growth_inside_last`` snapshot at mint into the
  ``tokens_owed`` figure a ``collect`` would release. The formula
  is the byte-exact V4 ``pool.modify`` settlement (the position's
  share is ``liquidity * (fee_growth_inside_now -
  fee_growth_inside_last) / 2 ** 128``), and the rounding is
  *floor* per V4 reference;
- the **protocol-fee separation** the Owner amendment of
  2026-09-16 records: when the input fee-growth figures are the
  *combined* swap-fee accumulator, the LP-owned share is the
  combined amount times ``(lp_fee - protocol_fee) / lp_fee`` where
  ``lp_fee`` is the V4 PoolKey's declared fee and
  ``protocol_fee`` is the directional 12-bit half T040 replays.
  When the input figures are already the LP-only fee growth, the
  protocol-fee split is the identity and the ``protocol_fee_*``
  fields are zero. Either path lands in the same canonical
  ``PositionValuation`` output;
- **mint / burn / collect** as the three lifecycle operations the
  valuation understands. A mint increases principal and bumps the
  ``fee_growth_inside_last`` snapshot to the current inside
  growth; a burn returns principal and owed fees; a collect
  returns owed fees and zeros the position's owed-fee accumulator
  while preserving principal. Add / remove operations are pure
  mint / burn with the same accounting rules;
- **donations** (``Donate`` event) and **hook credits** (verified
  per T043) — when the T043 hook-evidence pack marks a hook as
  verified, the framework attributes donation amounts and
  verified hook credit deltas to the position's principal (they
  are balance additions, not PnL). Without verified hook evidence
  the framework records donations as raw ``donation_amount*`` and
  does not fold them into principal (the verification gate is
  the T043 boundary; the position layer never reads hook
  semantics on its own).

Design constraints (binding):

- **No float on the protocol / valuation path.** Per ADR-004 every
  arithmetic step is performed as Python ``int``. The single
  ``2 ** 128`` divisor is integer floor division.
- **No future data.** ``compute_position_valuation`` is a pure
  function of the supplied snapshot and the supplied point-in-time
  state; it never reads a chain, an event, or a clock.
- **No wallet inference.** The position is identified by its
  ``PositionKey`` triple or, when known, by its V4 NFT
  ``tokenId``. The ``PoolManager`` ``sender`` is never used as a
  wallet identity (T051 must-not).
- **No PnL from deposits / withdrawals.** Principal is a ledger;
  deposits and withdrawals change the principal balance, never
  PnL (T051 must-not).

The module depends only on the protocol-domain package
(``math`` / ``ids`` / ``sizing``), the features.quote module's
``ObservationUnit`` vocabulary, and the stdlib. It must not
import RPC, storage, configuration, signing, execution or the
replay layer (the replay output is consumed by the caller; the
position layer never imports the replayer).

References:

- R7 (independent state reads): ``StateView.getFeeGrowthGlobals`` /
  ``getFeeGrowthInside`` / ``getTickFeeGrowthOutside`` — the source
  of the point-in-time fee-growth values the snapshot consumes.
- R8 (SDK comparator): ``Uniswap V4 SDK`` provides the
  ``Position.value`` API the framework follows, but every output
  here is a hand-derived integer (no SDK import).
- R15 (V3 whitepaper concentrated-liquidity math): the
  ``fee_growth_inside`` derivation and the
  ``tokens_owed = liquidity * delta_inside / 2**128`` formula.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from robinhood_lp.features.quote import ObservationUnit
from robinhood_lp.protocol.math import (
    MAX_TICK,
    MIN_TICK,
    get_amount0_delta,
    get_amount1_delta,
    get_sqrt_price_at_tick,
)
from robinhood_lp.protocol.sizing import (
    POSITION_ABOVE,
    POSITION_BELOW,
    POSITION_INSIDE,
    PositionRelative,
    get_amounts_for_liquidity,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Q128 fixed-point scale (the V4 ``feeGrowth*X128`` width). All
#: fee-growth inputs are non-negative integers in Q128 units; the
#: fee-owed formula is ``liquidity * delta_inside >> 128`` (floor).
Q128: Final[int] = 1 << 128

#: Q64.96 fixed-point scale (the native V4 ``sqrtPriceX96`` width).
Q96: Final[int] = 1 << 96

#: Bar / valuation version. Bumping the version is a breaking
#: change for downstream consumers (T101 / T052 / T070 read it).
POSITION_VALUATION_VERSION: Final[str] = "t051.position.v1"

#: The 24-bit ceiling on a V4 ``combined_swap_fee_*`` value. The
#: on-chain ABI uses ``uint24``; the value ``MAX_LP_FEE`` and the
#: ``DYNAMIC_FEE_FLAG`` sentinel both fit. The position layer does
#: not enforce it at runtime because T053 may pass PoolKey fees
#: that exceed ``MAX_LP_FEE`` (the dynamic-fee sentinel); the
#: constant is exposed so a downstream consumer that needs to
#: widen the type for a non-V4 pool can fork it locally.
POOL_FEE_GROWTH_MAX_UINT256: Final[int] = (1 << 24) - 1

#: Tick-bias sentinels (the framework's standard positional labels).
#: Mirrors ``robinhood_lp.protocol.sizing`` but is re-exported so
#: callers do not have to import two modules for the same labels.
RANGE_BELOW: Final[str] = POSITION_BELOW
RANGE_INSIDE: Final[str] = POSITION_INSIDE
RANGE_ABOVE: Final[str] = POSITION_ABOVE


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RangeState(StrEnum):
    """The three Range states a position can be in at a point in time.

    Strings are part of the public contract. The values mirror the
    project-wide literals (``"below"`` / ``"inside"`` / ``"above"``)
    so a downstream consumer can compare across layers without
    a string-conversion step.

    - :attr:`BELOW` — the current tick is strictly below the
      position's ``tick_lower``; the position holds only ``token0``
      (single-sided token0 inventory).
    - :attr:`INSIDE` — the current tick is in
      ``[tick_lower, tick_upper)``; the position holds a mix of
      ``token0`` and ``token1``. (V4 uses left-inclusive /
      right-exclusive bounds for the active bracket.)
    - :attr:`ABOVE` — the current tick is at or above the
      position's ``tick_upper``; the position holds only ``token1``.
    """

    BELOW = "below"
    INSIDE = "inside"
    ABOVE = "above"


class HookEvidenceState(StrEnum):
    """Whether the framework has a verified hook-evidence pack.

    The Owner amendment for T051 requires donations / hook
    adjustments be honoured only when the T043 hook-evidence pack
    covers the pool. The position layer never reads a hook pack
    itself; the caller passes the result of T043 here.

    - :attr:`VERIFIED` — T043 supplied a verified hook pack;
      donation amounts and hook credits are added to principal.
    - :attr:`UNVERIFIED` — the pool carries nonzero hook flags but
      T043 has not verified it (or the caller's evidence source
      is unverified); donation amounts are still recorded
      separately but do **not** enter principal, and hook credit
      deltas are recorded as zero (the framework never folds an
      unverified hook delta into a balance).
    - :attr:`NO_HOOK` — the pool's hook address is zero; no hook
      can ever debit or credit the position.
    """

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    NO_HOOK = "NO_HOOK"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PositionError(ValueError):
    """Base class for T051 position failures."""


class InvalidPositionKeyError(PositionError):
    """A :class:`PositionKey` violates its invariants."""


class InvalidFeeGrowthSnapshotError(PositionError):
    """A :class:`FeeGrowthSnapshot` violates its invariants."""


class InvalidPrincipalStateError(PositionError):
    """A :class:`PrincipalState` violates its invariants."""


class InvalidLiquidityError(PositionError):
    """A liquidity is out of the V4 uint128 domain."""


class InsufficientPrincipalError(PositionError):
    """A burn / collect / remove requested more than the ledger holds."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a Python ``int`` (``bool`` rejected)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise PositionError(f"{field}: must be int, got {type(value).__name__}")
    return value


def _require_non_negative_int(value: int, *, field: str) -> int:
    """Validate ``value`` is a non-negative Python ``int``."""
    value = _require_int(value, field=field)
    if value < 0:
        raise PositionError(f"{field}: must be non-negative, got {value}")
    return value


def _require_uint128(value: int, *, field: str) -> int:
    """Validate ``value`` fits in a uint128 (V4 liquidity width)."""
    value = _require_int(value, field=field)
    if value < 0 or value >= (1 << 128):
        raise PositionError(f"{field}: must fit in uint128, got {value}")
    return value


def _require_uint256(value: int, *, field: str) -> int:
    """Validate ``value`` fits in a uint256 (V4 token amount width)."""
    value = _require_non_negative_int(value, field=field)
    if value >= (1 << 256):
        raise PositionError(f"{field}: must fit in uint256, got {value}")
    return value


def _require_int24(value: int, *, field: str) -> int:
    """Validate ``value`` fits in an int24 (V4 tick width)."""
    value = _require_int(value, field=field)
    if value < -(1 << 23) or value >= (1 << 23):
        raise PositionError(f"{field}: must fit in int24, got {value}")
    return value


# ---------------------------------------------------------------------------
# Position identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PositionKey:
    """The framework's position identity.

    V4 identifies a position on chain by
    ``keccak256(abi.encodePacked(owner, tickLower, tickUpper, salt))``
    and exposes it through a PositionManager NFT ``tokenId``. The
    ``owner`` field is not emitted on chain by the V4
    ``ModifyLiquidity`` event and the framework cannot reconstruct
    it from the ``PoolManager`` ``sender`` alone (T051 must-not).

    The framework therefore uses the
    ``(tick_lower, tick_upper, salt)`` triple the ``ModifyLiquidity``
    event carries as the canonical position identity. A position
    carrying the same triple on a different ``PoolKey`` is a
    different position; the same triple on the same ``PoolKey`` is
    the same position. When an off-chain source (e.g. a Position
    Manager indexer or an authenticated ``tokenId`` mapping)
    supplies the V4 NFT ``tokenId``, the caller may attach it as
    ``token_id`` for downstream logging — it is **never** used as
    the primary identity and **never** used to infer the owner
    wallet from the PoolManager ``sender``.

    The triple is also the key the replay layer
    (:mod:`robinhood_lp.replay.ticks`) uses; the two layers agree
    on identity so the replay's per-position state is addressable
    from the position layer by the same key.
    """

    tick_lower: int
    tick_upper: int
    salt: int
    token_id: int | None = None

    def __post_init__(self) -> None:
        _require_int24(self.tick_lower, field="PositionKey.tick_lower")
        _require_int24(self.tick_upper, field="PositionKey.tick_upper")
        _require_non_negative_int(self.salt, field="PositionKey.salt")
        if self.salt >= (1 << 256):
            raise InvalidPositionKeyError(f"PositionKey.salt={self.salt} must fit in uint256")
        if self.tick_lower >= self.tick_upper:
            raise InvalidPositionKeyError(
                f"PositionKey.tick_lower={self.tick_lower} must be strictly less than "
                f"tick_upper={self.tick_upper}"
            )
        if self.tick_lower < MIN_TICK or self.tick_lower > MAX_TICK:
            raise InvalidPositionKeyError(
                f"PositionKey.tick_lower={self.tick_lower} outside V4 [{MIN_TICK}, {MAX_TICK}]"
            )
        if self.tick_upper < MIN_TICK or self.tick_upper > MAX_TICK:
            raise InvalidPositionKeyError(
                f"PositionKey.tick_upper={self.tick_upper} outside V4 [{MIN_TICK}, {MAX_TICK}]"
            )
        if self.token_id is not None:
            _require_non_negative_int(self.token_id, field="PositionKey.token_id")
            if self.token_id >= (1 << 256):
                raise InvalidPositionKeyError(
                    f"PositionKey.token_id={self.token_id} must fit in uint256"
                )

    @property
    def event_triple(self) -> tuple[int, int, int]:
        """Return the ``(tick_lower, tick_upper, salt)`` triple.

        The triple is the on-chain identity the ``ModifyLiquidity``
        event exposes; the replay and the position layer agree on
        it. ``token_id`` is off-chain metadata and is omitted here.
        """
        return (self.tick_lower, self.tick_upper, self.salt)


# ---------------------------------------------------------------------------
# Fee-growth snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeeGrowthSnapshot:
    """A point-in-time snapshot of the V4 fee-growth accumulators.

    The snapshot holds the per-position ``fee_growth_inside_last``
    figure the V4 ``pool.modify`` settlement persists on every
    mint / burn / collect. Two snapshots are enough to compute
    the LP fees owed to a position over the interval: the fees
    owed are ``liquidity * (fee_growth_inside_now - inside_last) /
    2 ** 128``.

    The V4 settlement uses *floor* division (``mulDiv`` with
    ``roundUp == false``); the position layer mirrors it.

    The snapshot does **not** include the global accumulators or
    the tick-outside values: those are point-in-time inputs the
    caller reads from ``StateView`` (R7) at the height they value
    the position at. The snapshot only persists the
    ``fee_growth_inside_last`` the position itself "remembers".

    - ``fee_growth_inside_last_0_x128`` / ``_1_x128`` — the
      Q128-scaled ``fee_growth_inside`` the position's last
      settlement recorded. The first snapshot at mint time stores
      the *current* ``fee_growth_inside`` so the interval
      starts with zero owed fees.
    - ``tokens_owed_0`` / ``tokens_owed_1`` — the integer balance
      of uncollected fees the position is sitting on. A collect
      returns this balance and zeros it; a burn returns it as
      part of the principal settlement.
    """

    fee_growth_inside_last_0_x128: int
    fee_growth_inside_last_1_x128: int
    tokens_owed_0: int = 0
    tokens_owed_1: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.fee_growth_inside_last_0_x128, int) or isinstance(
            self.fee_growth_inside_last_0_x128, bool
        ):
            raise InvalidFeeGrowthSnapshotError(
                f"FeeGrowthSnapshot.fee_growth_inside_last_0_x128: must be int, "
                f"got {type(self.fee_growth_inside_last_0_x128).__name__}"
            )
        if self.fee_growth_inside_last_0_x128 < 0:
            raise InvalidFeeGrowthSnapshotError(
                "FeeGrowthSnapshot.fee_growth_inside_last_0_x128: must be non-negative, "
                f"got {self.fee_growth_inside_last_0_x128}"
            )
        if self.fee_growth_inside_last_0_x128 >= (1 << 256):
            raise InvalidFeeGrowthSnapshotError(
                "FeeGrowthSnapshot.fee_growth_inside_last_0_x128 "
                f"{self.fee_growth_inside_last_0_x128} must fit in uint256"
            )
        if not isinstance(self.fee_growth_inside_last_1_x128, int) or isinstance(
            self.fee_growth_inside_last_1_x128, bool
        ):
            raise InvalidFeeGrowthSnapshotError(
                f"FeeGrowthSnapshot.fee_growth_inside_last_1_x128: must be int, "
                f"got {type(self.fee_growth_inside_last_1_x128).__name__}"
            )
        if self.fee_growth_inside_last_1_x128 < 0:
            raise InvalidFeeGrowthSnapshotError(
                "FeeGrowthSnapshot.fee_growth_inside_last_1_x128: must be non-negative, "
                f"got {self.fee_growth_inside_last_1_x128}"
            )
        if self.fee_growth_inside_last_1_x128 >= (1 << 256):
            raise InvalidFeeGrowthSnapshotError(
                "FeeGrowthSnapshot.fee_growth_inside_last_1_x128 "
                f"{self.fee_growth_inside_last_1_x128} must fit in uint256"
            )
        if not isinstance(self.tokens_owed_0, int) or isinstance(self.tokens_owed_0, bool):
            raise InvalidFeeGrowthSnapshotError(
                f"FeeGrowthSnapshot.tokens_owed_0: must be int, "
                f"got {type(self.tokens_owed_0).__name__}"
            )
        if self.tokens_owed_0 < 0 or self.tokens_owed_0 >= (1 << 256):
            raise InvalidFeeGrowthSnapshotError(
                f"FeeGrowthSnapshot.tokens_owed_0={self.tokens_owed_0} must fit in uint256"
            )
        if not isinstance(self.tokens_owed_1, int) or isinstance(self.tokens_owed_1, bool):
            raise InvalidFeeGrowthSnapshotError(
                f"FeeGrowthSnapshot.tokens_owed_1: must be int, "
                f"got {type(self.tokens_owed_1).__name__}"
            )
        if self.tokens_owed_1 < 0 or self.tokens_owed_1 >= (1 << 256):
            raise InvalidFeeGrowthSnapshotError(
                f"FeeGrowthSnapshot.tokens_owed_1={self.tokens_owed_1} must fit in uint256"
            )


# ---------------------------------------------------------------------------
# Principal state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrincipalState:
    """The principal-inventory ledger of a position.

    Principal is the *balance* the position deposited at mint,
    plus any verified donation / hook-credit the framework
    attributes to the position, minus any *principal* portion a
    ``collect`` or ``burn`` returned to the wallet.

    Principal is **not** PnL (T051 must-not): a deposit is a
    balance addition, a withdrawal is a balance subtraction, and
    the difference ``principal_t0 - principal_now`` is the wallet's
    cumulative cash-flow, not a return.

    The ledger carries per-side balances so a single ``mint``,
    ``burn`` or ``collect`` only touches the side it should; the
    two sides are independent ledgers and a reversal (e.g. a
    negative ``liquidity_delta`` from a ``ModifyLiquidity``) flows
    through the same code path as a positive one.

    ``donation_amount_0`` / ``_1`` are the cumulative donation
    totals the framework has attributed to the position since
    mint. The position layer only folds them into principal when
    the pool's hook-evidence pack is ``VERIFIED`` (T043); when the
    pack is ``UNVERIFIED`` the donations are recorded but kept
    separate so the audit trail can reject them.
    """

    principal_amount_0: int = 0
    principal_amount_1: int = 0
    donation_amount_0: int = 0
    donation_amount_1: int = 0
    hook_credit_0: int = 0
    hook_credit_1: int = 0

    def __post_init__(self) -> None:
        for name in (
            "principal_amount_0",
            "principal_amount_1",
            "donation_amount_0",
            "donation_amount_1",
            "hook_credit_0",
            "hook_credit_1",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise InvalidPrincipalStateError(
                    f"PrincipalState.{name}: must be int, got {type(value).__name__}"
                )
            if value < 0 or value >= (1 << 256):
                raise InvalidPrincipalStateError(
                    f"PrincipalState.{name}={value} must fit in uint256"
                )


# ---------------------------------------------------------------------------
# Pool fee inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolFeeState:
    """The pool's fee state at a point in time.

    Two inputs are needed when the caller feeds combined-fee
    growth values; one (``protocol_fee_token0`` /
    ``protocol_fee_token1``) is needed when the caller already
    has LP-only fee growth.

    - ``combined_swap_fee_0`` / ``_1`` — the V4 PoolKey ``fee``
      the pool emits when the current tick is in
      ``[tick_lower, tick_upper)``. The value is in hundredths
      of a bip and is the *combined* LP + protocol swap fee the
      pool charged on the in-range swaps. The LP portion is
      ``combined - protocol``; the protocol portion is the
      ``protocol_fee_*`` half T040 replays.
    - ``protocol_fee_token0`` / ``_1`` — the directional
      12-bit half T040 records. Each half is a 12-bit unsigned
      integer in ``[0, 4095]``. When the input fee-growth values
      are already LP-only the protocol-fee fields are unused and
      may stay zero.
    - ``is_lp_fee_growth`` — ``True`` when the caller's
      ``fee_growth_*_x128`` inputs are already LP-only (the V4
      ``feeGrowthGlobal`` accumulation). ``False`` when the
      inputs are combined and the framework must split. The
      default ``True`` matches the V4 design (``feeGrowthGlobal``
      is LP-only) and matches the T042 StateView comparison
      runner.
    """

    combined_swap_fee_0: int = 0
    combined_swap_fee_1: int = 0
    protocol_fee_token0: int = 0
    protocol_fee_token1: int = 0
    is_lp_fee_growth: bool = True

    def __post_init__(self) -> None:
        for name in ("combined_swap_fee_0", "combined_swap_fee_1"):
            value = getattr(self, name)
            _require_non_negative_int(value, field=f"PoolFeeState.{name}")
        for name in ("protocol_fee_token0", "protocol_fee_token1"):
            value = getattr(self, name)
            if value < 0 or value > 0xFFF:
                raise PositionError(f"PoolFeeState.{name}={value} must fit in 12 bits (max 4095)")
        if not isinstance(self.is_lp_fee_growth, bool):
            raise PositionError(
                f"PoolFeeState.is_lp_fee_growth: must be bool, "
                f"got {type(self.is_lp_fee_growth).__name__}"
            )


# ---------------------------------------------------------------------------
# Point-in-time pool state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolState:
    """The pool's point-in-time state the position layer consumes.

    Every field is a non-negative integer read from a V4
    ``StateView`` call (R7) or replayed from the per-pool event
    stream (T040 / T041 / T042):

    - ``sqrt_price_x96`` — current ``sqrtPriceX96``. uint160.
    - ``tick`` — current tick (``slot0.tick``). int24.
    - ``fee_growth_global_0_x128`` / ``_1_x128`` — the
      Q128-scaled global fee-growth accumulators
      (``StateView.getFeeGrowthGlobals``). uint256.
    - ``fee_growth_outside_lower_0_x128`` / ``_1_x128`` —
      ``StateView.getTickFeeGrowthOutside(tick_lower)``. uint256.
    - ``fee_growth_outside_upper_0_x128`` / ``_1_x128`` —
      ``StateView.getTickFeeGrowthOutside(tick_upper)``. uint256.

    The tick's ``feeGrowthOutside`` flag bits (V4 ``TickInfo``) are
    folded into the ``fee_growth_outside_*`` values upstream of
    this layer (the StateView read returns the *active* outside
    value, already adjusted for the uninitialised-bit flip the
    V4 reference performs on cross). The position layer therefore
    receives the values the V4 ``getFeeGrowthInside`` returns
    inside the bracket — the caller does the StateView flip
    calculation once and passes the resolved outside values
    here.
    """

    sqrt_price_x96: int
    tick: int
    fee_growth_global_0_x128: int
    fee_growth_global_1_x128: int
    fee_growth_outside_lower_0_x128: int
    fee_growth_outside_lower_1_x128: int
    fee_growth_outside_upper_0_x128: int
    fee_growth_outside_upper_1_x128: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.sqrt_price_x96, field="PoolState.sqrt_price_x96")
        if self.sqrt_price_x96 >= (1 << 160):
            raise PositionError(
                f"PoolState.sqrt_price_x96={self.sqrt_price_x96} must fit in uint160"
            )
        _require_int24(self.tick, field="PoolState.tick")
        for name in (
            "fee_growth_global_0_x128",
            "fee_growth_global_1_x128",
            "fee_growth_outside_lower_0_x128",
            "fee_growth_outside_lower_1_x128",
            "fee_growth_outside_upper_0_x128",
            "fee_growth_outside_upper_1_x128",
        ):
            value = getattr(self, name)
            _require_non_negative_int(value, field=f"PoolState.{name}")
            if value >= (1 << 256):
                raise PositionError(f"PoolState.{name}={value} must fit in uint256")


# ---------------------------------------------------------------------------
# The valuation output
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PositionValuation:
    """The canonical position valuation at a point in time.

    Two equal valuations are byte-identical: dataclass equality
    covers every field and hashing follows dataclass identity. A
    downstream consumer (T052 attribution, T070 risk, T101 panel)
    consumes the record by name and never reconstructs its shape
    ad hoc.

    Per-side fields are split so a consumer can render token0
    and token1 independently. The ``unit`` is the same
    :class:`ObservationUnit.RAW_TOKEN_INTEGER` the other feature
    rows use, so a downstream aggregator can compare a position
    valuation against a swap observation without a unit-conversion
    step.

    The valuation carries every piece the contract asks for:

    - ``raw_amount_0`` / ``raw_amount_1`` — the integer token
      amounts the position holds at the current price, computed
      from ``liquidity`` and ``[tick_lower, tick_upper]`` at the
      V4 ``sqrt_price_x96``. The "raw" prefix mirrors the
      ``RAW_TOKEN_INTEGER`` unit and reminds the consumer the
      amount is *atomic*, not decimal.
    - ``range_state`` — :class:`RangeState` (``BELOW`` /
      ``INSIDE`` / ``ABOVE``).
    - ``principal_amount_0`` / ``_1`` — the per-side principal
      ledger (see :class:`PrincipalState`).
    - ``earned_fees_0`` / ``_1`` — the LP-owned fees the
      position has earned since the last ``collect`` (or mint),
      after the protocol-fee separation. Floored with V4's
      ``mulDiv``.
    - ``protocol_fees_0`` / ``_1`` — the separated protocol-fee
      share the Owner amendment of 2026-09-16 records. Zero when
      the input fee-growth values are already LP-only.
    - ``donation_amount_0`` / ``_1`` — the cumulative donation
      totals attributed to the position. Recorded but **not**
      folded into principal unless the T043 hook-evidence pack
      is ``VERIFIED``.
    - ``hook_credit_0`` / ``_1`` — the verified hook credit
      deltas. Zero unless the T043 hook-evidence pack is
      ``VERIFIED``.
    - ``hook_evidence_state`` — the verification state the
      caller passed in; the audit trail can reconstruct the
      exact decision.
    - ``version`` — :data:`POSITION_VALUATION_VERSION`.
    - ``unit`` — :attr:`ObservationUnit.RAW_TOKEN_INTEGER`.
    """

    position_key: PositionKey
    liquidity: int
    pool_state: PoolState
    pool_fee_state: PoolFeeState
    range_state: RangeState
    raw_amount_0: int
    raw_amount_1: int
    principal_amount_0: int
    principal_amount_1: int
    earned_fees_0: int
    earned_fees_1: int
    protocol_fees_0: int
    protocol_fees_1: int
    donation_amount_0: int
    donation_amount_1: int
    hook_credit_0: int
    hook_credit_1: int
    fee_growth_inside_0_x128: int
    fee_growth_inside_1_x128: int
    hook_evidence_state: HookEvidenceState
    unit: ObservationUnit
    version: str

    def __post_init__(self) -> None:
        if not isinstance(self.position_key, PositionKey):
            raise PositionError(
                f"PositionValuation.position_key: must be PositionKey, "
                f"got {type(self.position_key).__name__}"
            )
        _require_uint128(self.liquidity, field="PositionValuation.liquidity")
        if not isinstance(self.pool_state, PoolState):
            raise PositionError(
                f"PositionValuation.pool_state: must be PoolState, "
                f"got {type(self.pool_state).__name__}"
            )
        if not isinstance(self.pool_fee_state, PoolFeeState):
            raise PositionError(
                f"PositionValuation.pool_fee_state: must be PoolFeeState, "
                f"got {type(self.pool_fee_state).__name__}"
            )
        if not isinstance(self.range_state, RangeState):
            raise PositionError(
                f"PositionValuation.range_state: must be RangeState, "
                f"got {type(self.range_state).__name__}"
            )
        for name in (
            "raw_amount_0",
            "raw_amount_1",
            "principal_amount_0",
            "principal_amount_1",
            "earned_fees_0",
            "earned_fees_1",
            "protocol_fees_0",
            "protocol_fees_1",
            "donation_amount_0",
            "donation_amount_1",
            "hook_credit_0",
            "hook_credit_1",
            "fee_growth_inside_0_x128",
            "fee_growth_inside_1_x128",
        ):
            value = getattr(self, name)
            _require_non_negative_int(value, field=f"PositionValuation.{name}")
            if value >= (1 << 256):
                raise PositionError(f"PositionValuation.{name}={value} must fit in uint256")
        if not isinstance(self.hook_evidence_state, HookEvidenceState):
            raise PositionError(
                f"PositionValuation.hook_evidence_state: must be HookEvidenceState, "
                f"got {type(self.hook_evidence_state).__name__}"
            )
        if not isinstance(self.unit, ObservationUnit):
            raise PositionError(
                f"PositionValuation.unit: must be ObservationUnit, got {type(self.unit).__name__}"
            )
        if self.unit is not ObservationUnit.RAW_TOKEN_INTEGER:
            raise PositionError(
                f"PositionValuation.unit: must be RAW_TOKEN_INTEGER, got {self.unit!r}"
            )
        if not isinstance(self.version, str) or not self.version:
            raise PositionError("PositionValuation.version: must be non-empty str")


# ---------------------------------------------------------------------------
# Range classification
# ---------------------------------------------------------------------------


def classify_range_state(
    *,
    current_tick: int,
    tick_lower: int,
    tick_upper: int,
) -> RangeState:
    """Classify the position's Range state at ``current_tick``.

    The V4 active bracket is ``[tick_lower, tick_upper)``:

    - ``current_tick < tick_lower`` → :attr:`RangeState.BELOW`.
    - ``tick_lower <= current_tick < tick_upper`` →
      :attr:`RangeState.INSIDE`.
    - ``current_tick >= tick_upper`` → :attr:`RangeState.ABOVE`.

    The function is pure: same inputs produce the same state in
    any order or process. ``bool`` is rejected at every int
    parameter; the V4 int24 width is enforced.
    """
    _require_int24(current_tick, field="classify_range_state.current_tick")
    _require_int24(tick_lower, field="classify_range_state.tick_lower")
    _require_int24(tick_upper, field="classify_range_state.tick_upper")
    if tick_lower >= tick_upper:
        raise PositionError(
            f"classify_range_state: tick_lower={tick_lower} must be strictly less "
            f"than tick_upper={tick_upper}"
        )
    if current_tick < tick_lower:
        return RangeState.BELOW
    if current_tick >= tick_upper:
        return RangeState.ABOVE
    return RangeState.INSIDE


# ---------------------------------------------------------------------------
# Fee-growth inside derivation (V4 standard formula)
# ---------------------------------------------------------------------------


def compute_fee_growth_inside(
    *,
    current_tick: int,
    tick_lower: int,
    tick_upper: int,
    fee_growth_global_x128: int,
    fee_growth_outside_lower_x128: int,
    fee_growth_outside_upper_x128: int,
) -> int:
    """Return the V4 ``fee_growth_inside`` for one token side.

    The formula matches ``Pool.getFeeGrowthInside``:

    - when ``current_tick < tick_lower`` (below the Range), only
      the lower tick's outside growth contributes;
    - when ``current_tick >= tick_upper`` (above the Range), only
      the upper tick's outside growth contributes;
    - otherwise (inside the Range), the inside growth is
      ``global - lower_outside - upper_outside`` (underflow
      saturates at zero per the V4 reference).

    The caller has already resolved the tick's
    ``feeGrowthOutside`` flag (V4 flips the stored value when the
    tick is uninitialised). The position layer takes the resolved
    outside value as input and applies the standard V4
    conditional.

    All inputs are non-negative Q128 values; the result is
    non-negative. No ``float`` enters this function (ADR-004).
    """
    _require_int24(current_tick, field="compute_fee_growth_inside.current_tick")
    _require_int24(tick_lower, field="compute_fee_growth_inside.tick_lower")
    _require_int24(tick_upper, field="compute_fee_growth_inside.tick_upper")
    _require_non_negative_int(
        fee_growth_global_x128, field="compute_fee_growth_inside.fee_growth_global_x128"
    )
    _require_non_negative_int(
        fee_growth_outside_lower_x128,
        field="compute_fee_growth_inside.fee_growth_outside_lower_x128",
    )
    _require_non_negative_int(
        fee_growth_outside_upper_x128,
        field="compute_fee_growth_inside.fee_growth_outside_upper_x128",
    )
    if tick_lower >= tick_upper:
        raise PositionError(
            f"compute_fee_growth_inside: tick_lower={tick_lower} must be strictly "
            f"less than tick_upper={tick_upper}"
        )
    if current_tick < tick_lower:
        # Below: subtract only the lower outside. The V4 conditional
        # inverts the upper outside on the way out so the
        # global - lower - upper collapse to global - lower here.
        diff = fee_growth_global_x128 - fee_growth_outside_lower_x128
        return diff if diff > 0 else 0
    if current_tick >= tick_upper:
        # Above: subtract only the upper outside.
        diff = fee_growth_global_x128 - fee_growth_outside_upper_x128
        return diff if diff > 0 else 0
    # Inside: subtract both outsides.
    diff = fee_growth_global_x128 - fee_growth_outside_lower_x128 - fee_growth_outside_upper_x128
    return diff if diff > 0 else 0


# ---------------------------------------------------------------------------
# Protocol-fee split
# ---------------------------------------------------------------------------


def split_protocol_fees(
    *,
    earned_combined_0: int,
    earned_combined_1: int,
    pool_fee_state: PoolFeeState,
) -> tuple[int, int, int, int]:
    """Split a combined-fee ``earned_*`` into LP-owned and protocol shares.

    Returns ``(earned_lp_0, earned_lp_1, protocol_0, protocol_1)``.

    When ``PoolFeeState.is_lp_fee_growth`` is ``True`` the inputs
    are already LP-only and the protocol-fee fields are zero
    (``earned_lp_* == earned_combined_*``).

    When ``PoolFeeState.is_lp_fee_growth`` is ``False`` the inputs
    are combined and the framework scales them by
    ``(lp_fee - protocol_fee) / lp_fee`` per token, where
    ``lp_fee`` is the combined swap fee and ``protocol_fee`` is
    the directional 12-bit half T040 replays. The split is the
    integer ``(earned_combined * (lp - protocol)) // lp`` and the
    rounding is *floor*; the protocol-fee share is
    ``earned_combined - earned_lp`` so the two sides sum to the
    combined total byte-exactly (no rounding loss across the
    split).

    The function is the T051 application of the Owner amendment
    of 2026-09-16: LP fees exclude the protocol share represented
    by T040's point-in-time directional protocol-fee state.

    Edge cases:

    - ``combined_swap_fee_* == 0``: the only valid output is
      ``earned_lp_* = 0, protocol_* = 0`` regardless of
      ``is_lp_fee_growth``; a zero combined fee implies a zero
      earned combined fee.
    - ``combined_swap_fee_* > 0`` but
      ``combined_swap_fee_* <= protocol_fee_*``: the protocol fee
      *cannot* exceed the combined fee (the V4 wire format would
      have rejected it). When ``is_lp_fee_growth is False`` the
      function treats the LP share as zero and the protocol share
      as ``earned_combined``; the V4 wire guarantee makes this a
      degenerate case rather than a normal operating point.
    """
    if not isinstance(pool_fee_state, PoolFeeState):
        raise PositionError(
            f"split_protocol_fees.pool_fee_state: must be PoolFeeState, "
            f"got {type(pool_fee_state).__name__}"
        )
    _require_non_negative_int(earned_combined_0, field="split_protocol_fees.earned_combined_0")
    _require_non_negative_int(earned_combined_1, field="split_protocol_fees.earned_combined_1")
    if earned_combined_0 >= (1 << 256) or earned_combined_1 >= (1 << 256):
        raise PositionError("split_protocol_fees: earned combined must fit in uint256")

    if pool_fee_state.is_lp_fee_growth:
        return (
            earned_combined_0,
            earned_combined_1,
            0,
            0,
        )

    # Combined-fee path: scale by (lp - protocol) / lp per side.
    earned_lp_0, protocol_0 = _split_one_side(
        earned_combined=earned_combined_0,
        combined_swap_fee=pool_fee_state.combined_swap_fee_0,
        protocol_fee=pool_fee_state.protocol_fee_token0,
    )
    earned_lp_1, protocol_1 = _split_one_side(
        earned_combined=earned_combined_1,
        combined_swap_fee=pool_fee_state.combined_swap_fee_1,
        protocol_fee=pool_fee_state.protocol_fee_token1,
    )
    return (earned_lp_0, earned_lp_1, protocol_0, protocol_1)


def _split_one_side(
    *,
    earned_combined: int,
    combined_swap_fee: int,
    protocol_fee: int,
) -> tuple[int, int]:
    """Split one side's combined fee into LP-owned and protocol share."""
    if earned_combined == 0 or combined_swap_fee == 0:
        return (0, 0)
    if protocol_fee >= combined_swap_fee:
        # Degenerate: protocol cannot exceed combined. The LP
        # share is zero and the protocol absorbs the entire
        # earned figure. V4 wire-format rejection makes this a
        # silent fallback rather than a normal case.
        return (0, earned_combined)
    lp_share = (earned_combined * (combined_swap_fee - protocol_fee)) // combined_swap_fee
    return (lp_share, earned_combined - lp_share)


# ---------------------------------------------------------------------------
# LP fees owed (the V4 mulDiv)
# ---------------------------------------------------------------------------


def compute_lp_fees_owed(
    *,
    liquidity: int,
    fee_growth_inside_now_x128: int,
    fee_growth_inside_last_x128: int,
) -> int:
    """Return the integer tokens owed since the last settlement.

    Mirrors the V4 ``mulDiv(liquidity, delta, Q128)`` settlement:

    - ``delta = fee_growth_inside_now - fee_growth_inside_last``
      (clamped at zero when the pool was reset; in practice
      ``fee_growth_inside`` is monotonically non-decreasing);
    - ``tokens_owed = (liquidity * delta) >> 128`` (floor).

    The result is always a non-negative integer. A position with
    ``liquidity == 0`` has nothing owed. The function is the
    per-token-side component of V4's ``pool.modify`` owed-fees
    settlement.
    """
    _require_uint128(liquidity, field="compute_lp_fees_owed.liquidity")
    _require_non_negative_int(
        fee_growth_inside_now_x128,
        field="compute_lp_fees_owed.fee_growth_inside_now_x128",
    )
    _require_non_negative_int(
        fee_growth_inside_last_x128,
        field="compute_lp_fees_owed.fee_growth_inside_last_x128",
    )
    if fee_growth_inside_now_x128 >= (1 << 256) or fee_growth_inside_last_x128 >= (1 << 256):
        raise PositionError("compute_lp_fees_owed: fee_growth_inside must fit in uint256")
    delta = fee_growth_inside_now_x128 - fee_growth_inside_last_x128
    if delta <= 0:
        return 0
    return (liquidity * delta) >> 128


# ---------------------------------------------------------------------------
# Raw inventory at a price
# ---------------------------------------------------------------------------


def compute_raw_inventory(
    *,
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
) -> tuple[int, int]:
    """Return the integer ``(amount0, amount1)`` the position holds.

    Delegates to the T049 sizer's
    :func:`robinhood_lp.protocol.sizing.get_amounts_for_liquidity`
    so the V4 rounding (floor on both sides) is the single
    source of truth. The function is exposed under the
    feature-layer name because the position layer is what reads
    it; the protocol layer's function is the underlying engine.

    The caller may also ask for the inventory at the V4 Range
    boundaries directly — see
    :func:`compute_raw_inventory_at_lower` /
    :func:`compute_raw_inventory_at_upper`. The boundary cases
    are the worst-case single-sided inventory T070 uses to cap
    exposure.
    """
    _require_int24(tick_lower, field="compute_raw_inventory.tick_lower")
    _require_int24(tick_upper, field="compute_raw_inventory.tick_upper")
    _require_uint128(liquidity, field="compute_raw_inventory.liquidity")
    if tick_lower >= tick_upper:
        raise PositionError(
            f"compute_raw_inventory: tick_lower={tick_lower} must be strictly "
            f"less than tick_upper={tick_upper}"
        )
    return get_amounts_for_liquidity(
        sqrt_price_x96=sqrt_price_x96,
        sqrt_price_a_x96=get_sqrt_price_at_tick(tick_lower),
        sqrt_price_b_x96=get_sqrt_price_at_tick(tick_upper),
        liquidity=liquidity,
    )


def compute_raw_inventory_at_lower(
    *,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
) -> tuple[int, int]:
    """Return the inventory when ``current_tick == tick_lower``.

    At the lower bound the position is at the boundary between
    ``BELOW`` and ``INSIDE``; the V4 bracket is
    ``[pa, pb)`` so the boundary is the all-token1 limit
    (``amount0 == 0, amount1 == get_amount1_delta(pa, pb, L)``).
    """
    _require_int24(tick_lower, field="compute_raw_inventory_at_lower.tick_lower")
    _require_int24(tick_upper, field="compute_raw_inventory_at_upper.tick_upper")
    _require_uint128(liquidity, field="compute_raw_inventory_at_lower.liquidity")
    if tick_lower >= tick_upper:
        raise PositionError(
            f"compute_raw_inventory_at_lower: tick_lower={tick_lower} must be "
            f"strictly less than tick_upper={tick_upper}"
        )
    pa = get_sqrt_price_at_tick(tick_lower)
    pb = get_sqrt_price_at_tick(tick_upper)
    return (
        0,
        get_amount1_delta(pa, pb, liquidity, round_up=False),
    )


def compute_raw_inventory_at_upper(
    *,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
) -> tuple[int, int]:
    """Return the inventory when ``current_tick == tick_upper``.

    At the upper bound the V4 bracket is closed (``[pa, pb]``) and
    the position is at the boundary between ``INSIDE`` and
    ``ABOVE``. The T051 acceptance treats the upper boundary as
    the "all token0" limit
    (``amount0 == get_amount0_delta(pa, pb, L)``,
    ``amount1 == 0``). The V4 ``get_amounts_for_liquidity`` keeps
    the bracket half-open, so the position layer rounds the upper
    boundary to the all-token0 figure directly.
    """
    _require_int24(tick_lower, field="compute_raw_inventory_at_upper.tick_lower")
    _require_int24(tick_upper, field="compute_raw_inventory_at_upper.tick_upper")
    _require_uint128(liquidity, field="compute_raw_inventory_at_upper.liquidity")
    if tick_lower >= tick_upper:
        raise PositionError(
            f"compute_raw_inventory_at_upper: tick_lower={tick_lower} must be "
            f"strictly less than tick_upper={tick_upper}"
        )
    pa = get_sqrt_price_at_tick(tick_lower)
    pb = get_sqrt_price_at_tick(tick_upper)
    return (
        get_amount0_delta(pa, pb, liquidity, round_up=False),
        0,
    )


# ---------------------------------------------------------------------------
# Snapshot lifecycle
# ---------------------------------------------------------------------------


def snapshot_at_mint(
    *,
    fee_growth_inside_0_x128: int,
    fee_growth_inside_1_x128: int,
) -> FeeGrowthSnapshot:
    """Return the snapshot a fresh position takes at mint time.

    The first snapshot stores the *current* inside growth so the
    interval starts with zero owed fees. The tokens-owed balance
    starts at zero — a mint is principal in, not earned fees in.
    """
    return FeeGrowthSnapshot(
        fee_growth_inside_last_0_x128=fee_growth_inside_0_x128,
        fee_growth_inside_last_1_x128=fee_growth_inside_1_x128,
        tokens_owed_0=0,
        tokens_owed_1=0,
    )


def snapshot_after_modify(
    *,
    snapshot: FeeGrowthSnapshot,
    liquidity: int,
    fee_growth_inside_0_x128: int,
    fee_growth_inside_1_x128: int,
) -> FeeGrowthSnapshot:
    """Apply a V4 ``pool.modify`` settlement to ``snapshot``.

    The settlement the V4 reference performs on every
    ``pool.modify`` (mint, burn, add, remove) is:

    - compute the LP fees owed since the snapshot's
      ``fee_growth_inside_last``;
    - add the fees to ``tokens_owed``;
    - update the snapshot's ``fee_growth_inside_last`` to the
      current inside growth.

    The function returns a new :class:`FeeGrowthSnapshot`; it
    never mutates the input. A position with ``liquidity == 0``
    is a degenerate post-modify state — the V4 reference
    permits ``liquidity == 0`` only after a full burn, and the
    fees owed at that moment are zero. The function does not
    raise on zero liquidity; it returns the snapshot unchanged
    in fees and updates ``fee_growth_inside_last`` so the next
    mint starts from the same baseline.
    """
    if not isinstance(snapshot, FeeGrowthSnapshot):
        raise PositionError(
            f"snapshot_after_modify.snapshot: must be FeeGrowthSnapshot, "
            f"got {type(snapshot).__name__}"
        )
    _require_uint128(liquidity, field="snapshot_after_modify.liquidity")
    earned_0 = compute_lp_fees_owed(
        liquidity=liquidity,
        fee_growth_inside_now_x128=fee_growth_inside_0_x128,
        fee_growth_inside_last_x128=snapshot.fee_growth_inside_last_0_x128,
    )
    earned_1 = compute_lp_fees_owed(
        liquidity=liquidity,
        fee_growth_inside_now_x128=fee_growth_inside_1_x128,
        fee_growth_inside_last_x128=snapshot.fee_growth_inside_last_1_x128,
    )
    return FeeGrowthSnapshot(
        fee_growth_inside_last_0_x128=fee_growth_inside_0_x128,
        fee_growth_inside_last_1_x128=fee_growth_inside_1_x128,
        tokens_owed_0=snapshot.tokens_owed_0 + earned_0,
        tokens_owed_1=snapshot.tokens_owed_1 + earned_1,
    )


def apply_collect(
    *,
    snapshot: FeeGrowthSnapshot,
) -> tuple[FeeGrowthSnapshot, int, int]:
    """Apply a ``collect`` to ``snapshot``.

    Returns ``(new_snapshot, collected_0, collected_1)``. The
    collected amounts are the uncollected ``tokens_owed`` the
    snapshot is sitting on; the new snapshot's
    ``tokens_owed_0`` / ``_1`` are zero. Principal is **not**
    touched (collecting fees is a fees-out flow, not a
    principal-out flow).

    A ``collect`` is fees only — never principal. Calling
    :func:`apply_collect` returns the owed-fees balance and
    zeroes the balance; the caller that wants to withdraw
    principal too must call :func:`apply_burn` instead, which
    combines the owed fees and the principal settlement into
    one step.
    """
    if not isinstance(snapshot, FeeGrowthSnapshot):
        raise PositionError(
            f"apply_collect.snapshot: must be FeeGrowthSnapshot, got {type(snapshot).__name__}"
        )
    return (
        FeeGrowthSnapshot(
            fee_growth_inside_last_0_x128=snapshot.fee_growth_inside_last_0_x128,
            fee_growth_inside_last_1_x128=snapshot.fee_growth_inside_last_1_x128,
            tokens_owed_0=0,
            tokens_owed_1=0,
        ),
        snapshot.tokens_owed_0,
        snapshot.tokens_owed_1,
    )


def apply_burn(
    *,
    snapshot: FeeGrowthSnapshot,
    principal: PrincipalState,
    liquidity: int,
    fee_growth_inside_0_x128: int,
    fee_growth_inside_1_x128: int,
) -> tuple[FeeGrowthSnapshot, PrincipalState, int, int, int, int]:
    """Apply a full burn (the V4 ``pool.modify`` with negative delta).

    Returns ``(new_snapshot, new_principal, returned_0, returned_1,
    settled_fees_0, settled_fees_1)``.

    A full burn returns the principal and the owed fees in one
    step:

    - ``settled_fees_*`` is the V4 mulDiv on the delta inside
      growth since the last settlement;
    - ``returned_*`` is the principal balance the position
      carried (the per-side ledger at burn time);
    - ``new_snapshot`` zeroes the owed-fees balance and updates
      ``fee_growth_inside_last`` so the next mint starts from the
      current inside growth;
    - ``new_principal`` zeroes the ledger.

    Partial burns (a burn that does not exhaust the position) are
    not modelled here: the position layer records the canonical
    *full* lifecycle, where a partial burn is followed by an
    ``add`` that returns to a positive liquidity. The caller that
    needs a partial burn records a proportional principal
    reduction before the burn.
    """
    if not isinstance(snapshot, FeeGrowthSnapshot):
        raise PositionError(
            f"apply_burn.snapshot: must be FeeGrowthSnapshot, got {type(snapshot).__name__}"
        )
    if not isinstance(principal, PrincipalState):
        raise PositionError(
            f"apply_burn.principal: must be PrincipalState, got {type(principal).__name__}"
        )
    _require_uint128(liquidity, field="apply_burn.liquidity")
    if liquidity == 0:
        raise PositionError("apply_burn: liquidity must be positive (a burn zeroes the position)")
    if principal.principal_amount_0 == 0 and principal.principal_amount_1 == 0:
        raise PositionError("apply_burn: principal ledger is empty")
    settled_fees_0 = compute_lp_fees_owed(
        liquidity=liquidity,
        fee_growth_inside_now_x128=fee_growth_inside_0_x128,
        fee_growth_inside_last_x128=snapshot.fee_growth_inside_last_0_x128,
    )
    settled_fees_1 = compute_lp_fees_owed(
        liquidity=liquidity,
        fee_growth_inside_now_x128=fee_growth_inside_1_x128,
        fee_growth_inside_last_x128=snapshot.fee_growth_inside_last_1_x128,
    )
    new_snapshot = FeeGrowthSnapshot(
        fee_growth_inside_last_0_x128=fee_growth_inside_0_x128,
        fee_growth_inside_last_1_x128=fee_growth_inside_1_x128,
        tokens_owed_0=0,
        tokens_owed_1=0,
    )
    new_principal = PrincipalState(
        principal_amount_0=0,
        principal_amount_1=0,
        donation_amount_0=principal.donation_amount_0,
        donation_amount_1=principal.donation_amount_1,
        hook_credit_0=principal.hook_credit_0,
        hook_credit_1=principal.hook_credit_1,
    )
    return (
        new_snapshot,
        new_principal,
        principal.principal_amount_0,
        principal.principal_amount_1,
        settled_fees_0,
        settled_fees_1,
    )


def apply_mint(
    *,
    snapshot: FeeGrowthSnapshot,
    principal: PrincipalState,
    amount_0: int,
    amount_1: int,
    fee_growth_inside_0_x128: int,
    fee_growth_inside_1_x128: int,
) -> tuple[FeeGrowthSnapshot, PrincipalState]:
    """Apply a mint (the V4 ``pool.modify`` with positive delta).

    Returns ``(new_snapshot, new_principal)``. The mint:

    - settles any outstanding earned fees (the V4 reference
      performs this on every ``pool.modify``);
    - adds ``amount_0`` and ``amount_1`` to the principal ledger;
    - updates ``fee_growth_inside_last`` to the current inside
      growth so the next interval starts from the new baseline.

    A mint of zero on both sides is rejected: the V4 reference
    would revert ``pool.modify`` with a zero liquidity delta, so
    the position layer mirrors the rejection.

    When the snapshot carries uncollected ``tokens_owed_*`` from a
    prior interval the caller must settle them first (via
    :func:`apply_collect` or :func:`snapshot_after_modify`). The
    position layer refuses the mint in that case so the audit
    trail never silently drops owed fees.
    """
    if not isinstance(snapshot, FeeGrowthSnapshot):
        raise PositionError(
            f"apply_mint.snapshot: must be FeeGrowthSnapshot, got {type(snapshot).__name__}"
        )
    if not isinstance(principal, PrincipalState):
        raise PositionError(
            f"apply_mint.principal: must be PrincipalState, got {type(principal).__name__}"
        )
    _require_uint256(amount_0, field="apply_mint.amount_0")
    _require_uint256(amount_1, field="apply_mint.amount_1")
    if amount_0 == 0 and amount_1 == 0:
        raise PositionError("apply_mint: at least one of amount_0, amount_1 must be positive")
    if snapshot.tokens_owed_0 > 0 or snapshot.tokens_owed_1 > 0:
        raise PositionError(
            "apply_mint: snapshot has uncollected tokens_owed; "
            "call apply_collect or snapshot_after_modify first"
        )
    new_snapshot = FeeGrowthSnapshot(
        fee_growth_inside_last_0_x128=fee_growth_inside_0_x128,
        fee_growth_inside_last_1_x128=fee_growth_inside_1_x128,
        tokens_owed_0=0,
        tokens_owed_1=0,
    )
    new_principal = PrincipalState(
        principal_amount_0=principal.principal_amount_0 + amount_0,
        principal_amount_1=principal.principal_amount_1 + amount_1,
        donation_amount_0=principal.donation_amount_0,
        donation_amount_1=principal.donation_amount_1,
        hook_credit_0=principal.hook_credit_0,
        hook_credit_1=principal.hook_credit_1,
    )
    return (new_snapshot, new_principal)


def apply_donation(
    *,
    principal: PrincipalState,
    amount_0: int,
    amount_1: int,
    hook_evidence_state: HookEvidenceState,
) -> PrincipalState:
    """Record a donation (V4 ``Donate`` event) on the position.

    Per V4 the ``Donate`` event credits the in-range liquidity
    pro-rata without changing the bitmap, ticks, or active
    liquidity. The amount credited to a position is
    ``donation * liquidity / active_liquidity``; the position
    layer records the *full* donation the framework attributes
    to the position (the caller has already done the pro-rata
    split upstream) and folds it into principal only when the
    T043 hook-evidence pack is :attr:`HookEvidenceState.VERIFIED`
    or the pool carries no hook (:attr:`HookEvidenceState.NO_HOOK`).

    When the pool carries nonzero hook flags and T043 has not
    verified the hook, the donations are recorded but kept
    separate (``donation_amount_*``); the audit trail records
    ``UNVERIFIED`` so the verification gate is never silently
    relaxed.
    """
    if not isinstance(principal, PrincipalState):
        raise PositionError(
            f"apply_donation.principal: must be PrincipalState, got {type(principal).__name__}"
        )
    _require_uint256(amount_0, field="apply_donation.amount_0")
    _require_uint256(amount_1, field="apply_donation.amount_1")
    if amount_0 == 0 and amount_1 == 0:
        raise PositionError("apply_donation: at least one of amount_0, amount_1 must be positive")
    if not isinstance(hook_evidence_state, HookEvidenceState):
        raise PositionError(
            f"apply_donation.hook_evidence_state: must be HookEvidenceState, "
            f"got {type(hook_evidence_state).__name__}"
        )
    if hook_evidence_state is HookEvidenceState.VERIFIED:
        return PrincipalState(
            principal_amount_0=principal.principal_amount_0 + amount_0,
            principal_amount_1=principal.principal_amount_1 + amount_1,
            donation_amount_0=principal.donation_amount_0 + amount_0,
            donation_amount_1=principal.donation_amount_1 + amount_1,
            hook_credit_0=principal.hook_credit_0,
            hook_credit_1=principal.hook_credit_1,
        )
    if hook_evidence_state is HookEvidenceState.NO_HOOK:
        return PrincipalState(
            principal_amount_0=principal.principal_amount_0 + amount_0,
            principal_amount_1=principal.principal_amount_1 + amount_1,
            donation_amount_0=principal.donation_amount_0 + amount_0,
            donation_amount_1=principal.donation_amount_1 + amount_1,
            hook_credit_0=principal.hook_credit_0,
            hook_credit_1=principal.hook_credit_1,
        )
    # UNVERIFIED: record the donation but keep it separate from
    # principal. The audit trail can reject the credit downstream.
    return PrincipalState(
        principal_amount_0=principal.principal_amount_0,
        principal_amount_1=principal.principal_amount_1,
        donation_amount_0=principal.donation_amount_0 + amount_0,
        donation_amount_1=principal.donation_amount_1 + amount_1,
        hook_credit_0=principal.hook_credit_0,
        hook_credit_1=principal.hook_credit_1,
    )


def apply_hook_credit(
    *,
    principal: PrincipalState,
    amount_0: int,
    amount_1: int,
    hook_evidence_state: HookEvidenceState,
) -> PrincipalState:
    """Record a verified hook credit on the position.

    When the T043 hook-evidence pack is :attr:`HookEvidenceState.VERIFIED`
    the hook credit is added to the principal ledger and recorded
    separately in ``hook_credit_*``; otherwise the credit is
    rejected — an unverified hook credit is never silently folded
    into a balance (T051 must-not + T043 must-not).
    """
    if not isinstance(principal, PrincipalState):
        raise PositionError(
            f"apply_hook_credit.principal: must be PrincipalState, got {type(principal).__name__}"
        )
    _require_uint256(amount_0, field="apply_hook_credit.amount_0")
    _require_uint256(amount_1, field="apply_hook_credit.amount_1")
    if amount_0 == 0 and amount_1 == 0:
        raise PositionError(
            "apply_hook_credit: at least one of amount_0, amount_1 must be positive"
        )
    if not isinstance(hook_evidence_state, HookEvidenceState):
        raise PositionError(
            f"apply_hook_credit.hook_evidence_state: must be HookEvidenceState, "
            f"got {type(hook_evidence_state).__name__}"
        )
    if hook_evidence_state is not HookEvidenceState.VERIFIED:
        raise PositionError(
            f"apply_hook_credit: hook credit requires HookEvidenceState.VERIFIED, "
            f"got {hook_evidence_state!r} (T043 gate)"
        )
    return PrincipalState(
        principal_amount_0=principal.principal_amount_0 + amount_0,
        principal_amount_1=principal.principal_amount_1 + amount_1,
        donation_amount_0=principal.donation_amount_0,
        donation_amount_1=principal.donation_amount_1,
        hook_credit_0=principal.hook_credit_0 + amount_0,
        hook_credit_1=principal.hook_credit_1 + amount_1,
    )


# ---------------------------------------------------------------------------
# Canonical valuation entry point
# ---------------------------------------------------------------------------


def compute_position_valuation(
    *,
    position_key: PositionKey,
    liquidity: int,
    pool_state: PoolState,
    pool_fee_state: PoolFeeState,
    snapshot: FeeGrowthSnapshot,
    principal: PrincipalState,
    hook_evidence_state: HookEvidenceState = HookEvidenceState.NO_HOOK,
) -> PositionValuation:
    """Compute the canonical :class:`PositionValuation`.

    The function is the T051 entry point. It is pure: same
    inputs produce byte-identical outputs in any order or
    process. The caller passes:

    - the position identity (``position_key``);
    - the current liquidity (``liquidity``);
    - the pool's point-in-time state (``pool_state``);
    - the pool's fee state (``pool_fee_state``) — combined-fee
      vs LP-only is the ``is_lp_fee_growth`` flag, and the
      protocol-fee halves T040 replays;
    - the position's per-position snapshot (``snapshot``);
    - the position's principal ledger (``principal``);
    - the T043 hook-evidence verdict (``hook_evidence_state``,
      default :attr:`HookEvidenceState.NO_HOOK`).

    The function returns:

    - the raw ``token0`` / ``token1`` inventory at the current
      price (the same figures the V4 settlement would use);
    - the :class:`RangeState` (``BELOW`` / ``INSIDE`` / ``ABOVE``);
    - the principal ledger;
    - the LP fees owed since the last settlement, with the
      protocol-fee share separated (Owner amendment 2026-09-16);
    - the cumulative donation totals and verified hook credits;
    - the resolved ``fee_growth_inside_*`` figures (so a
      downstream consumer can audit the V4 conditional).

    No side effects. No I/O. No float.
    """
    if not isinstance(position_key, PositionKey):
        raise PositionError(
            f"compute_position_valuation.position_key: must be PositionKey, "
            f"got {type(position_key).__name__}"
        )
    _require_uint128(liquidity, field="compute_position_valuation.liquidity")
    if not isinstance(pool_state, PoolState):
        raise PositionError(
            f"compute_position_valuation.pool_state: must be PoolState, "
            f"got {type(pool_state).__name__}"
        )
    if not isinstance(pool_fee_state, PoolFeeState):
        raise PositionError(
            f"compute_position_valuation.pool_fee_state: must be PoolFeeState, "
            f"got {type(pool_fee_state).__name__}"
        )
    if not isinstance(snapshot, FeeGrowthSnapshot):
        raise PositionError(
            f"compute_position_valuation.snapshot: must be FeeGrowthSnapshot, "
            f"got {type(snapshot).__name__}"
        )
    if not isinstance(principal, PrincipalState):
        raise PositionError(
            f"compute_position_valuation.principal: must be PrincipalState, "
            f"got {type(principal).__name__}"
        )
    if not isinstance(hook_evidence_state, HookEvidenceState):
        raise PositionError(
            f"compute_position_valuation.hook_evidence_state: must be HookEvidenceState, "
            f"got {type(hook_evidence_state).__name__}"
        )

    # V4 Range classification.
    range_state = classify_range_state(
        current_tick=pool_state.tick,
        tick_lower=position_key.tick_lower,
        tick_upper=position_key.tick_upper,
    )

    # Raw inventory at the current price. Boundary handling: at
    # the upper boundary V4's get_amounts_for_liquidity returns
    # the all-token0 figure (the [pa, pb) bracket is closed at
    # pa, so the boundary is "all token0"). At the lower
    # boundary the [pa, pb) bracket is open at pa, so the
    # boundary is "all token1". Both bounds are exercised
    # separately in the acceptance tests.
    raw_amount_0, raw_amount_1 = compute_raw_inventory(
        sqrt_price_x96=pool_state.sqrt_price_x96,
        tick_lower=position_key.tick_lower,
        tick_upper=position_key.tick_upper,
        liquidity=liquidity,
    )

    # V4 fee_growth_inside at the current tick.
    fee_growth_inside_0_x128 = compute_fee_growth_inside(
        current_tick=pool_state.tick,
        tick_lower=position_key.tick_lower,
        tick_upper=position_key.tick_upper,
        fee_growth_global_x128=pool_state.fee_growth_global_0_x128,
        fee_growth_outside_lower_x128=pool_state.fee_growth_outside_lower_0_x128,
        fee_growth_outside_upper_x128=pool_state.fee_growth_outside_upper_0_x128,
    )
    fee_growth_inside_1_x128 = compute_fee_growth_inside(
        current_tick=pool_state.tick,
        tick_lower=position_key.tick_lower,
        tick_upper=position_key.tick_upper,
        fee_growth_global_x128=pool_state.fee_growth_global_1_x128,
        fee_growth_outside_lower_x128=pool_state.fee_growth_outside_lower_1_x128,
        fee_growth_outside_upper_x128=pool_state.fee_growth_outside_upper_1_x128,
    )

    # LP fees owed since the last settlement. The protocol-fee
    # share is separated by split_protocol_fees.
    earned_combined_0 = snapshot.tokens_owed_0 + compute_lp_fees_owed(
        liquidity=liquidity,
        fee_growth_inside_now_x128=fee_growth_inside_0_x128,
        fee_growth_inside_last_x128=snapshot.fee_growth_inside_last_0_x128,
    )
    earned_combined_1 = snapshot.tokens_owed_1 + compute_lp_fees_owed(
        liquidity=liquidity,
        fee_growth_inside_now_x128=fee_growth_inside_1_x128,
        fee_growth_inside_last_x128=snapshot.fee_growth_inside_last_1_x128,
    )
    earned_lp_0, earned_lp_1, protocol_0, protocol_1 = split_protocol_fees(
        earned_combined_0=earned_combined_0,
        earned_combined_1=earned_combined_1,
        pool_fee_state=pool_fee_state,
    )

    return PositionValuation(
        position_key=position_key,
        liquidity=liquidity,
        pool_state=pool_state,
        pool_fee_state=pool_fee_state,
        range_state=range_state,
        raw_amount_0=raw_amount_0,
        raw_amount_1=raw_amount_1,
        principal_amount_0=principal.principal_amount_0,
        principal_amount_1=principal.principal_amount_1,
        earned_fees_0=earned_lp_0,
        earned_fees_1=earned_lp_1,
        protocol_fees_0=protocol_0,
        protocol_fees_1=protocol_1,
        donation_amount_0=principal.donation_amount_0,
        donation_amount_1=principal.donation_amount_1,
        hook_credit_0=principal.hook_credit_0,
        hook_credit_1=principal.hook_credit_1,
        fee_growth_inside_0_x128=fee_growth_inside_0_x128,
        fee_growth_inside_1_x128=fee_growth_inside_1_x128,
        hook_evidence_state=hook_evidence_state,
        unit=ObservationUnit.RAW_TOKEN_INTEGER,
        version=POSITION_VALUATION_VERSION,
    )


# ---------------------------------------------------------------------------
# Aggregation helpers (audit / cross-row reconciliation)
# ---------------------------------------------------------------------------


def sum_principal_amounts(valuations: Iterable[PositionValuation]) -> tuple[int, int]:
    """Return the sum of ``principal_amount_0`` / ``_1`` across rows.

    Used by T052 attribution / T070 risk to roll per-position
    ledgers up to a per-pool or per-wallet figure. The aggregation
    is byte-exact and uses Python ``int`` arithmetic only — no
    float, no Decimal.
    """
    sum_0 = 0
    sum_1 = 0
    for v in valuations:
        if not isinstance(v, PositionValuation):
            raise PositionError(
                f"sum_principal_amounts: every entry must be PositionValuation, "
                f"got {type(v).__name__}"
            )
        sum_0 += v.principal_amount_0
        sum_1 += v.principal_amount_1
    return sum_0, sum_1


def sum_earned_fees(valuations: Iterable[PositionValuation]) -> tuple[int, int]:
    """Return the sum of LP-owned ``earned_fees_0`` / ``_1`` across rows."""
    sum_0 = 0
    sum_1 = 0
    for v in valuations:
        if not isinstance(v, PositionValuation):
            raise PositionError(
                f"sum_earned_fees: every entry must be PositionValuation, got {type(v).__name__}"
            )
        sum_0 += v.earned_fees_0
        sum_1 += v.earned_fees_1
    return sum_0, sum_1


def sum_protocol_fees(valuations: Iterable[PositionValuation]) -> tuple[int, int]:
    """Return the sum of separated ``protocol_fees_0`` / ``_1`` across rows."""
    sum_0 = 0
    sum_1 = 0
    for v in valuations:
        if not isinstance(v, PositionValuation):
            raise PositionError(
                f"sum_protocol_fees: every entry must be PositionValuation, got {type(v).__name__}"
            )
        sum_0 += v.protocol_fees_0
        sum_1 += v.protocol_fees_1
    return sum_0, sum_1


def sum_raw_inventory(valuations: Iterable[PositionValuation]) -> tuple[int, int]:
    """Return the sum of ``raw_amount_0`` / ``_1`` across rows."""
    sum_0 = 0
    sum_1 = 0
    for v in valuations:
        if not isinstance(v, PositionValuation):
            raise PositionError(
                f"sum_raw_inventory: every entry must be PositionValuation, got {type(v).__name__}"
            )
        sum_0 += v.raw_amount_0
        sum_1 += v.raw_amount_1
    return sum_0, sum_1


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "FeeGrowthSnapshot",
    "HookEvidenceState",
    "InvalidFeeGrowthSnapshotError",
    "InvalidLiquidityError",
    "InvalidPositionKeyError",
    "InvalidPrincipalStateError",
    "InsufficientPrincipalError",
    "POOL_FEE_GROWTH_MAX_UINT256",
    "POSITION_VALUATION_VERSION",
    "PoolFeeState",
    "PoolState",
    "PositionError",
    "PositionKey",
    "PositionRelative",
    "PositionValuation",
    "PrincipalState",
    "Q128",
    "Q96",
    "RANGE_ABOVE",
    "RANGE_BELOW",
    "RANGE_INSIDE",
    "RangeState",
    "apply_burn",
    "apply_collect",
    "apply_donation",
    "apply_hook_credit",
    "apply_mint",
    "classify_range_state",
    "compute_fee_growth_inside",
    "compute_lp_fees_owed",
    "compute_position_valuation",
    "compute_raw_inventory",
    "compute_raw_inventory_at_lower",
    "compute_raw_inventory_at_upper",
    "snapshot_after_modify",
    "snapshot_at_mint",
    "split_protocol_fees",
    "sum_earned_fees",
    "sum_principal_amounts",
    "sum_protocol_fees",
    "sum_raw_inventory",
]
