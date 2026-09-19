"""Parse the §2.2 module-to-layer mapping from ARCHITECTURE.md (T006).

ARCHITECTURE.md §2.2 is a Markdown table whose rows bind a task
identifier to one or more module artefacts and a layer name. The
checker uses the same parser the T005 citation checker relies on, but
extracts a different shape: each row produces one or more
``(module, layer)`` pairs that the agreement check compares against
the layer map.

The parser is deliberately small and dependency-free:

- only the ``### 2.2`` section is read;
- the table is recognised by its leading/trailing ``|`` characters
  and the four-column header ``Phase | Task | Module | Layer``;
- module artefacts may be a backticked name (``robinhood_lp.x.y``) or
  a brace-expanded set (``robinhood_lp.storage.{a,b,c}``), matching
  the conventions the existing row text uses;
- task identifiers (``Txxx``) are validated against ``todo/config.yaml``
  so an unknown row is itself a fatal finding.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

# A dotted module name possibly followed by ``.{a,b,c}`` braces and
# possibly wrapped in backticks. Allows ``robinhood_lp.x.y`` and
# ``robinhood_lp.storage.{partition,manifest,reader,writer,measurement}``.
_MODULE = re.compile(
    r"`?(?P<head>robinhood_lp(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
    r"(?:\.\{(?P<set>[A-Za-z0-9_, ]+)\})?`?"
)
_TASK = re.compile(r"`?(T\d{3})`?")
_LINE = re.compile(r"line\s+(?P<line>\d+)")


@dataclass(frozen=True)
class ArchitectureRow:
    """One parsed row of the §2.2 table.

    ``modules`` is the list of artefact module names declared by the
    row. ``declared_layer`` is the raw layer string from the row
    (an alias such as ``presentation / ops``) — the agreement check
    canonicalises it via ``layer_map.canonical_layer`` before
    comparing with the map.
    """

    phase: str
    task: str
    modules: tuple[str, ...]
    declared_layer: str
    line: int


def _expand_token(token: str) -> list[str]:
    """Expand a brace-expanded module token into individual names.

    ``robinhood_lp.storage.{a,b,c}`` → ``['robinhood_lp.storage.a',
    'robinhood_lp.storage.b', 'robinhood_lp.storage.c']``.
    Tokens without braces produce a one-element list.
    """

    match = _MODULE.fullmatch(token.strip())
    if match is None:
        return []
    head = match.group("head")
    brace = match.group("set")
    if brace is None:
        return [head]
    return [f"{head}.{item.strip()}" for item in brace.split(",") if item.strip()]


def parse_section22(text: str) -> list[ArchitectureRow]:
    """Parse every row inside the ``### 2.2`` section of ``text``.

    The parser scans lines; once it sees ``### 2.2`` it enters the
    section and exits at the next ``### `` heading. Rows that do not
    match the four-column shape are skipped silently so prose between
    rows does not derail the parser.
    """

    rows: list[ArchitectureRow] = []
    in_section = False
    for line_number, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith("### 2.2"):
            in_section = True
            continue
        if in_section and raw.startswith("### "):
            break
        if not in_section or not raw.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in raw.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0] == "Phase" or cells[0].startswith("---"):
            continue
        phase = cells[0]
        task_match = _TASK.fullmatch(cells[1])
        if task_match is None:
            continue
        task = task_match.group(1)
        module_cell = cells[2]
        layer_cell = cells[3]
        module_tokens = [token.strip() for token in re.findall(r"`[^`]+`|[^`,\s]+", module_cell)]
        modules: list[str] = []
        for token in module_tokens:
            modules.extend(_expand_token(token))
        if not modules:
            continue
        rows.append(
            ArchitectureRow(
                phase=phase,
                task=task,
                modules=tuple(modules),
                declared_layer=layer_cell,
                line=line_number,
            )
        )
    return rows


def load_config_tasks(config_path: Path) -> dict[str, dict[str, object]]:
    """Load the ``tasks`` table from ``todo/config.yaml``.

    The file is valid JSON, so the stdlib ``json`` parser is enough
    — no PyYAML dependency.
    """

    data: dict[str, object] = json.loads(config_path.read_text(encoding="utf-8"))
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        return {}
    return tasks


def resolve_section22(
    rows: Iterable[ArchitectureRow],
    *,
    config_path: Path,
    path_for_finding: Callable[[Path], str],
) -> list[tuple[str, str, str, int, str]]:
    """Return the (module, declared_layer, source_path, line, message) tuples.

    Each tuple is one row of the §2.2 table after task validation. A
    row whose task identifier is not present in ``todo/config.yaml`` is
    returned with an ``unknown-task`` marker so the caller can report
    it as a fatal finding (T005 acceptance: an unparsable §2.2 row
    fails closed).
    """

    if not config_path.is_file():
        return [
            (
                "",
                "",
                "docs/spec/architecture/ARCHITECTURE.md",
                0,
                f"todo/config.yaml is missing at {config_path}",
            )
        ]
    tasks = load_config_tasks(config_path)
    out: list[tuple[str, str, str, int, str]] = []
    for row in rows:
        declared = row.declared_layer
        if row.task not in tasks:
            out.append(
                (
                    "",
                    declared,
                    "docs/spec/architecture/ARCHITECTURE.md",
                    row.line,
                    f"§2.2 row references task {row.task!r} which is not in todo/config.yaml",
                )
            )
            continue
        for module in row.modules:
            out.append(
                (
                    module,
                    declared,
                    "docs/spec/architecture/ARCHITECTURE.md",
                    row.line,
                    f"§2.2 row {row.task} declares {module!r} as {declared!r}",
                )
            )
    return out


__all__ = ["ArchitectureRow", "parse_section22", "load_config_tasks", "resolve_section22"]
