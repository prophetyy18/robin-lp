"""Baseline comparison for the pinned reference dataset (T036).

The 2026-09-17 baseline counts (recorded in
:mod:`robinhood_lp.qualification.reference`) are a measured
comparison target, never evidence in place of the run's own counts.
The qualification pipeline compares the dataset's own per-event-type
counts and distinct event-block count against the baseline and
surfaces any difference as a discrepancy rather than reconciling it.

The check produces a :class:`BaselineComparison` record that names
each per-event-type observation, the baseline value, the absolute
delta, and a boolean flag indicating agreement. A single
disagreement halts qualification (``baseline_discrepancy`` finding);
no tolerance, no reconciliation, no silent override. The sum of
per-event-type counts is checked against the recorded
``baseline_total_events`` so a missing-count event type surfaces as
a discrepancy instead of being silently absorbed.

The ``distinct_block_count`` check uses the T034 reason code
``range_coverage_gap`` when the run's distinct-block count disagrees
with the baseline, because a missing block would otherwise look like
a coverage gap rather than a count mismatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from robinhood_lp.qualification.reference import (
    BASELINE_DISTINCT_BLOCKS,
    BASELINE_DONATE_COUNT,
    BASELINE_INITIALIZE_COUNT,
    BASELINE_MODIFY_LIQUIDITY_COUNT,
    BASELINE_PROTOCOL_FEE_UPDATED_COUNT,
    BASELINE_SWAP_COUNT,
    BASELINE_TOTAL_EVENTS,
    ReferenceTarget,
)

#: The set of V4 event names the qualification pipeline counts.
#: Adding a new event type here requires a matching entry in the
#: reference target's ``per_event_type_baseline`` mapping.
QUALIFIED_EVENT_NAMES: tuple[str, ...] = (
    "Initialize",
    "ModifyLiquidity",
    "Swap",
    "ProtocolFeeUpdated",
    "Donate",
)


@dataclass(frozen=True, slots=True)
class BaselineComparison:
    """The baseline comparison outcome for one qualification run.

    Every per-event-type row carries its observed count, the
    baseline value, and an ``agrees`` flag. ``distinct_block_count``
    carries the observed distinct event-block count and its
    baseline. ``agrees_overall`` is ``True`` iff every row agrees.
    """

    per_event_type: dict[str, BaselineRow]
    distinct_block_count: int
    baseline_distinct_block_count: int
    total_events: int
    baseline_total_events: int
    agrees_overall: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_event_type": {
                name: {
                    "observed": row.observed,
                    "baseline": row.baseline,
                    "agrees": row.agrees,
                    "delta": row.delta,
                }
                for name, row in self.per_event_type.items()
            },
            "distinct_block_count": self.distinct_block_count,
            "baseline_distinct_block_count": self.baseline_distinct_block_count,
            "distinct_blocks_agree": (
                self.distinct_block_count == self.baseline_distinct_block_count
            ),
            "total_events": self.total_events,
            "baseline_total_events": self.baseline_total_events,
            "agrees_overall": self.agrees_overall,
        }


@dataclass(frozen=True, slots=True)
class BaselineRow:
    """One per-event-type row of the baseline comparison."""

    event_name: str
    observed: int
    baseline: int
    agrees: bool
    delta: int

    @property
    def has_discrepancy(self) -> bool:
        """Convenience: True iff the row disagrees with the baseline."""
        return not self.agrees


@dataclass(frozen=True, slots=True)
class _ObservedCounts:
    """The observed per-event-type and distinct-block counts.

    The qualification pipeline sums ``per_event_type.values()`` to
    derive the ``total_events`` so a missing-count event type surfaces
    as a discrepancy instead of being silently absorbed into the
    total. The ``distinct_blocks`` field carries the count of
    distinct block numbers across the event set.
    """

    per_event_type: dict[str, int] = field(default_factory=dict)
    distinct_blocks: int = 0


def compare_against_baseline(
    observed_per_event_type: dict[str, int],
    observed_distinct_block_count: int,
    *,
    reference: ReferenceTarget | None = None,
) -> BaselineComparison:
    """Compare observed counts against the reference target's baseline.

    Parameters
    ----------
    observed_per_event_type:
        Mapping from V4 event name (``Initialize``,
        ``ModifyLiquidity``, ``Swap``, ``ProtocolFeeUpdated``,
        ``Donate``) to the observed event count.
    observed_distinct_block_count:
        The count of distinct block numbers observed across the
        event set; the qualifier verifies this equals the
        baseline's ``baseline_distinct_blocks`` value.
    reference:
        The pinned reference target. When ``None``,
        :data:`REFERENCE_TARGET` is used.

    Raises
    ------
    ValueError:
        When ``observed_per_event_type`` is missing a key that is
        present in the baseline. The run cannot be silently
        compared when an event type was not observed at all.
    """
    ref = reference if reference is not None else ReferenceTarget()
    baseline_per_event_type = ref.per_event_type_baseline()
    # Refuse to silently compare when an event type is missing from
    # the observation dict: surface a discrepancy rather than
    # treating the missing key as ``0`` observed.
    missing = set(baseline_per_event_type) - set(observed_per_event_type)
    if missing:
        raise ValueError(
            "compare_against_baseline: observed_per_event_type missing keys "
            f"{sorted(missing)}; cannot silently treat missing keys as 0 observed"
        )
    per_event_type_rows: dict[str, BaselineRow] = {}
    overall = True
    total_observed = 0
    for event_name in QUALIFIED_EVENT_NAMES:
        observed = int(observed_per_event_type[event_name])
        baseline = int(baseline_per_event_type[event_name])
        delta = observed - baseline
        agrees = observed == baseline
        per_event_type_rows[event_name] = BaselineRow(
            event_name=event_name,
            observed=observed,
            baseline=baseline,
            agrees=agrees,
            delta=delta,
        )
        if not agrees:
            overall = False
        total_observed += observed
    distinct_blocks_agrees = int(observed_distinct_block_count) == int(ref.baseline_distinct_blocks)
    if not distinct_blocks_agrees:
        overall = False
    return BaselineComparison(
        per_event_type=per_event_type_rows,
        distinct_block_count=int(observed_distinct_block_count),
        baseline_distinct_block_count=int(ref.baseline_distinct_blocks),
        total_events=total_observed,
        baseline_total_events=int(ref.baseline_total_events),
        agrees_overall=overall,
    )


def observed_counts_from_event_set(events: Any) -> _ObservedCounts:
    """Build :class:`_ObservedCounts` from an iterable of typed
    V4 log records (or anything with a ``.sort_key()`` /
    ``.event_key()`` pair).

    Each record's event name is inferred from its concrete type:
    ``Initialize`` / ``ModifyLiquidity`` / ``Swap`` / ``Donate`` /
    ``ProtocolFeeUpdated`` records are counted into the matching
    bucket. Anything else surfaces as ``ValueError`` so the run
    does not silently lose an event type.

    The ``distinct_blocks`` count is the cardinality of the
    ``block_number`` set across the records, which is the
    T034 distinct-event-block count.
    """
    counts: dict[str, int] = {name: 0 for name in QUALIFIED_EVENT_NAMES}
    block_numbers: set[int] = set()
    for record in events:
        cls_name = type(record).__name__
        event_name = cls_name.removesuffix("LogRecord")
        if event_name not in counts:
            raise ValueError(
                f"observed_counts_from_event_set: unknown event record class {cls_name!r}"
            )
        counts[event_name] += 1
        block_number = getattr(record, "block_number", None)
        if not isinstance(block_number, int):
            raise ValueError(
                "observed_counts_from_event_set: record is missing integer block_number"
            )
        block_numbers.add(block_number)
    return _ObservedCounts(
        per_event_type=counts,
        distinct_blocks=len(block_numbers),
    )


def empty_observed_counts() -> _ObservedCounts:
    """Return an observed-counts row with every event type at 0.

    Useful for tests that exercise the no-events branch.
    """
    return _ObservedCounts(
        per_event_type={name: 0 for name in QUALIFIED_EVENT_NAMES},
        distinct_blocks=0,
    )


def make_baseline_consistent_observed_counts(
    *,
    distinct_blocks: int | None = None,
    initialize_count: int | None = None,
    modify_liquidity_count: int | None = None,
    swap_count: int | None = None,
    protocol_fee_updated_count: int | None = None,
    donate_count: int | None = None,
    reference: ReferenceTarget | None = None,
) -> _ObservedCounts:
    """Build an :class:`_ObservedCounts` row matching the baseline.

    The default values come from the reference target's baseline so
    callers can adjust a single dimension (e.g. ``Donate=2``) to
    exercise the discrepancy path. The ``distinct_blocks`` argument
    defaults to the baseline's distinct-block count; setting a
    different value exercises the range-coverage discrepancy path.
    """
    ref = reference if reference is not None else ReferenceTarget()
    return _ObservedCounts(
        per_event_type={
            "Initialize": (
                BASELINE_INITIALIZE_COUNT if initialize_count is None else initialize_count
            ),
            "ModifyLiquidity": (
                BASELINE_MODIFY_LIQUIDITY_COUNT
                if modify_liquidity_count is None
                else modify_liquidity_count
            ),
            "Swap": BASELINE_SWAP_COUNT if swap_count is None else swap_count,
            "ProtocolFeeUpdated": (
                BASELINE_PROTOCOL_FEE_UPDATED_COUNT
                if protocol_fee_updated_count is None
                else protocol_fee_updated_count
            ),
            "Donate": (BASELINE_DONATE_COUNT if donate_count is None else donate_count),
        },
        distinct_blocks=(
            int(ref.baseline_distinct_blocks) if distinct_blocks is None else distinct_blocks
        ),
    )


__all__ = [
    "BASELINE_DONATE_COUNT",
    "BASELINE_DISTINCT_BLOCKS",
    "BASELINE_INITIALIZE_COUNT",
    "BASELINE_MODIFY_LIQUIDITY_COUNT",
    "BASELINE_PROTOCOL_FEE_UPDATED_COUNT",
    "BASELINE_SWAP_COUNT",
    "BASELINE_TOTAL_EVENTS",
    "BaselineComparison",
    "BaselineRow",
    "QUALIFIED_EVENT_NAMES",
    "compare_against_baseline",
    "empty_observed_counts",
    "make_baseline_consistent_observed_counts",
    "observed_counts_from_event_set",
]
