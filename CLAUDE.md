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
  the G-LIVE-GATE-01 promotion gates: backtest → testnet → paper →
  security review → human promotion)

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

Before implementing a feature:

1. Inspect the relevant existing code.
2. Understand the current architecture.
3. Explain the proposed change briefly.
4. Implement the smallest reasonable solution.
5. Add or update tests.
6. Run the relevant tests.
7. Report what changed and any remaining risks.

Never claim code works without running the relevant test or command.

## Investigation Rule

Never speculate about code that has not been inspected.

If a question concerns an existing file, module, configuration, contract,
or implementation, read the relevant source before answering.

## Trading Safety

The default operating mode is **PAPER**.

Mainnet automated execution is the V1 acceptance condition (G-LIVE-01)
and is reached only through the promotion gates recorded in
`docs/product/PROJECT_GOALS.md` (backtest → testnet → paper → security
review → human promotion; G-LIVE-GATE-01). Live code paths are *not*
enabled by direct opt-in and are not automatically deployed after
implementing code.

The signing material required for live execution is held in a separate
signer process (G-SIGNER-01), not in the main V1 process. The signer
is introduced by Phase 9 (T090) and is out of scope for Phase 0–7.

Never expose, print, commit, or copy:

- private keys
- seed phrases
- API secrets
- wallet credentials
- production environment files

Secrets must come from environment variables. The signer (when it
exists) reads its key material from environment variables only and
never logs or echoes its contents.

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
