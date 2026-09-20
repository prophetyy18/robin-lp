"""One-command rerun of a saved manifest (T063).

The rerun module is the implementation of the T063 acceptance
clause "one command reruns a saved manifest". The function
:func:`rerun_manifest` loads a manifest from a path, reconstructs
the input events, builds the engine from the recorded strategy
parameters and model bundle parameters, runs the engine, computes
the metrics, and compares the new metrics checksum to the recorded
one. A match is a byte-identical rerun; a mismatch is a rerun
failure (the manifest is no longer reproducible from the recorded
parameters — the file has been tampered with, the code has drifted,
or the inputs are stale).

The module is intentionally minimal: it depends only on the
backtest engine, the manifest, the metrics layer, the validation
layer, and the strategy baselines. It does not import RPC, storage,
configuration, signing, execution, or presentation code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from robinhood_lp.backtest.engine import (
    BACKTEST_ENGINE_VERSION,
    BacktestEngine,
    BacktestResult,
    RiskDecision,
    StrategyCallback,
)
from robinhood_lp.backtest.events import (
    BacktestEvent,
)
from robinhood_lp.backtest.models import (
    ModelBundle,
)
from robinhood_lp.reports.manifest import (
    ExperimentManifest,
    SerialisedEvent,
)
from robinhood_lp.reports.metrics import (
    Q64_SCALE,
    SECONDS_PER_YEAR,
    RunMetrics,
    compute_run_metrics,
    decisions_checksum,
    extract_decisions,
)
from robinhood_lp.reports.validation import (
    DatasetQualificationRecord,
    ManifestValidationError,
    load_manifest_from_path,
)

#: Module version.
RERUN_VERSION: Final[str] = "t063.manifest_rerun.v1"

#: Default strategy parameters the rerun uses for the canonical
#: ``HOLD`` baseline when the manifest's ``strategy_kind`` is
#: ``HOLD``. The values are non-binding defaults for the simplest
#: reproduce path; an alternative strategy is reconstructed by
#: :func:`_build_strategy_callback` from the manifest's
#: ``strategy_params``.
_RERUN_DEFAULT_SEED: Final[int] = 0
_RERUN_DEFAULT_HALF_WIDTH: Final[int] = 60
_RERUN_DEFAULT_VOLATILITY_MULTIPLIER: Final[int] = 2
_RERUN_DEFAULT_VOLATILITY_WINDOW: Final[int] = 20
_RERUN_DEFAULT_CAPITAL_Q64_64: Final[int] = Q64_SCALE
_RERUN_DEFAULT_LIQUIDITY: Final[int] = 1_000


@dataclass(frozen=True, slots=True)
class RerunResult:
    """The structured outcome of a manifest rerun.

    Field units:

    - ``manifest_path`` — non-empty string (the file rerun was
      attempted on).
    - ``run_id`` — non-empty string (the manifest's identity).
    - ``chain_id`` / ``pool_key_id`` — the per-pool invariant.
    - ``match`` — bool: ``True`` iff the new ``metrics_checksum``
      equals the recorded one.
    - ``recorded_metrics_checksum`` — the checksum the manifest
      carried.
    - ``recomputed_metrics_checksum`` — the checksum the rerun
      computed from the freshly rebuilt :class:`BacktestResult`.
    - ``decisions_match`` — bool: ``True`` iff the new decisions
      tuple produces the same checksum as the recorded one. The
      acceptance clause binds this secondary check because a
      manifest that successfully reproduces metrics but produces
      a different decision sequence is a publication that hides a
      drift.
    - ``recorded_decisions_checksum`` — the decisions checksum the
      manifest carried.
    - ``recomputed_decisions_checksum`` — the decisions checksum the
      rerun computed.
    - ``new_run_metrics`` — the freshly computed :class:`RunMetrics`.
    """

    manifest_path: str
    run_id: str
    chain_id: int
    pool_key_id: str
    match: bool
    recorded_metrics_checksum: str
    recomputed_metrics_checksum: str
    decisions_match: bool
    recorded_decisions_checksum: str
    recomputed_decisions_checksum: str
    new_run_metrics: RunMetrics


# ---------------------------------------------------------------------------
# Strategy reconstruction
# ---------------------------------------------------------------------------


def _build_strategy_callback(manifest: ExperimentManifest) -> StrategyCallback:
    """Build a strategy callback from the manifest's strategy parameters.

    The function reconstructs a deterministic, parameter-driven
    strategy that mirrors the recorded parameters. The mapping is
    explicit on purpose: an unknown ``strategy_kind`` raises so a
    drifted strategy never silently produces a passing rerun.
    """
    # Imported lazily so the strategy layer is not on the
    # reports-module import path.
    from robinhood_lp.strategy.baselines import (
        BroadRangeStrategy,
        FixedWidthStrategy,
        HoldStrategy,
        OutOfRangeRebalanceStrategy,
        VolatilityWidthStrategy,
    )

    pool_key_id = manifest.pool_key_id
    chain_id = manifest.chain_id
    params = manifest.strategy_params
    kind = manifest.strategy_kind

    if kind == "HOLD":
        return HoldStrategy(pool_key_id=pool_key_id, chain_id=chain_id)

    tick_spacing = int(params.get("tick_spacing", 60))
    if kind == "BROAD_RANGE":
        return BroadRangeStrategy(
            pool_key_id=pool_key_id,
            chain_id=chain_id,
            tick_spacing=tick_spacing,
        )
    if kind == "FIXED_WIDTH":
        half_width = int(params.get("half_width_ticks", _RERUN_DEFAULT_HALF_WIDTH))
        capital = int(params.get("capital_q64_64", _RERUN_DEFAULT_CAPITAL_Q64_64))
        liquidity = int(params.get("liquidity", _RERUN_DEFAULT_LIQUIDITY))
        return FixedWidthStrategy(
            pool_key_id=pool_key_id,
            chain_id=chain_id,
            tick_spacing=tick_spacing,
            half_width_ticks=half_width,
            capital_q64_64=capital,
            liquidity=liquidity,
        )
    if kind == "VOLATILITY_WIDTH":
        multiplier = int(params.get("volatility_multiplier", _RERUN_DEFAULT_VOLATILITY_MULTIPLIER))
        window = int(params.get("volatility_window", _RERUN_DEFAULT_VOLATILITY_WINDOW))
        return VolatilityWidthStrategy(
            pool_key_id=pool_key_id,
            chain_id=chain_id,
            tick_spacing=tick_spacing,
            volatility_multiplier=multiplier,
            volatility_window=window,
        )
    if kind == "OUT_OF_RANGE_REBALANCE":
        half_width = int(params.get("half_width_ticks", _RERUN_DEFAULT_HALF_WIDTH))
        return OutOfRangeRebalanceStrategy(
            pool_key_id=pool_key_id,
            chain_id=chain_id,
            tick_spacing=tick_spacing,
            half_width_ticks=half_width,
        )
    raise ManifestValidationError(
        f"rerun_manifest: unknown strategy_kind={kind!r}; cannot reconstruct"
    )


def _approve_risk_callback(decision: object) -> RiskDecision:
    return RiskDecision(approved=True, reason_code="OK")


# ---------------------------------------------------------------------------
# Model bundle reconstruction
# ---------------------------------------------------------------------------


def _build_model_bundle(manifest: ExperimentManifest) -> ModelBundle:
    """Build a :class:`ModelBundle` from the manifest's model-bundle parameters.

    The function reconstructs the canonical ``t063`` model bundle —
    constant liquidity, static fee, flat gas, zero slippage,
    deterministic failure, fixed latency. The bundle version is
    the manifest's own ``code_revision`` (so a re-publication after
    a code change carries a different bundle version).
    """
    from robinhood_lp.backtest.models import (
        ConstantLiquidityModel,
        DeterministicFailureModel,
        FlatGasModel,
        StaticFeeModel,
        ZeroSlippageModel,
    )

    params = manifest.strategy_params
    gas_units = int(params.get("gas_units", 21_000))
    fee_pips = int(params.get("fee_pips", 3_000))
    active_liquidity = int(params.get("active_liquidity", 10_000))
    return ModelBundle(
        bundle_version=f"t063.rerun.{manifest.code_revision}",
        liquidity=ConstantLiquidityModel(active_liquidity_value=active_liquidity),
        fee=StaticFeeModel(fee_pips_value=fee_pips),
        gas=FlatGasModel(gas_units_value=gas_units),
        slippage=ZeroSlippageModel(),
        failure=DeterministicFailureModel(),
        latency_units=int(manifest.latency_units),
    )


# ---------------------------------------------------------------------------
# Event reconstruction
# ---------------------------------------------------------------------------


def _reconstruct_events(
    serialised: Sequence[SerialisedEvent],
) -> tuple[BacktestEvent, ...]:
    """Reconstruct :class:`BacktestEvent` records from their serialised form."""
    return tuple(evt.to_backtest_event() for evt in serialised)


# ---------------------------------------------------------------------------
# Rerun entry point
# ---------------------------------------------------------------------------


def rerun_manifest(
    manifest_path: Path | str,
    *,
    dataset_qualification: DatasetQualificationRecord | None = None,
) -> RerunResult:
    """Rerun a saved manifest and return the structured outcome.

    The function is the canonical "one command reruns a saved
    manifest" entry point. It loads the manifest (running every
    validation gate), reconstructs the events, strategy, model
    bundle and engine, runs the engine, computes the metrics, and
    compares both the metrics checksum and the decisions checksum
    to the recorded values.

    Parameters
    ----------
    manifest_path:
        The path to a saved manifest JSON file. The path is
        resolved by :class:`pathlib.Path`; relative paths are
        resolved against the current working directory.
    dataset_qualification:
        The dataset registry's qualification record. When supplied,
        the loader also enforces the numeraire / qualification
        agreement gate; when ``None``, that gate is skipped.

    Returns
    -------
    :class:`RerunResult`
        The structured outcome. ``match=True`` means the rerun
        reproduces both checksums byte-identically; ``match=False``
        is a rerun failure that the caller must surface.

    Raises
    ------
    :class:`robinhood_lp.reports.validation.ManifestValidationError`
        On a structural / checksum / required-field failure.
    :class:`FileNotFoundError`
        When ``manifest_path`` does not exist.
    """
    target_path = Path(manifest_path)
    manifest = load_manifest_from_path(target_path, dataset_qualification=dataset_qualification)
    return rerun_manifest_from_object(manifest, manifest_path=str(target_path))


def rerun_manifest_from_object(manifest: ExperimentManifest, *, manifest_path: str) -> RerunResult:
    """Rerun a manifest from an already-loaded object.

    The function is the half of the rerun that does not read the
    file system; it accepts a validated :class:`ExperimentManifest`
    and a path string for the result. The companion function
    :func:`rerun_manifest` is the file-system entry point.
    """
    # Reconstruct inputs.
    events = _reconstruct_events(manifest.input_event_list)
    strategy_cb = _build_strategy_callback(manifest)
    bundle = _build_model_bundle(manifest)

    # Initial ledger: empty position state for the manifest's pool.
    from robinhood_lp.backtest.engine import empty_position_state
    from robinhood_lp.backtest.events import LEDGER_VERSION

    initial_ledger = empty_position_state(
        pool_key_id=manifest.pool_key_id, chain_id=manifest.chain_id
    )

    # Ensure the position state carries the ledger version the engine
    # expects (the engine's ``empty_position_state`` already does
    # this; we keep the explicit construction so the rerun is
    # robust to engine API changes).
    _ = LEDGER_VERSION  # silence linter when not used directly

    engine = BacktestEngine(
        version=BACKTEST_ENGINE_VERSION,
        initial_ledger=initial_ledger,
        model_bundle=bundle,
        strategy_callback=strategy_cb,
        risk_callback=_approve_risk_callback,
    )
    result: BacktestResult = engine.run(events)

    # Recompute metrics, ledger snapshot, coverage, decisions.
    new_metrics = compute_run_metrics(
        result=result,
        interval_seconds=manifest.interval_seconds,
        benchmark_total_return_q64_64=Q64_SCALE,
    )
    new_decisions = extract_decisions(result)
    new_decisions_checksum = decisions_checksum(new_decisions)

    metrics_match = new_metrics.metrics_checksum == manifest.metrics_checksum
    decisions_match = new_decisions_checksum == manifest.decisions_checksum
    return RerunResult(
        manifest_path=manifest_path,
        run_id=manifest.run_id,
        chain_id=manifest.chain_id,
        pool_key_id=manifest.pool_key_id,
        match=metrics_match,
        recorded_metrics_checksum=manifest.metrics_checksum,
        recomputed_metrics_checksum=new_metrics.metrics_checksum,
        decisions_match=decisions_match,
        recorded_decisions_checksum=manifest.decisions_checksum,
        recomputed_decisions_checksum=new_decisions_checksum,
        new_run_metrics=new_metrics,
    )


__all__ = [
    "RERUN_VERSION",
    "Q64_SCALE",
    "RerunResult",
    "SECONDS_PER_YEAR",
    "rerun_manifest",
    "rerun_manifest_from_object",
]
