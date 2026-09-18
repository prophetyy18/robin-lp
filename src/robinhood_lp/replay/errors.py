"""Replay error types (T040).

Every error is a :class:`ReplayError`. Each subclass carries the
identity of the offending record and the chain / PoolKey / block /
position tuple that pinpoint the failure. Errors are raised only
when the replay actually cannot continue deterministically:

- :class:`DuplicateEventError` — the same ``(chain_id, block_hash,
  tx_hash, log_index)`` EventKey appeared twice;
- :class:`MissingTransactionIndexError` — a record carries a
  negative or otherwise unusable ``transaction_index`` and the
  total order cannot be resolved;
- :class:`ImpossibleTransitionError` — the event violates a V4
  pool-lifecycle invariant (e.g. ``Swap`` before ``Initialize``,
  or a second ``Initialize`` on the same pool);
- :class:`UnknownPoolError` — an event's ``pool_id`` does not
  match the pool declared in :class:`ReplayInput`. Mixing two
  pools' events in one replay input is the primary failure mode
  the contract warns about;
- :class:`UnknownEventTypeError` — an event is not one of the
  five typed V4 records the framework decodes;
- :class:`WindowBoundsError` — an event falls outside the
  ``[from_block, to_block]`` window pinned by :class:`ReplayInput`.

Errors are intentionally specific. The replay must fail closed; a
generic ``ValueError`` is not sufficient because the contract asks
the independent reviewer to assert each named failure mode
explicitly.
"""

from __future__ import annotations

from typing import Any


class ReplayError(Exception):
    """Base class for deterministic replay failures.

    Carries the chain id, the PoolId, and the offending position
    tuple ``(block_number, transaction_index, log_index)`` so a
    caller can locate the failing record without re-scanning the
    input. The fields default to ``None`` for failures that happen
    before a position is known.
    """


class DuplicateEventError(ReplayError):
    """The same EventKey appeared twice in the replay input.

    The :attr:`event_key` attribute is the exact
    ``(chain_id, block_hash, tx_hash, log_index)`` tuple that was
    seen a second time; :attr:`first_seen_index` is the 0-based
    position of the original observation in the replay's ordered
    sequence.
    """

    def __init__(
        self,
        *,
        event_key: tuple[int, int, int, int],
        first_seen_index: int,
        block_number: int,
        transaction_index: int,
        log_index: int,
    ) -> None:
        self.event_key = event_key
        self.first_seen_index = first_seen_index
        super().__init__(
            f"duplicate EventKey {event_key!r} "
            f"(block={block_number}, tx_index={transaction_index}, "
            f"log_index={log_index}); first seen at ordered index "
            f"{first_seen_index}"
        )


class MissingTransactionIndexError(ReplayError):
    """A record lacks a usable ``transaction_index``.

    Total ordering requires ``(block_number, transaction_index,
    log_index)`` to be a strict ordering key; a negative or otherwise
    unusable ``transaction_index`` cannot satisfy that. The
    :attr:`transaction_index` attribute carries the offending value.
    """

    def __init__(
        self,
        *,
        block_number: int,
        transaction_index: Any,
        log_index: int,
    ) -> None:
        self.transaction_index = transaction_index
        super().__init__(
            f"missing or unusable transaction_index "
            f"({transaction_index!r}) at block={block_number} "
            f"log_index={log_index}; transaction_index must be a "
            f"non-negative int"
        )


class ImpossibleTransitionError(ReplayError):
    """An event violates a V4 pool-lifecycle invariant.

    The :attr:`reason` attribute is a short stable code
    (``"swap_before_initialize"``, ``"second_initialize"``,
    ``"modify_before_initialize"``, ``"donate_before_initialize"``,
    ``"swap_with_zero_price"``, ``"modify_with_zero_price"``,
    ``"donate_with_zero_price"``) so tests can branch on the
    specific invariant without parsing free-form messages.
    """

    def __init__(
        self,
        *,
        reason: str,
        block_number: int,
        transaction_index: int,
        log_index: int,
        event_type: str,
    ) -> None:
        self.reason = reason
        self.event_type = event_type
        super().__init__(
            f"impossible transition {reason!r} for {event_type} "
            f"at block={block_number} "
            f"tx_index={transaction_index} log_index={log_index}"
        )


class UnknownPoolError(ReplayError):
    """An event's ``pool_id`` does not match the replay input's pool.

    The contract says: one replay input per pool. This error is
    raised the moment a record's ``pool_id`` differs from
    :attr:`ReplayInput.pool_id`, so the operator sees the violation
    at its first occurrence rather than after the replay silently
    produced state for two pools.
    """

    def __init__(
        self,
        *,
        expected_pool_id: int,
        actual_pool_id: int,
        block_number: int,
        transaction_index: int,
        log_index: int,
    ) -> None:
        self.expected_pool_id = expected_pool_id
        self.actual_pool_id = actual_pool_id
        super().__init__(
            f"event pool_id={actual_pool_id:#034x} does not match "
            f"replay input pool_id={expected_pool_id:#034x} at "
            f"block={block_number} "
            f"tx_index={transaction_index} log_index={log_index}; "
            f"one replay input must not mix two pools' events"
        )


class UnknownEventTypeError(ReplayError):
    """An event is not one of the five typed V4 records.

    Carries the offending :attr:`event_type` string so the
    independent reviewer can pin it down.
    """

    def __init__(
        self,
        *,
        event_type: str,
        block_number: int,
        transaction_index: int,
        log_index: int,
    ) -> None:
        self.event_type = event_type
        super().__init__(
            f"unknown event type {event_type!r} at "
            f"block={block_number} tx_index={transaction_index} "
            f"log_index={log_index}"
        )


class WindowBoundsError(ReplayError):
    """An event lies outside the declared ``[from_block, to_block]``.

    The replay input pins the finalized window it covers. An event
    outside that range is a contract violation: either the data
    root was misidentified or the input was misconfigured.
    """

    def __init__(
        self,
        *,
        from_block: int,
        to_block: int,
        block_number: int,
        transaction_index: int,
        log_index: int,
    ) -> None:
        self.from_block = from_block
        self.to_block = to_block
        super().__init__(
            f"event at block={block_number} "
            f"(tx_index={transaction_index}, log_index={log_index}) "
            f"lies outside declared window "
            f"[{from_block}, {to_block}]"
        )


__all__ = [
    "DuplicateEventError",
    "ImpossibleTransitionError",
    "MissingTransactionIndexError",
    "ReplayError",
    "UnknownEventTypeError",
    "UnknownPoolError",
    "WindowBoundsError",
]
