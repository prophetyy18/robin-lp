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

### Recorded state must be backed by evidence

Every transition writes its evidence — the review, triage report, owner decision,
planner or developer handoff, or abandonment record — in the *same commit* as the
state it explains. So the two must agree, and the controller checks that they do:
`derive_status` reconstructs a task's state from the committed artifacts alone,
and

- `validate` refuses a task whose stored facts add up to no state at all — a
  lifecycle and artifacts that contradict each other — because such a record did
  not come from a transition, and hand-editing `todo/config.yaml` is how six tasks
  were once added with no author role and no review. Running the workflow from a
  state nothing can explain is what must not happen;
- `status` reports every disagreement under `status_conflicts`, so an operator
  sees it while inspecting rather than when blocked. A revision that still carries
  the retired composite is judged there too: the recorded value must be one the
  facts admit.

`PLANNED`, `READY` and `IN_DEVELOPMENT` are the states no committed artifact can
distinguish by itself, because they differ only by the Manager's selection and by
whether an attempt is running — and `claimed` now carries the first of those
facts, so only the last remains. The whole history is the evidence, not a sample:
over all 281 revisions of `todo/config.yaml` (18,580 task-revisions) the
derivation reproduces every recorded value except one, and that one is a
pre-2026-09-15 semantics difference that must not be rewritten.
`tests/test_workflow_state_derivation.py` holds the walk and the named exception.

### What a task record actually says

Each task carries the two facts the composite status was hiding, so that no reader
has to decode an enum to find them:

- `lifecycle` — `OPEN`, `DELIVERED` or `ABANDONED`: the task's own state;
- `claimed` — whether the task holds the single-active-work lane.

The composite itself is retired: `status` is neither written nor stored any more,
and a reader that needs a single name projects one from those two facts and the
committed artifacts. `admitted_statuses` in `tools/workflow/core.py` is that
projection, and it is the only place the mapping lives; `project_status` turns it
into a name for a message or a report, and `LIFECYCLE_OF` turns a status back into
the two facts, which is how the historical revisions are read.

Routing reads the facts, never the field: every `prepare-*` / `finish-*` guard
asks whether the state it requires is one the facts admit, so editing a field by
hand cannot move a command.

Two properties are enforced rather than assumed. Where a revision still stores the
composite it and the facts must describe the same task, because storing one fact
twice is only safe while a disagreement is impossible. And a `lifecycle` is not
self-certifying: `DELIVERED` is admitted only when `approved_commit` and the
reviewer's verdict are present, and `ABANDONED` only when the Owner's record is,
so setting a field by hand can never declare work delivered.

Exactly one state remains genuinely undetermined by the artifacts: a claimed task
that has produced nothing is either sealed for review or still being worked on,
and *which* is a session fact no committed artifact records. The controller reads
its own runtime record; a reader that has none reads the config's declaration of
its active task, which is a statement about that very task because only the active
task may hold the lane.

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

If a healthy Developer run needs another session, its
`CONTINUATION_REQUIRED` handoff is consumed with:

```bash
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow continue-develop T001
```

The command preserves the task's `IN_DEVELOPMENT` state, attempt, branch and
worktree, then returns a prompt for a fresh Developer. If the visible Agent is
stopped specifically by its hard `maxTurns` limit before it can write the
handoff, the Manager may instead pass `--max-turns-exhausted`; the controller
creates a minimal checkpoint from the retained worktree. This flag is not a
substitute for `BLOCKED` or `TRIAGE_REQUIRED`. One continuation is allowed per
attempt, giving the Developer two sessions while retaining a finite cumulative
budget.

After T001 is approved, the Manager may select one dependency-complete planned
task and activate it explicitly:

```bash
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow ready T002
```

### Owner-directed amendments before implementation

Route a change by the highest authority it changes. Do not split one behavioral
decision across roles or let a lower layer reinterpret it:

