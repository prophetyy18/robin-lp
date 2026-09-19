"""Mechanical workflow gates and isolated FAIL -> repair -> PASS exercise."""

from __future__ import annotations

import json
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
            "## References\n\nNone.\n",
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
    assert manager.amendment_status("A0001") == {
        "amendment_id": "A0001",
        "status": "APPROVED",
    }


def test_supersede_amendment_retires_an_approved_task(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, worktree_root=tmp_path / "worktrees")
    _make_future_task_planned(repo, manager)
    before = manager.load_config()["tasks"]["T000"]
    prepared = manager.prepare_amendment(
        task_ids=["T000"],
        layer="SUPERSEDE",
        summary="retire the T000 decision in favour of T001",
        owner_direction="T001 supersedes T000; record the successor and keep the history.",
    )
    assert prepared["layer"] == "SUPERSEDE"
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
        },
    )
    with pytest.raises(WorkflowError, match="workflow state, evidence, model, SHA"):
        manager.finish_amendment("A0001")
    assert manager.load_config()["tasks"]["T000"]["approved_commit"] == before["approved_commit"]


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


def test_every_nonterminal_state_has_a_route_to_approved() -> None:
    def can_reach_approved(start: str) -> bool:
        pending = [start]
        visited: set[str] = set()
        while pending:
            state = pending.pop()
            if state == "APPROVED":
                return True
            if state in visited:
                continue
            visited.add(state)
            pending.extend(ALLOWED_TRANSITIONS[state])
        return False

    assert set(ALLOWED_TRANSITIONS) == STATES
    for state in STATES - {"APPROVED"}:
        assert can_reach_approved(state), f"{state} is a workflow dead end"


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


def _seal_prophet_result(worktree: Path) -> None:
    _write_json(
        worktree / ".workflow" / "amendment-result.json",
        {
            "amendment_id": "A0001",
            "outcome": "AMENDMENT_READY",
            "summary": "applied the recorded direction",
            "rationale": "matches the recorded direction",
            "unresolved_questions": [],
        },
    )


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
