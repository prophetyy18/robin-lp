# Isolated task workflow

This workflow separates reasoning roles and binds every handoff to Git commits.
The manager may choose a legal next action, but only `tools.workflow` performs
mechanical state transitions.

## Server prerequisites

The current server uses:

- Claude Code 2.1.269 at `/home/lpdev/.nvm/versions/node/v22.23.2/bin/claude`;
- project Python 3.12 at
  `/home/lpdev/miniconda3/envs/robinhood-lp/bin/python`;
- worktrees under `/home/lpdev/lp-worktrees/` by default.

Claude Code is authenticated through MiniMax's Anthropic-compatible endpoint.
The controller reads `agent_runtime` from `todo/config.yaml` and passes
`--model MiniMax-M3[1m]` on every Developer and Reviewer launch. Fallback is
prohibited. Agent frontmatter uses `model: inherit` only so that the controller's
explicit runtime selection remains authoritative. Authentication stays in the
user-level Claude Code settings and is never printed or persisted in this repo.

On this server the China endpoint is `https://api.minimax.cn/anthropic`. The
literal `[1m]` suffix is the Claude Code configuration form documented by MiniMax;
the API response reports the canonical serving model as `MiniMax-M3`. A local
`claude-code:unrecognized_model` diagnostic may still be emitted for this
third-party identifier, so success is determined by the final JSON envelope,
`is_error: false`, and a completed response—not by absence of that diagnostic.

## Source of truth

`todo/config.yaml` uses JSON syntax, which is valid YAML 1.2, so the bootstrap
controller can parse it with the Python standard library before T001 chooses and
locks dependencies. During an active failed/retry cycle, the newest config is on
the retained development branch; `python -m tools.workflow status` resolves that
branch through a private record under Git's common metadata directory. On PASS,
the branch is fast-forwarded into the invoking checkout and the tracked config
becomes current again.

Conversation text, an Agent's completion claim and uncommitted files are never
workflow state.

## Commands

Run from the repository root with the project Python:

```bash
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow validate
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow status
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow develop T001
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow review T001
```

### Claude Code as the Manager

The interactive Claude Code session in the main checkout may orchestrate these
commands, but it must not implement or review the task itself. Tell it the exact
task ID and instruct it to:

1. read `AGENTS.md`, `CLAUDE.md`, this file, `config.yaml`, `README.md`, and the
   selected task contract;
2. run `validate` and `status`;
3. call `develop <task>` only when that task is `READY`;
4. call `review <task>` after a candidate is produced;
5. call `retry <task>` followed by a fresh `review <task>` after
   `CHANGES_REQUESTED`;
6. stop on `SPEC_BLOCKED`, `BLOCKED`, or `APPROVED`, report the evidence and
   commit SHAs, and never start the next numbered task automatically.

The ready-to-copy Manager prompt is in the root `README.md`. A request such as
“implement T001 directly” is not a valid workflow invocation because it does not
preserve role isolation.

## Permission model

There are three separate permission boundaries:

- **Manager session:** start with `claude --permission-mode manual`. The operator
  approves only the expected `python -m tools.workflow <action> <task>` command.
- **Developer process:** the controller uses `acceptEdits` with an explicit tool
  allowlist. It may edit implementation files in its development worktree, but
  cannot invoke Agent, commit, push, merge, deploy, sign or broadcast.
- **Reviewer process:** the controller uses `dontAsk`, omits Edit/Write and uses a
  command allowlist. Anything requiring another permission is denied without an
  interactive prompt. Any tracked, staged or untracked file it leaves behind
  invalidates the review.

The controller itself performs the required Git worktree, candidate commit and
approved fast-forward operations under the server user's normal filesystem
permissions. It never uses `sudo` and never pushes. A dirty main checkout,
unwritable worktree parent, Git lock, denied child command, unavailable network,
or missing credential causes the command to stop; do not bypass the gate by
loosening global permissions. Preserve the error, inspect `status` and
`git worktree list`, then correct the specific host permission or configuration
before retrying. `SPEC_BLOCKED` requires planning or an owner decision, not a
filesystem permission override.

If review returns `CHANGES_REQUESTED`, start a new developer process:

```bash
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow retry T001
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow review T001
```

`develop` and `retry` invoke a new non-interactive Claude Code process. `review`
creates a detached worktree at the exact candidate SHA and invokes another new
process with the read-only reviewer definition.

## Commit and branch behaviour

The controller, not an Agent:

1. requires a clean invoking checkout and approved dependencies;
2. creates `workflow/<task>-attempt-<number>` and an isolated development worktree;
3. snapshots every protected Intent, Spec, task and workflow file;
4. launches the Developer without Git commit/push permission;
5. rejects any protected-file change, records evidence and creates a candidate commit;
6. launches the Reviewer in a detached worktree at that candidate;
7. rejects a Reviewer that changes tracked files;
8. persists both JSON and Markdown review reports;
9. retains a failed branch for a fresh retry, or fast-forwards an approved branch;
10. never pushes, deploys or sends an external message.

The approval metadata commit follows the reviewed candidate. It may contain only
`todo/config.yaml` and the generated review JSON/Markdown. `approved_commit` records
the exact implementation candidate that the Reviewer inspected.

## Failure handling

- `SPEC_BLOCKED`: return to a fresh Planner; do not guess or continue implementation.
- `BLOCKED`: preserve the branch and evidence for external/user action.
- `CHANGES_REQUESTED`: preserve the branch; `retry` starts a fresh Developer.
- inconsistent `PASS`, wrong SHA, malformed JSON, protected-file edits or a dirty
  Reviewer worktree invalidate the run.

Worktree deletion is never attempted for a failed development branch. A detached
review worktree that contains tracked modifications is retained for diagnosis.

## Bootstrap exercise

`tests/test_workflow.py::test_fake_fail_repair_pass_workflow` creates a temporary
Git repository and fake Claude-compatible executable. It exercises real Git
worktrees and commits through this sequence:

```text
READY -> first candidate -> FAIL -> CHANGES_REQUESTED
      -> fresh retry -> second candidate -> PASS -> APPROVED -> fast-forward
```

The fake executable makes the result deterministic and avoids treating an LLM's
opinion as proof that the controller works. A separate smoke invocation of the
installed Claude Code CLI verifies provider/client connectivity when explicitly run.
