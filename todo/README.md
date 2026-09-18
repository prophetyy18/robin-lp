# V1 implementation workflow

This is the executable delivery plan for the Robinhood Chain / Uniswap V4 LP
V1 system, from research through gated mainnet execution. Implement **one numbered task at a time**.
A phase is a release gate, not a prompt and not a unit of implementation.

Operational commands, role boundaries and worktree lifecycle are defined in
[`WORKFLOW.md`](WORKFLOW.md). Current progress exists only in [`config.yaml`](config.yaml).

Last plan review: **2026-09-14**. Network and deployment facts are mutable;
the links in this file are evidence sources, not values that may be copied into
code without the verification required by T024.

## 1. Product boundary and final delivery standard

V1 is a single-owner system for Robinhood Chain. It accepts one target-token contract,
discovers every V4 PoolKey containing that token, and lets the user approve exactly one
active PoolKey at a time. It delivers reproducible research, preliminary and formal paper
operation, an authenticated Web console, and gated mainnet LP/required-Swap execution.
It is not a FLYBRAIN/USDG-specific bot, does not support other chains, does not choose or
switch the target token automatically, and does not run simultaneous multi-pool strategies.

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
- a user can enter and approve one target-token contract, verify the Robinhood Chain
  deployment, discover all matching pools, select one active PoolKey, ingest a fixed historical
  range, resume after interruption, and produce a completeness report;
- replayed pool state matches independent block-pinned on-chain reads for the
  declared support class;
- at least two materially different pool fixtures pass end-to-end (different
  tokens/decimals/fees/tick spacing/hooks; one must use native currency or a
  nonzero hook);
- a saved experiment manifest reproduces the same decisions, ledger, and metrics;
- preliminary paper mode restarts without gaps, duplicated decisions, or balance drift;
- the Web console supports discovery, evidence review, pool selection, strategy comparison,
  paper operation, approvals, audit and emergency control without exposing secrets;
- unsupported hook semantics, stale/incomplete data, failed reconciliation, and
  breached risk limits fail closed;
- the isolated signer, deterministic planner and executor pass testnet and post-testnet
  paper/shadow evidence gates;
- a human-approved, smallest-cap mainnet canary completes and reconciles one bounded LP
  lifecycle without operating outside its chain, PoolKey, strategy, permission or capital scope.

Performance profitability is **not** a delivery criterion. Correct negative or
unprofitable results are valid; fabricated completeness or optimistic accounting
is not.

## 2. Task contract: required for every numbered task

Before editing:

1. Read `CLAUDE.md`, `docs/intent/PROJECT_GOALS.md`, this file, the task's
   references, and all affected files.
2. Restate the task outcome, dependencies, inputs, outputs, and acceptance checks.
3. Verify every dependency status and phase entry gate. If one is unmet, stop
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

Before requesting independent review:

1. Run task-specific normal, boundary, invalid-input, and failure-injection tests.
2. Run the full unit suite plus formatter, linter, and strict type checker.
3. Run integration/golden/fork tests required by the task; tests skipped because
   credentials are absent do not satisfy an acceptance criterion.
4. Inspect `git diff`; do not include unrelated changes or generated secrets.
5. Return commands, results, assumptions, data/block versions and residual risks to
   the workflow controller. Do not edit task state or review records.

`todo/config.yaml` is the only machine-readable progress source. `APPROVED` means every
dependency, phase entry condition and acceptance item has current evidence from an independent
review of the recorded candidate commit. Code may exist while a task remains `PLANNED`, `READY`
or `CHANGES_REQUESTED`; downstream code does not retroactively complete an unmet dependency.

### Workflow state machine

`PLANNED → READY → IN_DEVELOPMENT → AWAITING_REVIEW` is the normal forward path.
An unfinished Developer may checkpoint as `CONTINUATION_REQUIRED` without leaving
`IN_DEVELOPMENT`; the controller then starts one fresh Developer in the same
attempt and retained worktree.
Review produces `APPROVED`, `CHANGES_REQUESTED` or `BLOCKED`. Exceptional evidence may produce
`TRIAGE_REQUIRED`; a fresh read-only triager routes it to implementation repair, planning, an
owner decision, or an external blocker. Planning changes pass through `PLANNING →
AWAITING_PLAN_REVIEW` and return to `CHANGES_REQUESTED` for a fresh Developer. A supported
`NO_CHANGE_REQUIRED` planning result is valid: task completion depends on acceptance evidence,
not on manufacturing a file diff. Only the deterministic workflow controller may write progress
state. No Agent approves its own work.

If plan review is temporarily unavailable, `PLAN_REVIEW_BLOCKED` retains the exact
plan candidate so a fresh Plan Reviewer can retry without forcing the Planner to
rewrite an unchanged plan.

After an approved task, the Manager explicitly chooses one dependency-complete
`PLANNED` task and runs `ready <task>`. The controller never guesses among
multiple candidates and never activates more than one task.

An explicit Owner amendment is separate from task execution. It may update one
or more still-`PLANNED` contracts, and—when declared—related Spec or Intent text,
through an independent planning review. Passing that review merges only the
planning amendment; target tasks remain `PLANNED` with their implementation
attempt counters unchanged.

A `SUPERSEDE`-layer amendment retires work that is already `APPROVED` instead of
planning work that is not. It records the successor in the target's
`superseded_by` field and may change nothing else — status, attempts, commits,
evidence pointers and review records stay byte-identical, so an approval keeps
describing the exact candidate it reviewed. Retirement is therefore an
annotation on the history, not a rewrite of it, and "which approved work is
still live" is the set of `APPROVED` tasks whose `superseded_by` is null.

