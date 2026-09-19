"""T007 regression: the three ADR-006 import edges stay repaired.

T007 closes three import edges that contradict ADR-006. The fix
moves the contracts that those edges leaked (the strategy-callback
request / response, the input ``BacktestEvent`` and its closed
vocabularies, the V4 log-record dataclasses, and the V4 hook-flag bit
constants) into the protocol/domain layer so both sides import the
shared contract without crossing layer boundaries in the wrong
direction.

The regression is parsed from the sources with the standard library's
``ast`` module; the test never imports the production code, never
executes it, and only reads file bodies. Each rule below mirrors one
of the three edges the contract enumerates and would fail if any
caller re-introduces the forbidden import.

The checks are scoped narrowly:

- A module under :mod:`robinhood_lp.strategy` may not import
  :mod:`robinhood_lp.backtest`. (Strategy and backtest share the
  same ADR-006 tier per the layer map, but the strategy layer must
  not depend on a concrete backtest implementation; per T007 the
  shared contracts live in the protocol/domain layer.)
- A module under :mod:`robinhood_lp.replay` may not import
  :mod:`robinhood_lp.storage` or :mod:`robinhood_lp.rpc`. The
  reconstruction layer consumes injected read ports, not concrete
  storage / RPC modules (ADR-006 §"Decision").
- :mod:`robinhood_lp.qualification.hook_pack` may not import
  :mod:`robinhood_lp.config`. The qualification layer depends only on
  the protocol layer for the shared immutable hook-flag constants
  (T043 docstring contract; T007 deliverable 3).

A failure of this regression is the recorded evidence the review
relies on: a check that never fires is indistinguishable from one
that passes, so the contract requires the regression to *fail*
when the rule is broken.

The test is committed alongside the fix so a future contributor who
re-introduces any of the three edges hits the same regression at the
same place.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "robinhood_lp"


def _module_path_for(path: Path) -> str:
    """Return the ``robinhood_lp.<...>`` dotted path for ``path``.

    ``src/robinhood_lp/strategy/baselines.py`` →
    ``robinhood_lp.strategy.baselines``.
    ``src/robinhood_lp/replay/__init__.py`` → ``robinhood_lp.replay``.
    """
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(["robinhood_lp", *parts])


def _iter_python_files(root: Path) -> list[Path]:
    """Return every Python file under ``root`` sorted for determinism."""
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _robinhood_imports(path: Path) -> list[tuple[int, str]]:
    """Return every ``robinhood_lp.*`` import target inside ``path``.

    The walker only inspects ``Import`` and ``ImportFrom`` nodes; it
    reads the file body with :mod:`ast` so the production code is
    never imported or executed. The returned list pairs the source
    line number with the dotted module the import targets.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if not node.module or not node.module.startswith("robinhood_lp"):
                continue
            out.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("robinhood_lp"):
                    out.append((node.lineno, alias.name))
    return out


# ---------------------------------------------------------------------------
# Rule 1 — strategy layer must not import the backtest layer
# ---------------------------------------------------------------------------


def _strategy_files() -> list[Path]:
    return _iter_python_files(SRC / "strategy")


def _replay_files() -> list[Path]:
    return _iter_python_files(SRC / "replay")


@pytest.mark.parametrize("path", _strategy_files(), ids=_module_path_for)
def test_strategy_module_does_not_import_backtest(path: Path) -> None:
    """No module under :mod:`robinhood_lp.strategy` may import the
    backtest layer.

    The strategy and backtest tiers share the same ADR-006 rank, but
    the strategy layer must depend on the shared contracts in
    :mod:`robinhood_lp.protocol.contracts` (T007 deliverable 1) — not
    on a concrete backtest implementation. A re-introduction of the
    import below is the documented regression signal.
    """
    module_path = _module_path_for(path)
    offenders: list[tuple[int, str]] = []
    for line, target in _robinhood_imports(path):
        if target == "robinhood_lp.backtest" or target.startswith("robinhood_lp.backtest."):
            offenders.append((line, target))
    assert not offenders, (
        f"{module_path} imports the backtest layer; strategy must depend on "
        f"robinhood_lp.protocol.contracts instead. Offending imports: "
        + ", ".join(f"{module_path}:{line} -> {t}" for line, t in offenders)
    )


# ---------------------------------------------------------------------------
# Rule 2 — replay layer must not import storage or rpc
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _replay_files(), ids=_module_path_for)
def test_replay_module_does_not_import_storage_or_rpc(path: Path) -> None:
    """No module under :mod:`robinhood_lp.replay` may import the
    storage or RPC adapters.

    Per ADR-006 §"Decision", the reconstruction layer consumes injected
    read ports, not concrete storage / RPC modules. The log-record
    contracts moved to :mod:`robinhood_lp.protocol.records` (T007
    deliverable 2) so the replay modules can consume records through
    the input they are given. A re-introduction of the storage or
    rpc import below is the documented regression signal.
    """
    module_path = _module_path_for(path)
    offenders: list[tuple[int, str]] = []
    for line, target in _robinhood_imports(path):
        if (
            target == "robinhood_lp.storage"
            or target.startswith("robinhood_lp.storage.")
            or target == "robinhood_lp.rpc"
            or target.startswith("robinhood_lp.rpc.")
        ):
            offenders.append((line, target))
    assert not offenders, (
        f"{module_path} imports storage or rpc; replay must consume the "
        f"log-record contracts from robinhood_lp.protocol.records. "
        "Offending imports: " + ", ".join(f"{module_path}:{line} -> {t}" for line, t in offenders)
    )


# ---------------------------------------------------------------------------
# Rule 3 — qualification hook pack must not import config
# ---------------------------------------------------------------------------


def test_qualification_hook_pack_does_not_import_config() -> None:
    """:mod:`robinhood_lp.qualification.hook_pack` must not import
    :mod:`robinhood_lp.config`.

    The qualification hook pack depends only on the protocol layer
    for the shared immutable hook-flag constants (T043 docstring
    contract; T007 deliverable 3 moved the constants to
    :mod:`robinhood_lp.protocol.ids`). A re-introduction of the
    config import is the documented regression signal.
    """
    path = SRC / "qualification" / "hook_pack.py"
    offenders: list[tuple[int, str]] = []
    for line, target in _robinhood_imports(path):
        if target == "robinhood_lp.config" or target.startswith("robinhood_lp.config."):
            offenders.append((line, target))
    assert not offenders, (
        "robinhood_lp.qualification.hook_pack imports the config layer; the "
        "hook-flag constants live in robinhood_lp.protocol.ids. Offending "
        "imports: " + ", ".join(f"hook_pack.py:{line} -> {t}" for line, t in offenders)
    )
