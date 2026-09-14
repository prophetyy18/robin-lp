"""Typed configuration models.

Hard rules (from ADR-003, ADR-005, and ``docs/spec/protocol/PROTOCOL_FACTS.md``):

- models are frozen; unknown fields are rejected;
- chain IDs are positive integers and chain configurations are referenced by
  exact ``chain_id`` integer, not by name or symbol;
- pool identities are full V4 ``PoolKey`` tuples; token symbols are never
  identifiers;
- ``currency0`` is strictly less than ``currency1`` as ``uint160``;
- ``fee`` is either in ``[0, MAX_LP_FEE]`` or exactly the
  ``DYNAMIC_FEE_FLAG`` sentinel;
- hook address validity mirrors the V4 library: address-zero is allowed
  only with a non-dynamic fee; a non-zero hook address requires
  ``(addr & ALL_HOOK_MASK) != 0`` or a dynamic fee; delta-flags require the
  matching action-flag.
- secrets are read from environment variables by name and never appear in
  serialized form.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Protocol constants (pinned — see docs/spec/protocol/PROTOCOL_FACTS.md)
# ---------------------------------------------------------------------------
MAX_LP_FEE: Final[int] = 1_000_000
DYNAMIC_FEE_FLAG: Final[int] = 0x800000
ALL_HOOK_MASK: Final[int] = (1 << 14) - 1  # 0x3FFF

# Hook flag bits (must match the order in Hooks.sol). Bit 0 = LSB.
HOOK_FLAG_BITS: Final[tuple[int, ...]] = (
    1 << 0,  # AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA_FLAG
    1 << 1,  # AFTER_ADD_LIQUIDITY_RETURNS_DELTA_FLAG
    1 << 2,  # AFTER_SWAP_RETURNS_DELTA_FLAG
    1 << 3,  # BEFORE_SWAP_RETURNS_DELTA_FLAG
    1 << 4,  # AFTER_DONATE_FLAG
    1 << 5,  # BEFORE_DONATE_FLAG
    1 << 6,  # AFTER_SWAP_FLAG
    1 << 7,  # BEFORE_SWAP_FLAG
    1 << 8,  # AFTER_REMOVE_LIQUIDITY_FLAG
    1 << 9,  # BEFORE_REMOVE_LIQUIDITY_FLAG
    1 << 10,  # AFTER_ADD_LIQUIDITY_FLAG
    1 << 11,  # BEFORE_ADD_LIQUIDITY_FLAG
    1 << 12,  # AFTER_INITIALIZE_FLAG
    1 << 13,  # BEFORE_INITIALIZE_FLAG
)

# delta-flag -> required action-flag (from isValidHookAddress rule 1).
DELTA_TO_ACTION_FLAG: Final[dict[int, int]] = {
    1 << 3: 1 << 7,  # BEFORE_SWAP_RETURNS_DELTA -> BEFORE_SWAP
    1 << 2: 1 << 6,  # AFTER_SWAP_RETURNS_DELTA  -> AFTER_SWAP
    1 << 1: 1 << 10,  # AFTER_ADD_LIQUIDITY_RETURNS_DELTA -> AFTER_ADD_LIQUIDITY
    1 << 0: 1 << 8,  # AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA -> AFTER_REMOVE_LIQUIDITY
}

#: EIP-55 / EIP-1191 not enforced here; addresses are validated as 20-byte
#: lowercase-or-uppercase hex strings. The chain itself is the source of
#: truth for checksums; we record the bytes and let the caller compare.
_ADDRESS_RE: Final[re.Pattern[str]] = re.compile(r"^0x[0-9a-fA-F]{40}$")

#: detect a credential-shaped URL fragment inside a string (basic; not
#: a substitute for real URL parsing).
_CREDENTIAL_RE: Final[re.Pattern[str]] = re.compile(r"://[^/\s]*:[^/\s@]+@")


def _validate_address(value: str, *, field: str) -> str:
    """Validate an EVM address literal and return its canonical lowercase form."""
    if not isinstance(value, str):
        raise ValueError(f"{field}: address must be a string")
    if not _ADDRESS_RE.match(value):
        raise ValueError(f"{field}: address must match 0x[0-9a-fA-F]{{40}}, got {value!r}")
    return value.lower()


def _address_to_int(value: str, *, field: str) -> int:
    """Parse an address literal to a uint160 integer (native = 0)."""
    canonical = _validate_address(value, field=field)
    return int(canonical, 16)


# ``RunMode`` lives in ``robinhood_lp.protocol`` so the protocol/domain
# layer can use it without depending on the config layer. Re-export
# here so existing config-model imports keep working.
from robinhood_lp.protocol.run_mode import RunMode  # noqa: E402

__all__ = ["RunMode"]  # explicit re-export


class _StrictModel(BaseModel):
    """Shared strict configuration for every config model."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class PoolKey(_StrictModel):
    """Canonical V4 pool key. Identified by all five fields together."""

    currency0: str = Field(
        ...,
        description=(
            "Lower-sorted currency as a 0x-prefixed 20-byte address. Native "
            "currency is the zero address (0x0000...0000)."
        ),
    )
    currency1: str = Field(
        ...,
        description=(
            "Higher-sorted currency as a 0x-prefixed 20-byte address. Must "
            "compare strictly greater than currency0 as uint160."
        ),
    )
    fee: int = Field(
        ...,
        ge=0,
        le=DYNAMIC_FEE_FLAG,
        description=(
            "uint24 LP fee in hundredths of a bip. Must be <= MAX_LP_FEE "
            "(1_000_000) or exactly DYNAMIC_FEE_FLAG (0x800000)."
        ),
    )
    tick_spacing: int = Field(
        ...,
        ge=1,
        le=32_767,
        description="Positive int24 in [1, 32767]; positions must align to this.",
    )
    hooks: str = Field(
        default="0x0000000000000000000000000000000000000000",
        description="IHooks contract address; zero address means no hooks.",
    )

    @field_validator("currency0", "currency1", "hooks")
    @classmethod
    def _check_address_format(cls, v: str) -> str:
        return _validate_address(v, field="address")

    @field_validator("fee")
    @classmethod
    def _check_fee_validity(cls, v: int) -> int:
        if v == DYNAMIC_FEE_FLAG:
            return v
        if v <= MAX_LP_FEE:
            return v
        # Pydantic's le=DYNAMIC_FEE_FLAG already filtered above; anything
        # past MAX_LP_FEE and not the sentinel is invalid per LPFeeLibrary.
        raise ValueError(
            f"fee={v} is invalid: must be in [0, {MAX_LP_FEE}] "
            f"or exactly DYNAMIC_FEE_FLAG ({DYNAMIC_FEE_FLAG:#x})"
        )

    @model_validator(mode="after")
    def _check_currency_ordering_and_hook(self) -> PoolKey:
        c0 = _address_to_int(self.currency0, field="currency0")
        c1 = _address_to_int(self.currency1, field="currency1")
        if c0 >= c1:
            raise ValueError(
                f"currency0 ({self.currency0}) must be strictly less than "
                f"currency1 ({self.currency1}) as uint160"
            )

        # Mirror Hooks.isValidHookAddress(IHooks, uint24 fee).
        hook_int = _address_to_int(self.hooks, field="hooks")
        is_dynamic = self.fee == DYNAMIC_FEE_FLAG

        if hook_int == 0:
            if is_dynamic:
                raise ValueError(
                    "hooks=0x0..0 is not allowed with DYNAMIC_FEE_FLAG; "
                    "a dynamic-fee pool must have a hook address or be "
                    "rejected by the framework"
                )
            return self

        # Non-zero hook: rule 3 — (addr & ALL_HOOK_MASK) != 0 OR dynamic fee.
        if (hook_int & ALL_HOOK_MASK) == 0 and not is_dynamic:
            raise ValueError(
                f"hooks={self.hooks} has no flag bits set in the low 14 bits "
                f"and fee is not dynamic; V4 would reject this pool"
            )

        # Rule 1 — each delta-flag requires its action-flag.
        for delta_flag, action_flag in DELTA_TO_ACTION_FLAG.items():
            if (hook_int & delta_flag) != 0 and (hook_int & action_flag) == 0:
                raise ValueError(
                    f"hooks={self.hooks} sets delta flag {delta_flag:#x} "
                    f"without the required action flag {action_flag:#x}"
                )

        return self


