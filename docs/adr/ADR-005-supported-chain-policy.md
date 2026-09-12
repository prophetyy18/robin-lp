---
id: ADR-005
title: Supported-chain lifecycle (support levels)
status: proposed
date: 2026-09-12
owner: T000
supersedes: []
references: [R1, R2, R6, R14]
---

# ADR-005 — Supported-chain lifecycle (support levels)

## Context

Each `(chain, PoolKey)` carries different evidence and risk: a verified
deployment on a known chain is not the same as a pool with a verified
hook economic model; a deployment whose bytecode matches pinned
sources is not the same as one whose hook semantics have been
replayed against observed receipts. Without an explicit, auditable
classification, the system can silently fall back to plain-pool
behavior on hooked pools — the failure mode called out by Global
Prohibitions in `TODO.md`.

## Decision

Map each `(chain, PoolKey)` to exactly one of five support levels,
matching `TODO.md` §3:

| Level | Allowed | Minimum evidence | Forbidden |
| --- | --- | --- | --- |
| `rejected` | retain reason only | invalid identity / deployment / data evidence | ingestion, simulation, paper |
| `ingestion` | raw + normalized history, quality reports | verified deployment + ABI | valuation, strategy, paper |
| `backtest` | deterministic replay, valuation, backtest | replay reconciliation + modeled hook effects | paper decisions |
| `paper` | real-time decisions + simulated ledger | recovery + freshness + risk + shadow reconciliation | signed/broadcast transactions |
| `live` | reserved for a future separately authorized project | external security review + explicit owner approval | everything until that project exists |

Rules:

- the level is **explicit** and recorded with reason codes;
- support does **not** propagate from one pool to another merely
  because tokens or bytecode are shared;
- promotion is **manual**, evidence-backed, reversible, and audited;
- any unresolved data gap, code-hash change, hook behavior change, or
  reconciliation breach automatically demotes the pool to the last
  safe level;
- the **default level for any newly discovered `(chain, PoolKey)` is
  `rejected`** until T024 (chain capability) and T022/T023
  (registry + eligibility) produce the required evidence.

## Alternatives considered

- **Binary supported/unsupported.** Too coarse; cannot express that a
  pool can be ingested but not simulated.
- **Per-feature flags.** More flexible but loses the property that a
  pool has one auditable level; regressions hide behind feature
  toggles.
- **No default — assume best case.** Violates Global Prohibitions and
  creates silent fall-through to plain-pool behavior on unknown hooks.

## Consequences

Positive:

- every registered pool has a reasoned level; no silent fallback;
- promotion and demotion are auditable events with reason codes;
- risk checks (T070) can refuse execution intent originating from a
  pool whose level is not `paper` or higher.

Negative / risks:

- operators must consciously promote a pool; friction is the point,
  but it must be paired with a clear runbook;
- reason-code drift across phases requires a versioning discipline in
  the registry schema (T030).

## Migration trigger

Re-evaluate this ADR if any of the following occur:

- a new class of evidence becomes standard (e.g. formal verification
  of hook bytecode) that warrants a new level between `backtest` and
  `paper`;
- the project moves into a separate live project and the `live` level
  needs elaboration (this is the Phase 9 trigger, not a Phase 0 one).

## Owner

T000 (initial). Hand off to whoever owns the eligibility module
introduced by T023.
