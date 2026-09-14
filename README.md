# robinhood-lp

Automated liquidity-provision and market-making strategies for
**Robinhood Chain / Uniswap V4**.

Initial research target: concentrated-liquidity LP strategies such as
**FLYBRAIN / USDG**.

> ⚠️ **Default mode: PAPER.** Mainnet automated execution is the V1
> acceptance condition (G-LIVE-01) and is reached only through the
> promotion gates in [`docs/intent/PROJECT_GOALS.md`](docs/intent/PROJECT_GOALS.md)
> (backtest → testnet → post-testnet paper/shadow → security review →
> human promotion). A preliminary paper run may happen earlier to validate the
> implementation, but it provides no live-promotion evidence.
> See [Trading Safety](#trading-safety).

## Goals

The system supports, in order of priority:

1. Real-time on-chain data collection
2. LP range selection
3. LP entry / exit decisions
4. Paper trading
5. Backtesting
6. Risk management
7. Mainnet automated execution (V1 acceptance condition; reached via
   the documented promotion gates, not by direct opt-in)

Research correctness and reliable data collection come first; mainnet
execution is gated by `docs/intent/PROJECT_GOALS.md` and
`docs/spec/product/ASSET_ADMISSION.md`.

Plan-level goal-to-task coverage is indexed in
[`docs/implement/TRACEABILITY.md`](docs/implement/TRACEABILITY.md). The executable
task plan and current progress live under [`todo/`](todo/README.md).

## Requirements

- Python 3.12+
- Linux (developed and tested on this server)

## Quick start

```bash
# create a virtualenv
python3.12 -m venv .venv
source .venv/bin/activate

# install the package and development tools
python -m pip install -e '.[dev]'
```

## Architecture

Components are kept strictly separate:

| Layer         | Responsibility                                 |
| ------------- | ---------------------------------------------- |
| Data          | On-chain ingestion, normalization, storage     |
| Features      | Volatility, IL, depth, volume, gas metrics     |
| Strategy      | Range selection, entry / exit signals          |
| Risk          | Pre-exposure, inventory, drawdown checks       |
| Execution     | Transaction submission (paper by default)      |
| Storage       | Parquet / DB for raw and derived series        |

Strategy code never holds private keys. Execution never decides
strategy. Risk checks always run before execution.

## Trading Safety

- **Default mode is PAPER.** No real transactions are sent.
- Service secrets such as RPC credentials come from environment/secret injection.
  The live signer instead opens an encrypted Web3 Keystore and accepts its password
  only through a non-echoing interactive terminal prompt; neither is exposed to Web,
  strategy, risk, logs, or configuration serialization.
- `.env` / `.env.*` / `*.key` / `*.pem` / `secrets/` are git-ignored.
- Live execution requires the complete evidence gates and an explicit human promotion;
  a configuration switch alone cannot enable it.

## LP strategy evaluation

Performance is **not** measured by fee APR alone. Every strategy is
evaluated against:

- Realized volatility
- Impermanent loss
- Adverse selection
- Inventory exposure
- Rebalance cost
- Gas cost
- Liquidity depth
- Volume persistence

Measured data, not assumptions.

## Repository conventions

- Coding agents must read [`AGENTS.md`](./AGENTS.md), `CLAUDE.md`, and
  [`todo/config.yaml`](todo/config.yaml) before selecting a task.
- Smallest reasonable change per commit.
- Tests accompany every feature; never claim something works without
  running them.
- Inspect existing code before modifying it.
- Do not overwrite unrelated user changes. Inspect `git diff` before
  finishing substantial modifications.
- Do not push or merge unless explicitly requested.

See [`CLAUDE.md`](./CLAUDE.md) for the full project rules used by the
coding assistant in this repo.

## License

TBD.
