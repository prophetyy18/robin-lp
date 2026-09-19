"""Point-in-time quote and gas valuation (T053).

T053 is the quote-valuation half of the V1 features package. It
implements:

- the **provider-neutral observation schema** described in
  ``docs/spec/architecture/adr/ADR-014-research-universe-and-numeraire.md``
  §3 and ``docs/spec/strategy/LP_METRICS.md`` §2: every quoted
  quantity carries ``observed_at``, ``available_at``, ``source``,
  ``pair``, the originating ``block`` / ``time``, ``confidence``
  and ``staleness``, plus an explicit ``numeraire_level`` and a
  per-field ``unit`` (raw token integer, numeraire units, ratio,
  dimensionless);
- the **ADR-014 reporting-numeraire hierarchy** as a per-dataset
  qualification record (USDG when present with a qualified
  valuation, otherwise a qualified USD stablecoin, otherwise
  ETH-display, otherwise ``RELATIVE_ONLY``);
- the **conversion graph** that turns a raw observation in the
  dataset's reporting numeraire into the target numeraire the
  consumer asked for, with explicit handling of
  *delayed / revised / depegged / missing / cross-rate* scenarios;
- a per-numeraire qualification record carrying confidence,
  staleness and the reason one level was chosen over another;
- a **point-in-time USDG conversion** that the same module feeds
  to performance, exposure and both 5-minute extreme-move rules
  (``G-EMERGENCY-01`` down-spike and ``G-STRATEGY-SPIKE-01``
  up-spike) — the execution path therefore reads one canonical
  USDG price object;
- a **``RELATIVE_ONLY`` path** whose output carries no
  USD-denominated field anywhere — no PnL, value, fee, gas or
  risk field is permitted; the validator
  :func:`assert_no_usd_fields` is the typed guard.

Design constraints (binding):

- **No float on the protocol / valuation path.** Per ADR-004 every
  arithmetic step is performed as Python ``int`` or as
  ``decimal.Decimal`` only at the named display / statistical
  boundary. The integer / Q64.64 boundary used by
  :func:`protocol.sizing.usdg_amount_to_token_amount` is the
  precedent; this module never widens that boundary.
- **No future data.** ``observed_at`` / ``available_at`` are
  explicit; a feature row computed at decision time ``t`` must
  not depend on a row whose ``available_at > t``. Prefix
  invariance is enforced by the immutable, time-ordered
  construction of the observation rows.
- **No silent current-price use.** When an observation has a
  delayed / missing / depegged scenario, the missing policy
  surfaces the situation as a :class:`MissingPolicy` enum value
  rather than substituting a current price.
- **No stablecoin = 1 USD.** The qualification record carries the
  *observed* ``stablecoin_per_usdg`` ratio; the conversion graph
  applies that ratio rather than ``1 << 64`` per USDG.
- **No ETH-as-primary-benchmark.** When ``ETH_DISPLAY_ONLY`` is
  the dataset's reporting numeraire the conversion graph refuses
  to USDG-convert through ETH and returns ``MISSING`` rather than
  substituting a USDG value.

The module depends only on the protocol-domain package (``ids``,
``events``) and the stdlib. It never imports RPC, storage,
signing, execution, configuration or the Web layer. The public
surface is structured so that downstream code (T050 bars, T051
valuation, T052 attribution, T070 risk) imports the typed rows
and never constructs them ad hoc.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final

from robinhood_lp.protocol.events import BlockRef
from robinhood_lp.protocol.ids import ChainId

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Q64.64 scale. Used by the integer USDG conversion at the single
#: named numeraire boundary ``usdg_amount_to_token_amount`` (T049).
#: This module re-uses the same scale so the conversion graph and
#: the sizing math speak the same integer language.
Q64_SCALE: Final[int] = 1 << 64

#: Maximum value of a uint256 (the atomic-unit widths for raw
#: token balances and USDG amounts). Validation paths use this to
#: reject impossible values rather than silently truncate.
MAX_UINT256: Final[int] = (1 << 256) - 1

#: The two 5-minute extreme-move rule windows used by the
#: execution path. Both rules consume the same USDG point-in-time
#: object the conversion graph produces (T053 contract; ADR-014
#: §3; PROJECT_GOALS.md ``G-EMERGENCY-01`` / ``G-STRATEGY-SPIKE-01``).
FIVE_MINUTE_RULE_WINDOW_SECONDS: Final[int] = 300

#: The 100% up-spike threshold (PROJECT_GOALS.md
#: ``G-STRATEGY-SPIKE-01``): a 5-minute USDG K-line move that
#: exceeds 100% blocks new risk-taking candidates. The threshold
#: is expressed here so the conversion graph can label the rule
#: rather than the value being hard-coded inside strategy code.
FIVE_MINUTE_UP_SPIKE_FRACTION: Final[int] = 1 << 64  # 100% in Q64.64

#: The 80% down-spike threshold (PROJECT_GOALS.md
#: ``G-EMERGENCY-01``): a 5-minute USDG K-line move that loses
#: more than 80% of value is a strategy AUTO_EXIT signal. The
#: threshold is recorded here for the same labelling reason.
FIVE_MINUTE_DOWN_SPIKE_FRACTION: Final[int] = (80 * Q64_SCALE) // 100

#: Default depeg threshold applied to the stablecoin → USDG edge:
#: 5% absolute move (in Q64.64) trips the
#: :attr:`MissingPolicy.DEPEGGED` label. The threshold is the
#: default; consumers may pass a tighter value to ``convert_to_usdg``.
DEFAULT_DEPEG_THRESHOLD_Q64_64: Final[int] = (5 * Q64_SCALE) // 100

# ---------------------------------------------------------------------------
# Enums (stable, public contract)
# ---------------------------------------------------------------------------


class NumeraireLevel(StrEnum):
    """The ADR-014 reporting-numeraire hierarchy.

    Strings are part of the public contract. New values are additive;
    renaming an existing value is a breaking change.

    The four values are exactly the levels the ADR-014 hierarchy
    permits:

    - :attr:`USDG` — primary numeraire; reported when the dataset
      contains USDG and a qualified valuation exists for it. The
      execution path requires USDG conversion to be available
      regardless of the dataset's reporting numeraire.
    - :attr:`QUALIFIED_USD_STABLECOIN` — used when the dataset
      contains no USDG pair but a USD-pegged asset passed the same
      qualification as USDG; the qualification record carries the
      observed ratio rather than assuming ``1 USDG = 1 USD``.
    - :attr:`ETH_DISPLAY_ONLY` — used when neither USDG nor a
      qualified USD stablecoin is available; the dataset reports
      raw ETH quantity for display only. ETH is explicitly labelled
      volatile, never a primary benchmark, and never a basis for
      cross-numeraire ranking.
    - :attr:`RELATIVE_ONLY` — used when the dataset contains
      neither a USD-family asset nor a qualified conversion route;
      output carries relative quantities only (token1 per token0)
      and no USD-denominated field anywhere.
    """

    USDG = "USDG"
    QUALIFIED_USD_STABLECOIN = "QUALIFIED_USD_STABLECOIN"
    ETH_DISPLAY_ONLY = "ETH_DISPLAY_ONLY"
    RELATIVE_ONLY = "RELATIVE_ONLY"


class ObservationUnit(StrEnum):
    """Per-field unit carried by every :class:`Observation` row.

    The unit is part of the observation; two rows with the same
    numeric value but different units are not equal. The unit
    discriminates the four classes a research / execution feature
    may encounter (per LP_METRICS.md §2):

    - :attr:`RAW_TOKEN_INTEGER` — atomic ERC-20 integer units, the
      only on-chain truth for token balance.
    - :attr:`NUMERAIRE_UNITS` — atomic units of the reporting
      numeraire (e.g. atomic USDG); the convention is the same as
      on-chain atomic units.
    - :attr:`RATIO` — a dimensionless ratio expressed in Q64.64
      (per the single named numeraire boundary used by T049). The
      base unit is documented per row.
    - :attr:`DIMENSIONLESS` — a unitless marker (e.g. counts,
      flags); no physical unit attaches to the value.
    """

    RAW_TOKEN_INTEGER = "RAW_TOKEN_INTEGER"
    NUMERAIRE_UNITS = "NUMERAIRE_UNITS"
    RATIO = "RATIO"
    DIMENSIONLESS = "DIMENSIONLESS"


class SourceKind(StrEnum):
    """Where an observation row originated.

    Each row carries exactly one :class:`SourceKind`. Mixing two
    sources / frequencies inside a single row is forbidden; when an
    external feed is mixed with the activity-pool observation, the
    feed row and the pool row are two distinct :class:`Observation`
    instances attached to a single :class:`QuoteBar`.
    """

    ONCHAIN_POOL = "ONCHAIN_POOL"
    EXTERNAL_FEED = "EXTERNAL_FEED"
    DERIVED = "DERIVED"
    UNKNOWN = "UNKNOWN"


class ConfidenceLevel(StrEnum):
    """The qualitative confidence the framework assigns to a row.

    The confidence propagates from the source into the qualification
    record and into the converted USDG price. Cross-numeraire
    ranking and ranking against an ``EXPERIMENTAL_NOT_LIVE_APPROVED``
    threshold must respect the row's confidence level.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class MissingPolicy(StrEnum):
    """How the conversion graph treats a missing / impaired path.

    The conversion graph never substitutes a current price for a
    missing observation: it carries the situation as a
    :class:`MissingPolicy` value. Consumers (T051 valuation, T070
    risk, the 5-minute rules) read the policy and refuse to act
    when the policy forbids it.
    """

    OK = "OK"
    DELAYED = "DELAYED"
    REVISED = "REVISED"
    DEPEGGED = "DEPEGGED"
    MISSING = "MISSING"
    CROSS_RATE = "CROSS_RATE"


