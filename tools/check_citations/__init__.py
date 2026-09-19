"""Deterministic offline citation checker (T005).

Resolves five kinds of backticked tokens against the repository:

1. Dotted ``robinhood_lp.*`` module/attribute paths under ``src/robinhood_lp/``.
2. Two-part ``Class.member`` accesses whose first part is a class, enum or
   module defined under ``src/robinhood_lp/`` and whose second part is not a
   source-file extension.
3. ``Txxx`` task identifiers in ``todo/config.yaml`` and the §2.2 row format
   of ``docs/spec/architecture/ARCHITECTURE.md``.
4. Requirement identifiers from the families owned by the Intent and Spec
   documents (``G-*``, ``ADM-*``, ``WEB-*``, ``ECO-*``, ``CTRL-*``, ``M-*``,
   ``DS-*``, ``R*``).
5. The Tasks list of every ``todo/phases/<phase>/README.md``, which must
   match exactly the task identifiers whose ``phase`` is that phase in
   ``todo/config.yaml``.

Findings (closed failures) are read together with the explicit exception list
in ``docs/implement/ci/suppressions.toml``. Every entry in that list must
match at least one finding; every unlisted finding fails the check.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    """A single citation that fails to resolve.

    ``rule`` is the dotted name of the resolver that produced the finding.
    ``path`` is the repository-relative document path. ``line`` is the
    1-indexed line where the offending token appears. ``token`` is the exact
    token (after brace expansion, when applicable). ``message`` is a
    human-readable explanation suitable for printing with the finding list.
    """

    rule: str
    path: str
    line: int
    token: str
    message: str

    def key(self) -> tuple[str, str, str]:
        """Stable key used to match a finding against a suppression entry."""

        return (self.rule, self.path, self.token)


def document_set(repo_root: Path | str) -> list[Path]:
    """Return the declared document set in a stable order.

    The set covers the README roots plus every ``docs/**/*.md``,
    ``todo/phases/**/*.md``, ``todo/README.md`` and ``todo/WORKFLOW.md``. The
    set is small enough to load fully into memory and is deterministic, so a
    missing or empty document set is itself a fatal failure.

    This helper is exposed for the test suite; the CLI entry point reuses it.
    """

    root = Path(repo_root).resolve()
    documents: list[Path] = []
    for name in ("README.md", "CLAUDE.md", "AGENTS.md"):
        candidate = root / name
        if candidate.is_file():
            documents.append(candidate)
    for sub in ("docs",):
        if (root / sub).is_dir():
            documents.extend(sorted((root / sub).rglob("*.md")))
    todo = root / "todo"
    for rel in ("README.md", "WORKFLOW.md"):
        candidate = todo / rel
        if candidate.is_file():
            documents.append(candidate)
    phases = todo / "phases"
    if phases.is_dir():
        documents.extend(sorted(phases.rglob("*.md")))
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in documents:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped


def run(
    repo_root: Path | str,
    *,
    suppressions_path: Path | str | None = None,
    config_path: Path | str | None = None,
    documents: Iterable[Path | str] | None = None,
) -> list[Finding]:
    """Run the full citation check.

    Returns the list of *unlisted* findings. An empty list means the check
    passed: every finding is covered by a suppression in ``suppressions.toml``,
    and every suppression matches at least one finding. Any other outcome
    (an unlisted finding, an unused suppression, a missing document, an
    unreadable input) produces a non-empty list.
    """

    from .checker import run as _run

    return _run(
        repo_root,
        suppressions_path=suppressions_path,
        config_path=config_path,
        documents=documents,
    )


__all__ = ["Finding", "document_set", "run"]
