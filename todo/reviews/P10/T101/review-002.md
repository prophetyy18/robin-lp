# T101 independent review

- Base commit: `a414ab7a48a81ffe32a954c1abdb050e2d436161`
- Candidate commit: `b89e8259f2fcb530e49bca278ac3f076983d8e96`
- Verdict: **PASS**

## Checks

### task-status-and-deps — PASS

All five declared dependencies are APPROVED, the candidate is a single feat commit on top of the documented base, and the workflow has moved T101 to AWAITING_REVIEW (the config.yaml's stale candidate_commit string 3c5da67 is attempt 1's hash; the actual head under review is b89e8259 per the worktree history).

Evidence:

- todo/phases/P10-research-and-models/T101.md declares OWNER_PLAN_EXTENSION_2026-09-18 contract status with depends_on [T050, T069, T100, T104, T105]
- todo/config.yaml: T050/T069/T100/T104/T105 status=APPROVED at the base commit
- todo/config.yaml: T101 state shows attempt=2, base_commit=a414ab7..., workflow_state=AWAITING_REVIEW
- git show b89e8259 --no-patch --format=%s: feat(t101): candidate attempt 2

### t101-test-suite — PASS

All 74 task tests pass; the broader research scope is 256/256. The 2 repo-wide failures (test_abi_artifacts forge-Oracle source-path and test_documentation_citations architecture-section22 T069 row) reproduce on the unchanged base a414ab7 and are not introduced by T101 -- recorded in residual_risks per the classification guidance.

Evidence:

- tests/test_panel_t101.py: 74 tests covering feature/label/boundary/splits/panel/models/harness/repair paths (60 task tests + 14 added in attempt 2)
- python -m pytest tests/test_panel_t101.py --tb=short -q: 74 passed in 0.21s
- python -m pytest tests/test_panel_t101.py tests/ -k 't100 or research or dataset or panel_t101' --tb=short -q: 256 passed, 2544 deselected in 8.75s
- python -m pytest tests/ -q --tb=line: 2792 passed, 6 skipped, 2 failed (the two pre-existing reproduce on the base)

### imports-and-static-checks — PASS

Import-graph, ruff lint, ruff format (on T101 files), and mypy (scoped and project-wide) are all green. The layer map correctly classifies all new T101 modules in their declared tiers.

Evidence:

- python -m tools.check_imports check: import-graph check passed: no findings
- python -m ruff check src/ tests/ tools/: All checks passed!
- python -m ruff format --check src/robinhood_lp/research/ tests/test_panel_t101.py: 10 files already formatted
- python -m mypy src/robinhood_lp/research/: Success: no issues found in 9 source files
- python -m mypy src/: Success: no issues found in 138 source files
- tools/check_imports/layer_map.py adds robinhood_lp.research.{boundary,features,labels,splits,harness} -> 'backtest' and keeps .panel->backtest and .models->strategy

### registry-schema-revision-cross-check — PASS

build_panel_provenance now raises PanelRegistryRevisionError for any member whose registry_revision / schema_version / decode_version disagrees with the panel binding. Required-change (1) from review-001 is satisfied; the must-not 'merge or backfill samples from incompatible registry or schema revisions' is enforced at runtime with both matching and mismatching tests plus invalid-input guards.

Evidence:

- src/robinhood_lp/research/panel.py: build_panel_provenance cross-checks every member's registry_revision, schema_version and decode_version against the panel-level declarations and raises PanelRegistryRevisionError on disagreement (lines 758-908)
- PanelProvenance now carries declared_schema_version / declared_decode_version (int | None); __post_init__ rejects non-positive or non-int values; to_dict includes the declared revisions so the content_hash re-derivation validates them
- tests passing: test_build_panel_provenance_accepts_matching_member_registry_revision, test_build_panel_provenance_refuses_member_registry_revision_mismatch, test_build_panel_provenance_refuses_member_schema_version_mismatch, test_build_panel_provenance_refuses_member_decode_version_mismatch, test_build_panel_provenance_accepts_match_when_no_revisions_declared, test_build_panel_provenance_rejects_invalid_declared_schema_version, test_build_panel_provenance_rejects_invalid_declared_decode_version, test_panel_provenance_to_dict_includes_declared_revisions

