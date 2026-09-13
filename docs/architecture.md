# Architecture

> Status: living document. Owned by Phase 0 / T000. Mutated only via ADR or
> reviewable pull request. Does not ship executable code.

This document records the architectural choices for the Robinhood Chain /
Uniswap V4 LP research and paper-trading framework. It is the single entry
point for cross-cutting decisions; per-decision rationale, alternatives, and
migration triggers live in the ADR catalogue under `docs/adr/`.

## 1. Goals and non-goals

V1 scope (binding; see `docs/product/PROJECT_GOALS.md`):

- a single Robinhood Chain environment (testnet for safety drills,
  mainnet for approved real work);
- a single user-selected target token identified by Robinhood Chain
  contract address, with explicit user approval recorded;
- a single active V4 `PoolKey` that pairs the target token.

Goals (binding for the technical framework):

- reproducible research and paper-trading framework for that one V1 pool
  on Robinhood Chain;
- byte-equivalent replay, valuation, and backtest results across storage
  chunking, ingestion order, and host platform;
- hard separation between data, protocol math, features, strategy, risk,
  execution, and presentation concerns;
- explicit support-level classification per `(chain, PoolKey)` with auditable
  promotion and demotion (the five levels are still defined for forward
  compatibility, but only Robinhood Chain can be promoted above
  `rejected` in V1 — see ADR-005);
- no production signing, broadcast, or deploy path exists in the current
  release.

Non-goals (binding for V1):

- multi-chain operation or switching to other EVM chains;
- multi-pool operation or automatic rebalancing across pools;
- automatic selection or switching of the target token;
- profitability of any concrete LP strategy;
- smart-contract deployment or custom hook authoring;
- bypassing the V1 promotion gates (backtest → testnet → paper →
  security review → human promotion) for live execution;
- bypassing G-SIGNER-01: signing material in the main V1 process;
- dashboards, queues, and distributed workers beyond measured need.

## 2. Layered model

Each planned module lives in exactly one layer. A layer may depend only on
layers strictly below it.

```
                   ┌────────────────────────────────┐
                   │   presentation / reports       │  (Phase 5+, T050+)
                   └────────────────┬───────────────┘
                                    │
                ┌───────────────────┼───────────────────┐
                │                   │                   │
     ┌──────────▼────────┐ ┌─────────▼────────┐ ┌───────▼───────┐
     │    execution      │ │     strategy     │ │ backtest /    │
     │  (paper, live     │ │    (T060+)       │ │ research      │
     │   deferred)       │ │                  │ │ (T061+)       │
     └──────────┬────────┘ └─────────┬────────┘ └───────┬───────┘
                │                   │                   │
                └─────────┬─────────┴───────────────────┘
                          │
                 ┌────────▼────────┐
                 │      risk       │  (T070)
                 └────────┬────────┘
                          │
                 ┌────────▼────────┐
                 │    features     │  (T050–T053)
                 └────────┬────────┘
                          │
                 ┌────────▼────────┐
                 │ reconstruction  │  (T040–T043)
                 └────────┬────────┘
                          │
                 ┌────────▼────────┐
                 │     storage     │  (T030–T034)
                 └────────┬────────┘
                          │
                 ┌────────▼────────┐
                 │   rpc adapter   │  (T020)
                 └────────┬────────┘
                          │
                 ┌────────▼────────┐
                 │ protocol/domain │  (T010–T013)
                 └─────────────────┘
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
| execution | paper intent→fill→ledger pipeline; live interface reserved | exist with signing/broadcast in current release |
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
| 2 | T022 | `robinhood_lp.discovery.registry` | rpc adapter + storage |
| 2 | T023 | `robinhood_lp.discovery.eligibility` | storage |
| 2 | T024 | `robinhood_lp.discovery.chain_capability` | rpc adapter |
| 3 | T030 | `robinhood_lp.storage.schema` | storage |
| 3 | T031 | `robinhood_lp.storage.raw` | storage |
| 3 | T032 | `robinhood_lp.storage.ingest` | storage |
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
| 6 | T062 | `robinhood_lp.backtest.baselines` | strategy / backtest |
| 6 | T063 | `robinhood_lp.backtest.manifest` | backtest |
| 6 | T064 | `robinhood_lp.backtest.robustness` | backtest |
| 7 | T070 | `robinhood_lp.risk.checks` | risk |
| 7 | T071 | `robinhood_lp.execution.paper` | execution |
| 7 | T072 | `robinhood_lp.execution.realtime` | execution |
| 8 | T080 | `robinhood_lp.ops.observability` | presentation / ops |
| 8 | T081 | `robinhood_lp.ops.controls` | presentation / ops |
| 8 | T082 | `robinhood_lp.ops.soak` | presentation / ops |
| 8 | T083 | `robinhood_lp.ops.dossier` | presentation / ops |
| 9 | T090 | (planning only — no module) | n/a |

## 3. Cross-cutting policies

The following are enforced by every layer and audited by the Definition of
Done in `TODO.md`.

- **Precision.** On-chain integers remain integers from RPC through protocol
  accounting. Conversion to `Decimal` or display units happens only at an
  explicitly named boundary; `float` is forbidden in protocol, replay,
  features, valuation, and accounting code paths. See ADR-004.
- **Determinism.** Replay, backtest, and paper runs must produce
  byte-equivalent canonical outputs across input chunking and storage order
  after excluding declared observational fields (e.g. ingestion wall time).
  No wall clock or unseeded randomness inside deterministic paths.
- **Fail closed.** Missing data, unknown hooks, unsupported RPC behavior,
  address mismatch, or reconciliation failure must reject the operation
  rather than fall back to a default.
- **No silent secrets.** Secrets come from environment variables.
  Serialization, logs, reports, and audit records never include raw RPC
  URLs with credentials, private keys, seeds, or `.env` values.
- **Layer purity.** A layer may not import symbols from a layer above it or
  from a sibling layer that would create a cycle. CI enforces this.

## 4. ADR catalogue

Decisions live under `docs/adr/`. Each ADR is immutable once accepted;
superseding decisions are new ADRs that explicitly reference the prior one.

| ID | Title | Status |
| --- | --- | --- |
| ADR-001 | Web3 client & concurrency model | proposed |
| ADR-002 | Storage & query format | proposed |
| ADR-003 | Configuration & secrets handling | proposed |
| ADR-004 | Integer / decimal precision policy | proposed |
| ADR-005 | Supported-chain lifecycle (support levels) | proposed |
| ADR-006 | Dependency direction between layers | proposed |
| ADR-007 | Continuous integration provider | proposed |
| ADR-008 | Binding document precedence | proposed |

## 5. Open decisions (not blocking research)

These are tracked in `TODO.md` §6 and revisited as evidence arrives. None of
them block T000 itself.

- verified Robinhood Chain V4 PoolManager / StateView deployment and code
  hash (resolved by T024);
- which Robinhood environment is the first integration target (T024);
- verified hook source and semantics for any FLYBRAIN/USDG candidate
  (T023, T043);
- archive/history limits and finality behavior of at least two usable RPC
  endpoints (T024);
- storage engine and expected data volume/retention (refined in ADR-002 as
  evidence arrives);
- quote-currency observation source (resolved in T053);
- quantitative paper-mode SLOs and soak duration (T082);
- license compatibility for copied/ported protocol artifacts and
  third-party vectors (T001, T013).