| Requested change | Author | Route | Boundary |
| --- | --- | --- | --- |
| New or changed V1 goal, plan structure, phase ownership or collateral | Prophet | `PROPHET` | May add tasks; never edits an existing task |
| Changed Spec plus still-`PLANNED` contracts | Planner | `SPEC` | Named planned tasks and relevant `docs/spec/` only |
| Clarification confined to still-`PLANNED` contracts | Planner | `CONTRACT` | Named contracts and their dependency fields only |
| Retirement of already-`APPROVED` work | Planner | `SUPERSEDE` | Only each target's `superseded_by` annotation |
| Ordinary implementation of an accepted contract | Developer | develop/review | Implementation, tests and allowed delivery files |
| Reproduced low-risk implementation defect | Maintenance Developer | maintenance | Predeclared paths; no product, risk, execution or signer change |

If one Owner request spans rows, begin at the highest row, record every lower-layer
impact, then execute the required lower-layer amendments in dependency-safe order. An
unclear product choice stops at `OWNER_DECISION_REQUIRED`; an Agent never silently
chooses wording that is easier to implement.

When the Owner explicitly directs a planning change for one or more tasks that
are still `PLANNED`, do not manufacture a Developer failure. Prepare one reviewed
amendment instead:

```bash
python -m tools.workflow prepare-amendment \
  --task T031 --task T032 --task T033 --task T034 \
  --layer CONTRACT \
  --summary "merge the binding P03 owner amendments" \
  --owner-direction "<exact Owner direction>"
# Invoke the returned Planner visibly, then:
python -m tools.workflow finish-amendment A0001
python -m tools.workflow prepare-amendment-review A0001
# Invoke the returned Plan Reviewer visibly, then:
python -m tools.workflow finish-amendment-review A0001
```

`CONTRACT` permits only the named task contracts and their dependency fields.
`SPEC` additionally permits `docs/spec/` and `spec_revision`; `PROPHET` targets no
task: it states Intent, restructures the plan and corrects collateral documents,
may add tasks, and may change both revisions; `SUPERSEDE` targets `APPROVED` tasks
and annotates each with the successor it names in `superseded_by`. The Planner authors
CONTRACT, SPEC and the annotation-only SUPERSEDE layer; the Prophet authors only
PROPHET. Every route
preserves task status, implementation attempts, evidence, commit identities and
approval metadata. A PASS fast-forwards the reviewed amendment into the clean
invoking checkout without changing any target's status. FAIL retains the amendment
worktree and uses `prepare-amendment-retry <id>` on the same layer; resolved
BLOCKED amendments use the same retry gate. One amendment may target at most eight
tasks. Only one amendment may be *active*, and none may start while a product task
is unfinished.

A closed amendment keeps its record and its number. A change that will not land is
closed with

```bash
python -m tools.workflow withdraw-amendment A0014 \
  --reason "<why it will not land>"
```

which records the reason in `todo/amendments/<id>/` on the amendment's branch,
applies nothing, and leaves the record as a terminal entry: the lane is free for
the next change, and the ID is never handed out twice, so a re-issued change cannot
collide with a branch or worktree path the closed one still owns. Deleting a record
by hand is not the way to reopen the lane — it rewinds the ID counter onto names
that are still in use.

`finish-amendment` also runs the repository's deterministic gates — the citation,
acceptance-criteria and import-graph checks — and refuses a candidate that *adds* a
finding to any of them. The comparison is a delta against the attempt's base, never
"no findings at all": an already-red repository must stay amendable, or the gate
would be a dead end of its own. This is where a mechanical failure is caught, at the
moment the candidate is sealed; the independent review stays the place where
judgement is exercised, and it still re-runs every gate.

#### Closing work that will not land

Every non-terminal status has an Owner exit, and that is a property of the
state machine rather than a convention: `finished` work is not the only way a
work item may end. A task whose lane cannot be moved — an Agent stopped before
it wrote its handoff leaves a worktree no command will accept, and an external
dependency can deny progress for as long as it likes — is closed with

```bash
python -m tools.workflow abandon-task T073 \
  --reason "<why this will only be re-issued, if it is wanted at all>"
python -m tools.workflow abandon-maintenance M0002 \
  --reason "<why this repair does not fit the lane>"
```