class EdgePolicy(StrEnum):
    """How a single conversion-graph edge handles its inputs.

    The edge policy is the local decision for one edge; the
    :class:`MissingPolicy` is the resulting label on the converted
    observation.
    """

    DIRECT = "DIRECT"
    VIA_CROSS_RATE = "VIA_CROSS_RATE"
    DELAYED_TOLERATED = "DELAYED_TOLERATED"
    DEPEG_TOLERATED = "DEPEG_TOLERATED"


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: ISO-8601 UTC timestamp. We keep the time as an integer second
#: count to avoid float on the protocol / valuation path
#: (ADR-004). Display layers may format it back to ``str`` at the
#: presentation boundary.
UnixTimestamp = int

#: Block number; non-negative integer.
BlockNumber = int

#: Q64.64 fixed-point representation of a dimensionless ratio.
#: ``ratio_q64_64 == x << 64`` represents the rational ``x``.
RatioQ6464 = int


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class QuoteError(ValueError):
    """Base class for T053 quote / valuation failures."""


class QuoteObservationError(QuoteError):
    """An :class:`Observation` row violates its invariants."""


class QuoteGraphError(QuoteError):
    """The conversion graph cannot satisfy a request."""


class QuoteRelativeOnlyError(QuoteError):
    """A USD-denominated operation was attempted on a ``RELATIVE_ONLY`` row.

    The framework does not convert a ``RELATIVE_ONLY`` observation
    into USD terms. Cross-numeraire ranking against a USD result
    is also forbidden (ADR-014 §3).
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_int(value: int, *, field_name: str) -> int:
    """Validate ``value`` is a Python ``int`` (``bool`` is rejected)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise QuoteObservationError(f"{field_name}: must be int, got {type(value).__name__}")
    return value


def _require_non_negative_int(value: int, *, field_name: str) -> int:
    """Validate ``value`` is a non-negative Python ``int``."""
    value = _require_int(value, field_name=field_name)
    if value < 0:
        raise QuoteObservationError(f"{field_name}: must be non-negative, got {value}")
    return value


def _require_uint256(value: int, *, field_name: str) -> int:
    """Validate ``value`` is a non-negative integer fitting in uint256."""
    value = _require_non_negative_int(value, field_name=field_name)
    if value >= (1 << 256):
        raise QuoteObservationError(f"{field_name}: exceeds uint256 width, got {value}")
    return value


def _require_positive_ratio(value: int, *, field_name: str) -> int:
    """Validate ``value`` is a strictly positive Q64.64 ratio."""
    value = _require_int(value, field_name=field_name)
    if value <= 0:
        raise QuoteObservationError(f"{field_name}: must be positive Q64.64 ratio, got {value}")
    return value


# ---------------------------------------------------------------------------
# Observation row
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observation:
    """One provider-neutral point-in-time observation row.

    An observation is the smallest unit a quote bar can attach to
    the historical record. The schema is deliberately explicit
    about the unit, the source, the observation time and the
    availability time so the same row can be reused by performance
    (T052), exposure (T070), and both 5-minute extreme-move rules
    (``G-EMERGENCY-01`` and ``G-STRATEGY-SPIKE-01``).

    Notes
    -----
    - ``observed_at`` is the block / timestamp at which the source
      produced the value (the chain block time for ``ONCHAIN_POOL``,
      the feed's reported timestamp for ``EXTERNAL_FEED``).
    - ``available_at`` is the earliest time at which the consumer
      could have observed the row; it is the time the framework
      promises ``observed_at`` to be visible in the local store.
      Decision-time ``t`` must satisfy ``available_at <= t`` for the
      row to be admitted as a feature.
    - ``staleness_seconds`` is the wall-clock seconds elapsed between
      ``observed_at`` and ``available_at`` (the framework's record
      delay). It is **not** the difference to the decision time —
      that delta belongs to the consumer and is not stored here.
    - The ``unit`` discriminates how to interpret ``value``:

      * ``RAW_TOKEN_INTEGER`` — atomic ERC-20 integer units;
      * ``NUMERAIRE_UNITS`` — atomic units of ``numeraire_level``
        (USDG atomic units when ``numeraire_level == USDG``);
      * ``RATIO`` — Q64.64 fixed-point dimensionless ratio;
      * ``DIMENSIONLESS`` — counts or flags; no physical unit.

    The row is immutable; two observations are equal iff every
    field is equal. Equality and hashing follow dataclass identity.
    """

    observed_at: UnixTimestamp
    available_at: UnixTimestamp
    source: SourceKind
    pair: str
    block_number: BlockNumber
    block_ref: BlockRef
    confidence: ConfidenceLevel
    staleness_seconds: int
    numeraire_level: NumeraireLevel
    unit: ObservationUnit
    value: int
    chain_id: ChainId
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_negative_int(self.observed_at, field_name="Observation.observed_at")
        _require_non_negative_int(self.available_at, field_name="Observation.available_at")
        if self.available_at < self.observed_at:
            raise QuoteObservationError(
                f"Observation.available_at={self.available_at} must be >= "
                f"Observation.observed_at={self.observed_at}"
            )
        if not isinstance(self.source, SourceKind):
            raise QuoteObservationError(
                f"Observation.source: must be SourceKind, got {type(self.source).__name__}"
            )
        if not isinstance(self.pair, str) or not self.pair:
            raise QuoteObservationError(
                f"Observation.pair: must be non-empty str, got {self.pair!r}"
            )
        _require_non_negative_int(self.block_number, field_name="Observation.block_number")
        if not isinstance(self.block_ref, BlockRef):
            raise QuoteObservationError(
                f"Observation.block_ref: must be BlockRef, got {type(self.block_ref).__name__}"
            )
        if not isinstance(self.confidence, ConfidenceLevel):
            raise QuoteObservationError(
                f"Observation.confidence: must be ConfidenceLevel, got "
                f"{type(self.confidence).__name__}"
            )
        _require_non_negative_int(
            self.staleness_seconds, field_name="Observation.staleness_seconds"
        )
        if not isinstance(self.numeraire_level, NumeraireLevel):
            raise QuoteObservationError(
                f"Observation.numeraire_level: must be NumeraireLevel, got "
                f"{type(self.numeraire_level).__name__}"
            )
        if not isinstance(self.unit, ObservationUnit):
            raise QuoteObservationError(
                f"Observation.unit: must be ObservationUnit, got {type(self.unit).__name__}"
            )
        # ``value`` width per unit.
        if (
            self.unit is ObservationUnit.RAW_TOKEN_INTEGER
            or self.unit is ObservationUnit.NUMERAIRE_UNITS
        ):
            _require_uint256(self.value, field_name="Observation.value")
        elif self.unit is ObservationUnit.RATIO:
            _require_positive_ratio(self.value, field_name="Observation.value")
        else:  # DIMENSIONLESS
            _require_int(self.value, field_name="Observation.value")
        if not isinstance(self.chain_id, ChainId):
            raise QuoteObservationError(
                f"Observation.chain_id: must be ChainId, got {type(self.chain_id).__name__}"
            )
        if not isinstance(self.notes, tuple):
            raise QuoteObservationError(
                f"Observation.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise QuoteObservationError(
                    f"Observation.notes: every entry must be str, got {type(n).__name__}"
                )

    @property
    def is_relative_only(self) -> bool:
        """``True`` when the row is reported in ``RELATIVE_ONLY`` mode."""
        return self.numeraire_level is NumeraireLevel.RELATIVE_ONLY


