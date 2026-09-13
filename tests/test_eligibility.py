"""Tests for the eligibility classifier (T023).

Covers the T023 acceptance matrix:
- every registry row has a level plus evidence
- static-fee plain pool + complete metadata -> backtest
- dynamic-fee pool stays at ingestion
- nonzero hook address defaults to ingestion with hook evidence
- return-delta flag in hook address -> HOOK_HAS_DELTA_FLAG reason
- metadata incomplete -> METADATA_INCOMPLETE reason + ingestion
- upgrade / proxy observed -> capped at ingestion
- decisions are deterministic (same inputs -> same outputs)
"""

from __future__ import annotations

from typing import Any

import pytest

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
        assert len(decision.reasons) >= 1, f"empty reasons for fee={rec.pool_key.fee}, hooks={rec.pool_key.hooks.value}"


# ---------------------------------------------------------------------------
# Cross-check with the constants
# ---------------------------------------------------------------------------


def test_delta_flag_bits_constant() -> None:
    """The module's DELTA_FLAG_BITS covers bits 0..3 of the low 14."""
    assert DELTA_FLAG_BITS == (1 << 0, 1 << 1, 1 << 2, 1 << 3)
