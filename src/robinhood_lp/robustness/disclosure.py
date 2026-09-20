"""Multiple-comparison disclosure (T064).

A robustness sweep that ranks pools / regimes / parameter
combinations on the same metric is, by construction, a multiple-
comparison problem: the best rank is biased upward by the size of
the search space. T064 binds a disclosure the reviewer reads
*before* trusting a "best run" line: the disclosure names the
number of comparisons, the family-wise correction method, the
adjusted significant count, and the best-vs-sensitivity gap.

The module owns:

- :class:`MultipleComparisonMethod` — the closed vocabulary of
  adjustment methods.
- :class:`MultipleComparisonDisclosure` — the disclosure record
  the report carries.
- :func:`build_disclosure` — the canonical builder.

Design constraints:

- **Integer accounting.** Every count is a non-negative integer.
  The family-wise alpha is a Q64.64 ratio so a downstream consumer
  can compare it against the same scale other code uses.

- **Determinism.** The disclosure is a frozen dataclass; two
  equivalent inputs in any process produce the same disclosure.

- **Layer purity.** The module imports only the standard library.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

#: Module version.
DISCLOSURE_VERSION: Final[str] = "t064.robustness_disclosure.v1"

#: Q64.64 fixed-point scale (1.0 in Q64.64 terms).
_Q64_SCALE: Final[int] = 1 << 64

#: Closed vocabulary of adjustment methods. ``NONE`` means the
#: disclosure reports the unadjusted significant count alongside
#: the (potentially much smaller) adjusted count.
VALID_ADJUSTMENT_METHODS: Final[frozenset[str]] = frozenset(
    {"NONE", "BONFERRONI", "HOLM", "BENJAMINI_HOCHBERG"}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DisclosureError(ValueError):
    """Base class for disclosure construction failures."""


class InvalidAdjustmentMethodError(DisclosureError):
    """An adjustment method is outside the closed vocabulary."""


class DisclosureInputsError(DisclosureError):
    """A disclosure input is malformed (negative count, mismatched lengths, ...)."""


# ---------------------------------------------------------------------------
# Disclosure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MultipleComparisonDisclosure:
    """The multiple-comparison disclosure the report must carry.

    Field units:

    - ``n_comparisons`` — non-negative integer; the number of
      comparisons the search performed (e.g. grid points × pools).
    - ``n_families`` — positive integer; the number of independent
      comparison families the adjustment is applied across. ``1``
      means "single family".
    - ``adjustment_method`` — one of
      :data:`VALID_ADJUSTMENT_METHODS`.
    - ``family_wise_alpha_q64_64`` — Q64.64 ratio; the per-family
      Type-I error budget the adjustment preserves.
    - ``n_significant_unadjusted`` — non-negative integer; the
      count of comparisons significant under the unadjusted alpha.
    - ``n_significant_adjusted`` — non-negative integer; the
      count after the family-wise adjustment. Always ≤
      ``n_significant_unadjusted``.
    - ``best_metric_name`` — non-empty string; the metric the
      disclosure ranks on.
    - ``best_metric_value`` — non-negative integer; the best
      metric value seen across the search.
    - ``sensitivity_spread_q64_64`` — Q64.64 ratio; the
      range of metric values across the search. The disclosure
      carries the spread beside the best so the reviewer sees the
      sensitivity the runner measured.
    """

    n_comparisons: int
    n_families: int
    adjustment_method: str
    family_wise_alpha_q64_64: int
    n_significant_unadjusted: int
    n_significant_adjusted: int
    best_metric_name: str
    best_metric_value: int
    sensitivity_spread_q64_64: int

    def __post_init__(self) -> None:
        for name in (
            "n_comparisons",
            "n_families",
            "n_significant_unadjusted",
            "n_significant_adjusted",
            "best_metric_value",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise DisclosureInputsError(
                    f"MultipleComparisonDisclosure.{name}: must be int, got {type(value).__name__}"
                )
            if value < 0:
                raise DisclosureInputsError(
                    f"MultipleComparisonDisclosure.{name}: must be non-negative, got {value}"
                )
        if self.n_families <= 0:
            raise DisclosureInputsError(
                f"MultipleComparisonDisclosure.n_families: must be positive, got {self.n_families}"
            )
        if not isinstance(self.family_wise_alpha_q64_64, int) or isinstance(
            self.family_wise_alpha_q64_64, bool
        ):
            raise DisclosureInputsError(
                f"MultipleComparisonDisclosure.family_wise_alpha_q64_64: must be int, "
                f"got {type(self.family_wise_alpha_q64_64).__name__}"
            )
        if self.family_wise_alpha_q64_64 < 0:
            raise DisclosureInputsError(
                f"MultipleComparisonDisclosure.family_wise_alpha_q64_64: must be "
                f"non-negative, got {self.family_wise_alpha_q64_64}"
            )
        if self.adjustment_method not in VALID_ADJUSTMENT_METHODS:
            raise InvalidAdjustmentMethodError(
                f"MultipleComparisonDisclosure.adjustment_method: must be one of "
                f"{sorted(VALID_ADJUSTMENT_METHODS)}, got {self.adjustment_method!r}"
            )
        if not isinstance(self.best_metric_name, str) or not self.best_metric_name:
            raise DisclosureInputsError(
                "MultipleComparisonDisclosure.best_metric_name: must be non-empty str"
            )
        if not isinstance(self.sensitivity_spread_q64_64, int) or isinstance(
            self.sensitivity_spread_q64_64, bool
        ):
            raise DisclosureInputsError(
                f"MultipleComparisonDisclosure.sensitivity_spread_q64_64: must be int, "
                f"got {type(self.sensitivity_spread_q64_64).__name__}"
            )
        if self.sensitivity_spread_q64_64 < 0:
            raise DisclosureInputsError(
                f"MultipleComparisonDisclosure.sensitivity_spread_q64_64: must be "
                f"non-negative, got {self.sensitivity_spread_q64_64}"
            )
        if self.n_significant_adjusted > self.n_significant_unadjusted:
            raise DisclosureInputsError(
                f"MultipleComparisonDisclosure: n_significant_adjusted="
                f"{self.n_significant_adjusted} > n_significant_unadjusted="
                f"{self.n_significant_unadjusted}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "n_comparisons": self.n_comparisons,
            "n_families": self.n_families,
            "adjustment_method": self.adjustment_method,
            "family_wise_alpha_q64_64": self.family_wise_alpha_q64_64,
            "n_significant_unadjusted": self.n_significant_unadjusted,
            "n_significant_adjusted": self.n_significant_adjusted,
            "best_metric_name": self.best_metric_name,
            "best_metric_value": self.best_metric_value,
            "sensitivity_spread_q64_64": self.sensitivity_spread_q64_64,
        }


def build_disclosure(
    *,
    n_comparisons: int,
    n_families: int = 1,
    adjustment_method: str = "BONFERRONI",
    family_wise_alpha_q64_64: int = _Q64_SCALE // 20,
    metric_values: Sequence[int],
    best_metric_name: str,
    significant_unadjusted: Iterable[int] | None = None,
    significant_adjusted: Iterable[int] | None = None,
) -> MultipleComparisonDisclosure:
    """Build a :class:`MultipleComparisonDisclosure` from the search results.

    Parameters
    ----------
    n_comparisons:
        The total number of comparisons the search performed.
    n_families:
        The number of independent comparison families.
    adjustment_method:
        One of :data:`VALID_ADJUSTMENT_METHODS`. ``"BONFERRONI"``
        is the canonical default; ``"NONE"`` means "no adjustment".
    family_wise_alpha_q64_64:
        The per-family Type-I error budget the adjustment
        preserves.
    metric_values:
        The metric values the search ranked. The function picks
        the maximum (best) and computes the range (sensitivity
        spread).
    best_metric_name:
        The name of the metric the search ranked on.
    significant_unadjusted, significant_adjusted:
        Optional iterables whose lengths are the significant counts
        recorded in the disclosure. The function uses the lengths,
        not the values, so a caller can pass either the values or
        already-computed counts.
    """
    if not isinstance(n_comparisons, int) or isinstance(n_comparisons, bool):
        raise DisclosureInputsError(
            f"build_disclosure: n_comparisons must be int, got {type(n_comparisons).__name__}"
        )
    if n_comparisons < 0:
        raise DisclosureInputsError(
            f"build_disclosure: n_comparisons must be non-negative, got {n_comparisons}"
        )
    if not metric_values:
        raise DisclosureInputsError("build_disclosure: metric_values must be non-empty")
    if not isinstance(best_metric_name, str) or not best_metric_name:
        raise DisclosureInputsError("build_disclosure: best_metric_name must be non-empty str")
    best = -1
    worst = -1
    for value in metric_values:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise DisclosureInputsError(
                f"build_disclosure: every metric_value must be non-negative int, got {value!r}"
            )
        if best < 0 or value > best:
            best = value
        if worst < 0 or value < worst:
            worst = value
    spread = best - worst
    n_sig_unadj = len(list(significant_unadjusted)) if significant_unadjusted is not None else 0
    n_sig_adj = len(list(significant_adjusted)) if significant_adjusted is not None else n_sig_unadj
    return MultipleComparisonDisclosure(
        n_comparisons=n_comparisons,
        n_families=n_families,
        adjustment_method=adjustment_method,
        family_wise_alpha_q64_64=family_wise_alpha_q64_64,
        n_significant_unadjusted=n_sig_unadj,
        n_significant_adjusted=n_sig_adj,
        best_metric_name=best_metric_name,
        best_metric_value=best,
        sensitivity_spread_q64_64=spread,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "DISCLOSURE_VERSION",
    "VALID_ADJUSTMENT_METHODS",
    "DisclosureError",
    "DisclosureInputsError",
    "InvalidAdjustmentMethodError",
    "MultipleComparisonDisclosure",
    "build_disclosure",
]
