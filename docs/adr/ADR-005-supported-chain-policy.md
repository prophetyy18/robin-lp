---
id: ADR-005
title: Supported-chain lifecycle (V1: Robinhood Chain only)
status: accepted
date: 2026-09-13
owner: T000
supersedes: []
references: [R1, R2, R6, R14]
---

# ADR-005 — Supported-chain lifecycle (V1: Robinhood Chain only)

## Context

V1 (`docs/product/PROJECT_GOALS.md`) commits to a single-chain, single
target-token, single active-PoolKey product on Robinhood Chain. The
five-level support classification introduced by this ADR must reflect that
scope without overreaching.

The five levels still exist because each `(chain, PoolKey)` is verified
against progressively stricter evidence as the system is promoted from
ingestion to live execution. What changes is the **set of chains that can
ever be promoted**: V1 only admits Robinhood Chain. Other EVM chains are
always `rejected`, not because their protocol behavior is unknown, but
because the product does not target them.

## Decision

Map each `(chain, PoolKey)` to exactly one of five support levels,
matching `TODO.md` §3:

| Level | Allowed | Minimum evidence | Forbidden |
| --- | --- | --- | --- |
| `rejected` | retain reason only | invalid identity / deployment / data evidence | ingestion, simulation, paper |
| `ingestion` | raw + normalized history, quality reports | verified deployment + ABI | valuation, strategy, paper |
| `backtest` | deterministic replay, valuation, backtest | replay reconciliation + modeled hook effects | paper decisions |
| `paper` | real-time decisions + simulated ledger | recovery + freshness + risk + shadow reconciliation | signed/broadcast transactions |
| `live` | approved mainnet LP/Swap execution within V1 limits | all prior evidence, testnet drills, external security review + explicit owner promotion | execution outside the bound chain, PoolKey, strategy, operations or limits |

V1-specific rules:

- **Single chain.** V1 configuration accepts exactly one
  `ChainConfig` entry. Any additional chain is rejected at parse time
  (see T002).
- **Robinhood Chain only.** V1 admits the chain whose verified `chain_id`
  matches the deployment recorded by T024. Other chain IDs are accepted
  in the parser but forced to level `rejected`; they cannot be promoted
  above that level within V1.
- **Single active PoolKey.** V1 configuration accepts zero or one
  `PoolConfig` entry per chain. More than one active pool is rejected at
  parse time.
- **Single target token.** A target token is identified by its Robinhood
  Chain contract address, not by symbol. The user selects it explicitly
  and approves it (T002 records the address and approval state; deeper
  approval logic belongs to T070 / `docs/product/ASSET_ADMISSION.md`).
- **Default for any newly discovered `(chain, PoolKey)` is `rejected`**
  until T024 (chain capability) and T022/T023 (registry + eligibility)
  produce the required evidence.

Rules that still apply unchanged:

- the level is **explicit** and recorded with reason codes;
- support does **not** propagate from one pool to another merely because
  tokens or bytecode are shared;
- promotion is **manual**, evidence-backed, reversible, and audited;
- any unresolved data gap, code-hash change, hook behavior change, or
  reconciliation breach automatically demotes the pool to the last safe
  level;
- non-Robinhood chains remain parseable but cannot be promoted above
  `rejected`; this keeps the door open for a future V2 without changing
  the data model.

## Alternatives considered

- **Binary supported/unsupported.** Too coarse; cannot express that a
  pool can be ingested but not simulated.
- **Per-feature flags.** More flexible but loses the property that a
  pool has one auditable level; regressions hide behind feature toggles.
- **No default — assume best case.** Violates Global Prohibitions and
  creates silent fall-through to plain-pool behavior on unknown hooks.
- **Multi-chain from the start.** Rejected: V1 scope is explicitly
  single-chain; the cost of designing for multi-chain now is paid back
  only if V2 happens, which is out of scope for V1.

## Consequences

Positive:

- every registered pool has a reasoned level; no silent fallback;
- promotion and demotion are auditable events with reason codes;
- risk checks (T070) can refuse execution intent originating from a
  pool whose level is not `paper` or higher;
- the single-chain, single-active-pool constraint is enforced at parse time
  rather than discovered at runtime.

Negative / risks:

- operators must consciously promote a pool; friction is the point,
  but it must be paired with a clear runbook;
- reason-code drift across phases requires a versioning discipline in
  the registry schema (T030);
- if V2 is started, the "V1 admits one chain" rule must be revisited
  via a new ADR — it is not silently relaxed.

## Migration trigger

Re-evaluate this ADR if any of the following occur:

- V1 ships and V2 begins; the single-chain constraint must be lifted
  via a new ADR (this one is **not** amended);
- a new class of evidence becomes standard (e.g. formal verification
  of hook bytecode) that warrants a new level between `backtest` and
  `paper`;
- V1's live evidence gates, signer custody model, or execution boundary changes.

## Owner

T000 (initial). Hand off to whoever owns the eligibility module
introduced by T023.
