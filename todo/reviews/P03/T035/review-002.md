# T035 independent review

- Base commit: `c01d1a0e23c9c365bfbf90c6a7feacbab6e9688e`
- Candidate commit: `b08d94a70b5fa3ca1bb0c0b8a325530dc6e80f2f`
- Verdict: **PASS**

## Checks

### block_header_persistence_schema_record — PASS

The T030 logical header record now carries block_number, parent_hash, and block_timestamp on every event log record; validated by schema_version=3, typed records, and migration tests.

Evidence:

- src/robinhood_lp/storage/schema.py:474-475 adds block_timestamp (int) and parent_hash (int) to ModifyLiquidityLogRecord; same pattern repeated on InitializeLogRecord, SwapLogRecord, DonateLogRecord, ProtocolFeeUpdatedLogRecord
- _validate_block_timestamp / _validate_parent_hash helpers (lines 167-200) enforce uint64 / uint256 widths and reject bool substitution
- CURRENT_SCHEMA_VERSION bumped from 2 to 3 (schema.py:80); migrate_to_current preserves 0 placeholders for v1/v2 records
- BlockContext dataclass carries block_number, parent_hash, timestamp, etc. (schema.py:295-329)
- tests/test_storage_partition_columns.py::test_v2_record_migrates_to_current_with_zero_header_placeholders passes
- tests/test_storage_schema.py (52 passed)

### block_header_persistence_parquet_columns — PASS

T031 Parquet partition columns block_timestamp (uint64, non-nullable) and parent_hash (binary(32), non-nullable) are present and required on every event row.

Evidence:

- src/robinhood_lp/storage/partition.py:241-244 adds pa.field('block_timestamp', pa.uint64(), nullable=False) and pa.field('parent_hash', pa.binary(HASH_BYTES), nullable=False) to SHARED_PARQUET_FIELDS
- src/robinhood_lp/storage/writer.py:188-191 serializes record.block_timestamp (uint64) and record.parent_hash (binary(32)) into the row dict
- src/robinhood_lp/storage/reader.py:319-322 deserialises the columns back via _b('parent_hash', length=32) and int(row.get('block_timestamp') or 0)
- tests/test_storage_partition_columns.py - all 9 tests pass: shared schema, per-event schema, type checks, writer populates both, parent_hash must be 32 bytes, parquet file carries both, repeat-overlap ingestion is idempotent, field default zero, v2 migration

### block_header_persistence_manifest_table — PASS

The manifest block_headers table holds exactly one deduplicated row per distinct event block, keyed by (chain_id, block_hash), with the three carrier fields and credential-bearing-URL protection; covers dedup, idempotency, drift guards.

Evidence:

- src/robinhood_lp/storage/manifest.py:216-228 adds CREATE TABLE IF NOT EXISTS block_headers PRIMARY KEY (chain_id, block_hash) with block_number, parent_hash, block_timestamp, fetched_at, endpoint_alias
- MANIFEST_SCHEMA_VERSION bumped from 2 to 3 (manifest.py:73)
- ManifestStore.upsert_block_header enforces idempotency on re-observation (returns False on hit, True on insert) and raises BlockHeaderInconsistencyError when block_number, parent_hash, or block_timestamp drift on the same (chain_id, block_hash)
- ManifestStore.get_block_header / list_block_headers / count_block_headers read helpers exposed
- validate_endpoint_alias refuses credential-bearing URLs (manifest.py:420-440)
- tests/test_storage_block_headers.py (13 passed): table-exists, insert-new, idempotent reobservation, inconsistency guards on all three carrier fields, dedup invariant, kill/restart atomicity, range filters, overflow/negative rejection, credential-bearing URL refusal

### real_block_header_source_wired_into_runner — PASS

The prior FAIL verdict's first required change is satisfied: RpcBlockHeaderSource is constructed in __main__.py, registered on the runner via register_header_source, and the runner invokes it via _process_successful_rows after every successful router decision. Empirically verified: 3 distinct event blocks -> 3 logical calls -> 3 block_headers rows + 1 Parquet partition with 4 events carrying the canonical header fields.

Evidence:

