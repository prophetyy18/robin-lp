# M0004 independent review

- Base commit: `09e082aab68772bce9ab4dbc2dc284d507632e1e`
- Candidate commit: `0aeeb0120194afbb5d036ee1d7650df1fdc85c04`
- Verdict: **PASS**

## Checks

### contract-binding — PASS

Frozen request, base commit, candidate commit, branch and attempt all agree with the controller record; the detached review worktree is the exact clean candidate commit.

Evidence:

- sha256 of todo/maintenance/M0004/request.json in the candidate = 8213bf2ccbcb8ac429fb1c05bba2b6aa22d11f6711d20b88035bd1f11d4fdc3b = the controller's frozen copy at /home/lpdev/lp/.git/robinhood-lp-workflow/M0004-request.json (diff empty)
- git rev-list --parents -1 0aeeb0120194afbb5d036ee1d7650df1fdc85c04 => '0aeeb01 09e082a': the candidate's only parent is the declared base commit, so base..candidate is exactly the candidate commit
- review worktree HEAD = 0aeeb0120194afbb5d036ee1d7650df1fdc85c04 and git status --porcelain is empty
- runtime record /home/lpdev/lp/.git/robinhood-lp-workflow/M0004.json: attempt 1, branch maintenance/m0004-attempt-001, base 09e082a, candidate 0aeeb01, status AWAITING_REVIEW
- todo/config.yaml T109 record: status APPROVED, approved_commit c79e4c83debd0dc7e8996afb612c103fcb8b2e97, satisfying related_task

### scope-allowed-paths — PASS

The developer edited exactly the four predeclared allowed_paths files. No protected, governance, dependency-manifest, schema, task or risk/execution/signer path is in the diff; the two added files are controller-authored lane records.

Evidence:

- git diff --name-status 09e082aab68772bce9ab4dbc2dc284d507632e1e 0aeeb0120194afbb5d036ee1d7650df1fdc85c04 => M src/robinhood_lp/__main__.py, M src/robinhood_lp/backtest/engine.py, M src/robinhood_lp/protocol/contracts.py, M tests/test_t109_acceptance.py, A todo/maintenance/M0004/developer-001.json, A todo/maintenance/M0004/request.json
- the two added todo/maintenance/M0004 records are written by finish-maintenance-develop itself (tools/workflow/core.py lines 3096-3100), the same artifact class M0002 review-001 excluded as 'workflow artifacts written by prepare-maintenance, not implementation changes'
- grep of the changed-path list for ^(\.claude/|\.github/|docs/intent/|docs/spec/|todo/phases/|todo/schemas/|todo/config\.yaml|tools/workflow/|src/robinhood_lp/(execution|risk|signer)/|pyproject\.toml|requirements\.) produced no matches (grep exit 1)
- git diff --numstat for the four files: 21 insertions / 50 deletions total (7/22, 1/3, 1/3, 12/22), matching the developer handoff
- todo/maintenance/M0004/request.json did not exist at base (git show 09e082a:... => 'exists on disk, but not in 09e082a'), so it is a lane record the controller added

### formatting-only-diff — PASS

The diff is formatting-only: identical AST, identical literal constants, identical def/class set and no comment edits, so no product behavior, public interface, data schema or control flow changed.

Evidence:

- independent comparison (not the listed one, which compares HEAD to the clean worktree) of git show 09e082a:<path> vs git show 0aeeb01:<path> with ast.dump(ast.parse(...)): BASE_VS_CANDIDATE_AST_IDENTICAL for all four files
- all ast.Constant values compare equal between base and candidate (383 constants in __main__.py, 211 in engine.py, 133 in contracts.py, 676 in test_t109_acceptance.py) and all FunctionDef/AsyncFunctionDef/ClassDef name sequences compare equal
- git diff -U0 09e082a 0aeeb01 over the four files filtered for changed lines matching '^[+-]\s*#' returned no lines: no comment was edited
- manual read of the complete diff: only implicit-literal joins (help=('Cancel a queued or running run. A terminal record is left untouched.'), the two joined f-strings in tests/test_t109_acceptance.py), call/comprehension re-wrapping and def-signature joins; no token value or control-flow change
- the string constant change is provably value-preserving: adjacent literal concatenation yields the identical Constant node, and the f-string fusion yields the identical JoinedStr dump

### defect-reproduction — PASS

The claimed defect is reproduced exactly and only these four files were red at base; the request reason's historical attribution checks out against the named commits and the approved T109 review record.

Evidence:

- scan of all 271 tracked .py files at base through the pinned formatter's stdin path (git show 09e082a:<p> | python -m ruff format --check --stdin-filename <p> -): exactly 4 unformatted, and they are exactly the four allowed_paths files
- same stdin check on the candidate blobs: all four exit 0 (formatted)
- .github/workflows/ci.yml:104 is 'run: conda run -n "$CONDA_ENV_NAME" ruff format --check .', the gate named in the request
- history attribution verified: engine.py/contracts.py/test_t109_acceptance.py exit 0 at T109 base 791fc4e and exit 1 at T109 candidate c79e4c8; __main__.py exits 1 at T069 candidate 2f64e64
- todo/reviews/P06/T109/review-004.json:269 records only 'ruff check' (lint) and flags src/robinhood_lp/__main__.py:220 reformat drift as pre-existing/out of scope

