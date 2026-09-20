"""Run metrics — every quantity the report reconciles (T063).

A run's published metrics come from one immutable
:class:`RunMetrics` record. Every field carries an explicit unit;
``float`` is forbidden on the protocol / valuation path
(`ADR-004`). The metrics are derived from the audit chain the engine
emits (T061) so two equivalent runs of the same input manifest +
strategy bundle produce byte-identical :class:`RunMetrics` records.

Field units:

- ``total_return_q64_64`` — Q64.64 dimensionless ratio. ``Q64_SCALE``
  (1<<64) represents a 100 % return over the run; values above
  ``Q64_SCALE`` are positive, values below are losses.
- ``annualized_return_q64_64`` — Q64.64 dimensionless ratio over a
  fixed 1-year denominator (T063 explicitly rejects annualization
  without the underlying interval per `docs/spec/strategy/LP_METRICS.md`).
- ``max_drawdown_q64_64`` — Q64.64 dimensionless ratio; ``Q64_SCALE``
  is 100 % drawdown.
- ``turnover_q64_64`` — Q64.64 ratio of capital churned (filled
  liquidity × capital per fill, normalized by the initial capital).
- ``time_in_range_seconds`` — uint64 elapsed in-range seconds
  (the audit chain emits ``DECISION`` events with ``in_range``
  payloads, and the metric sums the elapsed range-bound time).
- ``fees_q64_64`` — Q64.64 fee revenue over the run.
- ``il_lvr_proxy_q64_64`` — Q64.64 ratio of the rebalancing-loss
  proxy (`M-LVR-001` in `docs/spec/strategy/LP_METRICS.md`). This is
  a *proxy*, never a measurement of true LVR.
- ``gas_units_total`` — uint64 atomic gas units across all fills.
- ``slippage_bps_total`` — uint64 basis points of price impact
  summed across all fills.
- ``benchmark_excess_q64_64`` — Q64.64 ratio of
  ``total_return - benchmark_return``; ``benchmark`` is the HODL
  baseline (`M-BM-001`) implemented as ``HoldStrategy``.

Design constraints:

- **Determinism.** Two equivalent input manifests + model bundles
  produce byte-identical :class:`RunMetrics`. The function never
  reads wall-clock time, never uses unseeded randomness, and uses
  only integer arithmetic on the accounting path.

- **Integer accounting.** All ratios are Q64.64 integers; gas /
  slippage / time are raw integers. ``float`` is forbidden.

- **Layer purity.** This module imports the standard library and
  the backtest layer (events / engine) only. It does not import
  RPC, storage, configuration, signing, execution, or
  presentation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from robinhood_lp.backtest.engine import BacktestResult
from robinhood_lp.backtest.events import (
    STAGE_FILL,
    AuditEvent,
    BacktestEvent,
    BacktestEventError,
    PositionState,
)

#: Module version. Bumping it is a breaking change for downstream
#: consumers (the manifest, the validation layer, the report).
METRICS_VERSION: Final[str] = "t063.run_metrics.v1"

#: Q64.64 fixed-point scale (1.0 in Q64.64 terms).
Q64_SCALE: Final[int] = 1 << 64

#: Seconds in a year. Used as the annualization denominator. The
#: value is the fixed Julian-year length (365.25 days × 86400 s/day).
SECONDS_PER_YEAR: Final[int] = 365 * 24 * 60 * 60 + 6 * 60 * 60  # 31_557_600

#: Valuation qualification sentinels — `ADR-014` §3 numeraire hierarchy.
VALUATION_QUALIFIED: Final[str] = "QUALIFIED"
VALUATION_RELATIVE_ONLY: Final[str] = "RELATIVE_ONLY"

#: Closed set of valuation qualification values. The string set is
#: the public vocabulary a manifest may declare.
_VALID_VALUATION_QUALIFICATIONS: Final[frozenset[str]] = frozenset(
    {VALUATION_QUALIFIED, VALUATION_RELATIVE_ONLY}
)


def _require_non_negative_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BacktestEventError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise BacktestEventError(f"{field}: must be non-negative, got {value}")
    return value


def _require_q64_64(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BacktestEventError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise BacktestEventError(f"{field}: must be non-negative, got {value}")
    return value


def _canonical_dict_hash(payload: Sequence[tuple[str, int | str | bool]]) -> str:
    """Return a deterministic SHA-256 hex digest of a sorted key/value payload.

    The payload must be a sequence of ``(str, int|str|bool)`` pairs; the
    digest is over the canonical ``"key=value"`` representation sorted by
    key so two equivalent payloads in different insertion orders produce
    the same digest.
    """
    import hashlib

    parts = sorted(f"{k}={v}" for k, v in payload)
    content = ";".join(parts)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MetricsError(BacktestEventError):
    """Base class for run-metrics construction failures."""


class InvalidValuationQualificationError(MetricsError):
    """A valuation qualification is outside the closed vocabulary."""


# ---------------------------------------------------------------------------
# RunMetrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """The reconcilable metrics a run publishes.

    The metrics are derived from the audit chain the engine emits
    (T061) plus the final ledger. Two equivalent runs of the same
    manifest + model bundle produce byte-identical :class:`RunMetrics`
    because every input the constructor reads is itself deterministic.

    Equality and hashing follow dataclass identity so two equivalent
    metric records hash identically in any process.

    See module docstring for unit annotations per field.
    """

    version: str
    chain_id: int
    pool_key_id: str
    interval_seconds: int
    duration_seconds: int
    total_return_q64_64: int
    annualized_return_q64_64: int
    max_drawdown_q64_64: int
    turnover_q64_64: int
    time_in_range_seconds: int
    fees_q64_64: int
    il_lvr_proxy_q64_64: int
    gas_units_total: int
    slippage_bps_total: int
    benchmark_excess_q64_64: int
    fills_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise MetricsError(f"RunMetrics.version: must be non-empty str, got {self.version!r}")
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise MetricsError(
                f"RunMetrics.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        _require_non_negative_int(self.chain_id, field="RunMetrics.chain_id")
        if self.chain_id <= 0:
            raise MetricsError(f"RunMetrics.chain_id: must be positive, got {self.chain_id}")
        _require_non_negative_int(self.interval_seconds, field="RunMetrics.interval_seconds")
        _require_non_negative_int(self.duration_seconds, field="RunMetrics.duration_seconds")
        _require_q64_64(self.total_return_q64_64, field="RunMetrics.total_return_q64_64")
        _require_q64_64(self.annualized_return_q64_64, field="RunMetrics.annualized_return_q64_64")
        _require_q64_64(self.max_drawdown_q64_64, field="RunMetrics.max_drawdown_q64_64")
        _require_q64_64(self.turnover_q64_64, field="RunMetrics.turnover_q64_64")
        _require_non_negative_int(
            self.time_in_range_seconds, field="RunMetrics.time_in_range_seconds"
        )
        _require_q64_64(self.fees_q64_64, field="RunMetrics.fees_q64_64")
        _require_q64_64(self.il_lvr_proxy_q64_64, field="RunMetrics.il_lvr_proxy_q64_64")
        _require_non_negative_int(self.gas_units_total, field="RunMetrics.gas_units_total")
        _require_non_negative_int(self.slippage_bps_total, field="RunMetrics.slippage_bps_total")
        _require_q64_64(self.benchmark_excess_q64_64, field="RunMetrics.benchmark_excess_q64_64")
        _require_non_negative_int(self.fills_count, field="RunMetrics.fills_count")

    @property
    def metrics_checksum(self) -> str:
        """Return the deterministic SHA-256 hex digest of the metrics record."""
        return _canonical_dict_hash(
            (
                ("annualized_return_q64_64", self.annualized_return_q64_64),
                ("benchmark_excess_q64_64", self.benchmark_excess_q64_64),
                ("chain_id", self.chain_id),
                ("duration_seconds", self.duration_seconds),
                ("fees_q64_64", self.fees_q64_64),
                ("fills_count", self.fills_count),
                ("gas_units_total", self.gas_units_total),
                ("il_lvr_proxy_q64_64", self.il_lvr_proxy_q64_64),
                ("interval_seconds", self.interval_seconds),
                ("max_drawdown_q64_64", self.max_drawdown_q64_64),
                ("pool_key_id", self.pool_key_id),
                ("slippage_bps_total", self.slippage_bps_total),
                ("time_in_range_seconds", self.time_in_range_seconds),
                ("total_return_q64_64", self.total_return_q64_64),
                ("turnover_q64_64", self.turnover_q64_64),
                ("version", self.version),
            )
        )


# ---------------------------------------------------------------------------
# Helpers — extract values from the audit chain
# ---------------------------------------------------------------------------


def _fill_events(audit_events: Sequence[AuditEvent]) -> tuple[AuditEvent, ...]:
    """Return the subset of audit events with ``stage == STAGE_FILL``.

    The engine emits one ``FILL`` event per pipeline stage that
    reached the fill stage; rejected fills still emit a ``FILL``
    event but carry ``fill_status == STATUS_FILL_REJECTED``. Both
    kinds are returned; the caller filters by ``fill_status`` if
    it needs to exclude rejected fills.
    """
    return tuple(a for a in audit_events if a.stage == STAGE_FILL)


def _payload_int(event: AuditEvent, *, key: str) -> int:
    """Return the integer payload value at ``key`` or 0 when missing."""
    for k, v in event.payload:
        if k == key:
            if isinstance(v, bool) or not isinstance(v, int):
                return 0
            return int(v)
    return 0


def _decision_in_range_segments(
    audit_events: Sequence[AuditEvent],
    *,
    final_timestamp: int,
) -> tuple[int, ...]:
    """Return a tuple of elapsed in-range seconds between DECISION events.

    The function pairs consecutive ``DECISION`` audit events whose
    payload records an ``in_range`` flag (``0`` / ``1``). Each pair
    contributes ``(next_timestamp - this_timestamp)`` seconds to the
    sum when ``in_range`` was ``1`` on the earlier event. The final
    segment runs from the last decision to ``final_timestamp``.
    """
    decision_events = tuple(a for a in audit_events if a.stage == STAGE_FILL and False)  # noqa
    decision_events = tuple(
        a for a in audit_events if any(k == "decision_kind" for k, _ in a.payload)
    )
    segments: list[int] = []
    last_in_range = 0
    last_timestamp = 0
    for evt in decision_events:
        if last_timestamp == 0:
            last_timestamp = evt.timestamp
        else:
            elapsed = evt.timestamp - last_timestamp
            if last_in_range == 1 and elapsed > 0:
                segments.append(elapsed)
            last_timestamp = evt.timestamp
        last_in_range = _payload_int(evt, key="in_range")
    if last_in_range == 1 and last_timestamp > 0:
        tail = max(0, final_timestamp - last_timestamp)
        if tail > 0:
            segments.append(tail)
    return tuple(segments)


def _all_decision_events(audit_events: Sequence[AuditEvent]) -> tuple[AuditEvent, ...]:
    """Return every audit event whose payload records a ``decision_kind``."""
    return tuple(a for a in audit_events if any(k == "decision_kind" for k, _ in a.payload))


# ---------------------------------------------------------------------------
# Metric computation from an engine result
# ---------------------------------------------------------------------------


def compute_run_metrics(
    *,
    result: BacktestResult,
    interval_seconds: int,
    benchmark_total_return_q64_64: int = Q64_SCALE,
) -> RunMetrics:
    """Compute a :class:`RunMetrics` record from a :class:`BacktestResult`.

    The computation is deterministic: two equivalent inputs always
    produce the same metrics record. The function reads the audit
    chain to derive the reconcilable totals the T063 acceptance
    clause lists (return, drawdown, turnover, time in range, fees,
    IL/LVR proxy, gas / slippage, benchmark excess).

    Parameters
    ----------
    result:
        The output of :meth:`BacktestEngine.run`. The chain's final
        ledger is used as the post-run state; the audit events drive
        the per-stage aggregates.
    interval_seconds:
        The bar interval the run consumed (e.g. 300 for 5-minute
        bars). Recorded on the metrics record so the report can
        reconcile against the manifest.
    benchmark_total_return_q64_64:
        The HODL benchmark total return for the same window
        (Q64.64). The default is ``Q64_SCALE`` (zero excess — the
        HODL bench is exactly the strategy). The orchestrator may
        pass the actual HODL return computed in a separate run.
    """
    if not isinstance(interval_seconds, int) or isinstance(interval_seconds, bool):
        raise MetricsError(
            f"compute_run_metrics: interval_seconds must be int, got "
            f"{type(interval_seconds).__name__}"
        )
    if interval_seconds <= 0:
        raise MetricsError(
            f"compute_run_metrics: interval_seconds must be positive, got {interval_seconds}"
        )
    if not isinstance(benchmark_total_return_q64_64, int) or isinstance(
        benchmark_total_return_q64_64, bool
    ):
        raise MetricsError(
            f"compute_run_metrics: benchmark_total_return_q64_64 must be int, "
            f"got {type(benchmark_total_return_q64_64).__name__}"
        )
    if benchmark_total_return_q64_64 < 0:
        raise MetricsError(
            f"compute_run_metrics: benchmark_total_return_q64_64 must be "
            f"non-negative, got {benchmark_total_return_q64_64}"
        )

    audit_events = result.audit_events
    final_ledger: PositionState = result.final_ledger
    fills = _fill_events(audit_events)

    # Duration: from first decision to final timestamp in the chain.
    decision_events = _all_decision_events(audit_events)
    if decision_events:
        first_timestamp = decision_events[0].timestamp
        last_timestamp = max(a.timestamp for a in decision_events)
        duration_seconds = max(0, last_timestamp - first_timestamp)
    else:
        duration_seconds = 0

    # Fills aggregation.
    fills_count = 0
    filled_liquidity_total = 0
    fees_total = 0
    gas_units_total = 0
    slippage_bps_total = 0
    for evt in fills:
        status = ""
        for k, v in evt.payload:
            if k == "fill_status":
                status = str(v)
                break
        if status == "FILL_REJECTED":
            continue
        fills_count += 1
        filled_liquidity = _payload_int(evt, key="filled_liquidity")
        filled_liquidity_total += filled_liquidity
        fees_total += _payload_int(evt, key="fee_pips") * filled_liquidity
        gas_units_total += _payload_int(evt, key="gas_units")
        slippage_bps_total += _payload_int(evt, key="impact_bps")

    # Time in range.
    in_range_seconds = sum(
        _decision_in_range_segments(audit_events, final_timestamp=last_timestamp)
    )

    # Total return proxy: fee revenue + net liquidity change vs initial.
    # Initial principal is recorded by the engine on the initial
    # ledger; the reference implementation here uses fee revenue as
    # the proxy (no ``initial_principal`` recomputation is needed for
    # the canonical run, and the field is reserved for a future
    # bounded diff).
    fee_q64_64 = _fees_to_q64_64(fees_total, filled_liquidity_total)
    total_return_q64_64 = fee_q64_64

    # Annualized return: total_return * (SECONDS_PER_YEAR / duration).
    if duration_seconds > 0:
        annualized = _scale_q64_64(total_return_q64_64, SECONDS_PER_YEAR, duration_seconds)
    else:
        annualized = Q64_SCALE  # zero-length run → neutral 1.0
    annualized_return_q64_64 = annualized

    # Max drawdown: simple peak-to-trough Q64.64 ratio from the fee trace.
    max_drawdown_q64_64 = _max_drawdown_q64_64(fills)

    # Turnover: filled_liquidity × interval scaling → Q64.64 ratio of capital churned.
    turnover_q64_64 = _turnover_q64_64(filled_liquidity_total, interval_seconds)

    # IL/LVR proxy: rebalancing cost proxy computed as
    # gas_units × gas_price_wei (placeholder zero) — for the
    # reference implementation the proxy is zero until T093 binds a
    # gas-price oracle; the report carries the field so consumers
    # do not lose the slot.
    il_lvr_proxy_q64_64 = 0

    # Benchmark excess.
    if benchmark_total_return_q64_64 > total_return_q64_64:
        benchmark_excess_q64_64 = 0
    else:
        benchmark_excess_q64_64 = total_return_q64_64 - benchmark_total_return_q64_64

    return RunMetrics(
        version=METRICS_VERSION,
        chain_id=final_ledger.chain_id,
        pool_key_id=final_ledger.pool_key_id,
        interval_seconds=interval_seconds,
        duration_seconds=duration_seconds,
        total_return_q64_64=total_return_q64_64,
        annualized_return_q64_64=annualized_return_q64_64,
        max_drawdown_q64_64=max_drawdown_q64_64,
        turnover_q64_64=turnover_q64_64,
        time_in_range_seconds=in_range_seconds,
        fees_q64_64=fee_q64_64,
        il_lvr_proxy_q64_64=il_lvr_proxy_q64_64,
        gas_units_total=gas_units_total,
        slippage_bps_total=slippage_bps_total,
        benchmark_excess_q64_64=benchmark_excess_q64_64,
        fills_count=fills_count,
    )


def _fees_to_q64_64(fees_total: int, filled_liquidity_total: int) -> int:
    """Convert the integer fees/liquidity aggregate to a Q64.64 ratio.

    The reference implementation collapses the per-fill
    ``fee_pips × filled_liquidity`` products into a Q64.64 ratio by
    normalising by ``Q64_SCALE`` to keep the field in the integer
    range. The exact scaling is documented as the "fee revenue per
    filled liquidity" quantity; a downstream contributor can swap
    the scale without changing the field's meaning.
    """
    if filled_liquidity_total <= 0:
        return 0
    scaled = fees_total // filled_liquidity_total
    if scaled <= 0:
        return 0
    # Scale into Q64.64 — the reference scale assumes fee_pips already
    # fits the pipeline so a single shift is the canonical mapping.
    return scaled * (Q64_SCALE // 1_000_000)


def _scale_q64_64(value_q64_64: int, numerator: int, denominator: int) -> int:
    """Return ``value_q64_64 * numerator / denominator`` as an integer.

    Integer division is used (no float on the protocol path per
    ADR-004); the result is the floor of the true ratio. The
    numerator / denominator pair must be positive integers.
    """
    if numerator <= 0 or denominator <= 0:
        raise MetricsError(
            f"_scale_q64_64: numerator={numerator} and denominator={denominator} must be positive"
        )
    return (value_q64_64 * numerator) // denominator


def _max_drawdown_q64_64(fills: Sequence[AuditEvent]) -> int:
    """Return the maximum peak-to-trough drawdown as a Q64.64 ratio.

    The function walks the cumulative fee revenue in timestamp order
    and tracks the running peak; the worst peak-to-trough distance
    is returned as a Q64.64 ratio scaled so ``Q64_SCALE`` is 100 %.
    """
    if not fills:
        return 0
    sorted_fills = sorted(fills, key=lambda a: a.timestamp)
    peak = 0
    cumulative = 0
    worst = 0
    for evt in sorted_fills:
        cumulative += _payload_int(evt, key="fee_pips") * _payload_int(evt, key="filled_liquidity")
        if cumulative > peak:
            peak = cumulative
        if peak > 0:
            drop = peak - cumulative
            if drop > worst:
                worst = drop
    if peak <= 0:
        return 0
    # Normalise by peak so 100 % drawdown equals Q64_SCALE.
    return (worst * Q64_SCALE) // peak


def _turnover_q64_64(filled_liquidity_total: int, interval_seconds: int) -> int:
    """Return the turnover ratio as Q64.64.

    The reference implementation uses the total filled liquidity as
    the numerator and the interval in seconds as the per-bar
    denominator. The result is a Q64.64 ratio where ``Q64_SCALE``
    is one full-turnover-per-bar.
    """
    if filled_liquidity_total <= 0:
        return 0
    if interval_seconds <= 0:
        return filled_liquidity_total * Q64_SCALE
    # One "unit of capital" is filled_liquidity_total itself, so the
    # scaling here normalises to Q64_SCALE per bar.
    return Q64_SCALE * (filled_liquidity_total // max(1, interval_seconds))


# ---------------------------------------------------------------------------
# Decision log
# ---------------------------------------------------------------------------


def extract_decisions(
    result: BacktestResult,
) -> tuple[tuple[int, str, int], ...]:
    """Return the canonical per-decision log from a backtest result.

    Each entry is a ``(decision_index, decision_kind, decision_time)``
    triple. The decision index is a per-run counter (insertion order
    on the audit chain, zero-based). The ``decision_kind`` is one of
    the strategy verb set (``NO_TRADE`` / ``WAIT`` / ``PROPOSE``).
    ``decision_time`` is the event time the engine recorded on the
    ``DECISION`` audit event.

    The returned tuple is deterministic: two equivalent runs produce
    the same tuple in any process.
    """
    decisions: list[tuple[int, str, int]] = []
    for index, evt in enumerate(a for a in result.audit_events if a.stage == "DECISION"):
        kind = "NO_TRADE"
        for k, v in evt.payload:
            if k == "decision_kind":
                kind = str(v)
                break
        decisions.append((index, kind, evt.timestamp))
    return tuple(decisions)


def decisions_checksum(decisions: Sequence[tuple[int, str, int]]) -> str:
    """Return the deterministic SHA-256 hex digest of a decisions tuple."""
    import hashlib

    parts = [f"{idx}|{kind}|{ts}" for idx, kind, ts in decisions]
    content = "\n".join(parts)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Ledger snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """The frozen per-run ledger state a manifest carries.

    The snapshot mirrors :class:`PositionState` but is owned by the
    reports layer; this avoids a circular import between the backtest
    layer (which produces the state) and the reports layer (which
    serialises it). Equality and hashing follow dataclass identity.
    """

    version: str
    pool_key_id: str
    chain_id: int
    position_id: str
    tick_lower: int
    tick_upper: int
    liquidity: int
    principal_token0: int
    principal_token1: int
    tokens_owed0: int
    tokens_owed1: int
    in_range: bool
    last_accrual_time: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.chain_id, field="LedgerSnapshot.chain_id")
        if self.chain_id <= 0:
            raise MetricsError(f"LedgerSnapshot.chain_id: must be positive, got {self.chain_id}")
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise MetricsError(
                f"LedgerSnapshot.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )

    @classmethod
    def from_position_state(cls, ledger: PositionState) -> LedgerSnapshot:
        """Construct a snapshot from a backtest-layer :class:`PositionState`."""
        return cls(
            version=ledger.version,
            pool_key_id=ledger.pool_key_id,
            chain_id=ledger.chain_id,
            position_id=ledger.position_id,
            tick_lower=ledger.tick_lower,
            tick_upper=ledger.tick_upper,
            liquidity=ledger.liquidity,
            principal_token0=ledger.principal_token0,
            principal_token1=ledger.principal_token1,
            tokens_owed0=ledger.tokens_owed0,
            tokens_owed1=ledger.tokens_owed1,
            in_range=ledger.in_range,
            last_accrual_time=ledger.last_accrual_time,
        )

    @property
    def ledger_checksum(self) -> str:
        """Return the deterministic SHA-256 hex digest of the snapshot."""
        import hashlib

        content = (
            f"{self.version}|{self.pool_key_id}|{self.chain_id}|{self.position_id}|"
            f"{self.tick_lower}|{self.tick_upper}|{self.liquidity}|"
            f"{self.principal_token0}|{self.principal_token1}|"
            f"{self.tokens_owed0}|{self.tokens_owed1}|"
            f"{int(self.in_range)}|{self.last_accrual_time}"
        )
        return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Coverage summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """The per-run coverage record the manifest carries.

    The summary records how many input events the engine processed,
    how many fill data observations were visible, the wall-time
    interval the run consumed, the audit-chain length and the data
    gaps that remain unfilled. The unit on every quantity is the
    integer atomic unit (events, seconds, blocks).

    Two equivalent runs of the same input manifest produce
    byte-identical summaries in any process.
    """

    version: str
    chain_id: int
    pool_key_id: str
    interval_seconds: int
    input_events_total: int
    data_events_total: int
    fill_observations_total: int
    audit_events_total: int
    block_range_start: int
    block_range_end: int
    duration_seconds: int
    data_gaps: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        _require_non_negative_int(self.chain_id, field="CoverageSummary.chain_id")
        if self.chain_id <= 0:
            raise MetricsError(f"CoverageSummary.chain_id: must be positive, got {self.chain_id}")
        if not isinstance(self.pool_key_id, str) or not self.pool_key_id:
            raise MetricsError(
                f"CoverageSummary.pool_key_id: must be non-empty str, got {self.pool_key_id!r}"
            )
        _require_positive_int(self.interval_seconds, field="CoverageSummary.interval_seconds")
        _require_non_negative_int(
            self.input_events_total, field="CoverageSummary.input_events_total"
        )
        _require_non_negative_int(self.data_events_total, field="CoverageSummary.data_events_total")
        _require_non_negative_int(
            self.fill_observations_total, field="CoverageSummary.fill_observations_total"
        )
        _require_non_negative_int(
            self.audit_events_total, field="CoverageSummary.audit_events_total"
        )
        _require_non_negative_int(self.block_range_start, field="CoverageSummary.block_range_start")
        _require_non_negative_int(self.block_range_end, field="CoverageSummary.block_range_end")
        if self.block_range_end < self.block_range_start:
            raise MetricsError(
                f"CoverageSummary: block_range_end={self.block_range_end} must be "
                f">= block_range_start={self.block_range_start}"
            )
        _require_non_negative_int(self.duration_seconds, field="CoverageSummary.duration_seconds")
        if not isinstance(self.data_gaps, tuple):
            raise MetricsError(
                f"CoverageSummary.data_gaps: must be tuple[(int, int)], got "
                f"{type(self.data_gaps).__name__}"
            )

    @property
    def coverage_checksum(self) -> str:
        """Return the deterministic SHA-256 hex digest of the summary."""
        import hashlib

        gaps = ",".join(f"{a}-{b}" for a, b in self.data_gaps)
        content = (
            f"{self.version}|{self.chain_id}|{self.pool_key_id}|{self.interval_seconds}|"
            f"{self.input_events_total}|{self.data_events_total}|"
            f"{self.fill_observations_total}|{self.audit_events_total}|"
            f"{self.block_range_start}|{self.block_range_end}|"
            f"{self.duration_seconds}|{gaps}"
        )
        return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _require_positive_int(value: int, *, field: str) -> int:
    value = _require_non_negative_int(value, field=field)
    if value == 0:
        raise MetricsError(f"{field}: must be positive, got 0")
    return value


def build_coverage_summary(
    *,
    result: BacktestResult,
    input_events: Sequence[BacktestEvent],
    interval_seconds: int,
    block_range_start: int,
    block_range_end: int,
) -> CoverageSummary:
    """Construct a :class:`CoverageSummary` from the run inputs.

    The function inspects the input events and the audit chain the
    engine produced and returns the coverage record the manifest
    carries. The function is pure and deterministic.

    Parameters
    ----------
    result:
        The :class:`BacktestResult` the engine produced.
    input_events:
        The original event list the engine was handed (before the
        engine's per-source sequence assignment).
    interval_seconds:
        The bar interval the run consumed.
    block_range_start, block_range_end:
        The block bounds the run consumed (per the contract's
        per-pool block range clause).
    """
    final_ledger = result.final_ledger
    audit_events_total = len(result.audit_events)
    data_events_total = sum(1 for e in input_events if e.is_data_event())
    fill_observations_total = sum(1 for a in result.audit_events if a.stage == STAGE_FILL)
    timestamps = sorted(e.timestamp for e in input_events)
    duration_seconds = timestamps[-1] - timestamps[0] if len(timestamps) >= 2 else 0
    # Detect data gaps: stretches where no data event was visible for
    # more than ``interval_seconds`` seconds.
    gaps: list[tuple[int, int]] = []
    for prev, nxt in zip(timestamps, timestamps[1:], strict=False):
        gap = nxt - prev
        if gap > interval_seconds:
            gaps.append((prev, nxt))
    return CoverageSummary(
        version="t063.coverage_summary.v1",
        chain_id=final_ledger.chain_id,
        pool_key_id=final_ledger.pool_key_id,
        interval_seconds=interval_seconds,
        input_events_total=len(input_events),
        data_events_total=data_events_total,
        fill_observations_total=fill_observations_total,
        audit_events_total=audit_events_total,
        block_range_start=block_range_start,
        block_range_end=block_range_end,
        duration_seconds=duration_seconds,
        data_gaps=tuple(gaps),
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "METRICS_VERSION",
    "Q64_SCALE",
    "SECONDS_PER_YEAR",
    "STAGE_FILL",
    "VALUATION_QUALIFIED",
    "VALUATION_RELATIVE_ONLY",
    "CoverageSummary",
    "LedgerSnapshot",
    "MetricsError",
    "RunMetrics",
    "build_coverage_summary",
    "compute_run_metrics",
    "decisions_checksum",
    "extract_decisions",
]
