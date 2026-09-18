---
id: ADR-014
title: Research universe, numeraire hierarchy, and the execution boundary
status: accepted
date: 2026-09-19
owner: Owner (direct Owner edit, 2026-09-19)
supersedes: []
references: [ADR-004, ADR-005, ADR-006, ADR-008, ADR-009, T020, T022, T023, T100, T101, T102, T103]
---

# ADR-014 — Research universe, numeraire hierarchy, and the execution boundary

## Context

Three pressures arrived together, and each of them looked like a conflict with an
earlier decision until the scopes were separated.

**Research needs many pools; execution needs exactly one.** ADR-005 states that V1
configuration accepts zero or one `PoolConfig` entry per chain and that more than one
active pool is rejected at parse time. That rule was written for the execution
configuration and is still correct for it, but it was being read as though it also
forbade a research collection — which would make cross-pool training and pool-holdout
evaluation impossible to even configure. `PROJECT_GOALS.md` §4 constrains the
*active* pool, the strategy, paper trading and live execution; it never constrained
what may be read, replayed and studied.

**Research needs a reporting unit that is not always USDG.** A dataset whose two
currencies are neither USDG nor any qualified USD asset cannot honestly produce a
USD return series, and expressing one anyway is precisely the failure mode the
project forbids for valuations. The alternative to an explicit hierarchy is not "no
numeraire problem", it is an unlabelled one.

**Models need floating point; the accounting paths must not have it.** ADR-004 keeps
integers from RPC through protocol accounting and permits `Decimal` only at an
explicitly named boundary. Elastic-net, quantile and gradient-boosting models are
float-native by construction. Without a named statistical boundary the model layer
would breach the precision policy on its first line of code.

## Decision

**1. Two scopes, not one.** The **execution scope** is unchanged: one target token,
one user-selected active `PoolKey`, one active strategy version, and every constraint
of `PROJECT_GOALS.md` §4 and ADR-005 continues to bind it exactly as written. The
**research universe** is a separate collection that may hold any number of
`PoolKey` values across different pairs, fees, tick spacings and hooks. A pool enters
the research universe only after its own `(chain_id, PoolKey)` technical identity,
hook classification, data completeness and support level are established, and only at
support level `backtest` or above may it be replayed, backtested or modelled. A
research member holds no assets and produces no transactions, so it requires no
`HOLD`, `LP` or `AUTO_SWAP` approval; and no research object may acquire execution
authority, appear as an approved pool, or be reachable from an execution-shaped
control.

**2. Pool-first onboarding.** A research pool is added by a full V4 `PoolKey`, by a
`PoolId`, or by a target token address. A supplied `PoolKey` is verified by deriving
`keccak256(abi.encode(PoolKey))` locally and reconciling it against the on-chain
`Initialize` event; a supplied `PoolId` is resolved by scanning `Initialize` events,
because `PoolId` is a one-way digest; a token address follows the existing discovery
path. V4 pools are entries in the singleton `PoolManager` and have no contract
address of their own, so **a 20-byte address is never accepted as a pool identity** —
including a position NFT, a link copied from a front end, or the `PoolManager`
itself. Such an input is rejected with a reason.

**3. Numeraire hierarchy.** A dataset records one reporting numeraire and the evidence
for it: USDG when present with a qualified valuation, otherwise a qualified USD
stablecoin, with ETH reserved for display and explicitly labelled as a volatile
numeraire. No stablecoin is assumed to equal one dollar. A dataset with neither a USD
asset nor a qualified route is reported `RELATIVE_ONLY`, carries relative results
only, and may contain no USD-denominated PnL field anywhere. Cross-numeraire results
are never ranked against each other.

**4. The dataset is the unit of research input.** Research consumes immutable,
versioned datasets rather than re-derived ranges. A dataset binds its member pools,
each member's block range, its reporting numeraire and valuation qualification, and
the partitions it resolves to; publishing is additive and an existing version is
never edited. Every result states the dataset version it depends on.

**5. Models occupy interfaces; they never make decisions.** A model implements the
strategy layer's existing replaceable regime and fee-opportunity interfaces and
estimates decision inputs — probability of leaving a range, forward volatility,
volume, fee density, rebalancing-loss proxy — not returns, and it never emits a
position, a size or a tick range. A model grants no execution authority and cannot
become the live default. Its final judgement is produced by the event-driven backtest
engine, and a model that improves a prediction metric while worsening net LP
economics is recorded as a rejection. Floating-point arithmetic is permitted only
inside an explicitly named statistical boundary owned by the model layer; protocol,
replay, valuation and accounting paths remain integer-exact per ADR-004.

## Consequences

`ADR-005` is not reopened and remains binding on the execution configuration; this
ADR adds a second, disjoint collection alongside it, and configuration must be able
to represent both without weakening the parse-time single-active-pool rule. The
research modules take their layer assignment from ADR-006: dataset assembly, labels
and model training sit with the features and backtest/research layers, and the model
implementations sit with strategy, importing neither RPC, storage nor execution.
Storage is already pool-agnostic — partitions and durable checkpoints are keyed by
`(chain_id, contract_address, pool_id)` and carry `pool_id` per row — so no schema
change is needed for N pools, but a run manifest currently records a single
`pool_id` and must either be per-pool or widened before a multi-pool run is claimed.
`ADR-009`'s display boundary is unaffected: a relative-price function stays
numeraire-free, and the numeraire is applied at the reporting layer, not inside it.

## Re-probe triggers

Revisit this decision when a second chain is proposed, when the research universe
exceeds the scale at which a per-pool data root is still practical, when a model
requests an input that is not a market quantity, when any research artifact is
proposed as an execution input, or when a numeraire is proposed that is neither USDG
nor a qualified USD asset.

## Owner

Owner, by direct Owner edit on 2026-09-19. Implementation is owned by T020, T022 and
T023 (pool-first onboarding and the registry), T041 (the fee-growth surface a range
evaluation consumes), T049, T052 and T053 (numeraire-aware sizing, attribution and
quotes), T100 (the dataset registry), T101 (labels and the harness), T102 (model-
backed strategy components) and T103 (the research console). No clause here is
retroactive: it changes no evidence recorded against an approved task, and the
contracts it names carry their own acceptance.
