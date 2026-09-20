"""Research dataset registry and per-dataset numeraire qualification (T100).

T100 binds a **named, immutable, versioned** dataset to:

- one or more ``(chain_id, PoolKey)`` member pools over declared
  block ranges;
- the partitions each member resolves to, with the ingestion and
  schema revisions those partitions carry;
- one **reporting numeraire** chosen from the ADR-014 hierarchy
  (USDG -> qualified USD stablecoin -> ETH display ->
  ``RELATIVE_ONLY``) with a recorded valuation qualification that
  carries ``observed_at``, ``available_at``, ``staleness_seconds``,
  ``confidence`` and ``source`` (T053 point-in-time discipline);
- an explicit content hash the framework can reproduce from the
  declaration alone.

A dataset is the unit of research input the backtest harness, the
model layer and the research console all reference. It is the
object that separates the research universe from the execution
scope (ADR-014):

- a research dataset may contain many ``PoolKey`` values;
- a research dataset imposes no token approval: research neither
  holds nor trades;
- a research dataset must never grant, feed or weaken the
  single-active-pool execution path;
- a research dataset must never acquire execution authority, appear
  as an approved pool, or become the live default.

The module is the storage-layer surface the rest of P10 imports
(see ``docs/spec/architecture/ARCHITECTURE.md`` §2.2, T100 row).
It depends on the protocol layer, the discovery research-universe
collection (T026), the research-classification gate (T027) and the
T053 numeraire qualification records. It does **not** import the
config, execution or signer layers.

## Immutability / versioning contract

Publishing a dataset is **additive**: a new declaration with a new
content hash produces a new :class:`DatasetVersion`. An existing
version is never edited; an overlapping range or a new pool is a
new version rather than a mutation. Two datasets covering the
same partitions remain isolated, and a result computed on one is
never attributed to the other (``DS-002``).

The framework therefore serialises the dataset declaration as a
canonical JSON document, hashes it, and refuses to register a
declaration whose hash already exists in the registry. Two
heterogeneous datasets (single-pool USDG and multi-pool mixed
numeraire) resolve identically across runs and hosts after
excluding the declared observational fields because the hash
keys only on the deterministic fields, never on ingestion wall
clock or retrieval time (``DS-001``, ``DS-043``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from robinhood_lp.discovery.research_classification import (
    ResearchClassificationDecision,
    ResearchSupportGateError,
    assert_research_member_backtest_eligible,
)
from robinhood_lp.discovery.research_universe import (
    ResearchUniverse,
    UnknownResearchMemberError,
)
from robinhood_lp.protocol.ids import ChainId, PoolId, PoolKey
from robinhood_lp.protocol.run_mode import RunMode

# Note: T100 is a storage-layer module (``robinhood_lp.research.dataset``
# sits at the ``storage`` tier per ``tools/check_imports/layer_map.py``
# §MODULE_OVERRIDES). ADR-006 forbids a lower-to-higher import, so this
# module deliberately re-declares the minimum numeraire vocabulary it
# needs rather than importing from ``robinhood_lp.features`` (the
# T053 quote layer). The point-in-time discipline the contract names
# is satisfied by the same field shape (``observed_at`` /
# ``available_at`` / ``staleness_seconds`` / ``confidence`` /
# ``source``) T053 uses; the string values of the enums match the
# T053 vocabulary byte-for-byte so a dataset declaration built here
# is forward-compatible with downstream consumers that read T053's
# types directly.


# ---------------------------------------------------------------------------
# Numeraire vocabulary (mirrored from T053 for layer-direction compliance)
# ---------------------------------------------------------------------------


class NumeraireLevel(StrEnum):
    """The ADR-014 reporting-numeraire hierarchy.

    The string values match :class:`robinhood_lp.features.quote.NumeraireLevel`
    byte-for-byte so a dataset declaration built here is forward-
    compatible with downstream consumers that read T053's types
    directly. New values are additive; renaming an existing value
    is a breaking change.
    """

    USDG = "USDG"
    QUALIFIED_USD_STABLECOIN = "QUALIFIED_USD_STABLECOIN"
    ETH_DISPLAY_ONLY = "ETH_DISPLAY_ONLY"
    RELATIVE_ONLY = "RELATIVE_ONLY"


class ConfidenceLevel(StrEnum):
    """The qualitative confidence the framework assigns to a row.

    The string values match
    :class:`robinhood_lp.features.quote.ConfidenceLevel` byte-for-byte
    so a dataset declaration built here is forward-compatible with
    downstream consumers that read T053's types directly. New
    values are additive; renaming an existing value is a breaking
    change.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class NumeraireQualification:
    """Why a particular numeraire level was selected for the dataset.

    One record is produced per :class:`NumeraireLevel` the dataset
    considered; the record selected as the dataset's reporting
    numeraire is the one whose :attr:`selected` flag is ``True``.
    Carrying the rejected runners — with the reason each was
    rejected — is what lets a downstream consumer (T101, T102,
    T103) audit why USDG was or was not chosen, rather than
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
            raise DatasetError(
                f"NumeraireQualification.level: must be NumeraireLevel, got "
                f"{type(self.level).__name__}"
            )
        if not isinstance(self.selected, bool):
            raise DatasetError(
                f"NumeraireQualification.selected: must be bool, got {type(self.selected).__name__}"
            )
        if not isinstance(self.rationale, str) or not self.rationale:
            raise DatasetError(
                f"NumeraireQualification.rationale: must be non-empty str, got {self.rationale!r}"
            )
        if not isinstance(self.confidence, ConfidenceLevel):
            raise DatasetError(
                f"NumeraireQualification.confidence: must be ConfidenceLevel, "
                f"got {type(self.confidence).__name__}"
            )
        if not isinstance(self.staleness_seconds, int) or isinstance(self.staleness_seconds, bool):
            raise DatasetError(
                f"NumeraireQualification.staleness_seconds: must be int, got "
                f"{type(self.staleness_seconds).__name__}"
            )
        if self.staleness_seconds < 0:
            raise DatasetError(
                f"NumeraireQualification.staleness_seconds: must be >= 0, got "
                f"{self.staleness_seconds}"
            )
        if self.stablecoin_per_usdg_q64_64 is not None and (
            not isinstance(self.stablecoin_per_usdg_q64_64, int)
            or isinstance(self.stablecoin_per_usdg_q64_64, bool)
            or self.stablecoin_per_usdg_q64_64 <= 0
        ):
            raise DatasetError(
                "NumeraireQualification.stablecoin_per_usdg_q64_64: must be "
                "positive int, got "
                f"{self.stablecoin_per_usdg_q64_64!r}"
            )
        if not isinstance(self.notes, tuple):
            raise DatasetError(
                f"NumeraireQualification.notes: must be tuple[str, ...], got "
                f"{type(self.notes).__name__}"
            )
        for note in self.notes:
            if not isinstance(note, str):
                raise DatasetError(
                    f"NumeraireQualification.notes: every entry must be str, got "
                    f"{type(note).__name__}"
                )

    @property
    def is_usd_denominated(self) -> bool:
        """``True`` when the level carries a USD denomination."""
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
    a dataset as USD-denominated.
    """

    records: tuple[NumeraireQualification, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple):
            raise DatasetError(
                f"QualificationBundle.records: must be tuple, got {type(self.records).__name__}"
            )
        seen_selected = 0
        seen_levels: set[NumeraireLevel] = set()
        for record in self.records:
            if not isinstance(record, NumeraireQualification):
                raise DatasetError(
                    f"QualificationBundle.records: every entry must be "
                    f"NumeraireQualification, got {type(record).__name__}"
                )
            if record.level in seen_levels:
                raise DatasetError(f"QualificationBundle.records: duplicate level {record.level!r}")
            seen_levels.add(record.level)
            if record.selected:
                seen_selected += 1
        if seen_selected != 1:
            raise DatasetError(
                f"QualificationBundle.records: exactly one record must have "
                f"selected=True, got {seen_selected}"
            )

    @property
    def selected(self) -> NumeraireQualification:
        """Return the single :class:`NumeraireQualification` with ``selected=True``."""
        for record in self.records:
            if record.selected:
                return record
        raise DatasetError("QualificationBundle.records: no record with selected=True")

    def record_for_level(self, level: NumeraireLevel) -> NumeraireQualification:
        """Return the record matching ``level``."""
        if not isinstance(level, NumeraireLevel):
            raise DatasetError(
                f"QualificationBundle.record_for_level: level must be "
                f"NumeraireLevel, got {type(level).__name__}"
            )
        for record in self.records:
            if record.level is level:
                return record
        raise DatasetError(f"QualificationBundle: no record for level {level!r}")


