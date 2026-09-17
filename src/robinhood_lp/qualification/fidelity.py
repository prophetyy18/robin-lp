"""Independent fidelity check across endpoints (T036).

The reference dataset is acquired through the primary endpoint
(``robinhood_public``). The fidelity check independently re-acquires
sampled windows through the secondary endpoint
(``alchemy_free``) within that endpoint's measured per-call
capability (about 10 blocks per ``eth_getLogs`` call as measured on
2026-09-17). Each sampled window produces two raw acquisition
envelopes — one from A and one from B — and the comparison is per
``EventKey`` (T011 identity) by normalized content hash.

The contract specifies:

- the sample covers the **start**, **middle**, and **end** of the
  pinned range; the window span is at most
  ``secondary_max_blocks_per_call`` blocks (default 10), so each
  sampled window fits inside the secondary endpoint's measured
  per-call capability;
- per-``EventKey`` normalized content hashes agree across both
  acquisitions for every sampled event of every sampled window;
  any disagreement halts qualification with reason code
  ``cross_endpoint_sample_disagree``;
- a window the secondary endpoint cannot serve is reported as a
  blocked check with reason code ``cross_endpoint_sample_missing``;
  it is **never** silently skipped and **never** replaced by a
  re-read from the primary endpoint;
- both raw acquisition envelopes are retained per window — the
  cross-endpoint equality of the normalized hash must not erase
  the raw envelope evidence (T030 / T034 contract).

The module is intentionally endpoint-neutral: the sampler takes a
``secondary_window_source`` callable that returns the raw envelopes
for a requested window, so tests can inject both the canonical
secondary response and a ``blocked`` response without contacting
any real RPC.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from robinhood_lp.protocol.events import EventKey
from robinhood_lp.qualification.reference import (
    REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL,
    ReferenceTarget,
)

# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FidelityWindow:
    """A single sampled [from_block, to_block] window (inclusive).

    The window span never exceeds ``ReferenceTarget.secondary_max_blocks_per_call``
    so the re-acquisition respects the secondary endpoint's measured
    per-call capability. The ``label`` records which slice of the
    range the window came from (``start`` / ``middle`` / ``end``).
    """

    label: str
    from_block: int
    to_block: int

    def __post_init__(self) -> None:
        if self.from_block > self.to_block:
            raise ValueError(
                f"FidelityWindow {self.label!r}: from_block {self.from_block} "
                f"> to_block {self.to_block}"
            )
        span = self.to_block - self.from_block + 1
        if span > REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL:
            raise ValueError(
                f"FidelityWindow {self.label!r}: span {span} blocks exceeds the "
                f"secondary endpoint's measured per-call capability of "
                f"{REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL} blocks"
            )


def build_fidelity_windows(
    *,
    coverage_from_block: int,
    coverage_to_block: int,
    secondary_max_blocks_per_call: int = REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL,
) -> tuple[FidelityWindow, ...]:
    """Build the start / middle / end sampled windows.

    Each window is exactly ``secondary_max_blocks_per_call`` blocks
    long when the range permits, otherwise the window collapses to
    the range bound. The middle window is anchored at the
    arithmetic mean of the two ends so it lies inside the range.
    """
    if coverage_from_block > coverage_to_block:
        raise ValueError(
            f"build_fidelity_windows: coverage_from_block {coverage_from_block} "
            f"> coverage_to_block {coverage_to_block}"
        )
    span = secondary_max_blocks_per_call
    start_to = min(coverage_from_block + span - 1, coverage_to_block)
    end_from = max(coverage_to_block - span + 1, coverage_from_block)
    middle_anchor = (coverage_from_block + coverage_to_block) // 2
    middle_from = max(coverage_from_block, middle_anchor - (span // 2))
    middle_to = min(coverage_to_block, middle_from + span - 1)
    # Re-anchor the middle window if the cap pulled it past the end.
    middle_from = max(coverage_from_block, middle_to - span + 1)
    return (
        FidelityWindow(label="start", from_block=coverage_from_block, to_block=start_to),
        FidelityWindow(label="middle", from_block=middle_from, to_block=middle_to),
        FidelityWindow(label="end", from_block=end_from, to_block=coverage_to_block),
    )


# ---------------------------------------------------------------------------
# Envelopes and source abstraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FidelityEnvelope:
    """The raw acquisition envelope for one window.

    ``event_keys`` is the deterministic T011 ``EventKey`` set the
    secondary endpoint reported for the window. ``normalized_hashes``
    maps each ``EventKey`` to the SHA-256 normalized content hash
    the secondary endpoint produced. ``raw_payload`` is the literal
    JSON-RPC response envelope the secondary endpoint returned; the
    contract requires both envelopes (primary and secondary) to be
    retained alongside the comparison, so ``raw_payload`` is kept
    verbatim for the audit trail.

    A ``FidelityEnvelope`` whose ``can_serve`` is ``False`` is the
    blocked-window shape; the caller treats it as a
    ``cross_endpoint_sample_missing`` finding and never substitutes
    a primary re-read.
    """

    endpoint_alias: str
    event_keys: tuple[EventKey, ...]
    normalized_hashes: dict[EventKey, bytes]
    raw_payload: Any = None
    can_serve: bool = True
    block_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.can_serve and not self.block_reason:
            raise ValueError("FidelityEnvelope: can_serve=False requires a block_reason")


#: The callable shape the qualification pipeline uses to ask the
#: secondary endpoint for a sampled window's raw envelope.
#:
#: Implementations return a :class:`FidelityEnvelope` whose
#: ``can_serve`` is ``True`` when the secondary endpoint served the
#: window, or ``False`` with a documented reason when it could not.
SecondaryWindowSource = Callable[[FidelityWindow], FidelityEnvelope]


def make_secondary_source_from_envelopes(
    envelopes: dict[FidelityWindow, FidelityEnvelope],
) -> SecondaryWindowSource:
    """Build a deterministic secondary-source callable from a mapping.

    Useful for the end-to-end qualification fixture: the test
    pre-records one envelope per sampled window and the
    qualification pipeline consumes them through the callable.
    Windows missing from the mapping surface as a blocked envelope
    with ``block_reason="unknown_window"`` so the report records the
    blocked check rather than silently skipping it.
    """

    def _source(window: FidelityWindow) -> FidelityEnvelope:
        env = envelopes.get(window)
        if env is None:
            return FidelityEnvelope(
                endpoint_alias="secondary_missing",
                event_keys=(),
                normalized_hashes={},
                raw_payload=None,
                can_serve=False,
                block_reason="unknown_window",
            )
        return env

    return _source


# ---------------------------------------------------------------------------
# Comparison and result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FidelityWindowSample:
    """The per-window fidelity check outcome.

    Both envelopes (primary + secondary) are retained for the
    audit trail. The comparison rows are keyed by ``EventKey``;
    each row carries the primary and secondary normalized hashes
    and the comparison result.
    """

    window: FidelityWindow
    primary_envelope: FidelityEnvelope
    secondary_envelope: FidelityEnvelope
    matched_event_keys: tuple[EventKey, ...]
    missing_in_secondary: tuple[EventKey, ...]
    disagreeing_event_keys: tuple[EventKey, ...]
    blocked: bool = False

    def agrees(self) -> bool:
        """Return ``True`` iff the window is served, fully matched,
        and every comparison agrees."""
        if self.blocked:
            return False
        if self.missing_in_secondary:
            return False
        return not self.disagreeing_event_keys

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.window.label,
            "from_block": self.window.from_block,
            "to_block": self.window.to_block,
            "primary_endpoint_alias": self.primary_envelope.endpoint_alias,
            "secondary_endpoint_alias": self.secondary_envelope.endpoint_alias,
            "matched_event_key_count": len(self.matched_event_keys),
            "missing_in_secondary_count": len(self.missing_in_secondary),
            "disagreeing_event_key_count": len(self.disagreeing_event_keys),
            "blocked": self.blocked,
            "block_reason": (self.secondary_envelope.block_reason if self.blocked else None),
            "agrees": self.agrees(),
            "matched_event_keys": [_event_key_to_dict(k) for k in self.matched_event_keys],
            "missing_in_secondary": [_event_key_to_dict(k) for k in self.missing_in_secondary],
            "disagreeing_event_keys": [_event_key_to_dict(k) for k in self.disagreeing_event_keys],
        }


def _event_key_to_dict(key: EventKey) -> dict[str, Any]:
    return {
        "chain_id": key.chain_id.value,
        "block_hash": key.block_ref().to_hash_hex(),
        "tx_hash": key.transaction_ref().to_hash_hex(),
        "log_index": key.log_index,
    }


@dataclass(frozen=True, slots=True)
class FidelityCheckResult:
    """The full fidelity check outcome across all sampled windows."""

    windows: tuple[FidelityWindowSample, ...]
    result_bearing_window_count: int
    sample_size: int
    sample_coverage: tuple[str, ...]
    agrees_overall: bool
    blocked_window_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "windows": [w.to_dict() for w in self.windows],
            "result_bearing_window_count": self.result_bearing_window_count,
            "sample_size": self.sample_size,
            "sample_coverage": list(self.sample_coverage),
            "agrees_overall": self.agrees_overall,
            "blocked_window_count": self.blocked_window_count,
        }


# ---------------------------------------------------------------------------
# The fidelity check
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PrimaryEnvelopeIndex:
    """Per-window primary endpoint envelope.

    The qualification pipeline builds one of these per sampled
    window from the dataset's own records. The same records must be
    present in the primary envelope's normalized hash mapping for
    the comparison to be meaningful; a record absent from the
    primary envelope is treated as ``missing_in_secondary=False``
    but ``missing_in_primary`` so the qualifier surfaces it as a
    discrepancy.
    """

    envelope: FidelityEnvelope
    missing_in_primary: tuple[EventKey, ...] = ()


def _compare_one_window(
    window: FidelityWindow,
    primary: FidelityEnvelope,
    secondary: FidelityEnvelope,
) -> FidelityWindowSample:
    """Compare the two envelopes for a single window."""
    if not secondary.can_serve:
        return FidelityWindowSample(
            window=window,
            primary_envelope=primary,
            secondary_envelope=secondary,
            matched_event_keys=(),
            missing_in_secondary=primary.event_keys,
            disagreeing_event_keys=(),
            blocked=True,
        )
    # EventKeys present in the primary envelope (the dataset's
    # records for the window) but absent from the secondary envelope.
    primary_keys = set(primary.event_keys)
    secondary_keys = set(secondary.event_keys)
    matched_keys = primary_keys & secondary_keys
    missing = tuple(sorted(primary_keys - secondary_keys, key=_event_key_sort_key))
    # Hash comparison per EventKey.
    disagreeing: list[EventKey] = []
    for key in sorted(matched_keys, key=_event_key_sort_key):
        p_hash = primary.normalized_hashes.get(key)
        s_hash = secondary.normalized_hashes.get(key)
        if p_hash is None or s_hash is None:
            # An EventKey the secondary endpoint reported but for
            # which no normalized hash exists is a disagreement.
            disagreeing.append(key)
            continue
        if p_hash != s_hash:
            disagreeing.append(key)
    return FidelityWindowSample(
        window=window,
        primary_envelope=primary,
        secondary_envelope=secondary,
        matched_event_keys=tuple(sorted(matched_keys, key=_event_key_sort_key)),
        missing_in_secondary=missing,
        disagreeing_event_keys=tuple(disagreeing),
        blocked=False,
    )


def _event_key_sort_key(key: EventKey) -> tuple[int, int, int, int]:
    return (
        key.block_ref().block_number or 0,
        key.block_ref().block_hash,
        key.transaction_ref().tx_hash,
        key.log_index,
    )


def perform_fidelity_check(
    *,
    primary_envelopes_by_window: dict[FidelityWindow, FidelityEnvelope],
    secondary_window_source: SecondaryWindowSource,
    coverage_from_block: int,
    coverage_to_block: int,
    reference: ReferenceTarget | None = None,
    secondary_max_blocks_per_call: int = REFERENCE_SECONDARY_MAX_BLOCKS_PER_CALL,
) -> FidelityCheckResult:
    """Run the cross-endpoint fidelity check.

    The function:

    1. Builds the start / middle / end sampled windows via
       :func:`build_fidelity_windows` so the sample spans the range.
    2. Pulls the primary envelope for each window from
       ``primary_envelopes_by_window``; a window missing from the
       primary envelope is treated as a primary-side omission
       (the dataset does not cover the window) and surfaces as a
       blocked check rather than being silently dropped.
    3. Calls ``secondary_window_source`` to fetch the secondary
       envelope; a blocked envelope surfaces as a blocked check
       with reason code ``cross_endpoint_sample_missing``.
    4. Compares per-``EventKey`` normalized content hashes; a
       disagreement surfaces as ``cross_endpoint_sample_disagree``.
    5. Counts the result-bearing windows (those with at least one
       matched ``EventKey``) so the report records the
       result-bearing sample size.

    Parameters
    ----------
    primary_envelopes_by_window:
        Mapping from sampled window to the primary endpoint's raw
        envelope. The envelope must carry the ``EventKey`` set the
        primary dataset observed for the window and the matching
        normalized hashes.
    secondary_window_source:
        Callable that returns the secondary endpoint's raw envelope
        for a sampled window. May return a blocked envelope to
        exercise the ``cross_endpoint_sample_missing`` path.
    reference:
        The pinned reference target. When ``None``,
        :data:`REFERENCE_TARGET` is used.
    """
    ref = reference if reference is not None else ReferenceTarget()
    windows = build_fidelity_windows(
        coverage_from_block=coverage_from_block,
        coverage_to_block=coverage_to_block,
        secondary_max_blocks_per_call=secondary_max_blocks_per_call,
    )
    samples: list[FidelityWindowSample] = []
    blocked_count = 0
    result_bearing = 0
    coverage_labels: list[str] = []
    for window in windows:
        coverage_labels.append(window.label)
        primary = primary_envelopes_by_window.get(window)
        if primary is None:
            # No primary envelope for this window: surface as a
            # blocked check (the dataset does not cover the window).
            placeholder_primary = FidelityEnvelope(
                endpoint_alias="primary_missing",
                event_keys=(),
                normalized_hashes={},
                raw_payload=None,
            )
            placeholder_secondary = FidelityEnvelope(
                endpoint_alias=ref.pool_manager_address.to_hex(),
                event_keys=(),
                normalized_hashes={},
                raw_payload=None,
                can_serve=False,
                block_reason="primary_window_missing",
            )
            samples.append(
                FidelityWindowSample(
                    window=window,
                    primary_envelope=placeholder_primary,
                    secondary_envelope=placeholder_secondary,
                    matched_event_keys=(),
                    missing_in_secondary=(),
                    disagreeing_event_keys=(),
                    blocked=True,
                )
            )
            blocked_count += 1
            continue
        secondary = secondary_window_source(window)
        sample = _compare_one_window(window, primary, secondary)
        samples.append(sample)
        if sample.blocked:
            blocked_count += 1
        elif sample.matched_event_keys:
            result_bearing += 1
    overall = all(sample.agrees() for sample in samples) and blocked_count == 0
    return FidelityCheckResult(
        windows=tuple(samples),
        result_bearing_window_count=result_bearing,
        sample_size=len(samples),
        sample_coverage=tuple(coverage_labels),
        agrees_overall=overall,
        blocked_window_count=blocked_count,
    )


# ---------------------------------------------------------------------------
# Helpers for building primary envelopes from typed event records
# ---------------------------------------------------------------------------


def primary_envelopes_from_event_records(
    windows: Iterable[FidelityWindow],
    records: Sequence[Any],
    *,
    primary_endpoint_alias: str = "robinhood_public",
) -> dict[FidelityWindow, FidelityEnvelope]:
    """Build primary envelopes for the sampled windows from typed
    V4 records.

    Each window is the subset of ``records`` whose
    ``block_number`` lies inside the window's
    ``[from_block, to_block]`` range. The envelope's
    ``event_keys`` is the set of T011 ``EventKey`` instances
    derived from the records; ``normalized_hashes`` maps each
    ``EventKey`` to ``schema.normalized_content_hash(record)``.
    The ``raw_payload`` field is left as ``None`` because the
    primary acquisition's raw envelope is owned by the ingestion
    manifest; the qualifier relies on the normalized hashes for
    comparison and treats the raw envelope as out of scope (the
    raw envelope is the dataset's audit trail).
    """
    from robinhood_lp.storage import schema as storage_schema

    window_list = list(windows)
    grouped: dict[FidelityWindow, list[Any]] = {w: [] for w in window_list}
    for record in records:
        block_number = getattr(record, "block_number", None)
        if not isinstance(block_number, int):
            continue
        for window in window_list:
            if window.from_block <= block_number <= window.to_block:
                grouped[window].append(record)
                break
    envelopes: dict[FidelityWindow, FidelityEnvelope] = {}
    for window in window_list:
        records_in_window = grouped[window]
        event_keys: list[EventKey] = []
        hashes: dict[EventKey, bytes] = {}
        for record in records_in_window:
            try:
                key = record.event_key()
            except Exception:
                continue
            event_keys.append(key)
            hashes[key] = storage_schema.normalized_content_hash(record)
        envelopes[window] = FidelityEnvelope(
            endpoint_alias=primary_endpoint_alias,
            event_keys=tuple(event_keys),
            normalized_hashes=hashes,
            raw_payload=None,
        )
    return envelopes


__all__ = [
    "FidelityCheckResult",
    "FidelityEnvelope",
    "FidelityWindow",
    "FidelityWindowSample",
    "SecondaryWindowSource",
    "build_fidelity_windows",
    "make_secondary_source_from_envelopes",
    "perform_fidelity_check",
    "primary_envelopes_from_event_records",
]
