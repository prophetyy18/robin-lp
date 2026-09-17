"""Common-ancestor search and canonical chain view (T033).

T033 owns the deterministic search that locates the deepest block
whose hash is shared by both the canonical view and a newly observed
view. The search is depth-bounded: when the bound is exceeded the
handler treats the reorg as a deep reorg and halts qualification.

The package deliberately keeps the canonical chain view in memory:

- the canonical view is a function of the reorg journal and the
  partition block bounds; both are tiny compared to the raw
  partition bytes and are easy to reconstruct on startup;
- every consumer that decides finality must be deterministic and
  testable without a network round-trip.

In-memory also makes the common-ancestor search auditable: a test
can hand-craft a chain, observe a reorg, and assert the search
returns the expected block number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Sentinel returned by :func:`find_common_ancestor` when the orphan
#: depth exceeded the policy's deep-reorg threshold or the genesis
#: block is unreachable from either side.
COMMON_ANCESTOR_UNREACHABLE: Final[int] = -1


# ---------------------------------------------------------------------------
# Block headers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockHeader:
    """The minimal header information T033 needs.

    ``block_hash`` is a lowercase ``0x``-prefixed hex string;
    ``parent_hash`` is the same format. ``block_number`` is the
    canonical block height. The handler compares hashes
    case-insensitively; this keeps the comparison deterministic
    regardless of how the RPC adapter capitalised the response.
    """

    block_number: int
    block_hash: str
    parent_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.block_number, int) or isinstance(self.block_number, bool):
            raise TypeError(
                f"BlockHeader.block_number: must be int, got {type(self.block_number).__name__}"
            )
        if self.block_number < 0:
            raise ValueError(f"BlockHeader.block_number: must be >= 0, got {self.block_number}")
        for attr in ("block_hash", "parent_hash"):
            value = getattr(self, attr)
            if not isinstance(value, str):
                raise TypeError(f"BlockHeader.{attr}: must be str, got {type(value).__name__}")
            normalized = _normalize_hash(value)
            if normalized is None:
                raise ValueError(f"BlockHeader.{attr}: not a 0x-prefixed hex string ({value!r})")
            # Freeze the normalised form so equality comparisons are
            # case-insensitive and the journal writes a canonical
            # value.
            object.__setattr__(self, attr, normalized)


def _normalize_hash(value: str) -> str | None:
    if not value:
        return None
    s = value.strip().lower()
    if not s.startswith("0x"):
        s = "0x" + s
    # 32-byte hex hash (64 hex chars after the 0x).
    body = s[2:]
    if len(body) != 64:
        return None
    try:
        int(body, 16)
    except ValueError:
        return None
    return s


# ---------------------------------------------------------------------------
# Canonical chain view
# ---------------------------------------------------------------------------


class CanonicalChainView:
    """In-memory representation of the canonical block ancestry.

    The view stores a ``block_number -> header`` mapping plus a
    ``block_number -> parent_block_number`` mapping. The depth of the
    longest known chain is bounded by the number of partitions the
    P03 ingestion pipeline has written; for the supported chains that
    is small enough to keep in memory.

    The view is append-only: once a block number has been added it
    may not be re-pointed to a different hash. Reorg demotion marks
    blocks as orphans (handled by the handler), not by mutating the
    view's contents.
    """

    def __init__(self) -> None:
        self._headers: dict[int, BlockHeader] = {}
        self._parent: dict[int, int] = {}

    # ----- mutation ------------------------------------------------------

    def add_header(self, header: BlockHeader, *, parent_number: int | None) -> None:
        """Append one header to the canonical view.

        ``parent_number`` is the block number of ``header.parent_hash``
        in this view. For the genesis block ``parent_number`` must be
        ``None``. Any block number that is already present in the
        view with a different hash is a programming error.
        """
        if not isinstance(header, BlockHeader):
            raise TypeError(
                "CanonicalChainView.add_header: header must be BlockHeader, "
                f"got {type(header).__name__}"
            )
        existing = self._headers.get(header.block_number)
        if existing is not None and existing.block_hash != header.block_hash:
            raise ValueError(
                "CanonicalChainView.add_header: cannot overwrite block "
                f"{header.block_number} (existing={existing.block_hash!r}, "
                f"new={header.block_hash!r}); finalized raw evidence is never rewritten"
            )
        if header.block_number == 0:
            if parent_number is not None:
                raise ValueError(
                    "CanonicalChainView.add_header: genesis block (0) must have parent_number=None"
                )
        else:
            if parent_number is None:
                raise ValueError(
                    "CanonicalChainView.add_header: non-genesis block "
                    f"{header.block_number} requires a parent_number"
                )
            if parent_number >= header.block_number:
                raise ValueError(
                    "CanonicalChainView.add_header: parent_number must be < block_number"
                )
            if parent_number not in self._headers:
                raise ValueError(
                    "CanonicalChainView.add_header: parent block "
                    f"{parent_number} is not in the view"
                )
        self._headers[header.block_number] = header
        if parent_number is not None:
            self._parent[header.block_number] = parent_number

    # ----- queries -------------------------------------------------------

    def header_at(self, block_number: int) -> BlockHeader | None:
        return self._headers.get(block_number)

    def parent_of(self, block_number: int) -> int | None:
        return self._parent.get(block_number)

    def canonical_tip(self) -> int | None:
        """Highest block number currently recorded."""
        if not self._headers:
            return None
        return max(self._headers)

    def max_height(self) -> int:
        return 0 if not self._headers else max(self._headers)

    def __contains__(self, block_number: int) -> bool:
        return block_number in self._headers

    def __len__(self) -> int:
        return len(self._headers)


# ---------------------------------------------------------------------------
# Common-ancestor search
# ---------------------------------------------------------------------------


def find_common_ancestor(
    canonical: CanonicalChainView,
    observed: CanonicalChainView,
    *,
    canonical_tip: int,
    observed_tip: int,
    max_depth: int,
) -> int:
    """Return the deepest block number whose hash is shared by both views.

    The search walks backwards from the observed tip toward the
    canonical tip using the **observed** view's parent edges until
    it lands on a block whose hash matches a known canonical header.
    That block (if any) is the common ancestor.

    Parameters
    ----------
    canonical:
        The canonical chain view. The handler uses this as the
        authoritative source of truth.
    observed:
        A second in-memory view built from the freshly observed
        headers (e.g. fetched from ``eth_getBlockByHash``).
    canonical_tip, observed_tip:
        The block numbers the search starts from. The caller is
        responsible for ensuring both tips exist in their respective
        views; passing a missing tip is a programming error.
    max_depth:
        Maximum number of parent steps the search is allowed to
        walk. When the search exceeds ``max_depth`` it returns
        :data:`COMMON_ANCESTOR_UNREACHABLE` so the handler can
        trigger the deep-reorg halt.

    Returns
    -------
    int
        The block number of the deepest shared header, or
        :data:`COMMON_ANCESTOR_UNREACHABLE` if no common ancestor is
        reachable within ``max_depth`` parent steps.
    """
    if not isinstance(canonical, CanonicalChainView):
        raise TypeError(
            "find_common_ancestor: canonical must be CanonicalChainView, "
            f"got {type(canonical).__name__}"
        )
    if not isinstance(observed, CanonicalChainView):
        raise TypeError(
            "find_common_ancestor: observed must be CanonicalChainView, "
            f"got {type(observed).__name__}"
        )
    if not isinstance(max_depth, int) or isinstance(max_depth, bool):
        raise TypeError(
            f"find_common_ancestor: max_depth must be int, got {type(max_depth).__name__}"
        )
    if max_depth < 0:
        raise ValueError(f"find_common_ancestor: max_depth must be >= 0, got {max_depth}")

    canonical_header = canonical.header_at(canonical_tip)
    if canonical_header is None:
        raise ValueError(
            f"find_common_ancestor: canonical_tip {canonical_tip} is not in the canonical view"
        )
    observed_header = observed.header_at(observed_tip)
    if observed_header is None:
        raise ValueError(
            f"find_common_ancestor: observed_tip {observed_tip} is not in the observed view"
        )

    current = observed_tip
    steps = 0
    while True:
        obs_header = observed.header_at(current)
        if obs_header is None:
            return COMMON_ANCESTOR_UNREACHABLE
        canon_at_same_height = canonical.header_at(current)
        if (
            canon_at_same_height is not None
            and canon_at_same_height.block_hash == obs_header.block_hash
        ):
            return current
        if current == 0:
            # Genesis did not match: there is no common ancestor.
            return COMMON_ANCESTOR_UNREACHABLE
        parent = observed.parent_of(current)
        if parent is None:
            return COMMON_ANCESTOR_UNREACHABLE
        if steps >= max_depth:
            return COMMON_ANCESTOR_UNREACHABLE
        steps += 1
        current = parent


# ---------------------------------------------------------------------------
# Block reconciliation helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReorgSegment:
    """The set of blocks above the common ancestor on one side of a reorg.

    ``depth`` is the inclusive number of blocks in the segment;
    ``orphan_depth`` is the count the policy compares against
    ``deep_reorg_threshold_blocks`` to decide between a shallow and
    a deep reorg.
    """

    lower_block_number: int
    upper_block_number: int
    depth: int

    def __post_init__(self) -> None:
        if self.lower_block_number < 0:
            raise ValueError(f"ReorgSegment: lower must be >= 0, got {self.lower_block_number}")
        if self.upper_block_number < self.lower_block_number:
            raise ValueError(
                f"ReorgSegment: upper must be >= lower, got {self.upper_block_number} < "
                f"{self.lower_block_number}"
            )
        if self.depth < 0:
            raise ValueError(f"ReorgSegment: depth must be >= 0, got {self.depth}")
        expected = self.upper_block_number - self.lower_block_number + 1
        if self.depth != expected:
            raise ValueError(
                f"ReorgSegment: depth {self.depth} does not match bounds "
                f"[{self.lower_block_number}, {self.upper_block_number}] (expected {expected})"
            )


def orphan_depth(
    canonical_tip: int,
    common_ancestor: int,
) -> int:
    """Return the orphan depth above the common ancestor.

    The depth is the inclusive number of canonical blocks that the
    reorg demotes. The handler compares this against the policy's
    ``deep_reorg_threshold_blocks`` to decide between a shallow and
    a deep reorg.
    """
    if canonical_tip < common_ancestor:
        raise ValueError(
            f"orphan_depth: canonical_tip {canonical_tip} < common_ancestor {common_ancestor}"
        )
    return canonical_tip - common_ancestor


__all__ = [
    "BlockHeader",
    "COMMON_ANCESTOR_UNREACHABLE",
    "CanonicalChainView",
    "ReorgSegment",
    "find_common_ancestor",
    "orphan_depth",
]
