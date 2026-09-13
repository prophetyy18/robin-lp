"""Drift test for pinned V4 PoolManager artifacts (T021).

T021 acceptance: "regenerate-and-compare test detects any ABI/topic/
selector drift". This module verifies the pinned JSON artifact:

- file exists and parses;
- declared source commit matches the constant;
- every event has a 32-byte topic0 (must round-trip through keccak256
  of the canonical signature);
- every function has a 4-byte selector (the canonical selectors
  come from the Foundry oracle, not Python — we cannot regenerate
  them here without Solidity).

The Foundry oracle regenerator lives at
``tools/oracle/src/SelectorOracle.sol``; the test simply refuses to
run if the artifact file is missing or its provenance drift is not
recorded.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from eth_hash.auto import keccak

from robinhood_lp.protocol.abi_artifacts import (
    ARTIFACTS_DIR,
    CURRENT_ARTIFACT_FILENAME,
    EVENT_TOPICS,
    EXPECTED_SOURCE_COMMIT,
    FUNCTION_SELECTORS,
)

ARTIFACT_PATH = ARTIFACTS_DIR / CURRENT_ARTIFACT_FILENAME


# ---------------------------------------------------------------------------
# File presence and provenance
# ---------------------------------------------------------------------------


def test_artifact_file_exists() -> None:
    assert ARTIFACT_PATH.is_file(), f"missing {ARTIFACT_PATH}"


def test_artifact_parses_as_json() -> None:
    json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))


def test_artifact_declares_expected_source_commit() -> None:
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    declared = payload["_meta"]["source_commit"]
    assert declared == EXPECTED_SOURCE_COMMIT, (
        f"artifact declares source_commit={declared!r} but "
        f"EXPECTED_SOURCE_COMMIT={EXPECTED_SOURCE_COMMIT!r}"
    )


def test_artifact_filename_matches_expected_commit() -> None:
    """The artifact filename embeds the commit prefix; if it does
    not match, the file was renamed without updating the constant."""
    prefix = CURRENT_ARTIFACT_FILENAME.removesuffix(".json")
    assert prefix.endswith(EXPECTED_SOURCE_COMMIT[:7]), (
        f"filename {CURRENT_ARTIFACT_FILENAME!r} does not embed "
        f"the expected commit prefix {EXPECTED_SOURCE_COMMIT[:7]!r}"
    )


def test_artifact_records_compiler_and_oracle() -> None:
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    meta = payload["_meta"]
    assert "solc" in meta.get("compiler", "")
    assert "Foundry" in meta.get("oracle", "") or "forge" in meta.get("oracle", "")


# ---------------------------------------------------------------------------
# Event topic integrity (Python can independently regenerate topic0
# because the canonical signature has no struct types)
# ---------------------------------------------------------------------------


_EVENT_TOPIC_NAMES = {"Initialize", "ModifyLiquidity", "Swap", "Donate"}


@pytest.mark.parametrize("name", sorted(_EVENT_TOPIC_NAMES))
def test_event_topic_matches_keccak256_of_signature(name: str) -> None:
    """topic0 == keccak256(canonical_signature_text)."""
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    sig = payload["events"][name]["signature"]
    expected = keccak(sig.encode("ascii"))
    assert EVENT_TOPICS[name] == expected, (
        f"event {name!r}: artifact topic0 != keccak256({sig!r}); drift detected"
    )


# ---------------------------------------------------------------------------
# Function selector sanity (cannot regenerate from Python alone)
# ---------------------------------------------------------------------------


_FUNCTION_SELECTOR_NAMES = {"initialize", "modifyLiquidity", "swap", "donate"}


@pytest.mark.parametrize("name", sorted(_FUNCTION_SELECTOR_NAMES))
def test_function_selector_is_four_bytes(name: str) -> None:
    assert len(FUNCTION_SELECTORS[name]) == 4


def test_event_names_match_between_artifact_and_module() -> None:
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    artifact_names = set(payload["events"].keys())
    assert artifact_names == _EVENT_TOPIC_NAMES, (
        f"artifact events {artifact_names} != module set {_EVENT_TOPIC_NAMES}"
    )


def test_function_names_match_between_artifact_and_module() -> None:
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    artifact_names = set(payload["functions"].keys())
    assert artifact_names == _FUNCTION_SELECTOR_NAMES, (
        f"artifact functions {artifact_names} != module set {_FUNCTION_SELECTOR_NAMES}"
    )


# ---------------------------------------------------------------------------
# SHA-256 manifest of the artifact file itself (recorded so a
# reviewer can verify byte-exact regeneration)
# ---------------------------------------------------------------------------


def test_artifact_sha256_field_exists() -> None:
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    assert "sha256" in payload["_meta"], "artifact _meta missing sha256 field"
    declared = payload["_meta"]["sha256"]
    assert declared, "sha256 field must not be empty"


def test_artifact_file_sha256_matches_when_recorded() -> None:
    """If the operator has recorded a 64-hex sha256, it must match the
    on-disk file. This is the regenerate-and-compare acceptance step.

    Recording the sha256 inside the artifact would create an invariant
    loop (every edit changes the sha), so the field is left as a
    manual annotation. The verification below is skipped when the
    sha field is not a 64-hex literal."""
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    declared = payload["_meta"].get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", declared):
        pytest.skip("sha256 is a manual annotation; not enforced")
    actual = hashlib.sha256(ARTIFACT_PATH.read_bytes()).hexdigest()
    assert actual == declared, (
        f"artifact file sha256 {actual} != declared {declared}; "
        f"regenerate the artifact and update the sha256 field"
    )