# ---------------------------------------------------------------------------
# Per-numeraire qualification record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NumeraireQualification:
    """Why a particular numeraire level was selected for the dataset.

    One record is produced per :class:`NumeraireLevel` the dataset
    considered; the record selected as the dataset's reporting
    numeraire is the one whose :attr:`selected` flag is ``True``.
    Carrying the rejected runners — with the reason each was
    rejected — is what lets a downstream consumer (T051, T070,
    T100) audit why USDG was or was not chosen, rather than
    inferring it from a single string.

    The ``stablecoin_per_usdg_q64_64`` field is the *observed*
    ratio; it is **never** assumed to equal ``1 << 64``. When the
    level is :attr:`NumeraireLevel.RELATIVE_ONLY` or
    :attr:`NumeraireLevel.ETH_DISPLAY_ONLY` the ratio is ``None``
    and the conversion graph refuses to USDG-convert the row.
    """

    level: NumeraireLevel
    selected: bool
    rationale: str
    confidence: ConfidenceLevel
    staleness_seconds: int
    stablecoin_per_usdg_q64_64: int | None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.level, NumeraireLevel):
            raise QuoteObservationError(
                f"NumeraireQualification.level: must be NumeraireLevel, got "
                f"{type(self.level).__name__}"
            )
        if not isinstance(self.selected, bool):
            raise QuoteObservationError(
                f"NumeraireQualification.selected: must be bool, got {type(self.selected).__name__}"
            )
        if not isinstance(self.rationale, str) or not self.rationale:
            raise QuoteObservationError(
                f"NumeraireQualification.rationale: must be non-empty str, got {self.rationale!r}"
            )
        if not isinstance(self.confidence, ConfidenceLevel):
            raise QuoteObservationError(
                f"NumeraireQualification.confidence: must be ConfidenceLevel, "
                f"got {type(self.confidence).__name__}"
            )
        _require_non_negative_int(
            self.staleness_seconds, field_name="NumeraireQualification.staleness_seconds"
        )
        if self.stablecoin_per_usdg_q64_64 is not None:
            _require_positive_ratio(
                self.stablecoin_per_usdg_q64_64,
                field_name="NumeraireQualification.stablecoin_per_usdg_q64_64",
            )
        if not isinstance(self.notes, tuple):
            raise QuoteObservationError(
                f"NumeraireQualification.notes: must be tuple[str, ...], got "
                f"{type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise QuoteObservationError(
                    f"NumeraireQualification.notes: every entry must be str, got {type(n).__name__}"
                )

    @property
    def is_usd_denominated(self) -> bool:
        """``True`` when the level carries a USD denomination.

        USDG and qualified USD stablecoins are USD-denominated;
        ETH-display and ``RELATIVE_ONLY`` are not.
        """
        return self.level in (
            NumeraireLevel.USDG,
            NumeraireLevel.QUALIFIED_USD_STABLECOIN,
        )


@dataclass(frozen=True, slots=True)
class QualificationBundle:
    """The full per-dataset qualification record.

    Carries one :class:`NumeraireQualification` per
    :class:`NumeraireLevel` and exposes the selected one. The bundle
    is the single object downstream code consults before treating
    a quote bar as USD-denominated.
    """

    records: tuple[NumeraireQualification, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple):
            raise QuoteObservationError(
                f"QualificationBundle.records: must be tuple, got {type(self.records).__name__}"
            )
        seen_selected = 0
        seen_levels: set[NumeraireLevel] = set()
        for record in self.records:
            if not isinstance(record, NumeraireQualification):
                raise QuoteObservationError(
                    f"QualificationBundle.records: every entry must be "
                    f"NumeraireQualification, got {type(record).__name__}"
                )
            if record.level in seen_levels:
                raise QuoteObservationError(
                    f"QualificationBundle.records: duplicate level {record.level!r}"
                )
            seen_levels.add(record.level)
            if record.selected:
                seen_selected += 1
        if seen_selected != 1:
            raise QuoteObservationError(
                f"QualificationBundle.records: exactly one record must have "
                f"selected=True, got {seen_selected}"
            )

    @property
    def selected(self) -> NumeraireQualification:
        """Return the single :class:`NumeraireQualification` with ``selected=True``."""
        for record in self.records:
            if record.selected:
                return record
        # __post_init__ ensures exactly one selected record exists.
        raise QuoteObservationError("QualificationBundle.records: no record with selected=True")

    def record_for_level(self, level: NumeraireLevel) -> NumeraireQualification:
        """Return the record matching ``level``."""
        for record in self.records:
            if record.level is level:
                return record
        raise QuoteObservationError(f"QualificationBundle: no record for level {level!r}")


def empty_qualification_bundle() -> QualificationBundle:
    """Return an empty :class:`QualificationBundle` (used as the neutral start).

    The returned bundle has a single ``RELATIVE_ONLY`` record with
    ``selected=True``. Callers extend it via
    :meth:`QualificationBundle.with_records`.
    """
    return QualificationBundle(
        records=(
            NumeraireQualification(
                level=NumeraireLevel.RELATIVE_ONLY,
                selected=True,
                rationale="empty qualification bundle defaults to RELATIVE_ONLY",
                confidence=ConfidenceLevel.UNKNOWN,
                staleness_seconds=0,
                stablecoin_per_usdg_q64_64=None,
            ),
        )
    )


# ---------------------------------------------------------------------------
# Conversion graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConversionEdge:
    """One edge of the conversion graph.

    An edge is keyed by ``(source, target)``. The ``policy``
    expresses how the edge handles its inputs and the
    ``max_staleness_seconds`` is the staleness the edge refuses to
    exceed. ``via`` carries an optional intermediate numeraire for
    cross-rate paths (e.g. ``QUALIFIED_USD_STABLECOIN`` → ``USDG``
    via ``QUALIFIED_USD_STABLECOIN`` itself is direct; a path
    through ``ETH_DISPLAY_ONLY`` is forbidden by construction).
    """

    source: NumeraireLevel
    target: NumeraireLevel
    policy: EdgePolicy
    max_staleness_seconds: int
    via: NumeraireLevel | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.source, NumeraireLevel):
            raise QuoteObservationError(
                f"ConversionEdge.source: must be NumeraireLevel, got {type(self.source).__name__}"
            )
        if not isinstance(self.target, NumeraireLevel):
            raise QuoteObservationError(
                f"ConversionEdge.target: must be NumeraireLevel, got {type(self.target).__name__}"
            )
        if not isinstance(self.policy, EdgePolicy):
            raise QuoteObservationError(
                f"ConversionEdge.policy: must be EdgePolicy, got {type(self.policy).__name__}"
            )
        _require_non_negative_int(
            self.max_staleness_seconds, field_name="ConversionEdge.max_staleness_seconds"
        )
        if self.via is not None and not isinstance(self.via, NumeraireLevel):
            raise QuoteObservationError(
                f"ConversionEdge.via: must be NumeraireLevel or None, got {type(self.via).__name__}"
            )
        if not isinstance(self.notes, tuple):
            raise QuoteObservationError(
                f"ConversionEdge.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise QuoteObservationError(
                    f"ConversionEdge.notes: every entry must be str, got {type(n).__name__}"
                )


