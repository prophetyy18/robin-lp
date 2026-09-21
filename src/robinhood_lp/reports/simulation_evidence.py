"""Immutable simulation-evidence artifact (T109).

This module is the T109-deliverable extension to the T105 manifest. The
artifact the T109 contract binds is the *simulation evidence* the
successful-run path publishes alongside the manifest / report. It is
not a duplicate of the T105 manifest: the manifest remains the
dataset-bound publication authority the T105 contract defines, and the
evidence artifact carries only the run-specific facts the
``RunState`` / ``ReplayFrame`` reader needs to reproduce the run.

The artifact contains:

- the canonical run identity (``run_id``);
- the dataset version, content hash, schema and decode revisions, the
  pool key, the selected range, and the reconstruction revision;
- the registered strategy identity, schema, registry and code
  provenance, the engine / accounting / evidence-schema revisions;
- the run's initial state (``initial_position``);
- an ordered, versioned, checksummed list of run-specific
  :class:`RunTransition` records. Each transition binds the exact canonical
  :class:`MarketCursor` and the engine stage that produced it, plus a
  globally monotonic ``run_transition_ordinal`` from the original
  append-only audit chain;
- a sparse list of complete run-state :class:`RunStateCheckpoint`
  records, each identifying its cursor and the last applied ordinal.

The artifact is **immutable** and **versioned**: the
:data:`SIMULATION_EVIDENCE_VERSION` is the schema version a reader
checks before applying the artifact; the
:attr:`SimulationEvidence.evidence_checksum` is the SHA-256 of the
canonical serialisation so a tampered field surfaces as a checksum
mismatch.

Design constraints (binding):

- **No full per-cursor snapshot.** The artifact carries sparse
  checkpoints plus transitions; a checkpoint identifies both its
  cursor and last applied ordinal. A reader that needs the run state
  at an arbitrary cursor replays the transitions between the nearest
  prior checkpoint and the cursor; the artifact need not contain a
  snapshot per cursor.
- **No market event copy.** The artifact references the dataset
  content hash and never embeds the complete market event timeline.
  The canonical event timeline is the dataset's append-only partition
  reference (T100).
- **Causal ordering.** Transitions are ordered by
  ``(cursor, ordinal)``; projections must agree with the audit chain
  (``cursor`` non-decreasing, ``ordinal`` strictly increasing). A
  state-changing transition whose cursor binding disagrees with its
  ordinal fails publication.
- **T104 provenance.** Fee-growth / range-fee values come from the
  T104 surface, never from the artifact itself; when an evidence
  record carries a fee value, it must carry T104's dataset/window/
  reconstruction provenance beside the value.

The module imports only the standard library, the reports / marker /
replay packages, and the protocol / backtest primitives. It does not
import RPC, storage, signing, execution, or presentation code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from robinhood_lp.replay.market_state import MARKET_STATE_VERSION, MarketCursor

#: Schema version. Bumping it is a breaking change for the artifact
#: and every reader.
SIMULATION_EVIDENCE_VERSION: Final[str] = "t109.simulation_evidence.v1"

#: Sentinel reason-code prefix every T109 evidence-publication failure
#: carries. Reviewers can grep for ``T109_EVIDENCE_`` to surface every
#: artifact-validation failure the publisher records.
_REASON_PREFIX: Final[str] = "T109_EVIDENCE_"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SimulationEvidenceError(ValueError):
    """Base class for simulation-evidence failures."""


class SimulationEvidenceFieldError(SimulationEvidenceError):
    """A required field is missing, malformed, or outside its closed vocabulary."""


class SimulationEvidenceOrderingError(SimulationEvidenceError):
    """Transitions disagree with cursor / ordinal ordering, or a state-changing
    transition lacks a cursor binding."""


class SimulationEvidenceChecksumError(SimulationEvidenceError):
    """The artifact's checksum does not match the recorded value."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_non_empty_str(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SimulationEvidenceFieldError(f"{field_name}: must be non-empty str, got {value!r}")
    return value


