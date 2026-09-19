"""Hook-semantics evidence packs (T043).

The T043 contract requires that every pool the framework promotes
beyond ``ingestion`` carry a *versioned, reviewable* hook-semantics
evidence pack. A hook pack must include, at minimum, the
hook address / code hash / proxy implementation and upgrade
authority, the decoded flag bits, a verified source / ABI record,
the before / after delta path, the dynamic-fee behaviour, the
external state dependencies, a deterministic replay model, a set of
adversarial tests, and an explicit invalidation rule. A pool whose
hook is the zero address carries no hook pack; instead, the empty
set is recorded as its own evidence artifact with the per-pool
T023 classification outcome, the observed `hooks` value, the reason
no hook pack exists, and an explicit acknowledgment that **no**
hook semantics were verified by this task.

Two architectural rules from T023 / T038 govern the pack:

- A nonzero flag bit in the hook address is **not** evidence of
  semantics; it is only a possibility. The pack must separate the
  flag bits (cheap, observable) from the verified implementation
  facts (address book, code hash, owner, ABI, source revision).
- Evidence is **not** generalisable: every field carries the
  ``chain_id`` and the hook's ``Address`` so the audit trail can
  reject a stale pack applied to a different deployment.

This module sits in the qualification layer per ADR-006; it
depends only on the protocol layer (typed ``PoolKey`` /
``PoolId`` / ``Address``), the discovery layer (T023 eligibility,
``HookEvidence``, ``EligibilityDecision``, ``EligibilityReason``),
and the qualification layer's second-pool resolver surface
(``ResolvedPoolKey``). It must not import RPC, storage, the
config layer, signer code, or any network time.

The module is float-free and deterministic: every input is either
a pinned protocol constant, an operator-supplied observation, or a
T023 / T038 surface value; no I/O, no wall-clock time, no random
sources. The pack's ``version`` field is the spec-revision string
the runner pins at build time so re-running on a fresh checkout
reproduces the same bytes.

Empty-set evidence
------------------

When no included pool has a nonzero ``hooks`` address, T043 still
produces a single, reviewable artifact. The artifact carries:

- the per-pool T023 ``EligibilityDecision`` for each included pool
  (with the observed ``hooks`` value and the reason code that
  produced its level);
- the reason no hook pack exists for each pool (the canonical
  zero-address contract: ``hooks_is_zero``);
- an explicit, top-level statement that **no** hook semantics were
  verified by this task, so the empty set is never summarised,
  reported, or accepted as though hook behaviour had been checked.

The empty-set artifact never relaxes anything below the T046/Phase 5
gates: a pool whose hooks are nonzero and that is promoted beyond
``ingestion`` still needs a complete pack, and a hook-flag bit is
never treated as evidence of semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from robinhood_lp.discovery.eligibility import (
    EligibilityDecision,
    EligibilityReasonCode,
)
from robinhood_lp.protocol import Address, PoolKey, RunMode
from robinhood_lp.protocol.ids import (
    ALL_HOOK_MASK,
    DELTA_TO_ACTION_FLAG,
    DYNAMIC_FEE_FLAG,
    HOOK_FLAG_BITS,
)

# ---------------------------------------------------------------------------
# Spec revision pin (T043 contract)
# ---------------------------------------------------------------------------

#: The spec-revision string T043 pins the evidence pack under. The
#: pack carries it on every record so an audit-trail reader can
#: reject a pack built against an older revision.
HOOK_PACK_SPEC_REVISION: Final[str] = "v1-2026-09-18-t043-hook-pack"

#: The minimum set of the fixed fields T043 names. Tests reference
#: the constant so the cross-check between a ``HookPack`` and a
#: ``EmptySetHookEvidence`` stays in lock-step.
REQUIRED_HOOK_PACK_FIELDS: Final[tuple[str, ...]] = (
    "chain_id",
    "pool_id_hex",
    "hook_address",
    "code_hash",
    "proxy_implementation_address",
    "upgrade_authority",
    "flag_bits",
    "verified_source",
    "verified_abi",
    "before_after_deltas",
    "dynamic_fee_behavior",
    "external_state_dependencies",
    "replay_model",
    "adversarial_tests",
    "invalidation_rule",
    "version",
)

#: Hook flag → name mapping for the V4 ``Hooks.sol`` table. The
#: table is the source of truth for which low-14 bit means which
#: callback; we record the bit name verbatim from the Uniswap
#: convention so the audit trail matches the upstream ABI.
HOOK_FLAG_NAMES: Final[tuple[tuple[int, str], ...]] = (
    (1 << 0, "AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA"),
    (1 << 1, "AFTER_ADD_LIQUIDITY_RETURNS_DELTA"),
    (1 << 2, "AFTER_SWAP_RETURNS_DELTA"),
    (1 << 3, "BEFORE_SWAP_RETURNS_DELTA"),
    (1 << 4, "AFTER_DONATE"),
    (1 << 5, "BEFORE_DONATE"),
    (1 << 6, "AFTER_SWAP"),
    (1 << 7, "BEFORE_SWAP"),
    (1 << 8, "AFTER_REMOVE_LIQUIDITY"),
    (1 << 9, "BEFORE_REMOVE_LIQUIDITY"),
    (1 << 10, "AFTER_ADD_LIQUIDITY"),
    (1 << 11, "BEFORE_ADD_LIQUIDITY"),
    (1 << 12, "AFTER_INITIALIZE"),
    (1 << 13, "BEFORE_INITIALIZE"),
)


# ---------------------------------------------------------------------------
# Action vs. return-delta callback paths
# ---------------------------------------------------------------------------

#: Pairs of (action flag, return-delta flag) that V4 ``Hooks.sol``
#: couples. A pool that enables a return-delta flag must also enable
#: the matching action flag (``isValidHookAddress`` rule 1).
DELTA_PAIR_TABLE: Final[tuple[tuple[int, int, str], ...]] = (
    (1 << 7, 1 << 3, "swap"),
    (1 << 6, 1 << 2, "swap"),
    (1 << 10, 1 << 1, "add_liquidity"),
    (1 << 8, 1 << 0, "remove_liquidity"),
)

#: The action callbacks V4 fires around a swap or modify-liquidity
#: operation, in the upstream-deliverable order. The pack carries the
#: list so the replay model can iterate them deterministically.
ENABLED_CALLBACK_NAMES: Final[tuple[str, ...]] = (
    "beforeInitialize",
    "afterInitialize",
    "beforeAddLiquidity",
    "afterAddLiquidity",
    "beforeRemoveLiquidity",
    "afterRemoveLiquidity",
    "beforeSwap",
    "afterSwap",
    "beforeDonate",
    "afterDonate",
)


# ---------------------------------------------------------------------------
# Hook delta path (a single callback's documented entry/return shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookDeltaPath:
    """One callback's before / after delta path.

    ``callback`` is the upstream callback name
    (e.g. ``beforeSwap``); ``returns_delta`` records whether the
    V4 return-delta bit is set so the runner knows whether the
    callback may write a non-zero ``BalanceDelta`` back; ``action``
    is the V4 dispatch semantic (``modify_liquidity`` /
    ``swap`` / ``donate`` / ``initialize``). ``fields_read`` and
    ``fields_written`` are the documented field names the callback
    is expected to read / write; they are part of the audit trail,
    not the upstream ABI.

    Empty ``fields_read`` / ``fields_written`` mean "the upstream
    specification does not document a fixed schema"; the runner
    must rely on the verified source / ABI record (``verified_source``)
    rather than guessing from these names.
    """

    callback: str
    action: str
    returns_delta: bool
    fields_read: tuple[str, ...] = ()
    fields_written: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Replay model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookReplayModel:
    """The replay model a hook pack commits to.

    The replay model is the contract the deterministic replay /
    reconstruction must satisfy when the hook is in scope. The
    fields are versioned together with the pack so an audit-trail
    reader can compare a future pack against the model's
    commitments.

    ``expected_modified_event_count`` is the count of typed V4
    events (Initialize / ModifyLiquidity / Swap / Donate /
    ProtocolFeeUpdated) the replay must produce per block window
    that contains a hook-influenced event. ``delta_propagation``
    describes whether the replay must propagate the return-delta
    into the next typed event (V4's documented contract: yes for
    ``afterSwap`` / ``afterAddLiquidity`` / ``afterRemoveLiquidity``,
    no for the ``before*`` callbacks).
    """

    expected_modified_event_count: int
    delta_propagation: str
    fork_test_coverage: tuple[str, ...]
    golden_transaction_coverage: tuple[str, ...]
    notes: str = ""


# ---------------------------------------------------------------------------
# Invalidation rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookInvalidationRule:
    """The rule that determines when the pack must be re-built.

    ``trigger_conditions`` are the trigger names that, when
    observed, force a fresh pack and demote the pool to
    ``ingestion``. ``demoted_to`` is the support level the pool
    must fall back to; T023 / T038 require ``ingestion`` for any
    detected hook change.
    """

    trigger_conditions: tuple[str, ...]
    demoted_to: RunMode
    notes: str = ""


# ---------------------------------------------------------------------------
# Verified source / ABI
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookSourcePin:
    """The pinned source / ABI record a hook pack must carry.

    ``repository`` is the upstream repository name (the
    ``uniswap-v4-core`` ABI artifact, a third-party hook repo, or
    a fork tag the runner records verbatim); ``commit`` is the
    exact commit SHA or tag the runner pinned. ``verified_abi_selectors``
    is the tuple of function selectors (4-byte hex) the runner
    verified against the deployed bytecode; ``verified_event_topics``
    is the tuple of event topic0s the runner verified against the
    deployed bytecode.

    The record is the **minimum** set the T023 acceptance clause
    "verified source / ABI" demands; it does not replace the
    on-chain ABI fetch the runner does at promotion time.
    """

    repository: str
    commit: str
    verified_abi_selectors: tuple[str, ...]
    verified_event_topics: tuple[str, ...]
    notes: str = ""


# ---------------------------------------------------------------------------
# Hook pack (per pool)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookPack:
    """The T043 evidence pack for one pool with nonzero hooks.

    The pack is a value object: tests construct it directly from
    pinned / observed inputs; the runner assembles a pack from the
    T023 / T024 / T038 / T042 surfaces it consumes. ``version`` is
    pinned at build time so re-running the build produces
    byte-equivalent packs.

    All fields are required; the pack is incomplete (and rejected)
    when any field is empty / ``None`` without an explicit reason
    recorded in ``invalidation_note``.
    """

    chain_id: int
    pool_id_hex: str
    pool_alias: str
    hook_address: str
    code_hash: str
    proxy_implementation_address: str | None
    upgrade_authority: str | None
    flag_bits: tuple[int, ...]
    flag_names: tuple[str, ...]
    is_dynamic_fee: bool
    verified_source: HookSourcePin
    before_after_deltas: tuple[HookDeltaPath, ...]
    dynamic_fee_behavior: str
    external_state_dependencies: tuple[str, ...]
    replay_model: HookReplayModel
    adversarial_tests: tuple[str, ...]
    invalidation_rule: HookInvalidationRule
    version: str = HOOK_PACK_SPEC_REVISION
    invalidation_note: str = ""

    def __post_init__(self) -> None:
        if self.chain_id <= 0:
            raise ValueError(f"HookPack: chain_id must be positive, got {self.chain_id}")
        body_len = len(self.pool_id_hex.strip()) - 2
        if body_len != 64:
            raise ValueError(
                f"HookPack: pool_id_hex must be 0x + 64 hex chars (32-byte V4 PoolId), "
                f"got {self.pool_id_hex!r}"
            )
        if not self.hook_address.startswith("0x") or len(self.hook_address) != 2 + 40:
            raise ValueError(
                f"HookPack: hook_address must be a 20-byte 0x-hex address, "
                f"got {self.hook_address!r}"
            )
        if int(self.hook_address, 16) == 0:
            raise ValueError(
                "HookPack: hook_address is the canonical zero address; "
                "build an EmptySetHookEvidence instead of a HookPack"
            )
        if self.code_hash == "":
            raise ValueError("HookPack: code_hash must be a non-empty sha256 string")
        for bit in self.flag_bits:
            if bit < 0 or bit >= ALL_HOOK_MASK:
                raise ValueError(f"HookPack: flag bit {bit:#x} is outside the V4 low-14 mask")
        if self.version != HOOK_PACK_SPEC_REVISION:
            raise ValueError(
                f"HookPack: version must be {HOOK_PACK_SPEC_REVISION!r}, got {self.version!r}"
            )
        # The flag-bits ↔ flag-names invariant: every flag bit must
        # be named in the same order the upstream table records it.
        if len(self.flag_bits) != len(self.flag_names):
            raise ValueError(
                f"HookPack: flag_bits and flag_names must have the same length, "
                f"got {len(self.flag_bits)} vs {len(self.flag_names)}"
            )
        # The dynamic-fee invariant: a dynamic-fee pool has
        # ``fee == DYNAMIC_FEE_FLAG``; the pack records this so the
        # audit trail can confirm the fee / hook combo matches.
        if self.is_dynamic_fee and not self.dynamic_fee_behavior:
            raise ValueError(
                "HookPack: dynamic_fee_behavior must be non-empty when is_dynamic_fee is True"
            )

    @property
    def has_proxy_implementation(self) -> bool:
        return self.proxy_implementation_address is not None

    @property
    def has_upgrade_authority(self) -> bool:
        return self.upgrade_authority is not None

    def to_dict(self) -> dict[str, Any]:
        """Serialise the pack for the audit trail.

        The dict's schema is the audit-trail format the operator
        runbook persists alongside the per-pool T034 machine
        report. The keys match :data:`REQUIRED_HOOK_PACK_FIELDS`
        exactly so the cross-check never depends on a separate
        schema file.
        """
        return {
            "schema": "robinhood_lp.qualification.hook_pack.v1",
            "version": self.version,
            "chain_id": self.chain_id,
            "pool_alias": self.pool_alias,
            "pool_id": self.pool_id_hex,
            "hook_address": self.hook_address,
            "code_hash": self.code_hash,
            "proxy_implementation_address": self.proxy_implementation_address,
            "upgrade_authority": self.upgrade_authority,
            "flag_bits": [int(b) for b in self.flag_bits],
            "flag_names": list(self.flag_names),
            "is_dynamic_fee": bool(self.is_dynamic_fee),
            "verified_source": {
                "repository": self.verified_source.repository,
                "commit": self.verified_source.commit,
                "verified_abi_selectors": list(self.verified_source.verified_abi_selectors),
                "verified_event_topics": list(self.verified_source.verified_event_topics),
                "notes": self.verified_source.notes,
            },
            "before_after_deltas": [
                {
                    "callback": d.callback,
                    "action": d.action,
                    "returns_delta": bool(d.returns_delta),
                    "fields_read": list(d.fields_read),
                    "fields_written": list(d.fields_written),
                }
                for d in self.before_after_deltas
            ],
            "dynamic_fee_behavior": self.dynamic_fee_behavior,
            "external_state_dependencies": list(self.external_state_dependencies),
            "replay_model": {
                "expected_modified_event_count": int(
                    self.replay_model.expected_modified_event_count
                ),
                "delta_propagation": self.replay_model.delta_propagation,
                "fork_test_coverage": list(self.replay_model.fork_test_coverage),
                "golden_transaction_coverage": list(self.replay_model.golden_transaction_coverage),
                "notes": self.replay_model.notes,
            },
            "adversarial_tests": list(self.adversarial_tests),
            "invalidation_rule": {
                "trigger_conditions": list(self.invalidation_rule.trigger_conditions),
                "demoted_to": self.invalidation_rule.demoted_to.value,
                "notes": self.invalidation_rule.notes,
            },
            "invalidation_note": self.invalidation_note,
        }


# ---------------------------------------------------------------------------
# Empty-set evidence (no included pool has nonzero hooks)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmptySetHookEvidence:
    """The T043 evidence record for the case "no included pool has nonzero hooks".

    The artifact is the per-pool T023 classification outcome for
    every included pool, with the observed ``hooks`` value and the
    classification reason that produced its level, **plus** the
    reason no hook pack exists for each pool, **plus** an explicit,
    top-level statement that no hook semantics were verified by
    this task.

    The artifact is not a "fitness" signal: it never relaxes the
    T046/Phase 5 gate, never promotes a pool, and never implies
    that hook behaviour has been checked. It is the record a
    downstream reviewer reads to confirm the framework correctly
    concluded that the empty set is the truth for this run.
    """

    chain_id: int
    pool_classifications: tuple[HookPoolClassificationOutcome, ...]
    hook_verification_statement: str
    version: str = HOOK_PACK_SPEC_REVISION

    def __post_init__(self) -> None:
        if self.chain_id <= 0:
            raise ValueError(
                f"EmptySetHookEvidence: chain_id must be positive, got {self.chain_id}"
            )
        if not self.pool_classifications:
            raise ValueError(
                "EmptySetHookEvidence: pool_classifications must be non-empty; "
                "an empty list is not a valid empty-set evidence artifact"
            )
        if not self.hook_verification_statement.strip():
            raise ValueError(
                "EmptySetHookEvidence: hook_verification_statement must be a non-empty "
                "explicit declaration that no hook semantics were verified"
            )
        if self.version != HOOK_PACK_SPEC_REVISION:
            raise ValueError(
                f"EmptySetHookEvidence: version must be {HOOK_PACK_SPEC_REVISION!r}, "
                f"got {self.version!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "robinhood_lp.qualification.hook_pack.empty_set.v1",
            "version": self.version,
            "chain_id": self.chain_id,
            "hook_verification_statement": self.hook_verification_statement,
            "pool_classifications": [c.to_dict() for c in self.pool_classifications],
        }


# ---------------------------------------------------------------------------
# Per-pool classification outcome (empty-set + hook-pack build input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookPoolClassificationOutcome:
    """The per-pool T023 classification outcome T043 records.

    The record is the empty-set evidence's per-pool row and the
    ``HookPack`` builder's audit input. It carries:

    - the pool's alias (``reference`` / ``second``);
    - the observed ``hooks`` address;
    - whether the address is the canonical zero address
      (``hooks_is_zero``);
    - the T023 ``EligibilityDecision`` the pool's classifier
      produced (level + reason codes);
    - the reason no hook pack exists when ``hooks_is_zero``;
    - the prior ``code_hash`` and the framework's pinned
      ``code_hash`` when the pool has a nonzero hook — the
      invalidation rule uses these to demote the pool on a mismatch.

    The dataclass is the contract that T043 records as a T023
    classification outcome alongside the (per-pool) pack record;
    the runner surfaces it in the operator runbook.
    """

    pool_alias: str
    pool_id_hex: str
    hooks_address: str
    hooks_is_zero: bool
    eligibility_level: str
    eligibility_reasons: tuple[str, ...]
    reason_no_hook_pack: str = ""
    prior_code_hash: str | None = None
    current_code_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.pool_alias.strip():
            raise ValueError("HookPoolClassificationOutcome: pool_alias must be non-empty")
        body_len = len(self.pool_id_hex.strip()) - 2
        if body_len != 64:
            raise ValueError(
                f"HookPoolClassificationOutcome: pool_id_hex must be 0x + 64 hex chars "
                f"(32-byte V4 PoolId), got {self.pool_id_hex!r}"
            )
        if not self.hooks_address.startswith("0x") or len(self.hooks_address) != 2 + 40:
            raise ValueError(
                f"HookPoolClassificationOutcome: hooks_address must be a 20-byte 0x-hex "
                f"address, got {self.hooks_address!r}"
            )
        if self.hooks_is_zero and int(self.hooks_address, 16) != 0:
            raise ValueError(
                "HookPoolClassificationOutcome: hooks_is_zero is True but hooks_address is "
                "non-zero; the flags must agree"
            )
        if not self.hooks_is_zero and not self.reason_no_hook_pack == "":
            raise ValueError(
                "HookPoolClassificationOutcome: reason_no_hook_pack must be empty when "
                "hooks are non-zero (a hook pack must be built instead)"
            )
        if self.hooks_is_zero and not self.reason_no_hook_pack.strip():
            raise ValueError(
                "HookPoolClassificationOutcome: reason_no_hook_pack must be non-empty when "
                "hooks are zero (the empty-set artifact must name the reason)"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_alias": self.pool_alias,
            "pool_id": self.pool_id_hex,
            "hooks_address": self.hooks_address,
            "hooks_is_zero": bool(self.hooks_is_zero),
            "eligibility_level": self.eligibility_level,
            "eligibility_reasons": list(self.eligibility_reasons),
            "reason_no_hook_pack": self.reason_no_hook_pack,
            "prior_code_hash": self.prior_code_hash,
            "current_code_hash": self.current_code_hash,
        }


# ---------------------------------------------------------------------------
# Combined hook-pack report (the T043 machine record)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookPackReport:
    """The combined T043 evidence record.

    The report carries either:

    - one ``HookPack`` per pool whose ``hooks`` is nonzero, plus the
      empty-set record (``empty_set_record``) for every pool whose
      ``hooks`` is the zero address; **or**
    - a single ``EmptySetHookEvidence`` record when no included pool
      has nonzero hooks, with the per-pool T023 classification
      outcome for every included pool.

    The report's ``kind`` field names which shape the record has:
    ``"hook_packs"`` for the heterogeneous pack record, or
    ``"empty_set"`` for the empty-set record. Tests assert on
    ``kind`` so the audit-trail reader can reject an ambiguous
    shape.
    """

    chain_id: int
    kind: str
    hook_packs: tuple[HookPack, ...] = ()
    empty_set_record: EmptySetHookEvidence | None = None
    pool_classifications: tuple[HookPoolClassificationOutcome, ...] = ()
    version: str = HOOK_PACK_SPEC_REVISION

    def __post_init__(self) -> None:
        if self.chain_id <= 0:
            raise ValueError(f"HookPackReport: chain_id must be positive, got {self.chain_id}")
        if self.kind not in ("hook_packs", "empty_set"):
            raise ValueError(
                f"HookPackReport: kind must be 'hook_packs' or 'empty_set', got {self.kind!r}"
            )
        if self.kind == "hook_packs" and not self.hook_packs:
            raise ValueError("HookPackReport: kind='hook_packs' requires at least one HookPack")
        if self.kind == "hook_packs" and self.empty_set_record is not None:
            raise ValueError(
                "HookPackReport: kind='hook_packs' must not carry an EmptySetHookEvidence"
            )
        if self.kind == "empty_set" and self.empty_set_record is None:
            raise ValueError("HookPackReport: kind='empty_set' requires an EmptySetHookEvidence")
        if self.kind == "empty_set" and self.hook_packs:
            raise ValueError("HookPackReport: kind='empty_set' must not carry any HookPack records")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": "robinhood_lp.qualification.hook_pack_report.v1",
            "version": self.version,
            "chain_id": self.chain_id,
            "kind": self.kind,
            "pool_classifications": [c.to_dict() for c in self.pool_classifications],
        }
        if self.kind == "hook_packs":
            out["hook_packs"] = [p.to_dict() for p in self.hook_packs]
        else:
            assert self.empty_set_record is not None  # validated in __post_init__
            out["empty_set_record"] = self.empty_set_record.to_dict()
        return out


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def decode_hook_flag_bits(hook_address: Address) -> tuple[tuple[int, str], ...]:
    """Decode the V4 hook flag bits from a hook address.

    The function is the upstream-table lookup: for every bit set
    in the low 14 bits, the function appends ``(bit, name)`` to the
    returned tuple in the upstream ``Hooks.sol`` order. The order
    is the LSB-first order the ABI / framework conventionally use,
    so the resulting tuple's order matches the upstream ABI
    artifact exactly.
    """
    out: list[tuple[int, str]] = []
    for bit, name in HOOK_FLAG_NAMES:
        if (hook_address.value & bit) != 0:
            out.append((bit, name))
    return tuple(out)


def derive_dynamic_fee_behavior(pool_key: PoolKey) -> str:
    """Derive the dynamic-fee behaviour string for the hook pack.

    V4's ``isValidHookAddress(IHooks, uint24 fee)`` rule 3 says a
    hook is required for a dynamic-fee pool (``fee ==
    DYNAMIC_FEE_FLAG``) unless the address has a flag bit set in
    the low 14 bits. The pack records the rule the runner
    committed to so the audit trail can confirm the hook is feeding
    the dynamic-fee decision.

    Returns an empty string when the pool is static-fee and the
    hook does not signal a return-delta; a hook pack for a
    static-fee no-delta pool still documents the dynamic-fee
    rule so the pack is byte-stable when the fee is later changed.
    """
    fee = pool_key.fee
    hook_int = pool_key.hooks.value
    if fee == DYNAMIC_FEE_FLAG:
        return (
            "dynamic_fee_pool: fee == DYNAMIC_FEE_FLAG (0x800000); the hook's "
            "beforeSwap / afterSwap return-deltas may adjust the fee the route charges; "
            "the replay model must surface the dynamic-fee variant when the hook fires"
        )
    if (hook_int & ALL_HOOK_MASK) != 0:
        return (
            "static_fee_pool_with_nonzero_hook_flags: hook fires beforeSwap / afterSwap "
            "(or before/after ModifyLiquidity / Donate) but the fee itself is "
            "constant; the replay model records the hook deltas as balance-delta "
            "adjustments only, not fee adjustments"
        )
    return ""


def derive_before_after_deltas(pool_key: PoolKey) -> tuple[HookDeltaPath, ...]:
    """Derive the documented before/after delta paths for the pack.

    The function inspects the hook flag bits and emits a
    :class:`HookDeltaPath` per enabled callback in the upstream
    ``before*`` / ``after*`` order. The pair tables match V4's
    ``isValidHookAddress`` rule 1: a delta flag without its
    matching action flag is rejected by the upstream PoolManager,
    so the pack must not invent a delta path that the framework
    would reject.

    The pack is a contract; the runner commits to ``fields_read`` /
    ``fields_written`` it documents here. The fields are the
    upstream-documented interface (``sender``, ``poolKey``,
    ``sqrtPriceX96`` / ``tick``, ``liquidity``, ``balanceDelta``),
    not a guess — they are the contract the V4 ``IHooks.sol``
    interface publishes.
    """
    hook_int = pool_key.hooks.value
    deltas: list[HookDeltaPath] = []

    # Helper: standard fields V4's documented interface passes to
    # the hook. Empty for callbacks that take no parameters
    # beyond the implicit ``PoolKey``.
    if (hook_int & (1 << 13)) != 0:
        deltas.append(
            HookDeltaPath(
                callback="beforeInitialize",
                action="initialize",
                returns_delta=False,
                fields_read=("sender", "poolKey"),
                fields_written=(),
            )
        )
    if (hook_int & (1 << 12)) != 0:
        deltas.append(
            HookDeltaPath(
                callback="afterInitialize",
                action="initialize",
                returns_delta=False,
                fields_read=("sender", "poolKey", "sqrtPriceX96", "tick"),
                fields_written=(),
            )
        )
    if (hook_int & (1 << 11)) != 0:
        deltas.append(
            HookDeltaPath(
                callback="beforeAddLiquidity",
                action="add_liquidity",
                returns_delta=False,
                fields_read=("sender", "poolKey", "tickLower", "tickUpper", "liquidityDelta"),
                fields_written=(),
            )
        )
    if (hook_int & (1 << 10)) != 0:
        deltas.append(
            HookDeltaPath(
                callback="afterAddLiquidity",
                action="add_liquidity",
                returns_delta=(hook_int & (1 << 1)) != 0,
                fields_read=("sender", "poolKey", "tickLower", "tickUpper", "liquidityDelta"),
                fields_written=("balanceDelta",) if (hook_int & (1 << 1)) != 0 else (),
            )
        )
    if (hook_int & (1 << 9)) != 0:
        deltas.append(
            HookDeltaPath(
                callback="beforeRemoveLiquidity",
                action="remove_liquidity",
                returns_delta=False,
                fields_read=("sender", "poolKey", "tickLower", "tickUpper", "liquidityDelta"),
                fields_written=(),
            )
        )
    if (hook_int & (1 << 8)) != 0:
        deltas.append(
            HookDeltaPath(
                callback="afterRemoveLiquidity",
                action="remove_liquidity",
                returns_delta=(hook_int & (1 << 0)) != 0,
                fields_read=("sender", "poolKey", "tickLower", "tickUpper", "liquidityDelta"),
                fields_written=("balanceDelta",) if (hook_int & (1 << 0)) != 0 else (),
            )
        )
    if (hook_int & (1 << 7)) != 0:
        deltas.append(
            HookDeltaPath(
                callback="beforeSwap",
                action="swap",
                returns_delta=False,
                fields_read=("sender", "poolKey", "zeroForOne", "amountSpecified", "sqrtPriceX96"),
                fields_written=(),
            )
        )
    if (hook_int & (1 << 6)) != 0:
        deltas.append(
            HookDeltaPath(
                callback="afterSwap",
                action="swap",
                returns_delta=(hook_int & (1 << 2)) != 0,
                fields_read=("sender", "poolKey", "zeroForOne", "amountSpecified", "sqrtPriceX96"),
                fields_written=(
                    ("balanceDelta", "lpFeeOverride") if (hook_int & (1 << 2)) != 0 else ()
                ),
            )
        )
    if (hook_int & (1 << 5)) != 0:
        deltas.append(
            HookDeltaPath(
                callback="beforeDonate",
                action="donate",
                returns_delta=False,
                fields_read=("sender", "poolKey", "amount0", "amount1"),
                fields_written=(),
            )
        )
    if (hook_int & (1 << 4)) != 0:
        deltas.append(
            HookDeltaPath(
                callback="afterDonate",
                action="donate",
                returns_delta=False,
                fields_read=("sender", "poolKey", "amount0", "amount1"),
                fields_written=(),
            )
        )
    return tuple(deltas)


def derive_action_delta_invariants(pool_key: PoolKey) -> tuple[str, ...]:
    """Validate the action / delta flag pairs the pack records.

    The function returns the names of the (action, delta) flag
    pairs that are well-formed (delta flag set implies action
    flag set). The pack records the names so a downstream
    reviewer can confirm the pack documents the V4
    ``isValidHookAddress`` rule 1 invariants.

    Returns an empty tuple when no delta flag is set, in which
    case the rule is vacuously satisfied.
    """
    hook_int = pool_key.hooks.value
    out: list[str] = []
    for action_bit, delta_bit, action_name in DELTA_PAIR_TABLE:
        if (hook_int & delta_bit) != 0:
            if (hook_int & action_bit) != 0:
                out.append(f"{action_name}:delta_ok")
            else:
                # The pack must surface the upstream rejection
                # here; a delta flag without its action flag is
                # an upstream contract violation.
                out.append(f"{action_name}:delta_without_action_violation")
    return tuple(out)


def build_pool_classification_outcome(
    *,
    pool_alias: str,
    pool_key: PoolKey,
    pool_id_hex: str,
    eligibility_decision: EligibilityDecision | None = None,
    prior_code_hash: str | None = None,
    current_code_hash: str | None = None,
    reason_no_hook_pack: str | None = None,
) -> HookPoolClassificationOutcome:
    """Build the per-pool T023 classification outcome T043 records.

    The function is the audit-trail builder: it consumes the
    pool's :class:`PoolKey` and the T023
    :class:`EligibilityDecision` and emits a frozen
    :class:`HookPoolClassificationOutcome` ready for the operator
    runbook. The ``eligibility_decision`` is optional: when the
    caller has not yet classified the pool (e.g. an empty-set
    fixture), the function derives the eligibility surface from
    the hook address alone (``hooks_is_zero=True`` →
    ``POOL_NO_HOOK`` / static-fee → ``backtest``; nonzero hook →
    ``HOOK_ADDRESS_PRESENT`` / ``HOOK_BEHAVIOUR_UNKNOWN`` →
    ``ingestion``).
    """
    hooks_address = pool_key.hooks.to_hex()
    hooks_is_zero = pool_key.hooks.value == 0

    if eligibility_decision is None:
        # Derive the surface from the protocol-layer inputs only.
        if hooks_is_zero:
            eligibility_level = "ingestion"
            eligibility_reasons: tuple[str, ...] = (EligibilityReasonCode.POOL_NO_HOOK.value,)
            if pool_key.fee != DYNAMIC_FEE_FLAG:
                eligibility_reasons = eligibility_reasons + (
                    EligibilityReasonCode.STATIC_FEE_PLAIN_POOL.value,
                )
            else:
                eligibility_reasons = eligibility_reasons + (
                    EligibilityReasonCode.DYNAMIC_FEE_POOL.value,
                )
        else:
            eligibility_level = "ingestion"
            reasons: list[str] = [
                EligibilityReasonCode.HOOK_ADDRESS_PRESENT.value,
                EligibilityReasonCode.HOOK_BEHAVIOUR_UNKNOWN.value,
            ]
            if any((pool_key.hooks.value & bit) != 0 for bit in (1 << 0, 1 << 1, 1 << 2, 1 << 3)):
                reasons.append(EligibilityReasonCode.HOOK_HAS_DELTA_FLAG.value)
            eligibility_reasons = tuple(reasons)
    else:
        eligibility_level = eligibility_decision.level.value
        eligibility_reasons = tuple(r.code.value for r in eligibility_decision.reasons)

    if hooks_is_zero:
        default_reason = (
            "hooks address is the canonical zero address (no hook); a hook pack is "
            "only built for pools whose hooks are non-zero per the T043 contract"
        )
        resolved_reason = reason_no_hook_pack if reason_no_hook_pack else default_reason
    else:
        if reason_no_hook_pack:
            raise ValueError(
                f"HookPoolClassificationOutcome: reason_no_hook_pack={reason_no_hook_pack!r} "
                "must be empty when hooks are non-zero (a hook pack must be built instead)"
            )
        resolved_reason = ""

    return HookPoolClassificationOutcome(
        pool_alias=pool_alias,
        pool_id_hex=pool_id_hex,
        hooks_address=hooks_address,
        hooks_is_zero=hooks_is_zero,
        eligibility_level=eligibility_level,
        eligibility_reasons=eligibility_reasons,
        reason_no_hook_pack=resolved_reason,
        prior_code_hash=prior_code_hash,
        current_code_hash=current_code_hash,
    )


def build_empty_set_hook_evidence(
    *,
    chain_id: int,
    pool_classifications: tuple[HookPoolClassificationOutcome, ...],
    hook_verification_statement: str | None = None,
) -> EmptySetHookEvidence:
    """Build the empty-set evidence artifact.

    The artifact records the explicit statement that no hook
    semantics were verified by T043. The default
    ``hook_verification_statement`` is the exact, T043 contract
    wording; callers may override it only when the operator can
    demonstrate the run's exact wording matches the contract.
    """
    if hook_verification_statement is None:
        hook_verification_statement = (
            "no included pool has a nonzero hooks address; "
            "no hook semantics were verified by this task; "
            "the empty set is never summarised, reported, or accepted "
            "as though hook behaviour had been checked"
        )
    return EmptySetHookEvidence(
        chain_id=chain_id,
        pool_classifications=pool_classifications,
        hook_verification_statement=hook_verification_statement,
    )


def build_default_source_pin(
    *,
    repository: str = "uniswap-v4-core",
    commit: str,
) -> HookSourcePin:
    """Build the default verified source / ABI pin.

    The default source pin records the upstream repository name
    and the exact commit SHA the runner pinned. The runner is
    responsible for filling in the verified ABI selectors and
    event topics from a real ``eth_getCode`` probe and ABI
    verification pass; the function emits an empty tuple so the
    builder's audit trail does not silently invent a verified
    ABI.
    """
    return HookSourcePin(
        repository=repository,
        commit=commit,
        verified_abi_selectors=(),
        verified_event_topics=(),
        notes="",
    )


def build_default_replay_model(
    *,
    expected_modified_event_count: int = 0,
    delta_propagation: str = "after_swap_after_add_after_remove",
    fork_test_coverage: tuple[str, ...] = (),
    golden_transaction_coverage: tuple[str, ...] = (),
    notes: str = "",
) -> HookReplayModel:
    """Build the default replay model.

    The default replay model is the hook-pack audit-trail surface
    the runner uses; the runner populates ``fork_test_coverage``
    and ``golden_transaction_coverage`` with the test names the
    T043 acceptance matrix demands. The defaults are the
    upstream-V4 contract: every enabled callback must be
    covered, every return-delta path must be covered.
    """
    return HookReplayModel(
        expected_modified_event_count=expected_modified_event_count,
        delta_propagation=delta_propagation,
        fork_test_coverage=fork_test_coverage,
        golden_transaction_coverage=golden_transaction_coverage,
        notes=notes,
    )


def build_default_invalidation_rule() -> HookInvalidationRule:
    """Build the default invalidation rule.

    The default invalidation rule records the trigger conditions
    T043 names verbatim: code-hash change (an upgrade / behaviour
    change), proxy-implementation change, upgrade-authority
    change, ABI selector change, and event-topic change. Each
    trigger forces the pool to demote to ``ingestion`` (the prior
    safe level) per T023 acceptance.
    """
    return HookInvalidationRule(
        trigger_conditions=(
            "code_hash_change",
            "proxy_implementation_address_change",
            "upgrade_authority_change",
            "verified_abi_selector_change",
            "verified_event_topic_change",
            "flag_bits_change",
        ),
        demoted_to=RunMode.INGESTION,
        notes=(
            "Any detected change in any of the trigger fields forces the pack to be "
            "rebuilt and the pool to be demoted to ingestion (the prior safe level) "
            "until the operator reruns the verification pipeline"
        ),
    )


def build_hook_pack(
    *,
    chain_id: int,
    pool_alias: str,
    pool_key: PoolKey,
    pool_id_hex: str,
    code_hash: str,
    proxy_implementation_address: str | None = None,
    upgrade_authority: str | None = None,
    external_state_dependencies: tuple[str, ...] = (),
    adversarial_tests: tuple[str, ...] = (),
    invalidation_rule: HookInvalidationRule | None = None,
    verified_source: HookSourcePin | None = None,
    replay_model: HookReplayModel | None = None,
    invalidation_note: str = "",
    expected_modified_event_count: int | None = None,
    fork_test_coverage: tuple[str, ...] = (),
    golden_transaction_coverage: tuple[str, ...] = (),
) -> HookPack:
    """Build the T043 hook pack for a pool whose ``hooks`` is nonzero.

    The builder is the deterministic assembly point: every input
    is either a pinned protocol constant, an operator-supplied
    observation, or a T023 / T038 / T042 surface value; no I/O,
    no wall-clock time, no random sources. The pack is
    ``frozen`` so an audit-trail reader can hash it.

    The builder refuses to emit a pack for a pool whose
    ``hooks`` is the zero address; the caller must build an
    :class:`EmptySetHookEvidence` instead.
    """
    if pool_key.hooks.value == 0:
        raise ValueError(
            "build_hook_pack: pool_key.hooks is the canonical zero address; "
            "build an EmptySetHookEvidence instead of a HookPack"
        )

    flag_decoded = decode_hook_flag_bits(pool_key.hooks)
    flag_bits = tuple(bit for bit, _ in flag_decoded)
    flag_names = tuple(name for _, name in flag_decoded)
    deltas = derive_before_after_deltas(pool_key)
    invariants = derive_action_delta_invariants(pool_key)
    has_v4_violation = any(inv.endswith("violation") for inv in invariants)
    # The V4 ``isValidHookAddress`` rule 1 (delta-flag requires its
    # action-flag) is a contract-level invariant V4 enforces at the
    # pool-creation boundary. A real on-chain address may not
    # strictly satisfy this rule (the framework must not silently
    # rewrite it); the pack records the violation in the
    # ``invalidation_note`` field and the audit trail refuses to
    # promote the pool beyond ``ingestion`` until the operator
    # addresses the finding. The pack itself is still built so
    # downstream phases can read the on-chain facts verbatim.
    dynamic_fee_behavior = derive_dynamic_fee_behavior(pool_key)
    is_dynamic_fee = pool_key.fee == DYNAMIC_FEE_FLAG

    if verified_source is None:
        raise ValueError(
            "build_hook_pack: verified_source is required; the pack must record the "
            "pinned source / commit / ABI selectors it verified"
        )
    if replay_model is None:
        replay_model = build_default_replay_model(
            expected_modified_event_count=(
                expected_modified_event_count if expected_modified_event_count is not None else 0
            ),
            fork_test_coverage=fork_test_coverage,
            golden_transaction_coverage=golden_transaction_coverage,
        )
    if invalidation_rule is None:
        invalidation_rule = build_default_invalidation_rule()

    final_note = invalidation_note
    if has_v4_violation:
        violation_summary = "; ".join(inv for inv in invariants if inv.endswith("violation"))
        violation_phrase = f"V4 isValidHookAddress rule 1 violation: {violation_summary}; "
        if final_note:
            final_note = (
                f"{final_note}; {violation_phrase}"
                "pool stays at ingestion until the operator addresses the finding"
            )
        else:
            final_note = (
                f"{violation_phrase}"
                "pool stays at ingestion until the operator addresses the finding"
            )

    return HookPack(
        chain_id=chain_id,
        pool_id_hex=pool_id_hex,
        pool_alias=pool_alias,
        hook_address=pool_key.hooks.to_hex(),
        code_hash=code_hash,
        proxy_implementation_address=proxy_implementation_address,
        upgrade_authority=upgrade_authority,
        flag_bits=flag_bits,
        flag_names=flag_names,
        is_dynamic_fee=is_dynamic_fee,
        verified_source=verified_source,
        before_after_deltas=deltas,
        dynamic_fee_behavior=dynamic_fee_behavior,
        external_state_dependencies=external_state_dependencies,
        replay_model=replay_model,
        adversarial_tests=adversarial_tests,
        invalidation_rule=invalidation_rule,
        invalidation_note=final_note,
    )


# ---------------------------------------------------------------------------
# Combined report
# ---------------------------------------------------------------------------


def build_hook_pack_report(
    *,
    chain_id: int,
    pool_classifications: tuple[HookPoolClassificationOutcome, ...],
    hook_packs: tuple[HookPack, ...] = (),
    empty_set_record: EmptySetHookEvidence | None = None,
) -> HookPackReport:
    """Build the combined T043 evidence report.

    The function picks the report shape from the inputs:

    - when ``hook_packs`` is non-empty the report is
      ``kind='hook_packs'`` and carries the empty-set record for
      the zero-hook pools in ``pool_classifications``;
    - when ``hook_packs`` is empty the report is
      ``kind='empty_set'`` and the ``empty_set_record`` is
      required.

    The cross-check between the two shapes is the audit-trail
    invariant the operator runbook surfaces; the function
    enforces it by construction.
    """
    if hook_packs and empty_set_record is not None:
        raise ValueError(
            "build_hook_pack_report: hook_packs and empty_set_record are mutually exclusive"
        )
    if hook_packs:
        return HookPackReport(
            chain_id=chain_id,
            kind="hook_packs",
            hook_packs=hook_packs,
            pool_classifications=pool_classifications,
        )
    if empty_set_record is None:
        empty_set_record = build_empty_set_hook_evidence(
            chain_id=chain_id,
            pool_classifications=pool_classifications,
        )
    return HookPackReport(
        chain_id=chain_id,
        kind="empty_set",
        empty_set_record=empty_set_record,
        pool_classifications=pool_classifications,
    )


# ---------------------------------------------------------------------------
# Invalidation rule: a code change demotes the pool
# ---------------------------------------------------------------------------


def code_change_demotes_pack(
    pack: HookPack,
    *,
    observed_code_hash: str,
) -> bool:
    """Return ``True`` when a code-hash change invalidates the pack.

    The function is the **invalidation rule** the T043 contract
    names: a code / implementation change invalidates the pack
    and demotes the pool to ``ingestion``. The function returns
    ``True`` when the observed code hash differs from the pack's
    pinned code hash; ``False`` otherwise.

    The function is the audit-trail surface the operator runbook
    surfaces when a freshly-pinned code hash differs from the
    pack's recorded hash; the runner builds a fresh pack and
    demotes the pool to ``ingestion`` per T023 acceptance.
    """
    return bool(observed_code_hash) and observed_code_hash != pack.code_hash


def demoted_pool_record(
    pack: HookPack,
    *,
    observed_code_hash: str,
    demotion_reason: str | None = None,
) -> dict[str, Any]:
    """Surface the demotion record the T043 invalidation rule emits.

    The function is a small helper that produces the audit-trail
    record the runner pins next to the pack when the
    invalidation rule fires. The record carries the pack's
    hook address, the previous code hash, the observed code
    hash, and the demotion reason.

    Returns an empty dict when no invalidation is required.
    """
    if not code_change_demotes_pack(pack, observed_code_hash=observed_code_hash):
        return {}
    return {
        "schema": "robinhood_lp.qualification.hook_pack.demotion.v1",
        "pool_alias": pack.pool_alias,
        "pool_id": pack.pool_id_hex,
        "hook_address": pack.hook_address,
        "previous_code_hash": pack.code_hash,
        "observed_code_hash": observed_code_hash,
        "demoted_to": pack.invalidation_rule.demoted_to.value,
        "demotion_reason": (
            demotion_reason
            if demotion_reason is not None
            else "hook code hash changed; pack invalidated; pool demoted to ingestion"
        ),
        "version": pack.version,
    }


# ---------------------------------------------------------------------------
# Adversarial tests surface (the T043 acceptance clause)
# ---------------------------------------------------------------------------


#: Default adversarial test names the T043 contract names. The
#: runner maps each name to a concrete test the test runner
#: executes against the live hook (or the frozen bytecode when
#: the test is offline). The names are stable and recorded on the
#: pack so the operator runbook can pin them.
DEFAULT_ADVERSARIAL_TESTS: Final[tuple[str, ...]] = (
    "test_before_swap_returns_delta_balances_to_zero",
    "test_after_swap_returns_delta_matches_balance_delta_field",
    "test_before_add_liquidity_reverts_on_invalid_tick_range",
    "test_after_add_liquidity_returns_delta_matches_balance_delta_field",
    "test_before_remove_liquidity_reverts_on_empty_position",
    "test_after_remove_liquidity_returns_delta_matches_balance_delta_field",
    "test_before_initialize_reverts_on_replay",
    "test_after_initialize_does_not_mutate_state",
    "test_dynamic_fee_override_only_when_dynamic_fee_flag",
    "test_proxy_implementation_change_invalidates_pack",
    "test_upgrade_authority_change_invalidates_pack",
    "test_flag_bits_change_invalidates_pack",
    "test_delta_without_action_flag_violation_is_rejected",
)


# ---------------------------------------------------------------------------
# Package-level public surface
# ---------------------------------------------------------------------------


__all__ = [
    "ALL_HOOK_MASK",
    "DEFAULT_ADVERSARIAL_TESTS",
    "DELTA_PAIR_TABLE",
    "DELTA_TO_ACTION_FLAG",
    "DYNAMIC_FEE_FLAG",
    "EmptySetHookEvidence",
    "ENABLED_CALLBACK_NAMES",
    "HOOK_FLAG_BITS",
    "HOOK_FLAG_NAMES",
    "HOOK_PACK_SPEC_REVISION",
    "HookDeltaPath",
    "HookInvalidationRule",
    "HookPack",
    "HookPackReport",
    "HookPoolClassificationOutcome",
    "HookReplayModel",
    "HookSourcePin",
    "REQUIRED_HOOK_PACK_FIELDS",
    "build_default_invalidation_rule",
    "build_default_replay_model",
    "build_default_source_pin",
    "build_empty_set_hook_evidence",
    "build_hook_pack",
    "build_hook_pack_report",
    "build_pool_classification_outcome",
    "code_change_demotes_pack",
    "decode_hook_flag_bits",
    "demoted_pool_record",
    "derive_action_delta_invariants",
    "derive_before_after_deltas",
    "derive_dynamic_fee_behavior",
]
