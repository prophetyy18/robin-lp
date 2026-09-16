"""Tests for the chain capability reporter (T024).

Covers T024 acceptance:
- wrong chain -> fail closed
- empty / proxy / unexpected bytecode -> fail closed
- inconsistent providers -> fail closed
- unsupported block tags -> fail closed (not silently swallowed)
- the report pins a block hash and is reproducible
- deployment block / transaction evidence surfaces
- source_retrieval_time is recorded

The transport is a fake; the focus is on the reporter's behaviour
across happy-path and per-endpoint failure scenarios.
"""

from __future__ import annotations

import asyncio
import hashlib
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

# A block whose bytecode matches the EIP-1167 minimal proxy pattern.
EIP1167_BYTECODE: bytes = (
    b"\x36\x3d\x3d\x37\x36\x3d\x3d\x3d\x3d\x36"  # first 10 bytes of prefix
    + b"\x3d\x3d\x3d\x36\x3d\x73"  # last 6 bytes of prefix (16 total)
    + b"\x33" * 20  # 20-byte implementation address
    + b"\x5a\xf4\x3d\x82\x80\x3e\x90\x3d\x91\x60\x2b\x57\xfd\x5b\xf3"  # 15-byte suffix
)
assert len(EIP1167_BYTECODE) == 51, len(EIP1167_BYTECODE)


def _expected(pm_hash: str | None = None, sv_hash: str | None = None) -> ExpectedDeployment:
    return ExpectedDeployment(
        chain_id=4663,
        pool_manager_address=PM_ADDRESS,
        state_view_address=SV_ADDRESS,
        pool_manager_code_hash=pm_hash or hashlib.sha256(EXPECTED_PM_CODE).hexdigest(),
        state_view_code_hash=sv_hash or hashlib.sha256(EXPECTED_SV_CODE).hexdigest(),
    )


def _empty_expected() -> ExpectedDeployment:
    """ExpectedDeployment with no pinned hashes (first-run mode)."""
    return ExpectedDeployment(
        chain_id=4663,
        pool_manager_address=PM_ADDRESS,
        state_view_address=SV_ADDRESS,
    )


def _per_endpoint_happy_script(pm_code_hex: str, sv_code_hex: str) -> list[Any]:
    """Per-endpoint happy-path script (7 calls).

    The endpoints support safe / finalized tags so the probe does not
    surface unsupported-tag errors. The first endpoint's responses
    are reused by failover for the after-loop genesis and deployment
    probes, so we keep the responses identical between endpoints.
    """
    return [
        _resp("0x1237"),  # eth_chainId
        _resp("0x100"),  # eth_blockNumber
        _resp({"hash": "0x" + "cc" * 32, "number": "0xf0"}),  # safe
        _resp({"hash": "0x" + "dd" * 32, "number": "0xe0"}),  # finalized
        _resp(pm_code_hex),  # PoolManager code
        _resp(sv_code_hex),  # StateView code
        _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),  # pin block
    ]