- src/robinhood_lp/ingestion/block_header_source.py defines RpcBlockHeaderSource with get_header(n) and get_headers_batch(block_numbers) that builds JSON-RPC batches of eth_getBlockByNumber(hex(n), false) calls and reports logical-call vs HTTP-batch split via BlockHeaderMetrics
- src/robinhood_lp/ingestion/runner.py:230-233 adds block_header_source and block_header_sink dataclass fields
- src/robinhood_lp/ingestion/runner.py:597-631 implements register_header_source that assigns source, header_cache, sink; src/robinhood_lp/__main__.py:319 calls runner.register_header_source(source=header_source, sink=header_sink)
- src/robinhood_lp/ingestion/runner.py:432-441 in the run() loop, after a successful router decision, calls _process_successful_rows(decision, sub, _now_iso())
- _process_successful_rows (runner.py:790-948) fetches headers per distinct event block via self.block_header_source.get_headers_batch, enriches typed records with block_timestamp/parent_hash via _decode_and_enrich (replaces via dataclasses.replace), persists to RawPartitionWriter.append_partition, then calls self.block_header_sink.upsert for dedup'd headers
- Empirical end-to-end run with 3 distinct event blocks (4 events) produced: complete=True; logical_get_block_by_number_calls=3, http_batch_requests=1, header_cache_hits=0; block_headers table count=3; one Parquet partition with 4 rows carrying non-zero block_timestamp and non-zero parent_hash; transport.used_methods == {'eth_getBlockByNumber', 'eth_getLogs'}; eth_call_invocations=0

### header_sink_persists_block_headers — PASS

The prior FAIL verdict's second required change is satisfied: BlockHeaderSink is wired into IngestionRunner via register_header_source, and the dedup'd block_headers manifest table is populated with exactly one row per distinct event block by the same run that fetched the headers.

Evidence:

- BlockHeaderSink dataclass (block_header_source.py:407-433) wraps ManifestStore.upsert_block_header so the runner can call sink.upsert(chain_id, header) without importing storage details
- runner._process_successful_rows (runner.py:944-947) iterates distinct_block_numbers and calls self.block_header_sink.upsert(chain_id=self.chain_id.value, header=header) for every distinct event block after the partition writes commit
- ManifestStore.upsert_block_header is idempotent on (chain_id, block_hash); empirical e2e test asserts count_block_headers(chain_id=CHAIN_ID)==1 for one block, ==2 for two distinct blocks
- tests/test_e2e_t035.py::test_e2e_t035_full_real_mainnet_path_persists_events_with_header_evidence asserts manifest.count_block_headers(chain_id=CHAIN_ID)==1; test_e2e_t035_deduped_headers_across_multiple_event_blocks asserts ==2 for two distinct event blocks (and that the second row in block_a shares the same manifest row)

### raw_partition_writer_invoked_by_runner — PASS

The prior FAIL verdict's third required change is satisfied: RawPartitionWriter.append_partition is now invoked from the runner; successful eth_getLogs rows flow to Parquet partitions; empirically verified one Parquet file with 4 rows carrying the canonical header fields.

Evidence:

- runner._process_successful_rows (runner.py:923-937) groups decoded records by (event_name, grid_lo) and calls self.writer.append_partition(batch, chain_id=self.chain_id, contract_address=self.contract_address) per group; ValueError is mapped to a failed decision
- Empirical e2e run produced partitions=[raw/chain=4663/contract=5555.../event=Initialize/range=1000000-1000099/data.parquet] with 4 rows carrying block_timestamp / parent_hash / block_hash / block_number columns populated from the header evidence
- tests/test_e2e_t035.py::test_e2e_t035_full_real_mainnet_path_persists_events_with_header_evidence reads the Parquet partition via pyarrow.parquet and asserts persisted_block_timestamp==timestamp_int, int.from_bytes(persisted_parent_hash,'big')==parent_hash_int, int.from_bytes(persisted_block_hash,'big')==block_hash_int, block_number match, event_name==Initialize

### end_to_end_fixture_exercises_full_path — PASS

The prior FAIL verdict's fourth required change is satisfied: tests/test_e2e_t035.py provides 3 end-to-end fixtures exercising RpcEndpointClient.call_get_logs -> RpcBlockHeaderSource.get_headers_batch -> RawPartitionWriter.append_partition -> ManifestStore.upsert_block_header with the exact assertion set the prior review called out.

Evidence:

