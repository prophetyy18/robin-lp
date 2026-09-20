# T069 independent review

- Base commit: `75a7e5446a9f431c424c715c743ed3bf48f68124`
- Candidate commit: `1569f13252bada1ba0e8c2a800efbd845d5b9da9`
- Verdict: **TRIAGE_REQUIRED**

## Checks

### dependencies_approved — PASS

All five declared dependencies are APPROVED in todo/config.yaml.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-002/todo/config.yaml T041 status APPROVED
- /home/lpdev/lp-worktrees/review-t069-attempt-002/todo/config.yaml T053 status APPROVED
- /home/lpdev/lp-worktrees/review-t069-attempt-002/todo/config.yaml T061 status APPROVED
- /home/lpdev/lp-worktrees/review-t069-attempt-002/todo/config.yaml T068 status APPROVED
- /home/lpdev/lp-worktrees/review-t069-attempt-002/todo/config.yaml T105 status APPROVED

### module_version_pinned — PASS

ORCHESTRATOR_VERSION is pinned to 't069.backtest_orchestrator.v1' and exposed at the module level; the contract's version-pinning requirement is satisfied.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-002/src/robinhood_lp/orchestrator/__init__.py line 146 ORCHESTRATOR_VERSION='t069.backtest_orchestrator.v1'
- tests/test_backtest_t069.py TestModuleVersion::test_orchestrator_version_is_t069 passes

### layer_purity_orchestrator — PASS

The orchestrator imports only the documented layers (backtest, reports, strategy) and the stdlib; no RPC, storage, signing, execution, web, or presentation imports were introduced. The layer_map now classifies robinhood_lp.orchestrator as 'application / orchestration'.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-002/src/robinhood_lp/orchestrator/__init__.py lines 90-142 import only from robinhood_lp.backtest.engine, backtest.events, backtest.models, reports.manifest, reports.metrics, reports.registry_binding, reports.validation, strategy.adapter, strategy.adaptive, strategy.registry and stdlib
- /home/lpdev/lp-worktrees/review-t069-attempt-002/tools/check_imports/layer_map.py line 153 registers robinhood_lp.orchestrator under 'application / orchestration'
- PYTHONPATH=src /tmp/lp-t001-cleanroom-venv/bin/python -m tools.check_imports.check ran clean (no findings)

### run_request_validation — PASS

RunRequest.__post_init__ rejects every malformed input the T069 contract binds to a closed vocabulary or non-empty string. All 14 validation tests pass.

Evidence:

- tests/test_backtest_t069.py TestRunRequestValidation 14 tests pass (empty run_id, empty dataset_version, negative block_range_start, block_range_end<start, zero interval, empty strategy_identity, negative seed, invalid clock/fill/cost/quote, invalid valuation_qualification, empty reporting_numeraire, empty code_revision)

### successful_publication_one_manifest_per_request — PASS

A successful run over a single pool publishes exactly one T105 registry-bound manifest and one report; the record round-trips through the RunStateStore without mutation.

Evidence:

- tests/test_backtest_t069.py TestSuccessfulRunPublication::test_single_pool_run_publishes_manifest_and_report passes
- tests/test_backtest_t069.py TestSuccessfulRunPublication::test_run_record_serialisation_round_trips passes
- src/robinhood_lp/orchestrator/__init__.py _execute writes one manifest and one report per successful request, then transitions to RunState.SUCCEEDED with manifest_path/report_path recorded

### two_heterogeneous_pools_one_run_identity — UNKNOWN

The contract clause 'one manifest per member pool under one run identity' is ambiguous. The implementation supports the 'shared dataset / numeraire / qualification identity across multiple run_ids' reading, which the deliverables' singular 'the pool and block range' phrasing also supports. The prior reviewer classified this ambiguity under Unknowns (not Required Changes); the contract owner has not resolved it and I concur with the prior classification. The test does verify the spirit of the heterogeneous-pool contract: each pool gets its own manifest and the manifests share dataset / numeraire / qualification.

Evidence:

- tests/test_backtest_t069.py TestSuccessfulRunPublication::test_two_heterogeneous_pools_share_run_identity passes
- tests/test_backtest_t069.py test asserts manifest_a and manifest_b share dataset_version, reporting_numeraire, valuation_qualification but differ in pool_key_id
- src/robinhood_lp/orchestrator/__init__.py RunRequest declares a single pool_key_id; BacktestOrchestrator.submit publishes one manifest per request

