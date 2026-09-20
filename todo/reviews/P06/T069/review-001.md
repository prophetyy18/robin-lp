# T069 independent review

- Base commit: `75a7e5446a9f431c424c715c743ed3bf48f68124`
- Candidate commit: `2f64e64c8d36d19e699c130f367f95f6a071b1f2`
- Verdict: **FAIL**

## Checks

### dependencies_approved — PASS

All five declared dependencies are APPROVED in todo/config.yaml.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/todo/config.yaml T041/T053/T061/T068/T105 all show status=APPROVED

### module_version_pinned — PASS

The orchestrator module is versioned and exposed.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/src/robinhood_lp/orchestrator/__init__.py line 146 ORCHESTRATOR_VERSION='t069.backtest_orchestrator.v1'
- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py TestModuleVersion passes

### layer_purity_orchestrator — PASS

Orchestrator imports only the documented layers and the layer_map reflects ADR-006 placement.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/src/robinhood_lp/orchestrator/__init__.py lines 90-142 only import backtest.engine/events/models, reports.manifest/metrics/registry_binding/validation, strategy.adapter/adaptive/registry and the stdlib; no rpc, storage, signing, execution, web, or presentation imports
- /home/lpdev/lp-worktrees/review-t069-attempt-001/tools/check_imports/layer_map.py added robinhood_lp.orchestrator to 'application / orchestration'
- /home/lpdev/lp-worktrees/review-t069-attempt-001/tools/check_imports/check runs clean

### run_request_validation — PASS

RunRequest.__post_init__ rejects every malformed input (empty run_id, invalid clock/fill/cost/quote, empty numeraire, etc.).

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py TestRunRequestValidation 14 tests pass

### successful_publication_one_manifest_per_request — PASS

A successful run publishes exactly one T105 manifest and one report under the runs root.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py TestSuccessfulRunPublication test_single_pool_run_publishes_manifest_and_report passes; manifest version == MANIFEST_VERSION ('t105.experiment_manifest.v1') and strategy_identity == IDENTITY_HOLD

### two_heterogeneous_pools_one_run_identity — FAIL

The orchestrator runs one pool per RunRequest, so two pools are published under two distinct run_ids, not under one run identity. The test docstring explicitly acknowledges the gap ('orchestrator itself runs one pool per request ... issuing two requests with the same run_id is rejected (RunAlreadyExists)'). This deviates from the literal acceptance text 'one manifest per member pool under one run identity'; the contract is interpreted as 'one manifest per member pool, each under its own run identity'.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py test_two_heterogeneous_pools_share_run_identity uses two separate RunRequests with run_id='multi-001' and run_id='multi-002'
- T069 contract acceptance: 'a new run over two heterogeneous pool fixtures produces exactly the registry-bound manifest and reports the T105 contract defines, one manifest per member pool under one run identity'
- RunRequest names exactly one (chain_id, pool_key_id); BacktestOrchestrator.submit publishes one manifest per request; no shared run_identity / RunIdentity is constructed across pool requests

### byte_equivalent_rerun — FAIL

The acceptance clause for byte-equivalent re-runs is not actually verified: the test contains trivial tautologies and never performs a fresh re-run. The underlying orchestrator IS deterministic (the only differing fields across two fresh runs with identical inputs are run_id and report_checksum, both expected), but the test does not verify this.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py test_same_request_produces_byte_equivalent_manifests asserts only 'reloaded_manifest_bytes == first_manifest_bytes' (re-reading the same on-disk file twice) and 'first_report_bytes == first_report_bytes' / 'first_record_bytes == first_record_bytes' (trivial tautologies)
- T069 contract acceptance: 're-running the same request reproduces byte-equivalent canonical artifacts (test)'
- Manual reproduction with two fresh RunRequests (run_id='rerun-A', 'rerun-B') and identical content shows the manifest bytes differ at position 2305 (run_id field) and report_checksum (derived from run_id) — no test exercises this

### cancellation_and_failure_closed — PASS

Cancellation, unknown identity, missing dataset, out-of-coverage range, and legacy pre-registry source all fail closed with a T069_ reason code and no manifest/report.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py TestCancellationAndFailureClosed 5 tests pass: cancellation, unknown strategy identity, missing dataset, block range outside coverage, legacy manifest source

### run_state_store_atomic_immutable — PASS

RunStateStore writes atomically via temp-file rename and refuses to overwrite a terminal record; the orchestrator returns the persisted record on duplicate submit of a terminal run.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py TestRunStateStore 4 tests pass (atomic round trip, terminal-record immutable, invalid state rejected, list_runs ordering)

### restart_while_running — PASS

A restart while a run is RUNNING neither duplicates the run nor loses its state; resume_in_flight picks up the in-flight record and drives it to a terminal state.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py TestRestartInFlight test_resume_in_flight_picks_up_running_records passes; only one record exists after restart