#: USD-denominated field names forbidden on a RELATIVE_ONLY dataset.
#: Mirrors :data:`robinhood_lp.features.quote.USD_DENOMINATED_FORBIDDEN_FIELDS`
#: so a payload the registry validates is forward-compatible with
#: downstream consumers that read T053's vocabulary directly.
USD_DENOMINATED_FORBIDDEN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "marked_pnl_usdg",
        "liquidatable_pnl_usdg",
        "cash_benchmark_excess_usdg",
        "token_beta_pnl_usdg",
        "lp_service_pnl_usdg",
        "fees_usdg",
        "value_usdg",
        "gas_usdg",
        "pnl_usd",
    }
)


def assert_no_usd_fields(
    payload: Mapping[str, object],
    *,
    context: str = "RELATIVE_ONLY payload",
) -> None:
    """Raise if ``payload`` carries any USD-denominated field.

    The guard enumerates :data:`USD_DENOMINATED_FORBIDDEN_FIELDS`
    plus the suffix ``"_usdg"`` / ``"_usd"``. A ``RELATIVE_ONLY``
    consumer must invoke it on every mapping it intends to
    serialise, report or feed into a downstream comparison.
    """
    if not isinstance(payload, Mapping):
        raise DatasetError(f"{context}: payload must be Mapping, got {type(payload).__name__}")
    for key in payload:
        if not isinstance(key, str):
            raise DatasetError(f"{context}: payload keys must be str, got {type(key).__name__}")
        if key in USD_DENOMINATED_FORBIDDEN_FIELDS:
            raise DatasetError(f"{context}: forbidden USD-denominated field {key!r}")
        if key.endswith("_usdg") or key.endswith("_usd"):
            raise DatasetError(f"{context}: forbidden USD-denominated field {key!r}")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Schema version of the on-disk dataset declaration. Bumped only when
#: the JSON shape or its interpretation changes. A reader that opens a
#: declaration with a higher version refuses it.
DATASET_SCHEMA_VERSION: Final[int] = 1

#: The default hash algorithm the registry uses to identify a
#: declaration. The algorithm is recorded in the declaration so a
#: future version can switch without breaking existing hashes.
DEFAULT_DATASET_HASH_ALGORITHM: Final[str] = "sha256-v1"

#: Run mode levels that satisfy the T100 "below ``backtest`` is
#: rejected" acceptance clause. The membership's classification
#: decision must carry one of these levels or the registry refuses
#: to publish the dataset.
SUPPORT_LEVEL_BACKTEST_OR_ABOVE: Final[frozenset[str]] = frozenset(
    {RunMode.BACKTEST.value, RunMode.PAPER.value, RunMode.LIVE.value}
)

#: Token whose presence as a dataset's reporting numeraire token is
#: a hard error (the dataset must never convert a pool's own token
#: into itself; that would make the numeraire a self-reference).
RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN: Final[str] = "self"

