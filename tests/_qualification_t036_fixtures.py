"""Shared fixtures for the T036 reference-dataset qualification tests.

The fixtures build:

- a deterministic typed-event stream whose per-event-type counts and
  distinct-block count equal the 2026-09-17 baseline
  (:data:`robinhood_lp.qualification.reference.BASELINE_*`); the
  stream is split into ``start`` / ``middle`` / ``end`` windows so
  the fidelity sampler can re-acquire sampled windows on the
  secondary endpoint;
- a deterministic secondary-window source that agrees with the
  primary stream on every ``EventKey`` normalized content hash;
- a deterministic block-pinned ``StateView`` callable that serves
  the canonical golden values at the pinned block tag, plus a
  blocked variant for the primary endpoint that cannot serve
  historical state at depth.

The fixtures are **synthetic-but-faithful**: every record is a
typed V4 log record decoded from the pinned ABI, every
``normalized_content_hash`` matches the storage schema's
SHA-256 derivation, and every ``EventKey`` follows the T011
identity rule. The fixtures do **not** contact any real RPC.

Tests are intentionally narrow: every test exercises a single
qualification check or a single failure path so a regression
points at the failing clause without ambiguity.
"""

from __future__ import annotations

import hashlib
from typing import Any

from robinhood_lp.protocol.ids import (
    Address,
    ChainId,
    PoolId,
    PoolKey,
)
from robinhood_lp.qualification.fidelity import (
    FidelityEnvelope,
    FidelityWindow,
    SecondaryWindowSource,
    build_fidelity_windows,
    make_secondary_source_from_envelopes,
    primary_envelopes_from_event_records,
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
    REFERENCE_TARGET,
    build_reference_pool_id,
    build_reference_pool_key,
)
from robinhood_lp.storage.schema import (
    AcquisitionProvenance,
    DonateLogRecord,
    InitializeLogRecord,
    ModifyLiquidityLogRecord,
    ProtocolFeeUpdatedLogRecord,
    SwapLogRecord,
)

# ---------------------------------------------------------------------------
# Synthetic-but-faithful event stream
# ---------------------------------------------------------------------------


def _make_initialize_record(*, block_number: int, log_index: int = 0) -> InitializeLogRecord:
    """Build one canonical ``Initialize`` record."""
    return InitializeLogRecord(
        chain_id=ChainId(REFERENCE_CHAIN_ID),
        pool_id=PoolId.from_hex(REFERENCE_POOL_ID_HEX),
        block_number=block_number,
        block_hash=_block_hash_for(block_number),
        transaction_hash=_tx_hash_for(block_number, log_index, 0),
        transaction_index=0,
        log_index=log_index,
        address=REFERENCE_TARGET.pool_manager_address,
        block_timestamp=1_700_000_000 + block_number,
        parent_hash=_parent_hash_for(block_number),
    )


def _make_modify_liquidity_record(
    *, block_number: int, log_index: int, sequence: int
) -> ModifyLiquidityLogRecord:
    """Build one canonical ``ModifyLiquidity`` record."""
    sender_int = (0x1000 + sequence) & ((1 << 160) - 1)
    sender = Address.from_hex("0x" + format(sender_int, "040x"))
    return ModifyLiquidityLogRecord(
        chain_id=ChainId(REFERENCE_CHAIN_ID),
        pool_id=PoolId.from_hex(REFERENCE_POOL_ID_HEX),
        block_number=block_number,
        block_hash=_block_hash_for(block_number),
        transaction_hash=_tx_hash_for(block_number, log_index, 0),
        transaction_index=0,
        log_index=log_index,
        address=REFERENCE_TARGET.pool_manager_address,
        sender=sender,
        tick_lower=-280 * 10,
        tick_upper=280 * 10,
        liquidity_delta=1_000_000 + sequence,
        salt=sequence,
        block_timestamp=1_700_000_000 + block_number,
        parent_hash=_parent_hash_for(block_number),
    )


