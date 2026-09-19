"""Regression tests for the T006 deterministic import-graph checker.

The contract (T006) requires that every rule in deliverables 2 and 3
has a seeded regression that *fails* the check when its rule is
broken; a check that never fires is indistinguishable from one that
passes. The tests below mirror the T005 pattern in
``test_documentation_citations.py``: each one builds a temporary
repository under ``tmp_path`` so the regressions never touch the
real working tree, then exercises the documented failure mode and
the fail-closed paths the contract enumerates.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from tools.check_imports import Finding, module_set, run
from tools.check_imports.check import format_findings

REPO = Path(__file__).resolve().parents[1]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_repo(
    root: Path,
    *,
    module_tree: dict[str, str] | None = None,
    extra_files: dict[str, str] | None = None,
    tasks: list[tuple[str, str, str]] | None = None,
    suppressions: str | None = None,
    architecture: str | None = None,
) -> None:
    """Create a minimal repository layout that the checker can scan.

    ``module_tree`` maps dotted ``robinhood_lp.<...>`` paths to file
    bodies. Intermediate segments become ``__init__.py`` packages so
    the layout matches the production tree under ``src/robinhood_lp/``.
    """

    src = root / "src" / "robinhood_lp"
    src.mkdir(parents=True, exist_ok=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    for dotted, body in (module_tree or {}).items():
        assert dotted.startswith("robinhood_lp.")
        parts = dotted.split(".")[1:]
        target_dir = src.joinpath(*parts[:-1])
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / f"{parts[-1]}.py").write_text(body, encoding="utf-8")
        for index in range(1, len(parts)):
            (src.joinpath(*parts[:index]) / "__init__.py").write_text("", encoding="utf-8")
    todo = root / "todo"
    todo.mkdir(parents=True, exist_ok=True)
    (todo / "README.md").write_text("# workflow readme\n", encoding="utf-8")
    (todo / "WORKFLOW.md").write_text("# workflow\n", encoding="utf-8")
    tasks = tasks or []
    task_payload = {task_id: {"phase": phase, "status": status} for task_id, phase, status in tasks}
    (todo / "config.yaml").write_text(
        json.dumps({"schema_version": 1, "tasks": task_payload}) + "\n",
        encoding="utf-8",
    )
    docs_impl = root / "docs" / "implement" / "ci"
    docs_impl.mkdir(parents=True, exist_ok=True)
    (docs_impl / "suppressions.toml").write_text(
        suppressions if suppressions is not None else "suppressions = []\n",
        encoding="utf-8",
    )
    arch = root / "docs" / "spec" / "architecture" / "ARCHITECTURE.md"
    arch.parent.mkdir(parents=True, exist_ok=True)
    if architecture is not None:
        arch.write_text(architecture, encoding="utf-8")
    else:
        arch.write_text(
            textwrap.dedent(
                """\
                # Architecture

                ### 2.2

                | Phase | Task | Module | Layer |
                | --- | --- | --- | --- |
                """
            ),
            encoding="utf-8",
        )
    for rel, content in (extra_files or {}).items():
        _write(root / rel, content)


# ---------------------------------------------------------------------------
# Positive / negative paths
# ---------------------------------------------------------------------------


def test_clean_repository_passes() -> None:
    """A repo whose edges respect ADR-006 and whose map matches §2.2 has no findings."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(
            root,
            module_tree={
                "robinhood_lp.protocol.ids": "class ChainId: pass\n",
                "robinhood_lp.features.bars": ("from robinhood_lp.protocol.ids import ChainId\n"),
            },
            tasks=[("T010", "P01", "APPROVED"), ("T050", "P05", "APPROVED")],
            architecture=textwrap.dedent(
                """\
                # Architecture

                ### 2.2

                | Phase | Task | Module | Layer |
                | --- | --- | --- | --- |
                | 1 | T010 | `robinhood_lp.protocol.ids` | protocol/domain |
                | 5 | T050 | `robinhood_lp.features.bars` | features |
                """
            ),
        )
        findings = run(root)
        assert findings == [], format_findings(findings, root)


# ---------------------------------------------------------------------------
# Deliverable 2 — seeded regressions
# ---------------------------------------------------------------------------


