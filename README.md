# robinhood-lp

Automated liquidity-provision and market-making strategies for
**Robinhood Chain / Uniswap V4**.

Initial research target: concentrated-liquidity LP strategies such as
**FLYBRAIN / USDG**.

> ⚠️ **Default mode: PAPER.** Mainnet automated execution is the V1
> acceptance condition (G-LIVE-01) and is reached only through the
> promotion gates in [`docs/product/PROJECT_GOALS.md`](docs/product/PROJECT_GOALS.md)
> (backtest → testnet → paper → security review → human promotion).
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
execution is gated by `docs/product/PROJECT_GOALS.md` and
`docs/product/ASSET_ADMISSION.md`.

## Requirements

- Python 3.12+
- Linux (developed and tested on this server)

## Quick start

```bash
# create a virtualenv
python3.12 -m venv .venv
source .venv/bin/activate

# install dependencies (added as the project grows)
pip install -U pip
```

A `pyproject.toml` will be introduced once the first modules land.

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
- Secrets (private keys, seed phrases, API tokens) come from
  environment variables. They are never printed, logged, or committed.
- `.env` / `.env.*` / `*.key` / `*.pem` / `secrets/` are git-ignored.
- Live execution is gated behind an explicit opt-in. Do not enable it
  without reviewing the risk layer first.

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
