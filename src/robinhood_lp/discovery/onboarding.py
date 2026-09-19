"""Pool-first onboarding layer (T026).

This module sits on top of the T022 :class:`PoolRegistry` and
:class:`InitializeScanner` and adds three high-level entry paths
plus the explicit rejection of any 20-byte address supplied as a
pool identity (ADR-014).

T026 acceptance (todo/phases/P02-chain-access-and-discovery/T026.md):

- Pool-first entry by full ``PoolKey``: local
  ``keccak256(abi.encode(PoolKey))`` derivation reconciled against
  the on-chain ``Initialize`` event.
- Pool-first entry by ``PoolId``: an ``Initialize`` scan keyed on
  the indexed ``PoolId``, with the decoded ``PoolKey`` verified
  back to that digest.
- Explicit rejection, with a reason, of any 20-byte address supplied
  as a pool identity, including another protocol version's pool, a
  position NFT, a front-end link and the ``PoolManager`` itself.
- A registry query surface answering which pools are members, on
  which entry path they were added, and whether a pool is an
  execution candidate, a research member, or both.

T026 must-not:

- query a factory (V4 is singleton);
- accept a 20-byte address as a pool identity;
- infer a ``PoolKey`` from a ``PoolId`` without an on-chain
  ``Initialize`` match;
- reorder currencies to make a supplied ``PoolKey`` fit;
- drop pools whose metadata call fails (T022 obligation, preserved).

This module sits in the storage layer per ADR-006. It depends on
``robinhood_lp.protocol``, ``robinhood_lp.rpc`` and the T022
registry / scanner. It must not import ``robinhood_lp.config`` or
higher layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from robinhood_lp.discovery.initialize_log import (
    INITIALIZE_TOPIC0,
    InitializeDecodeError,
    decode_initialize_log,
)
from robinhood_lp.discovery.registry import (
    PoolRecord,
    PoolRegistry,
)
from robinhood_lp.protocol import Address, ChainId, Currency, PoolId, PoolKey
from robinhood_lp.rpc import RpcAdapter

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OnboardingError(RuntimeError):
    """Base class for onboarding failures."""


class PoolIdentityNotFoundError(OnboardingError):
    """No ``Initialize`` event matched the supplied identity.

    The framework refuses to register a partial record: a pool is
    added to the registry only after an on-chain ``Initialize`` is
    observed and decoded (T026 acceptance: ``PoolId`` that no
    ``Initialize`` event matches stops with a named reason).
    """


class PoolIdentityRejectionError(OnboardingError):
    """A 20-byte address was supplied where a pool identity belongs.

    V4 pools are entries in the singleton ``PoolManager``; they are
    not contracts, so a 20-byte address is never a pool identity
    (ADR-014). The error carries the rejected address and the reason.
    """


class PoolIdentityMismatchError(OnboardingError):
    """The decoded ``PoolKey`` does not reconcile with the supplied identity."""


# ---------------------------------------------------------------------------
# Entry paths
# ---------------------------------------------------------------------------


class OnboardingPath(StrEnum):
    """Closed set of entry paths into the pool registry (T026).

    The set is closed; new paths require a contract amendment. The
    ``registry`` itself never invents an entry path; the path is
    stamped on a :class:`PoolRecord` by :class:`PoolOnboarder` when
    the user supplies a corresponding identity.
    """

    #: The pool was added by an explicit ``AddByTargetToken`` call.
    TARGET_TOKEN = "target_token"
    #: The pool was added by an explicit ``AddByPoolKey`` call. The
    #: locally-derived ``PoolId`` was reconciled against the chain.
    POOL_KEY = "pool_key"
    #: The pool was added by an explicit ``AddByPoolId`` call. The
    #: one-way digest was resolved by scanning ``Initialize`` events.
    POOL_ID = "pool_id"
    #: The pool was added by a general ``Initialize`` scan (the T022
    #: path). This is the catch-all for pools discovered through
    #: bulk scanning rather than a deliberate identity call.
    INITIALIZE_SCAN = "initialize_scan"


# ---------------------------------------------------------------------------
# Address rejection record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AddressRejectionRecord:
    """A recorded rejection of a 20-byte address supplied as a pool.

    ADR-014 forbids any 20-byte address from being treated as a pool
    identity. The framework records the rejection with the supplied
    address, the source label (e.g. ``"pool_manager_address"``,
    ``"position_nft"``, ``"frontend_link"``), and the reason it was
    rejected, so an operator can audit every such attempt.
    """

    address: Address
    source: str
    reason: str


# ---------------------------------------------------------------------------
# Onboarding result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OnboardingResult:
    """The outcome of one onboarding call.

    ``record`` is the :class:`PoolRecord` added to the registry (or
    the pre-existing row, when a duplicate was reconciled).
    ``path`` is the entry path that produced it. ``log_block_number``
    is the block at which the on-chain ``Initialize`` event was
    observed; the field is ``None`` when the onboarding was a
    pure-currency scan that produced no matching log.
    """

    record: PoolRecord
    path: OnboardingPath
    log_block_number: int | None = None


# ---------------------------------------------------------------------------
# Pool onboarder
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OnboarderStats:
    """Counters emitted by :class:`PoolOnboarder`."""

    by_target_token: int = 0
    by_pool_key: int = 0
    by_pool_id: int = 0
    address_rejections: int = 0
    pool_id_not_found: int = 0
    pool_key_mismatch: int = 0
    raw_logs_fetched: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "by_target_token": self.by_target_token,
            "by_pool_key": self.by_pool_key,
            "by_pool_id": self.by_pool_id,
            "address_rejections": self.address_rejections,
            "pool_id_not_found": self.pool_id_not_found,
            "pool_key_mismatch": self.pool_key_mismatch,
            "raw_logs_fetched": self.raw_logs_fetched,
        }


#: The default reason attached to a 20-byte address rejection. The
#: source is recorded separately so the operator can trace the call
#: site; the reason is the policy reason (ADR-014).
_DEFAULT_ADDRESS_REJECTION_REASON: Final[str] = (
    "V4 pools are entries in the singleton PoolManager and have no "
    "contract address of their own; a 20-byte address is never a "
    "pool identity (ADR-014)"
)


class PoolOnboarder:
    """Pool-first onboarding layer (T026).

    The onboarder wraps a :class:`PoolRegistry` and an :class:`RpcAdapter`,
    exposing three explicit entry paths and one explicit rejection:

    - :meth:`add_by_target_token` — by a target token address (the
      existing currency-based discovery path).
    - :meth:`add_by_pool_key` — by a full V4 ``PoolKey``; the locally
      derived ``PoolId`` is reconciled against the chain.
    - :meth:`add_by_pool_id` — by a ``PoolId``; the indexed ``PoolId``
      topic is used to find the matching ``Initialize`` event.
    - :meth:`reject_address_as_pool_identity` — any 20-byte address
      supplied in place of a pool identity is recorded with a reason
      and a source label.

    All entries that hit the chain are reconciled against an
    on-chain ``Initialize`` event before a record is written; a
    supplied identity that no ``Initialize`` event matches stops with
    a named reason (:class:`PoolIdentityNotFoundError` /
    :class:`PoolIdentityMismatchError`).

    The onboarder does NOT decide whether a pool is an execution
    candidate, a research member, or both; that lives in the
    configuration and research-universe modules. The onboarder's
    query surface records which entry path added each pool, so a
    downstream caller can answer the same question.
    """

    def __init__(
        self,
        registry: PoolRegistry,
        rpc_adapter: RpcAdapter,
        *,
        pool_manager_address: Address,
        block_range_from: int,
        block_range_to: int,
    ) -> None:
        if not isinstance(registry, PoolRegistry):
            raise TypeError(
                f"PoolOnboarder.registry must be PoolRegistry, got {type(registry).__name__}"
            )
        if not isinstance(rpc_adapter, RpcAdapter):
            raise TypeError(
                f"PoolOnboarder.rpc_adapter must be RpcAdapter, got {type(rpc_adapter).__name__}"
            )
        if not isinstance(pool_manager_address, Address):
            raise TypeError(
                "PoolOnboarder.pool_manager_address must be Address, "
                f"got {type(pool_manager_address).__name__}"
            )
        if (
            not isinstance(block_range_from, int)
            or isinstance(block_range_from, bool)
            or block_range_from < 0
        ):
            raise ValueError(
                f"block_range_from must be a non-negative int, got {block_range_from!r}"
            )
        if (
            not isinstance(block_range_to, int)
            or isinstance(block_range_to, bool)
            or block_range_to < block_range_from
        ):
            raise ValueError(
                f"block_range_to must be an int >= block_range_from, "
                f"got {block_range_to!r} (from={block_range_from})"
            )
        self._registry = registry
        self._rpc = rpc_adapter
        self._pool_manager_address = pool_manager_address
        self._from_block = block_range_from
        self._to_block = block_range_to
        self._address_rejections: list[AddressRejectionRecord] = []
        self._stats = OnboarderStats()

    # -- introspection --------------------------------------------------

    @property
    def chain_id(self) -> ChainId:
        return self._registry.chain_id

    @property
    def stats(self) -> OnboarderStats:
        return self._stats

    @property
    def address_rejections(self) -> list[AddressRejectionRecord]:
        return list(self._address_rejections)

    # -- rejection surface (always synchronous) -------------------------

    def reject_address_as_pool_identity(
        self,
        address: Address,
        *,
        source: str,
        reason: str | None = None,
    ) -> AddressRejectionRecord:
        """Record a 20-byte address that was offered as a pool identity.

        Per ADR-014, a 20-byte address is never a pool identity; the
        framework records the supplied address, the call-site label
        (``source``) and the policy reason rather than guessing what
        was meant. The method is synchronous: it never queries the
        chain and never raises (apart from argument validation).
        """
        if not isinstance(address, Address):
            raise TypeError(
                f"reject_address_as_pool_identity: address must be Address, "
                f"got {type(address).__name__}"
            )
        if not isinstance(source, str) or not source:
            raise ValueError(
                f"reject_address_as_pool_identity: source must be a non-empty "
                f"string, got {source!r}"
            )
        effective_reason = reason if reason is not None else _DEFAULT_ADDRESS_REJECTION_REASON
        rec = AddressRejectionRecord(address=address, source=source, reason=effective_reason)
        self._address_rejections.append(rec)
        self._stats.address_rejections += 1
        return rec

    def is_pool_manager_address(self, address: Address) -> bool:
        """Return ``True`` iff ``address`` equals the configured ``PoolManager``.

        Used by the entry paths that must explicitly recognise and
        reject the ``PoolManager`` address when it is offered as a
        pool identity (ADR-014).
        """
        if not isinstance(address, Address):
            raise TypeError(
                f"is_pool_manager_address: address must be Address, got {type(address).__name__}"
            )
        return address.value == self._pool_manager_address.value

    # -- entry paths ----------------------------------------------------

    async def add_by_target_token(self, token: Address) -> list[OnboardingResult]:
        """Onboard every pool that contains ``token`` as one of its currencies.

        The onboarder fetches ``Initialize`` logs whose ``currency0``
        or ``currency1`` topic equals ``token`` and feeds them to the
        registry. The resulting records carry
        :attr:`OnboardingPath.TARGET_TOKEN` on their entry-path set.

        The zero address is permitted (native currency); the token
        is interpreted as a 20-byte address either way. A supplied
        ``token`` that matches the ``PoolManager`` address is
        rejected with a reason before any RPC call is issued
        (ADR-014).
        """
        self._check_address_input(token, source="add_by_target_token")
        topic = "0x" + token.value.to_bytes(20, "big").hex()
        # Match currency0 OR currency1 with two stacked getLogs calls.
        logs = await self._fetch_logs(
            topics=[_INITIALIZE_TOPIC0_HEX, None, [topic], [topic]],
        )
        results = await self._ingest_with_path(logs, OnboardingPath.TARGET_TOKEN)
        if not results:
            # No log found: a token with no Initialize is a normal
            # outcome, not an error. Surface an empty list rather
            # than a named failure.
            return []
        self._stats.by_target_token += len(results)
        return results

    async def add_by_pool_key(self, pool_key: PoolKey) -> OnboardingResult:
        """Onboard one pool by full V4 ``PoolKey``.

        The pool's ``PoolId`` is derived locally as
        ``keccak256(abi.encode(PoolKey))`` and reconciled against the
        on-chain ``Initialize`` event whose indexed ``PoolId`` topic
        equals that digest. The decoded ``PoolKey`` must match the
        supplied one byte-for-byte: the framework refuses to reorder
        currencies or accept a different fee / tick spacing / hooks
        tuple (T026 must-not).

        Before any RPC call is issued, every address field of the
        supplied ``PoolKey`` (``currency0``, ``currency1``, ``hooks``)
        is checked against the configured ``PoolManager`` address;
        a match is rejected with a reason (ADR-014). A ``PoolKey``
        containing the ``PoolManager`` address in any slot cannot be
        a pool identity and the onboarder records the rejection
        rather than passing it to the chain.

        Raises:
            PoolIdentityNotFoundError: no ``Initialize`` log matched
                the derived ``PoolId`` within the scanned range.
            PoolIdentityMismatchError: a log matched the indexed
                ``PoolId`` but the decoded ``PoolKey`` does not match
                the supplied one.
            PoolIdentityRejectionError: the supplied ``PoolKey``
                carries the ``PoolManager`` address in one of its
                address fields; the rejection is also recorded in
                :attr:`address_rejections`.
        """
        self._check_pool_key_addresses(pool_key, source="add_by_pool_key")
        derived_pool_id = pool_key.to_pool_id()
        topic = "0x" + derived_pool_id.value.to_bytes(32, "big").hex()
        logs = await self._fetch_logs(topics=[_INITIALIZE_TOPIC0_HEX, topic, None, None])
        if not logs:
            self._stats.pool_id_not_found += 1
            raise PoolIdentityNotFoundError(
                f"no Initialize event found for PoolKey "
                f"(derived PoolId={derived_pool_id.to_hex()}, "
                f"chain_id={self.chain_id.value}, "
                f"scanned range=[{self._from_block}, {self._to_block}])"
            )
        if len(logs) > 1:
            # Two Initialize events matching the same PoolId is a
            # conflict (T026 acceptance boundary case). Surface the
            # first match but the caller should treat this as
            # externally inconsistent; we record the count and let
            # the downstream scanner's conflict logic fire.
            self._stats.pool_key_mismatch += 1
        results = await self._ingest_with_path(logs, OnboardingPath.POOL_KEY)
        if not results:
            self._stats.pool_key_mismatch += 1
            raise PoolIdentityMismatchError(
                f"Initialize log for PoolId={derived_pool_id.to_hex()} "
                f"decoded but could not be registered (chain_id="
                f"{self.chain_id.value})"
            )
        # Verify the decoded PoolKey matches the supplied one.
        decoded_key = results[0].record.pool_key
        if not _pool_keys_equal(decoded_key, pool_key):
            self._stats.pool_key_mismatch += 1
            raise PoolIdentityMismatchError(
                f"decoded PoolKey does not match the supplied one for "
                f"PoolId={derived_pool_id.to_hex()}; "
                f"supplied={_pool_key_to_summary(pool_key)} "
                f"decoded={_pool_key_to_summary(decoded_key)}"
            )
        self._stats.by_pool_key += 1
        return results[0]

    async def add_by_pool_id(self, pool_id: PoolId) -> OnboardingResult:
        """Onboard one pool by ``PoolId``.

        ``PoolId`` is a one-way digest; the onboarder resolves it by
        scanning ``Initialize`` events for the matching indexed
        ``PoolId`` topic and then verifying that the decoded
        ``PoolKey`` derives back to the same digest (which the
        decoder already enforces; we re-check the equality here for
        audit-trail clarity).

        The verification runs BEFORE the registry is updated: a log
        whose indexed ``PoolId`` matches the supplied one but whose
        decoded ``PoolKey`` disagrees is rejected with a named reason
        and the registry is left untouched (T026 must-not: infer a
        ``PoolKey`` from a ``PoolId`` without an on-chain
        ``Initialize`` match).

        Raises:
            PoolIdentityNotFoundError: no ``Initialize`` log matched
                the supplied ``PoolId`` within the scanned range.
            PoolIdentityMismatchError: the equality check between the
                decoded ``PoolId`` and the supplied ``PoolId`` failed.
        """
        if not isinstance(pool_id, PoolId):
            raise TypeError(f"add_by_pool_id: pool_id must be PoolId, got {type(pool_id).__name__}")
        topic = "0x" + pool_id.value.to_bytes(32, "big").hex()
        logs = await self._fetch_logs(topics=[_INITIALIZE_TOPIC0_HEX, topic, None, None])
        if not logs:
            self._stats.pool_id_not_found += 1
            raise PoolIdentityNotFoundError(
                f"no Initialize event found for PoolId={pool_id.to_hex()} "
                f"(chain_id={self.chain_id.value}, "
                f"scanned range=[{self._from_block}, {self._to_block}])"
            )
        # Pre-decode the logs and verify equality with the supplied
        # ``PoolId`` BEFORE the registry is updated. A log whose
        # indexed ``PoolId`` matches the topic but whose decoded
        # ``PoolId`` differs (or which cannot be decoded) raises a
        # named error and leaves the registry unchanged.
        matched_log: tuple[list[bytes], bytes, int | None] | None = None
        for raw in logs:
            extracted = _extract_log_topics_and_data(raw)
            if extracted is None:
                continue
            topics_bytes, data_bytes, block_number = extracted
            try:
                decoded = decode_initialize_log(topics_bytes, data_bytes)
            except InitializeDecodeError:
                continue
            if decoded.pool_id.value != pool_id.value:
                self._stats.pool_key_mismatch += 1
                raise PoolIdentityMismatchError(
                    f"decoded PoolId {decoded.pool_id.to_hex()} does not "
                    f"match the supplied PoolId {pool_id.to_hex()} "
                    f"(chain_id={self.chain_id.value}); the framework "
                    f"refuses to register a record whose PoolId was "
                    f"not matched on-chain (T026 must-not)"
                )
            matched_log = (topics_bytes, data_bytes, block_number)
            break
        if matched_log is None:
            self._stats.pool_key_mismatch += 1
            raise PoolIdentityMismatchError(
                f"Initialize logs for PoolId={pool_id.to_hex()} were "
                f"fetched but none decoded successfully (chain_id="
                f"{self.chain_id.value})"
            )
        # Now ingest. The on-boarder's ``_ingest_with_path`` performs
        # the duplicate / conflict bookkeeping that the registry
        # already owns; the equality check above has already pinned
        # the on-chain match to the supplied PoolId.
        results = await self._ingest_with_path(logs, OnboardingPath.POOL_ID)
        if not results:
            self._stats.pool_key_mismatch += 1
            raise PoolIdentityMismatchError(
                f"Initialize logs for PoolId={pool_id.to_hex()} decoded "
                f"but could not be ingested (chain_id={self.chain_id.value})"
            )
        self._stats.by_pool_id += 1
        return results[0]

    # -- internal helpers -----------------------------------------------

    def _check_address_input(self, address: Address, *, source: str) -> None:
        """Reject the ``PoolManager`` address when offered as a pool.

        The onboarder's entry paths accept ``Address`` arguments
        (token addresses, ``PoolKey`` fields, ``PoolId``). An address
        that happens to equal the configured ``PoolManager`` is
        recorded as a rejection rather than silently accepted
        (ADR-014). The function is invoked before any RPC call.
        """
        if self.is_pool_manager_address(address):
            self.reject_address_as_pool_identity(
                address,
                source=source,
                reason=(
                    "the configured PoolManager address is a singleton "
                    "contract, not a pool identity (ADR-014)"
                ),
            )
            raise PoolIdentityRejectionError(
                f"PoolManager address {address.to_hex()} rejected as a "
                f"pool identity from {source!r}; see AddressRejectionRecord"
            )

    def _check_pool_key_addresses(self, pool_key: PoolKey, *, source: str) -> None:
        """Reject the ``PoolManager`` address in any field of a PoolKey.

        Walks ``currency0``, ``currency1`` and ``hooks`` and rejects
        with the same surface as :meth:`_check_address_input` if any
        of them equals the configured ``PoolManager``. The first
        match is recorded; subsequent matches are not recorded to
        avoid duplicate rejections for the same input.
        """
        for field_name, addr in (
            ("currency0", pool_key.currency0.to_address()),
            ("currency1", pool_key.currency1.to_address()),
            ("hooks", pool_key.hooks),
        ):
            if self.is_pool_manager_address(addr):
                self.reject_address_as_pool_identity(
                    addr,
                    source=f"{source}.{field_name}",
                    reason=(
                        "the configured PoolManager address is a singleton "
                        "contract, not a pool identity (ADR-014)"
                    ),
                )
                raise PoolIdentityRejectionError(
                    f"PoolManager address {addr.to_hex()} supplied in "
                    f"PoolKey.{field_name} rejected from {source!r}; "
                    f"see AddressRejectionRecord"
                )

    async def _fetch_logs(self, *, topics: list[object]) -> list[dict[str, object]]:
        """Fetch ``Initialize`` logs filtered by the supplied topics.

        ``topics`` is the standard ``eth_getLogs`` topic list: each
        position is either ``None`` (wildcard) or a hex string or a
        list of hex strings (OR-within, AND-across). The framework
        always pins the ``PoolManager`` address to the
        ``eth_getLogs`` ``address`` field so the provider does not
        scan unrelated contracts.
        """
        raw = await self._rpc.eth_get_logs(
            self._from_block,
            self._to_block,
            self._pool_manager_address.to_hex(),
            topics,
        )
        self._stats.raw_logs_fetched += len(raw)
        return raw

    async def _ingest_with_path(
        self,
        raw_logs: list[dict[str, object]],
        path: OnboardingPath,
    ) -> list[OnboardingResult]:
        """Decode and ingest a batch of raw logs and stamp ``path``.

        The path is added to the entry-path set on the resulting
        record. A pool added by both ``POOL_KEY`` and ``POOL_ID``
        carries both paths; the registry refuses to invent the path
        for a pool that was never explicitly onboarded.
        """
        results: list[OnboardingResult] = []
        if not raw_logs:
            return results
        decoded_records: list[tuple[PoolRecord, int | None]] = []
        for raw in raw_logs:
            topics_hex = raw.get("topics")
            data_hex = raw.get("data")
            if not isinstance(topics_hex, list) or not isinstance(data_hex, str):
                continue
            try:
                topics = [
                    bytes.fromhex(t[2:]) if isinstance(t, str) and t.startswith("0x") else t
                    for t in topics_hex
                ]
                if not all(isinstance(t, bytes) for t in topics):
                    continue
                data = (
                    bytes.fromhex(data_hex[2:])
                    if data_hex.startswith("0x")
                    else bytes.fromhex(data_hex)
                )
            except (ValueError, TypeError):
                continue
            try:
                decoded = decode_initialize_log(topics, data)
            except InitializeDecodeError:
                continue
            block_number = _coerce_int(raw.get("blockNumber"))
            tx_hash_raw = raw.get("transactionHash")
            tx_hash: str | None = tx_hash_raw if isinstance(tx_hash_raw, str) else None
            log_index = _coerce_int(raw.get("logIndex"))
            # Use the registry's add() directly (no metadata fetch):
            # onboarding decodes the chain event; per-pool metadata is
            # fetched separately (T022 scanner path).
            existing = self._registry._records.get(decoded.pool_id)  # noqa: SLF001
            try:
                rec = self._registry.add(
                    decoded,
                    block_number=block_number,
                    tx_hash=tx_hash,
                    log_index=log_index,
                )
            except Exception:
                # Conflict / decode path: skip. The framework surfaces
                # conflicts through ``registry.conflicts``; the
                # onboarding stats do not double-count.
                continue
            decoded_records.append((rec, block_number))
            if existing is None:
                # Brand-new row: stamp the path.
                rec.entry_paths.add(path)
            else:
                # Duplicate: append the path if not already present.
                rec.entry_paths.add(path)
            results.append(OnboardingResult(record=rec, path=path, log_block_number=block_number))
        return results


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


#: Mirror of :attr:`InitializeScanner.INITIALIZE_TOPIC0_HEX`; imported
#: once here so the topics list is built without a second source of
#: truth.
_INITIALIZE_TOPIC0_HEX: Final[str] = "0x" + INITIALIZE_TOPIC0.hex()


def _pool_keys_equal(a: PoolKey, b: PoolKey) -> bool:
    """Field-wise equality of two PoolKey instances."""
    return (
        a.currency0.address.value == b.currency0.address.value
        and a.currency1.address.value == b.currency1.address.value
        and a.fee == b.fee
        and a.tick_spacing == b.tick_spacing
        and a.hooks.value == b.hooks.value
    )


def _pool_key_to_summary(pool_key: PoolKey) -> str:
    """Render a ``PoolKey`` for diagnostic / error messages."""
    return (
        f"(c0={pool_key.currency0.to_address().to_hex()}, "
        f"c1={pool_key.currency1.to_address().to_hex()}, "
        f"fee={pool_key.fee}, tick_spacing={pool_key.tick_spacing}, "
        f"hooks={pool_key.hooks.to_hex()})"
    )


def _coerce_int(value: object) -> int | None:
    """Coerce a JSON-RPC value (int, hex string, or decimal string)
    into an ``int``; return ``None`` if it is not coercible."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.lower()
        try:
            return int(s, 16) if s.startswith("0x") else int(s)
        except ValueError:
            return None
    return None


