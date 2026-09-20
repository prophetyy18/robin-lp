"""Research dataset registry and per-dataset numeraire qualification (T100).

T100 is the unit-of-research input module the rest of P10 builds on.
A research dataset binds:

- one **dataset identity** (``dataset_id`` + ``version``) and the
  **content hash** of its declaration;
- one or more ``(chain_id, PoolKey)`` **members**, each with its
  own block range and the partitions it resolves to;
- the **ingestion and schema revisions** those partitions carry;
- one **reporting numeraire** selected from the ADR-014 hierarchy
  with a recorded valuation qualification (point-in-time discipline
  carried over from T053);
- the **immutability / versioning** rule: publishing a dataset
  creates a new version; an existing version is never edited;
  overlapping ranges produce a new version rather than a mutation.

This module is **disjoint** from execution authority. A research
dataset holds no assets, produces no transactions, and grants no
``HOLD``, ``LP`` or ``AUTO_SWAP`` approval. The active-pool
configuration is unchanged: a research dataset cannot become an
approved pool, cannot appear as the active pool, and cannot feed
the active-pool path.

The module sits in the storage layer per ADR-006 and depends only
on :mod:`robinhood_lp.protocol` and :mod:`robinhood_lp.discovery`
(for :mod:`research_universe` and :mod:`research_classification`).
It must not import :mod:`robinhood_lp.config`,
:mod:`robinhood_lp.execution`, the signer module, or
:mod:`robinhood_lp.features` (which sits in a higher tier per
ADR-006); the minimum numeraire vocabulary the dataset needs is
re-declared here so the storage module stays self-contained while
preserving the byte-for-byte string compatibility downstream
consumers expect.
"""

from __future__ import annotations

from robinhood_lp.research.dataset import (
    DATASET_SCHEMA_VERSION,
    DEFAULT_DATASET_HASH_ALGORITHM,
    RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN,
    RELATIVE_ONLY_REJECTED_REASONS,
    SUPPORT_LEVEL_BACKTEST_OR_ABOVE,
    USD_DENOMINATED_FORBIDDEN_FIELDS,
    ConfidenceLevel,
    DatasetAcceptanceVerdict,
    DatasetAcceptanceVerdictCode,
    DatasetAlreadyExistsError,
    DatasetBlockRange,
    DatasetCandidate,
    DatasetContentHasher,
    DatasetDeclaration,
    DatasetError,
    DatasetId,
    DatasetMember,
    DatasetMemberNotFoundError,
    DatasetRegistry,
    DatasetRegistryQuery,
    DatasetRegistrySnapshot,
    DatasetRevision,
    DatasetVersion,
    EmptyDatasetError,
    InactiveResearchDatasetError,
    InvalidSupportLevelError,
    InvertedBlockRangeError,
    NumeraireLevel,
    NumeraireProvenance,
    NumeraireQualification,
    PoolNotResearchMemberError,
    QualificationBundle,
    RawBlockRange,
    UnknownNumeraireRouteError,
    assert_no_usd_fields,
    build_dataset_acceptance_verdict,
    default_content_hasher,
    hash_dataset_declaration,
    validate_numeraire_route,
)

__all__ = [
    "ConfidenceLevel",
    "DATASET_SCHEMA_VERSION",
    "DEFAULT_DATASET_HASH_ALGORITHM",
    "DatasetAcceptanceVerdict",
    "DatasetAcceptanceVerdictCode",
    "DatasetAlreadyExistsError",
    "DatasetBlockRange",
    "DatasetCandidate",
    "DatasetContentHasher",
    "DatasetDeclaration",
    "DatasetError",
    "DatasetId",
    "DatasetMember",
    "DatasetMemberNotFoundError",
    "DatasetRegistry",
    "DatasetRegistryQuery",
    "DatasetRegistrySnapshot",
    "DatasetRevision",
    "DatasetVersion",
    "EmptyDatasetError",
    "InactiveResearchDatasetError",
    "InvalidSupportLevelError",
    "InvertedBlockRangeError",
    "NumeraireLevel",
    "NumeraireProvenance",
    "NumeraireQualification",
    "PoolNotResearchMemberError",
    "QualificationBundle",
    "RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN",
    "RELATIVE_ONLY_REJECTED_REASONS",
    "RawBlockRange",
    "SUPPORT_LEVEL_BACKTEST_OR_ABOVE",
    "USD_DENOMINATED_FORBIDDEN_FIELDS",
    "UnknownNumeraireRouteError",
    "assert_no_usd_fields",
    "build_dataset_acceptance_verdict",
    "default_content_hasher",
    "hash_dataset_declaration",
    "validate_numeraire_route",
]
