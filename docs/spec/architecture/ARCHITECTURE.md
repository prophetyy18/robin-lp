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

| Phase | Task | Module / artifact | Layer |
| --- | --- | --- | --- |
| 1 | T010 | `robinhood_lp.protocol.ids` | protocol/domain |
| 1 | T011 | `robinhood_lp.protocol.events` | protocol/domain |
| 1 | T012 | `robinhood_lp.protocol.math` | protocol/domain |
| 1 | T013 | oracle regeneration harness: `tests/test_oracle_drift.py`, `tools/reviewers/allowed_signers` | protocol/domain |
| 2 | T020 | `robinhood_lp.rpc.adapter` | rpc adapter |
| 2 | T021 | `robinhood_lp.protocol.abi_artifacts` (pinned V4 artifacts: `docs/implement/protocol-artifacts/v4-core-e50237c.json`, `tools/oracle/src/SelectorOracle.sol`) | protocol/domain |
| 2 | T022 (retired; superseded by T026) | `robinhood_lp.discovery.registry` | storage |
| 2 | T023 (retired; superseded by T027) | `robinhood_lp.discovery.eligibility` | storage |
| 2 | T024 | `robinhood_lp.discovery.chain_capability` | rpc adapter |
| 2 | T025 | `robinhood_lp.discovery.asset_admission` | risk |
| 3 | T030 | `robinhood_lp.storage.schema` | storage |
| 3 | T031 | `robinhood_lp.storage.{partition,manifest,reader,writer,measurement}` | storage |
| 3 | T032 | `robinhood_lp.ingestion` (runner, planner, router, probe, capability, checkpoint) | application / orchestration |
| 3 | T033 | `robinhood_lp.storage.reorg` | storage |
| 3 | T034 | `robinhood_lp.quality` | storage |
| 4 | T040 | `robinhood_lp.replay.replayer` (`input`, `output`, `checkpoint`, `protocol_fee`) | reconstruction |
| 4 | T041 | `robinhood_lp.replay.ticks` | reconstruction |
| 4 | T042 | `robinhood_lp.replay.state_comparison` | reconstruction |
| 4 | T043 | `robinhood_lp.qualification.hook_pack` (hook semantics) | presentation / reports |
| 5 | T050 | `robinhood_lp.features.bars` | features |
| 5 | T051 | `robinhood_lp.features.position` | features |
| 5 | T052 | `robinhood_lp.features.attribution` | features |
| 5 | T053 | `robinhood_lp.features.quote` | features |
| 6 | T060 | `robinhood_lp.strategy.base` | strategy |
| 6 | T061 | `robinhood_lp.backtest.engine` | backtest |
| 6 | T062 | `robinhood_lp.strategy.baselines` | strategy |
| 6 | T063 (superseded by T105) | `robinhood_lp.reports.{manifest,metrics,rerun,run_identity,validation}` (predecessor delivery) | backtest / research |
| 6 | T064 (superseded by T106) | `robinhood_lp.robustness` (predecessor delivery) | backtest / research |
| 6 | T065 | `robinhood_lp.strategy.{adaptive,adapter}` | strategy |
| 6 | T066 (superseded by T107) | `robinhood_lp.experiments` (predecessor delivery) | backtest / research |
| 6 | T067 (superseded by T108) | `docs/implement/strategy/AUTHORING_GUIDE.md`, its example and drift tests (predecessor delivery) | none (implementation guidance and repository verification) |
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
| 2 | T026 | `robinhood_lp.discovery.registry` (pool-first entry, research universe) | storage |
| 2 | T027 | `robinhood_lp.discovery.research_classification` (research-universe classification) | storage |
| 3 | T039 | `robinhood_lp.qualification.{per_pool_window,per_pool_coverage,per_pool_window_runbook}` (per-pool extended-history acquisition) | presentation / reports |
| 10 | T100 | `robinhood_lp.research.dataset` | storage |
| 10 | T101 | `robinhood_lp.research.panel` (labels, splits, training harness) | backtest / research |
| 10 | T102 | `robinhood_lp.research.models` (model-backed strategy components) | strategy |
| 10 | T103 | `robinhood_lp.web` research pages | presentation / controls |
| 10 | T104 | `robinhood_lp.replay.fee_surface` | reconstruction |
| 1 | T014 | `tools/oracle/test/MathOracle.t.sol` (oracle vector harness) | protocol/domain |
| 0 | T015 | `tests/test_workflow_contracts.py` (repository test; owns no product layer) | none (repository test) |
| 3 | T035 | `robinhood_lp.ingestion.block_header_source`, `robinhood_lp.ingestion.endpoint_client` | application / orchestration |
| 3 | T036 | `robinhood_lp.qualification` (baseline check, fidelity, failure paths, pool-id check, reference, report, runbook, state spot check) | presentation / reports |
| 3 | T037 | `robinhood_lp.storage.reconciliation` | storage |
| 3 | T038 (retired; superseded by T039) | `robinhood_lp.qualification.{second_pool,two_pool,two_pool_window,two_pool_failure_paths,two_pool_runbook}` | presentation / reports |
| 5 | T049 | `robinhood_lp.protocol.sizing` | protocol/domain |
| 6 | T068 | `robinhood_lp.strategy.registry` (registered strategy identities, parameter schemas and code provenance) | strategy |
| 6 | T069 (superseded by T109) | `robinhood_lp.application.backtest_runs` (predecessor product-level run entry point over stored data, replay, features, the engine and the manifest) | application / orchestration |
| 6 | T105 (superseded by T109) | `robinhood_lp.reports.{manifest,validation,rerun,run_identity,metrics}` (predecessor registry-bound manifest and artifact-rerun delivery) | backtest / research |
| 6 | T109 | `robinhood_lp.application.backtest_runs` (product-run lifecycle, causal scheduling and atomic evidence publication) | application / orchestration |
| 6 | T109 | `robinhood_lp.reports.{manifest,validation,rerun,run_identity,metrics,evidence}` (canonical-dataset manifest/report/evidence publication) | backtest / research |
| 6 | T110 | `robinhood_lp.application.historical_replay` (qualified MarketState/RunState composition and replay frames) | application / orchestration |
| 6 | T111 | `robinhood_lp.reports.evidence_adapter` (validated T101/T102/T106 compatibility view over current artifacts) | backtest / research |
| 6 | T106 | `robinhood_lp.robustness` (schema-bound surfaces, splits, scenarios, runner and reports) | backtest / research |
| 6 | T107 | `robinhood_lp.experiments` (search, candidate lock, harness, distributions and stability) | backtest / research |
| 6 | T108 | `docs/implement/strategy/AUTHORING_GUIDE.md`, its example and drift tests | none (implementation guidance and repository verification) |
| 7 | T073 | `robinhood_lp.web.prepaper` (the bounded pre-paper authorization entry: active PoolKey, `HOLD`/`LP` approvals, preliminary-paper authorization) | presentation / controls |
| 8 | T087 | `robinhood_lp.web.runs` (strategy registry and backtest run surfaces: list, trigger, monitor, cancel) | presentation / controls |
| 8 | T088 | `robinhood_lp.web.backtest_result` (the backtest result page and its layered result view) | presentation / controls |
| 9 | T097 | `robinhood_lp.application.reduce_only`, `robinhood_lp.__main__` (read-only query commands and the reduce-only write commands) | application / orchestration |

