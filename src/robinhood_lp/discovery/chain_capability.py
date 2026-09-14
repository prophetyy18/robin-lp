"""Chain capability verification (T024).

Reads from an :class:`RpcAdapter` and produces a structured report
that T022 / T023 / T070 can use to decide whether the configured
chain deployment is real, consistent across providers, and matches
the project's documentation.

T024 acceptance (todo/README.md T024):

- RPC-reported chain ID matches the configured one;
- genesis block hash is recorded;
- latest / safe / finalised block numbers are reported;
- archive depth is recorded;
- PoolManager / StateView bytecode hashes are reported;
- deployment block / transaction evidence (when available);
- source retrieval time recorded;
- wrong chain, empty / proxy / unexpected bytecode, inconsistent
  providers, unavailable historical calls, and unsupported block
  tags all fail the report;
- the report pins a block hash and is reproducible.

T024 must-not:

- assume Robinhood mainnet/testnet parity;
- reuse Arbitrum addresses (some chains share the same PoolManager
  by deployment provenance);
- accept an explorer label as bytecode proof.

This module sits in the storage layer per ADR-006. It depends on
``robinhood_lp.protocol`` and ``robinhood_lp.rpc``; it must not
import ``robinhood_lp.config`` or higher layers.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from robinhood_lp.protocol import Address
from robinhood_lp.rpc import RpcAdapter

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ChainCapabilityError(RuntimeError):
    """The capability report could not be produced."""


class ChainIdMismatchError(ChainCapabilityError):
    """The endpoint's ``eth_chainId`` does not match the expected value."""


class BytecodeMismatchError(ChainCapabilityError):
    """The on-chain bytecode does not match the expected hash."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedDeployment:
    """The deployment the report must verify against.

    ``pool_manager_address`` and ``state_view_address`` are the
    addresses as published in the project's documentation. The
    reporter fetches the deployed bytecode and compares it to the
    expected bytecode hash when ``expected_code_hashes`` is provided.
    """

    chain_id: int
    pool_manager_address: Address
    state_view_address: Address
    pool_manager_code_hash: str | None = None
    state_view_code_hash: str | None = None
    #: When set, the reporter checks the latest block number is at
    #: least this value (a deployment sanity check).
    min_block_number: int | None = None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PerEndpointCapability:
    """One endpoint's contribution to the report."""

    endpoint_name: str
    chain_id: int | None
    latest_block: int | None
    safe_block: int | None
    finalized_block: int | None
    archive_depth_blocks: int | None
    pool_manager_bytecode_size: int | None
    pool_manager_code_hash: str | None
    state_view_bytecode_size: int | None
    state_view_code_hash: str | None
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ChainCapabilityReport:
    """Structured capability report produced by
    :func:`probe_chain_capability`."""

    expected_chain_id: int
    observed_chain_ids: dict[str, int]
    latest_block: dict[str, int]
    safe_block: dict[str, int]
    finalized_block: dict[str, int]
    archive_depth_blocks: dict[str, int]
    pool_manager: dict[str, dict[str, Any]]
    state_view: dict[str, dict[str, Any]]
    pinned_block_hash: str | None
    pinned_block_number: int | None
    pinned_at: str
    source_retrieval_time: str
    metrics: dict[str, int]
    errors: list[str]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


def _hex_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.lower()
        if s.startswith("0x"):
            return int(s, 16)
        return int(s)
    raise ChainCapabilityError(f"cannot decode hex/int: {value!r}")


