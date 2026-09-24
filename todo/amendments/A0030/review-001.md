# A0030 owner amendment review

- Base commit: `8e662a08a8a1e862737f8448aa42f5e1aaceb4c0`
- Candidate commit: `da7347081d25489ef4a1527c88a733c7ac7b18d1`
- Verdict: **FAIL**

## Summary

The candidate is correctly scoped to a single CONTRACT-layer amendment on T073 and targets only the T073 contract plus the three A0030 amendment records. The retarget of pre-paper current-evidence language from T109 to T112 is internally consistent in T073.md itself: Deliverables, Acceptance, Must-not and References all switch T109 references to T112 and preserve T109 as a historical read-only predecessor alongside T105. The existing must-not clauses (private key / Keystore / seed / RPC refusal, no generic transaction form, no live promotion, no P08 console / T103 page, no CLI / background replacement, no inferring approval from replay, and the explicit refusal to treat a pre-T112 manifest, evidence-unavailable run or T066 lock as current) survive intact and are in fact slightly tightened by the new pre-T112 wording. `tools.workflow validate`, `check_citations`, `check_acceptance`, `check_imports` and `progress` all pass; the candidate carries no new deterministic findings. The Owner direction, target-task set (T073 only) and CONTRACT layer are recorded exactly as supplied, and `resolved_task_impacts` correctly names the existing A0026-prefixed impact ID `A0026:T073:prepaper-current-evidence`, which is currently open on T073 and inside this amendment's scope. However, the candidate updated only the contract's `## Dependencies` section (T025, T027, T107, T109, T112) and left `todo/config.yaml` byte-identical: T073's `depends_on` is still `[T025, T027, T107, T109]` and does not include T112. The plan-reviewer requirement to keep dependency edits consistent across config and task contracts is therefore violated, and the candidate's own `impact_assessment.dependencies` and the affected_existing_tasks `required_disposition` both describe the change as `T073 depends_on extends from `T025, T027, T107, T109` to `T025, T027, T107, T109, T112`` / `DEPENDENCY: T112 added to T073 depends_on`, so the omission is not a deliberate scope choice but an incomplete repair.

## Required changes

- Update `todo/config.yaml` so that T073's `depends_on` is `[T025, T027, T107, T109, T112]`, matching the contract's `## Dependencies` section. The contract and the dependency graph must agree, and a Developer activating T073 should not be allowed by the controller to start before T112 is APPROVED while the contract textually requires T112. CONTRACT-layer amendments are explicitly permitted to edit `depends_on` for the named target tasks; the omission is the defect.

## Unknowns

- None.
