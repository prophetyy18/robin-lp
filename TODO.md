# Development TODO

This is the execution plan for Claude Code CLI (using MiniMax as the model).
Implement one numbered task at a time. Do not treat a phase as one prompt.

## Working protocol for every task

Before editing:

1. Read `CLAUDE.md`, this file, and all files related to the task.
2. Restate the selected task, its dependencies, and acceptance criteria.
3. Confirm that dependencies below are complete; otherwise stop and report them.

While editing:

1. Make the smallest change that completes only the selected task.
2. Keep data, domain, strategy, risk, backtest, execution, and storage concerns separate.
3. Do not add live transaction submission unless a future task explicitly requests it.
4. Never invent chain IDs, contract addresses, ABIs, RPC behavior, or hook behavior.
5. Pin every external ABI or protocol artifact to a documented source/version.

Before finishing:

1. Run the task's tests and the full test suite when practical.
2. Run formatting, linting, and type checking.
3. Inspect `git diff` and exclude unrelated changes.
4. Update only the checkbox for work that actually passes its acceptance criteria.
5. Report commands run, results, assumptions, and remaining risks.

## Product boundary

The initial product is a research and paper-trading framework for **any Uniswap
V4 pool on a configured EVM chain**, not a FLYBRAIN/USDG-specific application.

A pool is identified by the chain and canonical V4 `PoolKey` fields:

- `currency0`
- `currency1`
- `fee` (including dynamic-fee pools)
- `tickSpacing`
- `hooks`

The derived `PoolId` must be validated against the canonical Uniswap V4
implementation. Token symbols and names are display metadata and must never be
used as identifiers.

V4 hooks can change pool behavior. The generic engine must preserve hook
identity and observed on-chain results. It must not assume a hook behaves like a
plain pool. Unknown or unsupported hook behavior must make a pool ineligible for
simulation/execution rather than silently applying standard-pool assumptions.

## Definition of Done

Unless a task says otherwise, it is complete only when:

- public interfaces and non-obvious financial formulas are documented;
- unit tests cover normal, boundary, and invalid inputs;
- `pytest`, formatter, linter, and type checker pass;
- no secret, private key, RPC credential, or `.env` value is logged or committed;
- integer on-chain values remain integers until an explicit display/analysis boundary;
- errors contain chain, pool, block range, and retry context where applicable;
- the change works for at least two fixtures with different tokens, decimals,
  fees, tick spacing, and hook addresses.

---

## Phase 0 — Decisions and engineering baseline

- [ ] **T000 — Record initial architecture decisions**
  - Add `docs/architecture.md` and ADRs for Python/Web3 client, storage format,
    configuration approach, precision policy, and supported-chain policy.
  - Define support levels: `ingestion`, `backtest`, `paper`, `live`.
  - Acceptance: each decision lists context, choice, alternatives, and tradeoffs.

- [ ] **T001 — Create the Python 3.12 project skeleton** (depends on T000)
  - Add `pyproject.toml`, `src/robinhood_lp/`, and `tests/`.
  - Configure pytest, Ruff, and a strict type checker.
  - Add only dependencies required by the first implemented task.
  - Acceptance: clean environment installation and one smoke test pass.

- [ ] **T002 — Add safe typed configuration** (depends on T001)
  - Define chain configuration: chain ID, RPC environment-variable name,
    confirmations, start block, `PoolManager` address, request limits.
  - Define pool configuration using complete `PoolKey`, never token symbols.
  - Reject unknown fields and invalid/mismatched addresses and chain IDs.
  - Add `.env.example` containing names/placeholders only.
  - Acceptance: valid multi-chain/multi-pool fixtures load; secrets never appear
    in serialized config or logs.

- [ ] **T003 — Establish CI and quality gates** (depends on T001)
  - Run tests, lint, formatting check, and type checking.
  - Add secret scanning and dependency vulnerability checking if supported by CI.
  - Acceptance: CI passes on the baseline and fails on intentional test/lint errors.

## Phase 1 — Protocol model and deterministic math

- [ ] **T010 — Model canonical protocol identifiers** (depends on T001)
  - Add value objects for `ChainId`, `Address`, `Currency`, `PoolKey`, and `PoolId`.
  - Enforce currency ordering and V4 fee/tick-spacing constraints.
  - Implement canonical PoolId derivation from PoolKey.
  - Acceptance: results match pinned vectors produced by canonical V4 code,
    including native currency, static fee, dynamic fee, and nonzero hook cases.

- [ ] **T011 — Model tokens, blocks, and raw event identity** (depends on T010)
  - Token metadata includes address/native identity and decimals; symbols optional.
  - Event identity includes chain ID, transaction hash, log index, block hash,
    block number, and removed/canonical status.
  - Acceptance: identifiers serialize deterministically and cannot collide across chains.

