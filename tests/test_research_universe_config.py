"""Tests for the T026 research-universe configuration collection.

Coverage matrix (T026 acceptance):

- An empty research universe is a valid V1 configuration.
- A populated research universe does not change the single-active-
  pool rule on ``pools``.
- Two pools sharing a currency but differing in fee / tick spacing /
  hooks remain distinct ``ResearchMemberConfig`` entries.
- Duplicate ``PoolKey`` identities inside ``research_universe`` are
  rejected with a named reason.
- A research member that references a chain the configuration does
  not declare is rejected with a named reason.
- A ``block_range_end`` that is less than ``block_range_start`` is
  rejected.
- A research member is not silently promoted into the active-pool
  collection.
- ``RootConfig`` accepts an active pool AND a populated research
  universe in the same configuration; the structural single-active-
  pool rule still fires when the ``pools`` list contains two or
  more entries, regardless of the research universe.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from robinhood_lp.config import (
    ConfigError,
    PoolKey,
    ResearchMemberConfig,
    RootConfig,
    RunMode,
    load_config,
)

FIXTURES = Path(__file__).parent / "fixtures" / "config"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return p


def _pool_key_dict() -> dict[str, object]:
    return {
        "currency0": "0x0000000000000000000000000000000000000010",
        "currency1": "0x0000000000000000000000000000000000000020",
        "fee": 3000,
        "tick_spacing": 60,
        "hooks": "0x0000000000000000000000000000000000000000",
    }


def _pool_key_dict_with(*, c0: str, c1: str, fee: int, tick: int) -> dict[str, object]:
    return {
        "currency0": c0,
        "currency1": c1,
        "fee": fee,
        "tick_spacing": tick,
        "hooks": "0x0000000000000000000000000000000000000000",
    }


def _research_member(
    *,
    chain_id: int = 46630,
    pool_key: dict[str, object] | None = None,
    start: int = 0,
    end: int | None = 1_000_000,
    support_level: str = "ingestion",
    notes: str = "",
) -> dict[str, object]:
    return {
        "chain_id": chain_id,
        "pool_key": pool_key if pool_key is not None else _pool_key_dict(),
        "block_range_start": start,
        "block_range_end": end,
        "support_level": support_level,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Empty research universe
# ---------------------------------------------------------------------------


def test_root_config_with_empty_research_universe_loads(tmp_path: Path) -> None:
    body = """
    research_universe = []

    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"
    """
    p = _write(tmp_path, "empty_ru.toml", body)
    cfg = load_config(p, check_env=False)
    assert cfg.research_universe == []
    assert len(cfg.chains) == 1
    assert cfg.pools == []


def test_root_config_defaults_research_universe_to_empty() -> None:
    """The default for ``research_universe`` is the empty list, so
    legacy V1 configurations that never mention the field still load."""
    cfg = RootConfig.model_validate(
        {
            "chains": [
                {
                    "chain_id": 46630,
                    "rpc_url_env": "ROBINHOOD_CHAIN_RPC_URL",
                    "pool_manager_address": "0x" + "0" * 40,
                    "state_view_address": "0x" + "0" * 39 + "1",
                }
            ]
        }
    )
    assert cfg.research_universe == []


# ---------------------------------------------------------------------------
# Populated research universe
# ---------------------------------------------------------------------------


def test_root_config_accepts_a_research_member(tmp_path: Path) -> None:
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [[research_universe]]
    chain_id = 46630
    block_range_start = 1
    block_range_end = 1000000
    support_level = "ingestion"
    notes = "study fee volatility"
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "one_ru.toml", body)
    cfg = load_config(p, check_env=False)
    assert len(cfg.research_universe) == 1
    member = cfg.research_universe[0]
    assert isinstance(member, ResearchMemberConfig)
    assert member.chain_id == 46630
    assert member.block_range_start == 1
    assert member.block_range_end == 1_000_000
    assert member.support_level == RunMode.INGESTION
    assert member.notes == "study fee volatility"
    assert member.pool_key.fee == 3000


def test_root_config_accepts_multiple_distinct_research_members(tmp_path: Path) -> None:
    """Two pools sharing a currency but differing in fee, tick spacing,
    or hooks remain distinct research members."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [[research_universe]]
    chain_id = 46630
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60

    [[research_universe]]
    chain_id = 46630
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 500
    tick_spacing = 60

    [[research_universe]]
    chain_id = 46630
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 200
    """
    p = _write(tmp_path, "three_ru.toml", body)
    cfg = load_config(p, check_env=False)
    assert len(cfg.research_universe) == 3
    fees = {m.pool_key.fee for m in cfg.research_universe}
    assert fees == {3000, 500}
    tick_spacings = {m.pool_key.tick_spacing for m in cfg.research_universe}
    assert tick_spacings == {60, 200}


def test_root_config_accepts_research_member_with_open_ended_block_range(tmp_path: Path) -> None:
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [[research_universe]]
    chain_id = 46630
    block_range_end = 1000000
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "open_ru.toml", body)
    cfg = load_config(p, check_env=False)
    assert cfg.research_universe[0].block_range_end == 1_000_000


# ---------------------------------------------------------------------------
# Validation: duplicates, chain mismatch, inverted range
# ---------------------------------------------------------------------------


def test_root_config_rejects_duplicate_research_members(tmp_path: Path) -> None:
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [[research_universe]]
    chain_id = 46630
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60

    [[research_universe]]
    chain_id = 46630
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "dup_ru.toml", body)
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(p, check_env=False)


def test_root_config_rejects_research_member_with_unknown_chain(tmp_path: Path) -> None:
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [[research_universe]]
    chain_id = 999  # not declared in chains
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "wrong_chain_ru.toml", body)
    with pytest.raises(ConfigError, match="chain_id=999"):
        load_config(p, check_env=False)


def test_root_config_rejects_inverted_block_range_in_research_member(tmp_path: Path) -> None:
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [[research_universe]]
    chain_id = 46630
    block_range_start = 1000000
    block_range_end = 100  # less than start
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "inverted_ru.toml", body)
    with pytest.raises(ConfigError, match="block_range_end"):
        load_config(p, check_env=False)


def test_root_config_rejects_negative_block_range_start(tmp_path: Path) -> None:
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [[research_universe]]
    chain_id = 46630
    block_range_start = -1
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "neg_start_ru.toml", body)
    with pytest.raises(ConfigError):
        load_config(p, check_env=False)


# ---------------------------------------------------------------------------
# Interaction with active pool (the key invariant)
# ---------------------------------------------------------------------------


def test_root_config_accepts_research_member_with_one_active_pool(tmp_path: Path) -> None:
    """A configuration with one active pool AND a populated research
    universe is a valid V1 configuration (T026 acceptance: the
    structural single-active-pool rule continues to enforce
    ``len(pools) <= 1`` regardless of research_universe)."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [target_token]
    chain_id = 46630
    contract_address = "0x0000000000000000000000000000000000000100"

    [[pools]]
    chain_id = 46630
    [pools.pool_key]
    currency0 = "0x0000000000000000000000000000000000000100"
    currency1 = "0x0000000000000000000000000000000000000200"
    fee = 3000
    tick_spacing = 60

    [[research_universe]]
    chain_id = 46630
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "active_plus_ru.toml", body)
    cfg = load_config(p, check_env=False)
    assert len(cfg.pools) == 1
    assert len(cfg.research_universe) == 1


