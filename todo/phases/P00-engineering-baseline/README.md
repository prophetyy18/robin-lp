# P00 — Decisions, safety boundary, and engineering baseline

**Purpose:** make architectural and safety choices reviewable before code creates
compatibility commitments.

**Entry:** repository rules and the current project-goal baseline are accepted.

**Exit gate:** ADRs are approved; clean install and CI gates pass; the threat model
contains no unowned critical risk; no chain connection is needed to run unit tests.

**Phase prohibitions:** no RPC calls in domain modules, no production addresses in
defaults, no database/framework selected without an ADR, no trading code.

## Tasks

- [T000 — Record initial architecture decisions](T000.md)
- [T001 — Create the Python 3.12 project skeleton](T001.md)
- [T002 — Add safe typed configuration](T002.md)
- [T003 — Establish CI and supply-chain gates](T003.md)
- [T004 — Threat model and live-safety invariant](T004.md)
