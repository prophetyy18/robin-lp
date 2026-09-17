"""Configurable finality policy and reason codes (T033).

T033 makes the finality boundary an explicit, injected object. The
boundary is the JSON-RPC ``finalized`` tag returned by the qualified
endpoint; it is **not** a confirmations count. Failures, fallbacks,
errors, and qualified-endpoint ancestry disagreements stop
qualification and fail closed.

This module owns:

- :class:`FinalityPolicy` — the policy object every consumer of T033
  must inject explicitly. Production code never falls back to an
  implicit default: tests construct the policy by hand;
- the reason-code vocabulary the manifest records (see
  :data:`REASON_SHALLOW_REORG`, :data:`FINALIZED_ANCESTRY_VIOLATION`,
  and the other siblings);
- :data:`EXPERIMENTAL_NOT_LIVE_APPROVED` /
  :data:`EXPERIMENTAL_CONFIRMATIONS_PLACEHOLDER` — the only
  acknowledgement of ``confirmations = 12`` the contract permits. The
  policy treats ``confirmations = 12`` as a
  :class:`FinalityPolicyError` unless the caller explicitly sets the
  ``EXPERIMENTAL_NOT_LIVE_APPROVED`` flag;
- :func:`evaluate_finality_boundary` — the deterministic helper the
  handler uses to compare qualified-endpoint finalized headers and
  produce a typed decision.

The policy is **append-only with respect to raw evidence**: it never
mutates finalized raw evidence and never auto-falls back to a fixed
confirmations count.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

# ---------------------------------------------------------------------------
# Reason codes — the only strings the reorg journal may record
# ---------------------------------------------------------------------------

#: Shallow reorg: orphan depth is below the policy threshold and
#: common-ancestor search resolved the demotion.
REASON_SHALLOW_REORG: Final[str] = "shallow_reorg"

#: Deep reorg: orphan depth exceeded the policy threshold; the
#: handler halts and records an append-only critical-incident
#: candidate.
REASON_DEEP_REORG: Final[str] = "deep_reorg"

#: Two qualified endpoints disagreed on the finalized block hash or
#: finalized ancestry. The handler halts with
#: :data:`FINALIZED_ANCESTRY_DISAGREEMENT`.
REASON_PROVIDER_DISAGREEMENT: Final[str] = "provider_disagreement"

#: A log that was once observed and later marked ``removed`` is
#: demoted via orphan marking; the original raw evidence is
#: preserved.
REASON_REMOVED_LOG: Final[str] = "removed_log"

#: An orphan block that reappears on a new observation is
#: reconciled against the reorg journal; the reconciliation is
#: recorded as a new append-only evidence entry, not an overwrite.
REASON_ORPHAN_REAPPEARANCE: Final[str] = "orphan_reappearance"

#: Qualified endpoints could not return the ``finalized`` block
#: header (timeout, error, tag not implemented). The handler
#: halts qualification and surfaces the failure.
FINALIZED_UNAVAILABLE: Final[str] = "finalized_unavailable"

#: Two qualified endpoints returned a finalized block hash / parent
#: chain that disagrees. The handler halts qualification.
FINALIZED_ANCESTRY_DISAGREEMENT: Final[str] = "finalized_ancestry_disagreement"

#: The endpoint reports it does not implement the ``finalized`` tag
#: (e.g. a legacy provider). The handler halts qualification; the
#: policy refuses to fall back to a fixed confirmations count.
FINALITY_TAG_UNAVAILABLE: Final[str] = "finality_tag_unavailable"

#: A block previously marked finalized under the then-effective
#: policy later falls out of the finalized ancestry, **or**
#: qualified endpoints persistently disagree on the finalized block
#: hash / ancestry. The handler halts immediately, journals an
#: append-only critical-incident candidate, preserves every prior
#: finalized raw evidence row, and escalates to Phase 8.
FINALIZED_ANCESTRY_VIOLATION: Final[str] = "FINALIZED_ANCESTRY_VIOLATION"


#: The complete reason-code vocabulary. Anything outside this set is
#: rejected by :class:`ReorgJournal.record_orphan`.
VALID_REORG_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
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
)


# ---------------------------------------------------------------------------
# Experimental placeholder for confirmations = 12
# ---------------------------------------------------------------------------

#: Literal status token for ``confirmations = 12``. The contract
#: permits this only as a legacy / fixture placeholder; mainnet
#: qualification, paper trading, and live promotion must never
#: depend on it.
EXPERIMENTAL_NOT_LIVE_APPROVED: Final[str] = "EXPERIMENTAL_NOT_LIVE_APPROVED"

#: The legacy confirmations count the contract explicitly names as a
#: placeholder. Held only for fixture references and legacy config
#: values; production code must not consume it.
EXPERIMENTAL_CONFIRMATIONS_PLACEHOLDER: Final[int] = 12


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReorgError(RuntimeError):
    """Base class for T033 errors."""


class FinalityPolicyError(ReorgError):
    """The injected finality policy is malformed.

    Examples: ``finality_tag`` is not the literal ``"finalized"``,
    ``qualified_endpoint_aliases`` is empty, or the caller tried to
    enable the experimental ``confirmations = 12`` placebo without
    the explicit :data:`EXPERIMENTAL_NOT_LIVE_APPROVED` flag.
    """


class FinalityUnavailableError(ReorgError):
    """The qualified endpoint could not return the finalized block.

    The ``reason_code`` attribute distinguishes
    :data:`FINALIZED_UNAVAILABLE`, :data:`FINALITY_TAG_UNAVAILABLE`,
    and :data:`FINALIZED_ANCESTRY_DISAGREEMENT`.
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        if reason_code not in (
            FINALIZED_UNAVAILABLE,
            FINALIZED_ANCESTRY_DISAGREEMENT,
            FINALITY_TAG_UNAVAILABLE,
        ):
            raise FinalityPolicyError(
                f"FinalityUnavailableError: unknown reason_code {reason_code!r}"
            )
        self.reason_code = reason_code


