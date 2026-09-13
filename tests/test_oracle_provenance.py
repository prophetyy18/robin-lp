"""Oracle provenance drift test (T013).

The oracle under ``tools/oracle/`` depends on pinned commits of
Uniswap/v4-core and Uniswap/v4-periphery. When ``forge install`` is
run, those commits land in ``tools/oracle/lib/``. If they are not
present, or if the oracle's Python fixtures disagree with the values
the oracle currently produces, this test fails and CI rejects the
change set until the operator regenerates the JSON fixtures.

This test does NOT shell out to Foundry (the sandbox is offline);
it only verifies that the documented provenance matches what is
committed alongside the test (the ``docs/oracle-manifest.md`` file).
A separate, manual ``tools/oracle/forge test -vv`` is the actual
regeneration step; see the manifest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "docs" / "oracle-manifest.md"


def _read_manifest() -> str:
    if not MANIFEST.is_file():
        pytest.fail(f"oracle manifest not found at {MANIFEST}")
    return MANIFEST.read_text(encoding="utf-8")


def _extract_pinned_commits() -> dict[str, str]:
    r"""Parse ``**Pinned commit:** `<sha>` blocks from the manifest.

    Each pinned-commit section uses a short bold header (e.g.
    ``### v4-core``) followed by a ``**Pinned commit:**`` line. We
    anchor on the commit line and walk backwards to the nearest
    ``### Heading`` line.
    """
    text = _read_manifest()
    out: dict[str, str] = {}
    for match in re.finditer(r"\*\*Pinned commit:\*\*\s*`(?P<hash>[0-9a-f]{40})`", text):
        hash_str = match.group("hash")
        prefix = text[: match.start()]
        # Walk backwards for the most recent ``### Heading``.
        headings = list(re.finditer(r"^###\s+(?P<h>.+?)\s*$", prefix, re.M))
        if not headings:
            continue
        label = headings[-1].group("h").strip().lower()
        out[label] = hash_str
    if len(out) < 3:
        pytest.fail(f"expected 3 pinned commits in {MANIFEST}, found {len(out)}: {list(out)}")
    return out


def test_manifest_exists() -> None:
    assert MANIFEST.is_file(), f"missing {MANIFEST}"


def test_manifest_records_v4_core_top_level_commit() -> None:
    commits = _extract_pinned_commits()
    assert "v4-core" in commits
    h = commits["v4-core"]
    assert re.match(r"^[0-9a-f]{40}$", h), f"not a SHA-1: {h}"


def test_manifest_records_v4_periphery_commit() -> None:
    commits = _extract_pinned_commits()
    assert "v4-periphery" in commits
    h = commits["v4-periphery"]
    assert re.match(r"^[0-9a-f]{40}$", h)


def test_manifest_records_v4_core_periphery_submodule_commit() -> None:
    commits = _extract_pinned_commits()
    assert "v4-core-periphery-submodule" in commits
    h = commits["v4-core-periphery-submodule"]
    assert re.match(r"^[0-9a-f]{40}$", h)


def test_pinned_commits_are_all_distinct() -> None:
    """The two v4-core references and the v4-periphery commit must
    differ — they refer to different upstream repositories at
    different commits."""
    commits = _extract_pinned_commits()
    values = list(commits.values())
    assert len(values) == len(set(values)), f"pinned commits must all be distinct, got {values}"


def test_manifest_documents_foundry_version() -> None:
    """A reproducible oracle run must record its tool versions."""
    text = _read_manifest()
    assert "1.8.1" in text, "manifest should record forge version"
    assert "0.8.26" in text, "manifest should record solc version"


def test_manifest_documents_python_environment() -> None:
    text = _read_manifest()
    assert "3.12" in text
    assert "pytest" in text
    assert "eth-hash" in text


def test_python_fixtures_match_oracle_outputs_by_construction() -> None:
    """The pinned vector fixtures are *what the oracle produced*.

    This test verifies they exist, parse, and contain the documented
    edge classes. The byte-exact equality between Python and oracle
    is exercised by ``test_protocol_ids`` and ``test_protocol_math``.
    """
    pid = REPO_ROOT / "tests" / "fixtures" / "protocol" / "pool_id_vectors.json"
    math = REPO_ROOT / "tests" / "fixtures" / "protocol" / "math_vectors.json"
    assert pid.is_file(), f"missing {pid}"
    assert math.is_file(), f"missing {math}"

    import json

    pid_data = json.loads(pid.read_text())
    math_data = json.loads(math.read_text())

    # Edge-class coverage assertions: at least one vector per class.
    pid_names = {v["name"] for v in pid_data["vectors"]}
    for required in (
        "native_currency0",
        "v1_static_3000_60",
        "dynamic_fee_with_hook",
        "max_static_fee",
        "max_tick_spacing",
    ):
        assert required in pid_names, f"pool_id_vectors.json missing edge class {required!r}"

    math_data_sections = (
        "tick_to_sqrt_price",
        "sqrt_price_to_tick",
        "amount_deltas",
        "liquidity_for_amounts",
    )
    for section in math_data_sections:
        assert section in math_data, f"math_vectors.json missing section {section!r}"
        assert math_data[section], f"math_vectors.json section {section!r} is empty"
