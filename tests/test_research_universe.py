"""Tests for the T026 research-universe collection.

Coverage matrix (T026 acceptance):

- The research universe is a separate collection, disjoint from
  the single active execution pool.
- Membership is keyed by ``(chain_id, PoolId)``; a duplicate is
  rejected at add time with a named reason.
- An empty research universe is a valid configuration.
- A member carries its own block range and its own support level;
  two members sharing a currency but differing in fee, tick
  spacing, or hooks remain distinct entries.
- The query surface answers which pools are members and on which
  entry path they were added.
- The query surface answers whether a pool is an execution
  candidate, a research member, or both.

T026 must-not (research-universe half):

- merge research membership into the active execution pool;
- let a research-universe entry grant, or be readable as granting,
  any execution authority.
"""

from __future__ import annotations

import pytest

from robinhood_lp.discovery import (
    DEFAULT_RESEARCH_MEMBER_SUPPORT_LEVEL,
    DuplicateResearchMemberError,
    OnboardingPath,
    PoolMembershipView,
    PoolRole,
    ResearchMember,
    ResearchUniverse,
    ResearchUniverseError,
    UnknownResearchMemberError,
    build_membership_view,
)
from robinhood_lp.protocol import Address, ChainId, Currency, PoolKey, RunMode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


CHAIN_ID = 46630


PK_A = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=3000,
    tick_spacing=60,
    hooks=Address.zero(),
)
PK_B = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=500,  # same currencies, different fee
    tick_spacing=60,
    hooks=Address.zero(),
)
PK_C = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=3000,
    tick_spacing=200,  # different tick spacing
    hooks=Address.zero(),
)
PK_D = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=3000,
    tick_spacing=60,
    hooks=Address(1 << 7),  # BEFORE_SWAP flag
)


def _make_member(
    pk: PoolKey = PK_A,
    *,
    chain_id: int = CHAIN_ID,
    block_range_start: int = 0,
    block_range_end: int | None = 1_000_000,
    support_level: RunMode = DEFAULT_RESEARCH_MEMBER_SUPPORT_LEVEL,
    added_via: OnboardingPath = OnboardingPath.POOL_KEY,
    notes: str = "",
) -> ResearchMember:
    return ResearchMember(
        chain_id=chain_id,
        pool_key=pk,
        block_range_start=block_range_start,
        block_range_end=block_range_end,
        support_level=support_level,
        added_via=added_via,
        notes=notes,
    )


def _universe(chain_id: int = CHAIN_ID) -> ResearchUniverse:
    return ResearchUniverse(chain_id=ChainId(chain_id))


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_research_universe_construction_accepts_chain_id() -> None:
    u = _universe()
    assert u.chain_id == ChainId(CHAIN_ID)
    assert u.size == 0
    assert u.is_empty is True


def test_research_universe_rejects_non_chain_id() -> None:
    with pytest.raises(TypeError, match="ChainId"):
        ResearchUniverse(chain_id="not-a-chain-id")  # type: ignore[arg-type]


def test_research_universe_empty_does_not_contain_any_pool() -> None:
    u = _universe()
    assert u.is_member(PK_A.to_pool_id()) is False
    assert u.is_member_by_key(PK_A) is False
    assert u.try_get(PK_A.to_pool_id()) is None
    with pytest.raises(UnknownResearchMemberError):
        u.get(PK_A.to_pool_id())
    assert u.all_members() == []


# ---------------------------------------------------------------------------
# Add / duplicate
# ---------------------------------------------------------------------------


def test_add_single_member() -> None:
    u = _universe()
    member = _make_member()
    out = u.add(member)
    assert out is member
    assert u.size == 1
    assert u.is_empty is False
    assert u.is_member(member.pool_id) is True
    assert u.is_member_by_key(PK_A) is True


def test_add_two_distinct_members() -> None:
    u = _universe()
    u.add(_make_member(PK_A))
    u.add(_make_member(PK_B))
    assert u.size == 2
    assert u.is_member(PK_A.to_pool_id()) is True
    assert u.is_member(PK_B.to_pool_id()) is True


