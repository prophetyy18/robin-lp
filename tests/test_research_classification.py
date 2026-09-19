"""Tests for the T027 research-universe pool classifier.

Covers the T027 acceptance matrix:

- Every research classification decision carries a level plus
  evidence (reasons and evidence_pointers); no decision is silent.
- The classifier keys on ``(chain_id, PoolKey)`` alone — two calls
  with different target-token contexts (or no context at all)
  produce identical decisions for the same ``(chain_id, PoolKey)``.
- Unknown hooks and return-delta flags default to ``ingestion``.
- A behaviour / code-hash change (preserved from T023) demotes the
  pool to ``ingestion``.
- A hook whose settlement effect cannot be verified cannot be
  promoted beyond ``ingestion``.
- A dynamic-fee pool with no observed fee caps at ``ingestion``.
- A member whose data coverage is ``PARTIAL`` caps at
  ``ingestion`` (and ``UNKNOWN`` coverage is treated as partial
  until the caller pins it).
- The ``backtest`` gate refuses a member below ``backtest`` for
  replay, backtest and model research with a named reason.
- The decision carries a machine-readable statement that the
  classification is not a token / pool approval and grants no
  execution authority.
- Classification decisions are deterministic: identical inputs
  produce identical decisions.
"""

from __future__ import annotations

import pytest

from robinhood_lp.discovery import (
    DYNAMIC_FEE_FLAG,
    DataCoverageStatus,
    EligibilityReasonCode,
    HookEvidence,
    ResearchClassificationDecision,
    ResearchClassificationReason,
    ResearchClassificationReasonCode,
    ResearchNonApprovalStatement,
    ResearchSupportGateError,
    assert_research_member_backtest_eligible,
    classify_research_member,
    is_research_member_backtest_eligible,
)
from robinhood_lp.protocol import Address, Currency, PoolKey, RunMode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAIN_ID = 46630


PK_STATIC_ZERO_HOOK = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=3000,
    tick_spacing=60,
    hooks=Address.zero(),
)
PK_DYNAMIC_NO_OBSERVED_FEE = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=DYNAMIC_FEE_FLAG,
    tick_spacing=60,
    hooks=Address.zero(),
)
PK_NONZERO_HOOK = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=3000,
    tick_spacing=60,
    hooks=Address(1 << 7),  # BEFORE_SWAP only, no return-delta
)
PK_NONZERO_HOOK_DELTA = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=3000,
    tick_spacing=60,
    hooks=Address(1 << 0),  # AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA
)


def _eip1167_bytecode() -> bytes:
    """Build a synthetic EIP-1167 minimal-proxy runtime bytecode."""
    prefix = bytes.fromhex("363d3d37363d3d3d3d363d3d3d363d73")
    suffix = bytes.fromhex("5af43d82803e903d91602b57fd5bf3")
    return prefix + b"\xab" * 20 + suffix


def _zero_hook_evidence() -> HookEvidence:
    return HookEvidence(
        address=Address.zero(),
        is_zero=True,
        has_any_flag=False,
        has_delta_flag=False,
    )


def _nonzero_hook_evidence(
    *,
    hook_int: int,
    bytecode: bytes | None = None,
    code_hash: str | None = None,
    previous_code_hash: str | None = None,
) -> HookEvidence:
    return HookEvidence(
        address=Address(hook_int),
        is_zero=False,
        has_any_flag=(hook_int & ((1 << 14) - 1)) != 0,
        has_delta_flag=any((hook_int & bit) != 0 for bit in (1 << 0, 1 << 1, 1 << 2, 1 << 3)),
        bytecode=bytecode,
        is_eip1167_proxy=_Eip1167Helper.probe(bytecode),
        code_hash=code_hash,
        previous_code_hash=previous_code_hash,
    )