### byte_equivalent_rerun — PASS

Attempt 2 fully repaired the required change from review-001: the test now actually performs a fresh re-run with a new run_id and compares canonical content byte-for-byte. The previous trivial tautologies ('first_report_bytes == first_report_bytes', 'first_record_bytes == first_record_bytes', re-reading the same on-disk manifest twice) were removed; the new _canonical_json helper provides deterministic byte-equal serialisation matching the on-disk writer.

Evidence:

- tests/test_backtest_t069.py TestRerunReproducibility::test_same_request_produces_byte_equivalent_manifests now performs two fresh submits against two independent orchestrator instances with independent store roots, independent event sources, and independent run_ids
- test strips run-id-derived fields (manifest.run_id/report_checksum, report.run_id/manifest_checksum, record.run_id/manifest_path/report_path/request.run_id) and asserts canonical JSON byte-equivalence of the remaining payload via _canonical_json helper
- tests/test_backtest_t069.py TestRerunReproducibility passes
- git diff 2f64e64c8d36d19e699c130f367f95f6a071b1f2..1569f13252bada1ba0e8c2a800efbd845d5b9da9 -- 'src/**' shows zero production-code changes between attempt 1 and attempt 2 (only tests/, config.yaml, evidence/, reviews/ changed)

### cancellation_and_failure_closed — PASS

Cancelled runs, unknown identities, missing datasets, out-of-coverage ranges, and legacy pre-registry sources all fail closed with a T069_ reason code and no manifest/report is published.

Evidence:

- tests/test_backtest_t069.py TestCancellationAndFailureClosed 5 tests pass: cancellation, unknown strategy identity, missing dataset, block range outside coverage, legacy manifest source

### run_state_store_atomic_immutable — PASS

RunStateStore writes atomically via temp-file rename; terminal records are read-only (a second write returns the persisted terminal record). Reloaded records round-trip through run_record_from_dict.

Evidence:

- tests/test_backtest_t069.py TestRunStateStore 4 tests pass (atomic round trip, terminal-record immutable, invalid state rejected, list_runs ordering)
- src/robinhood_lp/orchestrator/__init__.py RunStateStore.write uses tempfile.mkstemp + os.replace for atomic write

### restart_while_running — PASS

A restart while a run is RUNNING neither duplicates the run nor loses its state; resume_in_flight picks up the in-flight record and drives it to a terminal state.

Evidence:

- tests/test_backtest_t069.py TestRestartInFlight::test_resume_in_flight_picks_up_running_records passes; only one record exists after restart
- src/robinhood_lp/orchestrator/__init__.py resume_in_flight reads every RUNNING record and drives it through _execute

### product_rerun_preserves_source — PASS

A product rerun creates a new durable RunRecord linked to source_manifest_path/source_checksum and leaves the source manifest byte-identical.

Evidence:

- tests/test_backtest_t069.py TestProductRerunPreservesSource::test_product_rerun_creates_new_record_and_preserves_source passes; source manifest bytes equal before and after, and the store contains both run records

### no_credentials_in_run_record — PASS

The run record carries no key material, endpoint alias, or secret.

Evidence:

- tests/test_backtest_t069.py TestRunRecordContainsNoCredentials::test_record_payload_contains_no_key_material passes; the forbidden tokens list covers 'private_key', 'PRIVATE_KEY', 'seed_phrase', 'mnemonic', 'rpc_secret', 'API_KEY', 'api_key', 'ALIAS', 'WEBHOOK', 'PASSWORD', 'password='

### cli_no_web_process — PASS

backtest start, list, observe, cancel (and resume via the orchestrator) work via CLI with no Web process running; an invalid request fails closed before any result is written.

Evidence:

- tests/test_backtest_t069.py TestCLISubcommandsWithoutWeb::test_cli_start_observe_cancel passes; subprocess invokes 'python -m robinhood_lp backtest start/list/observe/cancel' and verifies the JSON record round-trips with no Web session
- tests/test_backtest_t069.py TestCLISubcommandsWithoutWeb::test_cli_start_invalid_request_returns_nonzero passes; an invalid request exits non-zero with no manifest written

