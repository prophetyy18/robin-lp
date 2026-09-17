# T031 planning review

- Base commit: `18306c38ba4db072ddfa7cf2bdd68c363719403f`
- Candidate commit: `d56596350a6bef8daa8024702a0bb96d150f2ca2`
- Verdict: **PASS**

## Summary

Candidate commit d565963 correctly consolidates the 2026-09-16 Owner amendment into a single unified contract body. The top markers `Contract status: FROZEN_FROM_BASELINE_6C31778` and `Owner amendment (2026-09-16): storage evidence must support ADR-010...` are removed; the binding decisions are merged into Outcome (ADR-002 accepted and not reopened, ADR-010 refines field semantics of the two-layer structure, T030 = logical schema vs T031 = Parquet column / partition / manifest physical layout, acquisition provenance separated from normalized identity / content hash), Deliverables (Parquet column layout with raw evidence, typed / normalized, acquisition envelope, and schema / decode version column groups; staging / atomic commit; manifest rows with row count, block bounds, file SHA-256, schema / decode version, ingestion checkpoint plus the full split-by counter list endpoint alias / logical RPC calls / HTTP requests / batches / response bytes / normalized rows / provider units / Parquet bytes / scanned-empty intervals; reorg journal preserving audited raw evidence; T011 EventKey-based dedup; reader that checks SHA-256 and never infers from filenames; dual raw envelopes from A and B retained), Acceptance (idempotent repeat / overlap, crash-at-boundary atomicity, corruption and manifest mismatch detection, cross-provider normalized content hash equality with raw envelopes retained, dedup by T011 EventKey + normalized content with conflicting observations halting qualification, representative short-range partition-sizing measurement recorded as a measurement not a universal constant), Must-not (no mutate / delete orphaned raw evidence, no exposed half-written partitions, no filename-as-manifest, no reopen of ADR-002 in this task, no credential-bearing URLs, no weakening Acceptance), and References (R13, ADR-002, ADR-010). The appended `Binding direction reference` subsection explicitly identifies itself as audit trail only and forbids treating it as a second source of binding direction. The contract change is scoped to todo/phases/P03-ingestion-and-storage/T031.md; docs/intent/, docs/spec/, other task contracts, workflow / controller code, agent definitions, and review records are untouched. The accompanying todo/config.yaml workflow_state / status transition and todo/evidence/P03/T031/attempt-001-planner.json creation match the T030 planning-candidate pattern (commit 9a44378) and are workflow mechanics required to advance the state from PLANNING to AWAITING_PLAN_REVIEW. No implementation or contract-status data was edited. Developer can execute attempt 002 from the merged contract without returning to planning.

## Required changes

- None.

## Unknowns

- None.
