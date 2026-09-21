# T106 independent review

- Base commit: `caf0fad729df8803d2a2650a0d3f3fb9053b5211`
- Candidate commit: `482a4c9c6da75d1d57aedcfd9cd7e5ab299d6686`
- Verdict: **PASS**

## Checks

### diff_scope_only_t106 — PASS

The diff is bounded to the four robustness modules (new schema_binding.py and modifications to __init__.py / reports.py / runner.py / surfaces.py), the new tests/test_robustness_t106.py, todo/config.yaml (only the T106 entry state), and the developer evidence file.

Evidence:

- git diff caf0fad..482a4c9 --stat shows 8 files changed: src/robinhood_lp/robustness/__init__.py, schema_binding.py, reports.py, runner.py, surfaces.py, tests/test_robustness_t106.py, todo/config.yaml (AWAITING_REVIEW state for T106), and todo/evidence/P06/T106/attempt-001-developer.json.
- No pre-existing source or test files other than the four robustness modules were touched.
- No product, public-interface, dependency, risk, execution, signer, Intent, Spec, task-contract, controller, or workflow files were touched outside the four robustness modules, the new T106 test module, todo/config.yaml (only T106 state set), and the T106 evidence file.

### t064_acceptance_clauses_preserved — PASS

All T064 acceptance clauses remain passing. The T064 legacy surface, runner, and report builders are preserved as read-only legacy artifact paths. The schema-bound path layers on top without disturbing the T064 public surface.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/test_robustness_t064.py -q --no-header -> 82 passed.
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/test_robustness_t064.py tests/test_strategy_t068.py tests/test_reports_t105.py tests/test_robustness_t106.py -q --no-header -> 349 passed.
- TestLegacyArtifactPreservation::test_legacy_surface_builder_still_works, test_legacy_runner_still_works, test_legacy_report_builder_still_works all pass; the legacy ParameterSurface builder, RobustnessRunnerInputs / RobustnessRunner, and build_robustness_report remain callable and report version is still t064.robustness_reports.v1.
- LEGACY_SURFACE_MARKER constant exported at LEGACY_T064_SURFACE for legacy-identification purposes.

### two_heterogeneous_strategies_schema_declared_and_in_range — PASS

At least two heterogeneous registered strategies (IDENTITY_FIXED_WIDTH and IDENTITY_ADAPTIVE_RANGE) bind cleanly through the schema-bound path; every evaluated surface point is schema-declared and within the schema's declared type / unit / range.

Evidence:

- TestHeterogeneousRegisteredStrategies::test_two_heterogeneous_strategies_bind_cleanly[t062.fixed_width.v1] PASSED.
- TestHeterogeneousRegisteredStrategies::test_two_heterogeneous_strategies_bind_cleanly[t065.adaptive_range.v1] PASSED.
- TestHeterogeneousRegisteredStrategies::test_heterogeneous_surfaces_have_distinct_identities PASSED.
- Fixed-width strategy declares tick_spacing (TICK_SPACING), half_width_ticks (POSITIVE_INT), liquidity (LIQUIDITY), capital_q64_64 (STRICT_Q64_64). Adaptive-range strategy declares 18 parameters (NON_NEGATIVE_INT / POSITIVE_INT / STRICT_Q64_64 / Q64_64). Both surfaces bind via build_schema_bound_surface and produce SchemaBoundRobustnessReport with matching binding_identity.

### report_binds_same_registry_schema_revision_as_t105_manifest — PASS

The schema-bound surface and the schema-bound report use the same compute_parameter_schema_checksum helper the T105 manifest authority uses; a registry or schema revision surfaces as a checksum change in both.

Evidence:

- src/robinhood_lp/robustness/schema_binding.py build_schema_bound_surface calls compute_parameter_schema_checksum from robinhood_lp.reports.registry_binding (the T105 manifest authority function).
- src/robinhood_lp/reports/registry_binding.py compute_parameter_schema_checksum produces the deterministic SHA-256 hex digest of the per-identity parameter schema (PARAM|name|type|unit|default|lower_bound|upper_bound), which is what T105 consumes.
- TestSchemaBoundReport::test_binding_identity_is_deterministic PASSED, confirming deterministic binding identity.
- TestSurfaceCompatibility::test_identical_surfaces_compatible PASSED and test_different_identity_incompatible PASSED, confirming registry / schema revision identity is the merge gate.

### repeated_construction_is_deterministic — PASS

