"""Block-pinned StateView spot check at the range end (T036).

The 2026-09-17 routing fact is decisive for this check:

- the primary public endpoint (``robinhood_public``) cannot serve
  historical state through ``eth_call`` at the reference-range
  depth (about 9.4–10.4 million blocks behind the observed head);
- the secondary endpoint (``alchemy_free``) can.

The qualifier issues ``StateView.getSlot0`` and
``StateView.getLiquidity`` to the endpoint that **can** serve the
pinned depth and records the returned golden values. A
``latest`` substitution is refused: when an endpoint reports it
cannot serve the pinned block tag, the qualifier records a
blocked check with the ``cross_endpoint_sample_missing`` reason
code rather than silently falling back to the live chain head.

The module is intentionally endpoint-neutral: the caller supplies
a callable that performs the block-pinned ``eth_call`` and
returns the encoded result, so tests can inject the canonical
secondary response and the blocked response without contacting
any real RPC.

The ``StateViewGoldenValues`` dataclass is the persisted audit
record: it carries the explicit block tag the reads were pinned
to, the endpoint alias the reads were issued to, and the integer
fields the StateView ABI returns (``sqrtPriceX96``, ``tick``,
``lpFee``, ``protocolFee`` for ``getSlot0``; ``uint128`` for
``getLiquidity``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from robinhood_lp.qualification.reference import (
    REFERENCE_COVERAGE_TO_BLOCK,
    ReferenceTarget,
)

#: The two StateView methods the spot check exercises. The names
#: match the StateView ABI; downstream consumers map them to the
#: canonical four-byte selectors (``FUNCTION_SELECTORS["StateView"]``).
STATE_VIEW_METHOD_GET_SLOT_0: Final[str] = "getSlot0"
STATE_VIEW_METHOD_GET_LIQUIDITY: Final[str] = "getLiquidity"


@dataclass(frozen=True, slots=True)
class StateViewGoldenValues:
    """The recorded golden values for the block-pinned spot check.

    ``block_tag`` is the explicit block tag the reads were pinned
    to (``"0x<hex>"`` per JSON-RPC). ``endpoint_alias`` records the
    endpoint the reads were actually issued to — the secondary
    endpoint, because the primary cannot serve historical state at
    the reference depth.
    """

    block_tag: str
    endpoint_alias: str
    get_slot_0_result: bytes
    get_liquidity_result: bytes
    can_record_golden_values: bool = True
    block_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.can_record_golden_values and not self.block_reason:
            raise ValueError(
                "StateViewGoldenValues: can_record_golden_values=False requires a block_reason"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_tag": self.block_tag,
            "endpoint_alias": self.endpoint_alias,
            "get_slot_0_result_hex": "0x" + self.get_slot_0_result.hex(),
            "get_liquidity_result_hex": "0x" + self.get_liquidity_result.hex(),
            "can_record_golden_values": self.can_record_golden_values,
            "block_reason": self.block_reason,
        }


@dataclass(frozen=True, slots=True)
class StateSpotCheckResult:
    """The outcome of the block-pinned StateView spot check."""

    block_tag: str
    endpoint_alias: str
    served: bool
    golden_values: StateViewGoldenValues | None
    block_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_tag": self.block_tag,
            "endpoint_alias": self.endpoint_alias,
            "served": self.served,
            "block_reason": self.block_reason,
            "golden_values": (
                self.golden_values.to_dict() if self.golden_values is not None else None
            ),
        }


#: The callable shape the qualification pipeline uses to perform
#: a block-pinned ``eth_call`` against an endpoint that may or may
#: not be able to serve the historical state at the pinned depth.
#:
#: Implementations must:
#:
#: - call the endpoint at ``block_tag`` with the given StateView
#:   method and ``PoolId`` argument;
#: - return a 32-byte (or longer, for ``getLiquidity``) ``bytes``
#:   value when the endpoint served the call;
#: - return ``None`` when the endpoint cannot serve the pinned
#:   block tag (e.g. archive-state unavailable at depth), in which
#:   case the qualifier records a blocked check with the
#:   ``cross_endpoint_sample_missing`` reason code.
#:
#: Returning a ``latest``-based payload when the pinned tag cannot
#: be served is forbidden and would be visible in the audit trail
#: because the qualifier also records the endpoint alias and block
#: tag.
BlockPinnedStateCall = Callable[[str, str, bytes], bytes | None]


def _default_block_tag(coverage_to_block: int) -> str:
    """Return the default block tag for the spot check.

    The tag is the explicit pinned-block hex form of
    ``coverage_to_block``. The pipeline never falls back to
    ``latest``; when the call cannot be served at this tag the
    qualifier records a blocked check.
    """
    return "0x" + format(coverage_to_block, "x")


def perform_state_spot_check(
    *,
    coverage_to_block: int = REFERENCE_COVERAGE_TO_BLOCK,
    block_pinned_state_call: BlockPinnedStateCall,
    endpoint_alias: str = "alchemy_free",
    pool_id: bytes | None = None,
    reference: ReferenceTarget | None = None,
) -> StateSpotCheckResult:
    """Run the block-pinned StateView spot check.

    The function:

    1. Computes the explicit block tag for ``coverage_to_block``.
    2. Issues ``getSlot0`` to ``block_pinned_state_call`` at the
       pinned tag.
    3. Issues ``getLiquidity`` to ``block_pinned_state_call`` at
       the pinned tag.
    4. Records the golden values when both calls are served, or
       records a blocked check (with reason code
       ``cross_endpoint_sample_missing``) when either call
       returns ``None``.

    Parameters
    ----------
    coverage_to_block:
        The inclusive upper bound of the pinned range. Defaults to
        the reference target's pinned value.
    block_pinned_state_call:
        The callable that performs the ``eth_call`` against the
        chosen endpoint. Returns ``bytes`` on success and ``None``
        when the endpoint cannot serve the pinned tag.
    endpoint_alias:
        The alias of the endpoint the calls were issued to. The
        qualifier records the alias in the audit trail so a
        silent ``latest`` substitution is visible: the alias
        must always be the secondary endpoint (or another
        archive-state-capable endpoint), never ``latest``.
    pool_id:
        Optional override of the ``PoolId`` argument (test seam).
        Defaults to the reference target's ``PoolId``.
    """
    ref = reference if reference is not None else ReferenceTarget()
    block_tag = _default_block_tag(coverage_to_block)
    pool_id_bytes = pool_id if pool_id is not None else ref.pool_id.to_bytes()
    slot0_result = block_pinned_state_call(STATE_VIEW_METHOD_GET_SLOT_0, block_tag, pool_id_bytes)
    liquidity_result = block_pinned_state_call(
        STATE_VIEW_METHOD_GET_LIQUIDITY, block_tag, pool_id_bytes
    )
    if slot0_result is None or liquidity_result is None:
        block_reason = (
            "get_slot_0_unavailable_at_pinned_block"
            if slot0_result is None
            else "get_liquidity_unavailable_at_pinned_block"
        )
        return StateSpotCheckResult(
            block_tag=block_tag,
            endpoint_alias=endpoint_alias,
            served=False,
            golden_values=None,
            block_reason=block_reason,
        )
    golden = StateViewGoldenValues(
        block_tag=block_tag,
        endpoint_alias=endpoint_alias,
        get_slot_0_result=slot0_result,
        get_liquidity_result=liquidity_result,
    )
    return StateSpotCheckResult(
        block_tag=block_tag,
        endpoint_alias=endpoint_alias,
        served=True,
        golden_values=golden,
    )


def make_block_pinned_state_call(
    responses: dict[tuple[str, str, str], bytes | None],
    *,
    default_endpoint: str = "alchemy_free",
) -> BlockPinnedStateCall:
    """Build a deterministic :class:`BlockPinnedStateCall` from a
    mapping.

    The mapping keys are ``(method_name, block_tag, endpoint_alias)``
    tuples; the value is the bytes payload returned by the
    endpoint, or ``None`` to record a blocked check. The returned
    callable binds to ``default_endpoint`` so the spot check knows
    which endpoint the calls were issued to.

    This is the test seam the end-to-end qualification fixture
    uses to inject both a canonical secondary response (serving the
    pinned depth) and a blocked response (primary endpoint cannot
    serve historical state at depth) without contacting any real
    RPC.
    """

    def _call(method: str, block_tag: str, pool_id: bytes) -> bytes | None:
        key = (method, block_tag, default_endpoint)
        if key in responses:
            return responses[key]
        # An unmapped (method, block_tag, alias) tuple records the
        # endpoint as unable to serve the pinned depth.
        return None

    return _call


__all__ = [
    "BlockPinnedStateCall",
    "STATE_VIEW_METHOD_GET_LIQUIDITY",
    "STATE_VIEW_METHOD_GET_SLOT_0",
    "StateSpotCheckResult",
    "StateViewGoldenValues",
    "make_block_pinned_state_call",
    "perform_state_spot_check",
]
