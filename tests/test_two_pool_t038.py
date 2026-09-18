"""Tests for T038 two-pool ten-million-block qualification.

The tests exercise the deterministic T038 surfaces:

1. ``WindowPin`` finalized-block agreement: both endpoints must
   agree on block number and hash; disagreements / unavailability
   return ``None`` and the run halts closed.
2. ``apply_window_rule``: per-pool start = min(end - 10M, Initialize)
   when within the 2,000,000-block extension cap; pools whose
   ``Initialize`` lies further below are reported as
   ``pool_init_outside_window`` rather than extending the window.
3. Second-pool identity resolver: ``keccak256(abi.encode(PoolKey)) ==
   PoolId`` must hold; a mismatch halts qualification.
4. Per-pool T034 report: ``complete=True`` only when every interval
   is ``successful`` / ``scanned_empty``, reconciliation agrees, and
   no blocker was raised.
5. Combined T038 machine report: ``complete=True`` only when every
   included pool's per-pool report is ``complete=True`` and the
   second-pool resolver outcome is ``resolve_ok``.
6. Operator runbook: the routing table names the primary endpoint
   for the wide pool-filtered scan and the secondary endpoint for
   the sampled cross-validation and the block-pinned StateView
   read; the Markdown rendering pins the window end, the per-pool
   outcomes and the second-pool resolution summary.
7. Failure-path evidence: every documented two-pool failure mode
   produces its named reason code and never a false ``complete=True``.

The tests use synthetic-but-faithful inputs: every typed value is
constructed from the protocol-layer ABI / keccak, every pool identity
is a real V4 ``PoolKey``, and the offline keccak256 re-derivation
check is exercised end to end.
"""

from __future__ import annotations

import json

import pytest
from eth_hash.auto import keccak

from robinhood_lp.discovery.initialize_log import DecodedInitialize
from robinhood_lp.protocol import (
    Address,
    Currency,
    PoolId,
    PoolKey,
)
from robinhood_lp.protocol.abi import compute_pool_id, encode_pool_key
from robinhood_lp.qualification import (
    BASELINE_DISTINCT_BLOCKS,
    BASELINE_INITIALIZE_COUNT,
    BASELINE_MODIFY_LIQUIDITY_COUNT,
    BASELINE_PROTOCOL_FEE_UPDATED_COUNT,
    BASELINE_SWAP_COUNT,
    BASELINE_TOTAL_EVENTS,
    DEFAULT_PRIMARY_ALIAS,
    DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL,
    DEFAULT_SECONDARY_ALIAS,
    DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL,
    DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS,
    FAILURE_PATH_KIND_BUDGET_EXHAUSTED,
    FAILURE_PATH_KIND_FINALIZED_DISAGREEMENT,
    FAILURE_PATH_KIND_FINALIZED_UNAVAILABLE,
    FAILURE_PATH_KIND_HTTP_429,
    FAILURE_PATH_KIND_REQUEST_WIDER_THAN_CAPABILITY,
    OUTCOME_POOL_INCLUDED,
    OUTCOME_POOL_INIT_OUTSIDE_WINDOW,
    REFERENCE_CHAIN_ID,
    REFERENCE_POOL_ID_HEX,
    REFERENCE_TARGET,
    RESOLVE_CHAIN_ID_MISMATCH,
    RESOLVE_HOOK_ADDRESS_AMBIGUOUS,
    RESOLVE_OK,
    RESOLVE_PINNED_POOL_ID_SIZE_DEFECT,
    RESOLVE_POOL_ID_MISMATCH,
    RESOLVE_POOL_KEY_DECODE_ERROR,
    RESOLVE_WINDOW_UNRESOLVED,
    SECOND_POOL_CHAIN_ID,
    SECOND_POOL_HOOK_ADDRESS_HEX,
    SECOND_POOL_POOL_ID_HEX,
    SUPPORT_LEVEL_INGESTION,
    EndpointFinalizedObservation,
    PerPoolT034Report,
    PoolWindowOutcome,
    ResolvedPoolKey,
    SecondPoolResolveResult,
    TwoPoolCandidate,
    TwoPoolFailurePathEvidence,
    TwoPoolWindowPlan,
    WindowPin,
    all_documented_two_pool_failure_path_evidence,
    apply_window_rule,
    build_second_pool_resolve_result_from_resolved_fields,
    build_two_pool_failure_path_evidence,
    build_two_pool_operator_runbook,
    build_two_pool_report,
    classify_second_pool_support_level,
    fail_complete_under_two_pool_failure_paths,
    finalize_window_pin,
    reference_pool_candidate,
    resolve_second_pool_identity,
    resolve_second_pool_via_hook_scan,
    second_pool_candidate,
)
from robinhood_lp.qualification.reference import build_reference_pool_key

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_for_block(block_number: int) -> str:
    """Deterministic 0x-hex 32-byte hash for a block number."""
    h = keccak(block_number.to_bytes(8, "big"))
    return "0x" + h.hex()


def _window_pin(
    block_number: int,
    *,
    primary: str = "robinhood_public",
    secondary: str = "alchemy_free",
) -> WindowPin:
    return WindowPin(
        block_number=block_number,
        block_hash=_hash_for_block(block_number),
        primary_endpoint=primary,
        secondary_endpoint=secondary,
        pinned_at="2026-09-18T00:00:00+00:00",
    )


def _second_pool_pool_key(hooks: Address | None = None) -> PoolKey:
    """Build a deterministic second-pool PoolKey.

    The two currencies are arbitrary but distinct (zero < non-zero);
    ``fee=3000``, ``tick_spacing=60`` keeps the key within the
    V4 domain. The default hooks address is the zero address
    (no-hook); tests that exercise the nonzero-hook path pass an
    explicit address.
    """
    currency0 = Currency.from_hex("0x" + "11" * 20)
    currency1 = Currency.from_hex("0x" + "22" * 20)
    if hooks is None:
        hooks = Address.zero()
    return PoolKey(
        currency0=currency0,
        currency1=currency1,
        fee=3000,
        tick_spacing=60,
        hooks=hooks,
    )


# ---------------------------------------------------------------------------
# Constants (T038 contract)
# ---------------------------------------------------------------------------


def test_two_pool_constants_match_contract() -> None:
    """The T038 contract pins the Owner-second-pool identity, the
    chain id and the window width; the module exposes the same
    constants."""
    assert SECOND_POOL_POOL_ID_HEX == "0xEd50bDeeA8aDC232f159486192a4157281D722ff"
    assert SECOND_POOL_CHAIN_ID == 4663
    from robinhood_lp.qualification.two_pool_window import (
        TWO_POOL_CHAIN_ID,
        TWO_POOL_INIT_EXTENSION_CAP_BLOCKS,
        TWO_POOL_WINDOW_BLOCKS,
    )

    assert TWO_POOL_CHAIN_ID == 4663
    assert TWO_POOL_WINDOW_BLOCKS == 10_000_000
    assert TWO_POOL_INIT_EXTENSION_CAP_BLOCKS == 2_000_000


def test_reference_target_pool_id_matches_pinned_pool_id() -> None:
    """The pinned reference PoolId equals the offline keccak256
    re-derivation of the pinned PoolKey (T036 contract)."""
    derived = build_reference_pool_key().to_pool_id()
    assert derived.to_hex() == REFERENCE_POOL_ID_HEX


# ---------------------------------------------------------------------------
# finalize_window_pin — agreement check
# ---------------------------------------------------------------------------


def test_finalize_window_pin_returns_agreed_pin_on_match() -> None:
    """When both endpoints report the same block number and hash the
    pin is returned with both aliases recorded."""
    primary = EndpointFinalizedObservation(
        endpoint_alias="robinhood_public",
        block_number=70_000_000,
        block_hash=_hash_for_block(70_000_000),
        observed_at="2026-09-18T00:00:00+00:00",
    )
    secondary = EndpointFinalizedObservation(
        endpoint_alias="alchemy_free",
        block_number=70_000_000,
        block_hash=_hash_for_block(70_000_000),
        observed_at="2026-09-18T00:00:01+00:00",
    )
    pin = finalize_window_pin(primary, secondary)
    assert pin is not None
    assert pin.block_number == 70_000_000
    assert pin.block_hash == _hash_for_block(70_000_000)
    assert pin.primary_endpoint == "robinhood_public"
    assert pin.secondary_endpoint == "alchemy_free"