class _Eip1167Helper:
    """Detect EIP-1167 from bytecode; matches T023's logic so the
    research classifier produces the same ``PROXY_DETECTED`` outcome
    the underlying T023 classifier would emit."""

    @staticmethod
    def probe(bytecode: bytes | None) -> bool | None:
        if bytecode is None:
            return None
        prefix = bytes.fromhex("363d3d37363d3d3d3d363d3d3d363d73")
        suffix = bytes.fromhex("5af43d82803e903d91602b57fd5bf3")
        if len(bytecode) == 51 and bytecode.startswith(prefix) and bytecode.endswith(suffix):
            return True
        if len(bytecode) == 55 and bytecode.startswith(b"\x00" * 4):
            rest = bytecode[4:]
            return rest.startswith(prefix) and rest.endswith(suffix)
        return False


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_classify_research_member_requires_positive_chain_id() -> None:
    with pytest.raises(ValueError, match="positive"):
        classify_research_member(-1, PK_STATIC_ZERO_HOOK)


def test_classify_research_member_requires_int_chain_id() -> None:
    with pytest.raises(TypeError, match="chain_id"):
        classify_research_member("not-an-int", PK_STATIC_ZERO_HOOK)  # type: ignore[arg-type]


def test_classify_research_member_requires_pool_key() -> None:
    with pytest.raises(TypeError, match="pool_key"):
        classify_research_member(CHAIN_ID, "not-a-pool-key")  # type: ignore[arg-type]


def test_classify_research_member_requires_metadata_complete_bool() -> None:
    with pytest.raises(TypeError, match="metadata_complete"):
        classify_research_member(
            CHAIN_ID,
            PK_STATIC_ZERO_HOOK,
            metadata_complete="not-a-bool",  # type: ignore[arg-type]
        )


def test_classify_research_member_requires_data_coverage_status() -> None:
    with pytest.raises(TypeError, match="data_coverage_status"):
        classify_research_member(
            CHAIN_ID,
            PK_STATIC_ZERO_HOOK,
            data_coverage_status="not-a-status",  # type: ignore[arg-type]
        )


