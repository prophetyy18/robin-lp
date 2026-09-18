"""Reason codes for T034 data-quality and completeness reports.

Every defect, deviation, or qualification failure reported by T034 maps
to exactly one reason code from this module. The set is closed: each
code is a distinct surface that downstream code reads to understand
what went wrong, and the report never collapses distinct failure modes
into a single counter.

Reason codes are grouped by category. The categories are:

- **coverage** — gaps in block-range / block-hash coverage;
- **ordering** — duplicate or misordered log rows;
- **pool registry** — pools the scan did not expect to see;
- **decode / value** — events the schema / ABI decode pipeline could
  not reconcile;
- **endpoint** — staleness / metadata failures on the data source;
- **budget / capability** — run-level exhaustion / regression;
- **cross-endpoint sample** — missing or disagreeing A+B comparison;
- **infrastructure correlation** — evidence-state bookkeeping;
- **checkpoint** — warm-run qualified-checkpoint mismatches.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

REASON_RANGE_COVERAGE_GAP: Final[str] = "range_coverage_gap"
REASON_BLOCK_HASH_GAP: Final[str] = "block_hash_gap"

# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

REASON_DUPLICATE_EVENT_KEY: Final[str] = "duplicate_event_key"
REASON_MISORDERED_LOG: Final[str] = "misordered_log"

# ---------------------------------------------------------------------------
# Pool registry
# ---------------------------------------------------------------------------

REASON_UNKNOWN_POOL: Final[str] = "unknown_pool"

# ---------------------------------------------------------------------------
# Decode / value
# ---------------------------------------------------------------------------

REASON_DECODE_ERROR: Final[str] = "decode_error"
REASON_IMPOSSIBLE_VALUE: Final[str] = "impossible_value"

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

REASON_STALE_ENDPOINT: Final[str] = "stale_endpoint"
REASON_METADATA_FAILURE: Final[str] = "metadata_failure"
REASON_CROSS_PROVIDER_DISCREPANCY: Final[str] = "cross_provider_discrepancy"

# ---------------------------------------------------------------------------
# Budget / capability
# ---------------------------------------------------------------------------

REASON_BUDGET_EXHAUSTED: Final[str] = "budget_exhausted"
REASON_CAPABILITY_REGRESSION: Final[str] = "capability_regression"
REASON_USER_AGENT_REJECTED: Final[str] = "user_agent_rejected"

# ---------------------------------------------------------------------------
# Cross-endpoint sample (Decision 6)
# ---------------------------------------------------------------------------

REASON_CROSS_ENDPOINT_SAMPLE_MISSING: Final[str] = "cross_endpoint_sample_missing"
REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE: Final[str] = "cross_endpoint_sample_disagree"
REASON_CROSS_ENDPOINT_SAMPLE_INFRASTRUCTURE_UNCLEAR: Final[str] = (
    "infrastructure_correlation_state_unrecorded"
)

# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------

REASON_CHECKPOINT_MISMATCH: Final[str] = "checkpoint_mismatch"

# ---------------------------------------------------------------------------
# Partition reconciliation (T037)
# ---------------------------------------------------------------------------

#: The per-partition reconciliation check (storage/reconciliation.py)
#: compares every partition's ``event_index`` EventKey set against the
#: rows the on-disk Parquet file carries. A mismatch — rows present in
#: the index but not the file (silent append), or rows present in the
#: file but not the index (manifest corruption) — surfaces this reason
#: code and forces ``complete=false``. The data-quality verifier
#: (quality/verification.py) accepts the reconciliation reports and
#: refuses to mark the dataset complete while any partition disagrees.
REASON_PARTITION_EVENT_INDEX_PARQUET_MISMATCH: Final[str] = "partition_event_index_parquet_mismatch"

ALL_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        REASON_BLOCK_HASH_GAP,
        REASON_BUDGET_EXHAUSTED,
        REASON_CAPABILITY_REGRESSION,
        REASON_CHECKPOINT_MISMATCH,
        REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE,
        REASON_CROSS_ENDPOINT_SAMPLE_INFRASTRUCTURE_UNCLEAR,
        REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
        REASON_CROSS_PROVIDER_DISCREPANCY,
        REASON_DECODE_ERROR,
        REASON_DUPLICATE_EVENT_KEY,
        REASON_IMPOSSIBLE_VALUE,
        REASON_METADATA_FAILURE,
        REASON_MISORDERED_LOG,
        REASON_PARTITION_EVENT_INDEX_PARQUET_MISMATCH,
        REASON_RANGE_COVERAGE_GAP,
        REASON_STALE_ENDPOINT,
        REASON_UNKNOWN_POOL,
        REASON_USER_AGENT_REJECTED,
    }
)

#: Reason codes that force ``complete=false`` whenever they are raised.
#: A reason code is qualification-fatal when the report cannot recover
#: from the failure within the current run; the run must halt.
QUALIFICATION_FATAL_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        REASON_BUDGET_EXHAUSTED,
        REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE,
        REASON_CROSS_ENDPOINT_SAMPLE_INFRASTRUCTURE_UNCLEAR,
        REASON_CROSS_ENDPOINT_SAMPLE_MISSING,
        REASON_PARTITION_EVENT_INDEX_PARQUET_MISMATCH,
    }
)


def is_qualification_fatal_reason(reason_code: str) -> bool:
    """Return True iff ``reason_code`` forces ``complete=false``."""
    return reason_code in QUALIFICATION_FATAL_REASON_CODES


__all__ = [
    "ALL_REASON_CODES",
    "QUALIFICATION_FATAL_REASON_CODES",
    "REASON_BLOCK_HASH_GAP",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_CAPABILITY_REGRESSION",
    "REASON_CHECKPOINT_MISMATCH",
    "REASON_CROSS_ENDPOINT_SAMPLE_DISAGREE",
    "REASON_CROSS_ENDPOINT_SAMPLE_INFRASTRUCTURE_UNCLEAR",
    "REASON_CROSS_ENDPOINT_SAMPLE_MISSING",
    "REASON_CROSS_PROVIDER_DISCREPANCY",
    "REASON_DECODE_ERROR",
    "REASON_DUPLICATE_EVENT_KEY",
    "REASON_IMPOSSIBLE_VALUE",
    "REASON_METADATA_FAILURE",
    "REASON_MISORDERED_LOG",
    "REASON_PARTITION_EVENT_INDEX_PARQUET_MISMATCH",
    "REASON_RANGE_COVERAGE_GAP",
    "REASON_STALE_ENDPOINT",
    "REASON_UNKNOWN_POOL",
    "REASON_USER_AGENT_REJECTED",
    "is_qualification_fatal_reason",
]
