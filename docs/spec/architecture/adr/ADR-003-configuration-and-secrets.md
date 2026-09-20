---
id: ADR-003
title: Configuration and secrets handling
status: accepted
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
- service secrets such as RPC credentials are read from environment or deployment-secret
  injection by name, never embedded in config files or CLI flags;
- live signing uses a standard encrypted Web3 Keystore file; its password is accepted
  only through a non-echoing interactive terminal prompt and is never stored in config,
  environment variables, CLI arguments, Web state, logs, databases, or audit records;
- only the isolated signer process decrypts the Keystore, keeps the plaintext key in
  memory, and returns to a locked state after restart;
- the config root object holds:
  - per-chain: `chain_id`, RPC environment-variable name,
    confirmations/finality policy, start block, verified
    `PoolManager` and `StateView` addresses with code-hash references,
    request limits;
  - per-pool: complete V4 `PoolKey` (currency0, currency1, fee
    including dynamic-fee sentinel, tickSpacing, hooks);
  - run mode: one of `rejected | ingestion | backtest | paper | live`, defaulting to
    `paper` only when omitted; invalid or partial values are rejected.
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
- a config object can be hashed and recorded without exposing secrets in the
  predecessor T063 experiment manifests and, after retirement, in the registry-bound
  T105 successor manifests; T063 artifacts then remain read-only history.

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
- the approved custody model changes from an operator-unlocked encrypted Keystore;
- a CVE in pydantic v2 affects our usage and the upstream fix is
  delayed.

## Owner

T000 (initial). Hand off to whoever owns the config module introduced
by T002.
