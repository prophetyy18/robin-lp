"""Unit tests for the typed configuration layer (T002).

V1 scope (ADR-005 / docs/intent/PROJECT_GOALS.md):

- exactly one ChainConfig
- zero or one PoolConfig
- zero or one TargetTokenConfig; when set, it must appear in the active pool

Coverage:

- normal: V1 single-chain, single-pool, single-target-token config loads
- boundary: maximum tick spacing, maximum static fee, dynamic-fee sentinel
- invalid: malformed addresses, currency ordering, hook flag mismatch,
  V1 structural violations, target token not in pool, credential-shaped URLs,
  env-var name format
- secrets: redaction and credential detection
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from robinhood_lp.config import (
    ChainConfig,
    ConfigError,
    PoolConfig,
    PoolKey,
    ProjectRisk,
    RootConfig,
    RunMode,
    TargetTokenConfig,
    TechnicalEligibility,
    UserDecision,
    load_config,
)
from robinhood_lp.config.secrets import (
    contains_credential_url,
    looks_like_secret_env_name,
    redact,
)

FIXTURES = Path(__file__).parent / "fixtures" / "config"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return p


# ---------------------------------------------------------------------------
# Valid V1 fixture round-trip
# ---------------------------------------------------------------------------


def test_valid_v1_fixture_loads() -> None:
    """The shipped V1 fixture loads without error."""
    cfg = load_config(FIXTURES / "valid_robinhood.toml", check_env=False)
    assert isinstance(cfg, RootConfig)
    assert len(cfg.chains) == 1
    assert len(cfg.pools) == 1
    assert cfg.target_token is not None
    assert cfg.target_token.user_decision == UserDecision.APPROVED_WITH_LIMITS
    assert cfg.target_token.technical_eligibility == TechnicalEligibility.ELIGIBLE
    assert cfg.target_token.live_eligible is False


def test_default_run_mode_is_paper() -> None:
    """If unset, default_run_mode must default to 'paper', never 'live'."""
    cfg = load_config(FIXTURES / "valid_robinhood.toml", check_env=False)
    assert cfg.default_run_mode == RunMode.PAPER


def test_minimal_root_config_with_no_pool_loads(tmp_path: Path) -> None:
    """V1 permits zero pools when target_token is also unset (early state)."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"
    """
    p = _write(tmp_path, "minimal.toml", body)
    cfg = load_config(p, check_env=False)
    assert len(cfg.chains) == 1
    assert cfg.pools == []
    assert cfg.target_token is None


# ---------------------------------------------------------------------------
# PoolKey validation (unchanged from V3 contract rules)
# ---------------------------------------------------------------------------


def test_pool_key_currency_ordering_is_enforced() -> None:
    """currency0 must be strictly less than currency1 (uint160)."""
    with pytest.raises(ValueError, match="strictly less"):
        PoolKey(
            currency0="0x0000000000000000000000000000000000000020",
            currency1="0x0000000000000000000000000000000000000010",
            fee=3000,
            tick_spacing=60,
        )


def test_pool_key_rejects_dynamic_fee_with_zero_address() -> None:
    """The V4 rule forbids dynamic-fee + hooks=address(0)."""
    with pytest.raises(ValueError, match="DYNAMIC_FEE_FLAG"):
        PoolKey(
            currency0="0x0000000000000000000000000000000000000010",
            currency1="0x0000000000000000000000000000000000000020",
            fee=0x800000,
            tick_spacing=60,
        )


def test_pool_key_rejects_delta_flag_without_action_flag() -> None:
    """Setting AFTER_SWAP_RETURNS_DELTA without AFTER_SWAP is invalid."""
    # Bit 2 (AFTER_SWAP_RETURNS_DELTA) set, bit 6 (AFTER_SWAP) clear.
    bad_hook_int = 1 << 2
    bad_hook = f"0x{bad_hook_int:040x}"
    with pytest.raises(ValueError, match="delta flag"):
        PoolKey(
            currency0="0x0000000000000000000000000000000000000010",
            currency1="0x0000000000000000000000000000000000000020",
            fee=3000,
            tick_spacing=60,
            hooks=bad_hook,
        )


def test_pool_key_accepts_static_fee_with_zero_hook() -> None:
    """Zero hook + static fee is allowed."""
    pk = PoolKey(
        currency0="0x0000000000000000000000000000000000000010",
        currency1="0x0000000000000000000000000000000000000020",
        fee=3000,
        tick_spacing=60,
    )
    assert pk.hooks == "0x0000000000000000000000000000000000000000"


def test_pool_key_accepts_max_static_fee() -> None:
    """MAX_LP_FEE = 1_000_000 is the upper bound for static fees."""
    pk = PoolKey(
        currency0="0x0000000000000000000000000000000000000010",
        currency1="0x0000000000000000000000000000000000000020",
        fee=1_000_000,
        tick_spacing=60,
    )
    assert pk.fee == 1_000_000


def test_pool_key_rejects_fee_above_max_and_not_sentinel() -> None:
    """fee=1_000_001 is not a valid static fee and not the sentinel."""
    with pytest.raises(ValueError, match="invalid"):
        PoolKey(
            currency0="0x0000000000000000000000000000000000000010",
            currency1="0x0000000000000000000000000000000000000020",
            fee=1_000_001,
            tick_spacing=60,
        )


def test_pool_key_accepts_max_tick_spacing() -> None:
    pk = PoolKey(
        currency0="0x0000000000000000000000000000000000000010",
        currency1="0x0000000000000000000000000000000000000020",
        fee=3000,
        tick_spacing=32_767,
    )
    assert pk.tick_spacing == 32_767


def test_pool_key_rejects_zero_tick_spacing() -> None:
    with pytest.raises(ValueError):
        PoolKey(
            currency0="0x0000000000000000000000000000000000000010",
            currency1="0x0000000000000000000000000000000000000020",
            fee=3000,
            tick_spacing=0,
        )


def test_pool_key_native_currency_is_zero_address() -> None:
    """The zero address represents native currency (per V4 Currency.sol)."""
    pk = PoolKey(
        currency0="0x0000000000000000000000000000000000000000",
        currency1="0x0000000000000000000000000000000000000010",
        fee=3000,
        tick_spacing=60,
    )
    assert pk.currency0 == "0x0000000000000000000000000000000000000000"


def test_pool_key_rejects_malformed_address() -> None:
    with pytest.raises(ValueError, match="0x"):
        PoolKey(
            currency0="not-an-address",
            currency1="0x0000000000000000000000000000000000000020",
            fee=3000,
            tick_spacing=60,
        )


def test_pool_key_rejects_unknown_field() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        PoolKey.model_validate(
            {
                "currency0": "0x0000000000000000000000000000000000000010",
                "currency1": "0x0000000000000000000000000000000000000020",
                "fee": 3000,
                "tick_spacing": 60,
                "extra": "nope",
            }
        )


# ---------------------------------------------------------------------------
# V1 structural constraints (ADR-005)
# ---------------------------------------------------------------------------


def test_root_config_rejects_two_chains() -> None:
    """V1 admits exactly one ChainConfig."""
    with pytest.raises(ConfigError, match="V1 requires exactly one"):
        load_config(FIXTURES / "invalid_two_chains.toml", check_env=False)


def test_root_config_rejects_two_pools() -> None:
    """V1 admits at most one PoolConfig."""
    with pytest.raises(ConfigError, match="at most one active PoolConfig"):
        load_config(FIXTURES / "invalid_two_pools.toml", check_env=False)


def test_root_config_rejects_target_token_not_in_pool() -> None:
    """The active pool must pair the user-selected target token."""
    with pytest.raises(ConfigError, match="does not contain the target token"):
        load_config(FIXTURES / "invalid_target_token_not_in_pool.toml", check_env=False)


def test_root_config_rejects_target_token_with_unknown_chain(tmp_path: Path) -> None:
    """target_token.chain_id must match a ChainConfig entry."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"
    [target_token]
    chain_id = 1
    contract_address = "0x0000000000000000000000000000000000000100"
    """
    p = _write(tmp_path, "tt_bad_chain.toml", body)
    with pytest.raises(ConfigError, match="target_token.chain_id"):
        load_config(p, check_env=False)


def test_root_config_rejects_live_default_run_mode(tmp_path: Path) -> None:
    body = """
    default_run_mode = "live"
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"
    """
    p = _write(tmp_path, "live_default.toml", body)
    with pytest.raises(ConfigError, match="live"):
        load_config(p, check_env=False)


def test_loader_rejects_credential_url_in_config(tmp_path: Path) -> None:
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "https://user:secret@example.com/0000000000000000000000000000000000000002"
    """
    p = _write(tmp_path, "cred.toml", body)
    with pytest.raises(ConfigError, match="credential-shaped URL"):
        load_config(p, check_env=False)