def test_finalize_window_pin_normalises_hash_casing() -> None:
    """Case-insensitive comparison so different RPC clients' casing
    conventions do not falsely surface a disagreement."""
    hash_value = _hash_for_block(70_000_000)
    primary = EndpointFinalizedObservation(
        endpoint_alias="robinhood_public",
        block_number=70_000_000,
        block_hash=hash_value.upper(),
    )
    secondary = EndpointFinalizedObservation(
        endpoint_alias="alchemy_free",
        block_number=70_000_000,
        block_hash=hash_value.lower(),
    )
    assert finalize_window_pin(primary, secondary) is not None


def test_finalize_window_pin_returns_none_on_number_disagreement() -> None:
    """When the two endpoints disagree on block number the pin is
    ``None`` and the run halts with the
    ``finalized_endpoint_disagreement`` reason code."""
    primary = EndpointFinalizedObservation(
        endpoint_alias="robinhood_public",
        block_number=70_000_000,
        block_hash=_hash_for_block(70_000_000),
    )
    secondary = EndpointFinalizedObservation(
        endpoint_alias="alchemy_free",
        block_number=70_000_001,
        block_hash=_hash_for_block(70_000_001),
    )
    assert finalize_window_pin(primary, secondary) is None


def test_finalize_window_pin_returns_none_on_hash_disagreement() -> None:
    """When the two endpoints disagree on block hash the pin is
    ``None`` even when block numbers agree (the contract requires
    number-and-hash agreement)."""
    primary = EndpointFinalizedObservation(
        endpoint_alias="robinhood_public",
        block_number=70_000_000,
        block_hash=_hash_for_block(70_000_000),
    )
    secondary = EndpointFinalizedObservation(
        endpoint_alias="alchemy_free",
        block_number=70_000_000,
        block_hash=_hash_for_block(69_999_999),
    )
    assert finalize_window_pin(primary, secondary) is None


def test_finalize_window_pin_returns_none_when_unavailable() -> None:
    """When either endpoint returns ``None`` for the finalized tag
    the pin is ``None`` and the run halts with the
    ``finalized_unavailable`` reason code; ``latest`` is never a
    fallback."""
    primary = EndpointFinalizedObservation(
        endpoint_alias="robinhood_public",
        block_number=70_000_000,
        block_hash=_hash_for_block(70_000_000),
    )
    secondary = EndpointFinalizedObservation(
        endpoint_alias="alchemy_free",
        block_number=None,
        block_hash=None,
    )
    assert finalize_window_pin(primary, secondary) is None
    primary_missing = EndpointFinalizedObservation(
        endpoint_alias="robinhood_public",
        block_number=None,
        block_hash=None,
    )
    assert finalize_window_pin(primary_missing, secondary) is None


def test_finalize_window_pin_rejects_invalid_hash() -> None:
    """The pin validates the block hash shape so a malformed
    ``eth_getBlockByNumber("finalized", false)`` response is
    surfaced as a validation error rather than silently accepted."""
    with pytest.raises(ValueError):
        EndpointFinalizedObservation(
            endpoint_alias="robinhood_public",
            block_number=70_000_000,
            block_hash="0xZZZZ",
        )
    with pytest.raises(ValueError):
        EndpointFinalizedObservation(
            endpoint_alias="robinhood_public",
            block_number=70_000_000,
            block_hash="not_a_hash",
        )


# ---------------------------------------------------------------------------
# apply_window_rule — the per-pool window extension rule
# ---------------------------------------------------------------------------


def test_apply_window_rule_includes_reference_pool_with_no_extension() -> None:
    """The reference pool's ``Initialize`` is the inclusive lower
    bound of the pinned reference range (T036 contract); the
    default_start is therefore equal to or below the pool's
    Initialize, and no extension is required when ``Initialize``
    is at or above the default_start."""
    pin = _window_pin(block_number=70_000_000)
    candidates = (reference_pool_candidate(pool_init_block=60_000_000),)
    plan = apply_window_rule(window_pin=pin, candidates=candidates)
    assert plan.default_start_block == 60_000_000
    assert len(plan.pool_outcomes) == 1
    outcome = plan.pool_outcomes[0]
    assert outcome.outcome == OUTCOME_POOL_INCLUDED
    assert outcome.coverage_from_block == 60_000_000
    assert outcome.extension_below_default_start == 0
    assert outcome.gap_blocks is None


def test_apply_window_rule_default_start_is_end_minus_ten_million() -> None:
    """``default_start_block`` is ``pin.block_number - 10_000_000``
    per the T038 contract."""
    pin = _window_pin(block_number=75_000_000)
    plan = apply_window_rule(
        window_pin=pin,
        candidates=(reference_pool_candidate(pool_init_block=pin.block_number - 1),),
    )
    assert plan.default_start_block == 65_000_000


def test_apply_window_rule_extends_within_cap() -> None:
    """A pool whose ``Initialize`` lies within the 2,000,000-block
    extension cap below ``default_start`` widens the window below
    the default to chase it; the outcome records the extension."""
    pin = _window_pin(block_number=70_000_000)
    # default_start = 60_000_000; Initialize = 58_500_000; gap=1_500_000
    # which is inside the 2_000_000 cap; extension = 1_500_000
    candidates = (reference_pool_candidate(pool_init_block=58_500_000),)
    plan = apply_window_rule(window_pin=pin, candidates=candidates)
    outcome = plan.pool_outcomes[0]
    assert outcome.outcome == OUTCOME_POOL_INCLUDED
    assert outcome.coverage_from_block == 58_500_000
    assert outcome.extension_below_default_start == 1_500_000
    assert outcome.gap_blocks is None


def test_apply_window_rule_excludes_pool_outside_window() -> None:
    """A pool whose ``Initialize`` lies further below the default
    start than the extension cap is reported as
    ``pool_init_outside_window`` with the gap size recorded; the
    window does NOT widen to chase it."""
    pin = _window_pin(block_number=70_000_000)
    # default_start = 60_000_000; Initialize = 50_000_000; gap=10_000_000
    candidates = (reference_pool_candidate(pool_init_block=50_000_000),)
    plan = apply_window_rule(window_pin=pin, candidates=candidates)
    outcome = plan.pool_outcomes[0]
    assert outcome.outcome == OUTCOME_POOL_INIT_OUTSIDE_WINDOW
    assert outcome.coverage_from_block == 60_000_000
    assert outcome.extension_below_default_start == 0
    assert outcome.gap_blocks == 10_000_000
    assert "Initialize block 50000000" in outcome.reason
    assert "10,000,000" in outcome.reason or "10000000" in outcome.reason
    assert plan.excluded_pool_aliases == ("reference",)
    assert plan.included_pool_aliases == ()


def test_apply_window_rule_handles_unresolved_init_block() -> None:
    """A candidate whose ``pool_init_block`` is ``None`` (the
    operator did not find an Initialize log inside the search
    bounds) is reported as ``pool_init_outside_window`` with no
    gap size; the planner never guesses an ``Initialize``."""
    pin = _window_pin(block_number=70_000_000)
    candidates = (second_pool_candidate(pool_init_block=None),)
    plan = apply_window_rule(window_pin=pin, candidates=candidates)
    outcome = plan.pool_outcomes[0]
    assert outcome.outcome == OUTCOME_POOL_INIT_OUTSIDE_WINDOW
    assert outcome.gap_blocks is None
    assert "not resolved" in outcome.reason.lower() or "not supplied" in outcome.reason.lower()


def test_apply_window_rule_two_pools_mixed_outcomes() -> None:
    """A reference pool that is inside the window + a second pool
    that is outside the window yields one included and one excluded
    pool; the plan surfaces both."""
    pin = _window_pin(block_number=70_000_000)
    candidates = (
        reference_pool_candidate(pool_init_block=60_000_000),
        second_pool_candidate(pool_init_block=40_000_000),
    )
    plan = apply_window_rule(window_pin=pin, candidates=candidates)
    by_alias = {outcome.pool_alias: outcome for outcome in plan.pool_outcomes}
    assert by_alias["reference"].is_included
    assert by_alias["second"].is_excluded
    assert plan.included_pool_aliases == ("reference",)
    assert plan.excluded_pool_aliases == ("second",)
    assert by_alias["second"].gap_blocks == 20_000_000


def test_apply_window_rule_rejects_empty_candidates() -> None:
    """At least one candidate is required so the planner never
    silently produces an empty plan."""
    pin = _window_pin(block_number=70_000_000)
    with pytest.raises(ValueError):
        apply_window_rule(window_pin=pin, candidates=())


