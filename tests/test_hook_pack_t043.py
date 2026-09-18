"""Tests for the T043 hook-semantics evidence packs.

The T043 contract names two acceptance paths:

1. **Heterogeneous pack record.** Pools whose ``hooks`` are nonzero
   each carry a :class:`HookPack` with the hook address / code
   hash / proxy implementation / upgrade authority, the decoded
   flag bits, a verified source / ABI record, the before / after
   delta paths, the dynamic-fee behaviour, external state dependencies,
   a replay model, adversarial tests, and an invalidation rule.
   The pack's invalidation rule demotes the pool when the hook's
   code hash changes.

2. **Empty-set evidence.** When no included pool has nonzero
   hooks, T043 still produces a single reviewable artifact:
   the per-pool T023 classification outcome for each included
   pool, the reason no hook pack exists for each pool, and an
   explicit statement that no hook semantics were verified by
   this task.

The tests cover the T043 acceptance matrix:

- empty-set evidence path produces the correct artifact with
  per-pool classification outcomes and the explicit no-verification
  statement;
- pool with nonzero hooks produces a hook pack with every required
  field (``chain_id``, ``pool_id_hex``, ``hook_address``,
  ``code_hash``, ``proxy_implementation_address``,
  ``upgrade_authority``, ``flag_bits``, ``verified_source``,
  ``verified_abi``, ``before_after_deltas``,
  ``dynamic_fee_behavior``, ``external_state_dependencies``,
  ``replay_model``, ``adversarial_tests``, ``invalidation_rule``,
  ``version``);
- code-hash change demotes the pool to ``ingestion`` and surfaces
  the demotion record the operator runbook pins;
- adversarial tests cover edge cases (zero hooks, dynamic-fee
  pool, return-delta path, EIP-1167 proxy, delta-without-action
  violation, empty-set defaults, ambiguity between
  ``kind='hook_packs'`` and ``kind='empty_set'``).

The tests use deterministic inputs: every typed value is built
from the protocol-layer ABI / keccak, every pool key is a real V4
``PoolKey``, and the ``build_hook_pack`` builder is exercised
end to end.
"""

from __future__ import annotations

import hashlib

import pytest

from robinhood_lp.config.models import (
    ALL_HOOK_MASK,
    DYNAMIC_FEE_FLAG,
)
from robinhood_lp.discovery.eligibility import (
    EligibilityDecision,
    EligibilityReason,
    EligibilityReasonCode,
)
from robinhood_lp.protocol import Address, Currency, PoolId, PoolKey, RunMode
from robinhood_lp.qualification.hook_pack import (
    DEFAULT_ADVERSARIAL_TESTS,
    DELTA_PAIR_TABLE,
    ENABLED_CALLBACK_NAMES,
    HOOK_FLAG_NAMES,
    HOOK_PACK_SPEC_REVISION,
    REQUIRED_HOOK_PACK_FIELDS,
    EmptySetHookEvidence,
    HookPackReport,
    HookPoolClassificationOutcome,
    HookSourcePin,
    build_default_invalidation_rule,
    build_default_replay_model,
    build_default_source_pin,
    build_empty_set_hook_evidence,
    build_hook_pack,
    build_hook_pack_report,
    build_pool_classification_outcome,
    code_change_demotes_pack,
    decode_hook_flag_bits,
    demoted_pool_record,
    derive_action_delta_invariants,
    derive_before_after_deltas,
    derive_dynamic_fee_behavior,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


REFERENCE_CHAIN_ID = 4663


def _zero_hook_pool_key() -> PoolKey:
    """Build the canonical zero-hook PoolKey used by the reference pool."""
    return PoolKey(
        currency0=Currency.from_hex("0x5fc5360d0400a0fd4f2af552add042d716f1d168"),
        currency1=Currency.from_hex("0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a"),
        fee=28_001,
        tick_spacing=280,
        hooks=Address.zero(),
    )


def _second_pool_pool_key(
    *,
    hooks: Address | None = None,
    fee: int = 3_000,
    tick_spacing: int = 60,
) -> PoolKey:
    """Build the second-pool PoolKey with the Owner-pinned hook address.

    The default address is the pinned lookup signal
    ``0xEd50bDeeA8aDC232f159486192a4157281D722ff`` so the tests
    exercise the nonzero-hook path the T038 contract pins.
    """
    if hooks is None:
        hooks = Address.from_hex("0xEd50bDeeA8aDC232f159486192a4157281D722ff")
    return PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=fee,
        tick_spacing=tick_spacing,
        hooks=hooks,
    )


def _dummy_bytecode_hash(payload: str) -> str:
    """Build a stable 64-hex-char sha256 from a payload string."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pinned_source_pin(
    *,
    repository: str = "uniswap-v4-core",
    commit: str = "0000000000000000000000000000000000000000",
    abi_selectors: tuple[str, ...] = (
        "0x" + "10" * 4,
        "0x" + "20" * 4,
    ),
    event_topics: tuple[str, ...] = ("0x" + "30" * 32,),
) -> HookSourcePin:
    return HookSourcePin(
        repository=repository,
        commit=commit,
        verified_abi_selectors=abi_selectors,
        verified_event_topics=event_topics,
        notes="",
    )


# ---------------------------------------------------------------------------
# Constants (T043 contract)
# ---------------------------------------------------------------------------


def test_hook_pack_spec_revision_pin_is_stable() -> None:
    """The pack's spec-revision pin is the frozen T043 value."""
    assert HOOK_PACK_SPEC_REVISION == "v1-2026-09-18-t043-hook-pack"