def test_loader_rejects_malformed_toml(tmp_path: Path) -> None:
    p = tmp_path / "bad.toml"
    p.write_text("this is = = = not toml")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(p, check_env=False)


def test_loader_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.toml", check_env=False)


def test_chain_config_rejects_lowercase_env_name() -> None:
    with pytest.raises(ValueError, match="UPPER_SNAKE_CASE"):
        ChainConfig(
            chain_id=46630,
            rpc_url_env="lowercase",
            pool_manager_address="0x0000000000000000000000000000000000000001",
            state_view_address="0x0000000000000000000000000000000000000002",
        )


# ---------------------------------------------------------------------------
# TargetTokenConfig validation
# ---------------------------------------------------------------------------


def test_target_token_default_approval_state_is_safe() -> None:
    """Defaults are the safest possible state: UNKNOWN / UNKNOWN / PENDING / not live-eligible."""
    tt = TargetTokenConfig(
        chain_id=46630,
        contract_address="0x0000000000000000000000000000000000000100",
    )
    assert tt.technical_eligibility == TechnicalEligibility.UNKNOWN
    assert tt.project_risk == ProjectRisk.UNKNOWN
    assert tt.user_decision == UserDecision.PENDING
    assert tt.live_eligible is False


def test_target_token_blocks_live_when_blocked() -> None:
    """technical_eligibility=BLOCKED cannot coexist with live_eligible=True (ADM-TECH-005)."""
    with pytest.raises(ValueError, match="BLOCKED"):
        TargetTokenConfig(
            chain_id=46630,
            contract_address="0x0000000000000000000000000000000000000100",
            technical_eligibility=TechnicalEligibility.BLOCKED,
            live_eligible=True,
        )


