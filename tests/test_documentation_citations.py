"""Regression tests for the T005 deterministic citation checker.

The contract (T005) requires that each of the five resolution kinds has a
seeded regression that *fails* the check when its kind is broken; a check
that never fires is indistinguishable from one that passes. The tests
below build a temporary repository under ``tmp_path`` so the regressions
never touch the real working tree, and they exercise every documented
failure mode (missing document set, empty document set, unreadable
exception entry, unparsable §2.2 row, empty scan).
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Iterable
from pathlib import Path

import pytest
from tools.check_citations import Finding, document_set, run
from tools.check_citations.checker import format_findings

REPO = Path(__file__).resolve().parents[1]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_repo(
    root: Path,
    *,
    module_tree: dict[str, str] | None = None,
    extra_files: Iterable[Path] | None = None,
) -> None:
    """Create a minimal repository layout that the checker can scan.

    ``module_tree`` maps dotted ``robinhood_lp.<...>`` paths to file
    bodies. The leaf segment becomes a ``.py`` file; intermediate
    segments become ``__init__.py`` packages so the package layout
    matches the production layout under ``src/robinhood_lp/``.
    """

    src = root / "src" / "robinhood_lp"
    src.mkdir(parents=True, exist_ok=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    for dotted, body in (module_tree or {}).items():
        assert dotted.startswith("robinhood_lp.")
        parts = dotted.split(".")[1:]  # drop the leading "robinhood_lp"
        target_dir = src.joinpath(*parts[:-1])
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / f"{parts[-1]}.py").write_text(body, encoding="utf-8")
        # Ensure every intermediate segment is a package.
        for index in range(1, len(parts)):
            (src.joinpath(*parts[:index]) / "__init__.py").write_text("", encoding="utf-8")
    todo = root / "todo"
    todo.mkdir(parents=True, exist_ok=True)
    (todo / "README.md").write_text("# workflow readme\n", encoding="utf-8")
    (todo / "WORKFLOW.md").write_text("# workflow\n", encoding="utf-8")
    phases = todo / "phases"
    phases.mkdir(parents=True, exist_ok=True)
    (phases / "P00-baseline").mkdir(parents=True, exist_ok=True)
    (phases / "P00-baseline" / "README.md").write_text(
        textwrap.dedent(
            """\
            # P00

            ## Tasks

            - [T001 — placeholder](T001.md)
            """
        ),
        encoding="utf-8",
    )
    docs_impl = root / "docs" / "implement" / "ci"
    docs_impl.mkdir(parents=True, exist_ok=True)
    (docs_impl / "suppressions.toml").write_text("suppressions = []\n", encoding="utf-8")
    for extra in extra_files or []:
        _write(root / extra.name, extra.read_text(encoding="utf-8"))


def _config_yaml(
    tasks: Iterable[tuple[str, str, str] | tuple[str, str, str, str | None]],
) -> str:
    out: dict[str, dict[str, object]] = {}
    for entry in tasks:
        if len(entry) == 3:
            task_id, phase, status = entry
            superseded_by: str | None = None
        else:
            task_id, phase, status, superseded_by = entry
        out[task_id] = {
            "phase": phase,
            "status": status,
            "superseded_by": superseded_by,
        }
    return json.dumps({"schema_version": 1, "tasks": out}) + "\n"


def test_check_passes_on_clean_repository() -> None:
    """A repo with a valid citation has no findings."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(
            root,
            module_tree={
                "robinhood_lp.protocol.ids": "class ChainId: pass\n",
            },
        )
        readme = root / "README.md"
        readme.write_text(
            "Cites `robinhood_lp.protocol.ids` and `T001` and `G-SIGNAL-01`.\n",
            encoding="utf-8",
        )
        intent = root / "docs" / "intent" / "PROJECT_GOALS.md"
        intent.parent.mkdir(parents=True, exist_ok=True)
        intent.write_text(
            textwrap.dedent(
                """\
                # Goals

                - `G-SIGNAL-01`: signal.
                """
            ),
            encoding="utf-8",
        )
        (root / "todo" / "config.yaml").write_text(
            _config_yaml([("T001", "P00", "APPROVED")]),
            encoding="utf-8",
        )
        findings = run(root)
        assert findings == [], format_findings(findings, root)


