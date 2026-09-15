"""Pinned V4 PoolManager protocol artifacts (T021).

Loads the four event topics and four function selectors used by
the framework's adapters and decoder (T020, T022, T030). Values
come from ``docs/implement/protocol-artifacts/v4-core-<commit>.json`` and are
pinned to a single source repository and commit (T021 acceptance:
"regenerate-and-compare test detects any ABI/topic/selector
drift").

The artifact file only contains entries the framework actually
consumes (T021 acceptance: "only actually used ABI entries are
stored"). Adding a new event or function means:

1. extend the JSON artifact under a new pinned-commit filename;
2. update ``CURRENT_ARTIFACT_FILENAME`` below;
3. add an entry to the ``ARTIFACTS_DIR`` registry.

This module never builds ABI encoders at runtime; it only loads the
precomputed constants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# Artifact location
# ---------------------------------------------------------------------------

ARTIFACTS_DIR: Final[Path] = (
    Path(__file__).resolve().parents[3] / "docs" / "implement" / "protocol-artifacts"
)

CURRENT_ARTIFACT_FILENAME: Final[str] = "v4-core-e50237c.json"

# Expected source commit (must match the file name and the entry
# inside the JSON). Used by tests/test_abi_artifacts.py to refuse
# silent drift.
EXPECTED_SOURCE_COMMIT: Final[str] = "e50237c43811bd9b526eff40f26772152a42daba"


# ---------------------------------------------------------------------------
# Loaded constants
# ---------------------------------------------------------------------------


def load_artifact() -> dict[str, Any]:
    """Load the current pinned artifact and verify its provenance."""
    path = ARTIFACTS_DIR / CURRENT_ARTIFACT_FILENAME
    if not path.is_file():
        raise FileNotFoundError(
            f"V4 artifact not found at {path}; "
            f"re-run 'forge test --match-contract SelectorOracle' in tools/oracle/ "
            f"and commit the output under docs/implement/protocol-artifacts/"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    commit = payload.get("_meta", {}).get("source_commit")
    if commit != EXPECTED_SOURCE_COMMIT:
        raise ValueError(
            f"artifact {path.name} source_commit={commit!r} "
            f"does not match EXPECTED_SOURCE_COMMIT={EXPECTED_SOURCE_COMMIT!r}; "
            f"update either the file or the constant"
        )
    return payload  # type: ignore[no-any-return]


def _parse_topic(hex_str: str) -> bytes:
    """Validate and decode a 32-byte hex topic0."""
    s = hex_str.lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 64:
        raise ValueError(f"topic0 must be 32 bytes (64 hex chars), got {len(s)}: {hex_str!r}")
    return bytes.fromhex(s)


def _parse_selector(hex_str: str) -> bytes:
    """Validate and decode a 4-byte hex selector."""
    s = hex_str.lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 8:
        raise ValueError(f"selector must be 4 bytes (8 hex chars), got {len(s)}: {hex_str!r}")
    return bytes.fromhex(s)


#: Map from V4 event name to its canonical 32-byte topic0.
EVENT_TOPICS: Final[dict[str, bytes]] = {
    name: _parse_topic(info["topic0"]) for name, info in load_artifact()["events"].items()
}

#: Map from V4 contract name to map of function name -> canonical 4-byte selector.
#:
#: The artifact nests function selectors by contract (``PoolManager`` and
#: ``StateView``) so the selector origin is unambiguous (T021 scope:
#: StateView ABI coverage alongside PoolManager). Tests and downstream
#: code should iterate ``FUNCTION_SELECTORS[contract].items()``.
FUNCTION_SELECTORS: Final[dict[str, dict[str, bytes]]] = {
    contract_name: {
        fn_name: _parse_selector(fn_info["selector"])
        for fn_name, fn_info in contract_functions.items()
    }
    for contract_name, contract_functions in load_artifact()["functions"].items()
}


__all__ = [
    "ARTIFACTS_DIR",
    "CURRENT_ARTIFACT_FILENAME",
    "EVENT_TOPICS",
    "EXPECTED_SOURCE_COMMIT",
    "FUNCTION_SELECTORS",
    "load_artifact",
]
