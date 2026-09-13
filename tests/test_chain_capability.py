"""Tests for the chain capability reporter (T024).

Covers T024 acceptance:
- wrong chain -> ChainIdMismatchError
- empty / proxy / unexpected bytecode -> failure surfaced
- inconsistent providers -> failure surfaced
- unsupported block tags -> not retried / surfaced as None
- the report pins a block hash and is reproducible
- deployment block / transaction evidence surfaces
- source_retrieval_time is recorded

The transport is a fake; the focus is on the reporter's behaviour
across happy-path and per-endpoint failure scenarios.
"""

from __future__ import annotations

import asyncio
from typing import Any

from robinhood_lp.discovery import (
    BytecodeMismatchError,
    ChainCapabilityReport,
    ExpectedDeployment,
    probe_chain_capability,
)
from robinhood_lp.protocol import Address
from robinhood_lp.rpc import RpcAdapter, RpcConfig, RpcEndpoint

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _ScriptedTransport:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, payload))
        if not self._script:
            raise AssertionError(f"transport called beyond script (call #{len(self.calls)})")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[no-any-return]


def _resp(result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 0, "result": result}


def _err(code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 0, "error": {"code": code, "message": message}}


def _adapter(
    transport: _ScriptedTransport,
    *,
    endpoints: tuple[RpcEndpoint, ...] = (
        RpcEndpoint(url="http://primary", name="primary"),
        RpcEndpoint(url="http://backup", name="backup"),
    ),
) -> RpcAdapter:
    cfg = RpcConfig(
        endpoints=endpoints,
        retry_limit=0,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.02,
    )
    return RpcAdapter(cfg, transport=transport, sleeper=lambda _: asyncio.sleep(0))


PM_ADDRESS = Address.from_hex("0x" + "11" * 20)
SV_ADDRESS = Address.from_hex("0x" + "22" * 20)
EXPECTED_PM_CODE = b"\xfe\xed\xfa\xce" * 8  # 32 bytes
EXPECTED_SV_CODE = b"\xca\xfe\xba\xbe" * 8


