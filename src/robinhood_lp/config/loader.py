"""Configuration loader.

Reads a TOML file from disk, parses it through the strict models defined in
:mod:`robinhood_lp.config.models`, and returns a fully validated
:class:`RootConfig`.

The loader never logs or echoes the contents of secret-bearing fields.
All exceptions raised from the loader boundary have already been passed
through :func:`robinhood_lp.config.secrets.redact_text` so that pydantic
``ValidationError`` output, which echoes raw user input, cannot leak
credentials or other secret values into operator logs.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

from robinhood_lp.config.models import RootConfig
from robinhood_lp.config.secrets import (
    contains_credential_url,
    redact,
    redact_text,
)

if sys.version_info >= (3, 13):
    _tomllib_loads = tomllib.loads
else:
    _tomllib_loads = tomllib.loads  # 3.12 has tomllib in stdlib


class ConfigError(ValueError):
    """Raised when configuration parsing, validation, or secrets fail."""


def _check_no_credential_urls(payload: object, *, path: str = "") -> None:
    """Walk a parsed TOML payload and reject any string that looks like a credential URL."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            _check_no_credential_urls(v, path=f"{path}.{k}" if path else str(k))
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            _check_no_credential_urls(v, path=f"{path}[{i}]")
    elif isinstance(payload, str) and contains_credential_url(payload):
        raise ConfigError(
            f"{path}: contains a credential-shaped URL fragment; "
            f"secrets must come from environment variables, not config files"
        )


def _check_declared_env_vars(config: RootConfig) -> None:
    """Warn (not fail) if a referenced env var is unset and looks secret-shaped.

    Missing env vars are not a hard error at load time: ``load_config`` is
    permitted to return successfully and let the caller fail when the RPC
    adapter is first invoked. This function only emits a redacted notice so
    operators see the situation in their stderr.
    """
    for chain in config.chains:
        env_name = chain.rpc_url_env
        value = os.environ.get(env_name)
        if value is None:
            print(
                f"warning: env var {env_name!r} (chain_id={chain.chain_id}) "
                f"is not set; RPC operations will fail until it is",
                file=sys.stderr,
            )
            continue
        if contains_credential_url(value):
            # The value is shaped like a credential URL. The framework does
            # not reject this: secrets may legitimately include credentials.
            # We just record that we noticed it, redacted.
            print(
                f"notice: env var {env_name!r} (chain_id={chain.chain_id}) "
                f"contains a credential-shaped URL ({redact(value, env_name=env_name)})",
                file=sys.stderr,
            )


def load_config(path: str | Path, *, check_env: bool = True) -> RootConfig:
    """Load and validate a configuration file.

    Args:
        path: TOML file path.
        check_env: When True, emit redacted warnings about missing or
            credential-bearing environment variables referenced by chains.

    Returns:
        A validated :class:`RootConfig`.

    Raises:
        ConfigError: if the file is missing, malformed, contains a
            credential-shaped URL, or fails strict-model validation.
            The error message is scrubbed of secret-shaped substrings at
            the loader boundary so that downstream logging cannot leak
            credentials embedded in user-supplied input.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise ConfigError(f"config file not found: {file_path}")

    raw_bytes = file_path.read_bytes()
    try:
        payload = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {file_path}: {redact_text(str(e))}") from e

    _check_no_credential_urls(payload)

    try:
        config = RootConfig.model_validate(payload)
    except Exception as e:
        # pydantic's ValidationError echoes the raw user input into both
        # ``str(e)`` and the per-error ``msg``/``input`` fields. We pass
        # the whole rendered message through ``redact_text`` so that any
        # credential URL or secret-shaped substring is masked before it
        # reaches the operator's logs.
        raise ConfigError(f"config validation failed for {file_path}: {redact_text(str(e))}") from e

    if check_env:
        _check_declared_env_vars(config)

    return config


def list_pool_identities(config: RootConfig) -> Iterable[tuple[int, str, str, int, int, str]]:
    """Return the canonical ``(chain_id, currency0, currency1, fee, tick_spacing, hooks)``
    tuple for every configured pool. Useful for audit logs and duplicate detection.
    """
    for p in config.pools:
        pk = p.pool_key
        yield (
            p.chain_id,
            pk.currency0,
            pk.currency1,
            pk.fee,
            pk.tick_spacing,
            pk.hooks,
        )


__all__ = ["ConfigError", "list_pool_identities", "load_config"]
