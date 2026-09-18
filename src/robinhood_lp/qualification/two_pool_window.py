"""Two-pool ten-million-block window rule (T038 / ADR-013).

The T038 contract defines the rule that decides the per-pool inclusive
window a qualified dataset covers when the Owner pins a ten-million-block
range whose end must be a **finalized** block, not ``latest``.

This module is the deterministic, offline implementation of the rule.
It is the audit-trail surface the operator runbook and the per-pool
T034 machine report consume; the actual ``finalized`` block reading
and the PoolId-filtered Initialize scan are operator-side actions whose
evidence is pinned into the inputs this module consumes.

The window rule (Owner decision 2026-09-18, delegated):

1. The window end is a single **finalized** block read at run start
   from both qualified endpoints; the two readings must agree on
   block number and block hash. The agreed block is pinned by
   number and hash into the run manifest, the T034 report and the
   evidence pack.
2. ``latest``, a non-finalized block and any wall-clock-derived bound
   are excluded as the window end, including as a fallback when the
   tag is unavailable.
3. The window start is ``min(end - 10_000_000, Initialize block of
   each included pool)``.
4. The start is extended below ``end - 10_000_000`` only when a
   pool's ``Initialize`` lies within 2,000,000 blocks of it. A pool
   whose ``Initialize`` lies further below is reported as
   ``pool_init_outside_window`` and **excluded** from the qualified
   dataset rather than extending the window. Extended and excluded
   pools are both named in the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# Constants (T038 contract; not planning choices)
# ---------------------------------------------------------------------------

#: The Owner-pinned second pool PoolId (chain id 4663). The remaining
#: PoolKey fields are UNKNOWN as of the contract and must be resolved on
#: chain by an operator-side ``PoolId``-filtered Initialize scan.
SECOND_POOL_POOL_ID_HEX: Final[str] = "0xEd50bDeeA8aDC232f159486192a4157281D722ff"

#: The Owner-pinned chain id (Robinhood Chain mainnet).
TWO_POOL_CHAIN_ID: Final[int] = 4663

#: The fixed window width the T038 contract pins (ten million blocks).
TWO_POOL_WINDOW_BLOCKS: Final[int] = 10_000_000

#: The hard extension cap below ``end - 10_000_000`` an included
#: pool's ``Initialize`` block may sit at before the window widens
#: beyond ten million blocks. A pool whose ``Initialize`` lies
#: further below is excluded as ``pool_init_outside_window`` rather
#: than widening the window to chase it.
TWO_POOL_INIT_EXTENSION_CAP_BLOCKS: Final[int] = 2_000_000

#: Outcome strings the planner emits for pools the rule decides to
#: exclude (no PoolKey guess is produced).
OUTCOME_POOL_INCLUDED: Final[str] = "pool_included"
OUTCOME_POOL_INIT_OUTSIDE_WINDOW: Final[str] = "pool_init_outside_window"

#: Tag vocabulary for the per-endpoint finalized-pin evidence.
PIN_OK: Final[str] = "ok"
PIN_DISAGREEMENT: Final[str] = "finalized_endpoint_disagreement"
PIN_UNAVAILABLE: Final[str] = "finalized_unavailable"


# ---------------------------------------------------------------------------
# Per-endpoint finalized pin
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EndpointFinalizedObservation:
    """The finalized-block reading one endpoint returned at run start.

    ``endpoint_alias`` is the short opaque alias the runner
    configures. ``block_number`` is the decimal block height the
    ``eth_getBlockByNumber("finalized", false)`` call returned;
    ``None`` when the endpoint did not surface a finalized tag at
    all. ``block_hash`` is the hex form of the block hash; ``None``
    when the endpoint did not return a hash. ``observed_at`` is the
    audit-trail UTC ISO-8601 timestamp the observation was recorded
    at.

    A ``block_hash`` of the wrong length (not 0x + 64 hex chars) is a
    validation error; the planner never accepts it. An observation
    with ``block_number=None`` or ``block_hash=None`` is a
    ``finalized_unavailable`` outcome that the window planner turns
    into a run halt (the rule requires both qualified endpoints
    to surface a finalized tag and agree on it).
    """

    endpoint_alias: str
    block_number: int | None
    block_hash: str | None
    observed_at: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint_alias, str) or not self.endpoint_alias:
            raise ValueError(
                f"EndpointFinalizedObservation: endpoint_alias must be non-empty str, "
                f"got {self.endpoint_alias!r}"
            )
        if self.block_number is not None and self.block_number < 0:
            raise ValueError(
                f"EndpointFinalizedObservation: block_number must be >= 0, got {self.block_number}"
            )
        if self.block_hash is not None:
            if not isinstance(self.block_hash, str):
                raise TypeError(
                    f"EndpointFinalizedObservation: block_hash must be str, "
                    f"got {type(self.block_hash).__name__}"
                )
            s = self.block_hash.strip().lower()
            if not s.startswith("0x") or len(s) != 2 + 2 * 32:
                raise ValueError(
                    f"EndpointFinalizedObservation: block_hash must be 0x + 64 hex chars, "
                    f"got {self.block_hash!r}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint_alias": self.endpoint_alias,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "observed_at": self.observed_at,
        }


# ---------------------------------------------------------------------------
# Window pin (the agreed finalized block both endpoints reported)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowPin:
    """The pinned, agreed ``finalized`` block both qualified endpoints
    reported at run start.

    The pin is the window end. The block number and hash are recorded
    together so the audit trail can reproduce the exact block the
    acquisition covered. ``primary_endpoint`` / ``secondary_endpoint``
    record the two aliases that produced the observations; the
    planner refuses to accept a pin whose observations disagree.

    The pin is immutable: every consumer reads
    ``block_number`` / ``block_hash`` and never re-derives the
    window end from any other source.
    """

    block_number: int
    block_hash: str
    primary_endpoint: str
    secondary_endpoint: str
    pinned_at: str = ""

    def __post_init__(self) -> None:
        if self.block_number < 0:
            raise ValueError(f"WindowPin: block_number must be >= 0, got {self.block_number}")
        s = self.block_hash.strip().lower()
        if not s.startswith("0x") or len(s) != 2 + 2 * 32:
            raise ValueError(
                f"WindowPin: block_hash must be 0x + 64 hex chars, got {self.block_hash!r}"
            )
        if not self.primary_endpoint or not self.secondary_endpoint:
            raise ValueError("WindowPin: primary_endpoint and secondary_endpoint must be non-empty")
        if self.primary_endpoint == self.secondary_endpoint:
            raise ValueError("WindowPin: primary_endpoint and secondary_endpoint must differ")

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "primary_endpoint": self.primary_endpoint,
            "secondary_endpoint": self.secondary_endpoint,
            "pinned_at": self.pinned_at,
        }


# ---------------------------------------------------------------------------
# Per-pool window outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolWindowOutcome:
    """The window rule's per-pool verdict.

    ``outcome`` is one of :data:`OUTCOME_POOL_INCLUDED` or
    :data:`OUTCOME_POOL_INIT_OUTSIDE_WINDOW`. The
    ``coverage_from_block`` is the inclusive lower bound the runner
    uses for that pool; for an excluded pool the field still
    records the rule's computed start so the audit trail can show
    how the exclusion was reached.

    ``extension_below_default_start`` is the count of blocks the
    window extended below ``end - 10_000_000`` for this pool. It is
    ``0`` when the Initialize block was at or above
    ``end - 10_000_000`` (no extension needed), positive when the
    rule widened the window to chase an Initialize within the
    ``TWO_POOL_INIT_EXTENSION_CAP_BLOCKS`` cap, and
    ``gap_blocks`` is the distance from ``Initialize`` to
    ``end - 10_000_000`` when the rule excluded the pool.
    """

    pool_alias: str
    pool_id_hex: str
    pool_init_block: int
    outcome: str
    coverage_from_block: int
    default_start_block: int
    extension_below_default_start: int
    gap_blocks: int | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in (OUTCOME_POOL_INCLUDED, OUTCOME_POOL_INIT_OUTSIDE_WINDOW):
            raise ValueError(
                f"PoolWindowOutcome: outcome must be one of "
                f"({OUTCOME_POOL_INCLUDED!r}, {OUTCOME_POOL_INIT_OUTSIDE_WINDOW!r}), "
                f"got {self.outcome!r}"
            )
        if self.pool_init_block < 0:
            raise ValueError(
                f"PoolWindowOutcome: pool_init_block must be >= 0, got {self.pool_init_block}"
            )
        if self.coverage_from_block < 0:
            raise ValueError(
                f"PoolWindowOutcome: coverage_from_block must be >= 0, "
                f"got {self.coverage_from_block}"
            )
        if self.extension_below_default_start < 0:
            raise ValueError(
                f"PoolWindowOutcome: extension_below_default_start must be >= 0, "
                f"got {self.extension_below_default_start}"
            )

    @property
    def is_included(self) -> bool:
        return self.outcome == OUTCOME_POOL_INCLUDED

    @property
    def is_excluded(self) -> bool:
        return self.outcome == OUTCOME_POOL_INIT_OUTSIDE_WINDOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_alias": self.pool_alias,
            "pool_id_hex": self.pool_id_hex,
            "pool_init_block": self.pool_init_block,
            "outcome": self.outcome,
            "coverage_from_block": self.coverage_from_block,
            "default_start_block": self.default_start_block,
            "extension_below_default_start": self.extension_below_default_start,
            "gap_blocks": self.gap_blocks,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Pool candidate the planner consumes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TwoPoolCandidate:
    """One pool the window planner applies the rule to.

    ``pool_alias`` is a short opaque token (e.g. ``reference``,
    ``second``); ``pool_id_hex`` is the canonical lowercase 0x-hex
    PoolId the Owner pinned. ``pool_init_block`` is the block the
    operator resolved from the chain; a value of ``None`` represents
    "Initialize not yet resolved / Initialize outside the search
    bounds" and forces the ``pool_init_outside_window`` outcome
    without an on-chain guess.

    The validator accepts both 20-byte (address-sized) and 32-byte
    (keccak256-sized) pool identifiers so the owner-pinned value
    (which the T038 contract pins as a 40-char hex) does not
    silently abort the planner; the second-pool identity resolver
    is the audit-trail surface that surfaces the contract defect
    when the pinned hex is not a 32-byte keccak256 digest.
    """

    pool_alias: str
    pool_id_hex: str
    pool_init_block: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.pool_alias, str) or not self.pool_alias:
            raise ValueError(
                f"TwoPoolCandidate: pool_alias must be non-empty str, got {self.pool_alias!r}"
            )
        if not isinstance(self.pool_id_hex, str):
            raise TypeError(
                f"TwoPoolCandidate: pool_id_hex must be str, got {type(self.pool_id_hex).__name__}"
            )
        s = self.pool_id_hex.strip().lower()
        if not s.startswith("0x"):
            raise ValueError(
                f"TwoPoolCandidate: pool_id_hex must be 0x-prefixed hex, got {self.pool_id_hex!r}"
            )
        body_len = len(s) - 2
        if body_len not in (40, 64):
            raise ValueError(
                f"TwoPoolCandidate: pool_id_hex body must be 40 or 64 hex chars, "
                f"got {body_len} (from {self.pool_id_hex!r})"
            )
        try:
            int(s, 16)
        except ValueError as exc:
            raise ValueError(f"TwoPoolCandidate: pool_id_hex body is not valid hex: {exc}") from exc
        if self.pool_init_block is not None and self.pool_init_block < 0:
            raise ValueError(
                f"TwoPoolCandidate: pool_init_block must be >= 0 or None, "
                f"got {self.pool_init_block}"
            )


# ---------------------------------------------------------------------------
# The two-pool window plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TwoPoolWindowPlan:
    """The deterministic plan the window rule produces.

    ``window_pin`` is the agreed finalized block (window end).
    ``default_start_block`` is ``end - 10_000_000``; the per-pool
    ``PoolWindowOutcome.coverage_from_block`` is computed against
    that value. ``pool_outcomes`` is the per-pool verdict; included
    pools are listed alongside excluded pools so the audit trail
    names both. ``window_width_blocks`` is the inclusive span of
    the union of all included pools' coverage ranges.
    """

    window_pin: WindowPin
    default_start_block: int
    pool_outcomes: tuple[PoolWindowOutcome, ...]
    window_width_blocks: int
    primary_endpoint_alias: str
    secondary_endpoint_alias: str

    def __post_init__(self) -> None:
        if self.default_start_block < 0:
            raise ValueError(
                f"TwoPoolWindowPlan: default_start_block must be >= 0, "
                f"got {self.default_start_block}"
            )
        if self.default_start_block > self.window_pin.block_number:
            raise ValueError(
                f"TwoPoolWindowPlan: default_start_block {self.default_start_block} "
                f"> window_pin.block_number {self.window_pin.block_number}"
            )
        if self.window_width_blocks < 0:
            raise ValueError(
                f"TwoPoolWindowPlan: window_width_blocks must be >= 0, "
                f"got {self.window_width_blocks}"
            )

    @property
    def included_pools(self) -> tuple[PoolWindowOutcome, ...]:
        return tuple(outcome for outcome in self.pool_outcomes if outcome.is_included)

    @property
    def excluded_pools(self) -> tuple[PoolWindowOutcome, ...]:
        return tuple(outcome for outcome in self.pool_outcomes if outcome.is_excluded)

    @property
    def included_pool_aliases(self) -> tuple[str, ...]:
        return tuple(outcome.pool_alias for outcome in self.included_pools)

    @property
    def excluded_pool_aliases(self) -> tuple[str, ...]:
        return tuple(outcome.pool_alias for outcome in self.excluded_pools)

    def coverage_for(self, pool_alias: str) -> PoolWindowOutcome:
        for outcome in self.pool_outcomes:
            if outcome.pool_alias == pool_alias:
                return outcome
        raise KeyError(f"TwoPoolWindowPlan: no outcome recorded for pool_alias {pool_alias!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_pin": self.window_pin.to_dict(),
            "default_start_block": self.default_start_block,
            "window_width_blocks": self.window_width_blocks,
            "primary_endpoint_alias": self.primary_endpoint_alias,
            "secondary_endpoint_alias": self.secondary_endpoint_alias,
            "pool_outcomes": [outcome.to_dict() for outcome in self.pool_outcomes],
        }


# ---------------------------------------------------------------------------
# Pin agreement
# ---------------------------------------------------------------------------


def finalize_window_pin(
    primary: EndpointFinalizedObservation,
    secondary: EndpointFinalizedObservation,
) -> WindowPin | None:
    """Compute the agreed ``WindowPin`` from two endpoint observations.

    Returns the agreed pin when both observations report the same
    block number and block hash. Returns ``None`` when:

    - either observation has ``block_number=None`` or
      ``block_hash=None`` (``finalized_unavailable`` — the rule
      forbids falling back to ``latest`` or a non-finalized block);
    - the observations disagree on block number or block hash
      (``finalized_endpoint_disagreement`` — the rule fails closed).

    The ``pinned_at`` is left empty; the operator-supplied caller
    writes its own audit timestamp into the run manifest.
    """
    if (
        primary.block_number is None
        or primary.block_hash is None
        or secondary.block_number is None
        or secondary.block_hash is None
    ):
        return None
    if primary.block_number != secondary.block_number:
        return None
    if not _hashes_equal(primary.block_hash, secondary.block_hash):
        return None
    return WindowPin(
        block_number=int(primary.block_number),
        block_hash=str(primary.block_hash).strip().lower(),
        primary_endpoint=primary.endpoint_alias,
        secondary_endpoint=secondary.endpoint_alias,
    )


def _hashes_equal(a: str, b: str) -> bool:
    """Compare two 0x-hex block hashes case-insensitively."""
    return a.strip().lower().removeprefix("0x") == b.strip().lower().removeprefix("0x")


# ---------------------------------------------------------------------------
# Window rule application
# ---------------------------------------------------------------------------


def apply_window_rule(
    *,
    window_pin: WindowPin,
    candidates: tuple[TwoPoolCandidate, ...],
    extension_cap_blocks: int = TWO_POOL_INIT_EXTENSION_CAP_BLOCKS,
    window_blocks: int = TWO_POOL_WINDOW_BLOCKS,
) -> TwoPoolWindowPlan:
    """Apply the window rule to ``candidates`` and return the plan.

    The plan:

    1. Computes ``default_start_block = pin.block_number - window_blocks``
       (clamped to 0; the planner surfaces the clamped value in the
       audit trail so a negative-tail scenario is visible).
    2. For each candidate, picks ``coverage_from_block`` as
       ``min(default_start_block, pool_init_block)``.
    3. Records ``extension_below_default_start`` as the count of
       blocks the window was extended (0 when Initialize is at or
       above ``default_start_block``).
    4. Excludes a pool with ``pool_init_block is None`` or with
       ``gap_blocks = default_start_block - pool_init_block > extension_cap_blocks``
       as ``pool_init_outside_window`` and never widens the window
       to chase it.

    The function is deterministic: every input is either a pinned
    final fact or an operator-supplied candidate; no I/O, no wall
    clock, no random sources.
    """
    if window_blocks <= 0:
        raise ValueError(f"apply_window_rule: window_blocks must be positive, got {window_blocks}")
    if extension_cap_blocks < 0:
        raise ValueError(
            f"apply_window_rule: extension_cap_blocks must be >= 0, got {extension_cap_blocks}"
        )
    if not candidates:
        raise ValueError("apply_window_rule: at least one candidate is required")
    if window_pin.block_number < window_blocks:
        default_start = 0
    else:
        default_start = window_pin.block_number - window_blocks

    pool_outcomes: list[PoolWindowOutcome] = []
    included_start_min: int | None = None
    for candidate in candidates:
        if candidate.pool_init_block is None:
            outcome = PoolWindowOutcome(
                pool_alias=candidate.pool_alias,
                pool_id_hex=candidate.pool_id_hex,
                pool_init_block=0,
                outcome=OUTCOME_POOL_INIT_OUTSIDE_WINDOW,
                coverage_from_block=default_start,
                default_start_block=default_start,
                extension_below_default_start=0,
                gap_blocks=None,
                reason=(
                    "pool_init_block not resolved from on-chain Initialize scan; "
                    "PoolKey / Initialize block were not supplied"
                ),
            )
            pool_outcomes.append(outcome)
            continue
        gap = default_start - candidate.pool_init_block
        if gap > extension_cap_blocks:
            # Initialize lies further below the default start than the
            # extension cap permits. Exclude the pool rather than
            # widening the window to chase it.
            outcome = PoolWindowOutcome(
                pool_alias=candidate.pool_alias,
                pool_id_hex=candidate.pool_id_hex,
                pool_init_block=candidate.pool_init_block,
                outcome=OUTCOME_POOL_INIT_OUTSIDE_WINDOW,
                coverage_from_block=default_start,
                default_start_block=default_start,
                extension_below_default_start=0,
                gap_blocks=gap,
                reason=(
                    f"Initialize block {candidate.pool_init_block} lies "
                    f"{gap} blocks below default_start {default_start}, "
                    f"exceeding the {extension_cap_blocks}-block extension cap; "
                    "window would widen beyond ten million blocks to chase it"
                ),
            )
            pool_outcomes.append(outcome)
            continue
        coverage_from = min(default_start, candidate.pool_init_block)
        extension = max(0, default_start - candidate.pool_init_block)
        outcome = PoolWindowOutcome(
            pool_alias=candidate.pool_alias,
            pool_id_hex=candidate.pool_id_hex,
            pool_init_block=candidate.pool_init_block,
            outcome=OUTCOME_POOL_INCLUDED,
            coverage_from_block=coverage_from,
            default_start_block=default_start,
            extension_below_default_start=extension,
            gap_blocks=None,
            reason=("window extended within extension cap" if extension > 0 else "no extension"),
        )
        pool_outcomes.append(outcome)
        if included_start_min is None or coverage_from < included_start_min:
            included_start_min = coverage_from

    # The audit-trail window width is the inclusive span between the
    # earliest included pool's coverage start and the pinned end. An
    # all-excluded plan reports a 0 width so the run halts without
    # claiming coverage.
    if included_start_min is None:
        window_width = 0
    else:
        window_width = window_pin.block_number - included_start_min + 1

    return TwoPoolWindowPlan(
        window_pin=window_pin,
        default_start_block=default_start,
        pool_outcomes=tuple(pool_outcomes),
        window_width_blocks=window_width,
        primary_endpoint_alias=window_pin.primary_endpoint,
        secondary_endpoint_alias=window_pin.secondary_endpoint,
    )


# ---------------------------------------------------------------------------
# Pre-pinned second-pool candidate
# ---------------------------------------------------------------------------


def second_pool_candidate(pool_init_block: int | None) -> TwoPoolCandidate:
    """Return the Owner-pinned second-pool candidate.

    The function is the deterministic surface the operator runbook
    and the second-pool identity resolver share: only the resolved
    ``pool_init_block`` is operator-supplied; the PoolId is the
    pinned second-pool identity the contract names.
    """
    return TwoPoolCandidate(
        pool_alias="second",
        pool_id_hex=SECOND_POOL_POOL_ID_HEX,
        pool_init_block=pool_init_block,
    )


def reference_pool_candidate(pool_init_block: int | None) -> TwoPoolCandidate:
    """Return the pinned reference-pool candidate.

    The reference pool is already pinned (T036) and its
    ``Initialize`` block is known; this helper centralises the
    alias / pool_id pairing so the operator runbook and the
    two-pool planner share one name.
    """
    from robinhood_lp.qualification.reference import (
        REFERENCE_POOL_ID_HEX,
    )

    return TwoPoolCandidate(
        pool_alias="reference",
        pool_id_hex=REFERENCE_POOL_ID_HEX,
        pool_init_block=pool_init_block,
    )


__all__ = [
    "OUTCOME_POOL_INCLUDED",
    "OUTCOME_POOL_INIT_OUTSIDE_WINDOW",
    "PIN_DISAGREEMENT",
    "PIN_OK",
    "PIN_UNAVAILABLE",
    "EndpointFinalizedObservation",
    "PoolWindowOutcome",
    "SECOND_POOL_POOL_ID_HEX",
    "TWO_POOL_CHAIN_ID",
    "TWO_POOL_INIT_EXTENSION_CAP_BLOCKS",
    "TWO_POOL_WINDOW_BLOCKS",
    "TwoPoolCandidate",
    "TwoPoolWindowPlan",
    "WindowPin",
    "apply_window_rule",
    "finalize_window_pin",
    "reference_pool_candidate",
    "second_pool_candidate",
]


# Field annotation for ``field`` (placeholder for static type checkers
# that may not see all dataclass ``field`` usages).
_ = field
