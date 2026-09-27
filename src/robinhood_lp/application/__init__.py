"""Application / orchestration layer (T072, T097, T110, T113).

The ``robinhood_lp.application`` package is the supported entry
boundary every higher-tier caller (the CLI ``__main__`` surface,
the Web page consumers named by T087 / T103 once their contracts
are amended) composes against. The package owns:

- the :mod:`robinhood_lp.application.backtest` module — the T113
  stable public backtest use case that wires the approved T100
  dataset / partition readers, the T040/T041 replay and T050
  point-in-time feature path, the T061 engine, and the T112
  manifest / evidence publisher behind typed request, result and
  failure records;
- the historical application composition surfaces that earlier
  tasks delivered (T072 realtime, T097 reduce-only, T110
  historical_replay), exposed here as the contract grows.

The package sits at the ``application / orchestration`` tier per
``docs/spec/architecture/ARCHITECTURE.md`` §2.2 and ADR-006.
Composition roots live here; concrete adapter wiring is injected
through typed ports. The package does not import RPC, storage,
signing or execution code.
"""

from __future__ import annotations

from robinhood_lp.application.backtest import (
    BACKTEST_USE_CASE_VERSION,
    BacktestRequest,
    BacktestResult,
    BacktestUseCase,
    BacktestUseCaseError,
    EmptyEventSourceFailure,
    InvalidRunRequestFailure,
    MissingDatasetRegistryFailure,
    PartitionResolutionFailure,
    UnqualifiedPartitionFailure,
    build_default_application,
)

__all__ = [
    "BACKTEST_USE_CASE_VERSION",
    "BacktestRequest",
    "BacktestResult",
    "BacktestUseCase",
    "BacktestUseCaseError",
    "EmptyEventSourceFailure",
    "InvalidRunRequestFailure",
    "MissingDatasetRegistryFailure",
    "PartitionResolutionFailure",
    "UnqualifiedPartitionFailure",
    "build_default_application",
]
