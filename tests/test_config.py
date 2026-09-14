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

import datetime as _dt
import textwrap
from pathlib import Path

import pytest

from robinhood_lp.config import (
    ChainConfig,
    ConfigError,
    LiveApproval,
    PoolConfig,
    PoolKey,
    ProjectRisk,
    RootConfig,
    RunMode,
    SignerConfig,
    TargetTokenConfig,
    TechnicalEligibility,
    UserDecision,
    load_config,
)
from robinhood_lp.config.secrets import (
    contains_credential_url,
    looks_like_secret_env_name,
    redact,
    redact_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "config"

#: A valid LiveApproval record used by tests that exercise the live path.
#: The values are placeholders; the contract only checks shape, scope, and
#: cross-field consistency, not the operator's real identity.
_LIVE_APPROVAL: LiveApproval = LiveApproval(
    approved_by="test-operator",
    approved_at=_dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_dt.UTC),
    approved_scope="mainnet:test-pool:capped-1USDG",
    evidence_ref="todo/evidence/legacy/2026-09-14/T002-fixture.toml",
)


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
    assert cfg.target_token.live is None
    assert cfg.target_token.is_live_eligible is False


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
    assert tt.live is None
    assert tt.is_live_eligible is False


def test_target_token_blocks_live_when_blocked() -> None:
    """technical_eligibility=BLOCKED cannot coexist with a populated live block (ADM-TECH-005)."""
    with pytest.raises(ValueError, match="BLOCKED"):
        TargetTokenConfig(
            chain_id=46630,
            contract_address="0x0000000000000000000000000000000000000100",
            technical_eligibility=TechnicalEligibility.BLOCKED,
            live=_LIVE_APPROVAL,
        )


def test_target_token_blocks_live_when_rejected_or_revoked() -> None:
    """user_decision in {REJECTED, REVOKED} cannot coexist with a populated live block."""
    for decision in (UserDecision.REJECTED, UserDecision.REVOKED):
        with pytest.raises(ValueError, match=decision.value):
            TargetTokenConfig(
                chain_id=46630,
                contract_address="0x0000000000000000000000000000000000000100",
                user_decision=decision,
                live=_LIVE_APPROVAL,
            )


def test_target_token_live_eligible_requires_full_approval_path() -> None:
    """A populated live block is allowed only when eligible, decision is APPROVED*."""
    tt = TargetTokenConfig(
        chain_id=46630,
        contract_address="0x0000000000000000000000000000000000000100",
        technical_eligibility=TechnicalEligibility.ELIGIBLE,
        project_risk=ProjectRisk.HIGH,
        user_decision=UserDecision.APPROVED_WITH_LIMITS,
        live=_LIVE_APPROVAL,
    )
    assert tt.live is not None
    assert tt.is_live_eligible is True
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


# ---------------------------------------------------------------------------
# T002 V1 gap closures: live promotion binding, signer secret reference,
# duplicate identities, secret-free serialization, and credential-URL
# redaction at the loader boundary.
# ---------------------------------------------------------------------------


def test_valid_v1_fixture_exposes_expected_fields() -> None:
    """The shipped V1 fixture round-trips every field the contract names."""
    cfg = load_config(FIXTURES / "valid_robinhood.toml", check_env=False)

    chain = cfg.chains[0]
    assert chain.chain_id == 46630
    assert chain.rpc_url_env == "ROBINHOOD_CHAIN_RPC_URL"
    assert chain.confirmations == 12
    assert chain.start_block == 1
    assert chain.pool_manager_address == "0x0000000000000000000000000000000000000001"
    assert chain.state_view_address == "0x0000000000000000000000000000000000000002"
    assert chain.request_timeout_seconds == 30
    assert chain.max_block_range_per_request == 10_000

    assert cfg.target_token is not None
    assert cfg.target_token.contract_address == "0x0000000000000000000000000000000000000100"
    assert cfg.target_token.symbol == "TARGET"
    assert cfg.target_token.decimals == 18
    assert cfg.target_token.technical_eligibility == TechnicalEligibility.ELIGIBLE
    assert cfg.target_token.project_risk == ProjectRisk.MEDIUM
    assert cfg.target_token.user_decision == UserDecision.APPROVED_WITH_LIMITS
    assert cfg.target_token.live is None

    pool = cfg.pools[0]
    assert pool.chain_id == 46630
    assert pool.pool_key.currency0 == "0x0000000000000000000000000000000000000100"
    assert pool.pool_key.currency1 == "0x0000000000000000000000000000000000000200"
    assert pool.pool_key.fee == 3000
    assert pool.pool_key.tick_spacing == 60
    assert pool.pool_key.hooks == "0x0000000000000000000000000000000000000000"
    assert pool.support_level == RunMode.PAPER

    assert cfg.default_run_mode == RunMode.PAPER
    assert cfg.signer is None