An abandonment lands nothing. The branch, the worktree, the attempt and every
review record stay exactly as they were, so the abandoned work remains in Git
as evidence of what was tried; only the status and a record under
`todo/abandoned/<task>.md` move, and they move in the main checkout rather than
on the task branch, because nothing from that branch may enter the product.
The runtime attempt record is dropped only after that durable record has
captured where the work was retained, so the pointer outlives an unversioned
file under `.git/`.

`ABANDONED` is terminal, frees the single-active-work lane, and is refused for
`APPROVED` work: an approval is retired by the annotation-only `SUPERSEDE` route
below, never by rewriting a status. Abandoning is also refused while an open
task still depends on the target, because `_check_dependencies` requires every
dependency to be `APPROVED` and such a dependent could never activate. That
refusal redirects the Owner rather than trapping them: dependencies form a DAG,
so closing in reverse-dependency order always terminates.

`ESCALATED` is the maintenance lane's version of the same problem. It is
reached when the Developer reports that the repair does not fit the lane, and
the documented route is then a new numbered task — outside the lane entirely.
Nothing in the lane could close it, so `abandon-maintenance` is that exit.

#### A lost attempt

The other Owner exit repairs the controller's own records rather than the work
item. An attempt whose development worktree no longer exists cannot be
continued, finished, reviewed or triaged, and `prepare-develop` refuses to start
a new one while its runtime record is present — so the task cannot be worked on
at all, and every other route needs a worktree that is gone. A worktree can
disappear outside the controller, and records also survive from earlier
controller versions that did not clean up on approval:

```bash
python -m tools.workflow discard-attempt T015 \
  --reason "<how the worktree was lost>"
```

The record is written to `todo/evidence/<phase>/<task>/attempt-NNN-lost.json`
before the runtime record is deleted, because that file was the only place the
attempt's branch and commits were named; the protected snapshot is deliberately
kept, since the byte-exact-rebuild recovery reads it. The consumed attempt
number is never reused — the config is raised to at least the lost attempt, so
the next `prepare-develop` opens the one after it.

It moves no status. A task branch reaches the main checkout only through an
approval, so while an attempt is in flight the main checkout still shows
`PLANNED` or `READY`, and recovery needs no transition from either. A task in
any other *open* state is refused and redirected to `abandon-task`, which is
reachable from every state — the refusal names a route that works rather than
leaving the operator with a message and no move.

An `APPROVED` or `ABANDONED` task is served too, without the attempt bump. It
will never develop again, so its remnant needs no route; the point is that the
remnant must be clearable at all, or it would sit in `status` for good and the
diagnostic would stop meaning anything. `attempt` on an approved task is part of
what the reviewer inspected, so clearing a remnant there changes nothing else.

`status` reports every such record under `orphaned_attempts`, so a lost attempt
is visible before anyone tries to start the task it blocks.

The graph these rules describe is checked by
`tests/test_workflow_state_graph.py`, which asserts that every state is
reachable, that every non-terminal state can still reach a terminal state, that
every one has an Owner exit, and that the Agent named for each state actually
holds the permissions that state requires. The Agent capability table is parsed
from `.claude/agents/*.md` rather than written down a second time, so removing a
capability from a definition fails there instead of being discovered mid-run.

#### Retiring approved work

An `APPROVED` task is terminal: no other route may alter it. When an Owner
decision makes completed work obsolete, use the `SUPERSEDE` layer rather than
leaving an unexplained replacement task beside a stale rule:

```bash
python -m tools.workflow prepare-amendment \
  --task T038 --layer SUPERSEDE \
  --summary "retire the ten-million-block window rule in favour of T039" \
  --owner-direction "<exact Owner direction>"
```

`SUPERSEDE` targets `APPROVED` tasks and may change exactly one config field —
`superseded_by`, which must name an existing task. It may touch no contract,
dependency, document or approval evidence: the recorded state (`lifecycle` and
`claimed`, or the retired `status` where a revision still carries one), `attempt`,
every commit SHA, the evidence pointer and the review record stay byte-identical,
so the approval still describes exactly what was reviewed and the retirement is
an annotation recorded on top of it. A successor declares `replaces`, depends on
the work it replaces, and contains a `Replacement and migration` section covering
old implementation reachability, historical data/artifacts, runtime cutover,
downstream dependencies and verification. Every other `PLANNED` direct consumer
must be re-pointed before retirement; `finish-amendment` rejects a retirement that
would strand one, and `ready` refuses any task whose dependency closure contains an
open impact. The reason for retirement lives in `todo/amendments/<id>/`.

