"""T024 — Real-chain capability probe runner.

This script is invoked from the Developer worktree to populate the
chain capability report for the Robinhood Chain testnet (T024
deliverable). It honours the security constraints from
``AGENTS.md`` and ``docs/spec/architecture/adr/ADR-003-configuration-and-secrets.md``:

* The URL read from ``ROBINHOOD_CHAIN_RPC_URL`` is **never** printed,
  logged, copied, or persisted; the script references it by env var
  name only.
* All logs and report keys identify endpoints by ``RpcEndpoint.name``
  (``alchemy-testnet`` and ``robinhood-official-testnet``).
* The script exits non-zero when the report fails; the exit code is
  the only boolean the caller needs.

The default artifact paths match the ones the V1 acceptance
documentation references; the caller can override them via CLI flags.

Run from the worktree root with ``PYTHONPATH=src``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from robinhood_lp.discovery import (
    ChainCapabilityReport,
    ExpectedDeployment,
    probe_chain_capability,
)
from robinhood_lp.protocol import Address
from robinhood_lp.rpc import RpcAdapter, RpcConfig, RpcEndpoint

#: Name the env var holds. We only reference this by name; the value
#: is read but never printed or persisted.
RPC_URL_ENV_VAR = "ROBINHOOD_CHAIN_RPC_URL"

#: The two endpoints the probe must consult. Both names are redacted
#: to the canonical aliases from ``docs/spec/architecture/adr/ADR-005``
#: and the workflow spec; the URLs are never emitted.
ALCHEMY_ENDPOINT_NAME = "alchemy-testnet"
ROBINHOOD_OFFICIAL_ENDPOINT_NAME = "robinhood-official-testnet"
ROBINHOOD_OFFICIAL_URL = "https://rpc.testnet.chain.robinhood.com"

DEFAULT_CHAIN_ID = 46630
DEFAULT_POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
DEFAULT_STATE_VIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"

DEFAULT_TESTNET_ARTIFACT = Path("docs/implement/protocol-artifacts/robinhood-chain-testnet.json")
DEFAULT_REPORT_PATH = Path(
    "docs/implement/protocol-artifacts/chain-capability-report-robinhood-testnet.json"
)


def _build_endpoints() -> tuple[RpcEndpoint, ...]:
    """Construct the two-endpoint configuration for the probe.

    The Alchemy endpoint URL is read from the env var but only
    referenced by env var name elsewhere in this script.
    """
    alchemy_url = os.environ.get(RPC_URL_ENV_VAR, "")
    if not alchemy_url:
        raise SystemExit(f"env var {RPC_URL_ENV_VAR} is required; refusing to fabricate a URL")
    # Sanity-check the URL is what we expect (https, alchemist or
    # alchemy in the host) but do not log the value.
    if not alchemy_url.lower().startswith("http"):
        raise SystemExit(f"env var {RPC_URL_ENV_VAR} does not look like an http(s) URL")
    return (
        RpcEndpoint(url=alchemy_url, name=ALCHEMY_ENDPOINT_NAME, weight=0),
        RpcEndpoint(url=ROBINHOOD_OFFICIAL_URL, name=ROBINHOOD_OFFICIAL_ENDPOINT_NAME, weight=10),
    )


def _load_expected(artifact_path: Path) -> ExpectedDeployment:
    """Load the expected deployment from the testnet artifact.

    The artifact is the source of truth for the chain id and
    addresses. The bytecode hashes are loaded when present; the
    probe runs in first-run mode when both are null.
    """
    with artifact_path.open() as f:
        data = json.load(f)
    return ExpectedDeployment(
        chain_id=int(data["chain_id"]),
        pool_manager_address=Address.from_hex(data["pool_manager_address"]),
        state_view_address=Address.from_hex(data["state_view_address"]),
        pool_manager_code_hash=data.get("pool_manager_code_hash"),
        state_view_code_hash=data.get("state_view_code_hash"),
        min_block_number=data.get("min_block_number"),
    )


def _write_report(report: ChainCapabilityReport, path: Path) -> None:
    """Persist the report to disk.

    The report's ``to_dict`` is JSON-native; we strip nothing because
    the report never carries the endpoint URL (only its name).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(report.to_dict(), f, indent=2, sort_keys=True)
        f.write("\n")


def _print_summary(report: ChainCapabilityReport) -> None:
    """Print a short summary; never includes URLs or secrets."""
    print(f"chain_id expected: {report.expected_chain_id}")
    print(f"observed_chain_ids: {report.observed_chain_ids}")
    print(f"latest_block: {report.latest_block}")
    print(f"genesis_hash: {report.genesis_hash}")
    print(f"pinned_block_hash: {report.pinned_block_hash}")
    print(f"pinned_block_number: {report.pinned_block_number}")
    print(f"first_run: {report.first_run}")
    print(f"unsupported_block_tags: {report.unsupported_block_tags}")
    print(f"archive_probe_method: {report.archive_probe_method}")
    print(f"pool_manager: {report.pool_manager}")
    print(f"state_view: {report.state_view}")
    if report.cross_endpoint is not None:
        ce = report.cross_endpoint
        print(f"chain_id_agree: {ce.chain_id_agree}")
        print(f"pool_manager_code_hash_agree: {ce.pool_manager_code_hash_agree}")
        print(f"state_view_code_hash_agree: {ce.state_view_code_hash_agree}")
        print(f"latest_block_agree: {ce.latest_block_agree}")
        print(f"pinned_block_hash_agree: {ce.pinned_block_hash_agree}")
    for label, evidence in report.deployment_evidence.items():
        print(
            f"deployment[{label}]: earliest_block={evidence.earliest_block_with_code} "
            f"deployed_at_block_zero={evidence.deployed_at_block_zero} "
            f"method={evidence.archive_probe_method} reason={evidence.reason}"
        )
    print(f"errors: {report.errors}")
    print(f"passed: {report.passed}")


async def _run(
    *,
    artifact_path: Path,
    report_path: Path,
    timeout: float,
) -> int:
    expected = _load_expected(artifact_path)
    endpoints = _build_endpoints()
    config = RpcConfig(
        endpoints=endpoints,
        request_timeout_seconds=timeout,
        retry_limit=2,
        initial_backoff_seconds=0.25,
        max_backoff_seconds=2.0,
    )
    adapter = RpcAdapter(config)
    report = await probe_chain_capability(adapter, expected)
    _write_report(report, report_path)
    _print_summary(report)
    return 0 if report.passed else 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="T024 chain capability probe runner")
    parser.add_argument(
        "--artifact",
        type=Path,
        default=DEFAULT_TESTNET_ARTIFACT,
        help="Path to the testnet deployment artifact JSON.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="Path to write the capability report JSON.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-request timeout in seconds.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(
        _run(artifact_path=args.artifact, report_path=args.report, timeout=args.timeout)
    )


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
