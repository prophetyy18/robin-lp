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
    from robinhood_lp.backtest.events import BacktestEvent
    from robinhood_lp.ingestion.capability import (
        EndpointCapability,
        RemainingBudgetSource,
    )
    from robinhood_lp.ingestion.router import RouterConfig
    from robinhood_lp.orchestrator import (
        DatasetCoverage,
        DatasetResolver,
        EventSource,
        RunRequest,
        UnknownDatasetVersionError,
    )

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
            "saved manifest' extended by the T105 registry-binding "
            "clause. Both current T105 artifacts and legacy T063 "
            "artifacts are accepted; legacy artifacts are surfaced "
            "under an explicit LEGACY_T063 marker."
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
    # T069 — product-level backtest run lifecycle. The subcommands
    # drive the BacktestOrchestrator through the durable
    # RunStateStore; no Web process is required to start, observe
    # or cancel a run.
    backtest = subparsers.add_parser(
        "backtest",
        help=(
            "Drive the T069 product-level backtest run lifecycle "
            "(start, list, observe, cancel). The lifecycle is "
            "durable across process restarts; every transition is "
            "persisted under --runs-root."
        ),
    )
    backtest_sub = backtest.add_subparsers(dest="backtest_command")
    backtest_start = backtest_sub.add_parser(
        "start",
        help=(
            "Reserve a run as QUEUED and execute it. The lifecycle "
            "writes one manifest + report under --runs-root when the "
            "run succeeds; failure or cancellation publishes no "
            "manifest."
        ),
    )
    backtest_start.add_argument(
        "--request",
        type=Path,
        required=True,
        help=(
            "Path to a JSON file carrying a RunRequest "
            "(see ``robinhood_lp.backtest.orchestrator.RunRequest``)."
        ),
    )
    backtest_start.add_argument(
        "--runs-root",
        type=Path,
        required=True,
        help=(
            "Directory under which the orchestrator persists "
            "run records, manifests and reports. Created if "
            "missing."
        ),
    )
    backtest_start.add_argument(
        "--dataset-registry",
        type=Path,
        default=None,
        help=(
            "Optional path to a JSON file carrying the dataset "
            "registry the orchestrator consults. When omitted the "
            "orchestrator uses an empty in-memory registry (a "
            "run request that names a missing dataset version is "
            "rejected with a structured reason code)."
        ),
    )
    backtest_list = backtest_sub.add_parser(
        "list",
        help="List every persisted run record under --runs-root.",
    )
    backtest_list.add_argument(
        "--runs-root",
        type=Path,
        required=True,
        help="Directory holding the orchestrator's run records.",
    )
    backtest_observe = backtest_sub.add_parser(
        "observe",
        help=(
            "Return the persisted record for one run. The record "
            "carries the run's state, progress, reason code and "
            "(on success) the manifest and report paths."
        ),
    )
    backtest_observe.add_argument(
        "--runs-root",
        type=Path,
        required=True,
        help="Directory holding the orchestrator's run records.",
    )
    backtest_observe.add_argument(
        "--run-id",
        type=str,
        required=True,
        help="The run identifier to observe.",
    )
    backtest_cancel = backtest_sub.add_parser(
        "cancel",
        help=(
            "Cancel a queued or running run. A terminal record "
            "is left untouched."
        ),
    )
    backtest_cancel.add_argument(
        "--runs-root",
        type=Path,
        required=True,
        help="Directory holding the orchestrator's run records.",
    )
    backtest_cancel.add_argument(
        "--run-id",
        type=str,
        required=True,
        help="The run identifier to cancel.",
    )
    backtest_cancel.add_argument(
        "--reason",
        type=str,
        default="T069_CANCELLED_BY_OPERATOR",
        help="Structured reason code recorded on the run record.",
    )
    backtest_resume = backtest_sub.add_parser(
        "resume",
        help=(
            "Resume every RUNNING record under --runs-root. A "
            "restart while a run is in flight neither duplicates "
            "the run nor loses its state."
        ),
    )
    backtest_resume.add_argument(
        "--runs-root",
        type=Path,
        required=True,
        help="Directory holding the orchestrator's run records.",
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
    if args.command == "backtest":
        return _run_backtest(args)
    parser.print_help()
    return 0


# ---------------------------------------------------------------------------
# ``rerun-manifest`` subcommand (T063)
# ---------------------------------------------------------------------------


def _run_rerun_manifest(args: argparse.Namespace) -> int:
    """Execute the one-command rerun of a saved experiment manifest.

    The function is the operator's read-only entry point that
    satisfies the T063 acceptance clause "one command reruns a
    saved manifest" extended by the T105 registry-binding clause
    "verify and deterministically reproduce an already saved
    manifest as an artifact operation".

    The function auto-detects the artifact's manifest version:

    - A current T105 manifest is loaded through the standard
      :func:`rerun_manifest` entry point; the registry-binding
      check runs as part of the validation gate.
    - A legacy T063 manifest is loaded through
      :func:`load_legacy_manifest_from_path` and rerun under its
      recorded legacy schema with an explicit ``LEGACY_T063``
      marker in the response payload. A legacy artifact cannot be
      re-promoted as current evidence by this command; the
      migration is a separate ``migrate-legacy-manifest`` operation.

    The function never accepts, reads, or forwards any signing
    material; the rerun is a deterministic, fully local
    re-execution of the backtest engine (Phase 0–8 read-only
    boundary).
    """
    try:
        from robinhood_lp.reports import (
            LEGACY_MANIFEST_VERSION,
            DatasetQualificationRecord,
            InvalidLegacyManifestError,
            LegacyManifestError,
            load_legacy_manifest_from_path,
            rerun_manifest,
            rerun_manifest_from_object,
        )
        from robinhood_lp.reports.registry_binding import (
            RegistryBindingError,
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
    # Auto-detect the artifact's schema version. The check is a
    # byte-level read of the ``version`` field; both readers refuse
    # to load a mismatched version, so the dispatch is unambiguous.
    is_legacy = _manifest_version_is_legacy(manifest_path)
    try:
        if is_legacy:
            legacy = load_legacy_manifest_from_path(
                manifest_path, dataset_qualification=dataset_qualification
            )
            result = rerun_manifest_from_object(
                legacy.legacy_manifest, manifest_path=str(manifest_path)
            )
            legacy_marker = legacy.legacy_marker
            source_checksum = legacy.source_checksum
        else:
            result = rerun_manifest(
                manifest_path, dataset_qualification=dataset_qualification
            )
            legacy_marker = None
            source_checksum = None
    except ManifestValidationError as exc:
        sys.stderr.write(f"rerun-manifest: validation failed: {type(exc).__name__}: {exc}\n")
        return 1
    except (RegistryBindingError, InvalidLegacyManifestError, LegacyManifestError) as exc:
        sys.stderr.write(f"rerun-manifest: load failed: {type(exc).__name__}: {exc}\n")
        return 1
    except Exception as exc:  # noqa: BLE001 — surface unexpected failures too
        sys.stderr.write(f"rerun-manifest: rerun failed: {type(exc).__name__}: {exc}\n")
        return 1
    payload = {
        "manifest_path": str(manifest_path),
        "manifest_version": (
            LEGACY_MANIFEST_VERSION if is_legacy else "t105.experiment_manifest.v1"
        ),
        "legacy_marker": legacy_marker,
        "source_checksum": source_checksum,
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


def _manifest_version_is_legacy(manifest_path: Path) -> bool:
    """Return ``True`` iff the saved manifest declares the legacy version.

    The check is a byte-level read of the ``version`` field so the
    dispatch does not depend on the current publication path's
    structural rules. A malformed or unreadable JSON file falls
    through to the current path, where the standard loader raises
    with a structural error.
    """
    from robinhood_lp.reports import LEGACY_MANIFEST_VERSION as _LEGACY

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    declared_version = raw.get("version")
    return declared_version == _LEGACY


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


# ---------------------------------------------------------------------------
# ``backtest`` subcommand (T069)
# ---------------------------------------------------------------------------


def _run_backtest(args: argparse.Namespace) -> int:
    """Drive the T069 product-level backtest run lifecycle.

    The subcommand supports four operations: ``start``, ``list``,
    ``observe`` and ``cancel``. Every operation reads or writes
    the durable ``RunStateStore`` under ``--runs-root``; no Web
    process is required to start, observe or cancel a run.

    The subcommand is intentionally narrow: it composes the
    :class:`BacktestOrchestrator` (T069) with a CLI-supplied
    dataset resolver and event source. The CLI does not wire a
    Web session; the lifecycle is owned by the on-disk store.
    """
    try:
        from robinhood_lp.orchestrator import (
            BacktestOrchestrator,
            BacktestRunError,
            InvalidRunRequestError,
            RunRecord,
            RunState,
            RunStateStore,
        )
    except ImportError as exc:  # pragma: no cover - import smoke
        sys.stderr.write(f"backtest: required dependency missing: {exc}\n")
        return 1
    runs_root: Path = args.runs_root
    store = RunStateStore(runs_root)
    if args.backtest_command == "list":
        records = store.list_runs()
        payload = {
            "runs_root": str(runs_root),
            "run_ids": list(records),
        }
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 0
    if args.backtest_command == "observe":
        try:
            record = store.read(args.run_id)
        except FileNotFoundError:
            sys.stderr.write(f"backtest observe: run_id={args.run_id!r} not found\n")
            return 1
        sys.stdout.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
        return 0
    if args.backtest_command == "cancel":
        try:
            record = store.read(args.run_id)
        except FileNotFoundError:
            sys.stderr.write(f"backtest cancel: run_id={args.run_id!r} not found\n")
            return 1
        if record.state in (
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
        ):
            sys.stdout.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
            return 0
        # Build a terminal record and write it. The orchestrator's
        # full execution path is not exercised here because the
        # CLI cancel does not hold the cancel token the in-flight
        # orchestrator is polling; the next ``resume`` invocation
        # will observe the terminal record.
        now = int(_now_unix_seconds())
        cancelled = RunRecord(
            version=record.version,
            run_id=record.run_id,
            state=RunState.CANCELLED,
            request=record.request,
            progress=record.progress,
            reason_code=str(args.reason),
            error_message=None,
            manifest_path=None,
            report_path=None,
            source_manifest_path=record.source_manifest_path,
            source_checksum=record.source_checksum,
            created_at_unix_seconds=record.created_at_unix_seconds,
            updated_at_unix_seconds=now,
            terminal_at_unix_seconds=now,
        )
        store.write(cancelled)
        sys.stdout.write(json.dumps(cancelled.to_dict(), sort_keys=True) + "\n")
        return 0
    if args.backtest_command == "resume":
        # The CLI ``resume`` is the restart-while-in-flight
        # surface. The orchestrator's resume_in_flight re-executes
        # every RUNNING record under the store; the CLI composes
        # the same default event source + dataset resolver the
        # ``start`` subcommand uses.
        resolver, event_source = _build_cli_resolver_and_source(args)
        orchestrator = BacktestOrchestrator(
            store=store,
            dataset_resolver=resolver,
            event_source=event_source,
        )
        resumed = orchestrator.resume_in_flight()
        payload = {
            "runs_root": str(runs_root),
            "resumed": [record.run_id for record in resumed],
        }
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 0
    if args.backtest_command != "start":
        sys.stderr.write(
            f"backtest: unknown sub-command {args.backtest_command!r}\n"
        )
        return 2
    # ``start``: load the request, build the resolver / event
    # source, run the lifecycle, print the terminal record.
    request_path: Path = args.request
    if not request_path.exists():
        sys.stderr.write(f"backtest start: request file not found: {request_path}\n")
        return 1
    try:
        request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"backtest start: request load failed: {exc}\n")
        return 1
    if not isinstance(request_payload, dict):
        sys.stderr.write("backtest start: request must be a JSON object\n")
        return 1
    try:
        request = _request_from_cli_payload(request_payload)
    except InvalidRunRequestError as exc:
        sys.stderr.write(f"backtest start: invalid request: {exc}\n")
        return 1
    resolver, event_source = _build_cli_resolver_and_source(args)
    orchestrator = BacktestOrchestrator(
        store=store,
        dataset_resolver=resolver,
        event_source=event_source,
    )
    try:
        record = orchestrator.submit(request)
    except BacktestRunError as exc:
        sys.stderr.write(f"backtest start: {type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    if record.state == RunState.SUCCEEDED:
        return 0
    return 1


def _now_unix_seconds() -> int:
    """Return the current wall-clock time as integer Unix seconds."""
    import time

    return int(time.time())


def _request_from_cli_payload(payload: dict[str, Any]) -> RunRequest:
    """Reconstruct a :class:`RunRequest` from a CLI JSON payload.

    The function is the bridge between the JSON file the CLI
    reads and the dataclass the orchestrator consumes. Unknown
    fields are ignored so the CLI payload can carry operator
    comments without failing the parse.
    """
    from robinhood_lp.orchestrator import RunRequest as _RunRequest

    def _coerce_strategy_parameters(
        raw: object,
    ) -> dict[str, int | bool | str]:
        if not isinstance(raw, dict):
            return {}
        out: dict[str, int | bool | str] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not k:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                continue
            out[k] = v
        return out

    def _coerce_dependency_revisions(raw: object) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not k:
                continue
            if not isinstance(v, str):
                continue
            out[k] = v
        return out

    source_manifest_path = payload.get("source_manifest_path")
    if source_manifest_path is not None and not isinstance(source_manifest_path, str):
        source_manifest_path = None
    return _RunRequest(
        run_id=str(payload.get("run_id") or ""),
        dataset_version=str(payload.get("dataset_version") or ""),
        chain_id=int(payload.get("chain_id") or 0),
        pool_key_id=str(payload.get("pool_key_id") or ""),
        block_range_start=int(payload.get("block_range_start") or 0),
        block_range_end=int(payload.get("block_range_end") or 0),
        interval_seconds=int(payload.get("interval_seconds") or 1),
        strategy_identity=str(payload.get("strategy_identity") or ""),
        strategy_parameters=_coerce_strategy_parameters(
            payload.get("strategy_parameters") or {}
        ),
        seed=int(payload.get("seed") or 0),
        clock_assumption=str(payload.get("clock_assumption") or "EVENT_TIME"),
        fill_assumption=str(
            payload.get("fill_assumption") or "DETERMINISTIC_FAILURE"
        ),
        cost_assumption=str(payload.get("cost_assumption") or "FLAT_GAS"),
        quote_assumption=str(payload.get("quote_assumption") or "STATIC_FEE"),
        latency_units=int(payload.get("latency_units") or 0),
        latency_ms_estimate=int(payload.get("latency_ms_estimate") or 0),
        reporting_numeraire=str(payload.get("reporting_numeraire") or "USDG"),
        valuation_qualification=str(
            payload.get("valuation_qualification") or "QUALIFIED"
        ),
        code_revision=str(payload.get("code_revision") or "UNKNOWN"),
        dependency_revisions=_coerce_dependency_revisions(
            payload.get("dependency_revisions") or {}
        ),
        created_at_unix_seconds=int(payload.get("created_at_unix_seconds") or _now_unix_seconds()),
        source_manifest_path=source_manifest_path,
    )


def _build_cli_resolver_and_source(
    args: argparse.Namespace,
) -> tuple[DatasetResolver, EventSource]:
    """Build a CLI-friendly (resolver, event_source) pair.

    The CLI uses a file-backed dataset registry when
    ``--dataset-registry`` is supplied and a synthetic empty
    event source otherwise. The synthetic source is the
    contract surface every test relies on: a CLI invocation
    with no registered dataset / no events fails closed with
    a structured reason code (no manifest published).
    """
    from robinhood_lp.orchestrator import (
        DatasetCoverage,
        DatasetResolver,
        EventSource,
    )

    class _CLIDatasetResolver(DatasetResolver):
        def __init__(self, registry_path: Path | None) -> None:
            self.registry_path = registry_path
            self._datasets: dict[tuple[str, int, str], DatasetCoverage] = {}
            if registry_path is not None and registry_path.exists():
                payload = json.loads(registry_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    for entry in payload.get("datasets", []):
                        if not isinstance(entry, dict):
                            continue
                        coverage = DatasetCoverage(
                            chain_id=int(entry["chain_id"]),
                            pool_key_id=str(entry["pool_key_id"]),
                            dataset_version=str(entry["dataset_version"]),
                            dataset_schema_version=int(entry["dataset_schema_version"]),
                            dataset_decode_version=int(entry["dataset_decode_version"]),
                            dataset_content_hash=str(entry["dataset_content_hash"]),
                            reporting_numeraire=str(entry["reporting_numeraire"]),
                            valuation_qualification=str(
                                entry["valuation_qualification"]
                            ),
                            covered_start=int(entry["covered_start"]),
                            covered_end=int(entry["covered_end"]),
                        )
                        self._datasets[
                            (
                                coverage.dataset_version,
                                coverage.chain_id,
                                coverage.pool_key_id,
                            )
                        ] = coverage

        def resolve(
            self,
            *,
            dataset_version: str,
            chain_id: int,
            pool_key_id: str,
        ) -> DatasetCoverage:
            key = (dataset_version, chain_id, pool_key_id)
            if key not in self._datasets:
                raise UnknownDatasetVersionError(dataset_version=dataset_version)
            return self._datasets[key]

    class _CLIEventSource(EventSource):
        def load_events(
            self,
            *,
            chain_id: int,
            pool_key_id: str,
            block_range_start: int,
            block_range_end: int,
            cancel_token: object,
        ) -> list[BacktestEvent]:
            # The CLI subcommand operates with an empty event
            # source: an operator who supplies no replay / event
            # adapter gets an immediate failure with the
            # structured ``T069_NO_EVENTS`` reason. The contract
            # the CLI binds is: a run whose event source yields
            # no events is recorded as FAILED with no manifest
            # published.
            return []

    resolver = _CLIDatasetResolver(args.dataset_registry)
    event_source = _CLIEventSource()
    return resolver, event_source


if __name__ == "__main__":
    raise SystemExit(main())
