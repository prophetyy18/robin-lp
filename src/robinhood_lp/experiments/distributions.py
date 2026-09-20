"""Distribution summaries (T066).

T066 requires the experiment harness to publish PnL, profit
probability, Expected Shortfall, boundary, time-in-Range, fee and
cost distributions for the candidate it locked. This module owns
the distribution-summary value type and the deterministic
computations that produce it from a sequence of integer metric
samples.

The summaries are *integer* — the bound that
``docs/spec/architecture/ARCHITECTURE.md` ADR-004 places on
protocol / accounting paths. Histograms are Q64.64-bucketed
integer counts; descriptive statistics are integer-valued (count,
sum, min, max, median, percentile, expected shortfall); ``float``
never appears on this path.

Design constraints (binding):

- **Integer-only.** Every quantity is a Python ``int``. Percentiles
  use the *nearest-rank* method with integer interpolation; the
  median is the middle integer (or the lower of the two middles
  for an even-length sample).
- **Deterministic.** Two equivalent samples in any process produce
  byte-identical :class:`DistributionSummary` records.
- **No wall-clock reads.** This module does not import
  ``time.time``.
- **Layer purity.** This module imports the standard library only.
  It does not import the backtest engine, the manifest layer, RPC,
  storage, signing, execution, or presentation code.

References:

- T066 — Build versioned threshold experiments and provisional
  candidate review.
- `docs/spec/strategy/LP_METRICS.md` — the LP metrics vocabulary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: Module version. Bumping it is a breaking change for downstream
#: consumers (the harness, the audit chain).
DISTRIBUTIONS_VERSION: Final[str] = "t066.distributions.v1"

#: Closed vocabulary for the metric kind a distribution summarises.
#: The harness builds one distribution per metric kind; the
#: vocabulary mirrors the metric names the reports layer
#: (`robinhood_lp.reports.metrics`) records.
VALID_METRIC_KINDS: Final[frozenset[str]] = frozenset(
    {
        "PNL_USDG",
        "PROFIT_PROBABILITY",
        "EXPECTED_SHORTFALL_USDG",
        "BOUNDARY_TOUCH_PROBABILITY",
        "TIME_IN_RANGE_FRACTION",
        "FEE_USDG",
        "COST_USDG",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DistributionError(ValueError):
    """Base class for distribution-summary construction failures."""


class InvalidDistributionError(DistributionError):
    """A distribution-summary field violates its invariant."""


class EmptyDistributionError(DistributionError):
    """A distribution summary cannot be computed from an empty sample."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_str(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidDistributionError(f"{field}: must be str, got {type(value).__name__}")
    return value


def _require_non_empty_str(value: object, *, field: str) -> str:
    s = _require_str(value, field=field)
    if not s:
        raise InvalidDistributionError(f"{field}: must be non-empty str")
    return s


def _require_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDistributionError(f"{field}: must be int, got {type(value).__name__}")
    return int(value)


def _require_non_negative_int(value: object, *, field: str) -> int:
    n = _require_int(value, field=field)
    if n < 0:
        raise InvalidDistributionError(f"{field}: must be non-negative, got {n}")
    return n


def _sorted(values: Sequence[int]) -> tuple[int, ...]:
    return tuple(sorted(values))


def _percentile(sorted_values: Sequence[int], percentile_q64_64: int) -> int:
    """Return the integer percentile via the nearest-rank method.

    ``percentile_q64_64`` is a Q64.64 fraction in ``[0, Q64_SCALE]``.
    The function returns the value at index
    ``ceil(percentile * n) - 1`` (the nearest-rank convention);
    ``0`` returns the minimum, ``Q64_SCALE`` returns the maximum.
    Out-of-range percentiles raise :class:`InvalidDistributionError`.
    """
    if not sorted_values:
        raise EmptyDistributionError(
            "_percentile: cannot compute a percentile from an empty sample"
        )
    if not isinstance(percentile_q64_64, int) or isinstance(percentile_q64_64, bool):
        raise InvalidDistributionError(
            f"_percentile: percentile_q64_64 must be int, got {type(percentile_q64_64).__name__}"
        )
    if percentile_q64_64 < 0 or percentile_q64_64 > Q64_SCALE:
        raise InvalidDistributionError(
            f"_percentile: percentile_q64_64={percentile_q64_64} must be in [0, {Q64_SCALE}]"
        )
    n = len(sorted_values)
    # Nearest-rank: index = ceil(p * n) - 1, clamped to [0, n - 1].
    # Use integer ceiling: (p * n + Q64_SCALE - 1) // Q64_SCALE, then
    # convert to a 0-based index.
    raw = (percentile_q64_64 * n + Q64_SCALE - 1) // Q64_SCALE
    if raw <= 0:
        return int(sorted_values[0])
    if raw > n:
        return int(sorted_values[-1])
    return int(sorted_values[raw - 1])


