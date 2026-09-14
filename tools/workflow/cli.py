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
        else:  # pragma: no cover - argparse owns this boundary
            raise WorkflowError(f"unsupported command {args.command}")
    except WorkflowError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
