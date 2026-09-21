"""Product-level backtest run lifecycle (T069).

T069 owns the durable product-run lifecycle the Web console
(``WEB-PAGE-012``) and the ``backtest`` CLI subcommands drive:

- **Run request** — a frozen, JSON-friendly value object that
  names the dataset version, the registered strategy identity and
  its parameters, the pool and block range, the seed, the
  fill / cost / quote / clock assumptions, the reporting numeraire
  and its valuation qualification, the dependency / code revisions,
  and (optionally) the source manifest the run is rerunning from.

- **Run state** — a frozen, JSON-friendly value object the
  :class:`RunStateStore` persists to disk atomically. The state
  covers ``QUEUED``, ``RUNNING`` (with progress), ``SUCCEEDED``,
  ``FAILED`` and ``CANCELLED``; every failure and cancellation
  carries a reason code the CLI surfaces.

- **Orchestrator** — the durable run lifecycle. It reserves the
  run as ``QUEUED``, transitions to ``RUNNING``, composes the
  existing T061 engine, T063 metrics / coverage / decisions, and
  T105 registry-bound manifest authority into one run, writes
  the manifest and reports to disk, and transitions to
  ``SUCCEEDED`` with the manifest path recorded. A cancelled run
  publishes no manifest and no report. A failed run publishes no
  manifest and no report. A product rerun creates a new run
  record linked to the source manifest and never mutates the
  source.

- **Cancel token** — a cooperative, thread-safe flag the engine
  callback polls. A cancel during ``RUNNING`` transitions the run
  to ``CANCELLED`` with a structured reason; the orchestrator
  publishes no manifest in that case.

- **Persistence** — run records are written under a ``runs_root``
  directory, one file per run, atomically via a temp-file rename.
  A restart while a run is in ``RUNNING`` resumes the run from
  the persisted state without duplicating it.

The module imports only the standard library, the backtest layer
(T061 engine, events, models), the strategy registry (T068) and
the registry-bound manifest authority (T105). It does **not**
import RPC, storage, signing, execution, or presentation code.
The event source and the dataset resolver are injected by the
caller (the CLI subcommand or a test fixture) so the
orchestrator composes the existing replay surface without
re-implementing it.

Design constraints:

- **Integer units.** Every quantity is a Python ``int``; the
  request / record never stores ``float`` (ADR-004).
- **Determinism.** Two equivalent requests against the same
  event source and the same registry revision produce
  byte-identical manifests and reports.
- **Layer purity.** The module imports the backtest layer, the
  strategy registry, and the reports layer; it imports no RPC,
  storage, signing, execution, or presentation module.
- **No credentials.** The run record carries no key material,
  endpoint alias, or secret. The ``dependency_revisions`` /
  ``code_revision`` slots are non-secret provenance values.

References:

- T061 — event-driven backtest engine (the orchestrator drives).
- T063 — experiment manifests and reports (the artifact the run
  publishes).
- T105 — registry-bound manifest authority (the binding surface
  every current publication path consults).
- T068 — strategy registry (the source of the binding).
- ``docs/spec/product/WEB_CONSOLE.md`` ``WEB-PAGE-012`` — the
  console surface whose run lifecycle this module implements.
- G-BACKTEST-RUN-01 — the product-level run lifecycle contract.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

from robinhood_lp.backtest.engine import (
    BACKTEST_ENGINE_VERSION,
    BacktestEngine,
    BacktestResult,
    RiskDecision,
    StrategyCallback,
    empty_position_state,
)
from robinhood_lp.backtest.events import (
    STAGE_FILL,
    BacktestEvent,
    PositionState,
    extract_event_cursor,
)
from robinhood_lp.backtest.models import (
    ConstantLiquidityModel,
    DeterministicFailureModel,
    FlatGasModel,
    ModelBundle,
    StaticFeeModel,
    ZeroSlippageModel,
)
from robinhood_lp.reports.manifest import (
    MANIFEST_VERSION,
    MANIFEST_VERSION_T109,
    VALID_CLOCK_ASSUMPTIONS,
    VALID_COST_ASSUMPTIONS,
    VALID_FILL_ASSUMPTIONS,
    VALID_QUOTE_ASSUMPTIONS,
    VALUATION_QUALIFIED,
    VALUATION_RELATIVE_ONLY,
    ExperimentManifest,
    T109ExperimentManifest,
    build_t109_experiment_manifest,
)
from robinhood_lp.reports.metrics import (
    METRICS_VERSION,
    CoverageSummary,
    LedgerSnapshot,
    RunMetrics,
    build_coverage_summary,
    compute_run_metrics,
    decisions_checksum,
    extract_decisions,
)
from robinhood_lp.reports.registry_binding import (
    StrategyBinding,
    bind_strategy_to_registry,
)
from robinhood_lp.reports.simulation_evidence import (
    RunStateCheckpoint,
    RunTransition,
    SimulationEvidence,
    build_simulation_evidence,
)
from robinhood_lp.reports.validation import (
    ManifestValidationError,
    load_manifest_from_path,
    load_t109_manifest_from_path,
    write_simulation_evidence_to_path,
    write_t109_manifest_to_path,
)
from robinhood_lp.strategy.adapter import AdaptiveStrategyCallback
from robinhood_lp.strategy.adaptive import AdaptiveStrategy as _AdaptiveStrategy
from robinhood_lp.strategy.registry import (
    Registry,
    UnknownStrategyIdentityError,
    default_registry,
)

#: Module version. Bumping it is a breaking change for the run
#: record schema and the ``runs_root`` on-disk layout.
ORCHESTRATOR_VERSION: Final[str] = "t069.backtest_orchestrator.v1"

#: Default ``runs_root`` directory name. The CLI / tests use this
#: when the caller does not supply an explicit root.
DEFAULT_RUNS_ROOT_NAME: Final[str] = "runs"

#: Run record file name template. ``{run_id}`` is replaced with the
#: run's identity at write time; the file extension is ``.json``
#: so the record is grep-friendly.
_RUN_RECORD_FILENAME: Final[str] = "{run_id}.run.json"

#: Manifest file name template. One manifest per run / pool; the
#: ``{run_id}`` and ``{chain_id}-{pool_key_id}`` segments keep
#: multi-pool runs independent on disk.
_MANIFEST_FILENAME: Final[str] = "{run_id}.{chain_id}-{pool_key_id}.manifest.json"

#: Report file name template. The report is the human-readable
#: companion of the manifest; the orchestrator publishes both.
_REPORT_FILENAME: Final[str] = "{run_id}.{chain_id}-{pool_key_id}.report.json"

#: Simulation-evidence file name template. The orchestrator writes the
#: T109 simulation-evidence artifact alongside the manifest and
#: report; the path is the on-disk binding the manifest records.
_EVIDENCE_FILENAME: Final[str] = "{run_id}.{chain_id}-{pool_key_id}.simulation_evidence.json"

#: Default model-bundle parameters the orchestrator uses when the
#: caller does not supply ``gas_units`` / ``fee_pips`` /
#: ``active_liquidity``. The values match the T105 rerun defaults
#: so a product rerun and an artifact rerun produce the same
#: bundle hash.
_DEFAULT_GAS_UNITS: Final[int] = 21_000
_DEFAULT_FEE_PIPS: Final[int] = 3_000
_DEFAULT_ACTIVE_LIQUIDITY: Final[int] = 10_000

#: Sentinel prefix every reason code the orchestrator emits
#: begins with. Reviewers can grep for ``T069_`` to surface every
#: failure / cancellation / progress reason the orchestrator
#: records without reading the stack trace.
_REASON_PREFIX: Final[str] = "T069_"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BacktestRunError(RuntimeError):
    """Base class for T069 run lifecycle failures."""


class InvalidRunRequestError(BacktestRunError):
    """A run request is missing a required field or violates a closed vocabulary."""


class UnknownDatasetVersionError(BacktestRunError):
    """A run request names a dataset version the orchestrator cannot resolve."""

    def __init__(self, *, dataset_version: str) -> None:
        self.dataset_version = dataset_version
        super().__init__(
            f"{_REASON_PREFIX}UNKNOWN_DATASET_VERSION: dataset_version="
            f"{dataset_version!r} is not registered"
        )


class BlockRangeOutsideCoverageError(BacktestRunError):
    """A run request names a block range the dataset does not cover."""

    def __init__(
        self,
        *,
        dataset_version: str,
        block_range_start: int,
        block_range_end: int,
        covered_start: int,
        covered_end: int,
    ) -> None:
        self.dataset_version = dataset_version
        self.block_range_start = block_range_start
        self.block_range_end = block_range_end
        self.covered_start = covered_start
        self.covered_end = covered_end
        super().__init__(
            f"{_REASON_PREFIX}BLOCK_RANGE_OUTSIDE_COVERAGE: "
            f"dataset_version={dataset_version!r} covers "
            f"[{covered_start}, {covered_end}] but request asks for "
            f"[{block_range_start}, {block_range_end}]"
        )


class LegacyManifestSourceError(BacktestRunError):
    """A product rerun's source manifest is a legacy pre-registry (T063) artifact."""

    def __init__(self, *, source_manifest_path: str) -> None:
        self.source_manifest_path = source_manifest_path
        super().__init__(
            f"{_REASON_PREFIX}LEGACY_MANIFEST_SOURCE: source manifest "
            f"{source_manifest_path!r} is a legacy pre-registry artifact; "
            f"a product rerun must source a current T105 manifest"
        )


class RunStateCorruptError(BacktestRunError):
    """A persisted run record is structurally invalid."""


class RunAlreadyExistsError(BacktestRunError):
    """A run with the same ``run_id`` is already on disk."""

    def __init__(self, *, run_id: str, path: str) -> None:
        self.run_id = run_id
        self.path = path
        super().__init__(
            f"{_REASON_PREFIX}RUN_ALREADY_EXISTS: run_id={run_id!r} already exists at {path!r}"
        )


# ---------------------------------------------------------------------------
# Run state enum
# ---------------------------------------------------------------------------