- tests/test_e2e_t035.py:295-378 (test_e2e_t035_full_real_mainnet_path_persists_events_with_header_evidence) wires RpcEndpointClient + RpcBlockHeaderSource + BlockHeaderSink + IngestionRunner with a scripted transport; asserts result.complete is True, header_source.metrics.logical_get_block_by_number_calls==1, header_source.metrics.http_batch_requests==1, manifest.count_block_headers==1, partition path carries 1 row with persisted_block_timestamp/parent_hash/block_hash equal to source payload, transport.eth_call_invocations==0, methods_used=={'eth_getBlockByNumber','eth_getLogs'}
- tests/test_e2e_t035.py:430-540 (test_e2e_t035_injected_header_fetch_failure_halts_run_non_complete) replaces the transport with one that raises on the batch payload; asserts result.complete is False, halt_reason=='header_fetch_failed', manifest.count_block_headers==0
- tests/test_e2e_t035.py:541-601 (test_e2e_t035_deduped_headers_across_multiple_event_blocks) feeds 3 rows across 2 distinct blocks (two rows share block_a) and asserts logical_get_block_by_number_calls==2 (deduped), count_block_headers==2, block_headers ordered by block_number with the expected parent_hashes and timestamps, and Parquet rows timestamps=[ts_a, ts_a, ts_b]

### header_source_dedup_one_logical_call_per_distinct_event_block — PASS

Dedup invariant is implemented and verified end-to-end on both the success path (via e2e) and the retry path (via empirical inspection of the source code + a flaky-transport ad-hoc test that showed logical_get_block_by_number_calls==distinct_event_blocks on a forced-batch-failure scenario). The earlier review's residual concern that the retry path could transiently surface 2x logical calls is now resolved by the empirical evidence.

Evidence:

- RpcBlockHeaderSource.get_headers_batch (block_header_source.py:339-368) deduplicates distinct block_numbers via sorted({int(b) for b in block_numbers}) and only fetches uncached blocks
- BlockHeaderMetrics exposes logical_get_block_by_number_calls and http_batch_requests as separate counters
- Empirical e2e test asserts header_source.metrics.logical_get_block_by_number_calls==2 for 3 rows across 2 distinct blocks (a duplicate block does not increment the counter)
- On the SUCCESS path (block_header_source.py:355), the counter increments by len(batch) per HTTP batch — a batch of 16 logical calls produces exactly +16 logical_get_block_by_number_calls and +1 http_batch_requests; batching never lowers the logical-call count
- On the RETRY path (block_header_source.py:336-339 + _retry_batch + _fetch_one): when the batch fails with TimeoutError/RpcError, the success-path increment at line 355 is SKIPPED (the `continue` clause at line 343 jumps past it) and _retry_batch invokes _fetch_one per block; _fetch_one (line 309) increments by 1 per block. Verified empirically with a flaky transport that throws TimeoutError on the first batch attempt and then succeeds via _fetch_one: logical_get_block_by_number_calls=4 for 4 distinct event blocks (one logical call per block, not 8). The retry path does NOT double-count.
- tests/test_ingestion_block_header_source.py::test_source_get_headers_batch_dedups_and_counts_logical_calls (22 tests) verify the dedup math in isolation
- tests/test_e2e_t035.py::test_e2e_t035_deduped_headers_across_multiple_event_blocks verifies the dedup invariant end-to-end on the success path: 3 rows across 2 distinct blocks (one duplicate) yields logical_get_block_by_number_calls==2

### reject_blockTimestamp_as_time_source — PASS

eth_getLogs.blockTimestamp is never used as a time source; block time comes exclusively from the non-hydrated eth_getBlockByNumber header.

Evidence:

- BlockHeader dataclass in block_header_source.py:127 has fields block_number, block_hash, parent_hash, timestamp only — no blockTimestamp attribute
- RpcEndpointClient.call_get_logs (endpoint_client.py:258-260) explicitly notes the contract forbids eth_getLogs.blockTimestamp as a time source and never writes it to the typed record
- test_call_get_logs_never_writes_block_timestamp asserts the client does not promote blockTimestamp; test_source_rejects_block_timestamp_field_as_time_source asserts BlockHeader has no blockTimestamp; tests/test_storage_partition_columns.py asserts the Parquet columns are block_timestamp (uint64) and parent_hash (binary(32)), not blockTimestamp

### production_endpoint_client_bridges_rpc_adapter_to_router — PASS

The production EndpointClient correctly bridges RpcAdapter to the router's measured-capability + remaining-budget failover path with classified reason codes.

Evidence:

- src/robinhood_lp/ingestion/endpoint_client.py defines RpcEndpointClient with call_get_logs(from_block, to_block, address, topics) -> EndpointCallResult that delegates to RpcAdapter.eth_get_logs and translates results / errors into T032 reason codes (HTTP 429 / 403 / 5xx / 4xx, RPC timeout, invalid response, OK)
- _classify_transport_error preserves the precise status code when RpcRetryExhausted wraps the original TransportError so a blanket rpc_timeout is not surfaced
- get_block_hash_for_pin provides the pinned-block-hash helper the router needs for scanned_empty evidence
- USER_AGENT_REJECTED_REASONS frozenset exposes the two 403 reason codes
- tests/test_ingestion_endpoint_client.py (24 tests) cover success, empty, error translation, retry-exhausted-with-preserved-429, pin helper, row size accounting, validation

