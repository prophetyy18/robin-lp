"""Research-universe membership registry (T026).

This module owns the research-universe collection: an arbitrary set
of ``(chain_id, PoolKey)`` members, each carrying its own block
range and its own support level. The collection is **disjoint** from
the single active execution pool: a research member holds no
assets, produces no transactions, and grants no execution
authority (ADR-014, T026 acceptance).

T026 acceptance (todo/phases/P02-chain-access-and-discovery/T026.md):

- Research-universe membership: an arbitrary set of
  ``(chain_id, PoolKey)`` members, each carrying its own block
  range and its own support level, kept distinct from the single
  active execution pool and never conflated with it.
- A registry query surface answering which pools are members, on
  which entry path they were added, and whether a pool is an
  execution candidate, a research member, or both.

T026 must-not (research-universe half):

- merge research membership into the active execution pool;
- let a research-universe entry grant, or be readable as granting,
  any execution authority.

This module sits in the storage layer per ADR-006 and depends on
``robinhood_lp.protocol``. It must not import ``robinhood_lp.config``
or higher layers. The configuration-side counterpart
(:class:`robinhood_lp.config.models.ResearchMemberConfig`) lives in
the config layer and is parsed independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from robinhood_lp.discovery.onboarding import OnboardingPath
from robinhood_lp.protocol import ChainId, PoolId, PoolKey, RunMode

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ResearchUniverseError(RuntimeError):
    """Base class for research-universe failures."""


class DuplicateResearchMemberError(ResearchUniverseError):
    """A pool identity was added to the research universe twice.

    The duplicate is the same ``(chain_id, PoolKey)`` pair; the
    second add raises (T026 acceptance: a member list containing a
    duplicate is rejected with a named reason). The contract is
    fail-closed: the second ``add`` does not silently merge or
    overwrite the existing record.
    """


class UnknownResearchMemberError(ResearchUniverseError):
    """A research-universe lookup missed."""


class EmptyBlockRangeError(ResearchUniverseError):
    """An inverted or zero-length block range was supplied."""


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


class PoolRole(StrEnum):
    """Closed set of pool roles (T026 acceptance, ADR-014).

    The roles are independent and a pool may hold several at once
    (for example, an execution candidate that is also a research
    member). The execution candidate role comes from the
    configuration's single active pool; the research-member role
    comes from this module. Neither role is auto-promoted by the
    other.
    """

    #: The pool is the single user-selected active execution pool
    #: named in ``RootConfig.pools``. The role is read-only here:
    #: the framework never promotes a research member to this role.
    EXECUTION_CANDIDATE = "execution_candidate"
    #: The pool is a member of the research universe; it holds no
    #: assets, produces no transactions and grants no execution
    #: authority.
    RESEARCH_MEMBER = "research_member"


# ---------------------------------------------------------------------------
# Research member
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResearchMember:
    """One ``(chain_id, PoolKey)`` row of the research universe.

    The record carries the block range the research window covers
    for this member, the support level assigned to the member at
    registration time, the entry path that added it, and an optional
    operator note. The block range is inclusive on both ends and
    ``None`` end is reserved for "open-ended" semantics (left
    unmodelled here; the configuration-side field is also ``None``
    to mark "no upper bound").
    """

    chain_id: int
    pool_key: PoolKey
    #: Inclusive start block. ``0`` means "from the chain's
    #: earliest block the framework can read".
    block_range_start: int
    #: Inclusive end block. ``None`` is the sentinel for
    #: "open-ended" and is rejected by the configuration-side
    #: validator; the runtime registry accepts ``None`` but
    #: surfaces it as a missing upper bound.
    block_range_end: int | None
    support_level: RunMode
    added_via: OnboardingPath
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise TypeError(
                f"ResearchMember.chain_id: must be int, got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise ValueError(f"ResearchMember.chain_id: must be positive, got {self.chain_id}")
        if not isinstance(self.pool_key, PoolKey):
            raise TypeError(
                f"ResearchMember.pool_key: must be PoolKey, got {type(self.pool_key).__name__}"
            )
        if (
            not isinstance(self.block_range_start, int)
            or isinstance(self.block_range_start, bool)
            or self.block_range_start < 0
        ):
            raise ValueError(
                f"ResearchMember.block_range_start: must be non-negative int, "
                f"got {self.block_range_start!r}"
            )
        if self.block_range_end is not None and (
            not isinstance(self.block_range_end, int)
            or isinstance(self.block_range_end, bool)
            or self.block_range_end < self.block_range_start
        ):
            raise ValueError(
                f"ResearchMember.block_range_end: must be int >= "
                f"block_range_start ({self.block_range_start}), "
                f"got {self.block_range_end!r}"
            )
        if not isinstance(self.support_level, RunMode):
            raise TypeError(
                f"ResearchMember.support_level: must be RunMode, "
                f"got {type(self.support_level).__name__}"
            )
        if not isinstance(self.added_via, OnboardingPath):
            raise TypeError(
                f"ResearchMember.added_via: must be OnboardingPath, "
                f"got {type(self.added_via).__name__}"
            )

    @property
    def pool_id(self) -> PoolId:
        """The canonical V4 ``PoolId`` derived from the member's ``PoolKey``."""
        return self.pool_key.to_pool_id()

    def to_dict(self) -> dict[str, object]:
        return {
            "chain_id": self.chain_id,
            "pool_id": self.pool_id.to_hex(),
            "currency0": self.pool_key.currency0.to_address().to_hex(),
            "currency1": self.pool_key.currency1.to_address().to_hex(),
            "fee": self.pool_key.fee,
            "tick_spacing": self.pool_key.tick_spacing,
            "hooks": self.pool_key.hooks.to_hex(),
            "block_range_start": self.block_range_start,
            "block_range_end": self.block_range_end,
            "support_level": self.support_level.value,
            "added_via": self.added_via.value,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Research universe
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ResearchUniverse:
    """In-memory collection of :class:`ResearchMember` rows.

    The collection is keyed by ``(chain_id, pool_id)`` and refuses
    duplicate identities (T026 acceptance: a member list containing
    a duplicate is rejected). The collection is intentionally
    disjoint from the :class:`PoolRegistry`: a research member may
    not be added to the active execution pool automatically, and an
    active execution pool is never implicitly added to the research
    universe.

    The runtime class is **deliberately minimal**: it stores and
    queries; the configuration-side validator (in
    :mod:`robinhood_lp.config.models`) is the authoritative source
    of what the user submitted. Two ``ResearchUniverse`` instances
    built from byte-identical configurations are byte-identical.
    """

    chain_id: ChainId
    _members: dict[PoolId, ResearchMember] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise TypeError(
                f"ResearchUniverse.chain_id must be ChainId, got {type(self.chain_id).__name__}"
            )

    # -- introspection --------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._members)

    @property
    def is_empty(self) -> bool:
        return len(self._members) == 0

    def all_members(self) -> list[ResearchMember]:
        """Return a fresh list of every member (insertion order)."""
        return list(self._members.values())

    # -- membership operations ------------------------------------------

    def add(self, member: ResearchMember) -> ResearchMember:
        """Insert a new member; duplicates raise :class:`DuplicateResearchMemberError`."""
        if not isinstance(member, ResearchMember):
            raise TypeError(
                f"ResearchUniverse.add: member must be ResearchMember, got {type(member).__name__}"
            )
        if member.chain_id != self.chain_id.value:
            raise ResearchUniverseError(
                f"ResearchUniverse.add: member.chain_id={member.chain_id} "
                f"does not match this universe's chain_id={self.chain_id.value}"
            )
        pid = member.pool_id
        if pid in self._members:
            existing = self._members[pid]
            raise DuplicateResearchMemberError(
                f"PoolId {pid.to_hex()} already present in research universe "
                f"(added via {existing.added_via.value}, support_level="
                f"{existing.support_level.value}); duplicates are rejected"
            )
        self._members[pid] = member
        return member

    def is_member(self, pool_id: PoolId) -> bool:
        """``True`` iff ``pool_id`` is a research member of this universe."""
        if not isinstance(pool_id, PoolId):
            raise TypeError(
                f"ResearchUniverse.is_member: pool_id must be PoolId, got {type(pool_id).__name__}"
            )
        return pool_id in self._members

    def is_member_by_key(self, pool_key: PoolKey) -> bool:
        """``True`` iff ``pool_key`` resolves to a research member."""
        if not isinstance(pool_key, PoolKey):
            raise TypeError(
                f"ResearchUniverse.is_member_by_key: pool_key must be PoolKey, "
                f"got {type(pool_key).__name__}"
            )
        return pool_key.to_pool_id() in self._members

    def get(self, pool_id: PoolId) -> ResearchMember:
        """Return the member for ``pool_id`` or raise :class:`UnknownResearchMemberError`."""
        if not isinstance(pool_id, PoolId):
            raise TypeError(
                f"ResearchUniverse.get: pool_id must be PoolId, got {type(pool_id).__name__}"
            )
        try:
            return self._members[pool_id]
        except KeyError as exc:
            raise UnknownResearchMemberError(
                f"PoolId {pool_id.to_hex()} is not a research member of "
                f"chain_id={self.chain_id.value}"
            ) from exc

    def try_get(self, pool_id: PoolId) -> ResearchMember | None:
        """Return the member for ``pool_id`` or ``None``."""
        if not isinstance(pool_id, PoolId):
            raise TypeError(
                f"ResearchUniverse.try_get: pool_id must be PoolId, got {type(pool_id).__name__}"
            )
        return self._members.get(pool_id)

    def remove(self, pool_id: PoolId) -> ResearchMember:
        """Remove a member; raise :class:`UnknownResearchMemberError` if absent."""
        if not isinstance(pool_id, PoolId):
            raise TypeError(
                f"ResearchUniverse.remove: pool_id must be PoolId, got {type(pool_id).__name__}"
            )
        try:
            return self._members.pop(pool_id)
        except KeyError as exc:
            raise UnknownResearchMemberError(
                f"PoolId {pool_id.to_hex()} is not a research member of "
                f"chain_id={self.chain_id.value}"
            ) from exc

    # -- set operations -------------------------------------------------

    def intersection_pool_ids(self, pool_ids: Iterable[PoolId]) -> list[PoolId]:
        """Return the subset of ``pool_ids`` that are research members."""
        return [pid for pid in pool_ids if pid in self._members]

    def difference_pool_ids(self, pool_ids: Iterable[PoolId]) -> list[PoolId]:
        """Return the subset of ``pool_ids`` that are NOT research members."""
        return [pid for pid in pool_ids if pid not in self._members]


# ---------------------------------------------------------------------------
# Query surface
# ---------------------------------------------------------------------------


#: Default support level for a research member at registration time.
#: The classifier (T027) may later raise or lower it on its own
#: evidence; the value recorded here is the level the user supplied
#: when the member was added to the configuration.
DEFAULT_RESEARCH_MEMBER_SUPPORT_LEVEL: Final[RunMode] = RunMode.INGESTION


@dataclass(frozen=True, slots=True)
class PoolMembershipView:
    """A read-only view of one pool's research / execution roles.

    A pool can hold several roles at once (for example, an active
    execution candidate that is also a research member). The view
    is the query surface answer to ``"is this pool an execution
    candidate, a research member, or both?"`` (T026 acceptance).

    The view is immutable; rebuild it whenever the underlying
    inputs change.
    """

    chain_id: int
    pool_id: PoolId
    roles: frozenset[PoolRole]

    @property
    def is_execution_candidate(self) -> bool:
        return PoolRole.EXECUTION_CANDIDATE in self.roles

    @property
    def is_research_member(self) -> bool:
        return PoolRole.RESEARCH_MEMBER in self.roles

    def has_role(self, role: PoolRole) -> bool:
        return role in self.roles

    def to_dict(self) -> dict[str, object]:
        return {
            "chain_id": self.chain_id,
            "pool_id": self.pool_id.to_hex(),
            "roles": sorted(r.value for r in self.roles),
        }


def build_membership_view(
    *,
    chain_id: int,
    pool_id: PoolId,
    research_universe: ResearchUniverse | None,
    execution_candidate_pool_id: PoolId | None,
) -> PoolMembershipView:
    """Build a :class:`PoolMembershipView` for one ``(chain_id, PoolId)``.

    ``execution_candidate_pool_id`` is the single active pool named
    in the execution configuration (``RootConfig.pools[0].pool_key``);
    the function treats this as the only execution candidate for the
    chain. ``research_universe`` is the disjoint research collection;
    the function never promotes a research member into the
    execution candidate role (T026 must-not).
    """
    if not isinstance(chain_id, int) or isinstance(chain_id, bool):
        raise TypeError(
            f"build_membership_view: chain_id must be int, got {type(chain_id).__name__}"
        )
    if chain_id <= 0:
        raise ValueError(f"build_membership_view: chain_id must be positive, got {chain_id}")
    if not isinstance(pool_id, PoolId):
        raise TypeError(
            f"build_membership_view: pool_id must be PoolId, got {type(pool_id).__name__}"
        )
    roles: set[PoolRole] = set()
    if research_universe is not None:
        if research_universe.chain_id.value != chain_id:
            raise ValueError(
                f"build_membership_view: research_universe.chain_id="
                f"{research_universe.chain_id.value} does not match "
                f"chain_id={chain_id}"
            )
        if research_universe.is_member(pool_id):
            roles.add(PoolRole.RESEARCH_MEMBER)
    if (
        execution_candidate_pool_id is not None
        and execution_candidate_pool_id.value == pool_id.value
    ):
        roles.add(PoolRole.EXECUTION_CANDIDATE)
    return PoolMembershipView(
        chain_id=chain_id,
        pool_id=pool_id,
        roles=frozenset(roles),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_research_member(
    *,
    chain_id: int,
    pool_key: PoolKey,
    block_range_start: int,
    block_range_end: int | None,
    support_level: RunMode | None,
    added_via: OnboardingPath,
    notes: str,
) -> ResearchMember:
    """Validate and build a :class:`ResearchMember` from raw inputs.

    Used by the configuration layer's validator; kept here so the
    runtime invariants live next to the record they describe.
    """
    if block_range_end is not None and block_range_end < block_range_start:
        raise EmptyBlockRangeError(
            f"research member block_range_end={block_range_end} is less "
            f"than block_range_start={block_range_start}"
        )
    effective_level = (
        support_level if support_level is not None else DEFAULT_RESEARCH_MEMBER_SUPPORT_LEVEL
    )
    return ResearchMember(
        chain_id=chain_id,
        pool_key=pool_key,
        block_range_start=block_range_start,
        block_range_end=block_range_end,
        support_level=effective_level,
        added_via=added_via,
        notes=notes,
    )


__all__ = [
    "DEFAULT_RESEARCH_MEMBER_SUPPORT_LEVEL",
    "DuplicateResearchMemberError",
    "EmptyBlockRangeError",
    "PoolMembershipView",
    "PoolRole",
    "ResearchMember",
    "ResearchUniverse",
    "ResearchUniverseError",
    "UnknownResearchMemberError",
    "build_membership_view",
]


# Sentinel import for the type checker.
from collections.abc import Iterable  # noqa: E402
