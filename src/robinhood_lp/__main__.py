"""Command-line entry point for ``python -m robinhood_lp``.

Phase 0 supports exactly one subcommand: ``--version``. Real subcommands are
introduced by later phases (data ingestion, backtest, paper execution, etc.).
"""

from __future__ import annotations

import argparse
import sys

from robinhood_lp import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="robinhood-lp",
        description=(
            "Research and paper-trading framework for Uniswap V4 LP strategies on Robinhood Chain."
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the package version and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and execute the requested subcommand.

    Returns the process exit code (0 on success, non-zero on error).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.version:
        sys.stdout.write(f"{__version__}\n")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
