"""Regression tests for the T009 deterministic acceptance-criteria checker.

The contract (T009) requires that each of the three rule families has a
seeded regression that *fails* the check when its family is broken; a
check that never fires is indistinguishable from one that passes. The
tests below build a temporary repository under ``tmp_path`` so the
regressions never touch the real working tree, and they exercise every
documented failure mode (missing contract set, empty contract set,
unreadable exception entry, empty scan, missing Acceptance section,
placeholder marker, quantitative-rule mismatch, stale suppression).
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Iterable
from pathlib import Path

import pytest
from tools.check_acceptance import Finding, contract_set, run, vocabulary
from tools.check_acceptance.checker import format_findings

REPO = Path(__file__).resolve().parents[1]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_phase(root: Path, phase: str) -> Path:
    """Create an empty phase directory under ``todo/phases/``."""

    directory = root / "todo" / "phases" / phase
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _make_repo(
    root: Path,
    *,
    extra_contracts: Iterable[tuple[str, str]] | None = None,
    include_suppressions: bool = True,
    suppressions_text: str | None = None,
) -> Path:
    """Create the minimum repository layout the checker expects.

    ``extra_contracts`` is an iterable of ``(filename, body)`` pairs that
    the caller wants inside ``todo/phases/P00-baseline/``. When the
    iterable is None the function still creates the directory so the
    scanner has somewhere to look.
    """

    (root / "todo").mkdir(parents=True, exist_ok=True)
    (root / "todo" / "config.yaml").write_text(
        json.dumps({"schema_version": 1, "tasks": {}}) + "\n", encoding="utf-8"
    )
    phase_dir = _make_phase(root, "P00-baseline")
    if extra_contracts is None:
        # Leave the directory empty: callers can add contracts on
        # demand and exercise the empty-set rule separately.
        pass
    else:
        for name, body in extra_contracts:
            (phase_dir / name).write_text(body, encoding="utf-8")
    if include_suppressions:
        docs_impl = root / "docs" / "implement" / "ci"
        docs_impl.mkdir(parents=True, exist_ok=True)
        if suppressions_text is None:
            suppressions_text = "suppressions = []\n"
        (docs_impl / "suppressions.toml").write_text(suppressions_text, encoding="utf-8")
    return root


def _baseline_contract(name: str = "T001.md") -> str:
    """Return a contract body whose Acceptance satisfies the rule."""

    return textwrap.dedent(
        f"""\
        # {name[:-3]} — Placeholder contract that satisfies T009

        > Phase: `P00`
        > Contract status: `FROZEN_FROM_BASELINE`

        ## Dependencies

        none.

        ## Outcome

        outcome.

        ## Deliverables

        one deliverable.

        ## Acceptance

        tests/test_acceptance.py passes; pytest reports the regression
        test as passing; the check fires when its rule family is broken.

        ## Must not

        do nothing else.
        """
    )


def test_check_passes_on_clean_contract() -> None:
    """A contract whose Acceptance names a test surface is accepted."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(
            root,
            extra_contracts=[("T001.md", _baseline_contract("T001.md"))],
        )
        findings = run(root)
        assert findings == [], format_findings(findings, root)


def test_acceptance_present_seeded_regression() -> None:
    """A contract with no Acceptance section fails the check."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        body = textwrap.dedent(
            """\
            # T001 — Placeholder contract with no Acceptance

            > Phase: `P00`
            > Contract status: `FROZEN_FROM_BASELINE`

            ## Dependencies

            none.

            ## Outcome

            outcome.

            ## Deliverables

            one deliverable.

            ## Must not

            do nothing else.
            """
        )
        _make_repo(root, extra_contracts=[("T001.md", body)])
        findings = run(root)
        rules = {finding.rule for finding in findings}
        assert "acceptance-present" in rules, format_findings(findings, root)


def test_acceptance_empty_seeded_regression() -> None:
    """An empty Acceptance section is a separate, also-fatal failure."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        body = textwrap.dedent(
            """\
            # T001 — Empty Acceptance

            > Phase: `P00`
            > Contract status: `FROZEN_FROM_BASELINE`

            ## Dependencies

            none.

            ## Outcome

            outcome.

            ## Deliverables

            one deliverable.

            ## Acceptance


            ## Must not

            do nothing else.
            """
        )
        _make_repo(root, extra_contracts=[("T001.md", body)])
        findings = run(root)
        rules = {finding.rule for finding in findings}
        assert "acceptance-present" in rules, format_findings(findings, root)


