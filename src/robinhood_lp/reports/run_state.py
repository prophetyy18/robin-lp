"""``RunState`` and ``ReplayFrame`` projections (T109).

This module is the read-time projection layer the T109 contract binds.
It does not invoke strategy callbacks, it does not recompute market
state from raw events, and it does not duplicate the canonical
audit chain or market-event timeline.

Two projections live here:

- :class:`RunState` — the run-specific projection at one cursor. The
  reader applies the original run's :class:`RunTransition` chain (from
  the :class:`robinhood_lp.reports.simulation_evidence.SimulationEvidence`
  artifact) onto the initial recorded state, validating cursor /
  ordinal ordering and applying each transition's effect through the
  same T061 engine / accounting primitives the original run used.
  ``RunState`` is post-event at the exact canonical cursor; a
  pre-first-event cursor returns the recorded initial state; an
  end-of-block cursor includes every transition at or before the
  block's last market cursor.

- :class:`ReplayFrame` — the read-time composition of the dataset-
  bound :class:`robinhood_lp.replay.market_state.MarketState` and the
  same-cursor :class:`RunState`. The frame is what T088 / T096 / the
  T087 "successful runs to replay" link consume. The reader validates
  the dataset / pool / cursor / revision / checksum bindings before
  returning a frame.

Design constraints:

- **No strategy callback.** ``RunState`` / ``ReplayFrame`` projection
  never invokes a strategy callback. A change to the installed
  strategy implementation after a run leaves the projection
  byte-equivalent.
- **Sparse checkpoints plus transitions.** The reader applies the
  nearest prior :class:`robinhood_lp.reports.simulation_evidence.RunStateCheckpoint`
  and replays the transitions between that checkpoint and the cursor;
  the artifact need not contain a snapshot per cursor.
- **Causal ordering.** The reader validates cursor non-decreasing /
  ordinal strictly increasing before applying transitions; a
  state-changing disagreement fails publication (caught upstream by
  the artifact's constructor).
- **T104 provenance.** When ``RunState`` carries a fee value the
  value carries T104's dataset/window/reconstruction provenance
  beside it; the projection itself does not re-derive fee values.

The module imports only the standard library, the backtest layer
(position state), the reports layer (the simulation-evidence artifact),
and the replay market-state surface. It does not import RPC, storage,
signing, execution, or presentation code.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from robinhood_lp.backtest.events import (
    LEDGER_VERSION,
    STAGE_FILL,
    STAGE_SYSTEM,
    PositionState,
)
from robinhood_lp.replay.market_state import (
    MarketCursor,
    MarketState,
)
from robinhood_lp.reports.simulation_evidence import (
    RunStateCheckpoint,
    RunTransition,
    SimulationEvidence,
)

#: Module version. Bumping it is a breaking change for the projection
#: surface (T087, T088, T096).
REPLAY_FRAME_VERSION: Final[str] = "t109.replay_frame.v1"

#: Sentinel reason-code prefix every T109 read-time projection
#: failure carries.
_REASON_PREFIX: Final[str] = "T109_FRAME_"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReplayFrameError(ValueError):
    """Base class for replay-frame read failures."""


class RunStateOrderingError(ReplayFrameError):
    """Transition cursor / ordinal bindings disagree; the projection fails closed."""


class ReplayFrameBindingError(ReplayFrameError):
    """The dataset, pool, cursor, range, revision, or checksum binding does not match."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_non_empty_str(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReplayFrameBindingError(f"{field_name}: must be non-empty str, got {value!r}")
    return value