# ---------------------------------------------------------------------------
# Finality policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinalityPolicy:
    """The configurable finality policy T033 mandates.

    The policy's formal boundary is the JSON-RPC ``finalized`` tag.
    Every component that decides finality (the storage writer, the
    checkpoint advancer, the reorg handler) must accept an explicit
    :class:`FinalityPolicy` argument; no consumer may instantiate a
    production-shaped default at runtime.

    Attributes
    ----------
    finality_tag:
        The literal JSON-RPC block-tag string used as the finality
        boundary. The contract pins this to ``"finalized"``. Any
        other value is rejected.
    qualified_endpoint_aliases:
        Endpoint aliases the policy considers authoritative for the
        finalized header. The handler requires the qualified aliases
        to either all agree on the finalized block hash or fail
        closed with :data:`FINALIZED_ANCESTRY_DISAGREEMENT`.
    finalized_tag_request_timeout_seconds:
        Timeout the handler uses for the finalized-tag RPC call.
    finalized_tag_retry_limit:
        Number of in-endpoint retries the handler performs before
        reporting :data:`FINALIZED_UNAVAILABLE`.
    deep_reorg_threshold_blocks:
        Orphan depth threshold above which the handler halts with
        :data:`REASON_DEEP_REORG` and journals a critical-incident
        candidate. A threshold of ``1`` therefore rejects every
        orphan, which the contract accepts as a fail-closed setting.
    unfinalized_window_lag_blocks:
        Maximum block lag the handler treats as the unfinalized hash
        window ``[finalized + 1, latest]`` when the latest block
        report is missing. The window is never larger than this.
    deep_reorg_halt_qualifies_backtest:
        Whether the handler should block new backtest qualification
        on a deep reorg or critical incident. Defaults to ``True``;
        tests that need a different setting can override it.
    policy_id:
        A stable opaque identifier the manifest persists alongside
        the reorg journal entry. Distinct policies must have
        distinct ids so the audit trail can reproduce the policy in
        effect at the time of a reorg.
    """

    finality_tag: str = "finalized"
    qualified_endpoint_aliases: tuple[str, ...] = ()
    finalized_tag_request_timeout_seconds: float = 5.0
    finalized_tag_retry_limit: int = 2
    deep_reorg_threshold_blocks: int = 5
    unfinalized_window_lag_blocks: int = 64
    deep_reorg_halt_qualifies_backtest: bool = True
    policy_id: str = "default-finality-policy"

    def __post_init__(self) -> None:
        if not isinstance(self.finality_tag, str) or not self.finality_tag:
            raise FinalityPolicyError("finality_tag must be a non-empty string")
        # The contract pins the formal boundary to the JSON-RPC
        # ``finalized`` tag. We accept the literal "finalized"
        # (lowercase) and reject any other value outright: a chain-
        # aware fall-back to "safe" or to a confirmations count is
        # explicitly banned by the contract.
        if self.finality_tag != "finalized":
            raise FinalityPolicyError(
                f"finality_tag must be the literal 'finalized' tag; got {self.finality_tag!r}"
            )
        if not isinstance(self.qualified_endpoint_aliases, tuple):
            raise FinalityPolicyError("qualified_endpoint_aliases must be a tuple")
        if not self.qualified_endpoint_aliases:
            raise FinalityPolicyError("qualified_endpoint_aliases must list at least one endpoint")
        for alias in self.qualified_endpoint_aliases:
            if not isinstance(alias, str) or not alias:
                raise FinalityPolicyError(
                    f"qualified_endpoint_aliases: blank alias in {self.qualified_endpoint_aliases!r}"
                )
        if self.finalized_tag_request_timeout_seconds <= 0:
            raise FinalityPolicyError("finalized_tag_request_timeout_seconds must be positive")
        if self.finalized_tag_retry_limit < 0:
            raise FinalityPolicyError("finalized_tag_retry_limit must be non-negative")
        if self.deep_reorg_threshold_blocks < 1:
            raise FinalityPolicyError(
                "deep_reorg_threshold_blocks must be >= 1; a value < 1 disables all reorgs"
            )
        if self.unfinalized_window_lag_blocks < 0:
            raise FinalityPolicyError("unfinalized_window_lag_blocks must be >= 0")
        if not isinstance(self.policy_id, str) or not self.policy_id:
            raise FinalityPolicyError("policy_id must be a non-empty string")

    def qualified_aliases(self) -> tuple[str, ...]:
        """Return the qualified endpoint aliases as a tuple (frozen)."""
        return self.qualified_endpoint_aliases