@dataclass(frozen=True, slots=True)
class ConversionPath:
    """The result of resolving a conversion request to a chain of edges.

    ``edges`` is the ordered list of :class:`ConversionEdge` instances
    the framework walked; ``policy`` is the resulting
    :class:`MissingPolicy` on the converted observation.
    """

    source: NumeraireLevel
    target: NumeraireLevel
    edges: tuple[ConversionEdge, ...]
    policy: MissingPolicy


@dataclass(frozen=True, slots=True)
class ConversionGraph:
    """A directed multigraph of :class:`ConversionEdge` instances.

    The graph is the static, declaration-time catalogue of the
    conversion routes the framework supports. Conversion requests
    against an unknown edge raise :class:`QuoteGraphError`; the
    framework never silently substitutes a current price.
    """

    edges: tuple[ConversionEdge, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.edges, tuple):
            raise QuoteObservationError(
                f"ConversionGraph.edges: must be tuple, got {type(self.edges).__name__}"
            )
        seen_keys: set[tuple[NumeraireLevel, NumeraireLevel]] = set()
        for edge in self.edges:
            if not isinstance(edge, ConversionEdge):
                raise QuoteObservationError(
                    f"ConversionGraph.edges: every entry must be "
                    f"ConversionEdge, got {type(edge).__name__}"
                )
            key = (edge.source, edge.target)
            if key in seen_keys:
                raise QuoteObservationError(
                    f"ConversionGraph.edges: duplicate ConversionEdge "
                    f"from {edge.source.value} to {edge.target.value}"
                )
            seen_keys.add(key)

    def edge(self, source: NumeraireLevel, target: NumeraireLevel) -> ConversionEdge:
        """Return the unique edge from ``source`` to ``target`` or raise.

        The graph does not allow duplicate edges for the same
        ``(source, target)`` pair; constructing the graph with two
        edges for the same pair is a contract bug, not a runtime
        outcome.
        """
        matches = [edge for edge in self.edges if edge.source is source and edge.target is target]
        if not matches:
            raise QuoteGraphError(f"ConversionGraph: no edge from {source.value} to {target.value}")
        if len(matches) > 1:
            raise QuoteGraphError(
                f"ConversionGraph: duplicate edges from {source.value} to {target.value}"
            )
        return matches[0]

    def has_edge(self, source: NumeraireLevel, target: NumeraireLevel) -> bool:
        """``True`` iff the graph has an edge from ``source`` to ``target``."""
        return any(edge.source is source and edge.target is target for edge in self.edges)

    def neighbors(self, source: NumeraireLevel) -> tuple[NumeraireLevel, ...]:
        """Return the targets reachable in one hop from ``source``."""
        seen: set[NumeraireLevel] = set()
        result: list[NumeraireLevel] = []
        for edge in self.edges:
            if edge.source is source and edge.target not in seen:
                seen.add(edge.target)
                result.append(edge.target)
        return tuple(result)


def default_conversion_graph() -> ConversionGraph:
    """Return the canonical conversion graph the V1 framework installs.

    The graph encodes the ADR-014 hierarchy:

    - ``USDG`` → ``USDG`` (identity edge, used when the row is
      already USDG-denominated);
    - ``QUALIFIED_USD_STABLECOIN`` → ``USDG`` (direct edge with the
      ``DEPEG_TOLERATED`` policy);
    - ``QUALIFIED_USD_STABLECOIN`` → ``QUALIFIED_USD_STABLECOIN``
      (identity edge, used when the consumer asks for the dataset's
      reporting numeraire);
    - ``ETH_DISPLAY_ONLY`` → ``ETH_DISPLAY_ONLY`` (identity edge);
    - ``RELATIVE_ONLY`` → ``RELATIVE_ONLY`` (identity edge).

    The graph deliberately omits any ETH → USDG edge: per
    ADR-014 §3 ETH is display-only and may not be the basis for
    a USDG conversion (the ``must-not ETH-as-primary-benchmark``
    rule). A request that needs ETH → USDG therefore fails closed.
    """
    return ConversionGraph(
        edges=(
            ConversionEdge(
                source=NumeraireLevel.USDG,
                target=NumeraireLevel.USDG,
                policy=EdgePolicy.DIRECT,
                max_staleness_seconds=600,
                notes=("identity edge for USDG-denominated rows",),
            ),
            ConversionEdge(
                source=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
                target=NumeraireLevel.USDG,
                policy=EdgePolicy.DEPEG_TOLERATED,
                max_staleness_seconds=600,
                notes=(
                    "stablecoin -> USDG edge; uses the qualification record's "
                    "stablecoin_per_usdg_q64_64 ratio rather than assuming "
                    "1 USDG = 1 USD",
                ),
            ),
            ConversionEdge(
                source=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
                target=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
                policy=EdgePolicy.DIRECT,
                max_staleness_seconds=600,
                notes=("identity edge for stablecoin-denominated rows",),
            ),
            ConversionEdge(
                source=NumeraireLevel.ETH_DISPLAY_ONLY,
                target=NumeraireLevel.ETH_DISPLAY_ONLY,
                policy=EdgePolicy.DIRECT,
                max_staleness_seconds=600,
                notes=("identity edge for ETH-display rows; refuses USDG conversion",),
            ),
            ConversionEdge(
                source=NumeraireLevel.RELATIVE_ONLY,
                target=NumeraireLevel.RELATIVE_ONLY,
                policy=EdgePolicy.DIRECT,
                max_staleness_seconds=0,
                notes=(
                    "identity edge for RELATIVE_ONLY rows; conversion is a "
                    "no-op but is required so the graph is total on the "
                    "RELATIVE_ONLY domain",
                ),
            ),
        )
    )


#: The default conversion graph with the canonical
#: ``DEPEG_TOLERATED`` policy on the stablecoin → USDG edge. The
#: constant is the value downstream code imports; the underlying
#: :func:`default_conversion_graph` factory is preserved for tests
#: that need a fresh instance.
DEFAULT_CONVERSION_GRAPH: Final[ConversionGraph] = default_conversion_graph()


# ---------------------------------------------------------------------------
# Quote bar
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuoteBar:
    """The single object the strategy and risk layers read.

    A bar carries:

    - the raw :class:`Observation` the conversion graph started
      from (typically the activity-pool observation the dataset
      records, possibly augmented by an external feed observation);
    - the :class:`QualificationBundle` recorded for the dataset;
    - the resulting USDG-denominated point-in-time price
      (``usdg_per_token_q64_64``) when the conversion succeeded, or
      ``None`` when the row is ``RELATIVE_ONLY`` or the conversion
      failed closed;
    - the resulting :class:`MissingPolicy` the conversion graph
      applied;
    - the :class:`ConversionPath` the graph walked;
    - a 5-minute extreme-move ``applied_rule`` label when the bar
      is consulted by either rule;
    - the ``is_relative_only`` flag that downstream consumers
      consult before ranking.

    A bar with ``is_relative_only=True`` carries no USD-denominated
    field anywhere; ``usdg_per_token_q64_64`` is ``None`` and the
    ranking guard refuses to compare it with a USD-denominated bar
    (see :func:`assert_no_usd_fields`).
    """

    observation: Observation
    qualification: QualificationBundle
    usdg_per_token_q64_64: int | None
    missing_policy: MissingPolicy
    path: ConversionPath
    applied_rule: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.observation, Observation):
            raise QuoteObservationError(
                f"QuoteBar.observation: must be Observation, got {type(self.observation).__name__}"
            )
        if not isinstance(self.qualification, QualificationBundle):
            raise QuoteObservationError(
                f"QuoteBar.qualification: must be QualificationBundle, got "
                f"{type(self.qualification).__name__}"
            )
        if self.usdg_per_token_q64_64 is not None:
            _require_positive_ratio(
                self.usdg_per_token_q64_64, field_name="QuoteBar.usdg_per_token_q64_64"
            )
        if not isinstance(self.missing_policy, MissingPolicy):
            raise QuoteObservationError(
                f"QuoteBar.missing_policy: must be MissingPolicy, got "
                f"{type(self.missing_policy).__name__}"
            )
        if not isinstance(self.path, ConversionPath):
            raise QuoteObservationError(
                f"QuoteBar.path: must be ConversionPath, got {type(self.path).__name__}"
            )
        if self.applied_rule is not None and not isinstance(self.applied_rule, str):
            raise QuoteObservationError(
                f"QuoteBar.applied_rule: must be str or None, got "
                f"{type(self.applied_rule).__name__}"
            )
        if not isinstance(self.notes, tuple):
            raise QuoteObservationError(
                f"QuoteBar.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise QuoteObservationError(
                    f"QuoteBar.notes: every entry must be str, got {type(n).__name__}"
                )
        # Invariant: a RELATIVE_ONLY bar never carries a USD price.
        if self.is_relative_only and self.usdg_per_token_q64_64 is not None:
            raise QuoteObservationError("QuoteBar: RELATIVE_ONLY bars must not carry a USDG price")

    @property
    def is_relative_only(self) -> bool:
        """``True`` when the bar carries no USD-denominated field.

        The check considers three signals:

        - the observation's reporting numeraire (``RELATIVE_ONLY``
          rows never carry a USDG price);
        - the dataset qualification's selected level (the bundle
          may report ``RELATIVE_ONLY`` even when the raw
          observation is on a stablecoin or ETH pool);
        - the conversion path's target (``convert_observation`` to
          ``RELATIVE_ONLY`` always produces a non-USD output).

        The union ensures a USDG observation that was explicitly
        converted to ``RELATIVE_ONLY`` is treated as a relative bar
        (its ``usdg_per_token_q64_64`` is ``None``).
        """
        return (
            self.observation.numeraire_level is NumeraireLevel.RELATIVE_ONLY
            or self.qualification.selected.level is NumeraireLevel.RELATIVE_ONLY
            or self.path.target is NumeraireLevel.RELATIVE_ONLY
        )


