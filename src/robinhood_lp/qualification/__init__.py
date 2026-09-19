"""Reference-dataset and two-pool qualification (T036 + T038).

The ``robinhood_lp.qualification`` package owns the qualification
pipelines that decide whether a real-mainnet ingestion run may
carry ``complete=true``. It builds on top of the T034 data-quality
and completeness reports (``robinhood_lp.quality``) by wiring
together:

- the **pinned reference target** (chain, PoolKey, PoolId, inclusive
  range, and the 2026-09-17 baseline counts measured against the
  primary public endpoint);
- the **PoolId re-derivation check** that verifies the pinned PoolId
  equals ``keccak256(abi.encode(PoolKey))``;
- the **baseline comparison** that surfaces any per-event-type count
  or distinct event-block count that differs from the 2026-09-17
  baseline as a discrepancy rather than reconciling it;
- the **independent fidelity check** that re-acquires sampled windows
  through the second endpoint within that endpoint's measured
  per-call capability and compares per-``EventKey`` normalized
  content hashes, retaining both raw acquisition envelopes;
- the **block-pinned state spot check** that issues
  ``StateView.getSlot0`` and ``StateView.getLiquidity`` at the range
  end to an endpoint that can serve historical state at that depth
  and records the golden values, with a ``latest`` substitution
  refused;
- the **failure-path evidence** that demonstrates each documented
  failure mode (HTTP 429, ``-32000 logs matched by query exceeds
  limit of 10000``, ``-32000 log query timed out``, provider
  failover, budget exhaustion) produces its named reason code and
  never a false ``complete=true``;
- the **operator runbook** that states the endpoint routing
  explicitly: the primary endpoint carries the wide pool-filtered
  scan; the secondary endpoint participates only within its
  measured per-call capability for sampled cross-validation and the
  block-pinned StateView read;
- the **two-pool ten-million-block window pipeline** (T038) that
  pins the window end on a finalized block both qualified endpoints
  agree on, applies the per-pool window extension rule, resolves the
  Owner-pinned second pool's ``PoolKey`` + ``Initialize`` block
  with the offline keccak256 re-derivation check, and assembles the
  per-pool T034 machine reports over the same pinned window.

Downstream consumers (Phase 4 / Phase 5) refuse to load a dataset
whose qualification report is missing, non-passing, or synthetic-only.
"""

from __future__ import annotations

