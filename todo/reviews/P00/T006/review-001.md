# T006 independent review

- Base commit: `2b75f555b116f26813e1646507a3ed1279b4ecc4`
- Candidate commit: `8753f769a0f1e945206eb4292ecfbbf914d38fb3`
- Verdict: **PASS**

## Checks

### deliverable-1-layer-map — PASS

Layer map covers every package under src/robinhood_lp/ with package defaults plus per-module overrides for the documented discovery multi-layer case.

Evidence:

- tools/check_imports/layer_map.py defines PACKAGE_DEFAULTS (one entry per package), EXACT_DEFAULTS (top-level robinhood_lp), and MODULE_OVERRIDES for individual modules.
- robinhood_lp.discovery is handled with PACKAGE_DEFAULTS = 'storage' plus per-module overrides: robinhood_lp.discovery.chain_capability = 'rpc adapter', robinhood_lp.discovery.asset_admission = 'risk', robinhood_lp.discovery.__init__ = 'application / orchestration'.
- robinhood_lp.config = PLATFORM_RESERVED_LAYER ('platform-reserved' sentinel) per ADR-006 §Migration trigger.
- classify() walks MODULE_OVERRIDES (longest match wins) then EXACT_DEFAULTS then PACKAGE_DEFAULTS (longest match wins); unclassified modules return None.

### deliverable-2-ast-rules — PASS

AST-only, deterministic, no project-import dependency direction / cycle / clock / unclassified-module checks all fire on seeded regressions.

Evidence:

- tools/check_imports/graph.py uses only ast.parse with filename=source (no compile/exec); cycle, lower-to-higher, unseeded-clock, and unclassified-module rules are implemented.
- TIER_ORDER (layer_map.py) is protocol/domain → rpc → storage → reconstruction → features → strategy (shared with backtest) → risk → execution → application/orchestration → presentation/reports, matching ADR-006 §Decision.
- CLOCK_FORBIDDEN_TIERS = {protocol/domain, reconstruction, features, strategy, backtest}; UNSEEDED_CLOCK_MODULES = {time, random, datetime}.
- Seeded regressions: test_layer_direction_seeded_regression, test_import_cycle_seeded_regression, test_unseeded_clock_seeded_regression, test_unclassified_module_seeded_regression — each builds a tempdir repo that breaks the rule and asserts the corresponding finding rule fires.
- All 17 T006 tests + 18 T005 tests pass: pytest tests/test_import_graph.py tests/test_documentation_citations.py → 35 passed in 5.42s.

### deliverable-3-section22-agreement — PASS

Layer-map / §2.2 agreement check is implemented and has a failing-input regression.

Evidence:

- tools/check_imports/architecture.py parses the §2.2 table from ARCHITECTURE.md and validate each task against todo/config.yaml.
- tools/check_imports/check.py iterates resolved rows, canonicalises the declared layer via canonical_layer() and compares with classify(module_name); disagreement produces an 'architecture-disagreement' finding.
- Unparsable rows (unknown task) fail closed via 'unknown-task' marker.
- Seeded regression: test_architecture_disagreement_seeded_regression declares features.bars as 'risk' in §2.2 (map says 'features'); test asserts architecture-disagreement finding fires.

### deliverable-4-suppressions-and-finding-report — PASS

Suppression carries the four required fields plus location/token; finding report includes module + imported module + layer pair + source line; raw edge list is byte-reproducible across runs.

Evidence:

- docs/implement/ci/suppressions.toml has new [[suppression]] entry with rule='layer-direction', reason='ADR-006 §Migration trigger reserves a platform layer for a future ADR; the robinhood_lp.config package sits outside the ten-layer ordering until then.', owner='@T006-owner', expiry='2027-06-30', plus location and token.
- Finding.Finding dataclass carries (rule, path, line, token, message); format_findings prints rule: path:line (token): message including module, imported module, layer pair, source line.
- tools/check_imports/__main__.py exposes 'edges' subcommand that prints a deterministic CSV (source,target,line). Two runs are byte-identical: diff /tmp/edges-1.txt /tmp/edges-2.txt returns no differences; 283 deterministic edges emitted (excluding header).
- Perturbation test: with the T006 suppression removed, run('.') reports exactly one finding — layer-direction, src/robinhood_lp/qualification/hook_pack.py, token 'robinhood_lp.qualification.hook_pack -> robinhood_lp.config.models' — matching the suppression token precisely.

### deliverable-5-ci-wiring — PASS

CI step is strictly additive; existing required checks unaffected.

Evidence:

- .github/workflows/ci.yml inserts a new step 'Import-graph dependency check (T006)' between 'Document citation check (T005)' and 'Ruff check' inside the existing quality job. Diff is strictly additive (only +18 lines, no removals).
- The step exits non-zero on findings (return 1 in __main__.main).
- No existing job/step was modified or weakened; pin SHA, permissions, env, and other steps are byte-identical.

### deliverable-6-seeded-regressions — PASS

Every rule in deliverables 2 and 3 has a seeded regression that fails the check when the rule is broken, mirroring the test_documentation_links.py / test_documentation_citations.py pattern.

Evidence:

- tests/test_import_graph.py provides 17 tests: 4 deliverable-2 seeded regressions (layer-direction, import-cycle, unseeded-clock, unclassified-module) + 1 deliverable-3 seeded regression (architecture-disagreement) + 4 deliverable-7 fail-closed tests (empty module set, unreadable suppression, unparsable §2.2 row, unused suppression) + 8 supporting tests.
- Each seeded regression creates a tmp_path repo that breaks the rule, runs run(root), and asserts the specific finding rule (and token, where applicable) appears. test_clean_repository_passes asserts the positive path on a synthetic repo and test_check_passes_on_real_repository asserts the same on the real REPO.

