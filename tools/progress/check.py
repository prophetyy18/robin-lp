"""Validation checks for the on-demand plan renderer (T008).

The renderer must fail closed rather than render a view it cannot
substantiate. Every fact the renderer prints must come from
``todo/config.yaml`` at run time, and the configuration must satisfy
four closed conditions:

1. it can be read and parsed as JSON;
2. every task's ``status`` is a member of the plan's state machine;
3. every dependency and supersession pointer resolves to a configured
   task (and the supersession pointer targets an ``APPROVED`` task);
4. every configured task has a contract file on disk.

Each failing condition is reported as a ``ProgressIssue`` whose
``message`` is a plain-language explanation suitable for printing to
the operator. The CLI converts the issues into a non-zero exit code
in both ``render`` and ``--check`` modes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The full plan state machine, kept in step with ``tools.workflow``. The
#: renderer must never report a status the controller cannot record, so any
#: unknown value is a configuration defect.
PLAN_STATES: frozenset[str] = frozenset(
    {
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
        "ABANDONED",
    }
)

#: The two facts the composite status is built from. The tool stays independent
#: of ``tools.workflow`` -- it is a product tool, and the controller is a
#: governance surface -- so the values are stated here and the equivalence with
#: the controller's projection is asserted in the tests rather than shared as
#: code.
LIFECYCLE_VALUES: frozenset[str] = frozenset({"OPEN", "DELIVERED", "ABANDONED"})


def project_state(task_id: str, task: Mapping[str, Any], *, config: Mapping[str, Any]) -> str:
    """The state a task record is in.

    ``status`` used to say this. It is retired: a revision that still carries one
    is reported as recorded, and otherwise the state is projected from the facts
    the controller writes. A task that holds the single-active-work lane is the
    active task -- nothing else may claim it -- so the config's own declaration
    of its active task's state is a fact about this very record.
    """

    recorded = task.get("status")
    if isinstance(recorded, str) and recorded in PLAN_STATES:
        return recorded
    lifecycle = task.get("lifecycle")
    if lifecycle == "DELIVERED":
        return "APPROVED"
    if lifecycle == "ABANDONED":
        return "ABANDONED"
    if lifecycle == "OPEN" and task.get("claimed") is True:
        declared = config.get("workflow_state")
        if config.get("active_task") == task_id and declared in PLAN_STATES:
            return str(declared)
        return "READY"
    return "PLANNED"


#: Statuses that close a work item without delivering it. Like a superseded
#: task, an abandoned one is not live work: it must not be counted as
#: in-progress, and `is_ready` already excludes it because it is not `PLANNED`.
CLOSED_WITHOUT_DELIVERY: frozenset[str] = frozenset({"ABANDONED"})

#: The set of statuses that the renderer prints under the
#: "blocked / changes-requested / triage-required / owner-decision"
#: heading. Tasks in these states are named as such so the reader can
#: see which work has stopped and why without reading the config.
NOTABLE_STATES: frozenset[str] = frozenset(
    {
        "BLOCKED",
        "CHANGES_REQUESTED",
        "TRIAGE_REQUIRED",
        "OWNER_DECISION_REQUIRED",
    }
)

#: Task identifier pattern: three digits prefixed by ``T``. Matches
#: ``tools.workflow.TASK_PATTERN`` so the renderer accepts the same
#: identifiers the controller does.
TASK_PATTERN: re.Pattern[str] = re.compile(r"^T\d{3}$")

#: Phase identifier pattern: two digits prefixed by ``P``.
PHASE_PATTERN: re.Pattern[str] = re.compile(r"^P\d{2}$")


@dataclass(frozen=True)
class ProgressIssue(Exception):
    """A single configuration defect the renderer refuses to ignore.

    ``code`` is a short stable identifier for the failure class so the
    tests can match against it without scraping the message text.
    ``target`` is the task identifier the issue names (or the empty
    string for whole-config issues). ``message`` is the plain-language
    explanation the CLI prints verbatim. The class subclasses
    ``Exception`` so the loader can ``raise`` it directly when a
    configuration is unreadable, and the validator can return a list
    of the same type for the runnable checks.
    """

    code: str
    target: str
    message: str


def load_config(repo_root: Path, *, config_path: Path | None = None) -> dict[str, Any]:
    """Read and parse ``todo/config.yaml`` for the renderer.

    Raises ``ProgressIssue`` (the renderer's own signal type) when the
    file is missing or unparsable so the CLI can convert either into a
    non-zero exit with a plain-language message.
    """

    candidate = config_path or (repo_root / "todo" / "config.yaml")
    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError as error:
        raise ProgressIssue(
            code="CONFIG_UNREADABLE",
            target="",
            message=f"cannot read plan configuration at {candidate}: {error.strerror or error}",
        ) from error
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProgressIssue(
            code="CONFIG_UNPARSABLE",
            target="",
            message=(
                f"plan configuration at {candidate} is not valid JSON: "
                f"{error.msg} at line {error.lineno} column {error.colno}"
            ),
        ) from error
    if not isinstance(parsed, dict):
        raise ProgressIssue(
            code="CONFIG_NOT_OBJECT",
            target="",
            message=f"plan configuration at {candidate} must be a JSON object",
        )
    return parsed


def _read_contract(repo_root: Path, task_file: str, task_id: str) -> str:
    contract_path = repo_root / task_file
    try:
        return contract_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProgressIssue(
            code="CONTRACT_MISSING",
            target=task_id,
            message=(
                f"task {task_id} contract file {task_file} cannot be read: "
                f"{error.strerror or error}"
            ),
        ) from error


def validate(config: Mapping[str, Any], *, repo_root: Path) -> list[ProgressIssue]:
    """Run every fail-closed check the renderer relies on.

    The checks are independent: every issue is reported so the CLI can
    surface the full list to the operator in one pass. The renderer's
    own view-building step raises on the first issue it encounters so
    a corrupt plan never produces a half-rendered report.
    """

    issues: list[ProgressIssue] = []
    tasks = config.get("tasks")
    if not isinstance(tasks, Mapping):
        issues.append(
            ProgressIssue(
                code="TASKS_NOT_OBJECT",
                target="",
                message="plan configuration must contain a 'tasks' object",
            )
        )
        return issues

    # Index tasks by identifier so dependency and supersession pointers
    # can be resolved without scanning the mapping twice.
    task_ids: set[str] = set()
    for task_id in tasks:
        if not isinstance(task_id, str) or not TASK_PATTERN.fullmatch(task_id):
            issues.append(
                ProgressIssue(
                    code="TASK_ID_INVALID",
                    target=str(task_id),
                    message=(f"task identifier {task_id!r} must match the pattern T###"),
                )
            )
            continue
        task_ids.add(task_id)

    for task_id, task in tasks.items():
        if not isinstance(task, Mapping):
            issues.append(
                ProgressIssue(
                    code="TASK_NOT_OBJECT",
                    target=str(task_id),
                    message=f"task {task_id} must be a JSON object",
                )
            )
            continue
        if not isinstance(task_id, str) or not TASK_PATTERN.fullmatch(task_id):
            # Already recorded as TASK_ID_INVALID above; skip the rest.
            continue

        # A revision that still carries the retired composite must carry a
        # valid one; a revision written after the retirement carries the two
        # facts instead, and those are what a reader projects from.
        status = task.get("status")
        if status is not None and (not isinstance(status, str) or status not in PLAN_STATES):
            issues.append(
                ProgressIssue(
                    code="STATUS_OUTSIDE_STATE_MACHINE",
                    target=task_id,
                    message=(
                        f"task {task_id} status {status!r} is outside the plan's "
                        f"state machine; allowed values: {sorted(PLAN_STATES)}"
                    ),
                )
            )
        lifecycle = task.get("lifecycle")
        if lifecycle not in LIFECYCLE_VALUES:
            issues.append(
                ProgressIssue(
                    code="LIFECYCLE_INVALID",
                    target=task_id,
                    message=(
                        f"task {task_id} lifecycle {lifecycle!r} is not one of "
                        f"{sorted(LIFECYCLE_VALUES)}"
                    ),
                )
            )
        if not isinstance(task.get("claimed"), bool):
            issues.append(
                ProgressIssue(
                    code="CLAIMED_INVALID",
                    target=task_id,
                    message=f"task {task_id} 'claimed' must be a boolean",
                )
            )

        task_file = task.get("task_file")
        if not isinstance(task_file, str) or not task_file:
            issues.append(
                ProgressIssue(
                    code="TASK_FILE_MISSING",
                    target=task_id,
                    message=f"task {task_id} must declare a non-empty 'task_file'",
                )
            )
        else:
            contract_path = repo_root / task_file
            if not contract_path.is_file():
                issues.append(
                    ProgressIssue(
                        code="CONTRACT_MISSING",
                        target=task_id,
                        message=(f"task {task_id} contract file {task_file} does not exist"),
                    )
                )
            else:
                try:
                    _read_contract(repo_root, task_file, task_id)
                except ProgressIssue as issue:
                    issues.append(issue)

        depends_on = task.get("depends_on")
        if not isinstance(depends_on, list):
            issues.append(
                ProgressIssue(
                    code="DEPENDS_ON_INVALID",
                    target=task_id,
                    message=f"task {task_id} 'depends_on' must be a list of task identifiers",
                )
            )
        else:
            for dependency in depends_on:
                if not isinstance(dependency, str) or not TASK_PATTERN.fullmatch(dependency):
                    issues.append(
                        ProgressIssue(
                            code="DEPENDS_ON_INVALID",
                            target=task_id,
                            message=(
                                f"task {task_id} depends_on entry {dependency!r} "
                                f"must match the pattern T###"
                            ),
                        )
                    )
                    continue
                if dependency not in task_ids:
                    issues.append(
                        ProgressIssue(
                            code="DEPENDENCY_UNRESOLVED",
                            target=task_id,
                            message=(
                                f"task {task_id} depends on {dependency}, which is "
                                f"not declared in the plan configuration"
                            ),
                        )
                    )

        superseded_by = task.get("superseded_by")
        if superseded_by is not None:
            if not isinstance(superseded_by, str) or not TASK_PATTERN.fullmatch(superseded_by):
                issues.append(
                    ProgressIssue(
                        code="SUPERSEDED_BY_INVALID",
                        target=task_id,
                        message=(
                            f"task {task_id} superseded_by {superseded_by!r} "
                            f"must match the pattern T###"
                        ),
                    )
                )
            elif superseded_by == task_id:
                issues.append(
                    ProgressIssue(
                        code="SUPERSEDED_BY_SELF",
                        target=task_id,
                        message=f"task {task_id} superseded_by must not reference itself",
                    )
                )
            elif superseded_by not in task_ids:
                issues.append(
                    ProgressIssue(
                        code="SUPERSESSION_UNRESOLVED",
                        target=task_id,
                        message=(
                            f"task {task_id} superseded_by {superseded_by} does "
                            f"not name a configured task"
                        ),
                    )
                )
            elif task.get("lifecycle") != "DELIVERED":
                issues.append(
                    ProgressIssue(
                        code="SUPERSEDED_BUT_NOT_APPROVED",
                        target=task_id,
                        message=(
                            f"task {task_id} is superseded_by {superseded_by} "
                            f"but is not APPROVED (found {project_state(task_id, task, config=config)!r})"
                        ),
                    )
                )

    return issues


def is_ready(task_id: str, task: Mapping[str, Any], tasks: Mapping[str, Any]) -> bool:
    """Return whether ``task`` matches the ``ready`` condition.

    The condition mirrors the controller's ``_check_dependencies``:

    - the task is currently ``PLANNED`` — nobody has selected it, and it
      has not started;
    - every entry in ``depends_on`` is ``APPROVED``;
    - no entry in ``depends_on`` carries a ``superseded_by`` pointer,
      unless the task itself is the named successor.

    Both facts are read from the decomposition, which is what the controller
    routes on: ``PLANNED`` means the lifecycle is open and no lane is claimed,
    and a delivered dependency is one whose lifecycle says so.

    Anything else — including a missing dependency entry — keeps the
    task out of the ready-next set, because the controller would
    refuse it for the same reason.
    """

    if task.get("lifecycle") != "OPEN" or task.get("claimed") is not False:
        return False
    depends_on = task.get("depends_on")
    if not isinstance(depends_on, list):
        return False
    for dependency in depends_on:
        if not isinstance(dependency, str):
            return False
        other = tasks.get(dependency)
        if not isinstance(other, Mapping):
            return False
        if other.get("lifecycle") != "DELIVERED":
            return False
        superseded_by = other.get("superseded_by")
        if superseded_by is not None and superseded_by != task_id:
            return False
    return True


__all__ = [
    "CLOSED_WITHOUT_DELIVERY",
    "LIFECYCLE_VALUES",
    "NOTABLE_STATES",
    "PHASE_PATTERN",
    "PLAN_STATES",
    "ProgressIssue",
    "TASK_PATTERN",
    "is_ready",
    "load_config",
    "project_state",
    "validate",
]
