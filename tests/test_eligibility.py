"""Tests for the eligibility classifier (T023).

Covers the T023 acceptance matrix:
- every registry row has a level plus evidence (reasons and
  evidence pointers)
- static-fee plain pool + complete metadata -> backtest
- dynamic-fee pool stays at ingestion
- nonzero hook address defaults to ingestion with hook evidence
- return-delta flag in hook address -> HOOK_HAS_DELTA_FLAG reason
- metadata incomplete -> METADATA_INCOMPLETE reason + ingestion
- upgrade / proxy observed -> capped at ingestion
- EIP-1167 minimal-proxy hook bytecode -> demoted to ``rejected``
  (PROXY_DETECTED)
- behaviour / code-hash change -> demoted to ``ingestion``
  (BYTECODE_HASH_CHANGE)
- unknown hook (no bytecode retrieval) stays at ``ingestion`` (not
  auto-promoted to backtest/paper/live)
- decisions are deterministic (same inputs -> same outputs)
"""

from __future__ import annotations

from typing import Any

from robinhood_lp.discovery import (
    DELTA_FLAG_BITS,
    EligibilityReasonCode,
    HookEvidence,
    analyze_hook,
    classify_pool,
)
from robinhood_lp.discovery.token_metadata import TokenMetadataRecord
from robinhood_lp.protocol import Address, Currency, PoolKey, RunMode


def _make_record(
    *,
    fee: int = 3000,
    hooks: int = 0,
    token0_meta: TokenMetadataRecord | None = None,
    token1_meta: TokenMetadataRecord | None = None,
) -> Any:
    """Build a PoolRecord-shaped object without going through the registry.

    The classifier only touches ``pool_key``, ``token0_metadata``,
    ``token1_metadata`` and ``pool_id``; a duck-typed duck satisfies mypy.
    """
    from dataclasses import dataclass

    @dataclass
    class _Stub:
        pool_id: object
        pool_key: PoolKey
        token0_metadata: TokenMetadataRecord | None
        token1_metadata: TokenMetadataRecord | None

    pk = PoolKey(
        currency0=Currency.from_int(0x10),
        currency1=Currency.from_int(0x20),
        fee=fee,
        tick_spacing=60,
        hooks=Address(hooks),
    )
    return _Stub(
        pool_id=pk.to_pool_id(),
        pool_key=pk,
        token0_metadata=token0_meta,
        token1_metadata=token1_meta,
    )


def _complete_meta(addr_hex: str) -> TokenMetadataRecord:
    return TokenMetadataRecord(
        address=Address.from_hex(addr_hex),
        symbol="TKN",
        name="Token",
        decimals=18,
    )


# ---------------------------------------------------------------------------
# Hook analysis
# ---------------------------------------------------------------------------


def test_analyze_hook_zero_address() -> None:
    h = analyze_hook(Address.zero())
    assert h.is_zero is True
    assert h.has_any_flag is False
    assert h.has_delta_flag is False


def test_analyze_hook_non_zero_address_with_no_flag_bits() -> None:
    # An address whose low 14 bits are 0 but is not the zero address
    # (upper bits set) still has no flags.
    h = analyze_hook(Address(1 << 20))
    assert h.is_zero is False
    assert h.has_any_flag is False
    assert h.has_delta_flag is False


def test_analyze_hook_non_zero_address_with_one_flag_bit() -> None:
    # Set bit 7 (BEFORE_SWAP). It is not a *_RETURNS_DELTA bit.
    h = analyze_hook(Address(1 << 7))
    assert h.has_any_flag is True
    assert h.has_delta_flag is False


def test_analyze_hook_address_with_delta_flag_bit_0() -> None:
    # AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA = bit 0
    h = analyze_hook(Address(1 << 0))
    assert h.has_any_flag is True
    assert h.has_delta_flag is True


def test_analyze_hook_address_with_delta_flag_bit_3() -> None:
    # BEFORE_SWAP_RETURNS_DELTA = bit 3
    h = analyze_hook(Address(1 << 3))
    assert h.has_any_flag is True
    assert h.has_delta_flag is True