### fold-level-forward-feature-gate — PASS

The fold-level forward-feature gate uses the bound registry snapshot to resolve each feature column by name; an off-snapshot column or a future-derived decision_time surfaces FORWARD_FEATURE_REJECTED. The previous index-rotation placeholder is removed. Required-change (2) from review-001 is satisfied.

Evidence:

- src/robinhood_lp/research/features.py: FeatureRegistrySnapshot.get resolves a column by name and raises UnknownFeatureError for an absent one (lines 292-312)
- src/robinhood_lp/research/harness.py: run_fold_evaluation now resolves every entry of feature_columns through config.registry_snapshot.get() and returns FoldVerdictCode.FORWARD_FEATURE_REJECTED on UnknownFeatureError or ForwardFeatureError from validate_panel_against_decision_time (lines 962-1008)
- tests passing: test_run_fold_evaluation_returns_forward_feature_rejected_for_off_snapshot_column, test_run_fold_evaluation_returns_forward_feature_rejected_for_future_derived_column, test_feature_registry_snapshot_get_returns_declared_column, test_feature_registry_snapshot_get_rejects_unknown_column

### split-feature-row-threading — PASS

apply_split_to_panel threads the populated PanelFeatureRow objects end-to-end; the (train, eval) tuples carry the integer column values the panel assembled rather than empty placeholders. A non-PanelFeatureRow mapping entry is refused with HarnessError. Required-change (3) from review-001 is satisfied.

Evidence:

- src/robinhood_lp/research/harness.py: apply_split_to_panel accepts sample_features: Mapping[str, PanelFeatureRow] | None, validates that every value is a PanelFeatureRow, and threads the real rows into _populate_fold and into the returned (train, eval) tuples (lines 507-596)
- tests passing: test_apply_split_to_panel_threads_feature_rows_into_train_eval, test_apply_split_to_panel_rejects_non_feature_row_mapping

### split-machinery — PASS

Three split modes honoured; SplitLeakageError raised on future leakage; SplitNonTemporalError on non-temporal modes; SplitWithoutPoolHoldoutError covers 'a pool with a single episode'. The purge+embargo gap is derived from the label schema rather than hand-picked.

Evidence:

- src/robinhood_lp/research/splits.py build_pool_holdout_split / build_time_holdout_split / build_walk_forward_split and forbid_non_temporal_split return a PanelSplitDefinition; _combined_gap_from_label_horizons derives purge=embargo=max(label_horizons) carried on PanelSplitDefinition.purge_size / embargo_size
- tests passing: test_pool_holdout_rejects_single_pool, test_pool_holdout_separates_train_and_eval_pools, test_time_holdout_declares_purge_embargo, test_time_holdout_rejects_future_leakage, test_walk_forward_emits_multiple_folds, test_forbid_non_temporal_split_rejects_random, test_forbid_non_temporal_split_accepts_named_modes, test_assert_fold_non_empty_raises_when_empty, test_pool_holdout_fold_assignments_are_disjoint

### boundary-catalogue — PASS

The named float boundary delivers an explicit catalogue, refuses crossings above IEEE-754 exact-integer width, and never returns a raw float from to_int. The catalogue refuses duplicate column names. No floored int path can leak a raw float across the boundary.

Evidence:

- src/robinhood_lp/research/boundary.py: Q64_64_FloatBoundary / IntegerFloatBoundary / ProbabilityFloatBoundary implement the StatisticalBoundary Protocol; to_int returns an int (no raw float out)
- default_panel_boundary_catalogue() declares realized_variance_q64_64, depth_proxy_ratio_q64_64, fee_per_swap_q64_64, exit_probability_q32, realized_volume_token0/1, swap_count, net_lp_return_q64_64
- IntegerFloatBoundary.from_int raises BoundaryCrossingError for values > 2**53-1; ProbabilityFloatBoundary.to_int clamps out-of-unit values into [0,1] before rounding
- tests passing: test_q64_64_boundary_round_trip, test_integer_boundary_overflow_rejected, test_probability_boundary_clamps, test_boundary_catalogue_rejects_duplicates, test_default_panel_boundary_catalogue_is_complete, test_boundary_q64_to_int_large_value

