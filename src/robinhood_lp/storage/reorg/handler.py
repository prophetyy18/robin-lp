"""Reorg handler that ties the policy, the chain view, and the journal (T033).

The handler is the entry point the rest of the P03 ingestion pipeline
calls when a new chain tip is observed. It owns:

- :class:`ReorgDecision` — the typed verdict returned to the caller;
- :meth:`ReorgHandler.evaluate_boundary` — the fail-closed evaluation
  of the qualified-endpoint finalized headers (uses
  :func:`evaluate_finality_boundary`);
- :meth:`ReorgHandler.observe_new_tip` — the reorg-handling entry
  point. The handler performs common-ancestor search, records orphan
  markers in the journal, halts on deep reorgs, and escalates on
  :data:`FINALIZED_ANCESTRY_VIOLATION` critical incidents;
- :meth:`ReorgHandler.unfinalized_window` — the unfinalized hash
  window ``[finalized + 1, latest]`` the rest of the pipeline uses
  to discriminate observation from qualification.

The handler deliberately **does not** perform Phase 8 operator /
incident handling: T033 detects, emits the reason code, halts,
journals, and escalates. The human incident declaration and the
creation of a superseding canonical generation are owned by Phase 8.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Literal

from robinhood_lp.storage.reorg.ancestor import (
    COMMON_ANCESTOR_UNREACHABLE,
    BlockHeader,
    CanonicalChainView,
    find_common_ancestor,
)
from robinhood_lp.storage.reorg.journal import (
    ReorgJournal,
)
from robinhood_lp.storage.reorg.policy import (
    FINALIZED_ANCESTRY_VIOLATION,
    REASON_DEEP_REORG,
    REASON_ORPHAN_REAPPEARANCE,
    REASON_REMOVED_LOG,
    REASON_SHALLOW_REORG,
    FinalityBoundaryObservation,
    FinalityDecision,
    FinalityPolicy,
    FinalityPolicyError,
    UnfinalizedWindow,
    evaluate_finality_boundary,
)

# ---------------------------------------------------------------------------
# Decision codes
# ---------------------------------------------------------------------------

#: Decision code: the new tip agrees with the canonical view and no
#: reorg is required. The caller continues normally.
DECISION_CONTINUE: Final[str] = "continue"

#: Decision code: a shallow reorg was resolved via orphan marking /
#: replacement. The caller continues but downstream queries against
#: the demoted blocks must use the new fork.
DECISION_HALT_SHALLOW_REORG: Final[str] = "halt_shallow_reorg"  # noqa: F841 - exported via all

#: Decision code: a deep reorg exceeded the policy threshold.
#: Qualification halts; new backtest / paper / live decisions are
#: blocked until the operator recovers (Phase 8).
DECISION_HALT_DEEP_REORG: Final[str] = "halt_deep_reorg"

#: Decision code: the qualified endpoints disagreed on the finalized
#: boundary. The handler records a reorg journal row with
#: :data:`REASON_PROVIDER_DISAGREEMENT` and halts qualification.
DECISION_HALT_PROVIDER_DISAGREEMENT: Final[str] = "halt_provider_disagreement"  # noqa: F841

#: Decision code: the finalized tag was unavailable (transport error,
#: timeout, or the endpoint does not implement the tag). The
#: handler halts and refuses to fall back to a fixed confirmations
#: count.
DECISION_HALT_FINALIZED_UNAVAILABLE: Final[str] = "halt_finalized_unavailable"

#: Decision code: a :data:`FINALIZED_ANCESTRY_VIOLATION` was detected
#: (a block previously marked finalized under the then-effective
#: policy later fell out of the finalized ancestry, or qualified
#: endpoints persistently disagree on the finalized block hash /
#: ancestry). The handler records an append-only critical-incident
#: candidate and halts.
DECISION_HALT_CRITICAL_INCIDENT: Final[str] = "halt_critical_incident"

#: Decision code: a previously orphaned block reappeared in a new
#: observation. The handler reconciles the journal without rewriting
#: prior raw evidence and emits a new append-only evidence entry.
DECISION_RECONCILE_ORPHAN_REAPPEARANCE: Final[str] = "reconcile_orphan_reappearance"  # noqa: F841

#: Decision code: a log was previously observed and is now marked
#: ``removed``. The handler records an orphan marker and preserves
#: the original raw evidence.
DECISION_RECONCILE_REMOVED_LOG: Final[str] = "reconcile_removed_log"  # noqa: F841

#: Decision gate: the decision blocks new backtest qualification
#: (T061 / T062 / T063 / T064 / T065 / T066) until the operator
#: recovers.
DECISION_BLOCK_BACKTEST: Final[str] = "block_backtest"

#: Decision gate: the decision blocks new paper trading decisions
#: (T071).
DECISION_BLOCK_PAPER: Final[str] = "block_paper"

#: Decision gate: the decision blocks new live decisions (T091 /
#: T092 / T095). The V1 acceptance condition (G-LIVE-01) cannot
#: advance until the operator recovers.
DECISION_BLOCK_LIVE: Final[str] = "block_live"


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------


DecisionKind = Literal[
    "continue",
    "halt_deep_reorg",
    "halt_provider_disagreement",
    "halt_finalized_unavailable",
    "halt_critical_incident",
    "reconcile_orphan_reappearance",
    "reconcile_removed_log",
]


@dataclass(frozen=True, slots=True)
class ReorgDecision:
    """The typed verdict returned by :meth:`ReorgHandler.observe_new_tip`.

    ``kind`` selects the qualitative outcome; ``reason_code`` is the
    code the manifest records (``None`` when ``kind`` is
    ``"continue"``); ``orphan_journal_entries`` lists the rows the
    journal appended during this evaluation; ``critical_incident_id``
    is set when ``kind == "halt_critical_incident"`` and points at the
    appended-only row the recorder created; ``gates`` lists which
    decision gates are blocked (``block_backtest`` /
    ``block_paper`` / ``block_live``).
    """

    kind: DecisionKind
    reason_code: str | None
    common_ancestor_block_number: int | None
    orphan_depth: int
    orphan_journal_entries: tuple[dict[str, int], ...]
    critical_incident_id: int | None
    gates: tuple[str, ...] = field(default_factory=tuple)
    details: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualifiedBoundaryProbe:
    """One endpoint's finalized-tag probe result used by the handler."""

    endpoint_alias: str
    block_number: int | None
    block_hash: str | None
    parent_hash: str | None
    tag_supported: bool = True
    error: str | None = None

    def to_observation(self) -> FinalityBoundaryObservation:
        return FinalityBoundaryObservation(
            endpoint_alias=self.endpoint_alias,
            block_number=self.block_number,
            block_hash=self.block_hash,
            parent_hash=self.parent_hash,
            tag_supported=self.tag_supported,
            error=self.error,
        )


