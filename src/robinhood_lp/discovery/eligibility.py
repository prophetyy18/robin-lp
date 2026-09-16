"""Pool and hook eligibility classifier (T023).

Given a ``PoolRecord`` (from T022), assign one of the five
``RunMode`` support levels with reason-coded evidence. The
classifier is **deterministic and audited**: every level assignment
carries a list of ``EligibilityReason`` records plus a list of
``evidence_pointers`` (audit pointers) that explain *where* the
decision's inputs came from.

T023 acceptance:

- every registry row has a level plus evidence (reasons and
  evidence pointers);
- unknown hooks and return-delta flags default to ``ingestion``;
- EIP-1167 minimal-proxy hook bytecode is demoted to ``rejected``;
- behaviour / code-hash change demotes the row to its prior safe
  level (at most ``ingestion``);
- classification decisions are deterministic and audited.

T023 must-not:

- infer semantics from hook address flags alone (the flag bits
  only suggest the *possibility* of a callback, not its behaviour);
- whitelist by name;
- fall back to plain-pool behaviour (a pool with unknown hook
  evidence stays at ``ingestion``);
- auto-promote an unknown hook or a return-delta hook to
  ``backtest`` / ``paper`` / ``live``.

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
    #: The hook contract's runtime bytecode matches the EIP-1167
    #: minimal-proxy pattern; the hook is not safe to model as a
    #: plain hook. The row is demoted to ``rejected``.
    PROXY_DETECTED = "proxy_detected"
    #: The hook contract's runtime code hash differs from a previously
    #: recorded code hash (an upgrade / behaviour change). The row is
    #: demoted to ``ingestion`` (the prior safe level) so the change
    #: can be investigated before any simulation resumes.
    BYTECODE_HASH_CHANGE = "bytecode_hash_change"
    #: The hook contract's runtime bytecode could not be retrieved
    #: (e.g. ``eth_getCode`` failure, archive unavailability, or no
    #: caller-supplied bytecode). The hook stays at ``ingestion``;
    #: absence of bytecode is not a permission to simulate.
    HOOK_BYTECODE_UNAVAILABLE = "hook_bytecode_unavailable"


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


#: EIP-1167 minimal proxy prefix (16 bytes):
#: ``36 3d 3d 37 36 3d 3d 3d 3d 36 3d 3d 3d 36 3d 3d 3d 36 3d 73``.
#: The constants are kept locally in T023 so the classifier does not
#: import the (heavier) T024 chain-capability module; the algorithm
#: is identical to T024's ``_looks_like_eip1167_minimal_proxy``.
_EIP1167_PREFIX: bytes = bytes.fromhex("363d3d37363d3d3d3d363d3d3d363d73")


#: EIP-1167 minimal proxy suffix (15 bytes).
_EIP1167_SUFFIX: bytes = bytes.fromhex("5af43d82803e903d91602b57fd5bf3")


#: Number of bytes in the standard 51-byte EIP-1167 runtime bytecode.
_EIP1167_LENGTH_STANDARD: int = 16 + 20 + 15  # = 51


#: Number of bytes in the 55-byte EIP-1167 variant (4-byte leading
#: zero slot before the standard prefix).
_EIP1167_LENGTH_VARIANT: int = 4 + 16 + 20 + 15  # = 55


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookEvidence:
    """Evidence about a pool's hook address.

    The classifier decides whether the pool can be promoted past
    ``ingestion`` based on the evidence accumulated here, not on the
    raw address flags alone (T023 must-not).

    ``bytecode`` and ``is_eip1167_proxy`` are populated by the
    framework from a separate ``eth_getCode`` probe; the classifier
    does not fetch code itself.
    """

    address: Address
    #: Whether the address is the canonical zero address (no hook).
    is_zero: bool
    #: Whether the address has any flag bits set.
    has_any_flag: bool
    #: Whether the address has any *_RETURNS_DELTA flag set.
    has_delta_flag: bool
    #: The contract's runtime bytecode hash (sha256). ``None`` if
    #: the framework has not yet fetched it; the framework is
    #: responsible for populating this field via a separate
    #: ``eth_getCode`` probe (T023 deliberately does not fetch code
    #: from inside the classifier -- it consumes pre-fetched evidence).
    code_hash: str | None = None
    #: Whether the contract exposes an upgrade / proxy pattern.
    #: ``None`` if the framework has not yet decided.
    upgrade_proxy_observed: bool | None = None
    #: The contract's raw runtime bytecode, when the framework has
    #: fetched it. ``None`` if not yet retrieved; the classifier
    #: uses ``bytecode`` to detect the EIP-1167 minimal-proxy
    #: pattern (a separate signal from
    #: ``upgrade_proxy_observed``).
    bytecode: bytes | None = None
    #: ``True`` iff ``bytecode`` matches the EIP-1167 minimal-proxy
    #: pattern; ``False`` iff ``bytecode`` was fetched and is *not*
    #: such a proxy; ``None`` iff ``bytecode`` was not fetched.
    #: The classifier treats ``True`` as a hard ``rejected`` signal
    #: regardless of the rest of the evidence.
    is_eip1167_proxy: bool | None = None
    #: The previously recorded ``code_hash`` for this hook address,
    #: when the framework has a historical observation to compare
    #: against. ``None`` when there is no prior observation. A
    #: mismatch between ``previous_code_hash`` and ``code_hash`` is
    #: a behaviour / code-hash change; the classifier demotes the
    #: row to ``ingestion`` (the prior safe level).
    previous_code_hash: str | None = None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EligibilityDecision:
    """The classifier's verdict for one pool.

    ``level`` is the assigned ``RunMode``; ``reasons`` is the
    per-decision reason-code list; ``evidence_pointers`` is the
    audit-trail list of inputs the classifier used (pool id, fee,
    hook address, code hash, etc.). Both lists are part of the
    audit trail (T023 acceptance: every row has a level plus
    evidence).
    """

    pool_id: object  # PoolKey.PoolId; imported lazily to keep typing simple
    level: RunMode
    reasons: list[EligibilityReason] = field(default_factory=list)
    evidence_pointers: list[str] = field(default_factory=list)

    def reason_codes(self) -> list[EligibilityReasonCode]:
        return [r.code for r in self.reasons]

    def has(self, code: EligibilityReasonCode) -> bool:
        return code in self.reason_codes()


def _looks_like_eip1167_minimal_proxy(code: bytes) -> bool:
    """Detect the EIP-1167 minimal-proxy pattern.

    Mirrors :func:`robinhood_lp.discovery.chain_capability._looks_like_eip1167_minimal_proxy`
    so T023 and T024 share the same detection surface. The standard
    EIP-1167 runtime bytecode is exactly 51 bytes:
    ``_EIP1167_PREFIX (16) || <20-byte implementation address> ||
    _EIP1167_SUFFIX (15)``. The 55-byte variant (a 4-byte leading
    zero slot followed by the standard shape) is also accepted
    because the chain-by-chain evidence is the same.
    """
    if len(code) not in (_EIP1167_LENGTH_STANDARD, _EIP1167_LENGTH_VARIANT):
        return False
    if not code.endswith(_EIP1167_SUFFIX):
        return False
    if len(code) == _EIP1167_LENGTH_VARIANT and code.startswith(b"\x00" * 4):
        return code[4:].startswith(_EIP1167_PREFIX)
    return code.startswith(_EIP1167_PREFIX)


def _derive_eip1167(bytecode: bytes | None) -> bool | None:
    """Return ``True``/``False`` for a known bytecode, ``None`` for unknown.

    The classifier treats ``None`` as "no bytecode available", which
    is a *separate* signal from "bytecode was fetched and is not a
    proxy". The third state is explicit because the must-not rule
    forbids promoting a hook whose bytecode could not be retrieved.
    """
    if bytecode is None:
        return None
    return _looks_like_eip1167_minimal_proxy(bytecode)


def analyze_hook(
    hook_address: Address,
    *,
    bytecode: bytes | None = None,
    previous_code_hash: str | None = None,
) -> HookEvidence:
    """Compute hook-evidence structure from a hook address.

    The classifier does **not** fetch bytecode; ``bytecode`` (and
    therefore ``is_eip1167_proxy``) must be populated by the
    framework before the final classification is emitted. ``code_hash``
    is also left ``None`` here -- the classifier consumes the
    pre-fetched hash through the ``HookEvidence.code_hash`` field.
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
        bytecode=bytecode,
        is_eip1167_proxy=_derive_eip1167(bytecode),
        previous_code_hash=previous_code_hash,
    )