def test_required_hook_pack_fields_constant_lists_every_acceptance_field() -> None:
    """The T043 acceptance fields are exactly the constant audit packet."""
    expected = (
        "chain_id",
        "pool_id_hex",
        "hook_address",
        "code_hash",
        "proxy_implementation_address",
        "upgrade_authority",
        "flag_bits",
        "verified_source",
        "verified_abi",
        "before_after_deltas",
        "dynamic_fee_behavior",
        "external_state_dependencies",
        "replay_model",
        "adversarial_tests",
        "invalidation_rule",
        "version",
    )
    assert expected == REQUIRED_HOOK_PACK_FIELDS


def test_hook_flag_names_table_covers_low_14_bits() -> None:
    """The flag-name table covers the low 14 bits in upstream order."""
    seen = 0
    for bit, name in HOOK_FLAG_NAMES:
        seen |= bit
        assert name.startswith(tuple("BAF_")) or "_" in name, f"unexpected hook name {name!r}"
    assert seen == ALL_HOOK_MASK


def test_default_adversarial_tests_covers_every_documented_path() -> None:
    """The default adversarial tests list covers every required path."""
    required_substrings = (
        "before_swap",
        "after_swap",
        "before_add_liquidity",
        "after_add_liquidity",
        "before_remove_liquidity",
        "after_remove_liquidity",
        "before_initialize",
        "after_initialize",
        "dynamic_fee",
        "proxy_implementation_change",
        "upgrade_authority_change",
        "flag_bits_change",
        "delta_without_action_flag",
    )
    for substring in required_substrings:
        assert any(substring in name for name in DEFAULT_ADVERSARIAL_TESTS), (
            f"missing adversarial test name fragment: {substring}"
        )


# ---------------------------------------------------------------------------
# Hook flag decoding
# ---------------------------------------------------------------------------


def test_decode_hook_flag_bits_zero_address_returns_empty() -> None:
    """The zero hook address decodes to no flag bits."""
    decoded = decode_hook_flag_bits(Address.zero())
    assert decoded == ()


def test_decode_hook_flag_bits_returns_named_flags_in_upstream_order() -> None:
    """Flag bits are emitted in the upstream LSB-first order with names."""
    hook_int = (1 << 7) | (1 << 6) | (1 << 2)  # BEFORE_SWAP, AFTER_SWAP, AFTER_SWAP_RETURNS_DELTA
    decoded = decode_hook_flag_bits(Address(hook_int))
    bits = tuple(b for b, _ in decoded)
    names = tuple(n for _, n in decoded)
    assert bits == (1 << 2, 1 << 6, 1 << 7)
    assert names == (
        "AFTER_SWAP_RETURNS_DELTA",
        "AFTER_SWAP",
        "BEFORE_SWAP",
    )


def test_decode_hook_flag_bits_full_mask_decodes_all_callbacks() -> None:
    """A hook address with every low-14 bit set decodes all 14 names."""
    decoded = decode_hook_flag_bits(Address(ALL_HOOK_MASK))
    assert len(decoded) == 14
    bits = tuple(b for b, _ in decoded)
    assert bits == tuple(1 << i for i in range(14))


def test_decode_hook_flag_bits_outside_mask_is_ignored() -> None:
    """Bits outside the low-14 mask do not surface as flag names."""
    hook_int = (1 << 14) | (1 << 7)  # bit 14 is outside the mask
    decoded = decode_hook_flag_bits(Address(hook_int))
    assert decoded == ((1 << 7, "BEFORE_SWAP"),)


# ---------------------------------------------------------------------------
# Dynamic-fee behaviour
# ---------------------------------------------------------------------------


def test_derive_dynamic_fee_behavior_dynamic_fee_pool() -> None:
    """A dynamic-fee pool records the dynamic-fee-hook contract."""
    pk = _second_pool_pool_key(fee=DYNAMIC_FEE_FLAG)
    msg = derive_dynamic_fee_behavior(pk)
    assert "dynamic_fee_pool" in msg
    assert "0x800000" in msg or "DYNAMIC_FEE_FLAG" in msg


def test_derive_dynamic_fee_behavior_static_fee_with_nonzero_hook() -> None:
    """A static-fee pool with nonzero hook flags records the rule."""
    pk = _second_pool_pool_key(fee=3_000)
    msg = derive_dynamic_fee_behavior(pk)
    assert "static_fee_pool_with_nonzero_hook_flags" in msg


def test_derive_dynamic_fee_behavior_zero_hook_static_fee_is_empty() -> None:
    """A zero-hook static-fee pool records no dynamic-fee rule."""
    pk = _zero_hook_pool_key()
    assert derive_dynamic_fee_behavior(pk) == ""


# ---------------------------------------------------------------------------
# Before / after deltas
# ---------------------------------------------------------------------------


def test_derive_before_after_deltas_zero_hook_is_empty() -> None:
    """A zero-hook pool records no delta paths."""
    pk = _zero_hook_pool_key()
    assert derive_before_after_deltas(pk) == ()


