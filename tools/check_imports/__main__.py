"""Command-line entry point for ``python -m tools.check_imports``.

The CLI exposes two subcommands:

- ``check`` — run the checker and exit non-zero when at least one
  finding remains. The script exists so the CI gate can call the
  same code that the test suite covers.
- ``edges`` — print the raw, deterministically ordered edge list
  the checker enforced. Used by reviewers to re-derive the
  enforced graph independently.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import module_set
from .check import format_findings, run
from .graph import edges, parse_modules


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.check_imports",
        description=("Run the deterministic offline import-graph checker introduced by T006."),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser(
        "check",
        help="run the import-graph check and exit non-zero on findings",
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
    check.add_argument(
        "--architecture",
        type=Path,
        default=None,
        help=("path to ARCHITECTURE.md (default: docs/spec/architecture/ARCHITECTURE.md)"),
    )

    edge_dump = sub.add_parser(
        "edges",
        help="print the raw edge list enforced by the checker",
    )
    edge_dump.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="path to the repository root (default: current directory)",
    )
    return parser


def _edge_dump_text(repo_root: Path) -> str:
    src_root = (repo_root / "src").resolve()
    modules = parse_modules(module_set(repo_root), src_root=src_root)
    raw_edges = edges(modules)
    lines = ["source,target,line"]
    for edge in raw_edges:
        lines.append(f"{edge.source},{edge.target},{edge.line}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "check":
        findings = run(
            args.repo_root,
            suppressions_path=args.suppressions,
            config_path=args.config,
            architecture_path=args.architecture,
        )
        print(format_findings(findings, args.repo_root))
        return 1 if findings else 0
    if args.command == "edges":
        print(_edge_dump_text(args.repo_root.resolve()))
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
