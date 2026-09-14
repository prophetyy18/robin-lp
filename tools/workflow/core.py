"""Git- and commit-based workflow gates for one-task agent execution.

The language model reasons about a task. This module owns only mechanical
constraints: task state, dependency gates, protected paths, exact Git SHAs,
worktree creation, structured result validation and review persistence.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TASK_PATTERN = re.compile(r"^T[0-9]{3}$")
PHASE_PATTERN = re.compile(r"^P[0-9]{2}$")
REQUIRED_AGENT_MODEL = "MiniMax-M3[1m]"

STATES = {
    "PLANNED",
    "READY",
    "IN_DEVELOPMENT",
    "AWAITING_REVIEW",
    "CHANGES_REQUESTED",
    "TRIAGE_REQUIRED",
    "PLANNING",
    "AWAITING_PLAN_REVIEW",
    "PLAN_REVIEW_BLOCKED",
    "OWNER_DECISION_REQUIRED",
    "BLOCKED",
    "APPROVED",
}

ALLOWED_TRANSITIONS = {
    "PLANNED": {"READY"},
    "READY": {"IN_DEVELOPMENT", "BLOCKED"},
    "IN_DEVELOPMENT": {"AWAITING_REVIEW", "TRIAGE_REQUIRED", "BLOCKED"},
    "AWAITING_REVIEW": {
        "APPROVED",
        "CHANGES_REQUESTED",
        "TRIAGE_REQUIRED",
        "BLOCKED",
    },
    "CHANGES_REQUESTED": {"IN_DEVELOPMENT", "BLOCKED"},
    "TRIAGE_REQUIRED": {
        "CHANGES_REQUESTED",
        "PLANNING",
        "OWNER_DECISION_REQUIRED",
        "BLOCKED",
    },
    "PLANNING": {"AWAITING_PLAN_REVIEW", "OWNER_DECISION_REQUIRED", "BLOCKED"},
    "AWAITING_PLAN_REVIEW": {"CHANGES_REQUESTED", "PLANNING", "PLAN_REVIEW_BLOCKED"},
    "PLAN_REVIEW_BLOCKED": {"CHANGES_REQUESTED", "PLANNING", "PLAN_REVIEW_BLOCKED"},
    "OWNER_DECISION_REQUIRED": {"PLANNING", "BLOCKED"},
    "BLOCKED": {"READY"},
    "APPROVED": set(),
}

PROTECTED_PREFIXES = (
    ".claude/",
    "docs/intent/",
    "docs/spec/",
    "todo/phases/",
    "todo/schemas/",
    "tools/workflow/",
)
PROTECTED_FILES = {"AGENTS.md", "CLAUDE.md", "todo/README.md", "todo/config.yaml"}


class WorkflowError(RuntimeError):
    """Raised when a workflow invariant would be violated."""


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class AttemptRecord:
    task_id: str
    phase: str
    attempt: int
    base_commit: str
    candidate_commit: str | None
    branch: str
    development_worktree: str

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "phase": self.phase,
            "attempt": self.attempt,
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "branch": self.branch,
            "development_worktree": self.development_worktree,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AttemptRecord:
        return cls(
            task_id=_required_string(value, "task_id"),
            phase=_required_string(value, "phase"),
            attempt=_required_int(value, "attempt"),
            base_commit=_required_string(value, "base_commit"),
            candidate_commit=_optional_string(value, "candidate_commit"),
            branch=_required_string(value, "branch"),
            development_worktree=_required_string(value, "development_worktree"),
        )


@dataclass(frozen=True)
class PlanRecord:
    task_id: str
    attempt: int
    classification: str
    base_commit: str
    candidate_commit: str

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "attempt": self.attempt,
            "classification": self.classification,
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PlanRecord:
        return cls(
            task_id=_required_string(value, "task_id"),
            attempt=_required_int(value, "attempt"),
            classification=_required_string(value, "classification"),
            base_commit=_required_string(value, "base_commit"),
            candidate_commit=_required_string(value, "candidate_commit"),
        )


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise WorkflowError(f"{key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise WorkflowError(f"{key} must be null or a non-empty string")
    return item


def _required_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise WorkflowError(f"{key} must be an integer")
    return item


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: int | None = None,
) -> CommandResult:
    process = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    result = CommandResult(tuple(args), process.stdout, process.stderr, process.returncode)
    if check and process.returncode != 0:
        command = " ".join(args)
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        raise WorkflowError(f"command failed ({process.returncode}): {command}\n{detail}")
    return result


def _git(repo: Path, *args: str, check: bool = True) -> CommandResult:
    return _run(("git", *args), cwd=repo, check=check)


def _sha(repo: Path, revision: str = "HEAD") -> str:
    value = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").stdout.strip()
    if not SHA_PATTERN.fullmatch(value):
        raise WorkflowError(f"Git returned an invalid commit SHA for {revision}: {value!r}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"cannot load JSON-compatible YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"{path} must contain an object")
    return value


def _snapshot(paths: Sequence[Path], root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _protected_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in PROTECTED_FILES or any(
            relative.startswith(item) for item in PROTECTED_PREFIXES
        ):
            paths.append(path)
    return sorted(paths)


def _working_tree_changes(root: Path) -> list[str]:
    tracked = _git(root, "diff", "--name-only").stdout.splitlines()
    staged = _git(root, "diff", "--cached", "--name-only").stdout.splitlines()
    untracked = _git(root, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    return sorted(set(tracked + staged + untracked))


def _without_planner_owned_config_fields(
    config: Mapping[str, Any], classification: str
) -> dict[str, Any]:
    comparable = cast(dict[str, Any], json.loads(json.dumps(config)))
    for task in comparable["tasks"].values():
        task.pop("depends_on", None)
    if classification in {"SPEC_DEFECT", "OWNER_DECISION_REQUIRED"}:
        comparable.pop("spec_revision", None)
    if classification == "OWNER_DECISION_REQUIRED":
        comparable.pop("intent_revision", None)
    return comparable


def _render_review(result: Mapping[str, Any]) -> str:
    lines = [
        f"# {result['task_id']} independent review",
        "",
        f"- Base commit: `{result['base_commit']}`",
        f"- Candidate commit: `{result['candidate_commit']}`",
        f"- Verdict: **{result['verdict']}**",
        "",
        "## Checks",
        "",
    ]
    for check in result["checks"]:
        lines.append(f"### {check['id']} — {check['status']}")
        lines.append("")
        lines.append(check["finding"] or "No additional finding.")
        lines.append("")
        lines.append("Evidence:")
        lines.append("")
        evidence = check["evidence"] or ["None recorded."]
        lines.extend(f"- {item}" for item in evidence)
        lines.append("")
    for key, title in (
        ("must_not_violations", "Must-not violations"),
        ("unknowns", "Unknowns"),
        ("required_changes", "Required changes"),
        ("residual_risks", "Residual risks"),
    ):
        lines.extend([f"## {title}", ""])
        values = result.get(key, []) or ["None."]
        lines.extend(f"- {item}" for item in values)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_plan_review(result: Mapping[str, Any]) -> str:
    lines = [
        f"# {result['task_id']} planning review",
        "",
        f"- Base commit: `{result['base_commit']}`",
        f"- Candidate commit: `{result['candidate_commit']}`",
        f"- Verdict: **{result['verdict']}**",
        "",
        "## Summary",
        "",
        result["summary"] or "No summary supplied.",
        "",
    ]
    for key, title in (("required_changes", "Required changes"), ("unknowns", "Unknowns")):
        lines.extend([f"## {title}", ""])
        values = result.get(key, []) or ["None."]
        lines.extend(f"- {item}" for item in values)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class WorkflowManager:
    """Run one developer and one reviewer at a time against immutable commits."""

    def __init__(
        self,
        repo: Path,
        *,
        worktree_root: Path | None = None,
    ) -> None:
        self.repo = repo.resolve()
        discovered = Path(_git(self.repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if discovered != self.repo:
            raise WorkflowError(f"run from repository root {discovered}")
        self.worktree_root = (
            worktree_root or self.repo.parent / f"{self.repo.name}-worktrees"
        ).resolve()
        common_git = _git(self.repo, "rev-parse", "--git-common-dir").stdout.strip()
        common_git_path = Path(common_git)
        if not common_git_path.is_absolute():
            common_git_path = self.repo / common_git_path
        self.runtime_dir = common_git_path.resolve() / "robinhood-lp-workflow"

    @property
    def config_path(self) -> Path:
        return self.repo / "todo" / "config.yaml"

    def load_config(self, root: Path | None = None) -> dict[str, Any]:
        base = root or self.repo
        config = _load_json(base / "todo" / "config.yaml")
        self.validate_config(config, base)
        return config

    def validate_config(self, config: Mapping[str, Any], root: Path | None = None) -> None:
        base = root or self.repo
        if config.get("schema_version") != 1 or config.get("project") != "robinhood-lp-v1":
            raise WorkflowError("unsupported todo/config.yaml schema or project")
        baseline = config.get("product_baseline_commit")
        if not isinstance(baseline, str) or not SHA_PATTERN.fullmatch(baseline):
            raise WorkflowError("product_baseline_commit must be a full lowercase Git SHA")
        runtime = config.get("agent_runtime")
        if not isinstance(runtime, dict):
            raise WorkflowError("agent_runtime must be an object")
        if runtime.get("provider") != "MiniMax Anthropic-compatible API":
            raise WorkflowError("agent_runtime.provider must select MiniMax")
        if runtime.get("model") != REQUIRED_AGENT_MODEL:
            raise WorkflowError(f"agent_runtime.model must be {REQUIRED_AGENT_MODEL}")
        if runtime.get("context_window_tokens") != 1_000_000:
            raise WorkflowError("agent_runtime.context_window_tokens must be 1000000")
        if runtime.get("allow_model_fallback") is not False:
            raise WorkflowError("agent_runtime must prohibit model fallback")
        tasks = config.get("tasks")
        if not isinstance(tasks, dict) or not tasks:
            raise WorkflowError("tasks must be a non-empty object")
        for task_id, raw in tasks.items():
            if not isinstance(task_id, str) or not TASK_PATTERN.fullmatch(task_id):
                raise WorkflowError(f"invalid task ID: {task_id!r}")
            if not isinstance(raw, dict):
                raise WorkflowError(f"{task_id} record must be an object")
            phase = raw.get("phase")
            status = raw.get("status")
            dependencies = raw.get("depends_on")
            task_file = raw.get("task_file")
            if not isinstance(phase, str) or not PHASE_PATTERN.fullmatch(phase):
                raise WorkflowError(f"{task_id} has invalid phase")
            if status not in STATES:
                raise WorkflowError(f"{task_id} has invalid status {status!r}")
            if not isinstance(dependencies, list) or len(set(dependencies)) != len(dependencies):
                raise WorkflowError(f"{task_id} dependencies must be a unique list")
            for dependency in dependencies:
                if dependency not in tasks:
                    raise WorkflowError(f"{task_id} depends on unknown task {dependency}")
            if not isinstance(task_file, str) or not (base / task_file).is_file():
                raise WorkflowError(f"{task_id} task file does not exist: {task_file!r}")
            if raw.get("attempt") is None or not isinstance(raw.get("attempt"), int):
                raise WorkflowError(f"{task_id} attempt must be an integer")
            for key in ("base_commit", "candidate_commit", "approved_commit"):
                value = raw.get(key)
                if value is not None and (
                    not isinstance(value, str) or not SHA_PATTERN.fullmatch(value)
                ):
                    raise WorkflowError(f"{task_id}.{key} must be null or a full Git SHA")
        active_task = config.get("active_task")
        if active_task is not None:
            if active_task not in tasks:
                raise WorkflowError("active_task does not exist")
            if config.get("active_phase") != tasks[active_task]["phase"]:
                raise WorkflowError("active_phase does not match active_task")
            if config.get("workflow_state") != tasks[active_task]["status"]:
                raise WorkflowError("workflow_state does not match active task status")
        active_states = [
            task_id
            for task_id, task in tasks.items()
            if task["status"] not in {"PLANNED", "APPROVED"}
        ]
        if active_states and active_states != (
            [active_task] if isinstance(active_task, str) else []
        ):
            raise WorkflowError("exactly the active task may have an in-progress or READY state")
        self._validate_acyclic(tasks)

    def _validate_acyclic(self, tasks: Mapping[str, Any]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise WorkflowError(f"dependency cycle includes {task_id}")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in tasks[task_id]["depends_on"]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)

    def status(self) -> dict[str, object]:
        config = self.load_config()
        active = config.get("active_task")
        runtime = self.load_attempt(active) if isinstance(active, str) else None
        if runtime is not None and Path(runtime.development_worktree).is_dir():
            config = self.load_config(Path(runtime.development_worktree))
        active_plan = self.load_plan(active) if isinstance(active, str) else None
        return {
            "active_phase": config.get("active_phase"),
            "active_task": active,
            "workflow_state": config.get("workflow_state"),
            "task_status": config["tasks"][active]["status"] if active else None,
            "attempt": runtime.to_dict() if runtime else None,
            "plan": active_plan.to_dict() if active_plan else None,
        }

    def validate_repository(self) -> None:
        self.load_config()
        for agent in (
            "workflow-manager",
            "stage-developer",
            "stage-reviewer",
            "issue-triager",
            "planner",
            "plan-reviewer",
        ):
            if not (self.repo / ".claude" / "agents" / f"{agent}.md").is_file():
                raise WorkflowError(f"{agent} agent is missing")
        for name in (
            "config",
            "developer-result",
            "review-result",
            "triage-result",
            "planner-result",
            "plan-review-result",
        ):
            _load_json(self.repo / "todo" / "schemas" / f"{name}.schema.json")

    def _ensure_clean_main(self) -> None:
        status = _git(self.repo, "status", "--porcelain").stdout
        if status:
            raise WorkflowError("main checkout must be clean before starting or approving a task")

    def _task(self, config: Mapping[str, Any], task_id: str) -> dict[str, Any]:
        if not TASK_PATTERN.fullmatch(task_id):
            raise WorkflowError(f"invalid task ID {task_id!r}")
        raw = config["tasks"].get(task_id)
        if not isinstance(raw, dict):
            raise WorkflowError(f"unknown task {task_id}")
        return raw

    def _check_dependencies(self, config: Mapping[str, Any], task_id: str) -> None:
        task = self._task(config, task_id)
        incomplete = [
            dependency
            for dependency in task["depends_on"]
            if config["tasks"][dependency]["status"] != "APPROVED"
        ]
        if incomplete:
            raise WorkflowError(f"{task_id} has unapproved dependencies: {', '.join(incomplete)}")

    def ready(self, task_id: str) -> str:
        self._ensure_clean_main()
        config = self.load_config()
        task = self._task(config, task_id)
        if task["status"] != "PLANNED":
            raise WorkflowError(f"ready requires PLANNED, found {task['status']}")
        active = config.get("active_task")
        if isinstance(active, str) and config["tasks"][active]["status"] not in {
            "APPROVED",
            "PLANNED",
        }:
            raise WorkflowError(f"cannot activate {task_id} while {active} is unfinished")
        self._check_dependencies(config, task_id)
        self._set_state(config, task_id, "READY")
        _write_json(self.config_path, config)
        _git(self.repo, "add", "todo/config.yaml")
        _git(self.repo, "commit", "-m", f"chore(workflow): mark {task_id} ready")
        return _sha(self.repo)

    def _attempt_path(self, task_id: str) -> Path:
        return self.runtime_dir / f"{task_id}.json"

    def _plan_path(self, task_id: str) -> Path:
        return self.runtime_dir / f"{task_id}-plan.json"

    def _protected_snapshot_path(self, task_id: str) -> Path:
        return self.runtime_dir / f"{task_id}-protected.json"

    def save_attempt(self, attempt: AttemptRecord) -> None:
        _write_json(self._attempt_path(attempt.task_id), attempt.to_dict())

    def load_attempt(self, task_id: str | None) -> AttemptRecord | None:
        if task_id is None:
            return None
        path = self._attempt_path(task_id)
        if not path.is_file():
            return None
        return AttemptRecord.from_dict(_load_json(path))

    def save_plan(self, plan: PlanRecord) -> None:
        _write_json(self._plan_path(plan.task_id), plan.to_dict())

    def load_plan(self, task_id: str) -> PlanRecord | None:
        path = self._plan_path(task_id)
        if not path.is_file():
            return None
        return PlanRecord.from_dict(_load_json(path))

    def _set_state(
        self,
        config: dict[str, Any],
        task_id: str,
        state: str,
        **updates: object,
    ) -> None:
        if state not in STATES:
            raise WorkflowError(f"invalid target state {state}")
        task = self._task(config, task_id)
        current = task["status"]
        if state != current and state not in ALLOWED_TRANSITIONS[current]:
            raise WorkflowError(f"illegal state transition for {task_id}: {current} -> {state}")
        task["status"] = state
        task.update(updates)
        config["active_phase"] = task["phase"]
        config["active_task"] = task_id
        config["workflow_state"] = state

    def prepare_develop(self, task_id: str, *, retry: bool = False) -> dict[str, object]:
        """Prepare an isolated worktree without hiding the Claude Code agent run."""
        if retry:
            attempt = self.load_attempt(task_id)
            if attempt is None:
                raise WorkflowError(f"no retained attempt for {task_id}")
            worktree = Path(attempt.development_worktree)
            config = self.load_config(worktree)
            task = self._task(config, task_id)
            if task["status"] == "BLOCKED":
                self._set_state(config, task_id, "READY")
            elif task["status"] != "CHANGES_REQUESTED":
                raise WorkflowError(
                    f"retry requires CHANGES_REQUESTED or resolved BLOCKED, found {task['status']}"
                )
            attempt = AttemptRecord(
                task_id=attempt.task_id,
                phase=attempt.phase,
                attempt=attempt.attempt + 1,
                base_commit=attempt.base_commit,
                candidate_commit=None,
                branch=attempt.branch,
                development_worktree=attempt.development_worktree,
            )
        else:
            self._ensure_clean_main()
            config = self.load_config()
            task = self._task(config, task_id)
            if task["status"] != "READY":
                raise WorkflowError(f"develop requires READY, found {task['status']}")
            self._check_dependencies(config, task_id)
            if self.load_attempt(task_id) is not None:
                raise WorkflowError(f"runtime record already exists for {task_id}")
            base = _sha(self.repo)
            attempt_number = int(task["attempt"]) + 1
            branch = f"workflow/{task_id.lower()}-attempt-{attempt_number:03d}"
            worktree = self.worktree_root / f"dev-{task_id.lower()}-attempt-{attempt_number:03d}"
            worktree.parent.mkdir(parents=True, exist_ok=True)
            if worktree.exists():
                raise WorkflowError(f"development worktree path already exists: {worktree}")
            _git(self.repo, "worktree", "add", "-b", branch, str(worktree), base)
            attempt = AttemptRecord(
                task_id=task_id,
                phase=task["phase"],
                attempt=attempt_number,
                base_commit=base,
                candidate_commit=None,
                branch=branch,
                development_worktree=str(worktree),
            )

        self._set_state(config, task_id, "IN_DEVELOPMENT", attempt=attempt.attempt)
        _write_json(worktree / "todo" / "config.yaml", config)
        protected = _protected_paths(worktree)
        before = _snapshot(protected, worktree)
        self.save_attempt(attempt)
        _write_json(self._protected_snapshot_path(task_id), before)
        task_file = config["tasks"][task_id]["task_file"]
        prompt = (
            f"Implement exactly {task_id}. The frozen contract is {task_file}. "
            f"The approved base is {attempt.base_commit}. This is attempt {attempt.attempt}. "
            f"Work only in {worktree}. Do not commit. Before finishing, write the required "
            f"structured developer result to {worktree / '.workflow' / 'developer-result.json'}."
        )
        return {**attempt.to_dict(), "agent": "stage-developer", "prompt": prompt}

    def finish_develop(self, task_id: str) -> AttemptRecord:
        """Validate and seal a visible stage-developer run."""
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no prepared development attempt")
        worktree = Path(attempt.development_worktree)
        result_path = worktree / ".workflow" / "developer-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"developer result is missing: {result_path}")
        result = _load_json(result_path)
        before = _load_json(self._protected_snapshot_path(task_id))
        return self._finish_develop(attempt, result, before)

    def _finish_develop(
        self,
        attempt: AttemptRecord,
        result: Mapping[str, Any],
        before: Mapping[str, Any],
    ) -> AttemptRecord:
        task_id = attempt.task_id
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        self._validate_developer_result(result, task_id)
        (worktree / ".workflow" / "developer-result.json").unlink(missing_ok=True)
        after = _snapshot(_protected_paths(worktree), worktree)
        if before != after:
            raise WorkflowError(
                "developer modified a protected workflow, Intent, Spec, or task file"
            )

        evidence_path = (
            worktree
            / "todo"
            / "evidence"
            / attempt.phase
            / task_id
            / f"attempt-{attempt.attempt:03d}-developer.json"
        )
        _write_json(evidence_path, result)
        outcome = result["outcome"]
        if outcome != "CANDIDATE_READY":
            state = {
                "TRIAGE_REQUIRED": "TRIAGE_REQUIRED",
                "BLOCKED": "BLOCKED",
            }[outcome]
            self._set_state(config, task_id, state, base_commit=attempt.base_commit)
            _write_json(worktree / "todo" / "config.yaml", config)
            _git(worktree, "add", "-A")
            _git(worktree, "commit", "-m", f"chore(workflow): record {task_id} {state.lower()}")
            self.save_attempt(attempt)
            return attempt

        self._set_state(config, task_id, "AWAITING_REVIEW", base_commit=attempt.base_commit)
        _write_json(worktree / "todo" / "config.yaml", config)
        _git(worktree, "diff", "--check")
        changed = _git(worktree, "status", "--porcelain").stdout.strip()
        if not changed:
            raise WorkflowError("developer produced no candidate changes")
        _git(worktree, "add", "-A")
        _git(
            worktree,
            "commit",
            "-m",
            f"feat({task_id.lower()}): candidate attempt {attempt.attempt}",
        )
        candidate = _sha(worktree)
        attempt = AttemptRecord(
            task_id=attempt.task_id,
            phase=attempt.phase,
            attempt=attempt.attempt,
            base_commit=attempt.base_commit,
            candidate_commit=candidate,
            branch=attempt.branch,
            development_worktree=attempt.development_worktree,
        )
        self.save_attempt(attempt)
        self._protected_snapshot_path(task_id).unlink(missing_ok=True)
        return attempt

    def _validate_developer_result(self, result: Mapping[str, Any], task_id: str) -> None:
        if result.get("task_id") != task_id:
            raise WorkflowError("developer result task_id does not match")
        outcome = result.get("outcome")
        if outcome not in {"CANDIDATE_READY", "TRIAGE_REQUIRED", "BLOCKED"}:
            raise WorkflowError("developer result has invalid outcome")
        if not isinstance(result.get("summary"), str):
            raise WorkflowError("developer result summary must be a string")
        if not isinstance(result.get("commands"), list):
            raise WorkflowError("developer result commands must be a list")
        if not isinstance(result.get("residual_risks"), list):
            raise WorkflowError("developer result residual_risks must be a list")
        if outcome == "TRIAGE_REQUIRED":
            self._validate_triage_request(result.get("triage_request"))

    def _validate_triage_request(self, value: object) -> None:
        if not isinstance(value, dict):
            raise WorkflowError("TRIAGE_REQUIRED must include triage_request")
        if not isinstance(value.get("observed_problem"), str) or not value["observed_problem"]:
            raise WorkflowError("triage_request must describe the observed problem")
        if not isinstance(value.get("evidence"), list):
            raise WorkflowError("triage_request evidence must be a list")
        classifications = {
            "IMPLEMENTATION_DEFECT",
            "CONTRACT_MISMATCH",
            "SPEC_DEFECT",
            "OWNER_DECISION_REQUIRED",
            "EXTERNAL_BLOCKED",
        }
        if value.get("proposed_classification") not in classifications:
            raise WorkflowError("triage_request proposed classification is invalid")
        if not isinstance(value.get("requested_change"), str):
            raise WorkflowError("triage_request requested_change must be a string")

    def _validate_review_result(self, result: Mapping[str, Any], attempt: AttemptRecord) -> str:
        if result.get("task_id") != attempt.task_id:
            raise WorkflowError("review task_id does not match")
        if result.get("base_commit") != attempt.base_commit:
            raise WorkflowError("review base_commit does not match")
        if result.get("candidate_commit") != attempt.candidate_commit:
            raise WorkflowError("review candidate_commit does not match")
        verdict = result.get("verdict")
        if verdict not in {"PASS", "FAIL", "TRIAGE_REQUIRED", "BLOCKED"}:
            raise WorkflowError("review verdict is invalid")
        checks = result.get("checks")
        if not isinstance(checks, list) or not checks:
            raise WorkflowError("review must contain at least one check")
        statuses = []
        for check in checks:
            if not isinstance(check, dict) or check.get("status") not in {
                "PASS",
                "FAIL",
                "UNKNOWN",
            }:
                raise WorkflowError("review contains an invalid check")
            statuses.append(check["status"])
        violations = result.get("must_not_violations")
        unknowns = result.get("unknowns")
        required = result.get("required_changes")
        if not all(isinstance(value, list) for value in (violations, unknowns, required)):
            raise WorkflowError("review finding collections must be lists")
        if verdict == "TRIAGE_REQUIRED":
            self._validate_triage_request(result.get("triage_request"))
            return "TRIAGE_REQUIRED"
        mechanically_passes = (
            verdict == "PASS"
            and set(statuses) == {"PASS"}
            and not violations
            and not unknowns
            and not required
        )
        if verdict == "PASS" and not mechanically_passes:
            raise WorkflowError("PASS contradicts failed/unknown checks or unresolved findings")
        if mechanically_passes:
            return "APPROVED"
        if verdict == "BLOCKED" or "UNKNOWN" in statuses or unknowns:
            return "BLOCKED"
        return "CHANGES_REQUESTED"

    def prepare_review(self, task_id: str) -> dict[str, object]:
        """Create the exact detached review worktree without launching Claude."""
        attempt = self.load_attempt(task_id)
        if attempt is None or attempt.candidate_commit is None:
            raise WorkflowError(f"{task_id} has no candidate commit")
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        if self._task(config, task_id)["status"] != "AWAITING_REVIEW":
            raise WorkflowError("review requires AWAITING_REVIEW")
        if _sha(worktree) != attempt.candidate_commit:
            raise WorkflowError("development worktree HEAD no longer matches candidate commit")
        if _git(worktree, "status", "--porcelain").stdout:
            raise WorkflowError("development worktree must be clean before review")

        review_worktree = (
            self.worktree_root / f"review-{task_id.lower()}-attempt-{attempt.attempt:03d}"
        )
        if review_worktree.exists():
            raise WorkflowError(f"review worktree path already exists: {review_worktree}")
        _git(
            self.repo, "worktree", "add", "--detach", str(review_worktree), attempt.candidate_commit
        )
        task_file = config["tasks"][task_id]["task_file"]
        prompt = (
            f"Independently review exactly {task_id} using {task_file}. "
            f"Base commit: {attempt.base_commit}. Candidate commit: {attempt.candidate_commit}. "
            f"Work only in {review_worktree}. Inspect the full diff and run every obtainable "
            f"acceptance check. Do not fix anything. Write the structured review result only to "
            f"{review_worktree / '.workflow' / 'review-result.json'} before finishing."
        )
        return {
            **attempt.to_dict(),
            "agent": "stage-reviewer",
            "review_worktree": str(review_worktree),
            "prompt": prompt,
        }

    def finish_review(self, task_id: str) -> tuple[str, Path]:
        """Validate and record a visible stage-reviewer run."""
        attempt = self.load_attempt(task_id)
        if attempt is None or attempt.candidate_commit is None:
            raise WorkflowError(f"{task_id} has no candidate commit")
        review_worktree = (
            self.worktree_root / f"review-{task_id.lower()}-attempt-{attempt.attempt:03d}"
        )
        result_path = review_worktree / ".workflow" / "review-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"review result is missing: {result_path}")
        result = _load_json(result_path)
        state = self._validate_review_result(result, attempt)
        result_path.unlink()
        reviewer_changes = _git(review_worktree, "status", "--porcelain").stdout.strip()
        if reviewer_changes:
            raise WorkflowError("reviewer left changes outside its result handoff")
        if _sha(review_worktree) != attempt.candidate_commit:
            raise WorkflowError("review worktree no longer matches candidate commit")
        _git(self.repo, "worktree", "remove", str(review_worktree), check=False)

        return self._record_review_result(attempt, result, state)

    def _record_review_result(
        self, attempt: AttemptRecord, result: Mapping[str, Any], state: str
    ) -> tuple[str, Path]:
        task_id = attempt.task_id
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)

        relative_json = (
            Path("todo")
            / "reviews"
            / attempt.phase
            / task_id
            / f"review-{attempt.attempt:03d}.json"
        )
        relative_md = relative_json.with_suffix(".md")
        _write_json(worktree / relative_json, result)
        (worktree / relative_md).write_text(_render_review(result), encoding="utf-8")
        updates: dict[str, object] = {
            "base_commit": attempt.base_commit,
            "candidate_commit": attempt.candidate_commit,
            "latest_review": relative_json.as_posix(),
        }
        if state == "APPROVED":
            updates["approved_commit"] = attempt.candidate_commit
        self._set_state(config, task_id, state, **updates)
        _write_json(worktree / "todo" / "config.yaml", config)
        _git(worktree, "add", "todo/config.yaml", str(relative_json), str(relative_md))
        _git(
            worktree, "commit", "-m", f"chore(workflow): record {task_id} review {attempt.attempt}"
        )

        if state == "APPROVED":
            self._ensure_clean_main()
            _git(self.repo, "merge", "--ff-only", attempt.branch)
            _git(self.repo, "worktree", "remove", attempt.development_worktree)
            _git(self.repo, "branch", "-d", attempt.branch)
            self._attempt_path(task_id).unlink(missing_ok=True)
            report_path = self.repo / relative_md
        else:
            self.save_attempt(attempt)
            report_path = worktree / relative_md
        return state, report_path

    def _triage_report_path(self, attempt: AttemptRecord) -> Path:
        return (
            Path("todo")
            / "triage"
            / attempt.phase
            / attempt.task_id
            / f"triage-{attempt.attempt:03d}.json"
        )

    def prepare_triage(self, task_id: str) -> dict[str, object]:
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no retained attempt")
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        if self._task(config, task_id)["status"] != "TRIAGE_REQUIRED":
            raise WorkflowError("triage requires TRIAGE_REQUIRED")
        if _git(worktree, "status", "--porcelain").stdout:
            raise WorkflowError("development worktree must be clean before triage")
        issue_commit = _sha(worktree)
        triage_worktree = (
            self.worktree_root / f"triage-{task_id.lower()}-attempt-{attempt.attempt:03d}"
        )
        if triage_worktree.exists():
            raise WorkflowError(f"triage worktree path already exists: {triage_worktree}")
        _git(self.repo, "worktree", "add", "--detach", str(triage_worktree), issue_commit)
        prompt = (
            f"Classify the exceptional issue for {task_id}. Issue commit: {issue_commit}. "
            f"Task contract: {config['tasks'][task_id]['task_file']}. Work only in "
            f"{triage_worktree}. Use the recorded triage_request, verify it, and write the "
            f"structured result only to {triage_worktree / '.workflow' / 'triage-result.json'}."
        )
        return {
            **attempt.to_dict(),
            "agent": "issue-triager",
            "issue_commit": issue_commit,
            "triage_worktree": str(triage_worktree),
            "prompt": prompt,
        }

    def finish_triage(self, task_id: str) -> tuple[str, Path]:
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no retained attempt")
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        issue_commit = _sha(worktree)
        triage_worktree = (
            self.worktree_root / f"triage-{task_id.lower()}-attempt-{attempt.attempt:03d}"
        )
        result_path = triage_worktree / ".workflow" / "triage-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"triage result is missing: {result_path}")
        result = _load_json(result_path)
        classification = self._validate_triage_result(result, task_id, issue_commit)
        result_path.unlink()
        if _git(triage_worktree, "status", "--porcelain").stdout.strip():
            raise WorkflowError("triager left changes outside its result handoff")
        _git(self.repo, "worktree", "remove", str(triage_worktree), check=False)

        state = {
            "IMPLEMENTATION_DEFECT": "CHANGES_REQUESTED",
            "CONTRACT_MISMATCH": "PLANNING",
            "SPEC_DEFECT": "PLANNING",
            "OWNER_DECISION_REQUIRED": "OWNER_DECISION_REQUIRED",
            "EXTERNAL_BLOCKED": "BLOCKED",
        }[classification]
        report = self._triage_report_path(attempt)
        _write_json(worktree / report, result)
        self._set_state(config, task_id, state)
        _write_json(worktree / "todo" / "config.yaml", config)
        _git(worktree, "add", "todo/config.yaml", str(report))
        _git(worktree, "commit", "-m", f"chore(workflow): triage {task_id} as {classification}")
        self.save_attempt(attempt)
        return state, worktree / report

    def _validate_triage_result(
        self, result: Mapping[str, Any], task_id: str, issue_commit: str
    ) -> str:
        if result.get("task_id") != task_id or result.get("issue_commit") != issue_commit:
            raise WorkflowError("triage result identity or issue_commit does not match")
        classification = result.get("classification")
        allowed = {
            "IMPLEMENTATION_DEFECT",
            "CONTRACT_MISMATCH",
            "SPEC_DEFECT",
            "OWNER_DECISION_REQUIRED",
            "EXTERNAL_BLOCKED",
        }
        if not isinstance(classification, str) or classification not in allowed:
            raise WorkflowError("triage result classification is invalid")
        if not isinstance(result.get("summary"), str) or not result["summary"]:
            raise WorkflowError("triage result summary must be non-empty")
        if not isinstance(result.get("evidence"), list):
            raise WorkflowError("triage result evidence must be a list")
        if classification == "OWNER_DECISION_REQUIRED" and not isinstance(
            result.get("owner_question"), str
        ):
            raise WorkflowError("owner decision triage must include owner_question")
        return classification

    def prepare_plan(self, task_id: str, *, owner_decision: str | None = None) -> dict[str, object]:
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no retained attempt")
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        task = self._task(config, task_id)
        if task["status"] not in {"PLANNING", "OWNER_DECISION_REQUIRED"}:
            raise WorkflowError("plan requires PLANNING or OWNER_DECISION_REQUIRED")
        if _git(worktree, "status", "--porcelain").stdout:
            raise WorkflowError("development worktree must be clean before planning")
        triage_path = self._triage_report_path(attempt)
        triage_result = _load_json(worktree / triage_path)
        classification = _required_string(triage_result, "classification")
        owner_route = task["status"] == "OWNER_DECISION_REQUIRED"
        if owner_route:
            if not owner_decision or not owner_decision.strip():
                raise WorkflowError("planning this issue requires the owner's explicit decision")
            decision_path = triage_path.with_name(f"owner-decision-{attempt.attempt:03d}.json")
            _write_json(
                worktree / decision_path,
                {"task_id": task_id, "decision": owner_decision.strip()},
            )
            self._set_state(config, task_id, "PLANNING")
            _write_json(worktree / "todo" / "config.yaml", config)
            _git(worktree, "add", "todo/config.yaml", str(decision_path))
            _git(worktree, "commit", "-m", f"chore(workflow): record {task_id} owner decision")
        elif owner_decision is not None:
            raise WorkflowError("owner_decision is accepted only for OWNER_DECISION_REQUIRED")

        if owner_route:
            classification = "OWNER_DECISION_REQUIRED"

        plan_base = _sha(worktree)
        task_file = config["tasks"][task_id]["task_file"]
        prompt = (
            f"Resolve the triaged issue for exactly {task_id}. Classification: {classification}. "
            f"Triage report: {triage_path}. Task contract: {task_file}. "
            f"Planning base commit: {plan_base}. "
            + (
                f"The owner's recorded decision is: {owner_decision.strip()}. "
                if owner_decision
                else ""
            )
            + f"Work only in {worktree}. Change only paths allowed for this classification. "
            f"A no-change result is valid. Write the structured result to "
            f"{worktree / '.workflow' / 'planner-result.json'}."
        )
        _write_json(
            self.runtime_dir / f"{task_id}-planning-context.json",
            {"classification": classification, "plan_base": plan_base},
        )
        return {
            **attempt.to_dict(),
            "agent": "planner",
            "classification": classification,
            "plan_base": plan_base,
            "prompt": prompt,
        }

    def finish_plan(self, task_id: str) -> PlanRecord | None:
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no retained attempt")
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        context = _load_json(self.runtime_dir / f"{task_id}-planning-context.json")
        classification = _required_string(context, "classification")
        plan_base = _required_string(context, "plan_base")
        result_path = worktree / ".workflow" / "planner-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"planner result is missing: {result_path}")
        result = _load_json(result_path)
        outcome = self._validate_planner_result(result, task_id)
        result_path.unlink()
        changed = _working_tree_changes(worktree)
        forbidden: list[str] = []
        for path in changed:
            allowed = path.startswith("todo/phases/") or path == "todo/config.yaml"
            if classification == "SPEC_DEFECT":
                allowed = allowed or path.startswith("docs/spec/")
            if classification == "OWNER_DECISION_REQUIRED":
                allowed = (
                    allowed or path.startswith("docs/spec/") or path.startswith("docs/intent/")
                )
            if not allowed:
                forbidden.append(path)
        if forbidden:
            raise WorkflowError(
                "planner changed paths outside triaged scope: " + ", ".join(forbidden)
            )
        if "todo/config.yaml" in changed:
            planned_config = _load_json(worktree / "todo" / "config.yaml")
            self.validate_config(planned_config, worktree)
            if _without_planner_owned_config_fields(
                config, classification
            ) != _without_planner_owned_config_fields(planned_config, classification):
                raise WorkflowError(
                    "planner changed workflow state, evidence, model, SHA, or another "
                    "controller-owned config field"
                )
            config = planned_config
        if outcome == "NO_CHANGE_REQUIRED" and changed:
            raise WorkflowError("NO_CHANGE_REQUIRED contradicts planner file changes")

        evidence_path = (
            Path("todo")
            / "evidence"
            / attempt.phase
            / task_id
            / f"attempt-{attempt.attempt:03d}-planner.json"
        )
        _write_json(worktree / evidence_path, result)
        if outcome in {"BLOCKED", "OWNER_DECISION_REQUIRED"}:
            state = "BLOCKED" if outcome == "BLOCKED" else "OWNER_DECISION_REQUIRED"
            self._set_state(config, task_id, state)
            _write_json(worktree / "todo" / "config.yaml", config)
            _git(worktree, "add", "-A")
            _git(worktree, "commit", "-m", f"chore(workflow): record {task_id} planning {state}")
            return None

        self._set_state(config, task_id, "AWAITING_PLAN_REVIEW")
        _write_json(worktree / "todo" / "config.yaml", config)
        _git(worktree, "diff", "--check")
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", f"docs({task_id.lower()}): planning candidate")
        plan = PlanRecord(
            task_id=task_id,
            attempt=attempt.attempt,
            classification=classification,
            base_commit=plan_base,
            candidate_commit=_sha(worktree),
        )
        self.save_plan(plan)
        (self.runtime_dir / f"{task_id}-planning-context.json").unlink(missing_ok=True)
        return plan

    def _validate_planner_result(self, result: Mapping[str, Any], task_id: str) -> str:
        if result.get("task_id") != task_id:
            raise WorkflowError("planner result task_id does not match")
        outcome = result.get("outcome")
        if not isinstance(outcome, str) or outcome not in {
            "PLAN_READY",
            "NO_CHANGE_REQUIRED",
            "OWNER_DECISION_REQUIRED",
            "BLOCKED",
        }:
            raise WorkflowError("planner result outcome is invalid")
        if not isinstance(result.get("summary"), str) or not isinstance(
            result.get("rationale"), str
        ):
            raise WorkflowError("planner result summary and rationale must be strings")
        if not isinstance(result.get("unresolved_questions"), list):
            raise WorkflowError("planner unresolved_questions must be a list")
        return outcome

    def prepare_plan_review(self, task_id: str) -> dict[str, object]:
        attempt = self.load_attempt(task_id)
        plan = self.load_plan(task_id)
        if attempt is None or plan is None:
            raise WorkflowError(f"{task_id} has no plan candidate")
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        if self._task(config, task_id)["status"] not in {
            "AWAITING_PLAN_REVIEW",
            "PLAN_REVIEW_BLOCKED",
        }:
            raise WorkflowError("review-plan requires AWAITING_PLAN_REVIEW or PLAN_REVIEW_BLOCKED")
        if _sha(worktree) != plan.candidate_commit:
            raise WorkflowError("planning worktree HEAD no longer matches plan candidate")
        if _git(worktree, "status", "--porcelain").stdout:
            raise WorkflowError("planning worktree must be clean before plan review")
        review_worktree = (
            self.worktree_root / f"plan-review-{task_id.lower()}-attempt-{attempt.attempt:03d}"
        )
        if review_worktree.exists():
            raise WorkflowError(f"plan review worktree path already exists: {review_worktree}")
        _git(self.repo, "worktree", "add", "--detach", str(review_worktree), plan.candidate_commit)
        prompt = (
            f"Review the planning correction for {task_id}. Classification: {plan.classification}. "
            f"Base commit: {plan.base_commit}. Candidate commit: {plan.candidate_commit}. Work only "
            f"in {review_worktree}. Write the structured result only to "
            f"{review_worktree / '.workflow' / 'plan-review-result.json'}."
        )
        return {
            **plan.to_dict(),
            "agent": "plan-reviewer",
            "review_worktree": str(review_worktree),
            "prompt": prompt,
        }

    def finish_plan_review(self, task_id: str) -> tuple[str, Path]:
        attempt = self.load_attempt(task_id)
        plan = self.load_plan(task_id)
        if attempt is None or plan is None:
            raise WorkflowError(f"{task_id} has no plan candidate")
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        review_worktree = (
            self.worktree_root / f"plan-review-{task_id.lower()}-attempt-{attempt.attempt:03d}"
        )
        result_path = review_worktree / ".workflow" / "plan-review-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"plan review result is missing: {result_path}")
        result = _load_json(result_path)
        state = self._validate_plan_review_result(result, plan)
        result_path.unlink()
        if _git(review_worktree, "status", "--porcelain").stdout.strip():
            raise WorkflowError("plan reviewer left changes outside its result handoff")
        _git(self.repo, "worktree", "remove", str(review_worktree), check=False)

        json_report = (
            Path("todo")
            / "reviews"
            / attempt.phase
            / task_id
            / f"plan-review-{attempt.attempt:03d}.json"
        )
        markdown_report = json_report.with_suffix(".md")
        _write_json(worktree / json_report, result)
        (worktree / markdown_report).write_text(_render_plan_review(result), encoding="utf-8")
        self._set_state(config, task_id, state)
        _write_json(worktree / "todo" / "config.yaml", config)
        _git(worktree, "add", "todo/config.yaml", str(json_report), str(markdown_report))
        _git(worktree, "commit", "-m", f"chore(workflow): record {task_id} plan review")
        if state == "PLAN_REVIEW_BLOCKED":
            self.save_plan(
                PlanRecord(
                    task_id=plan.task_id,
                    attempt=plan.attempt,
                    classification=plan.classification,
                    base_commit=plan.base_commit,
                    candidate_commit=_sha(worktree),
                )
            )
        else:
            self._plan_path(task_id).unlink(missing_ok=True)
        return state, worktree / markdown_report

    def _validate_plan_review_result(self, result: Mapping[str, Any], plan: PlanRecord) -> str:
        if (
            result.get("task_id") != plan.task_id
            or result.get("base_commit") != plan.base_commit
            or result.get("candidate_commit") != plan.candidate_commit
        ):
            raise WorkflowError("plan review identity or commits do not match")
        verdict = result.get("verdict")
        required = result.get("required_changes")
        unknowns = result.get("unknowns")
        if verdict not in {"PASS", "FAIL", "BLOCKED"}:
            raise WorkflowError("plan review verdict is invalid")
        if not isinstance(required, list) or not isinstance(unknowns, list):
            raise WorkflowError("plan review findings must be lists")
        if verdict == "PASS":
            if required or unknowns:
                raise WorkflowError("plan review PASS contradicts unresolved findings")
            return "CHANGES_REQUESTED"
        if verdict == "BLOCKED" or unknowns:
            return "PLAN_REVIEW_BLOCKED"
        return "PLANNING"

    def check_changed_paths(self, base: str, root: Path | None = None) -> list[str]:
        worktree = (root or self.repo).resolve()
        if not SHA_PATTERN.fullmatch(base):
            raise WorkflowError("base must be a full lowercase Git SHA")
        tracked = _git(worktree, "diff", "--name-only", base, "--").stdout.splitlines()
        untracked = _git(worktree, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
        changed = sorted(set(tracked + untracked))
        forbidden = [
            path
            for path in changed
            if path in PROTECTED_FILES
            or any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES)
        ]
        if forbidden:
            raise WorkflowError("protected paths changed: " + ", ".join(forbidden))
        return changed
