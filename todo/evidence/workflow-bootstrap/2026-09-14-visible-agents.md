# Visible Agent control-plane migration — 2026-09-14

This supersedes the Agent-launch mechanism described in the earlier bootstrap
record; it does not alter that historical evidence and does not approve T001.

## Decision

Claude Code is the interactive control plane. The Python workflow package is a
short-lived mechanical gate and contains no code that locates, launches, prompts,
or waits for Claude.

Every role follows `prepare-* → visible Claude Code Agent → finish-*`. Prepare
returns the exact role, worktree, SHAs and prompt. The Agent writes one declared
`.workflow/*.json` handoff. Finish validates identity, result shape, path limits,
Git state and legal transitions before committing workflow state.

## Verification

- `claude plugin validate .claude/agents`: passed for all six Agent definitions.
- Python source assertion: `_launch_agent`, `find_claude`, and a Claude executable
  literal are absent from `tools/workflow/`.
- Visible Developer → detached Reviewer → approval integration test: passed.
- Visible Owner decision → Planner → Plan Reviewer integration test: passed.
- Reviewer modification outside its one handoff file is rejected.
- Full suite: 280 passed, 2 pre-existing explicit skips.
- Ruff format/check and strict mypy: passed.

The owner can inspect and steer Agent transcripts in Claude Code. Conversation is
still not acceptance evidence; only a validated handoff bound to exact commits can
advance workflow state.
