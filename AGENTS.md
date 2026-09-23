# Repository policy for coding agents

This file is the canonical repository-wide policy for every coding agent working
on robinhood-lp. Product intent, specifications, workflow mechanics, and task
state remain authoritative in their own documents; this file defines the shared
boundaries agents must respect.

## 1. Authority and conflict handling

The documentation model is Intent → Spec → Implement; see `docs/README.md`.
When sources conflict, use this order:

1. `docs/intent/PROJECT_GOALS.md`;
2. `docs/spec/product/`, `docs/spec/strategy/`, `docs/spec/operations/`, and
   `docs/spec/security/`;
3. the selected task contract under `todo/`;
4. `docs/spec/architecture/` and `docs/spec/protocol/`;
5. `docs/implement/`, code, and tests.

Report a conflict instead of silently choosing the easier lower-authority
interpretation. Do not turn an unconfirmed idea into a requirement. If an
unresolved product choice changes behavior, stop and request an Owner decision.

Canonical ownership is:

- product intent: `docs/intent/PROJECT_GOALS.md`;
- product, strategy, operations, security, architecture, and protocol
  requirements: `docs/spec/`;
- workflow state machine and controller operations: `todo/WORKFLOW.md`;
- machine-readable task state: `todo/config.yaml`;
- numbered-task contracts and phase gates: `todo/`;
- implementation and engineering evidence: `docs/implement/`, code, and tests.

## 2. Scope, contracts, and trust boundaries

- Work on one numbered task at a time. Before implementation, confirm its
  dependencies, phase entry conditions, contract, references, and affected files.
  Restate its Outcome, Dependencies, Deliverables, Acceptance, and Must not
  clauses, then implement all and only that contract.
- `todo/config.yaml` is the only machine-readable progress source. Existing task
  contracts and approval evidence are immutable history except through the
  reviewed routes defined in `todo/WORKFLOW.md`. Never edit task state or claim
  that existing code retroactively satisfies a task.
- Planner clarifies specifications and eligible contracts but does not implement;
  Developer implements one authorized task without changing Intent, Spec,
  contracts, review records, or task state; Reviewer verifies an exact candidate
  commit without fixing it; Issue Triager classifies exceptions read-only; Manager
  selects legal workflow actions but cannot substitute for a specialist or approve
  work. Planning changes also require an independent Plan Reviewer. Detailed role,
  amendment, maintenance, continuation, and state-transition semantics belong to
  `todo/WORKFLOW.md` and the role definitions.
- A task is complete only after an independent Reviewer has checked the recorded
  candidate commit and the controller records `APPROVED`. Uncommitted files,
  conversation text, self-review, skipped evidence, or a completion claim are not
  approval evidence. Handoffs bind the contract, base commit, candidate commit,
  and structured report. A repaired candidate requires a fresh independent review.
- Amendments preserve the recorded state (`lifecycle`/`claimed`), attempts,
  candidate/approval commits, evidence, and review history. Approved work is retired only through the annotation-only
  `SUPERSEDE` route; its historical contract and evidence are never rewritten.
- Work that will not land is closed with the Owner-directed `abandon-task` or
  `abandon-maintenance` route. It lands nothing, keeps the branch, worktree and
  review records as evidence, and is refused for `APPROVED` work. Every
  non-terminal status has this exit, so a work item can always be closed without
  the blocked actor acting and the single-active-work lane can always be
  released.
- `tools/workflow/`, `.claude/`, and `todo/schemas/` are protected governance
  surfaces changed only by an explicit bootstrap action. Dependency manifests may
  change only when a numbered task contract expressly requires it. No role may
  rewrite the gate that governs its own work.
- Within `.claude/` and `todo/schemas/`, the role definitions of `prophet.md` and
  `prophet-reviewer.md`, plus `tools/workflow/core.py` itself, are the **constitutional**
  surface and remain bootstrap-only. PROPHET may, however, rewrite the
  non-constitutional governance artifacts inside its delegated authority —
  the three reviewer and manager agent prompts that are not its own role
  definition, the three review-result schemas, and the predicates in
  `tools/workflow/core_governance.py` — through a normal PROPHET amendment
  reviewed by `prophet-reviewer`. An amendment that crosses the delegated
  boundary (e.g. attempts to edit `core.py`, the role definitions, or the
  editable/forbidden file lists themselves) is refused by the controller before
  review fires.

## 3. V1 product boundary

- Deliver V1 only: one Owner, Robinhood Chain only, one manually selected target
  token contract, and one manually selected active `PoolKey` at a time. The system
  may discover multiple candidates but must not auto-select or switch tokens or
  pools. Do not add V2, multi-chain, simultaneous active pools, multi-user, or
  strategy-market behavior.