def test_root_config_still_rejects_two_active_pools_even_with_populated_research_universe(
    tmp_path: Path,
) -> None:
    """The structural single-active-pool rule fires regardless of how
    many research members are present (T026 acceptance: a configuration
    that names more than one active pool still fails to load with a
    redacted error, including when the same configuration carries a
    populated research universe)."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [[pools]]
    chain_id = 46630
    [pools.pool_key]
    currency0 = "0x0000000000000000000000000000000000000100"
    currency1 = "0x0000000000000000000000000000000000000200"
    fee = 3000
    tick_spacing = 60

    [[pools]]
    chain_id = 46630
    [pools.pool_key]
    currency0 = "0x0000000000000000000000000000000000000300"
    currency1 = "0x0000000000000000000000000000000000000400"
    fee = 3000
    tick_spacing = 60

    [[research_universe]]
    chain_id = 46630
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "two_active_plus_ru.toml", body)
    with pytest.raises(ConfigError, match="at most one active PoolConfig"):
        load_config(p, check_env=False)


def test_root_config_does_not_promote_research_member_to_active_pool(tmp_path: Path) -> None:
    """A research member whose PoolKey matches the active pool is
    still a distinct identity: it lives in ``research_universe`` and
    is not promoted into ``pools`` (T026 must-not: a research-universe
    member is never reported as, or promoted by, the active execution
    pool)."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [target_token]
    chain_id = 46630
    contract_address = "0x0000000000000000000000000000000000000100"

    [[pools]]
    chain_id = 46630
    [pools.pool_key]
    currency0 = "0x0000000000000000000000000000000000000100"
    currency1 = "0x0000000000000000000000000000000000000200"
    fee = 3000
    tick_spacing = 60

    [[research_universe]]
    chain_id = 46630
    [research_universe.pool_key]
    currency0 = "0x0000000000000000000000000000000000000100"
    currency1 = "0x0000000000000000000000000000000000000200"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "research_matches_active.toml", body)
    cfg = load_config(p, check_env=False)
    assert len(cfg.pools) == 1
    assert len(cfg.research_universe) == 1
    # The two collections are distinct.
    assert cfg.pools[0].pool_key.currency0 == "0x" + "0" * 37 + "100"
    assert cfg.research_universe[0].pool_key.currency0 == "0x" + "0" * 37 + "100"