def test_module_path_seeded_regression() -> None:
    """A stale ``robinhood_lp.<missing>`` citation must be flagged."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(root)
        readme = root / "README.md"
        readme.write_text(
            "Cites a nonexistent `robinhood_lp.does.not.exist`.\n",
            encoding="utf-8",
        )
        (root / "todo" / "config.yaml").write_text(
            _config_yaml([]),
            encoding="utf-8",
        )
        findings = run(root)
        rules = {finding.rule for finding in findings}
        tokens = {finding.token for finding in findings}
        assert "module-path" in rules, format_findings(findings, root)
        assert "robinhood_lp.does.not.exist" in tokens


def test_member_access_seeded_regression() -> None:
    """A ``Class.member`` access where the member does not exist must fail."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(
            root,
            module_tree={
                "robinhood_lp.protocol.ids": "class ChainId:\n    def value(self) -> int:\n        return 1\n",
            },
        )
        readme = root / "README.md"
        readme.write_text(
            "Cites `ChainId.missing_member` which the class does not define.\n",
            encoding="utf-8",
        )
        (root / "todo" / "config.yaml").write_text(
            _config_yaml([("T001", "P00", "APPROVED")]),
            encoding="utf-8",
        )
        findings = run(root)
        rules = {finding.rule for finding in findings}
        tokens = {finding.token for finding in findings}
        assert "member-access" in rules, format_findings(findings, root)
        assert "ChainId.missing_member" in tokens


def test_task_id_seeded_regression() -> None:
    """A ``Txxx`` token absent from ``todo/config.yaml`` must fail."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(root)
        readme = root / "README.md"
        readme.write_text("Cites `T999` which is not in the config.\n", encoding="utf-8")
        (root / "todo" / "config.yaml").write_text(
            _config_yaml([("T001", "P00", "APPROVED")]),
            encoding="utf-8",
        )
        findings = run(root)
        rules = {finding.rule for finding in findings}
        tokens = {finding.token for finding in findings}
        assert "task-id" in rules, format_findings(findings, root)
        assert "T999" in tokens


def test_requirement_id_seeded_regression() -> None:
    """An undefined requirement identifier must fail."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(root)
        readme = root / "README.md"
        readme.write_text(
            "Cites `G-DOES-NOT-EXIST` which the intent does not define.\n",
            encoding="utf-8",
        )
        intent = root / "docs" / "intent" / "PROJECT_GOALS.md"
        intent.parent.mkdir(parents=True, exist_ok=True)
        intent.write_text(
            textwrap.dedent(
                """\
                # Goals

                - `G-SIGNAL-01`: signal.
                """
            ),
            encoding="utf-8",
        )
        (root / "todo" / "config.yaml").write_text(
            _config_yaml([("T001", "P00", "APPROVED")]),
            encoding="utf-8",
        )
        findings = run(root)
        rules = {finding.rule for finding in findings}
        tokens = {finding.token for finding in findings}
        assert "requirement-id" in rules, format_findings(findings, root)
        assert "G-DOES-NOT-EXIST" in tokens


def test_phase_tasks_seeded_regression() -> None:
    """A phase README whose Tasks list is missing a task must fail."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(root)
        # Override the P00 README to omit T002.
        (root / "todo" / "phases" / "P00-baseline" / "README.md").write_text(
            textwrap.dedent(
                """\
                # P00

                ## Tasks

                - [T001 — placeholder](T001.md)
                """
            ),
            encoding="utf-8",
        )
        (root / "todo" / "config.yaml").write_text(
            _config_yaml([("T001", "P00", "APPROVED"), ("T002", "P00", "APPROVED")]),
            encoding="utf-8",
        )
        findings = run(root)
        rules = {finding.rule for finding in findings}
        tokens = {finding.token for finding in findings}
        assert "phase-tasks" in rules, format_findings(findings, root)
        assert "T002" in tokens


def test_missing_document_set_fails_closed() -> None:
    """An empty document set is a fatal input; the checker must not pass."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(root)
        (root / "todo" / "config.yaml").write_text(
            _config_yaml([]),
            encoding="utf-8",
        )
        findings = run(root, documents=[])
        assert any(f.rule == "document-set" for f in findings), format_findings(findings, root)


