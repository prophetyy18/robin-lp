# robinhood-lp

robinhood-lp is a Python framework for researching, backtesting, paper trading,
and eventually executing concentrated-liquidity strategies on Uniswap V4 on
Robinhood Chain.

V1 is deliberately narrow: one Owner, one manually selected target-token contract,
and one manually selected active `PoolKey` at a time on Robinhood Chain. It covers
on-chain data acquisition, deterministic reconstruction and research, strategy and
risk evaluation, paper operation, and gated mainnet LP/required-Swap execution.
The complete product intent is in
[`docs/intent/PROJECT_GOALS.md`](docs/intent/PROJECT_GOALS.md).

> **Safety warning:** default operation is PAPER; ordinary and non-promoted
> operation is not unrestricted mainnet execution. Preliminary paper runs are
> implementation evidence only.
> Live operation requires the documented backtest, testnet, post-testnet
> paper/shadow, security-review, and explicit human-promotion gates. See the
> [project goals](docs/intent/PROJECT_GOALS.md) and
> [security specifications](docs/spec/security/THREAT_MODEL.md).

## Requirements

- Linux
- Python 3.12 or newer
- A virtual environment is recommended

The current server-specific Claude Code and workflow runtime requirements are
documented in [`todo/WORKFLOW.md`](todo/WORKFLOW.md).

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock.txt
python -m pip install -e '.[dev]'
```

The lock file contains hash-pinned third-party dependencies. Direct dependency
declarations live in `pyproject.toml`.

## Inspect and run

After installation, inspect the available read-only and research commands with:

```bash
robinhood-lp --help
robinhood-lp --version
```

The current CLI includes bounded historical ingestion and deterministic manifest
reruns. Use each subcommand's `--help` before supplying project configuration or
data paths:

```bash
robinhood-lp ingest --help
robinhood-lp rerun-manifest --help
```

Run the engineering checks with:

```bash
python -m pytest
python -m ruff format --check .
python -m ruff check .
python -m mypy src tests
```

Inspect the delivery plan without changing task state:

```bash
python -m tools.workflow validate
python -m tools.workflow status
```

## Architecture

The system keeps protocol and chain access separate from state reconstruction,
features, decisions, risk, execution, persistence, and presentation.

| Area | Responsibility |
| --- | --- |
| Protocol / RPC | Verified chain identities, V4 primitives, and read-only transport |
| Storage / reconstruction | Append-only observations and deterministic pool state |
| Features / valuation | Point-in-time market features and USDG-equivalent valuation |
| Strategy | Range and position-lifecycle decisions |
| Risk | Independent admission, exposure, loss, gas, and permission checks |
| Execution | Paper or explicitly promoted transaction planning and submission |
| Presentation | CLI, reports, and operator-facing controls |

[`docs/spec/architecture/ARCHITECTURE.md`](docs/spec/architecture/ARCHITECTURE.md)
is the canonical dependency and component model.

## Repository layout

| Path | Contents |
| --- | --- |
| `src/robinhood_lp/` | Application and research packages |
| `tests/` | Unit, integration, invariant, and workflow tests |
| `docs/intent/` | V1 goals, scope, non-goals, and success criteria |
| `docs/spec/` | Product, strategy, operations, security, architecture, and protocol specifications |
| `docs/implement/` | Traceability, implementation notes, and engineering evidence |
| `todo/` | Numbered task contracts, phase gates, workflow runbook, and task state |
| `tools/` | Workflow checks and development/oracle tooling |
| `.claude/agents/` | Claude Code specialist definitions |

## Documentation map

- Documentation layers: [`docs/README.md`](docs/README.md)
- Product goals: [`docs/intent/PROJECT_GOALS.md`](docs/intent/PROJECT_GOALS.md)
- Specifications: [`docs/spec/README.md`](docs/spec/README.md)
- Architecture: [`docs/spec/architecture/ARCHITECTURE.md`](docs/spec/architecture/ARCHITECTURE.md)
- Goal-to-task traceability: [`docs/implement/TRACEABILITY.md`](docs/implement/TRACEABILITY.md)
- Delivery plan and phase index: [`todo/README.md`](todo/README.md)
- Workflow state machine and controller operations: [`todo/WORKFLOW.md`](todo/WORKFLOW.md)
- Machine-readable task state: [`todo/config.yaml`](todo/config.yaml)

## AI-managed development

- Repository-wide coding-agent policy: [`AGENTS.md`](AGENTS.md)
- Workflow state machine and controller operations: [`todo/WORKFLOW.md`](todo/WORKFLOW.md)
- Current task state: [`todo/config.yaml`](todo/config.yaml)
- Claude Code adapter: [`CLAUDE.md`](CLAUDE.md)

For Claude Code, start from the repository root with the `workflow-manager`
project agent. Use the current model, worktree, Python, and permission-mode command
from `todo/WORKFLOW.md`; that runbook, rather than this landing page, owns the
operational procedure.

## License

Proprietary. See `pyproject.toml`.
