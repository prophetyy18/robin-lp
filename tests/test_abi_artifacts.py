"""Drift test for pinned V4 PoolManager artifacts (T021).

T021 acceptance: "regenerate-and-compare test detects any ABI/topic/
selector drift". This module verifies the pinned JSON artifact:

- file exists and parses;
- declared source commit matches the constant;
- every event has a 32-byte topic0 (must round-trip through keccak256
  of the canonical signature);
- every function has a 4-byte selector (the canonical selectors
  come from the Foundry oracle, not Python — we cannot regenerate
  them here without Solidity);
- the ``_meta.sha256`` field is a hard assertion of the file's
  SHA-256 (no ``pytest.skip``; per scope addition #4);
- StateView ABI is present alongside PoolManager (per scope
  addition #1);
- when ``forge`` is on PATH, the artifact byte-matches the
  output of the Foundry SelectorOracle regenerator (per scope
  addition #2; ``fails closed`` — no ``pytest.skip``).

The Foundry oracle regenerator lives at
``tools/oracle/src/SelectorOracle.sol``; the helper is
``robinhood_lp.tools.regenerate_artifact``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

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

ORACLE_DIR = Path(__file__).resolve().parents[1] / "tools" / "oracle"

# Expected contract names covered by the artifact. StateView is the
# T024/T042/T051 read-side oracle and must be present alongside
# PoolManager (T021 scope addition #1).
_EXPECTED_CONTRACTS = {"PoolManager", "StateView"}
_EXPECTED_EVENT_NAMES = {"Initialize", "ModifyLiquidity", "Swap", "Donate"}
_EXPECTED_POOLMANAGER_FUNCTIONS = {
    "initialize",
    "modifyLiquidity",
    "swap",
    "donate",
}
_EXPECTED_STATEVIEW_FUNCTIONS = {
    "getSlot0",
    "getTickInfo",
    "getTickLiquidity",
    "getTickFeeGrowthOutside",
    "getFeeGrowthGlobals",
    "getLiquidity",
    "getTickBitmap",
    "getPositionInfo_owner",
    "getPositionInfo_id",
    "getPositionLiquidity",
    "getFeeGrowthInside",
}

#: Cartesion-free mapping: which function names live under which
#: contract in the artifact. Used by ``test_function_selector_is_four_bytes``.
_FUNCTION_NAMES_BY_CONTRACT: dict[str, set[str]] = {
    "PoolManager": _EXPECTED_POOLMANAGER_FUNCTIONS,
    "StateView": _EXPECTED_STATEVIEW_FUNCTIONS,
}

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


def test_artifact_records_source_retrieval_timestamp() -> None:
    """Scope addition #5: source-retrieval timestamp must be present."""
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    meta = payload["_meta"]
    assert meta.get("source_retrieval_time"), "missing source_retrieval_time"
    assert meta.get("source_retrieval_method"), "missing source_retrieval_method"


def test_artifact_records_license_with_citation() -> None:
    """Scope addition #5: license must be verified against the pinned v4-core LICENSE."""
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    meta = payload["_meta"]
    assert "license_pool_manager" in meta, "missing license_pool_manager"
    assert "license_state_view" in meta, "missing license_state_view"
    assert "license_verification_paths" in meta, "missing license_verification_paths"
    paths = meta["license_verification_paths"]
    assert isinstance(paths, list) and paths, "license_verification_paths must be non-empty"


# ---------------------------------------------------------------------------
# Event topic integrity (Python can independently regenerate topic0
# because the canonical signature has no struct types)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_EXPECTED_EVENT_NAMES))
def test_event_topic_matches_keccak256_of_signature(name: str) -> None:
    """topic0 == keccak256(canonical_signature_text)."""
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    sig = payload["events"][name]["signature"]
    expected = keccak(sig.encode("ascii"))
    assert EVENT_TOPICS[name] == expected, (
        f"event {name!r}: artifact topic0 != keccak256({sig!r}); drift detected"
    )


def test_event_names_match_between_artifact_and_module() -> None:
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    artifact_names = set(payload["events"].keys())
    assert artifact_names == _EXPECTED_EVENT_NAMES, (
        f"artifact events {artifact_names} != module set {_EXPECTED_EVENT_NAMES}"
    )


# ---------------------------------------------------------------------------
# Function selector sanity (cannot regenerate from Python alone; the
# Foundry oracle is the canonical source). The selectors here come
# from the loaded FUNCTION_SELECTORS map which is nested by contract.
# ---------------------------------------------------------------------------


