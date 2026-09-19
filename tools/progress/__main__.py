"""Command-line entry point for ``python -m tools.progress``.

Two modes are exposed:

- the default ``render`` mode prints the full plain-language view of
  the committed plan and exits non-zero when the configuration fails
  any of the fail-closed checks;
- ``--check`` runs exactly the same checks and exits non-zero on the
  first one it finds, or zero on a plan that renders faithfully.

The command writes nothing to disk and never modifies the working
tree; it is purely a renderer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .check import ProgressIssue, load_config, validate
from .render import render


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.progress",
        description=(
            "Render the committed plan on demand without writing or caching anything (T008)."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="path to the repository root (default: current directory)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to todo/config.yaml (default: todo/config.yaml under --repo-root)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "run only the fail-closed checks; exit non-zero on the first "
            "failure and zero on a clean plan"
        ),
    )
    return parser


def _format_issues(issues: list[ProgressIssue]) -> str:
    if not issues:
        return "no configuration issues found"
    parts: list[str] = []
    for issue in issues:
        target = issue.target or "<config>"
        parts.append(f"[{issue.code}] {target}: {issue.message}")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_path = args.config.resolve() if args.config else None
    try:
        config = load_config(repo_root, config_path=config_path)
    except ProgressIssue as issue:
        print(_format_issues([issue]), file=sys.stderr)
        return 1

    issues = validate(config, repo_root=repo_root)
    if issues:
        print(_format_issues(issues), file=sys.stderr)
        return 1

    if args.check:
        print(_format_issues([]))
        return 0

    print(render(config, repo_root=repo_root))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
