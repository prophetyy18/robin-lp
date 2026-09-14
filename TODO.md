# Development TODO

This is the executable delivery plan for the Robinhood Chain / Uniswap V4 LP
research and paper-trading system. Implement **one numbered task at a time**.
A phase is a release gate, not a prompt and not a unit of implementation.

Last research review: **2026-09-12**. Network and deployment facts are mutable;
the links in this file are evidence sources, not values that may be copied into
code without the verification required by T024.

## 1. Product boundary and final delivery standard

The first deliverable is a reproducible research and paper-trading framework for
any explicitly configured Uniswap V4 pool on a verified EVM chain. It is not a
FLYBRAIN/USDG-specific bot. Live transaction submission is outside the current
authorized scope.

A pool is identified by `(chain_id, PoolKey)`, where `PoolKey` contains:

- `currency0`
- `currency1`
- `fee`, including the dynamic-fee sentinel
- `tickSpacing`
- `hooks`

Symbols, names, URLs, and UI labels are metadata only. A derived `PoolId` is not
accepted until it matches the canonical V4 implementation.

The current product is delivered only when all of these statements are true:

- a clean Python 3.12 environment can install and run the documented commands;
- a user can verify a chain deployment, discover pools, ingest a fixed historical
  range, resume after interruption, and produce a completeness report;
- replayed pool state matches independent block-pinned on-chain reads for the
  declared support class;
- at least two materially different pool fixtures pass end-to-end (different
  tokens/decimals/fees/tick spacing/hooks; one must use native currency or a
  nonzero hook);
- a saved experiment manifest reproduces the same decisions, ledger, and metrics;
- paper mode restarts without gaps, duplicated decisions, or balance drift;
- unsupported hook semantics, stale/incomplete data, failed reconciliation, and
  breached risk limits fail closed;
- no production signing path, private key loader, transaction broadcaster, or
  hidden live-mode switch exists in the current release.

Performance profitability is **not** a delivery criterion. Correct negative or
unprofitable results are valid; fabricated completeness or optimistic accounting
is not.

## 2. Task contract: required for every numbered task

Before editing:

1. Read `CLAUDE.md`, `docs/product/PROJECT_GOALS.md`, this file, the task's
   references, and all affected files.
2. Restate the task outcome, dependencies, inputs, outputs, and acceptance checks.
3. Verify every dependency checkbox and phase entry gate. If one is unmet, stop
   and report the exact missing artifact.
4. Record any mutable external fact with source URL, retrieval time, chain ID,
   block number/hash when applicable, and checksum or code hash when applicable.

While editing:

1. Make the smallest change that completes only the selected task.
2. Keep protocol/domain, RPC, storage, features, strategy, risk, execution, and
   presentation dependencies pointing inward through explicit interfaces.
3. Preserve integers from RPC through protocol accounting. Convert to decimal or
   float only at an explicitly named display/statistical boundary.
4. Make ordering and time semantics explicit. Never use wall-clock time or an
   unseeded random source inside deterministic replay/backtests.
5. Fail closed on missing data, unknown hooks, unsupported RPC behavior, address
   mismatch, or reconciliation failure.

Before marking `[x]`:

1. Run task-specific normal, boundary, invalid-input, and failure-injection tests.
2. Run the full unit suite plus formatter, linter, and strict type checker.
3. Run integration/golden/fork tests required by the task; tests skipped because
   credentials are absent do not satisfy an acceptance criterion.
4. Inspect `git diff`; do not include unrelated changes or generated secrets.
5. Update only the completed checkbox and report commands, results, assumptions,
   data/block versions, and residual risks.

### Universal Definition of Done

Unless a task explicitly narrows it, completion requires:

- named deliverables exist and contain no placeholder implementation;
- public interfaces, units, signs, rounding, and non-obvious formulas are documented;
- errors include chain, pool, block/range, endpoint alias, attempt, and causal
  exception where those fields apply, with credentials redacted;
- tests cover two heterogeneous fixtures plus zero/min/max and malformed inputs;
- repeated runs have byte-equivalent canonical outputs after excluding declared
  observational fields such as ingestion wall time;
- `pytest`, Ruff format/check, and the selected strict type checker pass;
- no secret, `.env` value, private key, seed, credential-bearing RPC URL, or raw
  authorization header is logged, snapshotted, or committed;
- source/version/checksum is recorded for copied ABI, formula, vector, or schema;
- acceptance evidence is machine-readable when the task produces data or reports.

### Global prohibitions

During **all current phases**, do not:

- sign, simulate signing, submit, bundle, relay, or deploy a live transaction;
- add a private-key/seed loader or keep signing material in process memory;
- infer a PoolManager/StateView address from another chain or from CREATE2
  similarity; verify each `(chain_id, address, runtime_code_hash)` independently;
- treat a third-party indexer, token symbol, explorer label, or frontend as protocol
  truth;
- silently model an unknown hook as a no-hook pool;
- fill historical gaps with interpolation, forward fill, synthetic swaps, or a
  later state read;
- calculate strategy features with observations that were unavailable at the
  decision timestamp;
- report fee APR alone as strategy performance or combine principal, fees, gas,
  hook deltas, and benchmark PnL into an unreconciled number;
- weaken a test, tolerance, type, safety gate, or risk limit merely to make CI pass.

## 3. Support levels and promotion rules

Each `(chain, PoolKey)` has one explicit level; support never propagates just
because another pool shares tokens or bytecode.