def test_function_contracts_match_expected_set() -> None:
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    artifact_contracts = set(payload["functions"].keys())
    assert artifact_contracts == _EXPECTED_CONTRACTS, (
        f"artifact function contracts {artifact_contracts} != expected set {_EXPECTED_CONTRACTS}"
    )


def test_function_contract_names_match_module() -> None:
    artifact_contracts = set(FUNCTION_SELECTORS.keys())
    assert artifact_contracts == _EXPECTED_CONTRACTS, (
        f"module FUNCTION_SELECTORS contracts {artifact_contracts} != "
        f"expected set {_EXPECTED_CONTRACTS}"
    )


def test_poolmanager_function_names_match_expected_set() -> None:
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    artifact_names = set(payload["functions"]["PoolManager"].keys())
    assert artifact_names == _EXPECTED_POOLMANAGER_FUNCTIONS, (
        f"artifact PoolManager functions {artifact_names} != "
        f"expected set {_EXPECTED_POOLMANAGER_FUNCTIONS}"
    )


def test_stateview_function_names_match_expected_set() -> None:
    """Scope addition #1: StateView ABI coverage alongside PoolManager."""
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    artifact_names = set(payload["functions"]["StateView"].keys())
    assert artifact_names == _EXPECTED_STATEVIEW_FUNCTIONS, (
        f"artifact StateView functions {artifact_names} != "
        f"expected set {_EXPECTED_STATEVIEW_FUNCTIONS}"
    )


@pytest.mark.parametrize(
    "contract,fn_name",
    sorted((c, f) for c, fns in _FUNCTION_NAMES_BY_CONTRACT.items() for f in fns),
)
def test_function_selector_is_four_bytes(contract: str, fn_name: str) -> None:
    assert contract in FUNCTION_SELECTORS, f"missing contract {contract!r}"
    assert fn_name in FUNCTION_SELECTORS[contract], f"missing {contract}.{fn_name!r}"
    assert len(FUNCTION_SELECTORS[contract][fn_name]) == 4, (
        f"{contract}.{fn_name}: selector must be 4 bytes, "
        f"got {len(FUNCTION_SELECTORS[contract][fn_name])}"
    )


# ---------------------------------------------------------------------------
# SHA-256 manifest — HARD ASSERTION (no pytest.skip; per scope addition #4).
# ---------------------------------------------------------------------------


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def test_artifact_sha256_field_is_well_formed_hex() -> None:
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    declared = payload["_meta"].get("sha256", "")
    assert _SHA256_RE.fullmatch(declared), (
        f"artifact _meta.sha256={declared!r} is not a 64-char lowercase hex string"
    )


def test_artifact_file_sha256_matches_declared() -> None:
    """The artifact's ``_meta.sha256`` MUST equal the SHA-256 of the
    canonical artifact body (with the field set to a 64-zero
    placeholder). A drift fails the test (no pytest.skip; scope
    addition #4).

    The sha256 is the hash of the *meaningful* body content, NOT the
    raw file bytes; that way the field does not include its own
    value and the hash is deterministic.
    """
    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    declared = payload["_meta"].get("sha256", "")
    assert _SHA256_RE.fullmatch(declared), (
        f"artifact _meta.sha256={declared!r} is not a 64-char lowercase hex string; "
        f"regenerate the artifact with `python -m robinhood_lp.tools.regenerate_artifact`"
    )

    # Canonical-body sha256: replace the sha256 field with zeros, then
    # serialize with sorted keys / 2-space indent / trailing newline.
    body = json.loads(json.dumps(payload))  # deep copy
    body["_meta"]["sha256"] = "0" * 64
    body_bytes = (json.dumps(body, sort_keys=True, indent=2) + "\n").encode("utf-8")
    actual = hashlib.sha256(body_bytes).hexdigest()
    assert actual == declared, (
        f"artifact body sha256 {actual} != declared {declared}; "
        f"regenerate the artifact with `python -m robinhood_lp.tools.regenerate_artifact`"
    )


# ---------------------------------------------------------------------------
# Regenerate-and-compare against the Foundry SelectorOracle.
#
# Scope addition #2: the Python side byte-compares and fails closed
# (no pytest.skip). If `forge` is not on PATH the test fails loudly
# with a clear remediation message rather than silently passing.
# ---------------------------------------------------------------------------


