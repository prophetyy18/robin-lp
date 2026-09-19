"""Parser for task-contract Markdown files (T009).

The contract format is a small, well-defined subset of Markdown:

- A top-level ``# Title`` line is the contract's title.
- ``> Phase:`` and ``> Contract status:`` quote blocks are status lines.
- Section headers begin with ``## `` and are case-sensitive; the
  contract always uses ``## Acceptance`` and ``## Deliverables``
  (the two sections the checker cares about).

The parser splits a contract into its named sections so the rule
modules can inspect the ``## Acceptance`` text alone (and the
``## Deliverables`` text alone) without leaking any context they do not
need. It is the single source of truth for what a section is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: The two sections the T009 checker reads.
ACCEPTANCE_HEADER = "## Acceptance"
DELIVERABLES_HEADER = "## Deliverables"

#: A header line is ``## `` followed by any non-empty text. The text
#: is taken verbatim (without the trailing newline) because the
#: contract always spells section names with consistent
#: capitalisation; the lookup helpers compare the captured title
#: against the canonical constants verbatim.
_HEADER = re.compile(r"^##\s+(?P<title>[^\n]+?)[ \t]*\n", re.MULTILINE)


@dataclass(frozen=True)
class Contract:
    """A single parsed task contract.

    ``path`` is the repository-relative path of the contract. ``text``
    is the raw source. ``title`` is the first ``# Title`` line.
    ``sections`` is the parsed section list in source order; each entry
    maps the verbatim ``## Name`` header to the text that follows it up
    to the next header (or to the end of the file). ``acceptance`` and
    ``deliverables`` are convenience aliases for the two sections the
    checker inspects.
    """

    path: str
    text: str
    title: str
    sections: tuple[tuple[str, str], ...]

    def section(self, header: str) -> str | None:
        """Return the text of the section whose header equals ``header``.

        ``None`` means the section is missing; an empty string means
        the section was present but had no body. The rule modules
        treat the two cases differently on purpose: an empty
        Acceptance section is still a finding, but the absence of an
        Acceptance section is a separate, more severe finding.
        """

        for name, body in self.sections:
            if name == header:
                return body
        return None

    @property
    def acceptance(self) -> str | None:
        return self.section(ACCEPTANCE_HEADER)

    @property
    def deliverables(self) -> str | None:
        return self.section(DELIVERABLES_HEADER)


def _title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    return ""


def parse_contract(path: str, text: str) -> Contract:
    """Parse one contract file.

    The parser is permissive: it never raises. A contract that lacks a
    title or has no recognised section headers is still returned; the
    rule modules inspect the result and decide what is missing.

    The stored ``header`` for each section is the captured title text
    (e.g. ``Acceptance``), reconstructed as the canonical
    ``## <Title>`` form so callers can compare against
    :data:`ACCEPTANCE_HEADER` and :data:`DELIVERABLES_HEADER` verbatim.
    """

    sections: list[tuple[str, str]] = []
    cursor = 0
    while cursor < len(text):
        match = _HEADER.search(text, cursor)
        if match is None:
            break
        header_title = match.group("title")
        header = f"## {header_title}"
        body_start = match.end()
        next_match = _HEADER.search(text, body_start)
        body_end = next_match.start() if next_match is not None else len(text)
        body = text[body_start:body_end]
        sections.append((header, body))
        cursor = body_end
    return Contract(
        path=path,
        text=text,
        title=_title(text),
        sections=tuple(sections),
    )


def discover_contracts(repo_root: Path) -> list[Path]:
    """Return every ``todo/phases/**/T[0-9]{3}.md`` file in deterministic order.

    The contract name is exactly three ASCII digits in square brackets:
    ``T000.md`` through ``T999.md``. A phase may hold any number of
    contracts; an empty result is a fatal finding (the rule ``empty-set``
    handles it).
    """

    root = Path(repo_root).resolve()
    phases = root / "todo" / "phases"
    if not phases.is_dir():
        return []
    out: list[Path] = []
    for candidate in sorted(phases.rglob("T[0-9][0-9][0-9].md")):
        if candidate.is_file():
            out.append(candidate.resolve())
    return out


def read_contract(path: Path) -> tuple[str, str]:
    """Read a contract file and return ``(relative_path, text)``.

    The relative path uses forward slashes and is relative to ``path``'s
    nearest ``todo`` ancestor; that shape matches the relative paths
    the report prints.
    """

    text = path.read_text(encoding="utf-8")
    relative = path.as_posix()
    return relative, text


def contract_set(repo_root: Path, contracts: Iterable[Path] | None = None) -> list[tuple[str, str]]:
    """Return the parsed ``(relative_path, text)`` pair list.

    When ``contracts`` is None the function discovers them via
    :func:`discover_contracts`; otherwise the caller is responsible for
    the list (used by tests that build a miniature repository).
    """

    root = Path(repo_root).resolve()
    paths = list(contracts) if contracts is not None else discover_contracts(root)
    return [read_contract(path) for path in paths]


__all__ = [
    "ACCEPTANCE_HEADER",
    "Contract",
    "DELIVERABLES_HEADER",
    "contract_set",
    "discover_contracts",
    "parse_contract",
    "read_contract",
]
