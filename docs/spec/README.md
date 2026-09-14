# Specification layer

Specifications translate V1 intent into testable behaviour and constraints.

- [`product/`](product/) contains admission and Web-console behaviour.
- [`strategy/`](strategy/) contains economic and strategy contracts.
- [`operations/`](operations/) contains operator and emergency controls.
- [`protocol/`](protocol/) contains externally verified protocol facts.
- [`security/`](security/) contains threats, controls and trust boundaries.
- [`architecture/`](architecture/) contains dependency direction and ADRs.

Specifications do not track task progress. A specification defect discovered
during development produces `SPEC_BLOCKED`; it is resolved by a fresh planner
before development resumes.