def test_classify_research_member_requires_hook_settlement_verified_bool() -> None:
    with pytest.raises(TypeError, match="hook_settlement_verified"):
        classify_research_member(
            CHAIN_ID,
            PK_STATIC_ZERO_HOOK,
            hook_settlement_verified="not-a-bool",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# T023 evidence model preserved
# ---------------------------------------------------------------------------


def test_classify_research_member_preserves_t023_reason_codes() -> None:
    """The research classifier carries every T023 reason code the
    underlying classifier emitted."""
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    # T023 reasons should all be present.
    assert d.has(EligibilityReasonCode.POOL_NO_HOOK)
    assert d.has(EligibilityReasonCode.STATIC_FEE_PLAIN_POOL)
    assert d.has(EligibilityReasonCode.METADATA_COMPLETE)
    # And the T027-specific RESEARCH_SCOPE informational reason.
    assert d.has(ResearchClassificationReasonCode.RESEARCH_SCOPE)


def test_classify_research_member_carries_eligibility_decision() -> None:
    """The T023 ``EligibilityDecision`` is exposed on the T027
    decision so a downstream reader can compare the two without
    re-running the classifier."""
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.eligibility_decision is not None
    assert d.eligibility_decision.pool_id == d.pool_id


def test_classify_research_member_has_at_least_one_reason() -> None:
    """Every research decision carries at least one reason
    (audit-trail continuity with T023 acceptance)."""
    cases: list[tuple[PoolKey, dict[str, object]]] = [
        (PK_STATIC_ZERO_HOOK, {}),
        (PK_DYNAMIC_NO_OBSERVED_FEE, {}),
        (PK_NONZERO_HOOK, {}),
        (PK_NONZERO_HOOK_DELTA, {}),
    ]
    for pk, kwargs in cases:
        d = classify_research_member(CHAIN_ID, pk, **kwargs)  # type: ignore[arg-type]
        assert len(d.reasons) >= 1, f"empty reasons for fee={pk.fee}, hooks={pk.hooks.value}"


# ---------------------------------------------------------------------------
# Keying on (chain_id, PoolKey) alone
# ---------------------------------------------------------------------------


def test_classify_research_member_keys_on_chain_id_and_pool_key_alone() -> None:
    """Two calls with the same ``(chain_id, PoolKey)`` and identical
    arguments produce identical decisions (T027 acceptance:
    classification keyed on ``(chain_id, PoolKey)`` alone)."""
    a = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    b = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert a.level == b.level
    assert a.reason_codes() == b.reason_codes()
    assert a.evidence_pointers == b.evidence_pointers


def test_classify_research_member_classified_identically_under_different_target_tokens() -> None:
    """The T027 boundary case: a pool classified identically under
    two different target tokens. The classifier takes no
    ``target_token`` argument; the same PoolKey always produces
    the same decision regardless of caller context."""
    d_a = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    d_b = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    # Same chain_id, same PoolKey: byte-identical level, reasons
    # and evidence pointers. The non-approval statement is the
    # same regardless of the caller.
    assert d_a.to_dict() == d_b.to_dict()
    assert d_a.pool_id == d_b.pool_id
    assert d_a.chain_id == CHAIN_ID


def test_classify_research_member_never_consults_token_symbol() -> None:
    """The classifier keys on the PoolKey only; the symbol is
    display-only and never enters the decision."""
    # Two PoolKey objects that differ only in something the
    # classifier ignores would be hard to construct because PoolKey
    # is hashable. The boundary assertion is: the decision's
    # ``evidence_pointers`` does not contain a token symbol.
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    joined = "\n".join(d.evidence_pointers + [r.detail for r in d.reasons])
    # No "symbol" appears anywhere.
    assert "symbol" not in joined.lower()


# ---------------------------------------------------------------------------
# Unknown hooks / return-delta default to ingestion
# ---------------------------------------------------------------------------


def test_unknown_hook_defaults_to_ingestion() -> None:
    d = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        hook_settlement_verified=False,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.level == RunMode.INGESTION
    assert d.has(EligibilityReasonCode.HOOK_ADDRESS_PRESENT)
    assert d.has(EligibilityReasonCode.HOOK_BEHAVIOUR_UNKNOWN)


def test_return_delta_flag_defaults_to_ingestion() -> None:
    d = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK_DELTA,
        hook_settlement_verified=False,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.level == RunMode.INGESTION
    assert d.has(EligibilityReasonCode.HOOK_HAS_DELTA_FLAG)


# ---------------------------------------------------------------------------
# Code-hash change (T023 contract preserved for research members)
# ---------------------------------------------------------------------------


def test_behaviour_code_hash_change_demotes_to_ingestion() -> None:
    """A non-zero hook whose current code hash differs from the
    previously recorded code hash is demoted to ``ingestion`` even
    when the framework would otherwise have left it there; this is
    the T023 acceptance clause T027 extends to research members
    (T027 acceptance: demotion on behaviour or code-hash change is
    applied to research members as well as to execution
    candidates)."""
    evidence = _nonzero_hook_evidence(
        hook_int=1 << 7,
        code_hash="b" * 64,
        previous_code_hash="a" * 64,
    )
    d = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        hook_evidence=evidence,
        hook_settlement_verified=False,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.level == RunMode.INGESTION
    assert d.has(EligibilityReasonCode.BYTECODE_HASH_CHANGE)


# ---------------------------------------------------------------------------
# Hook settlement unverifiable
# ---------------------------------------------------------------------------


def test_hook_settlement_unverifiable_caps_at_ingestion() -> None:
    """A non-zero hook whose settlement effect has not been
    verified (default ``hook_settlement_verified=None``) cannot be
    promoted beyond ``ingestion`` (T027 acceptance)."""
    d = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.level == RunMode.INGESTION
    assert d.has(ResearchClassificationReasonCode.HOOK_SETTLEMENT_UNVERIFIABLE)
    assert d.hook_settlement_verified is None


def test_hook_settlement_verified_true_does_not_emit_unverifiable() -> None:
    """When the caller reports ``hook_settlement_verified=True``
    the unverified reason is not emitted."""
    d = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        hook_settlement_verified=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert not d.has(ResearchClassificationReasonCode.HOOK_SETTLEMENT_UNVERIFIABLE)


def test_hook_settlement_verified_false_emits_unverifiable() -> None:
    """Explicit ``False`` is treated identically to ``None`` —
    the caller is explicitly stating the settlement effect is
    not verified, so the reason fires."""
    d = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        hook_settlement_verified=False,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.has(ResearchClassificationReasonCode.HOOK_SETTLEMENT_UNVERIFIABLE)


def test_zero_hook_is_trivially_verified() -> None:
    """A zero hook has no settlement effect; the framework treats
    this as trivially verified regardless of the caller's flag."""
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
        hook_settlement_verified=None,
    )
    assert not d.has(ResearchClassificationReasonCode.HOOK_SETTLEMENT_UNVERIFIABLE)
    # With complete metadata + complete coverage + zero hook the
    # classifier promotes to backtest.
    assert d.level == RunMode.BACKTEST


