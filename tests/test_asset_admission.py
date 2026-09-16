"""Tests for the two-track asset admission module (T025).

Covers the T025 acceptance matrix:

- Track A is computed deterministically from T022 (registry) + T023
  (eligibility) + T024 (capability) evidence; same inputs -> same
  outputs across two runs.
- Track B is empty / ``pending_owner`` for every PoolRecord until an
  explicit Owner decision arrives.
- A (chain, PoolKey) with T024 bytecode drift blocks Track A admission
  (``admission_status = rejected_by_capability``).
- A (chain, PoolKey) with T023 ``proxy_detected`` blocks Track A
  admission.
- A (chain, PoolKey) with T023 ``hook_bytecode_unavailable`` does NOT
  auto-promote; Track A admission = ``ingestion_only_pending_review``.
- The P02 closeout report (when loaded from the pinned artifact set)
  confirms the three exit-gate conditions above.
- An OwnerDecision artifact can be supplied to Track B; only then does
  Track B transition from ``pending_owner`` to ``admitted`` (live
  promotion is OUT of scope for T025; Track B does NOT directly
  promote live).
- Audit trail: every row's evidence_pointers reference the originating
  artifacts (artifact SHA, deployment tx hash, decode rule version).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from robinhood_lp.discovery import (
    EligibilityDecision,
    HookEvidence,
    PoolRegistry,
    classify_pool,
)
from robinhood_lp.discovery.asset_admission import (
    DEFAULT_DECODE_RULE_VERSION,
    TRACK_A_ADMITTED,
    TRACK_A_INGESTION_ONLY_PENDING_REVIEW,
    TRACK_A_PENDING_CAPABILITY,
    TRACK_A_REJECTED_BY_CAPABILITY,
    TRACK_A_REJECTED_BY_PROXY,
    TRACK_B_ADMITTED,
    TRACK_B_PENDING_OWNER,
    TRACK_B_REJECTED_BY_OWNER,
    OperatorDecision,
    OperatorDecisionOutcome,
    P02CloseoutReport,
    build_p02_closeout_report,
    build_pool_admission,
    report_to_json,
    sha256_of_report,
)
from robinhood_lp.discovery.chain_capability import (
    CrossEndpointAgreement,
    DeploymentEvidence,
)
from robinhood_lp.protocol import Address, ChainId, Currency, PoolKey

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAIN_ID = 46630
DECODE_RULE_VERSION = DEFAULT_DECODE_RULE_VERSION
ARTIFACT_SHA256 = "5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f"
DEPLOYMENT_TX_HASH = "0xab" * 32


def _make_pool_key(
    *,
    c0: int = 0x10,
    c1: int = 0x20,
    fee: int = 3000,
    tick_spacing: int = 60,
    hooks: int = 0,
) -> PoolKey:
    return PoolKey(
        currency0=Currency.from_int(c0),
        currency1=Currency.from_int(c1),
        fee=fee,
        tick_spacing=tick_spacing,
        hooks=Address(hooks),
    )


def _make_pool_record(
    pool_key: PoolKey,
    *,
    block_number: int = 100,
    tx_hash: str | None = None,
    log_index: int = 0,
) -> Any:
    """Build a duck-typed ``PoolRecord`` for tests that need one.

    The asset-admission module only reads ``pool_id``, ``pool_key``,
    ``block_number_*_seen``, ``tx_hash_first_seen``, ``log_index_first_seen``,
    ``sqrt_price_x96``, and ``initial_tick`` from the record, so the
    duck-typed object is sufficient.
    """

    @dataclass
    class _Stub:
        pool_id: Any
        pool_key: PoolKey
        block_number_first_seen: int | None
        block_number_last_seen: int | None
        tx_hash_first_seen: str | None
        log_index_first_seen: int | None
        occurrences: int
        sqrt_price_x96: int | None
        initial_tick: int | None

    return _Stub(
        pool_id=pool_key.to_pool_id(),
        pool_key=pool_key,
        block_number_first_seen=block_number,
        block_number_last_seen=block_number,
        tx_hash_first_seen=tx_hash or ("0x" + "ab" * 32),
        log_index_first_seen=log_index,
        occurrences=1,
        sqrt_price_x96=1 << 96,
        initial_tick=0,
    )


def _make_registry_with(*records: Any) -> PoolRegistry:
    """Build a :class:`PoolRegistry` containing the supplied records.

    We add each record via the registry's ``add`` path with a stub
    :class:`DecodedInitialize`-shaped object so the registry's internal
    contract holds.
    """
    from robinhood_lp.discovery.initialize_log import DecodedInitialize

    registry = PoolRegistry(chain_id=ChainId(CHAIN_ID))
    for rec in records:
        decoded = DecodedInitialize(
            pool_id=rec.pool_id,
            pool_key=rec.pool_key,
            sqrt_price_x96=rec.sqrt_price_x96 or (1 << 96),
            initial_tick=rec.initial_tick if rec.initial_tick is not None else 0,
        )
        registry.add(
            decoded,
            block_number=rec.block_number_first_seen,
            tx_hash=rec.tx_hash_first_seen,
            log_index=rec.log_index_first_seen,
        )
    return registry


def _make_capability_report(
    *,
    passed: bool = True,
    errors: list[str] | None = None,
    bytecode_drift: bool = False,
) -> Any:
    """Build a minimal ``ChainCapabilityReport`` with sensible defaults."""
    from robinhood_lp.discovery.chain_capability import ChainCapabilityReport

    if bytecode_drift and errors is None:
        errors = ["primary: PoolManager bytecode drift: observed=dead expected=cafe"]
    if errors is None:
        errors = []
    agreement = CrossEndpointAgreement(
        chain_id_agree=True,
        pool_manager_code_hash_agree=True,
        state_view_code_hash_agree=True,
        latest_block_agree=True,
        pinned_block_hash_agree=True,
        observed_chain_ids={"alchemy-testnet": CHAIN_ID, "official-testnet": CHAIN_ID},
        observed_latest_blocks={"alchemy-testnet": 1, "official-testnet": 1},
        observed_pinned_block_hashes={"alchemy-testnet": "0xa", "official-testnet": "0xa"},
    )
    pm_info = {"bytecode_size": 24009, "code_hash": "a" * 64}
    sv_info = {"bytecode_size": 3531, "code_hash": "b" * 64}
    evidence = {
        "pool_manager": DeploymentEvidence(
            address="0x8366a39cc670b4001a1121b8f6a443a643e40951",
            earliest_block_with_code=60121168,
            earliest_block_with_code_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            deployed_at_block_zero=False,
            archive_probe_method="eth_getCode(addr, earliest)",
            reason=None,
        ),
        "state_view": DeploymentEvidence(
            address="0xf3334192d15450cdd385c8b70e03f9a6bd9e673b",
            earliest_block_with_code=60121168,
            earliest_block_with_code_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            deployed_at_block_zero=False,
            archive_probe_method="eth_getCode(addr, earliest)",
            reason=None,
        ),
    }
    return ChainCapabilityReport(
        expected_chain_id=CHAIN_ID,
        observed_chain_ids={"alchemy-testnet": CHAIN_ID, "official-testnet": CHAIN_ID},
        latest_block={"alchemy-testnet": 1, "official-testnet": 1},
        safe_block={"alchemy-testnet": 1, "official-testnet": 1},
        finalized_block={"alchemy-testnet": 1, "official-testnet": 1},
        archive_depth_blocks={"alchemy-testnet": 1, "official-testnet": 1},
        pool_manager={"alchemy-testnet": pm_info, "official-testnet": pm_info},
        state_view={"alchemy-testnet": sv_info, "official-testnet": sv_info},
        pinned_block_hash="0xa" + "b" * 63,
        pinned_block_number=1,
        pinned_at="2026-09-16T04:47:57+00:00",
        source_retrieval_time="2026-09-16T04:47:57+00:00",
        metrics={
            "requests": 0,
            "successes": 0,
            "failures": 0,
            "retries": 0,
            "failovers": 0,
            "range_splits": 0,
        },
        errors=errors,
        passed=passed,
        genesis_hash="0x" + "00" * 32,
        genesis_block_number_zero=True,
        per_endpoint_pinned_block_hashes={
            "alchemy-testnet": "0xa" + "b" * 63,
            "official-testnet": "0xa" + "b" * 63,
        },
        cross_endpoint=agreement,
        deployment_evidence=evidence,
        unsupported_block_tags={},
        archive_probe_method="eth_getCode(addr, earliest)",
        first_run=False,
    )


def _eip1167_bytecode(length: int = 51) -> bytes:
    addr_bytes = b"\xab" * 20
    prefix = bytes.fromhex("363d3d37363d3d3d3d363d3d3d363d73")
    suffix = bytes.fromhex("5af43d82803e903d91602b57fd5bf3")
    if length == 51:
        return prefix + addr_bytes + suffix
    if length == 55:
        return b"\x00" * 4 + prefix + addr_bytes + suffix
    raise ValueError(f"unsupported EIP-1167 length {length}")


def _complete_meta(addr_hex: str) -> Any:
    from robinhood_lp.discovery.token_metadata import TokenMetadataRecord

    return TokenMetadataRecord(
        address=Address.from_hex(addr_hex),
        symbol="TKN",
        name="Token",
        decimals=18,
    )


def _classify_static_fee_no_hook() -> EligibilityDecision:
    """A complete metadata, zero-hook, static-fee decision -> BACKTEST."""
    rec = _make_pool_record(_make_pool_key())
    rec.token0_metadata = _complete_meta("0x" + "11" * 20)
    rec.token1_metadata = _complete_meta("0x" + "22" * 20)
    return classify_pool(rec)


def _classify_proxy_detected() -> EligibilityDecision:
    """A non-zero hook whose bytecode matches EIP-1167 -> REJECTED."""
    rec = _make_pool_record(_make_pool_key(hooks=1 << 7))
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
    )
    return classify_pool(rec, hook_evidence=evidence)


def _classify_hook_bytecode_unavailable() -> EligibilityDecision:
    """A non-zero hook with no bytecode retrieval -> INGESTION + unavailable."""
    rec = _make_pool_record(_make_pool_key(hooks=1 << 7))
    return classify_pool(rec)


# ---------------------------------------------------------------------------
# Track A: deterministic from T022 + T023 + T024 evidence
# ---------------------------------------------------------------------------


def test_track_a_is_deterministic_across_runs() -> None:
    """Same registry + same decisions + same capability report ->
    byte-identical admission records across two runs."""
    import dataclasses

    pk = _make_pool_key()
    record = _make_pool_record(pk)
    decision = _classify_static_fee_no_hook()
    cap = _make_capability_report()
    a = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=cap,
        artifact_sha256=ARTIFACT_SHA256,
        deployment_tx_hash=DEPLOYMENT_TX_HASH,
        decode_rule_version=DECODE_RULE_VERSION,
    )
    b = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=cap,
        artifact_sha256=ARTIFACT_SHA256,
        deployment_tx_hash=DEPLOYMENT_TX_HASH,
        decode_rule_version=DECODE_RULE_VERSION,
    )
    assert a == b
    # to_dict round-trip is byte-identical (use dataclasses.asdict for slots=True)
    a_dict = dataclasses.asdict(a)
    b_dict = dataclasses.asdict(b)
    assert json.dumps(a_dict, sort_keys=True, default=str) == json.dumps(
        b_dict, sort_keys=True, default=str
    )


def test_track_a_admitted_when_evidence_consistent() -> None:
    """Static-fee plain pool with complete metadata + passed capability ->
    Track A ``admitted``."""
    pk = _make_pool_key()
    record = _make_pool_record(pk)
    decision = _classify_static_fee_no_hook()
    cap = _make_capability_report(passed=True)
    record_obj = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=cap,
    )
    assert record_obj.track_a_status == TRACK_A_ADMITTED
    assert "eligibility_level=backtest" in record_obj.track_a_reasons


def test_track_a_pending_capability_when_no_report() -> None:
    """Missing capability report -> Track A ``pending_capability``."""
    pk = _make_pool_key()
    record = _make_pool_record(pk)
    decision = _classify_static_fee_no_hook()
    record_obj = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=None,
    )
    assert record_obj.track_a_status == TRACK_A_PENDING_CAPABILITY
    assert "capability_report_missing" in record_obj.track_a_reasons


def test_track_a_rejected_by_capability_on_bytecode_drift() -> None:
    """T024 bytecode drift blocks Track A admission."""
    pk = _make_pool_key()
    record = _make_pool_record(pk)
    decision = _classify_static_fee_no_hook()
    cap = _make_capability_report(passed=True, bytecode_drift=True)
    record_obj = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=cap,
    )
    assert record_obj.track_a_status == TRACK_A_REJECTED_BY_CAPABILITY
    assert any("bytecode_drift" in r for r in record_obj.track_a_reasons)


def test_track_a_rejected_by_capability_when_report_failed() -> None:
    """A T024 report whose ``passed`` flag is False rejects the row."""
    pk = _make_pool_key()
    record = _make_pool_record(pk)
    decision = _classify_static_fee_no_hook()
    cap = _make_capability_report(passed=False, errors=["primary: chain_id mismatch"])
    record_obj = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=cap,
    )
    assert record_obj.track_a_status == TRACK_A_REJECTED_BY_CAPABILITY


def test_track_a_rejected_by_proxy_when_eip1167_detected() -> None:
    """T023 ``proxy_detected`` blocks Track A admission even when the
    capability report is consistent and the eligibility otherwise
    would admit the row."""
    pk = _make_pool_key(hooks=1 << 7)
    record = _make_pool_record(pk)
    decision = _classify_proxy_detected()
    cap = _make_capability_report(passed=True)
    record_obj = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=cap,
    )
    assert record_obj.track_a_status == TRACK_A_REJECTED_BY_PROXY
    assert "proxy_detected" in record_obj.track_a_reasons


def test_track_a_ingestion_only_when_hook_bytecode_unavailable() -> None:
    """T023 ``hook_bytecode_unavailable`` does NOT auto-promote; Track A
    admission = ``ingestion_only_pending_review``."""
    pk = _make_pool_key(hooks=1 << 7)
    record = _make_pool_record(pk)
    decision = _classify_hook_bytecode_unavailable()
    cap = _make_capability_report(passed=True)
    record_obj = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=cap,
    )
    assert record_obj.track_a_status == TRACK_A_INGESTION_ONLY_PENDING_REVIEW
    assert "hook_bytecode_unavailable" in record_obj.track_a_reasons


# ---------------------------------------------------------------------------
# Track B: pending_owner until explicit Owner decision
# ---------------------------------------------------------------------------


def test_track_b_is_pending_owner_by_default() -> None:
    """Without an Owner decision Track B is ``pending_owner`` for every
    PoolRecord; the live block is ``None``."""
    pk = _make_pool_key()
    record = _make_pool_record(pk)
    decision = _classify_static_fee_no_hook()
    record_obj = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=_make_capability_report(),
    )
    assert record_obj.track_b_status == TRACK_B_PENDING_OWNER
    assert record_obj.track_b_owner_decision_ref is None
    assert record_obj.operator_decision is None
    assert record_obj.live_block is None


def test_track_b_admitted_only_when_owner_decision_approved() -> None:
    """An APPROVED Owner decision transitions Track B to ``admitted``;
    the live block remains ``None`` (Track B does NOT directly promote
    ``live``)."""
    pk = _make_pool_key()
    record = _make_pool_record(pk)
    decision = _classify_static_fee_no_hook()
    op = OperatorDecision(
        decision_id="OWNER-DEC-0001",
        chain_id=CHAIN_ID,
        pool_id=pk.to_pool_id().to_hex(),
        outcome=OperatorDecisionOutcome.APPROVED,
        owner_decision_ref="OWNER_DECISION_REQUIRED/T025",
        decided_at="2026-09-16T05:00:00+00:00",
        evidence_pointers=["operator_decision.json"],
        reason="explicit owner approval",
    )
    record_obj = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=_make_capability_report(),
        operator_decision=op,
    )
    assert record_obj.track_b_status == TRACK_B_ADMITTED
    assert record_obj.track_b_owner_decision_ref == "OWNER_DECISION_REQUIRED/T025"
    assert record_obj.operator_decision == op
    # Track B admitted does NOT auto-promote ``live``.
    assert record_obj.live_block is None


def test_track_b_rejected_when_owner_decision_rejected() -> None:
    """A REJECTED Owner decision sets Track B to ``rejected_by_owner``."""
    pk = _make_pool_key()
    record = _make_pool_record(pk)
    decision = _classify_static_fee_no_hook()
    op = OperatorDecision(
        decision_id="OWNER-DEC-0002",
        chain_id=CHAIN_ID,
        pool_id=pk.to_pool_id().to_hex(),
        outcome=OperatorDecisionOutcome.REJECTED,
        owner_decision_ref="OWNER_DECISION_REQUIRED/T025",
        decided_at="2026-09-16T05:00:00+00:00",
        evidence_pointers=["operator_decision.json"],
        reason="owner declined",
    )
    record_obj = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=_make_capability_report(),
        operator_decision=op,
    )
    assert record_obj.track_b_status == TRACK_B_REJECTED_BY_OWNER


def test_track_b_pending_owner_when_owner_decision_revoked() -> None:
    """A REVOKED Owner decision transitions Track B back to
    ``pending_owner`` (the audit trail still records the decision)."""
    pk = _make_pool_key()
    record = _make_pool_record(pk)
    decision = _classify_static_fee_no_hook()
    op = OperatorDecision(
        decision_id="OWNER-DEC-0003",
        chain_id=CHAIN_ID,
        pool_id=pk.to_pool_id().to_hex(),
        outcome=OperatorDecisionOutcome.REVOKED,
        owner_decision_ref="OWNER_DECISION_REQUIRED/T025",
        decided_at="2026-09-16T05:00:00+00:00",
        evidence_pointers=["operator_decision.json"],
        reason="owner revoked",
    )
    record_obj = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=_make_capability_report(),
        operator_decision=op,
    )
    assert record_obj.track_b_status == TRACK_B_PENDING_OWNER
    assert record_obj.operator_decision == op


# ---------------------------------------------------------------------------
# Audit trail: evidence_pointers reference the originating artifacts
# ---------------------------------------------------------------------------


def test_evidence_pointers_include_artifact_sha_and_deployment_tx() -> None:
    """Every row's ``evidence_pointers`` reference the originating
    artifacts: artifact SHA, deployment tx hash, decode rule version."""
    pk = _make_pool_key()
    record = _make_pool_record(pk)
    decision = _classify_static_fee_no_hook()
    record_obj = build_pool_admission(
        record=record,
        decision=decision,
        chain_id=ChainId(CHAIN_ID),
        capability_report=_make_capability_report(),
        artifact_sha256=ARTIFACT_SHA256,
        deployment_tx_hash=DEPLOYMENT_TX_HASH,
        decode_rule_version=DECODE_RULE_VERSION,
    )
    joined = "\n".join(record_obj.evidence_pointers)
    assert f"artifact_sha256={ARTIFACT_SHA256}" in joined
    assert f"deployment_tx_hash={DEPLOYMENT_TX_HASH}" in joined
    assert f"decode_rule_version={DECODE_RULE_VERSION}" in joined


def test_no_pool_record_lives_in_track_b_when_no_owner_decision() -> None:
    """Across multiple pools the ``live_block`` field is always
    ``None`` until a separate G-LIVE-GATE-01 promotion. Track B does
    not auto-promote live."""
    keys = [_make_pool_key(c0=0x10 + i, c1=0x20 + i) for i in range(3)]
    records = [_make_pool_record(k) for k in keys]
    for r in records:
        # Inject complete metadata so classify_pool returns BACKTEST.
        from robinhood_lp.discovery.token_metadata import TokenMetadataRecord

        r.token0_metadata = TokenMetadataRecord(
            address=Address.from_hex("0x" + "11" * 20),
            symbol="TKN",
            name="Token",
            decimals=18,
        )
        r.token1_metadata = TokenMetadataRecord(
            address=Address.from_hex("0x" + "22" * 20),
            symbol="TKN2",
            name="Token2",
            decimals=18,
        )
    for r in records:
        d = classify_pool(r)
        admission = build_pool_admission(
            record=r,
            decision=d,
            chain_id=ChainId(CHAIN_ID),
            capability_report=_make_capability_report(),
        )
        assert admission.live_block is None
        assert admission.track_b_status == TRACK_B_PENDING_OWNER


# ---------------------------------------------------------------------------
# P02 closeout report
# ---------------------------------------------------------------------------


def _populate_metadata(record: Any) -> None:
    """Helper to give a record complete token metadata for closeout tests."""
    from robinhood_lp.discovery.token_metadata import TokenMetadataRecord

    record.token0_metadata = TokenMetadataRecord(
        address=Address.from_hex("0x" + "11" * 20),
        symbol="TKN",
        name="Token",
        decimals=18,
    )
    record.token1_metadata = TokenMetadataRecord(
        address=Address.from_hex("0x" + "22" * 20),
        symbol="TKN2",
        name="Token2",
        decimals=18,
    )


def test_closeout_report_p02_exit_gate_met_when_all_three_passes() -> None:
    """When the T022/T023/T024 inputs are consistent the closeout
    report confirms the three P02 README exit-gate conditions."""
    pk1 = _make_pool_key()
    r1 = _make_pool_record(pk1, block_number=100, tx_hash="0x" + "11" * 32, log_index=0)
    _populate_metadata(r1)
    pk2 = _make_pool_key(c0=0x30, c1=0x40)
    r2 = _make_pool_record(pk2, block_number=200, tx_hash="0x" + "22" * 32, log_index=1)
    _populate_metadata(r2)
    registry = _make_registry_with(r1, r2)
    decisions = {r1.pool_id: classify_pool(r1), r2.pool_id: classify_pool(r2)}
    cap = _make_capability_report(passed=True)
    report = build_p02_closeout_report(
        chain_id=ChainId(CHAIN_ID),
        registry=registry,
        eligibility_decisions=decisions,
        capability_report=cap,
        artifact_sha256=ARTIFACT_SHA256,
        deployment_tx_hash=DEPLOYMENT_TX_HASH,
        decode_rule_version=DECODE_RULE_VERSION,
    )
    assert isinstance(report, P02CloseoutReport)
    assert report.two_provider_agreement is True
    assert report.deployment_report_passed is True
    assert report.every_pool_has_support_reason is True
    assert report.p02_exit_gate_met is True
    assert len(report.pool_admissions) == 2


def test_closeout_report_two_provider_agreement_fails_on_chain_mismatch() -> None:
    """A T024 cross-endpoint disagreement fails the closeout's
    two-provider agreement condition."""
    pk = _make_pool_key()
    r = _make_pool_record(pk)
    _populate_metadata(r)
    registry = _make_registry_with(r)
    decision = classify_pool(r)
    cap = _make_capability_report(passed=True)
    # Mutate the agreement summary to disagree on chain_id.
    cap.cross_endpoint = CrossEndpointAgreement(
        chain_id_agree=False,
        pool_manager_code_hash_agree=True,
        state_view_code_hash_agree=True,
        latest_block_agree=True,
        pinned_block_hash_agree=True,
        observed_chain_ids={"alchemy-testnet": CHAIN_ID, "official-testnet": 1},
        observed_latest_blocks={"alchemy-testnet": 1, "official-testnet": 1},
        observed_pinned_block_hashes={"alchemy-testnet": "0xa", "official-testnet": "0xa"},
    )
    report = build_p02_closeout_report(
        chain_id=ChainId(CHAIN_ID),
        registry=registry,
        eligibility_decisions={r.pool_id: decision},
        capability_report=cap,
    )
    assert report.two_provider_agreement is False
    assert report.p02_exit_gate_met is False


def test_closeout_report_deployment_report_fails_on_drift() -> None:
    """A T024 deployment-report failure (bytecode drift) fails the
    closeout's deployment condition."""
    pk = _make_pool_key()
    r = _make_pool_record(pk)
    _populate_metadata(r)
    registry = _make_registry_with(r)
    decision = classify_pool(r)
    cap = _make_capability_report(passed=True, bytecode_drift=True)
    report = build_p02_closeout_report(
        chain_id=ChainId(CHAIN_ID),
        registry=registry,
        eligibility_decisions={r.pool_id: decision},
        capability_report=cap,
    )
    assert report.deployment_report_passed is False
    assert report.p02_exit_gate_met is False