def test_derive_before_after_deltas_swap_callbacks() -> None:
    """A pool with beforeSwap / afterSwap flags records the swap callbacks."""
    hook_int = (1 << 7) | (1 << 6)  # BEFORE_SWAP, AFTER_SWAP (no delta)
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    deltas = derive_before_after_deltas(pk)
    callbacks = tuple(d.callback for d in deltas)
    assert callbacks == ("beforeSwap", "afterSwap")
    assert all(d.returns_delta is False for d in deltas)


def test_derive_before_after_deltas_swap_with_return_delta() -> None:
    """A pool with afterSwap return-delta records the delta path."""
    hook_int = (1 << 7) | (1 << 6) | (1 << 2)  # BEFORE_SWAP, AFTER_SWAP, AFTER_SWAP_RETURNS_DELTA
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    deltas = derive_before_after_deltas(pk)
    after_swap = next(d for d in deltas if d.callback == "afterSwap")
    assert after_swap.returns_delta is True
    assert "balanceDelta" in after_swap.fields_written


def test_derive_before_after_deltas_modify_liquidity_callbacks() -> None:
    """A pool with beforeAdd/afterAdd flags records the add callbacks."""
    hook_int = (1 << 11) | (1 << 10)  # BEFORE_ADD_LIQUIDITY, AFTER_ADD_LIQUIDITY
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    deltas = derive_before_after_deltas(pk)
    callbacks = tuple(d.callback for d in deltas)
    assert callbacks == ("beforeAddLiquidity", "afterAddLiquidity")


def test_derive_before_after_deltas_donate_callbacks() -> None:
    """A pool with donate flags records the donate callbacks."""
    hook_int = (1 << 5) | (1 << 4)  # BEFORE_DONATE, AFTER_DONATE
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    deltas = derive_before_after_deltas(pk)
    callbacks = tuple(d.callback for d in deltas)
    assert callbacks == ("beforeDonate", "afterDonate")


def test_derive_before_after_deltas_initialize_callbacks() -> None:
    """A pool with initialize flags records the initialize callbacks."""
    hook_int = (1 << 13) | (1 << 12)  # BEFORE_INITIALIZE, AFTER_INITIALIZE
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    deltas = derive_before_after_deltas(pk)
    callbacks = tuple(d.callback for d in deltas)
    assert callbacks == ("beforeInitialize", "afterInitialize")


# ---------------------------------------------------------------------------
# Action / delta invariants
# ---------------------------------------------------------------------------


def test_derive_action_delta_invariants_well_formed_returns_ok() -> None:
    """A hook with delta + matching action flags records ``delta_ok``."""
    hook_int = (1 << 7) | (1 << 6) | (1 << 2)  # BEFORE_SWAP, AFTER_SWAP, AFTER_SWAP_DELTA
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    invariants = derive_action_delta_invariants(pk)
    assert invariants == ("swap:delta_ok",)


def test_derive_action_delta_invariants_delta_without_action_violation() -> None:
    """A delta flag without its matching action flag is recorded as a violation."""
    hook_int = 1 << 2  # AFTER_SWAP_RETURNS_DELTA without AFTER_SWAP
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    invariants = derive_action_delta_invariants(pk)
    assert "swap:delta_without_action_violation" in invariants


def test_derive_action_delta_invariants_no_delta_flags_is_empty() -> None:
    """A hook with no delta flags records no invariants (vacuously true)."""
    pk = _zero_hook_pool_key()
    assert derive_action_delta_invariants(pk) == ()


# ---------------------------------------------------------------------------
# HookPoolClassificationOutcome
# ---------------------------------------------------------------------------


def test_pool_classification_zero_hook_records_reason_no_hook_pack() -> None:
    """A zero-hook pool records the canonical reason no pack exists."""
    pk = _zero_hook_pool_key()
    outcome = build_pool_classification_outcome(
        pool_alias="reference",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
    )
    assert outcome.hooks_is_zero is True
    assert outcome.hooks_address == pk.hooks.to_hex()
    assert "a hook pack is only built" in outcome.reason_no_hook_pack
    assert "hooks address is the canonical zero address" in outcome.reason_no_hook_pack
    assert outcome.eligibility_reasons  # at least one reason


def test_pool_classification_nonzero_hook_records_no_reason() -> None:
    """A nonzero-hook pool carries no ``reason_no_hook_pack``."""
    pk = _second_pool_pool_key()
    outcome = build_pool_classification_outcome(
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
    )
    assert outcome.hooks_is_zero is False
    assert outcome.reason_no_hook_pack == ""
    assert outcome.eligibility_reasons  # at least one reason


def test_pool_classification_uses_supplied_eligibility_decision() -> None:
    """When a T023 decision is supplied, the outcome surfaces its reasons."""
    pk = _second_pool_pool_key()
    decision = EligibilityDecision(
        pool_id=pk.to_pool_id(),
        level=RunMode.INGESTION,
        reasons=[
            EligibilityReason(EligibilityReasonCode.HOOK_ADDRESS_PRESENT, "detail"),
            EligibilityReason(EligibilityReasonCode.HOOK_BEHAVIOUR_UNKNOWN, "detail"),
        ],
    )
    outcome = build_pool_classification_outcome(
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        eligibility_decision=decision,
    )
    assert outcome.eligibility_level == RunMode.INGESTION.value
    assert "hook_address_present" in outcome.eligibility_reasons


