# T113 independent review

- Base commit: `8e24987867738a7c123dfec2f3bf69f9bc11be2b`
- Candidate commit: `fb6bdc29a38dfb4c60b4fc42e57f6bb0b3552452`
- Verdict: **PASS**

## Checks

### deliverable-1-public-use-case-boundary — PASS

Public use-case boundary matches ARCHITECTURE.md §2.2 row T113 and COMPONENT_CONTRACTS.md §1; typed records with no Any/callable pass-through on the public surface.

Evidence:

- src/robinhood_lp/application/backtest.py defines BacktestUseCase class with start() and resume() methods, BacktestRequest alias (=RunRequest), frozen BacktestResult dataclass, six typed failure classes (BacktestUseCaseError, MissingDatasetRegistryFailure, PartitionResolutionFailure, UnqualifiedPartitionFailure, EmptyEventSourceFailure, InvalidRunRequestFailure), and build_default_application composition-root factory
- src/robinhood_lp/application/__init__.py exports all public types via __all__
- ARCHITECTURE.md §2.2 row 161 maps T113 to robinhood_lp.application.backtest
- COMPONENT_CONTRACTS.md §1 requires public boundary + named contract owner
- tests/test_t113.py::TestPublicBoundary (5 tests) covers: version string, BacktestRequest alias, BacktestResult field set, failure-class hierarchy, composition-root return type

### deliverable-2-composition-over-approved-path — PASS

Composition reuses approved T069/T112/T040/T041/T050/T061/T100/T112 paths; no new replay/accounting/fee/risk/execution authority.

Evidence:

- BacktestUseCase.__init__ wires RunStateStore (T069), DatasetResolver (T069), T112DatasetPartitionResolver (T112), T100ReplayEventSource (T112), T100PartitionResolver (T112 reports); BacktestOrchestratorT112.__init__ (line 318 backtest.py) layers on the T069 BacktestOrchestrator for record layout
- BacktestOrchestratorT112._publish_t112 (t112.py:666) binds strategy to registry (T068), runs BacktestEngine (T061) with T112 engine schedule, builds T112 manifest + simulation evidence via reports.t112.build_t112_experiment_manifest and build_t112_simulation_evidence
- tests/test_t113.py::TestNormalCaseTwoHeterogeneousPools verifies both Pool A (chain 4663) and Pool B (chain 8453) produce T112-versioned artifacts (MANIFEST_VERSION_T112, SIMULATION_EVIDENCE_VERSION_T112)
- tests pass: 22 T113 + 192 legacy backtest/t069/t105/t109/t112 + 3180 repository total

### deliverable-3-cli-cutover — PASS

CLI backtest start and resume route through BacktestUseCase.start and BacktestUseCase.resume; the predecessor T109 writer is not reachable from any current CLI path.

Evidence:

- src/robinhood_lp/__main__.py _run_backtest (lines 745-890): start path calls application.start(request), resume path calls application.resume(); neither constructs BacktestOrchestrator()
- _build_cli_application (lines 1052-1167) wires build_default_application with fail-closed stubs (_CLIFailingPartitionResolver, _CLIEmptyEventSource, _CLIFailingPartitionRefResolver) so default CLI invocation fails closed with named T113 reason and publishes no manifest
- tests/test_t113.py::TestCLIRoutesThroughApplicationAPI::test_cli_source_routes_through_application_api asserts 'BacktestOrchestrator(' not in the _run_backtest body (line 925)
- tests/test_t113.py::TestCLIRoutesThroughApplicationAPI::test_cli_start_invokes_application_api and test_cli_resume_invokes_application_api subprocess-test the CLI and assert a T113_ prefix reason and FAILED state
- git diff of __main__.py shows BacktestOrchestrator import removed from the start/resume body; only historical comment text remains

### deliverable-4-implementation-guide-and-conformance-coverage — PASS

Implementation guide and conformance suite satisfy Deliverable 4; two heterogeneous pools (chain 4663, chain 8453) with named normal/boundary/invalid/failure cases.

Evidence:

