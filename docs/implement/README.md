# Implementation layer

This directory records what currently exists and the evidence produced by the
implementation. It does not define product intent.

- [`TRACEABILITY.md`](TRACEABILITY.md) maps intent and spec identifiers to tasks.
- [`DOCUMENTATION_INTEGRITY_AUDIT.md`](DOCUMENTATION_INTEGRITY_AUDIT.md) records the
  2026-09-19 audit of document-to-repository consistency, its fixes and what it left open.
- [`WORKFLOW_RULES_CHANGE_2026-09-20.md`](WORKFLOW_RULES_CHANGE_2026-09-20.md) records the
  2026-09-20 bootstrap change to the amendment rules — the ownership-annotation obligation,
  the withdrawal route, the seal-time deterministic gate — and what it deliberately left out.
- [`ci/`](ci/) holds CI evidence and procedures.
- [`evidence/`](evidence/) holds retained implementation evidence.
- [`protocol-artifacts/`](protocol-artifacts/) holds pinned machine-readable
  artifacts consumed by the implementation.

Task contracts, workflow state and independent review reports live under
[`../../todo/`](../../todo/README.md).
