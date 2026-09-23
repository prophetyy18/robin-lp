"""Mechanical workflow gates and isolated FAIL -> repair -> PASS exercise."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tools.workflow import WorkflowError, WorkflowManager
from tools.workflow.core import (
    ALLOWED_TRANSITIONS,
    LIFECYCLE_OF,
    STATES,
    AttemptRecord,
    admitted_statuses,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _impact_assessment() -> dict[str, str]:
    return {
        "intent": "checked",
        "specification": "checked",
        "contracts": "checked",
        "dependencies": "checked",
        "implementation": "checked",
        "data": "not applicable in this fixture",
        "operations": "not applicable in this fixture",
        "security": "checked",
        "verification": "checked",
    }


def _decompose(state: str) -> dict[str, object]:
    """The two facts that carry a state, from the controller's one table.

    Fixtures are written the way the controller writes a revision now. A fixture
    that still carried the retired composite would exercise a shape the workflow
    no longer produces -- and would have hidden the readers that kept asking for
    it, which is exactly how three of them were found.
    """

    lifecycle, claimed = LIFECYCLE_OF[state]
    return {"lifecycle": lifecycle, "claimed": claimed}


def _set_task_state(config: dict[str, Any], task_id: str, state: str, **fields: object) -> None:
    """Move a task the way the controller would, decomposition included.

    Tests that hand-edit a state have to hand-edit the two facts it is built
    from, or `validate_config` refuses the config -- which is the point of the
    decomposition, and is asserted on its own elsewhere.
    """
    config["tasks"][task_id].update(_decompose(state), **fields)


def _set_legacy_state(config: dict[str, Any], task_id: str, status: str, **fields: object) -> None:
    """Move a task the way the controller did before the composite was retired.

    A revision that still carries `status` is supported and checked -- every
    revision in Git is one -- so the tests that are *about* that field write it,
    and the tests that are about the current shape leave it out.
    """
    config["tasks"][task_id].update(_decompose(status), status=status, **fields)


def _minimal_config(baseline: str) -> dict[str, object]:
    common = {
        "attempt": 0,
        "base_commit": None,
        "candidate_commit": None,
        "latest_review": None,
    }
    return {
        "schema_version": 1,
        "project": "robinhood-lp-v1",
        "product_baseline_commit": baseline,
        "intent_revision": "test-intent",
        "spec_revision": "test-spec",
        "agent_runtime": {
            "provider": "MiniMax Anthropic-compatible API",
            "model": "MiniMax-M3[1m]",
            "context_window_tokens": 1_000_000,
            "allow_model_fallback": False,
        },
        "active_phase": "P00",
        "active_task": "T001",
        "workflow_state": "READY",
        "tasks": {
            "T000": {
                "phase": "P00",
                "depends_on": [],
                "task_file": "todo/phases/P00/T000.md",
                "approved_commit": baseline,
                **_decompose("APPROVED"),
                **common,
            },
            "T001": {
                "phase": "P00",
                "depends_on": ["T000"],
                "task_file": "todo/phases/P00/T001.md",
                "approved_commit": None,
                **_decompose("READY"),
                **common,
            },
        },
    }


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Workflow Test")
    _git(repo, "config", "user.email", "workflow@example.invalid")
    for task_id in ("T000", "T001"):
        task = repo / "todo" / "phases" / "P00" / f"{task_id}.md"
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text(
            f"# {task_id}\n\n## Dependencies\n\nNone.\n\n"
            "## Outcome\n\nTest outcome.\n\n## Deliverables\n\nTest file.\n\n"
            "## Acceptance\n\nValue is good.\n\n## Must not\n\nDo not edit contracts.\n\n"
            "## References\n\nNone.\n\n## Replacement and migration\n\n"
            "T001 replaces T000; old history remains immutable.\n",
            encoding="utf-8",
        )
    (repo / "src").mkdir()
    (repo / "src" / "value.txt").write_text("base\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("test policy\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("test policy\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")
    (repo / "todo" / "README.md").write_text("test workflow\n", encoding="utf-8")
    for agent in (
        # `validate_repository` requires every role, including the Manager, so a
        # fixture that omits one cannot exercise it at all.
        "workflow-manager",
        "stage-developer",
        "stage-reviewer",
        "issue-triager",
        "planner",
        "plan-reviewer",
        "prophet",
        "prophet-reviewer",
    ):
        path = repo / ".claude" / "agents" / f"{agent}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {agent}\ndescription: test\n---\n", encoding="utf-8")
    schema_dir = repo / "todo" / "schemas"
    # `validate_repository` loads every schema, so the fixture carries all of
    # them; with a partial set it could not be exercised here at all.
    for schema in (
        "config",
        "amendment-result",
        "amendment-review-result",
        "developer-result",
        "review-result",
        "triage-result",
        "planner-result",
        "plan-review-result",
    ):
        _write_json(schema_dir / f"{schema}.schema.json", {"type": "object"})
    _write_json(repo / "todo" / "config.yaml", _minimal_config("0" * 40))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    baseline = _git(repo, "rev-parse", "HEAD")
    config = _minimal_config(baseline)
    config["tasks"]["T000"]["approved_commit"] = baseline  # type: ignore[index]
    _write_json(repo / "todo" / "config.yaml", config)
    _git(repo, "add", "todo/config.yaml")
    _git(repo, "commit", "-m", "record baseline")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_visible_manager_prepare_and_finish_round_trip(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")

    prepared = manager.prepare_develop("T001")
    development = Path(str(prepared["development_worktree"]))
    assert prepared["agent"] == "stage-developer"
    (development / "src" / "value.txt").write_text("good\n", encoding="utf-8")
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "T001",
            "outcome": "CANDIDATE_READY",
            "summary": "visible developer completed the task",
            "commands": [{"command": "fake-check", "result": "passed"}],
            "residual_risks": [],
            "blocking_question": None,
        },
    )
    attempt = manager.finish_develop("T001")
    assert attempt.candidate_commit

    review = manager.prepare_review("T001")
    review_worktree = Path(str(review["review_worktree"]))
    assert review["agent"] == "stage-reviewer"
    _write_json(
        review_worktree / ".workflow" / "review-result.json",
        {
            "task_id": "T001",
            "base_commit": attempt.base_commit,
            "candidate_commit": attempt.candidate_commit,
            "verdict": "PASS",
            "checks": [
                {
                    "id": "ACCEPTANCE",
                    "status": "PASS",
                    "evidence": ["src/value.txt contains good"],
                    "finding": "acceptance satisfied",
                }
            ],
            "must_not_violations": [],
            "unknowns": [],
            "required_changes": [],
            "residual_risks": [],
        },
    )
    state, report = manager.finish_review("T001")
    assert state == "APPROVED"
    assert report.is_file()
    assert manager.status()["workflow_state"] == "APPROVED"


def test_developer_continuation_preserves_attempt_worktree_and_state(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")
    development = Path(str(prepared["development_worktree"]))
    (development / "src" / "value.txt").write_text("partially repaired\n", encoding="utf-8")
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "T001",
            "outcome": "CONTINUATION_REQUIRED",
            "summary": "implementation is healthy but needs another session",
            "commands": [{"command": "targeted-check", "result": "passed"}],
            "residual_risks": [],
            "continuation": {
                "reason": "TURN_BUDGET",
                "completed_work": ["implemented the main path"],
                "remaining_work": ["add the failure-path test"],
                "next_actions": ["inspect the retained diff and add the test"],
                "changed_paths": ["src/value.txt"],
            },
        },
    )

    continued = manager.continue_develop("T001")

    assert continued["attempt"] == prepared["attempt"]
    assert continued["branch"] == prepared["branch"]
    assert continued["development_worktree"] == prepared["development_worktree"]
    assert continued["continuation_count"] == 1
    assert manager.status()["workflow_state"] == "IN_DEVELOPMENT"
    assert (development / ".workflow" / "developer-continuation.json").is_file()
    assert not (development / ".workflow" / "developer-result.json").exists()

    (development / "src" / "value.txt").write_text("good\n", encoding="utf-8")
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "T001",
            "outcome": "CANDIDATE_READY",
            "summary": "continued session completed the task",
            "commands": [{"command": "full-check", "result": "passed"}],
            "residual_risks": [],
        },
    )
    candidate = manager.finish_develop("T001")

    assert candidate.candidate_commit is not None
    assert manager.status()["workflow_state"] == "AWAITING_REVIEW"
    assert _git(development, "status", "--porcelain") == ""


def test_max_turns_continuation_recovers_without_developer_handoff(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")
    development = Path(str(prepared["development_worktree"]))
    (development / "src" / "value.txt").write_text("partial\n", encoding="utf-8")

    continued = manager.continue_develop("T001", max_turns_exhausted=True)
    checkpoint = json.loads(
        (development / ".workflow" / "developer-continuation.json").read_text(encoding="utf-8")
    )

    assert continued["continuation_count"] == 1
    assert checkpoint["outcome"] == "CONTINUATION_REQUIRED"
    assert checkpoint["continuation"]["changed_paths"] == ["src/value.txt"]
    assert manager.status()["workflow_state"] == "IN_DEVELOPMENT"


def test_continuation_requires_handoff_or_explicit_max_turns_signal(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    manager.prepare_develop("T001")

    with pytest.raises(WorkflowError, match="CONTINUATION_REQUIRED handoff"):
        manager.continue_develop("T001")


def test_development_attempt_allows_only_one_continuation(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    manager.prepare_develop("T001")
    manager.continue_develop("T001", max_turns_exhausted=True)

    with pytest.raises(WorkflowError, match="continuation limit reached"):
        manager.continue_develop("T001", max_turns_exhausted=True)


def _finish_seed_task_for_maintenance(repo: Path, manager: WorkflowManager) -> None:
    config = manager.load_config()
    _set_task_state(config, "T001", "APPROVED", approved_commit=_git(repo, "rev-parse", "HEAD"))
    config["active_task"] = "T001"
    config["active_phase"] = "P00"
    config["workflow_state"] = "APPROVED"
    _write_json(repo / "todo" / "config.yaml", config)
    _git(repo, "add", "todo/config.yaml")
    _git(repo, "commit", "-m", "approve seed task")


def _make_future_task_planned(repo: Path, manager: WorkflowManager) -> None:
    config = manager.load_config()
    _set_task_state(config, "T001", "PLANNED")
    config["active_task"] = "T000"
    config["active_phase"] = "P00"
    config["workflow_state"] = "APPROVED"
    _write_json(repo / "todo" / "config.yaml", config)
    _git(repo, "add", "todo/config.yaml")
    _git(repo, "commit", "-m", "leave future task planned")


def _declare_successor(repo: Path, manager: WorkflowManager) -> None:
    config = manager.load_config()
    config["tasks"]["T001"]["replaces"] = ["T000"]
    _write_json(repo / "todo" / "config.yaml", config)
    _git(repo, "add", "todo/config.yaml")
    _git(repo, "commit", "-m", "declare successor")


def test_owner_amendment_updates_planned_contract_without_activating_task(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    prepared = manager.prepare_amendment(
        task_ids=["T001"],
        layer="CONTRACT",
        summary="clarify future acceptance",
        owner_direction="Require an explicit deterministic assertion.",
    )
    with pytest.raises(WorkflowError, match="owner amendment is unfinished"):
        manager.ready("T001")
    amendment = Path(str(prepared["worktree"]))
    task = amendment / "todo" / "phases" / "P00" / "T001.md"
    task.write_text(task.read_text(encoding="utf-8") + "\nOwner clarification.\n", encoding="utf-8")
    _write_json(
        amendment / ".workflow" / "amendment-result.json",
        {
            "amendment_id": "A0001",
            "outcome": "AMENDMENT_READY",
            "summary": "clarified the target contract",
            "rationale": "implements the recorded Owner direction",
            "unresolved_questions": [],
            "impact_assessment": _impact_assessment(),
            "affected_existing_tasks": [],
            "resolved_task_impacts": [],
        },
    )
    candidate = manager.finish_amendment("A0001")
    assert candidate.status == "AWAITING_REVIEW"
    review = manager.prepare_amendment_review("A0001")
    review_worktree = Path(str(review["review_worktree"]))
    _write_json(
        review_worktree / ".workflow" / "amendment-review-result.json",
        {
            "amendment_id": "A0001",
            "base_commit": candidate.base_commit,
            "candidate_commit": candidate.candidate_commit,
            "verdict": "PASS",
            "summary": "direction and scope are satisfied",
            "required_changes": [],
            "unknowns": [],
        },
    )

    state, report = manager.finish_amendment_review("A0001")

    assert state == "APPROVED"
    assert report.is_file()
    assert "Owner clarification" in (repo / "todo" / "phases" / "P00" / "T001.md").read_text(
        encoding="utf-8"
    )
    config = manager.load_config()
    assert (config["tasks"]["T001"]["lifecycle"], config["tasks"]["T001"]["claimed"]) == (
        LIFECYCLE_OF["PLANNED"]
    )
    assert config["tasks"]["T001"]["attempt"] == 0
    assert config["active_task"] == "T000"
    # A closed change keeps its record: the ID stays consumed, so the next
    # amendment cannot be handed a number whose branch and worktree path this
    # one still used. The status therefore reports the closed record rather than
    # the two-field shape a deleted record used to be reconstructed into.
    closed = manager.amendment_status("A0001")
    assert closed["amendment_id"] == "A0001"
    assert closed["status"] == "APPROVED"


def test_supersede_amendment_retires_an_approved_task(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    _declare_successor(repo, manager)
    before = manager.load_config()["tasks"]["T000"]
    prepared = manager.prepare_amendment(
        task_ids=["T000"],
        layer="SUPERSEDE",
        summary="retire the T000 decision in favour of T001",
        owner_direction="T001 supersedes T000; record the successor and keep the history.",
    )
    assert prepared["layer"] == "SUPERSEDE"
    assert prepared["agent"] == "planner"
    amendment = Path(str(prepared["worktree"]))
    request = json.loads(
        (amendment / ".workflow" / "amendment-request.json").read_text(encoding="utf-8")
    )
    assert request["target_status"] == "APPROVED"
    config_path = amendment / "todo" / "config.yaml"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["tasks"]["T000"]["superseded_by"] = "T001"
    _write_json(config_path, config)
    _write_json(
        amendment / ".workflow" / "amendment-result.json",
        {
            "amendment_id": "A0001",
            "outcome": "AMENDMENT_READY",
            "summary": "pointed T000 at its successor",
            "rationale": "implements the recorded Owner direction",
            "unresolved_questions": [],
            "impact_assessment": _impact_assessment(),
            "affected_existing_tasks": [],
            "resolved_task_impacts": [],
        },
    )
    candidate = manager.finish_amendment("A0001")
    review = manager.prepare_amendment_review("A0001")
    review_worktree = Path(str(review["review_worktree"]))
    _write_json(
        review_worktree / ".workflow" / "amendment-review-result.json",
        {
            "amendment_id": "A0001",
            "base_commit": candidate.base_commit,
            "candidate_commit": candidate.candidate_commit,
            "verdict": "PASS",
            "summary": "retirement is recorded and the history is untouched",
            "required_changes": [],
            "unknowns": [],
        },
    )
    state, _ = manager.finish_amendment_review("A0001")
    assert state == "APPROVED"
    after = manager.load_config()["tasks"]["T000"]
    assert after["superseded_by"] == "T001"
    # Retirement is an annotation on approved work: the approval evidence stays
    # byte-identical so the audit record still describes what was reviewed.
    assert after["lifecycle"] == "DELIVERED"
    assert after["attempt"] == before["attempt"]
    assert after["approved_commit"] == before["approved_commit"]
    assert after["base_commit"] == before["base_commit"]


def test_supersede_amendment_cannot_alter_approved_evidence(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    _declare_successor(repo, manager)
    before = manager.load_config()["tasks"]["T000"]
    prepared = manager.prepare_amendment(
        task_ids=["T000"],
        layer="SUPERSEDE",
        summary="retire the T000 decision",
        owner_direction="Retire T000 in favour of T001.",
    )
    amendment = Path(str(prepared["worktree"]))
    config_path = amendment / "todo" / "config.yaml"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["tasks"]["T000"]["superseded_by"] = "T001"
    config["tasks"]["T000"]["approved_commit"] = None
    _write_json(config_path, config)
    _write_json(
        amendment / ".workflow" / "amendment-result.json",
        {
            "amendment_id": "A0001",
            "outcome": "AMENDMENT_READY",
            "summary": "retired the task and cleared its approval",
            "rationale": "implements the recorded Owner direction",
            "unresolved_questions": [],
            "impact_assessment": _impact_assessment(),
            "affected_existing_tasks": [],
            "resolved_task_impacts": [],
        },
    )
    with pytest.raises(WorkflowError, match="workflow state, evidence, model, SHA"):
        manager.finish_amendment("A0001")
    assert manager.load_config()["tasks"]["T000"]["approved_commit"] == before["approved_commit"]


def test_supersede_amendment_cannot_edit_the_approved_contract(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    _declare_successor(repo, manager)
    prepared = manager.prepare_amendment(
        task_ids=["T000"],
        layer="SUPERSEDE",
        summary="retire T000",
        owner_direction="T001 replaces T000.",
    )
    amendment = Path(str(prepared["worktree"]))
    contract = amendment / "todo" / "phases" / "P00" / "T000.md"
    contract.write_text(contract.read_text(encoding="utf-8") + "\nrewritten\n", encoding="utf-8")
    config_path = amendment / "todo" / "config.yaml"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["tasks"]["T000"]["superseded_by"] = "T001"
    _write_json(config_path, config)
    _seal_prophet_result(amendment)

    with pytest.raises(WorkflowError, match="outside the owner amendment"):
        manager.finish_amendment("A0001")


def test_supersede_refuses_to_strand_a_planned_consumer(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    _declare_successor(repo, manager)
    (repo / "todo" / "phases" / "P00" / "T002.md").write_text(_NEW_CONTRACT, encoding="utf-8")
    _add_task_to_config(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add another planned consumer")
    prepared = manager.prepare_amendment(
        task_ids=["T000"],
        layer="SUPERSEDE",
        summary="retire T000",
        owner_direction="T001 replaces T000 after consumers move.",
    )
    amendment = Path(str(prepared["worktree"]))
    config_path = amendment / "todo" / "config.yaml"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["tasks"]["T000"]["superseded_by"] = "T001"
    _write_json(config_path, config)
    _seal_prophet_result(amendment)

    with pytest.raises(WorkflowError, match="PLANNED consumers still depend"):
        manager.finish_amendment("A0001")


def test_amendment_layers_require_their_target_status(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    with pytest.raises(WorkflowError, match="SUPERSEDE amendment targets must be APPROVED"):
        manager.prepare_amendment(
            task_ids=["T001"],
            layer="SUPERSEDE",
            summary="invalid target status",
            owner_direction="Not a valid retirement target.",
        )
    with pytest.raises(WorkflowError, match="CONTRACT amendment targets must be PLANNED"):
        manager.prepare_amendment(
            task_ids=["T000"],
            layer="CONTRACT",
            summary="invalid target status",
            owner_direction="Not a valid planning target.",
        )
    with pytest.raises(WorkflowError, match="amendment layer must be"):
        manager.prepare_amendment(
            task_ids=["T001"],
            layer="WHATEVER",
            summary="invalid layer",
            owner_direction="Not a valid layer.",
        )


def test_validate_config_rejects_invalid_superseded_by(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    valid = manager.load_config()

    unknown = json.loads(json.dumps(valid))
    unknown["tasks"]["T000"]["superseded_by"] = "T099"
    with pytest.raises(WorkflowError, match="references unknown task"):
        manager.validate_config(unknown)

    itself = json.loads(json.dumps(valid))
    itself["tasks"]["T000"]["superseded_by"] = "T000"
    with pytest.raises(WorkflowError, match="must not reference itself"):
        manager.validate_config(itself)

    malformed = json.loads(json.dumps(valid))
    malformed["tasks"]["T000"]["superseded_by"] = "T1"
    with pytest.raises(WorkflowError, match="superseded_by must be a task ID"):
        manager.validate_config(malformed)

    not_approved = json.loads(json.dumps(valid))
    not_approved["tasks"]["T001"]["superseded_by"] = "T000"
    with pytest.raises(WorkflowError, match="superseded_by requires APPROVED status"):
        manager.validate_config(not_approved)


def test_a_superseded_dependency_must_be_repointed(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    config["tasks"]["T000"]["superseded_by"] = "T002"
    with pytest.raises(WorkflowError, match="depends on superseded task"):
        manager._check_dependencies(config, "T001")
    # A successor legitimately builds on the work it replaces: T000 names T001 as
    # its successor, so T001 depending on T000 is the deliberate case, not the trap.
    config["tasks"]["T000"]["superseded_by"] = "T001"
    manager._check_dependencies(config, "T001")


def test_owner_amendment_rejects_an_unfinished_active_task(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")

    with pytest.raises(WorkflowError, match="T001 is unfinished"):
        manager.prepare_amendment(
            task_ids=["T001"],
            layer="CONTRACT",
            summary="invalid concurrent amendment",
            owner_direction="Change the active task.",
        )


def test_owner_amendment_rejects_an_untargeted_contract_change(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    prepared = manager.prepare_amendment(
        task_ids=["T001"],
        layer="CONTRACT",
        summary="clarify future acceptance",
        owner_direction="Change only T001.",
    )
    amendment = Path(str(prepared["worktree"]))
    other = amendment / "todo" / "phases" / "P00" / "T000.md"
    other.write_text(other.read_text(encoding="utf-8") + "\nout of scope\n", encoding="utf-8")
    _write_json(
        amendment / ".workflow" / "amendment-result.json",
        {
            "amendment_id": "A0001",
            "outcome": "AMENDMENT_READY",
            "summary": "changed the wrong task",
            "rationale": "invalid test fixture",
            "unresolved_questions": [],
            "impact_assessment": _impact_assessment(),
            "affected_existing_tasks": [],
            "resolved_task_impacts": [],
        },
    )

    with pytest.raises(WorkflowError, match="outside the owner amendment"):
        manager.finish_amendment("A0001")


def test_owner_amendment_review_failure_starts_fresh_planner_retry(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    prepared = manager.prepare_amendment(
        task_ids=["T001"],
        layer="CONTRACT",
        summary="clarify future acceptance",
        owner_direction="Require an explicit deterministic assertion.",
    )
    amendment = Path(str(prepared["worktree"]))
    _write_json(
        amendment / ".workflow" / "amendment-result.json",
        {
            "amendment_id": "A0001",
            "outcome": "NO_CHANGE_REQUIRED",
            "summary": "incorrectly claimed no change was needed",
            "rationale": "candidate for rejection",
            "unresolved_questions": [],
            "impact_assessment": _impact_assessment(),
            "affected_existing_tasks": [],
            "resolved_task_impacts": [],
        },
    )
    candidate = manager.finish_amendment("A0001")
    review = manager.prepare_amendment_review("A0001")
    review_worktree = Path(str(review["review_worktree"]))
    _write_json(
        review_worktree / ".workflow" / "amendment-review-result.json",
        {
            "amendment_id": "A0001",
            "base_commit": candidate.base_commit,
            "candidate_commit": candidate.candidate_commit,
            "verdict": "FAIL",
            "summary": "the Owner direction requires a contract change",
            "required_changes": ["add the deterministic assertion"],
            "unknowns": [],
        },
    )
    state, _ = manager.finish_amendment_review("A0001")

    retry = manager.prepare_amendment_retry("A0001")

    assert state == "CHANGES_REQUESTED"
    assert retry["agent"] == "planner"
    assert retry["attempt"] == 2
    assert retry["worktree"] == prepared["worktree"]
    assert manager.amendment_status("A0001")["status"] == "PLANNING"


def test_finish_amendment_refuses_a_candidate_that_adds_a_gate_finding(
    tmp_path: Path,
) -> None:
    """A candidate that adds a finding to a repository gate is refused at seal time.

    The comparison is a delta against the attempt's base, never "no findings at
    all": an already-red repository must stay amendable. What it refuses is
    sealing a candidate that *adds* a finding the independent review would
    otherwise have to discover by hand.
    """

    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    # The citation gate reads its suppressions file from the repository and stops
    # before scanning documents when ``src/robinhood_lp`` is absent, so the
    # fixture has to provide both before the gate can be evaluated at all.
    ci = repo / "docs" / "implement" / "ci"
    ci.mkdir(parents=True, exist_ok=True)
    (ci / "suppressions.toml").write_text("suppressions = []\n", encoding="utf-8")
    package = repo / "src" / "robinhood_lp"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add the files the citation gate reads")
    _make_future_task_planned(repo, manager)
    prepared = manager.prepare_amendment(
        task_ids=["T001"],
        layer="CONTRACT",
        summary="a change whose candidate breaks the citation gate",
        owner_direction="Record exactly this change and nothing else.",
    )
    amendment = Path(str(prepared["worktree"]))
    # The contract file is inside the amendment's own path scope and inside the
    # citation checker's document set, so citing an undeclared task there is a
    # finding the seal-time gate must catch.
    contract = amendment / "todo" / "phases" / "P00" / "T001.md"
    contract.write_text(
        contract.read_text(encoding="utf-8") + "\nSee `T999` for the follow-up.\n",
        encoding="utf-8",
    )
    _write_json(
        amendment / ".workflow" / "amendment-result.json",
        {
            "amendment_id": "A0001",
            "outcome": "AMENDMENT_READY",
            "summary": "candidate that breaks a deterministic gate",
            "rationale": "seeded regression for the seal-time gate",
            "unresolved_questions": [],
            "impact_assessment": _impact_assessment(),
            "affected_existing_tasks": [],
            "resolved_task_impacts": [],
        },
    )

    with pytest.raises(WorkflowError) as error:
        manager.finish_amendment("A0001")

    assert "check_citations" in str(error.value)
    assert "T999" in str(error.value)


def test_withdrawing_a_stuck_amendment_frees_the_lane_without_rewinding_ids(
    tmp_path: Path,
) -> None:
    """A change that cannot land must be closeable without deleting its record.

    The reported defect: only a PASS cleared the record, so an amendment whose
    own layer could not repair it blocked every later amendment -- and clearing
    the record by hand rewound the ID counter onto a branch and worktree path
    the failed attempt still occupied.
    """

    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    prepared = manager.prepare_amendment(
        task_ids=["T001"],
        layer="CONTRACT",
        summary="a change that will not land",
        owner_direction="An Owner direction this layer cannot satisfy.",
    )
    amendment = Path(str(prepared["worktree"]))

    closed = manager.withdraw_amendment(
        "A0001", reason="the recorded direction cannot be satisfied in this layer"
    )

    assert closed["status"] == "ABANDONED"
    withdrawal = amendment / "todo" / "amendments" / "A0001" / "withdrawal.md"
    assert withdrawal.is_file()
    assert "cannot be satisfied in this layer" in withdrawal.read_text(encoding="utf-8")
    assert manager.amendment_status("A0001")["status"] == "ABANDONED"

    # The lane is free again, and the closed ID stays consumed: A0001 still owns
    # its branch and worktree path, so the re-issued change must take a new one.
    reopened = manager.prepare_amendment(
        task_ids=["T001"],
        layer="CONTRACT",
        summary="the change, re-issued after the withdrawal",
        owner_direction="An Owner direction this layer can satisfy.",
    )
    assert reopened["amendment_id"] == "A0002"


def test_withdraw_requires_a_reason_and_refuses_a_closed_amendment(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    manager.prepare_amendment(
        task_ids=["T001"],
        layer="CONTRACT",
        summary="a change that will not land",
        owner_direction="An Owner direction this layer cannot satisfy.",
    )

    with pytest.raises(WorkflowError):
        manager.withdraw_amendment("A0001", reason="   ")

    manager.withdraw_amendment("A0001", reason="superseded by a re-issued change")
    with pytest.raises(WorkflowError):
        manager.withdraw_amendment("A0001", reason="withdrawn twice")


def test_low_risk_maintenance_round_trip_does_not_add_product_task(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _finish_seed_task_for_maintenance(repo, manager)
    task_ids_before = set(manager.load_config()["tasks"])

    prepared = manager.prepare_maintenance(
        summary="repair local value fixture",
        reason="the fixture contains the wrong deterministic value",
        allowed_paths=["src/value.txt"],
        verification_commands=["test $(cat src/value.txt) = repaired"],
        related_task="T001",
    )
    assert prepared["maintenance_id"] == "M0001"
    assert prepared["agent"] == "stage-developer"
    development = Path(str(prepared["development_worktree"]))
    (development / "src" / "value.txt").write_text("repaired\n", encoding="utf-8")
    continued = manager.continue_maintenance_develop("M0001", max_turns_exhausted=True)
    assert continued["attempt"] == prepared["attempt"]
    assert continued["continuation_count"] == 1
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "M0001",
            "outcome": "CANDIDATE_READY",
            "summary": "repaired the fixture",
            "commands": [{"command": "fixture check", "result": "passed"}],
            "residual_risks": [],
        },
    )
    candidate = manager.finish_maintenance_develop("M0001")
    assert candidate.status == "AWAITING_REVIEW"
    assert candidate.candidate_commit is not None

    review = manager.prepare_maintenance_review("M0001")
    review_worktree = Path(str(review["review_worktree"]))
    _write_json(
        review_worktree / ".workflow" / "review-result.json",
        {
            "task_id": "M0001",
            "base_commit": candidate.base_commit,
            "candidate_commit": candidate.candidate_commit,
            "verdict": "PASS",
            "checks": [
                {
                    "id": "bounded-repair",
                    "status": "PASS",
                    "evidence": ["only src/value.txt changed"],
                    "finding": "request satisfied",
                }
            ],
            "must_not_violations": [],
            "unknowns": [],
            "required_changes": [],
            "residual_risks": [],
        },
    )
    state, report = manager.finish_maintenance_review("M0001")

    assert state == "APPROVED"
    assert report.is_file()
    assert (repo / "src" / "value.txt").read_text(encoding="utf-8") == "repaired\n"
    assert set(manager.load_config()["tasks"]) == task_ids_before
    assert manager.maintenance_status("M0001") == {
        "maintenance_id": "M0001",
        "status": "APPROVED",
    }


@pytest.mark.parametrize(
    "path",
    [
        "todo/config.yaml",
        "todo/phases/P00/T001.md",
        "tools/workflow/core.py",
        "requirements.lock.txt",
        "src/robinhood_lp/risk/gate.py",
    ],
)
def test_maintenance_rejects_protected_or_high_risk_paths(tmp_path: Path, path: str) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _finish_seed_task_for_maintenance(repo, manager)

    with pytest.raises(WorkflowError, match="high-risk or protected"):
        manager.prepare_maintenance(
            summary="not actually low risk",
            reason="attempts to cross the maintenance boundary",
            allowed_paths=[path],
            verification_commands=["true"],
        )


def test_maintenance_cannot_start_while_product_task_is_active(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")

    with pytest.raises(WorkflowError, match="T001 is unfinished"):
        manager.prepare_maintenance(
            summary="repair local value fixture",
            reason="the fixture contains the wrong deterministic value",
            allowed_paths=["src/value.txt"],
            verification_commands=["true"],
        )


def test_ignored_bytecode_does_not_change_protected_snapshot(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")
    development = Path(str(prepared["development_worktree"]))
    (development / "src" / "value.txt").write_text("good\n", encoding="utf-8")
    cache = development / "tools" / "workflow" / "__pycache__" / "core.cpython-312.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"local bytecode")
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "T001",
            "outcome": "CANDIDATE_READY",
            "summary": "done",
            "commands": [],
            "residual_risks": [],
        },
    )

    attempt = manager.finish_develop("T001")

    assert attempt.candidate_commit is not None


def test_rejected_protected_change_preserves_developer_handoff(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")
    development = Path(str(prepared["development_worktree"]))
    (development / "todo" / "phases" / "P00" / "T001.md").write_text("changed\n", encoding="utf-8")
    result_path = development / ".workflow" / "developer-result.json"
    _write_json(
        result_path,
        {
            "task_id": "T001",
            "outcome": "CANDIDATE_READY",
            "summary": "done",
            "commands": [],
            "residual_risks": [],
        },
    )

    with pytest.raises(WorkflowError, match="developer modified a protected"):
        manager.finish_develop("T001")

    assert result_path.is_file()


def test_fail_verdict_with_unknowns_requests_changes(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")
    development = Path(str(prepared["development_worktree"]))
    (development / "src" / "value.txt").write_text("good\n", encoding="utf-8")
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "T001",
            "outcome": "CANDIDATE_READY",
            "summary": "done",
            "commands": [],
            "residual_risks": [],
        },
    )
    attempt = manager.finish_develop("T001")
    review = manager.prepare_review("T001")
    review_worktree = Path(str(review["review_worktree"]))
    _write_json(
        review_worktree / ".workflow" / "review-result.json",
        {
            "task_id": "T001",
            "base_commit": attempt.base_commit,
            "candidate_commit": attempt.candidate_commit,
            "verdict": "FAIL",
            "checks": [
                {
                    "id": "A",
                    "status": "FAIL",
                    "evidence": ["acceptance failed"],
                    "finding": "repair required",
                },
                {
                    "id": "B",
                    "status": "UNKNOWN",
                    "evidence": [],
                    "finding": "secondary evidence unavailable",
                },
            ],
            "must_not_violations": [],
            "unknowns": ["secondary evidence unavailable"],
            "required_changes": ["repair acceptance A"],
        },
    )

    state, _ = manager.finish_review("T001")

    assert state == "CHANGES_REQUESTED"


def test_a_task_retry_prompt_carries_the_review_it_repairs(tmp_path: Path) -> None:
    """The review record is the only durable statement of what the previous
    candidate got wrong. A retry prompt that omits it asks the Developer to
    guess at defects the Reviewer already named -- which the amendment and
    maintenance retry routes do not do."""
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    first = manager.prepare_develop("T001")
    development = Path(str(first["development_worktree"]))
    assert "review" not in first["prompt"]
    (development / "src" / "value.txt").write_text("good\n", encoding="utf-8")
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "T001",
            "outcome": "CANDIDATE_READY",
            "summary": "done",
            "commands": [],
            "residual_risks": [],
        },
    )
    attempt = manager.finish_develop("T001")
    review = manager.prepare_review("T001")
    _write_json(
        Path(str(review["review_worktree"])) / ".workflow" / "review-result.json",
        {
            "task_id": "T001",
            "base_commit": attempt.base_commit,
            "candidate_commit": attempt.candidate_commit,
            "verdict": "FAIL",
            "checks": [
                {
                    "id": "A",
                    "status": "FAIL",
                    "evidence": ["acceptance failed"],
                    "finding": "repair required",
                }
            ],
            "must_not_violations": [],
            "unknowns": [],
            "required_changes": ["repair acceptance A"],
        },
    )
    state, _ = manager.finish_review("T001")
    assert state == "CHANGES_REQUESTED"

    retry = manager.prepare_develop("T001", retry=True)

    assert retry["attempt"] == 2
    review_path = development / "todo" / "reviews" / "P00" / "T001" / "review-001.json"
    assert review_path.is_file()
    assert str(review_path) in retry["prompt"]
    assert "required_change" in retry["prompt"]


def test_config_validation_rejects_dependency_cycle(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    config["tasks"]["T000"]["depends_on"] = ["T001"]
    with pytest.raises(WorkflowError, match="dependency cycle"):
        manager.validate_config(config)


def test_protected_path_change_is_rejected(tmp_path: Path) -> None:
    repo, base = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    (repo / "todo" / "phases" / "P00" / "T001.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(WorkflowError, match="protected paths changed"):
        manager.check_changed_paths(base)


def test_short_or_symbolic_base_is_rejected(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    with pytest.raises(WorkflowError, match="full lowercase Git SHA"):
        manager.check_changed_paths("HEAD")


def test_illegal_state_transition_is_rejected(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    with pytest.raises(WorkflowError, match="READY -> APPROVED"):
        manager._set_state(config, "T001", "APPROVED")


def test_non_m3_agent_runtime_is_rejected(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    config["agent_runtime"]["model"] = "MiniMax-M2.7"
    with pytest.raises(WorkflowError, match="MiniMax-M3"):
        manager.validate_config(config)


def test_config_rejects_more_than_one_claimed_task(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    # T001 is the active task and holds the lane; claiming T000 too must fail.
    _set_task_state(config, "T000", "READY")
    with pytest.raises(WorkflowError, match="exactly the active task"):
        manager.validate_config(config)


def test_config_rejects_a_status_that_contradicts_its_decomposition(
    tmp_path: Path,
) -> None:
    """The composite status and the facts it is built from are one fact.

    Storing it twice is only safe because a disagreement is refused: otherwise a
    hand edit could move the status without moving the lane, and the two would
    mean different things to different readers.
    """
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    config["tasks"]["T001"]["status"] = "PLANNED"  # lifecycle/claimed still say READY
    with pytest.raises(WorkflowError, match="decomposes PLANNED as .* but lifecycle/claimed"):
        manager.validate_config(config)


def test_every_nonterminal_state_has_a_route_to_a_terminal_state() -> None:
    """No state is a trap, and every state has an Owner exit.

    The earlier form of this test asserted that every state could reach
    ``APPROVED``. That is the wrong invariant: it denies that a work item may
    legitimately end without being delivered, so a task that got stuck had no
    expressible ending at all. What the workflow owes an operator is that no
    state is a trap -- every non-terminal state can still reach *some* terminal
    state, and the Owner can always close the item from where it sits.
    """

    terminal = {"APPROVED", "ABANDONED"}

    def can_reach_terminal(start: str) -> bool:
        pending = [start]
        visited: set[str] = set()
        while pending:
            state = pending.pop()
            if state in terminal:
                return True
            if state in visited:
                continue
            visited.add(state)
            pending.extend(ALLOWED_TRANSITIONS[state])
        return False

    assert set(ALLOWED_TRANSITIONS) == STATES
    for state in STATES - terminal:
        assert can_reach_terminal(state), f"{state} cannot reach any terminal state"
        assert "ABANDONED" in ALLOWED_TRANSITIONS[state], (
            f"{state} has no Owner exit: a work item stuck here could not be closed"
        )
    # Approved work is retired by the annotation-only SUPERSEDE route, never by
    # rewriting its status, so it must remain un-abandonable.
    assert ALLOWED_TRANSITIONS["APPROVED"] == set()
    assert ALLOWED_TRANSITIONS["ABANDONED"] == set()


def test_ready_activates_one_dependency_complete_planned_task(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    _set_task_state(config, "T001", "PLANNED")
    config["active_task"] = "T000"
    config["workflow_state"] = "APPROVED"
    _write_json(repo / "todo" / "config.yaml", config)
    _git(repo, "add", "todo/config.yaml")
    _git(repo, "commit", "-m", "finish previous task")

    commit = manager.ready("T001")

    assert len(commit) == 40
    assert manager.load_config()["workflow_state"] == "READY"
    assert _git(repo, "status", "--porcelain") == ""


def test_reviewer_untracked_change_invalidates_review(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")
    development = Path(str(prepared["development_worktree"]))
    (development / "src" / "value.txt").write_text("good\n", encoding="utf-8")
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "T001",
            "outcome": "CANDIDATE_READY",
            "summary": "done",
            "commands": [],
            "residual_risks": [],
        },
    )
    manager.finish_develop("T001")
    review = manager.prepare_review("T001")
    review_worktree = Path(str(review["review_worktree"]))
    (review_worktree / "reviewer-note.txt").write_text("not allowed\n", encoding="utf-8")
    attempt = manager.load_attempt("T001")
    assert attempt is not None
    _write_json(
        review_worktree / ".workflow" / "review-result.json",
        {
            "task_id": "T001",
            "base_commit": attempt.base_commit,
            "candidate_commit": attempt.candidate_commit,
            "verdict": "PASS",
            "checks": [{"id": "A", "status": "PASS", "evidence": [], "finding": "ok"}],
            "must_not_violations": [],
            "unknowns": [],
            "required_changes": [],
        },
    )
    with pytest.raises(WorkflowError, match="outside its result handoff"):
        manager.finish_review("T001")


def test_visible_owner_planning_route(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")
    development = Path(str(prepared["development_worktree"]))
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "T001",
            "outcome": "TRIAGE_REQUIRED",
            "summary": "owner behavior is missing",
            "commands": [],
            "residual_risks": [],
            "triage_request": {
                "observed_problem": "intent choice is absent",
                "evidence": ["task contract"],
                "proposed_classification": "OWNER_DECISION_REQUIRED",
                "requested_change": "record the choice",
            },
        },
    )
    manager.finish_develop("T001")

    triage = manager.prepare_triage("T001")
    triage_worktree = Path(str(triage["triage_worktree"]))
    _write_json(
        triage_worktree / ".workflow" / "triage-result.json",
        {
            "task_id": "T001",
            "issue_commit": triage["issue_commit"],
            "classification": "OWNER_DECISION_REQUIRED",
            "summary": "owner must choose",
            "evidence": ["intent does not decide"],
            "recommended_action": "ask owner",
            "owner_question": "Which behavior is intended?",
        },
    )
    state, _ = manager.finish_triage("T001")
    assert state == "OWNER_DECISION_REQUIRED"
    with pytest.raises(WorkflowError, match="owner's explicit decision"):
        manager.prepare_plan("T001")

    planning = manager.prepare_plan("T001", owner_decision="Keep one active pool")
    assert planning["agent"] == "planner"
    (development / "docs" / "intent").mkdir(parents=True, exist_ok=True)
    (development / "docs" / "intent" / "decision.md").write_text(
        "one active pool\n", encoding="utf-8"
    )
    _write_json(
        development / ".workflow" / "planner-result.json",
        {
            "task_id": "T001",
            "outcome": "PLAN_READY",
            "summary": "owner decision recorded",
            "rationale": "matches explicit decision",
            "unresolved_questions": [],
        },
    )
    plan = manager.finish_plan("T001")
    assert plan is not None
    review = manager.prepare_plan_review("T001")
    review_worktree = Path(str(review["review_worktree"]))
    _write_json(
        review_worktree / ".workflow" / "plan-review-result.json",
        {
            "task_id": "T001",
            "base_commit": plan.base_commit,
            "candidate_commit": plan.candidate_commit,
            "verdict": "PASS",
            "summary": "planning is aligned",
            "required_changes": [],
            "unknowns": [],
        },
    )
    state, _ = manager.finish_plan_review("T001")
    assert state == "CHANGES_REQUESTED"


def _drive_to_owner_decision(tmp_path: Path) -> tuple[WorkflowManager, Path]:
    """Reach OWNER_DECISION_REQUIRED and hand the task to the Planner."""
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")
    development = Path(str(prepared["development_worktree"]))
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "T001",
            "outcome": "TRIAGE_REQUIRED",
            "summary": "owner behavior is missing",
            "commands": [],
            "residual_risks": [],
            "triage_request": {
                "observed_problem": "intent choice is absent",
                "evidence": ["task contract"],
                "proposed_classification": "OWNER_DECISION_REQUIRED",
                "requested_change": "record the choice",
            },
        },
    )
    manager.finish_develop("T001")
    triage = manager.prepare_triage("T001")
    _write_json(
        Path(str(triage["triage_worktree"])) / ".workflow" / "triage-result.json",
        {
            "task_id": "T001",
            "issue_commit": triage["issue_commit"],
            "classification": "OWNER_DECISION_REQUIRED",
            "summary": "owner must choose",
            "evidence": ["intent does not decide"],
            "recommended_action": "ask owner",
            "owner_question": "Which behavior is intended?",
        },
    )
    manager.finish_triage("T001")
    return manager, development


def test_planning_route_transcribes_the_owner_decision_into_intent(tmp_path: Path) -> None:
    """The triaged Planner may reach Intent, but only to transcribe the answer the
    Owner supplied through --owner-decision; that requirement, not a path ban, is
    what keeps transcription distinct from authoring a goal."""
    manager, development = _drive_to_owner_decision(tmp_path)
    manager.prepare_plan("T001", owner_decision="Keep one active pool")
    (development / "docs" / "intent").mkdir(parents=True, exist_ok=True)
    (development / "docs" / "intent" / "decision.md").write_text(
        "one active pool\n", encoding="utf-8"
    )
    _write_json(
        development / ".workflow" / "planner-result.json",
        {
            "task_id": "T001",
            "outcome": "PLAN_READY",
            "summary": "owner decision recorded",
            "rationale": "matches explicit decision",
            "unresolved_questions": [],
        },
    )
    plan = manager.finish_plan("T001")
    assert plan is not None


def test_a_prophet_change_targets_no_task_and_names_the_prophet_agent(tmp_path: Path) -> None:
    """A PROPHET change restructures the plan and may create tasks that do not
    exist yet, so it is the one layer that takes no --task."""
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    with pytest.raises(WorkflowError, match="requires at least one target task"):
        manager.prepare_amendment(
            task_ids=[],
            layer="CONTRACT",
            summary="no target",
            owner_direction="Not a valid CONTRACT amendment.",
        )
    prepared = manager.prepare_amendment(
        task_ids=[],
        layer="PROPHET",
        summary="state a goal and add the task that delivers it",
        owner_direction="Record the goal and the new task.",
    )
    assert prepared["layer"] == "PROPHET"
    assert prepared["agent"] == "prophet"
    assert prepared["task_ids"] == []


def test_prophet_retry_is_authored_by_the_prophet(tmp_path: Path) -> None:
    """A retry must be authored by the role that authored the first attempt. The
    planner is not scoped to the documents a PROPHET change writes, so handing it
    the repair produced a candidate the path rules then reject: a retry route that
    could never succeed."""
    manager, worktree = _prepare_prophet_change(tmp_path)
    target = worktree / "todo" / "README.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nA note.\n", encoding="utf-8")
    _seal_prophet_result(worktree)
    candidate = manager.finish_amendment("A0001")
    review = manager.prepare_amendment_review("A0001")
    _write_json(
        Path(str(review["review_worktree"])) / ".workflow" / "amendment-review-result.json",
        {
            "amendment_id": "A0001",
            "base_commit": candidate.base_commit,
            "candidate_commit": candidate.candidate_commit,
            "verdict": "FAIL",
            "summary": "the note is in the wrong section",
            "required_changes": ["move the note"],
            "unknowns": [],
        },
    )
    state, _ = manager.finish_amendment_review("A0001")
    retry = manager.prepare_amendment_retry("A0001")
    assert state == "CHANGES_REQUESTED"
    assert retry["agent"] == "prophet"
    assert retry["attempt"] == 2
    assert retry["worktree"] == str(worktree)


def test_prophet_retry_may_repair_a_contract_the_same_amendment_added(tmp_path: Path) -> None:
    """A repair must be able to fix the amendment's own output.

    A retry re-bases the attempt on the previous candidate, so without the
    amendment's original base the contract it had just added would look like a
    pre-existing file the layer may never touch and the only repair route would
    be to abandon the change. The safety property is unchanged: a contract that
    existed at the original base is still refused in the same retry.
    """
    manager, worktree = _prepare_prophet_change(tmp_path)
    (worktree / "todo" / "phases" / "P00" / "T002.md").write_text(_NEW_CONTRACT, encoding="utf-8")
    _add_task_to_config(worktree)
    _seal_prophet_result(worktree)
    candidate = manager.finish_amendment("A0001")
    review = manager.prepare_amendment_review("A0001")
    _write_json(
        Path(str(review["review_worktree"])) / ".workflow" / "amendment-review-result.json",
        {
            "amendment_id": "A0001",
            "base_commit": candidate.base_commit,
            "candidate_commit": candidate.candidate_commit,
            "verdict": "FAIL",
            "summary": "the new contract states the wrong dependency",
            "required_changes": ["correct the new contract"],
            "unknowns": [],
        },
    )
    manager.finish_amendment_review("A0001")
    manager.prepare_amendment_retry("A0001")

    added = worktree / "todo" / "phases" / "P00" / "T002.md"
    added.write_text(added.read_text(encoding="utf-8") + "\nCorrected.\n", encoding="utf-8")
    pre_existing = worktree / "todo" / "phases" / "P00" / "T000.md"
    pre_existing.write_text(
        pre_existing.read_text(encoding="utf-8") + "\nAnd also do this.\n", encoding="utf-8"
    )
    _seal_prophet_result(worktree)
    with pytest.raises(WorkflowError, match="outside its scope"):
        manager.finish_amendment("A0001")

    pre_existing.write_text(
        pre_existing.read_text(encoding="utf-8").replace("\nAnd also do this.\n", ""),
        encoding="utf-8",
    )
    repaired = manager.finish_amendment("A0001")
    assert repaired.status == "AWAITING_REVIEW"
    assert repaired.attempt == 2
    assert "Corrected." in added.read_text(encoding="utf-8")


def test_ready_refuses_a_task_an_amendment_recorded_as_conflicting(tmp_path: Path) -> None:
    """A PROPHET change cannot repair an existing contract, so it records the
    conflict instead of leaving it to be noticed by hand. The affected task must
    not be activated while that record is open, and a later amendment closes it.
    """
    manager, worktree = _prepare_prophet_change(tmp_path)
    (worktree / "todo" / "phases" / "P00" / "T002.md").write_text(_NEW_CONTRACT, encoding="utf-8")
    _add_task_to_config(worktree)
    _seal_prophet_result(
        worktree,
        affected=[
            {
                "impact_id": "A0001:T001:goal-conflict",
                "task_id": "T001",
                "reason": "the added goal contradicts T001's scope clause",
                "categories": ["CONTRACT", "IMPLEMENTATION"],
                "required_disposition": "amend T001 and verify the old path is unreachable",
            }
        ],
    )
    candidate = manager.finish_amendment("A0001")
    assert _pass_amendment_review(manager, "A0001", candidate) == "APPROVED"
    assert (repo_amendments := manager.repo / "todo" / "amendments" / "A0001").is_dir()
    assert "T001" in (repo_amendments / "impacts.json").read_text(encoding="utf-8")

    with pytest.raises(WorkflowError, match="no longer matches a governing document"):
        manager.ready("T001")

    resolution = manager.prepare_amendment(
        task_ids=["T001"],
        layer="CONTRACT",
        summary="bring T001 back in line with the amended goal",
        owner_direction="Correct the scope clause.",
    )
    resolution_worktree = Path(str(resolution["worktree"]))
    contract = resolution_worktree / "todo" / "phases" / "P00" / "T001.md"
    contract.write_text(contract.read_text(encoding="utf-8") + "\nAligned.\n", encoding="utf-8")
    _write_json(
        resolution_worktree / ".workflow" / "amendment-result.json",
        {
            "amendment_id": "A0002",
            "outcome": "AMENDMENT_READY",
            "summary": "aligned the contract with the amended goal",
            "rationale": "implements the recorded Owner direction",
            "unresolved_questions": [],
            "impact_assessment": _impact_assessment(),
            "affected_existing_tasks": [],
            "resolved_task_impacts": ["A0001:T001:goal-conflict"],
        },
    )
    repaired = manager.finish_amendment("A0002")
    assert _pass_amendment_review(manager, "A0002", repaired) == "APPROVED"

    assert manager.ready("T001")
    # Nothing writes the composite any more, so the state is asserted where it
    # now lives: the decomposition, and the projection built from it.
    record = manager.load_config()["tasks"]["T001"]
    assert (record["lifecycle"], record["claimed"]) == LIFECYCLE_OF["READY"]
    assert "status" not in record
    assert manager.status()["task_status"] == "READY"


def test_ready_refuses_a_task_when_an_approved_dependency_has_an_open_impact(
    tmp_path: Path,
) -> None:
    manager, worktree = _prepare_prophet_change(tmp_path)
    (worktree / "todo" / "phases" / "P00" / "T002.md").write_text(_NEW_CONTRACT, encoding="utf-8")
    _add_task_to_config(worktree)
    _seal_prophet_result(
        worktree,
        affected=[
            {
                "impact_id": "A0001:T000:authority-change",
                "task_id": "T000",
                "reason": "the approved dependency implements the superseded authority",
                "categories": ["IMPLEMENTATION", "DEPENDENCY"],
                "required_disposition": "create and approve a successor before consumers run",
            }
        ],
    )
    candidate = manager.finish_amendment("A0001")
    assert _pass_amendment_review(manager, "A0001", candidate) == "APPROVED"

    with pytest.raises(WorkflowError, match="T000: A0001:T000:authority-change"):
        manager.ready("T001")


def test_impact_resolution_closes_only_the_named_conflict(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _write_json(
        repo / "todo" / "amendments" / "A0001" / "impacts.json",
        {
            "amendment_id": "A0001",
            "layer": "PROPHET",
            "raised": [
                {
                    "impact_id": "A0001:T001:first-conflict",
                    "task_id": "T001",
                    "reason": "first",
                },
                {
                    "impact_id": "A0001:T001:second-conflict",
                    "task_id": "T001",
                    "reason": "second",
                },
            ],
            "resolves": [],
        },
    )
    _write_json(
        repo / "todo" / "amendments" / "A0002" / "impacts.json",
        {
            "amendment_id": "A0002",
            "layer": "CONTRACT",
            "raised": [],
            "resolves": ["A0001:T001:first-conflict"],
        },
    )

    findings = manager._unresolved_task_impacts("T001")
    assert not any("first-conflict" in finding for finding in findings)
    assert any("second-conflict" in finding for finding in findings)


def test_legacy_impact_gets_a_stable_synthetic_id(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _write_json(
        repo / "todo" / "amendments" / "A0001" / "impacts.json",
        {
            "amendment_id": "A0001",
            "layer": "PROPHET",
            "raised": [{"task_id": "T001", "reason": "legacy conflict"}],
            "resolves": [],
        },
    )
    _write_json(
        repo / "todo" / "amendments" / "A0002" / "impacts.json",
        {
            "amendment_id": "A0002",
            "layer": "CONTRACT",
            "raised": [],
            "resolves": ["T001"],
        },
    )

    assert manager._unresolved_task_impacts("T001") == [
        "A0001:T001:legacy raised by A0001: legacy conflict"
    ]


def test_a_contract_amendment_retry_is_still_a_planner(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    prepared = manager.prepare_amendment(
        task_ids=["T001"],
        layer="CONTRACT",
        summary="clarify future acceptance",
        owner_direction="Require an explicit deterministic assertion.",
    )
    amendment = Path(str(prepared["worktree"]))
    contract = amendment / "todo" / "phases" / "P00" / "T001.md"
    contract.write_text(contract.read_text(encoding="utf-8") + "\nExtra.\n", encoding="utf-8")
    _seal_prophet_result(amendment)
    candidate = manager.finish_amendment("A0001")
    review = manager.prepare_amendment_review("A0001")
    _write_json(
        Path(str(review["review_worktree"])) / ".workflow" / "amendment-review-result.json",
        {
            "amendment_id": "A0001",
            "base_commit": candidate.base_commit,
            "candidate_commit": candidate.candidate_commit,
            "verdict": "FAIL",
            "summary": "wrong clause",
            "required_changes": ["fix the clause"],
            "unknowns": [],
        },
    )
    manager.finish_amendment_review("A0001")
    assert manager.prepare_amendment_retry("A0001")["agent"] == "planner"


def test_prophet_change_refuses_a_task_it_would_only_ignore(tmp_path: Path) -> None:
    """--task is a restriction, not an instruction, and it says nothing about a
    PROPHET change. Accepting one silently would let a caller believe it had
    constrained a change it had not -- including when the name is a task that
    does not exist."""
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    for name in ("T001", "T099"):
        with pytest.raises(WorkflowError, match="takes no --task"):
            manager.prepare_amendment(
                task_ids=[name],
                layer="PROPHET",
                summary="names a task it would ignore",
                owner_direction="Not a valid PROPHET change.",
            )


_NEW_CONTRACT = (
    "# T002 — New task\n\n## Dependencies\n\nT000\n\n## Outcome\n\nTest outcome.\n\n"
    "## Deliverables\n\nTest file.\n\n## Acceptance\n\nValue is good.\n\n"
    "## Must not\n\nDo not edit contracts.\n\n## References\n\nNone.\n"
)


def _prepare_prophet_change(tmp_path: Path) -> tuple[WorkflowManager, Path]:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    prepared = manager.prepare_amendment(
        task_ids=[],
        layer="PROPHET",
        summary="add a task and state its goal",
        owner_direction="Record the goal and add the task that delivers it.",
    )
    return manager, Path(str(prepared["worktree"]))


def _seal_prophet_result(
    worktree: Path,
    *,
    affected: list[dict[str, object]] | None = None,
    resolves: list[str] | None = None,
) -> None:
    _write_json(
        worktree / ".workflow" / "amendment-result.json",
        {
            "amendment_id": "A0001",
            "outcome": "AMENDMENT_READY",
            "summary": "applied the recorded direction",
            "rationale": "matches the recorded direction",
            "unresolved_questions": [],
            "impact_assessment": _impact_assessment(),
            "affected_existing_tasks": affected or [],
            "resolved_task_impacts": resolves or [],
        },
    )


def _pass_amendment_review(manager: WorkflowManager, amendment_id: str, candidate: object) -> str:
    """Drive one amendment review to PASS and return the recorded verdict."""
    review = manager.prepare_amendment_review(amendment_id)
    _write_json(
        Path(str(review["review_worktree"])) / ".workflow" / "amendment-review-result.json",
        {
            "amendment_id": amendment_id,
            "base_commit": candidate.base_commit,
            "candidate_commit": candidate.candidate_commit,
            "verdict": "PASS",
            "summary": "direction and scope are satisfied",
            "required_changes": [],
            "unknowns": [],
        },
    )
    state, _ = manager.finish_amendment_review(amendment_id)
    return state


def _add_task_to_config(worktree: Path, task_id: str = "T002") -> None:
    config_path = worktree / "todo" / "config.yaml"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["tasks"][task_id] = {
        "phase": "P00",
        "depends_on": ["T000"],
        "task_file": f"todo/phases/P00/{task_id}.md",
        "attempt": 0,
        "base_commit": None,
        "candidate_commit": None,
        "approved_commit": None,
        "latest_review": None,
        **_decompose("PLANNED"),
    }
    _write_json(config_path, config)


def test_prophet_change_creates_a_task_and_passes_review(tmp_path: Path) -> None:
    manager, worktree = _prepare_prophet_change(tmp_path)
    (worktree / "todo" / "phases" / "P00" / "T002.md").write_text(_NEW_CONTRACT, encoding="utf-8")
    _add_task_to_config(worktree)
    _seal_prophet_result(worktree)
    candidate = manager.finish_amendment("A0001")
    assert candidate.status == "AWAITING_REVIEW"
    review = manager.prepare_amendment_review("A0001")
    assert review["agent"] == "prophet-reviewer"
    review_worktree = Path(str(review["review_worktree"]))
    _write_json(
        review_worktree / ".workflow" / "amendment-review-result.json",
        {
            "amendment_id": "A0001",
            "base_commit": candidate.base_commit,
            "candidate_commit": candidate.candidate_commit,
            "verdict": "PASS",
            "summary": "new task added, nothing existing touched",
            "required_changes": [],
            "unknowns": [],
        },
    )
    state, _ = manager.finish_amendment_review("A0001")
    assert state == "APPROVED"
    added = manager.load_config()["tasks"]["T002"]
    assert (added["lifecycle"], added["claimed"]) == LIFECYCLE_OF["PLANNED"]
    assert added["attempt"] == 0


def test_prophet_change_may_not_modify_an_existing_contract(tmp_path: Path) -> None:
    """The check this whole layer exists for: an APPROVED contract records what was
    reviewed, so a new obligation belongs to a new task, not to an edit of it."""
    manager, worktree = _prepare_prophet_change(tmp_path)
    target = worktree / "todo" / "phases" / "P00" / "T000.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\nAnd also do this.\n", encoding="utf-8"
    )
    _seal_prophet_result(worktree)
    with pytest.raises(WorkflowError, match="outside its scope"):
        manager.finish_amendment("A0001")


def test_prophet_change_may_not_touch_implementation_or_the_workflow(tmp_path: Path) -> None:
    for relative in ("src/robinhood_lp/new_module.py", "tools/workflow/core.py", ".claude/x.md"):
        case = tmp_path / relative.replace("/", "_")
        case.mkdir()
        manager, worktree = _prepare_prophet_change(case)
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed\n", encoding="utf-8")
        _seal_prophet_result(worktree)
        with pytest.raises(WorkflowError, match="outside its scope"):
            manager.finish_amendment("A0001")


def test_prophet_change_may_not_alter_an_existing_task(tmp_path: Path) -> None:
    manager, worktree = _prepare_prophet_change(tmp_path)
    config_path = worktree / "todo" / "config.yaml"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["tasks"]["T001"]["attempt"] = 5
    _write_json(config_path, config)
    _seal_prophet_result(worktree)
    with pytest.raises(WorkflowError, match="a task that already exists"):
        manager.finish_amendment("A0001")


# --------------------------------------------------------------------------
# Abandonment: the Owner's exit from work whose lane cannot be moved
# --------------------------------------------------------------------------


def _add_planned_dependent(repo: Path, manager: WorkflowManager) -> None:
    """Add T002 (PLANNED) depending on T001, so T001 has an open consumer."""
    contract = repo / "todo" / "phases" / "P00" / "T002.md"
    contract.write_text(
        "# T002\n\n## Dependencies\n\nT001\n\n## Outcome\n\nTest outcome.\n\n"
        "## Deliverables\n\nTest file.\n\n## Acceptance\n\nValue is good.\n\n"
        "## Must not\n\nDo not edit contracts.\n\n## References\n\nNone.\n\n"
        "## Replacement and migration\n\nNone.\n",
        encoding="utf-8",
    )
    config = manager.load_config()
    config["tasks"]["T002"] = {
        "phase": "P00",
        "depends_on": ["T001"],
        "task_file": "todo/phases/P00/T002.md",
        "attempt": 0,
        "base_commit": None,
        "candidate_commit": None,
        "approved_commit": None,
        "latest_review": None,
        **_decompose("PLANNED"),
    }
    _write_json(repo / "todo" / "config.yaml", config)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add planned dependent")


def test_abandon_task_closes_an_attempt_no_command_can_advance(tmp_path: Path) -> None:
    """A Developer that dies without a handoff used to trap the task forever.

    ``finish-develop`` needs the handoff file, and ``continue-develop`` needs
    either that handoff or ``--max-turns-exhausted``, which is authorized only
    when Claude Code actually stopped the agent at its hard turn limit. Every
    other command requires a different state, so both the work item and the
    single-active-work lane were stuck with no route out.
    """
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")
    worktree = Path(str(prepared["development_worktree"]))

    # The visible Developer was stopped by something that is not its turn limit.
    with pytest.raises(WorkflowError, match="continuation requires"):
        manager.continue_develop("T001")
    with pytest.raises(WorkflowError, match="developer result is missing"):
        manager.finish_develop("T001")

    result = manager.abandon_task("T001", reason="agent died before writing its handoff")

    assert result["status"] == "ABANDONED"
    assert manager.status()["task_status"] == "ABANDONED"
    assert manager.load_config()["tasks"]["T001"]["lifecycle"] == "ABANDONED"
    record = (repo / "todo" / "abandoned" / "T001.md").read_text(encoding="utf-8")
    assert "agent died before writing its handoff" in record
    # The record must state the live status, not the stale one on the last commit.
    assert "`IN_DEVELOPMENT`" in record
    # Nothing landed, and the attempt survives as the record of what was tried.
    assert worktree.is_dir()
    assert _git(repo, "branch", "--list", "workflow/t001-attempt-001")


def test_abandon_task_frees_the_single_active_work_lane(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    with pytest.raises(WorkflowError, match="unfinished"):
        manager.prepare_maintenance(
            summary="repair the fixture",
            reason="the fixture holds the wrong value",
            allowed_paths=["src/value.txt"],
            verification_commands=["true"],
        )

    manager.abandon_task("T001", reason="no longer wanted before it started")

    prepared = manager.prepare_maintenance(
        summary="repair the fixture",
        reason="the fixture holds the wrong value",
        allowed_paths=["src/value.txt"],
        verification_commands=["true"],
    )
    assert prepared["maintenance_id"] == "M0001"


def test_abandon_task_requires_a_reason_and_refuses_closed_work(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    with pytest.raises(WorkflowError, match="non-empty reason"):
        manager.abandon_task("T001", reason="   ")

    manager.abandon_task("T001", reason="first")
    with pytest.raises(WorkflowError, match="already closed"):
        manager.abandon_task("T001", reason="second")
    # Approved work is retired by the annotation-only SUPERSEDE route, never by
    # rewriting its status, so it must stay un-abandonable.
    with pytest.raises(WorkflowError, match="already closed"):
        manager.abandon_task("T000", reason="approved work is not abandonable")


def test_abandon_task_refuses_to_strand_an_open_dependent(tmp_path: Path) -> None:
    """The guard must redirect the Owner, not trap them.

    A dependent could never activate once its dependency is closed, because
    ``_check_dependencies`` requires every dependency to be ``APPROVED``. The
    refusal names it, and abandoning in reverse-dependency order then terminates
    -- dependencies form a DAG, so the route always exists.
    """
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _add_planned_dependent(repo, manager)

    with pytest.raises(WorkflowError, match="open tasks still depend on it: T002"):
        manager.abandon_task("T001", reason="depends on a consumer")

    manager.abandon_task("T002", reason="the consumer goes first")
    manager.abandon_task("T001", reason="now nothing depends on it")

    config = manager.load_config()
    assert config["tasks"]["T001"]["lifecycle"] == "ABANDONED"
    assert config["tasks"]["T002"]["lifecycle"] == "ABANDONED"


def test_abandon_maintenance_closes_an_escalated_repair(tmp_path: Path) -> None:
    """``ESCALATED`` had no exit at all.

    The maintenance Developer reports ``TRIAGE_REQUIRED`` when the repair does
    not fit the lane, and the documented route is then a new numbered task --
    outside the lane entirely. ``prepare-maintenance-retry`` accepts only
    ``CHANGES_REQUESTED`` or ``BLOCKED``, so the record could never be closed by
    anyone and the lane kept a dead entry forever.
    """
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _finish_seed_task_for_maintenance(repo, manager)
    prepared = manager.prepare_maintenance(
        summary="repair the fixture value",
        reason="the fixture holds the wrong deterministic value",
        allowed_paths=["src/value.txt"],
        verification_commands=["test $(cat src/value.txt) = repaired"],
        related_task="T001",
    )
    development = Path(str(prepared["development_worktree"]))
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "M0001",
            "outcome": "TRIAGE_REQUIRED",
            "summary": "the value is generated, so this is not a bounded repair",
            "commands": [],
            "residual_risks": [],
            "triage_request": {
                "observed_problem": "src/value.txt is generated from a manifest",
                "evidence": ["the fixture value is derived"],
                "proposed_classification": "CONTRACT_MISMATCH",
                "requested_change": "open a numbered task for the generator",
            },
        },
    )
    escalated = manager.finish_maintenance_develop("M0001")
    assert escalated.status == "ESCALATED"
    with pytest.raises(WorkflowError, match="maintenance retry requires"):
        manager.prepare_maintenance_retry("M0001")

    closed = manager.abandon_maintenance("M0001", reason="this needs a numbered task")

    assert closed["status"] == "ABANDONED"
    assert "this needs a numbered task" in (
        development / "todo" / "maintenance" / "M0001" / "withdrawal.md"
    ).read_text(encoding="utf-8")
    # The lane is free for the next change.
    again = manager.prepare_maintenance(
        summary="open the replacement work",
        reason="the generated value needs its own task",
        allowed_paths=["src/value.txt"],
        verification_commands=["true"],
        related_task="T001",
    )
    assert again["maintenance_id"] == "M0002"


def test_abandon_maintenance_requires_a_reason_and_refuses_closed_work(
    tmp_path: Path,
) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _finish_seed_task_for_maintenance(repo, manager)
    manager.prepare_maintenance(
        summary="repair the fixture",
        reason="the fixture holds the wrong value",
        allowed_paths=["src/value.txt"],
        verification_commands=["true"],
        related_task="T001",
    )
    with pytest.raises(WorkflowError, match="non-empty reason"):
        manager.abandon_maintenance("M0001", reason="  ")
    manager.abandon_maintenance("M0001", reason="not wanted")
    with pytest.raises(WorkflowError, match="already closed"):
        manager.abandon_maintenance("M0001", reason="again")
    with pytest.raises(WorkflowError, match="unknown maintenance repair"):
        manager.abandon_maintenance("M0009", reason="does not exist")


# --------------------------------------------------------------------------
# A lost attempt: a runtime record whose worktree no longer exists
# --------------------------------------------------------------------------


def _lose_the_worktree(manager: WorkflowManager, task_id: str) -> Path:
    """Remove a development worktree the way an incident would, out of band."""
    attempt = manager.load_attempt(task_id)
    assert attempt is not None
    worktree = Path(attempt.development_worktree)
    shutil.rmtree(worktree)
    _git(manager.repo, "worktree", "prune")
    return worktree


def test_discard_attempt_frees_a_task_whose_worktree_vanished(tmp_path: Path) -> None:
    """A record outliving its worktree used to block the task forever.

    ``prepare_develop`` refused with "runtime record already exists", and every
    other command requires a different state or a worktree that is no longer
    there, so the only escape was deleting a file under ``.git`` by hand. The
    task could not be developed, reviewed, triaged or planned, and because a
    started task holds the single-active-work lane by design, nothing else in
    the plan could move either.
    """
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    manager.prepare_develop("T001")
    worktree = _lose_the_worktree(manager, "T001")

    # The failure now names the situation and the exit instead of reporting a
    # record as if the attempt were still live.
    with pytest.raises(WorkflowError, match="no longer exists, so no command can use it"):
        manager.prepare_develop("T001")
    assert manager.status()["orphaned_attempts"] == [
        {
            "task_id": "T001",
            "phase": "P00",
            "attempt": 1,
            "base_commit": manager.load_attempt("T001").base_commit,  # type: ignore[union-attr]
            "candidate_commit": None,
            "branch": "workflow/t001-attempt-001",
            "development_worktree": str(worktree),
            "continuation_count": 0,
        }
    ]

    result = manager.discard_attempt("T001", reason="the worktree was removed by hand")

    assert result["status"] == "READY"
    assert result["discarded_attempt"] == 1
    # The consumed attempt number is never handed out again.
    assert result["next_attempt"] == 2
    record = (repo / "todo" / "evidence" / "P00" / "T001" / "attempt-001-lost.json").read_text(
        encoding="utf-8"
    )
    assert "the worktree was removed by hand" in record
    assert "workflow/t001-attempt-001" in record

    # The task can be worked on again, on the next attempt number.
    assert manager.status()["orphaned_attempts"] == []
    assert manager.prepare_develop("T001")["attempt"] == 2


def test_discard_attempt_refuses_a_live_attempt(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")

    with pytest.raises(WorkflowError, match="is still live at"):
        manager.discard_attempt("T001", reason="not actually lost")
    # The live attempt is untouched.
    assert manager.load_attempt("T001") is not None
    assert Path(str(prepared["development_worktree"])).is_dir()


def test_discard_attempt_requires_a_reason_and_a_record(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    with pytest.raises(WorkflowError, match="no recorded attempt to discard"):
        manager.discard_attempt("T001", reason="nothing to discard")

    manager.prepare_develop("T001")
    _lose_the_worktree(manager, "T001")
    with pytest.raises(WorkflowError, match="non-empty reason"):
        manager.discard_attempt("T001", reason="   ")
    # Still blocked: a refused discard must not half-apply.
    assert manager.load_attempt("T001") is not None


# --------------------------------------------------------------------------
# The recorded status must be supported by the committed artifacts
# --------------------------------------------------------------------------


def test_validate_refuses_a_status_no_artifact_supports(tmp_path: Path) -> None:
    """A status with nothing behind it did not come from a transition.

    Every transition commits its evidence in the same commit, so the two must
    agree. When they do not, the value was written by hand -- which is how six
    tasks were once added with no author role and no review -- and running the
    workflow from a state nothing can explain is exactly what must not happen.
    """
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    manager.validate_repository()

    config = manager.load_config()
    # Flip an approved task back to unstarted, decomposition and all, so the
    # config is internally consistent and only the artifacts disagree. The
    # composite is written too: this test is about a *recorded* status the
    # artifacts do not support, which is the shape every revision in Git has.
    _set_legacy_state(config, "T000", "PLANNED")
    _write_json(repo / "todo" / "config.yaml", config)

    # `status` reports it so an operator sees it while inspecting...
    assert manager.status()["status_conflicts"] == [
        {
            "task_id": "T000",
            "recorded": "PLANNED",
            "derived": "APPROVED",
            "reason": ("the stored facts add up to no status at all (artifacts say APPROVED)"),
        }
    ]
    # ...and `validate` refuses it outright.
    with pytest.raises(WorkflowError, match="T000=PLANNED but the stored facts add up"):
        manager.validate_repository()


def test_admitted_statuses_is_the_whole_projection() -> None:
    """The projection, exhaustively: one fact decides, one stays unknown.

    This is the table the whole decomposition rests on. `lifecycle` and the lane
    claim decide everywhere except a single genuinely undetermined case -- a
    claimed task that has produced nothing is either sealed for review or still
    being worked on, and *which* is a session fact no artifact records. An empty
    set means the facts contradict each other.
    """
    empty: frozenset[str] = frozenset()

    # Nothing selected: no artifact may claim otherwise.
    assert admitted_statuses(lifecycle="OPEN", claimed=False, derived="UNSTARTED") == {"PLANNED"}
    assert admitted_statuses(lifecycle="OPEN", claimed=False, derived="IN_FLIGHT") == empty
    assert admitted_statuses(lifecycle="OPEN", claimed=False, derived="AWAITING_REVIEW") == empty

    # Selected, nothing sealed: READY only -- a claimed task with no artifacts
    # cannot be IN_DEVELOPMENT until an attempt has actually been opened.
    assert admitted_statuses(lifecycle="OPEN", claimed=True, derived="UNSTARTED") == {"READY"}

    # Selected, an attempt opened, nothing produced: the one open question.
    assert admitted_statuses(lifecycle="OPEN", claimed=True, derived="IN_FLIGHT") == {
        "READY",
        "IN_DEVELOPMENT",
    }

    # Anything the artifacts decide is taken as decided.
    for derived in ("AWAITING_REVIEW", "CHANGES_REQUESTED", "PLANNING", "BLOCKED"):
        assert admitted_statuses(lifecycle="OPEN", claimed=True, derived=derived) == {derived}
        assert admitted_statuses(lifecycle="OPEN", claimed=False, derived=derived) == empty

    # The task's ending is admitted only when the artifact that proves it exists:
    # delivery is a reviewer's verdict on an exact candidate, and abandonment is
    # an Owner record. Neither may be declared by setting a field.
    assert admitted_statuses(lifecycle="DELIVERED", claimed=False, derived="APPROVED") == {
        "APPROVED"
    }
    assert admitted_statuses(lifecycle="ABANDONED", claimed=False, derived="ABANDONED") == {
        "ABANDONED"
    }
    for derived in ("UNSTARTED", "IN_FLIGHT", "AWAITING_REVIEW"):
        assert admitted_statuses(lifecycle="DELIVERED", claimed=False, derived=derived) == empty
        assert admitted_statuses(lifecycle="ABANDONED", claimed=False, derived=derived) == empty

    # A lifecycle nobody defines admits nothing at all.
    assert admitted_statuses(lifecycle="MADE_UP", claimed=False, derived="UNSTARTED") == empty


def test_validate_accepts_what_only_the_control_plane_can_decide(tmp_path: Path) -> None:
    """The same boundary, end to end through a real config."""
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    config_path = repo / "todo" / "config.yaml"

    def conflicts(status: str, attempt: int) -> list[dict[str, str]]:
        config = manager.load_config()
        _set_task_state(config, "T001", status)
        config["tasks"]["T001"]["attempt"] = attempt
        config["workflow_state"] = status
        _write_json(config_path, config)
        return manager.status_conflicts(config)

    # Nothing has run.
    assert conflicts("PLANNED", 0) == []
    assert conflicts("READY", 0) == []
    assert conflicts("AWAITING_REVIEW", 0) != []
    assert conflicts("APPROVED", 0) != []

    # An attempt is recorded with no artifact for it.
    assert conflicts("READY", 3) == []
    assert conflicts("IN_DEVELOPMENT", 3) == []
    assert conflicts("BLOCKED", 3) != []

    # A claimed task may not be recorded as unselected, and the reverse: where a
    # revision still carries the composite it and the facts behind it must
    # describe the same task.
    config = manager.load_config()
    _set_legacy_state(config, "T001", "READY")
    config["tasks"]["T001"]["claimed"] = False  # now lifecycle/claimed disagree
    with pytest.raises(WorkflowError, match="decomposes READY as"):
        manager.validate_config(config)


def test_discard_attempt_clears_a_remnant_on_a_closed_task(tmp_path: Path) -> None:
    """A completed task's remnant needs no route, so it must be clearable.

    Five of the six records on the development machine belong to approved
    amendments and one to an approved task. A closed task never develops again,
    so nothing is being unblocked -- but if the remnant could not be cleared it
    would sit in `status` for good, and the diagnostic would stop meaning
    anything. Clearing it must not touch the approval evidence: `attempt` on an
    APPROVED task is part of what the reviewer inspected.
    """

    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")
    development = Path(str(prepared["development_worktree"]))
    (development / "src" / "value.txt").write_text("good\n", encoding="utf-8")
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "T001",
            "outcome": "CANDIDATE_READY",
            "summary": "fixture developer completed the task",
            "commands": [{"command": "fixture-check", "result": "passed"}],
            "residual_risks": [],
        },
    )
    attempt = manager.finish_develop("T001")
    review = manager.prepare_review("T001")
    _write_json(
        Path(str(review["review_worktree"])) / ".workflow" / "review-result.json",
        {
            "task_id": "T001",
            "base_commit": attempt.base_commit,
            "candidate_commit": attempt.candidate_commit,
            "verdict": "PASS",
            "checks": [
                {
                    "id": "fixture",
                    "status": "PASS",
                    "evidence": ["fixture check"],
                    "finding": "satisfied",
                }
            ],
            "must_not_violations": [],
            "unknowns": [],
            "required_changes": [],
            "residual_risks": [],
        },
    )
    assert manager.finish_review("T001")[0] == "APPROVED"
    before = manager.load_config()["tasks"]["T001"]

    # Approval removes the record and the worktree, so put a record back the way
    # an older controller left it: the attempt is finished, its worktree gone.
    manager.save_attempt(
        AttemptRecord(
            task_id="T001",
            phase="P00",
            attempt=1,
            base_commit=attempt.base_commit,
            candidate_commit=str(attempt.candidate_commit),
            branch=attempt.branch,
            development_worktree=attempt.development_worktree,
        )
    )
    assert manager.status()["orphaned_attempts"] != []

    result = manager.discard_attempt("T001", reason="remnant left by an older controller")

    assert result["status"] == "APPROVED"
    assert result["next_attempt"] is None
    after = manager.load_config()["tasks"]["T001"]
    assert after["attempt"] == before["attempt"]
    assert after["approved_commit"] == before["approved_commit"]
    assert after["candidate_commit"] == before["candidate_commit"]
    assert manager.status()["orphaned_attempts"] == []


def test_discard_attempt_redirects_instead_of_trapping(tmp_path: Path) -> None:
    """A state the recovery cannot serve must name a route that can.

    A task branch reaches the main checkout only through an approval, so a lost
    attempt leaves the main checkout in PLANNED or READY. Any other state the
    committed artifacts support means something outside this path moved it, and
    the recovery refuses -- but ``abandon-task`` is reachable from every state,
    so the refusal always has somewhere to go.
    """
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    manager.prepare_develop("T001")
    _lose_the_worktree(manager, "T001")
    config = manager.load_config()
    _set_task_state(config, "T001", "CHANGES_REQUESTED")
    config["tasks"]["T001"]["attempt"] = 1
    config["workflow_state"] = "CHANGES_REQUESTED"
    _write_json(repo / "todo" / "config.yaml", config)
    # The state has to be one the evidence supports: the guard reads the
    # committed artifacts, so a status edit alone no longer describes a state
    # the workflow ever reaches (see the sibling test below for that case).
    _write_json(repo / "todo" / "reviews" / "P00" / "T001" / "review-001.json", {"verdict": "FAIL"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "move the task out of the recovery's reach")

    with pytest.raises(WorkflowError, match="close the work item with abandon-task instead"):
        manager.discard_attempt("T001", reason="out of reach")

    # The named route really is available from here.
    assert manager.abandon_task("T001", reason="the attempt is gone")["status"] == "ABANDONED"
    assert manager.status()["orphaned_attempts"] == []


def test_discard_attempt_refuses_a_record_its_artifacts_do_not_support(tmp_path: Path) -> None:
    """A recorded status with no artifact behind it is refused, not acted on.

    The guard reads the facts, so a config whose status was edited by hand can
    no longer decide what the recovery does: here the record claims
    CHANGES_REQUESTED while nothing committed supports it, and the refusal says
    so instead of silently treating the task as mid-flight.
    """
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    manager.prepare_develop("T001")
    _lose_the_worktree(manager, "T001")
    config = manager.load_config()
    _set_legacy_state(config, "T001", "CHANGES_REQUESTED")
    config["workflow_state"] = "CHANGES_REQUESTED"
    _write_json(repo / "todo" / "config.yaml", config)
    _git(repo, "add", "todo/config.yaml")
    _git(repo, "commit", "-m", "record a state no artifact produced")

    with pytest.raises(WorkflowError, match="its committed artifacts admit"):
        manager.discard_attempt("T001", reason="out of reach")

    # The disagreement is the thing to resolve, and `validate` is what says so.
    with pytest.raises(WorkflowError, match="disagrees with the committed artifacts"):
        manager.validate_repository()


def test_a_hand_edited_lane_claim_cannot_move_a_guard(tmp_path: Path) -> None:
    """Routing and reporting both read the facts, so editing one changes both.

    The lane claim is the fact that separates a planned task from a selected
    one. Dropping it by hand is not a state the workflow wrote, and the guard
    answers what the facts now say -- which is the whole reason routing stopped
    reading the field the claim used to be projected into.
    """
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    assert manager.status()["task_status"] == "READY"
    config["tasks"]["T001"]["claimed"] = False  # the decomposition only, by hand
    _write_json(repo / "todo" / "config.yaml", config)
    _git(repo, "add", "todo/config.yaml")
    _git(repo, "commit", "-m", "hand-edit the lane claim under READY")

    with pytest.raises(WorkflowError, match="develop requires"):
        manager.prepare_develop("T001")
    # The projection follows the facts as well: nobody has selected T001 now.
    assert manager.status()["task_status"] == "PLANNED"


# ---------------------------------------------------------------------------
# Bootstrap: PROPHET delegated-governance split
# ---------------------------------------------------------------------------
# These tests pin the contract introduced when the workflow controller was
# split into ``core`` (root, bootstrap-only) and ``core_governance`` (delegated
# governance, PROPHET-rewritable). They are not a replacement for the existing
# workflow tests; they live alongside them.


def _governance_symbols() -> tuple[Any, Any, Any, Any]:
    """Re-import the governance symbols without binding them at module load.

    Importing ``core_governance`` at the top of this file would force every
    other test to keep working when the controller is split; localising the
    import here lets the governance tests live next to the workflow tests
    without becoming a load-time dependency.
    """
    from tools.workflow.core import (  # noqa: WPS433  (deliberate late import)
        _PROPHET_EDITABLE_FILES,
        _PROPHET_FORBIDDEN_FILES,
        _change_statuses,
        _prophet_path_allowed,
    )

    return (
        _PROPHET_EDITABLE_FILES,
        _PROPHET_FORBIDDEN_FILES,
        _change_statuses,
        _prophet_path_allowed,
    )


# --- Class 1-6: PROPHET may modify delegated governance surfaces -----------


def test_prophet_may_edit_reviewer_prompts(tmp_path: Path) -> None:
    _, forbidden, _, allowed = _governance_symbols()
    for path in (
        ".claude/agents/stage-reviewer.md",
        ".claude/agents/plan-reviewer.md",
        ".claude/agents/workflow-manager.md",
    ):
        assert path not in forbidden
        assert allowed(path, "M") is True


def test_prophet_may_edit_review_result_schemas(tmp_path: Path) -> None:
    _, forbidden, _, allowed = _governance_symbols()
    for path in (
        "todo/schemas/review-result.schema.json",
        "todo/schemas/plan-review-result.schema.json",
        "todo/schemas/amendment-review-result.schema.json",
    ):
        assert path not in forbidden
        assert allowed(path, "M") is True


def test_prophet_may_edit_governance_predicates_file(tmp_path: Path) -> None:
    """The whole point of the split: PROPHET may rewrite core_governance.py."""
    _, forbidden, _, allowed = _governance_symbols()
    path = "tools/workflow/core_governance.py"
    assert path not in forbidden
    assert allowed(path, "M") is True


# --- Class 7: blacklist wins over allow-list (deny-first order) ------------


def test_prophet_may_not_edit_prophet_role_definition(tmp_path: Path) -> None:
    _, _, _, allowed = _governance_symbols()
    # Even though the agent prompt directory is otherwise open, the role's own
    # definition is permanent blacklist: the role cannot grant itself authority.
    assert allowed(".claude/agents/prophet.md", "M") is False
    assert allowed(".claude/agents/prophet-reviewer.md", "M") is False


def test_prophet_may_not_edit_root_controller(tmp_path: Path) -> None:
    _, _, _, allowed = _governance_symbols()
    # The root controller carries the state machine, transitions, and the gate
    # that calls into the governance predicates. Rewriting it would let an
    # amendment redefine its own approval.
    assert allowed("tools/workflow/core.py", "M") is False


def test_prophet_may_not_edit_state_bearing_config_fields(tmp_path: Path) -> None:
    _, _, _, allowed = _governance_symbols()
    assert allowed("todo/config.yaml", "M") is False


# --- Class 8: trusted-base review is mechanically guaranteed --------------


def test_review_worktree_derives_from_candidate_commit(tmp_path: Path) -> None:
    """Pin the review-worktree creation behaviour and surface the gap to the
    owner.

    The controller currently creates the independent review worktree from the
    candidate commit (``record.candidate_commit``), not the base commit. That
    means the reviewer evaluates the candidate using the **candidate's** copy
    of the controller and the schemas the candidate rewrote. For ordinary
    product amendments this is acceptable because the developer cannot edit
    the controller. For a PROPHET amendment that targets the governance
    predicates, however, it means a candidate that loosens ``_prophet_path_allowed``
    is reviewed against its own loosened predicate.

    This bootstrap does not change that behaviour — doing so requires a
    constitutional change to the controller itself, which is bootstrap-only.
    This test pins the current behaviour so the gap is explicit and the next
    bootstrap can close it without losing the recording.
    """
    manager, worktree = _prepare_prophet_change(tmp_path)
    (worktree / "todo" / "phases" / "P00" / "T002.md").write_text(_NEW_CONTRACT, encoding="utf-8")
    _add_task_to_config(worktree)
    _seal_prophet_result(worktree)
    candidate = manager.finish_amendment("A0001")
    base_commit = candidate.base_commit
    candidate_commit = candidate.candidate_commit
    assert base_commit != candidate_commit

    review_prepared = manager.prepare_amendment_review("A0001")
    review_worktree = Path(str(review_prepared["review_worktree"]))
    # Current behaviour: review worktree is created from the candidate commit.
    assert _git(review_worktree, "rev-parse", "HEAD") == candidate_commit
    assert _git(review_worktree, "rev-parse", "HEAD") != base_commit
    # The TRUSTED-BASE REVIEW invariant the owner instruction names — review
    # worktree derived from base_commit, not candidate_commit — is **not**
    # satisfied by the current controller. It is recorded here as a known
    # gap, not asserted.


# --- Class 9: legitimate delegated-governance amendment can PASS ----------


def test_legitimate_delegated_governance_amendment_can_pass(
    tmp_path: Path,
) -> None:
    """End-to-end: an amendment that touches only delegated-governance paths
    is reviewed against the pre-amendment gate, finds no boundary crossing,
    and returns the verdict the controller accepts.
    """
    manager, worktree = _prepare_prophet_change(tmp_path)
    # Create the delegated-governance file inside the worktree. The fixture
    # repo has no ``tools/workflow/`` directory, so we add it. The candidate
    # adds a single file under that directory and only touches the path that
    # the delegated-governance allow-list admits — ``core_governance.py`` —
    # which is not in the permanent blacklist.
    target_dir = worktree / "tools" / "workflow"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "core_governance.py"
    target.write_text(
        '"""Delegated-governance layer of the workflow controller.\n\n'
        "Created by test_legitimate_delegated_governance_amendment_can_pass.\n"
        '"""\n',
        encoding="utf-8",
    )
    _git(worktree, "add", "tools/workflow/core_governance.py")
    _git(
        worktree,
        "commit",
        "-m",
        "chore(amendment): add the governance predicates file",
    )
    _seal_prophet_result(worktree)
    candidate = manager.finish_amendment("A0001")
    review_state = _pass_amendment_review(manager, "A0001", candidate)
    assert review_state == "APPROVED"


