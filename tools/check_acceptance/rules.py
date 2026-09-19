"""Rule families for the acceptance-criteria check (T009).

Three families are enforced. Each function returns a list of
:class:`Finding`; the orchestrator in :mod:`tools.check_acceptance.checker`
calls all three and combines the result.

1. ``acceptance_present`` — every contract must have a non-empty
   ``## Acceptance`` section that states at least one falsifiable
   condition: a verification surface, an explicit pass/fail outcome,
   or a rejection / refusal / invalid-input condition. The list of
   verification surfaces, explicit-outcome phrases and the rest of the
   closed vocabulary is in :mod:`tools.check_acceptance.vocabulary`.

2. ``acceptance_placeholder`` — the ``## Acceptance`` section must not
   contain any of the closed placeholder markers (``TBD``, ``TODO``,
   ``to be determined``, ``as appropriate``, ``if possible``).

3. ``acceptance_quantitative`` — every comparison phrase (a comparison
   operator from the closed vocabulary followed by a number carrying a
   unit or a percent) that appears in ``## Deliverables`` must also
   appear, verbatim, in ``## Acceptance``. The phrase normalisation
   (whitespace + backtick strip + case fold) is the single place a
   near-miss can resolve, and the closed vocabulary is the single
   place a near-miss can become a match. No rule in this module may
   invent a vocabulary outside :mod:`tools.check_acceptance.vocabulary`.

Each rule returns findings against the *contract*, not the section.
The orchestrator converts the contract path into the relative path the
report prints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .parser import ACCEPTANCE_HEADER, DELIVERABLES_HEADER, Contract
from .types import Finding
from .vocabulary import (
    EXPLICIT_OUTCOME_TERMS,
    PLACEHOLDER_MARKERS,
    VERIFICATION_SURFACES,
    iter_comparison_phrases,
    normalise_phrase,
    surface_match,
)

#: Names of the rule families. The closed set is part of the contract;
#: the orchestrator uses the names both in finding messages and to
#: scope the suppression-unused check so T005 / T006 entries in the
#: shared suppression file are silently ignored here.
RULE_PRESENT = "acceptance-present"
RULE_PLACEHOLDER = "acceptance-placeholder"
RULE_QUANTITATIVE = "acceptance-quantitative"
RULE_FAMILY_NAMES: frozenset[str] = frozenset({RULE_PRESENT, RULE_PLACEHOLDER, RULE_QUANTITATIVE})


def _line_of(text: str, substring: str, *, start: int = 0) -> int:
    """Return the 1-indexed line number of ``substring`` inside ``text``.

    Used to attach a stable line to each finding; a ``substring`` that
    is not present returns 0 so the caller can still produce a finding.
    """

    index = text.find(substring, start)
    if index < 0:
        return 0
    return text.count("\n", 0, index) + 1


def _strip_for_search(section: str) -> str:
    """Return a search-friendly copy of a section.

    Strips backticks (T009's quantitative phrases often live in inline
    code spans) and collapses whitespace runs to a single space. The
    original section is preserved for line numbers; this copy is only
    used for substring searches.
    """

    return re.sub(r"\s+", " ", section.replace("`", " "))


def acceptance_present(contract: Contract) -> list[Finding]:
    """Rule family 1: the contract must declare a falsifiable outcome."""

    body = contract.acceptance
    findings: list[Finding] = []
    if body is None:
        findings.append(
            Finding(
                rule=RULE_PRESENT,
                path=contract.path,
                line=0,
                token="",
                message=(
                    f"contract has no {ACCEPTANCE_HEADER!r} section; the rule its "
                    "Deliverables state is enforced by nobody"
                ),
            )
        )
        return findings
    stripped = body.strip()
    if not stripped:
        findings.append(
            Finding(
                rule=RULE_PRESENT,
                path=contract.path,
                line=0,
                token="",
                message=(
                    f"{ACCEPTANCE_HEADER!r} section is empty; a criterion that says "
                    "nothing cannot decide its own task"
                ),
            )
        )
        return findings
    if surface_match(body, VERIFICATION_SURFACES):
        return findings
    if surface_match(body, EXPLICIT_OUTCOME_TERMS):
        return findings
    # Rejection / refusal / invalid-input conditions are stated in a
    # distinct family of phrases (e.g. "deny-by-default", "must reject
    # missing input"). Surface-match them through the same vocabulary
    # because they are part of the falsifiable-condition guarantee.
    if surface_match(body, _REJECTION_TERMS):
        return findings
    findings.append(
        Finding(
            rule=RULE_PRESENT,
            path=contract.path,
            line=_line_of(contract.text, ACCEPTANCE_HEADER),
            token="",
            message=(
                f"{ACCEPTANCE_HEADER!r} section states no falsifiable condition: "
                "no declared verification surface, no explicit pass/fail statement, "
                "and no rejection / refusal / invalid-input condition"
            ),
        )
    )
    return findings


#: Rejection / refusal / invalid-input phrases the scanner accepts.
#: Living beside the closed vocabulary is intentional: they are part of
#: the closed vocabulary even though the task contract enumerates them
#: in the same clause as the verification surfaces.
_REJECTION_TERMS: tuple[str, ...] = (
    "deny by default",
    "deny-by-default",
    "refuses",
    "rejects",
    "rejection",
    "refusal",
    "refused",
    "rejected",
    "invalid input",
    "invalid-input",
    "invalid inputs",
    "invalid-inputs",
    "missing input",
    "missing-input",
    "stale input",
    "stale-input",
    "unreadable",
    "unparsable",
    "fail closed",
    "fail-closed",
    "fail open",
    "fail-open",
)


def acceptance_placeholder(contract: Contract) -> list[Finding]:
    """Rule family 2: the Acceptance section must contain no placeholders.

    The two acronyms ``TBD`` and ``TODO`` match only the verbatim
    uppercase form, because ``todo/`` is a common repository path and
    ``tbd`` is a valid English word. The prose markers (``to be
    determined``, ``as appropriate``, ``if possible``) match case
    insensitively because their multi-word shape cannot collide with
    a path or filename.
    """

    body = contract.acceptance
    findings: list[Finding] = []
    if body is None or not body.strip():
        return findings
    for marker in PLACEHOLDER_MARKERS:
        if marker in ("TBD", "TODO"):
            pattern = re.compile(rf"\b{marker}\b")
        else:
            pattern = re.compile(re.escape(marker), re.IGNORECASE)
        for match in pattern.finditer(body):
            line = body.count("\n", 0, match.start()) + 1
            findings.append(
                Finding(
                    rule=RULE_PLACEHOLDER,
                    path=contract.path,
                    line=line,
                    token=match.group(0),
                    message=(
                        f"placeholder marker {match.group(0)!r} in {ACCEPTANCE_HEADER!r}; "
                        "a criterion that defers itself is not a criterion"
                    ),
                )
            )
    return findings


@dataclass(frozen=True)
class _Phrase:
    """A quantitative phrase the scanner flagged in a section."""

    raw: str
    normalised: str
    line: int


def _phrases(section: str) -> list[_Phrase]:
    out: list[_Phrase] = []
    for matched, line in iter_comparison_phrases(section):
        out.append(_Phrase(raw=matched, normalised=normalise_phrase(matched), line=line))
    return out


def acceptance_quantitative(contract: Contract) -> list[Finding]:
    """Rule family 3: every quantitative Deliverables rule must be required."""

    findings: list[Finding] = []
    deliverables = contract.deliverables
    acceptance = contract.acceptance
    if deliverables is None or acceptance is None:
        return findings
    # An Acceptance section that fails the ``acceptance-present`` rule
    # must not also generate ``acceptance-quantitative`` findings:
    # the section already failed, and the absence of the section is a
    # more severe defect than a missing quantitative match. The same
    # rule applies to a placeholder-stamped Acceptance — the contract
    # has already failed and a second round of findings would only
    # bury the real one.
    if not acceptance.strip():
        return findings
    for marker in PLACEHOLDER_MARKERS:
        if marker.lower() in acceptance.lower():
            return findings
    deliverable_phrases = _phrases(deliverables)
    if not deliverable_phrases:
        return findings
    acceptance_search = _strip_for_search(acceptance)
    for phrase in deliverable_phrases:
        normalised_phrase = phrase.normalised
        # The Acceptance section must contain the same phrase, after the
        # same normalisation, so a near-miss that the contract would
        # consider "close enough" cannot pass: a threshold stated in
        # Deliverables is a commitment, and a different threshold in
        # Acceptance is a different commitment. The phrase
        # normalisation is the single, audited place a comparison is
        # allowed to match.
        if normalised_phrase in acceptance_search:
            continue
        findings.append(
            Finding(
                rule=RULE_QUANTITATIVE,
                path=contract.path,
                line=phrase.line,
                token=phrase.raw,
                message=(
                    f"{DELIVERABLES_HEADER!r} states a quantitative rule "
                    f"{phrase.raw!r} that {ACCEPTANCE_HEADER!r} never requires "
                    "as a condition on the same quantity with the same direction"
                ),
            )
        )
    return findings


__all__ = [
    "RULE_FAMILY_NAMES",
    "RULE_PLACEHOLDER",
    "RULE_PRESENT",
    "RULE_QUANTITATIVE",
    "acceptance_placeholder",
    "acceptance_present",
    "acceptance_quantitative",
]