def test_acceptance_placeholder_seeded_regression() -> None:
    """A declared placeholder marker in Acceptance fails the check."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        body = textwrap.dedent(
            """\
            # T001 — Placeholder marker in Acceptance

            > Phase: `P00`
            > Contract status: `FROZEN_FROM_BASELINE`

            ## Dependencies

            none.

            ## Outcome

            outcome.

            ## Deliverables

            one deliverable.

            ## Acceptance

            tests/test_acceptance.py is in place; the TBD criteria are
            to be determined; if possible the team will revisit.

            ## Must not

            do nothing else.
            """
        )
        _make_repo(root, extra_contracts=[("T001.md", body)])
        findings = run(root)
        rules = {finding.rule for finding in findings}
        tokens = {finding.token for finding in findings}
        assert "acceptance-placeholder" in rules, format_findings(findings, root)
        assert "TBD" in tokens
        # ``to be determined`` and ``if possible`` also match.
        assert "to be determined" in tokens
        assert "if possible" in tokens


def test_acceptance_quantitative_seeded_regression() -> None:
    """A quantitative Deliverables rule must be required by Acceptance.

    The two historical cases (5-minute USDG return <= -80 percent and
    a return above 100 percent) are the grounded examples T009 names.
    This regression removes the Acceptance coverage for one of them and
    expects the check to fire.
    """

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        body = textwrap.dedent(
            """\
            # T001 — Quantitative rule without Acceptance coverage

            > Phase: `P00`
            > Contract status: `FROZEN_FROM_BASELINE`

            ## Dependencies

            none.

            ## Outcome

            outcome.

            ## Deliverables

            a complete 5-minute target-token USDG return `<= -80%`
            triggers an emergency circuit breaker, and a complete
            5-minute return above 100% suppresses risk-increasing
            candidates.

            ## Acceptance

            tests/test_acceptance.py is in place; the regression test
            passes when the rule is broken.

            ## Must not

            do nothing else.
            """
        )
        _make_repo(root, extra_contracts=[("T001.md", body)])
        findings = run(root)
        rules = {finding.rule for finding in findings}
        tokens = {finding.token for finding in findings}
        assert "acceptance-quantitative" in rules, format_findings(findings, root)
        assert "<= -80%" in tokens
        assert "above 100%" in tokens


def test_acceptance_quantitative_passes_when_acceptance_covers_rule() -> None:
    """A Deliverables rule mirrored in Acceptance passes the check."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        body = textwrap.dedent(
            """\
            # T001 — Quantitative rule covered by Acceptance

            > Phase: `P00`
            > Contract status: `FROZEN_FROM_BASELINE`

            ## Dependencies

            none.

            ## Outcome

            outcome.

            ## Deliverables

            a complete 5-minute target-token USDG return `<= -80%`
            triggers an emergency circuit breaker.

            ## Acceptance

            tests/test_acceptance.py is in place; the regression test
            exercises the complete 5-minute target-token USDG return
            `<= -80%` as a strategy-independent auto-exit condition.

            ## Must not

            do nothing else.
            """
        )
        _make_repo(root, extra_contracts=[("T001.md", body)])
        findings = run(root)
        assert findings == [], format_findings(findings, root)


def test_missing_contract_set_fails_closed() -> None:
    """No ``todo/phases/`` directory at all is fatal."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(root)
        # Remove the phase directory entirely so the contract set is
        # genuinely empty.
        import shutil

        shutil.rmtree(root / "todo" / "phases")
        findings = run(root)
        assert any(f.rule == "contract-set" for f in findings), format_findings(findings, root)


def test_empty_contract_set_fails_closed() -> None:
    """An empty contract set is fatal; the scanner must not pass."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(root)
        findings = run(root)
        assert any(f.rule == "contract-set" for f in findings), format_findings(findings, root)