def test_two_members_sharing_currency_but_different_fee_or_tick_or_hook_are_distinct() -> None:
    u = _universe()
    u.add(_make_member(PK_A))
    u.add(_make_member(PK_B))  # same currencies, fee differs
    u.add(_make_member(PK_C))  # same currencies, tick spacing differs
    u.add(_make_member(PK_D))  # same currencies, hooks differ
    assert u.size == 4
    members = u.all_members()
    pool_ids = {m.pool_id for m in members}
    assert len(pool_ids) == 4


def test_add_duplicate_member_raises() -> None:
    u = _universe()
    u.add(_make_member(PK_A))
    with pytest.raises(DuplicateResearchMemberError) as excinfo:
        u.add(_make_member(PK_A))
    assert "duplicate" in str(excinfo.value).lower()
    assert u.size == 1
    # The original member is preserved (no overwrite).
    only = u.get(PK_A.to_pool_id())
    assert only is not None


def test_add_rejects_wrong_chain_id() -> None:
    u = _universe()
    with pytest.raises(ResearchUniverseError, match="chain_id"):
        u.add(_make_member(chain_id=1))  # not CHAIN_ID


def test_add_rejects_non_member_input() -> None:
    u = _universe()
    with pytest.raises(TypeError, match="ResearchMember"):
        u.add("not-a-member")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Lookup / removal
# ---------------------------------------------------------------------------


def test_get_returns_member() -> None:
    u = _universe()
    m = _make_member(PK_A)
    u.add(m)
    assert u.get(m.pool_id) is m


def test_get_raises_when_absent() -> None:
    u = _universe()
    with pytest.raises(UnknownResearchMemberError):
        u.get(PK_A.to_pool_id())


def test_get_rejects_non_pool_id() -> None:
    u = _universe()
    with pytest.raises(TypeError, match="PoolId"):
        u.get("not-a-pool-id")  # type: ignore[arg-type]


def test_try_get_returns_member_or_none() -> None:
    u = _universe()
    m = _make_member(PK_A)
    u.add(m)
    assert u.try_get(m.pool_id) is m
    assert u.try_get(PK_B.to_pool_id()) is None
    with pytest.raises(TypeError, match="PoolId"):
        u.try_get("not-a-pool-id")  # type: ignore[arg-type]


def test_remove_drops_a_member() -> None:
    u = _universe()
    m = _make_member(PK_A)
    u.add(m)
    removed = u.remove(m.pool_id)
    assert removed is m
    assert u.size == 0
    assert u.is_member(m.pool_id) is False


def test_remove_raises_when_absent() -> None:
    u = _universe()
    with pytest.raises(UnknownResearchMemberError):
        u.remove(PK_A.to_pool_id())


def test_remove_rejects_non_pool_id() -> None:
    u = _universe()
    with pytest.raises(TypeError, match="PoolId"):
        u.remove("not-a-pool-id")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Set operations
# ---------------------------------------------------------------------------


def test_intersection_pool_ids() -> None:
    u = _universe()
    u.add(_make_member(PK_A))
    u.add(_make_member(PK_B))
    intersect = u.intersection_pool_ids([PK_A.to_pool_id(), PK_C.to_pool_id(), PK_B.to_pool_id()])
    assert intersect == [PK_A.to_pool_id(), PK_B.to_pool_id()]


def test_difference_pool_ids() -> None:
    u = _universe()
    u.add(_make_member(PK_A))
    diff = u.difference_pool_ids([PK_A.to_pool_id(), PK_B.to_pool_id()])
    assert diff == [PK_B.to_pool_id()]


# ---------------------------------------------------------------------------
# ResearchMember validation
# ---------------------------------------------------------------------------


