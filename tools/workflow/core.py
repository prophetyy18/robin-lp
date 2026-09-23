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
from typing import Any, Protocol, cast

from tools import check_acceptance, check_citations, check_imports

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TASK_PATTERN = re.compile(r"^T[0-9]{3}$")
MAINTENANCE_PATTERN = re.compile(r"^M[0-9]{4}$")
AMENDMENT_PATTERN = re.compile(r"^A[0-9]{4}$")
IMPACT_PATTERN = re.compile(r"^A[0-9]{4}:T[0-9]{3}:[a-z0-9][a-z0-9-]*$")
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

#: Amendment states that close a change. A closed record is kept rather than
#: deleted: the ID stays consumed (so it is never handed out twice) while the
#: lane is free for the next change. Without this, a failed change whose own
#: layer cannot repair it blocked every later amendment, and deleting the record
#: to clear the block rewound the ID counter onto a name still in use.
TERMINAL_AMENDMENT_STATES = frozenset({"APPROVED", "ABANDONED"})
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
    # The Owner's exit for work that will not land. A work item can get stuck
    # for reasons no role in its lane can resolve -- an agent that died without
    # writing its handoff leaves a worktree no command will accept, and an
    # external dependency can deny progress indefinitely. Without this value
    # every such state was a trap: the only escape was forward progress, which
    # is exactly what failed. `APPROVED` keeps its annotation-only SUPERSEDE
    # route and is deliberately NOT abandonable, so approval evidence can never
    # be rewritten by a later abandonment.
    "ABANDONED",
}

#: Every non-terminal task state can be closed by the Owner. The edge is drawn
#: from every state on purpose: an exit that depends on which sub-state the work
#: item is stuck in would reintroduce the trap this closes. `APPROVED` has no
#: outgoing edge -- approved work is retired by the annotation-only `SUPERSEDE`
#: route, never by rewriting its status.
ALLOWED_TRANSITIONS = {
    "PLANNED": {"READY", "ABANDONED"},
    "READY": {"IN_DEVELOPMENT", "BLOCKED", "ABANDONED"},
    "IN_DEVELOPMENT": {"AWAITING_REVIEW", "TRIAGE_REQUIRED", "BLOCKED", "ABANDONED"},
    "AWAITING_REVIEW": {
        "APPROVED",
        "CHANGES_REQUESTED",
        "TRIAGE_REQUIRED",
        "BLOCKED",
        "ABANDONED",
    },
    "CHANGES_REQUESTED": {"IN_DEVELOPMENT", "BLOCKED", "ABANDONED"},
    "TRIAGE_REQUIRED": {
        "CHANGES_REQUESTED",
        "PLANNING",
        "OWNER_DECISION_REQUIRED",
        "BLOCKED",
        "ABANDONED",
    },
    "PLANNING": {
        "AWAITING_PLAN_REVIEW",
        "OWNER_DECISION_REQUIRED",
        "BLOCKED",
        "ABANDONED",
    },
    "AWAITING_PLAN_REVIEW": {
        "CHANGES_REQUESTED",
        "PLANNING",
        "PLAN_REVIEW_BLOCKED",
        "ABANDONED",
    },
    "PLAN_REVIEW_BLOCKED": {
        "CHANGES_REQUESTED",
        "PLANNING",
        "PLAN_REVIEW_BLOCKED",
        "ABANDONED",
    },
    "OWNER_DECISION_REQUIRED": {"PLANNING", "BLOCKED", "ABANDONED"},
    "BLOCKED": {"READY", "ABANDONED"},
    "APPROVED": set(),
    "ABANDONED": set(),
}

#: Task statuses that do not occupy the single-active-work lane. `ABANDONED` is
#: here for the same reason `APPROVED` is: a closed work item must not block the
#: next one. Leaving it out would have let an abandonment keep the lane forever.
RESTING_STATES = frozenset({"PLANNED", "APPROVED", "ABANDONED"})

OPEN = "OPEN"
DELIVERED = "DELIVERED"
LIFECYCLE_VALUES = frozenset({OPEN, DELIVERED, "ABANDONED"})

#: The one place the composite status is decomposed. Everything else reads this
#: table or the projection built from it, so the two can never be written
#: independently. `lifecycle` is the task's own state; `claimed` is whether it
#: holds the single-active-work lane. Every in-flight status is claimed, and both
#: terminal statuses release the lane -- a closed work item must never keep it.
LIFECYCLE_OF: dict[str, tuple[str, bool]] = {
    "PLANNED": (OPEN, False),
    "READY": (OPEN, True),
    "IN_DEVELOPMENT": (OPEN, True),
    "AWAITING_REVIEW": (OPEN, True),
    "CHANGES_REQUESTED": (OPEN, True),
    "TRIAGE_REQUIRED": (OPEN, True),
    "PLANNING": (OPEN, True),
    "AWAITING_PLAN_REVIEW": (OPEN, True),
    "PLAN_REVIEW_BLOCKED": (OPEN, True),
    "OWNER_DECISION_REQUIRED": (OPEN, True),
    "BLOCKED": (OPEN, True),
    "APPROVED": (DELIVERED, False),
    "ABANDONED": ("ABANDONED", False),
}

#: Statuses a task can never leave, and therefore can never satisfy a dependency
#: again. A task depending on one of these is stranded: `_check_dependencies`
#: requires every dependency to be `APPROVED`, so activation is impossible and
#: no route can repair it. `PLANNED` is deliberately NOT here -- a planned task
#: is still reachable, and abandoning its dependency is what would strand it.
CLOSED_TASK_STATES = frozenset({"APPROVED", "ABANDONED"})

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
    # `ESCALATED` is reached when the maintenance Developer reports a
    # `TRIAGE_REQUIRED` outcome: the repair does not fit this lane, and the
    # documented route is a new numbered task or normal triage. Nothing in the
    # lane could close the record, so it sat forever -- `prepare-maintenance-retry`
    # accepts only `CHANGES_REQUESTED` or `BLOCKED`. `ABANDONED` is the Owner's
    # exit for it, and for any other maintenance state that will not land.
    "ABANDONED",
}

#: Maintenance statuses that close the lane.
TERMINAL_MAINTENANCE_STATES = frozenset({"APPROVED", "ABANDONED"})

#: The two status sets no committed artifact can distinguish, because the fact
#: they encode is a decision of the control plane rather than a property of the
#: artifacts. ``PLANNED`` and ``READY`` differ only by the Manager's selection,
#: and ``READY`` and ``IN_DEVELOPMENT`` differ only by whether an attempt is
#: currently running. Measured over all 280 historical revisions of
#: ``todo/config.yaml``: of 9,185 recorded values that are not ``PLANNED``,
#: 9,184 are reproduced exactly by :func:`derive_status` and the remaining 60
#: are these two sets. ``IN_DEVELOPMENT`` has in fact been *committed* exactly
#: once in the whole history, by a hand-made bootstrap commit, which is the
#: clearest evidence that it is a session condition rather than durable state.
UNSTARTED = "UNSTARTED"
IN_FLIGHT = "IN_FLIGHT"

UNDERDETERMINED: dict[str, frozenset[str]] = {
    UNSTARTED: frozenset({"PLANNED", "READY"}),
    IN_FLIGHT: frozenset({"READY", "IN_DEVELOPMENT"}),
}


class WorkflowError(RuntimeError):
    """Raised when a workflow invariant would be violated."""