# ---------------------------------------------------------------------------
# Relative-only output (no USD-denominated field)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelativeOnlyBar:
    """A quote bar whose output carries no USD-denominated field anywhere.

    The bar stores a ratio ``token1_per_token0_q64_64`` in Q64.64
    fixed-point arithmetic, plus the same observation/provenance
    fields a :class:`QuoteBar` carries. No PnL, value, fee, gas or
    risk field is permitted; the
    :data:`USD_DENOMINATED_FORBIDDEN_FIELDS` constant names the
    fields a relative-only consumer is forbidden to compute, and
    :func:`assert_no_usd_fields` is the typed guard.

    The bar is the canonical output the framework produces when the
    dataset reports ``RELATIVE_ONLY``. It is never ranked against a
    USD-denominated :class:`QuoteBar`; the helper
    :func:`ranking_blocked_between` is the typed guard.
    """

    observation: Observation
    qualification: QualificationBundle
    token1_per_token0_q64_64: int
    path: ConversionPath
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.observation, Observation):
            raise QuoteObservationError(
                f"RelativeOnlyBar.observation: must be Observation, got "
                f"{type(self.observation).__name__}"
            )
        if not isinstance(self.qualification, QualificationBundle):
            raise QuoteObservationError(
                f"RelativeOnlyBar.qualification: must be QualificationBundle, "
                f"got {type(self.qualification).__name__}"
            )
        _require_positive_ratio(
            self.token1_per_token0_q64_64,
            field_name="RelativeOnlyBar.token1_per_token0_q64_64",
        )
        if not isinstance(self.path, ConversionPath):
            raise QuoteObservationError(
                f"RelativeOnlyBar.path: must be ConversionPath, got {type(self.path).__name__}"
            )
        if not isinstance(self.notes, tuple):
            raise QuoteObservationError(
                f"RelativeOnlyBar.notes: must be tuple[str, ...], got {type(self.notes).__name__}"
            )
        for n in self.notes:
            if not isinstance(n, str):
                raise QuoteObservationError(
                    f"RelativeOnlyBar.notes: every entry must be str, got {type(n).__name__}"
                )
        if self.qualification.selected.level is not NumeraireLevel.RELATIVE_ONLY:
            raise QuoteObservationError(
                "RelativeOnlyBar: qualification.selected.level must be RELATIVE_ONLY"
            )


#: Names of every USD-denominated field a ``RELATIVE_ONLY``
#: consumer must never compute. The list is the exhaustive set
#: the framework audits via :func:`assert_no_usd_fields`; adding
#: a new USD-denominated field anywhere in the framework must
#: add it to this constant so the ``RELATIVE_ONLY`` guard stays
#: closed.
USD_DENOMINATED_FORBIDDEN_FIELDS: Final[tuple[str, ...]] = (
    "pnl_usdg",
    "value_usdg",
    "fee_usdg",
    "gas_usdg",
    "risk_usdg",
    "marked_pnl_usdg",
    "liquidatable_pnl_usdg",
    "cash_benchmark_excess_usdg",
    "token_beta_pnl_usdg",
    "lp_service_pnl_usdg",
)


def assert_no_usd_fields(
    payload: Mapping[str, object],
    *,
    context: str = "RELATIVE_ONLY payload",
) -> None:
    """Raise if ``payload`` carries any USD-denominated field.

    The guard enumerates :data:`USD_DENOMINATED_FORBIDDEN_FIELDS`
    plus the suffix ``"_usdg"``. A ``RELATIVE_ONLY`` consumer must
    invoke it on every mapping it intends to serialise, report or
    feed into a downstream comparison. The check is deliberately
    strict: any forbidden key raises immediately so the framework
    never silently cross-numeraire ranks a relative-only result
    against a USD-denominated one.
    """
    if not isinstance(payload, Mapping):
        raise QuoteRelativeOnlyError(
            f"{context}: payload must be Mapping, got {type(payload).__name__}"
        )
    for key in payload:
        if not isinstance(key, str):
            raise QuoteRelativeOnlyError(
                f"{context}: payload keys must be str, got {type(key).__name__}"
            )
        if key in USD_DENOMINATED_FORBIDDEN_FIELDS:
            raise QuoteRelativeOnlyError(f"{context}: forbidden USD-denominated field {key!r}")
        if key.endswith("_usdg") or key.endswith("_usd"):
            raise QuoteRelativeOnlyError(f"{context}: forbidden USD-denominated field {key!r}")


def ranking_blocked_between(
    left: QuoteBar | RelativeOnlyBar,
    right: QuoteBar | RelativeOnlyBar,
) -> bool:
    """Return ``True`` iff ranking ``left`` against ``right`` is forbidden.

    Ranking a USD-denominated bar against a ``RELATIVE_ONLY`` bar is
    forbidden by ADR-014 §3. The guard is symmetric: regardless of
    the order of arguments, the result is ``True`` when one bar
    carries a USD field and the other does not.
    """
    left_usd = _carries_usd(left)
    right_usd = _carries_usd(right)
    return left_usd != right_usd


def _carries_usd(bar: QuoteBar | RelativeOnlyBar) -> bool:
    """``True`` iff ``bar`` carries a USD-denominated field."""
    if isinstance(bar, RelativeOnlyBar):
        return False
    if isinstance(bar, QuoteBar):
        return not bar.is_relative_only
    raise QuoteRelativeOnlyError(
        f"bar: must be QuoteBar or RelativeOnlyBar, got {type(bar).__name__}"
    )


# ---------------------------------------------------------------------------
# USDG conversion (the single canonical point-in-time USDG price)
# ---------------------------------------------------------------------------