- [ ] **T012 — Port and validate V4 price/tick/liquidity math** (depends on T010)
  - Implement conversions among tick, `sqrtPriceX96`, raw token amounts, display
    prices, liquidity, and bounded position amounts.
  - Preserve Solidity rounding direction and integer bounds.
  - Acceptance: property and golden-vector tests agree with pinned canonical
    Solidity implementations at minimum/maximum ticks and decimal combinations.

## Phase 2 — Chain access and pool discovery

- [ ] **T020 — Build a read-only RPC adapter** (depends on T002, T011)
  - Support bounded `eth_getLogs`, block/header reads, contract calls, retries,
    backoff, timeouts, endpoint failover, and provider-specific range splitting.
  - No signing or transaction submission APIs in this adapter.
  - Acceptance: deterministic mocked tests cover throttling, partial failure,
    duplicate logs, and retry exhaustion.

- [ ] **T021 — Pin canonical V4 ABI artifacts** (depends on T001)
  - Store the minimum required `PoolManager`/state-view ABI and provenance:
    repository, tag or commit, contract name, and checksum.
  - Add a script/test that detects accidental ABI drift.
  - Acceptance: event signatures and selectors match the pinned source.

- [ ] **T022 — Discover and register pools from Initialize events**
  (depends on T010, T020, T021)
  - Decode `Initialize` and reconstruct the complete PoolKey registry per chain.
  - Verify emitted/derived PoolId and fetch token metadata defensively.
  - Handle native currency and broken/nonstandard metadata contracts.
  - Acceptance: discovery is resumable, idempotent, and tested for multiple
    fee/tick/hook combinations.

- [ ] **T023 — Add explicit pool eligibility classification** (depends on T022)
  - Classify static/dynamic fee, no-hook/hooked, metadata completeness, data
    availability, and supported simulation semantics.
  - Default unknown hooks to `ingestion-only` until their economics are modeled.
  - Acceptance: every registered pool has a reasoned support level; there is no
    implicit fallback to plain-pool behavior.

## Phase 3 — Historical ingestion and storage

- [ ] **T030 — Define versioned raw and normalized schemas**
  (depends on T011, T021)
  - Cover blocks and V4 `Initialize`, `ModifyLiquidity`, `Swap`, and `Donate`.
  - Preserve raw log fields beside normalized typed fields.
  - Include schema version and ingestion timestamp.
  - Acceptance: round-trip serialization loses no information.

- [ ] **T031 — Implement append-only raw storage** (depends on T030)
  - Partition by chain, contract/event, and block range/date as justified in ADR.
  - Use atomic writes, checksums/manifests, and unique event keys.
  - Acceptance: rerunning a range is idempotent and interrupted writes recover safely.

- [ ] **T032 — Implement checkpointed historical ingestion**
  (depends on T020, T022, T031)
  - Ingest configured ranges in bounded chunks with durable checkpoints.
  - Track scanned-empty ranges explicitly.
  - Acceptance: stop/resume and overlapping runs yield the same canonical dataset.

- [ ] **T033 — Handle confirmations and reorgs** (depends on T032)
  - Keep a configurable unfinalized window and compare stored block hashes.
  - Mark/rewrite orphaned normalized data without destroying raw audit history.
  - Acceptance: synthetic shallow and deep reorg tests converge to the canonical chain.

- [ ] **T034 — Add data-quality reports** (depends on T032, T033)
  - Detect block gaps, duplicate/misordered logs, unknown pools, impossible values,
    stale RPC data, and metadata failures.
  - Acceptance: a range is never declared complete while unexplained gaps exist.

## Phase 4 — State reconstruction

- [ ] **T040 — Build deterministic event replay** (depends on T012, T030, T034)
  - Order by block, transaction index, and log index.
  - Reconstruct observable price, tick, active liquidity, volume, and liquidity
    deltas per pool without querying future state.
  - Acceptance: replay is deterministic across input chunking and storage order.

- [ ] **T041 — Reconstruct tick-liquidity state** (depends on T040)
  - Apply liquidity gross/net updates at tick boundaries.
  - Add invariants for nonnegative gross liquidity and valid tick spacing.
  - Acceptance: selected historical checkpoints match independent on-chain reads.

- [ ] **T042 — Validate replay against chain state** (depends on T041)
  - Sample multiple pools, hook types, block heights, and decimal combinations.
  - Produce a machine-readable discrepancy report with tolerances justified.
  - Acceptance: unexplained mismatches block later backtesting tasks.

## Phase 5 — Features and valuation

- [ ] **T050 — Add time/block bars and market features** (depends on T042)
  - Volume, realized volatility, price range, active liquidity, depth proxy,
    fee rate, gas, and data freshness.
  - No forward-looking windows.
  - Acceptance: tests prove window alignment and no look-ahead leakage.

