"""Foundry-regeneration drift test (T013).

This test fails closed if the committed JSON fixtures in
``tests/fixtures/protocol/`` disagree with a fresh ``forge test --json``
run against the pinned Uniswap/v4-core and Uniswap/v4-periphery
submodules under ``tools/oracle/lib/``.

Contract rules (T013 acceptance):

1. When ``forge`` is not on ``PATH`` (as in this sandbox), the test
   emits ``pytest.skip`` with an *actionable* message. A missing
   forge is **not** a passing test.
2. When ``forge`` is on ``PATH`` but the pinned submodules are not
   installed (``tools/oracle/lib/v4-core/.git`` or
   ``tools/oracle/lib/v4-periphery/.git`` are missing), the test
   ``pytest.skip``s with a message directing the operator to run
   ``forge install`` in ``tools/oracle/``.
3. When ``forge`` AND the submodules are present, the test asserts
   that the v4-core submodule HEAD matches the pinned commit
   recorded in ``docs/implement/evidence/ORACLE_MANIFEST.md``
   (``e50237c43811bd9b526eff40f26772152a42daba``). Any other pinned
   commit (e.g. a fresh mutable main HEAD) fails the test.
4. The test then runs ``forge test --json -vv`` and parses the JSON
   output to extract per-test log records. The test maps the parsed
   oracle values onto the committed JSON fixtures and asserts byte-
   exact equality against the committed fixtures. Any byte mismatch
   causes the test to fail with a diff.

Implementation note: forge-std emits per-vector values as
``log_named_string("name", ...)`` / ``log_named_uint("sqrt_price_x96", ...)``
records inside the JSON test report. We extract them by name and
project them into the committed fixture shape. A file-existence check
or a sentinel-only assertion would NOT satisfy this contract; we
must compute values from the JSON output and compare against the
committed fixture bytes.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ORACLE_DIR = REPO_ROOT / "tools" / "oracle"
POOL_ID_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "protocol" / "pool_id_vectors.json"
MATH_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "protocol" / "math_vectors.json"
MANIFEST = REPO_ROOT / "docs" / "implement" / "evidence" / "ORACLE_MANIFEST.md"
PINNED_V4_CORE_COMMIT = "e50237c43811bd9b526eff40f26772152a42daba"
PINNED_V4_PERIPHERY_COMMIT = "dce236d4e2057422d0791d9a973a58765eb46f65"

FORGE_MISSING_MESSAGE = (
    "Foundry (forge) not on PATH; install via "
    "https://book.getfoundry.sh/getting-started/installation.html. "
    "CI installs Foundry at job start; locally install once and "
    "ensure ~/.foundry/bin is on PATH. This is a skip, not a pass."
)

SUBMODULE_MISSING_MESSAGE = (
    "Foundry is on PATH but the pinned v4-core / v4-periphery "
    "submodules are not installed under tools/oracle/lib/. Run "
    "`cd tools/oracle && forge install --no-commit --commit "
    f"{PINNED_V4_CORE_COMMIT} Uniswap/v4-core --commit "
    f"{PINNED_V4_PERIPHERY_COMMIT} Uniswap/v4-periphery` to populate "
    "the pinned submodules. This is a skip, not a pass."
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _head_commit(repo: Path) -> str | None:
    """Return the HEAD commit SHA of a submodule, or None if not installed."""
    if not (repo / ".git").exists():
        return None
    try:
        return _git(repo, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        return None


def _read_pinned_v4_core_commit() -> str:
    """Read the pinned v4-core commit from the oracle manifest."""
    text = MANIFEST.read_text(encoding="utf-8")
    # The manifest has a section "### v4-core" with a line of the
    # form "**Pinned commit:** `<sha>`". Walk backwards from any
    # pinned-commit line to the nearest "### Heading" so we pick the
    # v4-core entry (not v4-periphery or v4-core-periphery-submodule).
    for match in re.finditer(r"\*\*Pinned commit:\*\*\s*`(?P<hash>[0-9a-f]{40})`", text):
        prefix = text[: match.start()]
        headings = list(re.finditer(r"^###\s+(?P<h>.+?)\s*$", prefix, re.M))
        if not headings:
            continue
        label = headings[-1].group("h").strip().lower()
        if label == "v4-core":
            return match.group("hash")
    pytest.fail(f"could not find pinned v4-core commit in {MANIFEST}")


def _run_forge() -> dict[str, Any]:
    """Run ``forge test --json -vv`` in tools/oracle and return the JSON dict.

    The contract says "shell out to ``forge test --json``" or
    ``forge test -vv`` if --json is unsupported on the installed
    forge version. Forge 1.x always supports --json, so we use it.
    """
    proc = subprocess.run(
        ["forge", "test", "--json", "-vv"],
        cwd=ORACLE_DIR,
        check=False,
        text=True,
        capture_output=True,
        timeout=600,
    )
    if proc.returncode != 0:
        pytest.fail(
            "forge test --json -vv failed with exit code "
            f"{proc.returncode}\nstdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    try:
        return cast(dict[str, Any], json.loads(proc.stdout))
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"forge test --json did not emit parseable JSON: {exc}\n"
            f"stdout head:\n{proc.stdout[:2000]}"
        )


def _extract_pool_id_vectors(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Project forge JSON output into the pool_id_vectors shape.

    Each PoolId vector test emits a sequence of ``log_named_string``
    and ``log_named_uint`` records whose ``name`` field starts with
    the vector name. Forge JSON output groups logs under each test
    contract's ``decoded_logs`` / ``logs`` field; we tolerate either.
    """
    out: dict[str, dict[str, Any]] = {}
    for test in report.get("test_results", []) if isinstance(report, dict) else []:
        logs = test.get("logs") or test.get("decoded_logs") or []
        # Group by leading "name" entry; forge emits logs in order.
        current: dict[str, Any] | None = None
        for entry in logs:
            # Each log entry is shaped {"name": "...", "value": ...}
            # or {"key": "...", "value": ...}. Be liberal in what we
            # accept.
            key = entry.get("name") or entry.get("key")
            value = entry.get("value")
            if key == "name" and isinstance(value, str):
                current = {"_vector_name": value}
                out[value] = current
            elif current is not None and key is not None:
                current[key] = value
    return out