def convert_to_usdg(
    *,
    observation: Observation,
    qualification: QualificationBundle,
    graph: ConversionGraph = DEFAULT_CONVERSION_GRAPH,
    decision_time: UnixTimestamp | None = None,
    previous_ratio_q64_64: int | None = None,
    depeg_threshold_q64_64: int | None = None,
) -> QuoteBar:
    """Convert ``observation`` into the canonical point-in-time USDG price.

    The function is the single entry point the strategy, risk,
    attribution and 5-minute extreme-move consumers call. The
    returned :class:`QuoteBar` carries the same USDG price object
    regardless of which consumer invoked it, satisfying the T053
    contract clause "the same versioned point-in-time USDG price
    semantics feed performance, exposure and both 5-minute
    extreme-move rules".

    Parameters
    ----------
    observation:
        The raw observation row.
    qualification:
        The per-dataset qualification record. The selected record
        determines the source numeraire; the rejected records carry
        why USDG was or was not chosen.
    graph:
        The conversion graph to walk. Defaults to
        :data:`DEFAULT_CONVERSION_GRAPH`.
    decision_time:
        Optional Unix timestamp at which the consumer is making
        the decision; when supplied, the function rejects any row
        whose ``available_at`` is strictly after ``decision_time``.
        This is the prefix-invariance guard — feature rows are
        computed only from observations that were already
        available at the decision time.
    previous_ratio_q64_64:
        Optional previous ``usdg_per_token_q64_64`` value the
        consumer carries. When supplied, the function compares the
        new ratio to the previous one and raises the
        :attr:`MissingPolicy.DEPEGGED` label when the move exceeds
        ``depeg_threshold_q64_64`` (default 5% in Q64.64).
    depeg_threshold_q64_64:
        The Q64.64 absolute move threshold that flags a row as
        :attr:`MissingPolicy.DEPEGGED`. ``None`` disables the
        check.

    Notes
    -----
    - The function is **pure**: no RPC, no storage, no clock, no
      random source. ``decision_time`` is supplied by the caller,
      never read from the system.
    - The function never substitutes a current price. When the
      conversion graph cannot reach USDG, the returned bar's
      ``missing_policy`` is :attr:`MissingPolicy.MISSING` and
      ``usdg_per_token_q64_64`` is ``None``.
    - The function never assumes a stablecoin equals one USD. The
      stablecoin → USDG edge multiplies the supplied
      ``stablecoin_per_usdg_q64_64`` ratio carried by the
      qualification record; the result is the actual USDG value
      of one unit of the underlying token at the row's
      ``observed_at``.
    """
    if not isinstance(observation, Observation):
        raise QuoteObservationError(
            f"convert_to_usdg.observation: must be Observation, got {type(observation).__name__}"
        )
    if not isinstance(qualification, QualificationBundle):
        raise QuoteObservationError(
            f"convert_to_usdg.qualification: must be QualificationBundle, got "
            f"{type(qualification).__name__}"
        )
    if not isinstance(graph, ConversionGraph):
        raise QuoteObservationError(
            f"convert_to_usdg.graph: must be ConversionGraph, got {type(graph).__name__}"
        )
    if decision_time is not None:
        _require_non_negative_int(decision_time, field_name="convert_to_usdg.decision_time")
        if observation.available_at > decision_time:
            raise QuoteObservationError(
                f"convert_to_usdg: observation.available_at="
                f"{observation.available_at} is after decision_time="
                f"{decision_time} (future data)"
            )
    if previous_ratio_q64_64 is not None:
        _require_positive_ratio(
            previous_ratio_q64_64,
            field_name="convert_to_usdg.previous_ratio_q64_64",
        )
    if depeg_threshold_q64_64 is not None:
        _require_positive_ratio(
            depeg_threshold_q64_64,
            field_name="convert_to_usdg.depeg_threshold_q64_64",
        )

    source_level = observation.numeraire_level
    selected_level = qualification.selected.level

    # RELATIVE_ONLY rows: the canonical output is a QuoteBar
    # without a USDG price. The framework never produces a USDG
    # price for these rows; ranking is blocked by construction.
    if (
        source_level is NumeraireLevel.RELATIVE_ONLY
        or selected_level is NumeraireLevel.RELATIVE_ONLY
    ):
        path = ConversionPath(
            source=source_level,
            target=source_level,
            edges=(),
            policy=MissingPolicy.OK,
        )
        return QuoteBar(
            observation=observation,
            qualification=qualification,
            usdg_per_token_q64_64=None,
            missing_policy=MissingPolicy.OK,
            path=path,
            notes=("RELATIVE_ONLY row carries no USDG price",),
        )

    # USDG-denominated rows: convert directly.
    if source_level is NumeraireLevel.USDG:
        usdg_q64_64 = _observation_to_q64_64_ratio(observation)
        return QuoteBar(
            observation=observation,
            qualification=qualification,
            usdg_per_token_q64_64=usdg_q64_64,
            missing_policy=MissingPolicy.OK,
            path=ConversionPath(
                source=NumeraireLevel.USDG,
                target=NumeraireLevel.USDG,
                edges=(graph.edge(NumeraireLevel.USDG, NumeraireLevel.USDG),),
                policy=MissingPolicy.OK,
            ),
            notes=("USDG-denominated row, identity edge",),
        )

    # Stablecoin -> USDG path.
    if source_level is NumeraireLevel.QUALIFIED_USD_STABLECOIN:
        selected_record = qualification.record_for_level(NumeraireLevel.QUALIFIED_USD_STABLECOIN)
        edge = graph.edge(
            NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            NumeraireLevel.USDG,
        )
        if selected_record.stablecoin_per_usdg_q64_64 is None:
            return QuoteBar(
                observation=observation,
                qualification=qualification,
                usdg_per_token_q64_64=None,
                missing_policy=MissingPolicy.MISSING,
                path=ConversionPath(
                    source=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
                    target=NumeraireLevel.USDG,
                    edges=(edge,),
                    policy=MissingPolicy.MISSING,
                ),
                notes=(
                    "stablecoin -> USDG edge rejected: missing stablecoin_per_usdg_q64_64 ratio",
                ),
            )
        if observation.staleness_seconds > edge.max_staleness_seconds:
            return QuoteBar(
                observation=observation,
                qualification=qualification,
                usdg_per_token_q64_64=None,
                missing_policy=MissingPolicy.DELAYED,
                path=ConversionPath(
                    source=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
                    target=NumeraireLevel.USDG,
                    edges=(edge,),
                    policy=MissingPolicy.DELAYED,
                ),
                notes=(
                    f"stablecoin -> USDG edge rejected: staleness="
                    f"{observation.staleness_seconds}s exceeds edge budget",
                ),
            )
        stablecoin_q64_64 = _observation_to_q64_64_ratio(observation)
        # usdg_per_token = stablecoin_per_token / stablecoin_per_usdg
        # In Q64.64: (stablecoin_q64_64 << 64) / selected_record.stablecoin_per_usdg_q64_64
        usdg_q64_64 = _q64_64_divide(stablecoin_q64_64, selected_record.stablecoin_per_usdg_q64_64)
        # Depeg check: when a previous ratio is supplied and the move
        # exceeds the threshold, flag DEPEGGED.
        missing_policy = MissingPolicy.OK
        notes_list: list[str] = [
            "stablecoin -> USDG edge; ratio derived from "
            "stablecoin_per_usdg_q64_64 (no 1-USD assumption)"
        ]
        if previous_ratio_q64_64 is not None and depeg_threshold_q64_64 is not None:
            move = _q64_64_abs_diff(usdg_q64_64, previous_ratio_q64_64)
            if move > depeg_threshold_q64_64:
                missing_policy = MissingPolicy.DEPEGGED
                notes_list.append(
                    f"depeg flagged: move={move} exceeds "
                    f"depeg_threshold_q64_64={depeg_threshold_q64_64}"
                )
        return QuoteBar(
            observation=observation,
            qualification=qualification,
            usdg_per_token_q64_64=usdg_q64_64,
            missing_policy=missing_policy,
            path=ConversionPath(
                source=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
                target=NumeraireLevel.USDG,
                edges=(edge,),
                policy=missing_policy,
            ),
            notes=tuple(notes_list),
        )

    # ETH-display rows: refuse USDG conversion. ADR-014 §3 forbids
    # ETH-as-primary-benchmark; the graph has no ETH -> USDG edge.
    if source_level is NumeraireLevel.ETH_DISPLAY_ONLY:
        return QuoteBar(
            observation=observation,
            qualification=qualification,
            usdg_per_token_q64_64=None,
            missing_policy=MissingPolicy.MISSING,
            path=ConversionPath(
                source=NumeraireLevel.ETH_DISPLAY_ONLY,
                target=NumeraireLevel.USDG,
                edges=(),
                policy=MissingPolicy.MISSING,
            ),
            notes=("ETH-display row refuses USDG conversion",),
        )

    # Cross-rate path: stablecoin -> ETH-display is forbidden; the
    # framework therefore treats unknown numeraires as MISSING.
    raise QuoteGraphError(f"convert_to_usdg: no route from {source_level.value} to USDG")


