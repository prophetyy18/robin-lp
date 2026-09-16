# T030 independent review

- Base commit: `54b1edc9be3d218566e2638914ff1c91a804154a`
- Candidate commit: `870c07d0a673bf1c323e6facab4d9d4d65f82ef4`
- Verdict: **PASS**

## Checks

### contract.outcome.raw_lossless — PASS

Raw bytes (topics, data blob, JSON-RPC response wrapper) survive decode -> canonical_bytes -> from_canonical_bytes round trip. HTTP-batch metadata carried on AcquisitionProvenance.

Evidence:

- src/robinhood_lp/storage/decode_log.py:284-413 (decode_log preserves raw_topics, raw_data, raw envelope verbatim alongside typed fields)
- tests/test_storage_schema.py::test_decode_initialize_event (asserts record.raw_topics[0]==EVENT_TOPICS['Initialize'], record.raw_data==data, record.raw['data']=='0x'+data.hex())
- tests/test_storage_schema.py::test_decode_preserves_raw_response_wrapper (asserts record.raw['_jsonrpc_response_wrapper']==wrapper)
- tests/test_storage_schema.py::test_round_trip_preserves_every_field_for_every_record_type (asdict comparison including raw dict)

### contract.outcome.normalized_evolvable — PASS

unknown_fields forward-compat slot preserves forward-shape keys; migrate_to_current rejects future __schema_version__ without silent misrepresentation.

Evidence:

- src/robinhood_lp/storage/schema.py:657-714 (migrate_to_current routes unknown top-level keys into unknown_fields dict)
- tests/test_storage_schema.py::test_v1_migration_preserves_unknown_fields (asserts future_v3_field / future_array survive into unknown_fields)
- tests/test_storage_schema.py::test_round_trip_preserves_every_field_for_every_record_type (unknown_fields round-trip)

### contract.deliverables.schemas_block_tx_receipt — PASS

All three block/tx/receipt context schemas defined and round-trip verified.

Evidence:

- src/robinhood_lp/storage/schema.py:251-342 (BlockContext, TransactionContext, ReceiptContext dataclasses)
- tests/test_storage_schema.py::test_block_context_round_trip, test_transaction_context_round_trip, test_receipt_context_round_trip, test_block_context_accepts_optional_base_fee, test_transaction_context_with_no_to_address

### contract.deliverables.v4_events — PASS

All 5 V4 events have schemas and decoder branches; typed reconstruction produces typed records for each.

Evidence:

- src/robinhood_lp/storage/schema.py:349-591 (InitializeLogRecord, ModifyLiquidityLogRecord, SwapLogRecord, DonateLogRecord, ProtocolFeeUpdatedLogRecord)
- src/robinhood_lp/storage/decode_log.py:77-104 + 366-412 (per-event _DATA_FIELDS / indexed-topic counts and full decoder branches for all 5 events)
- tests/test_storage_schema.py::test_decode_initialize_event, test_decode_modify_liquidity_event, test_decode_swap_event_preserves_signed_amounts, test_decode_donate_event, test_decode_protocol_fee_updated_event

### contract.deliverables.typed_fields_exact_width — PASS

All ABI widths and signedness enforced; values that exceed width are rejected, not silently truncated; raw JSON shows no float coercion.

Evidence:

- src/robinhood_lp/storage/decode_log.py:149-185 (_slot_uint / _slot_int enforce bits; values that exceed bits are rejected)
- src/robinhood_lp/storage/decode_log.py:195-236 (_decode_data_fields dispatches uint24/uint128/uint160/uint256/int24/int128/int256/address/bytes32)
- tests/test_storage_schema.py::test_decode_swap_event_preserves_signed_amounts (asserts amount0=-(2**127), amount1=(2**127)-1, sqrt_price_x96=(1<<160)-1, liquidity=(1<<128)-1, tick=-(2**23), fee=(1<<24)-1)
- tests/test_storage_schema.py::test_decode_swap_rejects_fee_overflow (1<<24 fee slot raises ValueError matching 'Swap.fee')
- tests/test_storage_schema.py::test_decode_protocol_fee_updated_rejects_uint24_overflow
- tests/test_storage_schema.py::test_signed_amounts_preserve_exact_sign_and_width (asserts JSON contains no '1e' or '.0')

### contract.deliverables.provenance_vs_identity — PASS

Cross-provider normalized content hash equality verified while raw acquisition envelopes are retained (canonical_bytes differ).

Evidence:

