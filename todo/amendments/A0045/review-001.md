# A0045 owner amendment review

- Base commit: `2efa8623d65753c64c2820ded648e8ed9779e962`
- Candidate commit: `d4d8736d8d74e8792e506630d4f0589cdc51098e`
- Verdict: **PASS**

## Summary

A0045 is a narrow CONTRACT-layer amendment on a single still-PLANNED task (T073). Verified all recorded Owner directions and gating constraints: (1) todo/config.yaml is byte-identical to base — T073.depends_on is [T025, T027, T107, T112] as expected (A0039 already aligned it). (2) T073.md diff is exactly a single-line edit: line 8 (the ## Dependencies body) changed from 'T025, T027, T107, T109, T112' to 'T025, T027, T107, T112'. All other T109 mentions in T073.md (lines 38, 58, 74, 88 in Deliverables/Acceptance/Must-not/References) are preserved verbatim as legitimate historical-predecessor references, byte-identical to base. (3) Candidate diff touches exactly four paths: the three new lane files (todo/amendments/A0045/{impacts.json, planner-001.json, request.json}) and todo/phases/P07-risk-and-paper/T073.md. No edit to todo/config.yaml, no edit to any other contract, no edit to docs/intent, docs/spec, docs/implement, docs/spec/architecture, ADR, tools/workflow, .claude, or todo/schemas. (4) impacts.json: layer=CONTRACT, raised=[], resolves=[]. (5) request.json: task_ids=[T073], target_status=PLANNED, layer=CONTRACT; owner_direction matches the actual change. (6) planner-001.json: affected_existing_tasks=[] and resolved_task_impacts=[] as instructed (the A0026:T073:prepaper-current-evidence impact was already closed by A0030). impact_assessment correctly marks intent/specification/implementation/data/operations/verification as not-applicable and explains why removing T109 from the current-dependency list strengthens, not weakens, contract-vs-config alignment and the security guarantees. The amendment is internally consistent, stays within the CONTRACT planning path for a still-PLANNED task, preserves immutable history (T109 references in Acceptance/Must-not/References are byte-identical), and does not introduce any other requirements or workflow-state changes.

## Required changes

- None.

## Unknowns

- None.
