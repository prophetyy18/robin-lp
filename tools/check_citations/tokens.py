"""Extract backticked citation tokens from Markdown source.

Tokens arrive with their source path, 1-indexed line number, and the exact
text between the backticks. Brace-expanded tokens (``a.{b,c}``) are split
into their alternatives so each alternative can be resolved independently.
Tokens that contain a space (multi-word phrases) are skipped because every
resolver in this package expects an identifier-shaped token.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Match a single backticked run on a line; the run cannot span lines.
_BACKTICK = re.compile(r"`([^`\n]+)`")


@dataclass(frozen=True)
class Token:
    """A backticked token plus its source location."""

    path: Path
    line: int
    text: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.path}:{self.line}:{self.text}"


def iter_tokens(path: Path) -> Iterator[Token]:
    """Yield each backticked token in ``path``.

    Tokens that contain whitespace, or that resolve to the empty string after
    stripping, are skipped. The location is the 1-indexed line number where
    the opening backtick appears.
    """

    text = path.read_text(encoding="utf-8")
    for match in _BACKTICK.finditer(text):
        raw = match.group(1).strip()
        if not raw or any(ch.isspace() for ch in raw):
            continue
        line = text.count("\n", 0, match.start()) + 1
        for alternative in _expand_braces(raw):
            yield Token(path=path, line=line, text=alternative)


def _expand_braces(token: str) -> list[str]:
    """Expand ``a.{b,c,d}`` into ``[a.b, a.c, a.d]``.

    Nested braces are not produced by the repository's documents; the
    expansion handles one level only. A token that contains braces but no
    comma inside the brace group is left as-is, because it is then a
    legitimate literal name (for example a shell command with ``{``).
    """

    match = re.search(r"\{([^{}]+)\}", token)
    if not match:
        return [token]
    inner = match.group(1)
    if "," not in inner:
        return [token]
    head = token[: match.start()]
    tail = token[match.end() :]
    alternatives = [piece.strip() for piece in inner.split(",") if piece.strip()]
    return [f"{head}{piece}{tail}" for piece in alternatives]


__all__ = ["Token", "iter_tokens"]
