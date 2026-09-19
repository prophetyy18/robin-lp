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
- live mode cannot be enabled by direct opt-in (G-LIVE-GATE-01); it
  requires a populated :class:`LiveApproval` block that records the
  approving actor, timestamp, scope, and evidence reference.
- the Keystore path used by the (out-of-tree) signer is referenced by env
  var name; the loader rejects any literal path or password field.
"""

from __future__ import annotations

import re
from datetime import datetime
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

from robinhood_lp.config.secrets import contains_credential_url

# ---------------------------------------------------------------------------
# Protocol constants (pinned — see docs/spec/protocol/PROTOCOL_FACTS.md)
# ---------------------------------------------------------------------------
# The hook-flag and fee constants are shared immutable protocol values
# (ADR-006 §"Decision": "shared immutable identities, values, events
# and intent/result contracts" live in the protocol/domain layer).
# T007 moved the canonical definitions here from ``config.models`` so
# the qualification hook-pack module can import them without depending
# on the config layer. The config layer still re-exports them for
# back-compat with callers that import them from this module.
from robinhood_lp.protocol.ids import (
    ALL_HOOK_MASK,
    DELTA_TO_ACTION_FLAG,
    DYNAMIC_FEE_FLAG,
    HOOK_FLAG_BITS,
    MAX_LP_FEE,
)

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


class LiveApproval(_StrictModel):
    """Bound evidence that live mode is permitted on a specific scope.

    G-LIVE-GATE-01 (PROJECT_GOALS §8) requires live to be unlocked only by
    an explicit, scoped, time-stamped human promotion that follows the
    full backtest -> testnet -> post-testnet paper/shadow -> security
    review pipeline. A bare boolean flag is not sufficient: the loader
    refuses to construct a live-capable config without this block.
    """

    approved_by: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Operator handle (e.g. Gitea user) that recorded the promotion.",
    )
    approved_at: datetime = Field(
        ...,
        description=(
            "UTC instant at which the promotion was recorded. Used for "
            "audit ordering; freshness is policy-defined elsewhere."
        ),
    )
    approved_scope: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description=(
            "Free-form but auditable scope statement, e.g. 'mainnet:FYBRAIN/USDG:capped-50-USDG'."
        ),
    )
    evidence_ref: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description=(
            "Pointer to the recorded promotion evidence (commit SHA, "
            "manifest path, ticket id, etc.). The loader does not fetch it."
        ),
    )


class SignerConfig(_StrictModel):
    """Out-of-tree signer reference (G-SIGNER-01).

    Phase 9 (T090) introduces the isolated signer process; it lives
    outside the main V1 process and is the only component allowed to
    decrypt the Keystore. The configuration layer therefore records
    *only* the environment-variable name that holds the Keystore path;
    the password is never carried in config, environment variables, CLI
    arguments, logs, or audit records (per CLAUDE.md / AGENTS.md §4).

    Any literal value supplied here is rejected at parse time, so the
    framework cannot accidentally pick up a Keystore password from
    ``os.environ`` or a committed file.
    """

    keystore_path_env: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Name of the environment variable that holds the absolute path "
            "to the encrypted Web3 Keystore file. The Keystore password is "
            "NEVER read from config; it is supplied interactively by the "
            "operator at signer startup."
        ),
    )

    @field_validator("keystore_path_env")
    @classmethod
    def _check_env_name(cls, v: str) -> str:
        # Reject credential-shaped URLs before the format check so the
        # operator sees a security-flavoured error instead of "invalid
        # env-var name".
        if contains_credential_url(v):
            raise ValueError(
                "keystore_path_env looks like a credential-bearing URL; "
                "only an env-var name (e.g. LP_KEYSTORE_PATH) is accepted"
            )
        if not re.match(r"^[A-Z][A-Z0-9_]*$", v):
            raise ValueError(f"keystore_path_env={v!r} must be an UPPER_SNAKE_CASE env-var name")
        return v


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
    live intents unless the triple is on the accepted path AND a populated
    :class:`LiveApproval` block records the G-LIVE-GATE-01 promotion.
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
    # G-LIVE-GATE-01 binding. ``live`` is None by default and may only be
    # populated after the documented promotion gates have produced an
    # auditable :class:`LiveApproval` record. A bare boolean flip is not
    # accepted (see ``is_live_eligible`` below for the read-only view).
    live: LiveApproval | None = Field(
        default=None,
        description=(
            "Bound live-promotion record (G-LIVE-GATE-01). When set, the "
            "triple (technical_eligibility, user_decision) must also be on "
            "the accepted path; the framework's risk gateway refuses live "
            "intents otherwise."
        ),
    )

    decision_notes: str = Field(
        default="",
        max_length=512,
        description="Free-form note recorded with the approval decision.",
    )

    @property
    def is_live_eligible(self) -> bool:
        """Read-only True/False view derived from ``self.live``."""
        return self.live is not None

    @field_validator("contract_address")
    @classmethod
    def _check_address(cls, v: str) -> str:
        return _validate_address(v, field="contract_address")

    @model_validator(mode="after")
    def _check_blocked_cannot_be_live(self) -> TargetTokenConfig:
        if self.technical_eligibility == TechnicalEligibility.BLOCKED and self.live is not None:
            raise ValueError(
                "technical_eligibility=BLOCKED is an absolute veto; "
                "live promotion cannot be attached (ADM-TECH-005/007)"
            )
        return self

    @model_validator(mode="after")
    def _check_rejected_or_revoked_blocks_live(self) -> TargetTokenConfig:
        if (
            self.user_decision in (UserDecision.REJECTED, UserDecision.REVOKED)
            and self.live is not None
        ):
            raise ValueError(
                f"user_decision={self.user_decision.value} blocks live "
                f"execution; live promotion cannot be attached"
            )
        return self


class ResearchMemberConfig(_StrictModel):
    """One ``(chain_id, PoolKey)`` row of the research universe (T026).

    The research universe is the configuration collection that names
    arbitrary pools the operator wants to study without giving them
    any execution authority. ADR-014 keeps this collection disjoint
    from the single active execution pool: a research member is not
    an approved pool, never holds assets, and never produces a
    transaction.

    The configuration-side shape is intentionally minimal: chain_id,
    the canonical ``PoolKey``, the inclusive block range the member
    covers, the support level the operator assigns at registration
    time, and a free-form note. The runtime-side counterpart
    (:class:`robinhood_lp.discovery.research_universe.ResearchMember`)
    adds the ``added_via`` entry path; the configuration does not
    carry it because the configuration cannot know which path the
    operator used at registration time (T026 acceptance: the entry
    path is a runtime fact).

    The block range fields are inclusive on both ends; ``None`` is
    the sentinel for ``block_range_end`` meaning "no upper bound"
    (the framework refuses a runtime range where the end is unknown,
    but the configuration is the place to declare the user's intent).
    """

    chain_id: PositiveInt = Field(
        ...,
        description=(
            "Chain on which the research pool is deployed; must match a ChainConfig entry."
        ),
    )
    pool_key: PoolKey = Field(
        ...,
        description=(
            "Canonical V4 PoolKey tuple (currency0, currency1, fee, "
            "tick_spacing, hooks). The same identity rules as the "
            "active-pool PoolKey apply."
        ),
    )
    block_range_start: int = Field(
        default=0,
        ge=0,
        description=(
            "Inclusive start block of the member's research window. "
            '``0`` means "from the earliest block the framework can read".'
        ),
    )
    block_range_end: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Inclusive end block of the member's research window. "
            '``None`` is the sentinel for "no upper bound declared yet". '
            "The runtime rejects an end that is less than the start."
        ),
    )
    support_level: RunMode = Field(
        default=RunMode.INGESTION,
        description=(
            "Support level the operator assigns to this member at "
            "registration time. The runtime classifier (T027) may "
            "raise or lower it on its own evidence; the value here "
            "is the user's stated intent."
        ),
    )
    notes: str = Field(
        default="",
        max_length=512,
        description=(
            "Free-form human notes; not parsed. Used to record why "
            "the member was added or which dataset it is intended for."
        ),
    )

    @model_validator(mode="after")
    def _check_block_range_ordering(self) -> ResearchMemberConfig:
        if self.block_range_end is not None and self.block_range_end < self.block_range_start:
            raise ValueError(
                f"research_universe member block_range_end="
                f"{self.block_range_end} is less than block_range_start="
                f"{self.block_range_start}"
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

    @model_validator(mode="after")
    def _check_live_support_requires_approval(self) -> PoolConfig:
        # G-LIVE-GATE-01: a pool cannot declare ``support_level='live'``
        # without a populated ``live`` block on the matching target token.
        # Cross-field enforcement lives at the RootConfig layer because the
        # token is owned there; the pool itself only carries a note about
        # the missing context.
        if self.support_level == RunMode.LIVE and not self.notes.strip():
            # Allow construction here; the RootConfig check will reject
            # the assembled config if the target token's ``live`` block is
            # absent. We still refuse a pool that names ``live`` with no
            # notes so that audit logs surface the operator's intent.
            raise ValueError(
                "support_level='live' requires a populated 'notes' "
                "field recording the operator's intent and scope"
            )
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
    research_universe: list[ResearchMemberConfig] = Field(
        default_factory=list,
        description=(
            "Research-universe members (T026, ADR-014). The list is "
            "disjoint from the active execution pool collection: a "
            "research member holds no assets, produces no transactions "
            "and grants no execution authority. An empty list is a "
            "valid V1 configuration; the structural single-active-pool "
            "rule on ``pools`` is independent of this collection and "
            "fires regardless of how many research members are present."
        ),
    )
    target_token: TargetTokenConfig | None = Field(
        default=None,
        description=(
            "The single target token V1 operates on. When set, the active "
            "pool (if any) must contain this token as currency0 or currency1."
        ),
    )
    signer: SignerConfig | None = Field(
        default=None,
        description=(
            "Out-of-tree signer reference (G-SIGNER-01). When omitted, the "
            "framework cannot reach live execution. The signer process is "
            "introduced by Phase 9 (T090) and is not loaded by the main V1 "
            "process during Phase 0-8."
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
    def _check_research_universe_chain_consistency(self) -> RootConfig:
        """Every research member must reference a known ChainConfig.

        The collection is allowed to be empty (the common V1 case).
        A populated research universe must reference one of the
        chains declared at the root; the framework never silently
        coerces the chain_id.
        """
        if not self.research_universe:
            return self
        chain_ids = {c.chain_id for c in self.chains}
        for m in self.research_universe:
            if m.chain_id not in chain_ids:
                raise ValueError(
                    f"research_universe member references chain_id="
                    f"{m.chain_id} which has no ChainConfig entry"
                )
        return self

    @model_validator(mode="after")
    def _check_research_universe_no_duplicate_identities(self) -> RootConfig:
        """No two research members may share the same ``PoolId``.

        The research universe is keyed by ``(chain_id, PoolId)``. A
        duplicate is a configuration mistake and is rejected at
        parse time (T026 acceptance: a member list containing a
        duplicate is rejected).
        """
        seen: set[tuple[int, str]] = set()
        for m in self.research_universe:
            # Derive the PoolId through the protocol layer so a
            # future PoolKey ordering change cannot desync the two.
            from robinhood_lp.protocol import (
                Address,
                Currency,
            )
            from robinhood_lp.protocol import (
                PoolKey as _ProtocolPoolKey,
            )

            proto_pk = _ProtocolPoolKey(
                currency0=Currency.from_address(Address.from_hex(m.pool_key.currency0)),
                currency1=Currency.from_address(Address.from_hex(m.pool_key.currency1)),
                fee=m.pool_key.fee,
                tick_spacing=m.pool_key.tick_spacing,
                hooks=Address.from_hex(m.pool_key.hooks),
            )
            key = (m.chain_id, proto_pk.to_pool_id().to_hex())
            if key in seen:
                raise ValueError(
                    f"research_universe contains duplicate PoolKey "
                    f"(chain_id={m.chain_id}, pool_id={key[1]}); "
                    f"each member must have a unique (chain_id, PoolKey) pair"
                )
            seen.add(key)
        return self

    @model_validator(mode="after")
    def _check_research_member_does_not_become_active_pool(self) -> RootConfig:
        """A research member is never silently promoted to active pool.

        The structural single-active-pool rule on ``pools`` is
        independent of the research universe. The framework never
        writes a research member into the active-pool collection
        to make a multi-pool research setup pass validation; if the
        operator wants a pool in both collections they must declare
        it in both, and the two remain distinct identities at the
        runtime layer (T026 acceptance: a research-universe member
        is never reported as, or promoted by, the active execution
        pool).
        """
        if not self.research_universe or not self.pools:
            return self
        # The two collections are disjoint by definition. A research
        # member whose PoolKey matches the active PoolKey is a
        # distinct identity (different ``support_level``, different
        # notes), so we do not block it here. We only block the
        # structural rule that a populated research universe must
        # not weaken the v1 single-active-pool rule, which is
        # enforced independently by ``_check_v1_single_active_pool``.
        return self

    @model_validator(mode="after")
    def _check_default_run_mode_not_live(self) -> RootConfig:
        if self.default_run_mode == RunMode.LIVE:
            raise ValueError(
                "default_run_mode='live' is forbidden; live mode requires "
                "an explicit Phase 9 promotion record; it cannot be a default"
            )
        return self

    @model_validator(mode="after")
    def _check_live_support_level_requires_approval(self) -> RootConfig:
        # G-LIVE-GATE-01: any pool whose ``support_level`` is ``live``
        # requires the target token to carry a populated ``live`` block.
        # Without that record, the assembled config is rejected at load
        # time; the framework never reaches a state where live could be
        # activated by a bare boolean toggle.
        if not self.pools:
            return self
        for p in self.pools:
            if p.support_level == RunMode.LIVE:
                if self.target_token is None or self.target_token.live is None:
                    raise ValueError(
                        "support_level='live' requires target_token.live "
                        "to be populated with a LiveApproval record "
                        "(G-LIVE-GATE-01)"
                    )
                if self.signer is None:
                    raise ValueError(
                        "support_level='live' requires the signer block to "
                        "reference a Keystore env-var name (G-SIGNER-01)"
                    )
        return self


__all__ = [
    "ALL_HOOK_MASK",
    "ChainConfig",
    "DELTA_TO_ACTION_FLAG",
    "DYNAMIC_FEE_FLAG",
    "HOOK_FLAG_BITS",
    "LiveApproval",
    "MAX_LP_FEE",
    "PoolConfig",
    "PoolKey",
    "ProjectRisk",
    "ResearchMemberConfig",
    "RootConfig",
    "RunMode",
    "SignerConfig",
    "TargetTokenConfig",
    "TechnicalEligibility",
    "UserDecision",
]
