# T105 independent review

- Base commit: `8b9108125d599e5bbb09eff9b87b77c294e1ddd3`
- Candidate commit: `f913d44bcc3a0e99d716aac532334e3cd6358c9e`
- Verdict: **PASS**

## Checks

### T063 acceptance preserved — PASS

All T063 reconciliation, tamper, numeraire, unit, per-pool and multi-pool acceptance clauses continue to pass under the new binding path. The original test_manifest_rejects_unknown_strategy_kind was replaced by two sharper tests: test_manifest_rejects_unknown_strategy_identity_at_build (publish gate via bind_strategy_to_registry) and test_manifest_rejects_mismatched_binding_at_validation (validate_manifest raises RegistryBindingError on tampered binding).

Evidence:

- tests/test_reports_t063.py: 36 passed (PYTHONPATH=src pytest tests/test_reports_t063.py -q)
- tests/test_reports_t105.py + T063 + T068 + T062 + T065 + T061 combined: 333 passed
- Full test suite: 2358 passed, 4 skipped (forge tooling unavailable)
- test_manifest_rejects_unknown_strategy_identity_at_build: bind_strategy_to_registry raises UnknownStrategyIdentityError on unregistered identity
- test_manifest_rejects_mismatched_binding_at_validation: validate_manifest raises RegistryBindingError on tampered code_provenance_revision

### Registry binding replaces hard-coded strategy vocabulary — PASS

The T063 hard-coded strategy_kind vocabulary is fully removed from the current publication path. The manifest builder consumes a pre-built StrategyBinding and refuses any caller that bypasses the registry. No production, CLI, Web, background or test helper can publish a current manifest via VALID_STRATEGY_KINDS.

Evidence:

- src/robinhood_lp/reports/manifest.py: ExperimentManifest fields now include strategy_identity/strategy_version/registry_version/registry_checksum/parameter_schema_version/parameter_schema_checksum/code_provenance_module/revision/symbol
- src/robinhood_lp/reports/manifest.py: __post_init__ no longer consults VALID_STRATEGY_KINDS; replaced by _require_non_empty_str on the new binding slots
- src/robinhood_lp/reports/manifest.py: VALID_STRATEGY_KINDS retained but documented DEPRECATED and only exported for legacy reader
- src/robinhood_lp/reports/manifest.py: build_experiment_manifest signature now takes strategy_binding: StrategyBinding and refuses any free-text kind
- tests/test_reports_t105.py TestOldPathUnreachable: asserts build_experiment_manifest does not accept strategy_kind and that VALID_STRATEGY_KINDS is documented as DEPRECATED

### Manifest schema bumped to t105 with new registry-binding fields — PASS

Every reports module version is bumped to t105.* namespace, while the legacy reader anchors the prior t063.experiment_manifest.v1 version and the LEGACY_T063 marker. Downstream consumers that pinned the old versions need re-pinning (documented residual risk).

Evidence:

- MANIFEST_VERSION = t105.experiment_manifest.v1
- VALIDATION_VERSION = t105.manifest_validation.v1
- RERUN_VERSION = t105.manifest_rerun.v1
- BINDING_VERSION = t105.strategy_binding.v1
- LEGACY_MANIFEST_VERSION pinned to t063.experiment_manifest.v1
- LEGACY_MARKER pinned to LEGACY_T063 literal

### Registry-binding validation gate — PASS

Validation rejects every registry-binding disagreement the contract lists: registry_version, registry_checksum, strategy_version, parameter_schema_checksum, code_provenance_module/revision/symbol. All failure paths raise RegistryBindingError with the failing slot attached.

Evidence:

- src/robinhood_lp/reports/validation.py: validate_manifest now invokes _check_registry_binding which reconstructs StrategyBinding from manifest fields and calls assert_binding_matches_registry
- tests/test_reports_t105.py TestBindingChecksumAgreement: 6 binding mismatch tests cover registry_version, registry_checksum, strategy_version, parameter_schema_checksum, code_provenance_revision, code_provenance_module
- tests/test_reports_t105.py test_manifest_rejects_mismatched_binding_at_validation: tampered code_provenance_revision is caught

### Parameter schema enforcement via bind_strategy_to_registry — PASS

bind_strategy_to_registry is the single gate every current publication passes through; it rejects unregistered identities, undeclared parameters, missing parameters, wrong-type values and out-of-range values via the T068 schema surface.

Evidence:

- src/robinhood_lp/reports/registry_binding.py: bind_strategy_to_registry consults registry.lookup and registry.validate_parameters
- tests/test_reports_t105.py TestBindingParameterValidation: 8 tests cover unregistered identity, undeclared parameter, missing required parameter, wrong type, zero capital, out-of-range tick_spacing, bool passed as int, adaptive Q64.64 out-of-range threshold
- T068 registry.py: validate_parameters enforces all four schema rules (unknown, missing, wrong-type, out-of-range)

### Rerun path is registry-bound — PASS

The rerun path no longer consults a hard-coded strategy_kind switch. The strategy callback is reconstructed through the registry's factory surface; unregistered identities and tampered binding fields fail before any engine call. The adaptive strategy is wrapped in AdaptiveStrategyCallback so the engine can invoke it.

Evidence:

- src/robinhood_lp/reports/rerun.py: _build_strategy_callback now uses default_registry().lookup + factory.build and wraps AdaptiveStrategy in AdaptiveStrategyCallback
- tests/test_reports_t105.py test_artifact_rerun_reproduces_metrics_checksum: rerun reproduces decisions + metrics checksums
- tests/test_reports_t105.py test_artifact_rerun_rejects_unregistered_identity: tampered strategy_identity fails rerun with UnknownStrategyIdentityError
- tests/test_reports_t105.py test_artifact_rerun_detects_tampered_binding: tampered code_provenance_revision fails rerun with RegistryBindingError
- tests/test_reports_t105.py TestAdaptiveEndToEnd: T065 adaptive manifest reruns successfully

### Adaptive (T065) end-to-end — PASS

The T065 adaptive-Range strategy binds against the registry, produces a manifest whose identity, parameters, schema, registry revision and code provenance validate, and its canonical rerun reproduces the recorded metrics and decisions checksums.

Evidence:

- Verified live: build_experiment_manifest with IDENTITY_ADAPTIVE_RANGE + write_manifest_to_path + rerun_manifest yields match=True, decisions_match=True
- tests/test_reports_t105.py test_adaptive_range_identity_binds: validates 18 adaptive parameters against the schema
- tests/test_reports_t105.py test_adaptive_manifest_persists_full_provenance: code_provenance_module=robinhood_lp.strategy.adaptive, symbol=AdaptiveStrategy

### Per-pool and multi-pool manifests — PASS

Per-pool invariant and multi-pool run identity both pass under the new binding path. A multi-pool run publishes one manifest per member pool under one RunIdentity, each member binds independently against the registry.

Evidence:

- tests/test_reports_t105.py TestPerPoolAndMultiPool: per-pool manifest binds independently; multi-pool run publishes one manifest per member pool under one RunIdentity; cross-manifest gate rejects foreign members
- src/robinhood_lp/reports/manifest.py ExperimentManifest.assert_events_match_pool: per-pool invariant still enforced
- src/robinhood_lp/reports/validation.py validate_multi_pool_run: validates every member + cross-manifest identity

### T063 artifact command and T069 product lifecycle boundary — PASS

The rerun-manifest CLI is read-only and bound to its artifact-operation scope. The T069 product lifecycle (when it lands) creates a fresh run record by reading the source through load_manifest_from_path and rebuilding; the source is never mutated by either path.

Evidence:

- src/robinhood_lp/__main__.py _run_rerun_manifest: rerun-manifest CLI auto-detects legacy vs current; raises RegistryBindingError/InvalidLegacyManifestError/LegacyManifestError distinctly
- tests/test_reports_t105.py _t069_linked_rerun: T069 helper loads source through load_manifest_from_path and constructs a fresh ExperimentManifest with new run_id; source bytes are unchanged
- src/robinhood_lp/reports/rerun.py docstring + module surface: rerun never creates T069 state, never reads Web state, never mutates source manifest

### Legacy T063 reader + migration — PASS

Historical T063 artifacts are byte-identical preserved, marked LEGACY_T063, and migrate deterministically to current T105 manifests. Every legacy kind maps to a registered identity; the mapping is total so the migration never invents an identity.

Evidence:

- src/robinhood_lp/reports/legacy.py LEGACY_MANIFEST_VERSION = t063.experiment_manifest.v1; LEGACY_MARKER = LEGACY_T063
- src/robinhood_lp/reports/legacy.py load_legacy_manifest_from_path: refuses current artifacts, refuses malformed JSON, refuses non-object roots, refuses unknown files; source checksum is SHA-256 of raw bytes
- src/robinhood_lp/reports/legacy.py migrate_legacy_manifest: maps every T063 kind to a registered identity via _LEGACY_KIND_TO_IDENTITY; binds against live registry; recomputes report checksum against new field set
- _LEGACY_KIND_TO_IDENTITY is total: set(_LEGACY_KIND_TO_IDENTITY) == set(VALID_STRATEGY_KINDS)
- Verified live: legacy HOLD manifest migrates to t062.hold.v1 bound manifest that validate_manifest accepts

