"""Deterministic offline import-graph checker (T006).

Enforces the layered dependency direction declared in ADR-006 against
every ``robinhood_lp.*`` module under ``src/robinhood_lp/``. The check
parses Python source with the standard library's ``ast`` module — it
never imports or executes project code, never reaches the network, and
uses no third-party dependency.

The checker fails on any of:

- a ``robinhood_lp.*`` import whose source module is in a lower layer
  than its target (the lower-to-higher rule from ADR-006 §"Decision");
- an import cycle between two packages;
- an import of ``time``, ``random`` or another unseeded clock inside
  ``robinhood_lp.protocol``, ``replay``, ``features``, ``strategy`` or
  ``backtest`` (ADR-006 §"Decision", 5th bullet);
- a module under ``src/robinhood_lp/`` that the layer map does not
  classify;
- a §2.2 row in ``docs/spec/architecture/ARCHITECTURE.md`` whose
  declared layer disagrees with the map.

Findings are matched against the closed exception list in
``docs/implement/ci/suppressions.toml``. Each entry must carry the
four required fields (``rule``, ``reason``, ``owner``, ``expiry``) and
must match at least one finding; an entry that no longer matches a
finding is itself fatal (T005 reuse, T006 acceptance).

The ``robinhood_lp.config`` package is today's declared case. ADR-006's
migration trigger reserves a "platform" layer for a future ADR, so
the check classifies it with the sentinel ``"platform-reserved"`` and
treats every edge that touches it as a finding. The current edges are
recorded explicitly in ``suppressions.toml`` with an expiry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    """A single violation of the layered dependency direction.

    ``rule`` is the dotted name of the resolver that produced the
    finding. ``path`` is the repository-relative document or source
    path. ``line`` is the 1-indexed line where the offending token
    appears, or ``0`` for whole-file / cross-file findings. ``token``
    is a stable identifier (an edge like ``"A -> B"``, a module
    name, or a cycle description) that the suppression registry
    matches against. ``message`` is a human-readable explanation.
    """

    rule: str
    path: str
    line: int
    token: str
    message: str

    def key(self) -> tuple[str, str, str]:
        """Stable key used to match a finding against a suppression entry."""

        return (self.rule, self.path, self.token)


def module_set(repo_root: Path | str) -> list[Path]:
    """Return every Python file under ``src/robinhood_lp/`` in stable order.

    The set is the input to the AST walker. An empty set is a fatal
    failure: the checker would otherwise scan nothing. The set is
    deterministic (sorted) so repeated runs produce byte-equivalent
    edge lists.
    """

    root = Path(repo_root).resolve()
    src_root = root / "src" / "robinhood_lp"
    if not src_root.is_dir():
        return []
    return sorted(src_root.rglob("*.py"))


def run(
    repo_root: Path | str,
    *,
    suppressions_path: Path | str | None = None,
    config_path: Path | str | None = None,
    architecture_path: Path | str | None = None,
    modules: Iterable[Path | str] | None = None,
) -> list[Finding]:
    """Run the full import-graph check.

    Returns the list of *unmatched* findings plus one synthetic
    finding per unused suppression entry. An empty list means the
    repository passes the check. Any error in the input set (missing
    config, missing suppressions, empty module set, unparsable §2.2
    row, unclassified module, malformed suppression entry) is
    surfaced as a finding rather than raised, so the caller can
    present every failure in the same shape.
    """

    from .check import run as _run

    return _run(
        repo_root,
        suppressions_path=suppressions_path,
        config_path=config_path,
        architecture_path=architecture_path,
        modules=modules,
    )


__all__ = ["Finding", "module_set", "run"]