# ---------------------------------------------------------------------------
# Unfinalized hash window
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnfinalizedWindow:
    """The ``[finalized + 1, latest]`` window the policy permits to
    be observed but never promotes into the qualified dataset.

    The window is inclusive on both ends. The handler fetches and
    records observations for blocks inside this window for liveness,
    but the reader API and downstream qualification reject every
    block whose number falls inside an open window.
    """

    finalized_block_number: int
    latest_block_number: int

    def __post_init__(self) -> None:
        if self.finalized_block_number < 0:
            raise FinalityPolicyError(
                f"UnfinalizedWindow: finalized_block_number must be >= 0, "
                f"got {self.finalized_block_number}"
            )
        if self.latest_block_number < self.finalized_block_number:
            raise FinalityPolicyError(
                "UnfinalizedWindow: latest_block_number must be >= finalized_block_number"
            )

    @property
    def lower_bound(self) -> int:
        """First block number in the window (``finalized + 1``)."""
        return self.finalized_block_number + 1

    @property
    def upper_bound(self) -> int:
        """Last block number in the window (``latest``)."""
        return self.latest_block_number

    def contains(self, block_number: int) -> bool:
        """``True`` when ``block_number`` lies inside the window."""
        return self.lower_bound <= block_number <= self.upper_bound

    def is_empty(self) -> bool:
        """``True`` when the chain has no observed-but-unfinalized blocks."""
        return self.latest_block_number <= self.finalized_block_number


# ---------------------------------------------------------------------------
# Finalized-boundary evaluation
# ---------------------------------------------------------------------------


#: Decision codes returned by :func:`evaluate_finality_boundary`.
#: Strings only — the manifest records them verbatim.
FinalityDecisionKind = Literal[
    "agreed",
    "disagreement",
    "unavailable",
    "tag_unavailable",
]


@dataclass(frozen=True, slots=True)
class FinalityBoundaryObservation:
    """One endpoint's view of the finalized block header.

    ``block_hash`` and ``parent_hash`` are lowercase
    ``0x``-prefixed hex strings. ``error`` is set when the endpoint
    could not return a finalized block; ``tag_supported`` is
    ``False`` when the endpoint explicitly does not implement the
    ``finalized`` tag.
    """

    endpoint_alias: str
    block_number: int | None
    block_hash: str | None
    parent_hash: str | None
    tag_supported: bool = True
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FinalityDecision:
    """The handler's verdict on the qualified-endpoint finalized boundary.

    ``kind`` is one of :data:`FinalityDecisionKind`; ``reason_code``
    is the value the manifest records (``finalized_unavailable`` /
    ``finalized_ancestry_disagreement`` / ``finality_tag_unavailable``
    / ``None`` for ``agreed``). ``agreed_block`` is the agreed
    finalized header when ``kind == "agreed"``; ``None`` otherwise.
    """

    kind: FinalityDecisionKind
    reason_code: str | None
    agreed_block: FinalityBoundaryObservation | None
    observations: tuple[FinalityBoundaryObservation, ...]


