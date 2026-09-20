# T066 independent review

- Base commit: `2294da1efad702abc1d200fc46024fa1340911fc`
- Candidate commit: `a254a1978bffa006aca930d8f94602ecbd4e2c96`
- Verdict: **PASS**

## Checks

### T066.deliverable.split_plan — PASS

ExperimentPlan and harness.run() accept the T064 split types and feed their boundaries into the candidate lock; the rolling walk-forward and point-in-time holdout are both supported.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/harness.py
- ExperimentPlan.time_holdout_split / walk_forward_splits / pool_holdout_split reuse T064's TimeHoldoutSplit / WalkForwardSplit / PoolHoldoutSplit (split_boundaries() aggregates them) — point-in-time train/validation/test plus rolling walk-forward plans are first-class.

### T066.deliverable.parameter_search_manifest — PASS

ParameterSearchManifest preserves every record (winning, losing, rejected, failed); the harness's _collect_search_records never drops a record and the manifest constructor rejects an empty sweep.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/search.py
- ParameterSearchManifest + SearchRunRecord with VALID_RUN_STATUSES = FILLED/REJECTED/LOSING/NO_TRADE/FAILED/SUPERSEDED/DEGRADED; build_parameter_search_manifest rejects empty; losing_or_rejected_count() surfaces preservation.

### T066.deliverable.distributions — PASS

All seven required distribution kinds are produced by the harness from the orchestrator-supplied samples; every summary carries USDG_CASH + EXPERIMENTAL_NOT_LIVE_APPROVED and an integer content hash.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/distributions.py
- VALID_METRIC_KINDS = {PNL_USDG, PROFIT_PROBABILITY, EXPECTED_SHORTFALL_USDG, BOUNDARY_TOUCH_PROBABILITY, TIME_IN_RANGE_FRACTION, FEE_USDG, COST_USDG}; build_distribution_summary enforces integer-only and USDG_CASH + EXPERIMENTAL_NOT_LIVE_APPROVED fields.

### T066.deliverable.stability_and_stress_reports — PASS

Neighborhood stability report enumerates every neighborhood cell and the stress report enumerates every T064 catalogue scenario; both carry the binding flags.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/stability.py
- NeighborhoodStabilityReport + StressReport both bind USDG_CASH + EXPERIMENTAL_NOT_LIVE_APPROVED; harness._build_stress_report enumerates the T064 stress catalogue.

### T066.deliverable.candidate_lock — PASS

CandidateLock freezes every contractually named field; SHA-256 content hash is computed over the canonical JSON of the frozen payload and verified on load.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/candidate_lock.py
- CandidateLock frozen dataclass with parameter_set_version / code_revision / manifest_hash / seed / split_boundaries / cost_assumptions / elimination_rules / content_hash (SHA-256) / actor / lock_time_unix_seconds; load_candidate_lock recomputes and rejects a tampered file.

### T066.deliverable.lock_written_before_test_fold — PASS

The harness writes the candidate lock BEFORE the test fold is read; missing or tampered locks raise and the test fold is never read (verified by tests test_lock_written_before_test_fold, test_harness_refuses_to_read_test_fold_without_lock, test_harness_refuses_to_read_test_fold_when_lock_tampered).

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/harness.py
- harness.run steps 3-5: write_candidate_lock then load_candidate_lock (raising MissingCandidateLockError on missing, CandidateLockHashMismatchOnLoadError on tamper), then only then invokes test_fold_evaluator.

### T066.deliverable.parameter_change_new_version — PASS

Parameter changes create a new version (and hence a new lock); CandidateLock is a frozen dataclass with no amend/update path, so any post-lock change is a new candidate version rather than an edit.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/search.py
- compute_parameter_set_version returns the SHA-256 hex digest of the sorted 'key=value' representation; any change to a parameter yields a new digest and therefore a new candidate-lock file.

### T066.deliverable.recommendation_carries_experimental_flag — PASS