### orchestrator_composes_existing_surfaces — PASS

The orchestrator composes the existing T061 engine, T063 metrics / coverage / decisions, T105 registry-bound manifest authority, and T068 registry without re-implementing any of them.

Evidence:

- tests/test_backtest_t069.py TestOrchestratorComposition 4 tests pass; orchestrator module re-exports BacktestEngine, empty_position_state, compute_run_metrics, build_coverage_summary, extract_decisions, decisions_checksum, LedgerSnapshot, build_experiment_manifest, validate_manifest, write_manifest_to_path, bind_strategy_to_registry, UnknownStrategyIdentityError
- src/robinhood_lp/orchestrator/__init__.py imports BacktestEngine, BacktestEvent, ModelBundle classes from robinhood_lp.backtest; ExperimentManifest / metrics helpers / bind_strategy_to_registry / validation / registry classes from robinhood_lp.reports / robinhood_lp.strategy

### rerun_manifest_cli_not_absorbed — PASS

The T105 artifact rerun command stays an artifact operation carrying no T069 run state.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-002/src/robinhood_lp/__main__.py _run_rerun_manifest (lines 282-404) imports only from robinhood_lp.reports / reports.registry_binding / reports.validation — no orchestrator or RunStateStore
- The 'rerun-manifest' subcommand is registered alongside 'ingest' and 'backtest' as separate subcommands at __main__.py lines 96 and 138

### no_mutation_of_stored_data — PASS

The orchestrator does not mutate stored partitions, approvals, risk configuration, or the strategy registry; only RunStateStore state is written.

Evidence:

- src/robinhood_lp/orchestrator/__init__.py _execute calls read_manifest_from_path / validate_manifest against the source but only reads; the product rerun path stores source_checksum and never overwrites the source file
- tests/test_backtest_t069.py TestProductRerunPreservesSource confirms the source manifest is byte-identical after the rerun
- src/robinhood_lp/orchestrator/__init__.py does not import robinhood_lp.storage or any partition reader

### offline_tooling — PASS

All offline tooling passes; the new test module is fully green and the rest of the suite is unaffected.

Evidence:

- /tmp/lp-t001-cleanroom-venv/bin/ruff check src/ tests/ tools/ → All checks passed!
- /tmp/lp-t001-cleanroom-venv/bin/mypy src/ → Success: no issues found in 131 source files
- PYTHONPATH=src /tmp/lp-t001-cleanroom-venv/bin/python -m tools.check_imports.check → clean (no output, no findings)
- /tmp/lp-t001-cleanroom-venv/bin/python -m tools.check_acceptance check → acceptance check passed: no findings
- /tmp/lp-t001-cleanroom-venv/bin/python -m tools.check_citations check → citation check passed: no findings
- PYTHONPATH=src /tmp/lp-t001-cleanroom-venv/bin/python -m pytest tests/test_backtest_t069.py → 38 passed in 0.99s
- PYTHONPATH=src /tmp/lp-t001-cleanroom-venv/bin/python -m pytest --no-header --ignore=tests/test_abi_artifacts.py → 2685 passed, 6 skipped in 28.20s (the 6 skips are pre-existing forge / gpg / unsorted-currency skips unrelated to T069)

## Must-not violations

- None.

## Unknowns

- Multi-pool 'under one run identity' is ambiguous between 'shared run_id (one RunRequest names multiple pools)' and 'shared dataset / numeraire / qualification identity'. The implementation and test support the latter interpretation, consistent with the deliverables' singular 'the pool' phrasing. The prior review recorded this under Unknowns (not Required Changes) and the contract owner has not resolved the ambiguity; I concur.

## Required changes

- None.

## Residual risks

- The orchestrator builds its own private _build_model_bundle that mirrors the canonical T105 deterministic model bundle; this is a small duplication with src/robinhood_lp/reports/rerun.py::_build_model_bundle but the two serve different purposes (live run vs. artifact rerun from saved manifest) and is unchanged from the approved base.
- The default risk callback (_approve_risk_callback) approves every decision; wiring the T070 risk gateway is a deliberate follow-up tracked in the orchestrator's docstring and is unchanged from the approved base.
- tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output fails in this worktree due to a missing forge / Solidity library; the failure is environmental and unrelated to T069 (CI installs Foundry and clones the libraries).