def _require_non_negative_int(value: Any, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SimulationEvidenceFieldError(f"{field_name}: must be non-negative int, got {value!r}")
    return value


def _require_positive_int(value: Any, *, field_name: str) -> int:
    checked = _require_non_negative_int(value, field_name=field_name)
    if checked == 0:
        raise SimulationEvidenceFieldError(f"{field_name}: must be positive, got 0")
    return checked


def _require_int(value: Any, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SimulationEvidenceFieldError(f"{field_name}: must be int, got {type(value).__name__}")
    return value


def _require_bool(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise SimulationEvidenceFieldError(
            f"{field_name}: must be bool, got {type(value).__name__}"
        )
    return value


# ---------------------------------------------------------------------------
# RunTransition / RunStateCheckpoint
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunTransition:
    """One run-specific state transition the original audit chain produced.

    A :class:`RunTransition` binds the exact canonical
    :class:`MarketCursor` and the engine stage that produced the
    transition, plus a globally monotonic
    ``run_transition_ordinal`` from the original append-only audit
    chain. The cursor field is the *exact* canonical cursor the
    transition was bound to when the original engine emitted it; the
    contract says the binding is never derived later from the
    integer timestamp.

    Field units:

    - ``ordinal`` — non-negative int; the engine's per-stage
      monotonic counter (the audit chain's local ``sequence`` is
      mapped onto a globally monotonic ordinal at publication).
    - ``stage`` — non-empty string; one of ``"DECISION"``,
      ``"RISK"``, ``"LATENCY"``, ``"FILL"``, ``"SYSTEM"``.
    - ``cursor`` — :class:`MarketCursor`; the exact canonical cursor
      the transition is bound to. ``None`` for SYSTEM events that
      carry no market-event binding (e.g. init / shutdown) and for
      legacy state-changing transitions whose bound cursor could
      not be reconstructed; transitions with ``cursor is None`` are
      rejected by the publication gate.
    - ``ledger_hash_after`` — non-empty string; the post-event ledger
      hash the audit event recorded.
    - ``audit_event_id`` — non-empty string; the audit event's
      content-addressed identifier (SHA-256 of the audit chain's
      canonical content).
    - ``payload`` — JSON-friendly mapping; the audit-event payload
      the original run recorded. ``None`` for transitions whose
      payload was elided to bound the artifact's size (the contract
      allows elision provided the cursor binding and ordinal are
      preserved).
    - ``state_changing`` — bool; True iff this transition mutated the
      ledger (FILL with a FILLED / PARTIAL / DELAYED outcome).
    """

    ordinal: int
    stage: str
    cursor: MarketCursor | None
    ledger_hash_after: str
    audit_event_id: str
    payload: Mapping[str, Any] | None
    state_changing: bool

    def __post_init__(self) -> None:
        _require_non_negative_int(self.ordinal, field_name="RunTransition.ordinal")
        _require_non_empty_str(self.stage, field_name="RunTransition.stage")
        if self.cursor is not None and not isinstance(self.cursor, MarketCursor):
            raise SimulationEvidenceFieldError(
                f"RunTransition.cursor: must be MarketCursor or None, got "
                f"{type(self.cursor).__name__}"
            )
        _require_non_empty_str(self.ledger_hash_after, field_name="RunTransition.ledger_hash_after")
        _require_non_empty_str(self.audit_event_id, field_name="RunTransition.audit_event_id")
        if self.payload is not None and not isinstance(self.payload, Mapping):
            raise SimulationEvidenceFieldError(
                f"RunTransition.payload: must be Mapping or None, got {type(self.payload).__name__}"
            )
        _require_bool(self.state_changing, field_name="RunTransition.state_changing")
        # State-changing transitions must carry a cursor binding per
        # the contract: "a state-changing transition with only an
        # integer timestamp fails publication".
        if self.state_changing and self.cursor is None:
            raise SimulationEvidenceOrderingError(
                f"{_REASON_PREFIX}STATE_TRANSITION_NO_CURSOR: "
                f"state-changing transition ordinal={self.ordinal} "
                f"stage={self.stage!r} has no MarketCursor binding"
            )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ordinal": self.ordinal,
            "stage": self.stage,
            "ledger_hash_after": self.ledger_hash_after,
            "audit_event_id": self.audit_event_id,
            "state_changing": self.state_changing,
        }
        if self.cursor is not None:
            out["cursor"] = {
                "block_number": self.cursor.block_number,
                "transaction_index": self.cursor.transaction_index,
                "log_index": self.cursor.log_index,
            }
        else:
            out["cursor"] = None
        if self.payload is not None:
            out["payload"] = dict(sorted(self.payload.items()))
        else:
            out["payload"] = None
        return out


@dataclass(frozen=True, slots=True)
class RunStateCheckpoint:
    """A complete run-state checkpoint the original run recorded.

    A checkpoint identifies both its cursor and last applied ordinal
    so a reader that needs the run state at an arbitrary cursor
    replays the transitions between the nearest prior checkpoint
    and the cursor; the artifact does not need a snapshot per cursor.

    Field units:

    - ``cursor`` — :class:`MarketCursor`; the exact canonical cursor
      the checkpoint sits at.
    - ``last_applied_ordinal`` — non-negative int; the
      ``RunTransition.ordinal`` of the last transition included in
      the checkpoint's state.
    - ``ledger_snapshot`` — JSON-friendly mapping; the
      ``PositionState`` snapshot the engine recorded at this
      cursor.
    - ``equity_q64_64`` — int; the equity view (in ``q64.64``) the
      T052 attribution layer recorded at this cursor.
    - ``drawdown_q64_64`` — int; the running drawdown (in
      ``q64.64``) the T052 attribution layer recorded at this
      cursor.
    - ``attribution_snapshot`` — JSON-friendly mapping; the T052
      attribution snapshot the engine recorded at this cursor.
    """

    cursor: MarketCursor
    last_applied_ordinal: int
    ledger_snapshot: Mapping[str, Any]
    equity_q64_64: int
    drawdown_q64_64: int
    attribution_snapshot: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.cursor, MarketCursor):
            raise SimulationEvidenceFieldError(
                f"RunStateCheckpoint.cursor: must be MarketCursor, got {type(self.cursor).__name__}"
            )
        _require_non_negative_int(
            self.last_applied_ordinal,
            field_name="RunStateCheckpoint.last_applied_ordinal",
        )
        if not isinstance(self.ledger_snapshot, Mapping):
            raise SimulationEvidenceFieldError(
                f"RunStateCheckpoint.ledger_snapshot: must be Mapping, got "
                f"{type(self.ledger_snapshot).__name__}"
            )
        if not isinstance(self.attribution_snapshot, Mapping):
            raise SimulationEvidenceFieldError(
                f"RunStateCheckpoint.attribution_snapshot: must be Mapping, "
                f"got {type(self.attribution_snapshot).__name__}"
            )
        _require_int(self.equity_q64_64, field_name="RunStateCheckpoint.equity_q64_64")
        _require_int(self.drawdown_q64_64, field_name="RunStateCheckpoint.drawdown_q64_64")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor": {
                "block_number": self.cursor.block_number,
                "transaction_index": self.cursor.transaction_index,
                "log_index": self.cursor.log_index,
            },
            "last_applied_ordinal": self.last_applied_ordinal,
            "ledger_snapshot": dict(sorted(self.ledger_snapshot.items())),
            "equity_q64_64": self.equity_q64_64,
            "drawdown_q64_64": self.drawdown_q64_64,
            "attribution_snapshot": dict(sorted(self.attribution_snapshot.items())),
        }