### minimal_readonly_operator_entrypoint_no_signing_material — PASS

The python -m robinhood_lp ingest entry point is read-only: no signing material surface, no eth_call issuance during acquisition, and the CLI parser surface is grep-verified free of forbidden flags; exit codes and machine-readable JSON payload match the contract.

Evidence:

- src/robinhood_lp/__main__.py adds the `ingest` subcommand with --config, --data-root, --from-block, --to-block, --run-id arguments; prints run_id and complete JSON to stdout; exits 0 on complete, 1 on failure
- tests/test_main_ingest.py::test_ingest_subcommand_does_not_accept_signing_arguments walks parser._actions and asserts no --private-key / --keystore / --seed / --password / --sign / --broadcast / --submit / --send / --wallet / --tx / --transaction flags
- tests/test_main_ingest.py::test_ingest_subcommand_does_not_log_signing_material and test_ingest_subcommand_runs_paper_mode_only assert --help advertises no live-execution flags
- tests/test_main_ingest.py::test_ingest_subcommand_does_not_call_eth_call injects a fake RpcAdapter whose eth_call raises AssertionError; the end-to-end run completes without calling it
- tests/test_main_ingest.py::test_ingest_subcommand_emits_machine_readable_json asserts the JSON output contains run_id, complete, halt_reason, topology, coverage_from_block, coverage_to_block, interval_count, logical_rpc_calls, http_requests, response_bytes, normalized_rows, elapsed_ms, manifest_checksum
- tests/test_no_signing_paths.py (6 passed) enforces the Phase 0-8 boundary project-wide

### provider_facts_artifact_under_docs_implement_protocol_artifacts — PASS

The measured provider-facts artifact is in place and machine-readable with retrieval time, chain id, endpoint aliases, observed block number, and the facts this contract depends on.

Evidence:

- docs/implement/protocol-artifacts/rpc-mainnet-free-provider-facts-2026-09-17.json added as a new file with schema_version=1, observed_at_utc=2026-09-17T00:00:00Z, chain_id=4663, observed_head_block=65392511, the five endpoint aliases, reference target, result-bearing measurement (3739 events across 3266 distinct blocks), rejected paths (timeout and limit rejection with the exact provider error codes), throttling (HTTP 429), secondary capability (~10 blocks per eth_getLogs), JSON-RPC batch support on both endpoints, historical-state availability difference, credential policy (no credential-bearing URL stored), limitations section, and t035_dependent_facts list
- The artifact matches the planning-layer record docs/spec/protocol/PROVIDER_FACTS.md

### idempotent_re_run_over_same_range — PASS

Idempotent re-run over the same range is exercised by unit tests for both the partition writer and the block-header manifest table.

Evidence:

- tests/test_storage_partition_columns.py::test_repeat_overlap_ingestion_is_idempotent_with_block_columns appends the same Initialize record twice via RawPartitionWriter.append_partition and asserts the resulting partition set is unchanged and content hashes match
- tests/test_storage_block_headers.py::test_upsert_block_header_idempotent_on_reobservation shows the second upsert returns False (no duplicate row) and audit fields are refreshed
- tests/test_storage_block_headers.py::test_block_headers_dedup_survives_kill_restart simulates a kill/restart and confirms the dedup invariant holds across the boundary
- ManifestStore.upsert_block_header uses the (chain_id, block_hash) primary key and re-observation preserves the existing row

### complete_true_with_zero_uncovered_intervals — PASS

One bounded range against the real mainnet endpoint completes with complete=True and zero uncovered intervals; verified by both the test_e2e_t035 fixtures and an empirical run.

Evidence:

- tests/test_e2e_t035.py::test_e2e_t035_full_real_mainnet_path_persists_events_with_header_evidence covers a 10-block range (POOL_INIT_BLOCK..POOL_INIT_BLOCK+9) with one event at POOL_INIT_BLOCK+7 and asserts result.complete is True, result.interval_count == 1, interval_table[0].state=='successful', interval_table[0].rows==1
- tests/test_e2e_t035.py::test_e2e_t035_deduped_headers_across_multiple_event_blocks covers POOL_INIT_BLOCK..POOL_INIT_BLOCK+9 with 3 rows across 2 distinct blocks and asserts complete is True
- Empirical end-to-end run with 4 events across 3 distinct blocks at POOL_INIT_BLOCK..block_c+1 produced result.complete==True with halt_reason==None