# ---------------------------------------------------------------------------
# Classifier: static-fee, no hook, complete metadata
# ---------------------------------------------------------------------------


def test_classify_static_fee_no_hook_complete_metadata_is_backtest() -> None:
    rec = _make_record(
        fee=3000,
        hooks=0,
        token0_meta=_complete_meta("0x" + "11" * 20),
        token1_meta=_complete_meta("0x" + "22" * 20),
    )
    decision = classify_pool(rec)
    assert decision.level == RunMode.BACKTEST
    assert decision.has(EligibilityReasonCode.POOL_NO_HOOK)
    assert decision.has(EligibilityReasonCode.STATIC_FEE_PLAIN_POOL)
    assert decision.has(EligibilityReasonCode.METADATA_COMPLETE)
    assert not decision.has(EligibilityReasonCode.HOOK_ADDRESS_PRESENT)
    assert not decision.has(EligibilityReasonCode.HOOK_BEHAVIOUR_UNKNOWN)


def test_classify_static_fee_no_hook_incomplete_metadata_is_ingestion() -> None:
    rec = _make_record(
        fee=3000,
        hooks=0,
        # No metadata -> METADATA_INCOMPLETE
    )
    decision = classify_pool(rec)
    assert decision.level == RunMode.INGESTION
    assert decision.has(EligibilityReasonCode.POOL_NO_HOOK)
    assert decision.has(EligibilityReasonCode.STATIC_FEE_PLAIN_POOL)
    assert decision.has(EligibilityReasonCode.METADATA_INCOMPLETE)


def test_classify_static_fee_no_hook_partial_metadata_is_ingestion() -> None:
    """One token's metadata is complete, the other's is missing."""
    rec = _make_record(
        fee=3000,
        hooks=0,
        token0_meta=_complete_meta("0x" + "11" * 20),
        token1_meta=None,
    )
    decision = classify_pool(rec)
    assert decision.level == RunMode.INGESTION
    assert decision.has(EligibilityReasonCode.METADATA_INCOMPLETE)


# ---------------------------------------------------------------------------
# Classifier: dynamic fee stays at ingestion regardless of metadata
# ---------------------------------------------------------------------------


def test_classify_dynamic_fee_no_hook_complete_metadata_is_ingestion() -> None:
    rec = _make_record(
        fee=0x800000,
        hooks=0,
        token0_meta=_complete_meta("0x" + "11" * 20),
        token1_meta=_complete_meta("0x" + "22" * 20),
    )
    decision = classify_pool(rec)
    assert decision.level == RunMode.INGESTION
    assert decision.has(EligibilityReasonCode.POOL_NO_HOOK)
    assert decision.has(EligibilityReasonCode.DYNAMIC_FEE_POOL)
    # ``METADATA_COMPLETE`` is *not* added because the dynamic-fee
    # branch does not consider metadata completeness.
    assert not decision.has(EligibilityReasonCode.METADATA_COMPLETE)


# ---------------------------------------------------------------------------
# Classifier: nonzero hook defaults to ingestion
# ---------------------------------------------------------------------------


def test_classify_nonzero_hook_with_only_action_flag_is_ingestion() -> None:
    rec = _make_record(fee=3000, hooks=1 << 7)  # BEFORE_SWAP only
    decision = classify_pool(rec)
    assert decision.level == RunMode.INGESTION
    assert decision.has(EligibilityReasonCode.HOOK_ADDRESS_PRESENT)
    assert decision.has(EligibilityReasonCode.HOOK_BEHAVIOUR_UNKNOWN)
    assert not decision.has(EligibilityReasonCode.HOOK_HAS_DELTA_FLAG)


