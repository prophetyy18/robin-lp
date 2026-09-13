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
    ProjectRisk,
    RootConfig,
    RunMode,
    TargetTokenConfig,
    TechnicalEligibility,
    UserDecision,
)

__all__ = [
    "ChainConfig",
    "ConfigError",
    "PoolConfig",
    "PoolKey",
    "ProjectRisk",
    "RootConfig",
    "RunMode",
    "TargetTokenConfig",
    "TechnicalEligibility",
    "UserDecision",
    "load_config",
]