- Verify the actual chain ID, deployment addresses, runtime code hashes, Token,
  paired Token, `PoolKey`, and Hook independently. Names and symbols are display
  metadata. Unknown Token or Hook settlement behavior is ineligible for strategy,
  paper, and live use.
- Strategy handles normal-market signals and position lifecycle. Central risk is
  mandatory and owns capital, permission, valuation, gas, loss, and drawdown
  limits. `NO_NEW_RISK` is a scoped result with a reason code, not a global state or
  an automatic exit. Leaving a range is not itself an exit decision.
- V1 strategy market signals are price, volume, and liquidity distribution. USDG
  features and the confirmed five-minute extreme-move rule use T053-qualified,
  point-in-time reproducible USDG conversion. Primary capital, risk, and
  performance reporting is in USDG equivalent while retaining original integer
  token/ETH amounts.
- Do not invent additional economic or risk thresholds before paper trading.
  Temporary values must remain configurable, versioned, validated, evidence-bound,
  and marked `EXPERIMENTAL_NOT_LIVE_APPROVED`.
- V1 ends in gated Robinhood Chain mainnet execution. Backtest, testnet, and paper
  are evidence gates, not substitutes: backtest → testnet → post-testnet
  paper/shadow → security review → explicit human promotion.

## 4. Live execution and secrets

- Phases 0–8 may not add signing, broadcast, private-key loading, or plaintext
  Keystore decryption. Phase 9 permits only the explicitly contracted surfaces;
  completing earlier work never grants live authority. Mainnet execution requires
  the scoped human promotion defined by T094.
- The main application, Web, strategy, and risk processes must never read a private
  key or Keystore password. Private keys remain encrypted in a standard Web3
  Keystore and plaintext exists only in isolated signer memory.
- The signer accepts its password only from a non-echoing interactive terminal.
  Passwords and plaintext keys must never come from environment variables,
  configuration, CLI arguments, Web input, files, logs, or databases. A restarted
  signer is locked.
- Do not control the Owner's primary wallet or automatically transfer exit assets
  to an external address. Never print or commit secrets, environment contents,
  credential-bearing URLs, webhooks, keys, passwords, signing payloads, or
  authorization headers.

## 5. External facts and architecture

- Robinhood Chain, Uniswap deployments, ABIs, Hooks, Tokens, and RPC capabilities
  are mutable facts. Verify them from official source, official documentation, and
  block-pinned chain reads; third-party pages are cross-checks only. Record the
  source URL, retrieval time, chain ID, block number/hash, and code hash when
  applicable. If verification fails, return `UNKNOWN` or block promotion—never
  guess or copy another chain's values.
- Preserve the dependency direction in
  `docs/spec/architecture/ARCHITECTURE.md` across protocol, RPC, storage,
  reconstruction, features, strategy, risk, execution, and presentation.
  Strategy must not access RPC, storage, signer, or execution, mutate ledgers, or
  approve its own risk. Execution must not choose strategy or bypass central risk.
- Use integers for on-chain quantities, ticks, encoded prices, liquidity, and
  accounting. Convert to Decimal only at an explicit presentation or statistical
  boundary; protocol and accounting paths may not use float.
- Replay, features, and backtests use event time and only information available at
  the decision timestamp. No future data, wall clock, or unseeded randomness may
  influence deterministic results.
- Raw data and audit events are append-only. Never overwrite or delete them or fill
  gaps by interpolation. Errors must retain applicable chain, `PoolKey`,
  block/range, endpoint, attempt, and cause while redacting secrets.

## 6. Changes, verification, and reporting

- Inspect the current implementation before describing or changing it. Make the
  smallest contract-complete change and preserve unrelated user work.
- New behavior needs normal, boundary, invalid-input, and failure-path coverage;
  use genuinely heterogeneous fixtures when two are required. Run every
  task-specific check plus `pytest`, Ruff format/check, and strict mypy. Missing
  credentials or unavailable integration evidence is not a pass.
- Diagnose failures. Never delete tests, relax acceptance criteria, tolerances,
  types, safety gates, or risk limits, or add unjustified ignores merely to obtain
  PASS.
- Inspect `git diff` and run `git diff --check`. Do not commit, push, merge, deploy,
  sign, broadcast, or send external messages unless the applicable workflow and
  explicit Owner authority permit that action.
- Report changed files, exact commands and results, unmet acceptance items, skipped
  checks, assumptions, external-fact versions, and residual risks. If work is not
  approved, say so and preserve its non-`APPROVED` state. Do not replace evidence
  with “should work”, “mostly complete”, or equivalent language.
