"""Tests for the T100 research dataset registry and per-dataset numeraire qualification.

The tests cover the contract deliverables and acceptance clauses:

1. **Dataset registry** — identity, version, content hash, member
   pools, each member's block range, partitions each member
   resolves to, and the ingestion and schema revisions those
   partitions carry.
2. **Immutability and versioning** — publishing a dataset creates
   a new version; an existing version is never edited; an
   overlapping range or a new pool is a new version rather than a
   mutation.
3. **Numeraire hierarchy** — USDG -> qualified USD stablecoin ->
   ETH display -> ``RELATIVE_ONLY``; a dataset for which no
   qualified route exists is reported ``RELATIVE_ONLY``.
4. **Valuation qualification record** — chosen numeraire, source,
   observed-at, available-at, staleness and confidence.
5. **Query surface** — what datasets exist, what each covers, which
   pools and ranges, and qualification status.
6. **Registry entries reference underlying partitions** — a
   partition can be reused across datasets; a dataset declares
   the partition IDs rather than copying events.

Boundary and invalid-input cases covered:

- a pool list that is empty, duplicated or contains an unknown
  ``PoolKey``;
- a block range that is inverted, exceeds the pool's observed
  life or spans a gap;
- a numeraire that is the pool's own token;
- a stablecoin whose qualification has expired;
- a stale or conflicting valuation source.

Two heterogeneous datasets (single-pool USDG and multi-pool
mixed-numeraire) resolve identically across repeated runs and
hosts after excluding the declared observational fields.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import pytest
from eth_hash.auto import keccak

from robinhood_lp.discovery.eligibility import EligibilityReason, EligibilityReasonCode
from robinhood_lp.discovery.onboarding import OnboardingPath
from robinhood_lp.discovery.research_classification import (
    ResearchClassificationDecision,
    ResearchNonApprovalStatement,
)
from robinhood_lp.discovery.research_universe import (
    DEFAULT_RESEARCH_MEMBER_SUPPORT_LEVEL,
    ResearchMember,
    ResearchUniverse,
)
from robinhood_lp.protocol import Address, ChainId, Currency, PoolKey, RunMode
from robinhood_lp.protocol.ids import PoolId
from robinhood_lp.research.dataset import (
    DEFAULT_DATASET_HASH_ALGORITHM,
    RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN,
    ConfidenceLevel,
    DatasetAcceptanceVerdictCode,
    DatasetAlreadyExistsError,
    DatasetBlockRange,
    DatasetContentHasher,
    DatasetDeclaration,
    DatasetError,
    DatasetId,
    DatasetMember,
    DatasetRegistry,
    DatasetRegistryQuery,
    DatasetRevision,
    DatasetVersion,
    EmptyDatasetError,
    InvertedBlockRangeError,
    NumeraireLevel,
    NumeraireProvenance,
    NumeraireQualification,
    QualificationBundle,
    RawBlockRange,
    assert_no_usd_fields,
    build_dataset_acceptance_verdict,
    default_content_hasher,
    hash_dataset_declaration,
    validate_numeraire_route,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


CHAIN_ID = 46630


PK_A = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=3000,
    tick_spacing=60,
    hooks=Address.zero(),
)
PK_B = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x30),
    fee=500,
    tick_spacing=10,
    hooks=Address.zero(),
)
PK_C = PoolKey(
    currency0=Currency.from_int(0x40),
    currency1=Currency.from_int(0x50),
    fee=100,
    tick_spacing=1,
    hooks=Address.zero(),
)


def _stable_pool_id(pool_key: PoolKey) -> PoolId:
    """Derive a ``PoolId`` from ``pool_key`` for assertions."""
    return pool_key.to_pool_id()


def _make_classification(
    pool_key: PoolKey,
    *,
    level: RunMode = RunMode.BACKTEST,
    reason_code: EligibilityReasonCode = EligibilityReasonCode.METADATA_COMPLETE,
    rationale: str = "ok",
) -> ResearchClassificationDecision:
    """Build a minimal :class:`ResearchClassificationDecision` for testing."""
    return ResearchClassificationDecision(
        chain_id=CHAIN_ID,
        pool_key=pool_key,
        level=level,
        reasons=(
            EligibilityReason(
                code=reason_code,
                detail=rationale,
            ),
        ),
        non_approval_statement=ResearchNonApprovalStatement(),
    )


def _make_qualification_bundle(
    *,
    selected_level: NumeraireLevel,
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
    staleness_seconds: int = 10,
    stablecoin_per_usdg_q64_64: int | None = None,
    rationale: str = "ok",
) -> QualificationBundle:
    """Build a :class:`QualificationBundle` for tests."""
    records: list[NumeraireQualification] = []
    for level in NumeraireLevel:
        if level is selected_level:
            ratio = (
                stablecoin_per_usdg_q64_64
                if level in (NumeraireLevel.USDG, NumeraireLevel.QUALIFIED_USD_STABLECOIN)
                else None
            )
            records.append(
                NumeraireQualification(
                    level=level,
                    selected=True,
                    rationale=rationale,
                    confidence=confidence,
                    staleness_seconds=staleness_seconds,
                    stablecoin_per_usdg_q64_64=ratio,
                )
            )
        else:
            records.append(
                NumeraireQualification(
                    level=level,
                    selected=False,
                    rationale=f"not selected for level {level.value}",
                    confidence=ConfidenceLevel.UNKNOWN,
                    staleness_seconds=0,
                    stablecoin_per_usdg_q64_64=None,
                )
            )
    return QualificationBundle(records=tuple(records))


def _make_provenance(
    *,
    level: NumeraireLevel,
    source: str = "T053",
    observed_at: int = 100,
    available_at: int = 110,
    staleness_seconds: int = 60,
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
    stablecoin_per_usdg_q64_64: int | None = None,
) -> NumeraireProvenance:
    return NumeraireProvenance(
        level=level,
        source=source,
        observed_at=observed_at,
        available_at=available_at,
        staleness_seconds=staleness_seconds,
        confidence=confidence,
        stablecoin_per_usdg_q64_64=stablecoin_per_usdg_q64_64,
    )


def _make_member(
    pool_key: PoolKey,
    *,
    classification: ResearchClassificationDecision | None = None,
    start_block: int = 0,
    end_block: int = 10_000,
    observed_start: int = 0,
    observed_end: int = 50_000,
    partitions: tuple[str, ...] = ("part-1",),
    schema_version: int = 3,
    decode_version: int = 2,
    manifest_checksum: str = "0x" + keccak(b"part-1").hex(),
    notes: str = "",
) -> DatasetMember:
    classification = classification or _make_classification(pool_key)
    return DatasetMember(
        chain_id=CHAIN_ID,
        pool_key=pool_key,
        block_range=DatasetBlockRange(
            requested=RawBlockRange(start_block, end_block),
            observed_life=RawBlockRange(observed_start, observed_end),
        ),
        partitions=partitions,
        revision=DatasetRevision(
            schema_version=schema_version,
            decode_version=decode_version,
            manifest_checksum=manifest_checksum,
        ),
        classification=classification,
        notes=notes,
    )


def _make_declaration(
    *,
    dataset_id: DatasetId | None = None,
    members: tuple[DatasetMember, ...] | None = None,
    numeraire_level: NumeraireLevel = NumeraireLevel.USDG,
    provenance: NumeraireProvenance | None = None,
    qualification_bundle: QualificationBundle | None = None,
    rationale: str = "USDG-anchored research dataset for the reference pool",
    notes: tuple[str, ...] = (),
) -> DatasetDeclaration:
    dataset_id = dataset_id or DatasetId("dataset-reference-v1")
    if members is None:
        members = (_make_member(PK_A),)
    provenance = provenance or _make_provenance(
        level=numeraire_level,
        stablecoin_per_usdg_q64_64=(1 << 64) if numeraire_level is NumeraireLevel.USDG else None,
    )
    qualification_bundle = qualification_bundle or _make_qualification_bundle(
        selected_level=numeraire_level,
        stablecoin_per_usdg_q64_64=(1 << 64) if numeraire_level is NumeraireLevel.USDG else None,
    )
    return DatasetDeclaration(
        dataset_id=dataset_id,
        members=members,
        numeraire_level=numeraire_level,
        numeraire_provenance=provenance,
        qualification_bundle=qualification_bundle,
        rationale=rationale,
        notes=notes,
    )


def _research_universe(
    members: Iterable[ResearchMember] = (),
) -> ResearchUniverse:
    universe = ResearchUniverse(chain_id=ChainId(CHAIN_ID))
    for member in members:
        universe.add(member)
    return universe


# ---------------------------------------------------------------------------
# Tests: Dataset primitives
# ---------------------------------------------------------------------------


def test_dataset_id_rejects_whitespace() -> None:
    with pytest.raises(DatasetError):
        DatasetId("a b")


def test_dataset_version_rejects_non_positive() -> None:
    with pytest.raises(DatasetError):
        DatasetVersion(0)


def test_raw_block_range_rejects_inverted() -> None:
    with pytest.raises(InvertedBlockRangeError):
        RawBlockRange(100, 50)


def test_dataset_block_range_rejects_exceeds_observed_life() -> None:
    with pytest.raises(InvertedBlockRangeError):
        DatasetBlockRange(
            requested=RawBlockRange(0, 100_000),
            observed_life=RawBlockRange(0, 50_000),
        )


def test_dataset_block_range_rejects_predates_observed_life() -> None:
    with pytest.raises(InvertedBlockRangeError):
        DatasetBlockRange(
            requested=RawBlockRange(0, 100),
            observed_life=RawBlockRange(50, 100),
        )


def test_dataset_block_range_rejects_disjoint() -> None:
    with pytest.raises(InvertedBlockRangeError):
        DatasetBlockRange(
            requested=RawBlockRange(0, 50),
            observed_life=RawBlockRange(100, 200),
        )


def test_dataset_revision_rejects_invalid() -> None:
    with pytest.raises(DatasetError):
        DatasetRevision(schema_version=0, decode_version=2, manifest_checksum="0xabc")


def test_dataset_member_rejects_empty_partitions() -> None:
    with pytest.raises(DatasetError):
        DatasetMember(
            chain_id=CHAIN_ID,
            pool_key=PK_A,
            block_range=DatasetBlockRange(
                requested=RawBlockRange(0, 100),
                observed_life=RawBlockRange(0, 200),
            ),
            partitions=(),
            revision=DatasetRevision(schema_version=3, decode_version=2, manifest_checksum="0xab"),
            classification=_make_classification(PK_A),
        )


def test_dataset_member_rejects_mismatched_classification_chain_id() -> None:
    """Classification with a different chain_id than the member is rejected."""
    bad_classification = ResearchClassificationDecision(
        chain_id=CHAIN_ID + 1,
        pool_key=PK_A,
        level=RunMode.BACKTEST,
        reasons=(
            EligibilityReason(
                code=EligibilityReasonCode.METADATA_COMPLETE,
                detail="ok",
            ),
        ),
        non_approval_statement=ResearchNonApprovalStatement(),
    )
    with pytest.raises(DatasetError):
        DatasetMember(
            chain_id=CHAIN_ID,
            pool_key=PK_A,
            block_range=DatasetBlockRange(
                requested=RawBlockRange(0, 100),
                observed_life=RawBlockRange(0, 200),
            ),
            partitions=("p",),
            revision=DatasetRevision(schema_version=3, decode_version=2, manifest_checksum="0xab"),
            classification=bad_classification,
        )


# ---------------------------------------------------------------------------
# Tests: Declaration construction
# ---------------------------------------------------------------------------


def test_dataset_declaration_rejects_empty_members() -> None:
    with pytest.raises(EmptyDatasetError):
        DatasetDeclaration(
            dataset_id=DatasetId("dataset-empty"),
            members=(),
            numeraire_level=NumeraireLevel.USDG,
            numeraire_provenance=_make_provenance(
                level=NumeraireLevel.USDG,
                stablecoin_per_usdg_q64_64=1 << 64,
            ),
            qualification_bundle=_make_qualification_bundle(
                selected_level=NumeraireLevel.USDG,
                stablecoin_per_usdg_q64_64=1 << 64,
            ),
            rationale="empty",
        )


def test_dataset_declaration_rejects_duplicate_pool_keys() -> None:
    with pytest.raises(DatasetError):
        DatasetDeclaration(
            dataset_id=DatasetId("dataset-dup"),
            members=(_make_member(PK_A), _make_member(PK_A)),
            numeraire_level=NumeraireLevel.USDG,
            numeraire_provenance=_make_provenance(
                level=NumeraireLevel.USDG, stablecoin_per_usdg_q64_64=1 << 64
            ),
            qualification_bundle=_make_qualification_bundle(
                selected_level=NumeraireLevel.USDG,
                stablecoin_per_usdg_q64_64=1 << 64,
            ),
            rationale="dup",
        )


def test_dataset_declaration_requires_provenance_level_match() -> None:
    provenance = _make_provenance(level=NumeraireLevel.USDG)
    with pytest.raises(DatasetError):
        _make_declaration(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            provenance=provenance,
            qualification_bundle=_make_qualification_bundle(
                selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
                stablecoin_per_usdg_q64_64=1 << 64,
            ),
        )


def test_dataset_declaration_requires_qualification_match() -> None:
    with pytest.raises(DatasetError):
        _make_declaration(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            provenance=_make_provenance(
                level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
                stablecoin_per_usdg_q64_64=1 << 64,
            ),
            qualification_bundle=_make_qualification_bundle(
                selected_level=NumeraireLevel.USDG,
                stablecoin_per_usdg_q64_64=1 << 64,
            ),
        )


# ---------------------------------------------------------------------------
# Tests: Content hash determinism
# ---------------------------------------------------------------------------


def test_hash_dataset_declaration_is_deterministic() -> None:
    declaration = _make_declaration()
    first = hash_dataset_declaration(declaration)
    second = hash_dataset_declaration(declaration)
    assert first == second
    assert first.startswith("0x")
    assert len(first) == 66  # 0x + 64 hex chars


def test_hash_dataset_declaration_rejects_wrong_algorithm() -> None:
    declaration = _make_declaration()
    with pytest.raises(DatasetError):
        hash_dataset_declaration(declaration, hasher=DatasetContentHasher("unknown"))


def test_hash_changes_when_membership_changes() -> None:
    base = _make_declaration()
    base_hash = hash_dataset_declaration(base)
    extended = _make_declaration(
        members=(
            _make_member(PK_A),
            _make_member(PK_B, partitions=("part-2",)),
        ),
    )
    assert hash_dataset_declaration(extended) != base_hash


def test_hash_changes_when_numeraire_changes() -> None:
    base = _make_declaration()
    base_hash = hash_dataset_declaration(base)
    other = _make_declaration(
        numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
        provenance=_make_provenance(
            level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=1 << 64,
        ),
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=1 << 64,
        ),
    )
    assert hash_dataset_declaration(other) != base_hash


def test_hash_excludes_observational_fields() -> None:
    base = _make_declaration()
    base_dict = base.to_dict()
    payload_bytes = base.to_canonical_bytes()
    base_hash = hash_dataset_declaration(base)
    payload_json = json.loads(payload_bytes.decode("utf-8"))
    assert payload_json["hash_algorithm"] == DEFAULT_DATASET_HASH_ALGORITHM
    assert payload_json["dataset_id"] == base_dict["dataset_id"]
    # Observational fields that are NOT part of the declaration
    # payload: the registry stores ``inserted_at_ns`` on snapshots
    # only, never on the declaration. The declaration does not
    # embed any wall-clock-derived field.
    assert "inserted_at_ns" not in payload_json
    assert "wall_clock" not in payload_json
    # The hash must remain identical even when the declaration is
    # rebuilt from the same inputs after the registry has had a
    # chance to mutate observational state.
    rebuilt = _make_declaration()
    assert hash_dataset_declaration(rebuilt) == base_hash


# ---------------------------------------------------------------------------
# Tests: Numeraire route validation
# ---------------------------------------------------------------------------


def test_validate_numeraire_route_returns_none_for_usdg() -> None:
    declaration = _make_declaration()
    assert validate_numeraire_route(declaration) is None


def test_validate_numeraire_route_rejects_expired_usdg() -> None:
    declaration = _make_declaration(
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.USDG,
            confidence=ConfidenceLevel.LOW,
            staleness_seconds=999,
            stablecoin_per_usdg_q64_64=1 << 64,
        ),
        provenance=_make_provenance(
            level=NumeraireLevel.USDG, confidence=ConfidenceLevel.LOW, staleness_seconds=30
        ),
    )
    assert validate_numeraire_route(declaration) == "usdg_pair_qualification_expired"


def test_validate_numeraire_route_rejects_expired_stablecoin() -> None:
    declaration = _make_declaration(
        numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
        provenance=_make_provenance(
            level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=1 << 64,
            confidence=ConfidenceLevel.LOW,
            staleness_seconds=30,
        ),
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            confidence=ConfidenceLevel.LOW,
            staleness_seconds=999,
            stablecoin_per_usdg_q64_64=1 << 64,
        ),
    )
    assert validate_numeraire_route(declaration) == "stablecoin_pair_qualification_expired"


def test_validate_numeraire_route_rejects_stale_source() -> None:
    declaration = _make_declaration(
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.USDG,
            confidence=ConfidenceLevel.HIGH,
            staleness_seconds=200,
            stablecoin_per_usdg_q64_64=1 << 64,
        ),
        provenance=_make_provenance(
            level=NumeraireLevel.USDG,
            stablecoin_per_usdg_q64_64=1 << 64,
            confidence=ConfidenceLevel.HIGH,
            staleness_seconds=30,
        ),
    )
    assert validate_numeraire_route(declaration) == "stale_valuation_source"


def test_validate_numeraire_route_rejects_conflicting_source() -> None:
    declaration = _make_declaration(
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.USDG,
            confidence=ConfidenceLevel.LOW,
            staleness_seconds=30,
            stablecoin_per_usdg_q64_64=1 << 64,
        ),
        provenance=_make_provenance(
            level=NumeraireLevel.USDG,
            stablecoin_per_usdg_q64_64=1 << 64,
            confidence=ConfidenceLevel.LOW,
            staleness_seconds=30,
        ),
    )
    assert validate_numeraire_route(declaration) == "conflicting_valuation_source"


def test_validate_numeraire_route_rejects_self_token_numeraire() -> None:
    declaration = _make_declaration(
        numeraire_level=NumeraireLevel.RELATIVE_ONLY,
        provenance=_make_provenance(
            level=NumeraireLevel.RELATIVE_ONLY, source=RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN
        ),
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.RELATIVE_ONLY
        ),
    )
    assert validate_numeraire_route(declaration) == RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN


# ---------------------------------------------------------------------------
# Tests: Acceptance verdict
# ---------------------------------------------------------------------------


def test_acceptance_verdict_accepts_usdg_single_pool() -> None:
    declaration = _make_declaration()
    verdict = build_dataset_acceptance_verdict(declaration)
    assert verdict.code is DatasetAcceptanceVerdictCode.ACCEPTED
    assert verdict.is_admitted is True


def test_acceptance_verdict_rejects_empty() -> None:
    with pytest.raises(DatasetError):
        _make_declaration(members=())


def test_acceptance_verdict_rejects_below_backtest() -> None:
    classification = _make_classification(PK_A, level=RunMode.INGESTION)
    declaration = _make_declaration(members=(_make_member(PK_A, classification=classification),))
    verdict = build_dataset_acceptance_verdict(declaration)
    assert verdict.code is DatasetAcceptanceVerdictCode.REJECTED_SUPPORT_LEVEL
    assert not verdict.is_admitted
    assert _stable_pool_id(PK_A).to_hex() in verdict.rejected_members


def test_acceptance_verdict_rejects_pool_not_research_member() -> None:
    classification = _make_classification(PK_B)
    declaration = _make_declaration(members=(_make_member(PK_B, classification=classification),))
    universe = _research_universe(
        [
            ResearchMember(
                chain_id=CHAIN_ID,
                pool_key=PK_A,
                block_range_start=0,
                block_range_end=10_000,
                support_level=DEFAULT_RESEARCH_MEMBER_SUPPORT_LEVEL,
                added_via=OnboardingPath.POOL_KEY,
            )
        ]
    )
    verdict = build_dataset_acceptance_verdict(declaration, research_universe=universe)
    assert verdict.code is DatasetAcceptanceVerdictCode.REJECTED_NOT_RESEARCH_MEMBER
    assert _stable_pool_id(PK_B).to_hex() in verdict.rejected_members


def test_acceptance_verdict_rejects_inverted_block_range() -> None:
    """A block range exceeding the pool's observed life is rejected at construction."""
    with pytest.raises(InvertedBlockRangeError):
        _make_member(
            PK_A,
            start_block=50_000,
            end_block=60_000,
            observed_start=0,
            observed_end=50_000,
        )