from robinhood_lp.qualification.baseline_check import (
    BaselineComparison,
    compare_against_baseline,
)
from robinhood_lp.qualification.failure_paths import (
    FAILURE_PATH_KIND_BUDGET_EXHAUSTED,
    FAILURE_PATH_KIND_FAILOVER,
    FAILURE_PATH_KIND_HTTP_429,
    FAILURE_PATH_KIND_LOGS_LIMIT_REJECTION,
    FAILURE_PATH_KIND_RPC_TIMEOUT,
    FailurePathEvidence,
    build_failure_path_evidence,
)
from robinhood_lp.qualification.fidelity import (
    FidelityCheckResult,
    FidelityWindowSample,
    perform_fidelity_check,
)
from robinhood_lp.qualification.hook_pack import (
    DEFAULT_ADVERSARIAL_TESTS,
    DELTA_PAIR_TABLE,
    ENABLED_CALLBACK_NAMES,
    HOOK_FLAG_NAMES,
    HOOK_PACK_SPEC_REVISION,
    REQUIRED_HOOK_PACK_FIELDS,
    EmptySetHookEvidence,
    HookDeltaPath,
    HookInvalidationRule,
    HookPack,
    HookPackReport,
    HookPoolClassificationOutcome,
    HookReplayModel,
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
from robinhood_lp.qualification.per_pool_coverage import (
    PerPoolCostRecord,
    PerPoolCoverageReport,
    PerPoolDataRoot,
    PerPoolReconciliation,
    build_per_pool_coverage_report,
    reconcile_per_pool_reports_against_partitions,
)
from robinhood_lp.qualification.per_pool_window import (
    DEFAULT_ELAPSED_MS_BUDGET,
    DEFAULT_HTTP_REQUEST_BUDGET,
    DEFAULT_LOGICAL_CALL_BUDGET,
    DEFAULT_PROVIDER_UNIT_BUDGET,
    DEFAULT_RESPONSE_BYTES_BUDGET,
    DOCUMENTED_PER_POOL_REASON_CODES,
    OUTCOME_POOL_ACQUIRED,
    OUTCOME_POOL_ACQUIRED_AFTER_RESUME,
    OUTCOME_POOL_INITIALIZED_AFTER_END,
    OUTCOME_POOL_RESOLUTION_FAILED,
    PER_POOL_CHAIN_ID,
    REASON_BUDGET_EXHAUSTED,
    REASON_ENDPOINT_HISTORICAL_STATE_UNAVAILABLE,
    REASON_FINALIZED_DISAGREEMENT,
    REASON_FINALIZED_UNAVAILABLE,
    REASON_HTTP_429_RATE_LIMIT,
    REASON_LATEST_REJECTED,
    REASON_NON_FINALIZED_BLOCK_REJECTED,
    REASON_OK,
    REASON_POOL_INITIALIZE_NOT_LOCATED,
    REASON_POOL_INITIALIZED_AFTER_END,
    REASON_PROVIDER_RETURNED_NON_FINALIZED,
    REASON_RANGE_ALREADY_COVERED,
    REASON_RANGE_WIDER_THAN_CAPABILITY,
    REASON_WALL_CLOCK_REJECTED,
    BudgetCeiling,
    LogicalCallCountInvariantError,
    PerPoolWindowInputs,
    PerPoolWindowPlan,
    PlannedSubRange,
    PriorCoverage,
    assert_logical_call_count_equals_sub_ranges_plus_headers,
    parse_finalized_pin_payloads,
    per_pool_window_summary,
    plan_per_pool_window,
    plan_per_pool_window_after_resume,
)
from robinhood_lp.qualification.per_pool_window_runbook import (
    DEFAULT_PER_POOL_BUDGET_ELAPSED_MS,
    DEFAULT_PER_POOL_BUDGET_HTTP_REQUESTS,
    DEFAULT_PER_POOL_BUDGET_LOGICAL_CALLS,
    DEFAULT_PER_POOL_BUDGET_PROVIDER_UNITS,
    DEFAULT_PER_POOL_BUDGET_RESPONSE_BYTES,
    DEFAULT_PER_POOL_BUDGET_ROWS,
    DOCUMENTED_PER_POOL_RUNBOOK_ROLES,
    ROLE_BLOCK_PINNED_STATE_READ,
    ROLE_SAMPLED_CROSS_VALIDATION,
    ROLE_WIDE_POOL_FILTERED_SCAN,
    PerPoolEndpointRoutingEntry,
    PerPoolOperatorRunbook,
    PerPoolRunbookBudget,
    build_per_pool_operator_runbook,
)
from robinhood_lp.qualification.per_pool_window_runbook import (
    DEFAULT_PRIMARY_ALIAS as PER_POOL_DEFAULT_PRIMARY_ALIAS,
)
from robinhood_lp.qualification.per_pool_window_runbook import (
    DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL as PER_POOL_DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL,
)
from robinhood_lp.qualification.per_pool_window_runbook import (
    DEFAULT_SECONDARY_ALIAS as PER_POOL_DEFAULT_SECONDARY_ALIAS,
)
from robinhood_lp.qualification.per_pool_window_runbook import (
    DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL as PER_POOL_DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL,
)
from robinhood_lp.qualification.per_pool_window_runbook import (
    endpoints_summary as per_pool_endpoints_summary,
)
from robinhood_lp.qualification.pool_id_check import (
    PoolIdCheckResult,
    check_pool_id_derivation,
)
from robinhood_lp.qualification.reference import (
    BASELINE_DISTINCT_BLOCKS,
    BASELINE_DONATE_COUNT,
    BASELINE_INITIALIZE_COUNT,
    BASELINE_MODIFY_LIQUIDITY_COUNT,
    BASELINE_PROTOCOL_FEE_UPDATED_COUNT,
    BASELINE_SWAP_COUNT,
    BASELINE_TOTAL_EVENTS,
    REFERENCE_CHAIN_ID,
    REFERENCE_COVERAGE_FROM_BLOCK,
    REFERENCE_COVERAGE_TO_BLOCK,
    REFERENCE_POOL_ID_HEX,
    REFERENCE_POOL_INIT_BLOCK,
    REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL,
    REFERENCE_STATE_VIEW_ADDRESS_HEX,
    REFERENCE_TARGET,
    ReferenceTarget,
    build_reference_pool_key,
)
from robinhood_lp.qualification.report import (
    ReferenceQualificationInputs,
    ReferenceQualificationReport,
    build_reference_qualification_report,
)
from robinhood_lp.qualification.runbook import (
    OPERATOR_RUNBOOK,
    EndpointRoutingEntry,
    OperatorRunbook,
    build_operator_runbook,
)
from robinhood_lp.qualification.second_pool import (
    RESOLVE_CHAIN_ID_MISMATCH,
    RESOLVE_HOOK_ADDRESS_AMBIGUOUS,
    RESOLVE_OK,
    RESOLVE_PINNED_POOL_ID_SIZE_DEFECT,
    RESOLVE_POOL_ID_MISMATCH,
    RESOLVE_POOL_KEY_DECODE_ERROR,
    RESOLVE_WINDOW_UNRESOLVED,
    SECOND_POOL_CHAIN_ID,
    SUPPORT_LEVEL_INGESTION,
    SUPPORT_LEVEL_REJECTED,
    ResolvedPoolKey,
    SecondPoolResolveResult,
    build_resolved_pool_key,
    build_second_pool_resolve_result_from_resolved_fields,
    classify_second_pool_support_level,
    resolve_second_pool_identity,
    resolve_second_pool_via_hook_scan,
)
from robinhood_lp.qualification.state_spot_check import (
    StateSpotCheckResult,
    StateViewGoldenValues,
    perform_state_spot_check,
)
from robinhood_lp.qualification.two_pool import (
    PerPoolT034Report,
    TwoPoolT038Report,
    assemble_two_pool_report,
    build_excluded_pool_report,
    build_included_pool_report,
    build_two_pool_report,
)
from robinhood_lp.qualification.two_pool_failure_paths import (
    DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS,
    FAILURE_PATH_KIND_FINALIZED_DISAGREEMENT,
    FAILURE_PATH_KIND_FINALIZED_UNAVAILABLE,
    FAILURE_PATH_KIND_REQUEST_WIDER_THAN_CAPABILITY,
    TwoPoolFailurePathEvidence,
    all_documented_two_pool_failure_path_evidence,
    build_two_pool_failure_path_evidence,
    fail_complete_under_two_pool_failure_paths,
)
from robinhood_lp.qualification.two_pool_failure_paths import (
    FAILURE_PATH_KIND_BUDGET_EXHAUSTED as T038_FAIL_BUDGET_EXHAUSTED,
)
from robinhood_lp.qualification.two_pool_failure_paths import (
    FAILURE_PATH_KIND_HTTP_429 as T038_FAIL_HTTP_429,
)
from robinhood_lp.qualification.two_pool_runbook import (
    DEFAULT_PRIMARY_ALIAS,
    DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL,
    DEFAULT_SECONDARY_ALIAS,
    DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL,
    TwoPoolEndpointRoutingEntry,
    TwoPoolOperatorRunbook,
    build_two_pool_operator_runbook,
)
from robinhood_lp.qualification.two_pool_window import (
    OUTCOME_POOL_INCLUDED,
    OUTCOME_POOL_INIT_OUTSIDE_WINDOW,
    PIN_DISAGREEMENT,
    PIN_OK,
    PIN_UNAVAILABLE,
    SECOND_POOL_HOOK_ADDRESS_HEX,
    SECOND_POOL_POOL_ID_HEX,
    TWO_POOL_CHAIN_ID,
    TWO_POOL_INIT_EXTENSION_CAP_BLOCKS,
    TWO_POOL_WINDOW_BLOCKS,
    EndpointFinalizedObservation,
    PoolWindowOutcome,
    TwoPoolCandidate,
    TwoPoolWindowPlan,
    WindowPin,
    apply_window_rule,
    finalize_window_pin,
    reference_pool_candidate,
    second_pool_candidate,
)

__all__ = [
    "BASELINE_DONATE_COUNT",
    "BASELINE_DISTINCT_BLOCKS",
    "BASELINE_INITIALIZE_COUNT",
    "BASELINE_MODIFY_LIQUIDITY_COUNT",
    "BASELINE_PROTOCOL_FEE_UPDATED_COUNT",
    "BASELINE_SWAP_COUNT",
    "BASELINE_TOTAL_EVENTS",
    "DEFAULT_ADVERSARIAL_TESTS",
    "DEFAULT_PRIMARY_ALIAS",
    "DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL",
    "DEFAULT_SECONDARY_ALIAS",
    "DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL",
    "DELTA_PAIR_TABLE",
    "DOCUMENTED_TWO_POOL_FAILURE_PATH_KINDS",
    "ENABLED_CALLBACK_NAMES",
    "EmptySetHookEvidence",
    "EndpointFinalizedObservation",
    "EndpointRoutingEntry",
    "FAILURE_PATH_KIND_BUDGET_EXHAUSTED",
    "FAILURE_PATH_KIND_FAILOVER",
    "FAILURE_PATH_KIND_FINALIZED_DISAGREEMENT",
    "FAILURE_PATH_KIND_FINALIZED_UNAVAILABLE",
    "FAILURE_PATH_KIND_HTTP_429",
    "FAILURE_PATH_KIND_LOGS_LIMIT_REJECTION",
    "FAILURE_PATH_KIND_REQUEST_WIDER_THAN_CAPABILITY",
    "FAILURE_PATH_KIND_RPC_TIMEOUT",
    "FidelityCheckResult",
    "FidelityWindowSample",
    "FailurePathEvidence",
    "HOOK_FLAG_NAMES",
    "HOOK_PACK_SPEC_REVISION",
    "HookDeltaPath",
    "HookInvalidationRule",
    "HookPack",
    "HookPackReport",
    "HookPoolClassificationOutcome",
    "HookReplayModel",
    "HookSourcePin",
    "OPERATOR_RUNBOOK",
    "OUTCOME_POOL_INCLUDED",
    "OUTCOME_POOL_INIT_OUTSIDE_WINDOW",
    "OperatorRunbook",
    "PIN_DISAGREEMENT",
    "PIN_OK",
    "PIN_UNAVAILABLE",
    "PerPoolT034Report",
    "PoolIdCheckResult",
    "PoolWindowOutcome",
    "REFERENCE_CHAIN_ID",
    "REFERENCE_COVERAGE_FROM_BLOCK",
    "REFERENCE_COVERAGE_TO_BLOCK",
    "REFERENCE_POOL_ID_HEX",
    "REFERENCE_POOL_INIT_BLOCK",
    "REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL",
    "REFERENCE_STATE_VIEW_ADDRESS_HEX",
    "REFERENCE_TARGET",
    "REQUIRED_HOOK_PACK_FIELDS",
    "RESOLVE_CHAIN_ID_MISMATCH",
    "RESOLVE_HOOK_ADDRESS_AMBIGUOUS",
    "RESOLVE_OK",
    "RESOLVE_PINNED_POOL_ID_SIZE_DEFECT",
    "RESOLVE_POOL_ID_MISMATCH",
    "RESOLVE_POOL_KEY_DECODE_ERROR",
    "RESOLVE_WINDOW_UNRESOLVED",
    "ReferenceQualificationReport",
    "ReferenceQualificationInputs",
    "ReferenceTarget",
    "ResolvedPoolKey",
    "SECOND_POOL_CHAIN_ID",
    "SECOND_POOL_HOOK_ADDRESS_HEX",
    "SECOND_POOL_POOL_ID_HEX",
    "SUPPORT_LEVEL_INGESTION",
    "SUPPORT_LEVEL_REJECTED",
    "SecondPoolResolveResult",
    "StateSpotCheckResult",
    "StateViewGoldenValues",
    "T038_FAIL_BUDGET_EXHAUSTED",
    "T038_FAIL_HTTP_429",
    "TWO_POOL_CHAIN_ID",
    "TWO_POOL_INIT_EXTENSION_CAP_BLOCKS",
    "TWO_POOL_WINDOW_BLOCKS",
    "TwoPoolCandidate",
    "TwoPoolEndpointRoutingEntry",
    "TwoPoolFailurePathEvidence",
    "TwoPoolOperatorRunbook",
    "TwoPoolT038Report",
    "TwoPoolWindowPlan",
    "WindowPin",
    "apply_window_rule",
    "assemble_two_pool_report",
    "BaselineComparison",
    "DOCUMENTED_PER_POOL_RUNBOOK_ROLES",
    "all_documented_two_pool_failure_path_evidence",
    "build_default_invalidation_rule",
    "build_default_replay_model",
    "build_default_source_pin",
    "build_empty_set_hook_evidence",
    "build_excluded_pool_report",
    "build_failure_path_evidence",
    "build_hook_pack",
    "build_hook_pack_report",
    "build_included_pool_report",
    "build_operator_runbook",
    "build_per_pool_coverage_report",
    "build_per_pool_operator_runbook",
    "build_pool_classification_outcome",
    "build_reference_pool_key",
    "build_reference_qualification_report",
    "build_resolved_pool_key",
    "build_second_pool_resolve_result_from_resolved_fields",
    "build_two_pool_failure_path_evidence",
    "build_two_pool_operator_runbook",
    "build_two_pool_report",
    "check_pool_id_derivation",
    "classify_second_pool_support_level",
    "assert_logical_call_count_equals_sub_ranges_plus_headers",
    "code_change_demotes_pack",
    "compare_against_baseline",
    "decode_hook_flag_bits",
    "demoted_pool_record",
    "derive_action_delta_invariants",
    "derive_before_after_deltas",
    "derive_dynamic_fee_behavior",
    "fail_complete_under_two_pool_failure_paths",
    "finalize_window_pin",
    "perform_fidelity_check",
    "perform_state_spot_check",
    "reference_pool_candidate",
    "resolve_second_pool_identity",
    "resolve_second_pool_via_hook_scan",
    "second_pool_candidate",
    "DEFAULT_ELAPSED_MS_BUDGET",
    "DEFAULT_HTTP_REQUEST_BUDGET",
    "DEFAULT_LOGICAL_CALL_BUDGET",
    "DEFAULT_PROVIDER_UNIT_BUDGET",
    "DEFAULT_RESPONSE_BYTES_BUDGET",
    "LogicalCallCountInvariantError",
    "DEFAULT_PER_POOL_BUDGET_ELAPSED_MS",
    "DEFAULT_PER_POOL_BUDGET_HTTP_REQUESTS",
    "DEFAULT_PER_POOL_BUDGET_LOGICAL_CALLS",
    "DEFAULT_PER_POOL_BUDGET_PROVIDER_UNITS",
    "DEFAULT_PER_POOL_BUDGET_RESPONSE_BYTES",
    "DEFAULT_PER_POOL_BUDGET_ROWS",
    "OUTCOME_POOL_ACQUIRED",
    "OUTCOME_POOL_ACQUIRED_AFTER_RESUME",
    "OUTCOME_POOL_INITIALIZED_AFTER_END",
    "OUTCOME_POOL_RESOLUTION_FAILED",
    "PER_POOL_CHAIN_ID",
    "PER_POOL_DEFAULT_PRIMARY_ALIAS",
    "PER_POOL_DEFAULT_PRIMARY_MAX_BLOCKS_PER_CALL",
    "PER_POOL_DEFAULT_SECONDARY_ALIAS",
    "PER_POOL_DEFAULT_SECONDARY_MAX_BLOCKS_PER_CALL",
    "PER_POOL_DEFAULT_BUDGET_LOGICAL_CALLS",
    "PER_POOL_DEFAULT_BUDGET_HTTP_REQUESTS",
    "PER_POOL_DEFAULT_BUDGET_RESPONSE_BYTES",
    "PER_POOL_DEFAULT_BUDGET_ROWS",
    "PER_POOL_DEFAULT_BUDGET_PROVIDER_UNITS",
    "PER_POOL_DEFAULT_BUDGET_ELAPSED_MS",
    "PerPoolCostRecord",
    "PerPoolCoverageReport",
    "PerPoolDataRoot",
    "PerPoolEndpointRoutingEntry",
    "PerPoolOperatorRunbook",
    "PerPoolReconciliation",
    "PerPoolRunbookBudget",
    "PerPoolWindowInputs",
    "PerPoolWindowPlan",
    "PlannedSubRange",
    "PriorCoverage",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_ENDPOINT_HISTORICAL_STATE_UNAVAILABLE",
    "REASON_FINALIZED_DISAGREEMENT",
    "REASON_FINALIZED_UNAVAILABLE",
    "REASON_HTTP_429_RATE_LIMIT",
    "REASON_LATEST_REJECTED",
    "REASON_NON_FINALIZED_BLOCK_REJECTED",
    "REASON_OK",
    "REASON_POOL_INITIALIZE_NOT_LOCATED",
    "REASON_POOL_INITIALIZED_AFTER_END",
    "REASON_PROVIDER_RETURNED_NON_FINALIZED",
    "REASON_RANGE_ALREADY_COVERED",
    "REASON_RANGE_WIDER_THAN_CAPABILITY",
    "REASON_WALL_CLOCK_REJECTED",
    "ROLE_BLOCK_PINNED_STATE_READ",
    "ROLE_SAMPLED_CROSS_VALIDATION",
    "ROLE_WIDE_POOL_FILTERED_SCAN",
    "BudgetCeiling",
    "DOCUMENTED_PER_POOL_REASON_CODES",
    "parse_finalized_pin_payloads",
    "per_pool_endpoints_summary",
    "per_pool_window_summary",
    "plan_per_pool_window",
    "plan_per_pool_window_after_resume",
    "reconcile_per_pool_reports_against_partitions",
]