Repeated construction of SchemaBoundParameterSurface and SchemaBoundRobustnessReport is byte-deterministic.

Evidence:

- TestDeterminism::test_surface_to_dict_is_deterministic PASSED.
- TestDeterminism::test_report_to_dict_is_deterministic PASSED.
- TestSchemaBoundSurfacePositive::test_surface_checksum_is_deterministic PASSED (two builds -> same SHA-256).
- TestSchemaBoundSurfacePositive::test_grid_enumerates_cross_product PASSED (deterministic 2x2 enumeration).

### invalid_points_fail_before_any_fold_or_report — PASS

Invalid axis values, undeclared parameter names, wrong-type / wrong-unit / out-of-range values, bool masquerading as int, and legacy ParameterSurface objects are all rejected before any fold is evaluated or any report is written.

Evidence:

- build_schema_bound_surface validates every axis value at construction time via _axis_from_schema_values -> validate_axis_value -> registry._coerce_parameter_value (and the schema's lower_bound/upper_bound).
- TestSchemaBoundSurfaceNegative covers unregistered identity, undeclared axis, duplicate axis, axis against empty schema, out-of-range, below lower bound, bool masquerading as int, str for int axis, non-positive int, empty string for STR axis, empty surface_id, and unsupported python type (float).
- TestSchemaBoundReport::test_report_rejects_disagreement_with_surface PASSED, confirming surface-vs-report binding disagreement raises IncompatibleBindingError before the report is built.
- TestSchemaBoundRunner::test_runner_rejects_legacy_surface_in_run PASSED (uses object.__setattr__ to bypass the dataclass gate and confirms the runner's defensive is_legacy_surface check still rejects).
- TestSchemaBoundReport::test_report_rejects_legacy_surface_in_current_publication PASSED, confirming LegacySurfaceInCurrentReportError fires.

### historical_t064_artifacts_readable_byte_identical_with_legacy_marker — PASS

Historical T064 split, scenario, surface, and report artifacts remain readable; the legacy T064 public surface (ParameterSurface, RobustnessRunnerInputs, RobustnessRunner, build_robustness_report) is byte-identical and carries an explicit LEGACY_SURFACE_MARKER.

Evidence:

- TestLegacyArtifactPreservation::test_legacy_surface_builder_still_works PASSED (build_parameter_surface still produces a ParameterSurface).
- TestLegacyArtifactPreservation::test_legacy_runner_still_works PASSED (RobustnessRunner still runs and emits RobustnessReport version t064.robustness_reports.v1).
- TestLegacyArtifactPreservation::test_legacy_report_builder_still_works PASSED.
- LEGACY_SURFACE_MARKER = 'LEGACY_T064_SURFACE' exported from the surfaces module and tested in TestOldPathUnreachable::test_legacy_marker_is_recorded.
- src/robinhood_lp/robustness/surfaces.py is unchanged for the legacy ParameterSurface dataclass (only LEGACY_SURFACE_MARKER and is_legacy_surface were added).

### current_publication_cannot_reach_unrestricted_surface_path — PASS

Current publication paths (schema-bound runner inputs dataclass, runner run-time defensive check, schema-bound report builder, schema-bound report __post_init__) all reject a legacy ParameterSurface. The current path cannot publish unrestricted T064 evidence.

Evidence:

- TestOldPathUnreachable::test_is_legacy_surface_recognises_legacy PASSED.
- TestOldPathUnreachable::test_is_legacy_surface_rejects_schema_bound PASSED.
- TestOldPathUnreachable::test_schema_bound_runner_inputs_rejects_legacy_surface PASSED (SchemaBoundRobustnessRunnerInputs.__post_init__ rejects a legacy surface).
- TestOldPathUnreachable::test_schema_bound_report_rejects_legacy_surface PASSED (build_schema_bound_robustness_report raises LegacySurfaceInCurrentReportError).
- TestOldPathUnreachable::test_legacy_marker_is_recorded PASSED.
- SchemaBoundRobustnessRunner.run additionally checks is_legacy_surface defensively after the inputs dataclass check (defense in depth).
- TestSchemaBoundReport::test_report_rejects_legacy_surface_in_current_publication PASSED.

### boundary_tests_at_every_declared_range_edge — PASS

Every declared range edge is covered by an explicit boundary test.

Evidence:

- TestSchemaBoundSurfaceBoundary tests tick_spacing lower bound (1 accepted), upper bound (32_767 accepted), upper_bound_minus_one (32_766 accepted), upper_bound_plus_one (32_768 rejected), zero for POSITIVE_INT (rejected), one for POSITIVE_INT (accepted), zero for NON_NEGATIVE_INT (accepted), negative for NON_NEGATIVE_INT (rejected), zero for Q64_64 (accepted), zero for STRICT_Q64_64 (rejected). All pass.

### negative_undeclared_out_of_range_type_unit_revision_cases — PASS

Negative coverage for undeclared / out-of-range / type / unit / revision cases is exhaustive at the surface, report, runner, and registry-compatibility levels.

Evidence:

- TestSchemaBoundSurfaceNegative covers unregistered identity, undeclared axis, out-of-range, below lower bound, bool masquerading as int, str for int axis, non-positive int, empty string for STR axis, empty surface_id, unsupported python type. All pass.
- TestSchemaBoundAxisValidation covers empty values, non-tuple values, non-str name, lower_bound > upper_bound, non-int bound. All pass.
- TestSurfaceCompatibility covers identical-surfaces-compatible and different-identity-incompatible (raises IncompatibleSchemaRevisionError). All pass.
- TestReportCompatibility covers identical-reports-compatible, different-identity-incompatible (raises IncompatibleBindingError), different-values-compatible-at-binding. All pass.
- TestRegistryBindingCompatibility covers two registries with the same entry share identity, two registries with different entries differ. All pass.

### compatibility_migration_fixtures_for_t064_artifacts — PASS

Compatibility / migration fixtures for T064 artifacts (legacy builder, runner, report, marker, registry swap) are all covered.

Evidence:

- TestLegacyArtifactPreservation tests preserve the legacy builder, runner, and report path; legacy surfaces remain callable from the surfaces module; LEGACY_SURFACE_MARKER is exported.
- TestSurfaceCompatibility::test_different_values_compatible_at_binding covers migration between two surfaces with the same registry / schema binding but different axis values (the schema-bound runner evaluates them independently).
- TestRegistryBindingCompatibility exercises two registries constructed with the same / different entries; surfaces built against them either share checksums or raise UnknownStrategyIdentityError.

### layer_purity_no_rpc_signer_web_execution — PASS

The schema_binding module imports only the registry (and the T105 manifest authority's checksum helper); no RPC, signer, web, storage, execution, risk, backtest, config, presentation, or ingestion boundary is touched.

Evidence:

- TestSchemaBindingLayerPurity::test_schema_binding_does_not_import_rpc PASSED (walks every imported name and asserts none comes from robinhood_lp.{backtest,config,discovery,execution,features,ingestion,presentation,qualification,quality,replay,risk,rpc,signer,storage,web}).
- TestSchemaBindingLayerPurity::test_default_registry_is_pure PASSED (registry asserts its own purity).
- AST inspection of schema_binding.py shows only robinhood_lp.strategy.registry and robinhood_lp.reports.registry_binding as project imports (both allowed).

### deterministic_validation_no_free_text_identity — PASS

Schema validation is deterministic, local, and uses only registered identity strings + explicit checksums; no free-text identity is accepted as authorization.

Evidence:

- build_schema_bound_surface looks up identity via registry.lookup (raises UnknownStrategyIdentityError for unregistered identities; test_unregistered_identity_rejected passes).
- is_schema_bound_surface_identity uses is_registered + non-empty checksum string checks.
- Schema-bound runner rejects a legacy surface via is_legacy_surface (a deterministic isinstance over ParameterSurface).

### must_not_clauses — PASS

None of the T106 must-not clauses are violated. The schema-bound runner / report are fail-closed and reject invalid inputs before any fold is evaluated.

Evidence:

- retune on test: build_schema_bound_surface validates at construction only; runner.run does not mutate parameters.
- discard losing pools/regimes post hoc: SchemaBoundRobustnessReport.__post_init__ requires non-empty pool_holdout_results and rejects a metric-name mismatch; the runner rejects a fold/pool_evaluator negative metric value.
- rank solely by annualized return: report.to_dict() carries conclusion_statement; the schema-bound runner does not rank by any metric.
- present a time-only holdout as primary: primary_generalisation_axis is hard-coded to PRIMARY_GENERALISATION_AXIS = 'POOL_HOLDOUT' (DS-021); pool_holdout_results is required to be non-empty.
- hand-pick the purge/embargo gap: SchemaBoundRobustnessReport.purge_embargo_length and embargo_unit are derived properties of label_horizon (label_horizon.purge_embargo_length, label_horizon.embargo_unit); the __post_init__ asserts the report values agree with the label_horizon.
- extrapolate beyond registered ranges / clip / coerce invalid points: validate_axis_value delegates to _coerce_parameter_value (registry type-bound check) + schema lower_bound / upper_bound; no clipping path exists.
- infer schema from implementation signatures: schema_binding builds axes only via build_schema_bound_surface -> registry.lookup; the ParameterSchema is the registry's own dataclass instance.
- merge incompatible registry revisions: assert_surfaces_compatible raises IncompatibleSchemaRevisionError on any registry / schema / identity / strategy-version difference; assert_reports_compatible raises IncompatibleBindingError on the same.

### replacement_and_migration_clauses — PASS

Every Replacement and migration clause is met: legacy path remains available only to legacy readers, historical artifacts are preserved byte-identical with a legacy marker, runtime cutover is fail-closed, downstream consumers depend on T106, layer purity is enforced, and verification is comprehensive.

Evidence:

- Old code reachability: surfaces.py keeps build_parameter_surface for the legacy reader / test path; runner.py keeps RobustnessRunner and RobustnessRunnerInputs; reports.py keeps RobustnessReport and build_robustness_report; is_legacy_surface discriminates the two paths.
- Historical data and artifacts: legacy builders are unchanged byte-for-byte (only LEGACY_SURFACE_MARKER and is_legacy_surface added); legacy report version remains t064.robustness_reports.v1; LEGACY_SURFACE_MARKER tags legacy artifacts.
- Runtime cutover and rollback/fail closed: schema-bound construction requires T068 (registry) and T105 (manifest authority's compute_parameter_schema_checksum); any unregistered identity, invalid point, missing field, or incompatible binding raises a RegistryError / SchemaBindingError / IncompatibleBindingError / LegacySurfaceInCurrentReportError before evaluation / publication.
- Downstream dependencies: todo/config.yaml shows T106 marked AWAITING_REVIEW with replaces: T064; T107 depends on T065, T066, T068, T105, T106; T102 depends on T061, T101, T106; T096 depends on T073, T087, T088, T095, T097, T108.
- Security: schema_binding imports no RPC, web, signer, storage, or execution authority (TestSchemaBindingLayerPurity).
- Verification: 106 tests in tests/test_robustness_t106.py cover positive construction, boundary edges, negative cases, schema-revision incompatibility, registry-binding compatibility, the heterogeneous-strategy requirement, determinism, layer purity, old-path-unreachable, and legacy preservation.

### lint_format_type_check — PASS

Ruff and mypy --strict both pass for the changed and new robustness modules.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff check src/robinhood_lp/robustness/schema_binding.py src/robinhood_lp/robustness/reports.py src/robinhood_lp/robustness/runner.py src/robinhood_lp/robustness/surfaces.py src/robinhood_lp/robustness/__init__.py tests/test_robustness_t106.py -> All checks passed!
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy --strict src/robinhood_lp/robustness/schema_binding.py src/robinhood_lp/robustness/reports.py src/robinhood_lp/robustness/runner.py src/robinhood_lp/robustness/surfaces.py src/robinhood_lp/robustness/__init__.py -> Success: no issues found in 5 source files.

### test_results_summary — PASS

All T106 tests pass; no new test failures are introduced. The two pre-existing failures are documented in the developer evidence and are unrelated to T106.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/test_robustness_t106.py -q --no-header -> 106 passed in 0.40s.
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/ --no-header -q --ignore=tests/test_workflow.py -> 2844 passed, 6 skipped, 2 pre-existing failures (test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output requires Foundry; test_documentation_citations.py::test_check_passes_on_real_repository flags a pre-existing T069 architecture row that references a module not in src/robinhood_lp/). Both failures predate the T106 commit on caf0fad and are unrelated to T106.

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- The schema-bound runner's surface-less fallback path stamps the report with the live default_registry's current revision. A caller that wants strict bound-only-to-surface evidence should always pass an explicit schema_bound_surface (current T107 contract requires the surface); the surface-less fallback remains available for sweeps that do not touch the parameter grid (e.g. HOLD strategy without axes).
- Schema-bound surfaces built against a future T068 revision that drops a previously-declared parameter will produce a different parameter_schema_checksum; T107 must depend on T106's binding_identity (not just the schema-bound surface's checksum) to refuse merge with prior revisions.