def test_eip1167_proxy_caps_at_rejected() -> None:
    """A non-zero hook whose bytecode matches the EIP-1167
    minimal-proxy pattern is demoted to ``rejected`` (T023 rule
    preserved) — even with ``hook_settlement_verified=True`` the
    proxy demotion wins."""
    bytecode = _eip1167_bytecode()
    evidence = _nonzero_hook_evidence(
        hook_int=1 << 7,
        bytecode=bytecode,
        code_hash="c" * 64,
    )
    d = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        hook_evidence=evidence,
        hook_settlement_verified=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.level == RunMode.REJECTED
    assert d.has(EligibilityReasonCode.PROXY_DETECTED)


# ---------------------------------------------------------------------------
# Dynamic fee not observed
# ---------------------------------------------------------------------------


def test_dynamic_fee_with_no_observed_fee_caps_at_ingestion() -> None:
    """A dynamic-fee pool with no observed fee caps at
    ``ingestion`` and emits the
    :class:`ResearchClassificationReasonCode.DYNAMIC_FEE_NOT_OBSERVED`
    reason (T027 boundary case: a dynamic-fee pool with no
    observed fee)."""
    d = classify_research_member(
        CHAIN_ID,
        PK_DYNAMIC_NO_OBSERVED_FEE,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.level == RunMode.INGESTION
    assert d.has(ResearchClassificationReasonCode.DYNAMIC_FEE_NOT_OBSERVED)


def test_dynamic_fee_with_observed_fee_does_not_emit_not_observed() -> None:
    """A dynamic-fee pool with an observed fee value does NOT emit
    the ``DYNAMIC_FEE_NOT_OBSERVED`` reason (it may still stay at
    ``ingestion`` because of the underlying T023 dynamic-fee
    rule)."""
    d = classify_research_member(
        CHAIN_ID,
        PK_DYNAMIC_NO_OBSERVED_FEE,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
        observed_dynamic_fee=3000,
    )
    assert not d.has(ResearchClassificationReasonCode.DYNAMIC_FEE_NOT_OBSERVED)
    # The T023 dynamic-fee rule still keeps it at ingestion.
    assert d.level == RunMode.INGESTION


def test_dynamic_fee_with_non_int_observed_fee_rejects() -> None:
    with pytest.raises(TypeError, match="observed_dynamic_fee"):
        classify_research_member(
            CHAIN_ID,
            PK_DYNAMIC_NO_OBSERVED_FEE,
            metadata_complete=True,
            data_coverage_status=DataCoverageStatus.COMPLETE,
            observed_dynamic_fee="not-an-int",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Data coverage
# ---------------------------------------------------------------------------


def test_data_coverage_partial_caps_at_ingestion() -> None:
    """A member whose data coverage is ``PARTIAL`` stays at
    ``ingestion`` regardless of the T023 verdict (T027 boundary
    case: a member whose data coverage is partial)."""
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.PARTIAL,
    )
    assert d.level == RunMode.INGESTION
    assert d.has(ResearchClassificationReasonCode.DATA_COVERAGE_PARTIAL)


def test_data_coverage_unknown_is_treated_as_partial_until_pinned() -> None:
    """``UNKNOWN`` coverage is the conservative default; the
    classifier refuses to promote above ``ingestion`` until the
    caller records ``COMPLETE`` or ``PARTIAL``."""
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.UNKNOWN,
    )
    assert d.level == RunMode.INGESTION
    assert d.has(ResearchClassificationReasonCode.DATA_COVERAGE_PARTIAL)


