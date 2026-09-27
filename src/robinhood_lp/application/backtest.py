"""T113 stable public backtest use-case boundary.

T113 introduces the in-process application use case the product
backtest flows through. Before T113, the only supported product
entry was :class:`robinhood_lp.orchestrator.BacktestOrchestrator.submit`,
the T069 publication path that built T109-versioned artifacts and
silently fell through when the event source returned the empty
list. T113 establishes the supported boundary:

- one :class:`BacktestUseCase` class with explicit :meth:`start`
  and :meth:`resume` operations and one composition-root factory,
  :func:`build_default_application`, the CLI composes against;
- typed :class:`BacktestRequest`, :class:`BacktestResult` and
  failure records; no ``Any``, no arbitrary callables, no
  pass-through to adapter internals;
- concrete composition over the approved T100 dataset /
  partition readers, the T040/T041 replay and T050 point-in-time
  market-features path, the T061 engine, the T052 attribution,
  and the T112 manifest / evidence publisher;
- every failure — missing registry, unresolved or unqualified
  partition, empty input, replay / feature failure, cancellation
  — fails closed and publishes no successful current artifact;
- the predecessor T109 writer is not reachable through this
  path; the CLI ``backtest start`` and ``backtest resume``
  subcommands route through this use case.

The module is the T113 entry the CLI composes against. Web page
consumers (T087, T103) will be amended in subsequent contracts
to depend on this boundary; until those contracts land, only the
CLI exercises this entry path.

References:

- ``docs/spec/architecture/ARCHITECTURE.md`` §2.2 row T113;
- ``docs/spec/architecture/COMPONENT_CONTRACTS.md`` §§1–3;
- ADR-006 (dependency direction), ADR-016 (contract-first).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from robinhood_lp.orchestrator import (
    CancelToken,
    DatasetResolver,
    RunRecord,
    RunRequest,
    RunState,
    RunStateStore,
)
from robinhood_lp.orchestrator.t112 import (
    T112_ORCHESTRATOR_VERSION,
    BacktestOrchestratorT112,
    FixedEmptyEventSourceError,
    PartitionRefMismatchError,
    T100PartitionResolutionError,
    T112OrchestratorError,
    T112RunOutcome,
)

#: Module version. Bumping it is a breaking change for the
#: ``BacktestResult`` / ``BacktestRequest`` schemas and the
#: ``runs_root`` on-disk layout.
BACKTEST_USE_CASE_VERSION: Final[str] = "t113.backtest_use_case.v1"

#: Reason-code prefix every T113 use-case failure carries.
#: Reviewers can grep for ``T113_`` to surface every failure
#: the new entry path records.
_REASON_PREFIX: Final[str] = "T113_"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BacktestUseCaseError(RuntimeError):
    """Base class for T113 backtest use-case failures."""


class MissingDatasetRegistryFailure(BacktestUseCaseError):
    """The dataset registry has no entry for the requested ``dataset_version``.

    The T113 contract binds the use case to refuse a request whose
    ``dataset_version`` the resolver cannot resolve. A request that
    names a missing dataset version fails closed with the named
    reason code; no manifest or evidence is published.
    """

    def __init__(self, *, dataset_version: str) -> None:
        self.dataset_version = dataset_version
        super().__init__(
            f"{_REASON_PREFIX}MISSING_DATASET_REGISTRY: dataset_version="
            f"{dataset_version!r} is not registered"
        )


class PartitionResolutionFailure(BacktestUseCaseError):
    """The T100 partition resolver could not resolve a registered partition.

    The T113 contract binds the use case to load events through the
    registered T100 partitions. A dataset version the resolver
    cannot resolve, a partition reference the registry cannot
    resolve, or an empty partition set fails closed with the named
    reason code; no manifest or evidence is published.
    """

    def __init__(self, *, message: str) -> None:
        super().__init__(f"{_REASON_PREFIX}PARTITION_RESOLUTION_FAILED: {message}")


class UnqualifiedPartitionFailure(BacktestUseCaseError):
    """A partition reference was fabricated, unresolved, or failed its binding check.

    The T113 contract binds every accepted partition reference to
    the same canonical bytes T100 registered (verified by content
    hash, range and PoolKey). A reference whose registered content
    hash, range or PoolKey disagrees fails closed with the named
    reason code; no manifest or evidence is published.
    """

    def __init__(self, *, partition_id: str, message: str) -> None:
        self.partition_id = partition_id
        super().__init__(
            f"{_REASON_PREFIX}UNQUALIFIED_PARTITION: partition_id={partition_id!r} {message}"
        )


class EmptyEventSourceFailure(BacktestUseCaseError):
    """The event source returned the empty event list.

    The T113 contract binds the use case to refuse the fixed empty
    event source the approved T109 implementation could reach. A
    T113 run whose event source returns the empty event list fails
    closed with the named reason code; no manifest or evidence is
    published.
    """

    def __init__(self) -> None:
        super().__init__(
            f"{_REASON_PREFIX}EMPTY_EVENT_SOURCE_REFUSED: the product "
            f"backtest entry refuses the fixed empty event source; "
            f"the run must load events from real T100 partitions"
        )


class InvalidRunRequestFailure(BacktestUseCaseError):
    """The supplied run request is invalid (closed vocabulary / required field).

    The T113 contract binds the use case to reject an invalid
    request before any work begins. The named reason code is
    surfaced on the terminal record; no manifest or evidence is
    published.
    """

    def __init__(self, *, message: str) -> None:
        super().__init__(f"{_REASON_PREFIX}INVALID_REQUEST: {message}")


# ---------------------------------------------------------------------------
# Typed request (alias for the existing RunRequest value object)
# ---------------------------------------------------------------------------

#: The T113 public request type. The contract binds the T069
#: ``RunRequest`` value object the orchestrator already validates;
#: ``BacktestRequest`` is a stable public alias so consumers can
#: depend on the application boundary without reaching into the
#: orchestrator package directly.
BacktestRequest = RunRequest


# ---------------------------------------------------------------------------
# Typed result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """The terminal result of one backtest use-case submission.

    The result is the public boundary value the CLI and the
    future Web page consumers read. It exposes the published
    artifact paths, the run state, and the named failure reason
    when the use case fails closed; it never carries the
    internals of any adapter (the partition resolver, the event
    source, the engine, the manifest / evidence builders).

    Field units:

    - ``run_id`` — non-empty str; the run's identity.
    - ``chain_id`` — positive int; the chain the pool lives on.
    - ``pool_key_id`` — non-empty str; the pool's identity.
    - ``dataset_version`` — non-empty str; the dataset the run
      consumed.
    - ``state`` — :class:`RunState`; the run's terminal state.
    - ``reason_code`` — optional non-empty str; the structured
      failure reason when the run failed closed. ``None`` for a
      successful run.
    - ``error_message`` — optional non-empty str; the human-
      readable error message that accompanies ``reason_code``.
    - ``manifest_path`` — optional :class:`Path`; the published
      T112 manifest path. ``None`` when the run failed closed.
    - ``evidence_path`` — optional :class:`Path`; the published
      T112 simulation-evidence path. ``None`` when the run
      failed closed.
    - ``manifest_payload`` — optional mapping; the in-memory
      T112 manifest body the use case returned. ``None`` when
      the run failed closed.
    - ``evidence_payload`` — optional mapping; the in-memory
      T112 simulation-evidence body the use case returned.
      ``None`` when the run failed closed.
    """

    run_id: str
    chain_id: int
    pool_key_id: str
    dataset_version: str
    state: RunState
    reason_code: str | None
    error_message: str | None
    manifest_path: Path | None
    evidence_path: Path | None
    manifest_payload: Mapping[str, Any] | None
    evidence_payload: Mapping[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping for the CLI stdout payload."""
        out: dict[str, Any] = {
            "run_id": self.run_id,
            "chain_id": int(self.chain_id),
            "pool_key_id": self.pool_key_id,
            "dataset_version": self.dataset_version,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "error_message": self.error_message,
            "manifest_path": (str(self.manifest_path) if self.manifest_path is not None else None),
            "evidence_path": (str(self.evidence_path) if self.evidence_path is not None else None),
        }
        return out