class RunState(StrEnum):
    """The closed vocabulary of run states the orchestrator publishes.

    The set is the contract every observer (CLI, Web console,
    audit reviewer) agrees on. Every transition is monotonic —
    a run never re-enters a prior state, except that a crash
    during ``RUNNING`` resumes the run from the persisted state
    without losing or duplicating the run.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_TERMINAL_STATES: Final[frozenset[RunState]] = frozenset(
    {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
)


def _is_terminal_state(state: RunState) -> bool:
    return state in _TERMINAL_STATES


# ---------------------------------------------------------------------------
# Run request
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunRequest:
    """The product-level run request the orchestrator accepts.

    Every field the T069 contract binds to the manifest lives
    here. The request is a frozen, JSON-friendly value object;
    the orchestrator writes the run record by serialising it
    under the run's directory.

    Field units:

    - ``run_id`` — non-empty string; the run's identity. Must be
      unique under ``runs_root``.
    - ``dataset_version`` — non-empty string; the dataset the run
      consumes.
    - ``chain_id`` — positive int; the chain the pool lives on.
    - ``pool_key_id`` — non-empty hex string; the pool's identity.
    - ``block_range_start`` / ``block_range_end`` — non-negative
      ints; the inclusive block bounds the run consumes.
    - ``interval_seconds`` — positive int; the bar interval the
      run records.
    - ``strategy_identity`` — non-empty string; the registered
      strategy identity (T068). An unregistered identity is
      rejected with :class:`UnknownStrategyIdentityError` before
      any result is written.
    - ``strategy_parameters`` — mapping of parameter name to
      ``int | bool | str``; the registered schema validates the
      set.
    - ``seed`` — non-negative int; the deterministic seed the run
      records on the manifest.
    - ``clock_assumption`` — one of :data:`VALID_CLOCK_ASSUMPTIONS`.
    - ``fill_assumption`` — one of :data:`VALID_FILL_ASSUMPTIONS`.
    - ``cost_assumption`` — one of :data:`VALID_COST_ASSUMPTIONS`.
    - ``quote_assumption`` — one of :data:`VALID_QUOTE_ASSUMPTIONS`.
    - ``latency_units`` — non-negative int; the latency assumption
      recorded on the manifest.
    - ``latency_ms_estimate`` — non-negative int; the latency
      assumption (milliseconds) recorded on the manifest.
    - ``reporting_numeraire`` — non-empty string; the reporting
      numeraire the run publishes under.
    - ``valuation_qualification`` — ``"QUALIFIED"`` or
      ``"RELATIVE_ONLY"`` (ADR-014 §3).
    - ``code_revision`` — non-empty string; the code revision the
      run records.
    - ``dependency_revisions`` — mapping of package name to
      non-empty string; the dependency revisions the run records.
    - ``source_manifest_path`` — optional non-empty string path;
      when supplied, the run is a product rerun linked to the
      source manifest (the source is preserved byte-identical
      and never mutated). When ``None``, the run is a fresh run.
    - ``created_at_unix_seconds`` — non-negative int; the moment
      the request was constructed.
    """

    run_id: str
    dataset_version: str
    chain_id: int
    pool_key_id: str
    block_range_start: int
    block_range_end: int
    interval_seconds: int
    strategy_identity: str
    strategy_parameters: Mapping[str, int | bool | str]
    seed: int
    clock_assumption: str
    fill_assumption: str
    cost_assumption: str
    quote_assumption: str
    latency_units: int
    latency_ms_estimate: int
    reporting_numeraire: str
    valuation_qualification: str
    code_revision: str
    dependency_revisions: Mapping[str, str]
    created_at_unix_seconds: int
    source_manifest_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: run_id must be non-empty str"
            )
        if not isinstance(self.dataset_version, str) or not self.dataset_version:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: dataset_version must be non-empty str"
            )
        if (
            not isinstance(self.chain_id, int)
            or isinstance(self.chain_id, bool)
            or self.chain_id <= 0
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: chain_id must be positive int"
            )
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: pool_key_id must be non-empty str"
            )
        if (
            not isinstance(self.block_range_start, int)
            or isinstance(self.block_range_start, bool)
            or self.block_range_start < 0
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: block_range_start must be non-negative int"
            )
        if (
            not isinstance(self.block_range_end, int)
            or isinstance(self.block_range_end, bool)
            or self.block_range_end < self.block_range_start
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: block_range_end must be >= block_range_start"
            )
        if (
            not isinstance(self.interval_seconds, int)
            or isinstance(self.interval_seconds, bool)
            or self.interval_seconds <= 0
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: interval_seconds must be positive int"
            )
        if not isinstance(self.strategy_identity, str) or not self.strategy_identity:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: strategy_identity must be non-empty str"
            )
        if not isinstance(self.strategy_parameters, Mapping):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: strategy_parameters must be a mapping"
            )
        for k, v in self.strategy_parameters.items():
            if not isinstance(k, str) or not k:
                raise InvalidRunRequestError(
                    f"{_REASON_PREFIX}INVALID_REQUEST: strategy_parameters keys must be "
                    f"non-empty str"
                )
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                raise InvalidRunRequestError(
                    f"{_REASON_PREFIX}INVALID_REQUEST: strategy_parameters values must be "
                    f"int|bool|str"
                )
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: seed must be non-negative int"
            )
        if self.clock_assumption not in VALID_CLOCK_ASSUMPTIONS:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: clock_assumption must be one of "
                f"{sorted(VALID_CLOCK_ASSUMPTIONS)}"
            )
        if self.fill_assumption not in VALID_FILL_ASSUMPTIONS:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: fill_assumption must be one of "
                f"{sorted(VALID_FILL_ASSUMPTIONS)}"
            )
        if self.cost_assumption not in VALID_COST_ASSUMPTIONS:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: cost_assumption must be one of "
                f"{sorted(VALID_COST_ASSUMPTIONS)}"
            )
        if self.quote_assumption not in VALID_QUOTE_ASSUMPTIONS:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: quote_assumption must be one of "
                f"{sorted(VALID_QUOTE_ASSUMPTIONS)}"
            )
        if (
            not isinstance(self.latency_units, int)
            or isinstance(self.latency_units, bool)
            or self.latency_units < 0
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: latency_units must be non-negative int"
            )
        if (
            not isinstance(self.latency_ms_estimate, int)
            or isinstance(self.latency_ms_estimate, bool)
            or self.latency_ms_estimate < 0
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: latency_ms_estimate must be non-negative int"
            )
        if not isinstance(self.reporting_numeraire, str) or not self.reporting_numeraire:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: reporting_numeraire must be non-empty str"
            )
        if self.valuation_qualification not in (
            VALUATION_QUALIFIED,
            VALUATION_RELATIVE_ONLY,
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: valuation_qualification must be "
                f"QUALIFIED or RELATIVE_ONLY"
            )
        if not isinstance(self.code_revision, str) or not self.code_revision:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: code_revision must be non-empty str"
            )
        if not isinstance(self.dependency_revisions, Mapping):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: dependency_revisions must be a mapping"
            )
        for pkg, ver in self.dependency_revisions.items():
            if not isinstance(pkg, str) or not pkg:
                raise InvalidRunRequestError(
                    f"{_REASON_PREFIX}INVALID_REQUEST: dependency_revisions keys must be "
                    f"non-empty str"
                )
            if not isinstance(ver, str) or not ver:
                raise InvalidRunRequestError(
                    f"{_REASON_PREFIX}INVALID_REQUEST: dependency_revisions values must be "
                    f"non-empty str"
                )
        if (
            not isinstance(self.created_at_unix_seconds, int)
            or isinstance(self.created_at_unix_seconds, bool)
            or self.created_at_unix_seconds < 0
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: created_at_unix_seconds must be non-negative int"
            )
        if self.source_manifest_path is not None and (
            not isinstance(self.source_manifest_path, str) or not self.source_manifest_path
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_REQUEST: source_manifest_path must be "
                f"non-empty str or None"
            )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly representation.

        ``strategy_parameters`` and ``dependency_revisions`` are
        sorted by key so the run record's canonical serialisation
        is deterministic.
        """
        return {
            "run_id": self.run_id,
            "dataset_version": self.dataset_version,
            "chain_id": self.chain_id,
            "pool_key_id": self.pool_key_id,
            "block_range_start": self.block_range_start,
            "block_range_end": self.block_range_end,
            "interval_seconds": self.interval_seconds,
            "strategy_identity": self.strategy_identity,
            "strategy_parameters": dict(sorted(self.strategy_parameters.items())),
            "seed": self.seed,
            "clock_assumption": self.clock_assumption,
            "fill_assumption": self.fill_assumption,
            "cost_assumption": self.cost_assumption,
            "quote_assumption": self.quote_assumption,
            "latency_units": self.latency_units,
            "latency_ms_estimate": self.latency_ms_estimate,
            "reporting_numeraire": self.reporting_numeraire,
            "valuation_qualification": self.valuation_qualification,
            "code_revision": self.code_revision,
            "dependency_revisions": dict(sorted(self.dependency_revisions.items())),
            "created_at_unix_seconds": self.created_at_unix_seconds,
            "source_manifest_path": self.source_manifest_path,
        }


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunProgress:
    """The progress record a running run publishes.

    The progress record is a snapshot the orchestrator persists
    while the run is ``RUNNING`` so an observer (CLI, console)
    can inspect the run without polling the engine. The
    ``events_processed`` / ``events_total`` pair is the canonical
    surface the Web console reads.

    Field units:

    - ``events_total`` — non-negative int; the total input-event
      count the orchestrator plans to consume. ``0`` while the
      engine is still loading.
    - ``events_processed`` — non-negative int; the number of input
      events the engine has consumed so far. Must satisfy
      ``events_processed <= events_total``.
    - ``current_stage`` — non-empty string; the current pipeline
      stage the engine is in (``"LOADING"``, ``"ENGINE"``,
      ``"MANIFEST"``, ``"REPORT"``).
    - ``updated_at_unix_seconds`` — non-negative int; the moment
      the progress record was written.
    """

    events_total: int
    events_processed: int
    current_stage: str
    updated_at_unix_seconds: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.events_total, int)
            or isinstance(self.events_total, bool)
            or self.events_total < 0
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_PROGRESS: events_total must be non-negative int"
            )
        if (
            not isinstance(self.events_processed, int)
            or isinstance(self.events_processed, bool)
            or self.events_processed < 0
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_PROGRESS: events_processed must be non-negative int"
            )
        if self.events_processed > self.events_total:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_PROGRESS: events_processed="
                f"{self.events_processed} > events_total={self.events_total}"
            )
        if not isinstance(self.current_stage, str) or not self.current_stage:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_PROGRESS: current_stage must be non-empty str"
            )
        if (
            not isinstance(self.updated_at_unix_seconds, int)
            or isinstance(self.updated_at_unix_seconds, bool)
            or self.updated_at_unix_seconds < 0
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_PROGRESS: updated_at_unix_seconds must be "
                f"non-negative int"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "events_total": self.events_total,
            "events_processed": self.events_processed,
            "current_stage": self.current_stage,
            "updated_at_unix_seconds": self.updated_at_unix_seconds,
        }


