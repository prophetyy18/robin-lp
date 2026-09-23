"""Command-line entry point for the repository workflow controller."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import WorkflowError, WorkflowManager


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.workflow")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--worktree-root", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="validate configuration and repository wiring")
    subparsers.add_parser("status", help="show effective task state, including an active worktree")
    ready = subparsers.add_parser("ready", help="activate one dependency-complete PLANNED task")
    ready.add_argument("task_id")
    prepare_develop = subparsers.add_parser(
        "prepare-develop", help="prepare a worktree for a visible Claude Code developer"
    )
    prepare_develop.add_argument("task_id")
    finish_develop = subparsers.add_parser(
        "finish-develop", help="validate and seal a visible developer result"
    )
    finish_develop.add_argument("task_id")
    continue_develop = subparsers.add_parser(
        "continue-develop",
        help="continue an unfinished developer session in the same task attempt",
    )
    continue_develop.add_argument("task_id")
    continue_develop.add_argument("--max-turns-exhausted", action="store_true")
    prepare_retry = subparsers.add_parser(
        "prepare-retry", help="prepare a fresh visible developer after changes requested"
    )
    prepare_retry.add_argument("task_id")
    prepare_review = subparsers.add_parser(
        "prepare-review", help="prepare an exact detached worktree for a visible reviewer"
    )
    prepare_review.add_argument("task_id")
    finish_review = subparsers.add_parser(
        "finish-review", help="validate and record a visible reviewer result"
    )
    finish_review.add_argument("task_id")
    for name, help_text in (
        ("prepare-triage", "prepare a visible independent issue triager"),
        ("finish-triage", "validate and record a visible triage result"),
        ("finish-plan", "validate and seal a visible planner result"),
        ("prepare-plan-review", "prepare a visible independent plan reviewer"),
        ("finish-plan-review", "validate and record a visible plan review"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("task_id")
    prepare_plan = subparsers.add_parser(
        "prepare-plan", help="prepare a visible planner for a triaged issue"
    )
    prepare_plan.add_argument("task_id")
    prepare_plan.add_argument("--owner-decision")
    changed = subparsers.add_parser("check-paths", help="reject changes to protected paths")
    changed.add_argument("base_commit")
    amendment = subparsers.add_parser(
        "prepare-amendment",
        help="prepare an Owner-directed change: a planning amendment, or a PROPHET change",
    )
    # PROPHET restructures the plan and may create tasks that do not exist yet, so
    # it is the one layer that targets no task and needs no --task.
    amendment.add_argument("--task", dest="task_ids", action="append", default=[])
    amendment.add_argument(
        "--layer", choices=("CONTRACT", "SPEC", "PROPHET", "SUPERSEDE"), required=True
    )
    amendment.add_argument("--summary", required=True)
    amendment.add_argument("--owner-direction", required=True)
    for name, help_text in (
        ("finish-amendment", "validate and seal an Owner amendment candidate"),
        ("prepare-amendment-review", "prepare an independent Owner amendment review"),
        ("finish-amendment-review", "validate and record an Owner amendment review"),
        ("prepare-amendment-retry", "prepare amendment repair after review failure"),
        ("amendment-status", "show one Owner amendment's state"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("amendment_id")
    # The Owner's exit for a change that will not land: the lane stays open, the
    # record stays as a closed entry, and nothing from the candidate is applied.
    withdraw = subparsers.add_parser(
        "withdraw-amendment",
        help="close an Owner amendment that will not land, keeping its record",
    )
    withdraw.add_argument("amendment_id")
    withdraw.add_argument("--reason", required=True)
    # The Owner's exit for work whose lane cannot be moved. Lands nothing; keeps
    # the branch, worktree and evidence; frees the single-active-work lane.
    abandon_task = subparsers.add_parser(
        "abandon-task",
        help="close a task that will not land, keeping its record and branch",
    )
    abandon_task.add_argument("task_id")
    abandon_task.add_argument("--reason", required=True)
    abandon_maintenance = subparsers.add_parser(
        "abandon-maintenance",
        help="close a maintenance repair that will not land, keeping its record",
    )
    abandon_maintenance.add_argument("maintenance_id")
    abandon_maintenance.add_argument("--reason", required=True)
    maintenance = subparsers.add_parser(
        "prepare-maintenance",
        help="prepare a bounded low-risk repair outside the product task graph",
    )
    maintenance.add_argument("--summary", required=True)
    maintenance.add_argument("--reason", required=True)
    maintenance.add_argument("--path", dest="paths", action="append", required=True)
    maintenance.add_argument("--check", dest="checks", action="append", required=True)
    maintenance.add_argument("--related-task")
    for name, help_text in (
        ("finish-maintenance-develop", "validate and seal a maintenance developer result"),
        (
            "continue-maintenance-develop",
            "continue an unfinished maintenance developer session in the same attempt",
        ),
        ("prepare-maintenance-retry", "prepare a maintenance repair after review failure"),
        ("prepare-maintenance-review", "prepare an independent maintenance review"),
        ("finish-maintenance-review", "validate and record a maintenance review"),
        ("maintenance-status", "show one maintenance repair's state"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("maintenance_id")
        if name == "continue-maintenance-develop":
            command.add_argument("--max-turns-exhausted", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        manager = WorkflowManager(args.repo, worktree_root=args.worktree_root)
        if args.command == "validate":
            manager.validate_repository()
            output: object = {"status": "OK"}
        elif args.command == "status":
            output = manager.status()
        elif args.command == "ready":
            output = {"status": "READY", "commit": manager.ready(args.task_id)}
        elif args.command == "prepare-develop":
            output = manager.prepare_develop(args.task_id)
        elif args.command == "finish-develop":
            output = manager.finish_develop(args.task_id).to_dict()
        elif args.command == "continue-develop":
            output = manager.continue_develop(
                args.task_id,
                max_turns_exhausted=args.max_turns_exhausted,
            )
        elif args.command == "prepare-retry":
            output = manager.prepare_develop(args.task_id, retry=True)
        elif args.command == "prepare-review":
            output = manager.prepare_review(args.task_id)
        elif args.command == "finish-review":
            state, report = manager.finish_review(args.task_id)
            output = {"status": state, "report": str(report)}
        elif args.command == "prepare-triage":
            output = manager.prepare_triage(args.task_id)
        elif args.command == "finish-triage":
            state, report = manager.finish_triage(args.task_id)
            output = {"status": state, "report": str(report)}
        elif args.command == "prepare-plan":
            output = manager.prepare_plan(args.task_id, owner_decision=args.owner_decision)
        elif args.command == "finish-plan":
            plan = manager.finish_plan(args.task_id)
            output = (
                {"status": "AWAITING_PLAN_REVIEW", "plan": plan.to_dict()}
                if plan
                else {"status": manager.status()["workflow_state"]}
            )
        elif args.command == "prepare-plan-review":
            output = manager.prepare_plan_review(args.task_id)
        elif args.command == "finish-plan-review":
            state, report = manager.finish_plan_review(args.task_id)
            output = {"status": state, "report": str(report)}
        elif args.command == "check-paths":
            output = {"changed_paths": manager.check_changed_paths(args.base_commit)}
        elif args.command == "prepare-amendment":
            output = manager.prepare_amendment(
                task_ids=args.task_ids,
                layer=args.layer,
                summary=args.summary,
                owner_direction=args.owner_direction,
            )
        elif args.command == "finish-amendment":
            output = manager.finish_amendment(args.amendment_id).to_dict()
        elif args.command == "prepare-amendment-review":
            output = manager.prepare_amendment_review(args.amendment_id)
        elif args.command == "finish-amendment-review":
            state, report = manager.finish_amendment_review(args.amendment_id)
            output = {"status": state, "report": str(report)}
        elif args.command == "prepare-amendment-retry":
            output = manager.prepare_amendment_retry(args.amendment_id)
        elif args.command == "withdraw-amendment":
            output = manager.withdraw_amendment(args.amendment_id, reason=args.reason)
        elif args.command == "abandon-task":
            output = manager.abandon_task(args.task_id, reason=args.reason)
        elif args.command == "abandon-maintenance":
            output = manager.abandon_maintenance(args.maintenance_id, reason=args.reason)
        elif args.command == "amendment-status":
            output = manager.amendment_status(args.amendment_id)
        elif args.command == "prepare-maintenance":
            output = manager.prepare_maintenance(
                summary=args.summary,
                reason=args.reason,
                allowed_paths=args.paths,
                verification_commands=args.checks,
                related_task=args.related_task,
            )
        elif args.command == "finish-maintenance-develop":
            output = manager.finish_maintenance_develop(args.maintenance_id).to_dict()
        elif args.command == "continue-maintenance-develop":
            output = manager.continue_maintenance_develop(
                args.maintenance_id,
                max_turns_exhausted=args.max_turns_exhausted,
            )
        elif args.command == "prepare-maintenance-retry":
            output = manager.prepare_maintenance_retry(args.maintenance_id)
        elif args.command == "prepare-maintenance-review":
            output = manager.prepare_maintenance_review(args.maintenance_id)
        elif args.command == "finish-maintenance-review":
            state, report = manager.finish_maintenance_review(args.maintenance_id)
            output = {"status": state, "report": str(report)}
        elif args.command == "maintenance-status":
            output = manager.maintenance_status(args.maintenance_id)
        else:  # pragma: no cover - argparse owns this boundary
            raise WorkflowError(f"unsupported command {args.command}")
    except WorkflowError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