class ChainConfig(_StrictModel):
    """Per-chain connection and contract configuration."""

    chain_id: PositiveInt = Field(
        ...,
        description=(
            "EIP-155 chain ID. Positive integer; names/symbols are not accepted as identifiers."
        ),
    )
    rpc_url_env: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Name of the environment variable that holds the RPC URL. The "
            "URL itself is never written to the config file."
        ),
    )
    confirmations: PositiveInt = Field(
        default=12,
        description="Block confirmations before a block is treated as final.",
    )
    start_block: int = Field(
        default=0,
        ge=0,
        description="First block to ingest; historical ingestion is bounded.",
    )
    pool_manager_address: str = Field(
        ...,
        description="Deployed PoolManager contract address.",
    )
    state_view_address: str = Field(
        ...,
        description="Deployed StateView contract address used for read-only state.",
    )
    request_timeout_seconds: PositiveInt = Field(
        default=30, le=600, description="Per-RPC-call timeout."
    )
    max_block_range_per_request: PositiveInt = Field(
        default=10_000,
        le=100_000,
        description="Upper bound on a single eth_getLogs block range.",
    )

    @field_validator("rpc_url_env")
    @classmethod
    def _check_env_name(cls, v: str) -> str:
        if not re.match(r"^[A-Z][A-Z0-9_]*$", v):
            raise ValueError(f"rpc_url_env={v!r} must be an UPPER_SNAKE_CASE env-var name")
        return v

    @field_validator("pool_manager_address", "state_view_address")
    @classmethod
    def _check_address(cls, v: str) -> str:
        return _validate_address(v, field="contract address")