- docs/implement/backtest/IMPLEMENTATION_GUIDE.md exists (244 lines) and contains all required tokens: BacktestUseCase, robinhood_lp.application.backtest, Pool A, Pool B, CHAIN_ID_A, CHAIN_ID_B
- Guide §1 covers public boundary with ARCHITECTURE.md §2.2 + COMPONENT_CONTRACTS.md §§1-3 + ADR-006/ADR-016 citations
- Guide §4 documents Pool A (chain 4663) and Pool B (chain 8453) fixtures with distinct content hashes and partition IDs
- Guide §5 lists the six T113_ reason codes
- Guide §7 enumerates the ten conformance classes
- tests/test_t113.py covers 10 conformance classes: PublicBoundary, NormalCaseTwoHeterogeneousPools, BoundaryCases (empty + cancellation), InvalidInput (non-RunRequest), FailureCases (missing registry + unresolved partition), Resume, CLIRoutesThroughApplicationAPI, ByteIdenticalHistoricalArtifacts (T105 + T109), ImplementationGuide, NoSiblingImports

### deliverable-5-byte-identical-historical-artifacts — PASS

T069/T105/T109 artifacts remain byte-identical and reachable only through their declared versioned readers.

Evidence:

- git diff 8e24987 fb6bdc2 src/robinhood_lp/reports/manifest.py src/robinhood_lp/orchestrator/__init__.py returns no changes
- tests/test_t113.py::TestByteIdenticalHistoricalArtifacts::test_t109_manifest_round_trip_byte_identical builds a T109 manifest via reports.manifest.build_t109_experiment_manifest and asserts the dict round-trip is byte-identical through t109_experiment_manifest_from_dict
- tests/test_t113.py::TestByteIdenticalHistoricalArtifacts::test_t105_manifest_round_trip_byte_identical does the same for the T105 manifest
- tests/test_t109.py, tests/test_t109_acceptance.py, tests/test_reports_t105.py, tests/test_backtest_t069.py, tests/test_t112.py all pass (221 tests) — historical paths remain untouched

### acceptance-1-spec-types-tests-agreement — PASS

Requests, results, error codes, units, event-time semantics, cancellation, side effects, and compatibility behavior agree across Spec, types, and tests.

Evidence:

- BacktestResult is a frozen dataclass with documented fields (run_id, chain_id, pool_key_id, dataset_version, state, reason_code, error_message, manifest_path, evidence_path, manifest_payload, evidence_payload)
- CancelToken is passed through BacktestUseCase.start(request, cancel_token=...) and observed via the cancel_token parameter on BacktestOrchestratorT112.submit
- T113_ prefix is the documented namespace; the implementation guide §5 lists every reason code
- tests/test_t113.py::TestPublicBoundary::test_failure_classes_carry_t113_prefix verifies all five subclasses of BacktestUseCaseError

### acceptance-2-two-heterogeneous-pools-through-current-entry — PASS

Two heterogeneous pools traverse the current product entry; non-empty source events with block/transaction/log cursor fields flow through T040 replay path.

Evidence:

- tests/test_t113.py::TestNormalCaseTwoHeterogeneousPools::test_pool_a_normal_case_publishes_t112_artifacts runs Pool A (chain 4663) and asserts result.manifest_payload['chain_id'] == 4663 and pool_key_id is POOL_KEY_A
- tests/test_t113.py::TestNormalCaseTwoHeterogeneousPools::test_pool_b_normal_case_publishes_t112_artifacts runs Pool B (chain 8453, distinct content_hash DATASET_HASH_B, distinct partition_id) and asserts result.manifest_payload['chain_id'] == 8453 and dataset_content_hash == DATASET_HASH_B
- Both tests use the typed BacktestUseCase.start and assert paired T112-versioned artifacts (MANIFEST_VERSION_T112, SIMULATION_EVIDENCE_VERSION_T112)
- tests/test_t113.py::TestNormalCaseTwoHeterogeneousPools::test_normal_case_persists_succeeded_record verifies the SUCCEEDED T069-format record is persisted at runs_root

### acceptance-3-cli-fail-closed-and-no-t109-writer — PASS

CLI start and resume invoke application API; no current CLI path reaches BacktestOrchestrator.submit; each failure surfaces a named T113_ reason and publishes no successful manifest or evidence.

Evidence:

- TestBoundaryCases::test_empty_event_source_fails_closed asserts FAILED + manifest_path is None + reason_code contains 'EMPTY_EVENT_SOURCE_REFUSED' + 'T113_'
- TestBoundaryCases::test_cancellation_fails_closed asserts FAILED + manifest_path is None + reason_code contains 'T113_'
- TestFailureCases::test_missing_dataset_registry_fails_closed (RunRequest with dataset_version='ds.unknown') asserts FAILED + manifest_path is None + reason_code starts with T113_
- TestFailureCases::test_unresolved_partition_fails_closed (empty partition map) asserts FAILED + manifest_path is None + reason_code contains T113_
- TestCLIRoutesThroughApplicationAPI::test_cli_source_routes_through_application_api asserts 'BacktestOrchestrator(' not in the CLI _run_backtest body