#: Reason codes the registry surfaces when it has to label a dataset
#: ``RELATIVE_ONLY`` because no qualified route to USD exists. The
#: vocabulary is closed: a new code requires a contract amendment.
RELATIVE_ONLY_REJECTED_REASONS: Final[frozenset[str]] = frozenset(
    {
        "no_usdg_pair",
        "usdg_pair_qualification_expired",
        "stablecoin_pair_qualification_expired",
        "stablecoin_pair_depegged",
        "stale_valuation_source",
        "conflicting_valuation_source",
        "self_token_numeraire_refused",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DatasetError(RuntimeError):
    """Base class for research-dataset registry failures."""


class DatasetAlreadyExistsError(DatasetError):
    """The registry already holds a dataset version with this content hash.

    Two declarations that hash to the same value are the same
    declaration; the registry refuses the duplicate rather than
    silently merging it (``DS-002``).
    """


class DatasetMemberNotFoundError(DatasetError):
    """A pool identity is not present in the supplied research universe."""


class PoolNotResearchMemberError(DatasetMemberNotFoundError):
    """A dataset member is not present in the supplied research universe.

    The acceptance clause requires the member to be a research
    member; a pool added directly without a research-universe
    membership is rejected.
    """


class InvalidSupportLevelError(DatasetError):
    """A dataset member's classification decision is below ``backtest``."""


class InvertedBlockRangeError(DatasetError):
    """A member's block range is inverted, exceeds its observed life or spans a gap."""


class EmptyDatasetError(DatasetError):
    """A dataset declaration carries no members."""


class InactiveResearchDatasetError(DatasetError):
    """A query asked about a dataset version that was never registered."""


class UnknownNumeraireRouteError(DatasetError):
    """A numeraire level has no qualified route in the dataset's qualification bundle."""


# ---------------------------------------------------------------------------
# Versioning primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetId:
    """The string identifier the operator used to name a dataset.

    Two datasets with the same identifier but different
    declarations are two versions of the same dataset; two
    datasets with different identifiers are independent objects
    whose coverages may overlap (``DS-002``).
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value:
            raise DatasetError(f"DatasetId.value: must be non-empty str, got {self.value!r}")
        if any(c in self.value for c in ("\n", "\r", "\t", " ")):
            raise DatasetError(f"DatasetId.value: must not contain whitespace, got {self.value!r}")


@dataclass(frozen=True, slots=True)
class DatasetVersion:
    """A monotonically increasing version tag for one ``DatasetId``.

    Versions are assigned by the registry in insertion order; the
    same declaration (same content hash) cannot be inserted twice
    under the same ``DatasetId`` (``DS-002``).
    """

    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise DatasetError(
                f"DatasetVersion.value: must be int, got {type(self.value).__name__}"
            )
        if self.value < 1:
            raise DatasetError(f"DatasetVersion.value: must be >= 1, got {self.value}")


@dataclass(frozen=True, slots=True)
class DatasetRevision:
    """The ingestion and schema revision a partition carries.

    The T034 / T039 partitions the research dataset registry
    references carry a ``schema_version`` and a ``decode_version``
    (T030); the registry binds both so a research dataset
    declares the exact revisions its results depend on. A
    declaration that mixes revisions across members is allowed
    only when the operator explicitly declares the
    ``mixed_revisions=True`` flag (the dataset is then flagged
    in the acceptance verdict).
    """

    schema_version: int
    decode_version: int
    manifest_checksum: str

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise DatasetError(
                f"DatasetRevision.schema_version: must be int, got "
                f"{type(self.schema_version).__name__}"
            )
        if self.schema_version < 1:
            raise DatasetError(
                f"DatasetRevision.schema_version: must be >= 1, got {self.schema_version}"
            )
        if not isinstance(self.decode_version, int) or isinstance(self.decode_version, bool):
            raise DatasetError(
                f"DatasetRevision.decode_version: must be int, got "
                f"{type(self.decode_version).__name__}"
            )
        if self.decode_version < 1:
            raise DatasetError(
                f"DatasetRevision.decode_version: must be >= 1, got {self.decode_version}"
            )
        if not isinstance(self.manifest_checksum, str) or not self.manifest_checksum:
            raise DatasetError(
                "DatasetRevision.manifest_checksum: must be non-empty str, got "
                f"{self.manifest_checksum!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decode_version": self.decode_version,
            "manifest_checksum": self.manifest_checksum,
        }


# ---------------------------------------------------------------------------
# Block range
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawBlockRange:
    """The raw ``[start, end]`` inclusive range the operator requested.

    The framework does **not** widen this range; the registry
    rejects a range that exceeds the pool's observed life (its
    ``Initialize`` block to the run's agreed finalized end). The
    ``start_block`` is inclusive; ``end_block`` is inclusive.
    """

    start_block: int
    end_block: int

    def __post_init__(self) -> None:
        if not isinstance(self.start_block, int) or isinstance(self.start_block, bool):
            raise DatasetError(
                f"RawBlockRange.start_block: must be int, got {type(self.start_block).__name__}"
            )
        if self.start_block < 0:
            raise DatasetError(f"RawBlockRange.start_block: must be >= 0, got {self.start_block}")
        if not isinstance(self.end_block, int) or isinstance(self.end_block, bool):
            raise DatasetError(
                f"RawBlockRange.end_block: must be int, got {type(self.end_block).__name__}"
            )
        if self.end_block < self.start_block:
            raise InvertedBlockRangeError(
                f"RawBlockRange: end_block={self.end_block} < start_block={self.start_block}"
            )

    def width_blocks(self) -> int:
        """Inclusive width in blocks (``end - start + 1``)."""
        return self.end_block - self.start_block + 1

    def to_dict(self) -> dict[str, Any]:
        return {"start_block": self.start_block, "end_block": self.end_block}


@dataclass(frozen=True, slots=True)
class DatasetBlockRange:
    """The qualified block range a dataset member resolves to.

    ``requested`` is the operator-supplied range. ``observed_life``
    is the pool's own observed life (its ``Initialize`` block to
    the agreed finalized end). The registry refuses a request
    whose ``start`` predates ``observed_life.start`` or whose
    ``end`` exceeds ``observed_life.end``; it also refuses a
    range whose width is larger than the pool's observed life
    (a span that exceeds the pool's life is the "spans a gap"
    boundary case the contract names).
    """

    requested: RawBlockRange
    observed_life: RawBlockRange

    def __post_init__(self) -> None:
        if not isinstance(self.requested, RawBlockRange):
            raise DatasetError(
                f"DatasetBlockRange.requested: must be RawBlockRange, got "
                f"{type(self.requested).__name__}"
            )
        if not isinstance(self.observed_life, RawBlockRange):
            raise DatasetError(
                f"DatasetBlockRange.observed_life: must be RawBlockRange, got "
                f"{type(self.observed_life).__name__}"
            )
        if self.requested.start_block < self.observed_life.start_block:
            raise InvertedBlockRangeError(
                f"DatasetBlockRange: requested.start_block="
                f"{self.requested.start_block} is before the pool's "
                f"observed_life.start_block={self.observed_life.start_block}"
            )
        if self.requested.end_block > self.observed_life.end_block:
            raise InvertedBlockRangeError(
                f"DatasetBlockRange: requested.end_block="
                f"{self.requested.end_block} exceeds the pool's "
                f"observed_life.end_block={self.observed_life.end_block}"
            )
        if (
            self.requested.start_block > self.observed_life.end_block
            or self.requested.end_block < self.observed_life.start_block
        ):
            raise InvertedBlockRangeError(
                "DatasetBlockRange: requested range is disjoint from the pool's observed life"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested.to_dict(),
            "observed_life": self.observed_life.to_dict(),
            "width_blocks": self.requested.width_blocks(),
        }


# ---------------------------------------------------------------------------
# Numeraire provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NumeraireProvenance:
    """The provenance the dataset records for its reporting numeraire.

    The T100 contract generalises T053 point-in-time discipline:
    every dataset records the numeraire's source, its observed-at
    time, the available-at time, its staleness and its confidence.
    A ``RELATIVE_ONLY`` dataset records every observed but
    rejected numeraire level's provenance so a downstream consumer
    can audit why USD was not chosen (and why the dataset
    therefore carries no USD-denominated field anywhere).
    """

    level: NumeraireLevel
    source: str
    observed_at: int
    available_at: int
    staleness_seconds: int
    confidence: ConfidenceLevel
    stablecoin_per_usdg_q64_64: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.level, NumeraireLevel):
            raise DatasetError(
                f"NumeraireProvenance.level: must be NumeraireLevel, got "
                f"{type(self.level).__name__}"
            )
        if not isinstance(self.source, str) or not self.source:
            raise DatasetError(
                f"NumeraireProvenance.source: must be non-empty str, got {self.source!r}"
            )
        if not isinstance(self.observed_at, int) or isinstance(self.observed_at, bool):
            raise DatasetError(
                f"NumeraireProvenance.observed_at: must be int, got "
                f"{type(self.observed_at).__name__}"
            )
        if self.observed_at < 0:
            raise DatasetError(
                f"NumeraireProvenance.observed_at: must be >= 0, got {self.observed_at}"
            )
        if not isinstance(self.available_at, int) or isinstance(self.available_at, bool):
            raise DatasetError(
                f"NumeraireProvenance.available_at: must be int, got "
                f"{type(self.available_at).__name__}"
            )
        if self.available_at < self.observed_at:
            raise DatasetError(
                f"NumeraireProvenance.available_at={self.available_at} must be >= "
                f"observed_at={self.observed_at}"
            )
        if not isinstance(self.staleness_seconds, int) or isinstance(self.staleness_seconds, bool):
            raise DatasetError(
                f"NumeraireProvenance.staleness_seconds: must be int, got "
                f"{type(self.staleness_seconds).__name__}"
            )
        if self.staleness_seconds < 0:
            raise DatasetError(
                f"NumeraireProvenance.staleness_seconds: must be >= 0, got {self.staleness_seconds}"
            )
        if not isinstance(self.confidence, ConfidenceLevel):
            raise DatasetError(
                f"NumeraireProvenance.confidence: must be ConfidenceLevel, got "
                f"{type(self.confidence).__name__}"
            )
        if self.stablecoin_per_usdg_q64_64 is not None and (
            not isinstance(self.stablecoin_per_usdg_q64_64, int)
            or isinstance(self.stablecoin_per_usdg_q64_64, bool)
            or self.stablecoin_per_usdg_q64_64 <= 0
        ):
            raise DatasetError(
                "NumeraireProvenance.stablecoin_per_usdg_q64_64: must be "
                "positive int, got "
                f"{self.stablecoin_per_usdg_q64_64!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "level": self.level.value,
            "source": self.source,
            "observed_at": self.observed_at,
            "available_at": self.available_at,
            "staleness_seconds": self.staleness_seconds,
            "confidence": self.confidence.value,
        }
        if self.stablecoin_per_usdg_q64_64 is not None:
            out["stablecoin_per_usdg_q64_64"] = self.stablecoin_per_usdg_q64_64
        if self.notes:
            out["notes"] = list(self.notes)
        return out


# ---------------------------------------------------------------------------
# Dataset member
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetMember:
    """One ``(chain_id, PoolKey)`` member of a dataset declaration.

    The member binds the pool's block range, the partitions it
    resolves to and the dataset revision those partitions carry.
    The ``classification`` field records the
    :class:`ResearchClassificationDecision` the T027 classifier
    emitted for the pool at registration time, so the dataset
    registry can refuse a pool whose level is below
    ``backtest`` with the classifier's named reason.
    """

    chain_id: int
    pool_key: PoolKey
    block_range: DatasetBlockRange
    partitions: tuple[str, ...]
    revision: DatasetRevision
    classification: ResearchClassificationDecision
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise DatasetError(
                f"DatasetMember.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise DatasetError(f"DatasetMember.chain_id: must be > 0, got {self.chain_id}")
        if not isinstance(self.pool_key, PoolKey):
            raise DatasetError(
                f"DatasetMember.pool_key: must be PoolKey, got {type(self.pool_key).__name__}"
            )
        if not isinstance(self.block_range, DatasetBlockRange):
            raise DatasetError(
                f"DatasetMember.block_range: must be DatasetBlockRange, got "
                f"{type(self.block_range).__name__}"
            )
        if not isinstance(self.partitions, tuple):
            raise DatasetError(
                f"DatasetMember.partitions: must be tuple[str, ...], got "
                f"{type(self.partitions).__name__}"
            )
        if not self.partitions:
            raise DatasetError("DatasetMember.partitions: must be a non-empty tuple")
        for partition_id in self.partitions:
            if not isinstance(partition_id, str) or not partition_id:
                raise DatasetError(
                    f"DatasetMember.partitions: every partition_id must be "
                    f"non-empty str, got {partition_id!r}"
                )
        if not isinstance(self.revision, DatasetRevision):
            raise DatasetError(
                f"DatasetMember.revision: must be DatasetRevision, got "
                f"{type(self.revision).__name__}"
            )
        if not isinstance(self.classification, ResearchClassificationDecision):
            raise DatasetError(
                f"DatasetMember.classification: must be "
                f"ResearchClassificationDecision, got "
                f"{type(self.classification).__name__}"
            )
        if not isinstance(self.notes, str):
            raise DatasetError(f"DatasetMember.notes: must be str, got {type(self.notes).__name__}")
        # The classifier must classify the same pool as the member.
        if self.classification.chain_id != self.chain_id:
            raise DatasetError(
                "DatasetMember.classification: chain_id mismatch "
                f"({self.classification.chain_id} != {self.chain_id})"
            )
        if self.classification.pool_key != self.pool_key:
            raise DatasetError("DatasetMember.classification: PoolKey mismatch with member")

    @property
    def pool_id(self) -> PoolId:
        """The V4 ``PoolId`` derived from the member's ``PoolKey``."""
        return self.pool_key.to_pool_id()

    @property
    def support_level(self) -> RunMode:
        """The classification's ``RunMode`` level (T100 acceptance gate)."""
        return self.classification.level

    def is_backtest_eligible(self) -> bool:
        """``True`` iff the classifier says ``backtest`` or above."""
        return self.support_level.value in SUPPORT_LEVEL_BACKTEST_OR_ABOVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "pool_id": self.pool_id.to_hex(),
            "currency0": self.pool_key.currency0.to_address().to_hex(),
            "currency1": self.pool_key.currency1.to_address().to_hex(),
            "fee": self.pool_key.fee,
            "tick_spacing": self.pool_key.tick_spacing,
            "hooks": self.pool_key.hooks.to_hex(),
            "block_range": self.block_range.to_dict(),
            "partitions": list(self.partitions),
            "revision": self.revision.to_dict(),
            "support_level": self.support_level.value,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Dataset declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetDeclaration:
    """The deterministic declaration the registry hashes and stores.

    The declaration is the canonical, pre-hash shape of the
    dataset: identity, members, numeraire, provenance, schema
    version and hash algorithm. Two declarations that hash to
    the same value under the same algorithm are the same
    declaration; the registry refuses to store a duplicate.

    The declaration is a frozen value object: rebuilding it from
    the same inputs always yields a declaration with the same
    hash, so backtests, models and the console can reference the
    same object across runs and hosts.
    """

    dataset_id: DatasetId
    members: tuple[DatasetMember, ...]
    numeraire_level: NumeraireLevel
    numeraire_provenance: NumeraireProvenance
    qualification_bundle: QualificationBundle
    rationale: str
    notes: tuple[str, ...] = ()
    schema_version: int = DATASET_SCHEMA_VERSION
    hash_algorithm: str = DEFAULT_DATASET_HASH_ALGORITHM

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_id, DatasetId):
            raise DatasetError(
                f"DatasetDeclaration.dataset_id: must be DatasetId, got "
                f"{type(self.dataset_id).__name__}"
            )
        if not isinstance(self.members, tuple):
            raise DatasetError(
                f"DatasetDeclaration.members: must be tuple, got {type(self.members).__name__}"
            )
        if not self.members:
            raise EmptyDatasetError(
                "DatasetDeclaration.members: must be a non-empty tuple "
                "(empty datasets are rejected)"
            )
        seen_pool_ids: set[PoolId] = set()
        for member in self.members:
            if not isinstance(member, DatasetMember):
                raise DatasetError(
                    f"DatasetDeclaration.members: every entry must be "
                    f"DatasetMember, got {type(member).__name__}"
                )
            pid = member.pool_id
            if pid in seen_pool_ids:
                raise DatasetError(
                    f"DatasetDeclaration.members: duplicate pool_id "
                    f"{pid.to_hex()} (duplicates are rejected)"
                )
            seen_pool_ids.add(pid)
        if not isinstance(self.numeraire_level, NumeraireLevel):
            raise DatasetError(
                f"DatasetDeclaration.numeraire_level: must be NumeraireLevel, "
                f"got {type(self.numeraire_level).__name__}"
            )
        if not isinstance(self.numeraire_provenance, NumeraireProvenance):
            raise DatasetError(
                f"DatasetDeclaration.numeraire_provenance: must be "
                f"NumeraireProvenance, got "
                f"{type(self.numeraire_provenance).__name__}"
            )
        if self.numeraire_provenance.level is not self.numeraire_level:
            raise DatasetError(
                "DatasetDeclaration.numeraire_provenance.level "
                f"({self.numeraire_provenance.level.value}) must match "
                f"numeraire_level ({self.numeraire_level.value})"
            )
        if not isinstance(self.qualification_bundle, QualificationBundle):
            raise DatasetError(
                f"DatasetDeclaration.qualification_bundle: must be "
                f"QualificationBundle, got "
                f"{type(self.qualification_bundle).__name__}"
            )
        if self.qualification_bundle.selected.level is not self.numeraire_level:
            raise DatasetError(
                "DatasetDeclaration.qualification_bundle.selected.level "
                f"({self.qualification_bundle.selected.level.value}) must "
                f"match numeraire_level ({self.numeraire_level.value})"
            )
        if not isinstance(self.rationale, str) or not self.rationale:
            raise DatasetError(
                f"DatasetDeclaration.rationale: must be non-empty str, got {self.rationale!r}"
            )
        if not isinstance(self.notes, tuple):
            raise DatasetError(
                f"DatasetDeclaration.notes: must be tuple[str, ...], got "
                f"{type(self.notes).__name__}"
            )
        for note in self.notes:
            if not isinstance(note, str):
                raise DatasetError(
                    f"DatasetDeclaration.notes: every entry must be str, got {type(note).__name__}"
                )
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise DatasetError(
                f"DatasetDeclaration.schema_version: must be int, got "
                f"{type(self.schema_version).__name__}"
            )
        if self.schema_version < 1:
            raise DatasetError(
                f"DatasetDeclaration.schema_version: must be >= 1, got {self.schema_version}"
            )
        if not isinstance(self.hash_algorithm, str) or not self.hash_algorithm:
            raise DatasetError(
                f"DatasetDeclaration.hash_algorithm: must be non-empty str, "
                f"got {self.hash_algorithm!r}"
            )

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def is_relative_only(self) -> bool:
        """``True`` iff the dataset reports ``RELATIVE_ONLY`` results only."""
        return self.numeraire_level is NumeraireLevel.RELATIVE_ONLY

    def partitions(self) -> tuple[str, ...]:
        """The ordered tuple of every partition_id the declaration references."""
        out: list[str] = []
        for member in self.members:
            out.extend(member.partitions)
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hash_algorithm": self.hash_algorithm,
            "dataset_id": self.dataset_id.value,
            "members": [m.to_dict() for m in self.members],
            "numeraire_level": self.numeraire_level.value,
            "numeraire_provenance": self.numeraire_provenance.to_dict(),
            "qualification_bundle": {
                "selected_level": self.qualification_bundle.selected.level.value,
                "selected_rationale": self.qualification_bundle.selected.rationale,
                "records": [
                    {
                        "level": r.level.value,
                        "selected": r.selected,
                        "rationale": r.rationale,
                        "confidence": r.confidence.value,
                        "staleness_seconds": r.staleness_seconds,
                        "stablecoin_per_usdg_q64_64": r.stablecoin_per_usdg_q64_64,
                        "notes": list(r.notes),
                    }
                    for r in self.qualification_bundle.records
                ],
            },
            "rationale": self.rationale,
            "notes": list(self.notes),
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the declaration deterministically for hashing.

        The serialisation uses sorted keys and a fixed key order
        so two declarations that produce identical
        :meth:`to_dict` outputs always produce identical bytes
        and therefore identical hashes (the acceptance clause's
        "byte-identical across runs and hosts after excluding
        observational fields").
        """
        payload = self.to_dict()
        canonical = _canonicalise(payload)
        return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


class DatasetContentHasher:
    """Content-addressed hasher for :class:`DatasetDeclaration`.

    The hasher is parameterised by an algorithm string recorded in
    the declaration; the default uses SHA-256 ("sha256-v1") which
    is what the T100 acceptance clause's "byte-identical across
    runs and hosts" requirement relies on. Tests inject a fake
    hasher so the deterministic check does not depend on the
    stdlib hash implementation.
    """

    algorithm: str

    def __init__(self, algorithm: str = DEFAULT_DATASET_HASH_ALGORITHM) -> None:
        if not isinstance(algorithm, str) or not algorithm:
            raise DatasetError(
                f"DatasetContentHasher: algorithm must be non-empty str, got {algorithm!r}"
            )
        self.algorithm = algorithm

    def hash_bytes(self, payload: bytes) -> str:
        """Hash ``payload`` and return the 0x-hex digest string."""
        if not isinstance(payload, (bytes, bytearray)):
            raise DatasetError(
                f"DatasetContentHasher.hash_bytes: payload must be bytes, got "
                f"{type(payload).__name__}"
            )
        if self.algorithm == DEFAULT_DATASET_HASH_ALGORITHM:
            return "0x" + hashlib.sha256(bytes(payload)).hexdigest()
        # The default algorithm is the only algorithm the framework
        # recognises today; any other value is a contract change
        # rather than a configuration option.
        raise DatasetError(
            f"DatasetContentHasher: unknown algorithm {self.algorithm!r}; the "
            "framework only recognises sha256-v1 today"
        )


def default_content_hasher() -> DatasetContentHasher:
    """Return the framework's default :class:`DatasetContentHasher`."""
    return DatasetContentHasher(DEFAULT_DATASET_HASH_ALGORITHM)


def hash_dataset_declaration(
    declaration: DatasetDeclaration,
    *,
    hasher: DatasetContentHasher | None = None,
) -> str:
    """Return the content hash of ``declaration``.

    The hash is recorded on every :class:`DatasetRegistrySnapshot`
    the registry stores; two datasets whose declarations hash to
    the same value are the same dataset.
    """
    if not isinstance(declaration, DatasetDeclaration):
        raise DatasetError(
            f"hash_dataset_declaration: declaration must be DatasetDeclaration, "
            f"got {type(declaration).__name__}"
        )
    effective_hasher = hasher if hasher is not None else default_content_hasher()
    if not isinstance(effective_hasher, DatasetContentHasher):
        raise DatasetError(
            f"hash_dataset_declaration: hasher must be DatasetContentHasher, "
            f"got {type(effective_hasher).__name__}"
        )
    if effective_hasher.algorithm != declaration.hash_algorithm:
        raise DatasetError(
            f"hash_dataset_declaration: hasher algorithm "
            f"{effective_hasher.algorithm!r} != declaration.hash_algorithm "
            f"{declaration.hash_algorithm!r}"
        )
    return effective_hasher.hash_bytes(declaration.to_canonical_bytes())


# ---------------------------------------------------------------------------
# Numeraire routing
# ---------------------------------------------------------------------------


def validate_numeraire_route(
    declaration: DatasetDeclaration,
) -> str | None:
    """Validate the numeraire route declared on ``declaration``.

    Returns the rejection reason code when the declaration must
    be reported ``RELATIVE_ONLY`` because no qualified route to
    USD exists; returns ``None`` when the declared numeraire has
    a qualified route. The function never silently rewrites the
    declared numeraire: a declaration that asks for ``USDG``
    with an expired qualification is refused, not converted to
    ``QUALIFIED_USD_STABLECOIN`` (ADR-014 §3).

    The boundary cases covered:

    - a numeraire level that is neither present in the
      qualification bundle nor provided as the provenance
      (returns ``unknown_numeraire_route``);
    - a numeraire that is the pool's own token
      (returns ``self_token_numeraire_refused``);
    - a stablecoin or USDG pair whose qualification has
      expired (``*_qualification_expired``);
    - a stablecoin that has depegged
      (``stablecoin_pair_depegged``);
    - a stale or conflicting valuation source
      (``stale_valuation_source`` /
      ``conflicting_valuation_source``);
    - a stablecoin that cannot prove the ``1 USD == 1 USDG``
      assumption (the recorded ratio is ``None`` or
      non-positive; the dataset is reported ``RELATIVE_ONLY``).
    """
    if not isinstance(declaration, DatasetDeclaration):
        raise DatasetError(
            f"validate_numeraire_route: declaration must be DatasetDeclaration, "
            f"got {type(declaration).__name__}"
        )

    selected = declaration.qualification_bundle.selected
    if selected.level is not declaration.numeraire_level:
        return "unknown_numeraire_route"

    # A RELATIVE_ONLY declaration that does not record a
    # self-token source is the documented
    # "no qualified route to USD exists" boundary case; the
    # dataset is reported RELATIVE_ONLY with the appropriate
    # reason rather than refused outright.
    if declaration.numeraire_level is NumeraireLevel.RELATIVE_ONLY:
        provenance_source = declaration.numeraire_provenance.source
        if provenance_source == RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN:
            return RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN
        return "no_usdg_pair"

    # The provenance's confidence and staleness surface the
    # point-in-time discipline the dataset must record.
    # "expired" is modelled as ConfidenceLevel.LOW + positive
    # staleness above the provenance's recorded budget, the
    # documented signal that the qualification has lapsed
    # (T53's confidence enum does not define EXPIRED; the
    # T100 boundary case is captured by this combination).
    if (
        selected.confidence is ConfidenceLevel.LOW
        and selected.staleness_seconds > declaration.numeraire_provenance.staleness_seconds
    ):
        if declaration.numeraire_level is NumeraireLevel.USDG:
            return "usdg_pair_qualification_expired"
        if declaration.numeraire_level is NumeraireLevel.QUALIFIED_USD_STABLECOIN:
            return "stablecoin_pair_qualification_expired"
    if selected.staleness_seconds > declaration.numeraire_provenance.staleness_seconds:
        return "stale_valuation_source"
    if selected.confidence is ConfidenceLevel.LOW:
        return "conflicting_valuation_source"

    # A USDG- or stablecoin-denominated dataset must carry the
    # observed ratio; the framework never assumes ``1 USDG ==
    # 1 USD``. A missing or non-positive ratio is the
    # ``no_usdg_pair`` boundary case the contract names.
    if declaration.numeraire_level in (
        NumeraireLevel.USDG,
        NumeraireLevel.QUALIFIED_USD_STABLECOIN,
    ):
        ratio = declaration.numeraire_provenance.stablecoin_per_usdg_q64_64
        if ratio is None:
            return "no_usdg_pair"

    return None


# ---------------------------------------------------------------------------
# Acceptance verdict
# ---------------------------------------------------------------------------


class DatasetAcceptanceVerdictCode(StrEnum):
    """The codes the registry records for an acceptance verdict.

    The codes are the closed vocabulary the backtest harness,
    the model layer and the research console import. New codes
    are additive; renaming an existing code is a breaking
    change.
    """

    #: The declaration's members all satisfy ``backtest`` or
    #: above and the numeraire route is qualified. The dataset
    #: is ready for replay, backtest and model research.
    ACCEPTED = "accepted"
    #: The dataset reports ``RELATIVE_ONLY``: no qualified USD
    #: route exists, but the declaration is internally
    #: consistent. Backtest and model research may proceed;
    #: every USD-denominated field is forbidden on the result.
    RELATIVE_ONLY = "relative_only"
    #: At least one member's classification is below
    #: ``backtest``. The dataset is refused with the
    #: classifier's named reason.
    REJECTED_SUPPORT_LEVEL = "rejected_support_level"
    #: At least one member is not present in the supplied
    #: research universe.
    REJECTED_NOT_RESEARCH_MEMBER = "rejected_not_research_member"
    #: The numeraire route is unknown or conflicting.
    REJECTED_NUMERAIRE_ROUTE = "rejected_numeraire_route"
    #: The numeraire token is the pool's own token; the
    #: dataset would be measuring itself against itself.
    REJECTED_SELF_TOKEN_NUMERAIRE = "rejected_self_token_numeraire"
    #: The block range is inverted, exceeds the pool's
    #: observed life or spans a gap.
    REJECTED_BLOCK_RANGE = "rejected_block_range"
    #: The declaration is empty.
    REJECTED_EMPTY = "rejected_empty"


@dataclass(frozen=True, slots=True)
class DatasetAcceptanceVerdict:
    """The acceptance verdict the registry assigns to a declaration.

    The verdict is a value object: tests construct it directly
    from deterministic inputs and the registry constructs one
    when it stores a dataset version. The verdict records the
    code, the human-readable rationale, the rejected members
    and the stablecoin / numeraire route reason.

    A verdict with :attr:`DatasetAcceptanceVerdictCode.ACCEPTED`
    or :attr:`DatasetAcceptanceVerdictCode.RELATIVE_ONLY` admits
    the declaration for replay, backtest and model research; any
    other code refuses it.
    """

    code: DatasetAcceptanceVerdictCode
    rationale: str
    relative_only_reason: str | None = None
    rejected_members: tuple[str, ...] = ()
    rejected_reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, DatasetAcceptanceVerdictCode):
            raise DatasetError(
                f"DatasetAcceptanceVerdict.code: must be "
                f"DatasetAcceptanceVerdictCode, got "
                f"{type(self.code).__name__}"
            )
        if not isinstance(self.rationale, str) or not self.rationale:
            raise DatasetError(
                f"DatasetAcceptanceVerdict.rationale: must be non-empty str, got {self.rationale!r}"
            )
        if self.relative_only_reason is not None and not isinstance(self.relative_only_reason, str):
            raise DatasetError(
                "DatasetAcceptanceVerdict.relative_only_reason: must be str "
                f"or None, got {type(self.relative_only_reason).__name__}"
            )
        if not isinstance(self.rejected_members, tuple):
            raise DatasetError(
                f"DatasetAcceptanceVerdict.rejected_members: must be tuple, "
                f"got {type(self.rejected_members).__name__}"
            )
        for pool_id in self.rejected_members:
            if not isinstance(pool_id, str) or not pool_id:
                raise DatasetError(
                    "DatasetAcceptanceVerdict.rejected_members: every entry "
                    f"must be non-empty str, got {pool_id!r}"
                )
        if not isinstance(self.rejected_reason_codes, tuple):
            raise DatasetError(
                "DatasetAcceptanceVerdict.rejected_reason_codes: must be "
                f"tuple, got {type(self.rejected_reason_codes).__name__}"
            )
        for code in self.rejected_reason_codes:
            if not isinstance(code, str) or not code:
                raise DatasetError(
                    "DatasetAcceptanceVerdict.rejected_reason_codes: every "
                    f"entry must be non-empty str, got {code!r}"
                )

    @property
    def is_admitted(self) -> bool:
        """``True`` iff the dataset is admitted for replay / backtest / research."""
        return self.code in (
            DatasetAcceptanceVerdictCode.ACCEPTED,
            DatasetAcceptanceVerdictCode.RELATIVE_ONLY,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code.value,
            "rationale": self.rationale,
            "admitted": self.is_admitted,
        }
        if self.relative_only_reason is not None:
            out["relative_only_reason"] = self.relative_only_reason
        if self.rejected_members:
            out["rejected_members"] = list(self.rejected_members)
        if self.rejected_reason_codes:
            out["rejected_reason_codes"] = list(self.rejected_reason_codes)
        return out


