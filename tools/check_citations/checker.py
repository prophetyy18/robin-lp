"""Top-level orchestration of the citation check.

The ``run`` function is the single entry point used by the CLI and the
test suite. It performs, in order:

1. Resolve the document set (failing closed on a missing or empty set).
2. Tokenise every document.
3. Run each resolver against the tokens.
4. Read the suppressions file (failing closed on missing or malformed
   inputs).
5. Match every finding against a suppression; unmatched findings fail.
6. Verify every suppression was matched; unmatched suppressions fail.

The result is a list of findings that the caller is expected to surface in
its own way. The CLI prints the findings and exits non-zero when the list
is non-empty. The test suite asserts on the list directly.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from . import Finding, document_set, resolvers
from .suppressions import (
    SuppressionError,
    find_matching,
    partition_used_and_unused,
)
from .suppressions import (
    load as load_suppressions,
)
from .tokens import Token, iter_tokens


class CheckError(Exception):
    """Raised when the check cannot run at all (missing or unreadable inputs)."""


def run(
    repo_root: Path | str,
    *,
    suppressions_path: Path | str | None = None,
    config_path: Path | str | None = None,
    documents: Iterable[Path | str] | None = None,
) -> list[Finding]:
    """Run the full citation check.

    The returned list contains every *unmatched* finding, plus one synthetic
    finding per unused suppression. An empty list means the repository
    passes the check. Any error in the input set (missing config, missing
    suppressions file, empty document set, malformed suppressions) produces
    a single finding rather than raising, so the caller can present the
    failure in the same shape as every other check failure.
    """

    root = Path(repo_root).resolve()
    config = Path(config_path).resolve() if config_path else root / "todo" / "config.yaml"
    suppressions = (
        Path(suppressions_path).resolve()
        if suppressions_path
        else root / "docs" / "implement" / "ci" / "suppressions.toml"
    )
    src_root = (root / "src").resolve()

    failures: list[Finding] = []

    if not config.is_file():
        return [
            Finding(
                rule="config",
                path=str(config.relative_to(root)),
                line=0,
                token="",
                message=f"todo/config.yaml is missing at {config}",
            )
        ]
    if not (src_root / "robinhood_lp").is_dir():
        return [
            Finding(
                rule="module-path",
                path=str((src_root / "robinhood_lp").relative_to(root)),
                line=0,
                token="",
                message=f"src/robinhood_lp/ is missing at {src_root / 'robinhood_lp'}",
            )
        ]

    try:
        if documents is None:
            document_paths = [Path(p).resolve() for p in document_set(root)]
        else:
            document_paths = [Path(p).resolve() for p in documents]
    except (OSError, ValueError) as exc:
        return [
            Finding(
                rule="document-set",
                path="",
                line=0,
                token="",
                message=f"document set could not be enumerated: {exc}",
            )
        ]
    if not document_paths:
        return [
            Finding(
                rule="document-set",
                path="",
                line=0,
                token="",
                message="document set is empty; the checker would have scanned nothing",
            )
        ]

    tokens_by_doc: dict[Path, list[Token]] = {}
    for doc in document_paths:
        try:
            tokens_by_doc[doc] = list(iter_tokens(doc))
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(
                Finding(
                    rule="document-read",
                    path=str(doc.relative_to(root)),
                    line=0,
                    token="",
                    message=f"document could not be read: {exc}",
                )
            )

    def _path_for_finding(absolute_path: Path) -> str:
        try:
            return absolute_path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return absolute_path.resolve().as_posix()

    all_tokens: list[Token] = [token for tokens in tokens_by_doc.values() for token in tokens]

    tokens_outside_architecture = [
        token
        for token in all_tokens
        if not token.path.name.endswith("ARCHITECTURE.md") or "architecture" not in str(token.path)
    ]
    architecture = root / "docs" / "spec" / "architecture" / "ARCHITECTURE.md"

    failures.extend(
        resolvers.resolve_module_paths(
            tokens_outside_architecture, src_root=src_root, path_for_finding=_path_for_finding
        )
    )
    failures.extend(
        resolvers.resolve_member_accesses(
            tokens_outside_architecture, src_root=src_root, path_for_finding=_path_for_finding
        )
    )
    failures.extend(
        resolvers.resolve_task_ids(
            tokens_outside_architecture, config_path=config, path_for_finding=_path_for_finding
        )
    )
    failures.extend(
        resolvers.resolve_requirement_ids(
            tokens_outside_architecture, repo_root=root, path_for_finding=_path_for_finding
        )
    )
    failures.extend(resolvers.resolve_phase_tasks(root, config_path=config))
    if architecture.is_file():
        failures.extend(
            resolvers.resolve_architecture_section22(
                architecture,
                config_path=config,
                src_root=src_root,
                repo_root=root,
                path_for_finding=_path_for_finding,
            )
        )

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

    unlisted: list[Finding] = []
    for finding in failures:
        if find_matching(entries, rule=finding.rule, path=finding.path, token=finding.token):
            continue
        unlisted.append(finding)

    # Filter suppressions to entries that this checker produces. T006
    # (the import-graph checker) adds entries with rules such as
    # ``layer-direction`` to the same ``suppressions.toml`` file;
    # T005 is not the contract owner for those entries and they must
    # not appear here as "unused" findings.
    relevant_entries = [entry for entry in entries if entry.rule in _T005_RULES]

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
    """Format a list of findings for printing on CI logs."""

    lines = []
    for finding in findings:
        location = finding.path
        if finding.line:
            location = f"{location}:{finding.line}"
        if finding.token:
            location = f"{location} ({finding.token})"
        lines.append(f"{finding.rule}: {location}: {finding.message}")
    if not lines:
        return "citation check passed: no findings"
    return "citation check failed:\n" + "\n".join(lines)


__all__ = ["CheckError", "format_findings", "run"]


#: Rule names produced by the T005 citation checker. The shared
#: ``suppressions.toml`` file also carries entries for the T006
#: import-graph checker; the ``suppression-unused`` check scopes
#: itself to these rules so a T006 entry never leaks into a T005
#: failure.
_T005_RULES: frozenset[str] = frozenset(
    {
        "module-path",
        "member-access",
        "task-id",
        "requirement-id",
        "phase-tasks",
        "architecture-section22",
    }
)