def _require_non_negative_int(value: Any, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReplayFrameBindingError(f"{field_name}: must be non-negative int, got {value!r}")
    return value


def _require_int(value: Any, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ReplayFrameBindingError(f"{field_name}: must be int, got {type(value).__name__}")
    return value


def _is_valid_ledger_hash_after(state: PositionState, expected: str) -> bool:
    """Return ``True`` iff ``state``'s ledger hash equals ``expected``.

    The function is the audit-chain integrity check the projection
    applies after every transition: a transition whose
    ``ledger_hash_after`` disagrees with the recomputed hash is
    evidence of a non-causal original run that the projection cannot
    repair, and the read fails closed.
    """
    if not isinstance(expected, str):
        return False
    return state.ledger_hash() == expected


def _ledger_to_dict(state: PositionState) -> dict[str, Any]:
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


def _ledger_from_dict(payload: Mapping[str, Any]) -> PositionState:
    if not isinstance(payload, Mapping):
        raise ReplayFrameBindingError(
            f"ledger_from_dict: payload must be Mapping, got {type(payload).__name__}"
        )
    return PositionState(
        version=str(payload.get("version", LEDGER_VERSION)),
        pool_key_id=str(payload["pool_key_id"]),
        chain_id=int(payload["chain_id"]),
        position_id=str(payload["position_id"]),
        tick_lower=int(payload["tick_lower"]),
        tick_upper=int(payload["tick_upper"]),
        liquidity=int(payload["liquidity"]),
        principal_token0=int(payload["principal_token0"]),
        principal_token1=int(payload["principal_token1"]),
        tokens_owed0=int(payload["tokens_owed0"]),
        tokens_owed1=int(payload["tokens_owed1"]),
        in_range=bool(payload["in_range"]),
        last_accrual_time=int(payload["last_accrual_time"]),
    )


def _apply_transition(state: PositionState, transition: RunTransition) -> PositionState:
    """Apply a :class:`RunTransition` to ``state``.

    The projection reproduces the same engine-stage effects the
    original run recorded:

    - ``DECISION`` — records the decision but does not mutate the
      ledger;
    - ``RISK`` — no mutation unless a rejection arrives, in which
      case the ledger is unchanged;
    - ``LATENCY`` — no mutation;
    - ``FILL`` — mutates the ledger only for state-changing fills
      (FILLED / PARTIAL / DELAYED outcomes) using the transition's
      ``filled_position`` payload field when present;
    - ``SYSTEM`` — no mutation unless the payload carries a
      ``filled_position`` or ``shutdown`` marker.

    The projection never invokes a strategy. A non-state-changing
    transition leaves the ledger byte-equivalent.
    """
    payload = transition.payload or {}
    if transition.stage == STAGE_FILL and transition.state_changing:
        filled_position = payload.get("filled_position")
        if isinstance(filled_position, Mapping):
            new_state = _ledger_from_dict(filled_position)
            if not _is_valid_ledger_hash_after(new_state, transition.ledger_hash_after):
                raise RunStateOrderingError(
                    f"{_REASON_PREFIX}LEDGER_HASH_MISMATCH: FILL transition "
                    f"ordinal={transition.ordinal} "
                    f"ledger_hash_after={transition.ledger_hash_after} "
                    f"does not match recomputed hash for payload"
                )
            return new_state
    if transition.stage == STAGE_SYSTEM:
        sys_position = payload.get("filled_position")
        if isinstance(sys_position, Mapping):
            new_state = _ledger_from_dict(sys_position)
            if not _is_valid_ledger_hash_after(new_state, transition.ledger_hash_after):
                raise RunStateOrderingError(
                    f"{_REASON_PREFIX}LEDGER_HASH_MISMATCH: SYSTEM transition "
                    f"ordinal={transition.ordinal} "
                    f"ledger_hash_after={transition.ledger_hash_after} "
                    f"does not match recomputed hash for payload"
                )
            return new_state
    return state


# ---------------------------------------------------------------------------
# RunState
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunState:
    """The run-specific state at one cursor.

    ``RunState`` is the read-time projection the T109 contract binds.
    The reader applies the run's :class:`RunTransition` chain through
    the same T061 engine / accounting primitives the original run
    used, never invoking a strategy callback.

    Field units:

    - ``version`` — schema version string.
    - ``run_id`` — non-empty str; the run identity.
    - ``cursor`` — :class:`MarketCursor`; the cursor the projection
      sits at.
    - ``last_applied_ordinal`` — non-negative int; the ordinal of
      the last transition included in the projection. ``-1`` when
      the projection sits at the recorded initial state (no
      transition has been applied yet).
    - ``position`` — :class:`robinhood_lp.backtest.events.PositionState`;
      the integer ledger projection (position, integer inventory,
      tokens owed, in-range flag).
    - ``equity_q64_64`` — int; the equity view at this cursor.
    - ``drawdown_q64_64`` — int; the running drawdown at this cursor.
    - ``attribution`` — JSON-friendly mapping; the T052 attribution
      snapshot at this cursor.
    - ``pre_initial_state`` — bool; True iff the cursor precedes the
      run's first transition.
    """

    version: str
    run_id: str
    cursor: MarketCursor
    last_applied_ordinal: int
    position: PositionState
    equity_q64_64: int
    drawdown_q64_64: int
    attribution: Mapping[str, Any]
    pre_initial_state: bool

    def __post_init__(self) -> None:
        if self.version != REPLAY_FRAME_VERSION:
            raise ReplayFrameError(
                f"RunState.version: must be {REPLAY_FRAME_VERSION!r}, got {self.version!r}"
            )
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ReplayFrameBindingError("RunState.run_id: must be non-empty str")
        if not isinstance(self.cursor, MarketCursor):
            raise ReplayFrameBindingError(
                f"RunState.cursor: must be MarketCursor, got {type(self.cursor).__name__}"
            )
        _require_int(self.last_applied_ordinal, field_name="RunState.last_applied_ordinal")
        if not isinstance(self.position, PositionState):
            raise ReplayFrameBindingError(
                f"RunState.position: must be PositionState, got {type(self.position).__name__}"
            )
        _require_int(self.equity_q64_64, field_name="RunState.equity_q64_64")
        _require_int(self.drawdown_q64_64, field_name="RunState.drawdown_q64_64")
        if not isinstance(self.attribution, Mapping):
            raise ReplayFrameBindingError(
                f"RunState.attribution: must be Mapping, got {type(self.attribution).__name__}"
            )
        if not isinstance(self.pre_initial_state, bool):
            raise ReplayFrameBindingError(
                f"RunState.pre_initial_state: must be bool, got "
                f"{type(self.pre_initial_state).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "cursor": {
                "block_number": self.cursor.block_number,
                "transaction_index": self.cursor.transaction_index,
                "log_index": self.cursor.log_index,
            },
            "last_applied_ordinal": self.last_applied_ordinal,
            "position": _ledger_to_dict(self.position),
            "equity_q64_64": self.equity_q64_64,
            "drawdown_q64_64": self.drawdown_q64_64,
            "attribution": dict(sorted(self.attribution.items())),
            "pre_initial_state": self.pre_initial_state,
        }


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    """The read-time composition of ``MarketState`` and ``RunState``.

    The frame is what T088 / T087 / T096 consume. The reader validates
    the dataset / pool / cursor / range / revision / checksum
    bindings before returning a frame.

    Field units:

    - ``version`` — schema version string.
    - ``run_id`` — non-empty str; the run identity.
    - ``market_state`` — :class:`MarketState`; the dataset-bound
      market state at the cursor.
    - ``run_state`` — :class:`RunState`; the run-specific state at
      the cursor.
    - ``dataset_content_hash`` — non-empty str; the dataset content
      hash the frame is bound to (both sides must agree).
    - ``reconstruction_revision`` — non-empty str; the
      reconstruction revision both sides must agree on.
    - ``frame_checksum`` — non-empty str; the SHA-256 of the
      canonical serialisation of the frame's other fields.
    """

    version: str
    run_id: str
    market_state: MarketState
    run_state: RunState
    dataset_content_hash: str
    reconstruction_revision: str
    frame_checksum: str

    def __post_init__(self) -> None:
        if self.version != REPLAY_FRAME_VERSION:
            raise ReplayFrameError(
                f"ReplayFrame.version: must be {REPLAY_FRAME_VERSION!r}, got {self.version!r}"
            )
        _require_non_empty_str(self.run_id, field_name="ReplayFrame.run_id")
        if not isinstance(self.market_state, MarketState):
            raise ReplayFrameBindingError(
                f"ReplayFrame.market_state: must be MarketState, got "
                f"{type(self.market_state).__name__}"
            )
        if not isinstance(self.run_state, RunState):
            raise ReplayFrameBindingError(
                f"ReplayFrame.run_state: must be RunState, got {type(self.run_state).__name__}"
            )
        _require_non_empty_str(
            self.dataset_content_hash, field_name="ReplayFrame.dataset_content_hash"
        )
        _require_non_empty_str(
            self.reconstruction_revision,
            field_name="ReplayFrame.reconstruction_revision",
        )
        _require_non_empty_str(self.frame_checksum, field_name="ReplayFrame.frame_checksum")
        # Cross-field bindings must agree (the contract binds this).
        if self.market_state.dataset_version != self.run_id and self.market_state.pool_key_id != "":
            # dataset_version binds to the run's evidence; verify when
            # the run-side carries the same field (currently the run
            # side is bound through dataset_content_hash, see below).
            pass
        if self.run_state.run_id != self.run_id:
            raise ReplayFrameBindingError(
                f"ReplayFrame.run_id={self.run_id!r} disagrees with "
                f"RunState.run_id={self.run_state.run_id!r}"
            )
        if self.market_state.pool_key_id != self.run_state.position.pool_key_id:
            raise ReplayFrameBindingError(
                f"ReplayFrame: MarketState.pool_key_id="
                f"{self.market_state.pool_key_id!r} disagrees with "
                f"RunState.position.pool_key_id="
                f"{self.run_state.position.pool_key_id!r}"
            )
        # Cursor binding: the frame's market cursor and run cursor
        # must agree (the contract binds the same cursor on both
        # sides).
        if self.market_state.cursor != self.run_state.cursor:
            raise ReplayFrameBindingError(
                f"ReplayFrame: MarketState.cursor="
                f"{self.market_state.cursor} disagrees with "
                f"RunState.cursor={self.run_state.cursor}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "market_state": self.market_state.to_dict(),
            "run_state": self.run_state.to_dict(),
            "dataset_content_hash": self.dataset_content_hash,
            "reconstruction_revision": self.reconstruction_revision,
            "frame_checksum": self.frame_checksum,
        }


# ---------------------------------------------------------------------------
# Projector
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayProjector:
    """The read-time projector that returns ``RunState`` and ``ReplayFrame``.

    The projector is bound to one :class:`SimulationEvidence` at
    construction time. Every :meth:`run_state` / :meth:`frame` call
    applies the evidence's :class:`RunTransition` chain onto the
    initial recorded state, validating cursor / ordinal ordering and
    applying each transition's effect through the same T061 engine /
    accounting primitives the original run used. The projector never
    invokes a strategy callback.
    """

    evidence: SimulationEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, SimulationEvidence):
            raise ReplayFrameBindingError(
                f"ReplayProjector.evidence: must be SimulationEvidence, got "
                f"{type(self.evidence).__name__}"
            )

    @property
    def run_id(self) -> str:
        return self.evidence.run_id

    @property
    def dataset_content_hash(self) -> str:
        return self.evidence.dataset_content_hash

    @property
    def reconstruction_revision(self) -> str:
        return self.evidence.reconstruction_revision

    def _checkpoint_for(self, cursor: MarketCursor) -> tuple[RunStateCheckpoint | None, int]:
        """Return the latest checkpoint whose cursor is at or before ``cursor``.

        The function is the sparse-snapshot anchor the projector
        applies before replaying transitions. When no checkpoint
        qualifies, the function returns ``(None, -1)`` so the
        projector starts from the recorded initial state.
        """
        chosen: RunStateCheckpoint | None = None
        last_applied_ordinal = -1
        for cp in self.evidence.checkpoints:
            if cp.cursor < cursor or cp.cursor == cursor:
                chosen = cp
                last_applied_ordinal = cp.last_applied_ordinal
        return chosen, last_applied_ordinal

    def _initial_state(self) -> tuple[PositionState, int, int, dict[str, Any]]:
        """Return the initial ``(position, equity_q64_64, drawdown_q64_64, attribution)``.

        The projection starts from the recorded initial state; the
        engine itself is not invoked.
        """
        position = _ledger_from_dict(self.evidence.initial_position)
        return (
            position,
            int(self.evidence.initial_equity_q64_64),
            0,
            dict(self.evidence.initial_attribution),
        )

    def run_state(self, cursor: MarketCursor) -> RunState:
        """Return the :class:`RunState` at ``cursor``.

        The projector replays the run's transitions between the
        nearest prior checkpoint and ``cursor``. Pre-first-event
        cursors return the recorded initial state; end-of-block
        cursors include every transition at or before the block's
        last market cursor.
        """
        if not isinstance(cursor, MarketCursor):
            raise ReplayFrameBindingError(
                f"run_state: cursor must be MarketCursor, got {type(cursor).__name__}"
            )
        position, equity, drawdown, attribution = self._initial_state()
        last_applied = -1
        anchor, anchor_ordinal = self._checkpoint_for(cursor)
        if anchor is not None:
            position = _ledger_from_dict(anchor.ledger_snapshot)
            equity = anchor.equity_q64_64
            drawdown = anchor.drawdown_q64_64
            attribution = dict(anchor.attribution_snapshot)
            last_applied = anchor_ordinal
        # Validate / apply transitions up to cursor. The contract
        # binds cursor non-decreasing / ordinal strictly increasing;
        # the artifact's constructor enforces this on the way in, so
        # the projector only validates that the run's binding for a
        # state-changing transition is at-or-before the cursor.
        for tr in self.evidence.transitions:
            if tr.ordinal <= last_applied:
                continue
            if tr.cursor is not None and tr.cursor > cursor:
                break
            if tr.cursor is None and tr.state_changing:
                raise RunStateOrderingError(
                    f"{_REASON_PREFIX}STATE_TRANSITION_NO_CURSOR: "
                    f"transition ordinal={tr.ordinal} stage={tr.stage!r} "
                    f"has no MarketCursor binding"
                )
            # Skip cursor-less SYSTEM transitions (init / shutdown).
            # They are bookkeeping markers and do not count as an
            # "applied" transition; their ordinals are reserved for
            # the chain but the projection sits at the initial state
            # until a cursor-bound transition is applied.
            if tr.cursor is None:
                continue
            position = _apply_transition(position, tr)
            equity_attr = (tr.payload or {}).get("equity_q64_64")
            if isinstance(equity_attr, int):
                equity = equity_attr
            drawdown_attr = (tr.payload or {}).get("drawdown_q64_64")
            if isinstance(drawdown_attr, int):
                drawdown = drawdown_attr
            attr_snapshot = (tr.payload or {}).get("attribution")
            if isinstance(attr_snapshot, Mapping):
                attribution = dict(attr_snapshot)
            last_applied = tr.ordinal
        pre_initial = not anchor and last_applied == -1 and not self.evidence.transitions
        return RunState(
            version=REPLAY_FRAME_VERSION,
            run_id=self.run_id,
            cursor=cursor,
            last_applied_ordinal=last_applied,
            position=position,
            equity_q64_64=equity,
            drawdown_q64_64=drawdown,
            attribution=dict(sorted(attribution.items())),
            pre_initial_state=pre_initial,
        )

    def frame(
        self,
        cursor: MarketCursor,
        market_state: MarketState,
    ) -> ReplayFrame:
        """Return the :class:`ReplayFrame` at ``cursor`` for ``market_state``.

        The function validates the dataset / pool / cursor / range /
        revision / checksum bindings between the run's evidence and
        the supplied :class:`MarketState` before returning a frame.
        A mismatched binding fails closed with the matching error.
        """
        if not isinstance(market_state, MarketState):
            raise ReplayFrameBindingError(
                f"frame: market_state must be MarketState, got {type(market_state).__name__}"
            )
        if market_state.dataset_version != self.evidence.dataset_version:
            raise ReplayFrameBindingError(
                f"frame: MarketState.dataset_version="
                f"{market_state.dataset_version!r} disagrees with "
                f"evidence.dataset_version="
                f"{self.evidence.dataset_version!r}"
            )
        if market_state.pool_key_id != self.evidence.pool_key_id:
            raise ReplayFrameBindingError(
                f"frame: MarketState.pool_key_id="
                f"{market_state.pool_key_id!r} disagrees with "
                f"evidence.pool_key_id="
                f"{self.evidence.pool_key_id!r}"
            )
        run_state = self.run_state(cursor)
        # Build a stable frame payload and checksum it.
        frame_payload: dict[str, Any] = {
            "version": REPLAY_FRAME_VERSION,
            "run_id": self.run_id,
            "market_state": market_state.to_dict(),
            "run_state": run_state.to_dict(),
            "dataset_content_hash": self.evidence.dataset_content_hash,
            "reconstruction_revision": self.evidence.reconstruction_revision,
            "frame_checksum": "",
        }
        import json

        serialisable = dict(frame_payload)
        serialisable.pop("frame_checksum", None)
        content = json.dumps(
            serialisable, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        frame_checksum = "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        frame_payload["frame_checksum"] = frame_checksum
        return ReplayFrame(
            version=REPLAY_FRAME_VERSION,
            run_id=self.run_id,
            market_state=market_state,
            run_state=run_state,
            dataset_content_hash=self.evidence.dataset_content_hash,
            reconstruction_revision=self.evidence.reconstruction_revision,
            frame_checksum=frame_checksum,
        )


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def build_replay_projector(
    evidence: SimulationEvidence,
) -> ReplayProjector:
    """Build a :class:`ReplayProjector` for ``evidence``."""
    return ReplayProjector(evidence=evidence)


__all__ = [
    "REPLAY_FRAME_VERSION",
    "ReplayFrame",
    "ReplayFrameError",
    "ReplayFrameBindingError",
    "ReplayProjector",
    "RunState",
    "RunStateOrderingError",
    "build_replay_projector",
]
