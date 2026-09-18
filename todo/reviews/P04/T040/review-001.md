# T040 independent review

- Base commit: `7ed5725f8276e2a842efb13c7efde9814625dd92`
- Candidate commit: `98af26d0a6386ed0727f4df7849c3704ea040a13`
- Verdict: **PASS**

## Checks

### input-one-pool-per-replay — PASS

ReplayInput pins chain_id and PoolId at construction; the replayer raises UnknownPoolError at the first event whose pool_id (or chain_id) differs from the input. Verified end-to-end by test_unknown_pool_raises. The data_root is an opaque Path carried on ReplayInput, addressed per-pool, and not interpreted as a URL or used to load anything in the replay layer.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/input.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/replayer.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_unknown_pool_raises

### input-states-pool-and-data-root — PASS

ReplayInput dataclass carries chain_id, pool_id, data_root (Path), from_block, to_block, plus the optional bootstrap snapshot. ReplayOutput carries the input descriptor plus the deterministic checkpoint sequence. _canonical_payload records the data_root string in the output payload so a run states which pool and which data root it replayed.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/input.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/output.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/output.py::_canonical_payload

### no-mixing-of-two-pools — PASS

Per-record validation compares record.chain_id and record.pool_id against ReplayInput values; the first mismatch raises UnknownPoolError, so one replay input cannot mix two pools' events.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/replayer.py::_validate_event_identity
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_unknown_pool_raises

### superseded-dataset-forbidden — PASS

No code path reads, repairs, re-qualifies, overwrites, or merges run-680e65f4a59842d98b1712a45280779d. The identifier only appears in docstrings stating it is not a replay input; there is no constant, no default, no file path, and no fixture that loads or references it. No checkpoint can be derived from it because there is no code that consumes it.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/__init__.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/input.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/replayer.py

### no-assume-1m-block-range — PASS

ReplayInput accepts arbitrary from_block / to_block integers; the replayer has no constant 1,000,001 anywhere. WindowBoundsError rejects any event outside the declared range, so a misconfigured 1,000,001-block range is impossible to silently inherit.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/input.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/replayer.py

### consumes-p03-protocol-fee-updated — PASS

ProtocolFeeUpdatedLogRecord is a first-class event type handled by _apply_protocol_fee_updated; the packed uint24 is unpacked into (token0, token1) directional state. Same-block ordering is resolved by (transaction_index, log_index) via the global sort, so an update-before-swap applies the new fee to the swap that follows in the same block (verified by test_same_block_protocol_fee_update_before_swap_applies).

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/replayer.py::_apply_protocol_fee_updated
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/protocol_fee.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_protocol_fee_changes_in_both_directions
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_protocol_fee_event_carries_packed_value

### dynamic-fee-preserve-emitted-fee — PASS

Swap handler reads record.fee as int and stores it byte-for-byte as last_swap_fee; PoolCheckpoint.event_swap_fee is set to the same value when the producing event is a Swap. The replay never interpolates or guesses a between-swap fee: when no Swap has fired, last_swap_fee is None (verified by test_dynamic_fee_pool_does_not_reconstruct_unobserved_fees).

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/replayer.py::_apply_swap
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_dynamic_fee_pool_preserves_per_swap_fee
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_dynamic_fee_pool_does_not_reconstruct_unobserved_fees

### no-event-only-reconstruction-of-fees — PASS

There is no code path that derives a Swap's fee from anything other than the Swap event itself. test_dynamic_fee_pool_preserves_per_swap_fee swaps the input order and asserts checkpoints are identical — proving no fee is reconstructed from ingestion order or neighbouring events.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/replayer.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_dynamic_fee_pool_preserves_per_swap_fee

### deliverable-total-order — PASS

_order sorts every record by _sort_key = (block_number, transaction_index, log_index). Verified by the dedicated ordering test which shuffles three events with three distinct sort positions and asserts the checkpoint sequence matches the lex order.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/replayer.py::_order
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_replay_ordering_is_block_then_tx_then_log_index

### deliverable-replayed-state — PASS