| Level | Allowed | Minimum evidence | Explicitly forbidden |
| --- | --- | --- | --- |
| `rejected` | retain reason only | invalid identity/deployment/data evidence | ingestion, simulation, paper |
| `ingestion` | raw/normalized history and quality reports | verified deployment and ABI | valuation, strategy, paper |
| `backtest` | deterministic replay/valuation/backtest | replay reconciliation and modeled hook effects | paper decisions |
| `paper` | real-time decisions and simulated ledger | recovery, freshness, risk and shadow reconciliation gates | signed/broadcast transactions |
| `live` | reserved for a future separately authorized project | external security review and explicit owner approval | everything until that project exists |

Promotion is manual, evidence-backed, reversible, and audited. Any unresolved
data gap, code-hash change, hook upgrade/behavior change, or reconciliation breach
automatically demotes the pool to the last safe level.

## 4. Research findings and build-vs-reuse decision

The system uses raw JSON-RPC plus pinned Uniswap artifacts as the source of truth.
Existing services/libraries are useful as independent comparators, not unquestioned
inputs:

| Existing solution | Reuse | Do not rely on it for |
| --- | --- | --- |
| Uniswap V4 core/periphery and SDK | ABI/math vectors, PoolKey/PoolId semantics, StateView checks | mutable `main`, unverified chain addresses, Python runtime logic |
| Uniswap V4 Subgraph / The Graph | discovery cross-checks and exploratory aggregates | authoritative completeness, exact reorg policy, unsupported-chain coverage |
| web3.py | typed JSON-RPC transport/decoding primitives | provider limits, automatic safe retries, durable checkpoints |
| NautilusTrader | event-time, deterministic IDs, research/paper interface ideas | V4 pool/hook accounting out of the box |
| VectorBT | fast parameter exploration after validated event results exist | canonical event-by-event fills and hook semantics |
| QuantConnect LEAN | time-frontier and reality-model warnings | V4 liquidity reconstruction |
| Aloe/Gamma V3 simulators | independent formula/strategy test ideas | V4 correctness or hooked-pool support |

Decision rationale: V4's singleton, per-pool hook behavior, dynamic fees, and
chain-specific deployment facts require a small protocol-specific core. General
engines may later consume its validated outputs, but cannot replace it.

## Phase 0 — Decisions, safety boundary, and engineering baseline

**Purpose:** make architectural and safety choices reviewable before code creates
compatibility commitments.

**Entry:** repository rules and the current project-goal baseline are accepted.

**Exit gate:** ADRs are approved; clean install and CI gates pass; the threat model
contains no unowned critical risk; no chain connection is needed to run unit tests.

**Phase prohibitions:** no RPC calls in domain modules, no production addresses in
defaults, no database/framework selected without an ADR, no trading code.

- [x] **T000 — Record initial architecture decisions**
  - Outcome: future tasks share explicit choices for Python/Web3 client, package
    boundaries, configuration, storage/query format, precision, clock, and chain
    support lifecycle.
  - Deliverables: `docs/architecture.md`; one ADR each for client/concurrency,
    storage, configuration/secrets, integer/decimal precision, supported-chain
    policy, and dependency direction.
  - Acceptance: every ADR records context, decision, alternatives, consequences,
    migration trigger, and owner; architecture includes a dependency diagram and
    maps every planned module to exactly one layer.
  - Must not: write implementation code, assume Robinhood-specific contracts, or
    select a tool solely because it appears in this plan.
  - References: R1, R3, R7, R11, R17.

- [x] **T001 — Create the Python 3.12 project skeleton** (depends on T000)
  - Outcome: deterministic local development entry point.
  - Deliverables: `pyproject.toml`, locked dependencies, `src/robinhood_lp/`,
    `tests/`, test/format/lint/type commands, and one import/CLI smoke test.
  - Acceptance: build and install from a clean environment; all quality commands
    pass twice; package metadata contains supported Python bounds.
  - Must not: add blockchain/storage/dataframe dependencies before their first
    consumer task or expose environment contents in diagnostics.
  - References: R21, R22.

- [x] **T002 — Add safe typed configuration** (depends on T001)
  - Outcome: invalid or secret-bearing configuration cannot enter the system.
  - Deliverables: strict models for chain ID, RPC env-var names, endpoint aliases,
    confirmation/finality policy, start block, verified contract references,
    request limits, complete PoolKey, and run mode; `.env.example` names only.
  - Acceptance: multi-chain/multi-pool fixtures load; unknown fields, duplicate
    identities, literal credential URLs, invalid addresses/ranges, and chain-ID
    mismatch fail with redacted errors; serialization never includes secret values.
  - Must not: default to `latest`, auto-correct addresses/PoolKeys, or accept symbol
    identifiers.
  - References: R1, R2, R9.

- [x] **T003 — Establish CI and supply-chain gates** (depends on T001)
  - Outcome: every change receives the same reproducible checks.
  - Deliverables: pinned CI actions, tests, formatting, lint, type check, lockfile
    integrity, secret scan, dependency review/vulnerability scan, and artifact
    retention policy.
  - Acceptance: baseline passes; controlled branches prove test/lint/type/secret
    and vulnerable-dependency findings fail; every suppression has reason, owner,
    and expiry.
  - Must not: use floating action tags, auto-fix dependencies in CI, upload `.env`
    or RPC responses, or make network integration tests silently optional.
  - References: R21, R22.

