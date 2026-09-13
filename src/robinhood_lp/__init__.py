"""Robinhood Chain / Uniswap V4 LP research and paper-trading framework.

This package is intentionally minimal at Phase 0. It exists to:

- pin the supported Python version and packaging metadata (``pyproject.toml``);
- expose a single importable module path for downstream tests;
- provide a CLI entry point that prints the package version.

Subpackages and modules are added by Phase 1+ tasks (see ``docs/architecture.md``
section 2.2 for the planned module-to-layer mapping).
"""

from __future__ import annotations

__version__: str = "0.0.0"

__all__ = ["__version__"]
