"""Repository-level checks for the split Intent/Spec/Implement workflow."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tools.workflow import WorkflowManager

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SECTIONS = (
    "## Dependencies",
    "## Outcome",
    "## Deliverables",
    "## Acceptance",
    "## Must not",
    "## References",
)


def test_repository_workflow_configuration_is_valid() -> None:
    manager = WorkflowManager(ROOT)
    manager.validate_repository()
    config = manager.load_config()

    configured_contracts = {task["task_file"] for task in config["tasks"].values()}
    discovered_contracts = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "todo" / "phases").glob("P*/T[0-9][0-9][0-9].md")
    }
    assert configured_contracts == discovered_contracts
    # Delivered work is what the lifecycle says it is: the composite the
    # controller used to store beside those facts is retired.
    assert config["tasks"]["T000"]["lifecycle"] == "DELIVERED"
    assert config["tasks"]["T004"]["lifecycle"] == "DELIVERED"
    assert {"T000", "T004"} <= {
        task_id for task_id, task in config["tasks"].items() if task["lifecycle"] == "DELIVERED"
    }
    assert not any("status" in task for task in config["tasks"].values())


def test_python_workflow_never_launches_claude() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "tools" / "workflow").glob("*.py")
    )
    assert "_launch_agent" not in sources
    assert "find_claude" not in sources
    assert '"claude"' not in sources


def test_manager_can_invoke_every_amendment_role_returned_by_controller() -> None:
    manager = (ROOT / ".claude" / "agents" / "workflow-manager.md").read_text(encoding="utf-8")
    assert "prophet" in manager
    assert "prophet-reviewer" in manager
    planner = (ROOT / ".claude" / "agents" / "planner.md").read_text(encoding="utf-8")
    assert "`SUPERSEDE` is also yours" in planner


def test_every_task_is_a_separate_complete_contract() -> None:
    config = json.loads((ROOT / "todo" / "config.yaml").read_text(encoding="utf-8"))
    for task_id, task in config["tasks"].items():
        path = ROOT / task["task_file"]
        text = path.read_text(encoding="utf-8")
        assert text.startswith(f"# {task_id} —")
        for section in REQUIRED_SECTIONS:
            assert text.count(section) == 1, f"{task_id} missing or duplicates {section}"
        dependency_text = text.split("## Dependencies", 1)[1].split("## ", 1)[0]
        assert sorted(set(re.findall(r"T\d{3}", dependency_text))) == task["depends_on"]


def test_binding_documents_use_the_new_layer_paths() -> None:
    forbidden = (
        "docs/product/",
        "docs/specs/",
        "docs/architecture.md",
        "docs/adr/",
        "docs/threat-model.md",
        "docs/protocol-facts.md",
        "docs/STATUS.md",
        "docs/protocol-artifacts/",
        "TODO.md",
    )
    roots = (
        ROOT / "AGENTS.md",
        ROOT / "CLAUDE.md",
        ROOT / "README.md",
        ROOT / "docs",
        ROOT / "todo" / "README.md",
        ROOT / "todo" / "phases",
        ROOT / "src",
        ROOT / "tests",
        ROOT / "tools",
    )
    checked: list[Path] = []
    for root in roots:
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if (
                not path.is_file()
                or path == Path(__file__).resolve()
                or "todo/evidence/legacy" in path.as_posix()
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            checked.append(path)
            for old_path in forbidden:
                assert old_path not in text, f"stale path {old_path} in {path}"
    assert checked
