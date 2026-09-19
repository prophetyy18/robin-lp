"""On-demand renderer of the configured plan (T008).

The renderer is read-only: every fact it prints comes from the
committed configuration at run time, and it never writes a file,
cache, or snapshot. The same package exposes the validation
checks the ``--check`` mode runs, so callers can drive the same
fail-closed rules from tests or other tooling.
"""

from __future__ import annotations

from .check import (
    NOTABLE_STATES,
    PHASE_PATTERN,
    PLAN_STATES,
    TASK_PATTERN,
    ProgressIssue,
    is_ready,
    load_config,
    validate,
)
from .render import (
    PHASE_RULE,
    SECTION_RULE,
    PhaseView,
    TaskView,
    build_view,
    parse_outcome,
    parse_phase_purpose,
    parse_title,
    render,
)

__all__ = [
    "NOTABLE_STATES",
    "PHASE_PATTERN",
    "PHASE_RULE",
    "PLAN_STATES",
    "PhaseView",
    "ProgressIssue",
    "SECTION_RULE",
    "TASK_PATTERN",
    "TaskView",
    "build_view",
    "is_ready",
    "load_config",
    "parse_outcome",
    "parse_phase_purpose",
    "parse_title",
    "render",
    "validate",
]
