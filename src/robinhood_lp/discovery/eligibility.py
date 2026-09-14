"""Pool and hook eligibility classifier (T023).

Given a ``PoolRecord`` (from T022), assign one of the five
``RunMode`` support levels with reason-coded evidence. The
classifier is **deterministic and audited**: every level assignment
carries a list of ``EligibilityReason`` records that explain why.

T023 acceptance:

- every registry row has a level plus evidence;
- unknown hooks and return-delta flags default to ``ingestion``;
- behaviour / code-hash change demotes;
- classification decisions are deterministic and audited.

T023 must-not:

- infer semantics from hook address flags alone (the flag bits
  only suggest the *possibility* of a callback, not its behaviour);
- whitelist by name;
- fall back to plain-pool behaviour (a pool with unknown hook
  evidence stays at ``ingestion``).

This module sits in the storage layer per ADR-006 and depends on
``robinhood_lp.protocol`` and the T022 registry. It must not import
``robinhood_lp.config`` or higher layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from robinhood_lp.discovery.registry import PoolRecord
from robinhood_lp.protocol import Address, RunMode

# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------


class EligibilityReasonCode(StrEnum):
    """Stable reason codes for eligibility decisions.

    New codes require an ADR update so callers can branch on them.
    """

    STATIC_FEE_PLAIN_POOL = "static_fee_plain_pool"
    DYNAMIC_FEE_POOL = "dynamic_fee_pool"
    HOOK_ADDRESS_PRESENT = "hook_address_present"
    HOOK_HAS_DELTA_FLAG = "hook_has_delta_flag"
    HOOK_BEHAVIOUR_UNKNOWN = "hook_behaviour_unknown"
    HOOK_CODE_HASH_PINNED = "hook_code_hash_pinned"
    METADATA_COMPLETE = "metadata_complete"
    METADATA_INCOMPLETE = "metadata_incomplete"
    POOL_NO_HOOK = "pool_no_hook"
    UPGRADE_PROXY_OBSERVED = "upgrade_proxy_observed"


@dataclass(frozen=True, slots=True)
class EligibilityReason:
    """A single reason contributing to a support-level assignment.

    ``code`` is a stable ``EligibilityReasonCode``; ``detail`` is a
    free-form string for debugging. The classifier emits a list of
    reasons per pool; ``(code, detail)`` is the only audit format
    the framework relies on.
    """

    code: EligibilityReasonCode
    detail: str = ""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Bits in the hook address that signal a *return-delta* callback.
#: Per V4 Hooks.sol: bits 0..3 of the low 14 bits are *_RETURNS_DELTA
#: flags. See docs/spec/protocol/PROTOCOL_FACTS.md for the full table.
DELTA_FLAG_BITS: tuple[int, ...] = (
    1 << 0,  # AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA
    1 << 1,  # AFTER_ADD_LIQUIDITY_RETURNS_DELTA
    1 << 2,  # AFTER_SWAP_RETURNS_DELTA
    1 << 3,  # BEFORE_SWAP_RETURNS_DELTA
)

#: Bits in the hook address that signal a hook at all (any of the
#: low 14 bits set).
ALL_HOOK_FLAG_MASK: int = (1 << 14) - 1


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookEvidence:
    """Evidence about a pool's hook address.

    The classifier decides whether the pool can be promoted past
    ``ingestion`` based on the evidence accumulated here, not on the
    raw address flags alone (T023 must-not).
    """

    address: Address
    #: Whether the address is the canonical zero address (no hook).
    is_zero: bool
    #: Whether the address has any flag bits set.
    has_any_flag: bool
    #: Whether the address has any *_RETURNS_DELTA flag set.
    has_delta_flag: bool
    #: The contract's runtime bytecode hash (sha256). ``None`` if
    #: the classifier has not yet fetched it; the framework is
    #: responsible for populating this field via a separate
    #: ``eth_getCode`` probe (T023 deliberately does not fetch code
    #: from inside the classifier -- it consumes pre-fetched evidence).
    code_hash: str | None = None
    #: Whether the contract exposes an upgrade / proxy pattern.
    #: ``None`` if the framework has not yet decided.
    upgrade_proxy_observed: bool | None = None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EligibilityDecision:
    """The classifier's verdict for one pool.

    ``level`` is the assigned ``RunMode``; ``reasons`` is the
    audit trail. ``demote_to`` is a hint that the operator should
    downgrade the level if a condition later triggers (e.g. a code
    hash change).
    """

    pool_id: object  # PoolKey.PoolId; imported lazily to keep typing simple
    level: RunMode
    reasons: list[EligibilityReason] = field(default_factory=list)

    def reason_codes(self) -> list[EligibilityReasonCode]:
        return [r.code for r in self.reasons]

    def has(self, code: EligibilityReasonCode) -> bool:
        return code in self.reason_codes()


def analyze_hook(hook_address: Address) -> HookEvidence:
    """Compute hook-evidence structure from a hook address.

    The classifier does **not** fetch bytecode; ``code_hash`` is
    left None here and must be populated by the framework before the
    final classification is emitted.
    """
    is_zero = hook_address.value == 0
    low14 = hook_address.value & ALL_HOOK_FLAG_MASK
    has_any_flag = low14 != 0
    has_delta_flag = any((hook_address.value & bit) != 0 for bit in DELTA_FLAG_BITS)
    return HookEvidence(
        address=hook_address,
        is_zero=is_zero,
        has_any_flag=has_any_flag,
        has_delta_flag=has_delta_flag,
        code_hash=None,
        upgrade_proxy_observed=None,
    )


def classify_pool(
    record: PoolRecord,
    hook_evidence: HookEvidence | None = None,
) -> EligibilityDecision:
    """Classify one pool's eligibility for the framework.

    The decision is **deterministic and audited**: given the same
    record and hook evidence, the decision is byte-exactly
    reproducible. The reasons list is the audit trail.

    Algorithm:

    1. If the hook address is non-zero, the pool carries
       ``HOOK_ADDRESS_PRESENT``. The classifier cannot infer
       semantics from flags alone (T023 must-not), so the level
       defaults to ``ingestion`` with ``HOOK_BEHAVIOUR_UNKNOWN``.
    2. If the hook address has any *_RETURNS_DELTA flag bit set,
       ``HOOK_HAS_DELTA_FLAG`` is appended; the level remains
       ``ingestion`` with the additional reason.
    3. If the hook address is the zero address, the pool is
       ``POOL_NO_HOOK``. Static-fee pools with full metadata advance
       to ``backtest``; static-fee pools with missing metadata stay at
       ``ingestion``; dynamic-fee pools stay at ``ingestion``.
    4. If the hook evidence carries a pinned ``code_hash``, the
       ``HOOK_CODE_HASH_PINNED`` reason is appended. This does not
       promote the level by itself: a code-hash record is evidence,
       not permission.
    5. If ``upgrade_proxy_observed`` is True, ``UPGRADE_PROXY_OBSERVED``
       is appended and the level is capped at ``ingestion``.
    """
    if hook_evidence is None:
        hook_evidence = analyze_hook(record.pool_key.hooks)

    reasons: list[EligibilityReason] = []
    level = RunMode.INGESTION  # default; the most conservative level

    # 1. Hook address presence.
    if not hook_evidence.is_zero:
        reasons.append(
            EligibilityReason(
                EligibilityReasonCode.HOOK_ADDRESS_PRESENT,
                detail=f"hook address={hook_evidence.address.to_hex()}",
            )
        )
        # Until the framework has a pinned code hash and a verified
        # implementation, the level is ``ingestion``. We add the
        # unknown-behaviour reason.
        reasons.append(
            EligibilityReason(
                EligibilityReasonCode.HOOK_BEHAVIOUR_UNKNOWN,
                detail="hook semantics not modelled by the framework",
            )
        )

    # 2. Return-delta flag.
    if hook_evidence.has_delta_flag:
        reasons.append(
            EligibilityReason(
                EligibilityReasonCode.HOOK_HAS_DELTA_FLAG,
                detail="hook address has *_RETURNS_DELTA flag(s)",
            )
        )

    # 3. Zero-hook path: classify by fee + metadata coverage.
    if hook_evidence.is_zero:
        reasons.append(EligibilityReason(EligibilityReasonCode.POOL_NO_HOOK))
        is_static_fee = record.pool_key.fee <= 1_000_000 and record.pool_key.fee != 0x800000
        if not is_static_fee:
            reasons.append(
                EligibilityReason(
                    EligibilityReasonCode.DYNAMIC_FEE_POOL,
                    detail=f"fee={record.pool_key.fee}",
                )
            )
        else:
            reasons.append(
                EligibilityReason(
                    EligibilityReasonCode.STATIC_FEE_PLAIN_POOL,
                    detail=f"fee={record.pool_key.fee}",
                )
            )

        # Metadata coverage determines backtest eligibility for
        # static-fee plain pools. Dynamic-fee pools stay at ingestion
        # regardless of metadata.
        if is_static_fee and _metadata_complete(record):
            reasons.append(EligibilityReason(EligibilityReasonCode.METADATA_COMPLETE))
            level = RunMode.BACKTEST
        else:
            if not _metadata_complete(record):
                reasons.append(
                    EligibilityReason(
                        EligibilityReasonCode.METADATA_INCOMPLETE,
                        detail="token metadata call did not return symbol/name/decimals",
                    )
                )
            # Level stays at ingestion.

    # 4. Code-hash evidence: append reason but do not promote.
    if hook_evidence.code_hash is not None and not hook_evidence.is_zero:
        reasons.append(
            EligibilityReason(
                EligibilityReasonCode.HOOK_CODE_HASH_PINNED,
                detail=f"sha256={hook_evidence.code_hash[:16]}...",
            )
        )

    # 5. Upgrade / proxy: cap at ingestion.
    if hook_evidence.upgrade_proxy_observed is True:
        reasons.append(
            EligibilityReason(
                EligibilityReasonCode.UPGRADE_PROXY_OBSERVED,
                detail="contract exposes upgrade / proxy surface",
            )
        )
        level = RunMode.INGESTION

    return EligibilityDecision(
        pool_id=record.pool_id,
        level=level,
        reasons=reasons,
    )


def _metadata_complete(record: PoolRecord) -> bool:
    """Both tokens have a complete metadata record (symbol + name + decimals)."""
    return (
        record.token0_metadata is not None
        and record.token0_metadata.is_complete()
        and record.token1_metadata is not None
        and record.token1_metadata.is_complete()
    )


__all__ = [
    "DELTA_FLAG_BITS",
    "EligibilityDecision",
    "EligibilityReason",
    "EligibilityReasonCode",
    "HookEvidence",
    "analyze_hook",
    "classify_pool",
]
