# T009 independent review

- Base commit: `3946c46da9819bc197bd7e5c4dddb0d8d2145d54`
- Candidate commit: `8222186982f265f00e7c1532a6792688ec8f9217`
- Verdict: **PASS**

## Checks

### head-matches-candidate — PASS

HEAD of the review worktree equals the declared candidate commit.

Evidence:

- /home/lpdev/lp-worktrees/review-t009-attempt-002 git rev-parse HEAD = 8222186982f265f00e7c1532a6792688ec8f9217

### required-change-1-no-cross-package-import — PASS

The cross-package import from ..check_citations.suppressions is gone; tools.check_acceptance now imports only its own .suppressions module and that module is stdlib-only (tomllib). The on-disk registry schema is preserved so all existing entries in docs/implement/ci/suppressions.toml still parse.

Evidence:

- tools/check_acceptance/checker.py lines 34-39 now read: 'from .suppressions import (SuppressionError, find_matching, partition_used_and_unused); from .suppressions import load as load_suppressions' (within-package relative imports only)
- grep -rn 'from \.\.|import tools\.check_citations' tools/check_acceptance/ returns no matches
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -c "import tools.check_acceptance; import sys; print('tools.check_citations' in sys.modules)" prints False
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -c "import tools.check_acceptance.checker; import sys; print('tools.check_citations' in sys.modules)" prints False
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -c "import tools.check_acceptance; import sys; print('tools.check_citations.suppressions' in sys.modules)" prints False
- After importing tools.check_acceptance and all six sub-modules (__main__, checker, parser, rules, suppressions, types, vocabulary), sys.modules contains only tools.* under tools.check_acceptance.* and stdlib; 'check_citations' and 'check_documentation_links' both False
- tools/check_acceptance/suppressions.py imports are stdlib-only: tomllib, collections.abc.Iterable, dataclasses.dataclass, pathlib.Path
- tools/check_acceptance/suppressions.py defines Suppression (dataclass with rule/reason/owner/expiry/location/token and a matches() method), SuppressionError, load(path), find_matching, partition_used_and_unused, and REQUIRED_FIELDS = ('rule', 'reason', 'owner', 'expiry')
- load() accepts both layouts (inline suppressions = [...] and [[suppression]] array-of-tables) and validates required fields and unknown fields, raising SuppressionError on any deviation
- Spot-check of docs/implement/ci/suppressions.toml via the new loader: 9 entries parsed, all carrying rule/reason/owner/expiry plus optional location and token; the prior acceptance-present and acceptance-quantitative entries (including the '<= -80%' and 'above 100%' tokens) keep parsing unchanged

### required-change-2-tests-and-cli-still-pass — PASS

After the refactor, pytest tests/test_acceptance.py passes 25/25 and the CLI exits 0 with 'acceptance check passed: no findings', the closed vocabulary is printed, and the registry is reused unchanged.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/test_acceptance.py -v: 25 passed in 0.12s (the same 25 cases enumerated in review-001: test_check_passes_on_clean_contract, the three seeded regressions for families A/B/C plus the positive control, the four fail-closed cases, the four record-/format-/vocabulary tests, test_contract_set_covers_required_roots, test_check_passes_on_real_repository, test_finding_dataclass_supports_set_membership, and the six parametrized test_comparison_phrase_extraction cases including '<= -80%' and 'above 100%')
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.check_acceptance check --repo-root . exits 0 with output ending in 'acceptance check passed: no findings'
- Same CLI invocation prints the closed vocabulary in order: verification surfaces, explicit outcome terms, placeholder markers, comparison operators, unit terms
- subprocess.run(['python', '-m', 'tools.check_acceptance', 'check', '--repo-root', '.']) returns exit code 0 and prints the vocabulary header
- Closed registry entries are reused: docs/implement/ci/suppressions.toml parses 9 entries with the T006 four-field schema (rule/reason/owner/expiry) plus optional location/token; the load is strict on missing/unknown fields and raises SuppressionError, matching the prior fail-closed behaviour

### check-exits-zero-on-real-repo — PASS

Acceptance check exits 0 on the committed config and walks every todo/phases/**/T[0-9]{3}.md contract.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.check_acceptance check --repo-root . exits 0 with the final line 'acceptance check passed: no findings'

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
- docs/implement/ci/suppressions.toml adds five entries with rule/reason/owner/expiry and 2026-12-31 expiry; the new loader parses all nine entries with the T006 four-field schema and rejects missing/unknown fields

### ci-is-strictly-additive — PASS

The check is wired into CI as a strictly additive required check.

Evidence:

- git diff base..candidate for .github/workflows/ci.yml inserts only an 'Acceptance-criteria check (T009)' step between the T006 step and the existing Ruff/mypy gates; no existing job is weakened, skipped, or xfailed
- The new step has no continue-on-error, no if: always() override, and no skip pattern

### fail-closed-on-bad-inputs — PASS