#: Q64.64 fixed-point scale (1.0 in Q64.64 terms).
Q64_SCALE: Final[int] = 1 << 64


# ---------------------------------------------------------------------------
# Distribution summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DistributionSummary:
    """The integer-valued summary a metric distribution produces.

    Every field is a Python ``int``. The histogram is the
    canonical evidence the harness surfaces; the descriptive
    statistics are integer-valued projections of it.

    Field units:

    - ``metric_kind`` — one of :data:`VALID_METRIC_KINDS`.
    - ``sample_count`` — strictly positive integer.
    - ``sum_q64_64`` — Q64.64 sum (the integer sum multiplied by
      ``Q64_SCALE`` so the field scales consistently with the
      other Q64.64 statistics).
    - ``min_q64_64`` / ``max_q64_64`` — Q64.64 minimum / maximum.
    - ``median_q64_64`` — Q64.64 median.
    - ``percentile_5_q64_64`` / ``percentile_95_q64_64`` —
      Q64.64 lower-tail / upper-tail percentiles.
    - ``expected_shortfall_q64_64`` — Q64.64 expected shortfall
      (the mean of the bottom 5 % of the sample).
    - ``histogram_buckets`` — sorted tuple of
      ``(bucket_upper_q64_64, count)``; ``bucket_upper`` is
      ``Q64_SCALE`` for the final bucket.
    - ``primary_benchmark`` — the literal ``"USDG_CASH"``; the
      USDG cash primary benchmark the T066 acceptance clause
      pins.
    - ``experimental_flag`` — the literal
      ``"EXPERIMENTAL_NOT_LIVE_APPROVED"``; carried on every
      output so a downstream consumer cannot mistake a
      provisional recommendation for a live default.
    - ``content_hash`` — the SHA-256 hex digest of the
      serialisation of every other field.
    """

    metric_kind: str
    sample_count: int
    sum_q64_64: int
    min_q64_64: int
    max_q64_64: int
    median_q64_64: int
    percentile_5_q64_64: int
    percentile_95_q64_64: int
    expected_shortfall_q64_64: int
    histogram_buckets: tuple[tuple[int, int], ...]
    primary_benchmark: str
    experimental_flag: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.metric_kind not in VALID_METRIC_KINDS:
            raise InvalidDistributionError(
                f"DistributionSummary.metric_kind: must be one of "
                f"{sorted(VALID_METRIC_KINDS)}, got {self.metric_kind!r}"
            )
        _require_positive_int(self.sample_count, field="DistributionSummary.sample_count")
        for field_name in (
            "sum_q64_64",
            "min_q64_64",
            "max_q64_64",
            "median_q64_64",
            "percentile_5_q64_64",
            "percentile_95_q64_64",
            "expected_shortfall_q64_64",
        ):
            _require_int(getattr(self, field_name), field=f"DistributionSummary.{field_name}")
        if not isinstance(self.histogram_buckets, tuple):
            raise InvalidDistributionError(
                f"DistributionSummary.histogram_buckets: must be tuple, got "
                f"{type(self.histogram_buckets).__name__}"
            )
        total = 0
        for i, entry in enumerate(self.histogram_buckets):
            if not (isinstance(entry, tuple) and len(entry) == 2):
                raise InvalidDistributionError(
                    f"DistributionSummary.histogram_buckets[{i}]: must be (int, int)"
                )
            upper, count = entry
            if not isinstance(upper, int) or isinstance(upper, bool):
                raise InvalidDistributionError(
                    f"DistributionSummary.histogram_buckets[{i}][0]: must be int, "
                    f"got {type(upper).__name__}"
                )
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise InvalidDistributionError(
                    f"DistributionSummary.histogram_buckets[{i}][1]: must be "
                    f"non-negative int, got {count!r}"
                )
            total += count
        if total != self.sample_count:
            raise InvalidDistributionError(
                f"DistributionSummary.histogram_buckets: bucket counts total "
                f"{total} but sample_count={self.sample_count}"
            )
        if self.primary_benchmark != "USDG_CASH":
            raise InvalidDistributionError(
                f"DistributionSummary.primary_benchmark: must be 'USDG_CASH' "
                f"(T066 acceptance), got {self.primary_benchmark!r}"
            )
        if self.experimental_flag != "EXPERIMENTAL_NOT_LIVE_APPROVED":
            raise InvalidDistributionError(
                f"DistributionSummary.experimental_flag: must be "
                f"'EXPERIMENTAL_NOT_LIVE_APPROVED' (T066 acceptance), got "
                f"{self.experimental_flag!r}"
            )
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise InvalidDistributionError(
                "DistributionSummary.content_hash: must be non-empty str"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "metric_kind": self.metric_kind,
            "sample_count": self.sample_count,
            "sum_q64_64": self.sum_q64_64,
            "min_q64_64": self.min_q64_64,
            "max_q64_64": self.max_q64_64,
            "median_q64_64": self.median_q64_64,
            "percentile_5_q64_64": self.percentile_5_q64_64,
            "percentile_95_q64_64": self.percentile_95_q64_64,
            "expected_shortfall_q64_64": self.expected_shortfall_q64_64,
            "histogram_buckets": [list(entry) for entry in self.histogram_buckets],
            "primary_benchmark": self.primary_benchmark,
            "experimental_flag": self.experimental_flag,
            "content_hash": self.content_hash,
        }


