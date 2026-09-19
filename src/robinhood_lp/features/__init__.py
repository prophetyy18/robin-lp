"""Point-in-time features for the V1 research and execution paths (T050-T053).

The ``robinhood_lp.features`` package owns the deterministic,
event-time feature pipeline that turns reconciled V4 protocol state
into observation rows consumed by the strategy, risk, attribution
and reporting layers. Per ``docs/spec/architecture/ARCHITECTURE.md``
§2.1 the features layer may look ahead only inside the explicitly
declared unit/window semantics of each feature, never at the
strategy's decision time, and never against an unverified hook
model.

T053 — Add point-in-time quote and gas valuation — is the
quote-valuation half of this package: it defines the provider-neutral
observation schema, the ADR-014 numeraire hierarchy, the conversion
graph and missing policy, and the ``RELATIVE_ONLY`` output variant
that carries no USD-denominated field. T050 owns the bar / feature
rows; T053 owns the price and gas observation row that those bars
attach to.

This package must not import RPC, storage, signing, or execution;
it must not import ``float`` on any protocol / valuation path
(ADR-004); and a ``RELATIVE_ONLY`` output it produces must never be
treated as ranking-compatible with a USD-denominated output
(ADR-014 §3).
"""

from __future__ import annotations

__version__: str = "0.0.0"

__all__: list[str] = []