def test_root_config_rejects_unknown_field() -> None:
    """A stray key on RootConfig is refused by the strict model."""
    payload = {
        "chains": [
            {
                "chain_id": 46630,
                "rpc_url_env": "ROBINHOOD_CHAIN_RPC_URL",
                "pool_manager_address": "0x" + "0" * 40,
                "state_view_address": "0x" + "0" * 40,
            }
        ],
        "extra_root_field": 1,
    }
    with pytest.raises(ValueError, match="Extra inputs"):
        RootConfig.model_validate(payload)


def test_root_config_rejects_duplicate_pool_identities(tmp_path: Path) -> None:
    """Two pools with identical PoolKey tuples are rejected (V1 single-pool rule).

    In V1 the structural single-pool rule already forbids two pools from
    coexisting, so submitting the same PoolKey twice fails via that path
    *before* a dedicated duplicate-identity check would fire. Either
    rejection is acceptable; we assert the loader refuses the config.
    """
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [[pools]]
    chain_id = 46630
    [pools.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60

    [[pools]]
    chain_id = 46630
    [pools.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "dup.toml", body)
    with pytest.raises(ConfigError, match=r"(at most one active PoolConfig|duplicate)"):
        load_config(p, check_env=False)


def test_root_config_rejects_chain_id_mismatch_on_pool(tmp_path: Path) -> None:
    """A pool whose chain_id is not in ``chains`` is rejected."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [[pools]]
    chain_id = 1
    [pools.pool_key]
    currency0 = "0x0000000000000000000000000000000000000010"
    currency1 = "0x0000000000000000000000000000000000000020"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "wrong_chain.toml", body)
    with pytest.raises(ConfigError, match="chain_id=1"):
        load_config(p, check_env=False)


def test_loader_redacts_credential_url_in_validation_error(tmp_path: Path) -> None:
    """A pydantic validation error must not echo a credential substring.

    The contract says literal credential URLs cannot enter the system.
    Even if a future field accepts the URL string and rejects it on a
    different validation rule (e.g. address format), the error message
    surfaced by the loader must already be scrubbed of the credential.
    """
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "https://user:supersecret@example.com/x/0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"
    """
    p = _write(tmp_path, "leak.toml", body)
    with pytest.raises(ConfigError) as excinfo:
        load_config(p, check_env=False)
    msg = str(excinfo.value)
    assert "supersecret" not in msg, f"credential substring leaked through loader: {msg!r}"
    assert "user:supersecret" not in msg, f"credential substring leaked through loader: {msg!r}"


def test_loader_redacts_bearer_and_kv_secrets_in_error(tmp_path: Path) -> None:
    """The loader's redactor scrubs ``Bearer`` tokens and ``key=value`` secrets
    that pydantic might echo into ValidationError messages."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
    state_view_address = "password=hunter2abc"
    """
    p = _write(tmp_path, "bearer.toml", body)
    with pytest.raises(ConfigError) as excinfo:
        load_config(p, check_env=False)
    msg = str(excinfo.value)
    assert "hunter2abc" not in msg, f"password value leaked through loader: {msg!r}"
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in msg, (
        f"Bearer token leaked through loader: {msg!r}"
    )


def test_root_config_serialization_never_includes_secret_values() -> None:
    """model_dump / model_dump_json / str / repr must not contain any secret.

    The contract stores RPC URLs and Keystore paths by env-var name, so
    serialization is safe by construction. This test pins that property
    against future regressions (e.g. someone adding a literal password
    field).
    """
    cfg = load_config(FIXTURES / "valid_robinhood.toml", check_env=False)

    secret_substrings = (
        "supersecret",
        "hunter2",
        "password=",
        "Bearer ",
        "0xdeadbeef" + "deadbeef" * 7,  # 64 hex chars
        "https://user:",
        "LP_KEYSTORE_PASSWORD",
    )
    rendered = (
        cfg.model_dump_json()
        + "\n---\n"
        + str(cfg.model_dump())
        + "\n---\n"
        + repr(cfg)
        + "\n---\n"
        + str(cfg)
    )
    for needle in secret_substrings:
        assert needle not in rendered, f"serialization leaked {needle!r} in: {rendered[:200]!r}"

    # The env-var *name* (not value) is allowed.
    assert "ROBINHOOD_CHAIN_RPC_URL" in rendered


def test_pool_key_serialization_uses_canonical_lowercase() -> None:
    """PoolKey stores addresses canonically; serialization is reproducible."""
    pk = PoolKey(
        currency0="0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        currency1="0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        fee=3000,
        tick_spacing=60,
    )
    s = pk.model_dump_json()
    assert "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in s
    assert "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in s
    # No checksum-mixed form is allowed.
    assert "0xAAAA" not in s


# ---------------------------------------------------------------------------
# G-LIVE-GATE-01: live mode is bound to a populated LiveApproval block.
# ---------------------------------------------------------------------------


def test_live_approval_requires_all_fields() -> None:
    # ``approved_by`` is the required field under test. We pass an empty
    # string so Pydantic's ``min_length=1`` validator rejects it; the
    # resulting ``ValidationError`` mentions ``approved_by`` and the test
    # asserts the matching ``ValueError`` is raised.
    with pytest.raises(ValueError, match="approved_by"):
        LiveApproval(
            approved_by="",
            approved_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.UTC),
            approved_scope="scope",
            evidence_ref="ref",
        )