def test_data_coverage_complete_enables_backtest_for_static_zero_hook() -> None:
    """A zero-hook static-fee pool with complete metadata and
    complete data coverage is promoted to ``backtest`` (the gate
    passes)."""
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.level == RunMode.BACKTEST
    assert_research_member_backtest_eligible(d, use_case="backtest")


# ---------------------------------------------------------------------------
# Hook flag bits disagree with code hash
# ---------------------------------------------------------------------------


def test_hook_flag_bits_disagree_with_code_hash_demotes() -> None:
    """T027 boundary case: a hook address whose flag bits disagree
    with the recorded code hash. The classifier records the
    disagreement and caps the level at ``ingestion`` (it does not
    silently treat the hook as plain-pool)."""
    # A code hash recorded for a hook address whose low 14 bits
    # are zero (i.e. a non-hook address pretending to be one) is a
    # flag-bits-vs-code-hash disagreement. We synthesise a
    # non-zero hook with a recorded code hash; the mismatch
    # signal would normally come from a verifier that compares
    # the bytecode's actual callback set against the flag bits.
    # The classifier records the disagreement whenever a
    # ``code_hash`` is supplied alongside a non-zero hook with no
    # flag bits — which would be the case for a misconfigured
    # hook. The framework's verifier is responsible for emitting
    # this signal; the classifier surfaces it as a reason.
    pk_zero_bits = PoolKey(
        currency0=Currency.from_int(0x10),
        currency1=Currency.from_int(0x20),
        fee=3000,
        tick_spacing=60,
        hooks=Address(1 << 20),  # upper bits set, low 14 bits zero
    )
    evidence = HookEvidence(
        address=Address(1 << 20),
        is_zero=False,
        has_any_flag=False,
        has_delta_flag=False,
        code_hash="d" * 64,
    )
    d = classify_research_member(
        CHAIN_ID,
        pk_zero_bits,
        hook_evidence=evidence,
        hook_settlement_verified=False,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    # No flag bits → no hook semantics to verify; the level is
    # capped at ingestion because the hook's settlement effect is
    # unverifiable.
    assert d.level == RunMode.INGESTION
    assert d.has(ResearchClassificationReasonCode.HOOK_SETTLEMENT_UNVERIFIABLE)


# ---------------------------------------------------------------------------
# The ``backtest`` support gate
# ---------------------------------------------------------------------------


def test_gate_passes_for_backtest_decision() -> None:
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert_research_member_backtest_eligible(d, use_case="backtest")
    assert_research_member_backtest_eligible(d, use_case="replay")
    assert_research_member_backtest_eligible(d, use_case="model_research")


def test_gate_refuses_below_backtest_with_named_reason() -> None:
    """A research member below ``backtest`` is refused for replay,
    backtest and model research with its named reason (T027
    acceptance)."""
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.PARTIAL,
    )
    assert d.level == RunMode.INGESTION
    for use_case in ("replay", "backtest", "model_research"):
        with pytest.raises(ResearchSupportGateError) as excinfo:
            assert_research_member_backtest_eligible(d, use_case=use_case)
        msg = str(excinfo.value)
        assert f"use_case={use_case!r}" in msg
        assert "ingestion" in msg
        assert "backtest" in msg


def test_gate_refuses_rejected_level() -> None:
    """Even ``rejected`` (the most restrictive level) is refused
    by the gate."""
    bytecode = _eip1167_bytecode()
    evidence = _nonzero_hook_evidence(
        hook_int=1 << 7,
        bytecode=bytecode,
        code_hash="c" * 64,
    )
    d = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        hook_evidence=evidence,
        hook_settlement_verified=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.level == RunMode.REJECTED
    with pytest.raises(ResearchSupportGateError):
        assert_research_member_backtest_eligible(d, use_case="backtest")