- [x] **T004 — Threat model and live-safety invariant** (depends on T000)
  - Outcome: data poisoning, reorg, hook, RPC, supply-chain, key, and operational
    risks have owners and fail-safe responses.
  - Deliverables: `docs/threat-model.md`, trust boundaries/data-flow diagram, abuse
    cases, severity rubric, controls matrix, and explicit proof that current package
    exposes no signing/broadcast interface.
  - Acceptance: each critical/high threat maps to a test or later task; CI has a
    regression test denying forbidden signing dependencies/APIs.
  - Must not: call paper mode “safe” without enumerating external side effects or
    defer a critical unowned risk to live phase.
  - References: R4, R6, R21.

## Phase 1 — Canonical protocol model and deterministic math

**Purpose:** create a chain-independent protocol kernel whose results can be
compared byte-for-byte with pinned Solidity/SDK oracles.

**Entry:** Phase 0 exit gate.

**Exit gate:** identifiers and all supported math match independent golden and
property vectors at bounds; no RPC/storage import exists in the protocol package.

**Phase prohibitions:** no floats, display symbols as IDs, approximate tick math,
or vectors generated by the Python implementation under test.

- [x] **T010 — Model canonical protocol identifiers** (depends on T001)
  - Outcome: impossible/misordered protocol identities are unrepresentable.
  - Deliverables: immutable `ChainId`, `Address`, `Currency`, `PoolKey`, `PoolId`;
    canonical ABI encoding and PoolId derivation.
  - Acceptance: exact-match pinned vectors cover native currency, static/dynamic
    fee, zero/nonzero hooks, min/max tick spacing, ordering/equality errors, and
    cross-chain namespacing.
  - Must not: hash JSON/text, checksum-normalize before validating bytes, or include
    token metadata in equality/hash.
  - References: R3, R4, R5.

- [x] **T011 — Model tokens, blocks, transactions, and raw event identity**
  (depends on T010)
  - Outcome: every observation is uniquely located in a chain history.
  - Deliverables: token/native metadata, `BlockRef`, `TransactionRef`, `EventKey`,
    canonical/orphaned/removed status, and deterministic serialization.
  - Acceptance: identity includes chain ID, block hash/number, transaction hash and
    log index; collisions across chains/forks are tested; absent/nonstandard token
    metadata remains representable without changing identity.
  - Must not: require symbol/name, use `(tx_hash, log_index)` without chain, or
    discard an orphaned identity.
  - References: R4, R9, R10.

- [x] **T012 — Port V4 tick, price, liquidity, and amount math** (depends on T010)
  - Outcome: Python reproduces Solidity bounds and rounding exactly.
  - Deliverables: tick/`sqrtPriceX96`, raw amount, display price, liquidity, bounded
    position amount, and usable-tick functions with units and rounding documented.
  - Acceptance: golden/property tests cover MIN/MAX tick, both swap directions,
    exact-in/out rounding, 0/6/8/18/24 decimals, overflow/revert domains, monotonicity,
    and round trips against a pinned Solidity harness.
  - Must not: translate through float, broaden Solidity integer domains, or use a
    tolerance where canonical integer equality is possible.
  - References: R3, R14, R15.

- [x] **T013 — Build independent protocol conformance vectors** (depends on T010)
  - Outcome: Python tests do not validate themselves.
  - Deliverables: pinned Foundry/TypeScript vector generator, immutable JSON vectors,
    provenance manifest with repository commits/tool versions/checksums, and drift test.
  - Acceptance: a clean generator run reproduces committed vectors; intentional
    source/version drift fails; at least one manually reviewed vector per edge class.
  - Must not: import Python production math into the oracle or fetch mutable `main`
    during tests.
  - References: R3, R8, R22.

## Phase 2 — Verified chain access and pool discovery

**Purpose:** read only from a proven chain deployment and build a complete PoolKey
registry from canonical events.

**Entry:** typed configuration and event identities complete.

**Exit gate:** two providers (or provider plus fixture node) give equivalent results;
deployment report passes; every discovered pool has an explicit support reason.

**Phase prohibitions:** no signing middleware, hosted filters as sole ingestion
mechanism, copied deployment address without code verification, or unbounded RPC.

- [x] **T020 — Build a read-only bounded RPC adapter** (depends on T002, T011)
  - Outcome: deterministic, observable reads despite provider limits/failures.
  - Deliverables: bounded `eth_getLogs`, block/header/receipt and block-pinned
    `eth_call`; timeouts, classified retries with jitter, endpoint failover, adaptive
    range splitting, capability probe, and redacted metrics.
  - Acceptance: mocked tests cover 429/5xx/timeouts, malformed/partial responses,
    duplicate/out-of-order logs, inconsistent heads, single-block oversize failure,
    retry exhaustion, and no retry of non-idempotent/invalid requests.
  - Must not: expose generic arbitrary RPC, use remote filter state for durability,
    retry forever, or log endpoint credentials.
  - References: R9, R10, R11.

- [x] **T021 — Pin canonical V4 artifacts** (depends on T001)
  - Outcome: decoding/state calls are tied to reviewable protocol sources.
  - Deliverables: minimum PoolManager and StateView interfaces/ABIs, event topics,
    selectors, source repo+commit, compiler/package version, license, and SHA-256.
  - Acceptance: regenerate-and-compare test detects any ABI/topic/selector drift;
    only actually used ABI entries are stored.
  - Must not: scrape explorer ABI as primary source, pin a branch, or silently mix
    incompatible core/periphery commits.
  - References: R3, R4, R7.

