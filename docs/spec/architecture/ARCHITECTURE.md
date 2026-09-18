# Architecture

> Status: living document. Owned by Phase 0 / T000. Mutated only via ADR or
> reviewable pull request. Does not ship executable code.

This document records the architectural choices for the Robinhood Chain /
Uniswap V4 LP V1 system from research through gated mainnet execution. It is the single entry
point for cross-cutting decisions; per-decision rationale, alternatives, and
migration triggers live in the ADR catalogue under `docs/spec/architecture/adr/`.

## 1. Goals and non-goals

V1 scope (binding; see `docs/intent/PROJECT_GOALS.md`):

- a single Robinhood Chain environment (testnet for safety drills,
  mainnet for approved real work);
- a single user-selected target token identified by Robinhood Chain
  contract address, with explicit user approval recorded;
- a single active V4 `PoolKey` that pairs the target token.

Goals (binding for the technical framework):

- reproducible research, paper operation and gated mainnet execution for that one active
  V1 pool on Robinhood Chain;
- byte-equivalent replay, valuation, and backtest results across storage
  chunking, ingestion order, and host platform;
- hard separation between data, protocol math, features, strategy, risk,
  execution, and presentation concerns;
- explicit support-level classification per `(chain, PoolKey)` with auditable
  promotion and demotion (the five levels are still defined for forward
  compatibility, but only Robinhood Chain can be promoted above
  `rejected` in V1 — see ADR-005);
- live signing material inside the main application, generic arbitrary-call signing, or
  mainnet enablement that bypasses the promotion gates;

Non-goals (binding for V1):

- multi-chain operation or switching to other EVM chains;
- multi-pool *execution* or automatic rebalancing across pools. A multi-pool
  *research* universe is in scope under ADR-014 and is disjoint from execution: it
  holds no assets, produces no transactions and grants no authority;
- automatic selection or switching of the target token;
- profitability of any concrete LP strategy;
- smart-contract deployment or custom hook authoring;
- bypassing the V1 promotion gates (backtest → testnet → post-testnet paper/shadow →
  security review → human promotion) for live execution;
- bypassing G-SIGNER-01: signing material in the main V1 process;
- dashboards, queues, and distributed workers beyond measured need.

## 2. Layered model

Each planned module has one primary layer. Imports follow ADR-006; runtime composition is
shown below and does not grant one component permission to import or control another.

```
presentation / reports
          |
application / backtest orchestration
    |          |          |
 strategy     risk     execution ----> isolated signer
    |          |          |
    +----------+----------+
               |
            features
               |
        reconstruction
          /          \
   RPC adapter    storage adapter
          \          /
          protocol / domain contracts
```

### 2.1 Layer responsibilities

| Layer | Owns | May not |
| --- | --- | --- |
| protocol/domain | ChainId, Address, Currency, PoolKey, PoolId, BlockRef, TxRef, EventKey; tick/sqrtPriceX96/liquidity/amount math | import RPC, storage, web, IO, time, random |
| rpc adapter | bounded `eth_getLogs`, block/header/receipt/block-pinned `eth_call`, retries, failover, range splitting | expose arbitrary RPC, sign or send |
| storage | append-only raw/normalized partitions, manifests, checkpoints, readers | call future state, mutate committed partitions, lossy coercion |
| reconstruction | deterministic event replay, tick-liquidity state, replay-vs-chain comparison | read `latest`, use wall clock, infer hook effects |
| features | time/block bars, market features, LP position valuation, quote/gas observation, PnL attribution | look ahead, centered windows, hide residual PnL |
| strategy | pure observation→intent functions, deterministic clock/seed | call RPC, storage, signing, or execution; mutate ledger |
| backtest / research | event-driven engine, baseline strategies, manifests, robustness analysis | use future data, retune on held-out data |
| risk | centralized approve/reject gateway with reason codes | be bypassed by execution, strategy, or manual override |
| execution | paper intent→fill→ledger; deterministic V4 planner; separately packaged live executor and isolated signer (G-SIGNER-01) | let strategy bypass risk/planning, hold signing material outside signer, or expose arbitrary calls |
| application / orchestration | ingestion workflows and strategy → risk → execution wiring through ports | move policy into adapters or bypass a component's public contract |
| presentation / reports | structured reports, charts, dossiers, release evidence | mutate upstream state, leak credentials |

