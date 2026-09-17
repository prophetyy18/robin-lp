"""Reorg handling for the P03 ingestion pipeline (T033).

T033 owns the finality policy and reorg handling for the qualified
endpoints used by ingestion. The package exposes four collaborating
modules:

- :mod:`robinhood_lp.storage.reorg.policy` — the configurable
  finality policy whose formal boundary is the RPC ``finalized`` tag,
  the reason-code vocabulary, the critical-incident sentinel, and the
  exception types the rest of the package raises;
- :mod:`robinhood_lp.storage.reorg.ancestor` — deterministic
  common-ancestor search, an in-memory canonical chain view, and the
  block-header record used to drive orphan marking/replacement;
- :mod:`robinhood_lp.storage.reorg.journal` — an append-only reorg
  journal and an append-only critical-incident recorder that wrap the
  shared SQLite manifest store;
- :mod:`robinhood_lp.storage.reorg.handler` — the :class:`ReorgHandler`
  that ties the policy, the chain view, and the journal together to
  resolve shallow reorgs, halt on deep reorgs, escalate on
  :data:`FINALIZED_ANCESTRY_VIOLATION` critical incidents, and never
  rewrite finalized raw evidence.

The contract forbids equating ``confirmations`` with finality across
chains. ``confirmations = 12`` is retained here only as an
``EXPERIMENTAL_NOT_LIVE_APPROVED`` placeholder for fixture references
and legacy configuration values; it must never underpin mainnet
qualification, paper trading, or live promotion. Tests must inject
the finality policy explicitly — no test depends on an implicit
production default.
"""

from __future__ import annotations

from robinhood_lp.storage.reorg.ancestor import (
    COMMON_ANCESTOR_UNREACHABLE,
    BlockHeader,
    CanonicalChainView,
    find_common_ancestor,
)
from robinhood_lp.storage.reorg.handler import (
    DECISION_BLOCK_BACKTEST,
    DECISION_BLOCK_LIVE,
    DECISION_BLOCK_PAPER,
    DECISION_CONTINUE,
    DECISION_HALT_CRITICAL_INCIDENT,
    DECISION_HALT_DEEP_REORG,
    DECISION_HALT_FINALIZED_UNAVAILABLE,
    ReorgDecision,
    ReorgHandler,
)
from robinhood_lp.storage.reorg.journal import (
    CriticalIncident,
    CriticalIncidentRecorder,
    ReorgJournal,
    ReorgJournalEntry,
)
from robinhood_lp.storage.reorg.policy import (
    EXPERIMENTAL_CONFIRMATIONS_PLACEHOLDER,
    EXPERIMENTAL_NOT_LIVE_APPROVED,
    FINALITY_TAG_UNAVAILABLE,
    FINALIZED_ANCESTRY_DISAGREEMENT,
    FINALIZED_ANCESTRY_VIOLATION,
    FINALIZED_UNAVAILABLE,
    REASON_DEEP_REORG,
    REASON_ORPHAN_REAPPEARANCE,
    REASON_PROVIDER_DISAGREEMENT,
    REASON_REMOVED_LOG,
    REASON_SHALLOW_REORG,
    FinalityPolicy,
    FinalityPolicyError,
    ReorgError,
    UnfinalizedWindow,
    evaluate_finality_boundary,
)

__all__ = [
    "COMMON_ANCESTOR_UNREACHABLE",
    "DECISION_BLOCK_BACKTEST",
    "DECISION_BLOCK_LIVE",
    "DECISION_BLOCK_PAPER",
    "DECISION_CONTINUE",
    "DECISION_HALT_CRITICAL_INCIDENT",
    "DECISION_HALT_DEEP_REORG",
    "DECISION_HALT_FINALIZED_UNAVAILABLE",
    "EXPERIMENTAL_CONFIRMATIONS_PLACEHOLDER",
    "EXPERIMENTAL_NOT_LIVE_APPROVED",
    "FINALITY_TAG_UNAVAILABLE",
    "FINALIZED_ANCESTRY_DISAGREEMENT",
    "FINALIZED_ANCESTRY_VIOLATION",
    "FINALIZED_UNAVAILABLE",
    "BlockHeader",
    "CanonicalChainView",
    "CriticalIncident",
    "CriticalIncidentRecorder",
    "FinalityPolicy",
    "FinalityPolicyError",
    "ReorgDecision",
    "ReorgError",
    "ReorgHandler",
    "ReorgJournal",
    "ReorgJournalEntry",
    "UnfinalizedWindow",
    "evaluate_finality_boundary",
    "find_common_ancestor",
    "REASON_DEEP_REORG",
    "REASON_ORPHAN_REAPPEARANCE",
    "REASON_PROVIDER_DISAGREEMENT",
    "REASON_REMOVED_LOG",
    "REASON_SHALLOW_REORG",
]
