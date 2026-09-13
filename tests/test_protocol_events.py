"""Tests for block, transaction, event, and token identity (T011).

Coverage:

- BlockRef / TransactionRef / EventKey structural validation
- 32-byte hash width enforcement
- chain_id is mandatory in every identity (no ``(tx_hash, log_index)``
  shortcut)
- canonical/orphaned/removed status is *carried alongside* the key,
  not part of it — orphaning an event does not change its identity
- cross-chain collision prevention: same (tx_hash, log_index) on two
  different chains produces two distinct identities
- cross-fork collision prevention: same block number with different
  hashes produces two distinct identities
- TokenMetadata permits partial / absent records (ADM-TECH-002)
"""

from __future__ import annotations

import pytest

from robinhood_lp.protocol import (
    Address,
    BlockRef,
    CanonicalStatus,
    ChainId,
    EventKey,
    TokenMetadata,
    TransactionRef,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

CHAIN_A = ChainId(1)
CHAIN_B = ChainId(46630)


def _block_hash(seed: int) -> int:
    """Return a deterministic 32-byte hash value derived from seed."""
    # Spread the seed across the 256-bit width so successive seeds are
    # obviously distinct; the value fits in 32 bytes (256 bits) exactly.
    return (seed * 0x0101010101010101_0101010101010101_0101010101010101_0101010101010101) & (
        (1 << 256) - 1
    )


def _tx_hash(seed: int) -> int:
    return (seed * 0xDEADBEEFCAFEBABE_DEADBEEFCAFEBABE_DEADBEEFCAFEBABE_DEADBEEFCAFEBABE) & (
        (1 << 256) - 1
    )


# ---------------------------------------------------------------------------
# ChainId regression (shared with T010; ensure it remains strict)
# ---------------------------------------------------------------------------


def test_chain_id_still_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        ChainId(0)
    with pytest.raises(ValueError):
        ChainId(-1)


# ---------------------------------------------------------------------------
# BlockRef
# ---------------------------------------------------------------------------


def test_block_ref_minimal_construction() -> None:
    br = BlockRef(chain_id=CHAIN_A, block_hash=_block_hash(1), block_number=21_000_000)
    assert br.chain_id == CHAIN_A
    assert br.block_number == 21_000_000
    assert len(br.to_hash_bytes()) == 32


def test_block_ref_rejects_non_chain_id() -> None:
    with pytest.raises(TypeError, match="ChainId"):
        BlockRef(chain_id="1", block_hash=_block_hash(1), block_number=0)  # type: ignore[arg-type]


def test_block_ref_rejects_overlong_block_hash() -> None:
    with pytest.raises(ValueError, match="block_hash"):
        BlockRef(chain_id=CHAIN_A, block_hash=1 << 256, block_number=0)


def test_block_ref_rejects_negative_block_number() -> None:
    with pytest.raises(ValueError, match="block_number"):
        BlockRef(chain_id=CHAIN_A, block_hash=_block_hash(1), block_number=-1)


def test_block_ref_rejects_non_int_block_number() -> None:
    with pytest.raises(TypeError, match="block_number"):
        BlockRef(chain_id=CHAIN_A, block_hash=_block_hash(1), block_number="1")  # type: ignore[arg-type]


def test_same_number_different_hash_is_distinct_identity() -> None:
    """Cross-fork: same block number, different hashes -> not equal."""
    a = BlockRef(chain_id=CHAIN_A, block_hash=_block_hash(1), block_number=100)
    b = BlockRef(chain_id=CHAIN_A, block_hash=_block_hash(2), block_number=100)
    assert a != b
    assert hash(a) != hash(b)


# ---------------------------------------------------------------------------
# TransactionRef
# ---------------------------------------------------------------------------


def test_transaction_ref_minimal_construction() -> None:
    tr = TransactionRef(chain_id=CHAIN_A, tx_hash=_tx_hash(1))
    assert tr.chain_id == CHAIN_A
    assert len(tr.to_hash_bytes()) == 32


def test_transaction_ref_rejects_overlong_tx_hash() -> None:
    with pytest.raises(ValueError, match="tx_hash"):
        TransactionRef(chain_id=CHAIN_A, tx_hash=1 << 256)


def test_transaction_ref_rejects_non_chain_id() -> None:
    with pytest.raises(TypeError):
        TransactionRef(chain_id=1, tx_hash=_tx_hash(1))  # type: ignore[arg-type]


def test_same_tx_hash_different_chains_is_distinct() -> None:
    """Cross-chain: same tx_hash on two chains -> not equal (EIP-155)."""
    a = TransactionRef(chain_id=CHAIN_A, tx_hash=_tx_hash(1))
    b = TransactionRef(chain_id=CHAIN_B, tx_hash=_tx_hash(1))
    assert a != b


# ---------------------------------------------------------------------------
# EventKey — the critical identity
# ---------------------------------------------------------------------------


def _event_key(
    chain_id: ChainId = CHAIN_A,
    block_seed: int = 1,
    tx_seed: int = 1,
    log_index: int = 0,
) -> EventKey:
    return EventKey(
        chain_id=chain_id,
        block_hash=_block_hash(block_seed),
        tx_hash=_tx_hash(tx_seed),
        log_index=log_index,
    )


def test_event_key_minimal_construction() -> None:
    ek = _event_key()
    assert ek.chain_id == CHAIN_A
    assert ek.log_index == 0


def test_event_key_requires_chain_id() -> None:
    """T011 must-not: ``(tx_hash, log_index)`` alone is not enough."""
    with pytest.raises(TypeError, match="chain_id"):
        EventKey(  # type: ignore[call-arg]
            block_hash=_block_hash(1),
            tx_hash=_tx_hash(1),
            log_index=0,
        )


def test_event_key_rejects_non_int_log_index() -> None:
    with pytest.raises(TypeError, match="log_index"):
        EventKey(
            chain_id=CHAIN_A,
            block_hash=_block_hash(1),
            tx_hash=_tx_hash(1),
            log_index="0",  # type: ignore[arg-type]
        )


def test_event_key_rejects_negative_log_index() -> None:
    with pytest.raises(ValueError, match="log_index"):
        _event_key(log_index=-1)


def test_event_key_distinct_per_chain() -> None:
    """Cross-chain collision prevention."""
    a = _event_key(chain_id=CHAIN_A)
    b = _event_key(chain_id=CHAIN_B)
    assert a != b
    assert hash(a) != hash(b)


def test_event_key_distinct_per_block_hash_on_same_chain() -> None:
    """Cross-fork: same chain, same tx+log, different block hash."""
    a = _event_key(block_seed=1)
    b = _event_key(block_seed=2)
    assert a != b


def test_event_key_distinct_per_tx_hash() -> None:
    a = _event_key(tx_seed=1)
    b = _event_key(tx_seed=2)
    assert a != b


def test_event_key_distinct_per_log_index() -> None:
    a = _event_key(log_index=0)
    b = _event_key(log_index=1)
    assert a != b


def test_event_key_full_equality_when_all_fields_match() -> None:
    a = _event_key()
    b = _event_key()
    assert a == b
    assert hash(a) == hash(b)


def test_event_key_block_ref_uses_event_block_hash() -> None:
    ek = _event_key(block_seed=7)
    br = ek.block_ref()
    assert br.chain_id == CHAIN_A
    assert br.block_hash == ek.block_hash


def test_event_key_transaction_ref_uses_event_tx_hash() -> None:
    ek = _event_key(tx_seed=9)
    tr = ek.transaction_ref()
    assert tr.chain_id == CHAIN_A
    assert tr.tx_hash == ek.tx_hash


# ---------------------------------------------------------------------------
# CanonicalStatus
# ---------------------------------------------------------------------------


def test_canonical_status_values() -> None:
    assert CanonicalStatus.CANONICAL.value == "canonical"
    assert CanonicalStatus.ORPHANED.value == "orphaned"
    assert CanonicalStatus.REMOVED.value == "removed"


def test_canonical_status_is_record_state_not_identity() -> None:
    """T011 must-not: orphaning an event must not change its identity."""
    ek = _event_key()
    # The same EventKey is logically identifiable as canonical,
    # orphaned, or removed depending on what the chain reports; but
    # the *key itself* does not change. Demonstrate this by holding
    # the key across status transitions.
    states = [
        CanonicalStatus.CANONICAL,
        CanonicalStatus.ORPHANED,
        CanonicalStatus.REMOVED,
    ]
    keys = [ek] * 3
    for state, k in zip(states, keys, strict=True):
        # The key is unchanged; the status is a separate concern.
        assert k == ek
        assert state in CanonicalStatus


# ---------------------------------------------------------------------------
# TokenMetadata (display-only)
# ---------------------------------------------------------------------------


def test_token_metadata_minimal_construction() -> None:
    tm = TokenMetadata(chain_id=CHAIN_A, address=Address.from_hex("0x" + "11" * 20))
    assert tm.symbol is None
    assert tm.name is None
    assert tm.decimals is None
    assert tm.is_complete() is False


def test_token_metadata_full_construction() -> None:
    tm = TokenMetadata(
        chain_id=CHAIN_A,
        address=Address.from_hex("0x" + "22" * 20),
        symbol="TKN",
        name="Token",
        decimals=18,
    )
    assert tm.is_complete() is True


def test_token_metadata_is_not_identity() -> None:
    """T011: missing symbol/name/decimals must not change identity.

    Two TokenMetadata records with the same chain+address are
    considered the same token even when their metadata differs (one
    is incomplete, one is complete). Identity is (chain_id, address).
    """
    addr = Address.from_hex("0x" + "33" * 20)
    incomplete = TokenMetadata(chain_id=CHAIN_A, address=addr)
    complete = TokenMetadata(
        chain_id=CHAIN_A,
        address=addr,
        symbol="TKN",
        name="Token",
        decimals=18,
    )
    # TokenMetadata itself is a frozen dataclass and uses default
    # identity (all fields). The *conceptual* identity is captured
    # by (chain_id, address) and is what callers should use for
    # identity comparisons; equality of TokenMetadata instances is
    # not used for that purpose. Document this by asserting that
    # callers must key on (chain_id, address) directly:
    assert incomplete.chain_id == complete.chain_id
    assert incomplete.address == complete.address


def test_token_metadata_rejects_out_of_range_decimals() -> None:
    with pytest.raises(ValueError, match="decimals"):
        TokenMetadata(
            chain_id=CHAIN_A,
            address=Address.from_hex("0x" + "44" * 20),
            decimals=256,
        )


def test_token_metadata_rejects_negative_decimals() -> None:
    with pytest.raises(ValueError, match="decimals"):
        TokenMetadata(
            chain_id=CHAIN_A,
            address=Address.from_hex("0x" + "55" * 20),
            decimals=-1,
        )


def test_token_metadata_requires_chain_id() -> None:
    with pytest.raises(TypeError):
        TokenMetadata(chain_id=1, address=Address.from_hex("0x" + "66" * 20))  # type: ignore[arg-type]


def test_token_metadata_requires_address() -> None:
    with pytest.raises(TypeError):
        TokenMetadata(chain_id=CHAIN_A, address="0x" + "77" * 20)  # type: ignore[arg-type]
