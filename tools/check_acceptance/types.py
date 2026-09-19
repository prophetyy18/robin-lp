"""Shared dataclasses for the T009 acceptance-criteria checker.

The dataclasses live in their own module so the rule families can
import them without depending on :mod:`tools.check_acceptance`'s
``__init__`` module. The package ``__init__`` re-exports them so the
public API stays in one place.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    """A single contract clause that fails one of the three rule families.

    ``rule`` is the dotted name of the rule family. ``path`` is the
    repository-relative path of the contract. ``line`` is the 1-indexed
    line where the offending clause appears (``0`` when the clause is
    the section itself). ``token`` is the exact substring the scanner
    matched (empty when the failure is a missing section). ``message``
    is a human-readable explanation suitable for printing with the
    finding list.
    """

    rule: str
    path: str
    line: int
    token: str
    message: str

    def key(self) -> tuple[str, str, str]:
        """Stable key used to match a finding against a suppression entry."""

        return (self.rule, self.path, self.token)


@dataclass(frozen=True)
class Vocabulary:
    """The committed closed vocabulary, returned for printing alongside findings.

    The orchestrator prints the vocabulary with every finding so a
    reviewer can re-derive a finding by hand without having to inspect
    the checker's source.
    """

    verification_surfaces: tuple[str, ...]
    explicit_outcome_terms: tuple[str, ...]
    placeholder_markers: tuple[str, ...]
    comparison_operators: tuple[str, ...]
    unit_terms: tuple[str, ...]


__all__ = ["Finding", "Vocabulary"]
