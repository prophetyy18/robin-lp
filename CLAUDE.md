# Project: Robinhood Chain / Uniswap V4 LP Strategies

## Project Overview

This project develops automated liquidity-provision and market-making
strategies for Robinhood Chain / Uniswap V4.

The initial research target is concentrated-liquidity LP strategies such as
FLYBRAIN/USDG.

The system will eventually support:

- real-time on-chain data collection
- LP range selection
- LP entry / exit decisions
- paper trading
- backtesting
- risk management
- mainnet automated execution (the V1 acceptance condition; reached via
  the G-LIVE-GATE-01 promotion gates: backtest → testnet → post-testnet
  paper/shadow → security review → human promotion)

The first priority is research correctness and reliable data collection.
Live execution is the V1 acceptance condition (G-LIVE-01) and is reached
only through the documented promotion gates, not by direct opt-in.

## Development Environment

The project is developed and executed on this Linux server.

Use Python 3.12+ unless the existing project configuration specifies otherwise.

Before adding dependencies, check whether an existing dependency already
provides the required functionality.

Prefer simple, modular implementations.

## Development Workflow

When the user asks this outer Claude Code session to execute the workflow, act
only as the Manager: read `todo/WORKFLOW.md`, run its `validate` and `status`
commands, and invoke `develop`, `review`, or `retry` for exactly one numbered
task. Do not edit the business implementation or perform the independent review
in the Manager context. The controller must create the fresh Developer and
Reviewer processes. Stop on `SPEC_BLOCKED` or `BLOCKED`, do not advance to the
next task, and do not push unless the user separately requests it.
The recommended outer session permission mode is `manual`; request approval only
for the exact `tools.workflow` command needed for the current transition. Never
ask the user to approve `sudo`, a direct implementation edit, or a direct
commit/merge as a workaround for a failed gate.

Before implementing a feature:

1. Read `AGENTS.md`, `todo/config.yaml`, `todo/README.md`, and the one selected
   task contract.
2. Confirm the task is `READY`, all dependencies are `APPROVED`, and the phase
   entry gate is satisfied.
3. Inspect the relevant existing code and referenced Intent/Spec documents.
4. Explain the proposed change briefly and implement only that task.
5. Add or update tests and run every required verification command.
6. Produce a candidate commit for a fresh, independent reviewer.
7. Do not call the task complete until the workflow records `APPROVED`.

Never claim code works without running the relevant test or command.

## Investigation Rule

Never speculate about code that has not been inspected.

If a question concerns an existing file, module, configuration, contract,
or implementation, read the relevant source before answering.

## Trading Safety

The default operating mode is **PAPER**.

Mainnet automated execution is the V1 acceptance condition (G-LIVE-01)
and is reached only through the promotion gates recorded in
`docs/intent/PROJECT_GOALS.md` (backtest → testnet → post-testnet
paper/shadow → security review → human promotion; G-LIVE-GATE-01).
A pre-testnet preliminary paper run validates implementation only. Live code paths are *not*
enabled by direct opt-in and are not automatically deployed after
implementing code.

The signing material required for live execution is held in a separate
signer process (G-SIGNER-01), not in the main V1 process. The signer
is introduced by Phase 9 (T090) and is out of scope for Phase 0–8.

Never expose, print, commit, or copy:

- private keys
- seed phrases
- API secrets
- wallet credentials
- production environment files

Service secrets such as RPC credentials must come from environment or deployment-secret
injection. The signer is the sole exception: it reads an encrypted Web3 Keystore file and
accepts the decryption password only from a non-echoing interactive terminal prompt. The
password and plaintext private key must never come from environment variables, config,
CLI arguments, Web, logs, or persistent storage; plaintext exists only in signer memory.

## Architecture Principles

Keep these components separated:

- data collection
- feature calculation
- strategy logic
- execution
- risk management
- storage

Strategy code must not directly manage private keys.

Execution code must not decide strategy.

Risk checks must occur before execution.

## LP Strategy Research

For LP strategies, do not evaluate performance using fee APR alone.

Always consider:

- realized volatility
- impermanent loss
- adverse selection
- inventory exposure
- rebalance cost
- gas cost
- liquidity depth
- volume persistence

Prefer measured data over assumptions.

## Git

Do not overwrite unrelated user changes.

Inspect `git diff` before finishing substantial modifications.

Do not commit secrets or `.env` files.

Do not push or merge unless explicitly requested.
