"""Tests for the common-ancestor search and canonical chain view (T033).

T033 acceptance requires:

- deterministic common-ancestor search that locates the deepest
  block whose hash is shared by the canonical view and the observed
  view;
- depth-bounded search that triggers the deep-reorg halt when the
  bound is exceeded;
- append-only canonical chain view that never overwrites a block in
  place (finalized raw evidence is preserved).
"""

from __future__ import annotations

import pytest

from robinhood_lp.storage.reorg.ancestor import (
    COMMON_ANCESTOR_UNREACHABLE,
    BlockHeader,
    CanonicalChainView,
    ReorgSegment,
    find_common_ancestor,
    orphan_depth,
)

# ---------------------------------------------------------------------------
# BlockHeader
# ---------------------------------------------------------------------------


def test_block_header_normalises_hash_case() -> None:
    header = BlockHeader(
        block_number=1,
        block_hash="0x" + "AA" * 32,
        parent_hash="0X" + "BB" * 32,
    )
    assert header.block_hash == "0x" + "aa" * 32
    assert header.parent_hash == "0x" + "bb" * 32


def test_block_header_accepts_bare_hex() -> None:
    header = BlockHeader(
        block_number=1,
        block_hash="aa" * 32,
        parent_hash="bb" * 32,
    )
    assert header.block_hash == "0x" + "aa" * 32


def test_block_header_rejects_non_hex() -> None:
    with pytest.raises(ValueError, match="not a 0x-prefixed hex string"):
        BlockHeader(block_number=1, block_hash="not-a-hash", parent_hash="bb" * 32)
    with pytest.raises(ValueError, match="not a 0x-prefixed hex string"):
        BlockHeader(block_number=1, block_hash="0x" + "zz" * 32, parent_hash="bb" * 32)
    # Also reject a body that contains hex digits but is not actually
    # parseable once normalised.
    with pytest.raises(ValueError, match="not a 0x-prefixed hex string"):
        BlockHeader(
            block_number=1,
            block_hash="0x" + "ax" * 32,
            parent_hash="bb" * 32,
        )


def test_block_header_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="not a 0x-prefixed hex string"):
        BlockHeader(block_number=1, block_hash="0xabcd", parent_hash="bb" * 32)


def test_block_header_rejects_negative_number() -> None:
    with pytest.raises(ValueError, match="block_number"):
        BlockHeader(block_number=-1, block_hash="aa" * 32, parent_hash="bb" * 32)


# ---------------------------------------------------------------------------
# CanonicalChainView
# ---------------------------------------------------------------------------


def _make_header(block_number: int, hash_byte: int) -> BlockHeader:
    return BlockHeader(
        block_number=block_number,
        block_hash="0x" + bytes([hash_byte]).hex().rjust(64, "0"),
        parent_hash="0x" + bytes([hash_byte - 1]).hex().rjust(64, "0"),
    )


def test_canonical_view_adds_genesis_and_descendants() -> None:
    view = CanonicalChainView()
    view.add_header(_make_header(0, 0x10), parent_number=None)
    for n, byte in [(1, 0x11), (2, 0x12), (3, 0x13)]:
        view.add_header(_make_header(n, byte), parent_number=n - 1)
    assert view.canonical_tip() == 3
    header_at_2 = view.header_at(2)
    assert header_at_2 is not None
    assert header_at_2.block_hash == _hash_for_byte(0x12)


def test_canonical_view_rejects_genesis_with_parent() -> None:
    view = CanonicalChainView()
    with pytest.raises(ValueError, match="genesis block"):
        view.add_header(_make_header(0, 0x10), parent_number=99)


def test_canonical_view_rejects_missing_parent() -> None:
    view = CanonicalChainView()
    with pytest.raises(ValueError, match="requires a parent_number"):
        view.add_header(_make_header(2, 0x12), parent_number=None)


def test_canonical_view_rejects_unknown_parent() -> None:
    view = CanonicalChainView()
    view.add_header(_make_header(0, 0x10), parent_number=None)
    with pytest.raises(ValueError, match="parent block 5 is not in the view"):
        view.add_header(_make_header(6, 0x16), parent_number=5)