### ruff-format-check — PASS

The repo-wide format gate the repair targets is green on the candidate.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff format --check . in the review worktree => '549 files already formatted' (exit 0), run once

### ruff-check — PASS

The repo-wide lint gate is green on the candidate.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff check . in the review worktree => 'All checks passed!' (exit 0), run once

### ast-check-command — PASS

The listed AST command passes as written; its meaningful base-vs-candidate form also proves semantic identity of the four files.

Evidence:

- the exact listed command was run once: output 'AST_IDENTICAL' (exit 0)
- limitation noted: in a detached review worktree whose HEAD is the candidate and whose tree is clean, that command compares HEAD against identical working files, so it is vacuous here; the substantive form (base blob vs candidate blob) was re-run and is recorded under formatting-only-diff

### pytest-listed-command — PASS

No test failure is attributable to the candidate. The literal command cannot complete locally because the conda env's editable install points at a deleted worktree (pre-existing and diff-independent); with the repository-documented PYTHONPATH=src the suite is 3108 passed / 6 pre-existing skips / 1 environmental Foundry failure already classified pre-existing on base by T109 triage-003 and review-004.

Evidence:

- exact listed command '/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest -q' => 'Interrupted: 69 errors during collection' (exit 2); all 69 errors are "ModuleNotFoundError: No module named 'robinhood_lp'" (counted 69/69)
- cause, verified not assumed: pip show robinhood-lp => 'Editable project location: /home/lpdev/lp-worktrees/dev-t102-attempt-001', and that directory does not exist; 'python -c "import robinhood_lp"' fails identically, so no test module and none of the four reformatted files is ever read
- documented workaround run once: PYTHONPATH=src <env-python> -m pytest -q => '1 failed, 3108 passed, 6 skipped in 33.58s' (exit 1)
- the single failure is tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output, failing inside forge with 'tools/oracle/lib/v4-core/src/types/Currency.sol: No such file or directory'; that directory is absent in this worktree and present in /home/lpdev/lp, and the test file is untouched by the diff (git diff --name-only for it is empty)
- the 6 skips (3 Foundry-not-on-PATH, 2 gpg-attestation, 1 unsorted-currency) match the pre-existing skips recorded in M0002 review-001
- the changed test file alone passes: PYTHONPATH=src ... -m pytest tests/test_t109_acceptance.py -q => '22 passed'
- CI provisions the project with 'pip install -e ".[dev]"' (ci.yml:206), so the editable-install failure exists only in this local environment

### git-diff-check — PASS

No whitespace or line-ending errors in the candidate diff.

Evidence:

- literal listed command 'git diff --check' in the clean review worktree => no output (exit 0)
- git diff --check 09e082aab68772bce9ab4dbc2dc284d507632e1e 0aeeb0120194afbb5d036ee1d7650df1fdc85c04 => no output (exit 0)

### maintenance-eligibility — PASS

The repair fits the low-risk maintenance boundary: a reproduced implementation defect repaired within four predeclared paths with no product, interface, dependency, safety, execution, signer, Intent, Spec, task-contract or controller behavior change.

Evidence:

- request risk_attestation = LOW_RISK_IMPLEMENTATION_DEFECT; allowed_paths = 4 explicit normalized, non-glob relative paths, none matching MAINTENANCE_FORBIDDEN_FILES or MAINTENANCE_FORBIDDEN_PREFIXES (tools/workflow/core.py:168-187)
- the diff changes no Intent, Spec, task contract, schema, dependency manifest, controller or governance file, and no risk/execution/signer code; nothing adds signing, broadcast, key handling or external side effects
- semantics are preserved (AST identity), so there is no product-behavior, public-interface or data-schema change
- the failure and repair have concrete verification commands, all exercised once above
- related_task T109 is APPROVED in todo/config.yaml

### reviewer-write-boundary — PASS

The review changed nothing in the worktree except its own handoff; no fix or repair was attempted.

Evidence:

- after running every listed command plus the independent checks, 'git status --porcelain' and 'git ls-files --others --exclude-standard' in the review worktree are both empty (pytest/ruff caches are gitignored)
- the only file this review writes is .workflow/review-result.json

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- The listed command '<env-python> -m pytest -q' is not green locally: the conda env's editable install of robinhood_lp still points at the deleted worktree /home/lpdev/lp-worktrees/dev-t102-attempt-001, so collection fails with ModuleNotFoundError before any product code loads. This is pre-existing and diff-independent (it reproduces without the candidate) and only CI, which runs 'pip install -e ".[dev]"', exercises the command as written; the equivalent local verification needs the PYTHONPATH=src prefix documented in M0002 review-001.
- tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output fails in any checkout that lacks the forge-installed tools/oracle/lib dependencies (present in /home/lpdev/lp, absent in this worktree). Already classified pre-existing and out of scope by todo/triage/P06/T109/triage-003.json and recorded in the approved T109 review-004; it does not exercise the four reformatted files.
- The root-cause drift (files left unformatted by the approved T109 candidate c79e4c8, whose review ran only 'ruff check') is repaired for these four files only; any future edit that reintroduces unformatted text turns CI step 'Ruff format check' red again, which is the intended gate behavior.