def test_unreadable_suppression_entry_fails_closed() -> None:
    """A malformed ``suppressions.toml`` is itself a fatal failure."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(
            root,
            extra_contracts=[("T001.md", _baseline_contract("T001.md"))],
            suppressions_text="this is not valid TOML !!!\n",
        )
        findings = run(root)
        assert any(f.rule == "suppressions" for f in findings), format_findings(findings, root)


def test_stale_suppression_is_fatal() -> None:
    """An entry that no longer matches a finding fails the check."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _make_repo(
            root,
            extra_contracts=[("T001.md", _baseline_contract("T001.md"))],
            suppressions_text=textwrap.dedent(
                """\
                [[suppression]]
                rule = "acceptance-quantitative"
                reason = "stale entry that no longer matches anything"
                owner = "@dev"
                expiry = "2026-12-31"
                location = "todo/phases/P00-baseline/T001.md"
                token = "above 999%"
                """
            ),
        )
        findings = run(root)
        assert any(f.rule == "suppression-unused" and "above 999%" in f.token for f in findings), (
            format_findings(findings, root)
        )


def test_unused_placeholder_marker_is_a_finding() -> None:
    """A placeholder marker is recorded as a finding with line and token."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        body = textwrap.dedent(
            """\
            # T001 — TODO marker

            > Phase: `P00`
            > Contract status: `FROZEN_FROM_BASELINE`

            ## Dependencies

            none.

            ## Outcome

            outcome.

            ## Deliverables

            one deliverable.

            ## Acceptance

            tests/test_acceptance.py is in place; the TODO list is
            to be determined; ``as appropriate`` we revisit.

            ## Must not

            do nothing else.
            """
        )
        _make_repo(root, extra_contracts=[("T001.md", body)])
        findings = run(root)
        placeholder = [f for f in findings if f.rule == "acceptance-placeholder"]
        assert placeholder, format_findings(findings, root)
        # The scanner records line and matched text for every finding.
        assert all(f.line > 0 for f in placeholder)
        assert {f.token for f in placeholder} >= {"TODO", "to be determined", "as appropriate"}


def test_acceptance_present_records_section_and_line() -> None:
    """A missing Acceptance finding names the contract and the line."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        body = textwrap.dedent(
            """\
            # T001 — No Acceptance

            > Phase: `P00`
            > Contract status: `FROZEN_FROM_BASELINE`

            ## Dependencies

            none.

            ## Outcome

            outcome.

            ## Deliverables

            one deliverable.

            ## Must not

            do nothing else.
            """
        )
        _make_repo(root, extra_contracts=[("T001.md", body)])
        findings = run(root)
        present = [f for f in findings if f.rule == "acceptance-present"]
        assert present, format_findings(findings, root)
        # The finding names the contract (path) and an explanatory
        # message; both must be non-empty.
        for finding in present:
            assert finding.path
            assert finding.message


def test_acceptance_quantitative_records_matched_text() -> None:
    """A quantitative finding records the verbatim matched text."""

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        body = textwrap.dedent(
            """\
            # T001 — Quantitative mismatch with matched text

            > Phase: `P00`
            > Contract status: `FROZEN_FROM_BASELINE`

            ## Dependencies

            none.

            ## Outcome

            outcome.

            ## Deliverables

            completes a run when the 5-minute return exceeds 50% with
            95% confidence.

            ## Acceptance

            tests/test_acceptance.py is in place; the regression test
            passes when the rule is broken.

            ## Must not

            do nothing else.
            """
        )
        _make_repo(root, extra_contracts=[("T001.md", body)])
        findings = run(root)
        quantitative = [f for f in findings if f.rule == "acceptance-quantitative"]
        assert quantitative, format_findings(findings, root)
        # The token is the verbatim matched phrase; the line is the
        # Deliverables line it was found on.
        for finding in quantitative:
            assert finding.token
            assert finding.line > 0