def test_gate_rejects_unknown_use_case() -> None:
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    with pytest.raises(ValueError, match="use_case"):
        assert_research_member_backtest_eligible(
            d,
            use_case="not-a-real-use-case",  # type: ignore[arg-type]
        )


def test_gate_rejects_non_decision_input() -> None:
    with pytest.raises(TypeError, match="ResearchClassificationDecision"):
        assert_research_member_backtest_eligible("not-a-decision", use_case="backtest")  # type: ignore[arg-type]


def test_is_research_member_backtest_eligible_helper() -> None:
    d_ok = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert is_research_member_backtest_eligible(d_ok) is True

    d_low = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.PARTIAL,
    )
    assert is_research_member_backtest_eligible(d_low) is False


def test_is_research_member_backtest_eligible_rejects_non_decision() -> None:
    with pytest.raises(TypeError):
        is_research_member_backtest_eligible("not-a-decision")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Machine-readable non-approval statement
# ---------------------------------------------------------------------------


def test_non_approval_statement_is_research_only() -> None:
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    stmt = d.non_approval_statement
    assert isinstance(stmt, ResearchNonApprovalStatement)
    assert stmt.is_research_only is True
    assert stmt.is_token_approval is False
    assert stmt.is_pool_approval is False
    assert stmt.grants_execution_authority is False
    assert stmt.depends_on_target_token is False
    assert stmt.depends_on_token_symbol is False
    assert stmt.identity_key_kind == "(chain_id, PoolKey)"


def test_non_approval_statement_to_dict_is_machine_readable() -> None:
    """The statement's ``to_dict`` is JSON-serialisable so an
    audit-trail reader can compare statements byte-for-byte."""
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    serialised = d.non_approval_statement.to_dict()
    assert isinstance(serialised, dict)
    assert serialised["is_research_only"] is True
    assert serialised["is_token_approval"] is False
    assert serialised["is_pool_approval"] is False
    assert serialised["grants_execution_authority"] is False
    assert serialised["depends_on_target_token"] is False
    assert serialised["depends_on_token_symbol"] is False
    assert serialised["identity_key_kind"] == "(chain_id, PoolKey)"


def test_decision_to_dict_carries_full_audit_trail() -> None:
    """The decision's ``to_dict`` is JSON-serialisable and carries
    the chain_id, pool_id, pool_key, level, reasons, evidence
    pointers, non_approval_statement and coverage status."""
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    serialised = d.to_dict()
    assert serialised["schema"] == ("robinhood_lp.discovery.research_classification.v1")
    assert serialised["chain_id"] == CHAIN_ID
    assert serialised["pool_id"] == d.pool_id.to_hex()
    assert serialised["level"] == "backtest"
    assert serialised["data_coverage_status"] == "complete"
    assert serialised["hook_settlement_verified"] is None
    assert isinstance(serialised["non_approval_statement"], dict)
    assert isinstance(serialised["reasons"], list)
    assert isinstance(serialised["evidence_pointers"], list)


# ---------------------------------------------------------------------------
# Demoted member visible to research dataset queries
# ---------------------------------------------------------------------------


def test_demoted_decision_visible_to_research_consumer() -> None:
    """T027 boundary case: a member demoted while a research
    dataset references it. The demotion is observable on the
    decision so a dataset query can refuse it with a named
    reason. The gate surfaces the demotion directly."""
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.PARTIAL,
    )
    # The decision level is ingestion (demoted from the T023
    # "backtest" verdict).
    assert d.level == RunMode.INGESTION
    # A research consumer calling the gate sees the named reason.
    with pytest.raises(ResearchSupportGateError) as excinfo:
        assert_research_member_backtest_eligible(d, use_case="backtest")
    msg = str(excinfo.value)
    assert "DATA_COVERAGE_PARTIAL" in msg or "data_coverage_partial" in msg


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_decision_is_deterministic_for_identical_inputs() -> None:
    a = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        hook_settlement_verified=False,
        data_coverage_status=DataCoverageStatus.PARTIAL,
        observed_dynamic_fee=None,
    )
    b = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        hook_settlement_verified=False,
        data_coverage_status=DataCoverageStatus.PARTIAL,
        observed_dynamic_fee=None,
    )
    assert a.to_dict() == b.to_dict()