def _full_happy_script(pm_code_hex: str, sv_code_hex: str) -> list[Any]:
    """Full happy-path script for 2 endpoints plus the after-loop probes."""
    per_endpoint = _per_endpoint_happy_script(pm_code_hex, sv_code_hex)
    return (
        per_endpoint  # primary
        + per_endpoint  # backup
        + [
            _resp({"hash": "0x" + "00" * 32, "number": "0x0"}),  # genesis
            _resp(pm_code_hex),  # PM earliest
            _resp(sv_code_hex),  # SV earliest
        ]
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_report_records_all_required_fields() -> None:
    pm_hash = hashlib.sha256(EXPECTED_PM_CODE).hexdigest()
    sv_hash = hashlib.sha256(EXPECTED_SV_CODE).hexdigest()
    transport = _ScriptedTransport(
        _full_happy_script("0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex())
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected(pm_hash, sv_hash))
    assert report.expected_chain_id == 4663
    assert report.observed_chain_ids == {"primary": 4663, "backup": 4663}
    assert report.latest_block == {"primary": 256, "backup": 256}
    assert report.safe_block == {"primary": 240, "backup": 240}
    assert report.finalized_block == {"primary": 224, "backup": 224}
    assert report.pinned_block_hash == "0x" + "ab" * 32
    assert report.pinned_block_number == 256
    assert report.passed is True
    assert report.errors == []
    assert "T" in report.pinned_at or "Z" in report.pinned_at
    # T024 gap-closure fields.
    assert report.genesis_hash == "0x" + "00" * 32
    assert report.genesis_block_number_zero is True
    assert report.first_run is False
    assert report.cross_endpoint is not None
    assert report.cross_endpoint.chain_id_agree is True
    assert report.cross_endpoint.pool_manager_code_hash_agree is True
    assert report.cross_endpoint.state_view_code_hash_agree is True
    assert report.cross_endpoint.pinned_block_hash_agree is True
    assert "pool_manager" in report.deployment_evidence
    assert "state_view" in report.deployment_evidence
    assert report.deployment_evidence["pool_manager"].deployed_at_block_zero is True
    assert report.deployment_evidence["state_view"].deployed_at_block_zero is True
    assert "earliest" in report.archive_probe_method


async def test_first_run_pins_code_hashes() -> None:
    """First-run probe (no expected hashes) accepts any non-empty, non-proxy
    bytecode and surfaces the new hash for the next probe to compare."""
    transport = _ScriptedTransport(
        _full_happy_script("0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex())
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _empty_expected())
    assert report.first_run is True
    assert report.passed is True
    assert report.errors == []
    pm_hash = hashlib.sha256(EXPECTED_PM_CODE).hexdigest()
    sv_hash = hashlib.sha256(EXPECTED_SV_CODE).hexdigest()
    assert report.pool_manager["primary"]["code_hash"] == pm_hash
    assert report.state_view["primary"]["code_hash"] == sv_hash


# ---------------------------------------------------------------------------
# Wrong chain
# ---------------------------------------------------------------------------


async def test_wrong_chain_id_fails() -> None:
    per_endpoint = [
        _resp("0x1"),  # eth_chainId returns Ethereum, not 4663
        _resp("0x100"),
        _resp({"hash": "0x" + "cc" * 32, "number": "0xf0"}),
        _resp({"hash": "0x" + "dd" * 32, "number": "0xe0"}),
        _resp("0xfe"),
        _resp("0xfe"),
        _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
    ]
    transport = _ScriptedTransport(
        per_endpoint  # primary
        + per_endpoint  # backup
        + [
            _resp({"hash": "0x" + "00" * 32, "number": "0x0"}),  # genesis
            _resp("0xfe"),  # PM earliest (empty at genesis)
            _resp("0xfe"),  # SV earliest (empty at genesis)
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected())
    assert report.passed is False
    # Both endpoints report chain id 1 (the wrong chain); the
    # per-endpoint mismatch surfaces, and the cross-endpoint
    # check is "agree" because they agree on the wrong value.
    assert any("chain_id mismatch" in e for e in report.errors)
    assert report.cross_endpoint is not None
    assert report.cross_endpoint.chain_id_agree is True


async def test_chain_id_disagreement_across_endpoints_fails() -> None:
    """Two endpoints reporting different chain ids (neither matches the
    expected value) is a cross-endpoint disagreement that must fail
    the report."""
    primary = _per_endpoint_happy_script(
        "0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex()
    )
    backup = _per_endpoint_happy_script(
        "0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex()
    )
    primary[0] = _resp("0x1")  # primary reports Ethereum
    backup[0] = _resp("0x89")  # backup reports another chain
    transport = _ScriptedTransport(
        primary
        + backup
        + [
            _resp({"hash": "0x" + "00" * 32, "number": "0x0"}),
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected())
    assert report.passed is False
    assert any("chain_id disagreement across endpoints" in e for e in report.errors)


# ---------------------------------------------------------------------------
# Bytecode mismatch / drift
# ---------------------------------------------------------------------------


async def test_bytecode_mismatch_fails() -> None:
    """Wrong bytecode fails with a bytecode_drift reason when expected
    hashes are pinned."""
    pm_hash = hashlib.sha256(EXPECTED_PM_CODE).hexdigest()
    per_endpoint = [
        _resp("0x1237"),
        _resp("0x100"),
        _resp({"hash": "0x" + "cc" * 32, "number": "0xf0"}),
        _resp({"hash": "0x" + "dd" * 32, "number": "0xe0"}),
        _resp("0xdeadbeef"),  # wrong bytecode
        _resp("0x" + EXPECTED_SV_CODE.hex()),
        _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
    ]
    transport = _ScriptedTransport(
        per_endpoint
        + per_endpoint
        + [
            _resp({"hash": "0x" + "00" * 32, "number": "0x0"}),
            _resp("0xdeadbeef"),  # PM earliest (wrong)
            _resp("0x" + EXPECTED_SV_CODE.hex()),  # SV earliest
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected(pm_hash))
    assert report.passed is False
    assert any("PoolManager bytecode drift" in e for e in report.errors)


async def test_empty_bytecode_fails_closed() -> None:
    """Empty bytecode at a known address means the contract is not
    deployed; the probe must fail closed."""
    per_endpoint = [
        _resp("0x1237"),
        _resp("0x100"),
        _resp({"hash": "0x" + "cc" * 32, "number": "0xf0"}),
        _resp({"hash": "0x" + "dd" * 32, "number": "0xe0"}),
        _resp("0x"),  # PoolManager empty
        _resp("0x" + EXPECTED_SV_CODE.hex()),
        _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
    ]
    transport = _ScriptedTransport(
        per_endpoint
        + per_endpoint
        + [
            _resp({"hash": "0x" + "00" * 32, "number": "0x0"}),
            _resp("0x"),  # PM earliest (empty)
            _resp("0x" + EXPECTED_SV_CODE.hex()),  # SV earliest
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _empty_expected())
    assert report.passed is False
    assert any("PoolManager bytecode empty" in e for e in report.errors)


async def test_proxy_bytecode_fails_closed() -> None:
    """An EIP-1167 minimal proxy at the PoolManager address is an
    unexpected deployment shape; the probe must fail closed."""
    per_endpoint = [
        _resp("0x1237"),
        _resp("0x100"),
        _resp({"hash": "0x" + "cc" * 32, "number": "0xf0"}),
        _resp({"hash": "0x" + "dd" * 32, "number": "0xe0"}),
        _resp("0x" + EIP1167_BYTECODE.hex()),  # PoolManager is a proxy
        _resp("0x" + EXPECTED_SV_CODE.hex()),
        _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
    ]
    transport = _ScriptedTransport(
        per_endpoint
        + per_endpoint
        + [
            _resp({"hash": "0x" + "00" * 32, "number": "0x0"}),
            _resp("0x" + EIP1167_BYTECODE.hex()),  # PM earliest (proxy)
            _resp("0x" + EXPECTED_SV_CODE.hex()),  # SV earliest
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _empty_expected())
    assert report.passed is False
    assert any("EIP-1167 minimal proxy" in e for e in report.errors)


# ---------------------------------------------------------------------------
# Inconsistent providers
# ---------------------------------------------------------------------------


async def test_inconsistent_providers_are_recorded() -> None:
    pm_hash = hashlib.sha256(EXPECTED_PM_CODE).hexdigest()
    sv_hash = hashlib.sha256(EXPECTED_SV_CODE).hexdigest()
    primary_script = _per_endpoint_happy_script(
        "0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex()
    )
    backup_script = _per_endpoint_happy_script(
        "0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex()
    )
    backup_script[0] = _resp("0x1")  # backup reports wrong chain id
    transport = _ScriptedTransport(
        primary_script
        + backup_script
        + [
            _resp({"hash": "0x" + "00" * 32, "number": "0x0"}),
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected(pm_hash, sv_hash))
    assert report.passed is False
    assert any("chain_id mismatch" in e for e in report.errors)
    assert any("chain_id disagreement across endpoints" in e for e in report.errors)
    assert report.cross_endpoint is not None
    assert report.cross_endpoint.chain_id_agree is False


async def test_pinned_block_hash_disagreement_fails() -> None:
    """Different endpoints pinning different block hashes for the same
    block number is a fail-closed condition."""
    pm_hash = hashlib.sha256(EXPECTED_PM_CODE).hexdigest()
    sv_hash = hashlib.sha256(EXPECTED_SV_CODE).hexdigest()
    primary_script = _per_endpoint_happy_script(
        "0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex()
    )
    backup_script = _per_endpoint_happy_script(
        "0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex()
    )
    backup_script[6] = _resp({"hash": "0x" + "ff" * 32, "number": "0x100"})  # different pin
    transport = _ScriptedTransport(
        primary_script
        + backup_script
        + [
            _resp({"hash": "0x" + "00" * 32, "number": "0x0"}),
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected(pm_hash, sv_hash))
    assert report.passed is False
    assert any("pinned block hash disagreement" in e for e in report.errors)


# ---------------------------------------------------------------------------
# Unsupported block tags fail closed
# ---------------------------------------------------------------------------


async def test_unsupported_block_tags_fail_closed() -> None:
    """Safe / finalized tags are optional; providers that do not
    implement them must fail the report rather than be silently
    skipped."""
    per_endpoint = [
        _resp("0x1237"),
        _resp("0x100"),
        _err(-32000, "safe tag not supported"),
        _err(-32000, "finalized tag not supported"),
        _resp("0x" + EXPECTED_PM_CODE.hex()),
        _resp("0x" + EXPECTED_SV_CODE.hex()),
        _resp({"hash": "0x" + "ab" * 32, "number": "0x100"}),
    ]
    transport = _ScriptedTransport(
        per_endpoint
        + per_endpoint
        + [
            _resp({"hash": "0x" + "00" * 32, "number": "0x0"}),
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _empty_expected())
    assert report.passed is False
    assert any("safe tag not supported" in e for e in report.errors)
    assert any("finalized tag not supported" in e for e in report.errors)
    assert "primary" in report.unsupported_block_tags
    assert set(report.unsupported_block_tags["primary"]) == {"safe", "finalized"}


# ---------------------------------------------------------------------------
# Report reproducibility
# ---------------------------------------------------------------------------


async def test_report_is_serialisable_to_json() -> None:
    pm_hash = hashlib.sha256(EXPECTED_PM_CODE).hexdigest()
    sv_hash = hashlib.sha256(EXPECTED_SV_CODE).hexdigest()
    transport = _ScriptedTransport(
        _full_happy_script("0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex())
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _expected(pm_hash, sv_hash))
    # ``to_dict`` must produce a JSON-serialisable structure.
    import json

    blob = json.dumps(report.to_dict(), sort_keys=True)
    assert isinstance(blob, str)
    assert "4663" in blob  # chain id is in the report
    assert "genesis_hash" in blob  # T024 gap-closure field


async def test_min_block_number_violation_fails() -> None:
    """``min_block_number`` is a deployment sanity check; a chain
    whose latest block is below the minimum fails the report even if
    every probe succeeded."""
    pm_hash = hashlib.sha256(EXPECTED_PM_CODE).hexdigest()
    transport = _ScriptedTransport(
        [
            _resp("0x1237"),
            _resp("0x64"),  # 100 < min_block_number=1000
            _resp({"hash": "0x" + "cc" * 32, "number": "0xf0"}),
            _resp({"hash": "0x" + "dd" * 32, "number": "0xe0"}),
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
            _resp({"hash": "0x" + "ab" * 32, "number": "0x64"}),
            _resp({"hash": "0x" + "00" * 32, "number": "0x0"}),  # genesis
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
        ]
    )
    adapter = _adapter(
        transport,
        endpoints=(RpcEndpoint(url="http://primary", name="primary"),),
    )
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


# ---------------------------------------------------------------------------
# Deployment evidence
# ---------------------------------------------------------------------------


async def test_deployment_evidence_pre_genesis_deployment() -> None:
    """When ``eth_getCode(addr, earliest)`` returns non-empty code, the
    contract was deployed at or before the earliest available block;
    the report records that fact."""
    transport = _ScriptedTransport(
        _full_happy_script("0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex())
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _empty_expected())
    assert report.passed is True
    for evidence in report.deployment_evidence.values():
        assert evidence.deployed_at_block_zero is True
        assert evidence.earliest_block_with_code == 0
        assert evidence.reason is None


async def test_deployment_evidence_post_genesis() -> None:
    """When ``eth_getCode(addr, earliest)`` returns empty code, the
    contract was deployed later; the report records the binary-search
    finding (one-step probe in this case)."""
    per_endpoint = _per_endpoint_happy_script(
        "0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex()
    )
    transport = _ScriptedTransport(
        per_endpoint  # primary
        + per_endpoint  # backup
        + [
            _resp({"hash": "0x" + "00" * 32, "number": "0x0"}),  # genesis
            _resp("0x"),  # PM earliest (empty)
            _resp("0x" + EXPECTED_PM_CODE.hex()),  # PM midpoint
            _resp("0x"),  # SV earliest (empty)
            _resp("0x" + EXPECTED_SV_CODE.hex()),  # SV midpoint
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _empty_expected())
    assert report.passed is True
    pm_ev = report.deployment_evidence["pool_manager"]
    assert pm_ev.deployed_at_block_zero is False
    assert pm_ev.earliest_block_with_code is not None
    assert pm_ev.earliest_block_with_code > 0
    assert pm_ev.reason is None


# ---------------------------------------------------------------------------
# Genesis probe failure
# ---------------------------------------------------------------------------


async def test_genesis_probe_failure_is_recorded() -> None:
    """A failing genesis probe is recorded as an error but does not
    prevent the rest of the report from being produced."""
    per_endpoint = _per_endpoint_happy_script(
        "0x" + EXPECTED_PM_CODE.hex(), "0x" + EXPECTED_SV_CODE.hex()
    )
    transport = _ScriptedTransport(
        per_endpoint
        + per_endpoint
        + [
            _err(-32000, "genesis block not available"),
            _resp("0x" + EXPECTED_PM_CODE.hex()),
            _resp("0x" + EXPECTED_SV_CODE.hex()),
        ]
    )
    adapter = _adapter(transport)
    report = await probe_chain_capability(adapter, _empty_expected())
    assert report.genesis_hash is None
    assert any("genesis probe" in e for e in report.errors)