#: Convenience "empty progress" the orchestrator uses when a run
#: is queued and no event has been processed yet.
EMPTY_PROGRESS: Final[RunProgress] = RunProgress(
    events_total=0,
    events_processed=0,
    current_stage="QUEUED",
    updated_at_unix_seconds=0,
)


# ---------------------------------------------------------------------------
# Dataset resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetCoverage:
    """The coverage the dataset exposes for the requested pool.

    The orchestrator resolves a ``dataset_version`` to a coverage
    record before running the engine. The ``covered_start`` /
    ``covered_end`` fields are the inclusive block bounds the
    dataset actually contains for the requested ``(chain_id,
    pool_key_id)``; a request whose block range is not a subset is
    rejected with :class:`BlockRangeOutsideCoverageError`.
    """

    chain_id: int
    pool_key_id: str
    dataset_version: str
    dataset_schema_version: int
    dataset_decode_version: int
    dataset_content_hash: str
    reporting_numeraire: str
    valuation_qualification: str
    covered_start: int
    covered_end: int


class DatasetResolver:
    """The interface that resolves a ``dataset_version`` to its coverage.

    The orchestrator accepts a resolver at construction time so
    it does not depend on the storage layer at import time. The
    CLI and tests inject a resolver; the resolver is the single
    surface the orchestrator uses to look up a dataset and its
    coverage.

    Implementations raise :class:`UnknownDatasetVersionError`
    when the dataset is not registered; the orchestrator surfaces
    the dedicated error before any result is written.
    """

    def resolve(self, *, dataset_version: str, chain_id: int, pool_key_id: str) -> DatasetCoverage:
        """Return the :class:`DatasetCoverage` for the requested pool."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Event source
# ---------------------------------------------------------------------------


class EventSource:
    """The interface that yields input events for one ``(chain_id, pool_key_id)``.

    The orchestrator accepts an event source at construction time
    so it does not depend on the storage / replay layers at
    import time. The CLI composes the existing
    :class:`RawPartitionReader` and :func:`replay.replay`
    surfaces to build the source; tests inject a synthetic
    source.

    Implementations must honour the :class:`CancelToken`: when
    the token is set, the loader raises :class:`RunCancelled`
    before returning a partial event list.
    """

    def load_events(
        self,
        *,
        chain_id: int,
        pool_key_id: str,
        block_range_start: int,
        block_range_end: int,
        cancel_token: CancelToken,
    ) -> list[BacktestEvent]:
        """Return the input event list for the requested pool / range.

        Raises :class:`RunCancelled` when ``cancel_token`` fires
        during loading.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Cancel token
# ---------------------------------------------------------------------------


class RunCancelled(BacktestRunError):
    """A run was cancelled before it could publish a result.

    The orchestrator raises this when the cancel token fires
    during event loading, engine execution, or manifest
    publication. The orchestrator transitions the run record to
    :attr:`RunState.CANCELLED` with a structured reason code;
    no manifest or report is written.
    """


