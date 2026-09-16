# P03 — Versioned historical ingestion and audit storage

**Purpose:** preserve enough immutable evidence to re-decode, audit, and recover.

**Entry:** Phase 2 exit gate and storage ADR approved.

**Exit gate:** a fixed range survives interruption, overlap, corruption, provider
failover, budget exhaustion, and synthetic reorg tests while producing zero
unexplained gaps; its report separates RPC calls, HTTP requests, bytes, rows,
provider units, and storage size.

**Phase prohibitions:** no in-place destruction of raw history, future-state calls,
silent schema coercion, interpolation, or completion based only on “no rows”.

## Tasks

- [T030 — Define versioned raw and normalized schemas](T030.md)
- [T031 — Implement append-only raw storage](T031.md)
- [T032 — Implement checkpointed historical ingestion](T032.md)
- [T033 — Handle confirmations and reorgs](T033.md)
- [T034 — Produce data-quality and completeness reports](T034.md)