A row whose task is `APPROVED` names the module, module set or artifact that task
delivered, and every path it names exists in this repository. A row whose task is
not `APPROVED` names the planned path, which may not exist yet. Rows for the
`robinhood_lp.qualification` package name the qualification surface of the
`presentation / reports` layer: it reads protocol, discovery, storage and quality
surfaces and produces reviewable packs and reports without mutating them.

During the A0014 transition, the T063/T064/T066 rows and T067's delivered guide remain
the immutable record of the predecessor implementation, while T105–T108 name the
registry-bound successor ownership. Once `superseded_by` records retirement, the
successor is the only current authority for new writes, evaluation or guidance; an
unapproved successor leaves that current path unavailable and must fail closed rather
than fall back to the predecessor. The predecessor artifact may then be reached only
through the successor contract's explicit read-only legacy or migration path.

The successor is named in the row itself, ahead of the retirement: T063, T064, T066 and
T067 carry `superseded by T105`, `T106`, `T107` and `T108` respectively, and the four
successor rows no longer carry their predecessor's task token, so no §2.2 row is read as a
reference to a task under retirement. The T005 citation resolver requires that wording as
soon as `superseded_by` records a retirement — it fails a superseded row that does not name
its successor — and it reads a task token in a successor row as a reference to that task,
so the annotation has to land before the retirement is recorded; the retirement that
records `superseded_by` may change that field and nothing else, and so cannot repair this
table itself. An earlier attempt to record the same retirement was independently reviewed
and failed for precisely that reason: the ownership table still named T063, T064, T066 and
T067 as current, so annotating `superseded_by` turned the citation gate red. That attempt
was archived as branch `amendment/a0020-attempt-001-failed` and did not land. This
annotation therefore lands first, and the retirement is re-issued afterwards.

Each row that names `robinhood_lp.*` modules declares the layer the layer map assigns to
every module it names, so a row spanning two layers names only the modules of its own
layer and describes the rest in prose: T105 extends the artifact-rerun entry through the
`robinhood_lp.__main__` CLI surface, which the map classifies as application /
orchestration and which the T097 row already maps.

T109 replaces the T069/T105 current-write authorities and owns causal evidence plus atomic
publication. T110 composes the already-approved reconstruction primitives rather than absorbing
them: T040/T041 own post-event pool/tick reconstruction and T104 remains the sole dataset/window/
cursor/reconstruction-bound fee-growth and exact range-fee projection. T110 and the Web read
models may compose T104 but may not implement another fee path. T111's versioned compatibility
view preserves the logical run/dataset/pool/range/registry/schema bindings consumed by approved
T101 and T106 and the T102 path through them, resolving canonical event bytes from T100 references
instead of reopening T069/T105 publication.

For new T109 runs, delayed execution is a correction inside the one T061 engine schedule, not a
projection-layer reorder: a future fill remains queued until its actual fill-data MarketCursor is
reached, so intervening callbacks and accounting views remain pre-fill. Audit ordinal and cursor
order agree when created; T109 refuses publication instead of sorting a non-causal audit history
afterward. T110 projects that saved evidence without callbacks; T111 performs the approved-consumer
cutover. Historical predecessor artifacts remain read-only and unavailable for exact RunState, not
reinterpreted under the corrected schedule.

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
  (T027, T043);
- storage engine and expected data volume/retention (refined in ADR-002 as
  evidence arrives);
- qualified USDG quote source and availability-time semantics (T053);
- quantitative paper-mode SLOs and soak duration (T082);
- local Web authentication/session implementation (T073, T084, T085);
- final economic/risk thresholds, selected from post-testnet paper/shadow evidence in T093.
