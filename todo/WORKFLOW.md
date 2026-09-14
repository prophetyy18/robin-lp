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
The Manager session starts with `--model MiniMax-M3[1m]`; every project Agent uses
`model: inherit`. Fallback is prohibited and `validate` checks the declared runtime.
Authentication stays in user-level Claude Code settings and is never printed or
persisted in this repo.

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
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow prepare-develop T001
# Invoke the returned stage-developer visibly, then:
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow finish-develop T001
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow prepare-review T001
# Invoke the returned stage-reviewer visibly, then:
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow finish-review T001
```

After T001 is approved, the Manager may select one dependency-complete planned
task and activate it explicitly:

```bash
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow ready T002
```

### Claude Code as the Manager

The interactive Claude Code session in the main checkout may orchestrate these
commands, but it must not implement or review the task itself. Tell it the exact
task ID and instruct it to:

1. read `AGENTS.md`, `CLAUDE.md`, this file, `config.yaml`, `README.md`, and the
   selected task contract;
2. run `validate` and `status`;
3. call `prepare-develop <task>`, visibly invoke the returned Developer, then
   call `finish-develop <task>` after its handoff exists;
4. call `prepare-review <task>`, visibly invoke the returned Reviewer, then
   call `finish-review <task>`;
5. call `prepare-retry <task>` followed by the same finish and a fresh review after
   `CHANGES_REQUESTED`;
6. use `prepare-triage`/`finish-triage` only after `TRIAGE_REQUIRED`;
7. use `prepare-plan`/`finish-plan` and
   `prepare-plan-review`/`finish-plan-review` only when triage routes the issue
   to planning; ask the owner before passing `--owner-decision`;
8. stop on `BLOCKED`, `OWNER_DECISION_REQUIRED`, or `APPROVED`, report the evidence and
   commit SHAs, and never start the next numbered task automatically.

The ready-to-copy Manager prompt is in the root `README.md`. A request such as
“implement T001 directly” is not a valid workflow invocation because it does not
preserve role isolation.

## Permission model

There are three separate permission boundaries:

- **Manager session:** start with `claude --permission-mode manual`. The operator
  approves only the expected `python -m tools.workflow <action> <task>` command.
- **Developer Agent:** its definition uses `acceptEdits` with an explicit tool
  allowlist. It may edit implementation files in its development worktree, but
  cannot invoke Agent, commit, push, merge, deploy, sign or broadcast.
- **Reviewer Agent:** its definition uses `dontAsk`, omits Edit, and allows Write
  only for its declared `.workflow/review-result.json` handoff. Any other tracked,
  staged or untracked file invalidates the review.

The controller itself performs the required Git worktree, candidate commit and
approved fast-forward operations under the server user's normal filesystem
permissions. It never uses `sudo` and never pushes. A dirty main checkout,
unwritable worktree parent, Git lock, denied child command, unavailable network,
or missing credential causes the command to stop; do not bypass the gate by
loosening global permissions. Preserve the error, inspect `status` and
`git worktree list`, then correct the specific host permission or configuration
before retrying. A contract, Spec or owner question requires triage and the
matching planning route, not a filesystem permission override.

If review returns `CHANGES_REQUESTED`, start a new developer process:

```bash
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow prepare-retry T001
# Invoke Developer, finish-develop, prepare-review, invoke Reviewer, then:
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow finish-review T001
```

Exceptional scope or specification discoveries use:

```bash
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow prepare-triage T001
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow finish-triage T001
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow prepare-plan T001
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow finish-plan T001
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow prepare-plan-review T001
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow finish-plan-review T001
```

If triage returns `OWNER_DECISION_REQUIRED`, stop and ask the owner. Only after
the owner answers may the Manager run `prepare-plan T001 --owner-decision "..."`. Triage
is an exception path, not a mandatory ceremony. The planning role may return
`NO_CHANGE_REQUIRED`; an independent Plan Reviewer can accept that evidence
without forcing a meaningless edit.

Python never invokes Claude Code. Each `prepare-*` command returns the Agent name,
worktree, immutable SHAs and prompt. The Manager invokes that Agent through Claude
Code, where the owner can inspect its transcript and send follow-ups. The Agent
writes only its declared `.workflow/*.json` handoff; the matching `finish-*`
command validates and records it.

## Commit and branch behaviour

The controller, not an Agent:

1. requires a clean invoking checkout and approved dependencies;
2. creates `workflow/<task>-attempt-<number>` and an isolated development worktree;
3. snapshots every protected Intent, Spec, task and workflow file;
4. returns the Developer manifest for the Manager to invoke visibly;
5. rejects any protected-file change, records evidence and creates a candidate commit;
6. creates the detached Reviewer worktree and returns its visible Agent manifest;
7. rejects a Reviewer that changes tracked files;
8. persists both JSON and Markdown review reports;
9. retains a failed branch for a fresh retry, or fast-forwards an approved branch;
10. never pushes, deploys or sends an external message.

The approval metadata commit follows the reviewed candidate. It may contain only
`todo/config.yaml` and the generated review JSON/Markdown. `approved_commit` records
the exact implementation candidate that the Reviewer inspected.

## Failure handling

- `TRIAGE_REQUIRED`: run the independent triager; the reporting Agent's proposed
  classification is not authoritative.
- `PLANNING` / `AWAITING_PLAN_REVIEW`: correct only the triaged contract or Spec
  issue, then obtain independent plan review.
- `PLAN_REVIEW_BLOCKED`: retain the exact plan candidate and run `review-plan`
  again after the external review blocker is resolved; do not replan merely to
  clear the state.
- `OWNER_DECISION_REQUIRED`: stop and ask the owner; permissions cannot answer a
  product question.
- `BLOCKED`: preserve the branch and evidence for external/user action.
- `CHANGES_REQUESTED`: preserve the branch; `retry` starts a fresh Developer.
- inconsistent `PASS`, wrong SHA, malformed JSON, protected-file edits or a dirty
  Reviewer worktree invalidate the run.

Planning path permissions are broad enough to avoid artificial dead ends but
remain layer-bounded: contract triage may change task contracts and dependency
fields; Spec triage may additionally change `docs/spec/`; an owner decision may
additionally change `docs/intent/`. The controller rejects changes to task state,
attempts, evidence pointers, commit SHAs, model selection and approval data.
Plan Reviewer—not a brittle filename rule—decides whether changes inside the
allowed planning layer are relevant and proportionate.

If triage is raised after partial implementation work, the controller preserves
that work in the task branch as a diagnostic snapshot. Planning review compares
only the exact planning base and candidate, and a fresh Developer later decides
whether the preserved implementation remains useful under the corrected
contract. Both implementation reviews and planning reviews produce JSON evidence
and a human-readable Markdown report.

Worktree deletion is never attempted for a failed development branch. A detached
review worktree that contains tracked modifications is retained for diagnosis.

## Pre-existing unverified implementation

Implementation code that exists in the repository but was **not** produced
by the `develop → review` workflow for the specific task it nominally
satisfies is "pre-existing unverified implementation" (previously referred
to informally as "orphan code"). It remains in the Git history as evidence;
it does **not** advance any task's status in `todo/config.yaml`. `APPROVED`
still requires an independent Reviewer verdict on the exact candidate SHA.

### Migration rule

The migration rule for pre-existing unverified implementation is
**preserve, review per task, claim after approval**.

1. **Preserve.** No migration step rebases, squashes, or deletes the
   pre-existing commits. The current `main` HEAD and every historical
   commit on it remain intact until each task is independently approved
   by the independent Reviewer Agent.
2. **Review per task.** For each `PLANNED` task whose contract already has
   a pre-existing implementation, the Manager activates that task normally
   (`ready`, then `prepare-develop`). The Developer Agent receives the current
   `main` HEAD as its base commit, reads and cites the pre-existing code,
   and may reuse or extend it rather than rewrite it from scratch. Code
   reuse must still satisfy the task contract's Outcome, Deliverables,
   Acceptance, and Must-not clauses; the *Implementation status* notes in
   each task contract enumerate the gaps that must be closed.
3. **Claim after approval.** A task is not delivered until the independent
   Reviewer returns `APPROVED` for the Developer candidate commit. Until
   then, the pre-existing code is a hint, not acceptance. No commit is
   fast-forwarded into the `approved_commit` slot without the Reviewer's
   verdict on the exact candidate SHA.

If the Developer discovers that the pre-existing code is missing acceptance
evidence already recorded in the task contract (for example, a known defect
listed under *Implementation status*), the Developer must close that gap
inside the same task — deferring it is not allowed. If the Developer finds
a contract or Spec defect, it returns `TRIAGE_REQUIRED` and the planning
path applies as usual; the pre-existing branch is preserved as a diagnostic
snapshot per the Failure handling section above.

Pre-existing implementation is **never** fast-forwarded directly into
`APPROVED`. Every fast-forward operation runs through the controller and is
bound to the exact candidate SHA the Reviewer inspected.

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