def test_classify_nonzero_hook_with_delta_flag_is_ingestion_with_delta_reason() -> None:
    rec = _make_record(fee=3000, hooks=1 << 0)  # AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA
    decision = classify_pool(rec)
    assert decision.level == RunMode.INGESTION
    assert decision.has(EligibilityReasonCode.HOOK_ADDRESS_PRESENT)
    assert decision.has(EligibilityReasonCode.HOOK_BEHAVIOUR_UNKNOWN)
    assert decision.has(EligibilityReasonCode.HOOK_HAS_DELTA_FLAG)


def test_classify_nonzero_hook_with_action_and_delta_is_ingestion() -> None:
    rec = _make_record(fee=3000, hooks=(1 << 7) | (1 << 3))  # BEFORE_SWAP + delta
    decision = classify_pool(rec)
    assert decision.level == RunMode.INGESTION
    assert decision.has(EligibilityReasonCode.HOOK_HAS_DELTA_FLAG)


# ---------------------------------------------------------------------------
# Classifier: code-hash evidence does not promote
# ---------------------------------------------------------------------------


def test_code_hash_does_not_promote_above_ingestion() -> None:
    rec = _make_record(fee=3000, hooks=1 << 7)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="a" * 64,
        upgrade_proxy_observed=None,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert decision.level == RunMode.INGESTION
    assert decision.has(EligibilityReasonCode.HOOK_CODE_HASH_PINNED)
    # The HOOK_BEHAVIOUR_UNKNOWN reason is still there: a pinned code
    # hash is evidence, not a permission to simulate.
    assert decision.has(EligibilityReasonCode.HOOK_BEHAVIOUR_UNKNOWN)


# ---------------------------------------------------------------------------
# Classifier: upgrade / proxy observed caps at ingestion
# ---------------------------------------------------------------------------


