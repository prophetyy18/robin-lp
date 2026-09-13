"""Secret redaction and credential detection.

This module exists so that serialization helpers can be unit-tested in
isolation from the loader. The rules here are deliberately conservative —
any string that resembles a credential-shaped URL fragment is treated as
sensitive, even if the loader would have rejected it earlier.
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


def redact_each(values: Iterable[str], *, env_name: str | None = None) -> list[str]:
    """Apply :func:`redact` to every element of ``values``."""
    return [redact(v, env_name=env_name) for v in values]


__all__ = [
    "contains_credential_url",
    "looks_like_secret_env_name",
    "redact",
    "redact_each",
]