### product_rerun_preserves_source — PASS

A product rerun creates a new durable RunRecord with source_manifest_path / source_checksum and leaves the source manifest byte-identical.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py TestProductRerunPreservesSource test_product_rerun_creates_new_record_and_preserves_source passes; source manifest bytes equal before and after, and the store contains both run records

### no_credentials_in_run_record — PASS

The run record carries no key material, endpoint alias, or secret.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py TestRunRecordContainsNoCredentials test_record_payload_contains_no_key_material passes; the forbidden tokens list includes 'private_key', 'PRIVATE_KEY', 'seed_phrase', 'mnemonic', 'rpc_secret', 'API_KEY', 'api_key', 'ALIAS', 'WEBHOOK', 'PASSWORD', 'password='

### cli_no_web_process — PASS

backtest start, list, observe, cancel (and resume) work via CLI with no Web process running.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py TestCLISubcommandsWithoutWeb test_cli_start_observe_cancel and test_cli_start_invalid_request_returns_nonzero pass; subprocess invokes 'python -m robinhood_lp backtest start/observe/cancel' and verifies the JSON record round-trips with no Web session

### orchestrator_composes_existing_surfaces — PASS

The orchestrator composes the existing T061 / T063 / T105 / T068 surfaces rather than re-implementing them.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/tests/test_backtest_t069.py TestOrchestratorComposition 4 tests pass; orchestrator module re-exports BacktestEngine, empty_position_state, compute_run_metrics, build_coverage_summary, extract_decisions, decisions_checksum, LedgerSnapshot, build_experiment_manifest, validate_manifest, write_manifest_to_path, bind_strategy_to_registry, UnknownStrategyIdentityError

### rerun_manifest_cli_not_absorbed — PASS

The T105 artifact rerun command stays an artifact operation carrying no T069 run state.

Evidence:

- /home/lpdev/lp-worktrees/review-t069-attempt-001/src/robinhood_lp/__main__.py _run_rerun_manifest (lines 282-404) imports only from robinhood_lp.reports / reports.registry_binding / reports.validation — no orchestrator or RunStateStore
- The 'rerun-manifest' subcommand is registered alongside 'ingest' and 'backtest' as separate subcommands

### offline_tooling — PASS

All offline tooling passes; the new test module is fully green and the rest of the suite is unaffected.

Evidence:

- /tmp/lp-t001-cleanroom-venv/bin/ruff check src/ tests/ tools/ → All checks passed
- /tmp/lp-t001-cleanroom-venv/bin/mypy src/ → Success: no issues found in 131 source files
- PYTHONPATH=src /tmp/lp-t001-cleanroom-venv/bin/python -m tools.check_imports.check → clean
- /tmp/lp-t001-cleanroom-venv/bin/python -m tools.check_acceptance check → acceptance check passed: no findings
- /tmp/lp-t001-cleanroom-venv/bin/python -m tools.check_citations check → citation check passed: no findings
- PYTHONPATH=src /tmp/lp-t001-cleanroom-venv/bin/python -m pytest tests/test_backtest_t069.py → 38 passed in 1.11s
- PYTHONPATH=src /tmp/lp-t001-cleanroom-venv/bin/python -m pytest --no-header --ignore=tests/test_abi_artifacts.py → 2685 passed, 6 skipped in 27.98s (the 6 skips are pre-existing forge / gpg / unsorted-currency skips unrelated to T069)

## Must-not violations

- None.

## Unknowns

- Multi-pool 'under one run identity' is interpreted in the test as separate run_ids per pool; the contract text is ambiguous between 'shared run identity' and 'each member pool under its own run identity'.

## Required changes

- Fix tests/test_backtest_t069.py::TestRerunReproducibility::test_same_request_produces_byte_equivalent_manifests so it actually performs a fresh re-run with a new run_id and compares the manifest's canonical content. The current assertions include trivial tautologies (e.g. 'first_report_bytes == first_report_bytes') and re-read the same on-disk file twice, which means the acceptance clause 're-running the same request reproduces byte-equivalent canonical artifacts (test)' is not verified by the test even though the underlying orchestrator is deterministic.

## Residual risks

- The orchestrator intentionally builds its own private _build_model_bundle that mirrors the canonical T105 deterministic model bundle; this is a small duplication with src/robinhood_lp/reports/rerun.py::_build_model_bundle but the two serve different purposes (live run vs. artifact rerun from saved manifest).
- The default risk callback (_approve_risk_callback) approves every decision; wiring the T070 risk gateway is a deliberate follow-up tracked in the orchestrator's docstring.
- tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output fails in this worktree due to a missing forge / Solidity library; the failure is environmental and unrelated to T069 (CI installs Foundry and clones the libraries).
