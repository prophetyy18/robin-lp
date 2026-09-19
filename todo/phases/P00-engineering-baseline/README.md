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
- [T005 — Verify document citations against the repository](T005.md)
- [T006 — Enforce the layer dependency direction in CI](T006.md)
- [T007 — Make the implementation satisfy ADR-006's declared dependency direction](T007.md)
- [T008 — Add the on-demand global progress view over the plan](T008.md)
- [T009 — Enforce testable acceptance criteria in every task contract](T009.md)
- [T015 — Make test_repository_workflow_configuration_is_valid scale with the task catalog](T015.md)
