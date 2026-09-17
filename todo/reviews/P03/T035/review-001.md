# T035 independent review

- Base commit: `c01d1a0e23c9c365bfbf90c6a7feacbab6e9688e`
- Candidate commit: `e272b6759fc3bb462f288811997cb49037e4a609`
- Verdict: **FAIL**

## Checks

### block_header_persistence_schema_record — PASS

The T030 logical header record now carries block_number, parent_hash, and block_timestamp on every event log record. Verified by the new storage_schema fields and migration tests.

Evidence:

- src/robinhood_lp/storage/schema.py diff lines 410-634 add block_timestamp (int) and parent_hash (int) to InitializeLogRecord, ModifyLiquidityLogRecord, SwapLogRecord, DonateLogRecord, ProtocolFeeUpdatedLogRecord, all keyed implicitly by the record's EventKey (chain_id, block_hash)
- _validate_block_timestamp and _validate_parent_hash helpers enforce uint64 / uint256 widths
- CURRENT_SCHEMA_VERSION bumped from 2 to 3 with v2 -> v3 migration defaulting both new fields to 0
- tests/test_storage_partition_columns.py::test_v2_record_migrates_to_current_with_zero_header_placeholders round-trips the migration
- tests/test_storage_schema.py (52 passed)

### block_header_persistence_parquet_columns — PASS

T031 Parquet partition columns block_timestamp (uint64) and parent_hash (binary(32)) are present and required on every event row.

Evidence:

- src/robinhood_lp/storage/partition.py diff lines 233-244 add pa.field('block_timestamp', pa.uint64(), nullable=False) and pa.field('parent_hash', pa.binary(HASH_BYTES), nullable=False) to SHARED_PARQUET_FIELDS so every per-event Parquet schema inherits both
- src/robinhood_lp/storage/writer.py diff lines 183-191 serialize record.block_timestamp (uint64) and record.parent_hash (binary(32)) into the row dict
- src/robinhood_lp/storage/reader.py diff lines 319-324 deserialise the columns back via _b('parent_hash', length=32) and int(row.get('block_timestamp') or 0)
- tests/test_storage_partition_columns.py verifies uint64 / fixed_size_binary[32] types and the round-trip

### block_header_persistence_manifest_table — PASS

The manifest block_headers table holds exactly one deduplicated row per distinct event block, keyed by (chain_id, block_hash), with the three carrier fields and credential-bearing-URL protection.

Evidence:

- src/robinhood_lp/storage/manifest.py diff lines 200-228 add the block_headers table keyed by PRIMARY KEY (chain_id, block_hash) with block_number, parent_hash, block_timestamp, fetched_at, endpoint_alias columns
- MANIFEST_SCHEMA_VERSION bumped from 2 to 3
- ManifestStore.upsert_block_header enforces idempotency on re-observation and raises BlockHeaderInconsistencyError when block_number, parent_hash, or block_timestamp drifts on the same (chain_id, block_hash)
- ManifestStore.get_block_header, list_block_headers, count_block_headers read helpers are exposed
- validate_endpoint_alias refuses credential-bearing URLs (test_storage_block_headers.py::test_upsert_block_header_rejects_credential_bearing_alias)
- tests/test_storage_block_headers.py (12 tests) covers dedup, idempotency, drift guard, kill/restart atomicity, and credential-bearing-URL refusal

### real_block_header_source_class_exists_and_calls_eth_getBlockByNumber — FAIL

A real BlockHeaderSource class is implemented and unit-tested, but the IngestionRunner never constructs BlockHeaderCache or invokes get_header / get_headers_batch, so a real ingestion run issues zero eth_getBlockByNumber calls, the block_headers manifest table stays empty, and persisted events carry block_timestamp=0 / parent_hash=0 (the v3 dataclass default, not header evidence). The contract Outcome that 'the resulting dataset carries block-time and parent-hash evidence' is not satisfied.

Evidence:

