# T026 independent review

- Base commit: `cbbf44d4994e9ecd628535f1bfebecea729cb923`
- Candidate commit: `374766ed71ad9d41cf6a06758de009faf4a7b461`
- Verdict: **PASS**

## Checks

### t026:dependencies-allowed — PASS

No out-of-scope dependencies, manifests, or workflow-controller files were modified. The implementation is layered per ADR-006 (storage / discovery).

Evidence:

- todo/phases/P02-chain-access-and-discovery/T026.md declares dependencies T022 and T024
- todo/config.yaml lists T022 and T024 under T026.depends_on; both are APPROVED in the surrounding workflow state
- git diff cbbf44d..374766e --stat shows only src/robinhood_lp/config/*, src/robinhood_lp/discovery/*, three new tests, todo/config.yaml, todo/evidence/P02/T026/attempt-001-developer.json; no changes to pyproject.toml, requirements.lock.txt, environment.yml, AGENTS.md, CLAUDE.md, tools/workflow/, .claude/, todo/schemas/, or any spec/ADR/Intent/PROJECT_GOALS file

### t026:phase-entry-conditions — PASS

Phase entry gate and workflow-controller state machine are in the expected AWAITING_REVIEW shape for an independent review.

Evidence:

- T026 contract is in Phase P02 with status OWNER_PLAN_EXTENSION_2026-09-19
- todo/config.yaml shows workflow_state transitioned from READY to AWAITING_REVIEW and status from READY to AWAITING_REVIEW for T026; attempt incremented from 0 to 1; base_commit cbbf44d recorded; candidate_commit unset (waiting for review verdict)
- tools.workflow validate returns {"status": "OK"} when run on the candidate worktree

### t026:deliverable-init-decoder-scanner-registry-metadata-preserved — PASS

All T022 deliverables (Initialize decoder, scanner, derived-ID verification, idempotent registry, defensive token metadata reader, scan coverage) are preserved verbatim. The new entry_paths field is additive and defaults to empty for T022 rows.

Evidence:

- src/robinhood_lp/discovery/initialize_log.py is unchanged from the base commit
- src/robinhood_lp/discovery/registry.py preserves the InitializeScanner, PoolRegistry, PoolRecord, _coerce_int helper and conflict semantics; the only addition is an entry_paths: set[str] field defaulting to empty (preserving identity and idempotency for the T022 general-scan path)
- src/robinhood_lp/discovery/token_metadata.py is unchanged
- tests/test_initialize_scanner.py (22 pre-existing tests) all PASS: test_scanner_stop_resume_overlap_are_identical, test_registry_conflicting_pool_id_fails_closed, test_scanner_counts_conflicts, and other native-currency / dynamic-fee / nonzero-hook / broken-metadata tests are green
- Onboarding path test_t022_general_scan_still_works_with_no_entry_path confirms rows ingested via the T022 scanner path carry an empty entry_paths set (the T022 identity fields are not perturbed)

### t026:deliverable-pool-first-entry-by-pool-key — PASS

Pool-first entry by full V4 PoolKey is implemented with local derivation + on-chain Initialize reconciliation as required.

Evidence:

- src/robinhood_lp/discovery/onboarding.py: add_by_pool_key derives PoolId locally via pool_key.to_pool_id() and queries eth_getLogs with topic[1] pinned to that digest, then verifies decoded.pool_key field-wise equal to the supplied one
- tests/test_pool_onboarding.py: test_add_by_pool_key_derives_pool_id_and_reconciles_with_chain (PASS) covers the success path; test_add_by_pool_key_raises_when_no_matching_initialize_event (PASS) covers the missing-log boundary
- PoolKey.to_pool_id is keccak256(abi.encode(PoolKey)) per src/robinhood_lp/protocol/abi.py + ids.py: pool_key.to_pool_id() returns PoolId(compute_pool_id(self))

### t026:deliverable-pool-first-entry-by-pool-id — PASS

Pool-first entry by PoolId is implemented with a scan keyed on the indexed PoolId topic and an explicit equality re-check before the registry is mutated.

Evidence:

- src/robinhood_lp/discovery/onboarding.py: add_by_pool_id queries eth_getLogs with topic[1] pinned to the supplied PoolId, then for each candidate log pre-decodes and re-checks decoded.pool_id.value == pool_id.value BEFORE the registry is updated; if equality fails or no log decodes, raises PoolIdentityMismatchError
- tests/test_pool_onboarding.py: test_add_by_pool_id_finds_a_matching_initialize_log (PASS), test_add_by_pool_id_raises_when_no_initialize_event_matches (PASS), test_add_by_pool_id_rejects_mismatch_when_chain_returns_other_log (PASS) cover the success path, not-found path, and the must-not 'infer a PoolKey from a PoolId without an on-chain Initialize match' boundary

### t026:deliverable-20-byte-address-rejection — PASS

Any 20-byte address supplied as a pool identity is rejected with a named reason and an auditable AddressRejectionRecord; PoolManager / position-NFT / front-end-link flavours all share the same surface.

Evidence:

- src/robinhood_lp/discovery/onboarding.py: reject_address_as_pool_identity(addr, source, reason=None) records an AddressRejectionRecord(address, source, reason) and bumps stats.address_rejections; default reason references ADR-014
- _check_address_input rejects the PoolManager address before any RPC call; _check_pool_key_addresses walks currency0/currency1/hooks and rejects the PoolManager in any slot
- is_pool_manager_address() is exposed for callers that need to pre-classify
- tests/test_pool_onboarding.py: test_reject_address_records_a_rejection, test_reject_address_with_default_reason, test_reject_address_rejects_empty_source, test_reject_address_rejects_non_address_input, test_is_pool_manager_address_recognises_configured_manager, test_add_by_pool_id_rejects_pool_manager_address, test_add_by_pool_key_rejects_when_pool_manager_matches all PASS; the last two confirm a synchronous rejection (no RPC call) when the PoolManager is supplied via add_by_target_token or in a PoolKey

### t026:deliverable-research-universe-membership — PASS

Research-universe membership is a separate collection keyed by (chain_id, PoolId) with per-member block range and support level, kept disjoint from the single active execution pool. The query surface answers execution / research / both for any pool.

Evidence:

- src/robinhood_lp/config/models.py: new ResearchMemberConfig (chain_id, pool_key, block_range_start, block_range_end, support_level, notes) with model_validator rejecting inverted block range
- src/robinhood_lp/discovery/research_universe.py: ResearchUniverse keyed by PoolId; ResearchMember (chain_id, pool_key, block_range_start, block_range_end, support_level, added_via, notes) with __post_init__ validation; DuplicateResearchMemberError raised on duplicate; PoolRole enum (EXECUTION_CANDIDATE, RESEARCH_MEMBER); PoolMembershipView + build_membership_view query surface
- tests/test_research_universe.py: 38 tests PASS covering construction, add/get/remove, duplicate rejection, set operations (intersection/difference), per-member block range and support level, the EXECUTION_CANDIDATE+RESEARCH_MEMBER both-roles case, and the role-aggregation query surface
- tests/test_research_universe_config.py: 18 tests PASS covering empty research_universe loading, default-empty fallback, member validation (chain reference, duplicates, block range, wrong-order currencies, unknown field), and the key interaction with the active-pool collection

### t026:deliverable-registry-query-surface — PASS

The query surface answers which pools are members, on which entry path they were added, and whether a pool is an execution candidate, a research member, or both.

Evidence:

- entry_path_for(registry, pool_id) -> frozenset[OnboardingPath] returns the stamped entry-path set on a row; empty frozenset when unknown
- PoolRecord.entry_paths: set[str] carries TARGET_TOKEN, POOL_KEY, POOL_ID, or INITIALIZE_SCAN values; stamped only by PoolOnboarder (the registry itself never invents a path)
- PoolMembershipView exposes is_execution_candidate, is_research_member, has_role(role), to_dict() with sorted roles
- tests cover both: test_entry_path_for_unknown_pool_returns_empty_frozenset, test_membership_view_pool_can_be_both_research_and_execution, test_membership_view_to_dict_sorts_roles all PASS

### t026:acceptance-stop-resume-overlap — PASS

Stop, resume and overlapping scans produce identical registries; the new T026 entry surfaces do not perturb T022 idempotency.

Evidence:

- PoolRegistry.add keys on PoolId; identical PoolKey bumps occurrences and last-seen block; conflicting PoolKey raises RegistryConflictError which the scanner records in registry.conflicts (T022 behaviour preserved)
- InitializeScanner.ingest_logs is unchanged; test_scanner_stop_resume_overlap_are_identical in tests/test_initialize_scanner.py PASSES
- PoolOnboarder never re-decodes outside the registry; idempotency for the same pool added by both POOL_KEY and POOL_ID is asserted by test_same_pool_added_by_pool_key_and_pool_id_is_idempotent (PASS) and the entry-path set is the union

### t026:acceptance-native-currency-broken-reverting-oversized-dynamic-fee-nonzero-hook-duplicates-conflicts — PASS

Native currency, dynamic fee, nonzero hook, duplicates and conflicting Initialize data continue to be covered; conflicts still fail closed via the registry conflict path.

Evidence:

- InitializeDecoder and PoolRegistry behavior on native currency, dynamic fee, nonzero hook, duplicates and conflicting Initialize data are unchanged from T022; test_initialize_scanner.py covers these (22 PASS)
- tests/test_pool_onboarding.py adds coverage that two pools sharing a currency but differing in fee, tick spacing, or hooks remain distinct registry rows (test_two_pools_sharing_a_currency_but_different_fee_are_distinct, test_two_pools_with_different_tick_spacing_or_hooks_are_distinct, both PASS)
- test_add_by_pool_key_rejects_mismatch_when_chain_decodes_a_different_pool confirms a contradicting decoded PoolKey raises (PoolIdentityMismatchError, PoolIdentityNotFoundError) and no partial record is registered

### t026:acceptance-idempotent-merge-by-two-paths — PASS

A pool added by full PoolKey and the same pool added by PoolId resolve to one registry entry with identical fields; adding the same pool twice by two paths is idempotent.

Evidence:

- test_same_pool_added_by_pool_key_and_pool_id_is_idempotent (PASS): registry.size == 1, by_key.record is by_id.record, entry_paths set contains both POOL_KEY and POOL_ID, entry_path_for returns frozenset({POOL_KEY, POOL_ID})
- _ingest_with_path always calls registry.add() and only stamps the path after a successful add, so the same pool added by both paths lands on the same row

### t026:acceptance-poolid-not-found-named-reason — PASS

A PoolId that no Initialize event matches stops with a named reason (PoolIdentityNotFoundError); no partial record is written.

Evidence:

- add_by_pool_id raises PoolIdentityNotFoundError with the supplied PoolId hex, chain_id and scanned block range in the message when no log is returned; no partial record is registered (registry.size == 0)
- add_by_pool_key raises PoolIdentityNotFoundError with the derived PoolId hex, chain_id and scanned range when no log is returned
- Tests test_add_by_pool_id_raises_when_no_initialize_event_matches and test_add_by_pool_key_raises_when_no_matching_initialize_event both PASS

### t026:acceptance-20-byte-address-rejection-named — PASS

A 20-byte address input is rejected with a reason; the rejection is synchronous (no RPC call) and the rejection is recorded.

Evidence:

- PoolIdentityRejectionError carries the PoolManager address and the source label; AddressRejectionRecord(address, source, reason) is appended to onboarder.address_rejections
- test_add_by_pool_id_rejects_pool_manager_address (PASS) confirms synchronous rejection: no RPC call was made; address_rejections length is 1; reason text mentions ADR-014

### t026:acceptance-distinct-pools-sharing-currency — PASS

Two pools sharing a currency but differing in fee, tick spacing or hooks remain distinct entries in both the PoolRegistry and the ResearchUniverse.

Evidence:

- test_two_pools_sharing_a_currency_but_different_fee_are_distinct: add_by_target_token discovers two pools with the same currency but different fees; registry.size == 2 (PASS)
- test_two_pools_with_different_tick_spacing_or_hooks_are_distinct: three pools sharing a currency but differing in tick spacing or hooks all become distinct rows (PASS)
- test_two_members_sharing_currency_but_different_fee_or_tick_or_hook_are_distinct: same scenario on the research-universe collection (PASS)

### t026:acceptance-research-not-promoted-to-active — PASS

A research-universe member is never reported as, or promoted by, the active execution pool.

Evidence:

- PoolMembershipView only assigns EXECUTION_CANDIDATE when the supplied execution_candidate_pool_id matches the queried pool_id; a research member is never auto-promoted to the active role
- _check_research_member_does_not_become_active_pool in models.py is explicit that the two collections are disjoint and the structural rule is enforced independently by _check_v1_single_active_pool
- test_root_config_does_not_promote_research_member_to_active_pool (PASS): the configuration carrying both a research member whose PoolKey matches the active pool keeps both collections; len(pools) == 1 and len(research_universe) == 1
- test_membership_view_pool_can_be_both_research_and_execution (PASS): even when a pool is in both collections, the view simply reports both roles — it does not promote or merge

### t026:acceptance-single-active-pool-rule-still-fires — PASS

A configuration that names more than one active pool still fails to load with a redacted error, including when the same configuration carries a populated research universe.

Evidence:

- _check_v1_single_active_pool is preserved unchanged; test_root_config_still_rejects_two_active_pools_even_with_populated_research_universe (PASS) confirms the rule fires with message 'at most one active PoolConfig' even when the same configuration carries a populated research_universe
- test_two_pool_t038.py (78 pre-existing tests including all single-active-pool enforcement) PASS

### t026:boundary-initialize-out-of-range — PASS

A pool whose Initialize lies outside the scanned range is treated as 'not found' and stops with a named reason; the block range is configured at onboarder construction time.

Evidence:

- PoolOnboarder takes block_range_from and block_range_to at construction; the constructor rejects an inverted range with a named ValueError; test_onboarder_rejects_inverted_block_range (PASS) and test_onboarder_rejects_negative_block (PASS)
- When the chain returns no logs in the configured range, add_by_pool_key and add_by_pool_id raise PoolIdentityNotFoundError with the scanned range in the message; no partial record is written

### t026:boundary-two-initialize-events-same-poolid — PASS

Two Initialize events matching one PoolId do not silently overwrite. Conflicting PoolKey is surfaced through the registry's conflict list; identical PoolKey is an idempotent occurrence bump. The T022 conflict fail-closed invariant is preserved.

Evidence:

- On a multi-log match, add_by_pool_key increments stats.pool_key_mismatch and delegates to _ingest_with_path; the registry's add() raises RegistryConflictError on a different PoolKey for the same PoolId (preserved T022 fail-closed semantics) and treats an identical PoolKey as an idempotent duplicate (occurrences++). T022's test_scanner_counts_conflicts (PASS) covers the conflict-counter path.
- The onboarder's _ingest_with_path catches the conflict and continues (the conflict is surfaced through registry.conflicts per T022); the candidate is not silently overwritten.

### t026:boundary-poolkey-currencies-wrong-order — PASS

A PoolKey supplied with its currencies in the wrong order is rejected by the protocol-layer PoolKey constructor; the rejection propagates through both the onboarding surface and the ResearchMemberConfig surface.

Evidence:

- PoolKey.__post_init__ in src/robinhood_lp/protocol/ids.py raises ValueError 'PoolKey.currency0 must be strictly less than PoolKey.currency1 as uint160' on wrong-order input
- test_add_by_pool_key_rejects_currencies_in_wrong_order (PASS): wrong-order PoolKey cannot even be constructed; the test asserts the named ValueError
- test_research_member_config_inherits_pool_key_validation (PASS): ResearchMemberConfig's pool_key is the same pydantic PoolKey model; wrong-order currencies are rejected

### t026:boundary-poolkey-derived-id-disagrees-with-chain — PASS

A PoolKey whose derived PoolId disagrees with the chain is rejected with a named reason (PoolIdentityMismatchError or PoolIdentityNotFoundError) and no partial record is registered.

Evidence:

- add_by_pool_key re-checks field-wise equality between the decoded pool_key and the supplied one; on mismatch it raises PoolIdentityMismatchError with supplied vs decoded summary
- test_add_by_pool_key_rejects_mismatch_when_chain_decodes_a_different_pool (PASS) covers the synthetic case where the topic matches the derived PoolId but the data decodes to a different PoolKey (the decoder rejects internally and the onboarder surfaces PoolIdentityMismatchError / PoolIdentityNotFoundError)

### t026:boundary-empty-research-universe — PASS

An empty research universe is a valid V1 configuration; both the configuration validators and the runtime ResearchUniverse accept it as the empty case.

Evidence:

- ResearchMemberConfig is default_factory=list on RootConfig.research_universe; empty list is the default
- _check_research_universe_chain_consistency and _check_research_universe_no_duplicate_identities short-circuit when the list is empty (early return)
- test_root_config_defaults_research_universe_to_empty and test_root_config_with_empty_research_universe_loads PASS; ResearchUniverse.is_empty is true and is_member returns false for any pool

### t026:boundary-duplicate-research-member — PASS

A member list containing a duplicate is rejected with a named reason at both the runtime and the configuration layers.

Evidence:

- ResearchUniverse.add raises DuplicateResearchMemberError on a PoolId already present; the existing record is preserved (no overwrite)
- _check_research_universe_no_duplicate_identities derives PoolId through the protocol layer and rejects duplicate (chain_id, pool_id) pairs at parse time
- test_add_duplicate_member_raises and test_root_config_rejects_duplicate_research_members both PASS

### t026:must-not-query-factory — PASS

The onboarding layer never queries a factory.

Evidence:

- PoolOnboarder only ever calls eth_getLogs filtered by the PoolManager address; no factory lookup, no token-symbol-based search, no off-chain enumeration service
- _fetch_logs pins the eth_getLogs 'address' field to self._pool_manager_address.to_hex() and uses only event-topic filters
- tests use scripted RPC; no test or source path calls any factory contract

### t026:must-not-discover-by-token-symbol — PASS

Discovery by token symbol is not used.

Evidence:

- The onboarding surface takes addresses / PoolKey / PoolId only. add_by_target_token takes an Address (the currency address), not a symbol
- No symbol-based lookup is performed anywhere in onboarding.py or research_universe.py

### t026:must-not-accept-20-byte-address-as-pool-identity — PASS

No 20-byte address is accepted as a pool identity.

Evidence:

- PoolIdentityRejectionError is raised when the PoolManager address is offered via add_by_target_token or inside any PoolKey field; AddressRejectionRecord is appended; no RPC call is made in the rejection path
- Reject surface is exposed for callers that need to reject any other 20-byte address (position NFT, front-end link) with their own source label

### t026:must-not-infer-poolkey-from-poolid-without-on-chain-match — PASS

A PoolKey is never inferred from a PoolId without an on-chain Initialize match.

Evidence:

- add_by_pool_id raises PoolIdentityNotFoundError when no Initialize log is returned; raises PoolIdentityMismatchError when a log is returned whose decoded.pool_id does not equal the supplied PoolId
- In all rejection branches, registry.add() is never called (the rejection happens before _ingest_with_path)

### t026:must-not-reorder-currencies — PASS

Currencies are never reordered to make a supplied PoolKey fit. The OR-topic filter on currency0/currency1 is the standard V4 wildcard match, not an identity rewrite.

Evidence:

- PoolKey.__post_init__ forbids currency0 >= currency1 as uint160; the onboarding layer does not swap currencies to make a supplied PoolKey fit; the field-wise equality check in _pool_keys_equal compares the original currency0/currency1 slots
- add_by_target_token uses eth_getLogs topics [_, None, [token], [token]] so that topic[2] OR topic[3] matches the supplied token — this is the OR filter needed for V4 PoolKey's canonical ordering, not a reordering of the supplied identity

### t026:must-not-drop-pools-whose-metadata-fails — PASS

The T022 'never drop pools whose metadata call fails' rule is preserved: onboarding does not perform metadata fetches at all.

Evidence:

- The onboarding layer never calls read_token_metadata; metadata fetching is the T022 scanner path's responsibility and is unchanged
- Onboarding just calls registry.add() with the decoded Initialize data; a decode failure or conflict is counted and surfaced, but the pool is not silently dropped
- tests/test_initialize_scanner.py::test_*_metadata_failure tests (covered by T022) PASS

### t026:must-not-merge-research-membership-into-active-pool — PASS

Research membership is never merged into the active execution pool.

Evidence:

- PoolMembershipView only adds EXECUTION_CANDIDATE when the supplied execution_candidate_pool_id matches; build_membership_view does not consult the research universe for the execution role
- RootConfig.research_universe is a separate collection from RootConfig.pools; no validator writes a research member into the pools list
- test_root_config_does_not_promote_research_member_to_active_pool (PASS) confirms the two collections stay distinct even when a member's PoolKey matches the active pool

### t026:must-not-research-entry-grants-execution-authority — PASS

A research-universe entry does not grant, and is not readable as granting, any execution authority.

Evidence:

- ResearchMember holds no 'HOLD/LP/AUTO_SWAP approval' field; no execution-shaped control is reachable from a ResearchMember or a ResearchUniverse
- ADR-014 §1 is referenced in docstrings; the research universe is described as 'holds no assets, produces no transactions and grants no execution authority' in onboarding.py and research_universe.py
- PoolRole.RESEARCH_MEMBER is a read-only role tag; it has no corresponding execution authority in any module

### t026:test-suite-runs — PASS

All obtainable acceptance checks pass. The single failing test is the pre-existing T012 forge-std SelectorOracle blocker (missing tools/oracle/lib v4-core submodules in this worktree); it is documented in todo/evidence/P02/T026/attempt-001-developer.json as a residual risk and is reproducible on the base commit. It is unrelated to T026 surfaces.

Evidence:

- pytest tests/test_pool_onboarding.py tests/test_research_universe.py tests/test_research_universe_config.py -q -> 83 passed in 0.35s
- pytest -q -> 1238 passed, 6 skipped, 1 failed (tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output — pre-existing T012 forge blocker; failure is the Forge build of v4-core TickMath.sol/SqrtPriceMath.sol/etc. missing because tools/oracle/lib is not present in this worktree; identical to base commit cbbf44d and unrelated to T026)
- tests/test_initialize_scanner.py (T022 preservation): 22 PASS
- tests/test_two_pool_t038.py + tests/test_config.py: 122 PASS
- ruff check src tests: All checks passed
- ruff format --check src tests: 141 files already formatted
- mypy src: Success: no issues found in 81 source files
- mypy src tests/test_pool_onboarding.py tests/test_research_universe.py tests/test_research_universe_config.py: Success: no issues found in 84 source files
- tools.workflow validate: {"status": "OK"}

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- PRE-EXISTING OUT-OF-SCOPE: tests/test_abi_artifacts.py::test_artifact_byte_matches_regenerated_oracle_output fails in this worktree because the T012-era forge-std SelectorOracle blocker (tools/oracle/lib v4-core submodules missing) is unaddressed; reproducible on base commit cbbf44d; no T026 surface is touched. T026 evidence file already records this.
- STYLE / DEAD CODE: src/robinhood_lp/discovery/onboarding.py ends with a private _NullCurrencyProbe dataclass that is unused outside the file. mypy and ruff both pass; this is a minor code-smell, not a contract violation. A future PR could remove it.
- SUBTLE BEHAVIOR: In add_by_pool_key, when two Initialize logs match the same PoolId (boundary case), the code increments stats.pool_key_mismatch and continues rather than raising PoolIdentityMismatchError immediately. The conflict is still surfaced through registry.conflicts (T022 preserved behaviour) and the registry's add() raises RegistryConflictError on differing PoolKey, so the 'fails closed' invariant holds. This is not a contract violation but a downstream caller relying on PoolIdentityMismatchError for multi-log matches would not see it; the docstring notes the path.