def _block_gates(policy: FinalityPolicy, kind: DecisionKind) -> tuple[str, ...]:
    """Return the decision gates the caller must block on this verdict.

    Backtest is gated by the policy flag; paper and live are always
    gated. ``continue`` returns no gates.
    """
    if kind == "continue":
        return ()
    if kind == "halt_critical_incident":
        return (DECISION_BLOCK_BACKTEST, DECISION_BLOCK_PAPER, DECISION_BLOCK_LIVE)
    gates: list[str] = []
    if policy.deep_reorg_halt_qualifies_backtest:
        gates.append(DECISION_BLOCK_BACKTEST)
    if kind in (
        "halt_deep_reorg",
        "halt_provider_disagreement",
        "halt_finalized_unavailable",
    ):
        gates.append(DECISION_BLOCK_PAPER)
        gates.append(DECISION_BLOCK_LIVE)
    return tuple(gates)


class ReorgHandler:
    """The T033 reorg handler.

    Construction requires an explicit :class:`FinalityPolicy` and an
    explicit :class:`ReorgJournal`. The handler never falls back to
    an implicit policy.
    """

    def __init__(
        self,
        *,
        policy: FinalityPolicy,
        journal: ReorgJournal,
        canonical: CanonicalChainView | None = None,
    ) -> None:
        if not isinstance(policy, FinalityPolicy):
            raise FinalityPolicyError(
                f"ReorgHandler: policy must be FinalityPolicy, got {type(policy).__name__}"
            )
        if not isinstance(journal, ReorgJournal):
            raise FinalityPolicyError(
                f"ReorgHandler: journal must be ReorgJournal, got {type(journal).__name__}"
            )
        self._policy = policy
        self._journal = journal
        self._canonical: CanonicalChainView = (
            canonical if canonical is not None else CanonicalChainView()
        )

    # ----- accessors -----------------------------------------------------

    @property
    def policy(self) -> FinalityPolicy:
        return self._policy

    @property
    def canonical(self) -> CanonicalChainView:
        return self._canonical

    @property
    def journal(self) -> ReorgJournal:
        return self._journal

    # ----- finality boundary evaluation ---------------------------------

    def evaluate_boundary(self, probes: Iterable[QualifiedBoundaryProbe]) -> FinalityDecision:
        observations: dict[str, FinalityBoundaryObservation] = {}
        for probe in probes:
            if not isinstance(probe, QualifiedBoundaryProbe):
                raise FinalityPolicyError(
                    "ReorgHandler.evaluate_boundary: every probe must be "
                    f"QualifiedBoundaryProbe, got {type(probe).__name__}"
                )
            observations[probe.endpoint_alias] = probe.to_observation()
        return evaluate_finality_boundary(self._policy, observations)

    # ----- unfinalized hash window --------------------------------------

    def unfinalized_window(
        self,
        *,
        finalized_block_number: int,
        latest_block_number: int,
    ) -> UnfinalizedWindow:
        """Return the unfinalized hash window ``[finalized + 1, latest]``.

        When ``latest_block_number`` is more than
        ``policy.unfinalized_window_lag_blocks`` ahead of
        ``finalized_block_number`` the window is clamped to
        ``finalized + lag`` so the handler cannot be tricked into
        processing an arbitrarily large observation buffer.
        """
        if finalized_block_number < 0:
            raise FinalityPolicyError(
                f"ReorgHandler.unfinalized_window: finalized_block_number "
                f"must be >= 0, got {finalized_block_number}"
            )
        if latest_block_number < finalized_block_number:
            raise FinalityPolicyError(
                "ReorgHandler.unfinalized_window: latest_block_number must be "
                f">= finalized_block_number, got {latest_block_number} < {finalized_block_number}"
            )
        cap = finalized_block_number + self._policy.unfinalized_window_lag_blocks
        clamped_latest = min(latest_block_number, cap)
        return UnfinalizedWindow(
            finalized_block_number=finalized_block_number,
            latest_block_number=clamped_latest,
        )

    # ----- new tip observation -----------------------------------------

    def observe_new_tip(
        self,
        *,
        chain_id: int,
        observed_headers: Iterable[BlockHeader],
        finalized_block_number: int,
        finalized_block_hash: str,
        partition_id: str | None = None,
    ) -> ReorgDecision:
        """Reconcile a newly observed chain tip with the canonical view.

        The flow is:

        1. Append the observed headers into a fresh in-memory view.
           The handler does not mutate the canonical view until it
           has decided the reorg is shallow.
        2. Locate the deepest common ancestor (depth-bounded by
           ``deep_reorg_threshold_blocks``).
        3. If the common ancestor is unreachable the handler records
           a deep-reorg journal entry, halts, and returns
           :data:`DECISION_HALT_DEEP_REORG`.
        4. If the common ancestor equals the canonical tip and the
           observed tip agrees with the canonical tip the handler
           returns ``continue``.
        5. Otherwise the handler records an orphan marker per block
           in the canonical view above the common ancestor (shallow
           reorg) and returns ``continue`` with the journal entries.
        6. The handler never re-writes finalized raw evidence; the
           canonical view is appended to only after the reorg has
           been journaled.
        """
        if not isinstance(chain_id, int) or isinstance(chain_id, bool):
            raise FinalityPolicyError(
                f"ReorgHandler.observe_new_tip: chain_id must be int, got {type(chain_id).__name__}"
            )
        if finalized_block_number < 0:
            raise FinalityPolicyError(
                f"ReorgHandler.observe_new_tip: finalized_block_number must be >= 0, "
                f"got {finalized_block_number}"
            )

        observed_view = CanonicalChainView()
        observed_tip: int | None = None
        for header in observed_headers:
            parent_number = header.block_number - 1
            if parent_number < 0:
                parent_number_int: int | None = None
            else:
                parent_number_int = parent_number
            observed_view.add_header(header, parent_number=parent_number_int)
            if observed_tip is None or header.block_number > observed_tip:
                observed_tip = header.block_number
        if observed_tip is None:
            raise FinalityPolicyError(
                "ReorgHandler.observe_new_tip: observed_headers must contain at least one header"
            )

        canonical_tip = self._canonical.canonical_tip()
        if canonical_tip is None:
            # No canonical view yet: there is no common ancestor to
            # reason about. The handler appends the observed chain
            # and returns ``continue`` — the policy forbids deleting
            # raw evidence, but a brand-new chain has no raw evidence
            # to delete.
            self._absorb_observed(observed_view, observed_tip)
            return ReorgDecision(
                kind="continue",
                reason_code=None,
                common_ancestor_block_number=None,
                orphan_depth=0,
                orphan_journal_entries=(),
                critical_incident_id=None,
            )

        max_depth = self._policy.deep_reorg_threshold_blocks
        common_ancestor = find_common_ancestor(
            self._canonical,
            observed_view,
            canonical_tip=canonical_tip,
            observed_tip=observed_tip,
            max_depth=max_depth,
        )
        if common_ancestor == COMMON_ANCESTOR_UNREACHABLE:
            return self._halt_deep_reorg(
                chain_id=chain_id,
                canonical_tip=canonical_tip,
                observed_tip=observed_tip,
                partition_id=partition_id,
                finalized_block_number=finalized_block_number,
                finalized_block_hash=finalized_block_hash,
            )

        orphan_depth = canonical_tip - common_ancestor
        if orphan_depth >= max_depth:
            return self._halt_deep_reorg(
                chain_id=chain_id,
                canonical_tip=canonical_tip,
                observed_tip=observed_tip,
                partition_id=partition_id,
                finalized_block_number=finalized_block_number,
                finalized_block_hash=finalized_block_hash,
                common_ancestor=common_ancestor,
            )

        # Shallow reorg or no reorg at all. Record orphan markers
        # for canonical blocks above the common ancestor.
        journal_entries: list[dict[str, int]] = []
        for block_number in range(common_ancestor + 1, canonical_tip + 1):
            orphan_header = self._canonical.header_at(block_number)
            if orphan_header is None:
                continue
            observed_replacement = observed_view.header_at(block_number)
            replacement_hash = (
                observed_replacement.block_hash
                if observed_replacement is not None
                and observed_replacement.block_hash != orphan_header.block_hash
                else None
            )
            entry = self._journal.record_orphan(
                chain_id=chain_id,
                block_number=block_number,
                orphan_block_hash=orphan_header.block_hash,
                replacement_block_hash=replacement_hash,
                demotion_reason=REASON_SHALLOW_REORG,
                partition_id=partition_id,
            )
            journal_entries.append({"id": entry.id, "block_number": entry.block_number})

        # Mark the canonical view as demoted for the orphan range.
        # The handler never mutates finalized raw evidence; it only
        # appends new headers on top of the canonical view.
        self._absorb_observed(observed_view, observed_tip, lower_exclusive=common_ancestor + 1)

        if not journal_entries:
            return ReorgDecision(
                kind="continue",
                reason_code=None,
                common_ancestor_block_number=common_ancestor,
                orphan_depth=0,
                orphan_journal_entries=(),
                critical_incident_id=None,
            )
        return ReorgDecision(
            kind="continue",
            reason_code=REASON_SHALLOW_REORG,
            common_ancestor_block_number=common_ancestor,
            orphan_depth=orphan_depth,
            orphan_journal_entries=tuple(journal_entries),
            critical_incident_id=None,
        )

    # ----- finalized-ancestry violation ---------------------------------

    def detect_finalized_ancestry_violation(
        self,
        *,
        chain_id: int,
        finalized_block_number: int,
        finalized_block_hash: str,
        endpoint_alias: str,
        detail: dict[str, str] | None = None,
    ) -> ReorgDecision:
        """Record a :data:`FINALIZED_ANCESTRY_VIOLATION` critical incident.

        The handler never decides whether the violation is real; the
        caller hands it the evidence (a block previously marked
        finalized under the then-effective policy that later fell out
        of the finalized ancestry, or qualified endpoints that
        persistently disagree on the finalized block hash / ancestry).
        The handler records the append-only candidate and returns
        the halt decision. Operator declaration, re-checking, and
        recovery approval are owned by Phase 8.
        """
        incident = self._journal.record_critical_incident_candidate(
            chain_id=chain_id,
            incident_kind=FINALIZED_ANCESTRY_VIOLATION,
            reason_code=FINALIZED_ANCESTRY_VIOLATION,
            endpoint_alias=endpoint_alias,
            block_number=finalized_block_number,
            block_hash=finalized_block_hash,
            parent_block_hash=None,
            detail={"evidence": "finalized_ancestry_violation", **(detail or {})},
        )
        return ReorgDecision(
            kind="halt_critical_incident",
            reason_code=FINALIZED_ANCESTRY_VIOLATION,
            common_ancestor_block_number=None,
            orphan_depth=0,
            orphan_journal_entries=(),
            critical_incident_id=incident.id,
            gates=_block_gates(self._policy, "halt_critical_incident"),
            details={"endpoint_alias": endpoint_alias},
        )

    # ----- finalized-boundary verdict wrapper ---------------------------

    def apply_finality_decision(self, decision: FinalityDecision) -> ReorgDecision:
        """Translate a :class:`FinalityDecision` into a :class:`ReorgDecision`.

        ``agreed`` returns ``continue``; the other kinds return
        their respective halt decision with the appropriate gates
        set.
        """
        if decision.kind == "agreed":
            return ReorgDecision(
                kind="continue",
                reason_code=None,
                common_ancestor_block_number=None,
                orphan_depth=0,
                orphan_journal_entries=(),
                critical_incident_id=None,
            )
        if decision.kind == "disagreement":
            kind: DecisionKind = "halt_provider_disagreement"
        elif decision.kind == "unavailable" or decision.kind == "tag_unavailable":
            kind = "halt_finalized_unavailable"
        else:  # pragma: no cover - defensive
            raise FinalityPolicyError(
                f"ReorgHandler.apply_finality_decision: unknown kind {decision.kind!r}"
            )
        return ReorgDecision(
            kind=kind,
            reason_code=decision.reason_code,
            common_ancestor_block_number=None,
            orphan_depth=0,
            orphan_journal_entries=(),
            critical_incident_id=None,
            gates=_block_gates(self._policy, kind),
        )

    # ----- removed-log / orphan-reappearance helpers --------------------

    def record_removed_log(
        self,
        *,
        chain_id: int,
        block_number: int,
        orphan_block_hash: str,
        partition_id: str | None,
    ) -> ReorgDecision:
        """Demote a single log that was previously observed and is now ``removed``.

        The handler records an orphan marker with
        :data:`REASON_REMOVED_LOG` and preserves the original raw
        evidence (no bytes are deleted; only the journal entry is
        appended).
        """
        entry = self._journal.record_orphan(
            chain_id=chain_id,
            block_number=block_number,
            orphan_block_hash=orphan_block_hash,
            replacement_block_hash=None,
            demotion_reason=REASON_REMOVED_LOG,
            partition_id=partition_id,
        )
        return ReorgDecision(
            kind="reconcile_removed_log",
            reason_code=REASON_REMOVED_LOG,
            common_ancestor_block_number=None,
            orphan_depth=0,
            orphan_journal_entries=({"id": entry.id, "block_number": entry.block_number},),
            critical_incident_id=None,
        )

    def record_orphan_reappearance(
        self,
        *,
        chain_id: int,
        block_number: int,
        orphan_block_hash: str,
        replacement_block_hash: str,
        partition_id: str | None,
    ) -> ReorgDecision:
        """Reconcile an orphan block that reappeared on a new observation.

        The handler records a new orphan-replacement journal entry
        and **never** rewrites the prior raw evidence; the
        reappearance is recorded as a new append-only evidence row.
        """
        entry = self._journal.record_orphan(
            chain_id=chain_id,
            block_number=block_number,
            orphan_block_hash=orphan_block_hash,
            replacement_block_hash=replacement_block_hash,
            demotion_reason=REASON_ORPHAN_REAPPEARANCE,
            partition_id=partition_id,
        )
        return ReorgDecision(
            kind="reconcile_orphan_reappearance",
            reason_code=REASON_ORPHAN_REAPPEARANCE,
            common_ancestor_block_number=None,
            orphan_depth=0,
            orphan_journal_entries=({"id": entry.id, "block_number": entry.block_number},),
            critical_incident_id=None,
        )

    # ----- deep-reorg halt ---------------------------------------------

    def _halt_deep_reorg(
        self,
        *,
        chain_id: int,
        canonical_tip: int,
        observed_tip: int,
        partition_id: str | None,
        finalized_block_number: int,
        finalized_block_hash: str,
        common_ancestor: int | None = None,
    ) -> ReorgDecision:
        """Record a deep-reorg halt and return the typed verdict."""
        canonical_header = self._canonical.header_at(canonical_tip)
        orphan_hash = (
            canonical_header.block_hash if canonical_header is not None else "0x" + "00" * 32
        )
        entry = self._journal.record_orphan(
            chain_id=chain_id,
            block_number=canonical_tip,
            orphan_block_hash=orphan_hash,
            replacement_block_hash=None,
            demotion_reason=REASON_DEEP_REORG,
            partition_id=partition_id,
        )
        return ReorgDecision(
            kind="halt_deep_reorg",
            reason_code=REASON_DEEP_REORG,
            common_ancestor_block_number=common_ancestor,
            orphan_depth=max(
                0,
                canonical_tip - (common_ancestor if common_ancestor is not None else canonical_tip),
            ),
            orphan_journal_entries=({"id": entry.id, "block_number": entry.block_number},),
            critical_incident_id=None,
            gates=_block_gates(self._policy, "halt_deep_reorg"),
            details={
                "finalized_block_number": str(finalized_block_number),
                "finalized_block_hash": finalized_block_hash,
                "observed_tip": str(observed_tip),
            },
        )

    # ----- canonical view mutation --------------------------------------

    def _absorb_observed(
        self,
        observed_view: CanonicalChainView,
        observed_tip: int,
        *,
        lower_exclusive: int | None = None,
    ) -> None:
        """Append observed headers into the canonical view.

        ``lower_exclusive`` is the block number *below* the first
        header to absorb. The handler uses it after a shallow reorg
        to skip the unchanged portion of the canonical view.
        """
        if lower_exclusive is None:
            lower_exclusive = 0
        for block_number in range(lower_exclusive, observed_tip + 1):
            header = observed_view.header_at(block_number)
            if header is None:
                continue
            parent_number = header.block_number - 1
            if parent_number < 0:
                parent_number_int: int | None = None
            else:
                parent_number_int = parent_number
            existing = self._canonical.header_at(block_number)
            if existing is not None and existing.block_hash != header.block_hash:
                # Conflicting observation above the common ancestor:
                # the prior header stays in place; the reorg journal
                # already recorded the demotion. The handler refuses
                # to overwrite the prior canonical row.
                continue
            self._canonical.add_header(header, parent_number=parent_number_int)


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


__all__ = [
    "DECISION_BLOCK_BACKTEST",
    "DECISION_BLOCK_LIVE",
    "DECISION_BLOCK_PAPER",
    "DECISION_CONTINUE",
    "DECISION_HALT_CRITICAL_INCIDENT",
    "DECISION_HALT_DEEP_REORG",
    "DECISION_HALT_FINALIZED_UNAVAILABLE",
    "DECISION_HALT_PROVIDER_DISAGREEMENT",
    "DECISION_HALT_SHALLOW_REORG",
    "DECISION_RECONCILE_ORPHAN_REAPPEARANCE",
    "DECISION_RECONCILE_REMOVED_LOG",
    "DecisionKind",
    "QualifiedBoundaryProbe",
    "ReorgDecision",
    "ReorgHandler",
]