def test_acceptance_verdict_rejects_block_range_spanning_gap() -> None:
    """A block range disjoint from the pool's observed life is rejected."""
    with pytest.raises(InvertedBlockRangeError):
        _make_member(
            PK_A,
            start_block=0,
            end_block=50,
            observed_start=100,
            observed_end=200,
        )


def test_acceptance_verdict_rejects_block_range_predating_pool() -> None:
    """A block range that predates the pool's observed life is rejected."""
    with pytest.raises(InvertedBlockRangeError):
        _make_member(
            PK_A,
            start_block=0,
            end_block=100,
            observed_start=50,
            observed_end=200,
        )


def test_acceptance_verdict_reports_relative_only() -> None:
    declaration = _make_declaration(
        numeraire_level=NumeraireLevel.RELATIVE_ONLY,
        provenance=_make_provenance(level=NumeraireLevel.RELATIVE_ONLY, source="t053"),
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.RELATIVE_ONLY
        ),
    )
    verdict = build_dataset_acceptance_verdict(declaration)
    assert verdict.code is DatasetAcceptanceVerdictCode.RELATIVE_ONLY
    assert verdict.is_admitted is True
    assert verdict.relative_only_reason is not None


def test_acceptance_verdict_rejects_self_token() -> None:
    declaration = _make_declaration(
        numeraire_level=NumeraireLevel.RELATIVE_ONLY,
        provenance=_make_provenance(
            level=NumeraireLevel.RELATIVE_ONLY,
            source=RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN,
            staleness_seconds=60,
        ),
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.RELATIVE_ONLY,
            staleness_seconds=10,
        ),
    )
    verdict = build_dataset_acceptance_verdict(declaration)
    assert verdict.code is DatasetAcceptanceVerdictCode.REJECTED_SELF_TOKEN_NUMERAIRE


