"""PoolId re-derivation check (T036).

The pinned reference target carries a ``PoolId`` that the Owner
recorded against the V4 ``PoolId.sol::toId`` derivation rule
(``keccak256(abi.encode(PoolKey))``). The qualification pipeline
re-derives the ``PoolId`` from the pinned ``PoolKey`` and compares
it against the pinned ``PoolId``. A mismatch halts qualification:
a dataset whose ``PoolId`` is not the canonical derivation cannot
be trusted because subsequent reconstruction, identity comparison,
and EventKey-based comparison would refer to a different on-chain
entity.

The check is intentionally a separate module so the qualification
report can surface its outcome as a distinct finding (the
``metadata_failure`` reason code on a mismatch) without conflating
it with the baseline-comparison or fidelity-check findings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from robinhood_lp.protocol import PoolId, PoolKey
from robinhood_lp.qualification.reference import (
    ReferenceTarget,
    build_reference_pool_key,
)


@dataclass(frozen=True, slots=True)
class PoolIdCheckResult:
    """The outcome of the PoolId re-derivation check.

    ``derived_pool_id`` is always populated; it equals the
    ``keccak256(abi.encode(pool_key))`` derivation. ``matches_pinned``
    is ``True`` iff the derived ``PoolId`` equals the pinned
    reference ``PoolId``. ``pinned_pool_id`` is the reference target's
    pinned value the derived ``PoolId`` was compared against.
    """

    derived_pool_id: PoolId
    pinned_pool_id: PoolId
    matches_pinned: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "derived_pool_id": self.derived_pool_id.to_hex(),
            "pinned_pool_id": self.pinned_pool_id.to_hex(),
            "matches_pinned": self.matches_pinned,
        }


def check_pool_id_derivation(
    pool_key: PoolKey | None = None,
    reference: ReferenceTarget | None = None,
    *,
    pinned_pool_id: PoolId | None = None,
) -> PoolIdCheckResult:
    """Re-derive the ``PoolId`` from ``pool_key`` and compare to the
    pinned reference.

    Parameters
    ----------
    pool_key:
        The ``PoolKey`` to re-derive from. When ``None``, the pinned
        reference ``PoolKey`` is used; this is the default and the
        production path — the qualification pipeline re-derives from
        the same ``PoolKey`` the dataset is pinned to.
    reference:
        The pinned reference target. When ``None``,
        :data:`REFERENCE_TARGET` is used.
    pinned_pool_id:
        An explicit pinned ``PoolId`` override (test seam). When
        ``None``, the reference target's pinned ``PoolId`` is used.
    """
    ref = reference if reference is not None else ReferenceTarget()
    pk = pool_key if pool_key is not None else build_reference_pool_key()
    derived = pk.to_pool_id()
    pinned = pinned_pool_id if pinned_pool_id is not None else ref.pool_id
    return PoolIdCheckResult(
        derived_pool_id=derived,
        pinned_pool_id=pinned,
        matches_pinned=(derived == pinned),
    )


__all__ = [
    "PoolIdCheckResult",
    "check_pool_id_derivation",
]
