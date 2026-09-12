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
- optional live execution

The first priority is research correctness and reliable data collection,
not live trading.

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

The default operating mode is PAPER.

Do not send real blockchain transactions unless the user explicitly asks
to enable live execution.

Do not automatically deploy or run live trading after implementing code.

Never expose, print, commit, or copy:

- private keys
- seed phrases
- API secrets
- wallet credentials
- production environment files

Secrets must come from environment variables.

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