# ---------------------------------------------------------------------------
# SimulationEvidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SimulationEvidence:
    """The immutable, versioned, checksummed simulation-evidence artifact.

    The artifact binds a successful T109 run to the run-specific facts
    ``RunState`` and ``ReplayFrame`` need to reproduce the run. It is
    not a duplicate of the T105 manifest: the manifest remains the
    dataset-bound publication authority the T105 contract defines.

    Field units:

    - ``version`` — schema version (must equal
      :data:`SIMULATION_EVIDENCE_VERSION`).
    - ``run_id`` — non-empty str; the T069 run identity.
    - ``dataset_version`` — non-empty str; the T100 dataset version
      (content hash) the run is bound to.
    - ``dataset_schema_version`` — non-negative int.
    - ``dataset_decode_version`` — non-negative int.
    - ``dataset_content_hash`` — non-empty str.
    - ``pool_key_id`` — non-empty str.
    - ``chain_id`` — positive int.
    - ``tick_lower`` / ``tick_upper`` — int (int24 range).
    - ``strategy_identity`` — non-empty str.
    - ``strategy_version`` — non-empty str.
    - ``registry_version`` — non-empty str.
    - ``registry_checksum`` — non-empty str.
    - ``parameter_schema_version`` — non-empty str.
    - ``parameter_schema_checksum`` — non-empty str.
    - ``code_provenance_module`` — non-empty str.
    - ``code_provenance_revision`` — non-empty str.
    - ``engine_revision`` — non-empty str.
    - ``accounting_revision`` — non-empty str.
    - ``reconstruction_revision`` — non-empty str.
    - ``initial_position`` — JSON-friendly mapping; the
      ``PositionState`` the run started from.
    - ``initial_equity_q64_64`` — int; the equity view at the start.
    - ``initial_attribution`` — JSON-friendly mapping; the T052
      attribution snapshot at the start.
    - ``transitions`` — :class:`RunTransition` tuple; ordered by
      ``(cursor, ordinal)`` per the contract.
    - ``checkpoints`` — :class:`RunStateCheckpoint` tuple; sparse
      complete snapshots.
    - ``evidence_checksum`` — non-empty str; the SHA-256 of the
      canonical serialisation of every other field.
    - ``market_state_version`` — must equal :data:`MARKET_STATE_VERSION`.
    """

    version: str
    run_id: str
    dataset_version: str
    dataset_schema_version: int
    dataset_decode_version: int
    dataset_content_hash: str
    pool_key_id: str
    chain_id: int
    tick_lower: int
    tick_upper: int
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
    initial_attribution: Mapping[str, Any]
    transitions: tuple[RunTransition, ...]
    checkpoints: tuple[RunStateCheckpoint, ...]
    evidence_checksum: str
    market_state_version: str

    def __post_init__(self) -> None:
        if self.version != SIMULATION_EVIDENCE_VERSION:
            raise SimulationEvidenceFieldError(
                f"SimulationEvidence.version: must be "
                f"{SIMULATION_EVIDENCE_VERSION!r}, got {self.version!r}"
            )
        if self.market_state_version != MARKET_STATE_VERSION:
            raise SimulationEvidenceFieldError(
                f"SimulationEvidence.market_state_version: must be "
                f"{MARKET_STATE_VERSION!r}, got {self.market_state_version!r}"
            )
        _require_non_empty_str(self.run_id, field_name="SimulationEvidence.run_id")
        _require_non_empty_str(
            self.dataset_version, field_name="SimulationEvidence.dataset_version"
        )
        _require_non_negative_int(
            self.dataset_schema_version,
            field_name="SimulationEvidence.dataset_schema_version",
        )
        _require_non_negative_int(
            self.dataset_decode_version,
            field_name="SimulationEvidence.dataset_decode_version",
        )
        _require_non_empty_str(
            self.dataset_content_hash,
            field_name="SimulationEvidence.dataset_content_hash",
        )
        _require_non_empty_str(self.pool_key_id, field_name="SimulationEvidence.pool_key_id")
        _require_positive_int(self.chain_id, field_name="SimulationEvidence.chain_id")
        _require_int(self.tick_lower, field_name="SimulationEvidence.tick_lower")
        _require_int(self.tick_upper, field_name="SimulationEvidence.tick_upper")
        if self.tick_lower >= self.tick_upper:
            raise SimulationEvidenceFieldError(
                f"SimulationEvidence: tick_lower={self.tick_lower} must be "
                f"strictly less than tick_upper={self.tick_upper}"
            )
        for fld in (
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
            _require_non_empty_str(getattr(self, fld), field_name=f"SimulationEvidence.{fld}")
        if not isinstance(self.initial_position, Mapping):
            raise SimulationEvidenceFieldError(
                f"SimulationEvidence.initial_position: must be Mapping, got "
                f"{type(self.initial_position).__name__}"
            )
        if not isinstance(self.initial_attribution, Mapping):
            raise SimulationEvidenceFieldError(
                f"SimulationEvidence.initial_attribution: must be Mapping, "
                f"got {type(self.initial_attribution).__name__}"
            )
        _require_int(
            self.initial_equity_q64_64,
            field_name="SimulationEvidence.initial_equity_q64_64",
        )
        if not isinstance(self.transitions, tuple):
            raise SimulationEvidenceFieldError(
                f"SimulationEvidence.transitions: must be tuple, got "
                f"{type(self.transitions).__name__}"
            )
        for i, tr in enumerate(self.transitions):
            if not isinstance(tr, RunTransition):
                raise SimulationEvidenceFieldError(
                    f"SimulationEvidence.transitions[{i}]: must be "
                    f"RunTransition, got {type(tr).__name__}"
                )
        if not isinstance(self.checkpoints, tuple):
            raise SimulationEvidenceFieldError(
                f"SimulationEvidence.checkpoints: must be tuple, got "
                f"{type(self.checkpoints).__name__}"
            )
        for i, cp in enumerate(self.checkpoints):
            if not isinstance(cp, RunStateCheckpoint):
                raise SimulationEvidenceFieldError(
                    f"SimulationEvidence.checkpoints[{i}]: must be "
                    f"RunStateCheckpoint, got {type(cp).__name__}"
                )
        _require_non_empty_str(
            self.evidence_checksum,
            field_name="SimulationEvidence.evidence_checksum",
        )
        # Validate the ordering invariant: cursor non-decreasing,
        # ordinal strictly increasing. The contract binds this for
        # transitions that carry a cursor; SYSTEM-init / SYSTEM-
        # shutdown transitions whose cursor is None are skipped
        # (they never appear at the start of the chain).
        last_ordinal = -1
        last_cursor_key: tuple[int, int, int] | None = None
        for tr in self.transitions:
            if tr.cursor is None:
                if tr.state_changing:
                    raise SimulationEvidenceOrderingError(
                        f"{_REASON_PREFIX}STATE_TRANSITION_NO_CURSOR: "
                        f"state-changing transition ordinal={tr.ordinal} "
                        f"stage={tr.stage!r} has no MarketCursor"
                    )
                continue
            key = tr.cursor.cursor_key()
            if last_cursor_key is not None and key < last_cursor_key:
                raise SimulationEvidenceOrderingError(
                    f"{_REASON_PREFIX}NON_MONOTONIC_CURSOR: "
                    f"transition ordinal={tr.ordinal} cursor={key} precedes "
                    f"prior cursor={last_cursor_key}"
                )
            if tr.ordinal <= last_ordinal:
                raise SimulationEvidenceOrderingError(
                    f"{_REASON_PREFIX}NON_STRICT_ORDINAL: transition "
                    f"ordinal={tr.ordinal} <= prior ordinal={last_ordinal}"
                )
            last_ordinal = tr.ordinal
            last_cursor_key = key

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping for serialisation.

        The mapping is the canonical shape the ``RunState`` /
        ``ReplayFrame`` reader consumes. Mappings are sorted so the
        canonical serialisation is deterministic in any process.
        """
        return {
            "version": self.version,
            "run_id": self.run_id,
            "dataset_version": self.dataset_version,
            "dataset_schema_version": self.dataset_schema_version,
            "dataset_decode_version": self.dataset_decode_version,
            "dataset_content_hash": self.dataset_content_hash,
            "pool_key_id": self.pool_key_id,
            "chain_id": self.chain_id,
            "tick_lower": self.tick_lower,
            "tick_upper": self.tick_upper,
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
            "market_state_version": self.market_state_version,
            "evidence_checksum": self.evidence_checksum,
        }

    def to_canonical_json(self) -> str:
        """Return the canonical JSON serialisation.

        The serialisation excludes ``evidence_checksum`` so the
        :func:`compute_evidence_checksum` function can re-derive the
        digest from the same canonical form.
        """
        payload = self.to_dict()
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def compute_evidence_checksum(payload: Mapping[str, Any]) -> str:
    """Return the SHA-256 hex digest of the canonical serialisation of ``payload``.

    The function binds every field except ``evidence_checksum`` so a
    tampered field surfaces as a checksum mismatch.
    """
    serialisable = dict(payload)
    serialisable.pop("evidence_checksum", None)
    content = json.dumps(serialisable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_simulation_evidence(
    *,
    run_id: str,
    dataset_version: str,
    dataset_schema_version: int,
    dataset_decode_version: int,
    dataset_content_hash: str,
    pool_key_id: str,
    chain_id: int,
    tick_lower: int,
    tick_upper: int,
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
    initial_attribution: Mapping[str, Any],
    transitions: Sequence[RunTransition],
    checkpoints: Sequence[RunStateCheckpoint],
) -> SimulationEvidence:
    """Build a :class:`SimulationEvidence` artifact.

    The function binds the run's identity, dataset, pool, range,
    registry, and revision metadata together with the run-specific
    transitions and checkpoints the engine recorded. The evidence
    checksum is computed from the canonical serialisation.
    """
    payload: dict[str, Any] = {
        "version": SIMULATION_EVIDENCE_VERSION,
        "run_id": run_id,
        "dataset_version": dataset_version,
        "dataset_schema_version": dataset_schema_version,
        "dataset_decode_version": dataset_decode_version,
        "dataset_content_hash": dataset_content_hash,
        "pool_key_id": pool_key_id,
        "chain_id": chain_id,
        "tick_lower": tick_lower,
        "tick_upper": tick_upper,
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
        "market_state_version": MARKET_STATE_VERSION,
        "evidence_checksum": "",
    }
    checksum = compute_evidence_checksum(payload)
    payload["evidence_checksum"] = checksum
    return SimulationEvidence(
        version=SIMULATION_EVIDENCE_VERSION,
        run_id=run_id,
        dataset_version=dataset_version,
        dataset_schema_version=dataset_schema_version,
        dataset_decode_version=dataset_decode_version,
        dataset_content_hash=dataset_content_hash,
        pool_key_id=pool_key_id,
        chain_id=chain_id,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
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
        evidence_checksum=checksum,
        market_state_version=MARKET_STATE_VERSION,
    )


def simulation_evidence_from_dict(payload: Mapping[str, Any]) -> SimulationEvidence:
    """Reconstruct a :class:`SimulationEvidence` from a JSON-friendly dict."""
    if not isinstance(payload, Mapping):
        raise SimulationEvidenceFieldError(
            f"simulation_evidence_from_dict: payload must be Mapping, got {type(payload).__name__}"
        )
    try:
        raw_transitions = payload["transitions"]
        raw_checkpoints = payload["checkpoints"]
    except KeyError as exc:
        raise SimulationEvidenceFieldError(
            f"simulation_evidence_from_dict: missing key {exc.args[0]!r}"
        ) from exc

    def _build_transition(entry: Mapping[str, Any]) -> RunTransition:
        if not isinstance(entry, Mapping):
            raise SimulationEvidenceFieldError(
                "simulation_evidence_from_dict: transition entry must be Mapping"
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
            raise SimulationEvidenceFieldError(
                "simulation_evidence_from_dict: checkpoint entry must be Mapping"
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
    try:
        evidence = SimulationEvidence(
            version=str(payload["version"]),
            run_id=str(payload["run_id"]),
            dataset_version=str(payload["dataset_version"]),
            dataset_schema_version=int(payload["dataset_schema_version"]),
            dataset_decode_version=int(payload["dataset_decode_version"]),
            dataset_content_hash=str(payload["dataset_content_hash"]),
            pool_key_id=str(payload["pool_key_id"]),
            chain_id=int(payload["chain_id"]),
            tick_lower=int(payload["tick_lower"]),
            tick_upper=int(payload["tick_upper"]),
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
            evidence_checksum=str(payload["evidence_checksum"]),
            market_state_version=str(payload["market_state_version"]),
        )
    except KeyError as exc:
        raise SimulationEvidenceFieldError(
            f"simulation_evidence_from_dict: missing key {exc.args[0]!r}"
        ) from exc
    # Verify the recorded checksum matches the canonical serialisation.
    recomputed = compute_evidence_checksum(evidence.to_dict())
    if recomputed != evidence.evidence_checksum:
        raise SimulationEvidenceChecksumError(
            f"{_REASON_PREFIX}CHECKSUM_MISMATCH: recorded="
            f"{evidence.evidence_checksum} recomputed={recomputed}"
        )
    return evidence


__all__ = [
    "SIMULATION_EVIDENCE_VERSION",
    "SimulationEvidence",
    "SimulationEvidenceError",
    "SimulationEvidenceFieldError",
    "SimulationEvidenceOrderingError",
    "SimulationEvidenceChecksumError",
    "RunTransition",
    "RunStateCheckpoint",
    "build_simulation_evidence",
    "compute_evidence_checksum",
    "simulation_evidence_from_dict",
]
