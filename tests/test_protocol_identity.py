"""Tests for chain-agnostic record-level PoolIdentity (T011).

Coverage:

- ``pool_manager_address`` is required at construction (no default).
- ``PoolIdentity`` round-trips through ``(chain_id, pool_id, pool_manager_address)``.
- Equality and hashing use all three fields.
- ``PoolIdentity`` does NOT replace the on-chain V4 ``PoolId``: the
  underlying bytes are exactly the keccak256 of the ABI-encoded
  ``PoolKey``.
- Cross-chain collision prevention: identical ``PoolKey`` + identical
  ``PoolManager`` on two chains produce two distinct ``PoolIdentity``
  records.
- The type is chain-agnostic on day one: no chain-specific knowledge
  in the constructor; ``ChainId(1)``, the Owner-confirmed placeholder
  ``ChainId(46630)``, and a sentinel ``ChainId(999_999)`` all work.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from robinhood_lp.protocol import (
    Address,
    ChainId,
    Currency,
    PoolId,
    PoolIdentity,
    PoolKey,
)
from robinhood_lp.protocol.abi import compute_pool_id

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

CHAIN_MAINNET = ChainId(1)
CHAIN_PLACEHOLDER = ChainId(46630)  # Owner-confirmed placeholder until T024
CHAIN_SENTINEL = ChainId(999_999)  # arbitrary future chain; chain-agnostic proof

# A canonical V4 PoolManager address used across the collision tests
# (the exact value does not matter; only that it is a valid Address).
POOL_MANAGER_HEX = "0x" + "ab" * 20


def _mgr() -> Address:
    return Address.from_hex(POOL_MANAGER_HEX)


def _pool_key() -> PoolKey:
    """A canonical V4 PoolKey (currency0 < currency1 as uint160)."""
    return PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "22" * 20),
        fee=3000,
        tick_spacing=60,
        hooks=Address.zero(),
    )


def _pool_id_from_key(pk: PoolKey) -> PoolId:
    """Derive the on-chain V4 PoolId (keccak256 of the ABI-encoded PoolKey)."""
    return PoolId(compute_pool_id(pk))


# ---------------------------------------------------------------------------
# Construction: pool_manager_address is required (no default)
# ---------------------------------------------------------------------------


def test_pool_identity_required_pool_manager_address() -> None:
    """The contract forbids constructing PoolIdentity without pool_manager_address."""
    pool_id = _pool_id_from_key(_pool_key())
    # Positional: omit pool_manager_address → TypeError
    with pytest.raises(TypeError):
        PoolIdentity(CHAIN_MAINNET, pool_id)  # type: ignore[call-arg]
    # Keyword form omitting the field → TypeError (frozen dataclass without default)
    with pytest.raises(TypeError):
        PoolIdentity(chain_id=CHAIN_MAINNET, pool_id=pool_id)  # type: ignore[call-arg]


def test_pool_identity_rejects_non_address_pool_manager() -> None:
    pool_id = _pool_id_from_key(_pool_key())
    with pytest.raises(TypeError, match="pool_manager_address"):
        PoolIdentity(chain_id=CHAIN_MAINNET, pool_id=pool_id, pool_manager_address="0xabc")  # type: ignore[arg-type]


def test_pool_identity_rejects_non_chain_id() -> None:
    pool_id = _pool_id_from_key(_pool_key())
    with pytest.raises(TypeError, match="chain_id"):
        PoolIdentity(chain_id=1, pool_id=pool_id, pool_manager_address=_mgr())  # type: ignore[arg-type]


def test_pool_identity_rejects_non_pool_id() -> None:
    with pytest.raises(TypeError, match="pool_id"):
        PoolIdentity(
            chain_id=CHAIN_MAINNET,
            pool_id="0x" + "00" * 32,  # type: ignore[arg-type]
            pool_manager_address=_mgr(),
        )


def test_pool_identity_is_frozen() -> None:
    """Identity records must be immutable (no field reassignment)."""
    pi = PoolIdentity(
        chain_id=CHAIN_MAINNET, pool_id=_pool_id_from_key(_pool_key()), pool_manager_address=_mgr()
    )
    with pytest.raises(FrozenInstanceError):
        pi.chain_id = CHAIN_PLACEHOLDER  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_pool_identity_round_trip() -> None:
    pk = _pool_key()
    pool_id = _pool_id_from_key(pk)
    mgr = _mgr()
    original = PoolIdentity(chain_id=CHAIN_MAINNET, pool_id=pool_id, pool_manager_address=mgr)

    components = original.to_components()
    assert components == (CHAIN_MAINNET, pool_id, mgr)

    reconstructed = PoolIdentity(
        chain_id=components[0],
        pool_id=components[1],
        pool_manager_address=components[2],
    )
    assert reconstructed == original
    assert reconstructed.chain_id == original.chain_id
    assert reconstructed.pool_id == original.pool_id
    assert reconstructed.pool_manager_address == original.pool_manager_address

    # Byte-exact preservation of the on-chain PoolId across round-trip
    assert reconstructed.pool_id.to_bytes() == original.pool_id.to_bytes()
    assert reconstructed.pool_id.to_hex() == original.pool_id.to_hex()


def test_pool_identity_repr_contains_all_three_fields() -> None:
    pk = _pool_key()
    pi = PoolIdentity(
        chain_id=CHAIN_MAINNET, pool_id=_pool_id_from_key(pk), pool_manager_address=_mgr()
    )
    text = repr(pi)
    # The repr must carry all three fields in a human-readable form.
    assert "chain=1" in text
    assert pi.pool_id.to_hex() in text
    assert POOL_MANAGER_HEX in text


# ---------------------------------------------------------------------------
# Equality / hashing by all three fields
# ---------------------------------------------------------------------------


def test_pool_identity_equality_by_all_three_fields() -> None:
    pk = _pool_key()
    pool_id = _pool_id_from_key(pk)
    mgr = _mgr()
    a = PoolIdentity(chain_id=CHAIN_MAINNET, pool_id=pool_id, pool_manager_address=mgr)
    b = PoolIdentity(chain_id=CHAIN_MAINNET, pool_id=pool_id, pool_manager_address=mgr)
    assert a == b
    assert hash(a) == hash(b)

    # Differ on chain_id → not equal
    assert PoolIdentity(chain_id=CHAIN_PLACEHOLDER, pool_id=pool_id, pool_manager_address=mgr) != a

    # Differ on pool_manager_address → not equal
    other_mgr = Address.from_hex("0x" + "cd" * 20)
    assert (
        PoolIdentity(chain_id=CHAIN_MAINNET, pool_id=pool_id, pool_manager_address=other_mgr) != a
    )

    # Differ on pool_id (use a different PoolKey) → not equal
    other_pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "33" * 20),  # different currency1
        fee=3000,
        tick_spacing=60,
        hooks=Address.zero(),
    )
    assert _pool_id_from_key(other_pk) != pool_id
    assert (
        PoolIdentity(
            chain_id=CHAIN_MAINNET,
            pool_id=_pool_id_from_key(other_pk),
            pool_manager_address=mgr,
        )
        != a
    )


def test_pool_identity_hash_by_all_three_fields() -> None:
    pk = _pool_key()
    pool_id = _pool_id_from_key(pk)
    mgr = _mgr()
    base = PoolIdentity(chain_id=CHAIN_MAINNET, pool_id=pool_id, pool_manager_address=mgr)

    # Differ on chain_id → hash differs
    other_chain = PoolIdentity(
        chain_id=CHAIN_PLACEHOLDER, pool_id=pool_id, pool_manager_address=mgr
    )
    assert hash(other_chain) != hash(base)

    # Differ on pool_manager_address → hash differs
    other_mgr_pi = PoolIdentity(
        chain_id=CHAIN_MAINNET,
        pool_id=pool_id,
        pool_manager_address=Address.from_hex("0x" + "cd" * 20),
    )
    assert hash(other_mgr_pi) != hash(base)

    # Differ on pool_id → hash differs
    other_pk = PoolKey(
        currency0=Currency.from_hex("0x" + "11" * 20),
        currency1=Currency.from_hex("0x" + "33" * 20),
        fee=3000,
        tick_spacing=60,
        hooks=Address.zero(),
    )
    other_pool_id_pi = PoolIdentity(
        chain_id=CHAIN_MAINNET,
        pool_id=_pool_id_from_key(other_pk),
        pool_manager_address=mgr,
    )
    assert hash(other_pool_id_pi) != hash(base)


# ---------------------------------------------------------------------------
# PoolIdentity does NOT replace the V4 on-chain PoolId
# ---------------------------------------------------------------------------


def test_pool_identity_does_not_replace_pool_id() -> None:
    """PoolIdentity stores the on-chain V4 PoolId byte-for-byte.

    It does not re-derive the PoolId from any other field, and the
    stored PoolId equals the keccak256 of the ABI-encoded PoolKey.
    """
    pk = _pool_key()
    expected_pool_id = _pool_id_from_key(pk)

    pi = PoolIdentity(
        chain_id=CHAIN_MAINNET,
        pool_id=expected_pool_id,
        pool_manager_address=_mgr(),
    )

    # Byte-exact preservation
    assert pi.pool_id == expected_pool_id
    assert pi.pool_id.value == expected_pool_id.value
    assert pi.pool_id.to_bytes() == expected_pool_id.to_bytes()
    assert pi.pool_id.to_hex() == expected_pool_id.to_hex()

    # The .on_chain_pool_id accessor returns the same value object
    assert pi.on_chain_pool_id == expected_pool_id
    assert pi.on_chain_pool_id.to_bytes() == expected_pool_id.to_bytes()

    # And it equals the keccak256 of the ABI-encoded PoolKey
    assert pi.pool_id.value == compute_pool_id(pk)


# ---------------------------------------------------------------------------
# Cross-chain collision prevention
# ---------------------------------------------------------------------------


def test_pool_identity_cross_chain_collision_46630() -> None:
    """Identical PoolKey + identical PoolManager on two chains → distinct identities."""
    pk = _pool_key()
    pool_id = _pool_id_from_key(pk)
    mgr = _mgr()

    mainnet = PoolIdentity(chain_id=CHAIN_MAINNET, pool_id=pool_id, pool_manager_address=mgr)
    placeholder = PoolIdentity(
        chain_id=CHAIN_PLACEHOLDER, pool_id=pool_id, pool_manager_address=mgr
    )

    # Not equal because chain_id differs
    assert mainnet != placeholder

    # Hash differs
    assert hash(mainnet) != hash(placeholder)

    # The underlying on-chain PoolId bytes are byte-identical
    # (the chain is what disambiguates them).
    assert mainnet.pool_id.to_bytes() == placeholder.pool_id.to_bytes()
    assert mainnet.pool_manager_address == placeholder.pool_manager_address

    # But the full identity tuple differs.
    assert mainnet.to_components() != placeholder.to_components()
    assert mainnet.to_components()[0] != placeholder.to_components()[0]


def test_pool_identity_cross_chain_collision_byte_difference() -> None:
    """Explicit byte-level assertion that chain_id makes the records distinct."""
    pk = _pool_key()
    pool_id = _pool_id_from_key(pk)
    mgr = _mgr()

    mainnet = PoolIdentity(chain_id=CHAIN_MAINNET, pool_id=pool_id, pool_manager_address=mgr)
    placeholder = PoolIdentity(
        chain_id=CHAIN_PLACEHOLDER, pool_id=pool_id, pool_manager_address=mgr
    )

    mainnet_repr = repr(mainnet).encode("utf-8")
    placeholder_repr = repr(placeholder).encode("utf-8")
    assert mainnet_repr != placeholder_repr
    assert b"chain=1," in mainnet_repr
    assert b"chain=46630," in placeholder_repr


# ---------------------------------------------------------------------------
# Chain-agnostic type
# ---------------------------------------------------------------------------


def test_pool_identity_chain_agnostic_type() -> None:
    """The type does not embed chain-specific knowledge.

    Construction must succeed for any positive ChainId without the
    type having hard-coded chain values.
    """
    pk = _pool_key()
    pool_id = _pool_id_from_key(pk)
    mgr = _mgr()

    a = PoolIdentity(chain_id=CHAIN_MAINNET, pool_id=pool_id, pool_manager_address=mgr)
    b = PoolIdentity(chain_id=CHAIN_PLACEHOLDER, pool_id=pool_id, pool_manager_address=mgr)
    c = PoolIdentity(chain_id=CHAIN_SENTINEL, pool_id=pool_id, pool_manager_address=mgr)

    assert a.chain_id == CHAIN_MAINNET
    assert b.chain_id == CHAIN_PLACEHOLDER
    assert c.chain_id == CHAIN_SENTINEL

    # All three carry the byte-identical on-chain PoolId
    assert a.pool_id == b.pool_id == c.pool_id
    assert a.pool_id.to_bytes() == b.pool_id.to_bytes() == c.pool_id.to_bytes()

    # All three carry the byte-identical PoolManager
    assert a.pool_manager_address == b.pool_manager_address == c.pool_manager_address

    # But the records are pairwise distinct
    assert a != b
    assert a != c
    assert b != c


def test_pool_identity_pool_id_independent_of_chain_field() -> None:
    """The chain field does not affect the stored PoolId bytes.

    Two PoolIdentity records constructed with different chain_ids but
    the same source PoolKey carry byte-identical PoolId values.
    """
    pk = _pool_key()
    pool_id = _pool_id_from_key(pk)
    mgr = _mgr()

    a = PoolIdentity(chain_id=CHAIN_MAINNET, pool_id=pool_id, pool_manager_address=mgr)
    b = PoolIdentity(chain_id=CHAIN_SENTINEL, pool_id=pool_id, pool_manager_address=mgr)

    assert a.pool_id.to_bytes() == b.pool_id.to_bytes()
    assert a.pool_id.value == b.pool_id.value
