"""Plain-language renderer for the on-demand plan view (T008).

The renderer is intentionally read-only: every fact it prints comes
from the committed configuration at run time, and it never writes a
file or caches a result. The contract requires that the output
cover every configured phase and task exactly once, split the
approved set into live and superseded buckets, name blocked /
changes-requested / triage-required / owner-decision tasks as
such, and surface the same ``ready`` condition the controller uses
to choose the next task.

A renderer bug that printed a stored SHA, a hard-coded test count,
or a wall-clock timestamp would be a contract violation. The
``View`` dataclass and the ``render`` function therefore take only
the parsed configuration plus the repository root and return a
single string; nothing else.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .check import (
    CLOSED_WITHOUT_DELIVERY,
    NOTABLE_STATES,
    PHASE_PATTERN,
    PLAN_STATES,
    ProgressIssue,
    is_ready,
    project_state,
)

#: Width of the divider drawn under each top-level section header.
#: Short enough to fit a 100-column terminal, long enough to be
#: visible without dominating the page.
SECTION_RULE = "=" * 78

#: Width of the sub-section rule drawn under each phase heading.
PHASE_RULE = "-" * 78


@dataclass(frozen=True)
class TaskView:
    """One task as the renderer sees it.

    ``title`` and ``purpose`` come straight from the contract file —
    the renderer never paraphrases or restates them. ``phase`` and
    ``status`` come from the configuration. ``superseded_by`` is the
    configured successor (or ``None``) and ``ready`` reports whether
    the task would pass the controller's ``ready`` check.
    """

    task_id: str
    title: str
    purpose: str
    phase: str
    status: str
    superseded_by: str | None
    ready: bool


@dataclass(frozen=True)
class PhaseView:
    """One phase's purpose plus the tasks that belong to it.

    Tasks are kept in the order they appear in the configuration so
    a deterministic renderer produces a deterministic view.
    """

    phase_id: str
    purpose: str
    tasks: tuple[TaskView, ...]


def _read_text(repo_root: Path, task_file: str) -> str:
    return (repo_root / task_file).read_text(encoding="utf-8")


_TITLE_RE = re.compile(r"^# (T\d{3}) — (.+?)\s*$", re.MULTILINE)
#: ``## Outcome`` through the next ``## `` heading or end of file.
_OUTCOME_RE = re.compile(
    r"## Outcome\s*\n+(.*?)(?=\n## |\Z)",
    re.DOTALL,
)
#: ``**Purpose:**`` followed by a paragraph that runs until the next
#: bold key (``**Entry:**``, ``**Exit gate:**`` ...) or a blank line.
_PURPOSE_RE = re.compile(
    r"\*\*Purpose:\*\*\s*\n?((?:[^\n]*\n)*?)(?=\n\*\*[A-Z]|\n\n|\Z)",
)


def parse_title(contract: str, task_id: str) -> str:
    """Extract the H1 title from a contract body.

    Falls back to ``"<missing title>"`` only when the contract is so
    malformed the title cannot be read; the upstream validator has
    already confirmed the file exists.
    """

    match = _TITLE_RE.search(contract)
    if match is None:
        return "<missing title>"
    parsed_id, title = match.group(1), match.group(2).strip()
    if parsed_id != task_id:
        # The contract is for a different task than the configuration
        # claims. Surface the configured identifier rather than the
        # file's stray heading so the operator sees the real mismatch.
        return title
    return title


def parse_outcome(contract: str) -> str:
    """Return the body of the contract's ``## Outcome`` section.

    The renderer prints the outcome verbatim — re-stating it would
    violate the contract's "from its own contract, not restated in
    the renderer" rule and would let a renderer bug lie about what
    the work is for.
    """

    match = _OUTCOME_RE.search(contract)
    if match is None:
        return "<outcome not declared>"
    return match.group(1).strip()


def parse_phase_purpose(readme: str) -> str:
    """Return the first paragraph after ``**Purpose:**`` in a phase README."""

    match = _PURPOSE_RE.search(readme)
    if match is None:
        return "<purpose not declared>"
    text = match.group(1).strip()
    # Collapse internal blank lines so the view stays single-paragraph
    # without dropping content.
    text = re.sub(r"\n\s*\n", " ", text)
    return text


def _phase_readme(repo_root: Path, phase_id: str) -> str | None:
    matches = sorted(repo_root.glob(f"todo/phases/{phase_id}-*/README.md"))
    if not matches:
        return None
    return matches[0].read_text(encoding="utf-8")


def _build_task_view(
    task_id: str,
    task: Mapping[str, Any],
    *,
    repo_root: Path,
    tasks: Mapping[str, Any],
    config: Mapping[str, Any],
) -> TaskView:
    contract = _read_text(repo_root, str(task["task_file"]))
    title = parse_title(contract, task_id)
    purpose = parse_outcome(contract)
    superseded_by_raw = task.get("superseded_by")
    superseded_by = superseded_by_raw if isinstance(superseded_by_raw, str) else None
    status = project_state(task_id, task, config=config)
    return TaskView(
        task_id=task_id,
        title=title,
        purpose=purpose,
        phase=str(task.get("phase", "")),
        status=status,
        superseded_by=superseded_by,
        ready=is_ready(task_id, task, tasks),
    )


def build_view(config: Mapping[str, Any], *, repo_root: Path) -> list[PhaseView]:
    """Materialise the renderer's structured view of the plan.

    The function returns a list of ``PhaseView`` records so the caller
    can format it however it wants. It performs no I/O outside the
    repository, writes nothing, and reads every fact it needs from the
    configuration it received plus the contract files at run time.
    """

    tasks: Mapping[str, Any] = config.get("tasks", {})
    if not isinstance(tasks, Mapping):
        raise ProgressIssue(
            code="TASKS_NOT_OBJECT",
            target="",
            message="plan configuration must contain a 'tasks' object",
        )

    # Group tasks by phase so a single phase can show its full
    # membership without re-scanning the configuration.
    grouped: dict[str, list[str]] = {}
    for task_id, task in tasks.items():
        if not isinstance(task, Mapping):
            continue
        phase = task.get("phase")
        if not isinstance(phase, str) or not PHASE_PATTERN.fullmatch(phase):
            # Validation already reported the offending phase; skip
            # here so the renderer doesn't crash on a corrupt entry.
            continue
        grouped.setdefault(phase, []).append(str(task_id))

    # Tasks within a phase preserve the configuration order, which is
    # the declaration order the operator already understands.
    phases: list[PhaseView] = []
    for phase_id in sorted(grouped):
        readme = _phase_readme(repo_root, phase_id)
        purpose = parse_phase_purpose(readme) if readme else "<purpose not declared>"
        task_views: list[TaskView] = []
        for task_id in grouped[phase_id]:
            task = tasks[task_id]
            task_views.append(
                _build_task_view(
                    task_id,
                    task,
                    repo_root=repo_root,
                    tasks=tasks,
                    config=config,
                )
            )
        phases.append(
            PhaseView(
                phase_id=phase_id,
                purpose=purpose,
                tasks=tuple(task_views),
            )
        )
    return phases


def _format_task_line(task: TaskView, *, indent: str) -> str:
    status_marker = task.status
    if task.superseded_by is not None:
        status_marker = f"SUPERSEDED by {task.superseded_by}"
    return f"{indent}{task.task_id} — {task.title} [{status_marker}]"


def _format_task_block(task: TaskView, *, indent: str = "    ") -> list[str]:
    """Render one task with its purpose, phase and status metadata."""

    lines: list[str] = []
    lines.append(_format_task_line(task, indent=indent))
    purpose_indent = indent + "  for: "
    first, *rest = task.purpose.splitlines()
    lines.append(f"{purpose_indent}{first}")
    continuation_indent = " " * len(purpose_indent)
    for chunk in rest:
        lines.append(f"{continuation_indent}{chunk}")
    phase_line = f"{indent}  phase: {task.phase}    status: {task.status}"
    if task.superseded_by is not None:
        phase_line += f"    superseded by: {task.superseded_by}"
    lines.append(phase_line)
    return lines


def render(config: Mapping[str, Any], *, repo_root: Path) -> str:
    """Return the full plain-language view as a single string.

    The function is total: it raises ``ProgressIssue`` only for the
    classes of defect the upstream validator catches, so a corrupt
    plan never produces a half-rendered report. A clean plan returns
    a deterministic string with no SHA-shaped token, no test count
    and no timestamp.
    """

    phases = build_view(config, repo_root=repo_root)

    # Flatten the tasks once so the bucketed sections below don't have
    # to re-walk the phases in three different ways.
    all_tasks: list[TaskView] = [task for phase in phases for task in phase.tasks]

    live_approved = [
        task for task in all_tasks if task.status == "APPROVED" and task.superseded_by is None
    ]
    superseded = [
        task for task in all_tasks if task.status == "APPROVED" and task.superseded_by is not None
    ]
    in_progress = [
        task
        for task in all_tasks
        if task.status not in {"APPROVED", "PLANNED"} | CLOSED_WITHOUT_DELIVERY
        and task.superseded_by is None
    ]
    abandoned = [
        task
        for task in all_tasks
        if task.status in CLOSED_WITHOUT_DELIVERY and task.superseded_by is None
    ]
    planned = [
        task for task in all_tasks if task.status == "PLANNED" and task.superseded_by is None
    ]
    notable = [
        task for task in all_tasks if task.status in NOTABLE_STATES and task.superseded_by is None
    ]
    ready_next = [task for task in planned if task.ready]

    lines: list[str] = []
    lines.append(f"Plan: {config.get('project', '')}")
    active_phase = config.get("active_phase") or "<none>"
    active_task = config.get("active_task") or "<none>"
    lines.append(f"Active phase: {active_phase}")
    lines.append(f"Active task: {active_task}")
    workflow_state = config.get("workflow_state") or "<none>"
    lines.append(f"Workflow state: {workflow_state}")
    intent_revision = config.get("intent_revision") or "<none>"
    spec_revision = config.get("spec_revision") or "<none>"
    lines.append(f"Intent revision: {intent_revision}")
    lines.append(f"Spec revision: {spec_revision}")
    lines.append(SECTION_RULE)

    lines.append("Phases")
    for phase in phases:
        lines.append(f"{phase.phase_id} — purpose: {phase.purpose}")
        lines.append(PHASE_RULE)
        for task in phase.tasks:
            lines.extend(_format_task_block(task))
        lines.append("")

    lines.append(SECTION_RULE)
    lines.append("Approved (live)")
    if live_approved:
        for task in live_approved:
            lines.append(f"  {task.task_id} — {task.title}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Approved (superseded)")
    if superseded:
        for task in superseded:
            lines.append(f"  {task.task_id} — {task.title}  ->  succeeded by {task.superseded_by}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("In progress")
    if in_progress:
        for task in in_progress:
            lines.extend(_format_task_block(task, indent="  "))
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Abandoned (closed without delivery)")
    if abandoned:
        for task in abandoned:
            lines.append(f"  {task.task_id} — {task.title}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Planned")
    if planned:
        for task in planned:
            lines.extend(_format_task_block(task, indent="  "))
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Blocked / changes-requested / triage-required / owner-decision")
    if notable:
        for task in notable:
            label = _notable_label(task.status)
            lines.append(f"  {task.task_id} — {label}: {task.title}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Ready next (PLANNED, dependencies all APPROVED, none superseded)")
    if ready_next:
        for task in ready_next:
            lines.append(f"  {task.task_id} — {task.title}")
    else:
        lines.append("  (none)")
    lines.append("")

    return "\n".join(lines)


def _notable_label(status: str) -> str:
    """Map a status to the human label the renderer prints beside it."""

    return {
        "BLOCKED": "blocked",
        "CHANGES_REQUESTED": "changes-requested",
        "TRIAGE_REQUIRED": "triage-required",
        "OWNER_DECISION_REQUIRED": "owner-decision",
    }[status]


__all__ = [
    "NOTABLE_STATES",
    "PHASE_RULE",
    "PLAN_STATES",
    "PhaseView",
    "ProgressIssue",
    "SECTION_RULE",
    "TaskView",
    "build_view",
    "is_ready",
    "parse_outcome",
    "parse_phase_purpose",
    "parse_title",
    "render",
]
