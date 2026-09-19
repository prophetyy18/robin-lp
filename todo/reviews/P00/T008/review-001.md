# T008 independent review

- Base commit: `9075783e6086bc5ff99d66334bfa0b09219c7fc2`
- Candidate commit: `33b539d65d00d61dfb2ca5d000a632de6fa2b290`
- Verdict: **PASS**

## Checks

### t008.acceptance.1.default-render — PASS

Default renderer prints every configured phase and task exactly once at the phase level, splits APPROVED into live and superseded with successor named, surfaces the in-progress task, and computes the ready-next set from the config.

Evidence:

- `python -m tools.progress` exits 0 and prints 857 lines covering every configured phase (P00-P10, 11 phases) each with its purpose from the phase README (e.g. 'P00 - purpose: make architectural and safety choices reviewable before code creates compatibility commitments.'), every task (65 tasks) with id, title from contract, Outcome from contract, phase and status.
- APPROVED set is split into 'Approved (live)' (T000-T006, T010-T015, T020, T021, T024-T027, T030-T037, T039-T043, T049-T053, T060-T062) and 'Approved (superseded)' with successor named (T022 -> T026, T023 -> T027, T038 -> T039).
- T008 appears under 'In progress' with status AWAITING_REVIEW.
- Ready-next set lists T007, T009, T063, T067, T070, T100, T104 - matches the documented `ready` condition applied to the committed config (every PLANNED task whose dependencies are all APPROVED and none superseded).
- `Blocked / changes-requested / triage-required / owner-decision` section shows '(none)' since no task is in those states.

### t008.acceptance.2.on-demand-no-cache — PASS

Renderer writes nothing to disk and leaves the working tree unchanged across runs.

Evidence:

- `git status --porcelain` is empty before and after `python -m tools.progress` and `python -m tools.progress --check` invocations (diff between before.txt and after.txt is empty).
- `git ls-files` shows no rendered plan file or cache under tools/progress/ other than render.py itself.

### t008.acceptance.3.facts-from-config-no-stale-tokens — PASS

Every fact in the view comes from todo/config.yaml at run time; mutating the config changes the view accordingly; no commit SHA, test count or wall-clock timestamp is ever printed.

Evidence:

- Spot-check: mutated config flipping T007 status to AWAITING_REVIEW and T038 superseded_by from T039 to T040 produces a correspondingly different view (T007 leaves Planned and Ready-next, T007 enters In progress; T038 superseded target switches from T039 to T040 in both phase listing and 'Approved (superseded)' section).
- Regex `\b[0-9a-f]{40}\b` matches zero SHA-shaped tokens in the rendered output.
- No HH:MM wall-clock pattern (`\b\d{1,2}:\d{2}\b`) appears in the output.
- Substrings 'pass count', 'test count', 'elapsed', 'wall' are absent.
- All 80 commit SHAs present in todo/config.yaml are absent from the rendered output (verified by direct substring search).

### t008.acceptance.4.check-fails-closed — PASS

Each declared failure input (unreadable config, unparsable config, status outside state machine, unresolved dependency, unresolved supersession, missing contract file, superseded-but-not-approved) produces a non-zero exit and a plain-language stable code in stderr; --check exits 0 on the committed plan.

Evidence:

- `python -m tools.progress --check` exits 0 on the committed config and prints 'no configuration issues found'.
- Test `test_check_fails_on_unreadable_config` asserts exit!=0 and stderr contains 'CONFIG_UNPARSABLE' when the config is not JSON.
- Test `test_check_fails_on_missing_config` asserts exit!=0 and stderr contains 'CONFIG_UNREADABLE' when the file is absent.
- Test `test_check_fails_on_status_outside_state_machine` asserts exit!=0 and stderr contains 'STATUS_OUTSIDE_STATE_MACHINE' when status='READY_FOR_RELEASE'.
- Test `test_check_fails_on_unresolved_dependency` asserts exit!=0 and stderr contains 'DEPENDENCY_UNRESOLVED' when depends_on includes T999.
- Test `test_check_fails_on_unresolved_supersession` asserts exit!=0 and stderr contains 'SUPERSESSION_UNRESOLVED' when superseded_by='T999'.
- Test `test_check_fails_on_missing_contract_file` asserts exit!=0 and stderr contains 'CONTRACT_MISSING' when a configured task_file does not exist.
- Test `test_check_fails_on_supersede_without_approved_status` asserts exit!=0 and stderr contains 'SUPERSEDED_BUT_NOT_APPROVED' when a PLANNED task declares superseded_by.

