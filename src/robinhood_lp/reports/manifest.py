"""Experiment manifests — every published run's source of truth (T063 + T105).

A run manifest is the *only* artifact that ties a published LP result
back to its inputs. The manifest records the dataset, schema, code and
dependency revisions, the chain and ``PoolKey``, the interval and
block bounds, the strategy parameters, the random seed, the clock,
fill, cost and quote assumptions, the dataset version, the reporting
numeraire and its valuation qualification (ADR-014 §3), the registry
binding that authorised the strategy identity (T105), and the
deterministic checksums the consumer needs to reconcile the result
without trusting any intermediate artifact.

T105 cutover (binding):

- The hard-coded ``VALID_STRATEGY_KINDS`` vocabulary the T063 manifest
  carried is **deprecated**. The T105 manifest records a registered
  ``strategy_identity`` plus the full registry binding (registry
  version + checksum, parameter-schema version + checksum, and code
  provenance) so every published result is bound to the exact registry
  revision that authorised it. The ``strategy_kind`` field is replaced
  by ``strategy_identity``; a ``strategy_kind`` value the manifest
  builder accepts from the caller is no longer accepted by the
  current publication path.

- Historical T063 manifests remain readable through the dedicated
  :mod:`robinhood_lp.reports.legacy` module, which loads them under an
  explicit ``LEGACY_T063`` marker. A legacy manifest is never
  re-published as current evidence.

Field units are stated per field. The T063 acceptance clause binds
the unit on every quantity the manifest records — interval, block
bound, latency, cost and quote assumption are explicitly named —
because publishing an LP result whose units are undocumented is a
contract break. Every quantity is an integer or a closed-vocabulary
string; ``float`` never appears in the manifest record
(`ADR-004`).

The manifest is per-pool: it records exactly one
``(chain_id, PoolKey)``, that pool's own block range and interval,
its own decisions, ledger, metrics and report checksums, and never
another pool's. A multi-pool run publishes one manifest per member
pool under one shared run identity
(`RunIdentity` in :mod:`robinhood_lp.reports.run_identity`).

Design constraints (binding):

- **Determinism.** Two equivalent manifests produce byte-identical
  JSON serialisations. The :func:`manifest_checksum` function sorts
  every collection and uses canonical JSON so a re-ordered payload
  does not change the digest.

- **Integer units.** All numeric quantities are Python ``int``; the
  manifest never stores ``float``.

- **Layer purity.** This module imports the standard library, the
  backtest layer (events / engine), the in-package metrics layer, and
  the registry binding (T105) only. It does not import RPC, storage,
  configuration, signing, execution, or presentation code.

References:

- T063 — Add experiment manifests and reports.
- T068 — strategy registry (the source of truth for the binding).
- T105 — registry-bound manifest authority (this version).
- ADR-014 §3 — numeraire hierarchy and the ``QUALIFIED`` /
  ``RELATIVE_ONLY`` qualification.
- `docs/spec/research/DATASET_AND_EVALUATION.md` DS-001 /
  DS-003 — dataset identity and qualification.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from robinhood_lp.backtest.events import (
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
)
from robinhood_lp.reports.metrics import (
    _VALID_VALUATION_QUALIFICATIONS,
    METRICS_VERSION,
    VALUATION_QUALIFIED,
    VALUATION_RELATIVE_ONLY,
    CoverageSummary,
    LedgerSnapshot,
    RunMetrics,
)
from robinhood_lp.reports.registry_binding import (
    RegistryBindingError,
    StrategyBinding,
    assert_binding_matches_registry,
    bind_strategy_to_registry,
    binding_parameter_dict,
)

#: Module version. Bumping it is a breaking change for downstream
#: consumers (the validation layer, the report, the rerun).
#:
#: T105 cut the version to ``t105.experiment_manifest.v1`` when it
#: replaced the T063 hard-coded strategy vocabulary with the registry
#: binding. Legacy T063 manifests are loaded through
#: :mod:`robinhood_lp.reports.legacy`, which pins the prior version.
MANIFEST_VERSION: Final[str] = "t105.experiment_manifest.v1"

#: Sentinel string used when the code revision is unknown (no git
#: revision is available, e.g. an unpacked source tarball). The
#: manifest records the literal ``"UNKNOWN"`` rather than a fake SHA
#: so an honest reader can detect the gap.
UNKNOWN_CODE_REVISION: Final[str] = "UNKNOWN"

#: Sentinel string used when a dependency lock is missing. Same
#: rationale as :data:`UNKNOWN_CODE_REVISION`.
UNKNOWN_DEPENDENCY_REVISION: Final[str] = "UNKNOWN"

#: Closed vocabulary for the per-run clock assumption. The current
#: implementation accepts ``"EVENT_TIME"`` only — wall-clock driven
#: runs are forbidden by the determinism policy (`R17`, `R18`).
VALID_CLOCK_ASSUMPTIONS: Final[frozenset[str]] = frozenset({"EVENT_TIME"})

#: Closed vocabulary for the fill-assumption kind. Each entry names a
#: class of fill model the manifest may declare.
VALID_FILL_ASSUMPTIONS: Final[frozenset[str]] = frozenset(
    {"DETERMINISTIC_FAILURE", "PROBABILISTIC_FAILURE"}
)

#: Closed vocabulary for the cost-assumption kind. Each entry names a
#: class of gas model the manifest may declare.
VALID_COST_ASSUMPTIONS: Final[frozenset[str]] = frozenset({"FLAT_GAS", "DYNAMIC_GAS"})

#: Closed vocabulary for the quote-assumption kind. Each entry names a
#: class of fee / quote model the manifest may declare.
VALID_QUOTE_ASSUMPTIONS: Final[frozenset[str]] = frozenset({"STATIC_FEE", "DYNAMIC_FEE"})

#: DEPRECATED — the T063 hard-coded strategy-name vocabulary.
#:
#: Kept for the legacy reader (:mod:`robinhood_lp.reports.legacy`) and
#: for tests that exercise the historical schema. The T105 current
#: publication path does **not** consult this set: a manifest builder
#: receives a :class:`StrategyBinding`, not a free-text strategy
#: kind, and the binding's registry identity is the only authority.
#: No production, CLI, Web, background, or test helper may publish a
#: current manifest via this vocabulary; the constant exists only as
#: a historical anchor and a legacy-reader input.
VALID_STRATEGY_KINDS: Final[frozenset[str]] = frozenset(
    {
        "HOLD",
        "BROAD_RANGE",
        "FIXED_WIDTH",
        "VOLATILITY_WIDTH",
        "OUT_OF_RANGE_REBALANCE",
    }
)

_VALID_SOURCE_PRIORITIES: Final[frozenset[int]] = frozenset(
    {
        SOURCE_PRIORITY_DATA,
        SOURCE_PRIORITY_STRATEGY,
        SOURCE_PRIORITY_RISK,
        SOURCE_PRIORITY_EXECUTION,
        SOURCE_PRIORITY_SYSTEM,
    }
)

_VALID_KINDS: Final[frozenset[str]] = frozenset(
    {KIND_SWAP, KIND_MINT, KIND_BURN, KIND_TICK, KIND_OBSERVATION, KIND_SHUTDOWN}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ManifestError(ValueError):
    """Base class for manifest-construction failures."""


class InvalidManifestFieldError(ManifestError):
    """A manifest field is missing, has the wrong type, or is outside its closed vocabulary."""


class ManifestPoolMismatchError(ManifestError):
    """A manifest records data for a ``(chain_id, PoolKey)`` other than its own."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_non_negative_int(value: int, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidManifestFieldError(f"{field_name}: must be int, got {type(value).__name__}")
    if value < 0:
        raise InvalidManifestFieldError(f"{field_name}: must be non-negative, got {value}")
    return value


