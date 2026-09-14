---
id: ADR-006
title: Dependency direction between layers
status: accepted
date: 2026-09-12
owner: T000
supersedes: []
references: [R17, R22]
---

# ADR-006 — Dependency direction between layers

## Context

The plan defines ten layers (protocol/domain → rpc → storage →
reconstruction → features → strategy / backtest → risk → execution →
presentation). Without an enforced direction, code tends to grow
sideways: the strategy layer imports the RPC adapter "just for one
query", the storage layer learns about display decimals, the protocol
layer imports `time` for "just a timeout". Each of these leaks makes
replay non-deterministic and testing painful.

## Decision

Adopt an explicit directed acyclic dependency graph instead of pretending every component
belongs in one total order:

- protocol/domain contains shared immutable identities, values, events and intent/result
  contracts and imports no higher layer;
- RPC and storage are independent adapters that depend on protocol/domain, not on each other;
- reconstruction and features consume injected read ports, not concrete RPC/storage modules;
- strategy depends on immutable domain/feature contracts and produces candidate intents;
- risk depends on domain/valuation/authorization contracts and returns scoped decisions; it
  never imports a concrete strategy implementation;
- execution consumes only an approved intent plus protocol/risk contracts; it never imports
  a concrete strategy;
- backtest/application orchestration is the only layer that wires strategy → risk → paper or
  live execution through their public interfaces;
- presentation calls application services/read models and cannot mutate lower-layer storage
  or execution directly;
- sibling implementations do not import one another. A genuinely shared type moves into a
  lower contracts/domain module instead of creating a sideways dependency;
- `time`, `datetime.now()`, `random`, and any unseeded randomness are
  forbidden inside the protocol, replay, features, strategy, and
  backtest modules; a deterministic clock is injected instead;
- CI runs a `depend` / `import-linter` style check (see ADR-007) that
  fails on any violation.

Allowed dependency shape:

```
presentation / reports
          |
application / backtest orchestration
    |          |          |
 strategy     risk     execution ----> isolated signer (narrow request only)
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

Mapping from planned modules to layers is recorded in
`docs/spec/architecture/ARCHITECTURE.md` §2.2.

## Alternatives considered

- **Allow horizontal imports with code review.** Cheaper in the short
  term; loses the property that a layer can be reasoned about in
  isolation.
- **Single flat package.** Maximally flexible, but every test and
  every ADR becomes a fight against entropy.

## Consequences

Positive:

- protocol math is testable without spinning up an RPC client, a
  database, or a clock;
- storage changes cannot accidentally change replay results;
- strategy code is a pure function of observation, making T060
  feasible.

Negative / risks:

- the rule is enforced by tooling; tooling must be configured and
  maintained (T001 + T003);
- legitimate cross-cutting needs (logging, configuration) must be
  injected rather than imported directly; that requires discipline
  early in the codebase.

## Migration trigger

Re-evaluate this ADR if any of the following occur:

- a layer split becomes necessary because a layer has grown past a
  reasonable cognitive size;
- the project needs an explicit "platform" layer (logging, config,
  metrics) that all layers depend on; this would become a new ADR
  rather than relaxing this one.

## Owner

T000 (initial). Hand off to whoever owns the project skeleton
introduced by T001 and the CI gates introduced by T003.
