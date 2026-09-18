# T038 independent review

- Base commit: `cdae0239617450634a6f3fcd6c34a25d1daa28f6`
- Candidate commit: `4d60384a4321d7a19fa683c1f7886e563a6ee732`
- Verdict: **PASS**

## Checks

### diff_minimal_and_scoped — PASS

Candidate commit 4d60384 adds exactly the T038 qualification surfaces (second_pool, two_pool, two_pool_window, two_pool_failure_paths, two_pool_runbook, __init__ re-exports) plus the test module and the protocol-doc clarification; the controller-managed workflow state is updated; no Parquet byte, no protected task contract outside T038.md, and no Phase 0-2 surface was modified.

Evidence:

- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/second_pool.py (+744 NEW)
- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/two_pool_window.py (+672 NEW)
- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/two_pool.py (+506 NEW)
- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/two_pool_failure_paths.py (+193 NEW)
- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/two_pool_runbook.py (+359 NEW)
- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/__init__.py (+167/-x re-exports)
- /home/lpdev/lp-worktrees/review-t038-attempt-002/tests/test_two_pool_t038.py (+1630 NEW)
- /home/lpdev/lp-worktrees/review-t038-attempt-002/docs/spec/protocol/PROVIDER_FACTS.md (+12/-5 spec clarification for the hook-address semantics)
- /home/lpdev/lp-worktrees/review-t038-attempt-002/todo/phases/P03-ingestion-and-storage/T038.md (amended contract to match the Owner hook-address decision)
- /home/lpdev/lp-worktrees/review-t038-attempt-002/todo/config.yaml (controller-managed: status AWAITING_REVIEW, attempt=2, base_commit pinned)
- No Parquet file was modified: git diff cdae023..4d60384 -- '*.parquet' returned empty output.
- No edits to T030, T031, T032, T033, T034, T035, T036, T037 or other protected Phase 0-3 contracts: git diff --name-only returned only the files listed above.
- The 2026-09-18 reference dataset and its manifest are untouched (no data/ or implement/ paths in the diff).

### amendment_honored_hook_scan_not_poolid_scan — PASS

The hook-scan semantics the Owner amendment requires are honored end-to-end: the contract, the spec, the resolver, and the tests all converge on Initialize-log filtering by hooks address and the keccak256(abi.encode(PoolKey)) == PoolId re-derivation check.

Evidence:

- /home/lpdev/lp-worktrees/review-t038-attempt-002/todo/phases/P03-ingestion-and-storage/T038.md: contract amendment re-frames the Owner-pinned 20-byte value 0xEd50bDeeA8aDC232f159486192a4157281D722ff as a hook contract address lookup signal (NOT a PoolId) and requires the resolver to scan Initialize logs whose decoded hooks field equals that address.
- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/second_pool.py:478-604 resolve_second_pool_via_hook_scan filters logs by 'log.pool_key.hooks.value == pinned_hook_int' (uint160 equality) and verifies keccak256(abi.encode(PoolKey)) == pool_id through ResolvedPoolKey and resolve_second_pool_identity.
- /home/lpdev/lp-worktrees/review-t038-attempt-002/docs/spec/protocol/PROVIDER_FACTS.md: the 'Finalized pinning' section now explicitly identifies the pinned 20-byte value as a hook contract address lookup signal and clarifies the 32-byte PoolId is the chain-emitted keccak256 digest.
- /home/lpdev/lp-worktrees/review-t038-attempt-002/tests/test_two_pool_t038.py:668-879: test_resolve_second_pool_via_hook_scan_matches_pinned_address, _ignores_non_matching_logs, _reports_pool_init_outside_window, _reports_pool_init_outside_window_empty, _ambiguous_matches_halt, _defaults_to_pinned_hook_address, _accepts_str_hook_address, _result_propagates_block_number, _propagates_to_window_plan, _does_not_trigger_defensive_guard all pass.