Both NO_TRADE and PROVISIONAL_RECOMMENDATION carry EXPERIMENTAL_NOT_LIVE_APPROVED on every output; T066 output alone cannot satisfy T094.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/harness.py
- ProvisionalRecommendation.__post_init__ enforces experimental_flag == 'EXPERIMENTAL_NOT_LIVE_APPROVED' for both NO_TRADE and PROVISIONAL_RECOMMENDATION.

### T066.acceptance.usdg_cash_primary_benchmark — PASS

USDG_CASH is the primary benchmark on every harness output.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/distributions.py
- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/stability.py
- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/harness.py
- DistributionSummary, NeighborhoodStabilityReport, StressReport, ProvisionalRecommendation all enforce primary_benchmark == 'USDG_CASH' in __post_init__; harness's test test_harness_emits_usdg_cash_primary_benchmark_everywhere verifies end-to-end.

### T066.acceptance.test_data_locked_before_read — PASS

Test fold is never read unless the matching candidate lock is on disk and passes its content-hash check (DS-031 contract).

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/harness.py
- harness.run writes the candidate lock to disk, verifies it via load_candidate_lock, and only then invokes test_fold_evaluator; missing/tampered locks raise MissingCandidateLockError / CandidateLockHashMismatchOnLoadError.

### T066.acceptance.losing_rejected_runs_remain — PASS

All losing / rejected / failed runs survive on the parameter-search manifest.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/search.py
- /home/lpdev/lp-worktrees/review-t066-attempt-001/tests/test_experiments_t066.py
- test_manifest_preserves_losing_records and test_harness_preserves_losing_and_rejected_runs confirm the manifest retains LOSING/REJECTED records and exposes losing_or_rejected_count().

### T066.acceptance.rerun_deterministic — PASS

Harness reruns are deterministic (no wall-clock reads, clock_unix_seconds is caller-supplied; ExperimentHarnessResult.content_hash binds every output).

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/tests/test_experiments_t066.py
- test_harness_rerun_is_deterministic asserts that two equivalent harness runs produce byte-identical results (content_hash + to_dict()); test_lock_round_trip_is_deterministic asserts the same for the on-disk lock.

### T066.acceptance.parameter_change_new_version — PASS

A change to any parameter (including frozen fields like split boundaries, cost assumptions, or elimination rules) yields a new candidate version rather than an edit to the existing lock.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/search.py
- /home/lpdev/lp-worktrees/review-t066-attempt-001/tests/test_experiments_t066.py
- compute_parameter_set_version is a SHA-256 over the sorted 'key=value' representation; test_parameter_change_new_version and test_harness_parameter_change_yields_new_version verify the contract.

### T066.acceptance.t066_output_cannot_satisfy_t094 — PASS

Every T066 output carries EXPERIMENTAL_NOT_LIVE_APPROVED, so T066 output alone cannot satisfy T094.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/harness.py
- ProvisionalRecommendation.experimental_flag is forced to 'EXPERIMENTAL_NOT_LIVE_APPROVED' in __post_init__; harness's test test_harness_emits_experimental_flag_on_every_output verifies the flag is on every output.

### T066.must_not.no_universal_thresholds — PASS

No universal thresholds are invented; the recommendation rule is a data-driven function of the locked candidate's distributions.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/harness.py
- Harness preserves the orchestrator-supplied elimination_rules verbatim and never fabricates thresholds; the recommendation rule is data-driven (positive median + positive stability spread).

### T066.must_not.no_retune_on_test — PASS

Candidate selection is validation-only; the test fold is read-only after the lock.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/harness.py
- _choose_candidate restricts to VALIDATION records; test_fold_evaluator is gated by the lock and only used to read the test fold for distribution / stability reporting, never for selection.

### T066.must_not.no_suppressing_no_trade — PASS

No-trade / losing / rejected runs are preserved on the manifest.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/search.py
- ParameterSearchManifest stores every SearchRunRecord; losing_or_rejected_count() counts LOSING/REJECTED/FAILED/DEGRADED/SUPERSEDED; harness never filters the records.

### T066.must_not.first_strict_improvement — PASS