def test_closeout_report_fails_when_pool_lacks_support_reason() -> None:
    """A registry row with no T023 decision fails
    ``every_pool_has_support_reason`` and therefore the exit gate."""
    pk = _make_pool_key()
    r = _make_pool_record(pk)
    _populate_metadata(r)
    registry = _make_registry_with(r)
    cap = _make_capability_report(passed=True)
    # No decision supplied for this pool -> every_pool_has_support_reason=False
    report = build_p02_closeout_report(
        chain_id=ChainId(CHAIN_ID),
        registry=registry,
        eligibility_decisions={},
        capability_report=cap,
    )
    assert report.every_pool_has_support_reason is False
    assert report.p02_exit_gate_met is False


def test_closeout_report_to_dict_is_deterministic_excluding_timestamp() -> None:
    """The to_dict output is byte-identical when the ``generated_at``
    field is pinned."""
    pk = _make_pool_key()
    r = _make_pool_record(pk)
    _populate_metadata(r)
    registry = _make_registry_with(r)
    decision = classify_pool(r)
    cap = _make_capability_report(passed=True)

    def _build() -> dict[str, Any]:
        return build_p02_closeout_report(
            chain_id=ChainId(CHAIN_ID),
            registry=registry,
            eligibility_decisions={r.pool_id: decision},
            capability_report=cap,
            artifact_sha256=ARTIFACT_SHA256,
            deployment_tx_hash=DEPLOYMENT_TX_HASH,
            decode_rule_version=DECODE_RULE_VERSION,
        ).to_dict()

    a = _build()
    b = _build()
    # Pin the timestamp for the deterministic comparison.
    a["generated_at"] = "2026-09-16T05:00:00+00:00"
    b["generated_at"] = "2026-09-16T05:00:00+00:00"
    assert a == b
    assert sha256_of_report(a) == sha256_of_report(b)