def _require_positive_int(value: object, *, field: str) -> int:
    n = _require_int(value, field=field)
    if n <= 0:
        raise InvalidDistributionError(f"{field}: must be positive, got {n}")
    return n


def _fixed_histogram_buckets(
    sorted_values: Sequence[int], *, bucket_count: int
) -> tuple[tuple[int, int], ...]:
    """Bucket ``sorted_values`` into ``bucket_count`` integer buckets.

    The buckets are equal-count as much as possible: the function
    uses the integer-divided bucket size ``ceil(n / bucket_count)``
    so the tail bucket absorbs the remainder. Each bucket's
    ``upper`` is the maximum value inside the bucket; the final
    bucket's ``upper`` is the maximum value of the sample.

    The function accepts a non-positive ``bucket_count`` and
    returns a single bucket spanning the whole sample.
    """
    n = len(sorted_values)
    if bucket_count <= 0:
        bucket_count = 1
    size = (n + bucket_count - 1) // bucket_count
    buckets: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + size, n)
        bucket = sorted_values[start:end]
        buckets.append((int(bucket[-1]), int(len(bucket))))
        start = end
    return tuple(buckets)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_distribution_summary(
    *,
    metric_kind: str,
    samples: Sequence[int],
    bucket_count: int = 10,
    expected_shortfall_fraction_q64_64: int = (Q64_SCALE * 5) // 100,
) -> DistributionSummary:
    """Build a :class:`DistributionSummary` from a sequence of samples.

    ``samples`` is the per-decision / per-episode Q64.64 metric
    value the candidate produced on the test fold. The function
    computes the integer-valued statistics, the equal-count
    histogram, and the binding content hash, and embeds the
    primary-benchmark + experimental-flag fields the T066
    acceptance clause pins on every output.
    """
    if metric_kind not in VALID_METRIC_KINDS:
        raise InvalidDistributionError(
            f"build_distribution_summary: metric_kind must be one of "
            f"{sorted(VALID_METRIC_KINDS)}, got {metric_kind!r}"
        )
    if not samples:
        raise EmptyDistributionError("build_distribution_summary: samples must be non-empty")
    for i, sample in enumerate(samples):
        if isinstance(sample, bool) or not isinstance(sample, int):
            raise InvalidDistributionError(
                f"build_distribution_summary: samples[{i}] must be int, got {type(sample).__name__}"
            )
    sorted_samples = _sorted(samples)
    sample_count = len(sorted_samples)
    sum_q64_64 = int(sum(sorted_samples))
    min_q64_64 = int(sorted_samples[0])
    max_q64_64 = int(sorted_samples[-1])
    median_q64_64 = _percentile(sorted_samples, Q64_SCALE // 2)
    percentile_5_q64_64 = _percentile(sorted_samples, (Q64_SCALE * 5) // 100)
    percentile_95_q64_64 = _percentile(sorted_samples, (Q64_SCALE * 95) // 100)
    # Expected shortfall: the mean of the bottom
    # ``expected_shortfall_fraction`` of the sample. The integer
    # mean is ``floor(sum / count)``.
    cutoff = _percentile(sorted_samples, expected_shortfall_fraction_q64_64)
    tail = [v for v in sorted_samples if v <= cutoff]
    expected_shortfall_q64_64 = int(sum(tail) // len(tail)) if tail else int(sorted_samples[0])
    histogram_buckets = _fixed_histogram_buckets(sorted_samples, bucket_count=bucket_count)
    placeholder = DistributionSummary(
        metric_kind=metric_kind,
        sample_count=sample_count,
        sum_q64_64=sum_q64_64,
        min_q64_64=min_q64_64,
        max_q64_64=max_q64_64,
        median_q64_64=median_q64_64,
        percentile_5_q64_64=percentile_5_q64_64,
        percentile_95_q64_64=percentile_95_q64_64,
        expected_shortfall_q64_64=expected_shortfall_q64_64,
        histogram_buckets=histogram_buckets,
        primary_benchmark="USDG_CASH",
        experimental_flag="EXPERIMENTAL_NOT_LIVE_APPROVED",
        content_hash="0x" + "00" * 32,
    )
    content_hash = _compute_distribution_content_hash(placeholder)
    return DistributionSummary(
        metric_kind=placeholder.metric_kind,
        sample_count=placeholder.sample_count,
        sum_q64_64=placeholder.sum_q64_64,
        min_q64_64=placeholder.min_q64_64,
        max_q64_64=placeholder.max_q64_64,
        median_q64_64=placeholder.median_q64_64,
        percentile_5_q64_64=placeholder.percentile_5_q64_64,
        percentile_95_q64_64=placeholder.percentile_95_q64_64,
        expected_shortfall_q64_64=placeholder.expected_shortfall_q64_64,
        histogram_buckets=placeholder.histogram_buckets,
        primary_benchmark=placeholder.primary_benchmark,
        experimental_flag=placeholder.experimental_flag,
        content_hash=content_hash,
    )


def _compute_distribution_content_hash(summary: DistributionSummary) -> str:
    payload = {
        "metric_kind": summary.metric_kind,
        "sample_count": summary.sample_count,
        "sum_q64_64": summary.sum_q64_64,
        "min_q64_64": summary.min_q64_64,
        "max_q64_64": summary.max_q64_64,
        "median_q64_64": summary.median_q64_64,
        "percentile_5_q64_64": summary.percentile_5_q64_64,
        "percentile_95_q64_64": summary.percentile_95_q64_64,
        "expected_shortfall_q64_64": summary.expected_shortfall_q64_64,
        "histogram_buckets": [list(entry) for entry in summary.histogram_buckets],
        "primary_benchmark": summary.primary_benchmark,
        "experimental_flag": summary.experimental_flag,
    }
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def profit_probability_q64_64(samples: Sequence[int]) -> int:
    """Return the Q64.64 fraction of samples that are strictly positive.

    The probability is a Q64.64 fraction: ``Q64_SCALE`` means "every
    sample was positive", ``0`` means "no sample was positive".
    An empty sample raises :class:`EmptyDistributionError`.
    """
    if not samples:
        raise EmptyDistributionError("profit_probability_q64_64: samples must be non-empty")
    n = len(samples)
    positive = sum(1 for s in samples if s > 0)
    return (positive << 64) // n


def boundary_touch_probability_q64_64(samples: Sequence[int], *, boundary_value_q64_64: int) -> int:
    """Return the Q64.64 fraction of samples whose value is ``>= boundary_value``.

    The boundary may be the lower bound (e.g. zero for "exit /
    rebalance") or an upper bound (e.g. expected fee edge). The
    caller supplies the inequality direction through the meaning
    of ``boundary_value_q64_64``; the function uses ``>=``.
    """
    if not samples:
        raise EmptyDistributionError("boundary_touch_probability_q64_64: samples must be non-empty")
    if not isinstance(boundary_value_q64_64, int) or isinstance(boundary_value_q64_64, bool):
        raise InvalidDistributionError(
            f"boundary_touch_probability_q64_64: boundary_value_q64_64 must "
            f"be int, got {type(boundary_value_q64_64).__name__}"
        )
    n = len(samples)
    hits = sum(1 for s in samples if s >= boundary_value_q64_64)
    return (hits << 64) // n


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "DISTRIBUTIONS_VERSION",
    "Q64_SCALE",
    "VALID_METRIC_KINDS",
    "DistributionError",
    "DistributionSummary",
    "EmptyDistributionError",
    "InvalidDistributionError",
    "boundary_touch_probability_q64_64",
    "build_distribution_summary",
    "profit_probability_q64_64",
]
