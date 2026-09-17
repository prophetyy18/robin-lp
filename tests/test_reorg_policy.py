"""Tests for the configurable finality policy and reason codes (T033).

T033 acceptance requires the finality policy to be:

- explicitly injected into every consumer;
- pinned to the JSON-RPC ``finalized`` tag as the formal boundary;
- fail-closed on unavailability, fallback, error, or endpoint
  ancestry disagreement;
- never auto-replaced by a fixed ``confirmations = 12`` placebo.

This module covers each of those requirements in isolation; the
handler / ancestor / journal tests cover the integration paths.
"""

from __future__ import annotations

import pytest

from _reorg_t033_fixtures import make_finality_policy
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
    VALID_REORG_REASON_CODES,
    FinalityBoundaryObservation,
    FinalityPolicy,
    FinalityPolicyError,
    UnfinalizedWindow,
    evaluate_finality_boundary,
)

# ---------------------------------------------------------------------------
# Configurable finality policy — explicit injection
# ---------------------------------------------------------------------------


def test_finality_policy_explicit_injection_is_required() -> None:
    """The constructor rejects an unqualified endpoint list."""
    with pytest.raises(FinalityPolicyError, match="qualified_endpoint_aliases"):
        FinalityPolicy(
            finality_tag="finalized",
            qualified_endpoint_aliases=(),
            policy_id="bad",
        )


def test_finality_policy_finality_tag_must_be_finalized() -> None:
    """The contract pins the formal boundary to the ``finalized`` tag."""
    with pytest.raises(FinalityPolicyError, match="'finalized' tag"):
        FinalityPolicy(
            finality_tag="safe",
            qualified_endpoint_aliases=("robinhood_public",),
        )


def test_finality_policy_rejects_zero_deep_reorg_threshold() -> None:
    """A threshold < 1 would silently disable every reorg."""
    with pytest.raises(FinalityPolicyError, match="deep_reorg_threshold_blocks"):
        FinalityPolicy(
            finality_tag="finalized",
            qualified_endpoint_aliases=("robinhood_public",),
            deep_reorg_threshold_blocks=0,
        )


def test_finality_policy_rejects_non_positive_timeout() -> None:
    with pytest.raises(FinalityPolicyError, match="finalized_tag_request_timeout_seconds"):
        FinalityPolicy(
            finality_tag="finalized",
            qualified_endpoint_aliases=("robinhood_public",),
            finalized_tag_request_timeout_seconds=0.0,
        )


def test_finality_policy_rejects_negative_retry_limit() -> None:
    with pytest.raises(FinalityPolicyError, match="finalized_tag_retry_limit"):
        FinalityPolicy(
            finality_tag="finalized",
            qualified_endpoint_aliases=("robinhood_public",),
            finalized_tag_retry_limit=-1,
        )


def test_finality_policy_rejects_blank_alias() -> None:
    with pytest.raises(FinalityPolicyError, match="blank alias"):
        FinalityPolicy(
            finality_tag="finalized",
            qualified_endpoint_aliases=("robinhood_public", ""),
        )


def test_finality_policy_accepts_explicit_injection() -> None:
    """A well-formed policy round-trips its explicit fields."""
    policy = make_finality_policy()
    assert policy.finality_tag == "finalized"
    assert policy.qualified_endpoint_aliases == ("robinhood_public", "alchemy_free")
    assert policy.deep_reorg_threshold_blocks == 5
    assert policy.unfinalized_window_lag_blocks == 64
    assert policy.policy_id == "test-finality-policy"


# ---------------------------------------------------------------------------
# Experimental placeholder for confirmations = 12
# ---------------------------------------------------------------------------


def test_experimental_confirmations_placeholder_is_explicit() -> None:
    """The placeholder is a constant; the contract forbids deriving
    finality from a fixed confirmations count."""
    assert EXPERIMENTAL_CONFIRMATIONS_PLACEHOLDER == 12
    assert EXPERIMENTAL_NOT_LIVE_APPROVED == "EXPERIMENTAL_NOT_LIVE_APPROVED"
    # The placeholder is a fixture / legacy-config value, not a
    # production policy field. The policy refuses to consume it
    # because it has no ``confirmations`` parameter — the
    # constructor would raise if a future patch added one and tried
    # to use it as a finality substitute.
    policy = make_finality_policy()
    assert not hasattr(policy, "confirmations")
    assert policy.finality_tag == "finalized"


# ---------------------------------------------------------------------------
# Unfinalized hash window — finalized + 1 to latest
# ---------------------------------------------------------------------------


def test_unfinalized_window_lower_bound_is_finalized_plus_one() -> None:
    window = UnfinalizedWindow(finalized_block_number=10, latest_block_number=12)
    assert window.lower_bound == 11
    assert window.upper_bound == 12
    assert window.contains(11)
    assert window.contains(12)
    assert not window.contains(10)
    assert not window.contains(13)


def test_unfinalized_window_empty_when_tip_equals_finalized() -> None:
    window = UnfinalizedWindow(finalized_block_number=10, latest_block_number=10)
    assert window.is_empty()
    assert window.lower_bound == 11
    assert window.upper_bound == 10


def test_unfinalized_window_rejects_invalid_bounds() -> None:
    with pytest.raises(FinalityPolicyError, match="must be >= 0"):
        UnfinalizedWindow(finalized_block_number=-1, latest_block_number=5)
    with pytest.raises(FinalityPolicyError, match="latest_block_number"):
        UnfinalizedWindow(finalized_block_number=10, latest_block_number=9)


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------