def test_closeout_report_from_pinned_testnet_artifact() -> None:
    """When loaded with the pinned testnet artifact (the T024 chain
    capability report committed to docs/implement/protocol-artifacts/),
    the closeout confirms the three exit-gate conditions when the
    registry contains a complete set of pools with T023 decisions."""
    import pathlib

    artifact_path = (
        pathlib.Path(__file__).parent.parent
        / "docs"
        / "implement"
        / "protocol-artifacts"
        / "chain-capability-report-robinhood-testnet.json"
    )
    artifact = json.loads(artifact_path.read_text())

    from robinhood_lp.discovery.chain_capability import ChainCapabilityReport

    cap = (
        ChainCapabilityReport.from_dict(artifact)
        if hasattr(ChainCapabilityReport, "from_dict")
        else _capability_from_dict(artifact)
    )

    pk1 = _make_pool_key()
    r1 = _make_pool_record(pk1, block_number=60121168, tx_hash="0x" + "11" * 32)
    _populate_metadata(r1)
    registry = _make_registry_with(r1)
    decision = classify_pool(r1)
    report = build_p02_closeout_report(
        chain_id=ChainId(CHAIN_ID),
        registry=registry,
        eligibility_decisions={r1.pool_id: decision},
        capability_report=cap,
        artifact_sha256=ARTIFACT_SHA256,
        deployment_tx_hash=DEPLOYMENT_TX_HASH,
        decode_rule_version=DECODE_RULE_VERSION,
    )
    # The pinned testnet report has passed=True and agreement=True so
    # the closeout should surface the three exit-gate conditions.
    assert report.two_provider_agreement is True
    assert report.deployment_report_passed is True
    assert report.every_pool_has_support_reason is True