Every handoff binds the task contract, base commit and candidate commit. Review of uncommitted
files is invalid. A repaired implementation always receives a new candidate commit and a fresh
Reviewer context.

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

During **Phases 0–8**, do not:

- sign, simulate signing, submit, bundle, relay, or deploy a live transaction;
- add a private-key/seed loader or keep signing material in the main application process;
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

Phase 9 permits only the signing and broadcast surfaces named in T090–T095. The private key
must remain inside the isolated signer process, encrypted at rest, unlocked only by hidden
interactive terminal input, and bound to the current approved request. No task authorizes an
arbitrary-call signer, Web password input, automatic external transfer, or live execution before T094.

## 3. Support levels and promotion rules

Each `(chain, PoolKey)` has one explicit level; support never propagates just
because another pool shares tokens or bytecode.

| Level | Allowed | Minimum evidence | Explicitly forbidden |
| --- | --- | --- | --- |
| `rejected` | retain reason only | invalid identity/deployment/data evidence | ingestion, simulation, paper |
| `ingestion` | raw/normalized history and quality reports | verified deployment and ABI | valuation, strategy, paper |
| `backtest` | deterministic replay/valuation/backtest | replay reconciliation and modeled hook effects | paper decisions |
| `paper` | real-time decisions and simulated ledger | recovery, freshness, risk and shadow reconciliation gates | signed/broadcast transactions |
| `live` | bounded Robinhood mainnet LP and required Swap execution | backtest, testnet, post-testnet paper/shadow, security review and explicit human promotion | any action outside the approved identity, strategy, permission and limit bindings |

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

## Phase index

- [P00 — Decisions, safety boundary, and engineering baseline](phases/P00-engineering-baseline/README.md)
- [P01 — Canonical protocol model and deterministic math](phases/P01-protocol-foundation/README.md)
- [P02 — Verified chain access and pool discovery](phases/P02-chain-access-and-discovery/README.md)
- [P03 — Versioned historical ingestion and audit storage](phases/P03-ingestion-and-storage/README.md)
- [P04 — Deterministic state reconstruction and hook evidence](phases/P04-state-reconstruction/README.md)
- [P05 — Point-in-time features, position valuation, and attribution](phases/P05-features-and-valuation/README.md)
- [P06 — Event-driven backtesting and research protocol](phases/P06-backtesting-and-strategy/README.md)
- [P07 — Central risk and paper execution](phases/P07-risk-and-paper/README.md)
- [P08 — Operations, observability, and release evidence](phases/P08-operations-and-web/README.md)
- [P09 — Isolated signer, testnet proof and gated mainnet execution](phases/P09-signer-testnet-and-live/README.md)
- [P10 — Research universe, datasets, and the model laboratory](phases/P10-research-and-models/README.md)

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
- **R23 — Robinhood Chain endpoints:** [official connection documentation](https://docs.robinhood.com/chain/connecting/)
  for the public mainnet RPC, its rate-limited/non-production status, and the recommendation
  to use an archive provider for historical reads.
- **R24 — Alchemy Robinhood `eth_getLogs`:** [official method documentation](https://www.alchemy.com/docs/chains/robinhood-chain/robinhood-chain-api-endpoints/eth-get-logs)
  for the 10-block Free-tier range and per-call CU value; runtime probes remain authoritative
  for the configured endpoint.
- **R25 — Alchemy plan limits:** [official pricing documentation](https://www.alchemy.com/docs/reference/pricing-plans)
  for the current account-wide Free CU allowance and archive-data inclusion; runtime usage
  and the operator's remaining allowance remain authoritative.

## 6. Unresolved decisions and assigned resolution points

Do not guess these in an earlier task:

- T001 selects and locks the reproducible Python dependency set and retains clean-install
  evidence; later task code may exist but cannot be verified before this closes.
- T021 verifies exact core/periphery compatibility, StateView ABI coverage, artifact checksums
  and reproducible selector/topic generation.
- T024 verifies Robinhood mainnet/testnet chain identity, deployment blocks/transactions,
  runtime code hashes, archive/finality/range behavior and endpoint agreement. No address or
  chain behavior is accepted merely because it appears in a documentation artifact.
- T023/T043 determine the selected PoolKey's actual Hook identity and semantics.
- T031 measures data volume and confirms storage/partition/retention parameters under ADR-002;
  T032 implements ADR-010 capability-driven A+B routing and records preflight versus actual
  calls, HTTP requests, provider units, bytes, rows, and elapsed time. A first dataset starts
  at the selected pool's Initialize block; only a qualified local checkpoint permits a later
  run to ingest a short suffix without replaying the complete prefix.
- T053 selects and versions qualified USDG quote sources and availability-time semantics. The
  confirmed 5-minute rules use this point-in-time USDG price, not raw activity-pool relative
  price alone.
- T066/T070 implement the parameter, evidence and enforcement mechanisms. Before paper trading,
  values other than the confirmed 5-minute rules remain explicit experimental fixtures or
  provisional versions and do not become live defaults.
- T082 proposes and measures preliminary-paper SLOs without granting live qualification.
- T084/T085 select the local Web authentication/session implementation while preserving
  `WEB_CONSOLE.md` behavior and security requirements.
- T093 uses post-testnet paper/shadow evidence to finalize all deferred economic, loss,
  drawdown, liquidity, Range, SLO and promotion thresholds before T094.
- Preliminary paper may run before testnet for implementation validation. Formal live evidence
  remains ordered as backtest → testnet → post-testnet paper/shadow → security review
  → explicit human promotion.