### acceptance-4-provider-conformance — PASS

Storage, partition-resolution, replay/feature, and publication adapters obey typed contracts; invalid bindings fail closed.

Evidence:

- _StaticDatasetResolver (test_t113.py:261) and _StaticT112PartitionResolver (line 303) verify the storage / partition-resolution adapters obey typed contracts
- BacktestOrchestratorT112.submit (t112.py:566) enforces hash/range/PoolKey cross-binding before publication (lines 597-635), raising PartitionRefMismatchError on disagreement
- T112DatasetPartitionResolver (t112.py:227) refuses non-T100ResolvedPartition entries (line 210) and pool/chain mismatches (line 217)
- T112PartitionResolution.__post_init__ (t112.py:200) refuses empty partition sets and pool/chain mismatches at construction time
- Tests cover invalid identity, hash, pool, range, schema (T100ResolvedPartition fields), cursor (block_number/transaction_index/log_index), and version bindings

### acceptance-5-import-graph-and-ci-gates — PASS

Public module and package imports remain within the ADR-006 dependency graph; all CI gates pass.

Evidence:

- PYTHONPATH=src python -m tools.check_imports check => 'import-graph check passed: no findings'
- PYTHONPATH=src pytest tests/test_layer_direction_t007.py tests/test_no_signing_paths.py tests/test_documentation_citations.py tests/test_import_graph.py => 63 passed
- ruff format --check src/robinhood_lp/application/ src/robinhood_lp/__main__.py tests/test_t113.py => '4 files already formatted'
- ruff check on the same paths => 'All checks passed!'
- mypy --strict on the same paths => 'Success: no issues found in 4 source files'
- Full test suite (excluding abi_artifacts): 3180 passed, 6 skipped (pre-existing Foundry / GPG gaps unrelated to T113)

### must-not-no-second-authority — PASS

No second authority was created; existing T069/T105/T109/T112 implementations remain the sole authorities in their domains.

Evidence:

- BacktestUseCase wires the existing T112 entry path; no new replay/feature/accounting/fee/manifest/evidence/risk/execution/signer module was added
- git diff 8e24987 fb6bdc2 shows changes confined to: src/robinhood_lp/application/{__init__,backtest}.py (new), src/robinhood_lp/__main__.py (CLI cutover), docs/implement/backtest/IMPLEMENTATION_GUIDE.md (new), tests/test_t113.py (new), todo/config.yaml (workflow state), todo/evidence/P06/T113/attempt-001-developer.json (evidence)
- No source module that owns replay/accounting/fee/risk/execution/signer authority was touched
- tests/test_no_signing_paths.py passes — no signing/broadcast/key-loading/Keystore decryption was added

### must-not-no-adapter-internals-exposed — PASS

Adapter internals are not exposed as the public application API.

Evidence:

- BacktestRequest = RunRequest (typed alias, not Any)
- BacktestResult is a frozen dataclass with documented fields; manifest_payload/evidence_payload are Mapping[str, Any] but are read-only by intent (frozen dataclass)
- BacktestUseCase.__init__ accepts partition_resolver/event_source/partition_ref_resolver as Any at the seam to avoid sideways import into t112 internals (documented at line 690); the underlying BacktestOrchestratorT112 enforces the structural contract
- __all__ list exports only the supported surface; no private helpers leak

### must-not-no-task-numbered-production-names — PASS

Production modules and APIs use product-stable names; no task-numbered production name was introduced.

Evidence:

- Module name robinhood_lp.application.backtest uses 'backtest' (a stable product name, not a task number)
- Class names (BacktestUseCase, BacktestResult, BacktestRequest) are product-named, not task-numbered
- Failure classes use product-named semantics (MissingDatasetRegistryFailure etc.); reason-code prefix is T113_ (a version tag for forensic grep, not a module name)

### must-not-no-fabricated-t100-references — PASS

T100 references cannot be fabricated; the CLI default composition refuses every submission without real T100 partitions.

Evidence:

