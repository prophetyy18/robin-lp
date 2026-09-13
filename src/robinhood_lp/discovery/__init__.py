"""Discovery layer (T022-T024).

Re-exports the chain-capability probe. The pool-discovery scanner
(T022) and the eligibility classifier (T023) will be added here in
follow-up commits; for now this package hosts only T024.

This layer sits in the storage layer per ADR-006 and depends on
``robinhood_lp.protocol`` and ``robinhood_lp.rpc``. It must not
import ``robinhood_lp.config`` or higher layers.
"""

from __future__ import annotations

from robinhood_lp.discovery.chain_capability import (
    BytecodeMismatchError,
    ChainCapabilityError,
    ChainCapabilityReport,
    ChainIdMismatchError,
    ExpectedDeployment,
    PerEndpointCapability,
    probe_chain_capability,
)

__all__ = [
    "BytecodeMismatchError",
    "ChainCapabilityError",
    "ChainCapabilityReport",
    "ChainIdMismatchError",
    "ExpectedDeployment",
    "PerEndpointCapability",
    "probe_chain_capability",
]