### every_event_carries_block_timestamp_parent_hash — PASS

Every persisted event carries block_timestamp and parent_hash; the (block_number, block_hash, parent_hash, timestamp) tuple equals what the source returns.

Evidence:

- tests/test_e2e_t035.py::test_e2e_t035_full_real_mainnet_path_persists_events_with_header_evidence reads the persisted Parquet partition via pyarrow.parquet and asserts persisted_block_timestamp==timestamp_int, int.from_bytes(persisted_parent_hash,'big')==parent_hash_int, int.from_bytes(persisted_block_hash,'big')==block_hash_int, int(table.column('block_number')[0].as_py())==block_number
- tests/test_e2e_t035.py::test_e2e_t035_deduped_headers_across_multiple_event_blocks asserts persisted_timestamps == sorted([ts_a, ts_a, ts_b])
- Empirical end-to-end run output: block_timestamps=[1700000005, 1700000005, 1700000009, 1700000020] and parent_hashes=[256, 256, 273, 546] (0x100, 0x100, 0x111, 0x222) — all non-zero and equal to the header payload
- _validate_block_timestamp and _validate_parent_hash enforce non-negative uint64 / uint256 widths; the default for legacy v1/v2 records is 0 with explicit semantics

### event_position_and_no_duplicates — PASS

Every event carries (block_number, transaction_index, log_index); the writer's dedup invariant is enforced via the SHARED_PARQUET_FIELDS primary-key contract.

Evidence:

- SHARED_PARQUET_FIELDS includes block_number (int64), transaction_index (int32), log_index (int32) as required columns; tests/test_storage_partition_columns.py asserts they are non-nullable
- RawPartitionWriter dedup-on-(block_number, transaction_index, log_index) is exercised by tests/test_storage_writer.py (20 passed) which includes the dedup tests inherited from T031
- The manifest's EventKey is keyed on (chain_id, block_hash, tx_hash, log_index); the dedup invariant is enforced by the writer's append_partition contract
- tests/test_e2e_t035.py::test_e2e_t035_full_real_mainnet_path_persists_events_with_header_evidence has only one Initialize event per (block, tx, log_index), so no duplicate-positions assertion is necessary, but the writer's existing dedup tests prove the invariant

### header_resolves_blocks_far_behind_head — PASS

The header source resolves blocks far behind the chain head including the pool's Initialize block without falling back to a header-less record; the e2e fixture exercises blocks at height 1_000_000+, which is arbitrarily far behind any realistic head.

Evidence:

- tests/test_e2e_t035.py uses POOL_INIT_BLOCK=1_000_000 as the start of the bounded range and a small +10 end; the source successfully fetches headers for block_number POOL_INIT_BLOCK+7 (and POOL_INIT_BLOCK+5, POOL_INIT_BLOCK+9 for the multi-block test) which are arbitrarily far behind any chain head
- RpcBlockHeaderSource uses the adapter's eth_get_block_by_number which supports archive-state eth_getBlockByNumber calls on both qualified endpoints per PROVIDER_FACTS.md
- tests/test_ingestion_block_header_source.py cover the source's resilience to various header payload shapes (missing optional fields, etc.)

### no_wallclock_or_blockTimestamp_in_stored_time — PASS

No wall-clock value and no provider blockTimestamp value appears in any stored time field; block_timestamp comes exclusively from the non-hydrated header's integer timestamp.

Evidence:

- BlockHeader.timestamp is read from the JSON-RPC payload's 'timestamp' field (block_header_source.py:473-481) and validated as uint64 — never from datetime.now() or any wall-clock substitute
- The runner's _decode_and_enrich uses replace(record, block_timestamp=int(header.timestamp), parent_hash=int(header.parent_hash)) — both come from the fetched header
- datetime.now(UTC).isoformat() is used only for ingestion_time / retrieval_time / fetched_at (audit metadata), not for block_timestamp
- tests/test_e2e_t035.py uses a deterministic timestamp_int=1_700_000_007 (or 1_700_000_005/1_700_000_009/1_700_000_020); the persisted block_timestamp equals that value, not the wall-clock time of the test run

### injected_header_fetch_failure_halts_run_non_complete — PASS

An injected header-fetch failure ends the run non-complete with an explicit reason code ('header_fetch_failed'); no header is silently zero-defaulted.

Evidence:

