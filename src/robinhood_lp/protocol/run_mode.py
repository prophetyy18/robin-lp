"""Support-level classification (T002 / T023 / ADR-005).

Five levels, matching ``TODO.md`` §3 and ``docs/product/ASSET_ADMISSION.md``.
The order is intentionally from "lowest evidence required" to
"highest evidence required":

- ``rejected``:  retain reason only; no chain interaction permitted.
- ``ingestion``: raw + normalized history and quality reports.
- ``backtest``:  deterministic replay, valuation, backtest.
- ``paper``:     real-time decisions and simulated ledger.
- ``live``:     approved mainnet LP / Swap execution within V1 limits.

The T023 classifier assigns one of these levels to every pool in
the registry. V1 only ever promotes pools whose target chain is
Robinhood Chain (ADR-005); other chains remain at ``rejected``
within V1.
"""

from __future__ import annotations

from enum import StrEnum


class RunMode(StrEnum):
    """Pool / system support level (T002 / T023)."""

    REJECTED = "rejected"
    INGESTION = "ingestion"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


__all__ = ["RunMode"]
