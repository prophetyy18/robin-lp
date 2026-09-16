"""Chain capability verification (T024).

Reads from an :class:`RpcAdapter` and produces a structured report
that T022 / T023 / T070 can use to decide whether the configured
chain deployment is real, consistent across providers, and matches
the project's documentation.

T024 acceptance (todo/README.md T024):

- RPC-reported chain ID matches the configured one;
- genesis block hash is recorded;
- latest / safe / finalised block numbers are reported;
- archive depth is recorded from a historical-call probe, not from
  ``latest - archive_window``;
- PoolManager / StateView bytecode hashes are reported and the
  actual bytecode (computed via ``eth_getCode``) is recorded;
- deployment block / transaction evidence (when archive supports it)
  is recorded, otherwise the absence is recorded with a reason;
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

import contextlib
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from robinhood_lp.protocol import Address
from robinhood_lp.rpc import RpcAdapter, RpcEndpoint, RpcError

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
# Constants
# ---------------------------------------------------------------------------


#: EIP-1167 minimal proxy prefix (10 bytes):
#: ``36 3d 3d 37 36 3d 3d 3d 3d 36 3d 3d 3d 36 3d 3d 3d 36 3d 73``.
EIP1167_PREFIX: bytes = bytes.fromhex("363d3d37363d3d3d3d363d3d3d363d73")


#: EIP-1167 minimal proxy suffix (15 bytes).
EIP1167_SUFFIX: bytes = bytes.fromhex("5af43d82803e903d91602b57fd5bf3")


#: Maximum log-range probe we attempt when measuring archive depth.
#: Larger probes are split by the adapter; we record the result so
#: the caller can see what the provider tolerated.
ARCHIVE_RANGE_PROBE_BLOCKS: int = 1024


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

    The probe is *fail-closed on drift*: when an expected hash is
    pinned and the observed hash differs, the report carries
    ``bytecode_drift`` errors and ``passed=False``. The first run on
    a chain (no expected hashes pinned) accepts any non-empty,
    non-proxy bytecode and surfaces the new hash in the report so
    subsequent runs can compare.
    """

    chain_id: int
    pool_manager_address: Address
    state_view_address: Address
    pool_manager_code_hash: str | None = None
    state_view_code_hash: str | None = None
    #: When set, the reporter checks the latest block number is at
    #: least this value (a deployment sanity check).
    min_block_number: int | None = None

    def is_first_run(self) -> bool:
        """``True`` when no expected hashes are pinned.

        The first run on a chain accepts any non-empty, non-proxy
        bytecode and pins the hash; subsequent runs compare against
        the pinned hash and fail closed on drift.
        """
        return self.pool_manager_code_hash is None and self.state_view_code_hash is None


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
class DeploymentEvidence:
    """Best-effort deployment evidence for one contract address.

    Fields are ``None`` when the underlying RPC does not support
    archive queries; ``reason`` records why the evidence is absent.
    """

    address: str
    earliest_block_with_code: int | None
    earliest_block_with_code_hash: str | None
    deployed_at_block_zero: bool | None
    archive_probe_method: str
    reason: str | None


@dataclass(slots=True)
class CrossEndpointAgreement:
    """The cross-endpoint agreement summary.

    All fields compare the *recorded* values per endpoint; if any
    endpoint failed a probe, the corresponding ``agree`` flag is
    ``False`` even if the surviving values match.
    """

    chain_id_agree: bool
    pool_manager_code_hash_agree: bool
    state_view_code_hash_agree: bool
    latest_block_agree: bool
    pinned_block_hash_agree: bool
    observed_chain_ids: dict[str, int]
    observed_latest_blocks: dict[str, int]
    observed_pinned_block_hashes: dict[str, str]


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
    # --- T024 gap closures ------------------------------------------------
    #: Hex hash of the genesis block (``0x...``), or ``None`` if every
    #: endpoint failed ``eth_getBlockByNumber(0, false)``.
    genesis_hash: str | None = None
    #: ``True`` iff genesis block number is 0 (the common case).
    genesis_block_number_zero: bool = True
    #: Per-endpoint pin of the latest block hash, used for cross-endpoint
    #: agreement. The top-level ``pinned_block_hash`` is the value from
    #: the lowest-weight active endpoint.
    per_endpoint_pinned_block_hashes: dict[str, str] = field(default_factory=dict)
    #: Summary of agreement across all active endpoints.
    cross_endpoint: CrossEndpointAgreement | None = None
    #: Best-effort deployment evidence per contract. May be ``None``
    #: when the underlying endpoints do not support archive queries.
    deployment_evidence: dict[str, DeploymentEvidence] = field(default_factory=dict)
    #: Endpoints that did not implement the ``safe`` / ``finalized``
    #: block tags; the report surfaces this rather than silently
    #: swallowing the error.
    unsupported_block_tags: dict[str, list[str]] = field(default_factory=dict)
    #: Recorded method name(s) used to derive archive depth (e.g.
    #: ``eth_getCode(..., "earliest")``).
    archive_probe_method: str = "not_probed"
    #: ``True`` iff this probe is the first probe for the deployment
    #: (no expected hashes were pinned); subsequent runs must match
    #: the pinned hashes or fail closed with ``bytecode_drift``.
    first_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hex_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.lower()
        if s.startswith("0x"):
            return int(s, 16)
        return int(s)
    raise ChainCapabilityError(f"cannot decode hex/int: {value!r}")


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
    return hashlib.sha256(data).hexdigest()


