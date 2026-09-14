# P02 — Verified chain access and pool discovery

**Purpose:** read only from a proven chain deployment and build a complete PoolKey
registry from canonical events.

**Entry:** typed configuration and event identities complete.

**Exit gate:** two providers (or provider plus fixture node) give equivalent results;
deployment report passes; every discovered pool has an explicit support reason.

**Phase prohibitions:** no signing middleware, hosted filters as sole ingestion
mechanism, copied deployment address without code verification, or unbounded RPC.

## Tasks

- [T020 — Build a read-only bounded RPC adapter](T020.md)
- [T021 — Pin canonical V4 artifacts](T021.md)
- [T022 — Discover and register pools from `Initialize`](T022.md)
- [T023 — Classify pool and hook eligibility](T023.md)
- [T024 — Verify chain capabilities and contract deployments](T024.md)
- [T025 — Implement Token, PoolKey and permission approval records](T025.md)