class TechnicalEligibility(StrEnum):
    """``docs/spec/product/ASSET_ADMISSION.md`` §4.1.

    The framework's ability to correctly handle the token's on-chain
    behavior. Independent from project risk and user decision.
    """

    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    CONDITIONAL = "conditional"
    ELIGIBLE = "eligible"


class ProjectRisk(StrEnum):
    """``docs/spec/product/ASSET_ADMISSION.md`` §4.2.

    Honest classification of project-level risk. Carries no auto-veto
    power; the user may accept any level subject to ADM-TECH-* hard
    gates.
    """

    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class UserDecision(StrEnum):
    """``docs/spec/product/ASSET_ADMISSION.md`` §4.3.

    The user's recorded stance on the displayed project risk. Lives
    forever in the audit trail; revocation always permitted.
    """

    PENDING = "pending"
    APPROVED = "approved"
    APPROVED_WITH_LIMITS = "approved_with_limits"
    REJECTED = "rejected"
    REVOKED = "revoked"


class TargetTokenConfig(_StrictModel):
    """The single target token V1 operates on (ADR-005 / PROJECT_GOALS §4).

    Identified by Robinhood Chain contract address; symbol is display only.
    Carries the three-track approval state from
    ``docs/spec/product/ASSET_ADMISSION.md`` §4: technical eligibility, project
    risk, and user decision. The framework's risk gateway (T070) refuses
    live intents unless the triple is on the accepted path AND the
    ``live_eligible`` flag is True.
    """

    chain_id: PositiveInt = Field(
        ...,
        description=(
            "Robinhood Chain ID on which the target token is deployed. "
            "Must match exactly one ChainConfig entry (V1 single-chain rule)."
        ),
    )
    contract_address: str = Field(
        ...,
        description="Target token contract address on Robinhood Chain.",
    )
    symbol: str = Field(
        default="",
        max_length=32,
        description="Display-only token symbol; never used as an identifier.",
    )
    decimals: int | None = Field(
        default=None,
        ge=0,
        le=255,
        description="Token decimals; populated on first read from chain.",
    )

    # ---- ASSET_ADMISSION §4 dual-track approval --------------------------

    technical_eligibility: TechnicalEligibility = Field(
        default=TechnicalEligibility.UNKNOWN,
        description=(
            "Whether the framework can correctly model this token's "
            "on-chain behavior. BLOCKED is an absolute veto (ADM-TECH-*)."
        ),
    )
    project_risk: ProjectRisk = Field(
        default=ProjectRisk.UNKNOWN,
        description=(
            "Honest classification of project-level risk. Display-only; "
            "the user (not the framework) decides whether to accept it."
        ),
    )
    user_decision: UserDecision = Field(
        default=UserDecision.PENDING,
        description=(
            "The user's recorded stance. PENDING blocks paper; REJECTED "
            "and REVOKED block everything; APPROVED and "
            "APPROVED_WITH_LIMITS permit paper/live subject to other gates."
        ),
    )
    live_eligible: bool = Field(
        default=False,
        description=(
            "True only after the V1 promotion gates (backtest → testnet → "
            "paper → security review → human promotion; G-LIVE-GATE-01) "
            "have all been satisfied. The signer process (G-SIGNER-01, "
            "Phase 9 T090) refuses intents when this is False."
        ),
    )

    decision_notes: str = Field(
        default="",
        max_length=512,
        description="Free-form note recorded with the approval decision.",
    )

    @field_validator("contract_address")
    @classmethod
    def _check_address(cls, v: str) -> str:
        return _validate_address(v, field="contract_address")

    @model_validator(mode="after")
    def _check_blocked_cannot_be_live_eligible(self) -> TargetTokenConfig:
        if self.technical_eligibility == TechnicalEligibility.BLOCKED and self.live_eligible:
            raise ValueError(
                "technical_eligibility=BLOCKED is an absolute veto; "
                "live_eligible cannot be True (ADM-TECH-005/007)"
            )
        return self

    @model_validator(mode="after")
    def _check_rejected_or_revoked_blocks_live(self) -> TargetTokenConfig:
        if (
            self.user_decision in (UserDecision.REJECTED, UserDecision.REVOKED)
            and self.live_eligible
        ):
            raise ValueError(
                f"user_decision={self.user_decision.value} blocks live "
                f"execution; live_eligible must be False"
            )
        return self