def test_pool_classification_rejects_inconsistent_flags() -> None:
    """An outcome whose address disagrees with ``hooks_is_zero`` is rejected."""
    pk = _zero_hook_pool_key()
    # ``hooks_is_zero=True`` but the recorded address is nonzero:
    # the flags must agree.
    with pytest.raises(ValueError, match="hooks_is_zero is True"):
        HookPoolClassificationOutcome(
            pool_alias="reference",
            pool_id_hex=pk.to_pool_id().to_hex(),
            hooks_address="0x" + "ab" * 20,
            hooks_is_zero=True,
            eligibility_level=RunMode.INGESTION.value,
            eligibility_reasons=(),
            reason_no_hook_pack="some reason",
        )


def test_pool_classification_rejects_non_zero_hook_with_reason() -> None:
    """A nonzero-hook outcome must not carry a reason-no-pack."""
    pk = _second_pool_pool_key()
    with pytest.raises(ValueError, match="reason_no_hook_pack"):
        HookPoolClassificationOutcome(
            pool_alias="second",
            pool_id_hex=pk.to_pool_id().to_hex(),
            hooks_address=pk.hooks.to_hex(),
            hooks_is_zero=False,
            eligibility_level=RunMode.INGESTION.value,
            eligibility_reasons=(),
            reason_no_hook_pack="some reason",
        )


def test_pool_classification_rejects_zero_hook_without_reason() -> None:
    """A zero-hook outcome must carry a reason-no-hook-pack."""
    pk = _zero_hook_pool_key()
    with pytest.raises(ValueError, match="reason_no_hook_pack"):
        HookPoolClassificationOutcome(
            pool_alias="reference",
            pool_id_hex=pk.to_pool_id().to_hex(),
            hooks_address=pk.hooks.to_hex(),
            hooks_is_zero=True,
            eligibility_level=RunMode.INGESTION.value,
            eligibility_reasons=(),
            reason_no_hook_pack="",
        )


# ---------------------------------------------------------------------------
# Empty-set evidence
# ---------------------------------------------------------------------------


def test_empty_set_evidence_requires_at_least_one_pool() -> None:
    """The empty-set artifact refuses an empty per-pool list."""
    with pytest.raises(ValueError, match="pool_classifications"):
        EmptySetHookEvidence(
            chain_id=REFERENCE_CHAIN_ID,
            pool_classifications=(),
            hook_verification_statement="",
        )


def test_empty_set_evidence_requires_non_empty_verification_statement() -> None:
    """The empty-set artifact requires an explicit no-verification statement."""
    pk = _zero_hook_pool_key()
    outcome = build_pool_classification_outcome(
        pool_alias="reference",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
    )
    with pytest.raises(ValueError, match="hook_verification_statement"):
        EmptySetHookEvidence(
            chain_id=REFERENCE_CHAIN_ID,
            pool_classifications=(outcome,),
            hook_verification_statement="",
        )


def test_empty_set_evidence_default_statement_records_no_verification() -> None:
    """The default statement records that no hook semantics were verified."""
    pk = _zero_hook_pool_key()
    outcome = build_pool_classification_outcome(
        pool_alias="reference",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
    )
    artifact = build_empty_set_hook_evidence(
        chain_id=REFERENCE_CHAIN_ID,
        pool_classifications=(outcome,),
    )
    assert artifact.chain_id == 4663
    assert "no hook semantics were verified" in artifact.hook_verification_statement
    assert "empty set" in artifact.hook_verification_statement
    assert len(artifact.pool_classifications) == 1
    assert artifact.pool_classifications[0].hooks_is_zero is True


def test_empty_set_evidence_dual_pool_records_both_classifications() -> None:
    """A dual-pool empty-set artifact records both per-pool classifications."""
    reference_pk = _zero_hook_pool_key()
    second_pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address.zero(),
    )
    reference_outcome = build_pool_classification_outcome(
        pool_alias="reference",
        pool_key=reference_pk,
        pool_id_hex=reference_pk.to_pool_id().to_hex(),
    )
    second_outcome = build_pool_classification_outcome(
        pool_alias="second",
        pool_key=second_pk,
        pool_id_hex=second_pk.to_pool_id().to_hex(),
    )
    artifact = build_empty_set_hook_evidence(
        chain_id=REFERENCE_CHAIN_ID,
        pool_classifications=(reference_outcome, second_outcome),
    )
    assert len(artifact.pool_classifications) == 2
    aliases = tuple(c.pool_alias for c in artifact.pool_classifications)
    assert aliases == ("reference", "second")
    for classification in artifact.pool_classifications:
        assert classification.hooks_is_zero is True
        assert classification.reason_no_hook_pack != ""


def test_empty_set_evidence_to_dict_round_trip() -> None:
    """The empty-set ``to_dict`` carries every required audit field."""
    pk = _zero_hook_pool_key()
    outcome = build_pool_classification_outcome(
        pool_alias="reference",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
    )
    artifact = build_empty_set_hook_evidence(
        chain_id=REFERENCE_CHAIN_ID,
        pool_classifications=(outcome,),
    )
    d = artifact.to_dict()
    assert d["schema"] == "robinhood_lp.qualification.hook_pack.empty_set.v1"
    assert d["version"] == HOOK_PACK_SPEC_REVISION
    assert d["chain_id"] == REFERENCE_CHAIN_ID
    assert "no hook semantics were verified" in d["hook_verification_statement"]
    assert len(d["pool_classifications"]) == 1
    assert d["pool_classifications"][0]["hooks_is_zero"] is True