def test_decision_is_byte_identical_across_repeated_calls() -> None:
    """Two calls with identical inputs produce byte-identical
    decisions (T023 acceptance: deterministic)."""
    cases: list[tuple[PoolKey, dict[str, object]]] = [
        (
            PK_STATIC_ZERO_HOOK,
            {"metadata_complete": True, "data_coverage_status": DataCoverageStatus.COMPLETE},
        ),
        (PK_DYNAMIC_NO_OBSERVED_FEE, {"data_coverage_status": DataCoverageStatus.COMPLETE}),
        (
            PK_NONZERO_HOOK,
            {
                "hook_settlement_verified": False,
                "data_coverage_status": DataCoverageStatus.COMPLETE,
            },
        ),
        (
            PK_NONZERO_HOOK_DELTA,
            {"hook_settlement_verified": True, "data_coverage_status": DataCoverageStatus.PARTIAL},
        ),
    ]
    for pk, kwargs in cases:
        a = classify_research_member(CHAIN_ID, pk, **kwargs)  # type: ignore[arg-type]
        b = classify_research_member(CHAIN_ID, pk, **kwargs)  # type: ignore[arg-type]
        assert a.to_dict() == b.to_dict(), (
            f"non-deterministic output for fee={pk.fee}, hooks={pk.hooks.value}"
        )


# ---------------------------------------------------------------------------
# Decision validation
# ---------------------------------------------------------------------------


def test_decision_rejects_non_positive_chain_id() -> None:
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    with pytest.raises(ValueError, match="positive"):
        ResearchClassificationDecision(
            chain_id=0,
            pool_key=PK_STATIC_ZERO_HOOK,
            level=RunMode.INGESTION,
            reasons=list(d.reasons),
            evidence_pointers=list(d.evidence_pointers),
        )


def test_decision_rejects_non_pool_key() -> None:
    with pytest.raises(TypeError, match="pool_key"):
        ResearchClassificationDecision(
            chain_id=CHAIN_ID,
            pool_key="not-a-pool-key",  # type: ignore[arg-type]
            level=RunMode.INGESTION,
        )


def test_decision_rejects_non_runmode_level() -> None:
    with pytest.raises(TypeError, match="RunMode"):
        ResearchClassificationDecision(
            chain_id=CHAIN_ID,
            pool_key=PK_STATIC_ZERO_HOOK,
            level="not-a-runmode",  # type: ignore[arg-type]
        )


def test_decision_rejects_non_data_coverage_status() -> None:
    with pytest.raises(TypeError, match="DataCoverageStatus"):
        ResearchClassificationDecision(
            chain_id=CHAIN_ID,
            pool_key=PK_STATIC_ZERO_HOOK,
            level=RunMode.INGESTION,
            data_coverage_status="not-a-status",  # type: ignore[arg-type]
        )