def convert_observation(
    *,
    observation: Observation,
    qualification: QualificationBundle,
    target: NumeraireLevel,
    graph: ConversionGraph = DEFAULT_CONVERSION_GRAPH,
) -> QuoteBar:
    """Convert ``observation`` to ``target`` using ``graph``.

    The function is the multi-target variant of
    :func:`convert_to_usdg`. The same invariants apply: no current
    price substitution, no stablecoin = 1 USD, no ETH-as-primary.
    The function never returns a USD-denominated bar for a
    ``RELATIVE_ONLY`` row; the result is a
    :attr:`MissingPolicy.MISSING` :class:`QuoteBar` whose
    ``usdg_per_token_q64_64`` is ``None``.
    """
    if not isinstance(observation, Observation):
        raise QuoteObservationError(
            f"convert_observation.observation: must be Observation, got "
            f"{type(observation).__name__}"
        )
    if not isinstance(qualification, QualificationBundle):
        raise QuoteObservationError(
            f"convert_observation.qualification: must be QualificationBundle, "
            f"got {type(qualification).__name__}"
        )
    if not isinstance(target, NumeraireLevel):
        raise QuoteObservationError(
            f"convert_observation.target: must be NumeraireLevel, got {type(target).__name__}"
        )
    if not isinstance(graph, ConversionGraph):
        raise QuoteObservationError(
            f"convert_observation.graph: must be ConversionGraph, got {type(graph).__name__}"
        )

    if target is NumeraireLevel.USDG:
        return convert_to_usdg(
            observation=observation,
            qualification=qualification,
            graph=graph,
        )
    if target is NumeraireLevel.RELATIVE_ONLY:
        path = ConversionPath(
            source=observation.numeraire_level,
            target=target,
            edges=(),
            policy=MissingPolicy.OK,
        )
        return QuoteBar(
            observation=observation,
            qualification=qualification,
            usdg_per_token_q64_64=None,
            missing_policy=MissingPolicy.OK,
            path=path,
            notes=("explicit RELATIVE_ONLY conversion; no USD field",),
        )
    if not graph.has_edge(observation.numeraire_level, target):
        return QuoteBar(
            observation=observation,
            qualification=qualification,
            usdg_per_token_q64_64=None,
            missing_policy=MissingPolicy.MISSING,
            path=ConversionPath(
                source=observation.numeraire_level,
                target=target,
                edges=(),
                policy=MissingPolicy.MISSING,
            ),
            notes=(f"no edge from {observation.numeraire_level.value} to {target.value}",),
        )
    edge = graph.edge(observation.numeraire_level, target)
    if observation.staleness_seconds > edge.max_staleness_seconds:
        return QuoteBar(
            observation=observation,
            qualification=qualification,
            usdg_per_token_q64_64=None,
            missing_policy=MissingPolicy.DELAYED,
            path=ConversionPath(
                source=observation.numeraire_level,
                target=target,
                edges=(edge,),
                policy=MissingPolicy.DELAYED,
            ),
            notes=(
                f"edge {edge.source.value} -> {edge.target.value} delayed: "
                f"staleness={observation.staleness_seconds}s",
            ),
        )
    ratio_q64_64 = _observation_to_q64_64_ratio(observation)
    return QuoteBar(
        observation=observation,
        qualification=qualification,
        usdg_per_token_q64_64=ratio_q64_64,
        missing_policy=MissingPolicy.OK,
        path=ConversionPath(
            source=observation.numeraire_level,
            target=target,
            edges=(edge,),
            policy=MissingPolicy.OK,
        ),
        notes=(f"direct edge {edge.source.value} -> {edge.target.value}",),
    )


def build_relative_only_bar(
    *,
    observation: Observation,
    qualification: QualificationBundle,
) -> RelativeOnlyBar:
    """Construct a :class:`RelativeOnlyBar` from a relative-only row.

    The function refuses to construct a relative-only bar from a row
    whose selected qualification is not ``RELATIVE_ONLY``. A
    USD-denominated consumer must therefore use
    :func:`convert_to_usdg`; a relative-only consumer must use this
    helper. The two paths share no field types.
    """
    if not isinstance(observation, Observation):
        raise QuoteObservationError(
            f"build_relative_only_bar.observation: must be Observation, got "
            f"{type(observation).__name__}"
        )
    if not isinstance(qualification, QualificationBundle):
        raise QuoteObservationError(
            f"build_relative_only_bar.qualification: must be "
            f"QualificationBundle, got {type(qualification).__name__}"
        )
    if qualification.selected.level is not NumeraireLevel.RELATIVE_ONLY:
        raise QuoteRelativeOnlyError(
            "build_relative_only_bar: qualification.selected.level must be RELATIVE_ONLY"
        )
    ratio_q64_64 = _observation_to_q64_64_ratio(observation)
    path = ConversionPath(
        source=observation.numeraire_level,
        target=NumeraireLevel.RELATIVE_ONLY,
        edges=(),
        policy=MissingPolicy.OK,
    )
    return RelativeOnlyBar(
        observation=observation,
        qualification=qualification,
        token1_per_token0_q64_64=ratio_q64_64,
        path=path,
        notes=("RELATIVE_ONLY bar; no USD-denominated field",),
    )


# ---------------------------------------------------------------------------
# 5-minute rule labels
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FiveMinuteRuleVerdict:
    """The verdict of a 5-minute extreme-move rule.

    The verdict carries the rule's identity (``UP_SPIKE`` /
    ``DOWN_SPIKE``), the pre-move USDG price (``pre_q64_64``),
    the post-move USDG price (``post_q64_64``), the move in Q64.64
    (``move_q64_64``) and the threshold the framework applies
    (``threshold_q64_64``). When ``triggered`` is ``True`` the
    strategy layer must apply the rule; the framework never reads
    the rule — it only labels the price object so the strategy can
    consume it deterministically.

    Both rules consume the same :class:`QuoteBar` the framework
    produces via :func:`convert_to_usdg`, satisfying the T053
    contract clause "the same versioned point-in-time USDG price
    semantics feed performance, exposure and both 5-minute
    extreme-move rules".
    """

    rule_id: str
    triggered: bool
    pre_q64_64: int
    post_q64_64: int
    move_q64_64: int
    threshold_q64_64: int
    bar: QuoteBar
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.rule_id not in {"UP_SPIKE", "DOWN_SPIKE"}:
            raise QuoteObservationError(
                f"FiveMinuteRuleVerdict.rule_id: must be 'UP_SPIKE' or "
                f"'DOWN_SPIKE', got {self.rule_id!r}"
            )
        if not isinstance(self.triggered, bool):
            raise QuoteObservationError(
                f"FiveMinuteRuleVerdict.triggered: must be bool, got "
                f"{type(self.triggered).__name__}"
            )
        _require_positive_ratio(self.pre_q64_64, field_name="FiveMinuteRuleVerdict.pre_q64_64")
        _require_positive_ratio(self.post_q64_64, field_name="FiveMinuteRuleVerdict.post_q64_64")
        _require_non_negative_int(self.move_q64_64, field_name="FiveMinuteRuleVerdict.move_q64_64")
        _require_positive_ratio(
            self.threshold_q64_64,
            field_name="FiveMinuteRuleVerdict.threshold_q64_64",
        )
        if not isinstance(self.bar, QuoteBar):
            raise QuoteObservationError(
                f"FiveMinuteRuleVerdict.bar: must be QuoteBar, got {type(self.bar).__name__}"
            )
        if self.bar.is_relative_only:
            raise QuoteRelativeOnlyError(
                "FiveMinuteRuleVerdict: RELATIVE_ONLY bars are forbidden; "
                "both 5-minute rules require a USDG price"
            )