- src/robinhood_lp/ingestion/block_header_source.py defines RpcBlockHeaderSource with get_header(n) and get_headers_batch(block_numbers) that builds JSON-RPC batches of eth_getBlockByNumber(hex(n), false) calls and reports logical-call vs HTTP-batch split via BlockHeaderMetrics
- tests/test_ingestion_block_header_source.py (22 tests) cover the source class: dedup, batch split, batch retry path, cache hits, payload decode, rejection of blockTimestamp, alias validation, batch_size validation
- BUT: src/robinhood_lp/ingestion/runner.py IngestionRunner.header_cache (BlockHeaderCache | None = None) is declared as a dataclass field but is never assigned in __init__/__post_init__ and never read in the runner body; grep for self.header_cache in src/ returns no callers
- src/robinhood_lp/__main__.py constructs an IngestionRunner but does NOT construct an RpcBlockHeaderSource, does NOT construct a BlockHeaderSink, and does NOT register or wire them
- Empirical end-to-end run with a fake RpcAdapter returning a synthetic log row at block 105 produced a successful run_intervals row (1 interval, 1 normalized row) but ZERO rows in block_headers (verified via sqlite3 SELECT count(*) FROM block_headers) and ZERO Parquet partition files (verified via pathlib rglob *.parquet)
- The contract Outcome requires 'the resulting dataset carries block-time and parent-hash evidence pinned to canonical block headers'; the implementation provides a non-invoked class instead

### header_source_dedup_one_logical_call_per_distinct_event_block — UNKNOWN

The dedup invariant is implemented and tested in isolation, but is not exercised end-to-end because the source is never wired into the runner. An end-to-end proof is UNKNOWN.

Evidence:

- RpcBlockHeaderSource.get_headers_batch deduplicates distinct block_numbers via set() and only fetches uncached blocks, and increments logical_get_block_by_number_calls by len(batch) per HTTP batch
- BlockHeaderMetrics exposes logical_get_block_by_number_calls, http_batch_requests, header_cache_hits, header_fetch_failures
- BUT: because the runner never invokes the source, the invariant 'logical-call count equals the deduplicated distinct event block count' is never exercised in a real run
- tests/test_ingestion_block_header_source.py::test_source_get_headers_batch_dedups_and_counts_logical_calls and test_source_get_headers_batch_dedups_repeated_block_numbers verify the dedup math in isolation

### reject_blockTimestamp_as_time_source — PASS

eth_getLogs.blockTimestamp is never used as a time source; block time comes exclusively from the non-hydrated eth_getBlockByNumber header.

Evidence:

- BlockHeader dataclass in block_header_source.py has fields block_number, block_hash, parent_hash, timestamp only — no blockTimestamp attribute
- RpcEndpointClient.call_get_logs never extracts or promotes blockTimestamp; the raw row preserves the field as observational but the client never stores it on the typed record
- test_call_get_logs_never_writes_block_timestamp asserts the client does not promote blockTimestamp
- test_source_rejects_block_timestamp_field_as_time_source asserts the BlockHeader dataclass has no blockTimestamp attribute
- test_storage_partition_columns.py asserts the Parquet columns are block_timestamp (uint64) and parent_hash (binary(32)), not blockTimestamp

### production_endpoint_client_bridges_rpc_adapter_to_router — PASS

The production EndpointClient correctly bridges RpcAdapter.eth_get_logs to the router's measured-capability + remaining-budget failover path with classified reason codes.

Evidence:

- src/robinhood_lp/ingestion/endpoint_client.py defines RpcEndpointClient with call_get_logs(from_block, to_block, address, topics) -> EndpointCallResult that delegates to RpcAdapter.eth_get_logs and translates results / errors into T032 reason codes (HTTP 429 / 403 / 5xx / 4xx, RPC timeout, invalid response, OK)
- _classify_transport_error preserves the precise status code when RpcRetryExhausted wraps the original TransportError so a blanket rpc_timeout is not surfaced
- get_block_hash_for_pin provides the pinned-block-hash helper the router needs for scanned_empty evidence
- tests/test_ingestion_endpoint_client.py (22 tests) cover success, empty, error translation, retry-exhausted-with-preserved-429, pin helper, row size accounting, and validation

