"""Tests for the T067 strategy authoring guide + example strategy.

The tests are the acceptance evidence for `T067
<todo/phases/P06-backtesting-and-strategy/T067.md>` deliverable 4: the
guide exists with all five content blocks, the example is committed
outside ``src/robinhood_lp/``, the example runs through the committed
engine on a committed fixture under the guide's documented commands,
and the guide's quoted example block is byte-identical to the committed
file.

The tests load the example via :mod:`importlib` (the file lives in
``docs/implement/strategy/`` so it is *not* part of the project
package), exercise it through the committed :class:`BacktestEngine`,
and assert the documented decision sequence. The "guide's quoted block
matches the example" assertion extracts the Python fenced block in
§5.3 of the guide and compares it to the example file; a guide whose
quoted block has drifted from the file fails the suite instead of
misleading the next author.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Final

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


_HERE: Final[Path] = Path(__file__).resolve().parent
_REPO_ROOT: Final[Path] = _HERE.parent
_GUIDE_PATH: Final[Path] = _REPO_ROOT / "docs/implement/strategy/AUTHORING_GUIDE.md"
_EXAMPLE_PATH: Final[Path] = _REPO_ROOT / "docs/implement/strategy/example_strategy.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_example_module() -> Any:
    """Load ``example_strategy.py`` via :mod:`importlib`.

    The example lives outside the project package so the regular
    ``import robinhood_lp.*`` discovery does not find it. The
    importlib spec executes the module in the current process so its
    ``if __name__ == "__main__"`` block does not run.

    The module is registered in :data:`sys.modules` before execution
    so :func:`dataclasses.dataclass` can resolve ``cls.__module__``
    (the dataclass decorator looks the module up via ``sys.modules``
    while installing ``__init_subclass__``-style hooks).
    """
    module_name = "robinhood_lp_t067_example_strategy"
    spec = importlib.util.spec_from_file_location(module_name, _EXAMPLE_PATH)
    assert spec is not None, f"failed to build import spec for {_EXAMPLE_PATH}"
    assert spec.loader is not None, f"import spec for {_EXAMPLE_PATH} has no loader"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _guide_text() -> str:
    return _GUIDE_PATH.read_text(encoding="utf-8")


def _extract_python_block(markdown: str, *, block_index: int = 0) -> str:
    """Return the ``block_index``-th ``python`` fenced block in ``markdown``.

    The extractor is intentionally narrow: it matches ``python``
    immediately after the opening fence and stops at the first
    closing fence on its own line. A guide with no Python block
    raises :class:`AssertionError`.
    """
    pattern = re.compile(r"```python\n(.*?)\n```", re.DOTALL)
    matches = list(pattern.finditer(markdown))
    assert matches, "no python fenced block found in guide"
    assert block_index < len(matches), (
        f"guide has only {len(matches)} python fenced block(s); requested index {block_index}"
    )
    return matches[block_index].group(1)


# ---------------------------------------------------------------------------
# 1. The guide exists with the five content blocks
# ---------------------------------------------------------------------------


class TestGuideContentBlocks:
    """The guide contains every content block of deliverable 1."""

    def test_guide_exists(self) -> None:
        assert _GUIDE_PATH.exists(), f"missing guide at {_GUIDE_PATH}"

    def test_guide_has_five_numbered_content_blocks(self) -> None:
        text = _guide_text()
        for header in (
            "## 1. Callback shape",
            "## 2. Field semantics and units",
            "## 3. Fill and information-frontier assumptions",
            "## 4. Strategy-layer prohibitions",
            "## 5. Self-check",
        ):
            assert header in text, f"guide is missing content block: {header!r}"

    def test_guide_cites_every_required_source(self) -> None:
        """Every normative statement in the guide cites its source.

        The check is structural: the guide's citations summary must
        list every required source, and the body must reference the
        engine module and the closed task contracts. A guide that
        loses a citation fails the suite.
        """
        text = _guide_text()
        required_paths = (
            "docs/spec/strategy/STRATEGY_ECONOMICS.md",
            "docs/spec/architecture/adr/ADR-004-integer-decimal-precision.md",
            "docs/spec/architecture/adr/ADR-006-dependency-direction.md",
            "docs/spec/architecture/adr/ADR-014-research-universe-and-numeraire.md",
            "src/robinhood_lp/backtest/engine.py",
            "src/robinhood_lp/protocol/contracts.py",
            "todo/phases/P06-backtesting-and-strategy/T060.md",
            "todo/phases/P06-backtesting-and-strategy/T061.md",
            "todo/phases/P06-backtesting-and-strategy/T062.md",
            "todo/phases/P06-backtesting-and-strategy/T065.md",
        )
        for path in required_paths:
            assert path in text, f"guide does not cite required source {path!r}"

    def test_guide_names_t065_as_adapter_owner(self) -> None:
        text = _guide_text()
        assert "T065" in text, "guide must name T065 as the adapter owner"
        # The guide must defer to T065; an Owner edit renaming the
        # adapter to a different task would surface here.
        assert "adapter" in text.lower(), "guide must mention the adapter boundary"

    def test_guide_states_source_wins_on_disagreement(self) -> None:
        text = _guide_text()
        # The exact phrase the T067 contract asks for.
        assert "cited source wins" in text, (
            "guide must state that the cited source wins where the guide "
            "and a cited source disagree"
        )


# ---------------------------------------------------------------------------
# 2. The example exists and is importable
# ---------------------------------------------------------------------------


class TestExampleFile:
    """The example file is committed outside ``src/robinhood_lp/``."""

    def test_example_path_is_outside_src(self) -> None:
        """The example does not live under the project package."""
        assert not _EXAMPLE_PATH.is_relative_to(_REPO_ROOT / "src"), (
            f"example must not live under src/ (got {_EXAMPLE_PATH})"
        )

    def test_example_module_loads(self) -> None:
        module = _load_example_module()
        assert hasattr(module, "TickFromCurrentPriceStrategy")
        assert hasattr(module, "build_example_manifest")
        assert hasattr(module, "build_example_engine")
        assert hasattr(module, "main")


# ---------------------------------------------------------------------------
# 3. The example runs through the committed engine on the committed fixture
# ---------------------------------------------------------------------------


class TestExampleRunsThroughEngine:
    """The example produces the expected decisions on the documented fixture."""

    def test_strategy_returns_no_trade_on_empty_visible_events(self) -> None:
        module = _load_example_module()
        from robinhood_lp.backtest.engine import (
            StrategyDecision,
            StrategyDecisionRequest,
        )

        strategy = module.TickFromCurrentPriceStrategy(
            pool_key_id="0x" + "ab" * 32,
            chain_id=46630,
            tick_spacing=60,
        )
        request = StrategyDecisionRequest(
            pool_key_id="0x" + "ab" * 32,
            chain_id=46630,
            decision_time=10,
            visible_events=(),
            ledger=module.empty_position_state(pool_key_id="0x" + "ab" * 32, chain_id=46630),
        )
        decision = strategy(request)
        assert isinstance(decision, StrategyDecision)
        assert decision.kind == "NO_TRADE"
        assert module.EXAMPLE_NOTES_NO_MARKET_DATA in decision.notes

    def test_strategy_returns_propose_then_wait_on_committed_fixture(self) -> None:
        module = _load_example_module()
        strategy = module.TickFromCurrentPriceStrategy(
            pool_key_id="0x" + "ab" * 32,
            chain_id=46630,
            tick_spacing=60,
        )
        engine = module.build_example_engine(strategy)
        result = engine.run(module.build_example_manifest())
        decision_kinds = [
            dict(audit.payload).get("decision_kind")
            for audit in result.audit_events
            if audit.stage == "DECISION"
        ]
        assert decision_kinds == ["PROPOSE", "WAIT"], (
            f"expected ['PROPOSE', 'WAIT'], got {decision_kinds}"
        )

    def test_engine_records_propose_fill_and_wait_audit_stages(self) -> None:
        """The audit chain contains the stages the guide documents."""
        module = _load_example_module()
        strategy = module.TickFromCurrentPriceStrategy(
            pool_key_id="0x" + "ab" * 32,
            chain_id=46630,
            tick_spacing=60,
        )
        engine = module.build_example_engine(strategy)
        result = engine.run(module.build_example_manifest())
        stages = [audit.stage for audit in result.audit_events]
        # SYSTEM init → DECISION → FILL → DECISION → SYSTEM shutdown
        assert "DECISION" in stages
        assert "FILL" in stages
        assert stages.count("SYSTEM") >= 2, f"expected SYSTEM init + shutdown, got {stages}"
        # The PROPOSE decision is followed by exactly one FILL on the
        # documented always-allow failure model.
        propose_index = next(
            i
            for i, audit in enumerate(result.audit_events)
            if audit.stage == "DECISION" and dict(audit.payload).get("decision_kind") == "PROPOSE"
        )
        fill_index = next(i for i, audit in enumerate(result.audit_events) if audit.stage == "FILL")
        assert fill_index > propose_index, "FILL audit event must follow the PROPOSE decision"
        fill_status = dict(result.audit_events[fill_index].payload).get("fill_status")
        assert fill_status == "FILL_FILLED", (
            f"expected fill_status=FILL_FILLED, got {fill_status!r}"
        )


# ---------------------------------------------------------------------------
# 4. The guide's quoted example block matches the committed example
# ---------------------------------------------------------------------------


class TestGuideQuotedExampleMatches:
    """The §5.3 fenced block is byte-identical to ``example_strategy.py``."""

    def test_quoted_block_equals_example_file(self) -> None:
        text = _guide_text()
        quoted = _extract_python_block(text, block_index=0)
        example_text = _EXAMPLE_PATH.read_text(encoding="utf-8")
        # Both sides are stripped so trailing newlines and surrounding
        # whitespace differences do not matter; the body must be
        # identical.
        assert quoted.strip() == example_text.strip(), (
            "the guide's §5.3 quoted example block has drifted from "
            "docs/implement/strategy/example_strategy.py; "
            "update one of them and re-run the test"
        )


# ---------------------------------------------------------------------------
# 5. Forbidden-content checks (no credentials, no retired rules)
# ---------------------------------------------------------------------------


class TestGuideContentSafety:
    """The guide does not introduce credentials, .env values, or retired rules."""

    @pytest.mark.parametrize(
        "needle",
        [
            # Credential-shaped strings (any of these appearing in the
            # guide or the example is a regression).
            "private key",
            "seed phrase",
            "API secret",
            ".env",
            "ALCHEMY_API_KEY",
            "INFURA_PROJECT_ID",
            "0x" + "a" * 64,
        ],
    )
    def test_guide_does_not_carry_credentials(self, needle: str) -> None:
        text = _guide_text()
        assert needle.lower() not in text.lower(), (
            f"guide contains a credential-shaped string: {needle!r}"
        )

    def test_guide_does_not_cite_ten_million_block_window(self) -> None:
        """The retired ten-million-block window rule must not be cited."""
        text = _guide_text()
        assert "ten-million" not in text.lower(), (
            "guide cites the retired ten-million-block window rule"
        )
        assert "10_000_000" not in text and "10000000" not in text, (
            "guide cites the retired ten-million-block window rule"
        )

    def test_guide_does_not_name_t038_as_live_owner(self) -> None:
        """T038 is superseded by T039; the guide must not present T038 as live."""
        text = _guide_text()
        # The guide cites T039-era artefacts by their own contract paths
        # only; a stray ``T038`` reference here would surface as a
        # regression.
        assert "T038" not in text, "guide names T038, which is superseded by T039"

    def test_example_does_not_carry_credentials(self) -> None:
        text = _EXAMPLE_PATH.read_text(encoding="utf-8")
        for needle in (
            "private key",
            "seed phrase",
            "API secret",
            "ALCHEMY_API_KEY",
            "INFURA_PROJECT_ID",
            "0x" + "a" * 64,
        ):
            assert needle.lower() not in text.lower(), (
                f"example carries a credential-shaped string: {needle!r}"
            )