- src/robinhood_lp/storage/schema.py:93-96 (PROVENANCE_FIELD_NAMES and RAW_FIELD_NAMES excluded from normalized_content_hash)
- src/robinhood_lp/storage/schema.py:761-790 (normalized_content_hash pops provenance + raw fields before SHA-256)
- src/robinhood_lp/storage/schema.py:165-244 (AcquisitionProvenance dataclass with endpoint_alias / retrieval_time / request_from_block / request_to_block / http_batch_size / http_batch_position / request_attempt)
- tests/test_storage_schema.py::test_same_chain_event_different_endpoints_have_same_normalized_content_hash
- tests/test_storage_schema.py::test_normalized_content_hash_is_stable
- tests/test_storage_schema.py::test_acquisition_envelope_round_trips_and_is_excluded_from_normalized_hash (manually reconstructs the provenance-excluded canonical bytes and compares)
- tests/test_storage_schema.py::test_canonical_bytes_exclude_jsonrpc_response_wrapper_from_content_hash

### contract.deliverables.fork_distinct_by_block_hash — PASS

Same-height fork logs are distinct by EventKey.block_hash and by normalized_content_hash.

Evidence:

- src/robinhood_lp/storage/schema.py:382-398 (event_key()/sort_key() on InitializeLogRecord)
- src/robinhood_lp/protocol/events.py:158-225 (EventKey(chain_id, block_hash, tx_hash, log_index))
- tests/test_storage_schema.py::test_same_height_fork_logs_remain_distinct_by_block_hash (block_number=100/tx=BB/log=0 with different block_hash 0xAA vs 0xAB -> different EventKey and different normalized hash)
- tests/test_storage_schema.py::test_event_key_identity_matches_t011
- tests/test_storage_schema.py::test_decode_event_key_uses_t011_identity (record.event_key().block_ref() and .transaction_ref() match T011 BlockRef / TransactionRef)

### contract.deliverables.deterministic_ordering — PASS

Deterministic ordering by (block_number, transaction_index, log_index) verified.

Evidence:

- src/robinhood_lp/storage/schema.py:391-398, 440-441, 492-493, 533-534, 590-591 (sort_key() = (block_number, transaction_index, log_index) on every log record)
- tests/test_storage_schema.py::test_sort_key_is_block_number_then_transaction_index_then_log_index (asserts ordering over a 4-record unordered list)

### contract.deliverables.t021_drift_protocol_fee_updated — PASS

Pinned artifact extended with ProtocolFeeUpdated; drift test now covers all 5 V4 events; SelectorOracle regen path byte-matches the checked-in artifact.

Evidence:

- docs/implement/protocol-artifacts/v4-core-e50237c.json (adds ProtocolFeeUpdated signature + topic0, sha256 field updated and re-hashed; matches expected keccak256 of 'ProtocolFeeUpdated(bytes32,uint24)')
- tools/oracle/src/SelectorOracle.sol:50-62 (emitProtocolFeeUpdatedTopic oracle function)
- tools/oracle/test/SelectorOracle.t.sol:59-66 (test_PoolManager_ProtocolFeeUpdated_topic feeds the topic to the Python artifact regenerator)
- tests/test_abi_artifacts.py:55 (_EXPECTED_EVENT_NAMES now includes ProtocolFeeUpdated)
- tests/test_abi_artifacts.py::test_event_topic_matches_keccak256_of_signature (parametrized over all 5 events, including ProtocolFeeUpdated)
- SHA-256 re-derived in cleanroom: 6123a0f0e51b35ad6c0b5496801084753567f73161172315cbc227ea506b5473 matches declared value in artifact _meta.sha256

### contract.deliverables.schema_v1_migration — PASS

v1 -> v2 migration covers old-version records and unknown fields; future-schema blobs rejected.

Evidence:

- src/robinhood_lp/storage/schema.py:657-714 (migrate_to_current with __schema_version__ gate and class registry)
- src/robinhood_lp/storage/schema.py:717-753 (_migrate_v1_to_v2 backfills removed, transaction_index, acquisition envelope)
- tests/test_storage_schema.py::test_v1_blob_migrates_to_current (synthesised v1 payload loads, removed=False, transaction_index=0, acquisition.endpoint_alias='robinhood_public', legacy source_endpoint / ingestion_time preserved)
- tests/test_storage_schema.py::test_v1_migration_preserves_unknown_fields
- tests/test_storage_schema.py::test_migration_rejects_future_schema_version

### contract.must_not.raw_overwrite — PASS

Raw bytes are written alongside typed fields and never overwritten; canonical form preserves them.

Evidence:

- src/robinhood_lp/storage/decode_log.py:354-356 (raw_topics / raw_data / raw explicitly set on every returned record)
- src/robinhood_lp/storage/schema.py (canonical_bytes preserves raw / raw_topics / raw_data verbatim; normalized_content_hash excludes them by design)
- tests/test_storage_schema.py::test_decode_initialize_event, test_decode_modify_liquidity_event, test_decode_swap_event_preserves_signed_amounts (assert raw bytes equal input)

### contract.must_not.no_float — PASS

