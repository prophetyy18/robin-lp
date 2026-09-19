"""Discovery layer (T022-T026).

Re-exports the chain-capability probe (T024), the Initialize event
decoder (T022), the defensive token-metadata reader (T022), the
idempotent pool registry (T022 / T026), the Initialize scanner
(T022), the pool/hook eligibility classifier (T023), the pool-first
onboarder (T026) and the research-universe collection (T026).

This layer sits in the storage layer per ADR-006 and depends on
``robinhood_lp.protocol`` and ``robinhood_lp.rpc``. It must not
import ``robinhood_lp.config`` or higher layers.
"""

from __future__ import annotations

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
    PoolAdmissionRecord,
    build_p02_closeout_report,
    build_pool_admission,
    report_to_json,
    sha256_of_report,
)
from robinhood_lp.discovery.chain_capability import (
    BytecodeMismatchError,
    ChainCapabilityError,
    ChainCapabilityReport,
    ChainIdMismatchError,
    ExpectedDeployment,
    PerEndpointCapability,
    probe_chain_capability,
)
from robinhood_lp.discovery.eligibility import (
    DELTA_FLAG_BITS,
    EligibilityDecision,
    EligibilityReason,
    EligibilityReasonCode,
    HookEvidence,
    analyze_hook,
    classify_pool,
)
from robinhood_lp.discovery.initialize_log import (
    INITIALIZE_TOPIC0,
    DecodedInitialize,
    InitializeDecodeError,
    decode_initialize_log,
)
from robinhood_lp.discovery.onboarding import (
    AddressRejectionRecord,
    OnboarderStats,
    OnboardingError,
    OnboardingPath,
    OnboardingResult,
    PoolIdentityMismatchError,
    PoolIdentityNotFoundError,
    PoolIdentityRejectionError,
    PoolOnboarder,
    entry_path_for,
)
from robinhood_lp.discovery.registry import (
    InitializeScanner,
    PoolRecord,
    PoolRegistry,
    RegistryConflictError,
    ScannerStats,
)
from robinhood_lp.discovery.research_universe import (
    DEFAULT_RESEARCH_MEMBER_SUPPORT_LEVEL,
    DuplicateResearchMemberError,
    EmptyBlockRangeError,
    PoolMembershipView,
    PoolRole,
    ResearchMember,
    ResearchUniverse,
    ResearchUniverseError,
    UnknownResearchMemberError,
    build_membership_view,
)
from robinhood_lp.discovery.token_metadata import (
    TokenMetadataRecord,
    read_token_metadata,
)

__all__ = [
    "AddressRejectionRecord",
    "BytecodeMismatchError",
    "ChainCapabilityError",
    "ChainCapabilityReport",
    "ChainIdMismatchError",
    "DEFAULT_DECODE_RULE_VERSION",
    "DEFAULT_RESEARCH_MEMBER_SUPPORT_LEVEL",
    "DELTA_FLAG_BITS",
    "DecodedInitialize",
    "DuplicateResearchMemberError",
    "EXPECTED_DEPLOYMENT_PLACEHOLDER",
    "EligibilityDecision",
    "EligibilityReason",
    "EligibilityReasonCode",
    "EmptyBlockRangeError",
    "ExpectedDeployment",
    "HookEvidence",
    "INITIALIZE_TOPIC0",
    "InitializeDecodeError",
    "InitializeScanner",
    "OnboardingError",
    "OnboardingPath",
    "OnboardingResult",
    "OnboarderStats",
    "OperatorDecision",
    "OperatorDecisionOutcome",
    "P02CloseoutReport",
    "PerEndpointCapability",
    "PoolAdmissionRecord",
    "PoolIdentityMismatchError",
    "PoolIdentityNotFoundError",
    "PoolIdentityRejectionError",
    "PoolMembershipView",
    "PoolOnboarder",
    "PoolRecord",
    "PoolRegistry",
    "PoolRole",
    "RegistryConflictError",
    "ResearchMember",
    "ResearchUniverse",
    "ResearchUniverseError",
    "ScannerStats",
    "TRACK_A_ADMITTED",
    "TRACK_A_INGESTION_ONLY_PENDING_REVIEW",
    "TRACK_A_PENDING_CAPABILITY",
    "TRACK_A_REJECTED_BY_CAPABILITY",
    "TRACK_A_REJECTED_BY_PROXY",
    "TRACK_B_ADMITTED",
    "TRACK_B_PENDING_OWNER",
    "TRACK_B_REJECTED_BY_OWNER",
    "TokenMetadataRecord",
    "UnknownResearchMemberError",
    "analyze_hook",
    "build_membership_view",
    "build_p02_closeout_report",
    "build_pool_admission",
    "classify_pool",
    "decode_initialize_log",
    "entry_path_for",
    "probe_chain_capability",
    "read_token_metadata",
    "report_to_json",
    "sha256_of_report",
]


#: Convenience alias for the well-known Robinhood Chain mainnet
#: V4 deployment fields. The bytecode hashes are intentionally
#: ``None`` here; the T024 capability probe must populate them from a
#: real endpoint before T022 will accept the deployment as verified.
EXPECTED_DEPLOYMENT_PLACEHOLDER = ExpectedDeployment
