"""Tests for the reorg handler end-to-end behaviour (T033).

T033 acceptance requires:

- shallow reorgs converge through common-ancestor search and orphan
  marking / replacement, with the original raw evidence preserved;
- deep reorgs halt qualification, journal a critical-incident
  candidate, and block new backtest / paper / live decisions;
- provider disagreement halts qualification with
  ``finalized_ancestry_disagreement``;
- a removed-log fixture demotes the log via orphan marking and
  preserves the original raw evidence;
- an orphan-reappearance fixture reconciles against the reorg
  journal without rewriting the prior raw evidence and records the
  resolution as a new append-only evidence entry;
- finalized data is never rewritten, including during a critical
  incident;
- ``confirmations = 12`` is only an
  ``EXPERIMENTAL_NOT_LIVE_APPROVED`` placeholder;
- the finality policy is explicitly injected — no test depends on an
  implicit production default.

These tests build the inputs by hand and exercise the handler
directly; the deeper RPC / adapter wiring is out of scope here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _reorg_t033_fixtures import CHAIN_ID, QUALIFIED_ALIASES, make_finality_policy
from _storage_t031_fixtures import CHAIN, CONTRACT, make_swap_record
from robinhood_lp.storage.manifest import ManifestStore
from robinhood_lp.storage.reorg.ancestor import BlockHeader
from robinhood_lp.storage.reorg.handler import (
    DECISION_BLOCK_BACKTEST,
    DECISION_BLOCK_LIVE,
    DECISION_BLOCK_PAPER,
    QualifiedBoundaryProbe,
    ReorgHandler,
)
from robinhood_lp.storage.reorg.journal import (
    CriticalIncidentRecorder,
    ReorgJournal,
)
from robinhood_lp.storage.reorg.policy import (
    EXPERIMENTAL_CONFIRMATIONS_PLACEHOLDER,
    EXPERIMENTAL_NOT_LIVE_APPROVED,
    FINALITY_TAG_UNAVAILABLE,
    FINALIZED_ANCESTRY_DISAGREEMENT,
    FINALIZED_ANCESTRY_VIOLATION,
    FINALIZED_UNAVAILABLE,
    REASON_DEEP_REORG,
    REASON_ORPHAN_REAPPEARANCE,
    REASON_REMOVED_LOG,
    REASON_SHALLOW_REORG,
    FinalityPolicy,
    FinalityPolicyError,
)
from robinhood_lp.storage.writer import RawPartitionWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handler(
    tmp_path: Path,
    *,
    deep_reorg_threshold_blocks: int = 5,
    unfinalized_window_lag_blocks: int = 16,
    qualified_endpoint_aliases: tuple[str, ...] = QUALIFIED_ALIASES,
    deep_reorg_halt_qualifies_backtest: bool = True,
) -> tuple[ReorgHandler, ManifestStore, ReorgJournal, CriticalIncidentRecorder]:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    journal = ReorgJournal(manifest)
    recorder = CriticalIncidentRecorder(journal)
    policy = FinalityPolicy(
        finality_tag="finalized",
        qualified_endpoint_aliases=qualified_endpoint_aliases,
        finalized_tag_request_timeout_seconds=5.0,
        finalized_tag_retry_limit=2,
        deep_reorg_threshold_blocks=deep_reorg_threshold_blocks,
        unfinalized_window_lag_blocks=unfinalized_window_lag_blocks,
        deep_reorg_halt_qualifies_backtest=deep_reorg_halt_qualifies_backtest,
        policy_id="test-policy",
    )
    handler = ReorgHandler(policy=policy, journal=journal)
    return handler, manifest, journal, recorder


def _header(block_number: int, byte_value: int, parent_byte: int | None = None) -> BlockHeader:
    """Build a BlockHeader whose hash encodes ``byte_value``."""
    if parent_byte is None and block_number > 0:
        parent_byte = byte_value - 1
    parent_hash = (
        "0x" + bytes([parent_byte]).hex().rjust(64, "0") if parent_byte is not None else None
    )
    return BlockHeader(
        block_number=block_number,
        block_hash="0x" + bytes([byte_value]).hex().rjust(64, "0"),
        parent_hash=parent_hash or "0x" + "00" * 32,
    )


def _seed_canonical(
    handler: ReorgHandler,
    *,
    hashes: dict[int, int],
) -> None:
    """Populate the canonical view with the given block hashes.

    The handler's canonical view is empty when freshly constructed;
    we absorb the chain via ``observe_new_tip`` by starting from an
    empty canonical view, which appends the observed headers without
    producing a reorg.
    """
    # Build observed headers and feed them through the handler with
    # an empty canonical view. The handler takes the no-reorg path.
    headers = [_header(n, h) for n, h in sorted(hashes.items())]
    handler.observe_new_tip(
        chain_id=CHAIN_ID,
        observed_headers=headers,
        finalized_block_number=sorted(hashes)[-1],
        finalized_block_hash="0x" + bytes([sorted(hashes.values())[-1]]).hex().rjust(64, "0"),
    )


# ---------------------------------------------------------------------------
# Explicit-injection guard
# ---------------------------------------------------------------------------


def test_handler_requires_explicit_policy(tmp_path: Path) -> None:
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    journal = ReorgJournal(manifest)
    try:
        with pytest.raises(FinalityPolicyError, match="policy must be FinalityPolicy"):
            ReorgHandler(policy="not a policy", journal=journal)  # type: ignore[arg-type]
    finally:
        manifest.close()


def test_handler_requires_explicit_journal(tmp_path: Path) -> None:
    policy = make_finality_policy()
    with pytest.raises(FinalityPolicyError, match="journal must be ReorgJournal"):
        ReorgHandler(policy=policy, journal="not a journal")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Shallow reorg
# ---------------------------------------------------------------------------


def test_shallow_reorg_records_orphan_entries(tmp_path: Path) -> None:
    handler, manifest, journal, _recorder = _make_handler(tmp_path)
    try:
        # Canonical chain: 0..5 with bytes 0x10..0x15.
        canonical_hashes = {n: 0x10 + n for n in range(6)}
        _seed_canonical(handler, hashes=canonical_hashes)

        # Fork at block 4 (canonical[4]=0x14, observed[4]=0xAA).
        observed_hashes = dict(canonical_hashes)
        observed_hashes[4] = 0xAA
        observed_hashes[5] = 0xAB
        observed_headers = [_header(n, h) for n, h in sorted(observed_hashes.items())]

        decision = handler.observe_new_tip(
            chain_id=CHAIN_ID,
            observed_headers=observed_headers,
            finalized_block_number=3,
            finalized_block_hash="0x" + "13" * 32,
        )

        assert decision.kind == "continue"
        assert decision.reason_code == REASON_SHALLOW_REORG
        assert decision.common_ancestor_block_number == 3
        assert decision.orphan_depth == 2
        # Two orphan entries recorded for canonical blocks 4 and 5.
        assert len(decision.orphan_journal_entries) == 2
        recorded_blocks = sorted(e["block_number"] for e in decision.orphan_journal_entries)
        assert recorded_blocks == [4, 5]

        # Journal contains the orphan markers; the prior partition
        # row was never written because we did not write any
        # partitions in this test, but the journal is non-empty.
        reorgs = journal.list_reorgs()
        assert {r.block_number for r in reorgs} == {4, 5}
        assert {r.demotion_reason for r in reorgs} == {REASON_SHALLOW_REORG}
    finally:
        manifest.close()


def test_shallow_reorg_preserves_partition_row(tmp_path: Path) -> None:
    """Shallow-reorg demotion never rewrites the partition row."""
    handler, manifest, _journal, _recorder = _make_handler(tmp_path)
    try:
        # Write a partition first (T031 path) for blocks 0..5.
        writer = RawPartitionWriter(tmp_path, manifest)
        record = make_swap_record(block_number=2, log_index=0)
        result = writer.append_partition([record], chain_id=CHAIN, contract_address=CONTRACT)
        partition_id = result.partition_id
        before = manifest.get_partition(partition_id)
        assert before is not None
        before_sha = before.file_sha256

        # Canonical chain: 0..5 with bytes 0x10..0x15.
        _seed_canonical(
            handler,
            hashes={n: 0x10 + n for n in range(6)},
        )

        # Fork at block 4.
        observed_hashes = {n: 0x10 + n for n in range(6)}
        observed_hashes[4] = 0xAA
        observed_headers = [_header(n, h) for n, h in sorted(observed_hashes.items())]
        handler.observe_new_tip(
            chain_id=CHAIN_ID,
            observed_headers=observed_headers,
            finalized_block_number=3,
            finalized_block_hash="0x" + "13" * 32,
        )

        after = manifest.get_partition(partition_id)
        assert after is not None
        assert after.file_sha256 == before_sha
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Deep reorg halt
# ---------------------------------------------------------------------------


def test_deep_reorg_halts_qualification_and_blocks_backtest(tmp_path: Path) -> None:
    handler, manifest, _journal, _recorder = _make_handler(tmp_path, deep_reorg_threshold_blocks=3)
    try:
        canonical_hashes = {n: 0x10 + n for n in range(6)}
        _seed_canonical(handler, hashes=canonical_hashes)

        # Fork at block 0 — every block diverges, depth=6 > threshold=3.
        observed_hashes = {n: 0x10 + n for n in range(6)}
        for n in range(6):
            observed_hashes[n] = 0xA0 + n
        observed_headers = [_header(n, h) for n, h in sorted(observed_hashes.items())]

        decision = handler.observe_new_tip(
            chain_id=CHAIN_ID,
            observed_headers=observed_headers,
            finalized_block_number=5,
            finalized_block_hash="0x" + "15" * 32,
        )

        assert decision.kind == "halt_deep_reorg"
        assert decision.reason_code == REASON_DEEP_REORG
        assert DECISION_BLOCK_BACKTEST in decision.gates
        assert DECISION_BLOCK_PAPER in decision.gates
        assert DECISION_BLOCK_LIVE in decision.gates
    finally:
        manifest.close()


def test_deep_reorg_halts_when_orphan_depth_meets_threshold(tmp_path: Path) -> None:
    """orphan_depth >= deep_reorg_threshold_blocks halts (inclusive)."""
    handler, manifest, _journal, _recorder = _make_handler(tmp_path, deep_reorg_threshold_blocks=2)
    try:
        canonical_hashes = {n: 0x10 + n for n in range(4)}
        _seed_canonical(handler, hashes=canonical_hashes)

        # Fork at block 2; canonical tip is 3, common ancestor 2, depth 2 == threshold.
        observed_hashes = dict(canonical_hashes)
        observed_hashes[2] = 0xBB
        observed_hashes[3] = 0xCC
        observed_headers = [_header(n, h) for n, h in sorted(observed_hashes.items())]
        decision = handler.observe_new_tip(
            chain_id=CHAIN_ID,
            observed_headers=observed_headers,
            finalized_block_number=1,
            finalized_block_hash="0x" + "11" * 32,
        )
        assert decision.kind == "halt_deep_reorg"
    finally:
        manifest.close()


def test_deep_reorg_preserves_partition_evidence(tmp_path: Path) -> None:
    """A deep-reorg halt never overwrites the partition file row."""
    handler, manifest, _journal, _recorder = _make_handler(tmp_path, deep_reorg_threshold_blocks=2)
    try:
        # Pre-existing partition row.
        writer = RawPartitionWriter(tmp_path, manifest)
        record = make_swap_record(block_number=2, log_index=0)
        result = writer.append_partition([record], chain_id=CHAIN, contract_address=CONTRACT)
        partition_id = result.partition_id
        before = manifest.get_partition(partition_id)
        assert before is not None
        before_sha = before.file_sha256

        canonical_hashes = {n: 0x10 + n for n in range(4)}
        _seed_canonical(handler, hashes=canonical_hashes)

        observed_hashes = dict(canonical_hashes)
        observed_hashes[2] = 0xBB
        observed_hashes[3] = 0xCC
        observed_headers = [_header(n, h) for n, h in sorted(observed_hashes.items())]
        handler.observe_new_tip(
            chain_id=CHAIN_ID,
            observed_headers=observed_headers,
            finalized_block_number=1,
            finalized_block_hash="0x" + "11" * 32,
            partition_id=partition_id,
        )

        after = manifest.get_partition(partition_id)
        assert after is not None
        assert after.file_sha256 == before_sha
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Provider disagreement
# ---------------------------------------------------------------------------


def test_provider_disagreement_halts_qualification(tmp_path: Path) -> None:
    handler, manifest, _journal, _recorder = _make_handler(tmp_path)
    try:
        probes = [
            QualifiedBoundaryProbe(
                endpoint_alias="robinhood_public",
                block_number=100,
                block_hash="0x" + "aa" * 32,
                parent_hash="0x" + "bb" * 32,
            ),
            QualifiedBoundaryProbe(
                endpoint_alias="alchemy_free",
                block_number=100,
                block_hash="0x" + "cc" * 32,
                parent_hash="0x" + "bb" * 32,
            ),
        ]
        decision = handler.evaluate_boundary(probes)
        assert decision.kind == "disagreement"
        assert decision.reason_code == FINALIZED_ANCESTRY_DISAGREEMENT

        verdict = handler.apply_finality_decision(decision)
        assert verdict.kind == "halt_provider_disagreement"
        assert verdict.reason_code == FINALIZED_ANCESTRY_DISAGREEMENT
        assert DECISION_BLOCK_PAPER in verdict.gates
        assert DECISION_BLOCK_LIVE in verdict.gates
    finally:
        manifest.close()


def test_finalized_unavailable_halts_qualification(tmp_path: Path) -> None:
    handler, manifest, _journal, _recorder = _make_handler(tmp_path)
    try:
        probes = [
            QualifiedBoundaryProbe(
                endpoint_alias="robinhood_public",
                block_number=100,
                block_hash="0x" + "aa" * 32,
                parent_hash="0x" + "bb" * 32,
            ),
            QualifiedBoundaryProbe(
                endpoint_alias="alchemy_free",
                block_number=None,
                block_hash=None,
                parent_hash=None,
                error="transport timeout",
            ),
        ]
        decision = handler.evaluate_boundary(probes)
        assert decision.kind == "unavailable"
        assert decision.reason_code == FINALIZED_UNAVAILABLE

        verdict = handler.apply_finality_decision(decision)
        assert verdict.kind == "halt_finalized_unavailable"
        assert verdict.reason_code == FINALIZED_UNAVAILABLE
    finally:
        manifest.close()


def test_finality_tag_unavailable_halts_qualification(tmp_path: Path) -> None:
    handler, manifest, _journal, _recorder = _make_handler(tmp_path)
    try:
        probes = [
            QualifiedBoundaryProbe(
                endpoint_alias="robinhood_public",
                block_number=100,
                block_hash="0x" + "aa" * 32,
                parent_hash="0x" + "bb" * 32,
            ),
            QualifiedBoundaryProbe(
                endpoint_alias="alchemy_free",
                block_number=None,
                block_hash=None,
                parent_hash=None,
                tag_supported=False,
            ),
        ]
        decision = handler.evaluate_boundary(probes)
        assert decision.kind == "tag_unavailable"
        assert decision.reason_code == FINALITY_TAG_UNAVAILABLE
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# FINALIZED_ANCESTRY_VIOLATION critical incident
# ---------------------------------------------------------------------------


def test_finalized_ancestry_violation_records_critical_incident(tmp_path: Path) -> None:
    handler, manifest, _journal, recorder = _make_handler(tmp_path)
    try:
        decision = handler.detect_finalized_ancestry_violation(
            chain_id=CHAIN_ID,
            finalized_block_number=42,
            finalized_block_hash="0x" + "aa" * 32,
            endpoint_alias="robinhood_public",
            detail={"evidence": "previously_finalized_block_lost"},
        )
        assert decision.kind == "halt_critical_incident"
        assert decision.reason_code == FINALIZED_ANCESTRY_VIOLATION
        assert DECISION_BLOCK_BACKTEST in decision.gates
        assert DECISION_BLOCK_PAPER in decision.gates
        assert DECISION_BLOCK_LIVE in decision.gates
        assert decision.critical_incident_id is not None

        # The recorder appended the candidate.
        incidents = recorder.list()
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.incident_kind == FINALIZED_ANCESTRY_VIOLATION
        assert incident.endpoint_alias == "robinhood_public"
        assert incident.block_number == 42
        assert json.loads(incident.detail_json)["evidence"] == "previously_finalized_block_lost"
    finally:
        manifest.close()


def test_finalized_ancestry_violation_does_not_rewrite_partition_evidence(
    tmp_path: Path,
) -> None:
    """A critical-incident candidate must not rewrite finalized raw evidence."""
    handler, manifest, _journal, _recorder = _make_handler(tmp_path)
    try:
        writer = RawPartitionWriter(tmp_path, manifest)
        record = make_swap_record(block_number=42, log_index=0)
        result = writer.append_partition([record], chain_id=CHAIN, contract_address=CONTRACT)
        partition_id = result.partition_id
        before = manifest.get_partition(partition_id)
        assert before is not None
        before_sha = before.file_sha256

        handler.detect_finalized_ancestry_violation(
            chain_id=CHAIN_ID,
            finalized_block_number=42,
            finalized_block_hash="0x" + "aa" * 32,
            endpoint_alias="robinhood_public",
            detail={"evidence": "previously_finalized_block_lost"},
        )

        after = manifest.get_partition(partition_id)
        assert after is not None
        assert after.file_sha256 == before_sha
    finally:
        manifest.close()


def test_finalized_ancestry_violation_blocks_backtest_paper_and_live(
    tmp_path: Path,
) -> None:
    """Finalized-ancestry violation gates all three decision streams."""
    handler, manifest, _journal, _recorder = _make_handler(
        tmp_path, deep_reorg_halt_qualifies_backtest=False
    )
    try:
        decision = handler.detect_finalized_ancestry_violation(
            chain_id=CHAIN_ID,
            finalized_block_number=10,
            finalized_block_hash="0x" + "aa" * 32,
            endpoint_alias="robinhood_public",
            detail={"evidence": "endpoint_disagreement"},
        )
        # Critical incidents gate all three streams regardless of
        # the policy's deep_reorg_halt_qualifies_backtest flag.
        assert DECISION_BLOCK_BACKTEST in decision.gates
        assert DECISION_BLOCK_PAPER in decision.gates
        assert DECISION_BLOCK_LIVE in decision.gates
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Removed log and orphan reappearance
# ---------------------------------------------------------------------------


def test_removed_log_records_orphan_with_reason(tmp_path: Path) -> None:
    handler, manifest, _journal, _recorder = _make_handler(tmp_path)
    try:
        decision = handler.record_removed_log(
            chain_id=CHAIN_ID,
            block_number=7,
            orphan_block_hash="0x" + "11" * 32,
            partition_id=None,
        )
        assert decision.kind == "reconcile_removed_log"
        assert decision.reason_code == REASON_REMOVED_LOG
        assert decision.orphan_journal_entries[0]["block_number"] == 7
    finally:
        manifest.close()


def test_orphan_reappearance_records_new_evidence(tmp_path: Path) -> None:
    handler, manifest, _journal, _recorder = _make_handler(tmp_path)
    try:
        decision = handler.record_orphan_reappearance(
            chain_id=CHAIN_ID,
            block_number=8,
            orphan_block_hash="0x" + "11" * 32,
            replacement_block_hash="0x" + "22" * 32,
            partition_id=None,
        )
        assert decision.kind == "reconcile_orphan_reappearance"
        assert decision.reason_code == REASON_ORPHAN_REAPPEARANCE
        assert decision.orphan_journal_entries[0]["block_number"] == 8
    finally:
        manifest.close()


def test_orphan_reappearance_does_not_overwrite_prior_entry(tmp_path: Path) -> None:
    """A reappearance records a *new* journal row; the prior entry stays."""
    handler, manifest, journal, _recorder = _make_handler(tmp_path)
    try:
        first = handler.record_orphan_reappearance(
            chain_id=CHAIN_ID,
            block_number=9,
            orphan_block_hash="0x" + "11" * 32,
            replacement_block_hash="0x" + "22" * 32,
            partition_id=None,
        )
        second = handler.record_orphan_reappearance(
            chain_id=CHAIN_ID,
            block_number=9,
            orphan_block_hash="0x" + "33" * 32,
            replacement_block_hash="0x" + "44" * 32,
            partition_id=None,
        )
        # The two decisions carry distinct journal entry ids.
        assert first.orphan_journal_entries[0]["id"] != second.orphan_journal_entries[0]["id"]
        listed = journal.list_reorgs()
        assert len(listed) == 2
        # First entry preserved verbatim.
        assert listed[0].orphan_block_hash == "0x" + "11" * 32
        assert listed[1].orphan_block_hash == "0x" + "33" * 32
    finally:
        manifest.close()


# ---------------------------------------------------------------------------
# Unfinalized hash window
# ---------------------------------------------------------------------------


def test_unfinalized_window_lower_bound_is_finalized_plus_one(tmp_path: Path) -> None:
    handler, _manifest, _journal, _recorder = _make_handler(
        tmp_path, unfinalized_window_lag_blocks=16
    )
    window = handler.unfinalized_window(finalized_block_number=10, latest_block_number=12)
    assert window.lower_bound == 11
    assert window.upper_bound == 12


def test_unfinalized_window_clamps_to_policy_lag(tmp_path: Path) -> None:
    handler, _manifest, _journal, _recorder = _make_handler(
        tmp_path, unfinalized_window_lag_blocks=4
    )
    window = handler.unfinalized_window(finalized_block_number=10, latest_block_number=1000)
    assert window.upper_bound == 14  # 10 + 4


def test_unfinalized_window_rejects_invalid_inputs(tmp_path: Path) -> None:
    handler, _manifest, _journal, _recorder = _make_handler(tmp_path)
    with pytest.raises(FinalityPolicyError, match="latest_block_number"):
        handler.unfinalized_window(finalized_block_number=10, latest_block_number=5)


# ---------------------------------------------------------------------------
# Experimental placeholder for confirmations = 12
# ---------------------------------------------------------------------------


def test_experimental_confirmations_placeholder_is_constant() -> None:
    """``confirmations = 12`` is the literal placeholder; mainnet
    qualification, paper, and live promotion must never depend on it.
    The handler has no method that consumes a confirmations count —
    only the finalized tag is honoured.
    """
    assert EXPERIMENTAL_CONFIRMATIONS_PLACEHOLDER == 12
    assert EXPERIMENTAL_NOT_LIVE_APPROVED == "EXPERIMENTAL_NOT_LIVE_APPROVED"
    # The handler's policy object does not expose a ``confirmations``
    # field — the placeholder exists solely as a documented constant.
    handler, manifest, _journal, _recorder = _make_handler(_tmp())
    try:
        assert not hasattr(handler.policy, "confirmations")
    finally:
        manifest.close()


def _tmp() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="t033-experimental-"))


# ---------------------------------------------------------------------------
# No implicit production default
# ---------------------------------------------------------------------------


def test_policy_must_be_explicitly_injected() -> None:
    """The handler cannot be constructed without an explicit policy."""
    with pytest.raises(TypeError):
        ReorgHandler()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Continued-after-decision smoke test
# ---------------------------------------------------------------------------


def test_no_reorg_observation_continues_silently(tmp_path: Path) -> None:
    handler, manifest, _journal, _recorder = _make_handler(tmp_path)
    try:
        canonical_hashes = {n: 0x10 + n for n in range(3)}
        _seed_canonical(handler, hashes=canonical_hashes)

        # Identical observed chain: no reorg.
        observed_headers = [_header(n, h) for n, h in sorted(canonical_hashes.items())]
        decision = handler.observe_new_tip(
            chain_id=CHAIN_ID,
            observed_headers=observed_headers,
            finalized_block_number=2,
            finalized_block_hash="0x" + "12" * 32,
        )
        assert decision.kind == "continue"
        assert decision.reason_code is None
        assert decision.orphan_journal_entries == ()
    finally:
        manifest.close()
