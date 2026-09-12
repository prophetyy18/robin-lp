---
id: ADR-003
title: Configuration and secrets handling
status: proposed
date: 2026-09-12
owner: T000
supersedes: []
references: [R1, R2, R9]
---

# ADR-003 — Configuration and secrets handling

## Context

Configuration must:

- distinguish immutable protocol facts (chain IDs, contract addresses,
  ABIs) from mutable deployment facts (RPC URLs, finality behavior);
- accept complete V4 `PoolKey` tuples, never token symbols;
- reject unknown fields, mismatched chain IDs, malformed addresses,
  duplicate identities, and credential-bearing URLs;
- keep secrets out of serialized config, logs, reports, and audit
  records;
- be diff-able so a config change is reviewable and reproducible.

## Decision

Use **pydantic v2 strict models** as the single configuration surface:

- models are `frozen=True`, `extra="forbid"`, with explicit `Field(...)`
  declarations for every accepted key;
- secrets are read from environment variables by name, never from
  config files or CLI flags;
- the config root object holds:
  - per-chain: `chain_id`, RPC environment-variable name,
    confirmations/finality policy, start block, verified
    `PoolManager` and `StateView` addresses with code-hash references,
    request limits;
  - per-pool: complete V4 `PoolKey` (currency0, currency1, fee
    including dynamic-fee sentinel, tickSpacing, hooks);
  - run mode: one of `rejected | ingestion | backtest | paper | live`,
    defaulting to `paper` when missing, invalid, or partial.
- serializers reject literal credential URLs and strip secret values
  even when explicitly requested;
- `.env.example` contains only variable names and placeholders; real
  `.env` is git-ignored and never committed.

## Alternatives considered

- **`dataclasses` + hand-written validators.** Lower dependency
  surface but more code to maintain, no built-in serialization
  guarantees, and no schema-language self-documentation.
- **`dynaconf` / `OmegaConf`.** Convenient layering but loose typing,
  weaker rejection of unknown fields, and less explicit secret
  redaction.
- **YAML/TOML with manual parsing.** Common in Python CLIs but pushes
  the same validation problem down into every consumer.

## Consequences

Positive:

- strict models fail at parse time on unknown fields, mismatched
  chain IDs, malformed addresses, and duplicate identities;
- secret names are explicit and auditable; redaction is enforceable
  via a custom serializer rather than convention;
- a config object can be hashed and recorded in experiment manifests
  (T063) without exposing secrets.

Negative / risks:

- pydantic v2 dependency must be pinned with a hash and re-validated
  on bump;
- model definitions can drift from on-chain reality if not paired with
  T024 chain-capability reports;
- ergonomics for power users (deep-merge, layered configs) require
  project-level helpers, not pydantic itself.

## Migration trigger

Re-evaluate this ADR if any of the following occur:

- a maintained alternative provides equivalent strict-model guarantees
  with a smaller dependency footprint;
- the project needs remote/centralized configuration with auditable
  change history that pydantic cannot model without bespoke code;
- a CVE in pydantic v2 affects our usage and the upstream fix is
  delayed.

## Owner

T000 (initial). Hand off to whoever owns the config module introduced
by T002.
