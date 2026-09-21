# T069 planning review

- Base commit: `1ea801208c131f1d979c8263bdec54d3c6605679`
- Candidate commit: `d0506ee4f390517d249eae392c418d4be46b4a4f`
- Verdict: **PASS**

## Summary

The planning correction accurately transcribes the OWNER_DECISION_REQUIRED answer (Reading B) recorded in todo/triage/P06/T069/owner-decision-002.json into the T069 acceptance clause. The candidate touches only todo/config.yaml (workflow_state PLANNING -> AWAITING_PLAN_REVIEW and T069.status PLANNING -> AWAITING_PLAN_REVIEW, which are the controller-owned transitions performed by finish-plan), todo/evidence/P06/T069/attempt-002-planner.json (new planner evidence file matching todo/schemas/planner-result.schema.json with outcome PLAN_READY and empty unresolved_questions), and todo/phases/P06-backtesting-and-strategy/T069.md (acceptance clause rewrite). No implementation files (src/, tests/, tools/) are touched, no other contract section (Outcome, Deliverables, Must-not, References, Dependencies header) is altered, and no depends_on / intent_revision / spec_revision / attempt / evidence pointer / commit SHA / runtime model / approval data changes are introduced. The new acceptance clause (1) opens with the deliverables' singular wording 'a new run request naming the dataset version, the registered strategy identity and its parameters, the pool and block range, and the seed, fill, cost and quote assumptions', (2) makes Reading (b) explicit: 'one RunRequest carrying exactly one pool_key_id and publishing exactly one manifest under the request's own run_id', (3) reframes the two-heterogeneous-pool-fixtures scenario as two separate RunRequests whose manifests share dataset_version, reporting_numeraire and valuation_qualification under their respective run_ids, and (4) removes the 'one manifest per member pool under one run identity' phrasing the Owner told us to drop. The remaining acceptance bullets (byte-equivalent rerun, product rerun source preservation, cancellation/failure closed, in-flight restart, fail-closed validation, offline CLI) are preserved verbatim. The contract header's OWNER_PLAN_EXTENSION_2026-09-20 status is unchanged. Reading (b) is consistent with the existing implementation (RunRequest declares a single pool_key_id; submit publishes one manifest per request; _MANIFEST_FILENAME is keyed per (run_id, pool_key_id); test_two_heterogeneous_pools_share_run_identity issues two separate RunRequests and asserts the shared fields under their respective identities) and with the G-BACKTEST-RUN-01 intent (singular 'data range'). The T105 multi-pool 'one run identity over one dataset version and reporting numeraire with one manifest per member pool' behaviour is explicitly out of T069 scope per the Owner decision; T069 only produces one manifest per RunRequest.

## Required changes

- None.

## Unknowns

- None.
