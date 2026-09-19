"""Deterministic offline acceptance-criteria checker (T009).

The checker parses every ``todo/phases/**/T[0-9]{3}.md`` contract with
the standard library and never imports or executes any project module.
It fails closed on three rule families:

1. ``acceptance-present`` — a contract without an ``## Acceptance``
   section, with an empty one, or with one that names no verification
   surface, explicit pass/fail, rejection, refusal or invalid-input
   condition.
2. ``acceptance-placeholder`` — an ``## Acceptance`` section that
   contains any of the closed placeholder markers (``TBD``, ``TODO``,
   ``to be determined``, ``as appropriate``, ``if possible``).
3. ``acceptance-quantitative`` — a comparison phrase (closed operator
   + number with a unit or percent) that appears in ``## Deliverables``
   but is not required by ``## Acceptance``.

Findings are read together with the closed exception list in
``docs/implement/ci/suppressions.toml`` (the same file T005 and T006
share). Every entry must match at least one finding; every unlisted
finding fails the check.

The vocabulary that defines what a verification surface, a placeholder
marker or a comparison phrase is lives in
:mod:`tools.check_acceptance.vocabulary`; nothing in this module may
narrow it.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .types import Finding, Vocabulary
from .vocabulary import (
    COMPARISON_OPERATORS,
    EXPLICIT_OUTCOME_TERMS,
    PLACEHOLDER_MARKERS,
    UNIT_TERMS,
    VERIFICATION_SURFACES,
)


def vocabulary() -> Vocabulary:
    """Return the committed closed vocabulary."""

    return Vocabulary(
        verification_surfaces=VERIFICATION_SURFACES,
        explicit_outcome_terms=EXPLICIT_OUTCOME_TERMS,
        placeholder_markers=PLACEHOLDER_MARKERS,
        comparison_operators=COMPARISON_OPERATORS,
        unit_terms=UNIT_TERMS,
    )


def contract_set(repo_root: Path | str) -> list[Path]:
    """Return the declared contract set in a stable order.

    The set covers every ``todo/phases/**/T[0-9]{3}.md`` file in
    deterministic sort order. An empty result is a fatal input: the
    checker cannot assert anything against an empty contract set, and
    the rule ``contract-set-empty`` makes that visible.
    """

    from .parser import discover_contracts

    root = Path(repo_root).resolve()
    return discover_contracts(root)


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
    contract file) produces a single finding rather than raising, so the
    caller can present every failure in the same shape.
    """

    from .checker import run as _run

    return _run(
        repo_root,
        suppressions_path=suppressions_path,
        config_path=config_path,
        contracts=contracts,
    )


__all__ = [
    "Finding",
    "Vocabulary",
    "contract_set",
    "run",
    "vocabulary",
]
