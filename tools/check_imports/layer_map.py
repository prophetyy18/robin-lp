"""Layer map and tier order for the ADR-006 dependency checker (T006).

The tier order is read out of ADR-006's decision and the allowed-shape
diagram. A lower index sits closer to the bottom of the diagram
(``protocol/domain``) and a higher index sits closer to the top
(``presentation / reports``). Imports are allowed to flow from a
higher tier downward to a lower tier; the reverse ("lower to higher")
is forbidden.

ADR-006 writes ``strategy`` and ``backtest`` as the single tier
"strategy and backtest"; the table below uses ``strategy`` and
``backtest`` as two canonical names that occupy the same tier index
so that the diagram order is preserved. Application / orchestration
sits above strategy, risk and execution but below presentation
(ADR-006 §"Decision", diagram).

``robinhood_lp.config`` is classified with the sentinel layer name
``"platform-reserved"``. ADR-006's ten layers contain no platform
layer; its own migration trigger reserves one for a future ADR. The
checker treats every edge touching a sentinel layer as a violation
— the closed exception list in ``suppressions.toml`` documents the
known cases and carries an expiry that forces revisit.

The map is exposed both as a ``PACKAGE_DEFAULTS`` table (one entry
per package) and as ``MODULE_OVERRIDES`` (one entry per module).
Resolution looks up the longest matching module override first and
then falls back to the longest matching package default; modules
with no match are reported as ``unclassified-module`` findings.
"""

from __future__ import annotations

# Canonical tier order. A lower index is lower in ADR-006's diagram.
# ``strategy`` and ``backtest`` share the same tier because ADR-006
# writes "strategy and backtest" as a single tier entry; both names
# appear in ARCHITECTURE.md §2.2 and must map to the same rank.
TIER_ORDER: tuple[str, ...] = (
    "protocol/domain",
    "rpc adapter",
    "storage",
    "reconstruction",
    "features",
    "strategy",
    "backtest",
    "risk",
    "execution",
    "application / orchestration",
    "presentation / reports",
)

# Sentinel layer name for modules ADR-006 does not yet cover. Used by
# the checker to mark ``robinhood_lp.config`` until a new ADR introduces
# a real platform layer. Not in the tier order — every edge that
# touches it is reported.
PLATFORM_RESERVED_LAYER: str = "platform-reserved"

# Aliases that appear in §2.2 row text and must normalise to the
# canonical tier-order name. The aliases themselves never appear in
# the layer map; they exist only so the §2.2 agreement check can
# compare a row's declared layer against the canonical order.
LAYER_ALIASES: dict[str, str] = {
    "protocol/domain": "protocol/domain",
    "rpc adapter": "rpc adapter",
    "storage": "storage",
    "reconstruction": "reconstruction",
    "features": "features",
    "strategy": "strategy",
    "backtest": "backtest",
    "backtest / research": "backtest",
    "risk": "risk",
    "execution": "execution",
    "application / orchestration": "application / orchestration",
    "presentation / reports": "presentation / reports",
    "presentation / ops": "presentation / reports",
    "presentation / controls": "presentation / reports",
    "presentation": "presentation / reports",
    PLATFORM_RESERVED_LAYER: PLATFORM_RESERVED_LAYER,
}


def canonical_layer(layer: str) -> str:
    """Return the canonical tier name for a raw §2.2 layer string.

    Unknown layer strings are returned unchanged so the §2.2 agreement
    check can report them as disagreement findings instead of silently
    normalising them.
    """

    return LAYER_ALIASES.get(layer, layer)


def tier_index(layer: str) -> int:
    """Return the tier index for a canonical layer name.

    Layers not in the tier order (including ``PLATFORM_RESERVED_LAYER``)
    return ``-1``; callers use that to detect edges that touch a layer
    outside the ADR-006 ordering. ADR-006 writes "strategy and backtest"
    as a single tier, so both canonical names share the same index —
    a strategy module importing a backtest module is not a lower-to-
    higher violation because both sit at the same tier rank.
    """

    if layer == PLATFORM_RESERVED_LAYER:
        return -1
    # Strategy and backtest share the same tier per ADR-006 §"Decision".
    # The alias is resolved before the generic ``TIER_ORDER.index``
    # lookup so a strategy import that targets a backtest module is
    # not flagged as lower-to-higher.
    if layer == "backtest":
        return TIER_ORDER.index("strategy")
    try:
        return TIER_ORDER.index(layer)
    except ValueError:
        return -1