def test_apply_window_rule_window_width_uses_included_starts() -> None:
    """The window width is ``pin - earliest_included_start + 1``;
    an all-excluded plan reports ``0`` so the run halts without
    claiming coverage."""
    pin = _window_pin(block_number=70_000_000)
    candidates = (
        reference_pool_candidate(pool_init_block=40_000_000),
        second_pool_candidate(pool_init_block=30_000_000),
    )
    plan = apply_window_rule(window_pin=pin, candidates=candidates)
    assert plan.window_width_blocks == 0
    assert plan.included_pools == ()


def test_apply_window_rule_extension_cap_exact_boundary() -> None:
    """A pool whose ``Initialize`` is exactly 2,000,000 blocks below
    ``default_start`` is included (the gap is within the cap);
    a pool whose gap is 2,000,001 blocks is excluded."""
    pin = _window_pin(block_number=70_000_000)
    boundary = 60_000_000 - 2_000_000
    inside = apply_window_rule(
        window_pin=pin,
        candidates=(reference_pool_candidate(pool_init_block=boundary),),
    )
    assert inside.pool_outcomes[0].is_included
    outside = apply_window_rule(
        window_pin=pin,
        candidates=(reference_pool_candidate(pool_init_block=boundary - 1),),
    )
    assert outside.pool_outcomes[0].is_excluded


# ---------------------------------------------------------------------------
# Second-pool identity resolver (offline keccak256 re-derivation)
# ---------------------------------------------------------------------------


def _resolved_second_pool(
    *,
    chain_id: int = SECOND_POOL_CHAIN_ID,
    pool_id_hex: str | None = None,
    hooks_address: str | None = None,
    init_block: int = 60_000_000,
) -> ResolvedPoolKey:
    """Build a ResolvedPoolKey whose pool_id_hex re-derives from the
    PoolKey (32 bytes) — the Owner-pinned 20-byte PoolId is treated as
    a contract defect (see :data:`RESOLVE_PINNED_POOL_ID_SIZE_DEFECT`)
    and the tests below surface it explicitly.

    The helper defaults ``pool_id_hex`` to the keccak256 re-derivation
    of the second-pool PoolKey so the resolver's
    ``resolve_ok`` path is exercised; the contract-defect path uses
    the Owner-pinned 20-byte PoolId directly.
    """
    pool_key = _second_pool_pool_key(
        hooks=Address.from_hex(hooks_address) if hooks_address is not None else Address.zero()
    )
    derived_hex = "0x" + compute_pool_id(pool_key).to_bytes(32, "big").hex()
    return ResolvedPoolKey(
        chain_id=chain_id,
        pool_id_hex=pool_id_hex if pool_id_hex is not None else derived_hex,
        pool_key=pool_key,
        init_block=init_block,
        block_hash=_hash_for_block(init_block),
        derived_pool_id_hex=derived_hex,
    )


def test_resolve_second_pool_identity_success_zero_hooks() -> None:
    """When the operator-supplied ``PoolKey`` re-derives the
    32-byte keccak256 ``PoolId`` the resolver returns ``resolve_ok``;
    the ``hooks_is_zero`` bit reflects the canonical zero address."""
    resolved = _resolved_second_pool()
    # ``resolved.pool_id_hex`` is the derived 32-byte keccak256
    # digest by default; pinning that value matches the derivation.
    result = resolve_second_pool_identity(
        resolved=resolved,
        pinned_pool_id_hex=resolved.pool_id_hex,
    )
    assert result.outcome == RESOLVE_OK
    assert result.ok
    assert result.resolved is not None
    assert result.resolved.hooks_is_zero is True
    assert classify_second_pool_support_level(resolved) == SUPPORT_LEVEL_INGESTION


def test_resolve_second_pool_identity_success_nonzero_hooks() -> None:
    """When the resolved hooks address is non-zero the resolver still
    returns ``resolve_ok`` (the keccak256 check passes) but the
    T023 classification caps the support level at ``ingestion`` per
    the T038 contract."""
    hooks_hex = "0x" + "ab" * 20
    resolved = _resolved_second_pool(hooks_address=hooks_hex)
    # ``resolved.pool_id_hex`` is the derived 32-byte keccak256
    # digest for the resolved PoolKey (which now has nonzero hooks).
    result = resolve_second_pool_identity(
        resolved=resolved,
        pinned_pool_id_hex=resolved.pool_id_hex,
    )
    assert result.outcome == RESOLVE_OK
    assert result.resolved is not None
    assert result.resolved.hooks_is_zero is False
    assert classify_second_pool_support_level(resolved) == SUPPORT_LEVEL_INGESTION


def test_resolve_second_pool_identity_halts_on_pool_id_mismatch() -> None:
    """When the operator-supplied ``PoolKey`` re-derives a different
    32-byte ``PoolId`` the resolver returns ``resolve_pool_id_mismatch``
    and halts the run rather than reconciling by hand."""
    wrong_pool_id = PoolId(int.from_bytes(b"\x99" * 32, "big"))
    resolved = _resolved_second_pool()
    result = resolve_second_pool_identity(
        resolved=resolved,
        pinned_pool_id_hex=wrong_pool_id.to_hex(),
    )
    assert result.outcome == RESOLVE_POOL_ID_MISMATCH
    assert result.ok is False
    assert result.resolved is None
    assert "keccak256" in result.error_detail


def test_owner_pinned_20_byte_value_surfaces_as_hook_address_defensive_guard() -> None:
    """Defensive-guard test: when a caller explicitly feeds the
    20-byte Owner-pinned hook contract address as a V4 ``PoolId``
    (the attempt-1 misuse pattern), the resolver surfaces
    ``resolve_pinned_pool_id_size_defect`` with an error detail
    that names the 2026-09-18 T038 amendment semantics.

    Per the 2026-09-18 amendment, the Owner-pinned value
    ``0xEd50bDeeA8aDC232f159486192a4157281D722ff`` is a **hook
    contract address** (a 20-byte lookup signal), NOT a V4
    ``PoolId`` (which is ``keccak256(abi.encode(PoolKey))`` and 32
    bytes). The resolver's defensive guard rejects the misuse
    rather than silently producing a wrong answer; the new entry
    point ``resolve_second_pool_via_hook_scan`` is the supported
    path that scans ``Initialize`` logs filtered by the pinned
    hook address.
    """
    # Sanity: confirm the pinned identifier is address-sized.
    assert len(SECOND_POOL_POOL_ID_HEX) - 2 == 40
    resolved = _resolved_second_pool()
    result = resolve_second_pool_identity(
        resolved=resolved,
        pinned_pool_id_hex=SECOND_POOL_POOL_ID_HEX,
    )
    assert result.outcome == RESOLVE_PINNED_POOL_ID_SIZE_DEFECT
    assert result.ok is False
    # The new error text reflects the 2026-09-18 amendment: the
    # pinned value is a hook contract address (lookup signal),
    # not a V4 PoolId.
    assert "hook contract address" in result.error_detail
    assert "lookup signal" in result.error_detail
    assert "20 bytes" in result.error_detail
    assert "32 bytes" in result.error_detail
    assert "resolve_second_pool_via_hook_scan" in result.error_detail


def test_two_pool_report_incomplete_with_owner_pinned_20_byte_pool_id() -> None:
    """When the Owner-pinned 20-byte PoolId is fed to the resolver
    the combined T038 report is ``complete=False`` with the
    ``resolve_pinned_pool_id_size_defect`` blocker recorded; the
    audit trail names the contract defect rather than silently
    claiming coverage."""
    plan = _included_plan()
    resolved = _resolved_second_pool(init_block=60_000_000)
    result = resolve_second_pool_identity(
        resolved=resolved,
        pinned_pool_id_hex=SECOND_POOL_POOL_ID_HEX,
    )
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data/reference",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=True,
        reference_pool_event_index_parquet_match=True,
        second_pool_data_root="/data/second",
        second_pool_manifest_checksum="0x" + "cd" * 32,
        second_pool_partition_reconciliation_agreement=True,
        second_pool_event_index_parquet_match=True,
        second_pool_resolution=result,
        second_pool_resolved=resolved,
    )
    assert report.complete is False
    assert RESOLVE_PINNED_POOL_ID_SIZE_DEFECT in report.qualification_blockers


