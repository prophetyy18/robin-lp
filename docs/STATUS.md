# Project Status — 2026-09-13

> Snapshot of the current Robinhood V4 framework codebase. Read this
> before starting a new session so you can pick the next task with full
> context on what is done, what is blocked, and what is risky.

## 1. Big picture

The framework follows `TODO.md` Phase 0 through Phase 5, plus Phase 8
release evidence. Phases 0, 1, 2, and the first task of Phase 3 are
implemented and tested. Phases 3 (storage), 4 (replay), 5 (features),
6 (backtest), 7 (paper), 8 (ops), and 9 (live) are not implemented.

The repository contains **18 test files, 265 passing tests, zero
production signing surface, zero chain connection**. Every code
artifact has matching unit tests and the quality gates (pytest,
ruff check, ruff format, mypy) are green on every commit.

The goal is a single V1 framework for one user-selected Robinhood
Chain Uniswap V4 pool (`docs/product/PROJECT_GOALS.md` §4). Anything
that does not serve that goal is explicitly excluded.

## 2. What is done (and where)

### Phase 0 — engineering baseline

- **T000 architecture decisions**: `docs/architecture.md` plus 7 ADRs in
  `docs/adr/`. ADR-008 (binding document precedence) is the
  authoritative conflict-resolution rule.
- **T001 Python skeleton**: `pyproject.toml` (hatchling, pytest, ruff,
  mypy, pydantic, eth-hash[pycryptodome], httpx, pytest-asyncio),
  `src/robinhood_lp/__init__.py`, `__main__.py`.
- **T002 typed configuration**: `src/robinhood_lp/config/`, V1 single
  chain / single pool / single target token with dual-track approval
  (`technical_eligibility`, `project_risk`, `user_decision`,
  `live_eligible`).
- **T003 CI**: `.github/workflows/{ci,supply-chain,intentional-failure}.yml`
  with SHA-pinned actions. `intentional-failure.yml` proves CI can fail
  closed.
- **T004 threat model**: `docs/threat-model.md` with 12 STRIDE threats
  and `tests/test_no_signing_paths.py`.

### Phase 1 — protocol/domain + math

- **T010 canonical identifiers**: `src/robinhood_lp/protocol/ids.py`
  (`ChainId`, `Address`, `Currency`, `PoolKey`, `PoolId`) plus
  `abi.py` (canonical 5×32 ABI encoding + keccak256 PoolId).
- **T011 tokens / blocks / transactions**: `src/robinhood_lp/protocol/events.py`
  (`BlockRef`, `TransactionRef`, `EventKey`, `TokenMetadata`,
  `CanonicalStatus`).
- **T012 V4 math**: `src/robinhood_lp/protocol/math.py` (TickMath,
  SqrtPriceMath.getAmount0/1Delta, LiquidityAmounts).
- **T013 oracle vectors**: `tools/oracle/` (Foundry project with
  PoolIdOracle, MathOracle, SelectorOracle) plus
  `docs/oracle-manifest.md`, `tests/test_oracle_provenance.py`.

### Phase 2 — chain access + pool discovery

- **T020 bounded RPC adapter**: `src/robinhood_lp/rpc/adapter.py`.
  8 read-only methods, classified retries, failover, range
  splitting, capability probe. httpx-based default transport; tests
  inject a fake transport, so CI is offline.
- **T021 V4 artifacts**: `src/robinhood_lp/protocol/abi_artifacts.py`
  plus `docs/protocol-artifacts/v4-core-e50237c.json` (pinned 4 event
  topics + 4 function selectors with provenance).
- **T022 Initialize scanner + token metadata + registry**:
  `src/robinhood_lp/discovery/{initialize_log, token_metadata, registry}.py`.
  Decoder enforces `keccak256(abi.encode(PoolKey)) == topic1`. Scanner
  drives an in-memory idempotent registry.
- **T023 eligibility classifier**: `src/robinhood_lp/discovery/eligibility.py`.
  5-level RunMode (`rejected | ingestion | backtest | paper | live`)
  with reason-coded audit trail. Static-fee + complete metadata →
  `backtest`; dynamic-fee or unknown hook → `ingestion`.
- **T024 chain capability reporter**: `src/robinhood_lp/discovery/chain_capability.py`
  plus `docs/protocol-artifacts/robinhood-chain-mainnet.json`
  (`chain_id=4663`, PoolManager `0x8366a39cc670b4001a1121b8f6a443a643e40951`,
  StateView `0xf3334192d15450cdd385c8b70e03f9a6bd9e673b`).
  Bytecode hashes intentionally `null` until a real probe runs.