def build_dataset_acceptance_verdict(
    declaration: DatasetDeclaration,
    *,
    research_universe: ResearchUniverse | None = None,
) -> DatasetAcceptanceVerdict:
    """Compute the acceptance verdict for ``declaration``.

    The function is the deterministic, offline verifier the
    registry runs before admitting a declaration. It applies the
    contract's acceptance clauses:

    1. empty declaration -> rejected;
    2. members not present in the supplied research universe
       (when one is supplied) -> rejected with the pool's
       identity;
    3. members whose classification decision is below
       ``backtest`` -> rejected with the classifier's named
       reason;
    4. members with an inverted, exceeded or gapped block
       range -> rejected (the validator on
       :class:`DatasetBlockRange` already enforces this; the
       verdict names the failure);
    5. numeraire route that is unknown, self-referential,
       expired, depegged, stale or conflicting ->
       reported ``RELATIVE_ONLY`` when the rejection reason is
       compatible with ``RELATIVE_ONLY_REJECTED_REASONS``;
       rejected outright otherwise;
    6. otherwise accepted.

    The function does **not** import the registry: tests call
    it on a declaration in isolation, then construct the
    registry manually. The function never mutates its inputs.
    """
    if not isinstance(declaration, DatasetDeclaration):
        raise DatasetError(
            f"build_dataset_acceptance_verdict: declaration must be "
            f"DatasetDeclaration, got {type(declaration).__name__}"
        )
    if research_universe is not None and not isinstance(research_universe, ResearchUniverse):
        raise DatasetError(
            f"build_dataset_acceptance_verdict: research_universe must be "
            f"ResearchUniverse or None, got {type(research_universe).__name__}"
        )

    if not declaration.members:
        return DatasetAcceptanceVerdict(
            code=DatasetAcceptanceVerdictCode.REJECTED_EMPTY,
            rationale="dataset declaration carries no members",
            rejected_reason_codes=("empty_dataset",),
        )

    rejected_members: list[str] = []
    rejected_reason_codes: list[str] = []
    try:
        assert_research_member_backtest_eligible(
            declaration.members[0].classification,
            use_case="backtest",
        )
    except ResearchSupportGateError as exc:
        # This branch is a defensive check; the per-member loop below
        # also catches the same condition and emits the rejected pool
        # id, which the verdict requires.
        rejected_reason_codes.append("below_backtest_support_level")
        rejected_reason_codes.append(str(exc))

    for member in declaration.members:
        if research_universe is not None:
            try:
                research_universe.get(member.pool_id)
            except UnknownResearchMemberError:
                rejected_members.append(member.pool_id.to_hex())
                rejected_reason_codes.append("not_research_member")
                continue
        try:
            assert_research_member_backtest_eligible(member.classification, use_case="backtest")
        except ResearchSupportGateError as exc:
            rejected_members.append(member.pool_id.to_hex())
            rejected_reason_codes.append(str(exc))
            continue
        try:
            member.block_range  # noqa: B018 — validation happens in __post_init__
        except InvertedBlockRangeError as exc:
            rejected_members.append(member.pool_id.to_hex())
            rejected_reason_codes.append(str(exc))
            continue

    if rejected_members:
        return DatasetAcceptanceVerdict(
            code=DatasetAcceptanceVerdictCode.REJECTED_NOT_RESEARCH_MEMBER
            if any(code == "not_research_member" for code in rejected_reason_codes)
            else DatasetAcceptanceVerdictCode.REJECTED_SUPPORT_LEVEL,
            rationale=("dataset members failed research-universe or support-level qualification"),
            rejected_members=tuple(rejected_members),
            rejected_reason_codes=tuple(rejected_reason_codes),
        )

    relative_reason = validate_numeraire_route(declaration)
    if relative_reason == RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN:
        return DatasetAcceptanceVerdict(
            code=DatasetAcceptanceVerdictCode.REJECTED_SELF_TOKEN_NUMERAIRE,
            rationale=("dataset numeraire is the pool's own token (self-token numeraire refused)"),
            rejected_reason_codes=(RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN,),
        )
    if relative_reason is not None and relative_reason not in RELATIVE_ONLY_REJECTED_REASONS:
        return DatasetAcceptanceVerdict(
            code=DatasetAcceptanceVerdictCode.REJECTED_NUMERAIRE_ROUTE,
            rationale=(f"dataset numeraire route refused with reason {relative_reason!r}"),
            rejected_reason_codes=(relative_reason,),
        )
    if relative_reason is not None:
        return DatasetAcceptanceVerdict(
            code=DatasetAcceptanceVerdictCode.RELATIVE_ONLY,
            rationale=(
                "dataset has no qualified route to a USD family asset; "
                "results will be reported relative-only"
            ),
            relative_only_reason=relative_reason,
        )

    return DatasetAcceptanceVerdict(
        code=DatasetAcceptanceVerdictCode.ACCEPTED,
        rationale="all members qualified and numeraire route accepted",
    )