def test_resolve_second_pool_identity_window_unresolved() -> None:
    """When the operator reports the ``Initialize`` log is outside
    the search bounds the resolver returns
    ``resolve_window_unresolved`` with the bounds recorded for the
    audit trail; no PoolKey guess is produced."""
    result = resolve_second_pool_identity(
        resolved=None,
        search_bounds=(50_000_000, 60_000_000),
    )
    assert result.outcome == RESOLVE_WINDOW_UNRESOLVED
    assert result.pool_init_outside_window is True
    assert result.search_bounds == (50_000_000, 60_000_000)
    assert result.resolved is None
    assert result.candidate.pool_id_hex == SECOND_POOL_POOL_ID_HEX
    assert result.candidate.pool_init_block is None


def test_resolve_second_pool_identity_halts_on_chain_id_mismatch() -> None:
    """A resolved ``PoolKey`` whose ``chain_id`` differs from the
    Owner-pinned chain id is a hard error; the resolver surfaces
    ``resolve_chain_id_mismatch``."""
    resolved = _resolved_second_pool(chain_id=1)
    result = resolve_second_pool_identity(resolved=resolved)
    assert result.outcome == RESOLVE_CHAIN_ID_MISMATCH
    assert result.resolved is None
    assert "chain_id" in result.error_detail


# ---------------------------------------------------------------------------
# Hook-address-scanning resolver (T038 amendment, 2026-09-18)
# ---------------------------------------------------------------------------


def _decoded_initialize_log(
    *,
    pool_key: PoolKey | None = None,
    block_number: int = 60_000_000,
    tx_hash: str | None = None,
    log_index: int | None = 0,
) -> DecodedInitialize:
    """Build a synthetic ``DecodedInitialize`` for hook-scan tests.

    Each log is decoded end-to-end: the 32-byte ``pool_id`` field is
    the keccak256 re-derivation of ``pool_key`` (so the decoder's
    internal consistency check, which the resolver inherits, is
    satisfied). The ``pool_key`` defaults to the synthetic
    second-pool PoolKey with the pinned hook contract address; tests
    that exercise non-matching hooks pass an explicit ``pool_key``.
    """
    pk = (
        pool_key
        if pool_key is not None
        else _second_pool_pool_key(hooks=Address.from_hex(SECOND_POOL_HOOK_ADDRESS_HEX))
    )
    derived_int = compute_pool_id(pk)
    return DecodedInitialize(
        pool_key=pk,
        pool_id=PoolId(derived_int),
        block_number=block_number,
        tx_hash=tx_hash if tx_hash is not None else "0x" + "ab" * 32,
        log_index=log_index,
        sqrt_price_x96=(1 << 96),
        initial_tick=0,
    )


def test_resolve_second_pool_via_hook_scan_matches_pinned_address() -> None:
    """The hook-scanning resolver picks the ``Initialize`` log whose
    decoded ``hooks`` field equals the Owner-pinned hook contract
    address and returns ``resolve_ok`` with the chain-emitted
    32-byte ``PoolId`` recorded in the ``ResolvedPoolKey``.

    The PoolKey re-derivation check
    ``keccak256(abi.encode(PoolKey)) == PoolId`` holds because the
    decoder enforces it and the resolver re-verifies it
    defensively.
    """
    log = _decoded_initialize_log(block_number=60_000_000)
    result = resolve_second_pool_via_hook_scan(
        initialize_logs=(log,),
        search_bounds=(50_000_000, 70_000_000),
    )
    assert result.outcome == RESOLVE_OK
    assert result.ok is True
    assert result.resolved is not None
    # The resolved PoolId is the 32-byte chain-emitted keccak256
    # digest, NOT the 20-byte pinned hook contract address.
    assert len(result.resolved.pool_id_hex) == 2 + 64
    assert result.resolved.pool_id_hex == log.pool_id.to_hex()
    # The hooks field of the resolved PoolKey equals the pinned
    # hook address (compared case-insensitively: the canonical
    # ``Address.to_hex()`` rendering is lowercase).
    assert result.resolved.pool_key.hooks.to_hex() == SECOND_POOL_HOOK_ADDRESS_HEX.lower()
    # PoolKey re-derivation check: derived == chain-emitted.
    derived_int = compute_pool_id(result.resolved.pool_key)
    derived_hex = "0x" + derived_int.to_bytes(32, "big").hex()
    assert derived_hex == result.resolved.pool_id_hex
    assert result.resolved.derived_pool_id_hex == derived_hex
    # Init block and chain id propagate.
    assert result.resolved.init_block == 60_000_000
    assert result.resolved.chain_id == SECOND_POOL_CHAIN_ID


def test_resolve_second_pool_via_hook_scan_ignores_non_matching_logs() -> None:
    """The hook-scanning resolver ignores ``Initialize`` logs whose
    decoded ``hooks`` field differs from the Owner-pinned hook
    contract address; a matching log later in the batch is still
    picked."""
    non_match_pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3000,
        tick_spacing=60,
        hooks=Address.zero(),  # not the pinned hook address
    )
    non_match = _decoded_initialize_log(pool_key=non_match_pk, block_number=60_000_000)
    match = _decoded_initialize_log(block_number=60_000_001)
    result = resolve_second_pool_via_hook_scan(
        initialize_logs=(non_match, match),
        search_bounds=(50_000_000, 70_000_000),
    )
    assert result.outcome == RESOLVE_OK
    assert result.resolved is not None
    assert result.resolved.pool_key.hooks.to_hex() == SECOND_POOL_HOOK_ADDRESS_HEX.lower().lower()
    assert result.resolved.init_block == 60_000_001


def test_resolve_second_pool_via_hook_scan_reports_pool_init_outside_window() -> None:
    """When no hook-matched ``Initialize`` is found inside the
    candidate window the resolver returns
    ``resolve_window_unresolved`` with the search bounds recorded
    for the audit trail; the second pool is excluded rather than
    the resolver guessing values.

    The contract forbids guessing the second pool's ``PoolKey``,
    fee, tick spacing or hooks from the pinned hook address or
    from any other identifier without a hook-matched
    ``Initialize`` log inside the candidate window.
    """
    non_match_pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3000,
        tick_spacing=60,
        hooks=Address.zero(),
    )
    non_match = _decoded_initialize_log(pool_key=non_match_pk, block_number=60_000_000)
    bounds = (50_000_000, 70_000_000)
    result = resolve_second_pool_via_hook_scan(
        initialize_logs=(non_match,),
        search_bounds=bounds,
    )
    assert result.outcome == RESOLVE_WINDOW_UNRESOLVED
    assert result.pool_init_outside_window is True
    assert result.resolved is None
    assert result.search_bounds == bounds
    assert result.candidate.pool_id_hex == SECOND_POOL_HOOK_ADDRESS_HEX
    assert "0xEd50bDeeA8aDC232f159486192a4157281D722ff".lower() in result.error_detail
    assert "no Initialize log matched" in result.error_detail


def test_resolve_second_pool_via_hook_scan_reports_pool_init_outside_window_empty() -> None:
    """An empty ``Initialize`` log batch (the operator scanned the
    candidate window and found no logs at all) also returns
    ``resolve_window_unresolved`` with the search bounds; no
    PoolKey guess is produced."""
    bounds = (60_000_000, 70_000_000)
    result = resolve_second_pool_via_hook_scan(
        initialize_logs=(),
        search_bounds=bounds,
    )
    assert result.outcome == RESOLVE_WINDOW_UNRESOLVED
    assert result.resolved is None
    assert result.search_bounds == bounds
    assert result.candidate.pool_id_hex == SECOND_POOL_HOOK_ADDRESS_HEX


def test_resolve_second_pool_via_hook_scan_ambiguous_matches_halt() -> None:
    """When more than one ``Initialize`` log matches the pinned
    hook contract address the resolver halts closed with
    ``resolve_hook_address_ambiguous``; the lookup signal is
    ambiguous and the resolver refuses to pick one without
    operator intervention."""
    pk_a = _second_pool_pool_key(hooks=Address.from_hex(SECOND_POOL_HOOK_ADDRESS_HEX))
    pk_b = PoolKey(
        currency0=Currency.from_hex("0x" + "33" * 20),
        currency1=Currency.from_hex("0x" + "44" * 20),
        fee=500,
        tick_spacing=10,
        hooks=Address.from_hex(SECOND_POOL_HOOK_ADDRESS_HEX),
    )
    log_a = _decoded_initialize_log(pool_key=pk_a, block_number=60_000_000, log_index=0)
    log_b = _decoded_initialize_log(pool_key=pk_b, block_number=60_000_001, log_index=0)
    result = resolve_second_pool_via_hook_scan(
        initialize_logs=(log_a, log_b),
        search_bounds=(50_000_000, 70_000_000),
    )
    assert result.outcome == RESOLVE_HOOK_ADDRESS_AMBIGUOUS
    assert result.resolved is None
    assert result.search_bounds == (50_000_000, 70_000_000)
    assert "ambiguous" in result.error_detail
    assert log_a.pool_id.to_hex() in result.error_detail
    assert log_b.pool_id.to_hex() in result.error_detail