### Phase 3 — versioned historical ingestion (in progress)

- **T030 schemas**: `src/robinhood_lp/storage/schema.py`. Block / tx /
  receipt contexts plus four V4 event records, all with `raw` and
  `unknown_fields` for forward-compatibility, and a byte-exact
  canonical form with embedded `schema_version` / `decode_version` /
  `__class__`.

### Documentation

- `docs/architecture.md` (10-layer dependency graph, module-to-layer
  mapping for every Phase 1-9 task)
- `docs/adr/ADR-001` through `ADR-008` (binding, in ADR-008 precedence
  order)
- `docs/protocol-facts.md` (V4 ABI constants, source-pinned)
- `docs/oracle-manifest.md` (Oracle provenance)
- `docs/protocol-artifacts/v4-core-e50237c.json`
  (4 events, 4 functions)
- `docs/protocol-artifacts/robinhood-chain-mainnet.json` (chain
  capability expectations)
- `tools/oracle/` (Foundry project — PoolIdOracle, MathOracle,
  SelectorOracle)
- `tools/oracle/README.md` (regeneration instructions)
- `docs/threat-model.md` (12 STRIDE threats)

## 3. What is NOT done (per task)

The remaining tasks all sit in Phase 3 onward. Every entry below
states the **actual blocker** (not a wishlist), the **smallest next
step** that can be done offline, and the **dependency** that
unblocks the full task.

### T031 — append-only raw storage

**Status:** schema is defined (T030); storage backend is not.

**Blocker:** decision between JSON-lines (no extra dependency) and
Parquet (requires `pyarrow`, which is not installed). JSON-lines
satisfies all T031 acceptance: append-only, idempotent via unique
event keys, atomic staging, manifest with row count + block bounds
+ file checksum, recoverable from a crash at any commit boundary.

**Smallest next step (offline):**
1. Implement `StorageBackend` ABC with `append(events, schema_version)`
   and `iter_events(range)` (range-bounded, returns sorted by
   `(block_number, tx_index, log_index)`).
2. Provide `JsonLinesStorage(path, partition_layout)` that writes
   one JSON object per line, maintains a sibling `.manifest.json`,
   and uses atomic `tempfile + rename` for the manifest.
3. Test: crash mid-write → previous state intact; idempotent re-run
   on overlap; corrupt manifest → `StorageIntegrityError`.

**Dependency:** none — pure stdlib + json.

### T032 — checkpointed historical ingestion

**Status:** RPC adapter is ready; schemas are ready; **no ingestion
driver**.

**Blocker:** requires real Robinhood Chain RPC traffic (T024 has only
mocked tests). The driver logic itself is testable with a fake
transport.

**Smallest next step (offline):**
1. Implement `IngestionDriver(rpc_adapter, registry, storage, scanner,
   checkpoint_store)` that walks block ranges, calls
   `eth_getLogs(PoolManager_address, topics=Initialize)`, feeds the
   result into `InitializeScanner`, and writes per-block decoded
   events plus the raw log bytes to the storage backend.
2. Implement `CheckpointStore` (file-based JSON line of
   `(chain_id, pool_manager_address, last_block, last_block_hash,
   last_ingestion_time)`).
3. Test: kill/restart at each boundary produces the same registry;
   overlapping blocks converge to one canonical event per
   `(chain, pool_id, block, tx_index, log_index)`.

**Dependency:** T031 storage backend + a Robinhood Chain RPC
endpoint to do real runs. T024 must populate the chain capability
report with real bytecode hashes before T032 can verify them.

### T033 — confirmations and reorgs

**Status:** EventKey already carries `block_hash` so reorg detection
at the identity level is structurally ready; **no reorg policy
implemented**.

**Blocker:** requires realistic multi-block fork fixtures. The
V4 `Initialize` event emits at most once per pool, but
`ModifyLiquidity` and `Swap` can re-org freely; the orphan-marking
and common-ancestor search require non-trivial state machines.

**Smallest next step (offline):**
1. Implement `ReorgDetector(rpc_adapter, finality_blocks,
   storage)` that compares canonical block hashes at the unfinalized
   window head and emits a `ReorgEvent` when they diverge.
2. Implement `OrphanJournal` that records the displaced range so the
   raw log bytes remain intact (T031 must-not) but the normalized
   rows are marked orphaned.
3. Test with synthetic fork fixtures: shallow reorg (1 block) and
   deep reorg (across finality boundary). Finalized data must never
   be rewritten without a critical incident flag.

