# Claude Code adapter

`AGENTS.md` is the canonical shared repository policy. Claude Code must follow it;
this file adds only Claude-specific operating behavior.

@AGENTS.md

For workflow states, controller commands, amendment/maintenance routes, and
failure recovery, follow `todo/WORKFLOW.md`. Do not restate or reinterpret those
rules here.

## Manager behavior

When asked to execute the repository workflow, the outer Claude Code session acts
only as Manager:

- use the `workflow-manager` project agent and read current controller status;
- do not directly implement, plan, triage, review, fix, or approve specialist work;
- run only the controller-selected legal transition for the one selected task or
  maintenance item;
- after each `prepare-*` command, visibly launch the returned specialist through
  Claude Code's Agent tool with the returned prompt unchanged;
- wait for the structured handoff before running the matching `finish-*` command;
- never fabricate, edit, or summarize a handoff into a different conclusion; and
- never start another numbered task automatically.

Project specialist definitions live in `.claude/agents/`. The controller owns
worktrees, commit binding, protected-path checks, and state transitions; Python
must never launch Claude Code or hide a specialist run.

Continue ordinary authorized prepare/Agent/finish/review gates without asking for
approval at every step. Stop and report the recorded evidence on `BLOCKED`,
`OWNER_DECISION_REQUIRED`, `FAIL`/`CHANGES_REQUESTED`, `TRIAGE_REQUIRED`, an
explicit completed state, scope growth, or an external side effect requiring new
authority. Follow the next route named by `todo/WORKFLOW.md`; the Manager does not
invent a route or answer an Owner decision itself.

## Permissions and recovery

- Start the Manager with Claude Code permission mode `manual`. Approve only the
  exact, expected `tools.workflow` command for the current transition. Do not use
  `sudo`, direct edits, direct Git mutation, or broader permissions to bypass a
  failed gate.
- Specialist permission modes and tool allowlists come from their files under
  `.claude/agents/`; the Manager must not broaden them.
- A `CONTINUATION_REQUIRED` handoff is resumed only with the matching continuation
  command. Use `--max-turns-exhausted` only when Claude Code actually stopped that
  visible specialist at its hard turn limit before it could write the handoff.
  Preserve the controller-retained task, attempt, branch, and worktree.
- If context or a transcript is missing, recover from controller state and
  repository evidence. Do not treat chat memory as workflow state.

## Runtime

Run Claude Code from the repository root. Use the project Python, worktree root,
model, and provider settings documented in `todo/WORKFLOW.md`; credentials remain
in user-level Claude Code settings and must not be printed or persisted in the
repository.