def test_contract_set_covers_required_roots() -> None:
    """The declared contract set covers every task contract."""

    paths = contract_set(REPO)
    rel = {path.resolve().relative_to(REPO.resolve()).as_posix() for path in paths}
    # Spot-check a handful of contracts from each phase.
    assert "todo/phases/P00-engineering-baseline/T005.md" in rel
    assert "todo/phases/P00-engineering-baseline/T009.md" in rel
    assert "todo/phases/P06-backtesting-and-strategy/T065.md" in rel
    assert "todo/phases/P07-risk-and-paper/T070.md" in rel
    assert "todo/phases/P10-research-and-models/T104.md" in rel


def test_check_passes_on_real_repository() -> None:
    """The committed repository passes the acceptance check.

    Pre-existing findings against binding documents are recorded in
    ``docs/implement/ci/suppressions.toml``; the check must pass without
    any new findings or unused entries.
    """

    findings = run(REPO)
    assert findings == [], format_findings(findings, REPO)


def test_finding_dataclass_supports_set_membership() -> None:
    """The ``Finding`` dataclass is hashable so duplicate findings collapse."""

    finding = Finding(
        rule="acceptance-present",
        path="todo/phases/P00-engineering-baseline/T001.md",
        line=10,
        token="",
        message="missing",
    )
    bucket = {finding, finding}
    assert len(bucket) == 1


def test_format_findings_prints_vocabulary() -> None:
    """The formatter prints the closed vocabulary with the findings."""

    text = format_findings([], REPO)
    vocab = vocabulary()
    assert "Declared closed vocabulary" in text
    for surface in vocab.verification_surfaces:
        assert surface in text
    for marker in vocab.placeholder_markers:
        assert marker in text
    for op in vocab.comparison_operators:
        assert op in text


def test_format_findings_handles_empty() -> None:
    """An empty finding list still prints the vocabulary and the pass marker."""

    text = format_findings([], REPO)
    assert "passed" in text


def test_vocabulary_lists_match_contract() -> None:
    """The committed closed vocabulary matches the task contract exactly.

    T009 enumerates: ``TBD``, ``TODO``, ``to be determined``, ``as
    appropriate``, ``if possible``; ``<=``, ``>=``, ``<``, ``>``, ``at
    least``, ``at most``, ``above``, ``below``, ``more than``, ``less
    than``, ``exceeds`` (plus ``exceeding`` as its present-participle).
    The check must reject any future contract that moves a case out of
    either list.
    """

    vocab = vocabulary()
    assert "TBD" in vocab.placeholder_markers
    assert "TODO" in vocab.placeholder_markers
    assert "to be determined" in vocab.placeholder_markers
    assert "as appropriate" in vocab.placeholder_markers
    assert "if possible" in vocab.placeholder_markers
    for op in (
        "<=",
        ">=",
        "<",
        ">",
        "at least",
        "at most",
        "above",
        "below",
        "more than",
        "less than",
        "exceeds",
    ):
        assert op in vocab.comparison_operators
    # The committed vocabulary also names the canonical verification
    # surfaces the contract enumerates (a test path or module, a
    # command, a fixture, golden, vector, manifest, report, ledger,
    # checksum or schema).
    for surface in (
        "tests/",
        "test_",
        "fixture",
        "golden",
        "vector",
        "manifest",
        "report",
        "ledger",
        "checksum",
        "schema",
    ):
        assert surface in vocab.verification_surfaces


@pytest.mark.parametrize(
    "phrase",
    [
        "<= -80%",
        "above 100%",
        "at least 5 minutes",
        "exceeds 80 percent",
        "more than 100 USDG",
        "below 50 ms",
    ],
)
def test_comparison_phrase_extraction(phrase: str) -> None:
    """The phrase extraction handles every grounded quantitative form."""

    from tools.check_acceptance.vocabulary import iter_comparison_phrases, normalise_phrase

    matches = list(iter_comparison_phrases(phrase))
    assert matches, phrase
    assert normalise_phrase(matches[0][0]) == normalise_phrase(phrase)