def test_resolve_second_pool_via_hook_scan_defaults_to_pinned_hook_address() -> None:
    """The hook-scanning resolver defaults the pinned hook address
    to the Owner-pinned value
    ``0xEd50bDeeA8aDC232f159486192a4157281D722ff`` so the
    operator does not have to repeat it. The default matches the
    contract value exactly.
    """
    assert SECOND_POOL_HOOK_ADDRESS_HEX == "0xEd50bDeeA8aDC232f159486192a4157281D722ff"
    log = _decoded_initialize_log()
    result = resolve_second_pool_via_hook_scan(initialize_logs=(log,))
    assert result.outcome == RESOLVE_OK
    assert result.resolved is not None
    assert result.resolved.pool_key.hooks.to_hex() == SECOND_POOL_HOOK_ADDRESS_HEX.lower()


def test_resolve_second_pool_via_hook_scan_accepts_str_hook_address() -> None:
    """The hook-scanning resolver accepts either a typed
    :class:`Address` or a 0x-hex string for ``pinned_hook_address``
    so operators can pass whichever form they already hold."""
    pk = _second_pool_pool_key(hooks=Address.from_hex("0x" + "ab" * 20))
    log = _decoded_initialize_log(pool_key=pk)
    # Pass the hook address as a 0x-hex string.
    result = resolve_second_pool_via_hook_scan(
        pinned_hook_address="0x" + "ab" * 20,
        initialize_logs=(log,),
    )
    assert result.outcome == RESOLVE_OK
    assert result.resolved is not None
    assert result.resolved.pool_key.hooks.to_hex() == "0x" + "ab" * 20


def test_resolve_second_pool_via_hook_scan_result_propagates_block_number() -> None:
    """The hook-scanning resolver records the ``Initialize`` block
    number on the :class:`ResolvedPoolKey` so the window rule can
    place the second pool in the coverage plan."""
    log = _decoded_initialize_log(block_number=58_000_000)
    result = resolve_second_pool_via_hook_scan(initialize_logs=(log,))
    assert result.outcome == RESOLVE_OK
    assert result.resolved is not None
    assert result.resolved.init_block == 58_000_000


def test_resolve_second_pool_via_hook_scan_propagates_to_window_plan() -> None:
    """The resolved ``init_block`` from the hook-scan resolver
    flows through :func:`apply_window_rule` so the second pool is
    placed in the coverage plan with the correct extension below
    ``end - 10_000_000``."""
    log = _decoded_initialize_log(block_number=60_000_000)
    result = resolve_second_pool_via_hook_scan(initialize_logs=(log,))
    assert result.ok is True
    resolved_init = result.resolved.init_block if result.resolved is not None else 0
    pin = _window_pin(block_number=70_000_000)
    plan = apply_window_rule(
        window_pin=pin,
        candidates=(second_pool_candidate(pool_init_block=resolved_init),),
    )
    assert plan.included_pool_aliases == ("second",)
    assert plan.pool_outcomes[0].pool_init_block == 60_000_000
    assert plan.pool_outcomes[0].coverage_from_block == 60_000_000


def test_resolve_second_pool_via_hook_scan_does_not_trigger_defensive_guard() -> None:
    """The hook-scan resolver does NOT surface
    ``resolve_pinned_pool_id_size_defect``; it is the new entry
    point and the defensive guard is retained only on the
    ``resolve_second_pool_identity`` / convenience wrapper APIs
    that still accept ``pinned_pool_id_hex``."""

    log = _decoded_initialize_log()
    result = resolve_second_pool_via_hook_scan(initialize_logs=(log,))
    assert result.outcome == RESOLVE_OK
    assert result.outcome != RESOLVE_PINNED_POOL_ID_SIZE_DEFECT


def test_two_pool_report_complete_with_hook_scan_resolved_pool() -> None:
    """The combined T038 machine report is ``complete=True`` when
    the second-pool resolver outcome is ``resolve_ok`` produced
    by the hook-scan path; the per-pool T034 reports agree and
    the per-pool data roots are recorded.

    This is the end-to-end happy path the new contract requires:
    the second pool's ``PoolKey`` and 32-byte ``PoolId`` were
    resolved on chain by scanning ``Initialize`` logs filtered by
    the pinned hook contract address.
    """
    pin = _window_pin(block_number=70_000_000)
    log = _decoded_initialize_log(block_number=60_000_000)
    result = resolve_second_pool_via_hook_scan(
        initialize_logs=(log,),
        search_bounds=(50_000_000, 70_000_000),
    )
    resolved = result.resolved
    assert resolved is not None
    plan = apply_window_rule(
        window_pin=pin,
        candidates=(
            reference_pool_candidate(pool_init_block=60_000_000),
            second_pool_candidate(pool_init_block=resolved.init_block),
        ),
    )
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data/reference",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=True,
        reference_pool_event_index_parquet_match=True,
        second_pool_data_root="/data/second",
        second_pool_manifest_checksum="0x" + "cd" * 32,
        second_pool_partition_reconciliation_agreement=True,
        second_pool_event_index_parquet_match=True,
        second_pool_resolution=result,
        second_pool_resolved=resolved,
    )
    assert report.complete is True
    assert report.second_pool_resolution is not None
    assert report.second_pool_resolution.outcome == RESOLVE_OK
    assert report.second_pool_support_level == SUPPORT_LEVEL_INGESTION


def test_two_pool_report_incomplete_when_hook_scan_finds_no_match() -> None:
    """When the hook-scan resolver returns
    ``resolve_window_unresolved`` (no hook-matched ``Initialize``
    in the search bounds) the combined T038 report is
    ``complete=False`` and the second pool is excluded from the
    qualified dataset rather than the resolver guessing values.
    """
    pin = _window_pin(block_number=70_000_000)
    bounds = (60_000_000, 70_000_000)
    result = resolve_second_pool_via_hook_scan(
        initialize_logs=(),
        search_bounds=bounds,
    )
    plan = apply_window_rule(
        window_pin=pin,
        candidates=(
            reference_pool_candidate(pool_init_block=60_000_000),
            second_pool_candidate(pool_init_block=None),
        ),
    )
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data/reference",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=True,
        reference_pool_event_index_parquet_match=True,
        second_pool_resolution=result,
    )
    assert report.complete is False
    # The resolver outcome is ``resolve_window_unresolved``; the
    # combined report's blockers list includes it.
    assert RESOLVE_WINDOW_UNRESOLVED in report.qualification_blockers


def test_build_resolved_pool_key_compute_hash_keccak_offline() -> None:
    """The offline keccak256 re-derivation check reproduces the
    canonical V4 ``PoolId`` exactly when the PoolKey matches the
    Owner-pinned PoolId; the bytes match the ``encode_pool_key``
    ABI encoding the Solidity reference produces."""
    pool_key = _second_pool_pool_key()
    derived = compute_pool_id(pool_key)
    encoded = encode_pool_key(pool_key)
    assert len(encoded) == 160
    # The keccak256 of the 160-byte encoding is the PoolId.
    expected = int.from_bytes(keccak(encoded), "big")
    assert derived == expected


def test_build_second_pool_resolve_result_from_resolved_fields_ok() -> None:
    """The convenience wrapper constructs a ResolvedPoolKey and runs
    the offline keccak256 re-derivation check; matching PoolKeys
    return ``resolve_ok``."""
    pool_key = _second_pool_pool_key()
    derived_hex = "0x" + compute_pool_id(pool_key).to_bytes(32, "big").hex()
    result = build_second_pool_resolve_result_from_resolved_fields(
        chain_id=SECOND_POOL_CHAIN_ID,
        pool_id_hex=derived_hex,
        currency0_address=pool_key.currency0.to_address().to_hex(),
        currency1_address=pool_key.currency1.to_address().to_hex(),
        fee=pool_key.fee,
        tick_spacing=pool_key.tick_spacing,
        hooks_address=pool_key.hooks.to_hex(),
        init_block=60_000_000,
        pinned_pool_id_hex=derived_hex,
    )
    assert result.outcome == RESOLVE_OK