**Dependency:** T031 + T032 + a real Robinhood Chain endpoint for
fork observation.

### T034 — data-quality and completeness reports

**Status:** scanner stats exist (`ScannerStats`); **no report
generator**.

**Blocker:** depends on T031/T032/T033 producing real coverage data.

**Smallest next step (offline):**
1. Implement `QualityReport(coverage, gaps, duplicates, decode_errors,
   metadata_failures, cross_provider_disagreements)` with stable
   reason codes (enum, not free text).
2. `complete=true` requires zero unexplained gaps; the report records
   a `dataset_manifest_checksum` so a future T040 can refuse to
   replay an incomplete dataset.
3. Test: seeded defect fixtures trigger the correct reason code;
   aggregates never lose warning counts (e.g. `len(warnings) > 0`
   is preserved, not just `count > 0`).

**Dependency:** T031-T033.

### T040 — deterministic event replay

**Status:** math (T012) and event schemas (T030) are ready; **no
replay engine**.

**Blocker:** real decoded events from T032; the engine itself can
be tested with hand-constructed fixture batches.

**Smallest next step (offline):**
1. Implement `ReplayEngine(storage)` that orders events by
   `(block_number, transaction_index, log_index)` and reduces them
   into `(PoolId -> PoolState)` snapshots. PoolState holds
   `sqrt_price_x96`, `current_tick`, `current_liquidity`,
   `fee_growth_global_0/1`, and per-tick liquidity.
2. Implement `checkpoint(state, block_number)` that materialises a
   serialisable snapshot for the next run to resume from.
3. Test: shuffled / chunked / restarted input → byte-identical
   snapshots; duplicate event → idempotent; missing transaction
   index → `ReplayImpossible`; unknown pool → `ReplayImpossible`.

**Dependency:** T031 + T032.

### T041 — reconstruct tick-liquidity state

**Status:** tick math is in T012; **no tick bitmap / liquidity
accumulator**.

**Blocker:** requires real V4 PoolManager deployment on testnet; the
algorithm is testable against Foundry-computed vectors from a V4
deployment harness.

**Smallest next step (offline):**
1. Implement `TickBitmap(tick_spacing) -> dict[tick, InitialisedFlag]`
   and `TickState(liquidity_net, liquidity_gross)`.
2. Implement `apply_modify_liquidity(state, params)` that updates the
   bitmap and the per-tick liquidity according to the V4 rules
   (crossing logic, gross-invariant, spacing invariant).
3. Test with hand-crafted vectors: add / remove / poke; boundary
   crossing both directions; same-block multi-action; max liquidity;
   negative gross detected; spacing invariant.

**Dependency:** T040 + a Foundry harness that produces tick-crossing
vectors from a real V4 deployment.

### T042 — validate replay against independent chain state

**Status:** `RpcAdapter.eth_call` is ready; **no StateView
comparator**.

**Blocker:** requires Robinhood Chain RPC access to call
`StateView.getSlot0(poolId)` and similar at multiple pinned blocks.
The comparison itself is simple arithmetic.

**Smallest next step (offline):**
1. Implement `ReplayValidator(replay_state, state_view_calls)` that
   reads each pool's slot0 / tick / liquidity / fee growth from
   `StateView` at multiple pinned blocks and compares to the replay's
   snapshot.
2. Output a `ValidationReport` listing every integer field that
   matches and every field that diverges. Any divergence on a
   finalized block is **fatal**; on an unfinalized block, divergence
   is **reportable**.
3. Test with Foundry-computed fixtures: V4 storage slot0 read at
   `block N` matches the replay's snapshot at `block N`.

**Dependency:** T040 + real Robinhood Chain RPC.

### T043 — hook semantics evidence packs

**Status:** hook flag analysis (`HookEvidence`) is in T023; **no
hook-specific model**.

**Blocker:** requires either (a) verified source for the hook
contract or (b) a Foundry harness replaying its behaviour. Closed-
source hooks stay at `ingestion` per T023 must-not.

**Smallest next step (offline):**
1. Define `HookEvidencePack(hook_address, code_hash, source_commit,
   verified_source_uri, before_deltas, after_deltas, dynamic_fee_rule,
   external_state, replay_model, invalidation_rule)`.
2. Define the invalidation rule: when `code_hash` changes, the pack
   is invalidated and the pool is demoted to `ingestion`.
3. Test: a pack built against Foundry fixtures matches
   observed receipts; changing the source commit invalidates the
   pack and the demote path is exercised.

**Dependency:** T040 + T042 + Foundry hook fixtures (or
verified-source hook).

