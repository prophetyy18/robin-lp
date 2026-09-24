# Component interface contracts

> Status: normative architecture specification.
> Scope: new and materially changed cross-module boundaries; existing modules migrate only
> through a reviewed task that names them.

This document turns the layer rules in `ARCHITECTURE.md` and ADR-006 into a contract-first
working rule. It defines the interface obligations shared by component authors and consumers.
It does not replace domain requirements in the product, strategy, operations, security, or
protocol specifications.

## 1. Public boundary

A public component boundary is the supported way for another component or entry point to
request behavior or consume a result. Its owner identifies the public package/module and
exports the supported names from that package. Cross-component callers must not depend on
private helpers, internal persistence layouts, or sibling implementation modules.

The V1 application is an in-process system. A public boundary may be a Python service API, a
read/write port, or a typed domain contract. A boundary does not imply a network service, HTTP
endpoint, queue, or separately deployed process. Add those only through a separate approved
architecture decision when a measured product need requires them.

Each boundary has one contract owner. The owner maintains the public types and compatibility
notes; provider tasks implement the contract; consumer tasks use it; the application
composition root supplies concrete implementations. The human Owner holds architectural
decision authority and approves changes to layer or dependency rules. The architecture Spec
records those rules; T006/T007 static gates enforce import direction, while contract-conformance
tests and independent task review verify runtime behavior and provider/consumer agreement. If a public type is shared across layers,
place it in the lowest layer permitted by ADR-006 that all providers and consumers can import.
An application use-case API may remain in the application layer when only presentation or
other higher-level callers consume it.

## 2. Required contract contents

The interface specification and its task reference together identify:

- boundary name, owner, provider, consumers, and public import path;
- operations, typed request and result shapes, optionality, and closed vocabularies;
- units, integer/decimal boundary, event-time and availability semantics, and identity scope;
- normal results, named failures, retry/cancellation behavior, and whether failure has side effects;
- state mutation, persistence, idempotency, determinism, and audit obligations;
- security and permission limits, including data that must never cross the boundary;
- compatibility policy, versioning, and migration behavior for persisted or externally consumed
  records;
- representative valid, boundary, invalid, and failure examples or a direct reference to the
  contract-conformance suite.

A signature alone is not a complete contract. Meaning that affects behavior belongs in the
Spec; implementation docs may show current usage but cannot weaken or redefine that meaning.

## 3. Provider, consumer, and composition rules

- Providers implement the named contract and preserve its error and data semantics. They do not
  make consumers import the concrete adapter.
- Consumers depend on the contract and handle its declared failures. They do not reach around
  the contract to read another component's storage or state.
- The application composition root is the only place that selects concrete adapters for a
  product use case. It injects them through typed ports and makes no hidden fallback choice.
- A port belongs at a dependency location consistent with ADR-006. If no existing layer can
  own it without a forbidden import, the architecture must be amended before implementation;
  do not resolve that conflict with a sibling import or duplicate semantic types.
- Protocol/domain values remain free of IO and framework dependencies. Adapter-facing ports are
  abstractions and typed contracts, not permission to move storage or RPC behavior into domain
  code.
- Production code uses stable domain or boundary names. Task numbers remain in task contracts,
  review evidence, and version tags only where a persisted format genuinely needs an explicit
  version.

## 4. Change and compatibility rules

A task that adds or changes a public boundary names the contract and its provider/consumer
impact. Breaking a public operation, changing field meaning, units, ordering, availability,
error behavior, or persisted interpretation requires an Owner-directed Spec/ADR route and a
reviewed migration plan before implementation. Additive fields or operations still require
explicit compatibility rules. Approved task contracts and historical artifacts are never
rewritten to make them appear to have used a later interface.

This rule is mandatory for T113 and for tasks created or amended after A0046. Existing
approved contracts gain no new obligations from this decision alone. Existing open contracts
that conflict with the corrected T109/T112 state or introduce/consume a boundary governed here
are listed in A0046 impact records; they remain frozen and may not activate until a reviewed
CONTRACT amendment closes the exact impact. Other legacy boundaries migrate only through
reviewed tasks that name them. Each migration task states which callers move, what old path is
disabled or retained read-only, and how the transition fails closed.

## 5. Verification and agent handoffs

For each changed boundary, the task contract requires provider conformance checks and consumer
checks at the boundary, including normal, boundary, invalid-input, and failure behavior where
applicable. At least two implementations or fixtures must be materially heterogeneous when
the contract claims implementation independence. The CI import-graph gate continues to enforce
layer direction and module classification; it does not substitute for behavioral contract
tests.

Developer handoffs identify public interfaces changed, providers and consumers affected,
compatibility and migration behavior, and the exact conformance checks run. Reviewers inspect
the implementation and evidence against the contract. Agents may not silently choose a new
public API, change a Spec while implementing a task, or treat the existence of an implementation
module as proof that it is the supported interface.