class ArtifactSource(Protocol):
    """Read-only view of the artifacts a status may be derived from.

    Two implementations are needed and neither can be the other: the controller
    reads the working tree it is about to commit, and the equivalence check
    reads a historical revision through Git. Keeping the derivation behind this
    interface is what lets the same rules be proved against 280 revisions.
    """

    def exists(self, path: str) -> bool: ...

    def read(self, path: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class RepositoryArtifacts:
    """An :class:`ArtifactSource` over a checked-out tree."""

    root: Path

    def exists(self, path: str) -> bool:
        return (self.root / path).is_file()

    def read(self, path: str) -> Mapping[str, Any]:
        try:
            payload = json.loads((self.root / path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkflowError(f"artifact {path} is unreadable: {error}") from error
        if not isinstance(payload, Mapping):
            raise WorkflowError(f"artifact {path} is not a JSON object")
        return payload


def _artifact_paths(phase: str, task_id: str, attempt: int) -> dict[str, str]:
    stamped = f"{attempt:03d}"
    return {
        "review": f"todo/reviews/{phase}/{task_id}/review-{stamped}.json",
        "plan_review": f"todo/reviews/{phase}/{task_id}/plan-review-{stamped}.json",
        "triage": f"todo/triage/{phase}/{task_id}/triage-{stamped}.json",
        "owner_decision": f"todo/triage/{phase}/{task_id}/owner-decision-{stamped}.json",
        "planner": f"todo/evidence/{phase}/{task_id}/attempt-{stamped}-planner.json",
        "developer": f"todo/evidence/{phase}/{task_id}/attempt-{stamped}-developer.json",
    }


#: Verdict to status. A checker's conclusion is a fact about the artifact, so it
#: belongs in the derived status rather than in a separate field -- but note that
#: the mapping is not injective: a plan review PASS means "go implement", which
#: the composite field can only express as CHANGES_REQUESTED.
_REVIEW_VERDICTS = {
    "PASS": "APPROVED",
    "FAIL": "CHANGES_REQUESTED",
    "TRIAGE_REQUIRED": "TRIAGE_REQUIRED",
    "BLOCKED": "BLOCKED",
}
_TRIAGE_CLASSIFICATIONS = {
    "IMPLEMENTATION_DEFECT": "CHANGES_REQUESTED",
    "CONTRACT_MISMATCH": "PLANNING",
    "SPEC_DEFECT": "PLANNING",
    "OWNER_DECISION_REQUIRED": "OWNER_DECISION_REQUIRED",
    "EXTERNAL_BLOCKED": "BLOCKED",
}
_DEVELOPER_OUTCOMES = {
    "CANDIDATE_READY": "AWAITING_REVIEW",
    "TRIAGE_REQUIRED": "TRIAGE_REQUIRED",
    "BLOCKED": "BLOCKED",
}
_PLAN_REVIEW_VERDICTS = {
    "PASS": "CHANGES_REQUESTED",
    "FAIL": "PLANNING",
    "BLOCKED": "PLAN_REVIEW_BLOCKED",
}


def _lookup(mapping: Mapping[str, str], value: object, path: str, field: str) -> str:
    if not isinstance(value, str) or value not in mapping:
        raise WorkflowError(f"artifact {path} has an unrecognised {field} {value!r}")
    return mapping[value]


def admitted_statuses(
    *,
    lifecycle: str,
    claimed: bool,
    derived: str,
) -> frozenset[str]:
    """The statuses the stored facts admit -- the projection, as a set.

    This is the point of the decomposition: `status` should carry no information
    that `lifecycle`, the lane claim and the committed artifacts do not already
    carry. A set rather than a single value, because exactly one thing is still
    genuinely underdetermined: a claimed task that has produced nothing is either
    sealed for review or still being worked on, and *which* of those is a session
    fact no artifact records. Nothing in the workflow needs the difference -- the
    controller knows whether an attempt is running from its own runtime record --
    so the field does not have to pretend to know.

    An empty set means the facts do not add up to any status at all.
    """

    # A lifecycle claim is not self-certifying. Delivery means an independent
    # reviewer passed an exact candidate, and abandonment means the Owner
    # recorded one, so each is admitted only when the artifact that proves it is
    # present -- otherwise setting `lifecycle` by hand would be enough to declare
    # a task delivered.
    if lifecycle == DELIVERED:
        return frozenset({"APPROVED"}) if derived == "APPROVED" else frozenset()
    if lifecycle == "ABANDONED":
        return frozenset({"ABANDONED"}) if derived == "ABANDONED" else frozenset()
    if lifecycle != OPEN:
        return frozenset()
    if not claimed:
        # Nothing selected: no artifact may claim otherwise.
        return frozenset({"PLANNED"}) if derived == UNSTARTED else frozenset()
    if derived == UNSTARTED:
        return frozenset({"READY"})
    if derived == IN_FLIGHT:
        return frozenset({"READY", "IN_DEVELOPMENT"})
    return frozenset({derived})


def project_status(
    *,
    task_id: str,
    task: Mapping[str, Any],
    config: Mapping[str, Any],
    artifacts: ArtifactSource,
    running: bool | None = None,
) -> str | None:
    """The one status a task's facts project to, when they project to one.

    ``status`` used to hold this value. Nothing writes it any more, so a reader
    that needs a single name -- a message, a report -- gets it here. The case
    the facts cannot decide is a claimed task with nothing sealed: the caller's
    knowledge of the runtime record settles it (``running`` is True for a live
    attempt, False when there is none, and None for a reader that cannot know).
    A reader that cannot know still has one fact available -- the config
    declares its active task's state, and only the active task may hold the
    lane -- so an in-flight task's declaration is read rather than guessed.
    """

    derived = derive_status(
        phase=str(task["phase"]),
        task_id=task_id,
        attempt=int(task["attempt"]),
        approved_commit=task["approved_commit"],
        artifacts=artifacts,
    )
    admitted = admitted_statuses(
        lifecycle=str(task["lifecycle"]),
        claimed=bool(task["claimed"]),
        derived=derived,
    )
    if len(admitted) == 1:
        return next(iter(admitted))
    if admitted == UNDERDETERMINED[IN_FLIGHT]:
        if running is None and config.get("active_task") == task_id:
            declared = config.get("workflow_state")
            if declared in admitted:
                return str(declared)
        return "IN_DEVELOPMENT" if running else "READY"
    # The facts add up to nothing: an inconsistent record, which `validate`
    # refuses. A diagnostic still has to print something, so a revision that
    # carries the retired field prints what it recorded.
    recorded = task.get("status")
    return recorded if isinstance(recorded, str) else None


def derive_status(
    *,
    phase: str,
    task_id: str,
    attempt: int,
    approved_commit: str | None,
    artifacts: ArtifactSource,
) -> str:
    """Return the status the committed artifacts imply.

    The result is either a concrete status, or :data:`UNSTARTED` / :data:`IN_FLIGHT`
    when no artifact can decide it -- see :data:`UNDERDETERMINED`. The function
    never reads ``status`` itself and never consults the runtime records under
    ``.git/``: those do not exist in a committed revision, which is the whole
    reason the derivation has to be provable against history.

    Within one attempt the artifacts accumulate, so the *latest* event decides.
    The order is the controller's own sequence:

        developer < review | triage < owner decision < planner < plan review
    """
    if approved_commit:
        return "APPROVED"

    # Abandonment is recorded with the task, not with an attempt: it closes the
    # work item rather than one try at it, so `abandon_task` writes an unstamped
    # record and this reads the same path.
    if artifacts.exists(f"todo/abandoned/{task_id}.md"):
        return "ABANDONED"

    paths = _artifact_paths(phase, task_id, attempt)

    plan_review = (
        artifacts.read(paths["plan_review"]) if artifacts.exists(paths["plan_review"]) else None
    )
    if plan_review is not None:
        return _lookup(
            _PLAN_REVIEW_VERDICTS, plan_review.get("verdict"), paths["plan_review"], "verdict"
        )

    planner = artifacts.read(paths["planner"]) if artifacts.exists(paths["planner"]) else None
    if planner is not None:
        outcome = planner.get("outcome")
        if outcome in {"BLOCKED", "OWNER_DECISION_REQUIRED"}:
            return str(outcome)
        # A supported NO_CHANGE_REQUIRED result still lands as a plan candidate
        # to be reviewed: completion depends on acceptance evidence, never on
        # manufacturing a file diff.
        if outcome not in {"PLAN_READY", "NO_CHANGE_REQUIRED"}:
            raise WorkflowError(
                f"artifact {paths['planner']} has an unrecognised outcome {outcome!r}"
            )
        return "AWAITING_PLAN_REVIEW"

    if artifacts.exists(paths["owner_decision"]):
        return "PLANNING"

    triage = artifacts.read(paths["triage"]) if artifacts.exists(paths["triage"]) else None
    if triage is not None:
        return _lookup(
            _TRIAGE_CLASSIFICATIONS,
            triage.get("classification"),
            paths["triage"],
            "classification",
        )

    review = artifacts.read(paths["review"]) if artifacts.exists(paths["review"]) else None
    if review is not None:
        return _lookup(_REVIEW_VERDICTS, review.get("verdict"), paths["review"], "verdict")

    developer = artifacts.read(paths["developer"]) if artifacts.exists(paths["developer"]) else None
    if developer is not None:
        return _lookup(_DEVELOPER_OUTCOMES, developer.get("outcome"), paths["developer"], "outcome")

    return UNSTARTED if attempt == 0 else IN_FLIGHT


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
    #: The commit this amendment started from, before a retry re-based
    #: ``base_commit`` onto the previous candidate. The PROPHET freeze test
    #: compares changed paths against *this* commit, so a repaired attempt may
    #: still edit a contract the same amendment added, while every path that
    #: existed at the original base stays frozen exactly as before. Absent on
    #: records written before this field existed, where the attempt base is the
    #: original base.
    original_base_commit: str | None = None

    @property
    def freeze_base(self) -> str:
        """The commit the freeze test compares against."""
        return self.original_base_commit or self.base_commit

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
            "original_base_commit": self.original_base_commit,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AmendmentRecord:
        amendment_id = _required_string(value, "amendment_id")
        if not AMENDMENT_PATTERN.fullmatch(amendment_id):
            raise WorkflowError(f"invalid amendment ID {amendment_id!r}")
        status = _required_string(value, "status")
        if status not in {
            "PLANNING",
            "AWAITING_REVIEW",
            "CHANGES_REQUESTED",
            "BLOCKED",
            "APPROVED",
            "ABANDONED",
        }:
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
            original_base_commit=_optional_string(value, "original_base_commit"),
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


#: The repository's deterministic gates, as ``(name, run)`` pairs. Each one is
#: mechanical: a finding is a fact about the tree rather than a judgement, so a
#: candidate that introduces one is refused at seal time instead of spending an
#: independent review on a question a script already answered.
_DETERMINISTIC_GATES = (
    ("check_citations", check_citations.run),
    ("check_acceptance", check_acceptance.run),
    ("check_imports", check_imports.run),
)


def _deterministic_findings(root: Path) -> dict[str, set[tuple[str, str, str]] | None]:
    """Run every deterministic gate over ``root``.

    Findings are keyed by ``(rule, path, token)``. The line number is excluded on
    purpose: an edit above a flagged row shifts every line below it, and a
    line-based key would report the file as newly broken when nothing about the
    finding changed.

    ``None`` marks a gate that could not be evaluated over this tree at all --
    a minimal checkout without the documents a gate reads, for example.
    """

    findings: dict[str, set[tuple[str, str, str]] | None] = {}
    for name, gate in _DETERMINISTIC_GATES:
        try:
            findings[name] = {
                (str(finding.rule), str(finding.path), str(finding.token)) for finding in gate(root)
            }
        except Exception:  # noqa: BLE001 - an unevaluable gate is reported, not raised
            findings[name] = None
    return findings


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
        if layer == "SUPERSEDE":
            comparable["tasks"][task_id].pop("superseded_by", None)
        else:
            comparable["tasks"][task_id].pop("depends_on", None)
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
            # `status` was the composite, and nothing writes it any more. A
            # revision that still carries one -- every revision up to the
            # retirement -- is checked exactly as before, so history keeps the
            # guarantee it was written under.
            if status is not None and status not in STATES:
                raise WorkflowError(f"{task_id} has invalid status {status!r}")
            lifecycle = raw.get("lifecycle")
            claimed = raw.get("claimed")
            if lifecycle not in LIFECYCLE_VALUES:
                raise WorkflowError(f"{task_id} has invalid lifecycle {lifecycle!r}")
            if not isinstance(claimed, bool):
                raise WorkflowError(f"{task_id} claimed must be a boolean")
            # Where the composite is still stored it and the facts it is built
            # from are one fact stated twice, so a config that disagrees with
            # itself is refused rather than silently believed -- otherwise a hand
            # edit could move the status without moving the lane.
            if status is not None and LIFECYCLE_OF[status] != (lifecycle, claimed):
                raise WorkflowError(
                    f"{task_id} decomposes {status} as {LIFECYCLE_OF[status]} but "
                    f"lifecycle/claimed say {(lifecycle, claimed)}"
                )
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
                # A retired task is one that was delivered: the lifecycle says
                # so, and `status` -- where a revision still carries it -- is
                # checked against that same fact a few lines up.
                if lifecycle != DELIVERED:
                    raise WorkflowError(
                        f"{task_id} superseded_by requires APPROVED status, found {status}"
                    )
            replaces = raw.get("replaces")
            if replaces is not None:
                if not isinstance(replaces, list) or len(set(replaces)) != len(replaces):
                    raise WorkflowError(f"{task_id} replaces must be a unique list")
                for replaced in replaces:
                    if not isinstance(replaced, str) or not TASK_PATTERN.fullmatch(replaced):
                        raise WorkflowError(f"{task_id} replaces contains invalid task ID")
                    if replaced == task_id:
                        raise WorkflowError(f"{task_id} must not replace itself")
                    if replaced not in tasks:
                        raise WorkflowError(f"{task_id} replaces unknown task {replaced}")
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
            # `workflow_state` is the config's own declaration about its active
            # task. Here it is only checked structurally, and against the stored
            # composite where a revision still carries one; whether the evidence
            # supports the declaration is reported by `status_conflicts`, so a
            # config that is wrong in that way is diagnosed rather than refused
            # at load.
            declared_state = config.get("workflow_state")
            if declared_state not in STATES:
                raise WorkflowError(
                    f"workflow_state must be a workflow state, found {declared_state!r}"
                )
            recorded_state = tasks[active_task].get("status")
            if isinstance(recorded_state, str) and declared_state != recorded_state:
                raise WorkflowError("workflow_state does not match active task status")
        # The lane is held by at most one task, and the claim is now the field
        # that says so rather than a consequence of the status enum.
        claimed_tasks = [task_id for task_id, task in tasks.items() if task["claimed"]]
        if claimed_tasks and claimed_tasks != (
            [active_task] if isinstance(active_task, str) else []
        ):
            raise WorkflowError("exactly the active task may hold the single-active-work lane")
        self._validate_acyclic(tasks)
        self._validate_supersession_acyclic(tasks)

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

    def _validate_supersession_acyclic(self, tasks: Mapping[str, Any]) -> None:
        """Reject cycles in the independent supersession relation."""
        for origin in tasks:
            seen: set[str] = set()
            current = origin
            while True:
                successor = tasks[current].get("superseded_by")
                if not isinstance(successor, str):
                    break
                if successor in seen or successor == origin:
                    raise WorkflowError(f"supersession cycle includes {origin}")
                seen.add(successor)
                current = successor

    def status(self) -> dict[str, object]:
        config = self.load_config()
        config_root = self.repo
        active = config.get("active_task")
        runtime = self.load_attempt(active) if isinstance(active, str) else None
        live_attempt = runtime is not None and Path(runtime.development_worktree).is_dir()
        if live_attempt and runtime is not None:
            config_root = Path(runtime.development_worktree)
            config = self.load_config(config_root)
        active_plan = self.load_plan(active) if isinstance(active, str) else None
        active_maintenance = [
            record.to_dict()
            for record in self._maintenance_records()
            if record.status in {"IN_DEVELOPMENT", "AWAITING_REVIEW", "CHANGES_REQUESTED"}
        ]
        active_amendments = [record.to_dict() for record in self._active_amendment_records()]
        open_impacts = []
        for task_id in config["tasks"]:
            for finding in self._unresolved_task_impacts(task_id):
                impact_id, raised = finding.split(" raised by ", 1)
                amendment_id = raised.split(":", 1)[0]
                open_impacts.append(
                    {
                        "impact_id": impact_id,
                        "task_id": task_id,
                        "amendment_id": amendment_id,
                    }
                )
        return {
            "active_phase": config.get("active_phase"),
            "active_task": active,
            "workflow_state": config.get("workflow_state"),
            # The projected name, not a stored field: nothing writes `status`
            # any more, and a live runtime record is what settles the one case
            # the facts leave open.
            "task_status": (
                self._projected_status(config, active, config_root, running=live_attempt)
                if isinstance(active, str)
                else None
            ),
            "attempt": runtime.to_dict() if runtime else None,
            "plan": active_plan.to_dict() if active_plan else None,
            "active_maintenance": active_maintenance,
            "active_amendments": active_amendments,
            "open_impacts": open_impacts,
            # Reported rather than left to be discovered: a lost attempt blocks
            # `prepare-develop` on the task it belongs to, and nothing else in
            # the workflow surfaces that until someone tries to start it.
            "orphaned_attempts": [record.to_dict() for record in self.orphaned_attempts()],
            # A status with no artifact behind it did not come from a transition.
            # `validate` refuses one outright; this reports it so an operator
            # sees the disagreement while inspecting rather than when blocked.
            "status_conflicts": self.status_conflicts(config, config_root),
        }

    def status_conflicts(
        self, config: Mapping[str, Any], root: Path | None = None
    ) -> list[dict[str, str]]:
        """Recorded statuses the committed artifacts do not support.

        Every status a controller transition writes has its evidence committed in
        the same commit, so the two must agree. When they do not, the recorded
        value did not come from a transition -- a hand-edited config is how six
        tasks were once added with no author role and no review -- and acting on
        it would run the workflow from a state nothing can explain.

        The recorded status is checked against :func:`admitted_statuses`, so this
        also catches a config that contradicts itself: a status, a lifecycle and
        a lane claim that do not describe the same task, or a status the
        artifacts cannot produce.

        A revision written after the field was retired carries nothing to check
        against, so the question there is the one that still has an answer: the
        facts must add up to some status rather than to none.
        """
        base = root or self.repo
        artifacts = RepositoryArtifacts(base)
        conflicts: list[dict[str, str]] = []
        for task_id, task in config["tasks"].items():
            recorded = task.get("status")
            derived = derive_status(
                phase=str(task["phase"]),
                task_id=task_id,
                attempt=int(task["attempt"]),
                approved_commit=task["approved_commit"],
                artifacts=artifacts,
            )
            admitted = admitted_statuses(
                lifecycle=str(task["lifecycle"]),
                claimed=bool(task["claimed"]),
                derived=derived,
            )
            if isinstance(recorded, str):
                if recorded in admitted:
                    continue
                reason = (
                    f"the stored facts admit only {sorted(admitted)}"
                    if admitted
                    else f"the stored facts add up to no status at all (artifacts say {derived})"
                )
            else:
                if admitted:
                    continue
                reason = f"the stored facts add up to no status at all (artifacts say {derived})"
            conflicts.append(
                {
                    "task_id": task_id,
                    "recorded": recorded if isinstance(recorded, str) else "",
                    "derived": derived,
                    "reason": reason,
                }
            )
        # The config also declares its active task's state at the top level.
        # That declaration is a recorded status too, so it is judged the same
        # way -- and through the same channel, so an operator sees it while
        # inspecting instead of being blocked at load.
        active = config.get("active_task")
        declared = config.get("workflow_state")
        if isinstance(active, str) and isinstance(declared, str):
            admitted_active = self._admitted_states(config, active, base)
            if declared not in admitted_active:
                conflicts.append(
                    {
                        "task_id": active,
                        "recorded": declared,
                        "derived": "",
                        "reason": (
                            "workflow_state declares a state the active task's stored facts do not "
                            f"admit ({sorted(admitted_active) if admitted_active else 'none'})"
                        ),
                    }
                )
        return conflicts

    def validate_repository(self) -> None:
        config = self.load_config()
        conflicts = self.status_conflicts(config)
        if conflicts:
            rendered = "; ".join(
                f"{item['task_id']}={item['recorded']} but {item['reason']}" for item in conflicts
            )
            raise WorkflowError(
                "recorded task status disagrees with the committed artifacts: " + rendered
            )
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

    def _admitted_states(
        self, config: Mapping[str, Any], task_id: str, root: Path
    ) -> frozenset[str]:
        """The statuses a task's stored facts and committed artifacts admit.

        Routing reads this instead of ``status``. The two agree for every
        revision this repository has ever committed -- measured over all 18,580
        task-revisions in ``tests/test_workflow_state_derivation.py``, with the
        three enumerated exceptions pinned there -- so nothing routes on a field
        a hand edit can rewrite while the evidence stays where it was.

        ``root`` is the tree the caller read ``config`` from. Artifacts and the
        config that describes them must come from the same revision: during an
        attempt both live on the retained development branch.
        """
        task = self._task(config, task_id)
        derived = derive_status(
            phase=str(task["phase"]),
            task_id=task_id,
            attempt=int(task["attempt"]),
            approved_commit=task["approved_commit"],
            artifacts=RepositoryArtifacts(root),
        )
        return admitted_statuses(
            lifecycle=str(task["lifecycle"]),
            claimed=bool(task["claimed"]),
            derived=derived,
        )

    def _projected_status(
        self,
        config: Mapping[str, Any],
        task_id: str,
        root: Path,
        *,
        running: bool | None = None,
    ) -> str | None:
        """The projection, read against a checked-out tree."""

        return project_status(
            task_id=task_id,
            task=self._task(config, task_id),
            config=config,
            artifacts=RepositoryArtifacts(root),
            running=running,
        )

    def _check_dependencies(self, config: Mapping[str, Any], task_id: str) -> None:
        task = self._task(config, task_id)
        incomplete = [
            dependency
            for dependency in task["depends_on"]
            if "APPROVED" not in self._admitted_states(config, dependency, self.repo)
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
        if self._active_amendment_records():
            raise WorkflowError("cannot activate a task while an owner amendment is unfinished")
        config = self.load_config()
        admitted = self._admitted_states(config, task_id, self.repo)
        if "PLANNED" not in admitted:
            raise WorkflowError(
                "ready requires an unselected task with no committed artifacts, whose facts admit "
                f"{sorted(admitted) if admitted else 'no status at all'}"
            )
        active = config.get("active_task")
        # The lane is held exactly by the statuses the decomposition marks as
        # claimed, so occupancy is a fact about the record rather than a
        # membership test over a copy of the state list.
        if isinstance(active, str) and bool(config["tasks"][active]["claimed"]):
            raise WorkflowError(f"cannot activate {task_id} while {active} is unfinished")
        self._check_dependencies(config, task_id)
        blocked: list[str] = []
        for dependency in self._dependency_closure(config, task_id):
            blocked.extend(
                f"{dependency}: {finding}" for finding in self._unresolved_task_impacts(dependency)
            )
        if blocked:
            raise WorkflowError(
                f"cannot activate {task_id}: an earlier Owner amendment recorded that this "
                "contract no longer matches a governing document, and no later amendment has "
                "resolved it -- " + "; ".join(blocked)
            )
        self._set_state(config, task_id, "READY")
        _write_json(self.config_path, config)
        _git(self.repo, "add", "todo/config.yaml")
        _git(self.repo, "commit", "-m", f"chore(workflow): mark {task_id} ready")
        return _sha(self.repo)

    def _dependency_closure(self, config: Mapping[str, Any], task_id: str) -> list[str]:
        """Return the task and every transitive dependency in stable traversal order."""
        ordered: list[str] = []
        visited: set[str] = set()

        def visit(current: str) -> None:
            if current in visited:
                return
            visited.add(current)
            ordered.append(current)
            for dependency in config["tasks"][current]["depends_on"]:
                visit(dependency)

        visit(task_id)
        return ordered

    def _unresolved_task_impacts(self, task_id: str) -> list[str]:
        """Return the open contract conflicts an earlier amendment raised on a task.

        A property Owner amendment may add tasks and correct collateral
        documents, and doing so can leave an *existing* contract asserting
        something a governing document no longer says. Nothing in the layer that
        makes the change can repair that contract, so the change records the
        conflict instead; this is what keeps the affected task from being
        activated until a later amendment resolves it. Resolutions are amendments
        with a strictly greater ID, so a register can only be closed forward.
        """
        root = self.repo / "todo" / "amendments"
        if not root.is_dir():
            return []
        raised: list[tuple[str, str, str, str]] = []
        exact_resolutions: list[tuple[str, str]] = []
        for path in sorted(root.glob("*/impacts.json")):
            payload = _load_json(path)
            amendment_id = str(payload.get("amendment_id", path.parent.name))
            entries = payload.get("raised")
            if isinstance(entries, list):
                for item in entries:
                    if isinstance(item, Mapping) and item.get("task_id") == task_id:
                        impact_id = item.get("impact_id")
                        if not isinstance(impact_id, str):
                            impact_id = f"{amendment_id}:{task_id}:legacy"
                        raised.append(
                            (amendment_id, impact_id, task_id, str(item.get("reason", "")))
                        )
            resolves = payload.get("resolves")
            if isinstance(resolves, list):
                for resolution in resolves:
                    if not isinstance(resolution, str):
                        continue
                    if resolution.startswith("A") and f":{task_id}:" in resolution:
                        exact_resolutions.append((amendment_id, resolution))
        open_impacts = [
            (amendment_id, impact_id, reason)
            for amendment_id, impact_id, _affected_task, reason in raised
            if not any(
                resolution_amendment > amendment_id and resolution_id == impact_id
                for resolution_amendment, resolution_id in exact_resolutions
            )
        ]
        return [
            f"{impact_id} raised by {amendment_id}: {reason}"
            for amendment_id, impact_id, reason in open_impacts
        ]

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

    def _active_amendment_records(self) -> list[AmendmentRecord]:
        """The amendments that still occupy the lane.

        A closed change (``APPROVED`` or ``ABANDONED``) keeps its record so its
        ID stays consumed, but it must not block the next change: only a change
        that could still move is a conflict.
        """
        return [
            record
            for record in self._amendment_records()
            if record.status not in TERMINAL_AMENDMENT_STATES
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
        *,
        root: Path | None = None,
        **updates: object,
    ) -> None:
        if state not in STATES:
            raise WorkflowError(f"invalid target state {state}")
        task = self._task(config, task_id)
        # The state this transition leaves is the one the facts currently admit.
        # A record that still carries `status` -- every revision before the field
        # was retired -- is the fallback, so a historical record is judged
        # exactly as it was written.
        admitted = self._admitted_states(config, task_id, root or self.repo)
        recorded = task.get("status")
        current = admitted or ({recorded} if isinstance(recorded, str) else set())
        illegal = [
            candidate
            for candidate in current
            if state != candidate and state not in ALLOWED_TRANSITIONS[candidate]
        ]
        if illegal:
            raise WorkflowError(
                f"illegal state transition for {task_id}: "
                f"{sorted(current) if len(current) > 1 else next(iter(current), 'nothing')} "
                f"-> {state}"
            )
        # Written here and nowhere else, so the two facts cannot drift apart --
        # and no projection of them is stored beside them any more: `status` was
        # that projection, and after this increment nothing writes it.
        lifecycle, claimed = LIFECYCLE_OF[state]
        task["lifecycle"] = lifecycle
        task["claimed"] = claimed
        task.pop("status", None)
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
            admitted = self._admitted_states(config, task_id, worktree)
            if "BLOCKED" in admitted:
                self._set_state(config, task_id, "READY", root=worktree)
            elif "CHANGES_REQUESTED" not in admitted:
                raise WorkflowError(
                    "retry requires a review that requested changes or a resolved block; the "
                    f"facts admit {sorted(admitted) if admitted else 'no status at all'}"
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
            admitted = self._admitted_states(config, task_id, self.repo)
            if "READY" not in admitted:
                raise WorkflowError(
                    "develop requires a selected task with no sealed candidate, whose facts "
                    f"admit {sorted(admitted) if admitted else 'no status at all'}"
                )
            self._check_dependencies(config, task_id)
            retained = self.load_attempt(task_id)
            if retained is not None:
                worktree = Path(retained.development_worktree)
                if worktree.is_dir():
                    raise WorkflowError(f"runtime record already exists for {task_id}")
                # A record whose worktree is gone cannot be continued, finished,
                # reviewed or triaged. Saying only "a record exists" made this
                # look like a live attempt and left the operator to discover by
                # hand that no command would accept it.
                raise WorkflowError(
                    f"{task_id} has a retained attempt {retained.attempt} whose worktree "
                    f"{worktree} no longer exists, so no command can use it; record it as "
                    f"lost with 'discard-attempt {task_id} --reason ...' and start again"
                )
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
        # A retry has to carry the verdict that caused it. The review record is
        # committed in the retained worktree, so it is the only durable statement
        # of what the previous candidate got wrong; a retry prompt that omits it
        # asks the Developer to guess at defects the Reviewer already named.
        latest_review = task.get("latest_review")
        review_path = worktree / latest_review if isinstance(latest_review, str) else None
        if retry and review_path is not None and review_path.is_file():
            prompt += (
                f" This attempt repairs the independent review at {review_path}: address every "
                f"required_change, and do not regress a check that review already passed."
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
        admitted = self._admitted_states(config, task_id, worktree)
        if "IN_DEVELOPMENT" not in admitted or attempt.candidate_commit is not None:
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
            self._set_state(config, task_id, state, base_commit=attempt.base_commit, root=worktree)
            _write_json(worktree / "todo" / "config.yaml", config)
            (worktree / ".workflow" / "developer-result.json").unlink(missing_ok=True)
            (worktree / ".workflow" / "developer-continuation.json").unlink(missing_ok=True)
            self._continuation_path(task_id).unlink(missing_ok=True)
            _git(worktree, "add", "-A")
            _git(worktree, "commit", "-m", f"chore(workflow): record {task_id} {state.lower()}")
            self.save_attempt(attempt)
            return attempt

        self._set_state(
            config, task_id, "AWAITING_REVIEW", base_commit=attempt.base_commit, root=worktree
        )
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
        active_claimed = (
            bool(config["tasks"][active]["claimed"]) if isinstance(active, str) else False
        )
        if active_claimed:
            raise WorkflowError(f"cannot start an amendment while {active} is unfinished")
        if self._active_amendment_records():
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
                if required_status not in self._admitted_states(config, task_id, self.repo):
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
            original_base_commit=base,
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
        elif layer == "SUPERSEDE":
            agent, scope = (
                "planner",
                f"Record the Owner-directed retirement {amendment_id} for exactly these "
                f"APPROVED tasks: {', '.join(normalized)}. Change only each target's "
                "superseded_by field; contracts, dependencies and approval evidence are frozen",
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
        self._validate_impact_resolutions(
            record,
            cast(list[str], result["resolved_task_impacts"]),
        )
        changed = [
            path for path in _working_tree_changes(worktree) if not path.startswith(".workflow/")
        ]
        config = self.load_config(worktree)
        base_config = self.load_config()
        allowed_contracts = {
            base_config["tasks"][task_id]["task_file"] for task_id in record.task_ids
        }
        # The freeze test reads the status letter against the amendment's
        # *original* base, not the attempt base. A retry re-bases the attempt on
        # the previous candidate, which would otherwise make the amendment's own
        # additions look like pre-existing files and refuse to let it repair
        # them; the original base keeps every path that existed before the
        # amendment frozen exactly as it was.
        statuses = _change_statuses(worktree, record.freeze_base)
        forbidden: list[str] = []
        for path in changed:
            if record.layer == "PROPHET":
                allowed = path == "todo/config.yaml" or _prophet_path_allowed(
                    path, statuses.get(path, "M")
                )
            elif record.layer == "SUPERSEDE":
                allowed = path == "todo/config.yaml"
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
        if outcome == "AMENDMENT_READY":
            self._assert_no_new_deterministic_findings(
                worktree=worktree,
                amendment_id=amendment_id,
                attempt=record.attempt,
                base_commit=record.base_commit,
            )
        if record.layer == "SUPERSEDE" and outcome == "AMENDMENT_READY":
            self._validate_supersede_candidate(
                base_config=base_config,
                candidate_config=config,
                task_ids=record.task_ids,
                resolved_impacts=cast(list[str], result["resolved_task_impacts"]),
            )
        if outcome == "NO_CHANGE_REQUIRED" and changed:
            raise WorkflowError("NO_CHANGE_REQUIRED contradicts planner file changes")
        relative_root = Path("todo") / "amendments" / amendment_id
        _write_json(worktree / relative_root / "request.json", request)
        author = "prophet" if record.layer == "PROPHET" else "planner"
        _write_json(worktree / relative_root / f"{author}-{record.attempt:03d}.json", result)
        _write_json(
            worktree / relative_root / "impacts.json",
            {
                "amendment_id": amendment_id,
                "layer": record.layer,
                "raised": [dict(item) for item in result["affected_existing_tasks"]],
                "resolves": list(result["resolved_task_impacts"]),
            },
        )
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
            original_base_commit=record.freeze_base,
        )
        self.save_amendment(updated)
        return updated

    def _validate_impact_resolutions(
        self, record: AmendmentRecord, resolved_impacts: Sequence[str]
    ) -> None:
        """Bind every resolution to a currently open, in-scope impact."""
        config = self.load_config()
        open_impacts: dict[str, str] = {}
        for task_id in config["tasks"]:
            for finding in self._unresolved_task_impacts(task_id):
                impact_id = finding.split(" raised by ", 1)[0]
                open_impacts[impact_id] = task_id
        for impact_id in resolved_impacts:
            affected_task = open_impacts.get(impact_id)
            if affected_task is None:
                raise WorkflowError(f"resolved impact is not currently open: {impact_id}")
            if record.layer != "PROPHET" and affected_task not in record.task_ids:
                raise WorkflowError(
                    f"{record.layer} amendment cannot resolve untargeted impact "
                    f"{impact_id} on {affected_task}"
                )

    def _validate_supersede_candidate(
        self,
        *,
        base_config: Mapping[str, Any],
        candidate_config: Mapping[str, Any],
        task_ids: Sequence[str],
        resolved_impacts: Sequence[str],
    ) -> None:
        """Require an auditable, dependency-safe retirement before sealing it."""
        for task_id in task_ids:
            before = base_config["tasks"][task_id]
            after = candidate_config["tasks"][task_id]
            successor_id = after.get("superseded_by")
            if not isinstance(successor_id, str) or successor_id == before.get("superseded_by"):
                raise WorkflowError(f"SUPERSEDE must set a new successor for {task_id}")
            successor = candidate_config["tasks"][successor_id]
            if successor.get("superseded_by"):
                raise WorkflowError(f"SUPERSEDE successor {successor_id} is itself superseded")
            if task_id not in successor.get("depends_on", []):
                raise WorkflowError(
                    f"SUPERSEDE successor {successor_id} must depend on the work it replaces: "
                    f"{task_id}"
                )
            if task_id not in successor.get("replaces", []):
                raise WorkflowError(
                    f"SUPERSEDE successor {successor_id} must declare replaces: {task_id}"
                )
            successor_contract = self.repo / successor["task_file"]
            successor_text = successor_contract.read_text(encoding="utf-8")
            if (
                "## Replacement and migration" not in successor_text
                or task_id not in successor_text.split("## Replacement and migration", 1)[1]
            ):
                raise WorkflowError(
                    f"SUPERSEDE successor {successor_id} must document {task_id} in its "
                    "Replacement and migration section"
                )
            stranded = [
                consumer_id
                for consumer_id, consumer in candidate_config["tasks"].items()
                if consumer_id != successor_id
                and consumer.get("status") == "PLANNED"
                and task_id in consumer.get("depends_on", [])
            ]
            if stranded:
                raise WorkflowError(
                    f"cannot retire {task_id}; PLANNED consumers still depend on it: "
                    + ", ".join(stranded)
                )
            open_impact_ids = {
                finding.split(" raised by ", 1)[0]
                for finding in self._unresolved_task_impacts(task_id)
            }
            missing_resolutions = sorted(open_impact_ids - set(resolved_impacts))
            if missing_resolutions:
                raise WorkflowError(
                    f"SUPERSEDE must resolve every open impact on {task_id}: "
                    + ", ".join(missing_resolutions)
                )

    def _validate_amendment_result(self, result: Mapping[str, Any], amendment_id: str) -> str:
        _validate_object_keys(
            result,
            required={
                "amendment_id",
                "outcome",
                "summary",
                "rationale",
                "unresolved_questions",
                "impact_assessment",
                "affected_existing_tasks",
                "resolved_task_impacts",
            },
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
        assessment = result.get("impact_assessment")
        assessment_fields = {
            "intent",
            "specification",
            "contracts",
            "dependencies",
            "implementation",
            "data",
            "operations",
            "security",
            "verification",
        }
        if not isinstance(assessment, Mapping) or set(assessment) != assessment_fields:
            raise WorkflowError(
                "amendment impact_assessment must contain exactly: "
                + ", ".join(sorted(assessment_fields))
            )
        for field in sorted(assessment_fields):
            value = assessment[field]
            if not isinstance(value, str) or not value.strip():
                raise WorkflowError(f"amendment impact_assessment.{field} must be non-empty")
        # Every amendment states, explicitly, whether it left an existing
        # contract asserting something a governing document no longer says. An
        # empty list is a claim the independent review can test; silence is not.
        affected = result.get("affected_existing_tasks")
        if not isinstance(affected, list):
            raise WorkflowError("amendment affected_existing_tasks must be a list")
        impact_ids: set[str] = set()
        for item in affected:
            required_impact_fields = {
                "impact_id",
                "task_id",
                "reason",
                "categories",
                "required_disposition",
            }
            if not isinstance(item, Mapping) or set(item) != required_impact_fields:
                raise WorkflowError(
                    "each amendment affected_existing_tasks entry requires exactly "
                    + ", ".join(sorted(required_impact_fields))
                )
            affected_id = item["task_id"]
            if not isinstance(affected_id, str) or not TASK_PATTERN.fullmatch(affected_id):
                raise WorkflowError(f"invalid affected task ID {affected_id!r}")
            if affected_id not in self.load_config()["tasks"]:
                raise WorkflowError(f"affected task does not exist: {affected_id}")
            impact_id = item["impact_id"]
            if (
                not isinstance(impact_id, str)
                or not IMPACT_PATTERN.fullmatch(impact_id)
                or not impact_id.startswith(f"{amendment_id}:{affected_id}:")
            ):
                raise WorkflowError(
                    f"affected impact ID must bind amendment and task: {impact_id!r}"
                )
            if impact_id in impact_ids:
                raise WorkflowError(f"duplicate affected impact ID: {impact_id}")
            impact_ids.add(impact_id)
            reason = item["reason"]
            if not isinstance(reason, str) or not reason.strip():
                raise WorkflowError(
                    f"affected_existing_tasks[{affected_id}] needs a non-empty reason"
                )
            categories = item["categories"]
            allowed_categories = {
                "CONTRACT",
                "DEPENDENCY",
                "IMPLEMENTATION",
                "DATA",
                "OPERATIONS",
                "SECURITY",
                "VERIFICATION",
            }
            if (
                not isinstance(categories, list)
                or not categories
                or len(set(categories)) != len(categories)
                or any(category not in allowed_categories for category in categories)
            ):
                raise WorkflowError(
                    f"affected_existing_tasks[{affected_id}] has invalid categories"
                )
            disposition = item["required_disposition"]
            if not isinstance(disposition, str) or not disposition.strip():
                raise WorkflowError(
                    f"affected_existing_tasks[{affected_id}] needs a required_disposition"
                )
        resolves = result.get("resolved_task_impacts")
        _validate_string_list(resolves, "amendment resolved_task_impacts")
        if len(cast(list[str], resolves)) != len(set(cast(list[str], resolves))):
            raise WorkflowError("amendment resolved_task_impacts must be unique")
        for resolved_id in cast(list[str], resolves):
            if not IMPACT_PATTERN.fullmatch(resolved_id):
                raise WorkflowError(f"invalid resolved impact ID {resolved_id!r}")
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
            # The record is kept as a closed entry instead of being deleted: the
            # ID stays consumed, so the next amendment cannot be handed a number
            # whose branch and worktree path this one still used.
            self.save_amendment(replace(record, status="APPROVED"))
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
            original_base_commit=record.freeze_base,
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

    def withdraw_amendment(self, amendment_id: str, *, reason: str) -> dict[str, object]:
        """Close an amendment that will not land, without deleting its record.

        A failed change whose own layer cannot repair it used to block every
        later amendment: only a PASS cleared the record, and the lane refuses
        while any record exists. Withdrawal is the Owner's exit. It records the
        reason in the amendment's own directory on its branch, lands nothing,
        and leaves a terminal record behind so the ID stays consumed.
        """
        record = self.load_amendment(amendment_id)
        if record is None:
            raise WorkflowError(f"unknown owner amendment {amendment_id}")
        if record.status in TERMINAL_AMENDMENT_STATES:
            raise WorkflowError(f"amendment {amendment_id} is already closed ({record.status})")
        if not reason.strip():
            raise WorkflowError("withdraw-amendment requires a non-empty reason")
        worktree = Path(record.worktree)
        relative_path = Path("todo") / "amendments" / amendment_id / "withdrawal.md"
        target = worktree / relative_path
        withdrawal = "\n".join(
            [
                f"# {amendment_id} withdrawal",
                "",
                f"- Layer: `{record.layer}`",
                f"- Targets: {', '.join(record.task_ids) or 'none'}",
                f"- Status at withdrawal: `{record.status}`",
                f"- Attempt: {record.attempt}",
                f"- Base commit: `{record.base_commit}`",
                f"- Candidate commit: `{record.candidate_commit or 'none'}`",
                "",
                "## Reason",
                "",
                reason.strip(),
                "",
                "Nothing from this amendment landed. The worktree and branch are kept as",
                "the record of what was attempted, and the change must be re-issued as a",
                "new amendment if it is still wanted.",
                "",
            ]
        )
        if worktree.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(withdrawal, encoding="utf-8")
            _git(worktree, "add", str(relative_path))
            _git(worktree, "commit", "-m", f"chore(amendment): withdraw {amendment_id}")
        closed = replace(record, status="ABANDONED")
        self.save_amendment(closed)
        self._amendment_request_path(amendment_id).unlink(missing_ok=True)
        return {**closed.to_dict(), "withdrawal": str(target)}

    def abandon_task(self, task_id: str, *, reason: str) -> dict[str, object]:
        """Close a task that will not land, keeping every record it produced.

        This is the Owner's exit from a work item whose lane cannot be moved. A
        Developer that dies before writing its handoff leaves a worktree that no
        command accepts; an external dependency can deny progress for as long as
        it likes. In both cases the only exit used to be forward progress --
        exactly the thing that had failed -- so the work item and the
        single-active-work lane were stuck together, and no route could retire
        it: ``SUPERSEDE`` requires ``APPROVED``, ``CONTRACT``/``SPEC`` require
        ``PLANNED``, and deletion is refused everywhere.

        An abandonment lands no implementation. The branch, the worktree and the
        attempt stay exactly as they were, so the abandoned work remains in Git
        as evidence; only the status and this record move, and they move in the
        main checkout rather than on the task branch, because nothing from that
        branch may enter the product.

        The guard is dependency-shaped: a task still depended on by an open task
        may not be abandoned, because its dependents could then never activate.
        Dependencies form a DAG, so abandoning in reverse-dependency order always
        terminates -- the refusal redirects the Owner, it does not trap them.
        """
        self._ensure_clean_main()
        if not reason.strip():
            raise WorkflowError("abandon-task requires a non-empty reason")
        config = self.load_config()
        config_root = self.repo
        attempt = self.load_attempt(task_id)
        live_attempt = attempt is not None and Path(attempt.development_worktree).is_dir()
        if live_attempt and attempt is not None:
            # While an attempt is in flight the retained worktree holds the live
            # facts; the main checkout still shows whatever the last commit that
            # touched it recorded. Resolve the same way `status` does, so the
            # abandonment record states the state the work item was actually in.
            config_root = Path(attempt.development_worktree)
            config = self.load_config(config_root)
        task = self._task(config, task_id)
        # `running` is the one thing the facts cannot say: a claimed task with
        # nothing sealed is READY or IN_DEVELOPMENT, and this command has just
        # looked at the runtime record, so its record states which.
        status = (
            self._projected_status(config, task_id, config_root, running=live_attempt) or "unknown"
        )
        # Closedness is the task's own lifecycle, not the projected status: a
        # hand-edited status could otherwise present closed work as open (or the
        # reverse) while the decomposition disagreed.
        if str(task["lifecycle"]) != OPEN:
            raise WorkflowError(f"{task_id} is already closed ({status})")
        stranded = [
            other_id
            for other_id, other in config["tasks"].items()
            if other_id != task_id
            and str(other["lifecycle"]) == OPEN
            and task_id in other["depends_on"]
        ]
        if stranded:
            raise WorkflowError(
                f"cannot abandon {task_id}; open tasks still depend on it: "
                + ", ".join(stranded)
                + " -- close or re-point them first"
            )

        relative_record = Path("todo") / "abandoned" / f"{task_id}.md"
        record_path = self.repo / relative_record
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            "\n".join(
                [
                    f"# {task_id} abandonment",
                    "",
                    f"- Phase: `{task['phase']}`",
                    f"- Status at abandonment: `{status}`",
                    f"- Attempt: {task['attempt']}",
                    f"- Base commit: `{task['base_commit'] or 'none'}`",
                    f"- Candidate commit: `{task['candidate_commit'] or 'none'}`",
                    f"- Latest review: `{task['latest_review'] or 'none'}`",
                    f"- Retained branch: `{attempt.branch if attempt else 'none'}`",
                    f"- Retained worktree: `{attempt.development_worktree if attempt else 'none'}`",
                    "",
                    "## Reason",
                    "",
                    reason.strip(),
                    "",
                    "Nothing from this task landed. The branch and worktree above are kept",
                    "as the record of what was attempted, and the work must be re-issued",
                    "as a new task if it is still wanted.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        previous_active = config.get("active_task")
        self._set_state(config, task_id, "ABANDONED", root=config_root)
        if isinstance(previous_active, str) and previous_active != task_id:
            # Closing a resting task must not move the active pointer onto it.
            keeper = config["tasks"][previous_active]
            config["active_task"] = previous_active
            config["active_phase"] = keeper["phase"]
            config["workflow_state"] = keeper["status"]
        _write_json(self.config_path, config)
        _git(self.repo, "add", str(relative_record), "todo/config.yaml")
        _git(self.repo, "commit", "-m", f"chore(workflow): abandon {task_id}")
        # The runtime record is dropped only after the durable record above has
        # captured where the work was retained: an unversioned file under .git
        # is not an acceptable last copy of that pointer.
        self._attempt_path(task_id).unlink(missing_ok=True)
        return {
            "task_id": task_id,
            "status": "ABANDONED",
            "record": str(relative_record),
            "commit": _sha(self.repo),
        }

    def _attempt_records(self) -> list[AttemptRecord]:
        if not self.runtime_dir.is_dir():
            return []
        return [
            AttemptRecord.from_dict(_load_json(path))
            for path in sorted(self.runtime_dir.glob("T[0-9][0-9][0-9].json"))
        ]

    def orphaned_attempts(self) -> list[AttemptRecord]:
        """Runtime records whose worktree no longer exists.

        Such a record is unusable. No command can continue, finish, review or
        triage the attempt it describes, and ``prepare_develop`` refuses to start
        a new one while it is present -- so the task cannot be worked on at all,
        and deleting the file by hand is the only escape. The record outlives its
        worktree when a worktree is removed outside the controller, and it also
        survives from earlier controller versions that did not clean up on
        approval: ``.git/robinhood-lp-workflow/T015.json`` is such a remnant.
        """
        return [
            record
            for record in self._attempt_records()
            if not Path(record.development_worktree).is_dir()
        ]

    def discard_attempt(self, task_id: str, *, reason: str) -> dict[str, object]:
        """Discard a lost attempt's bookkeeping so the task can be started again.

        This repairs the controller's own records, not the work item. An attempt
        whose worktree is gone cannot be continued, finished, reviewed or
        triaged, so the only truthful options are to close the task or to declare
        the attempt lost; ``abandon-task`` is the first and this is the second.

        It does **not** move a status. A task branch reaches the main checkout
        only through an approval, so while an attempt is in flight the main
        checkout still shows ``PLANNED`` or ``READY`` -- and the recovery needs
        no transition from either. A task in any other *open* state is refused
        and redirected to ``abandon-task``, which is reachable from every state,
        so the refusal cannot become a trap.

        A closed task (``APPROVED`` or ``ABANDONED``) is served too, without the
        attempt bump: it will never develop again, so its remnant needs no route
        at all, and ``attempt`` on an approved task is part of what the reviewer
        inspected. Without that, a remnant on a completed task could never be
        cleared by any route and would sit in ``status`` for good.

        The consumed attempt number is never reused: for an open task the config
        is raised to at least the lost attempt, so the next ``prepare-develop``
        opens the one after it. The lost attempt is recorded under
        ``todo/evidence/`` first, because the runtime record is the only place
        its branch and commits were named, and it is about to be deleted.
        """
        self._ensure_clean_main()
        if not reason.strip():
            raise WorkflowError("discard-attempt requires a non-empty reason")
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no recorded attempt to discard")
        if Path(attempt.development_worktree).is_dir():
            raise WorkflowError(
                f"{task_id}'s attempt is still live at {attempt.development_worktree}; "
                "use the normal develop, review or triage route"
            )
        config = self.load_config()
        task = self._task(config, task_id)
        recorded = task.get("status")
        status = self._projected_status(config, task_id, self.repo) or "unknown"
        closed = str(task["lifecycle"]) != OPEN
        if not closed:
            admitted = self._admitted_states(config, task_id, self.repo)
            # A record that still carries the retired field has to agree with its
            # own evidence; a record without one has nothing to disagree with.
            if isinstance(recorded, str) and recorded not in admitted:
                raise WorkflowError(
                    f"{task_id} records {recorded}, but its committed artifacts admit "
                    f"{sorted(admitted) if admitted else 'no status at all'}; settle that "
                    "disagreement with 'validate' before clearing a lost attempt"
                )
            # A lost attempt only leaves the main checkout before the work has
            # sealed anything. The facts cannot separate READY from
            # IN_DEVELOPMENT, and here the difference does not matter: both mean
            # the attempt produced no candidate, which is the remnant this
            # command exists to clear. Anything beyond that pair is a task stuck
            # mid-flight, which `abandon-task` closes.
            if not admitted <= {"PLANNED", "READY", "IN_DEVELOPMENT"}:
                raise WorkflowError(
                    f"{task_id} is {status}, but a lost attempt only leaves the main checkout "
                    "before any candidate is sealed; close the work item with abandon-task "
                    "instead"
                )

        relative = (
            Path("todo")
            / "evidence"
            / str(task["phase"])
            / task_id
            / f"attempt-{attempt.attempt:03d}-lost.json"
        )
        checkpoint = self._continuation_path(task_id)
        _write_json(
            self.repo / relative,
            {
                "task_id": task_id,
                "attempt": attempt.attempt,
                "reason": reason.strip(),
                "lost_record": attempt.to_dict(),
                "continuation_checkpoint_existed": checkpoint.is_file(),
                "protected_snapshot_kept": self._protected_snapshot_path(task_id).is_file(),
            },
        )
        # A closed task never develops again, so its remnant needs no route and
        # its record is cleared without touching the approval evidence: `attempt`
        # on an APPROVED task is part of what the reviewer inspected, and raising
        # it would be rewriting that evidence.
        consumed = int(task["attempt"])
        if not closed:
            consumed = max(consumed, attempt.attempt)
            config["tasks"][task_id]["attempt"] = consumed
        _write_json(self.config_path, config)
        _git(self.repo, "add", str(relative), "todo/config.yaml")
        _git(self.repo, "commit", "-m", f"chore(workflow): discard the lost {task_id} attempt")
        self._attempt_path(task_id).unlink(missing_ok=True)
        checkpoint.unlink(missing_ok=True)
        return {
            "task_id": task_id,
            "status": status,
            "discarded_attempt": attempt.attempt,
            "next_attempt": None if closed else consumed + 1,
            "record": str(relative),
            "commit": _sha(self.repo),
        }

    def abandon_maintenance(self, maintenance_id: str, *, reason: str) -> dict[str, object]:
        """Close a maintenance repair that will not land, keeping its record.

        The lane reaches ``ESCALATED`` when the Developer reports that the repair
        does not fit the maintenance boundary; the documented route is then a new
        numbered task or normal triage, i.e. outside this lane. Nothing in the
        lane could close the record, so it stayed forever. This is that exit, and
        it also serves any other maintenance state that will not land.
        """
        record = self.load_maintenance(maintenance_id)
        if record is None:
            raise WorkflowError(f"unknown maintenance repair {maintenance_id}")
        if record.status in TERMINAL_MAINTENANCE_STATES:
            raise WorkflowError(f"maintenance {maintenance_id} is already closed ({record.status})")
        if not reason.strip():
            raise WorkflowError("abandon-maintenance requires a non-empty reason")
        worktree = Path(record.development_worktree)
        relative_path = Path("todo") / "maintenance" / maintenance_id / "withdrawal.md"
        target = worktree / relative_path
        withdrawal = "\n".join(
            [
                f"# {maintenance_id} abandonment",
                "",
                f"- Status at abandonment: `{record.status}`",
                f"- Attempt: {record.attempt}",
                f"- Base commit: `{record.base_commit}`",
                f"- Candidate commit: `{record.candidate_commit or 'none'}`",
                f"- Retained branch: `{record.branch}`",
                "",
                "## Reason",
                "",
                reason.strip(),
                "",
                "Nothing from this repair landed. The branch and worktree are kept as",
                "the record of what was attempted.",
                "",
            ]
        )
        if worktree.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(withdrawal, encoding="utf-8")
            _git(worktree, "add", str(relative_path))
            _git(worktree, "commit", "-m", f"chore(maintenance): abandon {maintenance_id}")
        closed = replace(record, status="ABANDONED")
        self.save_maintenance(closed)
        self._maintenance_request_path(maintenance_id).unlink(missing_ok=True)
        return {**closed.to_dict(), "withdrawal": str(target)}

    def _assert_no_new_deterministic_findings(
        self,
        *,
        worktree: Path,
        amendment_id: str,
        attempt: int,
        base_commit: str,
    ) -> None:
        """Refuse a candidate that adds a finding to a deterministic gate.

        The comparison is a *delta* against the attempt's base, never "no
        findings at all": a repository that is already red must stay amendable,
        or the gate would be a dead end of its own. A gate that cannot be
        evaluated on either side is skipped, except when the base could be
        evaluated and the candidate could not -- that is the candidate breaking
        the gate, not an environment limitation.
        """

        base_tree = self.worktree_root / f"amendment-base-{amendment_id.lower()}-{attempt:03d}"
        if base_tree.exists():
            raise WorkflowError(f"base comparison worktree already exists: {base_tree}")
        _git(self.repo, "worktree", "add", "--detach", str(base_tree), base_commit)
        try:
            base_findings = _deterministic_findings(base_tree)
            candidate_findings = _deterministic_findings(worktree)
        finally:
            _git(self.repo, "worktree", "remove", str(base_tree), check=False)
        for gate, candidate in sorted(candidate_findings.items()):
            baseline = base_findings.get(gate)
            if baseline is None:
                # Nothing to compare against: the base itself could not be
                # evaluated, so the gate cannot judge this candidate either.
                continue
            if candidate is None:
                raise WorkflowError(
                    f"{amendment_id} candidate cannot be evaluated by {gate}, "
                    "which the base could be: the gate fails on this candidate"
                )
            added = candidate - baseline
            if added:
                rendered = ", ".join(
                    f"{path} ({token or rule})" for rule, path, token in sorted(added)
                )
                raise WorkflowError(
                    f"{amendment_id} candidate adds {len(added)} finding(s) to {gate}: {rendered}"
                )

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
        if self._active_amendment_records():
            raise WorkflowError("cannot start maintenance while an owner amendment is unfinished")
        config = self.load_config()
        active = config.get("active_task")
        if isinstance(active, str) and bool(config["tasks"][active]["claimed"]):
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
        if related_task is not None and "APPROVED" not in self._admitted_states(
            config, related_task, self.repo
        ):
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
        if "AWAITING_REVIEW" not in self._admitted_states(config, task_id, worktree):
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
        self._set_state(config, task_id, state, **updates, root=worktree)
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
        if "TRIAGE_REQUIRED" not in self._admitted_states(config, task_id, worktree):
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
        self._set_state(config, task_id, state, root=worktree)
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
        admitted = self._admitted_states(config, task_id, worktree)
        if not admitted & {"PLANNING", "OWNER_DECISION_REQUIRED"}:
            raise WorkflowError("plan requires PLANNING or OWNER_DECISION_REQUIRED")
        if _git(worktree, "status", "--porcelain").stdout:
            raise WorkflowError("development worktree must be clean before planning")
        triage_path = self._triage_report_path(attempt)
        triage_result = _load_json(worktree / triage_path)
        classification = _required_string(triage_result, "classification")
        owner_route = "OWNER_DECISION_REQUIRED" in admitted
        if owner_route:
            if not owner_decision or not owner_decision.strip():
                raise WorkflowError("planning this issue requires the owner's explicit decision")
            decision_path = triage_path.with_name(f"owner-decision-{attempt.attempt:03d}.json")
            _write_json(
                worktree / decision_path,
                {"task_id": task_id, "decision": owner_decision.strip()},
            )
            self._set_state(config, task_id, "PLANNING", root=worktree)
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
            self._set_state(config, task_id, state, root=worktree)
            _write_json(worktree / "todo" / "config.yaml", config)
            _git(worktree, "add", "-A")
            _git(worktree, "commit", "-m", f"chore(workflow): record {task_id} planning {state}")
            return None

        self._set_state(config, task_id, "AWAITING_PLAN_REVIEW", root=worktree)
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
        if not self._admitted_states(config, task_id, worktree) & {
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
        self._set_state(config, task_id, state, root=worktree)
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