### 2.2 Module-to-layer mapping

| Phase | Task | Module (planned) | Layer |
| --- | --- | --- | --- |
| 1 | T010 | `robinhood_lp.protocol.ids` | protocol/domain |
| 1 | T011 | `robinhood_lp.protocol.events` | protocol/domain |
| 1 | T012 | `robinhood_lp.protocol.math` | protocol/domain |
| 1 | T013 | `robinhood_lp.protocol.vectors` (oracle harness) | protocol/domain |
| 2 | T020 | `robinhood_lp.rpc.adapter` | rpc adapter |
| 2 | T021 | `robinhood_lp.protocol.abi` (pinned artifacts) | protocol/domain |
| 2 | T022 | `robinhood_lp.discovery.registry` | storage |
| 2 | T023 | `robinhood_lp.discovery.eligibility` | storage |
| 2 | T024 | `robinhood_lp.discovery.chain_capability` | rpc adapter |
| 2 | T025 | `robinhood_lp.admission.lifecycle` | risk |
| 3 | T030 | `robinhood_lp.storage.schema` | storage |
| 3 | T031 | `robinhood_lp.storage.raw` | storage |
| 3 | T032 | `robinhood_lp.application.ingest` | application / orchestration |
| 3 | T033 | `robinhood_lp.storage.reorg` | storage |
| 3 | T034 | `robinhood_lp.storage.quality` | storage |
| 4 | T040 | `robinhood_lp.replay.engine` | reconstruction |
| 4 | T041 | `robinhood_lp.replay.ticks` | reconstruction |
| 4 | T042 | `robinhood_lp.replay.validate` | reconstruction |
| 4 | T043 | `robinhood_lp.replay.evidence` (hook semantics) | reconstruction |
| 5 | T050 | `robinhood_lp.features.bars` | features |
| 5 | T051 | `robinhood_lp.features.position` | features |
| 5 | T052 | `robinhood_lp.features.attribution` | features |
| 5 | T053 | `robinhood_lp.features.quote` | features |
| 6 | T060 | `robinhood_lp.strategy.base` | strategy |
| 6 | T061 | `robinhood_lp.backtest.engine` | backtest |
| 6 | T062 | `robinhood_lp.backtest.baselines` | strategy |
| 6 | T063 | `robinhood_lp.backtest.manifest` | backtest |
| 6 | T064 | `robinhood_lp.backtest.robustness` | backtest |
| 6 | T065 | `robinhood_lp.strategy.usdg_range` | strategy |
| 6 | T066 | `robinhood_lp.backtest.threshold_review` | backtest / research |
| 7 | T070 | `robinhood_lp.risk.checks` | risk |
| 7 | T071 | `robinhood_lp.execution.paper` | execution |
| 7 | T072 | `robinhood_lp.application.realtime` | application / orchestration |
| 8 | T080 | `robinhood_lp.ops.observability` | presentation / ops |
| 8 | T081 | `robinhood_lp.ops.controls` | presentation / ops |
| 8 | T082 | `robinhood_lp.ops.soak` | presentation / ops |
| 8 | T083 | `robinhood_lp.ops.dossier` | presentation / ops |
| 8 | T084–T086 | `robinhood_lp.web` | presentation / controls |
| 9 | T090 | separately packaged signer service | isolated signing boundary |
| 9 | T091 | V4 transaction planner | execution |
| 9 | T092 | testnet executor and evidence | isolated execution service |
| 9 | T093–T094 | review and promotion evidence | operations / controls |
| 9 | T095 | mainnet canary execution | isolated execution service |
| 9 | T096 | final V1 traceability dossier | presentation / ops |
| 3 | T039 | per-pool extended-history acquisition | application / orchestration |
| 10 | T100 | `robinhood_lp.research.dataset` | storage |
| 10 | T101 | `robinhood_lp.research.panel` (labels, splits, training harness) | backtest / research |
| 10 | T102 | `robinhood_lp.research.models` (model-backed strategy components) | strategy |
| 10 | T103 | `robinhood_lp.web` research pages | presentation / controls |

