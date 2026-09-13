---
id: ADR-008
title: Binding document precedence
status: proposed
date: 2026-09-13
owner: T000 / T004
supersedes: []
references: [R6]
---

# ADR-008 — Binding document precedence

## Context

V1 ships several categories of binding documents that occasionally
disagree:

- **Product documents**: `docs/product/PROJECT_GOALS.md`,
  `docs/product/ASSET_ADMISSION.md` — describe what the system is for
  and which hard gates are not user-overridable.
- **Delivery plan**: `TODO.md` — describes phases and tasks.
- **Architectural decisions**: `docs/adr/ADR-*.md` and the living
  `docs/architecture.md` — describe technical choices and layering.
- **Safety invariant**: `docs/threat-model.md` — describes the threat
  surface and the controls that mitigate it.

When two of these disagree (as happened between T004
`docs/threat-model.md` and `PROJECT_GOALS.md` G-LIVE-01), the current
state has no explicit rule for which wins. Different reviewers can
make different choices; agents and humans both need a single, durable
answer.

## Decision

When documents disagree, the following precedence applies (highest
authority first):

1. **`docs/product/PROJECT_GOALS.md`** — V1 product scope and goals.
2. **`docs/product/ASSET_ADMISSION.md`** — V1 hard gates that the
   framework must enforce (`ADM-TECH-*`, `ADM-RISK-*`, `ADM-PAIR-*`,
   `ADM-HOOK-*`).
3. **`TODO.md`** — phase and task definitions, dependencies,
   acceptance criteria.
4. **`docs/threat-model.md`** — threat surface and controls.
5. **`docs/architecture.md`** and **`docs/adr/ADR-*.md`** — technical
   choices and layering.
6. **Source code** — must conform to all of the above; source cannot
   redefine product scope.

Rules:

- A lower-precedence document **must not** contradict a
  higher-precedence one. If it appears to, the lower-precedence
  document is wrong and must be updated to match.
- An apparent contradiction between two documents of the **same**
  precedence is a bug to be resolved by the document owner; no
  automatic precedence exists within a tier.
- An apparent contradiction **within** `PROJECT_GOALS.md` or
  `ASSET_ADMISSION.md` is a product-spec bug. Resolution requires the
  product owner.
- A new ADR that wishes to change a higher-precedence rule must
  itself amend the higher-precedence document in the same change
  set, with the same review.
- Code review rejects any change that makes the source contradict a
  higher-precedence document. The reviewer names the document and the
  conflict.

Explicit cross-references that this ADR enforces:

- `threat-model.md` §1 *Scope* and T-12 bind to `PROJECT_GOALS.md`
  G-LIVE-01 and G-SIGNER-01.
- `ADR-005` binds to `PROJECT_GOALS.md` §4 (single chain / single
  target token / single active pool) and to `ASSET_ADMISSION.md`
  §9 (PoolKey + hook rules).
- `TargetTokenConfig` binds to `ASSET_ADMISSION.md` §4 (dual-track
  approval) and `PROJECT_GOALS.md` G-LIVE-GATE-01
  (`live_eligible`).

## Consequences

Positive:

- a single durable rule replaces ad-hoc reconciliation;
- the product documents become the canonical source of truth for
  scope and gates, removing ambiguity for both humans and agents;
- a new contradiction is resolved by naming the higher-precedence
  document in the commit or PR that fixes the lower-precedence one.

Negative / risks:

- product documents become a critical dependency; if they are stale
  or wrong, code and ADRs are forced to follow them;
- raising a product document's scope without coordinating engineering
  causes churn; demoting scope is also churn;
- the precedence itself is itself a higher-precedence rule (ADR-008
  ranks below the product documents, not above them), so the
  precedence can be amended by editing ADR-008 alone.

## Migration trigger

Re-evaluate this ADR if any of the following occur:

- a new category of binding document is introduced (e.g. a security
  policy that outranks the threat model);
- the product owner formally designates a different document as the
  scope-of-truth (e.g. splitting PRODUCT_GOALS into a vision doc and
  a release-spec doc);
- two of the listed precedence tiers must merge, splitting, or
  re-ranking becomes necessary.

## Owner

T000 / T004. This ADR is the lowest of the listed tiers; it is the
rule for resolving internal disagreements, not a rule for product
scope.