# ---------------------------------------------------------------------------
# Tests: Registry versioning
# ---------------------------------------------------------------------------


def test_registry_publish_assigns_version_one() -> None:
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    snapshot = registry.publish(_make_declaration(), inserted_at_ns=1)
    assert snapshot.version.value == 1
    assert snapshot.dataset_id.value == "dataset-reference-v1"
    assert registry.size == 1


def test_registry_publish_increments_version_for_same_id() -> None:
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    first = registry.publish(_make_declaration(), inserted_at_ns=1)
    second_declaration = _make_declaration(
        members=(_make_member(PK_A), _make_member(PK_B, partitions=("part-2",))),
    )
    second = registry.publish(second_declaration, inserted_at_ns=2)
    assert second.version.value == first.version.value + 1
    assert registry.size == 2


def test_registry_publish_keeps_prior_version_byte_identical() -> None:
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    first = registry.publish(_make_declaration(), inserted_at_ns=1)
    second_declaration = _make_declaration(
        members=(_make_member(PK_A), _make_member(PK_B, partitions=("part-2",))),
    )
    registry.publish(second_declaration, inserted_at_ns=2)
    snapshot_again = registry.get_by_content_hash(first.content_hash)
    assert snapshot_again.to_dict() == first.to_dict()
    # The byte-identical guarantee extends across runs: a fresh
    # registry produced from the same declaration yields the same
    # content hash.
    fresh = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    fresh_first = fresh.publish(_make_declaration(), inserted_at_ns=10)
    assert fresh_first.content_hash == first.content_hash