def _require_positive_int(value: int, *, field_name: str) -> int:
    _require_non_negative_int(value, field_name=field_name)
    if value == 0:
        raise InvalidManifestFieldError(f"{field_name}: must be positive, got 0")
    return value


def _require_non_empty_str(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidManifestFieldError(f"{field_name}: must be non-empty str, got {value!r}")
    return value


def _require_str(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidManifestFieldError(f"{field_name}: must be str, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# Serialised input event (for rerun)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SerialisedEvent:
    """The canonical JSON-friendly form of a :class:`BacktestEvent`.

    The manifest embeds the full input event sequence so a saved
    manifest can be rerun by one command. The fields are the same
    nine scalars the event-id binds to; ``payload`` is rendered as a
    sorted list of ``[key, value]`` pairs so JSON round-tripping is
    deterministic. Two equivalent events in any process serialise to
    the same JSON string and reconstruct to the same ``BacktestEvent``.

    Field units: ``timestamp``/``observed_at``/``available_at`` are
    integer Unix-seconds event-time; ``sequence`` is the per-source
    monotonic counter the engine assigns; ``source_priority`` is one
    of the closed sentinels; ``kind`` is one of the closed sentinels;
    ``payload`` items are ``(str, int|str|bool)`` pairs.
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
    payload: tuple[tuple[str, int | str | bool], ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(self.version, field_name="SerialisedEvent.version")
        _require_non_negative_int(self.timestamp, field_name="SerialisedEvent.timestamp")
        _require_non_negative_int(self.sequence, field_name="SerialisedEvent.sequence")
        if self.source_priority not in _VALID_SOURCE_PRIORITIES:
            raise InvalidManifestFieldError(
                f"SerialisedEvent.source_priority: must be one of "
                f"{sorted(_VALID_SOURCE_PRIORITIES)}, got {self.source_priority}"
            )
        if self.kind not in _VALID_KINDS:
            raise InvalidManifestFieldError(
                f"SerialisedEvent.kind: must be one of {sorted(_VALID_KINDS)}, got {self.kind!r}"
            )
        _require_non_empty_str(self.pool_key_id, field_name="SerialisedEvent.pool_key_id")
        _require_positive_int(self.chain_id, field_name="SerialisedEvent.chain_id")
        _require_non_negative_int(self.observed_at, field_name="SerialisedEvent.observed_at")
        _require_non_negative_int(self.available_at, field_name="SerialisedEvent.available_at")
        if self.available_at < self.observed_at:
            raise InvalidManifestFieldError(
                f"SerialisedEvent.available_at={self.available_at} must be "
                f">= observed_at={self.observed_at}"
            )
        if not isinstance(self.payload, tuple):
            raise InvalidManifestFieldError(
                f"SerialisedEvent.payload: must be tuple[(str, int|str|bool)], "
                f"got {type(self.payload).__name__}"
            )

    @classmethod
    def from_backtest_event(cls, event: BacktestEvent) -> SerialisedEvent:
        return cls(
            version=event.version,
            timestamp=event.timestamp,
            sequence=event.sequence,
            source_priority=event.source_priority,
            kind=event.kind,
            pool_key_id=event.pool_key_id,
            chain_id=event.chain_id,
            observed_at=event.observed_at,
            available_at=event.available_at,
            payload=event.payload,
        )

    def to_backtest_event(self) -> BacktestEvent:
        return BacktestEvent(
            version=self.version,
            timestamp=self.timestamp,
            sequence=self.sequence,
            source_priority=self.source_priority,
            kind=self.kind,
            pool_key_id=self.pool_key_id,
            chain_id=self.chain_id,
            observed_at=self.observed_at,
            available_at=self.available_at,
            payload=tuple(self.payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "source_priority": self.source_priority,
            "kind": self.kind,
            "pool_key_id": self.pool_key_id,
            "chain_id": self.chain_id,
            "observed_at": self.observed_at,
            "available_at": self.available_at,
            "payload": [[k, v] for k, v in self.payload],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SerialisedEvent:
        try:
            raw_payload = payload["payload"]
        except KeyError as exc:
            raise InvalidManifestFieldError(
                f"SerialisedEvent.from_dict: missing key {exc.args[0]!r}"
            ) from exc
        if not isinstance(raw_payload, list):
            raise InvalidManifestFieldError(
                "SerialisedEvent.from_dict: payload must be a list of [k, v] pairs"
            )
        normalised_payload: list[tuple[str, int | str | bool]] = []
        for entry in raw_payload:
            if not isinstance(entry, list) or len(entry) != 2:
                raise InvalidManifestFieldError(
                    "SerialisedEvent.from_dict: payload entry must be a [k, v] pair"
                )
            k, v = entry
            if not isinstance(k, str) or not k:
                raise InvalidManifestFieldError(
                    "SerialisedEvent.from_dict: payload key must be non-empty str"
                )
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                raise InvalidManifestFieldError(
                    "SerialisedEvent.from_dict: payload value must be int|str|bool"
                )
            normalised_payload.append((k, v))
        normalised_payload.sort(key=lambda kv: kv[0])
        try:
            return cls(
                version=str(payload["version"]),
                timestamp=int(payload["timestamp"]),
                sequence=int(payload["sequence"]),
                source_priority=int(payload["source_priority"]),
                kind=str(payload["kind"]),
                pool_key_id=str(payload["pool_key_id"]),
                chain_id=int(payload["chain_id"]),
                observed_at=int(payload["observed_at"]),
                available_at=int(payload["available_at"]),
                payload=tuple(normalised_payload),
            )
        except KeyError as exc:
            raise InvalidManifestFieldError(
                f"SerialisedEvent.from_dict: missing key {exc.args[0]!r}"
            ) from exc


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    """The per-pool manifest a run publishes.

    The manifest binds the result to its inputs: dataset / schema /
    code / dependency revisions, chain, ``PoolKey``, interval, block
    bounds, dataset version, reporting numeraire and its valuation
    qualification, registry binding (T105: strategy identity, version,
    parameter-schema checksum, code provenance), and the four
    checksum slots the reconciliation contract depends on
    (decisions, ledger, metrics, coverage, report). The coverage
    checksum is carried separately so a coverage gap cannot be hidden
    behind a passing manifest checksum.

    Every quantity carries an explicit unit (the field docstring
    states it). The validation layer rejects any manifest whose
    ``dataset_version`` or ``reporting_numeraire`` is missing, or
    whose recorded numeraire disagrees with the dataset's
    qualification record. The ``pool_key_id`` and ``chain_id`` are
    the per-pool invariant: every embedded event must carry the same
    pair, and the metrics / ledger / coverage / decisions checksums
    must all derive from results on this pair.

    Field-by-field units:

    - ``version`` — manifest schema version (string).
    - ``run_id`` — multi-pool run identity (string).
    - ``chain_id`` — positive int (chain ID).
    - ``pool_key_id`` — hex string of the canonical V4 PoolKey digest.
    - ``block_range_start`` / ``block_range_end`` — non-negative
      integers (block numbers).
    - ``interval_seconds`` — positive integer seconds.
    - ``dataset_version`` — non-empty string (DS-001).
    - ``dataset_schema_version`` — non-negative integer.
    - ``dataset_decode_version`` — non-negative integer.
    - ``dataset_content_hash`` — non-empty hex digest string.
    - ``reporting_numeraire`` — non-empty string (e.g. ``"USDG"``,
      ``"ETH"``, ``"RELATIVE_ONLY"``).
    - ``valuation_qualification`` — ``"QUALIFIED"`` or
      ``"RELATIVE_ONLY"`` per ADR-014 §3.
    - ``code_revision`` — git SHA, or :data:`UNKNOWN_CODE_REVISION`.
    - ``dependency_revisions`` — sorted mapping of package name to
      version string (or :data:`UNKNOWN_DEPENDENCY_REVISION`).
    - ``strategy_identity`` — registered strategy identity string
      (T105 replaces the T063 ``strategy_kind`` vocabulary).
    - ``strategy_version`` — per-identity version string the registry
      captured at binding time.
    - ``registry_version`` — registry schema version string.
    - ``registry_checksum`` — registry canonical SHA-256 hex digest.
    - ``parameter_schema_version`` — per-identity parameter-schema
      version string.
    - ``parameter_schema_checksum`` — per-identity schema canonical
      SHA-256 hex digest.
    - ``code_provenance_module`` — dotted module path of the
      registered implementation.
    - ``code_provenance_revision`` — git SHA, or ``"UNKNOWN"``.
    - ``code_provenance_symbol`` — symbol name (empty when the entry
      does not bind a symbol).
    - ``strategy_params`` — sorted mapping of validated parameter
      name to a JSON-serializable scalar (``int``/``str``/``bool``).
    - ``seed`` — non-negative integer.
    - ``clock_assumption`` — :data:`VALID_CLOCK_ASSUMPTIONS`.
    - ``fill_assumption`` — :data:`VALID_FILL_ASSUMPTIONS`.
    - ``cost_assumption`` — :data:`VALID_COST_ASSUMPTIONS`.
    - ``quote_assumption`` — :data:`VALID_QUOTE_ASSUMPTIONS`.
    - ``latency_units`` — non-negative integer (latency assumption).
    - ``latency_ms_estimate`` — non-negative integer (latency
      assumption in milliseconds — must be supplied so the
      acceptance clause "latency" unit is satisfied).
    - ``decisions_checksum`` — hex digest string.
    - ``ledger_checksum`` — hex digest string.
    - ``metrics_checksum`` — hex digest string.
    - ``coverage_checksum`` — hex digest string.
    - ``report_checksum`` — hex digest string of the canonical
      serialisation of every other field.
    - ``metrics_version`` — version string of the metrics layer
      the run consumed. Part of the report checksum's input so a
      code drift surfaces as a checksum mismatch.
    - ``input_event_list`` — the canonical sorted event list
      embedded for one-command rerun.
    - ``created_at_unix_seconds`` — integer Unix seconds the manifest
      was first written. Used for the "do not overwrite" rule: a
      later attempt to publish the same ``run_id`` at a different
      timestamp is rejected by the publish gate.
    """

    version: str
    run_id: str
    chain_id: int
    pool_key_id: str
    block_range_start: int
    block_range_end: int
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
    input_event_list: tuple[SerialisedEvent, ...]
    created_at_unix_seconds: int

    def __post_init__(self) -> None:
        _require_non_empty_str(self.version, field_name="ExperimentManifest.version")
        if self.version != MANIFEST_VERSION:
            raise InvalidManifestFieldError(
                f"ExperimentManifest.version: must be {MANIFEST_VERSION!r}, got {self.version!r}"
            )
        _require_non_empty_str(self.run_id, field_name="ExperimentManifest.run_id")
        _require_positive_int(self.chain_id, field_name="ExperimentManifest.chain_id")
        _require_non_empty_str(self.pool_key_id, field_name="ExperimentManifest.pool_key_id")
        _require_non_negative_int(
            self.block_range_start, field_name="ExperimentManifest.block_range_start"
        )
        _require_non_negative_int(
            self.block_range_end, field_name="ExperimentManifest.block_range_end"
        )
        if self.block_range_end < self.block_range_start:
            raise InvalidManifestFieldError(
                f"ExperimentManifest: block_range_end={self.block_range_end} "
                f"must be >= block_range_start={self.block_range_start}"
            )
        _require_positive_int(
            self.interval_seconds, field_name="ExperimentManifest.interval_seconds"
        )
        # ``dataset_version`` and ``reporting_numeraire`` are
        # checked for type only at construction so a malformed
        # record can still be loaded and surfaced through the
        # validation layer; :func:`validate_manifest` enforces the
        # "non-empty" policy on these two fields and rejects the
        # load with :class:`MissingRequiredFieldError`. The T063
        # acceptance clause binds this behaviour.
        _require_str(self.dataset_version, field_name="ExperimentManifest.dataset_version")
        _require_non_negative_int(
            self.dataset_schema_version,
            field_name="ExperimentManifest.dataset_schema_version",
        )
        _require_non_negative_int(
            self.dataset_decode_version,
            field_name="ExperimentManifest.dataset_decode_version",
        )
        _require_str(
            self.dataset_content_hash,
            field_name="ExperimentManifest.dataset_content_hash",
        )
        _require_str(
            self.reporting_numeraire,
            field_name="ExperimentManifest.reporting_numeraire",
        )
        if self.valuation_qualification not in _VALID_VALUATION_QUALIFICATIONS:
            raise InvalidManifestFieldError(
                f"ExperimentManifest.valuation_qualification: must be one of "
                f"{sorted(_VALID_VALUATION_QUALIFICATIONS)}, "
                f"got {self.valuation_qualification!r}"
            )
        _require_str(self.code_revision, field_name="ExperimentManifest.code_revision")
        if not isinstance(self.dependency_revisions, dict):
            raise InvalidManifestFieldError(
                f"ExperimentManifest.dependency_revisions: must be dict[str, str], "
                f"got {type(self.dependency_revisions).__name__}"
            )
        for pkg, ver in self.dependency_revisions.items():
            if not isinstance(pkg, str) or not isinstance(ver, str):
                raise InvalidManifestFieldError(
                    "ExperimentManifest.dependency_revisions: every key/value must be str"
                )
        # T105 — registry-bound strategy identity. The manifest does
        # not consult the deprecated ``VALID_STRATEGY_KINDS`` set; the
        # identity is bound at build time by :func:`bind_strategy_to_registry`
        # and verified at validation time by
        # :func:`assert_binding_matches_registry`.
        _require_non_empty_str(
            self.strategy_identity, field_name="ExperimentManifest.strategy_identity"
        )
        _require_non_empty_str(
            self.strategy_version, field_name="ExperimentManifest.strategy_version"
        )
        _require_non_empty_str(
            self.registry_version, field_name="ExperimentManifest.registry_version"
        )
        _require_non_empty_str(
            self.registry_checksum, field_name="ExperimentManifest.registry_checksum"
        )
        _require_non_empty_str(
            self.parameter_schema_version,
            field_name="ExperimentManifest.parameter_schema_version",
        )
        _require_non_empty_str(
            self.parameter_schema_checksum,
            field_name="ExperimentManifest.parameter_schema_checksum",
        )
        _require_non_empty_str(
            self.code_provenance_module,
            field_name="ExperimentManifest.code_provenance_module",
        )
        _require_non_empty_str(
            self.code_provenance_revision,
            field_name="ExperimentManifest.code_provenance_revision",
        )
        _require_str(
            self.code_provenance_symbol,
            field_name="ExperimentManifest.code_provenance_symbol",
        )
        if not isinstance(self.strategy_params, dict):
            raise InvalidManifestFieldError(
                f"ExperimentManifest.strategy_params: must be "
                f"dict[str, int|str|bool], got {type(self.strategy_params).__name__}"
            )
        for k, v in self.strategy_params.items():
            if not isinstance(k, str) or not k:
                raise InvalidManifestFieldError(
                    "ExperimentManifest.strategy_params: every key must be non-empty str"
                )
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                raise InvalidManifestFieldError(
                    "ExperimentManifest.strategy_params: every value must be int|str|bool"
                )
        _require_non_negative_int(self.seed, field_name="ExperimentManifest.seed")
        if self.clock_assumption not in VALID_CLOCK_ASSUMPTIONS:
            raise InvalidManifestFieldError(
                f"ExperimentManifest.clock_assumption: must be one of "
                f"{sorted(VALID_CLOCK_ASSUMPTIONS)}, got {self.clock_assumption!r}"
            )
        if self.fill_assumption not in VALID_FILL_ASSUMPTIONS:
            raise InvalidManifestFieldError(
                f"ExperimentManifest.fill_assumption: must be one of "
                f"{sorted(VALID_FILL_ASSUMPTIONS)}, got {self.fill_assumption!r}"
            )
        if self.cost_assumption not in VALID_COST_ASSUMPTIONS:
            raise InvalidManifestFieldError(
                f"ExperimentManifest.cost_assumption: must be one of "
                f"{sorted(VALID_COST_ASSUMPTIONS)}, got {self.cost_assumption!r}"
            )
        if self.quote_assumption not in VALID_QUOTE_ASSUMPTIONS:
            raise InvalidManifestFieldError(
                f"ExperimentManifest.quote_assumption: must be one of "
                f"{sorted(VALID_QUOTE_ASSUMPTIONS)}, got {self.quote_assumption!r}"
            )
        _require_non_negative_int(self.latency_units, field_name="ExperimentManifest.latency_units")
        _require_non_negative_int(
            self.latency_ms_estimate, field_name="ExperimentManifest.latency_ms_estimate"
        )
        for slot in (
            "decisions_checksum",
            "ledger_checksum",
            "metrics_checksum",
            "coverage_checksum",
            "report_checksum",
        ):
            value = getattr(self, slot)
            _require_non_empty_str(value, field_name=f"ExperimentManifest.{slot}")
        _require_non_empty_str(
            self.metrics_version, field_name="ExperimentManifest.metrics_version"
        )
        if not isinstance(self.input_event_list, tuple):
            raise InvalidManifestFieldError(
                f"ExperimentManifest.input_event_list: must be "
                f"tuple[SerialisedEvent, ...], got {type(self.input_event_list).__name__}"
            )
        _require_non_negative_int(
            self.created_at_unix_seconds,
            field_name="ExperimentManifest.created_at_unix_seconds",
        )

    # ------------------------------------------------------------------
    # Per-pool invariant checks
    # ------------------------------------------------------------------

    def pool_identity(self) -> tuple[int, str]:
        """Return the ``(chain_id, pool_key_id)`` this manifest is bound to."""
        return (self.chain_id, self.pool_key_id)

    def assert_events_match_pool(self) -> None:
        """Reject a manifest whose embedded events carry a foreign pool.

        The T063 acceptance clause binds this check: "a manifest that
        carries another pool's data or results fails validation".
        Every embedded event must carry the manifest's
        ``(chain_id, pool_key_id)``; a foreign event is a manifest
        integrity failure, not a soft warning.
        """
        for i, evt in enumerate(self.input_event_list):
            if (evt.chain_id, evt.pool_key_id) != self.pool_identity():
                raise ManifestPoolMismatchError(
                    f"ExperimentManifest: input_event_list[{i}] carries "
                    f"(chain_id={evt.chain_id}, pool_key_id={evt.pool_key_id!r}) "
                    f"which is not this manifest's pool "
                    f"(chain_id={self.chain_id}, pool_key_id={self.pool_key_id!r})"
                )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly ``dict`` representation.

        The serialisation sorts every mapping so two equivalent
        manifests produce byte-identical JSON. The report checksum
        slot is included so a downstream reader can verify the
        manifest without a separate integrity file; it is computed
        independently by :func:`compute_report_checksum` and recorded
        at construction time so a tampered field surfaces as a
        checksum mismatch.
        """
        return {
            "version": self.version,
            "run_id": self.run_id,
            "chain_id": self.chain_id,
            "pool_key_id": self.pool_key_id,
            "block_range_start": self.block_range_start,
            "block_range_end": self.block_range_end,
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
            "input_event_list": [evt.to_dict() for evt in self.input_event_list],
            "created_at_unix_seconds": self.created_at_unix_seconds,
        }

    def to_canonical_json(self) -> str:
        """Return the canonical JSON serialisation of this manifest.

        The function is the contract every consumer agrees on: the
        serialisation is the manifest. Two equivalent manifests in
        different field orders produce byte-identical strings.
        """
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _coverage_to_mapping(coverage: CoverageSummary) -> dict[str, Any]:
    """Return a JSON-friendly summary of the coverage record.

    The checksum is the contract; the rest is human-readable context
    for the reviewer.
    """
    return {
        "version": coverage.version,
        "chain_id": coverage.chain_id,
        "pool_key_id": coverage.pool_key_id,
        "interval_seconds": coverage.interval_seconds,
        "input_events_total": coverage.input_events_total,
        "data_events_total": coverage.data_events_total,
        "fill_observations_total": coverage.fill_observations_total,
        "audit_events_total": coverage.audit_events_total,
        "block_range_start": coverage.block_range_start,
        "block_range_end": coverage.block_range_end,
        "duration_seconds": coverage.duration_seconds,
        "data_gaps": [[a, b] for a, b in coverage.data_gaps],
        "coverage_checksum": coverage.coverage_checksum,
    }


def compute_report_checksum(manifest_payload: Mapping[str, Any]) -> str:
    """Return the SHA-256 hex digest of the canonical serialisation of ``payload``.

    The function binds every field except the four checksum slots so
    a tampered field surfaces as a checksum mismatch. The
    :data:`ExperimentManifest.report_checksum` is computed by this
    function at build time and compared by the validation layer on
    every load.
    """
    serialisable = dict(manifest_payload)
    # Strip the slots the checksum is independent of: a tampered
    # checksum is caught by the dedicated check; a tampered field is
    # caught by this checksum.
    serialisable.pop("report_checksum", None)
    content = json.dumps(serialisable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_experiment_manifest(
    *,
    run_id: str,
    metrics: RunMetrics,
    coverage: CoverageSummary,
    ledger_snapshot: LedgerSnapshot,
    decisions_checksum: str,
    input_events: Sequence[BacktestEvent],
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
    created_at_unix_seconds: int,
    block_range_start: int,
    block_range_end: int,
) -> ExperimentManifest:
    """Construct an :class:`ExperimentManifest` from a completed run.

    The function is the canonical builder: the per-run ledger
    snapshot, the run metrics, the coverage summary, the decision
    log, and the registry binding (T105) all carry their own
    deterministic metadata; this builder binds them onto the
    manifest and computes the report checksum.

    The builder enforces the per-pool invariant: the metrics
    record's ``(chain_id, pool_key_id)`` and the ledger snapshot's
    ``(chain_id, pool_key_id)`` and the coverage summary's
    ``(chain_id, pool_key_id)`` must all agree, and the embedded
    input event list is asserted to match the manifest's pool before
    the builder returns.

    The builder requires a pre-built :class:`StrategyBinding`. The
    binding captures the registry-derived fields
    (``strategy_identity``, ``strategy_version``, ``registry_version``,
    ``registry_checksum``, ``parameter_schema_version``,
    ``parameter_schema_checksum``, ``code_provenance_*``, validated
    parameters); the builder copies them verbatim onto the manifest
    and refuses any binding whose identity is unregistered or whose
    parameters violate the schema. A future revision may opt to
    accept ``identity`` + ``parameters`` and call
    :func:`bind_strategy_to_registry` internally, but the T105
    contract makes the binding the call site's responsibility so a
    caller cannot bypass the registry at the manifest layer.
    """
    pool_identity = (metrics.chain_id, metrics.pool_key_id)
    if pool_identity != (ledger_snapshot.chain_id, ledger_snapshot.pool_key_id):
        raise ManifestPoolMismatchError(
            f"build_experiment_manifest: metrics pool {pool_identity} disagrees "
            f"with ledger pool "
            f"({ledger_snapshot.chain_id}, {ledger_snapshot.pool_key_id!r})"
        )
    if pool_identity != (coverage.chain_id, coverage.pool_key_id):
        raise ManifestPoolMismatchError(
            f"build_experiment_manifest: metrics pool {pool_identity} disagrees "
            f"with coverage pool "
            f"({coverage.chain_id}, {coverage.pool_key_id!r})"
        )

    serialised = tuple(SerialisedEvent.from_backtest_event(e) for e in input_events)

    payload: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "run_id": run_id,
        "chain_id": metrics.chain_id,
        "pool_key_id": metrics.pool_key_id,
        "block_range_start": block_range_start,
        "block_range_end": block_range_end,
        "interval_seconds": metrics.interval_seconds,
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
        "strategy_params": dict(sorted(binding_parameter_dict(strategy_binding).items())),
        "seed": seed,
        "clock_assumption": clock_assumption,
        "fill_assumption": fill_assumption,
        "cost_assumption": cost_assumption,
        "quote_assumption": quote_assumption,
        "latency_units": latency_units,
        "latency_ms_estimate": latency_ms_estimate,
        "decisions_checksum": decisions_checksum,
        "ledger_checksum": ledger_snapshot.ledger_checksum,
        "metrics_checksum": metrics.metrics_checksum,
        "coverage_checksum": coverage.coverage_checksum,
        "report_checksum": "",  # placeholder, filled in below
        "metrics_version": METRICS_VERSION,
        "input_event_list": [evt.to_dict() for evt in serialised],
        "created_at_unix_seconds": created_at_unix_seconds,
    }
    report_checksum = compute_report_checksum(payload)
    payload["report_checksum"] = report_checksum

    manifest = ExperimentManifest(
        version=MANIFEST_VERSION,
        run_id=run_id,
        chain_id=metrics.chain_id,
        pool_key_id=metrics.pool_key_id,
        block_range_start=block_range_start,
        block_range_end=block_range_end,
        interval_seconds=metrics.interval_seconds,
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
        strategy_params=dict(sorted(binding_parameter_dict(strategy_binding).items())),
        seed=seed,
        clock_assumption=clock_assumption,
        fill_assumption=fill_assumption,
        cost_assumption=cost_assumption,
        quote_assumption=quote_assumption,
        latency_units=latency_units,
        latency_ms_estimate=latency_ms_estimate,
        decisions_checksum=decisions_checksum,
        ledger_checksum=ledger_snapshot.ledger_checksum,
        metrics_checksum=metrics.metrics_checksum,
        coverage_checksum=coverage.coverage_checksum,
        report_checksum=report_checksum,
        metrics_version=METRICS_VERSION,
        input_event_list=serialised,
        created_at_unix_seconds=created_at_unix_seconds,
    )
    # Per-pool invariant: every embedded event must carry this
    # manifest's pool. The builder runs this check before returning
    # so the constructed manifest is internally consistent.
    manifest.assert_events_match_pool()
    return manifest


def manifest_checksum(manifest: ExperimentManifest) -> str:
    """Return the SHA-256 hex digest of the manifest's canonical JSON.

    The function is the manifest-level checksum the validation layer
    compares against an externally supplied digest. It is the same
    hash as :attr:`ExperimentManifest.report_checksum` (computed
    from the same canonical serialisation); the dedicated function
    exists so the validation layer can call it without going through
    the dataclass field.
    """
    return compute_report_checksum(manifest.to_dict())


# ---------------------------------------------------------------------------
# Deserialisation
# ---------------------------------------------------------------------------


def serialised_event_from_dict(payload: Mapping[str, Any]) -> SerialisedEvent:
    return SerialisedEvent.from_dict(payload)


def experiment_manifest_from_dict(payload: Mapping[str, Any]) -> ExperimentManifest:
    """Reconstruct an :class:`ExperimentManifest` from a JSON-friendly ``dict``.

    The function is the loader half of the manifest contract: the
    validation layer calls it on a deserialised JSON object, then
    validates every checksum / requirement. The loader rejects a
    foreign pool at load time so a tampered file fails before any
    engine call.
    """
    if not isinstance(payload, Mapping):
        raise InvalidManifestFieldError(
            f"experiment_manifest_from_dict: payload must be Mapping, got {type(payload).__name__}"
        )
    try:
        raw_events = payload["input_event_list"]
    except KeyError as exc:
        raise InvalidManifestFieldError(
            f"experiment_manifest_from_dict: missing key {exc.args[0]!r}"
        ) from exc
    if not isinstance(raw_events, list):
        raise InvalidManifestFieldError(
            "experiment_manifest_from_dict: input_event_list must be a list"
        )
    serialised = tuple(
        SerialisedEvent.from_dict(entry) for entry in raw_events if isinstance(entry, Mapping)
    )
    try:
        manifest = ExperimentManifest(
            version=str(payload["version"]),
            run_id=str(payload["run_id"]),
            chain_id=int(payload["chain_id"]),
            pool_key_id=str(payload["pool_key_id"]),
            block_range_start=int(payload["block_range_start"]),
            block_range_end=int(payload["block_range_end"]),
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
            input_event_list=serialised,
            created_at_unix_seconds=int(payload["created_at_unix_seconds"]),
        )
    except KeyError as exc:
        raise InvalidManifestFieldError(
            f"experiment_manifest_from_dict: missing key {exc.args[0]!r}"
        ) from exc
    manifest.assert_events_match_pool()
    return manifest


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def build_strategy_binding(
    identity: str,
    parameters: Mapping[str, object],
) -> StrategyBinding:
    """Convenience wrapper around :func:`bind_strategy_to_registry`.

    The helper exists so call sites that build manifests do not need
    to import the registry-binding module directly; the manifest
    module re-exports the binding surface for ergonomics.
    """
    return bind_strategy_to_registry(identity=identity, parameters=parameters)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "MANIFEST_VERSION",
    "RegistryBindingError",
    "StrategyBinding",
    "UNKNOWN_CODE_REVISION",
    "UNKNOWN_DEPENDENCY_REVISION",
    "VALID_CLOCK_ASSUMPTIONS",
    "VALID_COST_ASSUMPTIONS",
    "VALID_FILL_ASSUMPTIONS",
    "VALID_QUOTE_ASSUMPTIONS",
    "VALID_STRATEGY_KINDS",
    "ExperimentManifest",
    "InvalidManifestFieldError",
    "ManifestError",
    "ManifestPoolMismatchError",
    "SerialisedEvent",
    "VALUATION_QUALIFIED",
    "VALUATION_RELATIVE_ONLY",
    "assert_binding_matches_registry",
    "bind_strategy_to_registry",
    "build_experiment_manifest",
    "build_strategy_binding",
    "compute_report_checksum",
    "experiment_manifest_from_dict",
    "manifest_checksum",
    "serialised_event_from_dict",
]