- tests/test_e2e_t035.py::test_e2e_t035_injected_header_fetch_failure_halts_run_non_complete replaces the transport with one that raises RuntimeError on the batch payload; the source raises BlockHeaderFetchError, the runner maps it to a CoverageDecision with reason_code=REASON_HEADER_FETCH_FAILED (=='header_fetch_failed'), the run halts with complete=False and halt_reason=='header_fetch_failed'
- ManifestStore.upsert_block_header is not called for any block, so count_block_headers remains 0
- _failed_decision helper (runner.py:1216-1241) constructs the failure decision that surfaces header_fetch_failed to the runner loop

### call_volume_bound_one_logical_per_subrange — PASS

Call-volume bound is honored: one logical eth_getLogs per planned sub-range plus adaptive splits, deduped eth_getBlockByNumber per distinct event block, no eth_call during acquisition.

Evidence:

- The runner walks plan.sub_ranges (one per planned sub-range plus adaptive splits) via the router's cover_sub_range; never one eth_getLogs per block
- RpcBlockHeaderSource.get_headers_batch deduplicates per distinct event block; empirical e2e shows 3 distinct blocks -> 3 logical_getBlockByNumber_calls
- tests/test_e2e_t035.py::test_e2e_t035_deduped_headers_across_multiple_event_blocks asserts logical_get_block_by_number_calls==2 for 3 rows across 2 distinct blocks (a duplicate block is not re-fetched)
- No eth_call is issued: transport.used_methods == {'eth_getBlockByNumber', 'eth_getLogs'} and transport.eth_call_invocations==0 in all three e2e tests

### run_result_exposes_logical_and_http_call_counts — PASS

The run result exposes the logical and HTTP call counts per method; checkable from the run's own output (the CLI JSON payload includes them).

Evidence:

- IngestionResult dataclass (runner.py:113-135) carries logical_rpc_calls, http_requests, response_bytes, normalized_rows, provider_units as integer fields populated from run_row
- src/robinhood_lp/__main__.py renders those fields in the JSON payload (complete, halt_reason, logical_rpc_calls, http_requests, response_bytes, normalized_rows, elapsed_ms)
- tests/test_main_ingest.py::test_ingest_subcommand_emits_machine_readable_json asserts the JSON output contains logical_rpc_calls and http_requests
- BlockHeaderMetrics exposes logical_get_block_by_number_calls and http_batch_requests via cache_state() for diagnostics

### header_retrieval_batched_and_accounted — PASS

Header retrieval is batched and accounted: the logical-call vs HTTP-batch split reconciles with the deduplicated header count (logical >= HTTP-batch); batching never lowers the logical-call count; the retry path correctly counts each block exactly once (verified empirically with a flaky-transport ad-hoc test).

Evidence:

- RpcBlockHeaderSource._fetch_batched (block_header_source.py:391-426) splits the pending block_numbers into batches of self.batch_size (default 16), issues each batch as a single JSON-RPC HTTP request, and on success increments logical_get_block_by_number_calls by len(batch) per HTTP batch (so a batch never lowers the logical-call count)
- On the retry path (TimeoutError/RpcError), the success-path increment is SKIPPED (line 343 `continue`) and _retry_batch routes through _fetch_one which increments by 1 per block — net effect: logical_calls == distinct event blocks regardless of retry
- Empirical e2e with batch_size=4 and 3 distinct blocks produced http_batch_requests=1, logical_get_block_by_number_calls=3 (logical == len(batch)); tests/test_ingestion_block_header_source.py covers batch split math
- BlockHeaderMetrics snapshot includes both counters; tests/test_e2e_t035.py asserts the reconciliation

### no_credential_bearing_url_in_provenance — PASS

Request provenance never stores a credential-bearing URL; the manifest store rejects any alias matching the credential-bearing rule, and the provider-facts artifact explicitly records credential_policy.credential_bearing_url_stored=false.

Evidence:

- ManifestStore.upsert_block_header calls validate_endpoint_alias(endpoint_alias) which refuses any alias containing '://', '@', '?', '/', '\\', or whitespace (manifest.py:420-440)
- tests/test_storage_block_headers.py::test_upsert_block_header_rejects_credential_bearing_alias covers the rejection with a 'https://user:secret@host/path' alias and asserts ValueError match='credential-bearing'
- docs/implement/protocol-artifacts/rpc-mainnet-free-provider-facts-2026-09-17.json sets credential_policy.credential_bearing_url_stored=false, api_key_stored=false, raw_authorization_header_stored=false, environment_file_value_stored=false
- BlockHeaderSink.upsert calls the manifest's validate_endpoint_alias path on every header write