- [x] **T022 — Discover and register pools from `Initialize`**
  (depends on T010, T020, T021, T024)
  - Outcome: complete, idempotent PoolKey registry for a verified PoolManager range.
  - Deliverables: Initialize decoder/scanner, derived-ID verification, registry,
    defensive token metadata reader, provenance and scan coverage.
  - Acceptance: stop/resume/overlap are identical; native currency, broken/reverting/
    oversized metadata, dynamic fee, nonzero hook, duplicates, and conflicting
    Initialize data are tested; conflict fails closed.
  - Must not: query a factory (V4 is singleton), discover by token symbol, or drop
    pools whose metadata call fails.
  - References: R3, R4, R12.

- [x] **T023 — Classify pool and hook eligibility** (depends on T022)
  - Outcome: ingestion capability cannot be confused with simulation capability.
  - Deliverables: reason-coded classification for deployment, static/dynamic fee,
    hook flags/code hash/upgradeability evidence, metadata/data coverage, modeled
    deltas, and resulting support level.
  - Acceptance: every registry row has a level plus evidence; unknown hooks and
    return-delta flags default to `ingestion`; behavior/code-hash change demotes;
    classification decisions are deterministic and audited.
  - Must not: infer semantics from hook address flags alone, whitelist by name, or
    fall back to plain-pool behavior.
  - References: R6, R14.

- [x] **T024 — Verify chain capabilities and contract deployments**
  (depends on T002, T020, T021)
  - Outcome: configured mutable facts are proven before ingestion.
  - Deliverables: report containing RPC-reported chain ID, genesis hash, latest/safe/
    finalized behavior, archive depth, range limits, PoolManager/StateView bytecode
    and code hashes, deployment block/transaction evidence, and source retrieval time.
  - Acceptance: wrong chain, empty/proxy/unexpected bytecode, inconsistent providers,
    unavailable historical calls, and unsupported block tags fail; report pins a
    block hash and is reproducible.
  - Must not: assume Robinhood mainnet/testnet parity, reuse Arbitrum addresses, or
    accept an explorer label as bytecode proof.
  - References: R1, R2, R9.

## Phase 3 — Versioned historical ingestion and audit storage

**Purpose:** preserve enough immutable evidence to re-decode, audit, and recover.

**Entry:** Phase 2 exit gate and storage ADR approved.

**Exit gate:** a fixed range survives interruption, overlap, corruption, and
synthetic reorg tests while producing zero unexplained gaps.

**Phase prohibitions:** no in-place destruction of raw history, future-state calls,
silent schema coercion, interpolation, or completion based only on “no rows”.

- [x] **T030 — Define versioned raw and normalized schemas**
  (depends on T011, T021)
  - Outcome: raw evidence is lossless and normalized data is evolvable.
  - Deliverables: schemas for block/header/receipt context and V4 `Initialize`,
    `ModifyLiquidity`, `Swap`, `Donate`; raw topics/data/response fields alongside
    typed fields; schema/decode version and ingestion provenance.
  - Acceptance: raw JSON → normalized → canonical serialization round trip loses no
    source bytes; compatibility/migration tests cover unknown fields and old versions.
  - Must not: overwrite raw values with decoded/display values, store integers as
    float, or make token metadata required.
  - References: R4, R9.

- [ ] **T031 — Implement append-only raw storage** (depends on T030)
  - Outcome: writes are atomic, deduplicated, verifiable, and recoverable.
  - Deliverables: partition layout justified by ADR; staging/atomic commit; manifest
    with row count, block bounds/hashes and file checksum; event unique keys; reader.
  - Acceptance: repeat/overlap is idempotent; crash at every commit boundary leaves
    either previous or complete new state; corruption and manifest mismatch are found.
  - Must not: mutate/delete orphaned raw evidence, expose half-written partitions,
    or trust filenames as manifests.
  - References: R13.

- [ ] **T032 — Implement checkpointed historical ingestion**
  (depends on T020, T022, T031)
  - Outcome: bounded scans resume without gaps or duplicate canonical events.
  - Deliverables: range planner, durable checkpoint, explicit scanned-empty intervals,
    retry ledger, run manifest, and deterministic normalization pipeline.
  - Acceptance: kill/restart at each boundary and overlapping/concurrent-run tests
    converge; every requested block is covered exactly once by a successful or
    explicitly failed interval; failed intervals prevent completion.
  - Must not: advance checkpoint before durable commit, infer empty coverage from an
    empty table, or continue past an irreducible failed block as “complete”.
  - References: R9, R11.

- [ ] **T033 — Handle confirmations and reorgs** (depends on T032)
  - Outcome: canonical views converge while audit history remains intact.
  - Deliverables: configurable finality policy, unfinalized hash window, common-
    ancestor search, orphan marking/replacement, deep-reorg halt, and reorg journal.
  - Acceptance: shallow/deep reorg, provider disagreement, removed logs, and orphan
    reappearance fixtures converge or halt as specified; finalized data is never
    rewritten without a critical incident.
  - Must not: equate confirmations with finality across chains, delete orphan raw
    logs, or continue decisions during unresolved ancestry.
  - References: R9, R10.