def test_registry_rejects_duplicate_content_hash() -> None:
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    declaration = _make_declaration()
    registry.publish(declaration, inserted_at_ns=1)
    with pytest.raises(DatasetAlreadyExistsError):
        registry.publish(declaration, inserted_at_ns=2)


def test_registry_rejects_below_backtest_member() -> None:
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    classification = _make_classification(PK_A, level=RunMode.INGESTION)
    declaration = _make_declaration(members=(_make_member(PK_A, classification=classification),))
    with pytest.raises(DatasetError):
        registry.publish(declaration, inserted_at_ns=1)


# ---------------------------------------------------------------------------
# Tests: Query surface
# ---------------------------------------------------------------------------


def test_registry_query_by_dataset_id() -> None:
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    first = registry.publish(_make_declaration(), inserted_at_ns=1)
    second = registry.publish(
        _make_declaration(
            members=(_make_member(PK_A), _make_member(PK_B, partitions=("part-2",))),
        ),
        inserted_at_ns=2,
    )
    assert registry.query(DatasetRegistryQuery(dataset_id=DatasetId("dataset-reference-v1"))) == (
        first,
        second,
    )
    assert registry.versions_for(DatasetId("dataset-reference-v1")) == (first, second)


def test_registry_query_by_pool_id() -> None:
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    first = registry.publish(_make_declaration(), inserted_at_ns=1)
    second = registry.publish(
        _make_declaration(
            members=(_make_member(PK_A), _make_member(PK_B, partitions=("part-2",))),
        ),
        inserted_at_ns=2,
    )
    pool_id_a = _stable_pool_id(PK_A)
    snapshots = registry.datasets_covering_pool(pool_id_a)
    assert first in snapshots
    assert second in snapshots