## 3. Cross-cutting policies

The following are enforced by every layer and audited by the Definition of
Done in `todo/README.md`.

- **Precision.** On-chain integers remain integers from RPC through protocol
  accounting. Conversion to `Decimal` or display units happens only at an
  explicitly named boundary; `float` is forbidden in protocol, replay,
  features, valuation, and accounting code paths. See ADR-004. Statistical and
  machine-learning code is the one additional exception, and only inside a
  boundary that its own module names and documents: model inputs and outputs stay
  typed, unit-carrying and integer-exact at the edge, and no float value produced
  there may reach a protocol, accounting or valuation path (ADR-014).
- **Determinism.** Replay, backtest, and paper runs must produce
  byte-equivalent canonical outputs across input chunking and storage order
  after excluding declared observational fields (e.g. ingestion wall time).
  No wall clock or unseeded randomness inside deterministic paths.
- **Fail closed.** Missing data, unknown hooks, unsupported RPC behavior,
  address mismatch, or reconciliation failure must reject the operation
  rather than fall back to a default.
- **No silent secrets.** Service credentials come from environment/deployment-secret
  injection. The signer alone reads an encrypted Keystore and accepts its password via a
  non-echoing terminal prompt. Serialization, logs, reports, and audit records never
  include credential URLs, private keys, passwords, seeds, or `.env` values.
- **Layer purity.** A layer may not import symbols from a layer above it or
  from a sibling layer that would create a cycle. CI enforces this.

## 4. ADR catalogue

Decisions live under `docs/spec/architecture/adr/`. Each ADR is immutable once accepted;
superseding decisions are new ADRs that explicitly reference the prior one.

| ID | Title | Status |
| --- | --- | --- |
| ADR-001 | Web3 client & concurrency model | superseded by ADR-011 |
| ADR-002 | Storage & query format | accepted |
| ADR-003 | Configuration & secrets handling | accepted |
| ADR-004 | Integer / decimal precision policy | accepted |
| ADR-005 | Supported-chain lifecycle (support levels) | accepted |
| ADR-006 | Dependency direction between layers | accepted |
| ADR-007 | Continuous integration provider | accepted |
| ADR-008 | Binding document precedence | accepted |
| ADR-009 | Display / Decimal boundary | accepted |
| ADR-010 | Free dual-provider historical ingestion | accepted |
| ADR-011 | Project-owned bounded JSON-RPC transport | accepted |
| ADR-012 | Block-header time persistence and acquisition call volume | accepted (addendum to ADR-002 / ADR-010) |
| ADR-013 | Finalized window pinning and partition reconciliation | accepted (addendum to ADR-002 / ADR-010 / ADR-012); window rule replaced by ADR-015 |
| ADR-014 | Research universe, numeraire hierarchy, and the execution boundary | accepted (bounds ADR-005 and ADR-004 for the research scope) |
| ADR-015 | Per-pool extended-history research window | accepted (addendum to ADR-002 / ADR-010 / ADR-011 / ADR-012; replaces ADR-013's window rule) |

## 5. Open decisions (not blocking research)

These are tracked in `todo/README.md` §6 and resolved by the named task before its consumer runs.

Two product choices are already closed: both 5-minute rules use T053-qualified,
point-in-time USDG prices; pre-testnet preliminary paper validates implementation only,
while formal live evidence requires post-testnet paper/shadow.

- deployment/finality/archive/range behavior is capability-probed per endpoint and
  re-probed for each material ingestion configuration (T024, ADR-010, T032);
- verified hook source and semantics for the user-selected PoolKey
  (T023, T043);
- storage engine and expected data volume/retention (refined in ADR-002 as
  evidence arrives);
- qualified USDG quote source and availability-time semantics (T053);
- quantitative paper-mode SLOs and soak duration (T082);
- local Web authentication/session implementation (T084, T085);
- final economic/risk thresholds, selected from post-testnet paper/shadow evidence in T093.