# --- Class 10: amendment touching a blacklist path is rejected -----------


def test_amendment_touching_blacklist_path_is_rejected(tmp_path: Path) -> None:
    """The change-set check refuses a candidate that touches a blacklisted path.

    A PROPHET amendment candidate that modifies any file on the permanent
    blacklist is refused by ``_prophet_path_allowed`` regardless of git
    status. The predicate itself is the gate the controller's
    ``finish_amendment`` consults before review fires; if it returns False for
    a blacklisted path, the change-set check appends the path to its
    ``forbidden`` list and raises.

    This test pins the predicate behaviour for every blacklist entry across
    every git status letter, so a future reordering of deny-first cannot
    silently re-authorise a blacklisted path.
    """
    from tools.workflow.core_governance import (
        _PROPHET_FORBIDDEN_FILES,
        _prophet_path_allowed,
    )

    for blacklisted in _PROPHET_FORBIDDEN_FILES:
        for status in ("A", "M", "D", "??", ""):
            assert _prophet_path_allowed(blacklisted, status) is False, (
                f"blacklisted path {blacklisted!r} with status {status!r} "
                f"must be refused by the predicate"
            )
    # And the deny-first ordering wins even when a blacklisted path is also
    # an allowed path (which the predicate invariants forbid, but the test
    # pins that invariant independently).
    editable, forbidden, _, _ = _governance_symbols()
    assert not (editable & forbidden), (
        "no path may appear on both the editable allow-list and the permanent "
        "blacklist; if the lists overlap, the deny-first ordering check would "
        "silently become vacuous"
    )