# ---------------------------------------------------------------------------
# HookPack builder
# ---------------------------------------------------------------------------


def test_build_hook_pack_zero_hook_is_rejected() -> None:
    """A pool with the zero hook address must build empty-set, not pack."""
    pk = _zero_hook_pool_key()
    with pytest.raises(ValueError, match="zero address"):
        build_hook_pack(
            chain_id=REFERENCE_CHAIN_ID,
            pool_alias="reference",
            pool_key=pk,
            pool_id_hex=pk.to_pool_id().to_hex(),
            code_hash=_dummy_bytecode_hash("ref"),
            verified_source=_pinned_source_pin(),
        )


def test_build_hook_pack_nonzero_hook_produces_all_required_fields() -> None:
    """A pool with a nonzero hook produces a pack with every required field."""
    pk = _second_pool_pool_key()
    code_hash = _dummy_bytecode_hash("second-pool-bytecode")
    proxy_impl = "0x" + "ab" * 20
    upgrade_auth = "0x" + "cd" * 20
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=code_hash,
        proxy_implementation_address=proxy_impl,
        upgrade_authority=upgrade_auth,
        external_state_dependencies=("0x" + "ee" * 20,),
        adversarial_tests=DEFAULT_ADVERSARIAL_TESTS,
        verified_source=_pinned_source_pin(),
        expected_modified_event_count=2,
        fork_test_coverage=("test_before_swap_returns_delta",),
        golden_transaction_coverage=("golden_swap_with_delta",),
    )
    # The ``REQUIRED_HOOK_PACK_FIELDS`` constant names every
    # T043 acceptance field. Some of those fields live on
    # ``verified_source`` (e.g. ``verified_abi`` /
    # ``verified_source``); others live directly on the pack.
    # The audit trail must carry every name through either the
    # pack or one of its sub-fields, so we resolve them via
    # ``pack.to_dict()`` and then re-check the schema.
    d = pack.to_dict()
    audit_keys: set[str] = set(d.keys())
    # Sub-fields surfaced via the verified_source dict.
    audit_keys.update(d["verified_source"].keys())
    # Re-check that the audit dict carries every constant.
    constant_to_audit = {
        "chain_id": "chain_id",
        "pool_id_hex": "pool_id",
        "hook_address": "hook_address",
        "code_hash": "code_hash",
        "proxy_implementation_address": "proxy_implementation_address",
        "upgrade_authority": "upgrade_authority",
        "flag_bits": "flag_bits",
        "verified_source": "verified_source",
        "verified_abi": "verified_abi_selectors",
        "before_after_deltas": "before_after_deltas",
        "dynamic_fee_behavior": "dynamic_fee_behavior",
        "external_state_dependencies": "external_state_dependencies",
        "replay_model": "replay_model",
        "adversarial_tests": "adversarial_tests",
        "invalidation_rule": "invalidation_rule",
        "version": "version",
    }
    for const, audit_key in constant_to_audit.items():
        assert audit_key in audit_keys, (
            f"missing audit-trail key {audit_key!r} for required field {const!r}"
        )
    assert pack.chain_id == REFERENCE_CHAIN_ID
    assert pack.pool_id_hex == pk.to_pool_id().to_hex()
    assert pack.hook_address == pk.hooks.to_hex()
    assert pack.code_hash == code_hash
    assert pack.proxy_implementation_address == proxy_impl
    assert pack.upgrade_authority == upgrade_auth
    assert pack.version == HOOK_PACK_SPEC_REVISION
    assert pack.has_proxy_implementation is True
    assert pack.has_upgrade_authority is True


def test_build_hook_pack_records_delta_without_action_violation() -> None:
    """A delta flag without its action flag is recorded in the pack note.

    V4 would ``isValidHookAddress`` reject the pool at creation
    time, but the framework must record the on-chain fact rather
    than silently rewrite it. The pack keeps the violation in the
    ``invalidation_note`` field so the operator runbook pins it.
    """
    hook_int = 1 << 2  # AFTER_SWAP_RETURNS_DELTA without AFTER_SWAP
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("bad"),
        verified_source=_pinned_source_pin(),
    )
    assert "V4 isValidHookAddress rule 1 violation" in pack.invalidation_note
    assert "swap:delta_without_action_violation" in pack.invalidation_note
    assert "ingestion" in pack.invalidation_note


def test_build_hook_pack_appends_violation_to_existing_invalidation_note() -> None:
    """A user-supplied ``invalidation_note`` is preserved and the
    violation is appended to it (so the audit trail keeps the
    operator's annotation plus the framework's detection)."""
    hook_int = 1 << 2  # AFTER_SWAP_RETURNS_DELTA without AFTER_SWAP
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("bad"),
        verified_source=_pinned_source_pin(),
        invalidation_note="operator flagged this pool for review",
    )
    assert "operator flagged this pool for review" in pack.invalidation_note
    assert "V4 isValidHookAddress rule 1 violation" in pack.invalidation_note