# ---------------------------------------------------------------------------
# Dataset candidate (the input the registry hashes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetCandidate:
    """A candidate declaration paired with its verdict and content hash.

    The candidate is the artefact the registry stores: the
    declaration (which the hash keys on), the content hash, and
    the verdict the acceptance function produced. The candidate
    is a frozen value object so re-publishing the same
    declaration produces the same candidate.
    """

    declaration: DatasetDeclaration
    content_hash: str
    verdict: DatasetAcceptanceVerdict

    def __post_init__(self) -> None:
        if not isinstance(self.declaration, DatasetDeclaration):
            raise DatasetError(
                f"DatasetCandidate.declaration: must be DatasetDeclaration, "
                f"got {type(self.declaration).__name__}"
            )
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise DatasetError(
                f"DatasetCandidate.content_hash: must be non-empty str, got {self.content_hash!r}"
            )
        if not isinstance(self.verdict, DatasetAcceptanceVerdict):
            raise DatasetError(
                f"DatasetCandidate.verdict: must be DatasetAcceptanceVerdict, "
                f"got {type(self.verdict).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "verdict": self.verdict.to_dict(),
            "declaration": self.declaration.to_dict(),
        }


# ---------------------------------------------------------------------------
# Registry snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetRegistrySnapshot:
    """One immutable snapshot of a registered dataset version.

    The snapshot binds the dataset id, its assigned version, the
    content hash, the verdict and the registry insertion time.
    The insertion time is recorded as an integer (Unix
    nanoseconds, for example) so the snapshot can be hashed
    deterministically when needed; the registry never uses wall
    clock on the protocol / dataset / acceptance paths.
    """

    dataset_id: DatasetId
    version: DatasetVersion
    content_hash: str
    verdict: DatasetAcceptanceVerdict
    declaration: DatasetDeclaration
    inserted_at_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_id, DatasetId):
            raise DatasetError(
                f"DatasetRegistrySnapshot.dataset_id: must be DatasetId, "
                f"got {type(self.dataset_id).__name__}"
            )
        if not isinstance(self.version, DatasetVersion):
            raise DatasetError(
                f"DatasetRegistrySnapshot.version: must be DatasetVersion, "
                f"got {type(self.version).__name__}"
            )
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise DatasetError(
                f"DatasetRegistrySnapshot.content_hash: must be non-empty "
                f"str, got {self.content_hash!r}"
            )
        if not isinstance(self.verdict, DatasetAcceptanceVerdict):
            raise DatasetError(
                f"DatasetRegistrySnapshot.verdict: must be "
                f"DatasetAcceptanceVerdict, got "
                f"{type(self.verdict).__name__}"
            )
        if not isinstance(self.declaration, DatasetDeclaration):
            raise DatasetError(
                f"DatasetRegistrySnapshot.declaration: must be "
                f"DatasetDeclaration, got "
                f"{type(self.declaration).__name__}"
            )
        if not isinstance(self.inserted_at_ns, int) or isinstance(self.inserted_at_ns, bool):
            raise DatasetError(
                f"DatasetRegistrySnapshot.inserted_at_ns: must be int, got "
                f"{type(self.inserted_at_ns).__name__}"
            )
        if self.inserted_at_ns < 0:
            raise DatasetError(
                f"DatasetRegistrySnapshot.inserted_at_ns: must be >= 0, got {self.inserted_at_ns}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id.value,
            "version": self.version.value,
            "content_hash": self.content_hash,
            "verdict": self.verdict.to_dict(),
            "declaration": self.declaration.to_dict(),
            "inserted_at_ns": self.inserted_at_ns,
        }