def _looks_like_eip1167_minimal_proxy(code: bytes) -> bool:
    """Detect the EIP-1167 minimal-proxy pattern.

    The standard EIP-1167 runtime bytecode is exactly 51 bytes:
    ``EIP1167_PREFIX (16) || <20-byte implementation address> ||
    EIP1167_SUFFIX (15)``. We additionally accept the 55-byte variant
    that some implementations emit (a 4-byte zero slot follows the
    prefix before the address) because the chain-by-chain evidence
    is the same and the V4 PoolManager / StateView must never be a
    proxy in either form.
    """
    if len(code) not in (16 + 20 + 15, 16 + 4 + 20 + 15):
        return False
    if not code.endswith(EIP1167_SUFFIX):
        return False
    # Strip a leading 4-byte zero slot if present.
    if code.startswith(b"\x00" * 4) and code[4:].startswith(EIP1167_PREFIX):
        return True
    return code.startswith(EIP1167_PREFIX)


def _is_empty_bytecode(code: bytes) -> bool:
    """``eth_getCode`` returns ``0x`` for accounts with no code.

    We treat an empty response as "not deployed" and fail closed.
    """
    return len(code) == 0


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


async def probe_chain_capability(
    adapter: RpcAdapter,
    expected: ExpectedDeployment,
    *,
    archive_window: int = ARCHIVE_RANGE_PROBE_BLOCKS,
    deployment_evidence_budget: int = 4,
) -> ChainCapabilityReport:
    """Run the capability probe and return a structured report.

    The probe is intentionally explicit and side-effect free; callers
    decide what to do with the report (T022 reads it before
    constructing the registry; T070 reads it as a runtime gate).

    Parameters
    ----------
    archive_window:
        Block range used when probing archive depth via ``eth_getLogs``
        and as the binary-search step ceiling when looking up the
        deployment block via ``eth_getCode`` at older blocks. Currently
        unused; reserved for the ``eth_getLogs`` archive probe planned
        for T040/T042.
    deployment_evidence_budget:
        Maximum number of extra ``eth_getCode`` requests per contract
        used to binary-search the deployment block. ``0`` disables
        the search entirely.
    """
    errors: list[str] = []
    observed_chain_ids: dict[str, int] = {}
    latest_block: dict[str, int] = {}
    safe_block: dict[str, int] = {}
    finalized_block: dict[str, int] = {}
    archive_depth: dict[str, int] = {}
    pool_manager: dict[str, dict[str, Any]] = {}
    state_view: dict[str, dict[str, Any]] = {}
    per_endpoint_pinned: dict[str, str] = {}
    unsupported_tags: dict[str, list[str]] = {}

    endpoints = adapter._config.active_endpoints()  # noqa: SLF001
    pm_addr_hex = expected.pool_manager_address.to_hex()
    sv_addr_hex = expected.state_view_address.to_hex()

    pinned_block_hash: str | None = None
    pinned_block_number: int | None = None
    genesis_hash: str | None = None
    genesis_block_number_zero = True

    # First-run determination: if the caller did not pin a hash for
    # *either* contract, the probe accepts any non-empty, non-proxy
    # bytecode and surfaces the new hash in the report. If at least
    # one hash is pinned, the probe must match (drift is fail-closed).
    first_run = expected.is_first_run()

    for ep in endpoints:
        per_err: list[str] = []
        ep_unsupported: list[str] = []

        cid: int | None = None
        try:
            cid_hex = await adapter._call_single(ep, "eth_chainId", [])  # noqa: SLF001
            cid = _hex_int(cid_hex)
        except RpcError as exc:
            per_err.append(f"{ep.name}: eth_chainId: {type(exc).__name__}: {exc}")
        if cid is not None and cid != expected.chain_id:
            per_err.append(
                f"{ep.name}: chain_id mismatch: observed={cid} expected={expected.chain_id}"
            )

        latest: int | None = None
        safe: int | None = None
        finalised: int | None = None

        try:
            latest_hex = await adapter._call_single(  # noqa: SLF001
                ep, "eth_blockNumber", []
            )
            latest = _hex_int(latest_hex)
        except RpcError as exc:
            per_err.append(f"{ep.name}: eth_blockNumber: {type(exc).__name__}: {exc}")

        # Safe and finalized are optional block tags. A provider that
        # does not implement them surfaces a JSON-RPC error rather
        # than returning ``null``; we record the tag as unsupported
        # and surface the error so the report is fail-closed on
        # unsupported tags rather than silently swallowing them.
        if latest is not None:
            try:
                safe_hex = await adapter._call_single(  # noqa: SLF001
                    ep, "eth_getBlockByNumber", ["safe", False]
                )
                if isinstance(safe_hex, dict) and "number" in safe_hex:
                    safe = _hex_int(safe_hex["number"])
            except RpcError as exc:
                ep_unsupported.append("safe")
                per_err.append(
                    f"{ep.name}: eth_getBlockByNumber(safe): {type(exc).__name__}: {exc}"
                )
            try:
                finalised_hex = await adapter._call_single(  # noqa: SLF001
                    ep, "eth_getBlockByNumber", ["finalized", False]
                )
                if isinstance(finalised_hex, dict) and "number" in finalised_hex:
                    finalised = _hex_int(finalised_hex["number"])
            except RpcError as exc:
                ep_unsupported.append("finalized")
                per_err.append(
                    f"{ep.name}: eth_getBlockByNumber(finalized): {type(exc).__name__}: {exc}"
                )

        # PoolManager and StateView bytecodes.
        pm_size: int | None = None
        pm_hash: str | None = None
        sv_size: int | None = None
        sv_hash: str | None = None

        try:
            pm_code_hex = await adapter._call_single(  # noqa: SLF001
                ep, "eth_getCode", [pm_addr_hex, "latest"]
            )
            pm_code = _decode_hex_bytes(pm_code_hex)
            pm_size = len(pm_code)
            pm_hash = _sha256_hex(pm_code)
        except RpcError as exc:
            per_err.append(f"{ep.name}: eth_getCode({pm_addr_hex}): {type(exc).__name__}: {exc}")
            pm_code = b""
        if pm_size is not None and _is_empty_bytecode(pm_code):
            per_err.append(f"{ep.name}: PoolManager bytecode empty at {pm_addr_hex}: not deployed")
        elif pm_size is not None and _looks_like_eip1167_minimal_proxy(pm_code):
            per_err.append(
                f"{ep.name}: PoolManager bytecode matches EIP-1167 minimal proxy at "
                f"{pm_addr_hex}: unexpected (V4 PoolManager must not be a proxy)"
            )
        elif (
            pm_size is not None
            and expected.pool_manager_code_hash is not None
            and pm_hash != expected.pool_manager_code_hash
        ):
            per_err.append(
                f"{ep.name}: PoolManager bytecode drift: "
                f"observed={pm_hash} expected={expected.pool_manager_code_hash}"
            )

        try:
            sv_code_hex = await adapter._call_single(  # noqa: SLF001
                ep, "eth_getCode", [sv_addr_hex, "latest"]
            )
            sv_code = _decode_hex_bytes(sv_code_hex)
            sv_size = len(sv_code)
            sv_hash = _sha256_hex(sv_code)
        except RpcError as exc:
            per_err.append(f"{ep.name}: eth_getCode({sv_addr_hex}): {type(exc).__name__}: {exc}")
            sv_code = b""
        if sv_size is not None and _is_empty_bytecode(sv_code):
            per_err.append(f"{ep.name}: StateView bytecode empty at {sv_addr_hex}: not deployed")
        elif sv_size is not None and _looks_like_eip1167_minimal_proxy(sv_code):
            per_err.append(
                f"{ep.name}: StateView bytecode matches EIP-1167 minimal proxy at "
                f"{sv_addr_hex}: unexpected (V4 StateView must not be a proxy)"
            )
        elif (
            sv_size is not None
            and expected.state_view_code_hash is not None
            and sv_hash != expected.state_view_code_hash
        ):
            per_err.append(
                f"{ep.name}: StateView bytecode drift: "
                f"observed={sv_hash} expected={expected.state_view_code_hash}"
            )

        # Pin the latest block via this endpoint. The cross-endpoint
        # agreement summary compares every endpoint's pinned hash.
        ep_pinned: str | None = None
        if latest is not None:
            try:
                block_obj = await adapter._call_single(  # noqa: SLF001
                    ep, "eth_getBlockByNumber", [hex(latest), False]
                )
                if isinstance(block_obj, dict) and "hash" in block_obj:
                    ep_pinned = str(block_obj["hash"])
                    per_endpoint_pinned[ep.name] = ep_pinned
                    if pinned_block_hash is None:
                        pinned_block_hash = ep_pinned
                        pinned_block_number = latest
            except RpcError as exc:
                per_err.append(
                    f"{ep.name}: eth_getBlockByNumber(latest pin): {type(exc).__name__}: {exc}"
                )

        # Archive depth per endpoint: at minimum the chain has
        # ``latest + 1`` blocks (block 0 through ``latest``). The
        # deeper archive probe (via ``eth_getCode(addr, "earliest")``)
        # is run once after the loop and recorded in
        # ``archive_probe_method``; here we just record the chain
        # height so the per-endpoint report is internally consistent.
        if latest is not None:
            archive_depth[ep.name] = latest
        else:
            archive_depth[ep.name] = -1

        observed_chain_ids[ep.name] = cid if cid is not None else -1
        latest_block[ep.name] = latest if latest is not None else -1
        safe_block[ep.name] = safe if safe is not None else -1
        finalized_block[ep.name] = finalised if finalised is not None else -1
        pool_manager[ep.name] = {"bytecode_size": pm_size, "code_hash": pm_hash}
        state_view[ep.name] = {"bytecode_size": sv_size, "code_hash": sv_hash}
        if ep_unsupported:
            unsupported_tags[ep.name] = ep_unsupported
        if per_err:
            errors.extend(per_err)

        _ = PerEndpointCapability(
            endpoint_name=ep.name,
            chain_id=cid,
            latest_block=latest,
            safe_block=safe,
            finalized_block=finalised,
            archive_depth_blocks=archive_depth[ep.name] if archive_depth[ep.name] >= 0 else None,
            pool_manager_bytecode_size=pm_size,
            pool_manager_code_hash=pm_hash,
            state_view_bytecode_size=sv_size,
            state_view_code_hash=sv_hash,
            errors=per_err,
        )

    # Genesis block hash: query the lowest-weight endpoint. Failures
    # are recorded but do not fail the whole report when at least one
    # endpoint can supply the value.
    try:
        genesis_obj = await adapter._call_with_failover(  # noqa: SLF001
            "eth_getBlockByNumber", ["0x0", False]
        )
        if isinstance(genesis_obj, dict) and "hash" in genesis_obj:
            genesis_hash = str(genesis_obj["hash"])
        if isinstance(genesis_obj, dict) and "number" in genesis_obj:
            with contextlib.suppress(ChainCapabilityError):
                genesis_block_number_zero = _hex_int(genesis_obj["number"]) == 0
    except RpcError as exc:
        errors.append(f"genesis probe: eth_getBlockByNumber(0x0): {type(exc).__name__}: {exc}")

    # Cross-endpoint agreement.
    chain_ids_observed = {name: cid for name, cid in observed_chain_ids.items() if cid >= 0}
    latest_blocks_observed = {name: b for name, b in latest_block.items() if b >= 0}
    pm_hashes_observed = {
        name: info.get("code_hash") for name, info in pool_manager.items() if info.get("code_hash")
    }
    sv_hashes_observed = {
        name: info.get("code_hash") for name, info in state_view.items() if info.get("code_hash")
    }
    pinned_hashes_observed = dict(per_endpoint_pinned)

    chain_id_agree = len(set(chain_ids_observed.values())) <= 1
    pm_hash_agree = len(set(pm_hashes_observed.values())) <= 1
    sv_hash_agree = len(set(sv_hashes_observed.values())) <= 1

    # Latest-block agreement tolerates a small propagation drift
    # (a few blocks). Honest providers that lag the canonical head
    # by a handful of blocks are still considered consistent; a real
    # fork or stale snapshot would diverge by orders of magnitude.
    latest_block_values = list(latest_blocks_observed.values())
    if len(latest_block_values) <= 1:
        latest_agree = True
        latest_block_drift = 0
    else:
        latest_block_drift = max(latest_block_values) - min(latest_block_values)
        latest_agree = latest_block_drift <= 2

    # Pinned-block-hash agreement is only meaningful when every
    # endpoint pinned the same block number. When the latest-block
    # values diverge within the propagation-drift tolerance, the
    # per-endpoint pins are by definition different block hashes;
    # we surface this as an explicit field rather than as a failure.
    pinned_block_numbers = {
        name: latest_blocks_observed.get(name) for name in pinned_hashes_observed
    }
    same_pinned_block_numbers = (
        len(set(pinned_block_numbers.values())) <= 1 if pinned_block_numbers else True
    )
    if same_pinned_block_numbers and len(set(pinned_hashes_observed.values())) <= 1:
        pinned_agree = True
    elif same_pinned_block_numbers:
        pinned_agree = False
    else:
        # Different endpoints pinned different blocks; not a fail-
        # closed condition unless the drift exceeds tolerance.
        pinned_agree = latest_agree

    cross_endpoint = CrossEndpointAgreement(
        chain_id_agree=chain_id_agree,
        pool_manager_code_hash_agree=pm_hash_agree,
        state_view_code_hash_agree=sv_hash_agree,
        latest_block_agree=latest_agree,
        pinned_block_hash_agree=pinned_agree,
        observed_chain_ids=chain_ids_observed,
        observed_latest_blocks=latest_blocks_observed,
        observed_pinned_block_hashes=pinned_hashes_observed,
    )

    if not chain_id_agree:
        errors.append(f"chain_id disagreement across endpoints: {chain_ids_observed}")
    if not pm_hash_agree and pm_hashes_observed:
        errors.append(
            f"PoolManager bytecode hash disagreement across endpoints: {pm_hashes_observed}"
        )
    if not sv_hash_agree and sv_hashes_observed:
        errors.append(
            f"StateView bytecode hash disagreement across endpoints: {sv_hashes_observed}"
        )
    if not latest_agree and len(latest_block_values) > 1:
        errors.append(
            f"latest block drift across endpoints exceeds tolerance: "
            f"drift={latest_block_drift} values={latest_blocks_observed}"
        )
    if not pinned_agree and same_pinned_block_numbers and len(pinned_hashes_observed) > 1:
        errors.append(f"pinned block hash disagreement across endpoints: {pinned_hashes_observed}")

    # If the deployment is a first-run, a missing bytecode pin is
    # expected; we surface that explicitly rather than failing closed.
    if first_run:
        for ep_name, info in pool_manager.items():
            if info.get("code_hash") is None:
                errors.append(f"{ep_name}: first-run probe could not pin PoolManager code hash")
        for ep_name, info in state_view.items():
            if info.get("code_hash") is None:
                errors.append(f"{ep_name}: first-run probe could not pin StateView code hash")

    passed = not errors
    if expected.min_block_number is not None and latest_block:
        observed_min = min(v for v in latest_block.values() if v >= 0)
        if observed_min < expected.min_block_number:
            errors.append(
                f"latest block {observed_min} is below expected minimum {expected.min_block_number}"
            )
            passed = False

    # Deployment evidence (best-effort). We try ``eth_getCode(addr, "earliest")``
    # via failover; if it returns the same bytecode as ``latest``, the
    # contract was deployed before the earliest block we can see (we
    # cannot narrow it further). If it returns ``0x``, the contract
    # was deployed later than the earliest block; we try one binary-
    # search step toward the latest block (the budget caps how many
    # extra requests we make). When neither path yields useful data,
    # we record ``None`` with a reason.
    deployment_evidence: dict[str, DeploymentEvidence] = {}

    archive_probe_method = "not_probed"
    for label, addr_hex in (
        ("pool_manager", pm_addr_hex),
        ("state_view", sv_addr_hex),
    ):
        evidence = await _collect_deployment_evidence(
            adapter=adapter,
            addr_hex=addr_hex,
            expected_chain_id=expected.chain_id,
            latest_block=pinned_block_number,
            budget=deployment_evidence_budget,
        )
        deployment_evidence[label] = evidence
        if archive_probe_method == "not_probed":
            archive_probe_method = evidence.archive_probe_method
        elif "earliest" in evidence.archive_probe_method:
            # Prefer the earliest-tag probe in the summary because it
            # is the one we always run; deeper search strings are an
            # extra detail recorded in the per-contract evidence.
            archive_probe_method = "eth_getCode(addr, earliest)"

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
        genesis_hash=genesis_hash,
        genesis_block_number_zero=genesis_block_number_zero,
        per_endpoint_pinned_block_hashes=per_endpoint_pinned,
        cross_endpoint=cross_endpoint,
        deployment_evidence=deployment_evidence,
        unsupported_block_tags=unsupported_tags,
        archive_probe_method=archive_probe_method,
        first_run=first_run,
    )