- [ ] **T051 — Add LP position valuation** (depends on T012, T042)
  - Track token0/token1 inventory, in/out-of-range state, principal, and fees.
  - Separate raw token amounts from quote-currency valuation.
  - Acceptance: boundary ticks and price reversals match golden vectors.

- [ ] **T052 — Add benchmark and PnL attribution** (depends on T051)
  - Compare against hold and configurable rebalanced inventory benchmarks.
  - Attribute inventory PnL, LP fees, impermanent loss/divergence loss, gas,
    slippage, hook effects, and rebalance cost separately.
  - Acceptance: accounting identity reconciles within documented rounding tolerance.

## Phase 6 — Backtest engine and strategies

- [ ] **T060 — Define strategy contracts** (depends on T050, T051)
  - Strategy receives only timestamped market/portfolio state and emits a target
    position or no-op; it cannot call RPC, storage, signing, or execution.
  - Validate proposed ticks against pool tick spacing and bounds.
  - Acceptance: one strategy instance can operate on any eligible PoolKey.

- [ ] **T061 — Build the event-driven backtest engine** (depends on T052, T060)
  - Define decision timing, information availability, fill timing, fee accrual,
    latency, gas, and slippage explicitly.
  - Reuse domain objects intended for paper trading.
  - Acceptance: deterministic runs, no future data access, full audit trail.

- [ ] **T062 — Implement baseline strategies** (depends on T061)
  - Full-range where protocol-valid, fixed-width, volatility-width, and
    out-of-range rebalance policies.
  - Include hold/no-LP baseline.
  - Acceptance: strategies contain no pool-specific addresses or token assumptions.

- [ ] **T063 — Add experiment configuration and reports** (depends on T062)
  - Record dataset/version, chain, PoolKey, period, strategy parameters, code
    revision, seed, and cost assumptions.
  - Report total/annualized return, drawdown, turnover, time in range, fees,
    IL, gas, slippage, and benchmark excess return.
  - Acceptance: the same experiment can be reproduced from one saved manifest.

- [ ] **T064 — Add robustness analysis** (depends on T063)
  - Walk-forward/out-of-sample splits, parameter sensitivity, stressed gas and
    latency, missing-data scenarios, and pool-regime segmentation.
  - Acceptance: reports label in-sample and out-of-sample results prominently.

## Phase 7 — Risk and paper trading

- [ ] **T070 — Define centralized risk checks** (depends on T060)
  - Pool eligibility, hook support, data freshness, max capital, token exposure,
    price deviation, rebalance frequency/cost, drawdown, and global kill switch.
  - Acceptance: execution intent cannot bypass risk approval.

- [ ] **T071 — Implement paper execution and ledger** (depends on T061, T070)
  - Simulate intents, fills, liquidity changes, fee collection, gas, failures,
    and balances; store an immutable audit trail.
  - Acceptance: backtest and paper modes share strategy/risk interfaces and reconcile.

- [ ] **T072 — Add real-time ingestion and recovery** (depends on T033, T071)
  - Backfill first, then follow head; reconnect and fill gaps before resuming decisions.
  - Acceptance: disconnect/restart tests create neither gaps nor duplicate decisions.

- [ ] **T073 — Add observability and alerts** (depends on T072)
  - Structured metrics/logs for lag, gaps, RPC health, decisions, risk rejects,
    simulated PnL, and reconciliation errors.
  - Acceptance: secrets are redacted and critical alerts have runbooks.

## Phase 8 — Live execution (explicitly deferred)

- [ ] **T080 — Write a live-execution threat model and approval checklist**
  (depends on successful paper-trading evaluation)
  - Cover signer isolation, allowance risk, malicious hooks/tokens, simulation,
    slippage, MEV, nonce/replacement, chain reorg, RPC compromise, and emergency exit.
  - This task writes documents and tests only; it sends no transaction.

- [ ] **T081 — Design live adapter behind compile/runtime gates** (depends on T080)
  - Requires a separate explicit user decision and implementation plan.
  - PAPER remains the default under missing, invalid, or partial configuration.

## Deferred until evidence justifies them

- Smart-contract deployment or custom hook development.
- Multi-pool capital optimization and routing.
- Automated key custody or unattended live trading.
- Dashboards, databases, queues, and distributed workers beyond measured need.
- Pool-specific heuristics inside the generic protocol/domain layers.

## Suggested Claude Code invocation

Use a fresh or clearly scoped session per task. Example:

```text
Read CLAUDE.md and TODO.md. Implement only T001. First inspect the repository and
state T001's dependencies and acceptance criteria. Make the smallest compliant
change, add tests, run all required checks, inspect git diff, then report evidence.
Do not start T002 and do not mark T001 complete unless every acceptance criterion
passes.
```

For MiniMax or any non-Anthropic model behind Claude Code, do not rely on model
name-specific behavior. Keep prompts explicit, grant the minimum tool permissions,
use one task per run, and require command output as completion evidence.
