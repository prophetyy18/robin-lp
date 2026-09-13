"""Configuration package.

Exposes the typed configuration models and the loader. The loader is the
only public entry point; everything else is an implementation detail.
"""

from __future__ import annotations

from robinhood_lp.config.loader import ConfigError, load_config
from robinhood_lp.config.models import (
    ChainConfig,
    PoolConfig,
    PoolKey,
    RootConfig,
    RunMode,
    TargetTokenConfig,
)

__all__ = [
    "ChainConfig",
    "ConfigError",
    "PoolConfig",
    "PoolKey",
    "RootConfig",
    "RunMode",
    "TargetTokenConfig",
    "load_config",
]
