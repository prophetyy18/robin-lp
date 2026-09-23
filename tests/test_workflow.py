"""Mechanical workflow gates and isolated FAIL -> repair -> PASS exercise."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from tools.workflow import WorkflowError, WorkflowManager
from tools.workflow.core import ALLOWED_TRANSITIONS, STATES


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
                "status": "APPROVED",
                "depends_on": [],
                "task_file": "todo/phases/P00/T000.md",
                "approved_commit": baseline,
                **common,
            },
            "T001": {
                "phase": "P00",
                "status": "READY",
                "depends_on": ["T000"],
                "task_file": "todo/phases/P00/T001.md",
                "approved_commit": None,
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
    for schema in (
        "config",
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
    config["tasks"]["T001"]["status"] = "APPROVED"
    config["tasks"]["T001"]["approved_commit"] = _git(repo, "rev-parse", "HEAD")
    config["active_task"] = "T001"
    config["active_phase"] = "P00"
    config["workflow_state"] = "APPROVED"
    _write_json(repo / "todo" / "config.yaml", config)
    _git(repo, "add", "todo/config.yaml")
    _git(repo, "commit", "-m", "approve seed task")


def _make_future_task_planned(repo: Path, manager: WorkflowManager) -> None:
    config = manager.load_config()
    config["tasks"]["T001"]["status"] = "PLANNED"
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
    assert config["tasks"]["T001"]["status"] == "PLANNED"
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
    assert after["status"] == "APPROVED"
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


def test_config_rejects_more_than_one_active_task(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    config["tasks"]["T000"]["status"] = "READY"
    with pytest.raises(WorkflowError, match="exactly the active task"):
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
    config["tasks"]["T001"]["status"] = "PLANNED"
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
    assert manager.load_config()["tasks"]["T001"]["status"] == "READY"


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
        "status": "PLANNED",
        "depends_on": ["T000"],
        "task_file": f"todo/phases/P00/{task_id}.md",
        "attempt": 0,
        "base_commit": None,
        "candidate_commit": None,
        "approved_commit": None,
        "latest_review": None,
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
    assert added["status"] == "PLANNED"
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
        "status": "PLANNED",
        "depends_on": ["T001"],
        "task_file": "todo/phases/P00/T002.md",
        "attempt": 0,
        "base_commit": None,
        "candidate_commit": None,
        "approved_commit": None,
        "latest_review": None,
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
    assert manager.load_config()["tasks"]["T001"]["status"] == "ABANDONED"
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
    assert config["tasks"]["T001"]["status"] == "ABANDONED"
    assert config["tasks"]["T002"]["status"] == "ABANDONED"


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


def test_discard_attempt_redirects_instead_of_trapping(tmp_path: Path) -> None:
    """A state the recovery cannot serve must name a route that can.

    A task branch reaches the main checkout only through an approval, so a lost
    attempt leaves the main checkout in PLANNED or READY. Any other recorded
    state means something outside this path moved it, and the recovery refuses
    -- but ``abandon-task`` is reachable from every state, so the refusal always
    has somewhere to go.
    """
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    manager.prepare_develop("T001")
    _lose_the_worktree(manager, "T001")
    config = manager.load_config()
    config["tasks"]["T001"]["status"] = "CHANGES_REQUESTED"
    config["workflow_state"] = "CHANGES_REQUESTED"
    _write_json(repo / "todo" / "config.yaml", config)
    _git(repo, "add", "todo/config.yaml")
    _git(repo, "commit", "-m", "move the task out of the recovery's reach")

    with pytest.raises(WorkflowError, match="close the work item with abandon-task instead"):
        manager.discard_attempt("T001", reason="out of reach")

    # The named route really is available from here.
    assert manager.abandon_task("T001", reason="the attempt is gone")["status"] == "ABANDONED"
    assert manager.status()["orphaned_attempts"] == []