### Code provenance and parameter schema checksums are deterministic — PASS

Both checksums are deterministic and depend on every relevant field; drift in the registry or in the per-identity schema surfaces as a checksum change that the manifest authority rejects.

Evidence:

- compute_parameter_schema_checksum: SHA-256 hex of canonical PARAM|name|type|unit|default|lower_bound|upper_bound lines
- Registry.checksum: SHA-256 hex of canonical REGISTRY_VERSION + IDENTITY + VERSION + MODULE + REVISION + SYMBOL + PARAM lines
- tests/test_reports_t105.py test_parameter_schema_checksum_is_deterministic and test_parameter_schema_checksum_depends_on_schema: verified

### Dependency direction (no T068↔T105 cycle, no RPC/storage/signer/Web) — PASS

No T068↔T105 cycle. Layer purity (ADR-006) preserved across the cutover: reports → strategy.registry is unidirectional; strategy.registry imports neither reports nor any disallowed layer.

Evidence:

- src/robinhood_lp/reports/registry_binding.py imports from robinhood_lp.strategy.registry only
- src/robinhood_lp/strategy/registry.py imports stdlib + lower protocol contracts only; no robinhood_lp.reports import
- tools/check_imports check: import-graph check passed: no findings
- Layer purity preserved: no RPC, storage, signer, Web, presentation imports introduced in reports/registry_binding.py or reports/legacy.py

### Must-not clauses (overwrite / chart / numeraire / USD / outside-T068 identity / Web rerun / no-execution-authority) — PASS

None of the must-not clauses are violated by the T105 cutover. The binding path strengthens the 'no identity outside T068' rule.

Evidence:

- assert_no_prior_run_at_path still enforced: writing a manifest over an existing run with overwrite=False raises PriorRunOverwriteError
- ExperimentManifest.coverage_checksum still required; coverage gap detectable through checksum mismatch
- dataset_version and reporting_numeraire still required non-empty fields; validate_manifest rejects MissingRequiredFieldError
- assert_presentation_numeraire_safe still rejects RELATIVE_ONLY presented under USD tokens (USDG/USDC/USDT/DAI/USD)
- bind_strategy_to_registry is the sole builder; no path accepts a caller-supplied or caller-invented identity outside the registry
- _run_rerun_manifest does not read Web state, does not read signing material, and is a deterministic local re-execution
- ExperimentManifest has no execution, approval or promotion authority; manifests are evidence only

### Lint/type/static checks — PASS

Ruff lint, ruff format, and mypy strict all clean on the new and modified files.

Evidence:

- ruff check src/robinhood_lp/reports/ tests/test_reports_t105.py: All checks passed!
- ruff format --check: 9 files already formatted
- mypy src/robinhood_lp/reports/ tests/test_reports_t105.py tests/test_reports_t063.py tests/test_strategy_t068.py: Success: no issues found in 11 source files

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- The T065 AdaptiveStrategy is non-callable; the rerun path wraps it in AdaptiveStrategyCallback so the engine can invoke it. A future strategy that returns a non-callable, non-AdaptiveStrategy object from factory.build would surface as ManifestValidationError at rerun time (the existing test_manifest_rejects_mismatched_binding_at_validation and the explicit error message in _build_strategy_callback document this contract).
- The legacy manifest reader projects a T063 payload into an ExperimentManifest with placeholder registry-binding fields (legacy.t063.unspecified). Calling validate_manifest on the legacy wrapper fails the registry-binding check by design; the legacy marker is the canonical signal that the artifact is not current registry-bound evidence. Migration through migrate_legacy_manifest is the only path to current promotion evidence. This is a deliberate design choice documented in legacy.py and in the developer residual_risks.
- The T105 cutover bumps MANIFEST_VERSION, VALIDATION_VERSION, RERUN_VERSION and BINDING_VERSION. Downstream consumers (T069, T073, T084, T101, T106, T107, T096) are PLANNED in todo/config.yaml and will re-pin against t105.*; that is owned by their own contracts and outside T105's scope.
- The developer's command-line evidence listed 333 passed for the T063+T068+T062+T065+T061 combination; my run of that exact set returns 266 passed. The 333 figure matches when test_reports_t105.py is included. The discrepancy is a count-reporting issue in the developer evidence, not a test outcome difference (all listed tests pass either way).
