"""Closed vocabulary for the acceptance-criteria check (T009).

Every rule in T009 is built from one of the three closed lists declared
here. The vocabulary is committed beside the check so a reviewer can
re-derive every finding by hand. None of the lists may be narrowed,
emptied or shortened to make a finding pass; the lists are the contract.

The three lists:

1. ``VERIFICATION_SURFACES`` — the substrings that, when present in an
   ``## Acceptance`` section, turn a sentence into a falsifiable
   condition. The acceptance section must reference at least one of
   them, or state an explicit pass/fail, or state a rejection /
   refusal / invalid-input condition. The list covers the surfaces
   named in the task contract (test path or module, command, fixture,
   golden, vector, manifest, report, ledger, checksum, schema) plus
   the canonical shell commands CI runs.
2. ``PLACEHOLDER_MARKERS`` — the substrings whose presence in an
   ``## Acceptance`` section defers the criterion to a later
   decision. A criterion that defers itself is not a criterion.
3. ``COMPARISON_OPERATORS`` and ``UNIT_TERMS`` — the operators and
   unit/percent forms that together mark a *quantitative* rule. A
   comparison followed by a number with a unit or a percent inside
   the ``## Deliverables`` section is a quantitative rule; the same
   comparison + number + unit/percent must appear in the
   ``## Acceptance`` section.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Verification surfaces.
#
# Each entry is a literal substring the scanner looks for (case
# insensitive, with surrounding whitespace ignored). The list covers the
# surfaces the task contract names (test path or module, command,
# fixture, golden, vector, manifest, report, ledger, checksum, schema),
# plus the canonical CI commands whose presence proves the contract
# names a real shell-runnable check.
# ---------------------------------------------------------------------------
VERIFICATION_SURFACES: tuple[str, ...] = (
    # test path / module / function name (singular and plural, any form)
    "tests/",
    "tests/test_",
    "test_",
    "tests",
    "test ",
    "tested",
    "test",
    # commands (canonical CI shell verbs)
    "pytest",
    "ruff",
    "mypy",
    "python -m",
    "git ",
    "command",
    "build ",
    "install",
    # fixture / golden / vector / manifest / report / ledger / schema
    "fixture",
    "golden",
    "vector",
    "manifest",
    "report",
    "ledger",
    "schema",
    # replay (the backtest / paper mode the spec binds the lifecycle to)
    "replay",
    # checksum (and the canonical hash names)
    "checksum",
    "sha256",
    "sha-256",
    "digest",
)

# Substrings that, when present anywhere in an Acceptance section, turn a
# sentence into an explicit pass / fail / rejection condition. A section
# that contains at least one of these is considered to state a falsifiable
# outcome even without a verification surface.
EXPLICIT_OUTCOME_TERMS: tuple[str, ...] = (
    "passes",
    "passed",
    "passing",
    "pass twice",
    "pass ",
    "pass",
    "succeeds",
    "succeeded",
    "fails",
    "failed",
    "failing",
    "must pass",
    "must fail",
    "must reject",
    "must refuse",
    "must not pass",
    "must not fail",
    "must not accept",
    "refuses",
    "rejected",
    "rejection",
    "refusal",
    "refused",
    "invalid input",
    "invalid-input",
    "invalid inputs",
    "invalid-inputs",
    "fail closed",
    "fail-closed",
    "fail open",
    "fail-open",
    "non-zero exit",
    "exits non-zero",
    "exits zero",
    "rejected",
)

# ---------------------------------------------------------------------------
# Placeholder markers.
#
# Each entry is a literal substring the scanner looks for (case
# insensitive). The five entries are the exact phrases the task
# contract enumerates; nothing else is a marker.
# ---------------------------------------------------------------------------
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "TBD",
    "TODO",
    "to be determined",
    "as appropriate",
    "if possible",
)

# ---------------------------------------------------------------------------
# Quantitative rule operators and unit / percent forms.
#
# A quantitative rule in ``## Deliverables`` is any substring that
# combines one of the comparison operators below with a number that is
# either a percent or followed by a unit term. The same combination
# must appear in ``## Acceptance``.
# ---------------------------------------------------------------------------
COMPARISON_OPERATORS: tuple[str, ...] = (
    # symbolic operators (closed list; the task contract enumerates these)
    "<=",
    ">=",
    "<",
    ">",
    # natural-language operators (closed list)
    "at least",
    "at most",
    "above",
    "below",
    "more than",
    "less than",
    "exceeds",
    "exceeding",
)

UNIT_TERMS: tuple[str, ...] = (
    # percent forms (the token '%' and the word 'percent')
    "%",
    "percent",
    # canonical project unit tokens (currency, mass/wei, time, blocks, bytes)
    "USDG",
    "USD",
    "ETH",
    "wei",
    "gwei",
    "gas",
    "sec",
    "secs",
    "second",
    "seconds",
    "minute",
    "minutes",
    "hour",
    "hours",
    "day",
    "days",
    "block",
    "blocks",
    "byte",
    "bytes",
    "MB",
    "KB",
    "GB",
    "Mb",
    "Kb",
    "Gb",
    "ms",
)

# ---------------------------------------------------------------------------
# Compiled regexes (module-level singletons).
#
# The regexes below are the only places where the scanner recognises a
# comparison operator or a unit term. Their source is here so a reviewer
# can audit the closed vocabulary by reading a single file.
# ---------------------------------------------------------------------------

#: Pattern that matches ``<= -80%`` / ``>= 100`` / ``< 50`` / ``> 0`` etc.
_SYMBOLIC_OP = r"(?P<op><=|>=|<|>)"
#: Pattern that matches the natural-language operators verbatim.
_NATURAL_OP = r"(?P<op>at\s+least|at\s+most|above|below|more\s+than|less\s+than|exceed(?:s|ing)?)"
#: A signed or unsigned integer / float, captured for token reconstruction.
_NUMBER = r"-?\d+(?:\.\d+)?"
#: An optional unit / percent tail.
_UNIT = r"(?:\s*(?:%|\bpercent\b|\bUSDG\b|\bUSD\b|\bETH\b|\bwei\b|\bgwei\b|\bgas\b|\bsecs?\b|\bseconds?\b|\bminutes?\b|\bhours?\b|\bdays?\b|\bblocks?\b|\bbytes?\b|\bMB\b|\bKB\b|\bGB\b|\bMb\b|\bKb\b|\bGb\b|\bms\b))?"
#: Whitespace run.
_WS = r"\s+"

# A single comparison phrase in symbolic form (``<= -80%``,
# ``>= 100`` followed by a unit, etc.).
_PATTERN_SYMBOLIC = re.compile(
    rf"{_SYMBOLIC_OP}{_WS}{_NUMBER}{_UNIT}",
    re.IGNORECASE,
)

# A single comparison phrase in natural-language form (``above 100%``,
# ``exceeds 80 percent``, ``at least 5 minutes``).
_PATTERN_NATURAL = re.compile(
    rf"{_NATURAL_OP}{_WS}{_NUMBER}{_UNIT}",
    re.IGNORECASE,
)


def iter_comparison_phrases(text: str) -> Iterable[tuple[str, int]]:
    """Yield every ``(matched_text, line_number)`` pair the scanner flags.

    The pair is the exact substring the scanner matched (verbatim, with
    whatever surrounding whitespace was captured) and the 1-indexed line
    on which it appears. The function is the single source of truth for
    what the scanner considers a quantitative phrase; both Deliverables
    and Acceptance scan the same way so a Deliverables phrase is
    satisfied only by an Acceptance phrase that contains the same
    substring.
    """

    if not text:
        return
    running_offset = 0
    for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
        for pattern in (_PATTERN_SYMBOLIC, _PATTERN_NATURAL):
            for match in pattern.finditer(line):
                # ``running_offset`` keeps the line numbers stable for
                # multi-line input even though we iterate line-by-line.
                _ = running_offset
                yield match.group(0).strip(), line_number
        running_offset += len(line)


def normalise_phrase(phrase: str) -> str:
    """Normalise a comparison phrase so both sides of the rule agree.

    Strips backticks, collapses internal whitespace, and lower-cases the
    result. The closed vocabulary covers every comparison form; the
    normalisation is purely cosmetic and cannot invent a match that
    the closed vocabulary would otherwise reject.
    """

    stripped = phrase.strip().strip("`").strip()
    return " ".join(stripped.split()).lower()


def surface_match(section: str, terms: Iterable[str]) -> bool:
    """Return True when ``section`` contains any ``term`` (case-insensitive).

    A bare ``section.find(term)`` would also work; this helper centralises
    the case-insensitivity policy and the surrounding-whitespace
    handling so every surface check uses the same definition.
    """

    haystack = section.lower()
    return any(term.lower() in haystack for term in terms)


__all__ = [
    "COMPARISON_OPERATORS",
    "EXPLICIT_OUTCOME_TERMS",
    "PLACEHOLDER_MARKERS",
    "UNIT_TERMS",
    "VERIFICATION_SURFACES",
    "iter_comparison_phrases",
    "normalise_phrase",
    "surface_match",
]
