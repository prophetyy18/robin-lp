---
id: ADR-011
title: Project-owned bounded JSON-RPC transport
status: accepted
date: 2026-09-16
owner: T020 / Phase 3
supersedes: [ADR-001]
references: [R9, R11, ADR-010]
---

# ADR-011 — Project-owned bounded JSON-RPC transport

## Context

ADR-001 selected web3.py over aiohttp. The independently reviewed T020
implementation instead delivered a smaller project-owned read-only adapter over
`httpx`, with the retry, failover, range splitting, allow-list, redaction, and
metrics behavior at the project boundary. The dependency lock and tests now pin
that implementation. Leaving ADR-001 accepted would make the Spec contradict
the approved code immediately before P03 depends on it.

The A+B probe also showed that transport details are observable provider
capabilities: urllib's default User-Agent received HTTP 403 from the Robinhood
public endpoint, while the project's current httpx transport successfully read
the endpoint. Headers must therefore remain explicit and non-secret even though
the domain layer must not depend on a particular HTTP library.

## Decision

Supersede ADR-001's client-library choice with the existing project-owned
`RpcAdapter` over `httpx`:

- expose only the read methods required by the task contracts;
- keep bounded timeouts/concurrency, classified retry, endpoint failover,
  adaptive log-range splitting, and redacted metrics inside the adapter;
- use an explicit non-secret application User-Agent for production probes and
  ingestion, without persisting credential-bearing URLs or raw authorization
  headers;
- inject fake transports in unit tests and keep provider capability/routing
  policy in application orchestration rather than in protocol code;
- add JSON-RPC batching only after measurement shows that reducing HTTP round
  trips materially helps header retrieval. A batch never counts as fewer
  logical RPC calls and partial batch responses must fail closed.

No signing or transaction-submission API is added. ABI decoding and protocol
math continue to use the pinned project artifacts rather than a provider/client
library's mutable defaults.

## Consequences

P03 can extend the tested adapter without adding web3.py or maintaining two RPC
stacks. The project owns more JSON-RPC validation code, but that code already
exists and has narrower authority than a general Web3 client. If a future
provider requires a protocol feature the adapter cannot safely express, a new
ADR may replace this choice behind the same read-only port.
