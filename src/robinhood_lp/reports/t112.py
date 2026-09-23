"""T112 manifest and simulation-evidence schema — pipeline-repair cutover.

T112 is the replacement for T109. The T109 schema allowed a dataset
partition reference to be derived from the running event list or from
a coverage string, and it conflated the source ``block_range`` with
the position ``tick_range`` on the simulation-evidence artifact.
T112 binds three invariants the contract names:

1. **Pipeline repair.** Every new successful product backtest loads
   the dataset's *real* T100 partitions and resolves them through
   the existing T040/T041 replay and T050 point-in-time
   market-features paths. The fixed empty event source the
   approved T109 implementation could reach is no longer reachable
   as a current entry.

2. **Real-partition reference binding.** A ``dataset_partition_ref``
   must resolve to the same canonical bytes T100 registered
   (verified by content hash, range and PoolKey). A reference cannot
   be authored from an event cursor, a coverage string, a placeholder
   or a synthetic list; the loader rejects mismatched or
   non-resolvable references with a named reason.

3. **Block-range vs tick-range separation.** The manifest binds the
   source ``block_range``; the simulation-evidence artifact
   separately records the source ``block_range`` and the actual
   position ``tick_range`` as distinct typed field pairs. Block
   bounds agree with the T100 partition source range and the
   manifest; tick bounds agree with the actual T061 position and
   the T040/T041 reconstruction at the fill cursor.

The module is layered on top of the existing ``reports`` package.
Historical T069, T105 and T109 artifacts remain byte-identical and
read-only; they continue to load through their existing legacy
readers. The T112 manifest and evidence each carry a new, explicit
schema version distinct from ``t109.experiment_manifest.v1`` /
``t109.simulation_evidence.v1``; a reader dispatches by the paired
versions before interpreting fields and rejects mixed, unknown,
missing or downgraded versions with named reasons.

Design constraints (binding):

- **Real-partition resolution.** The loader refuses to accept a
  partition reference whose shape suggests it was authored from a
  running event cursor (``N-N-N`` format), from a coverage string
  (``coverage-N-N`` format), from a placeholder, or from a
  synthetic list. A reference is accepted only when the supplied
  resolver proves it resolves to a real T100 partition with the
  same content hash, range and PoolKey.

- **Strict version dispatch.** T109-versioned records are never
  accepted as current T112 evidence — even if they carry the new
  field names — and T112-versioned records are never accepted as
  legacy T109 evidence. The dispatch returns the named reason
  code ``T112_HISTORICAL_UNAVAILABLE`` so a downstream caller can
  present the historical artifact as readable but explicitly
  unavailable for current promotion.

- **Fill-cursor preservation.** The T112 simulation-evidence
  artifact carries enough run-specific facts at the actual fill
  cursor to restore position, integer inventory, equity, drawdown
  and T052 attribution through the existing T061 engine and T052
  attribution semantics. The reader never derives a snapshot from a
  range-derived synthetic, an interpolated tick, or a strategy
  callback.

- **No second authority.** T112 does not introduce a new manifest,
  evidence, accounting, fee or publication authority. The T069
  orchestrator entry point and the T105/T109 publication paths
  are the only authority on the manifest; T112 adapts them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from robinhood_lp.backtest.events import (
    LEDGER_VERSION,
    PositionState,
)
from robinhood_lp.replay.market_state import MARKET_STATE_VERSION, MarketCursor
from robinhood_lp.reports.registry_binding import (
    StrategyBinding,
    assert_binding_matches_registry,
)
from robinhood_lp.reports.run_identity import RunIdentity
from robinhood_lp.reports.run_state import REPLAY_FRAME_VERSION
from robinhood_lp.reports.simulation_evidence import (
    RunStateCheckpoint,
    RunTransition,
    SimulationEvidence,
    compute_evidence_checksum,
)

# ---------------------------------------------------------------------------
# Schema versions (new, distinct from T109)
# ---------------------------------------------------------------------------

#: Manifest schema version for T112. Distinct from ``t105`` and
#: ``t109`` so a T109-versioned record is never accepted as current
#: T112 evidence and a T112-versioned record is never accepted as
#: legacy T109 evidence.
MANIFEST_VERSION_T112: Final[str] = "t112.experiment_manifest.v1"

#: Simulation-evidence schema version for T112. Paired with the
#: T112 manifest version; a reader dispatches by the paired
#: versions before interpreting fields.
SIMULATION_EVIDENCE_VERSION_T112: Final[str] = "t112.simulation_evidence.v1"

#: Reason-code prefix every T112 dispatch / partition-resolution
#: failure carries. Reviewers can grep for ``T112_`` to surface
#: every failure the new path records.
_REASON_PREFIX: Final[str] = "T112_"

#: The T109 manifest schema version. Recorded as a constant so the
#: dispatch logic cannot be confused by a future rename.
_T109_MANIFEST_VERSION: Final[str] = "t109.experiment_manifest.v1"

#: The T109 simulation-evidence schema version. Recorded as a
#: constant so the dispatch logic cannot be confused by a future
#: rename.
_T109_SIMULATION_EVIDENCE_VERSION: Final[str] = "t109.simulation_evidence.v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class T112Error(ValueError):
    """Base class for T112 manifest / evidence / partition failures."""


class T112FieldError(T112Error):
    """A required T112 field is missing, malformed, or outside its closed vocabulary."""


class T112VersionDispatchError(T112Error):
    """A manifest / evidence record carries a wrong / mixed / downgraded version pair.

    The T112 contract binds strict paired-version dispatch before
    any field interpretation. A reader dispatches by the paired
    manifest / evidence versions; a mismatch, downgrade, or mixed
    pair raises this error with the named reason code so the
    caller can surface a fail-closed verdict.
    """


class T112PartitionRefError(T112Error):
    """A ``dataset_partition_ref`` is fabricated or non-resolvable.

    The T112 contract rejects partition references authored from an
    event cursor (``N-N-N`` format), from a coverage string
    (``coverage-N-N`` format), from a placeholder, or from a
    synthetic list. A reference that does not resolve through the
    supplied resolver to a real T100 partition with the same
    content hash, range and PoolKey also raises this error.
    """


class T112BlockTickConflationError(T112Error):
    """A ``block_range`` value is stored in a ``tick_range`` field or vice versa.

    The T112 contract binds block-range and tick-range as distinct
    typed field pairs. A reader that finds a block-range value in a
    tick-range field — or a tick-range value in a block-range field
    — fails closed with the named reason code so the conflation
    cannot reach publication.
    """


class T112IdentityDisagreementError(T112Error):
    """A manifest / evidence binding disagrees on run / dataset / pool / range / checksum."""


# ---------------------------------------------------------------------------
# Reference shape detection (rejects fabricated partitions)
# ---------------------------------------------------------------------------


#: Regex matching the cursor-fabricated reference shape the T109
#: implementation used to derive partition references from event
#: cursors: ``N-N-N`` (zero-padded decimal block / transaction /
#: log-index triple). Any reference whose value matches this shape
#: is refused as a fabricated cursor-fabricated partition ref.
_CURSOR_FABRICATED_REF_RE: Final[re.Pattern[str]] = re.compile(r"^\d+-\d+-\d+$")

#: Regex matching the coverage-string reference shape the T109
#: implementation fell back to when no event cursor was available:
#: ``coverage-N-N``. Any reference whose value matches this shape
#: is refused as a fabricated coverage-string partition ref.
_COVERAGE_STRING_REF_RE: Final[re.Pattern[str]] = re.compile(r"^coverage-\d+-\d+$")

#: Reserved literal sentinels that signal a placeholder reference.
#: The T112 contract forbids placeholder references; any value in
#: this set is refused by the loader.
_PLACEHOLDER_REFS: Final[frozenset[str]] = frozenset(
    {
        "",
        "TBD",
        "TODO",
        "PLACEHOLDER",
        "FIXME",
        "UNKNOWN",
        "NONE",
        "NULL",
    }
)


def _is_fabricated_partition_ref(ref: str) -> str | None:
    """Return the named reason code if ``ref`` is a fabricated partition reference.

    The function is the gate every T112 loader consults before
    resolving a reference against the T100 registry. It returns
    ``None`` when ``ref`` passes the shape checks; otherwise it
    returns one of:

    - ``T112_REJECTED_CURSOR_FABRICATED_REF`` — the value matches
      the ``N-N-N`` cursor-fabricated shape the T109 implementation
      derived from event cursors.
    - ``T112_REJECTED_COVERAGE_FABRICATED_REF`` — the value matches
      the ``coverage-N-N`` coverage-string shape the T109 fallback
      produced.
    - ``T112_REJECTED_PLACEHOLDER_REF`` — the value is a reserved
      placeholder literal.
    """
    if not isinstance(ref, str) or not ref:
        return f"{_REASON_PREFIX}REJECTED_PLACEHOLDER_REF: empty / non-str reference"
    if ref in _PLACEHOLDER_REFS:
        return (
            f"{_REASON_PREFIX}REJECTED_PLACEHOLDER_REF: reserved placeholder "
            f"reference {ref!r} is not a real T100 partition"
        )
    if _CURSOR_FABRICATED_REF_RE.match(ref):
        return (
            f"{_REASON_PREFIX}REJECTED_CURSOR_FABRICATED_REF: reference "
            f"{ref!r} was authored from an event cursor (N-N-N); only "
            f"real T100 partition references are accepted"
        )
    if _COVERAGE_STRING_REF_RE.match(ref):
        return (
            f"{_REASON_PREFIX}REJECTED_COVERAGE_FABRICATED_REF: reference "
            f"{ref!r} was authored from a coverage string (coverage-N-N); "
            f"only real T100 partition references are accepted"
        )
    return None


# ---------------------------------------------------------------------------
# Typed range pairs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockRange:
    """The source ``block_range`` a T112 manifest / evidence binds to.

    The block bounds are the inclusive ``[start_block, end_block]``
    pair the T100 dataset covers for the run's
    ``(chain_id, pool_key_id)``. The T112 contract binds the
    block range and the tick range as distinct typed field pairs; a
    block-bound value must never appear in a tick-range field or
    vice versa.
    """

    start_block: int
    end_block: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.start_block, int)
            or isinstance(self.start_block, bool)
            or self.start_block < 0
        ):
            raise T112FieldError(
                f"BlockRange.start_block: must be non-negative int, got {self.start_block!r}"
            )
        if (
            not isinstance(self.end_block, int)
            or isinstance(self.end_block, bool)
            or self.end_block < self.start_block
        ):
            raise T112FieldError(
                f"BlockRange.end_block: must be >= start_block={self.start_block}, "
                f"got {self.end_block!r}"
            )

    def to_dict(self) -> dict[str, int]:
        return {"start_block": self.start_block, "end_block": self.end_block}


@dataclass(frozen=True, slots=True)
class TickRange:
    """The actual position ``tick_range`` a T112 simulation evidence binds to.

    The tick bounds are the inclusive ``[tick_lower, tick_upper]``
    pair the T061 engine observed at the actual fill cursor. The
    T112 contract binds the tick range and the block range as
    distinct typed field pairs; a tick-bound value must never
    appear in a block-range field or vice versa.
    """

    tick_lower: int
    tick_upper: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tick_lower, int)
            or isinstance(self.tick_lower, bool)
            or self.tick_lower < -(1 << 23)
            or self.tick_lower >= (1 << 23)
        ):
            raise T112FieldError(f"TickRange.tick_lower: must fit int24, got {self.tick_lower!r}")
        if (
            not isinstance(self.tick_upper, int)
            or isinstance(self.tick_upper, bool)
            or self.tick_upper <= self.tick_lower
        ):
            raise T112FieldError(
                f"TickRange.tick_upper: must be strictly greater than "
                f"tick_lower={self.tick_lower}, got {self.tick_upper!r}"
            )

    def to_dict(self) -> dict[str, int]:
        return {"tick_lower": self.tick_lower, "tick_upper": self.tick_upper}


@dataclass(frozen=True, slots=True)
class PartitionReference:
    """One resolved T100 partition reference a T112 manifest binds to.

    The reference is the canonical partition identity the dataset
    registry (T100) exposes for the run's ``(chain_id, pool_key_id)``;
    the loader rejects any reference whose value was authored from
    an event cursor, a coverage string, a placeholder or a
    synthetic list.
    """

    partition_id: str
    chain_id: int
    pool_key_id: str
    content_hash: str
    range: BlockRange
    schema_version: int
    decode_version: int

    def __post_init__(self) -> None:
        rejection = _is_fabricated_partition_ref(self.partition_id)
        if rejection is not None:
            raise T112PartitionRefError(rejection)
        if (
            not isinstance(self.chain_id, int)
            or isinstance(self.chain_id, bool)
            or self.chain_id <= 0
        ):
            raise T112FieldError(
                f"PartitionReference.chain_id: must be positive int, got {self.chain_id!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise T112FieldError(
                f"PartitionReference.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        if (
            not isinstance(self.content_hash, str)
            or not self.content_hash.startswith("0x")
            or len(self.content_hash) != 66
        ):
            raise T112FieldError(
                f"PartitionReference.content_hash: must be 0x-prefixed 32-byte "
                f"hex digest, got {self.content_hash!r}"
            )
        if not isinstance(self.range, BlockRange):
            raise T112FieldError(
                f"PartitionReference.range: must be BlockRange, got {type(self.range).__name__}"
            )
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version < 1
        ):
            raise T112FieldError(
                f"PartitionReference.schema_version: must be positive int, got "
                f"{self.schema_version!r}"
            )
        if (
            not isinstance(self.decode_version, int)
            or isinstance(self.decode_version, bool)
            or self.decode_version < 1
        ):
            raise T112FieldError(
                f"PartitionReference.decode_version: must be positive int, got "
                f"{self.decode_version!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "partition_id": self.partition_id,
            "chain_id": self.chain_id,
            "pool_key_id": self.pool_key_id,
            "content_hash": self.content_hash,
            "range": self.range.to_dict(),
            "schema_version": self.schema_version,
            "decode_version": self.decode_version,
        }


# ---------------------------------------------------------------------------
# Resolver interface
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class T100ResolvedPartition:
    """A partition the dataset registry (T100) authorises.

    The resolver returns this struct for every partition reference
    the T112 loader accepts. The reference is bound to the canonical
    content hash the registry recorded; a mismatch fails closed.
    """

    partition_id: str
    chain_id: int
    pool_key_id: str
    content_hash: str
    range: BlockRange
    schema_version: int
    decode_version: int

    def to_partition_reference(self) -> PartitionReference:
        return PartitionReference(
            partition_id=self.partition_id,
            chain_id=self.chain_id,
            pool_key_id=self.pool_key_id,
            content_hash=self.content_hash,
            range=self.range,
            schema_version=self.schema_version,
            decode_version=self.decode_version,
        )


class T100PartitionResolver:
    """The T100 partition resolver the T112 loader consults.

    The resolver is the bridge between a ``dataset_partition_ref`` on
    a T112 manifest and the registered partition the T100 dataset
    registry authorises. A loader that cannot prove a reference
    resolves through this resolver (or whose proof disagrees on
    content hash, range or PoolKey) refuses the reference.

    Implementations are typically backed by the in-memory
    :class:`robinhood_lp.research.dataset.DatasetRegistry` or by a
    storage-layer read; the contract surface here is the read-only
    query, not the publishing authority.
    """

    def resolve(
        self,
        *,
        chain_id: int,
        pool_key_id: str,
        partition_ref: str,
    ) -> T100ResolvedPartition:
        """Return the registered partition for ``partition_ref``."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Fill-cursor run facts (T112 simulation evidence)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FillCursorRunFacts:
    """The run-specific facts the T112 evidence binds to the fill cursor.

    The T112 contract binds this struct to the actual fill cursor
    the engine emitted at emission time. A ``RunState`` reader at
    that cursor restores the position, integer inventory,
    realised fees, cost components, equity, drawdown and T052
    attribution components through the existing T061 engine and
    T052 attribution semantics — never through a strategy
    callback, a range-derived synthetic, or an interpolated tick.

    Field units mirror :class:`RunStateCheckpoint` for the run-
    specific fields the reader must restore, plus three explicit
    fields the T109 schema did not bind:

    - ``fill_cursor`` — :class:`MarketCursor`; the exact canonical
      cursor the engine bound the fill transition to. Block-only
      cursors carry ``(transaction_index=-1, log_index=-1)`` to
      denote the end-of-block view.
    - ``raw_token0`` / ``raw_token1`` — non-negative int; the
      integer token amounts the engine recorded in the ledger at
      the fill cursor. These are the raw principal / tokens-owed
      values, not a derived synthetic.
    - ``realised_fees_q64_64`` — int; the realised fee value the
      T104 surface recorded at the fill cursor, expressed in
      q64.64 fixed-point. The reader never recomputes fee values
      from a range or from an interpolated tick.
    - ``cost_components`` — JSON-friendly mapping; the gas /
      slippage / impact cost values the model bundle recorded at
      the fill cursor. Each entry is an integer; no floats appear
      on the accounting path.
    - ``equity_q64_64`` — int; the equity view at the fill cursor.
    - ``drawdown_q64_64`` — int; the running drawdown at the fill
      cursor.
    - ``attribution_snapshot`` — JSON-friendly mapping; the T052
      attribution snapshot at the fill cursor.
    - ``position_snapshot`` — :class:`PositionState`; the integer
      ledger snapshot at the fill cursor. The position carries
      the actual tick bounds, raw token amounts, integer
      ``liquidity``, and the in-range flag.
    """

    fill_cursor: MarketCursor
    position_snapshot: PositionState
    raw_token0: int
    raw_token1: int
    realised_fees_q64_64: int
    cost_components: Mapping[str, int]
    equity_q64_64: int
    drawdown_q64_64: int
    attribution_snapshot: Mapping[str, int]

    def __post_init__(self) -> None:
        if not isinstance(self.fill_cursor, MarketCursor):
            raise T112FieldError(
                f"FillCursorRunFacts.fill_cursor: must be MarketCursor, got "
                f"{type(self.fill_cursor).__name__}"
            )
        if not isinstance(self.position_snapshot, PositionState):
            raise T112FieldError(
                f"FillCursorRunFacts.position_snapshot: must be PositionState, "
                f"got {type(self.position_snapshot).__name__}"
            )
        if (
            not isinstance(self.raw_token0, int)
            or isinstance(self.raw_token0, bool)
            or self.raw_token0 < 0
        ):
            raise T112FieldError(
                f"FillCursorRunFacts.raw_token0: must be non-negative int, got {self.raw_token0!r}"
            )
        if (
            not isinstance(self.raw_token1, int)
            or isinstance(self.raw_token1, bool)
            or self.raw_token1 < 0
        ):
            raise T112FieldError(
                f"FillCursorRunFacts.raw_token1: must be non-negative int, got {self.raw_token1!r}"
            )
        if not isinstance(self.realised_fees_q64_64, int) or isinstance(
            self.realised_fees_q64_64, bool
        ):
            raise T112FieldError(
                f"FillCursorRunFacts.realised_fees_q64_64: must be int, got "
                f"{type(self.realised_fees_q64_64).__name__}"
            )
        if not isinstance(self.cost_components, Mapping):
            raise T112FieldError(
                f"FillCursorRunFacts.cost_components: must be Mapping[str, int], "
                f"got {type(self.cost_components).__name__}"
            )
        for key, value in self.cost_components.items():
            if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool):
                raise T112FieldError(
                    f"FillCursorRunFacts.cost_components: every entry must be "
                    f"str→int, got {key!r}:{value!r}"
                )
        if not isinstance(self.attribution_snapshot, Mapping):
            raise T112FieldError(
                f"FillCursorRunFacts.attribution_snapshot: must be Mapping[str, int], "
                f"got {type(self.attribution_snapshot).__name__}"
            )
        for key, value in self.attribution_snapshot.items():
            if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool):
                raise T112FieldError(
                    f"FillCursorRunFacts.attribution_snapshot: every entry must be "
                    f"str→int, got {key!r}:{value!r}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_cursor": {
                "block_number": self.fill_cursor.block_number,
                "transaction_index": self.fill_cursor.transaction_index,
                "log_index": self.fill_cursor.log_index,
            },
            "position_snapshot": _position_state_to_dict(self.position_snapshot),
            "raw_token0": self.raw_token0,
            "raw_token1": self.raw_token1,
            "realised_fees_q64_64": self.realised_fees_q64_64,
            "cost_components": dict(sorted(self.cost_components.items())),
            "equity_q64_64": self.equity_q64_64,
            "drawdown_q64_64": self.drawdown_q64_64,
            "attribution_snapshot": dict(sorted(self.attribution_snapshot.items())),
        }