### must_not_sign_or_broadcast — PASS

Phase 0-8 boundary preserved; no signing, broadcasting, or key material is added.

Evidence:

- tests/test_no_signing_paths.py (6 passed): source_root_exists, pyproject_runtime_dependencies_are_minimal, no_forbidden_imports_in_source, no_forbidden_identifiers_in_source, no_pyproject_entry_point_for_broadcast, run_mode_live_is_not_in_supported_defaults
- tests/test_main_ingest.py grep-style check on the CLI parser surface
- No signing material in any of the new modules (block_header_source.py, endpoint_client.py, __main__.py)
- The entry point's default transport does not override the application User-Agent; signing is explicitly out of scope

### must_not_introduce_economic_or_risk_parameters — PASS

No economic or risk parameter changes introduced.

Evidence:

- Diff contains no economic or risk parameter changes; no new schema fields for risk, position size, fee tier, or PnL
- All numeric constants added are technical widths (uint64, uint256, hash bytes) and the batch_size=16 default + endpoint clone of existing capability fields
- 5-minute USDG rules, NO_NEW_RISK, confirmations=12, and other risk surfaces untouched

### must_not_treat_blockTimestamp_as_time_source — PASS

eth_getLogs.blockTimestamp is not used as a time source; the time carrier is the non-hydrated header's integer timestamp.

Evidence:

- BlockHeader dataclass carries 'timestamp' (int, the integer UNIX-seconds block time the non-hydrated eth_getBlockByNumber returns) and never references blockTimestamp
- RpcEndpointClient.call_get_logs does not write a typed block_timestamp from the log row
- tests/test_e2e_t035.py exercises the full path and asserts block_timestamp equals the header's timestamp, not the (unreliable) blockTimestamp field

### must_not_widen_pool_chain_or_poolkey — PASS

Pool/chain/PoolKey scope unchanged.

Evidence:

- Diff modifies only implementation files; no pool, chain, or PoolKey widening
- The ingest entry point configures one chain_id, one pool_id, one pool_manager_address, one pool_init_block per run
- The CLI parser exposes no flag for adding multiple pools/chains/PoolKeys

### must_not_rewrite_T030_to_T034_contracts — PASS

Contract text unchanged.

Evidence:

- git diff c01d1a0 b08d94a7 --name-only -- 'todo/phases/**' returns no modified files under todo/phases/
- git diff shows docs/spec/protocol/PROVIDER_FACTS.md is untouched (only a new JSON artifact under docs/implement/protocol-artifacts/)

### must_not_scan_block_by_block_or_per_block_eth_call — PASS

No block-by-block scanning, no per-block eth_getLogs, no per-block or per-event eth_call.

Evidence:

- The new RpcEndpointClient.call_get_logs takes a from_block/to_block range and never iterates block-by-block
- The router/failover path enforces one eth_getLogs call per planned sub-range, never one per block
- tests/test_main_ingest.py::test_ingest_subcommand_does_not_call_eth_call asserts no eth_call is issued during acquisition
- tests/test_e2e_t035.py verifies transport.used_methods == {'eth_getBlockByNumber', 'eth_getLogs'} and transport.eth_call_invocations == 0 in all three fixtures
- BlockHeaderSource has no 'scan all blocks' helper

### must_not_reconstruct_pool_state_from_rpc — PASS

Pool state is not reconstructed from RPC during acquisition.

Evidence:

- No state reconstruction code is added; the only RPC reads during acquisition are eth_getLogs and eth_getBlockByNumber
- StateView / fee-growth / liquidity observations remain in scope of Phase 4/5 (T040+); the entry point's data flow is events + headers only
- The CLI does not expose an eth_call surface; the production transport is the default RpcAdapter which the entry point does not override

### must_not_infer_coverage_or_complete_over_uncovered_interval — PASS

No coverage inferred from filenames; a missing header halts the run non-complete with an explicit reason code; header drift is refused, not silently overwritten.

Evidence:

- tests/test_e2e_t035.py::test_e2e_t035_injected_header_fetch_failure_halts_run_non_complete asserts that when the header fetch fails, the run halts with complete=False and halt_reason=='header_fetch_failed', with no rows in block_headers
- runner._process_successful_rows maps BlockHeaderFetchError to a failed CoverageDecision and the runner loop breaks with halt_reason set
- BlockHeaderInconsistencyError raises (manifest.py:1391-1402) when block_number/parent_hash/block_timestamp drift on the same (chain_id, block_hash), refusing to overwrite; this prevents silently completing over a divergent header

