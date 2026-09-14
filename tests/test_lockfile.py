"""Reproducibility guard for the locked dependency file.

``requirements.lock.txt`` is the Phase 0 acceptance artefact promised by
T001. It pins every direct + transitive runtime/dev dependency of the
project to an exact version with a PyPI SHA-256 hash so that
``pip install --require-hashes -r requirements.lock.txt`` produces a
byte-identical set of wheels on every host.

These tests verify only the **shape and self-consistency** of the file.
They do not (and cannot) exercise ``pip install`` end-to-end without
network access. The full clean-install evidence is captured separately
under ``todo/evidence/P00-engineering-baseline/T001/``.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Repository root relative to this test file.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
LOCKFILE: Path = REPO_ROOT / "requirements.lock.txt"

#: Packages the runtime declares in ``pyproject.toml [project] dependencies``.
#: Each must appear in the lockfile so a fresh ``pip install`` reproduces
#: the same transitive closure that ``pip install -e .[dev]`` would.
DIRECT_RUNTIME_DEPS: tuple[str, ...] = (
    "pydantic",
    "eth-hash",
    "httpx",
)

#: Packages the test/lint/type toolchain declares in ``[project.optional-
#: dependencies].dev``. Each must appear in the lockfile.
DIRECT_DEV_DEPS: tuple[str, ...] = (
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "ruff",
    "mypy",
)

#: ``eth-hash`` is declared with the ``[pycryptodome]`` extra in
#: pyproject.toml. The lockfile must therefore resolve a PyPI entry
#: whose wheel ships the native ``pycryptodome`` backend; without it
#: ``keccak256`` falls back to the slower pure-Python implementation.
REQUIRED_BACKEND: str = "pycryptodome"

#: 64-character lowercase hex SHA-256 digest.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _parse_lockfile(text: str) -> dict[str, list[str]]:
    """Parse ``requirements.lock.txt`` into ``{package: [hash, ...]}``.

    Lines ending with a backslash continue the current record. Comments
    (``#``) and blank lines are skipped.
    """
    records: dict[str, list[str]] = {}
    current_name: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        # Continuation lines in pip-compile output end with a single
        # backslash; strip it before classifying the line.
        if line.endswith("\\"):
            line = line[:-1].rstrip()
        stripped = line.lstrip()
        if stripped.startswith("--hash=sha256:"):
            assert current_name is not None, "hash appears before any package name"
            digest = stripped.removeprefix("--hash=sha256:").strip()
            assert _SHA256_RE.match(digest), f"invalid SHA-256 hex: {digest!r}"
            records[current_name].append(digest)
            continue
        # New package header (e.g. ``pydantic==2.13.5 \`` or ``pydantic==2.13.5``)
        assert "==" in stripped, f"unrecognised lockfile line: {raw_line!r}"
        name, _version = stripped.split("==", 1)
        name = name.strip()
        current_name = name
        records.setdefault(name, [])
    return records


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_lockfile_exists_at_repo_root() -> None:
    """The locked dependency file lives at the repository root."""
    assert LOCKFILE.is_file(), f"missing lockfile at {LOCKFILE}"


def test_lockfile_is_parseable_and_consistent() -> None:
    """Every record has a name, exactly one pinned version, and at least one valid hash."""
    text = LOCKFILE.read_text(encoding="utf-8")
    records = _parse_lockfile(text)
    assert records, "lockfile parsed to zero records"

    for name, hashes in records.items():
        assert hashes, f"{name} has no --hash entries"
        # Hashes must be unique per package — duplicate hashes are a sign
        # of an upstream re-upload that we should not silently trust.
        assert len(set(hashes)) == len(hashes), f"{name} has duplicate hashes"


def test_lockfile_covers_every_direct_runtime_dependency() -> None:
    """Every name declared in ``[project] dependencies`` appears in the lockfile."""
    text = LOCKFILE.read_text(encoding="utf-8")
    records = _parse_lockfile(text)
    for required in DIRECT_RUNTIME_DEPS:
        assert required in records, f"runtime dependency {required!r} missing from lockfile"


def test_lockfile_covers_every_direct_dev_dependency() -> None:
    """Every name declared in ``[project.optional-dependencies].dev`` appears in the lockfile."""
    text = LOCKFILE.read_text(encoding="utf-8")
    records = _parse_lockfile(text)
    for required in DIRECT_DEV_DEPS:
        assert required in records, f"dev dependency {required!r} missing from lockfile"


def test_lockfile_pins_eth_hash_pycryptodome_backend() -> None:
    """``eth-hash`` is declared with the ``pycryptodome`` extra and the lockfile resolves it."""
    text = LOCKFILE.read_text(encoding="utf-8")
    assert REQUIRED_BACKEND in text, (
        f"lockfile does not reference the {REQUIRED_BACKEND!r} backend "
        f"required by ``eth-hash[pycryptodome]``"
    )
    records = _parse_lockfile(text)
    assert "pycryptodome" in records, "pycryptodome must be pinned by the lockfile"
    assert "eth-hash" in records, "eth-hash must be pinned by the lockfile"
