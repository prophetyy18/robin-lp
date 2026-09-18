"""Second-pool identity resolution (T038 / ADR-013).

The second pool's identity is pinned by the Owner by its **hook
contract address** (a 20-byte lookup signal), NOT by its V4
``PoolId``. The remaining ``PoolKey`` fields (``currency0``,
``currency1``, ``fee``, ``tickSpacing``) and the 32-byte V4
``PoolId`` and the on-chain ``Initialize`` block are UNKNOWN as of
the T038 contract (per the 2026-09-18 amendment) and must be
resolved on chain by an ``Initialize`` log scan filtered by
``hooks == <pinned hook contract address>``.

This module is the audit-trail surface the operator runbook and
the per-pool T034 machine report consume: the primary entry point
:func:`resolve_second_pool_via_hook_scan` scans operator-supplied
``Initialize`` logs whose decoded ``hooks`` field equals the pinned
hook contract address; for each match it decodes the ``PoolKey``
and verifies the offline keccak256 re-derivation check
``keccak256(abi.encode(PoolKey)) == PoolId`` (where ``PoolId`` is
the 32-byte keccak256 digest the chain emits). A mismatch halts
qualification rather than being reconciled by hand.

When the on-chain ``Initialize`` scan reports no hook-matched
``Initialize`` is found inside the candidate window, the resolver
records the documented ``pool_init_outside_window`` outcome with
the search bounds the operator observed; it never guesses an
``Initialize`` block from the hook address / symbol / bytecode /
cross-pool correlation.

Defensive guard: :func:`resolve_second_pool_identity` keeps its
``RESOLVE_PINNED_POOL_ID_SIZE_DEFECT`` branch as a guard against
attempt-1-style misuse that fed the 20-byte hook address as a V4
``PoolId``. The error detail names the new semantics so the audit
trail surfaces the configuration error rather than silently
treating the 20-byte value as a ``PoolId``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from robinhood_lp.discovery.initialize_log import DecodedInitialize
from robinhood_lp.protocol import (
    Address,
    Currency,
    PoolId,
    PoolKey,
)
from robinhood_lp.protocol.abi import compute_pool_id
from robinhood_lp.qualification.two_pool_window import (
    OUTCOME_POOL_INIT_OUTSIDE_WINDOW,
    SECOND_POOL_HOOK_ADDRESS_HEX,
    TWO_POOL_CHAIN_ID,
    TwoPoolCandidate,
    second_pool_candidate,
)

# ---------------------------------------------------------------------------
# Resolve result
# ---------------------------------------------------------------------------


#: The chain id the Owner pinned the second pool under. The
#: T038 contract names ``4663`` (Robinhood Chain mainnet); the
#: resolver never accepts a different chain id.
SECOND_POOL_CHAIN_ID: Final[int] = TWO_POOL_CHAIN_ID

#: Outcome codes the second-pool resolver emits.
RESOLVE_OK: Final[str] = "resolve_ok"
RESOLVE_POOL_ID_MISMATCH: Final[str] = "resolve_pool_id_mismatch"
RESOLVE_POOL_KEY_DECODE_ERROR: Final[str] = "resolve_pool_key_decode_error"
RESOLVE_CHAIN_ID_MISMATCH: Final[str] = "resolve_chain_id_mismatch"
RESOLVE_WINDOW_UNRESOLVED: Final[str] = "resolve_window_unresolved"
#: Multiple ``Initialize`` logs matched the pinned hook address —
#: the hook address lookup signal is ambiguous; the resolver halts
#: closed rather than picking one without operator intervention.
RESOLVE_HOOK_ADDRESS_AMBIGUOUS: Final[str] = "resolve_hook_address_ambiguous"
#: Defensive-guard outcome: a caller fed the 20-byte hook address
#: as a V4 ``PoolId`` (the attempt-1 framing). Per the 2026-09-18
#: amendment the T038 contract treats the pinned value as a hook
#: contract address (a lookup signal), not as a V4 ``PoolId``
#: (which is a 32-byte keccak256 digest emitted on chain). The
#: resolver surfaces this outcome with an explicit
#: configuration-error ``error_detail`` so the audit trail records
#: the misuse rather than silently producing a wrong answer.
RESOLVE_PINNED_POOL_ID_SIZE_DEFECT: Final[str] = "resolve_pinned_pool_id_size_defect"


@dataclass(frozen=True, slots=True)
class ResolvedPoolKey:
    """A second-pool ``PoolKey`` the operator resolved on chain.

    ``chain_id`` is the chain id the Owner pinned (Robinhood Chain
    mainnet, ``4663``). ``pool_id_hex`` is the **resolved** 32-byte
    V4 ``PoolId`` the chain emitted in the matching ``Initialize``
    log (i.e. ``keccak256(abi.encode(PoolKey))``), NOT the
    Owner-pinned 20-byte hook contract address. ``pool_key`` is
    the resolved ``PoolKey``; ``init_block`` is the on-chain block
    the ``Initialize`` log was emitted at; both are operator-side
    observed values.

    The ``derived_pool_id_hex`` field carries the offline keccak256
    re-derivation ``keccak256(abi.encode(pool_key))``; the resolver
    compares it to ``pool_id_hex`` and refuses to record a mismatch
    as a successful resolution.
    """

    chain_id: int
    pool_id_hex: str
    pool_key: PoolKey
    init_block: int
    block_hash: str | None = None
    tx_hash: str | None = None
    log_index: int | None = None
    derived_pool_id_hex: str = ""

    def __post_init__(self) -> None:
        if self.chain_id <= 0:
            raise ValueError(f"ResolvedPoolKey: chain_id must be positive, got {self.chain_id}")
        if self.init_block < 0:
            raise ValueError(f"ResolvedPoolKey: init_block must be >= 0, got {self.init_block}")
        s = self.pool_id_hex.strip().lower()
        body_len = len(s) - 2 if s.startswith("0x") else len(s)
        if not s.startswith("0x") or body_len != 64:
            raise ValueError(
                f"ResolvedPoolKey: pool_id_hex must be 0x + 64 hex chars (32-byte V4 PoolId), "
                f"got {self.pool_id_hex!r}; the T038 amendment treats the 20-byte Owner-pinned "
                "value as a hook contract address (lookup signal), not as a PoolId"
            )
        if self.block_hash is not None:
            bh = self.block_hash.strip().lower()
            if not bh.startswith("0x") or len(bh) != 2 + 2 * 32:
                raise ValueError(
                    f"ResolvedPoolKey: block_hash must be 0x + 64 hex chars when provided, "
                    f"got {self.block_hash!r}"
                )

    @property
    def pool_id(self) -> PoolId:
        return PoolId.from_hex(self.pool_id_hex)

    @property
    def hooks_is_zero(self) -> bool:
        """True iff ``hooks`` is the canonical zero address.

        The T038 contract classifies a nonzero hook pool as not
        exceeding the ``ingestion`` support level until a
        T043-level evidence pack exists. The runner surfaces this
        bit in the per-pool T034 machine report so the verifier can
        reject a pool that has been promoted beyond its
        classification.
        """
        return self.pool_key.hooks.value == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "pool_id_hex": self.pool_id_hex,
            "currency0_address": self.pool_key.currency0.to_address().to_hex(),
            "currency1_address": self.pool_key.currency1.to_address().to_hex(),
            "fee": int(self.pool_key.fee),
            "tick_spacing": int(self.pool_key.tick_spacing),
            "hooks_address": self.pool_key.hooks.to_hex(),
            "init_block": int(self.init_block),
            "block_hash": self.block_hash,
            "tx_hash": self.tx_hash,
            "log_index": self.log_index,
            "derived_pool_id_hex": self.derived_pool_id_hex
            or ("0x" + compute_pool_id(self.pool_key).to_bytes(32, "big").hex()),
            "hooks_is_zero": self.hooks_is_zero,
        }


@dataclass(frozen=True, slots=True)
class SecondPoolResolveResult:
    """The deterministic outcome of the second-pool resolver.

    ``outcome`` is one of:

    - :data:`RESOLVE_OK` — the on-chain ``Initialize`` log scan
      matched the pinned hook contract address; the resolved
      ``PoolKey``'s keccak256 re-derivation equals the chain-emitted
      ``PoolId``; the candidate is placed in the window plan;
    - :data:`RESOLVE_WINDOW_UNRESOLVED` — no hook-matched
      ``Initialize`` was found inside the operator-supplied
      ``search_bounds``; the resolver records
      ``pool_init_outside_window`` with the search bounds and
      excludes the second pool rather than guessing;
    - :data:`RESOLVE_POOL_ID_MISMATCH` — the offline keccak256
      re-derivation check disagrees with the chain-emitted
      ``PoolId`` of the matched log. The run halts.
    - :data:`RESOLVE_POOL_KEY_DECODE_ERROR` — the operator
      supplied a ``PoolKey`` whose ``PoolKey.__post_init__`` rejected
      (currency ordering, fee, tick spacing, hooks); the resolver
      surfaces the underlying error rather than silently rewriting
      the field.
    - :data:`RESOLVE_CHAIN_ID_MISMATCH` — the resolved ``PoolKey``
      carries a chain id different from the Owner-pinned one.
    - :data:`RESOLVE_HOOK_ADDRESS_AMBIGUOUS` — multiple
      ``Initialize`` logs matched the pinned hook contract
      address; the resolver halts closed rather than picking one
      without operator intervention.
    - :data:`RESOLVE_PINNED_POOL_ID_SIZE_DEFECT` — defensive guard:
      a caller fed the 20-byte hook contract address as a V4
      ``PoolId``. The T038 amendment treats the pinned value as a
      hook contract address (lookup signal), not as a V4 ``PoolId``
      (which is 32 bytes); the resolver surfaces the configuration
      error explicitly.

    ``search_bounds`` records the candidate window the operator
    searched when ``outcome == pool_init_outside_window``; it is
    empty otherwise. ``resolved`` carries the operator-supplied
    ``PoolKey`` and ``init_block`` when ``outcome == resolve_ok``.
    """

    outcome: str
    resolved: ResolvedPoolKey | None = None
    search_bounds: tuple[int, int] | None = None
    error_detail: str = ""
    candidate: TwoPoolCandidate = field(default_factory=lambda: second_pool_candidate(None))

    def __post_init__(self) -> None:
        if self.outcome not in (
            RESOLVE_OK,
            RESOLVE_POOL_ID_MISMATCH,
            RESOLVE_POOL_KEY_DECODE_ERROR,
            RESOLVE_CHAIN_ID_MISMATCH,
            RESOLVE_WINDOW_UNRESOLVED,
            RESOLVE_HOOK_ADDRESS_AMBIGUOUS,
            RESOLVE_PINNED_POOL_ID_SIZE_DEFECT,
        ):
            raise ValueError(
                f"SecondPoolResolveResult: outcome must be a documented outcome, "
                f"got {self.outcome!r}"
            )
        if self.outcome == RESOLVE_OK and self.resolved is None:
            raise ValueError(
                "SecondPoolResolveResult: outcome=resolve_ok requires a resolved PoolKey"
            )
        if self.outcome != RESOLVE_OK and self.resolved is not None:
            raise ValueError(
                f"SecondPoolResolveResult: outcome={self.outcome!r} must not carry a resolved PoolKey"
            )

    @property
    def ok(self) -> bool:
        return self.outcome == RESOLVE_OK

    @property
    def pool_init_outside_window(self) -> bool:
        return self.outcome == RESOLVE_WINDOW_UNRESOLVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "search_bounds": (
                [int(self.search_bounds[0]), int(self.search_bounds[1])]
                if self.search_bounds is not None
                else None
            ),
            "error_detail": self.error_detail,
            "candidate": {
                "pool_alias": self.candidate.pool_alias,
                "pool_id_hex": self.candidate.pool_id_hex,
                "pool_init_block": self.candidate.pool_init_block,
            },
            "resolved": self.resolved.to_dict() if self.resolved is not None else None,
        }


# ---------------------------------------------------------------------------
# Resolve function
# ---------------------------------------------------------------------------


def build_resolved_pool_key(
    *,
    chain_id: int,
    pool_id_hex: str,
    currency0_address: str,
    currency1_address: str,
    fee: int,
    tick_spacing: int,
    hooks_address: str,
    init_block: int,
    block_hash: str | None = None,
    tx_hash: str | None = None,
    log_index: int | None = None,
) -> ResolvedPoolKey:
    """Build a :class:`ResolvedPoolKey` and pre-compute the
    offline keccak256 re-derivation hash.

    The function does not perform the pool-id match check; it only
    validates the structural fields and surfaces the derived
    hash. The match check is the next step in
    :func:`resolve_second_pool_identity`.
    """
    # Currency ordering: PoolKey enforces currency0 < currency1 as
    # uint160. The resolver surfaces a clear ``ValueError`` when the
    # operator-supplied ordering is wrong rather than silently
    # reordering the currencies (which would change the keccak256
    # digest and silently invalidate the comparison against the
    # Owner-pinned PoolId).
    currency0 = Currency.from_hex(currency0_address)
    currency1 = Currency.from_hex(currency1_address)
    hooks = Address.from_hex(hooks_address)
    try:
        pool_key = PoolKey(
            currency0=currency0,
            currency1=currency1,
            fee=int(fee),
            tick_spacing=int(tick_spacing),
            hooks=hooks,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"build_resolved_pool_key: PoolKey construction rejected the resolved fields: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    derived = compute_pool_id(pool_key)
    return ResolvedPoolKey(
        chain_id=int(chain_id),
        pool_id_hex=str(pool_id_hex),
        pool_key=pool_key,
        init_block=int(init_block),
        block_hash=block_hash,
        tx_hash=tx_hash,
        log_index=log_index,
        derived_pool_id_hex="0x" + derived.to_bytes(32, "big").hex(),
    )


def resolve_second_pool_identity(
    *,
    resolved: ResolvedPoolKey | None,
    search_bounds: tuple[int, int] | None = None,
    error_detail: str = "",
    pinned_pool_id_hex: str | None = None,
    pinned_chain_id: int = SECOND_POOL_CHAIN_ID,
) -> SecondPoolResolveResult:
    """Verify the operator-resolved ``PoolKey`` against the chain id and
    (when supplied) an externally-pinned ``PoolId``.

    The function is deterministic and offline:

    - when ``resolved`` is ``None``, the operator reported the
      ``Initialize`` log was not found inside the search bounds;
      the resolver returns ``resolve_window_unresolved`` with the
      ``search_bounds`` recorded for the audit trail;
    - when ``resolved`` is supplied, the function performs the
      offline keccak256 re-derivation check
      (``resolved.derived_pool_id_hex == resolved.pool_id_hex``),
      the chain id match check, and surfaces a structured outcome
      that the planner and the operator runbook consume.

    The resolver never guesses a ``PoolKey`` from the ``PoolId``;
    a non-matching ``PoolKey`` is a hard error that halts the run.

    **Defensive guard** (``pinned_pool_id_hex``): the 2026-09-18
    T038 amendment treats the Owner-pinned value
    ``0xEd50bDeeA8aDC232f159486192a4157281D722ff`` as a hook
    contract address (a 20-byte lookup signal), NOT as a V4
    ``PoolId`` (which is the 32-byte keccak256 digest the chain
    emits). When a caller explicitly passes a 20-byte (address-sized)
    hex as the ``pinned_pool_id_hex``, the resolver surfaces
    :data:`RESOLVE_PINNED_POOL_ID_SIZE_DEFECT` with an
    ``error_detail`` that names the configuration error so the audit
    trail records the misuse rather than silently producing a wrong
    answer. The check is retained as a defensive guard only; new
    code should use :func:`resolve_second_pool_via_hook_scan` and
    leave ``pinned_pool_id_hex`` at its ``None`` default.
    """
    if resolved is None:
        candidate = second_pool_candidate(None)
        return SecondPoolResolveResult(
            outcome=RESOLVE_WINDOW_UNRESOLVED,
            resolved=None,
            search_bounds=search_bounds,
            error_detail=error_detail or "operator reported Initialize log not found",
            candidate=candidate,
        )
    candidate = second_pool_candidate(resolved.init_block)
    if resolved.chain_id != pinned_chain_id:
        return SecondPoolResolveResult(
            outcome=RESOLVE_CHAIN_ID_MISMATCH,
            resolved=None,
            search_bounds=search_bounds,
            error_detail=(
                f"resolved chain_id {resolved.chain_id} does not match pinned chain_id "
                f"{pinned_chain_id}"
            ),
            candidate=candidate,
        )
    # Defensive guard: a caller fed the Owner-pinned 20-byte hook
    # contract address as a V4 ``PoolId``. The 2026-09-18 T038
    # amendment treats the pinned value as a hook contract address
    # (a lookup signal), not as a V4 ``PoolId`` (32 bytes). Surface
    # the configuration error explicitly so the audit trail records
    # it; the resolver does not retry, fall back, or guess.
    if pinned_pool_id_hex is not None:
        pinned_body_len = len(pinned_pool_id_hex.strip().lower()) - 2
        if pinned_body_len == 40:
            return SecondPoolResolveResult(
                outcome=RESOLVE_PINNED_POOL_ID_SIZE_DEFECT,
                resolved=None,
                search_bounds=search_bounds,
                error_detail=(
                    f"pinned_pool_id_hex {pinned_pool_id_hex!r} is "
                    f"{pinned_body_len} hex chars (20 bytes, address-sized); "
                    "per the 2026-09-18 T038 amendment the Owner-pinned value "
                    "is a hook contract address (a 20-byte lookup signal), "
                    "NOT a V4 PoolId (which is keccak256(abi.encode(PoolKey)) "
                    "and 32 bytes). Use resolve_second_pool_via_hook_scan() to "
                    "scan Initialize logs filtered by the pinned hook address; "
                    "do not feed the hook address as a PoolId."
                ),
                candidate=candidate,
            )
        if pinned_body_len != 64:
            return SecondPoolResolveResult(
                outcome=RESOLVE_POOL_ID_MISMATCH,
                resolved=None,
                search_bounds=search_bounds,
                error_detail=(
                    f"pinned_pool_id_hex {pinned_pool_id_hex!r} has body length "
                    f"{pinned_body_len}; expected 64 (keccak256-sized) hex chars "
                    "or None to disable the external-pinned check"
                ),
                candidate=candidate,
            )
    # Internal consistency: derived PoolId must equal the chain-emitted
    # PoolId the resolver was supplied.
    derived = PoolId.from_hex(resolved.derived_pool_id_hex)
    chain_pool_id = PoolId.from_hex(resolved.pool_id_hex)
    if derived != chain_pool_id:
        return SecondPoolResolveResult(
            outcome=RESOLVE_POOL_ID_MISMATCH,
            resolved=None,
            search_bounds=search_bounds,
            error_detail=(
                f"keccak256(abi.encode(PoolKey)) {derived.to_hex()} != "
                f"chain-emitted PoolId {chain_pool_id.to_hex()}; the resolved PoolKey "
                "is internally inconsistent with the chain-emitted PoolId; "
                "refusing to reconcile by hand"
            ),
            candidate=candidate,
        )
    # Optional legacy check: when a 64-byte ``pinned_pool_id_hex`` is
    # supplied, it must equal the resolved PoolId. The 2026-09-18
    # amendment removes the default 20-byte pin; callers that need a
    # separate cross-pool identity to chain-match must pass an explicit
    # 32-byte hex here.
    if pinned_pool_id_hex is not None:
        external_pool_id = PoolId.from_hex(pinned_pool_id_hex)
        if external_pool_id != chain_pool_id:
            return SecondPoolResolveResult(
                outcome=RESOLVE_POOL_ID_MISMATCH,
                resolved=None,
                search_bounds=search_bounds,
                error_detail=(
                    f"external pinned_pool_id_hex keccak256 {external_pool_id.to_hex()} "
                    f"!= keccak256(abi.encode(PoolKey)) {chain_pool_id.to_hex()}; the "
                    "resolved PoolKey does not match the externally-pinned PoolId; "
                    "refusing to reconcile by hand"
                ),
                candidate=candidate,
            )
    return SecondPoolResolveResult(
        outcome=RESOLVE_OK,
        resolved=resolved,
        search_bounds=search_bounds,
        error_detail=error_detail,
        candidate=candidate,
    )


def resolve_second_pool_via_hook_scan(
    *,
    pinned_hook_address: Address | str | None = None,
    initialize_logs: Sequence[DecodedInitialize] = (),
    chain_id: int = SECOND_POOL_CHAIN_ID,
    search_bounds: tuple[int, int] | None = None,
    error_detail: str = "",
) -> SecondPoolResolveResult:
    """Resolve the second pool by scanning ``Initialize`` logs whose
    decoded ``hooks`` field equals the Owner-pinned hook contract
    address.

    Per the 2026-09-18 T038 amendment, the Owner-pinned value
    ``0xEd50bDeeA8aDC232f159486192a4157281D722ff`` is a **hook
    contract address** (a 20-byte lookup signal), NOT a V4
    ``PoolId``. The remaining ``PoolKey`` fields
    (``currency0``, ``currency1``, ``fee``, ``tickSpacing``) and
    the 32-byte ``PoolId`` (the keccak256 digest the chain emits)
    and the ``Initialize`` block are UNKNOWN as of this contract
    and are resolved here from an ``Initialize`` log scan filtered
    by ``hooks == <pinned hook contract address>``.

    The function is deterministic and offline: every input is
    either an operator-supplied observation or a pinned default;
    no I/O, no wall clock, no random sources.

    Algorithm:

    1. Default the pinned hook address to the Owner-pinned value
       (:data:`SECOND_POOL_HOOK_ADDRESS_HEX`) when not supplied.
    2. Iterate the operator-supplied ``initialize_logs`` (each one
       is a successfully-decoded ``Initialize`` event). For each
       log, compare ``log.pool_key.hooks`` to the pinned hook
       address case-insensitively (uint160 equality).
    3. Take the first hook-matched log. If more than one log
       matches, return :data:`RESOLVE_HOOK_ADDRESS_AMBIGUOUS`
       rather than picking one without operator intervention.
    4. Build a :class:`ResolvedPoolKey` from the matched log; the
       ``pool_id_hex`` is the chain-emitted 32-byte keccak256
       digest; ``derived_pool_id_hex`` is the offline
       keccak256 re-derivation of ``PoolKey``. The decoder
       (``robinhood_lp.discovery.initialize_log``) enforces the
       equality between the two; the resolver re-verifies the
       equality defensively.
    5. Hand off to :func:`resolve_second_pool_identity` for the
       chain-id match check and the internal-consistency
       re-verification. The ``pinned_pool_id_hex`` defensive guard
       is bypassed by passing ``None`` (the hook-scan resolver is
       the new entry point; old attempt-1 misuse patterns cannot
       reach it).

    When no hook-matched ``Initialize`` is found inside the
    candidate window, the resolver returns
    :data:`RESOLVE_WINDOW_UNRESOLVED` with the ``search_bounds``
    recorded; the second pool is excluded rather than the resolver
    guessing values.
    """
    # Default the pinned hook address to the Owner-pinned value.
    if pinned_hook_address is None:
        pinned_hook_address = Address.from_hex(SECOND_POOL_HOOK_ADDRESS_HEX)
    elif isinstance(pinned_hook_address, str):
        pinned_hook_address = Address.from_hex(pinned_hook_address)

    pinned_hook_int = pinned_hook_address.value

    matches: list[DecodedInitialize] = []
    for log in initialize_logs:
        if log.pool_key.hooks.value == pinned_hook_int:
            matches.append(log)

    if len(matches) > 1:
        candidate = second_pool_candidate(None)
        match_pool_ids = ", ".join(m.pool_id.to_hex() for m in matches)
        return SecondPoolResolveResult(
            outcome=RESOLVE_HOOK_ADDRESS_AMBIGUOUS,
            resolved=None,
            search_bounds=search_bounds,
            error_detail=(
                f"hook address {pinned_hook_address.to_hex()} matched "
                f"{len(matches)} Initialize logs (PoolIds: {match_pool_ids}); "
                "the lookup signal is ambiguous; the resolver halts closed "
                "rather than picking one without operator intervention"
            ),
            candidate=candidate,
        )

    if not matches:
        candidate = second_pool_candidate(None)
        bounds_detail = (
            f" inside search bounds {search_bounds!r}" if search_bounds is not None else ""
        )
        return SecondPoolResolveResult(
            outcome=RESOLVE_WINDOW_UNRESOLVED,
            resolved=None,
            search_bounds=search_bounds,
            error_detail=(
                error_detail
                or (
                    f"no Initialize log matched hooks == {pinned_hook_address.to_hex()}"
                    f"{bounds_detail}; the resolver excludes the second pool "
                    "rather than guessing an Initialize block"
                )
            ),
            candidate=candidate,
        )

    matched = matches[0]
    # Build the ResolvedPoolKey from the matched log. The decoder
    # already enforced ``keccak256(abi.encode(PoolKey)) == pool_id``;
    # the resolver's internal consistency check in
    # ``resolve_second_pool_identity`` re-verifies it defensively.
    resolved = ResolvedPoolKey(
        chain_id=chain_id,
        pool_id_hex=matched.pool_id.to_hex(),
        pool_key=matched.pool_key,
        init_block=matched.block_number if matched.block_number is not None else 0,
        block_hash=None,
        tx_hash=matched.tx_hash,
        log_index=matched.log_index,
        derived_pool_id_hex="0x" + compute_pool_id(matched.pool_key).to_bytes(32, "big").hex(),
    )
    return resolve_second_pool_identity(
        resolved=resolved,
        search_bounds=search_bounds,
        pinned_pool_id_hex=None,
        pinned_chain_id=chain_id,
    )


def build_second_pool_resolve_result_from_resolved_fields(
    *,
    chain_id: int,
    pool_id_hex: str,
    currency0_address: str,
    currency1_address: str,
    fee: int,
    tick_spacing: int,
    hooks_address: str,
    init_block: int,
    block_hash: str | None = None,
    tx_hash: str | None = None,
    log_index: int | None = None,
    search_bounds: tuple[int, int] | None = None,
    pinned_pool_id_hex: str | None = None,
    pinned_chain_id: int = SECOND_POOL_CHAIN_ID,
) -> SecondPoolResolveResult:
    """Convenience wrapper that constructs the resolved ``PoolKey``
    and runs the offline keccak256 re-derivation check.

    The function surfaces the :class:`PoolKey` construction error as
    ``resolve_pool_key_decode_error`` rather than letting it
    propagate, so the planner records a structured failure-path row
    instead of crashing the run with an unhandled exception.

    Defensive guard: a 20-byte (address-sized) ``pool_id_hex`` is
    surfaced as :data:`RESOLVE_PINNED_POOL_ID_SIZE_DEFECT` because
    the T038 amendment treats the Owner-pinned 20-byte value as a
    hook contract address (a lookup signal), not as a V4 ``PoolId``
    (32 bytes). Callers that need the hook-scan path should use
    :func:`resolve_second_pool_via_hook_scan` instead.
    """
    # Defensive guard for the wrapper: a 20-byte ``pool_id_hex``
    # signals the attempt-1 misuse pattern (feeding the hook address
    # as a PoolId). Surface it before construction so the audit
    # trail names the configuration error.
    pool_id_body_len = len(pool_id_hex.strip().lower()) - 2
    if pool_id_body_len == 40:
        candidate = second_pool_candidate(None)
        return SecondPoolResolveResult(
            outcome=RESOLVE_PINNED_POOL_ID_SIZE_DEFECT,
            resolved=None,
            search_bounds=search_bounds,
            error_detail=(
                f"pool_id_hex {pool_id_hex!r} is {pool_id_body_len} hex chars "
                "(20 bytes, address-sized); per the 2026-09-18 T038 amendment "
                "the Owner-pinned value is a hook contract address (a 20-byte "
                "lookup signal), NOT a V4 PoolId (which is 32 bytes). "
                "Use resolve_second_pool_via_hook_scan() instead."
            ),
            candidate=candidate,
        )
    try:
        resolved = build_resolved_pool_key(
            chain_id=chain_id,
            pool_id_hex=pool_id_hex,
            currency0_address=currency0_address,
            currency1_address=currency1_address,
            fee=fee,
            tick_spacing=tick_spacing,
            hooks_address=hooks_address,
            init_block=init_block,
            block_hash=block_hash,
            tx_hash=tx_hash,
            log_index=log_index,
        )
    except (TypeError, ValueError) as exc:
        candidate = second_pool_candidate(None)
        return SecondPoolResolveResult(
            outcome=RESOLVE_POOL_KEY_DECODE_ERROR,
            resolved=None,
            search_bounds=search_bounds,
            error_detail=f"{type(exc).__name__}: {exc}",
            candidate=candidate,
        )
    return resolve_second_pool_identity(
        resolved=resolved,
        search_bounds=search_bounds,
        pinned_pool_id_hex=pinned_pool_id_hex,
        pinned_chain_id=pinned_chain_id,
    )


# ---------------------------------------------------------------------------
# Hook classification
# ---------------------------------------------------------------------------


#: The T023 hook-classification outcomes the T038 contract
#: surfaces for the second pool when ``hooks != 0x0``. The support
#: level the second pool may carry must not exceed ``ingestion``
#: until a T043-level evidence pack exists.
SUPPORT_LEVEL_INGESTION: Final[str] = "ingestion"
SUPPORT_LEVEL_REJECTED: Final[str] = "rejected"


def classify_second_pool_support_level(resolved: ResolvedPoolKey) -> str:
    """Classify the second pool's T023 support level.

    The contract sets a hard ceiling at ``ingestion`` until a
    T043-level evidence pack exists; a hook pool (i.e. one whose
    ``hooks`` is not the canonical zero address) is reported as
    ``ingestion`` here even when the unknown semantics would
    otherwise warrant a lower level, so the contract surfaces the
    ceiling the operator must respect before promoting the pool.
    The classification never exceeds ``ingestion`` here; the
    :data:`SUPPORT_LEVEL_REJECTED` outcome is reserved for the
    case where the structural validation rejected the resolved
    PoolKey.
    """
    if not resolved.hooks_is_zero:
        # Nonzero hook — the T038 contract pins the support level at
        # ``ingestion`` until a T043 evidence pack exists. We never
        # report a higher level from this classification.
        return SUPPORT_LEVEL_INGESTION
    return SUPPORT_LEVEL_INGESTION


__all__ = [
    "OUTCOME_POOL_INIT_OUTSIDE_WINDOW",
    "RESOLVE_CHAIN_ID_MISMATCH",
    "RESOLVE_HOOK_ADDRESS_AMBIGUOUS",
    "RESOLVE_OK",
    "RESOLVE_POOL_ID_MISMATCH",
    "RESOLVE_POOL_KEY_DECODE_ERROR",
    "RESOLVE_WINDOW_UNRESOLVED",
    "RESOLVE_PINNED_POOL_ID_SIZE_DEFECT",
    "SECOND_POOL_CHAIN_ID",
    "SUPPORT_LEVEL_INGESTION",
    "SUPPORT_LEVEL_REJECTED",
    "ResolvedPoolKey",
    "SecondPoolResolveResult",
    "build_resolved_pool_key",
    "build_second_pool_resolve_result_from_resolved_fields",
    "classify_second_pool_support_level",
    "resolve_second_pool_identity",
    "resolve_second_pool_via_hook_scan",
]