### T050 — time/block bars and market features

**Status:** event schemas are ready; **no bar aggregator**.

**Blocker:** requires a real (or replayed) event stream.

**Smallest next step (offline):**
1. Implement `BarAggregator(replay, bar_size_blocks)` that emits
   `(start_block, end_block, volume_token0, volume_token1, swap_count,
   liquidity_avg, fees_token0, fees_token1)` per bar.
2. Test: left/right window boundaries; late event handling; empty
   windows; prefix invariance (`prefix + new_events == re_bar(prefix + new_events)`).

**Dependency:** T040.

### T051 — LP position valuation

**Status:** T012 math ready; **no position model**.

**Blocker:** needs an oracle for `current_sqrt_price_x96` and the
position's tick range. The math is purely a function of
`(sqrt_price_lower, sqrt_price_upper, current_sqrt_price,
liquidity, decimals)`.

**Smallest next step (offline):**
1. Implement `PositionValuator.position_value(position, current_sqrt_price_x96,
   decimals_0, decimals_1) -> tuple[amount0, amount1]`.
2. Test: below / at lower boundary / inside / at upper / above
   boundary; reversals (current price crosses lower then upper);
   add / remove / collect round-trip.

**Dependency:** T050 + T053 (for current price oracle).

### T052 — benchmarks and PnL attribution

**Status:** position math (T051) ready; **no benchmark engine**.

**Blocker:** requires a quote currency (the spec assumes USDG or
similar; the choice lives in `docs/specs/STRATEGY_ECONOMICS.md`).

**Smallest next step (offline):**
1. Implement `Attribution` with components: inventory PnL, LP fees,
   IL / divergence loss, gas, slippage, hook deltas, rebalance cost,
   external cash flow, residual.
2. Define the accounting identity `delta_equity == sum(components) +
   residual` and assert it closes within documented rounding
   tolerance.
3. Test: zero-fee / static-price / no-trade / price-reversal
   fixtures have known results.

**Dependency:** T051 + quote oracle (T053).

### T053 — point-in-time quote and gas valuation

**Status:** **no quote provider abstraction**.

**Blocker:** T053 acceptance requires `provider-neutral observation
schema with observed-at / available-at`. The actual quote sources
(USDG on Robinhood, ETH/USD via Chainlink, etc.) are not yet
identified.

**Smallest next step (offline):**
1. Define `QuoteObservation(provider, pair, price, block_number,
   observed_at, available_at)` with delayed/revised/depegged
   fixtures.
2. Implement `QuoteRouter(observations)` that resolves a requested
   `(token, timestamp)` pair to a single observation; missing is
   never silently filled.
3. Test: missing → `QuoteUnavailable`; revised → router picks the
   later observation; depegged stablecoin → explicit warning.

**Dependency:** independent quote sources; not on-chain dependent.

### T060-T064 (Phase 6 — strategy + backtest)

**Status:** schema and math ready; **no strategy contract, no
backtest engine, no manifest, no robustness harness**.

**Blockers:**
- T060 (strategy contracts) needs a concrete decision on which
  signal library lives in `protocol/`. V1 spec is "single user,
  single pool" so the strategy library will be very small.
- T061 (backtest engine) is the largest single task in Phase 6;
  it consumes T040-T053 and is where the determinism + audit
  requirements hit hardest.
- T063 (experiment manifests) is bound to the backtest engine.

**Smallest next step (offline, all):** none — Phase 6 is wholly
on top of Phase 3-5 outputs and cannot start standalone.

### T070-T072 (Phase 7 — risk + paper)

**Status:** dual-track approval state in T002 config; **no
centralized risk gateway, no paper ledger, no real-time adapter**.

**Blockers:**
- T070 requires T060-T064 outputs as inputs.
- T071 requires a stable paper execution semantic; the simplest is
  `eth_call` against the on-chain PoolManager + StateView using the
  replay snapshots, which still depends on T040-T043.
- T072 requires the reorg-aware ingestion driver (T033).

### T080-T083 (Phase 8 — ops, observability, release)

**Status:** the intentional-failure CI workflow already exists.

**Blockers:** every other Phase 7 task; T083 (release dossier) requires
running T082 (shadow + soak) for a real observation window.

### T090-T096 (Phase 9 — live-readiness planning only)

**Status:** `RunMode.LIVE` exists; **no separate live-execution
project**.

**Blockers:** T090 is explicitly a planning task per `TODO.md` §Phase 9.
It depends on T083 having produced a paper-only dossier and a human
reviewer having signed off. T091-T096 are downstream of the T090
go/no-go decision.

