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
    (repo / "todo" / "README.md").write_text("test workflow\n", encoding="utf-8")
    for agent in (
        "stage-developer",
        "stage-reviewer",
        "issue-triager",
        "planner",
        "plan-reviewer",
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
