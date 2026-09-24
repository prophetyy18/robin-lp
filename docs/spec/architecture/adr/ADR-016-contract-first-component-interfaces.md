---
id: ADR-016
title: Contract-first component interfaces and application composition
status: accepted
date: 2026-09-25
owner: T000
supersedes: []
references: [ADR-006, R17]
---

# ADR-016 — Contract-first component interfaces and application composition

## Context

The architecture defines layers and a static import-direction gate, but those rules do not
fully define the supported calls between components. Implementations can therefore expose
unclear or task-named entry points, and consumers may depend on internal modules even when the
package-level import graph remains valid. The product is currently an in-process application;
its components need a stable collaboration boundary without introducing distributed-service
complexity.

## Decision

Adopt the contract-first rules in `COMPONENT_CONTRACTS.md`:

- every new or materially changed cross-module boundary has a documented, typed public
  contract with explicit semantic, failure, ownership, and compatibility rules;
- providers and consumers depend on that contract, while concrete adapters are selected and
  wired by the application composition root;
- shared ports and values are located so all participants can import them without violating
  ADR-006;
- public product APIs use stable domain or boundary names rather than task identifiers;
- V1 boundaries remain in-process unless an independently reviewed architecture decision
  establishes a need for a network or process boundary;
- existing code migrates only through reviewed tasks that identify the exact callers and old
  path disposition. This decision does not retroactively certify existing implementations.

The human Owner holds architectural decision authority. The repository's planning workflow
assigns document authorship and independent review according to the scope of each change.

## Alternatives considered

- **Rely on package layers and code review alone.** This catches some invalid imports but does
  not state the supported operations, data meaning, failure behavior, or compatibility promised
  to consumers.
- **Split every component into a network service.** This adds transport, deployment, and
  failure modes without a current V1 requirement; in-process ports provide the needed seam.
- **Let each task invent its own interfaces.** This makes task completion local while allowing
  incompatible public surfaces and duplicate authorities to accumulate.

## Consequences

Positive:

- a provider and consumer can be developed and reviewed independently against one stable
  contract;
- public interfaces expose units, event-time semantics, errors, side effects, and migration
  rules instead of leaving those to implementation inference;
- CI import checks and contract-conformance checks verify different properties and can be
  reviewed separately.

Risks:

- poorly placed port definitions can violate the dependency graph; the lowest-common-layer
  rule and T006/T007 gates must be applied to every new boundary;
- documentation and Python types can drift; task acceptance must check the implementation
  against the normative Spec and conformance examples;
- a broad retrofit would destabilize approved work; migration is therefore incremental and
  contract-scoped.

## Migration trigger

Every new or materially changed boundary follows this decision immediately. Existing boundaries
are migrated when a reviewed task changes that boundary or a dedicated successor task names it.
The first product entry migration is T113, which establishes the backtest application boundary
and composes the approved T112 run path.