class PoolConfig(_StrictModel):
    """A pool registered for the framework, scoped to a single chain.

    V1 admits zero or one active pool. The pool's ``currency0`` or
    ``currency1`` must equal the target token's contract address; this is
    enforced by :class:`RootConfig`.
    """

    chain_id: PositiveInt = Field(
        ...,
        description="Chain on which the pool is deployed; must match ChainConfig.",
    )
    pool_key: PoolKey = Field(..., description="Full V4 PoolKey tuple.")
    support_level: RunMode = Field(
        default=RunMode.INGESTION,
        description=(
            "Initial support level (ADR-005). Default is 'ingestion' for "
            "explicitly configured pools; the framework may demote later."
        ),
    )
    notes: str = Field(
        default="",
        max_length=512,
        description="Free-form human notes; not parsed.",
    )

    @model_validator(mode="after")
    def _check_currency_zero_consistency(self) -> PoolConfig:
        # PoolConfig currently has no field that depends on PoolKey beyond
        # what's already validated; this hook is the place to add cross-field
        # checks when more pool-level fields are added in T022+.
        return self


class RootConfig(_StrictModel):
    """Top-level configuration object (V1: single chain, single pool, single target token)."""

    chains: list[ChainConfig] = Field(
        default_factory=list,
        description=(
            "Exactly one ChainConfig is allowed in V1 (see ADR-005 and "
            "docs/intent/PROJECT_GOALS.md)."
        ),
    )
    pools: list[PoolConfig] = Field(
        default_factory=list,
        description=(
            "Zero or one PoolConfig is allowed in V1. Multi-pool operation is out of scope."
        ),
    )
    target_token: TargetTokenConfig | None = Field(
        default=None,
        description=(
            "The single target token V1 operates on. When set, the active "
            "pool (if any) must contain this token as currency0 or currency1."
        ),
    )
    default_run_mode: RunMode = Field(
        default=RunMode.PAPER,
        description=(
            "Default run mode when a pool does not override it. Must not be 'live' by default."
        ),
    )

    # ---- V1 structural constraints ---------------------------------------

    @model_validator(mode="after")
    def _check_v1_single_chain(self) -> RootConfig:
        if len(self.chains) != 1:
            raise ValueError(
                f"V1 requires exactly one ChainConfig; got {len(self.chains)}. "
                f"Multi-chain operation is out of scope for V1 (ADR-005)."
            )
        return self

    @model_validator(mode="after")
    def _check_v1_single_active_pool(self) -> RootConfig:
        if len(self.pools) > 1:
            raise ValueError(
                f"V1 allows at most one active PoolConfig; got {len(self.pools)}. "
                f"Multi-pool operation is out of scope for V1."
            )
        return self

    @model_validator(mode="after")
    def _check_target_token_matches_chain(self) -> RootConfig:
        if self.target_token is None:
            return self
        chain_ids = {c.chain_id for c in self.chains}
        if self.target_token.chain_id not in chain_ids:
            raise ValueError(
                f"target_token.chain_id={self.target_token.chain_id} does not match any ChainConfig"
            )
        return self

    @model_validator(mode="after")
    def _check_pool_chain_reference(self) -> RootConfig:
        chain_ids = {c.chain_id for c in self.chains}
        for p in self.pools:
            if p.chain_id not in chain_ids:
                raise ValueError(
                    f"pool references chain_id={p.chain_id} which has no ChainConfig entry"
                )
        return self

    @model_validator(mode="after")
    def _check_pool_contains_target_token(self) -> RootConfig:
        if self.target_token is None or not self.pools:
            return self
        target = self.target_token.contract_address.lower()
        pool = self.pools[0]
        c0 = pool.pool_key.currency0.lower()
        c1 = pool.pool_key.currency1.lower()
        if target not in (c0, c1):
            raise ValueError(
                f"active pool's PoolKey ({c0}, {c1}) does not contain the "
                f"target token ({target}); V1 requires the active pool to "
                f"pair the user-selected target token"
            )
        return self

    @model_validator(mode="after")
    def _check_default_run_mode_not_live(self) -> RootConfig:
        if self.default_run_mode == RunMode.LIVE:
            raise ValueError(
                "default_run_mode='live' is forbidden; live mode requires "
                "an explicit Phase 9 promotion record; it cannot be a default"
            )
        return self


__all__ = [
    "ALL_HOOK_MASK",
    "ChainConfig",
    "DELTA_TO_ACTION_FLAG",
    "DYNAMIC_FEE_FLAG",
    "HOOK_FLAG_BITS",
    "MAX_LP_FEE",
    "PoolConfig",
    "PoolKey",
    "ProjectRisk",
    "RootConfig",
    "RunMode",
    "TargetTokenConfig",
    "TechnicalEligibility",
    "UserDecision",
]