Because that layer may change nothing else, the ownership table has to name the
successor *before* the retirement is recorded. The obligation therefore attaches
where it can be met: creating a task that declares `replaces` obliges the same
change to name it on the replaced task's row in `ARCHITECTURE.md` §2.2, in the form
the citation resolver reads (`superseded by Txxx`). A successor created without that
annotation cannot be sealed — the citation check reports it — so a retirement never
has to edit a document its own layer may not write. Reading the row's relationship
parenthetical (`T105 (successor to T063)`) as a reference is *not* required and is
not done: the requirement lands on the row of the task being replaced, once.

#### Two rules for changing these rules

The workflow is enforced by `tools/`, which no role and no layer may edit — so a
rule change is an explicit bootstrap act, made by the Owner's direction rather than
by any agent's convenience. Two obligations apply to such a change:

- **Name the layer that must satisfy a new rule, and prove that it can.** A rule
  that requires work of a layer with no authority over what it requires is
  unsatisfiable, and two such rules can deadlock a change between them: the rule
  that a retirement name its successor in §2.2 and the rule that a retirement touch
  only `todo/config.yaml` once blocked each other exactly so. When no layer can
  satisfy a rule where it is written, it belongs as a check in the layer that *can*
  do the work, not as an obligation on a layer that cannot.
- **Keep derived state and evidence distinct.** Ownership maps, traceability tables
  and phase summaries are derived: they must agree with `todo/config.yaml` and the
  task contracts, and they may move when that authority moves. Approval evidence —
  contract text, the recorded state (`lifecycle`/`claimed`), `attempt`, commit
  identities, evidence pointers, review records — is immutable, and no rule may
  require it to move. A rule that makes a
  derived document follow an evidence field, or the reverse, has confused the two.

#### The PROPHET layer: goals, plan structure and collateral

Three axes had no route at all: the plan structure itself, the collateral
documents that describe the project, and the goal statements. The `PROPHET` layer
supplies them, and it is a *role* as well as a scope — the `prophet` agent
authors, the `prophet-reviewer` agent reviews. The layer is named for the role,
not for the Owner, so that the human Owner and the agent that restates their
goals are never confused.

```bash
python -m tools.workflow prepare-amendment \
  --layer PROPHET \
  --summary "record the goal and add the task that delivers it" \
  --owner-direction "<exact Owner direction>"
```

A PROPHET change takes no `--task`: it restructures the plan and may create tasks
that do not exist yet, so it targets none of them. It is the only layer that
allows an empty target set.

It may write `docs/intent/`, `docs/spec/`, `docs/implement/`, `README.md`,
`CLAUDE.md`, `AGENTS.md`, `todo/README.md`, `todo/WORKFLOW.md`, a phase
`README.md`, and the `todo/config.yaml` `tasks` entries for tasks it adds.

Two rules bound it, and both are enforced by the controller rather than left to
the reviewer:

- **It may create a new task contract and may never modify an existing one.** The
  status letter of the changed path is part of the test: a contract path that is
  not an addition is refused. This is what stops a goal restatement from quietly
  adding an obligation to an `APPROVED` contract, which would leave its
  `approved_commit` describing something other than what was reviewed. A new
  obligation belongs to a new task that supersedes the old one.
- **Every task that already exists is frozen.** Only newly added tasks and the two
  revisions may differ in `todo/config.yaml`. Status, attempt, commit SHAs,
  evidence pointers, review records, dependencies, phases and contract paths are
  all compared and must be byte-identical.

The freeze is measured against the commit the amendment *started* from, not the commit
the current attempt is based on. A retry re-bases its attempt on the previous candidate,
which would otherwise turn the amendment's own additions into pre-existing files it may
never touch — a repair route that cannot repair. Against the original base, a contract
the same amendment added stays editable (`A`) while every path that existed before the
amendment stays frozen (`M`) exactly as it was. Deletion stays refused everywhere.

