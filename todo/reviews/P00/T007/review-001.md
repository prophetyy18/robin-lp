# T007 independent review

- Base commit: `6180817ca49079b0c4b229487d3b39ca8ff549b7`
- Candidate commit: `32e7008f15a607e46c132c66a00a37788caf6147`
- Verdict: **PASS**

## Checks

### strategy_baselines_no_backtest_import — PASS

strategy/baselines.py no longer imports robinhood_lp.backtest.engine or robinhood_lp.backtest.events.

Evidence:

- src/robinhood_lp/strategy/baselines.py lines 102-109 import from robinhood_lp.protocol.contracts; the prior imports from robinhood_lp.backtest.engine (line 102 at base) and robinhood_lp.backtest.events (line 106 at base) are removed.
- grep -rn '^from robinhood_lp.backtest\|^import robinhood_lp.backtest' src/robinhood_lp/strategy/ returned no output.

### shared_strategy_backtest_contracts_moved — PASS

Strategy-callback contracts and input-event primitives live in robinhood_lp.protocol.contracts; the engine and events modules re-export them so existing callers see the same frozen dataclass instances and the same constants.

Evidence:

- src/robinhood_lp/protocol/contracts.py declares StrategyDecision and StrategyDecisionRequest, plus BacktestEvent, KIND_SWAP, KIND_OBSERVATION, SOURCE_PRIORITY_DATA, BacktestEventError, BACKTEST_EVENT_VERSION and the full closed vocabulary of source-priority and kind constants.
- Identity check (PYTHONPATH=src python -c): robinhood_lp.backtest.engine.StrategyDecision is robinhood_lp.protocol.contracts.StrategyDecision -> True; StrategyDecisionRequest likewise True; robinhood_lp.backtest.events.BacktestEvent is robinhood_lp.protocol.contracts.BacktestEvent -> True; BacktestEventError likewise True.
- src/robinhood_lp/backtest/engine.py re-exports the two dataclasses via `from robinhood_lp.protocol.contracts import StrategyDecision, StrategyDecisionRequest`. src/robinhood_lp/backtest/events.py re-exports all of BacktestEvent, BacktestEventError, BACKTEST_EVENT_VERSION, KIND_BURN/KIND_MINT/KIND_OBSERVATION/KIND_SHUTDOWN/KIND_SWAP/KIND_TICK, SOURCE_PRIORITY_DATA/EXECUTION/RISK/STRATEGY/SYSTEM. _is_backtest_events_module is widened to also pass robinhood_lp.protocol.contracts so the events-module purity check still accepts the new dependency.

### replay_no_storage_import — PASS

replay/replayer.py and replay/ticks.py no longer import robinhood_lp.storage.schema.

Evidence:

- src/robinhood_lp/replay/replayer.py lines 64-70 import from robinhood_lp.protocol.records; the prior robinhood_lp.storage.schema import (line 83 at base) is removed.
- src/robinhood_lp/replay/ticks.py lines 91-96 import from robinhood_lp.protocol.records; the prior robinhood_lp.storage.schema import (line 87 at base) is removed.
- grep -rn '^from robinhood_lp.storage\|^from robinhood_lp.rpc' src/robinhood_lp/replay/ returned no output.

### storage_records_subclass_protocol — PASS

Storage subclasses extend the protocol base classes and produce the same records for the same raw input; protocol fields and ordering are preserved.

Evidence:

- src/robinhood_lp/storage/schema.py imports the five record classes from robinhood_lp.protocol.records as _ProtocolInitializeLogRecord, _ProtocolModifyLiquidityLogRecord, _ProtocolSwapLogRecord, _ProtocolDonateLogRecord, _ProtocolProtocolFeeUpdatedLogRecord.
- Storage-side InitializeLogRecord, ModifyLiquidityLogRecord, SwapLogRecord, DonateLogRecord, ProtocolFeeUpdatedLogRecord each extend their protocol counterpart (`class InitializeLogRecord(_ProtocolInitializeLogRecord)`).
- Identity check: issubclass(StorageInit, ProtoInit) -> True for all five; storage record is isinstance of protocol record -> True; protocol fields present and equal between the two forms (event_key()/sort_key() match); storage-only fields are acquisition/decode_version/ingestion_time/raw/raw_data/raw_topics/schema_version/source_endpoint/unknown_fields, all defaulted.