Integers stay Python int; no float coercion anywhere in the schema or decoder.

Evidence:

- src/robinhood_lp/storage/schema.py:122-131 (_require_uint enforces int type and rejects bool / float)
- src/robinhood_lp/storage/decode_log.py:149-185 (slot decoders produce int)
- tests/test_storage_schema.py::test_records_carry_no_float (asserts '1e' and '.0' not in canonical JSON)
- tests/test_storage_schema.py::test_signed_amounts_preserve_exact_sign_and_width

### contract.must_not.token_metadata_optional — PASS

No record carries symbol/name/decimals; record construction does not require them.

Evidence:

- src/robinhood_lp/storage/schema.py:349-591 (InitializeLogRecord / ModifyLiquidityLogRecord / SwapLogRecord / DonateLogRecord / ProtocolFeeUpdatedLogRecord all have no symbol/name/decimals fields)
- tests/test_storage_schema.py::test_token_metadata_is_not_required_for_event_records (constructs InitializeLogRecord without metadata)

### contract.must_not.credential_url_prohibition — PASS

Endpoint alias cannot carry a credential-bearing URL; userinfo, secret-query, slash, and whitespace markers all rejected at the schema boundary.

Evidence:

- src/robinhood_lp/storage/schema.py:134-157 (_validate_alias rejects '://', '@', '?', '/', '\\', whitespace)
- tests/test_storage_schema.py::test_acquisition_provenance_validates_alias (rejects https://, alice@, rpc?apiKey=secret, path/with/slashes, has space)
- tests/test_storage_schema.py::test_acquisition_alias_rejects_credential_bearing_url (parametrized over 7 credential-bearing aliases incl. http://user:pass@host:1234/path and rpc.example.com/?apiKey=secret)
- tests/test_storage_schema.py::test_acquisition_provenance_validates_request_interval_order
- tests/test_storage_schema.py::test_acquisition_provenance_validates_batch_position

### contract.must_not.no_acquisition_collapse — PASS

Two distinct acquisitions of the same event produce distinct canonical bytes; only the normalized hash collapses them.

Evidence:

- tests/test_storage_schema.py::test_same_chain_event_different_endpoints_have_same_normalized_content_hash (canonical_bytes(record_a) != canonical_bytes(record_b); both acquisition envelopes retained)
- tests/test_storage_schema.py::test_normalized_content_hash_is_stable

### contract.must_not.no_fast_forward — PASS

T030 is in AWAITING_REVIEW state, not APPROVED; pre-existing implementation status remains explicit; controller alone controls todo/config.yaml.

Evidence:

- todo/config.yaml:13-15 (workflow_state=AWAITING_REVIEW; T030.status=AWAITING_REVIEW; attempt=2; base_commit=54b1edc...; candidate_commit=null; approved_commit=null; latest_review=null)
- todo/config.yaml is changed only by controller, not by candidate 870c07d (workflow-state transitions are controller-managed)
- todo/phases/P03-ingestion-and-storage/T030.md (Implementation status section + Still open section both still present in merged contract)

### quality.pytest_storage_and_abi — PASS

All targeted and regression tests pass; skips are environmental, not introduced by candidate.

Evidence:

- PYTHONPATH=src /tmp/lp-t001-cleanroom-venv/bin/python -m pytest tests/test_storage_schema.py tests/test_abi_artifacts.py -q -> 87 passed in 0.46s
- PYTHONPATH=src /tmp/lp-t001-cleanroom-venv/bin/python -m pytest tests/ -q --ignore=tests/test_workflow.py -> 439 passed, 6 skipped (skips are forge-on-PATH / gpg-verification / reordered-currency-vector, all pre-existing and unrelated to T030)

### quality.ruff_format — PASS

ruff format clean on the candidate's files.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/ruff format --check src/robinhood_lp/storage tests/test_storage_schema.py tests/test_abi_artifacts.py -> '5 files already formatted'

### quality.ruff_check — PASS

ruff lint clean on the candidate's files.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/ruff check src/robinhood_lp/storage tests/test_storage_schema.py tests/test_abi_artifacts.py -> 'All checks passed!'

### quality.mypy_storage — PASS

mypy clean on src/robinhood_lp/storage (decode_log.py, schema.py, __init__.py).

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/mypy src/robinhood_lp/storage -> 'Success: no issues found in 3 source files'

### quality.git_diff_check — PASS

No whitespace errors in the candidate diff.

Evidence:

- git diff --check 54b1edc9... 870c07d0... -> no output (exit 0)

### quality.artifact_sha256 — PASS

Pinned artifact is internally consistent (sha256 + topic0).

Evidence:

- Re-derived canonical-body SHA-256 in cleanroom (sha256 field zeroed) = 6123a0f0e51b35ad6c0b5496801084753567f73161172315cbc227ea506b5473 matches declared value in artifact _meta.sha256
- keccak256('ProtocolFeeUpdated(bytes32,uint24)') = 0xe9c42593e71f84403b84352cd168d693e2c9fcd1fdbcc3feb21d92b43e6696f9 matches artifact topic0

### quality.path_compliance — PASS

Candidate touches only the paths allowed by the merged T030 contract (src/, tests/, docs/implement/protocol-artifacts/, tools/oracle/ for the T021 drift test extension). todo/config.yaml transitions are controller-managed.

Evidence:

- git diff --name-status lists: docs/implement/protocol-artifacts/v4-core-e50237c.json, src/robinhood_lp/storage/__init__.py, src/robinhood_lp/storage/decode_log.py, src/robinhood_lp/storage/schema.py, tests/test_abi_artifacts.py, tests/test_storage_schema.py, todo/config.yaml, todo/evidence/P03/T030/attempt-001-developer.json, todo/evidence/P03/T030/attempt-001-planner.json, todo/evidence/P03/T030/attempt-002-developer.json, todo/phases/P03-ingestion-and-storage/T030.md, todo/reviews/P03/T030/plan-review-001.json, todo/reviews/P03/T030/plan-review-001.md, todo/triage/P03/T030/triage-001.json, tools/oracle/src/SelectorOracle.sol, tools/oracle/test/SelectorOracle.t.sol
- No files under docs/intent/, docs/spec/, AGENTS.md, CLAUDE.md, .claude/, tools/workflow/, other task contracts, or other todo/phases/*.md

### quality.t011_dependency_verification — PASS

T011 EventKey / BlockRef / TransactionRef identity is consumed by every log record's event_key() and the decoder's identity re-derivation.

Evidence:

- src/robinhood_lp/storage/decode_log.py:38 (imports Address, ChainId, PoolId from robinhood_lp.protocol.ids)
- src/robinhood_lp/storage/schema.py:47 (imports Address, ChainId, EventKey, PoolId from robinhood_lp.protocol)
- tests/test_storage_schema.py::test_event_key_identity_matches_t011 (record.event_key() == EventKey(chain_id, block_hash, tx_hash, log_index))
- tests/test_storage_schema.py::test_decode_event_key_uses_t011_identity (record.event_key().block_ref() and .transaction_ref() match T011 surface)

### quality.t021_artifact_drift_test — PASS

T021 drift test extended to cover ProtocolFeeUpdated topic0 and the v2 schema's decode_version is enforced via SHA-256 / Foundry oracle regen.

Evidence:

- tests/test_abi_artifacts.py:147-156 (test_event_topic_matches_keccak256_of_signature parametrized over {Initialize, ModifyLiquidity, Swap, Donate, ProtocolFeeUpdated})
- tests/test_abi_artifacts.py::test_artifact_file_sha256_matches_declared (no pytest.skip; sha256 mismatch fails closed)
- tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output (Foundry SelectorOracle regenerator byte-compares; passes after forge install Uniswap/v4-core@v4-core + v4-periphery + forge-std in worktree)

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- InitializeLogRecord's typed surface still only exposes pool_id; the Deliverables-enumerated Initialize typed fields (currency0, currency1, fee, tick_spacing, hooks, sqrt_price_x96, tick) are present in raw_topics / raw_data but not as dataclass attributes. The decoder decodes the data slots correctly and the source-of-truth raw bytes survive, but the typed Initialize record does not surface them. The constraint 'preserve the existing files' (Must-not: do not mutate pre-existing schema.py outside the T030 contract) plus the pre-existing dataclass shape in attempt 001 prevent closing this in-attempt. Forward-compat is preserved (unknown_fields mechanism + _meta.schema_version bump to 2), so a future T030 maintenance or T031 owner can extend the dataclass without re-querying the chain. Documented inline at src/robinhood_lp/storage/decode_log.py:373-378.
- Eight '# type: ignore[arg-type]' comments in tests/test_storage_schema.py are flagged by mypy as unused-ignore (4 pre-existing from attempt 001 at lines 597/598/618/619; 4 newly added by attempt 002 in test_normalized_content_hash_is_stable and test_normalized_content_hash_changes_on_typed_field_change at lines 1015/1022/1058/1061). mypy on src/robinhood_lp/storage is clean (the contract gate). The unused-ignore flags are non-blocking but are a cleanliness follow-up; a maintenance repair may remove the stale directives.
- test_storage_schema.py mypy output also surfaces 5 'Skipping analyzing module: installed but missing library stubs or py.typed marker' warnings on robinhood_lp.protocol / protocol.abi_artifacts / protocol.events / storage.decode_log / storage.schema. These are project-wide py.typed configuration gaps, not introduced by the candidate; they do not affect any acceptance check or runtime test.