Two consequences follow, and they are the reason the conflict record below exists rather
than an exception to the freeze. A PROPHET change cannot repair an existing contract, and
its own additions can leave one asserting something a governing document no longer says —
a page it says another task owns, a rule restated in the old wording, a task list that has
since grown. So every amendment result assesses Intent, Spec, contracts, dependencies,
implementation, data, operations, security and verification; states which existing
contracts it leaves stale (`affected_existing_tasks`) under stable per-conflict impact
IDs; and names the exact earlier impacts it repairs (`resolved_task_impacts`). An empty
list is a claim the independent review tests, not a default for an omission.
`finish-amendment` writes those declarations to `todo/amendments/<id>/impacts.json`, and
`ready` refuses to activate a task named by an open record, or a task depending on it,
until a later amendment resolves that exact impact. A goal restatement therefore cannot
silently leave an instruction that no longer matches the plan, and the affected task
cannot run on it in the meantime.

Impact records created before stable IDs were introduced remain immutable. The controller
exposes each one as `Axxxx:Txxx:legacy`; a later amendment resolves that synthetic ID
explicitly. It never rewrites the historical amendment or treats a task ID as permission
to clear every finding on that task.

Because a plan-structure change can add tasks without touching a line of Intent or Spec
text, the two revision strings alone cannot say that the plan moved. `todo/README.md`
carries a plan-structure revision line naming the last amendment that changed the plan's
structure, and every PROPHET change updates it even when both revisions stay byte-identical.

Everything else is refused by omission. `tools/workflow/`, `.claude/` and
`todo/schemas/` are in no role's and no layer's scope at all: only an explicit
bootstrap act changes them, and that is deliberate — a role that can rewrite the
gate it is checked by is not checked by anything. The dependency manifests
(`pyproject.toml`, `requirements.in`, `requirements.lock.txt`) are outside every
amendment layer and the maintenance lane, and only a Developer whose task contract
requires it changes them. `src/`, `tests/` and `.github/` are the working surface
of an ordinary development task — a Developer edits them inside its task contract
and the work counts only after an independent review, and the maintenance lane may
edit only the explicit paths its record declares in advance and may not touch
`.github/` or execution, risk or signer code.

Within `.claude/` and `todo/schemas/`, the PROPHET amendment lane is the
exception: it owns the small, explicit surface of *delegated governance*. The
predicate that decides which paths a PROPHET amendment may modify lives in
`tools/workflow/core_governance.py`, and the editable list inside that module
admits the three reviewer and manager agent prompts that are not PROPHET itself,
the three review-result schemas, and the predicate file itself. Everything
else inside `.claude/`, `todo/schemas/` and `tools/workflow/` — including the
PROPHET and prophet-reviewer role definitions, `tools/workflow/core.py`, and
the editable/forbidden file lists themselves — remains in the constitutional
bootstrap surface. A PROPHET amendment that touches any path outside the
editable list is refused by the controller's change-set check before the
reviewer is invoked, so a Prophet cannot grant itself authority by editing
its own gate. The dependency manifests
(`pyproject.toml`, `requirements.in`, `requirements.lock.txt`) are outside every
amendment layer and the maintenance lane, and only a Developer whose task contract
requires it changes them.
`.github/` or execution, risk or signer code.

Intent belongs to PROPHET rather than to the Planner. The Planner translates a
goal into task text, and on the triaged route it may reach `docs/intent/` only to
transcribe the Owner decision that `--owner-decision` was required to supply
before it would start. Authoring a goal and translating one are never the same
act performed by the same role.

### Claude Code as the Manager

The interactive Claude Code session in the main checkout may orchestrate these
commands, but it must not implement or review the task itself. Tell it the exact
task ID and instruct it to:

1. read `AGENTS.md`, `CLAUDE.md`, this file, `config.yaml`, `README.md`, and the
   selected task contract;
2. run `validate` and `status`;
3. call `prepare-develop <task>`, visibly invoke the returned Developer, then
   call `finish-develop <task>` after its terminal handoff exists; use
   `continue-develop <task>` for `CONTINUATION_REQUIRED`;