- [ ] **T034 — Produce data-quality and completeness reports**
  (depends on T032, T033)
  - Outcome: downstream code consumes only explicitly qualified datasets.
  - Deliverables: machine/human reports for range coverage, block/hash gaps, duplicate/
    misordered logs, unknown pools, decode errors, impossible values, stale endpoints,
    metadata failures, and cross-provider/subgraph discrepancies.
  - Acceptance: seeded defect fixtures trigger the correct reason code; `complete=true`
    requires zero unexplained gaps/errors and records dataset manifest checksum.
  - Must not: let warning counts disappear in aggregates or treat a third-party
    match as proof when raw chain evidence is incomplete.
  - References: R12, R13.

## Phase 4 — Deterministic state reconstruction and hook evidence

**Purpose:** prove what can and cannot be reconstructed from observations without
querying future state.

**Entry:** a complete, finalized dataset manifest from Phase 3.

**Exit gate:** supported pools reconcile at multiple pinned blocks; discrepancies
and hook assumptions are zero or explicitly blocking.

**Phase prohibitions:** no current/latest state in historical replay, no arbitrary
tolerance for exact integers, no inferred hook effects, no backtest on failed pools.

- [ ] **T040 — Build deterministic event replay** (depends on T012, T030, T034)
  - Outcome: chunking/storage order cannot change observable pool state.
  - Deliverables: total ordering by block number, transaction index, log index;
    replayed price/tick/active liquidity/volume/effective swap fee; checkpoint state.
  - Acceptance: shuffled/chunked/restarted inputs yield identical checkpoints; duplicate,
    missing transaction index, impossible transition, and unknown pool fail explicitly.
    For dynamic-fee pools, preserve the effective fee emitted by each `Swap`; do not
    claim event-only reconstruction of unobserved between-swap fee updates.
  - Must not: use ingestion order, wall time, future calls, or token display decimals
    in protocol state.
  - References: R4, R14.

- [ ] **T041 — Reconstruct tick-liquidity state** (depends on T040)
  - Outcome: initialized ticks and active-liquidity crossings obey V4 invariants.
  - Deliverables: gross/net tick updates, bitmap/initialized state, crossing logic,
    position key treatment, and invariant checks.
  - Acceptance: add/remove/poke, boundary crossing both directions, same-block multi-
    action, max liquidity, negative gross, spacing and overflow vectors match core.
  - Must not: derive owner inventory from pool liquidity events or assume one event
    equals one user position lifecycle.
  - References: R3, R4.

- [ ] **T042 — Validate replay against independent chain state** (depends on T041)
  - Outcome: replay correctness is measured, not asserted.
  - Deliverables: block-pinned StateView comparison runner and discrepancy report for
    slot0, active liquidity, ticks/bitmap, fee growth where supported.
  - Acceptance: multiple heights and heterogeneous pools match exact integer fields;
    any tolerated derived decimal lists formula and bound; unexplained mismatch makes
    dataset ineligible for Phase 5.
  - Must not: validate against the same replay output, `latest`, or only one favorable
    checkpoint.
  - References: R7, R8.

- [ ] **T043 — Build hook semantics evidence packs** (depends on T023, T042)
  - Outcome: each promoted hooked pool has versioned, reviewable economic semantics.
  - Deliverables: hook address/code hash/proxy implementation and upgrade authority,
    flags, verified source/ABI, before/after deltas, dynamic-fee behavior, external
    state dependencies, replay model, adversarial tests, and invalidation rule.
  - Acceptance: fork/golden transactions cover every enabled callback and return-
    delta path; model matches observed receipts/state; code/implementation change
    invalidates the pack and demotes the pool.
  - Must not: promote closed-source/unverified behavior beyond ingestion, equate flags
    with semantics, or generalize evidence from one hook deployment to another.
  - References: R4, R6, R14.

## Phase 5 — Point-in-time features, position valuation, and attribution

**Purpose:** turn reconciled protocol state into decision-safe features and fully
reconciled economic results.

**Entry:** Phase 4 exit gate for each target pool, including T043 for every hooked
pool promoted beyond ingestion.

**Exit gate:** every feature has availability time/units; position accounting closes
for boundary scenarios; quote valuation has point-in-time provenance.

**Phase prohibitions:** no backward fill, centered windows, future close prices,
unlabeled oracle substitution, or residual PnL hidden in “other”.

- [ ] **T050 — Add time/block bars and market features** (depends on T042)
  - Outcome: features expose only information available at their declared timestamp.
  - Deliverables: bars for volume, realized volatility, price range, active liquidity,
    depth proxy, effective fee, gas, and freshness; event-time/watermark contract.
  - Acceptance: tests prove left/right window boundaries, late event handling, empty
    windows, irregular blocks, and prefix invariance (adding future data never changes
    an earlier feature row).
  - Must not: use centered/global normalization or label a proxy as observed depth.
  - References: R18.

- [ ] **T051 — Add LP position valuation** (depends on T012, T042)
  - Outcome: principal inventory and earned fees are separately correct at any point.
  - Deliverables: token0/token1 raw inventory, in/out-of-range state, principal,
    fee-growth accounting, collections, donations/hook adjustments where supported.
  - Acceptance: below/at/inside/at/above bounds, reversals, add/remove/collect, native
    currency, decimal asymmetry, and rounding agree with pinned vectors/StateView.
  - Must not: count deposits/withdrawals as PnL or infer a wallet's ownership solely
    from PoolManager sender.
  - References: R7, R8, R15.

