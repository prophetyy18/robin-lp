"""Build the ``robinhood_lp`` import graph with the stdlib ``ast`` (T006).

The graph is the input to every other rule in the checker:

- the lower-to-higher rule walks every edge and compares the two
  endpoints' layer ranks (see ``layer_map.TIER_ORDER``);
- the cycle rule performs a depth-first search over the package
  nodes;
- the unseeded-clock rule inspects each module's import set for
  ``time``, ``random`` and the ``datetime`` family — modules in the
  ``protocol``, ``replay``, ``features``, ``strategy`` and ``backtest``
  tiers must never import them;
- the unclassified-module rule walks every module that appears in
  the edge list and demands a classification from ``layer_map``.

The parser is deliberately narrow:

- only ``Import`` and ``ImportFrom`` nodes are read;
- only modules that start with ``robinhood_lp`` (or resolve to one
  through a relative import) are kept, because the rule applies to
  the project's own module graph;
- relative imports are resolved against the importing module's
  package path so a ``from .sibling import X`` in
  ``robinhood_lp.discovery.registry`` becomes
  ``robinhood_lp.discovery.sibling``;
- ``ast.parse`` is called without ``compile`` so nothing is executed;
  any syntax error is surfaced as a fatal finding rather than raised.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Modules whose import would inject an unseeded clock into a
# deterministic path. ``datetime`` is intentionally included because the
# ADR forbids ``datetime.now()`` — the only way to obtain it is via an
# import of the ``datetime`` module or a ``from datetime import ...``
# statement. ``time``, ``random`` and the private ``_strptime_time``
# helper share the same intent.
UNSEEDED_CLOCK_MODULES: frozenset[str] = frozenset(
    {
        "time",
        "random",
        "datetime",
        # ``secrets`` is *not* in this set on purpose: it sources
        # entropy from the OS and is permitted in strategy / backtest
        # only when the deterministic-clock rule does not require
        # seeding. ADR-006 reserves the right to add it explicitly
        # later, so the checker does not pre-empt that decision.
    }
)

# Tier names that the ADR forbids from importing the unseeded-clock
# modules above. The names match ``layer_map.TIER_ORDER`` exactly.
CLOCK_FORBIDDEN_TIERS: frozenset[str] = frozenset(
    {
        "protocol/domain",
        "reconstruction",
        "features",
        "strategy",
        "backtest",
    }
)


@dataclass(frozen=True)
class Edge:
    """A single ``from -> to`` import edge between two project modules.

    Both endpoints are dotted module paths (``robinhood_lp.*``); the
    line number is the 1-indexed line of the import statement in the
    source file. The same edge can appear multiple times when the
    importing module has more than one ``from X import ...`` line; the
    checker dedupes by ``(source, target)`` for cycle / lower-to-higher
    checks but preserves the line numbers for the finding report.
    """

    source: str
    target: str
    line: int


@dataclass(frozen=True)
class Module:
    """One parsed Python file under ``src/robinhood_lp/``."""

    path: str  # dotted module path (e.g. ``robinhood_lp.discovery.registry``)
    file: Path  # absolute filesystem path
    imports: tuple[Edge, ...]
    clock_imports: tuple[tuple[str, int], ...]  # (module, line) for time/random/datetime
    parse_error: str | None


def _module_path_for(file: Path, src_root: Path) -> str:
    """Convert a ``.py`` file path to a dotted module name.

    ``src_root`` must point at ``src`` so that the relative path
    ``robinhood_lp/discovery/registry.py`` becomes
    ``robinhood_lp.discovery.registry``. Package ``__init__`` files
    use the dotted path of the package plus an explicit ``.__init__``
    suffix so the layer map can distinguish package façades (which
    aggregate across tiers) from sibling modules that share the
    package's runtime name.
    """

    relative = file.resolve().relative_to(src_root.resolve()).as_posix()
    parts = relative.split("/")
    if parts[-1] == "__init__.py":
        parts[-1] = "__init__"
    else:
        parts[-1] = parts[-1][:-3]  # strip ".py"
    return ".".join(parts)


def _resolve_relative(level: int, module: str | None, package: str) -> str:
    """Resolve a ``from . import X`` / ``from ..pkg import X`` style import.

    ``level`` is the number of leading dots in the import statement.
    ``module`` is the dotted module name that follows the dots (or
    ``None`` for a plain ``from . import X``). ``package`` is the
    importing module's dotted name without the trailing leaf
    (``robinhood_lp.discovery`` for the file
    ``robinhood_lp/discovery/registry.py``).
    """

    base = package.split(".")
    # Drop one segment per leading dot beyond the first; one dot means
    # "the current package" so ``base`` already represents the right
    # parent. Python's semantics: ``level=1`` → current package,
    # ``level=2`` → parent of current package, etc.
    if level > len(base):
        raise ValueError(
            f"relative import level {level} exceeds package depth {len(base)} for {package!r}"
        )
    base = base[: len(base) - (level - 1)]
    if module:
        return ".".join(base + module.split("."))
    return ".".join(base)


def _normalise_target(
    *, level: int, module: str | None, package: str, source_module: str
) -> str | None:
    """Return the absolute dotted module name an import resolves to.

    Returns ``None`` when the import targets the stdlib / a third
    party. Relative imports are resolved against the importing
    module's package; absolute imports whose top-level package is
    not ``robinhood_lp`` are dropped.
    """

    if level > 0:
        try:
            return _resolve_relative(level, module, package)
        except ValueError:
            return None
    if module is None:
        # ``import X`` with no further attributes resolves to the
        # top-level package. We keep only project packages.
        return None
    if module == source_module:
        # Self-imports (rare but legal) carry no dependency.
        return None
    if not module.startswith("robinhood_lp"):
        return None
    return module


def parse_module(file: Path, *, src_root: Path, package: str | None = None) -> Module:
    """Parse a single ``.py`` file and return its edges + clock imports.

    ``package`` is the dotted name of the module's enclosing package;
    it is used to resolve relative imports. When ``None``, it is
    derived from the file's position under ``src_root``.
    """

    if package is None:
        package_path = _module_path_for(file, src_root)
        package = ".".join(package_path.split(".")[:-1])

    path = _module_path_for(file, src_root)
    try:
        source = file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return Module(path=path, file=file, imports=(), clock_imports=(), parse_error=str(exc))

    edges: list[Edge] = []
    clocks: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            line = node.lineno
            for alias in node.names:
                target = _normalise_target(
                    level=0, module=alias.name, package=package, source_module=path
                )
                if target is not None:
                    edges.append(Edge(source=path, target=target, line=line))
                if alias.name in UNSEEDED_CLOCK_MODULES:
                    clocks.append((alias.name, line))
        elif isinstance(node, ast.ImportFrom):
            line = node.lineno
            target = _normalise_target(
                level=node.level or 0,
                module=node.module,
                package=package,
                source_module=path,
            )
            if target is not None:
                edges.append(Edge(source=path, target=target, line=line))
            # An unseeded-clock import is recorded regardless of
            # whether the target is a project module, so a
            # ``from time import sleep`` in a forbidden tier still
            # fires the rule even though ``time`` is not a
            # ``robinhood_lp`` package.
            if node.level == 0 and node.module in UNSEEDED_CLOCK_MODULES:
                clocks.append((node.module, line))

    return Module(
        path=path,
        file=file,
        imports=tuple(edges),
        clock_imports=tuple(clocks),
        parse_error=None,
    )


def parse_modules(files: Iterable[Path], *, src_root: Path) -> list[Module]:
    """Parse every file in ``files`` and return a sorted list of modules.

    Sorting on the dotted path keeps the edge list deterministic
    across runs; it also makes the failure output reproducible when
    the same repository is scanned twice.
    """

    out = [parse_module(file, src_root=src_root) for file in files]
    out.sort(key=lambda module: module.path)
    return out


def edges(modules: Iterable[Module]) -> list[Edge]:
    """Return every edge in stable order.

    Edges are sorted by ``(source, target, line)`` so the raw edge
    list in the failure output is byte-equivalent across repeated
    runs.
    """

    out: list[Edge] = []
    for module in modules:
        out.extend(module.imports)
    out.sort(key=lambda edge: (edge.source, edge.target, edge.line))
    return out


def find_cycle(modules: Iterable[Module]) -> tuple[str, ...] | None:
    """Return the first import cycle or ``None``.

    Cycles are reported at the *package* granularity: a cycle between
    ``robinhood_lp.discovery`` and ``robinhood_lp.storage`` is a
    single cycle even when the underlying modules cycle through many
    files. The package view is the one ARCHITECTURE.md §2.1 uses.

    Tarjan's algorithm is overkill here; an iterative DFS that walks
    the directed graph of packages is enough for the handful of
    modules the project ships. The first cycle found in canonical
    sort order is returned so the output is deterministic.
    """

    adjacency: dict[str, set[str]] = {}
    for module in modules:
        for edge in module.imports:
            source_pkg = ".".join(edge.source.split(".")[:2])
            target_pkg = ".".join(edge.target.split(".")[:2])
            if source_pkg == target_pkg:
                continue
            adjacency.setdefault(source_pkg, set()).add(target_pkg)

    WHITE, GRAY, BLACK = 0, 1, 2
    colour: dict[str, int] = {node: WHITE for node in adjacency}
    parent: dict[str, str | None] = {node: None for node in adjacency}

    def cycle_from(node: str) -> tuple[str, ...] | None:
        colour[node] = GRAY
        for neighbour in sorted(adjacency.get(node, ())):
            if colour.get(neighbour, WHITE) == GRAY:
                # Reconstruct the cycle from the parent pointers.
                cycle = [neighbour, node]
                cursor = node
                while parent.get(cursor) is not None and cursor != neighbour:
                    cursor = parent[cursor]  # type: ignore[assignment]
                    if cursor is None:
                        break
                    cycle.append(cursor)
                    if cursor == neighbour:
                        break
                cycle.reverse()
                # Trim the suffix that is not part of the cycle.
                while len(cycle) > 1 and cycle[0] != cycle[-1]:
                    cycle.pop(0)
                return tuple(cycle)
            if colour.get(neighbour, WHITE) == WHITE:
                parent[neighbour] = node
                found = cycle_from(neighbour)
                if found is not None:
                    return found
        colour[node] = BLACK
        return None

    for node in sorted(adjacency):
        if colour[node] == WHITE:
            found = cycle_from(node)
            if found is not None:
                return found
    return None


__all__ = [
    "CLOCK_FORBIDDEN_TIERS",
    "Edge",
    "Module",
    "UNSEEDED_CLOCK_MODULES",
    "edges",
    "find_cycle",
    "parse_module",
    "parse_modules",
]