def _hash_for_byte(byte_value: int) -> str:
    """Return the canonical 32-byte 0x-prefixed hex hash for ``byte_value``."""
    body = bytes([byte_value]).hex().rjust(64, "0")
    return "0x" + body


def test_canonical_view_rejects_in_place_overwrite() -> None:
    """Finalized raw evidence is never rewritten."""
    view = CanonicalChainView()
    header = _make_header(0, 0x10)
    view.add_header(header, parent_number=None)
    new_header = BlockHeader(
        block_number=0,
        block_hash="0x" + "ff" * 32,
        parent_hash="0x" + "00" * 32,
    )
    with pytest.raises(ValueError, match="cannot overwrite"):
        view.add_header(new_header, parent_number=None)
    # Original header is still intact.
    header_at_0 = view.header_at(0)
    assert header_at_0 is not None
    assert header_at_0.block_hash == _hash_for_byte(0x10)


def test_canonical_view_idempotent_same_hash() -> None:
    """Re-appending the same header is a no-op (idempotent re-run)."""
    view = CanonicalChainView()
    view.add_header(_make_header(0, 0x10), parent_number=None)
    view.add_header(_make_header(0, 0x10), parent_number=None)
    assert view.header_at(0) is not None


# ---------------------------------------------------------------------------
# find_common_ancestor
# ---------------------------------------------------------------------------


def _populate(view: CanonicalChainView, hashes: dict[int, int]) -> None:
    """Append headers whose block hash encodes the byte ``hashes[n]``."""
    for n in sorted(hashes):
        view.add_header(_make_header(n, hashes[n]), parent_number=(n - 1 if n > 0 else None))


def test_find_common_ancestor_on_identical_chains() -> None:
    canonical = CanonicalChainView()
    observed = CanonicalChainView()
    _populate(canonical, {0: 0x10, 1: 0x11, 2: 0x12})
    _populate(observed, {0: 0x10, 1: 0x11, 2: 0x12})
    ancestor = find_common_ancestor(
        canonical, observed, canonical_tip=2, observed_tip=2, max_depth=10
    )
    assert ancestor == 2


def test_find_common_ancestor_returns_deepest_shared_block() -> None:
    canonical = CanonicalChainView()
    observed = CanonicalChainView()
    # Canonical: 0..3 with hash bytes 10, 11, 12, 13
    # Observed:  0..4 forks at block 2 (both canonical and observed agree on block 2)
    _populate(canonical, {0: 0x10, 1: 0x11, 2: 0x12, 3: 0x13})
    _populate(observed, {0: 0x10, 1: 0x11, 2: 0x12, 3: 0x99, 4: 0x9A})
    ancestor = find_common_ancestor(
        canonical, observed, canonical_tip=3, observed_tip=4, max_depth=10
    )
    assert ancestor == 2


def test_find_common_ancestor_returns_zero_when_genesis_is_shared() -> None:
    canonical = CanonicalChainView()
    observed = CanonicalChainView()
    _populate(canonical, {0: 0x10, 1: 0xAA})
    _populate(observed, {0: 0x10, 1: 0xBB, 2: 0xCC})
    ancestor = find_common_ancestor(
        canonical, observed, canonical_tip=1, observed_tip=2, max_depth=10
    )
    assert ancestor == 0


def test_find_common_ancestor_unreachable_when_no_shared_history() -> None:
    canonical = CanonicalChainView()
    observed = CanonicalChainView()
    _populate(canonical, {0: 0x10, 1: 0x11})
    _populate(observed, {0: 0xFF, 1: 0xFE})
    ancestor = find_common_ancestor(
        canonical, observed, canonical_tip=1, observed_tip=1, max_depth=10
    )
    assert ancestor == COMMON_ANCESTOR_UNREACHABLE