def test_build_second_pool_resolve_result_surfaces_owner_20_byte_defect() -> None:
    """The convenience wrapper also surfaces the Owner-pinned
    20-byte PoolId as ``resolve_pinned_pool_id_size_defect`` so
    the audit trail records the contract defect regardless of
    which entry point the operator uses."""
    pool_key = _second_pool_pool_key()
    result = build_second_pool_resolve_result_from_resolved_fields(
        chain_id=SECOND_POOL_CHAIN_ID,
        pool_id_hex=SECOND_POOL_POOL_ID_HEX,
        currency0_address=pool_key.currency0.to_address().to_hex(),
        currency1_address=pool_key.currency1.to_address().to_hex(),
        fee=pool_key.fee,
        tick_spacing=pool_key.tick_spacing,
        hooks_address=pool_key.hooks.to_hex(),
        init_block=60_000_000,
    )
    assert result.outcome == RESOLVE_PINNED_POOL_ID_SIZE_DEFECT
    assert "20 bytes" in result.error_detail


def test_build_second_pool_resolve_result_decode_error() -> None:
    """A ``PoolKey`` whose constructor rejects the structural fields
    is surfaced as ``resolve_pool_key_decode_error`` rather than
    raising.

    The wrapper is fed a 64-byte placeholder ``pool_id_hex`` (the
    shape a chain-resolved PoolId carries); a 20-byte input would
    surface the ``RESOLVE_PINNED_POOL_ID_SIZE_DEFECT`` defensive
    guard instead and bypass the constructor.
    """
    placeholder_pool_id = "0x" + "11" * 32
    # The ``0x...ZZZ`` currency is malformed; the constructor must
    # surface the error rather than silently reconstructing the key.
    result = build_second_pool_resolve_result_from_resolved_fields(
        chain_id=SECOND_POOL_CHAIN_ID,
        pool_id_hex=placeholder_pool_id,
        currency0_address="not_a_hex",
        currency1_address="0x" + "22" * 20,
        fee=3000,
        tick_spacing=60,
        hooks_address="0x" + "00" * 20,
        init_block=60_000_000,
    )
    assert result.outcome == RESOLVE_POOL_KEY_DECODE_ERROR


def test_build_second_pool_resolve_result_currency_ordering_violation() -> None:
    """A resolved PoolKey whose currency ordering violates the V4
    invariant ``currency0 < currency1`` is surfaced as
    ``resolve_pool_key_decode_error`` (the contract forbids silent
    reordering).

    The wrapper is fed a 64-byte placeholder ``pool_id_hex`` (the
    shape a chain-resolved PoolId carries).
    """
    placeholder_pool_id = "0x" + "11" * 32
    result = build_second_pool_resolve_result_from_resolved_fields(
        chain_id=SECOND_POOL_CHAIN_ID,
        pool_id_hex=placeholder_pool_id,
        # currency0 > currency1 (uint160): this is a violation.
        currency0_address="0x" + "ff" * 20,
        currency1_address="0x" + "11" * 20,
        fee=3000,
        tick_spacing=60,
        hooks_address="0x" + "00" * 20,
        init_block=60_000_000,
    )
    assert result.outcome == RESOLVE_POOL_KEY_DECODE_ERROR


# ---------------------------------------------------------------------------
# Per-pool T034 report + two-pool orchestrator
# ---------------------------------------------------------------------------


def _per_pool_window_outcome(
    *, alias: str = "reference", included: bool = True
) -> PoolWindowOutcome:
    if included:
        return PoolWindowOutcome(
            pool_alias=alias,
            pool_id_hex=REFERENCE_POOL_ID_HEX
            if alias == "reference"
            else _second_pool_pool_key().to_pool_id().to_hex(),
            pool_init_block=60_000_000,
            outcome=OUTCOME_POOL_INCLUDED,
            coverage_from_block=60_000_000,
            default_start_block=60_000_000,
            extension_below_default_start=0,
        )
    # Excluded pool: use a 32-byte derived hex so PoolId.from_hex
    # in the report builder accepts it; the Owner-pinned 20-byte
    # value is treated as a contract defect by the resolver and
    # never reaches the per-pool report builder in production.
    return PoolWindowOutcome(
        pool_alias=alias,
        pool_id_hex=_second_pool_pool_key().to_pool_id().to_hex(),
        pool_init_block=40_000_000,
        outcome=OUTCOME_POOL_INIT_OUTSIDE_WINDOW,
        coverage_from_block=60_000_000,
        default_start_block=60_000_000,
        extension_below_default_start=0,
        gap_blocks=20_000_000,
        reason="Initialize block 40000000 lies 20000000 blocks below default_start 60000000",
    )


def _included_plan(outcome_aliases: tuple[str, ...] = ("reference", "second")) -> TwoPoolWindowPlan:
    pin = _window_pin(block_number=70_000_000)
    candidates = []
    for alias in outcome_aliases:
        if alias == "reference":
            candidates.append(reference_pool_candidate(pool_init_block=60_000_000))
        else:
            # Use a 32-byte pool_id_hex that re-derives from the
            # second-pool PoolKey so the resolver's ``resolve_ok``
            # path is exercised (the Owner-pinned 20-byte PoolId
            # is treated as a contract defect and surfaced by a
            # dedicated test below).
            resolved_id_hex = _second_pool_pool_key().to_pool_id().to_hex()
            candidates.append(
                TwoPoolCandidate(
                    pool_alias="second",
                    pool_id_hex=resolved_id_hex,
                    pool_init_block=60_000_000,
                )
            )
    return apply_window_rule(window_pin=pin, candidates=tuple(candidates))


def test_per_pool_report_complete_when_no_blockers() -> None:
    """An included pool whose per-pool reconciliation agrees and
    whose event_index / Parquet agree produces ``complete=True``."""
    report = build_two_pool_report(
        window_plan=apply_window_rule(
            window_pin=_window_pin(block_number=70_000_000),
            candidates=(reference_pool_candidate(pool_init_block=60_000_000),),
        ),
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data/reference",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=True,
        reference_pool_event_index_parquet_match=True,
        second_pool_resolution=None,
    )
    included_report = report.per_pool("reference")
    assert included_report.complete is True
    assert included_report.coverage_outcome == "complete"
    assert included_report.window_outcome == "pool_included"
    assert included_report.partition_reconciliation_agreement is True
    assert included_report.event_index_parquet_match is True
    assert included_report.data_root == "/data/reference"


def test_per_pool_report_incomplete_when_partition_mismatch() -> None:
    """An included pool whose per-pool reconciliation disagrees
    produces ``complete=False`` with the blocker recorded."""
    plan = apply_window_rule(
        window_pin=_window_pin(block_number=70_000_000),
        candidates=(reference_pool_candidate(pool_init_block=60_000_000),),
    )
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data/reference",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=False,
        reference_pool_event_index_parquet_match=True,
        second_pool_resolution=None,
    )
    included_report = report.per_pool("reference")
    assert included_report.complete is False
    assert included_report.coverage_outcome == "incomplete"
    assert "partition_event_index_parquet_mismatch" in included_report.qualification_blockers


def test_excluded_pool_report_carries_gap_size_and_outcome() -> None:
    """An excluded pool's per-pool T034 report carries the gap size
    and the ``pool_init_outside_window`` blocker; ``complete=False``
    and ``coverage_outcome=excluded``."""
    plan = apply_window_rule(
        window_pin=_window_pin(block_number=70_000_000),
        candidates=(second_pool_candidate(pool_init_block=40_000_000),),
    )
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="",
        reference_pool_manifest_checksum="",
        reference_pool_partition_reconciliation_agreement=False,
        reference_pool_event_index_parquet_match=False,
        second_pool_resolution=None,
    )
    excluded_report = report.per_pool("second")
    assert excluded_report.complete is False
    assert excluded_report.coverage_outcome == "excluded"
    assert excluded_report.window_outcome == OUTCOME_POOL_INIT_OUTSIDE_WINDOW
    assert "pool_init_outside_window" in excluded_report.qualification_blockers
    gap_finding = next(
        finding
        for finding in excluded_report.findings
        if finding["reason_code"] == "pool_init_outside_window"
    )
    assert gap_finding["detail"]["gap_blocks"] == 20_000_000
    assert report.excluded_pool_aliases == ("second",)