PoolCheckpoint carries sqrt_price_x96, tick, active_liquidity, cumulative_volume0, cumulative_volume1, protocol_fee_token0, protocol_fee_token1, last_swap_fee and pool_fee. Each is overwritten from the event's emitted integer values; covered by per-handler tests.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/checkpoint.py::PoolCheckpoint
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_swap_updates_price_tick_active_liquidity
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_modify_liquidity_updates_active_liquidity_when_in_range

### deliverable-directional-packed-protocol-fee — PASS

PROTOCOL_FEE_HALF_MASK/PROTOCOL_FEE_HALF_MAX constants in protocol_fee.py match the V4 ABI (12-bit halves, shift=12). pack/unpack are mutual inverses on the valid domain; the checkpoint preserves both the unpacked halves and the raw packed event value under event_protocol_fee_packed.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/protocol_fee.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/checkpoint.py::PoolCheckpoint
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_pack_unpack_protocol_fee_roundtrip
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_protocol_fee_event_carries_packed_value

### deliverable-checkpoint-state — PASS

PoolCheckpoint is a frozen, hashable dataclass with slot storage; ReplayOutput is similarly frozen and hashable. final_checkpoint exposes the terminal snapshot; is_empty distinguishes the empty case.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/checkpoint.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/output.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_pool_checkpoint_is_frozen

### deliverable-t051-integer-evidence — PASS

Each checkpoint carries event_swap_fee (None unless the producing event is a Swap) and event_protocol_fee_packed (None unless the producing event is ProtocolFeeUpdated); both are the raw wire integer the event carried. The directional (token0, token1) state is the unpacked form, so T051 has both the wire and the unpacked view to separate protocol fees from LP fee growth.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/checkpoint.py::PoolCheckpoint.event_swap_fee
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/checkpoint.py::PoolCheckpoint.event_protocol_fee_packed
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_checkpoint_carries_integer_event_evidence_for_t051

### acceptance-shuffled-chunked-restarted-determinism — PASS

Three independent tests assert identical checkpoint sequences and identical replay_output_fingerprint values under shuffling (RNG-seeded), 4-way chunking via itertools.chain, and re-created replayer instances per chunk. The fingerprint uses JSON with sort_keys=True over every checkpoint field plus the input descriptor.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_replay_is_deterministic_under_shuffling
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_replay_is_deterministic_under_chunking
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_replay_is_deterministic_under_restart_boundaries

### acceptance-duplicate-event-error — PASS

DuplicateEventError raised from both _order (full pre-apply scan) and _detect_duplicate (incremental). The exception carries event_key, first_seen_index, block_number, transaction_index, log_index. Two dedicated tests confirm in-order and out-of-order duplicates both raise.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/errors.py::DuplicateEventError
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_duplicate_event_key_raises
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_duplicate_event_key_with_different_sort_position_still_detected

### acceptance-missing-tx-index-error — PASS

MissingTransactionIndexError raised by _sort_key when transaction_index is not a non-negative int. Two tests cover the negative-int and string-int cases.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/errors.py::MissingTransactionIndexError
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/replayer.py::_sort_key
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_negative_transaction_index_raises
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_non_int_transaction_index_raises

### acceptance-impossible-transition-error — PASS

ImpossibleTransitionError carries a stable reason code (swap_before_initialize, modify_before_initialize, donate_before_initialize, second_initialize, swap_with_zero_price). One dedicated test per reason confirms the exception class and reason attribute.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/errors.py::ImpossibleTransitionError
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_swap_before_initialize_raises
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_modify_liquidity_before_initialize_raises
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_donate_before_initialize_raises
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_second_initialize_raises
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_swap_with_zero_price_raises

### acceptance-unknown-pool-error — PASS

UnknownPoolError raised with expected_pool_id, actual_pool_id, and the offending position. Verified by test_unknown_pool_raises.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/errors.py::UnknownPoolError
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_unknown_pool_raises

### acceptance-dynamic-fee-byte-for-byte — PASS

PoolCheckpoint.event_swap_fee is the same int the Swap event's fee field carried. The test asserts cps[i].event_swap_fee == swap_i.fee for three consecutive swaps with distinct fees.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/checkpoint.py::PoolCheckpoint.event_swap_fee
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_dynamic_fee_pool_preserves_per_swap_fee