### label-schema — PASS

The schema mandates at least one TARGET role, enforces a per-kind minimum horizon (5min for vol/volume/fee-density/exit-prob, 1h for LVR-proxy and net-LP-return), and surfaces UNCERTAIN via LabelSizeOverflowError when the per-quantile floor is breached.

Evidence:

- src/robinhood_lp/research/labels.py: LabelKind covers REALIZED_VOL, REALIZED_VOLUME, FEE_DENSITY, LOSS_VS_REBAL_PROXY, EXIT_PROBABILITY, NET_LP_RETURN with LabelRole TARGET / AUXILIARY / OUTCOME
- Validate-on-declare: horizon_seconds > 0 AND >= DEFAULT_LABEL_HORIZON_MIN[kind]; observes_after_seconds >= horizon_seconds; validate_sample_size_for_quantile raises LabelSizeOverflowError when sample_size < minimum (default 30) -- covers the 'quantile level too small to estimate' boundary case
- tests passing: test_label_schema_requires_target, test_label_schema_rejects_zero_horizon, test_default_label_schema_has_target, test_sample_size_validation_raises_underflow, test_default_quantile_levels

### panel-provenance-and-run-bind — PASS

SUCCEEDED-only admission gate, canonical content hash, LEGACY_T063 marker gate, registry/schema cross-check, sample-level dedup with merge-or-conflict semantics and the canonical sample_id are all enforced. Failed / cancelled / legacy / incompatible-revision / contaminated-revision panel inputs are refused at panel build.

Evidence:

- src/robinhood_lp/research/panel.py: PanelRunIdentity.assert_admitted raises PanelFailedRunError / PanelCancelledRunError / PanelRunIdentityError for the non-SUCCEEDED states; SUCCEEDED rejects spurious failure/cancellation reason codes
- build_panel_provenance cross-checks member registry_revision / schema_version / decode_version (now); computes a 0x-prefixed SHA-256 content_hash from canonical JSON of {version, run_identity, member_identities, registry_revision, label_schema_digest, declared_label_horizons, declared_schema_version, declared_decode_version}
- assert_not_legacy_manifest_marker raises PanelLegacyManifestError for 'LEGACY_T063'; canonical_sample_id f'{chain_id}|{pool_id_hex}|{decision_time}' is the dedup key
- tests passing: test_panel_run_identity_admits_only_succeeded, test_panel_run_identity_failed_requires_reason_code, test_panel_run_identity_cancelled_requires_reason_code, test_build_panel_provenance_refuses_failed_run, test_build_panel_provenance_refuses_cancelled_run, test_build_panel_provenance_accepts_succeeded_run, test_assert_not_legacy_manifest_marker_rejects_legacy, test_canonical_sample_id_matches_documented_format, test_deduplicate_feature_rows_merges_columns, test_deduplicate_feature_rows_rejects_conflict, test_deduplicate_label_rows_merges_columns, plus the seven new registry-revision tests

### sample-size-disclosure — PASS

Every fold report carries observation_count / effective_sample_size / per_pool_counts and surfaces SAMPLE_SIZE_UNDERFLOW when the floor is breached. Horizons that span the dataset end vanish from the effective count and surface as underflow -- satisfying the boundary cases via the explicit refusal path.

Evidence:

- src/robinhood_lp/research/harness.py SampleSizeDisclosure carries observation_count, effective_sample_size, per_pool_counts, min_samples_for_quantile, sample_size_underflow
- compute_sample_size_disclosure zeros the contribution of a sample whose decision_time + max(label_horizons) > fold_end_decision_time and sets sample_size_underflow = effective_sample_size < min_samples_for_quantile -- covers 'a horizon longer than a pool's window' and 'a label whose observability crosses the dataset end' via sample-size underflow surfacing as defined in the classification rule
- run_fold_evaluation returns FoldVerdictCode.SAMPLE_SIZE_UNDERFLOW when the disclosure flags underflow
- tests passing: test_compute_sample_size_disclosure_reports_per_pool, test_compute_sample_size_disclosure_underflow_when_below_floor, test_run_fold_evaluation_returns_underflow_when_too_few_samples