# --- Class 11: existing valid PROPHET future-plan amendments still work ---


def test_prophet_can_still_create_a_new_task_contract(tmp_path: Path) -> None:
    """The original PROPHET responsibility — adding a new task contract under
    ``todo/phases/`` — must still work after the governance split.
    """
    from tools.workflow.core import _prophet_path_allowed

    assert _prophet_path_allowed("todo/phases/P00-engineering-baseline/T999.md", "A") is True
    # And an existing contract path with status M (modified) is refused —
    # this is the original never rule.
    assert _prophet_path_allowed("todo/phases/P00-engineering-baseline/T001.md", "M") is False


# --- Class 12: helper that the other governance tests already need --------


def test_governance_predicate_ordering(tmp_path: Path) -> None:
    """Deny-first ordering: even if a path is on the allow-list, an entry on
    the permanent blacklist overrides it. This pins the order so a future
    reordering cannot silently re-authorise a blacklisted path.
    """
    editable, forbidden, _, _ = _governance_symbols()
    # The role's own prompt directory is not in either list, but if the
    # blacklist ever grew to include something also on the allow-list, the
    # deny must win.
    common = editable & forbidden
    assert not common, (
        "no path may appear on both the editable allow-list and the permanent "
        "blacklist; if the lists overlap, the deny-first ordering test below "
        "would silently become vacuous"
    )
    # And the predicate must check forbidden before editable.
    _, _, _, allowed = _governance_symbols()
    # Spot-check the deny-first branch by constructing an artificial forbidden
    # path that is also on the editable list (only possible if the test's own
    # invariants are violated, which the assertion above prevents).
    assert allowed(".claude/agents/prophet.md", "M") is False
    assert allowed(".claude/agents/stage-reviewer.md", "M") is True


