"""Resolve each of the five citation kinds against the repository.

Each resolver returns the list of findings its kind produced for one
document. The top-level checker composes them so the failure messages stay
localised to the rule that produced them.

The resolvers are intentionally read-only: they inspect ``src/robinhood_lp/``
and ``todo/config.yaml`` but never import the modules at test time. A
``robinhood_lp.<module>`` citation resolves to a directory or ``.py`` file
under ``src/robinhood_lp/``; longer names resolve to attributes defined in
the deepest matching module, discovered by static ``ast`` parsing so the
checker stays independent of import-time side effects.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from . import Finding
from .tokens import Token

PathForFinding = Callable[[Path], str]

# Source-file extensions whose presence in the second component of a
# two-part token disqualifies it from the class-member rule. Anything that
# looks like a file path (``.py``, ``.md``, ``.sol``, ...) is treated as a
# plain file reference, not a Python attribute access.
_FILE_EXTENSION = frozenset(
    {
        "py",
        "md",
        "rst",
        "txt",
        "toml",
        "yaml",
        "yml",
        "json",
        "csv",
        "tsv",
        "html",
        "xml",
        "sh",
        "bash",
        "lock",
        "so",
        "sol",
        "png",
        "svg",
        "jpg",
        "jpeg",
    }
)


@dataclass(frozen=True)
class ResolvedPath:
    """A module path resolved to a directory or Python source file."""

    parts: tuple[str, ...]

    @property
    def dotted(self) -> str:
        return ".".join(self.parts)

    def exists(self, src_root: Path) -> bool:
        base = src_root.joinpath(*self.parts)
        if base.is_dir():
            return True
        if base.with_suffix(".py").is_file():
            return True
        return any((base / candidate).is_file() for candidate in ("__init__.py",))


def _module_members(module_path: Path) -> set[str]:
    """Return the set of names defined at module scope of ``module_path``.

    The check parses the file with ``ast`` and walks top-level statements,
    so import-time side effects (network, random, time) are avoided.
    """

    if not module_path.is_file():
        return set()
    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])
    return names


def _resolve_module_path(token: str, src_root: Path) -> ResolvedPath | None:
    """Try to resolve ``token`` as a ``robinhood_lp.<...>`` path.

    The longest prefix that names an actual module under ``src/`` wins. If
    no prefix matches, the resolution fails and the caller records a
    finding.
    """

    if not token.startswith("robinhood_lp"):
        return None
    parts = token.split(".")
    if parts[0] != "robinhood_lp":
        return None
    # The bare ``robinhood_lp`` package name resolves to the package itself.
    if len(parts) == 1:
        if (src_root / "robinhood_lp").is_dir():
            return ResolvedPath(parts=("robinhood_lp",))
        return None
    resolved: ResolvedPath | None = None
    for length in range(1, len(parts) + 1):
        candidate = ResolvedPath(parts=(*parts[:length],))
        if candidate.exists(src_root):
            resolved = candidate
    if resolved is None:
        return None
    if resolved.dotted == token:
        return resolved
    if len(resolved.parts) >= len(parts):
        return ResolvedPath(parts=tuple(parts))
    tail = parts[len(resolved.parts) :]
    tail_module_root = src_root.joinpath(*resolved.parts)
    if tail_module_root.is_dir():
        leaf = tail_module_root / "__init__.py"
    else:
        leaf = tail_module_root.with_suffix(".py")
    members = _module_members(leaf)
    if tail[0] in members:
        return ResolvedPath(parts=tuple(parts))
    return None


def _resolve_member_access(token: str, src_root: Path) -> tuple[bool, str]:
    """Check ``token`` of the form ``Class.member``.

    Returns ``(ok, reason)``. ``ok`` is True when the token resolves to a
    known member of a class, enum or module that exists under ``src/``.
    Tokens whose first component is not a class, enum or module under
    ``src/robinhood_lp/`` are *skipped* (not failed) because the rule
    simply does not apply to them; a token like ``os.environ`` cites the
    Python standard library and is outside the resolver's scope.

    The implementation looks for ``head`` in three places, in order:

    1. A module whose dotted path is exactly ``head`` (i.e. a top-level
       ``src/robinhood_lp/<head>.py`` or ``<head>/__init__.py``).
    2. A class, enum or function defined at module scope of *any* module
       under ``src/robinhood_lp/`` that re-exports ``head`` by name.
    3. (Skipped) ``head`` is not recognised; the rule does not apply.
    """

    parts = token.split(".")
    if len(parts) != 2:
        return True, "skipped: not a two-part token"
    head, tail = parts
    if not head.isidentifier() or not tail.isidentifier():
        return True, "skipped: components are not Python identifiers"
    if tail in _FILE_EXTENSION:
        return True, "skipped: second component is a file extension"
    module_path = src_root.joinpath("robinhood_lp", f"{head}.py")
    package_init = src_root.joinpath("robinhood_lp", head, "__init__.py")
    target_module = module_path if module_path.is_file() else package_init
    if target_module.is_file():
        return _check_module_members(target_module, head, tail)
    owner = _find_class_owner(src_root, head)
    if owner is None:
        return True, (
            f"skipped: first component {head!r} is not a class, enum or "
            "module under src/robinhood_lp/ and the rule does not apply"
        )
    owner_module, owner_node = owner
    if owner_node is None:
        return _check_module_members(owner_module, head, tail)
    if _class_contains(owner_node, tail):
        return True, "class member"
    return False, f"class {head!r} does not define {tail!r}"


def _check_module_members(target_module: Path, head: str, tail: str) -> tuple[bool, str]:
    try:
        ast.parse(target_module.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return False, f"could not parse {target_module}: {exc}"
    module_members = _module_members(target_module)
    if head in module_members and tail in module_members:
        return True, "module-level name and member"
    if tail in module_members:
        return True, "module-level attribute"
    return False, f"module {head!r} does not define {tail!r}"


def _find_class_owner(src_root: Path, class_name: str) -> tuple[Path, ast.ClassDef | None] | None:
    """Return the (module_path, ClassDef) where ``class_name`` is defined."""

    package_root = src_root / "robinhood_lp"
    if not package_root.is_dir():
        return None
    for module_path in sorted(package_root.rglob("*.py")):
        try:
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return module_path, node
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == class_name
            ):
                return module_path, None
    return None


def _find_class_in_tree(tree: ast.Module, class_name: str) -> ast.ClassDef | None:
    """Return the ``ClassDef`` node for ``class_name`` inside ``tree``."""

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _class_contains(class_node: ast.ClassDef, member_name: str) -> bool:
    for item in class_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name == member_name:
                return True
        elif isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name) and target.id == member_name:
                    return True
        elif (
            isinstance(item, ast.AnnAssign)
            and isinstance(item.target, ast.Name)
            and item.target.id == member_name
        ):
            return True
    return False


def _defined_requirement_ids(doc_path: Path) -> set[str]:
    """Return the set of requirement identifiers defined inside ``doc_path``.

    Each definition is detected as a backticked identifier inside a heading
    or list item of the same family prefix as the document's owning family.
    The implementation intentionally does not interpret the heading level:
    the same family prefix is shared by every family the citation rules
    recognise, and the only family owned by a particular document is the one
    the contract binds to it. A missing file returns an empty set so the
    caller's "family requires at least one defined identifier" check
    produces a synthetic finding instead of crashing.
    """

    if not doc_path.is_file():
        return set()
    text = doc_path.read_text(encoding="utf-8")
    return set(re.findall(r"`([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)`", text))


def _family_for_token(token: str) -> tuple[str, Path | None]:
    """Map a requirement token to ``(family, owning_doc)``.

    ``family`` is the document-specific prefix (for example ``G-`` or
    ``ADM-``). ``owning_doc`` is the document that is contracted to define
    identifiers of that family; ``None`` when the family is unknown. A token
    that does not match any known family yields ``("", None)``.
    """

    families: dict[str, Path] = {
        "G-": Path("docs/intent/PROJECT_GOALS.md"),
        "ADM-": Path("docs/spec/product/ASSET_ADMISSION.md"),
        "WEB-": Path("docs/spec/product/WEB_CONSOLE.md"),
        "ECO-": Path("docs/spec/strategy/STRATEGY_ECONOMICS.md"),
        "CTRL-": Path("docs/spec/operations/OPERATOR_CONTROL.md"),
        "M-": Path("docs/spec/strategy/LP_METRICS.md"),
        "DS-": Path("docs/spec/research/DATASET_AND_EVALUATION.md"),
    }
    if re.fullmatch(r"R[0-9]+", token):
        return "R", Path("todo/README.md")
    for prefix, owner in families.items():
        if token.startswith(prefix):
            return prefix, owner
    return "", None


def resolve_module_paths(
    tokens: Iterable[Token],
    *,
    src_root: Path,
    path_for_finding: PathForFinding | None = None,
) -> list[Finding]:
    """Resolve every ``robinhood_lp.<...>`` token."""

    path_for_finding = path_for_finding or (lambda p: p.name)
    findings: list[Finding] = []
    for tok in tokens:
        if not tok.text.startswith("robinhood_lp"):
            continue
        if _resolve_module_path(tok.text, src_root) is not None:
            continue
        findings.append(
            Finding(
                rule="module-path",
                path=path_for_finding(tok.path),
                line=tok.line,
                token=tok.text,
                message=f"module path {tok.text!r} does not resolve under src/robinhood_lp/",
            )
        )
    return findings


def resolve_member_accesses(
    tokens: Iterable[Token],
    *,
    src_root: Path,
    path_for_finding: PathForFinding | None = None,
) -> list[Finding]:
    """Resolve every two-part ``Class.member`` token."""

    path_for_finding = path_for_finding or (lambda p: p.name)
    findings: list[Finding] = []
    for tok in tokens:
        if tok.text.startswith("robinhood_lp"):
            continue
        parts = tok.text.split(".")
        if len(parts) != 2:
            continue
        ok, reason = _resolve_member_access(tok.text, src_root)
        if ok:
            continue
        findings.append(
            Finding(
                rule="member-access",
                path=path_for_finding(tok.path),
                line=tok.line,
                token=tok.text,
                message=reason,
            )
        )
    return findings


def _config_tasks(config_path: Path) -> dict[str, dict[str, object]]:
    """Parse ``todo/config.yaml`` as a minimal JSON document.

    The config uses JSON syntax inside a YAML 1.2 file, so ``json.loads``
    is sufficient and avoids pulling in PyYAML. The function returns the
    raw ``tasks`` mapping; callers extract what they need.
    """

    data: dict[str, object] = json.loads(config_path.read_text(encoding="utf-8"))
    tasks = data["tasks"]
    return cast("dict[str, dict[str, object]]", tasks)


def resolve_task_ids(
    tokens: Iterable[Token],
    *,
    config_path: Path,
    path_for_finding: PathForFinding | None = None,
) -> list[Finding]:
    """Resolve every ``Txxx`` token against ``todo/config.yaml``."""

    path_for_finding = path_for_finding or (lambda p: p.name)
    tasks = _config_tasks(config_path)
    findings: list[Finding] = []
    for tok in tokens:
        if not re.fullmatch(r"T[0-9]{3}", tok.text):
            continue
        if tok.text in tasks:
            continue
        findings.append(
            Finding(
                rule="task-id",
                path=path_for_finding(tok.path),
                line=tok.line,
                token=tok.text,
                message=f"task identifier {tok.text!r} is not declared in todo/config.yaml",
            )
        )
    return findings


def resolve_architecture_section22(
    architecture_path: Path,
    *,
    config_path: Path,
    src_root: Path,
    repo_root: Path,
    path_for_finding: PathForFinding | None = None,
) -> list[Finding]:
    """Resolve the §2.2 mapping of ARCHITECTURE.md.

    Each row of the table binds a task to one or more module artefacts. The
    task must exist in ``config.yaml``. When the task is APPROVED, every
    module token in the row must resolve. When the task is replaced -- either
    because its retirement is recorded via ``superseded_by`` or because a task
    declaring ``replaces`` for it already exists -- the row must name that
    successor; the module tokens must then resolve against the *successor's*
    phase. PLANNED rows are recorded as planned and the module may be absent.

    The requirement attaches to the successor's *creation*, not only to the
    retirement, because the SUPERSEDE layer may change nothing but
    ``superseded_by``. A table that could only learn the successor's name from
    the retirement would force that layer to edit a document it may not write.
    """

    path_for_finding = path_for_finding or (lambda p: p.name)
    rel = path_for_finding(architecture_path)
    if not architecture_path.is_file():
        return [
            Finding(
                rule="architecture-section22",
                path=rel,
                line=0,
                token="",
                message="ARCHITECTURE.md §2.2 mapping is missing",
            )
        ]

    text = architecture_path.read_text(encoding="utf-8")
    tasks = _config_tasks(config_path)
    #: predecessor -> the successor task that declares ``replaces`` for it.
    replaced_by: dict[str, str] = {}
    for successor_id, successor_record in tasks.items():
        for predecessor in cast("list[str]", successor_record.get("replaces") or []):
            replaced_by.setdefault(predecessor, successor_id)
    findings: list[Finding] = []
    in_section = False
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if raw_line.startswith("### 2.2"):
            in_section = True
            continue
        if in_section and raw_line.startswith("### "):
            break
        if not in_section or not raw_line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in raw_line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] == "Phase" or cells[0].startswith("---"):
            continue
        task_cell = cells[1]
        module_cell = cells[2]
        task_ids = _expand_task_range(task_cell)
        for task_id in task_ids:
            record = tasks.get(task_id)
            if record is None:
                findings.append(
                    Finding(
                        rule="architecture-section22",
                        path=rel,
                        line=line_number,
                        token=task_id,
                        message=(
                            f"§2.2 row references task {task_id!r} which is not "
                            "declared in todo/config.yaml"
                        ),
                    )
                )
                continue
            status = record.get("status")
            superseded_by = record.get("superseded_by")
            successor = superseded_by or replaced_by.get(task_id)
            successor_named = bool(successor) and f"superseded by {successor}" in task_cell
            if successor and not successor_named:
                described = (
                    f"superseded task {task_id!r}"
                    if superseded_by
                    else f"task {task_id!r}, which a successor task replaces,"
                )
                findings.append(
                    Finding(
                        rule="architecture-section22",
                        path=rel,
                        line=line_number,
                        token=task_id,
                        message=(
                            f"§2.2 row references {described} "
                            f"without naming its successor {successor!r}"
                        ),
                    )
                )
                continue
            check_modules = status == "APPROVED" or successor_named
            if not check_modules:
                continue
            module_tokens = _module_tokens_in_cell(module_cell)
            if not module_tokens:
                continue
            findings.extend(
                _check_module_tokens_for_row(
                    module_tokens,
                    architecture_path,
                    line_number,
                    task_id,
                    src_root,
                    repo_root,
                    rel,
                )
            )
    return findings


#: A parenthetical that states how a row's task relates to another task. It is an
#: annotation, not a reference: the row is about the task it leads with, so a token
#: inside is not expanded as one this row also covers. Without this, the successor
#: row ``T105 (successor to T063)`` would be read as also referencing T063, and
#: would be asked to carry T063's successor phrase on T105's own row.
_RELATIONSHIP_PARENTHETICAL = re.compile(
    r"\((?:successor to|supersedes|superseded by|replaces|replacing)[^)]*\)",
    re.IGNORECASE,
)


def _expand_task_range(cell: str) -> list[str]:
    """Expand ``T084-T086`` or ``T093-T094`` style ranges into single IDs.

    Relationship parentheticals are masked first, so they neither contribute a
    token nor turn a bare id into a range.
    """

    masked = _RELATIONSHIP_PARENTHETICAL.sub(" ", cell)
    match = re.search(r"(T[0-9]{3})\s*[–-]\s*(T[0-9]{3})", masked)
    if not match:
        return [token for token in re.findall(r"T[0-9]{3}", masked)]
    start = int(match.group(1)[1:])
    end = int(match.group(2)[1:])
    return [f"T{index:03d}" for index in range(start, end + 1)]


def _module_tokens_in_cell(cell: str) -> list[str]:
    """Extract the backticked module tokens from one §2.2 row cell."""

    return [match for match in re.findall(r"`([^`\n]+)`", cell)]


def _check_module_tokens_for_row(
    tokens: Iterable[str],
    architecture_path: Path,
    line: int,
    task_id: str,
    src_root: Path,
    repo_root: Path,
    rel: str,
) -> list[Finding]:
    """Validate every module token in a §2.2 row against the repository.

    The first token is treated as the *primary* module reference; any
    remaining tokens are tested against the repository as relative paths
    (typical for ``tests/`` and ``tools/`` artefacts), as sibling modules
    of the primary module's package, or as attributes of the primary
    module. The primary token may also be a relative path or a test/tool
    artefact; rows that name test harnesses or Solidty files therefore still
    resolve.
    """

    tokens_list = list(tokens)
    if not tokens_list:
        return []
    primary = tokens_list[0]
    primary_resolved = (
        _resolve_module_path(primary, src_root) if primary.startswith("robinhood_lp") else None
    )
    findings: list[Finding] = []
    for index, raw in enumerate(tokens_list):
        for token in _expand_braces_in_cell(raw):
            if token.startswith("robinhood_lp"):
                if _resolve_module_path(token, src_root) is None:
                    findings.append(
                        Finding(
                            rule="architecture-section22",
                            path=rel,
                            line=line,
                            token=token,
                            message=(
                                f"§2.2 row for {task_id!r} references module "
                                f"{token!r} which does not resolve under "
                                "src/robinhood_lp/"
                            ),
                        )
                    )
                continue
            if index == 0:
                # Primary token may be a tests/ tools/ docs/ path or a
                # Solidity file. Try resolving as a path; failure means
                # the row has no anchor.
                if (repo_root / token.split("(", 1)[0].strip().rstrip("/")).exists():
                    continue
                findings.append(
                    Finding(
                        rule="architecture-section22",
                        path=rel,
                        line=line,
                        token=token,
                        message=(
                            f"§2.2 row for {task_id!r} primary module token "
                            f"{token!r} does not resolve to any repository path"
                        ),
                    )
                )
                continue
            if _resolve_subtoken(token, primary, primary_resolved, src_root, repo_root):
                continue
            findings.append(
                Finding(
                    rule="architecture-section22",
                    path=rel,
                    line=line,
                    token=token,
                    message=(
                        f"§2.2 row for {task_id!r} references path or "
                        f"sub-module {token!r} which does not exist in the "
                        "repository"
                    ),
                )
            )
    return findings


def _resolve_subtoken(
    token: str,
    primary: str,
    primary_resolved: ResolvedPath | None,
    src_root: Path,
    repo_root: Path,
) -> bool:
    """Return True when ``token`` resolves as a sibling, attribute, or path."""

    cleaned = token.split("(", 1)[0].strip().rstrip("/")
    candidate = repo_root / cleaned
    if candidate.exists():
        return True
    if primary.startswith("robinhood_lp"):
        candidate = repo_root / "src" / "robinhood_lp" / cleaned
        if candidate.exists():
            return True
    if primary_resolved is not None and len(primary_resolved.parts) >= 3:
        # Sibling of the primary module inside the parent package
        parent_parts = primary_resolved.parts[:-1]
        sibling = src_root.joinpath(*parent_parts, f"{cleaned}.py")
        if sibling.is_file():
            return True
        sibling_pkg = src_root.joinpath(*parent_parts, cleaned, "__init__.py")
        if sibling_pkg.is_file():
            return True
    if primary_resolved is not None and "." in primary:
        # Attribute of the primary module
        module_file = (
            src_root.joinpath(*primary_resolved.parts[:-1]) / f"{primary_resolved.parts[-1]}.py"
            if not (src_root.joinpath(*primary_resolved.parts)).is_dir()
            else src_root.joinpath(*primary_resolved.parts) / "__init__.py"
        )
        members = _module_members(module_file)
        if cleaned in members:
            return True
    return False


def _expand_braces_in_cell(token: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", token)
    if not match or "," not in match.group(1):
        return [token]
    head = token[: match.start()]
    tail = token[match.end() :]
    alternatives = [piece.strip() for piece in match.group(1).split(",") if piece.strip()]
    return [f"{head}{piece}{tail}" for piece in alternatives]


def _check_path_token(
    token: str,
    architecture_path: Path,
    line: int,
    task_id: str,
    findings: list[Finding],
) -> None:
    """Check a non-``robinhood_lp`` module token that lives outside ``src/``."""

    relative = token.split("(", 1)[0].strip().rstrip("/")
    candidate = (architecture_path.parent.parent / relative).resolve()
    if not candidate.exists():
        findings.append(
            Finding(
                rule="architecture-section22",
                path=str(architecture_path.name),
                line=line,
                token=token,
                message=(
                    f"§2.2 row for {task_id!r} references path "
                    f"{token!r} which does not exist in the repository"
                ),
            )
        )


def resolve_requirement_ids(
    tokens: Iterable[Token],
    *,
    repo_root: Path,
    path_for_finding: PathForFinding | None = None,
) -> list[Finding]:
    """Resolve every requirement identifier token against its owning doc."""

    path_for_finding = path_for_finding or (lambda p: p.name)
    findings: list[Finding] = []
    by_family: dict[str, set[str]] = {}
    for tok in tokens:
        family, owner = _family_for_token(tok.text)
        if not family or owner is None:
            continue
        if tok.text.endswith("-*"):
            continue
        owner_path = repo_root / owner
        defined = by_family.setdefault(family, _defined_requirement_ids(owner_path))
        if tok.text in defined:
            continue
        findings.append(
            Finding(
                rule="requirement-id",
                path=path_for_finding(tok.path),
                line=tok.line,
                token=tok.text,
                message=(
                    f"requirement identifier {tok.text!r} is not defined in {owner.as_posix()}"
                ),
            )
        )
    for family, owner in (
        ("G-", Path("docs/intent/PROJECT_GOALS.md")),
        ("ADM-", Path("docs/spec/product/ASSET_ADMISSION.md")),
        ("WEB-", Path("docs/spec/product/WEB_CONSOLE.md")),
        ("ECO-", Path("docs/spec/strategy/STRATEGY_ECONOMICS.md")),
        ("CTRL-", Path("docs/spec/operations/OPERATOR_CONTROL.md")),
        ("M-", Path("docs/spec/strategy/LP_METRICS.md")),
        ("DS-", Path("docs/spec/research/DATASET_AND_EVALUATION.md")),
    ):
        owner_path = repo_root / owner
        if not owner_path.is_file():
            continue
        defined = _defined_requirement_ids(owner_path)
        if not defined:
            findings.append(
                Finding(
                    rule="requirement-id",
                    path=owner.as_posix(),
                    line=0,
                    token=family + "*",
                    message=(
                        f"family pattern {family}* requires at least one "
                        f"defined identifier in {owner.as_posix()}, but none "
                        "are present"
                    ),
                )
            )
    return findings


def resolve_phase_tasks(repo_root: Path, *, config_path: Path) -> list[Finding]:
    """Resolve the Tasks list of every phase README.

    The Tasks list is the bullet section under the ``## Tasks`` heading; the
    rest of the document is ignored, so cross-references in Entry/Exit
    prose do not influence membership. The phase identifier is the
    ``P<NN>`` prefix of the README directory (``P00-engineering-baseline``
    maps to ``P00``) so it matches the ``phase`` field in
    ``todo/config.yaml``.
    """

    phases_dir = repo_root / "todo" / "phases"
    if not phases_dir.is_dir():
        return [
            Finding(
                rule="phase-tasks",
                path="todo/phases/",
                line=0,
                token="",
                message="todo/phases/ directory is missing",
            )
        ]
    tasks = _config_tasks(config_path)
    expected_by_phase: dict[str, set[str]] = {}
    for task_id, record in tasks.items():
        phase_value = record.get("phase", "")
        phase_key = phase_value if isinstance(phase_value, str) else ""
        expected_by_phase.setdefault(phase_key, set()).add(task_id)
    findings: list[Finding] = []
    for readme in sorted(phases_dir.glob("*/README.md")):
        directory = readme.parent.name
        phase_id = directory.split("-", 1)[0]
        if not phase_id:
            findings.append(
                Finding(
                    rule="phase-tasks",
                    path=str(readme.relative_to(repo_root)),
                    line=0,
                    token="",
                    message=(
                        f"phase README directory {directory!r} does not start "
                        "with a P<NN> identifier"
                    ),
                )
            )
            continue
        text = readme.read_text(encoding="utf-8")
        listed = _extract_tasks_section(text)
        expected = expected_by_phase.get(phase_id, set())
        missing = expected - listed
        extra = listed - expected
        for task_id in sorted(missing):
            findings.append(
                Finding(
                    rule="phase-tasks",
                    path=str(readme.relative_to(repo_root)),
                    line=0,
                    token=task_id,
                    message=(f"phase {phase_id!r} is missing task {task_id!r} from its Tasks list"),
                )
            )
        for task_id in sorted(extra):
            findings.append(
                Finding(
                    rule="phase-tasks",
                    path=str(readme.relative_to(repo_root)),
                    line=0,
                    token=task_id,
                    message=(
                        f"phase {phase_id!r} lists task {task_id!r} whose phase "
                        "in todo/config.yaml is different"
                    ),
                )
            )
    return findings


def _extract_tasks_section(text: str) -> set[str]:
    """Return the task identifiers listed under the ``## Tasks`` section."""

    lines = text.splitlines()
    in_section = False
    listed: set[str] = set()
    for raw in lines:
        if raw.startswith("## "):
            if in_section:
                break
            if raw.strip() == "## Tasks":
                in_section = True
                continue
        if not in_section:
            continue
        listed.update(re.findall(r"\b(T[0-9]{3})\b", raw))
    return listed


__all__ = [
    "resolve_module_paths",
    "resolve_member_accesses",
    "resolve_task_ids",
    "resolve_architecture_section22",
    "resolve_requirement_ids",
    "resolve_phase_tasks",
]