def test_build_hook_pack_rejects_empty_code_hash() -> None:
    """The pack refuses an empty code hash (no permission to simulate)."""
    pk = _second_pool_pool_key()
    with pytest.raises(ValueError, match="code_hash"):
        build_hook_pack(
            chain_id=REFERENCE_CHAIN_ID,
            pool_alias="second",
            pool_key=pk,
            pool_id_hex=pk.to_pool_id().to_hex(),
            code_hash="",
            verified_source=_pinned_source_pin(),
        )


def test_build_hook_pack_requires_verified_source() -> None:
    """The pack refuses to omit the source pin (T043 acceptance: verified source)."""
    pk = _second_pool_pool_key()
    with pytest.raises(ValueError, match="verified_source"):
        build_hook_pack(
            chain_id=REFERENCE_CHAIN_ID,
            pool_alias="second",
            pool_key=pk,
            pool_id_hex=pk.to_pool_id().to_hex(),
            code_hash=_dummy_bytecode_hash("second-pool"),
            verified_source=None,
        )


def test_build_hook_pack_populates_default_invalidation_rule() -> None:
    """When no invalidation rule is supplied, the default rule is used."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
    )
    rule = pack.invalidation_rule
    assert "code_hash_change" in rule.trigger_conditions
    assert "flag_bits_change" in rule.trigger_conditions
    assert rule.demoted_to == RunMode.INGESTION


def test_build_hook_pack_default_replay_model_uses_upstream_contract() -> None:
    """The default replay model names the upstream-V4 contract."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
    )
    assert pack.replay_model.delta_propagation == "after_swap_after_add_after_remove"


def test_build_hook_pack_propagates_flag_bits_in_upstream_order() -> None:
    """The pack records flag bits in the upstream LSB-first order."""
    hook_int = (1 << 7) | (1 << 6) | (1 << 2)  # BEFORE_SWAP, AFTER_SWAP, AFTER_SWAP_RETURNS_DELTA
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
    )
    assert pack.flag_bits == (1 << 2, 1 << 6, 1 << 7)
    assert pack.flag_names == (
        "AFTER_SWAP_RETURNS_DELTA",
        "AFTER_SWAP",
        "BEFORE_SWAP",
    )


def test_build_hook_pack_dynamic_fee_pool_records_dynamic_fee_behavior() -> None:
    """A dynamic-fee pool's pack records the dynamic-fee behaviour string."""
    pk = _second_pool_pool_key(fee=DYNAMIC_FEE_FLAG)
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
    )
    assert pack.is_dynamic_fee is True
    assert "dynamic_fee_pool" in pack.dynamic_fee_behavior


def test_build_hook_pack_to_dict_round_trip() -> None:
    """The pack's ``to_dict`` carries every required field's audit packet."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
        adversarial_tests=DEFAULT_ADVERSARIAL_TESTS,
    )
    d = pack.to_dict()
    assert d["schema"] == "robinhood_lp.qualification.hook_pack.v1"
    assert d["version"] == HOOK_PACK_SPEC_REVISION
    assert d["chain_id"] == REFERENCE_CHAIN_ID
    assert d["pool_alias"] == "second"
    assert d["pool_id"] == pk.to_pool_id().to_hex()
    assert d["hook_address"] == pk.hooks.to_hex()
    assert d["code_hash"] == _dummy_bytecode_hash("second-pool")
    assert "verified_source" in d
    assert "verified_abi_selectors" in d["verified_source"]
    assert "before_after_deltas" in d
    assert "dynamic_fee_behavior" in d
    assert "external_state_dependencies" in d
    assert "replay_model" in d
    assert "adversarial_tests" in d
    assert "invalidation_rule" in d
    assert "version" in d


# ---------------------------------------------------------------------------
# Invalidation rule
# ---------------------------------------------------------------------------


def test_code_change_demotes_pack_returns_true_on_mismatch() -> None:
    """A different observed code hash triggers the demotion rule."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("original"),
        verified_source=_pinned_source_pin(),
    )
    assert (
        code_change_demotes_pack(pack, observed_code_hash=_dummy_bytecode_hash("upgraded")) is True
    )


def test_code_change_demotes_pack_returns_false_on_match() -> None:
    """An identical observed code hash does not trigger the demotion rule."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("stable"),
        verified_source=_pinned_source_pin(),
    )
    assert (
        code_change_demotes_pack(pack, observed_code_hash=_dummy_bytecode_hash("stable")) is False
    )


def test_code_change_demotes_pack_returns_false_on_empty_observed() -> None:
    """An empty observed code hash never triggers the demotion rule."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("stable"),
        verified_source=_pinned_source_pin(),
    )
    assert code_change_demotes_pack(pack, observed_code_hash="") is False