## 4. Cross-cutting risks

- **Real-chain dependency.** T032, T033, T040-T043, T050-T053,
  T070-T072 all require a real (or replayed) Robinhood Chain
  endpoint with the canonical PoolManager and StateView at the
  addresses recorded in `docs/protocol-artifacts/robinhood-chain-mainnet.json`.
  The Foundry oracle can produce unit-test fixtures but cannot
  replace a live endpoint for final acceptance.
- **V4 hook evidence.** Per T023 must-not, the framework does not
  promote a pool past `ingestion` without verified hook behaviour.
  V1's only supported pool with a hook must therefore ship a hook
  evidence pack (T043) before paper or live use.
- **Document drift.** `TODO.md`, `docs/product/`, and the ADRs are
  all updated by humans / other agents. ADR-008 (binding document
  precedence) is the conflict-resolution rule; any contradiction
  must be resolved by editing the lower-precedence document.

## 5. Recommended order for a follow-up session

The next session should pick **one** of the following offline tasks.
Each is bounded, has no real-chain dependency, and pushes the
framework forward without re-introducing cross-cutting complexity.

1. **T031 (small, ~half a day).** `JsonLinesStorage` is pure stdlib +
   json. The unit tests can cover atomic staging, idempotent overlap
   re-run, and crash recovery without any RPC. This unblocks T032.
2. **T053 (small, ~half a day).** `QuoteObservation` and `QuoteRouter`
   are pure data structures; the test suite covers missing/revised/
   depegged without any RPC. Useful for T051-T052 even before a
   real quote source is chosen.
3. **T034 (medium, ~one day).** The reason-code enum, the report
   dataclass, and the seeded-defect test suite are all pure local
   code. The actual coverage data is stubbed until T031-T033 exist.

The remaining offline-friendly tasks (T033 partial, T040 partial,
T041 partial) all need significant test-fixture engineering and
should wait for a real Robinhood Chain endpoint.

## 6. How to start a follow-up session

1. Read this file end to end.
2. Pick one task from §5 and confirm with the user before starting.
3. Re-read `docs/product/PROJECT_GOALS.md` and
   `docs/product/ASSET_ADMISSION.md` to make sure the new code matches
   the binding product scope (V1 single chain + single pool + single
   token).
4. Re-read `docs/adr/ADR-008-document-precedence.md` so any conflict
   between source code and the binding documents is resolved by
   editing the lower-precedence side.
5. Implement the smallest reasonable change; add or update tests;
   run the full quality gate (`pytest`, `ruff check`, `ruff format
   --check`, `mypy src tests`); inspect `git diff`; commit; push.
6. Tick the corresponding `[ ]` in `TODO.md` and commit that change
   separately so the status board stays accurate.

## 7. Quick index of files

```
docs/
  STATUS.md                          ← this file
  architecture.md                    ← 10-layer dependency graph
  adr/ADR-001..ADR-008               ← binding architectural decisions
  threat-model.md                    ← 12 STRIDE threats
  protocol-facts.md                  ← V4 ABI constants
  oracle-manifest.md                 ← Oracle provenance
  protocol-artifacts/
    v4-core-e50237c.json             ← event topics + function selectors
    robinhood-chain-mainnet.json     ← T024 expected deployment
  product/                           ← product binding docs (other agents)
  specs/                              ← strategy/economics specs (other agents)

src/robinhood_lp/
  __init__.py, __main__.py           ← T001
  config/                            ← T002 typed configuration
  protocol/                          ← T010-T013 + T030 math + ids
  rpc/                               ← T020 bounded RPC adapter
  discovery/                         ← T022, T023, T024
  storage/                           ← T030 schemas

tools/oracle/                        ← Foundry oracle (T010/T012/T013)
tests/                               ← 18 test files
.github/workflows/                  ← T003 CI + supply-chain gates
```

## 8. Status board

| Phase | Tasks done / total | Last commit |
| --- | --- | --- |
| 0 | 5 / 5 | 30986db |
| 1 | 4 / 4 | 30986db |
| 2 | 5 / 5 | 5deda6f |
| 3 | 1 / 5 (T030 done; T031-T034 pending) | 2270e85 |
| 4 | 0 / 4 | — |
| 5 | 0 / 4 | — |
| 6 | 0 / 5 | — |
| 7 | 0 / 3 | — |
| 8 | 0 / 4 | — |
| 9 | 0 / 7 (planning only) | — |

16 implementation commits + 1 docs commit have been pushed to
`main`. The framework is ready for the next session to continue
from T031.