def test_two_pool_report_complete_only_when_all_pools_complete() -> None:
    """The combined T038 machine report is ``complete=True`` only
    when every included pool's per-pool T034 report is
    ``complete=True`` AND the second-pool resolver outcome is
    ``resolve_ok``."""
    plan = _included_plan()
    resolved = _resolved_second_pool()
    # Pass the derived 32-byte PoolId so the keccak256 re-derivation
    # check agrees; the Owner-pinned 20-byte PoolId is treated as a
    # contract defect (see test_owner_pinned_pool_id_is_20_bytes_contract_defect).
    result = resolve_second_pool_identity(
        resolved=resolved,
        pinned_pool_id_hex=resolved.pool_id_hex,
    )
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data/reference",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=True,
        reference_pool_event_index_parquet_match=True,
        second_pool_data_root="/data/second",
        second_pool_manifest_checksum="0x" + "cd" * 32,
        second_pool_partition_reconciliation_agreement=True,
        second_pool_event_index_parquet_match=True,
        second_pool_resolution=result,
        second_pool_resolved=resolved,
    )
    assert report.complete is True
    assert report.second_pool_support_level == SUPPORT_LEVEL_INGESTION
    assert report.second_pool_resolution is not None
    assert report.second_pool_resolution.outcome == RESOLVE_OK


def test_two_pool_report_incomplete_when_second_pool_resolution_fails() -> None:
    """When the second-pool resolver outcome is not ``resolve_ok``
    the combined report is ``complete=False`` and the blocker is
    recorded."""
    plan = _included_plan()
    resolved = _resolved_second_pool()
    bad_result = SecondPoolResolveResult(
        outcome=RESOLVE_POOL_ID_MISMATCH,
        resolved=None,
        search_bounds=None,
        error_detail="mismatch",
        candidate=second_pool_candidate(resolved.init_block),
    )
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data/reference",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=True,
        reference_pool_event_index_parquet_match=True,
        second_pool_resolution=bad_result,
    )
    assert report.complete is False
    assert "resolve_pool_id_mismatch" in report.qualification_blockers


def test_two_pool_report_incomplete_when_second_pool_resolution_missing() -> None:
    """A missing second-pool resolver outcome forces ``complete=False``;
    the T038 contract requires the second pool's identity to be
    resolved on chain."""
    plan = _included_plan()
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data/reference",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=True,
        reference_pool_event_index_parquet_match=True,
        second_pool_resolution=None,
    )
    assert report.complete is False
    assert "second_pool_resolution_missing" in report.qualification_blockers


def test_two_pool_report_to_dict_is_json_serialisable() -> None:
    """The combined T038 machine report is JSON-serialisable so the
    audit-trail record is reproducible."""
    plan = _included_plan()
    resolved = _resolved_second_pool()
    result = resolve_second_pool_identity(resolved=resolved)
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data/reference",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=True,
        reference_pool_event_index_parquet_match=True,
        second_pool_data_root="/data/second",
        second_pool_manifest_checksum="0x" + "cd" * 32,
        second_pool_partition_reconciliation_agreement=True,
        second_pool_event_index_parquet_match=True,
        second_pool_resolution=result,
        second_pool_resolved=resolved,
    )
    blob = json.dumps(report.to_dict(), sort_keys=True, ensure_ascii=False)
    assert "window_pin" in blob
    assert "per_pool_reports" in blob
    assert "second_pool_resolution" in blob


# ---------------------------------------------------------------------------
# Failure-path evidence
# ---------------------------------------------------------------------------


def test_all_documented_failure_path_kinds_have_matching_reason_codes() -> None:
    """Every documented two-pool failure-path kind maps to a stable
    reason code; the closed mapping is enforced by the
    :class:`TwoPoolFailurePathEvidence` validator."""
    rows = all_documented_two_pool_failure_path_evidence()
    assert len(rows) == len(DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS)
    expected_reasons = {
        FAILURE_PATH_KIND_FINALIZED_UNAVAILABLE: "finalized_unavailable",
        FAILURE_PATH_KIND_FINALIZED_DISAGREEMENT: "finalized_endpoint_disagreement",
        FAILURE_PATH_KIND_REQUEST_WIDER_THAN_CAPABILITY: "range_too_large",
        FAILURE_PATH_KIND_HTTP_429: "http_429_rate_limit",
        FAILURE_PATH_KIND_BUDGET_EXHAUSTED: "budget_exhausted",
    }
    for row in rows:
        assert row.reason_code == expected_reasons[row.kind]


def test_two_pool_failure_path_evidence_rejects_unknown_kind() -> None:
    """An unknown failure-path kind is rejected with a clear
    error rather than silently coerced to a stable reason code."""
    with pytest.raises(ValueError):
        TwoPoolFailurePathEvidence(
            kind="unknown_failure_path_kind",
            reason_code="some_reason_code",
        )


def test_two_pool_failure_path_evidence_rejects_kind_reason_mismatch() -> None:
    """The closed kind-to-reason mapping is enforced."""
    with pytest.raises(ValueError):
        TwoPoolFailurePathEvidence(
            kind=FAILURE_PATH_KIND_FINALIZED_UNAVAILABLE,
            reason_code="http_429_rate_limit",
        )


def test_two_pool_failure_path_force_complete_false() -> None:
    """``finalized_unavailable`` and ``finalized_endpoint_disagreement``
    force ``complete=False`` regardless of the other check
    outcomes; the contract requires a pinned window end."""
    for kind, reason in (
        (FAILURE_PATH_KIND_FINALIZED_UNAVAILABLE, "finalized_unavailable"),
        (FAILURE_PATH_KIND_FINALIZED_DISAGREEMENT, "finalized_endpoint_disagreement"),
        (FAILURE_PATH_KIND_REQUEST_WIDER_THAN_CAPABILITY, "range_too_large"),
        (FAILURE_PATH_KIND_BUDGET_EXHAUSTED, "budget_exhausted"),
    ):
        ev = build_two_pool_failure_path_evidence(kind=kind)
        assert ev.reason_code == reason
        assert fail_complete_under_two_pool_failure_paths([ev]) is True
    # HTTP 429 alone does NOT force complete=False: the runner
    # retries; only documented budget / capability failures force
    # the run closed.
    ev_http = build_two_pool_failure_path_evidence(kind=FAILURE_PATH_KIND_HTTP_429)
    assert fail_complete_under_two_pool_failure_paths([ev_http]) is False


# ---------------------------------------------------------------------------
# Operator runbook
# ---------------------------------------------------------------------------


def test_two_pool_runbook_routes_acquisition_paths() -> None:
    """The runbook routes the wide pool-filtered scan to the primary
    endpoint and the sampled cross-validation / block-pinned
    StateView reads to the secondary endpoint."""
    plan = _included_plan()
    resolved = _resolved_second_pool()
    result = resolve_second_pool_identity(
        resolved=resolved,
        pinned_pool_id_hex=resolved.pool_id_hex,
    )
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data/reference",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=True,
        reference_pool_event_index_parquet_match=True,
        second_pool_data_root="/data/second",
        second_pool_manifest_checksum="0x" + "cd" * 32,
        second_pool_partition_reconciliation_agreement=True,
        second_pool_event_index_parquet_match=True,
        second_pool_resolution=result,
        second_pool_resolved=resolved,
    )
    runbook = build_two_pool_operator_runbook(
        per_pool_reports=report.per_pool_reports,
        window_plan=plan,
        second_pool_resolution_summary={
            "outcome": result.outcome,
            "pool_id_hex": resolved.pool_id_hex,
            "init_block": resolved.init_block,
            "hooks_address": resolved.pool_key.hooks.to_hex(),
        },
    )
    roles = {entry.role: entry for entry in runbook.endpoints}
    assert roles["wide_pool_filtered_scan"].endpoint_alias == DEFAULT_PRIMARY_ALIAS
    assert roles["sampled_cross_validation"].endpoint_alias == DEFAULT_SECONDARY_ALIAS
    assert roles["block_pinned_state_read"].endpoint_alias == DEFAULT_SECONDARY_ALIAS
    assert (
        str(DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL)
        in roles["wide_pool_filtered_scan"].measured_capability
    )
    assert (
        str(DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL)
        in roles["sampled_cross_validation"].measured_capability
    )
    markdown = runbook.to_markdown()
    assert "Two-Pool Ten-Million-Block Acquisition Runbook" in markdown
    assert "Window pin" in markdown
    assert "Endpoint routing" in markdown
    assert "Acquisition paths" in markdown
    assert "Limitations" in markdown
    # The Markdown rendering pins the window end + the per-pool
    # outcomes so the runbook is the audit-trail surface the T038
    # contract requires.
    assert "pinned finalized end block" in markdown.lower() or "window end" in markdown.lower()