def _make_swap_record(*, block_number: int, log_index: int, sequence: int) -> SwapLogRecord:
    """Build one canonical ``Swap`` record."""
    sender_int = (0x2000 + sequence) & ((1 << 160) - 1)
    sender = Address.from_hex("0x" + format(sender_int, "040x"))
    return SwapLogRecord(
        chain_id=ChainId(REFERENCE_CHAIN_ID),
        pool_id=PoolId.from_hex(REFERENCE_POOL_ID_HEX),
        block_number=block_number,
        block_hash=_block_hash_for(block_number),
        transaction_hash=_tx_hash_for(block_number, log_index, 0),
        transaction_index=0,
        log_index=log_index,
        address=REFERENCE_TARGET.pool_manager_address,
        sender=sender,
        amount0=-(1_000 + sequence),
        amount1=2_000 + sequence,
        sqrt_price_x96=79228162514264337593543950336,
        liquidity=10_000_000 + sequence,
        tick=0,
        fee=28001,
        block_timestamp=1_700_000_000 + block_number,
        parent_hash=_parent_hash_for(block_number),
    )


def _make_donate_record(*, block_number: int, log_index: int, sequence: int) -> DonateLogRecord:
    """Build one canonical ``Donate`` record."""
    sender_int = (0x3000 + sequence) & ((1 << 160) - 1)
    sender = Address.from_hex("0x" + format(sender_int, "040x"))
    return DonateLogRecord(
        chain_id=ChainId(REFERENCE_CHAIN_ID),
        pool_id=PoolId.from_hex(REFERENCE_POOL_ID_HEX),
        block_number=block_number,
        block_hash=_block_hash_for(block_number),
        transaction_hash=_tx_hash_for(block_number, log_index, 0),
        transaction_index=0,
        log_index=log_index,
        address=REFERENCE_TARGET.pool_manager_address,
        sender=sender,
        amount0=100 + sequence,
        amount1=200 + sequence,
        block_timestamp=1_700_000_000 + block_number,
        parent_hash=_parent_hash_for(block_number),
    )


def _make_protocol_fee_record(*, block_number: int, log_index: int) -> ProtocolFeeUpdatedLogRecord:
    """Build one canonical ``ProtocolFeeUpdated`` record."""
    return ProtocolFeeUpdatedLogRecord(
        chain_id=ChainId(REFERENCE_CHAIN_ID),
        pool_id=PoolId.from_hex(REFERENCE_POOL_ID_HEX),
        block_number=block_number,
        block_hash=_block_hash_for(block_number),
        transaction_hash=_tx_hash_for(block_number, log_index, 0),
        transaction_index=0,
        log_index=log_index,
        address=REFERENCE_TARGET.pool_manager_address,
        protocol_fee=0,
        block_timestamp=1_700_000_000 + block_number,
        parent_hash=_parent_hash_for(block_number),
    )


# Deterministic per-block hash helpers. The hash bytes are the
# SHA-256 of ``b"block-{n}"`` truncated to 32 bytes; this keeps the
# fixture independent of any real chain. The same helper is used
# for ``parent_hash`` (``b"parent-of-{n}"``) so reorg / contiguity
# tests can refer to a stable mapping.
def _block_hash_for(block_number: int) -> int:
    digest = hashlib.sha256(f"block-{block_number}".encode()).digest()
    return int.from_bytes(digest, "big")


def _parent_hash_for(block_number: int) -> int:
    digest = hashlib.sha256(f"parent-of-{block_number}".encode()).digest()
    return int.from_bytes(digest, "big")


def _tx_hash_for(block_number: int, log_index: int, sequence: int) -> int:
    digest = hashlib.sha256(f"tx-{block_number}-{log_index}-{sequence}".encode()).digest()
    return int.from_bytes(digest, "big")


# ---------------------------------------------------------------------------
# Baseline-consistent event stream
# ---------------------------------------------------------------------------


