"""Reference-dataset qualification (T036).

The ``robinhood_lp.qualification`` package owns the qualification
pipeline that decides whether the real-mainnet ingestion run that
produced the pinned reference dataset may carry
``complete=true``. It builds on top of the T034 data-quality and
completeness reports (``robinhood_lp.quality``) by wiring together:

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
- the **operator runbook** that states the endpoint routing explicitly:
  the primary endpoint carries the wide pool-filtered scan; the
  secondary endpoint participates only within its measured
  per-call capability for sampled cross-validation and the
  block-pinned StateView read.

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
from robinhood_lp.qualification.state_spot_check import (
    StateSpotCheckResult,
    StateViewGoldenValues,
    perform_state_spot_check,
)

__all__ = [
    "BASELINE_DONATE_COUNT",
    "BASELINE_DISTINCT_BLOCKS",
    "BASELINE_INITIALIZE_COUNT",
    "BASELINE_MODIFY_LIQUIDITY_COUNT",
    "BASELINE_PROTOCOL_FEE_UPDATED_COUNT",
    "BASELINE_SWAP_COUNT",
    "BASELINE_TOTAL_EVENTS",
    "FAILURE_PATH_KIND_BUDGET_EXHAUSTED",
    "FAILURE_PATH_KIND_FAILOVER",
    "FAILURE_PATH_KIND_HTTP_429",
    "FAILURE_PATH_KIND_LOGS_LIMIT_REJECTION",
    "FAILURE_PATH_KIND_RPC_TIMEOUT",
    "FailurePathEvidence",
    "EndpointRoutingEntry",
    "FidelityCheckResult",
    "FidelityWindowSample",
    "OPERATOR_RUNBOOK",
    "OperatorRunbook",
    "REFERENCE_CHAIN_ID",
    "REFERENCE_COVERAGE_FROM_BLOCK",
    "REFERENCE_COVERAGE_TO_BLOCK",
    "REFERENCE_POOL_ID_HEX",
    "REFERENCE_POOL_INIT_BLOCK",
    "REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL",
    "REFERENCE_STATE_VIEW_ADDRESS_HEX",
    "REFERENCE_TARGET",
    "BaselineComparison",
    "PoolIdCheckResult",
    "ReferenceQualificationReport",
    "ReferenceQualificationInputs",
    "ReferenceTarget",
    "StateSpotCheckResult",
    "StateViewGoldenValues",
    "build_failure_path_evidence",
    "build_operator_runbook",
    "build_reference_pool_key",
    "build_reference_qualification_report",
    "check_pool_id_derivation",
    "compare_against_baseline",
    "perform_fidelity_check",
    "perform_state_spot_check",
]
