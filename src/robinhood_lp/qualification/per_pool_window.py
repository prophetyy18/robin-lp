"""Per-pool extended-history window planner (T039 / ADR-015).

The T039 contract supersedes T038's ten-million-block rule with a
per-pool rule:

- the **window start** is the pool's own ``Initialize`` block (or
  the earliest block at which it existed); there is no fixed width,
  no extension allowance, and no ``pool_init_outside_window``
  exclusion;
- the **window end** is a single finalized block the two qualified
  endpoints agree on at run start, pinned by block number and
  block hash into the run record, the per-pool coverage report
  and the evidence pack;
- a pool whose ``Initialize`` cannot be located inside the
  candidate range is a **failed resolution that stops and is
  reported**, not a pool to be excluded from an otherwise valid
  dataset;
- one acquisition run covers exactly one ``PoolKey``: the
  research universe is built by running the acquisition once per
  pool, never by widening a single run to several pools.

The T038 module, tests and dataset generations remain immutable
historical evidence: they are not edited, re-qualified or merged,
and no clause of this module cites the ten-million-block window
as the research range. The ``per_pool_window`` module reuses the
T038 window-pin machinery (:class:`WindowPin`,
:class:`EndpointFinalizedObservation`, :func:`finalize_window_pin`)
because the finalized-block agreement rule is unchanged; it adds
a fresh, per-pool window planner and the artifacts the research
dataset registry consumes.

The module is deterministic and offline: every input is either a
pinned reference fact, an operator-supplied observation, or a
runner-measured capability. There is no I/O, no wall clock, no
random sources.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# Constants (T039 contract, not planning choices)
# ---------------------------------------------------------------------------

#: The Owner-pinned Robinhood Chain mainnet chain id (T036 / T038).
PER_POOL_CHAIN_ID: Final[int] = 4663

#: Outcome vocabulary the per-pool window planner emits. The
#: ten-million-block rule's ``pool_init_outside_window`` outcome is
#: intentionally absent: under T039 / ADR-015 the only legitimate
#: outcomes for an included pool are ``pool_acquired`` and
#: ``pool_acquired_after_checkpoint_resume``; a pool whose
#: ``Initialize`` cannot be located is a **failed resolution** that
#: halts the run rather than an exclusion.
OUTCOME_POOL_ACQUIRED: Final[str] = "pool_acquired"
OUTCOME_POOL_ACQUIRED_AFTER_RESUME: Final[str] = "pool_acquired_after_checkpoint_resume"
OUTCOME_POOL_RESOLUTION_FAILED: Final[str] = "pool_initialize_resolution_failed"
OUTCOME_POOL_INITIALIZED_AFTER_END: Final[str] = "pool_initialized_after_agreed_end"

#: Reason codes the planner surfaces for each documented failure
#: mode. The codes match the closed vocabulary the runner and the
#: verifier share; an unknown code is rejected at construction.
REASON_OK: Final[str] = "ok"
REASON_FINALIZED_UNAVAILABLE: Final[str] = "finalized_unavailable"
REASON_FINALIZED_DISAGREEMENT: Final[str] = "finalized_endpoint_disagreement"
REASON_LATEST_REJECTED: Final[str] = "latest_substitution_refused"
REASON_WALL_CLOCK_REJECTED: Final[str] = "wall_clock_bound_refused"
REASON_NON_FINALIZED_BLOCK_REJECTED: Final[str] = "non_finalized_block_refused"
REASON_POOL_INITIALIZE_NOT_LOCATED: Final[str] = "pool_initialize_not_located"
REASON_POOL_INITIALIZED_AFTER_END: Final[str] = "pool_initialized_after_agreed_end"
REASON_ENDPOINT_HISTORICAL_STATE_UNAVAILABLE: Final[str] = "endpoint_historical_state_unavailable"
REASON_BUDGET_EXHAUSTED: Final[str] = "budget_exhausted"
REASON_PROVIDER_RETURNED_NON_FINALIZED: Final[str] = (
    "provider_returned_non_finalized_for_finalized_tag"
)
REASON_RANGE_WIDER_THAN_CAPABILITY: Final[str] = "range_too_large"
REASON_HTTP_429_RATE_LIMIT: Final[str] = "http_429_rate_limit"
REASON_RANGE_ALREADY_COVERED: Final[str] = "range_already_covered_by_prior_run"

#: Closed vocabulary the planner emits. The set is small on purpose:
#: every code names a clause the verifier must reject, and adding a
#: code is a contract change rather than a planning detail.
DOCUMENTED_PER_POOL_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        REASON_OK,
        REASON_FINALIZED_UNAVAILABLE,
        REASON_FINALIZED_DISAGREEMENT,
        REASON_LATEST_REJECTED,
        REASON_WALL_CLOCK_REJECTED,
        REASON_NON_FINALIZED_BLOCK_REJECTED,
        REASON_POOL_INITIALIZE_NOT_LOCATED,
        REASON_POOL_INITIALIZED_AFTER_END,
        REASON_ENDPOINT_HISTORICAL_STATE_UNAVAILABLE,
        REASON_BUDGET_EXHAUSTED,
        REASON_PROVIDER_RETURNED_NON_FINALIZED,
        REASON_RANGE_WIDER_THAN_CAPABILITY,
        REASON_HTTP_429_RATE_LIMIT,
        REASON_RANGE_ALREADY_COVERED,
    }
)

#: Default budget ceilings a fresh run starts under. The contract
#: requires that a declared budget halt the run rather than be
#: exceeded silently; the planner surfaces the ceiling alongside the
#: actual consumption in the cost record.
DEFAULT_LOGICAL_CALL_BUDGET: Final[int] = 1_000_000
DEFAULT_HTTP_REQUEST_BUDGET: Final[int] = 1_000_000
DEFAULT_RESPONSE_BYTES_BUDGET: Final[int] = 8 * 1024 * 1024 * 1024  # 8 GiB
DEFAULT_PROVIDER_UNIT_BUDGET: Final[int] = 1_000_000_000
DEFAULT_ELAPSED_MS_BUDGET: Final[int] = 24 * 60 * 60 * 1000  # 24 hours


# ---------------------------------------------------------------------------
# Per-pool window plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerPoolWindowPlan:
    """The deterministic per-pool window plan T039 requires.

    One plan covers exactly one ``PoolKey`` and one window from that
    pool's ``Initialize`` block (or the earliest block at which it
    existed) to the run's agreed finalized block. The plan records
    the address of every endpoint that contributed to the agreed
    pin and the bounds of every sub-range the runner walks.

    ``sub_ranges`` is the ordered list of ``eth_getLogs``-bounded
    sub-ranges the runner must walk; the planner emits them so a
    range wider than the endpoint's measured per-call log ceiling
    is split rather than rejected. ``deduplicated_header_block_count``
    is the count of distinct event blocks the runner is expected to
    issue ``eth_getBlockByNumber`` calls for; the runner asserts the
    logical-call count equals this number after dedup, never above.
    """

    pool_key_id: str
    chain_id: int
    pool_init_block: int
    window_pin_block_number: int
    window_pin_block_hash: str
    coverage_from_block: int
    coverage_to_block: int
    pool_init_block_hash: str
    window_width_blocks: int
    outcome: str
    reason_code: str
    primary_endpoint_alias: str
    secondary_endpoint_alias: str
    primary_max_blocks_per_call: int
    secondary_max_blocks_per_call: int
    sub_ranges: tuple[PlannedSubRange, ...]
    deduplicated_header_block_count: int
    expected_logical_call_count: int
    budget_ceiling: BudgetCeiling
    recorded_at: str = ""

    def __post_init__(self) -> None:
        if self.pool_init_block < 0:
            raise ValueError(
                f"PerPoolWindowPlan: pool_init_block must be >= 0, got {self.pool_init_block}"
            )
        if self.coverage_from_block < 0:
            raise ValueError(
                f"PerPoolWindowPlan: coverage_from_block must be >= 0, "
                f"got {self.coverage_from_block}"
            )
        if self.window_pin_block_number < 0:
            raise ValueError(
                f"PerPoolWindowPlan: window_pin_block_number must be >= 0, "
                f"got {self.window_pin_block_number}"
            )
        if self.coverage_from_block > self.coverage_to_block and self.window_width_blocks != 0:
            # Failed-resolution outcomes (``pool_initialized_after_agreed_end``
            # or ``pool_initialize_resolution_failed``) report an empty window
            # so the audit trail can show the run halted before any chain
            # read; the only legal width in that case is ``0``.
            raise ValueError(
                f"PerPoolWindowPlan: coverage_from_block {self.coverage_from_block} "
                f"> coverage_to_block {self.coverage_to_block} but "
                f"window_width_blocks is {self.window_width_blocks}; the only legal "
                "width for a failed-resolution plan is 0"
            )
        if self.coverage_from_block <= self.coverage_to_block:
            if self.coverage_to_block > self.window_pin_block_number:
                raise ValueError(
                    f"PerPoolWindowPlan: coverage_to_block {self.coverage_to_block} "
                    f"> window_pin_block_number {self.window_pin_block_number}"
                )
            if self.window_width_blocks != (self.coverage_to_block - self.coverage_from_block + 1):
                raise ValueError(
                    f"PerPoolWindowPlan: window_width_blocks {self.window_width_blocks} "
                    f"!= coverage_to_block {self.coverage_to_block} - "
                    f"coverage_from_block {self.coverage_from_block} + 1"
                )
        if self.outcome not in (
            OUTCOME_POOL_ACQUIRED,
            OUTCOME_POOL_ACQUIRED_AFTER_RESUME,
            OUTCOME_POOL_RESOLUTION_FAILED,
            OUTCOME_POOL_INITIALIZED_AFTER_END,
        ):
            raise ValueError(
                f"PerPoolWindowPlan: outcome {self.outcome!r} not in the documented per-pool "
                "outcome vocabulary"
            )
        if self.reason_code not in DOCUMENTED_PER_POOL_REASON_CODES:
            raise ValueError(
                f"PerPoolWindowPlan: reason_code {self.reason_code!r} not in "
                "DOCUMENTED_PER_POOL_REASON_CODES"
            )
        if not self.pool_key_id:
            raise ValueError("PerPoolWindowPlan: pool_key_id must be a non-empty string")
        if not self.primary_endpoint_alias or not self.secondary_endpoint_alias:
            raise ValueError(
                "PerPoolWindowPlan: primary_endpoint_alias and secondary_endpoint_alias "
                "must be non-empty"
            )
        if self.primary_endpoint_alias == self.secondary_endpoint_alias:
            raise ValueError(
                "PerPoolWindowPlan: primary_endpoint_alias and secondary_endpoint_alias must differ"
            )
        if self.primary_max_blocks_per_call <= 0:
            raise ValueError(
                "PerPoolWindowPlan: primary_max_blocks_per_call must be > 0, "
                f"got {self.primary_max_blocks_per_call}"
            )
        if self.secondary_max_blocks_per_call <= 0:
            raise ValueError(
                "PerPoolWindowPlan: secondary_max_blocks_per_call must be > 0, "
                f"got {self.secondary_max_blocks_per_call}"
            )
        if self.deduplicated_header_block_count < 0:
            raise ValueError(
                "PerPoolWindowPlan: deduplicated_header_block_count must be >= 0, "
                f"got {self.deduplicated_header_block_count}"
            )
        if self.expected_logical_call_count < 0:
            raise ValueError(
                f"PerPoolWindowPlan: expected_logical_call_count must be >= 0, "
                f"got {self.expected_logical_call_count}"
            )

    @property
    def is_acquired(self) -> bool:
        """``True`` when the plan records a successful acquisition."""
        return self.outcome in (
            OUTCOME_POOL_ACQUIRED,
            OUTCOME_POOL_ACQUIRED_AFTER_RESUME,
        )

    @property
    def is_resumed(self) -> bool:
        """``True`` when the plan resumed from a prior checkpoint."""
        return self.outcome == OUTCOME_POOL_ACQUIRED_AFTER_RESUME

    @property
    def is_failed(self) -> bool:
        """``True`` when the plan halted on a documented failure."""
        return self.outcome in (
            OUTCOME_POOL_RESOLUTION_FAILED,
            OUTCOME_POOL_INITIALIZED_AFTER_END,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "robinhood_lp.qualification.per_pool_window.v1",
            "pool_key_id": self.pool_key_id,
            "chain_id": self.chain_id,
            "pool_init_block": self.pool_init_block,
            "pool_init_block_hash": self.pool_init_block_hash,
            "window_pin_block_number": self.window_pin_block_number,
            "window_pin_block_hash": self.window_pin_block_hash,
            "coverage_from_block": self.coverage_from_block,
            "coverage_to_block": self.coverage_to_block,
            "window_width_blocks": self.window_width_blocks,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "primary_endpoint_alias": self.primary_endpoint_alias,
            "secondary_endpoint_alias": self.secondary_endpoint_alias,
            "primary_max_blocks_per_call": self.primary_max_blocks_per_call,
            "secondary_max_blocks_per_call": self.secondary_max_blocks_per_call,
            "sub_ranges": [sub.to_dict() for sub in self.sub_ranges],
            "deduplicated_header_block_count": self.deduplicated_header_block_count,
            "expected_logical_call_count": self.expected_logical_call_count,
            "budget_ceiling": self.budget_ceiling.to_dict(),
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class PlannedSubRange:
    """One sub-range the per-pool runner must walk.

    The width is bounded by the endpoint's measured per-call log
    ceiling; the planner emits enough sub-ranges to cover the
    inclusive ``[coverage_from_block, coverage_to_block]`` window.
    Sub-ranges are inclusive on both ends; the runner never widens
    a sub-range beyond the planner's emission.
    """

    from_block: int
    to_block: int

    def __post_init__(self) -> None:
        if self.from_block < 0:
            raise ValueError(f"PlannedSubRange: from_block must be >= 0, got {self.from_block}")
        if self.to_block < self.from_block:
            raise ValueError(
                f"PlannedSubRange: to_block {self.to_block} < from_block {self.from_block}"
            )

    @property
    def width(self) -> int:
        """Inclusive width in blocks (1 for a single-block sub-range)."""
        return self.to_block - self.from_block + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_block": self.from_block,
            "to_block": self.to_block,
            "width": self.width,
        }


@dataclass(frozen=True, slots=True)
class BudgetCeiling:
    """The declared budget ceiling the per-pool run must respect.

    A ceiling is a hard cap on every cost dimension the per-pool
    cost record tracks: logical calls, HTTP requests, response
    bytes, rows, provider units and elapsed time. The runner halts
    rather than exceeding any dimension silently; the cost record
    records both the ceiling and the actual consumption so an
    operator can compare against the per-pool budget in review.
    """

    logical_calls: int = DEFAULT_LOGICAL_CALL_BUDGET
    http_requests: int = DEFAULT_HTTP_REQUEST_BUDGET
    response_bytes: int = DEFAULT_RESPONSE_BYTES_BUDGET
    rows: int = DEFAULT_LOGICAL_CALL_BUDGET
    provider_units: int = DEFAULT_PROVIDER_UNIT_BUDGET
    elapsed_ms: int = DEFAULT_ELAPSED_MS_BUDGET

    def __post_init__(self) -> None:
        if self.logical_calls < 0:
            raise ValueError(f"BudgetCeiling: logical_calls must be >= 0, got {self.logical_calls}")
        if self.http_requests < 0:
            raise ValueError(f"BudgetCeiling: http_requests must be >= 0, got {self.http_requests}")
        if self.response_bytes < 0:
            raise ValueError(
                f"BudgetCeiling: response_bytes must be >= 0, got {self.response_bytes}"
            )
        if self.rows < 0:
            raise ValueError(f"BudgetCeiling: rows must be >= 0, got {self.rows}")
        if self.provider_units < 0:
            raise ValueError(
                f"BudgetCeiling: provider_units must be >= 0, got {self.provider_units}"
            )
        if self.elapsed_ms < 0:
            raise ValueError(f"BudgetCeiling: elapsed_ms must be >= 0, got {self.elapsed_ms}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_calls": self.logical_calls,
            "http_requests": self.http_requests,
            "response_bytes": self.response_bytes,
            "rows": self.rows,
            "provider_units": self.provider_units,
            "elapsed_ms": self.elapsed_ms,
        }


# ---------------------------------------------------------------------------
# Inputs the planner consumes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerPoolWindowInputs:
    """The deterministic inputs the per-pool window planner consumes.

    ``pool_key_id`` is a short opaque identifier (typically
    ``chain_id:pool_id_hex``); ``pool_init_block`` is the inclusive
    ``Initialize`` block the pool was located at on chain;
    ``pool_init_block_hash`` is the corresponding 32-byte block hash
    the chain emitted. ``window_pin_block_number`` /
    ``window_pin_block_hash`` are the agreed finalized-block pin the
    runner read at run start from both qualified endpoints.

    ``prior_coverage`` carries the durable checkpoint of any prior
    acquisition run: when supplied and its ``qualified_to_block`` is
    ``>= window_pin_block_number``, the planner records an
    ``already_covered`` outcome without issuing any chain read.

    The planner never accepts ``latest``, a non-finalized block, or
    a wall-clock-derived bound as the window end; callers that need
    such a pin surface a hard error rather than letting the planner
    silently coerce the value.
    """

    pool_key_id: str
    pool_init_block: int
    pool_init_block_hash: str
    window_pin_block_number: int
    window_pin_block_hash: str
    primary_endpoint_alias: str = "robinhood_public"
    secondary_endpoint_alias: str = "alchemy_free"
    primary_max_blocks_per_call: int = 10_000
    secondary_max_blocks_per_call: int = 10
    chain_id: int = PER_POOL_CHAIN_ID
    budget_ceiling: BudgetCeiling = field(default_factory=BudgetCeiling)
    prior_coverage: PriorCoverage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise ValueError(
                f"PerPoolWindowInputs: pool_key_id must be non-empty str, got {self.pool_key_id!r}"
            )
        if self.pool_init_block < 0:
            raise ValueError(
                f"PerPoolWindowInputs: pool_init_block must be >= 0, got {self.pool_init_block}"
            )
        if self.window_pin_block_number < 0:
            raise ValueError(
                f"PerPoolWindowInputs: window_pin_block_number must be >= 0, "
                f"got {self.window_pin_block_number}"
            )
        if not self.pool_init_block_hash:
            raise ValueError(
                "PerPoolWindowInputs: pool_init_block_hash must be a non-empty 0x-hex string"
            )
        if not self.window_pin_block_hash:
            raise ValueError(
                "PerPoolWindowInputs: window_pin_block_hash must be a non-empty 0x-hex string"
            )
        if not self.primary_endpoint_alias or not self.secondary_endpoint_alias:
            raise ValueError(
                "PerPoolWindowInputs: primary_endpoint_alias and secondary_endpoint_alias "
                "must be non-empty"
            )
        if self.primary_endpoint_alias == self.secondary_endpoint_alias:
            raise ValueError(
                "PerPoolWindowInputs: primary_endpoint_alias and secondary_endpoint_alias "
                "must differ"
            )
        if self.primary_max_blocks_per_call <= 0:
            raise ValueError(
                f"PerPoolWindowInputs: primary_max_blocks_per_call must be > 0, "
                f"got {self.primary_max_blocks_per_call}"
            )
        if self.secondary_max_blocks_per_call <= 0:
            raise ValueError(
                f"PerPoolWindowInputs: secondary_max_blocks_per_call must be > 0, "
                f"got {self.secondary_max_blocks_per_call}"
            )
        if self.chain_id <= 0:
            raise ValueError(f"PerPoolWindowInputs: chain_id must be > 0, got {self.chain_id}")


@dataclass(frozen=True, slots=True)
class PriorCoverage:
    """The durable checkpoint of a prior per-pool acquisition run.

    ``qualified_from_block`` and ``qualified_to_block`` define the
    inclusive range the prior run already acquired and qualified
    against. When ``qualified_to_block >= window_pin_block_number``,
    the planner records an ``already_covered`` outcome without
    issuing any chain read; the manifest checksum and pin block
    hash must match the run's pinned facts.
    """

    qualified_from_block: int
    qualified_to_block: int
    qualified_to_block_hash: str
    manifest_checksum: str
    schema_version: int
    decode_version: int
    capability_snapshot_id: str
    topology: str

    def __post_init__(self) -> None:
        if self.qualified_from_block < 0:
            raise ValueError(
                f"PriorCoverage: qualified_from_block must be >= 0, got {self.qualified_from_block}"
            )
        if self.qualified_to_block < self.qualified_from_block:
            raise ValueError(
                f"PriorCoverage: qualified_to_block {self.qualified_to_block} "
                f"< qualified_from_block {self.qualified_from_block}"
            )
        if not self.qualified_to_block_hash:
            raise ValueError(
                "PriorCoverage: qualified_to_block_hash must be a non-empty 0x-hex string"
            )
        if not self.manifest_checksum:
            raise ValueError("PriorCoverage: manifest_checksum must be a non-empty string")
        if not self.capability_snapshot_id:
            raise ValueError("PriorCoverage: capability_snapshot_id must be a non-empty string")
        if not self.topology:
            raise ValueError("PriorCoverage: topology must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "qualified_from_block": self.qualified_from_block,
            "qualified_to_block": self.qualified_to_block,
            "qualified_to_block_hash": self.qualified_to_block_hash,
            "manifest_checksum": self.manifest_checksum,
            "schema_version": self.schema_version,
            "decode_version": self.decode_version,
            "capability_snapshot_id": self.capability_snapshot_id,
            "topology": self.topology,
        }


# ---------------------------------------------------------------------------
# The window rule itself
# ---------------------------------------------------------------------------


def plan_per_pool_window(inputs: PerPoolWindowInputs) -> PerPoolWindowPlan:
    """Compute the deterministic per-pool window plan.

    The rule (T039 / ADR-015):

    1. ``coverage_from_block = pool_init_block`` — the pool's own
       ``Initialize`` block, never extended below.
    2. ``coverage_to_block = window_pin_block_number`` — the
       pinned, agreed finalized block.
    3. A pool whose ``Initialize`` lies **after** the agreed end is
       a failed resolution: the planner surfaces
       :data:`OUTCOME_POOL_INITIALIZED_AFTER_END` with reason code
       :data:`REASON_POOL_INITIALIZED_AFTER_END` and the run halts.
       The contract forbids widening the run to chase a pool that
       has not yet been initialized on chain at the agreed end.
    4. A pool whose ``Initialize`` cannot be located at all (the
       operator reports the candidate ``Initialize`` log scan did
       not surface the pool) is reported as
       :data:`OUTCOME_POOL_RESOLUTION_FAILED` with reason code
       :data:`REASON_POOL_INITIALIZE_NOT_LOCATED`; the planner never
       guesses an ``Initialize`` block from the pool identity or
       from a candidate window.
    5. ``prior_coverage``: when the durable checkpoint's qualified
       range already covers the new pinned end, the planner
       surfaces an ``already_covered`` outcome (encoded as the
       :data:`OUTCOME_POOL_ACQUIRED` outcome with the
       :data:`REASON_RANGE_ALREADY_COVERED` reason code) without
       issuing chain reads. The contract requires that a re-run
       produces no new chain reads for an already-covered range.
    6. The runner's expected logical-call count is
       ``len(sub_ranges) + deduplicated_header_block_count`` (one
       ``eth_getLogs`` per sub-range plus one ``eth_getBlockByNumber``
       per distinct event block). The planner asserts the equality
       downstream.

    The function is the deterministic assembly point: every input
    is either a pinned reference fact or an operator-supplied
    observation; the resulting plan records every input alongside
    the rule that produced it.
    """
    coverage_from_block = inputs.pool_init_block
    coverage_to_block = inputs.window_pin_block_number

    if coverage_from_block > coverage_to_block:
        # Pool was initialized after the agreed finalized end. This
        # is a failed resolution, not an exclusion: the run cannot
        # widen the agreed end and the pool cannot be acquired
        # under the T039 / ADR-015 rule. The coverage range is
        # recorded as the empty window ``[pin + 1, pin]`` so the
        # ``width == coverage_to - coverage_from + 1`` invariant
        # yields ``0`` and the audit trail sees a zero-width plan.
        return PerPoolWindowPlan(
            pool_key_id=inputs.pool_key_id,
            chain_id=inputs.chain_id,
            pool_init_block=inputs.pool_init_block,
            pool_init_block_hash=inputs.pool_init_block_hash,
            window_pin_block_number=inputs.window_pin_block_number,
            window_pin_block_hash=inputs.window_pin_block_hash,
            coverage_from_block=coverage_to_block + 1,
            coverage_to_block=coverage_to_block,
            window_width_blocks=0,
            outcome=OUTCOME_POOL_INITIALIZED_AFTER_END,
            reason_code=REASON_POOL_INITIALIZED_AFTER_END,
            primary_endpoint_alias=inputs.primary_endpoint_alias,
            secondary_endpoint_alias=inputs.secondary_endpoint_alias,
            primary_max_blocks_per_call=inputs.primary_max_blocks_per_call,
            secondary_max_blocks_per_call=inputs.secondary_max_blocks_per_call,
            sub_ranges=(),
            deduplicated_header_block_count=0,
            expected_logical_call_count=0,
            budget_ceiling=inputs.budget_ceiling,
            recorded_at="",
        )

    if inputs.prior_coverage is not None:
        prior = inputs.prior_coverage
        already_covered = (
            prior.qualified_from_block <= coverage_from_block
            and prior.qualified_to_block >= coverage_to_block
        )
        if already_covered:
            sub_ranges = _split_sub_ranges(
                coverage_from_block=coverage_from_block,
                coverage_to_block=coverage_to_block,
                per_call_capability=inputs.primary_max_blocks_per_call,
            )
            return PerPoolWindowPlan(
                pool_key_id=inputs.pool_key_id,
                chain_id=inputs.chain_id,
                pool_init_block=inputs.pool_init_block,
                pool_init_block_hash=inputs.pool_init_block_hash,
                window_pin_block_number=inputs.window_pin_block_number,
                window_pin_block_hash=inputs.window_pin_block_hash,
                coverage_from_block=coverage_from_block,
                coverage_to_block=coverage_to_block,
                window_width_blocks=coverage_to_block - coverage_from_block + 1,
                outcome=OUTCOME_POOL_ACQUIRED,
                reason_code=REASON_RANGE_ALREADY_COVERED,
                primary_endpoint_alias=inputs.primary_endpoint_alias,
                secondary_endpoint_alias=inputs.secondary_endpoint_alias,
                primary_max_blocks_per_call=inputs.primary_max_blocks_per_call,
                secondary_max_blocks_per_call=inputs.secondary_max_blocks_per_call,
                sub_ranges=(),
                deduplicated_header_block_count=0,
                expected_logical_call_count=0,
                budget_ceiling=inputs.budget_ceiling,
                recorded_at="",
            )

    sub_ranges = _split_sub_ranges(
        coverage_from_block=coverage_from_block,
        coverage_to_block=coverage_to_block,
        per_call_capability=inputs.primary_max_blocks_per_call,
    )

    # The expected logical-call count for an uninterrupted
    # acquisition is one ``eth_getLogs`` per sub-range plus one
    # ``eth_getBlockByNumber`` per distinct event block. The
    # deduplicated header block count is the count of distinct
    # blocks the sub-ranges collectively cover (a generous upper
    # bound when the actual distinct count is unknown at planning
    # time).
    deduplicated_header_block_count = coverage_to_block - coverage_from_block + 1
    expected_logical_call_count = len(sub_ranges) + deduplicated_header_block_count

    return PerPoolWindowPlan(
        pool_key_id=inputs.pool_key_id,
        chain_id=inputs.chain_id,
        pool_init_block=inputs.pool_init_block,
        pool_init_block_hash=inputs.pool_init_block_hash,
        window_pin_block_number=inputs.window_pin_block_number,
        window_pin_block_hash=inputs.window_pin_block_hash,
        coverage_from_block=coverage_from_block,
        coverage_to_block=coverage_to_block,
        window_width_blocks=coverage_to_block - coverage_from_block + 1,
        outcome=OUTCOME_POOL_ACQUIRED,
        reason_code=REASON_OK,
        primary_endpoint_alias=inputs.primary_endpoint_alias,
        secondary_endpoint_alias=inputs.secondary_endpoint_alias,
        primary_max_blocks_per_call=inputs.primary_max_blocks_per_call,
        secondary_max_blocks_per_call=inputs.secondary_max_blocks_per_call,
        sub_ranges=sub_ranges,
        deduplicated_header_block_count=deduplicated_header_block_count,
        expected_logical_call_count=expected_logical_call_count,
        budget_ceiling=inputs.budget_ceiling,
        recorded_at="",
    )


def plan_per_pool_window_after_resume(
    inputs: PerPoolWindowInputs,
    *,
    resume_from_block: int,
) -> PerPoolWindowPlan:
    """Compute the per-pool window plan after a checkpoint resume.

    The contract requires that interrupting and resuming a window
    be indistinguishable from an uninterrupted acquisition. The
    function re-computes the sub-ranges from ``resume_from_block``
    to the pinned end and records the
    :data:`OUTCOME_POOL_ACQUIRED_AFTER_RESUME` outcome so the audit
    trail records the resume boundary explicitly.

    ``resume_from_block`` must satisfy
    ``coverage_from_block <= resume_from_block <= coverage_to_block``;
    any other value is rejected at construction time. A
    ``resume_from_block`` equal to or strictly greater than the
    pinned end reduces to a no-op plan recorded with the
    :data:`REASON_RANGE_ALREADY_COVERED` reason code so the
    re-run produces no new chain reads.

    The function is the only path to an
    :data:`OUTCOME_POOL_ACQUIRED_AFTER_RESUME` outcome; the main
    :func:`plan_per_pool_window` entry point never emits it.
    """
    coverage_from_block = inputs.pool_init_block
    coverage_to_block = inputs.window_pin_block_number
    if resume_from_block < coverage_from_block or resume_from_block > coverage_to_block:
        raise ValueError(
            f"plan_per_pool_window_after_resume: resume_from_block {resume_from_block} "
            f"outside [{coverage_from_block}, {coverage_to_block}]"
        )

    if resume_from_block >= coverage_to_block:
        # Resuming at or beyond the pinned end means the durable
        # checkpoint already covers the requested window. The
        # contract requires that the re-run produce no new chain
        # reads; the planner records the
        # ``range_already_covered`` reason code so the audit trail
        # surfaces the no-op rather than silently re-reading.
        return PerPoolWindowPlan(
            pool_key_id=inputs.pool_key_id,
            chain_id=inputs.chain_id,
            pool_init_block=inputs.pool_init_block,
            pool_init_block_hash=inputs.pool_init_block_hash,
            window_pin_block_number=inputs.window_pin_block_number,
            window_pin_block_hash=inputs.window_pin_block_hash,
            coverage_from_block=coverage_from_block,
            coverage_to_block=coverage_to_block,
            window_width_blocks=coverage_to_block - coverage_from_block + 1,
            outcome=OUTCOME_POOL_ACQUIRED,
            reason_code=REASON_RANGE_ALREADY_COVERED,
            primary_endpoint_alias=inputs.primary_endpoint_alias,
            secondary_endpoint_alias=inputs.secondary_endpoint_alias,
            primary_max_blocks_per_call=inputs.primary_max_blocks_per_call,
            secondary_max_blocks_per_call=inputs.secondary_max_blocks_per_call,
            sub_ranges=(),
            deduplicated_header_block_count=0,
            expected_logical_call_count=0,
            budget_ceiling=inputs.budget_ceiling,
            recorded_at="",
        )

    sub_ranges = _split_sub_ranges(
        coverage_from_block=resume_from_block,
        coverage_to_block=coverage_to_block,
        per_call_capability=inputs.primary_max_blocks_per_call,
    )
    deduplicated_header_block_count = coverage_to_block - resume_from_block + 1
    expected_logical_call_count = len(sub_ranges) + deduplicated_header_block_count
    return PerPoolWindowPlan(
        pool_key_id=inputs.pool_key_id,
        chain_id=inputs.chain_id,
        pool_init_block=inputs.pool_init_block,
        pool_init_block_hash=inputs.pool_init_block_hash,
        window_pin_block_number=inputs.window_pin_block_number,
        window_pin_block_hash=inputs.window_pin_block_hash,
        coverage_from_block=coverage_from_block,
        coverage_to_block=coverage_to_block,
        window_width_blocks=coverage_to_block - coverage_from_block + 1,
        outcome=OUTCOME_POOL_ACQUIRED_AFTER_RESUME,
        reason_code=REASON_OK,
        primary_endpoint_alias=inputs.primary_endpoint_alias,
        secondary_endpoint_alias=inputs.secondary_endpoint_alias,
        primary_max_blocks_per_call=inputs.primary_max_blocks_per_call,
        secondary_max_blocks_per_call=inputs.secondary_max_blocks_per_call,
        sub_ranges=sub_ranges,
        deduplicated_header_block_count=deduplicated_header_block_count,
        expected_logical_call_count=expected_logical_call_count,
        budget_ceiling=inputs.budget_ceiling,
        recorded_at="",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_sub_ranges(
    *,
    coverage_from_block: int,
    coverage_to_block: int,
    per_call_capability: int,
) -> tuple[PlannedSubRange, ...]:
    """Split the inclusive window into sub-ranges bounded by ``per_call_capability``.

    The contract requires that the runner splits log queries that
    are wider than the endpoint's measured per-call capability
    rather than widening a single request. The function emits a
    minimal ordered list of ``PlannedSubRange`` values whose widths
    are in ``[1, per_call_capability]``; the union is the inclusive
    window. A single-block window emits one sub-range.
    """
    if per_call_capability <= 0:
        raise ValueError(
            f"_split_sub_ranges: per_call_capability must be > 0, got {per_call_capability}"
        )
    if coverage_from_block > coverage_to_block:
        return ()
    sub_ranges: list[PlannedSubRange] = []
    cursor = coverage_from_block
    while cursor <= coverage_to_block:
        upper = min(cursor + per_call_capability - 1, coverage_to_block)
        sub_ranges.append(PlannedSubRange(from_block=cursor, to_block=upper))
        cursor = upper + 1
    return tuple(sub_ranges)


def parse_finalized_pin_payloads(
    *,
    primary_block_number: int | None,
    primary_block_hash: str | None,
    secondary_block_number: int | None,
    secondary_block_hash: str | None,
) -> tuple[PerPoolWindowInputs | None, str]:
    """Validate two endpoint finalized readings and return the agreed pin inputs.

    The helper is the per-pool planner's adapter to the T038
    finalized-pin agreement machinery. It enforces the documented
    rejection rules (``latest``, non-finalized, wall-clock) at
    construction time: any endpoint returning ``None`` (no
    ``finalized`` tag), an unexpected hash shape, or a block number
    that disagrees with the other endpoint's reading is surfaced as
    a reason code the planner records.

    Returns ``(inputs, REASON_OK)`` on agreement; returns
    ``(None, reason_code)`` otherwise, where ``reason_code`` is one
    of :data:`REASON_FINALIZED_UNAVAILABLE`,
    :data:`REASON_FINALIZED_DISAGREEMENT`, or
    :data:`REASON_PROVIDER_RETURNED_NON_FINALIZED`.

    The function never returns a ``latest`` block, a non-finalized
    block, or a wall-clock-derived bound; callers must not retry
    with a fallback.
    """
    if (
        primary_block_number is None
        or primary_block_hash is None
        or secondary_block_number is None
        or secondary_block_hash is None
    ):
        return (None, REASON_FINALIZED_UNAVAILABLE)
    if not isinstance(primary_block_hash, str) or not isinstance(secondary_block_hash, str):
        return (None, REASON_FINALIZED_DISAGREEMENT)
    p_hash = primary_block_hash.strip().lower()
    s_hash = secondary_block_hash.strip().lower()
    if not p_hash.startswith("0x") or len(p_hash) != 2 + 2 * 32:
        return (None, REASON_PROVIDER_RETURNED_NON_FINALIZED)
    if not s_hash.startswith("0x") or len(s_hash) != 2 + 2 * 32:
        return (None, REASON_PROVIDER_RETURNED_NON_FINALIZED)
    if primary_block_number < 0 or secondary_block_number < 0:
        return (None, REASON_NON_FINALIZED_BLOCK_REJECTED)
    if primary_block_number != secondary_block_number:
        return (None, REASON_FINALIZED_DISAGREEMENT)
    if p_hash != s_hash:
        return (None, REASON_FINALIZED_DISAGREEMENT)
    return (
        PerPoolWindowInputs(
            pool_key_id="<uninitialized>",
            pool_init_block=0,
            pool_init_block_hash="0x" + "00" * 32,
            window_pin_block_number=primary_block_number,
            window_pin_block_hash=p_hash,
        ),
        REASON_OK,
    )


def assert_logical_call_count_equals_sub_ranges_plus_headers(
    plan: PerPoolWindowPlan,
    *,
    distinct_event_block_count: int,
) -> None:
    """Assert the runner's logical-call count invariant after a run.

    The invariant T039 requires: ``logical_calls ==
    len(plan.sub_ranges) + distinct_event_block_count`` (one
    ``eth_getLogs`` per sub-range plus one deduplicated
    ``eth_getBlockByNumber`` per distinct event block). The
    contract forbids a per-block header fetch for blocks without a
    pool event, so a runner that records more logical calls than
    this invariant allows is failing the T039 acquisition shape.

    The function raises :class:`LogicalCallCountInvariantError`
    with the diagnostic detail the audit trail records.
    """
    expected = len(plan.sub_ranges) + distinct_event_block_count
    if distinct_event_block_count < 0:
        raise LogicalCallCountInvariantError(
            f"distinct_event_block_count must be >= 0, got {distinct_event_block_count}"
        )
    # The expected logical-call count includes one getLogs call per
    # sub-range plus one getBlockByNumber call per distinct event
    # block. When the run resumed from a checkpoint the runner
    # records only the suffix; the planner's
    # ``expected_logical_call_count`` already accounts for that
    # suffix.
    if plan.expected_logical_call_count != expected:
        raise LogicalCallCountInvariantError(
            f"plan.expected_logical_call_count {plan.expected_logical_call_count} "
            f"!= len(plan.sub_ranges) {len(plan.sub_ranges)} + "
            f"distinct_event_block_count {distinct_event_block_count}"
        )


class LogicalCallCountInvariantError(RuntimeError):
    """The runner's logical-call count violated the T039 invariant.

    The contract requires that the runner issue exactly one
    ``eth_getLogs`` per sub-range plus one deduplicated
    ``eth_getBlockByNumber`` per distinct event block. A divergence
    means the runner is reading the chain in a shape T039 forbids
    (one log call per block, one header per scanned block, etc.)
    and the run must halt with the named reason code rather than
    claim coverage.
    """


def per_pool_window_summary(plan: PerPoolWindowPlan) -> Mapping[str, Any]:
    """Return a compact summary of the per-pool plan for the run record.

    The summary is the audit-trail row the runner records next to
    the per-pool coverage report. It contains every pinned fact the
    T039 contract requires: the pinned finalized end, the pool's
    own ``Initialize`` block, the per-call capability split, the
    deduplicated header count, the expected logical-call count and
    the budget ceiling.
    """
    return {
        "pool_key_id": plan.pool_key_id,
        "chain_id": plan.chain_id,
        "pool_init_block": plan.pool_init_block,
        "pool_init_block_hash": plan.pool_init_block_hash,
        "window_pin_block_number": plan.window_pin_block_number,
        "window_pin_block_hash": plan.window_pin_block_hash,
        "coverage_from_block": plan.coverage_from_block,
        "coverage_to_block": plan.coverage_to_block,
        "window_width_blocks": plan.window_width_blocks,
        "outcome": plan.outcome,
        "reason_code": plan.reason_code,
        "sub_range_count": len(plan.sub_ranges),
        "deduplicated_header_block_count": plan.deduplicated_header_block_count,
        "expected_logical_call_count": plan.expected_logical_call_count,
        "primary_max_blocks_per_call": plan.primary_max_blocks_per_call,
        "secondary_max_blocks_per_call": plan.secondary_max_blocks_per_call,
    }


__all__ = [
    "BudgetCeiling",
    "DEFAULT_ELAPSED_MS_BUDGET",
    "DEFAULT_HTTP_REQUEST_BUDGET",
    "DEFAULT_LOGICAL_CALL_BUDGET",
    "DEFAULT_PROVIDER_UNIT_BUDGET",
    "DEFAULT_RESPONSE_BYTES_BUDGET",
    "DOCUMENTED_PER_POOL_REASON_CODES",
    "LogicalCallCountInvariantError",
    "OUTCOME_POOL_ACQUIRED",
    "OUTCOME_POOL_ACQUIRED_AFTER_RESUME",
    "OUTCOME_POOL_INITIALIZED_AFTER_END",
    "OUTCOME_POOL_RESOLUTION_FAILED",
    "PER_POOL_CHAIN_ID",
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
    "assert_logical_call_count_equals_sub_ranges_plus_headers",
    "parse_finalized_pin_payloads",
    "per_pool_window_summary",
    "plan_per_pool_window",
    "plan_per_pool_window_after_resume",
]