def test_unreadable_suppression_entry_fails_closed() -> None:
    """A malformed ``suppressions.toml`` is itself a fatal failure."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(root)
        (root / "todo" / "config.yaml").write_text(
            _config_yaml([]),
            encoding="utf-8",
        )
        (root / "docs" / "implement" / "ci" / "suppressions.toml").write_text(
            "this is not valid TOML !!!\n", encoding="utf-8"
        )
        findings = run(root)
        assert any(f.rule == "suppressions" for f in findings), format_findings(findings, root)


def test_unparsable_architecture_section22_fails_closed() -> None:
    """A §2.2 row that names a task not in ``todo/config.yaml`` must fail."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(root)
        arch = root / "docs" / "spec" / "architecture" / "ARCHITECTURE.md"
        arch.parent.mkdir(parents=True, exist_ok=True)
        arch.write_text(
            textwrap.dedent(
                """\
                # Architecture

                ### 2.2

                | Phase | Task | Module | Layer |
                | --- | --- | --- | --- |
                | 0 | T999 | `robinhood_lp.x.y` | n/a |
                """
            ),
            encoding="utf-8",
        )
        (root / "todo" / "config.yaml").write_text(
            _config_yaml([]),
            encoding="utf-8",
        )
        findings = run(root)
        rules = {finding.rule for finding in findings}
        assert "architecture-section22" in rules, format_findings(findings, root)


def test_empty_scan_fails_closed() -> None:
    """No documents at all is fatal; the scanner must not silently pass."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        # Provide the minimum the checker needs to reach the document-set
        # step, but ensure the discovered document set is empty.
        (root / "src" / "robinhood_lp").mkdir(parents=True)
        (root / "src" / "robinhood_lp" / "__init__.py").write_text("")
        (root / "todo").mkdir(parents=True)
        (root / "todo" / "config.yaml").write_text(_config_yaml([]), encoding="utf-8")
        (root / "docs" / "implement" / "ci").mkdir(parents=True)
        (root / "docs" / "implement" / "ci" / "suppressions.toml").write_text("suppressions = []\n")
        findings = run(root)
        assert findings, format_findings(findings, root)
        assert any(f.rule == "document-set" for f in findings)


def test_unused_suppression_is_fatal() -> None:
    """An entry that no longer matches a finding fails the check."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(root)
        (root / "todo" / "config.yaml").write_text(
            _config_yaml([("T001", "P00", "APPROVED")]),
            encoding="utf-8",
        )
        (root / "docs" / "implement" / "ci" / "suppressions.toml").write_text(
            textwrap.dedent(
                """\
                [[suppression]]
                rule = "module-path"
                reason = "stale entry that no longer matches anything"
                owner = "@dev"
                expiry = "2026-12-31"
                token = "robinhood_lp.no.such.module"
                """
            ),
            encoding="utf-8",
        )
        findings = run(root)
        assert any(
            f.rule == "suppression-unused" and "robinhood_lp.no.such.module" in f.token
            for f in findings
        ), format_findings(findings, root)


def test_document_set_covers_required_roots() -> None:
    """The declared document set covers README, CLAUDE, AGENTS, docs, todo."""

    documents = document_set(REPO)
    rel = {path.resolve().relative_to(REPO.resolve()).as_posix() for path in documents}
    assert "README.md" in rel
    assert "CLAUDE.md" in rel
    assert "AGENTS.md" in rel
    assert "todo/README.md" in rel
    assert "todo/WORKFLOW.md" in rel
    assert any(path.startswith("docs/") for path in rel)
    assert any(path.startswith("todo/phases/") for path in rel)


def test_check_passes_on_real_repository() -> None:
    """The committed repository passes the citation check.

    Pre-existing findings against binding documents are recorded in
    ``docs/implement/ci/suppressions.toml``; the check must pass without
    any new findings or unused entries.
    """

    findings = run(REPO)
    assert findings == [], format_findings(findings, REPO)


@pytest.mark.parametrize(
    "module_path",
    [
        "robinhood_lp.protocol.ids",
        "robinhood_lp.discovery.asset_admission",
        "robinhood_lp.storage.schema",
    ],
)
def test_real_modules_resolve(module_path: str) -> None:
    """Spot-check that real modules pass the resolver outside any §2.2 row."""

    from tools.check_citations.resolvers import _resolve_module_path

    src_root = (REPO / "src").resolve()
    resolved = _resolve_module_path(module_path, src_root)
    assert resolved is not None, module_path


def test_finding_dataclass_supports_set_membership() -> None:
    """The ``Finding`` dataclass is hashable so duplicate findings collapse."""

    finding = Finding(
        rule="module-path",
        path="docs/spec/architecture/ARCHITECTURE.md",
        line=10,
        token="robinhood_lp.x.y",
        message="missing",
    )
    bucket = {finding, finding}
    assert len(bucket) == 1


def test_format_findings_handles_empty() -> None:
    """The formatter announces an empty result explicitly."""

    text = format_findings([], REPO)
    assert "passed" in text