def test_demoted_pool_record_emits_audit_packet_on_mismatch() -> None:
    """A code-hash mismatch emits the demotion record the runbook pins."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("original"),
        verified_source=_pinned_source_pin(),
    )
    record = demoted_pool_record(
        pack,
        observed_code_hash=_dummy_bytecode_hash("upgraded"),
        demotion_reason="proxy implementation upgraded",
    )
    assert record["schema"] == "robinhood_lp.qualification.hook_pack.demotion.v1"
    assert record["pool_alias"] == "second"
    assert record["previous_code_hash"] == pack.code_hash
    assert record["observed_code_hash"] == _dummy_bytecode_hash("upgraded")
    assert record["demoted_to"] == RunMode.INGESTION.value
    assert "proxy implementation upgraded" in record["demotion_reason"]


def test_demoted_pool_record_emits_empty_dict_on_match() -> None:
    """An identical observed code hash emits no demotion record."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("stable"),
        verified_source=_pinned_source_pin(),
    )
    record = demoted_pool_record(pack, observed_code_hash=_dummy_bytecode_hash("stable"))
    assert record == {}


# ---------------------------------------------------------------------------
# HookPackReport
# ---------------------------------------------------------------------------


def test_hook_pack_report_kind_hook_packs() -> None:
    """A heterogeneous pack record has ``kind='hook_packs'``."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
    )
    reference_pk = _zero_hook_pool_key()
    classification = build_pool_classification_outcome(
        pool_alias="reference",
        pool_key=reference_pk,
        pool_id_hex=reference_pk.to_pool_id().to_hex(),
    )
    report = build_hook_pack_report(
        chain_id=REFERENCE_CHAIN_ID,
        pool_classifications=(classification,),
        hook_packs=(pack,),
    )
    assert report.kind == "hook_packs"
    assert len(report.hook_packs) == 1
    assert report.hook_packs[0].pool_alias == "second"
    assert report.empty_set_record is None


def test_hook_pack_report_kind_empty_set() -> None:
    """A no-nonzero-hook report has ``kind='empty_set'``."""
    reference_pk = _zero_hook_pool_key()
    second_pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address.zero(),
    )
    reference_class = build_pool_classification_outcome(
        pool_alias="reference",
        pool_key=reference_pk,
        pool_id_hex=reference_pk.to_pool_id().to_hex(),
    )
    second_class = build_pool_classification_outcome(
        pool_alias="second",
        pool_key=second_pk,
        pool_id_hex=second_pk.to_pool_id().to_hex(),
    )
    report = build_hook_pack_report(
        chain_id=REFERENCE_CHAIN_ID,
        pool_classifications=(reference_class, second_class),
    )
    assert report.kind == "empty_set"
    assert report.hook_packs == ()
    assert report.empty_set_record is not None
    assert len(report.empty_set_record.pool_classifications) == 2


def test_hook_pack_report_rejects_empty_hook_packs_with_kind_hook_packs() -> None:
    """A ``hook_packs`` report without any pack is rejected."""
    with pytest.raises(ValueError, match="requires at least one HookPack"):
        HookPackReport(
            chain_id=REFERENCE_CHAIN_ID,
            kind="hook_packs",
            hook_packs=(),
        )


def test_hook_pack_report_rejects_empty_set_without_artifact() -> None:
    """A ``empty_set`` report without the empty-set artifact is rejected."""
    with pytest.raises(ValueError, match="requires an EmptySetHookEvidence"):
        HookPackReport(
            chain_id=REFERENCE_CHAIN_ID,
            kind="empty_set",
            empty_set_record=None,
        )


def test_hook_pack_report_rejects_combined_hook_packs_and_empty_set() -> None:
    """A report that carries both pack and empty set is rejected."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
    )
    classification = build_pool_classification_outcome(
        pool_alias="reference",
        pool_key=_zero_hook_pool_key(),
        pool_id_hex=_zero_hook_pool_key().to_pool_id().to_hex(),
    )
    empty_set = build_empty_set_hook_evidence(
        chain_id=REFERENCE_CHAIN_ID,
        pool_classifications=(classification,),
    )
    with pytest.raises(ValueError, match="hook_packs and empty_set_record are mutually exclusive"):
        build_hook_pack_report(
            chain_id=REFERENCE_CHAIN_ID,
            pool_classifications=(classification,),
            hook_packs=(pack,),
            empty_set_record=empty_set,
        )