def test_member_rejects_negative_chain_id() -> None:
    with pytest.raises(ValueError, match="positive"):
        ResearchMember(
            chain_id=-1,
            pool_key=PK_A,
            block_range_start=0,
            block_range_end=100,
            support_level=RunMode.INGESTION,
            added_via=OnboardingPath.POOL_KEY,
        )


def test_member_rejects_negative_block_range_start() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        ResearchMember(
            chain_id=CHAIN_ID,
            pool_key=PK_A,
            block_range_start=-1,
            block_range_end=100,
            support_level=RunMode.INGESTION,
            added_via=OnboardingPath.POOL_KEY,
        )


def test_member_rejects_inverted_block_range() -> None:
    with pytest.raises(ValueError, match="block_range_end"):
        ResearchMember(
            chain_id=CHAIN_ID,
            pool_key=PK_A,
            block_range_start=100,
            block_range_end=50,
            support_level=RunMode.INGESTION,
            added_via=OnboardingPath.POOL_KEY,
        )


def test_member_accepts_open_ended_block_range() -> None:
    """``block_range_end=None`` is the sentinel for "no upper bound";
    the runtime member accepts it but records the open end."""
    m = ResearchMember(
        chain_id=CHAIN_ID,
        pool_key=PK_A,
        block_range_start=0,
        block_range_end=None,
        support_level=RunMode.INGESTION,
        added_via=OnboardingPath.POOL_KEY,
    )
    assert m.block_range_end is None


def test_member_pool_id_derived_from_pool_key() -> None:
    m = _make_member(PK_A)
    assert m.pool_id == PK_A.to_pool_id()


def test_member_to_dict_has_canonical_fields() -> None:
    m = _make_member(PK_A, notes="a note")
    d = m.to_dict()
    assert d["chain_id"] == CHAIN_ID
    assert d["pool_id"] == PK_A.to_pool_id().to_hex()
    assert d["currency0"] == "0x" + "0" * 38 + "10"
    assert d["fee"] == 3000
    assert d["support_level"] == RunMode.INGESTION.value
    assert d["added_via"] == OnboardingPath.POOL_KEY.value
    assert d["notes"] == "a note"


# ---------------------------------------------------------------------------
# Query surface (membership view)
# ---------------------------------------------------------------------------


def test_membership_view_no_roles_when_pool_is_unknown() -> None:
    universe = _universe()
    pool_id = PK_A.to_pool_id()
    view = build_membership_view(
        chain_id=CHAIN_ID,
        pool_id=pool_id,
        research_universe=universe,
        execution_candidate_pool_id=None,
    )
    assert isinstance(view, PoolMembershipView)
    assert view.is_execution_candidate is False
    assert view.is_research_member is False
    assert view.roles == frozenset()


def test_membership_view_research_member_role() -> None:
    universe = _universe()
    member = _make_member(PK_A)
    universe.add(member)
    view = build_membership_view(
        chain_id=CHAIN_ID,
        pool_id=member.pool_id,
        research_universe=universe,
        execution_candidate_pool_id=None,
    )
    assert view.is_research_member is True
    assert view.is_execution_candidate is False


def test_membership_view_execution_candidate_role() -> None:
    universe = _universe()
    pool_id = PK_A.to_pool_id()
    view = build_membership_view(
        chain_id=CHAIN_ID,
        pool_id=pool_id,
        research_universe=universe,
        execution_candidate_pool_id=pool_id,
    )
    assert view.is_execution_candidate is True
    assert view.is_research_member is False


def test_membership_view_pool_can_be_both_research_and_execution() -> None:
    """The query surface must support the case where a pool is both an
    active execution candidate AND a research member (T026 acceptance:
    whether a pool is an execution candidate, a research member, or both)."""
    universe = _universe()
    member = _make_member(PK_A)
    universe.add(member)
    view = build_membership_view(
        chain_id=CHAIN_ID,
        pool_id=member.pool_id,
        research_universe=universe,
        execution_candidate_pool_id=member.pool_id,
    )
    assert view.is_execution_candidate is True
    assert view.is_research_member is True
    assert view.roles == frozenset({PoolRole.EXECUTION_CANDIDATE, PoolRole.RESEARCH_MEMBER})