def test_layer_direction_seeded_regression() -> None:
    """A lower-to-higher import is a fatal finding."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        # Protocol imports presentation: this is the canonical
        # forbidden direction in ADR-006's diagram. protocol/domain
        # sits at the bottom of the tier order and presentation /
        # reports at the top, so this is the lower-to-higher edge
        # the rule forbids.
        _make_repo(
            root,
            module_tree={
                "robinhood_lp.protocol.ids": (
                    "from robinhood_lp.presentation.prices import DECIMAL_QUANTUM\n"
                    "DECIMAL_QUANTUM = 1\n"
                ),
                "robinhood_lp.presentation.prices": ("DECIMAL_QUANTUM = 1\n"),
            },
            tasks=[("T010", "P01", "APPROVED"), ("T081", "P08", "APPROVED")],
            architecture=textwrap.dedent(
                """\
                # Architecture

                ### 2.2

                | Phase | Task | Module | Layer |
                | --- | --- | --- | --- |
                | 1 | T010 | `robinhood_lp.protocol.ids` | protocol/domain |
                | 8 | T081 | `robinhood_lp.presentation.prices` | presentation / reports |
                """
            ),
        )
        findings = run(root)
        rules = {finding.rule for finding in findings}
        tokens = {finding.token for finding in findings}
        assert "layer-direction" in rules, format_findings(findings, root)
        assert "robinhood_lp.protocol.ids -> robinhood_lp.presentation.prices" in tokens, tokens


def test_import_cycle_seeded_regression() -> None:
    """A cycle between two packages is a fatal finding."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        # A -> B and B -> A forms a package-level cycle. The two
        # modules live in different packages so the cycle detector's
        # package-level view sees it.
        _make_repo(
            root,
            module_tree={
                "robinhood_lp.protocol.ids": (
                    "from robinhood_lp.features.bars import BARS_KIND\nBARS_KIND = 1\n"
                ),
                "robinhood_lp.features.bars": (
                    "from robinhood_lp.protocol.ids import ChainId\nBARS_KIND = 1\n"
                ),
            },
            tasks=[("T001", "P00", "APPROVED")],
        )
        findings = run(root)
        rules = {finding.rule for finding in findings}
        assert "import-cycle" in rules, format_findings(findings, root)


def test_unseeded_clock_seeded_regression() -> None:
    """A ``time`` import in a forbidden tier is a fatal finding."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(
            root,
            module_tree={
                "robinhood_lp.protocol.ids": (
                    "import time\ndef now() -> int:\n    return int(time.time())\n"
                ),
            },
            tasks=[("T010", "P01", "APPROVED")],
            architecture=textwrap.dedent(
                """\
                # Architecture

                ### 2.2

                | Phase | Task | Module | Layer |
                | --- | --- | --- | --- |
                | 1 | T010 | `robinhood_lp.protocol.ids` | protocol/domain |
                """
            ),
        )
        findings = run(root)
        rules = {finding.rule for finding in findings}
        tokens = {finding.token for finding in findings}
        assert "unseeded-clock" in rules, format_findings(findings, root)
        assert "time" in tokens


def test_unclassified_module_seeded_regression() -> None:
    """A module the map does not classify is a fatal finding."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        # Add a brand-new package the layer map does not cover. The
        # checker must report every file in it as unclassified.
        _make_repo(
            root,
            module_tree={
                "robinhood_lp.unknown_pkg.foo": "x = 1\n",
            },
            tasks=[("T001", "P00", "APPROVED")],
            architecture=textwrap.dedent(
                """\
                # Architecture

                ### 2.2

                | Phase | Task | Module | Layer |
                | --- | --- | --- | --- |
                """
            ),
        )
        findings = run(root)
        rules = {finding.rule for finding in findings}
        tokens = {finding.token for finding in findings}
        assert "unclassified-module" in rules, format_findings(findings, root)
        assert "robinhood_lp.unknown_pkg.foo" in tokens


# ---------------------------------------------------------------------------
# Deliverable 3 — §2.2 agreement seeded regression
# ---------------------------------------------------------------------------