- [ ] **T052 — Add benchmarks and PnL attribution** (depends on T051, T053)
  - Outcome: total equity change reconciles to understandable components.
  - Deliverables: hold and configurable rebalanced-inventory benchmarks; inventory
    PnL, LP fees, divergence/IL, LVR/adverse-selection proxy, gas, slippage, hook
    deltas, rebalance cost, external cash flow, and residual.
  - Acceptance: accounting identity closes within documented conversion rounding;
    zero-fee/static-price/no-trade/price-reversal fixtures have known results; residual
    above threshold fails report generation.
  - Must not: call IL the sole opportunity cost or annualize an incomplete period
    without labeling method and coverage.
  - References: R15, R16.

- [ ] **T053 — Add point-in-time quote and gas valuation** (depends on T034)
  - Outcome: cross-token performance never uses future or unverifiable prices.
  - Deliverables: provider-neutral observation schema with observed-at/available-at,
    source, pair, block/time, confidence/staleness; conversion graph and missing policy.
  - Acceptance: delayed/revised/depegged/missing/cross-rate fixtures are tested;
    prefix invariance holds; reports can also remain in raw-token units.
  - Must not: assume a stablecoin equals one dollar, silently use current prices, or
    mix sources/frequencies without provenance.
  - References: R18.

## Phase 6 — Event-driven backtesting and research protocol

**Purpose:** evaluate generic strategies under explicit information, fill, latency,
cost, and robustness assumptions.

**Entry:** Phase 5 exit gate and target pools at `backtest` support.

**Exit gate:** manifests reproduce decisions/ledger/reports; look-ahead adversarial
tests pass; results include honest benchmarks, costs, and in/out-of-sample labels.

**Phase prohibitions:** no strategy RPC/storage access, same-event clairvoyant fill,
parameter selection on held-out data, or profitability-based acceptance.

- [ ] **T060 — Define strategy contracts** (depends on T050, T051)
  - Outcome: strategy logic is a pure consumer of timestamped state.
  - Deliverables: immutable observation/portfolio input and target-position/no-op
    output; deterministic clock/random seed; tick/capital proposal validation.
  - Acceptance: dependency tests prevent RPC/storage/signing/execution imports;
    one instance works unchanged on heterogeneous eligible PoolKeys; invalid ticks,
    stale state, NaN/display values, and excessive capital fail before intent creation.
  - Must not: let strategy approve its own risk or mutate ledger/state directly.
  - References: R17.

- [ ] **T061 — Build the event-driven backtest engine** (depends on T052, T060)
  - Outcome: decision and simulated execution timing are explicit and deterministic.
  - Deliverables: ordered event clock, information frontier, decision→risk→latency→fill
    pipeline, liquidity/fee/gas/slippage/failure models, immutable audit events.
  - Acceptance: future-data trap tests fail; same manifest produces identical event IDs,
    decisions and ledger; same-timestamp ordering, rejected/partial/delayed fills,
    out-of-range accrual, and shutdown behavior are covered.
  - Must not: fill using the price that triggered a decision unless the declared
    model proves availability; hide failed/rejected intents.
  - References: R17, R18.

- [ ] **T062 — Implement baseline strategies** (depends on T061)
  - Outcome: comparisons start from simple, auditable, pool-agnostic policies.
  - Deliverables: hold/no-LP, protocol-valid broad range, fixed-width, volatility-
    width, and out-of-range rebalance policies with documented parameters.
  - Acceptance: zero-action and known-path golden scenarios; no addresses/symbols or
    token-specific thresholds; usable ticks and insufficient capital handled.
  - Must not: optimize defaults on the evaluation period or claim broad range is
    literally infinite/full range where protocol/griefing constraints disagree.
  - References: R8, R15, R20.

- [ ] **T063 — Add experiment manifests and reports** (depends on T062)
  - Outcome: every published result is reproducible and auditable.
  - Deliverables: manifest with dataset/schema/code/dependency revisions, chain,
    PoolKey, interval, strategy parameters, seed, clock/fill/cost/quote assumptions;
    decisions, ledger, metrics, and report checksums.
  - Acceptance: one command reruns a saved manifest; total/annualized return, drawdown,
    turnover, time in range, fees, IL/LVR proxy, gas/slippage and benchmark excess
    reconcile; tampered inputs fail checksum validation.
  - Must not: overwrite a prior run or publish a chart without manifest/coverage.
  - References: R17, R19.

- [ ] **T064 — Add robustness and anti-overfitting analysis** (depends on T063)
  - Outcome: favorable parameters are challenged outside their selection sample.
  - Deliverables: anchored/rolling walk-forward splits, untouched holdout, parameter
    surfaces, pool/regime segmentation, stressed gas/latency/slippage/fees, missing-
    data and reorg scenarios, and multiple-comparison disclosure.
  - Acceptance: split boundaries are point-in-time and machine recorded; reports
    prominently separate train/validation/test; failure scenarios halt or degrade as
    specified; conclusions include sensitivity, not only best run.
  - Must not: retune on test, discard losing pools/regimes post hoc, or rank solely
    by annualized return.
  - References: R16, R18, R19.

## Phase 7 — Central risk and paper execution

**Purpose:** exercise the same intent/risk/ledger contracts under live data without
creating any ability to transact.

**Entry:** robust backtest evidence and explicit `paper` promotion for target pools.

**Exit gate:** fault/restart/shadow tests reconcile; all kill switches fail closed;
the process contains no signer/broadcaster.

**Phase prohibitions:** no wallet approvals, calldata broadcast, private keys,
real balances presented as paper balances, or risk bypass for manual intents.

