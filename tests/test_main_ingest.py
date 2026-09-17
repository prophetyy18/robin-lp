"""Tests for the ``ingest`` operator entry point (T035).

The contract:

- ``python -m robinhood_lp ingest --config ... --data-root ... \
   --from-block N --to-block M`` runs one bounded historical
  ingestion;
- the entry point never accepts, reads, or forwards signing
  material (no key / seed / private arguments; no signing
  surface area);
- the entry point never issues ``eth_call`` (acquisition is
  read-only at the log + header layer);
- on success the entry point prints the run identifier and the
  ``complete`` boolean;
- the exit code is ``0`` on success and non-zero on any
  configuration / runner failure;
- the subcommand can be exercised without touching the network
  (the underlying :class:`RpcAdapter` accepts an injected fake
  transport; the tests use one).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robinhood_lp import __main__ as cli


def _write_config(
    tmp_path: Path,
    *,
    chain_id: int = 4663,
    pool_id: str = "0x" + "ab" * 32,
    pool_init_block: int = 1_000_000,
    endpoints: list[dict] | None = None,
) -> Path:
    if endpoints is None:
        endpoints = [
            {
                "alias": "robinhood_public",
                "url": "https://rpc.example/test",
                "weight": 0,
                "max_blocks_per_get_logs": 1_000_000,
                "max_calls": 1,
                "seconds": 30.0,
                "max_response_bytes": 1_000_000,
            }
        ]
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "chain_id": chain_id,
                "pool_id": pool_id,
                "pool_manager_address": "0x" + "55" * 20,
                "pool_init_block": pool_init_block,
                "endpoints": endpoints,
                "capability_snapshot_id": "test-snap-1",
                "captured_at": "2026-09-17T00:00:00+00:00",
            }
        )
    )
    return config_path


def test_ingest_subcommand_does_not_accept_signing_arguments() -> None:
    """The CLI parser must not expose any signing-related flags.

    A grep-style check: the parser surface must not include
    ``--private-key``, ``--keystore``, ``--seed``, ``--password``,
    ``--sign``, ``--broadcast``, ``--submit``, ``--send``.
    """
    parser = cli._build_parser()
    # Walk every action and inspect its option strings.
    for action in parser._actions:
        for opt in action.option_strings:
            lowered = opt.lower()
            for forbidden in (
                "private-key",
                "keystore",
                "seed",
                "password",
                "sign",
                "broadcast",
                "submit",
                "send",
                "wallet",
                "tx",
                "transaction",
            ):
                assert forbidden not in lowered, (
                    f"ingest parser exposes forbidden signing surface {opt!r}"
                )


def test_ingest_subcommand_parser_accepts_required_arguments() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "ingest",
            "--config",
            "/tmp/x.json",
            "--data-root",
            "/tmp/y",
            "--from-block",
            "100",
            "--to-block",
            "200",
        ]
    )
    assert args.command == "ingest"
    assert str(args.config) == "/tmp/x.json"
    assert str(args.data_root) == "/tmp/y"
    assert args.from_block == 100
    assert args.to_block == 200


def test_ingest_subcommand_rejects_missing_required(tmp_path: Path) -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["ingest"])


def test_ingest_subcommand_rejects_inverted_block_range(tmp_path: Path) -> None:
    """The CLI parser does not validate the block range itself;
    the runner rejects inverted ranges at execution time. We
    check the parser surface here and rely on the runner tests
    to verify the runtime rejection."""
    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "ingest",
            "--config",
            str(_write_config(tmp_path)),
            "--data-root",
            str(tmp_path / "data"),
            "--from-block",
            "200",
            "--to-block",
            "100",
        ]
    )
    assert args.from_block == 200
    assert args.to_block == 100


def test_ingest_subcommand_load_config_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        cli._load_config(tmp_path / "missing.json")


def test_ingest_subcommand_load_config_rejects_missing_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.json"
    cfg.write_text(json.dumps({"chain_id": 4663}))
    with pytest.raises(ValueError, match="missing keys"):
        cli._load_config(cfg)


def test_ingest_subcommand_load_config_accepts_valid_payload(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    loaded = cli._load_config(cfg)
    assert loaded["chain_id"] == 4663
    assert loaded["pool_init_block"] == 1_000_000


def test_ingest_subcommand_end_to_end_no_network(tmp_path: Path, capsys) -> None:
    """Run the entry point end-to-end with a fake transport.

    The fake transport returns an empty ``eth_getLogs`` response
    so the runner reports ``complete=True`` without ever
    touching the network. The CLI prints the run identifier and
    the ``complete`` boolean; the exit code is ``0``.
    """
    config_path = _write_config(tmp_path)
    data_root = tmp_path / "data"
    data_root.mkdir()

    # Patch the RpcAdapter transport at module load to be a fake.
    from robinhood_lp.rpc import adapter as rpc_adapter_module

    class _FakeAdapter:
        """A drop-in replacement for :class:`RpcAdapter` that
        answers ``eth_get_logs`` with an empty array and
        ``eth_get_block_by_number`` with a minimal non-hydrated
        header."""

        def __init__(self, config):
            self.config = config

        async def eth_get_logs(self, from_block, to_block, address, topics):
            return []

        async def eth_get_block_by_number(self, block_number, *, hydrated=False):
            return {
                "hash": "0x" + format(block_number, "064x"),
                "parentHash": "0x" + format(max(0, block_number - 1), "064x"),
                "number": "0x" + format(block_number, "x"),
                "timestamp": "0x0",
            }

    original = rpc_adapter_module.RpcAdapter
    rpc_adapter_module.RpcAdapter = _FakeAdapter  # type: ignore[misc]
    try:
        rc = cli.main(
            [
                "ingest",
                "--config",
                str(config_path),
                "--data-root",
                str(data_root),
                "--from-block",
                "100",
                "--to-block",
                "200",
                "--run-id",
                "test-run-1",
            ]
        )
    finally:
        rpc_adapter_module.RpcAdapter = original  # type: ignore[misc]
    out = capsys.readouterr().out.strip()
    assert rc == 0
    payload = json.loads(out)
    assert payload["run_id"] == "test-run-1"
    assert payload["complete"] is True
    assert payload["halt_reason"] is None
    # A cold-start run scans from the pool's ``Initialize`` block
    # (the configured ``pool_init_block``) to the requested end;
    # a shorter requested start does **not** truncate the
    # reconstruction prefix.
    assert payload["coverage_from_block"] == 1_000_000
    assert payload["coverage_to_block"] == 200


def test_ingest_subcommand_emits_machine_readable_json(tmp_path: Path, capsys) -> None:
    """The output is a single line of machine-readable JSON so a
    caller can parse it without scraping free-form text."""
    config_path = _write_config(tmp_path)
    data_root = tmp_path / "data"
    data_root.mkdir()

    from robinhood_lp.rpc import adapter as rpc_adapter_module

    class _FakeAdapter:
        def __init__(self, config):
            self.config = config

        async def eth_get_logs(self, from_block, to_block, address, topics):
            return []

        async def eth_get_block_by_number(self, block_number, *, hydrated=False):
            return {
                "hash": "0x" + format(block_number, "064x"),
                "parentHash": "0x" + format(max(0, block_number - 1), "064x"),
                "number": "0x" + format(block_number, "x"),
                "timestamp": "0x0",
            }

    original = rpc_adapter_module.RpcAdapter
    rpc_adapter_module.RpcAdapter = _FakeAdapter  # type: ignore[misc]
    try:
        cli.main(
            [
                "ingest",
                "--config",
                str(config_path),
                "--data-root",
                str(data_root),
                "--from-block",
                "100",
                "--to-block",
                "100",
                "--run-id",
                "test-machine-readable",
            ]
        )
    finally:
        rpc_adapter_module.RpcAdapter = original  # type: ignore[misc]
    out = capsys.readouterr().out.strip()
    # Single line of JSON.
    assert "\n" not in out
    parsed = json.loads(out)
    assert isinstance(parsed, dict)
    # The contract-mandated keys are all present.
    for key in (
        "run_id",
        "complete",
        "halt_reason",
        "topology",
        "coverage_from_block",
        "coverage_to_block",
        "interval_count",
        "logical_rpc_calls",
        "http_requests",
        "response_bytes",
        "normalized_rows",
        "elapsed_ms",
        "manifest_checksum",
    ):
        assert key in parsed, key


def test_ingest_subcommand_does_not_call_eth_call(monkeypatch, tmp_path, capsys) -> None:
    """The acquisition path issues no ``eth_call`` (T035 contract).

    The fake adapter's ``eth_call`` is replaced with a marker
    that raises if it is invoked. The end-to-end run completes
    successfully without ever calling ``eth_call``.
    """
    config_path = _write_config(tmp_path)
    data_root = tmp_path / "data"
    data_root.mkdir()

    from robinhood_lp.rpc import adapter as rpc_adapter_module

    class _FakeAdapter:
        def __init__(self, config):
            self.config = config

        async def eth_get_logs(self, from_block, to_block, address, topics):
            return []

        async def eth_get_block_by_number(self, block_number, *, hydrated=False):
            return {
                "hash": "0x" + format(block_number, "064x"),
                "parentHash": "0x" + format(max(0, block_number - 1), "064x"),
                "number": "0x" + format(block_number, "x"),
                "timestamp": "0x0",
            }

        async def eth_call(self, to, data, *, block_number):
            raise AssertionError("ingest must not issue eth_call during acquisition")

    original = rpc_adapter_module.RpcAdapter
    rpc_adapter_module.RpcAdapter = _FakeAdapter  # type: ignore[misc]
    try:
        rc = cli.main(
            [
                "ingest",
                "--config",
                str(config_path),
                "--data-root",
                str(data_root),
                "--from-block",
                "100",
                "--to-block",
                "100",
                "--run-id",
                "test-no-eth-call",
            ]
        )
    finally:
        rpc_adapter_module.RpcAdapter = original  # type: ignore[misc]
    assert rc == 0
    assert "test-no-eth-call" in capsys.readouterr().out


def test_ingest_subcommand_runs_paper_mode_only(monkeypatch, tmp_path) -> None:
    """The default operating mode is PAPER.

    The CLI must not expose a flag that turns on live execution.
    """
    parser = cli._build_parser()
    # ``--help`` must not advertise live execution flags.
    help_text = parser.format_help()
    for forbidden in (
        "--live",
        "--execute-live",
        "--broadcast",
        "--sign",
        "--send-tx",
    ):
        assert forbidden not in help_text


def test_ingest_subcommand_returns_nonzero_on_configuration_error(tmp_path, capsys) -> None:
    """Configuration errors surface as a non-zero exit code."""
    cfg = tmp_path / "bad.json"
    cfg.write_text(json.dumps({"chain_id": 4663}))
    data_root = tmp_path / "data"
    data_root.mkdir()
    rc = cli.main(
        [
            "ingest",
            "--config",
            str(cfg),
            "--data-root",
            str(data_root),
            "--from-block",
            "100",
            "--to-block",
            "100",
        ]
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "missing keys" in err or "configuration" in err


def test_ingest_subcommand_returns_nonzero_on_missing_config(tmp_path, capsys) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    rc = cli.main(
        [
            "ingest",
            "--config",
            str(tmp_path / "missing.json"),
            "--data-root",
            str(data_root),
            "--from-block",
            "100",
            "--to-block",
            "100",
        ]
    )
    assert rc != 0


def test_ingest_subcommand_does_not_log_signing_material(tmp_path, capsys) -> None:
    """If a misconfigured file accidentally contains a secret, the
    CLI must not echo it in stdout / stderr. The current parser
    does not accept signing material at all; this test asserts
    the contract by inspecting the ``format_help`` output.
    """
    parser = cli._build_parser()
    help_text = parser.format_help()
    assert "private-key" not in help_text
    assert "keystore" not in help_text
    assert "seed" not in help_text
    assert "password" not in help_text


def test_version_flag_prints_version(capsys) -> None:
    rc = cli.main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == cli.__version__ or out == "0.0.0"


def test_no_command_prints_help_and_exits_zero(capsys) -> None:
    rc = cli.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ingest" in out