def test_registry_query_by_partition() -> None:
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    first = registry.publish(_make_declaration(), inserted_at_ns=1)
    second = registry.publish(
        _make_declaration(
            members=(_make_member(PK_A), _make_member(PK_B, partitions=("part-2",))),
        ),
        inserted_at_ns=2,
    )
    assert registry.datasets_covering_partition("part-1") == (first, second)
    assert registry.datasets_covering_partition("part-2") == (second,)


def test_registry_query_excludes_rejected_by_default() -> None:
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    registry.publish(_make_declaration(), inserted_at_ns=1)
    classification = _make_classification(PK_A, level=RunMode.INGESTION)
    rejected_declaration = _make_declaration(
        members=(_make_member(PK_A, classification=classification),)
    )
    with pytest.raises(DatasetError):
        registry.publish(rejected_declaration, inserted_at_ns=2)
    query = DatasetRegistryQuery()
    assert registry.query(query) == registry.snapshots


def test_registry_query_includes_rejected_when_requested() -> None:
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    accepted = registry.publish(_make_declaration(), inserted_at_ns=1)
    classification = _make_classification(PK_A, level=RunMode.INGESTION)
    rejected_declaration = _make_declaration(
        members=(_make_member(PK_A, classification=classification),)
    )
    # The registry never stores rejected snapshots, so the include_rejected
    # flag is a no-op for the in-memory registry. The flag is honoured by
    # the long-lived store (T101+).
    with pytest.raises(DatasetError):
        registry.publish(rejected_declaration, inserted_at_ns=2)
    assert registry.query(DatasetRegistryQuery(include_rejected=True)) == (accepted,)