# ---------------------------------------------------------------------------
# Registry query surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetRegistryQuery:
    """A read-only query against the registry.

    The query surface answers "what datasets exist?", "what does
    each cover?", "which pools and ranges does it contain?" and
    "what is its qualification status?". The query is itself a
    value object: tests construct it directly, and the registry
    returns a deterministic sequence of snapshots that match.
    """

    dataset_id: DatasetId | None = None
    pool_id: PoolId | None = None
    include_rejected: bool = False
    numeraire_level: NumeraireLevel | None = None

    def __post_init__(self) -> None:
        if self.dataset_id is not None and not isinstance(self.dataset_id, DatasetId):
            raise DatasetError(
                f"DatasetRegistryQuery.dataset_id: must be DatasetId or None, "
                f"got {type(self.dataset_id).__name__}"
            )
        if self.pool_id is not None and not isinstance(self.pool_id, PoolId):
            raise DatasetError(
                f"DatasetRegistryQuery.pool_id: must be PoolId or None, "
                f"got {type(self.pool_id).__name__}"
            )
        if not isinstance(self.include_rejected, bool):
            raise DatasetError(
                f"DatasetRegistryQuery.include_rejected: must be bool, got "
                f"{type(self.include_rejected).__name__}"
            )
        if self.numeraire_level is not None and not isinstance(
            self.numeraire_level, NumeraireLevel
        ):
            raise DatasetError(
                f"DatasetRegistryQuery.numeraire_level: must be NumeraireLevel "
                f"or None, got {type(self.numeraire_level).__name__}"
            )


