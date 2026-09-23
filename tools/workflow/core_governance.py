"""Delegated-governance layer of the workflow controller.

This module owns the rules that decide whether a PROPHET amendment may modify a
given path, including the permanent blacklist of files that only an Owner
bootstrap may change. It is the smallest coherent surface that a delegated
PROPHET amendment may rewrite without weakening the root-of-trust invariants
defined in ``core.py``.

What is bootstrap-only (must NOT be modified by PROPHET):

* ``tools/workflow/core.py`` — state machine, validation, transitions
* ``tools/workflow/core_policy.py`` (if added later) — root authority
* ``.claude/agents/prophet.md`` and ``.claude/agents/prophet-reviewer.md`` —
  the role definitions that would let the role grant itself new authority
* the protected path prefixes enumerated in ``core.py`` (``PROTECTED_PREFIXES``)
* the editable/forbidden file lists themselves (this module's own contents)

What PROPHET may modify through this module:

* ``_PROPHET_EDITABLE_FILES`` — the explicit allow-list
* ``_PROPHET_EDITABLE_PREFIXES`` — the explicit prefix allow-list
* ``_prophet_path_allowed`` — the predicate that uses them
* ``_PROPHET_FORBIDDEN_FILES`` — the explicit deny-list (deny wins)
* ``_TASK_CONTRACT_PATH`` — the new-task-contract pattern
* ``_change_statuses`` — the helper that maps ``git diff`` output to single-letter
  Git statuses consumed by ``_prophet_path_allowed``

Anything not listed above in this module that is needed at runtime is
imported from ``.core`` (WorkflowManager, _git, etc.).
"""

from __future__ import annotations

import re
from pathlib import Path

from .core import _git

#: A new task contract a PROPHET change may create. Modifying an existing file
#: that matches this pattern is the one thing the Owner role must never do, so
#: the status letter is part of the test rather than an afterthought.
_TASK_CONTRACT_PATH = re.compile(r"^todo/phases/[^/]+/T[0-9]{3}\.md$")


#: Permanent deny-list: these paths may only be changed through an Owner
#: bootstrap. The check order in ``_prophet_path_allowed`` is deny-first, so any
#: path that appears here is refused even if it is also listed as editable.
_PROPHET_FORBIDDEN_FILES = frozenset(
    {
        # The role that defines Prophet itself cannot edit its own definition.
        ".claude/agents/prophet.md",
        # Same for the reviewer that validates Prophet's amendments.
        ".claude/agents/prophet-reviewer.md",
        # The root controller remains bootstrap-only. The root policy module,
        # if separated later, would also belong here.
        "tools/workflow/core.py",
        # The config file that records task state is bootstrap-only.
        "todo/config.yaml",
    }
)


#: Files a PROPHET change may edit freely. Everything absent from this set is
#: refused, so the enforcement core, the agent definitions, the schemas, CI and
#: the source tree stay out of reach without needing to be listed.
#:
#: Delegated-governance additions (the reason this module exists) let PROPHET
#: touch:
#:   * the three reviewer agent prompts that are not PROPHET itself
#:   * the workflow-manager agent prompt
#:   * the three review-result schemas
_PROPHET_EDITABLE_FILES = frozenset(
    {
        "README.md",
        "CLAUDE.md",
        "AGENTS.md",
        "todo/README.md",
        "todo/WORKFLOW.md",
        # Delegated governance additions:
        ".claude/agents/stage-reviewer.md",
        ".claude/agents/plan-reviewer.md",
        ".claude/agents/workflow-manager.md",
        "todo/schemas/review-result.schema.json",
        "todo/schemas/plan-review-result.schema.json",
        "todo/schemas/amendment-review-result.schema.json",
        # The governance predicates themselves are the surface this module
        # defines. Adding a contract file path here without changing this entry
        # would silently leave the predicates themselves bootstrap-only.
        "tools/workflow/core_governance.py",
    }
)
_PROPHET_EDITABLE_PREFIXES = ("docs/spec/", "docs/intent/", "docs/implement/")


def _prophet_path_allowed(path: str, status: str) -> bool:
    """Decide whether a PROPHET amendment may modify ``path``.

    Order: deny-first, then allow-list. A path on the permanent deny-list is
    refused even if it would otherwise be allowed, so the role definition and
    the root controller are protected regardless of how the editable lists
    evolve.
    """
    if path in _PROPHET_FORBIDDEN_FILES:
        return False
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


__all__ = [
    "_PROPHET_FORBIDDEN_FILES",
    "_PROPHET_EDITABLE_FILES",
    "_PROPHET_EDITABLE_PREFIXES",
    "_TASK_CONTRACT_PATH",
    "_prophet_path_allowed",
    "_change_statuses",
]