# ---------------------------------------------------------------------------
# X2 bootstrap: continue-* commands and CONTINUATION_REQUIRED schema support
# ---------------------------------------------------------------------------


def _prepare_amendment_in_planning(tmp_path: Path) -> tuple[WorkflowManager, str, Path]:
    """Drive a PROPHET amendment to PLANNING status without writing a result."""
    manager, worktree = _prepare_prophet_change(tmp_path)
    return manager, "A0001", worktree


def test_continue_amendment_without_flag_or_handoff_raises(tmp_path: Path) -> None:
    """continue_amendment without --max-turns-exhausted and no handoff refuses."""
    manager, amendment_id, worktree = _prepare_amendment_in_planning(tmp_path)
    with pytest.raises(WorkflowError, match="CONTINUATION_REQUIRED handoff"):
        manager.continue_amendment(amendment_id)


def test_continue_amendment_with_max_turns_exhausted_returns_prompt(tmp_path: Path) -> None:
    """continue_amendment --max-turns-exhausted returns a fresh prompt and bumps count."""
    manager, amendment_id, worktree = _prepare_amendment_in_planning(tmp_path)
    out = manager.continue_amendment(amendment_id, max_turns_exhausted=True)
    assert out["continuation_count"] == 1
    assert "agent" in out
    assert amendment_id in out["amendment_id"]
    assert "checkpoint" in out["prompt"].lower() or "worktree" in out["prompt"].lower()
    # The worktree now carries an amendment-continuation.json handoff
    assert (worktree / ".workflow" / "amendment-continuation.json").is_file()