### minimal_readonly_operator_entrypoint_no_signing_material — PASS

The python -m robinhood_lp ingest entry point is read-only: no signing material surface, no eth_call issuance during acquisition, and the CLI parser surface is grep-verified free of forbidden flags.

Evidence:

- src/robinhood_lp/__main__.py adds the `ingest` subcommand with --config, --data-root, --from-block, --to-block, --run-id arguments; prints run_id and complete JSON to stdout; exits 0 on complete, 1 on failure
- tests/test_main_ingest.py::test_ingest_subcommand_does_not_accept_signing_arguments walks parser._actions and asserts no --private-key / --keystore / --seed / --password / --sign / --broadcast / --submit / --send / --wallet / --tx / --transaction flags
- tests/test_main_ingest.py::test_ingest_subcommand_runs_paper_mode_only and test_ingest_subcommand_does_not_log_signing_material assert --help advertises no live-execution flags
- tests/test_main_ingest.py::test_ingest_subcommand_does_not_call_eth_call injects a fake RpcAdapter whose eth_call raises AssertionError if invoked; the end-to-end run completes without calling it
- tests/test_no_signing_paths.py (6 passed) enforces the Phase 0-8 boundary project-wide

### provider_facts_artifact_under_docs_implement_protocol_artifacts — PASS

The measured provider-facts artifact is in place and machine-readable. Retrieval time, chain id, endpoint aliases, observed block number, and the facts this contract depends on are all present.

Evidence:

- docs/implement/protocol-artifacts/rpc-mainnet-free-provider-facts-2026-09-17.json added as a new file with schema_version=1, observed_at_utc=2026-09-17T00:00:00Z, chain_id=4663, observed_head_block=65392511, the five endpoint aliases, reference target, result-bearing measurement (3739 events across 3266 distinct blocks), rejected paths (timeout and limit rejection with the exact provider error codes), throttling (HTTP 429), secondary capability (~10 blocks per eth_getLogs), JSON-RPC batch support on both endpoints, historical-state availability difference, credential policy (no credential-bearing URL stored), limitations section, and t035_dependent_facts list
- The artifact matches the planning-layer record docs/spec/protocol/PROVIDER_FACTS.md

### ingestion_runner_writes_parquet_partitions — FAIL

The runner never invokes writer.append_partition. Events from eth_getLogs are recorded in the SQLite run_intervals manifest but are never written to Parquet. The acceptance clause 'every persisted event carries block_timestamp and parent_hash' cannot be evaluated because no event is ever persisted.

Evidence:

- IngestionRunner.writer: RawPartitionWriter is declared as a dataclass field on src/robinhood_lp/ingestion/runner.py:211
- grep for self.writer.append_partition across src/ returns ZERO matches; the runner never calls the writer
- Empirical end-to-end run with a fake RpcAdapter returning a synthetic log row produced run_intervals row (1, 'test-real-2', 100, 200, 'robinhood_public', 'successful', 'ok', 1, 692, 1, 1, ...) but partitions table count=0 and event_index count=0 and no Parquet files under data/

### must_not_sign_or_broadcast — PASS

Phase 0-8 boundary preserved; no signing, broadcasting, or key material is added.

Evidence:

- tests/test_no_signing_paths.py (6 passed)
- tests/test_main_ingest.py grep-style check on the CLI parser surface
- No signing material in any of the new modules (block_header_source.py, endpoint_client.py, __main__.py)
- The entry point's default transport does not override the application User-Agent; signing is explicitly out of scope

### must_not_introduce_economic_or_risk_parameters — PASS

No economic or risk parameter changes introduced.

Evidence:

- Diff contains no economic or risk parameter changes; no new schema fields for risk, position size, fee tier, or PnL
- All numeric constants added are technical widths (uint64, uint256, hash bytes) and the batch_size=16 default
- 5-minute USDG rules, NO_NEW_RISK, confirmations=12, and other risk surfaces untouched

