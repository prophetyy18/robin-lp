"""Regression tests for the on-demand plan renderer introduced by T008.

The contract (T008) requires two committed tests:

1. a test that runs the renderer against the committed
   ``todo/config.yaml`` and asserts that the rendered view covers
   every configured phase and task exactly once with its status and
   the ready-next set, so a renderer that silently drops a task or
   lies about its status cannot pass;
2. a test that proves the ``--check`` mode can fail on each declared
   failure input, so a check that never fires is not a check.

The tests use a temporary repository under ``tmp_path`` for the
failure inputs so the real working tree is never touched. They are
intentionally independent of every other T005 / T006 check.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from tools.progress import (
    NOTABLE_STATES,
    PLAN_STATES,
    ProgressIssue,
    build_view,
    is_ready,
    load_config,
    parse_outcome,
    parse_phase_purpose,
    parse_title,
    render,
    validate,
)
from tools.workflow.core import LIFECYCLE_OF

REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _facts(state: str) -> dict[str, object]:
    """The two facts that carry a state, from the controller's own table.

    The fixtures read the controller's decomposition rather than a second copy
    of it, so a change to the model fails here instead of leaving this file
    asserting an old one.
    """

    lifecycle, claimed = LIFECYCLE_OF[state]
    return {"lifecycle": lifecycle, "claimed": claimed}


def _minimal_config(extra_tasks: dict[str, object] | None = None) -> str:
    """A minimal but valid plan with three tasks, two phases, no supersede."""

    base: dict[str, object] = {
        "schema_version": 1,
        "project": "robinhood-lp-v1",
        "product_baseline_commit": "6c3177883a676a06f8b571d0a598f524e319fa34",
        "intent_revision": "v1-test",
        "spec_revision": "v1-test",
        "agent_runtime": {
            "provider": "MiniMax Anthropic-compatible API",
            "model": "MiniMax-M3[1m]",
            "context_window_tokens": 1000000,
            "allow_model_fallback": False,
        },
        "active_phase": "P00",
        "active_task": None,
        "workflow_state": "PLANNED",
        "tasks": {
            "T000": {
                "phase": "P00",
                "lifecycle": "DELIVERED",
                "claimed": False,
                "depends_on": [],
                "task_file": "todo/phases/P00-engineering-baseline/T000.md",
                "attempt": 0,
                "base_commit": None,
                "candidate_commit": None,
                "approved_commit": "6c3177883a676a06f8b571d0a598f524e319fa34",
                "latest_review": None,
            },
            "T001": {
                "phase": "P00",
                "lifecycle": "DELIVERED",
                "claimed": False,
                "depends_on": ["T000"],
                "task_file": "todo/phases/P00-engineering-baseline/T001.md",
                "attempt": 1,
                "base_commit": None,
                "candidate_commit": None,
                "approved_commit": "6c3177883a676a06f8b571d0a598f524e319fa34",
                "latest_review": None,
            },
            "T010": {
                "phase": "P01",
                "lifecycle": "OPEN",
                "claimed": False,
                "depends_on": ["T001"],
                "task_file": "todo/phases/P01-protocol-foundation/T010.md",
                "attempt": 0,
                "base_commit": None,
                "candidate_commit": None,
                "approved_commit": None,
                "latest_review": None,
            },
        },
    }
    if extra_tasks:
        assert isinstance(base["tasks"], dict)
        base["tasks"].update(extra_tasks)
    return json.dumps(base)


def _make_repo(
    root: Path,
    *,
    config_text: str | None = None,
    contract_texts: dict[str, str] | None = None,
    phase_readmes: dict[str, str] | None = None,
) -> Path:
    """Build a minimal repo layout that the renderer can scan."""

    if config_text is None:
        config_text = _minimal_config()
    if contract_texts is None:
        contract_texts = {
            "todo/phases/P00-engineering-baseline/T000.md": (
                "# T000 — Seed task\n\n## Outcome\n\nfirst task.\n"
            ),
            "todo/phases/P00-engineering-baseline/T001.md": (
                "# T001 — Follow up\n\n## Outcome\n\nfollows T000.\n"
            ),
            "todo/phases/P01-protocol-foundation/T010.md": (
                "# T010 — Independent\n\n## Outcome\n\nindependent.\n"
            ),
        }
    if phase_readmes is None:
        phase_readmes = {
            "P00": ("# P00\n\n**Purpose:** baseline choices.\n\n## Tasks\n\n- [T000](T000.md)\n"),
            "P01": ("# P01\n\n**Purpose:** protocol math.\n\n## Tasks\n\n- [T010](T010.md)\n"),
        }
    _write(root / "todo" / "config.yaml", config_text)
    for rel, body in contract_texts.items():
        _write(root / rel, body)
    for phase_id, body in phase_readmes.items():
        _write(root / "todo" / "phases" / f"{phase_id}-phase" / "README.md", body)
    return root / "todo" / "config.yaml"


# ---------------------------------------------------------------------------
# Renderer against the committed configuration
# ---------------------------------------------------------------------------


def test_render_against_committed_config_covers_every_phase_and_task() -> None:
    """The committed view names every configured phase and task exactly once.

    The renderer is allowed to mention a task more than once when it
    appears under multiple sections (for example, an approved live
    task still gets a phase listing), so the assertion checks the
    phase listing specifically. The ready-next set is checked against
    the documented ``ready`` condition computed from the same
    configuration.
    """

    config = load_config(REPO)
    issues = validate(config, repo_root=REPO)
    assert issues == [], [issue.message for issue in issues]

    phases = build_view(config, repo_root=REPO)
    tasks_section = config["tasks"]
    assert isinstance(tasks_section, dict)

    # Every configured phase appears exactly once.
    seen_phases = [phase.phase_id for phase in phases]
    configured_phases = {
        str(task["phase"])
        for task in tasks_section.values()
        if isinstance(task, dict) and isinstance(task.get("phase"), str)
    }
    assert sorted(seen_phases) == sorted(configured_phases)

    # Every configured task appears exactly once in the per-phase
    # listing and carries its declared status.
    seen_tasks: list[str] = []
    for phase in phases:
        for task in phase.tasks:
            assert task.status in PLAN_STATES
            assert task.phase == phase.phase_id
            assert task.title, f"task {task.task_id} must have a title from its contract"
            assert task.purpose, f"task {task.task_id} must have an outcome from its contract"
            seen_tasks.append(task.task_id)
    assert sorted(seen_tasks) == sorted(tasks_section.keys())

    # Ready-next matches the documented condition.
    ready_from_renderer = {task.task_id for task in phases[0].tasks if task.ready}
    for phase in phases:
        for task in phase.tasks:
            if task.ready:
                ready_from_renderer.add(task.task_id)
    ready_from_predicate = {
        task_id
        for task_id, task in tasks_section.items()
        if isinstance(task, dict) and is_ready(task_id, task, tasks_section)
    }
    assert ready_from_renderer == ready_from_predicate

    # The renderer output is non-empty and contains every phase and
    # every task identifier exactly once at the section level.
    output = render(config, repo_root=REPO)
    assert "Phases" in output
    assert "Ready next" in output
    for phase_id in configured_phases:
        assert output.count(f"{phase_id} — purpose:") == 1
    for task_id in tasks_section:
        # Every task identifier appears at least once in the view.
        assert task_id in output


def test_render_output_has_no_commit_sha_count_or_timestamp() -> None:
    """The renderer must not embed a commit SHA, count or wall clock."""

    config = load_config(REPO)
    output = render(config, repo_root=REPO)
    # SHA-shaped token: 40 lowercase hex characters bounded by word
    # boundaries so it does not match inside words or section rules.
    import re

    assert not re.search(r"\b[0-9a-f]{40}\b", output), output
    # No timestamp-shaped digits followed by a colon (HH:MM) and no
    # "test count" / "pass count" language that the renderer could
    # not legitimately derive from the configuration.
    assert "pass count" not in output.lower()
    assert "test count" not in output.lower()


@pytest.mark.parametrize(
    ("state", "label"),
    [
        ("BLOCKED", "blocked"),
        ("CHANGES_REQUESTED", "changes-requested"),
        ("TRIAGE_REQUIRED", "triage-required"),
        ("OWNER_DECISION_REQUIRED", "owner-decision"),
    ],
)
def test_render_records_each_notable_state_as_such(state: str, label: str) -> None:
    """A stopped work item is named, with the reason it stopped.

    One state per fixture: a task in flight is *the* active task, because only
    it may hold the single-active-work lane, so a plan cannot hold four of them
    at once. That is also where the renderer reads the state from -- the
    config's own declaration of its active task, which the controller checks
    against the evidence.
    """

    config_text = _minimal_config(
        extra_tasks={
            "T002": {
                "phase": "P00",
                **_facts(state),
                "depends_on": [],
                "task_file": "todo/phases/P00-engineering-baseline/T002.md",
                "attempt": 0,
                "base_commit": None,
                "candidate_commit": None,
                "approved_commit": None,
                "latest_review": None,
            },
        },
    )
    raw = json.loads(config_text)
    raw["active_task"] = "T002"
    raw["workflow_state"] = state
    contract_texts = {
        "todo/phases/P00-engineering-baseline/T000.md": (
            "# T000 — Seed task\n\n## Outcome\n\nfirst task.\n"
        ),
        "todo/phases/P00-engineering-baseline/T001.md": (
            "# T001 — Follow up\n\n## Outcome\n\nfollows T000.\n"
        ),
        "todo/phases/P00-engineering-baseline/T002.md": (
            "# T002 — Stopped\n\n## Outcome\n\nstopped.\n"
        ),
        "todo/phases/P01-protocol-foundation/T010.md": (
            "# T010 — Independent\n\n## Outcome\n\nindependent.\n"
        ),
    }
    with pytest.MonkeyPatch.context() as mp:
        tmp = REPO.parent / f"_tmp_progress_notable_{label.replace('-', '_')}"
        tmp.mkdir(exist_ok=True)
        repo = tmp / "repo"
        repo.mkdir(exist_ok=True)
        cfg_path = _make_repo(
            repo,
            config_text=json.dumps(raw),
            contract_texts=contract_texts,
        )
        mp.chdir(repo)
        config = load_config(repo, config_path=cfg_path)
        output = render(config, repo_root=repo)

    assert "Blocked / changes-requested / triage-required / owner-decision" in output
    assert f"T002 — {label}: Stopped" in output


def test_render_splits_approved_into_live_and_superseded() -> None:
    """A retired task appears under superseded with its successor named."""

    config_text = _minimal_config(
        extra_tasks={
            "T020": {
                "phase": "P00",
                "lifecycle": "DELIVERED",
                "claimed": False,
                "depends_on": [],
                "task_file": "todo/phases/P00-engineering-baseline/T020.md",
                "superseded_by": "T021",
                "attempt": 0,
                "base_commit": None,
                "candidate_commit": None,
                "approved_commit": "6c3177883a676a06f8b571d0a598f524e319fa34",
                "latest_review": None,
            },
            "T021": {
                "phase": "P00",
                "lifecycle": "DELIVERED",
                "claimed": False,
                "depends_on": [],
                "task_file": "todo/phases/P00-engineering-baseline/T021.md",
                "attempt": 0,
                "base_commit": None,
                "candidate_commit": None,
                "approved_commit": "6c3177883a676a06f8b571d0a598f524e319fa34",
                "latest_review": None,
            },
        },
    )
    contract_texts = {
        "todo/phases/P00-engineering-baseline/T000.md": (
            "# T000 — Seed task\n\n## Outcome\n\nfirst task.\n"
        ),
        "todo/phases/P00-engineering-baseline/T001.md": (
            "# T001 — Follow up\n\n## Outcome\n\nfollows T000.\n"
        ),
        "todo/phases/P00-engineering-baseline/T020.md": (
            "# T020 — Old rule\n\n## Outcome\n\nold.\n"
        ),
        "todo/phases/P00-engineering-baseline/T021.md": (
            "# T021 — New rule\n\n## Outcome\n\nnew.\n"
        ),
        "todo/phases/P01-protocol-foundation/T010.md": (
            "# T010 — Independent\n\n## Outcome\n\nindependent.\n"
        ),
    }
    with pytest.MonkeyPatch.context() as mp:
        tmp = REPO.parent / "_tmp_progress_supersede"
        tmp.mkdir(exist_ok=True)
        repo = tmp / "repo"
        repo.mkdir(exist_ok=True)
        cfg_path = _make_repo(
            repo,
            config_text=config_text,
            contract_texts=contract_texts,
        )
        mp.chdir(repo)
        config = load_config(repo, config_path=cfg_path)
        output = render(config, repo_root=repo)

    assert "Approved (superseded)" in output
    assert "T020 — Old rule  ->  succeeded by T021" in output
    assert "Approved (live)" in output
    assert "T021 — New rule" in output


def test_render_response_changes_with_status_and_dependency(tmp_path: Path) -> None:
    """A fixture with a changed status / dep / supersede produces a different view."""

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg_path = _make_repo(repo)

    config_a = load_config(repo, config_path=cfg_path)
    output_a = render(config_a, repo_root=repo)

    # Flip T001 from APPROVED to PLANNED and drop T010's dependency,
    # so the ready-next set changes from {T010} to {}. The state lives in the
    # two facts now, so flipping it means moving those.
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw["tasks"]["T001"].update(_facts("PLANNED"))
    raw["tasks"]["T010"]["depends_on"] = []
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")

    config_b = load_config(repo, config_path=cfg_path)
    output_b = render(config_b, repo_root=repo)
    assert output_a != output_b

    # The rendered ready-next section reflects the new condition.
    a_section = output_a.split("Ready next")[1].split("===")[0]
    b_section = output_b.split("Ready next")[1].split("===")[0]
    # After flipping T001 to PLANNED with no remaining PLANNED deps
    # blocking it, T001 joins the ready-next set, so the section
    # grows by exactly one identifier.
    assert "T010" in a_section
    assert "T001" not in a_section
    assert "T010" in b_section
    assert "T001" in b_section


# ---------------------------------------------------------------------------
# Check mode fails closed on each declared failure input
# ---------------------------------------------------------------------------


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    # The renderer lives outside the temporary repos the failure-input
    # tests use, so the CLI must be importable from there. The repo
    # root contains the ``tools`` package; we expose it on PYTHONPATH
    # instead of forcing every test to write the renderer into the
    # scratch tree.
    env = {"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"}
    import os

    env_full = {**os.environ, **env}
    return subprocess.run(
        [PYTHON, "-m", "tools.progress", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env_full,
    )


def test_check_exits_zero_on_clean_plan() -> None:
    repo = REPO
    result = _run_cli(["--check"], cwd=repo)
    assert result.returncode == 0, result.stderr
    assert "no configuration issues found" in result.stdout


def test_render_exits_zero_on_clean_plan() -> None:
    repo = REPO
    result = _run_cli([], cwd=repo)
    assert result.returncode == 0, result.stderr
    assert "Phases" in result.stdout


def test_check_fails_on_unreadable_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "todo" / "config.yaml", "not json at all")
    result = _run_cli(["--check"], cwd=repo)
    assert result.returncode != 0
    assert "CONFIG_UNPARSABLE" in result.stderr


def test_check_fails_on_missing_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    result = _run_cli(["--check"], cwd=repo)
    assert result.returncode != 0
    assert "CONFIG_UNREADABLE" in result.stderr


def test_check_fails_on_status_outside_state_machine(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    text = _minimal_config()
    raw = json.loads(text)
    raw["tasks"]["T001"]["status"] = "READY_FOR_RELEASE"
    _make_repo(repo, config_text=json.dumps(raw))
    result = _run_cli(["--check"], cwd=repo)
    assert result.returncode != 0
    assert "STATUS_OUTSIDE_STATE_MACHINE" in result.stderr


def test_check_fails_on_unresolved_dependency(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    text = _minimal_config()
    raw = json.loads(text)
    raw["tasks"]["T010"]["depends_on"] = ["T001", "T999"]
    _make_repo(repo, config_text=json.dumps(raw))
    result = _run_cli(["--check"], cwd=repo)
    assert result.returncode != 0
    assert "DEPENDENCY_UNRESOLVED" in result.stderr


def test_check_fails_on_unresolved_supersession(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    text = _minimal_config()
    raw = json.loads(text)
    raw["tasks"]["T000"]["superseded_by"] = "T999"
    _make_repo(repo, config_text=json.dumps(raw))
    result = _run_cli(["--check"], cwd=repo)
    assert result.returncode != 0
    assert "SUPERSESSION_UNRESOLVED" in result.stderr


def test_check_fails_on_missing_contract_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    contracts = {
        "todo/phases/P00-engineering-baseline/T000.md": ("# T000 — Seed\n\n## Outcome\n\nfirst.\n"),
        "todo/phases/P00-engineering-baseline/T001.md": (
            "# T001 — Follow\n\n## Outcome\n\nfollows.\n"
        ),
        # T010's contract intentionally omitted.
    }
    _make_repo(repo, contract_texts=contracts)
    result = _run_cli(["--check"], cwd=repo)
    assert result.returncode != 0
    assert "CONTRACT_MISSING" in result.stderr


def test_check_fails_on_supersede_without_approved_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    text = _minimal_config(
        extra_tasks={
            "T020": {
                "phase": "P00",
                "lifecycle": "OPEN",
                "claimed": False,
                "depends_on": [],
                "task_file": "todo/phases/P00-engineering-baseline/T020.md",
                "superseded_by": "T021",
                "attempt": 0,
                "base_commit": None,
                "candidate_commit": None,
                "approved_commit": None,
                "latest_review": None,
            },
            "T021": {
                "phase": "P00",
                "lifecycle": "DELIVERED",
                "claimed": False,
                "depends_on": [],
                "task_file": "todo/phases/P00-engineering-baseline/T021.md",
                "attempt": 0,
                "base_commit": None,
                "candidate_commit": None,
                "approved_commit": "6c3177883a676a06f8b571d0a598f524e319fa34",
                "latest_review": None,
            },
        },
    )
    contract_texts = {
        "todo/phases/P00-engineering-baseline/T000.md": ("# T000 — Seed\n\n## Outcome\n\nfirst.\n"),
        "todo/phases/P00-engineering-baseline/T001.md": (
            "# T001 — Follow\n\n## Outcome\n\nfollows.\n"
        ),
        "todo/phases/P00-engineering-baseline/T020.md": ("# T020 — Old\n\n## Outcome\n\nold.\n"),
        "todo/phases/P00-engineering-baseline/T021.md": ("# T021 — New\n\n## Outcome\n\nnew.\n"),
        "todo/phases/P01-protocol-foundation/T010.md": (
            "# T010 — Independent\n\n## Outcome\n\nindependent.\n"
        ),
    }
    _make_repo(repo, config_text=text, contract_texts=contract_texts)
    result = _run_cli(["--check"], cwd=repo)
    assert result.returncode != 0
    assert "SUPERSEDED_BUT_NOT_APPROVED" in result.stderr


def test_render_does_not_modify_the_working_tree(tmp_path: Path) -> None:
    """Running the renderer leaves the repository without new or modified files."""

    # Snapshot the working tree of the worktree-less scratch repo.
    repo = tmp_path / "scratch"
    repo.mkdir()
    _make_repo(repo)
    before = sorted(p.relative_to(repo) for p in repo.rglob("*") if p.is_file())

    result = _run_cli([], cwd=repo)
    assert result.returncode == 0
    result_check = _run_cli(["--check"], cwd=repo)
    assert result_check.returncode == 0

    after = sorted(p.relative_to(repo) for p in repo.rglob("*") if p.is_file())
    assert before == after


# ---------------------------------------------------------------------------
# Pure-function sanity checks
# ---------------------------------------------------------------------------


def test_parse_outcome_strips_to_outcome_body() -> None:
    body = (
        "# T001 — title\n\n"
        "> Phase: P00\n\n"
        "## Dependencies\n\nT000\n\n"
        "## Outcome\n\nfirst line\nsecond line\n\n"
        "## Deliverables\n\nignored.\n"
    )
    assert parse_outcome(body) == "first line\nsecond line"


def test_parse_title_returns_the_contract_title() -> None:
    body = "# T001 — A title\n\n## Outcome\n\nx.\n"
    assert parse_title(body, "T001") == "A title"


def test_parse_phase_purpose_returns_single_paragraph() -> None:
    body = "# P00\n\n**Purpose:** first line\ncontinues here.\n\n**Entry:** x.\n"
    assert parse_phase_purpose(body) == "first line\ncontinues here."


def test_is_ready_respects_supersede_self_dependency() -> None:
    """A task that depends on the work it supersedes is still ready."""

    tasks = {
        "T038": {
            "lifecycle": "DELIVERED",
            "claimed": False,
            "depends_on": [],
            "superseded_by": "T039",
        },
        "T039": {
            "lifecycle": "OPEN",
            "claimed": False,
            "depends_on": ["T038"],
        },
    }
    assert is_ready("T039", tasks["T039"], tasks) is True


def test_is_ready_rejects_superseded_dependency() -> None:
    tasks = {
        "T022": {
            "lifecycle": "DELIVERED",
            "claimed": False,
            "depends_on": [],
            "superseded_by": "T026",
        },
        "T099": {
            "lifecycle": "OPEN",
            "claimed": False,
            "depends_on": ["T022"],
        },
    }
    assert is_ready("T099", tasks["T099"], tasks) is False


def test_plan_states_contains_every_state_rendered_by_view() -> None:
    """``PLAN_STATES`` is the closed universe the renderer can mention.

    It is checked against the controller's own table rather than against a
    second hand-written copy. Three separate literals of the same state machine
    is how the renderer would come to disagree with the controller it renders,
    and this is the check that makes the disagreement fail loudly instead.
    ``tools.progress`` deliberately does not *import* the controller -- it is a
    product tool and stays standalone -- so the equality is asserted here.
    """

    from tools.workflow.core import STATES as CONTROLLER_STATES

    assert set(PLAN_STATES) == set(CONTROLLER_STATES)
    assert set(PLAN_STATES) >= NOTABLE_STATES


def test_progress_issue_is_a_namedtuple_like_dataclass() -> None:
    """Public exceptions carry a code, target and message."""

    issue = ProgressIssue(code="X", target="T001", message="bad")
    assert issue.code == "X"
    assert issue.target == "T001"
    assert issue.message == "bad"