def test_continue_amendment_exceeds_limit(tmp_path: Path) -> None:
    """A second continue beyond MAX_DEVELOPMENT_CONTINUATIONS is refused."""
    manager, amendment_id, _ = _prepare_amendment_in_planning(tmp_path)
    manager.continue_amendment(amendment_id, max_turns_exhausted=True)
    with pytest.raises(WorkflowError, match="amendment continuation limit reached"):
        manager.continue_amendment(amendment_id, max_turns_exhausted=True)


def test_continue_task_review_refuses_without_flag_when_no_handoff(
    tmp_path: Path,
) -> None:
    """continue_task_review needs the explicit flag when the reviewer wrote nothing."""
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    prepared = manager.prepare_develop("T001")
    development = Path(str(prepared["development_worktree"]))
    (development / "src" / "value.txt").write_text("candidate\n", encoding="utf-8")
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "T001",
            "outcome": "CANDIDATE_READY",
            "summary": "candidate ready",
            "commands": [],
            "residual_risks": [],
        },
    )
    manager.finish_develop("T001")
    manager.prepare_review("T001")
    # Reviewer wrote nothing — review-result.json does not exist.
    with pytest.raises(WorkflowError, match="--max-turns-exhausted"):
        manager.continue_task_review("T001")


def test_continue_maintenance_review_with_max_turns_exhausted(tmp_path: Path) -> None:
    """continue_maintenance_review --max-turns-exhausted bumps the maintenance continuation count."""
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    prepared = manager.prepare_maintenance(
        summary="continue-review test repair",
        reason="x",
        allowed_paths=["src/value.txt"],
        verification_commands=["test -f src/value.txt"],
        related_task="T000",
    )
    development = Path(str(prepared["development_worktree"]))
    (development / "src" / "value.txt").write_text("repaired\n", encoding="utf-8")
    _write_json(
        development / ".workflow" / "developer-result.json",
        {
            "task_id": "M0001",
            "outcome": "CANDIDATE_READY",
            "summary": "candidate ready",
            "commands": [],
            "residual_risks": [],
        },
    )
    manager.finish_maintenance_develop("M0001")
    review = manager.prepare_maintenance_review("M0001")
    Path(str(review["review_worktree"]))  # ensure review worktree exists
    out = manager.continue_maintenance_review("M0001", max_turns_exhausted=True)
    assert out["continuation_count"] == 1
    assert out["maintenance_id"] == "M0001"