### new_resolve_second_pool_via_hook_scan_function — PASS

The new hook-scan resolver is the primary entry point for the second-pool identity: it defaults the pinned hook address to SECOND_POOL_HOOK_ADDRESS_HEX, filters by hooks field uint160 equality, halts closed on ambiguity, records search bounds on miss, and forwards to the identity-check pipeline.

Evidence:

- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/second_pool.py:478 defines resolve_second_pool_via_hook_scan(*, pinned_hook_address, initialize_logs, chain_id, search_bounds, error_detail).
- Filters Initialize logs by 'log.pool_key.hooks.value == pinned_hook_int' (line 545).
- Returns RESOLVE_HOOK_ADDRESS_AMBIGUOUS when more than one log matches (line 552), RESOLVE_WINDOW_UNRESOLVED when none match (line 570), and a ResolvedPoolKey through resolve_second_pool_identity when exactly one matches (line 599).
- Exported from __init__.py (line 280) and listed in __all__.

### RESOLVE_HOOK_ADDRESS_AMBIGUOUS_outcome_constant — PASS

The new outcome constant RESOLVE_HOOK_ADDRESS_AMBIGUOUS is defined, whitelisted, exported, and exercised by the dedicated ambiguity test.

Evidence:

- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/second_pool.py:78 defines RESOLVE_HOOK_ADDRESS_AMBIGUOUS = 'resolve_hook_address_ambiguous'.
- Whitelisted in SecondPoolResolveResult.__post_init__ (line 230).
- Exported from __init__.py (line 225).
- Covered by tests/test_two_pool_t038.py:779 test_resolve_second_pool_via_hook_scan_ambiguous_matches_halt.

### RESOLVE_PINNED_POOL_ID_SIZE_DEFECT_preserved_as_defensive_guard — PASS

RESOLVE_PINNED_POOL_ID_SIZE_DEFECT is preserved as a defensive guard with rewritten text reflecting the 2026-09-18 amendment: the audit trail surfaces the configuration error (feeding the hook address as a PoolId) instead of silently producing a wrong answer.

Evidence:

- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/second_pool.py:87 defines RESOLVE_PINNED_POOL_ID_SIZE_DEFECT with new docstring naming the 2026-09-18 amendment semantics.
- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/second_pool.py:401-419 the defensive guard fires when a caller explicitly passes a 20-byte pinned_pool_id_hex; the error_detail names 'hook contract address', 'lookup signal', '20 bytes', '32 bytes', and 'resolve_second_pool_via_hook_scan'.
- build_second_pool_resolve_result_from_resolved_fields (lines 643-658) applies the same 20-byte defensive guard.
- Covered by tests/test_two_pool_t038.py:536 test_owner_pinned_20_byte_value_surfaces_as_hook_address_defensive_guard (renamed from the attempt-1 contract-defect test) and tests at line 996.

### Sequence_is_collections_abc — PASS

Sequence is imported from collections.abc as required; mypy --strict accepts it.

Evidence:

- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/second_pool.py:39 'from collections.abc import Sequence'.
- Used at line 481 for the initialize_logs parameter typing.
- mypy --strict src/ exits 0 over 69 source files.

### docstrings_reflect_chain_emitted_32_byte_poolid — PASS

All required docstrings have been updated to reflect the chain-emitted 32-byte PoolId semantics and the hook-address lookup-signal framing.

Evidence:

- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/second_pool.py: module docstring (lines 1-35) describes the 20-byte hook-address lookup signal and the 32-byte chain-emitted PoolId keccak256 digest.
- ResolvedPoolKey docstring (lines 92-107) names 'keccak256(abi.encode(pool_key))' and the chain-emitted 32-byte PoolId.
- SecondPoolResolveResult docstring (lines 178-215) enumerates the chain-emitted semantics for RESOLVE_OK.
- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/two_pool_window.py: module header (lines 32-39) and SECOND_POOL_HOOK_ADDRESS_HEX docstring (lines 51-60) state the value is a hook contract address lookup signal, not a V4 PoolId.