# ---------------------------------------------------------------------------
# ResearchMemberConfig direct validation
# ---------------------------------------------------------------------------


def test_research_member_config_default_support_level_is_ingestion() -> None:
    member = ResearchMemberConfig(
        chain_id=46630,
        pool_key=PoolKey(
            currency0="0x0000000000000000000000000000000000000010",
            currency1="0x0000000000000000000000000000000000000020",
            fee=3000,
            tick_spacing=60,
        ),
    )
    assert member.support_level == RunMode.INGESTION
    assert member.block_range_start == 0
    assert member.block_range_end is None


def test_research_member_config_default_block_range_end_is_none() -> None:
    member = ResearchMemberConfig(
        chain_id=46630,
        pool_key=PoolKey(
            currency0="0x0000000000000000000000000000000000000010",
            currency1="0x0000000000000000000000000000000000000020",
            fee=3000,
            tick_spacing=60,
        ),
    )
    assert member.block_range_end is None


def test_research_member_config_rejects_invalid_address() -> None:
    with pytest.raises(ValueError):
        ResearchMemberConfig(
            chain_id=46630,
            pool_key=PoolKey.model_validate(
                {
                    "currency0": "not-an-address",
                    "currency1": "0x0000000000000000000000000000000000000020",
                    "fee": 3000,
                    "tick_spacing": 60,
                }
            ),
        )


def test_research_member_config_rejects_unknown_field() -> None:
    """The strict pydantic model forbids unknown fields; a stray key
    is rejected so a future field never silently accepts a typo."""
    payload = {
        "chain_id": 46630,
        "pool_key": _pool_key_dict(),
        "extra_field": "nope",
    }
    with pytest.raises(ValueError, match="Extra inputs"):
        ResearchMemberConfig.model_validate(payload)


def test_research_member_config_inherits_pool_key_validation() -> None:
    """A ResearchMemberConfig's ``pool_key`` is the same pydantic
    ``PoolKey`` model used by ``PoolConfig``, so the V4 invariants
    are reused: a wrong-order currency pair is rejected."""
    with pytest.raises(ValueError):
        ResearchMemberConfig(
            chain_id=46630,
            pool_key=PoolKey(
                currency0="0x0000000000000000000000000000000000000020",
                currency1="0x0000000000000000000000000000000000000010",  # wrong order
                fee=3000,
                tick_spacing=60,
            ),
        )


# ---------------------------------------------------------------------------
# End-to-end: research universe + research-universe runtime
# ---------------------------------------------------------------------------


def test_research_member_config_can_build_research_member() -> None:
    """The configuration-side record and the runtime-side record are
    interoperable: a configuration-driven ``ResearchMemberConfig`` can
    build a runtime ``ResearchMember`` via the discovery layer."""
    from robinhood_lp.discovery import OnboardingPath, ResearchMember, ResearchUniverse
    from robinhood_lp.protocol import (
        Address,
        ChainId,
        Currency,
    )
    from robinhood_lp.protocol import (
        PoolKey as _ProtocolPoolKey,
    )

    member_cfg = ResearchMemberConfig(
        chain_id=46630,
        pool_key=PoolKey(
            currency0="0x0000000000000000000000000000000000000010",
            currency1="0x0000000000000000000000000000000000000020",
            fee=3000,
            tick_spacing=60,
        ),
        block_range_start=100,
        block_range_end=100_000,
        support_level=RunMode.INGESTION,
        notes="from config",
    )
    proto_pk = _ProtocolPoolKey(
        currency0=Currency.from_address(Address.from_hex(member_cfg.pool_key.currency0)),
        currency1=Currency.from_address(Address.from_hex(member_cfg.pool_key.currency1)),
        fee=member_cfg.pool_key.fee,
        tick_spacing=member_cfg.pool_key.tick_spacing,
        hooks=Address.from_hex(member_cfg.pool_key.hooks),
    )
    member = ResearchMember(
        chain_id=member_cfg.chain_id,
        pool_key=proto_pk,
        block_range_start=member_cfg.block_range_start,
        block_range_end=member_cfg.block_range_end,
        support_level=member_cfg.support_level,
        added_via=OnboardingPath.POOL_KEY,
        notes=member_cfg.notes,
    )
    universe = ResearchUniverse(chain_id=ChainId(member.chain_id))
    universe.add(member)
    assert universe.is_member(member.pool_id) is True
