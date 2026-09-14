# T001 independent review

- Base commit: `7fbdf204c8d333ea0c0b0c8dc12d547751c0a884`
- Candidate commit: `33d28ab885657b9c1845d0699fa10d5b642cb388`
- Verdict: **FAIL**

## Checks

### diff_minimal_and_scoped — PASS

T001 diff is minimal, scoped, and contains no unrelated edits or secrets.

Evidence:

- git diff --stat between 7fbdf20 and 33d28ab shows README.md, requirements.lock.txt, tests/test_lockfile.py, todo/config.yaml, todo/evidence/P00/T001/attempt-001-developer.json only.

### no_secrets_in_diff — PASS

No private keys, API secrets, tokens, or env reads in the candidate diff; .gitignore continues to block .env/.key/.pem.

Evidence:

- Searched candidate diff for private keys, API secrets, tokens, passwords. None present.

### deliverable_pyproject_toml — PASS

pyproject.toml is unchanged and declares requires-python='>=3.12' with Python 3.12/3.13 classifiers.

Evidence:

- pyproject.toml declares requires-python = '>=3.12' and classifiers Programming Language :: Python :: 3.12 and 3.13.

### deliverable_locked_dependencies — PASS

requirements.lock.txt is pip-compile-generated with --generate-hashes; 27 packages, 586 SHA-256 entries cover every direct runtime and dev dep.

Evidence:

- Header documents the regeneration recipe. pip install --require-hashes --dry-run resolves cleanly.

### deliverable_src_package — PASS

src/robinhood_lp/ subpackages config, discovery, protocol, rpc, storage exist with __init__.py and __main__.py.

Evidence:

- Directory listing shows the expected skeleton.

### deliverable_tests_dir — PASS

tests/ contains the existing suite plus the new tests/test_lockfile.py.

Evidence:

- Directory listing.

### deliverable_quality_commands_defined — PASS

pytest, ruff check, ruff format --check, mypy src tests are all wired via pyproject.toml.

Evidence:

- pyproject.toml [tool.pytest.ini_options] and [tool.mypy] strict=true, python_version='3.12'.

### deliverable_smoke_test — PASS

tests/test_smoke.py contains three passing CLI/import smoke tests.

Evidence:

- test_package_version_is_string, test_python_dash_m_version_exits_zero, test_python_dash_m_help_exits_zero all green.

### deliverable_lockfile_tests — PASS

tests/test_lockfile.py adds five passing shape/consistency tests for the lockfile.

Evidence:

- test_lockfile_exists_at_repo_root, _is_parseable_and_consistent, _covers_every_direct_runtime_dependency, _covers_every_direct_dev_dependency, _pins_eth_hash_pycryptodome_backend all pass.

### acceptance_clean_install_evidence — FAIL

The candidate did not perform a real clean-room 'pip install --require-hashes -r requirements.lock.txt'; only a pip --dry-run was executed.

Evidence:

- Developer handoff explicitly states a clean-room byte-identical install was not re-exercised.
- Legacy evidence file todo/evidence/legacy/2026-09-14/T001-environment-delivery.md records the same gap.

### acceptance_quality_commands_pass_twice — FAIL

Both pytest runs report 1 failed (tests/test_workflow_contracts.py::test_repository_workflow_configuration_is_valid hard-codes T001.status=='READY'), 284 passed, 2 skipped. ruff check, ruff format --check, mypy src tests all pass both runs.

Evidence:

- Run 1: 1 failed, 284 passed, 2 skipped in 1.07s.
- Run 2: 1 failed, 284 passed, 2 skipped in 1.09s. Identical summary, no flakes.
- The failing test asserts config['tasks']['T001']['status']=='READY' but the controller has correctly moved T001 to AWAITING_REVIEW.
- ruff check: All checks passed.
- ruff format --check: 147 files already formatted.
- mypy src tests: Success: no issues found in 39 source files.

### acceptance_python_bounds — PASS

pyproject.toml declares requires-python='>=3.12' and Python 3.12/3.13 classifiers; host Python 3.12.14 runs all quality commands.

Evidence:

- pyproject.toml metadata.
- Host python is 3.12.14 at /home/lpdev/miniconda3/envs/robinhood-lp/bin/python.

### must_not_blockchain_storage_dataframe_deps — PASS

Runtime deps remain pydantic, eth-hash[pycryptodome], httpx; no web3, eth-account, pyarrow, pandas, numpy, polars added.

Evidence:

- pyproject.toml [project] dependencies section.

### must_not_expose_environment_contents — PASS

No diagnostic code added by the candidate logs environment variables or host metadata.

Evidence:

- test_lockfile.py only reads the lockfile from disk.
- Existing tests/test_no_signing_paths.py and tests/test_config.py::test_loader_rejects_credential_url_in_config remain green.

### dependency_T000_approved — PASS

T000 status is APPROVED with approved_commit 6c3177883a676a06f8b571d0a598f524e319fa34; T001.depends_on is exactly [T000].

Evidence:

- todo/config.yaml T000 entry.

## Must-not violations

- None.

## Unknowns

- Whether a fresh 'pip install --require-hashes -r requirements.lock.txt' against an empty pip cache or new venv would succeed without hash errors. pip --dry-run --require-hashes resolves cleanly in the host environment but a true clean-room install was not exercised.

## Required changes

- Triage the conflict between tests/test_workflow_contracts.py::test_repository_workflow_configuration_is_valid and the workflow state machine (READY -> IN_DEVELOPMENT -> AWAITING_REVIEW). Until the assertion is repaired or the test is restructured, pytest cannot satisfy 'all quality commands pass twice' for any task once its active review state has been entered. This is an orthogonal planning/contract concern; the developer role cannot resolve it from inside T001 scope.
- Re-run 'pip install --require-hashes -r requirements.lock.txt' in a clean environment (clear pip cache or new venv) and capture the command, the resolved wheel set, and a hash-verification log under todo/evidence/P00/T001/. The current attempt only performed a pip --dry-run.

## Residual risks

- None.