def test_reason_code_vocabulary_is_complete() -> None:
    expected = {
        REASON_SHALLOW_REORG,
        REASON_DEEP_REORG,
        REASON_PROVIDER_DISAGREEMENT,
        REASON_REMOVED_LOG,
        REASON_ORPHAN_REAPPEARANCE,
        FINALIZED_UNAVAILABLE,
        FINALIZED_ANCESTRY_DISAGREEMENT,
        FINALITY_TAG_UNAVAILABLE,
        FINALIZED_ANCESTRY_VIOLATION,
    }
    assert expected == VALID_REORG_REASON_CODES


# ---------------------------------------------------------------------------
# Finality-boundary evaluation
# ---------------------------------------------------------------------------


def test_evaluate_boundary_agrees_when_qualified_endpoints_match() -> None:
    policy = make_finality_policy()
    observations = {
        "robinhood_public": FinalityBoundaryObservation(
            endpoint_alias="robinhood_public",
            block_number=100,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "bb" * 32,
        ),
        "alchemy_free": FinalityBoundaryObservation(
            endpoint_alias="alchemy_free",
            block_number=100,
            block_hash="0x" + "AA" * 32,  # mixed case — must be normalised
            parent_hash="0x" + "BB" * 32,
        ),
    }
    decision = evaluate_finality_boundary(policy, observations)
    assert decision.kind == "agreed"
    assert decision.reason_code is None
    assert decision.agreed_block is not None
    assert decision.agreed_block.block_number == 100


def test_evaluate_boundary_reports_disagreement_on_hash_mismatch() -> None:
    policy = make_finality_policy()
    observations = {
        "robinhood_public": FinalityBoundaryObservation(
            endpoint_alias="robinhood_public",
            block_number=100,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "bb" * 32,
        ),
        "alchemy_free": FinalityBoundaryObservation(
            endpoint_alias="alchemy_free",
            block_number=100,
            block_hash="0x" + "cc" * 32,
            parent_hash="0x" + "bb" * 32,
        ),
    }
    decision = evaluate_finality_boundary(policy, observations)
    assert decision.kind == "disagreement"
    assert decision.reason_code == FINALIZED_ANCESTRY_DISAGREEMENT


def test_evaluate_boundary_reports_disagreement_on_parent_hash_mismatch() -> None:
    policy = make_finality_policy()
    observations = {
        "robinhood_public": FinalityBoundaryObservation(
            endpoint_alias="robinhood_public",
            block_number=100,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "bb" * 32,
        ),
        "alchemy_free": FinalityBoundaryObservation(
            endpoint_alias="alchemy_free",
            block_number=100,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "dd" * 32,
        ),
    }
    decision = evaluate_finality_boundary(policy, observations)
    assert decision.kind == "disagreement"
    assert decision.reason_code == FINALIZED_ANCESTRY_DISAGREEMENT


def test_evaluate_boundary_reports_unavailable_when_endpoint_errors() -> None:
    policy = make_finality_policy()
    observations = {
        "robinhood_public": FinalityBoundaryObservation(
            endpoint_alias="robinhood_public",
            block_number=100,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "bb" * 32,
        ),
        "alchemy_free": FinalityBoundaryObservation(
            endpoint_alias="alchemy_free",
            block_number=None,
            block_hash=None,
            parent_hash=None,
            error="transport timeout",
        ),
    }
    decision = evaluate_finality_boundary(policy, observations)
    assert decision.kind == "unavailable"
    assert decision.reason_code == FINALIZED_UNAVAILABLE


def test_evaluate_boundary_reports_tag_unavailable() -> None:
    policy = make_finality_policy()
    observations = {
        "robinhood_public": FinalityBoundaryObservation(
            endpoint_alias="robinhood_public",
            block_number=100,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "bb" * 32,
        ),
        "alchemy_free": FinalityBoundaryObservation(
            endpoint_alias="alchemy_free",
            block_number=None,
            block_hash=None,
            parent_hash=None,
            tag_supported=False,
        ),
    }
    decision = evaluate_finality_boundary(policy, observations)
    assert decision.kind == "tag_unavailable"
    assert decision.reason_code == FINALITY_TAG_UNAVAILABLE


def test_evaluate_boundary_ignores_non_qualified_aliases() -> None:
    """Endpoints outside the qualified subset cannot veto agreement."""
    policy = make_finality_policy()
    observations = {
        "robinhood_public": FinalityBoundaryObservation(
            endpoint_alias="robinhood_public",
            block_number=100,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "bb" * 32,
        ),
        "alchemy_free": FinalityBoundaryObservation(
            endpoint_alias="alchemy_free",
            block_number=100,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "bb" * 32,
        ),
        "third_party": FinalityBoundaryObservation(
            endpoint_alias="third_party",
            block_number=999,
            block_hash="0x" + "cc" * 32,
            parent_hash="0x" + "dd" * 32,
        ),
    }
    decision = evaluate_finality_boundary(policy, observations)
    assert decision.kind == "agreed"


def test_evaluate_boundary_rejects_missing_observation() -> None:
    policy = make_finality_policy()
    with pytest.raises(FinalityPolicyError, match="missing observation"):
        evaluate_finality_boundary(
            policy,
            {
                "robinhood_public": FinalityBoundaryObservation(
                    endpoint_alias="robinhood_public",
                    block_number=100,
                    block_hash="0x" + "aa" * 32,
                    parent_hash="0x" + "bb" * 32,
                ),
            },
        )