def test_upgrade_proxy_caps_classification_at_ingestion() -> None:
    """A static-fee plain pool with full metadata that has an
    upgrade-proxy hook stays at ingestion, not backtest."""
    rec = _make_record(
        fee=3000,
        hooks=0,
        token0_meta=_complete_meta("0x" + "11" * 20),
        token1_meta=_complete_meta("0x" + "22" * 20),
    )
    # The flag bits on hooks=0 are zero; the upgrade-proxy observation
    # is what triggers the demote.
    evidence = HookEvidence(
        address=Address.zero(),
        is_zero=True,
        has_any_flag=False,
        has_delta_flag=False,
        code_hash=None,
        upgrade_proxy_observed=True,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert decision.level == RunMode.INGESTION
    assert decision.has(EligibilityReasonCode.UPGRADE_PROXY_OBSERVED)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_classification_is_deterministic() -> None:
    """Same record + same evidence -> same decision byte-for-byte."""
    rec = _make_record(
        fee=3000,
        hooks=(1 << 7) | (1 << 3),
        token0_meta=_complete_meta("0x" + "11" * 20),
        token1_meta=_complete_meta("0x" + "22" * 20),
    )
    evidence = HookEvidence(
        address=Address((1 << 7) | (1 << 3)),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=True,
        code_hash="b" * 64,
        upgrade_proxy_observed=False,
    )
    a = classify_pool(rec, hook_evidence=evidence)
    b = classify_pool(rec, hook_evidence=evidence)
    assert a.level == b.level
    assert a.reason_codes() == b.reason_codes()
    assert a.pool_id == b.pool_id


def test_each_decision_has_at_least_one_reason() -> None:
    """Every registry row gets at least one reason; the audit trail
    is never empty (T023 acceptance)."""
    cases = [
        # Static fee, no hook, no metadata
        _make_record(fee=3000, hooks=0),
        # Static fee, no hook, full metadata
        _make_record(
            fee=3000,
            hooks=0,
            token0_meta=_complete_meta("0x" + "11" * 20),
            token1_meta=_complete_meta("0x" + "22" * 20),
        ),
        # Dynamic fee, no hook, full metadata
        _make_record(
            fee=0x800000,
            hooks=0,
            token0_meta=_complete_meta("0x" + "11" * 20),
            token1_meta=_complete_meta("0x" + "22" * 20),
        ),
        # Nonzero hook with delta
        _make_record(fee=3000, hooks=(1 << 0) | (1 << 7)),
    ]
    for rec in cases:
        decision = classify_pool(rec)
        assert len(decision.reasons) >= 1, (
            f"empty reasons for fee={rec.pool_key.fee}, hooks={rec.pool_key.hooks.value}"
        )


# ---------------------------------------------------------------------------
# Cross-check with the constants
# ---------------------------------------------------------------------------


def test_delta_flag_bits_constant() -> None:
    """The module's DELTA_FLAG_BITS covers bits 0..3 of the low 14."""
    assert DELTA_FLAG_BITS == (1 << 0, 1 << 1, 1 << 2, 1 << 3)


# ---------------------------------------------------------------------------
# Hook evidence: bytecode + previous_code_hash plumbing
# ---------------------------------------------------------------------------


def _eip1167_bytecode(length: int = 51, *, implementation: int | None = None) -> bytes:
    """Build a synthetic EIP-1167 minimal-proxy runtime bytecode.

    Mirrors the layout used by T024's chain_capability tests so the
    detector here agrees with the detector there. ``implementation``
    defaults to ``0xab`` repeated 20 times so different calls produce
    different (but valid) addresses.
    """
    addr_bytes = (
        implementation.to_bytes(20, "big") if implementation is not None else (b"\xab" * 20)
    )
    prefix = bytes.fromhex("363d3d37363d3d3d3d363d3d3d363d73")  # 16 bytes
    suffix = bytes.fromhex("5af43d82803e903d91602b57fd5bf3")  # 15 bytes
    if length == 51:
        return prefix + addr_bytes + suffix
    if length == 55:
        return b"\x00" * 4 + prefix + addr_bytes + suffix
    raise ValueError(f"unsupported EIP-1167 length {length}")


def test_analyze_hook_carries_eip1167_signal_when_bytecode_supplied() -> None:
    """analyze_hook computes is_eip1167_proxy from a supplied bytecode."""
    bytecode = _eip1167_bytecode(51)
    h = analyze_hook(Address(1 << 7), bytecode=bytecode)
    assert h.bytecode == bytecode
    assert h.is_eip1167_proxy is True


def test_analyze_hook_marks_non_proxy_bytecode_as_false() -> None:
    """A supplied bytecode that does not match the proxy pattern is
    explicitly ``False`` (not ``None``); the classifier treats
    ``False`` as "fetched and verified, no proxy"."""
    bytecode = b"\x60\x80\x60\x40" + b"\x00" * 60  # 64 bytes, not EIP-1167
    h = analyze_hook(Address(1 << 7), bytecode=bytecode)
    assert h.is_eip1167_proxy is False


def test_analyze_hook_records_previous_code_hash() -> None:
    """The previous_code_hash field round-trips through analyze_hook."""
    h = analyze_hook(Address(1 << 7), bytecode=b"\x01" * 32, previous_code_hash="a" * 64)
    assert h.previous_code_hash == "a" * 64


# ---------------------------------------------------------------------------
# Classifier: EIP-1167 minimal-proxy hook demotes to ``rejected``
# ---------------------------------------------------------------------------


def test_eip1167_bytecode_51_byte_variant_demotes_to_rejected() -> None:
    """A 51-byte EIP-1167 minimal-proxy hook is demoted to ``rejected``
    even though the pool has full metadata and a static fee; the
    proxy shape is itself the disqualifying evidence."""
    rec = _make_record(
        fee=3000,
        hooks=1 << 7,
        token0_meta=_complete_meta("0x" + "11" * 20),
        token1_meta=_complete_meta("0x" + "22" * 20),
    )
    bytecode = _eip1167_bytecode(51)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="c" * 64,
        upgrade_proxy_observed=None,
        bytecode=bytecode,
        is_eip1167_proxy=True,
        previous_code_hash=None,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert decision.level == RunMode.REJECTED
    assert decision.has(EligibilityReasonCode.PROXY_DETECTED)
    # The HOOK_BEHAVIOUR_UNKNOWN / HOOK_ADDRESS_PRESENT reasons are
    # still emitted -- the proxy detection is an additional signal.
    assert decision.has(EligibilityReasonCode.HOOK_ADDRESS_PRESENT)
    assert decision.has(EligibilityReasonCode.HOOK_BEHAVIOUR_UNKNOWN)


def test_eip1167_bytecode_55_byte_variant_demotes_to_rejected() -> None:
    """The 55-byte EIP-1167 variant (4-byte zero slot + standard
    prefix) is detected identically to the 51-byte form and demotes
    the row to ``rejected``."""
    rec = _make_record(fee=3000, hooks=(1 << 7) | (1 << 3))
    bytecode = _eip1167_bytecode(55)
    evidence = HookEvidence(
        address=Address((1 << 7) | (1 << 3)),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=True,
        code_hash="d" * 64,
        bytecode=bytecode,
        is_eip1167_proxy=True,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert decision.level == RunMode.REJECTED
    assert decision.has(EligibilityReasonCode.PROXY_DETECTED)


def test_non_proxy_bytecode_does_not_demote() -> None:
    """A hook whose bytecode is fetched and is *not* an EIP-1167
    proxy stays at ``ingestion`` (the proxy detector returns ``False``
    explicitly, not ``None``)."""
    bytecode = b"\x60\x80\x60\x40" + b"\x00" * 60  # 64 bytes
    rec = _make_record(fee=3000, hooks=1 << 7)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="e" * 64,
        bytecode=bytecode,
        is_eip1167_proxy=False,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert decision.level == RunMode.INGESTION
    assert not decision.has(EligibilityReasonCode.PROXY_DETECTED)


def test_proxy_detection_overrides_zero_hook_path() -> None:
    """A zero-hook static-fee plain pool with full metadata is
    normally ``backtest``; here we explicitly mark the zero address
    as ``is_eip1167_proxy=True`` (synthetic, used to verify the
    override) and confirm the row is still demoted to ``rejected``."""
    rec = _make_record(
        fee=3000,
        hooks=0,
        token0_meta=_complete_meta("0x" + "11" * 20),
        token1_meta=_complete_meta("0x" + "22" * 20),
    )
    bytecode = _eip1167_bytecode(51)
    evidence = HookEvidence(
        address=Address.zero(),
        is_zero=True,
        has_any_flag=False,
        has_delta_flag=False,
        code_hash=None,
        bytecode=bytecode,
        is_eip1167_proxy=True,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    # PROXY_DETECTED is the override; the level is REJECTED even
    # though the zero-hook path would otherwise promote to BACKTEST.
    assert decision.level == RunMode.REJECTED
    assert decision.has(EligibilityReasonCode.PROXY_DETECTED)


# ---------------------------------------------------------------------------
# Classifier: behaviour / code-hash change demotes
# ---------------------------------------------------------------------------


def test_bytecode_hash_change_demotes_to_ingestion() -> None:
    """A non-zero hook whose current code hash differs from the
    previously recorded code hash is demoted to ``ingestion`` even
    if it would otherwise have stayed at ``ingestion``; the change
    itself is the disqualifying signal."""
    rec = _make_record(fee=3000, hooks=1 << 7)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="b" * 64,
        previous_code_hash="a" * 64,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert decision.level == RunMode.INGESTION
    assert decision.has(EligibilityReasonCode.BYTECODE_HASH_CHANGE)


def test_bytecode_hash_match_does_not_emit_change_reason() -> None:
    """When ``previous_code_hash == code_hash`` the classifier does
    *not* emit ``BYTECODE_HASH_CHANGE`` (no demotion)."""
    rec = _make_record(fee=3000, hooks=1 << 7)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="a" * 64,
        previous_code_hash="a" * 64,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert decision.level == RunMode.INGESTION
    assert not decision.has(EligibilityReasonCode.BYTECODE_HASH_CHANGE)


def test_bytecode_hash_change_with_no_previous_is_ignored() -> None:
    """A hook whose ``previous_code_hash`` is ``None`` (no prior
    observation) is treated as a fresh classification -- no change
    reason is emitted and no demotion occurs."""
    rec = _make_record(fee=3000, hooks=1 << 7)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="a" * 64,
        previous_code_hash=None,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert decision.level == RunMode.INGESTION
    assert not decision.has(EligibilityReasonCode.BYTECODE_HASH_CHANGE)


def test_bytecode_hash_change_on_zero_hook_is_ignored() -> None:
    """A zero-hook pool carries no hook bytecode, so a hash change
    cannot apply; the classifier ignores ``previous_code_hash``
    mismatches on zero hooks (no spurious ``BYTECODE_HASH_CHANGE``)."""
    rec = _make_record(fee=3000, hooks=0)
    evidence = HookEvidence(
        address=Address.zero(),
        is_zero=True,
        has_any_flag=False,
        has_delta_flag=False,
        code_hash="b" * 64,
        previous_code_hash="a" * 64,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert not decision.has(EligibilityReasonCode.BYTECODE_HASH_CHANGE)


def test_bytecode_hash_change_does_not_override_rejected() -> None:
    """If the hook is *both* an EIP-1167 proxy *and* its code hash
    has changed, the row stays at ``rejected``; the change signal
    must not override the proxy demotion."""
    rec = _make_record(fee=3000, hooks=1 << 7)
    bytecode = _eip1167_bytecode(51)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="b" * 64,
        previous_code_hash="a" * 64,
        bytecode=bytecode,
        is_eip1167_proxy=True,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert decision.level == RunMode.REJECTED
    assert decision.has(EligibilityReasonCode.PROXY_DETECTED)
    assert decision.has(EligibilityReasonCode.BYTECODE_HASH_CHANGE)


# ---------------------------------------------------------------------------
# Classifier: unknown hook (no bytecode) stays at ``ingestion``
# ---------------------------------------------------------------------------


def test_nonzero_hook_without_bytecode_is_ingestion_not_promoted() -> None:
    """A non-zero hook with no bytecode retrieval stays at
    ``ingestion``. The T023 must-not rule forbids auto-promoting an
    unknown hook to backtest / paper / live."""
    rec = _make_record(fee=3000, hooks=1 << 7)
    decision = classify_pool(rec)
    assert decision.level == RunMode.INGESTION
    assert decision.has(EligibilityReasonCode.HOOK_ADDRESS_PRESENT)
    assert decision.has(EligibilityReasonCode.HOOK_BEHAVIOUR_UNKNOWN)
    # No evidence of any "permission to simulate" reason -- not
    # STATIC_FEE_PLAIN_POOL promoted to BACKTEST, no proxy demote.
    assert not decision.has(EligibilityReasonCode.PROXY_DETECTED)
    assert not decision.has(EligibilityReasonCode.BYTECODE_HASH_CHANGE)


def test_nonzero_hook_with_code_hash_but_no_bytecode_emits_unavailable() -> None:
    """A hook whose code hash was recorded but whose raw bytecode
    was not retrieved surfaces ``HOOK_BYTECODE_UNAVAILABLE``. This
    is the explicit "no bytecode retrieval possible" signal the
    classifier records when only the SHA-256 is known."""
    rec = _make_record(fee=3000, hooks=1 << 7)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="a" * 64,
        bytecode=None,
        is_eip1167_proxy=None,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert decision.level == RunMode.INGESTION
    assert decision.has(EligibilityReasonCode.HOOK_CODE_HASH_PINNED)
    assert decision.has(EligibilityReasonCode.HOOK_BYTECODE_UNAVAILABLE)


def test_nonzero_hook_with_bytecode_does_not_emit_unavailable() -> None:
    """When the framework has supplied the raw bytecode the
    ``HOOK_BYTECODE_UNAVAILABLE`` reason must not appear -- the
    bytecode *is* available; only the proxy / not-proxy outcome is
    unknown."""
    rec = _make_record(fee=3000, hooks=1 << 7)
    bytecode = b"\x60\x80\x60\x40" + b"\x00" * 60  # 64 bytes
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="a" * 64,
        bytecode=bytecode,
        is_eip1167_proxy=False,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert not decision.has(EligibilityReasonCode.HOOK_BYTECODE_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Classifier: evidence pointers (audit trail)
# ---------------------------------------------------------------------------


def test_decision_carries_evidence_pointers() -> None:
    """Every decision has a non-empty ``evidence_pointers`` list (T023
    acceptance: every row gets a level plus evidence)."""
    rec = _make_record(
        fee=3000,
        hooks=0,
        token0_meta=_complete_meta("0x" + "11" * 20),
        token1_meta=_complete_meta("0x" + "22" * 20),
    )
    decision = classify_pool(rec)
    assert isinstance(decision.evidence_pointers, list)
    assert len(decision.evidence_pointers) >= 1


def test_evidence_pointers_include_pool_id_fee_and_hooks() -> None:
    """The default evidence pointers include the pool id, the fee,
    and the hooks address -- the minimum audit trail for every row."""
    rec = _make_record(fee=3000, hooks=0)
    decision = classify_pool(rec)
    joined = "\n".join(decision.evidence_pointers)
    assert "pool_id=" in joined
    assert "fee=3000" in joined
    assert "hooks=0x" in joined


def test_evidence_pointers_record_hook_code_hash_when_pinned() -> None:
    """When the hook evidence carries a code hash the audit trail
    records it as a pointer."""
    rec = _make_record(fee=3000, hooks=1 << 7)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="a" * 64,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert any(p.startswith("hook_code_hash=") for p in decision.evidence_pointers)


def test_evidence_pointers_record_proxy_shape() -> None:
    """A PROXY_DETECTED decision records the EIP-1167 shape in its
    evidence pointers so the audit trail names the bytecode shape."""
    rec = _make_record(fee=3000, hooks=1 << 7)
    bytecode = _eip1167_bytecode(51)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="a" * 64,
        bytecode=bytecode,
        is_eip1167_proxy=True,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert any("eip1167_minimal_proxy" in p for p in decision.evidence_pointers)


def test_evidence_pointers_record_hash_change() -> None:
    """A BYTECODE_HASH_CHANGE decision records the change flag in
    its evidence pointers."""
    rec = _make_record(fee=3000, hooks=1 << 7)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="b" * 64,
        previous_code_hash="a" * 64,
    )
    decision = classify_pool(rec, hook_evidence=evidence)
    assert any("hook_code_hash_changed=true" in p for p in decision.evidence_pointers)


def test_evidence_pointers_are_deterministic_across_calls() -> None:
    """Two calls with identical inputs produce identical evidence
    pointer lists (T023 acceptance: deterministic)."""
    rec = _make_record(fee=3000, hooks=1 << 7)
    evidence = HookEvidence(
        address=Address(1 << 7),
        is_zero=False,
        has_any_flag=True,
        has_delta_flag=False,
        code_hash="a" * 64,
        bytecode=_eip1167_bytecode(51),
        is_eip1167_proxy=True,
        previous_code_hash="b" * 64,
    )
    a = classify_pool(rec, hook_evidence=evidence)
    b = classify_pool(rec, hook_evidence=evidence)
    assert a.evidence_pointers == b.evidence_pointers


# ---------------------------------------------------------------------------
# Reason-code export
# ---------------------------------------------------------------------------


def test_new_reason_codes_are_exported() -> None:
    """The T023 acceptance codes ``proxy_detected`` and
    ``bytecode_hash_change`` must be reachable on the enum."""
    assert EligibilityReasonCode.PROXY_DETECTED.value == "proxy_detected"
    assert EligibilityReasonCode.BYTECODE_HASH_CHANGE.value == "bytecode_hash_change"
    assert EligibilityReasonCode.HOOK_BYTECODE_UNAVAILABLE.value == "hook_bytecode_unavailable"
