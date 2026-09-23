"""Graph completeness checks for the workflow state machine.

``tools/workflow`` decides what may happen next; the Agent definitions under
``.claude/agents/`` decide who is *able* to do it. A state is only traversable
if both agree, and a rule that requires work from a role with no authority over
it is unsatisfiable -- two such rules can deadlock a change between them, which
is exactly what happened to the retirement route before 2026-09-20.

So the workflow is checked here as a graph:

* nodes are states;
* edges are legal transitions;
* every edge carries the actor that must walk it and what that actor must be
  able to do.

The questions asked are the ones an operator cares about: can every state be
reached, can every state be left, can a stuck work item always be closed, and
does the role named for each edge actually hold the permissions it needs.

The agent capability table is *parsed from the Agent definitions* rather than
written down a second time, so a definition edit that removes a capability
fails here instead of being discovered mid-run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.workflow.core import (
    ALLOWED_TRANSITIONS,
    MAINTENANCE_STATES,
    RESTING_STATES,
    STATES,
    TERMINAL_MAINTENANCE_STATES,
)

ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / ".claude" / "agents"

TERMINAL_TASK_STATES = frozenset({"APPROVED", "ABANDONED"})


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", f"{path.name} has no frontmatter"
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    raise AssertionError(f"{path.name} frontmatter is not terminated")


def _tools(field: str) -> set[str]:
    """Split a tool list, keeping ``Agent(a, b)`` as one entry.

    The Manager's definition lists the specialists it may spawn as
    ``Agent(stage-developer, stage-reviewer, ...)``, so a naive comma split
    would turn one permission into several nonsense ones.
    """
    tools: set[str] = set()
    buffer = ""
    depth = 0
    for character in field:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            tools.add(buffer.strip())
            buffer = ""
        else:
            buffer += character
    tools.add(buffer.strip())
    return {tool.split("(", 1)[0].strip() for tool in tools if tool.strip()}


def agent_capabilities(name: str) -> set[str]:
    """Capabilities implied by one Agent definition.

    ``Edit`` is the distinguishing capability: a role that has it can change the
    artifact it is looking at, and a role that does not cannot. ``Write`` alone
    is the handoff surface -- the reviewer definitions allow it for their one
    declared result file and nothing else.
    """
    fields = _frontmatter(AGENTS_DIR / f"{name}.md")
    allowed = _tools(fields.get("tools", ""))
    denied = _tools(fields.get("disallowedTools", ""))
    capabilities: set[str] = set()
    if "Read" in allowed:
        capabilities.add("read")
    if "Bash" in allowed:
        capabilities.add("bash")
    if "Write" in allowed:
        capabilities.add("handoff")
    if "Edit" in allowed and "Edit" not in denied:
        capabilities.add("edit")
    if "Agent" in allowed:
        capabilities.add("delegate")
    return capabilities


#: For each state that requires an Agent run, the agent the controller returns
#: and what that agent must be able to do to walk the state's edge. A producer
#: must be able to change the artifact; a checker must be able to read it and
#: write its verdict, and must NOT be able to change it.
REQUIRED_ACTOR: dict[str, tuple[str, frozenset[str]]] = {
    "IN_DEVELOPMENT": ("stage-developer", frozenset({"read", "bash", "edit"})),
    "PLANNING": ("planner", frozenset({"read", "bash", "edit"})),
    "AWAITING_REVIEW": ("stage-reviewer", frozenset({"read", "bash", "handoff"})),
    "AWAITING_PLAN_REVIEW": ("plan-reviewer", frozenset({"read", "bash", "handoff"})),
    "PLAN_REVIEW_BLOCKED": ("plan-reviewer", frozenset({"read", "bash", "handoff"})),
    "TRIAGE_REQUIRED": ("issue-triager", frozenset({"read", "bash", "handoff"})),
}

#: States whose next move is a controller command rather than an Agent run. The
#: Manager must be able to run the command and must NOT be able to do the work.
MANAGER_STATES = frozenset({"PLANNED", "READY", "CHANGES_REQUESTED", "BLOCKED"})

#: States whose next move only the human Owner can make.
OWNER_STATES = frozenset({"OWNER_DECISION_REQUIRED"})


def _reachable(start: str) -> set[str]:
    seen: set[str] = set()
    pending = [start]
    while pending:
        state = pending.pop()
        if state in seen:
            continue
        seen.add(state)
        pending.extend(ALLOWED_TRANSITIONS[state])
    return seen


def test_every_state_is_reachable_and_leavable() -> None:
    """No state is dead code and no state is a trap."""

    # Reachability: walk forward from PLANNED, the entry state, and confirm every
    # declared state is actually enterable.
    reachable = _reachable("PLANNED")
    assert reachable == STATES, f"unreachable states: {sorted(STATES - reachable)}"

    # Escape: every non-terminal state can still reach a terminal state. Cycles
    # such as CHANGES_REQUESTED -> IN_DEVELOPMENT -> AWAITING_REVIEW -> back are
    # fine because they can be exited; a state with no exit at all is not.
    for state in STATES - TERMINAL_TASK_STATES:
        forward = _reachable(state)
        assert forward & TERMINAL_TASK_STATES, f"{state} cannot reach any terminal state"


def test_every_nonterminal_state_has_an_owner_exit() -> None:
    """The Owner can always close a work item, whatever sub-state it is stuck in.

    This is the guarantee that makes the single-active-work lane releasable.
    Without it, a task whose lane cannot move holds the plan open forever, and
    no route can retire it: SUPERSEDE requires APPROVED, CONTRACT/SPEC require
    PLANNED, and deletion is refused everywhere.
    """

    for state in STATES - TERMINAL_TASK_STATES:
        assert "ABANDONED" in ALLOWED_TRANSITIONS[state], (
            f"{state} has no Owner exit: a work item stuck here could not be closed"
        )
    # Approved work is retired by the annotation-only SUPERSEDE route, never by
    # rewriting a status, so APPROVED must stay closed to abandonment.
    assert ALLOWED_TRANSITIONS["APPROVED"] == set()


def test_maintenance_lane_declares_an_exit_from_escalation() -> None:
    """``ESCALATED`` must not be terminal, and the lane must have a closer.

    The maintenance lane has no transition table -- statuses are assigned by the
    controller -- so only the structural guarantee can be asserted here: the
    escalated result of a Developer that says "this does not fit the lane" is
    not an ending, and abandonment is part of the lane's status set. That such a
    record is then actually closable is exercised behaviourally in
    ``tests/test_workflow.py``.
    """

    assert "ESCALATED" in MAINTENANCE_STATES
    assert "ESCALATED" not in TERMINAL_MAINTENANCE_STATES
    assert "ABANDONED" in MAINTENANCE_STATES
    assert frozenset({"APPROVED", "ABANDONED"}) == TERMINAL_MAINTENANCE_STATES


def test_every_lane_that_holds_the_work_slot_can_release_it() -> None:
    """Only resting states release the single-active-work lane."""

    assert frozenset({"PLANNED", "APPROVED", "ABANDONED"}) == RESTING_STATES
    for state in STATES:
        holds = state not in RESTING_STATES
        if holds:
            assert "ABANDONED" in ALLOWED_TRANSITIONS[state], (
                f"{state} holds the work slot but cannot release it"
            )


def test_required_actor_can_actually_act() -> None:
    """Each Agent run is performed by a role that holds the permissions for it."""

    for state, (agent, required) in REQUIRED_ACTOR.items():
        capabilities = agent_capabilities(agent)
        missing = required - capabilities
        assert not missing, f"{state} needs {agent} to have {sorted(missing)}"


def test_checkers_cannot_mutate_the_candidate() -> None:
    """Independent verification is independent because the checker cannot edit.

    The controller also enforces this at runtime by rejecting any changed file
    outside the handoff, but the definition is the first boundary and the one a
    future edit is most likely to relax by accident.
    """

    for state in ("AWAITING_REVIEW", "AWAITING_PLAN_REVIEW", "PLAN_REVIEW_BLOCKED"):
        agent, _ = REQUIRED_ACTOR[state]
        capabilities = agent_capabilities(agent)
        assert "edit" not in capabilities, f"{agent} can edit; it must not be able to"
        assert "handoff" in capabilities, f"{agent} cannot write its verdict"
        assert "delegate" not in capabilities, f"{agent} can spawn agents"
    assert "edit" not in agent_capabilities("issue-triager")


def test_producers_cannot_delegate_or_self_approve() -> None:
    """No producing role may spawn another agent to widen its own scope."""

    for agent in ("stage-developer", "planner", "prophet"):
        assert "delegate" not in agent_capabilities(agent), f"{agent} can spawn agents"


def test_manager_cannot_implement_or_review() -> None:
    """The Manager routes; it does not produce or check the artifact."""

    capabilities = agent_capabilities("workflow-manager")
    assert "edit" not in capabilities
    assert "handoff" not in capabilities
    assert "bash" in capabilities, "the Manager must be able to run the controller"
    assert "delegate" in capabilities, "the Manager must be able to invoke specialists"


def test_every_controller_agent_exists() -> None:
    """Every role the graph names has a definition on disk."""

    named = {agent for agent, _ in REQUIRED_ACTOR.values()} | {"workflow-manager"}
    for states in (MANAGER_STATES, OWNER_STATES):
        assert states <= STATES
    for agent in sorted(named):
        assert (AGENTS_DIR / f"{agent}.md").is_file(), f"{agent} has no definition"


@pytest.mark.parametrize("state", sorted(STATES))
def test_state_has_a_declared_next_actor(state: str) -> None:
    """Every state names who moves it, or is terminal.

    A state that is neither terminal nor assigned to a producer, a checker, the
    Manager or the Owner is a state nobody is responsible for.
    """

    if state in TERMINAL_TASK_STATES:
        return
    assert state in REQUIRED_ACTOR or state in MANAGER_STATES or state in OWNER_STATES, (
        f"{state} has no declared next actor"
    )