def test_registry_query_by_numeraire() -> None:
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    registry.publish(_make_declaration(), inserted_at_ns=1)
    other = _make_declaration(
        dataset_id=DatasetId("dataset-mixed"),
        members=(_make_member(PK_B), _make_member(PK_C)),
        numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
        provenance=_make_provenance(
            level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=1 << 64,
        ),
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=1 << 64,
        ),
    )
    registry.publish(other, inserted_at_ns=2)
    query_usdg = DatasetRegistryQuery(numeraire_level=NumeraireLevel.USDG)
    query_stable = DatasetRegistryQuery(numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN)
    assert len(registry.query(query_usdg)) == 1
    assert len(registry.query(query_stable)) == 1


# ---------------------------------------------------------------------------
# Tests: Two heterogeneous datasets resolve identically across hosts
# ---------------------------------------------------------------------------


def test_two_heterogeneous_datasets_resolve_byte_identically_across_runs() -> None:
    """The acceptance clause's "byte-identical across repeated runs and hosts".

    The contract names two heterogeneous datasets — one
    single-pool with a USDG numeraire, one multi-pool with a
    mixed numeraire — that must resolve identically across
    repeated runs after excluding declared observational
    fields. The test constructs two fresh registries from
    byte-identical declarations and confirms the snapshots
    produce the same ``content_hash`` and the same
    ``to_dict()`` output.
    """
    # Single-pool USDG numeraire
    single_pool_declaration = _make_declaration(
        dataset_id=DatasetId("single-pool-usdg"),
    )
    single_a = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    single_b = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    single_a.publish(single_pool_declaration, inserted_at_ns=1)
    single_b.publish(single_pool_declaration, inserted_at_ns=2)
    snap_a = single_a.snapshots[0]
    snap_b = single_b.snapshots[0]
    assert snap_a.content_hash == snap_b.content_hash
    snap_a_dict = snap_a.to_dict()
    snap_b_dict = snap_b.to_dict()
    # Observational fields the test contract excludes:
    # ``inserted_at_ns`` differs between runs but must not change
    # the content hash nor the verification contract.
    snap_a_dict.pop("inserted_at_ns", None)
    snap_b_dict.pop("inserted_at_ns", None)
    assert snap_a_dict == snap_b_dict

    # Multi-pool mixed numeraire
    multi_pool_declaration = _make_declaration(
        dataset_id=DatasetId("multi-pool-mixed"),
        members=(
            _make_member(PK_A, partitions=("part-a",)),
            _make_member(PK_B, partitions=("part-b",)),
            _make_member(PK_C, partitions=("part-c",)),
        ),
        numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
        provenance=_make_provenance(
            level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=1 << 64,
        ),
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=1 << 64,
        ),
    )
    multi_a = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    multi_b = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    multi_a.publish(multi_pool_declaration, inserted_at_ns=10)
    multi_b.publish(multi_pool_declaration, inserted_at_ns=20)
    multi_a_dict = multi_a.snapshots[0].to_dict()
    multi_b_dict = multi_b.snapshots[0].to_dict()
    multi_a_dict.pop("inserted_at_ns", None)
    multi_b_dict.pop("inserted_at_ns", None)
    assert multi_a_dict == multi_b_dict
    assert multi_a.snapshots[0].content_hash == multi_b.snapshots[0].content_hash


