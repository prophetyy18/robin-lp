# Specification layer

Specifications translate V1 intent into testable behaviour and constraints.

- [`product/`](product/) contains admission and Web-console behaviour.
- [`strategy/`](strategy/) contains economic and strategy contracts.
- [`research/`](research/) contains the research dataset, split and model-evaluation
  protocol for the research universe defined by `ADR-014`.
- [`operations/`](operations/) contains operator and emergency controls.
- [`protocol/`](protocol/) contains externally verified protocol facts.
- [`security/`](security/) contains threats, controls and trust boundaries.
- [`architecture/`](architecture/) contains dependency direction and ADRs.

Specifications do not track task progress. A specification defect discovered
during development is returned as `TRIAGE_REQUIRED` and classified `SPEC_DEFECT`
by an independent triager (`todo/schemas/triage-result.schema.json`). That
classification routes the task to `PLANNING`, where a fresh Planner corrects the
specification and an independent Plan Reviewer approves the correction before
development resumes.
