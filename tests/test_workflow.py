"""Mechanical workflow gates and isolated FAIL -> repair -> PASS exercise."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest
from tools.workflow import WorkflowError, WorkflowManager


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
    for agent in ("stage-developer", "stage-reviewer"):
        path = repo / ".claude" / "agents" / f"{agent}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {agent}\ndescription: test\n---\n", encoding="utf-8")
    schema_dir = repo / "todo" / "schemas"
    for schema in ("config", "developer-result", "review-result"):
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


def _make_fake_claude(tmp_path: Path) -> Path:
    script = tmp_path / "fake-claude"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import re
            import sys
            from pathlib import Path

            assert sys.argv[sys.argv.index("--model") + 1] == "MiniMax-M3[1m]"
            assert "--fallback-model" not in sys.argv
            agent = sys.argv[sys.argv.index("--agent") + 1]
            prompt = sys.argv[-1]
            if agent == "stage-developer":
                repaired = Path("todo/reviews").exists()
                Path("src/value.txt").write_text("good\\n" if repaired else "bad\\n")
                payload = {
                    "task_id": "T001",
                    "outcome": "CANDIDATE_READY",
                    "summary": "deterministic fake candidate",
                    "commands": [{"command": "fake-check", "result": "completed"}],
                    "residual_risks": [],
                    "blocking_question": None,
                }
            elif agent == "stage-reviewer":
                base = re.search(r"Base commit: ([0-9a-f]{40})", prompt).group(1)
                candidate = re.search(r"Candidate commit: ([0-9a-f]{40})", prompt).group(1)
                passed = Path("src/value.txt").read_text().strip() == "good"
                payload = {
                    "task_id": "T001",
                    "base_commit": base,
                    "candidate_commit": candidate,
                    "verdict": "PASS" if passed else "FAIL",
                    "checks": [{
                        "id": "ACCEPTANCE",
                        "status": "PASS" if passed else "FAIL",
                        "evidence": ["src/value.txt"] if passed else [],
                        "finding": "value is good" if passed else "value is not good",
                    }],
                    "must_not_violations": [],
                    "unknowns": [],
                    "required_changes": [] if passed else ["write the required good value"],
                    "residual_risks": [],
                }
            else:
                raise SystemExit(3)
            print(json.dumps({"structured_output": payload}))
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | 0o111)
    return script


def test_config_validation_rejects_dependency_cycle(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, claude_command="unused", worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    config["tasks"]["T000"]["depends_on"] = ["T001"]
    with pytest.raises(WorkflowError, match="dependency cycle"):
        manager.validate_config(config)


def test_protected_path_change_is_rejected(tmp_path: Path) -> None:
    repo, base = _make_repo(tmp_path)
    manager = WorkflowManager(repo, claude_command="unused", worktree_root=tmp_path / "worktrees")
    (repo / "todo" / "phases" / "P00" / "T001.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(WorkflowError, match="protected paths changed"):
        manager.check_changed_paths(base)


def test_short_or_symbolic_base_is_rejected(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, claude_command="unused", worktree_root=tmp_path / "worktrees")
    with pytest.raises(WorkflowError, match="full lowercase Git SHA"):
        manager.check_changed_paths("HEAD")


def test_illegal_state_transition_is_rejected(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, claude_command="unused", worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    with pytest.raises(WorkflowError, match="READY -> APPROVED"):
        manager._set_state(config, "T001", "APPROVED")


def test_non_m3_agent_runtime_is_rejected(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    manager = WorkflowManager(repo, claude_command="unused", worktree_root=tmp_path / "worktrees")
    config = manager.load_config()
    config["agent_runtime"]["model"] = "MiniMax-M2.7"
    with pytest.raises(WorkflowError, match="MiniMax-M3"):
        manager.validate_config(config)


def test_fake_fail_repair_pass_workflow(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    fake_claude = _make_fake_claude(tmp_path)
    manager = WorkflowManager(
        repo,
        claude_command=str(fake_claude),
        worktree_root=tmp_path / "worktrees",
    )

    first = manager.develop("T001")
    assert first.candidate_commit is not None
    state, failed_report = manager.review("T001")
    assert state == "CHANGES_REQUESTED"
    assert failed_report.is_file()

    second = manager.develop("T001", retry=True)
    assert second.attempt == 2
    assert second.candidate_commit != first.candidate_commit
    state, passed_report = manager.review("T001")

    assert state == "APPROVED"
    assert passed_report.is_file()
    assert (repo / "src" / "value.txt").read_text(encoding="utf-8") == "good\n"
    config = manager.load_config()
    assert config["tasks"]["T001"]["status"] == "APPROVED"
    assert config["tasks"]["T001"]["approved_commit"] == second.candidate_commit
    assert _git(repo, "status", "--porcelain") == ""
    assert manager.load_attempt("T001") is None