def test_find_common_ancestor_returns_unreachable_when_depth_exceeded() -> None:
    canonical = CanonicalChainView()
    observed = CanonicalChainView()
    # Canonical: 0..9. Observed: 0..9 but with a divergent fork at
    # the tip (block 9) — the shared ancestor is at 8, but the search
    # budget only allows zero parent steps so the search gives up
    # before walking back from block 9 to block 8.
    canonical_hashes = {n: 0x10 + n for n in range(10)}
    canonical_hashes[0] = 0x10
    _populate(canonical, canonical_hashes)
    observed_hashes = dict(canonical_hashes)
    observed_hashes[9] = 0xFE  # diverges at the tip
    _populate(observed, observed_hashes)
    ancestor = find_common_ancestor(
        canonical, observed, canonical_tip=9, observed_tip=9, max_depth=0
    )
    assert ancestor == COMMON_ANCESTOR_UNREACHABLE


def test_find_common_ancestor_depth_bounded_but_succeeds_within_budget() -> None:
    canonical = CanonicalChainView()
    observed = CanonicalChainView()
    canonical_hashes = {n: 0x10 + n for n in range(10)}
    canonical_hashes[0] = 0x10
    _populate(canonical, canonical_hashes)
    observed_hashes = dict(canonical_hashes)
    observed_hashes[9] = 0xFE
    _populate(observed, observed_hashes)
    ancestor = find_common_ancestor(
        canonical, observed, canonical_tip=9, observed_tip=9, max_depth=8
    )
    assert ancestor == 8


def test_find_common_ancestor_rejects_unknown_canonical_tip() -> None:
    canonical = CanonicalChainView()
    canonical.add_header(_make_header(0, 0x10), parent_number=None)
    observed = CanonicalChainView()
    observed.add_header(_make_header(0, 0x10), parent_number=None)
    with pytest.raises(ValueError, match="canonical_tip 5"):
        find_common_ancestor(canonical, observed, canonical_tip=5, observed_tip=0, max_depth=10)


def test_find_common_ancestor_rejects_unknown_observed_tip() -> None:
    canonical = CanonicalChainView()
    canonical.add_header(_make_header(0, 0x10), parent_number=None)
    observed = CanonicalChainView()
    observed.add_header(_make_header(0, 0x10), parent_number=None)
    with pytest.raises(ValueError, match="observed_tip 5"):
        find_common_ancestor(canonical, observed, canonical_tip=0, observed_tip=5, max_depth=10)


def test_find_common_ancestor_rejects_negative_max_depth() -> None:
    canonical = CanonicalChainView()
    canonical.add_header(_make_header(0, 0x10), parent_number=None)
    observed = CanonicalChainView()
    observed.add_header(_make_header(0, 0x10), parent_number=None)
    with pytest.raises(ValueError, match="max_depth must be >= 0"):
        find_common_ancestor(canonical, observed, canonical_tip=0, observed_tip=0, max_depth=-1)


# ---------------------------------------------------------------------------
# ReorgSegment / orphan_depth
# ---------------------------------------------------------------------------


def test_orphan_depth_is_inclusive() -> None:
    assert orphan_depth(canonical_tip=10, common_ancestor=8) == 2


def test_orphan_depth_zero_at_tip() -> None:
    assert orphan_depth(canonical_tip=10, common_ancestor=10) == 0


def test_orphan_depth_rejects_inverted_inputs() -> None:
    with pytest.raises(ValueError, match=r"canonical_tip \d+ < common_ancestor \d+"):
        orphan_depth(canonical_tip=5, common_ancestor=10)


def test_reorg_segment_validates_bounds() -> None:
    with pytest.raises(ValueError, match=r"depth \d+ does not match bounds"):
        ReorgSegment(lower_block_number=1, upper_block_number=3, depth=5)
    with pytest.raises(ValueError, match=r"upper must be >= lower"):
        ReorgSegment(lower_block_number=5, upper_block_number=4, depth=1)
    with pytest.raises(ValueError, match=r"lower must be >= 0"):
        ReorgSegment(lower_block_number=-1, upper_block_number=4, depth=6)