def classify_pool(
    record: PoolRecord,
    hook_evidence: HookEvidence | None = None,
) -> EligibilityDecision:
    """Classify one pool's eligibility for the framework.

    The decision is **deterministic and audited**: given the same
    record and hook evidence, the decision is byte-exactly
    reproducible. The reasons list and the ``evidence_pointers``
    list together are the audit trail.

    Algorithm (ordered by precedence, most-restrictive wins):

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
    6. If ``previous_code_hash`` differs from the current
       ``code_hash``, ``BYTECODE_HASH_CHANGE`` is appended and the
       level is capped at ``ingestion`` (the prior safe level --
       behaviour / code-hash change demotes; T023 acceptance).
    7. If the hook bytecode matches the EIP-1167 minimal-proxy
       pattern, ``PROXY_DETECTED`` is appended and the level is
       demoted to ``rejected`` -- this is the most restrictive
       level and overrides everything above.
    """
    if hook_evidence is None:
        hook_evidence = analyze_hook(record.pool_key.hooks)

    reasons: list[EligibilityReason] = []
    evidence_pointers: list[str] = [
        f"pool_id={record.pool_id.to_hex()}",
        f"fee={record.pool_key.fee}",
        f"hooks={record.pool_key.hooks.to_hex()}",
    ]
    # Default; the most conservative non-rejected level.
    level = RunMode.INGESTION

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
        evidence_pointers.append(f"hook_code_hash={hook_evidence.code_hash[:16]}")

    # 5. Upgrade / proxy: cap at ingestion.
    if hook_evidence.upgrade_proxy_observed is True:
        reasons.append(
            EligibilityReason(
                EligibilityReasonCode.UPGRADE_PROXY_OBSERVED,
                detail="contract exposes upgrade / proxy surface",
            )
        )
        evidence_pointers.append("hook_upgrade_proxy_observed=true")
        level = RunMode.INGESTION

    # 6. Code-hash change: demote to ingestion (the prior safe level).
    #    A non-zero hook whose code hash differs from a previously
    #    recorded code hash is a behaviour change; we cannot trust
    #    any prior simulation against the new code, so the row
    #    stays at ingestion (T023 acceptance).
    if (
        hook_evidence.previous_code_hash is not None
        and hook_evidence.code_hash is not None
        and hook_evidence.previous_code_hash != hook_evidence.code_hash
        and not hook_evidence.is_zero
    ):
        reasons.append(
            EligibilityReason(
                EligibilityReasonCode.BYTECODE_HASH_CHANGE,
                detail=(
                    f"previous={hook_evidence.previous_code_hash[:16]} "
                    f"current={hook_evidence.code_hash[:16]}"
                ),
            )
        )
        evidence_pointers.append("hook_code_hash_changed=true")
        if level != RunMode.REJECTED:
            level = RunMode.INGESTION

    # 6b. Hook bytecode unavailable: surface as a reason (still at
    #     ingestion -- "no bytecode" is not a permission to simulate).
    if hook_evidence.bytecode is None and not hook_evidence.is_zero:
        # Emit HOOK_BYTECODE_UNAVAILABLE whenever the framework
        # did not supply bytecode, regardless of whether a code hash
        # was pinned: the proxy-detection step needs the raw
        # bytecode, and "hash only" still leaves the proxy shape
        # unknown.
        reasons.append(
            EligibilityReason(
                EligibilityReasonCode.HOOK_BYTECODE_UNAVAILABLE,
                detail="hook bytecode was not retrieved; proxy shape unknown",
            )
        )

    # 7. EIP-1167 minimal-proxy: demote to ``rejected``. This is the
    #    most restrictive signal; it overrides every earlier rule.
    if hook_evidence.is_eip1167_proxy is True:
        reasons.append(
            EligibilityReason(
                EligibilityReasonCode.PROXY_DETECTED,
                detail="hook contract bytecode matches EIP-1167 minimal proxy",
            )
        )
        evidence_pointers.append("hook_bytecode_shape=eip1167_minimal_proxy")
        level = RunMode.REJECTED

    return EligibilityDecision(
        pool_id=record.pool_id,
        level=level,
        reasons=reasons,
        evidence_pointers=evidence_pointers,
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
