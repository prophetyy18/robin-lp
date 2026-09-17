# P03 — Versioned historical ingestion and audit storage

**Purpose:** preserve enough immutable evidence to re-decode, audit, and recover.

**Entry:** Phase 2 exit gate and storage ADR approved.

**Exit gate:** a fixed range survives interruption, overlap, corruption, provider
failover, budget exhaustion, and synthetic reorg tests while producing zero
unexplained gaps; at least one real Robinhood Chain mainnet range has been acquired
through the live path with `complete=true`, its block time and parent hash are pinned
to canonical block headers, and the dataset has been cross-checked against block-pinned
on-chain state; its report separates RPC calls, HTTP requests, bytes, rows, provider
units, and storage size.

**Phase prohibitions:** no in-place destruction of raw history, future-state calls,
silent schema coercion, interpolation, or completion based only on “no rows”. Acquisition
is range-based: never one `eth_getLogs` call per block, never a header fetch for a
block with no pool event, and never a per-block or per-event `eth_call` — pool state
is rebuilt locally from the ingested event stream, and on-chain state reads are
reserved for explicitly enumerated block-pinned verification.

## Tasks

- [T030 — Define versioned raw and normalized schemas](T030.md)
- [T031 — Implement append-only raw storage](T031.md)
- [T032 — Implement checkpointed historical ingestion](T032.md)
- [T033 — Handle confirmations and reorgs](T033.md)
- [T034 — Produce data-quality and completeness reports](T034.md)
