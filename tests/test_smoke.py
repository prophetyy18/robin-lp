"""Smoke tests for the Phase 0 project skeleton.

These tests exist only to prove the package can be imported and the CLI
entry point runs in a clean environment. Real test coverage is added by
later tasks (T010+, see ``docs/spec/architecture/ARCHITECTURE.md``).
"""

from __future__ import annotations

import subprocess
import sys

import robinhood_lp


def test_package_version_is_string() -> None:
    """The package exposes a string version constant."""
    assert isinstance(robinhood_lp.__version__, str)
    assert robinhood_lp.__version__  # non-empty


def test_python_dash_m_version_exits_zero() -> None:
    """``python -m robinhood_lp --version`` runs and prints the version."""
    result = subprocess.run(
        [sys.executable, "-m", "robinhood_lp", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == robinhood_lp.__version__


def test_python_dash_m_help_exits_zero() -> None:
    """``python -m robinhood_lp`` (no args) prints help and exits zero."""
    result = subprocess.run(
        [sys.executable, "-m", "robinhood_lp"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower() or "options" in result.stdout.lower()
