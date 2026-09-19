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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TASK_PATTERN = re.compile(r"^T[0-9]{3}$")
MAINTENANCE_PATTERN = re.compile(r"^M[0-9]{4}$")
AMENDMENT_PATTERN = re.compile(r"^A[0-9]{4}$")
PHASE_PATTERN = re.compile(r"^P[0-9]{2}$")
#: Owner-directed change layers. CONTRACT edits the target task contracts and
#: their dependency fields; SPEC additionally edits ``docs/spec/``. SUPERSEDE is a
#: separate axis: it retires already-APPROVED tasks by recording their successor,
#: and may touch no document and no approval evidence.
#:
#: PROPHET is a separate role as well as a separate scope, and it is named for the
#: role rather than for the Owner so that the human Owner and the agent that
#: restates their goals are never confused. It may state Intent, restructure the
#: plan and correct collateral documents, and it may create new task contracts but
#: never modify an existing one. Authoring a goal belongs here; the Planner
#: translates a goal into task text, and on the triaged route it only transcribes
#: an Owner decision the controller required before it would start.
AMENDMENT_LAYERS = frozenset({"CONTRACT", "SPEC", "PROPHET", "SUPERSEDE"})
REQUIRED_AGENT_MODEL = "MiniMax-M3[1m]"
MAX_DEVELOPMENT_CONTINUATIONS = 1

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
MAINTENANCE_FORBIDDEN_PREFIXES = (
    ".claude/",
    ".github/",
    "docs/intent/",
    "docs/spec/",
    "todo/phases/",
    "todo/schemas/",
    "todo/maintenance/",
    "tools/workflow/",
    "src/robinhood_lp/execution/",
    "src/robinhood_lp/risk/",
    "src/robinhood_lp/signer/",
)
MAINTENANCE_FORBIDDEN_FILES = PROTECTED_FILES | {
    "pyproject.toml",
    "requirements.in",
    "requirements.lock.txt",
    "tools/oracle/foundry.toml",
    "tools/oracle/remappings.txt",
}
MAINTENANCE_STATES = {
    "IN_DEVELOPMENT",
    "AWAITING_REVIEW",
    "CHANGES_REQUESTED",
    "ESCALATED",
    "BLOCKED",
}


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
    continuation_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "phase": self.phase,
            "attempt": self.attempt,
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "branch": self.branch,
            "development_worktree": self.development_worktree,
            "continuation_count": self.continuation_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AttemptRecord:
        continuation_count = value.get("continuation_count", 0)
        if (
            not isinstance(continuation_count, int)
            or isinstance(continuation_count, bool)
            or continuation_count < 0
        ):
            raise WorkflowError("continuation_count must be a non-negative integer")
        return cls(
            task_id=_required_string(value, "task_id"),
            phase=_required_string(value, "phase"),
            attempt=_required_int(value, "attempt"),
            base_commit=_required_string(value, "base_commit"),
            candidate_commit=_optional_string(value, "candidate_commit"),
            branch=_required_string(value, "branch"),
            development_worktree=_required_string(value, "development_worktree"),
            continuation_count=continuation_count,
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


@dataclass(frozen=True)
class AmendmentRecord:
    amendment_id: str
    status: str
    attempt: int
    layer: str
    task_ids: tuple[str, ...]
    base_commit: str
    candidate_commit: str | None
    branch: str
    worktree: str

    def to_dict(self) -> dict[str, object]:
        return {
            "amendment_id": self.amendment_id,
            "status": self.status,
            "attempt": self.attempt,
            "layer": self.layer,
            "task_ids": list(self.task_ids),
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "branch": self.branch,
            "worktree": self.worktree,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AmendmentRecord:
        amendment_id = _required_string(value, "amendment_id")
        if not AMENDMENT_PATTERN.fullmatch(amendment_id):
            raise WorkflowError(f"invalid amendment ID {amendment_id!r}")
        status = _required_string(value, "status")
        if status not in {"PLANNING", "AWAITING_REVIEW", "CHANGES_REQUESTED", "BLOCKED"}:
            raise WorkflowError(f"invalid amendment status {status!r}")
        layer = _required_string(value, "layer")
        if layer not in AMENDMENT_LAYERS:
            raise WorkflowError(f"invalid amendment layer {layer!r}")
        raw_task_ids = value.get("task_ids")
        if not isinstance(raw_task_ids, list) or (not raw_task_ids and layer != "PROPHET"):
            raise WorkflowError(
                f"{layer} task_ids must be a non-empty list"
                if layer != "PROPHET"
                else "PROPHET task_ids must be a list (empty is allowed)"
            )
        if not all(isinstance(item, str) and TASK_PATTERN.fullmatch(item) for item in raw_task_ids):
            raise WorkflowError("amendment task_ids contain an invalid task ID")
        return cls(
            amendment_id=amendment_id,
            status=status,
            attempt=_required_int(value, "attempt"),
            layer=layer,
            task_ids=tuple(cast(list[str], raw_task_ids)),
            base_commit=_required_string(value, "base_commit"),
            candidate_commit=_optional_string(value, "candidate_commit"),
            branch=_required_string(value, "branch"),
            worktree=_required_string(value, "worktree"),
        )


@dataclass(frozen=True)
class MaintenanceRecord:
    maintenance_id: str
    status: str
    attempt: int
    base_commit: str
    candidate_commit: str | None
    branch: str
    development_worktree: str
    continuation_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "maintenance_id": self.maintenance_id,
            "status": self.status,
            "attempt": self.attempt,
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "branch": self.branch,
            "development_worktree": self.development_worktree,
            "continuation_count": self.continuation_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MaintenanceRecord:
        maintenance_id = _required_string(value, "maintenance_id")
        if not MAINTENANCE_PATTERN.fullmatch(maintenance_id):
            raise WorkflowError(f"invalid maintenance ID {maintenance_id!r}")
        status = _required_string(value, "status")
        if status not in MAINTENANCE_STATES:
            raise WorkflowError(f"invalid maintenance status {status!r}")
        continuation_count = value.get("continuation_count", 0)
        if (
            not isinstance(continuation_count, int)
            or isinstance(continuation_count, bool)
            or continuation_count < 0
        ):
            raise WorkflowError("continuation_count must be a non-negative integer")
        return cls(
            maintenance_id=maintenance_id,
            status=status,
            attempt=_required_int(value, "attempt"),
            base_commit=_required_string(value, "base_commit"),
            candidate_commit=_optional_string(value, "candidate_commit"),
            branch=_required_string(value, "branch"),
            development_worktree=_required_string(value, "development_worktree"),
            continuation_count=continuation_count,
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


def _validate_object_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
    label: str,
) -> None:
    optional_fields = optional or set()
    missing = required - value.keys()
    unexpected = value.keys() - required - optional_fields
    if missing:
        raise WorkflowError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    if unexpected:
        raise WorkflowError(f"{label} has unexpected fields: {', '.join(sorted(unexpected))}")


def _validate_string_list(value: object, label: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkflowError(f"{label} must be a list of strings")


def _snapshot(paths: Sequence[Path], root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _protected_paths(root: Path) -> list[Path]:
    # Match Git's view of repository content. Files ignored by the repository
    # (bytecode, test caches, build output, and similar local artefacts) are not
    # candidate changes and must not alter the protected snapshot. Tracked files
    # and non-ignored untracked files remain covered.
    pathspecs = [*sorted(PROTECTED_FILES), *PROTECTED_PREFIXES]
    relative_paths = _git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        *pathspecs,
    ).stdout.splitlines()
    return sorted(root / relative for relative in relative_paths if (root / relative).is_file())


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
        # The Planner transcribes the Owner's answer here; it never authors one.
        # ``--owner-decision`` is mandatory for this classification, and that
        # requirement is what keeps transcription distinct from invention.
        comparable.pop("intent_revision", None)
    return comparable


def _without_amendment_owned_config_fields(
    config: Mapping[str, Any], task_ids: Sequence[str], layer: str
) -> dict[str, Any]:
    comparable = cast(dict[str, Any], json.loads(json.dumps(config)))
    for task_id in task_ids:
        comparable["tasks"][task_id].pop("depends_on", None)
        if layer == "SUPERSEDE":
            comparable["tasks"][task_id].pop("superseded_by", None)
    if layer in {"SPEC", "PROPHET"}:
        comparable.pop("spec_revision", None)
    if layer == "PROPHET":
        comparable.pop("intent_revision", None)
    return comparable


def _without_prophet_owned_config_fields(
    config: Mapping[str, Any], base_config: Mapping[str, Any]
) -> dict[str, Any]:
    """Reduce a config to the fields a PROPHET change may not touch.

    An Owner change may add tasks and raise the two revisions. It may not alter
    anything about a task that already exists -- not its state, not its
    dependencies, not its evidence, not its contract path. Dropping every task
    key absent from the base and both revisions leaves exactly the frozen set,
    so a candidate that edits an existing task compares unequal here.
    """
    comparable = cast(dict[str, Any], json.loads(json.dumps(config)))
    comparable.pop("spec_revision", None)
    comparable.pop("intent_revision", None)
    existing = set(base_config["tasks"])
    comparable["tasks"] = {
        task_id: task for task_id, task in comparable["tasks"].items() if task_id in existing
    }
    return comparable


#: A new task contract a PROPHET change may create. Modifying an existing file
#: that matches this pattern is the one thing the Owner role must never do, so
#: the status letter is part of the test rather than an afterthought.
_TASK_CONTRACT_PATH = re.compile(r"^todo/phases/[^/]+/T[0-9]{3}\.md$")

#: Files a PROPHET change may edit freely. Everything absent from this set is
#: refused, so the enforcement core, the agent definitions, the schemas, CI and
#: the source tree stay out of reach without needing to be listed.
_PROPHET_EDITABLE_FILES = frozenset(
    {
        "README.md",
        "CLAUDE.md",
        "AGENTS.md",
        "todo/README.md",
        "todo/WORKFLOW.md",
    }
)
_PROPHET_EDITABLE_PREFIXES = ("docs/spec/", "docs/intent/", "docs/implement/")


def _prophet_path_allowed(path: str, status: str) -> bool:
    if status == "D":
        return False
    if path in _PROPHET_EDITABLE_FILES or path.startswith(_PROPHET_EDITABLE_PREFIXES):
        return True
    if path.startswith("todo/phases/"):
        if path.endswith("README.md"):
            return True
        if _TASK_CONTRACT_PATH.fullmatch(path):
            return status == "A"
    return False


def _change_statuses(root: Path, base: str) -> dict[str, str]:
    """Map every changed path to its single-letter Git status."""
    statuses: dict[str, str] = {}
    for line in _git(root, "diff", "--name-status", base, "--").stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[-1]:
            statuses[parts[-1]] = parts[0][0]
    for path in _git(root, "ls-files", "--others", "--exclude-standard").stdout.splitlines():
        statuses.setdefault(path, "A")
    return statuses


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


def _render_amendment_review(result: Mapping[str, Any]) -> str:
    lines = [
        f"# {result['amendment_id']} owner amendment review",
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
            superseded_by = raw.get("superseded_by")
            if superseded_by is not None:
                if not isinstance(superseded_by, str) or not TASK_PATTERN.fullmatch(superseded_by):
                    raise WorkflowError(f"{task_id} superseded_by must be a task ID")
                if superseded_by == task_id:
                    raise WorkflowError(f"{task_id} superseded_by must not reference itself")
                if superseded_by not in tasks:
                    raise WorkflowError(
                        f"{task_id} superseded_by references unknown task {superseded_by}"
                    )
                if status != "APPROVED":
                    raise WorkflowError(
                        f"{task_id} superseded_by requires APPROVED status, found {status}"
                    )
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
        active_maintenance = [
            record.to_dict()
            for record in self._maintenance_records()
            if record.status in {"IN_DEVELOPMENT", "AWAITING_REVIEW", "CHANGES_REQUESTED"}
        ]
        active_amendments = [record.to_dict() for record in self._amendment_records()]
        return {
            "active_phase": config.get("active_phase"),
            "active_task": active,
            "workflow_state": config.get("workflow_state"),
            "task_status": config["tasks"][active]["status"] if active else None,
            "attempt": runtime.to_dict() if runtime else None,
            "plan": active_plan.to_dict() if active_plan else None,
            "active_maintenance": active_maintenance,
            "active_amendments": active_amendments,
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
            "prophet",
            "prophet-reviewer",
        ):
            if not (self.repo / ".claude" / "agents" / f"{agent}.md").is_file():
                raise WorkflowError(f"{agent} agent is missing")
        for name in (
            "config",
            "amendment-result",
            "amendment-review-result",
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
        # A task may depend on the work it supersedes -- that is the one case where
        # building on a retired contract is the point. Any other dependency on a
        # retired task must be re-pointed at the successor deliberately.
        retired = [
            dependency
            for dependency in task["depends_on"]
            if config["tasks"][dependency].get("superseded_by")
            and config["tasks"][dependency]["superseded_by"] != task_id
        ]
        if retired:
            raise WorkflowError(
                f"{task_id} depends on superseded task(s): {', '.join(retired)}; "
                "re-point the dependency at the successor through an owner amendment"
            )

    def ready(self, task_id: str) -> str:
        self._ensure_clean_main()
        if self._amendment_records():
            raise WorkflowError("cannot activate a task while an owner amendment is unfinished")
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

    def _continuation_path(self, task_id: str) -> Path:
        return self.runtime_dir / f"{task_id}-continuation.json"

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

    def _amendment_path(self, amendment_id: str) -> Path:
        return self.runtime_dir / f"{amendment_id}.json"

    def _amendment_request_path(self, amendment_id: str) -> Path:
        return self.runtime_dir / f"{amendment_id}-request.json"

    def save_amendment(self, record: AmendmentRecord) -> None:
        _write_json(self._amendment_path(record.amendment_id), record.to_dict())

    def _amendment_records(self) -> list[AmendmentRecord]:
        if not self.runtime_dir.is_dir():
            return []
        return [
            AmendmentRecord.from_dict(_load_json(path))
            for path in sorted(self.runtime_dir.glob("A[0-9][0-9][0-9][0-9].json"))
        ]

    def load_amendment(self, amendment_id: str) -> AmendmentRecord | None:
        if not AMENDMENT_PATTERN.fullmatch(amendment_id):
            raise WorkflowError(f"invalid amendment ID {amendment_id!r}")
        path = self._amendment_path(amendment_id)
        if not path.is_file():
            return None
        return AmendmentRecord.from_dict(_load_json(path))

    def _next_amendment_id(self) -> str:
        numbers: set[int] = set()
        amendment_root = self.repo / "todo" / "amendments"
        if amendment_root.is_dir():
            for path in amendment_root.glob("A[0-9][0-9][0-9][0-9]"):
                if path.is_dir() and AMENDMENT_PATTERN.fullmatch(path.name):
                    numbers.add(int(path.name[1:]))
        if self.runtime_dir.is_dir():
            for path in self.runtime_dir.glob("A[0-9][0-9][0-9][0-9].json"):
                if AMENDMENT_PATTERN.fullmatch(path.stem):
                    numbers.add(int(path.stem[1:]))
        number = max(numbers, default=0) + 1
        if number > 9999:
            raise WorkflowError("amendment ID space is exhausted")
        return f"A{number:04d}"

    def _maintenance_path(self, maintenance_id: str) -> Path:
        return self.runtime_dir / f"{maintenance_id}.json"

    def _maintenance_request_path(self, maintenance_id: str) -> Path:
        return self.runtime_dir / f"{maintenance_id}-request.json"

    def _maintenance_records(self) -> list[MaintenanceRecord]:
        if not self.runtime_dir.is_dir():
            return []
        records: list[MaintenanceRecord] = []
        for path in sorted(self.runtime_dir.glob("M[0-9][0-9][0-9][0-9].json")):
            records.append(MaintenanceRecord.from_dict(_load_json(path)))
        return records

    def save_maintenance(self, record: MaintenanceRecord) -> None:
        _write_json(self._maintenance_path(record.maintenance_id), record.to_dict())

    def load_maintenance(self, maintenance_id: str) -> MaintenanceRecord | None:
        if not MAINTENANCE_PATTERN.fullmatch(maintenance_id):
            raise WorkflowError(f"invalid maintenance ID {maintenance_id!r}")
        path = self._maintenance_path(maintenance_id)
        if not path.is_file():
            return None
        return MaintenanceRecord.from_dict(_load_json(path))

    def _next_maintenance_id(self) -> str:
        numbers: set[int] = set()
        maintenance_root = self.repo / "todo" / "maintenance"
        if maintenance_root.is_dir():
            for path in maintenance_root.glob("M[0-9][0-9][0-9][0-9]"):
                if path.is_dir() and MAINTENANCE_PATTERN.fullmatch(path.name):
                    numbers.add(int(path.name[1:]))
        if self.runtime_dir.is_dir():
            for path in self.runtime_dir.glob("M[0-9][0-9][0-9][0-9].json"):
                if MAINTENANCE_PATTERN.fullmatch(path.stem):
                    numbers.add(int(path.stem[1:]))
        number = max(numbers, default=0) + 1
        if number > 9999:
            raise WorkflowError("maintenance ID space is exhausted")
        return f"M{number:04d}"

    def _validate_maintenance_paths(self, allowed_paths: Sequence[str]) -> list[str]:
        if not allowed_paths or len(allowed_paths) > 5:
            raise WorkflowError("maintenance requires between one and five explicit paths")
        normalized: list[str] = []
        for raw in allowed_paths:
            path = Path(raw)
            if path.is_absolute() or raw != path.as_posix() or ".." in path.parts:
                raise WorkflowError(f"maintenance path must be normalized and relative: {raw!r}")
            if any(char in raw for char in "*?[]"):
                raise WorkflowError(f"maintenance path must not contain a glob: {raw!r}")
            if raw in MAINTENANCE_FORBIDDEN_FILES or any(
                raw.startswith(prefix) for prefix in MAINTENANCE_FORBIDDEN_PREFIXES
            ):
                raise WorkflowError(f"maintenance path is high-risk or protected: {raw}")
            resolved = self.repo / raw
            if resolved.exists() and not resolved.is_file():
                raise WorkflowError(f"maintenance path must identify a file: {raw}")
            normalized.append(raw)
        if len(set(normalized)) != len(normalized):
            raise WorkflowError("maintenance paths must be unique")
        return normalized

    def _validate_maintenance_request(self, request: Mapping[str, Any]) -> None:
        _validate_object_keys(
            request,
            required={
                "maintenance_id",
                "summary",
                "reason",
                "allowed_paths",
                "verification_commands",
                "related_task",
                "risk_attestation",
            },
            label="maintenance request",
        )
        maintenance_id = request.get("maintenance_id")
        if not isinstance(maintenance_id, str) or not MAINTENANCE_PATTERN.fullmatch(maintenance_id):
            raise WorkflowError("maintenance request has an invalid maintenance_id")
        for field in ("summary", "reason"):
            if not isinstance(request.get(field), str) or not request[field].strip():
                raise WorkflowError(f"maintenance request {field} must be non-empty")
            if "\n" in request[field] or "\r" in request[field]:
                raise WorkflowError(f"maintenance request {field} must be one line")
        if len(request["summary"]) > 120 or len(request["reason"]) > 1000:
            raise WorkflowError("maintenance summary or reason is too long")
        request_paths = request.get("allowed_paths")
        if not isinstance(request_paths, list) or not all(
            isinstance(path, str) for path in request_paths
        ):
            raise WorkflowError("maintenance allowed_paths must be a list of strings")
        self._validate_maintenance_paths(request_paths)
        commands = request.get("verification_commands")
        if (
            not isinstance(commands, list)
            or not commands
            or len(commands) > 8
            or not all(isinstance(command, str) and command.strip() for command in commands)
        ):
            raise WorkflowError("maintenance requires between one and eight verification commands")
        related_task = request.get("related_task")
        if related_task is not None and (
            not isinstance(related_task, str) or not TASK_PATTERN.fullmatch(related_task)
        ):
            raise WorkflowError("maintenance related_task must be null or a valid task ID")
        if request.get("risk_attestation") != "LOW_RISK_IMPLEMENTATION_DEFECT":
            raise WorkflowError("maintenance requires the low-risk defect attestation")

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

    def continue_develop(
        self,
        task_id: str,
        *,
        max_turns_exhausted: bool = False,
    ) -> dict[str, object]:
        """Start a fresh Developer without changing the task attempt or worktree."""
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no prepared development attempt")
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        task = self._task(config, task_id)
        if task["status"] != "IN_DEVELOPMENT" or attempt.candidate_commit is not None:
            raise WorkflowError(
                "development continuation requires an unfinished IN_DEVELOPMENT attempt"
            )
        before = _load_json(self._protected_snapshot_path(task_id))
        after = _snapshot(_protected_paths(worktree), worktree)
        if before != after:
            raise WorkflowError(
                "developer modified a protected workflow, Intent, Spec, or task file"
            )
        checkpoint = self._load_or_create_continuation_checkpoint(
            task_id,
            worktree,
            max_turns_exhausted=max_turns_exhausted,
        )
        if attempt.continuation_count >= MAX_DEVELOPMENT_CONTINUATIONS:
            raise WorkflowError(
                f"development continuation limit reached for {task_id}; preserve the worktree "
                "and ask the Owner whether to expand the cumulative budget or triage task scope"
            )
        updated = AttemptRecord(
            task_id=attempt.task_id,
            phase=attempt.phase,
            attempt=attempt.attempt,
            base_commit=attempt.base_commit,
            candidate_commit=None,
            branch=attempt.branch,
            development_worktree=attempt.development_worktree,
            continuation_count=attempt.continuation_count + 1,
        )
        self.save_attempt(updated)
        _write_json(self._continuation_path(task_id), checkpoint)
        checkpoint_path = worktree / ".workflow" / "developer-continuation.json"
        _write_json(checkpoint_path, checkpoint)
        (worktree / ".workflow" / "developer-result.json").unlink(missing_ok=True)
        task_file = config["tasks"][task_id]["task_file"]
        prompt = (
            f"Continue exactly {task_id} in the existing attempt {attempt.attempt}. The frozen "
            f"contract is {task_file}; approved base is {attempt.base_commit}. Work only in "
            f"{worktree}. Read the prior checkpoint at {checkpoint_path}, then inspect the actual "
            "git diff and test state because the worktree is authoritative. Do not commit or "
            "start over. Before finishing, write the required structured developer result to "
            f"{worktree / '.workflow' / 'developer-result.json'}."
        )
        return {**updated.to_dict(), "agent": "stage-developer", "prompt": prompt}

    def _load_or_create_continuation_checkpoint(
        self,
        task_id: str,
        worktree: Path,
        *,
        max_turns_exhausted: bool,
    ) -> dict[str, Any]:
        result_path = worktree / ".workflow" / "developer-result.json"
        if result_path.is_file():
            result = _load_json(result_path)
            self._validate_developer_result(result, task_id)
            if result["outcome"] != "CONTINUATION_REQUIRED":
                raise WorkflowError(
                    "developer result is terminal; use the matching finish command instead"
                )
            return result
        if not max_turns_exhausted:
            raise WorkflowError(
                "continuation requires a CONTINUATION_REQUIRED handoff or the explicit "
                "--max-turns-exhausted flag"
            )
        changed_paths = [
            path
            for path in _working_tree_changes(worktree)
            if not path.startswith(".workflow/") and path != "todo/config.yaml"
        ]
        return {
            "task_id": task_id,
            "outcome": "CONTINUATION_REQUIRED",
            "summary": "The previous Developer exhausted maxTurns before writing a handoff.",
            "commands": [],
            "residual_risks": [
                "The fresh Developer must reconstruct progress from the actual worktree and rerun "
                "all required checks before producing a candidate."
            ],
            "continuation": {
                "reason": "TURN_BUDGET",
                "completed_work": [],
                "remaining_work": ["Inspect the retained worktree and complete the task contract."],
                "next_actions": ["Review git diff and current test state before making new edits."],
                "changed_paths": changed_paths,
            },
        }

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
        if result["outcome"] == "CONTINUATION_REQUIRED":
            raise WorkflowError(
                "continuation handoff requires continue-develop, not finish-develop"
            )
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
            (worktree / ".workflow" / "developer-result.json").unlink(missing_ok=True)
            (worktree / ".workflow" / "developer-continuation.json").unlink(missing_ok=True)
            self._continuation_path(task_id).unlink(missing_ok=True)
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
        (worktree / ".workflow" / "developer-result.json").unlink(missing_ok=True)
        (worktree / ".workflow" / "developer-continuation.json").unlink(missing_ok=True)
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
            continuation_count=attempt.continuation_count,
        )
        self.save_attempt(attempt)
        self._protected_snapshot_path(task_id).unlink(missing_ok=True)
        self._continuation_path(task_id).unlink(missing_ok=True)
        return attempt

    def _validate_developer_result(self, result: Mapping[str, Any], task_id: str) -> None:
        _validate_object_keys(
            result,
            required={"task_id", "outcome", "summary", "commands", "residual_risks"},
            optional={"blocking_question", "triage_request", "continuation"},
            label="developer result",
        )
        if result.get("task_id") != task_id:
            raise WorkflowError("developer result task_id does not match")
        outcome = result.get("outcome")
        if outcome not in {
            "CANDIDATE_READY",
            "CONTINUATION_REQUIRED",
            "TRIAGE_REQUIRED",
            "BLOCKED",
        }:
            raise WorkflowError("developer result has invalid outcome")
        if not isinstance(result.get("summary"), str):
            raise WorkflowError("developer result summary must be a string")
        commands = result.get("commands")
        if not isinstance(commands, list):
            raise WorkflowError("developer result commands must be a list")
        for command in commands:
            if not isinstance(command, dict):
                raise WorkflowError("developer result command must be an object")
            _validate_object_keys(
                command,
                required={"command", "result"},
                label="developer result command",
            )
            if not all(isinstance(command[key], str) for key in ("command", "result")):
                raise WorkflowError("developer result command fields must be strings")
        _validate_string_list(result.get("residual_risks"), "developer result residual_risks")
        if result.get("blocking_question") is not None and not isinstance(
            result.get("blocking_question"), str
        ):
            raise WorkflowError("developer result blocking_question must be a string or null")
        if outcome == "TRIAGE_REQUIRED":
            self._validate_triage_request(result.get("triage_request"))
        elif "triage_request" in result:
            raise WorkflowError("triage_request is allowed only for TRIAGE_REQUIRED")
        if outcome == "CONTINUATION_REQUIRED":
            self._validate_continuation(result.get("continuation"))
            if result.get("blocking_question") is not None:
                raise WorkflowError("continuation must not include a blocking question")
        elif "continuation" in result:
            raise WorkflowError("continuation is allowed only for CONTINUATION_REQUIRED")

    def _validate_continuation(self, value: object) -> None:
        if not isinstance(value, dict):
            raise WorkflowError("CONTINUATION_REQUIRED must include continuation")
        _validate_object_keys(
            value,
            required={
                "reason",
                "completed_work",
                "remaining_work",
                "next_actions",
                "changed_paths",
            },
            label="continuation",
        )
        if value.get("reason") != "TURN_BUDGET":
            raise WorkflowError("continuation reason must be TURN_BUDGET")
        for key in ("completed_work", "remaining_work", "next_actions", "changed_paths"):
            _validate_string_list(value.get(key), f"continuation {key}")
        if not value["remaining_work"] or not value["next_actions"]:
            raise WorkflowError("continuation must identify remaining work and next actions")

    def _validate_triage_request(self, value: object) -> None:
        if not isinstance(value, dict):
            raise WorkflowError("TRIAGE_REQUIRED must include triage_request")
        _validate_object_keys(
            value,
            required={
                "observed_problem",
                "evidence",
                "proposed_classification",
                "requested_change",
            },
            label="triage_request",
        )
        if not isinstance(value.get("observed_problem"), str) or not value["observed_problem"]:
            raise WorkflowError("triage_request must describe the observed problem")
        _validate_string_list(value.get("evidence"), "triage_request evidence")
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

    def prepare_amendment(
        self,
        *,
        task_ids: Sequence[str],
        layer: str,
        summary: str,
        owner_direction: str,
    ) -> dict[str, object]:
        """Prepare an Owner-directed change without a Developer failure."""
        self._ensure_clean_main()
        layer = layer.upper()
        if layer not in AMENDMENT_LAYERS:
            raise WorkflowError("amendment layer must be " + ", ".join(sorted(AMENDMENT_LAYERS)))
        config = self.load_config()
        active = config.get("active_task")
        active_status = config["tasks"][active]["status"] if isinstance(active, str) else None
        if active_status not in {None, "APPROVED", "PLANNED"}:
            raise WorkflowError(f"cannot start an amendment while {active} is unfinished")
        if self._amendment_records():
            raise WorkflowError("another owner amendment is already active")
        active_repairs = [
            record.maintenance_id
            for record in self._maintenance_records()
            if record.status in {"IN_DEVELOPMENT", "AWAITING_REVIEW", "CHANGES_REQUESTED"}
        ]
        if active_repairs:
            raise WorkflowError("cannot start an amendment while maintenance is unfinished")
        normalized = tuple(dict.fromkeys(task_ids))
        # ``--task`` names the *existing* contracts a change may edit. It is a
        # restriction, not an instruction: it never creates anything, and a task
        # an amendment adds is created by the contract file and config entry the
        # author writes. A PROPHET change therefore cannot be given one -- it
        # targets no existing contract, and accepting a name it ignores would let
        # a caller believe it had constrained a change it had not.
        if layer == "PROPHET" and normalized:
            raise WorkflowError(
                "a PROPHET change takes no --task: it targets no existing contract, and "
                "the tasks it adds are created by the files the prophet writes"
            )
        if not normalized and layer != "PROPHET":
            raise WorkflowError(f"a {layer} amendment requires at least one target task")
        if len(normalized) > 8:
            raise WorkflowError("an amendment may target at most eight tasks")
        # A SUPERSEDE amendment retires already-approved work by pointing it at
        # its successor; every other layer corrects work that has not run yet.
        required_status = "APPROVED" if layer == "SUPERSEDE" else "PLANNED"
        if layer != "PROPHET":
            for task_id in normalized:
                task = self._task(config, task_id)
                if task["status"] != required_status:
                    raise WorkflowError(
                        f"{layer} amendment targets must be {required_status}, "
                        f"found {task_id}={task['status']}"
                    )
        if not summary.strip() or not owner_direction.strip():
            raise WorkflowError("amendment summary and owner direction must be non-empty")
        amendment_id = self._next_amendment_id()
        base = _sha(self.repo)
        branch = f"amendment/{amendment_id.lower()}-attempt-001"
        worktree = self.worktree_root / f"amendment-{amendment_id.lower()}-attempt-001"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if worktree.exists():
            raise WorkflowError(f"amendment worktree path already exists: {worktree}")
        _git(self.repo, "worktree", "add", "-b", branch, str(worktree), base)
        record = AmendmentRecord(
            amendment_id=amendment_id,
            status="PLANNING",
            attempt=1,
            layer=layer,
            task_ids=normalized,
            base_commit=base,
            candidate_commit=None,
            branch=branch,
            worktree=str(worktree),
        )
        request: dict[str, object] = {
            "amendment_id": amendment_id,
            "task_ids": list(normalized),
            "layer": layer,
            "target_status": required_status if layer != "PROPHET" else None,
            "summary": summary.strip(),
            "owner_direction": owner_direction.strip(),
        }
        self.save_amendment(record)
        _write_json(self._amendment_request_path(amendment_id), request)
        request_path = worktree / ".workflow" / "amendment-request.json"
        _write_json(request_path, request)
        task_files = [config["tasks"][task_id]["task_file"] for task_id in normalized]
        if layer == "PROPHET":
            agent, scope = (
                "prophet",
                "Apply this Owner-directed change with the PROPHET layer. It restructures "
                "the plan, states Intent, or corrects collateral documents; it targets no "
                "task in particular",
            )
        else:
            agent, scope = (
                "planner",
                f"Apply Owner-directed amendment {amendment_id} to exactly these "
                f"{required_status} tasks: {', '.join(normalized)}",
            )
        prompt = (
            f"{scope}. Read {request_path}. Target "
            f"contracts: {', '.join(task_files) or 'none'}. Layer: {layer}. Work only in "
            f"{worktree}. Do not implement business code or change workflow state. Make only "
            "the smallest planning changes required by the recorded Owner direction. Write "
            f"the structured result only to {worktree / '.workflow' / 'amendment-result.json'}."
        )
        return {**record.to_dict(), "agent": agent, "prompt": prompt}

    def finish_amendment(self, amendment_id: str) -> AmendmentRecord:
        record = self.load_amendment(amendment_id)
        if record is None or record.status != "PLANNING":
            raise WorkflowError("finish-amendment requires an active PLANNING amendment")
        worktree = Path(record.worktree)
        request = _load_json(self._amendment_request_path(amendment_id))
        result_path = worktree / ".workflow" / "amendment-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"amendment result is missing: {result_path}")
        result = _load_json(result_path)
        outcome = self._validate_amendment_result(result, amendment_id)
        changed = [
            path for path in _working_tree_changes(worktree) if not path.startswith(".workflow/")
        ]
        config = self.load_config(worktree)
        base_config = self.load_config()
        allowed_contracts = {
            base_config["tasks"][task_id]["task_file"] for task_id in record.task_ids
        }
        statuses = _change_statuses(worktree, record.base_commit)
        forbidden: list[str] = []
        for path in changed:
            if record.layer == "PROPHET":
                allowed = path == "todo/config.yaml" or _prophet_path_allowed(
                    path, statuses.get(path, "M")
                )
            else:
                allowed = path in allowed_contracts or path == "todo/config.yaml"
                if record.layer == "SPEC":
                    allowed = allowed or path.startswith("docs/spec/")
            if not allowed:
                forbidden.append(path)
        if forbidden:
            joined = ", ".join(forbidden)
            if record.layer == "PROPHET":
                raise WorkflowError(f"prophet change altered paths outside its scope: {joined}")
            raise WorkflowError(f"planner changed paths outside the owner amendment: {joined}")
        if "todo/config.yaml" in changed:
            if record.layer == "PROPHET":
                drifted = _without_prophet_owned_config_fields(
                    config, base_config
                ) != _without_prophet_owned_config_fields(base_config, base_config)
            else:
                drifted = _without_amendment_owned_config_fields(
                    base_config, record.task_ids, record.layer
                ) != _without_amendment_owned_config_fields(config, record.task_ids, record.layer)
            if drifted:
                if record.layer == "PROPHET":
                    raise WorkflowError(
                        "prophet change altered workflow state, evidence, model, SHA, or a task "
                        "that already exists"
                    )
                raise WorkflowError(
                    "planner changed workflow state, evidence, model, SHA, or a non-target "
                    "config field"
                )
        if outcome == "NO_CHANGE_REQUIRED" and changed:
            raise WorkflowError("NO_CHANGE_REQUIRED contradicts planner file changes")
        relative_root = Path("todo") / "amendments" / amendment_id
        _write_json(worktree / relative_root / "request.json", request)
        author = "prophet" if record.layer == "PROPHET" else "planner"
        _write_json(worktree / relative_root / f"{author}-{record.attempt:03d}.json", result)
        result_path.unlink()
        (worktree / ".workflow" / "amendment-request.json").unlink(missing_ok=True)
        if outcome == "BLOCKED":
            _git(worktree, "add", "-A")
            _git(worktree, "commit", "-m", f"chore(amendment): record {amendment_id} blocked")
            updated = replace(record, status="BLOCKED")
            self.save_amendment(updated)
            return updated
        _git(worktree, "diff", "--check")
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", f"docs({amendment_id.lower()}): owner amendment candidate")
        updated = AmendmentRecord(
            amendment_id=record.amendment_id,
            status="AWAITING_REVIEW",
            attempt=record.attempt,
            layer=record.layer,
            task_ids=record.task_ids,
            base_commit=record.base_commit,
            candidate_commit=_sha(worktree),
            branch=record.branch,
            worktree=record.worktree,
        )
        self.save_amendment(updated)
        return updated

    def _validate_amendment_result(self, result: Mapping[str, Any], amendment_id: str) -> str:
        _validate_object_keys(
            result,
            required={"amendment_id", "outcome", "summary", "rationale", "unresolved_questions"},
            label="amendment result",
        )
        if result.get("amendment_id") != amendment_id:
            raise WorkflowError("amendment result identity does not match")
        outcome = result.get("outcome")
        if outcome not in {"AMENDMENT_READY", "NO_CHANGE_REQUIRED", "BLOCKED"}:
            raise WorkflowError("amendment result outcome is invalid")
        if not isinstance(result.get("summary"), str) or not isinstance(
            result.get("rationale"), str
        ):
            raise WorkflowError("amendment summary and rationale must be strings")
        _validate_string_list(result.get("unresolved_questions"), "amendment unresolved_questions")
        return cast(str, outcome)

    def prepare_amendment_review(self, amendment_id: str) -> dict[str, object]:
        record = self.load_amendment(amendment_id)
        if record is None or record.status != "AWAITING_REVIEW" or record.candidate_commit is None:
            raise WorkflowError("amendment review requires an AWAITING_REVIEW candidate")
        worktree = Path(record.worktree)
        if (
            _sha(worktree) != record.candidate_commit
            or _git(worktree, "status", "--porcelain").stdout
        ):
            raise WorkflowError("amendment worktree must exactly match its clean candidate")
        review_worktree = (
            self.worktree_root / f"amendment-review-{amendment_id.lower()}-{record.attempt:03d}"
        )
        if review_worktree.exists():
            raise WorkflowError(f"amendment review worktree already exists: {review_worktree}")
        _git(
            self.repo, "worktree", "add", "--detach", str(review_worktree), record.candidate_commit
        )
        if record.layer == "PROPHET":
            subject = (
                f"Independently review PROPHET change {amendment_id}, which restates a goal, "
                "restructures the plan or corrects high-level documents. It targets no task."
            )
            reviewer = "prophet-reviewer"
        else:
            subject = (
                f"Independently review Owner amendment {amendment_id}. Target tasks: "
                f"{', '.join(record.task_ids)}."
            )
            reviewer = "plan-reviewer"
        prompt = (
            f"{subject} Base commit: {record.base_commit}. Candidate commit: "
            f"{record.candidate_commit}. Work only in {review_worktree}; do not edit planning "
            "files. Write the structured result only to "
            f"{review_worktree / '.workflow' / 'amendment-review-result.json'}."
        )
        return {
            **record.to_dict(),
            "agent": reviewer,
            "review_worktree": str(review_worktree),
            "prompt": prompt,
        }

    def finish_amendment_review(self, amendment_id: str) -> tuple[str, Path]:
        record = self.load_amendment(amendment_id)
        if record is None or record.candidate_commit is None:
            raise WorkflowError(f"{amendment_id} has no amendment candidate")
        review_worktree = (
            self.worktree_root / f"amendment-review-{amendment_id.lower()}-{record.attempt:03d}"
        )
        result_path = review_worktree / ".workflow" / "amendment-review-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"amendment review result is missing: {result_path}")
        result = _load_json(result_path)
        state = self._validate_amendment_review_result(result, record)
        if state == "APPROVED":
            self._ensure_clean_main()
        if [
            path
            for path in _working_tree_changes(review_worktree)
            if path != ".workflow/amendment-review-result.json"
        ]:
            raise WorkflowError("amendment reviewer left changes outside its handoff")
        if _sha(review_worktree) != record.candidate_commit:
            raise WorkflowError("amendment review worktree no longer matches candidate")
        worktree = Path(record.worktree)
        relative_root = Path("todo") / "amendments" / amendment_id
        review_name = f"review-{record.attempt:03d}"
        _write_json(worktree / relative_root / f"{review_name}.json", result)
        report = worktree / relative_root / f"{review_name}.md"
        report.write_text(_render_amendment_review(result), encoding="utf-8")
        _git(
            worktree,
            "add",
            str(relative_root / f"{review_name}.json"),
            str(relative_root / f"{review_name}.md"),
        )
        _git(worktree, "commit", "-m", f"chore(amendment): record {amendment_id} review")
        result_path.unlink()
        _git(self.repo, "worktree", "remove", str(review_worktree), check=False)
        if state == "APPROVED":
            _git(self.repo, "merge", "--ff-only", record.branch)
            _git(self.repo, "worktree", "remove", record.worktree)
            _git(self.repo, "branch", "-d", record.branch)
            self._amendment_path(amendment_id).unlink(missing_ok=True)
            self._amendment_request_path(amendment_id).unlink(missing_ok=True)
            return state, self.repo / relative_root / f"{review_name}.md"
        updated = replace(record, status=state)
        self.save_amendment(updated)
        return state, report

    def _validate_amendment_review_result(
        self, result: Mapping[str, Any], record: AmendmentRecord
    ) -> str:
        _validate_object_keys(
            result,
            required={
                "amendment_id",
                "base_commit",
                "candidate_commit",
                "verdict",
                "summary",
                "required_changes",
                "unknowns",
            },
            label="amendment review result",
        )
        if (
            result.get("amendment_id") != record.amendment_id
            or result.get("base_commit") != record.base_commit
            or result.get("candidate_commit") != record.candidate_commit
        ):
            raise WorkflowError("amendment review identity or commits do not match")
        verdict = result.get("verdict")
        required = result.get("required_changes")
        unknowns = result.get("unknowns")
        if verdict not in {"PASS", "FAIL", "BLOCKED"}:
            raise WorkflowError("amendment review verdict is invalid")
        if not isinstance(result.get("summary"), str):
            raise WorkflowError("amendment review summary must be a string")
        _validate_string_list(required, "amendment review required_changes")
        _validate_string_list(unknowns, "amendment review unknowns")
        if verdict == "PASS":
            if required or unknowns:
                raise WorkflowError("amendment review PASS contradicts unresolved findings")
            return "APPROVED"
        return "BLOCKED" if verdict == "BLOCKED" else "CHANGES_REQUESTED"

    def prepare_amendment_retry(self, amendment_id: str) -> dict[str, object]:
        record = self.load_amendment(amendment_id)
        if record is None or record.status not in {"CHANGES_REQUESTED", "BLOCKED"}:
            raise WorkflowError("amendment retry requires CHANGES_REQUESTED or resolved BLOCKED")
        worktree = Path(record.worktree)
        if _git(worktree, "status", "--porcelain").stdout:
            raise WorkflowError("amendment worktree must be clean before retry")
        updated = AmendmentRecord(
            amendment_id=record.amendment_id,
            status="PLANNING",
            attempt=record.attempt + 1,
            layer=record.layer,
            task_ids=record.task_ids,
            base_commit=_sha(worktree),
            candidate_commit=None,
            branch=record.branch,
            worktree=record.worktree,
        )
        self.save_amendment(updated)
        request = _load_json(self._amendment_request_path(amendment_id))
        request_path = worktree / ".workflow" / "amendment-request.json"
        _write_json(request_path, request)
        # The retry must be authored by the same role that authored the first
        # attempt. A PROPHET change is written by the prophet agent, whose writable
        # paths include documents the planner is not scoped to touch, so handing
        # its repair to the planner would produce a candidate the path rules then
        # reject -- a retry route that cannot succeed.
        if record.layer == "PROPHET":
            author = "prophet"
            scope = f"Repair PROPHET change {amendment_id} after independent review"
        else:
            author = "planner"
            scope = f"Repair Owner amendment {amendment_id} after independent review"
        prompt = (
            f"{scope}. Read {request_path} "
            f"and prior review under todo/amendments/{amendment_id}/review-{record.attempt:03d}.json. "
            f"Work only in {worktree}; preserve the layer and, for a task-targeted layer, the "
            f"target tasks. Write the structured "
            f"result only to {worktree / '.workflow' / 'amendment-result.json'}."
        )
        return {**updated.to_dict(), "agent": author, "prompt": prompt}

    def amendment_status(self, amendment_id: str) -> dict[str, object]:
        record = self.load_amendment(amendment_id)
        if record is not None:
            return record.to_dict()
        root = self.repo / "todo" / "amendments" / amendment_id
        reviews = sorted(root.glob("review-*.json")) if root.is_dir() else []
        if reviews and _load_json(reviews[-1]).get("verdict") == "PASS":
            return {"amendment_id": amendment_id, "status": "APPROVED"}
        raise WorkflowError(f"unknown owner amendment {amendment_id}")

    def prepare_maintenance(
        self,
        *,
        summary: str,
        reason: str,
        allowed_paths: Sequence[str],
        verification_commands: Sequence[str],
        related_task: str | None = None,
    ) -> dict[str, object]:
        """Prepare a low-risk repair without adding a numbered product task."""
        self._ensure_clean_main()
        if self._amendment_records():
            raise WorkflowError("cannot start maintenance while an owner amendment is unfinished")
        config = self.load_config()
        active = config.get("active_task")
        if isinstance(active, str) and config["tasks"][active]["status"] not in {
            "APPROVED",
            "PLANNED",
        }:
            raise WorkflowError(f"cannot start maintenance while {active} is unfinished")
        active_repairs = [
            record.maintenance_id
            for record in self._maintenance_records()
            if record.status in {"IN_DEVELOPMENT", "AWAITING_REVIEW", "CHANGES_REQUESTED"}
        ]
        if active_repairs:
            raise WorkflowError(
                "cannot start maintenance while another repair is active: "
                + ", ".join(active_repairs)
            )
        if related_task is not None:
            task = self._task(config, related_task)
            if task["status"] != "APPROVED":
                raise WorkflowError("maintenance may reference only an APPROVED task")
        paths = self._validate_maintenance_paths(allowed_paths)
        if (
            not verification_commands
            or len(verification_commands) > 8
            or not all(command.strip() for command in verification_commands)
        ):
            raise WorkflowError("maintenance requires between one and eight verification commands")
        maintenance_id = self._next_maintenance_id()
        base = _sha(self.repo)
        request: dict[str, object] = {
            "maintenance_id": maintenance_id,
            "summary": summary.strip(),
            "reason": reason.strip(),
            "allowed_paths": paths,
            "verification_commands": list(verification_commands),
            "related_task": related_task,
            "risk_attestation": "LOW_RISK_IMPLEMENTATION_DEFECT",
        }
        self._validate_maintenance_request(request)
        branch = f"maintenance/{maintenance_id.lower()}-attempt-001"
        worktree = self.worktree_root / f"maintenance-{maintenance_id.lower()}-attempt-001"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if worktree.exists():
            raise WorkflowError(f"maintenance worktree path already exists: {worktree}")
        _git(self.repo, "worktree", "add", "-b", branch, str(worktree), base)
        record = MaintenanceRecord(
            maintenance_id=maintenance_id,
            status="IN_DEVELOPMENT",
            attempt=1,
            base_commit=base,
            candidate_commit=None,
            branch=branch,
            development_worktree=str(worktree),
        )
        self.save_maintenance(record)
        _write_json(self._maintenance_request_path(maintenance_id), request)
        _write_json(worktree / ".workflow" / "maintenance-request.json", request)
        prompt = (
            f"Implement low-risk maintenance repair {maintenance_id}. Read the frozen request at "
            f"{worktree / '.workflow' / 'maintenance-request.json'}. Base commit: {base}. "
            f"Work only in {worktree}; change only the explicit allowed_paths and do not commit. "
            f"Run the listed verification commands once; deterministic generated artifacts may "
            f"require byte comparison, but ordinary test stdout does not. Write the standard "
            f"developer result to {worktree / '.workflow' / 'developer-result.json'} with "
            f"task_id={maintenance_id}. If the repair changes behavior, public interfaces, "
            "dependencies, Intent, Spec, safety policy, or requires another path, return "
            "TRIAGE_REQUIRED instead of widening scope."
        )
        return {**record.to_dict(), "agent": "stage-developer", "prompt": prompt}

    def finish_maintenance_develop(self, maintenance_id: str) -> MaintenanceRecord:
        record = self.load_maintenance(maintenance_id)
        if record is None:
            raise WorkflowError(f"unknown maintenance repair {maintenance_id}")
        if record.status != "IN_DEVELOPMENT":
            raise WorkflowError("maintenance development requires IN_DEVELOPMENT")
        worktree = Path(record.development_worktree)
        request = _load_json(self._maintenance_request_path(maintenance_id))
        self._validate_maintenance_request(request)
        result_path = worktree / ".workflow" / "developer-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"developer result is missing: {result_path}")
        result = _load_json(result_path)
        self._validate_developer_result(result, maintenance_id)
        if result["outcome"] == "CONTINUATION_REQUIRED":
            raise WorkflowError(
                "continuation handoff requires continue-maintenance-develop, not "
                "finish-maintenance-develop"
            )
        changed = [
            path
            for path in _working_tree_changes(worktree)
            if path
            not in {
                ".workflow/developer-result.json",
                ".workflow/developer-continuation.json",
                ".workflow/maintenance-request.json",
            }
        ]
        forbidden = sorted(set(changed) - set(request["allowed_paths"]))
        if forbidden:
            raise WorkflowError(
                "maintenance developer changed paths outside the request: " + ", ".join(forbidden)
            )
        if result["outcome"] != "CANDIDATE_READY":
            status = "ESCALATED" if result["outcome"] == "TRIAGE_REQUIRED" else "BLOCKED"
            updated = MaintenanceRecord(
                maintenance_id=record.maintenance_id,
                status=status,
                attempt=record.attempt,
                base_commit=record.base_commit,
                candidate_commit=record.candidate_commit,
                branch=record.branch,
                development_worktree=record.development_worktree,
                continuation_count=record.continuation_count,
            )
            self.save_maintenance(updated)
            (worktree / ".workflow" / "developer-continuation.json").unlink(missing_ok=True)
            self._continuation_path(maintenance_id).unlink(missing_ok=True)
            return updated
        if not changed:
            raise WorkflowError("maintenance developer produced no requested file change")
        relative_root = Path("todo") / "maintenance" / maintenance_id
        _write_json(worktree / relative_root / "request.json", request)
        _write_json(worktree / relative_root / f"developer-{record.attempt:03d}.json", result)
        result_path.unlink()
        (worktree / ".workflow" / "developer-continuation.json").unlink(missing_ok=True)
        (worktree / ".workflow" / "maintenance-request.json").unlink(missing_ok=True)
        _git(worktree, "diff", "--check")
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", f"fix({maintenance_id.lower()}): {request['summary']}")
        candidate = _sha(worktree)
        updated = MaintenanceRecord(
            maintenance_id=maintenance_id,
            status="AWAITING_REVIEW",
            attempt=record.attempt,
            base_commit=record.base_commit,
            candidate_commit=candidate,
            branch=record.branch,
            development_worktree=record.development_worktree,
            continuation_count=record.continuation_count,
        )
        self.save_maintenance(updated)
        self._continuation_path(maintenance_id).unlink(missing_ok=True)
        return updated

    def continue_maintenance_develop(
        self,
        maintenance_id: str,
        *,
        max_turns_exhausted: bool = False,
    ) -> dict[str, object]:
        """Start a fresh Developer in the same unfinished maintenance attempt."""
        record = self.load_maintenance(maintenance_id)
        if record is None:
            raise WorkflowError(f"unknown maintenance repair {maintenance_id}")
        if record.status != "IN_DEVELOPMENT" or record.candidate_commit is not None:
            raise WorkflowError(
                "maintenance continuation requires an unfinished IN_DEVELOPMENT attempt"
            )
        worktree = Path(record.development_worktree)
        request = _load_json(self._maintenance_request_path(maintenance_id))
        self._validate_maintenance_request(request)
        changed = [
            path for path in _working_tree_changes(worktree) if not path.startswith(".workflow/")
        ]
        forbidden = sorted(set(changed) - set(request["allowed_paths"]))
        if forbidden:
            raise WorkflowError(
                "maintenance developer changed paths outside the request: " + ", ".join(forbidden)
            )
        checkpoint = self._load_or_create_continuation_checkpoint(
            maintenance_id,
            worktree,
            max_turns_exhausted=max_turns_exhausted,
        )
        if record.continuation_count >= MAX_DEVELOPMENT_CONTINUATIONS:
            raise WorkflowError(
                f"development continuation limit reached for {maintenance_id}; preserve the "
                "worktree and ask the Owner whether to expand the cumulative budget or escalate"
            )
        updated = MaintenanceRecord(
            maintenance_id=record.maintenance_id,
            status=record.status,
            attempt=record.attempt,
            base_commit=record.base_commit,
            candidate_commit=None,
            branch=record.branch,
            development_worktree=record.development_worktree,
            continuation_count=record.continuation_count + 1,
        )
        self.save_maintenance(updated)
        _write_json(self._continuation_path(maintenance_id), checkpoint)
        checkpoint_path = worktree / ".workflow" / "developer-continuation.json"
        _write_json(checkpoint_path, checkpoint)
        (worktree / ".workflow" / "developer-result.json").unlink(missing_ok=True)
        prompt = (
            f"Continue low-risk maintenance repair {maintenance_id} in existing attempt "
            f"{record.attempt}. Read the frozen request at "
            f"{worktree / '.workflow' / 'maintenance-request.json'} and the prior checkpoint at "
            f"{checkpoint_path}. Inspect the actual git diff and test state because the worktree "
            f"is authoritative. Preserve the original allowed_paths and base {record.base_commit}. "
            f"Work only in {worktree}, do not commit, and write the standard developer result to "
            f"{worktree / '.workflow' / 'developer-result.json'} with task_id={maintenance_id}."
        )
        return {**updated.to_dict(), "agent": "stage-developer", "prompt": prompt}

    def prepare_maintenance_retry(self, maintenance_id: str) -> dict[str, object]:
        record = self.load_maintenance(maintenance_id)
        if record is None:
            raise WorkflowError(f"unknown maintenance repair {maintenance_id}")
        if record.status not in {"CHANGES_REQUESTED", "BLOCKED"}:
            raise WorkflowError("maintenance retry requires CHANGES_REQUESTED or resolved BLOCKED")
        worktree = Path(record.development_worktree)
        if _git(worktree, "status", "--porcelain").stdout:
            raise WorkflowError("maintenance worktree must be clean before retry")
        request = _load_json(self._maintenance_request_path(maintenance_id))
        self._validate_maintenance_request(request)
        updated = MaintenanceRecord(
            maintenance_id=maintenance_id,
            status="IN_DEVELOPMENT",
            attempt=record.attempt + 1,
            base_commit=record.base_commit,
            candidate_commit=None,
            branch=record.branch,
            development_worktree=record.development_worktree,
        )
        self.save_maintenance(updated)
        prompt = (
            f"Repair maintenance candidate {maintenance_id} after independent review. Read the "
            f"frozen request at {worktree / 'todo' / 'maintenance' / maintenance_id / 'request.json'} "
            f"and prior review at "
            f"{worktree / 'todo' / 'maintenance' / maintenance_id / f'review-{record.attempt:03d}.json'}. "
            f"This is attempt {updated.attempt}; preserve the original allowed_paths and base "
            f"{updated.base_commit}. Work only in {worktree}, do not commit, and write the standard "
            f"developer result to {worktree / '.workflow' / 'developer-result.json'} with "
            f"task_id={maintenance_id}."
        )
        return {**updated.to_dict(), "agent": "stage-developer", "prompt": prompt}

    def prepare_maintenance_review(self, maintenance_id: str) -> dict[str, object]:
        record = self.load_maintenance(maintenance_id)
        if record is None or record.candidate_commit is None:
            raise WorkflowError(f"{maintenance_id} has no maintenance candidate")
        if record.status != "AWAITING_REVIEW":
            raise WorkflowError("maintenance review requires AWAITING_REVIEW")
        worktree = Path(record.development_worktree)
        if _sha(worktree) != record.candidate_commit:
            raise WorkflowError("maintenance worktree no longer matches its candidate")
        if _git(worktree, "status", "--porcelain").stdout:
            raise WorkflowError("maintenance worktree must be clean before review")
        review_worktree = (
            self.worktree_root / f"review-{maintenance_id.lower()}-attempt-{record.attempt:03d}"
        )
        if review_worktree.exists():
            raise WorkflowError(f"review worktree path already exists: {review_worktree}")
        _git(
            self.repo,
            "worktree",
            "add",
            "--detach",
            str(review_worktree),
            record.candidate_commit,
        )
        request_path = f"todo/maintenance/{maintenance_id}/request.json"
        prompt = (
            f"Independently review low-risk maintenance repair {maintenance_id}. The frozen "
            f"request is {request_path}. Base commit: {record.base_commit}. Candidate commit: "
            f"{record.candidate_commit}. Work only in {review_worktree}. Verify the exact diff, "
            "the maintenance eligibility boundary, allowed paths, and listed commands once. "
            "Do not fix anything. Write the standard review result only to "
            f"{review_worktree / '.workflow' / 'review-result.json'} with "
            f"task_id={maintenance_id}."
        )
        return {
            **record.to_dict(),
            "agent": "stage-reviewer",
            "review_worktree": str(review_worktree),
            "prompt": prompt,
        }

    def finish_maintenance_review(self, maintenance_id: str) -> tuple[str, Path]:
        record = self.load_maintenance(maintenance_id)
        if record is None or record.candidate_commit is None:
            raise WorkflowError(f"{maintenance_id} has no maintenance candidate")
        review_worktree = (
            self.worktree_root / f"review-{maintenance_id.lower()}-attempt-{record.attempt:03d}"
        )
        result_path = review_worktree / ".workflow" / "review-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"review result is missing: {result_path}")
        result = _load_json(result_path)
        attempt = AttemptRecord(
            task_id=maintenance_id,
            phase="maintenance",
            attempt=record.attempt,
            base_commit=record.base_commit,
            candidate_commit=record.candidate_commit,
            branch=record.branch,
            development_worktree=record.development_worktree,
        )
        state = self._validate_review_result(result, attempt)
        reviewer_changes = [
            path
            for path in _working_tree_changes(review_worktree)
            if path != ".workflow/review-result.json"
        ]
        if reviewer_changes:
            raise WorkflowError("maintenance reviewer left changes outside its handoff")
        if _sha(review_worktree) != record.candidate_commit:
            raise WorkflowError("maintenance review worktree no longer matches candidate")
        worktree = Path(record.development_worktree)
        relative_root = Path("todo") / "maintenance" / maintenance_id
        review_name = f"review-{record.attempt:03d}"
        _write_json(worktree / relative_root / f"{review_name}.json", result)
        report = worktree / relative_root / f"{review_name}.md"
        report.write_text(_render_review(result), encoding="utf-8")
        _git(
            worktree,
            "add",
            str(relative_root / f"{review_name}.json"),
            str(relative_root / f"{review_name}.md"),
        )
        _git(worktree, "commit", "-m", f"chore(maintenance): record {maintenance_id} review")
        result_path.unlink()
        _git(self.repo, "worktree", "remove", str(review_worktree), check=False)
        if state == "APPROVED":
            self._ensure_clean_main()
            _git(self.repo, "merge", "--ff-only", record.branch)
            _git(self.repo, "worktree", "remove", record.development_worktree)
            _git(self.repo, "branch", "-d", record.branch)
            self._maintenance_path(maintenance_id).unlink(missing_ok=True)
            self._maintenance_request_path(maintenance_id).unlink(missing_ok=True)
            return "APPROVED", self.repo / relative_root / f"{review_name}.md"
        updated = MaintenanceRecord(
            maintenance_id=maintenance_id,
            status=state,
            attempt=record.attempt,
            base_commit=record.base_commit,
            candidate_commit=record.candidate_commit,
            branch=record.branch,
            development_worktree=record.development_worktree,
        )
        self.save_maintenance(updated)
        return state, report

    def maintenance_status(self, maintenance_id: str) -> dict[str, object]:
        record = self.load_maintenance(maintenance_id)
        if record is not None:
            return record.to_dict()
        root = self.repo / "todo" / "maintenance" / maintenance_id
        reviews = sorted(root.glob("review-*.json")) if root.is_dir() else []
        if reviews and _load_json(reviews[-1]).get("verdict") == "PASS":
            return {"maintenance_id": maintenance_id, "status": "APPROVED"}
        raise WorkflowError(f"unknown maintenance repair {maintenance_id}")

    def _validate_review_result(self, result: Mapping[str, Any], attempt: AttemptRecord) -> str:
        _validate_object_keys(
            result,
            required={
                "task_id",
                "base_commit",
                "candidate_commit",
                "verdict",
                "checks",
                "must_not_violations",
                "unknowns",
                "required_changes",
            },
            optional={"residual_risks", "triage_request"},
            label="review result",
        )
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
            _validate_object_keys(
                check,
                required={"id", "status", "evidence", "finding"},
                label="review check",
            )
            if not isinstance(check.get("id"), str) or not check["id"]:
                raise WorkflowError("review check id must be a non-empty string")
            _validate_string_list(check.get("evidence"), "review check evidence")
            if not isinstance(check.get("finding"), str):
                raise WorkflowError("review check finding must be a string")
            statuses.append(check["status"])
        violations = result.get("must_not_violations")
        unknowns = result.get("unknowns")
        required = result.get("required_changes")
        _validate_string_list(violations, "review must_not_violations")
        _validate_string_list(unknowns, "review unknowns")
        _validate_string_list(required, "review required_changes")
        if "residual_risks" in result:
            _validate_string_list(result.get("residual_risks"), "review residual_risks")
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
        if verdict == "BLOCKED":
            return "BLOCKED"
        if verdict == "FAIL":
            if "FAIL" not in statuses and not violations and not required:
                raise WorkflowError("FAIL must include a failed check or required change")
            return "CHANGES_REQUESTED"
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
        rendered_review = _render_review(result)
        reviewer_changes = [
            path
            for path in _working_tree_changes(review_worktree)
            if path != ".workflow/review-result.json"
        ]
        if reviewer_changes:
            raise WorkflowError("reviewer left changes outside its result handoff")
        if _sha(review_worktree) != attempt.candidate_commit:
            raise WorkflowError("review worktree no longer matches candidate commit")
        recorded = self._record_review_result(attempt, result, state, rendered_review)
        result_path.unlink()
        _git(self.repo, "worktree", "remove", str(review_worktree), check=False)
        return recorded

    def _record_review_result(
        self,
        attempt: AttemptRecord,
        result: Mapping[str, Any],
        state: str,
        rendered_review: str | None = None,
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
        (worktree / relative_md).write_text(
            rendered_review if rendered_review is not None else _render_review(result),
            encoding="utf-8",
        )
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
        _validate_object_keys(
            result,
            required={
                "task_id",
                "issue_commit",
                "classification",
                "summary",
                "evidence",
                "recommended_action",
            },
            optional={"owner_question"},
            label="triage result",
        )
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
        _validate_string_list(result.get("evidence"), "triage result evidence")
        if not isinstance(result.get("recommended_action"), str):
            raise WorkflowError("triage result recommended_action must be a string")
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
                # Intent is reachable on this route only to transcribe the Owner's
                # mandatory decision. Authoring a goal belongs to the PROPHET layer,
                # which is a different role with a different review.
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
        _validate_object_keys(
            result,
            required={"task_id", "outcome", "summary", "rationale", "unresolved_questions"},
            label="planner result",
        )
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
        _validate_string_list(result.get("unresolved_questions"), "planner unresolved_questions")
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
        _validate_object_keys(
            result,
            required={
                "task_id",
                "base_commit",
                "candidate_commit",
                "verdict",
                "summary",
                "required_changes",
                "unknowns",
            },
            label="plan review result",
        )
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
        if not isinstance(result.get("summary"), str):
            raise WorkflowError("plan review summary must be a string")
        _validate_string_list(required, "plan review required_changes")
        _validate_string_list(unknowns, "plan review unknowns")
        if verdict == "PASS":
            if required or unknowns:
                raise WorkflowError("plan review PASS contradicts unresolved findings")
            return "CHANGES_REQUESTED"
        if verdict == "BLOCKED":
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
