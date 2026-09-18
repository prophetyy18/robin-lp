"""Second-pool identity resolution (T038 / ADR-013).

The second pool's identity is pinned by the Owner by its ``PoolId``
only; the remaining ``PoolKey`` fields (``currency0``, ``currency1``,
``fee``, ``tickSpacing``, ``hooks``) and the on-chain ``Initialize``
block are UNKNOWN as of the T038 contract and must be resolved by a
``PoolId``-filtered ``Initialize`` log scan.

The T038 must-not list forbids guessing the second pool's
``PoolKey``, ``fee``, ``tickSpacing`` or ``hooks`` from its
``PoolId``, from a symbol or from another pool's bytecode. This
module is therefore the audit-trail surface the operator runbook
and the per-pool T034 machine report consume: it accepts a
``PoolId``-filtered ``Initialize`` observation (one resolved
``PoolKey`` + the on-chain block it was emitted at) and verifies
the offline keccak256 re-derivation check
``keccak256(abi.encode(PoolKey)) == PoolId``. A mismatch halts
qualification rather than being reconciled by hand.

When the on-chain ``Initialize`` scan reports the block is outside
the candidate window, the resolver records the documented
``pool_init_outside_window`` outcome with the search bounds the
operator observed; it never guesses an ``Initialize`` block from
the symbol / bytecode / cross-pool correlation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from robinhood_lp.protocol import (
    Address,
    Currency,
    PoolId,
    PoolKey,
)
from robinhood_lp.protocol.abi import compute_pool_id
from robinhood_lp.qualification.two_pool_window import (
    OUTCOME_POOL_INIT_OUTSIDE_WINDOW,
    SECOND_POOL_POOL_ID_HEX,
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
#: Contract-defect outcome: the Owner-pinned PoolId is address-sized
#: (20 bytes) rather than PoolId-sized (32 bytes). The keccak256
#: re-derivation check cannot produce a 20-byte output, so the
#: resolver surfaces this outcome with an explicit size-mismatch
#: ``error_detail`` so the audit trail records the contract defect.
RESOLVE_PINNED_POOL_ID_SIZE_DEFECT: Final[str] = "resolve_pinned_pool_id_size_defect"


@dataclass(frozen=True, slots=True)
class ResolvedPoolKey:
    """A second-pool ``PoolKey`` the operator resolved on chain.

    ``chain_id`` is the chain id the Owner pinned. ``pool_id_hex``
    is the Owner-pinned PoolId the ``Initialize`` log reported.
    ``pool_key`` is the resolved ``PoolKey``; ``init_block`` is
    the on-chain block the ``Initialize`` log was emitted at; both
    are operator-side observed values.

    The ``derived_pool_id_hex`` field carries the offline
    keccak256 re-derivation ``keccak256(abi.encode(pool_key))``;
    the resolver compares it to ``pool_id_hex`` and refuses to
    record a mismatch as a successful resolution.
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
        if not s.startswith("0x") or body_len not in (40, 64):
            raise ValueError(
                f"ResolvedPoolKey: pool_id_hex must be 0x + 40 or 64 hex chars, "
                f"got {self.pool_id_hex!r}"
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

    - :data:`RESOLVE_OK` — the resolved ``PoolKey`` matches the
      Owner-pinned ``PoolId`` and the candidate was placed in the
      window plan;
    - :data:`RESOLVE_POOL_ID_MISMATCH` — the offline keccak256
      re-derivation check disagrees with the Owner-pinned
      ``PoolId``. The run halts.
    - :data:`RESOLVE_POOL_KEY_DECODE_ERROR` — the operator
      supplied a ``PoolKey`` whose ``PoolKey.__post_init__`` rejected
      (currency ordering, fee, tick spacing, hooks); the resolver
      surfaces the underlying error rather than silently rewriting
      the field.
    - :data:`RESOLVE_CHAIN_ID_MISMATCH` — the resolved ``PoolKey``
      carries a chain id different from the Owner-pinned one.
    - :data:`RESOLVE_WINDOW_UNRESOLVED` — the operator reported
      the ``Initialize`` block is outside the search bounds; the
      resolver records ``pool_init_outside_window`` rather than
      guessing a value.

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
    pinned_pool_id_hex: str = SECOND_POOL_POOL_ID_HEX,
    pinned_chain_id: int = SECOND_POOL_CHAIN_ID,
) -> SecondPoolResolveResult:
    """Verify the operator-resolved ``PoolKey`` against the Owner-pinned
    ``PoolId`` and chain id.

    The function is deterministic and offline:

    - when ``resolved`` is ``None``, the operator reported the
      ``Initialize`` log was not found inside the search bounds;
      the resolver returns ``resolve_window_unresolved`` with the
      ``search_bounds`` recorded for the audit trail;
    - when ``resolved`` is supplied, the function performs the
      offline keccak256 re-derivation check
      (``derived_pool_id_hex == pinned_pool_id_hex``), the chain
      id match check, and surfaces a structured outcome that the
      planner and the operator runbook consume.

    The resolver never guesses a ``PoolKey`` from the ``PoolId``;
    a non-matching ``PoolKey`` is a hard error that halts the run.

    **Contract-defect surface**: when the Owner-pinned PoolId is
    20-byte address-sized rather than 32-byte keccak256-sized the
    re-derivation check cannot agree by construction (keccak256
    output is 32 bytes). The resolver surfaces this as
    ``resolve_pool_id_mismatch`` with an explicit ``error_detail``
    that names the size mismatch so the audit trail is unambiguous.
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
    # Validate the pinned PoolId shape (40 or 64 hex chars) so a
    # future spec amendment that introduces a third size fails
    # closed here rather than silently passing through the
    # keccak256 re-derivation check.
    pinned_body_len = len(pinned_pool_id_hex.strip().lower()) - 2
    if pinned_body_len == 40:
        # The Owner-pinned PoolId is address-sized (20 bytes) rather
        # than keccak256-sized (32 bytes). The re-derivation check
        # cannot agree by construction; surface the contract defect
        # explicitly so the audit trail records it rather than
        # reporting a generic ``resolve_pool_id_mismatch`` mismatch.
        return SecondPoolResolveResult(
            outcome=RESOLVE_PINNED_POOL_ID_SIZE_DEFECT,
            resolved=None,
            search_bounds=search_bounds,
            error_detail=(
                f"Owner-pinned PoolId {pinned_pool_id_hex!r} is "
                f"{pinned_body_len} hex chars (20 bytes, address-sized); "
                "the V4 PoolId is keccak256(abi.encode(PoolKey)) (32 bytes). "
                "The re-derivation check cannot produce a 20-byte output, "
                "so the size mismatch is a contract defect that the run "
                "halts closed on; raise T038 triage so the Owner can amend "
                "the pinned PoolId or supply a separate address-sized identifier."
            ),
            candidate=candidate,
        )
    if pinned_body_len != 64:
        return SecondPoolResolveResult(
            outcome=RESOLVE_POOL_ID_MISMATCH,
            resolved=None,
            search_bounds=search_bounds,
            error_detail=(
                f"pinned PoolId {pinned_pool_id_hex!r} has body length "
                f"{pinned_body_len}; expected 40 (address-sized) or "
                "64 (keccak256-sized) hex chars"
            ),
            candidate=candidate,
        )
    # Offline keccak256 re-derivation check.
    pinned_id = PoolId.from_hex(pinned_pool_id_hex)
    derived = PoolId.from_hex(resolved.derived_pool_id_hex)
    if derived != pinned_id:
        return SecondPoolResolveResult(
            outcome=RESOLVE_POOL_ID_MISMATCH,
            resolved=None,
            search_bounds=search_bounds,
            error_detail=(
                f"keccak256(abi.encode(PoolKey)) {derived.to_hex()} != "
                f"pinned PoolId {pinned_id.to_hex()}; the resolved PoolKey does not "
                "match the Owner-pinned PoolId; refusing to reconcile by hand"
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
    pinned_pool_id_hex: str = SECOND_POOL_POOL_ID_HEX,
    pinned_chain_id: int = SECOND_POOL_CHAIN_ID,
) -> SecondPoolResolveResult:
    """Convenience wrapper that constructs the resolved ``PoolKey``
    and runs the offline keccak256 re-derivation check.

    The function surfaces the :class:`PoolKey` construction error as
    ``resolve_pool_key_decode_error`` rather than letting it
    propagate, so the planner records a structured failure-path row
    instead of crashing the run with an unhandled exception.

    The ``pinned_pool_id_hex`` and ``pinned_chain_id`` parameters
    default to the Owner-pinned values (which the T038 contract
    pins as a 40-byte address-sized hex); the resolver detects
    the size defect when the candidate's derived PoolId cannot
    match the Owner-pinned value's size.
    """
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
]
