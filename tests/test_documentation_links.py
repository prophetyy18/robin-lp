"""Repository-level check that relative Markdown links resolve.

This exists because the gate that was supposed to catch stale document paths could
not catch them. `test_workflow_contracts.py::test_binding_documents_use_the_new_layer_paths`
asserts that a handful of literal path prefixes from the pre-reorganisation layout
never appear -- and the prefixes it names are spelled relative to the repository root,
so they are not substrings of the links that actually broke. Those links were written
relative to the document that contained them, and neither their text nor their shape
contained anything the older gate looked for. It stayed silent while seven links in
the goal document pointed at nothing.

A test that enumerates the shapes a mistake must not take will always miss the next
shape. Resolving every link against the filesystem catches the class instead.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

# Vendored third-party sources, excluded from every repository rule.
# Retained evidence and review records are immutable history: a reference in them to
# a path that existed when they were written is correct, not a broken link. The
# legacy directory still cites docs/implement/STATUS.md, which was deliberately
# deleted, and editing those records to hide that would destroy the history the
# deletion itself relies on.
_EXCLUDED_PREFIXES = (
    "tools/oracle/lib/",
    "todo/evidence/legacy/",
)


def _tracked_documents() -> list[Path]:
    documents = []
    for path in sorted(ROOT.rglob("*.md")):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(".git/") or relative.startswith(_EXCLUDED_PREFIXES):
            continue
        documents.append(path)
    return documents


def test_every_relative_markdown_link_resolves() -> None:
    broken: list[str] = []
    for path in _tracked_documents():
        text = path.read_text(encoding="utf-8")
        for match in _LINK.finditer(text):
            target = match.group(1).split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if (path.parent / target).resolve().exists():
                continue
            line = text[: match.start()].count("\n") + 1
            broken.append(f"{path.relative_to(ROOT).as_posix()}:{line} -> {target}")

    assert not broken, "unresolved relative Markdown links:\n" + "\n".join(broken)


def test_the_check_can_actually_fail() -> None:
    """A link checker that never fires is indistinguishable from one that passes.

    The gate this replaces was silent for months precisely because nobody proved it
    could fail. This asserts the detection works on a link shaped exactly like the
    ones that went unnoticed."""

    def resolves(document: Path, target: str) -> bool:
        return (document.parent / target).resolve().exists()

    document = ROOT / "docs" / "intent" / "PROJECT_GOALS.md"
    assert resolves(document, "../spec/strategy/STRATEGY_ECONOMICS.md")
    assert not resolves(document, "../specs/STRATEGY_ECONOMICS.md")
    assert not resolves(document, "./ASSET_ADMISSION.md")
