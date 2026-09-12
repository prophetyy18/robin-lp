---
id: ADR-006
title: Dependency direction between layers
status: proposed
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

Adopt the following **dependency rules**:

- a layer may import symbols only from layers strictly below it;
- a layer may not import symbols from siblings;
- a layer may not import a symbol whose transitive closure violates
  the same rule;
- `time`, `datetime.now()`, `random`, and any unseeded randomness are
  forbidden inside the protocol, replay, features, strategy, and
  backtest modules; a deterministic clock is injected instead;
- CI runs a `depend` / `import-linter` style check (see ADR-007) that
  fails on any violation.

Layer ordering (highest to lowest):

```
presentation / reports
  execution
    strategy
      backtest / research
        risk
          features
            reconstruction
              storage
                rpc adapter
                  protocol/domain
```

Mapping from planned modules to layers is recorded in
`docs/architecture.md` §2.2.

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