def test_live_approval_rejects_empty_scope() -> None:
    with pytest.raises(ValueError, match="approved_scope"):
        LiveApproval(
            approved_by="op",
            approved_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.UTC),
            approved_scope="",
            evidence_ref="ref",
        )


def test_root_config_rejects_live_pool_without_approval_block(tmp_path: Path) -> None:
    """support_level='live' on a pool without target_token.live is rejected."""
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
    support_level = "live"
    notes = "intends live without approval block"
    [pools.pool_key]
    currency0 = "0x0000000000000000000000000000000000000100"
    currency1 = "0x0000000000000000000000000000000000000200"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "live_no_approval.toml", body)
    with pytest.raises(ConfigError, match="live"):
        load_config(p, check_env=False)


def test_root_config_accepts_live_pool_with_full_approval_block(tmp_path: Path) -> None:
    """support_level='live' is accepted when target_token.live is populated
    AND the signer reference is provided."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [target_token]
    chain_id = 46630
    contract_address = "0x0000000000000000000000000000000000000100"
    technical_eligibility = "eligible"
    user_decision = "approved_with_limits"
    [target_token.live]
    approved_by = "ops-lead"
    approved_at = 2026-01-01T00:00:00Z
    approved_scope = "mainnet:test-pool:capped-1USDG"
    evidence_ref = "todo/evidence/legacy/2026-09-14/T002.toml"

    [[pools]]
    chain_id = 46630
    support_level = "live"
    notes = "live pool approved via G-LIVE-GATE-01"
    [pools.pool_key]
    currency0 = "0x0000000000000000000000000000000000000100"
    currency1 = "0x0000000000000000000000000000000000000200"
    fee = 3000
    tick_spacing = 60

    [signer]
    keystore_path_env = "LP_KEYSTORE_PATH"
    """
    p = _write(tmp_path, "live_full.toml", body)
    cfg = load_config(p, check_env=False)
    assert cfg.pools[0].support_level == RunMode.LIVE
    assert cfg.target_token is not None
    assert cfg.target_token.live is not None
    assert cfg.target_token.live.approved_by == "ops-lead"
    assert cfg.signer is not None
    assert cfg.signer.keystore_path_env == "LP_KEYSTORE_PATH"


def test_root_config_rejects_live_pool_without_signer(tmp_path: Path) -> None:
    """Even with a live approval block, a missing signer reference rejects live."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [target_token]
    chain_id = 46630
    contract_address = "0x0000000000000000000000000000000000000100"
    technical_eligibility = "eligible"
    user_decision = "approved_with_limits"
    [target_token.live]
    approved_by = "ops-lead"
    approved_at = 2026-01-01T00:00:00Z
    approved_scope = "mainnet:test-pool:capped-1USDG"
    evidence_ref = "todo/evidence/legacy/2026-09-14/T002.toml"

    [[pools]]
    chain_id = 46630
    support_level = "live"
    notes = "no signer"
    [pools.pool_key]
    currency0 = "0x0000000000000000000000000000000000000100"
    currency1 = "0x0000000000000000000000000000000000000200"
    fee = 3000
    tick_spacing = 60
    """
    p = _write(tmp_path, "live_no_signer.toml", body)
    with pytest.raises(ConfigError, match="signer"):
        load_config(p, check_env=False)