### must_not_treat_blockTimestamp_as_time_source — PASS

eth_getLogs.blockTimestamp is not used as a time source; the time carrier is the non-hydrated header's integer timestamp.

Evidence:

- BlockHeader dataclass carries 'timestamp' (int, the integer UNIX-seconds block time the non-hydrated eth_getBlockByNumber returns) and never references blockTimestamp
- RpcEndpointClient.call_get_logs does not write a typed block_timestamp from the log row

### must_not_widen_pool_chain_or_poolkey — PASS

Pool/chain/PoolKey scope unchanged.

Evidence:

- Diff modifies only implementation files; no pool, chain, or PoolKey widening
- The ingest entry point configures one chain_id, one pool_id, one pool_manager_address, one pool_init_block per run
- The CLI parser exposes no flag for adding multiple pools/chains/PoolKeys

### must_not_rewrite_T030_to_T034_contracts — PASS

Contract text unchanged.

Evidence:

- git diff c01d1a0 e272b6759 --name-only shows no file under todo/phases/ is modified
- git diff shows docs/spec/protocol/PROVIDER_FACTS.md is untouched (only a new JSON artifact under docs/implement/protocol-artifacts/)

### must_not_scan_block_by_block_or_per_block_eth_call — PASS

No block-by-block scanning, no per-block eth_getLogs, no per-block or per-event eth_call.

Evidence:

- The new RpcEndpointClient.call_get_logs takes a from_block/to_block range and never iterates block-by-block
- The router/failover path enforces one eth_getLogs call per planned sub-range, never one per block
- tests/test_main_ingest.py::test_ingest_subcommand_does_not_call_eth_call asserts no eth_call is issued during acquisition
- BlockHeaderSource has no 'scan all blocks' helper

### must_not_reconstruct_pool_state_from_rpc — PASS

Pool state is not reconstructed from RPC during acquisition.

Evidence:

- No state reconstruction code is added; the only RPC reads during acquisition are eth_getLogs and eth_getBlockByNumber
- StateView / fee-growth / liquidity observations remain in scope of Phase 4/5 (T040+); the entry point's data flow is events + headers only

### test_runner_acceptance — PASS

Ruff format/check, strict mypy, and pytest are all clean for the modules and broader related set; no flake or type error in the new code.

Evidence:

- 91 passed in 0.83s: tests/test_storage_block_headers.py + tests/test_storage_partition_columns.py + tests/test_ingestion_block_header_source.py + tests/test_ingestion_endpoint_client.py + tests/test_main_ingest.py + tests/test_storage_measurement.py
- 215 passed: tests/test_main_ingest.py + tests/test_ingestion_runner.py + tests/test_ingestion_router.py + tests/test_ingestion_planner.py + tests/test_storage_partition_columns.py + tests/test_storage_block_headers.py + tests/test_storage_schema.py + tests/test_storage_writer.py + tests/test_storage_reader.py + tests/test_storage_partition.py + tests/test_storage_manifest.py
- 800 passed, 6 skipped (full suite with --ignore=tests/test_abi_artifacts.py); the 6 skips are documented (forge-foundry / gpg / pre-existing python-invariant vector) and identical to main
- 52 passed: tests/test_storage_schema.py
- tests/test_no_signing_paths.py 6 passed
- ruff format --check: 98 files already formatted
- ruff check: All checks passed
- mypy --strict: Success: no issues found in 54 source files

## Must-not violations

- None.

## Unknowns

