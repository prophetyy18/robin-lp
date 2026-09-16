---
id: ADR-010
title: Free dual-provider historical ingestion
status: accepted
date: 2026-09-16
owner: Owner / Phase 3
supersedes: []
references: [R1, R9, R23, R24, R25, ADR-011]
---

# ADR-010 — Free dual-provider historical ingestion

## Context

The V1 owner selected the free A+B path for short-horizon Robinhood Chain
Uniswap V4 research:

- A: Robinhood's rate-limited public mainnet RPC;
- B: an operator-supplied Alchemy Free Robinhood mainnet endpoint.

The providers are not interchangeable. A read-only probe on 2026-09-16 at
comparison block `64513873` observed matching chain ID `4663`, genesis hash,
safe/finalized blocks, pinned block hash, PoolManager code, and StateView code.
The same probe observed:

- Robinhood public RPC accepted a high-selectivity `eth_getLogs` query spanning
  100,000 blocks, but historical state one million blocks behind the head was
  unavailable;
- Alchemy Free served that historical state, but accepted at most 10 blocks in
  one `eth_getLogs` request. This limit is also documented by Alchemy;
- Robinhood public RPC rejected urllib's default User-Agent with HTTP 403 and
  accepted the same JSON-RPC request with an explicit application User-Agent.

A second result-bearing probe located PoolManager's earliest mainnet code at
block `9070` through Alchemy archive reads. Robinhood public RPC then returned
16 real `Initialize` logs for blocks `9070..109069`. A 10-block interval
containing one of those events normalized byte-for-byte identically from both
providers. This proves historical result retrieval for that interval, not
unlimited retention or capacity for every pool and range.

The log-range probes used a deliberately absent `poolId` topic and therefore
prove request-range acceptance only. They do not prove that a result-bearing
query of the same width will stay below response-size, execution-time, or rate
limits.

## Decision

Historical ingestion is capability-driven, not bound to a permanent primary
provider:

1. Probe each endpoint at run start and record its alias, chain/deployment
   identity, supported finality tags, archive-state depth, accepted log range,
   latency, and probe block/hash. Never persist a credential-bearing URL.
2. Prefer Robinhood public RPC for wide, pool-filtered V4 event backfill. Send
   an explicit non-secret application User-Agent and adaptively split on range,
   response-size, timeout, throttling, or single-block overflow errors.
3. Use Alchemy Free for archive/block-pinned state reads and independent checks.
   It may take over log intervals only through its measured 10-block plan and
   an explicit preflight call/CU/time budget.
4. Fetch `Initialize | ModifyLiquidity | Swap | Donate | ProtocolFeeUpdated` in
   one topic0 OR filter and filter the selected `poolId` in topic1. The system
   must not multiply a scan into one request stream per event type when one
   bounded query suffices. `ProtocolFeeUpdated` is required because the fee in
   `Swap` is the combined swap fee, not automatically the LP-owned share.
5. Record provenance per successful or failed interval. Provider failover must
   not silently turn two partial responses into one supposedly complete range.
6. Verify identity and a deterministic sample of block hashes/log intervals
   across both providers. A mismatch, missing interval, exhausted budget, or
   irreducible provider error prevents dataset qualification. Two endpoint
   brands are not proof of infrastructure independence; record known operator
   and upstream correlation, and describe this check as cross-endpoint unless
   independence is evidenced.
7. Calculate estimates from the requested block bounds and measured caps. Keep
   JSON-RPC calls, HTTP batch requests, response bytes, rows, and provider CUs
   as separate metrics. Calendar duration alone is not a call-count input.
8. A cold-start dataset begins at the selected pool's `Initialize` block, not
   merely at the requested backtest start. This is required to reconstruct the
   tick bitmap, liquidity-net crossings, active liquidity, and protocol-fee
   history without future-state assumptions. Once a qualified local checkpoint
   exists, later short-horizon runs reuse it and ingest only the missing suffix.
   A block-pinned full-state bootstrap is a future optimization and is not
   accepted unless it proves complete tick/fee state against event replay.
9. Fetch one non-hydrated block header per distinct event block for timestamp
   and ancestry context. Do not download every transaction, hydrated block, or
   receipt during ordinary pool-history ingestion; fetch a receipt only when a
   later hook-evidence or reconciliation task explicitly requires it.

For illustration only, a qualified checkpoint followed by a seven-day suffix
containing about 2.42 million blocks would require about 241,920 Alchemy log
calls at the measured 10-block cap, or at least 25 Robinhood calls at a
100,000-block starting range before adaptive splits. At Alchemy's documented
60 CU per `eth_getLogs`, that suffix scan alone is about 14.5 million CU before
block/header/state reads, against the currently documented account-wide 30
million CU/month Free allowance. A first run has an additional cold-start span
from the pool's actual initialization block; it must not quote the suffix-only
estimate. These figures are planning estimates, not defaults or completion
evidence; the run must use a provider usage API or an operator-supplied
remaining-budget bound rather than assume an unused monthly quota.

## Required data boundary

Raw evidence preserves the JSON-RPC log and request provenance. Normalized
records retain, at minimum:

- common identity/order: `chain_id`, PoolManager, `pool_id`, block number/hash,
  transaction hash/index, log index, removed flag, event name, endpoint alias,
  request interval, schema/decode version, and retrieval time;
- block context: parent hash and integer timestamp;
- `Initialize`: currencies, fee, tick spacing, hooks, `sqrtPriceX96`, tick;
- `ModifyLiquidity`: sender, lower/upper ticks, signed liquidity delta, salt;
- `Swap`: sender, signed amount0/amount1, `sqrtPriceX96`, active liquidity, tick,
  and emitted LP fee;
- `Donate`: sender and amount0/amount1.
- `ProtocolFeeUpdated`: pool ID and packed protocol-fee value with exact integer
  semantics from the pinned core artifact.

Acquisition provenance (endpoint, retrieval time, request interval, HTTP
batching) is observational and belongs in the raw envelope/manifest. Canonical
normalized event identity and content hashes exclude those fields, so the same
chain event fetched from A and B compares equal. Raw evidence is still retained
separately and checksummed; excluding provenance from normalized identity never
authorizes its deletion.

StateView observations such as fee-growth globals, tick liquidity, and
fee-growth-outside are block-pinned validation/reconstruction evidence, not
fields to fabricate for every event. Their necessity and sampling points are
owned by Phase 4/5.

## Consequences

The free path is feasible, but a first correct run is a pool-history bootstrap,
not merely a short calendar download. Alchemy-only log backfill has high request
overhead and cannot be described as “a few hundred calls.” Robinhood public RPC
reduces log-call count but is rate-limited and has pruned historical state. Both
providers remain external mutable dependencies; every run must preserve enough
evidence to explain its routing and budget.

## Re-probe triggers

Re-run the capability and budget probes when the provider plan, endpoint,
headers, chain deployment, requested horizon, event density, or observed limit
changes. Re-probe before claiming a new range is complete.
