# T009 independent review

- Base commit: `3946c46da9819bc197bd7e5c4dddb0d8d2145d54`
- Candidate commit: `0ced44b3d649731b0f058fd4f2968b4cab2f48ca`
- Verdict: **FAIL**

## Checks

### head-matches-candidate — PASS

HEAD of the review worktree equals the declared candidate commit.

Evidence:

- /home/lpdev/lp-worktrees/review-t009-attempt-001 git rev-parse HEAD = 0ced44b3d649731b0f058fd4f2968b4cab2f48ca

### check-exits-zero-on-real-repo — PASS

Acceptance check exits 0 on the committed config and walks every todo/phases/**/T[0-9]{3}.md contract.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.check_acceptance check --repo-root . exited 0 with output 'acceptance check passed: no findings'

### rule-family-A-seeded-regression — PASS

Rule family A has a seeded regression that breaks when a contract has no Acceptance section or an empty one, and the check fires (acceptance-present).

Evidence:

- tests/test_acceptance.py::test_acceptance_present_seeded_regression PASSED
- tests/test_acceptance.py::test_acceptance_empty_seeded_regression PASSED

### rule-family-B-seeded-regression — PASS

Rule family B has a seeded regression; the closed placeholder vocabulary is committed and printed; a fixture with the markers produces findings.

Evidence:

- tests/test_acceptance.py::test_acceptance_placeholder_seeded_regression PASSED; matches TBD, TODO, to be determined, as appropriate, if possible

### rule-family-C-seeded-regression-with-historical-cases — PASS

Rule family C has a seeded regression using the two historical cases (<= -80% USDG return, above 100% return) and a positive control that confirms the rule does not fire when Acceptance mirrors Deliverables.

Evidence:

- tests/test_acceptance.py::test_acceptance_quantitative_seeded_regression PASSED with tokens '<= -80%' and 'above 100%'
- test_acceptance_quantitative_passes_when_acceptance_covers_rule PASSED

### finding-report-names-section-line-and-text — PASS

Every finding carries rule, path, line, token (matched text) and a message; the closed vocabulary is printed alongside the findings.

Evidence:

- tests/test_acceptance.py::test_acceptance_present_records_section_and_line PASSED
- tests/test_acceptance.py::test_acceptance_quantitative_records_matched_text PASSED
- tests/test_acceptance.py::test_format_findings_prints_vocabulary PASSED

### exception-list-reuses-T006-registry — PASS

Exception list reuses the T006 registry (rule/reason/owner/expiry); an unreadable entry fails closed and an unused entry is fatal.

Evidence:

- tests/test_acceptance.py::test_unreadable_suppression_entry_fails_closed PASSED
- tests/test_acceptance.py::test_stale_suppression_is_fatal PASSED
- docs/implement/ci/suppressions.toml adds five entries with rule/reason/owner/expiry and 2026-12-31 expiry

### ci-is-strictly-additive — PASS

The check is wired into CI as a strictly additive required check.

Evidence:

- git diff for .github/workflows/ci.yml inserts only an 'Acceptance-criteria check (T009)' step between the T006 step and the existing Ruff/mypy gates; no existing job is weakened, skipped, or xfailed
- The new step has no continue-on-error, no if: always() override, and no skip pattern

### fail-closed-on-bad-inputs — PASS

Missing todo/phases/, empty contract set, contract with no Acceptance, unreadable/unparsable suppression entry, and an empty scan each fail closed instead of being skipped or defaulted.

Evidence:

- tests/test_acceptance.py::test_missing_contract_set_fails_closed PASSED
- tests/test_acceptance.py::test_empty_contract_set_fails_closed PASSED
- tests/test_acceptance.py::test_unreadable_suppression_entry_fails_closed PASSED
- Manual reproduction with empty suppressions = [] + a contract missing Acceptance fired 'acceptance-present' (no findings were silently dropped)

### imports-are-stdlib-only — FAIL

tools/check_acceptance/checker.py imports from ..check_citations.suppressions at check time. This is a cross-package import of a project module (tools.check_citations.suppressions) and violates the T009 Must-not clause that forbids importing or executing a project module at check time. The check should load and match the suppression registry using only the standard library (e.g. tomllib), as tools.check_citations.suppressions itself does. The T006 suppression infrastructure uses within-package relative imports (tools.check_citations.*) and the same discipline should have been applied here.

Evidence:

- tools/check_acceptance/checker.py lines 24-28: 'from ..check_citations.suppressions import (SuppressionError, find_matching, partition_used_and_unused); from ..check_citations.suppressions import load as load_suppressions'
- sys.modules after import tools.check_acceptance: 'tools.check_citations' and 'tools.check_citations.suppressions' are loaded
- tools/check_citations/suppressions.py itself only imports stdlib (tomllib, dataclasses, pathlib); the check_acceptance loader therefore transitively imports a non-stdlib project module
- Contract Must not (T009): 'add a third-party dependency or a network read, or import or execute a project module at check time'