- The RpcBlockHeaderSource's batch retry path uses the adapter's per-call path for failed batches; the logical-call counter increments by len(batch) on success and by len(batch) again on the retry path, so a flaky batch can briefly surface more logical calls than the deduplicated distinct block count until the run reaches a steady state (Developer noted this as a residual risk in todo/evidence/P03/T035/attempt-001-developer.json). The deduplication invariant is implemented but its end-to-end reconciliation in a real run is UNKNOWN because the source is not wired.
- End-to-end behaviour of block-pinned state reads at depth (B can serve historical state at reference depth, A cannot) is not exercised by any T035 test fixture; the routing rule is documented in the provider-facts artifact but not enforced by a test.
- tests/test_abi_artifacts.py is excluded from the Reviewer pytest run because its oracle submodules live under tools/oracle/lib/ which is populated in /home/lpdev/lp/ but not in the worktree's tools/oracle/lib/; the test_abi_artifacts.py test failure is environmental and present on /home/lpdev/lp/ as well (Developer noted). The Reviewer could not byte-for-byte confirm that the v3 schema bump does not perturb the pre-existing test_abi_artifacts oracle.

## Required changes

- Wire RpcBlockHeaderSource into IngestionRunner so a real ingestion run actually calls eth_getBlockByNumber(hex(n), false) per distinct event block: the runner's header_cache field must be initialized with an RpcBlockHeaderSource (or BlockHeaderCache(source=...)) so that for every event block observed the integer block timestamp and parent hash are populated from the source and not defaulted to 0. Until then, the contract Outcome that 'the resulting dataset carries block-time and parent-hash evidence pinned to canonical block headers' is not met, and the acceptance clause 'every persisted event carries block_timestamp and parent_hash, and for every event block (block_number, block_hash, parent_hash, timestamp) equals the values read directly from eth_getBlockByNumber at validation time' is UNVERIFIABLE.
- Wire BlockHeaderSink (or equivalent) into IngestionRunner so the dedup'd block_headers manifest table is populated by the same run that fetches the headers. The acceptance clause 'the manifest header table holds exactly one row per distinct event block' requires end-to-end persistence, which the current implementation does not exercise (verified empirically: a run_intervals row exists but block_headers count=0).
- Wire the RawPartitionWriter into IngestionRunner so successful eth_getLogs rows actually flow to Parquet partitions. Empirically verified: an end-to-end run with a fake RpcAdapter produces 1 normalized row and 0 Parquet partitions; self.writer.append_partition is never invoked from any src/ module. Without this, the contract Outcome 'the resulting dataset carries block-time and parent-hash evidence' is not observable; complete=true with partitions=0 is not a passing acceptance per T032's 'every requested block is covered exactly once by either a successful interval or an explicitly recorded empty interval' clause.
- Add at least one end-to-end fixture that exercises the full real-mainnet path (RpcEndpointClient.call_get_logs -> RpcBlockHeaderSource.get_headers_batch -> RawPartitionWriter.append_partition -> ManifestStore.upsert_block_header) and asserts the acceptance clauses: events persisted with non-zero block_timestamp and non-zero parent_hash; the (block_number, block_hash, parent_hash, timestamp) tuple equals what the source returns; the block_headers manifest table has exactly one row per distinct event block; and no eth_call is issued.

## Residual risks

- The IngestionRunner's writer and header_cache are declared fields but not exercised end-to-end; if the next Developer wires them, additional calls-volume reconciliation work may surface (the RpcBlockHeaderSource batch retry path can transiently increment logical_get_block_by_number_calls by len(batch) twice on a flaky batch — see Developer evidence file).
- The v3 schema bump (CURRENT_SCHEMA_VERSION=3, MANIFEST_SCHEMA_VERSION=3) is additive so existing v2 manifests open cleanly; legacy v1/v2 partitions persist as-is and migrate_to_current defaults block_timestamp=0 and parent_hash=0, which the consumer must treat as 'header not retained at decode time'. A future reader that does not distinguish 0-as-legacy from 0-as-current could silently treat a fresh ingestion with no header fetches as fully-qualified.
- The default RpcAdapter transport does not override the application User-Agent (documented as a known gap the operator must configure at the deployment layer); an operator running ingest with the default transport against the Robinhood public RPC will receive HTTP 403 from urllib's default User-Agent. This is consistent with ADR-010/ADR-011 and pre-dates T035, but it is worth re-flagging for the production deployment.