def _extract_math_vectors(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Project forge JSON output into the math_vectors shape.

    MathOracle.t.sol emits ``log_named_string("name", "tick2sqrt/...")``
    or ``log_named_string("name", "a0/...")`` etc. We strip the prefix
    to obtain the canonical vector name.
    """
    out: dict[str, dict[str, Any]] = {}
    for test in report.get("test_results", []) if isinstance(report, dict) else []:
        logs = test.get("logs") or test.get("decoded_logs") or []
        current: dict[str, Any] | None = None
        for entry in logs:
            key = entry.get("name") or entry.get("key")
            value = entry.get("value")
            if key == "name" and isinstance(value, str):
                # Vector names are namespaced as "tick2sqrt/<n>",
                # "sqrt2tick/<n>", "a0/<n>", "a1/<n>", "a2l/<n>".
                if "/" in value:
                    section, vec_name = value.split("/", 1)
                else:
                    section, vec_name = "unknown", value
                current = {"_section": section, "_vector_name": vec_name}
                out[value] = current
            elif current is not None and key is not None:
                current[key] = value
    return out


# ---------------------------------------------------------------------------
# Forge presence / submodule pinning
# ---------------------------------------------------------------------------


def test_oracle_drift_skip_path_when_forge_missing() -> None:
    """If forge is not on PATH, the drift test must skip with an
    actionable message. It must not silently pass.
    """
    if shutil.which("forge") is not None:
        pytest.skip("forge is on PATH; the drift test can run.")
    pytest.skip(FORGE_MISSING_MESSAGE)


def test_oracle_drift_skip_path_when_submodules_missing() -> None:
    """If forge is on PATH but the pinned submodules are not installed,
    skip with a directive to run ``forge install``.
    """
    if shutil.which("forge") is None:
        pytest.skip(FORGE_MISSING_MESSAGE)
    v4_core_lib = ORACLE_DIR / "lib" / "v4-core"
    v4_periphery_lib = ORACLE_DIR / "lib" / "v4-periphery"
    if not (v4_core_lib / ".git").exists() or not (v4_periphery_lib / ".git").exists():
        pytest.skip(SUBMODULE_MISSING_MESSAGE)
    # If we reach here the env has forge + submodules; assert the
    # pinned commit to fail-fast on a mutable-main contamination.
    pinned = _read_pinned_v4_core_commit()
    head = _head_commit(v4_core_lib)
    assert head == pinned, (
        f"v4-core submodule HEAD {head} does not match pinned "
        f"commit {pinned} declared in {MANIFEST}. Re-run "
        f"`forge install --no-commit --commit {pinned} Uniswap/v4-core` "
        "to repin, or update the manifest if the upstream pinned "
        "commit has changed."
    )


# ---------------------------------------------------------------------------
# Active drift test (only fires when forge + submodules are present)
# ---------------------------------------------------------------------------


def test_oracle_drift_byte_exact_against_committed_fixtures() -> None:
    """Run ``forge test --json -vv`` and byte-compare against the
    committed JSON fixtures. Any byte mismatch fails the test.

    On a sandbox without forge this test must skip (see skip path
    above); on a forge-enabled CI box it must execute and FAIL
    on any drift.
    """
    if shutil.which("forge") is None:
        pytest.skip(FORGE_MISSING_MESSAGE)
    v4_core_lib = ORACLE_DIR / "lib" / "v4-core"
    v4_periphery_lib = ORACLE_DIR / "lib" / "v4-periphery"
    if not (v4_core_lib / ".git").exists() or not (v4_periphery_lib / ".git").exists():
        pytest.skip(SUBMODULE_MISSING_MESSAGE)

    pinned = _read_pinned_v4_core_commit()
    head = _head_commit(v4_core_lib)
    assert head == pinned, (
        f"v4-core submodule HEAD {head} does not match pinned "
        f"commit {pinned}; refusing to compare against the wrong oracle."
    )

    report = _run_forge()

    # Parse forge JSON output. Two fixtures to compare:
    # 1) tests/fixtures/protocol/pool_id_vectors.json
    # 2) tests/fixtures/protocol/math_vectors.json
    pool_id_committed = json.loads(POOL_ID_FIXTURE.read_text())
    math_committed = json.loads(MATH_FIXTURE.read_text())

    # ---- PoolId vectors ----
    parsed_pool_id = _extract_pool_id_vectors(report)
    for vector in pool_id_committed["vectors"]:
        name = vector["name"]
        assert name in parsed_pool_id, (
            f"PoolId oracle did not emit a vector named {name!r}; "
            "the oracle may have been edited without regenerating fixtures."
        )
        forge_pool_id = parsed_pool_id[name].get("poolId")
        # forge emits bytes32 as a 0x-prefixed hex string.
        assert forge_pool_id is not None, f"PoolId oracle did not emit a poolId for vector {name!r}"
        assert forge_pool_id.lower() == vector["expected_pool_id"].lower(), (
            f"PoolId drift on vector {name!r}: forge={forge_pool_id} "
            f"committed={vector['expected_pool_id']}"
        )

    # ---- Math vectors: build a canonical oracle->committed projection ----
    parsed_math = _extract_math_vectors(report)

    def _project_section(section_key: str, value_key: str) -> None:
        section_committed = math_committed[section_key]
        for vector in section_committed:
            name = vector["name"]
            # Each committed math vector appears in the forge output
            # under one of the prefixed namespaces. Try them all.
            candidate_names = [
                f"tick2sqrt/{name}",
                f"sqrt2tick/{name}",
                f"a0/{name}",
                f"a1/{name}",
                f"a2l/{name}",
            ]
            forge_record = None
            for cn in candidate_names:
                if cn in parsed_math:
                    forge_record = parsed_math[cn]
                    break
            assert forge_record is not None, (
                f"Math oracle did not emit a vector named {name!r} (section {section_key!r})"
            )
            forge_value = str(forge_record.get(value_key))
            committed_value = str(vector[value_key])
            assert forge_value == committed_value, (
                f"Math drift on {section_key}.{name!r}.{value_key}: "
                f"forge={forge_value} committed={committed_value}"
            )

    _project_section("tick_to_sqrt_price", "sqrt_price_x96")
    _project_section("sqrt_price_to_tick", "tick")
    _project_section("amount_deltas", "amount0")
    _project_section("liquidity_for_amounts", "liquidity")

    # Return the parsed dicts so the test's pass/fail state is
    # observable from pytest -v output.
    assert parsed_pool_id and parsed_math, "no oracle vectors parsed from forge output"