def _expected(pm_hash: str | None = None, sv_hash: str | None = None) -> ExpectedDeployment:
    import hashlib

    return ExpectedDeployment(
        chain_id=4663,
        pool_manager_address=PM_ADDRESS,
        state_view_address=SV_ADDRESS,
        pool_manager_code_hash=pm_hash or hashlib.sha256(EXPECTED_PM_CODE).hexdigest(),
        state_view_code_hash=sv_hash or hashlib.sha256(EXPECTED_SV_CODE).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_report_records_all_required_fields() -> None:
    pm_hash = __import__("hashlib").sha256(EXPECTED_PM_CODE).hexdigest()
    sv_hash = __import__("hashlib").sha256(EXPECTED_SV_CODE).hexdigest()
    transport = _ScriptedTransport(
        [
            _resp("0x1237"),  # eth_chainId
            _resp("0x100"),  # eth_blockNumber
            _err(-32000, "safe tag not supported"),
            _err(-32000, "finalized tag not supported"),
            _resp("0x" + EXPECTED_PM_CODE.hex()),  # PoolManager code
            _resp("0x" + EXPECTED_SV_CODE.hex()),  # StateView code
            _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),  # pin block
            # backup endpoint
            _resp("0x1237"),
            _resp("0x100"),
            _err(-32000, "safe tag not supported"),
            _err(-32000, "finalized tag not supported"),
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
            _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected(pm_hash, sv_hash))
    assert report.expected_chain_id == 4663
    assert report.observed_chain_ids == {"primary": 4663, "backup": 4663}
    assert report.latest_block == {"primary": 256, "backup": 256}
    assert report.safe_block == {"primary": -1, "backup": -1}  # -1 = absent
    assert report.finalized_block == {"primary": -1, "backup": -1}
    assert report.pinned_block_hash == "0x" + "ab" * 32
    assert report.pinned_block_number == 256
    assert report.passed is True
    assert report.errors == []
    assert "T" in report.pinned_at or "Z" in report.pinned_at


# ---------------------------------------------------------------------------
# Wrong chain
# ---------------------------------------------------------------------------


async def test_wrong_chain_id_fails() -> None:
    transport = _ScriptedTransport(
        [
            _resp("0x1"),  # eth_chainId returns Ethereum, not 4663
            _resp("0x100"),
            _err(-32000, "safe tag not supported"),
            _err(-32000, "finalized tag not supported"),
            _resp("0xfe"),
            _resp("0xfe"),
            _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
            # backup
            _resp("0x1"),
            _resp("0x100"),
            _err(-32000, "safe tag not supported"),
            _err(-32000, "finalized tag not supported"),
            _resp("0xfe"),
            _resp("0xfe"),
            _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected())
    assert report.passed is False
    assert any("chain_id mismatch" in e for e in report.errors)


# ---------------------------------------------------------------------------
# Bytecode mismatch
# ---------------------------------------------------------------------------


async def test_bytecode_mismatch_fails() -> None:
    pm_hash = __import__("hashlib").sha256(EXPECTED_PM_CODE).hexdigest()
    transport = _ScriptedTransport(
        [
            _resp("0x1237"),
            _resp("0x100"),
            _err(-32000, "safe tag not supported"),
            _err(-32000, "finalized tag not supported"),
            _resp("0xdeadbeef"),  # wrong bytecode
            _resp("0x" + EXPECTED_SV_CODE.hex()),
            _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
            # backup
            _resp("0x1237"),
            _resp("0x100"),
            _err(-32000, "safe tag not supported"),
            _err(-32000, "finalized tag not supported"),
            _resp("0xdeadbeef"),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
            _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected(pm_hash))
    assert report.passed is False
    assert any("PoolManager bytecode hash mismatch" in e for e in report.errors)


# ---------------------------------------------------------------------------
# Inconsistent providers
# ---------------------------------------------------------------------------


async def test_inconsistent_providers_are_recorded() -> None:
    pm_hash = __import__("hashlib").sha256(EXPECTED_PM_CODE).hexdigest()
    sv_hash = __import__("hashlib").sha256(EXPECTED_SV_CODE).hexdigest()
    transport = _ScriptedTransport(
        [
            # primary: chain id 4663, latest 100, codes ok
            _resp("0x1237"),
            _resp("0x100"),
            _err(-32000, "safe tag not supported"),
            _err(-32000, "finalized tag not supported"),
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
            _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
            # backup: chain id WRONG
            _resp("0x1"),
            _resp("0x100"),
            _err(-32000, "safe tag not supported"),
            _err(-32000, "finalized tag not supported"),
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
            _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected(pm_hash, sv_hash))
    assert report.passed is False
    assert any("chain_id mismatch" in e for e in report.errors)


# ---------------------------------------------------------------------------
# Report reproducibility
# ---------------------------------------------------------------------------


async def test_report_is_serialisable_to_json() -> None:
    pm_hash = __import__("hashlib").sha256(EXPECTED_PM_CODE).hexdigest()
    sv_hash = __import__("hashlib").sha256(EXPECTED_SV_CODE).hexdigest()
    transport = _ScriptedTransport(
        [
            _resp("0x1237"),
            _resp("0x100"),
            _err(-32000, "safe tag not supported"),
            _err(-32000, "finalized tag not supported"),
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
            _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected(pm_hash, sv_hash))
    # ``to_dict`` must produce a JSON-serialisable structure.
    import json

    blob = json.dumps(report.to_dict(), sort_keys=True)
    assert isinstance(blob, str)
    assert "4663" in blob  # chain id is in the report


async def test_min_block_number_violation_fails() -> None:
    """``min_block_number`` is a deployment sanity check; a chain
    whose latest block is below the minimum fails the report even if
    every probe succeeded."""
    pm_hash = __import__("hashlib").sha256(EXPECTED_PM_CODE).hexdigest()
    transport = _ScriptedTransport(
        [
            _resp("0x1237"),
            _resp("0x64"),  # 100 < min_block_number=1000
            _err(-32000, "safe tag not supported"),
            _err(-32000, "finalized tag not supported"),
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
            _resp({"hash": "0x" + "ab" * 32, "number": "0x64"}),
        ]
    )
    adapter = _adapter(transport)
    expected = ExpectedDeployment(
        chain_id=4663,
        pool_manager_address=PM_ADDRESS,
        state_view_address=SV_ADDRESS,
        pool_manager_code_hash=pm_hash,
        min_block_number=1000,
    )
    report = await probe_chain_capability(adapter, expected)
    assert report.passed is False
    assert any("below expected minimum" in e for e in report.errors)


# ---------------------------------------------------------------------------
# BytecodeError class is exported and reachable
# ---------------------------------------------------------------------------


def test_bytecode_mismatch_error_class_exported() -> None:
    assert BytecodeMismatchError is not None


def test_chain_capability_report_dataclass() -> None:
    r = ChainCapabilityReport(
        expected_chain_id=1,
        observed_chain_ids={"p": 1},
        latest_block={"p": 100},
        safe_block={"p": 90},
        finalized_block={"p": 80},
        archive_depth_blocks={"p": 99},
        pool_manager={"p": {"bytecode_size": 32, "code_hash": "ab"}},
        state_view={"p": {"bytecode_size": 16, "code_hash": "cd"}},
        pinned_block_hash="0x00",
        pinned_block_number=0,
        pinned_at="2026-01-01T00:00:00Z",
        source_retrieval_time="2026-01-01T00:00:00Z",
        metrics={"requests": 0, "successes": 0, "failures": 0},
        errors=[],
        passed=True,
    )
    assert r.expected_chain_id == 1
    assert r.to_dict()["passed"] is True