def evaluate_five_minute_rules(
    *,
    pre_bar: QuoteBar,
    post_bar: QuoteBar,
    up_threshold_q64_64: int = FIVE_MINUTE_UP_SPIKE_FRACTION,
    down_threshold_q64_64: int = FIVE_MINUTE_DOWN_SPIKE_FRACTION,
) -> tuple[FiveMinuteRuleVerdict, FiveMinuteRuleVerdict]:
    """Evaluate both 5-minute extreme-move rules on the supplied bars.

    The function consumes the same USDG price object the strategy,
    risk and attribution layers consume, so the rule verdicts share
    the same USDG semantics the rest of the framework reads.
    """
    if not isinstance(pre_bar, QuoteBar):
        raise QuoteObservationError(
            f"evaluate_five_minute_rules.pre_bar: must be QuoteBar, got {type(pre_bar).__name__}"
        )
    if not isinstance(post_bar, QuoteBar):
        raise QuoteObservationError(
            f"evaluate_five_minute_rules.post_bar: must be QuoteBar, got {type(post_bar).__name__}"
        )
    if pre_bar.is_relative_only or post_bar.is_relative_only:
        raise QuoteRelativeOnlyError("evaluate_five_minute_rules: RELATIVE_ONLY bars are forbidden")
    if pre_bar.usdg_per_token_q64_64 is None or post_bar.usdg_per_token_q64_64 is None:
        raise QuoteObservationError("evaluate_five_minute_rules: bars must carry USDG prices")
    pre_q64_64 = pre_bar.usdg_per_token_q64_64
    post_q64_64 = post_bar.usdg_per_token_q64_64
    move_q64_64 = _q64_64_abs_diff(pre_q64_64, post_q64_64)

    up_verdict = FiveMinuteRuleVerdict(
        rule_id="UP_SPIKE",
        triggered=post_q64_64 > pre_q64_64 and move_q64_64 > up_threshold_q64_64,
        pre_q64_64=pre_q64_64,
        post_q64_64=post_q64_64,
        move_q64_64=move_q64_64,
        threshold_q64_64=up_threshold_q64_64,
        bar=post_bar,
        notes=(f"UP_SPIKE rule; threshold={up_threshold_q64_64}",),
    )
    down_verdict = FiveMinuteRuleVerdict(
        rule_id="DOWN_SPIKE",
        triggered=post_q64_64 < pre_q64_64 and move_q64_64 > down_threshold_q64_64,
        pre_q64_64=pre_q64_64,
        post_q64_64=post_q64_64,
        move_q64_64=move_q64_64,
        threshold_q64_64=down_threshold_q64_64,
        bar=post_bar,
        notes=(f"DOWN_SPIKE rule; threshold={down_threshold_q64_64}",),
    )
    return up_verdict, down_verdict


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _observation_to_q64_64_ratio(observation: Observation) -> int:
    """Return the observation's value as a positive Q64.64 ratio.

    The function normalises :attr:`ObservationUnit.RATIO`,
    :attr:`ObservationUnit.NUMERAIRE_UNITS` (treated as one unit
    per ``Q64_SCALE``) and :attr:`ObservationUnit.RAW_TOKEN_INTEGER`
    (also treated as one unit per ``Q64_SCALE``) into a positive
    Q64.64 value. :attr:`ObservationUnit.DIMENSIONLESS` rows are
    rejected: a dimensionless row does not carry a price.
    """
    unit = observation.unit
    if unit is ObservationUnit.RATIO:
        return _require_positive_ratio(observation.value, field_name="Observation.value")
    if unit is ObservationUnit.NUMERAIRE_UNITS:
        if observation.value <= 0:
            raise QuoteObservationError(
                f"Observation.value={observation.value}: NUMERAIRE_UNITS rows "
                f"must be positive to be converted to a Q64.64 ratio"
            )
        return observation.value * Q64_SCALE
    if unit is ObservationUnit.RAW_TOKEN_INTEGER:
        if observation.value <= 0:
            raise QuoteObservationError(
                f"Observation.value={observation.value}: RAW_TOKEN_INTEGER "
                f"rows must be positive to be converted to a Q64.64 ratio"
            )
        return observation.value * Q64_SCALE
    raise QuoteObservationError(f"Observation.unit={unit!r}: cannot be converted to a Q64.64 ratio")


def _q64_64_divide(numerator_q64_64: int, denominator_q64_64: int) -> int:
    """Compute ``numerator_q64_64 / denominator_q64_64`` in Q64.64 fixed-point."""
    if denominator_q64_64 <= 0:
        raise QuoteObservationError(
            f"_q64_64_divide: denominator must be positive, got {denominator_q64_64}"
        )
    if numerator_q64_64 <= 0:
        raise QuoteObservationError(
            f"_q64_64_divide: numerator must be positive, got {numerator_q64_64}"
        )
    return (numerator_q64_64 << 64) // denominator_q64_64


def _q64_64_abs_diff(left_q64_64: int, right_q64_64: int) -> int:
    """Return ``|left - right|`` for two Q64.64 values."""
    if left_q64_64 < 0 or right_q64_64 < 0:
        raise QuoteObservationError(
            f"_q64_64_abs_diff: inputs must be non-negative, got {left_q64_64} and {right_q64_64}"
        )
    if left_q64_64 >= right_q64_64:
        return left_q64_64 - right_q64_64
    return right_q64_64 - left_q64_64


# ---------------------------------------------------------------------------
# Display boundary (Decimal only here, ADR-009)
# ---------------------------------------------------------------------------


def format_ratio_decimal(
    ratio_q64_64: int,
    *,
    fractional_digits: int = 18,
) -> Decimal:
    """Convert ``ratio_q64_64`` to a Decimal value rounded to ``fractional_digits``.

    The function lives at the named display / statistical boundary
    per ADR-009. It is the only place in this module that builds a
    :class:`decimal.Decimal`; protocol / valuation code consumes the
    integer ratio.
    """
    if not isinstance(ratio_q64_64, int) or isinstance(ratio_q64_64, bool):
        raise QuoteObservationError(
            f"format_ratio_decimal.ratio_q64_64: must be int, got {type(ratio_q64_64).__name__}"
        )
    if ratio_q64_64 < 0:
        raise QuoteObservationError(
            f"format_ratio_decimal.ratio_q64_64: must be non-negative, got {ratio_q64_64}"
        )
    if not isinstance(fractional_digits, int) or isinstance(fractional_digits, bool):
        raise QuoteObservationError(
            f"format_ratio_decimal.fractional_digits: must be int, got "
            f"{type(fractional_digits).__name__}"
        )
    if fractional_digits < 0 or fractional_digits > 36:
        raise QuoteObservationError(
            f"format_ratio_decimal.fractional_digits: must be in [0, 36], got {fractional_digits}"
        )
    quantum = Decimal(10) ** -fractional_digits
    return (Decimal(ratio_q64_64) / Decimal(Q64_SCALE)).quantize(quantum)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_CONVERSION_GRAPH",
    "DEFAULT_DEPEG_THRESHOLD_Q64_64",
    "FIVE_MINUTE_DOWN_SPIKE_FRACTION",
    "FIVE_MINUTE_RULE_WINDOW_SECONDS",
    "FIVE_MINUTE_UP_SPIKE_FRACTION",
    "MAX_UINT256",
    "Q64_SCALE",
    "USD_DENOMINATED_FORBIDDEN_FIELDS",
    "BlockNumber",
    "ConfidenceLevel",
    "ConversionEdge",
    "ConversionGraph",
    "ConversionPath",
    "EdgePolicy",
    "FiveMinuteRuleVerdict",
    "MissingPolicy",
    "NumeraireLevel",
    "NumeraireQualification",
    "Observation",
    "ObservationUnit",
    "QualificationBundle",
    "QuoteBar",
    "QuoteError",
    "QuoteGraphError",
    "QuoteObservationError",
    "QuoteRelativeOnlyError",
    "RatioQ6464",
    "RelativeOnlyBar",
    "SourceKind",
    "UnixTimestamp",
    "assert_no_usd_fields",
    "build_relative_only_bar",
    "convert_observation",
    "convert_to_usdg",
    "default_conversion_graph",
    "empty_qualification_bundle",
    "evaluate_five_minute_rules",
    "format_ratio_decimal",
    "ranking_blocked_between",
]