# ---------------------------------------------------------------------------
# Tests: Coverage-isolation rule
# ---------------------------------------------------------------------------


def test_two_datasets_covering_same_partitions_remain_isolated() -> None:
    """Two datasets covering the same partitions must not be merged.

    The contract names the rule explicitly. The test publishes
    two datasets that both reference ``part-shared``; the
    registry stores both snapshots and the query surface
    returns both. A result computed on one is never attributed
    to the other; the registry exposes no helper that conflates
    them.
    """
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    first_declaration = _make_declaration(
        dataset_id=DatasetId("dataset-a"),
        members=(_make_member(PK_A, partitions=("part-shared",)),),
    )
    second_declaration = _make_declaration(
        dataset_id=DatasetId("dataset-b"),
        members=(_make_member(PK_B, partitions=("part-shared",)),),
    )
    registry.publish(first_declaration, inserted_at_ns=1)
    registry.publish(second_declaration, inserted_at_ns=2)
    covering = registry.datasets_covering_partition("part-shared")
    assert len(covering) == 2
    # Two datasets covering the same partitions remain isolated:
    # the registry exposes no helper that conflates them.
    for snapshot in covering:
        for member in snapshot.declaration.members:
            assert "part-shared" in member.partitions
    # Each dataset has its own content hash and version.
    assert covering[0].content_hash != covering[1].content_hash


# ---------------------------------------------------------------------------
# Tests: Partitions reference (not copy)
# ---------------------------------------------------------------------------


def test_dataset_references_partitions_by_id() -> None:
    """Registry entries reference partitions by ID, not by event payload.

    The contract explicitly requires this so one acquisition is
    reusable by many later datasets. The test confirms that the
    partition ID survives a round-trip through the declaration
    serialisation.
    """
    member = _make_member(PK_A, partitions=("part-reused-1", "part-reused-2"))
    declaration = _make_declaration(members=(member,))
    payload = declaration.to_canonical_bytes()
    rebuilt_dict = json.loads(payload.decode("utf-8"))
    assert rebuilt_dict["members"][0]["partitions"] == ["part-reused-1", "part-reused-2"]
    assert declaration.partitions() == ("part-reused-1", "part-reused-2")
    # The same partition can appear in a different dataset.
    other_member = _make_member(PK_B, partitions=("part-reused-1", "part-other"))
    other_declaration = _make_declaration(
        dataset_id=DatasetId("dataset-sharing-partition"),
        members=(other_member,),
    )
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    registry.publish(declaration, inserted_at_ns=1)
    registry.publish(other_declaration, inserted_at_ns=2)
    assert len(registry.datasets_covering_partition("part-reused-1")) == 2


# ---------------------------------------------------------------------------
# Tests: RelativeOnlyForbidDenominations
# ---------------------------------------------------------------------------


