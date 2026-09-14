---
id: ADR-001
title: Web3 client and concurrency model
status: accepted
date: 2026-09-12
owner: T000
supersedes: []
references: [R9, R11]
---

# ADR-001 — Web3 client and concurrency model

## Context

The framework needs a JSON-RPC client for Ethereum-compatible chains to
support bounded `eth_getLogs`, block/header/receipt reads, and
block-pinned `eth_call`. Reads are high volume (historical scans,
real-time follow), must survive provider throttling and transient
failures, and must never be capable of signing or sending transactions.
Concurrency must be explicit so retry, failover, and range splitting
behaviors stay observable and testable.

## Decision

Use **`web3.py` (async provider) over `aiohttp`** as the JSON-RPC transport,
wrapped by a project-owned bounded adapter that enforces:

- explicit per-call timeouts and bounded concurrency;
- classified retries (429 / 5xx / timeout) with jitter, no retry of
  non-idempotent or invalid requests;
- endpoint failover across a configured alias list;
- adaptive block-range splitting for `eth_getLogs`;
- capability probe per endpoint (e.g. archive depth, log range limits,
  `eth_getLogs` block-tag support);
- redacted metrics and logs (no endpoint credentials, no raw request
  bodies containing authorization headers).

The adapter exposes only read methods; signing and transaction
submission APIs are not imported anywhere in the codebase.

## Alternatives considered

- **Brownie.** Deprecated, mixes signing concerns into the read path,
  and is not designed for high-volume historical scans.
- **`ethers-py` / community forks.** Smaller ecosystem, less mature
  filter/async story, weaker typed contract interfaces.
- **Raw `aiohttp` JSON-RPC without web3.py.** Re-implements ABI
  decoding, keccak, and typed filters that web3.py already maintains
  against the pinned Ethereum specs.
- **`web3.py` sync provider.** Simpler code but blocks the event loop
  under historical backfills and complicates retry/backoff testing.

## Consequences

Positive:

- typed decoding primitives and a maintained keccak/ABI implementation
  track Ethereum spec changes;
- async provider matches the deterministic event-loop model used by
  the backtest engine;
- adapter boundary isolates provider limits and retry policy from
  domain code, enabling mocked tests for throttling, partial failure,
  duplicate logs, and retry exhaustion.

Negative / risks:

- web3.py tracks Ethereum mainnet defaults; chain-specific quirks
  (e.g. Robinhood Chain finality tags) must be exercised by capability
  probes, not assumed;
- dependency upgrades can shift defaults; pin a minor version range
  with a hash in `pyproject.toml` and re-run T001 smoke test on bump.

## Migration trigger

Re-evaluate this ADR if any of the following occur:

- a maintained async Ethereum JSON-RPC client emerges with materially
  better typed-decoding guarantees and an equal or smaller surface area;
- a chain the framework must support has RPC behavior that web3.py
  cannot express within the adapter (e.g. custom log filtering
  semantics);
- web3.py's async provider becomes unmaintained for two consecutive
  minor releases.

## Owner

T000 (initial). Hand off to whoever owns the rpc adapter module
introduced by T020.