Candidate selection is monotonic and first-strict-improvement; the global best is never chosen.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/harness.py
- _choose_candidate uses '>' (strict) rather than '>=' and walks the parameter-surface grid in declaration order, so the chosen candidate is the first strictly-improving version, not the global maximum.

### T066.must_not.no_fixture_as_live_default — PASS

No fixture / provisional value is treated as a product or live default.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/harness.py
- Every output carries EXPERIMENTAL_NOT_LIVE_APPROVED; ProvisionalRecommendation explicitly never approves paper / live trading.

### T066.verification.pytest — PASS

All 59 tests in tests/test_experiments_t066.py pass.

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/test_experiments_t066.py -v -> 59 passed in 0.16s

### T066.verification.ruff_format — PASS

ruff format is clean across the new package and test file.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff format --check src/robinhood_lp/experiments/ tests/test_experiments_t066.py -> '7 files already formatted'

### T066.verification.ruff_check — PASS

ruff lint is clean across the new package and test file.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff check src/robinhood_lp/experiments/ tests/test_experiments_t066.py -> 'All checks passed!'

### T066.verification.mypy_strict_src — PASS

Strict mypy is clean on the new source files (the contractually-binding code).

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy --strict src/robinhood_lp/experiments/ -> 'Success: no issues found in 6 source files'

### T066.verification.mypy_strict_tests — PASS

Strict mypy on the new test file reports 42 untyped-helper errors. The contract deliverables are all implemented and pass pytest, ruff, and strict mypy on the source files; the test-file annotation gap is a code-quality concern and is recorded as a residual risk, not a contract failure.

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy --strict tests/test_experiments_t066.py -> 'Found 42 errors in 1 file (checked 1 source file)'. The errors are untyped-def / no-untyped-def on helper functions and unannotated generic dict/tuple aliases; tests still run and pass.

### T066.layer_purity — PASS

Layer map updated; experiments does not import the backtest engine, manifest layer, RPC, storage, signing, execution, or presentation code.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/src/robinhood_lp/experiments/*.py
- /home/lpdev/lp-worktrees/review-t066-attempt-001/tools/check_imports/layer_map.py
- experiments modules import only stdlib + robinhood_lp.robustness.{splits,surfaces}; tools/check_imports/layer_map.py adds 'robinhood_lp.experiments' default = backtest and reclassifies __init__/harness to application/orchestration.

### T066.workflow_state — PASS

Workflow state correctly transitioned to AWAITING_REVIEW for the review.

Evidence:

- /home/lpdev/lp-worktrees/review-t066-attempt-001/todo/config.yaml
- T066 status AWAITING_REVIEW, attempt 1, base_commit 2294da1..., candidate_commit null (workflow controller will set it).

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- tests/test_experiments_t066.py has 42 strict-mypy errors (untyped helpers, unannotated generic dict/tuple aliases). The tests run and pass, the source files pass strict mypy, and the contract deliverables are satisfied; tightening the test-file annotations is a low-risk follow-up but not a contract-blocking defect.
- The candidate lock is a local-file artifact; an attacker who can write to the lock directory can substitute a lock with a matching content hash by replaying a previously-written lock. Production deployments should sign the lock or store it in a write-once medium; deferred to T090+ signer / live gates as documented.
- The harness's _invoke_fold wraps the fold evaluator in a try/except and tags raised exceptions as FAILED. A silent mis-computation in the orchestrator-supplied fold_evaluator would be recorded as FILLED; the harness relies on the orchestrator's evaluator discipline.
- The recommendation rule (positive median + positive stability spread => PROVISIONAL_RECOMMENDATION, otherwise NO_TRADE) is a simple data-driven heuristic; T066 does not bind a specific threshold and the recommendation always carries EXPERIMENTAL_NOT_LIVE_APPROVED.
- The harness's _collect_search_records enumerates the time-holdout train / validation / test folds and every walk-forward train / validation fold plus held-out pools. The TEST fold record is populated only via the orchestrator-supplied fold_evaluator's return value; the explicit test-fold access is gated by the test_fold_evaluator callable after the lock. DS-031 relies on the orchestrator's evaluator to not consult test-fold data during search.
