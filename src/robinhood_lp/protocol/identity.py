"""Chain-agnostic record-level pool identity (T011).

Identity rules (per ``docs/spec/protocol/PROTOCOL_FACTS.md`` and
``todo/phases/P01-protocol-foundation/T011.md``):

- The on-chain V4 ``PoolId`` is a 32-byte value: keccak256 of the
  ABI-encoded ``PoolKey``. It is fixed by the V4 protocol and MUST NOT
  be re-derived by ``PoolIdentity``;
- ``PoolIdentity`` is a *record-level* composite that wraps the
  on-chain ``PoolId`` and adds the chain identifier and the canonical
  ``PoolManager`` address for the chain. Downstream registries use it
  to disambiguate pools across chains and across PoolManager
  deployments without altering the V4 on-chain identity;
- ``PoolIdentity(chain_id, pool_id, pool_manager_address)`` is the
  full 3-tuple identity. All three fields are required at construction
  time (no defaults). A missing ``pool_manager_address`` is rejected;
- The class is chain-agnostic on day one: it does not encode any
  chain-specific knowledge (e.g. Robinhood Chain's chain id). A
  placeholder ``ChainId(46630)`` is used in cross-chain collision
  tests until T024 verifies the real value;
- Equality and hashing use all three fields. The on-chain ``PoolId``
  bytes are preserved byte-for-byte; ``PoolIdentity`` does **not**
  replace the on-chain ``PoolId``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from robinhood_lp.protocol.ids import Address, ChainId, PoolId

if TYPE_CHECKING:
    pass


@dataclass(frozen=True, slots=True)
class PoolIdentity:
    """Chain-agnostic record-level identity for a V4 pool.

    Identity is the 3-tuple
    ``(chain_id, pool_id, pool_manager_address)``:

    - ``chain_id`` namespaces pools across chains;
    - ``pool_id`` is the on-chain V4 ``PoolId`` (keccak256 of the
      ABI-encoded ``PoolKey``) and is stored here byte-for-byte. It is
      NOT re-derived;
    - ``pool_manager_address`` is the canonical ``PoolManager`` for
      the chain. Per V4 the ``PoolManager`` is a singleton per chain,
      but ``PoolIdentity`` is chain-agnostic and accepts any valid
      address; downstream configuration provides the concrete value.

    All three fields are required at construction time; there is no
    default for ``pool_manager_address``. Two ``PoolIdentity`` records
    are equal iff every field is equal.
    """

    chain_id: ChainId
    pool_id: PoolId
    pool_manager_address: Address

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise TypeError(
                f"PoolIdentity.chain_id: must be ChainId, got {type(self.chain_id).__name__}"
            )
        if not isinstance(self.pool_id, PoolId):
            raise TypeError(
                f"PoolIdentity.pool_id: must be PoolId, got {type(self.pool_id).__name__}"
            )
        if not isinstance(self.pool_manager_address, Address):
            raise TypeError(
                "PoolIdentity.pool_manager_address: must be Address, "
                f"got {type(self.pool_manager_address).__name__}"
            )

    def to_components(self) -> tuple[ChainId, PoolId, Address]:
        """Return the canonical 3-tuple ``(chain_id, pool_id, pool_manager_address)``.

        This is the round-trip serialization form: reconstruct a
        ``PoolIdentity`` by passing the three values to the
        constructor.
        """
        return (self.chain_id, self.pool_id, self.pool_manager_address)

    @property
    def on_chain_pool_id(self) -> PoolId:
        """Return the underlying V4 ``PoolId`` byte-for-byte.

        Provided as an explicit accessor so that code paths that need
        the V4 on-chain identifier can read it without the chain or
        manager fields.
        """
        return self.pool_id

    def __repr__(self) -> str:
        return (
            f"PoolIdentity(chain={self.chain_id.value}, "
            f"pool_id={self.pool_id.to_hex()}, "
            f"pool_manager={self.pool_manager_address.to_hex()})"
        )


__all__ = [
    "PoolIdentity",
]
