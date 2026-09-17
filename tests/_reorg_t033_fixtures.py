"""Shared fixtures for the T033 reorg test suite.

This module is not a test module; it exists so the T033 policy /
ancestor / journal / handler tests can construct typed objects with
identical structure. The module deliberately mirrors the shape of
``_storage_t031_fixtures.py`` so subsequent P03 tasks can re-use
the same construction conventions.
"""

from __future__ import annotations

from robinhood_lp.storage.reorg.policy import (
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
)

CHAIN_ID: int = 4663

# Qualified endpoint aliases the fixtures use. Tests that need a
# different set construct their own policy.
QUALIFIED_ALIASES: tuple[str, ...] = ("robinhood_public", "alchemy_free")


def make_finality_policy(
    *,
    qualified_endpoint_aliases: tuple[str, ...] = QUALIFIED_ALIASES,
    deep_reorg_threshold_blocks: int = 5,
    finalized_tag_request_timeout_seconds: float = 5.0,
    finalized_tag_retry_limit: int = 2,
    unfinalized_window_lag_blocks: int = 64,
    deep_reorg_halt_qualifies_backtest: bool = True,
    policy_id: str = "test-finality-policy",
) -> FinalityPolicy:
    """Return a FinalityPolicy with the values the T033 fixture suite uses.

    Tests explicitly inject the finality policy; production code must
    not depend on an implicit default.
    """
    return FinalityPolicy(
        finality_tag="finalized",
        qualified_endpoint_aliases=qualified_endpoint_aliases,
        finalized_tag_request_timeout_seconds=finalized_tag_request_timeout_seconds,
        finalized_tag_retry_limit=finalized_tag_retry_limit,
        deep_reorg_threshold_blocks=deep_reorg_threshold_blocks,
        unfinalized_window_lag_blocks=unfinalized_window_lag_blocks,
        deep_reorg_halt_qualifies_backtest=deep_reorg_halt_qualifies_backtest,
        policy_id=policy_id,
    )


#: Re-export the reason-code constants so tests can keep their
#: imports narrow.
REASONS = {
    "shallow_reorg": REASON_SHALLOW_REORG,
    "deep_reorg": REASON_DEEP_REORG,
    "provider_disagreement": REASON_PROVIDER_DISAGREEMENT,
    "removed_log": REASON_REMOVED_LOG,
    "orphan_reappearance": REASON_ORPHAN_REAPPEARANCE,
    "finalized_unavailable": FINALIZED_UNAVAILABLE,
    "finalized_ancestry_disagreement": FINALIZED_ANCESTRY_DISAGREEMENT,
    "finality_tag_unavailable": FINALITY_TAG_UNAVAILABLE,
    "FINALIZED_ANCESTRY_VIOLATION": FINALIZED_ANCESTRY_VIOLATION,
}


__all__ = [
    "CHAIN_ID",
    "QUALIFIED_ALIASES",
    "REASONS",
    "make_finality_policy",
]