### t008.acceptance.5.committed-tests — PASS

Committed tests cover both required cases: renderer against committed config (phase+task coverage plus ready-next), and check-mode failure on each declared failure input. All 22 new tests pass and existing workflow tests are unaffected.

Evidence:

- `pytest tests/test_progress.py -q`: 22 passed in 0.67s.
- Test `test_render_against_committed_config_covers_every_phase_and_task` loads the committed config, validates it, builds the view, asserts every configured phase appears exactly once and every configured task appears with status/title/outcome, and asserts the renderer-reported ready-next set equals the predicate's ready-next set.
- Seven check-failure-input tests prove the check can fail on every declared failure input.
- `pytest tests/test_progress.py tests/test_workflow_contracts.py tests/test_workflow.py`: 70 passed (44 + 4 + 22).

### t008.constraints.location-and-deps — PASS

Renderer lives under tools/progress/, outside tools/workflow/ and outside src/robinhood_lp/. No third-party dependency; no network read.

Evidence:

- `ls tools/`: __init__.py, check_citations, check_imports, oracle, progress, reviewers, t024, workflow. tools/progress/ is a sibling of tools/workflow/, not inside it.
- `ls tools/progress/`: __init__.py, __main__.py, check.py, render.py - no module under src/robinhood_lp/.
- All imports in tools/progress/*.py are Python stdlib (argparse, collections.abc, dataclasses, json, pathlib, re, sys, typing). Only test_progress.py imports pytest.
- No `import urllib`, `import requests`, `import socket`, or `import http` in tools/progress/*.py.

### t008.constraints.protected-files — PASS

Candidate commit touches no protected file. The todo/config.yaml change is the workflow controller's routine finish-develop transition, not a renderer or Developer write to a protected file.

Evidence:

- `git diff --name-only 9075783..33b539d` lists only: tests/test_progress.py, todo/config.yaml, todo/evidence/P00/T008/attempt-001-developer.json, tools/progress/__init__.py, tools/progress/__main__.py, tools/progress/check.py, tools/progress/render.py.
- No file under src/, docs/, tools/workflow/, todo/schemas/, .claude/, or any todo/phases/ contract file is modified.
- The todo/config.yaml delta is the controller's finish-develop status transition (status READY -> AWAITING_REVIEW, attempt 0->1, base_commit set to 9075783e...); the renderer code itself never writes to todo/config.yaml at runtime.

### t008.constraints.static-checks — PASS

ruff check, ruff format --check and mypy all pass on the new files.

Evidence:

- `ruff check tools/progress tests/test_progress.py`: 'All checks passed!'.
- `ruff format --check tools/progress tests/test_progress.py`: '5 files already formatted'.
- `mypy tools/progress tests/test_progress.py`: 'Success: no issues found in 5 source files'.

### t008.constraints.no-duplicate-of-t005-t006 — PASS

T008 does not duplicate the citation checks T005 owns or the layer-import checks T006 owns; its checks are the plan-rendering checks the contract declares.

Evidence:

- tools/progress/check.py implements only plan-level checks (readable/parsable config, status within state machine, resolved dependency/supersession pointers, contract file presence, superseded-but-not-approved).
- No mention of citations, layer imports, ADR-006, or tools/check_citations / tools/check_imports inside tools/progress/*.py.
- tests/test_progress.py explicitly states 'intentionally independent of every other T005 / T006 check' in its module docstring.

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- None.
