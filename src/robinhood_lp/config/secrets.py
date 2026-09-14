"""Secret redaction and credential detection.

This module exists so that serialization helpers can be unit-tested in
isolation from the loader. The rules here are deliberately conservative -
any string that resembles a credential-shaped URL fragment, an explicit
``Bearer``/``password``/``secret``/``key`` assignment, or a private key
hex blob is treated as sensitive, even if the loader would have rejected
it earlier.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

#: A URL with ``user:password@`` inside the authority component.
_CREDENTIAL_URL_RE: Final[re.Pattern[str]] = re.compile(r"://[^/\s?#]*:[^/\s?#@]+@")

#: Environment-variable names that look sensitive. Used only for warning
#: output during redaction; matching is intentionally broad to err on the
#: safe side.
_SENSITIVE_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(secret|token|key|password|passwd|credential|auth)"
)

#: 0x-prefixed 64-character hex string (looks like a 32-byte secret
#: blob; could be a 256-bit symmetric key, a 32-byte hash, or similar).
#: We mask any such literal anywhere it appears in error output.
_HEX64_RE: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]{64}\b")

#: ``key=value`` or ``key: value`` substring that names a secret-bearing
#: word (catches ``password=foo``, ``api_secret: bar`` etc. that may leak
#: through exception text). Group 1 is the key name, group 2 is the
#: ``=`` / ``:`` separator.
_KV_SECRET_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|auth[_-]?token|private[_-]?key)"
    r"(?P<sep>\s*[:=]\s*)"
    r"[^\s,;}\]\"']+"
)

#: Standalone ``Bearer <token>`` fragment.
_BEARER_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")


def contains_credential_url(value: str) -> bool:
    """Return True if ``value`` contains a credential-shaped URL fragment."""
    return _CREDENTIAL_URL_RE.search(value) is not None


def looks_like_secret_env_name(name: str) -> bool:
    """Return True if an environment variable name suggests a secret value."""
    return _SENSITIVE_NAME_RE.search(name) is not None


def redact(value: str, *, env_name: str | None = None) -> str:
    """Return a redacted representation of ``value`` for logs and errors.

    Args:
        value: The string to potentially redact.
        env_name: If provided, and the env-var name looks sensitive,
            the value is redacted as well.
    """
    if contains_credential_url(value):
        return "<redacted: credential-bearing URL>"
    if env_name is not None and looks_like_secret_env_name(env_name):
        return "<redacted: secret env-var value>"
    return value


def redact_text(value: str) -> str:
    """Redact every secret-shaped substring inside a free-form text value.

    Used by the loader to scrub exception text that may echo user input
    (such as pydantic ``ValidationError`` messages that include the raw
    field value). Returns ``value`` unchanged if nothing matches.
    """
    out = value
    out = _CREDENTIAL_URL_RE.sub("<redacted: credential-bearing URL>", out)
    out = _BEARER_RE.sub("Bearer <redacted>", out)
    out = _KV_SECRET_RE.sub(
        lambda m: f"{m.group(1)}{m.group('sep')}<redacted>",
        out,
    )
    out = _HEX64_RE.sub("<redacted: hex secret>", out)
    return out


def redact_each(values: Iterable[str], *, env_name: str | None = None) -> list[str]:
    """Apply :func:`redact` to every element of ``values``."""
    return [redact(v, env_name=env_name) for v in values]


__all__ = [
    "contains_credential_url",
    "looks_like_secret_env_name",
    "redact",
    "redact_each",
    "redact_text",
]
