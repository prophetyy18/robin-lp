# P02 — Verified chain access and pool discovery

**Purpose:** read only from a proven chain deployment and build a complete PoolKey
registry from canonical events.

**Entry:** typed configuration and event identities complete.

**Exit gate:** two providers (or provider plus fixture node) give equivalent results;
deployment report passes; every discovered pool has an explicit support reason.

**Phase prohibitions:** no signing middleware, hosted filters as sole ingestion
mechanism, copied deployment address without code verification, or unbounded RPC. A
20-byte address is never a pool identity: V4 pools are entries in the singleton
`PoolManager`, and any other protocol version's pool address, position NFT or
front-end link supplied as a pool is rejected with a reason rather than guessed at
(ADR-014).

## Tasks

- [T020 — Build a read-only bounded RPC adapter](T020.md)
- [T021 — Pin canonical V4 artifacts](T021.md)
- [T022 — Discover and register pools from `Initialize`](T022.md)
- [T023 — Classify pool and hook eligibility](T023.md)
- [T024 — Verify chain capabilities and contract deployments](T024.md)
- [T025 — Implement Token, PoolKey and permission approval records](T025.md)
- [T026 — Add pool-first onboarding and the research universe registry](T026.md)
- [T027 — Classify research-universe pools and their support levels](T027.md)
