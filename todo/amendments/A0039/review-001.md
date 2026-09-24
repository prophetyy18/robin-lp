# A0039 owner amendment review

- Base commit: `48e368c05ecfb655293290685d39759de9e9f0d9`
- Candidate commit: `7c2ab5ad0833cf07cd2f3639c476e7add2c3dab5`
- Verdict: **PASS**

## Summary

A0039 is a config-only bookkeeping amendment that closes the drift between five CONTRACT-layer contracts (T073, T084, T107, T108, T110) and the controller-visible dependency graph in todo/config.yaml. The candidate diff is exactly four files: the three A0039 lane files under todo/amendments/A0039/ (impacts.json, planner-001.json, request.json) plus todo/config.yaml. The config.yaml diff removes precisely five lines, each `"T109",`, one inside the depends_on list of each of the five target tasks; every other depends_on entry is preserved verbatim, including T112. No contract file is modified, no other config field is touched, and no protected path is changed (no Intent/Spec/ADR, no src/, tests/, docs/implement/, no tools/workflow/, .claude/, or todo/schemas/). T112 still depends on T109 and replaces T109, and T109 itself is unchanged (lifecycle DELIVERED, approved_commit c79e4c83, depends_on/replaces intact). After this amendment no PLANNED task directly depends on T109 in its depends_on field, so a future SUPERSEDE T109 by T112 amendment will become unblocked. affected_existing_tasks is empty (the bootstrap self-impact guard from 5a3012f rejects such entries on the five target tasks). resolved_task_impacts is empty, consistent with Owner direction that the five A0026-prefixed impacts were already closed by A0030 and A0032-A0035 and must not be re-listed (a closed impact cannot be resolved again by the controller). The five target contracts were independently inspected: each already describes T109 as a 'historical read-only predecessor' and T112 as the 'current registry-bound source', which matches the planner's stated rationale and confirms the config.yaml deltas only realign state with already-contracted reality.

## Required changes

- None.

## Unknowns

- None.
