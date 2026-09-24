# A0030 owner amendment review

- Base commit: `bd4e18a5f96bf13989e63f9314f8ba9118d9ee09`
- Candidate commit: `b987317a3590773d30927eabb696a57593f6084d`
- Verdict: **PASS**

## Summary

The candidate is the A0030 retry (attempt 2) branch amendment/a0030-attempt-001, strictly retargeting T073 pre-paper current-evidence language from T109 to T112 at the CONTRACT layer and adding T112 to T073.depends_on in both the contract and todo/config.yaml. The diff against base bd4e18a is exactly three files: todo/amendments/A0030/impacts.json (raised-impact text updated to name the missing config edit), todo/amendments/A0030/planner-002.json (new planner record with AMENDMENT_READY outcome, full impact_assessment, and `resolved_task_impacts: [A0026:T073:prepaper-current-evidence]`), and todo/config.yaml (single-line addition of T112 to T073.depends_on, taking it from `[T025, T027, T107, T109]` to `[T025, T027, T107, T109, T112]`). No other T073 field is changed (lifecycle OPEN, claimed false, task_file, attempt, base_commit, candidate_commit, approved_commit, latest_review all untouched); no other task record is changed; top-level active_task / active_phase / workflow_state / intent_revision / spec_revision / agent_runtime are byte-identical. T073.md is byte-identical between attempts da73470 and b987317 and its T109→T112 retargeting remains consistent: T109 is only referenced as historical read-only predecessor (lines 38, 58, 74, 88) and pre-T112 manifest / evidence-unavailable run / T066 lock wording, while T112 is named as the current registry-bound manifest/evidence/candidate-lock source in Deliverables (lines 33, 36), Acceptance (lines 57-58), Must-not (line 74) and References (line 86). All existing must-not clauses survive intact (private-key / Keystore / seed / RPC refusal, no generic transaction form, no live promotion, no P08 console / T103 page, no CLI / background replacement, no inferring approval from replay); the new pre-T112 qualifier tightens rather than weakens them. Owner direction in todo/amendments/A0030/request.json is recorded exactly as supplied (CONTRACT layer, target T073, A0026 impact ID in resolved_task_impacts) and the candidate matches it. `tools.workflow validate` reports `status: OK`, confirming the controller's drift check accepts the new T073.depends_on; the controller's `_without_amendment_owned_config_fields` deliberately strips `depends_on` from the CONTRACT drift check, so this edit is permitted and required at this layer. No business code is authored; no workflow_state, intent/spec revisions or top-level config field changes; no other amendment branch contains b987317. T073.depends_on in config.yaml and T073.md `## Dependencies` now agree, so the FAIL defect from review-001 is repaired.

## Required changes

- None.

## Unknowns

- None.