def _position_state_to_dict(state: PositionState) -> dict[str, Any]:
    return {
        "version": state.version,
        "pool_key_id": state.pool_key_id,
        "chain_id": state.chain_id,
        "position_id": state.position_id,
        "tick_lower": state.tick_lower,
        "tick_upper": state.tick_upper,
        "liquidity": state.liquidity,
        "principal_token0": state.principal_token0,
        "principal_token1": state.principal_token1,
        "tokens_owed0": state.tokens_owed0,
        "tokens_owed1": state.tokens_owed1,
        "in_range": state.in_range,
        "last_accrual_time": state.last_accrual_time,
    }


# ---------------------------------------------------------------------------
# T112 manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class T112ExperimentManifest:
    """The T112 dataset-referenced manifest a new product-run publishes.

    The T112 manifest is the version-cutover cousin of
    :class:`T109ExperimentManifest`. It carries every byte a
    downstream consumer needs to verify a published run, binds
    the run to real T100 partition references (rejected by the
    loader when fabricated), and pairs the manifest version with
    the T112 simulation-evidence version + checksum so a reader
    can dispatch by the paired versions before interpreting
    fields.

    Field units mirror :class:`T109ExperimentManifest` for the
    registry / dataset / pool slots, plus the new T112 fields:

    - ``simulation_evidence_version`` — non-empty str; the
      simulation-evevidence version the manifest binds to. Must
      equal :data:`SIMULATION_EVIDENCE_VERSION_T112`; any other
      value (including :data:`_T109_SIMULATION_EVIDENCE_VERSION`)
      raises :class:`T112VersionDispatchError`.
    - ``simulation_evidence_checksum`` — non-empty str; the
      simulation-evidence ``evidence_checksum`` the manifest binds
      to. The loader refuses a manifest whose bound checksum
      disagrees with the recomputed value on the evidence file.
    - ``dataset_partition_refs`` — tuple of :class:`PartitionReference`;
      the sorted real T100 partitions the dataset exposes for the
      run's ``(chain_id, pool_key_id)``. A reference cannot be
      authored from an event cursor, a coverage string, a
      placeholder or a synthetic list; the loader rejects every
      fabricated value with the named reason code.
    - ``source_block_range`` — :class:`BlockRange`; the source
      block range the manifest binds to. Distinct from the position
      tick range the simulation-evidence artifact carries.
    """

    version: str
    run_id: str
    chain_id: int
    pool_key_id: str
    source_block_range: BlockRange
    interval_seconds: int
    dataset_version: str
    dataset_schema_version: int
    dataset_decode_version: int
    dataset_content_hash: str
    reporting_numeraire: str
    valuation_qualification: str
    code_revision: str
    dependency_revisions: dict[str, str]
    strategy_identity: str
    strategy_version: str
    registry_version: str
    registry_checksum: str
    parameter_schema_version: str
    parameter_schema_checksum: str
    code_provenance_module: str
    code_provenance_revision: str
    code_provenance_symbol: str
    strategy_params: dict[str, int | str | bool]
    seed: int
    clock_assumption: str
    fill_assumption: str
    cost_assumption: str
    quote_assumption: str
    latency_units: int
    latency_ms_estimate: int
    decisions_checksum: str
    ledger_checksum: str
    metrics_checksum: str
    coverage_checksum: str
    report_checksum: str
    metrics_version: str
    dataset_partition_refs: tuple[PartitionReference, ...]
    dataset_event_count: int
    simulation_evidence_ref: str
    simulation_evidence_version: str
    simulation_evidence_checksum: str
    reconstruction_revision: str
    created_at_unix_seconds: int

    def __post_init__(self) -> None:
        if self.version != MANIFEST_VERSION_T112:
            raise T112VersionDispatchError(
                f"{_REASON_PREFIX}MANIFEST_VERSION_MISMATCH: must be "
                f"{MANIFEST_VERSION_T112!r}, got {self.version!r}"
            )
        if not isinstance(self.run_id, str) or not self.run_id:
            raise T112FieldError("T112ExperimentManifest.run_id: must be non-empty str")
        if (
            not isinstance(self.chain_id, int)
            or isinstance(self.chain_id, bool)
            or self.chain_id <= 0
        ):
            raise T112FieldError(
                f"T112ExperimentManifest.chain_id: must be positive int, got {self.chain_id!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise T112FieldError(
                f"T112ExperimentManifest.pool_key_id: must be non-empty str, got "
                f"{self.pool_key_id!r}"
            )
        if not isinstance(self.source_block_range, BlockRange):
            raise T112FieldError(
                f"T112ExperimentManifest.source_block_range: must be BlockRange, "
                f"got {type(self.source_block_range).__name__}"
            )
        if (
            not isinstance(self.interval_seconds, int)
            or isinstance(self.interval_seconds, bool)
            or self.interval_seconds <= 0
        ):
            raise T112FieldError(
                f"T112ExperimentManifest.interval_seconds: must be positive int, "
                f"got {self.interval_seconds!r}"
            )
        if not isinstance(self.dataset_version, str) or not self.dataset_version:
            raise T112FieldError("T112ExperimentManifest.dataset_version: must be non-empty str")
        if (
            not isinstance(self.dataset_schema_version, int)
            or isinstance(self.dataset_schema_version, bool)
            or self.dataset_schema_version < 1
        ):
            raise T112FieldError(
                f"T112ExperimentManifest.dataset_schema_version: must be positive "
                f"int, got {self.dataset_schema_version!r}"
            )
        if (
            not isinstance(self.dataset_decode_version, int)
            or isinstance(self.dataset_decode_version, bool)
            or self.dataset_decode_version < 1
        ):
            raise T112FieldError(
                f"T112ExperimentManifest.dataset_decode_version: must be positive "
                f"int, got {self.dataset_decode_version!r}"
            )
        if (
            not isinstance(self.dataset_content_hash, str)
            or not self.dataset_content_hash.startswith("0x")
            or len(self.dataset_content_hash) != 66
        ):
            raise T112FieldError(
                f"T112ExperimentManifest.dataset_content_hash: must be 0x-prefixed "
                f"32-byte hex digest, got {self.dataset_content_hash!r}"
            )
        if not isinstance(self.reporting_numeraire, str) or not self.reporting_numeraire:
            raise T112FieldError(
                "T112ExperimentManifest.reporting_numeraire: must be non-empty str"
            )
        if self.valuation_qualification not in ("QUALIFIED", "RELATIVE_ONLY"):
            raise T112FieldError(
                f"T112ExperimentManifest.valuation_qualification: must be "
                f"QUALIFIED or RELATIVE_ONLY, got {self.valuation_qualification!r}"
            )
        if not isinstance(self.code_revision, str) or not self.code_revision:
            raise T112FieldError("T112ExperimentManifest.code_revision: must be non-empty str")
        if not isinstance(self.dependency_revisions, dict):
            raise T112FieldError(
                f"T112ExperimentManifest.dependency_revisions: must be "
                f"dict[str, str], got {type(self.dependency_revisions).__name__}"
            )
        for pkg, ver in self.dependency_revisions.items():
            if not isinstance(pkg, str) or not isinstance(ver, str) or not ver:
                raise T112FieldError(
                    "T112ExperimentManifest.dependency_revisions: every key/value "
                    "must be non-empty str"
                )
        if not isinstance(self.strategy_identity, str) or not self.strategy_identity:
            raise T112FieldError("T112ExperimentManifest.strategy_identity: must be non-empty str")
        for slot in (
            "strategy_version",
            "registry_version",
            "registry_checksum",
            "parameter_schema_version",
            "parameter_schema_checksum",
            "code_provenance_module",
            "code_provenance_revision",
        ):
            value = getattr(self, slot)
            if not isinstance(value, str) or not value:
                raise T112FieldError(f"T112ExperimentManifest.{slot}: must be non-empty str")
        if not isinstance(self.code_provenance_symbol, str):
            raise T112FieldError(
                f"T112ExperimentManifest.code_provenance_symbol: must be str, got "
                f"{type(self.code_provenance_symbol).__name__}"
            )
        if not isinstance(self.strategy_params, dict):
            raise T112FieldError(
                f"T112ExperimentManifest.strategy_params: must be dict[str, int|str|bool], "
                f"got {type(self.strategy_params).__name__}"
            )
        for k, v in self.strategy_params.items():
            if not isinstance(k, str) or not k:
                raise T112FieldError(
                    "T112ExperimentManifest.strategy_params: every key must be non-empty str"
                )
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                raise T112FieldError(
                    "T112ExperimentManifest.strategy_params: every value must be int|str|bool"
                )
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise T112FieldError(
                f"T112ExperimentManifest.seed: must be non-negative int, got {self.seed!r}"
            )
        if self.clock_assumption != "EVENT_TIME":
            raise T112FieldError(
                f"T112ExperimentManifest.clock_assumption: must be 'EVENT_TIME', got "
                f"{self.clock_assumption!r}"
            )
        if self.fill_assumption not in ("DETERMINISTIC_FAILURE", "PROBABILISTIC_FAILURE"):
            raise T112FieldError(
                f"T112ExperimentManifest.fill_assumption: must be "
                f"DETERMINISTIC_FAILURE or PROBABILISTIC_FAILURE, got "
                f"{self.fill_assumption!r}"
            )
        if self.cost_assumption not in ("FLAT_GAS", "DYNAMIC_GAS"):
            raise T112FieldError(
                f"T112ExperimentManifest.cost_assumption: must be FLAT_GAS or "
                f"DYNAMIC_GAS, got {self.cost_assumption!r}"
            )
        if self.quote_assumption not in ("STATIC_FEE", "DYNAMIC_FEE"):
            raise T112FieldError(
                f"T112ExperimentManifest.quote_assumption: must be STATIC_FEE or "
                f"DYNAMIC_FEE, got {self.quote_assumption!r}"
            )
        if (
            not isinstance(self.latency_units, int)
            or isinstance(self.latency_units, bool)
            or self.latency_units < 0
        ):
            raise T112FieldError(
                f"T112ExperimentManifest.latency_units: must be non-negative int, "
                f"got {self.latency_units!r}"
            )
        if (
            not isinstance(self.latency_ms_estimate, int)
            or isinstance(self.latency_ms_estimate, bool)
            or self.latency_ms_estimate < 0
        ):
            raise T112FieldError(
                f"T112ExperimentManifest.latency_ms_estimate: must be "
                f"non-negative int, got {self.latency_ms_estimate!r}"
            )
        for slot in (
            "decisions_checksum",
            "ledger_checksum",
            "metrics_checksum",
            "coverage_checksum",
            "report_checksum",
        ):
            value = getattr(self, slot)
            if not isinstance(value, str) or not value:
                raise T112FieldError(f"T112ExperimentManifest.{slot}: must be non-empty str")
        if not isinstance(self.metrics_version, str) or not self.metrics_version:
            raise T112FieldError("T112ExperimentManifest.metrics_version: must be non-empty str")
        if not isinstance(self.dataset_partition_refs, tuple):
            raise T112FieldError(
                f"T112ExperimentManifest.dataset_partition_refs: must be tuple, "
                f"got {type(self.dataset_partition_refs).__name__}"
            )
        if not self.dataset_partition_refs:
            raise T112PartitionRefError(
                f"{_REASON_PREFIX}EMPTY_PARTITION_REFS: at least one real T100 "
                f"partition reference is required"
            )
        seen: set[str] = set()
        for ref in self.dataset_partition_refs:
            if not isinstance(ref, PartitionReference):
                raise T112FieldError(
                    f"T112ExperimentManifest.dataset_partition_refs: every entry "
                    f"must be PartitionReference, got {type(ref).__name__}"
                )
            # Cross-binding: every partition ref must agree with the
            # manifest's run identity on (chain_id, pool_key_id). A
            # reference that disagrees with the manifest's pool is a
            # rejected cross-pool binding.
            if ref.chain_id != self.chain_id or ref.pool_key_id != self.pool_key_id:
                raise T112IdentityDisagreementError(
                    f"{_REASON_PREFIX}PARTITION_POOL_MISMATCH: partition ref "
                    f"({ref.chain_id}, {ref.pool_key_id!r}) disagrees with "
                    f"manifest pool ({self.chain_id}, {self.pool_key_id!r})"
                )
            if ref.partition_id in seen:
                raise T112PartitionRefError(
                    f"{_REASON_PREFIX}DUPLICATE_PARTITION_REF: partition ref "
                    f"{ref.partition_id!r} appears more than once in the manifest"
                )
            seen.add(ref.partition_id)
        if (
            not isinstance(self.dataset_event_count, int)
            or isinstance(self.dataset_event_count, bool)
            or self.dataset_event_count < 0
        ):
            raise T112FieldError(
                f"T112ExperimentManifest.dataset_event_count: must be "
                f"non-negative int, got {self.dataset_event_count!r}"
            )
        if not isinstance(self.simulation_evidence_ref, str) or not self.simulation_evidence_ref:
            raise T112FieldError(
                "T112ExperimentManifest.simulation_evidence_ref: must be non-empty str"
            )
        # Paired version dispatch: the manifest version is
        # ``MANIFEST_VERSION_T112`` and the bound evidence version
        # must be ``SIMULATION_EVIDENCE_VERSION_T112``. Any T109 or
        # unknown / downgraded evidence version fails closed.
        if self.simulation_evidence_version != SIMULATION_EVIDENCE_VERSION_T112:
            if self.simulation_evidence_version == _T109_SIMULATION_EVIDENCE_VERSION:
                raise T112VersionDispatchError(
                    f"{_REASON_PREFIX}PAIRED_VERSION_DOWNGRADE: manifest "
                    f"version={self.version!r} paired with T109 evidence "
                    f"version={self.simulation_evidence_version!r}; "
                    f"T112 evidence must carry {SIMULATION_EVIDENCE_VERSION_T112!r}"
                )
            raise T112VersionDispatchError(
                f"{_REASON_PREFIX}PAIRED_VERSION_MISMATCH: simulation evidence "
                f"version={self.simulation_evidence_version!r} is not the T112 "
                f"paired version {SIMULATION_EVIDENCE_VERSION_T112!r}"
            )
        if (
            not isinstance(self.simulation_evidence_checksum, str)
            or not self.simulation_evidence_checksum
        ):
            raise T112FieldError(
                "T112ExperimentManifest.simulation_evidence_checksum: must be non-empty str"
            )
        if not isinstance(self.reconstruction_revision, str) or not self.reconstruction_revision:
            raise T112FieldError(
                "T112ExperimentManifest.reconstruction_revision: must be non-empty str"
            )
        if (
            not isinstance(self.created_at_unix_seconds, int)
            or isinstance(self.created_at_unix_seconds, bool)
            or self.created_at_unix_seconds < 0
        ):
            raise T112FieldError(
                f"T112ExperimentManifest.created_at_unix_seconds: must be "
                f"non-negative int, got {self.created_at_unix_seconds!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "chain_id": self.chain_id,
            "pool_key_id": self.pool_key_id,
            "source_block_range": self.source_block_range.to_dict(),
            "interval_seconds": self.interval_seconds,
            "dataset_version": self.dataset_version,
            "dataset_schema_version": self.dataset_schema_version,
            "dataset_decode_version": self.dataset_decode_version,
            "dataset_content_hash": self.dataset_content_hash,
            "reporting_numeraire": self.reporting_numeraire,
            "valuation_qualification": self.valuation_qualification,
            "code_revision": self.code_revision,
            "dependency_revisions": dict(sorted(self.dependency_revisions.items())),
            "strategy_identity": self.strategy_identity,
            "strategy_version": self.strategy_version,
            "registry_version": self.registry_version,
            "registry_checksum": self.registry_checksum,
            "parameter_schema_version": self.parameter_schema_version,
            "parameter_schema_checksum": self.parameter_schema_checksum,
            "code_provenance_module": self.code_provenance_module,
            "code_provenance_revision": self.code_provenance_revision,
            "code_provenance_symbol": self.code_provenance_symbol,
            "strategy_params": dict(sorted(self.strategy_params.items())),
            "seed": self.seed,
            "clock_assumption": self.clock_assumption,
            "fill_assumption": self.fill_assumption,
            "cost_assumption": self.cost_assumption,
            "quote_assumption": self.quote_assumption,
            "latency_units": self.latency_units,
            "latency_ms_estimate": self.latency_ms_estimate,
            "decisions_checksum": self.decisions_checksum,
            "ledger_checksum": self.ledger_checksum,
            "metrics_checksum": self.metrics_checksum,
            "coverage_checksum": self.coverage_checksum,
            "report_checksum": self.report_checksum,
            "metrics_version": self.metrics_version,
            "dataset_partition_refs": [
                ref.to_dict()
                for ref in sorted(self.dataset_partition_refs, key=lambda r: r.partition_id)
            ],
            "dataset_event_count": self.dataset_event_count,
            "simulation_evidence_ref": self.simulation_evidence_ref,
            "simulation_evidence_version": self.simulation_evidence_version,
            "simulation_evidence_checksum": self.simulation_evidence_checksum,
            "reconstruction_revision": self.reconstruction_revision,
            "created_at_unix_seconds": self.created_at_unix_seconds,
        }

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )


def compute_t112_report_checksum(payload: Mapping[str, Any]) -> str:
    """Return the SHA-256 hex digest of the canonical serialisation of ``payload``.

    The function binds every T112 field except ``report_checksum``
    and ``simulation_evidence_checksum`` so a tampered field
    surfaces as a checksum mismatch. Excluding
    ``simulation_evidence_checksum`` breaks the circular binding
    between the manifest's report_checksum and the evidence's
    checksum: each is computed independently from the other
    side's content.
    """
    serialisable = dict(payload)
    serialisable.pop("report_checksum", None)
    serialisable.pop("simulation_evidence_checksum", None)
    content = json.dumps(serialisable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_t112_experiment_manifest(
    *,
    run_id: str,
    chain_id: int,
    pool_key_id: str,
    source_block_range: BlockRange,
    interval_seconds: int,
    dataset_version: str,
    dataset_schema_version: int,
    dataset_decode_version: int,
    dataset_content_hash: str,
    reporting_numeraire: str,
    valuation_qualification: str,
    code_revision: str,
    dependency_revisions: Mapping[str, str],
    strategy_binding: StrategyBinding,
    seed: int,
    clock_assumption: str,
    fill_assumption: str,
    cost_assumption: str,
    quote_assumption: str,
    latency_units: int,
    latency_ms_estimate: int,
    decisions_checksum: str,
    ledger_checksum: str,
    metrics_checksum: str,
    coverage_checksum: str,
    metrics_version: str,
    dataset_partition_refs: Sequence[PartitionReference],
    dataset_event_count: int,
    simulation_evidence_ref: str,
    simulation_evidence_checksum: str,
    reconstruction_revision: str,
    created_at_unix_seconds: int,
) -> T112ExperimentManifest:
    """Build the T112 manifest the new product-run entry publishes.

    The function is the canonical builder. It freezes every field
    the T112 contract binds, computes the report checksum, and
    rejects fabricated partition references before returning the
    manifest.
    """
    sorted_refs = tuple(sorted(dataset_partition_refs, key=lambda r: r.partition_id))
    payload: dict[str, Any] = {
        "version": MANIFEST_VERSION_T112,
        "run_id": run_id,
        "chain_id": chain_id,
        "pool_key_id": pool_key_id,
        "source_block_range": source_block_range.to_dict(),
        "interval_seconds": interval_seconds,
        "dataset_version": dataset_version,
        "dataset_schema_version": dataset_schema_version,
        "dataset_decode_version": dataset_decode_version,
        "dataset_content_hash": dataset_content_hash,
        "reporting_numeraire": reporting_numeraire,
        "valuation_qualification": valuation_qualification,
        "code_revision": code_revision,
        "dependency_revisions": dict(sorted(dependency_revisions.items())),
        "strategy_identity": strategy_binding.strategy_identity,
        "strategy_version": strategy_binding.strategy_version,
        "registry_version": strategy_binding.registry_version,
        "registry_checksum": strategy_binding.registry_checksum,
        "parameter_schema_version": strategy_binding.parameter_schema_version,
        "parameter_schema_checksum": strategy_binding.parameter_schema_checksum,
        "code_provenance_module": strategy_binding.code_provenance_module,
        "code_provenance_revision": strategy_binding.code_provenance_revision,
        "code_provenance_symbol": strategy_binding.code_provenance_symbol,
        "strategy_params": dict(
            sorted(
                ((k, v) for k, v in strategy_binding.validated_parameters),
                key=lambda kv: kv[0],
            )
        ),
        "seed": seed,
        "clock_assumption": clock_assumption,
        "fill_assumption": fill_assumption,
        "cost_assumption": cost_assumption,
        "quote_assumption": quote_assumption,
        "latency_units": latency_units,
        "latency_ms_estimate": latency_ms_estimate,
        "decisions_checksum": decisions_checksum,
        "ledger_checksum": ledger_checksum,
        "metrics_checksum": metrics_checksum,
        "coverage_checksum": coverage_checksum,
        "report_checksum": "",
        "metrics_version": metrics_version,
        "dataset_partition_refs": [ref.to_dict() for ref in sorted_refs],
        "dataset_event_count": dataset_event_count,
        "simulation_evidence_ref": simulation_evidence_ref,
        "simulation_evidence_version": SIMULATION_EVIDENCE_VERSION_T112,
        "simulation_evidence_checksum": simulation_evidence_checksum,
        "reconstruction_revision": reconstruction_revision,
        "created_at_unix_seconds": created_at_unix_seconds,
    }
    report_checksum = compute_t112_report_checksum(payload)
    payload["report_checksum"] = report_checksum
    return T112ExperimentManifest(
        version=MANIFEST_VERSION_T112,
        run_id=run_id,
        chain_id=chain_id,
        pool_key_id=pool_key_id,
        source_block_range=source_block_range,
        interval_seconds=interval_seconds,
        dataset_version=dataset_version,
        dataset_schema_version=dataset_schema_version,
        dataset_decode_version=dataset_decode_version,
        dataset_content_hash=dataset_content_hash,
        reporting_numeraire=reporting_numeraire,
        valuation_qualification=valuation_qualification,
        code_revision=code_revision,
        dependency_revisions=dict(sorted(dependency_revisions.items())),
        strategy_identity=strategy_binding.strategy_identity,
        strategy_version=strategy_binding.strategy_version,
        registry_version=strategy_binding.registry_version,
        registry_checksum=strategy_binding.registry_checksum,
        parameter_schema_version=strategy_binding.parameter_schema_version,
        parameter_schema_checksum=strategy_binding.parameter_schema_checksum,
        code_provenance_module=strategy_binding.code_provenance_module,
        code_provenance_revision=strategy_binding.code_provenance_revision,
        code_provenance_symbol=strategy_binding.code_provenance_symbol,
        strategy_params=dict(
            sorted(
                ((k, v) for k, v in strategy_binding.validated_parameters),
                key=lambda kv: kv[0],
            )
        ),
        seed=seed,
        clock_assumption=clock_assumption,
        fill_assumption=fill_assumption,
        cost_assumption=cost_assumption,
        quote_assumption=quote_assumption,
        latency_units=latency_units,
        latency_ms_estimate=latency_ms_estimate,
        decisions_checksum=decisions_checksum,
        ledger_checksum=ledger_checksum,
        metrics_checksum=metrics_checksum,
        coverage_checksum=coverage_checksum,
        report_checksum=report_checksum,
        metrics_version=metrics_version,
        dataset_partition_refs=sorted_refs,
        dataset_event_count=dataset_event_count,
        simulation_evidence_ref=simulation_evidence_ref,
        simulation_evidence_version=SIMULATION_EVIDENCE_VERSION_T112,
        simulation_evidence_checksum=simulation_evidence_checksum,
        reconstruction_revision=reconstruction_revision,
        created_at_unix_seconds=created_at_unix_seconds,
    )


def t112_experiment_manifest_from_dict(
    payload: Mapping[str, Any],
    *,
    resolver: T100PartitionResolver | None = None,
) -> T112ExperimentManifest:
    """Reconstruct a :class:`T112ExperimentManifest` from a JSON-friendly dict.

    The loader is the strict paired-version dispatch entry point.
    It refuses:

    - any record whose ``version`` is not
      :data:`MANIFEST_VERSION_T112` (including the T109 version,
      ``t109.experiment_manifest.v1``, and the legacy T105 version,
      ``t105.experiment_manifest.v1``);
    - any record whose ``simulation_evidence_version`` is not
      :data:`SIMULATION_EVIDENCE_VERSION_T112` (including the T109
      evidence version);
    - any record that still embeds the legacy ``input_event_list``
      payload — T112 forbids embedded event copies;
    - any record whose ``dataset_partition_refs`` were authored
      from an event cursor, a coverage string, a placeholder or a
      synthetic list, or whose references do not resolve through
      the supplied resolver to a real T100 partition with the same
      content hash, range and PoolKey;
    - any record whose ``report_checksum`` does not match the
      recomputed value.

    When ``resolver`` is ``None`` the loader still rejects every
    fabricated reference shape but does not require external
    resolution — the contract preserves the loader-only path for
    offline tests and review surfaces that do not have a live
    registry handle.
    """
    if not isinstance(payload, Mapping):
        raise T112FieldError(
            f"t112_experiment_manifest_from_dict: payload must be Mapping, "
            f"got {type(payload).__name__}"
        )
    declared_version = payload.get("version")
    if declared_version != MANIFEST_VERSION_T112:
        if declared_version == _T109_MANIFEST_VERSION:
            raise T112VersionDispatchError(
                f"{_REASON_PREFIX}T109_HISTORICAL_UNAVAILABLE: manifest declares "
                f"version={declared_version!r}, expected "
                f"{MANIFEST_VERSION_T112!r}; pre-evidence historical manifests "
                f"are readable only through the legacy read-only path"
            )
        raise T112VersionDispatchError(
            f"{_REASON_PREFIX}MANIFEST_VERSION_MISMATCH: manifest declares "
            f"version={declared_version!r}, expected {MANIFEST_VERSION_T112!r}"
        )
    if "input_event_list" in payload:
        raise T112VersionDispatchError(
            f"{_REASON_PREFIX}T109_HISTORICAL_UNAVAILABLE: manifest still "
            f"carries the legacy input_event_list; T112 forbids embedded event "
            f"copies and only accepts real T100 partition references"
        )
    try:
        raw_partitions = payload["dataset_partition_refs"]
    except KeyError as exc:
        raise T112FieldError(
            f"t112_experiment_manifest_from_dict: missing key {exc.args[0]!r}"
        ) from exc
    if not isinstance(raw_partitions, list):
        raise T112FieldError(
            "t112_experiment_manifest_from_dict: dataset_partition_refs must be a list"
        )
    if not raw_partitions:
        raise T112PartitionRefError(
            f"{_REASON_PREFIX}EMPTY_PARTITION_REFS: at least one real T100 "
            f"partition reference is required"
        )
    manifest_chain_id = int(payload["chain_id"])
    manifest_pool_key_id = str(payload["pool_key_id"])
    partition_refs: list[PartitionReference] = []
    for raw in raw_partitions:
        if not isinstance(raw, Mapping):
            raise T112FieldError(
                "t112_experiment_manifest_from_dict: dataset_partition_refs entry must be a Mapping"
            )
        partition_id = str(raw.get("partition_id", ""))
        # Fabricated-reference detection runs first so the loader
        # never resolves a cursor-fabricated / coverage-string /
        # placeholder value through the resolver.
        rejection = _is_fabricated_partition_ref(partition_id)
        if rejection is not None:
            raise T112PartitionRefError(rejection)
        try:
            content_hash = str(raw["content_hash"])
            range_payload = raw["range"]
            schema_version = int(raw["schema_version"])
            decode_version = int(raw["decode_version"])
        except KeyError as exc:
            raise T112FieldError(
                f"t112_experiment_manifest_from_dict: missing key "
                f"{exc.args[0]!r} in partition_ref entry"
            ) from exc
        if not isinstance(range_payload, Mapping):
            raise T112FieldError(
                "t112_experiment_manifest_from_dict: partition_ref.range must be a Mapping"
            )
        try:
            range_obj = BlockRange(
                start_block=int(range_payload["start_block"]),
                end_block=int(range_payload["end_block"]),
            )
        except KeyError as exc:
            raise T112FieldError(
                f"t112_experiment_manifest_from_dict: missing key "
                f"{exc.args[0]!r} in partition_ref.range"
            ) from exc
        ref = PartitionReference(
            partition_id=partition_id,
            chain_id=manifest_chain_id,
            pool_key_id=manifest_pool_key_id,
            content_hash=content_hash,
            range=range_obj,
            schema_version=schema_version,
            decode_version=decode_version,
        )
        if resolver is not None:
            # The resolver must prove the reference resolves to a
            # real T100 partition with the same content hash,
            # range and PoolKey. A mismatch fails closed.
            try:
                resolved = resolver.resolve(
                    chain_id=manifest_chain_id,
                    pool_key_id=manifest_pool_key_id,
                    partition_ref=partition_id,
                )
            except _RESOLVER_REJECT_EXCEPTIONS as exc:
                raise T112PartitionRefError(
                    f"{_REASON_PREFIX}UNRESOLVABLE_PARTITION_REF: partition "
                    f"reference {partition_id!r} does not resolve through the "
                    f"T100 registry: {exc}"
                ) from exc
            if (
                resolved.content_hash != ref.content_hash
                or resolved.range != ref.range
                or resolved.chain_id != ref.chain_id
                or resolved.pool_key_id != ref.pool_key_id
            ):
                raise T112PartitionRefError(
                    f"{_REASON_PREFIX}PARTITION_BINDING_MISMATCH: resolved "
                    f"partition does not match the manifest reference: "
                    f"recorded={ref.to_dict()} resolved="
                    f"{resolved.to_partition_reference().to_dict()}"
                )
        partition_refs.append(ref)
    # Block/tick conflation guard: the loader refuses any
    # dataset_partition_refs entry whose ``range`` field carries a
    # tick-shaped ``tick_lower`` / ``tick_upper`` pair (a tick-range
    # value stored in a block-range field). The T112 contract binds
    # the block_range and tick_range as distinct typed field pairs;
    # a tick value in a block-range field is a closed failure.
    for raw in raw_partitions:
        if not isinstance(raw, Mapping):
            continue
        range_payload = raw.get("range")
        if not isinstance(range_payload, Mapping):
            continue
        if "tick_lower" in range_payload or "tick_upper" in range_payload:
            raise T112BlockTickConflationError(
                f"{_REASON_PREFIX}BLOCK_TICK_CONFLATION: dataset_partition_refs "
                f"range entry carries tick-shaped fields; T112 binds block_range "
                f"and tick_range as distinct typed field pairs"
            )
    try:
        source_block_range_payload = payload["source_block_range"]
        if not isinstance(source_block_range_payload, Mapping):
            raise T112FieldError(
                "t112_experiment_manifest_from_dict: source_block_range must be a Mapping"
            )
        source_block_range = BlockRange(
            start_block=int(source_block_range_payload["start_block"]),
            end_block=int(source_block_range_payload["end_block"]),
        )
        if "tick_lower" in source_block_range_payload or "tick_upper" in source_block_range_payload:
            raise T112BlockTickConflationError(
                f"{_REASON_PREFIX}BLOCK_TICK_CONFLATION: manifest source_block_range "
                f"carries tick-shaped fields; T112 binds block_range and tick_range "
                f"as distinct typed field pairs"
            )
    except KeyError as exc:
        raise T112FieldError(
            f"t112_experiment_manifest_from_dict: missing key {exc.args[0]!r} in source_block_range"
        ) from exc
    try:
        manifest = T112ExperimentManifest(
            version=str(payload["version"]),
            run_id=str(payload["run_id"]),
            chain_id=manifest_chain_id,
            pool_key_id=manifest_pool_key_id,
            source_block_range=source_block_range,
            interval_seconds=int(payload["interval_seconds"]),
            dataset_version=str(payload["dataset_version"]),
            dataset_schema_version=int(payload["dataset_schema_version"]),
            dataset_decode_version=int(payload["dataset_decode_version"]),
            dataset_content_hash=str(payload["dataset_content_hash"]),
            reporting_numeraire=str(payload["reporting_numeraire"]),
            valuation_qualification=str(payload["valuation_qualification"]),
            code_revision=str(payload["code_revision"]),
            dependency_revisions=dict(payload["dependency_revisions"]),
            strategy_identity=str(payload["strategy_identity"]),
            strategy_version=str(payload["strategy_version"]),
            registry_version=str(payload["registry_version"]),
            registry_checksum=str(payload["registry_checksum"]),
            parameter_schema_version=str(payload["parameter_schema_version"]),
            parameter_schema_checksum=str(payload["parameter_schema_checksum"]),
            code_provenance_module=str(payload["code_provenance_module"]),
            code_provenance_revision=str(payload["code_provenance_revision"]),
            code_provenance_symbol=str(payload["code_provenance_symbol"]),
            strategy_params=dict(payload["strategy_params"]),
            seed=int(payload["seed"]),
            clock_assumption=str(payload["clock_assumption"]),
            fill_assumption=str(payload["fill_assumption"]),
            cost_assumption=str(payload["cost_assumption"]),
            quote_assumption=str(payload["quote_assumption"]),
            latency_units=int(payload["latency_units"]),
            latency_ms_estimate=int(payload["latency_ms_estimate"]),
            decisions_checksum=str(payload["decisions_checksum"]),
            ledger_checksum=str(payload["ledger_checksum"]),
            metrics_checksum=str(payload["metrics_checksum"]),
            coverage_checksum=str(payload["coverage_checksum"]),
            report_checksum=str(payload["report_checksum"]),
            metrics_version=str(payload["metrics_version"]),
            dataset_partition_refs=tuple(partition_refs),
            dataset_event_count=int(payload["dataset_event_count"]),
            simulation_evidence_ref=str(payload["simulation_evidence_ref"]),
            simulation_evidence_version=str(payload["simulation_evidence_version"]),
            simulation_evidence_checksum=str(payload["simulation_evidence_checksum"]),
            reconstruction_revision=str(payload["reconstruction_revision"]),
            created_at_unix_seconds=int(payload["created_at_unix_seconds"]),
        )
    except KeyError as exc:
        raise T112FieldError(
            f"t112_experiment_manifest_from_dict: missing key {exc.args[0]!r}"
        ) from exc
    recomputed = compute_t112_report_checksum(manifest.to_dict())
    if recomputed != manifest.report_checksum:
        raise T112IdentityDisagreementError(
            f"{_REASON_PREFIX}REPORT_CHECKSUM_MISMATCH: recorded="
            f"{manifest.report_checksum} recomputed={recomputed}"
        )
    return manifest


# ---------------------------------------------------------------------------
# T112 simulation evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class T112SimulationEvidence:
    """The T112 simulation-evidence artifact.

    The T112 evidence binds the run-specific facts a ``RunState``
    reader needs to reproduce a run at the actual fill cursor.
    The artifact is the version-cutover cousin of
    :class:`SimulationEvidence`; it carries every T109 field plus
    the T112 fields the contract names:

    - ``manifest_version`` — non-empty str; the manifest version
      the evidence is bound to. Must equal
      :data:`MANIFEST_VERSION_T112`; any other value (including
      :data:`_T109_MANIFEST_VERSION`) fails closed.
    - ``source_block_range`` — :class:`BlockRange`; the source
      block range the evidence binds to. Distinct from
      ``position_tick_range`` and recorded as a typed pair so the
      contract's "block_range vs tick_range" separation cannot be
      conflated.
    - ``position_tick_range`` — :class:`TickRange`; the actual
      position tick range the engine observed at the fill cursor.
      Recorded as a typed pair; the loader refuses any tick-shaped
      value in a block-range field or vice versa.
    - ``fill_cursor_facts`` — :class:`FillCursorRunFacts`; the
      run-specific facts the reader restores at the fill cursor
      through the existing T061 engine and T052 attribution
      semantics.
    """

    version: str
    run_id: str
    dataset_version: str
    dataset_schema_version: int
    dataset_decode_version: int
    dataset_content_hash: str
    pool_key_id: str
    chain_id: int
    source_block_range: BlockRange
    position_tick_range: TickRange
    strategy_identity: str
    strategy_version: str
    registry_version: str
    registry_checksum: str
    parameter_schema_version: str
    parameter_schema_checksum: str
    code_provenance_module: str
    code_provenance_revision: str
    engine_revision: str
    accounting_revision: str
    reconstruction_revision: str
    initial_position: Mapping[str, Any]
    initial_equity_q64_64: int
    initial_attribution: Mapping[str, int]
    transitions: tuple[RunTransition, ...]
    checkpoints: tuple[RunStateCheckpoint, ...]
    fill_cursor_facts: FillCursorRunFacts
    evidence_checksum: str
    market_state_version: str
    manifest_version: str
    manifest_checksum: str

    def __post_init__(self) -> None:
        if self.version != SIMULATION_EVIDENCE_VERSION_T112:
            raise T112VersionDispatchError(
                f"{_REASON_PREFIX}EVIDENCE_VERSION_MISMATCH: must be "
                f"{SIMULATION_EVIDENCE_VERSION_T112!r}, got {self.version!r}"
            )
        if not isinstance(self.run_id, str) or not self.run_id:
            raise T112FieldError("T112SimulationEvidence.run_id: must be non-empty str")
        if not isinstance(self.dataset_version, str) or not self.dataset_version:
            raise T112FieldError("T112SimulationEvidence.dataset_version: must be non-empty str")
        if (
            not isinstance(self.dataset_schema_version, int)
            or isinstance(self.dataset_schema_version, bool)
            or self.dataset_schema_version < 1
        ):
            raise T112FieldError(
                f"T112SimulationEvidence.dataset_schema_version: must be positive "
                f"int, got {self.dataset_schema_version!r}"
            )
        if (
            not isinstance(self.dataset_decode_version, int)
            or isinstance(self.dataset_decode_version, bool)
            or self.dataset_decode_version < 1
        ):
            raise T112FieldError(
                f"T112SimulationEvidence.dataset_decode_version: must be positive "
                f"int, got {self.dataset_decode_version!r}"
            )
        if (
            not isinstance(self.dataset_content_hash, str)
            or not self.dataset_content_hash.startswith("0x")
            or len(self.dataset_content_hash) != 66
        ):
            raise T112FieldError(
                f"T112SimulationEvidence.dataset_content_hash: must be 0x-prefixed "
                f"32-byte hex digest, got {self.dataset_content_hash!r}"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise T112FieldError("T112SimulationEvidence.pool_key_id: must be non-empty str")
        if (
            not isinstance(self.chain_id, int)
            or isinstance(self.chain_id, bool)
            or self.chain_id <= 0
        ):
            raise T112FieldError(
                f"T112SimulationEvidence.chain_id: must be positive int, got {self.chain_id!r}"
            )
        if not isinstance(self.source_block_range, BlockRange):
            raise T112FieldError(
                f"T112SimulationEvidence.source_block_range: must be BlockRange, "
                f"got {type(self.source_block_range).__name__}"
            )
        if not isinstance(self.position_tick_range, TickRange):
            raise T112FieldError(
                f"T112SimulationEvidence.position_tick_range: must be TickRange, "
                f"got {type(self.position_tick_range).__name__}"
            )
        # Block/tick conflation guard: a TickRange value in a
        # BlockRange slot or vice versa is the contract's
        # "conflate block_range with tick_range" violation; the
        # loader refuses it with the named reason code.
        if (
            self.source_block_range.start_block == self.position_tick_range.tick_lower
            and self.source_block_range.end_block == self.position_tick_range.tick_upper
        ):
            # The block bounds and tick bounds happen to coincide
            # numerically, but they are distinct typed fields; the
            # loader still records the typed pair and rejects
            # ``start_block == tick_lower`` etc. only when the
            # *payload* conflates the two (the payload check
            # happens in ``t112_simulation_evidence_from_dict``).
            pass
        if self.market_state_version != MARKET_STATE_VERSION:
            raise T112FieldError(
                f"T112SimulationEvidence.market_state_version: must be "
                f"{MARKET_STATE_VERSION!r}, got {self.market_state_version!r}"
            )
        for slot in (
            "strategy_identity",
            "strategy_version",
            "registry_version",
            "registry_checksum",
            "parameter_schema_version",
            "parameter_schema_checksum",
            "code_provenance_module",
            "code_provenance_revision",
            "engine_revision",
            "accounting_revision",
            "reconstruction_revision",
        ):
            value = getattr(self, slot)
            if not isinstance(value, str) or not value:
                raise T112FieldError(f"T112SimulationEvidence.{slot}: must be non-empty str")
        if not isinstance(self.initial_position, Mapping):
            raise T112FieldError(
                f"T112SimulationEvidence.initial_position: must be Mapping, got "
                f"{type(self.initial_position).__name__}"
            )
        if not isinstance(self.initial_attribution, Mapping):
            raise T112FieldError(
                f"T112SimulationEvidence.initial_attribution: must be "
                f"Mapping[str, int], got {type(self.initial_attribution).__name__}"
            )
        if not isinstance(self.transitions, tuple):
            raise T112FieldError(
                f"T112SimulationEvidence.transitions: must be tuple, got "
                f"{type(self.transitions).__name__}"
            )
        for i, tr in enumerate(self.transitions):
            if not isinstance(tr, RunTransition):
                raise T112FieldError(
                    f"T112SimulationEvidence.transitions[{i}]: must be "
                    f"RunTransition, got {type(tr).__name__}"
                )
        if not isinstance(self.checkpoints, tuple):
            raise T112FieldError(
                f"T112SimulationEvidence.checkpoints: must be tuple, got "
                f"{type(self.checkpoints).__name__}"
            )
        for i, cp in enumerate(self.checkpoints):
            if not isinstance(cp, RunStateCheckpoint):
                raise T112FieldError(
                    f"T112SimulationEvidence.checkpoints[{i}]: must be "
                    f"RunStateCheckpoint, got {type(cp).__name__}"
                )
        if not isinstance(self.fill_cursor_facts, FillCursorRunFacts):
            raise T112FieldError(
                f"T112SimulationEvidence.fill_cursor_facts: must be "
                f"FillCursorRunFacts, got {type(self.fill_cursor_facts).__name__}"
            )
        if not isinstance(self.evidence_checksum, str) or not self.evidence_checksum:
            raise T112FieldError("T112SimulationEvidence.evidence_checksum: must be non-empty str")
        # Paired version dispatch: the evidence version is
        # ``SIMULATION_EVIDENCE_VERSION_T112`` and the bound manifest
        # version must be ``MANIFEST_VERSION_T112``. A T109 or
        # unknown manifest version fails closed.
        if self.manifest_version != MANIFEST_VERSION_T112:
            if self.manifest_version == _T109_MANIFEST_VERSION:
                raise T112VersionDispatchError(
                    f"{_REASON_PREFIX}PAIRED_VERSION_DOWNGRADE: evidence "
                    f"version={self.version!r} paired with T109 manifest "
                    f"version={self.manifest_version!r}; T112 evidence must "
                    f"carry {MANIFEST_VERSION_T112!r}"
                )
            raise T112VersionDispatchError(
                f"{_REASON_PREFIX}PAIRED_VERSION_MISMATCH: manifest version="
                f"{self.manifest_version!r} is not the T112 paired version "
                f"{MANIFEST_VERSION_T112!r}"
            )
        if not isinstance(self.manifest_checksum, str) or not self.manifest_checksum:
            raise T112FieldError("T112SimulationEvidence.manifest_checksum: must be non-empty str")
        # Cross-binding: the fill cursor's position snapshot must
        # agree with the recorded position_tick_range; a snapshot
        # whose tick bounds disagree with the tick_range field is a
        # closed failure.
        snapshot = self.fill_cursor_facts.position_snapshot
        if (
            snapshot.tick_lower != self.position_tick_range.tick_lower
            or snapshot.tick_upper != self.position_tick_range.tick_upper
        ):
            raise T112IdentityDisagreementError(
                f"{_REASON_PREFIX}TICK_RANGE_POSITION_MISMATCH: fill-cursor "
                f"position snapshot tick bounds "
                f"({snapshot.tick_lower}, {snapshot.tick_upper}) disagree with "
                f"position_tick_range "
                f"({self.position_tick_range.tick_lower}, "
                f"{self.position_tick_range.tick_upper})"
            )
        if snapshot.pool_key_id != self.pool_key_id or snapshot.chain_id != self.chain_id:
            raise T112IdentityDisagreementError(
                f"{_REASON_PREFIX}POSITION_POOL_MISMATCH: fill-cursor position "
                f"snapshot pool ({snapshot.chain_id}, {snapshot.pool_key_id!r}) "
                f"disagrees with evidence pool ({self.chain_id}, "
                f"{self.pool_key_id!r})"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "dataset_version": self.dataset_version,
            "dataset_schema_version": self.dataset_schema_version,
            "dataset_decode_version": self.dataset_decode_version,
            "dataset_content_hash": self.dataset_content_hash,
            "pool_key_id": self.pool_key_id,
            "chain_id": self.chain_id,
            "source_block_range": self.source_block_range.to_dict(),
            "position_tick_range": self.position_tick_range.to_dict(),
            "strategy_identity": self.strategy_identity,
            "strategy_version": self.strategy_version,
            "registry_version": self.registry_version,
            "registry_checksum": self.registry_checksum,
            "parameter_schema_version": self.parameter_schema_version,
            "parameter_schema_checksum": self.parameter_schema_checksum,
            "code_provenance_module": self.code_provenance_module,
            "code_provenance_revision": self.code_provenance_revision,
            "engine_revision": self.engine_revision,
            "accounting_revision": self.accounting_revision,
            "reconstruction_revision": self.reconstruction_revision,
            "initial_position": dict(sorted(self.initial_position.items())),
            "initial_equity_q64_64": self.initial_equity_q64_64,
            "initial_attribution": dict(sorted(self.initial_attribution.items())),
            "transitions": [tr.to_dict() for tr in self.transitions],
            "checkpoints": [cp.to_dict() for cp in self.checkpoints],
            "fill_cursor_facts": self.fill_cursor_facts.to_dict(),
            "market_state_version": self.market_state_version,
            "manifest_version": self.manifest_version,
            "manifest_checksum": self.manifest_checksum,
            "evidence_checksum": self.evidence_checksum,
        }

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )


def compute_t112_evidence_checksum(payload: Mapping[str, Any]) -> str:
    """Return the SHA-256 hex digest of the canonical serialisation of ``payload``.

    The function binds every T112 evidence field except
    ``evidence_checksum`` and ``manifest_checksum`` so a tampered
    field surfaces as a checksum mismatch. Excluding
    ``manifest_checksum`` breaks the circular binding between the
    manifest's report_checksum and the evidence's checksum:
    each is computed independently from the other side's content.
    """
    serialisable = dict(payload)
    serialisable.pop("evidence_checksum", None)
    serialisable.pop("manifest_checksum", None)
    content = json.dumps(serialisable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_t112_simulation_evidence(
    *,
    run_id: str,
    dataset_version: str,
    dataset_schema_version: int,
    dataset_decode_version: int,
    dataset_content_hash: str,
    pool_key_id: str,
    chain_id: int,
    source_block_range: BlockRange,
    position_tick_range: TickRange,
    strategy_identity: str,
    strategy_version: str,
    registry_version: str,
    registry_checksum: str,
    parameter_schema_version: str,
    parameter_schema_checksum: str,
    code_provenance_module: str,
    code_provenance_revision: str,
    engine_revision: str,
    accounting_revision: str,
    reconstruction_revision: str,
    initial_position: Mapping[str, Any],
    initial_equity_q64_64: int,
    initial_attribution: Mapping[str, int],
    transitions: Sequence[RunTransition],
    checkpoints: Sequence[RunStateCheckpoint],
    fill_cursor_facts: FillCursorRunFacts,
    manifest_checksum: str,
) -> T112SimulationEvidence:
    """Build the T112 simulation-evidence artifact the new entry publishes.

    The function is the canonical builder. It freezes every field
    the T112 contract binds, computes the evidence checksum, and
    pairs the manifest checksum the orchestrator will record on
    the manifest.
    """
    payload: dict[str, Any] = {
        "version": SIMULATION_EVIDENCE_VERSION_T112,
        "run_id": run_id,
        "dataset_version": dataset_version,
        "dataset_schema_version": dataset_schema_version,
        "dataset_decode_version": dataset_decode_version,
        "dataset_content_hash": dataset_content_hash,
        "pool_key_id": pool_key_id,
        "chain_id": chain_id,
        "source_block_range": source_block_range.to_dict(),
        "position_tick_range": position_tick_range.to_dict(),
        "strategy_identity": strategy_identity,
        "strategy_version": strategy_version,
        "registry_version": registry_version,
        "registry_checksum": registry_checksum,
        "parameter_schema_version": parameter_schema_version,
        "parameter_schema_checksum": parameter_schema_checksum,
        "code_provenance_module": code_provenance_module,
        "code_provenance_revision": code_provenance_revision,
        "engine_revision": engine_revision,
        "accounting_revision": accounting_revision,
        "reconstruction_revision": reconstruction_revision,
        "initial_position": dict(sorted(initial_position.items())),
        "initial_equity_q64_64": initial_equity_q64_64,
        "initial_attribution": dict(sorted(initial_attribution.items())),
        "transitions": [tr.to_dict() for tr in transitions],
        "checkpoints": [cp.to_dict() for cp in checkpoints],
        "fill_cursor_facts": fill_cursor_facts.to_dict(),
        "market_state_version": MARKET_STATE_VERSION,
        "manifest_version": MANIFEST_VERSION_T112,
        "manifest_checksum": manifest_checksum,
        "evidence_checksum": "",
    }
    checksum = compute_t112_evidence_checksum(payload)
    payload["evidence_checksum"] = checksum
    return T112SimulationEvidence(
        version=SIMULATION_EVIDENCE_VERSION_T112,
        run_id=run_id,
        dataset_version=dataset_version,
        dataset_schema_version=dataset_schema_version,
        dataset_decode_version=dataset_decode_version,
        dataset_content_hash=dataset_content_hash,
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        source_block_range=source_block_range,
        position_tick_range=position_tick_range,
        strategy_identity=strategy_identity,
        strategy_version=strategy_version,
        registry_version=registry_version,
        registry_checksum=registry_checksum,
        parameter_schema_version=parameter_schema_version,
        parameter_schema_checksum=parameter_schema_checksum,
        code_provenance_module=code_provenance_module,
        code_provenance_revision=code_provenance_revision,
        engine_revision=engine_revision,
        accounting_revision=accounting_revision,
        reconstruction_revision=reconstruction_revision,
        initial_position=dict(sorted(initial_position.items())),
        initial_equity_q64_64=initial_equity_q64_64,
        initial_attribution=dict(sorted(initial_attribution.items())),
        transitions=tuple(transitions),
        checkpoints=tuple(checkpoints),
        fill_cursor_facts=fill_cursor_facts,
        evidence_checksum=checksum,
        market_state_version=MARKET_STATE_VERSION,
        manifest_version=MANIFEST_VERSION_T112,
        manifest_checksum=manifest_checksum,
    )


def t112_simulation_evidence_from_dict(
    payload: Mapping[str, Any],
) -> T112SimulationEvidence:
    """Reconstruct a :class:`T112SimulationEvidence` from a JSON-friendly dict.

    The loader is the strict paired-version dispatch entry point
    for the evidence artifact. It refuses:

    - any record whose ``version`` is not
      :data:`SIMULATION_EVIDENCE_VERSION_T112` (including the T109
      evidence version);
    - any record whose ``manifest_version`` is not
      :data:`MANIFEST_VERSION_T112`;
    - any record whose ``source_block_range`` carries tick-shaped
      fields or whose ``position_tick_range`` carries block-shaped
      fields (the contract binds the two as distinct typed pairs);
    - any record whose ``evidence_checksum`` does not match the
      recomputed value.
    """
    if not isinstance(payload, Mapping):
        raise T112FieldError(
            f"t112_simulation_evidence_from_dict: payload must be Mapping, "
            f"got {type(payload).__name__}"
        )
    declared_version = payload.get("version")
    if declared_version != SIMULATION_EVIDENCE_VERSION_T112:
        if declared_version == _T109_SIMULATION_EVIDENCE_VERSION:
            raise T112VersionDispatchError(
                f"{_REASON_PREFIX}T109_HISTORICAL_UNAVAILABLE: evidence "
                f"declares version={declared_version!r}, expected "
                f"{SIMULATION_EVIDENCE_VERSION_T112!r}; pre-evidence historical "
                f"artifacts are readable only through the legacy read-only path"
            )
        raise T112VersionDispatchError(
            f"{_REASON_PREFIX}EVIDENCE_VERSION_MISMATCH: evidence declares "
            f"version={declared_version!r}, expected "
            f"{SIMULATION_EVIDENCE_VERSION_T112!r}"
        )
    # Block/tick conflation guard on the raw payload: a block
    # range slot carrying tick-shaped fields (or vice versa) is
    # the contract's conflation violation. The strict typed-pair
    # check happens at construction; the payload-shape check
    # catches a record that authors ``start_block`` /
    # ``end_block`` values inside the ``position_tick_range`` slot
    # (or vice versa).
    raw_block = payload.get("source_block_range")
    raw_tick = payload.get("position_tick_range")
    if not isinstance(raw_block, Mapping):
        raise T112FieldError(
            "t112_simulation_evidence_from_dict: source_block_range must be a Mapping"
        )
    if not isinstance(raw_tick, Mapping):
        raise T112FieldError(
            "t112_simulation_evidence_from_dict: position_tick_range must be a Mapping"
        )
    if "tick_lower" in raw_block or "tick_upper" in raw_block:
        raise T112BlockTickConflationError(
            f"{_REASON_PREFIX}BLOCK_TICK_CONFLATION: source_block_range carries "
            f"tick-shaped fields; T112 binds block_range and tick_range as "
            f"distinct typed field pairs"
        )
    if "start_block" in raw_tick or "end_block" in raw_tick:
        raise T112BlockTickConflationError(
            f"{_REASON_PREFIX}BLOCK_TICK_CONFLATION: position_tick_range carries "
            f"block-shaped fields; T112 binds block_range and tick_range as "
            f"distinct typed field pairs"
        )
    try:
        raw_transitions = payload["transitions"]
        raw_checkpoints = payload["checkpoints"]
        raw_fill = payload["fill_cursor_facts"]
    except KeyError as exc:
        raise T112FieldError(
            f"t112_simulation_evidence_from_dict: missing key {exc.args[0]!r}"
        ) from exc

    def _build_transition(entry: Mapping[str, Any]) -> RunTransition:
        if not isinstance(entry, Mapping):
            raise T112FieldError(
                "t112_simulation_evidence_from_dict: transition entry must be Mapping"
            )
        cursor: MarketCursor | None = None
        if entry.get("cursor") is not None:
            c = entry["cursor"]
            cursor = MarketCursor(
                block_number=int(c["block_number"]),
                transaction_index=int(c["transaction_index"]),
                log_index=int(c["log_index"]),
            )
        return RunTransition(
            ordinal=int(entry["ordinal"]),
            stage=str(entry["stage"]),
            cursor=cursor,
            ledger_hash_after=str(entry["ledger_hash_after"]),
            audit_event_id=str(entry["audit_event_id"]),
            payload=(dict(entry["payload"]) if entry.get("payload") is not None else None),
            state_changing=bool(entry["state_changing"]),
        )

    def _build_checkpoint(entry: Mapping[str, Any]) -> RunStateCheckpoint:
        if not isinstance(entry, Mapping):
            raise T112FieldError(
                "t112_simulation_evidence_from_dict: checkpoint entry must be Mapping"
            )
        c = entry["cursor"]
        return RunStateCheckpoint(
            cursor=MarketCursor(
                block_number=int(c["block_number"]),
                transaction_index=int(c["transaction_index"]),
                log_index=int(c["log_index"]),
            ),
            last_applied_ordinal=int(entry["last_applied_ordinal"]),
            ledger_snapshot=dict(entry["ledger_snapshot"]),
            equity_q64_64=int(entry["equity_q64_64"]),
            drawdown_q64_64=int(entry["drawdown_q64_64"]),
            attribution_snapshot=dict(entry["attribution_snapshot"]),
        )

    transitions = tuple(_build_transition(e) for e in raw_transitions)
    checkpoints = tuple(_build_checkpoint(e) for e in raw_checkpoints)
    if not isinstance(raw_fill, Mapping):
        raise T112FieldError(
            "t112_simulation_evidence_from_dict: fill_cursor_facts must be a Mapping"
        )
    fill_cursor_payload = raw_fill["fill_cursor"]
    fill_cursor = MarketCursor(
        block_number=int(fill_cursor_payload["block_number"]),
        transaction_index=int(fill_cursor_payload["transaction_index"]),
        log_index=int(fill_cursor_payload["log_index"]),
    )
    position_payload = raw_fill["position_snapshot"]
    position_snapshot = PositionState(
        version=str(position_payload.get("version", LEDGER_VERSION)),
        pool_key_id=str(position_payload["pool_key_id"]),
        chain_id=int(position_payload["chain_id"]),
        position_id=str(position_payload["position_id"]),
        tick_lower=int(position_payload["tick_lower"]),
        tick_upper=int(position_payload["tick_upper"]),
        liquidity=int(position_payload["liquidity"]),
        principal_token0=int(position_payload["principal_token0"]),
        principal_token1=int(position_payload["principal_token1"]),
        tokens_owed0=int(position_payload["tokens_owed0"]),
        tokens_owed1=int(position_payload["tokens_owed1"]),
        in_range=bool(position_payload["in_range"]),
        last_accrual_time=int(position_payload["last_accrual_time"]),
    )
    fill_cursor_facts = FillCursorRunFacts(
        fill_cursor=fill_cursor,
        position_snapshot=position_snapshot,
        raw_token0=int(raw_fill["raw_token0"]),
        raw_token1=int(raw_fill["raw_token1"]),
        realised_fees_q64_64=int(raw_fill["realised_fees_q64_64"]),
        cost_components=dict(raw_fill["cost_components"]),
        equity_q64_64=int(raw_fill["equity_q64_64"]),
        drawdown_q64_64=int(raw_fill["drawdown_q64_64"]),
        attribution_snapshot=dict(raw_fill["attribution_snapshot"]),
    )
    try:
        evidence = T112SimulationEvidence(
            version=str(payload["version"]),
            run_id=str(payload["run_id"]),
            dataset_version=str(payload["dataset_version"]),
            dataset_schema_version=int(payload["dataset_schema_version"]),
            dataset_decode_version=int(payload["dataset_decode_version"]),
            dataset_content_hash=str(payload["dataset_content_hash"]),
            pool_key_id=str(payload["pool_key_id"]),
            chain_id=int(payload["chain_id"]),
            source_block_range=BlockRange(
                start_block=int(raw_block["start_block"]),
                end_block=int(raw_block["end_block"]),
            ),
            position_tick_range=TickRange(
                tick_lower=int(raw_tick["tick_lower"]),
                tick_upper=int(raw_tick["tick_upper"]),
            ),
            strategy_identity=str(payload["strategy_identity"]),
            strategy_version=str(payload["strategy_version"]),
            registry_version=str(payload["registry_version"]),
            registry_checksum=str(payload["registry_checksum"]),
            parameter_schema_version=str(payload["parameter_schema_version"]),
            parameter_schema_checksum=str(payload["parameter_schema_checksum"]),
            code_provenance_module=str(payload["code_provenance_module"]),
            code_provenance_revision=str(payload["code_provenance_revision"]),
            engine_revision=str(payload["engine_revision"]),
            accounting_revision=str(payload["accounting_revision"]),
            reconstruction_revision=str(payload["reconstruction_revision"]),
            initial_position=dict(payload["initial_position"]),
            initial_equity_q64_64=int(payload["initial_equity_q64_64"]),
            initial_attribution=dict(payload["initial_attribution"]),
            transitions=transitions,
            checkpoints=checkpoints,
            fill_cursor_facts=fill_cursor_facts,
            evidence_checksum=str(payload["evidence_checksum"]),
            market_state_version=str(payload["market_state_version"]),
            manifest_version=str(payload["manifest_version"]),
            manifest_checksum=str(payload["manifest_checksum"]),
        )
    except KeyError as exc:
        raise T112FieldError(
            f"t112_simulation_evidence_from_dict: missing key {exc.args[0]!r}"
        ) from exc
    recomputed = compute_t112_evidence_checksum(evidence.to_dict())
    if recomputed != evidence.evidence_checksum:
        raise T112IdentityDisagreementError(
            f"{_REASON_PREFIX}EVIDENCE_CHECKSUM_MISMATCH: recorded="
            f"{evidence.evidence_checksum} recomputed={recomputed}"
        )
    return evidence


# ---------------------------------------------------------------------------
# Resolver exception allowlist (loader)
# ---------------------------------------------------------------------------


#: The exception types the loader treats as a "not resolvable"
#: verdict when the resolver raises them. Anything else propagates
#: as a normal exception so the loader does not mask real errors.
_RESOLVER_REJECT_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    KeyError,
    LookupError,
    ValueError,
)


# ---------------------------------------------------------------------------
# Convenience: T112 reader dispatch
# ---------------------------------------------------------------------------


def dispatch_manifest_version(
    payload: Mapping[str, Any],
) -> str:
    """Return the manifest version literal the loader accepts.

    The function is the strict paired-version dispatch the T112
    contract binds. It returns the version literal only when the
    payload declares the T112 manifest version; any other value
    raises :class:`T112VersionDispatchError` with the named reason
    code ``T112_T109_HISTORICAL_UNAVAILABLE`` for T109 records and
    ``T112_MANIFEST_VERSION_MISMATCH`` for unknown / downgraded /
    mixed values.
    """
    if not isinstance(payload, Mapping):
        raise T112VersionDispatchError(
            f"{_REASON_PREFIX}MANIFEST_VERSION_MISMATCH: payload must be Mapping, "
            f"got {type(payload).__name__}"
        )
    declared = payload.get("version")
    if declared == MANIFEST_VERSION_T112:
        return MANIFEST_VERSION_T112
    if declared == _T109_MANIFEST_VERSION:
        raise T112VersionDispatchError(
            f"{_REASON_PREFIX}T109_HISTORICAL_UNAVAILABLE: manifest declares "
            f"version={declared!r}; T109 records are readable only through the "
            f"legacy read-only path and never as current T112 evidence"
        )
    raise T112VersionDispatchError(
        f"{_REASON_PREFIX}MANIFEST_VERSION_MISMATCH: manifest declares "
        f"version={declared!r}; expected {MANIFEST_VERSION_T112!r}"
    )


def dispatch_evidence_version(payload: Mapping[str, Any]) -> str:
    """Return the evidence version literal the loader accepts.

    The function is the strict paired-version dispatch the T112
    contract binds. It returns the version literal only when the
    payload declares the T112 evidence version; any other value
    raises :class:`T112VersionDispatchError` with the named reason
    code ``T112_T109_HISTORICAL_UNAVAILABLE`` for T109 records and
    ``T112_EVIDENCE_VERSION_MISMATCH`` for unknown / downgraded /
    mixed values.
    """
    if not isinstance(payload, Mapping):
        raise T112VersionDispatchError(
            f"{_REASON_PREFIX}EVIDENCE_VERSION_MISMATCH: payload must be Mapping, "
            f"got {type(payload).__name__}"
        )
    declared = payload.get("version")
    if declared == SIMULATION_EVIDENCE_VERSION_T112:
        return SIMULATION_EVIDENCE_VERSION_T112
    if declared == _T109_SIMULATION_EVIDENCE_VERSION:
        raise T112VersionDispatchError(
            f"{_REASON_PREFIX}T109_HISTORICAL_UNAVAILABLE: evidence declares "
            f"version={declared!r}; T109 evidence is readable only through the "
            f"legacy read-only path and never as current T112 evidence"
        )
    raise T112VersionDispatchError(
        f"{_REASON_PREFIX}EVIDENCE_VERSION_MISMATCH: evidence declares "
        f"version={declared!r}; expected {SIMULATION_EVIDENCE_VERSION_T112!r}"
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    # versions
    "MANIFEST_VERSION_T112",
    "SIMULATION_EVIDENCE_VERSION_T112",
    # errors
    "T112Error",
    "T112FieldError",
    "T112VersionDispatchError",
    "T112PartitionRefError",
    "T112BlockTickConflationError",
    "T112IdentityDisagreementError",
    # typed field pairs
    "BlockRange",
    "TickRange",
    "PartitionReference",
    # resolver interface
    "T100ResolvedPartition",
    "T100PartitionResolver",
    # fill-cursor preservation
    "FillCursorRunFacts",
    # dataclasses
    "T112ExperimentManifest",
    "T112SimulationEvidence",
    # builders / loaders
    "build_t112_experiment_manifest",
    "build_t112_simulation_evidence",
    "compute_t112_report_checksum",
    "compute_t112_evidence_checksum",
    "dispatch_manifest_version",
    "dispatch_evidence_version",
    "t112_experiment_manifest_from_dict",
    "t112_simulation_evidence_from_dict",
    # legacy preserved
    "REPLAY_FRAME_VERSION",
    "RunIdentity",
    "SimulationEvidence",
    "assert_binding_matches_registry",
    "compute_evidence_checksum",
]