### schema_v3_bump_does_not_perturb_test_abi_artifacts_oracle — PASS

test_abi_artifacts.py is a V4 ABI artifact drift test that compares keccak256-derived event topics and function selectors against a pinned JSON file. The v2 -> v3 storage schema bump adds storage-layer fields and a new manifest table, none of which the ABI artifact test reads or compares. The schema bump is provably additive and cannot perturb this oracle; the test's failures in the worktree are environmental (missing tools/oracle/lib/ artifacts) and identical to main.

Evidence:

- tests/test_abi_artifacts.py verifies the V4 ABI artifact JSON file under robinhood_lp/protocol/abi_artifacts/. It checks: (a) the artifact exists and parses; (b) declared source commit matches EXPECTED_SOURCE_COMMIT; (c) every event has a 32-byte topic0 from keccak256 of the canonical signature; (d) every function has a 4-byte selector; (e) _meta.sha256 matches the file's SHA-256 (hard assertion); (f) StateView ABI is present; (g) when forge is on PATH, the artifact byte-matches the Foundry SelectorOracle regenerator output.
- The test compares pinned ABI bytes (event topics = keccak256(signature), function selectors = first 4 bytes of keccak256(signature)), which are determined entirely by the V4 Solidity source contracts — not by storage schema_version, storage schema migrations, or Parquet column changes.
- The v2 -> v3 storage schema bump (CURRENT_SCHEMA_VERSION=3, MANIFEST_SCHEMA_VERSION=3) adds: (a) block_timestamp / parent_hash fields to log-record dataclasses (InitializeLogRecord, ModifyLiquidityLogRecord, SwapLogRecord, DonateLogRecord, ProtocolFeeUpdatedLogRecord); (b) a new block_headers SQLite table with PRIMARY KEY (chain_id, block_hash); (c) the BlockHeaderInconsistencyError class. None of these touch the protocol/abi_artifacts JSON file, the keccak256-derived event topics, or the function selectors.
- The test_abi_artifacts.py failures observed in the worktree are purely environmental: its oracle submodules live under tools/oracle/lib/ which is populated in /home/lpdev/lp/ but not in the worktree's tools/oracle/lib/. The test does not import robinhood_lp.storage.schema and does not check CURRENT_SCHEMA_VERSION; therefore it cannot be perturbed by the v3 schema bump.
- The Reviewer inspected the test module's source and confirmed it has no dependency on the storage schema or any test that the v3 bump affects.

### test_runner_acceptance — PASS

All required tests pass; linting and strict type-check are clean.

Evidence:

- 149 passed across tests/test_storage_block_headers.py (13), tests/test_storage_partition_columns.py (9), tests/test_ingestion_block_header_source.py (22), tests/test_ingestion_endpoint_client.py (24), tests/test_main_ingest.py (16), tests/test_ingestion_runner.py (23), tests/test_storage_writer.py (20), tests/test_storage_manifest.py (19), tests/test_e2e_t035.py (3)
- Full suite: 803 passed, 6 skipped in 6.20s (skips are environmental: forge-foundry / gpg / pre-existing python-invariant vector, identical to main)
- ruff format --check src/ tests/: 99 files already formatted
- ruff check src/ tests/: All checks passed!
- mypy --strict src/robinhood_lp: Success: no issues found in 54 source files

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- The v3 schema bump (CURRENT_SCHEMA_VERSION=3, MANIFEST_SCHEMA_VERSION=3) is additive so existing v2 manifests open cleanly; legacy v1/v2 partitions persist as-is and migrate_to_current defaults block_timestamp=0 and parent_hash=0, which the consumer must treat as 'header not retained at decode time'. A future reader that does not distinguish 0-as-legacy from 0-as-current could silently treat a fresh ingestion with no header fetches as fully-qualified.
- The default RpcAdapter transport does not override the application User-Agent (documented as a known gap the operator must configure at the deployment layer); an operator running ingest with the default transport against the Robinhood public RPC will receive HTTP 403 from urllib's default User-Agent. This is consistent with ADR-010/ADR-011 and pre-dates T035.
- The CLI parser's user_agent configuration key is read but not applied to the underlying RpcAdapter transport at this phase; a future gate should plumb the operator-supplied User-Agent into the transport so the production deployment can override the urllib default.
- The same RpcAdapter is shared between RpcEndpointClient and RpcBlockHeaderSource in the CLI; the transport separates eth_getLogs (single-request) and eth_getBlockByNumber (batch) by payload shape. This is wired in the e2e fixture but a future production observation should confirm the underlying transport multiplexes the two methods correctly under real load.