# ---------------------------------------------------------------------------
# Dataset registry
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DatasetRegistry:
    """The in-memory registry of :class:`DatasetRegistrySnapshot` rows.

    The registry is the storage-layer surface that owns the
    versioning discipline: insertion is append-only, every
    snapshot is byte-identical across runs and hosts (after
    excluding the declared observational fields), and the
    query surface answers the contract's acceptance questions.

    The registry is **disjoint** from the active execution pool
    collection: a research dataset cannot appear as the active
    pool, and the active pool configuration is never imported
    here. The active pool's ``HOLD``/``LP``/``AUTO_SWAP``
    authority is owned by a different module; this registry
    grants no such authority.
    """

    chain_id: ChainId
    _snapshots: list[DatasetRegistrySnapshot] = field(default_factory=list)
    _by_content_hash: dict[str, DatasetRegistrySnapshot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise DatasetError(
                f"DatasetRegistry.chain_id: must be ChainId, got {type(self.chain_id).__name__}"
            )

    @property
    def size(self) -> int:
        """Number of stored snapshots (versions), all datasets combined."""
        return len(self._snapshots)

    @property
    def snapshots(self) -> tuple[DatasetRegistrySnapshot, ...]:
        """Tuple of every stored snapshot in insertion order."""
        return tuple(self._snapshots)

    def publish(
        self,
        declaration: DatasetDeclaration,
        *,
        research_universe: ResearchUniverse | None = None,
        inserted_at_ns: int = 0,
        hasher: DatasetContentHasher | None = None,
    ) -> DatasetRegistrySnapshot:
        """Publish ``declaration`` as a new dataset version.

        The function is the only path that mutates the registry:
        it hashes the declaration, computes the verdict, refuses
        rejected declarations, and inserts the snapshot. The
        function does not mutate any member's classification
        decision; it does not import the config or execution
        layers; it does not grant execution authority.

        Two datasets with the same content hash are the same
        dataset: re-publishing the same declaration under the
        same identifier raises :class:`DatasetAlreadyExistsError`
        rather than silently re-inserting it (``DS-002``).
        """
        if not isinstance(declaration, DatasetDeclaration):
            raise DatasetError(
                f"DatasetRegistry.publish: declaration must be "
                f"DatasetDeclaration, got {type(declaration).__name__}"
            )
        if declaration.dataset_id.value == "":
            raise DatasetError("DatasetRegistry.publish: dataset_id must be non-empty")
        if declaration.members and any(
            m.chain_id != self.chain_id.value for m in declaration.members
        ):
            raise DatasetError(
                f"DatasetRegistry.publish: dataset members must share the "
                f"registry's chain_id={self.chain_id.value}"
            )
        if not isinstance(inserted_at_ns, int) or isinstance(inserted_at_ns, bool):
            raise DatasetError(
                f"DatasetRegistry.publish: inserted_at_ns must be int, got "
                f"{type(inserted_at_ns).__name__}"
            )
        if inserted_at_ns < 0:
            raise DatasetError(
                f"DatasetRegistry.publish: inserted_at_ns must be >= 0, got {inserted_at_ns}"
            )
        if research_universe is not None and not isinstance(research_universe, ResearchUniverse):
            raise DatasetError(
                f"DatasetRegistry.publish: research_universe must be "
                f"ResearchUniverse or None, got "
                f"{type(research_universe).__name__}"
            )

        content_hash = hash_dataset_declaration(declaration, hasher=hasher)
        if content_hash in self._by_content_hash:
            raise DatasetAlreadyExistsError(
                f"DatasetRegistry.publish: content_hash {content_hash} already "
                "registered (immutable / versioning rule DS-002)"
            )
        verdict = build_dataset_acceptance_verdict(declaration, research_universe=research_universe)
        if not verdict.is_admitted:
            raise DatasetError(
                f"DatasetRegistry.publish: declaration refused with code "
                f"{verdict.code.value!r}; "
                f"rationale={verdict.rationale!r}"
            )

        # Versioning: a new declaration under an existing
        # ``DatasetId`` increments the version tag; a brand-new
        # identifier starts at version 1.
        existing_versions = [
            snapshot.version.value
            for snapshot in self._snapshots
            if snapshot.dataset_id.value == declaration.dataset_id.value
        ]
        next_version = max(existing_versions, default=0) + 1
        snapshot = DatasetRegistrySnapshot(
            dataset_id=declaration.dataset_id,
            version=DatasetVersion(next_version),
            content_hash=content_hash,
            verdict=verdict,
            declaration=declaration,
            inserted_at_ns=inserted_at_ns,
        )
        self._snapshots.append(snapshot)
        self._by_content_hash[content_hash] = snapshot
        return snapshot

    def get_by_content_hash(self, content_hash: str) -> DatasetRegistrySnapshot:
        """Return the snapshot whose content hash equals ``content_hash``.

        Raises :class:`InactiveResearchDatasetError` if the hash
        is unknown; the registry never silently returns an empty
        result for an unregistered content hash.
        """
        if not isinstance(content_hash, str) or not content_hash:
            raise DatasetError(
                f"DatasetRegistry.get_by_content_hash: content_hash must be "
                f"non-empty str, got {content_hash!r}"
            )
        try:
            return self._by_content_hash[content_hash]
        except KeyError as exc:
            raise InactiveResearchDatasetError(
                f"DatasetRegistry.get_by_content_hash: no dataset registered "
                f"with content_hash={content_hash!r}"
            ) from exc

    def versions_for(self, dataset_id: DatasetId) -> tuple[DatasetRegistrySnapshot, ...]:
        """Return every stored snapshot for ``dataset_id`` in insertion order."""
        if not isinstance(dataset_id, DatasetId):
            raise DatasetError(
                f"DatasetRegistry.versions_for: dataset_id must be DatasetId, "
                f"got {type(dataset_id).__name__}"
            )
        return tuple(
            snapshot
            for snapshot in self._snapshots
            if snapshot.dataset_id.value == dataset_id.value
        )

    def query(self, query: DatasetRegistryQuery) -> tuple[DatasetRegistrySnapshot, ...]:
        """Return every stored snapshot that satisfies ``query``.

        The query surface is deterministic: the same registry
        state and the same query always return the same sequence
        of snapshots in insertion order.
        """
        if not isinstance(query, DatasetRegistryQuery):
            raise DatasetError(
                f"DatasetRegistry.query: query must be DatasetRegistryQuery, "
                f"got {type(query).__name__}"
            )

        def _matches(snapshot: DatasetRegistrySnapshot) -> bool:
            if query.dataset_id is not None and snapshot.dataset_id.value != query.dataset_id.value:
                return False
            if (
                query.numeraire_level is not None
                and snapshot.declaration.numeraire_level is not query.numeraire_level
            ):
                return False
            if not query.include_rejected and not snapshot.verdict.is_admitted:
                return False
            return not (
                query.pool_id is not None
                and not any(
                    member.pool_id.value == query.pool_id.value
                    for member in snapshot.declaration.members
                )
            )

        return tuple(snapshot for snapshot in self._snapshots if _matches(snapshot))

    def datasets_covering_partition(self, partition_id: str) -> tuple[DatasetRegistrySnapshot, ...]:
        """Return every snapshot whose members reference ``partition_id``.

        Two datasets covering the same partitions remain
        isolated; the function returns every such dataset so the
        consumer can attribute each result to the dataset that
        owns it (``DS-002``).
        """
        if not isinstance(partition_id, str) or not partition_id:
            raise DatasetError(
                f"DatasetRegistry.datasets_covering_partition: partition_id "
                f"must be non-empty str, got {partition_id!r}"
            )
        return tuple(
            snapshot
            for snapshot in self._snapshots
            if any(partition_id in member.partitions for member in snapshot.declaration.members)
        )

    def datasets_covering_pool(self, pool_id: PoolId) -> tuple[DatasetRegistrySnapshot, ...]:
        """Return every snapshot whose members include ``pool_id``."""
        if not isinstance(pool_id, PoolId):
            raise DatasetError(
                f"DatasetRegistry.datasets_covering_pool: pool_id must be "
                f"PoolId, got {type(pool_id).__name__}"
            )
        return tuple(
            snapshot
            for snapshot in self._snapshots
            if any(member.pool_id.value == pool_id.value for member in snapshot.declaration.members)
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonicalise(value: Any) -> Any:
    """Return a deep-copied, deterministic representation of ``value``.

    Tuples become lists, mapping values have their keys sorted
    recursively, and every other value is left as-is. The
    resulting tree is the JSON-encoded deterministic shape the
    hash function consumes.
    """
    if isinstance(value, Mapping):
        return {key: _canonicalise(value[key]) for key in sorted(value.keys())}
    if isinstance(value, tuple):
        return [_canonicalise(v) for v in value]
    if isinstance(value, list):
        return [_canonicalise(v) for v in value]
    if isinstance(value, frozenset):
        return sorted(_canonicalise(v) for v in value)
    return value


__all__ = [
    "DATASET_SCHEMA_VERSION",
    "DEFAULT_DATASET_HASH_ALGORITHM",
    "DatasetAcceptanceVerdict",
    "DatasetAcceptanceVerdictCode",
    "DatasetAlreadyExistsError",
    "DatasetBlockRange",
    "DatasetCandidate",
    "DatasetContentHasher",
    "DatasetDeclaration",
    "DatasetError",
    "DatasetId",
    "DatasetMember",
    "DatasetMemberNotFoundError",
    "DatasetRegistry",
    "DatasetRegistryQuery",
    "DatasetRegistrySnapshot",
    "DatasetRevision",
    "DatasetVersion",
    "EmptyDatasetError",
    "InactiveResearchDatasetError",
    "InvalidSupportLevelError",
    "InvertedBlockRangeError",
    "NumeraireProvenance",
    "PoolNotResearchMemberError",
    "RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN",
    "RELATIVE_ONLY_REJECTED_REASONS",
    "RawBlockRange",
    "SUPPORT_LEVEL_BACKTEST_OR_ABOVE",
    "UnknownNumeraireRouteError",
    "build_dataset_acceptance_verdict",
    "default_content_hasher",
    "hash_dataset_declaration",
    "validate_numeraire_route",
]


# Sentinel import for the type checker.
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from robinhood_lp.protocol.run_mode import RunMode  # noqa: F401