### acceptance-no-reconstruct-between-swap-fees — PASS

There is no code path that infers a between-swap fee; with no Swap fired, last_swap_fee stays None. test_dynamic_fee_pool_preserves_per_swap_fee also swaps the input order to prove ingestion order is not used to derive fees.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/replayer.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_dynamic_fee_pool_preserves_per_swap_fee
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_dynamic_fee_pool_does_not_reconstruct_unobserved_fees

### acceptance-protocol-fee-both-directions — PASS

test_protocol_fee_changes_in_both_directions emits three updates that decrease token0 then decrease token1; the resulting checkpoint sequence reflects each transition independently.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_protocol_fee_changes_in_both_directions

### acceptance-same-block-update-before-swap — PASS

Three tests verify (a) update-before-swap applies the new fee and swap's emitted fee is still preserved, (b) swap-before-update does not retroactively change the swap's emitted fee, and (c) multiple updates in the same block apply cumulatively.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_same_block_protocol_fee_update_before_swap_applies
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_same_block_protocol_fee_update_after_swap_does_not_change_swap_fee
- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_same_block_swap_uses_latest_protocol_fee_within_block

### acceptance-zero-protocol-fee-init-state — PASS

_PoolState initialises protocol_fee_token0 and protocol_fee_token1 to 0; after Initialize only, the checkpoint has (0, 0) and pack_protocol_fee(0, 0) == 0.

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py::test_protocol_fee_state_initialized_to_zero
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/replayer.py::_PoolState

### verification-pytest-strict-types-ruff — PASS

Independent reviewer run of the Developer-reported commands reproduces the same exit status and outcome summaries. No skipped tests introduced by T040 (6 pre-existing skips are forge / gpg / PoolKey-vector skips).

Evidence:

- /home/lpdev/lp-worktrees/review-t040-attempt-001/tests/test_replay_t040.py
- /home/lpdev/lp-worktrees/review-t040-attempt-001/src/robinhood_lp/replay/
- command: pytest tests/test_replay_t040.py = 45 passed in 0.18s
- command: pytest tests/ --ignore=tests/test_abi_artifacts.py = 967 passed, 6 skipped
- command: ruff check src/robinhood_lp/replay tests/test_replay_t040.py tests/_replay_t040_fixtures.py = All checks passed
- command: ruff format --check src/robinhood_lp/replay tests/test_replay_t040.py tests/_replay_t040_fixtures.py = 9 files already formatted
- command: mypy --strict src/robinhood_lp/replay tests/test_replay_t040.py tests/_replay_t040_fixtures.py = Success: no issues found in 9 source files
- command: mypy --strict src/ = Success: no issues found in 76 source files
- command: ruff check . = All checks passed
- command: ruff format --check . = 291 files already formatted

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- The replay carries the bootstrap snapshot (initial_sqrt_price_x96, initial_tick) on the ReplayInput because V4's Initialize log event does not emit sqrtPriceX96 / tick on chain. Sourcing that snapshot from a block-pinned StateView read at the pool's Initialize block is owned by T041/T042 and is out of scope here. The replay refuses a Swap until both an Initialize event has been observed and sqrt_price_x96 is non-zero, so the natural fallback is correct.
- The replay's active_liquidity view is event-only: Swap events overwrite active_liquidity directly from the event's emitted liquidity; ModifyLiquidity updates active_liquidity only when the current tick is inside [tick_lower, tick_upper]. A full liquidity_net bitmap model that crosses ticks without an intervening Swap is owned by T041 and out of scope for T040.
- The replay does not consult the superseded 2026-09-18 reference dataset and provides no back door for it; the data-root loader that refuses to load that path is owned by T041/T042.
- The __init__.py and input.py docstrings reference a load_replay_events helper that is owned by T041/T042 and is not implemented in T040. The replay's public surface is Replayer / replay / ReplayInput / ReplayOutput, which match the T040 deliverables; the docstring forward-reference is informational, not an exposed API.
