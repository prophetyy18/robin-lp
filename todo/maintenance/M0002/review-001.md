# M0002 independent review

- Base commit: `eee15b76c6cbd20b2a3d7f5c4fd4a35fd89897ea`
- Candidate commit: `e20c35c1b956795dcd0701ed68c558dbd42231ca`
- Verdict: **PASS**

## Checks

### scope-diff — PASS

Exactly the 4 allowed_paths files were modified for the implementation (1 new, 3 modified). No forbidden MAINTENANCE paths touched. No Intent, Spec, task contract, public-interface, dependency, risk, execution, signer, or controller surface altered.

Evidence:

- git diff eee15b7 e20c35c --name-only returns: src/robinhood_lp/py.typed, src/robinhood_lp/quality/sample_selection.py, src/robinhood_lp/storage/reader.py, tests/test_storage_reader.py, todo/maintenance/M0002/developer-001.json, todo/maintenance/M0002/request.json
- The 4 implementation files match allowed_paths exactly (1 new py.typed + 3 modified); the other 2 are workflow artifacts written by prepare-maintenance, not implementation changes
- git diff name-only | grep -E '(docs/intent|docs/spec|\.claude/|tools/workflow/|todo/config\.yaml|pyproject\.toml|requirements\.lock\.txt)' produced no output

### py-typed — PASS

src/robinhood_lp/py.typed is a 0-byte PEP 561 marker placed at the package root without touching pyproject.toml. mypy no longer emits any of the 5 prior 'import-untyped' warnings (mypy reports 0 issues across 91 files).

Evidence:

- ls -la src/robinhood_lp/py.typed shows -rw-rw-r-- 1 lpdev lpdev 0
- wc -c src/robinhood_lp/py.typed = 0 (correct PEP 561 empty marker)
- git show eee15b7:src/robinhood_lp/py.typed errors with 'exists on disk, but not in eee15b7' confirming the file was added by M0002
- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy src/ tests/ -> 'Success: no issues found in 91 source files'
- mypy output grep for 'import-untyped|library stubs' produced no lines

### block-hash-check — PASS

_check_bounds_against_file now also validates the min/max block HASH against the manifest row, closing the P04 entry condition for hash-drifted boundary blocks identified as a T033 residual risk. The check is forward-compatible (skipped when the manifest field is None).

Evidence:

- reader.py diff lines 224-246 add a new block after the min/max_block_number check that reads the Parquet column 'block_hash', locates the rows at min_block/max_block via block_numbers.index, formats as '0x' + bytes(hash).hex(), and compares to bounds.min_block_hash / bounds.max_block_hash
- uses getattr(bounds, 'min_block_hash', None) and getattr(bounds, 'max_block_hash', None) so older manifests without hash fields are tolerated (forward-compat per the inline comment)
- raises BoundsMismatchError on mismatch with partition_id, side (min/max), file vs manifest values
- BoundsMismatchError is already exported from robinhood_lp.storage.reader (line 66 / __all__ at 484); no new import required
- Parquet 'block_hash' column is written as 32-byte big-endian by RawPartitionWriter (writer.py:182) and 64-char hex by the manifest writer (writer.py:778-780); reader reconstructs the same 0x+64-char form

### sample-selection-cleanup — PASS

Dead _stable_pick_int removed cleanly, window_count() off-by-2 fix matches the docstring (per_partition*2 + per_event_type*1 + per_failover*2), and all 4 sample dataclasses now surface manifest_checksum in their selection_inputs_dict() to close the T034/T032 cross-stage contract.

Evidence:

- grep -rn '_stable_pick_int' --include='*.py' . returns nothing; the function is removed and has no remaining references
- RequiredSamples.window_count() now returns '2 * len(self.per_partition) + len(self.per_event_type) + 2 * len(self.per_failover)'; the previous body added a literal 'count = 2' up front (off-by-2) which is gone
- docstring updated to state 'The per-run sample does not produce 10-block windows' and matches the new return formula exactly
- 4 dataclasses get a new field 'manifest_checksum: str = ""' at lines 119, 141, 163, 183 (PerRunSample, PerPartitionSample, PerEventTypeSample, PerFailoverSample)
- 4 selection_inputs_dict() methods include 'manifest_checksum' at lines 126, 149, 170, 189
- select_per_run_sample, select_per_partition_sample, select_per_event_type_sample, select_per_failover_sample all accept manifest_checksum and propagate to the dataclass; select_required_samples forwards to each

### tests — PASS

Full test suite (683 tests) passes. The new test covers the hash-drift detection path while preserving the original block-number-only check, including a positive restoration step.

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/ -q --ignore=tests/test_workflow.py --ignore=tests/test_abi_artifacts.py --ignore=tests/test_workflow_contracts.py -> '683 passed, 6 skipped'
- the 6 skips are Foundry / gpg / sorted-currency tests unrelated to M0002 (pre-existing environment skips, not introduced by this diff)
- new test test_check_bounds_against_file_validates_block_hash runs in isolation and passes; it tampers the manifest row's min_block_hash while keeping min_block_number unchanged and asserts BoundsMismatchError is raised with 'min_block_hash' in the message, then restores the correct hash and re-reads successfully
- all pre-existing test_storage_reader.py cases still pass (file went from N to N+1 tests)

### ruff-format — PASS

All 91 source/test files conform to the project formatter.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff format --check src/ tests/ -> '91 files already formatted'

### ruff-check — PASS

Ruff linter reports no issues across src/ and tests/.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff check src/ tests/ -> 'All checks passed!'

### mypy — PASS

mypy is clean across the whole src/ and tests/ tree. The PEP 561 marker silences the prior import-untyped noise without changing pyproject.toml.

Evidence:

- /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy src/ tests/ -> 'Success: no issues found in 91 source files'
- the 5 prior 'import-untyped' warnings on optional deps (httpx, pyarrow, etc.) are gone after the py.typed marker is added

### git-diff-check — PASS

No whitespace or line-ending issues in the diff.

Evidence:

- git diff --check produced no output (no whitespace errors)
- git diff eee15b7 e20c35c --check produced no output

### maintenance-eligibility — PASS

M0002 fits the maintenance eligibility boundary: LOW_RISK_IMPLEMENTATION_DEFECT, only the 4 allowed implementation paths, no public-interface or schema break (manifest_checksum defaults to '' and 4 select_* helpers take it as a defaulted keyword), no dependency change, no forbidden surface touched.

Evidence:

- request.json risk_attestation = 'LOW_RISK_IMPLEMENTATION_DEFECT'
- allowed_paths is a closed set: [py.typed, storage/reader.py, quality/sample_selection.py, tests/test_storage_reader.py]; diff --name-only contains exactly that set plus workflow artifacts
- no docs/intent, docs/spec, .claude, tools/workflow, todo/config.yaml, pyproject.toml, requirements.lock.txt was modified (grep returned no matches)
- new public API surface is nil: py.typed is a marker; the reader change is internal to _check_bounds_against_file and raises the same BoundsMismatchError; sample_selection adds a defaulted dataclass field (manifest_checksum: str = '') and a defaulted keyword-only argument on 4 select_* helpers — both are backward compatible additions, not breaking
- no dependency change (pyproject.toml untouched)
- no risk/safety/execution/signer/Intent/Spec/task-contract/controller change (forbidden paths untouched, no policy edits)

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- Tests require PYTHONPATH=src because robinhood_lp is not pip-installed into the conda env; the developer-001.json notes this and the existing workflow already documents it. No code change recommended — installation mode is out of M0002's scope and would touch pyproject.toml (a forbidden path).