# ---------------------------------------------------------------------------
# G-SIGNER-01: Keystore reference is env-var-name only; passwords are
# never carried in config.
# ---------------------------------------------------------------------------


def test_signer_config_requires_keystore_env_name() -> None:
    """The signer block stores an env-var name, not a path or password."""
    sc = SignerConfig(keystore_path_env="LP_KEYSTORE_PATH")
    assert sc.keystore_path_env == "LP_KEYSTORE_PATH"


def test_signer_config_rejects_lowercase_env_name() -> None:
    with pytest.raises(ValueError, match="UPPER_SNAKE_CASE"):
        SignerConfig(keystore_path_env="keystore_path")


def test_signer_config_rejects_credential_url_as_env_name() -> None:
    """An env-var name that *looks* like a credential URL is refused."""
    with pytest.raises(ValueError, match="credential-bearing URL"):
        SignerConfig(keystore_path_env="https://user:pw@host/path")


def test_root_config_rejects_password_field_in_signer(tmp_path: Path) -> None:
    """A literal ``keystore_password`` field is rejected as unknown."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [signer]
    keystore_path_env = "LP_KEYSTORE_PATH"
    keystore_password = "hunter2"
    """
    p = _write(tmp_path, "pw.toml", body)
    with pytest.raises(ConfigError, match="Extra inputs"):
        load_config(p, check_env=False)


def test_root_config_rejects_literal_keystore_path(tmp_path: Path) -> None:
    """A literal filesystem path is not a valid ``keystore_path_env`` value
    because the field requires an UPPER_SNAKE_CASE env-var name."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"

    [signer]
    keystore_path_env = "/home/user/.config/keystore.json"
    """
    p = _write(tmp_path, "literal_path.toml", body)
    with pytest.raises(ConfigError, match="UPPER_SNAKE_CASE"):
        load_config(p, check_env=False)


# ---------------------------------------------------------------------------
# Redaction helper (free-form text scrubbing)
# ---------------------------------------------------------------------------


def test_redact_text_masks_credential_url() -> None:
    out = redact_text("Value error, got 'https://user:pw@example.com/x'")
    assert "user:pw" not in out
    assert "<redacted" in out


def test_redact_text_masks_bearer_token() -> None:
    out = redact_text("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert "Bearer <redacted>" in out


def test_redact_text_masks_kv_secrets() -> None:
    out = redact_text("ctx: password=hunter2 next")
    assert "hunter2" not in out
    assert "password=<redacted>" in out


def test_redact_text_masks_64hex_secret() -> None:
    secret = "0x" + "deadbeef" * 8  # 64 hex chars
    out = redact_text(f"trace: input_value={secret}")
    assert secret not in out
    assert "<redacted: hex secret>" in out


def test_redact_text_passes_through_safe_strings() -> None:
    safe = "chains[0].pool_manager_address must match 0x[0-9a-fA-F]{40}"
    assert redact_text(safe) == safe


# ---------------------------------------------------------------------------
# Per-task V1 invariants: default run mode, "latest" policy, no auto-correct.
# ---------------------------------------------------------------------------


def test_root_config_rejects_latest_start_block(tmp_path: Path) -> None:
    """``start_block`` cannot be set to a sentinel like "latest"."""
    body = """
    [[chains]]
    chain_id = 46630
    rpc_url_env = "ROBINHOOD_CHAIN_RPC_URL"
    pool_manager_address = "0x0000000000000000000000000000000000000001"
    state_view_address = "0x0000000000000000000000000000000000000002"
    start_block = -1
    """
    p = _write(tmp_path, "latest.toml", body)
    with pytest.raises(ConfigError, match="start_block"):
        load_config(p, check_env=False)


def test_chain_config_does_not_auto_correct_address_case() -> None:
    """Addresses are canonicalized to lowercase but never silently replaced."""
    chain = ChainConfig(
        chain_id=46630,
        rpc_url_env="ROBINHOOD_CHAIN_RPC_URL",
        pool_manager_address="0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        state_view_address="0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
    )
    # The contract canonicalizes the input but does not coerce a different
    # address or substitute defaults.
    assert chain.pool_manager_address == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert chain.state_view_address == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