def test_architecture_disagreement_seeded_regression() -> None:
    """A §2.2 row whose layer disagrees with the map is a fatal finding."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(
            root,
            module_tree={
                "robinhood_lp.protocol.ids": "class ChainId: pass\n",
                "robinhood_lp.features.bars": "x = 1\n",
            },
            tasks=[("T050", "P05", "APPROVED")],
            # The map classifies features.bars as ``features``; the
            # row below declares it as ``risk``. The check must
            # report the disagreement and refuse to pass.
            architecture=textwrap.dedent(
                """\
                # Architecture

                ### 2.2

                | Phase | Task | Module | Layer |
                | --- | --- | --- | --- |
                | 5 | T050 | `robinhood_lp.features.bars` | risk |
                """
            ),
        )
        findings = run(root)
        rules = {finding.rule for finding in findings}
        tokens = {finding.token for finding in findings}
        assert "architecture-disagreement" in rules, format_findings(findings, root)
        assert "robinhood_lp.features.bars" in tokens


# ---------------------------------------------------------------------------
# Fail-closed inputs
# ---------------------------------------------------------------------------


def test_empty_module_set_fails_closed() -> None:
    """No ``src/robinhood_lp/*.py`` at all is a fatal input."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        # Provide the minimum the checker needs to reach the
        # module-set step, but ensure the discovered module set is
        # empty.
        _make_repo(
            root,
            tasks=[("T001", "P00", "APPROVED")],
            module_tree={"robinhood_lp.__init__": ""},
        )
        # Remove every module to leave an empty module set.
        for path in (root / "src" / "robinhood_lp").rglob("*.py"):
            path.unlink()
        findings = run(root)
        assert any(f.rule == "module-set" for f in findings), format_findings(findings, root)


def test_unreadable_suppression_entry_fails_closed() -> None:
    """A malformed ``suppressions.toml`` is itself a fatal failure."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(
            root,
            module_tree={"robinhood_lp.protocol.ids": "x = 1\n"},
            tasks=[("T001", "P00", "APPROVED")],
            suppressions="this is not valid TOML !!!\n",
        )
        findings = run(root)
        assert any(f.rule == "suppressions" for f in findings), format_findings(findings, root)


def test_unparsable_section22_row_fails_closed() -> None:
    """A §2.2 row that references a task absent from ``config.yaml`` fails closed."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(
            root,
            module_tree={"robinhood_lp.protocol.ids": "x = 1\n"},
            tasks=[("T001", "P00", "APPROVED")],
            architecture=textwrap.dedent(
                """\
                # Architecture

                ### 2.2

                | Phase | Task | Module | Layer |
                | --- | --- | --- | --- |
                | 0 | T999 | `robinhood_lp.unknown.module` | protocol/domain |
                """
            ),
        )
        findings = run(root)
        assert any(
            f.rule == "architecture-disagreement" and "T999" in f.message for f in findings
        ), format_findings(findings, root)


def test_unused_suppression_is_fatal() -> None:
    """A suppression entry that no longer matches a finding is itself fatal."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(
            root,
            module_tree={"robinhood_lp.protocol.ids": "x = 1\n"},
            tasks=[("T001", "P00", "APPROVED")],
            suppressions=textwrap.dedent(
                """\
                [[suppression]]
                rule = "layer-direction"
                reason = "stale entry that no longer matches anything"
                owner = "@dev"
                expiry = "2026-12-31"
                token = "robinhood_lp.no.such.module -> robinhood_lp.other"
                """
            ),
        )
        findings = run(root)
        assert any(
            f.rule == "suppression-unused" and "robinhood_lp.no.such.module" in f.token
            for f in findings
        ), format_findings(findings, root)


# ---------------------------------------------------------------------------
# Real-repository sanity check
# ---------------------------------------------------------------------------


def test_check_passes_on_real_repository() -> None:
    """The committed repository passes the import-graph check.

    The ``robinhood_lp.config`` exception is recorded in
    ``docs/implement/ci/suppressions.toml`` with an expiry that
    forces the owner to revisit the case before the platform layer
    ADR lands.
    """

    findings = run(REPO)
    assert findings == [], format_findings(findings, REPO)


def test_module_set_covers_real_repository() -> None:
    """The declared module set is non-empty on the committed repo."""

    rel = {path.resolve().relative_to(REPO.resolve()).as_posix() for path in module_set(REPO)}
    assert any(path.startswith("src/robinhood_lp/") for path in rel)
    assert "src/robinhood_lp/protocol/ids.py" in rel


def test_finding_dataclass_supports_set_membership() -> None:
    """The ``Finding`` dataclass is hashable so duplicate findings collapse."""

    finding = Finding(
        rule="layer-direction",
        path="src/robinhood_lp/protocol/ids.py",
        line=10,
        token="A -> B",
        message="example",
    )
    bucket = {finding, finding}
    assert len(bucket) == 1


def test_format_findings_handles_empty() -> None:
    """The formatter announces an empty result explicitly."""

    text = format_findings([], REPO)
    assert "passed" in text


@pytest.mark.parametrize(
    "module_path",
    [
        "robinhood_lp.protocol.ids",
        "robinhood_lp.features.bars",
        "robinhood_lp.qualification.reference",
    ],
)
def test_real_modules_are_classified(module_path: str) -> None:
    """Spot-check that real modules are classified by the layer map."""

    from tools.check_imports.layer_map import classify

    assert classify(module_path) is not None, module_path