def _require_forge() -> str:
    """Locate the ``forge`` binary.

    Per the T021 attempt-1 remediation the canonical user-level install
    lives under ``$HOME/.foundry/bin/forge``; we try that path first
    before falling back to ``shutil.which`` on ``PATH``. We do NOT
    ``pytest.skip`` when forge is missing — the test must fail closed
    with a clear remediation message.
    """
    home = os.environ.get("HOME") or str(Path.home())
    candidates = [str(Path(home) / ".foundry" / "bin" / "forge")]
    on_path = shutil.which("forge")
    if on_path is not None:
        candidates.append(on_path)
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    pytest.fail(
        "`forge` not found; tried "
        + ", ".join(repr(c) for c in candidates)
        + ". Install Foundry (https://getfoundry.sh) and re-run pytest."
    )
    # Unreachable: pytest.fail raises; the return keeps the type checker happy.
    raise AssertionError("unreachable")


def _run_forge_oracle(forge: str) -> dict[str, object]:
    """Run the Foundry SelectorOracle regenerator.

    Sequence (T021 attempt-1 remediation):

    1. ``cd tools/oracle && forge build --skip test/MathOracle.t.sol
       --skip 'test/**'`` — pre-warm the artifact cache, scoping
       compilation to the SelectorOracle sources only. We pass
       ``check=False`` and ignore the exit code so the test still
       proceeds when the build surfaces pre-existing, out-of-scope
       warnings.
    2. ``cd tools/oracle && forge test --match-path
       'test/SelectorOracle.t.sol' --json -vv`` — runs only the
       T013/T021 SelectorOracleTest contract and emits parseable
       JSON containing the canonical event topic0 and function
       selector bytes that the artifact pins.

    Both invocations are fail-closed: a missing forge binary fails
    in ``_require_forge``; a non-zero ``forge test`` exit, malformed
    JSON, or selector/topic byte mismatch all raise ``pytest.fail``
    with remediation guidance.
    """
    # Step 1: best-effort pre-build, scoped to SelectorOracle sources.
    build_proc = subprocess.run(
        [
            forge,
            "build",
            "--skip",
            "test/MathOracle.t.sol",
            "--skip",
            "test/**",
        ],
        cwd=str(ORACLE_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    if build_proc.returncode != 0:
        # Surface the failure but continue to forge test, since
        # forge test will re-attempt compilation with --match-path.
        # If the regeneration step also fails the JSON parse error
        # below will report the underlying problem.
        pass

    # Step 2: run only the SelectorOracleTest contract.
    test_proc = subprocess.run(
        [
            forge,
            "test",
            "--match-path",
            "test/SelectorOracle.t.sol",
            "--json",
            "-vv",
        ],
        cwd=str(ORACLE_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    if test_proc.returncode != 0:
        pytest.fail(
            f"forge test failed (rc={test_proc.returncode}); "
            f"pre-build rc={build_proc.returncode}.\n"
            f"stdout:\n{test_proc.stdout[:2000]}\n"
            f"stderr:\n{test_proc.stderr[:2000]}"
        )
    try:
        result: dict[str, object] = json.loads(test_proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"forge output is not valid JSON: {exc}; stdout head: {test_proc.stdout[:500]}")
    return result


def _parse_decoded_logs(decoded_logs: list[str]) -> dict[str, str]:
    """Parse forge-std decoded logs into ``{key: value}``.

    Each line is ``key: value`` (e.g. ``topic0: 0xdd46...``,
    ``selector_bytes32: 0x6276cbbe...``). Values may contain colons
    (e.g. ``contract: PoolManager`` has none; but the convention
    splits on the first colon to preserve trailing colons in
    values).
    """
    parsed: dict[str, str] = {}
    for line in decoded_logs:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        parsed[key.strip()] = value.strip()
    return parsed


def _selector_from_bytes32(padded: str) -> str:
    """Extract the leading 8 hex chars (the bytes4 selector) from a
    bytes32-padded selector value emitted by ``forge-std``'s
    ``log_named_bytes32``. The bytes4 occupies the most-significant
    bytes (the leading 8 hex chars)."""
    s = padded.lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 64:
        raise ValueError(f"expected 64-char hex bytes32, got {len(s)} chars: {padded!r}")
    return "0x" + s[:8]


def _build_regenerated_artifact(forge_json: dict[str, object]) -> dict[str, object]:
    """Reconstruct a fresh artifact from forge's JSON output.

    Topic0 and selector values come from the oracle. Signatures,
    license, compiler info, etc. are inherited from the checked-in
    artifact so that the regenerated file is byte-comparable.
    """
    checked_in = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))

    # Locate the SelectorOracleTest contract block.
    contract_block = None
    for k, v in forge_json.items():
        if k.endswith(":SelectorOracleTest"):
            contract_block = v
            break
    if contract_block is None:
        pytest.fail("forge JSON does not contain SelectorOracleTest results")
    assert isinstance(contract_block, dict)
    results_any: object = contract_block["test_results"]
    assert isinstance(results_any, dict)
    results: dict[str, object] = results_any

    events: dict[str, dict[str, str]] = {}
    functions: dict[str, dict[str, dict[str, object]]] = {
        "PoolManager": {},
        "StateView": {},
    }

    for test_name, test_payload in results.items():
        if not isinstance(test_payload, dict):
            continue
        decoded = _parse_decoded_logs([str(x) for x in test_payload.get("decoded_logs") or []])
        kind = decoded.get("kind")
        contract = decoded.get("contract")
        name = decoded.get("name")
        if not kind or not contract or not name:
            continue
        if kind == "event":
            topic0 = decoded.get("topic0")
            if not topic0:
                pytest.fail(f"{test_name}: missing topic0 in decoded logs")
            # Inherit signature from checked-in artifact.
            signature = checked_in["events"][name]["signature"]
            events[name] = {"signature": signature, "topic0": topic0}
        elif kind == "function":
            sel_padded = decoded.get("selector_bytes32")
            sel_uint = decoded.get("selector_uint")
            if not sel_padded or not sel_uint:
                pytest.fail(f"{test_name}: missing selector_bytes32/selector_uint in decoded logs")
            selector = _selector_from_bytes32(sel_padded)
            signature = checked_in["functions"][contract][name]["signature"]
            functions[contract][name] = {
                "signature": signature,
                "selector": selector,
                "selector_decimal": int(sel_uint),
            }
        else:
            pytest.fail(f"{test_name}: unknown kind {kind!r}")

    if set(events.keys()) != _EXPECTED_EVENT_NAMES:
        pytest.fail(
            f"regenerated events {set(events.keys())} != expected set {_EXPECTED_EVENT_NAMES}"
        )
    for contract in _EXPECTED_CONTRACTS:
        if not functions[contract]:
            pytest.fail(f"regenerated artifact missing {contract} function selectors")
    if set(functions["PoolManager"].keys()) != _EXPECTED_POOLMANAGER_FUNCTIONS:
        pytest.fail(
            f"regenerated PoolManager functions "
            f"{set(functions['PoolManager'].keys())} != "
            f"expected set {_EXPECTED_POOLMANAGER_FUNCTIONS}"
        )
    if set(functions["StateView"].keys()) != _EXPECTED_STATEVIEW_FUNCTIONS:
        pytest.fail(
            f"regenerated StateView functions "
            f"{set(functions['StateView'].keys())} != "
            f"expected set {_EXPECTED_STATEVIEW_FUNCTIONS}"
        )

    # Build a fresh artifact, inheriting static metadata from checked-in.
    # Strip _meta.sha256 — the SHA-256 is set after the file is finalized
    # so the field would otherwise create an invariant loop.
    meta = {k: v for k, v in checked_in["_meta"].items() if k != "sha256"}
    return {"_meta": meta, "events": events, "functions": functions}


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    """Render payload to canonical bytes (sorted keys, 2-space indent,
    trailing newline) so two semantically-equal artifacts compare
    equal."""
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def test_artifact_byte_matches_regenerated_oracle_output() -> None:
    """Scope addition #2: the Python side regenerates the artifact
    from forge and byte-compares against the checked-in file.

    Fails closed (no pytest.skip): if forge is missing, the oracle
    cannot run, the test fails with a clear remediation message.
    """
    forge = _require_forge()
    forge_json = _run_forge_oracle(forge)
    regenerated = _build_regenerated_artifact(forge_json)
    checked_in = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    checked_in_for_compare = {
        "_meta": {k: v for k, v in checked_in["_meta"].items() if k != "sha256"},
        "events": checked_in["events"],
        "functions": checked_in["functions"],
    }
    regenerated_bytes = _canonical_bytes(regenerated)
    checked_in_bytes = _canonical_bytes(checked_in_for_compare)
    if regenerated_bytes != checked_in_bytes:
        # Surface a compact diff: which keys/values diverged.
        diff_keys = []
        for k in sorted(set(regenerated) | set(checked_in_for_compare)):
            if regenerated.get(k) != checked_in_for_compare.get(k):
                diff_keys.append(k)
        pytest.fail(
            f"regenerated artifact (from forge) does not byte-match checked-in "
            f"artifact (after stripping _meta.sha256). "
            f"Drifted top-level keys: {diff_keys}. "
            f"Re-run `python -m robinhood_lp.tools.regenerate_artifact` "
            f"and commit the result."
        )