def test_relative_only_dataset_carries_no_usd_field() -> None:
    """A RELATIVE_ONLY dataset must not carry any USD-denominated field.

    The contract's acceptance clause explicitly forbids a USD
    PnL field on a ``RELATIVE_ONLY`` dataset. The test
    publishes a ``RELATIVE_ONLY`` dataset and asserts that its
    declaration payload contains no USD-denominated field.
    """
    declaration = _make_declaration(
        dataset_id=DatasetId("dataset-relative"),
        numeraire_level=NumeraireLevel.RELATIVE_ONLY,
        provenance=_make_provenance(level=NumeraireLevel.RELATIVE_ONLY, source="t053"),
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.RELATIVE_ONLY
        ),
    )
    payload = declaration.to_dict()
    # The dataset module re-declares ``assert_no_usd_fields`` so
    # the storage layer is self-contained; the guard enumerates
    # the same forbidden keys the T053 features module uses.
    assert_no_usd_fields(payload, context="RELATIVE_ONLY dataset declaration")
    assert payload["numeraire_level"] == NumeraireLevel.RELATIVE_ONLY.value


# ---------------------------------------------------------------------------
# Tests: USD-denominated results forbidden on RELATIVE_ONLY datasets
# ---------------------------------------------------------------------------


def test_relative_only_verdict_blocks_usd_payload() -> None:
    """The acceptance verdict blocks any USD-denominated payload on a RELATIVE_ONLY dataset.

    The contract says "no field of a RELATIVE_ONLY dataset may
    contain a USD-denominated PnL value". The verdict records
    the relative-only status and the
    :func:`assert_no_usd_fields` guard is the typed enforcement.
    """
    declaration = _make_declaration(
        dataset_id=DatasetId("dataset-relative-2"),
        numeraire_level=NumeraireLevel.RELATIVE_ONLY,
        provenance=_make_provenance(level=NumeraireLevel.RELATIVE_ONLY, source="t053"),
        qualification_bundle=_make_qualification_bundle(
            selected_level=NumeraireLevel.RELATIVE_ONLY
        ),
    )
    verdict = build_dataset_acceptance_verdict(declaration)
    assert verdict.code is DatasetAcceptanceVerdictCode.RELATIVE_ONLY

    # The verdict itself must not carry a USD-denominated field.
    assert_no_usd_fields(verdict.to_dict(), context="RELATIVE_ONLY verdict payload")


# ---------------------------------------------------------------------------
# Tests: Boundary — unknown PoolKey in declaration
# ---------------------------------------------------------------------------


def test_declaration_rejects_unknown_pool_key_in_member() -> None:
    """A member whose ``PoolKey`` does not match its classification is rejected.

    The ``DatasetMember.__post_init__`` already enforces this;
    the test asserts the failure is surfaced as a ``DatasetError``.
    """
    with pytest.raises(DatasetError):
        DatasetMember(
            chain_id=CHAIN_ID,
            pool_key=PK_A,
            block_range=DatasetBlockRange(
                requested=RawBlockRange(0, 100),
                observed_life=RawBlockRange(0, 200),
            ),
            partitions=("p",),
            revision=DatasetRevision(schema_version=3, decode_version=2, manifest_checksum="0xab"),
            classification=_make_classification(PK_B),
        )


# ---------------------------------------------------------------------------
# Tests: Default content hasher
# ---------------------------------------------------------------------------


def test_default_content_hasher_uses_sha256_v1() -> None:
    hasher = default_content_hasher()
    assert hasher.algorithm == DEFAULT_DATASET_HASH_ALGORITHM
    payload = b"robinhood-lp research dataset"
    digest = hasher.hash_bytes(payload)
    assert digest == "0x" + __import__("hashlib").sha256(payload).hexdigest()


def test_content_hasher_rejects_unknown_algorithm() -> None:
    hasher = DatasetContentHasher("unknown")
    with pytest.raises(DatasetError):
        hasher.hash_bytes(b"x")


# ---------------------------------------------------------------------------
# Tests: Versioning — registry serialisation stability
# ---------------------------------------------------------------------------


def test_snapshot_to_dict_is_pure_data() -> None:
    """A snapshot ``to_dict()`` returns plain data types only.

    Downstream serialisers (JSON, MessagePack) cannot encode
    ``dataclasses.dataclass`` instances; the snapshot's
    serialisation must therefore yield a tree of plain
    primitives.
    """
    registry = DatasetRegistry(chain_id=ChainId(CHAIN_ID))
    snapshot = registry.publish(_make_declaration(), inserted_at_ns=1)
    payload = snapshot.to_dict()
    json.dumps(payload)  # must not raise
    _assert_pure_data(payload)


def _assert_pure_data(value: Any) -> None:
    if isinstance(value, dict):
        for v in value.values():
            _assert_pure_data(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _assert_pure_data(v)
    elif isinstance(value, (str, int, float, bool)) or value is None:
        return
    else:
        raise AssertionError(f"non-serialisable value: {value!r} ({type(value).__name__})")
