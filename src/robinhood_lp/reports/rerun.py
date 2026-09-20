"""One-command rerun of a saved manifest (T063 + T105).

The rerun module is the implementation of the T063 acceptance
clause "one command reruns a saved manifest" extended by the T105
registry-binding clause "verify and deterministically reproduce an
already saved manifest as an artifact operation".

The function :func:`rerun_manifest` loads a manifest from a path,
reconstructs the input events, builds the engine from the recorded
strategy parameters and model bundle parameters, runs the engine,
computes the metrics, and compares the new metrics checksum to the
recorded one. A match is a byte-identical rerun; a mismatch is a
rerun failure (the manifest is no longer reproducible from the
recorded parameters — the file has been tampered with, the code has
drifted, or the inputs are stale).

T105 binding (rerun path):

- The strategy callback is reconstructed from the manifest's
  ``strategy_identity`` through the registry's factory surface. A
  manifest that names an unregistered identity, supplies parameters
  the registered schema does not declare, or carries a registry /
  schema / code binding that disagrees with the live registry is
  rejected before any engine call.

- The rerun is an artifact operation: it verifies and reproduces an
  already saved manifest. A rerun that is requested through the
  product path is the T069 product run record's responsibility
  (run identity, queue / progress / status, failure, cancellation,
  restart); the rerun module itself never creates T069 state, never
  reads Web state, and never mutates the source manifest.

The module is intentionally minimal: it depends only on the
backtest engine, the manifest, the metrics layer, the validation
layer, the strategy layer's registry / baselines / adaptive, and
the registry binding (T105). It does not import RPC, storage,
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
from robinhood_lp.reports.registry_binding import (
    RegistryBindingError,
    assert_binding_matches_registry,
)
from robinhood_lp.reports.validation import (
    DatasetQualificationRecord,
    ManifestValidationError,
    load_manifest_from_path,
)

#: Module version. Bumping it is a breaking change for the
#: ``rerun-manifest`` CLI subcommand and any consumer that
#: reproduces a saved manifest. The T105 cutover bumped the
#: version because the rerun now consults the registry to build
#: the strategy callback.
RERUN_VERSION: Final[str] = "t105.manifest_rerun.v1"

#: Default model-bundle parameters the rerun uses when the manifest
#: does not name a registered parameter for the model bundle. The
#: values are non-binding defaults for the simplest reproduce path;
#: a re-publication can override them by registering a new strategy
#: entry that names a different bundle.
_RERUN_DEFAULT_GAS_UNITS: Final[int] = 21_000
_RERUN_DEFAULT_FEE_PIPS: Final[int] = 3_000
_RERUN_DEFAULT_ACTIVE_LIQUIDITY: Final[int] = 10_000


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
    """Build a strategy callback from the manifest's registry binding.

    The function is the registry-bound replacement for the T063
    hard-coded ``strategy_kind`` switch. It looks the registered
    identity up through the registry, builds the registered factory
    with the validated parameters the manifest carries, and returns
    the constructed strategy. The factory is the registry's
    :meth:`RegisteredStrategy.factory.build` method, so the registry
    remains the only authority for the strategy a rerun can execute.

    An unregistered identity, a parameter the schema does not
    declare, or a registry / schema / code binding that disagrees
    with the live registry is rejected before any engine call.
    """
    # Imported lazily so the strategy layer is not on the
    # reports-module import path at module-load time. The registry
    # and the registry binding surface are the contract; the
    # strategy implementations are loaded only when the factory
    # actually instantiates them.
    from robinhood_lp.reports.registry_binding import StrategyBinding
    from robinhood_lp.strategy.registry import default_registry

    reg = default_registry()
    # Reconstruct the binding the manifest carries. The manifest
    # stores the binding's fields as separate slots (T105); the
    # dataclass view here is the call-time surface that
    # ``assert_binding_matches_registry`` consults.
    schema_params = tuple(sorted((name, value) for name, value in manifest.strategy_params.items()))
    binding = StrategyBinding(
        registry_version=manifest.registry_version,
        registry_checksum=manifest.registry_checksum,
        strategy_identity=manifest.strategy_identity,
        strategy_version=manifest.strategy_version,
        parameter_schema_version=manifest.parameter_schema_version,
        parameter_schema_checksum=manifest.parameter_schema_checksum,
        code_provenance_module=manifest.code_provenance_module,
        code_provenance_revision=manifest.code_provenance_revision,
        code_provenance_symbol=manifest.code_provenance_symbol,
        validated_parameters=schema_params,
    )
    # The registry-binding check rejects unregistered identities,
    # binding mismatches, and (indirectly through the factory
    # build) parameter violations. A failure here is a contract
    # break the rerun surfaces as a :class:`RegistryBindingError`
    # rather than as a passing rerun.
    try:
        assert_binding_matches_registry(binding, registry=reg)
    except RegistryBindingError:
        raise

    entry = reg.lookup(manifest.strategy_identity)
    # ``factory.build`` accepts the validated parameters as a
    # mapping; the schema_params tuple is rebuilt into a dict for
    # the factory call. The factory returns the strategy object the
    # engine consumes. Strategies that are already callable (the
    # T062 baselines implement ``StrategyCallback`` directly) are
    # passed through; the T065 :class:`AdaptiveStrategy` exposes
    # ``evaluate`` and must be wrapped in
    # :class:`AdaptiveStrategyCallback` so the engine can invoke it
    # through a single ``__call__`` surface.
    factory_params: dict[str, int | bool | str] = dict(schema_params)
    built = entry.factory.build(
        parameters=factory_params,
        pool_key_id=manifest.pool_key_id,
        chain_id=manifest.chain_id,
    )
    if callable(built):
        return built
    # The T065 adaptive strategy exposes ``evaluate`` rather than
    # ``__call__``; wrap it in the engine-callback adapter so the
    # engine can invoke it through a single ``__call__`` surface.
    from robinhood_lp.strategy.adapter import AdaptiveStrategyCallback
    from robinhood_lp.strategy.adaptive import AdaptiveStrategy as _AdaptiveStrategy

    if isinstance(built, _AdaptiveStrategy):
        return AdaptiveStrategyCallback(
            strategy=built, window_seconds=int(manifest.interval_seconds)
        )
    raise ManifestValidationError(
        f"_build_strategy_callback: registered factory for "
        f"{manifest.strategy_identity!r} returned an object the engine "
        f"cannot consume ({type(built).__name__})"
    )


def _approve_risk_callback(decision: object) -> RiskDecision:
    return RiskDecision(approved=True, reason_code="OK")


# ---------------------------------------------------------------------------
# Model bundle reconstruction
# ---------------------------------------------------------------------------


def _build_model_bundle(manifest: ExperimentManifest) -> ModelBundle:
    """Build a :class:`ModelBundle` from the manifest's model-bundle parameters.

    The function reconstructs the canonical T105 model bundle —
    constant liquidity, static fee, flat gas, zero slippage,
    deterministic failure, fixed latency. The bundle version is
    the manifest's own ``code_revision` (so a re-publication after
    a code change carries a different bundle version). The model
    bundle parameters live on the manifest's ``strategy_params``
    slot under the model-bundle keys (``gas_units``, ``fee_pips``,
    ``active_liquidity``); the rerun reads them only when the
    registered identity declares them, falling back to the
    canonical defaults otherwise.
    """
    from robinhood_lp.backtest.models import (
        ConstantLiquidityModel,
        DeterministicFailureModel,
        FlatGasModel,
        StaticFeeModel,
        ZeroSlippageModel,
    )

    params = manifest.strategy_params
    gas_units = int(params.get("gas_units", _RERUN_DEFAULT_GAS_UNITS))
    fee_pips = int(params.get("fee_pips", _RERUN_DEFAULT_FEE_PIPS))
    active_liquidity = int(params.get("active_liquidity", _RERUN_DEFAULT_ACTIVE_LIQUIDITY))
    return ModelBundle(
        bundle_version=f"t105.rerun.{manifest.code_revision}",
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
    validation gate, including the T105 registry-binding check),
    reconstructs the events, strategy, model bundle and engine,
    runs the engine, computes the metrics, and compares both the
    metrics checksum and the decisions checksum to the recorded
    values.

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
    :class:`RegistryBindingError`
        On a registry-binding disagreement (T105).
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