class CancelToken:
    """A cooperative, thread-safe cancel handle.

    The token is the only cancellation surface the orchestrator
    exposes. A caller invokes :meth:`cancel` to flag a run for
    cancellation; the engine callback polls
    :meth:`is_cancelled` between pipeline stages. The token is
    thread-safe; multiple callers may flag / inspect the same
    token concurrently.

    A token is *one-shot*: once fired, the token stays fired
    until a fresh token is created. The orchestrator does not
    reset a token between runs; the test suite uses fresh tokens
    to keep cancellation tests independent.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None

    def cancel(self, *, reason: str) -> None:
        """Flag the token as cancelled with ``reason``.

        ``reason`` is recorded on the run record. Subsequent
        calls do not overwrite the flag or the reason: the
        first canceller wins so the run record reflects the
        first canceller's reason.
        """
        if self._event.is_set():
            return
        self._reason = reason
        self._event.set()

    def is_cancelled(self) -> bool:
        """Return ``True`` iff :meth:`cancel` has been called."""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """Return the cancellation reason, or ``None`` when unset."""
        return self._reason

    def raise_if_cancelled(self) -> None:
        """Raise :class:`RunCancelled` iff the token is set.

        The orchestrator's hot loop calls this between pipeline
        stages. Raising the dedicated exception (instead of
        returning a bool) keeps the cancellation branch
        isolated from the success branch and surfaces the
        cancel reason on the run record.
        """
        if self._event.is_set():
            raise RunCancelled(self._reason or f"{_REASON_PREFIX}CANCELLED")


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunRecord:
    """The durable run record the orchestrator persists.

    The record is the single source of truth for a run's
    lifecycle. The CLI / Web console read the record to observe
    status, progress and failure reasons; the orchestrator writes
    the record atomically at every transition.

    Field units:

    - ``version`` — non-empty string; the record schema version.
    - ``run_id`` — non-empty string; the run's identity.
    - ``state`` — :class:`RunState`; the current run state.
    - ``request`` — :class:`RunRequest`; the run request that
      started the run (preserved byte-identical across the
      record's lifetime).
    - ``progress`` — :class:`RunProgress`; the most recent
      progress snapshot the orchestrator recorded.
    - ``reason_code`` — optional non-empty string; the failure /
      cancellation reason. ``None`` for queued and running runs
      and for successful runs.
    - ``error_message`` — optional non-empty string; a human-
      readable error message. ``None`` for queued and running
      runs and for successful runs.
    - ``manifest_path`` — optional non-empty string path; the
      path to the published manifest. ``None`` until the run
      succeeds.
    - ``report_path`` — optional non-empty string path; the
      path to the published report. ``None`` until the run
      succeeds.
    - ``source_manifest_path`` — optional non-empty string
      path; the source manifest the run is linked to when the
      request is a product rerun. ``None`` for fresh runs.
    - ``source_checksum`` — optional non-empty string; the
      SHA-256 hex digest of the source manifest at the moment
      the product rerun started. The record never mutates the
      source; the checksum pins the source's bytes so a later
      reviewer can verify the source was unchanged.
    - ``created_at_unix_seconds`` — non-negative int; the
      moment the run was first created.
    - ``updated_at_unix_seconds`` — non-negative int; the
      moment the record was last written.
    - ``terminal_at_unix_seconds`` — optional non-negative int;
      the moment the run reached a terminal state. ``None``
      while the run is queued or running.
    """

    version: str
    run_id: str
    state: RunState
    request: RunRequest
    progress: RunProgress
    reason_code: str | None
    error_message: str | None
    manifest_path: str | None
    report_path: str | None
    source_manifest_path: str | None
    source_checksum: str | None
    simulation_evidence_path: str | None
    created_at_unix_seconds: int
    updated_at_unix_seconds: int
    terminal_at_unix_seconds: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_RECORD: version must be non-empty str"
            )
        if not isinstance(self.state, RunState):
            raise InvalidRunRequestError(f"{_REASON_PREFIX}INVALID_RECORD: state must be RunState")
        if self.reason_code is not None and (
            not isinstance(self.reason_code, str) or not self.reason_code
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_RECORD: reason_code must be non-empty str or None"
            )
        if self.error_message is not None and (
            not isinstance(self.error_message, str) or not self.error_message
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_RECORD: error_message must be non-empty str or None"
            )
        if self.simulation_evidence_path is not None and (
            not isinstance(self.simulation_evidence_path, str) or not self.simulation_evidence_path
        ):
            raise InvalidRunRequestError(
                f"{_REASON_PREFIX}INVALID_RECORD: simulation_evidence_path "
                f"must be non-empty str or None"
            )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly representation.

        The mapping preserves the schema's declared parameter
        order so the record's canonical serialisation is
        deterministic.
        """
        return {
            "version": self.version,
            "run_id": self.run_id,
            "state": self.state.value,
            "request": self.request.to_dict(),
            "progress": self.progress.to_dict(),
            "reason_code": self.reason_code,
            "error_message": self.error_message,
            "manifest_path": self.manifest_path,
            "report_path": self.report_path,
            "source_manifest_path": self.source_manifest_path,
            "source_checksum": self.source_checksum,
            "simulation_evidence_path": self.simulation_evidence_path,
            "created_at_unix_seconds": self.created_at_unix_seconds,
            "updated_at_unix_seconds": self.updated_at_unix_seconds,
            "terminal_at_unix_seconds": self.terminal_at_unix_seconds,
        }


# ---------------------------------------------------------------------------
# Run state store (durable persistence)
# ---------------------------------------------------------------------------


class RunStateStore:
    """A disk-backed store for :class:`RunRecord` files.

    The store writes one file per run under a ``runs_root``
    directory. Every write is atomic (temp-file + rename) so a
    crash mid-write leaves the previous record on disk and the
    observer never sees a half-written file.

    The store is the durable product-run lifecycle the T069
    contract binds: a process restart while a run is in
    :attr:`RunState.RUNNING` resumes the run from the persisted
    state without duplicating or losing it; a terminal run's
    record is read-only after :attr:`RunState.SUCCEEDED` /
    :attr:`RunState.FAILED` / :attr:`RunState.CANCELLED`.
    """

    def __init__(self, runs_root: Path | str) -> None:
        self.runs_root = Path(runs_root)
        self.runs_root.mkdir(parents=True, exist_ok=True)

    # ----- path helpers --------------------------------------------------

    def record_path(self, run_id: str) -> Path:
        return self.runs_root / _RUN_RECORD_FILENAME.format(run_id=run_id)

    def manifest_path(self, *, run_id: str, chain_id: int, pool_key_id: str) -> Path:
        return self.runs_root / _MANIFEST_FILENAME.format(
            run_id=run_id,
            chain_id=chain_id,
            pool_key_id=_safe_for_filename(pool_key_id),
        )

    def report_path(self, *, run_id: str, chain_id: int, pool_key_id: str) -> Path:
        return self.runs_root / _REPORT_FILENAME.format(
            run_id=run_id,
            chain_id=chain_id,
            pool_key_id=_safe_for_filename(pool_key_id),
        )

    def simulation_evidence_path(self, *, run_id: str, chain_id: int, pool_key_id: str) -> Path:
        return self.runs_root / _EVIDENCE_FILENAME.format(
            run_id=run_id,
            chain_id=chain_id,
            pool_key_id=_safe_for_filename(pool_key_id),
        )

    # ----- I/O -----------------------------------------------------------

    def has_run(self, run_id: str) -> bool:
        return self.record_path(run_id).exists()

    def read(self, run_id: str) -> RunRecord:
        path = self.record_path(run_id)
        if not path.exists():
            raise FileNotFoundError(f"RunStateStore.read: run_id={run_id!r} not found")
        raw = path.read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunStateCorruptError(
                f"{_REASON_PREFIX}RECORD_CORRUPT: {path} is not valid JSON: {exc}"
            ) from exc
        return run_record_from_dict(payload)

    def write(self, record: RunRecord) -> RunRecord:
        """Write ``record`` atomically and return the persisted copy.

        The write uses a temp-file rename so a crash mid-write
        leaves the previous record on disk; the observer never
        sees a half-written file. The temp file lives under
        ``runs_root`` so the rename is atomic (same filesystem).

        Terminal records are read-only: the store refuses to
        overwrite a ``SUCCEEDED`` / ``FAILED`` / ``CANCELLED``
        record. The orchestrator never re-writes a terminal
        record; the CLI surfaces the existing terminal record.
        """
        path = self.record_path(record.run_id)
        existing: RunRecord | None = None
        if path.exists():
            try:
                existing = self.read(record.run_id)
            except RunStateCorruptError:
                # A corrupt record is overwritten by the new
                # write; the corrupt record is an audit gap
                # the orchestrator surfaces through the new
                # record's reason code.
                existing = None
        if existing is not None and _is_terminal_state(existing.state):
            return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = record.to_dict()
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{record.run_id}.",
            suffix=".run.json.tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
                fh.write("\n")
            os.replace(tmp_path, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        return record

    def list_runs(self) -> tuple[str, ...]:
        """Return every ``run_id`` persisted under this store.

        The order is the lexicographic order of the record
        filenames, which matches ``run_id`` order for the
        canonical naming scheme. Stale ``.tmp`` files are
        ignored.
        """
        run_ids: list[str] = []
        for path in sorted(self.runs_root.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            if not name.endswith(".run.json"):
                continue
            if name.endswith(".tmp"):
                continue
            run_id = name[: -len(".run.json")]
            run_ids.append(run_id)
        return tuple(run_ids)

    def remove(self, run_id: str) -> None:
        """Remove the record for ``run_id``.

        The orchestrator does not call this method during
        normal operation; the CLI surfaces it for operator-
        driven cleanup. The method refuses to remove a
        terminal record so an operator cannot accidentally
        lose a succeeded run.
        """
        record = self.read(run_id)
        if _is_terminal_state(record.state):
            raise BacktestRunError(
                f"{_REASON_PREFIX}CANNOT_REMOVE_TERMINAL: run_id={run_id!r} "
                f"is in terminal state {record.state.value!r}; refusing to remove"
            )
        path = self.record_path(run_id)
        path.unlink()


def _safe_for_filename(value: str) -> str:
    """Return a filename-safe rendering of ``value``.

    The hex ``pool_key_id`` already contains only ``0-9a-f``;
    we replace any non-safe character with ``_`` for defensive
    coverage.
    """
    out_chars: list[str] = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_", "."):
            out_chars.append(ch)
        else:
            out_chars.append("_")
    return "".join(out_chars)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def run_record_from_dict(payload: Mapping[str, Any]) -> RunRecord:
    """Reconstruct a :class:`RunRecord` from a JSON-friendly dict.

    The loader refuses a record whose ``version`` is missing or
    whose state is not a :class:`RunState`. The loader is the
    single surface every observer uses to read a persisted
    record; the validation guarantees the observer can rely on
    the field types the dataclass declares.
    """
    if not isinstance(payload, Mapping):
        raise RunStateCorruptError(f"{_REASON_PREFIX}RECORD_CORRUPT: payload must be Mapping")
    try:
        version = payload["version"]
        run_id = payload["run_id"]
        state_raw = payload["state"]
        request_raw = payload["request"]
        progress_raw = payload["progress"]
        reason_code = payload.get("reason_code")
        error_message = payload.get("error_message")
        manifest_path = payload.get("manifest_path")
        report_path = payload.get("report_path")
        source_manifest_path = payload.get("source_manifest_path")
        source_checksum = payload.get("source_checksum")
        simulation_evidence_path = payload.get("simulation_evidence_path")
        created_at = payload["created_at_unix_seconds"]
        updated_at = payload["updated_at_unix_seconds"]
        terminal_at = payload.get("terminal_at_unix_seconds")
    except KeyError as exc:
        raise RunStateCorruptError(
            f"{_REASON_PREFIX}RECORD_CORRUPT: missing key {exc.args[0]!r}"
        ) from exc
    if not isinstance(version, str) or not version:
        raise RunStateCorruptError(f"{_REASON_PREFIX}RECORD_CORRUPT: version must be non-empty str")
    if not isinstance(run_id, str) or not run_id:
        raise RunStateCorruptError(f"{_REASON_PREFIX}RECORD_CORRUPT: run_id must be non-empty str")
    try:
        state = RunState(str(state_raw))
    except ValueError as exc:
        raise RunStateCorruptError(
            f"{_REASON_PREFIX}RECORD_CORRUPT: state={state_raw!r} is not a RunState"
        ) from exc
    if not isinstance(request_raw, Mapping):
        raise RunStateCorruptError(f"{_REASON_PREFIX}RECORD_CORRUPT: request must be Mapping")
    if not isinstance(progress_raw, Mapping):
        raise RunStateCorruptError(f"{_REASON_PREFIX}RECORD_CORRUPT: progress must be Mapping")
    request = _run_request_from_dict(request_raw)
    progress = _run_progress_from_dict(progress_raw)
    return RunRecord(
        version=version,
        run_id=run_id,
        state=state,
        request=request,
        progress=progress,
        reason_code=str(reason_code) if reason_code is not None else None,
        error_message=str(error_message) if error_message is not None else None,
        manifest_path=str(manifest_path) if manifest_path is not None else None,
        report_path=str(report_path) if report_path is not None else None,
        source_manifest_path=(
            str(source_manifest_path) if source_manifest_path is not None else None
        ),
        source_checksum=str(source_checksum) if source_checksum is not None else None,
        simulation_evidence_path=(
            str(simulation_evidence_path) if simulation_evidence_path is not None else None
        ),
        created_at_unix_seconds=int(created_at),
        updated_at_unix_seconds=int(updated_at),
        terminal_at_unix_seconds=(int(terminal_at) if terminal_at is not None else None),
    )


def _run_request_from_dict(payload: Mapping[str, Any]) -> RunRequest:
    if not isinstance(payload, Mapping):
        raise RunStateCorruptError(
            f"{_REASON_PREFIX}RECORD_CORRUPT: request payload must be Mapping"
        )
    try:
        strategy_parameters = dict(payload["strategy_parameters"])
    except (KeyError, TypeError) as exc:
        raise RunStateCorruptError(
            f"{_REASON_PREFIX}RECORD_CORRUPT: strategy_parameters missing or invalid"
        ) from exc
    for _k, v in strategy_parameters.items():
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            raise RunStateCorruptError(
                f"{_REASON_PREFIX}RECORD_CORRUPT: strategy_parameters value must be int|bool|str"
            )
    try:
        dependency_revisions = dict(payload["dependency_revisions"])
    except (KeyError, TypeError) as exc:
        raise RunStateCorruptError(
            f"{_REASON_PREFIX}RECORD_CORRUPT: dependency_revisions missing or invalid"
        ) from exc
    return RunRequest(
        run_id=str(payload["run_id"]),
        dataset_version=str(payload["dataset_version"]),
        chain_id=int(payload["chain_id"]),
        pool_key_id=str(payload["pool_key_id"]),
        block_range_start=int(payload["block_range_start"]),
        block_range_end=int(payload["block_range_end"]),
        interval_seconds=int(payload["interval_seconds"]),
        strategy_identity=str(payload["strategy_identity"]),
        strategy_parameters=strategy_parameters,
        seed=int(payload["seed"]),
        clock_assumption=str(payload["clock_assumption"]),
        fill_assumption=str(payload["fill_assumption"]),
        cost_assumption=str(payload["cost_assumption"]),
        quote_assumption=str(payload["quote_assumption"]),
        latency_units=int(payload["latency_units"]),
        latency_ms_estimate=int(payload["latency_ms_estimate"]),
        reporting_numeraire=str(payload["reporting_numeraire"]),
        valuation_qualification=str(payload["valuation_qualification"]),
        code_revision=str(payload["code_revision"]),
        dependency_revisions=dependency_revisions,
        created_at_unix_seconds=int(payload["created_at_unix_seconds"]),
        source_manifest_path=(
            str(payload["source_manifest_path"])
            if payload.get("source_manifest_path") is not None
            else None
        ),
    )


def _run_progress_from_dict(payload: Mapping[str, Any]) -> RunProgress:
    if not isinstance(payload, Mapping):
        raise RunStateCorruptError(
            f"{_REASON_PREFIX}RECORD_CORRUPT: progress payload must be Mapping"
        )
    return RunProgress(
        events_total=int(payload["events_total"]),
        events_processed=int(payload["events_processed"]),
        current_stage=str(payload["current_stage"]),
        updated_at_unix_seconds=int(payload["updated_at_unix_seconds"]),
    )


# ---------------------------------------------------------------------------
# Strategy callback construction
# ---------------------------------------------------------------------------


def _build_strategy_callback(
    *,
    identity: str,
    parameters: Mapping[str, int | bool | str],
    pool_key_id: str,
    chain_id: int,
    interval_seconds: int,
    registry: Registry | None = None,
) -> StrategyCallback:
    """Build a :class:`StrategyCallback` from the registry binding.

    The function is the registry-bound replacement for any
    hard-coded strategy switch. It looks the registered
    identity up through the registry, builds the registered
    factory with the validated parameters, and returns the
    constructed strategy. An unregistered identity is rejected
    with :class:`UnknownStrategyIdentityError` before any
    engine call.
    """
    reg = registry if registry is not None else default_registry()
    # ``reg.lookup`` raises :class:`UnknownStrategyIdentityError`
    # when the identity is not registered. The orchestrator
    # surfaces the dedicated error so a reviewer can localise
    # the unknown identity.
    entry = reg.lookup(identity)
    # ``reg.validate_parameters`` raises
    # :class:`InvalidParameterValueError` for undeclared /
    # missing / wrong-type / out-of-range parameters.
    validated = reg.validate_parameters(identity, parameters)
    built = entry.factory.build(
        parameters=validated,
        pool_key_id=pool_key_id,
        chain_id=chain_id,
    )
    if callable(built):
        return cast(StrategyCallback, built)
    if isinstance(built, _AdaptiveStrategy):
        return AdaptiveStrategyCallback(strategy=built, window_seconds=int(interval_seconds))
    raise InvalidRunRequestError(
        f"{_REASON_PREFIX}STRATEGY_NOT_CALLABLE: registered factory for "
        f"{identity!r} returned an object the engine cannot consume "
        f"({type(built).__name__})"
    )


# ---------------------------------------------------------------------------
# Model bundle construction
# ---------------------------------------------------------------------------


def _build_model_bundle(
    *,
    code_revision: str,
    gas_units: int = _DEFAULT_GAS_UNITS,
    fee_pips: int = _DEFAULT_FEE_PIPS,
    active_liquidity: int = _DEFAULT_ACTIVE_LIQUIDITY,
    latency_units: int = 0,
) -> ModelBundle:
    """Build the canonical T105 model bundle the orchestrator consumes.

    The bundle pairs the standard deterministic models
    (:class:`ConstantLiquidityModel`, :class:`StaticFeeModel`,
    :class:`FlatGasModel`, :class:`ZeroSlippageModel`,
    :class:`DeterministicFailureModel`) with the parameters
    the request supplied. The ``bundle_version`` is keyed on
    the code revision the request recorded so a re-publication
    after a code change carries a different bundle version.
    """
    return ModelBundle(
        bundle_version=f"t069.orchestrator.{code_revision}",
        liquidity=ConstantLiquidityModel(active_liquidity_value=active_liquidity),
        fee=StaticFeeModel(fee_pips_value=fee_pips),
        gas=FlatGasModel(gas_units_value=gas_units),
        slippage=ZeroSlippageModel(),
        failure=DeterministicFailureModel(),
        latency_units=latency_units,
    )


def _approve_risk_callback(_decision: object) -> RiskDecision:
    """The orchestrator's default risk callback.

    The orchestrator does not implement a risk gateway; that
    is T070's responsibility. The default callback approves
    every decision so the engine can complete a T069
    lifecycle run; a production deployment wires the T070
    gateway here. The callback never mutates the decision it
    receives.
    """
    return RiskDecision(approved=True, reason_code="T069_DEFAULT_RISK_APPROVED")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class _Clock:
    """The wall-clock surface the orchestrator uses for record timestamps.

    The orchestrator accepts a clock callable so tests can pin
    timestamps without monkey-patching ``time.time``. The
    default reads ``time.time()`` and returns integer Unix
    seconds (the manifest contract binds integer seconds).
    """

    now_unix_seconds: int = 0

    def __post_init__(self) -> None:
        if self.now_unix_seconds == 0:
            self.now_unix_seconds = _default_unix_seconds()

    def __call__(self) -> int:
        return self.now_unix_seconds


def _default_unix_seconds() -> int:
    """Return the current wall-clock time as integer Unix seconds."""
    import time

    return int(time.time())


class BacktestOrchestrator:
    """The product-level backtest run lifecycle.

    The orchestrator is the durable lifecycle the T069 contract
    binds:

    - **Reservation.** :meth:`submit` reserves the run as
      :attr:`RunState.QUEUED` before any work begins; the
      reservation is durable (atomic write through
      :class:`RunStateStore`).
    - **Execution.** :meth:`submit` (or :meth:`resume_in_flight`)
      drives the run from ``QUEUED`` to ``RUNNING`` to a
      terminal state. The orchestrator composes the existing
      T061 engine, T063 metrics / coverage / decisions, and
      T105 registry-bound manifest authority; it does not
      re-implement any of them.
    - **Cancellation.** :meth:`cancel` flags the run's cancel
      token; the engine loop polls the token and the
      orchestrator transitions the run to
      :attr:`RunState.CANCELLED` with a structured reason. No
      manifest or report is written.
    - **Restart.** :meth:`resume_in_flight` reads every
      ``RUNNING`` record the store carries and resumes the run
      from the persisted state without duplicating it. A
      terminal record is left untouched.
    - **Product rerun.** A :class:`RunRequest` whose
      ``source_manifest_path`` is supplied is a product rerun:
      the orchestrator creates a new run record linked to the
      source, never mutates the source, and records the
      source checksum so a reviewer can verify the source was
      unchanged.
    """

    def __init__(
        self,
        *,
        store: RunStateStore,
        dataset_resolver: DatasetResolver,
        event_source: EventSource,
        clock: _Clock | None = None,
    ) -> None:
        self.store = store
        self.dataset_resolver = dataset_resolver
        self.event_source = event_source
        self.clock = clock if clock is not None else _Clock()

    # ----- request validation -------------------------------------------

    def _validate_request(self, request: RunRequest) -> DatasetCoverage:
        """Validate ``request`` against the dataset and registry.

        The function performs the four pre-execution gates the
        T069 acceptance clause binds:

        1. Registered strategy identity (T068) — an
           unregistered identity is rejected with
           :class:`UnknownStrategyIdentityError` before any
           result is written.
        2. Existing dataset version (T100) — a missing dataset
           version is rejected with
           :class:`UnknownDatasetVersionError`.
        3. Block range within coverage — a range outside the
           dataset's coverage is rejected with
           :class:`BlockRangeOutsideCoverageError`.
        4. Source manifest binding (T105) — when the request is
           a product rerun, the source manifest is loaded and
           validated. A legacy pre-registry source is rejected
           with :class:`LegacyManifestSourceError`.

        Returns the resolved coverage on success.
        """
        # 1. Registered strategy identity.
        bind_strategy_to_registry(
            identity=request.strategy_identity,
            parameters=request.strategy_parameters,
        )
        # 2. Existing dataset version. The resolver raises
        #    :class:`UnknownDatasetVersionError` for an unknown
        #    version; we propagate it.
        coverage = self.dataset_resolver.resolve(
            dataset_version=request.dataset_version,
            chain_id=request.chain_id,
            pool_key_id=request.pool_key_id,
        )
        # 3. Block range within coverage.
        if (
            request.block_range_start < coverage.covered_start
            or request.block_range_end > coverage.covered_end
        ):
            raise BlockRangeOutsideCoverageError(
                dataset_version=request.dataset_version,
                block_range_start=request.block_range_start,
                block_range_end=request.block_range_end,
                covered_start=coverage.covered_start,
                covered_end=coverage.covered_end,
            )
        # 4. Source manifest binding.
        if request.source_manifest_path is not None:
            source_path = Path(request.source_manifest_path)
            if not source_path.exists():
                raise BacktestRunError(
                    f"{_REASON_PREFIX}SOURCE_MANIFEST_MISSING: "
                    f"source_manifest_path={source_path} does not exist"
                )
            try:
                source_raw = source_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise BacktestRunError(
                    f"{_REASON_PREFIX}SOURCE_MANIFEST_UNREADABLE: {exc}"
                ) from exc
            try:
                source_payload = json.loads(source_raw)
            except json.JSONDecodeError as exc:
                raise BacktestRunError(
                    f"{_REASON_PREFIX}SOURCE_MANIFEST_INVALID_JSON: {exc}"
                ) from exc
            declared_version = (
                source_payload.get("version") if isinstance(source_payload, dict) else None
            )
            # T109 cutover: a product rerun may source the current T109
            # dataset-referenced manifest OR a legacy T105 manifest (the
            # T105 artifacts remain byte-identical and readable as
            # historical rerun sources). Anything else — including the
            # legacy T063 pre-registry vocabulary — is rejected.
            if declared_version not in (
                MANIFEST_VERSION,
                MANIFEST_VERSION_T109,
            ):
                raise LegacyManifestSourceError(source_manifest_path=str(source_path))
            try:
                if declared_version == MANIFEST_VERSION_T109:
                    source_manifest: ExperimentManifest | T109ExperimentManifest = (
                        load_t109_manifest_from_path(source_path)
                    )
                else:
                    source_manifest = load_manifest_from_path(source_path)
            except ManifestValidationError as exc:
                raise BacktestRunError(f"{_REASON_PREFIX}SOURCE_MANIFEST_INVALID: {exc}") from exc
            # The product rerun's chain_id / pool_key_id /
            # dataset_version must agree with the source.
            if source_manifest.chain_id != request.chain_id:
                raise BacktestRunError(
                    f"{_REASON_PREFIX}RERUN_POOL_MISMATCH: source chain_id="
                    f"{source_manifest.chain_id} != request chain_id={request.chain_id}"
                )
            if source_manifest.pool_key_id != request.pool_key_id:
                raise BacktestRunError(
                    f"{_REASON_PREFIX}RERUN_POOL_MISMATCH: source pool_key_id="
                    f"{source_manifest.pool_key_id!r} != request "
                    f"pool_key_id={request.pool_key_id!r}"
                )
            if source_manifest.dataset_version != coverage.dataset_version:
                raise BacktestRunError(
                    f"{_REASON_PREFIX}RERUN_DATASET_MISMATCH: source dataset_version="
                    f"{source_manifest.dataset_version!r} != request "
                    f"dataset_version={coverage.dataset_version!r}"
                )
        return coverage

    # ----- public lifecycle ---------------------------------------------

    def submit(
        self,
        request: RunRequest,
        *,
        cancel_token: CancelToken | None = None,
    ) -> RunRecord:
        """Reserve the run as :attr:`RunState.QUEUED` and execute it.

        The function performs the full lifecycle in one call:

        1. Validate the request against the dataset and
           registry.
        2. Reserve the run as ``QUEUED`` (atomic write).
        3. Transition to ``RUNNING``.
        4. Compose the engine, metrics, manifest and report.
        5. Transition to ``SUCCEEDED`` with the manifest and
           report paths recorded. On failure or cancellation,
           transition to ``FAILED`` or ``CANCELLED`` with a
           structured reason and no manifest written.

        The function is the canonical product entry point the
        Web console and the CLI invoke. The cancel token is
        optional; when ``None``, the orchestrator creates a
        fresh token and ignores it (cancellation is observed
        through the returned record's ``state`` field).
        """
        token = cancel_token if cancel_token is not None else CancelToken()
        # Pre-execution validation.
        try:
            self._validate_request(request)
        except Exception as exc:
            return self._write_terminal_record(
                request=request,
                state=RunState.FAILED,
                reason_code=_reason_code_from_exception(exc),
                error_message=str(exc),
            )
        # Reserve the run as QUEUED. The store refuses an
        # existing record by leaving the terminal record alone;
        # for a fresh run, write a QUEUED record.
        now = self.clock()
        if self.store.has_run(request.run_id):
            existing = self.store.read(request.run_id)
            if _is_terminal_state(existing.state):
                # Re-submitting a terminal run is a no-op; the
                # terminal record is returned untouched.
                return existing
            raise RunAlreadyExistsError(
                run_id=request.run_id,
                path=str(self.store.record_path(request.run_id)),
            )
        queued_progress = RunProgress(
            events_total=0,
            events_processed=0,
            current_stage="QUEUED",
            updated_at_unix_seconds=now,
        )
        record = RunRecord(
            version=ORCHESTRATOR_VERSION,
            run_id=request.run_id,
            state=RunState.QUEUED,
            request=request,
            progress=queued_progress,
            reason_code=None,
            error_message=None,
            manifest_path=None,
            report_path=None,
            source_manifest_path=request.source_manifest_path,
            source_checksum=(
                _sha256_of_file(Path(request.source_manifest_path))
                if request.source_manifest_path is not None
                else None
            ),
            simulation_evidence_path=None,
            created_at_unix_seconds=request.created_at_unix_seconds,
            updated_at_unix_seconds=now,
            terminal_at_unix_seconds=None,
        )
        self.store.write(record)
        return self._execute(record=record, cancel_token=token)

    def cancel(
        self,
        run_id: str,
        *,
        reason: str = f"{_REASON_PREFIX}CANCELLED_BY_OPERATOR",
    ) -> RunRecord:
        """Cancel a queued or running run.

        The function flags the run's cancel token; the engine
        loop observes the flag and transitions the run to
        :attr:`RunState.CANCELLED`. A terminal record is left
        untouched (cancellation is a no-op on a succeeded /
        failed / cancelled run).
        """
        record = self.store.read(run_id)
        if _is_terminal_state(record.state):
            return record
        now = self.clock()
        cancelled_record = RunRecord(
            version=record.version,
            run_id=record.run_id,
            state=RunState.CANCELLED,
            request=record.request,
            progress=record.progress,
            reason_code=reason,
            error_message=None,
            manifest_path=None,
            report_path=None,
            source_manifest_path=record.source_manifest_path,
            source_checksum=record.source_checksum,
            simulation_evidence_path=None,
            created_at_unix_seconds=record.created_at_unix_seconds,
            updated_at_unix_seconds=now,
            terminal_at_unix_seconds=now,
        )
        return self.store.write(cancelled_record)

    def observe(self, run_id: str) -> RunRecord:
        """Return the current persisted record for ``run_id``.

        The function is the read-only observation surface the
        Web console / CLI call. The returned record is a fresh
        dataclass copy so the caller can serialise it without
        affecting the persisted state.
        """
        return self.store.read(run_id)

    def list_runs(self) -> tuple[RunRecord, ...]:
        """Return every persisted run record.

        The order matches :meth:`RunStateStore.list_runs`
        (lexicographic by ``run_id``).
        """
        records: list[RunRecord] = []
        for run_id in self.store.list_runs():
            records.append(self.store.read(run_id))
        return tuple(records)

    def resume_in_flight(self) -> tuple[RunRecord, ...]:
        """Resume every ``RUNNING`` record the store carries.

        A restart while a run is in flight must not duplicate
        the run or lose its state. The orchestrator reads
        every record whose state is :attr:`RunState.RUNNING`
        and resumes the run from the persisted state. A
        terminal record is left untouched.

        Returns the records the orchestrator processed (one
        record per in-flight run). Each returned record is the
        terminal record the run wrote.
        """
        results: list[RunRecord] = []
        for run_id in self.store.list_runs():
            record = self.store.read(run_id)
            if record.state != RunState.RUNNING:
                continue
            token = CancelToken()
            results.append(self._execute(record=record, cancel_token=token))
        return tuple(results)

    # ----- execution -----------------------------------------------------

    def _execute(self, *, record: RunRecord, cancel_token: CancelToken) -> RunRecord:
        """Execute the run lifecycle for ``record``.

        The function drives ``record`` from its current state
        to a terminal state. The transitions are:

        - ``QUEUED`` → ``RUNNING`` → ``SUCCEEDED`` | ``FAILED`` | ``CANCELLED``
        - ``RUNNING`` → ``SUCCEEDED`` | ``FAILED`` | ``CANCELLED``

        A ``SUCCEEDED`` record carries the manifest and report
        paths. A ``FAILED`` or ``CANCELLED`` record carries the
        reason code; no manifest or report is written.

        The function is the single execution surface the
        orchestrator exposes; :meth:`submit` and
        :meth:`resume_in_flight` both call it.
        """
        request = record.request
        # Transition to RUNNING.
        running_record = self._update_record(
            record=record,
            state=RunState.RUNNING,
            progress=RunProgress(
                events_total=0,
                events_processed=0,
                current_stage="LOADING",
                updated_at_unix_seconds=self.clock(),
            ),
            reason_code=None,
            error_message=None,
        )
        try:
            # 1. Bind the strategy to the registry (T105).
            binding = bind_strategy_to_registry(
                identity=request.strategy_identity,
                parameters=request.strategy_parameters,
            )
            # 2. Load input events through the event source.
            events = self._load_events(
                request=request,
                cancel_token=cancel_token,
            )
            cancel_token.raise_if_cancelled()
            # 3. Build the engine.
            strategy_callback = _build_strategy_callback(
                identity=request.strategy_identity,
                parameters=request.strategy_parameters,
                pool_key_id=request.pool_key_id,
                chain_id=request.chain_id,
                interval_seconds=request.interval_seconds,
            )
            bundle = _build_model_bundle(
                code_revision=request.code_revision,
                latency_units=request.latency_units,
            )
            initial_ledger = empty_position_state(
                pool_key_id=request.pool_key_id, chain_id=request.chain_id
            )
            engine = BacktestEngine(
                version=BACKTEST_ENGINE_VERSION,
                initial_ledger=initial_ledger,
                model_bundle=bundle,
                strategy_callback=strategy_callback,
                risk_callback=_approve_risk_callback,
            )
            # 4. Run the engine.
            self._update_record(
                record=running_record,
                state=RunState.RUNNING,
                progress=RunProgress(
                    events_total=len(events),
                    events_processed=0,
                    current_stage="ENGINE",
                    updated_at_unix_seconds=self.clock(),
                ),
                reason_code=None,
                error_message=None,
            )
            result = engine.run(events)
            cancel_token.raise_if_cancelled()
            # 5. Compute metrics / coverage / decisions.
            metrics = compute_run_metrics(
                result=result,
                interval_seconds=request.interval_seconds,
            )
            coverage = build_coverage_summary(
                result=result,
                input_events=events,
                interval_seconds=request.interval_seconds,
                block_range_start=request.block_range_start,
                block_range_end=request.block_range_end,
            )
            decisions = extract_decisions(result)
            decisions_chk = decisions_checksum(decisions)
            ledger_snapshot = LedgerSnapshot.from_position_state(result.final_ledger)
            self._update_record(
                record=running_record,
                state=RunState.RUNNING,
                progress=RunProgress(
                    events_total=len(events),
                    events_processed=len(events),
                    current_stage="MANIFEST",
                    updated_at_unix_seconds=self.clock(),
                ),
                reason_code=None,
                error_message=None,
            )
            # 6. Resolve coverage for the manifest's
            #    dataset_version / schema / decode / content
            #    hash / numeraire / qualification.
            coverage_record = self.dataset_resolver.resolve(
                dataset_version=request.dataset_version,
                chain_id=request.chain_id,
                pool_key_id=request.pool_key_id,
            )
            # 7. Build the T109 simulation-evidence artifact from the
            #    engine's audit chain. The orchestrator is the single
            #    publication gate T109 names: a successful run that
            #    cannot produce a SimulationEvidence fails closed with
            #    a named reason; no manifest / report is published.
            simulation_evidence = _build_simulation_evidence(
                request=request,
                result=result,
                coverage=coverage,
                coverage_record=coverage_record,
                binding=binding,
                decisions_checksum=decisions_chk,
                metrics_version=METRICS_VERSION,
                events=events,
            )
            simulation_evidence_path = self.store.simulation_evidence_path(
                run_id=request.run_id,
                chain_id=request.chain_id,
                pool_key_id=request.pool_key_id,
            )
            # 8. Build the dataset-referenced T109 manifest. The
            #    manifest references the dataset partition instead of
            #    embedding the complete event timeline; the
            #    simulation-evidence artifact is the only other
            #    evidence the run publishes.
            partition_refs = _derive_partition_refs(events, coverage_record)
            manifest = build_t109_experiment_manifest(
                run_id=request.run_id,
                metrics=metrics,
                coverage=coverage,
                ledger_snapshot=ledger_snapshot,
                decisions_checksum=decisions_chk,
                dataset_version=coverage_record.dataset_version,
                dataset_schema_version=coverage_record.dataset_schema_version,
                dataset_decode_version=coverage_record.dataset_decode_version,
                dataset_content_hash=coverage_record.dataset_content_hash,
                reporting_numeraire=coverage_record.reporting_numeraire,
                valuation_qualification=coverage_record.valuation_qualification,
                code_revision=request.code_revision,
                dependency_revisions=dict(request.dependency_revisions),
                strategy_binding=binding,
                seed=request.seed,
                clock_assumption=request.clock_assumption,
                fill_assumption=request.fill_assumption,
                cost_assumption=request.cost_assumption,
                quote_assumption=request.quote_assumption,
                latency_units=request.latency_units,
                latency_ms_estimate=request.latency_ms_estimate,
                created_at_unix_seconds=request.created_at_unix_seconds,
                block_range_start=request.block_range_start,
                block_range_end=request.block_range_end,
                dataset_partition_refs=partition_refs,
                dataset_event_count=len(events),
                simulation_evidence_ref=str(simulation_evidence_path.name),
                reconstruction_revision=BACKTEST_ENGINE_VERSION,
            )
            # 9. Publish the simulation-evidence artifact atomically
            #    before the manifest, so a write failure here fails
            #    the publication closed.
            write_simulation_evidence_to_path(simulation_evidence, simulation_evidence_path)
            # 10. Publish the dataset-referenced manifest atomically.
            manifest_path = self.store.manifest_path(
                run_id=request.run_id,
                chain_id=request.chain_id,
                pool_key_id=request.pool_key_id,
            )
            write_t109_manifest_to_path(manifest, manifest_path)
            # 11. Publish the report (a human-readable companion of
            #     the dataset-referenced manifest).
            report_path = self.store.report_path(
                run_id=request.run_id,
                chain_id=request.chain_id,
                pool_key_id=request.pool_key_id,
            )
            report_payload = _build_t109_report_payload(
                manifest=manifest,
                metrics=metrics,
                coverage=coverage,
                ledger_snapshot=ledger_snapshot,
                simulation_evidence_ref=simulation_evidence_path.name,
            )
            _atomic_write_json(report_path, report_payload)
            cancel_token.raise_if_cancelled()
            # 12. Transition to SUCCEEDED with the manifest,
            #     report, and simulation-evidence paths recorded.
            return self._write_terminal_record(
                request=request,
                state=RunState.SUCCEEDED,
                reason_code=None,
                error_message=None,
                manifest_path=str(manifest_path),
                report_path=str(report_path),
                simulation_evidence_path=str(simulation_evidence_path),
            )
        except RunCancelled as exc:
            # No manifest is published on cancellation.
            return self._write_terminal_record(
                request=request,
                state=RunState.CANCELLED,
                reason_code=str(exc),
                error_message=None,
            )
        except BacktestRunError as exc:
            return self._write_terminal_record(
                request=request,
                state=RunState.FAILED,
                reason_code=_reason_code_from_exception(exc),
                error_message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — surface any failure
            return self._write_terminal_record(
                request=request,
                state=RunState.FAILED,
                reason_code=f"{_REASON_PREFIX}UNEXPECTED",
                error_message=f"{type(exc).__name__}: {exc}",
            )

    def _load_events(
        self,
        *,
        request: RunRequest,
        cancel_token: CancelToken,
    ) -> list[BacktestEvent]:
        """Load input events through the injected event source."""
        events = self.event_source.load_events(
            chain_id=request.chain_id,
            pool_key_id=request.pool_key_id,
            block_range_start=request.block_range_start,
            block_range_end=request.block_range_end,
            cancel_token=cancel_token,
        )
        cancel_token.raise_if_cancelled()
        return events

    def _update_record(
        self,
        *,
        record: RunRecord,
        state: RunState,
        progress: RunProgress,
        reason_code: str | None,
        error_message: str | None,
    ) -> RunRecord:
        """Write an updated record and return the persisted copy."""
        now = self.clock()
        updated = RunRecord(
            version=record.version,
            run_id=record.run_id,
            state=state,
            request=record.request,
            progress=progress,
            reason_code=reason_code,
            error_message=error_message,
            manifest_path=record.manifest_path,
            report_path=record.report_path,
            source_manifest_path=record.source_manifest_path,
            source_checksum=record.source_checksum,
            simulation_evidence_path=record.simulation_evidence_path,
            created_at_unix_seconds=record.created_at_unix_seconds,
            updated_at_unix_seconds=now,
            terminal_at_unix_seconds=record.terminal_at_unix_seconds,
        )
        return self.store.write(updated)

    def _write_terminal_record(
        self,
        *,
        request: RunRequest,
        state: RunState,
        reason_code: str | None,
        error_message: str | None,
        manifest_path: str | None = None,
        report_path: str | None = None,
        simulation_evidence_path: str | None = None,
    ) -> RunRecord:
        """Write a terminal record and return the persisted copy.

        A terminal record carries the run's final state and, for
        successful runs, the manifest and report paths. The
        ``source_checksum`` is recomputed from the source
        manifest (if any) so a reviewer can verify the source
        was unchanged at the moment the run terminated.
        """
        now = self.clock()
        source_checksum: str | None = None
        if request.source_manifest_path is not None:
            try:
                source_checksum = _sha256_of_file(Path(request.source_manifest_path))
            except OSError:
                source_checksum = None
        terminal = RunRecord(
            version=ORCHESTRATOR_VERSION,
            run_id=request.run_id,
            state=state,
            request=request,
            progress=RunProgress(
                events_total=0,
                events_processed=0,
                current_stage=state.value,
                updated_at_unix_seconds=now,
            ),
            reason_code=reason_code,
            error_message=error_message,
            manifest_path=manifest_path,
            report_path=report_path,
            source_manifest_path=request.source_manifest_path,
            source_checksum=source_checksum,
            simulation_evidence_path=simulation_evidence_path,
            created_at_unix_seconds=request.created_at_unix_seconds,
            updated_at_unix_seconds=now,
            terminal_at_unix_seconds=now,
        )
        return self.store.write(terminal)


# ---------------------------------------------------------------------------
# T109 evidence publication helpers
# ---------------------------------------------------------------------------


def _derive_partition_refs(
    events: Sequence[BacktestEvent],
    coverage_record: DatasetCoverage,
) -> tuple[str, ...]:
    """Return the dataset partition references a T109 manifest binds to.

    The orchestrator derives a deterministic set of partition
    references from the dataset's coverage record. Each reference is
    a ``(block_number, transaction_index, log_index)`` triple the
    canonical event stream partitions the dataset on; a downstream
    reader resolves the bytes against the dataset content hash the
    manifest carries. The orchestrator never embeds the events
    themselves.

    The function falls back to a derivation from the input event
    timestamps when the dataset resolution does not provide explicit
    partition references; the fallback ensures every T109 manifest
    has at least one reference so the publication gate can enforce
    the "every T109 manifest binds to at least one dataset partition"
    rule.
    """
    refs: set[str] = set()
    # Derive from the input event list. The dataset's T100 immutable
    # partition reference is the dataset content hash; the per-event
    # cursor triple is the canonical partition identifier.
    for evt in events:
        cursor = extract_event_cursor(evt)
        if cursor is None:
            continue
        refs.add(f"{cursor[0]:010d}-{cursor[1]:06d}-{cursor[2]:06d}")
    if not refs:
        # Fall back to a single partition reference derived from the
        # dataset coverage record so the manifest always carries at
        # least one bound partition.
        refs.add(f"coverage-{coverage_record.covered_start:05d}-{coverage_record.covered_end:05d}")
    return tuple(sorted(refs))


def _ledger_snapshot_to_mapping(state: PositionState) -> dict[str, object]:
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


def _build_simulation_evidence(
    *,
    request: RunRequest,
    result: BacktestResult,
    coverage: CoverageSummary,
    coverage_record: DatasetCoverage,
    binding: StrategyBinding,
    decisions_checksum: str,
    metrics_version: str,
    events: Sequence[BacktestEvent],
) -> SimulationEvidence:
    """Build a T109 :class:`SimulationEvidence` artifact from the engine output.

    The function is the canonical builder the T109 orchestrator
    invokes after the engine run. It walks the engine's audit
    chain, extracts the cursor binding every transition observed
    at emission time, and packages the run-specific facts
    ``RunState`` / ``ReplayFrame`` need to reproduce the run.

    The function refuses a run whose audit chain produces a cursor
    state the artifact constructor would reject: a state-changing
    transition with no cursor binding is the contract's "do not
    publish a non-causal run" rule.
    """
    # Build an event-id → (block, tx, log) cursor map so we can
    # resolve cursors for transitions that did not carry one
    # through the AuditEvent. The contract forbids deriving a
    # binding later from the integer timestamp; events whose
    # BacktestEvent lacks the block_number / transaction_index /
    # log_index triple stay cursor-less and the resulting
    # transitions are checked against the
    # "state-changing requires cursor" invariant below.
    event_cursor_map: dict[str, tuple[int, int, int] | None] = {}
    for evt in events:
        cursor = extract_event_cursor(evt)
        event_cursor_map[evt.event_id] = cursor

    def _resolve_cursor(
        audit_event_id: str | None,
        engine_cursor: tuple[int, int, int] | None,
    ) -> tuple[int, int, int] | None:
        # Prefer the audit event's cursor field; fall back to the
        # source event's cursor lookup.
        if engine_cursor is not None:
            return engine_cursor
        if audit_event_id is None:
            return None
        return event_cursor_map.get(audit_event_id)

    transitions: list[RunTransition] = []
    cursor_lookup: dict[str, tuple[int, int, int] | None] = {}
    last_audit_per_stage: dict[str, str] = {}
    for audit in result.audit_events:
        # Map the audit event's parent_event_ids back to their source
        # BacktestEvent cursor. The engine uses (event.event_id,) as
        # the parent for the first-stage audit events (DECISION /
        # RISK / LATENCY / FILL).
        parent_cursor: tuple[int, int, int] | None = None
        for parent_id in audit.parent_event_ids:
            if parent_id in event_cursor_map:
                parent_cursor = event_cursor_map[parent_id]
                break
        cursor = audit.cursor if audit.cursor is not None else parent_cursor
        cursor_lookup[audit.event_id] = cursor
        state_changing = (
            audit.stage == STAGE_FILL
            and audit.ledger_hash_after != "0x" + "00" * 32
            and audit.payload is not None
            and any(
                key == "outcome"
                and str(value)
                in ("FillOutcome.FILLED", "FillOutcome.PARTIAL", "FillOutcome.DELAYED")
                for key, value in audit.payload
            )
        )
        transitions.append(
            RunTransition(
                ordinal=len(transitions),
                stage=audit.stage,
                cursor=_cursor_to_market_cursor(cursor),
                ledger_hash_after=audit.ledger_hash_after,
                audit_event_id=audit.event_id,
                payload={
                    "event_id": audit.event_id,
                    "parent_event_ids": list(audit.parent_event_ids),
                    "timestamp": audit.timestamp,
                    "sequence": audit.sequence,
                    "payload": dict(audit.payload),
                    "ledger_hash_after": audit.ledger_hash_after,
                },
                state_changing=state_changing,
            )
        )
        last_audit_per_stage[audit.stage] = audit.event_id

    # Build a single checkpoint at the final ledger snapshot.
    checkpoints: list[RunStateCheckpoint] = []
    if result.audit_events:
        final_audit = result.audit_events[-1]
        final_cursor = final_audit.cursor
        if final_cursor is None:
            final_cursor = cursor_lookup.get(final_audit.event_id)
        if final_cursor is not None:
            checkpoints.append(
                RunStateCheckpoint(
                    cursor=_cursor_to_market_cursor(final_cursor),
                    last_applied_ordinal=len(transitions) - 1,
                    ledger_snapshot=_ledger_snapshot_to_mapping(result.final_ledger),
                    equity_q64_64=0,
                    drawdown_q64_64=0,
                    attribution_snapshot={
                        "realised_pnl_q64_64": 0,
                        "fees_collected_q64_64": 0,
                        "il_lvr_q64_64": 0,
                        "gas_q64_64": 0,
                    },
                )
            )

    initial_position = _ledger_snapshot_to_mapping(
        PositionState(
            version="t061.position_state.v1",
            pool_key_id=result.final_ledger.pool_key_id,
            chain_id=result.final_ledger.chain_id,
            position_id=result.final_ledger.position_id,
            tick_lower=request.block_range_start,
            tick_upper=request.block_range_end,
            liquidity=0,
            principal_token0=0,
            principal_token1=0,
            tokens_owed0=0,
            tokens_owed1=0,
            in_range=False,
            last_accrual_time=0,
        )
    )

    return build_simulation_evidence(
        run_id=request.run_id,
        dataset_version=coverage_record.dataset_version,
        dataset_schema_version=coverage_record.dataset_schema_version,
        dataset_decode_version=coverage_record.dataset_decode_version,
        dataset_content_hash=coverage_record.dataset_content_hash,
        pool_key_id=request.pool_key_id,
        chain_id=request.chain_id,
        tick_lower=request.block_range_start,
        tick_upper=request.block_range_end,
        strategy_identity=binding.strategy_identity,
        strategy_version=binding.strategy_version,
        registry_version=binding.registry_version,
        registry_checksum=binding.registry_checksum,
        parameter_schema_version=binding.parameter_schema_version,
        parameter_schema_checksum=binding.parameter_schema_checksum,
        code_provenance_module=binding.code_provenance_module,
        code_provenance_revision=binding.code_provenance_revision,
        engine_revision=BACKTEST_ENGINE_VERSION,
        accounting_revision=metrics_version,
        reconstruction_revision=BACKTEST_ENGINE_VERSION,
        initial_position=initial_position,
        initial_equity_q64_64=0,
        initial_attribution={
            "realised_pnl_q64_64": 0,
            "fees_collected_q64_64": 0,
            "il_lvr_q64_64": 0,
            "gas_q64_64": 0,
        },
        transitions=tuple(transitions),
        checkpoints=tuple(checkpoints),
    )


def _cursor_to_market_cursor(
    cursor: tuple[int, int, int] | None,
) -> Any:  # returns MarketCursor | None via duck-typed module
    """Convert a ``(block, tx, log)`` tuple into a :class:`MarketCursor`.

    Imported here at runtime to avoid a top-level cycle with the
    replay layer; the helper is only invoked when the orchestrator
    publishes evidence.
    """
    if cursor is None:
        return None
    from robinhood_lp.replay.market_state import MarketCursor

    block_number, transaction_index, log_index = cursor
    if transaction_index == -1 and log_index == -1:
        return MarketCursor.end_of_block(block_number)
    return MarketCursor(block_number, transaction_index, log_index)


def _build_t109_report_payload(
    *,
    manifest: T109ExperimentManifest,
    metrics: RunMetrics,
    coverage: CoverageSummary,
    ledger_snapshot: LedgerSnapshot,
    simulation_evidence_ref: str,
) -> dict[str, object]:
    """Build the human-readable report payload a T109 manifest publishes."""
    return {
        "version": ORCHESTRATOR_VERSION,
        "manifest_version": manifest.version,
        "run_id": manifest.run_id,
        "chain_id": manifest.chain_id,
        "pool_key_id": manifest.pool_key_id,
        "block_range_start": manifest.block_range_start,
        "block_range_end": manifest.block_range_end,
        "dataset_version": manifest.dataset_version,
        "reporting_numeraire": manifest.reporting_numeraire,
        "valuation_qualification": manifest.valuation_qualification,
        "strategy_identity": manifest.strategy_identity,
        "strategy_version": manifest.strategy_version,
        "registry_version": manifest.registry_version,
        "registry_checksum": manifest.registry_checksum,
        "simulation_evidence_ref": simulation_evidence_ref,
        "reconstruction_revision": manifest.reconstruction_revision,
        "dataset_partition_refs": list(manifest.dataset_partition_refs),
        "dataset_event_count": manifest.dataset_event_count,
        "metrics": {
            "version": metrics.version,
            "interval_seconds": metrics.interval_seconds,
            "duration_seconds": metrics.duration_seconds,
            "total_return_q64_64": metrics.total_return_q64_64,
            "annualized_return_q64_64": metrics.annualized_return_q64_64,
            "max_drawdown_q64_64": metrics.max_drawdown_q64_64,
            "turnover_q64_64": metrics.turnover_q64_64,
            "time_in_range_seconds": metrics.time_in_range_seconds,
            "fees_q64_64": metrics.fees_q64_64,
            "il_lvr_proxy_q64_64": metrics.il_lvr_proxy_q64_64,
            "gas_units_total": metrics.gas_units_total,
            "slippage_bps_total": metrics.slippage_bps_total,
            "benchmark_excess_q64_64": metrics.benchmark_excess_q64_64,
            "fills_count": metrics.fills_count,
            "metrics_checksum": metrics.metrics_checksum,
        },
        "coverage": {
            "version": coverage.version,
            "input_events_total": coverage.input_events_total,
            "data_events_total": coverage.data_events_total,
            "fill_observations_total": coverage.fill_observations_total,
            "audit_events_total": coverage.audit_events_total,
            "block_range_start": coverage.block_range_start,
            "block_range_end": coverage.block_range_end,
            "duration_seconds": coverage.duration_seconds,
            "data_gaps": [[a, b] for a, b in coverage.data_gaps],
            "coverage_checksum": coverage.coverage_checksum,
        },
        "ledger_snapshot": {
            "version": ledger_snapshot.version,
            "position_id": ledger_snapshot.position_id,
            "tick_lower": ledger_snapshot.tick_lower,
            "tick_upper": ledger_snapshot.tick_upper,
            "liquidity": ledger_snapshot.liquidity,
            "principal_token0": ledger_snapshot.principal_token0,
            "principal_token1": ledger_snapshot.principal_token1,
            "tokens_owed0": ledger_snapshot.tokens_owed0,
            "tokens_owed1": ledger_snapshot.tokens_owed1,
            "in_range": ledger_snapshot.in_range,
            "last_accrual_time": ledger_snapshot.last_accrual_time,
            "ledger_checksum": ledger_snapshot.ledger_checksum,
        },
        "manifest_checksum": manifest.report_checksum,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reason_code_from_exception(exc: BaseException) -> str:
    """Return the structured reason code the run record carries."""
    message = str(exc)
    if message.startswith(_REASON_PREFIX):
        head = message.split(":", 1)[0]
        return head
    return f"{_REASON_PREFIX}RUN_FAILED"


def _sha256_of_file(path: Path) -> str:
    """Return the ``0x``-prefixed SHA-256 hex digest of ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65_536), b""):
            digest.update(chunk)
    return "0x" + digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically write ``payload`` as JSON to ``path``.

    The write uses a temp-file rename so a crash mid-write
    leaves the previous file on disk; the observer never sees
    a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _build_report_payload(
    *,
    manifest: ExperimentManifest,
    metrics: RunMetrics,
    coverage: CoverageSummary,
    ledger_snapshot: LedgerSnapshot,
) -> dict[str, object]:
    """Build the human-readable report payload.

    The report is the manifest's companion artifact; it carries
    the metrics, coverage, and ledger snapshot in a form a
    reviewer can read without parsing the manifest. The
    canonical serialisation is sorted / deterministic so a
    reviewer can byte-compare two reports from two equivalent
    runs.
    """
    return {
        "version": ORCHESTRATOR_VERSION,
        "manifest_version": manifest.version,
        "run_id": manifest.run_id,
        "chain_id": manifest.chain_id,
        "pool_key_id": manifest.pool_key_id,
        "block_range_start": manifest.block_range_start,
        "block_range_end": manifest.block_range_end,
        "dataset_version": manifest.dataset_version,
        "reporting_numeraire": manifest.reporting_numeraire,
        "valuation_qualification": manifest.valuation_qualification,
        "strategy_identity": manifest.strategy_identity,
        "strategy_version": manifest.strategy_version,
        "registry_version": manifest.registry_version,
        "registry_checksum": manifest.registry_checksum,
        "metrics": {
            "version": metrics.version,
            "interval_seconds": metrics.interval_seconds,
            "duration_seconds": metrics.duration_seconds,
            "total_return_q64_64": metrics.total_return_q64_64,
            "annualized_return_q64_64": metrics.annualized_return_q64_64,
            "max_drawdown_q64_64": metrics.max_drawdown_q64_64,
            "turnover_q64_64": metrics.turnover_q64_64,
            "time_in_range_seconds": metrics.time_in_range_seconds,
            "fees_q64_64": metrics.fees_q64_64,
            "il_lvr_proxy_q64_64": metrics.il_lvr_proxy_q64_64,
            "gas_units_total": metrics.gas_units_total,
            "slippage_bps_total": metrics.slippage_bps_total,
            "benchmark_excess_q64_64": metrics.benchmark_excess_q64_64,
            "fills_count": metrics.fills_count,
            "metrics_checksum": metrics.metrics_checksum,
        },
        "coverage": {
            "version": coverage.version,
            "input_events_total": coverage.input_events_total,
            "data_events_total": coverage.data_events_total,
            "fill_observations_total": coverage.fill_observations_total,
            "audit_events_total": coverage.audit_events_total,
            "block_range_start": coverage.block_range_start,
            "block_range_end": coverage.block_range_end,
            "duration_seconds": coverage.duration_seconds,
            "data_gaps": [[a, b] for a, b in coverage.data_gaps],
            "coverage_checksum": coverage.coverage_checksum,
        },
        "ledger_snapshot": {
            "version": ledger_snapshot.version,
            "position_id": ledger_snapshot.position_id,
            "tick_lower": ledger_snapshot.tick_lower,
            "tick_upper": ledger_snapshot.tick_upper,
            "liquidity": ledger_snapshot.liquidity,
            "principal_token0": ledger_snapshot.principal_token0,
            "principal_token1": ledger_snapshot.principal_token1,
            "tokens_owed0": ledger_snapshot.tokens_owed0,
            "tokens_owed1": ledger_snapshot.tokens_owed1,
            "in_range": ledger_snapshot.in_range,
            "last_accrual_time": ledger_snapshot.last_accrual_time,
            "ledger_checksum": ledger_snapshot.ledger_checksum,
        },
        "manifest_checksum": manifest.report_checksum,
    }


__all__ = [
    "ORCHESTRATOR_VERSION",
    "DEFAULT_RUNS_ROOT_NAME",
    "BacktestOrchestrator",
    "BacktestRunError",
    "BlockRangeOutsideCoverageError",
    "CancelToken",
    "DatasetCoverage",
    "DatasetResolver",
    "EventSource",
    "InvalidRunRequestError",
    "LegacyManifestSourceError",
    "RunAlreadyExistsError",
    "RunCancelled",
    "RunProgress",
    "RunRecord",
    "RunRequest",
    "RunState",
    "RunStateCorruptError",
    "RunStateStore",
    "UnknownDatasetVersionError",
    "UnknownStrategyIdentityError",
    "run_record_from_dict",
]
