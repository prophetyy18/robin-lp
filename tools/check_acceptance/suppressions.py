"""Read and validate ``docs/implement/ci/suppressions.toml``.

A suppression entry has the four required fields ``rule``, ``reason``,
``owner`` and ``expiry``, plus an optional ``location`` that pinpoints
the finding to suppress and an optional ``token`` whose value is the
exact token to suppress.

A suppression matches a finding when ``rule`` matches, the ``token``
agrees (either both empty, or both equal), and ``location`` is either
empty or appears in the document path or ``path:line`` of the finding.

This loader is deliberately stdlib-only: the T009 contract forbids the
checker from importing or executing a project module at check time, so
the matching logic is re-implemented here instead of being imported
from :mod:`tools.check_citations.suppressions`. The on-disk schema is
identical to the one the citation checker uses; only the Python
loader changes so the existing entries in
``docs/implement/ci/suppressions.toml`` keep parsing unchanged.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REQUIRED_FIELDS: tuple[str, ...] = ("rule", "reason", "owner", "expiry")


@dataclass(frozen=True)
class Suppression:
    """A single suppression entry from ``suppressions.toml``.

    ``token`` is the exact token the suppression covers (empty string means
    *any* token matching the rule and path). ``location`` is an opaque
    substring that must appear in the document path or ``path:line`` of the
    finding (empty string means *any* location).
    """

    rule: str
    reason: str
    owner: str
    expiry: str
    location: str = ""
    token: str = ""

    def matches(self, *, rule: str, path: str, token: str) -> bool:
        if self.rule != rule:
            return False
        if self.token and self.token != token:
            return False
        return not (self.location and self.location not in path)


class SuppressionError(Exception):
    """Raised when the suppressions file is missing, unreadable, or malformed."""


def load(path: Path) -> list[Suppression]:
    """Load and validate the suppression registry.

    The file must be readable TOML. Two layouts are accepted:

    * an inline ``suppressions = [{...}, {...}]`` array, or
    * a ``[[suppression]]`` array-of-tables block (the canonical form
      documented in the registry's preamble).

    Every entry must carry the four required fields. The load is strict on
    purpose: a missing field, an unknown field, or a parse error fails
    closed instead of being silently skipped.
    """

    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise SuppressionError(f"suppressions file missing: {path}") from exc
    except OSError as exc:
        raise SuppressionError(f"suppressions file unreadable: {path} ({exc})") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise SuppressionError(f"suppressions file invalid: {path} ({exc})") from exc

    entries: list[dict[str, object]]
    if "suppressions" in data:
        if not isinstance(data["suppressions"], list):
            raise SuppressionError(f"suppressions file 'suppressions' field must be a list: {path}")
        entries = list(data["suppressions"])
    elif "suppression" in data:
        if not isinstance(data["suppression"], list):
            raise SuppressionError(f"suppressions file 'suppression' field must be a list: {path}")
        entries = list(data["suppression"])
    else:
        raise SuppressionError(
            f"suppressions file missing 'suppressions' or 'suppression' array: {path}"
        )

    allowed_fields = set(REQUIRED_FIELDS) | {"location", "token"}
    out: list[Suppression] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SuppressionError(f"suppressions[{index}] must be a table: {path}")
        missing = [field for field in REQUIRED_FIELDS if field not in entry]
        if missing:
            raise SuppressionError(f"suppressions[{index}] missing fields {missing}: {path}")
        unknown = sorted(set(entry) - allowed_fields)
        if unknown:
            raise SuppressionError(f"suppressions[{index}] has unknown fields {unknown}: {path}")
        out.append(
            Suppression(
                rule=str(entry["rule"]),
                reason=str(entry["reason"]),
                owner=str(entry["owner"]),
                expiry=str(entry["expiry"]),
                location=str(entry.get("location", "")),
                token=str(entry.get("token", "")),
            )
        )
    return out


def find_matching(
    suppressions: Iterable[Suppression], *, rule: str, path: str, token: str
) -> Suppression | None:
    """Return the first suppression that covers the given finding."""

    for entry in suppressions:
        if entry.matches(rule=rule, path=path, token=token):
            return entry
    return None


def partition_used_and_unused(
    suppressions: Iterable[Suppression], findings: Iterable[tuple[str, str, str]]
) -> tuple[list[Suppression], list[Suppression]]:
    """Split ``suppressions`` into used and unused entries.

    ``findings`` is an iterable of ``(rule, path, token)`` triples. A
    suppression is *used* when at least one finding matches it; otherwise
    it is *unused* and must itself fail the check.
    """

    materialised = list(suppressions)
    used: set[int] = set()
    for rule, path, token in findings:
        for index, entry in enumerate(materialised):
            if index in used:
                continue
            if entry.matches(rule=rule, path=path, token=token):
                used.add(index)
                break
    used_entries = [entry for index, entry in enumerate(materialised) if index in used]
    unused_entries = [entry for index, entry in enumerate(materialised) if index not in used]
    return used_entries, unused_entries


__all__ = [
    "REQUIRED_FIELDS",
    "Suppression",
    "SuppressionError",
    "find_matching",
    "load",
    "partition_used_and_unused",
]