def test_two_pool_runbook_includes_measured_capabilities() -> None:
    """The runbook records the measured per-endpoint capability
    bounds; the routing decisions must reference them."""
    runbook = build_two_pool_operator_runbook(
        primary_max_blocks_per_call=10_000,
        secondary_max_blocks_per_call=10,
    )
    rendered = runbook.to_markdown()
    assert "10_000" in rendered or "10000" in rendered
    assert "10 blocks" in rendered or "10-blocks-per-call" in rendered
    for entry in runbook.endpoints:
        assert entry.measured_capability


def test_two_pool_runbook_includes_excluded_pool_in_window_outcome() -> None:
    """When a pool is excluded as ``pool_init_outside_window`` the
    runbook's per-pool window outcome row records the exclusion
    with its gap size, not silently."""
    pin = _window_pin(block_number=70_000_000)
    plan = apply_window_rule(
        window_pin=pin,
        candidates=(second_pool_candidate(pool_init_block=40_000_000),),
    )
    runbook = build_two_pool_operator_runbook(window_plan=plan)
    markdown = runbook.to_markdown()
    assert "pool_init_outside_window" in markdown
    # Gap is 20,000,000 blocks; the runbook's per-pool window
    # outcome row renders the gap with comma separators.
    assert "20,000,000" in markdown


def test_two_pool_runbook_serialises_to_dict() -> None:
    """The runbook's ``to_dict`` is JSON-serialisable so the
    audit-trail record can be stored next to the qualification
    report."""
    plan = _included_plan()
    runbook = build_two_pool_operator_runbook(window_plan=plan)
    blob = json.dumps(runbook.to_dict(), sort_keys=True, ensure_ascii=False)
    assert "title" in blob
    assert "endpoints" in blob
    assert "window_plan" in blob


# ---------------------------------------------------------------------------
# Per-pool baseline surface (sanity: the per-pool report records
# the same baseline numbers the T036 reference pipeline does, so
# the T038 acceptance contract — "row counts compared against
# baseline, any difference raised" — holds at the per-pool level)
# ---------------------------------------------------------------------------


def test_per_pool_baseline_numbers_match_t036() -> None:
    """The per-pool T038 acceptance checks the dataset's own
    per-event-type counts against the T036 baseline; the baseline
    numbers must agree (T036 contract is the comparison target)."""
    assert BASELINE_INITIALIZE_COUNT == 1
    assert BASELINE_MODIFY_LIQUIDITY_COUNT == 578
    assert BASELINE_SWAP_COUNT == 3159
    assert BASELINE_PROTOCOL_FEE_UPDATED_COUNT == 1
    assert BASELINE_DISTINCT_BLOCKS == 3266
    assert BASELINE_TOTAL_EVENTS == 3739
    assert REFERENCE_CHAIN_ID == 4663


# ---------------------------------------------------------------------------
# TwoPoolCandidate validation
# ---------------------------------------------------------------------------


def test_two_pool_candidate_validates_pool_id_hex() -> None:
    """The candidate rejects malformed PoolId hex so a typo in the
    Owner-pinned identifier fails closed at the planner."""
    with pytest.raises(ValueError):
        TwoPoolCandidate(pool_alias="second", pool_id_hex="not_a_pool_id", pool_init_block=1)


def test_two_pool_candidate_rejects_negative_init_block() -> None:
    """A negative ``Initialize`` block is rejected with a clear
    error."""
    with pytest.raises(ValueError):
        TwoPoolCandidate(
            pool_alias="second",
            pool_id_hex=SECOND_POOL_POOL_ID_HEX,
            pool_init_block=-1,
        )


# ---------------------------------------------------------------------------
# PerPoolT034Report validation
# ---------------------------------------------------------------------------


def test_per_pool_report_validates_coverage_ordering() -> None:
    """A report whose ``coverage_from_block > coverage_to_block``
    is rejected at construction time."""
    with pytest.raises(ValueError):
        PerPoolT034Report(
            pool_alias="reference",
            pool_id_hex=REFERENCE_POOL_ID_HEX,
            chain_id=4663,
            contract_address=REFERENCE_TARGET.pool_manager_address.to_hex(),
            data_root="/data",
            pool_init_block=60_000_000,
            coverage_from_block=70_000_000,
            coverage_to_block=60_000_000,
            window_outcome="pool_included",
            coverage_outcome="complete",
        )


def test_per_pool_report_validates_window_outcome() -> None:
    """An unknown ``window_outcome`` is rejected."""
    with pytest.raises(ValueError):
        PerPoolT034Report(
            pool_alias="reference",
            pool_id_hex=REFERENCE_POOL_ID_HEX,
            chain_id=4663,
            contract_address=REFERENCE_TARGET.pool_manager_address.to_hex(),
            data_root="/data",
            pool_init_block=60_000_000,
            coverage_from_block=60_000_000,
            coverage_to_block=70_000_000,
            window_outcome="unknown",
            coverage_outcome="complete",
        )


# ---------------------------------------------------------------------------
# TwoPoolT038Report.per_pool() raises on unknown alias
# ---------------------------------------------------------------------------


def test_two_pool_report_per_pool_unknown_alias_raises() -> None:
    """An unknown pool alias surfaces a clear KeyError so the
    audit trail never silently drops a pool."""
    plan = _included_plan()
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=True,
        reference_pool_event_index_parquet_match=True,
        second_pool_resolution=None,
    )
    with pytest.raises(KeyError):
        report.per_pool("unknown_pool")


# ---------------------------------------------------------------------------
# End-to-end: combined T038 machine report
# ---------------------------------------------------------------------------


def test_end_to_end_two_pool_report_with_real_pin_and_resolved_keys() -> None:
    """A full two-pool T038 machine report assembled from a real
    pin, two resolved PoolKeys, and complete per-pool data;
    serialises to JSON for the evidence pack."""
    pin = _window_pin(block_number=70_000_000)
    resolved = _resolved_second_pool(init_block=60_000_000)
    # Use the derived 32-byte PoolId so the keccak256 re-derivation
    # check agrees; the Owner-pinned 20-byte PoolId is treated as a
    # contract defect and surfaced separately by the resolver.
    result = resolve_second_pool_identity(
        resolved=resolved,
        pinned_pool_id_hex=resolved.pool_id_hex,
    )
    plan = apply_window_rule(
        window_pin=pin,
        candidates=(
            reference_pool_candidate(pool_init_block=60_000_000),
            second_pool_candidate(pool_init_block=resolved.init_block),
        ),
    )
    report = build_two_pool_report(
        window_plan=plan,
        reference_pool=REFERENCE_TARGET,
        reference_pool_data_root="/data/reference",
        reference_pool_manifest_checksum="0x" + "ab" * 32,
        reference_pool_partition_reconciliation_agreement=True,
        reference_pool_event_index_parquet_match=True,
        second_pool_data_root="/data/second",
        second_pool_manifest_checksum="0x" + "cd" * 32,
        second_pool_partition_reconciliation_agreement=True,
        second_pool_event_index_parquet_match=True,
        second_pool_resolution=result,
        second_pool_resolved=resolved,
    )
    assert report.complete is True
    blob = report.to_dict()
    # Pin survives the JSON round-trip.
    assert blob["window_plan"]["window_pin"]["block_number"] == pin.block_number
    assert blob["window_plan"]["window_pin"]["block_hash"] == pin.block_hash
    # Both pools' per-pool reports are present and complete.
    by_alias = {r["pool_alias"]: r for r in blob["per_pool_reports"]}
    assert by_alias["reference"]["complete"] is True
    assert by_alias["second"]["complete"] is True
    # Second-pool resolution summary survives.
    assert blob["second_pool_resolution"]["outcome"] == RESOLVE_OK
    assert blob["second_pool_support_level"] == SUPPORT_LEVEL_INGESTION
