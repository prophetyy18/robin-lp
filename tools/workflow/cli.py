"""Command-line entry point for the repository workflow controller."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import WorkflowError, WorkflowManager, find_claude


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.workflow")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--worktree-root", type=Path)
    parser.add_argument("--claude-command", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="validate configuration and repository wiring")
    subparsers.add_parser("status", help="show effective task state, including an active worktree")
    ready = subparsers.add_parser("ready", help="activate one dependency-complete PLANNED task")
    ready.add_argument("task_id")
    develop = subparsers.add_parser("develop", help="start a fresh developer for one READY task")
    develop.add_argument("task_id")
    retry = subparsers.add_parser("retry", help="start a fresh developer after changes requested")
    retry.add_argument("task_id")
    review = subparsers.add_parser("review", help="start a fresh reviewer for the candidate commit")
    review.add_argument("task_id")
    triage = subparsers.add_parser("triage", help="classify an exceptional reported task issue")
    triage.add_argument("task_id")
    plan = subparsers.add_parser("plan", help="resolve a triaged contract or specification issue")
    plan.add_argument("task_id")
    plan.add_argument("--owner-decision")
    plan_review = subparsers.add_parser(
        "review-plan", help="independently review a planning correction"
    )
    plan_review.add_argument("task_id")
    changed = subparsers.add_parser("check-paths", help="reject changes to protected paths")
    changed.add_argument("base_commit")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        manager = WorkflowManager(
            args.repo,
            claude_command=args.claude_command or find_claude(),
            worktree_root=args.worktree_root,
        )
        if args.command == "validate":
            manager.validate_repository()
            output: object = {"status": "OK"}
        elif args.command == "status":
            output = manager.status()
        elif args.command == "ready":
            output = {"status": "READY", "commit": manager.ready(args.task_id)}
        elif args.command == "develop":
            output = manager.develop(args.task_id).to_dict()
        elif args.command == "retry":
            output = manager.develop(args.task_id, retry=True).to_dict()
        elif args.command == "review":
            state, report = manager.review(args.task_id)
            output = {"status": state, "report": str(report)}
        elif args.command == "triage":
            state, report = manager.triage(args.task_id)
            output = {"status": state, "report": str(report)}
        elif args.command == "plan":
            plan = manager.plan(args.task_id, owner_decision=args.owner_decision)
            output = (
                {"status": "AWAITING_PLAN_REVIEW", "plan": plan.to_dict()}
                if plan
                else {"status": manager.status()["workflow_state"]}
            )
        elif args.command == "review-plan":
            state, report = manager.review_plan(args.task_id)
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