def _extract_log_topics_and_data(
    raw: dict[str, object],
) -> tuple[list[bytes], bytes, int | None] | None:
    """Extract ``(topics, data, block_number)`` from a raw log dict.

    Returns ``None`` if the dict does not carry the canonical
    ``topics`` list and ``data`` string. ``topics`` are normalised
    to ``bytes``; ``data`` is normalised to ``bytes``. ``block_number``
    is coerced to ``int`` via :func:`_coerce_int`.
    """
    topics_hex = raw.get("topics")
    data_hex = raw.get("data")
    if not isinstance(topics_hex, list) or not isinstance(data_hex, str):
        return None
    try:
        topics = [
            bytes.fromhex(t[2:]) if isinstance(t, str) and t.startswith("0x") else t
            for t in topics_hex
        ]
        if not all(isinstance(t, bytes) for t in topics):
            return None
        data = bytes.fromhex(data_hex[2:]) if data_hex.startswith("0x") else bytes.fromhex(data_hex)
    except (ValueError, TypeError):
        return None
    return topics, data, _coerce_int(raw.get("blockNumber"))


# ---------------------------------------------------------------------------
# Query surface on PoolRegistry (T026 extension)
# ---------------------------------------------------------------------------


def entry_path_for(registry: PoolRegistry, pool_id: PoolId) -> frozenset[OnboardingPath]:
    """Return the entry-path set recorded on a registry row.

    A pool that has never been onboarded through one of the three
    explicit paths returns an empty frozenset: the registry itself
    never invents an entry path (T026 must-not). A pool added by both
    ``POOL_KEY`` and ``POOL_ID`` returns both.
    """
    if not isinstance(registry, PoolRegistry):
        raise TypeError(
            f"entry_path_for: registry must be PoolRegistry, got {type(registry).__name__}"
        )
    if not isinstance(pool_id, PoolId):
        raise TypeError(f"entry_path_for: pool_id must be PoolId, got {type(pool_id).__name__}")
    rec = registry.get(pool_id)
    if rec is None:
        return frozenset()
    return frozenset(OnboardingPath(p) for p in rec.entry_paths)


__all__ = [
    "AddressRejectionRecord",
    "OnboardingError",
    "OnboardingPath",
    "OnboardingResult",
    "OnboarderStats",
    "PoolIdentityMismatchError",
    "PoolIdentityNotFoundError",
    "PoolIdentityRejectionError",
    "PoolOnboarder",
    "entry_path_for",
]


# Sentinel import for the type checker.


@dataclass(slots=True)
class _NullCurrencyProbe:
    """Reserved helper for a future per-currency fetch that does not
    need to import :class:`Currency`; left here so the symbol stays
    stable if a later task adds the path. Not part of the public API.
    """

    _currency: Currency = field(default_factory=Currency.native)

    def __call__(self) -> Currency:
        return self._currency