### no-edits-to-protected-files-or-vocabulary-narrowing — PASS

No protected file was edited substantively, no contract was edited to silence a finding (the suppression registry was used), and the declared vocabulary was not narrowed to make any finding pass.

Evidence:

- git diff base..candidate touches .github/workflows/ci.yml, docs/implement/ci/suppressions.toml, tests/test_acceptance.py, todo/config.yaml, todo/evidence/P00/T009/attempt-001-developer.json, and the new tools/check_acceptance/ package
- No edits to docs/intent/, docs/spec/, tools/workflow/, .claude/, todo/schemas/
- todo/config.yaml changes match the established pattern of every prior feat(tXXX): candidate attempt N commit (status READY -> AWAITING_REVIEW, attempt++, base_commit); they are workflow-state transitions, not contract edits
- No task contract was edited; pre-existing findings against T000, T023, T092, and T009's own quantitative self-reference are recorded in docs/implement/ci/suppressions.toml with rule/reason/owner/expiry
- Vocabulary lists (verification_surfaces, placeholder_markers, comparison_operators, unit_terms) are widened, not narrowed: e.g. 'tests/', 'test_', 'replay', 'sha256' are added alongside the contract's enumerated surfaces

### vocabulary-and-suppressions-printed-together — PASS

The closed vocabulary is printed with the findings so a reviewer can reproduce every match by hand.

Evidence:

- python -m tools.check_acceptance check --repo-root . prints 'Declared closed vocabulary: verification surfaces [...], explicit outcome terms [...], placeholder markers [...], comparison operators [...], unit terms [...]' followed by 'acceptance check passed: no findings'
- tests/test_acceptance.py::test_format_findings_prints_vocabulary PASSED; assert verifies all five lists appear in the output

### ruff-format-check — PASS

Ruff format and ruff check pass on the new files.

Evidence:

- ruff format --check tools/check_acceptance/ tests/test_acceptance.py: '8 files already formatted'
- ruff check tools/check_acceptance/ tests/test_acceptance.py: 'All checks passed!'

### mypy-strict — PASS

Strict mypy passes on the new package and test module.

Evidence:

- mypy --strict tools/check_acceptance/ tests/test_acceptance.py: 'Success: no issues found in 8 source files'

### pytest-test-acceptance — PASS

The T009 test module passes 25/25; the pattern test_documentation_links.py is unaffected (also passes).

Evidence:

- pytest tests/test_acceptance.py: 25 passed in 0.12s (test_check_passes_on_clean_contract, test_acceptance_present_seeded_regression, test_acceptance_empty_seeded_regression, test_acceptance_placeholder_seeded_regression, test_acceptance_quantitative_seeded_regression, test_acceptance_quantitative_passes_when_acceptance_covers_rule, test_missing_contract_set_fails_closed, test_empty_contract_set_fails_closed, test_unreadable_suppression_entry_fails_closed, test_stale_suppression_is_fatal, test_unused_placeholder_marker_is_a_finding, test_acceptance_present_records_section_and_line, test_acceptance_quantitative_records_matched_text, test_contract_set_covers_required_roots, test_check_passes_on_real_repository, test_finding_dataclass_supports_set_membership, test_format_findings_prints_vocabulary, test_format_findings_handles_empty, test_vocabulary_lists_match_contract, and the six parametrized test_comparison_phrase_extraction cases including '<= -80%' and 'above 100%')

### full-pytest-no-new-regressions — PASS

No new test regressions introduced by the candidate commit.

Evidence:

- pytest (whole repo): 2054 passed, 6 skipped, 2 failed
- The 2 failures (tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output and tests/test_documentation_citations.py::test_check_passes_on_real_repository) are pre-existing on base 3946c46 and unrelated to T009 (Foundry not on PATH; a pre-existing T005 citation finding on docs/implement/DOCUMENTATION_INTEGRITY_AUDIT.md:242)

## Must-not violations

- tools/check_acceptance/checker.py imports from ..check_citations.suppressions at check time. The T009 contract's Must-not clause forbids 'import or execute a project module at check time'; tools.check_citations.suppressions is a project module. The check should load and match the suppression registry using only the standard library (e.g. tomllib) and inline the matching logic, the same way tools.check_citations/suppressions.py itself does.

## Unknowns

- None.

## Required changes

- Replace the cross-package import 'from ..check_citations.suppressions import (...)' in tools/check_acceptance/checker.py with a stdlib-only loader. Use tomllib (already in the stdlib) to parse docs/implement/ci/suppressions.toml and implement find_matching / partition_used_and_unused inline. The closed registry format (rule/reason/owner/expiry/location/token) must remain so the existing suppression entries stay valid; only the Python loader changes.
- After the refactor, verify tools.check_acceptance no longer pulls in tools.check_citations (sys.modules inspection or a smoke test), and re-run pytest tests/test_acceptance.py plus python -m tools.check_acceptance check --repo-root . to confirm the check still passes on the committed repository.

## Residual risks

- None.
