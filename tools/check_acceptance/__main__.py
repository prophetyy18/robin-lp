"""Command-line entry point for ``python -m tools.check_acceptance``.

The CLI keeps the surface minimal: a single ``check`` subcommand runs
the checker and exits non-zero when at least one finding remains. The
script exists so the CI gate can call the same code that the test
suite covers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .checker import format_findings, run


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.check_acceptance",
        description=(
            "Run the deterministic offline acceptance-criteria checker introduced by T009."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser(
        "check",
        help="run the acceptance check and exit non-zero on findings",
    )
    check.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="path to the repository root (default: current directory)",
    )
    check.add_argument(
        "--suppressions",
        type=Path,
        default=None,
        help="path to suppressions.toml (default: docs/implement/ci/suppressions.toml)",
    )
    check.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to todo/config.yaml (default: todo/config.yaml under repo root)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command != "check":
        return 2
    findings = run(
        args.repo_root,
        suppressions_path=args.suppressions,
        config_path=args.config,
    )
    print(format_findings(findings, args.repo_root))
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