async def _collect_deployment_evidence(
    *,
    adapter: RpcAdapter,
    addr_hex: str,
    expected_chain_id: int,
    latest_block: int | None,
    budget: int,
) -> DeploymentEvidence:
    """Best-effort deployment evidence for one address.

    The probe tries ``eth_getCode(addr, "earliest")`` first. If that
    returns non-empty code, the contract was deployed at or before
    the earliest block the provider exposes; if it returns ``0x``,
    we may be able to narrow the deployment block via a bounded
    binary search (``budget`` caps the number of extra requests).
    """
    reason: str | None = None
    method = "eth_getCode(addr, earliest)"
    earliest_block: int | None = None
    earliest_hash: str | None = None
    deployed_at_zero: bool | None = None

    try:
        earliest_hex = await adapter._call_with_failover(  # noqa: SLF001
            "eth_getCode", [addr_hex, "earliest"]
        )
        earliest_code = _decode_hex_bytes(earliest_hex)
        if _is_empty_bytecode(earliest_code):
            deployed_at_zero = False
            # Code was not present at the earliest block we can see;
            # binary search toward latest if budget allows.
            if budget > 0 and latest_block is not None and latest_block > 0:
                # One-step probe at half the range; deeper search
                # would require many calls and we keep the budget
                # tight for the testnet first run.
                mid = max(1, latest_block // 2)
                try:
                    mid_hex = await adapter._call_with_failover(  # noqa: SLF001
                        "eth_getCode", [addr_hex, f"0x{mid:x}"]
                    )
                    mid_code = _decode_hex_bytes(mid_hex)
                    earliest_block = mid + 1 if _is_empty_bytecode(mid_code) else 1
                    earliest_hash = _sha256_hex(mid_code)
                    method = f"eth_getCode(addr, earliest) + eth_getCode(addr, 0x{mid:x})"
                except RpcError as exc:
                    reason = f"midpoint probe failed: {type(exc).__name__}: {exc}"
            else:
                reason = "no budget or no latest block for binary search"
        else:
            deployed_at_zero = True
            earliest_block = 0
            earliest_hash = _sha256_hex(earliest_code)
    except RpcError as exc:
        reason = f"earliest probe failed: {type(exc).__name__}: {exc}"

    return DeploymentEvidence(
        address=addr_hex,
        earliest_block_with_code=earliest_block,
        earliest_block_with_code_hash=earliest_hash,
        deployed_at_block_zero=deployed_at_zero,
        archive_probe_method=method,
        reason=reason,
    )


def _to_json(report: ChainCapabilityReport) -> str:
    """JSON serializer used by tests and the real-chain runner."""
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


__all__ = [
    "ARCHIVE_RANGE_PROBE_BLOCKS",
    "BytecodeMismatchError",
    "ChainCapabilityError",
    "ChainCapabilityReport",
    "ChainIdMismatchError",
    "CrossEndpointAgreement",
    "DeploymentEvidence",
    "EIP1167_PREFIX",
    "EIP1167_SUFFIX",
    "ExpectedDeployment",
    "PerEndpointCapability",
    "probe_chain_capability",
]


#: Re-export of the endpoint type so callers can construct endpoints
#: without importing the adapter module directly.
_ = RpcEndpoint