# Package defaults. A package default is consulted when no module
# override matches. Every sub-package listed in
# ``docs/spec/architecture/ARCHITECTURE.md`` §2.2 has an entry here;
# packages planned by future tasks carry their intended default.
# The top-level ``robinhood_lp`` package sits in the
# application / orchestration tier and is matched by exact name
# only (see ``classify``), so a brand-new sub-package whose name
# has not yet been added to the map still fails the unclassified
# rule.
PACKAGE_DEFAULTS: dict[str, str] = {
    "robinhood_lp.protocol": "protocol/domain",
    "robinhood_lp.rpc": "rpc adapter",
    "robinhood_lp.storage": "storage",
    # ``discovery`` is a multi-layer package per ADR-006 — most of
    # its modules are storage, with one rpc-adapter prober and one
    # risk admission component; per-module overrides below cover the
    # exceptions and ``__init__`` itself, which aggregates across
    # layers and therefore must be classified to a higher tier.
    "robinhood_lp.discovery": "storage",
    "robinhood_lp.replay": "reconstruction",
    "robinhood_lp.features": "features",
    "robinhood_lp.strategy": "strategy",
    "robinhood_lp.backtest": "backtest",
    "robinhood_lp.reports": "backtest",
    "robinhood_lp.robustness": "backtest",
    "robinhood_lp.qualification": "presentation / reports",
    "robinhood_lp.ingestion": "application / orchestration",
    "robinhood_lp.quality": "storage",
    "robinhood_lp.presentation": "presentation / reports",
    # Packages planned by future tasks. Keeping their declared
    # default here makes the map authoritative even when the
    # package's modules are not yet present.
    "robinhood_lp.risk": "risk",
    "robinhood_lp.execution": "execution",
    "robinhood_lp.application": "application / orchestration",
    "robinhood_lp.ops": "presentation / reports",
    "robinhood_lp.research": "backtest",
    "robinhood_lp.web": "presentation / reports",
    # ``config`` is the reserved platform case (ADR-006 §"Migration
    # trigger"). The sentinel layer name signals that this module is
    # intentionally outside ADR-006's tier order; the exception list
    # documents today's known edges.
    "robinhood_lp.config": PLATFORM_RESERVED_LAYER,
}

# Exact-match defaults. These apply only when ``module_path`` is
# equal to the key; they do not propagate to sub-modules. Used for
# the top-level package name ``robinhood_lp``, whose runtime module
# is the same as ``robinhood_lp.__init__`` but appears without the
# ``.__init__`` suffix in AST import targets.
EXACT_DEFAULTS: dict[str, str] = {
    "robinhood_lp": "application / orchestration",
}

# Per-module overrides. Looked up before any package default so that
# individual modules can be reclassified when their actual role
# differs from their package's default. ``__init__`` files use the
# suffix ``.__init__`` so the layer map can distinguish a package
# façade (which aggregates across tiers) from sibling modules.
MODULE_OVERRIDES: dict[str, str] = {
    # Top-level façade: the library ``__init__`` and the ``__main__``
    # CLI both sit in the application / orchestration tier. Listed
    # explicitly because ``PACKAGE_DEFAULTS`` deliberately omits
    # the top-level package (see comment in PACKAGE_DEFAULTS).
    "robinhood_lp.__init__": "application / orchestration",
    "robinhood_lp.__main__": "application / orchestration",
    # ``discovery`` houses one rpc-adapter prober and one risk
    # admission component per §2.2 (T024, T025).
    "robinhood_lp.discovery.chain_capability": "rpc adapter",
    "robinhood_lp.discovery.asset_admission": "risk",
    # ``discovery.__init__`` aggregates modules from multiple layers
    # (storage, rpc adapter and risk); the only legal tier for a
    # cross-layer façade is application / orchestration.
    "robinhood_lp.discovery.__init__": "application / orchestration",
    # ``research`` is a multi-layer package (T100–T104). Each module
    # sits in a distinct tier per §2.2.
    "robinhood_lp.research.dataset": "storage",
    "robinhood_lp.research.panel": "backtest",
    "robinhood_lp.research.models": "strategy",
    # T103 names ``robinhood_lp.web`` research pages, which the §2.2
    # row classifies as ``presentation / controls`` — the canonical
    # alias for ``presentation / reports``.
}


def classify(module_path: str) -> str | None:
    """Return the canonical layer for ``module_path`` or ``None``.

    Resolution walks ``MODULE_OVERRIDES`` first (longest match wins)
    and then ``PACKAGE_DEFAULTS`` (longest match wins). ``None``
    means the module is not classified — the caller must report it.
    The top-level package path (``robinhood_lp``) returns the
    package default; a non-existent module path returns ``None``.
    """

    candidates = sorted(MODULE_OVERRIDES, key=len, reverse=True)
    for candidate in candidates:
        if module_path == candidate or module_path.startswith(candidate + "."):
            return MODULE_OVERRIDES[candidate]
    # Exact-match defaults (only when ``module_path`` equals the key).
    # The top-level ``robinhood_lp`` package lives here because the
    # package default below would otherwise propagate the same value
    # to every ``robinhood_lp.*`` sub-module and mask unknown
    # packages as "classified".
    if module_path in EXACT_DEFAULTS:
        return EXACT_DEFAULTS[module_path]
    candidates = sorted(PACKAGE_DEFAULTS, key=len, reverse=True)
    for candidate in candidates:
        if module_path == candidate or module_path.startswith(candidate + "."):
            return PACKAGE_DEFAULTS[candidate]
    return None


__all__ = [
    "EXACT_DEFAULTS",
    "LAYER_ALIASES",
    "MODULE_OVERRIDES",
    "PACKAGE_DEFAULTS",
    "PLATFORM_RESERVED_LAYER",
    "TIER_ORDER",
    "canonical_layer",
    "classify",
    "tier_index",
]