- [ ] **T070 — Define centralized risk checks** (depends on T060)
  - Outcome: every intent receives one auditable approve/reject decision.
  - Deliverables: pool/hook eligibility, deployment hash, freshness/completeness,
    capital/token concentration, price deviation, range, rebalance cadence/cost,
    drawdown, loss, gas, and global/per-pool kill-switch checks with reason codes.
  - Acceptance: all intent sources use the same gateway; deny-by-default tests cover
    missing/stale/NaN/overflow/config errors and simultaneous breaches; configuration
    changes are versioned and cannot retroactively alter decisions.
  - Must not: let execution/strategy override rejection or continue on risk-service
    error/timeout.
  - References: R17.

- [ ] **T071 — Implement paper execution and immutable ledger**
  (depends on T061, T070)
  - Outcome: simulated balances/positions reconcile through realistic failures.
  - Deliverables: intents, approvals, fills, liquidity changes, fee collection, gas,
    failure/retry and balances as append-only double-entry-style audit events.
  - Acceptance: backtest and paper share strategy/risk interfaces; every balance
    change links to an event; duplicate/reordered/crash recovery is idempotent; daily
    opening + flows + PnL = closing equity.
  - Must not: mutate balances without a ledger event, call `eth_sendRawTransaction`,
    or label a simulated receipt as on-chain.
  - References: R17.

- [ ] **T072 — Add real-time read-only ingestion and recovery**
  (depends on T033, T071)
  - Outcome: backfill reaches a safe head before decisions, then follows without gaps.
  - Deliverables: backfill→catch-up→follow state machine, polling/subscription adapter,
    watermarks, reconnect/common-ancestor recovery, decision pause/resume, health state.
  - Acceptance: disconnect, dropped/duplicate/out-of-order notifications, restart,
    provider switch, shallow/deep reorg and head-stall tests create neither gaps nor
    duplicate decisions; decisions resume only after completeness/freshness gates pass.
  - Must not: depend on remote filter survival or make decisions while catching up.
  - References: R9, R11.

## Phase 8 — Operations, observability, and release evidence

**Purpose:** make silent degradation impossible and package a reproducible paper-only
release.

**Entry:** Phase 7 behavior passes locally.

**Exit gate:** a clean operator can deploy paper mode, observe injected faults,
restore from backup, and reproduce the release evidence without privileged context.

**Phase prohibitions:** no credential/cardinality leaks in telemetry, unauthenticated
control endpoints, mutable audit history, or untested restore procedure.

- [ ] **T080 — Add structured observability and SLOs** (depends on T072)
  - Outcome: freshness, correctness and safety failures alert before silent decisions.
  - Deliverables: redacted structured logs, metrics/traces for head lag, coverage gaps,
    RPC errors/range size, reorgs, reconciliation, risk rejects, ledger drift, and
    decision latency; SLOs and runbooks with severity/owner.
  - Acceptance: fault injection proves each critical alert and recovery signal; labels
    are bounded; logs pass automated secret/URL redaction tests.
  - Must not: log raw config/environment, token authorization headers, or use alert
    absence as evidence of correctness.

- [ ] **T081 — Add operational controls, backup, and restore** (depends on T071, T080)
  - Outcome: operators can halt safely and recover verifiably.
  - Deliverables: authenticated paper kill switch, graceful drain, immutable snapshots,
    backup/retention/checksum policy, restore and disaster-recovery runbooks.
  - Acceptance: restore into an empty environment reproduces checkpoints/ledger/report
    checksums; corrupt/partial backup fails; kill switch blocks new intents while
    retaining ingestion and audit evidence.
  - Must not: delete the only copy during retention or make recovery depend on a
    developer's local machine.

- [ ] **T082 — Run shadow and soak validation** (depends on T072, T080, T081)
  - Outcome: paper pipeline is stable over an agreed observation window.
  - Deliverables: predeclared duration/pools/SLOs, daily completeness/reconciliation,
    incident log, resource profile, restart/provider-failover drills, final report.
  - Acceptance: zero unexplained gaps/ledger drift/safety bypass; every SLO breach has
    disposition and rerun evidence; duration is chosen before results are viewed.
  - Must not: reset evidence to hide an incident or shorten the window after failure.

- [ ] **T083 — Produce the paper-only release dossier** (depends on T064, T082)
  - Outcome: a reviewer can decide release readiness from evidence, not claims.
  - Deliverables: versioned install/runbook, architecture/threat model, supported-pool
    matrix, dataset/experiment/soak manifests, test/security scan results, known limits,
    rollback and explicit live-execution exclusion.
  - Acceptance: clean-room reproduction succeeds; every checkbox links to evidence;
    unresolved critical/high issues block release and medium issues have owner/date.
  - Must not: call the release production/live ready or omit losing backtests/incidents.

## Phase 9 — Future live-readiness decision (planning only)

No implementation task in this phase is authorized by the current plan.

- [ ] **T090 — Make an explicit go/no-go decision for a separate live project**
  (depends on T083 and explicit user authorization)
  - Outcome: decide whether to draft a new, independently reviewed live-execution plan.
  - Required evidence: sustained paper SLOs, independent protocol/hook and application
    security reviews, key custody/rotation, transaction simulation and allowance design,
    nonce/replacement/reorg/MEV controls, capital caps, incident response, legal/
    operational approval, testnet drills, and named human release authority.
  - Acceptance: signed decision records scope, residual risks and approvers. A “go”
    authorizes planning only, not a transaction or live code change.
  - Must not: add signer/broadcaster/deployer code under T090 or infer consent from
    completion of paper mode.

