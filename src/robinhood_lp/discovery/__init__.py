"""Discovery layer (T022-T024).

Re-exports the chain-capability probe, the Initialize event decoder,
the defensive token-metadata reader, the idempotent pool registry,
and the Initialize scanner.

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
from robinhood_lp.discovery.initialize_log import (
    INITIALIZE_TOPIC0,
    DecodedInitialize,
    InitializeDecodeError,
    decode_initialize_log,
)
from robinhood_lp.discovery.registry import (
    InitializeScanner,
    PoolRecord,
    PoolRegistry,
    RegistryConflictError,
    ScannerStats,
)
from robinhood_lp.discovery.token_metadata import (
    TokenMetadataRecord,
    read_token_metadata,
)

__all__ = [
    "BytecodeMismatchError",
    "ChainCapabilityError",
    "ChainCapabilityReport",
    "ChainIdMismatchError",
    "DecodedInitialize",
    "EXPECTED_DEPLOYMENT_PLACEHOLDER",
    "EXPECTED_DEPLOYMENT_PLACEHOLDER",  # see note below
    "ExpectedDeployment",
    "INITIALIZE_TOPIC0",
    "InitializeDecodeError",
    "InitializeScanner",
    "PerEndpointCapability",
    "PoolRecord",
    "PoolRegistry",
    "RegistryConflictError",
    "ScannerStats",
    "TokenMetadataRecord",
    "decode_initialize_log",
    "probe_chain_capability",
    "read_token_metadata",
]


#: Convenience re-export of the well-known Robinhood Chain mainnet
#: V4 deployment fields. The bytecode hashes are intentionally
#: ``None`` here; the T024 capability probe must populate them from a
#: real endpoint before T022 will accept the deployment as verified.
EXPECTED_DEPLOYMENT_PLACEHOLDER = ExpectedDeployment  # alias for clarity
