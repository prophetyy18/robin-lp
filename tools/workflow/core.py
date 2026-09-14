"""Git- and commit-based workflow gates for one-task agent execution.

The language model reasons about a task. This module owns only mechanical
constraints: task state, dependency gates, protected paths, exact Git SHAs,
worktree creation, structured result validation and review persistence.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    "SPEC_BLOCKED",
    "BLOCKED",
    "APPROVED",
}

ALLOWED_TRANSITIONS = {
    "PLANNED": {"READY"},
    "READY": {"IN_DEVELOPMENT", "BLOCKED"},
    "IN_DEVELOPMENT": {"AWAITING_REVIEW", "SPEC_BLOCKED", "BLOCKED"},
    "AWAITING_REVIEW": {"APPROVED", "CHANGES_REQUESTED", "BLOCKED"},
    "CHANGES_REQUESTED": {"IN_DEVELOPMENT", "BLOCKED"},
    "SPEC_BLOCKED": {"READY", "BLOCKED"},
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


class WorkflowManager:
    """Run one developer and one reviewer at a time against immutable commits."""

    def __init__(
        self,
        repo: Path,
        *,
        claude_command: str = "claude",
        worktree_root: Path | None = None,
    ) -> None:
        self.repo = repo.resolve()
        discovered = Path(_git(self.repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if discovered != self.repo:
            raise WorkflowError(f"run from repository root {discovered}")
        self.claude_command = claude_command
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
        return {
            "active_phase": config.get("active_phase"),
            "active_task": active,
            "workflow_state": config.get("workflow_state"),
            "task_status": config["tasks"][active]["status"] if active else None,
            "attempt": runtime.to_dict() if runtime else None,
        }

    def validate_repository(self) -> None:
        self.load_config()
        if not (self.repo / ".claude" / "agents" / "stage-developer.md").is_file():
            raise WorkflowError("stage-developer agent is missing")
        if not (self.repo / ".claude" / "agents" / "stage-reviewer.md").is_file():
            raise WorkflowError("stage-reviewer agent is missing")
        for name in ("config", "developer-result", "review-result"):
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

    def _attempt_path(self, task_id: str) -> Path:
        return self.runtime_dir / f"{task_id}.json"

    def save_attempt(self, attempt: AttemptRecord) -> None:
        _write_json(self._attempt_path(attempt.task_id), attempt.to_dict())

    def load_attempt(self, task_id: str | None) -> AttemptRecord | None:
        if task_id is None:
            return None
        path = self._attempt_path(task_id)
        if not path.is_file():
            return None
        return AttemptRecord.from_dict(_load_json(path))

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

    def _agent_result(self, result: CommandResult) -> dict[str, Any]:
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise WorkflowError(f"Claude Code failed ({result.returncode}): {detail}")
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise WorkflowError("Claude Code did not return a JSON envelope") from exc
        structured = envelope.get("structured_output")
        if not isinstance(structured, dict):
            raise WorkflowError("Claude Code JSON has no structured_output object")
        return structured

    def _launch_agent(
        self,
        *,
        agent: str,
        prompt: str,
        schema_path: Path,
        cwd: Path,
        developer: bool,
    ) -> dict[str, Any]:
        runtime = self.load_config(cwd)["agent_runtime"]
        model = runtime["model"]
        schema = schema_path.read_text(encoding="utf-8")
        tools = [
            "Read",
            "Grep",
            "Glob",
            "Bash(git status *)",
            "Bash(git diff *)",
            "Bash(git log *)",
            "Bash(python *)",
            "Bash(python3 *)",
            "Bash(pytest *)",
            "Bash(ruff *)",
            "Bash(mypy *)",
        ]
        if developer:
            tools.extend(("Edit", "Write"))
        args = (
            self.claude_command,
            "-p",
            "--agent",
            agent,
            "--model",
            model,
            "--permission-mode",
            "acceptEdits" if developer else "dontAsk",
            "--permission-prompts",
            "none",
            "--allowedTools",
            ",".join(tools),
            "--output-format",
            "json",
            "--json-schema",
            schema,
            prompt,
        )
        return self._agent_result(_run(args, cwd=cwd, check=False, timeout=7200))

    def develop(self, task_id: str, *, retry: bool = False) -> AttemptRecord:
        if retry:
            attempt = self.load_attempt(task_id)
            if attempt is None:
                raise WorkflowError(f"no retained attempt for {task_id}")
            worktree = Path(attempt.development_worktree)
            config = self.load_config(worktree)
            task = self._task(config, task_id)
            if task["status"] != "CHANGES_REQUESTED":
                raise WorkflowError(f"retry requires CHANGES_REQUESTED, found {task['status']}")
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
        task_file = config["tasks"][task_id]["task_file"]
        prompt = (
            f"Implement exactly {task_id}. The frozen contract is {task_file}. "
            f"The approved base is {attempt.base_commit}. This is attempt {attempt.attempt}. "
            "Do not commit. Return the required structured developer result."
        )
        result = self._launch_agent(
            agent="stage-developer",
            prompt=prompt,
            schema_path=worktree / "todo" / "schemas" / "developer-result.schema.json",
            cwd=worktree,
            developer=True,
        )
        self._validate_developer_result(result, task_id)
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
            state = "SPEC_BLOCKED" if outcome == "SPEC_BLOCKED" else "BLOCKED"
            self._set_state(config, task_id, state, base_commit=attempt.base_commit)
            _write_json(worktree / "todo" / "config.yaml", config)
            _git(worktree, "add", "todo/config.yaml", str(evidence_path.relative_to(worktree)))
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
        return attempt

    def _validate_developer_result(self, result: Mapping[str, Any], task_id: str) -> None:
        if result.get("task_id") != task_id:
            raise WorkflowError("developer result task_id does not match")
        if result.get("outcome") not in {"CANDIDATE_READY", "SPEC_BLOCKED", "BLOCKED"}:
            raise WorkflowError("developer result has invalid outcome")
        if not isinstance(result.get("summary"), str):
            raise WorkflowError("developer result summary must be a string")
        if not isinstance(result.get("commands"), list):
            raise WorkflowError("developer result commands must be a list")
        if not isinstance(result.get("residual_risks"), list):
            raise WorkflowError("developer result residual_risks must be a list")

    def _validate_review_result(self, result: Mapping[str, Any], attempt: AttemptRecord) -> str:
        if result.get("task_id") != attempt.task_id:
            raise WorkflowError("review task_id does not match")
        if result.get("base_commit") != attempt.base_commit:
            raise WorkflowError("review base_commit does not match")
        if result.get("candidate_commit") != attempt.candidate_commit:
            raise WorkflowError("review candidate_commit does not match")
        verdict = result.get("verdict")
        if verdict not in {"PASS", "FAIL", "BLOCKED"}:
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

    def review(self, task_id: str) -> tuple[str, Path]:
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
        try:
            task_file = config["tasks"][task_id]["task_file"]
            prompt = (
                f"Independently review exactly {task_id} using {task_file}. "
                f"Base commit: {attempt.base_commit}. Candidate commit: {attempt.candidate_commit}. "
                "Inspect the full diff and run every obtainable acceptance check. "
                "Return the required structured review result and do not fix anything."
            )
            result = self._launch_agent(
                agent="stage-reviewer",
                prompt=prompt,
                schema_path=review_worktree / "todo" / "schemas" / "review-result.schema.json",
                cwd=review_worktree,
                developer=False,
            )
            if _git(review_worktree, "diff", "--quiet", check=False).returncode != 0:
                raise WorkflowError("reviewer modified tracked files")
            if _git(review_worktree, "diff", "--cached", "--quiet", check=False).returncode != 0:
                raise WorkflowError("reviewer staged tracked changes")
            state = self._validate_review_result(result, attempt)
        finally:
            _git(self.repo, "worktree", "remove", str(review_worktree), check=False)

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


def find_claude() -> str:
    executable = shutil.which("claude")
    if executable is None:
        raise WorkflowError("claude executable is not available")
    return executable