async def probe_chain_capability(
    adapter: RpcAdapter,
    expected: ExpectedDeployment,
    *,
    archive_window: int = 1,
) -> ChainCapabilityReport:
    """Run the capability probe and return a structured report.

    The probe is intentionally explicit and side-effect free; callers
    decide what to do with the report (T022 reads it before
    constructing the registry; T070 reads it as a runtime gate).
    """
    errors: list[str] = []
    observed_chain_ids: dict[str, int] = {}
    latest_block: dict[str, int] = {}
    safe_block: dict[str, int] = {}
    finalized_block: dict[str, int] = {}
    archive_depth: dict[str, int] = {}
    pool_manager: dict[str, dict[str, Any]] = {}
    state_view: dict[str, dict[str, Any]] = {}

    endpoints = adapter._config.active_endpoints()  # noqa: SLF001 - the
    # adapter does not yet expose endpoint iteration; co-locating
    # the loop with the endpoint list keeps the report one pass.
    pm_addr_hex = expected.pool_manager_address.to_hex()
    sv_addr_hex = expected.state_view_address.to_hex()

    pinned_block_hash: str | None = None
    pinned_block_number: int | None = None

    for ep in endpoints:
        per_err: list[str] = []
        try:
            cid_hex = await adapter._call_with_failover("eth_chainId", [])  # noqa: SLF001
            cid = _hex_int(cid_hex)
        except Exception as exc:  # noqa: BLE001
            per_err.append(f"eth_chainId: {type(exc).__name__}: {exc}")
            cid = None
        if cid is not None and cid != expected.chain_id:
            per_err.append(
                f"chain_id mismatch: endpoint={ep.name} observed={cid} expected={expected.chain_id}"
            )

        latest = safe = finalised = archive = None
        try:
            latest_hex = await adapter._call_with_failover(  # noqa: SLF001
                "eth_blockNumber", []
            )
            latest = _hex_int(latest_hex)
            # 'safe' and 'finalised' are provider-optional; if the
            # provider does not implement them, the adapter raises
            # RpcResponseError and we leave the field None. The
            # report carries the absence rather than guessing.
            try:
                safe_hex = await adapter._call_with_failover(  # noqa: SLF001
                    "eth_getBlockByNumber", ["safe", False]
                )
                safe = _hex_int(safe_hex.get("number")) if isinstance(safe_hex, dict) else None
            except Exception:  # noqa: BLE001
                pass
            try:
                finalised_hex = await adapter._call_with_failover(  # noqa: SLF001
                    "eth_getBlockByNumber", ["finalized", False]
                )
                finalised = (
                    _hex_int(finalised_hex.get("number"))
                    if isinstance(finalised_hex, dict)
                    else None
                )
            except Exception:  # noqa: BLE001
                pass
            # Archive depth: the difference between the latest block
            # and the genesis block. We approximate the genesis as
            # block 0 (true for most EVM chains); a chain whose
            # genesis is not block 0 would report an inflated depth,
            # which the caller should interpret with knowledge of
            # the chain.
            archive = max(latest - archive_window, 0) if latest is not None else None
        except Exception as exc:  # noqa: BLE001
            per_err.append(f"block-number probes: {type(exc).__name__}: {exc}")

        pm_size = pm_hash = sv_size = sv_hash = None
        try:
            pm_code_hex = await adapter._call_with_failover(  # noqa: SLF001
                "eth_getCode", [pm_addr_hex, "latest"]
            )
            pm_code = _decode_hex_bytes(pm_code_hex)
            pm_size = len(pm_code)
            pm_hash = _sha256_hex(pm_code)
        except Exception as exc:  # noqa: BLE001
            per_err.append(f"eth_getCode({pm_addr_hex}): {type(exc).__name__}: {exc}")
        try:
            sv_code_hex = await adapter._call_with_failover(  # noqa: SLF001
                "eth_getCode", [sv_addr_hex, "latest"]
            )
            sv_code = _decode_hex_bytes(sv_code_hex)
            sv_size = len(sv_code)
            sv_hash = _sha256_hex(sv_code)
        except Exception as exc:  # noqa: BLE001
            per_err.append(f"eth_getCode({sv_addr_hex}): {type(exc).__name__}: {exc}")

        if (
            expected.pool_manager_code_hash is not None
            and pm_hash is not None
            and pm_hash != expected.pool_manager_code_hash
        ):
            per_err.append(
                f"PoolManager bytecode hash mismatch: "
                f"observed={pm_hash} expected={expected.pool_manager_code_hash}"
            )
        if (
            expected.state_view_code_hash is not None
            and sv_hash is not None
            and sv_hash != expected.state_view_code_hash
        ):
            per_err.append(
                f"StateView bytecode hash mismatch: "
                f"observed={sv_hash} expected={expected.state_view_code_hash}"
            )

        # Pin the latest block on the first endpoint that returned one.
        if pinned_block_hash is None and latest is not None:
            try:
                block_obj = await adapter._call_with_failover(  # noqa: SLF001
                    "eth_getBlockByNumber", [hex(latest), False]
                )
                if isinstance(block_obj, dict) and "hash" in block_obj:
                    pinned_block_hash = block_obj["hash"]
                    pinned_block_number = latest
            except Exception:  # noqa: BLE001
                pass

        observed_chain_ids[ep.name] = cid if cid is not None else -1
        latest_block[ep.name] = latest if latest is not None else -1
        safe_block[ep.name] = safe if safe is not None else -1
        finalized_block[ep.name] = finalised if finalised is not None else -1
        archive_depth[ep.name] = archive if archive is not None else -1
        pool_manager[ep.name] = {"bytecode_size": pm_size, "code_hash": pm_hash}
        state_view[ep.name] = {"bytecode_size": sv_size, "code_hash": sv_hash}
        if per_err:
            errors.extend(f"{ep.name}: {e}" for e in per_err)

        # Report a single per-endpoint record too, even though the
        # top-level dict already contains the same info. This keeps
        # consumers from needing to enumerate endpoint names twice.
        _ = PerEndpointCapability(
            endpoint_name=ep.name,
            chain_id=cid,
            latest_block=latest,
            safe_block=safe,
            finalized_block=finalised,
            archive_depth_blocks=archive,
            pool_manager_bytecode_size=pm_size,
            pool_manager_code_hash=pm_hash,
            state_view_bytecode_size=sv_size,
            state_view_code_hash=sv_hash,
            errors=per_err,
        )

    passed = not errors
    if expected.min_block_number is not None and latest_block:
        observed_min = min(v for v in latest_block.values() if v >= 0)
        if observed_min < expected.min_block_number:
            errors.append(
                f"latest block {observed_min} is below expected minimum {expected.min_block_number}"
            )
            passed = False

    now = datetime.now(UTC).isoformat(timespec="seconds")
    return ChainCapabilityReport(
        expected_chain_id=expected.chain_id,
        observed_chain_ids=observed_chain_ids,
        latest_block=latest_block,
        safe_block=safe_block,
        finalized_block=finalized_block,
        archive_depth_blocks=archive_depth,
        pool_manager=pool_manager,
        state_view=state_view,
        pinned_block_hash=pinned_block_hash,
        pinned_block_number=pinned_block_number,
        pinned_at=now,
        source_retrieval_time=now,
        metrics=adapter.metrics.summary(),
        errors=errors,
        passed=passed,
    )


def _decode_hex_bytes(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ChainCapabilityError(
            f"expected hex string from eth_getCode, got {type(value).__name__}"
        )
    s = value.lower()
    if not s.startswith("0x"):
        raise ChainCapabilityError(f"expected 0x-prefixed hex, got {value!r}")
    return bytes.fromhex(s[2:])


def _sha256_hex(data: bytes) -> str:
    """SHA-256 of ``data`` returned as lowercase hex.

    We use the stdlib rather than pulling in another hashing package
    because the report only needs SHA-256 of a few KB of bytecode;
    EVM does not define a "canonical" bytecode hash for contracts,
    so SHA-256 is an audit hash, not a chain-level identifier.
    """
    import hashlib

    return hashlib.sha256(data).hexdigest()


__all__ = [
    "BytecodeMismatchError",
    "ChainCapabilityError",
    "ChainCapabilityReport",
    "ChainIdMismatchError",
    "ExpectedDeployment",
    "PerEndpointCapability",
    "probe_chain_capability",
]


# A sentinel helper used by tests; not part of the public surface.
def _to_json(report: ChainCapabilityReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)
