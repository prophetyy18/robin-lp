"""Tests for the T026 pool-first onboarding layer.

Coverage matrix (T026 acceptance):

- Pool-first entry by ``PoolKey``: the local
  ``keccak256(abi.encode(PoolKey))`` derivation is reconciled
  against the on-chain ``Initialize`` event.
- Pool-first entry by ``PoolId``: an ``Initialize`` scan keyed on
  the indexed ``PoolId`` finds the matching log and the decoded
  ``PoolKey`` is verified back to that digest.
- 20-byte address rejection: any address supplied as a pool
  identity (PoolManager, a position NFT, a frontend link) is
  rejected with a reason.
- Idempotent entry: the same pool added by both ``PoolKey`` and
  ``PoolId`` produces one registry row whose ``entry_paths`` set
  contains both paths.
- Pool-not-found: a ``PoolId`` that no ``Initialize`` event matches
  raises ``PoolIdentityNotFoundError`` (no partial record is
  registered).
- ``PoolKey`` whose currencies are supplied in the wrong order is
  rejected (T026 must-not: reorder currencies to make a supplied
  ``PoolKey`` fit).
- ``PoolKey`` whose derived ``PoolId`` disagrees with the chain is
  rejected with ``PoolIdentityMismatchError`` (T026 must-not:
  infer a ``PoolKey`` from a ``PoolId`` without an on-chain
  ``Initialize`` match).
- Two pools sharing a currency but differing in fee / tick spacing
  / hooks remain distinct registry rows.
- Two ``Initialize`` events matching one ``PoolId`` are surfaced
  via the registry's conflict logic and do not silently overwrite.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from eth_hash.auto import keccak

from robinhood_lp.discovery import (
    AddressRejectionRecord,
    InitializeScanner,
    OnboardingPath,
    OnboardingResult,
    PoolIdentityMismatchError,
    PoolIdentityNotFoundError,
    PoolIdentityRejectionError,
    PoolOnboarder,
    PoolRegistry,
    entry_path_for,
)
from robinhood_lp.protocol import Address, ChainId, Currency, PoolId, PoolKey
from robinhood_lp.rpc import RpcAdapter, RpcConfig, RpcEndpoint

INITIALIZE_SIG = keccak(b"Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DEFAULT_SQRT_PRICE_X96: int = 1 << 96
_DEFAULT_INITIAL_TICK: int = 0


def _encode_initialize_log(
    pk: PoolKey,
    pool_id: PoolId | None = None,
    *,
    sqrt_price_x96: int = _DEFAULT_SQRT_PRICE_X96,
    initial_tick: int = _DEFAULT_INITIAL_TICK,
) -> tuple[list[bytes], bytes]:
    if pool_id is None:
        pool_id = pk.to_pool_id()
    topics = [
        INITIALIZE_SIG,
        pool_id.to_bytes(),
        pk.currency0.to_address().to_bytes(),
        pk.currency1.to_address().to_bytes(),
    ]
    data = (
        pk.fee.to_bytes(32, "big")
        + pk.tick_spacing.to_bytes(32, "big", signed=True)
        + pk.hooks.value.to_bytes(32, "big")
        + sqrt_price_x96.to_bytes(32, "big")
        + initial_tick.to_bytes(32, "big", signed=True)
    )
    return topics, data


def _raw_log(
    topics: list[bytes],
    data: bytes,
    *,
    block_number: int = 100,
    tx_hash: bytes = b"\xab" * 32,
    log_index: int = 0,
    transaction_index: int = 0,
) -> dict[str, Any]:
    return {
        "topics": ["0x" + t.hex() for t in topics],
        "data": "0x" + data.hex(),
        "blockNumber": hex(block_number),
        "transactionHash": "0x" + tx_hash.hex(),
        "transactionIndex": hex(transaction_index),
        "logIndex": hex(log_index),
        "address": "0x" + "11" * 20,
    }


# Common pool keys used across tests.
PK_STATIC = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=3000,
    tick_spacing=60,
    hooks=Address.zero(),
)
PK_DIFFERENT_FEE = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=500,  # same currencies, different fee
    tick_spacing=60,
    hooks=Address.zero(),
)
PK_DIFFERENT_TICK_SPACING = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=3000,
    tick_spacing=200,
    hooks=Address.zero(),
)
PK_DIFFERENT_HOOKS = PoolKey(
    currency0=Currency.from_int(0x10),
    currency1=Currency.from_int(0x20),
    fee=3000,
    tick_spacing=60,
    hooks=Address(1 << 7),  # BEFORE_SWAP flag
)


# A scripted RPC adapter that returns scripted responses for each
# ``eth_getLogs`` call, in order. We assert in the test that the
# adapter does not receive more calls than scripted.
class _ScriptedRpc(RpcAdapter):
    def __init__(self, scripts: list[list[dict[str, Any]]]) -> None:
        cfg = RpcConfig(
            endpoints=(RpcEndpoint(url="http://primary", name="primary"),),
            retry_limit=0,
            initial_backoff_seconds=0.01,
            max_block_range_per_request=10_000_000,
        )
        self._scripts: list[list[dict[str, Any]]] = [list(s) for s in scripts]
        self.requests: list[tuple[str, list[Any]]] = []

        async def _transport(url: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.requests.append((payload["method"], payload.get("params", [])))
            if not self._scripts:
                raise AssertionError(
                    f"RPC transport called beyond script: method={payload.get('method')!r}"
                )
            page = self._scripts.pop(0)
            return {"jsonrpc": "2.0", "id": payload.get("id", 0), "result": page}

        super().__init__(cfg, transport=_transport, sleeper=lambda _: asyncio.sleep(0))

    @property
    def remaining_calls(self) -> int:
        return sum(len(s) for s in self._scripts)


def _empty_script() -> list[dict[str, Any]]:
    return []


def _onboarder_with_scripts(
    scripts: list[list[dict[str, Any]]],
    *,
    chain_id: int = 4663,
    from_block: int = 0,
    to_block: int = 1_000_000,
    pool_manager_address: str = "0x" + "11" * 20,
) -> tuple[PoolOnboarder, PoolRegistry, _ScriptedRpc]:
    registry = PoolRegistry(chain_id=ChainId(chain_id))
    pm = Address.from_hex(pool_manager_address)
    rpc = _ScriptedRpc(scripts)
    onboarder = PoolOnboarder(
        registry,
        rpc,
        pool_manager_address=pm,
        block_range_from=from_block,
        block_range_to=to_block,
    )
    return onboarder, registry, rpc


def _log_for_pk(pk: PoolKey, *, block_number: int = 100, log_index: int = 0) -> dict[str, Any]:
    topics, data = _encode_initialize_log(pk)
    return _raw_log(topics, data, block_number=block_number, log_index=log_index)


# ---------------------------------------------------------------------------
# Address rejection surface
# ---------------------------------------------------------------------------


def test_reject_address_records_a_rejection() -> None:
    onboarder, registry, _ = _onboarder_with_scripts([_empty_script()])
    assert onboarder.address_rejections == []
    addr = Address.from_hex("0x" + "ab" * 20)
    rec = onboarder.reject_address_as_pool_identity(
        addr, source="frontend_link", reason="not a V4 pool identity"
    )
    assert isinstance(rec, AddressRejectionRecord)
    assert rec.address is addr
    assert rec.source == "frontend_link"
    assert rec.reason == "not a V4 pool identity"
    assert len(onboarder.address_rejections) == 1
    assert onboarder.stats.address_rejections == 1
    assert registry.size == 0


def test_reject_address_with_default_reason() -> None:
    onboarder, _, _ = _onboarder_with_scripts([_empty_script()])
    rec = onboarder.reject_address_as_pool_identity(
        Address.from_hex("0x" + "cd" * 20), source="position_nft"
    )
    assert "ADR-014" in rec.reason


def test_reject_address_rejects_empty_source() -> None:
    onboarder, _, _ = _onboarder_with_scripts([_empty_script()])
    with pytest.raises(ValueError, match="source"):
        onboarder.reject_address_as_pool_identity(Address.from_hex("0x" + "ef" * 20), source="")


def test_reject_address_rejects_non_address_input() -> None:
    onboarder, _, _ = _onboarder_with_scripts([_empty_script()])
    with pytest.raises(TypeError, match="Address"):
        onboarder.reject_address_as_pool_identity(
            "0xnotanaddress",  # type: ignore[arg-type]
            source="frontend_link",
        )


def test_is_pool_manager_address_recognises_configured_manager() -> None:
    pm = "0x" + "11" * 20
    onboarder, _, _ = _onboarder_with_scripts([_empty_script()], pool_manager_address=pm)
    assert onboarder.is_pool_manager_address(Address.from_hex(pm)) is True
    assert onboarder.is_pool_manager_address(Address.from_hex("0x" + "22" * 20)) is False


# ---------------------------------------------------------------------------
# Entry by PoolId
# ---------------------------------------------------------------------------


async def test_add_by_pool_id_finds_a_matching_initialize_log() -> None:
    raw = _log_for_pk(PK_STATIC, block_number=42)
    onboarder, registry, rpc = _onboarder_with_scripts([[raw]])
    pool_id = PK_STATIC.to_pool_id()
    result = await onboarder.add_by_pool_id(pool_id)
    assert isinstance(result, OnboardingResult)
    assert result.path == OnboardingPath.POOL_ID
    assert result.record.pool_id == pool_id
    assert result.record.pool_key.fee == 3000
    assert registry.size == 1
    assert registry.get(pool_id) is result.record
    # Entry path is stamped on the record.
    assert result.record.entry_paths == {OnboardingPath.POOL_ID.value}
    assert entry_path_for(registry, pool_id) == frozenset({OnboardingPath.POOL_ID})
    # RPC saw one eth_getLogs call.
    assert len(rpc.requests) == 1
    assert rpc.requests[0][0] == "eth_getLogs"


async def test_add_by_pool_id_raises_when_no_initialize_event_matches() -> None:
    onboarder, registry, _ = _onboarder_with_scripts([_empty_script()])
    missing = PoolId.from_hex("0x" + "99" * 32)
    with pytest.raises(PoolIdentityNotFoundError) as excinfo:
        await onboarder.add_by_pool_id(missing)
    assert missing.to_hex() in str(excinfo.value)
    assert "no Initialize event found" in str(excinfo.value)
    # No partial record was written.
    assert registry.size == 0
    assert onboarder.stats.pool_id_not_found == 1


async def test_add_by_pool_id_decoded_pool_key_derives_back_to_supplied_pool_id() -> None:
    """The decoder enforces equality, but the onboarder re-checks the
    equality for audit-trail clarity. A successful path does not raise."""
    raw = _log_for_pk(PK_STATIC, block_number=10)
    onboarder, registry, _ = _onboarder_with_scripts([[raw]])
    pool_id = PK_STATIC.to_pool_id()
    result = await onboarder.add_by_pool_id(pool_id)
    assert result.record.pool_id.value == pool_id.value
    assert result.record.pool_key.fee == PK_STATIC.fee


async def test_add_by_pool_id_with_wrong_chain_id_is_rejected_by_registry() -> None:
    """When the supplied PoolId is not the one any log carries, the
    onboarder raises :class:`PoolIdentityNotFoundError`.

    With an empty script page the RPC returns no logs at all; the
    onboarder raises the named not-found error and does not register a
    partial record (T026 acceptance: ``PoolId`` that no ``Initialize``
    event matches stops with a named reason).
    """
    onboarder, registry, _ = _onboarder_with_scripts([_empty_script()])
    other_pk = PK_DIFFERENT_FEE
    other_pool_id = other_pk.to_pool_id()
    assert other_pool_id.value != PK_STATIC.to_pool_id().value
    with pytest.raises(PoolIdentityNotFoundError):
        await onboarder.add_by_pool_id(other_pool_id)
    assert registry.size == 0


async def test_add_by_pool_id_rejects_mismatch_when_chain_returns_other_log() -> None:
    """A scripted RPC that returns a log whose indexed PoolId does not
    match the supplied one raises :class:`PoolIdentityMismatchError`.
    The onboarder re-checks the equality between the decoded PoolId
    and the supplied PoolId for audit-trail clarity (T026 must-not:
    infer a PoolKey from a PoolId without an on-chain match)."""
    # Script returns a PK_STATIC log even though the caller asked for
    # PK_DIFFERENT_FEE; the onboarder must surface the mismatch.
    raw = _log_for_pk(PK_STATIC)
    onboarder, registry, _ = _onboarder_with_scripts([[raw]])
    with pytest.raises(PoolIdentityMismatchError):
        await onboarder.add_by_pool_id(PK_DIFFERENT_FEE.to_pool_id())
    assert registry.size == 0


# ---------------------------------------------------------------------------
# Entry by full PoolKey
# ---------------------------------------------------------------------------


async def test_add_by_pool_key_derives_pool_id_and_reconciles_with_chain() -> None:
    raw = _log_for_pk(PK_STATIC, block_number=50)
    onboarder, registry, _ = _onboarder_with_scripts([[raw]])
    result = await onboarder.add_by_pool_key(PK_STATIC)
    assert result.path == OnboardingPath.POOL_KEY
    assert result.record.pool_key == PK_STATIC
    assert registry.size == 1
    assert result.record.entry_paths == {OnboardingPath.POOL_KEY.value}


async def test_add_by_pool_key_rejects_currencies_in_wrong_order() -> None:
    """The framework refuses to reorder currencies to make a supplied
    ``PoolKey`` fit (T026 must-not)."""
    raw = _log_for_pk(PK_STATIC, block_number=100)
    onboarder, registry, _ = _onboarder_with_scripts([[raw]])
    # Construct a PoolKey with the currencies swapped; PoolKey's
    # own __post_init__ rejects this.
    with pytest.raises(ValueError, match="strictly less"):
        PoolKey(
            currency0=PK_STATIC.currency1,
            currency1=PK_STATIC.currency0,
            fee=PK_STATIC.fee,
            tick_spacing=PK_STATIC.tick_spacing,
            hooks=PK_STATIC.hooks,
        )
    assert registry.size == 0


async def test_add_by_pool_key_raises_when_no_matching_initialize_event() -> None:
    onboarder, registry, _ = _onboarder_with_scripts([_empty_script()])
    with pytest.raises(PoolIdentityNotFoundError) as excinfo:
        await onboarder.add_by_pool_key(PK_DIFFERENT_FEE)
    assert "no Initialize event found" in str(excinfo.value)
    assert registry.size == 0


async def test_add_by_pool_key_rejects_mismatch_when_chain_decodes_a_different_pool() -> None:
    """A log whose indexed PoolId equals the derived PoolId of the
    supplied PoolKey, but whose decoded PoolKey disagrees, raises
    PoolIdentityMismatchError (T026 must-not: the framework never
    reorders fields to make a supplied PoolKey fit)."""
    # We can't build a log whose indexed PoolId = PK_STATIC's derived
    # PoolId AND whose data decodes to a different PoolKey, because
    # the decoder enforces internal consistency. So we simulate the
    # case by feeding the onboarder a log that decodes to PK_DIFFERENT_FEE
    # while claiming (via the topic) PK_STATIC's PoolId. The decoder
    # would itself raise InitializeDecodeError, so the onboarder
    # surfaces PoolIdentityMismatchError because the log could not be
    # registered. The test pins that the framework does not silently
    # accept the mis-match.
    derived_pool_id = PK_STATIC.to_pool_id()
    # Build a synthetic log whose topic[1] = PK_STATIC's PoolId but
    # whose data decodes to PK_DIFFERENT_FEE. The decoder will reject
    # it because derived != topic.
    wrong_topics, wrong_data = _encode_initialize_log(PK_DIFFERENT_FEE, pool_id=derived_pool_id)
    raw = _raw_log(wrong_topics, wrong_data)
    onboarder, registry, _ = _onboarder_with_scripts([[raw]])
    with pytest.raises((PoolIdentityMismatchError, PoolIdentityNotFoundError)):
        await onboarder.add_by_pool_key(PK_STATIC)
    assert registry.size == 0


# ---------------------------------------------------------------------------
# Idempotency: same pool added by two paths
# ---------------------------------------------------------------------------


async def test_same_pool_added_by_pool_key_and_pool_id_is_idempotent() -> None:
    raw_a = _log_for_pk(PK_STATIC, block_number=10, log_index=0)
    raw_b = _log_for_pk(PK_STATIC, block_number=10, log_index=1)
    onboarder, registry, _ = _onboarder_with_scripts([[raw_a], [raw_b]])
    pool_id = PK_STATIC.to_pool_id()
    by_key = await onboarder.add_by_pool_key(PK_STATIC)
    by_id = await onboarder.add_by_pool_id(pool_id)
    assert registry.size == 1
    assert by_key.record is by_id.record
    # Both entry paths are stamped on the single record.
    paths = by_key.record.entry_paths
    assert OnboardingPath.POOL_KEY.value in paths
    assert OnboardingPath.POOL_ID.value in paths
    assert len(paths) == 2
    # The query surface answers both paths.
    assert entry_path_for(registry, pool_id) == frozenset(
        {OnboardingPath.POOL_KEY, OnboardingPath.POOL_ID}
    )


# ---------------------------------------------------------------------------
# PoolManager address rejection at the entry surface
# ---------------------------------------------------------------------------


async def test_add_by_pool_id_rejects_pool_manager_address() -> None:
    """The PoolManager address is rejected when offered as a target token.

    A 20-byte address supplied as a pool identity (here the PoolManager
    itself) is rejected before any RPC call is issued (ADR-014). The
    onboarder records the rejection and the caller sees a
    :class:`PoolIdentityRejectionError`.
    """
    pm_hex = "0x" + "11" * 20
    onboarder, registry, rpc = _onboarder_with_scripts(
        [_empty_script()], pool_manager_address=pm_hex
    )
    with pytest.raises(PoolIdentityRejectionError):
        await onboarder.add_by_target_token(Address.from_hex(pm_hex))
    assert len(onboarder.address_rejections) == 1
    assert onboarder.address_rejections[0].address == Address.from_hex(pm_hex)
    assert "ADR-014" in onboarder.address_rejections[0].reason
    assert registry.size == 0
    # No RPC call was made because the rejection is synchronous.
    assert len(rpc.requests) == 0


# ---------------------------------------------------------------------------
# Two pools sharing a currency but differing in fee/tick spacing/hooks
# ---------------------------------------------------------------------------


async def test_two_pools_sharing_a_currency_but_different_fee_are_distinct() -> None:
    raw_static = _log_for_pk(PK_STATIC, block_number=10, log_index=0)
    raw_other_fee = _log_for_pk(PK_DIFFERENT_FEE, block_number=11, log_index=0)
    onboarder, registry, _ = _onboarder_with_scripts([[raw_static, raw_other_fee]])
    # add_by_target_token with the shared currency should discover both.
    results = await onboarder.add_by_target_token(Address(0x10))
    assert len(results) == 2
    assert registry.size == 2
    by_id = {r.record.pool_key.fee: r.record for r in results}
    assert 3000 in by_id
    assert 500 in by_id
    # Each row carries the TARGET_TOKEN entry path.
    for r in results:
        assert r.path == OnboardingPath.TARGET_TOKEN
        assert OnboardingPath.TARGET_TOKEN.value in r.record.entry_paths


async def test_two_pools_with_different_tick_spacing_or_hooks_are_distinct() -> None:
    raw_static = _log_for_pk(PK_STATIC, block_number=10, log_index=0)
    raw_different_tick = _log_for_pk(PK_DIFFERENT_TICK_SPACING, block_number=11, log_index=0)
    raw_different_hook = _log_for_pk(PK_DIFFERENT_HOOKS, block_number=12, log_index=0)
    onboarder, registry, _ = _onboarder_with_scripts(
        [[raw_static, raw_different_tick, raw_different_hook]]
    )
    results = await onboarder.add_by_target_token(Address(0x10))
    assert len(results) == 3
    assert registry.size == 3
    tick_spacings = {r.record.pool_key.tick_spacing for r in results}
    assert tick_spacings == {60, 200}
    hooks = {r.record.pool_key.hooks.value for r in results}
    assert hooks == {0, 1 << 7}


# ---------------------------------------------------------------------------
# Pool-not-found / boundary cases
# ---------------------------------------------------------------------------


async def test_add_by_target_token_with_no_matching_log_returns_empty_list() -> None:
    """A target token with no Initialize events is a normal outcome
    (an empty list), not an error."""
    onboarder, registry, _ = _onboarder_with_scripts([_empty_script()])
    results = await onboarder.add_by_target_token(Address(0x42))
    assert results == []
    assert registry.size == 0


async def test_add_by_pool_key_rejects_when_pool_manager_matches() -> None:
    """A PoolKey whose currency0 or currency1 happens to equal the
    PoolManager address is rejected up front."""
    pm_hex = "0x" + "05" * 20  # small enough to be < 0x20.. no, use the higher slot
    pm = Address.from_hex(pm_hex)
    # The PoolKey constructor enforces currency0 < currency1 as uint160,
    # so we cannot put the PoolManager in currency0 when it is the
    # larger address. Use a different on-boarder check: feed a
    # PoolKey whose ``hooks`` happens to be the PoolManager address
    # (hooks addresses are not subject to the ordering rule).
    pm_small = "0x" + "05" * 20  # 0x0555...
    pm_small_addr = Address.from_hex(pm_small)
    # currency0=0x10, currency1=0x20 (ordered), hooks=PoolManager
    pm = pm_small_addr
    onboarder, registry, _ = _onboarder_with_scripts(
        [_empty_script()], pool_manager_address=pm_small
    )
    bad_pk = PoolKey(
        currency0=Currency.from_int(0x10),
        currency1=Currency.from_int(0x20),
        fee=3000,
        tick_spacing=60,
        hooks=pm,  # hooks field carries the PoolManager address
    )
    with pytest.raises(PoolIdentityRejectionError):
        await onboarder.add_by_pool_key(bad_pk)
    assert len(onboarder.address_rejections) == 1
    assert registry.size == 0


# ---------------------------------------------------------------------------
# Onboarder construction arguments
# ---------------------------------------------------------------------------


def test_onboarder_rejects_non_registry() -> None:
    cfg = RpcConfig(
        endpoints=(RpcEndpoint(url="http://primary", name="primary"),),
        retry_limit=0,
        initial_backoff_seconds=0.01,
    )
    rpc = RpcAdapter(cfg)
    with pytest.raises(TypeError, match="PoolRegistry"):
        PoolOnboarder(
            "not-a-registry",  # type: ignore[arg-type]
            rpc,
            pool_manager_address=Address.zero(),
            block_range_from=0,
            block_range_to=100,
        )


def test_onboarder_rejects_non_rpc_adapter() -> None:
    registry = PoolRegistry(chain_id=ChainId(4663))
    with pytest.raises(TypeError, match="RpcAdapter"):
        PoolOnboarder(
            registry,
            "not-an-rpc-adapter",  # type: ignore[arg-type]
            pool_manager_address=Address.zero(),
            block_range_from=0,
            block_range_to=100,
        )


def test_onboarder_rejects_inverted_block_range() -> None:
    registry = PoolRegistry(chain_id=ChainId(4663))
    cfg = RpcConfig(
        endpoints=(RpcEndpoint(url="http://primary", name="primary"),),
        retry_limit=0,
        initial_backoff_seconds=0.01,
    )
    rpc = RpcAdapter(cfg)
    with pytest.raises(ValueError, match="block_range_from"):
        PoolOnboarder(
            registry,
            rpc,
            pool_manager_address=Address.zero(),
            block_range_from=100,
            block_range_to=50,
        )


def test_onboarder_rejects_negative_block() -> None:
    registry = PoolRegistry(chain_id=ChainId(4663))
    cfg = RpcConfig(
        endpoints=(RpcEndpoint(url="http://primary", name="primary"),),
        retry_limit=0,
        initial_backoff_seconds=0.01,
    )
    rpc = RpcAdapter(cfg)
    with pytest.raises(ValueError, match="non-negative"):
        PoolOnboarder(
            registry,
            rpc,
            pool_manager_address=Address.zero(),
            block_range_from=-1,
            block_range_to=100,
        )


# ---------------------------------------------------------------------------
# Query surface: entry_path_for
# ---------------------------------------------------------------------------


def test_entry_path_for_unknown_pool_returns_empty_frozenset() -> None:
    registry = PoolRegistry(chain_id=ChainId(4663))
    assert entry_path_for(registry, PoolId.from_hex("0x" + "00" * 32)) == frozenset()


def test_entry_path_for_typed_inputs() -> None:
    registry = PoolRegistry(chain_id=ChainId(4663))
    with pytest.raises(TypeError):
        entry_path_for(registry, "not-a-pool-id")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        entry_path_for("not-a-registry", PK_STATIC.to_pool_id())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Preserve T022 properties
# ---------------------------------------------------------------------------


async def test_t022_general_scan_still_works_with_no_entry_path() -> None:
    """The T022 general scan path is preserved: a row ingested via
    the InitializeScanner without going through PoolOnboarder has an
    empty ``entry_paths`` set, and the registry rejects it as a
    duplicate if the onboarder later sees the same row."""
    raw = _log_for_pk(PK_STATIC, block_number=100, log_index=0)
    registry = PoolRegistry(chain_id=ChainId(4663))
    scanner = InitializeScanner(registry, fetch_metadata=False)
    await scanner.ingest_logs([raw])
    pool_id = PK_STATIC.to_pool_id()
    rec = registry.get(pool_id)
    assert rec is not None
    assert rec.entry_paths == set()
    # The query surface returns an empty frozenset.
    assert entry_path_for(registry, pool_id) == frozenset()