### second_pool_window_constant_renamed_with_back_compat_alias — PASS

SECOND_POOL_HOOK_ADDRESS_HEX is the new canonical name; SECOND_POOL_POOL_ID_HEX is preserved as a back-compat alias; the module header, docstrings, and exports are all consistent.

Evidence:

- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/two_pool_window.py:61 SECOND_POOL_HOOK_ADDRESS_HEX = '0xEd50bDeeA8aDC232f159486192a4157281D722ff'.
- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/two_pool_window.py:69 SECOND_POOL_POOL_ID_HEX = SECOND_POOL_HOOK_ADDRESS_HEX (back-compat alias).
- Both names exported in __all__ (lines 656-657) and from __init__.py (lines 236-237).
- TwoPoolCandidate docstring (lines 296-318) and second_pool_candidate docstring (lines 608-619) describe the hook-address semantics.
- Module header (lines 32-39) reflects the hook-address lookup-signal framing.
- tests/test_two_pool_t038.py:164 imports SECOND_POOL_POOL_ID_HEX for the back-compat assertion.

### qualification_init_re_exports — PASS

The new resolver function, the new outcome constant, and SECOND_POOL_HOOK_ADDRESS_HEX are all exported from robinhood_lp.qualification.

Evidence:

- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/__init__.py:101-119 imports resolve_second_pool_via_hook_scan, RESOLVE_HOOK_ADDRESS_AMBIGUOUS, RESOLVE_PINNED_POOL_ID_SIZE_DEFECT, and the rest of the resolver surface.
- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/__init__.py:158-178 imports SECOND_POOL_HOOK_ADDRESS_HEX and SECOND_POOL_POOL_ID_HEX from two_pool_window.
- /home/lpdev/lp-worktrees/review-t038-attempt-002/src/robinhood_lp/qualification/__init__.py: __all__ lists the new resolver function (line 280), the new outcome (line 225), and the new constant (line 236).

### tests_cover_required_scenarios — PASS

All required test scenarios are present: hook-scan happy path, non-matching logs ignored, pool_init_outside_window (empty + non-matching), ambiguous matches, default hook address, str-form hook address, init-block propagation, end-to-end window-plan flow; the renamed defensive-guard test exists; and the decode-error tests use a 64-byte placeholder pool_id_hex.

Evidence:

- Hook-scan happy path: tests/test_two_pool_t038.py:668 test_resolve_second_pool_via_hook_scan_matches_pinned_address.
- Non-matching logs ignored: tests/test_two_pool_t038.py:705 test_resolve_second_pool_via_hook_scan_ignores_non_matching_logs.
- pool_init_outside_window (non-matching): tests/test_two_pool_t038.py:729 test_resolve_second_pool_via_hook_scan_reports_pool_init_outside_window.
- pool_init_outside_window (empty): tests/test_two_pool_t038.py:763 test_resolve_second_pool_via_hook_scan_reports_pool_init_outside_window_empty.
- Ambiguous matches: tests/test_two_pool_t038.py:779 test_resolve_second_pool_via_hook_scan_ambiguous_matches_halt.
- Default hook address: tests/test_two_pool_t038.py:807 test_resolve_second_pool_via_hook_scan_defaults_to_pinned_hook_address.
- str-form hook address: tests/test_two_pool_t038.py:822 test_resolve_second_pool_via_hook_scan_accepts_str_hook_address.
- Init-block propagation: tests/test_two_pool_t038.py:838 test_resolve_second_pool_via_hook_scan_result_propagates_block_number.
- End-to-end window-plan flow: tests/test_two_pool_t038.py:849 test_resolve_second_pool_via_hook_scan_propagates_to_window_plan and tests/test_two_pool_t038.py:1585 test_end_to_end_two_pool_report_with_real_pin_and_resolved_keys.
- Renamed defensive-guard test: tests/test_two_pool_t038.py:536 test_owner_pinned_20_byte_value_surfaces_as_hook_address_defensive_guard.
- Decode-error 64-byte placeholder: tests/test_two_pool_t038.py:1016 test_build_second_pool_resolve_result_decode_error and line 1042 test_build_second_pool_resolve_result_currency_ordering_violation both use placeholder_pool_id = '0x' + '11' * 32 (64 hex chars).