### hook_pack_no_config_import — PASS

hook_pack.py no longer imports robinhood_lp.config.models; the canonical flag constants come from robinhood_lp.protocol.ids; config.models re-exports them for back-compat without a circular dependency (config.models -> protocol.ids; protocol.ids does not import config.models).

Evidence:

- src/robinhood_lp/qualification/hook_pack.py lines 73-78 import ALL_HOOK_MASK, DELTA_TO_ACTION_FLAG, DYNAMIC_FEE_FLAG, HOOK_FLAG_BITS from robinhood_lp.protocol.ids; the prior `from robinhood_lp.config.models import (...)` is removed.
- grep -rn '^from robinhood_lp.config\|^import robinhood_lp.config' src/robinhood_lp/ | grep -v '^src/robinhood_lp/config/' returned no output.
- Identity check: robinhood_lp.config.models.MAX_LP_FEE is robinhood_lp.protocol.ids.MAX_LP_FEE -> True; likewise True for DYNAMIC_FEE_FLAG, ALL_HOOK_MASK, HOOK_FLAG_BITS, DELTA_TO_ACTION_FLAG.

### new_modules_classified_by_layer_map — PASS

All new modules under src/robinhood_lp/protocol/ are classified by the T006 layer map's package default; no new layer, package default or per-module override was added.

Evidence:

- tools/check_imports/layer_map.py declares the package default `robinhood_lp.protocol` -> `protocol/domain`; robinhood_lp.protocol.contracts and robinhood_lp.protocol.records inherit that default.
- PYTHONPATH=src python -m tools.check_imports check -> 'import-graph check passed: no findings'.

### regression_test_fails_on_reintroduction — PASS

The regression test fails when any of the three edges is reintroduced and passes again after the file is restored.

Evidence:

- tests/test_layer_direction_t007.py is AST-based (uses ast.parse + ast.walk on file bodies only; never imports or executes production code). It defines test_strategy_module_does_not_import_backtest, test_replay_module_does_not_import_storage_or_rpc, and test_qualification_hook_pack_does_not_import_config matching the three edges.
- Perturbation probe 1 (sed-replace `from robinhood_lp.protocol.contracts` -> `from robinhood_lp.backtest.engine` in strategy/baselines.py): pytest tests/test_layer_direction_t007.py::test_strategy_module_does_not_import_backtest -> FAILED with 'robinhood_lp.strategy.baselines:102 -> robinhood_lp.backtest.engine'. File restored after probe.
- Perturbation probe 2 (sed-replace in replay/replayer.py to import from robinhood_lp.storage.schema): pytest tests/test_layer_direction_t007.py::test_replay_module_does_not_import_storage_or_rpc -> FAILED with 'robinhood_lp.replay.replayer:64 -> robinhood_lp.storage.schema'. File restored after probe.
- Perturbation probe 3 (sed-replace in qualification/hook_pack.py to import from robinhood_lp.config.models): pytest tests/test_layer_direction_t007.py::test_qualification_hook_pack_does_not_import_config -> FAILED with 'hook_pack.py:73 -> robinhood_lp.config.models'. File restored after probe.
- After all three restorations, git status --short is clean (no worktree modifications remain).

### authoring_guide_and_example_strategy_state — PASS

AUTHORING_GUIDE.md and example_strategy.py do not exist at base; no stale citations of moved symbols in those files need to be updated. No citation in docs/spec/ or task contracts was edited; the only docs/implement/ edits are DOCUMENTATION_INTEGRITY_AUDIT.md (a §8.1 row describing the three sites as repaired, implementation doc) and docs/implement/ci/suppressions.toml (deleting the hook_pack->config.models entry). docs/spec/ and todo/phases/P00-engineering-baseline/T007.md are unchanged.

Evidence:

- ls docs/implement/strategy/ shows only V4_POSITION_SIZING.md; AUTHORING_GUIDE.md and example_strategy.py do not exist at base or candidate.
- git show 6180817 --stat -- docs/implement/strategy/ returned no output (the directory had no tracked files at base for these names).