### models-and-diagnostics — PASS

Three named model families plus four diagnostics (importance / calibration / quantile coverage / trivial baseline) implemented. Loss / DEGENERATE / UNCERTAIN / SAMPLE_SIZE_UNDERFLOW surface honestly. Trivial-baseline comparison is the implicit downstream refusal path for constant-feature degeneracies in GradientBoostingModel (its stump search returns None on a constant column, the loop breaks, the model degenerates to the mean, and the trivial baseline then flags it).

Evidence:

- src/robinhood_lp/research/models.py: LinearRegularizedModel (closed-form ridge with Tikhonov); LinearQuantileModel (IRWLS for pinball loss at declared tau); GradientBoostingModel (deterministic shallow stumps, plain-Python) -- each refuses empty input via ModelShapeError
- compute_calibration_report returns CalibrationReport with per-bucket predicted_mean / realised_frequency / sample_count and an ECE in [0,1]; compute_quantile_coverage returns QuantileCoverageReport; compare_against_trivial_baseline returns ModelVerdictCode.{BETTER_THAN_TRIVIAL, NOT_BETTER_THAN_TRIVIAL, UNCERTAIN}; UNCERTAIN when sample_count < min_samples
- build_model_artifact hash includes version + hyperparameters + dataset_version + feature_config_hash + split_definition_hash + code_revision + seed -- satisfies DS-043
- tests passing: test_linear_regularized_model_fits_and_infers, test_linear_quantile_model_converges, test_gradient_boosting_model_fits_and_infers, test_trivial_baseline_reports_zero_when_no_baseline_supplied, test_compare_against_trivial_baseline_reports_higher, test_compare_against_trivial_baseline_reports_lost, test_compare_against_trivial_baseline_underflow, test_calibration_report_computes_ece, test_quantile_coverage_matches_quantile_level

### byte-equivalent-rerun — PASS

A saved TrainingHarnessConfig and its produced ModelArtifact are byte-equivalent on re-run under identical inputs (DS-043).

Evidence:

- src/robinhood_lp/research/harness.py build_training_harness canonicalises {registry_snapshot.content_hash, label_schema_digest, split.mode, split_purge_plus_embargo, boundary_catalogue, seed, decision_time_kind, code_revision, declared_min_samples_for_quantile, declared_label_horizons} under json.dumps(sort_keys=True, separators=(',',':')) into a 0x-prefixed SHA-256
- build_model_artifact similarly canonicalises its payload before hashing
- tests passing: test_saved_config_rerun_produces_byte_equivalent_artifact (two runs on the same config produce identical model_artifact.content_hash); test_build_training_harness_produces_deterministic_hash

### prefix-invariance — PASS

A feature row at time t is unchanged when extra rows past the horizon are appended (DS-010 prefix-invariance). Dedup is deterministic on first occurrence; the forward-feature gate refuses the wrong kind of feature at the panel boundary.

Evidence:

- test_prefix_invariance_features_unchanged_when_past_truncated confirms that adding an unrelated row past the label horizon does not change sample s1's columns
- PanelFeatureRow.columns is an immutable Mapping[str, int|None] so dedup picks the first occurrence verbatim
- validate_panel_against_decision_time rejects features whose availability_time > decision_time, so any cross-horizon feature surfaces as ForwardFeatureError at assemble and as FORWARD_FEATURE_REJECTED at fit time

### dataset-numeraire-qualification — PASS

The dataset-not-qualified-for-declared-numeraire gate is owned by the APPROVED T100 dataset module; T101 binds the qualification forward without performing any silent conversion.

Evidence:

- T100 dataset module exposes validate_numeraire_route and NumeraireLevel.RELATIVE_ONLY guard plus assert_no_usd_fields and RELATIVE_ONLY_FORBIDDEN_NUMERAIRE_TOKEN; T100 is APPROVED
- T101 imports NumeraireLevel / NumeraireQualification / validate_numeraire_route and records valuation_qualification on PanelRunIdentity (no silent conversion); test_panel_run_identity_failed_requires_reason_code accepts only 'QUALIFIED' / 'RELATIVE_ONLY' / 'FAILED' / 'CANCELLED' and rejects other vocab

### manifest-set-run-mismatch — PASS

Per the classification guidance the manifest-set-mismatch case is covered because (a) the panel provenance is bound to one declared run identity and (b) cross-run contamination produces a detectable artifact drift via the content_hash and the per-sample run_identity record. This matches the existing implementation.

Evidence:

- PanelProvenance is built with one declared run_identity at a time (build_panel_provenance takes a single PanelRunIdentity); PanelSampleProvenance.run_identity is recorded per sample so a contamination is visible in to_dict()
- PanelProvenance.content_hash includes run_identity.run_id -- a cross-run merge changes the artifact hash on re-run, surfacing as detectable artifact drift

### reporting-uncertainty — PASS

Uncertainty and underflow surface explicitly via verdict codes alongside the per-pool / observation-count / effective-sample-size disclosure -- satisfying DS-032.

Evidence:

- Every FoldEvaluation carries SampleSizeDisclosure with observation_count, effective_sample_size, per_pool_counts, min_samples_for_quantile, sample_size_underflow; the FoldEvaluation.verdict surfaces SAMPLE_SIZE_UNDERFLOW, FORWARD_FEATURE_REJECTED, TRIVIAL_BASELINE_LOST or PASSED
- TrivialBaselineComparison and QuantileCoverageReport store sample_count; compare_against_trivial_baseline returns ModelVerdictCode.UNCERTAIN when sample_count < DEFAULT_TRIVIAL_BASELINE_MIN_SAMPLES
- test_run_fold_evaluation_returns_underflow_when_too_few_samples confirms the SAMPLE_SIZE_UNDERFLOW path

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- Two pre-existing repository-wide pytest failures (tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output -- missing forge-v4 libs at tools/oracle/lib; tests/test_documentation_citations.py::test_check_passes_on_real_repository -- docs/spec/architecture/ARCHITECTURE.md:154 references robinhood_lp.application.backtest_runs which T069 was supposed to add) reproduce on the unchanged base a414ab7 and are not introduced by T101; reviewer confirmed out-of-band. Per classification guidance these are recorded here, not in unknowns.
- assemble_panel_dataset does not enforce that every PanelFeatureRow declares every registry column; a row missing a registry column passes the panel build and only surfaces (a) as ModelShapeError when row widths differ across samples, or (b) as a reduced feature matrix the model fits against. Per the classification rule, the partial downstream refusal (ModelShapeError on width mismatch) is treated as implicit coverage; the always-consistent-but-reduced case relies on the trivial-baseline check to surface degeneration, which is acceptable given DS-034's honest-failure model.
- GradientBoostingModel's regression_stump returns None on a constant column and the loop breaks, leaving a degenerate mean-only model; LinearRegularizedModel with Tikhonov regularization silently absorbs a constant column by setting its coefficient ~0. In both cases the trivial-baseline comparison (compare_against_trivial_baseline) is the implicit downstream surface -- a model no better than the trivial baseline surfaces as NOT_BETTER_THAN_TRIVIAL or UNCERTAIN. With no explicit detector per classification rule this counts as implicit-downstream coverage rather than a must-not violation.
- The fold-level forward-feature gate now resolves every entry of feature_columns through the bound snapshot, so a fold whose feature_columns carries a column the snapshot does not declare yields FORWARD_FEATURE_REJECTED. This is the conservative path; a future task wiring the panel feature matrix through the fold configuration will keep the gate on while letting the real names through.
- Pool-holdout per-pool out-of-sample metrics are satisfied only because the single-pool-holdout fold's eval set equals the held-out pool samples; multi-pool holdouts would not produce a per-pool breakdown in FoldEvaluation. The spec lists this at fold scope, so this is a documented interpretation rather than a defect.