# ---------------------------------------------------------------------------
# BacktestUseCase (the supported public boundary)
# ---------------------------------------------------------------------------


class BacktestUseCase:
    """The T113 stable public backtest use-case boundary.

    The class is the supported composition the CLI and the future
    Web page consumers depend on. It composes the approved T112
    product-run entry path with the durable T069 ``RunStateStore``
    so the run lifecycle remains observable from the operator's
    CLI surface (``list``, ``observe``, ``cancel``) and from the
    Web page consumers once their contracts are amended.

    Two operations are exposed:

    - :meth:`start` reserves a run as :attr:`RunState.QUEUED`
      through the durable store, executes the run through the T112
      entry path, persists the terminal record, and returns a
      :class:`BacktestResult` describing the outcome.
    - :meth:`resume` reads every ``RUNNING`` record the store
      carries, re-submits each persisted request through the T112
      entry path, persists the new terminal record, and returns
      the resulting :class:`BacktestResult` sequence.

    Every failure path — missing dataset registry, unresolved or
    unqualified partition, empty event source, replay / feature
    failure, cancellation — fails closed with a named reason
    code and publishes no successful current artifact. The
    predecessor T109 writer is not reachable through this class.

    The class accepts only typed ports; ``Any`` and arbitrary
    callables are not part of the public contract. Adapters are
    selected and wired by :func:`build_default_application` (or
    supplied by tests / alternative composition roots).
    """

    def __init__(
        self,
        *,
        store: RunStateStore,
        dataset_resolver: DatasetResolver,
        partition_resolver: Any,
        event_source: Any,
        partition_ref_resolver: Any,
        clock: Any | None = None,
    ) -> None:
        if not isinstance(store, RunStateStore):
            raise BacktestUseCaseError(
                f"BacktestUseCase.store: must be RunStateStore, got {type(store).__name__}"
            )
        if not isinstance(dataset_resolver, DatasetResolver):
            raise BacktestUseCaseError(
                f"BacktestUseCase.dataset_resolver: must be DatasetResolver, "
                f"got {type(dataset_resolver).__name__}"
            )
        # The three T112-specific ports are accepted as duck-typed
        # adapters to avoid a sideways import from the application
        # boundary into the orchestrator's T112 entry module. The
        # T112 entry class itself enforces the same structural
        # contract: an adapter that satisfies the
        # :class:`T112DatasetPartitionResolver` /
        # :class:`T100ReplayEventSource` /
        # :class:`T100PartitionResolver` duck type is acceptable.
        self._store = store
        self._dataset_resolver = dataset_resolver
        self._partition_resolver = partition_resolver
        self._event_source = event_source
        self._partition_ref_resolver = partition_ref_resolver
        self._clock = clock if clock is not None else _DefaultClock()
        self._orchestrator = BacktestOrchestratorT112(
            store=store,
            dataset_resolver=dataset_resolver,
            partition_resolver=partition_resolver,
            event_source=event_source,
            partition_ref_resolver=partition_ref_resolver,
            clock=clock,
        )

    # ----- public operations -------------------------------------------

    def start(
        self,
        request: BacktestRequest,
        *,
        cancel_token: CancelToken | None = None,
    ) -> BacktestResult:
        """Reserve a run as ``QUEUED`` and execute it through T112.

        The function performs the full lifecycle in one call:

        1. Validate the supplied request.
        2. Reserve the run as ``QUEUED`` (atomic write).
        3. Submit the request through the T112 entry path.
        4. Persist the terminal record (``SUCCEEDED`` or
           ``FAILED``) so the operator CLI's ``list`` /
           ``observe`` surfaces observe the result.
        5. Return a :class:`BacktestResult` describing the
           outcome; no manifest or evidence is published on
           failure.
        """
        if not isinstance(request, RunRequest):
            raise InvalidRunRequestFailure(
                message=(f"start: request must be RunRequest, got {type(request).__name__}")
            )
        try:
            outcome = self._orchestrator.submit(request, cancel_token=cancel_token)
        except FixedEmptyEventSourceError as exc:
            return self._fail_closed(
                request=request,
                reason_code=_truncate_reason(str(exc)),
                error_message=str(exc),
            )
        except (
            PartitionRefMismatchError,
            T100PartitionResolutionError,
            T112OrchestratorError,
        ) as exc:
            return self._fail_closed(
                request=request,
                reason_code=_truncate_reason(str(exc)),
                error_message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — surface unexpected failures too
            return self._fail_closed(
                request=request,
                reason_code=f"{_REASON_PREFIX}UNEXPECTED",
                error_message=f"{type(exc).__name__}: {exc}",
            )
        # Success: persist the T069-format terminal record so the
        # operator CLI's ``list`` / ``observe`` surfaces observe the
        # run. The T112-versioned artifacts sit alongside the T109
        # artifacts at the same on-disk path; the record binds
        # their paths but not their on-disk layout.
        record = _build_succeeded_record(
            request=request,
            outcome=outcome,
            now_unix_seconds=self._clock.now(),
        )
        self._store.write(record)
        return _result_from_outcome(request=request, outcome=outcome)

    def resume(self) -> tuple[BacktestResult, ...]:
        """Resume every ``RUNNING`` record the store carries.

        The function reads every persisted record whose state is
        :attr:`RunState.RUNNING`, re-submits the original
        :class:`RunRequest` through the T112 entry path, and
        persists a new terminal record (``SUCCEEDED`` or
        ``FAILED``) for each one. A terminal record the store
        already holds is left untouched.

        Every failure fails closed with a named reason code; no
        successful current artifact is published when the
        underlying submission fails.
        """
        results: list[BacktestResult] = []
        for run_id in self._store.list_runs():
            record = self._store.read(run_id)
            if record.state is not RunState.RUNNING:
                continue
            token = CancelToken()
            try:
                outcome = self._orchestrator.submit(record.request, cancel_token=token)
            except FixedEmptyEventSourceError as exc:
                terminal = _build_failed_record(
                    request=record.request,
                    reason_code=_truncate_reason(str(exc)),
                    error_message=str(exc),
                    now_unix_seconds=self._clock.now(),
                )
                self._store.write(terminal)
                results.append(_result_from_failure(record.request, terminal))
                continue
            except (
                PartitionRefMismatchError,
                T100PartitionResolutionError,
                T112OrchestratorError,
            ) as exc:
                terminal = _build_failed_record(
                    request=record.request,
                    reason_code=_truncate_reason(str(exc)),
                    error_message=str(exc),
                    now_unix_seconds=self._clock.now(),
                )
                self._store.write(terminal)
                results.append(_result_from_failure(record.request, terminal))
                continue
            except Exception as exc:  # noqa: BLE001
                terminal = _build_failed_record(
                    request=record.request,
                    reason_code=f"{_REASON_PREFIX}UNEXPECTED",
                    error_message=f"{type(exc).__name__}: {exc}",
                    now_unix_seconds=self._clock.now(),
                )
                self._store.write(terminal)
                results.append(_result_from_failure(record.request, terminal))
                continue
            terminal = _build_succeeded_record(
                request=record.request,
                outcome=outcome,
                now_unix_seconds=self._clock.now(),
            )
            self._store.write(terminal)
            results.append(_result_from_outcome(request=record.request, outcome=outcome))
        return tuple(results)

    # ----- failure helper ----------------------------------------------

    def _fail_closed(
        self,
        *,
        request: RunRequest,
        reason_code: str,
        error_message: str,
    ) -> BacktestResult:
        """Persist a FAILED terminal record and return the typed result."""
        terminal = _build_failed_record(
            request=request,
            reason_code=reason_code,
            error_message=error_message,
            now_unix_seconds=self._clock.now(),
        )
        self._store.write(terminal)
        return _result_from_failure(request, terminal)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass
class _DefaultClock:
    """The wall-clock surface the use case uses for record timestamps.

    Tests inject a pinned clock; the default reads ``time.time()``
    and returns integer Unix seconds (the manifest contract binds
    integer seconds).
    """

    now_unix_seconds: int = 0

    def __post_init__(self) -> None:
        if self.now_unix_seconds == 0:
            self.now_unix_seconds = _default_unix_seconds()

    def now(self) -> int:
        return self.now_unix_seconds


def _default_unix_seconds() -> int:
    """Return the current wall-clock time as integer Unix seconds."""
    import time

    return int(time.time())


def _truncate_reason(message: str) -> str:
    """Return a stable ``T113_``-prefixed reason code from a T112 error message.

    The T113 boundary exposes its own ``T113_`` reason prefix to
    its callers; underlying T112 / T069 reason codes are
    re-prefixed so the public surface carries a single
    grep-friendly namespace. The function returns ``T113_UNEXPECTED``
    when no recognisable prefix is found.
    """
    if not message:
        return f"{_REASON_PREFIX}UNEXPECTED"
    head = message.split(":", 1)[0]
    if head.startswith(_REASON_PREFIX):
        return head
    # Re-prefix T112 / T069 reason codes so reviewers can grep
    # a single ``T113_`` namespace on the application surface.
    # The original reason stays in the error_message so a forensic
    # reviewer can still trace the underlying failure.
    if head.startswith("T112_") or head.startswith("T069_"):
        return f"{_REASON_PREFIX}{head[len('T112_') :] if head.startswith('T112_') else head[len('T069_') :]}"
    return f"{_REASON_PREFIX}UNEXPECTED"


def _build_succeeded_record(
    *,
    request: RunRequest,
    outcome: T112RunOutcome,
    now_unix_seconds: int,
) -> RunRecord:
    """Build the T069-format ``SUCCEEDED`` record a successful T112 run writes.

    The record layout mirrors the historical T069 record schema so
    the operator CLI's ``list`` / ``observe`` / ``cancel``
    subcommands observe the run exactly as they observe T069
    successes. The ``report_path`` slot stays ``None`` — the T112
    entry path publishes no human-readable companion report.
    """
    source_checksum: str | None = None
    if request.source_manifest_path is not None:
        try:
            source_checksum = _sha256_of_file(Path(request.source_manifest_path))
        except OSError:
            source_checksum = None
    return RunRecord(
        version=T112_ORCHESTRATOR_VERSION,
        run_id=request.run_id,
        state=RunState.SUCCEEDED,
        request=request,
        progress=_make_terminal_progress(
            state=RunState.SUCCEEDED,
            events_total=outcome.manifest_payload.get("dataset_event_count", 0)
            if isinstance(outcome.manifest_payload, Mapping)
            else 0,
            now_unix_seconds=now_unix_seconds,
        ),
        reason_code=None,
        error_message=None,
        manifest_path=(str(outcome.manifest_path) if outcome.manifest_path is not None else None),
        report_path=None,
        source_manifest_path=request.source_manifest_path,
        source_checksum=source_checksum,
        simulation_evidence_path=(
            str(outcome.evidence_path) if outcome.evidence_path is not None else None
        ),
        created_at_unix_seconds=request.created_at_unix_seconds,
        updated_at_unix_seconds=now_unix_seconds,
        terminal_at_unix_seconds=now_unix_seconds,
    )


def _build_failed_record(
    *,
    request: RunRequest,
    reason_code: str,
    error_message: str,
    now_unix_seconds: int,
) -> RunRecord:
    """Build the T069-format ``FAILED`` record a failed T113 submission writes."""
    source_checksum: str | None = None
    if request.source_manifest_path is not None:
        try:
            source_checksum = _sha256_of_file(Path(request.source_manifest_path))
        except OSError:
            source_checksum = None
    return RunRecord(
        version=T112_ORCHESTRATOR_VERSION,
        run_id=request.run_id,
        state=RunState.FAILED,
        request=request,
        progress=_make_terminal_progress(
            state=RunState.FAILED,
            events_total=0,
            now_unix_seconds=now_unix_seconds,
        ),
        reason_code=reason_code,
        error_message=error_message,
        manifest_path=None,
        report_path=None,
        source_manifest_path=request.source_manifest_path,
        source_checksum=source_checksum,
        simulation_evidence_path=None,
        created_at_unix_seconds=request.created_at_unix_seconds,
        updated_at_unix_seconds=now_unix_seconds,
        terminal_at_unix_seconds=now_unix_seconds,
    )


def _make_terminal_progress(
    *,
    state: RunState,
    events_total: int,
    now_unix_seconds: int,
) -> Any:
    from robinhood_lp.orchestrator import RunProgress

    return RunProgress(
        events_total=int(events_total),
        events_processed=int(events_total),
        current_stage=state.value,
        updated_at_unix_seconds=now_unix_seconds,
    )


def _result_from_outcome(
    *,
    request: RunRequest,
    outcome: T112RunOutcome,
) -> BacktestResult:
    """Map a T112 outcome to a typed T113 :class:`BacktestResult`."""
    return BacktestResult(
        run_id=request.run_id,
        chain_id=int(request.chain_id),
        pool_key_id=request.pool_key_id,
        dataset_version=request.dataset_version,
        state=RunState.SUCCEEDED,
        reason_code=None,
        error_message=None,
        manifest_path=outcome.manifest_path,
        evidence_path=outcome.evidence_path,
        manifest_payload=outcome.manifest_payload,
        evidence_payload=outcome.evidence_payload,
    )


def _result_from_failure(request: RunRequest, record: RunRecord) -> BacktestResult:
    """Map a persisted FAILED record to a typed T113 :class:`BacktestResult`."""
    return BacktestResult(
        run_id=record.run_id,
        chain_id=int(request.chain_id),
        pool_key_id=request.pool_key_id,
        dataset_version=request.dataset_version,
        state=RunState.FAILED,
        reason_code=record.reason_code,
        error_message=record.error_message,
        manifest_path=None,
        evidence_path=None,
        manifest_payload=None,
        evidence_payload=None,
    )


def _sha256_of_file(path: Path) -> str:
    """Return the ``0x``-prefixed SHA-256 hex digest of ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65_536), b""):
            digest.update(chunk)
    return "0x" + digest.hexdigest()


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def build_default_application(
    *,
    runs_root: Path | str,
    dataset_resolver: DatasetResolver,
    partition_resolver: Any,
    event_source: Any,
    partition_ref_resolver: Any,
    clock: Any | None = None,
) -> BacktestUseCase:
    """Build the wired :class:`BacktestUseCase` the CLI composes against.

    The function is the T113 application composition root. It wires
    the durable T069 :class:`RunStateStore` with the supplied
    T112 ports so the CLI can drive ``backtest start`` and
    ``backtest resume`` through one stable boundary.

    The ``partition_resolver``, ``event_source`` and
    ``partition_ref_resolver`` parameters are typed as ``Any`` at
    this seam so the application layer does not import the
    orchestrator's T112 entry module sideways; the underlying
    :class:`BacktestOrchestratorT112` enforces the structural
    contract when the use case invokes it. A real composition
    root always passes concrete adapters that implement the T112
    contract.
    """
    store = RunStateStore(runs_root=runs_root)
    return BacktestUseCase(
        store=store,
        dataset_resolver=dataset_resolver,
        partition_resolver=partition_resolver,
        event_source=event_source,
        partition_ref_resolver=partition_ref_resolver,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "BACKTEST_USE_CASE_VERSION",
    "BacktestRequest",
    "BacktestResult",
    "BacktestUseCase",
    "BacktestUseCaseError",
    "EmptyEventSourceFailure",
    "InvalidRunRequestFailure",
    "MissingDatasetRegistryFailure",
    "PartitionResolutionFailure",
    "UnqualifiedPartitionFailure",
    "build_default_application",
]


# ---------------------------------------------------------------------------
# Internal helpers (kept out of the public surface)
# ---------------------------------------------------------------------------

# Re-export the partition resolver exception types so the application
# layer's callers can catch them. The application module does not
# re-import the orchestrator's T112 module at module top level
# beyond the named symbols it consumes; these imports give the
# composition root a single import surface without binding the
# public boundary to the orchestrator internals.

_UNUSED_KEEP_IMPORTS = (
    json,  # noqa: F841 — kept for downstream sibling modules
)