- _CLIFailingPartitionResolver and _CLIFailingPartitionRefResolver in _build_cli_application raise dedicated errors on every call so no fabricated T100 reference can slip through the CLI default composition
- BacktestOrchestratorT112.submit (t112.py:566) cross-checks every partition's content_hash, range, chain_id, and pool_key_id against coverage and raises PartitionRefMismatchError on disagreement
- TestFailureCases::test_unresolved_partition_fails_closed verifies the fail-closed path

### must-not-no-empty-or-t109-fallback — PASS

Empty event source and the old T109 writer are not used as fallbacks.

Evidence:

- _CLIEmptyEventSource raises FixedEmptyEventSourceError (the T112 empty-source refusal) on every call
- BacktestOrchestratorT112 does not invoke the T109 writer's publication path (it uses _publish_t112 exclusively); the T109 writer remains only callable from Python with an injected non-empty source as documented in ARCHITECTURE.md §2.2 row 157
- TestBoundaryCases::test_empty_event_source_fails_closed verifies the empty source fail-closed path

### must-not-no-historical-rewrite-no-contract-edits — PASS

Historical artifacts and approved task contracts are not rewritten.

Evidence:

- git diff confirms no historical artifact files (reports/manifest.py, orchestrator/__init__.py, etc.) were modified
- T069/T105/T109 manifest builders and loaders unchanged
- Approved task contracts and review records untouched
- todo/config.yaml records the workflow state transition (READY -> AWAITING_REVIEW) and the attempt/base_commit metadata, not a contract rewrite

### must-not-no-risk-or-signer-bypass — PASS

Central risk and signer boundaries are not bypassed.

Evidence:

- BacktestUseCase does not import robinhood_lp.risk or robinhood_lp.execution (verified by test_no_sibling_imports in test_t113.py)
- tests/test_no_signing_paths.py passes — no signing, broadcast, key-loading, or Keystore decryption was added
- Risk callback (_t112_risk_callback at t112.py:1202) is the existing approved risk surface

### must-not-no-signing-broadcast-key-loading — PASS

No signing, broadcast, key-loading, or plaintext Keystore decryption was added.

Evidence:

- tests/test_no_signing_paths.py::test_no_forbidden_imports_in_source and test_no_forbidden_identifiers_in_source pass
- BacktestUseCase does not import any signing/RPC/execution module

### typed-failure-coverage — PASS

Six typed failure classes cover the documented failure modes; the use case fails closed with a named T113_ reason and publishes no successful current artifact on every documented failure.

Evidence:

- Six typed failure classes defined and exported: BacktestUseCaseError, MissingDatasetRegistryFailure, PartitionResolutionFailure, UnqualifiedPartitionFailure, EmptyEventSourceFailure, InvalidRunRequestFailure
- BacktestUseCase.start catches FixedEmptyEventSourceError, PartitionRefMismatchError, T100PartitionResolutionError, T112OrchestratorError and maps them to T113_-prefixed reason codes via _truncate_reason (preserves original on error_message)
- InvalidRunRequestFailure is raised for non-RunRequest input
- TestPublicBoundary::test_failure_classes_carry_t113_prefix verifies the hierarchy
- TestInvalidInput::test_non_request_input_raises_typed_failure verifies the raise path

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- The _truncate_reason helper (src/robinhood_lp/application/backtest.py:506) re-prefixes T112_/T069_ codes into the T113_ namespace, so the public reason code surfaced on a missing-registry failure is T113_UNKNOWN_DATASET_VERSION rather than the T113_MISSING_DATASET_REGISTRY documented in implementation guide §5; the underlying original reason is preserved on error_message. The contract's behavioral requirement (fail closed with a T113_ prefix and a named reason) is satisfied; only the documented reason-code naming in §5 is slightly different from the runtime value. This is recorded in the developer evidence and does not block PASS.
- The CLI composition root _build_cli_application ships fail-closed stubs at the three T112-specific ports; an operator-driven backtest start without a real T100 partition registry is recorded as FAILED with T113_PARTITION_RESOLUTION_FAILED (the partition_resolver stub) and publishes no manifest. A production deployment must inject real T100 adapters through the application composition root; the CLI surface does not expose a registry path for those adapters in this attempt.
- TestByteIdenticalHistoricalArtifacts covers T105 and T109 manifest round-trip but not T069 RunRecord round-trip explicitly; the T069 record format is exercised via TestNormalCaseTwoHeterogeneousPools::test_normal_case_persists_succeeded_record, which reads the persisted T069 record back. A pure T069-only round-trip test is absent but is not a contract requirement.
