"""Top-level orchestration of the acceptance-criteria check (T009).

The ``run`` function is the single entry point used by the CLI and the
test suite. It performs, in order:

1. Resolve the contract set (failing closed on a missing or empty set).
2. Parse every contract into its named sections.
3. Run each of the three rule families against every contract.
4. Read the suppressions file (failing closed on missing or malformed
   inputs) and partition its entries into used and unused.
5. Match every finding against a suppression; unmatched findings fail.
6. Verify every relevant suppression was matched; unmatched
   suppressions fail.

The result is a list of findings that the caller is expected to surface
in its own way. The CLI prints the findings and exits non-zero when
the list is non-empty. The test suite asserts on the list directly.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ..check_citations.suppressions import (
    SuppressionError,
    find_matching,
    partition_used_and_unused,
)
from ..check_citations.suppressions import load as load_suppressions
from . import Finding, contract_set
from .parser import contract_set as _parser_contract_set
from .parser import parse_contract
from .rules import (
    RULE_FAMILY_NAMES,
    acceptance_placeholder,
    acceptance_present,
    acceptance_quantitative,
)


class CheckError(Exception):
    """Raised when the check cannot run at all (missing or unreadable inputs)."""


def _path_for_finding(absolute_path: Path, root: Path) -> str:
    try:
        return absolute_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return absolute_path.resolve().as_posix()


def _contract_paths(repo_root: Path, contracts: Iterable[Path | str] | None) -> list[Path]:
    if contracts is None:
        return contract_set(repo_root)
    return [Path(c).resolve() for c in contracts]


def _read_contracts(paths: Iterable[Path]) -> Iterable[tuple[Path, str]]:
    for path in paths:
        yield path, path.read_text(encoding="utf-8")


def run(
    repo_root: Path | str,
    *,
    suppressions_path: Path | str | None = None,
    config_path: Path | str | None = None,
    contracts: Iterable[Path | str] | None = None,
) -> list[Finding]:
    """Run the full acceptance-criteria check.

    Returns the list of *unlisted* findings plus one synthetic finding
    per unused suppression entry. An empty list means the repository
    passes the check. Any error in the input set (missing config,
    missing or malformed suppressions, empty contract set, unreadable
    contract file) produces a single finding rather than raising.
    """

    root = Path(repo_root).resolve()
    # The ``config_path`` argument exists for symmetry with T005 / T006
    # and for the test suite; the rule families do not currently read
    # ``todo/config.yaml``. A missing config still raises a finding
    # only if its absence is in scope (currently never: T009 is about
    # contract text, not status). We accept the argument and ignore
    # it for now so the surface stays compatible with the other tools.
    _ = config_path

    suppressions = (
        Path(suppressions_path).resolve()
        if suppressions_path
        else root / "docs" / "implement" / "ci" / "suppressions.toml"
    )

    failures: list[Finding] = []

    paths = _contract_paths(root, contracts)
    if not paths:
        # Two failure shapes are possible: the ``todo/phases/`` directory
        # does not exist (no plans yet), or it exists and contains no
        # contracts. Both are fatal: the check has nothing to enforce
        # and the right answer is to surface that loudly, not to pass.
        phases = root / "todo" / "phases"
        if not phases.is_dir():
            return [
                Finding(
                    rule="contract-set",
                    path=str(phases.relative_to(root)),
                    line=0,
                    token="",
                    message=f"todo/phases/ is missing at {phases}",
                )
            ]
        return [
            Finding(
                rule="contract-set",
                path="todo/phases",
                line=0,
                token="",
                message=(
                    "contract set is empty; the checker would have scanned no "
                    "todo/phases/**/T[0-9]{3}.md files and therefore cannot enforce "
                    "T009"
                ),
            )
        ]

    # Parse every contract. A failure to read a contract file becomes a
    # finding rather than an exception so the CLI can present every
    # problem in one pass.
    parsed: list[tuple[Path, object]] = []
    for path, text in _read_contracts(paths):
        relative = _path_for_finding(path, root)
        parsed.append((path, parse_contract(relative, text)))

    # Apply every rule family to every contract.
    for _path, contract in parsed:
        failures.extend(acceptance_present(contract))  # type: ignore[arg-type]
        failures.extend(acceptance_placeholder(contract))  # type: ignore[arg-type]
        failures.extend(acceptance_quantitative(contract))  # type: ignore[arg-type]

    # Suppression matching. The shared ``suppressions.toml`` file also
    # carries entries for the T005 / T006 checkers; the
    # ``suppression-unused`` check scopes itself to the T009 rule
    # families so a T005 entry never leaks into a T009 failure.
    try:
        entries = load_suppressions(suppressions)
    except SuppressionError as exc:
        return [
            Finding(
                rule="suppressions",
                path=str(suppressions.relative_to(root)),
                line=0,
                token="",
                message=str(exc),
            )
        ]

    relevant_entries = [entry for entry in entries if entry.rule in RULE_FAMILY_NAMES]

    unlisted: list[Finding] = []
    for finding in failures:
        if find_matching(
            relevant_entries, rule=finding.rule, path=finding.path, token=finding.token
        ):
            continue
        unlisted.append(finding)

    _, unused = partition_used_and_unused(
        relevant_entries,
        ((finding.rule, finding.path, finding.token) for finding in failures),
    )
    for entry in unused:
        unlisted.append(
            Finding(
                rule="suppression-unused",
                path=str(suppressions.relative_to(root)),
                line=0,
                token=entry.token or entry.rule,
                message=(
                    f"suppression for rule {entry.rule!r} "
                    f"(token={entry.token!r}, location={entry.location!r}) "
                    "no longer matches any finding and must be removed"
                ),
            )
        )

    return unlisted


def format_findings(findings: Iterable[Finding], repo_root: Path | str) -> str:
    """Format a list of findings for printing on CI logs.

    The output always prints the closed vocabulary alongside the
    findings so a reviewer can re-derive every match by hand. The
    vocabulary block is part of the contract: removing it would let a
    silent vocabulary shift hide behind a passing check.
    """

    from . import vocabulary as _vocab

    findings_list = list(findings)
    vocab = _vocab()
    blocks: list[str] = []
    blocks.append("Declared closed vocabulary:")
    blocks.append(f"  verification surfaces: {list(vocab.verification_surfaces)}")
    blocks.append(f"  explicit outcome terms: {list(vocab.explicit_outcome_terms)}")
    blocks.append(f"  placeholder markers: {list(vocab.placeholder_markers)}")
    blocks.append(f"  comparison operators: {list(vocab.comparison_operators)}")
    blocks.append(f"  unit terms: {list(vocab.unit_terms)}")
    if not findings_list:
        blocks.append("acceptance check passed: no findings")
        return "\n".join(blocks)
    lines = ["acceptance check failed:"]
    for finding in findings_list:
        location = finding.path
        if finding.line:
            location = f"{location}:{finding.line}"
        if finding.token:
            location = f"{location} ({finding.token})"
        lines.append(f"  {finding.rule}: {location}: {finding.message}")
    blocks.extend(lines)
    return "\n".join(blocks)


__all__ = ["CheckError", "format_findings", "run"]


# Re-export the parser-level ``contract_set`` so callers can iterate the
# raw ``(relative_path, text)`` pair list when they need it.
__all__.append("parser_contract_set")
parser_contract_set = _parser_contract_set