def test_target_token_blocks_live_when_rejected_or_revoked() -> None:
    """user_decision in {REJECTED, REVOKED} cannot coexist with live_eligible=True."""
    for decision in (UserDecision.REJECTED, UserDecision.REVOKED):
        with pytest.raises(ValueError, match=decision.value):
            TargetTokenConfig(
                chain_id=46630,
                contract_address="0x0000000000000000000000000000000000000100",
                user_decision=decision,
                live_eligible=True,
            )


def test_target_token_live_eligible_requires_full_approval_path() -> None:
    """live_eligible=True is allowed only when eligible, decision is APPROVED*."""
    tt = TargetTokenConfig(
        chain_id=46630,
        contract_address="0x0000000000000000000000000000000000000100",
        technical_eligibility=TechnicalEligibility.ELIGIBLE,
        project_risk=ProjectRisk.HIGH,
        user_decision=UserDecision.APPROVED_WITH_LIMITS,
        live_eligible=True,
    )
    assert tt.live_eligible is True
    assert tt.user_decision == UserDecision.APPROVED_WITH_LIMITS


def test_target_token_rejects_unknown_enum_value() -> None:
    """Unknown strings are rejected by the strict enum types."""
    with pytest.raises(ValueError):
        TargetTokenConfig.model_validate(
            {
                "chain_id": 46630,
                "contract_address": "0x0000000000000000000000000000000000000100",
                "technical_eligibility": "maybe",
            }
        )


def test_target_token_rejects_malformed_address() -> None:
    with pytest.raises(ValueError, match="0x"):
        TargetTokenConfig(
            chain_id=46630,
            contract_address="not-an-address",
        )


# ---------------------------------------------------------------------------
# Secret redaction helpers
# ---------------------------------------------------------------------------


def test_contains_credential_url_matches_known_shapes() -> None:
    assert contains_credential_url("https://user:pw@host/path")
    assert contains_credential_url("http://alice:secret@example.com")
    assert not contains_credential_url("https://example.com/path")
    assert not contains_credential_url("https://user@host/path")  # no password


def test_looks_like_secret_env_name_is_conservative() -> None:
    assert looks_like_secret_env_name("RPC_SECRET_TOKEN")
    assert looks_like_secret_env_name("DB_PASSWORD")
    assert not looks_like_secret_env_name("RPC_URL")


def test_redact_replaces_credential_url() -> None:
    out = redact("https://u:p@example.com")
    assert "<redacted" in out
    assert "u:p" not in out


def test_redact_uses_env_name_when_provided() -> None:
    assert "redacted" in redact("https://example.com", env_name="API_TOKEN")
    assert redact("https://example.com", env_name="RPC_URL") == "https://example.com"


# ---------------------------------------------------------------------------
# PoolConfig smoke
# ---------------------------------------------------------------------------


def test_pool_config_default_support_level_is_ingestion() -> None:
    pc = PoolConfig(
        chain_id=46630,
        pool_key=PoolKey(
            currency0="0x0000000000000000000000000000000000000010",
            currency1="0x0000000000000000000000000000000000000020",
            fee=3000,
            tick_spacing=60,
        ),
    )
    assert pc.support_level == RunMode.INGESTION
