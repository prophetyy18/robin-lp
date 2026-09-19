"""Orchestrator for the imported-graph dependency check (T006).

The orchestrator wires together the pieces:

- ``module_set`` enumerates every Python file under
  ``src/robinhood_lp/``; an empty set is a fatal finding.
- ``graph.parse_modules`` turns those files into ``Module`` records
  with edges and clock imports.
- ``layer_map.classify`` resolves each module (and each edge target)
  to a canonical tier name; unclassified modules are findings.
- ``graph.find_cycle`` looks for the first package-level cycle.
- ``architecture.parse_section22`` + ``resolve_section22`` produce
  one row per §2.2 entry; rows whose task identifier is unknown to
  ``todo/config.yaml`` fail closed.
- The agreement check compares each §2.2 row's declared layer to
  the layer the map returns for the same module; disagreement is
  fatal.
- Lower-to-higher edges (``tier(source) > tier(target)``) are
  reported as ``layer-direction`` findings; an edge that touches a
  non-tier layer (``platform-reserved``) is reported with the same
  rule and a token that the suppression registry can pin.
- The closed exception list is read from
  ``docs/implement/ci/suppressions.toml`` via
  ``tools.check_citations.suppressions``. Every entry must match at
  least one finding; an entry that matches nothing is fatal.

The orchestrator never raises: every error is converted into a
``Finding`` so the CLI can present all failures in one pass.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..check_citations.suppressions import (
    SuppressionError,
    find_matching,
    partition_used_and_unused,
)
from ..check_citations.suppressions import load as load_suppressions
from . import Finding, module_set
from . import graph as graph_module
from .architecture import parse_section22, resolve_section22
from .graph import CLOCK_FORBIDDEN_TIERS as _CLOCK_FORBIDDEN_TIERS
from .layer_map import (
    PLATFORM_RESERVED_LAYER,
    canonical_layer,
    classify,
    tier_index,
)


@dataclass(frozen=True)
class CheckResult:
    """The structured outcome of one checker run.

    ``findings`` are the unmatched findings; an empty list means the
    repository passes. ``edges`` is the full edge list the checker
    enforced, in deterministic order, so a reviewer can re-derive the
    graph independently. ``cycle`` is the first cycle the checker
    found, if any.
    """

    findings: list[Finding]
    edges: tuple[tuple[str, str, int], ...]
    cycle: tuple[str, ...] | None


def _path_for_finding(absolute_path: Path, root: Path) -> str:
    try:
        return absolute_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return absolute_path.resolve().as_posix()


def _edge_token(source: str, target: str) -> str:
    """Stable token used to match an edge against a suppression entry."""

    return f"{source} -> {target}"


def _cycle_token(cycle: tuple[str, ...]) -> str:
    return " -> ".join(list(cycle) + [cycle[0]])


# Rules that this checker produces. Used to filter suppressions so
# that ``suppression-unused`` findings only fire on T006-relevant
# entries; the T005 citation entries (rule ``module-path`` and
# friends) are silently skipped because they belong to a different
# checker that reads the same file.
T006_RULES: frozenset[str] = frozenset(
    {
        "layer-direction",
        "import-cycle",
        "unseeded-clock",
        "unclassified-module",
        "architecture-disagreement",
        "module-set",
        "module-parse",
    }
)


def run(
    repo_root: Path | str,
    *,
    suppressions_path: Path | str | None = None,
    config_path: Path | str | None = None,
    architecture_path: Path | str | None = None,
    modules: Iterable[Path | str] | None = None,
) -> list[Finding]:
    """Run the full check.

    Returns the list of *unmatched* findings plus one synthetic
    finding per unused suppression entry. Empty means pass.
    """

    root = Path(repo_root).resolve()
    config = Path(config_path).resolve() if config_path else root / "todo" / "config.yaml"
    suppressions = (
        Path(suppressions_path).resolve()
        if suppressions_path
        else root / "docs" / "implement" / "ci" / "suppressions.toml"
    )
    arch = (
        Path(architecture_path).resolve()
        if architecture_path
        else root / "docs" / "spec" / "architecture" / "ARCHITECTURE.md"
    )
    src_root = (root / "src").resolve()

    findings: list[Finding] = []

    if not src_root.is_dir():
        return [
            Finding(
                rule="module-set",
                path=str(src_root.relative_to(root)),
                line=0,
                token="",
                message=f"src/ is missing at {src_root}",
            )
        ]
    if not (src_root / "robinhood_lp").is_dir():
        return [
            Finding(
                rule="module-set",
                path=str((src_root / "robinhood_lp").relative_to(root)),
                line=0,
                token="",
                message=f"src/robinhood_lp/ is missing at {src_root / 'robinhood_lp'}",
            )
        ]

    if modules is None:
        try:
            module_paths = module_set(root)
        except (OSError, ValueError) as exc:
            return [
                Finding(
                    rule="module-set",
                    path="",
                    line=0,
                    token="",
                    message=f"module set could not be enumerated: {exc}",
                )
            ]
    else:
        module_paths = [Path(p).resolve() for p in modules]

    if not module_paths:
        return [
            Finding(
                rule="module-set",
                path="src/robinhood_lp",
                line=0,
                token="",
                message=(
                    "module set is empty; the checker would have scanned no "
                    "robinhood_lp.* imports and therefore cannot enforce ADR-006"
                ),
            )
        ]

    parsed = graph_module.parse_modules(module_paths, src_root=src_root)

    # Surface parse errors as findings (fail closed). A module whose
    # source file fails to parse cannot have its edges validated, so
    # the only safe choice is to abort the check.
    for module in parsed:
        if module.parse_error is None:
            continue
        findings.append(
            Finding(
                rule="module-parse",
                path=_path_for_finding(module.file, root),
                line=0,
                token=module.path,
                message=f"could not parse source: {module.parse_error}",
            )
        )
    if any(f.rule == "module-parse" for f in findings):
        # A parse error short-circuits the rest of the check: the
        # edges it produces are incomplete, and reporting them as
        # violations would be misleading.
        return findings

    # Unclassified-module rule.
    for module in parsed:
        if classify(module.path) is None:
            findings.append(
                Finding(
                    rule="unclassified-module",
                    path=_path_for_finding(module.file, root),
                    line=0,
                    token=module.path,
                    message=(
                        f"module {module.path!r} is not classified by the layer "
                        "map; add a per-module override in tools/check_imports/layer_map.py"
                    ),
                )
            )

    # Unseeded-clock rule.
    for module in parsed:
        layer = classify(module.path)
        if layer is None or layer not in _CLOCK_FORBIDDEN_TIERS:
            continue
        for clock_module, line in module.clock_imports:
            findings.append(
                Finding(
                    rule="unseeded-clock",
                    path=_path_for_finding(module.file, root),
                    line=line,
                    token=clock_module,
                    message=(
                        f"{clock_module!r} is an unseeded clock; ADR-006 forbids it "
                        f"inside the {layer!r} tier"
                    ),
                )
            )

    # Lower-to-higher edges and edges that touch the sentinel
    # platform-reserved layer. ADR-006's diagram places higher tiers
    # above lower tiers; imports are allowed to flow downward, so the
    # forbidden direction is the reverse: a module whose tier sits
    # lower in the diagram reaching into one that sits higher.
    raw_edges: list[tuple[str, str, int]] = []
    seen_edges: set[tuple[str, str]] = set()
    edge_findings: dict[str, Finding] = {}
    for module in parsed:
        for edge in module.imports:
            raw_edges.append((edge.source, edge.target, edge.line))
            if (edge.source, edge.target) in seen_edges:
                continue
            seen_edges.add((edge.source, edge.target))
            source_layer = classify(edge.source)
            target_layer = classify(edge.target)
            if source_layer is None or target_layer is None:
                # Either side is unclassified; an unclassified-module
                # finding will already exist. Skipping avoids double
                # reporting the same defect.
                continue
            # Platform-reserved sentinel: every cross-package edge
            # whose target is the reserved layer (something imports
            # ``robinhood_lp.config`` from outside) is a violation.
            # Internal edges inside the reserved package, and edges
            # from the reserved package downward, are allowed — the
            # reserved layer sits "above" every §2.1 tier by intent.
            if target_layer == PLATFORM_RESERVED_LAYER and source_layer != PLATFORM_RESERVED_LAYER:
                token = _edge_token(edge.source, edge.target)
                edge_findings[token] = Finding(
                    rule="layer-direction",
                    path=_path_for_finding(module.file, root),
                    line=edge.line,
                    token=token,
                    message=(
                        f"edge {edge.source!r} ({source_layer}) -> "
                        f"{edge.target!r} ({target_layer}) crosses the "
                        "platform-reserved layer ADR-006 reserves for a future ADR"
                    ),
                )
                continue
            source_tier = tier_index(source_layer)
            target_tier = tier_index(target_layer)
            if source_tier == -1 or target_tier == -1:
                # Either side is in a layer the tier order does not
                # recognise. The sentinel case above already caught
                # the cross-package ``config`` edges; this branch
                # handles any other layer not in the ADR-006 ordering
                # (which would itself be an unclassified-module
                # finding).
                continue
            if source_tier < target_tier:
                token = _edge_token(edge.source, edge.target)
                edge_findings[token] = Finding(
                    rule="layer-direction",
                    path=_path_for_finding(module.file, root),
                    line=edge.line,
                    token=token,
                    message=(
                        f"edge {edge.source!r} ({source_layer}, tier {source_tier}) "
                        f"-> {edge.target!r} ({target_layer}, tier {target_tier}) "
                        "is a lower-to-higher import that contradicts ADR-006"
                    ),
                )
    findings.extend(edge_findings.values())

    # Import cycles.
    cycle = graph_module.find_cycle(parsed)
    if cycle is not None:
        findings.append(
            Finding(
                rule="import-cycle",
                path="src/robinhood_lp",
                line=0,
                token=_cycle_token(cycle),
                message=("import cycle between packages: " + " -> ".join(list(cycle) + [cycle[0]])),
            )
        )

    # §2.2 / map agreement check.
    if not arch.is_file():
        findings.append(
            Finding(
                rule="architecture-disagreement",
                path=str(arch.relative_to(root)),
                line=0,
                token="",
                message=f"ARCHITECTURE.md is missing at {arch}",
            )
        )
    else:
        arch_text = arch.read_text(encoding="utf-8")
        rows = parse_section22(arch_text)
        # An empty row list is not, by itself, a defect: it just
        # means §2.2 declares nothing for this repository. Findings
        # are produced only when a row is actually present and
        # disagrees with the map (or references an unknown task).
        resolved = resolve_section22(rows, config_path=config, path_for_finding=lambda p: str(p))
        for module_name, declared, _arch_path, line, reason in resolved:
            if not module_name:
                # Task validation failure — already a fatal finding.
                findings.append(
                    Finding(
                        rule="architecture-disagreement",
                        path=str(arch.relative_to(root)),
                        line=line,
                        token="",
                        message=reason,
                    )
                )
                continue
            canonical = canonical_layer(declared)
            map_layer = classify(module_name)
            if map_layer is None:
                findings.append(
                    Finding(
                        rule="architecture-disagreement",
                        path=str(arch.relative_to(root)),
                        line=line,
                        token=module_name,
                        message=(
                            f"§2.2 row declares {module_name!r} as {declared!r}; the layer "
                            "map does not classify this module"
                        ),
                    )
                )
                continue
            if canonical != map_layer:
                findings.append(
                    Finding(
                        rule="architecture-disagreement",
                        path=str(arch.relative_to(root)),
                        line=line,
                        token=module_name,
                        message=(
                            f"§2.2 row declares {module_name!r} as {declared!r} "
                            f"(canonical {canonical!r}); the layer map classifies it "
                            f"as {map_layer!r}"
                        ),
                    )
                )

    # Suppression matching.
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

    # Filter to entries that this checker cares about. The
    # ``suppressions.toml`` file is shared between T005 and T006;
    # entries with rules this checker never produces belong to T005
    # and are silently ignored here. An unused-entry finding for a
    # T005 entry would otherwise leak across checkers and produce
    # false positives.
    relevant_entries = [entry for entry in entries if entry.rule in T006_RULES]

    unlisted: list[Finding] = []
    for finding in findings:
        if find_matching(
            relevant_entries, rule=finding.rule, path=finding.path, token=finding.token
        ):
            continue
        unlisted.append(finding)

    _, unused = partition_used_and_unused(
        relevant_entries,
        ((finding.rule, finding.path, finding.token) for finding in findings),
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
    """Format the findings for printing on CI logs.

    The output is human-readable; a separate, machine-readable dump
    is produced by the ``report`` subcommand of the CLI.
    """

    out = list(findings)
    if not out:
        return "import-graph check passed: no findings"
    lines = ["import-graph check failed:"]
    for finding in out:
        location = finding.path
        if finding.line:
            location = f"{location}:{finding.line}"
        if finding.token:
            location = f"{location} ({finding.token})"
        lines.append(f"  {finding.rule}: {location}: {finding.message}")
    return "\n".join(lines)


__all__ = ["CheckResult", "format_findings", "run"]