def test_continue_amendment_result_schema_accepts_continuation_required(
    tmp_path: Path,
) -> None:
    """amendment-result.schema.json accepts outcome=CONTINUATION_REQUIRED with a continuation block.

    Drive a real PROPHET amendment into the continue-amendment path with a
    pre-existing CONTINUATION_REQUIRED handoff; controller reads it via the
    amendment-result validator, so passing it is the schema-level acceptance
    test.
    """
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    prepared = manager.prepare_amendment(
        task_ids=[],
        layer="PROPHET",
        summary="schema accept test",
        owner_direction="test CONTINUATION_REQUIRED acceptance",
    )
    amendment_id = str(prepared["amendment_id"])
    worktree = Path(str(prepared["worktree"]))
    _write_json(
        worktree / ".workflow" / "amendment-result.json",
        {
            "amendment_id": amendment_id,
            "outcome": "CONTINUATION_REQUIRED",
            "summary": "agent interrupted",
            "rationale": "needs another session",
            "unresolved_questions": [],
            "impact_assessment": {
                "intent": "no change",
                "specification": "no change",
                "contracts": "no change",
                "dependencies": "no change",
                "implementation": "no change",
                "data": "no change",
                "operations": "no change",
                "security": "no change",
                "verification": "no change",
            },
            "affected_existing_tasks": [],
            "resolved_task_impacts": [],
            "continuation": {
                "reason": "TURN_BUDGET",
                "completed_work": [],
                "remaining_work": ["finish the planning"],
                "next_actions": ["inspect git diff"],
                "changed_paths": [],
            },
        },
    )
    # Validator must accept it without raising and bump continuation_count.
    out = manager.continue_amendment(amendment_id, max_turns_exhausted=False)
    assert out["continuation_count"] == 1