### acceptance_pytest_two_pool_t038 — PASS

The T038 test module reports 63 passed, matching the expected count.

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/test_two_pool_t038.py -q: '63 passed in 0.21s'.

### acceptance_pytest_full_suite — PASS

Full suite reports 922 passed and 6 skipped, matching the expected outcome. No new failures or skips introduced.

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m pytest tests/ --ignore=tests/test_abi_artifacts.py -q: '922 passed, 6 skipped in 15.37s'.
- Skipped tests are pre-existing Foundry/gpg/sorted-currency skips unrelated to T038.

### acceptance_ruff_format — PASS

ruff format --check reports all files already formatted.

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff format --check src/ tests/: '118 files already formatted'.

### acceptance_ruff_check — PASS

ruff check reports no lint failures.

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff check src/ tests/: 'All checks passed!'.

### acceptance_mypy_strict — PASS

mypy --strict reports no type issues across all 69 source files.

Evidence:

- PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy --strict src/: 'Success: no issues found in 69 source files'.

### must_not_parquet_bytes — PASS

No Parquet byte was modified by the candidate commit.

Evidence:

- git diff cdae023..4d60384 -- '*.parquet' returns empty output.
- git diff cdae023..4d60384 --name-only does not list any *.parquet, docs/implement/, or data/ path.

### must_not_protected_files_T030_T037 — PASS

No Phase 0-3 task contract outside T038.md was modified.

Evidence:

- git diff cdae023..4d60384 --name-only list does not contain T030.md, T031.md, T032.md, T033.md, T034.md, T035.md, T036.md, or T037.md.
- Only todo/phases/P03-ingestion-and-storage/T038.md was modified (the amendment target).

### must_not_reference_dataset_2026_09_18 — PASS

The 2026-09-18 reference dataset and its manifest are untouched.

Evidence:

- git diff cdae023..4d60384 -- 'docs/implement/' is empty.
- No data/ or lp-data/ paths appear in the diff.
- The .workflow/t037-lp-data-reconciliation.json snapshot from the T037 review is preserved unchanged in the review worktree.

### no_secrets_in_diff — PASS

No credentials or sensitive material are introduced.

Evidence:

- The candidate commit adds only Python source, tests, and protocol-doc clarifications; no private key, seed phrase, API key, bearer token, or credential value is present.
- The new modules are pure value-object / pipeline code with no I/O, no environment reads, no logging, no network.

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- The second-pool identity resolver is implemented against a synthetic hook-match path; the on-chain Initialize scan that produces a real DecodedInitialize carrying the pinned hook address remains an operator-side run-time action to be exercised when the live endpoints permit it. The deterministic, offline contract is fully covered by tests.
- src/robinhood_lp/qualification/__init__.py contains a duplicated 'SUPPORT_LEVEL_INGESTION' entry in __all__ (lines 217 and 222 of the diff); this is a harmless duplicate because __all__ deduplicates by membership semantics, but a future cleanup pass could remove one entry.
- The editable install at /home/lpdev/miniconda3/envs/robinhood-lp/lib/python3.12/site-packages/_editable_impl_robinhood_lp.pth points to /home/lpdev/lp/src (the main checkout), not the worktree. Tests were run with PYTHONPATH=src (the worktree's src) per the project convention.
- Attempt-1 review trails and attempt-001 evidence files remain on disk in todo/evidence/P03/T038/ for diagnostic continuity; only attempt-002 was evaluated here.