def test_hook_pack_report_to_dict_round_trip() -> None:
    """The combined report's ``to_dict`` carries every required field."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
    )
    classification = build_pool_classification_outcome(
        pool_alias="reference",
        pool_key=_zero_hook_pool_key(),
        pool_id_hex=_zero_hook_pool_key().to_pool_id().to_hex(),
    )
    report = build_hook_pack_report(
        chain_id=REFERENCE_CHAIN_ID,
        pool_classifications=(classification,),
        hook_packs=(pack,),
    )
    d = report.to_dict()
    assert d["schema"] == "robinhood_lp.qualification.hook_pack_report.v1"
    assert d["kind"] == "hook_packs"
    assert len(d["hook_packs"]) == 1
    assert len(d["pool_classifications"]) == 1


# ---------------------------------------------------------------------------
# Default builders
# ---------------------------------------------------------------------------


def test_build_default_source_pin_records_repository_and_commit() -> None:
    """The default source pin records the repository and the commit."""
    pin = build_default_source_pin(commit="deadbeef" * 8)
    assert pin.repository == "uniswap-v4-core"
    assert pin.commit == "deadbeef" * 8
    assert pin.verified_abi_selectors == ()
    assert pin.verified_event_topics == ()


def test_build_default_replay_model_uses_upstream_delta_propagation() -> None:
    """The default replay model uses the upstream-V4 delta-propagation rule."""
    model = build_default_replay_model(expected_modified_event_count=1)
    assert model.expected_modified_event_count == 1
    assert model.delta_propagation == "after_swap_after_add_after_remove"


def test_build_default_invalidation_rule_demotes_to_ingestion() -> None:
    """The default invalidation rule demotes a pool to ``ingestion``."""
    rule = build_default_invalidation_rule()
    assert rule.demoted_to == RunMode.INGESTION
    assert "code_hash_change" in rule.trigger_conditions


# ---------------------------------------------------------------------------
# Adversarial tests
# ---------------------------------------------------------------------------


def test_adversarial_tests_for_dynamic_fee_pool_include_dynamic_fee_test() -> None:
    """The adversarial tests surface the dynamic-fee hook contract."""
    pk = _second_pool_pool_key(fee=DYNAMIC_FEE_FLAG)
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
        adversarial_tests=DEFAULT_ADVERSARIAL_TESTS,
    )
    assert any("dynamic_fee" in name for name in pack.adversarial_tests)


def test_adversarial_tests_for_swap_with_delta_cover_delta_paths() -> None:
    """The adversarial tests cover both return-delta paths."""
    hook_int = (1 << 7) | (1 << 6) | (1 << 2)
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
        adversarial_tests=DEFAULT_ADVERSARIAL_TESTS,
    )
    assert any("after_swap_returns_delta" in name for name in pack.adversarial_tests)
    assert any("before_swap_returns_delta" in name for name in pack.adversarial_tests)


# ---------------------------------------------------------------------------
# Frozen and immutability
# ---------------------------------------------------------------------------


def test_hook_pack_is_frozen() -> None:
    """The pack is a frozen value object: field mutation is rejected."""
    pk = _second_pool_pool_key()
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
    )
    with pytest.raises((AttributeError, Exception)):
        pack.code_hash = _dummy_bytecode_hash("mutated")  # type: ignore[misc]


def test_empty_set_evidence_is_frozen() -> None:
    """The empty-set artifact is a frozen value object."""
    pk = _zero_hook_pool_key()
    outcome = build_pool_classification_outcome(
        pool_alias="reference",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
    )
    artifact = build_empty_set_hook_evidence(
        chain_id=REFERENCE_CHAIN_ID,
        pool_classifications=(outcome,),
    )
    with pytest.raises((AttributeError, Exception)):
        artifact.chain_id = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Hook flag table sanity
# ---------------------------------------------------------------------------


def test_delta_pair_table_includes_every_documented_action() -> None:
    """The delta-pair table covers swap, add_liquidity, remove_liquidity."""
    names = {entry[2] for entry in DELTA_PAIR_TABLE}
    assert names == {"swap", "add_liquidity", "remove_liquidity"}


def test_enabled_callback_names_covers_all_v4_callbacks() -> None:
    """The enabled callback names cover every documented V4 callback."""
    expected = {
        "beforeInitialize",
        "afterInitialize",
        "beforeAddLiquidity",
        "afterAddLiquidity",
        "beforeRemoveLiquidity",
        "afterRemoveLiquidity",
        "beforeSwap",
        "afterSwap",
        "beforeDonate",
        "afterDonate",
    }
    assert set(ENABLED_CALLBACK_NAMES) == expected


# ---------------------------------------------------------------------------
# Builder cross-checks
# ---------------------------------------------------------------------------


def test_pool_classification_rejects_bad_pool_id_hex() -> None:
    """The pool classification outcome refuses a non-32-byte pool id."""
    pk = _zero_hook_pool_key()
    with pytest.raises(ValueError, match="pool_id_hex"):
        build_pool_classification_outcome(
            pool_alias="reference",
            pool_key=pk,
            pool_id_hex="0xabcd",
        )


def test_build_hook_pack_rejects_bad_pool_id_hex() -> None:
    """The pack builder refuses a non-32-byte pool id."""
    # Use a hook address with valid V4 flags so the validation
    # failure is on the pool id alone.
    hook_int = (1 << 7) | (1 << 6)  # BEFORE_SWAP, AFTER_SWAP
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    with pytest.raises(ValueError, match="pool_id_hex"):
        build_hook_pack(
            chain_id=REFERENCE_CHAIN_ID,
            pool_alias="second",
            pool_key=pk,
            pool_id_hex="0xab",
            code_hash=_dummy_bytecode_hash("second-pool"),
            verified_source=_pinned_source_pin(),
        )


def test_hook_pack_pool_id_is_keccak_of_pool_key() -> None:
    """The pool id recorded on the pack equals ``keccak256(abi.encode(PoolKey))``."""
    # Use a well-formed hook address (BEFORE_SWAP, AFTER_SWAP)
    # that does not violate V4 rule 1.
    hook_int = (1 << 7) | (1 << 6)
    pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3_000,
        tick_spacing=60,
        hooks=Address(hook_int),
    )
    pack = build_hook_pack(
        chain_id=REFERENCE_CHAIN_ID,
        pool_alias="second",
        pool_key=pk,
        pool_id_hex=pk.to_pool_id().to_hex(),
        code_hash=_dummy_bytecode_hash("second-pool"),
        verified_source=_pinned_source_pin(),
    )
    expected = PoolId.from_hex(pk.to_pool_id().to_hex())
    assert pack.pool_id_hex == expected.to_hex()