### suppression_registry_3_site_entries_deleted — PASS

The hook_pack -> config.models layer-direction entry is deleted; the Owner-deferred entries for unclassified packages (T012 math names, T083 ops.dossier, T084 web) are left untouched; the layer check passes with the three-site entries absent.

Evidence:

- Diff of docs/implement/ci/suppressions.toml between base and candidate shows the [[suppression]] entry whose rule=layer-direction / location=src/robinhood_lp/qualification/hook_pack.py / token=robinhood_lp.qualification.hook_pack -> robinhood_lp.config.models is removed. The four Owner-deferred entries (two for T012 protocol-math names, two for T083/T084 module names) remain.
- PYTHONPATH=src python -m tools.check_imports check returned 'import-graph check passed: no findings' with those three entries absent.

### acceptance_test_suites_pass_unchanged — PASS

The five required test suites pass unchanged; the additional backtest / storage / reader suites (which exercise the engine / replay / storage-side classes) also pass.

Evidence:

- pytest tests/test_layer_direction_t007.py tests/test_replay_t040.py tests/test_tick_liquidity_t041.py tests/test_hook_pack_t043.py tests/test_strategy_t062.py -> '206 passed' (13/45/60/73/35 across the five suites).
- tests/test_backtest_t061.py tests/test_storage_schema.py tests/test_storage_reader.py -> '113 passed'. Combined: 319 passed.

### behavioral_equivalence_records — PASS

Recorded outputs of the engine, replay and hook pack are byte-identical before and after; the change is a move of contracts, not a behaviour change.

Evidence:

- Identity check confirms strategy-callback dataclasses and event constants re-exported from backtest.engine and backtest.events are the same Python objects as the canonical definitions in robinhood_lp.protocol.contracts (is True for both).
- Identity check confirms config.models hook-flag constants are the same Python objects as the canonical definitions in robinhood_lp.protocol.ids (is True for all five).
- Field-set check: storage subclasses (InitializeLogRecord etc.) include every protocol field plus only storage-side envelope fields (acquisition/decode_version/ingestion_time/raw/raw_data/raw_topics/schema_version/source_endpoint/unknown_fields); a record built with identical protocol arguments has identical event_key() and sort_key() output across the protocol and storage forms.
- BacktestEvent payload-normalisation: two equivalent payloads with different insertion order hash to the same event_id (verified end-to-end).
- All 113 storage/backtest/replay behavioural tests pass against the moved contracts.

### lint_format_mypy — PASS

ruff check, ruff format --check, and mypy src+tests (strict on changed files) all pass.

Evidence:

- python -m ruff check -> 'All checks passed!'
- python -m ruff format --check -> '406 files already formatted'
- mypy --strict on the 12 changed src/test files -> 'Success: no issues found in 12 source files'.

### must_not_constraints — PASS

No must-not violation: tier/layer/§2.1/§2.2 unchanged; no exception renewal; no spec/task/workflow/config edits beyond the routine status transition; no behaviour / interface / numeric / reason-code / error-class change; no module / import / §2.2 row deleted; no third-party dependency or network read added; no skipped findings.

Evidence:

- ARCHITECTURE.md §2.1 and §2.2 unchanged (git diff --stat shows no entry for docs/spec/architecture/ARCHITECTURE.md).
- suppressions.toml change is the deletion of the three-site entry only; no Owner-deferred entry was modified.
- git diff --name-only shows no edits under docs/spec/, tools/workflow/, .claude/, todo/schemas/, todo/phases/. The only todo/ edits are todo/config.yaml (workflow_state READY -> AWAITING_REVIEW; task T007 status READY -> AWAITING_REVIEW; attempt 0 -> 1; base_commit null -> base SHA; routine controller transitions) and todo/evidence/P00/T007/attempt-001-developer.json (the developer evidence file).
- No new third-party dependency was added (pyproject.toml / requirements*.txt are not in the diff). No network or new fixtures added.
- No §2.2 row or module is deleted (the protocol package, backtest package, replay package, qualification/hook_pack, storage/schema, config/models and all their public symbols remain).

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- None.