def test_membership_view_rejects_chain_id_mismatch_with_universe() -> None:
    universe = _universe(chain_id=CHAIN_ID)
    with pytest.raises(ValueError, match="chain_id"):
        build_membership_view(
            chain_id=1,  # wrong
            pool_id=PK_A.to_pool_id(),
            research_universe=universe,
            execution_candidate_pool_id=None,
        )


def test_membership_view_rejects_non_positive_chain_id() -> None:
    with pytest.raises(ValueError, match="positive"):
        build_membership_view(
            chain_id=-1,
            pool_id=PK_A.to_pool_id(),
            research_universe=None,
            execution_candidate_pool_id=None,
        )


def test_membership_view_rejects_non_pool_id() -> None:
    with pytest.raises(TypeError, match="PoolId"):
        build_membership_view(
            chain_id=CHAIN_ID,
            pool_id="not-a-pool-id",  # type: ignore[arg-type]
            research_universe=None,
            execution_candidate_pool_id=None,
        )


def test_membership_view_no_research_universe_means_no_research_role() -> None:
    """``research_universe=None`` is the "research universe is empty
    or not supplied" path; the view returns no research role."""
    view = build_membership_view(
        chain_id=CHAIN_ID,
        pool_id=PK_A.to_pool_id(),
        research_universe=None,
        execution_candidate_pool_id=None,
    )
    assert view.is_research_member is False


def test_membership_view_has_role_helper() -> None:
    universe = _universe()
    member = _make_member(PK_A)
    universe.add(member)
    view = build_membership_view(
        chain_id=CHAIN_ID,
        pool_id=member.pool_id,
        research_universe=universe,
        execution_candidate_pool_id=None,
    )
    assert view.has_role(PoolRole.RESEARCH_MEMBER) is True
    assert view.has_role(PoolRole.EXECUTION_CANDIDATE) is False


def test_membership_view_to_dict_sorts_roles() -> None:
    universe = _universe()
    member = _make_member(PK_A)
    universe.add(member)
    view = build_membership_view(
        chain_id=CHAIN_ID,
        pool_id=member.pool_id,
        research_universe=universe,
        execution_candidate_pool_id=member.pool_id,
    )
    d = view.to_dict()
    assert d["roles"] == ["execution_candidate", "research_member"]


# ---------------------------------------------------------------------------
# Default support level and entry paths
# ---------------------------------------------------------------------------


def test_default_support_level_is_ingestion() -> None:
    assert DEFAULT_RESEARCH_MEMBER_SUPPORT_LEVEL == RunMode.INGESTION


def test_member_records_added_via_path() -> None:
    """The entry path that added a member is recorded on the row;
    multiple paths are not allowed at registration time (the
    configuration validator enforces single entry path)."""
    m_pool_id = _make_member(PK_A, added_via=OnboardingPath.POOL_ID)
    m_target = _make_member(PK_B, added_via=OnboardingPath.TARGET_TOKEN)
    assert m_pool_id.added_via == OnboardingPath.POOL_ID
    assert m_target.added_via == OnboardingPath.TARGET_TOKEN


def test_member_rejects_non_runmode_support_level() -> None:
    with pytest.raises(TypeError, match="RunMode"):
        ResearchMember(
            chain_id=CHAIN_ID,
            pool_key=PK_A,
            block_range_start=0,
            block_range_end=100,
            support_level="not-a-runmode",  # type: ignore[arg-type]
            added_via=OnboardingPath.POOL_KEY,
        )


def test_member_rejects_non_onboarding_path() -> None:
    with pytest.raises(TypeError, match="OnboardingPath"):
        ResearchMember(
            chain_id=CHAIN_ID,
            pool_key=PK_A,
            block_range_start=0,
            block_range_end=100,
            support_level=RunMode.INGESTION,
            added_via="not-an-onboarding-path",  # type: ignore[arg-type]
        )
