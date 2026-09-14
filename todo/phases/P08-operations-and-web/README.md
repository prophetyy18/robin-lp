# P08 — Operations, observability, and release evidence

**Purpose:** make silent degradation impossible, provide the required Web console, and
package reproducible preliminary-paper evidence before testnet/live work.

**Entry:** Phase 7 behavior passes locally.

**Exit gate:** a clean operator can deploy paper mode, operate every required Web journey,
observe injected faults, restore from backup, and reproduce release evidence without
developer-only context.

**Phase prohibitions:** no credential/cardinality leaks in telemetry, unauthenticated
control endpoints, mutable audit history, or untested restore procedure.

## Tasks

- [T080 — Add structured observability and SLOs](T080.md)
- [T081 — Add operational controls, backup, and restore](T081.md)
- [T082 — Run shadow and soak validation](T082.md)
- [T083 — Produce the preliminary-paper release dossier](T083.md)
- [T084 — Build the authenticated read-model Web console](T084.md)
- [T085 — Implement versioned Web approval and safety controls](T085.md)
- [T086 — Validate complete owner journeys and Web security](T086.md)