def _normalize_hash(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s.startswith("0x"):
        s = "0x" + s
    return s


def evaluate_finality_boundary(
    policy: FinalityPolicy,
    observations: Mapping[str, FinalityBoundaryObservation],
) -> FinalityDecision:
    """Compare the qualified-endpoint finalized headers.

    Returns a :class:`FinalityDecision`. The decision is fail-closed:

    - if any qualified endpoint returned ``tag_supported = False`` the
      handler reports :data:`FINALITY_TAG_UNAVAILABLE`;
    - if any qualified endpoint could not return a finalized header
      (transport error, timeout, malformed response) the handler
      reports :data:`FINALIZED_UNAVAILABLE`;
    - if the qualified endpoints disagree on the finalized block hash
      or parent hash the handler reports
      :data:`FINALIZED_ANCESTRY_DISAGREEMENT`;
    - otherwise the handler agrees on a single finalized header and
      returns ``kind = "agreed"``.

    Endpoints not in ``policy.qualified_endpoint_aliases`` are
    ignored; this lets the caller forward probes for several
    endpoints but only enforce agreement on the qualified subset.
    """
    if not isinstance(policy, FinalityPolicy):  # pragma: no cover - defensive
        raise FinalityPolicyError("evaluate_finality_boundary: policy must be a FinalityPolicy")
    qualified: list[FinalityBoundaryObservation] = []
    seen_aliases: set[str] = set()
    for alias in policy.qualified_endpoint_aliases:
        obs = observations.get(alias)
        if obs is None:
            raise FinalityPolicyError(
                f"evaluate_finality_boundary: missing observation for qualified alias {alias!r}"
            )
        if alias in seen_aliases:
            raise FinalityPolicyError(f"evaluate_finality_boundary: duplicate alias {alias!r}")
        seen_aliases.add(alias)
        qualified.append(obs)

    observations_tuple = tuple(qualified)

    for obs in qualified:
        if not obs.tag_supported:
            return FinalityDecision(
                kind="tag_unavailable",
                reason_code=FINALITY_TAG_UNAVAILABLE,
                agreed_block=None,
                observations=observations_tuple,
            )
        if obs.error is not None or obs.block_hash is None or obs.block_number is None:
            return FinalityDecision(
                kind="unavailable",
                reason_code=FINALIZED_UNAVAILABLE,
                agreed_block=None,
                observations=observations_tuple,
            )

    canonical = qualified[0]
    canonical_hash = _normalize_hash(canonical.block_hash)
    canonical_parent = _normalize_hash(canonical.parent_hash)
    canonical_number = canonical.block_number
    for obs in qualified[1:]:
        if (
            _normalize_hash(obs.block_hash) != canonical_hash
            or _normalize_hash(obs.parent_hash) != canonical_parent
            or obs.block_number != canonical_number
        ):
            return FinalityDecision(
                kind="disagreement",
                reason_code=FINALIZED_ANCESTRY_DISAGREEMENT,
                agreed_block=None,
                observations=observations_tuple,
            )
    # Build an agreed-block observation that carries the canonical
    # normalized hashes the rest of the handler compares against.
    agreed = FinalityBoundaryObservation(
        endpoint_alias=canonical.endpoint_alias,
        block_number=canonical.block_number,
        block_hash=canonical_hash,
        parent_hash=canonical_parent,
        tag_supported=True,
        error=None,
    )
    return FinalityDecision(
        kind="agreed",
        reason_code=None,
        agreed_block=agreed,
        observations=observations_tuple,
    )


__all__ = [
    "EXPERIMENTAL_CONFIRMATIONS_PLACEHOLDER",
    "EXPERIMENTAL_NOT_LIVE_APPROVED",
    "FINALITY_TAG_UNAVAILABLE",
    "FINALIZED_ANCESTRY_DISAGREEMENT",
    "FINALIZED_ANCESTRY_VIOLATION",
    "FINALIZED_UNAVAILABLE",
    "FinalityBoundaryObservation",
    "FinalityDecision",
    "FinalityDecisionKind",
    "FinalityPolicy",
    "FinalityPolicyError",
    "FinalityUnavailableError",
    "REASON_DEEP_REORG",
    "REASON_ORPHAN_REAPPEARANCE",
    "REASON_PROVIDER_DISAGREEMENT",
    "REASON_REMOVED_LOG",
    "REASON_SHALLOW_REORG",
    "ReorgError",
    "UnfinalizedWindow",
    "VALID_REORG_REASON_CODES",
    "evaluate_finality_boundary",
]