def _spread_block_numbers(*, count: int, coverage_from: int, coverage_to: int) -> list[int]:
    """Spread ``count`` distinct block numbers evenly across
    ``[coverage_from, coverage_to]``.

    The spread is deterministic so the fixture reproduces the
    exact same ``EventKey`` set across runs. The coverage range is
    inclusive on both ends.
    """
    if count <= 0:
        return []
    span = coverage_to - coverage_from
    if count == 1:
        return [coverage_from]
    if span < count - 1:
        # Range smaller than the requested count: still produce
        # ``count`` distinct numbers by collapsing neighbours.
        return [coverage_from + (i * span) // (count - 1) for i in range(count)]
    step = max(1, span // (count - 1))
    result = [coverage_from + i * step for i in range(count)]
    # Force the last element to the coverage_to_block so the
    # distinct-block count spans the full range.
    result[-1] = coverage_to
    return result


def build_baseline_consistent_event_stream() -> list[Any]:
    """Build the synthetic-but-faithful V4 event stream whose
    per-event-type and distinct-block counts equal the
    2026-09-17 baseline.

    The stream is deterministic: re-running the function returns
    the same ``EventKey`` set with the same ``normalized_content_hash``
    mapping.

    The baseline is ``Initialize=1``, ``ModifyLiquidity=578``,
    ``Swap=3159``, ``ProtocolFeeUpdated=1``, ``Donate=0`` —
    total ``3739`` events across ``3266`` distinct blocks. The
    distinct-block count is below the event count, so the stream
    shares some blocks between ModifyLiquidity and Swap events.

    The layout is deterministic: the 3266 distinct blocks are
    spread evenly across the inclusive reference range; the first
    block carries the ``Initialize`` event; the last block carries
    the ``ProtocolFeeUpdated`` event; the remaining 3264 blocks
    each carry one or two events:

    - ``473`` blocks carry both a ModifyLiquidity and a Swap event
      (the shared blocks);
    - ``105`` blocks carry only a ModifyLiquidity event (the
      modify-only blocks);
    - ``2686`` blocks carry only a Swap event (the swap-only
      blocks).

    Sums: ``473 + 105 + 2686 + 1 + 1 = 3266`` distinct blocks;
    ``473 + 105 = 578`` ModifyLiquidity events;
    ``473 + 2686 = 3159`` Swap events; total ``1 + 578 + 3159 + 1 = 3739``
    events.
    """
    coverage_from = REFERENCE_COVERAGE_FROM_BLOCK
    coverage_to = REFERENCE_COVERAGE_TO_BLOCK
    shared_count = (
        BASELINE_INITIALIZE_COUNT
        + BASELINE_MODIFY_LIQUIDITY_COUNT
        + BASELINE_SWAP_COUNT
        + BASELINE_PROTOCOL_FEE_UPDATED_COUNT
        - BASELINE_DISTINCT_BLOCKS
    )
    modify_only_count = BASELINE_MODIFY_LIQUIDITY_COUNT - shared_count
    swap_only_count = BASELINE_SWAP_COUNT - shared_count
    assert shared_count + modify_only_count + swap_only_count == (
        BASELINE_DISTINCT_BLOCKS - BASELINE_INITIALIZE_COUNT - BASELINE_PROTOCOL_FEE_UPDATED_COUNT
    )
    # Spread ``BASELINE_DISTINCT_BLOCKS - 2`` non-reserved blocks
    # across ``[coverage_from + 1, coverage_to - 1]`` (inclusive),
    # so the first block carries Initialize and the last carries
    # ProtocolFeeUpdated. The non-reserved blocks are partitioned
    # deterministically: the first ``shared_count`` blocks are
    # shared, the next ``modify_only_count`` are modify-only, the
    # remaining ``swap_only_count`` are swap-only.
    non_reserved_total = (
        BASELINE_DISTINCT_BLOCKS - BASELINE_INITIALIZE_COUNT - BASELINE_PROTOCOL_FEE_UPDATED_COUNT
    )
    inner_blocks = _spread_block_numbers(
        count=non_reserved_total,
        coverage_from=coverage_from + 1,
        coverage_to=coverage_to - 1,
    )
    shared_blocks = inner_blocks[:shared_count]
    modify_only_blocks = inner_blocks[shared_count : shared_count + modify_only_count]
    swap_only_blocks = inner_blocks[shared_count + modify_only_count :]
    records: list[Any] = []
    # Initialize: one event at the pool init block (the lower bound).
    records.append(_make_initialize_record(block_number=REFERENCE_POOL_INIT_BLOCK))
    # ModifyLiquidity on shared blocks.
    for sequence, block_number in enumerate(shared_blocks):
        records.append(
            _make_modify_liquidity_record(
                block_number=block_number, log_index=sequence, sequence=sequence
            )
        )
    # Swap on shared blocks (uses a different log_index from the
    # ModifyLiquidity entry on the same block).
    for sequence, block_number in enumerate(shared_blocks):
        records.append(
            _make_swap_record(
                block_number=block_number,
                log_index=BASELINE_MODIFY_LIQUIDITY_COUNT + sequence,
                sequence=sequence,
            )
        )
    # ModifyLiquidity on modify-only blocks.
    for sequence, block_number in enumerate(modify_only_blocks):
        records.append(
            _make_modify_liquidity_record(
                block_number=block_number,
                log_index=shared_count + sequence,
                sequence=shared_count + sequence,
            )
        )
    # Swap on swap-only blocks.
    for sequence, block_number in enumerate(swap_only_blocks):
        records.append(
            _make_swap_record(
                block_number=block_number,
                log_index=shared_count + modify_only_count + sequence,
                sequence=shared_count + sequence,
            )
        )
    # ProtocolFeeUpdated: one event at the very last block of the range.
    records.append(_make_protocol_fee_record(block_number=REFERENCE_COVERAGE_TO_BLOCK, log_index=0))
    return records


def _observed_counts_from_records(records: list[Any]) -> dict[str, int]:
    """Compute the per-event-type observed counts from a record set.

    Helper for tests that need to assert the synthetic-but-faithful
    stream's per-event-type counts equal the baseline.
    """
    counts: dict[str, int] = {
        "Initialize": 0,
        "ModifyLiquidity": 0,
        "Swap": 0,
        "ProtocolFeeUpdated": 0,
        "Donate": 0,
    }
    for record in records:
        cls_name = type(record).__name__
        event_name = cls_name.removesuffix("LogRecord")
        if event_name not in counts:
            raise ValueError(f"unexpected record class {cls_name!r}")
        counts[event_name] += 1
    return counts


def distinct_block_count_from_records(records: list[Any]) -> int:
    """Return the cardinality of the distinct-block set across the
    records."""
    return len({int(record.block_number) for record in records})


def assert_baseline_consistent(records: list[Any]) -> None:
    """Assert the synthetic-but-faithful stream's per-event-type
    and distinct-block counts equal the baseline.

    Tests that exercise the end-to-end qualification fixture call
    this helper before invoking the report builder.
    """
    counts = _observed_counts_from_records(records)
    assert counts["Initialize"] == BASELINE_INITIALIZE_COUNT
    assert counts["ModifyLiquidity"] == BASELINE_MODIFY_LIQUIDITY_COUNT
    assert counts["Swap"] == BASELINE_SWAP_COUNT
    assert counts["ProtocolFeeUpdated"] == BASELINE_PROTOCOL_FEE_UPDATED_COUNT
    assert counts["Donate"] == BASELINE_DONATE_COUNT
    assert sum(counts.values()) == BASELINE_TOTAL_EVENTS
    assert distinct_block_count_from_records(records) == BASELINE_DISTINCT_BLOCKS


# ---------------------------------------------------------------------------
# Primary envelopes and agreeing secondary source
# ---------------------------------------------------------------------------


def build_primary_envelopes_by_window(
    records: list[Any],
    windows: tuple[FidelityWindow, ...] | None = None,
    *,
    primary_endpoint_alias: str = "robinhood_public",
) -> dict[FidelityWindow, FidelityEnvelope]:
    """Build primary envelopes for the sampled windows.

    When ``windows`` is ``None``, the function builds the start /
    middle / end sampled windows via
    :func:`build_fidelity_windows`. The envelope's
    ``normalized_hashes`` map matches the storage schema's
    SHA-256 derivation, so the secondary endpoint's identical
    payload produces an exact match.
    """
    if windows is None:
        windows = build_fidelity_windows(
            coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
            coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
        )
    return primary_envelopes_from_event_records(
        windows=windows,
        records=records,
        primary_endpoint_alias=primary_endpoint_alias,
    )


def build_agreeing_secondary_source(
    primary_envelopes_by_window: dict[FidelityWindow, FidelityEnvelope],
) -> SecondaryWindowSource:
    """Build a secondary window source that agrees with the
    primary envelope on every ``EventKey`` normalized content hash.

    The test injects this source via the qualification inputs'
    ``secondary_window_source`` field; the resulting
    :class:`FidelityCheckResult` carries
    ``agrees_overall=True`` and ``blocked_window_count=0``.
    """
    envelopes: dict[FidelityWindow, FidelityEnvelope] = {}
    for window, primary in primary_envelopes_by_window.items():
        envelopes[window] = FidelityEnvelope(
            endpoint_alias="alchemy_free",
            event_keys=primary.event_keys,
            normalized_hashes=dict(primary.normalized_hashes),
            raw_payload={"window": window.label, "rows": list(primary.event_keys)},
        )
    return make_secondary_source_from_envelopes(envelopes)


def build_blocked_secondary_source(
    window: FidelityWindow, *, reason: str = "depth_unavailable"
) -> SecondaryWindowSource:
    """Build a secondary source that reports the named window as
    blocked.

    Useful for the failure-path test that exercises the
    ``cross_endpoint_sample_missing`` reason code on the fidelity
    check. The returned source returns a blocked envelope for the
    named window and an agreeing envelope for every other window
    in the standard start / middle / end set.
    """
    standard = build_fidelity_windows(
        coverage_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        coverage_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )
    envelopes: dict[FidelityWindow, FidelityEnvelope] = {}
    for w in standard:
        if w == window:
            envelopes[w] = FidelityEnvelope(
                endpoint_alias="alchemy_free",
                event_keys=(),
                normalized_hashes={},
                raw_payload=None,
                can_serve=False,
                block_reason=reason,
            )
        else:
            envelopes[w] = FidelityEnvelope(
                endpoint_alias="alchemy_free",
                event_keys=(),
                normalized_hashes={},
                raw_payload={"empty_window": w.label},
            )
    return make_secondary_source_from_envelopes(envelopes)


# ---------------------------------------------------------------------------
# Block-pinned StateView callables
# ---------------------------------------------------------------------------


def build_state_call_secondary_serving(
    *,
    block_tag: str = "0x" + format(REFERENCE_COVERAGE_TO_BLOCK, "x"),
) -> Any:
    """Build a block-pinned StateView callable that the secondary
    endpoint uses to serve the canonical golden values.

    Returns a 32-byte ``getSlot0`` payload and a 32-byte
    ``getLiquidity`` payload. The alias is
    ``alchemy_free``.
    """
    from robinhood_lp.qualification.state_spot_check import (
        STATE_VIEW_METHOD_GET_LIQUIDITY,
        STATE_VIEW_METHOD_GET_SLOT_0,
        make_block_pinned_state_call,
    )

    slot0_payload = b"\x01" * 32  # arbitrary, deterministic
    liquidity_payload = b"\x02" * 32  # arbitrary, deterministic
    responses = {
        (STATE_VIEW_METHOD_GET_SLOT_0, block_tag, "alchemy_free"): slot0_payload,
        (STATE_VIEW_METHOD_GET_LIQUIDITY, block_tag, "alchemy_free"): liquidity_payload,
    }
    return make_block_pinned_state_call(responses)


def build_state_call_primary_blocked(
    *,
    block_tag: str = "0x" + format(REFERENCE_COVERAGE_TO_BLOCK, "x"),
) -> Any:
    """Build a block-pinned StateView callable that records the
    primary endpoint as unable to serve the pinned depth.

    The returned callable returns ``None`` for every
    ``(method, block_tag, "robinhood_public")`` lookup, exercising
    the depth-cannot-serve path the T036 contract names.
    """
    from robinhood_lp.qualification.state_spot_check import make_block_pinned_state_call

    responses: dict[tuple[str, str, str], bytes | None] = {}
    return make_block_pinned_state_call(responses, default_endpoint="robinhood_public")


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def fresh_chain_id() -> ChainId:
    """Return the pinned chain id as a :class:`ChainId`."""
    return ChainId(REFERENCE_CHAIN_ID)


def fresh_pool_id() -> PoolId:
    """Return the pinned PoolId."""
    return build_reference_pool_id()


def fresh_pool_key() -> PoolKey:
    """Return the pinned PoolKey."""
    return build_reference_pool_key()


def minimal_acquisition_provenance(*, endpoint_alias: str) -> AcquisitionProvenance:
    """Build a minimal :class:`AcquisitionProvenance` for tests."""
    return AcquisitionProvenance(
        endpoint_alias=endpoint_alias,
        retrieval_time="2026-09-17T00:00:00Z",
        request_from_block=REFERENCE_COVERAGE_FROM_BLOCK,
        request_to_block=REFERENCE_COVERAGE_TO_BLOCK,
    )


__all__ = [
    "assert_baseline_consistent",
    "build_agreeing_secondary_source",
    "build_baseline_consistent_event_stream",
    "build_blocked_secondary_source",
    "build_primary_envelopes_by_window",
    "build_state_call_primary_blocked",
    "build_state_call_secondary_serving",
    "distinct_block_count_from_records",
    "fresh_chain_id",
    "fresh_pool_id",
    "fresh_pool_key",
    "minimal_acquisition_provenance",
]