Missing todo/phases/, empty contract set, contract with no Acceptance, unreadable/unparsable suppression entry, and an empty scan each fail closed instead of being skipped or defaulted.

Evidence:

- tests/test_acceptance.py::test_missing_contract_set_fails_closed PASSED
- tests/test_acceptance.py::test_empty_contract_set_fails_closed PASSED
- tests/test_acceptance.py::test_unreadable_suppression_entry_fails_closed PASSED
- tools/check_acceptance/suppressions.py raises SuppressionError for missing/unreadable/invalid files, missing array, missing required fields, and unknown fields; checker.run() converts the exception into a single 'suppressions' finding
- Manual reproduction with an empty contract set + the checker returns a single contract-set finding rather than an empty result

### imports-are-stdlib-only — PASS

At check time, tools.check_acceptance loads only stdlib and its own within-package modules; the prior cross-package import into tools.check_citations is removed.

Evidence:

- tools/check_acceptance/suppressions.py imports only stdlib (tomllib, collections.abc.Iterable, dataclasses.dataclass, pathlib.Path)
- After importing tools.check_acceptance and every sub-module, sys.modules contains only tools.check_acceptance.* plus stdlib; tools.check_citations and tools.check_documentation_links are not loaded
- tools/check_acceptance/{__init__,__main__,checker,parser,rules,types,vocabulary}.py each import only stdlib and within-package modules

### no-edits-to-protected-files-or-vocabulary-narrowing — PASS

No protected file was edited, no contract was edited to silence a finding (the suppression registry was used), and the declared vocabulary was not narrowed to make any finding pass.

Evidence:

- git diff 3946c46 8222186 --name-only lists only: .github/workflows/ci.yml, docs/implement/ci/suppressions.toml, tests/test_acceptance.py, todo/config.yaml, todo/evidence/P00/T009/attempt-001-developer.json, todo/evidence/P00/T009/attempt-002-developer.json, todo/reviews/P00/T009/review-001.json, todo/reviews/P00/T009/review-001.md, tools/check_acceptance/{__init__,__main__,checker,parser,rules,suppressions,types,vocabulary}.py
- No edits to docs/intent/, docs/spec/, tools/workflow/, .claude/, todo/schemas/
- todo/config.yaml changes are the routine workflow transition: status READY -> AWAITING_REVIEW, attempt = 2, base_commit = 3946c46..., candidate_commit = 0ced44b..., latest_review set to review-001.json
- No task contract was edited; pre-existing findings against T000, T023, T092, and T009's own quantitative self-reference are recorded in docs/implement/ci/suppressions.toml with rule/reason/owner/expiry
- Vocabulary lists in tools/check_acceptance/vocabulary.py: VERIFICATION_SURFACES widened (tests/, test_, pytest, ruff, mypy, python -m, git, command, build, install, fixture, golden, vector, manifest, report, ledger, schema, replay, checksum, sha256, sha-256, digest), PLACEHOLDER_MARKERS == contract's five entries, COMPARISON_OPERATORS == contract's enumerated operators, UNIT_TERMS widened with percent/USDG/USD/ETH/wei/gwei/gas/sec/seconds/minutes/hours/days/blocks/bytes/MB/KB/GB/Mb/Kb/Gb/ms; nothing was narrowed or removed

### vocabulary-and-suppressions-printed-together — PASS

The closed vocabulary is printed with the findings so a reviewer can reproduce every match by hand.

Evidence:

- python -m tools.check_acceptance check --repo-root . prints 'Declared closed vocabulary: verification surfaces [...], explicit outcome terms [...], placeholder markers [...], comparison operators [...], unit terms [...]' followed by 'acceptance check passed: no findings'
- tests/test_acceptance.py::test_format_findings_prints_vocabulary PASSED; assert verifies all five lists appear in the output

### ruff-format-check — PASS

Ruff format and ruff check pass on the new files.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff format --check tools/check_acceptance/ tests/test_acceptance.py: '9 files already formatted'
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff check tools/check_acceptance/ tests/test_acceptance.py: 'All checks passed!'

### mypy-strict — PASS

Strict mypy passes on the new package and test module.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy --strict tools/check_acceptance/ tests/test_acceptance.py: 'Success: no issues found in 9 source files'

### pytest-test-acceptance — PASS

The T009 test module passes 25/25; the pattern test_documentation_links.py is unaffected (also passes).

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/test_acceptance.py -v: 25 passed in 0.12s
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/test_documentation_links.py: 2 passed

### full-pytest-no-new-regressions — PASS

No new test regressions introduced by the candidate commit.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest: 2054 passed, 6 skipped, 2 failed
- The 2 failures (tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output and tests/test_documentation_citations.py::test_check_passes_on_real_repository) are pre-existing on base 3946c46 and unrelated to T009 (Foundry not on PATH; a pre-existing T005 citation finding on docs/implement/DOCUMENTATION_INTEGRITY_AUDIT.md:242)

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- None.