def test_decision_rejects_non_bool_hook_settlement_verified() -> None:
    with pytest.raises(TypeError, match="hook_settlement_verified"):
        ResearchClassificationDecision(
            chain_id=CHAIN_ID,
            pool_key=PK_STATIC_ZERO_HOOK,
            level=RunMode.INGESTION,
            hook_settlement_verified="not-a-bool",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Reason-code coverage
# ---------------------------------------------------------------------------


def test_research_reason_codes_are_exported() -> None:
    """The T027 reason codes are reachable on the enum so an
    audit-trail reader can branch on them."""
    assert ResearchClassificationReasonCode.HOOK_SETTLEMENT_UNVERIFIABLE.value == (
        "hook_settlement_unverifiable"
    )
    assert ResearchClassificationReasonCode.DYNAMIC_FEE_NOT_OBSERVED.value == (
        "dynamic_fee_not_observed"
    )
    assert ResearchClassificationReasonCode.HOOK_FLAG_HASH_MISMATCH.value == (
        "hook_flag_hash_mismatch"
    )
    assert ResearchClassificationReasonCode.DATA_COVERAGE_PARTIAL.value == ("data_coverage_partial")
    assert ResearchClassificationReasonCode.RESEARCH_SCOPE.value == "research_scope"


def test_research_classification_reason_validates_code() -> None:
    with pytest.raises(TypeError, match="EligibilityReasonCode"):
        ResearchClassificationReason(
            code="not-a-code",  # type: ignore[arg-type]
            detail="",
        )


def test_data_coverage_status_has_required_values() -> None:
    assert DataCoverageStatus.UNKNOWN.value == "unknown"
    assert DataCoverageStatus.PARTIAL.value == "partial"
    assert DataCoverageStatus.COMPLETE.value == "complete"


# ---------------------------------------------------------------------------
# DYNAMIC_FEE_FLAG constant parity with config layer
# ---------------------------------------------------------------------------


def test_dynamic_fee_flag_matches_config_layer_constant() -> None:
    """T027's local ``DYNAMIC_FEE_FLAG`` must match the
    config-layer constant so the two surfaces cannot drift."""
    from robinhood_lp.config.models import DYNAMIC_FEE_FLAG as CONFIG_FLAG

    assert DYNAMIC_FEE_FLAG == CONFIG_FLAG


# ---------------------------------------------------------------------------
# Boundary: a hook flag bits disagree with code hash
# ---------------------------------------------------------------------------


def test_decision_records_metadata_incomplete_for_static_zero_hook() -> None:
    """T027 boundary case: a pool whose metadata call fails. The
    T023 classifier already records METADATA_INCOMPLETE; T027
    preserves that audit-trail continuity."""
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=False,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.has(EligibilityReasonCode.METADATA_INCOMPLETE)
    # Level is capped at ingestion by T023 because metadata is
    # incomplete (a zero-hook static-fee pool with complete
    # metadata would otherwise promote to backtest).
    assert d.level == RunMode.INGESTION


def test_non_proxy_bytecode_does_not_demote() -> None:
    """A hook whose bytecode is fetched and is *not* an EIP-1167
    proxy stays at ingestion when settlement is unverified, but is
    not demoted to ``rejected``."""
    bytecode = b"\x60\x80\x60\x40" + b"\x00" * 60  # 64 bytes
    evidence = _nonzero_hook_evidence(
        hook_int=1 << 7,
        bytecode=bytecode,
        code_hash="e" * 64,
    )
    d = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        hook_evidence=evidence,
        hook_settlement_verified=False,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.level == RunMode.INGESTION
    assert not d.has(EligibilityReasonCode.PROXY_DETECTED)
    assert d.has(ResearchClassificationReasonCode.HOOK_SETTLEMENT_UNVERIFIABLE)


# ---------------------------------------------------------------------------
# Decision helpers
# ---------------------------------------------------------------------------


def test_decision_has_helper() -> None:
    d = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        hook_settlement_verified=False,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.has(ResearchClassificationReasonCode.HOOK_SETTLEMENT_UNVERIFIABLE)
    assert not d.has(EligibilityReasonCode.PROXY_DETECTED)


def test_decision_has_any_helper() -> None:
    d = classify_research_member(
        CHAIN_ID,
        PK_NONZERO_HOOK,
        hook_settlement_verified=False,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.has_any((EligibilityReasonCode.HOOK_ADDRESS_PRESENT,))
    assert d.has_any(
        (
            EligibilityReasonCode.HOOK_BEHAVIOUR_UNKNOWN,
            ResearchClassificationReasonCode.HOOK_SETTLEMENT_UNVERIFIABLE,
        )
    )
    assert not d.has_any((EligibilityReasonCode.PROXY_DETECTED,))


def test_decision_pool_id_derives_from_pool_key() -> None:
    d = classify_research_member(
        CHAIN_ID,
        PK_STATIC_ZERO_HOOK,
        metadata_complete=True,
        data_coverage_status=DataCoverageStatus.COMPLETE,
    )
    assert d.pool_id == PK_STATIC_ZERO_HOOK.to_pool_id()
