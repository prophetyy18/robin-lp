"""Prove that ``derive_status`` reproduces the recorded status across history.

``todo/config.yaml`` has carried every status this project ever recorded, and the
controller commits the evidence for a transition in the *same* commit as the
transition itself. That makes the whole history a free oracle: for any revision,
the status recorded there must be reconstructible from the artifacts committed
there, or the derivation is wrong -- or the value records something no artifact
can carry, which is a finding about the model rather than about the code.

The walk covers every revision of the file. It is not a sample: a sample would
let a rule set that happens to fit the common path pass while quietly failing the
rare one, and the rare ones are exactly what this is looking for.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from tools.workflow.core import (
    IN_FLIGHT,
    LIFECYCLE_OF,
    UNDERDETERMINED,
    UNSTARTED,
    RepositoryArtifacts,
    WorkflowError,
    admitted_statuses,
    derive_status,
)

ROOT = Path(__file__).resolve().parents[1]

#: The one recorded value the current rules cannot reproduce, and the reason it
#: must not be reproduced.
#:
#: Before 2026-09-15 the controller mapped a FAIL verdict to BLOCKED whenever any
#: check was UNKNOWN or any unknown remained (`tools/workflow/core.py` at
#: 63cfb2cc: ``if verdict == "BLOCKED" or "UNKNOWN" in statuses or unknowns``).
#: That review carried an unresolved clean-room-install unknown, so BLOCKED was
#: the *correct* value under the semantics then in force. Rewriting it to match
#: today's mapping would be rewriting history, which the repository forbids;
#: recording it here instead keeps the exception visible and bounded.
PRE_SEMANTICS_EXCEPTIONS = frozenset(
    {("63cfb2cc1ea7ac4e1c7a8c7ef3170f45949e9439", "T001", "BLOCKED", "CHANGES_REQUESTED")}
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout


class GitArtifacts:
    """An :class:`ArtifactSource` over one historical revision.

    The path set is read once per revision; blob contents are fetched lazily and
    cached, because only a handful of the committed artifacts are ever consulted
    and fetching them all would dominate the walk.
    """

    def __init__(self, sha: str) -> None:
        self._sha = sha
        self._paths = set(
            _git(
                "ls-tree",
                "-r",
                "--name-only",
                sha,
                "--",
                "todo/reviews",
                "todo/triage",
                "todo/evidence",
            ).splitlines()
        )
        self._cache: dict[str, Mapping[str, Any]] = {}

    def exists(self, path: str) -> bool:
        return path in self._paths

    def read(self, path: str) -> Mapping[str, Any]:
        if path not in self._cache:
            try:
                payload = json.loads(_git("show", f"{self._sha}:{path}"))
            except (json.JSONDecodeError, subprocess.CalledProcessError) as error:
                raise WorkflowError(f"artifact {path} is unreadable: {error}") from error
            if not isinstance(payload, Mapping):
                raise WorkflowError(f"artifact {path} is not a JSON object")
            self._cache[path] = payload
        return self._cache[path]


class DictArtifacts:
    """An :class:`ArtifactSource` over a literal mapping, for the rule tests."""

    def __init__(self, files: Mapping[str, object]) -> None:
        self._files = files

    def exists(self, path: str) -> bool:
        return path in self._files

    def read(self, path: str) -> Mapping[str, Any]:
        if path not in self._files:
            raise WorkflowError(f"artifact {path} does not exist")
        payload = self._files[path]
        if not isinstance(payload, Mapping):
            raise WorkflowError(f"artifact {path} is not a JSON object")
        return payload


def _derive(
    files: Mapping[str, object],
    *,
    attempt: int = 1,
    approved_commit: str | None = None,
) -> str:
    return derive_status(
        phase="P00",
        task_id="T001",
        attempt=attempt,
        approved_commit=approved_commit,
        artifacts=DictArtifacts(files),
    )


REVIEW = "todo/reviews/P00/T001/review-001.json"
TRIAGE = "todo/triage/P00/T001/triage-001.json"
DECISION = "todo/triage/P00/T001/owner-decision-001.json"
PLANNER = "todo/evidence/P00/T001/attempt-001-planner.json"
PLAN_REVIEW = "todo/reviews/P00/T001/plan-review-001.json"
DEVELOPER = "todo/evidence/P00/T001/attempt-001-developer.json"
ABANDONED_RECORD = "todo/abandoned/T001.md"


# --------------------------------------------------------------------------
# the derivation rules
# --------------------------------------------------------------------------


def test_nothing_recorded_is_undetermined_by_artifacts() -> None:
    assert _derive({}, attempt=0) == UNSTARTED
    assert _derive({}, attempt=3) == IN_FLIGHT
    assert UNDERDETERMINED[UNSTARTED] == {"PLANNED", "READY"}
    assert UNDERDETERMINED[IN_FLIGHT] == {"READY", "IN_DEVELOPMENT"}


def test_delivery_evidence_outranks_every_artifact() -> None:
    files = {DEVELOPER: {"outcome": "CANDIDATE_READY"}, REVIEW: {"verdict": "FAIL"}}
    assert _derive(files, approved_commit="a" * 40) == "APPROVED"


def test_abandonment_is_read_from_the_task_record_not_an_attempt() -> None:
    """Abandonment closes the work item, so its record is not attempt-stamped.

    It is written by `abandon-task` and must be derivable like every other
    status, or the consistency check would report a conflict on every abandoned
    task. Approval wins if both are somehow present: `abandon-task` refuses
    approved work, and the approval claim is the one that is immutable.
    """

    assert _derive({ABANDONED_RECORD: "# T001 abandonment"}) == "ABANDONED"
    assert _derive({ABANDONED_RECORD: "# T001 abandonment"}, approved_commit="a" * 40) == "APPROVED"


def test_every_artifact_kind_maps_to_its_recorded_status() -> None:
    cases = [
        ({DEVELOPER: {"outcome": "CANDIDATE_READY"}}, "AWAITING_REVIEW"),
        ({DEVELOPER: {"outcome": "TRIAGE_REQUIRED"}}, "TRIAGE_REQUIRED"),
        ({DEVELOPER: {"outcome": "BLOCKED"}}, "BLOCKED"),
        ({REVIEW: {"verdict": "FAIL"}}, "CHANGES_REQUESTED"),
        ({REVIEW: {"verdict": "TRIAGE_REQUIRED"}}, "TRIAGE_REQUIRED"),
        ({REVIEW: {"verdict": "BLOCKED"}}, "BLOCKED"),
        ({TRIAGE: {"classification": "IMPLEMENTATION_DEFECT"}}, "CHANGES_REQUESTED"),
        ({TRIAGE: {"classification": "SPEC_DEFECT"}}, "PLANNING"),
        ({TRIAGE: {"classification": "OWNER_DECISION_REQUIRED"}}, "OWNER_DECISION_REQUIRED"),
        ({TRIAGE: {"classification": "EXTERNAL_BLOCKED"}}, "BLOCKED"),
        ({DECISION: {"decision": "answered"}}, "PLANNING"),
        ({PLANNER: {"outcome": "PLAN_READY"}}, "AWAITING_PLAN_REVIEW"),
        ({PLANNER: {"outcome": "BLOCKED"}}, "BLOCKED"),
        ({PLAN_REVIEW: {"verdict": "FAIL"}}, "PLANNING"),
        ({PLAN_REVIEW: {"verdict": "BLOCKED"}}, "PLAN_REVIEW_BLOCKED"),
    ]
    for files, expected in cases:
        assert _derive(files) == expected, files


def test_a_passed_plan_review_is_recorded_as_changes_requested() -> None:
    """The composite field states the opposite of the verdict it records.

    A plan review PASS means "the correction is right, go implement it". The
    only value the composite field has for that is CHANGES_REQUESTED, which
    elsewhere means an implementation review failed. This test exists so the
    inversion is stated in one place rather than rediscovered: it is the
    clearest single piece of evidence that the field carries unrelated facts.
    """

    assert _derive({PLAN_REVIEW: {"verdict": "PASS"}}) == "CHANGES_REQUESTED"


def test_later_artifacts_outrank_earlier_ones_within_an_attempt() -> None:
    """Artifacts accumulate, so the newest event must decide, not the first."""

    assert _derive({DECISION: {}, PLANNER: {"outcome": "PLAN_READY"}}) == "AWAITING_PLAN_REVIEW"
    assert (
        _derive({PLANNER: {"outcome": "PLAN_READY"}, PLAN_REVIEW: {"verdict": "FAIL"}})
        == "PLANNING"
    )
    assert (
        _derive(
            {DEVELOPER: {"outcome": "CANDIDATE_READY"}, TRIAGE: {"classification": "SPEC_DEFECT"}}
        )
        == "PLANNING"
    )


def test_an_unrecognised_artifact_value_is_refused() -> None:
    """A malformed artifact must fail loudly, not silently derive a wrong value."""

    with pytest.raises(WorkflowError, match="unrecognised verdict"):
        _derive({REVIEW: {"verdict": "MAYBE"}})
    with pytest.raises(WorkflowError, match="unrecognised classification"):
        _derive({TRIAGE: {"classification": "UNKNOWN_KIND"}})
    with pytest.raises(WorkflowError, match="unrecognised outcome"):
        _derive({DEVELOPER: {"outcome": "SOMETHING_ELSE"}})
    with pytest.raises(WorkflowError, match="unrecognised verdict"):
        _derive({REVIEW: {}})
    with pytest.raises(WorkflowError, match="does not exist"):
        DictArtifacts({}).read(REVIEW)


def test_repository_artifacts_read_a_checked_out_tree(tmp_path: Path) -> None:
    target = tmp_path / REVIEW
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"verdict": "FAIL"}), encoding="utf-8")
    artifacts = RepositoryArtifacts(tmp_path)

    assert artifacts.exists(REVIEW)
    assert artifacts.read(REVIEW) == {"verdict": "FAIL"}
    assert not artifacts.exists("todo/reviews/P00/T002/review-001.json")

    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(WorkflowError, match="unreadable"):
        artifacts.read(REVIEW)


# --------------------------------------------------------------------------
# the equivalence proof over every revision of todo/config.yaml
# --------------------------------------------------------------------------


def _revisions() -> list[str]:
    revisions = _git("log", "--format=%H", "--", "todo/config.yaml").split()
    assert revisions, "no historical revisions found"
    return revisions


#: Every routing guard that used to read the composite ``status`` and now reads
#: the stored facts and the committed artifacts, as the status set it accepts.
#: The key names the command whose precondition it is.
ROUTING_GUARDS: dict[str, frozenset[str]] = {
    "ready": frozenset({"PLANNED"}),
    "develop": frozenset({"READY"}),
    "retry": frozenset({"CHANGES_REQUESTED"}),
    "retry-from-blocked": frozenset({"BLOCKED"}),
    "continue-develop": frozenset({"IN_DEVELOPMENT"}),
    "review": frozenset({"AWAITING_REVIEW"}),
    "triage": frozenset({"TRIAGE_REQUIRED"}),
    "plan": frozenset({"PLANNING", "OWNER_DECISION_REQUIRED"}),
    "review-plan": frozenset({"AWAITING_PLAN_REVIEW", "PLAN_REVIEW_BLOCKED"}),
    "dependency": frozenset({"APPROVED"}),
    "amendment-target": frozenset({"PLANNED", "APPROVED"}),
    "maintenance-related": frozenset({"APPROVED"}),
}

#: The task-revisions whose recorded status its own decomposition cannot admit.
#: Exactly the pre-semantics revision: the same one ``PRE_SEMANTICS_EXCEPTIONS``
#: pins for the derivation, seen from the routing side.
ROUTING_NOT_ADMITTED = frozenset({("63cfb2cc", "T001", "BLOCKED", "CHANGES_REQUESTED")})

#: The task-revisions where the facts admit the one underdetermined pair, and
#: therefore cannot say whether the work is selected or already running.
ROUTING_PAIR_CASES = {"IN_DEVELOPMENT": 1}

#: Every historical decision the two rules answer differently, and why each is
#: safe. A new entry here is a behaviour change and must be argued, not added.
#:
#: - ``develop``: the one pair case. The new rule admits ``READY``, which the
#:   facts cannot distinguish from ``IN_DEVELOPMENT``; ``prepare-develop``
#:   refuses moments later whenever a live runtime record exists, and that record
#:   is the authority on whether an attempt is running.
#: - the two ``retry`` rows: the pre-semantics revision, where the artifacts say
#:   ``CHANGES_REQUESTED`` while the record says ``BLOCKED``. The new rule
#:   follows the evidence in both directions.
ROUTING_DIVERGENCES = {
    ("develop", "IN_DEVELOPMENT", "IN_FLIGHT"): 1,
    ("retry", "BLOCKED", "CHANGES_REQUESTED"): 1,
    ("retry-from-blocked", "BLOCKED", "CHANGES_REQUESTED"): 1,
}


def test_every_guard_answers_the_same_over_the_whole_history() -> None:
    """The migration's judge: the facts must reproduce every recorded decision.

    Each revision of ``todo/config.yaml`` carries both the status a transition
    recorded and the evidence committed with it, so the whole history is an
    oracle for routing as much as for the derivation. For every task-revision
    and every guard, the answer read from the facts must equal the answer read
    from the recorded field -- except where the two are enumerated above, with
    the reason each is safe.
    """
    not_admitted: Counter[tuple[str, str, str, str]] = Counter()
    pair_cases: Counter[str] = Counter()
    divergences: Counter[tuple[str, str, str]] = Counter()
    total = 0

    for sha in _revisions():
        config = json.loads(_git("show", f"{sha}:todo/config.yaml"))
        artifacts = GitArtifacts(sha)
        for task_id, task in config["tasks"].items():
            recorded = task["status"]
            derived = derive_status(
                phase=task["phase"],
                task_id=task_id,
                attempt=task["attempt"],
                approved_commit=task["approved_commit"],
                artifacts=artifacts,
            )
            lifecycle, claimed = LIFECYCLE_OF[recorded]
            admitted = admitted_statuses(lifecycle=lifecycle, claimed=claimed, derived=derived)
            total += 1
            if admitted == UNDERDETERMINED[IN_FLIGHT]:
                pair_cases[recorded] += 1
            if recorded not in admitted:
                not_admitted[(sha[:8], task_id, recorded, derived)] += 1
            for name, accepted in ROUTING_GUARDS.items():
                if (recorded in accepted) != bool(admitted & accepted):
                    divergences[(name, recorded, derived)] += 1

    # Shape rather than a fixed total: the plan may grow, but a walk that
    # suddenly covers a fraction of the history is a broken test, not a clean
    # repository.
    assert total > 18_000, f"only {total} task-revisions walked"

    assert dict(not_admitted) == {key: 1 for key in ROUTING_NOT_ADMITTED}, (
        "a recorded status its own facts do not admit appeared outside the "
        f"documented pre-semantics revision: {dict(not_admitted)}"
    )
    assert dict(pair_cases) == ROUTING_PAIR_CASES
    assert dict(divergences) == ROUTING_DIVERGENCES, (
        "routing now answers differently from the recorded status in cases that "
        f"are not documented: {dict(divergences)}"
    )


def test_derived_status_reproduces_every_historical_revision() -> None:
    revisions = _revisions()

    reproduced: Counter[str] = Counter()
    underdetermined: Counter[str] = Counter()
    exceptions: set[tuple[str, str, str, str]] = set()
    unexpected: list[str] = []

    for sha in revisions:
        config = json.loads(_git("show", f"{sha}:todo/config.yaml"))
        artifacts = GitArtifacts(sha)
        for task_id, task in config["tasks"].items():
            recorded = task["status"]
            derived = derive_status(
                phase=task["phase"],
                task_id=task_id,
                attempt=task["attempt"],
                approved_commit=task["approved_commit"],
                artifacts=artifacts,
            )
            if derived == recorded:
                reproduced[recorded] += 1
            elif recorded in UNDERDETERMINED.get(derived, frozenset()):
                underdetermined[recorded] += 1
            else:
                exceptions.add((sha, task_id, recorded, derived))
                unexpected.append(
                    f"{sha[:8]} {task_id} attempt={task['attempt']} "
                    f"recorded={recorded} derived={derived}"
                )

    unknown = exceptions - PRE_SEMANTICS_EXCEPTIONS
    assert not unknown, (
        "the derivation disagrees with history for reasons that are not the "
        "documented pre-2026-09-15 semantics:\n  " + "\n  ".join(sorted(unexpected))
    )

    # The exception must stay exactly as documented: a second one is a new
    # finding, and a vanished one means the exception list is stale.
    assert exceptions == PRE_SEMANTICS_EXCEPTIONS

    # Every status the history actually used, other than the two markers that
    # mean "no artifact decides this", must be reproduced. Shape checks rather
    # than fixed totals, so the plan may grow freely.
    expected_derivable = {
        "APPROVED",
        "AWAITING_REVIEW",
        "CHANGES_REQUESTED",
        "TRIAGE_REQUIRED",
        "PLANNING",
        "AWAITING_PLAN_REVIEW",
        "OWNER_DECISION_REQUIRED",
        "BLOCKED",
    }
    assert set(reproduced) == expected_derivable
    assert sum(reproduced.values()) > 9_000

    # PLANNED dominates simply because most task-revisions have not started.
    # What matters is how many *started* work items still need the control
    # plane: that is the real cost of keeping the composite field, and it should
    # stay at the size of the two selection facts the model expects.
    assert set(underdetermined) <= {"PLANNED", "READY", "IN_DEVELOPMENT"}
    started = sum(count for status, count in underdetermined.items() if status != "PLANNED")
    assert started < 200, (
        f"{started} started work items needed the control plane to explain their "
        "status; the model expects only the READY / IN_DEVELOPMENT selection facts"
    )


def test_history_records_in_development_exactly_once() -> None:
    """The measured evidence that IN_DEVELOPMENT is not durable state.

    It is written into the live worktree and never committed: a controller
    transition sets it in the working tree only, and the next commit records the
    sealed candidate instead. The single committed occurrence is a hand-made
    bootstrap commit, which is why it is named here by SHA -- if a future
    controller change starts committing this value, this test says so.
    """

    occurrences = [
        sha
        for sha in _git("log", "--format=%H", "--", "todo/config.yaml").split()
        if '"status": "IN_DEVELOPMENT"' in _git("show", f"{sha}:todo/config.yaml")
    ]
    assert occurrences == ["d697dd7899409c20a9178cf2bafba67848b5cfad"]