4. call `prepare-review <task>`, visibly invoke the returned Reviewer, then
   call `finish-review <task>`;
5. call `prepare-retry <task>` followed by the same finish and a fresh review after
   `CHANGES_REQUESTED`;
6. use `prepare-triage`/`finish-triage` only after `TRIAGE_REQUIRED`;
7. use `prepare-plan`/`finish-plan` and
   `prepare-plan-review`/`finish-plan-review` only when triage routes the issue
   to planning; ask the owner before passing `--owner-decision`;
8. stop on `BLOCKED`, `OWNER_DECISION_REQUIRED`, or `APPROVED`, report the evidence and
   commit SHAs, and never start the next numbered task automatically;
9. when no route forward exists, report that to the Owner and ask whether to close the
   work item with `abandon-task` or `abandon-maintenance` — an Agent never abandons
   another Agent's work on its own initiative.

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

## Low-risk maintenance repairs

A reproduced implementation defect may use the `Mxxxx` maintenance lane instead
of creating another product `Txxx` task when all of these are true:

- it does not change Intent, Spec, a task contract, product behavior, a public
  interface, a dependency, a data schema, a safety/risk rule, or chain execution;
- every editable file is known in advance (one to five explicit paths, no globs);
- the failure and the repair have concrete verification commands;
- it does not touch `.claude/`, `tools/workflow/`, `todo/config.yaml`, task/schema
  files, dependency manifests, or risk/execution/signer code.

The controller allocates the next `M0001`-style ID. Maintenance records live under
`todo/maintenance/<id>/` and never enter `todo/config.yaml`, the product dependency
graph, phase gates, or task-count tests. It still uses an isolated Developer,
candidate commit, detached independent Reviewer, and fast-forward approval:

```bash
python -m tools.workflow prepare-maintenance \
  --summary "replace unsupported test helper" \
  --reason "the pinned library does not expose it" \
  --path tests/example.py \
  --check "python -m pytest tests/example.py -q" \
  --related-task T012
# Invoke the returned stage-developer visibly.
python -m tools.workflow finish-maintenance-develop M0001
python -m tools.workflow prepare-maintenance-review M0001
# Invoke the returned stage-reviewer visibly.
python -m tools.workflow finish-maintenance-review M0001
```

Maintenance uses `continue-maintenance-develop M0001` under the same rules. The
`--max-turns-exhausted` recovery is allowed only when the visible maintenance
Developer was stopped by that hard limit.

If review returns `CHANGES_REQUESTED`, run
`prepare-maintenance-retry M0001`, invoke the fresh Developer, and repeat the
finish/review gates. The original request and allowed paths remain frozen.

If the repair needs another path or changes any excluded behavior, the Agent must
return `TRIAGE_REQUIRED`; the Manager then uses normal triage/planning or a new
numbered task. Planner is not the first stop for an ordinary implementation bug.

One explicit Owner instruction to execute a task or maintenance repair authorizes
the Manager to continue through the mechanical gates until the first terminal
result. Do not ask again between prepare, finish, and review. Pause on FAIL,
BLOCKED, TRIAGE_REQUIRED, OWNER_DECISION_REQUIRED, scope growth, or an external
side effect requiring new authority.

Run ordinary quality gates once in the Developer and once independently in the
Reviewer. Compare exit status and semantic results. Byte-for-byte comparison is
reserved for deterministic fixtures, protocol artifacts, and serialized outputs;
pytest timing, memory addresses, and other volatile console text are not evidence
of nondeterminism.

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
- no route forward: close the work item with `abandon-task` or
  `abandon-maintenance` rather than waiting for progress that cannot happen.
- a worktree that cannot be reached or completed: `status` lists it under
  `orphaned_attempts`; `discard-attempt` records the loss and lets the task
  start again.
- `CONTINUATION_REQUIRED`: keep `IN_DEVELOPMENT`, preserve the same attempt and
  uncommitted worktree, and start one fresh Developer through the continuation
  gate. It is neither a task-state transition nor exception triage.
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