### deliverable-7-fail-closed — PASS

Unclassified modules, unreadable suppression entries, empty module sets, and unparsable §2.2 rows each fail closed.

Evidence:

- Empty module set: test_empty_module_set_fails_closed clears all modules from src/robinhood_lp/ and asserts module-set finding fires.
- Unreadable suppression entry: test_unreadable_suppression_entry_fails_closed writes 'this is not valid TOML !!!' to suppressions.toml and asserts a suppressions finding fires.
- Unparsable §2.2 row: test_unparsable_section22_row_fails_closed adds a row referencing T999 (absent from config) and asserts an architecture-disagreement finding referencing 'T999' fires.
- Unclassified module: test_unclassified_module_seeded_regression creates robinhood_lp.unknown_pkg.foo and asserts unclassified-module finding fires.

### deliverable-8-unused-suppression — PASS

Unused-suppression invariant is enforced: an entry that no longer matches a finding is itself fatal.

Evidence:

- check.py calls partition_used_and_unused with (relevant_entries, [(rule, path, token), ...]) and emits a suppression-unused finding for every entry not matched by a finding.
- test_unused_suppression_is_fatal seeds a suppression for a non-existent token and asserts a suppression-unused finding fires.
- Perturbation: writing a stale T006 entry with token 'robinhood_lp.no.such.module -> robinhood_lp.other' produces a suppression-unused finding (verified by direct test invocation).

### must-not-no-third-party-no-import-no-network — PASS

No third-party dependency added; no network access; no project module imported or executed at check time.

Evidence:

- tools/check_imports/ uses only stdlib modules: ast, json, re, tomllib, dataclasses, pathlib, collections.abc, argparse, sys. No third-party imports.
- pyproject.toml is unchanged in the candidate diff; requirements.in and requirements.lock.txt are unchanged.
- graph.py uses ast.parse without compile; it does not import or execute any robinhood_lp module.
- Verified empirically: importing tools.check_imports.check leaves sys.modules containing zero entries starting with 'robinhood_lp'.

### must-not-protected-paths — PASS

No protected path was touched by the implementation; only the controller's bookkeeping fields in todo/config.yaml advanced.

Evidence:

- git diff --name-only shows the candidate touches: .github/workflows/ci.yml, docs/implement/ci/suppressions.toml, tests/test_import_graph.py, todo/config.yaml, todo/evidence/P00/T006/attempt-001-developer.json, tools/check_citations/checker.py, and the six new tools/check_imports/* files.
- No changes to tools/workflow/, .claude/, todo/schemas/, todo/config.yaml field semantics (only the workflow-state machine fields workflow_state, status, attempt, base_commit updated — controller bookkeeping, not a contract edit), docs/spec/, docs/intent/, or other task contracts.
- git diff src/ is empty; git diff docs/spec/architecture/ARCHITECTURE.md is empty.

### must-not-no-product-change — PASS

No deletions, no tier-order relaxation, no product or interface change in src/.

Evidence:

- git diff src/ returns no output — no product code change.
- ARCHITECTURE.md §2.2 unchanged; TIER_ORDER in layer_map.py matches ADR-006's diagram verbatim (protocol/domain → rpc → storage → reconstruction → features → strategy → backtest → risk → execution → application/orchestration → presentation/reports).
- tools/check_citations/checker.py change is a one-block filter that scopes the suppression-unused check to T005 rules (frozenset _T005_RULES); it does not alter any T005 finding or weaken the T005 unused-suppression invariant for T005's own entries.
- No module, import, or §2.2 row was deleted; no tier order relaxation.

### developer-repro-commands — PASS

All reported commands reproduced exactly: 35 tests pass (>= 35), check passes, edges are byte-equal, ruff format/check clean, strict mypy clean.

Evidence:

- pytest tests/test_import_graph.py: 17 passed in 0.41s.
- pytest tests/test_import_graph.py tests/test_documentation_citations.py: 35 passed in 5.42s.
- python -m tools.check_imports check: 'import-graph check passed: no findings' (exit 0).
- python -m tools.check_imports edges run twice: byte-identical output (diff empty), 283 edges.
- ruff format --check tools/check_imports tools/check_citations tests/test_import_graph.py: '13 files already formatted' (clean).
- ruff check tools/check_imports tools/check_citations tests/test_import_graph.py: 'All checks passed!'.
- mypy --strict tools/check_imports tools/check_citations/checker.py tests/test_import_graph.py: 'Success: no issues found in 8 source files'.

### working-tree-clean — PASS

Review worktree clean, HEAD matches the recorded candidate commit, only the permitted review-result.json is being written.

Evidence:

- git status reports 'Not currently on any branch. nothing to commit, working tree clean.'
- HEAD = 8753f769a0f1e945206eb4292ecfbbf914d38fb3 (matches candidate commit recorded in workflow metadata).
- Only permitted write: .workflow/review-result.json (this file).

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- tools/check_citations/checker.py added a _T005_RULES filter that scopes its suppression-unused check to T005 rules. A future third checker that reads the same suppressions.toml must add its own rule-name filter or risk silently ignoring its unused entries — worth a sentence in the next checker's docstring.
