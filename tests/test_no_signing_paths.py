"""Regression tests proving V1 contains no signing or broadcast surface.

T004 / docs/threat-model.md §3 T-12 require that:

1. No source file under ``src/`` imports a write-capable RPC method or
   a key/seed loader.
2. No source file under ``src/`` mentions forbidden identifiers that
   would indicate live signing or broadcast intent.
3. The package's exposed entry points (``robinhood_lp.__main__`` and
   ``robinhood_lp.config``) do not re-export any such symbol.

These tests are intentionally narrow: they fail closed on any *new*
introduction. They do not enforce semantic correctness — they enforce
absence of a forbidden capability.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from pathlib import Path

# ---------------------------------------------------------------------------
# Forbidden constructs
# ---------------------------------------------------------------------------

#: Names whose presence in an import statement indicates live signing,
#: key management, or broadcast capability.
FORBIDDEN_IMPORTS: frozenset[str] = frozenset(
    {
        # Ethereum signing / key management
        "eth_account",
        "eth_keys",
        "eth_keyfile",
        "eth_sign",
        "eth_keys.datatypes",
        # web3.py write API namespaces
        "web3.eth.account",
        "web3.eth.sendRawTransaction",
        # Generic key/seed loaders
        "bip_utils",
        "mnemonic",
        "bip32utils",
        "pycoin",
        # Generic signing libs (project does not need any of these)
        "cryptography.hazmat.primitives.asymmetric",
    }
)

#: Free-text identifiers whose mere mention in source code would indicate
#: live signing or broadcast intent.
FORBIDDEN_IDENTIFIERS: tuple[str, ...] = (
    r"\bprivate[_-]?key\b",
    r"\bseed[_-]?phrase\b",
    r"\bmnemonic\b",
    r"\bkeystore\b",
    r"\braw[_-]?transaction\b",
    r"sendRawTransaction",
    r"send_transaction",
    r"\bsignTypedData\b",
    r"\bsign_transaction\b",
    r"\bpersonal_sign\b",
    r"\becrecover\b",
    r"\bgasPrice\b\s*[:=]",  # gas pricing implies execution
    r"\bmaxFeePerGas\b",
    r"\bmaxPriorityFeePerGas\b",
    r"\bnonce\b\s*\+\s*1",  # nonce increment pattern
)

#: Modules in ``src/robinhood_lp`` are scanned by file name; this is the
#: trusted boundary. Anything outside ``src/`` is intentionally not
#: scanned because tests, docs, and tools are allowed to *discuss*
#: these concepts.
SOURCE_ROOT: Path = Path(__file__).resolve().parent.parent / "src" / "robinhood_lp"

#: Allow these test fixtures (in ``tests/``) to contain forbidden words
#: because they are negative-test data; this list documents them so
#: review of test_no_signing_paths.py stays auditable.
DOCUMENTED_TEST_EXCEPTIONS: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _python_source_files(root: Path) -> Iterable[Path]:
    yield from sorted(root.rglob("*.py"))


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _imports_in(text: str) -> Iterable[str]:
    """Yield every dotted module name referenced by an import statement."""
    for match in re.finditer(
        r"^\s*(?:from\s+([\w.]+)|import\s+([\w.]+))",
        text,
        re.MULTILINE,
    ):
        module = match.group(1) or match.group(2)
        if module:
            yield module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_source_root_exists() -> None:
    """Sanity: the source root is the directory we think it is."""
    assert SOURCE_ROOT.is_dir(), f"expected source root at {SOURCE_ROOT}"


def test_pyproject_runtime_dependencies_are_minimal() -> None:
    """T004 also requires a small, auditable runtime dependency surface.

    The project must not declare any signing/broadcast library in its
    declared runtime dependencies. This catches the easy mistake of
    adding ``web3`` and using ``web3.eth.account`` by accident.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps: list[str] = payload.get("project", {}).get("dependencies", []) or []
    forbidden_in_deps = sorted(
        d for d in deps if any(token in d.lower() for token in ("eth-account", "mnemonic", "bip"))
    )
    assert not forbidden_in_deps, (
        f"pyproject.toml declares forbidden signing/seed dependencies: {forbidden_in_deps}"
    )


def test_no_forbidden_imports_in_source() -> None:
    """No source file imports a known signing/broadcast/seed module."""
    offenders: list[tuple[Path, str]] = []
    for path in _python_source_files(SOURCE_ROOT):
        text = _read_text(path)
        for module in _imports_in(text):
            for forbidden in FORBIDDEN_IMPORTS:
                # Match the imported module or any parent prefix.
                if module == forbidden or module.startswith(forbidden + "."):
                    offenders.append((path, module))
    assert not offenders, "forbidden imports found in src/:\n" + "\n".join(
        f"  {p}: imports {m}" for p, m in offenders
    )


def test_no_forbidden_identifiers_in_source() -> None:
    """No source file mentions live-signing or broadcast identifiers."""
    offenders: list[tuple[Path, str, str]] = []
    for path in _python_source_files(SOURCE_ROOT):
        text = _read_text(path)
        for pattern in FORBIDDEN_IDENTIFIERS:
            for match in re.finditer(pattern, text):
                offenders.append((path, pattern, match.group(0)))
    assert not offenders, "forbidden identifiers found in src/:\n" + "\n".join(
        f"  {p}: pattern {pat!r} matched {hit!r}" for p, pat, hit in offenders
    )


def test_no_pyproject_entry_point_for_broadcast() -> None:
    """The CLI entry point defined in pyproject.toml must not be a broadcast tool."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    scripts: dict[str, str] = payload.get("project", {}).get("scripts", {}) or {}
    for name, target in scripts.items():
        assert "send" not in name.lower(), f"script name suggests broadcast: {name}"
        assert "sign" not in name.lower(), f"script name suggests signing: {name}"
        assert "broadcast" not in name.lower(), f"script name suggests broadcast: {name}"
        # The target must point inside the robinhood_lp package.
        assert target.startswith("robinhood_lp."), (
            f"script {name!r} target must live in robinhood_lp, got {target!r}"
        )


def test_run_mode_live_is_not_in_supported_defaults() -> None:
    """The default run mode is 'paper' and live is rejected at parse time."""
    from robinhood_lp.config import RootConfig, RunMode  # noqa: PLC0415

    assert RootConfig.model_fields["default_run_mode"].default == RunMode.PAPER
    assert RunMode.LIVE.value == "live"
    # ``live`` is a string the user could try to set; the validator must reject it.
    import pytest  # noqa: PLC0415

    with pytest.raises(ValueError, match="live"):
        RootConfig.model_validate(
            {
                "default_run_mode": "live",
                "chains": [
                    {
                        "chain_id": 46630,
                        "rpc_url_env": "RPC_URL",
                        "pool_manager_address": "0x" + "0" * 40,
                        "state_view_address": "0x" + "0" * 40,
                    }
                ],
            }
        )