## 5. Reference catalogue

Primary/canonical sources take precedence. Pin repository commits in implementation;
do not pin the mutable URLs below as if they were versions.

- **R1 — Robinhood Chain facts:** [Connecting to Robinhood Chain](https://docs.robinhood.com/chain/connecting/)
  and [running a node](https://docs.robinhood.com/chain/run-a-full-node/). Use for
  candidate network facts only; T024 must verify them through RPC.
- **R2 — Uniswap deployment facts:** [official V4 deployments](https://developers.uniswap.org/docs/protocols/v4/deployments)
  and [unified deployments](https://developers.uniswap.org/deployments). Absence of a
  chain is not permission to guess an address.
- **R3 — Protocol source:** [Uniswap v4-core](https://github.com/Uniswap/v4-core).
  Select and record an immutable commit/tag.
- **R4 — Events and behavior:** [`IPoolManager.sol`](https://github.com/Uniswap/v4-core/blob/main/src/interfaces/IPoolManager.sol)
  and [`PoolManager.sol`](https://github.com/Uniswap/v4-core/blob/main/src/PoolManager.sol).
- **R5 — Identity:** [`PoolKey.sol`](https://github.com/Uniswap/v4-core/blob/main/src/types/PoolKey.sol)
  and [`PoolId.sol`](https://github.com/Uniswap/v4-core/blob/main/src/types/PoolId.sol).
- **R6 — Hook flags/callbacks:** [`Hooks.sol`](https://github.com/Uniswap/v4-core/blob/main/src/libraries/Hooks.sol).
- **R7 — Independent state reads:** [official `StateView.sol`](https://github.com/Uniswap/v4-periphery/blob/main/src/lens/StateView.sol)
  and [`IStateView.sol`](https://github.com/Uniswap/v4-periphery/blob/main/src/interfaces/IStateView.sol).
- **R8 — SDK comparator:** [fetching V4 pool data](https://developers.uniswap.org/docs/sdks/v4/guides/pool-data)
  and [V4 position minting/math usage](https://developers.uniswap.org/docs/sdks/v4/guides/managing-liquidity/position-minting).
- **R9 — RPC contract:** [Ethereum JSON-RPC specification](https://ethereum.org/developers/docs/apis/json-rpc/).
- **R10 — Block-hash log queries/reorg rationale:** [EIP-234](https://eips.ethereum.org/EIPS/eip-234).
- **R11 — Python transport/scanning:** [web3.py internals and retry behavior](https://web3py.readthedocs.io/en/stable/internals.html)
  and [stateful event scanner example](https://web3py.readthedocs.io/en/stable/filters.html).
- **R12 — Existing V4 index:** [Uniswap V4 Subgraph queries](https://developers.uniswap.org/docs/ecosystem/subgraphs/concepts/v4/queries).
- **R13 — Indexer alternative:** [The Graph Subgraphs](https://thegraph.com/docs/en/subgraphs/overview/)
  and [manifest/start-block/history controls](https://thegraph.com/docs/en/subgraphs/developing/creating/subgraph-manifest/).
- **R14 — V4 design:** [Uniswap V4 whitepaper](https://raw.githubusercontent.com/Uniswap/v4-core/main/docs/whitepaper/whitepaper-v4.pdf).
- **R15 — Concentrated-liquidity math/economics:** [Uniswap V3 whitepaper](https://app.uniswap.org/whitepaper-v3.pdf).
- **R16 — LP opportunity cost:** [Automated Market Making and Loss-Versus-Rebalancing](https://arxiv.org/abs/2208.06046).
- **R17 — Event-driven design comparator:** [NautilusTrader architecture](https://nautilustrader.io/docs/latest/concepts/overview/)
  and [backtest execution ordering](https://nautilustrader.io/docs/latest/concepts/backtesting/execution-flow/).
- **R18 — Backtest reality/time frontier:** [QuantConnect live reconciliation and
  look-ahead warnings](https://www.quantconnect.com/docs/v1/live-trading/live-reconciliation).
- **R19 — Vectorized research comparator:** [VectorBT](https://vectorbt.dev/) and
  [portfolio cost/slippage model](https://vectorbt.dev/api/portfolio/base/).
- **R20 — Third-party LP comparators (non-authoritative):** [Aloe V3 simulator](https://github.com/aloelabs/uniswap-simulator)
  and [Gamma/Synthrio strategy framework](https://github.com/Synthrio/synths-strategy-testing-framework).
- **R21 — Dependency security:** [OSV-Scanner](https://google.github.io/osv-scanner/)
  and [GitHub dependency review](https://docs.github.com/en/code-security/concepts/supply-chain-security/dependency-review).
- **R22 — Property-based testing:** [Hypothesis documentation](https://hypothesis.readthedocs.io/).

## 6. Open decisions that block implementation, not research

Resolve these in T000/T024 rather than guessing in later tasks:

- canonical Robinhood Chain V4 PoolManager/StateView deployment, deployment block,
  runtime code hash, and source commit compatibility;
- which Robinhood environment is the first integration target;
- verified hook source and semantics for any FLYBRAIN/USDG candidate pool;
- archive/history limits and finality behavior of at least two usable RPC endpoints;
- storage/query engine and expected data volume/retention;
- quote-currency observation source and availability-time semantics;
- quantitative paper-mode SLOs and soak duration;
- license compatibility for copied/ported protocol artifacts and third-party vectors.