def _capability_from_dict(d: dict[str, Any]) -> Any:
    """Convert the JSON dict into a ``ChainCapabilityReport``.

    We rebuild only the fields used by the closeout computation. The
    T024 ``from_dict`` helper is not exposed publicly; rebuilding the
    relevant subset is simpler than re-parsing the entire dataclass.
    """
    from robinhood_lp.discovery.chain_capability import ChainCapabilityReport

    obs_cids = d["observed_chain_ids"]
    agreement = CrossEndpointAgreement(
        chain_id_agree=d["cross_endpoint"]["chain_id_agree"],
        pool_manager_code_hash_agree=d["cross_endpoint"]["pool_manager_code_hash_agree"],
        state_view_code_hash_agree=d["cross_endpoint"]["state_view_code_hash_agree"],
        latest_block_agree=d["cross_endpoint"]["latest_block_agree"],
        pinned_block_hash_agree=d["cross_endpoint"]["pinned_block_hash_agree"],
        observed_chain_ids=obs_cids,
        observed_latest_blocks=d["latest_block"],
        observed_pinned_block_hashes=d["per_endpoint_pinned_block_hashes"],
    )
    return ChainCapabilityReport(
        expected_chain_id=d["expected_chain_id"],
        observed_chain_ids=obs_cids,
        latest_block=d["latest_block"],
        safe_block=d["safe_block"],
        finalized_block=d["finalized_block"],
        archive_depth_blocks=d["archive_depth_blocks"],
        pool_manager=d["pool_manager"],
        state_view=d["state_view"],
        pinned_block_hash=d["pinned_block_hash"],
        pinned_block_number=d["pinned_block_number"],
        pinned_at=d["pinned_at"],
        source_retrieval_time=d["source_retrieval_time"],
        metrics=d["metrics"],
        errors=list(d["errors"]),
        passed=bool(d["passed"]),
        genesis_hash=d.get("genesis_hash"),
        genesis_block_number_zero=bool(d.get("genesis_block_number_zero", True)),
        per_endpoint_pinned_block_hashes=d["per_endpoint_pinned_block_hashes"],
        cross_endpoint=agreement,
        deployment_evidence={
            k: DeploymentEvidence(**v) for k, v in d["deployment_evidence"].items()
        },
        unsupported_block_tags=d.get("unsupported_block_tags", {}),
        archive_probe_method=d.get("archive_probe_method", "not_probed"),
        first_run=bool(d.get("first_run", False)),
    )


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------


def test_report_to_json_round_trips() -> None:
    """``report_to_json`` produces stable JSON that can be re-parsed."""
    pk = _make_pool_key()
    r = _make_pool_record(pk)
    _populate_metadata(r)
    registry = _make_registry_with(r)
    decision = classify_pool(r)
    cap = _make_capability_report(passed=True)
    report = build_p02_closeout_report(
        chain_id=ChainId(CHAIN_ID),
        registry=registry,
        eligibility_decisions={r.pool_id: decision},
        capability_report=cap,
    )
    payload = report_to_json(report)
    reparsed = json.loads(payload)
    assert reparsed["chain_id"] == CHAIN_ID
    assert reparsed["two_provider_agreement"] is True
    assert reparsed["deployment_report_passed"] is True
    assert reparsed["every_pool_has_support_reason"] is True
    assert reparsed["p02_exit_gate_met"] is True
    assert len(reparsed["pool_admissions"]) == 1
