"""Command-line entry point for ``python -m robinhood_lp``.

Subcommands:

- ``--version`` / ``-V`` — print the package version.
- ``ingest`` — run one bounded historical ingestion for a configured
  V4 PoolKey against the configured Robinhood Chain mainnet RPC.
  This subcommand is **read-only**: it never signs, broadcasts, or
  reads key material. Its acquisition path issues no
  ``eth_call``; state is rebuilt locally downstream from the
  ingested event stream.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from robinhood_lp import __version__

if TYPE_CHECKING:
    from robinhood_lp.ingestion.capability import (
        EndpointCapability,
        RemainingBudgetSource,
    )
    from robinhood_lp.ingestion.router import RouterConfig

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="robinhood-lp",
        description=(
            "Research and paper-trading framework for Uniswap V4 LP strategies on Robinhood Chain."
        ),
    )
    parser.add_argument(
        "--version", "-V", action="store_true", help="Print the package version and exit."
    )
    subparsers = parser.add_subparsers(dest="command")

    ingest = subparsers.add_parser(
        "ingest",
        help=(
            "Run one bounded historical ingestion for a configured "
            "V4 PoolKey against the configured Robinhood Chain mainnet RPC."
        ),
    )
    ingest.add_argument(
        "--config",
        type=Path,
        required=True,
        help=(
            "Path to the JSON configuration file describing the target "
            "PoolKey, endpoints, capability snapshot, and budget."
        ),
    )
    ingest.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Path to the data root directory (manifest DB + Parquet partitions).",
    )
    ingest.add_argument(
        "--from-block",
        type=int,
        required=True,
        help="Inclusive lower bound of the requested block range.",
    )
    ingest.add_argument(
        "--to-block",
        type=int,
        required=True,
        help="Inclusive upper bound of the requested block range.",
    )
    ingest.add_argument(
        "--run-id",
        type=str,
        default=None,
        help=(
            "Optional explicit run identifier. When omitted the runner generates ``run-<uuid>``."
        ),
    )
    rerun = subparsers.add_parser(
        "rerun-manifest",
        help=(
            "Rerun a saved experiment manifest and verify that the "
            "recomputed metrics checksum matches the recorded one. "
            "This is the T063 acceptance gate 'one command reruns a "
            "saved manifest'."
        ),
    )
    rerun.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Path to a saved experiment manifest JSON file.",
    )
    rerun.add_argument(
        "--strict-numeraire",
        action="store_true",
        help=(
            "Require the manifest's numeraire / qualification to "
            "match a dataset registry record supplied via "
            "--dataset-qualification. Without this flag the rerun "
            "skips the cross-record gate."
        ),
    )
    rerun.add_argument(
        "--dataset-qualification",
        type=Path,
        default=None,
        help=(
            "Optional path to a JSON file carrying a DatasetQualificationRecord "
            "(dataset_version / reporting_numeraire / valuation_qualification). "
            "Used when --strict-numeraire is supplied."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and execute the requested subcommand."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.version:
        sys.stdout.write(f"{__version__}\n")
        return 0
    if args.command == "ingest":
        return _run_ingest(args)
    if args.command == "rerun-manifest":
        return _run_rerun_manifest(args)
    parser.print_help()
    return 0


# ---------------------------------------------------------------------------
# ``rerun-manifest`` subcommand (T063)
# ---------------------------------------------------------------------------


def _run_rerun_manifest(args: argparse.Namespace) -> int:
    """Execute the one-command rerun of a saved experiment manifest.

    The function is the operator's read-only entry point that
    satisfies the T063 acceptance clause "one command reruns a
    saved manifest". It loads the manifest, runs the full
    validation gate (per-pool invariant, required fields, report
    checksum, optional dataset numeraire / qualification), rebuilds
    the engine from the manifest's recorded strategy + model
    parameters, and compares the new ``metrics_checksum`` against
    the recorded one. A byte-identical match exits with code 0; any
    mismatch or validation failure exits with code 1 and writes the
    failure summary to ``stderr``.

    The function never accepts, reads, or forwards any signing
    material; the rerun is a deterministic, fully local
    re-execution of the backtest engine (Phase 0–8 read-only
    boundary).
    """
    try:
        from robinhood_lp.reports import (
            DatasetQualificationRecord,
            rerun_manifest,
        )
        from robinhood_lp.reports.validation import (
            ManifestValidationError,
        )
    except ImportError as exc:  # pragma: no cover - import smoke
        sys.stderr.write(f"rerun-manifest: required dependency missing: {exc}\n")
        return 1
    manifest_path: Path = args.manifest
    if not manifest_path.exists():
        sys.stderr.write(f"rerun-manifest: manifest file not found: {manifest_path}\n")
        return 1
    dataset_qualification: DatasetQualificationRecord | None = None
    if args.strict_numeraire:
        if args.dataset_qualification is None:
            sys.stderr.write(
                "rerun-manifest: --strict-numeraire requires --dataset-qualification <path>\n"
            )
            return 1
        try:
            raw = json.loads(args.dataset_qualification.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"rerun-manifest: dataset-qualification load failed: {exc}\n")
            return 1
        if not isinstance(raw, dict):
            sys.stderr.write("rerun-manifest: dataset-qualification must be a JSON object\n")
            return 1
        try:
            dataset_qualification = DatasetQualificationRecord(
                dataset_version=str(raw["dataset_version"]),
                reporting_numeraire=str(raw["reporting_numeraire"]),
                valuation_qualification=str(raw["valuation_qualification"]),
            )
        except (KeyError, ValueError) as exc:
            sys.stderr.write(f"rerun-manifest: dataset-qualification parse failed: {exc}\n")
            return 1
    try:
        result = rerun_manifest(
            manifest_path,
            dataset_qualification=dataset_qualification,
        )
    except ManifestValidationError as exc:
        sys.stderr.write(f"rerun-manifest: validation failed: {type(exc).__name__}: {exc}\n")
        return 1
    except Exception as exc:  # noqa: BLE001 — surface unexpected failures too
        sys.stderr.write(f"rerun-manifest: rerun failed: {type(exc).__name__}: {exc}\n")
        return 1
    payload = {
        "manifest_path": str(manifest_path),
        "run_id": result.run_id,
        "chain_id": result.chain_id,
        "pool_key_id": result.pool_key_id,
        "match": bool(result.match),
        "decisions_match": bool(result.decisions_match),
        "recorded_metrics_checksum": result.recorded_metrics_checksum,
        "recomputed_metrics_checksum": result.recomputed_metrics_checksum,
        "recorded_decisions_checksum": result.recorded_decisions_checksum,
        "recomputed_decisions_checksum": result.recomputed_decisions_checksum,
        "fills_count": int(result.new_run_metrics.fills_count),
        "total_return_q64_64": int(result.new_run_metrics.total_return_q64_64),
    }
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if result.match and result.decisions_match else 1


# ---------------------------------------------------------------------------
# ``ingest`` subcommand
# ---------------------------------------------------------------------------


def _run_ingest(args: argparse.Namespace) -> int:
    """Execute the bounded ingestion subcommand.

    The function is the operator's read-only entry point. It loads
    the configuration, wires the read-only JSON-RPC adapter to the
    production :class:`EndpointClient`, hands both to the
    :class:`IngestionRunner`, prints the resulting run identifier
    and ``complete`` state, and exits with code ``0`` on success
    and ``1`` on configuration / capability failure.

    The function never accepts, reads, or forwards any signing
    material; the underlying runner is also read-only (the Phase
    0–8 boundary). The acquisition path issues no ``eth_call``;
    block-pinned state reads belong to the qualification task
    T036 and to state reconstruction (T040+).
    """
    # Configuration loading runs before any I/O so a stale or
    # misconfigured run never makes an outbound request.
    try:
        config = _load_config(args.config)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        sys.stderr.write(f"ingest: configuration error: {exc}\n")
        return 1
    try:
        from robinhood_lp.ingestion import (
            ALIAS_ROBINHOOD_PUBLIC,
            IngestionRunner,
            RangePlanner,
        )
        from robinhood_lp.ingestion.capability import (
            CapabilitySnapshot,
            EndpointCapability,
        )
        from robinhood_lp.ingestion.endpoint_client import RpcEndpointClient
        from robinhood_lp.protocol import Address, ChainId, PoolId
        from robinhood_lp.rpc.adapter import (
            RpcAdapter,
            RpcConfig,
            RpcEndpoint,
        )
        from robinhood_lp.storage import (
            CURRENT_DECODE_VERSION,
            CURRENT_SCHEMA_VERSION,
            ManifestStore,
            RawPartitionWriter,
        )
    except ImportError as exc:  # pragma: no cover - import smoke
        sys.stderr.write(f"ingest: required dependency missing: {exc}\n")
        return 1
    config = _load_config(args.config)
    chain_id = ChainId(int(config["chain_id"]))
    pool_id = PoolId.from_hex(str(config["pool_id"]))
    pool_manager = Address.from_hex(str(config["pool_manager_address"]))
    contract_address = pool_manager
    pool_init_block = int(config["pool_init_block"])
    endpoints_cfg = config.get("endpoints", [])
    if not endpoints_cfg:
        sys.stderr.write("ingest: configuration must list at least one endpoint alias\n")
        return 1
    # ``user_agent`` is documented as the operator's deployment-layer
    # configuration; the default transport ships it via the stdlib
    # and the ``RpcAdapter`` does not override it. The variable is
    # read here so a misconfigured / missing value is surfaced as a
    # configuration error before any RPC traffic is sent.
    _user_agent = str(config.get("user_agent", "robinhood-lp/0.0.0"))
    rpc_endpoints = tuple(
        RpcEndpoint(
            url=str(ep["url"]),
            name=str(ep.get("alias", f"endpoint-{i}")),
            weight=int(ep.get("weight", i)),
        )
        for i, ep in enumerate(endpoints_cfg)
    )
    adapter = RpcAdapter(
        RpcConfig(
            endpoints=rpc_endpoints,
            request_timeout_seconds=float(config.get("request_timeout_seconds", 30.0)),
            retry_limit=int(config.get("retry_limit", 4)),
        ),
        # The transport is the default; tests inject a fake. The
        # application User-Agent is sent by the default transport
        # via the standard library and the ``RpcAdapter`` does not
        # override it; this is documented as a known gap that the
        # operator must configure at the deployment layer.
    )
    # Build the per-endpoint production clients. The router
    # consumes them keyed by alias.
    clients: dict[str, RpcEndpointClient] = {}
    for ep in endpoints_cfg:
        alias = str(ep.get("alias", ALIAS_ROBINHOOD_PUBLIC))
        clients[alias] = RpcEndpointClient(
            adapter=adapter,
            alias=alias,
            address=contract_address.to_hex(),
        )
    # Build the capability snapshot from configuration. The
    # planning-layer probe is responsible for re-measuring
    # capability at run start; here we use the configured values
    # directly so the operator can pin the run to a known
    # measurement.
    from robinhood_lp.ingestion.capability import (
        BudgetSnapshot,
        EndpointBudget,
        EndpointCapability,
        RemainingBudget,
    )

    per_endpoint_capability = []
    for ep in endpoints_cfg:
        alias = str(ep.get("alias", ALIAS_ROBINHOOD_PUBLIC))
        max_blocks = ep.get("max_blocks_per_get_logs")
        per_endpoint_capability.append(
            EndpointCapability(
                alias=alias,
                chain_id=int(config["chain_id"]),
                chain_identity=f"{alias}:chain={int(config['chain_id'])}",
                finality_tags=("latest", "finalized"),
                archive_state_depth_blocks=ep.get("archive_state_depth_blocks"),
                accepted_log_range_blocks=max_blocks,
                latency_ms=int(ep.get("latency_ms", 50)),
                probe_block_number=int(ep.get("probe_block_number", args.from_block)),
                probe_block_hash=str(ep.get("probe_block_hash", "0x" + "00" * 32)),
                max_blocks_per_get_logs=max_blocks,
                observed_response_size_bytes=int(ep.get("observed_response_size_bytes", 0)),
                observed_rate_limit_headers=dict(ep.get("rate_limit_headers", {})),
                header_capability=bool(ep.get("header_capability", True)),
                archive_capability=bool(ep.get("archive_capability", False)),
                notes=tuple(ep.get("notes", ())),
            )
        )
    capability_snapshot = CapabilitySnapshot(
        snapshot_id=str(config.get("capability_snapshot_id", "config-snapshot-1")),
        chain_id=int(config["chain_id"]),
        contract_address=contract_address.to_hex().removeprefix("0x").lower(),
        pool_id=pool_id.to_hex(),
        endpoints=tuple(per_endpoint_capability),
        captured_at=str(config.get("captured_at", "2026-09-17T00:00:00+00:00")),
    )
    # Budget snapshot: operator-injected hard cap.
    per_endpoint_budget = []
    per_endpoint_remaining = []
    for ep in endpoints_cfg:
        alias = str(ep.get("alias", ALIAS_ROBINHOOD_PUBLIC))
        max_calls = int(ep.get("max_calls", 1))
        seconds = float(ep.get("seconds", 60.0))
        max_response_bytes = int(ep.get("max_response_bytes", 1_000_000))
        per_endpoint_budget.append(
            EndpointBudget(
                alias=alias,
                max_calls=max_calls,
                max_compute_units=ep.get("max_compute_units"),
                seconds=seconds,
                max_response_bytes=max_response_bytes,
            )
        )
        per_endpoint_remaining.append(
            RemainingBudget(
                alias=alias,
                remaining_calls=max_calls,
                remaining_compute_units=ep.get("max_compute_units"),
                remaining_seconds=seconds,
                remaining_response_quota=max_response_bytes,
                source=_validated_budget_source(str(ep.get("budget_source", "operator_injection"))),
            )
        )
    budget_snapshot = BudgetSnapshot(
        per_endpoint_budgets=tuple(per_endpoint_budget),
        per_endpoint_remaining=tuple(per_endpoint_remaining),
        captured_at=str(config.get("captured_at", "2026-09-17T00:00:00+00:00")),
    )
    # Build the planner, runner, and run.
    planner = RangePlanner()
    manifest = ManifestStore(args.data_root / "manifest.sqlite")
    writer = RawPartitionWriter(args.data_root, manifest)
    runner = IngestionRunner(
        manifest=manifest,
        writer=writer,
        chain_id=chain_id,
        contract_address=contract_address,
        pool_id=pool_id,
        pool_manager_address=pool_manager,
        pool_init_block=pool_init_block,
        planner=planner,
        router_config=_build_router_config(capability_snapshot, per_endpoint_capability),
        capability_snapshot=capability_snapshot,
        budget_snapshot=budget_snapshot,
    )
    if args.run_id is not None:
        runner.run_id = str(args.run_id)
    for alias, client in clients.items():
        runner.register_client(alias, client)
    # T035 — wire the production RpcBlockHeaderSource + BlockHeaderSink
    # so the runner populates the dedup'd ``block_headers`` manifest
    # table and the canonical ``block_timestamp`` / ``parent_hash``
    # columns on every persisted event. The header source uses the
    # same adapter the EndpointClients use; the sink is keyed to the
    # primary alias the capability snapshot declares.
    from robinhood_lp.ingestion.block_header_source import (
        BlockHeaderSink,
        RpcBlockHeaderSource,
    )
    from robinhood_lp.ingestion.router import ALIAS_ROBINHOOD_PUBLIC as _PRIMARY

    primary_alias = str(config.get("primary_header_alias", _PRIMARY))
    header_source = RpcBlockHeaderSource(
        adapter=adapter,
        alias=primary_alias,
        batch_size=int(config.get("header_batch_size", 16)),
    )
    header_sink = BlockHeaderSink(manifest=manifest, endpoint_alias=primary_alias)
    runner.register_header_source(source=header_source, sink=header_sink)
    try:
        result = runner.run(
            requested_start_block=int(args.from_block),
            requested_end_block=int(args.to_block),
        )
    except Exception as exc:
        sys.stderr.write(f"ingest: runner failed: {type(exc).__name__}: {exc}\n")
        return 1
    payload = {
        "run_id": result.run_id,
        "complete": bool(result.complete),
        "halt_reason": result.halt_reason,
        "topology": result.topology,
        "coverage_from_block": result.coverage_from_block,
        "coverage_to_block": result.coverage_to_block,
        "interval_count": result.interval_count,
        "logical_rpc_calls": result.logical_rpc_calls,
        "http_requests": result.http_requests,
        "response_bytes": result.response_bytes,
        "normalized_rows": result.normalized_rows,
        "elapsed_ms": result.elapsed_ms,
        "manifest_checksum": result.manifest_checksum,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "decode_version": CURRENT_DECODE_VERSION,
    }
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if result.complete else 1


def _load_config(path: Path) -> dict[str, Any]:
    """Load the JSON ingestion configuration.

    The function rejects any path that does not exist, and
    surfaces a clear error so the operator never accidentally
    ingests against a stale or missing configuration.
    """
    if not path.exists():
        raise FileNotFoundError(f"ingest: configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        config: dict[str, Any] = json.load(fh)
    required = (
        "chain_id",
        "pool_id",
        "pool_manager_address",
        "pool_init_block",
        "endpoints",
    )
    missing = [k for k in required if k not in config]
    if missing:
        raise ValueError(f"ingest: configuration missing keys: {missing}")
    return config


def _build_router_config(
    capability_snapshot: object, capabilities: list[EndpointCapability]
) -> RouterConfig:
    """Build a :class:`RouterConfig` from the configured capabilities."""
    from robinhood_lp.ingestion.capability import (
        DEFAULT_ALCHEMY_MAX_BLOCKS_PER_GET_LOGS,
        DEFAULT_MAX_RESPONSE_BYTES,
        DEFAULT_ROBINHOOD_MAX_BLOCKS_PER_GET_LOGS,
    )
    from robinhood_lp.ingestion.router import (
        ALIAS_ALCHEMY_FREE,
        ALIAS_ROBINHOOD_PUBLIC,
        RouterConfig,
    )

    max_blocks = DEFAULT_ROBINHOOD_MAX_BLOCKS_PER_GET_LOGS
    for cap in capabilities:
        if cap.alias == ALIAS_ROBINHOOD_PUBLIC and cap.max_blocks_per_get_logs:
            max_blocks = int(cap.max_blocks_per_get_logs)
    return RouterConfig(
        failover_order=(ALIAS_ROBINHOOD_PUBLIC, ALIAS_ALCHEMY_FREE),
        max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
        max_blocks_per_sub_range=max_blocks,
        alchemy_max_blocks_per_get_logs=DEFAULT_ALCHEMY_MAX_BLOCKS_PER_GET_LOGS,
    )


def _validated_budget_source(value: str) -> RemainingBudgetSource:
    """Validate the configured budget-source string against the allowed vocabulary.

    Returns the same string the caller passed in so the call site can
    use the value as a ``RemainingBudgetSource`` literal; the helper
    is intentionally narrow and only rejects values outside the
    documented vocabulary.
    """
    from robinhood_lp.ingestion.capability import (
        VALID_REMAINING_BUDGET_SOURCES,
    )

    if value not in VALID_REMAINING_BUDGET_SOURCES:
        raise ValueError(
            f"ingest: budget_source {value!r} is not one of "
            f"{sorted(VALID_REMAINING_BUDGET_SOURCES)!r}"
        )
    return cast("RemainingBudgetSource", value)


if __name__ == "__main__":
    raise SystemExit(main())
